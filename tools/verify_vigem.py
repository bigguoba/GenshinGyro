"""Thin wrapper so the self-test can also be run directly from ``tools/``.

The implementation lives in ``selftest.py`` at the project root so that frozen
(PyInstaller) builds bundle it.  Run either of:

    python main.py --self-test
    python tools\\verify_vigem.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import selftest  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(selftest.run())
