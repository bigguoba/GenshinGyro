"""Create ONLY a virtual DualSense audio channel (no gamepad) and watch for haptics.

Purpose: find out whether a game keeps sending audio haptics when there is no
DualSense *gamepad* at all -- only the USB Audio Class interface.  Nothing is
created but the audio function, so if haptics still arrive, the rumble pipeline
never needs a virtual DualSense pad (and the virtual DualShock 4 can keep handling
input, which is the configuration that already feels right).

    python tools\\viiper_audioonly.py
    python tools\\viiper_audioonly.py --type dualsensecombinedaudioduplexv5
    python tools\\viiper_audioonly.py --seconds 120 --capture D:\\haptics.bin

What to look at while it runs:
  * "音频端点" -- the Windows endpoint this device creates; that is what the game
    has to write to.
  * the per-second counters -- "触觉" frames are the signal we are after.
  * the first haptics frame is hex-dumped, and every frame is appended to the
    capture file for later analysis.

Ctrl+C stops it and removes the device.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time
import winreg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import viiper  # noqa: E402

#: Audio-only variants: same DualSense identity, but no HID gamepad function.
AUDIO_ONLY_TYPES = (
    "dualsenseaudioonlyduplexv5",
    "dualsenseaudioonlyduplexv5rawinputevents",
)

MMDEVICES_RENDER = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
MMDEVICES_CAPTURE = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
PROP_FRIENDLY = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
PROP_DESC = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"

#: Where the haptics PCM sits inside a feedback frame: a DualSense "combined
#: haptics" report carries 64 bytes (32 stereo frames, signed 8-bit) starting at
#: offset 78; the atomic variant has two extra leading bytes, so offset 80.
HAPTICS_SAMPLE_LENGTH = 64
HAPTICS_SAMPLE_OFFSETS = {viiper.FRAME_REALTIME_HAPTICS: 78,
                          viiper.FRAME_ATOMIC_AUDIO_HAPTICS: 80}


def sample_region(frame_type: int, payload: bytes) -> bytes:
    offset = HAPTICS_SAMPLE_OFFSETS.get(frame_type, 78)
    if len(payload) < offset + HAPTICS_SAMPLE_LENGTH:
        return b""
    return payload[offset:offset + HAPTICS_SAMPLE_LENGTH]


def bar(level: float, width: int = 24) -> str:
    filled = int(round(min(1.0, level) * width))
    return "#" * filled + "." * (width - filled)


class DeviationDetector:
    """Compare each haptics frame against the idle template learned at start-up.

    VIIPER pushes haptics frames continuously, filled with an idle pattern, so the
    frame count says nothing.  What matters is whether the payload *changes*.  The
    first frames therefore build a per-byte template for each frame type; anything
    later that deviates from it is real data from the host.

    This is deliberately layout-agnostic: the exact byte offsets inside the report
    are not needed to answer "did the game send anything".
    """

    def __init__(self, warmup: int = 150):
        self.warmup = warmup
        self.templates: dict[int, list[float]] = {}
        self.seen: dict[int, int] = {}
        self.baseline: dict[int, float] = {}
        self.best: dict[int, tuple[float, float]] = {}      # type -> (score, maxdiff)

    def feed(self, frame_type: int, region: bytes) -> tuple[float, float]:
        """Returns (mean absolute deviation, max deviation) against the template.

        Only the 64-byte sample region is compared: the rest of the report carries
        a counter and a CRC, which change every frame and would drown the signal.
        """
        template = self.templates.get(frame_type)
        if template is None or len(template) != len(region):
            self.templates[frame_type] = [float(value) for value in region]
            self.seen[frame_type] = 1
            return 0.0, 0.0

        count = self.seen[frame_type]
        if count < self.warmup:
            # Running mean while warming up: cheap and stable enough for a floor.
            template = [(old * count + value) / (count + 1)
                        for old, value in zip(template, region)]
            self.templates[frame_type] = template
            self.seen[frame_type] = count + 1
            return 0.0, 0.0

        total = 0
        worst = 0
        for position, value in enumerate(region):
            diff = abs(value - template[position])
            total += diff
            if diff > worst:
                worst = diff
        score = total / max(1, len(region))
        if frame_type not in self.baseline:
            self.baseline[frame_type] = score
        best = self.best.get(frame_type, (0.0, 0.0))
        if score > best[0]:
            best = (score, worst)
        self.best[frame_type] = best
        return score, worst

    def threshold(self, frame_type: int) -> float:
        floor = self.baseline.get(frame_type, 0.0)
        return max(1.0, floor * 3.0)


def audio_endpoints() -> list[tuple[str, str, str, str]]:
    """[(kind, state, friendly, description)] for endpoints that look like ours."""
    found = []
    for kind, root in (("输出", MMDEVICES_RENDER), ("输入", MMDEVICES_CAPTURE)):
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(key, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(key, sub_name) as sub:
                        state = winreg.QueryValueEx(sub, "DeviceState")[0]
                        with winreg.OpenKey(sub, "Properties") as props:
                            friendly = _value(props, PROP_FRIENDLY)
                            desc = _value(props, PROP_DESC)
                except OSError:
                    continue
                text = f"{friendly} {desc}"
                if "DualSense" in text or "Wireless Controller" in text:
                    found.append((kind, str(state), friendly, desc))
    return found


def _value(key, name: str) -> str:
    try:
        return str(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", default=AUDIO_ONLY_TYPES[0],
                        help=f"device type to create (default {AUDIO_ONLY_TYPES[0]})")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="stop after this many seconds (0 = run until Ctrl+C)")
    parser.add_argument("--capture", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "haptics-capture.bin"),
                        help="file that receives every frame (type, length, payload)")
    parser.add_argument("--status-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)

    server = viiper.ViiperServer()
    print("1) 建虚拟总线")
    bus_id = server.create_bus()

    print(f"2) 加设备：{args.type}（只含音频，没有任何手柄功能）")
    added = server.add_device(bus_id, args.type)
    device_id = str(added["devId"])
    print(f"   vid/pid = {added.get('vid')}/{added.get('pid')}  "
          f"USB/IP 端口 = {added.get('usbipPort')}")

    stream = viiper.DeviceStream(server, bus_id, device_id, log=print)
    stream.open()
    print("3) 设备流已连接（音频通道在线）")

    capture = None
    if args.capture:
        capture = open(args.capture, "wb")
        capture.write(b"VIIP HAPTICS CAPTURE 1\n")

    first_haptics = None
    detector = DeviationDetector()
    signals: list[tuple[int, bytes, float, float]] = []

    def on_frame(frame_type: int, payload: bytes, sequence: int) -> None:
        nonlocal first_haptics
        if capture is not None:
            capture.write(struct.pack("<BH", frame_type, len(payload)))
            capture.write(payload)
        if frame_type not in (viiper.FRAME_ATOMIC_AUDIO_HAPTICS,
                              viiper.FRAME_REALTIME_HAPTICS):
            return
        if first_haptics is None:
            first_haptics = (frame_type, payload)
            name = viiper.FRAME_NAMES.get(frame_type, hex(frame_type))
            print(f"   >>> 收到第一个触觉帧：{name}，{len(payload)} 字节")
        score, worst = detector.feed(frame_type, sample_region(frame_type, payload))
        if score >= detector.threshold(frame_type):
            signals.append((frame_type, payload, score, worst))

    stream.on_frame = on_frame

    time.sleep(1.5)                              # let Windows create the endpoints
    endpoints = audio_endpoints()
    print("4) 本机与 DualSense 相关的音频端点：")
    if endpoints:
        for kind, state, friendly, desc in endpoints:
            note = "（已激活）" if state == "1" else f"（状态 {state}）"
            print(f"   [{kind}]{note} {friendly} | {desc}")
    else:
        print("   （没有找到——设备可能还没被 Windows 枚举完，稍等再按 Ctrl+C 前看一次）")

    print()
    print("5) 现在去游戏里触发震动。下面每行是一次统计：")
    print("   * 扬声器PCM —— 游戏是不是真的在往这个音频端点写数据（关键指标）")
    print("   * 触觉帧    —— VIIPER 的空闲节拍，一直会有，不看数量")
    print("   * 样本区差异 —— 触觉内容相对空闲时的变化，会跳一下说明有数据")
    print("   这个设备没有任何手柄功能，游戏可能根本看不到手柄——")
    print("   那正是本次测试要回答的：只有音频通道时，数据还来不来。")
    print()

    started = time.perf_counter()
    next_status = started + args.status_seconds
    try:
        while True:
            time.sleep(0.05)
            now = time.perf_counter()
            if args.seconds and now - started >= args.seconds:
                break
            if now >= next_status:
                next_status = now + args.status_seconds
                counts = dict(stream.frame_counts)
                speaker = counts.get(viiper.FRAME_SPEAKER_PCM, 0)
                mic = counts.get(viiper.FRAME_MIC_PCM, 0)
                atomic = counts.get(viiper.FRAME_ATOMIC_AUDIO_HAPTICS, 0)
                realtime = counts.get(viiper.FRAME_REALTIME_HAPTICS, 0)
                output = counts.get(viiper.FRAME_OUTPUT_STATE, 0)
                elapsed = now - started
                flag = "  ← 游戏在写音频！" if speaker else ""
                print(f"   [{elapsed:5.1f}s] 扬声器PCM {speaker:6d}{flag}"
                      f"   触觉 {atomic + realtime:5d}"
                      f"   麦克风PCM {mic:4d}   震动/LED 帧 {output:4d}")
                warm = sum(1 for count in detector.seen.values()
                           if count >= detector.warmup)
                if warm:
                    for frame_type in (viiper.FRAME_ATOMIC_AUDIO_HAPTICS,
                                       viiper.FRAME_REALTIME_HAPTICS):
                        if frame_type not in detector.best:
                            continue
                        score, worst = detector.best[frame_type]
                        name = viiper.FRAME_NAMES.get(frame_type, hex(frame_type))
                        threshold = detector.threshold(frame_type)
                        print(f"             {name:20} 样本区差异"
                              f" {bar(score / threshold if threshold else 0, 14)}"
                              f" {score:5.2f}  最差 {worst:3.0f}  阈值 {threshold:0.2f}")
                else:
                    print("             正在学习空闲基线…")
                if signals:
                    frame_type, payload, score, worst = signals[-1]
                    name = viiper.FRAME_NAMES.get(frame_type, hex(frame_type))
                    print(f"             >>> 触觉内容有变化：{name} 差异 {score:0.2f}"
                          f"（最差 {worst:0.0f}），累计 {len(signals)} 帧")
    except KeyboardInterrupt:
        print("\n   Ctrl+C：停止")
    finally:
        if capture is not None:
            capture.close()
        stream.close()
        total_speaker = stream.frame_counts.get(viiper.FRAME_SPEAKER_PCM, 0)
        total_haptics = (stream.frame_counts.get(viiper.FRAME_ATOMIC_AUDIO_HAPTICS, 0)
                         + stream.frame_counts.get(viiper.FRAME_REALTIME_HAPTICS, 0))
        server.remove_device(bus_id, device_id)
        server.remove_bus(bus_id)
        print("6) 设备已移除")
        print(f"   累计：扬声器 PCM 帧 {total_speaker}，触觉帧 {total_haptics}，"
              f"触觉内容变化帧 {len(signals)}")
        print()
        if total_speaker > 0 or signals:
            print("   结论：只有音频通道时也收到了数据 —— 不需要虚拟 DS5 手柄就够。")
        else:
            print("   结论：只有音频通道时没有收到任何音频数据。")
            print("         如果游戏这时确实在放震动，那就说明它需要先看到一个 DS5 手柄。")
        if args.capture:
            size = os.path.getsize(args.capture) if os.path.exists(args.capture) else 0
            print(f"   捕获文件：{args.capture}（{size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
