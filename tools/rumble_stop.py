"""Stop the pad's vibration.

The rumble state is latched by the controller: it keeps buzzing at the last level
it was told, even after the report sender goes away.  One neutral report at the end
of a session is therefore not enough -- if the sender exits too abruptly the pad can
be left running.  This sends the neutral report several times, spaced out, and keeps
the handle open until then, so it is always safe to run:

    python tools\\rumble_stop.py
    python tools\\rumble_stop.py --count 12 --interval 0.03
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sources  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=10, help="neutral reports to send")
    parser.add_argument("--interval", type=float, default=0.03)
    parser.add_argument("--device", default=None)
    args = parser.parse_args(argv)

    print("打开手柄（NS 模式）…")
    kind, pad = sources.open_source("switch", args.device, log=print)
    try:
        for index in range(max(1, args.count)):
            pad.stop_rumble()
            time.sleep(max(0.0, args.interval))
        print(f"  已连续发送 {args.count} 次静音报文（全零载荷）")
        time.sleep(0.2)                      # let the last packet land before close
        print("  完成")
    finally:
        try:
            pad.close()
        except Exception:                    # noqa: BLE001 - best effort
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
