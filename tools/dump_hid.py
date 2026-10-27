"""Dump and analyse ANY HID game controller, to work out its report layout.

Written for adding support for a new pad (e.g. a Switch 2 Pro Controller or a
third-party clone): it says what the device actually sends instead of trusting a
constant.  Everything is device-agnostic -- the report id, the button bytes, the
stick bytes and the IMU offsets are all *discovered* from the data.

    python tools\\dump_hid.py --list                      # what controllers exist
    python tools\\dump_hid.py --vid 057E --pid 2069        # summary + descriptor
    python tools\\dump_hid.py --match "Pro Controller" --analyze 5

The analysis prints, for every candidate layout:
  * per-byte variability (a frozen IMU/timestamp byte is the tell-tale sign of a
    "minimal report" mode where motion simply is not transmitted);
  * candidate accelerometer triples: three consecutive int16 whose magnitude is
    *stable* while the pad is held still (gravity is 1 g no matter how you turn);
  * candidate gyro triples: three consecutive int16 whose magnitude is near zero
    while still and grows when you rotate the pad;
  * candidate timestamps/counters: 16- or 32-bit fields that only increase.
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hidwin  # noqa: E402
import hiddesc  # noqa: E402

GAMEPAD_USAGE_PAGE = 0x01
GAMEPAD_USAGES = {0x04, 0x05}          # joystick, gamepad


def controller_like(info: dict) -> bool:
    """Cheap filter: gamepad/joystick usage, or a name that looks like a pad."""
    usage_page = info.get("usage_page") or 0
    usage = info.get("usage") or 0
    if usage_page == GAMEPAD_USAGE_PAGE and usage in GAMEPAD_USAGES:
        return True
    name = f"{info.get('manufacturer', '')} {info.get('product', '')}".lower()
    return any(word in name for word in ("controller", "gamepad", "joy-con", "joycon",
                                         "pro controller", "dualshock", "dualsense"))


def list_devices(show_all: bool) -> list[dict]:
    rows = []
    for info in hidwin.enumerate_hid():
        if info.get("input_len", 0) <= 0:
            continue                                    # output-only interface
        if not show_all and not controller_like(info):
            continue
        rows.append(info)
        kind = "EMULATED" if hidwin.is_emulated(info) else hidwin.transport_of(info)
        print(f"  {info.get('vid', 0):04X}:{info.get('pid', 0):04X} "
              f"in={info.get('input_len'):<4} out={info.get('output_len'):<4} "
              f"usage=0x{info.get('usage_page', 0):04X}:{info.get('usage', 0):04X} "
              f"{kind:9s} {info.get('manufacturer', '')!r} {info.get('product', '')!r}")
    return rows


def pick(rows: list[dict], vid: int | None, pid: int | None,
         needle: str | None) -> dict | None:
    for info in rows:
        if vid is not None and info.get("vid") != vid:
            continue
        if pid is not None and info.get("pid") != pid:
            continue
        if needle:
            text = f"{info.get('manufacturer', '')} {info.get('product', '')}".lower()
            if needle.lower() not in text:
                continue
        return info
    return None


def describe(info: dict) -> None:
    print("\ndescriptor:")
    raw = hidwin.report_descriptor_for_path(info.get("path", ""))
    if not raw:
        print("  descriptor unavailable")
        return
    print(hiddesc.summarise(raw))
    layout = hiddesc.input_layout(raw)
    print("  input reports declared in the descriptor:")
    for report_id, bits, usage in layout:
        print(f"    id=0x{report_id:02X} {bits // 8} bytes  {usage}")


def _magnitudes(reports: list[bytes], offset: int, end: int | None = None) -> list[float]:
    values = []
    for report in (reports if end is None else reports[:end]):
        if len(report) < offset + 6:
            continue
        triple = struct.unpack_from("<3h", report, offset)
        values.append(math.sqrt(sum(value * value for value in triple)))
    return values


def accel_candidates(reports: list[bytes], length: int) -> list[tuple]:
    """Stable-magnitude int16 triples: gravity does not care how you hold it."""
    scored = []
    for offset in range(1, max(1, length - 5)):
        magnitudes = sorted(_magnitudes(reports, offset))
        if len(magnitudes) < 10:
            continue
        low, median, high = magnitudes[0], magnitudes[len(magnitudes) // 2], magnitudes[-1]
        if median <= 100:
            continue
        spread = (high - low) / median if median else 1.0
        scored.append((spread, offset, low, median, high))
    scored.sort()
    return scored


def gyro_candidates(reports: list[bytes], length: int) -> list[tuple]:
    """Quiet while still *and* strong when turned -- a counter is neither.

    A pure counter also looks "quiet" as an int16 triple (its bytes step by a
    constant), so counter-like windows are excluded explicitly; the synthetic
    self-test is what caught that.
    """
    scored = []
    for offset in range(1, max(1, length - 5)):
        # A triple covers five 16-bit windows; if any of them is a counter the
        # triple is not motion.
        if any(counterlike(reports, offset + shift) for shift in range(5)):
            continue
        magnitudes = _magnitudes(reports, offset, end=35)
        if len(magnitudes) < 10:
            continue
        ordered = sorted(magnitudes)
        median = ordered[len(ordered) // 2]
        peak = max(_magnitudes(reports, offset) or [0.0])
        if not (median < 200 and peak > max(median, 1.0) * 3):
            continue
        scored.append((median, -peak, offset, peak))
    scored.sort()
    return scored


def counterlike(reports: list[bytes], offset: int, width: int = 2) -> bool:
    """Is this offset a monotonically stepping counter (not motion)?"""
    fmt = "<H" if width == 2 else "<I"
    values = [struct.unpack_from(fmt, report, offset)[0]
              for report in reports if len(report) >= offset + width]
    if len(values) < 10:
        return False
    deltas = {(b - a) % (1 << (8 * width)) for a, b in zip(values, values[1:])}
    return len(deltas) == 1 and 0 not in deltas


def scan_accel(reports: list[bytes], length: int, top: int = 6) -> None:
    print("\n  accelerometer candidates (three consecutive int16, magnitude stable):")
    scored = accel_candidates(reports, length)
    if not scored:
        print("    none found")
    for spread, offset, low, median, high in scored[:top]:
        print(f"    offset {offset:3d}: median |a| = {median:8.0f} "
              f"(min {low:.0f}, max {high:.0f}, spread {spread * 100:.1f}%)"
              f"{'   <-- very plausible' if spread < 0.15 else ''}")


def scan_gyro(reports: list[bytes], length: int, top: int = 6) -> None:
    print("\n  gyro candidates (quiet while still, strong response when moved):")
    scored = gyro_candidates(reports, length)
    if not scored:
        print("    none found -- hold still, then rotate the pad during the capture")
    for median, _negative_peak, offset, peak in scored[:top]:
        print(f"    offset {offset:3d}: median |w| = {median:8.0f}, peak {peak:8.0f}"
              f"  (peak/median {peak / max(median, 1.0):.1f}x)"
              f"{'   <-- very plausible' if median < 100 and peak > 1000 else ''}")


def scan_counters(reports: list[bytes], length: int) -> None:
    print("\n  counter / timestamp candidates (only ever increases):")
    found = 0
    for offset in range(0, max(1, length - 3)):
        for width, fmt in ((2, "<H"), (4, "<I")):
            if offset + width > length:
                continue
            values = [struct.unpack_from(fmt, report, offset)[0]
                      for report in reports if len(report) >= offset + width]
            if len(values) < 10:
                continue
            deltas = [(b - a) % (1 << (8 * width)) for a, b in zip(values, values[1:])]
            if all(delta > 0 for delta in deltas) and len(set(deltas)) <= 3:
                deltas.sort()
                print(f"    offset {offset:3d} {8 * width}-bit: constant step "
                      f"{deltas[len(deltas) // 2]} (range {deltas[0]}..{deltas[-1]})")
                found += 1
    if not found:
        print("    none found (a frozen timestamp means the pad is in a reduced mode)")


def self_test() -> int:
    """Prove the scanners find a layout we already know, using synthetic reports.

    The synthesised pad puts the gyro at 10, the accelerometer at 16 (constant
    4096 = 1 g whatever the direction) and a 16-bit counter at 6 stepping by 4.
    """
    import random

    random.seed(7)
    reports = []
    counter = 0
    for step in range(120):
        body = bytearray(40)
        body[0] = 0x30                                    # report id
        counter = (counter + 4) % 65536
        struct.pack_into("<H", body, 6, counter)          # counter
        angle = step * 0.05
        # gyro: a real one reads within a few counts of zero while still, then
        # swings to thousands when the pad is turned.
        gyro = (int(18 * math.sin(angle)), int(18 * math.cos(angle)), 5)
        if 40 <= step < 60:
            gyro = (9000, -4000, 2500)                    # a fast turn
        struct.pack_into("<3h", body, 10, *gyro)
        # accel: magnitude 4096 in a rotating direction
        accel = (int(4096 * math.sin(angle)), 0, int(4096 * math.cos(angle)))
        struct.pack_into("<3h", body, 16, *accel)
        body[24:40] = bytes(random.randrange(256) for _ in range(16))   # buttons
        reports.append(bytes(body))

    failures = 0

    def check(name: str, ok: bool) -> None:
        nonlocal failures
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
        failures += 0 if ok else 1

    print("[self-test] synthetic pad: gyro@10 accel@16 counter@6")
    stable = accel_candidates(reports, 40)
    check("accelerometer found at offset 16",
          bool(stable) and stable[0][1] == 16 and abs(stable[0][3] - 4096) < 200)
    print("    accel candidates: " + ", ".join(f"{offset}(spread {spread * 100:.1f}%)"
                                                for spread, offset, *_ in stable[:4]))

    gyros = gyro_candidates(reports, 40)
    print("    gyro candidates: " + ", ".join(f"{offset}(med {median:.0f})"
                                               for median, _, offset, _ in gyros[:5]))
    check("gyro@10 among the top 3 plausible", 10 in [offset for _, _, offset, _ in gyros[:3]])
    check("counter bytes are not offered as gyro",
          all(offset not in (2, 4, 6) for _, _, offset, _ in gyros[:5]))

    counters = []
    for offset in range(0, 36):
        values = [struct.unpack_from("<H", report, offset)[0] for report in reports]
        deltas = {(b - a) % 65536 for a, b in zip(values, values[1:])}
        if deltas == {4}:
            counters.append(offset)
    check("counter found at offset 6 with step 4", 6 in counters)

    frozen = [index for index in range(40)
              if len({report[index] for report in reports}) == 1]
    check("frozen bytes detected (report id 0 stays constant)", 0 in frozen)

    print(f"\n  {failures} failure(s)")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true",
                        help="check the scanners against synthetic reports and exit")
    parser.add_argument("--list", action="store_true", help="list candidate devices")
    parser.add_argument("--all", action="store_true", help="list every HID device")
    parser.add_argument("--vid", help="vendor id, hex (e.g. 057E)")
    parser.add_argument("--pid", help="product id, hex (e.g. 2069)")
    parser.add_argument("--match", help="substring of the product/manufacturer name")
    parser.add_argument("--path", help="use this interface path directly")
    parser.add_argument("--analyze", type=float, default=3.0,
                        help="seconds of reports to collect (default 3)")
    parser.add_argument("--raw", type=int, default=6, help="hex-dump N reports first")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    print("controller-like HID interfaces:")
    rows = list_devices(args.all)
    if args.list:
        return 0

    vid = int(args.vid, 16) if args.vid else None
    pid = int(args.pid, 16) if args.pid else None
    target = None
    if args.path:
        target = next((info for info in hidwin.enumerate_hid()
                       if info.get("path") == args.path), None)
    else:
        target = pick(rows, vid, pid, args.match)
    if not target:
        print("\nno matching device; pass --vid/--pid/--match (see the list above).")
        return 1

    print(f"\ntarget: {target.get('vid', 0):04X}:{target.get('pid', 0):04X} "
          f"{target.get('manufacturer', '')!r} {target.get('product', '')!r}")
    describe(target)

    print(f"\nreading for {args.analyze:g}s ...")
    reports: list[bytes] = []
    started = time.monotonic()
    try:
        with hidwin.HidDevice(target["path"], target) as device:
            while time.monotonic() - started < args.analyze:
                report = device.read(500)
                if report:
                    reports.append(bytes(report))
    except Exception as exc:                            # noqa: BLE001
        print(f"  read failed: {exc}")
        return 1

    duration = time.monotonic() - started
    print(f"  {len(reports)} reports in {duration:.2f}s -> "
          f"{len(reports) / duration:.1f} reports/s")
    if not reports:
        return 1

    ids: dict[int, int] = {}
    for report in reports:
        ids[report[0]] = ids.get(report[0], 0) + 1
    print("  report ids: " + ", ".join(f"0x{k:02X} x{v}" for k, v in sorted(ids.items())))

    for index in range(min(args.raw, len(reports))):
        report = reports[index]
        print(f"  [{index}] id=0x{report[0]:02X} len={len(report):3d}  {report[:48].hex(' ')}")

    length = max(len(report) for report in reports)
    print(f"\nper-byte statistics over {length} bytes "
          f"(a byte that never changes cannot be motion):")
    print("    byte  min  max  changes")
    frozen = []
    for index in range(length):
        values = [report[index] for report in reports if len(report) > index]
        if len(values) < 2:
            continue
        changes = sum(1 for a, b in zip(values, values[1:]) if a != b)
        if changes == 0:
            frozen.append(index)
        print(f"    {index:4d} {min(values):4d} {max(values):4d} {changes:8d}")
    print(f"  always-identical bytes: "
          + (", ".join(str(index) for index in frozen) if frozen else "none"))

    scan_accel(reports, length)
    scan_gyro(reports, length)
    scan_counters(reports, length)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
