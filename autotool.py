import ctypes
import sys
import os
import time
import threading
import random
import struct
import subprocess
import csv
import json
import cv2
import numpy as np
from PIL import ImageGrab

import pyautogui
import keyboard
from typing import Optional, Tuple


def is_admin():
    try:
        return ctypes.windll.kernel32.GetCurrentProcessId() and \
                ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


if not is_admin() and '--elevated' not in sys.argv:
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, 
                                        f'"{sys.argv[0]}" --elevated', None, 1)
    sys.exit()


print("Hello meow - Feature-based detection mode")

TARGET_NAME = os.path.join(os.getcwd(), "current_target.png")
SCREENSHOTS_DIR = os.path.join(os.getcwd(), "screenshots")
PROCESS_NAME = "metin2client.bin"
PROCESS_OFFSETS_PATH = os.path.join(os.getcwd(), "metin2_offsets.json")
stop_event = threading.Event()
search_thread = None
lock = threading.Lock()

# Click blacklist
last_clicked_position: Optional[Tuple[float, float]] = None  # (x, y)
blacklist_radius = 60  # Don't click anything within 60px of last click

# Health bar detection
HEALTH_BAR_X1, HEALTH_BAR_Y1 = 630, 50
HEALTH_BAR_X2, HEALTH_BAR_Y2 = 990, 80
last_enemy_name = ""
last_enemy_check_time = 0
enemy_name_update_interval = 2.0  # Update every 2 seconds
in_combat = False  # Track if we've clicked on an enemy
process_offsets_cache = None
process_offsets_mtime = None
process_pid_cache = None
process_last_lookup = 0.0
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
CLICK_JITTER_PX = 1.0
TARGET_LOCK_TIMEOUT_SECONDS = 1.2

# Tunable parameters
MATCH_THRESHOLD = 10  # Minimum number of feature matches (higher = stricter)
SCALES = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]  # Try different scales
USE_COLOR_DETECTION = True  # Also use dominant color for extra filtering
COLOR_TOLERANCE = 50  # How much color variation to allow (0-255)

pyautogui.FAILSAFE = True


def _is_image_file(filename: str) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    return ext in {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def load_templates():
    templates = []

    if os.path.isdir(SCREENSHOTS_DIR):
        for filename in sorted(os.listdir(SCREENSHOTS_DIR)):
            if not _is_image_file(filename):
                continue
            full_path = os.path.join(SCREENSHOTS_DIR, filename)
            template_cv = cv2.imread(full_path)
            if template_cv is None:
                print(f"Skipping unreadable template: {full_path}")
                continue
            h, w = template_cv.shape[:2]
            templates.append({
                "name": filename,
                "path": full_path,
                "cv": template_cv,
                "size": (w, h)
            })

    if os.path.exists(TARGET_NAME):
        template_cv = cv2.imread(TARGET_NAME)
        if template_cv is not None:
            h, w = template_cv.shape[:2]
            templates.append({
                "name": os.path.basename(TARGET_NAME),
                "path": TARGET_NAME,
                "cv": template_cv,
                "size": (w, h)
            })

    return templates


def get_metin2_pid():
    """Return PID for metin2client.bin, caching the last successful lookup for 2 seconds."""
    global process_pid_cache, process_last_lookup
    if not sys.platform.startswith("win"):
        return 0

    now = time.time()
    if process_pid_cache is not None and process_pid_cache > 0 and (now - process_last_lookup) < 2.0:
        return process_pid_cache

    process_last_lookup = now
    process_pid_cache = 0
    try:
        output = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL,
            text=True
        )
        reader = csv.reader(output.splitlines())
        for row in reader:
            if len(row) < 2:
                continue
            image_name = row[0].strip().lower()
            if image_name == PROCESS_NAME.lower():
                pid_value = row[1].strip().replace(",", "")
                try:
                    process_pid_cache = int(pid_value)
                    return process_pid_cache
                except ValueError:
                    continue
    except Exception:
        return 0

    return 0


