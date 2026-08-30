"""Establishing an authenticated session, deterministically and without a model.

Authentication is a *precondition* of a capability, not part of one. Three
reasons, and only the first is what led me here:

1. An automation agent should not be handling credentials at all. Asking a
   model to type a password into a login form is the shape of a genuine attack,
   and it is right that it looks like one -- our discovery runs were declined
   under the `cyber` policy for exactly that, twice, even with truthful context
   about the target being a local practice fixture. The refusal was pointing at
   a design flaw rather than getting in the way of one.

2. A capability that embeds its own sign-on carries credential handling into
   every artifact, every replay and every log. Hoisting it out means a recorded
   flow contains no secrets at all, which is a much easier property to audit
   than "we redact them carefully".

3. Real institutions do not authenticate this way anyway. Production would put
   SSO, a session broker or an injected cookie here. This module is the seam
   where that swap happens; everything above it starts from "a session exists".

Note the brief's own example goal -- "look up member 12345 and read their
current savings balance" -- likewise begins after sign-on.
"""

from __future__ import annotations

from cua.artifact.schema import Relation, Target, TextAnchor
from cua.perception import anchor as anchor_mod
from cua.perception import ocr
from cua.surface.base import Surface, SurfaceError


class BootstrapError(RuntimeError):
    """The session could not be established. Nothing downstream can run."""


def _click(surface: Surface, text: str, relation: Relation, offset: int = 60):
    frame = surface.observe()
    screen = ocr.read(frame.png)
    target = Target(label=TextAnchor(text=text, relation=relation, offset_px=offset))
    resolution = anchor_mod.resolve(target, screen, frame.png)
    surface.click(resolution.point)
    return resolution


def sign_on(
    surface: Surface,
    entry_url: str,
    operator: str,
    password: str,
    landing: str = "Member Search",
    attempts: int = 3,
) -> None:
    """Sign on and leave the surface on an authenticated page.

    Uses the same label-anchored targeting as replay, so it exercises the real
    perception path rather than a special case -- but it is fixed code, not a
    recorded artifact, because it is infrastructure rather than a capability.
    """
    for attempt in range(1, attempts + 1):
        try:
            surface.navigate(entry_url)
            surface.wait(2.0)

            _click(surface, "Operator ID", Relation.RIGHT_OF)
            surface.key("ctrl+a")
            surface.type_text(operator)

            _click(surface, "Password", Relation.RIGHT_OF)
            surface.key("ctrl+a")
            surface.type_text(password)

            _click(surface, "Sign On", Relation.ON)
            surface.wait(2.0)

            screen = ocr.read(surface.observe().png)
            if screen.contains(landing):
                return
            last = f"signed on but did not reach {landing!r}"
        except (anchor_mod.UnresolvedTarget, SurfaceError) as e:
            last = str(e)
        if attempt < attempts:
            surface.wait(1.5)

    raise BootstrapError(f"could not establish a session after {attempts} attempts: {last}")


__all__ = ["sign_on", "BootstrapError"]
