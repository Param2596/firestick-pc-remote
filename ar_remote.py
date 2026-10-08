"""Forward an Amazon Fire TV remote, paired to an Android phone, into Windows keys.

The phone is the translator: it already understands the remote. This script
reads that phone over adb (USB or Wi-Fi) and injects matching keys here.
"""

from __future__ import annotations

import argparse
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import winsound
import zipfile
from pathlib import Path

import ctypes
from ctypes import wintypes

ADB = Path.home() / "AppData/Local/Android/platform-tools/adb.exe"
PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
ENDPOINT_FILE = Path(__file__).with_name("endpoint.txt")
SOUND_FILE = Path(__file__).with_name("sound.txt")
CHIME_FILE = Path(__file__).with_name("mode.wav")
BRIDGE_FILE = Path(__file__).with_name("bridge.sh")
BRIDGE_SESSION = Path(__file__).with_name("bridge-session.sh")
BRIDGE_SECRET = Path(__file__).with_name("bridge.txt")
BRIDGE_PORT = 47655
REMOTE_NAME_HINTS = ("ar keyboard", "ar", "amazon", "fire tv", "firetv")

# Linux evdev name -> (virtual-key, extended).
# Power stays unmapped. Alexa toggles between this map and VOLUME_MAP.
KEY_MAP: dict[str, tuple[int, bool]] = {
    "KEY_UP": (0x26, True),
    "KEY_DOWN": (0x28, True),
    "KEY_LEFT": (0x25, True),
    "KEY_RIGHT": (0x27, True),
    "KEY_ENTER": (0x0D, False),
    "KEY_SELECT": (0x0D, False),
    "KEY_KPENTER": (0x0D, False),
    "KEY_BACK": (0x1B, False),
    "KEY_ESC": (0x1B, False),
    "KEY_HOME": (0x5B, True),
    "KEY_HOMEPAGE": (0x5B, True),
    "KEY_MENU": (0x09, False),
    "KEY_PLAYPAUSE": (0xB3, False),
    "KEY_PLAY": (0xB3, False),
    "KEY_PAUSE": (0xB3, False),
    "KEY_REWIND": (0xB1, False),
    "KEY_PREVIOUSSONG": (0xB1, False),
    "KEY_FASTFORWARD": (0xB0, False),
    "KEY_NEXTSONG": (0xB0, False),
    "KEY_VOLUMEUP": (0xAF, False),
    "KEY_VOLUMEDOWN": (0xAE, False),
    "KEY_MUTE": (0xAD, False),
}

# Fired once on press. Win+Tab opens Task View.
CHORDS: dict[str, tuple[tuple[int, bool], ...]] = {
    "KEY_PROGRAM": ((0x5B, True), (0x09, False)),
}

# Media pad. Mute and track keys are tapped once so mute does not flip twice.
VOLUME_MAP: dict[str, tuple[int, bool]] = {
    "KEY_UP": (0xAF, False),
    "KEY_DOWN": (0xAE, False),
    "KEY_LEFT": (0x25, True),
    "KEY_RIGHT": (0x27, True),
    "KEY_ENTER": (0xAD, False),
    "KEY_SELECT": (0xAD, False),
    "KEY_KPENTER": (0xAD, False),
}
TAP_KEYS = {0xAD, 0xB0, 0xB1}
REPEAT_KEYS = {"KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT", "KEY_MENU"}
CURSOR_MOVE = {
    "KEY_UP": (0, -1),
    "KEY_DOWN": (0, 1),
    "KEY_LEFT": (-1, 0),
    "KEY_RIGHT": (1, 0),
}
CLICK_KEYS = {"KEY_ENTER", "KEY_SELECT", "KEY_KPENTER"}
MODES = ("controls", "volume", "cursor", "tv")
MODE_TITLES = {"controls": "Controls", "volume": "Volume", "cursor": "Cursor", "tv": "TV"}
APPS_MENU = Path(__file__).with_name("app_menu.py")

mode = "controls"
cursor_held: set[str] = set()
cursor_lock = threading.Lock()
repeat_held: dict[str, tuple[int, bool]] = {}
repeat_lock = threading.Lock()
mouse_left_down = False
mouse_right_down = False

