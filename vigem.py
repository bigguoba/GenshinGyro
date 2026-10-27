"""Minimal ctypes binding for the ViGEmBus user-mode client (ViGEmClient.dll).

Only the DualShock 4 target is implemented, including the extended report that
carries gyroscope/accelerometer data (``vigem_target_ds4_update_ex``).

The DS4 report layout below is the raw DualShock 4 HID input report: report id
``0x01`` followed by 63 data bytes.  ViGEmClient accepts exactly those 63 data
bytes through ``DS4_REPORT_EX``, so the field offsets here are the same offsets
a real DS4 uses on the wire.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

# --------------------------------------------------------------------------- #
# Error codes (VIGEM_ERROR enum from ViGEmClient.h)
# --------------------------------------------------------------------------- #

VIGEM_ERROR_NONE = 0x20000000

VIGEM_ERROR_NAMES = {
    0x20000000: "VIGEM_ERROR_NONE",
    0xE0000001: "VIGEM_ERROR_BUS_NOT_FOUND",
    0xE0000002: "VIGEM_ERROR_NO_FREE_SLOT",
    0xE0000003: "VIGEM_ERROR_INVALID_TARGET",
    0xE0000004: "VIGEM_ERROR_REMOVAL_FAILED",
    0xE0000005: "VIGEM_ERROR_ALREADY_CONNECTED",
    0xE0000006: "VIGEM_ERROR_TARGET_UNINITIALIZED",
    0xE0000007: "VIGEM_ERROR_TARGET_NOT_PLUGGED_IN",
    0xE0000008: "VIGEM_ERROR_BUS_VERSION_MISMATCH",
    0xE0000009: "VIGEM_ERROR_BUS_ACCESS_FAILED",
    0xE000000A: "VIGEM_ERROR_CALLBACK_ALREADY_REGISTERED",
    0xE000000B: "VIGEM_ERROR_CALLBACK_NOT_FOUND",
    0xE000000C: "VIGEM_ERROR_BUS_ALREADY_CONNECTED",
    0xE000000D: "VIGEM_ERROR_BUS_INVALID_HANDLE",
    0xE000000E: "VIGEM_ERROR_XUSB_USER_INDEX",
    0xE000000F: "VIGEM_ERROR_INVALID_ARGUMENT",
    0xE0000010: "VIGEM_ERROR_NOT_SUPPORTED",
    0xE0000011: "VIGEM_ERROR_WINAPI",
    0xE0000012: "VIGEM_ERROR_TIMED_OUT",
    0xE0000013: "VIGEM_ERROR_IS_DISPOSING",
}


def error_name(code: int) -> str:
    return VIGEM_ERROR_NAMES.get(code, f"unknown ViGEm error 0x{code:08X}")


class ViGEmError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# DS4 report structures
# --------------------------------------------------------------------------- #

#: DS4 button bits, low nibble of ``buttons`` is the d-pad hat (8 = released).
DS4_BUTTONS = {
    "square": 1 << 4,
    "cross": 1 << 5,
    "circle": 1 << 6,
    "triangle": 1 << 7,
    "l1": 1 << 8,
    "r1": 1 << 9,
    "l2": 1 << 10,
    "r2": 1 << 11,
    "share": 1 << 12,
    "options": 1 << 13,
    "l3": 1 << 14,
    "r3": 1 << 15,
}

DS4_SPECIAL_PS = 1 << 0
DS4_SPECIAL_TOUCHPAD = 1 << 1

#: d-pad hat values (``DS4_DPAD_NEUTRAL`` means "no direction pressed").
DS4_DPAD_NEUTRAL = 8


class _DS4ReportExFields(ctypes.Structure):
    """The 63 data bytes of a DS4 input report."""

    _pack_ = 1
    _fields_ = [
        ("bThumbLX", ctypes.c_ubyte),
        ("bThumbLY", ctypes.c_ubyte),
        ("bThumbRX", ctypes.c_ubyte),
        ("bThumbRY", ctypes.c_ubyte),
        ("wButtons", ctypes.c_ushort),
        ("bSpecial", ctypes.c_ubyte),
        ("bTriggerL", ctypes.c_ubyte),
        ("bTriggerR", ctypes.c_ubyte),
        ("wTimestamp", ctypes.c_ushort),
        ("bBatteryLvl", ctypes.c_ubyte),
        ("wGyroX", ctypes.c_short),
        ("wGyroY", ctypes.c_short),
        ("wGyroZ", ctypes.c_short),
        ("wAccelX", ctypes.c_short),
        ("wAccelY", ctypes.c_short),
        ("wAccelZ", ctypes.c_short),
        ("_bUnknown1", ctypes.c_ubyte * 5),
        ("bBatteryLvlSpecial", ctypes.c_ubyte),
        ("_bUnknown2", ctypes.c_ubyte * 2),
        ("bTouchPacketsN", ctypes.c_ubyte),
        ("sCurrentTouch", ctypes.c_ubyte * 4),
        ("sPreviousTouch", (ctypes.c_ubyte * 4) * 2),
        # Trailing reserved bytes, kept so the union matches the 63-byte report.
        ("_bReserved", ctypes.c_ubyte * 18),
    ]


class DS4_REPORT_EX(ctypes.Union):
    _pack_ = 1
    _anonymous_ = ("Report",)
    _fields_ = [
        ("Report", _DS4ReportExFields),
        ("ReportBuffer", ctypes.c_ubyte * 63),
    ]


DS4_REPORT = ctypes.c_ubyte * 9  # not used by this tool, kept for completeness

assert ctypes.sizeof(_DS4ReportExFields) == 63, ctypes.sizeof(_DS4ReportExFields)
assert ctypes.sizeof(DS4_REPORT_EX) == 63, ctypes.sizeof(DS4_REPORT_EX)


def neutral_report() -> DS4_REPORT_EX:
    """A centred DS4 report with no buttons pressed and an untouched touchpad."""
    r = DS4_REPORT_EX()
    ctypes.memset(ctypes.byref(r), 0, ctypes.sizeof(r))
    r.bThumbLX = 128
    r.bThumbLY = 128
    r.bThumbRX = 128
    r.bThumbRY = 128
    r.wButtons = DS4_DPAD_NEUTRAL
    r.bBatteryLvl = 0x0B  # ~ full, as a real DS4 reports on USB power
    r.bTouchPacketsN = 1
    # Bit 7 set on the touch status byte means "finger up".
    for touch in (r.sCurrentTouch, r.sPreviousTouch[0], r.sPreviousTouch[1]):
        touch[0] = 0x00  # packet counter
        touch[1] = 0x80  # finger 1 up
        touch[2] = 0x00
        touch[3] = 0x00
    return r


# --------------------------------------------------------------------------- #
# Library loading and function signatures
# --------------------------------------------------------------------------- #

def _candidate_dll_paths() -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "vendor", "ViGEmClient.dll"),
        os.path.join(here, "ViGEmClient.dll"),
    ]
    # Frozen (PyInstaller) builds unpack bundled files under sys._MEIPASS.
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.insert(0, os.path.join(bundle, "vendor", "ViGEmClient.dll"))
    env = os.environ.get("VIGEMCLIENT_DLL")
    if env:
        candidates.insert(0, env)
    # ViGEmBus used to install the client next to the driver.
    candidates.append(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "ViGEmClient.dll"))
    return candidates


def load_client_dll(path: str | None = None):
    """Load ViGEmClient.dll, preferring the copy shipped in ``vendor/``."""
    if path:
        return ctypes.WinDLL(path)
    tried = []
    for candidate in _candidate_dll_paths():
        if not os.path.isfile(candidate):
            tried.append(f"{candidate} (missing)")
            continue
        try:
            return ctypes.WinDLL(candidate)
        except OSError as exc:  # pragma: no cover - depends on host
            tried.append(f"{candidate} ({exc})")
    raise ViGEmError("ViGEmClient.dll not found; looked at:\n  " + "\n  ".join(tried))


def _bind(dll) -> None:
    dll.vigem_alloc.restype = ctypes.c_void_p
    dll.vigem_alloc.argtypes = []

    dll.vigem_free.restype = None
    dll.vigem_free.argtypes = [ctypes.c_void_p]

    dll.vigem_connect.restype = ctypes.c_uint
    dll.vigem_connect.argtypes = [ctypes.c_void_p]

    dll.vigem_disconnect.restype = None
    dll.vigem_disconnect.argtypes = [ctypes.c_void_p]

    dll.vigem_target_ds4_alloc.restype = ctypes.c_void_p
    dll.vigem_target_ds4_alloc.argtypes = []

    dll.vigem_target_add.restype = ctypes.c_uint
    dll.vigem_target_add.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    dll.vigem_target_remove.restype = ctypes.c_uint
    dll.vigem_target_remove.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    dll.vigem_target_free.restype = None
    dll.vigem_target_free.argtypes = [ctypes.c_void_p]

    dll.vigem_target_ds4_update.restype = ctypes.c_uint
    dll.vigem_target_ds4_update.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ubyte),  # 9-byte basic report
    ]

    dll.vigem_target_ds4_update_ex.restype = ctypes.c_uint
    dll.vigem_target_ds4_update_ex.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(DS4_REPORT_EX),
    ]


# --------------------------------------------------------------------------- #
# High level API
# --------------------------------------------------------------------------- #

class ViGEmClient:
    """Owns the connection to the ViGEmBus driver."""

    def __init__(self, dll_path: str | None = None):
        self._dll = load_client_dll(dll_path)
        _bind(self._dll)
        self._client = self._dll.vigem_alloc()
        if not self._client:
            raise ViGEmError("vigem_alloc() failed")
        self._connected = False

    def connect(self) -> None:
        code = self._dll.vigem_connect(self._client)
        if code != VIGEM_ERROR_NONE:
            raise ViGEmError(f"vigem_connect() failed: {error_name(code)}")
        self._connected = True

    @property
    def connected(self) -> bool:
        return self._connected

    def create_ds4(self) -> "DS4Controller":
        if not self._connected:
            raise ViGEmError("client is not connected")
        target = self._dll.vigem_target_ds4_alloc()
        if not target:
            raise ViGEmError("vigem_target_ds4_alloc() failed")
        return DS4Controller(self, target)

    # -- internals used by DS4Controller ---------------------------------- #

    def _plug(self, target) -> None:
        code = self._dll.vigem_target_add(self._client, target)
        if code != VIGEM_ERROR_NONE:
            raise ViGEmError(f"vigem_target_add() failed: {error_name(code)}")

    def _unplug(self, target) -> None:
        self._dll.vigem_target_remove(self._client, target)

    def _free_target(self, target) -> None:
        self._dll.vigem_target_free(target)

    def _update_ex(self, target, report: DS4_REPORT_EX) -> int:
        return self._dll.vigem_target_ds4_update_ex(self._client, target, ctypes.byref(report))

    def close(self) -> None:
        if self._client:
            if self._connected:
                self._dll.vigem_disconnect(self._client)
                self._connected = False
            self._dll.vigem_free(self._client)
            self._client = None

    def __enter__(self) -> "ViGEmClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class DS4Controller:
    """A virtual DualShock 4 plugged into the system."""

    def __init__(self, client: ViGEmClient, target):
        self._client = client
        self._target = target
        self._plugged = False
        self._report = neutral_report()

    @property
    def report(self) -> DS4_REPORT_EX:
        return self._report

    def plug_in(self) -> None:
        self._client._plug(self._target)
        self._plugged = True

    def unplug(self) -> None:
        if self._plugged:
            self._client._unplug(self._target)
            self._plugged = False

    def submit(self) -> None:
        """Push the current report to the driver."""
        code = self._client._update_ex(self._target, self._report)
        if code != VIGEM_ERROR_NONE:
            raise ViGEmError(f"vigem_target_ds4_update_ex() failed: {error_name(code)}")

    def reset(self) -> None:
        self._report = neutral_report()
        self.submit()

    def set_sticks(self, lx: int, ly: int, rx: int, ry: int) -> None:
        self._report.bThumbLX = max(0, min(255, lx))
        self._report.bThumbLY = max(0, min(255, ly))
        self._report.bThumbRX = max(0, min(255, rx))
        self._report.bThumbRY = max(0, min(255, ry))

    def set_triggers(self, left: int, right: int) -> None:
        self._report.bTriggerL = max(0, min(255, left))
        self._report.bTriggerR = max(0, min(255, right))

    def set_buttons(self, buttons: int, special: int = 0) -> None:
        self._report.wButtons = buttons
        self._report.bSpecial = special

    def set_motion(self, gyro, accel) -> None:
        gx, gy, gz = gyro
        ax, ay, az = accel
        self._report.wGyroX = _clamp_i16(gx)
        self._report.wGyroY = _clamp_i16(gy)
        self._report.wGyroZ = _clamp_i16(gz)
        self._report.wAccelX = _clamp_i16(ax)
        self._report.wAccelY = _clamp_i16(ay)
        self._report.wAccelZ = _clamp_i16(az)

    def set_battery(self, level: int, cable_connected: bool = False) -> None:
        """``level`` 0..10 (0 empty, 10 full), as a real DualShock 4 reports it."""
        self.set_battery_byte((level & 0x0F) | (0x10 if cable_connected else 0x00))

    def set_battery_byte(self, value: int) -> None:
        """Write the raw battery byte.

        Real hardware carries it in the byte at payload offset 29 -- the field
        ViGEmClient calls ``bBatteryLvlSpecial`` (a real pad leaves ``bBatteryLvl``
        at offset 11 as zero, confirmed by capturing a physical DualShock 4).
        """
        self._report.bBatteryLvl = 0
        self._report.bBatteryLvlSpecial = value & 0xFF

    def __del__(self):  # pragma: no cover - best effort cleanup
        try:
            self.unplug()
        except Exception:
            pass
        try:
            self._client._free_target(self._target)
        except Exception:
            pass


def _clamp_i16(value: int) -> int:
    if value > 32767:
        return 32767
    if value < -32768:
        return -32768
    return int(value)
