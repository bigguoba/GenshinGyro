"""Small Win32 HID layer built directly on ctypes (no third-party packages).

Provides device enumeration via SetupAPI and overlapped (timeout-capable) reads
and writes through the HID class driver.  Everything this tool needs from a HID
device goes through here: the Nintendo Switch Pro Controller (physical) and the
virtual DualShock 4 exposed by ViGEmBus (used for self-tests).
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

GUID_DEVINTERFACE_HID = "{4D1E55B2-F16F-11CF-88CB-001111000030}"

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x01
FILE_SHARE_WRITE = 0x02
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000

INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102

HIDP_STATUS_SUCCESS = 0x00110000

#: DeviceIoControl code that returns a HID device's report descriptor.
IOCTL_HID_GET_REPORT_DESCRIPTOR = 0x000B0190

#: Report-descriptor usages that identify a gamepad-ish collection.
USAGE_PAGE_GENERIC_DESKTOP = 0x01
USAGE_GENERIC_DESKTOP_GAMEPAD = 0x05
USAGE_GENERIC_DESKTOP_JOYSTICK = 0x04
USAGE_GENERIC_DESKTOP_MULTI_AXIS = 0x08
USAGE_PAGE_VENDOR_DEFINED = 0xFF00


ole32 = ctypes.WinDLL("ole32")
ole32.CLSIDFromString.restype = ctypes.c_long


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, text: str | None = None):
        super().__init__()
        if text:
            if ole32.CLSIDFromString(text, ctypes.byref(self)) != 0:
                raise ValueError(f"bad GUID: {text}")


ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.POINTER(ctypes.c_ulong)),
    ]


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", GUID),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Size", ctypes.c_ulong),
        ("VendorID", ctypes.c_ushort),
        ("ProductID", ctypes.c_ushort),
        ("VersionNumber", ctypes.c_ushort),
    ]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberInputDataIndices", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberOutputDataIndices", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
        ("NumberFeatureDataIndices", ctypes.c_ushort),
    ]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.POINTER(ctypes.c_ulong)),
        ("InternalHigh", ctypes.POINTER(ctypes.c_ulong)),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


# --------------------------------------------------------------------------- #
# Function signatures
# --------------------------------------------------------------------------- #

setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD,
]

setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
]

setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]

setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]

setupapi.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.LPWSTR,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]

setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]

SPDRP_DEVICEDESC = 0x00
SPDRP_ENUMERATOR_NAME = 0x16

kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

kernel32.ReadFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED),
]

kernel32.WriteFile.restype = wintypes.BOOL
kernel32.WriteFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED),
]

kernel32.GetOverlappedResult.restype = wintypes.BOOL
kernel32.GetOverlappedResult.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD),
    wintypes.BOOL,
]

kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [
    ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR,
]

kernel32.CancelIoEx.restype = wintypes.BOOL
kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]

kernel32.ResetEvent.restype = wintypes.BOOL
kernel32.ResetEvent.argtypes = [wintypes.HANDLE]

hid.HidD_GetAttributes.restype = wintypes.BOOL
hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]

hid.HidD_GetPreparsedData.restype = wintypes.BOOL
hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]

hid.HidD_FreePreparsedData.restype = wintypes.BOOL
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]

hid.HidP_GetCaps.restype = ctypes.c_long
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]

for name in ("HidD_GetProductString", "HidD_GetManufacturerString", "HidD_GetSerialNumberString"):
    fn = getattr(hid, name)
    fn.restype = wintypes.BOOL
    fn.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_ulong]

hid.HidD_SetNumInputBuffers.restype = wintypes.BOOL
hid.HidD_SetNumInputBuffers.argtypes = [wintypes.HANDLE, ctypes.c_ulong]

hid.HidD_SetOutputReport.restype = wintypes.BOOL
hid.HidD_SetOutputReport.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_ulong]

hid.HidD_GetFeature.restype = wintypes.BOOL
hid.HidD_GetFeature.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_ulong]

hid.HidD_SetFeature.restype = wintypes.BOOL
hid.HidD_SetFeature.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_ulong]

kernel32.DeviceIoControl.restype = wintypes.BOOL
kernel32.DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]

cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
cfgmgr32.CM_Locate_DevNodeW.restype = wintypes.DWORD
cfgmgr32.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(wintypes.DWORD),
                                       wintypes.LPCWSTR, wintypes.DWORD]
cfgmgr32.CM_Get_Parent.restype = wintypes.DWORD
cfgmgr32.CM_Get_Parent.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                   wintypes.DWORD]
cfgmgr32.CM_Get_Device_IDW.restype = wintypes.DWORD
cfgmgr32.CM_Get_Device_IDW.argtypes = [wintypes.DWORD, wintypes.LPWSTR,
                                       wintypes.DWORD, wintypes.DWORD]

CM_LOCATE_DEVNODE_NORMAL = 0x00000000


def device_ancestry(instance_id: str, depth: int = 6) -> list[str]:
    """Device instance ids of the parents, nearest first.

    A physical pad sits under ``USB\\...`` / ``BTHENUM\\...``; a pad created by
    ViGEmBus sits under a virtual bus (``ROOT\\...``).  That is how an emulated
    DualShock 4 can be told apart from real hardware, since they share a pid.
    """
    devinst = wintypes.DWORD(0)
    if cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(devinst), instance_id,
                                   CM_LOCATE_DEVNODE_NORMAL) != 0:
        return []
    chain: list[str] = []
    current = devinst
    for _ in range(depth):
        parent = wintypes.DWORD(0)
        if cfgmgr32.CM_Get_Parent(ctypes.byref(parent), current, 0) != 0:
            break
        buffer = ctypes.create_unicode_buffer(512)
        if cfgmgr32.CM_Get_Device_IDW(parent, buffer, ctypes.sizeof(buffer) // 2, 0) != 0:
            break
        chain.append(buffer.value)
        current = parent
    return chain


def is_emulated(info: dict) -> bool:
    """True when the device lives on a virtual bus (e.g. a ViGEm target)."""
    for ancestor in device_ancestry(info.get("instance_id") or ""):
        upper = ancestor.upper()
        if upper.startswith("ROOT"):
            return True
    return False


cfgmgr32.CM_Get_DevNode_Status.restype = wintypes.DWORD
cfgmgr32.CM_Get_DevNode_Status.argtypes = [
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    wintypes.DWORD, wintypes.DWORD,
]

DN_STARTED = 0x00000008
DN_DEVICE_DISCONNECTED = 0x02000000
DN_NEED_RESTART = 0x00000100
DN_HAS_PROBLEM = 0x00000400


def device_status(instance_id: str) -> dict:
    """Whether a device node is really started/connected right now.

    A paired-but-off Bluetooth pad still enumerates, so reading it can return a
    stale cached report; this distinguishes that from live hardware.
    """
    devinst = wintypes.DWORD(0)
    if cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(devinst), instance_id,
                                   CM_LOCATE_DEVNODE_NORMAL) != 0:
        return {"found": False}
    status = wintypes.DWORD(0)
    problem = wintypes.DWORD(0)
    if cfgmgr32.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem),
                                      devinst, 0) != 0:
        return {"found": True, "status": None}
    return {
        "found": True,
        "started": bool(status.value & DN_STARTED),
        "disconnected": bool(status.value & DN_DEVICE_DISCONNECTED),
        "problem": problem.value,
        "status": status.value,
    }


def transport_of(info: dict) -> str:
    for ancestor in device_ancestry(info.get("instance_id") or ""):
        upper = ancestor.upper()
        if upper.startswith("USB"):
            return "usb"
        if upper.startswith("BTHENUM") or upper.startswith("BTHLE"):
            return "bluetooth"
    return "unknown"


def get_report_descriptor(handle, max_size: int = 8192) -> bytes:
    """Read a HID device's report descriptor through the HID class driver."""
    buf = ctypes.create_string_buffer(max_size)
    returned = wintypes.DWORD(0)
    ok = kernel32.DeviceIoControl(
        handle, IOCTL_HID_GET_REPORT_DESCRIPTOR, None, 0,
        buf, max_size, ctypes.byref(returned), None,
    )
    if not ok:
        raise OSError(ctypes.get_last_error(),
                      "IOCTL_HID_GET_REPORT_DESCRIPTOR failed")
    return buf.raw[: returned.value]