IGNORED = {"KEY_POWER", "KEY_SLEEP", "KEY_VOICECOMMAND", "KEY_ASSISTANT"}
EVENT_RE = re.compile(r"EV_KEY\s+(\S+)\s+(DOWN|UP|REPEAT)")
CODE_TO_NAME = {
    103: "KEY_UP",
    108: "KEY_DOWN",
    105: "KEY_LEFT",
    106: "KEY_RIGHT",
    28: "KEY_ENTER",
    96: "KEY_KPENTER",
    353: "KEY_SELECT",
    158: "KEY_BACK",
    1: "KEY_ESC",
    102: "KEY_HOME",
    172: "KEY_HOMEPAGE",
    139: "KEY_MENU",
    164: "KEY_PLAYPAUSE",
    207: "KEY_PLAY",
    119: "KEY_PAUSE",
    168: "KEY_REWIND",
    165: "KEY_PREVIOUSSONG",
    208: "KEY_FASTFORWARD",
    163: "KEY_NEXTSONG",
    115: "KEY_VOLUMEUP",
    114: "KEY_VOLUMEDOWN",
    113: "KEY_MUTE",
    116: "KEY_POWER",
    142: "KEY_SLEEP",
    582: "KEY_VOICECOMMAND",
    583: "KEY_ASSISTANT",
    362: "KEY_PROGRAM",
    217: "KEY_SEARCH",
}
# Keys that must be released on startup. Home used to hold the Windows key down,
# which made every later button look like it did nothing.
STUCK_KEYS = (
    (0x5B, True),
    (0x5C, True),
    (0x5D, True),
    (0x10, False),
    (0x11, False),
    (0x12, False),
    (0xA0, False),
    (0xA1, False),
    (0xA2, False),
    (0xA3, False),
    (0xA4, False),
    (0xA5, False),
    (0x25, True),
    (0x26, True),
    (0x27, True),
    (0x28, True),
    (0x0D, False),
    (0x1B, False),
    (0xAC, False),
)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class INPUTUNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("union", INPUTUNION))


user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT


def send_key(vk: int, extended: bool, up: bool) -> None:
    flags = 0
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    if up:
        flags |= KEYEVENTF_KEYUP
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.union.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        raise ctypes.WinError(ctypes.get_last_error())


def send_mouse(flags: int, dx: int = 0, dy: int = 0) -> None:
    inp = INPUT(type=INPUT_MOUSE)
    inp.union.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=0)
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        raise ctypes.WinError(ctypes.get_last_error())


def say(message: str) -> None:
    if sys.stdout is not None:
        print(message)


def ensure_adb() -> str:
    """Use a local adb, one already on PATH, or download Google's platform-tools."""
    if ADB.exists():
        return str(ADB)
    found = shutil.which("adb")
    if found:
        return found
    say("Downloading Android platform-tools (adb). This happens once.")
    dest_root = ADB.parent.parent
    dest_root.mkdir(parents=True, exist_ok=True)
    zip_path = dest_root / "platform-tools.zip"
    try:
        urllib.request.urlretrieve(PLATFORM_TOOLS_URL, zip_path)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(dest_root)
    finally:
        zip_path.unlink(missing_ok=True)
    if not ADB.exists():
        raise SystemExit(f"adb download finished, but {ADB} is missing.")
    say(f"adb is at {ADB}")
    return str(ADB)


def adb_bin() -> str:
    return ensure_adb()


def pythonw_path() -> Path:
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if candidate.exists():
        return candidate
    return Path(sys.executable)


