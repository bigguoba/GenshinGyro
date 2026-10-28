"""Create the virtual DualSense and keep its audio endpoint alive.

`tools/audio_rumble.py` captures the loopback of the "Wireless Controller Audio"
endpoint.  That endpoint only exists while VIIPER has a DualSense of the
`combined audio duplex` type attached, and the device stream has to stay open --
so someone has to keep it running while the game plays.

This tool does exactly that and nothing else:

    1. make sure the VIIPER server is listening on 127.0.0.1:3242
       (optionally start VIIPER-*.exe itself)
    2. create a bus + an audio-only `dualsenseaudioonlyduplexv5` device
       (pass --with-gamepad for the full combined device)
    3. attach the device stream and keep it open
    4. wait until Windows exposes the audio endpoint, then say so and idle

Ctrl+C removes the device again.

    python tools\\viiper_audio_device.py --start-server
    python tools\\viiper_audio_device.py --seconds 30
"""

from __future__ import annotations

import argparse
import glob
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if not sys.stdout.isatty():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                   # noqa: BLE001
        pass

import viiper                                       # noqa: E402

HOST, PORT = viiper.DEFAULT_HOST, viiper.DEFAULT_PORT
DEFAULT_ENDPOINT_HINT = "DualSense Wireless Controller"
#: Same DualSense identity, but only the audio function -- no HID gamepad.
AUDIO_ONLY_TYPE = "dualsenseaudioonlyduplexv5"


