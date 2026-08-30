"""The error taxonomy: telling three very different things apart.

The brief names conflating these as the most common design mistake in this
problem, and it is right -- "no such member" is an *answer*, and a caller that
receives it as an exception has been told nothing useful.

    BUSINESS     A legitimate result the caller asked for and needs. The run
                 did what it was told; the world said no. Not an error.
    RECOVERABLE  A transient condition the replay is expected to absorb on its
                 own, within bounds, and then carry on.
    HARD         The run cannot continue and a human needs a debuggable report.
    STUCK        The run cannot continue but a human could finish it, so it
                 escalates rather than fails.

Detection is by screen signature, because OCR is the only sense we have. That
sounds fragile and mostly isn't: these apps render stable error furniture
(reference codes like E-403-PROFILE, fixed banner copy) precisely so their own
support staff can triage from a screenshot. We are keying on the same thing a
human operator keys on.

The catalogue is data, not control flow, so that a second vendor product is a
new table rather than a new code path -- the multi-tenant seam in §3.7.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from cua.perception.ocr import Screen


class OutcomeClass(StrEnum):
    BUSINESS = "business"
    RECOVERABLE = "recoverable"
    HARD = "hard"
    STUCK = "stuck"


class Recovery(StrEnum):
    """What a RECOVERABLE signature tells the engine to actually do."""

    RETRY_AFTER = "retry_after"              # honour Retry-After, try again
    DISMISS_INTERSTITIAL = "dismiss_interstitial"
    WAIT_FOR_CONTENT = "wait_for_content"    # navigation done, data isn't
    ACCEPT_DIALOG = "accept_dialog"          # a native window.confirm()
    REAUTH = "reauth"                        # session died; sign on and resume


@dataclass(frozen=True)
class Signature:
    """One recognisable screen state.

    `any_of` holds normalised fragments; a screen matching any one of them is
    this signature. Reference codes come first where they exist because they
    are far more stable than prose -- copy gets reworded between releases,
    E-403-PROFILE does not.
    """

    code: str
    klass: OutcomeClass
    any_of: tuple[str, ...]
    message: str
    recovery: Recovery | None = None
    # Fragments that, if present, veto the match. Guards against a signature
    # firing on a page that merely mentions the condition.
    unless: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# CoreTeller (mock_teller). Both tenants share it: same vendor product, and the
# reference codes are identical even though the nouns differ.
# ---------------------------------------------------------------------------

CORETELLER: tuple[Signature, ...] = (
    # -------------------------------------------------- business outcomes
    Signature(
        code="RECORD_NOT_FOUND",
        klass=OutcomeClass.BUSINESS,
        any_of=("no member record was found", "no customer record was found",
                "record exists for number"),
        message="No such record. The search succeeded and matched nothing.",
    ),
    Signature(
        code="PERMISSION_DENIED",
        klass=OutcomeClass.BUSINESS,
        any_of=("e-403-profile", "access restricted"),
        message="The operator profile is not authorised to view this record.",
    ),
    Signature(
        code="LIMIT_REACHED",
        klass=OutcomeClass.BUSINESS,
        any_of=("e-409-maxsub", "already holds the maximum"),
        message="The record already holds the maximum number of sub-accounts.",
    ),
    Signature(
        # Replaying an irreversible write is the NORMAL case for this system,
        # so it gets a first-class result. The screen hands back the original
        # confirmation number, which makes this the idempotent answer rather
        # than a failure: the work is done, here is its receipt.
        code="ALREADY_EXISTS",
        klass=OutcomeClass.BUSINESS,
        any_of=("e-409-duplicate", "already exists"),
        message="This sub-account was already opened; the original "
                "confirmation is returned. No second account was created.",
    ),
    Signature(
        code="ACCOUNT_FROZEN",
        klass=OutcomeClass.BUSINESS,
        any_of=("accounts are frozen", "cannot be transacted against"),
        message="Balances are readable but the account is frozen.",
    ),
    Signature(
        code="AMBIGUOUS_MATCH",
        klass=OutcomeClass.BUSINESS,
        any_of=("more than one member matched", "more than one customer matched"),
        message="The search matched several records; disambiguation required.",
    ),
    Signature(
        code="VALIDATION_ERROR",
        klass=OutcomeClass.BUSINESS,
        # Deliberately the *whole* message, not a fragment of it. An earlier
        # version keyed on "is required" and duly classified the maintenance
        # interstitial -- "No action is required." -- as a validation error,
        # ending the run with a business outcome that never happened. A
        # signature has to be specific enough that it cannot occur in ordinary
        # page prose, which rules out any phrase a UI might say in passing.
        any_of=("nickname is required", "purpose must be selected",
                "must be a numeric amount", "deposit must be at least",
                "password of at least"),
        message="The form rejected the supplied values.",
    ),

    # -------------------------------------------------- recoverable
    Signature(
        code="SERVICE_BUSY",
        klass=OutcomeClass.RECOVERABLE,
        any_of=("503 service unavailable", "temporarily busy"),
        message="Transient upstream unavailability.",
        recovery=Recovery.RETRY_AFTER,
    ),
    Signature(
        code="MAINTENANCE_INTERSTITIAL",
        klass=OutcomeClass.RECOVERABLE,
        any_of=("scheduled maintenance notice",),
        message="A maintenance notice is covering the page.",
        recovery=Recovery.DISMISS_INTERSTITIAL,
    ),
    Signature(
        code="DEFERRED_LOAD",
        klass=OutcomeClass.RECOVERABLE,
        any_of=("retrieving account positions", "please wait"),
        message="Navigation completed before the data did.",
        recovery=Recovery.WAIT_FOR_CONTENT,
    ),
    Signature(
        code="SESSION_EXPIRED",
        klass=OutcomeClass.RECOVERABLE,
        any_of=("session has ended due to inactivity",),
        message="The session expired mid-flow; re-authentication can resume it.",
        recovery=Recovery.REAUTH,
    ),

    # -------------------------------------------------- stuck -> escalate
    Signature(
        # The agent cannot obtain a supervisor code, by construction. Guessing
        # would be worse than stopping, so this routes to a human instead of
        # burning retries.
        code="SUPERVISOR_OVERRIDE_REQUIRED",
        klass=OutcomeClass.STUCK,
        any_of=("supervisor override code required", "supervisor authorisation is required"),
        message="A supervisor override code is required to continue.",
    ),

    # -------------------------------------------------- hard failures
    Signature(
        code="SERVER_FAULT",
        klass=OutcomeClass.HARD,
        any_of=("server error in", "servicingexception", "correlation id"),
        message="The application faulted. The correlation id identifies the run "
                "in the vendor's logs.",
    ),
    Signature(
        code="LOST_DRAFT",
        klass=OutcomeClass.HARD,
        any_of=("e-440-nodraft", "session data lost"),
        message="The staged draft is gone; the flow must restart from the form.",
    ),
)


CATALOGUES: dict[str, tuple[Signature, ...]] = {"coreteller": CORETELLER}


def classify(screen: Screen, product: str = "coreteller") -> Signature | None:
    """Identify the screen state, if it is one we know about.

    Order matters and follows the catalogue: the more specific and more serious
    conditions are listed before the general ones, so a 500 page that happens
    to contain the word "required" is not mistaken for a validation error.
    """
    signatures = CATALOGUES.get(product, CORETELLER)
    for sig in signatures:
        if any(screen.contains(frag) for frag in sig.any_of):
            if any(screen.contains(veto) for veto in sig.unless):
                continue
            return sig
    return None


def is_terminal(sig: Signature | None) -> bool:
    """Whether reaching this signature ends the run one way or another."""
    return sig is not None and sig.klass in (
        OutcomeClass.BUSINESS, OutcomeClass.HARD, OutcomeClass.STUCK
    )


__all__ = [
    "OutcomeClass", "Recovery", "Signature", "CORETELLER", "CATALOGUES",
    "classify", "is_terminal",
]
