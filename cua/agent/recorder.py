"""Turning a successful discovery run into a capability artifact.

This is the hinge of the whole system. The model worked in coordinates, because
that is all a screenshot offers. If we wrote those coordinates down we would
have a macro that breaks the first time a row is added above the field -- and
on this target, it would already be broken across the second tenant, whose form
orders the same four fields differently.

So the recorder does the conversion the model could not: for each click it goes
back to the frame the model was looking at, finds the text nearest the click,
and records the *relationship* instead of the position. "The box to the right of
'Nickname'" is a claim that survives reflow, reordering and rebranding;
"(512, 300)" is not.

Two things make this trustworthy rather than hopeful:

- Every derived target is immediately re-resolved against the very frame it was
  derived from. If it does not land back within tolerance of the original
  click, the step is flagged `low_confidence` -- the artifact says so, and a
  reviewer sees it before approving.
- Nothing is emitted as APPROVED. A model proposing a flow and a human
  accepting it are different events, and unattended replay of an irreversible
  step requires the second one.
"""

from __future__ import annotations

import io
import time

from PIL import Image

from cua.agent.loop import Decision, DiscoveryResult
from cua.artifact.schema import (
    Action, ActionKind, AppRef, Approval, Capability, Checkpoint, Output, Param,
    Point, Provenance, Relation, Risk, Step, Target, TemplateAnchor, TextAnchor,
)
from cua.perception import anchor as anchor_mod
from cua.perception import ocr, template
from cua.surface.base import Frame

# How close a re-resolved target must land to the original click to count.
VALIDATION_TOLERANCE_PX = 24
# Half-size of the template patch cropped around a click.
PATCH_W, PATCH_H = 60, 16

# How far away a word may be and still plausibly be this control's label.
# Without these, derivation happily anchors a field to a stray character 680px
# up in the masthead: the arithmetic validates (the relation reproduces the
# click), but the claim is nonsense. Self-validation checks consistency, not
# meaning, so meaning has to be constrained here.
MAX_GAP_RIGHT_PX = 260     # label in the cell to the left, maybe two cells
MAX_GAP_BELOW_PX = 90      # label stacked directly above its control
# OCR litters sparse pages with one-character artifacts at low confidence.
# A single glyph is never a label worth anchoring to.
MIN_ANCHOR_CHARS = 2


def _usable_anchor(block: ocr.TextBlock) -> bool:
    return len(ocr.normalize(block.text)) >= MIN_ANCHOR_CHARS


def derive_target(frame: Frame, point: Point) -> Target:
    """Describe how to find the control the model clicked at `point`.

    Preference order mirrors how a person would describe it: if the click
    landed on words, name those words; otherwise name the label beside it.
    """
    screen = ocr.read(frame.png)
    label: TextAnchor | None = None

    # 1. Did the click land on text? Then the text IS the control -- a button
    #    or a link. This is the most robust anchor available.
    for block in screen.blocks:
        b = block.box
        if b.x <= point.x <= b.right and b.y <= point.y <= b.bottom:
            if not _usable_anchor(block):
                continue
            label = TextAnchor(text=block.text, relation=Relation.ON,
                               occurrence=_occurrence(screen, block))
            break

    # 2. Otherwise look left along the same line. A two-column legacy form puts
    #    the label in the cell before its input, which is why this beats
    #    looking upward on this class of application.
    if label is None:
        best = None
        for block in screen.blocks:
            b = block.box
            same_line = b.y - 6 <= point.y <= b.bottom + 6
            to_the_left = b.right <= point.x
            gap = point.x - b.right
            if same_line and to_the_left and gap <= MAX_GAP_RIGHT_PX \
                    and _usable_anchor(block):
                if best is None or gap < best[0]:
                    best = (gap, block)
        if best is not None:
            gap, block = best
            label = TextAnchor(text=block.text, relation=Relation.RIGHT_OF,
                               occurrence=_occurrence(screen, block),
                               offset_px=max(8, gap))

    # 3. Failing that, look directly above -- stacked forms do exist.
    if label is None:
        best = None
        for block in screen.blocks:
            b = block.box
            overlaps_x = b.x - 40 <= point.x <= b.right + 40
            above = b.bottom <= point.y
            gap = point.y - b.bottom
            if overlaps_x and above and gap <= MAX_GAP_BELOW_PX \
                    and _usable_anchor(block):
                if best is None or gap < best[0]:
                    best = (gap, block)
        if best is not None:
            gap, block = best
            label = TextAnchor(text=block.text, relation=Relation.BELOW,
                               occurrence=_occurrence(screen, block),
                               offset_px=max(8, gap))

    target = Target(
        label=label,
        template=_crop_patch(frame, point),
        absolute=point,
    )

    # Self-validation: does the anchor we just wrote actually find the thing we
    # just clicked, on the screen we derived it from? If not, say so now.
    if label is not None:
        hit = anchor_mod.resolve_label(label, screen)
        if hit is None:
            target.low_confidence = True
        else:
            found, _ = hit
            drift = abs(found.x - point.x) + abs(found.y - point.y)
            target.low_confidence = drift > VALIDATION_TOLERANCE_PX
    else:
        # No text anywhere near it. Replay will lean on the patch, which is
        # weaker; a human should look at this step.
        target.low_confidence = True

    return target


