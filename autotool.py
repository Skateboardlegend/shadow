import ctypes
import glob
import json
import os
import random
import struct
import sys
import threading
import time
from typing import List, Optional, Tuple

import cv2
import keyboard
import numpy as np
import pyautogui
from PIL import ImageGrab

# --- Configuration ---
PROCESS_NAME = "metin2client.bin"
OFFSETS_PATH = "offsets.json"
SCREENSHOTS_DIR = os.path.join(os.getcwd(), "screenshots")
TARGET_NAME = os.path.join(os.getcwd(), "current_target.png")

# Optional process-memory player anchor (screen coordinates)
PROCESS_PLAYER_X_ADDR = None
PROCESS_PLAYER_Y_ADDR = None
PROCESS_PLAYER_COORD_TYPE = "float"
USE_PROCESS_ANCHOR = False

# Template matching / click behavior
MATCH_CONFIDENCE_THRESHOLD = 0.52
SCALES = [0.7, 0.85, 1.0, 1.15, 1.3]
SEARCH_RADIUS = 460
BLACKLIST_RADIUS = 35
CLICK_DURATION = 0.03
LOOP_IDLE_DELAY = (0.12, 0.28)
LOOP_ATTACK_DELAY = (0.10, 0.20)

NO_MATCH_RELAX_STEP = 0.02
MAX_RELAXED_THRESHOLD = 0.40
BUSY_TIGHTEN_STEP = 0.01
MIN_STRICT_THRESHOLD = 0.60
THRESHOLD_STEP = 0.01
THRESHOLD_MIN = 0.30
THRESHOLD_MAX = 0.80
RADIUS_STEP = 20
RADIUS_MIN = 120
RADIUS_MAX = 900

FALLBACK_BEST_MIN_CONF = 0.30
FALLBACK_CLICK_COOLDOWN = 0.18

# Roaming
ROAM_TRIGGER_IDLE_SEC = 4.0
ROAM_FORWARD_TIME = (0.45, 0.85)
ROAM_TURN_PIXELS = 150

# -------------------------
# Windows elevation helpers
# -------------------------


def is_running_as_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        script_path = os.path.abspath(globals().get("__file__", sys.argv[0]))
        params = " ".join(f'"{arg}"' for arg in sys.argv[1:])
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            sys.executable,
            f'"{script_path}" {params}'.strip(),
            None,
            1,
        )
        if result <= 32:
            print(f"ShellExecuteW failed with code {result}")
            return False
        return True
    except Exception as exc:
        print(f"Failed to relaunch as admin: {exc}")
        return False


pyautogui.FAILSAFE = True

# Globals
stop_event = threading.Event()
search_thread: Optional[threading.Thread] = None
lock = threading.Lock()
last_clicked_position: Optional[Tuple[int, int]] = None
current_match_threshold = MATCH_CONFIDENCE_THRESHOLD
current_search_radius = SEARCH_RADIUS
target_only_metin = False
roam_enabled = False


# -------------------------
# Win32 window helpers
# -------------------------


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def get_main_window_for_pid(pid: int) -> Optional[int]:
    user32 = ctypes.windll.user32
    result: dict[str, Optional[int]] = {"hwnd": None}

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def enum_proc(hwnd, lparam):
        window_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(window_pid))
        if window_pid.value != pid:
            return True
        if not user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
            return True
        if user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd)) <= 0:
            return True
        result["hwnd"] = int(hwnd)
        return False

    user32.EnumWindows(EnumWindowsProc(enum_proc), 0)
    return result["hwnd"]