def report_descriptor_for_path(path: str) -> bytes:
    handle = _open_handle(path, read=True, write=False)
    try:
        return get_report_descriptor(handle)
    finally:
        kernel32.CloseHandle(handle)


class _ValueCapsRange(ctypes.Structure):
    _fields_ = [
        ("UsageMin", ctypes.c_ushort), ("UsageMax", ctypes.c_ushort),
        ("StringMin", ctypes.c_ushort), ("StringMax", ctypes.c_ushort),
        ("DesignatorMin", ctypes.c_ushort), ("DesignatorMax", ctypes.c_ushort),
        ("DataIndexMin", ctypes.c_ushort), ("DataIndexMax", ctypes.c_ushort),
    ]


class _ValueCapsNotRange(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_ushort), ("Reserved1", ctypes.c_ushort),
        ("StringIndex", ctypes.c_ushort), ("Reserved2", ctypes.c_ushort),
        ("DesignatorIndex", ctypes.c_ushort), ("Reserved3", ctypes.c_ushort),
        ("DataIndex", ctypes.c_ushort), ("Reserved4", ctypes.c_ushort),
    ]


class _ValueCapsUnion(ctypes.Union):
    _fields_ = [("Range", _ValueCapsRange), ("NotRange", _ValueCapsNotRange)]


