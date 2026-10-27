"""Find which packet actually stops this pad's vibration.

The documented neutral rumble payload (00 01 40 40 per side) does not stop every
pad -- some clones latch the last strength until they are powered off.  Since the
whole haptics feature is unsafe without a reliable stop, this walks a ladder of
candidate clear sequences and asks after each one.

Run it with the pad already buzzing (start any rumble first), then answer y/n:

    python tools\\rumble_clear_probe.py
    python tools\\rumble_clear_probe.py --only 3     # run a single step

Each step prints what it sent.  Report the number of the step that stopped it --
or "none" if only a power cycle works.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sources  # noqa: E402
import switchpro  # noqa: E402

NEUTRAL_SIDE = bytes((0x00, 0x01, 0x40, 0x40))


def neutral_10(controller, count: int, rate_hz: float) -> None:
    """Send the rumble-only neutral report a number of times at a steady rate."""
    interval = 1.0 / rate_hz
    for _ in range(count):
        controller.set_rumble_payload(NEUTRAL_SIDE, NEUTRAL_SIDE)
        time.sleep(interval)


def neutral_01(controller, count: int, rate_hz: float) -> None:
    """Neutral rumble carried inside a 0x01 (rumble + subcommand) report."""
    interval = 1.0 / rate_hz
    for _ in range(count):
        controller._write(bytes((switchpro.OUTPUT_REPORT_RUMBLE_AND_SUBCMD,
                                 controller._next_command_number())) + switchpro.NEUTRAL_RUMBLE)
        time.sleep(interval)


def raw(controller, left: bytes, right: bytes | None = None,
        count: int = 30, rate_hz: float = 100.0) -> None:
    """Send one explicit payload several times (for probing non-standard pads)."""
    right = left if right is None else right
    interval = 1.0 / rate_hz
    for _ in range(count):
        controller.set_rumble_payload(left, right)
        time.sleep(interval)


STEPS = [
    ("0x10 中立报文 ×30 @100Hz（文档值 00 01 40 40）",
     lambda c: neutral_10(c, 30, 100.0)),
    ("0x10 中立报文 ×120 @100Hz（持续压制 1.2 秒）",
     lambda c: neutral_10(c, 120, 100.0)),
    ("低频幅度字节置 0：00 01 40 00 ×30（最可能的原因）",
     lambda c: raw(c, bytes((0x00, 0x01, 0x40, 0x00)))),
    ("两个幅度字节都置 0：00 00 40 00 ×30",
     lambda c: raw(c, bytes((0x00, 0x00, 0x40, 0x00)))),
    ("全零载荷 00 00 00 00 ×30",
     lambda c: raw(c, bytes(4))),
    ("0x01 报文（含中立震动载荷）×30 @100Hz",
     lambda c: neutral_01(c, 30, 100.0)),
    ("子命令 0x48 关闭震动上报 + 中立 ×10",
     lambda c: (c.send_subcommand(switchpro.SUB_ENABLE_VIBRATION, b"\x00"),
                neutral_10(c, 10, 100.0))),
    ("切换输入报文模式（0x03 置 0x30）+ 中立 ×10",
     lambda c: (c.send_subcommand(0x03, b"\x30"),
                neutral_10(c, 10, 100.0))),
    ("先强震 50 ms 再中立 ×30（强制一次状态转换）",
     lambda c: (c.set_rumble_payload(switchpro.rumble_side(1.0),
                                     switchpro.rumble_side(1.0)), time.sleep(0.05),
                neutral_10(c, 30, 100.0))),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", type=int, default=0,
                        help="run just this step number (1-based)")
    parser.add_argument("--no-buzz", action="store_true",
                        help="do not start a vibration first (assume the pad already buzzes)")
    parser.add_argument("--device", default=None)
    args = parser.parse_args(argv)

    print("打开手柄（NS 模式）…")
    kind, controller = sources.open_source("switch", args.device, log=print)
    print(f"  已连接：{controller.describe()}")
    if not args.no_buzz:
        print("\n先让手柄震起来（100%，0.8 秒），这样每一步都能验证“停没停”")
        for _ in range(24):
            controller.set_rumble_payload(switchpro.rumble_side(1.0),
                                          switchpro.rumble_side(1.0))
            time.sleep(0.033)
        print("  手柄现在应该在震 ✓")
    try:
        steps = ([(args.only, STEPS[args.only - 1])] if args.only
                 else list(enumerate(STEPS, start=1)))
        for number, (title, action) in steps:
            print(f"\n--- 第 {number} 步：{title}")
            action(controller)
            if args.only:
                print("   （单步模式，结束）")
                continue
            try:
                answer = input("   现在还在震吗？(y=还在震 / n=停了)：").strip().lower()
            except EOFError:
                return 0
            if answer.startswith("n"):
                print(f"\n   >>> 第 {number} 步有效：这条路能让手柄停下。")
                print("   请把这条消息发给我，我按它实现 M3 的停止逻辑。")
                return 0
        print("\n   所有步骤都没能让它停下（或者你没确认）。")
        print("   >>> 那说明这个手柄只能靠断电停止 —— 请把这一结论告诉我。")
        return 0
    finally:
        print("   收尾：再发 10 次中立报文")
        for _ in range(10):
            try:
                controller.set_rumble_payload(NEUTRAL_SIDE, NEUTRAL_SIDE)
            except Exception:                    # noqa: BLE001 - best effort
                break
            time.sleep(0.03)
        time.sleep(0.2)
        try:
            controller.close()
        except Exception:                        # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
