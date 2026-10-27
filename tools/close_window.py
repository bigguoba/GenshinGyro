"""Send WM_CLOSE to a window, i.e. simulate clicking its X button.

    python tools\\close_window.py --class-name TkTopLevel
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
GetWindowTextW = user32.GetWindowTextW
GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
GetClassNameW = user32.GetClassNameW
GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
IsWindowVisible = user32.IsWindowVisible
IsWindowVisible.argtypes = [wintypes.HWND]
PostMessageW = user32.PostMessageW
PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

WM_CLOSE = 0x0010


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--class-name", default="TkTopLevel")
    parser.add_argument("--title", default="")
    args = parser.parse_args()

    targets = []

    def callback(hwnd, _param):
        if not user32.IsWindowVisible(hwnd):
            return True
        class_buffer = ctypes.create_unicode_buffer(256)
        GetClassNameW(hwnd, class_buffer, 256)
        if class_buffer.value != args.class_name:
            return True
        title_buffer = ctypes.create_unicode_buffer(512)
        GetWindowTextW(hwnd, title_buffer, 512)
        if args.title and args.title.casefold() not in title_buffer.value.casefold():
            return True
        targets.append((hwnd, title_buffer.value))
        return True

    EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(callback),
               0)
    if not targets:
        print(f"no visible window with class {args.class_name!r}")
        return 1
    for hwnd, title in targets:
        print(f"posting WM_CLOSE to 0x{hwnd:X} {title!r}")
        PostMessageW(hwnd, WM_CLOSE, 0, 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