def _occurrence(screen: ocr.Screen, block: ocr.TextBlock) -> int:
    """Which instance of this text it is, in reading order.

    The account grid repeats 'Balance' once per row, so without this an
    artifact would be quietly ambiguous.
    """
    matches = screen.find_all(block.text)
    for i, candidate in enumerate(matches):
        if candidate.box == block.box:
            return i
    return 0


def _crop_patch(frame: Frame, point: Point) -> TemplateAnchor | None:
    try:
        with Image.open(io.BytesIO(frame.png)) as img:
            x0 = max(0, point.x - PATCH_W // 2)
            y0 = max(0, point.y - PATCH_H // 2)
            x1 = min(img.width, x0 + PATCH_W)
            y1 = min(img.height, y0 + PATCH_H)
            if x1 - x0 < 8 or y1 - y0 < 6:
                return None
            patch = img.crop((x0, y0, x1, y1))
    except OSError:
        return None
    return TemplateAnchor(
        patch_b64=template.encode_patch(patch),
        hotspot=Point(x=point.x - x0, y=point.y - y0),
    )


def _point_of(decision: Decision) -> Point | None:
    coord = decision.args.get("coordinate")
    if isinstance(coord, (list, tuple)) and len(coord) == 2:
        return Point(x=int(coord[0]), y=int(coord[1]))
    return None


def _parameterise(text: str, params: dict[str, str]) -> str:
    """Replace literal values the run used with their parameter names.

    Without this the artifact would hard-code the member number the discovery
    run happened to use, which is the difference between a capability and a
    recording of one specific afternoon.
    """
    for name, value in params.items():
        if value and value == text:
            return f"{{{name}}}"
    for name, value in params.items():
        if value and value in text:
            text = text.replace(value, f"{{{name}}}")
    return text


CLICKS = {"left_click", "double_click", "triple_click"}


def record(
    result: DiscoveryResult,
    *,
    cap_id: str,
    title: str,
    description: str,
    app: AppRef,
    checkpoint: Checkpoint,
    params: list[Param] | None = None,
    outputs: list[Output] | None = None,
    param_values: dict[str, str] | None = None,
    model: str | None = None,
    run_id: str | None = None,
    irreversible_from: int | None = None,
) -> Capability:
    """Build a Capability from a discovery trajectory."""
    params = params or []
    outputs = outputs or []
    values = param_values or {}

    steps: list[Step] = []
    pending_target: Target | None = None
    pending_frame: Frame | None = None

    for decision in result.trajectory:
        name = decision.action

        if name in CLICKS:
            point = _point_of(decision)
            if point is None:
                continue
            target = derive_target(decision.frame_before, point)
            # Hold it: a click followed by typing is one act -- filling a
            # field -- and recording them separately would make replay click,
            # then re-find the same control to type into it.
            pending_target = target
            pending_frame = decision.frame_before
            continue

        if name == "type":
            text = _parameterise(str(decision.args.get("text", "")), values)
            steps.append(Step(
                index=len(steps),
                intent=_describe_type(pending_target, text),
                action=Action(kind=ActionKind.TYPE, value=text),
                target=pending_target,
                risk=Risk.SENSITIVE,
            ))
            pending_target = None
            pending_frame = None
            continue

        if name == "key":
            if pending_target is not None:
                steps.append(_click_step(len(steps), pending_target))
                pending_target = None
            keys = str(decision.args.get("text", "Return"))
            steps.append(Step(
                index=len(steps),
                intent=f"press {keys}",
                action=Action(kind=ActionKind.KEY, value=keys),
                risk=Risk.SENSITIVE if keys == "Return" else Risk.SAFE,
            ))
            continue

        # Anything else that survived into the trajectory (scroll, drag) is
        # recorded as a click on its target if it had one, and otherwise
        # dropped: a recorded flow that needs a drag is usually a recording of
        # incidental fiddling rather than of intent.
        if pending_target is not None:
            steps.append(_click_step(len(steps), pending_target))
            pending_target = None

    if pending_target is not None:
        steps.append(_click_step(len(steps), pending_target))

    if irreversible_from is not None:
        for step in steps[irreversible_from:]:
            step.risk = Risk.IRREVERSIBLE
            step.on_failure = step.on_failure

    # The final checkpoint belongs to the last step too, so a failure is
    # reported against the action that caused it rather than as a vague
    # end-of-run mismatch.
    if steps:
        steps[-1].expect = checkpoint

    return Capability(
        id=cap_id,
        title=title,
        description=description,
        app=app,
        params=params,
        outputs=outputs,
        steps=steps,
        checkpoint=checkpoint,
        # Always DRAFT. A model proposing a flow is not a human approving it.
        approval=Approval.DRAFT,
        provenance=Provenance(
            recorded_by="llm_discovery",
            model=model,
            run_id=run_id,
            recorded_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            # If a refusal fallback served any turn, the run was not produced
            # by the requested model alone and the record must say so. A
            # provenance field that quietly overstates which model authored a
            # capability is worse than no provenance field.
            notes=(
                f"{result.steps} model actions; stop_reason={result.stop_reason!r}"
                + ("; one or more turns served by a refusal fallback model"
                   if result.used_fallback else "")
            ),
        ),
    )


def _click_step(index: int, target: Target) -> Step:
    return Step(
        index=index,
        intent=_describe_click(target),
        action=Action(kind=ActionKind.CLICK),
        target=target,
        risk=Risk.SAFE,
    )


def _describe_click(target: Target) -> str:
    if target.label is not None:
        if target.label.relation is Relation.ON:
            return f"click {target.label.text!r}"
        return f"click the control {target.label.relation.value} {target.label.text!r}"
    return "click a control identified by appearance"


def _describe_type(target: Target | None, text: str) -> str:
    where = ""
    if target is not None and target.label is not None:
        where = f" into the field beside {target.label.text!r}"
    return f"type {text!r}{where}"


def low_confidence_steps(cap: Capability) -> list[Step]:
    """Steps a reviewer should look at before approving.

    Surfaced by the CLI on save, because an artifact whose weak points are
    buried in JSON will be approved without anyone reading them.
    """
    return [s for s in cap.steps if s.target is not None and s.target.low_confidence]


__all__ = ["record", "derive_target", "low_confidence_steps",
           "VALIDATION_TOLERANCE_PX", "MAX_GAP_RIGHT_PX", "MAX_GAP_BELOW_PX"]