def _parse_address(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        value = value.strip()
        if value.lower().startswith("0x"):
            return int(value, 16)
        return int(value)
    return None


def load_process_offsets():
    global process_offsets_cache, process_offsets_mtime
    try:
        if not os.path.exists(PROCESS_OFFSETS_PATH):
            process_offsets_cache = None
            process_offsets_mtime = None
            return None

        mtime = os.path.getmtime(PROCESS_OFFSETS_PATH)
        if process_offsets_cache is not None and process_offsets_mtime == mtime:
            return process_offsets_cache

        with open(PROCESS_OFFSETS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)

        parsed = {
            "x": _parse_address(raw.get("target_x_address")),
            "y": _parse_address(raw.get("target_y_address")),
            "valid": _parse_address(raw.get("target_valid_address")),
            "valid_min": int(raw.get("target_valid_min", 1)),
            "x_type": str(raw.get("target_x_type", "float")).lower(),
            "y_type": str(raw.get("target_y_type", "float")).lower(),
        }
        if parsed["x"] is None or parsed["y"] is None:
            process_offsets_cache = None
            process_offsets_mtime = mtime
            return None

        process_offsets_cache = parsed
        process_offsets_mtime = mtime
        print(f"Loaded process offsets from {PROCESS_OFFSETS_PATH}")
        return process_offsets_cache
    except Exception as e:
        print(f"Failed to load process offsets: {e}")
        process_offsets_cache = None
        process_offsets_mtime = None
        return None


def _read_process_value(process_handle, address, value_type):
    """Read a 4-byte value from process memory as int/uint/float, or return None on failure."""
    size = 4
    buffer = ctypes.create_string_buffer(size)
    bytes_read = ctypes.c_size_t(0)
    ok = ctypes.windll.kernel32.ReadProcessMemory(
        process_handle,
        ctypes.c_void_p(address),
        buffer,
        size,
        ctypes.byref(bytes_read)
    )
    if not ok or bytes_read.value != size:
        return None

    raw = buffer.raw
    if value_type == "int":
        return struct.unpack("<i", raw)[0]
    if value_type == "uint":
        return struct.unpack("<I", raw)[0]
    return struct.unpack("<f", raw)[0]


def read_process_target_position():
    """Read target screen position from metin2client memory and return (x, y) or None."""
    offsets = load_process_offsets()
    if not offsets:
        return None

    pid = get_metin2_pid()
    if not pid:
        return None

    process_handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_VM_READ | PROCESS_QUERY_INFORMATION,
        False,
        pid
    )
    if not process_handle:
        return None

    try:
        if offsets.get("valid") is not None:
            valid = _read_process_value(process_handle, offsets["valid"], "int")
            if valid is None or valid < offsets.get("valid_min", 1):
                return None

        x = _read_process_value(process_handle, offsets["x"], offsets.get("x_type", "float"))
        y = _read_process_value(process_handle, offsets["y"], offsets.get("y_type", "float"))
        if x is None or y is None:
            return None

        screen_w, screen_h = pyautogui.size()
        if not (0 <= x < screen_w and 0 <= y < screen_h):
            return None

        return float(x), float(y)
    except Exception:
        return None
    finally:
        ctypes.windll.kernel32.CloseHandle(process_handle)


def _is_blacklisted(x, y):
    if not last_clicked_position:
        return False
    dist = _distance(x, y, last_clicked_position[0], last_clicked_position[1])
    return dist <= blacklist_radius


def _distance(x1, y1, x2, y2):
    return ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5


def _confirm_target_lock(x, y, timeout=1.0):
    start_time = time.time()
    while time.time() - start_time < timeout and not stop_event.is_set():
        screenshot_check = ImageGrab.grab()
        screenshot_check_cv = cv2.cvtColor(np.array(screenshot_check), cv2.COLOR_RGB2BGR)

        if detect_red_circle(screenshot_check_cv, x, y, radius=70):
            return True

        process_pos = read_process_target_position()
        if process_pos is not None:
            px, py = process_pos
            if _distance(px, py, x, y) <= 35:
                return True

        time.sleep(0.08)

    return False


