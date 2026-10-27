"""Choosing which controller to read, shared by the CLI bridge and the GUI.

Two families are supported and both end in the same virtual DualShock 4:

* ``switch``  -- a Switch Pro Controller, mapped across device families;
* ``ds4``     -- a physical DualShock 4 (wired or Bluetooth), relayed 1:1.

The DualShock 4 case exists because a Bluetooth pad sends report ``0x11`` with a
two-byte header, which some games (Genshin Impact) do not read motion from, while
the virtual pad always emits the wired ``0x01`` report.
"""

from __future__ import annotations

import dualshock4
import switchpro

KINDS = ("auto", "switch", "ds4")

KIND_LABELS = {
    "auto": "自动（谁在就用手柄）",
    "switch": "Switch Pro 手柄",
    "ds4": "DualShock 4（有线/蓝牙）",
}


class NoControllerError(RuntimeError):
    pass


def detect_presence() -> dict[str, bool]:
    return {
        "switch": bool(switchpro.find_controller_interfaces()),
        "ds4": bool(dualshock4.find_ds4_interfaces()),
    }


def open_source(source: str = "auto", device_path: str | None = None, log=print):
    """Open the first usable controller; returns ``(kind, controller)``.

    ``auto`` prefers the Switch Pro Controller when both are connected, so an
    existing setup does not change behaviour just because a DualShock 4 is also
    paired; pass ``--source ds4`` to relay the DualShock 4 instead.
    """
    if source not in KINDS:
        raise ValueError(f"unknown source {source!r}")

    present = detect_presence()
    if source == "auto":
        if present["switch"] and present["ds4"]:
            log("both a Switch Pro Controller and a DualShock 4 are connected; "
                "using the Switch Pro (choose DualShock 4 to relay that instead)")
        wanted = [kind for kind in ("switch", "ds4") if present[kind]]
    else:
        wanted = [source]

    errors = []
    for kind in wanted:
        try:
            if kind == "switch":
                controller = switchpro.SwitchProController(path=device_path, log=log)
                controller.open()
                controller.initialize()
                return "switch", controller
            controller = dualshock4.DualShock4(path=device_path, log=log)
            controller.open()
            controller.initialize()
            return "ds4", controller
        except (switchpro.SwitchProError, dualshock4.DS4Error, OSError) as exc:
            errors.append(f"{kind}: {exc}")
    raise NoControllerError("; ".join(errors) if errors
                            else "no supported controller found")
