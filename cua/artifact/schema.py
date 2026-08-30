"""The capability artifact: a typed, versioned, reviewable contract.

This is the centre of the system. Everything else is either producing one of
these (discovery) or consuming one (replay), so the shape here decides what the
rest can do.

Three commitments drive the design:

1. **A capability is an API, not a macro.** A calling agent needs to know what
   it must supply, what it gets back, and how to tell success from a legitimate
   "no such member". So `params`, `outputs` and the outcome contract are typed
   and first-class, not implied by the step list.

2. **Never record pixels.** We perceive the screen as pixels -- no DOM is ever
   read -- but a recorded coordinate is worthless the moment anything reflows.
   Every `Target` therefore carries a resolution *chain* that is re-derived
   against the live screen at replay time. See `Target` for why the order is
   what it is.

3. **The artifact is not the transcript.** `Provenance` records which model
   produced this and which run, but the reasoning is evidence, not contract.
   A reviewer reading this file should not have to read a model transcript to
   understand what the capability does.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# Bumped when THIS file's shape changes in a way old artifacts can't satisfy.
# Distinct from Capability.version, which versions one capability's flow.
SCHEMA_VERSION = "1.0"


class Strict(BaseModel):
    """Reject unknown keys everywhere.

    A typo in a hand-edited artifact must fail loudly at load time rather than
    be silently ignored and change behaviour at replay time.
    """

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- geometry

class Point(Strict):
    x: int
    y: int


class Rect(Strict):
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> Point:
        return Point(x=self.x + self.w // 2, y=self.y + self.h // 2)

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h


# ---------------------------------------------------------------- targeting

class Relation(StrEnum):
    """Where the control sits relative to its text anchor.

    Legacy table forms label to the left of the control (`RIGHT_OF`) far more
    often than above it, because that is what a two-column <table> layout
    produces. ON is for controls whose own text IS the anchor -- buttons and
    links.
    """

    RIGHT_OF = "right_of"
    BELOW = "below"
    LEFT_OF = "left_of"
    ABOVE = "above"
    ON = "on"


class TextAnchor(Strict):
    """Find a control by the words next to it. Tier 1, and the one that matters.

    This is the only tier that survives the drift we actually expect. The two
    mock_teller tenants order their sub-account form fields differently
    (nickname/deposit/purpose/statements vs purpose/nickname/statements/deposit)
    while rendering byte-identical <input> boxes, so:

      - an absolute coordinate lands on the WRONG field, and
      - a template match of the input box cannot tell the fields apart,

    but "the word 'Nickname', then the box to its right" is correct on both.

    `aliases` carries the other axis of tenant drift: the same vendor product
    ships as "Member Number" for one institution and "Customer Number" for the
    next. One artifact, both tenants, no re-recording.
    """

    text: str
    # Other spellings of the SAME control across tenants/versions. Tried in
    # order after `text` misses.
    aliases: list[str] = Field(default_factory=list)
    # OCR merges and splits words unpredictably -- the "Sign On" button comes
    # back from tesseract as "Signon". Comparison therefore normalises
    # whitespace and case by default rather than matching literally.
    match: Literal["normalized", "exact", "contains"] = "normalized"
    relation: Relation = Relation.RIGHT_OF
    # Which hit to take when the label legitimately repeats. The account grid
    # shows "Balance" once per row, so occurrence is how you say "the savings
    # one" without resorting to coordinates.
    occurrence: int = 0
    # How far past the anchor's edge to aim, in px. The default lands inside a
    # typical adjacent-cell input rather than on the 1px border.
    offset_px: int = 30


class TemplateAnchor(Strict):
    """Find a control by what it looks like. Tier 2.

    Earns its place only for controls with no usable nearby text -- icons,
    unlabelled toolbar buttons. It is deliberately NOT the primary tier: every
    text input in this app renders identically, so a patch match is ambiguous
    exactly where tier 1 is precise. It also breaks on rebranding, which is the
    common per-tenant difference.
    """

    # PNG bytes, base64. Small: a patch, not a screenshot.
    patch_b64: str
    # Normalised cross-correlation score below which we refuse the match.
    threshold: float = 0.92
    # Where in the patch the click goes, relative to the patch's top-left.
    hotspot: Point


class Target(Strict):
    """How to find one control, as an ordered chain of independent strategies.

    Replay tries tiers in order and records which one fired. That signal is the
    drift detector: a capability that has started resolving via `absolute`
    instead of `label` still works today but is one layout change from
    breaking, and it should be re-recorded before it does.
    """

    label: TextAnchor | None = None
    template: TemplateAnchor | None = None
    # Last resort, and recorded as such. Present so a step is never
    # unresolvable, never because we trust it.
    absolute: Point | None = None
    # How far a lower tier may land from where the recorder saw the control
    # before we call it a different control.
    tolerance_px: int = 12
    # Set by the recorder when tier 1 could not be validated against the frame
    # it was recorded from. A human should look at these before approval.
    low_confidence: bool = False


class ResolutionTier(StrEnum):
    LABEL = "label"
    TEMPLATE = "template"
    ABSOLUTE = "absolute"


# ---------------------------------------------------------------- actions

class ActionKind(StrEnum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    KEY = "key"
    WAIT_FOR = "wait_for"
    EXTRACT = "extract"
    ACCEPT_DIALOG = "accept_dialog"


class Action(Strict):
    """What to do. Deliberately small.

    Kept far narrower than the 17-member computer toolset the model gets during
    discovery: a recorded flow that needs `hold_key` or `left_mouse_down` is
    telling you the recording captured incidental fiddling rather than intent.
    Narrowing here is what makes replay reviewable.
    """

    kind: ActionKind
    # NAVIGATE: the URL. TYPE: the literal or "{param}" template. KEY: key name.
    value: str | None = None


# ---------------------------------------------------------------- checkpoints

class Checkpoint(Strict):
    """An assertion that we actually reached the state we expected.

    Text-based, because OCR is the only sense we have. `absent` matters as much
    as `present`: "the spinner is gone" is how you wait for content rather than
    for page load, and this app renders a spinner whose data genuinely is not
    in the document yet.
    """

    kind: Literal["text_present", "text_absent", "text_matches"]
    text: str | None = None
    # Regex, for text_matches. Used by outputs that must look like money.
    pattern: str | None = None
    # Restrict OCR to a region. Anchored regions are preferred; a raw rect is
    # a coordinate by another name.
    within: Rect | None = None
    timeout_ms: int = 10_000
    description: str | None = None


# ---------------------------------------------------------------- io contract

class ParamType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    MONEY = "money"
    DATE = "date"


class Param(Strict):
    """A typed input the calling agent supplies per invocation."""

    name: str
    type: ParamType = ParamType.STRING
    required: bool = True
    description: str | None = None
    # Validated before a single action is taken. Cheap way to turn a malformed
    # call into a clear error instead of a confusing mid-flow failure.
    pattern: str | None = None
    example: str | None = None
    # Inputs that must never reach a log or an artifact. See safety.redaction.
    sensitive: bool = False


class Extraction(Strict):
    """Where an output value is on screen, expressed relative to an anchor.

    Never an absolute rect: the whole point of the targeting design is that
    positions move. "The text to the right of the word 'Savings'" survives the
    row order changing; a rect does not.
    """

    anchor: TextAnchor
    # How far to read past the anchor, in px, along the relation direction.
    span_px: int = 260
    # Optional regex applied to the OCR'd region; group 1 wins if present.
    # Also the last line of defence against OCR noise bleeding into an output.
    pattern: str | None = None


class Output(Strict):
    """A typed value the capability returns to its caller."""

    name: str
    type: ParamType = ParamType.STRING
    extract: Extraction
    description: str | None = None
    # Redacted in evidence and logs, still returned to the caller in-process.
    sensitive: bool = False


# ---------------------------------------------------------------- steps

class Risk(StrEnum):
    """How much damage this step can do if replay is wrong about the state.

    The distinction the brief asks for. SAFE is reversible navigation and
    reading. SENSITIVE mutates a draft but nothing durable. IRREVERSIBLE
    creates or destroys a real record and cannot be undone from the UI, so it
    is gated on explicit approval rather than on the agent's confidence.
    """

    SAFE = "safe"
    SENSITIVE = "sensitive"
    IRREVERSIBLE = "irreversible"


class OnFailure(StrEnum):
    RETRY = "retry"
    ESCALATE = "escalate"
    ABORT = "abort"


class Step(Strict):
    index: int
    # Why this step exists, in a reviewer's words. Not the model's reasoning.
    intent: str
    action: Action
    target: Target | None = None
    # Postcondition. A step without one is a step that assumes its click worked.
    expect: Checkpoint | None = None
    risk: Risk = Risk.SAFE
    on_failure: OnFailure = OnFailure.ABORT
    max_retries: int = 2


# ---------------------------------------------------------------- capability

class AppRef(Strict):
    """Which surface this was recorded against.

    Product plus version plus tenant variant, because "works on CoreTeller
    7.2.1 as configured for NorthStar" is a different claim from "works on
    CoreTeller". Replaying against a different variant is allowed -- that is
    the cross-tenant reuse story -- but the result should say it happened.
    """

    product: str
    version: str | None = None
    tenant_variant: str | None = None
    entry_url: str


class Provenance(Strict):
    """How this artifact came to exist. Evidence pointer, not the evidence."""

    recorded_by: Literal["llm_discovery", "human"] = "llm_discovery"
    model: str | None = None
    run_id: str | None = None
    recorded_at: str | None = None
    # Deliberately NOT the transcript. The artifact is decoupled from the raw
    # model output; the run log in evidence/ holds the reasoning.
    notes: str | None = None


class Approval(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"


class Capability(Strict):
    """A reusable, agent-invocable capability."""

    schema_version: str = SCHEMA_VERSION
    # Dotted and stable: what a calling agent invokes by name.
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")]
    version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")] = "1.0.0"
    title: str
    description: str

    app: AppRef
    params: list[Param] = Field(default_factory=list)
    outputs: list[Output] = Field(default_factory=list)
    steps: list[Step]
    # The overall success condition, checked after the last step. Per-step
    # `expect` catches a wrong turn early; this one decides the run.
    checkpoint: Checkpoint

    # Unattended replay of an IRREVERSIBLE step requires APPROVED. A freshly
    # discovered capability is always DRAFT: the model proposing a flow is not
    # the same thing as a human accepting it.
    approval: Approval = Approval.DRAFT
    provenance: Provenance = Field(default_factory=Provenance)

    @property
    def ref(self) -> str:
        """`id@version` -- how a caller names one exact capability."""
        return f"{self.id}@{self.version}"

    @property
    def is_irreversible(self) -> bool:
        return any(s.risk is Risk.IRREVERSIBLE for s in self.steps)

    def param(self, name: str) -> Param | None:
        return next((p for p in self.params if p.name == name), None)


__all__ = [
    "SCHEMA_VERSION", "Point", "Rect", "Relation", "TextAnchor",
    "TemplateAnchor", "Target", "ResolutionTier", "ActionKind", "Action",
    "Checkpoint", "ParamType", "Param", "Extraction", "Output", "Risk",
    "OnFailure", "Step", "AppRef", "Provenance", "Approval", "Capability",
]
