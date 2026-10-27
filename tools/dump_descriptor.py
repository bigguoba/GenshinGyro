"""Compare the HID report descriptors of the real and virtual DualShock 4.

A game finds the gyroscope/accelerometer fields by reading the report
descriptor, so a difference between the emulated device and real hardware is a
prime suspect when one axis is ignored.

    python tools\\dump_descriptor.py --vid 054C --pid 09CC
    python tools\\dump_descriptor.py --virtual          # plug in a ViGEm DS4, dump it
    python tools\\dump_descriptor.py --diff             # real DS4 vs virtual DS4
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hiddesc  # noqa: E402
import hidwin  # noqa: E402

DS4_VID = 0x054C
REAL_PID = 0x09CC       # DS4 v2 (what is plugged in here)
VIRTUAL_PID = 0x05C4    # what ViGEmBus creates


def descriptors_for(vid: int, pid: int) -> list[tuple[dict, bytes]]:
    """Descriptors for present devices, falling back to the registry cache."""
    result = []
    for info in hidwin.enumerate_hid():
        if info.get("vid") != vid or info.get("pid") != pid:
            continue
        try:
            descriptor = hidwin.report_descriptor_for_path(info["path"])
        except OSError:
            continue
        result.append((info, descriptor))
    if not result:
        for key_path, blob in hidwin.report_descriptors_from_registry(vid, pid):
            result.append(({"path": f"(registry) {key_path}", "vid": vid, "pid": pid,
                            "input_len": None, "output_len": None,
                            "product": "(not currently connected)"}, blob))
    return result


def show(title: str, info: dict, descriptor: bytes) -> None:
    print(f"\n=== {title} ===")
    print(f"device : {hidwin.describe(info)}")
    print(f"descriptor: {len(descriptor)} bytes")
    print(f"hex    : {descriptor.hex(' ')}")
    fields = hiddesc.parse(descriptor)
    inputs = [f for f in fields if f.kind == "Input"]
    print(f"input fields: {len(inputs)}")
    for _report_id, byte_offset, text in hiddesc.input_layout(descriptor):
        print(f"    @{byte_offset:3d}  {text}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vid", type=lambda v: int(v, 16), default=DS4_VID)
    parser.add_argument("--pid", type=lambda v: int(v, 16))
    parser.add_argument("--virtual", action="store_true",
                        help="create a ViGEm DS4, dump it, then remove it")
    parser.add_argument("--diff", action="store_true",
                        help="dump the real DS4 and a ViGEm DS4 side by side")
    parser.add_argument("--raw", action="store_true", help="also print the raw bytes")
    args = parser.parse_args()

    if args.virtual or args.diff:
        import vigem

        with vigem.ViGEmClient() as client:
            before = {i["path"] for i in hidwin.enumerate_hid()}
            pad = client.create_ds4()
            pad.plug_in()
            try:
                import time
                time.sleep(1.5)
                virtual = [(i, hiddesc.parse(b), b) for i, b in descriptors_for(DS4_VID, VIRTUAL_PID)
                           if i["path"] not in before]
                if not virtual:
                    print("no virtual DS4 found")
                    return 1

                real = descriptors_for(DS4_VID, REAL_PID)
                if real:
                    show("real DualShock 4", real[0][0], real[0][1])
                else:
                    print("\n(no physical DS4 found)")

                for info, _fields, descriptor in virtual:
                    show("virtual DualShock 4 (ViGEmBus)", info, descriptor)

                if real and virtual:
                    real_desc = real[0][1]
                    virt_desc = virtual[0][2]
                    print("\n=== differences ===")
                    if real_desc == virt_desc:
                        print("descriptors are byte-identical")
                    else:
                        print(f"length: real={len(real_desc)} virtual={len(virt_desc)}")
                        real_set = {hiddesc.parse(real_desc)[i].describe()
                                    for i in range(len(hiddesc.parse(real_desc)))}
                        virt_set = {hiddesc.parse(virt_desc)[i].describe()
                                    for i in range(len(hiddesc.parse(virt_desc)))}
                        print("only in the real device:")
                        for line in sorted(real_set - virt_set):
                            print(f"  + {line}")
                        print("only in the virtual device:")
                        for line in sorted(virt_set - real_set):
                            print(f"  - {line}")
                return 0
            finally:
                pad.unplug()

    pid = args.pid if args.pid is not None else REAL_PID
    found = descriptors_for(args.vid, pid)
    if not found:
        print(f"no device with VID {args.vid:04X} PID {pid:04X}")
        return 1
    for info, descriptor in found:
        show(f"VID {args.vid:04X} PID {pid:04X}", info, descriptor)
        if not args.raw:
            continue
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
