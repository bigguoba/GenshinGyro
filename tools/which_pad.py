"""Identify Sony/Nintendo HID devices, and say whether each is really live.

Useful when a pad "is connected" but nothing works:

* a ViGEm virtual DualShock 4 shares the real product id, so it is told apart by
  its device tree (virtual bus vs USB/Bluetooth);
* a paired-but-off Bluetooth pad still enumerates, and reads then return a
  frozen cached report -- this checks whether the bytes actually change.

    python tools\\which_pad.py
    python tools\\which_pad.py --read 2      # also sample 2 s of reports
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dualshock4  # noqa: E402
import hidwin  # noqa: E402

VENDORS = {0x054C: "Sony", 0x057E: "Nintendo"}


def classify(info: dict) -> str:
    if hidwin.is_emulated(info):
        return "EMULATED (virtual bus) - e.g. a ViGEm target"
    transport = hidwin.transport_of(info)
    if transport != "unknown":
        return f"physical ({transport})"
    enumerator = (info.get("enumerator") or "").upper()
    return f"unknown transport (enumerator={enumerator or '?'})"


def describe_status(info: dict) -> str:
    status = hidwin.device_status(info.get("instance_id") or "")
    if not status.get("found"):
        return "device node not found"
    if status.get("status") is None:
        return "status unavailable"
    bits = []
    if status.get("disconnected"):
        bits.append("DISCONNECTED")
    elif status.get("started"):
        bits.append("started/connected")
    else:
        bits.append("not started")
    if status.get("problem"):
        bits.append(f"problem code {status['problem']}")
    return ", ".join(bits)


def sample(info: dict, seconds: float) -> None:
    try:
        device = hidwin.HidDevice(info["path"], info).open()
    except OSError as exc:
        print(f"    data    : cannot open ({exc})")
        return
    with device:
        device.drain(max_reports=32)
        first = None
        changes = 0
        reports = 0
        started = time.monotonic()
        previous = None
        while time.monotonic() - started < seconds:
            report = device.read(timeout_ms=300)
            if not report:
                continue
            reports += 1
            if first is None:
                first = report
            if previous is not None and report != previous:
                changes += 1
            previous = report
        elapsed = time.monotonic() - started
        if not reports:
            print("    data    : nothing received -> pad is off, asleep or hidden")
            return
        verdict = ("live (bytes change)" if changes
                   else "STATIC - same report repeated: pad is probably off/asleep, "
                        "or another program is holding it")
        print(f"    data    : {reports} reports in {elapsed:.1f}s "
              f"(~{reports / elapsed:.0f}/s), {changes} changes -> {verdict}")
        if first:
            print(f"    first   : id=0x{first[0]:02X} len={len(first)} "
                  f"{first[:32].hex(' ')}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--read", type=float, default=0.0,
                        help="sample this many seconds of reports per device")
    args = parser.parse_args()

    pads = [i for i in hidwin.enumerate_hid() if i.get("vid") in VENDORS]
    if not pads:
        print("no Sony/Nintendo HID device present")
        return 1
    for info in pads:
        print(f"{info['vid']:04X}:{info['pid']:04X}  in={info.get('input_len')} "
              f"out={info.get('output_len')}  {info.get('product','')!r}")
        print(f"    {classify(info)}")
        print(f"    status  : {describe_status(info)}")
        print(f"    instance: {info.get('instance_id')}")
        if args.read > 0:
            sample(info, args.read)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
