"""音频 → 实体手柄震动：产品侧的震动引擎。

链路（每一项都在真手柄上验证过，见 ``HANDOVER-RUMBLE.md`` 第 2 节）：

    虚拟 DualSense 音频端点（回环）
      → 每声道平均成单声道 → 512 点 FFT（48 kHz 下 10.7 ms/块）
      → 低频段 / 高频段 RMS → 门限、gamma、强度
      → 起振 / 释放平滑 → 死区 → 脉冲保持（默认关）
      → 两个 0..1 值 → 后台发送线程 → 四种手柄报文

四种报文（DS4 有线 ``0x05``、DS4 蓝牙精确 78 字节 ``0x11``、NS Pro ``0x10`` +
4 位计数器）在 ``tools/rumble_probe.py`` 里用真手柄定过，这里只照抄，不做"顺手修正"。
几处容易改坏的地方，改之前先读交接报告：

* NS Pro 的 ``0x10`` 报文 **byte 1 是 4 位计数器**，少了它载荷整体左移；
* NS Pro 振幅要用 **100 档对数表**（:func:`_log_index`），仓库原表只到 33 档；
* DS4 蓝牙必须 **精确 78 字节 + 中断管道**，``DualShock4.write_output()`` 会补零；
* 音频 **四声道平均**，双马达 **高频→左、低频→右**；
* HID 写必须在后台线程里（蓝牙单次写会超过一个采集块）。

模块结构：

* :class:`AudioEndpoint` —— 音频端点的生命周期：有就复用，没有就建 audio-only，
  关的时候只拆自己建的；
* :class:`RumblePad` —— 一台物理手柄和它那两种写报文的路径；
* :class:`HapticsEngine` —— 采集线程 + 发送线程 + 参数；
* :class:`HapticsRunner` —— 界面用的薄封装：把慢启动放到后台，开关立刻可响应。

numpy / pyaudiowpatch 都是按需导入的：没有它们时本模块照样可以被 import，
只有真正要开始震动时才报错（:class:`HapticsError`），窗口不会因为缺依赖而起不来。
"""

from __future__ import annotations

import atexit
import glob
import math
import os
import socket
import subprocess
import sys
import threading
import time

import dualshock4
import hidwin
import switchpro
import viiper

try:                                            # 可选依赖：只用真震动时才需要
    import numpy as np
except Exception:                               # noqa: BLE001
    np = None                                   # type: ignore[assignment]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 48 kHz 下 10.7 ms 一块；越小延迟越低，但 FFT 的分辨率也越粗。
CHUNK = 512

#: Switch Pro 的手柄固件不接受比 30 ms 更密的震动报文（SDL 的
#: RUMBLE_WRITE_FREQUENCY_MS）。
NS_PRO_MIN_INTERVAL_S = 0.030
#: 蓝牙 DS4 一次 HID 写要 10–20 ms，按 20 ms 限速就够。
DS4_BT_MIN_INTERVAL_S = 0.020

#: 上升沿（相对上一帧的增量）超过这个值时允许插队，起振才不迟钝。
SEND_LEAD_RISE = 0.10
#: 运行期强制的最小起振系数：源项目的 0.46 手感偏软。
ATTACK_FLOOR = 0.85

#: 停震时重复发几次静音包：有些手柄会锁存上一次的强度（HANDOVER 9.6）。
STOP_REPEATS = 5
STOP_GAP_S = 0.05

#: 总强度倍率的范围。界面（settings.HAPTICS_RANGES["gain"]）用 1.5；引擎本身放宽
#: 一点，命令行工具拿它做手感试验时不至于被卡住（输出最后仍会夹到 0..1）。
GAIN_MIN, GAIN_MAX = 0.0, 2.0

# --------------------------------------------------------------------------- #
# 手动参数（取代"预设下拉"）
#
# 预设里那十几个数只有一部分值得让人拖：能直接说清"调大了会发生什么"的才留。
# 剩下的（intensity / in_max / attack / hold_ms / vocal 上沿）留在引擎里当常数，
# 理由见 HANDOVER 和界面那一节的注释。
# --------------------------------------------------------------------------- #

#: 灵敏度：0 = 最不灵敏（门限 800 RMS），100 = 最灵敏（门限 80 RMS）。
SENSITIVITY_MIN, SENSITIVITY_MAX = 0.0, 100.0
#: 起振：每 10.7 ms 追上目标值的比例。0.85 是验证过的下限（再低起振发木），
#: 但让人自己拖到 0.40 才有"软起振"的余地——默认值仍是 0.85。
ATTACK_MIN, ATTACK_MAX = 0.40, 1.00
#: 拖尾：从当前强度掉到 10% 的时长（毫秒）。越大，余震越长——和滑块方向一致。
RELEASE_MIN, RELEASE_MAX = 30.0, 900.0
#: 死区：低于这个强度就归零，"把背景音滤掉"。
DEAD_ZONE_MIN, DEAD_ZONE_MAX = 0.0, 0.20
#: 曲线形状：<1 更线性、更容易起振，>1 前段更迟钝。
GAMMA_MIN, GAMMA_MAX = 0.6, 2.2
#: 低频/高频的分界（Hz）；两段相邻，改一个另一个跟着走。
SPLIT_MIN, SPLIT_MAX = 250.0, 900.0

#: 一块音频的时长（毫秒），按 48 kHz 的标准值算（CHUNK 512 → 10.67 ms）。
BLOCK_MS = CHUNK * 1000.0 / 48000.0

#: 各手动参数的范围，界面滑块和引擎夹取用的是同一张表。
RANGES = {
    "sensitivity": (SENSITIVITY_MIN, SENSITIVITY_MAX),
    "attack": (ATTACK_MIN, ATTACK_MAX),
    "release": (RELEASE_MIN, RELEASE_MAX),
    "dead_zone": (DEAD_ZONE_MIN, DEAD_ZONE_MAX),
    "gamma": (GAMMA_MIN, GAMMA_MAX),
    "split": (SPLIT_MIN, SPLIT_MAX),
}

#: 滑块 → 预设里的字段名（灵敏度 / 拖尾还要过一遍映射函数）。
_PARAM_FIELDS = {
    "sensitivity": "threshold",
    "attack": "attack",
    "release": "release",
    "dead_zone": "dead_zone",
    "gamma": "gamma",
    "split": "bass_high",
}


def release_from_tail(tail_ms: float) -> float:
    """拖尾时长（毫秒）→ :func:`smooth_signal` 的每块追平比例。

    拖尾定义成"从当前强度掉到 10% 要多久"：每块只剩 ``(1 - r)``，所以
    ``(1 - r) ** 块数 = 0.1``，其中 ``块数 = tail_ms / BLOCK_MS``。
    时长越大 → 比例越小 → 掉得越慢（这正是滑块的直觉方向）。
    """
    try:
        tail = float(tail_ms)
    except (TypeError, ValueError):
        tail = 140.0
    if tail != tail:                            # NaN
        tail = 140.0
    blocks = max(0.05, max(RELEASE_MIN, min(RELEASE_MAX, tail)) / BLOCK_MS)
    return 1.0 - 0.1 ** (1.0 / blocks)


def tail_from_release(release: float) -> float:
    """反函数：每块比例 → 拖尾毫秒（换算滑块位置用）。"""
    try:
        rate = float(release)
    except (TypeError, ValueError):
        return 140.0
    rate = max(1e-4, min(0.9999, rate))
    return math.log(0.1) / math.log(1.0 - rate) * BLOCK_MS

#: 灵敏度刻度的对数底：0 → 800 RMS，100 → 80 RMS。
_SENSITIVITY_LOW, _SENSITIVITY_HIGH = 800.0, 80.0


