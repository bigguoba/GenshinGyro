"""Record a guided hardware session to a UTF-8 log file (one-off verification).

Writes, for each second, a summary line (raw gyro/accel, converted values,
sticks, battery), plus an event line whenever the pressed-button set changes
(with the raw button bytes) or a stick axis passes 80% of its travel.  That is
enough to verify the button/axis mapping against the physical controller.

    python tools\\record_session.py --seconds 600 --log-file hardware_session.txt
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hidwin  # noqa: E402
import switchpro  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=600.0)
    parser.add_argument("--log-file", default="hardware_session.txt")
    args = parser.parse_args()

    controller = switchpro.SwitchProController(log=lambda m: print(f"[init] {m}", flush=True))
    for info in switchpro.find_controller_interfaces():
        print(f"[init] {hidwin.describe(info)}", flush=True)
    controller.open()
    with controller, open(args.log_file, "w", encoding="utf-8") as logfile:
        def emit(line: str) -> None:
            print(line, flush=True)
            logfile.write(line + "\n")
            logfile.flush()

        controller.initialize()
        emit(f"[init] {controller.describe()}")
        emit(f"[init] accel_scale={tuple(round(v, 8) for v in controller.imu_calibration.accel_scale)} "
             f"gyro_scale={tuple(round(v, 6) for v in controller.imu_calibration.gyro_scale)} "
             f"accel_origin={controller.imu_calibration.accel_origin} "
             f"gyro_origin={controller.imu_calibration.gyro_origin}")
        emit(f"[init] stick centre L=({controller.left_stick_calibration.x.center},"
             f"{controller.left_stick_calibration.y.center}) "
             f"L range x=[{controller.left_stick_calibration.x.minimum},"
             f"{controller.left_stick_calibration.x.maximum}] "
             f"R centre=({controller.right_stick_calibration.x.center},"
             f"{controller.right_stick_calibration.y.center})")
        emit("[rec] ready - begin whenever you like")

        start = time.monotonic()
        deadline = start + args.seconds
        next_summary = start
        previous_buttons = None
        last_stick_event = 0.0
        reports = 0

        while time.monotonic() < deadline:
            state = controller.read_state(timeout_ms=300)
            if state is None:
                continue
            reports += 1
            now = time.monotonic()
            elapsed = now - start

            names = state.pressed_names
            if names != previous_buttons:
                previous_buttons = names
                emit(f"t={elapsed:6.1f}s  BUTTONS -> {'+'.join(names) or 'none'}"
                     f"   bytes=({state.buttons[0]:#04x},{state.buttons[1]:#04x},"
                     f"{state.buttons[2]:#04x})")

            left = controller.left_stick_calibration
            right = controller.right_stick_calibration
            nx, ny = left.x.normalize(state.left_stick[0]), left.y.normalize(state.left_stick[1])
            mx, my = right.x.normalize(state.right_stick[0]), right.y.normalize(state.right_stick[1])
            if (max(abs(nx), abs(ny), abs(mx), abs(my)) > 0.8
                    and now - last_stick_event > 0.4):
                last_stick_event = now
                emit(f"t={elapsed:6.1f}s  STICK   L_raw=({state.left_stick[0]},"
                     f"{state.left_stick[1]}) -> ({nx:+.2f},{ny:+.2f})  "
                     f"R_raw=({state.right_stick[0]},{state.right_stick[1]}) -> "
                     f"({mx:+.2f},{my:+.2f})")

            if now >= next_summary:
                next_summary = now + 1.0
                gyro = state.imu[-1].gyro if state.imu else (0, 0, 0)
                accel = state.imu[-1].accel if state.imu else (0, 0, 0)
                g = controller.imu_calibration.to_g(accel)
                dps = controller.imu_calibration.to_dps(gyro)
                emit(f"t={elapsed:6.1f}s  raw_g=({gyro[0]:6d},{gyro[1]:6d},{gyro[2]:6d})  "
                     f"raw_a=({accel[0]:6d},{accel[1]:6d},{accel[2]:6d})  "
                     f"a_g=({g[0]:7.3f},{g[1]:7.3f},{g[2]:7.3f})  "
                     f"g_dps=({dps[0]:7.2f},{dps[1]:7.2f},{dps[2]:7.2f})  "
                     f"L=({state.left_stick[0]},{state.left_stick[1]}) "
                     f"R=({state.right_stick[0]},{state.right_stick[1]})  "
                     f"batt={state.battery_level_8}/8  [{'+'.join(names) or '-'}]")

        elapsed = time.monotonic() - start
        emit(f"[rec] done: {reports} reports in {elapsed:.1f}s "
             f"(~{reports / elapsed:.1f} reports/s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