def port_open(host: str = HOST, port: int = PORT, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(seconds: float, host: str = HOST, port: int = PORT) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if port_open(host, port):
            return True
        time.sleep(0.25)
    return False


def find_server_exe() -> str | None:
    """VIIPER-*.exe next to the project, next to the workspace, or on PATH."""
    here = os.path.dirname(os.path.abspath(__file__))
    roots = [os.path.dirname(here),                          # GenshinGyro\
             os.path.dirname(os.path.dirname(here))]         # workspace\
    for root in roots:
        matches = sorted(glob.glob(os.path.join(root, "VIIPER-*.exe")))
        if matches:
            return matches[-1]
    return None


def list_loopback_names() -> list[str]:
    try:
        import pyaudiowpatch as pyaudio
    except Exception:                                   # noqa: BLE001
        return []
    audio = pyaudio.PyAudio()
    try:
        names = []
        for index in range(audio.get_device_count()):
            info = audio.get_device_info_by_index(index)
            if info.get("isLoopbackDevice", False):
                names.append(info["name"])
        return names
    finally:
        audio.terminate()


def find_endpoint(hint: str | None = None) -> str | None:
    hints = (hint,) if hint else (DEFAULT_ENDPOINT_HINT, "Wireless Controller")
    names = list_loopback_names()
    for item in hints:
        needle = item.lower()
        for name in names:
            if needle in name.lower():
                return name
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="创建虚拟 DualSense（含音频端点）并保持它存活。")
    parser.add_argument("--start-server", action="store_true",
                        help="VIIPER 没在跑时，自动启动同目录下的 VIIPER-*.exe")
    parser.add_argument("--server-exe", default=None,
                        help="指定 VIIPER 可执行文件路径")
    parser.add_argument("--keep-server", action="store_true",
                        help="退出时保留自动启动的 VIIPER 服务")
    parser.add_argument("--endpoint", default=None,
                        help="端点名字片段；不填则依次尝试 "
                             "DualSense Wireless Controller / Wireless Controller")
    parser.add_argument("--wait-endpoint", type=float, default=25.0,
                        help="最多等多少秒让 Windows 出现音频端点，默认 25")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="保持多少秒后自动退出，0 = 一直等到 Ctrl+C")
    parser.add_argument("--with-gamepad", action="store_true",
                        help="同时创建虚拟手柄（默认只建音频接口）")
    parser.add_argument("--type", default=AUDIO_ONLY_TYPE,
                        help=f"只建音频时使用的设备类型，默认 {AUDIO_ONLY_TYPE}")
    args = parser.parse_args(argv)

    server_process = None
    pad = None
    server = viiper.ViiperServer()
    bus_id = None
    device_id = None
    stream = None

    print()
    print("=" * 60)
    print("虚拟 DualSense · 音频端点")
    print("=" * 60)

    # -- 1. server ------------------------------------------------------ #
    if port_open():
        print(f"[1/4] VIIPER 已在运行（{HOST}:{PORT}）")
    elif args.start_server:
        exe = args.server_exe or find_server_exe()
        if not exe or not os.path.exists(exe):
            print("[1/4] 没找到 VIIPER-*.exe，请用 --server-exe 指定路径。")
            return 2
        print(f"[1/4] 启动 VIIPER：{os.path.basename(exe)}")
        server_process = subprocess.Popen(
            [exe, "server", "--log.level=info"],
            cwd=os.path.dirname(exe),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        if not wait_for_port(20.0):
            print("      服务在 20 秒内没有开始监听，请手动启动看看窗口里的报错。")
            return 2
        print("      服务已就绪。")
    else:
        print(f"[1/4] VIIPER 没有在运行（{HOST}:{PORT} 连不上）。")
        print("      先运行 启动-VIIPER服务.bat，或者加 --start-server 让我来启动。")
        return 2

    try:
        # -- 2. device -------------------------------------------------- #
        device_type = viiper.DEVICE_TYPE if args.with_gamepad else args.type
        print(f"[2/4] 创建设备：{device_type}")
        print("      模式：完整手柄 + 音频" if args.with_gamepad
              else "      模式：只建音频接口（不建虚拟手柄）")
        if args.with_gamepad:
            pad = viiper.DualSensePad(log=lambda message: print(f"      {message}"))
            pad.plug_in()
            bus_id, device_id = pad.bus_id, pad.device_id
            stream = pad.stream
        else:
            bus_id = server.create_bus()
            added = server.add_device(bus_id, device_type)
            device_id = str(added["devId"])
            # A stream has to stay attached; without a gamepad function there
            # is no input state to send, so the stream is only kept open.
            stream = viiper.DeviceStream(server, bus_id, device_id,
                                         log=lambda message: print(f"      {message}"))
            stream.open()
        print(f"      bus={bus_id}  dev={device_id}")

        # -- 3. audio endpoint ------------------------------------------ #
        print(f"[3/4] 等待 Windows 出现音频端点（最多 {args.wait_endpoint:.0f} 秒）")
        endpoint = None
        deadline = time.monotonic() + args.wait_endpoint
        while time.monotonic() < deadline:
            if stream is None or stream.closed:
                print("      设备流已断开，结束。")
                return 1
            endpoint = find_endpoint(args.endpoint)
            if endpoint:
                break
            time.sleep(0.5)

        if endpoint:
            print(f"      已就绪：{endpoint}")
        else:
            print("      没等到音频端点。可能原因：")
            print("        · USB/IP 驱动没有连接这个设备（看 VIIPER 窗口的日志）")
            print("        · 系统还没有装好 USB 音频驱动（第一次会慢一些）")
            print("        · 端点名字不含 "
                  f"{args.endpoint!r}，可以加 --endpoint 换个片段")
            print("      设备仍然保持运行，你可以先在 Windows 声音设置里看看。")

        print("[4/4] 保持运行中。")
        print()
        print("  现在可以双击 启动-音频震动.bat 去采集这个端点。")
        print("  保持这个窗口开着；按 Ctrl+C 结束并移除虚拟设备。")
        if not args.with_gamepad:
            print("  （如果游戏里完全没有震动，说明它需要看到一个手柄："
                  "用 --with-gamepad 再试。）")
        print()

        started = time.monotonic()
        while True:
            if args.seconds and time.monotonic() - started >= args.seconds:
                print("  时间到，结束。")
                break
            if stream is None or stream.closed:
                print("  设备流已断开，结束。")
                break
            time.sleep(0.5)
        return 0

    except KeyboardInterrupt:
        print("\n  收到 Ctrl+C。")
        return 0
    except viiper.ViiperError as exc:
        print(f"\n  VIIPER 出错：{exc}")
        return 1
    except Exception as exc:                            # noqa: BLE001
        print(f"\n  出错：{exc}")
        return 1
    finally:
        if pad is not None:
            print("  移除虚拟设备 ...")
            try:
                pad.unplug()
            except Exception:                           # noqa: BLE001
                pass
        else:
            print("  移除虚拟设备 ...")
            try:
                if stream is not None:
                    stream.close()
                if bus_id is not None and device_id is not None:
                    server.remove_device(bus_id, device_id)
                if bus_id is not None:
                    server.remove_bus(bus_id)
            except Exception:                           # noqa: BLE001
                pass
        if server_process is not None and not args.keep_server:
            print("  关闭由本工具启动的 VIIPER ...")
            try:
                server_process.terminate()
            except Exception:                           # noqa: BLE001
                pass
        print("  已退出。")


if __name__ == "__main__":
    sys.exit(main())
