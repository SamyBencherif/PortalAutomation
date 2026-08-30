"""The seam between "how we perceive and act on a surface" and "the flow".

Everything above this line -- artifacts, replay, the taxonomy, escalation --
is written against `Surface` and knows nothing about X11, browsers or DOMs.
That is the whole answer to §3.7: porting to a legacy web app in a frameset, or
to a native desktop client over the accessibility API, is a new implementation
of this protocol and nothing else. The recorded flow does not change, because a
flow is expressed in labels and intents rather than in selectors.

The method set is deliberately the *human* action vocabulary -- look, point,
click, type, press, scroll, wait -- rather than anything web-shaped. There is no
`query_selector` here, and its absence is the design. A surface that could offer
one would tempt every layer above into depending on it, and then the desktop
implementation becomes impossible rather than merely unwritten.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from cua.artifact.schema import Point

MouseButton = Literal["left", "right", "middle"]
ScrollDirection = Literal["up", "down", "left", "right"]


@dataclass(frozen=True)
class Frame:
    """One observation: what the screen looked like at a moment."""

    png: bytes
    width: int
    height: int
    taken_at: float = field(default_factory=time.time)

    def __repr__(self) -> str:  # keep megabytes of PNG out of tracebacks
        return f"Frame({self.width}x{self.height}, {len(self.png)} bytes)"


@runtime_checkable
class Surface(Protocol):
    """A thing that can be looked at and operated.

    Implementations must be honest about one thing above all: `observe` returns
    what is on screen *now*, with no waiting and no cleverness. Every wait in
    this system is expressed as a checkpoint on content, because "the page
    loaded" is a lie on any app that fills itself in afterwards -- and the
    target deliberately does exactly that.
    """

    def observe(self) -> Frame: ...

    def navigate(self, url: str) -> None: ...

    def click(
        self, point: Point, button: MouseButton = "left", modifiers: str | None = None
    ) -> None: ...

    def double_click(self, point: Point, modifiers: str | None = None) -> None: ...

    def triple_click(self, point: Point, modifiers: str | None = None) -> None: ...

    def move(self, point: Point) -> None: ...

    def drag(self, start: Point, end: Point, modifiers: str | None = None) -> None: ...

    def mouse_down(self) -> None: ...

    def mouse_up(self) -> None: ...

    def cursor_position(self) -> Point: ...

    def type_text(self, text: str) -> None: ...

    def key(self, keys: str, repeat: int = 1) -> None: ...

    def hold_key(self, keys: str, duration: float) -> None: ...

    def scroll(
        self,
        point: Point | None,
        direction: ScrollDirection,
        amount: int = 3,
        modifiers: str | None = None,
    ) -> None: ...

    def wait(self, seconds: float) -> None: ...


class SurfaceError(RuntimeError):
    """The surface itself failed -- display gone, input tool missing.

    Distinct from every condition in the taxonomy: those are things the *app*
    did, this is the automation losing its hands. It is always a hard failure.
    """


__all__ = ["Frame", "Surface", "SurfaceError", "MouseButton", "ScrollDirection"]