def get_window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    user32 = ctypes.windll.user32
    rect = RECT()
    ok = user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    if ok == 0:
        return None
    left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def activate_window(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    try:
        user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    except Exception:
        pass


def get_bound_window(reader) -> Tuple[Optional[int], Optional[Tuple[int, int, int, int]]]:
    if not reader or not reader.pid:
        return None, None
    hwnd = get_main_window_for_pid(reader.pid)
    if hwnd is None:
        return None, None
    rect = get_window_rect(hwnd)
    if rect is None:
        return None, None
    return hwnd, rect


# -------------------------
# Persistence
# -------------------------


def load_offsets() -> None:
    global PROCESS_PLAYER_X_ADDR, PROCESS_PLAYER_Y_ADDR, PROCESS_PLAYER_COORD_TYPE

    if not os.path.exists(OFFSETS_PATH):
        print("No offsets.json found. Using window-center anchor.")
        return

    try:
        with open(OFFSETS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        PROCESS_PLAYER_X_ADDR = int(data.get("player_x", 0)) if data.get("player_x") else None
        PROCESS_PLAYER_Y_ADDR = int(data.get("player_y", 0)) if data.get("player_y") else None
        PROCESS_PLAYER_COORD_TYPE = data.get("player_type", PROCESS_PLAYER_COORD_TYPE)

        if PROCESS_PLAYER_X_ADDR is None and data.get("x"):
            PROCESS_PLAYER_X_ADDR = int(data.get("x"))
        if PROCESS_PLAYER_Y_ADDR is None and data.get("y"):
            PROCESS_PLAYER_Y_ADDR = int(data.get("y"))

        print(f"Loaded offsets from {OFFSETS_PATH}")
    except Exception as exc:
        print(f"Failed to load offsets.json: {exc}")


# -------------------------
# Process memory utilities
# -------------------------


class ProcessMemoryReader:
    def __init__(self, process_name: str):
        self.pid = None
        self.hProcess = None
        self.process_name = process_name
        self._open_process()

    def _find_pid_by_name_psutil(self, name: str) -> Optional[int]:
        try:
            import psutil
        except Exception:
            return None

        for proc in psutil.process_iter(["pid", "name"]):
            try:
                proc_name = proc.info.get("name")
                if proc_name and name.lower() in proc_name.lower():
                    return proc.info["pid"]
            except Exception:
                continue
        return None

    def _find_pid_by_name_toolhelp(self, name: str) -> Optional[int]:
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_uint32),
                ("cntUsage", ctypes.c_uint32),
                ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.c_uint32),
                ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_uint32),
                ("szExeFile", ctypes.c_char * 260),
            ]

        CreateToolhelp32Snapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot
        Process32First = ctypes.windll.kernel32.Process32First
        Process32Next = ctypes.windll.kernel32.Process32Next
        CloseHandle = ctypes.windll.kernel32.CloseHandle

        hSnapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if hSnapshot == -1:
            return None

        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        success = Process32First(hSnapshot, ctypes.byref(entry))

        while success:
            try:
                exe = entry.szExeFile.decode(errors="ignore")
                if exe and name.lower() in exe.lower():
                    pid = entry.th32ProcessID
                    CloseHandle(hSnapshot)
                    return pid
            except Exception:
                pass
            success = Process32Next(hSnapshot, ctypes.byref(entry))

        CloseHandle(hSnapshot)
        return None

    def _open_process(self) -> None:
        pid = self._find_pid_by_name_psutil(self.process_name)
        if pid is None:
            pid = self._find_pid_by_name_toolhelp(self.process_name)

        self.pid = pid
        if not pid:
            print(f"Process '{self.process_name}' not found.")
            return

        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_VM_READ = 0x0010

        self.hProcess = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
            False,
            pid,
        )
        if not self.hProcess:
            print(f"Failed to open process {pid}.")
        else:
            print(f"Opened process {self.process_name} (pid={pid})")

    def read_bytes(self, addr: int, size: int) -> Optional[bytes]:
        if not self.hProcess or not addr or size <= 0:
            return None

        buffer = ctypes.create_string_buffer(size)
        bytes_read = ctypes.c_size_t()
        res = ctypes.windll.kernel32.ReadProcessMemory(
            self.hProcess,
            ctypes.c_void_p(addr),
            buffer,
            size,
            ctypes.byref(bytes_read),
        )
        if res == 0:
            return None
        return buffer.raw[: bytes_read.value]

    def read_int32(self, addr: int) -> Optional[int]:
        data = self.read_bytes(addr, 4)
        if not data or len(data) < 4:
            return None
        return struct.unpack("<i", data)[0]

    def read_float(self, addr: int) -> Optional[float]:
        data = self.read_bytes(addr, 4)
        if not data or len(data) < 4:
            return None
        return struct.unpack("<f", data)[0]

    def close(self) -> None:
        if self.hProcess:
            ctypes.windll.kernel32.CloseHandle(self.hProcess)
            self.hProcess = None


