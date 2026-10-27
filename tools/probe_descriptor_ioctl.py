"""Probe candidate IOCTL codes for reading a HID report descriptor."""
import ctypes
import sys
import os
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hidwin

kernel32 = hidwin.kernel32

CANDIDATES = [
    0x000B0190,   # commonly cited in C# samples
    0x000B0192,
    0x000B0191,
    0x000B0007,   # CTL_CODE(FILE_DEVICE_KEYBOARD, 1, METHOD_NEITHER, ANY)
    0x000B0004,
    0x000B0008,
    0x000B000C,
]

target = None
for info in hidwin.enumerate_hid():
    if info.get("vid") == 0x057E and info.get("pid") == 0x2009:
        target = info
        break
if target is None:
    print("Switch controller not found")
    raise SystemExit(1)

print(f"probing against: {hidwin.describe(target)}")
handle = hidwin._open_handle(target["path"], read=True, write=False)
try:
    for code in CANDIDATES:
        for size in (512, 1024):
            buf = ctypes.create_string_buffer(size)
            returned = wintypes.DWORD(0)
            ok = kernel32.DeviceIoControl(handle, code, None, 0, buf, size,
                                          ctypes.byref(returned), None)
            if ok and returned.value:
                data = buf.raw[:returned.value]
                print(f"  code 0x{code:08X} size {size}: OK, {returned.value} bytes, "
                      f"starts {data[:8].hex(' ')}")
                break
            else:
                err = ctypes.get_last_error()
                print(f"  code 0x{code:08X} size {size}: fail (err {err})")
finally:
    kernel32.CloseHandle(handle)