class HIDP_VALUE_CAPS(ctypes.Structure):
    """HIDP_VALUE_CAPS (ntddhid.h) -- what a device declares for one value field."""

    _fields_ = [
        ("UsagePage", ctypes.c_ushort),
        ("ReportID", ctypes.c_ubyte),
        ("IsAlias", ctypes.c_ubyte),
        ("BitField", ctypes.c_ushort),
        ("LinkCollection", ctypes.c_ushort),
        ("LinkUsage", ctypes.c_ushort),
        ("LinkUsagePage", ctypes.c_ushort),
        ("IsRange", ctypes.c_ubyte),
        ("IsStringRange", ctypes.c_ubyte),
        ("IsDesignatorRange", ctypes.c_ubyte),
        ("IsAbsolute", ctypes.c_ubyte),
        ("HasNull", ctypes.c_ubyte),
        ("Reserved", ctypes.c_ubyte),
        ("BitSize", ctypes.c_ushort),
        ("ReportCount", ctypes.c_ushort),
        ("Reserved2", ctypes.c_ushort * 5),
        ("UnitsExp", ctypes.c_ulong),
        ("Units", ctypes.c_ulong),
        ("LogicalMin", ctypes.c_long),
        ("LogicalMax", ctypes.c_long),
        ("PhysicalMin", ctypes.c_long),
        ("PhysicalMax", ctypes.c_long),
        ("Value", _ValueCapsUnion),
    ]


hid.HidP_GetValueCaps.restype = ctypes.c_long
hid.HidP_GetValueCaps.argtypes = [
    ctypes.c_int, ctypes.POINTER(HIDP_VALUE_CAPS),
    ctypes.POINTER(ctypes.c_ushort), ctypes.c_void_p,
]


def input_value_caps(path: str) -> tuple[dict, list[dict]]:
    """Return (device info, input value fields) as a game would read them."""
    handle = _open_handle(path, read=True, write=False)
    try:
        preparsed = ctypes.c_void_p()
        if not hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)) or not preparsed:
            raise OSError("HidD_GetPreparsedData failed")
        try:
            caps = HIDP_CAPS()
            if hid.HidP_GetCaps(preparsed, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
                raise OSError("HidP_GetCaps failed")
            count = ctypes.c_ushort(caps.NumberInputValueCaps or 0)
            fields = []
            if count.value:
                array = (HIDP_VALUE_CAPS * count.value)()
                length = ctypes.c_ushort(count.value)
                if hid.HidP_GetValueCaps(0, array, ctypes.byref(length), preparsed) == HIDP_STATUS_SUCCESS:
                    for index in range(length.value):
                        item = array[index]
                        usage = (item.Value.NotRange.Usage if not item.IsRange
                                 else item.Value.Range.UsageMin)
                        fields.append({
                            "usage_page": item.UsagePage,
                            "usage": usage,
                            "usage_max": item.Value.Range.UsageMax if item.IsRange else usage,
                            "is_range": bool(item.IsRange),
                            "report_id": item.ReportID,
                            "bit_size": item.BitSize,
                            "report_count": item.ReportCount,
                            "logical_min": item.LogicalMin,
                            "logical_max": item.LogicalMax,
                            "physical_min": item.PhysicalMin,
                            "physical_max": item.PhysicalMax,
                            "units": item.Units,
                            "units_exp": item.UnitsExp,
                            "absolute": bool(item.IsAbsolute),
                        })
        finally:
            hid.HidD_FreePreparsedData(preparsed)
    finally:
        kernel32.CloseHandle(handle)
    return query_device(path), fields


def report_descriptors_from_registry(vid: int, pid: int) -> list[tuple[str, bytes]]:
    """Report descriptors Windows cached for a VID/PID, newest instances first.

    Works even for devices that are currently unplugged, which is handy when
    comparing an emulated device against real hardware.
    """
    import winreg

    results: list[tuple[str, bytes]] = []
    wanted = f"VID_{vid:04X}&PID_{pid:04X}"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                              r"SYSTEM\CurrentControlSet\Enum\HID")
    except OSError:
        return results
    with root:
        count = winreg.QueryInfoKey(root)[0]
        for index in range(count):
            try:
                hardware_id = winreg.EnumKey(root, index)
            except OSError:
                continue
            if wanted.lower() not in hardware_id.lower():
                continue
            try:
                instances = winreg.OpenKey(root, hardware_id)
            except OSError:
                continue
            with instances:
                instance_count = winreg.QueryInfoKey(instances)[0]
                for instance_index in range(instance_count):
                    try:
                        instance = winreg.EnumKey(instances, instance_index)
                    except OSError:
                        continue
                    key_path = rf"{hardware_id}\{instance}\Device Parameters"
                    try:
                        with winreg.OpenKey(instances, rf"{instance}\Device Parameters") as params:
                            blob, _kind = winreg.QueryValueEx(params, "ReportDescriptor")
                    except OSError:
                        continue
                    if isinstance(blob, bytes) and blob:
                        results.append((key_path, blob))
    return results


