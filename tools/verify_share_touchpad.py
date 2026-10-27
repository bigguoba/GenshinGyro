"""Watch the virtual pad for the Share -> touchpad-click mapping.

The Python build verified this bit against real hardware; this checks the C# build
produces it too.  While it runs, press and release Share (or "-" on a Switch Pro)
a few times.

    python tools\\verify_share_touchpad.py --seconds 120
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dualshock4  # noqa: E402
import hidwin  # noqa: E402

TOUCHPAD = 0x02
SHARE = 1 << 12


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def log(message: str) -> None:
        print(message, flush=True)
        lines.append(message)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")

    candidates = [info for info in hidwin.enumerate_hid()
                  if info.get("vid") == 0x054C and hidwin.is_emulated(info)]
    if not candidates:
        log("no virtual DualShock 4 present: start the bridge first")
        return 1

    device = hidwin.HidDevice(candidates[0]["path"], candidates[0]).open()
    device.drain(max_reports=64, timeout_ms=0)
    log(f"watching {hidwin.describe(candidates[0])}")
    log("press and release Share a few times now…")

    touchpad_reports = 0
    share_leaked = 0
    transitions: list[str] = []
    last_state = 0
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        report = device.read(timeout_ms=120)
        if not report or report[0] != 0x01 or len(report) < 10:
            continue
        state = dualshock4.parse_report(report, offset=1)
        if state is None:
            continue

        touchpad = bool(state.special & TOUCHPAD)
        share = bool(state.buttons & SHARE)
        if touchpad:
            touchpad_reports += 1
            if share:
                share_leaked += 1          # both bits at once would be wrong
        if touchpad != bool(last_state & TOUCHPAD):
            transitions.append(f"t={time.monotonic() % 1000:6.2f}s "
                               f"touchpad={'on ' if touchpad else 'off'} "
                               f"share={'on' if share else 'off'}")
            log("  " + transitions[-1])
        last_state = (TOUCHPAD if touchpad else 0) | (SHARE if share else 0)

    device.close()
    log("")
    log(f"touchpad-on reports: {touchpad_reports}   "
        f"reports where Share leaked through: {share_leaked}")
    if touchpad_reports == 0:
        log("RESULT: no touchpad click seen (did you press Share?)")
        return 1
    if share_leaked:
        log("RESULT: FAIL  Share is still reported as Share while the touchpad is on")
        return 1
    log("RESULT: PASS  Share becomes the touchpad click, and is not duplicated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