# -------------------------
# Image/template utilities
# -------------------------


def ensure_screenshots_dir() -> None:
    if not os.path.exists(SCREENSHOTS_DIR):
        os.makedirs(SCREENSHOTS_DIR)


def load_templates_from_screenshots() -> List[str]:
    ensure_screenshots_dir()

    files: List[str] = []
    for ext in ("*.png", "*.jpg", "*.jpeg", "*.bmp"):
        files.extend(glob.glob(os.path.join(SCREENSHOTS_DIR, ext)))

    files = sorted(files)
    if files:
        print(f"Loaded {len(files)} templates from screenshots/")
    else:
        print("No templates in screenshots/. Use F2 to capture one.")
    return files


def read_template_cv(path: str):
    try:
        return cv2.imread(path)
    except Exception:
        return None


def is_metin_template(path: str) -> bool:
    name = os.path.basename(path).lower()
    return "metin" in name or "stone" in name


def find_matches_template_multi(screenshot_cv, template_paths: List[str], scales=None, threshold: Optional[float] = None):
    if scales is None:
        scales = [1.0]
    if threshold is None:
        threshold = MATCH_CONFIDENCE_THRESHOLD

    screenshot_gray = cv2.cvtColor(screenshot_cv, cv2.COLOR_BGR2GRAY)
    all_matches = []

    for template_path in template_paths:
        template_cv = read_template_cv(template_path)
        if template_cv is None:
            continue

        template_gray = cv2.cvtColor(template_cv, cv2.COLOR_BGR2GRAY)
        h0, w0 = template_gray.shape

        for scale in scales:
            try:
                if scale != 1.0:
                    resized = cv2.resize(
                        template_gray,
                        (max(1, int(w0 * scale)), max(1, int(h0 * scale))),
                    )
                else:
                    resized = template_gray

                result = cv2.matchTemplate(screenshot_gray, resized, cv2.TM_CCOEFF_NORMED)
                ys, xs = np.where(result >= threshold)

                for y, x in zip(ys, xs):
                    conf = float(result[y, x])
                    all_matches.append(
                        {
                            "x": int(x + resized.shape[1] / 2),
                            "y": int(y + resized.shape[0] / 2),
                            "confidence": conf,
                            "path": template_path,
                        }
                    )
            except Exception:
                continue

    deduped = []
    for match in sorted(all_matches, key=lambda m: m["confidence"], reverse=True):
        duplicate = False
        for kept in deduped:
            dist = ((match["x"] - kept["x"]) ** 2 + (match["y"] - kept["y"]) ** 2) ** 0.5
            if dist < 30:
                duplicate = True
                break
        if not duplicate:
            deduped.append(match)

    return deduped


def find_best_match_template_multi(screenshot_cv, template_paths: List[str], scales=None):
    if scales is None:
        scales = [1.0]

    screenshot_gray = cv2.cvtColor(screenshot_cv, cv2.COLOR_BGR2GRAY)
    best = None

    for template_path in template_paths:
        template_cv = read_template_cv(template_path)
        if template_cv is None:
            continue

        template_gray = cv2.cvtColor(template_cv, cv2.COLOR_BGR2GRAY)
        h0, w0 = template_gray.shape

        for scale in scales:
            try:
                if scale != 1.0:
                    resized = cv2.resize(
                        template_gray,
                        (max(1, int(w0 * scale)), max(1, int(h0 * scale))),
                    )
                else:
                    resized = template_gray

                result = cv2.matchTemplate(screenshot_gray, resized, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)

                candidate = {
                    "x": int(max_loc[0] + resized.shape[1] / 2),
                    "y": int(max_loc[1] + resized.shape[0] / 2),
                    "confidence": float(max_val),
                    "path": template_path,
                }

                if best is None or candidate["confidence"] > best["confidence"]:
                    best = candidate
            except Exception:
                continue

    return best


