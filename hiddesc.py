"""Minimal HID report descriptor parser.

Enough of the HID spec to read what a device declares: for every Input, Output
and Feature item it reports the report id, size/count, usages, logical and
physical range and unit.  That is what a game uses to find sensor fields, so it
is the right level of detail when comparing a real device with an emulated one.

Reference: HID 1.11 specification, section 6.2.2 (item format).
"""

from __future__ import annotations

from dataclasses import dataclass, field

ITEM_TYPES = {0: "Main", 1: "Global", 2: "Local", 3: "Reserved"}

MAIN_TAGS = {0x8: "Input", 0x9: "Output", 0xB: "Feature",
             0xA: "Collection", 0xC: "EndCollection"}

GLOBAL_TAGS = {0x0: "UsagePage", 0x1: "LogicalMin", 0x2: "LogicalMax",
               0x3: "PhysicalMin", 0x4: "PhysicalMax", 0x5: "UnitExp",
               0x6: "Unit", 0x7: "ReportSize", 0x8: "ReportID",
               0x9: "ReportCount", 0xA: "Push", 0xB: "Pop"}

LOCAL_TAGS = {0x0: "Usage", 0x1: "UsageMin", 0x2: "UsageMax",
              0x3: "DesignatorIndex", 0x4: "DesignatorMin", 0x5: "DesignatorMax",
              0x6: "StringIndex", 0x7: "StringMin", 0x8: "StringMax",
              0x9: "Delimiter"}

#: Human readable names for the usages that matter for gamepad sensors.
USAGE_PAGE_NAMES = {
    0x01: "GenericDesktop",
    0x02: "Simulation",
    0x06: "GenericDevice",
    0x09: "Button",
    0x0C: "Consumer",
    0xFF00: "VendorDefined",
    0xFFFF: "VendorDefined16",
}

GENERIC_DESKTOP_USAGES = {
    0x30: "X", 0x31: "Y", 0x32: "Z", 0x33: "Rx", 0x34: "Ry", 0x35: "Rz",
    0x36: "Slider", 0x37: "Dial", 0x38: "Wheel", 0x39: "HatSwitch",
    0x20: "Vx", 0x21: "Vy", 0x22: "Vz", 0x23: "Vbrx", 0x24: "Vbry",
    0x25: "Vbrz", 0x26: "Vno", 0x27: "FeatureNotification",
    0x40: "Pointer", 0x41: "Mouse", 0x42: "Joystick", 0x43: "GamePad",
    0x44: "Keyboard", 0x80: "SystemControl",
}

#: HID unit nibbles for the unit item (bits 0-3 = length/angle/mass/..., but for
#: our purposes only the "unit system" and the SI codes matter).
UNIT_NAMES = {
    0x00: "none",
    0x01: "cm", 0x02: "radians", 0x03: "degrees",
    0x04: "grams", 0x05: "seconds", 0x06: "kelvin",
    0x10: "SI Linear (cm)", 0x11: "SI Rotation (rad)", 0x12: "SI Rotation (deg)",
    0x13: "SI Linear (in)", 0x14: "English Rotation (deg)",
}


@dataclass
class Field:
    """One Input/Output/Feature item with the state that applied to it."""

    kind: str
    offset: int
    report_id: int
    report_size: int
    report_count: int
    usage_page: int
    usages: list[int] = field(default_factory=list)
    usage_min: int | None = None
    usage_max: int | None = None
    logical_min: int | None = None
    logical_max: int | None = None
    physical_min: int | None = None
    physical_max: int | None = None
    unit: int | None = None
    unit_exp: int | None = None
    flags: int = 0

    @property
    def bits(self) -> int:
        return self.report_size * self.report_count

    def usage_text(self) -> str:
        page = USAGE_PAGE_NAMES.get(self.usage_page, f"0x{self.usage_page:04X}")
        names = []
        for usage in self.usages:
            if self.usage_page == 0x01:
                names.append(GENERIC_DESKTOP_USAGES.get(usage, f"0x{usage:02X}"))
            elif self.usage_page == 0x09:
                names.append(f"Button{usage}")
            else:
                names.append(f"0x{usage:02X}")
        if self.usage_min is not None and self.usage_max is not None:
            names.append(f"range 0x{self.usage_min:02X}-0x{self.usage_max:02X}")
        body = ",".join(names) if names else "-"
        return f"{page}:{body}"

    def describe(self) -> str:
        parts = [f"id={self.report_id} {self.report_size}b x{self.report_count}"
                 f" ({self.bits}b) {self.usage_text()}"]
        if self.logical_min is not None:
            parts.append(f"logical={self.logical_min}..{self.logical_max}")
        if self.physical_min is not None:
            parts.append(f"physical={self.physical_min}..{self.physical_max}")
        if self.unit:
            unit_name = UNIT_NAMES.get(self.unit, f"0x{self.unit:02X}")
            exponent = self.unit_exp or 0
            parts.append(f"unit={unit_name}^{exponent}" if exponent else f"unit={unit_name}")
        if self.flags:
            marks = []
            if self.flags & 0x01:
                marks.append("const")
            if self.flags & 0x02:
                marks.append("var")
            if self.flags & 0x04:
                marks.append("rel")
            else:
                marks.append("abs")
            parts.append("/".join(marks))
        return "  ".join(parts)


