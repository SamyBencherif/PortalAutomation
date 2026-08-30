"""Recording: coordinates in, stable targets out.

Run against the real sign-on screenshot, because the claim being tested is
specifically that this works on a real legacy screen with 11px text and no
test ids. A synthetic image would prove the arithmetic and nothing else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cua.agent.loop import Decision, DiscoveryResult
from cua.agent.recorder import derive_target, low_confidence_steps, record
from cua.artifact.schema import (
    ActionKind, AppRef, Approval, Checkpoint, Param, Point, Relation, Risk,
)
from cua.perception import ocr
from cua.surface.base import Frame

FIXTURE = Path(__file__).parent / "fixtures" / "login_northstar.png"


@pytest.fixture(scope="module")
def frame() -> Frame:
    png = FIXTURE.read_bytes()
    return Frame(png=png, width=1280, height=800)


@pytest.fixture(scope="module")
def screen(frame: Frame) -> ocr.Screen:
    return ocr.read(frame.png)


def _box(screen: ocr.Screen, label: str):
    block = screen.find(label)
    assert block is not None, f"fixture no longer contains {label!r}"
    return block.box


# ------------------------------------------------------------- derivation

def test_a_click_in_a_field_records_the_label_beside_it(frame, screen):
    """The core conversion: a coordinate becomes 'right of Operator ID'."""
    box = _box(screen, "Operator ID")
    click = Point(x=box.right + 40, y=box.center.y)

    target = derive_target(frame, click)

    assert target.label is not None
    assert target.label.text == "Operator ID"
    assert target.label.relation is Relation.RIGHT_OF
    assert not target.low_confidence, "should have validated against its own frame"


def test_a_click_on_a_button_records_the_button_text(frame, screen):
    """Clicking words means the words are the anchor -- the strongest kind."""
    box = _box(screen, "Sign On")
    target = derive_target(frame, box.center)

    assert target.label is not None
    assert target.label.relation is Relation.ON
    assert ocr.normalize(target.label.text) == "signon"
    assert not target.low_confidence


def test_the_recorded_coordinate_is_kept_but_is_not_the_primary(frame, screen):
    """Tier 3 exists as a fallback AND as tier 2's search hint."""
    box = _box(screen, "Password")
    click = Point(x=box.right + 40, y=box.center.y)
    target = derive_target(frame, click)

    assert target.absolute == click
    assert target.template is not None
    assert target.label is not None, "the label must still be the primary tier"


def test_a_click_in_empty_space_is_flagged_low_confidence(frame):
    """No text nearby means no robust anchor. Say so rather than pretend."""
    target = derive_target(frame, Point(x=1200, y=700))
    assert target.low_confidence, "an unanchorable click must be flagged for review"


def test_derivation_validates_against_the_frame_it_came_from(frame, screen):
    """The property that makes the artifact trustworthy.

    Every anchor is re-resolved immediately; one that cannot find its own
    control on its own screenshot is marked rather than shipped.
    """
    box = _box(screen, "Operator ID")
    target = derive_target(frame, Point(x=box.right + 40, y=box.center.y))

    from cua.perception.anchor import resolve_label
    hit = resolve_label(target.label, screen)
    assert hit is not None
    found, _ = hit
    assert abs(found.y - target.absolute.y) <= 24


# ---------------------------------------------------------------- assembly

def _trajectory(frame: Frame, screen: ocr.Screen) -> list[Decision]:
    op = _box(screen, "Operator ID")
    pw = _box(screen, "Password")
    return [
        Decision(action="left_click", args={"coordinate": [op.right + 40, op.center.y]},
                 frame_before=frame, step=1),
        Decision(action="type", args={"text": "teller1"}, frame_before=frame, step=2),
        Decision(action="left_click", args={"coordinate": [pw.right + 40, pw.center.y]},
                 frame_before=frame, step=3),
        Decision(action="type", args={"text": "hunter2"}, frame_before=frame, step=4),
        Decision(action="key", args={"text": "Return"}, frame_before=frame, step=5),
    ]


def _result(frame, screen) -> DiscoveryResult:
    return DiscoveryResult(goal="sign on", status="succeeded",
                           trajectory=_trajectory(frame, screen), steps=5,
                           stop_reason="end_turn")


def _record(frame, screen, **kw):
    return record(
        _result(frame, screen),
        cap_id="teller.sign_on",
        title="Sign on",
        description="Authenticate an operator.",
        app=AppRef(product="coreteller", entry_url="http://target:8800/login"),
        checkpoint=Checkpoint(kind="text_present", text="Member Search"),
        model="claude-opus-5",
        run_id="discovery-test",
        **kw,
    )


def test_a_click_then_type_becomes_one_step(frame, screen):
    """Filling a field is one act.

    Recording the click and the typing separately would make replay locate the
    control, then locate it again to type into it -- two chances to fail for
    one intention.
    """
    cap = _record(frame, screen)
    kinds = [s.action.kind for s in cap.steps]
    assert kinds == [ActionKind.TYPE, ActionKind.TYPE, ActionKind.KEY]
    assert cap.steps[0].target is not None
    assert cap.steps[0].target.label.text == "Operator ID"


def test_literal_values_are_replaced_by_parameter_names(frame, screen):
    """Otherwise the artifact hard-codes the discovery run's own inputs."""
    cap = _record(frame, screen,
                  params=[Param(name="operator")],
                  param_values={"operator": "teller1"})
    assert cap.steps[0].action.value == "{operator}"
    # The password was not declared as a parameter, so it stays literal --
    # which is exactly why the CLI declares it as one in practice.
    assert cap.steps[1].action.value == "hunter2"


def test_the_checkpoint_lands_on_the_final_step(frame, screen):
    cap = _record(frame, screen)
    assert cap.steps[-1].expect is not None
    assert cap.steps[-1].expect.text == "Member Search"
    assert cap.checkpoint.text == "Member Search"


def test_a_recorded_capability_is_always_a_draft(frame, screen):
    """A model proposing a flow is not a human approving it."""
    cap = _record(frame, screen)
    assert cap.approval is Approval.DRAFT
    assert not cap.is_irreversible


def test_irreversible_steps_can_be_marked_at_record_time(frame, screen):
    cap = _record(frame, screen, irreversible_from=2)
    assert cap.is_irreversible
    assert cap.steps[2].risk is Risk.IRREVERSIBLE
    assert cap.steps[0].risk is not Risk.IRREVERSIBLE


def test_provenance_points_at_the_run_without_embedding_it(frame, screen):
    """The artifact is decoupled from the transcript, by design."""
    cap = _record(frame, screen)
    assert cap.provenance.model == "claude-opus-5"
    assert cap.provenance.run_id == "discovery-test"
    dumped = cap.model_dump_json()
    assert "Goal:" not in dumped and "system" not in dumped.lower()[:200]


def test_weak_steps_are_surfaced_for_review(frame, screen):
    cap = _record(frame, screen)
    weak = low_confidence_steps(cap)
    assert all(s.target.low_confidence for s in weak)


def test_a_recorded_capability_round_trips_through_the_store(frame, screen, tmp_path):
    from cua.artifact.store import CapabilityExists, Store

    store = Store(tmp_path)
    cap = _record(frame, screen)
    store.save(cap)

    loaded = store.load(cap.ref)
    assert loaded.model_dump() == cap.model_dump()
    assert store.load("teller.sign_on").version == cap.version

    with pytest.raises(CapabilityExists):
        store.save(cap)
