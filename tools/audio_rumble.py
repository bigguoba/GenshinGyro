"""音频 → 震动：命令行薄壳（真正的实现在根目录 :mod:`haptics`）。

这个工具原来装着整套验证代码：采集、DSP、四种发送器。那些已经搬进 ``haptics.py``
给产品用了，这里只解析参数、打印诊断、按 Ctrl+C 收尾——所以工具和产品跑的是同一份
逻辑，不会出现"工具能震、程序不震"这种两边分叉。

音频端点默认要求已经存在（用 ``启动-虚拟音频手柄.bat`` 或 DS5 后端建好）；
``--create-endpoint`` 让引擎自己建 audio-only 设备，``--start-server`` 顺带把
VIIPER 服务拉起来。

用法：
    python tools\\audio_rumble.py --list-endpoints
    python tools\\audio_rumble.py --case nspro-bt
    python tools\\audio_rumble.py --case ds4-bt --preset 低频沉浸 --seconds 60
    python tools\\audio_rumble.py --case nspro-usb --create-endpoint --start-server
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

import haptics                                        # noqa: E402
from haptics import PRESETS                           # noqa: E402

MENU_CASES = (
    ("1", "NS Pro 蓝牙", "nspro-bt"),
    ("2", "NS Pro 有线", "nspro-usb"),
    ("3", "DS4 蓝牙", "ds4-bt"),
    ("4", "DS4 有线", "ds4-usb"),
)


def split_case(case: str) -> tuple[str, str]:
    """'nspro-bt' -> ('nspro', 'bluetooth')；'ds4-usb' -> ('ds4', 'usb')。"""
    kind, wire = case.split("-")
    return kind, ("bluetooth" if wire == "bt" else "usb")


def print_endpoints() -> int:
    """列出可用的回环设备（括号里是声道数 / 采样率）。"""
    try:
        devices = haptics.loopback_devices()
    except haptics.HapticsError as exc:
        print(f"\n  读不到回环设备：{exc}")
        return 1
    print("\n可用的回环设备（括号里是声道数 / 采样率）：")
    for info in devices:
        print(f"  {info['name']}   "
              f"({int(info['maxInputChannels'])}ch / "
              f"{int(info['defaultSampleRate'])} Hz)")
    return 0


def interactive_menu() -> tuple[str, bool] | None:
    print("\n请先确认：虚拟音频端点已创建（否则下面列表里看不到它）。\n")
    if print_endpoints():
        return None
    print("\n" + "-" * 52)
    for key, label, _ in MENU_CASES:
        print(f"   {key}  {label}")
    print("   0  只看设备，不启动")
    print("-" * 52)
    try:
        answer = input("  输入数字后按回车：").strip()
    except EOFError:
        return None
    case = None
    for key, _label, item in MENU_CASES:
        if answer == key:
            case = item
            break
    if case is None:
        return None

    print("\n  马达分配：")
    print("   1  照原版：高频→左，低频→右")
    print("   2  交换：  低频→左（大马达吃低频），高频→右")
    try:
        swap_answer = input("  输入 1 或 2 后按回车 [默认 1]：").strip()
    except EOFError:
        swap_answer = "1"
    return case, swap_answer == "2"


def run(case: str, hints, preset: str, auto: bool, seconds: float, gain: float,
        swap: bool, legacy: bool, chunk: int, hold_ms: float,
        attack: float | None, create: bool, start_server: bool) -> int:
    kind, transport = split_case(case)
    print(f"\n[1/2] 手柄：{haptics.KIND_LABEL[kind]} · "
          f"{haptics.TRANSPORT_LABEL[transport]} ({case})")

    endpoint = haptics.AudioEndpoint(
        hints=hints, allow_create=create, allow_server_start=start_server,
        log=lambda message: print(f"      {message}"))
    engine = haptics.HapticsEngine(
        endpoint=endpoint, chunk=chunk, preset=preset, gain=gain,
        # None = 跟着预设自己的 dual 走，和迁移前的工具行为一致。
        dual=None, swap=swap, auto=auto, legacy=legacy, hold_ms=hold_ms,
        attack=attack, log=lambda message: print(f"      {message}"))
    engine.on_status = lambda line: print(f"  {line}", flush=True)

    mapping = ("低频→左 / 高频→右（自然映射）" if swap
               else "高频→左 / 低频→右（源项目原样）")
    timing = ("源项目原样（慢起振、无保持）" if legacy
              else f"新逻辑（起振≥{haptics.ATTACK_FLOOR:.2f}，上升沿插队"
                   + (f"，脉冲保持 {hold_ms:.0f} ms）" if hold_ms > 0 else "，无脉冲保持）"))
    print(f"\n[2/2] 音频：{'自己建端点' if create else '使用现成的端点'}")
    print(f"  预设 {preset}，自动识别{'开' if auto else '关'}，"
          f"强度 x{gain:.2f}，马达分配：{mapping}")
    print(f"  节奏：{timing}")
    print("  Ctrl+C 停止。\n")

    try:
        name = engine.start(kind, transport)
    except haptics.HapticsError as exc:
        print(f"  起不来：{exc}")
        if not create:
            print("  提示：加 --create-endpoint 让引擎自己建，"
                  "或先运行 启动-虚拟音频手柄.bat。")
        return 2

    print(f"  已开始：{name}\n")
    started = time.monotonic()
    try:
        while True:
            time.sleep(0.2)
            if engine.error is not None:
                print(f"  中断：{engine.error}")
                return 1
            if seconds and time.monotonic() - started >= seconds:
                print("  时间到。")
                break
    except KeyboardInterrupt:
        print("\n  收到 Ctrl+C，停止。")
    finally:
        engine.stop()
        print("  已停止并静音。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把虚拟 DualSense 的音频触觉实时转成实体手柄震动。")
    parser.add_argument("--case", required=False,
                        choices=("ds4-usb", "ds4-bt", "nspro-usb", "nspro-bt"),
                        help="目标手柄与该手柄的传输")
    parser.add_argument("--endpoint", default=None,
                        help="回环设备名字里包含的一段；不填则依次尝试 "
                             "DualSense Wireless Controller / Wireless Controller")
    parser.add_argument("--preset", default=haptics.PRESET_DEFAULT, choices=tuple(PRESETS),
                        help=f"参数预设，默认 {haptics.PRESET_DEFAULT}")
    parser.add_argument("--auto", action="store_true",
                        help="开启内容识别自动切换参数（源项目的默认行为）")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="运行多少秒后自动停止，0 = 一直跑到 Ctrl+C")
    parser.add_argument("--gain", type=float, default=1.0,
                        help="总强度倍率，默认 1.0")
    parser.add_argument("--swap", action="store_true",
                        help="交换左右马达：低频→左(大)、高频→右(小)；不加则照抄源项目")
    parser.add_argument("--legacy", action="store_true",
                        help="用源项目的原始节奏：无脉冲保持、无上升沿插队、慢起振")
    parser.add_argument("--chunk", type=int, default=haptics.CHUNK,
                        help=f"采集块大小，默认 {haptics.CHUNK}（越小延迟越低）")
    parser.add_argument("--hold-ms", type=float, default=0.0,
                        help="短脉冲保持时长，默认 0（关闭；想试可给 20~40）")
    parser.add_argument("--attack", type=float, default=None,
                        help="起振系数 0..1，默认至少 0.85（越大越快）")
    parser.add_argument("--create-endpoint", action="store_true",
                        help="端点不存在时自己建一个 audio-only 设备（默认只复用）")
    parser.add_argument("--start-server", action="store_true",
                        help="VIIPER 没在跑时尝试启动它（需要 --create-endpoint）")
    parser.add_argument("--list-endpoints", action="store_true",
                        help="只列出可用的回环设备")
    parser.add_argument("--menu", action="store_true",
                        help="先列回环设备，再用数字菜单选手柄（给双击的 .bat 用）")
    args = parser.parse_args(argv)

    if args.list_endpoints:
        return print_endpoints()
    if args.menu:
        chosen = interactive_menu()
        if chosen is None:
            return 0
        args.case, args.swap = chosen
    if not args.case:
        parser.print_help()
        return 2
    hints = ((args.endpoint,) if args.endpoint
             else haptics.ENDPOINT_HINTS)
    return run(args.case, hints, args.preset, args.auto, args.seconds, args.gain,
               args.swap, args.legacy, args.chunk, args.hold_ms, args.attack,
               args.create_endpoint, args.start_server)


if __name__ == "__main__":
    sys.exit(main())
