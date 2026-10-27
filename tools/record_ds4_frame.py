"""Read a real DualShock 4: declared capabilities + raw IMU frame under motion.

Two things in one run, because both need the real controller plugged in:

1. the input fields the device declares (what a game's HID parser sees), so it
   can be compared with the ViGEm virtual DS4;
2. the raw gyro/accelerometer of the real hardware while you perform known
   motions -- this is what tells us which raw field carries yaw, which pitch,
   which roll, and which way gravity points.  That is the ground truth the
   virtual pad has to reproduce for a game to behave identically.

    python tools\\record_ds4_frame.py --seconds 900
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hidwin  # noqa: E402

DS4_VID = 0x054C
DS4_PIDS = (0x09CC, 0x05C4)

GYRO_LSB_PER_DPS = 16.0        # SDL: 1/16 deg/s per count
ACCEL_LSB_PER_G = 8192.0


def pick_ds4() -> dict | None:
    """The 64-byte report interface is the one carrying the sensor data."""
    best = None
    for info in hidwin.enumerate_hid():
        if info.get("vid") != DS4_VID or info.get("pid") not in DS4_PIDS:
            continue
        if (info.get("input_len") or 0) == 64:
            return info
        if best is None:
            best = info
    return best


def decode(report: bytes):
    if report[0] == 0x01:
        payload = report[1:]
    elif report[0] == 0x11 and len(report) >= 78:
        payload = report[2:]
    else:
        return None
    if len(payload) < 24:
        return None
    buttons = payload[4] | (payload[5] << 8)
    gyro = struct.unpack_from("<3h", payload, 12)
    accel = struct.unpack_from("<3h", payload, 18)
    return buttons, gyro, accel


def show_caps(wait_seconds: float = 0.0) -> dict | None:
    deadline = time.monotonic() + wait_seconds
    info = pick_ds4()
    while info is None and time.monotonic() < deadline:
        print("waiting for a DualShock 4 (plug in the USB cable)...", flush=True)
        time.sleep(2.0)
        info = pick_ds4()
    if info is None:
        print("no DualShock 4 found (plug it in with the USB cable)")
        return None
    print(f"device: {hidwin.describe(info)}")
    try:
        _info, fields = hidwin.input_value_caps(info["path"])
    except OSError as exc:
        print(f"  cannot read declared capabilities: {exc}")
        return info
    print(f"declared input value fields: {len(fields)}")
    for field in fields:
        page = {0x01: "GenericDesktop", 0x09: "Button", 0xFF00: "VendorDefined"}.get(
            field["usage_page"], f"0x{field['usage_page']:04X}")
        usage = (f"0x{field['usage']:02X}" if field["usage_page"] != 0x09
                 else f"Button{field['usage']}")
        if field["is_range"]:
            usage += f"..0x{field['usage_max']:02X}"
        print(f"  id={field['report_id']} {field['bit_size']:2d}b x{field['report_count']:<2d} "
              f"{page}:{usage:<14s} logical={field['logical_min']}..{field['logical_max']} "
              f"physical={field['physical_min']}..{field['physical_max']} "
              f"units=0x{field['units']:X} exp={field['units_exp']}")
    sixteen = [f for f in fields if f["bit_size"] == 16]
    print(f"16-bit fields: {len(sixteen)}")
    return info


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=900.0)
    parser.add_argument("--wait", type=float, default=180.0,
                        help="seconds to wait for the controller to be plugged in")
    parser.add_argument("--log-file", default="ds4_frame.txt")
    args = parser.parse_args()

    info = show_caps(wait_seconds=args.wait)
    if info is None:
        return 1

    with open(args.log_file, "w", encoding="utf-8") as logfile:
        def emit(line: str) -> None:
            print(line, flush=True)
            logfile.write(line + "\n")
            logfile.flush()

        emit(f"[cap] {hidwin.describe(info)}")
        emit("[rec] recording; perform the motions whenever you like")

        device = hidwin.HidDevice(info["path"], info).open()
        start = time.monotonic()
        window_end = start + 0.5
        gyro_peak = [0, 0, 0]
        accel_peak = [0, 0, 0]
        gyro_sum = [0.0, 0.0, 0.0]
        accel_sum = [0.0, 0.0, 0.0]
        samples = 0
        total = 0
        try:
            while time.monotonic() - start < args.seconds:
                report = device.read(timeout_ms=300)
                if not report:
                    continue
                parsed = decode(report)
                if parsed is None:
                    continue
                _buttons, gyro, accel = parsed
                total += 1
                samples += 1
                for index in range(3):
                    gyro_peak[index] = max(gyro_peak[index], abs(gyro[index]))
                    accel_peak[index] = max(accel_peak[index], abs(accel[index]))
                    gyro_sum[index] += gyro[index]
                    accel_sum[index] += accel[index]
                now = time.monotonic()
                if now >= window_end:
                    elapsed = now - start
                    mean_gyro = [value / samples for value in gyro_sum]
                    mean_accel = [value / samples for value in accel_sum]
                    emit(
                        f"t={elapsed:6.1f}s  "
                        f"gyro peak=({gyro_peak[0]:6d},{gyro_peak[1]:6d},{gyro_peak[2]:6d}) "
                        f"mean=({mean_gyro[0]:7.1f},{mean_gyro[1]:7.1f},{mean_gyro[2]:7.1f})  "
                        f"accel peak=({accel_peak[0]:6d},{accel_peak[1]:6d},{accel_peak[2]:6d}) "
                        f"mean=({mean_accel[0]:7.1f},{mean_accel[1]:7.1f},{mean_accel[2]:7.1f})"
                    )
                    gyro_peak = [0, 0, 0]
                    accel_peak = [0, 0, 0]
                    gyro_sum = [0.0, 0.0, 0.0]
                    accel_sum = [0.0, 0.0, 0.0]
                    samples = 0
                    window_end = now + 0.5
        finally:
            device.close()
        emit(f"[rec] done: {total} reports")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
