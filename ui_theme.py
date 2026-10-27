"""Colours, typography and the four custom-drawn controls.

Design system for a dark desktop utility: **one accent, used sparingly**.  Slate
surfaces carry the structure, and the green accent is reserved for "it is
running" and "this passed" -- so a window full of sliders does not shout at the
viewer.  Windows has no Inter installed, so Microsoft YaHei UI / Consolas carry
the type.

Tk cannot restyle its own controls far enough for this look, so the switch, the
slider, the value box and the choice box are drawn on canvases.  Tk's canvas
has no anti-aliasing, which shows up as stair-stepped circles and rounded
corners -- the one thing that makes a hand-drawn Tk window look cheap on a 1080p
recording.  Every shape is therefore composed on a Pillow image at
``SUPERSAMPLE`` times the size and scaled back down with LANCZOS.  Pillow is
optional: without it the widgets fall back to plain canvas drawing, so the
program still runs on the standard library alone.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

# -- anti-aliasing (optional) ---------------------------------------------- #

try:                                    # Pillow gives us real anti-aliasing
    from PIL import Image, ImageDraw, ImageTk

    HAVE_PIL = True
except Exception:                       # no Pillow: fall back to canvas shapes
    Image = ImageDraw = ImageTk = None  # type: ignore[assignment]
    HAVE_PIL = False

#: Shapes are drawn this many times oversized, then scaled back down.  3 is
#: enough to remove the staircase without making the redraws noticeable.
SUPERSAMPLE = 3

# -- palette: semantic tokens ---------------------------------------------- #

BG = "#080D18"             # page background
SURFACE = "#202B45"        # card / row surface
SURFACE_ALT = "#182238"    # recessed: slider track, value box
BORDER = "#3B4E76"         # card outline and hairline inside a card
BORDER_STRONG = "#455A86"  # switch track when off, slider knob ring
TEXT = "#F8FAFC"           # primary text
TEXT_MUTED = "#94A3B8"     # secondary text (>=3:1 on SURFACE)
ACCENT = "#22C55E"         # the single accent: switch-on, slider fill, "running"
ACCENT_DIM = "#166534"
DANGER = "#EF4444"
KNOB = "#F8FAFC"

# -- spacing scale (4/8/16/24) --------------------------------------------- #

SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 16
SPACE_LG = 24

# -- type scale ------------------------------------------------------------ #

SIZE_SECTION = 9           # small, muted section label
SIZE_TITLE = 13
SIZE_BODY = 10
SIZE_SMALL = 9
SIZE_READOUT = 12          # the live numbers: the one thing worth reading

FAMILY = "Microsoft YaHei UI"
FAMILY_MONO = "Consolas"


def font(size: int = SIZE_BODY, bold: bool = False) -> tkfont.Font:
    return tkfont.Font(family=FAMILY, size=size,
                       weight="bold" if bold else "normal")


def mono(size: int = SIZE_BODY) -> tkfont.Font:
    return tkfont.Font(family=FAMILY_MONO, size=size)


def mix(first: str, second: str, t: float) -> str:
    """Blend two ``#rrggbb`` colours; ``t`` 0 gives *first*, 1 gives *second*."""
    t = max(0.0, min(1.0, float(t)))
    left = tuple(int(first[index:index + 2], 16) for index in (1, 3, 5))
    right = tuple(int(second[index:index + 2], 16) for index in (1, 3, 5))
    return "#%02X%02X%02X" % tuple(
        int(round(a + (b - a) * t)) for a, b in zip(left, right))


#: Breathing-dot colours, dim -> full accent.  Gui picks one per frame.
PULSE = tuple(mix(ACCENT_DIM, ACCENT, index / 9) for index in range(1, 10))


# -- drawing --------------------------------------------------------------- #

