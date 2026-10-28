"""Small user-settings file, stored next to the program.

Only the values the window has to remember live here.  Everything is defensive by
design: a missing, unreadable, malformed or out-of-range value falls back to the
default, because a broken settings file must never stop the program from starting.

The file is ``settings.json`` beside the exe (frozen build) or beside the sources,
the same place the verification report and captured mapping go -- see :mod:`paths`.
"""

from __future__ import annotations

import json
import os
import time

import paths

#: Written when the file is missing or a value cannot be used.
DEFAULT_SETTINGS: dict = {
    "gyro_gain": 1.0,
    "deadzone_pitch": 0.0,
    "deadzone_yaw": 0.0,
    "deadzone_roll": 0.0,
    "mode": "off",
    #: exchange the virtual pad's ✕↔○ and □↔△.
    "swap_face_buttons": False,
    #: audio haptics: master switch plus the seven sliders and two switches.
    "haptics_enabled": False,
    "haptics_gain": 1.0,
    "haptics_sensitivity": 56.0,
    "haptics_attack": 0.85,
    "haptics_release_ms": 140.0,
    "haptics_dead_zone": 0.04,
    "haptics_gamma": 1.05,
    "haptics_split": 640.0,
    "haptics_dual_motor": True,
    "haptics_motor_swap": False,
}

#: Same limits as the GUI sliders.
GAIN_MIN, GAIN_MAX = 0.0, 5.0
DEADZONE_MIN, DEADZONE_MAX = 0.0, 5.0

#: Slider order, which is also the DS4 gyro order the game receives.
DEADZONE_AXES = ("pitch", "yaw", "roll")

#: Which checkbox was ticked last time.  A word keeps the file readable; anything
#: unknown falls back to "off", i.e. nothing forwarded.
MODE_DEFAULT = "off"
#: "ds5" = the same full forwarding as "full", but the virtual pad is a DualSense.
MODE_CHOICES = ("off", "full", "motion", "genshin", "ds5")

#: Audio haptics (the rumble feature).  The window shows six sliders; each one is
#: stored as the raw parameter the engine consumes, so ``settings.json`` stays
#: readable and hand-editable.  The ranges and decimals mirror ``haptics.RANGES``
#: (cross-checked by ``tools/test_haptics.py``); they live here as plain numbers so
#: this module keeps its "standard library only" promise.
HAPTICS_RANGES = {
    "gain": (0.0, 1.5),
    "sensitivity": (0.0, 100.0),
    "attack": (0.40, 1.00),
    #: 拖尾时长（毫秒）：越大余震越长。
    "release": (30.0, 900.0),
    "dead_zone": (0.0, 0.20),
    "gamma": (0.6, 2.2),
    "split": (250.0, 900.0),
}
HAPTICS_DECIMALS = {
    "gain": 2, "sensitivity": 1, "attack": 2, "release": 0, "dead_zone": 3,
    "gamma": 2, "split": 0,
}
#: slider key -> settings key
HAPTICS_KEYS = {
    "gain": "haptics_gain",
    "sensitivity": "haptics_sensitivity",
    "attack": "haptics_attack",
    "release": "haptics_release_ms",
    "dead_zone": "haptics_dead_zone",
    "gamma": "haptics_gamma",
    "split": "haptics_split",
}
#: switch key -> settings key
HAPTICS_FLAGS = {
    "dual": "haptics_dual_motor",
    "swap": "haptics_motor_swap",
}


def settings_path() -> str:
    return paths.app_path("settings.json")


def load(path: str | None = None) -> dict:
    """Read the settings; always returns a complete, usable dictionary.

    ``utf-8-sig`` so a file saved by an editor that adds a byte-order mark (Notepad
    on older Windows, PowerShell's Set-Content) still counts instead of silently
    falling back to the defaults.
    """
    values = dict(DEFAULT_SETTINGS)
    try:
        with open(path or settings_path(), encoding="utf-8-sig") as handle:
            stored = json.load(handle)
        if isinstance(stored, dict):
            values.update(stored)
    except (OSError, ValueError):
        pass                                    # missing or unreadable: defaults
    # The preset dropdown became six sliders; drop the old key when a file from
    # that build is loaded, so it stops being written back on every save.  The
    # tail used to be stored as a 0..1 per-block coefficient; the key is now
    # *_ms (milliseconds) so an old value cannot be misread as 0.16 ms.
    values.pop("haptics_preset", None)
    values.pop("haptics_release", None)
    return values


def save(values: dict, path: str | None = None, attempts: int = 5) -> str | None:
    """Write the settings; returns the path, or None when it could not be saved.

    A few retries: a directory that was created moments ago can refuse the first
    write while Windows finishes propagating access, and losing the user's
    sensitivity to that race would be unpleasant.  Failures are still swallowed --
    a read-only folder must never break the program.
    """
    target = path or settings_path()
    merged = dict(DEFAULT_SETTINGS)
    # Merge with what is already stored: saving one setting must not reset the
    # others (the sensitivity and the three deadzones are saved separately).
    merged.update(load(target))
    merged.update(values)
    for attempt in range(max(1, attempts)):
        try:
            directory = os.path.dirname(target)
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                # ensure_ascii=False so a Chinese preset name stays readable in a
                # file the module docstring invites people to hand-edit.
                json.dump(merged, handle, indent=2, sort_keys=True,
                          ensure_ascii=False)
            return target
        except OSError:
            if attempt + 1 < max(1, attempts):
                time.sleep(0.05)
    return None                             # read-only folder etc.: not fatal