def get_dominant_color(img):
    """Get the dominant color from image center region (excluding edges)"""
    h, w = img.shape[:2]
    # Get center 30% of image
    center_h_start = int(h * 0.35)
    center_h_end = int(h * 0.65)
    center_w_start = int(w * 0.35)
    center_w_end = int(w * 0.65)
    
    center_region = img[center_h_start:center_h_end, center_w_start:center_w_end]
    
    # Get average color in center
    avg_color = cv2.mean(center_region)
    return np.array(avg_color[:3], dtype=np.uint8)


def detect_red_circle(screenshot_cv, x, y, radius=40):
    """Detect if there's a red circle around the target position"""
    try:
        # Convert to HSV
        hsv = cv2.cvtColor(screenshot_cv, cv2.COLOR_BGR2HSV)
        
        # Red color range in HSV (red wraps around, so we check both ends)
        # Lower red range
        lower_red1 = np.array([0, 100, 100])
        upper_red1 = np.array([10, 255, 255])
        
        # Upper red range
        lower_red2 = np.array([170, 100, 100])
        upper_red2 = np.array([180, 255, 255])
        
        mask1 = cv2.inRange(hsv, lower_red1, upper_red1)
        mask2 = cv2.inRange(hsv, lower_red2, upper_red2)
        red_mask = cv2.bitwise_or(mask1, mask2)
        
        # Check for red pixels in a circle around the target
        h, w = screenshot_cv.shape[:2]
        y_min = max(0, int(y - radius))
        y_max = min(h, int(y + radius))
        x_min = max(0, int(x - radius))
        x_max = min(w, int(x + radius))
        
        roi = red_mask[y_min:y_max, x_min:x_max]
        red_pixel_count = cv2.countNonZero(roi)
        
        # If we find enough red pixels (at least 20), assume circle is present
        has_red_circle = red_pixel_count > 20
        
        if has_red_circle:
            print(f"✓ Red circle detected ({red_pixel_count} red pixels)")
        else:
            print(f"✗ No red circle found ({red_pixel_count} red pixels)")
        
        return has_red_circle
    except Exception as e:
        print(f"Error detecting red circle: {e}")
        return False


def detect_health_bar(screenshot_cv):
    """Detect if an enemy health bar appears at the top of the screen"""
    try:
        # Check top portion of screen (roughly top 100 pixels)
        h, w = screenshot_cv.shape[:2]
        top_region = screenshot_cv[0:min(100, h), :]
        
        # Health bar is typically green/red colored
        hsv = cv2.cvtColor(top_region, cv2.COLOR_BGR2HSV)
        
        # Green range (for health bar)
        lower_green = np.array([35, 100, 100])
        upper_green = np.array([85, 255, 255])
        green_mask = cv2.inRange(hsv, lower_green, upper_green)
        
        # Red range (for damaged health bar)
        lower_red1 = np.array([0, 100, 100])
        upper_red1 = np.array([10, 255, 255])
        lower_red2 = np.array([170, 100, 100])
        upper_red2 = np.array([180, 255, 255])
        red_mask1 = cv2.inRange(hsv, lower_red1, upper_red1)
        red_mask2 = cv2.inRange(hsv, lower_red2, upper_red2)
        red_mask = cv2.bitwise_or(red_mask1, red_mask2)
        
        # Combine masks
        health_mask = cv2.bitwise_or(green_mask, red_mask)
        pixel_count = cv2.countNonZero(health_mask)
        
        # If we find enough health bar pixels, consider it present
        has_health_bar = pixel_count > 50
        
        if has_health_bar:
            print(f"✓ Health bar detected at top ({pixel_count} pixels)")
        else:
            print(f"✗ No health bar detected ({pixel_count} pixels)")
        
        return has_health_bar
    except Exception as e:
        print(f"Error detecting health bar: {e}")
        return False


