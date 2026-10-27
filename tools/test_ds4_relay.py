"""Tests for DualShock 4 input parsing and the 1:1 relay to a virtual DS4.

The important case: a Bluetooth pad sends report id 0x11 with a two-byte header
before the state; the virtual pad must emit the wired 0x01 report with the same
state values, because that is the only format games like Genshin Impact read.

Run:  python tools\\test_ds4_relay.py
"""

from __future__ import annotations

import math
import os
import struct
import sys
import threading
import zlib
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dualshock4  # noqa: E402
import hidwin  # noqa: E402
import mapping  # noqa: E402
import vigem  # noqa: E402

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


# --------------------------------------------------------------------------- #
# Synthetic reports
# --------------------------------------------------------------------------- #

def make_state(lx=0x80, ly=0x80, rx=0x80, ry=0x80, buttons=0x0008, special=0,
               l2=0, r2=0, timestamp=0x1234, gyro=(0, 0, 0), accel=(0, 8192, 0),
               battery=0x1B, length=63) -> bytes:
    state = bytearray(length)
    state[0:4] = bytes((lx, ly, rx, ry))
    struct.pack_into("<H", state, 4, buttons)
    state[6] = special
    state[7] = l2
    state[8] = r2
    struct.pack_into("<H", state, 9, timestamp)
    struct.pack_into("<3h", state, 12, *gyro)
    struct.pack_into("<3h", state, 18, *accel)
    state[29] = battery
    return bytes(state)


def wired_report(**kwargs) -> bytes:
    return bytes((dualshock4.REPORT_WIRED,)) + make_state(**kwargs)


def bluetooth_report(**kwargs) -> bytes:
    # 0x11 + two header bytes, then the same state payload
    return bytes((dualshock4.REPORT_BLUETOOTH, 0xC0, 0x00)) + make_state(**kwargs)