def threshold_from_sensitivity(value) -> float:
    """0..100 的"灵敏度"→ 触发门限（int16 RMS）。

    对数刻度：0 → 800（几乎只有爆炸才震），100 → 80（很轻的声音也震）。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 56.0
    if number != number:                        # NaN
        number = 56.0
    dial = max(SENSITIVITY_MIN, min(SENSITIVITY_MAX, number)) / 100.0
    return _SENSITIVITY_LOW * (_SENSITIVITY_HIGH / _SENSITIVITY_LOW) ** dial


def sensitivity_from_threshold(threshold) -> float:
    """触发门限 → 灵敏度刻度（:func:`threshold_from_sensitivity` 的反函数）。"""
    try:
        value = max(1e-3, float(threshold))
    except (TypeError, ValueError):
        value = 220.0
    dial = 100.0 * math.log(value / _SENSITIVITY_LOW) \
        / math.log(_SENSITIVITY_HIGH / _SENSITIVITY_LOW)
    return round(max(SENSITIVITY_MIN, min(SENSITIVITY_MAX, dial)), 1)


#: NS Pro 只用高频段：实测低频段整体手感差，同频段内换频率又没区别。
NS_PRO_HIGH_HZ = 320.0

KIND_LABEL = {"ds4": "DS4", "nspro": "NS Pro"}
TRANSPORT_LABEL = {"usb": "有线", "bluetooth": "蓝牙"}

#: 端点名字里的一段（HANDOVER 第 2 节 #13：不是 "Wireless Controller Audio"）。
ENDPOINT_HINTS = ("DualSense Wireless Controller", "Wireless Controller")
#: 只带音频功能的 DualSense 设备类型：不建虚拟手柄，但音频端点同样出现。
AUDIO_ONLY_DEVICE_TYPE = "dualsenseaudioonlyduplexv5"
ENDPOINT_WAIT_S = 20.0
ENDPOINT_POLL_S = 0.5


# --------------------------------------------------------------------------- #
# 预设（源项目 Xbox Audio Rumble Studio 的四组参数，未改动）
# --------------------------------------------------------------------------- #

PRESETS = {
    "人声清晰": dict(dual=True, vocal_only=True, adaptive=True, intensity=0.72,
                 threshold=260.0, in_max=4200.0, gamma=1.28, attack=0.38,
                 release=0.12, dead_zone=0.045, vocal_low=650, vocal_high=3600,
                 bass_high=520),
    "低频沉浸": dict(dual=True, vocal_only=False, adaptive=True, intensity=0.86,
                 threshold=220.0, in_max=5200.0, gamma=1.05, attack=0.46,
                 release=0.16, dead_zone=0.04, vocal_low=600, vocal_high=4000,
                 bass_high=640),
    "轻柔细腻": dict(dual=False, vocal_only=True, adaptive=True, intensity=0.52,
                 threshold=340.0, in_max=4800.0, gamma=1.55, attack=0.24,
                 release=0.08, dead_zone=0.06, vocal_low=700, vocal_high=3300,
                 bass_high=480),
    "全频强劲": dict(dual=False, vocal_only=False, adaptive=False, intensity=1.0,
                 threshold=180.0, in_max=6000.0, gamma=0.9, attack=0.58,
                 release=0.22, dead_zone=0.035, vocal_low=600, vocal_high=4200,
                 bass_high=700),
}
PRESET_DEFAULT = "低频沉浸"

#: 内容识别（auto）时覆盖的参数，照抄源项目的 classify_content 分支。
AUTO_MUSIC = dict(dual=True, vocal_only=False, adaptive=True, in_max=5600.0,
                  gamma=0.92, attack=0.58, release=0.17, dead_zone=0.035)
AUTO_ASMR = dict(dual=False, vocal_only=True, adaptive=True, in_max=2300.0,
                 gamma=1.75, attack=0.20, release=0.045, dead_zone=0.012)
AUTO_QUIET = dict(attack=0.16, release=0.09, dead_zone=0.05, intensity=0.0)

#: 界面滑块的默认值 = 旧「低频沉浸」那一档，换成滑块的刻度表示。改这四个预设的
#: 数值不会影响这里；两者一致由 tools/test_haptics.py 交叉核对。
DEFAULT_TUNING = {
    "gain": 1.0,
    #: 56 → 门限 ≈220，也就是旧「低频沉浸」预设那一档（误差 <1 RMS）。
    "sensitivity": 56.0,
    #: 0.85 = ATTACK_FLOOR，原来运行期强制的下限，现在只是默认值。
    "attack": ATTACK_FLOOR,
    #: 140 ms ≈ 旧「低频沉浸」预设的 0.16 每块比例（掉到 10% 约 141 ms）。
    "release": 140.0,
    "dead_zone": float(PRESETS[PRESET_DEFAULT]["dead_zone"]),       # 0.04
    "gamma": float(PRESETS[PRESET_DEFAULT]["gamma"]),               # 1.05
    "split": float(PRESETS[PRESET_DEFAULT]["bass_high"]),           # 640
    "dual": True,
    "swap": False,
}


# --------------------------------------------------------------------------- #
# 错误
# --------------------------------------------------------------------------- #

class HapticsError(RuntimeError):
    """震动起不来；``str(exc)`` 已经是可以直接显示给用户的一句话。"""


class EndpointError(HapticsError):
    """音频端点那条链路的问题（VIIPER 没起、设备建不出来、端点没出现）。"""


class PadError(HapticsError):
    """物理手柄那条链路的问题（没找到、打不开、写不进去）。"""


# --------------------------------------------------------------------------- #
# 可选依赖
# --------------------------------------------------------------------------- #

def _numpy():
    if np is None:                              # pragma: no cover - 环境相关
        raise HapticsError("缺少 numpy，无法分析音频（pip install numpy）")
    return np


def _pyaudio():
    try:
        import pyaudiowpatch as pyaudio
    except Exception as exc:                    # noqa: BLE001 - 环境相关
        raise HapticsError(
            f"缺少 pyaudiowpatch，读不到音频回环端点（pip install pyaudiowpatch）：{exc}"
        ) from exc
    return pyaudio


#: PortAudio 的 Initialize/Terminate 不是线程安全的：端点轮询（后台线程）和引擎
#: 开流可能同时在两个线程里做这两件事，实测会直接崩（0xC0000005 访问冲突）。
#: 每一段 PyAudio 会话（create → 用 → terminate）都用这一把锁串起来。
_AUDIO_LOCK = threading.RLock()


def _open_audio_stream(pyaudio, channels: int, rate: int, index,
                       chunk: int) -> tuple:
    """Open a capture stream and return ``(PyAudio, stream)``.

    Kept next to :func:`_close_audio` so every PyAudio session in this module goes
    through the same lock (see ``_AUDIO_LOCK``).
    """
    with _AUDIO_LOCK:
        audio = pyaudio.PyAudio()
        try:
            stream = audio.open(
                format=pyaudio.paInt16,
                channels=int(channels),
                rate=int(rate),
                input=True,
                input_device_index=index,
                frames_per_buffer=int(chunk),
            )
        except Exception:
            audio.terminate()
            raise
    return audio, stream


def _close_audio(audio, stream) -> None:
    """Close a stream and terminate its PyAudio instance, in that order."""
    if stream is not None:
        try:
            with _AUDIO_LOCK:
                stream.close()
        except Exception:                       # noqa: BLE001
            pass
    if audio is not None:
        try:
            with _AUDIO_LOCK:
                audio.terminate()
        except Exception:                       # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 纯逻辑：曲线、频段、内容识别
# --------------------------------------------------------------------------- #

def clamp(value, low: float = 0.0, high: float = 1.0):
    return max(low, min(high, value))


def smooth_signal(current: float, target: float, attack: float, release: float) -> float:
    rate = attack if target > current else release
    return current + (target - current) * clamp(rate)


def map_value_gamma(value: float, threshold: float, in_max: float, gamma: float) -> float:
    if value <= threshold:
        return 0.0
    span = max(1.0, in_max - threshold)
    normalized = clamp((value - threshold) / span)
    return normalized ** max(0.1, gamma)


def band_rms(fft_data, freqs, low: float, high: float, n: int) -> float:
    mask = (freqs >= low) & (freqs <= high)
    if not np.any(mask):
        return 0.0
    band_fft = np.zeros_like(fft_data)
    band_fft[mask] = fft_data[mask]
    return float(np.sqrt(np.mean(np.square(np.fft.irfft(band_fft, n=n))) + 1e-10))


def classify_content(total_rms: float, vocal_rms: float, bass_rms: float,
                     transient: float, previous_mode: str) -> str:
    if total_rms < 55:
        return "安静"
    total = max(total_rms, 1.0)
    bass_ratio = bass_rms / total
    vocal_ratio = vocal_rms / total
    music_score = bass_ratio * 1.25 + transient * 1.65
    asmr_score = (vocal_ratio * 1.15
                  + (1.0 - min(transient * 2.4, 1.0)) * 0.55
                  - bass_ratio * 0.45)
    if previous_mode == "音乐":
        music_score += 0.12
    elif previous_mode == "ASMR":
        asmr_score += 0.12
    if music_score > asmr_score + 0.16:
        return "音乐"
    if asmr_score > music_score + 0.10:
        return "ASMR"
    return previous_mode if previous_mode in {"音乐", "ASMR"} else "ASMR"


# --------------------------------------------------------------------------- #
# 报文编码
# --------------------------------------------------------------------------- #

def _frequency_code(hz: float, band: str) -> int:
    """文档里的频率码：round(log2(hz / 10) * 32)，按频段夹住。

    与 :func:`switchpro._rumble_frequency_code` 完全相同（test_haptics 会逐个
    频点核对），这里重写一份只是为了不在产品代码里调用私有函数。
    """
    if band == "high":
        hz = min(1252.0, max(82.0, hz))
    else:
        hz = min(626.0, max(41.0, hz))
    return int(round(math.log2(hz / 10.0) * 32))


def _log_index(level: float) -> int:
    """0..1 → 振幅表 0..100 档（JoyShockLibrary 的 encode*Ampli 曲线）。

    SDL 的 101 行表把 0..65535 铺满 0..100，整段用满 ``0x00..0xC8``；仓库原来的
    ``switchpro.rumble_payload()`` 只到第 33 行（0x42），也就是满量程的三分之一，
    这就是"有区分但很不明显"的原因。
    """
    if level <= 0.0:
        return 0
    level = min(1.0, float(level))
    if level < 0.012:
        row = 1.0
    elif level < 0.112:
        row = round(4 * math.log2(level * 110))
    elif level < 0.225:
        row = round(16 * math.log2(level * 17))
    else:
        row = round(32 * math.log2(level * 8.7))
    return max(0, min(100, int(row)))


def nspro_payload_bands(low_level: float, low_hz: float,
                        high_level: float, high_hz: float) -> bytes:
    """八字节载荷：两个频段的幅度和频率各自给（0 强度 = 该段静音）。"""
    low_row = _log_index(low_level)
    high_row = _log_index(high_level)
    high_byte = 2 * high_row
    low_word = (0x8000 if low_row % 2 else 0) | (0x40 + low_row // 2)
    high_frequency = (_frequency_code(high_hz, "high") - 0x60) * 4
    low_code = _frequency_code(low_hz, "low")
    side = bytes((
        high_frequency & 0xFF,
        # byte 1 = 8 位幅度 + 高频码的第 9 位（bit0）。表里的幅度都是偶数
        # （2*row），bit0 是空的；把幅度掩成 7 位会把 0xC8 变成 0x48，
        # 上半段整个丢掉（HANDOVER 第 4 节 #3）。
        (high_byte & 0xFE) | ((high_frequency >> 8) & 0x01),
        ((low_code - 0x40) & 0x7F) | ((low_word >> 8) & 0x80),
        low_word & 0xFF,
    ))
    return side + side


def nspro_packet(payload: bytes, counter: int) -> bytes:
    """``0x10`` 报文：byte 1 是滚动 4 位计数器，后面才是八字节载荷。"""
    return bytes((switchpro.OUTPUT_REPORT_RUMBLE, counter & 0x0F)) + bytes(payload[:8])


def ds4_report(small: int, big: int, transport: str) -> bytes:
    """一帧把两个马达都设好的输出报文；0..255 直给。

    ``small`` 是小马达（右），``big`` 是大马达（左）。
    """
    small = max(0, min(255, int(round(small))))
    big = max(0, min(255, int(round(big))))

    if transport != "bluetooth":
        report = bytearray(dualshock4.OUTPUT_REPORT_USB_SIZE)    # 32
        report[0] = dualshock4.OUTPUT_REPORT_USB                 # 0x05
        report[1] = 0x07                                         # flags
        report[4] = small
        report[5] = big
        report[6], report[7], report[8] = dualshock4.DEFAULT_LIGHTBAR
        return bytes(report)

    report = bytearray(dualshock4.OUTPUT_REPORT_BT_SIZE)         # 78
    report[0] = dualshock4.OUTPUT_REPORT_BT                      # 0x11
    # 0xC4 = HID + CRC32 + 4 ms 轮询，SDL / dualsense-tester 用的值。
    report[1] = (dualshock4.HWCTL_HID | dualshock4.HWCTL_CRC32
                 | dualshock4.DEFAULT_POLL_INTERVAL_MS)
    report[2] = 0                                                # audio control
    report[3] = dualshock4.VALID_FLAG0_MOTOR | dualshock4.VALID_FLAG0_LED
    report[4] = 0                                                # valid_flag1
    report[6] = small
    report[7] = big
    report[8], report[9], report[10] = dualshock4.DEFAULT_LIGHTBAR
    report[-4:] = dualshock4.output_crc32(bytes(report)).to_bytes(4, "little")
    return bytes(report)


def pad_kind(kind: str) -> str:
    """把输入侧的叫法收敛成 ``"ds4"`` / ``"nspro"``。"""
    text = (kind or "").strip().lower()
    if text in ("ds4", "dualshock4", "dualshock 4", "ps4"):
        return "ds4"
    if text in ("nspro", "switch", "switchpro", "switch pro", "pro"):
        return "nspro"
    raise PadError(f"不支持的输入手柄：{kind!r}")


def normalize_transport(transport: str) -> str:
    """``"bt"`` / ``"bluetooth"`` → ``"bluetooth"``；其余当有线。"""
    text = (transport or "").strip().lower()
    if text in ("bt", "bluetooth", "wireless"):
        return "bluetooth"
    return "usb"


def pad_candidates(kind: str, transport: str | None = None) -> list[dict]:
    """当前接在 ``transport`` 上的同型号手柄接口；``None`` 表示不挑传输。"""
    kind = pad_kind(kind)
    if kind == "ds4":
        found = dualshock4.find_ds4_interfaces()
    else:
        found = switchpro.find_controller_interfaces()
    if transport is None:
        return list(found)
    want = normalize_transport(transport)
    return [info for info in found if hidwin.transport_of(info) == want]


def interval_for(kind: str, transport: str) -> float:
    """两次 HID 写之间的最小间隔。"""
    if pad_kind(kind) == "nspro":
        return NS_PRO_MIN_INTERVAL_S
    if normalize_transport(transport) == "bluetooth":
        return DS4_BT_MIN_INTERVAL_S
    return 0.0


def _quiet(_message: str) -> None:
    """吞掉底层库的碎话，节奏由调用方自己打。"""


# --------------------------------------------------------------------------- #
# 发送侧
# --------------------------------------------------------------------------- #

class PulseHold:
    """把短脉冲保持几十毫秒，让转子马达起得来。

    默认关闭：把峰值硬按住再掉到 0，在真手柄上手感发粘（用户实测"效果太差"）。
    留着是给 ``--hold-ms`` 做试验的。
    """

    __slots__ = ("hold_s", "peak", "until")

    def __init__(self, hold_s: float = 0.0):
        self.hold_s = max(0.0, float(hold_s))
        self.peak = 0.0
        self.until = 0.0

    def update(self, value: float, now: float) -> float:
        if self.hold_s <= 0.0:
            return value
        if value > 0.0 and (value >= self.peak or now >= self.until):
            self.peak = value
            self.until = now + self.hold_s
        if now < self.until:
            return max(value, self.peak)
        return value


class LevelSender(threading.Thread):
    """在后台线程里写 HID。

    ``HidDevice.write()`` 是同步的重叠 WriteFile：它要等蓝牙栈收下这条报文。
    蓝牙 DS4 上这个等待会超过一个采集块（10.7 ms），于是采集循环越跑越落后、
    回环缓冲越积越多，感觉就是延迟越来越大（HANDOVER 第 4 节 #6）。Switch 没有
    这个问题只是因为它的写被限速在 30 ms。

    邮箱里只留最新一组值：最多多等一次写的时间，而不是一个永远追不上的队列。
    """

    def __init__(self, pad, min_interval: float, lead_rise: float, log=None):
        super().__init__(daemon=True, name="haptics-sender")
        self.pad = pad
        self.min_interval = float(min_interval)
        self.lead_rise = float(lead_rise)
        self.log = log or _quiet
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._latest = (0.0, 0.0)
        self._last = (0.0, 0.0)
        self._last_time = 0.0
        self.error: Exception | None = None
        self.sent = 0

    def set_levels(self, left: float, right: float) -> None:
        with self._lock:
            self._latest = (float(left), float(right))
        self._wake.set()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()

    def run(self) -> None:
        while not self._halt.is_set():
            self._wake.wait(0.05)
            self._wake.clear()
            if self._halt.is_set():
                return
            with self._lock:
                left, right = self._latest
            now = time.monotonic()
            due = (self.min_interval <= 0.0
                   or now - self._last_time >= self.min_interval)
            rise = max(left, right) - max(self._last[0], self._last[1])
            if not due and rise <= self.lead_rise:
                continue
            try:
                self.pad.set_levels(left, right)
            except Exception as exc:                # noqa: BLE001 - 交给上层显示
                self.error = exc
                return
            self._last = (left, right)
            self._last_time = now
            self.sent += 1
            # 只有真的发出去了才算数：上层据此判断"手柄还在不在"。


# --------------------------------------------------------------------------- #
# 一台物理手柄
# --------------------------------------------------------------------------- #

class RumblePad:
    """一台物理手柄，以及它认的那两种写报文路径。

    传入 ``controller`` 时（主程序已经打开了手柄）只借用，不关闭；否则自己打开
    一个句柄（``owns=True``），关闭时自己关掉。
    """

    def __init__(self, kind: str, transport: str, pad=None, controller=None,
                 owns: bool = False, log=None):
        self.kind = pad_kind(kind)
        self.transport = normalize_transport(transport)
        self._ds4 = pad
        self._nspro = controller
        self._owns = owns
        self._log = log or _quiet
        self._counter = 0
        self.closed = False

    # -- 打开 ------------------------------------------------------------- #

    @classmethod
    def open(cls, kind: str, transport: str = "usb", controller=None,
             info: dict | None = None, log=None) -> "RumblePad":
        kind = pad_kind(kind)
        log = log or _quiet
        if controller is not None:
            # 主程序已经握着这个手柄：用它自己的句柄，别开第二个。
            actual = getattr(controller, "transport", None)
            if not actual or actual == "unknown":
                actual = transport
            pad = cls(kind, actual, owns=False, log=log)
            if kind == "ds4":
                pad._ds4 = controller
            else:
                pad._nspro = controller
            return pad

        wanted = normalize_transport(transport)
        found = pad_candidates(kind, wanted)
        if not found:
            # 型号在、只是传输对不上（比如 hidwin 认不出 USB/BTHENUM 祖先）：
            # 退回到"这台机器上有的那一台"，免得因为一个枚举细节完全不震。
            found = pad_candidates(kind, None)
            if found:
                wanted = normalize_transport(hidwin.transport_of(found[0]))
        if not found:
            raise PadError(
                f"没找到 {KIND_LABEL[kind]}（{TRANSPORT_LABEL.get(wanted, wanted)}）手柄，"
                "先连上手柄或取消震动")
        info = info or found[0]

        if kind == "ds4":
            pad = dualshock4.DualShock4(path=info["path"], info=info, log=_quiet).open()
            rumble_pad = cls(kind, wanted, pad=pad, owns=True, log=log)
            if rumble_pad.transport == "bluetooth":
                rumble_pad._request_extended_mode()
            return rumble_pad

        ctl = switchpro.SwitchProController(
            path=info["path"], info=info, log=_quiet).open()
        rumble_pad = cls(kind, wanted, controller=ctl, owns=True, log=log)
        try:
            # 震动本身只走 0x10，不需要 0x30 数据流；初始化失败也照样能震。
            ctl.initialize()
        except Exception as exc:                    # noqa: BLE001
            log(f"NS Pro 初始化告警（继续）：{exc}")
        return rumble_pad

    def _request_extended_mode(self) -> None:
        """蓝牙 DS4 先切到 0x11 报文模式（精确 78 字节，走控制通道请求）。"""
        assert self._ds4 is not None
        request = dualshock4.build_extended_mode_report()
        try:
            sent = bool(self._ds4._device.set_output_report(request))
        except (OSError, AttributeError):
            sent = self._ds4.request_extended_mode()
        self._log(f"蓝牙 DS4 请求扩展模式：{'已发送' if sent else '发送失败'}")
        time.sleep(0.35)

    # -- 写 --------------------------------------------------------------- #

    def set_levels(self, left: float, right: float) -> None:
        """两个马达独立驱动，强度 0..1（左 = 大马达 / 右 = 小马达）。"""
        if self.closed:
            return
        left = clamp(float(left))
        right = clamp(float(right))
        if self._ds4 is not None:
            self._write_ds4(ds4_report(round(right * 255), round(left * 255),
                                       self.transport))
            return
        if self._nspro is None:
            raise PadError("手柄已经关闭了")
        # 只开高频段：实测高频段整体手感更好，低频段振幅置 0。
        side_left = nspro_payload_bands(0.0, switchpro.RUMBLE_LOW_HZ,
                                        left, NS_PRO_HIGH_HZ)[:4]
        side_right = nspro_payload_bands(0.0, switchpro.RUMBLE_LOW_HZ,
                                         right, NS_PRO_HIGH_HZ)[:4]
        self._write_nspro(side_left + side_right)

    def _write_ds4(self, data: bytes) -> None:
        assert self._ds4 is not None
        if self.transport == "bluetooth":
            # HANDOVER #7：蓝牙必须是**精确 78 字节**写在**中断管道**上。
            # write_output() 会补零到 Windows 声明的 547 字节、并且走控制通道，
            # 两者都试过：手柄丢弃报文，一点都不震。
            device = getattr(self._ds4, "_device", None)
            if device is None:
                raise PadError("DS4 已经关闭了")
            device.write(data)
            return
        self._ds4.write_output(data)

    def _write_nspro(self, payload: bytes) -> None:
        assert self._nspro is not None
        counter = self._counter
        self._counter = (self._counter + 1) & 0x0F
        writer = getattr(self._nspro, "set_rumble_payload_counted", None)
        if writer is not None:
            writer(payload, counter)
        else:                                       # pragma: no cover - 旧模块兜底
            self._nspro._write(nspro_packet(payload, counter))

    # -- 停 / 关 ---------------------------------------------------------- #

    def silence(self, repeats: int = STOP_REPEATS) -> None:
        """发几次静音包；有些手柄会锁存上一次的强度，一次停不下来。"""
        for _ in range(max(1, int(repeats))):
            try:
                if self._ds4 is not None:
                    self._write_ds4(ds4_report(0, 0, self.transport))
                elif self._nspro is not None:
                    # 全零，不是文档里的 0x40 中立：克隆手柄把 0x40 当四分之一。
                    self._write_nspro(switchpro.RUMBLE_SILENCE)
            except Exception:                       # noqa: BLE001 - 停震别再抛
                return
            time.sleep(STOP_GAP_S)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._owns:
            for target in (self._ds4, self._nspro):
                if target is not None:
                    try:
                        target.close()
                    except Exception:               # noqa: BLE001
                        pass
        self._ds4 = None
        self._nspro = None

    def describe(self) -> str:
        return (f"{KIND_LABEL[self.kind]} · "
                f"{TRANSPORT_LABEL.get(self.transport, self.transport)}")


# --------------------------------------------------------------------------- #
# 音频端点
# --------------------------------------------------------------------------- #

#: 本进程建出来的端点，退出时兜底拆掉（被强杀时当然没机会跑）。
_LIVE_ENDPOINTS: list["AudioEndpoint"] = []
_LIVE_LOCK = threading.Lock()


def _release_all_endpoints() -> None:
    with _LIVE_LOCK:
        endpoints = list(_LIVE_ENDPOINTS)
        _LIVE_ENDPOINTS.clear()
    for endpoint in endpoints:
        try:
            endpoint.release()
        except Exception:                           # noqa: BLE001
            pass


atexit.register(_release_all_endpoints)


def loopback_devices() -> list[dict]:
    """当前系统里所有回环（loopback）设备。"""
    pyaudio = _pyaudio()
    with _AUDIO_LOCK:
        audio = pyaudio.PyAudio()
        try:
            found = []
            for index in range(audio.get_device_count()):
                info = audio.get_device_info_by_index(index)
                if info.get("isLoopbackDevice", False):
                    found.append(info)
            return found
        finally:
            audio.terminate()


def find_endpoint_device(hints=ENDPOINT_HINTS) -> dict | None:
    """名字里含 ``hints`` 中任意一段的第一个回环设备。"""
    devices = loopback_devices()
    for hint in hints:
        needle = hint.lower()
        for info in devices:
            if needle in str(info.get("name", "")).lower():
                return info
    return None


def viiper_port_open(host: str = viiper.DEFAULT_HOST, port: int = viiper.DEFAULT_PORT,
                     timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_viiper(seconds: float, host: str = viiper.DEFAULT_HOST,
                    port: int = viiper.DEFAULT_PORT) -> bool:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if viiper_port_open(host, port):
            return True
        time.sleep(0.25)
    return False


def find_server_exe() -> str | None:
    """项目目录 / 工作区目录里的 ``VIIPER-*.exe``（与排障工具同一套找法）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for root in (here, os.path.dirname(here)):
        matches = sorted(glob.glob(os.path.join(root, "VIIPER-*.exe")))
        if matches:
            return matches[-1]
    return None


