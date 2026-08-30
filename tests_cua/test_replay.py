"""Replay engine tests against a scripted fake surface.

The fake renders real PNGs and the engine reads them with real OCR, so these
exercise the actual perception path -- only the browser is replaced. That is
the right seam to fake: it keeps the tests fast and hermetic while still
proving that the engine can read a screen, classify it, and act.

The cases are chosen to cover the distinction the brief cares most about: that
a business outcome, a recoverable blip, a stuck state and a hard failure each
produce a *different* result, and that the caller can tell which is which.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from cua.artifact.schema import (
    Action, ActionKind, AppRef, Approval, Capability, Checkpoint, Extraction,
    Output, Param, ParamType, Point, Relation, Risk, Step, Target, TextAnchor,
)
from cua.evidence.run import RunLog
from cua.replay.engine import ParamError, ReplayEngine, validate_params
from cua.safety.policy import Policy
from cua.surface.base import Frame

FONT_PATH = "/usr/share/fonts/TTF/DejaVuSans.ttf"
CELL_X = 300          # column pitch; wide enough that OCR sees separate blocks
ROW_Y = 46


def render(lines: list[str]) -> bytes:
    """Draw a screen. ' | ' starts a new column, mimicking a table cell."""
    img = Image.new("RGB", (1100, 620), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT_PATH, 19)
    for row, line in enumerate(lines):
        for col, cell in enumerate(line.split(" | ")):
            draw.text((30 + col * CELL_X, 30 + row * ROW_Y), cell,
                      fill=(0, 0, 0), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeSurface:
    """A surface that shows scripted screens and advances on every action.

    Satisfies the `Surface` protocol structurally. Records what it was asked to
    do so tests can assert on behaviour, not just on the final verdict.
    """

    # Screens carrying one of these redirect themselves after a moment, the way
    # the real deferred-load page does via setTimeout. Without this the fake
    # would sit on the spinner forever and misrepresent the engine.
    SELF_ADVANCING = ("Retrieving",)

    def __init__(self, screens: list[list[str]]) -> None:
        self.screens = screens
        self.index = 0
        self.actions: list[tuple[str, object]] = []

    # -- observation
    def observe(self) -> Frame:
        png = render(self.screens[min(self.index, len(self.screens) - 1)])
        return Frame(png=png, width=1100, height=620)

    def _record(self, what: str, detail: object = None) -> None:
        self.actions.append((what, detail))

    def _advance(self, what: str, detail: object = None) -> None:
        """Only a real page transition moves the script forward.

        Navigating, following a link and pressing Return submit; typing into a
        field does not. Getting this right matters: a single TYPE step issues
        click + ctrl+a + type at the surface level, and a fake that advanced on
        every one of those would skip three screens and make the engine look
        broken when it is behaving correctly.
        """
        self._record(what, detail)
        if self.index < len(self.screens) - 1:
            self.index += 1

    # -- actions
    def navigate(self, url: str) -> None: self._advance("navigate", url)
    def click(self, point, button="left", modifiers=None): self._advance("click", (point.x, point.y))
    def double_click(self, point, modifiers=None): self._advance("double_click", point)
    def triple_click(self, point, modifiers=None): self._advance("triple_click", point)
    def move(self, point): pass
    def drag(self, start, end, modifiers=None): self._advance("drag", (start, end))
    def mouse_down(self): pass
    def mouse_up(self): pass
    def cursor_position(self): return Point(x=0, y=0)
    def type_text(self, text: str) -> None: self._record("type", text)
    def key(self, keys: str, repeat: int = 1) -> None:
        # Return submits; ctrl+a and friends are editing, not navigation.
        (self._advance if keys == "Return" else self._record)("key", keys)
    def hold_key(self, keys, duration): pass
    def scroll(self, point, direction, amount=3, modifiers=None): self._record("scroll", direction)
    def wait(self, seconds: float) -> None:
        # No real sleeping in tests -- but a self-advancing screen moves on.
        current = " ".join(self.screens[min(self.index, len(self.screens) - 1)])
        if any(marker in current for marker in self.SELF_ADVANCING):
            if self.index < len(self.screens) - 1:
                self.index += 1


class StuckSurface(FakeSurface):
    """Never advances -- for asserting that recovery budgets are bounded."""

    def _advance(self, what: str, detail: object = None) -> None:
        self._record(what, detail)


@pytest.fixture
def log(tmp_path: Path) -> RunLog:
    return RunLog.create("test", root=tmp_path)


def lookup_capability(**kw) -> Capability:
    """The read flow: search for a member, then read their savings balance."""
    return Capability(
        id="member.read_savings_balance",
        title="Read savings balance",
        description="Look up a member and return their savings balance.",
        app=AppRef(product="coreteller", version="7.2.1",
                   tenant_variant="northstar",
                   entry_url="http://target:8800/login"),
        params=[Param(name="member_no", type=ParamType.STRING,
                      pattern=r"\d{5}", example="10001")],
        outputs=[Output(
            name="savings_balance", type=ParamType.MONEY,
            extract=Extraction(
                anchor=TextAnchor(text="Savings", relation=Relation.RIGHT_OF),
                span_px=400,
                pattern=r"([\d,]+\.\d{2})",
            ),
        )],
        steps=[
            Step(index=0, intent="open the member search",
                 action=Action(kind=ActionKind.NAVIGATE,
                               value="http://target:8800/members"),
                 expect=Checkpoint(kind="text_present", text="Member Search",
                                   timeout_ms=500)),
            Step(index=1, intent="enter the member number",
                 action=Action(kind=ActionKind.TYPE, value="{member_no}"),
                 target=Target(label=TextAnchor(text="Member Number",
                                                relation=Relation.RIGHT_OF)),
                 expect=Checkpoint(kind="text_present", text="Results",
                                   timeout_ms=500)),
            Step(index=2, intent="open the matched record",
                 action=Action(kind=ActionKind.CLICK),
                 target=Target(label=TextAnchor(text="Dana Reyes",
                                                relation=Relation.ON)),
                 expect=Checkpoint(kind="text_present", text="Account Positions",
                                   timeout_ms=500)),
        ],
        checkpoint=Checkpoint(kind="text_present", text="Account Positions",
                              timeout_ms=500),
        **kw,
    )


# The fake advances one screen per action, so every script starts on the screen
# the run begins from -- here, the signed-on landing page before the first
# navigate takes effect.
START = ["Operator Sign On", "NorthStar Core Banking"]
SEARCH = ["Member Search", "Member Number | 10001", "Find"]
RESULTS = ["Results 1 record", "Member No. | Name", "10001 | Dana Reyes"]
DETAIL = ["Member 10001 Dana Reyes", "Account Positions",
          "Savings | 4,182.55 | Active", "Checking | 1,043.19 | Active"]

FLOW = [START, SEARCH, RESULTS, DETAIL]


# --------------------------------------------------------------- happy path

def test_a_successful_replay_returns_typed_outputs(log):
    surface = FakeSurface(FLOW)
    engine = ReplayEngine(surface, Policy(), log)
    result = engine.run(lookup_capability(), {"member_no": "10001"})

    assert result.status == "success", result.failure
    assert result.outputs["savings_balance"] == "4,182.55"
    assert result.steps_executed == 3


def test_the_parameter_is_actually_typed_into_the_form(log):
    surface = FakeSurface(FLOW)
    ReplayEngine(surface, Policy(), log).run(lookup_capability(), {"member_no": "10001"})
    typed = [d for what, d in surface.actions if what == "type"]
    assert "10001" in typed, f"parameter never reached the form: {surface.actions}"


def test_evidence_is_written_for_every_run(log):
    surface = FakeSurface(FLOW)
    ReplayEngine(surface, Policy(), log).run(lookup_capability(), {"member_no": "10001"})
    assert (log.root / "result.json").exists()
    assert (log.root / "run.jsonl").exists()
    assert list((log.root / "frames").glob("*.png")), "no frames captured"
    kinds = {e["event"] for e in log.events()}
    assert {"replay_started", "step_started", "replay_succeeded"} <= kinds


# ---------------------------------------------------- the four result kinds

def test_record_not_found_is_a_business_outcome_not_a_failure(log):
    """The distinction the brief calls the most common mistake."""
    surface = FakeSurface([START, SEARCH, ["No member record was found for member number 99999"]])
    result = ReplayEngine(surface, Policy(), log).run(
        lookup_capability(), {"member_no": "99999"})

    assert result.status == "business_outcome"
    assert result.outcome["code"] == "RECORD_NOT_FOUND"
    assert result.failure is None, "a legitimate answer must not be reported as a failure"


def test_permission_denial_is_also_a_business_outcome(log):
    surface = FakeSurface([START, SEARCH, ["Access Restricted", "E-403-PROFILE"]])
    result = ReplayEngine(surface, Policy(), log).run(
        lookup_capability(), {"member_no": "10003"})
    assert result.status == "business_outcome"
    assert result.outcome["code"] == "PERMISSION_DENIED"


def test_a_server_fault_is_a_hard_failure_with_debuggable_detail(log):
    surface = FakeSurface([START, SEARCH,
                                ["Server Error in '/CoreTeller' Application",
                                 "Correlation Id CT-89124939"]])
    result = ReplayEngine(surface, Policy(), log).run(
        lookup_capability(), {"member_no": "10001"})

    assert result.status == "failed"
    assert result.failure["code"] == "SERVER_FAULT"
    assert "stage" in result.failure, "a failure must say WHERE it happened"


def test_a_stuck_state_escalates_rather_than_failing(log):
    """A human could finish this run; reporting it as broken would be wrong."""
    routed: list[dict] = []
    surface = FakeSurface([START, SEARCH, ["Supervisor override code required."]])
    engine = ReplayEngine(surface, Policy(), log, escalate=routed.append)
    result = engine.run(lookup_capability(), {"member_no": "10005"})

    assert result.status == "escalated"
    assert result.outcome["code"] == "SUPERVISOR_OVERRIDE_REQUIRED"
    assert routed, "an intervention request must actually be routed"
    assert routed[0]["run_id"] == log.run_id
    assert "frame" in routed[0], "the human needs to see what the agent saw"


# ------------------------------------------------------------- recoverables

def test_an_interstitial_is_dismissed_and_the_run_continues(log):
    """A maintenance notice is furniture, not an outcome."""
    surface = FakeSurface([
        START,
        SEARCH,
        ["Scheduled Maintenance Notice", "Dismiss"],
        RESULTS,
        DETAIL,
    ])
    result = ReplayEngine(surface, Policy(), log).run(
        lookup_capability(), {"member_no": "10001"})

    assert result.status == "success", result.failure
    assert any(r["code"] == "MAINTENANCE_INTERSTITIAL" for r in result.recovered)
    assert any(r["recovery"] == "dismiss_interstitial" for r in result.recovered)


def test_a_deferred_load_is_waited_out(log):
    surface = FakeSurface([START, SEARCH, RESULTS,
                          ["Retrieving account positions, please wait"], DETAIL])
    result = ReplayEngine(surface, Policy(), log).run(
        lookup_capability(), {"member_no": "10001"})
    assert result.status == "success", result.failure
    assert any(r["code"] == "DEFERRED_LOAD" for r in result.recovered)


def test_recovery_is_bounded_rather_than_looping_forever(log):
    """A permanently-503ing host must fail, not hang.

    An unbounded retry is indistinguishable from a hang to the caller, which is
    the worst of both worlds: no result and no diagnosis.
    """
    surface = StuckSurface([["503 Service Unavailable"]])
    result = ReplayEngine(surface, Policy(), log).run(
        lookup_capability(), {"member_no": "10001"})

    assert result.status != "success"
    from cua.replay.engine import MAX_RECOVERIES_PER_RUN
    assert len(result.recovered) <= MAX_RECOVERIES_PER_RUN


# ------------------------------------------------------------- input contract

def test_missing_and_malformed_parameters_fail_before_any_action(log):
    surface = FakeSurface(FLOW)
    result = ReplayEngine(surface, Policy(), log).run(lookup_capability(), {})
    assert result.status == "failed"
    assert result.failure["stage"] == "parameters"
    assert surface.actions == [], "nothing should have been touched"


def test_parameter_validation_rules():
    cap = lookup_capability()
    assert validate_params(cap, {"member_no": "10001"}) == {"member_no": "10001"}
    with pytest.raises(ParamError, match="missing required"):
        validate_params(cap, {})
    with pytest.raises(ParamError, match="does not match"):
        validate_params(cap, {"member_no": "abc"})
    with pytest.raises(ParamError, match="unknown parameter"):
        validate_params(cap, {"member_no": "10001", "nope": "1"})


# ------------------------------------------------------------ safety gating

def test_an_irreversible_step_without_approval_escalates(log):
    cap = lookup_capability()
    cap.steps.append(Step(index=3, intent="commit the new account",
                          action=Action(kind=ActionKind.CLICK),
                          risk=Risk.IRREVERSIBLE))
    routed: list[dict] = []
    surface = FakeSurface(FLOW)
    engine = ReplayEngine(surface, Policy(allow_irreversible=False), log,
                          escalate=routed.append)
    result = engine.run(cap, {"member_no": "10001"})

    assert result.status == "escalated"
    assert result.outcome["code"] == "POLICY_BLOCKED"
    assert routed, "a blocked irreversible action must reach a human"


def test_navigation_off_the_allowlist_is_refused(log):
    cap = lookup_capability()
    cap.steps[0].action.value = "http://target:8800/_control/scenario"
    surface = FakeSurface(FLOW)
    policy = Policy(deny_paths=["/_control"])
    result = ReplayEngine(surface, policy, log).run(cap, {"member_no": "10001"})

    assert result.status == "failed"
    assert result.failure["kind"] == "surface"
    assert "allowlist" in result.failure["error"]


# ------------------------------------------------------------------- drift

def test_a_coordinate_only_match_refuses_to_click_blind(log):
    """A recorded coordinate identifies nothing.

    Regression for a false success: a run whose session had expired clicked
    blind through a stale page left by the previous run, matched its checkpoint
    against that page, and reported success with a balance it never fetched.
    Refusing is the only safe answer -- a wrong result presented as a right one
    is worse than no result.
    """
    cap = lookup_capability()
    cap.steps[1].target = Target(
        label=TextAnchor(text="A Label That Is Not On This Screen"),
        absolute=Point(x=400, y=76),
    )
    surface = FakeSurface(FLOW)
    result = ReplayEngine(surface, Policy(), log).run(cap, {"member_no": "10001"})

    assert result.status == "failed"
    assert result.failure["kind"] == "unresolved_target"
    assert "blind" in result.failure["observed"]


def test_a_target_that_cannot_be_found_at_all_fails_clearly(log):
    cap = lookup_capability()
    cap.steps[1].target = Target(label=TextAnchor(text="Nowhere To Be Seen"))
    surface = FakeSurface(FLOW)
    result = ReplayEngine(surface, Policy(), log).run(cap, {"member_no": "10001"})

    assert result.status == "failed"
    assert result.failure["kind"] == "unresolved_target"
    assert "expected" in result.failure and "observed" in result.failure
