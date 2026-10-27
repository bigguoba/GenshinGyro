"""Drive the Python relay against the physical pad and report what reaches the pad.

Same measurement as the C# bridge log: how many *distinct* motion values per second
actually land on the virtual DualShock 4.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dualshock4  # noqa: E402
import hidwin  # noqa: E402
import mapping  # noqa: E402
import vigem  # noqa: E402


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0

    pad = dualshock4.DualShock4()
    pad.open()
    pad.initialize()

    client = vigem.ViGEmClient()
    client.connect()
    target = client.create_ds4()
    target.plug_in()

    relay = mapping.DS4ToDS4Relay(pad, target, mapping.MappingConfig())
    relay.start_streaming()

    time.sleep(0.5)
    infos = [i for i in hidwin.enumerate_hid()
             if i.get("vid") == 0x054C and hidwin.is_emulated(i)]
    if not infos:
        print("no virtual pad")
        return 1

    device = hidwin.HidDevice(infos[0]["path"], infos[0]).open()
    device.drain(64, 0)

    seen: set = set()
    reports = 0
    start = time.monotonic()
    last_beat = start
    submitted_before = relay.submitted
    while time.monotonic() - start < seconds:
        state = pad.read_state(timeout_ms=200)
        if state is not None:
            relay.push_state(state)

        report = device.read(timeout_ms=0)
        while report:
            if report[0] == 0x01 and len(report) >= 10:
                parsed = dualshock4.parse_report(report, offset=1)
                if parsed is not None:
                    seen.add(parsed.gyro)
                    reports += 1
            report = device.read(timeout_ms=0)

        now = time.monotonic()
        if now - last_beat >= 1.0:
            print(f"  python relay: padReports/s={reports} distinctGyro/s={len(seen)} "
                  f"relaySubmits/s={relay.submitted - submitted_before}")
            seen.clear()
            reports = 0
            last_beat = now
            submitted_before = relay.submitted

    device.close()
    relay.stop_streaming()
    target.unplug()
    client.close()
    pad.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
