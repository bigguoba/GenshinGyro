"""Probe: how does a Bluetooth DualShock 4 want to receive the 0x11 output report?

Three delivery paths are tried against the live pad, each with the kernel-style
CRC (0xA2 as the CRC32 init value, covering report[0..73]):

    WriteFile(78)            interrupt OUT, short buffer
    WriteFile(padded to 547) interrupt OUT, declared report size
    HidD_SetOutputReport     control transfer, declared report size

Whoever flips the pad from 0x01 (minimal) to 0x11 (full) is the answer.

    python tools\\probe_ds4_delivery.py
"""

from __future__ import annotations

import os
import struct
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dualshock4  # noqa: E402

SIZE = dualshock4.OUTPUT_REPORT_BT_SIZE
SEED = dualshock4.OUTPUT_CRC32_SEED


def build(hw_control: int = 0xC4) -> bytes:
    report = bytearray(SIZE)
    report[0] = dualshock4.OUTPUT_REPORT_BT
    report[1] = hw_control
    report[3] = dualshock4.VALID_FLAG0_LED
    report[8], report[9], report[10] = dualshock4.DEFAULT_LIGHTBAR
    struct.pack_into("<I", report, SIZE - 4, zlib.crc32(bytes(report[:-4]), SEED))
    return bytes(report)


def watch(pad, seconds: float) -> dict:
    ids: dict[int, int] = {}
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        report = pad._read_raw(120)
        if report:
            ids[report[0]] = ids.get(report[0], 0) + 1
    return ids


def main() -> int:
    items = [i for i in dualshock4.find_ds4_interfaces()
             if "00001124" in (i.get("path") or "").lower()]
    if not items:
        print("no Bluetooth DualShock 4 found")
        return 1
    pad = dualshock4.DualShock4(items[0]["path"], info=items[0], log=lambda m: None)
    pad.open()
    device = pad._device
    try:
        declared = (pad._info or {}).get("output_len") or SIZE
        print(f"transport={pad.transport} declared out={declared} report={SIZE}")
        report = build()
        print(f"crc = 0x{struct.unpack_from('<I', report, SIZE - 4)[0]:08X}")
        print(f"baseline: { {hex(k): v for k, v in watch(pad, 1.5).items()} }")

        attempts = [
            ("WriteFile 78", lambda: device.write(report)),
            ("WriteFile padded", lambda: device.write(report + bytes(declared - SIZE))),
            ("HidD_SetOutputReport", lambda: device.set_output_report(
                report + bytes(declared - SIZE))),
        ]
        for label, action in attempts:
            try:
                result = action()
            except Exception as exc:                        # noqa: BLE001
                print(f"  {label:22} raised {type(exc).__name__}: {exc}")
                continue
            ids = watch(pad, 2.5)
            full = ids.get(dualshock4.REPORT_BLUETOOTH, 0)
            print(f"  {label:22} returned {result!r:8} -> "
                  f"{ {hex(k): v for k, v in sorted(ids.items())} }"
                  + ("   <== WORKS" if full else ""))
            if full:
                print(f"\ndelivery path that works: {label}")
                return 0
        print("\nnone worked")
        return 1
    finally:
        pad.close()


if __name__ == "__main__":
    raise SystemExit(main())