def _creation_flags() -> int:
    """窗口版程序里别弹一个控制台出来；源码运行时留着控制台方便看报错。"""
    if getattr(sys, "frozen", False):
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


def launch_viiper_server(exe: str | None = None) -> subprocess.Popen | None:
    exe = exe or find_server_exe()
    if not exe or not os.path.exists(exe):
        return None
    return subprocess.Popen(
        [exe, "server", "--log.level=info"],
        cwd=os.path.dirname(exe),
        creationflags=_creation_flags(),
    )


class AudioEndpoint:
    """虚拟 DualSense 的音频端点：开关打开时要保证它在，关掉时只拆自己建的。

    流程（HANDOVER 第 5 节第 3 步）：

    1. 枚举回环设备，找名字含 ``ENDPOINT_HINTS`` 的；
    2. 找到就复用 —— 它可能来自正在运行的 DS5 后端，**不能拆**；
    3. 没有就先看 VIIPER 在不在（127.0.0.1:3242），没在就尝试拉起来；
    4. ``bus/create`` → ``add_device("dualsenseaudioonlyduplexv5")``；
    5. 挂上设备流并保持（audio-only 没有输入帧要发，流开着端点就不会消失）；
    6. 轮询等 Windows 把端点暴露出来（最多 ``wait_s`` 秒）；
    7. 就绪后引擎才起采集。

    构造函数的若干 ``*_fn`` 参数是给测试用的注入点：换掉"怎么列设备""端口通不通"
    "怎么建设备流"，就能在没有声卡、没有 VIIPER 的情况下把整条生命周期跑一遍。
    """

    def __init__(self, server=None, hints=ENDPOINT_HINTS,
                 device_type: str = AUDIO_ONLY_DEVICE_TYPE, log=None,
                 wait_s: float = ENDPOINT_WAIT_S, poll_s: float = ENDPOINT_POLL_S,
                 allow_server_start: bool = True, allow_create: bool = True,
                 server_exe: str | None = None,
                 list_fn=None, port_fn=None, launch_fn=None,
                 stream_fn=None, sleep_fn=None):
        self.server = server if server is not None else viiper.ViiperServer()
        self.hints = tuple(hints)
        self.device_type = device_type
        self.log = log or _quiet
        self.wait_s = float(wait_s)
        self.poll_s = max(0.05, float(poll_s))
        self.allow_server_start = bool(allow_server_start)
        #: False = 只复用别人建的端点（DS5 后端那条路径），绝不自己再建一个。
        self.allow_create = bool(allow_create)
        self.server_exe = server_exe
        self._list = list_fn or loopback_devices
        self._port_open = port_fn or (lambda: viiper_port_open())
        self._launch = launch_fn or (lambda: launch_viiper_server(self.server_exe))
        self._stream_fn = stream_fn or (
            lambda server, bus_id, device_id: viiper.DeviceStream(
                server, bus_id, device_id, log=self.log))
        self._sleep = sleep_fn or time.sleep

        self._device: dict | None = None
        self._name = ""
        self._owned = False
        self._bus_id: int | str | None = None
        self._device_id: str | None = None
        self._stream = None
        self._server_process = None
        self._failed_server = False

    # -- 查询 ------------------------------------------------------------- #

    def _matches(self, info: dict) -> bool:
        name = str(info.get("name", "")).lower()
        return any(hint.lower() in name for hint in self.hints)

    def find(self) -> dict | None:
        """第一个名字匹配的回环设备；枚举本身失败按端点错误上报。"""
        try:
            devices = self._list() or []
        except EndpointError:
            raise
        except HapticsError as exc:                 # 缺 pyaudiowpatch 之类
            raise EndpointError(str(exc)) from exc
        except Exception as exc:                    # noqa: BLE001 - 声卡层什么都可能抛
            raise EndpointError(f"读不到音频回环设备：{exc}") from exc
        for info in devices:
            if self._matches(info):
                return info
        return None

    @property
    def device(self) -> dict | None:
        """找到/建好的回环设备信息（``index`` / ``maxInputChannels`` / 采样率）。"""
        return self._device

    @property
    def name(self) -> str:
        return self._name or (self._device or {}).get("name", "")

    @property
    def owned(self) -> bool:
        """这个端点的设备是本进程建的（关的时候要拆掉）吗？"""
        return self._owned

    # -- 生命周期 --------------------------------------------------------- #

    def ensure(self) -> str:
        """保证端点存在；返回端点名。失败抛 :class:`EndpointError`。"""
        found = self.find()
        if found is not None:
            if self._owned and self._device is not None:
                # 自己建的那台还在：刷新索引（重枚举后 index 会变），所有权不变。
                self._device = found
                self._name = str(found.get("name", ""))
                return self._name
            return self._adopt(found)
        if self._owned:
            # 自己建的那台被外部拆掉了（VIIPER 重启、设备被移除）：收干净再重建。
            self.log("自己建的音频端点已经不见了，重新创建")
            self.release()

        if not self.allow_create:
            # 后端正在建自己的带手柄设备时，端点要过一会儿才出现；这段时间只能
            # 等，不能再建一个 audio-only 设备（那会在系统里留第二份虚拟声卡）。
            found = self._wait_for_endpoint()
            if found is not None:
                return self._adopt(found)
            raise EndpointError(
                "音频端点没有出现：虚拟 DualSense 的音频接口还没被 Windows 认出来"
                f"（等了 {int(self.wait_s)} 秒）")

        if not self._port_open():
            if not self.allow_server_start or self._failed_server:
                raise EndpointError(
                    "虚拟音频通道创建失败：VIIPER 服务没有运行"
                    "（先运行 启动-VIIPER服务.bat，或用 --start-server 自动启动）")
            exe = self.server_exe or find_server_exe()
            if exe is None:
                raise EndpointError(
                    "虚拟音频通道创建失败：没找到 VIIPER-*.exe，无法自动启动服务")
            self.log(f"VIIPER 没在运行，启动：{os.path.basename(exe)}")
            self._server_process = self._launch()
            if self._server_process is None or not wait_for_viiper(20.0):
                self._failed_server = True
                self._stop_own_server()
                raise EndpointError("虚拟音频通道创建失败：VIIPER 服务没有起来")
            self.log("VIIPER 服务已就绪")

        last_error = None
        for attempt in (1, 2):
            try:
                self._bus_id = self.server.create_bus()
                # 从这一刻起，建出来的东西都算自己的：失败时 release() 要收干净。
                self._owned = True
                added = self.server.add_device(self._bus_id, self.device_type)
                self._device_id = str(added["devId"])
                # audio-only 设备没有输入状态要发，但流得一直挂着，端点才不会消失。
                self._stream = self._stream_fn(self.server, self._bus_id,
                                               self._device_id)
                self._stream.open()
                last_error = None
                break
            except Exception as exc:                # noqa: BLE001 - 统一成一句话
                self.release()
                last_error = exc
                if attempt == 1:
                    # usbip 偶尔在上一台设备刚拆掉时拒绝立刻挂新设备
                    # （"Failed to auto-attach device ... native attach failed"），
                    # 换一个新 bus 再试一次通常就好了。
                    self.log(f"第一次建设备失败（{exc}），换一个 bus 重试")
                    self._sleep(0.5)
        if last_error is not None:
            raise EndpointError(f"虚拟音频通道创建失败：{last_error}") from last_error

        with _LIVE_LOCK:
            if self not in _LIVE_ENDPOINTS:
                _LIVE_ENDPOINTS.append(self)
        self.log(f"已创建设备 {self.device_type}（bus={self._bus_id} "
                 f"dev={self._device_id}），等待音频端点")

        found = self._wait_for_endpoint()
        if found is None:
            self.release()
            raise EndpointError(
                f"虚拟音频通道创建失败：{int(self.wait_s)} 秒内没有出现音频端点"
                "（USB/IP 没连上、声卡驱动没装好，或端点名字不含 "
                f"{self.hints[0]!r}）")
        self._device = found
        self._name = str(found.get("name", ""))
        self.log(f"音频端点就绪：{self._name}")
        return self._name

    def _adopt(self, found: dict) -> str:
        """复用一个已经存在的端点（不是本程序建的，所以关的时候不能拆）。"""
        self._device = found
        self._owned = False
        self._name = str(found.get("name", ""))
        self.log(f"音频端点已存在，直接复用：{self._name}")
        return self._name

    def _wait_for_endpoint(self) -> dict | None:
        deadline = time.monotonic() + max(0.0, self.wait_s)
        while True:
            found = self.find()
            if found is not None:
                return found
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return None
            self._sleep(min(self.poll_s, remaining))

    def release(self) -> None:
        """关掉自己建的设备/服务；复用来的端点只清掉本地状态，绝不拆。"""
        stream, bus_id, device_id = self._stream, self._bus_id, self._device_id
        owned = self._owned
        self._stream = self._bus_id = self._device_id = None
        self._owned = False
        self._device = None
        self._name = ""
        with _LIVE_LOCK:
            if self in _LIVE_ENDPOINTS:
                _LIVE_ENDPOINTS.remove(self)
        if stream is not None:
            try:
                stream.close()
            except Exception:                       # noqa: BLE001
                pass
        if owned:
            if bus_id is not None and device_id is not None:
                try:
                    self.server.remove_device(bus_id, device_id)
                except Exception:                   # noqa: BLE001
                    pass
            if bus_id is not None:
                try:
                    self.server.remove_bus(bus_id)
                except Exception:                   # noqa: BLE001
                    pass
            self.log("音频端点已拆除（本程序建的）")
        self._stop_own_server()

    def _stop_own_server(self) -> None:
        process, self._server_process = self._server_process, None
        if process is not None:
            try:
                process.terminate()
            except Exception:                       # noqa: BLE001
                pass

    def __enter__(self) -> "AudioEndpoint":
        self.ensure()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