# --------------------------------------------------------------------------- #
# Enumeration
# --------------------------------------------------------------------------- #

def _open_handle(path: str, read: bool = True, write: bool = True, overlapped: bool = True):
    access = 0
    if read:
        access |= GENERIC_READ
    if write:
        access |= GENERIC_WRITE
    flags = FILE_FLAG_OVERLAPPED if overlapped else 0
    handle = kernel32.CreateFileW(
        path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, flags, None
    )
    if handle == INVALID_HANDLE_VALUE and access != 0:
        # Some devices (or a restrictive security descriptor) only allow query access.
        handle = kernel32.CreateFileW(
            path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, flags, None
        )
    if handle == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), f"CreateFileW failed for {path}")
    return handle


def _wide_string(handle, getter, length: int = 126) -> str:
    buf = ctypes.create_unicode_buffer(length + 2)
    if not getter(handle, buf, ctypes.sizeof(buf)):
        return ""
    return buf.value


def query_device(path: str) -> dict:
    """Read VID/PID/caps/strings for a device interface path (open + close)."""
    handle = _open_handle(path, read=False, write=False)
    info = {"path": path}
    try:
        attrs = HIDD_ATTRIBUTES()
        attrs.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
        if hid.HidD_GetAttributes(handle, ctypes.byref(attrs)):
            info["vid"] = attrs.VendorID
            info["pid"] = attrs.ProductID
            info["version"] = attrs.VersionNumber
        else:
            info["vid"] = info["pid"] = info["version"] = None

        preparsed = ctypes.c_void_p()
        if hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)) and preparsed:
            try:
                caps = HIDP_CAPS()
                status = hid.HidP_GetCaps(preparsed, ctypes.byref(caps))
                if status == HIDP_STATUS_SUCCESS:
                    info["usage_page"] = caps.UsagePage
                    info["usage"] = caps.Usage
                    info["input_len"] = caps.InputReportByteLength
                    info["output_len"] = caps.OutputReportByteLength
                    info["feature_len"] = caps.FeatureReportByteLength
            finally:
                hid.HidD_FreePreparsedData(preparsed)

        info["product"] = _wide_string(handle, hid.HidD_GetProductString)
        info["manufacturer"] = _wide_string(handle, hid.HidD_GetManufacturerString)
        info["serial"] = _wide_string(handle, hid.HidD_GetSerialNumberString)
    finally:
        kernel32.CloseHandle(handle)
    return info


def _device_instance_id(hdev, devinfo) -> str:
    """Device instance id, e.g. ``USB\\VID_054C&PID_09CC\\...`` or ``ROOT\\...``."""
    buffer = ctypes.create_unicode_buffer(512)
    required = wintypes.DWORD(0)
    if setupapi.SetupDiGetDeviceInstanceIdW(
        hdev, ctypes.byref(devinfo), buffer, ctypes.sizeof(buffer), ctypes.byref(required)
    ):
        return buffer.value
    return ""


