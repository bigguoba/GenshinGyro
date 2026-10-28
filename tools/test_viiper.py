"""Tests for the VIIPER client: frame codec and DualSense state packing.

No service and no hardware needed -- these are the pure parts that must be exactly
right, because a wrong byte here shows up as a broken controller in the game.

Run:  python tools\\test_viiper.py
"""

from __future__ import annotations

import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import viiper  # noqa: E402

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


def run_tests() -> None:
    print("\n[1] 分帧：16 字节头 + 载荷 + CRC32")
    payload = bytes(range(33))
    frame = viiper.build_frame(viiper.FRAME_INPUT_STATE, payload, sequence=0x11223344)
    check("总长度 = 16 + 载荷", len(frame), 16 + len(payload))
    check("魔数 VPCM", frame[0:4], b"VPCM")
    check("版本 5", frame[4], 0x05)
    check("帧类型 0x01（输入）", frame[5], 0x01)
    check("载荷长度（小端）", frame[6] | (frame[7] << 8), len(payload))
    check("序号（小端）", int.from_bytes(frame[8:12], "little"), 0x11223344)
    expected_crc = zlib.crc32(frame[4:12] + payload) & 0xFFFFFFFF
    check("CRC 覆盖 [4..12)+载荷，小端存放",
          int.from_bytes(frame[12:16], "little"), expected_crc)
    check("CRC 不含魔数", int.from_bytes(frame[12:16], "little") !=
          zlib.crc32(frame[0:12] + payload) & 0xFFFFFFFF, True)
    check("载荷原样跟在头后", frame[16:], payload)

    print("\n[2] 帧类型常量")
    check("输出状态 0x81", viiper.FRAME_OUTPUT_STATE, 0x81)
    check("扬声器 PCM 0x82", viiper.FRAME_SPEAKER_PCM, 0x82)
    check("原子音频触觉 0x83", viiper.FRAME_ATOMIC_AUDIO_HAPTICS, 0x83)
    check("实时触觉 0x84", viiper.FRAME_REALTIME_HAPTICS, 0x84)

    print("\n[3] 状态包：33 字节布局")
    pad = viiper.DualSensePad(log=lambda *_: None)
    pad.set_sticks(228, 28, 178, 78)          # DS4 字节，128 = 中位
    pad.set_buttons(0x0008 | 0x0020 | 0x0100)  # 中立十字键 + ✕ + L1
    pad.set_motion((1600, -3200, 4800), (0, 0, -8192))
    state = pad.pack_state()
    check("长度 33", len(state), 33)
    lx, ly, rx, ry = struct.unpack_from("<4b", state, 0)
    check("摇杆转成 -128..127", (lx, ly, rx, ry), (100, -100, 50, -50))
    buttons, dpad, l2, r2 = struct.unpack_from("<IBBB", state, 4)
    check("按键位保留（十字键位被清掉）", buttons, 0x0020 | 0x0100)
    check("十字键中立时 dpad=0", dpad, 0x00)
    check("扳机为 0", (l2, r2), (0, 0))
    gyro = struct.unpack_from("<3h", state, 21)
    check("陀螺 ×1.024（16 → 16.384 counts/dps）", gyro,
          (round(1600 * 1.024), round(-3200 * 1.024), round(4800 * 1.024)))
    accel = struct.unpack_from("<3h", state, 27)
    check("加速度按 VIIPER 的 835.07/(8192/9.80665) 缩放",
          accel, tuple(round(value * viiper._accel_scale) for value in (0, 0, -8192)))
    check("该缩放系数极接近 1（0.03% 差异）",
          abs(viiper._accel_scale - 1.0) < 0.001, True)

    print("\n[3b] 触摸板不报触摸点")
    pad = viiper.DualSensePad(log=lambda *_: None)
    state = pad.pack_state()
    check("常量是 0x80（HID bit7 = 未接触）", viiper.TOUCH_INACTIVE, 0x80)
    check("touch1 的 active 字节 = 0x80", state[15], 0x80)
    check("touch2 的 active 字节 = 0x80", state[20], 0x80)
    check("两个触摸点的坐标都是 0",
          (state[11:15], state[16:20]), (bytes(4), bytes(4)))

    print("\n[4] 十字键编码（DS4 的 hat → DualSense 位域）")
    for hat, expected in ((0, 0x01), (2, 0x08), (4, 0x02), (6, 0x04),
                          (1, 0x09), (3, 0x0A), (5, 0x06), (7, 0x05), (8, 0x00)):
        pad.set_buttons(0x0000 | hat)
        check(f"hat {hat} -> 0x{expected:02X}",
              struct.unpack_from("<B", pad.pack_state(), 8)[0], expected)

    print("\n[5] 特殊键：PS / 触控板搬到 DualSense 的高位")
    pad.set_buttons(0x0008, special=0x01)
    check("PS -> 0x10000", struct.unpack_from("<I", pad.pack_state(), 4)[0] & 0x10000,
          0x10000)
    pad.set_buttons(0x0008, special=0x02)
    check("触控板 -> 0x20000",
          struct.unpack_from("<I", pad.pack_state(), 4)[0] & 0x20000, 0x20000)
    pad.set_buttons(0x0008, special=0x00)
    check("都不按则没有高位",
          struct.unpack_from("<I", pad.pack_state(), 4)[0] & 0x30000, 0)

    print("\n[6] 限幅")
    pad.set_sticks(0, 255, 300, -300)
    check("摇杆输入被限幅", struct.unpack_from("<4b", pad.pack_state(), 0),
          (-128, 127, 127, -128))
    pad.set_triggers(-10, 300)
    check("扳机限幅到 0..255", struct.unpack_from("<BB", pad.pack_state(), 9),
          (0, 255))
    pad.set_motion((40000, -40000, 0), (0, 0, 0))
    check("陀螺限幅到 int16",
          struct.unpack_from("<2h", pad.pack_state(), 21), (32767, -32768))

    print("\n[7] pad 接口兼容（mappers 直接调用）")
    check("提供 set_buttons", callable(getattr(pad, "set_buttons", None)), True)
    check("提供 set_sticks", callable(getattr(pad, "set_sticks", None)), True)
    check("提供 set_triggers", callable(getattr(pad, "set_triggers", None)), True)
    check("提供 set_motion", callable(getattr(pad, "set_motion", None)), True)
    check("提供 set_battery", callable(getattr(pad, "set_battery", None)), True)
    check("提供 submit", callable(getattr(pad, "submit", None)), True)
    check("提供 reset", callable(getattr(pad, "reset", None)), True)
    check("提供 plug_in / unplug",
          (callable(getattr(pad, "plug_in", None)),
           callable(getattr(pad, "unplug", None))), (True, True))
    pad.report.wTimestamp = 12345           # mappers write this; must not explode
    check("接受 wTimestamp 写入", pad.report.wTimestamp, 12345)
    pad.reset()
    check("reset 后是中立态", struct.unpack_from("<4b", pad.pack_state(), 0),
          (0, 0, 0, 0))
    pad.unplug()                            # not plugged: must be a no-op
    check("未插入时 unplug 不报错", pad.plugged, False)

    print("\n[9] 实体 DS4 中继直接写 report 也能驱动虚拟 DualSense")
    import types

    import mapping

    def relayed_pad(**overrides):
        pad = viiper.DualSensePad(log=lambda *_: None)
        config = mapping.MappingConfig(motion_pacing="raw")
        for key, value in overrides.items():
            setattr(config, key, value)
        relay = mapping.DS4ToDS4Relay(controller=None, pad=pad, config=config)
        state = types.SimpleNamespace(
            sticks=(228, 28, 178, 78),
            buttons=0x0008 | 0x0020,          # 中立十字键 + ✕
            special=0x00, triggers=(10, 200), battery=8,
            gyro=(1600, -3200, 4800), accel=(0, 0, -8192),
            timestamp=1234, motion_valid=True)
        relay.push_state(state)
        return pad, relay

    pad, relay = relayed_pad()
    state = pad.pack_state()
    check("中继写进来的摇杆原样到 DualSense",
          struct.unpack_from("<4b", state, 0), (100, -100, 50, -50))
    check("按键位传过来了", struct.unpack_from("<I", state, 4)[0] & 0x0020, 0x0020)
    check("扳机传过来了", struct.unpack_from("<BB", state, 9), (10, 200))
    check("陀螺仍按 DualSense 单位缩放",
          struct.unpack_from("<3h", state, 21),
          tuple(round(value * 1.024) for value in (1600, -3200, 4800)))
    check("加速度同样缩放",
          struct.unpack_from("<3h", state, 27)[2],
          round(-8192 * viiper._accel_scale))
    check("时间戳被接受但不需要（DualSense 无时间戳）", pad.report.wTimestamp, 1234)

    pad, _ = relayed_pad(swap_face_buttons=True)
    buttons = struct.unpack_from("<I", pad.pack_state(), 4)[0]
    check("键值转换对虚拟 DualSense 生效：✕ 变成 ○",
          (bool(buttons & 0x0020), bool(buttons & 0x0040)), (False, True))

    pad, _ = relayed_pad(motion_only=True)
    state = pad.pack_state()
    check("仅体感模式：按键中性", struct.unpack_from("<I", state, 4)[0] & 0xFFF0, 0)
    check("仅体感模式：摇杆中位", struct.unpack_from("<4b", state, 0), (0, 0, 0, 0))
    check("仅体感模式：体感照旧",
          struct.unpack_from("<3h", state, 21),
          tuple(round(value * 1.024) for value in (1600, -3200, 4800)))

    print("\n[10] 两种写法不会互相干扰")
    pad = viiper.DualSensePad(log=lambda *_: None)
    pad.set_buttons(0x0008 | 0x0100)          # Switch 写法：L1
    pad.submit()
    check("set_* + submit 仍然是 L1",
          struct.unpack_from("<I", pad.pack_state(), 4)[0] & 0x0100, 0x0100)
    pad.report.wButtons = 0x0008 | 0x0200     # 中继写法：R1
    pad.submit()
    check("之后直接写 report + submit 也生效",
          struct.unpack_from("<I", pad.pack_state(), 4)[0] & 0x0300, 0x0200)

    print("\n[11] 服务不可达时报错清晰")
    server = viiper.ViiperServer(port=1, timeout=0.5)
    try:
        server.request("bus/list")
        check("应当抛 ViiperError", False, True)
    except viiper.ViiperError as exc:
        check("提示里含启动方式", "VIIPER" in str(exc), True)


def main() -> int:
    run_tests()
    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
