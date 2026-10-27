"""Rumble test for a Switch Pro style pad (NS mode), left and right separately.

Two payload encodings are available, because pads differ:

``doc``  the documented dekuNukem layout (frequency codes + amplitude table).
         Proven on this pad: it buzzes and the two sides are separable, but the
         strength curve is poor because the amplitude table only covers a narrow
         slice of the byte range.
``raw``  only the second byte of each side group is set, 0..255.  On this pad that
         produced a *monotonically* stronger vibration in a both-sides test, but it
         does not drive the sides independently and its response is not monotone
         once the sides differ -- so it is kept as a diagnostic only.

Stop is always the all-zero payload: that is the only thing proven to silence this
pad (the documented "neutral" 0x40 means a quarter-on for it).

Modes
-----
    python tools\\rumble_test.py --sweep-amplitude     strength ladder, fixed carrier
    python tools\\rumble_test.py --sweep-frequency     carrier ladder (doc encoding)
    python tools\\rumble_test.py --sweep               both / left / right / alternating
    python tools\\rumble_test.py --sweep-raw           one payload byte at a time
    python tools\\rumble_test.py --interactive          keyboard control
    python tools\\rumble_test.py --stop                just silence the pad

Use ``--encoding raw`` to run a mode with the raw-drive layout.
"""

from __future__ import annotations

import argparse
import msvcrt
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sources  # noqa: E402
import switchpro  # noqa: E402

REFRESH_HZ = 30.0
STEP = 0.1
HOLD_SECONDS = 0.9


# -- payload builders -------------------------------------------------------- #

def side_bytes(level: float, encoding: str) -> bytes:
    if encoding == "raw":
        return switchpro.rumble_side_level(level)
    return switchpro.rumble_side(level)


def payload_bytes(left: float, right: float, encoding: str) -> bytes:
    return side_bytes(left, encoding) + side_bytes(right, encoding)


def neutralize(controller, times: int = 10, interval: float = 0.03) -> None:
    """Send the all-zero payload repeatedly; the only thing that silences this pad."""
    for _ in range(max(1, times)):
        try:
            controller.stop_rumble()
        except Exception:                        # noqa: BLE001 - best effort
            pass
        time.sleep(max(0.0, interval))


# -- controller wrapper ------------------------------------------------------ #

class Pad:
    """Keeps one setting alive and can always stop the vibration."""

    def __init__(self, controller, encoding: str = "doc", log=print):
        self.controller = controller
        self.encoding = encoding
        self.log = log
        self.left = 0.0
        self.right = 0.0

    def send_sides(self, left: float, right: float, note: str = "") -> None:
        self.left = max(0.0, min(1.0, left))
        self.right = max(0.0, min(1.0, right))
        payload = payload_bytes(self.left, self.right, self.encoding)
        self.controller.set_rumble_payload(payload[:4], payload[4:])
        if note:
            self.log(f"   {note:28} L {self.left * 100:3.0f}%  R {self.right * 100:3.0f}%"
                     f"   报文 {' '.join(f'{b:02x}' for b in payload)}")

    def apply(self, level: float, note: str = "") -> None:
        self.send_sides(level, level, note)

    def _resend(self) -> None:
        payload = payload_bytes(self.left, self.right, self.encoding)
        self.controller.set_rumble_payload(payload[:4], payload[4:])

    def hold(self, seconds: float) -> None:
        deadline = time.perf_counter() + seconds
        next_send = time.perf_counter()
        while time.perf_counter() < deadline:
            now = time.perf_counter()
            if now >= next_send:
                self._resend()
                next_send += 1.0 / REFRESH_HZ
            time.sleep(0.001)


def hold_payload(controller, side: bytes, seconds: float, rate_hz: float = 30.0) -> None:
    """Keep one explicit payload alive for a while."""
    deadline = time.perf_counter() + seconds
    next_send = time.perf_counter()
    while time.perf_counter() < deadline:
        now = time.perf_counter()
        if now >= next_send:
            controller.set_rumble_payload(side, side)
            next_send += 1.0 / rate_hz
        time.sleep(0.005)


# -- modes ------------------------------------------------------------------- #