def rounded_rect(canvas: tk.Canvas, x0: float, y0: float, x1: float, y1: float,
                 radius: float, **kwargs) -> int:
    """A rounded rectangle as one smoothed canvas polygon (fallback path)."""
    radius = max(0.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
    points = [
        x0 + radius, y0, x1 - radius, y0, x1, y0,
        x1, y0 + radius, x1, y1 - radius, x1, y1,
        x1 - radius, y1, x0 + radius, y1, x0, y1,
        x0, y1 - radius, x0, y0 + radius, x0, y0,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=18, **kwargs)


class Painter:
    """Collects one widget's shapes, anti-aliased when Pillow is available.

    Coordinates are plain widget pixels; the Pillow backend multiplies them by
    ``SUPERSAMPLE`` internally.  Call :meth:`finish` last -- it puts the composed
    image on the canvas and returns its item id (or ``None`` in the fallback
    path, where every shape is already a canvas item).  Anything drawn on the
    canvas *after* ``finish`` lands on top of the shapes, which is how the
    choice box keeps its text crisp (Tk's own text rendering is anti-aliased).
    """

    def __init__(self, canvas: tk.Canvas, width: int, height: int, bg: str):
        self._canvas = canvas
        self._width = max(1, int(width))
        self._height = max(1, int(height))
        self._image = None
        self._draw = None
        if HAVE_PIL:
            self._image = Image.new("RGB", (self._width * SUPERSAMPLE,
                                            self._height * SUPERSAMPLE), bg)
            self._draw = ImageDraw.Draw(self._image)

    # -- shapes ------------------------------------------------------- #

    def rrect(self, x0: float, y0: float, x1: float, y1: float,
              radius: float, fill: str | None = None,
              outline: str | None = None, width: float = 1) -> None:
        if self._draw is None:
            rounded_rect(self._canvas, x0, y0, x1, y1, radius, fill=fill,
                         outline=outline, width=width)
            return
        scale = SUPERSAMPLE
        radius = max(0.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
        self._draw.rounded_rectangle(
            [x0 * scale, y0 * scale, x1 * scale, y1 * scale],
            radius=radius * scale, fill=fill, outline=outline,
            width=max(1, int(round(width * scale))))

    def pill(self, x0: float, y0: float, x1: float, y1: float,
             fill: str | None = None, outline: str | None = None,
             width: float = 1) -> None:
        """A rounded rectangle whose radius is half its height."""
        self.rrect(x0, y0, x1, y1, (y1 - y0) / 2, fill=fill, outline=outline,
                   width=width)

    def ellipse(self, x0: float, y0: float, x1: float, y1: float,
                fill: str | None = None, outline: str | None = None,
                width: float = 1) -> None:
        if self._draw is None:
            self._canvas.create_oval(x0, y0, x1, y1, fill=fill,
                                     outline=outline if outline else fill,
                                     width=width)
            return
        scale = SUPERSAMPLE
        self._draw.ellipse([x0 * scale, y0 * scale, x1 * scale, y1 * scale],
                           fill=fill, outline=outline,
                           width=max(1, int(round(width * scale))))

    # -- commit ------------------------------------------------------- #

    def finish(self) -> int | None:
        """Scale the image down, put it on the canvas, return its item id."""
        if self._draw is None or self._image is None:
            return None
        image = self._image.resize((self._width, self._height), Image.LANCZOS)
        # master= ties the photo to *this* canvas's interpreter.  Without it the
        # image lands in whatever Tk happens to be the default root, which breaks
        # the moment a second Tk() exists (the test suite opens several).
        photo = ImageTk.PhotoImage(image, master=self._canvas)
        item = self._canvas.create_image(0, 0, anchor="nw", image=photo)
        # Tk does not own the image; without a live reference it is collected
        # and the widget goes blank.
        self._canvas._aa_photo = photo
        return item


# -- layout helpers -------------------------------------------------------- #

class RoundedFrame(tk.Canvas):
    """A card that grows to fit whatever is placed inside it."""

    def __init__(self, master, radius: float = 10.0, padding: tuple[int, int] = (12, 6),
                 fill: str = SURFACE, page: str = BG, **kwargs):
        super().__init__(master, bg=page, highlightthickness=0, bd=0, height=10,
                         **kwargs)
        self._radius = radius
        self._padding = padding
        self._fill = fill
        self._page = page
        self._shape: int | None = None
        self.content = tk.Frame(self, bg=fill)
        self._window = self.create_window(padding[0], padding[1],
                                          window=self.content, anchor="nw")
        self.bind("<Configure>", self._redraw)
        self.content.bind("<Configure>", self._resize_to_content)
        self.after_idle(self._resize_to_content)

    def _resize_to_content(self, event=None) -> None:
        if event is not None:
            height = event.height
        else:
            self.content.update_idletasks()
            height = self.content.winfo_reqheight()
        wanted = int(height) + 2 * self._padding[1]
        if abs(int(self["height"]) - wanted) > 1:
            self.configure(height=wanted)

    def _redraw(self, event=None) -> None:
        width = self.winfo_width()
        height = self.winfo_height()
        if width < 4 or height < 4:
            return
        if self._shape is not None:
            self.delete(self._shape)
            self._shape = None
        painter = Painter(self, width, height, self._page)
        painter.rrect(0.5, 0.5, width - 1.5, height - 1.5, self._radius,
                      fill=self._fill, outline=BORDER)
        self._shape = painter.finish()
        if self._shape is not None:
            self.tag_lower(self._shape)
        self.coords(self._window, self._padding[0], self._padding[1])
        self.itemconfigure(self._window, width=max(10, width - 2 * self._padding[0]))


def section_label(master, text: str, page: str = BG, top: int = SPACE_MD) -> tk.Label:
    """The small, muted group heading used above a block of rows."""
    label = tk.Label(master, text=text, bg=page, fg=TEXT_MUTED,
                     font=font(SIZE_SECTION), anchor="w")
    label.pack(fill="x", padx=SPACE_XS, pady=(top, SPACE_XS))
    return label


# -- controls -------------------------------------------------------------- #

class ToggleSwitch(tk.Canvas):
    """Pill switch: recessed track when off, accent track when on."""

    WIDTH, HEIGHT = 44, 22

    def __init__(self, master, variable: tk.BooleanVar, command=None,
                 bg: str = SURFACE, scale: float = 1.0):
        width, height = int(self.WIDTH * scale), int(self.HEIGHT * scale)
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2",
                         takefocus=1)
        self.scale = scale
        self._var = variable
        self._command = command
        self._trace = variable.trace_add("write", lambda *_: self._draw())
        self.bind("<Button-1>", self._click)
        self.bind("<space>", self._click)
        self.bind("<Return>", self._click)
        self.bind("<FocusIn>", lambda _e: self._draw())
        self.bind("<FocusOut>", lambda _e: self._draw())
        self._draw()

    def destroy(self) -> None:
        try:
            self._var.trace_remove("write", self._trace)
        except Exception:
            pass
        super().destroy()

    def _click(self, _event=None) -> None:
        self._var.set(not self._var.get())
        if self._command is not None:
            self._command()

    def _draw(self) -> None:
        self.delete("all")
        width = int(self.WIDTH * self.scale)
        height = int(self.HEIGHT * self.scale)
        on = bool(self._var.get())
        painter = Painter(self, width, height, self["bg"])
        painter.pill(1, 1, width - 1, height - 1,
                     fill=ACCENT if on else SURFACE_ALT,
                     outline=ACCENT_DIM if on else BORDER_STRONG)
        pad = 3 * self.scale
        radius = height / 2 - pad
        cx = (width - pad - radius) if on else (pad + radius)
        painter.ellipse(cx - radius, height / 2 - radius, cx + radius,
                        height / 2 + radius, fill=KNOB, outline=KNOB)
        painter.finish()


class ValueBox(tk.Canvas):
    """Recessed field for a number; the entry inside behaves like a normal Entry."""

    def __init__(self, master, width: int = 64, height: int = 26, bg: str = SURFACE,
                 scale: float = 1.0):
        super().__init__(master, width=int(width * scale), height=int(height * scale),
                         bg=bg, highlightthickness=0, bd=0)
        width_px, height_px = int(width * scale), int(height * scale)
        painter = Painter(self, width_px, height_px, bg)
        painter.rrect(1, 1, width_px - 1, height_px - 1, 6 * scale,
                      fill=SURFACE_ALT, outline=BORDER)
        painter.finish()
        self.entry = tk.Entry(self, width=5, justify="center", bg=SURFACE_ALT,
                              fg=TEXT, relief="flat", bd=0, insertbackground=ACCENT,
                              font=mono(11))
        self.create_window(width_px / 2, height_px / 2, window=self.entry)

    # the call sites treat this like a plain tk.Entry
    def get(self) -> str:
        return self.entry.get()

    def delete(self, first, last=None) -> None:
        self.entry.delete(first, last)

    def insert(self, index, text) -> None:
        self.entry.insert(index, text)

    def bind(self, sequence=None, func=None, add=None):
        return self.entry.bind(sequence, func, add)

    def focus_set(self) -> None:
        self.entry.focus_set()


class DiamondSlider(tk.Canvas):
    """Slider: recessed track, accent fill up to the handle, neutral knob.

    Named for history; the handle has been a round knob since the second
    palette.  Keeps the part of the ``tk.Scale`` interface the rest of the
    program uses (``get``/``set``/``cget``), so behaviour is unchanged.

    The accent colours only the travelled part of the track: the knob stays
    neutral so four sliders sitting next to each other do not turn into four
    competing green dots.
    """

    HEIGHT = 26

    def __init__(self, master, variable: tk.DoubleVar, from_: float, to: float,
                 step: float, command=None, bg: str = SURFACE, scale: float = 1.0):
        super().__init__(master, height=int(self.HEIGHT * scale), bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.scale = scale
        self._var = variable
        self._from, self._to, self._step = float(from_), float(to), float(step)
        self._command = command
        self._dragging = False
        self._trace = variable.trace_add("write", lambda *_: self._draw())
        self.bind("<Left>", lambda _e: self._apply(self.get() - self._step))
        self.bind("<Right>", lambda _e: self._apply(self.get() + self._step))
        self.bind("<Home>", lambda _e: self._apply(self._from))
        self.bind("<End>", lambda _e: self._apply(self._to))
        self.bind("<FocusIn>", lambda _e: self._draw())
        self.bind("<FocusOut>", lambda _e: self._draw())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<MouseWheel>", self._wheel)

    def destroy(self) -> None:
        try:
            self._var.trace_remove("write", self._trace)
        except Exception:
            pass
        super().destroy()

    # -- tk.Scale-compatible surface --------------------------------------- #

    def get(self) -> float:
        return float(self._var.get())

    def set(self, value) -> None:
        self._var.set(self._quantise(value))
        self._draw()

    def cget(self, key):
        if key == "to":
            return self._to
        if key == "from":
            return self._from
        if key == "resolution":
            return self._step
        if key == "troughcolor":
            return SURFACE_ALT
        return super().cget(key)

    # -- geometry ---------------------------------------------------------- #

    def _span(self) -> tuple[float, float]:
        knob = 8 * self.scale
        return knob + 2, max(knob + 3, self.winfo_width() - knob - 2)

    def _quantise(self, value) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = self._from
        number = max(self._from, min(self._to, number))
        steps = round((number - self._from) / self._step) if self._step else 0
        return round(self._from + steps * self._step, 4)

    def _value_at(self, x: float) -> float:
        left, right = self._span()
        ratio = 0.0 if right <= left else (x - left) / (right - left)
        return self._quantise(self._from + ratio * (self._to - self._from))

    def _draw(self) -> None:
        self.delete("all")
        left, right = self._span()
        height = int(self.HEIGHT * self.scale)
        middle = height / 2
        thickness = max(4, int(5 * self.scale))
        painter = Painter(self, self.winfo_width(), height, self["bg"])
        # recessed track, then the accent fill up to the handle
        painter.pill(left, middle - thickness / 2, right, middle + thickness / 2,
                     fill=SURFACE_ALT)
        span = (self._to - self._from) or 1.0
        ratio = (self.get() - self._from) / span
        cx = left + ratio * (right - left)
        if cx - left > thickness / 2:
            painter.pill(left, middle - thickness / 2, cx, middle + thickness / 2,
                         fill=ACCENT)
        radius = 7 * self.scale
        focused = self.focus_get() is self
        painter.ellipse(cx - radius, middle - radius, cx + radius, middle + radius,
                        fill=KNOB,
                        outline=ACCENT if focused else BORDER_STRONG,
                        width=max(2, int(2 * self.scale)))
        painter.finish()

    # -- interaction ------------------------------------------------------- #

    def _press(self, event) -> None:
        self._dragging = True
        self._apply(self._value_at(event.x))

    def _drag(self, event) -> None:
        if self._dragging:
            self._apply(self._value_at(event.x))

    def _release(self, _event=None) -> None:
        self._dragging = False

    def _wheel(self, event) -> None:
        self._apply(self.get() + (self._step if event.delta > 0 else -self._step))

    def _apply(self, value: float) -> None:
        self._var.set(value)
        self._draw()
        if self._command is not None:
            self._command(value)


class ChoiceBox(tk.Canvas):
    """Recessed field showing the current choice and a chevron."""

    HEIGHT = 26

    def __init__(self, master, variable: tk.StringVar, values, command=None,
                 width: int = 132, bg: str = SURFACE, scale: float = 1.0):
        super().__init__(master, width=int(width * scale),
                         height=int(self.HEIGHT * scale), bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.scale = scale
        self._var = variable
        self._values = list(values)
        self._command = command
        self._selected_callback = None
        self.bind("<Button-1>", self._open)
        self._var.trace_add("write", lambda *_: self._draw())
        self._draw()

    def cget(self, key):
        if key == "values":
            return tuple(self._values)
        return super().cget(key)

    def bind(self, sequence=None, func=None, add=None):
        if sequence == "<<ComboboxSelected>>":
            self._selected_callback = func
            return None
        return super().bind(sequence, func, add)

    def set_values(self, values) -> None:
        self._values = list(values)
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        width = int(self.winfo_reqwidth())
        height = int(self.HEIGHT * self.scale)
        if width < 4 or height < 4:
            return
        painter = Painter(self, width, height, self["bg"])
        painter.rrect(1, 1, width - 1, height - 1, 6 * self.scale,
                      fill=SURFACE_ALT, outline=BORDER)
        painter.finish()
        # text goes on top of the image, drawn by Tk (already anti-aliased)
        self.create_text(10 * self.scale, height / 2, text=self._var.get(),
                         anchor="w", fill=TEXT, font=font(SIZE_BODY))
        self.create_text(width - 11 * self.scale, height / 2 - 1 * self.scale,
                         text="▾", anchor="e", fill=TEXT_MUTED, font=font(SIZE_BODY))

    def _open(self, _event=None) -> None:
        menu = tk.Menu(self, tearoff=0, bg=SURFACE, fg=TEXT,
                       activebackground=ACCENT_DIM, activeforeground=TEXT,
                       relief="flat", bd=0, font=font(SIZE_BODY))
        for value in self._values:
            menu.add_command(label=value, command=lambda v=value: self._choose(v))
        try:
            menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height())
        finally:
            menu.grab_release()

    def _choose(self, value: str) -> None:
        self._var.set(value)
        self._draw()
        if self._selected_callback is not None:
            self._selected_callback(None)
        if self._command is not None:
            self._command()
