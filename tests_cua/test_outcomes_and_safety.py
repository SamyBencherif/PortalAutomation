"""The taxonomy and the guardrails.

These are pure-logic tests: no browser, no container, no API key. That is
deliberate -- the classification rules and the policy gates are the parts most
likely to be quietly wrong, and they should be checkable in under a second.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from cua.artifact.schema import (
    Action, ActionKind, AppRef, Approval, Capability, Checkpoint, Risk, Step,
)
from cua.perception.ocr import Rect, Screen, TextBlock, Word
from cua.replay import outcomes
from cua.replay.outcomes import OutcomeClass, Recovery, classify
from cua.safety import policy

# A real face, not PIL's default bitmap font (tesseract cannot read that one).
FONT = "/usr/share/fonts/TTF/DejaVuSans.ttf"


def screen_of(*lines: str) -> Screen:
    """Build a Screen from text, bypassing OCR.

    Classification is a function of the text on screen, so it can and should be
    tested without dragging tesseract in.
    """
    blocks = []
    for i, line in enumerate(lines):
        words = tuple(
            Word(text=w, conf=90.0, box=Rect(x=10 + 60 * j, y=20 * i, w=50, h=10))
            for j, w in enumerate(line.split())
        )
        if words:
            blocks.append(TextBlock(words=words))
    return Screen(blocks)


# --------------------------------------------------------------- taxonomy

@pytest.mark.parametrize(
    "text,code,klass",
    [
        ("No member record was found for member number 99999",
         "RECORD_NOT_FOUND", OutcomeClass.BUSINESS),
        ("Access Restricted E-403-PROFILE", "PERMISSION_DENIED", OutcomeClass.BUSINESS),
        ("E-409-MAXSUB already holds the maximum", "LIMIT_REACHED", OutcomeClass.BUSINESS),
        ("E-409-DUPLICATE Sub-Account Already Exists", "ALREADY_EXISTS", OutcomeClass.BUSINESS),
        ("More than one member matched", "AMBIGUOUS_MATCH", OutcomeClass.BUSINESS),
        ("Nickname is required.", "VALIDATION_ERROR", OutcomeClass.BUSINESS),
        ("503 Service Unavailable", "SERVICE_BUSY", OutcomeClass.RECOVERABLE),
        ("Scheduled Maintenance Notice", "MAINTENANCE_INTERSTITIAL", OutcomeClass.RECOVERABLE),
        ("Retrieving account positions, please wait", "DEFERRED_LOAD", OutcomeClass.RECOVERABLE),
        ("Your session has ended due to inactivity", "SESSION_EXPIRED", OutcomeClass.RECOVERABLE),
        ("Supervisor override code required.", "SUPERVISOR_OVERRIDE_REQUIRED", OutcomeClass.STUCK),
        ("Server Error in '/CoreTeller' Application", "SERVER_FAULT", OutcomeClass.HARD),
        ("E-440-NODRAFT Session Data Lost", "LOST_DRAFT", OutcomeClass.HARD),
    ],
)
def test_each_condition_lands_in_the_right_class(text, code, klass):
    sig = classify(screen_of(text))
    assert sig is not None, f"{text!r} matched no signature"
    assert sig.code == code
    assert sig.klass is klass


def test_a_not_found_result_is_not_an_error():
    """The distinction the brief calls the most common mistake.

    A caller asking "does member 99999 exist" got its answer. Returning that as
    a failure tells them nothing and invites a pointless retry.
    """
    sig = classify(screen_of("No member record was found for member number 99999"))
    assert sig.klass is OutcomeClass.BUSINESS
    assert sig.klass is not OutcomeClass.HARD


def test_a_duplicate_write_is_a_business_outcome_not_a_failure():
    """Replaying an irreversible write is this system's normal case.

    The screen returns the ORIGINAL confirmation, so the honest reading is
    "already done, here is the receipt" -- an idempotent success for the
    caller, not a crash.
    """
    sig = classify(screen_of("Sub-Account Already Exists E-409-DUPLICATE "
                             "confirmation CNF-000001. No new account was opened."))
    assert sig.code == "ALREADY_EXISTS"
    assert sig.klass is OutcomeClass.BUSINESS


def test_recoverable_conditions_carry_an_instruction():
    """Classifying a transient without saying what to do about it is useless."""
    for text, expected in [
        ("503 Service Unavailable", Recovery.RETRY_AFTER),
        ("Scheduled Maintenance Notice", Recovery.DISMISS_INTERSTITIAL),
        ("Retrieving account positions", Recovery.WAIT_FOR_CONTENT),
        ("Your session has ended due to inactivity", Recovery.REAUTH),
    ]:
        sig = classify(screen_of(text))
        assert sig.recovery is expected


def test_stuck_is_distinct_from_hard_failure():
    """A supervisor code is obtainable by a human and not by the agent.

    Collapsing this into HARD would burn the escalation path: the run would be
    reported as broken when in fact it is one human action from completing.
    """
    stuck = classify(screen_of("Supervisor override code required."))
    hard = classify(screen_of("Server Error in '/CoreTeller' Application"))
    assert stuck.klass is OutcomeClass.STUCK
    assert hard.klass is OutcomeClass.HARD
    assert outcomes.is_terminal(stuck) and outcomes.is_terminal(hard)


def test_an_ordinary_screen_matches_nothing():
    assert classify(screen_of("Member 10001 Dana Reyes", "Account Positions")) is None


def test_both_tenants_share_one_catalogue():
    """Same vendor product, different nouns -- and identical reference codes.

    This is the cross-tenant claim in miniature: the codes are what we key on
    precisely because they survive the rebranding that the prose does not.
    """
    assert classify(screen_of("No customer record was found")).code == "RECORD_NOT_FOUND"
    assert classify(screen_of("No member record was found")).code == "RECORD_NOT_FOUND"


# ---------------------------------------------------------------- allowlist

def test_the_control_plane_is_refused():
    """An agent that can rearm its own target is not under test."""
    d = policy.DEFAULT_POLICY.check_url("http://target:8800/_control/scenario")
    assert not d.allowed
    assert "_control" in d.reason


def test_the_destructive_admin_route_is_refused():
    d = policy.DEFAULT_POLICY.check_url("http://target:8800/admin/members/10001/close")
    assert not d.allowed


def test_the_real_flows_are_permitted():
    for url in (
        "http://target:8800/login",
        "http://target:8800/members?memberNumber=10001",
        "http://target:8800/members/10001/subaccounts/new",
        "http://target:8800/frame/members/10001",
        "http://target:8800/pb/customers",
    ):
        assert policy.DEFAULT_POLICY.check_url(url).allowed, url


def test_an_off_allowlist_host_is_refused():
    d = policy.DEFAULT_POLICY.check_url("http://evil.example.com/members")
    assert not d.allowed
    assert "host" in d.reason


def test_deny_beats_allow():
    p = policy.Policy(allow_paths=["/"], deny_paths=["/admin"])
    assert not p.check_url("http://h/admin/x").allowed


# ------------------------------------------------------- irreversible gate

def _capability(approval: Approval) -> Capability:
    return Capability(
        id="member.open_subaccount",
        title="Open a sub-account",
        description="test",
        app=AppRef(product="coreteller", entry_url="http://target:8800/login"),
        steps=[Step(index=0, intent="commit",
                    action=Action(kind=ActionKind.CLICK), risk=Risk.IRREVERSIBLE)],
        checkpoint=Checkpoint(kind="text_present", text="Sub-Account Opened"),
        approval=approval,
    )


def test_an_irreversible_step_needs_both_gates():
    """Approval and explicit run authorisation are independent on purpose.

    Either one alone is a single mistake away from opening real accounts in a
    real institution.
    """
    step = _capability(Approval.APPROVED).steps[0]

    draft = _capability(Approval.DRAFT)
    permissive = policy.Policy(allow_irreversible=True)
    assert not permissive.check_step(step, draft).allowed, "draft must not run unattended"

    approved = _capability(Approval.APPROVED)
    default = policy.Policy(allow_irreversible=False)
    assert not default.check_step(step, approved).allowed, "run must opt in"

    assert permissive.check_step(step, approved).allowed


def test_a_safe_step_needs_no_ceremony():
    cap = _capability(Approval.DRAFT)
    safe = Step(index=0, intent="read", action=Action(kind=ActionKind.EXTRACT))
    assert policy.Policy().check_step(safe, cap).allowed


# ---------------------------------------------------------------- redaction

def test_ssn_and_dates_are_stripped_from_text():
    out = policy.redact_text("SSN 412-55-9080 DOB 1984-07-19 for Dana Reyes")
    assert "412-55-9080" not in out
    assert "1984-07-19" not in out
    assert "Dana Reyes" in out, "redaction should be surgical, not scorched-earth"


def test_pii_is_masked_in_pixels_before_a_frame_becomes_durable():
    """Evidence screenshots are the place regulated data would persist.

    Rendered rather than fixture-loaded because the detail screen needs a
    session; the end-to-end run validates this against a real frame.
    """
    img = Image.new("RGB", (460, 120), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    # A real TrueType face at a real size. PIL's default bitmap font is
    # illegible to tesseract, which would make this test pass or fail for
    # reasons that have nothing to do with redaction.
    font = ImageFont.truetype(FONT, 20)
    draw.text((10, 10), "SSN 412-55-9080", fill=(0, 0, 0), font=font)
    draw.text((10, 60), "Branch Riverside", fill=(0, 0, 0), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    masked, report = policy.redact_frame(buf.getvalue())

    assert report.regions_masked >= 1, "the SSN should have been found and masked"
    from cua.perception import ocr as _ocr
    assert "412-55-9080" not in _ocr.read(masked).text
    # And the run must be able to say that information was withheld, not absent.
    assert report.values_masked == report.regions_masked
