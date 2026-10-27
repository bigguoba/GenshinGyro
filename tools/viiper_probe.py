"""Probe the VIIPER API from Python (no client library needed).

The API is a TCP service on port 3242: each request is a single line terminated
by a null byte (``bus/list\\0``), the reply is JSON on the same connection.
Localhost needs no authentication.

    python tools\\viiper_probe.py bus/list
    python tools\\viiper_probe.py 'bus/1/add {"type":"dualsense"}'
    python tools\\viiper_probe.py --raw 'bus/1/1'          # show raw bytes of a stream reply
"""

from __future__ import annotations

import argparse
import json
import socket
import sys

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3242


def request(payload: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
            timeout: float = 5.0) -> str:
    """Send one request, return the raw reply text (server closes the connection)."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload.encode("utf-8") + b"\0")
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if len(chunks) > 4096:              # runaway guard
                break
    return b"".join(chunks).decode("utf-8", "replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send one request to the VIIPER API.")
    parser.add_argument("payload", help='request line, e.g. "bus/list" or \'bus/1/add {"type":"dualsense"}\'')
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--raw", action="store_true", help="print the raw reply, unparsed")
    args = parser.parse_args(argv)

    try:
        reply = request(args.payload, args.host, args.port, args.timeout)
    except OSError as exc:
        print(f"cannot reach VIIPER at {args.host}:{args.port}: {exc}")
        return 2

    if args.raw:
        print(repr(reply))
        return 0
    text = reply.strip()
    if not text:
        print("(empty reply: success, no payload)")
        return 0
    try:
        print(json.dumps(json.loads(text), indent=2, ensure_ascii=False))
    except ValueError:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
