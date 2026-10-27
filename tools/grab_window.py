"""Save a screenshot of a window found by (part of) its title.

Used to check how the GUI actually renders.  A DPI-unaware process sees
virtualised window coordinates, so the rect is scaled by the window's real DPI
before grabbing physical pixels.

    python tools\\grab_window.py                       # default title + output
    python tools\\grab_window.py --title "Switch Pro" --out ui.png
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
try:
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
except OSError:  # pragma: no cover - very old Windows
    shcore = None


def _become_dpi_aware() -> bool:
    """Ask for real pixels before any window query.

    Whether GetWindowRect returns physical or virtualised coordinates depends on
    the *caller's* DPI awareness, so this must happen before the first call.
    """
    try:
        if shcore is not None and shcore.SetProcessDpiAwareness(1) == 0:
            return True
    except Exception:
        pass
    try:
        return bool(user32.SetProcessDPIAware())
    except Exception:
        return False


DPI_AWARE = _become_dpi_aware()


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.EnumWindows.argtypes = [EnumProc, wintypes.LPARAM]
user32.GetDpiForWindow.argtypes = [wintypes.HWND]


def find_window(needle: str, class_name: str | None = None):
    """Largest visible window whose title (and class) match and whose owner lives.

    Titles are matched case-insensitively; passing ``class_name`` (``TkTopLevel``
    for a Tk window) is the reliable way to tell this program's window from another
    with a similar title.  Windows whose owner process is already gone, minimised
    windows and off-screen helpers (rect x at -32000) are ignored -- a killed
    process can leave its window behind briefly, and picking one gives a shot of
    the wrong place.
    """
    from ctypes import wintypes

    found = []
    GetWindowThreadProcessId = user32.GetWindowThreadProcessId
    GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

    def alive(pid: int) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)      # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259                               # STILL_ACTIVE

    def callback(hwnd, _param):
        if not user32.IsWindowVisible(hwnd):
            return True
        buffer = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buffer, 512)
        title = buffer.value
        if needle and needle.casefold() not in title.casefold():
            return True
        if class_name:
            class_buffer = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buffer, 256)
            if not class_buffer.value.startswith(class_name):
                return True
        pid = wintypes.DWORD(0)
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not alive(pid.value):
            print(f"  skipping dead window {title!r} (pid {pid.value})")
            return True
        rect = RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if rect.left > -10000 and width > 300 and height > 200:
                found.append((width * height, hwnd))
        return True

    user32.EnumWindows(EnumProc(callback), 0)
    if not found:
        return None
    found.sort(reverse=True)
    return found[0][1]


def list_windows() -> None:
    print("visible windows with a title and a real size:")
    rows = []

    def callback(hwnd, _param):
        if not user32.IsWindowVisible(hwnd):
            return True
        buffer = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buffer, 512)
        if not buffer.value.strip():
            return True
        rect = RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            rows.append((width * height, hwnd, buffer.value, width, height, rect.left, rect.top))
        return True

    user32.EnumWindows(EnumProc(callback), 0)
    for _area, hwnd, title, width, height, left, top in sorted(rows, reverse=True)[:25]:
        print(f"  0x{hwnd:X} {width:5d}x{height:<5d} at ({left},{top})  {title[:70]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", default="Switch Pro")
    parser.add_argument("--any", action="store_true",
                        help="ignore the title, match by class only")
    parser.add_argument("--list", action="store_true", help="list window titles and exit")
    parser.add_argument("--class-name", default="TkTopLevel",
                        help="require this window class; TkTopLevel is our own window, "
                             "which avoids matching another window with a similar title")
    parser.add_argument("--out", default="gui_screenshot.png")
    parser.add_argument("--wait", type=float, default=20.0,
                        help="seconds to wait for the window to appear")
    args = parser.parse_args()

    if args.list:
        list_windows()
        return 0

    deadline = time.time() + args.wait
    hwnd = None
    while time.time() < deadline:
        hwnd = find_window("" if args.any else args.title, args.class_name)
        if hwnd:
            break
        time.sleep(0.5)
    if not hwnd:
        print(f"window containing {args.title!r} not found")
        return 1

    rect = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    print(f"window {hex(hwnd)}: rect=({rect.left},{rect.top}) {width}x{height} "
          f"dpi={user32.GetDpiForWindow(hwnd) if hasattr(user32, 'GetDpiForWindow') else '?'}")

    # PrintWindow: the window renders itself into a bitmap, so DPI virtualisation
    # and screen coordinates cannot crop the shot at the wrong offset.
    from PIL import Image

    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    window_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    gdi32.SelectObject(mem_dc, bitmap)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32),
                    ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32),
                    ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]

    try:
        rendered = user32.PrintWindow(hwnd, mem_dc, 2)     # PW_RENDERFULLCONTENT
        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth, header.biHeight = width, -height   # top-down rows
        header.biPlanes, header.biBitCount = 1, 32
        buffer = ctypes.create_string_buffer(width * height * 4)
        copied = gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer,
                                 ctypes.byref(header), 0)
        if not rendered or not copied:
            # Some windows refuse to render themselves; fall back to a screen grab
            # of the same rectangle.
            print(f"PrintWindow failed (rendered={rendered}, copied={copied}); "
                  f"falling back to a screen grab")
            from PIL import ImageGrab
            screen = ImageGrab.grab(bbox=(rect.left, rect.top,
                                          rect.left + width, rect.top + height))
            screen.save(args.out)
            print(f"saved {args.out} ({screen.width}x{screen.height})")
            return 0
        image = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1)
        image.convert("RGB").save(args.out)
        print(f"saved {args.out} ({image.width}x{image.height})")
        return 0
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, window_dc)


if __name__ == "__main__":
    raise SystemExit(main())
