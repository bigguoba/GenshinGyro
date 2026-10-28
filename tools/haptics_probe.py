"""Haptics probe: capture the virtual DualSense audio endpoint and drive a pad.

This is the *validated* route: point a WASAPI loopback capture at the DS5 audio
endpoint, take its two haptic channels, turn them into a 0..1 strength, and send
that to the physical pad.

Why not read VIIPER's 0x83/0x84 frames instead?  They carry the same signal but
VIIPER has already resampled it to 3 kHz, so everything above 1.5 kHz is gone.
The loopback keeps the full 48 kHz stream, which is what the reference test (an
Xbox-rumble tool capturing this same endpoint) actually used.

Signal chain (rewritten from the well-known pattern, not copied):

    loopback 48 kHz / 4 ch  ->  take ch3+ch4 (the haptic pair)
      ->  RMS per side (int16 units)
      ->  threshold + gamma  ->  0..1
      ->  intensity, attack/release smoothing, dead zone
      ->  pad-specific report (DS4 0x05/0x11, Switch 0x10)

Everything on the output side (report bytes, exact lengths, write channel) comes
from tools/rumble_probe.py, which was validated on hardware.

Requirements:
    pip install numpy pyaudiowpatch

Run:
    python tools\\haptics_probe.py --list
    python tools\\haptics_probe.py --case ds4-bt
    python tools\\haptics_probe.py --menu
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if not sys.stdout.isatty():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                   # noqa: BLE001
        pass

try:
    import numpy as np
    import pyaudiowpatch as pyaudio
except ImportError as exc:                              # noqa: BLE001
    print()
    print("缺少依赖：", getattr(exc, "name", exc))
    print("请先在命令行运行这一句（只需一次）：")
    print()
    print("    pip install numpy pyaudiowpatch")
    print()
    sys.exit(2)

import rumble_probe as probe  # noqa: E402  (same directory)

CASES = probe.CASES
KIND_LABEL = probe.KIND_LABEL
TRANSPORT_LABEL = probe.TRANSPORT_LABEL

CHUNK = 512                 # frames per read: 512 / 48000 = 10.7 ms
SEND_INTERVAL_S = 0.030     # SDL's minimum rumble write spacing
DEFAULT_DEVICE_HINT = "wireless controller"

#: The 0..1 curve.  Values are in int16 RMS units (0..32767), which is what the
#: loopback hands us, so no rescaling is needed.
PROFILES = {
    "punchy": dict(threshold=180.0, in_max=6000.0, gamma=0.90,
                   intensity=1.00, attack=0.58, release=0.22, dead_zone=0.030),
    "bass":   dict(threshold=150.0, in_max=5200.0, gamma=1.05,
                   intensity=0.95, attack=0.46, release=0.16, dead_zone=0.035),
    "smooth": dict(threshold=220.0, in_max=4200.0, gamma=1.30,
                   intensity=0.80, attack=0.30, release=0.10, dead_zone=0.040),
}


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------- #
# Audio endpoint discovery
# --------------------------------------------------------------------------- #

def loopback_devices(audio) -> list[dict]:
    found = []
    for index in range(audio.get_device_count()):
        info = audio.get_device_info_by_index(index)
        if info.get("isLoopbackDevice", False):
            found.append(info)
    return found


def find_endpoint(audio, hint: str, index: int | None) -> dict | None:
    devices = loopback_devices(audio)
    if index is not None:
        for info in devices:
            if int(info["index"]) == int(index):
                return info
        return None

    needle = (hint or "").lower()
    matches = [info for info in devices if needle and needle in info["name"].lower()]
    if matches:
        # Prefer a device that exposes the four-channel DS5 layout.
        matches.sort(key=lambda info: 0 if int(info.get("maxInputChannels", 0)) >= 4 else 1)
        return matches[0]

    # Nothing matched: fall back to the only 4-channel loopback, if it is unique.
    four = [info for info in devices if int(info.get("maxInputChannels", 0)) >= 4]
    if len(four) == 1:
        return four[0]
    return None


def cmd_list() -> int:
    audio = pyaudio.PyAudio()
    try:
        print()
        print("=" * 66)
        print("WASAPI 回环设备（能采到的都是“正在播放”的输出端点）")
        print("=" * 66)
        devices = loopback_devices(audio)
        if not devices:
            print("  一个都没有。确认系统里有正在工作的播放设备。")
            return 1
        for info in devices:
            mark = " ← 触觉端点" if int(info.get("maxInputChannels", 0)) >= 4 else ""
            print(f"  [{info['index']:>3}] {info['maxInputChannels']} 声道  "
                  f"{info['defaultSampleRate']:.0f} Hz  {info['name']}{mark}")
        print()
        print("  用 --device 名字片段 或 --device-index 序号 指定要采哪一个。")
        return 0
    finally:
        audio.terminate()


# --------------------------------------------------------------------------- #
# Pad output
# --------------------------------------------------------------------------- #

class PadSink:
    """Per-side 0..1 strength -> the report the physical pad accepts."""

    def __init__(self, case: str, log=print):
        self.case = case
        self.log = log
        self.mode, self.transport = probe.split_case(case)
        self.pad = None
        self.ctl = None
        self._counter = 0

    def open(self) -> None:
        infos = probe.candidates(self.mode, self.transport)
        if not infos:
            raise RuntimeError(
                f"没找到处于「{TRANSPORT_LABEL[self.transport]}」的 "
                f"{KIND_LABEL[self.mode]} 设备")
        info = infos[0]
        self.log(f"手柄：{info.get('product') or '未知'}  in={info.get('input_len')} "
                 f"out={info.get('output_len')}")

        if self.mode == "ds4":
            import dualshock4
            self.pad = dualshock4.DualShock4(
                path=info["path"], info=info, log=lambda _m: None).open()
            self.log(f"已打开：{self.pad.device_name} / {self.pad.transport}")
            return

        import switchpro
        self.ctl = switchpro.SwitchProController(
            path=info["path"], info=info, log=lambda _m: None).open()
        try:
            self.ctl.initialize()
            self.log(f"已打开：{self.ctl.device_type_name} / {self.ctl.transport}")
        except Exception as exc:                        # noqa: BLE001
            self.log(f"初始化告警（继续）：{exc}")

    def send(self, left: float, right: float) -> None:
        left = clamp(left)
        right = clamp(right)
        if self.pad is not None:
            report = probe.ds4_report(int(round(left * 255)), int(round(right * 255)),
                                      self.transport)
            if self.transport == "bluetooth":
                assert self.pad._device is not None
                self.pad._device.write(report)          # exact 78 bytes, interrupt pipe
            else:
                self.pad.write_output(report)
            return

        assert self.ctl is not None
        # High band only: measured as the better-feeling band on this pad.
        side_l = probe.nspro_payload_bands(0.0, 160.0, left, 320.0)[:4]
        side_r = probe.nspro_payload_bands(0.0, 160.0, right, 320.0)[:4]
        counter = self._counter
        self._counter = (self._counter + 1) & 0x0F
        import switchpro
        self.ctl._write(bytes((switchpro.OUTPUT_REPORT_RUMBLE, counter))
                        + side_l + side_r)

    def silence(self) -> None:
        for _ in range(probe.STOP_REPEATS):
            try:
                self.send(0.0, 0.0)
            except Exception:                           # noqa: BLE001
                return
            time.sleep(probe.STOP_GAP_S)

    def close(self) -> None:
        for obj in (self.pad, self.ctl):
            if obj is None:
                continue
            try:
                obj.close()
            except Exception:                           # noqa: BLE001
                pass
        self.pad = self.ctl = None


# --------------------------------------------------------------------------- #
# Signal chain
# --------------------------------------------------------------------------- #

def map_strength(rms: float, profile: dict) -> float:
    if rms <= profile["threshold"]:
        return 0.0
    span = max(1.0, profile["in_max"] - profile["threshold"])
    value = clamp((rms - profile["threshold"]) / span)
    return value ** max(0.1, profile["gamma"])


def smooth(current: float, target: float, profile: dict) -> float:
    rate = profile["attack"] if target > current else profile["release"]
    return current + (target - current) * clamp(rate)


def band_rms(block, rate: int, low: float, high: float) -> float:
    spectrum = np.fft.rfft(block)
    freqs = np.fft.rfftfreq(len(block), 1.0 / rate)
    mask = (freqs >= low) & (freqs <= high)
    if not np.any(mask):
        return 0.0
    kept = np.zeros_like(spectrum)
    kept[mask] = spectrum[mask]
    return float(np.sqrt(np.mean(np.square(np.fft.irfft(kept, n=len(block)))) + 1e-10))


def channel_rms(block) -> float:
    return float(np.sqrt(np.mean(np.square(block.astype(np.float64))) + 1e-10))


def run(case: str, args) -> int:
    profile = dict(PROFILES[args.profile])
    if args.intensity is not None:
        profile["intensity"] = clamp(args.intensity)
    if args.threshold is not None:
        profile["threshold"] = max(0.0, args.threshold)

    audio = pyaudio.PyAudio()
    sink = PadSink(case, log=print)
    stream = None
    try:
        endpoint = find_endpoint(audio, args.device, args.device_index)
        if endpoint is None:
            print("没找到可采集的音频端点。先跑一次 --list 看看有哪些，")
            print("再用 --device 名字片段 或 --device-index 序号 指定。")
            return 1

        channels = int(endpoint["maxInputChannels"])
        rate = int(endpoint["defaultSampleRate"])
        print(f"音频源：{endpoint['name']} / {rate} Hz / {channels} 声道")
        if channels < 4:
            print(f"⚠ 只有 {channels} 声道，取不到独立的触觉声道，将使用混音。")

        sink.open()
        stream = audio.open(
            format=pyaudio.paInt16, channels=channels, rate=rate, input=True,
            input_device_index=endpoint["index"], frames_per_buffer=CHUNK)
        print("开始采集。Ctrl+C 停止。\n")

        left_out = right_out = 0.0
        last_send = 0.0
        source = args.source
        while True:
            raw = stream.read(CHUNK, exception_on_overflow=False)
            data = np.frombuffer(raw, dtype=np.int16)
            if channels > 1:
                data = data.reshape(-1, channels)

            if channels >= 4:
                left_block = data[:, 2]          # DS5 haptic left
                right_block = data[:, 3]         # DS5 haptic right
            else:
                mono = data.mean(axis=1) if channels > 1 else data
                left_block = right_block = mono

            if source == "bass":
                left_rms = band_rms(left_block, rate, 20.0, 520.0)
                right_rms = band_rms(right_block, rate, 20.0, 520.0)
            elif source == "mid":
                left_rms = band_rms(left_block, rate, 650.0, 3600.0)
                right_rms = band_rms(right_block, rate, 650.0, 3600.0)
            else:
                left_rms = channel_rms(left_block)
                right_rms = channel_rms(right_block)

            left_target = map_strength(left_rms, profile) * profile["intensity"]
            right_target = map_strength(right_rms, profile) * profile["intensity"]
            left_out = smooth(left_out, left_target, profile)
            right_out = smooth(right_out, right_target, profile)
            if left_out < profile["dead_zone"]:
                left_out = 0.0
            if right_out < profile["dead_zone"]:
                right_out = 0.0

            now = time.monotonic()
            if now - last_send >= SEND_INTERVAL_S:
                last_send = now
                sink.send(left_out, right_out)
                if args.verbose:
                    bars_l = "#" * int(left_out * 20)
                    bars_r = "#" * int(right_out * 20)
                    print(f"\rL {left_rms:7.0f} [{bars_l:<20}]  "
                          f"R {right_rms:7.0f} [{bars_r:<20}]", end="", flush=True)
    except KeyboardInterrupt:
        print("\n\n已停止（Ctrl+C）。")
    finally:
        sink.silence()
        sink.close()
        if stream is not None:
            try:
                stream.close()
            except Exception:                           # noqa: BLE001
                pass
        audio.terminate()
        print("手柄震动已清零。")
    return 0


MENU = (
    ("1", "DS4 有线", "ds4-usb"),
    ("2", "DS4 蓝牙", "ds4-bt"),
    ("3", "NS Pro 有线", "nspro-usb"),
    ("4", "NS Pro 蓝牙", "nspro-bt"),
)


def interactive_menu() -> str | None:
    cmd_list()
    print()
    print("  " + "-" * 46)
    for key, label, _ in MENU:
        print(f"   {key}  {label}")
    print("   0  退出")
    print("  " + "-" * 46)
    try:
        answer = input("  输入数字后按回车：").strip()
    except EOFError:
        return None
    for key, _label, case in MENU:
        if answer == key:
            return case
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把虚拟 DS5 音频端点的触觉声道实时转成实体手柄震动。")
    parser.add_argument("--list", action="store_true", help="列出声卡回环设备")
    parser.add_argument("--menu", action="store_true", help="列设备 + 数字菜单")
    parser.add_argument("--case", choices=CASES, help="目标手柄与传输")
    parser.add_argument("--device", default=DEFAULT_DEVICE_HINT,
                        help="音频端点名字片段，默认 wireless controller")
    parser.add_argument("--device-index", type=int, default=None,
                        help="直接指定回环设备序号（见 --list）")
    parser.add_argument("--source", choices=("total", "bass", "mid"), default="total",
                        help="用全频 / 低频段 / 中频段算强度，默认 total")
    parser.add_argument("--profile", choices=tuple(PROFILES), default="punchy",
                        help="手感预设，默认 punchy")
    parser.add_argument("--intensity", type=float, default=None,
                        help="总强度 0..1.5，覆盖预设")
    parser.add_argument("--threshold", type=float, default=None,
                        help="起振门限（int16 RMS），默认取预设")
    parser.add_argument("--verbose", action="store_true", help="打印实时电平")
    args = parser.parse_args(argv)

    if args.list:
        return cmd_list()
    if args.menu:
        case = interactive_menu()
        if case is None:
            return 0
        args.case = case
    if not args.case:
        parser.print_help()
        return 2
    return run(args.case, args)


if __name__ == "__main__":
    sys.exit(main())