def _device_property(hdev, devinfo, property_id: int) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    required = wintypes.DWORD(0)
    property_type = wintypes.DWORD(0)
    if setupapi.SetupDiGetDeviceRegistryPropertyW(
        hdev, ctypes.byref(devinfo), property_id, ctypes.byref(property_type),
        buffer, ctypes.sizeof(buffer), ctypes.byref(required),
    ):
        return buffer.value
    return ""


def enumerate_hid() -> list[dict]:
    """All present HID device interfaces with their capabilities."""
    guid = GUID(GUID_DEVINTERFACE_HID)
    devices = []
    hdev = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    )
    if hdev == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "SetupDiGetClassDevsW failed")
    try:
        index = 0
        while True:
            did = SP_DEVICE_INTERFACE_DATA()
            did.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                hdev, None, ctypes.byref(guid), index, ctypes.byref(did)
            ):
                break
            index += 1
            required = wintypes.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, ctypes.byref(did), None, 0, ctypes.byref(required), None
            )
            if not required.value:
                continue
            buf = ctypes.create_string_buffer(required.value)
            # sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W) is 8 on 64-bit, 6 on 32-bit.
            ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint32))[0] = 8 if sys.maxsize > 2**32 else 6
            devinfo = SP_DEVINFO_DATA()
            devinfo.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, ctypes.byref(did), buf, required.value, None, ctypes.byref(devinfo)
            ):
                continue
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            try:
                info = query_device(path)
            except OSError:
                info = {"path": path, "error": "open failed"}
            info["instance_id"] = _device_instance_id(hdev, devinfo)
            info["enumerator"] = _device_property(hdev, devinfo, SPDRP_ENUMERATOR_NAME)
            devices.append(info)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return devices


# --------------------------------------------------------------------------- #
# Device I/O
# --------------------------------------------------------------------------- #