# -------------------------
# Player anchor, attack and roam logic
# -------------------------


def _read_coord(reader: ProcessMemoryReader, addr: int, coord_type: str) -> Optional[int]:
    if coord_type == "int32":
        value = reader.read_int32(addr)
    else:
        value = reader.read_float(addr)
        if value is None:
            value = reader.read_int32(addr)

    if value is None:
        return None
    return int(value)


def get_player_anchor(
    reader: Optional[ProcessMemoryReader],
    local_w: int,
    local_h: int,
    win_left: int,
    win_top: int,
) -> Tuple[int, int]:
    # Use window-center anchor by default to avoid world/screen coordinate mismatch.
    if USE_PROCESS_ANCHOR and reader and reader.hProcess and PROCESS_PLAYER_X_ADDR is not None and PROCESS_PLAYER_Y_ADDR is not None:
        px = _read_coord(reader, PROCESS_PLAYER_X_ADDR, PROCESS_PLAYER_COORD_TYPE)
        py = _read_coord(reader, PROCESS_PLAYER_Y_ADDR, PROCESS_PLAYER_COORD_TYPE)
        if px is not None and py is not None:
            local_x = px - win_left
            local_y = py - win_top
            if 0 <= local_x < local_w and 0 <= local_y < local_h:
                return local_x, local_y

    return local_w // 2, local_h // 2


def choose_nearest_enemy(matches, player_anchor: Tuple[int, int]):
    if not matches:
        return None

    px, py = player_anchor

    nearby = []
    for match in matches:
        dist = ((match["x"] - px) ** 2 + (match["y"] - py) ** 2) ** 0.5
        if dist <= current_search_radius:
            nearby.append((dist, match))

    pool = nearby if nearby else [
        (((m["x"] - px) ** 2 + (m["y"] - py) ** 2) ** 0.5, m) for m in matches
    ]

    if last_clicked_position is not None:
        filtered = []
        lx, ly = last_clicked_position
        for dist, match in pool:
            last_dist = ((match["x"] - lx) ** 2 + (match["y"] - ly) ** 2) ** 0.5
            if last_dist > BLACKLIST_RADIUS:
                filtered.append((dist, match))
        if filtered:
            pool = filtered

    pool.sort(key=lambda item: item[0])
    return pool[0][1]


def simple_move_and_attack(win_left: int, win_top: int, local_x: int, local_y: int) -> None:
    global last_clicked_position

    screen_x = win_left + local_x
    screen_y = win_top + local_y

    pyautogui.moveTo(screen_x, screen_y, duration=CLICK_DURATION)
    pyautogui.click()
    last_clicked_position = (local_x, local_y)


def roam_step(hwnd: Optional[int], rect: Tuple[int, int, int, int]) -> None:
    left, top, right, bottom = rect
    cx = (left + right) // 2
    cy = (top + bottom) // 2

    if hwnd:
        activate_window(hwnd)

    pyautogui.moveTo(cx, cy, duration=0.05)
    pyautogui.click()

    pyautogui.keyDown("w")
    time.sleep(random.uniform(*ROAM_FORWARD_TIME))
    pyautogui.keyUp("w")

    pyautogui.moveRel(random.choice([-ROAM_TURN_PIXELS, ROAM_TURN_PIXELS]), 0, duration=0.12)
    print("Roam step executed (F8 enabled).")