def ps_quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def install_shortcuts() -> None:
    pythonw = pythonw_path()
    script = Path(__file__).resolve()
    ps1 = script.with_name("_shortcuts.ps1")
    ps1.write_text(
        "\n".join(
            [
                "$shell = New-Object -ComObject WScript.Shell",
                "$desktop = [Environment]::GetFolderPath('Desktop')",
                "$startup = Join-Path $env:APPDATA 'Microsoft\\Windows\\Start Menu\\Programs\\Startup'",
                f"$target = {ps_quote(pythonw)}",
                f"$script = {ps_quote(script)}",
                f"$work = {ps_quote(script.parent)}",
                "foreach ($dir in @($desktop, $startup)) {",
                "  $link = $shell.CreateShortcut((Join-Path $dir 'Fire Remote.lnk'))",
                "  $link.TargetPath = $target",
                "  $link.Arguments = '\"' + $script + '\" --tray'",
                "  $link.WorkingDirectory = $work",
                "  $link.WindowStyle = 7",
                "  $link.Description = 'Toggle the Fire TV remote on this PC'",
                "  $link.Save()",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
            check=True,
            creationflags=NO_WINDOW,
        )
    finally:
        ps1.unlink(missing_ok=True)
    say("Shortcuts are on the desktop and in Startup. The tray icon starts off.")


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_adb(args: list[str], timeout: float = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [adb_bin(), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    )


def adb_text(args: list[str], timeout: float = 15) -> str:
    result = run_adb(args, timeout=timeout)
    return (result.stdout or "") + (result.stderr or "")


def parse_devices(text: str) -> list[tuple[str, str]]:
    devices = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in {"device", "unauthorized", "offline"}:
            if parts[0] != "List":
                devices.append((parts[0], parts[1]))
    return devices


def phone_ip(serial: str) -> str | None:
    text = adb_text(["-s", serial, "shell", "ip", "-4", "route", "get", "1.0.0.1"])
    match = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", text)
    return match.group(1) if match else None


def connect_endpoint(endpoint: str) -> bool:
    text = adb_text(["connect", endpoint])
    print(text.strip())
    return "connected" in text.lower() or "already connected" in text.lower()


def discover_mdns() -> list[str]:
    text = adb_text(["mdns", "services"])
    found = []
    for line in text.splitlines():
        if "_adb-tls-connect._tcp" not in line:
            continue
        match = re.search(r"(\d+\.\d+\.\d+\.\d+:\d+)", line)
        if match:
            found.append(match.group(1))
    return found


NETWORK_WARNING = """
DO NOT DO THIS ON PUBLIC WI-FI.

Use a private network you trust, such as your home Wi-Fi.
A cafe, hotel, airport, school, or guest network is not safe.

Wireless debugging lets this PC run commands on the phone:
install apps, read files, and control the screen. Anyone else
on that same network who pairs with the phone can do the same.
It has to stay on while the remote controls this PC.
Turn it off when you are finished, and leave it off away from home.
"""


def confirm_private_network() -> None:
    say(NETWORK_WARNING)
    answer = input("Type YES to continue on a private network: ").strip()
    if answer.upper() != "YES":
        raise SystemExit("Stopped. Connect to a private network, then run this again.")


def remember_device(endpoint: str) -> bool:
    if not connect_endpoint(endpoint):
        return False
    devices = parse_devices(adb_text(["devices"]))
    if any(serial == endpoint and state == "device" for serial, state in devices):
        ENDPOINT_FILE.write_text(endpoint, encoding="utf-8")
        say(f"Phone saved at {endpoint}")
        return True
    return False


def connect_phone() -> None:
    """Reconnect after a reboot. Uses the main Wireless debugging IP:port, not a pairing code."""
    confirm_private_network()
    ensure_adb()
    run_adb(["start-server"])
    saved = ENDPOINT_FILE.read_text(encoding="utf-8").strip() if ENDPOINT_FILE.exists() else ""
    if saved and remember_device(saved):
        say("Phone is already connected. Click the tray icon.")
        return
    say("After a phone reboot you usually do NOT need a pairing code.")
    say("On the phone, turn Wireless debugging on and stay on that page.")
    say("Use IP address & Port from the MAIN page. Do not open Pair device with pairing code.")
    endpoint = input("IP and port from the Wireless debugging page, such as 192.168.1.20:41403: ").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+:\d+", endpoint):
        raise SystemExit("That is not an IP and port. Use the main Wireless debugging page, not the pairing popup.")
    if remember_device(endpoint):
        say("Connected over Wi-Fi. Click the tray icon.")
        return
    raise SystemExit(
        "Could not connect. If this PC has never been paired, run: python ar_remote.py --pair\n"
        "If it has, the port on the phone changed. Copy IP address & Port again while Wireless debugging is on."
    )


def pair_phone() -> None:
    """First-time Wi-Fi pairing with the 6-digit code. After a reboot, use --connect instead."""
    confirm_private_network()
    ensure_adb()
    run_adb(["start-server"])
    say("This is only needed the first time, or if you revoked USB debugging authorizations.")
    say("On the phone, open Developer options, then Wireless debugging.")
    say("Tap Pair device with pairing code and leave that popup open.")
    say("The popup's port is NOT the same as IP address & Port on the main page.")
    endpoint = input("IP and port from the PAIRING popup, such as 192.168.1.20:37123: ").strip()
    code = input("6-digit pairing code: ").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+:\d+", endpoint) or not re.fullmatch(r"\d{6}", code):
        raise SystemExit("Use the IP, port, and 6-digit code shown on the pairing popup.")
    text = adb_text(["pair", endpoint, code], timeout=40)
    say(text.strip())
    if "successfully paired" not in text.lower():
        raise SystemExit(
            "Pairing failed. The code expires in about 2 minutes. "
            "Open Pair device with pairing code again. Do not use the main-page port here."
        )
    time.sleep(1)
    for found in discover_mdns():
        if remember_device(found):
            say("Paired over Wi-Fi. You can close the popup.")
            return
    say("Paired. Close the popup. Now copy IP address & Port from the MAIN Wireless debugging page.")
    connect_to = input("IP and port from that page: ").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+:\d+", connect_to):
        raise SystemExit("That is not an IP and port. Look at the Wireless debugging page again.")
    if not remember_device(connect_to):
        raise SystemExit(f"Could not connect to {connect_to}. Check Wireless debugging is still on.")
    say("Paired over Wi-Fi. Unplug any cable. The tray icon is the switch from here.")


def choose_serial() -> str:
    """Prefer an authorized Wi-Fi serial. Fall back to USB, then switch it to Wi-Fi."""
    saved = ENDPOINT_FILE.read_text(encoding="utf-8").strip() if ENDPOINT_FILE.exists() else ""
    if saved:
        connect_endpoint(saved)

    for endpoint in discover_mdns():
        connect_endpoint(endpoint)

    devices = parse_devices(adb_text(["devices", "-l"]))
    authorized = [serial for serial, state in devices if state == "device"]
    wireless = [serial for serial in authorized if ":" in serial]
    if wireless:
        ENDPOINT_FILE.write_text(wireless[0], encoding="utf-8")
        return wireless[0]

    usb = [serial for serial in authorized if ":" not in serial]
    if usb:
        serial = usb[0]
        ip = phone_ip(serial)
        if ip:
            print(f"USB is up. Switching {serial} to Wi-Fi at {ip}:5555")
            print(adb_text(["-s", serial, "tcpip", "5555"]).strip())
            time.sleep(1.5)
            endpoint = f"{ip}:5555"
            connect_endpoint(endpoint)
            ENDPOINT_FILE.write_text(endpoint, encoding="utf-8")
            # Give the phone a moment to show the Allow prompt for the new socket.
            for _ in range(20):
                devices = parse_devices(adb_text(["devices"]))
                states = {name: state for name, state in devices}
                if states.get(endpoint) == "device":
                    return endpoint
                time.sleep(0.5)
            raise SystemExit(
                f"Phone is at {endpoint} but has not authorized this PC yet. "
                "Unlock the phone, tap Allow on the USB debugging prompt, then run this again."
            )
        return serial

    pending = [serial for serial, state in devices if state == "unauthorized"]
    if pending:
        raise SystemExit(
            "The phone is connected but not authorized. Unlock it, tap Allow, "
            "and check 'Always allow from this computer'."
        )
    raise SystemExit(
        "No phone found. After a reboot run: python ar_remote.py --connect\n"
        "First time only, run: python ar_remote.py --pair"
    )


def find_remote(serial: str) -> tuple[str, str]:
    text = adb_text(["-s", serial, "shell", "getevent", "-pl"], timeout=20)
    devices: list[tuple[str, str, str]] = []
    current_path = ""
    current_name = ""
    current_block: list[str] = []

    def flush() -> None:
        if current_path:
            devices.append((current_path, current_name, "\n".join(current_block)))

    for line in text.splitlines():
        added = re.match(r"add device \d+:\s+(\S+)", line)
        if added:
            flush()
            current_path = added.group(1)
            current_name = ""
            current_block = [line]
            continue
        current_block.append(line)
        name = re.search(r'name:\s+"(.*)"', line)
        if name:
            current_name = name.group(1)
    flush()

    ranked: list[tuple[int, str, str]] = []
    for path, name, block in devices:
        score = 0
        lowered = name.lower()
        if lowered == "ar keyboard":
            score += 100
        elif any(hint in lowered for hint in REMOTE_NAME_HINTS):
            score += 40
        if "KEY_UP" in block and "KEY_ENTER" in block:
            score += 10
        if lowered in {"touchpanel", "gpio-keys", "pmic_pwrkey"}:
            score -= 50
        ranked.append((score, path, name))
    ranked.sort(reverse=True)
    if not ranked or ranked[0][0] <= 0:
        names = ", ".join(f"{name} ({path})" for _, path, name in ranked) or "none"
        raise SystemExit(f"Could not find the AR remote among input devices: {names}")
    _, path, name = ranked[0]
    return path, name


def release_stuck_keys() -> None:
    for vk, extended in STUCK_KEYS:
        send_key(vk, extended, up=True)


def send_chord(keys: tuple[tuple[int, bool], ...]) -> None:
    for vk, extended in keys:
        send_key(vk, extended, up=False)
    for vk, extended in reversed(keys):
        send_key(vk, extended, up=True)


def binding_for(name: str) -> tuple[int, bool] | None:
    if mode == "volume":
        volume = VOLUME_MAP.get(name)
        if volume is not None:
            return volume
    return KEY_MAP.get(name)


class ModeLabel:
    def __init__(self) -> None:
        self.root = None
        self.label = None
        self.hide_job = None
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            import tkinter as tk
        except ImportError:
            self.ready.set()
            return
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg="#1c1f18")
        label = tk.Label(
            root,
            text="",
            font=("Segoe UI", 15),
            bg="#1c1f18",
            fg="#f3f1e8",
            padx=18,
            pady=8,
        )
        label.pack()
        root.withdraw()
        self.root = root
        self.label = label
        self.ready.set()
        root.mainloop()

    def show(self, title: str) -> None:
        if not self.ready.wait(0.5) or self.root is None:
            return

        def go() -> None:
            self.label.configure(text=title)
            self.root.update_idletasks()
            width = self.root.winfo_reqwidth()
            x = max(0, (self.root.winfo_screenwidth() - width) // 2)
            self.root.geometry(f"+{x}+36")
            self.root.deiconify()
            self.root.lift()
            if self.hide_job is not None:
                self.root.after_cancel(self.hide_job)
            self.hide_job = self.root.after(1600, self.root.withdraw)

        self.root.after(0, go)


mode_label = ModeLabel()


def load_sound_enabled() -> bool:
    try:
        return SOUND_FILE.read_text(encoding="utf-8").strip() != "off"
    except OSError:
        return True


def save_sound_enabled(enabled: bool) -> None:
    SOUND_FILE.write_text("on" if enabled else "off", encoding="utf-8")


def load_chime() -> bytes:
    # Thanks to Kenney for bong_001 from Interface Sounds: https://kenney.nl/assets/interface-sounds
    try:
        return CHIME_FILE.read_bytes()
    except OSError:
        return b""


CHIME = load_chime()
sound_enabled = load_sound_enabled()


def play_chime() -> None:
    if not sound_enabled or not CHIME:
        return
    # Synchronous on purpose. An async play is tied to the calling thread, and
    # the mode-switch thread exits immediately, which cuts the chime off.
    try:
        winsound.PlaySound(CHIME, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
    except RuntimeError:
        pass


def announce_mode() -> None:
    title = MODE_TITLES[mode]
    print(f"mode: {mode}")
    mode_label.show(title)
    threading.Thread(target=play_chime, daemon=True).start()


def release_mouse() -> None:
    global mouse_left_down, mouse_right_down
    if mouse_left_down:
        send_mouse(MOUSEEVENTF_LEFTUP)
        mouse_left_down = False
    if mouse_right_down:
        send_mouse(MOUSEEVENTF_RIGHTUP)
        mouse_right_down = False


def toggle_mode(held: dict[str, tuple[int, bool]]) -> None:
    global mode
    for vk, extended in list(held.values()):
        send_key(vk, extended, up=True)
    held.clear()
    with cursor_lock:
        cursor_held.clear()
    with repeat_lock:
        repeat_held.clear()
    release_mouse()
    mode = MODES[(MODES.index(mode) + 1) % len(MODES)]
    announce_mode()


def cursor_loop() -> None:
    speed = 10.0
    while True:
        with cursor_lock:
            names = set(cursor_held)
        dx = sum(CURSOR_MOVE[name][0] for name in names)
        dy = sum(CURSOR_MOVE[name][1] for name in names)
        if dx or dy:
            send_mouse(MOUSEEVENTF_MOVE, int(dx * speed), int(dy * speed))
            speed = min(48.0, speed + 2.2)
            time.sleep(0.016)
        else:
            speed = 10.0
            time.sleep(0.03)


threading.Thread(target=cursor_loop, daemon=True).start()


def pulse_key(vk: int, extended: bool) -> None:
    send_key(vk, extended, up=False)
    send_key(vk, extended, up=True)


def repeat_loop() -> None:
    interval = 0.18
    while True:
        with repeat_lock:
            keys = list(repeat_held.values())
        if not keys:
            interval = 0.18
            time.sleep(0.03)
            continue
        time.sleep(interval)
        with repeat_lock:
            keys = list(repeat_held.values())
        if not keys:
            interval = 0.18
            continue
        for vk, extended in keys:
            pulse_key(vk, extended)
        interval = max(0.06, interval * 0.72)


threading.Thread(target=repeat_loop, daemon=True).start()


def open_apps() -> None:
    """Bring up the TV app menu. Home does this in TV mode."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hwnd = user32.FindWindowW(None, "Apps")
    if hwnd:
        user32.ShowWindow(hwnd, 9)
        user32.keybd_event(0x12, 0, 0, 0)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(0x12, 0, 2, 0)
        return
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = exe
    subprocess.Popen([str(pythonw), str(APPS_MENU)], creationflags=NO_WINDOW)


def apply_key(name: str, action: str, held: dict[str, tuple[int, bool]], seen_ignored: set[str]) -> None:
    global mouse_left_down
    if name == "KEY_SEARCH":
        if action == "DOWN":
            toggle_mode(held)
        return
    if mode == "tv" and name in {"KEY_HOME", "KEY_HOMEPAGE"}:
        if action == "DOWN":
            open_apps()
            print(name)
        return
    if mode == "tv" and name in {"KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT"}:
        mapped = binding_for(name)
        if mapped is not None and action == "DOWN":
            pulse_key(mapped[0], mapped[1])
            print(name)
        return
    if mode != "cursor" and name in REPEAT_KEYS:
        mapped = binding_for(name)
        if mapped is not None:
            if action == "DOWN":
                fresh = False
                with repeat_lock:
                    if name not in repeat_held:
                        repeat_held[name] = mapped
                        fresh = True
                if fresh:
                    pulse_key(mapped[0], mapped[1])
                    print(name)
            elif action == "UP":
                with repeat_lock:
                    repeat_held.pop(name, None)
            return
    if mode == "cursor" and name in CURSOR_MOVE:
        with cursor_lock:
            if action == "DOWN":
                cursor_held.add(name)
            elif action == "UP":
                cursor_held.discard(name)
        return
    if mode == "cursor" and name in CLICK_KEYS:
        if action == "DOWN" and not mouse_left_down:
            send_mouse(MOUSEEVENTF_LEFTDOWN)
            mouse_left_down = True
            print(name)
        elif action == "UP" and mouse_left_down:
            send_mouse(MOUSEEVENTF_LEFTUP)
            mouse_left_down = False
        return
    if mode == "cursor" and name == "KEY_MENU":
        if action == "DOWN":
            send_mouse(MOUSEEVENTF_RIGHTDOWN)
            send_mouse(MOUSEEVENTF_RIGHTUP)
            print(name)
        return
    chord = CHORDS.get(name)
    if chord is not None:
        if action == "DOWN":
            send_chord(chord)
            print(name)
        return
    if name in IGNORED:
        if name not in seen_ignored:
            print(f"{name} ignored")
            seen_ignored.add(name)
        return
    mapped = binding_for(name)
    if mapped is None:
        if action == "DOWN":
            print(f"unmapped {name}")
        return
    if action == "REPEAT":
        return
    if action == "DOWN":
        if mapped[0] in TAP_KEYS:
            send_key(mapped[0], mapped[1], up=False)
            send_key(mapped[0], mapped[1], up=True)
            print(name)
            return
        if name in held:
            return
        held[name] = mapped
        send_key(mapped[0], mapped[1], up=False)
        print(name)
        return
    pressed = held.pop(name, None)
    if pressed is None:
        return
    send_key(pressed[0], pressed[1], up=True)


def install_grabber(serial: str) -> None:
    binary = Path(__file__).with_name("grabevent")
    if not binary.exists():
        raise SystemExit("grabevent is missing. Run build_grabevent.py first.")
    pushed = run_adb(["-s", serial, "push", str(binary), "/data/local/tmp/grabevent"])
    if pushed.returncode != 0:
        raise SystemExit(pushed.stdout + pushed.stderr)
    run_adb(["-s", serial, "shell", "chmod", "755", "/data/local/tmp/grabevent"])


class Bridge:
    process: subprocess.Popen | None = None


bridge = Bridge()


def stream(serial: str, device: str, stop: threading.Event | None = None) -> None:
    print("Listening. Alexa cycles Controls, Volume, and Cursor.")
    print(f"mode: {mode}")
    release_stuck_keys()
    # Drop a previous grabber so this one can take the device.
    run_adb(["-s", serial, "shell", "pkill grabevent"], timeout=5)
    time.sleep(0.2)
    # -tt forces a terminal on the phone, so each line is flushed instead of
    # sitting in a buffer. Raw 24-byte packets over exec-out were the lag.
    process = subprocess.Popen(
        [adb_bin(), "-s", serial, "shell", "-tt", "/data/local/tmp/grabevent", device],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        creationflags=NO_WINDOW,
    )
    bridge.process = process
    held: dict[str, tuple[int, bool]] = {}
    seen_ignored: set[str] = set()
    pending = b""
    assert process.stdout is not None
    try:
        while not (stop is not None and stop.is_set()):
            incoming = process.stdout.read(128)
            if not incoming:
                code = process.wait(timeout=3)
                if code == 2:
                    raise ConnectionError("phone refused to hand over the remote")
                raise ConnectionError("adb closed the button stream")
            pending += incoming
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                match = re.search(rb"(\d+)\s+(\d+)", line)
                if not match:
                    continue
                code = int(match.group(1))
                value = int(match.group(2))
                name = CODE_TO_NAME.get(code, f"KEY_{code}")
                action = {0: "UP", 1: "DOWN", 2: "REPEAT"}.get(value)
                if action is None:
                    continue
                apply_key(name, action, held, seen_ignored)
    except KeyboardInterrupt:
        print("\nStopped.")
        return
    finally:
        for vk, extended in list(held.values()):
            send_key(vk, extended, up=True)
        release_stuck_keys()
        if bridge.process is process:
            bridge.process = None
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        run_adb(["-s", serial, "shell", "pkill grabevent"], timeout=5)


def feed_buttons(pending: bytes, incoming: bytes, held: dict[str, tuple[int, bool]], seen_ignored: set[str]) -> bytes:
    pending += incoming
    while b"\n" in pending:
        line, pending = pending.split(b"\n", 1)
        match = re.search(rb"(\d+)\s+(\d+)", line)
        if not match:
            continue
        code = int(match.group(1))
        value = int(match.group(2))
        name = CODE_TO_NAME.get(code, f"KEY_{code}")
        action = {0: "UP", 1: "DOWN", 2: "REPEAT"}.get(value)
        if action is None:
            continue
        apply_key(name, action, held, seen_ignored)
    return pending


def bridge_token() -> str:
    if BRIDGE_SECRET.exists():
        return BRIDGE_SECRET.read_text(encoding="utf-8").strip()
    token = secrets.token_hex(16)
    BRIDGE_SECRET.write_text(token, encoding="utf-8")
    return token


def pc_ip_toward(phone_host: str) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((phone_host, 9))
        return sock.getsockname()[0]
    finally:
        sock.close()


def allow_bridge_port() -> None:
    subprocess.run(
        [
            "netsh", "advfirewall", "firewall", "add", "rule",
            "name=Fire Remote bridge", "dir=in", "action=allow",
            "protocol=TCP", f"localport={BRIDGE_PORT}",
        ],
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
    )


def phone_bridge_alive(serial: str) -> bool:
    text = adb_text(["-s", serial, "shell", "cat /data/local/tmp/bridge.pid"], timeout=8).strip()
    if not text.isdigit():
        return False
    check = adb_text(["-s", serial, "shell", f"ls /proc/{text}"], timeout=8)
    return "No such file" not in check and check.strip() != ""


def start_phone_bridge(serial: str) -> None:
    host = serial.split(":")[0] if ":" in serial else phone_ip(serial)
    if not host:
        raise SystemExit("Could not tell which IP the phone is using.")
    pc_ip = pc_ip_toward(host)
    token = bridge_token()
    install_grabber(serial)
    for local, remote in (
        (BRIDGE_FILE, "/data/local/tmp/bridge.sh"),
        (BRIDGE_SESSION, "/data/local/tmp/bridge-session.sh"),
    ):
        pushed = run_adb(["-s", serial, "push", str(local), remote])
        if pushed.returncode != 0:
            raise SystemExit(pushed.stdout + pushed.stderr)
        run_adb(["-s", serial, "shell", f"chmod 755 {remote}"])
    # Stop an older copy, then the adb-attached grabber, so the new one can take the remote.
    run_adb(["-s", serial, "shell", "pid=$(cat /data/local/tmp/bridge.pid 2>/dev/null); [ -n \"$pid\" ] && kill $pid"], timeout=8)
    run_adb(["-s", serial, "shell", "pkill grabevent"], timeout=5)
    time.sleep(0.3)
    run_adb(["-s", serial, "shell", f"sh /data/local/tmp/bridge.sh {pc_ip} {BRIDGE_PORT} {token}"], timeout=15)
    time.sleep(0.6)
    if phone_bridge_alive(serial):
        say(f"Phone helper is running and will call this PC at {pc_ip}:{BRIDGE_PORT}.")
        say("Phone helper is asleep until the remote connects over Bluetooth.")
        say("Wireless debugging can be turned off. A phone reboot needs it on once, to start the helper again.")
    else:
        say("The phone helper did not stay running.")


def ensure_phone_bridge() -> None:
    """Start the on-phone helper when adb is up. If debugging is already off, just wait for it."""
    try:
        run_adb(["start-server"])
        serial = choose_serial()
    except SystemExit as exc:
        say(str(exc))
        say("Waiting for the phone helper. It keeps running after debugging is turned off.")
        return
    if phone_bridge_alive(serial):
        say("Phone helper is already running.")
        return
    start_phone_bridge(serial)


def tune_link(conn: socket.socket) -> None:
    """Notice a phone that went idle and dropped Wi-Fi, instead of holding the dead link."""
    try:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):
            conn.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 45_000, 10_000))
    except OSError:
        pass


def serve_bridge(stop: threading.Event) -> None:
    token = bridge_token().encode()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", BRIDGE_PORT))
    server.listen(1)
    server.settimeout(0.5)
    say(f"Listening for the phone on {BRIDGE_PORT}.")
    release_stuck_keys()
    try:
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
            except (TimeoutError, socket.timeout):
                continue
            conn.settimeout(0.5)
            tune_link(conn)
            held: dict[str, tuple[int, bool]] = {}
            seen_ignored: set[str] = set()
            pending = b""
            authed = False
            try:
                while not stop.is_set():
                    try:
                        incoming = conn.recv(256)
                    except (TimeoutError, socket.timeout):
                        continue
                    if not incoming:
                        break
                    if not authed:
                        pending += incoming
                        if b"\n" not in pending:
                            continue
                        line, pending = pending.split(b"\n", 1)
                        if line.strip() != token:
                            break
                        authed = True
                        say("Phone connected.")
                        if not pending:
                            continue
                        incoming = b""
                    pending = feed_buttons(pending, incoming, held, seen_ignored)
            finally:
                for vk, extended in list(held.values()):
                    send_key(vk, extended, up=True)
                release_stuck_keys()
                conn.close()
                if authed:
                    say("Phone disconnected. The remote is back on the phone until it reconnects.")
    finally:
        server.close()


def run_forever(stop: threading.Event) -> None:
    allow_bridge_port()
    threading.Thread(target=ensure_phone_bridge, daemon=True).start()
    while not stop.is_set():
        try:
            serve_bridge(stop)
            return
        except OSError as exc:
            say(f"Listener stopped ({exc}). Retrying...")
            if stop.wait(2):
                return


def tray_icon(on: bool):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    fill = (214, 242, 92, 255) if on else (90, 94, 82, 255)
    draw.ellipse((8, 8, 56, 56), fill=fill)
    return image


def already_running() -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    global tray_mutex
    tray_mutex = kernel32.CreateMutexW(None, False, "Local\\FireRemoteTray")
    return ctypes.get_last_error() == 183


def serve_tray(start_on: bool) -> None:
    if already_running():
        print("Fire remote toggle is already running in the tray.")
        return
    import pystray

    state = {"on": False}
    stop = threading.Event()
    worker: dict[str, threading.Thread | None] = {"thread": None}

    def set_on(on: bool) -> None:
        if on == state["on"]:
            return
        if on:
            stop.clear()
            thread = threading.Thread(target=run_forever, args=(stop,), daemon=True)
            worker["thread"] = thread
            thread.start()
        else:
            stop.set()
            if bridge.process is not None:
                bridge.process.kill()
            thread = worker["thread"]
            if thread is not None:
                thread.join(timeout=8)
        state["on"] = on
        icon.icon = tray_icon(on)
        icon.title = "Fire remote: On" if on else "Fire remote: Off"

    def toggle(icon, item) -> None:
        set_on(not state["on"])

    def quit_app(icon, item) -> None:
        set_on(False)
        icon.stop()

    def toggle_sound(icon, item) -> None:
        global sound_enabled
        sound_enabled = not sound_enabled
        save_sound_enabled(sound_enabled)
        if sound_enabled:
            threading.Thread(target=play_chime, daemon=True).start()

    icon = pystray.Icon(
        "fire-remote",
        tray_icon(False),
        "Fire remote: Off",
        menu=pystray.Menu(
            pystray.MenuItem("Use remote on this PC", toggle, checked=lambda item: state["on"], default=True),
            pystray.MenuItem("Mode sound", toggle_sound, checked=lambda item: sound_enabled),
            pystray.MenuItem("Quit", quit_app),
        ),
    )

    def setup(icon) -> None:
        icon.visible = True
        if start_on:
            set_on(True)

    icon.run(setup)


def main() -> None:
    parser = argparse.ArgumentParser(description="Forward the Fire TV remote on your phone to this PC.")
    parser.add_argument("--list", action="store_true", help="Print input devices and exit.")
    parser.add_argument("--tray", action="store_true", help="Show a tray toggle instead of running in this window.")
    parser.add_argument("--on", action="store_true", help="With --tray, start already forwarding.")
    parser.add_argument("--setup", action="store_true", help="Download adb if needed and create shortcuts.")
    parser.add_argument("--pair", action="store_true", help="First-time Wi-Fi pairing with the 6-digit code.")
    parser.add_argument(
        "--connect",
        action="store_true",
        help="Reconnect after a phone reboot using the main Wireless debugging IP:port.",
    )
    args = parser.parse_args()

    if args.pair:
        pair_phone()
        return

    if args.connect:
        connect_phone()
        return

    if args.setup:
        ensure_adb()
        install_shortcuts()
        say("Next, on a private network: python ar_remote.py --connect")
        say("Only run python ar_remote.py --pair the first time, or if pairing was revoked.")
        return

    if args.tray:
        serve_tray(args.on)
        return

    if args.list:
        run_adb(["start-server"])
        serial = choose_serial()
        path, name = find_remote(serial)
        print(f"Phone {serial}")
        print(f"Remote {name} at {path}")
        return

    stop = threading.Event()
    try:
        run_forever(stop)
    except KeyboardInterrupt:
        stop.set()
        print("\nStopped.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
