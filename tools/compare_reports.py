"""Push a real DualShock 4 report through the emulated one and diff the result.

Captures a live report from the physical pad, copies those exact 63 payload
bytes into a ViGEm DS4, reads the virtual pad's report back and compares them
byte by byte.  Any position the emulated pad changes (or fails to reproduce)
shows exactly what a game would see differently.

    python tools\\compare_reports.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hidwin  # noqa: E402
import vigem  # noqa: E402


def find_real_ds4() -> dict | None:
    for info in hidwin.enumerate_hid():
        if info.get("vid") == 0x054C and (info.get("input_len") or 0) == 64 \
                and not hidwin.is_emulated(info):
            return info
    return None


def main() -> int:
    real = find_real_ds4()
    if real is None:
        print("no physical DualShock 4 present")
        return 1
    print(f"real pad: {hidwin.describe(real)}")

    with hidwin.HidDevice(real["path"], real).open() as device:
        device.drain(max_reports=32, timeout_ms=0)
        captured = None
        deadline = time.time() + 3.0
        while time.time() < deadline and captured is None:
            captured = device.read(timeout_ms=500)
        if captured is None:
            print("no report from the real pad")
            return 1
    if captured[0] != 0x01:
        print(f"unexpected report id 0x{captured[0]:02X}")
        return 1
    real_payload = captured[1:64]
    print(f"captured real payload ({len(real_payload)} bytes): {real_payload.hex(' ')}")

    with vigem.ViGEmClient() as client:
        before = {i["path"] for i in hidwin.enumerate_hid()}
        pad = client.create_ds4()
        pad.plug_in()
        try:
            time.sleep(1.2)
            virtual = None
            for info in hidwin.enumerate_hid():
                if info["path"] not in before and info.get("vid") == 0x054C:
                    virtual = info
                    break
            if virtual is None:
                print("virtual pad not found")
                return 1

            for index, value in enumerate(real_payload):
                pad.report.ReportBuffer[index] = value
            pad.submit()
            time.sleep(0.4)

            with hidwin.HidDevice(virtual["path"], virtual).open() as device:
                device.drain(max_reports=16, timeout_ms=0)
                got = None
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    data = device.read(timeout_ms=500)
                    if data:
                        got = data[1:64] if data[0] == 0x01 else data[:63]
                        break
            if got is None:
                print("no report read back from the virtual pad")
                return 1

            print(f"read back virtual payload: {got.hex(' ')}")
            fields = [
                (0, 4, "sticks LX/LY/RX/RY"), (4, 6, "buttons"), (6, 7, "special"),
                (7, 9, "triggers"), (9, 11, "timestamp"), (11, 12, "battery"),
                (12, 18, "GYRO X/Y/Z"), (18, 24, "ACCEL X/Y/Z"),
                (24, 29, "unknown1"), (29, 30, "battery special"),
                (30, 32, "unknown2"), (32, 33, "touch packets"),
                (33, 45, "touch data"), (45, 63, "trailing"),
            ]
            print("\nbyte-by-byte (real -> virtual):")
            differences = 0
            for start, end, label in fields:
                same = real_payload[start:end] == got[start:end]
                if not same:
                    differences += 1
                print(f"  {'same' if same else 'DIFF'}  {label:18s} "
                      f"real={real_payload[start:end].hex(' ')}  "
                      f"virtual={got[start:end].hex(' ')}")
            print(f"\n{len(fields) - differences}/{len(fields)} field groups reproduced exactly")
            return 0
        finally:
            pad.unplug()


if __name__ == "__main__":
    raise SystemExit(main())
