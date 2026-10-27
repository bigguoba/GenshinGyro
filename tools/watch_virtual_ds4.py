"""Watch what a virtual DualShock 4 is actually receiving.

Reads the emulated pad (the ViGEm target) and decodes its payload, so a relay can
be verified end to end: if the physical pad moves, these numbers must move.

    python tools\\watch_virtual_ds4.py --seconds 6
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dualshock4  # noqa: E402
import hidwin  # noqa: E402


def find_virtual() -> dict | None:
    for info in hidwin.enumerate_hid():
        if info.get("vid") == 0x054C and hidwin.is_emulated(info) \
                and (info.get("input_len") or 0) == 64:
            return info
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=6.0)
    args = parser.parse_args()

    info = find_virtual()
    if info is None:
        print("no virtual DualShock 4 present (is the relay running?)")
        return 1
    print(f"virtual pad: {hidwin.describe(info)}")

    with hidwin.HidDevice(info["path"], info).open() as device:
        device.drain(max_reports=32)
        started = time.monotonic()
        printed = 0
        stamps: set[int] = set()
        gyro_values: set[tuple] = set()
        accel_values: set[tuple] = set()
        while time.monotonic() - started < args.seconds:
            report = device.read(timeout_ms=300)
            if not report or report[0] != 0x01:
                continue
            payload = report[1:]
            state = dualshock4.parse_report(report, offset=1)
            stamps.add(state.timestamp)
            gyro_values.add(state.gyro)
            accel_values.add(state.accel)
            if printed < 8 and time.monotonic() - started > printed * 0.6:
                printed += 1
                touchpad = "按下" if state.special & 0x02 else "—"
                print(f"  t={time.monotonic() - started:5.2f}s  "
                      f"sticks={state.sticks}  buttons=0x{state.buttons:04X}  "
                      f"special=0x{state.special:02X}（触控板{touchpad}）  "
                      f"timestamp={state.timestamp:5d}  "
                      f"gyro=({state.gyro[0]:6d},{state.gyro[1]:6d},{state.gyro[2]:6d}) counts "
                      f"=({state.gyro[0] / 16:6.1f},{state.gyro[1] / 16:6.1f},"
                      f"{state.gyro[2] / 16:6.1f}) dps  "
                      f"accel=({state.accel[0] / 8192:5.2f},{state.accel[1] / 8192:5.2f},"
                      f"{state.accel[2] / 8192:5.2f}) g")

        print(f"\n  distinct timestamps: {len(stamps)}   "
              f"distinct gyro triples: {len(gyro_values)}   "
              f"distinct accel triples: {len(accel_values)}")
        verdict = ("LIVE: the virtual pad is carrying changing motion data"
                   if len(gyro_values) > 3 and len(stamps) > 3
                   else "STATIC: the virtual pad is not receiving live motion")
        print(f"  -> {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
