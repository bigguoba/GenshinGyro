"""Tests for the Switch rumble payload encoder.

The byte layout comes from dekuNukem/Nintendo_Switch_Reverse_Engineering
(rumble_data_table.md); a wrong byte here means either no vibration at all or a
pad that buzzes when it should be silent, so the invariants are pinned here.

Run:  python tools\\test_rumble.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import switchpro  # noqa: E402

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


def main() -> int:
    print("\n[1] 中立（停止）报文必须和官方一致")
    check("0 强度 = 00 01 40 40", switchpro.rumble_side(0.0), bytes((0x00, 0x01, 0x40, 0x40)))
    check("负数也算停止", switchpro.rumble_side(-1.0), bytes((0x00, 0x01, 0x40, 0x40)))
    check("与代码里既有的 NEUTRAL_RUMBLE 一致",
          switchpro.rumble_side(0.0) * 2, switchpro.NEUTRAL_RUMBLE)

    print("\n[2] 低频码：160 Hz -> 0x40（文档与 DS4Windows 测试断言一致）")
    side = switchpro.rumble_side(1.0, low_hz=160.0, high_hz=320.0)
    check("byte2 的低 7 位 = 0x40", side[2] & 0x7F, 0x40)

    print("\n[3] 强度递增时幅度单调不减（按表里的 high / 12 位低值检查）")
    previous = None
    for step in range(0, 11):
        level = step / 10.0
        high, low = switchpro._rumble_amplitude(level)
        magnitude = (high, low & 0x0FFF)
        if previous is not None:
            check(f"{level * 100:3.0f}% 的两个幅度分量都不减",
                  (magnitude[0] >= previous[0], magnitude[1] >= previous[1]),
                  (True, True))
        previous = magnitude
    check("100% 与 0% 明确不同", switchpro.rumble_side(1.0) != switchpro.rumble_side(0.0), True)
    check("最高档的幅度字节确实更大",
          switchpro.rumble_side(1.0)[1] > switchpro.rumble_side(0.2)[1], True)

    print("\n[4] 左右独立")
    left_only = switchpro.rumble_payload(1.0, 0.0)
    right_only = switchpro.rumble_payload(0.0, 1.0)
    both = switchpro.rumble_payload(1.0, 1.0)
    check("载荷总是 8 字节", len(both), 8)
    check("只左：右半是中立", left_only[4:], switchpro.rumble_side(0.0))
    check("只右：左半是中立", right_only[:4], switchpro.rumble_side(0.0))
    check("只左 != 只右", left_only != right_only, True)
    check("同时 == 两半拼接",
          both, switchpro.rumble_side(1.0) + switchpro.rumble_side(1.0))

    print("\n[5] 超范围输入被夹住")
    check("2.0 等同 1.0", switchpro.rumble_side(2.0), switchpro.rumble_side(1.0))
    check("负值等同 0", switchpro.rumble_side(-0.5), switchpro.rumble_side(0.0))

    print("\n[6] 频率参数改变时链路码随之改变")
    slow = switchpro.rumble_side(1.0, low_hz=41.0, high_hz=82.0)
    fast = switchpro.rumble_side(1.0, low_hz=626.0, high_hz=1252.0)
    check("低频码不同", slow[2] & 0x7F != fast[2] & 0x7F, True)
    check("高频码不同", slow[0] != fast[0], True)
    # 41 Hz 是文档下界，对应码 0x41，所以 byte2 的低 7 位是 0x01（160 Hz 时才是 0x40）
    check("低于下界被夹到 41 Hz（码 0x41 -> 低 7 位 0x01）",
          switchpro.rumble_side(1.0, low_hz=1.0)[2] & 0x7F, 0x01)
    check("与显式 41 Hz 完全相同",
          switchpro.rumble_side(1.0, low_hz=1.0), switchpro.rumble_side(1.0, low_hz=41.0))
    check("高于上界被夹到 1252 Hz",
          switchpro.rumble_side(1.0, high_hz=99999.0),
          switchpro.rumble_side(1.0, high_hz=1252.0))

    print("\n[7] 频段可以单独驱动（隔离测试用）")
    low_only = switchpro.rumble_side_bands(1.0, 160.0, 0.0, 320.0)
    high_only = switchpro.rumble_side_bands(0.0, 160.0, 1.0, 320.0)
    check("仅低频：高频幅度字节为静音值", low_only[1], 0x01)
    check("仅低频：低频幅度字节非静音", low_only[3], switchpro.rumble_side(1.0)[3])
    check("仅高频：低频幅度字节为静音值", high_only[2:4], bytes((0x40, 0x40)))
    check("仅高频：高频幅度字节非静音", high_only[1], switchpro.rumble_side(1.0)[1])
    check("只低频 != 只高频", low_only != high_only, True)
    check("两个频段都给 = 普通 rumble_side",
          switchpro.rumble_side_bands(0.6, 160.0, 0.6, 320.0),
          switchpro.rumble_side(0.6))
    check("两段都静音 = 中立报文", switchpro.rumble_side_bands(0.0, 160.0, 0.0, 320.0),
          bytes((0x00, 0x01, 0x40, 0x40)))

    print("\n[8] 频率扫频时只有频率字节变化，幅度字节不动")
    slow_low = switchpro.rumble_side_bands(1.0, 41.0, 0.0, 320.0)
    fast_low = switchpro.rumble_side_bands(1.0, 626.0, 0.0, 320.0)
    check("低频段：byte2 变化", slow_low[2] != fast_low[2], True)
    check("低频段：幅度字节不变", (slow_low[1], slow_low[3]), (fast_low[1], fast_low[3]))
    slow_high = switchpro.rumble_side_bands(0.0, 160.0, 1.0, 82.0)
    fast_high = switchpro.rumble_side_bands(0.0, 160.0, 1.0, 1253.0)
    check("高频段：byte0 变化", slow_high[0] != fast_high[0], True)
    # byte1 的低位是频率码的第 9 位（1253 Hz 时为 1），幅度在 bit1..6
    check("高频段：幅度位（bit1..6）不变",
          slow_high[1] & 0x7E, fast_high[1] & 0x7E)
    check("高频段：第 9 位确实来自频率码",
          (slow_high[1] & 0x01, fast_high[1] & 0x01), (0, 1))

    print("\n[9] 停止载荷：必须是全零（这个手柄把 0x40 当成 25% 驱动）")
    check("RUMBLE_SILENCE 是 8 个零字节", switchpro.RUMBLE_SILENCE, bytes(8))
    check("停止载荷与文档中立不同（后者停不下这类手柄）",
          switchpro.RUMBLE_SILENCE != switchpro.NEUTRAL_RUMBLE, True)
    check("逐字节载荷：只动 byte3", switchpro.rumble_side_raw(0x80),
          bytes((0x00, 0x01, 0x40, 0x80)))
    check("逐字节载荷：只动 byte2", switchpro.rumble_side_raw(0, frequency_byte=0x80),
          bytes((0x00, 0x01, 0x80, 0x00)))
    check("逐字节载荷：只动 byte1", switchpro.rumble_side_raw(0, high_byte=0x80,
                                                              frequency_byte=0x00),
          bytes((0x00, 0x80, 0x00, 0x00)))
    check("全零载荷就是 4 个零字节",
          switchpro.rumble_side_raw(0, high_byte=0, frequency_byte=0), bytes(4))

    print("\n[10] 原始驱动编码（这个手柄真正认的那套：byte2 = 0..255）")
    check("0% -> 全零（与停止载荷相同）",
          switchpro.rumble_side_level(0.0), bytes(4))
    check("停止载荷 == 0% 载荷",
          switchpro.rumble_payload_level(0.0, 0.0), switchpro.RUMBLE_SILENCE)
    check("100% -> byte2 = 0xFF",
          switchpro.rumble_side_level(1.0), bytes((0x00, 0x00, 0xFF, 0x00)))
    check("只有 byte2 承载强度",
          [switchpro.rumble_side_level(0.5)[i] for i in (0, 1, 3)], [0, 0, 0])
    previous = -1
    monotone = True
    for step in range(0, 21):
        drive = switchpro.rumble_side_level(step / 20.0)[2]
        if drive < previous:
            monotone = False
        previous = drive
    check("byte2 随强度单调不减", monotone, True)
    check("满量程用满 0..255",
          (switchpro.rumble_side_level(0.5)[2], switchpro.rumble_side_level(1.0)[2]),
          (128, 255))
    check("左右独立：只左",
          switchpro.rumble_payload_level(1.0, 0.0),
          bytes((0x00, 0x00, 0xFF, 0x00)) + bytes(4))
    check("左右独立：只右",
          switchpro.rumble_payload_level(0.0, 1.0),
          bytes(4) + bytes((0x00, 0x00, 0xFF, 0x00)))

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