def detect_enemy_health_bar(screenshot_cv):
    """Detect if an enemy health bar appears in the specific area (630,50 to 990,80)
    Also tries to extract enemy name from the region above the health bar"""
    try:
        h, w = screenshot_cv.shape[:2]
        
        # Validate coordinates are within bounds
        x1 = max(0, min(HEALTH_BAR_X1, w))
        y1 = max(0, min(HEALTH_BAR_Y1, h))
        x2 = max(0, min(HEALTH_BAR_X2, w))
        y2 = max(0, min(HEALTH_BAR_Y2, h))
        
        # Extract the health bar region
        health_region = screenshot_cv[y1:y2, x1:x2]
        
        if health_region.size == 0:
            return False, ""
        
        # Convert to HSV for color detection
        hsv = cv2.cvtColor(health_region, cv2.COLOR_BGR2HSV)
        
        # Red range (red health bar indicating enemy is being attacked)
        lower_red1 = np.array([0, 100, 100])
        upper_red1 = np.array([10, 255, 255])
        lower_red2 = np.array([170, 100, 100])
        upper_red2 = np.array([180, 255, 255])
        
        red_mask1 = cv2.inRange(hsv, lower_red1, upper_red1)
        red_mask2 = cv2.inRange(hsv, lower_red2, upper_red2)
        red_mask = cv2.bitwise_or(red_mask1, red_mask2)
        
        # Count red pixels in the health bar area
        red_pixel_count = cv2.countNonZero(red_mask)
        has_red_health_bar = red_pixel_count > 20
        
        enemy_name = ""
        
        # Try to extract enemy name from region above health bar
        if has_red_health_bar:
            # Get region above health bar for name (y: 20-50, x: 630-990)
            name_y1 = max(0, y1 - 30)
            name_y2 = y1
            name_region = screenshot_cv[name_y1:name_y2, x1:x2]
            
            if name_region.size > 0:
                # Convert to grayscale for text detection
                gray = cv2.cvtColor(name_region, cv2.COLOR_BGR2GRAY)
                
                # Apply threshold to get text areas
                _, binary = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY_INV)
                
                # Find contours (text regions)
                contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                
                # Sort contours left to right
                contours = sorted(contours, key=lambda c: cv2.boundingRect(c)[0])
                
                # Try to extract text from contours
                text_chars = []
                for contour in contours:
                    area = cv2.contourArea(contour)
                    if 20 < area < 500:  # Filter by reasonable text size
                        text_chars.append(contour)
                
                if text_chars:
                    enemy_name = f"Enemy detected ({len(text_chars)} text regions)"
        
        return has_red_health_bar, enemy_name
        
    except Exception as e:
        print(f"Error detecting enemy health bar: {e}")
        return False, ""


def color_distance(color1, color2):
    """Calculate Euclidean distance between two colors (BGR)"""
    return np.sqrt(np.sum((color1.astype(float) - color2.astype(float))**2))


