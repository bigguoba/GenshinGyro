"""List every top-level window a process owns, with its window class.

Used to prove that the windowed build really has no console window: a console
would show up as a ``ConsoleWindowClass`` owned by the same PID.

    python tools\\list_process_windows.py --name GenshinGyro
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import sys

user32 = ctypes.WinDLL("user32", use_last_error=True)
EnumWindows = user32.EnumWindows
EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                           wintypes.LPARAM), wintypes.LPARAM]
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
GetClassNameW = user32.GetClassNameW
GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
GetWindowTextW = user32.GetWindowTextW
GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
IsWindowVisible = user32.IsWindowVisible
IsWindowVisible.argtypes = [wintypes.HWND]

import subprocess  # noqa: E402


def pids_named(name: str) -> list[int]:
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/FO", "CSV"],
                         capture_output=True, text=True, errors="replace").stdout
    pids = []
    for line in out.splitlines()[1:]:
        parts = [part.strip('"') for part in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="GenshinGyro")
    parser.add_argument("--pid", type=int, action="append", default=None,
                        help="check these process ids instead of looking up a name")
    args = parser.parse_args()

    pids = args.pid if args.pid else pids_named(args.name)
    print(f"checking pids: {pids or 'none'}")
    rows = []

    def callback(hwnd, _param):
        pid = wintypes.DWORD(0)
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            class_name = ctypes.create_unicode_buffer(256)
            GetClassNameW(hwnd, class_name, 256)
            title = ctypes.create_unicode_buffer(512)
            GetWindowTextW(hwnd, title, 512)
            rows.append((pid.value, class_name.value, title.value,
                         bool(IsWindowVisible(hwnd))))
        return True

    EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(callback),
                0)
    print(f"windows owned by the process: {len(rows)}")
    consoles = 0
    for pid, class_name, title, visible in rows:
        marker = ""
        if "ConsoleWindowClass" in class_name:
            consoles += 1
            marker = "   <-- CONSOLE WINDOW"
        print(f"  pid={pid} class={class_name!r} visible={visible} title={title!r}{marker}")
    print(f"console windows: {consoles}")
    return 0 if consoles == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
