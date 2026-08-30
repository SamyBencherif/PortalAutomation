"""Perception tests, run against a REAL screenshot of the real target.

The fixture is an actual 1280x800 render of mock_teller's sign-on screen, not a
synthetic image with clean fonts. That matters: the whole tier-1 strategy rests
on tesseract coping with 11px Verdana on a #d4d0c8 ground, and a test drawn
with PIL in Arial 24 would prove nothing about that.

These are the tests that would catch the perception layer silently rotting --
if OCR quality regresses, anchoring degrades to coordinates and every replay
becomes brittle without any test failing. So they assert on tiers, not just on
success.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cua.artifact.schema import (
    Point, Relation, ResolutionTier, Target, TemplateAnchor, TextAnchor,
)
from cua.perception import anchor, ocr, template

FIXTURE = Path(__file__).parent / "fixtures" / "login_northstar.png"


@pytest.fixture(scope="module")
def frame() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture(scope="module")
def screen(frame: bytes) -> ocr.Screen:
    return ocr.read(frame)


# ------------------------------------------------------------------- ocr

def test_the_anchor_labels_are_actually_readable(screen: ocr.Screen):
    """The premise of the whole targeting design.

    If this fails, tier 1 is dead and every capability degrades to coordinates.
    """
    for label in ("Operator ID", "Password", "Sign On"):
        assert screen.find(label) is not None, f"{label!r} not recovered from the frame"


def test_multi_word_labels_are_reassembled(screen: ocr.Screen):
    """'Operator ID' reaches us as two words that must be rejoined.

    tesseract returns "Operator" and "ID" as separate rows sharing a block;
    grouping by block is what makes the label findable at all.
    """
    block = screen.find("Operator ID")
    assert block is not None
    assert len(block.words) == 2
    assert block.text == "Operator ID"


def test_normalization_survives_ocr_word_splitting(screen: ocr.Screen):
    """The submit button reads back as "Signon" -- one token, no space.

    Any comparison that respects whitespace fails here intermittently, which is
    why normalisation is the default rather than an opt-in.
    """
    raw = [b.text for b in screen.blocks]
    assert "Signon" in raw, f"expected the merged token in {raw}"
    # ...and it is still findable by its real name.
    assert screen.find("Sign On") is not None


def test_whole_block_matching_avoids_the_header_trap(screen: ocr.Screen):
    """The trap that motivated the strict default.

    The panel header "Operator Sign On" and the submit button "Sign On" both
    CONTAIN "Sign On". A substring default silently targets the header, and the
    run fails somewhere later with no clue why. Whole-block equality picks the
    button.
    """
    contains_hits = screen.find_all("Sign On", match="contains")
    assert len(contains_hits) >= 2, "expected header and button to both contain it"

    exact = screen.find("Sign On")           # default: normalized whole-block
    assert exact is not None
    assert exact.norm == "signon"
    # The header must NOT be what we picked.
    assert "operator" not in exact.norm


def test_low_confidence_noise_is_dropped(frame: bytes):
    strict = ocr.read(frame, min_conf=95.0)
    loose = ocr.read(frame, min_conf=0.0)
    assert len(strict.blocks) < len(loose.blocks)


def test_normalize_collapses_spacing_case_and_punctuation():
    assert ocr.normalize("Sign On") == ocr.normalize("signon") == "signon"
    assert ocr.normalize("Password:") == "password"


# -------------------------------------------------------------- anchoring

def test_right_of_lands_to_the_right_of_the_label(screen: ocr.Screen):
    """A legacy two-column form labels to the LEFT of its input."""
    block = screen.find("Operator ID")
    assert block is not None
    point = anchor.point_for(block, Relation.RIGHT_OF, offset_px=30)
    assert point.x > block.box.right
    # Vertically centred on the label, so it hits the input's row.
    assert block.box.y <= point.y <= block.box.bottom


def test_relations_point_the_way_they_say(screen: ocr.Screen):
    block = screen.find("Password")
    assert block is not None
    box = block.box
    assert anchor.point_for(block, Relation.ON, 30) == box.center
    assert anchor.point_for(block, Relation.BELOW, 10).y > box.bottom
    assert anchor.point_for(block, Relation.ABOVE, 10).y < box.y
    assert anchor.point_for(block, Relation.LEFT_OF, 10).x < box.x


def test_tier1_resolves_and_reports_itself_as_healthy(screen, frame):
    target = Target(label=TextAnchor(text="Operator ID", relation=Relation.RIGHT_OF))
    res = anchor.resolve(target, screen, frame)
    assert res.tier is ResolutionTier.LABEL
    assert not res.is_degraded


def test_an_alias_resolves_when_the_primary_text_is_absent(screen, frame):
    """The cross-tenant mechanism, in miniature.

    One artifact must serve the institution that says "Operator ID" and the one
    that says something else, without being re-recorded.
    """
    target = Target(
        label=TextAnchor(text="Benutzerkennung", aliases=["Operator ID"])
    )
    res = anchor.resolve(target, screen, frame)
    assert res.tier is ResolutionTier.LABEL
    assert "alias" in res.detail


def test_absolute_is_used_but_flagged_as_degraded(screen, frame):
    """Falling back must still work -- and must still be visible."""
    target = Target(
        label=TextAnchor(text="No Such Label Anywhere"),
        absolute=Point(x=500, y=142),
    )
    res = anchor.resolve(target, screen, frame)
    assert res.tier is ResolutionTier.ABSOLUTE
    assert res.is_degraded, "a coordinate fallback must surface as drift"


def test_an_unresolvable_target_raises_with_what_it_tried(screen, frame):
    target = Target(label=TextAnchor(text="Nothing Matches This"))
    with pytest.raises(anchor.UnresolvedTarget) as e:
        anchor.resolve(target, screen, frame)
    assert "Nothing Matches This" in str(e.value)


# --------------------------------------------------------------- template

def test_template_finds_a_patch_cut_from_the_frame_itself(frame: bytes):
    """Tier 2 against ground truth: crop a region, then find it again."""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(frame))
    crop_box = (440, 135, 520, 160)          # around the "Operator ID" label
    patch = img.crop(crop_box)

    anchor_spec = TemplateAnchor(
        patch_b64=template.encode_patch(patch),
        hotspot=Point(x=5, y=5),
        threshold=0.9,
    )
    match = template.find(frame, anchor_spec, near=Point(x=445, y=140))
    assert match is not None
    assert match.score > 0.99
    assert abs(match.point.x - (crop_box[0] + 5)) <= 2
    assert abs(match.point.y - (crop_box[1] + 5)) <= 2


def test_template_search_is_bounded_by_the_hint(frame: bytes):
    """A patch that exists, but far from the hint, must NOT be found.

    Tier 2 promises a local refinement, not a global search. Keeping that
    promise honest is what stops it from silently matching one of the many
    identical-looking inputs elsewhere on the screen.
    """
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(frame))
    patch = img.crop((440, 135, 520, 160))
    spec = TemplateAnchor(
        patch_b64=template.encode_patch(patch), hotspot=Point(x=5, y=5), threshold=0.9
    )
    far_away = Point(x=100, y=700)
    assert template.find(frame, spec, near=far_away, radius=60) is None


def test_a_featureless_patch_is_refused_rather_than_guessed(frame: bytes):
    """A blank box correlates equally well with every other blank box.

    Returning None is the correct answer; returning "the first one" would be a
    confidently wrong click.
    """
    from PIL import Image

    blank = Image.new("RGB", (24, 12), (255, 255, 255))
    spec = TemplateAnchor(
        patch_b64=template.encode_patch(blank), hotspot=Point(x=2, y=2), threshold=0.9
    )
    assert template.find(frame, spec, near=Point(x=600, y=145)) is None
