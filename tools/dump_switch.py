"""Live read-out for a real Switch Pro Controller (hardware bring-up tool).

Finds the controller, runs the handshake, then prints decoded state so the
protocol, calibration and 6-axis data can be checked against the physical
device.

    python tools\\dump_switch.py                    # live state, 10 seconds
    python tools\\dump_switch.py --seconds 30
    python tools\\dump_switch.py --raw              # hexdump every report
    python tools\\dump_switch.py --analyze          # plus an at-rest summary

``--analyze`` prints the average gravity vector, which axis currently points up
and the residual gyro rate.  Comparing that with a DualShock 4 read-out
(tools\\probe_ds4_frame.py) is how the IMU axis mapping is confirmed.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hidwin  # noqa: E402
import switchpro  # noqa: E402


def hexdump(data: bytes, width: int = 16) -> str:
    lines = []
    for offset in range(0, len(data), width):
        chunk = data[offset:offset + width]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"    {offset:04X}  {hex_part:<{width * 3}}  {text}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read a real Switch Pro Controller")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--device", metavar="PATH")
    parser.add_argument("--raw", action="store_true", help="hexdump every input report")
    parser.add_argument("--analyze", action="store_true",
                        help="finish with an at-rest gravity/gyro summary")
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    args = parser.parse_args()

    candidates = switchpro.find_controller_interfaces()
    print(f"matching HID interfaces: {len(candidates)}")
    for info in candidates:
        marker = " <-- using"
        print(f"  {hidwin.describe(info)}")

    controller = switchpro.SwitchProController(path=args.device, log=print)
    try:
        controller.open()
    except (switchpro.SwitchProError, OSError) as exc:
        print(f"cannot open the controller: {exc}")
        print("Is it connected and paired?  If HidHide is installed, check that "
              "this program is on its allow list.")
        return 1

    with controller:
        print("initialising...")
        controller.initialize()
        print(f"device: {controller.describe()}")

        accels, gyros = [], []
        timers = []
        deadline = time.monotonic() + args.seconds
        last_print = 0.0
        while time.monotonic() < deadline:
            state = controller.read_state(timeout_ms=300)
            if state is None:
                continue
            timers.append(time.monotonic())
            sample = state.imu[-1] if state.imu else None
            if sample is not None:
                accels.append(sample.accel)
                gyros.append(sample.gyro)

            if args.raw:
                print(f"  report 0x30: buttons={state.buttons} "
                      f"L={state.left_stick} R={state.right_stick} "
                      f"batt={state.battery_level}{' charging' if state.charging else ''}")
                if sample is not None:
                    print(f"    accel={sample.accel} gyro={sample.gyro}")

            now = time.monotonic()
            if not args.quiet and now - last_print > 0.1:
                last_print = now
                line = (f"  batt {state.battery_level_8}/8 "
                        f"L={state.left_stick} R={state.right_stick}")
                if sample is not None:
                    dps = controller.imu_calibration.to_dps(sample.gyro)
                    g = controller.imu_calibration.to_g(sample.accel)
                    line += (f"  gyro=({dps[0]:7.2f},{dps[1]:7.2f},{dps[2]:7.2f})dps"
                             f"  accel=({g[0]:6.3f},{g[1]:6.3f},{g[2]:6.3f})g")
                buttons = "+".join(state.pressed_names) or "-"
                print(f"{line}  [{buttons}]")

        if len(timers) > 1:
            intervals = [b - a for a, b in zip(timers, timers[1:])]
            print(f"\nreports: {len(timers)} in {args.seconds:g}s "
                  f"(~{len(timers) / args.seconds:.1f} reports/s, "
                  f"median interval {statistics.median(intervals) * 1000:.1f} ms)")

        if args.analyze and accels:
            mean_accel = tuple(statistics.fmean(sample[i] for sample in accels) for i in range(3))
            mean_gyro = tuple(statistics.fmean(sample[i] for sample in gyros) for i in range(3))
            g = controller.imu_calibration.to_g(tuple(int(v) for v in mean_accel))
            print("\nat-rest analysis (controller should be still, lying flat):")
            print(f"  mean raw accel: ({mean_accel[0]:8.1f}, {mean_accel[1]:8.1f}, {mean_accel[2]:8.1f})")
            print(f"  mean accel (g): ({g[0]:8.4f}, {g[1]:8.4f}, {g[2]:8.4f})")
            magnitudes = [abs(g[i]) for i in range(3)]
            axis = magnitudes.index(max(magnitudes))
            print(f"  gravity points along {'XYZ'[axis]} with sign "
                  f"{'+' if g[axis] >= 0 else '-'} ({g[axis]:+.4f} g)")
            print(f"  mean raw gyro : ({mean_gyro[0]:8.1f}, {mean_gyro[1]:8.1f}, {mean_gyro[2]:8.1f})")
            dps = controller.imu_calibration.to_dps(tuple(int(v) for v in mean_gyro))
            print(f"  residual rate : ({dps[0]:7.3f}, {dps[1]:7.3f}, {dps[2]:7.3f}) dps "
                  f"(should be close to 0)")
            print("  expected per Linux hid-nintendo.c: X toward the triggers, "
                  "Y to the player's left, Z up out of the buttons,")
            print("  so a controller lying flat face up should read about +Z = +1 g "
                  "(1 g = 4096 counts).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
