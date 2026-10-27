"""Read a real DualShock 4's IMU at rest to establish the DS4 axis frame.

Passive and read-only: it only reads HID input reports from an already-connected
DS4 (or from the virtual pad while one is plugged in).

Run:  python tools\\probe_ds4_frame.py
"""

from __future__ import annotations

import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hidwin  # noqa: E402

DS4_VID = 0x054C
DS4_PIDS = (0x05C4, 0x09CC)


def pick_ds4() -> dict | None:
    best = None
    for info in hidwin.enumerate_hid():
        if info.get("vid") != DS4_VID or info.get("pid") not in DS4_PIDS:
            continue
        # The 64-byte report carrying interface is the usable one.
        if (info.get("input_len") or 0) == 64:
            return info
        if best is None:
            best = info
    return best


def decode_ds4(report: bytes):
    """Return (sticks, buttons, triggers, gyro, accel) from a DS4 input report."""
    if report[0] == 0x01:
        payload = report[1:]
    elif report[0] == 0x11 and len(report) >= 78:
        payload = report[2:]  # Bluetooth: 0x11, flags, then the same payload
    else:
        return None
    if len(payload) < 24:
        return None
    sticks = tuple(payload[0:4])
    buttons = struct.unpack_from("<H", payload, 4)[0]
    special = payload[6]
    triggers = (payload[7], payload[8])
    gyro = struct.unpack_from("<3h", payload, 12)
    accel = struct.unpack_from("<3h", payload, 18)
    return sticks, buttons, special, triggers, gyro, accel


def main() -> int:
    info = pick_ds4()
    if not info:
        print("no physical DualShock 4 found")
        return 1
    print("device:", hidwin.describe(info))

    gyros, accels = [], []
    with hidwin.HidDevice(info["path"], info).open() as device:
        device.drain(max_reports=32, timeout_ms=0)
        deadline = time.time() + 4.0
        while time.time() < deadline and len(accels) < 60:
            report = device.read(timeout_ms=500)
            if not report:
                continue
            parsed = decode_ds4(report)
            if parsed is None:
                continue
            _, buttons, special, triggers, gyro, accel = parsed
            gyros.append(gyro)
            accels.append(accel)

    if not accels:
        print("no decodable reports")
        return 1

    def average(rows):
        return tuple(sum(row[i] for row in rows) / len(rows) for i in range(3))

    gyro_avg = average(gyros)
    accel_avg = average(accels)
    print(f"samples: {len(accels)}")
    print(f"buttons: 0x{buttons:04X}  special: 0x{special:02X}  triggers: {triggers}")
    print(f"gyro  raw avg: ({gyro_avg[0]:9.1f}, {gyro_avg[1]:9.1f}, {gyro_avg[2]:9.1f})")
    print(f"gyro  dps     : ({gyro_avg[0] / 16.384:9.2f}, {gyro_avg[1] / 16.384:9.2f}, {gyro_avg[2] / 16.384:9.2f})")
    print(f"accel raw avg: ({accel_avg[0]:9.1f}, {accel_avg[1]:9.1f}, {accel_avg[2]:9.1f})")
    print(f"accel g       : ({accel_avg[0] / 8192:9.3f}, {accel_avg[1] / 8192:9.3f}, {accel_avg[2] / 8192:9.3f})")

    magnitudes = [abs(accel_avg[i]) for i in range(3)]
    dominant = magnitudes.index(max(magnitudes))
    print(
        f"gravity dominates axis {'XYZ'[dominant]} with sign "
        f"{'+' if accel_avg[dominant] > 0 else '-'} "
        f"({accel_avg[dominant] / 8192:+.3f} g) -> "
        f"{'controller is roughly level' if max(magnitudes) / 8192 > 0.75 else 'controller is tilted/at an angle'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
