"""Analyse a haptics capture written by tools\\viiper_audioonly.py.

The capture is a sequence of records: <u8 frame type><u16 payload length><payload>.
This prints the frame mix, payload length distribution, and -- most important --
where in the payload the samples live and how loud they are, so silence and real
haptics can be told apart.

    python tools\\haptics_analyse.py haptics-capture.bin
"""

from __future__ import annotations

import argparse
import collections
import struct
import sys

sys.path.insert(0, __file__.rsplit("\\tools\\", 1)[0])

import viiper  # noqa: E402

HEADER = b"VIIP HAPTICS CAPTURE 1\n"


def load(path: str) -> list[tuple[int, bytes]]:
    with open(path, "rb") as handle:
        data = handle.read()
    if data.startswith(HEADER):
        data = data[len(HEADER):]
    frames = []
    offset = 0
    while offset + 3 <= len(data):
        frame_type, length = struct.unpack_from("<BH", data, offset)
        offset += 3
        payload = data[offset:offset + length]
        offset += length
        if len(payload) < length:
            break
        frames.append((frame_type, payload))
    return frames


def peak(payload: bytes) -> int:
    if not payload:
        return 0
    return max(abs(value - 128) if False else abs(struct.unpack_from("<b", payload, i)[0])
               for i in range(len(payload)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="haptics-capture.bin")
    parser.add_argument("--frames", type=int, default=6, help="how many sample frames to dump")
    args = parser.parse_args(argv)

    frames = load(args.path)
    print(f"帧总数: {len(frames)}")
    kinds = collections.Counter(frame_type for frame_type, _ in frames)
    for frame_type, count in sorted(kinds.items()):
        print(f"  {viiper.FRAME_NAMES.get(frame_type, hex(frame_type)):24} {count:6} 帧")

    lengths = collections.Counter(len(payload) for _, payload in frames)
    print(f"\n载荷长度分布（前 6 种）:")
    for length, count in lengths.most_common(6):
        print(f"  {length:6} 字节 × {count}")

    # Look for structure: dump the first bytes of a few frames of the main type.
    main_type = kinds.most_common(1)[0][0]
    print(f"\n主要帧类型 {viiper.FRAME_NAMES.get(main_type, hex(main_type))}：")
    shown = 0
    for frame_type, payload in frames:
        if frame_type != main_type:
            continue
        shown += 1
        if shown > args.frames:
            break
        print(f"  #{shown} len={len(payload)} head={payload[:20].hex(' ')}")
        # print a byte-value histogram of the region after a small header
        for offset in (0, 4, 8, 12, 16):
            tail = payload[offset:]
            if len(tail) >= 64:
                values = [abs(struct.unpack_from("<b", tail, i)[0]) for i in range(64)]
                print(f"    从 +{offset:2} 起 64 字节: 峰值 {max(values):3}  "
                      f"均值 {sum(values) / len(values):6.2f}")
        diff = sum(1 for a, b in zip(payload, payload[1:]) if a != b)
        print(f"    帧内相邻字节不同处: {diff}/{len(payload) - 1}")

    # Are different frames actually different?  (i.e. is this live data or a
    # constant filler pattern)
    if len(frames) > 2:
        unique = len({payload for _, payload in frames})
        print(f"\n去重后不同载荷数: {unique} / {len(frames)}")

    # Where is the non-zero data?  Print the occupied byte ranges of one frame of
    # each type, plus the peak over the whole payload.
    for kind in sorted(kinds):
        payload = next(payload for frame_type, payload in frames if frame_type == kind)
        name = viiper.FRAME_NAMES.get(kind, hex(kind))
        nonzero = [index for index, value in enumerate(payload) if value]
        print(f"\n{name}: len={len(payload)} 非零字节 {len(nonzero)} 个")
        if nonzero:
            ranges = []
            start = previous = nonzero[0]
            for index in nonzero[1:]:
                if index == previous + 1:
                    previous = index
                    continue
                ranges.append((start, previous))
                start = previous = index
            ranges.append((start, previous))
            for start, end in ranges[:12]:
                chunk = payload[start:end + 1]
                print(f"  +{start:4}..{end:4} ({end - start + 1:3} 字节) {chunk[:16].hex(' ')}")
            peak_value = max(abs(struct.unpack_from("<b", payload, i)[0]) for i in nonzero)
            print(f"  整帧峰值 {peak_value}（若落在头部则只是头部字段，不是音频样本）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
