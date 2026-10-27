"""Compare the motion streams of the physical and the virtual DualShock 4.

The question this answers: where do repeated ("staircase") motion samples come
from?  Either the relay submits more reports than the pad produces (each extra
one repeating the previous sample), or the emulated device is polled faster than
the relay updates it and answers every poll with the last report.  In the first
case the fix is to stop sending duplicates; in the second the only fix is to
update often enough that every poll sees a fresh, interpolated value.

Run the relay first, then:

    python tools\\compare_motion_streams.py --seconds 5
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dualshock4  # noqa: E402
import hidwin  # noqa: E402


@dataclass
class Sample:
    at: float
    report_id: int
    gyro: tuple[int, int, int]
    accel: tuple[int, int, int]
    timestamp: int


def find_devices() -> tuple[dict | None, dict | None]:
    physical = None
    virtual = None
    for info in hidwin.enumerate_hid():
        if info.get("vid") != dualshock4.DS4_VID:
            continue
        if hidwin.is_emulated(info):
            if (info.get("input_len") or 0) == 64 and virtual is None:
                virtual = info
        elif physical is None:
            physical = info
    return physical, virtual


def reader(info: dict, offset: int | None, out: list[Sample], stop: threading.Event,
           label: str) -> None:
    try:
        with hidwin.HidDevice(info["path"], info).open() as device:
            device.drain(max_reports=64)
            while not stop.is_set():
                report = device.read(timeout_ms=100)
                if not report:
                    continue
                this_offset = offset if offset is not None \
                    else dualshock4.state_offset_for(report[0])
                state = dualshock4.parse_report(report, this_offset)
                if state is None:
                    continue
                out.append(Sample(time.monotonic(), state.report_id, state.gyro,
                                  state.accel, state.timestamp))
    except OSError as exc:  # pragma: no cover - hardware dependent
        print(f"  {label}: read failed: {exc}")


def describe(label: str, samples: list[Sample], seconds: float) -> None:
    if len(samples) < 3:
        print(f"  {label}: too few samples ({len(samples)})")
        return
    span = samples[-1].at - samples[0].at
    print(f"\n{label}: {len(samples)} reports in {span:.2f}s "
          f"-> {len(samples) / span:.1f} reports/s")

    duplicated = 0
    stalled_timestamp = 0
    for previous, current in zip(samples, samples[1:]):
        if current.gyro == previous.gyro and current.accel == previous.accel:
            duplicated += 1
        if current.timestamp == previous.timestamp:
            stalled_timestamp += 1
    total = len(samples) - 1
    print(f"  相邻两条报文体感完全相同: {duplicated}/{total} "
          f"({duplicated / total * 100:.0f}%)")
    print(f"  相邻两条报文时间戳相同:   {stalled_timestamp}/{total} "
          f"({stalled_timestamp / total * 100:.0f}%)")

    distinct = len({(s.gyro, s.accel) for s in samples})
    print(f"  不同的体感样本数: {distinct} "
          f"(= {distinct / span:.1f} 个/秒；报告 {len(samples) / span:.1f} 个/秒)")

    # Spikes: the value turns around sharply and immediately turns back, which is
    # what an extrapolation that has to be retracted looks like.
    values = [s.gyro[0] for s in samples]
    spikes = 0
    worst_spike = 0
    for previous, current, following in zip(values, values[1:], values[2:]):
        up, down = current - previous, following - current
        if up * down < 0:
            size = min(abs(up), abs(down))
            if size >= 6:                     # below the sensor noise floor
                spikes += 1
                worst_spike = max(worst_spike, size)
    span_pairs = max(1, len(values) - 2)
    print(f"  毛刺（相邻方向的急转）: {spikes}/{span_pairs} = "
          f"{spikes / span_pairs * 100:.1f}%，最大幅度 {worst_spike} counts")
    print(f"  回退步数: "
          f"{sum(1 for a, b in zip(values, values[1:]) if b < a)}/{len(values) - 1}，"
          f"最大回退 {abs(min([b - a for a, b in zip(values, values[1:])] or [0]))} counts")

    gaps = [b.at - a.at for a, b in zip(samples, samples[1:])]
    gaps.sort()
    median = gaps[len(gaps) // 2] * 1000
    print(f"  报文到达间隔: 中位 {median:.2f} ms, "
          f"最小 {gaps[0] * 1000:.2f} ms, 最大 {gaps[-1] * 1000:.2f} ms")

    print("  陀螺仪 X 连续 26 条（看是否出现重复台阶）:")
    values = [s.gyro[0] for s in samples[:26]]
    print("    " + " ".join(f"{v:6d}" for v in values))
    if len(samples) > 26:
        values = [s.gyro[0] for s in samples[26:52]]
        print("    " + " ".join(f"{v:6d}" for v in values))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=5.0)
    args = parser.parse_args()

    physical, virtual = find_devices()
    if physical is None:
        print("no physical DualShock 4 present")
        return 1
    if virtual is None:
        print("no virtual DualShock 4 present (start the relay first)")
        return 1
    print(f"physical: {hidwin.describe(physical)}")
    print(f"virtual : {hidwin.describe(virtual)}")

    physical_samples: list[Sample] = []
    virtual_samples: list[Sample] = []
    stop = threading.Event()
    threads = [
        threading.Thread(target=reader, args=(physical, None, physical_samples, stop,
                                             "physical"), daemon=True),
        threading.Thread(target=reader, args=(virtual, 1, virtual_samples, stop,
                                             "virtual"), daemon=True),
    ]
    for thread in threads:
        thread.start()
    time.sleep(args.seconds)
    stop.set()
    for thread in threads:
        thread.join(timeout=1.0)

    describe("实体手柄 (physical)", physical_samples, args.seconds)
    describe("虚拟手柄 (virtual)", virtual_samples, args.seconds)

    print("\n结论提示:")
    print("  * 若虚拟手柄的『不同样本数/秒』≈ 实体手柄的报告率，而报告率更高")
    print("    -> 多出来的报告只是重复上一条：台阶来自虚拟设备的轮询频率。")
    print("  * 若两者报告率相近但虚拟仍有大量重复 -> 重复来自实体手柄本身。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
