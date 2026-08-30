"""Turning a recorded `Target` back into a live coordinate.

This function is where the system's central claim is either true or false. We
perceive only pixels, yet replay has to be deterministic and stable -- so the
artifact stores *how to find* a control rather than *where it was*, and this is
the code that does the finding.

The tiers run in order and the winner is reported, never hidden. A capability
that has quietly started resolving through `absolute` still passes its tests
today, but it has lost its robustness and is one reflow from breaking. Surfacing
the tier is what turns that from an invisible decay into a drift signal a human
can act on.
"""

from __future__ import annotations

from dataclasses import dataclass

from cua.artifact.schema import (
    Point, Relation, ResolutionTier, Target, TextAnchor,
)
from cua.perception import template
from cua.perception.ocr import Screen, TextBlock


@dataclass(frozen=True)
class Resolution:
    point: Point
    tier: ResolutionTier
    # What actually matched, for the run log: which alias, what score.
    detail: str
    score: float = 1.0

    @property
    def is_degraded(self) -> bool:
        """True when we fell back past the tier we trust.

        Callers log this as drift. It is not an error -- the step will proceed
        -- but it is the early warning that this artifact needs re-recording.
        """
        return self.tier is not ResolutionTier.LABEL


class UnresolvedTarget(Exception):
    """No tier could locate the control. The step cannot proceed."""

    def __init__(self, target: Target, tried: list[str]) -> None:
        self.target = target
        self.tried = tried
        super().__init__("could not locate control; tried: " + "; ".join(tried))


def point_for(block: TextBlock, relation: Relation, offset_px: int) -> Point:
    """Where to click, given the anchor text's box and which way to go.

    `RIGHT_OF` is the default elsewhere in the schema because a two-column
    legacy <table> form puts the label in the cell to the left of its control
    far more often than above it.
    """
    box = block.box
    match relation:
        case Relation.ON:
            return box.center
        case Relation.RIGHT_OF:
            return Point(x=box.right + offset_px, y=box.center.y)
        case Relation.LEFT_OF:
            return Point(x=max(0, box.x - offset_px), y=box.center.y)
        case Relation.BELOW:
            return Point(x=box.center.x, y=box.bottom + offset_px)
        case Relation.ABOVE:
            return Point(x=box.center.x, y=max(0, box.y - offset_px))
    raise ValueError(f"unhandled relation {relation!r}")


def resolve_label(anchor: TextAnchor, screen: Screen) -> tuple[Point, str] | None:
    """Tier 1. Try the recorded text, then each alias in turn.

    Aliases are what let one artifact serve several tenants running the same
    vendor product: the institution that calls it "Member Number" and the one
    that calls it "Customer Number" are the same control, and re-recording per
    tenant is exactly the cost this design exists to avoid.
    """
    for candidate in (anchor.text, *anchor.aliases):
        block = screen.find(candidate, anchor.match, anchor.occurrence)
        if block is not None:
            point = point_for(block, anchor.relation, anchor.offset_px)
            via = "text" if candidate == anchor.text else f"alias {candidate!r}"
            return point, f"{via} -> {block.text!r} @ {block.box.x},{block.box.y}"
    return None


def resolve(target: Target, screen: Screen, frame_png: bytes) -> Resolution:
    """Locate the control described by `target` on the current frame."""
    tried: list[str] = []

    if target.label is not None:
        hit = resolve_label(target.label, screen)
        if hit is not None:
            point, detail = hit
            return Resolution(point=point, tier=ResolutionTier.LABEL, detail=detail)
        tried.append(
            f"label {target.label.text!r}"
            + (f" (+{len(target.label.aliases)} aliases)" if target.label.aliases else "")
        )

    if target.template is not None:
        match = template.find(frame_png, target.template, near=target.absolute)
        if match is not None:
            return Resolution(
                point=match.point,
                tier=ResolutionTier.TEMPLATE,
                detail=f"patch matched at {match.score:.3f}",
                score=match.score,
            )
        tried.append(f"template (threshold {target.template.threshold})")

    if target.absolute is not None:
        # Reached only when both robust tiers missed. The step will probably
        # work and might click the wrong thing; either way the caller is told,
        # via is_degraded, that this artifact is no longer trustworthy.
        return Resolution(
            point=target.absolute,
            tier=ResolutionTier.ABSOLUTE,
            detail="fell back to recorded coordinates",
            score=0.0,
        )
    tried.append("no absolute fallback recorded")

    raise UnresolvedTarget(target, tried)


__all__ = ["Resolution", "UnresolvedTarget", "resolve", "resolve_label", "point_for"]
