"""Where the program keeps its own files (report, captured corrections).

Running from source: next to the .py files.
Running frozen (PyInstaller): next to the .exe.

The distinction matters because a frozen build unpacks the code into an
``_internal`` directory; writing there would hide the report inside the bundle
and lose it on the next rebuild.
"""

from __future__ import annotations

import os
import sys


def app_dir() -> str:
    """Directory that user-visible files belong in."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def app_path(filename: str) -> str:
    return os.path.join(app_dir(), filename)


def resource_path(relative: str) -> str:
    """A file shipped *with* the program (icon, assets) rather than written by it.

    A frozen build unpacks its bundled data to ``sys._MEIPASS``; running from
    source the files sit next to this module.  Keep this separate from
    :func:`app_path`: the report and the captured fixes belong beside the exe,
    the read-only assets belong inside the bundle.
    """
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative)
