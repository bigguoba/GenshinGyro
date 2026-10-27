"""End-to-end check of the VIIPER input path, without any game.

Creates a virtual DualSense, attaches the framed stream, pushes distinctive
values, and reads the *emulated* HID device back to see whether Windows received
them.  DualSense USB input report 0x01 (64 bytes) keeps sticks at 1..4 and gyro at
12..17, so a match proves the whole chain.

    python tools\\viiper_readback.py
"""

from __future__ import annotations

import sys
import threading
import time

sys.path.insert(0, __file__.rsplit("\\tools\\", 1)[0])
sys.path.insert(0, __file__.rsplit("\\tools\\", 1)[0] + "\\tools")

import hidwin  # noqa: E402
import viiper_smoke as smoke  # noqa: E402

DEVICE_TYPE = "dualsensecombinedaudioduplexv5"
DS_VENDOR, DS_PRODUCT = 0x054C, 0x0CE6


def find_virtual_pad():
    for info in hidwin.enumerate_hid():
        if info.get("vid") == DS_VENDOR and info.get("pid") == DS_PRODUCT:
            return info
    return None


def main() -> int:
    print("1) create bus + device")
    created = smoke.api("bus/create")
    bus_id = created.get("busId")
    added = smoke.api(f'bus/{bus_id}/add {{"type":"{DEVICE_TYPE}"}}')
    if "devId" not in added:
        print(f"   failed: {added}")
        return 1
    device_id = added["devId"]
    print(f"   bus={bus_id} dev={device_id} usbipPort={added.get('usbipPort')}")

    stream = smoke.socket.create_connection((smoke.HOST, smoke.PORT), timeout=5.0)
    stream.sendall(f"bus/{bus_id}/{device_id}".encode() + b"\0")
    print("2) stream attached")

    # Distinctive values: sticks hard over, gyro (1000, -2000, 3000).
    payload = smoke.build_state(lx=100, ly=-100, rx=50, ry=-50,
                                gyro=(1000, -2000, 3000), accel=(0, 0, -8192))
    stop = threading.Event()

    def pump():
        seq = 0
        next_send = time.perf_counter()
        while not stop.is_set():
            stream.sendall(smoke.frame(smoke.FRAME_INPUT_STATE, payload, seq))
            seq = (seq + 1) & 0xFFFFFFFF
            next_send += 0.004
            remaining = next_send - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
            else:
                next_send = time.perf_counter()

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    try:
        time.sleep(2.0)                       # let Windows enumerate the device
        info = find_virtual_pad()
        if info is None:
            print("3) virtual DualSense not found in HID")
            return 1
        print(f"3) found {info.get('product')!r} "
              f"usage={info.get('usage_page', 0):04X}:{info.get('usage', 0):04X}")
        print(f"   info keys: {sorted(info)}")

        device = hidwin.HidDevice(info.get("path"), info).open()
        try:
            latest = None
            deadline = time.perf_counter() + 2.0
            while time.perf_counter() < deadline:
                packet = device.read(200)
                if packet:
                    latest = packet
            if not latest:
                print("4) no input reports from the virtual pad")
                return 1
            print(f"4) input report: {len(latest)} bytes, id=0x{latest[0]:02X}")
            print(f"   sticks 1..4 = {list(latest[1:5])}  (want [228, 28, 178, 78])")
            print(f"   gyro 12..18 = {list(latest[12:18])}  (want 1000,-2000,3000 = e8 03 30 f8 b8 0b)")
            ok_sticks = list(latest[1:5]) == [228, 28, 178, 78]
            ok_gyro = list(latest[12:18]) == [0xE8, 0x03, 0x30, 0xF8, 0xB8, 0x0B]
            print(f"   sticks match: {ok_sticks}   gyro match: {ok_gyro}")
        finally:
            device.close()
    finally:
        stop.set()
        thread.join(timeout=1.0)
        stream.close()
        smoke.api(f"bus/{bus_id}/remove {device_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
