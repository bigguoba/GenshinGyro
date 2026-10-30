"""Tests for the audio-haptics module (``haptics.py``) and its window wiring.

Nothing here needs a controller, a sound card or a VIIPER server: the pieces that
would talk to hardware are injected (fake VIIPER client, fake pad, fake audio
stream, fake engine), which is exactly the seam the module was built with.  What
*is* pinned down is everything that was measured on real hardware, because those
byte layouts are the difference between "vibrates" and "does nothing":

* NS Pro ``0x10`` payload with the counter byte, full-range amplitude table
  (0xC8 must not be masked down to 0x48), high band only;
* DS4 ``0x05`` (32 bytes) and DS4 Bluetooth ``0x11`` (exactly 78 bytes, CRC32);
* ``LevelSender`` keeping only the newest value and honouring the transport's
  minimum write interval;
* the endpoint lifecycle: reuse what exists, build an audio-only device when there
  is none, and on release remove *only* what this program built.

Run:  python tools\\test_haptics.py
"""

from __future__ import annotations

import math
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dualshock4  # noqa: E402
import haptics  # noqa: E402
import settings  # noqa: E402
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


def check_true(name: str, value) -> None:
    check(name, bool(value), True)


def wait_until(predicate, seconds: float = 3.0, step: float = 0.01) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeStream:
    def __init__(self):
        self.opened = False
        self.closed = False

    def open(self, timeout: float = 5.0):
        self.opened = True
        return self

    def close(self):
        self.closed = True


class FakeServer:
    """Just enough of ``viiper.ViiperServer`` for the endpoint lifecycle."""

    def __init__(self):
        self.buses = []
        self.devices = []
        self.removed_devices = []
        self.removed_buses = []
        self.add_calls = []
        self.fail_add = None
        self.fail_times = 0

    def create_bus(self):
        bus = len(self.buses) + 100
        self.buses.append(bus)
        return bus

    def add_device(self, bus_id, device_type="dualsenseaudioonlyduplexv5",
                   device_specific=None):
        if self.fail_add is not None:
            raise self.fail_add
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("Conflict: Failed to auto-attach device")
        self.add_calls.append((bus_id, device_type))
        device_id = f"dev{len(self.devices) + 1}"
        self.devices.append((bus_id, device_type, device_id))
        return {"devId": device_id, "usbipPort": 1}

    def remove_device(self, bus_id, device_id):
        self.removed_devices.append((bus_id, device_id))

    def remove_bus(self, bus_id):
        self.removed_buses.append(bus_id)


class FakePad:
    """Records the values the engine asked for; counts silence/close calls."""

    def __init__(self):
        self.levels = []
        self.silenced = 0
        self.closed = False
        self.error = None
        self.transport = "usb"

    def set_levels(self, left, right):
        if self.error is not None:
            raise self.error
        self.levels.append((left, right))

    def silence(self, repeats=1):
        self.silenced += 1
        self.levels.append((0.0, 0.0))

    def close(self):
        self.closed = True

    def describe(self):
        return "fake pad"


class FakeNsPro:
    """Stand-in for ``switchpro.SwitchProController`` on the rumble path."""

    def __init__(self):
        self.packets = []
        self.closed = False
        self.transport = "usb"

    def set_rumble_payload_counted(self, payload, counter):
        self.packets.append(bytes((counter & 0x0F,)) + bytes(payload))

    def close(self):
        self.closed = True


class FakeHidDevice:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)


class FakeDs4:
    """Stand-in for ``dualshock4.DualShock4`` (wired uses write_output)."""

    def __init__(self, transport="usb"):
        self.transport = transport
        self.written = []
        self.closed = False
        self._device = FakeHidDevice()

    def write_output(self, data):
        self.written.append(bytes(data))
        return len(data)

    def close(self):
        self.closed = True


class FakeAudio:
    """The parts of ``pyaudiowpatch`` the engine uses."""

    paInt16 = 8

    def __init__(self, freq=1000.0, amplitude=12000.0, idle=False):
        self.stream = (IdleInputStream() if idle
                       else FakeInputStream(freq, amplitude))
        self.opened = []
        self.terminated = False

    def PyAudio(self):                              # noqa: N802 - mirrors the real API
        return self

    def open(self, **kwargs):
        self.opened.append(kwargs)
        return self.stream

    def terminate(self):
        self.terminated = True


