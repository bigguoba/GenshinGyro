"""Read the physical pad and the virtual one at the same time and compare them.

This answers the only question that matters for "the virtual pad only updates a few
times a second": do the *values* travel, or only the reports?

    python tools\\compare_physical_vs_virtual.py --seconds 12
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dualshock4  # noqa: E402
import hidwin  # noqa: E402


def open_pair():
    physical_items = [i for i in hidwin.enumerate_hid()
                      if i.get("vid") in (0x054C, 0x057E) and not hidwin.is_emulated(i)]
    virtual_items = [i for i in hidwin.enumerate_hid()
                     if i.get("vid") == 0x054C and hidwin.is_emulated(i)]
    if not physical_items:
        print("no physical pad present")
        return None, None
    if not virtual_items:
        print("no virtual pad present (is forwarding on?)")
        return None, None

    physical = hidwin.HidDevice(physical_items[0]["path"], physical_items[0]).open()
    virtual = hidwin.HidDevice(virtual_items[0]["path"], virtual_items[0]).open()
    physical.drain(64, 0)
    virtual.drain(64, 0)
    return physical, virtual


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=12.0)
    args = parser.parse_args()

    physical, virtual = open_pair()
    if physical is None or virtual is None:
        return 1

    print(f"physical: {hidwin.describe(physical.info)}")
    print(f"virtual : {hidwin.describe(virtual.info)}")
    print("move the left stick and tilt the pad now…")

    left: list[tuple[int, int]] = []      # (stick x, buttons)
    right: list[tuple[int, int]] = []
    vleft: list[tuple[int, int]] = []
    vright: list[tuple[int, int]] = []
    vgyro: list[tuple[int, int, int]] = []
    lgyro: list[tuple[int, int, int]] = []

    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        report = physical.read(timeout_ms=30)
        if report:
            state = dualshock4.parse_report(report, offset=3 if report[0] == 0x11 else 1)
            if state is not None:
                left.append((state.sticks[0], state.buttons))
                lgyro.append(state.gyro)
        report = virtual.read(timeout_ms=0)
        while report:
            state = dualshock4.parse_report(report, offset=1)
            if state is not None:
                vleft.append((state.sticks[0], state.buttons))
                vgyro.append(state.gyro)
            report = virtual.read(timeout_ms=0)

    physical.close()
    virtual.close()

    def report(label: str, rows: list[tuple[int, int]], gyros: list) -> None:
        if not rows:
            print(f"  {label}: nothing read")
            return
        sticks = [row[0] for row in rows]
        print(f"  {label}: {len(rows)} reports, distinct stick X = {len(set(sticks))}, "
              f"distinct buttons = {len(set(row[1] for row in rows))}, "
              f"distinct gyro = {len(set(gyros))}")

    print("")
    report("physical", left, lgyro)
    report("virtual ", vleft, vgyro)

    if left and vleft:
        physical_values = [row[0] for row in left]
        virtual_values = [row[0] for row in vleft]
        print(f"\n  stick X range  physical {min(physical_values)}..{max(physical_values)}"
              f"   virtual {min(virtual_values)}..{max(virtual_values)}")
        verdict = ("VALUES MOVE: the virtual pad follows the physical one"
                   if len(set(virtual_values)) > 5 else
                   "VALUES FROZEN: reports flow but the fields do not change")
        print(f"  -> {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
