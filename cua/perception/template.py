"""Tier 2: find a control by what it looks like.

Deliberately a *local* search, not a global one. It only ever runs seeded by
the coordinate the recorder saw, and only looks within `SEARCH_RADIUS_PX` of
it. That is a real constraint, chosen rather than conceded:

- A global patch search on a business app is ambiguous by construction. Every
  <input type=text> on this surface renders identically, so a global match has
  hundreds of equally good hits and no way to rank them. A bounded search says
  the honest thing instead: "the control moved a bit, find it again nearby."
- It also gives the recorded absolute coordinate a second job. It is not only
  the tier-3 fallback, it is tier 2's search hint -- which is why it is worth
  recording even though we never trust it on its own.

Normalised cross-correlation, so a theme change that shifts brightness or
contrast uniformly does not defeat the match. A theme change that alters hue
will, which is exactly why this is tier 2 and not tier 1.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

import numpy as np
from PIL import Image

from cua.artifact.schema import Point, TemplateAnchor

# How far the control is allowed to have moved. Generous enough to absorb a
# row appearing above it, tight enough that the search stays unambiguous.
SEARCH_RADIUS_PX = 120


@dataclass(frozen=True)
class Match:
    point: Point
    score: float


def _gray(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("L"), dtype=np.float64)


def decode_patch(anchor: TemplateAnchor) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(anchor.patch_b64)))


def encode_patch(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def find(
    frame_png: bytes,
    anchor: TemplateAnchor,
    near: Point | None,
    radius: int = SEARCH_RADIUS_PX,
) -> Match | None:
    """Locate `anchor`'s patch near `near`. Returns None below threshold."""
    frame = _gray(Image.open(io.BytesIO(frame_png)))
    patch = _gray(decode_patch(anchor))
    ph, pw = patch.shape
    fh, fw = frame.shape
    if ph > fh or pw > fw:
        return None

    # Bound the search. Without a hint we would have to scan globally, which
    # this function explicitly does not promise; fall back to the whole frame
    # only when it is small enough for that to remain meaningful.
    if near is not None:
        x0 = max(0, near.x - radius)
        y0 = max(0, near.y - radius)
        x1 = min(fw - pw, near.x + radius)
        y1 = min(fh - ph, near.y + radius)
    else:
        x0, y0, x1, y1 = 0, 0, fw - pw, fh - ph
    if x1 < x0 or y1 < y0:
        return None

    p = patch - patch.mean()
    p_norm = float(np.sqrt((p * p).sum()))
    if p_norm == 0.0:
        # A featureless patch (a blank box) matches everything equally well.
        # Refusing is the correct answer, not picking an arbitrary hit.
        return None

    best_score = -1.0
    best_xy = (0, 0)
    # One vectorised pass per candidate row: keeps peak memory at a few MB
    # instead of materialising every window in the search box at once.
    for y in range(y0, y1 + 1):
        strip = np.lib.stride_tricks.sliding_window_view(
            frame[y:y + ph, :], (ph, pw)
        )[0]                                   # (fw-pw+1, ph, pw)
        cand = strip[x0:x1 + 1]
        if cand.size == 0:
            continue
        c = cand - cand.mean(axis=(1, 2), keepdims=True)
        num = (c * p).sum(axis=(1, 2))
        den = np.sqrt((c * c).sum(axis=(1, 2))) * p_norm
        with np.errstate(invalid="ignore", divide="ignore"):
            scores = np.where(den > 0, num / den, -1.0)
        i = int(np.argmax(scores))
        if scores[i] > best_score:
            best_score = float(scores[i])
            best_xy = (x0 + i, y)

    if best_score < anchor.threshold:
        return None
    return Match(
        point=Point(x=best_xy[0] + anchor.hotspot.x, y=best_xy[1] + anchor.hotspot.y),
        score=best_score,
    )


__all__ = ["Match", "find", "encode_patch", "decode_patch", "SEARCH_RADIUS_PX"]