def find_matches_orb(screenshot_cv, template_cv, scales=None):
    """Find matches using multi-scale template matching (no ORB needed)"""
    if scales is None:
        scales = [1.0]
    
    try:
        screenshot_gray = cv2.cvtColor(screenshot_cv, cv2.COLOR_BGR2GRAY)
        template_gray = cv2.cvtColor(template_cv, cv2.COLOR_BGR2GRAY)
        
        matches_list = []
        
        # Try different scales
        for scale in scales:
            if scale == 1.0:
                template_to_match = template_gray
            else:
                h, w = template_gray.shape
                new_w = int(w * scale)
                new_h = int(h * scale)
                if new_w > 0 and new_h > 0:
                    template_to_match = cv2.resize(template_gray, (new_w, new_h))
                else:
                    continue
            
            # Single best matching method - more lenient
            try:
                result = cv2.matchTemplate(screenshot_gray, template_to_match, cv2.TM_CCOEFF_NORMED)
                
                # Find matches above LOW threshold
                matches = np.where(result >= 0.4)  # Very lenient - was 0.6
                
                for pt in zip(*matches[::-1]):
                    confidence = float(result[pt[1], pt[0]])
                    matches_list.append({
                        'x': pt[0],
                        'y': pt[1],
                        'confidence': confidence,
                        'scale': scale
                    })
            except Exception as e:
                print(f"Scale {scale} error: {e}")
                pass
        
        # Remove duplicate matches (within 40 pixels)
        unique_matches = []
        for match in sorted(matches_list, key=lambda m: m['confidence'], reverse=True):
            is_duplicate = False
            for existing in unique_matches:
                dist = _distance(match['x'], match['y'], existing['x'], existing['y'])
                if dist < 40:
                    is_duplicate = True
                    break
            if not is_duplicate:
                unique_matches.append(match)
        
        if unique_matches:
            print(f"Found {len(unique_matches)} pattern matches (best: {unique_matches[0]['confidence']:.3f})")
        
        return unique_matches
            
    except Exception as e:
        print(f"Error in pattern matching: {e}")
        return []


