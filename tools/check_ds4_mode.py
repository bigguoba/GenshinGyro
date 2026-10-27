"""Find out which input report mode a live DualShock 4 is using.

A DualShock 4 does not always send the same thing: over Bluetooth it can answer
with the "simple" report id 0x01 (sticks and buttons only, no motion, frozen
timestamp) or with the extended report id 0x11 that carries gyro/accelerometer.
Games that need motion need the extended one -- and a pad stuck in simple mode is
exactly what "Bluetooth gyro is not recognised" looks like.

This samples the pad, optionally pokes it the way SDL/DS4Windows do (read feature
report 0x02, send output report 0x05) and reports whether the mode changed.

    python tools\\check_ds4_mode.py
    python tools\\check_ds4_mode.py --poke
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dualshock4  # noqa: E402


def sample(pad: dualshock4.DualShock4, seconds: float) -> dict:
    """Collect reports and describe what is actually moving."""
    reports = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        report = pad._read_raw(300)
        if report:
            reports.append(report)
    if not reports:
        return {"reports": 0}

    ids: dict[int, int] = {}
    for report in reports:
        ids[report[0]] = ids.get(report[0], 0) + 1
    length = min(len(report) for report in reports)
    changing = [index for index in range(length)
                if len({report[index] for report in reports}) > 1]

    def moved(offset: int, size: int) -> bool:
        for report in reports:
            if len(report) < offset + size:
                continue
            first = struct.unpack_from(f"<{size // 2}h", report, offset)
            break
        else:
            return False
        return any(struct.unpack_from(f"<{size // 2}h", report, offset) != first
                   for report in reports)

    stamps = [struct.unpack_from("<H", report, 10)[0] for report in reports
              if len(report) >= 12]
    return {
        "reports": len(reports),
        "rate": len(reports) / seconds,
        "ids": ids,
        "length": length,
        "changing": changing,
        "gyro_moves": moved(13, 6),
        "accel_moves": moved(19, 6),
        "timestamp_moves": len(set(stamps)) > 1 if stamps else False,
        "battery": reports[-1][30] if len(reports) > 30 else None,
    }


def describe(label: str, info: dict) -> None:
    if not info.get("reports"):
        print(f"  {label}: no reports")
        return
    ids = ", ".join(f"0x{k:02X} x{v}" for k, v in sorted(info["ids"].items()))
    print(f"  {label}: {info['reports']} reports (~{info['rate']:.0f}/s), ids: {ids}")
    print(f"    长度 {info['length']} 字节；发生变化的字节: "
          + (", ".join(map(str, info["changing"][:24]))
             + (" ..." if len(info["changing"]) > 24 else "") if info["changing"] else "无"))
    print(f"    时间戳递增: {info['timestamp_moves']}   "
          f"陀螺仪有变化: {info['gyro_moves']}   加速度计有变化: {info['accel_moves']}   "
          f"电量字节: {info['battery']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poke", action="store_true",
                        help="read feature 0x02 and send output report 0x05 (what SDL does)")
    parser.add_argument("--seconds", type=float, default=2.0)
    args = parser.parse_args()

    pad = dualshock4.DualShock4(log=print)
    pad.open()
    with pad:
        print(f"{pad.describe()}\n")
        pad.drain(max_reports=32)
        print("before:")
        describe("sample", sample(pad, args.seconds))

        if args.poke:
            print("\npoking the pad the way SDL / DS4Windows do:")
            for report_id in (0x02, 0x05, 0xA3):
                try:
                    data = pad._device.get_feature(report_id, 64)
                    print(f"  feature 0x{report_id:02X}: " +
                          (f"{len(data)} bytes {data[:12].hex(' ')}" if data else "no data"))
                except OSError as exc:
                    print(f"  feature 0x{report_id:02X}: error {exc}")
            # output report 0x05: report id, flags, rumble, LED, flash, then padding
            try:
                pad._device.write(bytes((0x05, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00,
                                         0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                                         0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                                         0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                                         0x00, 0x00, 0x00, 0x00)))
                print("  output report 0x05 sent")
            except OSError as exc:
                print(f"  output report 0x05: error {exc}")
            time.sleep(0.4)
            print("\nafter:")
            describe("sample", sample(pad, args.seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
