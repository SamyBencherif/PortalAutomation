"""The one concrete surface: an X11 display driven with xdotool.

This is deliberately OS-level rather than browser-level. There is no CDP
connection, no WebDriver, no `page.click("#id")` -- we move a real pointer on a
real display and photograph the result. Two reasons, in order of importance:

1. It is the only approach that ports. The next surface after this one is a
   native desktop client with no DOM at all, and a browser-shaped abstraction
   would have to be thrown away to get there. This one would not.

2. It keeps the automation honest about what it can actually see. A DOM-level
   driver can read a value the user cannot see on screen; this cannot, and the
   target was built with an iframe and a deferred-load spinner specifically to
   punish anything that confuses "in the document" with "visible".

The cost is real and worth stating: we are slower, and we inherit every
flakiness of a window manager. The mitigation is that every wait in this system
waits on *content*, never on a timer or a load event.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import io

from PIL import Image

from cua.artifact.schema import Point
from cua.surface.base import Frame, MouseButton, ScrollDirection, SurfaceError

# xdotool speaks button numbers; 4/5 are the scroll wheel, 6/7 horizontal.
_BUTTONS: dict[str, int] = {"left": 1, "middle": 2, "right": 3}
_SCROLL: dict[str, int] = {"up": 4, "down": 5, "left": 6, "right": 7}

# Typing too fast drops characters in legacy form fields that re-render on
# input. 12ms is empirically slow enough to be reliable and fast enough not to
# dominate a run.
TYPE_DELAY_MS = 12

# Anchoring never keys on the masthead, and that is not an accident: it is
# white-on-navy, and OCR reads light-on-dark text far worse than the
# dark-on-light body of the page. "NorthStar Core Banking" comes back as
# 'Ne' / 'ths' / 'ar Core' while the form labels beside it read perfectly.
# Worth knowing before trusting a screen dump that looks half-broken.


class X11Surface:
    """Drives an X display. Satisfies `Surface`."""

    def __init__(self, display: str | None = None, browser_window: str = "") -> None:
        self.display = display or os.environ.get("DISPLAY", ":99")
        self.browser_window = browser_window
        for tool in ("xdotool", "import"):
            if shutil.which(tool) is None:
                raise SurfaceError(
                    f"{tool!r} not found. The X11 surface needs xdotool and "
                    f"ImageMagick; both are installed in the workbench image."
                )

    # ------------------------------------------------------------- plumbing

    def _env(self) -> dict[str, str]:
        return {**os.environ, "DISPLAY": self.display}

    def _run(self, *args: str, capture: bool = False) -> str:
        proc = subprocess.run(
            args, env=self._env(), capture_output=True, check=False,
        )
        if proc.returncode != 0:
            raise SurfaceError(
                f"{args[0]} failed: {proc.stderr.decode('utf-8', 'replace').strip()}"
            )
        return proc.stdout.decode("utf-8", "replace") if capture else ""

    def _xdo(self, *args: str, capture: bool = False) -> str:
        return self._run("xdotool", *args, capture=capture)

    # ------------------------------------------------------------- observe

    def _grab(self) -> bytes:
        proc = subprocess.run(
            ["import", "-window", "root", "-silent", "png:-"],
            env=self._env(), capture_output=True, check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            raise SurfaceError(
                "screen capture failed: "
                + (proc.stderr.decode("utf-8", "replace").strip() or "no output")
            )
        return proc.stdout

    def observe(self) -> Frame:
        """Photograph the whole display, right now.

        Deliberately captures the root window rather than the browser's
        viewport: native dialogs, and anything the window manager draws, are
        part of what a human operator sees and therefore part of what we must
        see. The target raises a real `window.confirm()` on its irreversible
        action, and a viewport-only capture would miss it entirely.

        Deliberately does NOT wait for the screen to settle. I added that,
        suspecting mid-repaint captures were producing garbled OCR, then
        measured it: six consecutive grabs of a live page were byte-identical,
        and the garbling had two other causes entirely (11px text, since fixed
        by scaling the surface, and white-on-navy masthead text). Waiting for
        a stable frame would have been unfalsifiable padding hiding neither
        problem. Staleness is handled where it belongs -- `_await` in the
        replay engine polls on content, so an early frame costs one iteration.
        """
        png = self._grab()
        with Image.open(io.BytesIO(png)) as img:
            width, height = img.size
        return Frame(png=png, width=width, height=height)

    # --------------------------------------------------------------- input

    def navigate(self, url: str) -> None:
        """Go to a URL the way a person does: focus the address bar and type.

        Not a driver call. This is the only navigation primitive that also
        exists on a desktop app (where it becomes a menu or a shortcut), so
        keeping it human-shaped is what stops the abstraction leaking.
        """
        self._focus_browser()
        self.key("ctrl+l")
        time.sleep(0.15)
        # Select-all first: the address bar retains the previous URL, and
        # typing would otherwise append to it.
        self.key("ctrl+a")
        self.type_text(url)
        self.key("Return")

    def _focus_browser(self) -> None:
        """Bring the browser forward, cheaply.

        `--onlyvisible` is doing real work here, not tidying. Without it the
        search matches four Chromium windows -- the visible one plus hidden
        utility windows -- and `windowactivate --sync` waits on each in turn for
        one that will never activate. Measured: 15,293ms without the filter,
        36ms with it. That single flag was 77% of a replay's wall clock and read
        to a watcher as the automation hanging before it started.

        Skipping when the window is already focused, which is the common case,
        keeps even the 36ms off the usual path.
        """
        if not self.browser_window:
            return
        try:
            visible = self._xdo(
                "search", "--onlyvisible", "--name", self.browser_window, capture=True
            ).split()
            if not visible:
                return
            active = self._xdo("getactivewindow", capture=True).strip()
            if active in visible:
                return
            self._xdo("windowactivate", "--sync", visible[-1])
        except SurfaceError:
            # A missing window is not fatal here; the click lands on whatever is
            # focused and the checkpoint catches it.
            pass

    def _click(self, point: Point, button: int, modifiers: str | None, repeat: int) -> None:
        self._xdo("mousemove", "--sync", str(point.x), str(point.y))
        if modifiers:
            for mod in modifiers.split("+"):
                self._xdo("keydown", mod)
        try:
            self._xdo("click", "--repeat", str(repeat), str(button))
        finally:
            if modifiers:
                for mod in reversed(modifiers.split("+")):
                    self._xdo("keyup", mod)

    def click(
        self, point: Point, button: MouseButton = "left", modifiers: str | None = None
    ) -> None:
        self._click(point, _BUTTONS[button], modifiers, 1)

    def double_click(self, point: Point, modifiers: str | None = None) -> None:
        self._click(point, 1, modifiers, 2)

    def triple_click(self, point: Point, modifiers: str | None = None) -> None:
        self._click(point, 1, modifiers, 3)

    def move(self, point: Point) -> None:
        self._xdo("mousemove", "--sync", str(point.x), str(point.y))

    def drag(self, start: Point, end: Point, modifiers: str | None = None) -> None:
        self.move(start)
        if modifiers:
            for mod in modifiers.split("+"):
                self._xdo("keydown", mod)
        try:
            self._xdo("mousedown", "1")
            self.move(end)
            self._xdo("mouseup", "1")
        finally:
            if modifiers:
                for mod in reversed(modifiers.split("+")):
                    self._xdo("keyup", mod)

    def mouse_down(self) -> None:
        self._xdo("mousedown", "1")

    def mouse_up(self) -> None:
        self._xdo("mouseup", "1")

    def cursor_position(self) -> Point:
        out = self._xdo("getmouselocation", "--shell", capture=True)
        vals: dict[str, str] = {}
        for line in out.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                vals[k] = v
        return Point(x=int(vals.get("X", 0)), y=int(vals.get("Y", 0)))

    def type_text(self, text: str) -> None:
        # `--` guards against text that begins with a hyphen being parsed as a
        # flag; `--clearmodifiers` stops a still-held modifier from mangling it.
        self._xdo("type", "--clearmodifiers", "--delay", str(TYPE_DELAY_MS), "--", text)

    def key(self, keys: str, repeat: int = 1) -> None:
        self._xdo("key", "--clearmodifiers", "--repeat", str(max(1, repeat)), keys)

    def hold_key(self, keys: str, duration: float) -> None:
        self._xdo("keydown", keys)
        try:
            time.sleep(duration)
        finally:
            self._xdo("keyup", keys)

    def scroll(
        self,
        point: Point | None,
        direction: ScrollDirection,
        amount: int = 3,
        modifiers: str | None = None,
    ) -> None:
        if point is not None:
            self.move(point)
        self._click(point or self.cursor_position(), _SCROLL[direction], modifiers, max(1, amount))

    def wait(self, seconds: float) -> None:
        time.sleep(min(seconds, 300))


__all__ = ["X11Surface", "TYPE_DELAY_MS"]
