"""The Bluetooth output report must go over the control channel, not WriteFile.

Measured on real hardware: a 0x11 mode request written with ``WriteFile`` on the
interrupt OUT endpoint is accepted (it even reports all bytes written) but the pad
never sees it, so it stays in the motionless 0x01 report -- which shows up as
"Bluetooth gyro does not work unless a game is running".  The same bytes through
``HidD_SetOutputReport`` switch the pad immediately.

This test pins the choice down with a fake device, so no hardware is needed.

Run:  python tools\\test_ds4_output_path.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dualshock4  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, actual, expected) -> None:
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {actual!r}, want {expected!r}")


class FakeDevice:
    """Records which Windows API the pad class decided to use."""

    def __init__(self, accept_control: bool = True):
        self.accept_control = accept_control
        self.control_calls: list[int] = []
        self.write_calls: list[int] = []

    def set_output_report(self, data: bytes) -> bool:
        self.control_calls.append(len(data))
        return self.accept_control

    def write(self, data: bytes, timeout_ms: int = 500) -> int:
        self.write_calls.append(len(data))
        return len(data)


def make_pad(transport: str, output_len: int, accept_control: bool = True):
    pad = dualshock4.DualShock4(info={"output_len": output_len},
                               log=lambda *_: None)
    pad.transport = transport
    pad._device = FakeDevice(accept_control)
    return pad, pad._device


def main() -> int:
    report = dualshock4.build_extended_mode_report()

    print("\n[1] 蓝牙：走控制通道（HidD_SetOutputReport）")
    pad, device = make_pad("bluetooth", 547)
    written = pad.write_output(report)
    check("只调用了控制通道一次", len(device.control_calls), 1)
    check("没有用 WriteFile", len(device.write_calls), 0)
    check("长度补到设备声明的 547", device.control_calls[0], 547)
    check("返回值是实际发送长度", written, 547)

    print("\n[2] 有线：仍然用 WriteFile")
    pad, device = make_pad("usb", 32)
    pad.write_output(report)
    check("走 WriteFile 一次", len(device.write_calls), 1)
    check("没有误用控制通道", len(device.control_calls), 0)

    print("\n[3] 控制通道被拒时退回 WriteFile（并说明原因）")
    pad, device = make_pad("bluetooth", 547, accept_control=False)
    pad.write_output(report)
    check("先用控制通道试过", len(device.control_calls), 1)
    check("再退回 WriteFile", len(device.write_calls), 1)

    print("\n[4] 声明长度比报文短时不会截断")
    pad, device = make_pad("bluetooth", 10)
    written = pad.write_output(report)
    check("仍按报文原长发送", max(device.control_calls), len(report))
    check("返回值合理", written >= len(report), True)

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