def clamp_gain(value) -> float:
    """Coerce anything into a usable sensitivity (0.1 .. 10.0, one decimal)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_SETTINGS["gyro_gain"]
    if number != number:                        # NaN
        return DEFAULT_SETTINGS["gyro_gain"]
    return round(max(GAIN_MIN, min(GAIN_MAX, number)), 1)


def gyro_gain(path: str | None = None) -> float:
    return clamp_gain(load(path).get("gyro_gain"))


def set_gyro_gain(value, path: str | None = None) -> float:
    """Remember the sensitivity; returns the value actually stored."""
    gain = clamp_gain(value)
    save({"gyro_gain": gain}, path)
    return gain


def clamp_deadzone(value) -> float:
    """Coerce anything into a usable deadzone compensation (0.0 .. 5.0 °/s)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_SETTINGS["deadzone_pitch"]
    if number != number:                        # NaN
        return DEFAULT_SETTINGS["deadzone_pitch"]
    return round(max(DEADZONE_MIN, min(DEADZONE_MAX, number)), 1)


def motion_deadzone(path: str | None = None) -> tuple[float, float, float]:
    """The remembered (pitch, yaw, roll) compensation in degrees/second."""
    stored = load(path)
    return tuple(clamp_deadzone(stored.get(f"deadzone_{axis}")) for axis in DEADZONE_AXES)


def set_motion_deadzone(values, path: str | None = None) -> tuple[float, float, float]:
    """Remember the per-axis compensation; returns what was actually stored."""
    triple = tuple(clamp_deadzone(value) for value in values)
    save({f"deadzone_{axis}": value for axis, value in zip(DEADZONE_AXES, triple)}, path)
    return triple
    return triple


def mode(path: str | None = None) -> str:
    """Which forwarding mode was active when the program last closed."""
    stored = load(path).get("mode")
    return stored if stored in MODE_CHOICES else MODE_DEFAULT


def set_mode(value: str, path: str | None = None) -> str:
    """Remember the checkbox state; returns what was actually stored."""
    chosen = value if value in MODE_CHOICES else MODE_DEFAULT
    save({"mode": chosen}, path)
    return chosen


def clamp_flag(value) -> bool:
    """Coerce anything into a usable on/off value.

    JSON holds real booleans, but a hand-edited file may contain ``"true"``,
    ``1`` or ``""``.  Those have an obvious meaning; everything else falls back
    to off, which is the safe direction for a switch that changes the controls.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
    return False


def swap_face_buttons(path: str | None = None) -> bool:
    """Whether the face buttons were being exchanged when the program last closed."""
    return clamp_flag(load(path).get("swap_face_buttons"))


def set_swap_face_buttons(value, path: str | None = None) -> bool:
    """Remember the face-button exchange; returns what was actually stored."""
    chosen = clamp_flag(value)
    save({"swap_face_buttons": chosen}, path)
    return chosen


# --------------------------------------------------------------------------- #
# Audio haptics
# --------------------------------------------------------------------------- #

def clamp_haptics(key: str, value) -> float:
    """Coerce anything into a usable rumble parameter (range from HAPTICS_RANGES)."""
    if key not in HAPTICS_RANGES:
        return 0.0
    low, high = HAPTICS_RANGES[key]
    default = DEFAULT_SETTINGS[HAPTICS_KEYS[key]]
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:                        # NaN
        return default
    return round(max(low, min(high, number)), HAPTICS_DECIMALS[key])


def haptics_tuning(path: str | None = None) -> dict:
    """The remembered rumble parameters, as the engine's ``apply_tuning`` wants them."""
    stored = load(path)
    values = {key: clamp_haptics(key, stored.get(HAPTICS_KEYS[key]))
              for key in HAPTICS_KEYS}
    for name, settings_key in HAPTICS_FLAGS.items():
        values[name] = clamp_flag(stored.get(settings_key))
    return values


def save_haptics_tuning(values: dict, path: str | None = None) -> dict:
    """Remember a whole set of rumble parameters; returns what was stored."""
    values = values or {}
    numbers = {key: clamp_haptics(key, values.get(key, DEFAULT_SETTINGS[HAPTICS_KEYS[key]]))
               for key in HAPTICS_KEYS}
    flags = {name: clamp_flag(values.get(name, DEFAULT_SETTINGS[settings_key]))
             for name, settings_key in HAPTICS_FLAGS.items()}
    stored = {HAPTICS_KEYS[key]: value for key, value in numbers.items()}
    stored.update({HAPTICS_FLAGS[name]: value for name, value in flags.items()})
    save(stored, path)
    result = dict(numbers)
    result.update(flags)
    return result


def haptics_enabled(path: str | None = None) -> bool:
    """Whether the rumble switch was on when the program last closed."""
    return clamp_flag(load(path).get("haptics_enabled"))


def set_haptics_enabled(value, path: str | None = None) -> bool:
    """Remember the master switch; returns what was actually stored."""
    chosen = clamp_flag(value)
    save({"haptics_enabled": chosen}, path)
    return chosen