def find_matches_color(screenshot_cv, template_cv, tolerance=40):
    """Find regions with similar color to target center, filtering out bright textures"""
    try:
        # Convert to HSV for better color matching
        screenshot_hsv = cv2.cvtColor(screenshot_cv, cv2.COLOR_BGR2HSV)
        template_hsv = cv2.cvtColor(template_cv, cv2.COLOR_BGR2HSV)
        
        # Get dominant color from VERY CENTER REGION ONLY (innermost 25%)
        h, w = template_hsv.shape[:2]
        center_h_start = int(h * 0.375)
        center_h_end = int(h * 0.625)
        center_w_start = int(w * 0.375)
        center_w_end = int(w * 0.625)
        
        center_region = template_hsv[center_h_start:center_h_end, center_w_start:center_w_end]
        avg_color = cv2.mean(center_region)
        target_hsv = np.array(avg_color[:3], dtype=np.uint8)
        target_brightness = target_hsv[2]  # V channel
        
        print(f"Target color (H,S,V): ({target_hsv[0]}, {target_hsv[1]}, {target_hsv[2]})")
        
        # Create MUCH MORE RESTRICTIVE ranges - only match very similar colors
        # Reduced from ±12,35,35 to ±8,25,25 for stricter matching
        lower_hsv = np.array([max(0, target_hsv[0] - 8), max(0, target_hsv[1] - 25), max(0, target_hsv[2] - 25)])
        upper_hsv = np.array([min(179, target_hsv[0] + 8), min(255, target_hsv[1] + 25), min(255, target_hsv[2] + 25)])
        
        # Filter out VERY BRIGHT pixels (background) - stricter
        # Only keep pixels close to target brightness (±40 instead of ±50)
        brightness_mask = (screenshot_hsv[:, :, 2] >= target_brightness - 30) & (screenshot_hsv[:, :, 2] <= target_brightness + 30)
        
        # Get color mask
        color_mask = cv2.inRange(screenshot_hsv, lower_hsv, upper_hsv)
        
        # Combine: only keep pixels that match color AND are similar brightness to target
        combined_mask = cv2.bitwise_and(color_mask, brightness_mask.astype(np.uint8) * 255)
        
        # Dilate and erode to connect nearby regions
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        combined_mask = cv2.morphologyEx(combined_mask, cv2.MORPH_CLOSE, kernel)
        
        # Find contours
        contours, _ = cv2.findContours(combined_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        matches_list = []
        for contour in contours:
            area = cv2.contourArea(contour)
            # HIGHER minimum area threshold - ignore small noise (was 50, now 150)
            if area > 150:
                x, y, w, h = cv2.boundingRect(contour)
                matches_list.append({
                    'x': x + w // 2,
                    'y': y + h // 2,
                    'confidence': min(area / 3000.0, 1.0),
                    'method': 'color'
                })
        
        if matches_list:
            print(f"Found {len(matches_list)} color-based matches (strict center-focused)")
        
        return matches_list
        
    except Exception as e:
        print(f"Error in color matching: {e}")
        return []


def locate_and_click_loop():
    global last_clicked_position, last_enemy_name, last_enemy_check_time, in_combat
    print("F3 loop started: searching for target using process + template detection (press ESC to stop)")
    templates = load_templates()
    if not templates:
        print("No templates found. Add images in screenshots/ or capture current_target.png with F2.")
        return

    print(f"Loaded {len(templates)} templates (screenshots/ + current_target.png if present)")
    print("Mode: Process-assisted targeting + multi-template matching")
    print(f"Enemy health bar monitoring: Enabled (Area: {HEALTH_BAR_X1},{HEALTH_BAR_Y1} to {HEALTH_BAR_X2},{HEALTH_BAR_Y2})")

    last_match_time = time.time()
    in_combat = False

    while not stop_event.is_set():
        try:
            screenshot = ImageGrab.grab()
            screenshot_cv = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

            has_enemy_health_bar, enemy_name = detect_enemy_health_bar(screenshot_cv)
            current_time = time.time()
            if in_combat and has_enemy_health_bar and (current_time - last_enemy_check_time) >= enemy_name_update_interval:
                last_enemy_name = enemy_name
                last_enemy_check_time = current_time
                print(f"[ENEMY ENGAGED] {enemy_name if enemy_name else 'Unknown'}")

            if in_combat and has_enemy_health_bar:
                print(f"⚠ In combat with enemy - skipping click action")
                time.sleep(random.uniform(1, 2))
                continue

            if in_combat and not has_enemy_health_bar:
                print(f"✓ Enemy defeated - exiting combat")
                in_combat = False

            selected_target = None
            process_position = read_process_target_position()
            if process_position is not None:
                px, py = process_position
                if not _is_blacklisted(px, py):
                    selected_target = {
                        "x": px,
                        "y": py,
                        "source": "process",
                        "cluster_size": 1
                    }
                    print(f"Using process target at ({px:.1f}, {py:.1f})")

            if selected_target is None:
                all_matches = []
                for template in templates:
                    feature_matches = find_matches_orb(screenshot_cv, template["cv"], scales=SCALES)
                    color_matches = find_matches_color(screenshot_cv, template["cv"], tolerance=COLOR_TOLERANCE) if USE_COLOR_DETECTION else []
                    for m in feature_matches:
                        m["source"] = f"feature:{template['name']}"
                    for m in color_matches:
                        m["source"] = f"color:{template['name']}"
                    all_matches.extend(feature_matches + color_matches)

                if all_matches:
                    filtered_matches = [m for m in all_matches if not _is_blacklisted(m["x"], m["y"])]
                    if len(filtered_matches) < len(all_matches):
                        print(f"Filtered out {len(all_matches) - len(filtered_matches)} blacklisted matches near last click")

                    if filtered_matches:
                        last_match_time = time.time()
                        clusters = []
                        used = set()

                        for i, match in enumerate(filtered_matches):
                            if i in used:
                                continue
                            cluster = [match]
                            used.add(i)

                            for j, other in enumerate(filtered_matches):
                                if j > i and j not in used:
                                    dist = _distance(match['x'], match['y'], other['x'], other['y'])
                                    if dist < 50:
                                        cluster.append(other)
                                        used.add(j)

                            clusters.append(cluster)

                        best_cluster = max(clusters, key=lambda c: (len(c), sum(v.get("confidence", 0.0) for v in c)))
                        cx = int(np.mean([m['x'] for m in best_cluster]))
                        cy = int(np.mean([m['y'] for m in best_cluster]))

                        selected_target = {
                            "x": float(cx),
                            "y": float(cy),
                            "source": "vision",
                            "cluster_size": len(best_cluster)
                        }
                    else:
                        print("All matches are blacklisted, searching for new target...")

            if selected_target is not None:
                click_jitter = CLICK_JITTER_PX
                rx = selected_target["x"] + random.uniform(-click_jitter, click_jitter)
                ry = selected_target["y"] + random.uniform(-click_jitter, click_jitter)

                print(f"Moving to target ({rx:.1f}, {ry:.1f}) [{selected_target['source']}]...")
                pyautogui.moveTo(rx, ry)

                if not _confirm_target_lock(rx, ry, timeout=TARGET_LOCK_TIMEOUT_SECONDS):
                    print("✗ Target lock check failed, skipping click")
                    time.sleep(random.uniform(0.4, 1.2))
                    continue

                print("Clicking on confirmed target...")
                pyautogui.click()
                print(
                    f"Clicked at ({rx:.1f}, {ry:.1f}) "
                    f"[source={selected_target['source']}, cluster={selected_target['cluster_size']}]"
                )

                last_clicked_position = (rx, ry)
                in_combat = True
                last_match_time = time.time()
                print(f"Blacklisting area around ({rx:.1f}, {ry:.1f}) with radius {blacklist_radius}px")
                time.sleep(5)
            else:
                time_since_match = time.time() - last_match_time
                if time_since_match > 10:
                    print(f"No targets found for 10 seconds, rotating camera...")
                    start_time = time.time()
                    while time.time() - start_time < 5 and not stop_event.is_set():
                        current_x, current_y = pyautogui.position()
                        pyautogui.moveTo(current_x + 30, current_y)
                        time.sleep(0.05)
                    print("Camera rotation finished, resuming search...")
                    last_match_time = time.time()
                else:
                    print(f"No targets found ({time_since_match:.1f}s idle)")

            time.sleep(random.uniform(0, 0.5))
        except Exception as e:
            print(f"Error during search/click: {type(e).__name__}: {e}")
            time.sleep(0.5)
    
    print("F3 loop stopped")


def save_target():
    x, y = pyautogui.position()
    left = int(x - 37.5)  # 75/2
    top = int(y - 37.5)   # 75/2
    if left < 0:
        left = 0
    if top < 0:
        top = 0
    try:
        img = pyautogui.screenshot(region=(left, top, 75, 75))
        img.save(TARGET_NAME)
        print(f"Saved target image to: {TARGET_NAME}")
    except Exception as e:
        print("Failed to save screenshot:", e)


def on_f2(event=None):
    with lock:
        save_target()


def on_f3(event=None):
    global search_thread
    with lock:
        if search_thread and search_thread.is_alive():
            print("Search already running")
            return
        stop_event.clear()
        search_thread = threading.Thread(target=locate_and_click_loop, daemon=True)
        search_thread.start()


def on_esc(event=None):
    if not stop_event.is_set():
        stop_event.set()
        print("ESC pressed: stopping loops")


def main():
    print("=== Process + Template Target Detection ===")
    print("Press F2 to capture target (75x75 around cursor)")
    print("Press F3 to start searching and clicking")
    print("Press ESC to stop")
    if not sys.platform.startswith("win"):
        print("Warning: process-assisted metin2client.bin reading is Windows-only.")
    print(f"Templates folder: {SCREENSHOTS_DIR}")
    print(f"Optional process offsets file: {PROCESS_OFFSETS_PATH}")
    print(f"\nSettings:")
    print(f"  Feature match threshold: {MATCH_THRESHOLD}")
    print(f"  Color tolerance: {COLOR_TOLERANCE}")
    print(f"  Click offset: Very small (±{CLICK_JITTER_PX:.1f}px)")
    print(f"  Auto-rotate: 5 sec after 10 sec idle")
    print()
    
    keyboard.on_press_key("f2", lambda e: on_f2(e))
    keyboard.on_press_key("f3", lambda e: on_f3(e))
    keyboard.on_press_key("esc", lambda e: on_esc(e))
    try:
        keyboard.wait()
    except KeyboardInterrupt:
        print("Exiting...")


if __name__ == "__main__":
    main()