# --------------------------------------------------------------------------- #
# 界面参数 → 引擎
# --------------------------------------------------------------------------- #

def apply_tuning(engine, values: dict) -> dict:
    """把一组滑块参数套到引擎上，返回实际生效的值（已被夹取）。

    ``main.py`` 和窗口都用这一条路径，免得两处各写一遍赋值顺序。
    """
    values = dict(values or {})
    applied = {
        "gain": engine.set_gain(values.get("gain", DEFAULT_TUNING["gain"])),
        "sensitivity": engine.set_sensitivity(
            values.get("sensitivity", DEFAULT_TUNING["sensitivity"])),
        "attack": engine.set_attack(values.get("attack", DEFAULT_TUNING["attack"])),
        "release": engine.set_release(
            values.get("release", DEFAULT_TUNING["release"])),
        "dead_zone": engine.set_dead_zone(
            values.get("dead_zone", DEFAULT_TUNING["dead_zone"])),
        "gamma": engine.set_gamma(values.get("gamma", DEFAULT_TUNING["gamma"])),
        "split": engine.set_split(values.get("split", DEFAULT_TUNING["split"])),
    }
    engine.set_dual(values.get("dual", DEFAULT_TUNING["dual"]))
    engine.set_swap(values.get("swap", DEFAULT_TUNING["swap"]))
    applied["dual"] = values.get("dual", DEFAULT_TUNING["dual"])
    applied["swap"] = values.get("swap", DEFAULT_TUNING["swap"])
    return applied


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #

class HapticsEngine:
    """采集音频 → 算左右强度 → 驱动一台物理手柄。

    ``start()`` 会先把音频端点准备好（没有就建），再打开手柄、起两个线程：
    采集线程算强度，发送线程写报文。``stop()`` 的顺序与 HANDOVER 一致：
    停采集 → 发静音包 → 停发送线程 → 关音频流 → （只拆自己建的）端点。
    """

    def __init__(self, endpoint: AudioEndpoint | None = None, log=None,
                 chunk: int = CHUNK, preset: str | None = None, gain: float = 1.0,
                 dual=True, swap: bool = False, auto: bool = False,
                 legacy: bool = False, hold_ms: float = 0.0,
                 attack: float | None = None, pad_factory=None,
                 owns_endpoint: bool = True):
        self.log = log or _quiet
        self.endpoint = endpoint if endpoint is not None else AudioEndpoint(log=self.log)
        #: 端点是不是这个引擎该管的：'tool'/主程序那条路径自己建、自己拆；
        #: 界面那条路径的端点归 HapticsRunner（软件会期），引擎只借用，
        #: 无论启动失败还是 stop() 都不许拆掉它。
        self._owns_endpoint = bool(owns_endpoint)
        #: 打开物理手柄的那一步；测试可以注入假手柄，真实现就是 RumblePad.open。
        self._pad_factory = pad_factory or RumblePad.open
        self.on_status = None                   # callable(str)，命令行工具用来打日志
        self.error: Exception | None = None

        self._lock = threading.RLock()
        self._halt = threading.Event()
        self._pad: RumblePad | None = None
        self._sender: LevelSender | None = None
        self._thread: threading.Thread | None = None
        self._audio = None
        self._stream = None
        self._kind = "nspro"
        self._transport = "usb"
        self._chunk = max(64, int(chunk))
        self._rate = 48000
        self._channels = 1
        self._running = False
        self._enabled = False
        self._status = "未启动"
        self._next_report = 0.0

        self._preset = preset if preset in PRESETS else PRESET_DEFAULT
        #: 引擎当前生效的参数（由预设初始化，之后由各个 set_* 手动改）。
        self._params = dict(PRESETS[self._preset])
        #: 界面滑块的当前刻度（非法输入时用来保持原值，也方便反查）。
        self._dials = dict(DEFAULT_TUNING)
        self._sync_dials_from_params()
        self._gain = clamp(gain, GAIN_MIN, GAIN_MAX)
        #: True/False = 用户定的左右分工；None = 跟着预设走（命令行工具的旧行为）。
        self._dual = None if dual is None else bool(dual)
        self._swap = bool(swap)
        self._auto = bool(auto)
        self._legacy = bool(legacy)
        self._hold_s = 0.0 if legacy else max(0.0, float(hold_ms)) / 1000.0
        self._attack = attack
        self._reset_dsp()

    # -- 参数 ------------------------------------------------------------- #

    def _reset_dsp(self) -> None:
        self._left = 0.0
        self._right = 0.0
        self._hold_left = PulseHold(self._hold_s)
        self._hold_right = PulseHold(self._hold_s)
        self._noise = 0.0
        self._energy = 0.0
        self._transient = 0.0
        self._mode = "识别中"
        self._total_rms = 0.0
        self._bass_rms = 0.0
        self._vocal_rms = 0.0
        self._threshold = 0.0
        self._level = (0.0, 0.0)

    @property
    def preset(self) -> str:
        return self._preset

    @property
    def gain(self) -> float:
        return self._gain

    @property
    def running(self) -> bool:
        return self._running

    @property
    def endpoint_name(self) -> str:
        return self.endpoint.name

    def set_preset(self, name: str) -> str:
        """套用一整套预设（命令行工具用；界面已经改成手动参数了）。"""
        self._preset = name if name in PRESETS else PRESET_DEFAULT
        self._params = dict(PRESETS[self._preset])
        self._sync_dials_from_params()
        return self._preset

    def _sync_dials_from_params(self) -> None:
        """让滑块刻度跟上当前参数（构造 / set_preset 之后调）。"""
        self._dials.update({
            "sensitivity": sensitivity_from_threshold(self._params["threshold"]),
            "attack": self._params["attack"],
            "release": tail_from_release(self._params["release"]),
            "dead_zone": self._params["dead_zone"],
            "gamma": self._params["gamma"],
            "split": float(self._params["bass_high"]),
        })

    def params(self) -> dict:
        """当前生效的一整套参数（只读副本，测试和排障用）。"""
        return dict(self._params)

    def set_gain(self, value) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return self._gain
        if number != number:                        # NaN
            return self._gain
        self._gain = clamp(number, GAIN_MIN, GAIN_MAX)
        return self._gain

    # -- 手动参数（界面的滑块） ------------------------------------------- #

    def _dial(self, key: str, value) -> float:
        """把滑块值夹进该参数的范围；非法输入保持上一次的刻度。"""
        low, high = RANGES[key]
        current = self._dials.get(key, DEFAULT_TUNING[key])
        try:
            number = float(value)
        except (TypeError, ValueError):
            return float(current)
        if number != number:                        # NaN
            return float(current)
        return max(low, min(high, number))

    def set_sensitivity(self, value) -> float:
        """0..100 的灵敏度 → 触发门限（对数：越大越容易震）。"""
        dial = self._dial("sensitivity", value)
        self._dials["sensitivity"] = dial
        self._params["threshold"] = threshold_from_sensitivity(dial)
        return dial

    def set_attack(self, value) -> float:
        """起振快慢（每块追上目标的比例）。越大越"硬"，越小越"软"。

        设过之后就按这个值来，不再套 0.85 的下限；没设过（还没拖过滑块）时
        仍然用预设值 + 那个下限，命令行工具的手感不受影响。
        """
        dial = self._dial("attack", value)
        self._dials["attack"] = dial
        self._params["attack"] = dial
        self._attack = dial
        return dial

    def set_release(self, value) -> float:
        """拖尾时长（毫秒）：越大掉得越慢，余震越长。"""
        dial = self._dial("release", value)
        self._dials["release"] = dial
        self._params["release"] = release_from_tail(dial)
        return dial

    def set_dead_zone(self, value) -> float:
        """死区：低于这个强度直接归零。"""
        dial = self._dial("dead_zone", value)
        self._dials["dead_zone"] = dial
        self._params["dead_zone"] = dial
        return dial

    def set_gamma(self, value) -> float:
        """曲线形状：<1 更线性，>1 前段更迟钝。"""
        dial = self._dial("gamma", value)
        self._dials["gamma"] = dial
        self._params["gamma"] = dial
        return dial

    def set_split(self, value) -> float:
        """低频/高频的分界（Hz）：两段相邻，低频上沿与高频下沿一起动。"""
        dial = self._dial("split", value)
        self._dials["split"] = dial
        self._params["bass_high"] = dial
        self._params["vocal_low"] = dial
        return dial

    def set_dual(self, value) -> None:
        """左右分工：开 = 高频→左、低频→右；关 = 两个马达同一强度。

        ``None`` 表示跟着预设自己的 ``dual`` 走（命令行工具用）。
        """
        self._dual = None if value is None else bool(value)

    def set_swap(self, value) -> None:
        """交换两个马达（低频→左）。默认关：用户选的是源项目的原版分配。"""
        self._swap = bool(value)

    def set_auto(self, value) -> None:
        """内容识别自动切参数（源项目的默认行为，产品里默认关）。"""
        self._auto = bool(value)

    def set_enabled(self, value) -> None:
        """静音/恢复输出；采集线程继续跑，所以恢复时没有启动延迟。"""
        self._enabled = bool(value)
        if not self._enabled and self._pad is not None:
            sender = self._sender
            if sender is not None:
                # 邮箱里可能还压着一条非零值：不先冲掉，静音之后它还会被写出去
                # 一帧，手柄就一直停在那个强度上。
                sender.set_levels(0.0, 0.0)
            try:
                self._pad.silence(repeats=2)
            except Exception:                       # noqa: BLE001
                pass
            self._level = (0.0, 0.0)

    def levels(self) -> tuple[float, float]:
        """最近一块的左右强度，给界面画电平用（静音时就是 0/0）。"""
        return self._level if self._enabled else (0.0, 0.0)

    def status(self) -> str:
        return self._status

    def status_line(self) -> str:
        left, right = self._level
        return (f"{self._mode:4s} L={left:.2f} R={right:.2f} "
                f"| rms {self._total_rms:6.1f} 低 {self._bass_rms:6.1f} "
                f"人声 {self._vocal_rms:6.1f} | 阈值 {self._threshold:6.1f}")

    # -- 启停 ------------------------------------------------------------- #

    def start(self, kind: str = "nspro", transport: str = "usb",
              controller=None) -> str:
        """准备好端点与手柄并开始震动；返回音频端点名。失败抛 HapticsError。"""
        with self._lock:
            if self._running:
                return self.endpoint.name
        try:                                        # 缺依赖属于"根本起不来"
            _numpy()
            pyaudio = _pyaudio()
        except HapticsError as exc:
            raise EndpointError(str(exc)) from exc
        self.error = None
        self._status = "正在准备音频端点…"
        pad = None
        audio = None
        stream = None
        try:
            device = self.endpoint.ensure()
            info = self.endpoint.device or {}
            if not info:
                raise EndpointError(f"音频端点不可用：{device!r}")
            self._kind = pad_kind(kind)
            self._transport = normalize_transport(transport)
            pad = self._pad_factory(self._kind, self._transport, controller=controller,
                                    log=self.log)
            self._transport = getattr(pad, "transport", self._transport)
            self._channels = max(1, int(info.get("maxInputChannels") or 1))
            self._rate = int(info.get("defaultSampleRate") or 48000)
            audio, stream = _open_audio_stream(
                pyaudio, self._channels, self._rate, info.get("index"), self._chunk)
        except Exception:
            # 把已经建到一半的东西收干净；复用的端点 release() 是空操作。
            _close_audio(audio, stream)
            if pad is not None:
                pad.close()
            if self._owns_endpoint:
                self.endpoint.release()
            self._status = "启动失败"
            raise

        with self._lock:
            self._pad = pad
            self._audio = audio
            self._stream = stream
            self._reset_dsp()
            self._halt.clear()
            self._sender = LevelSender(
                pad, interval_for(self._kind, self._transport),
                0.0 if self._legacy else SEND_LEAD_RISE, log=self.log)
            self._sender.start()
            self._thread = threading.Thread(target=self._capture_loop, daemon=True,
                                            name="haptics-capture")
            self._thread.start()
            self._enabled = True
            self._running = True
            self._status = f"已开启（{self.endpoint.name}）"
            self._next_report = time.monotonic()
        self.log(f"音频震动已启动：{pad.describe()}，端点 {self.endpoint.name}")
        return self.endpoint.name

    def stop(self, keep_endpoint: bool = False) -> None:
        """停采集、发静音包、停发送线程、关音频流，然后（默认）拆自己建的端点。

        ``keep_endpoint=True`` 只停引擎、把端点留给调用方：界面那条路径就是这样，
        手柄断开/重连时虚拟声卡要一直在，只有关开关或退程序才拆（见
        :class:`HapticsRunner`）。
        """
        with self._lock:
            was_running = self._running or self._pad is not None
            self._halt.set()
            thread = self._thread
            self._thread = None
        if thread is not None:
            # 采集线程会自己关音频流（见 _capture_loop 的 finally）。这里只等它：
            # 在 read 还没返回的时候 close/terminate，PortAudio 会直接崩。
            thread.join(timeout=2.0)
            if thread.is_alive():
                self.log("采集线程没及时退出，音频流留给它自己关")
            else:
                self._close_streams()
        else:
            self._close_streams()
        with self._lock:
            sender, self._sender = self._sender, None
            pad, self._pad = self._pad, None
        if sender is not None:
            sender.stop()
            try:
                sender.join(timeout=1.0)
            except Exception:                       # noqa: BLE001
                pass
        if pad is not None:
            try:
                pad.silence()
            finally:
                pad.close()
        if not keep_endpoint and self._owns_endpoint:
            self.endpoint.release()
        self._running = False
        self._enabled = False
        self._level = (0.0, 0.0)
        if was_running:
            self._status = "已关闭"
            self.log("音频震动已停止")

    def __enter__(self) -> "HapticsEngine":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- 采集 ------------------------------------------------------------- #

    def _read_block(self, stream) -> bytes | None:
        """One capture block, without blocking forever.

        WASAPI 回环在端点安静时**不产生任何数据**（实测 ``get_read_available()``
        一直是 0）。直接 ``read()`` 会把采集线程挂住几十秒，``stop()`` 于是要么
        陪着等，要么从别的线程硬关流——PortAudio 那样会崩。所以这里"够一块才
        读"，安静时按 2 ms 轮询：

        * 读到数据 → 返回那块字节；
        * 静音超过 20 ms → 返回 ``b""``（让上层按静音块处理，强度才会掉下去，
          否则游戏一停音，手柄就停在最后一次的强度上）；
        * 收到停止信号 → 返回 ``None``。
        """
        available = getattr(stream, "get_read_available", None)
        if available is None:                       # pragma: no cover - 老版本
            return stream.read(self._chunk, exception_on_overflow=False)
        idle_since = None
        while not self._halt.is_set():
            if available() >= self._chunk:
                return stream.read(self._chunk, exception_on_overflow=False)
            now = time.monotonic()
            if idle_since is None:
                idle_since = now
            elif now - idle_since >= 0.020:
                idle_since = None
                return b""
            time.sleep(0.002)
        return None

    def _capture_loop(self) -> None:
        try:
            while not self._halt.is_set():
                stream = self._stream
                if stream is None:
                    break
                raw = self._read_block(stream)
                if raw is None:                     # 已收到停止信号
                    break
                if raw:
                    array = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                    if self._channels > 1:
                        count = (array.size // self._channels) * self._channels
                        array = array[:count].reshape(-1, self._channels).mean(axis=1)
                else:
                    # 端点安静：按一整块零处理，让平滑曲线自然衰减到 0。
                    array = np.zeros(self._chunk, dtype=np.float32)
                left, right = self.step(array)
                self._level = (left, right)
                sender = self._sender
                if self._enabled and sender is not None:
                    sender.set_levels(left, right)
                # 发送线程写不进去（手柄拔了、句柄失效）要当成采集链路中断，
                # 否则引擎会一直"正常运行"，界面永远不知道手柄已经不在了。
                if sender is not None and sender.error is not None:
                    raise PadError(f"写震动失败：{sender.error}")
                self._maybe_report()
        except Exception as exc:                    # noqa: BLE001 - 线程里不能抛
            self.error = exc
            self._status = f"音频采集中断：{exc}"
            self.log(self._status)
        finally:
            # 流只在采集线程里关：close/terminate 与 read 并发会让 PortAudio 崩溃。
            self._close_streams()

    def _close_streams(self) -> None:
        """Close the capture stream once, whoever gets here first."""
        with self._lock:
            audio, stream = self._audio, self._stream
            self._audio = self._stream = None
        if stream is not None or audio is not None:
            _close_audio(audio, stream)

    def _maybe_report(self) -> None:
        callback = self.on_status
        if callback is None:
            return
        now = time.monotonic()
        if now < self._next_report:
            return
        self._next_report = now + 1.0
        try:
            callback(self.status_line())
        except Exception:                           # noqa: BLE001
            pass

    def step(self, samples) -> tuple[float, float]:
        """一块单声道样本 → 左右两个 0..1 强度（同时更新刻度）。"""
        _numpy()
        samples = np.asarray(samples, dtype=np.float32)
        if samples.size == 0:
            return (0.0, 0.0)
        rate = self._rate
        # 手动参数是"当前生效值"，每块直接读；auto 那条分支会先复制再覆盖。
        cfg = self._params

        fft_data = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(len(samples), 1 / rate)
        bass_rms = band_rms(fft_data, freqs, 20, cfg["bass_high"], len(samples))
        vocal_rms = band_rms(fft_data, freqs, cfg["vocal_low"], cfg["vocal_high"],
                             len(samples))
        total_rms = float(np.sqrt(np.mean(np.square(samples)) + 1e-10))

        energy_avg = self._energy = self._energy * 0.965 + total_rms * 0.035
        transient = clamp((total_rms - energy_avg) / max(energy_avg, 80.0))
        self._transient = self._transient * 0.82 + transient * 0.18
        self._noise = self._noise * 0.995 + min(total_rms, vocal_rms) * 0.005
        detected = classify_content(total_rms, vocal_rms, bass_rms,
                                    self._transient, self._mode)

        effective = cfg
        if self._auto:
            self._mode = detected
            if detected == "音乐":
                effective = dict(cfg)
                effective.update(AUTO_MUSIC,
                                 threshold=max(170.0, self._noise * 1.7),
                                 intensity=clamp(cfg["intensity"] * 0.9, 0.1, 1.0))
            elif detected == "ASMR":
                effective = dict(cfg)
                effective.update(AUTO_ASMR,
                                 threshold=max(75.0, self._noise * 1.18),
                                 intensity=clamp(cfg["intensity"] * 0.48, 0.05, 0.72))
            else:
                effective = dict(cfg)
                effective.update(AUTO_QUIET)

        adaptive_floor = self._noise * 1.55 if effective["adaptive"] else 0.0
        threshold = max(effective["threshold"], adaptive_floor)

        def calc(rms: float) -> float:
            value = map_value_gamma(rms, threshold, effective["in_max"],
                                    effective["gamma"])
            return clamp(value * effective["intensity"] * self._gain)

        if (effective["dual"] if self._dual is None else self._dual):
            high_value = calc(vocal_rms)            # 600–4000 Hz，"质感"段
            low_value = calc(bass_rms)              # 20–640 Hz，"隆隆"段
            if self._swap:
                target_left, target_right = low_value, high_value
            else:
                # 源项目的分配：高频→左、低频→右（用户对比后选的这个）。
                target_left, target_right = high_value, low_value
        else:
            source = vocal_rms if effective["vocal_only"] else total_rms
            target_left = target_right = calc(source)

        if self._attack is not None:
            # 用户拖过起振滑块：按他拖的值来，不再套下限（"软起振"是合法选择）。
            attack = self._attack
        else:
            attack = effective["attack"]
            if not self._legacy:
                attack = max(attack, ATTACK_FLOOR)
        left = smooth_signal(self._left, target_left, attack, effective["release"])
        right = smooth_signal(self._right, target_right, attack, effective["release"])
        now = time.monotonic()
        left = self._hold_left.update(left, now)
        right = self._hold_right.update(right, now)
        if left < effective["dead_zone"]:
            left = 0.0
        if right < effective["dead_zone"]:
            right = 0.0
        self._left, self._right = left, right

        self._total_rms, self._bass_rms, self._vocal_rms = total_rms, bass_rms, vocal_rms
        self._threshold = threshold
        return (left, right)


# --------------------------------------------------------------------------- #
# 界面用的薄封装
# --------------------------------------------------------------------------- #

class HapticsRunner(threading.Thread):
    """把引擎的慢启停放后台，让界面的开关瞬间可点。

    界面只表达"我现在要什么"，这个后台线程负责把实际状态对上：

    * :meth:`prepare` —— 开关刚打开、手柄还没连：先把音频端点建出来；
    * :meth:`request` —— 有手柄了：起引擎（端点复用，不重建）；
    * :meth:`request_off` —— 关开关：停引擎 **并拆掉自己建的端点**。

    端点所有权在 runner 这一层，所以手柄断开/重连只影响引擎，虚拟声卡一直在
    （用户要求：断开时别撤声卡，关软件/关开关才撤）。端点建不出来时 ``failed``
    会被置上，窗口据此把开关弹回去并显示原因（不能静默失败，也不能让开关停在
    "开"上）。手柄类问题（没接、拔了）只重试，不弹开关。
    """

    #: 手柄暂时不可用时的重试间隔。
    RETRY_S = 2.0

    def __init__(self, engine_factory=None, endpoint=None, log=None):
        super().__init__(daemon=True, name="haptics-runner")
        self.log = log or _quiet
        #: 音频端点归这个 runner（也就是"软件会期"）所有：手柄断开时只停引擎，
        #: 端点留着；关开关或退程序才拆。
        self.endpoint = endpoint if endpoint is not None else AudioEndpoint(log=self.log)
        self._engine_factory = engine_factory or (
            lambda: HapticsEngine(endpoint=self.endpoint, log=self.log,
                                  owns_endpoint=False))
        self._lock = threading.Lock()
        self._wanted: dict | None = None
        self._engine: HapticsEngine | None = None
        self._halt = threading.Event()
        self._wake = threading.Event()
        self._tuning = dict(DEFAULT_TUNING)
        self._endpoint_ready = False
        self.status = "已关闭"
        self.error: str | None = None
        self.failed = False
        self._endpoint_name = ""

    # -- 界面发来的意图 --------------------------------------------------- #

    def prepare(self) -> None:
        """开关打开了、手柄还没连上：先把音频端点建出来。

        端点先存在，游戏才会往它输出震动音频；手柄接上以后再起引擎即可，中间
        不用重建设备（这正是"手柄断开时声卡别消失"的反向要求）。
        """
        with self._lock:
            self._wanted = {"kind": None, "transport": None, "controller": None}
        self._ensure_thread()
        self._wake.set()

    def request(self, kind: str, transport: str, controller=None) -> None:
        with self._lock:
            self._wanted = {"kind": kind, "transport": transport,
                            "controller": controller}
        self._ensure_thread()
        self._wake.set()

    def request_off(self) -> None:
        with self._lock:
            self._wanted = None
        self._ensure_thread()
        self._wake.set()

    # -- 参数 ------------------------------------------------------------- #

    def set_tuning(self, values: dict) -> None:
        """整组滑块参数（开关打开时用）。"""
        if not values:
            return
        with self._lock:
            for key in DEFAULT_TUNING:
                if key in values:
                    self._tuning[key] = values[key]
            tuning = dict(self._tuning)
            engine = self._engine
        if engine is not None:
            apply_tuning(engine, tuning)

    def set_value(self, key: str, value) -> None:
        """拖动某一个滑块：立刻作用到正在跑的引擎上。"""
        if key not in DEFAULT_TUNING:
            return
        with self._lock:
            self._tuning[key] = value
            engine = self._engine
        if engine is not None:
            getattr(engine, f"set_{key}")(value)

    def tuning(self) -> dict:
        with self._lock:
            return dict(self._tuning)

    def levels(self) -> tuple[float, float]:
        with self._lock:
            engine = self._engine
        return engine.levels() if engine is not None else (0.0, 0.0)

    @property
    def endpoint_name(self) -> str:
        return self._endpoint_name

    @property
    def running(self) -> bool:
        with self._lock:
            return self._engine is not None

    # -- 线程 ------------------------------------------------------------- #

    def _ensure_thread(self) -> None:
        if self._halt.is_set():
            return
        if self.is_alive():
            return
        try:
            self.start()
        except RuntimeError:                        # 已经起来过又还没退干净
            pass

    def run(self) -> None:
        # 这个线程活到窗口关闭为止：关掉开关后只是停掉引擎，自己在事件上睡觉，
        # 这样再打开时不用（也不能）重新 start 同一个 Thread。
        while not self._halt.is_set():
            with self._lock:
                wanted = dict(self._wanted) if self._wanted else None
                engine = self._engine
            if wanted is None:
                if engine is not None:
                    self._shutdown_engine()
                self._release_endpoint()
                self.status = "已关闭"
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            if not self._endpoint_ready:
                if not self._ensure_endpoint():
                    # 端点建不出来是致命的（VIIPER 起不来 / 驱动缺失）：放弃请求，
                    # 让窗口把开关弹回去。能重试的问题不会走到这里。
                    self._wake.wait(0.5)
                    self._wake.clear()
                    continue
                continue
            if wanted.get("kind") is None:
                # 只要端点：手柄还没接上（或刚拔掉）。引擎该停就停，端点留着。
                if engine is not None:
                    self._shutdown_engine()
                self.status = "音频端点已就绪，等待手柄连接…"
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            if engine is None:
                if not self._start_engine(wanted):
                    self._sleep_retry()
                continue
            if engine.error is not None:
                self.error = str(engine.error)
                self._shutdown_engine()
                self.status = f"震动中断：{engine.error}"
                self._sleep_retry()
                continue
            self.status = engine.status()
            self._endpoint_name = engine.endpoint_name
            self._wake.wait(0.1)
            self._wake.clear()

    def _sleep_retry(self) -> None:
        self._wake.wait(self.RETRY_S)
        self._wake.clear()

    def _ensure_endpoint(self) -> bool:
        self.status = "正在准备音频端点…"
        try:
            name = self.endpoint.ensure()
        except EndpointError as exc:
            self.error = str(exc)
            self.status = str(exc)
            self.failed = True
            with self._lock:
                self._wanted = None
            self.log(self.status)
            return False
        except Exception as exc:                    # noqa: BLE001 - 别让线程死掉
            self.error = str(exc)
            self.status = f"音频端点出错：{exc}"
            self.log(self.status)
            return False
        self._endpoint_ready = True
        self._endpoint_name = name
        return True

    def _release_endpoint(self) -> None:
        ready, self._endpoint_ready = self._endpoint_ready, False
        self._endpoint_name = ""
        if not ready:
            return
        try:
            self.endpoint.release()
        except Exception as exc:                    # noqa: BLE001
            self.log(f"拆除音频端点出错：{exc}")

    def _start_engine(self, wanted: dict) -> bool:
        with self._lock:
            tuning = dict(self._tuning)
        engine = None
        try:
            engine = self._engine_factory()
            apply_tuning(engine, tuning)
            engine.start(wanted["kind"], wanted["transport"],
                         controller=wanted.get("controller"))
        except PadError as exc:
            # 手柄不在 / 打不开：端点留着，过两秒再试，开关保持打开。
            if engine is not None:
                self._safe_stop(engine)
            self.error = str(exc)
            self.status = f"等待手柄（{exc}）"
            return False
        except EndpointError as exc:
            # 引擎在 start() 里复查端点时发现它没了：下一轮重新走 ensure()。
            if engine is not None:
                self._safe_stop(engine)
            self._endpoint_ready = False
            self.error = str(exc)
            self.status = str(exc)
            self.log(self.status)
            return False
        except Exception as exc:                    # noqa: BLE001
            if engine is not None:
                self._safe_stop(engine)
            self.error = str(exc)
            self.status = str(exc)
            self.log(f"震动启动失败：{exc}")
            return False
        with self._lock:
            self._engine = engine
        self.error = None
        self.status = f"已开启（{engine.endpoint_name}）"
        return True

    def _shutdown_engine(self) -> None:
        with self._lock:
            engine, self._engine = self._engine, None
        self.status = "已关闭"
        if engine is not None:
            self._safe_stop(engine)

    def _safe_stop(self, engine) -> None:
        try:
            # 端点不在这里拆：它归 runner，手柄断开时声卡必须留着。
            engine.stop(keep_endpoint=True)
        except Exception as exc:                    # noqa: BLE001
            self.log(f"停止震动出错：{exc}")

    def stop(self) -> None:
        """窗口关闭时调用：停引擎、拆自己的端点、收线程。"""
        self._halt.set()
        with self._lock:
            self._wanted = None
        self._wake.set()
        if self.is_alive():
            self.join(timeout=3.0)
        with self._lock:
            engine = self._engine
        if engine is not None:
            self._shutdown_engine()
        self._release_endpoint()