def sweep_amplitude(pad: Pad) -> None:
    levels = (0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
    print(f"\n[A] 固定载频，只加强度（编码 {pad.encoding}），每档 {HOLD_SECONDS:g} 秒")
    print("    请记：从第几档开始明显？是否逐档变强？有没有中间反而变弱？")
    for index, level in enumerate(levels, start=1):
        pad.apply(level, note=f"第 {index:2} 档 {level * 100:3.0f}%")
        pad.hold(HOLD_SECONDS)
    pad.apply(0.0, note="停止")
    print("\n    A 档位对应：1=5% 2=10% 3..11=20%..100%")


def sweep_frequency(pad: Pad) -> None:
    low_band = (41.0, 60.0, 90.0, 120.0, 160.0, 220.0, 320.0, 450.0, 626.0)
    high_band = (82.0, 120.0, 170.0, 240.0, 340.0, 480.0, 700.0, 1000.0, 1253.0)
    print("\n[B1] 强度 100%，只改低频段载频（desk 编码）")
    for index, hz in enumerate(low_band, start=1):
        side = switchpro.rumble_side_bands(1.0, hz, 0.0, 320.0)
        pad.log(f"   B1 第 {index:2} 档 低频 {hz:6.0f}Hz  报文 "
                f"{' '.join(f'{b:02x}' for b in side)}")
        hold_payload(pad.controller, side, HOLD_SECONDS)
    print("\n[B2] 强度 100%，只改高频段载频")
    for index, hz in enumerate(high_band, start=1):
        side = switchpro.rumble_side_bands(0.0, 160.0, 1.0, hz)
        pad.log(f"   B2 第 {index:2} 档 高频 {hz:6.0f}Hz  报文 "
                f"{' '.join(f'{b:02x}' for b in side)}")
        hold_payload(pad.controller, side, HOLD_SECONDS)
    pad.apply(0.0, note="停止")


def sweep_all(pad: Pad) -> None:
    steps = (0.2, 0.4, 0.6, 0.8, 1.0)
    print(f"\n[1] 左右同时（编码 {pad.encoding}）")
    for level in steps:
        pad.apply(level, note=f"同时 {level * 100:3.0f}%")
        pad.hold(1.0)
    print("\n[2] 只左")
    for level in steps:
        pad.send_sides(level, 0.0, note=f"只左 {level * 100:3.0f}%")
        pad.hold(1.0)
    print("\n[3] 只右")
    for level in steps:
        pad.send_sides(0.0, level, note=f"只右 {level * 100:3.0f}%")
        pad.hold(1.0)
    print("\n[4] 交替 左 → 右 → 左 → 右（100%）")
    for left, right in ((1.0, 0.0), (0.0, 1.0), (1.0, 0.0), (0.0, 1.0)):
        pad.send_sides(left, right, note="交替")
        pad.hold(0.6)
    pad.apply(0.0, note="停止")


def sweep_raw(controller, log=print) -> None:
    """Which payload byte is the strength knob on a non-standard pad?"""
    values = (0x20, 0x40, 0x80, 0xC0, 0xFF)
    positions = ((1, "byte1 (高频幅度)"), (2, "byte2 (低频码+幅度高位)"),
                 (3, "byte3 (低频幅度低字节)"))
    print("\n[R] 逐字节扫描（左右发送相同的载荷），每档 0.7 秒")
    for index, name in positions:
        print(f"\n[R{index}] 只动 {name}")
        for value in values:
            payload = bytearray(4)
            payload[index] = value
            log(f"   {name} = 0x{value:02X}   载荷 "
                f"{' '.join(f'{b:02x}' for b in payload)}")
            hold_payload(controller, bytes(payload), 0.7)
    print("\n[R4] 参考：文档满强度 00 43 c0 50")
    hold_payload(controller, bytes((0x00, 0x43, 0xC0, 0x50)), 0.9)
    print("\n[R5] 停止：全零")
    hold_payload(controller, bytes(4), 0.4)


def interactive(pad: Pad) -> None:
    print("\n按键：0-9 同时设强度   a/z 左减/加   k/m 右减/加")
    print("      l 只左   r 只右   b 两个一起   o 停止   q 退出\n")
    left = right = 0.0
    pad.send_sides(0.0, 0.0)
    last_send = time.perf_counter()
    while True:
        now = time.perf_counter()
        if now - last_send >= 1.0 / REFRESH_HZ:
            pad._resend()
            last_send = now
        if not msvcrt.kbhit():
            time.sleep(0.005)
            continue
        key = msvcrt.getwch().lower()
        if key in ("q", "\x03"):
            print("   退出")
            return
        if key.isdigit():
            left = right = 1.0 if key == "0" else int(key) / 10.0
        elif key == "a":
            left = max(0.0, left - STEP)
        elif key == "z":
            left = min(1.0, left + STEP)
        elif key == "k":
            right = max(0.0, right - STEP)
        elif key == "m":
            right = min(1.0, right + STEP)
        elif key == "l":
            left, right = 1.0, 0.0
        elif key == "r":
            left, right = 0.0, 1.0
        elif key == "b":
            left = right = 1.0
        elif key == "o":
            left = right = 0.0
        else:
            print("   （忽略这个键）")
            continue
        pad.send_sides(left, right, note="交互")


# -- entry point ------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--left", type=float, default=None)
    parser.add_argument("--right", type=float, default=None)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--encoding", choices=("doc", "raw"), default="doc")
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--sweep-amplitude", action="store_true")
    parser.add_argument("--sweep-frequency", action="store_true")
    parser.add_argument("--sweep-raw", action="store_true")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--device", default=None)
    args = parser.parse_args(argv)

    print("打开手柄（NS 模式）…")
    kind, controller = sources.open_source("switch", args.device, log=print)
    print(f"  已连接：{controller.describe()}  编码={args.encoding}")
    pad = Pad(controller, encoding=args.encoding, log=print)
    try:
        neutralize(controller, times=3, interval=0.02)
        if args.stop:
            neutralize(controller)
            print("  已发送全零载荷")
            return 0
        if args.left is not None or args.right is not None:
            left = 0.0 if args.left is None else args.left
            right = 0.0 if args.right is None else args.right
            print(f"\n保持 {args.seconds:g} 秒")
            pad.send_sides(left, right, note="固定档")
            pad.hold(args.seconds)
        elif args.sweep_raw:
            sweep_raw(controller, log=print)
        elif args.sweep_frequency:
            sweep_frequency(pad)
        elif args.sweep:
            sweep_all(pad)
        elif args.interactive:
            interactive(pad)
        else:
            sweep_amplitude(pad)
    except KeyboardInterrupt:
        print("\n   Ctrl+C：停止")
    finally:
        neutralize(controller)
        time.sleep(0.2)
        print("  已发送全零载荷（停止震动）")
        try:
            controller.close()
        except Exception:                        # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