class FakeInputStream:
    """A 1 kHz tone in the 600–4000 Hz ("vocal") band.

    ``channels`` interleaved channels, laid out like the real virtual DualSense:
    a 4-channel endpoint has the **haptics on channels 2/3** (the 3rd and 4th
    channel, like a real PS5) and the **speaker on 0/1**.  Writing the tone into
    all four (the old fake) painted a picture the hardware never has -- the engine
    now reads 2/3 only.
    """

    def __init__(self, freq=1000.0, amplitude=12000.0, rate=48000, channels=4,
                 tone_channels=None):
        self.freq = float(freq)
        self.amplitude = float(amplitude)
        self.rate = rate
        self.channels = channels
        if tone_channels is None:
            tone_channels = (2, 3) if channels >= 4 else tuple(range(channels))
        self.tone_channels = tuple(tone_channels)
        self.phase = 0
        self.closed = False
        self.reads = 0

    def read(self, frames, exception_on_overflow=True):
        import numpy as np

        self.reads += 1
        time.sleep(0.002)
        index = np.arange(frames) + self.phase
        block = (self.amplitude
                 * np.sin(2 * np.pi * self.freq * index / self.rate)).astype(np.int16)
        # 门限式的 4 Hz 方波包络：自适应噪底会把"一直响的稳定音"慢慢当成底噪
        # （这是预设里 adaptive=True 的固有行为），真实的游戏音效也不是连续的，
        # 所以测试用有起伏的信号，否则测的是自适应而不是"恢复输出"。
        gate_on = ((index % (self.rate // 4)) < (self.rate // 8))
        block = np.where(gate_on, block, 0).astype(np.int16)
        self.phase += frames
        interleaved = np.zeros((frames, self.channels), dtype=np.int16)
        for channel in self.tone_channels:
            interleaved[:, channel] = block
        return interleaved.reshape(-1).tobytes()

    def close(self):
        self.closed = True


class IdleInputStream:
    """A loopback with nothing to give -- what WASAPI really does on silence.

    ``get_read_available()`` stays 0 and a blocking ``read()`` would hang, so the
    engine must poll and synthesise silent blocks instead.
    """

    def __init__(self, channels=4, rate=48000):
        self.channels = channels
        self.rate = rate
        self.closed = False
        self.read_calls = 0

    def get_read_available(self):
        return 0

    def read(self, frames, exception_on_overflow=True):
        self.read_calls += 1
        raise AssertionError("silence must not fall back to a blocking read()")

    def close(self):
        self.closed = True


class FakeEndpoint:
    """Endpoint that reports a ready device without touching audio at all."""

    def __init__(self, name="扬声器 (DualSense Wireless Controller) [Loopback]",
                 fail_with=None):
        self.name = name
        self.device = {"name": name, "index": 7, "maxInputChannels": 4,
                       "defaultSampleRate": 48000, "isLoopbackDevice": True}
        self.fail_with = fail_with
        self.ensured = 0
        self.released = 0

    def ensure(self):
        self.ensured += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self.name

    def release(self):
        self.released += 1


class FakeEngine:
    """Engine used to test the runner without audio or hardware."""

    def __init__(self, fail_with=None):
        self.fail_with = fail_with
        self.started = None
        self.stopped = 0
        self.keep_endpoint = None
        self.error = None
        self.tuning = {}
        self._levels = (0.25, 0.5)
        self._status = "已开启（fake）"
        self.endpoint_name = "fake-endpoint"

    def start(self, kind, transport, controller=None):
        if self.fail_with is not None:
            raise self.fail_with
        self.started = (kind, transport, controller)
        return self.endpoint_name

    def stop(self, keep_endpoint: bool = False):
        self.stopped += 1
        self.keep_endpoint = keep_endpoint
        self._status = "已关闭"

    def set_gain(self, value):
        self.tuning["gain"] = value
        return value

    def set_sensitivity(self, value):
        self.tuning["sensitivity"] = value
        return value

    def set_attack(self, value):
        self.tuning["attack"] = value
        return value

    def set_release(self, value):
        self.tuning["release"] = value
        return value

    def set_dead_zone(self, value):
        self.tuning["dead_zone"] = value
        return value

    def set_gamma(self, value):
        self.tuning["gamma"] = value
        return value

    def set_split(self, value):
        self.tuning["split"] = value
        return value

    def set_dual(self, value):
        self.tuning["dual"] = value

    def set_swap(self, value):
        self.tuning["swap"] = value

    def levels(self):
        return self._levels

    def status(self):
        return self._status


# --------------------------------------------------------------------------- #
# [1] Pure signal logic
# --------------------------------------------------------------------------- #

def test_signal_helpers() -> None:
    print("\n[1] 曲线与工具函数")
    check("clamp 默认夹到 0..1", (haptics.clamp(-1), haptics.clamp(2), haptics.clamp(0.5)),
          (0.0, 1.0, 0.5))
    check("clamp 可以换上下界", haptics.clamp(5, 0, 3), 3)
    check("上升用 attack", round(haptics.smooth_signal(0.0, 1.0, 0.5, 0.1), 4), 0.5)
    check("下降用 release", round(haptics.smooth_signal(1.0, 0.0, 0.5, 0.25), 4), 0.75)
    check("阈值以下就是 0", haptics.map_value_gamma(100, 220, 5200, 1.05), 0.0)
    check("满量程 = 1", round(haptics.map_value_gamma(5200, 220, 5200, 1.05), 4), 1.0)
    check("gamma > 1 压低下半段",
          haptics.map_value_gamma(2710, 220, 5200, 2.0)
          < haptics.map_value_gamma(2710, 220, 5200, 1.0), True)
    check("安静段识别为安静", haptics.classify_content(10, 5, 3, 0.1, "识别中"), "安静")
    check("低频 + 强瞬态偏向音乐",
          haptics.classify_content(900, 200, 700, 0.9, "识别中"), "音乐")
    check("人声占比高偏向 ASMR",
          haptics.classify_content(900, 800, 40, 0.05, "识别中"), "ASMR")

    # 虚拟 DualSense 的音频端点 4 声道：2/3（第 3、4 声道）= 触觉，0/1 = 手柄扬声器。
    # 只取 2/3，且除数用总声道数，保证同样的音频不会因为"忽略扬声器"而变强/变弱。
    import numpy as np

    print("\n[1b] 4 声道混音：只取第 3/4 声道（触觉），第 1/2 声道不参与")
    haptic_only = np.zeros((4, 4), dtype=np.float32)
    haptic_only[:, 2] = haptic_only[:, 3] = 1000.0
    check("只有触觉声道时输出 = 旧版电平",
          [round(v, 1) for v in haptics.mix_to_mono(haptic_only.reshape(-1), 4)],
          [500.0] * 4)

    speaker_only = np.zeros((4, 4), dtype=np.float32)
    speaker_only[:, 0] = speaker_only[:, 1] = 8000.0
    check("只有 1/2 声道（对白/扬声器）时完全不震",
          [round(v, 1) for v in haptics.mix_to_mono(speaker_only.reshape(-1), 4)],
          [0.0] * 4)

    both = haptic_only + speaker_only
    check("1/2 声道再响也不会污染震动强度",
          [round(v, 1) for v in haptics.mix_to_mono(both.reshape(-1), 4)],
          [500.0] * 4)

    check("触觉声道只取 2/3，且与 1/2 声道不重叠",
          (haptics.HAPTIC_CHANNELS[4],
           set(haptics.HAPTIC_CHANNELS[4]) & {0, 1}),
          ((2, 3), set()))

    stereo = np.ones((4, 2), dtype=np.float32) * 1000.0
    check("2 声道端点仍然取平均",
          [round(v, 1) for v in haptics.mix_to_mono(stereo.reshape(-1), 2)],
          [1000.0] * 4)
    check("单声道原样通过",
          [round(v, 1) for v in haptics.mix_to_mono(
              np.array([1.0, 2.0], dtype=np.float32), 1)],
          [1.0, 2.0])
    check("4 声道分工表就是 (2, 3)", haptics.HAPTIC_CHANNELS[4], (2, 3))


def test_log_index() -> None:
    print("\n[2] NS Pro 振幅表（100 档对数曲线）")
    check("0 = 0 档", haptics._log_index(0.0), 0)
    check("负数 = 0 档", haptics._log_index(-0.5), 0)
    check("1.0 = 100 档", haptics._log_index(1.0), 100)
    check("超过 1 也夹在 100", haptics._log_index(2.0), 100)
    check("0.5 = 68 档（不是 33 档的旧表）", haptics._log_index(0.5), 68)
    previous = -1
    monotone = True
    for step in range(0, 101):
        row = haptics._log_index(step / 100.0)
        if row < previous:
            monotone = False
        previous = row
    check("整条曲线单调不减", monotone, True)
    check("空档也覆盖得住（0.01 不是 0）", haptics._log_index(0.01) > 0, True)
    check("满档就是满量程（2 * 100 = 0xC8）", 2 * haptics._log_index(1.0), 0xC8)


def test_frequency_codes() -> None:
    print("\n[3] 频率码与 switchpro 的文档编码一致")
    mismatched = []
    for band, sweep in (("low", (1.0, 41.0, 80.0, 160.0, 320.0, 500.0, 626.0, 9999.0)),
                        ("high", (1.0, 82.0, 160.0, 320.0, 640.0, 1252.0, 99999.0))):
        for hz in sweep:
            mine = haptics._frequency_code(hz, band)
            theirs = switchpro._rumble_frequency_code(hz, band)
            if mine != theirs:
                mismatched.append((band, hz, mine, theirs))
    check("所有试点都相同（含越界夹取）", mismatched, [])


# --------------------------------------------------------------------------- #
# [4] Report encoders
# --------------------------------------------------------------------------- #

def test_nspro_payload() -> None:
    print("\n[4] NS Pro 报文（0x10 + 计数器 + 100 档）")
    packet = haptics.nspro_packet(b"\x01" * 8, 3)
    check("长度 = 1 + 1 + 8", len(packet), 10)
    check("byte0 = 0x10", packet[0], switchpro.OUTPUT_REPORT_RUMBLE)
    check("byte1 = 计数器", packet[1], 3)
    check("计数器是 4 位（16 -> 0）", haptics.nspro_packet(bytes(8), 16)[1], 0)
    check("计数器负数也能过", haptics.nspro_packet(bytes(8), -1)[1], 15)
    check("载荷只有 8 字节（多的截掉）",
          len(haptics.nspro_packet(bytes(20), 0)), 10)

    full = haptics.nspro_payload_bands(0.0, 160.0, 1.0, 320.0)
    check("满档单侧 = 00 C9 40 40（交接报告里的字节样例）",
          full[:4].hex(" ").upper(), "00 C9 40 40")
    check("左右两半相同（没有左右参数时）", full[:4], full[4:])
    check("满档幅度 0xC8 没有被掩成 0x48", full[1] & 0xFE, 0xC8)
    check("byte1 的最低位是高频码的第 9 位，所以整字节是 0xC9", full[1], 0xC9)
    check("低频段置 0 时是中立值", full[2:4], bytes((0x40, 0x40)))
    check("0 强度 = 文档中立报文（00 01 40 40 ×2）",
          haptics.nspro_payload_bands(0.0, 160.0, 0.0, 320.0).hex(" ").upper(),
          "00 01 40 40 00 01 40 40")
    check("只驱动低频段时高频字节是 0x01",
          haptics.nspro_payload_bands(1.0, 160.0, 0.0, 320.0)[1], 0x01)
    # 幅度必须随强度单调（高频段的幅度在 byte1 的 bit1..6）。
    previous = -1
    monotone = True
    for step in range(0, 21):
        byte = haptics.nspro_payload_bands(0.0, 160.0, step / 20.0, 320.0)[1]
        high = byte & 0xFE
        if high < previous:
            monotone = False
        previous = high
    check("幅度随强度单调不减", monotone, True)


def test_ds4_report() -> None:
    print("\n[5] DS4 报文（有线 0x05 / 蓝牙 0x11）")
    wired = haptics.ds4_report(255, 128, "usb")
    check("有线 32 字节", len(wired), dualshock4.OUTPUT_REPORT_USB_SIZE)
    check("byte0 = 0x05", wired[0], dualshock4.OUTPUT_REPORT_USB)
    check("byte4 = 小马达（右）", wired[4], 255)
    check("byte5 = 大马达（左）", wired[5], 128)
    check("灯条保持玩家 1 蓝", wired[6:9], bytes(dualshock4.DEFAULT_LIGHTBAR))
    check("开头与交接报告的样例一致",
          wired[:9].hex(" ").upper(), "05 07 00 00 FF 80 00 00 40")

    bt = haptics.ds4_report(255, 128, "bluetooth")
    check("蓝牙精确 78 字节", len(bt), dualshock4.OUTPUT_REPORT_BT_SIZE)
    check("byte0 = 0x11", bt[0], dualshock4.OUTPUT_REPORT_BT)
    check("byte1 = 0xC4（HID + CRC32 + 4 ms）", bt[1],
          dualshock4.HWCTL_HID | dualshock4.HWCTL_CRC32
          | dualshock4.DEFAULT_POLL_INTERVAL_MS)
    check("byte3 = 马达 + 灯都有效", bt[3],
          dualshock4.VALID_FLAG0_MOTOR | dualshock4.VALID_FLAG0_LED)
    check("byte6 = 小马达（右）", bt[6], 255)
    check("byte7 = 大马达（左）", bt[7], 128)
    crc = int.from_bytes(bt[-4:], "little")
    check("CRC32 覆盖 0xA2 + 前 74 字节", crc, dualshock4.output_crc32(bt))
    check("末尾 4 字节就是 CRC（不会被补零）", bt[-4:], crc.to_bytes(4, "little"))

    check("超过 255 被夹住", haptics.ds4_report(300, 999, "usb")[4:6],
          bytes((255, 255)))
    check("负数被夹到 0", haptics.ds4_report(-5, -1, "usb")[4:6], bytes((0, 0)))


# --------------------------------------------------------------------------- #
# [6] RumblePad over fake handles
# --------------------------------------------------------------------------- #

def test_rumble_pad() -> None:
    print("\n[6] RumblePad：左右分配 / 计数器 / 静音 / 借用不关闭")
    ns = FakeNsPro()
    pad = haptics.RumblePad("nspro", "usb", controller=ns)
    pad.set_levels(1.0, 0.0)
    check("只左：第一个包的计数器是 0", ns.packets[0][0], 0)
    left_on = ns.packets[0][1:5]
    right_off = ns.packets[0][5:9]
    check("左半 = 满档高频", left_on.hex(" ").upper(), "00 C9 40 40")
    check("右半 = 中立", right_off.hex(" ").upper(), "00 01 40 40")
    pad.set_levels(0.0, 1.0)
    check("只右：第二个包计数器是 1", ns.packets[1][0], 1)
    check("只右：左半中立", ns.packets[1][1:5].hex(" ").upper(), "00 01 40 40")
    check("只右：右半满档", ns.packets[1][5:9].hex(" ").upper(), "00 C9 40 40")
    for _ in range(18):
        pad.set_levels(0.5, 0.5)
    counters = [packet[0] for packet in ns.packets[2:]]
    check("计数器都落在 0..15", all(0 <= value <= 15 for value in counters), True)
    check("计数器确实绕回去了（出现过 15 和 0）",
          (15 in counters, 0 in counters), (True, True))

    ns.packets.clear()
    pad.silence()
    check("静音发 5 次", len(ns.packets), haptics.STOP_REPEATS)
    check("静音是全零载荷（不是 0x40 中立）",
          ns.packets[0][1:9], switchpro.RUMBLE_SILENCE)
    pad.close()
    check("借来的手柄不会被关闭", ns.closed, False)
    pad.set_levels(1.0, 1.0)
    check("关闭后 set_levels 是空操作", len(ns.packets), haptics.STOP_REPEATS)

    print("\n[6b] DS4 两条写路径")
    wired = FakeDs4("usb")
    pad = haptics.RumblePad("ds4", "usb", pad=wired)
    pad.set_levels(1.0, 0.0)
    check("有线走 write_output", len(wired.written), 1)
    check("有线报文 32 字节", len(wired.written[0]), 32)
    check("左 = 大马达（byte5）", wired.written[0][5], 255)
    check("右 = 小马达（byte4）", wired.written[0][4], 0)

    wireless = FakeDs4("bluetooth")
    pad = haptics.RumblePad("ds4", "bluetooth", pad=wireless)
    pad.set_levels(0.0, 1.0)
    check("蓝牙不走 write_output（它会补零）", len(wireless.written), 0)
    check("蓝牙走中断管道 WriteFile", len(wireless._device.writes), 1)
    check("蓝牙报文精确 78 字节", len(wireless._device.writes[0]), 78)
    check("蓝牙小马达（byte6）= 255", wireless._device.writes[0][6], 255)
    check("蓝牙大马达（byte7）= 0", wireless._device.writes[0][7], 0)


def test_pulse_hold_and_sender() -> None:
    print("\n[7] PulseHold（默认关就是原样返回）")
    hold = haptics.PulseHold(0.0)
    check("关掉时原样返回", hold.update(0.4, 100.0), 0.4)
    hold = haptics.PulseHold(0.05)
    check("打开时先返回当前值", hold.update(0.4, 100.0), 0.4)
    check("保持期内给峰值", hold.update(0.0, 100.01), 0.4)
    check("保持期后放行", hold.update(0.0, 100.06), 0.0)

    print("\n[8] LevelSender：只保留最新值 / 限速 / 干净停止")
    pad = FakePad()
    sender = haptics.LevelSender(pad, min_interval=0.0, lead_rise=0.0)
    sender.start()
    try:
        for index in range(1, 51):
            sender.set_levels(index / 100.0, 0.0)
        check_true("至少发出过一次", wait_until(lambda: len(pad.levels) >= 1))
        wait_until(lambda: len(pad.levels) >= 3, 1.0)
        check("没有把 50 次全发出去（邮箱只留最新）", len(pad.levels) < 50, True)
        check("最后写的是最新的值", pad.levels[-1], (0.5, 0.0))
    finally:
        sender.stop()
        sender.join(timeout=1.0)
    check("停止后线程结束", sender.is_alive(), False)

    print("\n[8a] 限速生效（NS Pro 30 ms / 蓝牙 DS4 20 ms 的机制）")
    throttled = FakePad()
    sender = haptics.LevelSender(throttled, min_interval=0.06, lead_rise=10.0)
    sender.start()
    started = time.monotonic()
    try:
        for _ in range(12):
            sender.set_levels(0.3, 0.3)
            time.sleep(0.01)
    finally:
        sender.stop()
        sender.join(timeout=1.0)
    elapsed = time.monotonic() - started
    check("0.12 秒内最多 3 次", len(throttled.levels) <= 3, True)
    check("但确实发了", len(throttled.levels) >= 1, True)
    check("确实跑满了这段时间", elapsed > 0.05, True)

    print("\n[8b] 写失败要停下并记下来")
    broken = FakePad()
    broken.error = OSError("handle 丢了")
    sender = haptics.LevelSender(broken, min_interval=0.0, lead_rise=0.0)
    sender.start()
    try:
        sender.set_levels(0.5, 0.5)
        check_true("错误被记录", wait_until(lambda: sender.error is not None, 1.0))
        check_true("线程自己退出（不会一直报错）",
                   wait_until(lambda: not sender.is_alive(), 1.0))
    finally:
        sender.stop()
        sender.join(timeout=1.0)


# --------------------------------------------------------------------------- #
# [9] AudioEndpoint lifecycle with a fake VIIPER client
# --------------------------------------------------------------------------- #

ENDPOINT_NAME = "扬声器 (DualSense Wireless Controller) [Loopback]"


def make_endpoint(server=None, devices=None, **kwargs):
    """An AudioEndpoint whose "Windows device list" is a list we control."""
    devices = devices if devices is not None else []

    def sleep(_seconds):
        return None                                 # 不要真等 20 秒

    return haptics.AudioEndpoint(
        server=server if server is not None else FakeServer(),
        list_fn=lambda: list(devices),
        sleep_fn=sleep,
        **kwargs), devices


def test_endpoint_lifecycle() -> None:
    print("\n[9] 音频端点：没有就建，有关就复用")
    server = FakeServer()
    endpoint, devices = make_endpoint(server=server)
    streams = []

    def device_appears():
        # 模拟 Windows：设备建好之后，端点才出现在枚举结果里。
        if server.devices and not devices:
            devices.append({"name": ENDPOINT_NAME, "index": 3,
                            "maxInputChannels": 4, "defaultSampleRate": 48000,
                            "isLoopbackDevice": True})
        return list(devices)

    endpoint._list = device_appears
    endpoint._stream_fn = lambda _server, bus_id, device_id: (
        streams.append(FakeStream()) or streams[-1])
    endpoint._port_open = lambda: True

    name = endpoint.ensure()
    check("返回端点名", name, ENDPOINT_NAME)
    check("建了一个 bus", len(server.buses), 1)
    check("加的是 audio-only 设备", server.add_calls,
          [(server.buses[0], haptics.AUDIO_ONLY_DEVICE_TYPE)])
    check("设备流被打开了", (len(streams), streams[0].opened), (1, True))
    check("端点归本程序所有", endpoint.owned, True)
    check("device 信息可供引擎使用", endpoint.device["index"], 3)

    endpoint.release()
    check("release 关掉流", streams[0].closed, True)
    check("release 只拆自己建的设备", server.removed_devices,
          [(server.buses[0], server.devices[0][2])])
    check("release 也拆 bus", server.removed_buses, [server.buses[0]])
    check("release 后状态清空", (endpoint.owned, endpoint.name), (False, ""))
    check("没有 VIIPER 服务被启动（服务本来就在）", endpoint._server_process, None)

    print("\n[9b] 已有的端点只复用，绝不拆")
    server = FakeServer()
    devices = [{"name": ENDPOINT_NAME, "index": 5, "maxInputChannels": 4,
                "defaultSampleRate": 48000, "isLoopbackDevice": True}]
    endpoint, _devices = make_endpoint(server=server, devices=devices)
    endpoint._port_open = lambda: True
    check("复用现有端点", endpoint.ensure(), ENDPOINT_NAME)
    check("没有建 bus", server.buses, [])
    check("没有加设备", server.add_calls, [])
    check("也不归本程序所有", endpoint.owned, False)
    endpoint.release()
    check("关的时候没有拆设备", server.removed_devices, [])
    check("关的时候没有拆 bus", server.removed_buses, [])
    check("但本地状态也清了", endpoint.name, "")

    print("\n[9c] 端点名字匹配（含回退）")
    devices = [{"name": "扬声器 (Realtek(R) Audio) [Loopback]", "index": 1},
               {"name": "扬声器 (Wireless Controller) [Loopback]", "index": 2}]
    endpoint, _devices = make_endpoint(devices=devices)
    check("回退匹配 Wireless Controller", endpoint.find()["index"], 2)
    devices = [{"name": "扬声器 (Realtek(R) Audio) [Loopback]", "index": 1}]
    endpoint, _devices = make_endpoint(devices=devices)
    check("普通声卡不算", endpoint.find(), None)

    print("\n[9d] VIIPER 没在跑：不许建，也不许静默失败")
    server = FakeServer()
    endpoint, _devices = make_endpoint(server=server, allow_server_start=False)
    endpoint._port_open = lambda: False
    try:
        endpoint.ensure()
        check("应该抛出 EndpointError", "no exception", "EndpointError")
    except haptics.EndpointError as exc:
        check_true("提示里说明服务没运行", "VIIPER" in str(exc))
        check_true("给出了怎么办", ("启动" in str(exc)) or ("VIIPER" in str(exc)))
    check("没有偷偷加设备", server.add_calls, [])

    print("\n[9e] 自动启动服务后能建出端点")
    server = FakeServer()
    launched = []

    class FakeProcess:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

    def launch():
        launched.append(True)
        return FakeProcess()

    endpoint, devices = make_endpoint(server=server, launch_fn=launch)
    endpoint._port_open = lambda: False          # 服务确实没在跑

    def list_after_build():
        if server.devices and not devices:
            devices.append({"name": ENDPOINT_NAME, "index": 9,
                            "maxInputChannels": 4, "defaultSampleRate": 48000})
        return list(devices)

    endpoint._list = list_after_build
    endpoint._stream_fn = lambda *_args: FakeStream()
    # wait_for_viiper 是模块级函数，这里换成"启动后立刻监听成功"，
    # 免得真轮询 20 秒。
    saved_wait = haptics.wait_for_viiper
    haptics.wait_for_viiper = lambda _seconds, *_a, **_k: True
    try:
        name = endpoint.ensure()
    finally:
        haptics.wait_for_viiper = saved_wait
    check("启动了 VIIPER", launched, [True])
    check("端点建出来了", name, ENDPOINT_NAME)
    process = endpoint._server_process
    endpoint.release()
    check("release 关掉自己启动的服务",
          process is not None and process.terminated, True)

    print("\n[9f] 端点 20 秒没出现：收干净并报错")
    server = FakeServer()
    endpoint, _devices = make_endpoint(server=server)
    endpoint._port_open = lambda: True
    endpoint._stream_fn = lambda *_args: FakeStream()
    endpoint._wait_for_endpoint = lambda: None      # 永远等不到
    try:
        endpoint.ensure()
        check("应该抛出 EndpointError", "no exception", "EndpointError")
    except haptics.EndpointError as exc:
        check_true("错误里说明了原因", "音频端点" in str(exc))
    check("超时后设备被移除（不留垃圾）", server.removed_devices,
          [(server.buses[0], server.devices[0][2])])
    check("超时后 bus 也被移除", server.removed_buses, [server.buses[0]])

    print("\n[9g] add_device 失败：不留半成品")
    server = FakeServer()
    server.fail_add = RuntimeError("VIIPER 拒绝了这个设备类型")
    endpoint, _devices = make_endpoint(server=server)
    endpoint._port_open = lambda: True
    try:
        endpoint.ensure()
        check("应该抛出 EndpointError", "no exception", "EndpointError")
    except haptics.EndpointError as exc:
        check_true("把底层原因带出来", "拒绝" in str(exc))
    check("失败后没有残留设备", server.devices, [])
    check("每次尝试的 bus 都被移除（不留垃圾）",
          (len(server.buses) in (1, 2), server.removed_buses == server.buses),
          (True, True))

    print("\n[9g2] 第一次挂设备冲突：换一个 bus 自动重试成功")
    server = FakeServer()
    server.fail_times = 1
    endpoint, _devices = make_endpoint(server=server)
    endpoint._port_open = lambda: True
    endpoint._stream_fn = lambda *_args: FakeStream()
    endpoint._wait_for_endpoint = lambda: {
        "name": ENDPOINT_NAME, "index": 2, "maxInputChannels": 4,
        "defaultSampleRate": 48000}
    check("重试后建出设备", endpoint.ensure(), ENDPOINT_NAME)
    check("用了两个 bus（第一个失败）", len(server.buses), 2)
    check("失败的 bus 被清掉", server.removed_buses, [server.buses[0]])
    check("端点归自己", endpoint.owned, True)

    print("\n[9h] 自己建的端点被外部拆掉：自动重建")
    server = FakeServer()
    endpoint, _devices = make_endpoint(server=server)
    endpoint._port_open = lambda: True
    endpoint._stream_fn = lambda *_args: FakeStream()
    endpoint._wait_for_endpoint = lambda: {
        "name": ENDPOINT_NAME, "index": 4, "maxInputChannels": 4,
        "defaultSampleRate": 48000}
    check("第一次建出来", endpoint.ensure(), ENDPOINT_NAME)
    check("归自己所有", endpoint.owned, True)
    check("用了 1 个 bus", len(server.buses), 1)
    # 设备被外部拆掉（比如 VIIPER 重启）：枚举结果里没有它了
    check("第二次会重建", endpoint.ensure(), ENDPOINT_NAME)
    check("旧 bus 被拆掉", server.removed_buses, [server.buses[0]])
    check("建了第二个 bus", len(server.buses), 2)
    check("重建后仍然归自己", endpoint.owned, True)
    check("重建后还有设备流", endpoint._stream is not None, True)

    print("\n[9i] allow_create=False：只复用（DS5 后端那条路径）")
    server = FakeServer()
    devices = []
    endpoint, _devices = make_endpoint(server=server, devices=devices,
                                       allow_create=False)
    endpoint._port_open = lambda: False
    endpoint._wait_for_endpoint = lambda: None
    try:
        endpoint.ensure()
        check("应该抛出 EndpointError", "no exception", "EndpointError")
    except haptics.EndpointError as exc:
        check_true("提示端点没出现", "音频端点没有出现" in str(exc))
    check("绝不自建第二个设备", server.add_calls, [])
    check("也没有残留 bus", server.removed_buses, [])

    devices.append({"name": ENDPOINT_NAME, "index": 4, "maxInputChannels": 4,
                    "defaultSampleRate": 48000})
    endpoint, _devices = make_endpoint(server=server, devices=devices,
                                       allow_create=False)
    check("端点出现后就能复用", endpoint.ensure(), ENDPOINT_NAME)
    check("复用来的不归自己", endpoint.owned, False)

    print("\n[9j] 找 VIIPER-*.exe：打包后（__file__ 在 _internal 里）也要能找到")
    import paths as paths_module
    original_app_dir = paths_module.app_dir
    scratch = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "build", "viiper_exe_test")
    app = os.path.join(scratch, "GenshinGyro-v1.4.0")
    try:
        os.makedirs(os.path.join(app, "_internal"), exist_ok=True)
        # 用户最常放的位置：exe 旁边
        beside = os.path.join(app, "VIIPER-1.0.0-x64.exe")
        open(beside, "wb").close()
        paths_module.app_dir = lambda: app
        check("exe 旁边能找到", haptics.find_server_exe(), beside)

        # 只放在上一级目录（发行包解压在别处时的常见情况）
        os.remove(beside)
        above = os.path.join(scratch, "VIIPER-2.0.0-x64.exe")
        open(above, "wb").close()
        check("上一级目录也能找到", haptics.find_server_exe(), above)

        # 哪儿都没有
        os.remove(above)
        check("都没有就返回 None", haptics.find_server_exe(), None)
        check("候选目录里包含程序目录和它的上一级",
              haptics._viiper_search_roots()[:2], [app, os.path.dirname(app)])
    finally:
        paths_module.app_dir = original_app_dir
        shutil.rmtree(scratch, ignore_errors=True)


# --------------------------------------------------------------------------- #
# [10] HapticsEngine with fake audio / pad
# --------------------------------------------------------------------------- #

def test_engine() -> None:
    print("\n[10] HapticsEngine：起停 / 参数 / 静音 / 拆自己的端点")
    fake_audio = FakeAudio()
    saved_pyaudio = haptics._pyaudio
    haptics._pyaudio = lambda: fake_audio
    endpoint = FakeEndpoint()
    pad = FakePad()
    created = []

    def pad_factory(kind, transport, controller=None, log=None):
        created.append((kind, transport, controller))
        return pad

    engine = haptics.HapticsEngine(endpoint=endpoint, pad_factory=pad_factory,
                                   gain=1.0, preset="低频沉浸", dual=True)
    try:
        name = engine.start("switch", "usb")
        check("返回端点名", name, endpoint.name)
        check("端点被准备好", endpoint.ensured, 1)
        check("用的是 NS Pro 那条路径", created[0][0], "nspro")
        check("开了音频流（四声道 / 48 kHz）",
              (fake_audio.opened[0]["channels"], fake_audio.opened[0]["rate"]),
              (4, 48000))
        check("采集块 512", fake_audio.opened[0]["frames_per_buffer"], 512)
        check_true("采集线程跑起来了",
                   wait_until(lambda: engine.levels() != (0.0, 0.0), 2.0))
        check_true("开始往手柄写", wait_until(lambda: len(pad.levels) > 0, 2.0))

        print("\n[10b] 运行时参数立刻生效")
        check("set_gain 夹到范围", engine.set_gain(9.0), haptics.GAIN_MAX)
        check("set_gain 非法值保留原值", engine.set_gain("abc"), haptics.GAIN_MAX)
        engine.set_gain(1.25)
        check("set_gain 正常值", engine.gain, 1.25)
        check("set_preset 接受四预设之一", engine.set_preset("人声清晰"), "人声清晰")
        check("set_preset 非法值回默认", engine.set_preset("乱写"),
              haptics.PRESET_DEFAULT)
        engine.set_preset("低频沉浸")
        engine.set_dual(False)
        engine.set_dual(True)
        engine.set_swap(True)
        engine.set_swap(False)

        print("\n[10b2] 六个手动参数：范围 / 映射 / 非法输入")
        check("灵敏度 0 -> 0", engine.set_sensitivity(0), 0.0)
        check("门限跟着变成 800", round(engine.params()["threshold"]), 800)
        check("灵敏度 100 -> 100", engine.set_sensitivity(100), 100.0)
        check("门限跟着变成 80", round(engine.params()["threshold"]), 80)
        check("超过 100 被夹住", engine.set_sensitivity(500), 100.0)
        check("非法输入保持原值", engine.set_sensitivity("abc"), 100.0)
        check("NaN 也保持原值", engine.set_sensitivity(float("nan")), 100.0)
        engine.set_sensitivity(haptics.DEFAULT_TUNING["sensitivity"])
        check("默认灵敏度 ≈ 旧「低频沉浸」的门限",
              abs(engine.params()["threshold"]
                  - haptics.PRESETS[haptics.PRESET_DEFAULT]["threshold"]) < 1.0, True)
        check("拖尾写 300 ms", engine.set_release(300), 300.0)
        check("拖尾进参数（换算成每块比例，300 ms ≈ 0.0786）",
              round(engine.params()["release"], 4), 0.0786)
        check("拖尾越大 → 比例越小（掉得越慢）",
              haptics.release_from_tail(900) < haptics.release_from_tail(30), True)
        check("拖尾换算可逆",
              round(haptics.tail_from_release(haptics.release_from_tail(300)), 1),
              300.0)
        check("拖尾超过上限夹住", engine.set_release(9000), haptics.RELEASE_MAX)
        check("拖尾低于下限夹住", engine.set_release(0), haptics.RELEASE_MIN)
        check("默认拖尾 ≈ 旧预设（0.16 比例 ≈ 141 ms）",
              abs(haptics.HapticsEngine()._dials["release"] - 140.9) < 1.0, True)
        check("死区写 0.08", engine.set_dead_zone(0.08), 0.08)
        check("死区进参数", engine.params()["dead_zone"], 0.08)
        check("死区上限 0.20", engine.set_dead_zone(9), haptics.DEAD_ZONE_MAX)
        check("曲线写 1.4", engine.set_gamma(1.4), 1.4)
        check("曲线低于下限夹住", engine.set_gamma(0.1), haptics.GAMMA_MIN)
        check("曲线高于上限夹住", engine.set_gamma(9), haptics.GAMMA_MAX)
        check("分界写 400", engine.set_split(400), 400.0)
        check("分界同时移动高频段下沿",
              (engine.params()["bass_high"], engine.params()["vocal_low"]), (400.0, 400.0))
        check("分界超出范围夹住", engine.set_split(5000), haptics.SPLIT_MAX)
        check("分界低于范围夹住", engine.set_split(1), haptics.SPLIT_MIN)
        engine.set_preset(haptics.PRESET_DEFAULT)
        check("套用预设会把门限和拖尾都改回来",
              (round(engine.params()["threshold"]), engine.params()["release"]),
              (220, 0.16))

        print("\n[10b3] apply_tuning：界面那一组值一次性套上")
        applied = haptics.apply_tuning(engine, {
            "gain": 1.2, "sensitivity": 0.0, "release": 400.0, "dead_zone": 0.1,
            "gamma": 1.8, "split": 300.0, "dual": False, "swap": True})
        check("返回实际生效的值", applied["release"], 400.0)
        check("强度也进去了", engine.gain, 1.2)
        check("门限来自灵敏度", round(engine.params()["threshold"]), 800)
        check("分界两段一起动",
              (engine.params()["bass_high"], engine.params()["vocal_low"]),
              (300.0, 300.0))
        check("两个开关也带上了", (applied["dual"], applied["swap"]), (False, True))
        haptics.apply_tuning(engine, haptics.DEFAULT_TUNING)
        check("恢复默认后门限回到 ≈220",
              abs(engine.params()["threshold"] - 220) < 1.0, True)
        check("dual=None 表示跟着预设走（命令行工具用）",
              haptics.HapticsEngine(dual=None)._dual, None)

        print("\n[10c] 静音与停止")
        engine.set_enabled(False)
        time.sleep(0.3)                             # 让邮箱里最后一条 0 写出去
        check("关闭输出时发了静音", pad.silenced >= 1, True)
        check("关闭后电平归零", engine.levels(), (0.0, 0.0))
        count = len(pad.levels)
        time.sleep(0.25)
        # 发送线程会定期重申最后一级（原实现就是这样，让手柄不掉档），所以这里
        # 只要求"之后写出去的全是 0"，而不是"一条都不写"。
        check("关闭后写出去的只有 0",
              all(level == (0.0, 0.0) for level in pad.levels[count - 1:]), True)
        engine.set_enabled(True)
        check_true("恢复后继续写", wait_until(lambda: len(pad.levels) > count, 2.0))
        check_true("恢复后强度回来了", wait_until(lambda: engine.levels() != (0.0, 0.0), 2.0))

        engine.stop()
        check("停止后不再运行", engine.running, False)
        check("停止时发过静音", pad.silenced >= 1, True)
        check("手柄被关闭", pad.closed, True)
        check("音频流被关闭", fake_audio.stream.closed, True)
        check("PyAudio 被 terminate", fake_audio.terminated, True)
        check("端点被释放（只拆自己建的）", endpoint.released >= 1, True)
        check("状态显示已关闭", engine.status(), "已关闭")
    finally:
        haptics._pyaudio = saved_pyaudio
        try:
            engine.stop()
        except Exception:
            pass

    print("\n[10d] 手柄打不开时不留半成品")
    fake_audio = FakeAudio()
    haptics._pyaudio = lambda: fake_audio
    endpoint = FakeEndpoint()

    def broken_factory(*_args, **_kwargs):
        raise haptics.PadError("没找到手柄")

    engine = haptics.HapticsEngine(endpoint=endpoint, pad_factory=broken_factory)
    try:
        engine.start("nspro", "usb")
        check("应该抛出 PadError", "no exception", "PadError")
    except haptics.PadError:
        check_true("手柄错误原样抛出", True)
    finally:
        haptics._pyaudio = saved_pyaudio
    check("端点被释放（不留虚拟设备）", endpoint.released, 1)
    check("引擎没有在运行", engine.running, False)

    print("\n[10d2] 手柄中途失灵：引擎要报错，不能一直假装正常")
    fake_audio = FakeAudio()
    haptics._pyaudio = lambda: fake_audio
    endpoint = FakeEndpoint()
    pad = FakePad()
    engine = haptics.HapticsEngine(endpoint=endpoint, gain=1.0,
                                   pad_factory=lambda *a, **k: pad)
    try:
        engine.start("nspro", "usb")
        check_true("先正常运行", wait_until(lambda: len(pad.levels) > 0, 2.0))
        pad.error = OSError("手柄拔了")
        check_true("写不进去就被记成错误",
                   wait_until(lambda: engine.error is not None, 2.0))
        check_true("状态里写明是写震动失败", "写震动失败" in engine.status())
        engine.stop()
        check("停掉后端点也被释放", endpoint.released >= 1, True)
    finally:
        haptics._pyaudio = saved_pyaudio
        try:
            engine.stop()
        except Exception:
            pass

    print("\n[10e] step()：静音块不产生震动，响块产生强度")
    engine = haptics.HapticsEngine(preset="低频沉浸", gain=1.0, dual=True)
    engine._rate = 48000
    check("全零样本 = 0/0", engine.step([0.0] * 512), (0.0, 0.0))
    tone = [12000.0 * math.sin(i / 8) for i in range(512)]
    loud = 0.0
    for _ in range(40):                             # 让平滑爬到稳态
        left, right = engine.step(tone)
        loud = max(loud, left, right)
    check_true("响样本能让马达起来", loud > 0.05)
    engine.set_gain(0.0)
    for _ in range(60):                             # 衰减到死区以下
        left, right = engine.step(tone)
    check("强度 0 时输出归零", (left, right), (0.0, 0.0))

    print("\n[10f] stop(keep_endpoint=True)：只停引擎，端点留给上层")
    fake_audio = FakeAudio()
    haptics._pyaudio = lambda: fake_audio
    endpoint = FakeEndpoint()
    kept = FakePad()
    engine = haptics.HapticsEngine(endpoint=endpoint,
                                   pad_factory=lambda *a, **k: kept)
    released = silenced = closed = None
    try:
        engine.start("nspro", "usb")
        engine.stop(keep_endpoint=True)
        released, silenced, closed = endpoint.released, kept.silenced, kept.closed
    finally:
        haptics._pyaudio = saved_pyaudio
        try:
            engine.stop()                       # 再来一次（拆端点）收干净
        except Exception:
            pass
    check("引擎停了", engine.running, False)
    check("端点没被拆（手柄断开时声卡要留着）", released, 0)
    check("手柄照样静音并关闭", (silenced >= 1, closed), (True, True))
    check("再调一次 stop() 才拆端点", endpoint.released, 1)

    print("\n[10g] 端点安静时：不阻塞、按静音块衰减、停止干脆")
    fake_audio = FakeAudio(idle=True)
    haptics._pyaudio = lambda: fake_audio
    endpoint = FakeEndpoint()
    quiet_pad = FakePad()
    engine = haptics.HapticsEngine(endpoint=endpoint,
                                   pad_factory=lambda *a, **k: quiet_pad)
    thread = None
    try:
        engine.start("nspro", "usb")
        thread = engine._thread
        check("安静时不会去调阻塞 read()", fake_audio.stream.read_calls, 0)
        engine._left = engine._right = 1.0          # 假装刚才还在震
        check_true("静音块把强度降到 0",
                   wait_until(lambda: engine.levels() == (0.0, 0.0), 2.0))
        started = time.monotonic()
        engine.stop()
        took = time.monotonic() - started
        check_true("stop() 不陪着等（< 1 秒）", took < 1.0)
        check("采集线程自己退出了", thread.is_alive(), False)
        check("流被采集线程关掉", fake_audio.stream.closed, True)
        check("引擎里的流也清空了", engine._stream, None)
    finally:
        haptics._pyaudio = saved_pyaudio
        try:
            engine.stop()
        except Exception:
            pass

    print("\n[10h] 借用端点（owns_endpoint=False）：失败和停止都不许拆")
    fake_audio = FakeAudio()
    haptics._pyaudio = lambda: fake_audio
    endpoint = FakeEndpoint()

    def broken(*_args, **_kwargs):
        raise haptics.PadError("没找到手柄")

    engine = haptics.HapticsEngine(endpoint=endpoint, owns_endpoint=False,
                                   pad_factory=broken)
    try:
        try:
            engine.start("nspro", "usb")
        except haptics.PadError:
            pass
        check("启动失败也不拆借来的端点", endpoint.released, 0)
        borrowed = FakePad()
        engine._pad_factory = lambda *a, **k: borrowed
        engine.start("nspro", "usb")
        engine.stop()
        check("正常停止也不拆", endpoint.released, 0)
        check("手柄照样被关", borrowed.closed, True)
        check("默认（不写这个参数）就是自己管端点",
              haptics.HapticsEngine(endpoint=FakeEndpoint())._owns_endpoint, True)
        check("runner 的默认引擎不拥有端点",
              haptics.HapticsRunner()._engine_factory()._owns_endpoint, False)
    finally:
        haptics._pyaudio = saved_pyaudio


# --------------------------------------------------------------------------- #
# [11] HapticsRunner (what the window talks to)
# --------------------------------------------------------------------------- #

def test_runner() -> None:
    print("\n[11] HapticsRunner：后台起停 / 参数 / 端点所有权")
    engines = []

    def factory():
        engine = FakeEngine()
        engines.append(engine)
        return engine

    endpoint = FakeEndpoint()
    runner = haptics.HapticsRunner(engine_factory=factory, endpoint=endpoint)
    try:
        runner.set_tuning({"gain": 1.2, "sensitivity": 30.0, "release": 300.0,
                           "dual": False})
        check("没启动时只是记住", runner.tuning()["gain"], 1.2)
        runner.request("nspro", "usb", controller="PA D")
        check_true("引擎被启动", wait_until(lambda: engines and engines[0].started, 2.0))
        check("启动参数传对了", engines[0].started, ("nspro", "usb", "PA D"))
        check("启动时把记住的参数套上了",
              (engines[0].tuning["gain"], engines[0].tuning["sensitivity"],
               engines[0].tuning["release"], engines[0].tuning["dual"]),
              (1.2, 30.0, 300.0, False))
        check("端点只建一次", endpoint.ensured, 1)
        check_true("状态显示已开启", wait_until(lambda: "已开启" in runner.status, 1.0))
        check("端点名露出来了", runner.endpoint_name, "fake-endpoint")
        check("levels 转发给引擎", runner.levels(), (0.25, 0.5))

        runner.set_value("gain", 0.5)
        check("运行中改强度也生效", engines[0].tuning["gain"], 0.5)
        check("也记住了", runner.tuning()["gain"], 0.5)

        print("\n[11b] 手柄断开：只停引擎，虚拟声卡留着")
        runner.prepare()
        check_true("引擎被停", wait_until(lambda: engines[0].stopped >= 1, 2.0))
        check("停引擎时要求保留端点", engines[0].keep_endpoint, True)
        check("端点没被拆", endpoint.released, 0)
        check_true("状态变成等待手柄",
                   wait_until(lambda: "等待手柄" in runner.status, 2.0))
        check("没有重建设备", endpoint.ensured, 1)

        print("\n[11c] 手柄回来：复用同一个端点")
        runner.request("nspro", "usb", controller="PA D")
        check_true("第二次起来了", wait_until(lambda: len(engines) == 2, 2.0))
        check("端点还是只建过一次", endpoint.ensured, 1)
        check("端点一直没拆", endpoint.released, 0)

        print("\n[11d] 关开关：引擎停 + 拆自己建的端点")
        runner.request_off()
        check_true("引擎停了", wait_until(lambda: engines[1].stopped >= 1, 2.0))
        check_true("端点被拆", wait_until(lambda: endpoint.released >= 1, 2.0))
        check_true("状态回到已关闭", wait_until(lambda: runner.status == "已关闭", 1.0))
        check("线程没死在第一次关闭上（还能再开）", runner.is_alive(), True)
    finally:
        runner.stop()
    check("stop() 之后线程结束", runner.is_alive(), False)

    print("\n[11e] 开关打开、手柄还没到：先只建端点")
    endpoint = FakeEndpoint()
    engines = []

    def late_factory():
        engine = FakeEngine()
        engines.append(engine)
        return engine

    runner = haptics.HapticsRunner(engine_factory=late_factory, endpoint=endpoint)
    try:
        runner.prepare()
        check_true("端点被建出来", wait_until(lambda: endpoint.ensured == 1, 2.0))
        check("还没有引擎", engines, [])
        check_true("状态提示在等手柄",
                   wait_until(lambda: "等待手柄" in runner.status, 2.0))
        runner.request("nspro", "usb", controller="PA D")
        check_true("手柄到了就起引擎",
                   wait_until(lambda: engines and engines[0].started, 2.0))
        check("端点没有重建", endpoint.ensured, 1)
    finally:
        runner.stop()
    check("stop() 会拆端点", endpoint.released >= 1, True)

    print("\n[11f] 端点建不出来：开关必须弹回去")
    endpoint = FakeEndpoint(
        fail_with=haptics.EndpointError("虚拟音频通道创建失败：VIIPER 没在跑"))
    runner = haptics.HapticsRunner(engine_factory=factory, endpoint=endpoint)
    try:
        runner.request("nspro", "usb")
        check_true("标记成失败", wait_until(lambda: runner.failed, 2.0))
        check("错误文字被保留", runner.error,
              "虚拟音频通道创建失败：VIIPER 没在跑")
        check("只尝试了一次（端点问题不重试）", endpoint.ensured, 1)
        check_true("状态里写了原因", "VIIPER" in runner.status)
        runner.failed = False                       # 窗口会把它清掉
        time.sleep(0.2)
        check("清了之后不会自己再试", endpoint.ensured, 1)
    finally:
        runner.stop()

    print("\n[11g] 手柄类问题会重试（且不拆端点、不弹开关）")
    calls = []
    endpoint = FakeEndpoint()

    def flaky_factory():
        calls.append(True)
        return FakeEngine(fail_with=haptics.PadError("没找到手柄"))

    runner = haptics.HapticsRunner(engine_factory=flaky_factory, endpoint=endpoint)
    try:
        runner.request("nspro", "usb")
        check_true("试了至少两次", wait_until(lambda: len(calls) >= 2, 5.0))
        check("但没有标记成致命失败", runner.failed, False)
        check("端点一直留着", endpoint.released, 0)
        check("状态说是等手柄", "等待手柄" in runner.status, True)
    finally:
        runner.stop()


# --------------------------------------------------------------------------- #
# [12] Settings integration
# --------------------------------------------------------------------------- #

def test_settings() -> None:
    print("\n[12] 设置项：十个键、范围与默认值")
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    directory = os.path.join(project, "build", "haptics_test")
    path = os.path.join(directory, "settings.json")
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass
    try:
        if os.path.exists(path):
            os.remove(path)
        tuning = settings.haptics_tuning(path)
        check("默认关", settings.haptics_enabled(path), False)
        check("默认值和引擎的默认值一致", tuning, haptics.DEFAULT_TUNING)
        check("默认强度 100%", tuning["gain"], 1.0)
        check("默认灵敏度 56", tuning["sensitivity"], 56.0)
        check("默认拖尾 140 ms", tuning["release"], 140.0)
        check("默认死区 0.04", tuning["dead_zone"], 0.04)
        check("默认曲线 1.05", tuning["gamma"], 1.05)
        check("默认分界 640", tuning["split"], 640.0)
        check("默认左右分工开", tuning["dual"], True)
        check("默认不交换马达", tuning["swap"], False)

        check("打开会被记住", settings.set_haptics_enabled(True, path), True)
        check("再读是开", settings.haptics_enabled(path), True)

        print("\n[12b] 存一组、读一组，范围与精度")
        stored = settings.save_haptics_tuning({
            "gain": 1.25, "sensitivity": 90.0, "attack": 0.7, "release": 300.0,
            "dead_zone": 0.1, "gamma": 1.8, "split": 400.0, "dual": False,
            "swap": True}, path)
        check("返回值就是存进去的值", stored, {
            "gain": 1.25, "sensitivity": 90.0, "attack": 0.7, "release": 300.0,
            "dead_zone": 0.1, "gamma": 1.8, "split": 400.0, "dual": False,
            "swap": True})
        check("读回一致", settings.haptics_tuning(path), stored)
        check("存一组不会把开关清掉", settings.haptics_enabled(path), True)

        clamped = settings.save_haptics_tuning({
            "gain": 9, "sensitivity": -5, "attack": 5, "release": 5,
            "dead_zone": 9, "gamma": 99, "split": 1}, path)
        check("全部按范围夹住", clamped, {
            "gain": 1.5, "sensitivity": 0.0, "attack": 1.0, "release": 30.0,
            "dead_zone": 0.2, "gamma": 2.2, "split": 250.0, "dual": True,
            "swap": False})
        check("起振下限 0.40", settings.clamp_haptics("attack", 0.1), 0.4)
        check("起振精度两位", settings.clamp_haptics("attack", 0.876), 0.88)
        check("非法输入回默认", settings.clamp_haptics("gain", "abc"), 1.0)
        check("NaN 回默认", settings.clamp_haptics("gamma", float("nan")), 1.05)
        check("拖尾取整到 1 ms", settings.clamp_haptics("release", 123.456), 123.0)
        check("拖尾下限 30 ms", settings.clamp_haptics("release", 1), 30.0)
        check("拖尾上限 900 ms", settings.clamp_haptics("release", 9999), 900.0)
        check("精度：死区三位", settings.clamp_haptics("dead_zone", 0.123456), 0.123)
        check("精度：分界取整", settings.clamp_haptics("split", 641.6), 642.0)

        print("\n[12c] 文件里正好是这十个键")
        import json
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        check("震动相关的键",
              sorted(key for key in raw if key.startswith("haptics_")),
              ["haptics_attack", "haptics_dead_zone", "haptics_dual_motor",
               "haptics_enabled", "haptics_gain", "haptics_gamma",
               "haptics_motor_swap", "haptics_release_ms",
               "haptics_sensitivity",
               "haptics_split"])

        print("\n[12d] 界面和引擎用同一张范围表")
        check("强度：界面上限在引擎允许范围内（引擎放宽是给命令行试验的）",
              (settings.HAPTICS_RANGES["gain"][0] >= haptics.GAIN_MIN,
               settings.HAPTICS_RANGES["gain"][1] <= haptics.GAIN_MAX), (True, True))
        for key, (low, high) in settings.HAPTICS_RANGES.items():
            if key == "gain":
                continue                        # 见上：这两个范围有意不同
            check(f"{key} 的范围与 haptics.RANGES 一致",
                  (low, high), haptics.RANGES[key])
        check("默认值都是引擎认识的数",
              set(settings.haptics_tuning(path)), set(haptics.DEFAULT_TUNING))
        check("默认灵敏度对应的门限约等于旧预设定值",
              abs(haptics.threshold_from_sensitivity(
                  haptics.DEFAULT_TUNING["sensitivity"])
                  - haptics.PRESETS[haptics.PRESET_DEFAULT]["threshold"]) < 1.0, True)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
        try:
            os.rmdir(directory)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# [13] The window wiring (no hardware: demo worker, fake runner)
# --------------------------------------------------------------------------- #

class FakeRunner:
    """Records what the window asks the haptics layer to do."""

    def __init__(self):
        self.requests = []
        self.prepares = 0
        self.offs = 0
        self.values = {}
        self.tuning = None
        self.status = "已关闭"
        self.error = None
        self.failed = False
        self.running = False
        self.stopped = 0
        self.prefer_calls = []

    def prefer_combined(self, value):
        self.prefer_calls.append(bool(value))

    def prepare(self):
        self.prepares += 1

    def request(self, kind, transport, controller=None):
        self.requests.append((kind, transport, controller))

    def request_off(self):
        self.offs += 1
        self.running = False

    def set_tuning(self, values):
        self.tuning = dict(values)

    def set_value(self, key, value):
        self.values[key] = value

    def levels(self):
        return (0.5, 0.25)

    def stop(self):
        self.stopped += 1


def test_gui_wiring() -> None:
    print("\n[13] 界面接线：开关 / 七个滑块 / 左右分工 / 恢复默认")
    try:
        import tkinter as tk

        import gui
    except Exception as exc:                        # pragma: no cover - 无显示环境
        print(f"  !! 跳过界面测试（{exc}）")
        return

    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    scratch_dir = os.path.join(project, "build", "haptics_gui_test")
    os.makedirs(scratch_dir, exist_ok=True)
    settings_file = os.path.join(scratch_dir, "settings.json")
    try:
        os.remove(settings_file)
    except OSError:
        pass
    saved_path = settings.settings_path
    settings.settings_path = lambda: settings_file
    root = None
    app = None
    try:
        root = tk.Tk()
        root.withdraw()
        app = gui.App(root, demo=True)
        app.haptics_runner = FakeRunner()

        check("有总开关", hasattr(app, "haptics_check"), True)
        check("有七个滑块", sorted(app.haptics_scales),
              ["attack", "dead_zone", "gain", "gamma", "release", "sensitivity",
               "split"])
        check("每个滑块都有输入框", sorted(app.haptics_entries),
              ["attack", "dead_zone", "gain", "gamma", "release", "sensitivity",
               "split"])
        check("有左右分工开关", hasattr(app, "haptics_dual_check"), True)
        check("有状态提示", hasattr(app, "haptics_note"), True)
        check("有恢复默认", hasattr(app, "haptics_reset"), True)
        check("预设下拉已经去掉", hasattr(app, "haptics_preset_box"), False)
        check("默认关", app.haptics_var.get(), False)

        print("\n[13b] 默认值与滑块的显示")
        expected = {"gain": "100%", "sensitivity": "56", "attack": "0.85",
                    "release": "140ms", "dead_zone": "4.0%", "gamma": "1.05",
                    "split": "640Hz"}
        check("七个输入框都按单位显示",
              {key: entry.get() for key, entry in app.haptics_entries.items()},
              expected)
        for key, (low, high) in settings.HAPTICS_RANGES.items():
            scale = app.haptics_scales[key]
            check(f"{key} 滑块范围", (float(scale.cget("from")), float(scale.cget("to"))),
                  (low, high))
            check(f"{key} 滑块初始值", round(float(scale.get()), 6),
                  round(float(haptics.DEFAULT_TUNING[key]), 6))

        print("\n[13b2] 每个可调参数都带一行说明")
        texts = []

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, tk.Label):
                    texts.append(str(child.cget("text")))
                walk(child)

        walk(app.footer)
        check("卡片名字是 PS5手柄原生震动", "PS5手柄原生震动" in texts, True)
        check("总开关叫 开启震动", "开启震动" in texts, True)
        check("八条说明都真的画在界面上",
              [hint for hint in gui.HAPTICS_HINTS.values() if hint not in texts], [])
        check("每个滑块都有说明",
              [key for key in app.haptics_scales if key not in gui.HAPTICS_HINTS], [])
        check("左右分工也有说明", gui.HAPTICS_HINTS["dual"] in texts, True)
        check("开关本身没有说明（名字已经说明白了）",
              "把游戏声音变成手柄震动" in texts, False)

        print("\n[13c] 拖滑块：立刻进引擎，松手落盘")
        app.haptics_scales["sensitivity"].set(90)
        app._on_haptics_scale("sensitivity")
        check("灵敏度 90 传给引擎", app.haptics_runner.values["sensitivity"], 90.0)
        check("输入框跟着走", app.haptics_entries["sensitivity"].get(), "90")
        check("值也记在界面状态里", app.haptics_values["sensitivity"], 90.0)
        app._remember_haptics()
        check("松手后写进设置文件",
              settings.haptics_tuning(settings_file)["sensitivity"], 90.0)

        print("\n[13d] 输入框：单位、夹取、垃圾输入")
        for key, text, want_value, want_text in (
                ("gain", "40%", 0.4, "40%"),
                ("gain", "999%", 1.5, "150%"),
                ("attack", "0.95", 0.95, "0.95"),
                ("attack", "0.1", 0.4, "0.40"),
                ("release", "300ms", 300.0, "300ms"),
                ("release", "5ms", 30.0, "30ms"),
                ("release", "999", 900.0, "900ms"),
                ("split", "800Hz", 800.0, "800Hz"),
                ("split", "9999", 900.0, "900Hz"),
                ("dead_zone", "12.5%", 0.125, "12.5%"),
                ("gamma", "1.4", 1.4, "1.40")):
            entry = app.haptics_entries[key]
            entry.delete(0, "end")
            entry.insert(0, text)
            app._on_haptics_entry(key)
            check(f"{key} 输入 {text} -> {want_value}",
                  (app.haptics_values[key], entry.get()), (want_value, want_text))
        entry = app.haptics_entries["gamma"]
        entry.delete(0, "end")
        entry.insert(0, "乱写")
        app._on_haptics_entry("gamma")
        check("乱写保留原值", entry.get(), "1.40")

        print("\n[13e] 恢复默认")
        app.reset_haptics_defaults()
        check("六个值都回到默认",
              {key: app.haptics_values[key]
               for key in list(settings.HAPTICS_KEYS) + ["dual"]},
              {key: haptics.DEFAULT_TUNING[key]
               for key in list(settings.HAPTICS_KEYS) + ["dual"]})
        check("输入框也回到默认",
              {key: entry.get() for key, entry in app.haptics_entries.items()},
              expected)
        saved = settings.haptics_tuning(settings_file)
        check("默认值被写进设置文件", saved["sensitivity"],
              haptics.DEFAULT_TUNING["sensitivity"])

        print("\n[13f] 左右分工")
        app.haptics_dual_var.set(False)
        app.toggle_haptics_dual()
        check("关掉会告诉引擎", app.haptics_runner.values["dual"], False)
        check("也写进设置文件", settings.haptics_tuning(settings_file)["dual"], False)
        app.reset_haptics_defaults()
        check("恢复默认也把开关打开", app.haptics_values["dual"], True)

        print("\n[13g] 开关：没手柄先建端点，有手柄才起引擎")
        check("还没打开时没有请求",
              (app.haptics_runner.requests, app.haptics_runner.prepares), ([], 0))
        app.haptics_var.set(True)
        app.toggle_haptics()
        check("演示手柄不可用 -> 只准备端点", app.haptics_runner.prepares, 1)
        check("没有起引擎", app.haptics_runner.requests, [])
        check("开关状态被记住", settings.haptics_enabled(settings_file), True)

        app.worker.kind = "switch"
        app.worker.controller.transport = "usb"
        app._sync_haptics()
        check("手柄可用后请求引擎", len(app.haptics_runner.requests), 1)
        check("请求的是 NS Pro / 有线", app.haptics_runner.requests[0][:2],
              ("nspro", "usb"))
        check("不再把工作线程的句柄交给引擎（避免 close/write 撞车）",
              app.haptics_runner.requests[0][2], None)

        app.worker.controller.is_open = False
        app._sync_haptics()
        check("手柄句柄关掉后回去等手柄", app.haptics_runner.prepares, 2)
        check("不再请求新引擎", len(app.haptics_runner.requests), 1)

        app.haptics_var.set(False)
        app.toggle_haptics()
        check("关掉要求停引擎并拆端点", app.haptics_runner.offs >= 1, True)
        check("关掉也记住了", settings.haptics_enabled(settings_file), False)

        print("\n[13g2] 只有虚拟 DualSense 模式会把端点让给它（Windows 的 2- 命名）")
        runner = app.haptics_runner
        runner.prefer_calls.clear()
        app.forward_var.set(True)
        app.toggle_forward()
        check("完整转发（DS4）不碰端点归属", runner.prefer_calls, [])
        app.motion_only_var.set(True)
        app.toggle_motion_only()
        check("仅体感也不碰", runner.prefer_calls, [])
        app.ds5_var.set(True)
        app.toggle_ds5()
        check("虚拟 DualSense → 让出去（复用它的音频接口）",
              runner.prefer_calls, [True])
        app.ds5_var.set(False)
        app.toggle_ds5()
        check("离开虚拟 DualSense → 收回来自建",
              runner.prefer_calls, [True, False])
        check("（震动开关本身仍是关的）", app.haptics_var.get(), False)

        print("\n[13h] 起不来时开关必须弹回去")
        app.haptics_var.set(True)
        app.toggle_haptics()
        app.haptics_runner.running = True
        app.haptics_runner.failed = True
        app.haptics_runner.error = "虚拟音频通道创建失败：VIIPER 没在跑"
        app._tick()
        check("开关被弹回关闭", app.haptics_var.get(), False)
        check("设置里也是关", settings.haptics_enabled(settings_file), False)
        check_true("提示里写了原因", "VIIPER" in app.haptics_note.cget("text"))
        check("失败标记被清掉", app.haptics_runner.failed, False)
    finally:
        try:
            if app is not None:
                app.haptics_runner.stop()
                app.worker.stop()
        except Exception:
            pass
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass
        settings.settings_path = saved_path
        try:
            os.remove(settings_file)
        except OSError:
            pass
        try:
            os.rmdir(scratch_dir)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# [14] main.py wiring (the console bridge)
# --------------------------------------------------------------------------- #

def test_main_wiring() -> None:
    print("\n[14] main.py 接线：只有 DS5 后端 + Switch 输入才起震动")
    try:
        import mapping

        import main as main_module
    except Exception as exc:                        # pragma: no cover - 环境相关
        print(f"  !! 跳过 main 接线测试（{exc}）")
        return

    def bridge(backend, haptics_enabled):
        return main_module.Bridge(mapping.MappingConfig(), backend=backend,
                                  haptics_enabled=haptics_enabled)

    check("--haptics 优先于设置文件", bridge("dualsense", True).haptics_wanted(), True)
    check("--no-haptics 也优先", bridge("dualsense", False).haptics_wanted(), False)
    check("不给参数时看设置文件（默认关）", bridge("dualsense", None).haptics_wanted(),
          settings.haptics_enabled())

    check("没开开关就不起引擎",
          bridge("dualsense", False)._start_haptics("switch", FakeNsPro()), None)
    check("vigem 后端说明需要 dualsense",
          bridge("vigem", True)._start_haptics("switch", FakeNsPro()), None)
    check("DS4 输入 + DS5 输出还没做，明确拒绝",
          bridge("dualsense", True)._start_haptics("ds4", FakeDs4()), None)

    # 走完整条启动路径，但把端点、声卡、手柄都换成假的。
    endpoint = FakeEndpoint()
    saved_endpoint = haptics.AudioEndpoint
    saved_pyaudio = haptics._pyaudio
    saved_open = haptics.RumblePad.open
    pad = FakePad()
    opened = []

    def fake_open(kind="nspro", transport="usb", controller=None, info=None,
                  log=None):
        opened.append((kind, transport, controller))
        pad.transport = transport
        return pad

    haptics.AudioEndpoint = lambda **_kwargs: endpoint
    haptics._pyaudio = lambda: FakeAudio()
    haptics.RumblePad.open = fake_open
    engine = None
    try:
        engine = bridge("dualsense", True)._start_haptics("switch", FakeNsPro())
        check_true("真的把引擎起来了", engine is not None)
        check("端点被准备好（复用后端建的那个）", endpoint.ensured, 1)
        check("震动自己开了一个句柄（不借主程序那个）",
              opened and opened[0][2] is None, True)
        check("打开的是 NS Pro / 有线", opened[0][:2], ("nspro", "usb"))
        check_true("状态里带端点名", endpoint.name in engine.status())
    finally:
        if engine is not None:
            engine.stop()
        haptics.AudioEndpoint = saved_endpoint
        haptics._pyaudio = saved_pyaudio
        haptics.RumblePad.open = saved_open
    check("停掉之后自己开的句柄被关掉", pad.closed, True)
    check("停掉之后端点被释放", endpoint.released, 1)


# --------------------------------------------------------------------------- #

def main() -> int:
    test_signal_helpers()
    test_log_index()
    test_frequency_codes()
    test_nspro_payload()
    test_ds4_report()
    test_rumble_pad()
    test_pulse_hold_and_sender()
    test_endpoint_lifecycle()
    test_engine()
    test_runner()
    test_settings()
    test_gui_wiring()
    test_main_wiring()
    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
