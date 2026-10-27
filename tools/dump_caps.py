"""List the input value fields a HID device declares (as a game reads them).

    python tools\\dump_caps.py --vid 054C --pid 09CC     # a real DualShock 4
    python tools\\dump_caps.py --virtual                 # a ViGEm DualShock 4
    python tools\\dump_caps.py --virtual --ds4-only      # only sensor-ish fields
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hidwin  # noqa: E402

PAGE_NAMES = {0x01: "GenericDesktop", 0x02: "Simulation", 0x06: "GenericDevice",
              0x09: "Button", 0x0C: "Consumer", 0xFF00: "VendorDefined"}
USAGE_NAMES = {0x30: "X", 0x31: "Y", 0x32: "Z", 0x33: "Rx", 0x34: "Ry", 0x35: "Rz",
               0x36: "Slider", 0x37: "Dial", 0x38: "Wheel", 0x39: "HatSwitch",
               0x20: "Vx", 0x21: "Vy", 0x22: "Vz", 0x23: "Vbrx", 0x24: "Vbry",
               0x25: "Vbrz", 0x40: "Pointer", 0x42: "Joystick", 0x43: "GamePad"}


def describe(info: dict, field: dict) -> str:
    page = PAGE_NAMES.get(field["usage_page"], f"0x{field['usage_page']:04X}")
    if field["usage_page"] == 0x01:
        usage = USAGE_NAMES.get(field["usage"], f"0x{field['usage']:02X}")
    elif field["usage_page"] == 0x09:
        usage = f"Button{field['usage']}"
    else:
        usage = f"0x{field['usage']:02X}"
    if field["is_range"] and field["usage_max"] != field["usage"]:
        usage += f"..0x{field['usage_max']:02X}"
    return (f"  id={field['report_id']} {field['bit_size']:2d}b x{field['report_count']:<2d} "
            f"{page}:{usage:<12s} logical={field['logical_min']}..{field['logical_max']} "
            f"physical={field['physical_min']}..{field['physical_max']} "
            f"units={field['units']} exp={field['units_exp']}")


def show_for(info: dict) -> None:
    print(f"\n=== {hidwin.describe(info)} ===")
    print(f"  instance: {info.get('instance_id')}")
    print(f"  kind    : {'EMULATED (virtual bus)' if hidwin.is_emulated(info) else 'physical ' + hidwin.transport_of(info)}")
    try:
        _info, fields = hidwin.input_value_caps(info["path"])
    except OSError as exc:
        print(f"  cannot read caps: {exc}")
        return
    print(f"  input value fields: {len(fields)}")
    for field in fields:
        print(describe(info, field))
    sixteen = [f for f in fields if f["bit_size"] == 16]
    print(f"  16-bit fields (candidate gyro/accel): {len(sixteen)}")
    for field in sixteen:
        print(f"    - {describe({'': ''}, field).strip()}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vid", type=lambda v: int(v, 16), default=0x054C)
    parser.add_argument("--pid", type=lambda v: int(v, 16))
    parser.add_argument("--virtual", action="store_true")
    parser.add_argument("--diff", action="store_true",
                        help="show the physical pad and a ViGEm pad together")
    args = parser.parse_args()

    if args.diff:
        import time

        import vigem

        for info in hidwin.enumerate_hid():
            if info.get("vid") == 0x054C and not hidwin.is_emulated(info):
                show_for(info)
                break
        else:
            print("no physical DualShock 4 present")

        with vigem.ViGEmClient() as client:
            before = {i["path"] for i in hidwin.enumerate_hid()}
            pad = client.create_ds4()
            pad.plug_in()
            try:
                time.sleep(1.5)
                for info in hidwin.enumerate_hid():
                    if info["path"] not in before and info.get("vid") == 0x054C:
                        show_for(info)
            finally:
                pad.unplug()
        return 0

    targets = []
    if args.virtual:
        import time

        import vigem

        with vigem.ViGEmClient() as client:
            before = {i["path"] for i in hidwin.enumerate_hid()}
            pad = client.create_ds4()
            pad.plug_in()
            try:
                time.sleep(1.5)
                for info in hidwin.enumerate_hid():
                    if info["path"] not in before and info.get("vid") == 0x054C:
                        targets.append(info)
                if not targets:
                    print("virtual DS4 not found")
                    return 1
                for info in targets:
                    show_for(info)
                return 0
            finally:
                pad.unplug()

    for info in hidwin.enumerate_hid():
        if info.get("vid") != args.vid:
            continue
        if args.pid is not None and info.get("pid") != args.pid:
            continue
        targets.append(info)
    if not targets:
        print("no matching device present "
              "(a real DS4 must be plugged in to read its capabilities)")
        return 1
    for info in targets:
        show_for(info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