def locate_and_click_loop() -> None:
    global current_match_threshold

    print("F3 loop started: window-bound nearest target attack.")

    templates = load_templates_from_screenshots()
    if not templates:
        print("No templates available; loop exits.")
        return

    reader = ProcessMemoryReader(PROCESS_NAME)
    last_target_time = time.time()

    while not stop_event.is_set():
        try:
            hwnd, rect = get_bound_window(reader)
            if rect is None:
                print("Metin2 window not found; waiting...")
                time.sleep(0.5)
                continue

            left, top, right, bottom = rect
            screenshot = ImageGrab.grab(bbox=(left, top, right, bottom))
            screenshot_cv = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
            height, width = screenshot_cv.shape[:2]

            # Keep actions bound to the game window.
            if hwnd:
                activate_window(hwnd)

            anchor = get_player_anchor(reader, width, height, left, top)

            active_templates = templates
            if target_only_metin:
                active_templates = [p for p in templates if is_metin_template(p)]
                if not active_templates:
                    print("Metin-only mode enabled but no template filename contains 'metin' or 'stone'.")
                    time.sleep(0.5)
                    continue

            matches = find_matches_template_multi(
                screenshot_cv,
                active_templates,
                scales=SCALES,
                threshold=current_match_threshold,
            )

            if not matches:
                current_match_threshold = max(
                    MAX_RELAXED_THRESHOLD,
                    current_match_threshold - NO_MATCH_RELAX_STEP,
                )

                best = find_best_match_template_multi(screenshot_cv, active_templates, scales=SCALES)
                if best and best["confidence"] >= FALLBACK_BEST_MIN_CONF:
                    tx, ty = int(best["x"]), int(best["y"])
                    print(
                        f"Fallback attack: target=({tx},{ty}), conf={best['confidence']:.3f}, "
                        f"th={current_match_threshold:.2f}"
                    )
                    simple_move_and_attack(left, top, tx, ty)
                    last_target_time = time.time()
                    time.sleep(FALLBACK_CLICK_COOLDOWN)
                    continue

                idle = time.time() - last_target_time
                print(f"No targets. th={current_match_threshold:.2f}, idle={idle:.1f}s")
                if roam_enabled and idle >= ROAM_TRIGGER_IDLE_SEC:
                    roam_step(hwnd, rect)
                    last_target_time = time.time()
                else:
                    time.sleep(random.uniform(*LOOP_IDLE_DELAY))
                continue

            if len(matches) >= 25:
                current_match_threshold = min(
                    MIN_STRICT_THRESHOLD,
                    current_match_threshold + BUSY_TIGHTEN_STEP,
                )

            target = choose_nearest_enemy(matches, anchor)
            if target is None:
                time.sleep(random.uniform(*LOOP_IDLE_DELAY))
                continue

            tx, ty = int(target["x"]), int(target["y"])
            distance = ((tx - anchor[0]) ** 2 + (ty - anchor[1]) ** 2) ** 0.5

            print(
                f"anchor=({anchor[0]},{anchor[1]}), target=({tx},{ty}), dist={distance:.1f}, "
                f"conf={target['confidence']:.3f}, th={current_match_threshold:.2f}"
            )

            simple_move_and_attack(left, top, tx, ty)
            last_target_time = time.time()
            time.sleep(random.uniform(*LOOP_ATTACK_DELAY))

        except Exception as exc:
            print(f"Error in loop: {type(exc).__name__}: {exc}")
            time.sleep(0.3)

    if reader:
        reader.close()
    print("F3 loop stopped.")


# -------------------------
# Capture helpers
# -------------------------


def save_target_to_screenshots() -> None:
    ensure_screenshots_dir()

    x, y = pyautogui.position()
    left = max(0, int(x - 100))
    top = max(0, int(y - 100))

    timestamp = int(time.time())
    path = os.path.join(SCREENSHOTS_DIR, f"target_{timestamp}.png")

    try:
        img = pyautogui.screenshot(region=(left, top, 200, 200))
        img.save(path)
        print(f"Saved target image to: {path}")
    except Exception as exc:
        print(f"Failed to save screenshot: {exc}")


