"""Compare the feature reports of the real and emulated DualShock 4.

Games and libraries read feature report 0x02 (motion calibration), 0x12 (serial)
and 0xA3 (firmware info) from a real pad.  If the emulated pad answers those
differently, a game's gyro scaling can end up different even when the input
report carries identical data.

    python tools\\compare_features.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hidwin  # noqa: E402
import vigem  # noqa: E402

FEATURES = [0x02, 0x03, 0x05, 0x12, 0xA3]


def read_features(info: dict, label: str) -> None:
    print(f"\n=== {label}: {info.get('product','')} "
          f"({'emulated' if hidwin.is_emulated(info) else 'physical ' + hidwin.transport_of(info)}) ===")
    try:
        device = hidwin.HidDevice(info["path"], info).open()
    except OSError as exc:
        print(f"  cannot open: {exc}")
        return
    with device:
        for report_id in FEATURES:
            try:
                data = device.get_feature(report_id, 64)
            except OSError as exc:
                print(f"  feature 0x{report_id:02X}: error {exc}")
                continue
            if data is None:
                print(f"  feature 0x{report_id:02X}: not supported / failed")
                continue
            body = data[:24]
            nonzero = any(body)
            print(f"  feature 0x{report_id:02X}: {len(data)} bytes, "
                  f"{'data' if nonzero else 'ALL ZERO'}  {body.hex(' ')}")


def main() -> int:
    for info in hidwin.enumerate_hid():
        if info.get("vid") == 0x054C and not hidwin.is_emulated(info):
            read_features(info, "real DualShock 4")
            break
    else:
        print("no physical DualShock 4 present")

    with vigem.ViGEmClient() as client:
        before = {i["path"] for i in hidwin.enumerate_hid()}
        pad = client.create_ds4()
        pad.plug_in()
        try:
            time.sleep(1.5)
            for info in hidwin.enumerate_hid():
                if info["path"] not in before and info.get("vid") == 0x054C:
                    read_features(info, "virtual DualShock 4 (ViGEmBus)")
        finally:
            pad.unplug()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
