"""Measure a real DualShock 4's report rate and timestamp behaviour.

The DS4 report carries a 16-bit timestamp in 5.33 us units (SDL: "Sensor
timestamp is in 5.33us units", timestamp_us = value * 16 / 3).  A game that
integrates one camera axis over time depends on that field being plausible, so
this measures what real hardware does.

    python tools\\measure_ds4_timing.py
"""

from __future__ import annotations

import os
import statistics
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hidwin  # noqa: E402


def find_real_ds4() -> dict | None:
    for info in hidwin.enumerate_hid():
        if info.get("vid") == 0x054C and (info.get("input_len") or 0) == 64 \
                and not hidwin.is_emulated(info):
            return info
    return None


def main() -> int:
    info = find_real_ds4()
    if info is None:
        print("no physical DualShock 4 present")
        return 1
    print(f"device: {hidwin.describe(info)}")

    samples = []
    with hidwin.HidDevice(info["path"], info).open() as device:
        device.drain(max_reports=64, timeout_ms=0)
        start = time.monotonic()
        while time.monotonic() - start < 3.0:
            report = device.read(timeout_ms=500)
            if not report or report[0] != 0x01:
                continue
            payload = report[1:]
            timestamp = struct.unpack_from("<H", payload, 9)[0]
            samples.append((time.monotonic(), timestamp))
    if len(samples) < 20:
        print(f"only {len(samples)} reports; is the pad idle/streaming?")
        return 1

    time_deltas = [b[0] - a[0] for a, b in zip(samples, samples[1:])]
    stamp_deltas = []
    for (ta, sa), (tb, sb) in zip(samples, samples[1:]):
        delta = sb - sa
        if delta < 0:                      # 16-bit wrap
            delta += 1 << 16
        stamp_deltas.append(delta)

    duration = samples[-1][0] - samples[0][0]
    print(f"reports: {len(samples)} in {duration:.2f}s "
          f"-> {len(samples) / duration:.1f} reports/s")
    print(f"report interval: median {statistics.median(time_deltas) * 1000:.2f} ms "
          f"(min {min(time_deltas) * 1000:.2f}, max {max(time_deltas) * 1000:.2f})")
    print(f"timestamp delta: median {statistics.median(stamp_deltas):.0f} "
          f"(min {min(stamp_deltas)}, max {max(stamp_deltas)})")
    median_delta = statistics.median(stamp_deltas)
    if statistics.median(time_deltas) > 0:
        implied_unit_s = (median_delta * 5.33e-6) / statistics.median(time_deltas)
        print(f"timestamp units per report / 5.33us = {implied_unit_s:.3f} "
              f"(1.0 means the 5.33us unit is right)")
    print(f"timestamp wraps at 65536 -> wraps every "
          f"{65536 / median_delta * statistics.median(time_deltas):.2f}s")
    print("\nfirst 10 (t, timestamp, delta):")
    for index, (t, stamp) in enumerate(samples[:10]):
        delta = stamp_deltas[index] if index < len(stamp_deltas) else 0
        print(f"  {t - samples[0][0]:8.4f}s  {stamp:6d}  +{delta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