def save_target() -> None:
    save_target_to_screenshots()

    try:
        pos = pyautogui.position()
        img = pyautogui.screenshot(region=(max(0, pos[0] - 100), max(0, pos[1] - 100), 200, 200))
        img.save(TARGET_NAME)
        print(f"Saved quick target to: {TARGET_NAME}")
    except Exception:
        pass


# -------------------------
# Key handlers
# -------------------------


def on_f2(event=None) -> None:
    with lock:
        save_target()


def on_f3(event=None) -> None:
    global search_thread
    with lock:
        if search_thread and search_thread.is_alive():
            print("Search already running")
            return

        stop_event.clear()
        search_thread = threading.Thread(target=locate_and_click_loop, daemon=True)
        search_thread.start()


def on_f7(event=None) -> None:
    global target_only_metin
    with lock:
        target_only_metin = not target_only_metin
        mode = "ON" if target_only_metin else "OFF"
        print(f"Metin-only mode: {mode}")


def on_f8(event=None) -> None:
    global roam_enabled
    with lock:
        roam_enabled = not roam_enabled
        mode = "ON" if roam_enabled else "OFF"
        print(f"Roam mode: {mode}")


def on_esc(event=None) -> None:
    if not stop_event.is_set():
        stop_event.set()
        print("ESC pressed: stopping loops")


def on_5(event=None) -> None:
    global current_match_threshold
    with lock:
        current_match_threshold = min(THRESHOLD_MAX, current_match_threshold + THRESHOLD_STEP)
        print(f"Threshold increased: {current_match_threshold:.2f}")


def on_6(event=None) -> None:
    global current_match_threshold
    with lock:
        current_match_threshold = max(THRESHOLD_MIN, current_match_threshold - THRESHOLD_STEP)
        print(f"Threshold decreased: {current_match_threshold:.2f}")


def on_7(event=None) -> None:
    global current_search_radius
    with lock:
        current_search_radius = min(RADIUS_MAX, current_search_radius + RADIUS_STEP)
        print(f"Radius increased: {current_search_radius}")


def on_8(event=None) -> None:
    global current_search_radius
    with lock:
        current_search_radius = max(RADIUS_MIN, current_search_radius - RADIUS_STEP)
        print(f"Radius decreased: {current_search_radius}")


def main() -> None:
    load_offsets()

    if os.name == "nt" and not is_running_as_admin():
        print("Restarting with administrator privileges so elevated programs can be accessed.")
        if relaunch_as_admin():
            return
        print("Administrator relaunch was not started.")

    print("=== Window-Bound Metin2 Attacker ===")
    print("Keys:")
    print("  F2 - capture target image into screenshots/")
    print("  F3 - start window-bound searching and attack")
    print("  F7 - toggle metin-only targets")
    print("  F8 - toggle roam mode")
    print("  5 - increase match threshold")
    print("  6 - decrease match threshold")
    print("  7 - increase search radius")
    print("  8 - decrease search radius")
    print("  ESC - stop")
    print()

    keyboard.on_press_key("f2", lambda e: on_f2(e))
    keyboard.on_press_key("f3", lambda e: on_f3(e))
    keyboard.on_press_key("f7", lambda e: on_f7(e))
    keyboard.on_press_key("f8", lambda e: on_f8(e))
    keyboard.on_press_key("5", lambda e: on_5(e))
    keyboard.on_press_key("6", lambda e: on_6(e))
    keyboard.on_press_key("7", lambda e: on_7(e))
    keyboard.on_press_key("8", lambda e: on_8(e))
    keyboard.on_press_key("esc", lambda e: on_esc(e))

    try:
        keyboard.wait()
    except KeyboardInterrupt:
        print("Exiting...")


if __name__ == "__main__":
    main()