class HidDevice:
    """An opened HID device with timeout-capable overlapped I/O."""

    def __init__(self, path: str, info: dict | None = None):
        self.path = path
        self.info = info if info is not None else query_device(path)
        self._handle = None
        self._read_event = None
        self._write_event = None
        self.input_len = int(self.info.get("input_len") or 64)
        self.output_len = int(self.info.get("output_len") or 64)
        self.feature_len = int(self.info.get("feature_len") or 64)

    # -- lifecycle -------------------------------------------------------- #

    def open(self) -> "HidDevice":
        self._handle = _open_handle(self.path, read=True, write=True, overlapped=True)
        self._read_event = kernel32.CreateEventW(None, True, False, None)
        self._write_event = kernel32.CreateEventW(None, True, False, None)
        if not self._read_event or not self._write_event:
            raise OSError("CreateEventW failed")
        hid.HidD_SetNumInputBuffers(self._handle, 64)
        return self

    def close(self) -> None:
        if self._handle:
            try:
                kernel32.CancelIoEx(self._handle, None)
            except Exception:
                pass
            kernel32.CloseHandle(self._handle)
            self._handle = None
        for attr in ("_read_event", "_write_event"):
            event = getattr(self, attr)
            if event:
                kernel32.CloseHandle(event)
                setattr(self, attr, None)

    def __enter__(self) -> "HidDevice":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    def __repr__(self) -> str:
        vid = self.info.get("vid")
        pid = self.info.get("pid")
        return (
            f"<HidDevice {self.info.get('product')!r} "
            f"vid=0x{vid:04X} pid=0x{pid:04X} "
            f"usage=0x{self.info.get('usage_page', 0):04X}:0x{self.info.get('usage', 0):04X} "
            f"in={self.input_len} out={self.output_len}>"
        ) if vid is not None else f"<HidDevice {self.path}>"

    # -- I/O -------------------------------------------------------------- #

    def read(self, timeout_ms: int = 1000, length: int | None = None) -> bytes | None:
        """Read one input report; ``None`` on timeout or when the device vanished."""
        if not self._handle:
            raise RuntimeError("device is not open")
        size = length or self.input_len
        buf = ctypes.create_string_buffer(max(size, 1))
        overlapped = OVERLAPPED()
        overlapped.hEvent = self._read_event
        kernel32.ResetEvent(self._read_event)
        read = wintypes.DWORD(0)
        ok = kernel32.ReadFile(self._handle, buf, size, ctypes.byref(read), ctypes.byref(overlapped))
        if not ok:
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                raise OSError(err, "ReadFile failed")
            wait = kernel32.WaitForSingleObject(self._read_event, timeout_ms)
            if wait == WAIT_TIMEOUT:
                kernel32.CancelIoEx(self._handle, ctypes.byref(overlapped))
                # Wait for the cancellation to complete so the event is reusable.
                kernel32.WaitForSingleObject(self._read_event, 1000)
                kernel32.GetOverlappedResult(
                    self._handle, ctypes.byref(overlapped), ctypes.byref(read), False
                )
                return None
            if wait != WAIT_OBJECT_0:
                return None
            if not kernel32.GetOverlappedResult(
                self._handle, ctypes.byref(overlapped), ctypes.byref(read), False
            ):
                # The read completed with an error: the device was removed.
                raise OSError(ctypes.get_last_error(), "device read failed (removed?)")
        return buf.raw[: read.value]

    def write(self, data: bytes, timeout_ms: int = 500) -> int:
        """Write one output report through WriteFile (interrupt OUT endpoint)."""
        if not self._handle:
            raise RuntimeError("device is not open")
        size = len(data)
        buf = ctypes.create_string_buffer(bytes(data), size)
        overlapped = OVERLAPPED()
        overlapped.hEvent = self._write_event
        kernel32.ResetEvent(self._write_event)
        written = wintypes.DWORD(0)
        ok = kernel32.WriteFile(self._handle, buf, size, ctypes.byref(written), ctypes.byref(overlapped))
        if not ok:
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                raise OSError(err, "WriteFile failed")
            wait = kernel32.WaitForSingleObject(self._write_event, timeout_ms)
            if wait != WAIT_OBJECT_0:
                kernel32.CancelIoEx(self._handle, ctypes.byref(overlapped))
                kernel32.WaitForSingleObject(self._write_event, 1000)
                raise TimeoutError("WriteFile timed out")
            if not kernel32.GetOverlappedResult(
                self._handle, ctypes.byref(overlapped), ctypes.byref(written), False
            ):
                raise OSError(ctypes.get_last_error(), "GetOverlappedResult failed")
        return written.value

    def set_output_report(self, data: bytes) -> bool:
        """Send an output report as a control transfer (HidD_SetOutputReport)."""
        if not self._handle:
            raise RuntimeError("device is not open")
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        return bool(hid.HidD_SetOutputReport(self._handle, buf, len(data)))

    def get_feature(self, report_id: int, length: int | None = None) -> bytes | None:
        if not self._handle:
            raise RuntimeError("device is not open")
        size = length or self.feature_len
        buf = ctypes.create_string_buffer(max(size, 1))
        buf[0] = bytes([report_id & 0xFF])
        if not hid.HidD_GetFeature(self._handle, buf, size):
            return None
        return buf.raw[:size]

    def set_feature(self, data: bytes) -> bool:
        if not self._handle:
            raise RuntimeError("device is not open")
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        return bool(hid.HidD_SetFeature(self._handle, buf, len(data)))

    def drain(self, max_reports: int = 64, timeout_ms: int = 0) -> list[bytes]:
        """Read whatever is already queued (used to skip stale reports)."""
        out = []
        for _ in range(max_reports):
            report = self.read(timeout_ms=timeout_ms)
            if report is None:
                break
            out.append(report)
        return out


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def find_devices(vid: int | None = None, pid: int | None = None) -> list[dict]:
    result = []
    for info in enumerate_hid():
        if vid is not None and info.get("vid") != vid:
            continue
        if pid is not None and info.get("pid") != pid:
            continue
        result.append(info)
    return result


def describe(info: dict) -> str:
    vid = info.get("vid")
    pid = info.get("pid")
    vid_s = f"{vid:04X}" if vid is not None else "????"
    pid_s = f"{pid:04X}" if pid is not None else "????"
    return (
        f"VID_{vid_s}&PID_{pid_s} "
        f"usage=0x{info.get('usage_page', 0):04X}:0x{info.get('usage', 0):04X} "
        f"in={info.get('input_len')} out={info.get('output_len')} feat={info.get('feature_len')} "
        f"{info.get('product', '')!r} {info.get('manufacturer', '')!r}"
    )