def _signed(value: int, size: int) -> int:
    """Interpret a little-endian item payload as a signed integer."""
    bits = size * 8
    if bits and value & (1 << (bits - 1)):
        value -= 1 << bits
    return value


def parse(descriptor: bytes) -> list[Field]:
    """Return the Input/Output/Feature fields declared by a descriptor."""
    fields: list[Field] = []
    index = 0
    usage_page = 0
    report_id = 0
    report_size = 0
    report_count = 0
    logical_min = logical_max = None
    physical_min = physical_max = None
    unit = unit_exp = None
    usages: list[int] = []
    usage_min = usage_max = None

    while index < len(descriptor):
        prefix = descriptor[index]
        index += 1
        if prefix == 0xFE:                    # long item
            size = descriptor[index]
            index += 2 + size
            continue
        size = prefix & 0x03
        if size == 3:
            size = 4
        item_type = (prefix >> 2) & 0x03
        tag = prefix >> 4
        payload = int.from_bytes(descriptor[index:index + size], "little")
        if size:
            index += size

        if item_type == 1:                     # Global
            name = GLOBAL_TAGS.get(tag)
            if name == "UsagePage":
                usage_page = payload
            elif name == "ReportID":
                report_id = payload
            elif name == "ReportSize":
                report_size = payload
            elif name == "ReportCount":
                report_count = payload
            elif name == "LogicalMin":
                logical_min = _signed(payload, size)
            elif name == "LogicalMax":
                logical_max = _signed(payload, size)
            elif name == "PhysicalMin":
                physical_min = _signed(payload, size)
            elif name == "PhysicalMax":
                physical_max = _signed(payload, size)
            elif name == "Unit":
                unit = payload
            elif name == "UnitExp":
                unit_exp = _signed(payload, size)
        elif item_type == 2:                   # Local
            name = LOCAL_TAGS.get(tag)
            if name == "Usage":
                usages.append(payload)
            elif name == "UsageMin":
                usage_min = payload
            elif name == "UsageMax":
                usage_max = payload
        elif item_type == 0:                   # Main
            name = MAIN_TAGS.get(tag)
            if name in ("Input", "Output", "Feature"):
                fields.append(Field(
                    kind=name, offset=index, report_id=report_id,
                    report_size=report_size, report_count=report_count,
                    usage_page=usage_page, usages=list(usages),
                    usage_min=usage_min, usage_max=usage_max,
                    logical_min=logical_min, logical_max=logical_max,
                    physical_min=physical_min, physical_max=physical_max,
                    unit=unit, unit_exp=unit_exp, flags=payload,
                ))
            usages = []
            usage_min = usage_max = None
    return fields


def input_layout(descriptor: bytes) -> list[tuple[int, int, str]]:
    """Byte layout of each input report: (report_id, byte_offset, field text)."""
    layout: list[tuple[int, int, str]] = []
    offsets: dict[int, int] = {}
    for item in parse(descriptor):
        if item.kind != "Input":
            continue
        bit_offset = offsets.get(item.report_id, 0)
        layout.append((item.report_id, bit_offset // 8, item.describe()))
        offsets[item.report_id] = bit_offset + item.bits
    return layout


def summarise(descriptor: bytes, interesting_only: bool = False) -> str:
    lines = []
    for item in parse(descriptor):
        if item.kind not in ("Input", "Output", "Feature"):
            continue
        if interesting_only and item.flags & 0x01:      # skip constants/padding
            continue
        lines.append(f"  {item.kind:8s} {item.describe()}")
    return "\n".join(lines)
