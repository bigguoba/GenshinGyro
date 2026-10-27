"""Print VIIPER's device telemetry for an audio-only DualSense (what the host wrote).

The tool creates the audio-only device, waits a moment, asks the API for the
device list (which carries counters such as speakerBytesReceived), then removes
the device.  This is the authoritative answer to "did anything write audio to that
endpoint".

    python tools\\viiper_telemetry_probe.py
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import viiper  # noqa: E402

INTERESTING = (
    "speakerBytesReceived", "speakerPayloadsReceived", "speakerInterfaceActive",
    "speakerBytesWritten", "speakerBytesEnqueued", "speakerPayloadsEnqueued",
    "microphonePacketsRead", "microphoneInterfaceActive", "hapticsConverter",
    "inputStatesReceived", "mediaReportDrops",
)


def main() -> int:
    server = viiper.ViiperServer()
    bus_id = server.create_bus()
    added = server.add_device(bus_id, "dualsenseaudioonlyduplexv5")
    device_id = str(added["devId"])
    stream = viiper.DeviceStream(server, bus_id, device_id, log=print).open()
    print(f"设备已建立：bus={bus_id} dev={device_id}")
    try:
        for round_index in range(3):
            time.sleep(2.0)
            listing = server.request(f"bus/{bus_id}/list")
            print(f"\n--- 第 {round_index + 1} 次 bus/{bus_id}/list ---")
            print(json.dumps(listing, indent=2, ensure_ascii=False)[:1800])
    finally:
        stream.close()
        server.remove_device(bus_id, device_id)
        server.remove_bus(bus_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
