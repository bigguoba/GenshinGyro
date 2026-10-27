"""List every HID interface Windows exposes, with the fields the bridge cares about.

Useful to check whether a virtual pad (and its usage pages) really appeared.

    python tools\\hid_list.py
    python tools\\hid_list.py --filter 054C
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __file__.rsplit("\\tools\\", 1)[0])

import hidwin  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", default="", help="substring of VID:PID / product")
    args = parser.parse_args(argv)

    items = hidwin.enumerate_hid()
    print(f"HID interfaces: {len(items)}")
    for info in items:
        vid = info.get("vid") or 0
        pid = info.get("pid") or 0
        label = (f"{vid:04X}:{pid:04X} "
                 f"usage={info.get('usage_page', 0):04X}:{info.get('usage', 0):04X} "
                 f"in={info.get('input_report_len', info.get('input_len'))} "
                 f"out={info.get('output_len')} "
                 f"product={info.get('product')!r}")
        if args.filter and args.filter.casefold() not in label.casefold():
            continue
        try:
            emulated = hidwin.is_emulated(info)
        except Exception as exc:                     # noqa: BLE001 - diagnostic tool
            emulated = f"?({exc})"
        print(f"  {label} emulated={emulated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
