"""Why does the virtual pad look like it runs at ~300 Hz instead of 250 Hz?

Reads the *same* virtual pad twice: once as fast as possible, once paced at
exactly the real pad's rate.  If the delivered report rate follows the reader
while the number of *distinct motion samples* stays the same, then the extra
reports are repeats produced by the reader's own polling, not extra data -- the
emulated device answers every read immediately with its latest state, whereas a
real pad is paced by its USB interrupt endpoint (bInterval 4 ms -> 250 Hz cap).

    python tools\\read_rate_probe.py --seconds 3
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

REAL_PAD_HZ = 250.0


def collect(info: dict, seconds: float, pace_hz: float | None) -> list[tuple[int, int]]:
    samples: list[tuple[int, int]] = []
    with hidwin.HidDevice(info["path"], info).open() as device:
        device.drain(max_reports=64)
        started = time.perf_counter()
        next_read = started
        while time.perf_counter() - started < seconds:
            report = device.read(timeout_ms=50)
            if report and report[0] == 0x01:
                state = dualshock4.parse_report(report, offset=1)
                samples.append((state.timestamp, state.gyro[0]))
            if pace_hz:
                next_read += 1.0 / pace_hz
                while True:
                    remaining = next_read - time.perf_counter()
                    if remaining <= 0:
                        break
                    if remaining > 0.0006:
                        time.sleep(remaining - 0.0004)
                    else:
                        while time.perf_counter() < next_read:
                            pass
        return samples


def report(label: str, samples: list[tuple[int, int]], seconds: float) -> None:
    if len(samples) < 5:
        print(f"  {label}: too few reports ({len(samples)})")
        return
    stamps = [stamp for stamp, _ in samples]
    values = [value for _, value in samples]
    steps = {(b - a) % 65536 for a, b in zip(stamps, stamps[1:])}
    nonzero = {step for step in steps if step}
    print(f"  {label}")
    print(f"    交付报文 {len(samples)} 条 / {seconds:.1f}s = "
          f"{len(samples) / seconds:.0f} 条/秒")
    print(f"    不同的体感样本 {len(set(values))} 个 = "
          f"{len(set(values)) / seconds:.0f} 个/秒")
    print(f"    时间戳非零步长集合 {sorted(nonzero)[:4]}  "
          f"（重复读取产生的 0 步占 {sum(1 for s in steps if not s) / max(1, len(steps)) * 100:.0f}%）")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=3.0)
    args = parser.parse_args()

    virtual = None
    physical = None
    for info in hidwin.enumerate_hid():
        if info.get("vid") != dualshock4.DS4_VID:
            continue
        if hidwin.is_emulated(info):
            if (info.get("input_len") or 0) == 64 and virtual is None:
                virtual = info
        elif physical is None:
            physical = info
    if physical is None or virtual is None:
        print("need both a physical pad (to relay) and the virtual pad running")
        return 1

    print(f"实体手柄: {hidwin.describe(physical)}")
    print(f"虚拟手柄: {hidwin.describe(virtual)}\n")

    print("实体手柄（真机由 USB 端点定速，读到多少就是多少）:")
    report("尽快读", collect(physical, args.seconds, None), args.seconds)

    print("\n虚拟手柄:")
    report("尽快读（模拟一个只管拼命轮询的读取方）",
           collect(virtual, args.seconds, None), args.seconds)
    report(f"按真实节奏 {REAL_PAD_HZ:.0f} Hz 读",
           collect(virtual, args.seconds, REAL_PAD_HZ), args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
