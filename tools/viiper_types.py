"""Probe which device type names this VIIPER build accepts.

The bundled build (0.1.9-rc4.6.6) uses the newer "v5" device names instead of the
upstream `dualsense`/`dualshock4` ones, so the exact string has to be discovered
rather than guessed.

    python tools\\viiper_types.py
    python tools\\viiper_types.py --types dualsensecombinedaudioduplexv5,foo
"""

from __future__ import annotations

import argparse
import json
import sys

from viiper_smoke import api


DEFAULT_CANDIDATES = [
    # names found in the shipped binary's strings
    "dualsensegamepadv5",
    "dualsenseaudioonlyduplexv5",
    "dualsensecombinedaudioduplexv5",
    "dualsenseedgecombinedaudioduplexv5",
    "dualsenseaudioonlyduplexv5rawinputevents",
    "dualsensecombinedaudioduplexv5rawinputevents",
    # upstream names, for reference
    "dualsense",
    "dualsenseedge",
    "dualshock4",
    "xbox360",
    "keyboard",
    "mouse",
    "ns2pro",
]


def try_type(device_type: str) -> tuple[str, str]:
    """Create a bus, try to add this device type, clean up.  Returns (verdict, detail)."""
    created = api("bus/create")
    bus_id = created.get("busId") if isinstance(created, dict) else None
    if bus_id is None:
        return "NO-BUS", str(created)
    payload = json.dumps({"type": device_type})
    added = api(f"bus/{bus_id}/add {payload}")
    if isinstance(added, dict) and "devId" in added:
        detail = (f"vid/pid={added.get('vid')}/{added.get('pid')} "
                  f"deviceSpecific={added.get('deviceSpecific')}")
        api(f"bus/{bus_id}/remove {added['devId']}")
        api(f"bus/{bus_id}/remove")
        return "OK", detail
    detail = added.get("detail") if isinstance(added, dict) else str(added)
    api(f"bus/{bus_id}/remove")
    return "REJECTED", str(detail)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--types", default="", help="comma separated candidates")
    args = parser.parse_args(argv)

    candidates = ([item.strip() for item in args.types.split(",") if item.strip()]
                  or DEFAULT_CANDIDATES)
    accepted = []
    for device_type in candidates:
        verdict, detail = try_type(device_type)
        print(f"  {verdict:8} {device_type:44} {detail[:110]}")
        if verdict == "OK":
            accepted.append(device_type)
    print(f"\naccepted: {accepted or 'none'}")
    return 0 if accepted else 1


if __name__ == "__main__":
    sys.exit(main())
