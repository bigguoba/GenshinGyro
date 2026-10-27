"""Inspect a physical DualShock 4: interfaces, framing, rate and layout.

Bluetooth and wired DualShock 4s frame their reports differently in practice, so
this dumps what the pad really sends instead of trusting a constant:

    python tools\\dump_ds4.py                 # summary + chosen interface
    python tools\\dump_ds4.py --raw 4         # first 4 reports as hex
    python tools\\dump_ds4.py --analyze 3     # 3 s of per-byte stats + offset scores
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dualshock4  # noqa: E402
import hidwin  # noqa: E402


def list_candidates() -> None:
    print("candidate HID interfaces:")
    for info in hidwin.enumerate_hid():
        if info.get("vid") != dualshock4.DS4_VID or info.get("pid") not in dualshock4.DS4_PIDS:
            continue
        kind = "EMULATED" if hidwin.is_emulated(info) else hidwin.transport_of(info)
        print(f"  {info['vid']:04X}:{info['pid']:04X} in={info.get('input_len')} "
              f"out={info.get('output_len')} {kind:9s} {info.get('instance_id')}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=int, default=0, help="dump N reports as hex")
    parser.add_argument("--analyze", type=float, default=0.0,
                        help="collect N seconds of per-byte statistics")
    parser.add_argument("--path", help="use a specific interface path")
    args = parser.parse_args()

    list_candidates()

    pad = dualshock4.DualShock4(path=args.path, log=print)
    pad.open()
    with pad:
        print(f"\n{pad.describe()}")
        print(f"transport: {pad.transport}, input_len={pad._info.get('input_len')}, "
              f"default state offset 0x01->{dualshock4.state_offset_for(0x01)} "
              f"0x11->{dualshock4.state_offset_for(0x11)}")

        pad.drain(max_reports=32)

        if args.raw:
            print(f"\nfirst {args.raw} reports:")
            for index in range(args.raw):
                report = pad._read_raw(500)
                if not report:
                    print(f"  [{index}] timeout")
                    continue
                print(f"  [{index}] id=0x{report[0]:02X} len={len(report)}  "
                      f"{report[:40].hex(' ')}")

        if args.analyze > 0:
            print(f"\ncollecting {args.analyze:g}s ...")
            reports = []
            started = time.monotonic()
            while time.monotonic() - started < args.analyze:
                report = pad._read_raw(300)
                if report:
                    reports.append(report)
            duration = time.monotonic() - started
            print(f"  {len(reports)} reports in {duration:.2f}s "
                  f"-> {len(reports) / duration:.1f} reports/s")
            if not reports:
                return 1

            ids: dict[int, int] = {}
            for report in reports:
                ids[report[0]] = ids.get(report[0], 0) + 1
            print(f"  report ids: " + ", ".join(f"0x{k:02X} x{v}" for k, v in sorted(ids.items())))

            length = min(len(report) for report in reports)
            print(f"  shortest report: {length} bytes")
            print("\n  per-byte over the first 40 bytes "
                  "(value range / how often it changes):")
            print("    byte  min  max  changes  meaning-guess")
            for index in range(min(40, length)):
                values = [report[index] for report in reports]
                changes = sum(1 for a, b in zip(values, values[1:]) if a != b)
                marker = ""
                if index in (0,):
                    marker = "report id"
                elif index in (9, 10) and changes:
                    marker = "timestamp candidate"
                elif 12 <= index < 24 and changes:
                    marker = "gyro/accel candidate"
                print(f"    {index:4d} {min(values):4d} {max(values):4d} "
                      f"{changes:8d}  {marker}")

            offsets, scores = dualshock4.detect_offsets(reports)
            print(f"\n  offset scores (higher = more plausible): {scores}")
            print(f"  chosen offsets: " + ", ".join(f"0x{k:02X}->{v}"
                                                  for k, v in sorted(offsets.items())))

            # Every byte of the report, not just the head: the state can sit
            # anywhere in the oversized Bluetooth buffer.
            changing = []
            for index in range(length):
                values = [report[index] for report in reports]
                if len(set(values)) > 1:
                    changing.append(index)
            ranges = []
            for index in changing:
                if ranges and index == ranges[-1][1] + 1:
                    ranges[-1][1] = index
                else:
                    ranges.append([index, index])
            print(f"\n  bytes that change anywhere in the {length}-byte report: "
                  + (", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in ranges)
                     or "none"))

            print("\n  timestamp behaviour per candidate offset:")
            for offset in (1, 2, 3):
                deltas = []
                previous = None
                for report in reports:
                    if report[0] not in (0x01, 0x11) or len(report) < offset + 11:
                        continue
                    stamp = struct.unpack_from("<H", report, offset + 9)[0]
                    if previous is not None:
                        deltas.append((stamp - previous) % 65536)
                    previous = stamp
                if deltas:
                    frozen = sum(1 for delta in deltas if delta == 0)
                    print(f"    offset {offset}: median delta "
                          f"{sorted(deltas)[len(deltas) // 2]}, "
                          f"{frozen}/{len(deltas)} unchanged, "
                          f"min {min(deltas)}, max {max(deltas)}")

            print("\n  acceleration magnitude per candidate offset (should be ~1 g):")
            for offset in (1, 2, 3):
                magnitudes = []
                for report in reports:
                    if report[0] not in (0x01, 0x11) or len(report) < offset + 24:
                        continue
                    accel = struct.unpack_from("<3h", report, offset + 18)
                    magnitudes.append(sum(value * value for value in accel) ** 0.5)
                if magnitudes:
                    magnitudes.sort()
                    print(f"    offset {offset}: median |a| = "
                          f"{magnitudes[len(magnitudes) // 2]:.0f} "
                          f"(min {magnitudes[0]:.0f}, max {magnitudes[-1]:.0f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