def main() -> int:
    print("DualShock 4 输入解析 + 1:1 中继测试")

    # -- 1. parsing ------------------------------------------------------- #
    print("\n[1] 报文解析：有线 0x01 / 蓝牙 0x11")
    payload = make_state(lx=0x11, ly=0x22, rx=0x33, ry=0x44, buttons=0x0408,
                         special=0x02, l2=0x30, r2=0x40, timestamp=0xABCD,
                         gyro=(100, -200, 300), accel=(-1000, 8192, 500),
                         battery=0x1A)

    state = dualshock4.parse_report(bytes((0x01,)) + payload)
    check("有线：摇杆", state.sticks, (0x11, 0x22, 0x33, 0x44))
    check("有线：按键", state.buttons, 0x0408)
    check("有线：特殊/扳机", (state.special, state.triggers), (0x02, (0x30, 0x40)))
    check("有线：时间戳", state.timestamp, 0xABCD)
    check("有线：陀螺仪", state.gyro, (100, -200, 300))
    check("有线：加速度计", state.accel, (-1000, 8192, 500))
    check("有线：电量", (state.battery_level, state.charging), (10, True))
    check("有线：判定为有线", state.wired, True)

    state = dualshock4.parse_report(bytes((0x11, 0xC0, 0x00)) + payload)
    check("蓝牙：摇杆", state.sticks, (0x11, 0x22, 0x33, 0x44))
    check("蓝牙：按键", state.buttons, 0x0408)
    check("蓝牙：陀螺仪", state.gyro, (100, -200, 300))
    check("蓝牙：加速度计", state.accel, (-1000, 8192, 500))
    check("蓝牙：时间戳", state.timestamp, 0xABCD)
    check("蓝牙：判定为非有线", state.wired, False)

    # a wrong offset must NOT silently produce plausible-looking data
    wrong = dualshock4.parse_report(bytes((0x11, 0xC0, 0x00)) + payload, offset=2)
    check("错误偏移会读出不同的摇杆（说明偏移很关键）",
          wrong.sticks != state.sticks, True)

    check("非 DS4 报文返回 None", dualshock4.parse_report(b"\x30" + bytes(62)), None)
    check("过短的报文返回 None", dualshock4.parse_report(b"\x01\x00"), None)

    # -- 2. offset detection ---------------------------------------------- #
    print("\n[2] 状态偏移自动判定（按报文 ID 分别判定）")
    bluetooth_reports = [
        bluetooth_report(lx=0x80 + offset, accel=(0, 8192, 0), timestamp=0x1000 + offset)
        for offset in range(6)
    ]
    offsets, scores = dualshock4.detect_offsets(bluetooth_reports)
    check("蓝牙报文 -> 0x11 选中偏移 3", offsets.get(dualshock4.REPORT_BLUETOOTH), 3)
    check("没有 0x01 报文就不改 0x01 的判定", offsets.get(dualshock4.REPORT_WIRED), None)
    check("两个候选都被评估", sorted(scores), ["0x11@2", "0x11@3"])

    wired_reports = [wired_report(timestamp=0x2000 + offset) for offset in range(6)]
    offsets, _scores = dualshock4.detect_offsets(wired_reports)
    check("有线报文 -> 0x01 选中偏移 1", offsets.get(dualshock4.REPORT_WIRED), 1)

    # magnitude of gravity identifies the layout even when the pad is tilted
    tilted = [bluetooth_report(accel=(-4000, 3000, 6000), timestamp=0x3000 + offset)
              for offset in range(6)]
    check("倾斜放置也能判对",
          dualshock4.detect_offsets(tilted)[0].get(dualshock4.REPORT_BLUETOOTH), 3)

    # Regression: a pad that is asleep at start-up answers with a cached 0x01
    # report.  Detection must not let that decide how a later live 0x11 report
    # is parsed, or every Bluetooth state report would be read shifted.
    pad_stub = dualshock4.DualShock4(log=lambda *_: None)
    found, _ = dualshock4.detect_offsets([wired_report()] * 6)   # only 0x01 seen
    pad_stub._offsets.update(found)
    check("启动时只看得到 0x01（手柄休眠）也不影响 0x11 的偏移",
          pad_stub.offset_for(dualshock4.REPORT_BLUETOOTH), 3)
    check("0x01 仍为偏移 1", pad_stub.offset_for(dualshock4.REPORT_WIRED), 1)
    check("没见过的报文 ID 有兜底", pad_stub.offset_for(0x21), 1)

    # -- 3. relay through a real virtual pad ------------------------------ #
    print("\n[3] 中继到虚拟 DS4（关键：蓝牙输入 -> 有线输出）")
    before = {i["path"] for i in hidwin.enumerate_hid()}
    with vigem.ViGEmClient() as client:
        pad = client.create_ds4()
        pad.plug_in()
        try:
            info = None
            deadline = time.time() + 8
            while time.time() < deadline and info is None:
                for candidate in hidwin.enumerate_hid():
                    if candidate["path"] not in before and candidate.get("vid") == 0x054C \
                            and (candidate.get("input_len") or 0) == 64:
                        info = candidate
                        break
                time.sleep(0.2)
            if info is None:
                print("  FAIL: virtual pad did not appear")
                return 1

            def read_back():
                with hidwin.HidDevice(info["path"], info).open() as device:
                    device.drain(max_reports=32, timeout_ms=0)
                    deadline = time.time() + 2
                    while time.time() < deadline:
                        data = device.read(timeout_ms=300)
                        if data:
                            return data
                return None

            # Sections 3-6 verify the plain field-for-field copy, so smoothing is
            # switched off here; section 8 covers the smoothing path itself.
            relay = mapping.DS4ToDS4Relay(None, pad)
            relay.config.motion_pacing = "raw"
            for label, report in (("有线输入", wired_report(
                                        lx=0x10, ly=0x20, rx=0x30, ry=0x40, buttons=0x0A08,
                                        special=0x01, l2=0x11, r2=0x22, timestamp=0x5555,
                                        gyro=(1000, -2000, 3000), accel=(-4000, 5000, -6000),
                                        battery=0x1A)),
                                  ("蓝牙输入", bluetooth_report(
                                        lx=0x10, ly=0x20, rx=0x30, ry=0x40, buttons=0x0A08,
                                        special=0x01, l2=0x11, r2=0x22, timestamp=0x6666,
                                        gyro=(1000, -2000, 3000), accel=(-4000, 5000, -6000),
                                        battery=0x1A))):
                parsed = dualshock4.parse_report(report)
                relay.push_state(parsed)
                time.sleep(0.15)
                got = read_back()
                if got is None:
                    print(f"  FAIL: no report read back for {label}")
                    return 1
                check(f"{label} -> 虚拟手柄输出的是有线报文 0x01", got[0], 0x01)
                virtual = got[1:]
                check(f"{label}：摇杆一致", virtual[0:4], parsed.sticks and
                      bytes(parsed.sticks))
                check(f"{label}：按键一致", struct.unpack_from("<H", virtual, 4)[0],
                      parsed.buttons)
                check(f"{label}：特殊键一致", virtual[6], parsed.special)
                check(f"{label}：扳机一致", (virtual[7], virtual[8]), parsed.triggers)
                check(f"{label}：时间戳原样转发", struct.unpack_from("<H", virtual, 9)[0],
                      parsed.timestamp)
                check(f"{label}：陀螺仪一致（未做任何换算）",
                      struct.unpack_from("<3h", virtual, 12), parsed.gyro)
                check(f"{label}：加速度计一致", struct.unpack_from("<3h", virtual, 18),
                      parsed.accel)
                check(f"{label}：电量一致", virtual[29], parsed.battery)

            # -- 4. frozen timestamp falls back to a synthesised one -------- #
            print("\n[4] 时间戳不前进时自动改用合成时间戳")
            frozen = [bluetooth_report(timestamp=0x7777) for _ in range(9)]
            stamps = []
            for report in frozen:
                relay.push_state(dualshock4.parse_report(report))
                time.sleep(0.01)
                got = read_back()
                stamps.append(struct.unpack_from("<H", got[1:], 9)[0])
            check("前几条仍原样转发", stamps[0], 0x7777)
            check("之后自动开始递增", len(set(stamps[-4:])) > 1, True)

            # -- 5. minimal Bluetooth report relays no phantom motion ------- #
            print("\n[5] 最小蓝牙报文（无体感）中继时不发出假体感")
            minimal = bytes((0x01, 0x11, 0x22, 0x33, 0x44, 0x08, 0x00, 0x00, 0x00, 0x00)) \
                + bytes(537)
            minimal_state = dualshock4.parse_report(minimal, bluetooth_minimal=True)
            check("最小报文：摇杆仍然正确", minimal_state.sticks, (0x11, 0x22, 0x33, 0x44))
            check("最小报文：按键仍然正确", minimal_state.buttons, 0x0008)
            check("最小报文：标记为没有体感数据", minimal_state.motion_valid, False)
            relay.push_state(minimal_state)
            time.sleep(0.15)
            virtual = read_back()[1:]
            check("最小报文：转发后体感为零", (
                struct.unpack_from("<3h", virtual, 12), struct.unpack_from("<3h", virtual, 18)),
                ((0, 0, 0), (0, 0, 0)))
            check("最小报文：按键/摇杆照常转发",
                  (virtual[0:4], struct.unpack_from("<H", virtual, 4)[0]),
                  (bytes((0x11, 0x22, 0x33, 0x44)), 0x0008))

            # -- 6. motion-only mode ---------------------------------------- #
            print("\n[6] 仅体感模式：按键/摇杆保持中立，体感照常转发")
            relay.config.motion_only = True
            relay.push_state(dualshock4.parse_report(wired_report(
                lx=0x10, ly=0x20, rx=0x30, ry=0x40, buttons=0x0A08, special=0x01,
                l2=0x11, r2=0x22, timestamp=0x1234, gyro=(1000, -2000, 3000),
                accel=(-4000, 5000, -6000))))
            time.sleep(0.15)
            virtual = read_back()[1:]
            check("仅体感：体感仍然转发",
                  (struct.unpack_from("<3h", virtual, 12), struct.unpack_from("<3h", virtual, 18)),
                  ((1000, -2000, 3000), (-4000, 5000, -6000)))
            check("仅体感：摇杆居中", virtual[0:4], bytes((0x80, 0x80, 0x80, 0x80)))
            check("仅体感：按键全松（只有十字键中立位）",
                  struct.unpack_from("<H", virtual, 4)[0], 0x0008)
            check("仅体感：特殊键与扳机为 0", (virtual[6], virtual[7], virtual[8]),
                  (0, 0, 0))

            # Share doubles as the touchpad click in this mode (some pads have an
            # unreliable touchpad button over Bluetooth); nothing else may leak.
            relay.config.share_as_touchpad = True
            relay.push_state(dualshock4.parse_report(wired_report(
                lx=0x10, ly=0x20, rx=0x30, ry=0x40, buttons=0x0A08 | (1 << 12),
                special=0x01, l2=0x11, r2=0x22, gyro=(1000, -2000, 3000),
                accel=(-4000, 5000, -6000))))
            time.sleep(0.15)
            virtual = read_back()[1:]
            check("仅体感：按 Share -> 触控板按下位被置起",
                  virtual[6] & mapping.TOUCHPAD_MASK, mapping.TOUCHPAD_MASK)
            check("仅体感：PS 位不会被带出来（实体手柄的 PS 不转发）",
                  virtual[6] & 0x01, 0)
            check("仅体感：Share 本身不转发到按键位",
                  struct.unpack_from("<H", virtual, 4)[0], 0x0008)
            check("仅体感：摇杆仍居中", virtual[0:4], bytes((0x80, 0x80, 0x80, 0x80)))
            check("仅体感：体感照旧",
                  struct.unpack_from("<3h", virtual, 12), (1000, -2000, 3000))

            # …and the mapping must be switchable off.
            relay.config.share_as_touchpad = False
            relay.push_state(dualshock4.parse_report(wired_report(buttons=0x0A08 | (1 << 12))))
            time.sleep(0.15)
            check("仅体感：关掉开关后 Share 不再映射",
                  read_back()[1:][6], 0)
            relay.config.motion_only = False

            # -- 7b. Share mapping in full relay ---------------------------- #
            # The same switch must work while everything is relayed 1:1: Share is
            # moved to the touchpad bit and no longer reported as Share itself.
            print("\n[7b] 整机转发时也可以把 Share 映射成触控板")
            relay.config.share_as_touchpad = False
            relay.push_state(dualshock4.parse_report(wired_report(
                lx=0x11, ly=0x22, rx=0x33, ry=0x44, buttons=0x0A08 | (1 << 12),
                special=0x01, l2=0x55, r2=0x66)))
            time.sleep(0.15)
            virtual = read_back()[1:]
            check("关闭时不映射：Share 仍在按键位",
                  struct.unpack_from("<H", virtual, 4)[0] & (1 << 12), 1 << 12)
            check("关闭时不映射：触控板位为 0", virtual[6] & mapping.TOUCHPAD_MASK, 0)
            check("关闭时其它按键照旧转发",
                  struct.unpack_from("<H", virtual, 4)[0], 0x0A08 | (1 << 12))

            relay.config.share_as_touchpad = True
            relay.push_state(dualshock4.parse_report(wired_report(
                lx=0x11, ly=0x22, rx=0x33, ry=0x44, buttons=0x0A08 | (1 << 12),
                special=0x01, l2=0x55, r2=0x66)))
            time.sleep(0.15)
            virtual = read_back()[1:]
            check("开启后：触控板位被置起",
                  virtual[6] & mapping.TOUCHPAD_MASK, mapping.TOUCHPAD_MASK)
            check("开启后：Share 位被移走（不是重复）",
                  struct.unpack_from("<H", virtual, 4)[0] & (1 << 12), 0)
            check("开启后：其它按键不受影响",
                  struct.unpack_from("<H", virtual, 4)[0], 0x0A08)
            check("开启后：PS 位等其它特殊位保留", virtual[6] & 0x01, 0x01)
            check("开启后：摇杆/扳机照旧 1:1",
                  (virtual[0:4], virtual[7], virtual[8]),
                  (bytes((0x11, 0x22, 0x33, 0x44)), 0x55, 0x66))

            # Releasing Share must clear the touchpad bit again.
            relay.push_state(dualshock4.parse_report(wired_report(buttons=0x0A08)))
            time.sleep(0.15)
            check("松开 Share 后触控板位归零", read_back()[1:][6] & mapping.TOUCHPAD_MASK, 0)
            relay.config.share_as_touchpad = False

            # -- 8. motion smoothing ---------------------------------------- #
            # The host polls the emulated pad every 5.33 ms (187.5 Hz) and gets the
            # last report each time, while a Bluetooth pad delivers its ~125
            # samples/s in bursts (two samples ~8 ms apart, together, every
            # ~16 ms).  A plain copy therefore repeats a sample on a third of the
            # polls -- the staircase.  Rendering on a steady cadence with
            # interpolation in the pad's own timestamp domain must raise the number
            # of distinct samples and cut those repeats.
            print("\n[8] 运动平滑：突发式 125 Hz 输入 -> 更平滑的高频输出")

            def burst_run(smoothing: bool):
                """Feed bursty samples; measure what the virtual pad delivers."""
                relay.config.motion_pacing = ("smooth" if smoothing else "raw")
                relay.config.motion_only = False
                relay.reset()
                stop_feed = threading.Event()
                failure = {"error": None}

                def feeder() -> None:
                    index = 0
                    next_at = time.perf_counter()
                    while not stop_feed.is_set():
                        for _ in range(2):      # one burst: two samples 8 ms apart
                            parsed = dualshock4.parse_report(wired_report(
                                gyro=(index * 32, 0, 0),
                                timestamp=(0x1000 + index * 1500) & 0xFFFF))
                            try:
                                if smoothing:
                                    relay.feed(parsed)
                                else:
                                    relay.push_state(parsed)
                            except Exception as exc:   # pragma: no cover - defensive
                                failure["error"] = repr(exc)
                                return
                            index += 1
                        next_at += 0.016
                        time.sleep(max(0.0, next_at - time.perf_counter()))

                feed_thread = threading.Thread(target=feeder, daemon=True)
                feed_thread.start()
                if smoothing:
                    relay.start_streaming()
                time.sleep(0.3)                 # let the interpolation buffer fill

                samples = []
                with hidwin.HidDevice(info["path"], info).open() as device:
                    device.drain(max_reports=64)
                    started = time.perf_counter()
                    next_read = started
                    while time.perf_counter() - started < 1.0:
                        report = device.read(timeout_ms=50)
                        if report and report[0] == 0x01:
                            samples.append(struct.unpack_from("<3h", report[1:], 12)[0])
                        # Precise pacing: time.sleep alone is bursty on Windows
                        # (15.6 ms granularity) and would fake extra repeats.
                        next_read += 1.0 / 187.5
                        while True:
                            remaining = next_read - time.perf_counter()
                            if remaining <= 0:
                                break
                            if remaining > 0.0006:
                                time.sleep(remaining - 0.0004)
                            else:
                                while time.perf_counter() < next_read:
                                    pass
                span = time.perf_counter() - started
                stop_feed.set()
                feed_thread.join(timeout=1.0)
                if smoothing:
                    relay.stop_streaming()
                if failure["error"]:
                    print(f"       feeder error: {failure['error']}")
                return samples, span

            plain, plain_span = burst_run(smoothing=False)
            smoothed, smooth_span = burst_run(smoothing=True)

            def repeats(values) -> float:
                if len(values) < 2:
                    return 1.0
                same = sum(1 for a, b in zip(values, values[1:]) if a == b)
                return same / (len(values) - 1)

            plain_dup, smooth_dup = repeats(plain), repeats(smoothed)
            plain_distinct = len(set(plain)) / max(0.001, plain_span)
            smooth_distinct = len(set(smoothed)) / max(0.001, smooth_span)
            print(f"       未平滑：{len(plain)} 条/{plain_span:.2f}s，"
                  f"不同样本 {plain_distinct:.0f}/s，重复 {plain_dup * 100:.0f}%")
            print(f"       已平滑：{len(smoothed)} 条/{smooth_span:.2f}s，"
                  f"不同样本 {smooth_distinct:.0f}/s，重复 {smooth_dup * 100:.0f}%")

            check("平滑后不同样本数明显增加（≥ 未平滑的 1.25 倍）",
                  smooth_distinct >= plain_distinct * 1.25, True)
            check("平滑后相邻重复降到原来的三分之一以下",
                  smooth_dup < plain_dup / 3.0, True)
            # Delivering a pure increasing counter through this path already
            # repeats ~12%, so that is a floor, not a defect of the interpolation.
            check("平滑后重复率低于 25%（交付通道本身约 12%）", smooth_dup < 0.25, True)
            check("平滑后数值仍在变化（不是冻结）", len(set(smoothed)) > 50, True)

            # A monotone ramp must stay monotone apart from the small corrections
            # that bounded extrapolation makes when the next burst arrives.
            steps = [b - a for a, b in zip(smoothed, smoothed[1:])]
            worst_back = min(steps) if steps else 0
            check("回退幅度不超过一个样本步（±40 counts 以内）", worst_back >= -40, True)

            # A monotone ramp has a constant slope, so guessing across a gap never
            # overshoots.  Real motion changes rate, and then the guess has to be
            # taken back -- that retraction is the little spike on a waveform.
            # Demonstrated deterministically here, with no device or threads:
            # a rising pair, then a gap, then a burst that has dropped back down.
            print("\n[9] 毛刺的成因：外推在真实数据到达时被迫回头")

            def probe(extrapolate: bool):
                resampler = mapping.MotionResampler(extrapolate=extrapolate)

                def burst(at: float, index: int, value: int) -> None:
                    """Two samples 8 ms apart in pad time, arriving together."""
                    resampler.add(at, (0x1000 + index * 1500) & 0xFFFF,
                                  (value, 0, 0), (0, 8192, 0))
                    resampler.add(at + 0.0001, (0x1000 + (index + 1) * 1500) & 0xFFFF,
                                  (value + 32, 0, 0), (0, 8192, 0))

                burst(0.0000, 0, 0)          # rising motion
                burst(0.0160, 2, 1000)
                burst(0.0320, 4, 2000)       # real samples so far: 0 … 2032
                when = resampler.clock_at(0.0420) - resampler.latency_units
                guessed = resampler.sample_at(when)[0][0]
                burst(0.0480, 6, 800)        # the pad reveals it actually slowed
                when = resampler.clock_at(0.0482) - resampler.latency_units
                after = resampler.sample_at(when)[0][0]
                return guessed, after

            guess_extrap, after_extrap = probe(extrapolate=True)
            guess_plain, after_plain = probe(extrapolate=False)
            print(f"       外推  ：空档里报到 {guess_extrap:.0f}，"
                  f"真实数据到达后回落到 {after_extrap:.0f}")
            print(f"       纯插值：空档里报到 {guess_plain:.0f}，"
                  f"之后为 {after_plain:.0f}（真实样本范围 0–2032）")
            check("外推会冲过真实样本范围（这就是毛刺的上升段）",
                  guess_extrap > 2032, True)
            check("外推随后被迫回头（这就是毛刺的下降段）", after_extrap < guess_extrap, True)
            check("纯插值从不越过真实样本范围", 0 <= guess_plain <= 2032, True)
            check("纯插值不出现回头修正", after_plain >= guess_plain, True)

            # -- 10. hardware pacing ---------------------------------------- #
            # What a real wired pad does, and what a game's per-report yaw
            # integration assumes: a report every 4.096 ms whose timestamp
            # advances by exactly 768, carrying the newest IMU sample unchanged --
            # so with a 125 Hz input the motion values legitimately repeat.
            print("\n[10] 硬件节奏：4.096 ms 一条、时间戳每条 +768、样本原样保持")
            relay.config.motion_pacing = "hardware"
            relay.config.motion_only = False
            relay.reset()
            stop_feed = threading.Event()

            def hardware_feeder() -> None:
                index = 0
                next_at = time.perf_counter()
                while not stop_feed.is_set():
                    relay.feed(dualshock4.parse_report(wired_report(
                        gyro=(index * 100, 0, 0),
                        timestamp=(0x1000 + index * 1500) & 0xFFFF)))
                    index += 1
                    next_at += 0.008            # 125 Hz real samples
                    time.sleep(max(0.0, next_at - time.perf_counter()))

            feed_thread = threading.Thread(target=hardware_feeder, daemon=True)
            feed_thread.start()
            relay.start_streaming()
            time.sleep(0.15)
            before_submitted = relay.submitted
            collected = []
            with hidwin.HidDevice(info["path"], info).open() as device:
                device.drain(max_reports=64)
                started = time.perf_counter()
                next_read = started
                while time.perf_counter() - started < 0.7:
                    report = device.read(timeout_ms=50)
                    if report and report[0] == 0x01:
                        state = dualshock4.parse_report(report, offset=1)
                        collected.append((state.timestamp, state.gyro[0]))
                    next_read += 1.0 / 244.0
                    while True:
                        remaining = next_read - time.perf_counter()
                        if remaining <= 0:
                            break
                        if remaining > 0.0006:
                            time.sleep(remaining - 0.0004)
                        else:
                            while time.perf_counter() < next_read:
                                pass
            span = time.perf_counter() - started
            written = relay.submitted - before_submitted
            stop_feed.set()
            feed_thread.join(timeout=1.0)
            relay.stop_streaming()

            stamps = [stamp for stamp, _value in collected]
            steps = {(b - a) % 65536 for a, b in zip(stamps, stamps[1:])}
            values = [value for _stamp, value in collected]
            print(f"       写入 {written} 条 / {span:.2f}s = {written / span:.0f}/s；"
                  f"时间戳步长集合 {sorted(steps)[:6]}；"
                  f"唯一值 {len(set(values))} 个")
            check("写入节奏 ≈ 244 条/秒（真机 4.096 ms 间隔）",
                  200 <= written / span <= 290, True)
            nonzero = {step for step in steps if step}
            check("时间戳每条固定 +768（真机步长；重复读取会产生 0 步）",
                  nonzero == {768}, True)
            check("写入的样本全部来自真实样本（原样、未插值）",
                  all(value % 100 == 0 for value in values), True)
            check("125 Hz 输入 -> 样本成对重复（唯一值远少于报文数）",
                  len(set(values)) < len(values) * 0.65, True)

            # A custom interval must carry the timestamp step with it, so a dt
            # derived from the timestamp always agrees with the report cadence.
            relay.config.ds4_report_interval_s = 0.005
            stop_feed2 = threading.Event()

            def feeder2() -> None:
                index = 0
                next_at = time.perf_counter()
                while not stop_feed2.is_set():
                    relay.feed(dualshock4.parse_report(wired_report(
                        gyro=(index * 100, 0, 0),
                        timestamp=(0x1000 + index * 1500) & 0xFFFF)))
                    index += 1
                    next_at += 0.008
                    time.sleep(max(0.0, next_at - time.perf_counter()))

            thread2 = threading.Thread(target=feeder2, daemon=True)
            thread2.start()
            relay.start_streaming()
            time.sleep(0.15)
            before_submitted = relay.submitted
            custom_stamps = []
            with hidwin.HidDevice(info["path"], info).open() as device:
                device.drain(max_reports=64)
                started = time.perf_counter()
                next_read = started
                while time.perf_counter() - started < 0.4:
                    report = device.read(timeout_ms=50)
                    if report and report[0] == 0x01:
                        custom_stamps.append(struct.unpack_from("<H", report[1:], 9)[0])
                    next_read += 1.0 / 200.0
                    while True:
                        remaining = next_read - time.perf_counter()
                        if remaining <= 0:
                            break
                        if remaining > 0.0006:
                            time.sleep(remaining - 0.0004)
                        else:
                            while time.perf_counter() < next_read:
                                pass
            custom_span = time.perf_counter() - started
            written = relay.submitted - before_submitted
            stop_feed2.set()
            thread2.join(timeout=1.0)
            relay.stop_streaming()
            custom_steps = {(b - a) % 65536
                            for a, b in zip(custom_stamps, custom_stamps[1:])}
            custom_steps.discard(0)
            print(f"       改用 5 ms 间隔：写入 {written / custom_span:.0f}/s，"
                  f"时间戳步长 {sorted(custom_steps)[:3]}")
            check("自定义 5 ms 间隔 -> 写入约 200 条/秒",
                  150 <= written / custom_span <= 240, True)
            check("时间戳步长随间隔变为 938（5ms × 187500）",
                  custom_steps <= {938, 1876}, True)
            relay.config.ds4_report_interval_s = mapping.DS4_NATIVE_REPORT_INTERVAL_S

            # -- 11. gyro sensitivity --------------------------------------- #
            # The GUI's sensitivity slider scales the rotation rate the game sees;
            # gravity must stay 1 g so the tilt reference games use is untouched.
            print("\n[11] 体感灵敏度：只缩放角速度，加速度不受影响")
            relay.config.motion_pacing = "raw"
            for gain, expected in ((2.0, (2000, -4000, 6000)),
                                   (0.5, (500, -1000, 1500)),
                                   (1.0, (1000, -2000, 3000))):
                relay.config.gyro_gain = gain
                relay.push_state(dualshock4.parse_report(wired_report(
                    gyro=(1000, -2000, 3000), accel=(-4000, 5000, -6000))))
                time.sleep(0.12)
                virtual = read_back()[1:]
                check(f"增益 ×{gain:g} -> 角速度 {expected}",
                      struct.unpack_from("<3h", virtual, 12), expected)
                check(f"增益 ×{gain:g} 时加速度仍是原值",
                      struct.unpack_from("<3h", virtual, 18), (-4000, 5000, -6000))
            relay.config.gyro_gain = 1.0

            # -- 11b. deadzone compensation ---------------------------------- #
            # A small constant is added in the direction of motion, per axis, so the
            # game's own deadzone no longer swallows slow rotations.  Below the gate
            # nothing is added, so a pad lying still cannot drift.
            print("\n[11b] 死区补偿：按轴向叠加、方向跟随真实转动")
            relay.config.motion_deadzone_dps = (2.0, 0.0, 0.0)      # pitch only
            relay.push_state(dualshock4.parse_report(wired_report(
                gyro=(100, -200, 300), accel=(-4000, 5000, -6000))))
            time.sleep(0.15)
            check("俯仰 +2°/s：正向叠加 32 counts",
                  read_back()[1:][12:14], struct.pack("<h", 132))
            check("其它两轴不受影响",
                  struct.unpack_from("<2h", read_back()[1:], 14), (-200, 300))

            relay.push_state(dualshock4.parse_report(wired_report(gyro=(-100, 200, -300))))
            time.sleep(0.15)
            check("反向转动时补偿也反向（不是固定加在正方向）",
                  read_back()[1:][12:14], struct.pack("<h", -132))

            relay.push_state(dualshock4.parse_report(wired_report(gyro=(5, 5, 5))))
            time.sleep(0.15)
            check("低于门限（0.5°/s = 8 counts）不补偿，静止不会漂",
                  struct.unpack_from("<3h", read_back()[1:], 12), (5, 5, 5))

            relay.config.motion_deadzone_dps = (1.0, 2.0, 4.0)
            relay.push_state(dualshock4.parse_report(wired_report(gyro=(100, 100, 100))))
            time.sleep(0.15)
            check("三轴独立：各自加上自己的补偿",
                  struct.unpack_from("<3h", read_back()[1:], 12),
                  (100 + 16, 100 + 32, 100 + 64))

            relay.config.motion_deadzone_dps = (0.0, 0.0, 0.0)
            relay.push_state(dualshock4.parse_report(wired_report(gyro=(100, 100, 100))))
            time.sleep(0.15)
            check("全部为 0 时完全不干预",
                  struct.unpack_from("<3h", read_back()[1:], 12), (100, 100, 100))

            relay.config.motion_pacing = "raw"
            pad.unplug()
        finally:
            pad.unplug()

    # -- 5. Bluetooth report mode (request + minimal detection) ------------ #
    print("\n[6] 蓝牙报文模式：扩展报文请求")
    report = dualshock4.build_extended_mode_report()
    check("扩展模式请求长度 78", len(report), 78)
    check("请求报文 ID 0x11", report[0], 0x11)
    check("hw_control 同时要求 HID + CRC + 4ms",
          report[1], dualshock4.HWCTL_HID | dualshock4.HWCTL_CRC32 | 4)
    expected_crc = zlib.crc32(bytes((dualshock4.OUTPUT_CRC32_SEED,)) + report[:74]) & 0xFFFFFFFF
    check("CRC32 正确（种子 0xA2）", struct.unpack_from("<I", report, 74)[0], expected_crc)
    check("带上默认灯光颜色", report[8:11], bytes(dualshock4.DEFAULT_LIGHTBAR))

    print("\n[7] 退回最小报文时自动重新请求扩展报文")
    stub = dualshock4.DualShock4(log=lambda *_: None)
    stub.transport = "bluetooth"
    stub.motion_mode = "minimal"
    stub.static_count = 0
    calls: list[int] = []

    def fake_request() -> bool:
        calls.append(1)
        stub._last_mode_request = time.monotonic()
        return True

    stub.request_extended_mode = fake_request
    check("退回最小报文 -> 重新请求", bool(stub.ensure_motion()), True)
    check("确实发了一次请求", len(calls), 1)
    check("2 秒内不重复打扰", stub.ensure_motion(), None)
    check("请求次数没变", len(calls), 1)
    stub.motion_mode = "extended"
    check("正常扩展模式 -> 不打扰手柄", stub.ensure_motion(), None)
    stub.static_count = 90
    stub._last_mode_request = 0.0
    check("报文冻结 -> 也要重新请求", bool(stub.ensure_motion()), True)
    stub.transport = "usb"
    stub._last_mode_request = 0.0
    check("有线手柄不做这个请求", stub.ensure_motion(), None)

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
