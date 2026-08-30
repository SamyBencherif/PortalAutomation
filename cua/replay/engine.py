"""Deterministic replay: the path an AI agent actually triggers in production.

No model is consulted here. Given the same artifact, the same inputs and the
same target state, this does the same thing every time -- that is the entire
value proposition, and it is why the target was built with no randomness in it.

The interesting code is not the happy path, which is a for-loop. It is
`_settle`: before every action and every assertion, the engine looks at the
screen and asks the taxonomy what it is looking at. That is where a transient
503 gets absorbed, a maintenance interstitial gets dismissed, a deferred load
gets waited out -- and where a "no such member" gets recognised as an answer
rather than blundered past.

The result contract distinguishes four things a caller genuinely needs to tell
apart:

    success           the goal was reached; outputs are attached
    business_outcome  the app said something legitimate and final
    escalated         a human could finish this; the run is paused, not broken
    failed            something is wrong and here is what, where, and expected-
                      versus-observed
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from cua.artifact.schema import (
    ActionKind, Capability, Checkpoint, Extraction, Output, Param, ParamType,
    ResolutionTier, Step,
)
from cua.evidence.run import RunLog
from cua.perception import anchor as anchor_mod
from cua.perception import ocr
from cua.replay.outcomes import OutcomeClass, Recovery, Signature, classify
from cua.safety.policy import Policy
from cua.surface.base import Frame, Surface, SurfaceError

# Per `_settle` call: how many conditions we will clear before handing back
# whatever we are looking at.
MAX_RECOVERIES = 8
# Per run, across every settle. A legitimate long flow hits several
# interstitials (the target raises one every third page view), so this is
# generous -- but it is still a ceiling, because an unbounded retry loop
# against a permanently-broken host is indistinguishable from a hang.
MAX_RECOVERIES_PER_RUN = 24
POLL_INTERVAL_S = 0.4


@dataclass
class ReplayResult:
    status: str                                   # success|business_outcome|escalated|failed
    capability: str
    run_id: str
    outputs: dict[str, str] = field(default_factory=dict)
    outcome: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    recovered: list[dict[str, Any]] = field(default_factory=list)
    # Steps that resolved below tier 1. Not errors -- early warning that this
    # artifact is drifting and should be re-recorded before it breaks.
    drift: list[dict[str, Any]] = field(default_factory=list)
    steps_executed: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ParamError(ValueError):
    """The caller's inputs are wrong. Caught before a single action is taken."""


def validate_params(cap: Capability, given: dict[str, str]) -> dict[str, str]:
    """Check inputs against the capability's declared contract.

    Deliberately front-loaded. A missing member number should be a clear error
    from the API boundary, not a confusing failure four screens deep after the
    automation has already typed an empty string into a form.
    """
    resolved: dict[str, str] = {}
    for p in cap.params:
        if p.name not in given or given[p.name] == "":
            if p.required:
                raise ParamError(f"missing required parameter {p.name!r}")
            continue
        value = str(given[p.name])
        if p.pattern and not re.fullmatch(p.pattern, value):
            raise ParamError(
                f"parameter {p.name!r} = {value!r} does not match {p.pattern!r}"
            )
        if p.type is ParamType.INTEGER and not value.lstrip("-").isdigit():
            raise ParamError(f"parameter {p.name!r} must be an integer, got {value!r}")
        resolved[p.name] = value

    unknown = set(given) - {p.name for p in cap.params}
    if unknown:
        raise ParamError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
    return resolved


class ReplayEngine:
    def __init__(
        self,
        surface: Surface,
        policy: Policy,
        log: RunLog,
        product: str = "coreteller",
        credentials: tuple[str, str] | None = None,
        escalate: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.surface = surface
        self.policy = policy
        self.log = log
        self.product = product
        self.credentials = credentials
        self._escalate = escalate
        self.recovered: list[dict[str, Any]] = []
        self.drift: list[dict[str, Any]] = []
        self._budget = MAX_RECOVERIES_PER_RUN

    # ------------------------------------------------------------ observing

    def _look(self) -> tuple[Frame, ocr.Screen]:
        frame = self.surface.observe()
        return frame, ocr.read(frame.png)

    def _settle(self, context: str) -> tuple[Frame, ocr.Screen, Signature | None]:
        """Absorb recoverable conditions, then report what we are looking at.

        Returns the first non-recoverable state it finds. A terminal signature
        (business / hard / stuck) is handed back for the caller to act on --
        this function deliberately does not decide what a "record not found"
        means, only that it is not something to retry through.
        """
        for _ in range(MAX_RECOVERIES):
            frame, screen = self._look()
            sig = classify(screen, self.product)
            if sig is None or sig.klass is not OutcomeClass.RECOVERABLE:
                return frame, screen, sig

            if self._budget <= 0:
                # Spent. Stop absorbing and let the condition stand as the
                # answer, so the caller gets a diagnosis instead of a hang.
                self.log.event("recovery_budget_exhausted", context=context, code=sig.code)
                return frame, screen, sig
            self._budget -= 1

            self.log.event(
                "recovering", context=context, code=sig.code,
                recovery=sig.recovery.value if sig.recovery else None,
            )
            self.recovered.append({"context": context, "code": sig.code,
                                   "recovery": sig.recovery.value if sig.recovery else None})
            if not self._recover(sig, screen):
                return frame, screen, sig

        # Out of budget. Something is looping; that is a hard failure, not an
        # invitation to keep going.
        frame, screen = self._look()
        return frame, screen, classify(screen, self.product)

    def _recover(self, sig: Signature, screen: ocr.Screen) -> bool:
        """Apply one recovery. Returns False if we cannot."""
        match sig.recovery:
            case Recovery.RETRY_AFTER:
                # The target sends Retry-After: 1. Waiting slightly longer than
                # asked costs a second and avoids a second rejection.
                self.surface.wait(1.5)
                self.surface.key("F5")
                self.surface.wait(0.8)
                return True

            case Recovery.WAIT_FOR_CONTENT:
                # Navigation finished, the data has not arrived. This is the
                # case a load-event wait gets wrong: the DOM is genuinely empty,
                # not merely hidden.
                self.surface.wait(1.2)
                return True

            case Recovery.DISMISS_INTERSTITIAL:
                block = screen.find("Dismiss")
                if block is None:
                    return False
                # The shade eats clicks, so the Dismiss anchor itself must be
                # hit -- there is no Escape key handling and no .close().
                self.surface.click(block.box.center)
                self.surface.wait(0.6)
                return True

            case Recovery.ACCEPT_DIALOG:
                # A native window.confirm() is not in the page at all; it is a
                # browser-level window. Return activates its default button.
                self.surface.key("Return")
                self.surface.wait(0.4)
                return True

            case Recovery.REAUTH:
                if not self.credentials:
                    return False
                operator, password = self.credentials
                self.surface.type_text(operator)
                self.surface.key("Tab")
                self.surface.type_text(password)
                self.surface.key("Return")
                self.surface.wait(1.0)
                return True

        return False

    # --------------------------------------------------------- checkpoints

    def _check(self, screen: ocr.Screen, check: Checkpoint) -> bool:
        if check.kind == "text_present":
            return bool(check.text) and screen.contains(check.text)
        if check.kind == "text_absent":
            return bool(check.text) and not screen.contains(check.text)
        if check.kind == "text_matches":
            return bool(check.pattern) and re.search(check.pattern, screen.text) is not None
        return False

    def _await(self, check: Checkpoint, context: str):
        """Poll until the checkpoint holds or its timeout expires.

        Waits on *content*, never on a load event or a fixed sleep. The target
        renders a spinner and swaps real data in afterwards precisely to punish
        anything that assumes navigation means arrival.
        """
        deadline = time.time() + check.timeout_ms / 1000
        frame, screen, sig = self._settle(context)
        while True:
            if self._check(screen, check):
                return True, frame, screen, sig
            if sig is not None and sig.klass in (
                OutcomeClass.BUSINESS, OutcomeClass.HARD, OutcomeClass.STUCK
            ):
                # The app has given a final answer. Waiting for the checkpoint
                # we hoped for would just burn the timeout.
                return False, frame, screen, sig
            if time.time() >= deadline:
                return False, frame, screen, sig
            self.surface.wait(POLL_INTERVAL_S)
            frame, screen, sig = self._settle(context)

    # ---------------------------------------------------------- extraction

    def _extract(self, output: Output, screen: ocr.Screen) -> str | None:
        """Read one declared output off the screen, anchored to a label.

        Never a fixed rectangle: the row order in the account grid is not
        guaranteed, so "the text to the right of the word Savings" is the only
        formulation that survives the grid changing.
        """
        spec: Extraction = output.extract
        block = None
        for candidate in (spec.anchor.text, *spec.anchor.aliases):
            block = screen.find(candidate, spec.anchor.match, spec.anchor.occurrence)
            if block is not None:
                break
        if block is None:
            return None

        box = block.box
        # Words on the same visual line, within the span to the right.
        picked = []
        for other in screen.blocks:
            ob = other.box
            same_line = ob.y < box.bottom and ob.bottom > box.y
            in_span = box.right <= ob.x <= box.right + spec.span_px
            if same_line and in_span:
                picked.append((ob.x, other.text))
        text = " ".join(t for _, t in sorted(picked))

        if spec.pattern:
            m = re.search(spec.pattern, text)
            if not m:
                return None
            return m.group(1) if m.groups() else m.group(0)
        return text.strip() or None

    # ----------------------------------------------------------- execution

    def run(self, cap: Capability, params: dict[str, str]) -> ReplayResult:
        result = ReplayResult(status="failed", capability=cap.ref, run_id=self.log.run_id)
        self.log.event("replay_started", capability=cap.ref,
                       approval=cap.approval.value, params=sorted(params))

        try:
            values = validate_params(cap, params)
        except ParamError as e:
            result.failure = {"stage": "parameters", "error": str(e)}
            self.log.event("param_error", error=str(e))
            return self._finish(result)

        for step in cap.steps:
            verdict = self.policy.check_step(step, cap)
            if not verdict.allowed:
                # A blocked irreversible step is not a crash -- a human can
                # authorise it. Route it rather than failing it.
                return self._finish(self._escalation(
                    result, step, reason=verdict.reason, code="POLICY_BLOCKED"
                ))

            try:
                outcome = self._run_step(step, values, cap)
            except SurfaceError as e:
                result.failure = {"stage": f"step {step.index}", "error": str(e),
                                  "kind": "surface"}
                self.log.event("surface_error", step=step.index, error=str(e))
                return self._finish(result)
            except anchor_mod.UnresolvedTarget as e:
                frame, _ = self._look()
                self.log.frame(frame, f"step{step.index:02d}-unresolved")
                result.failure = {
                    "stage": f"step {step.index}", "intent": step.intent,
                    "expected": "the control to be locatable",
                    "observed": str(e), "kind": "unresolved_target",
                }
                self.log.event("unresolved_target", step=step.index, tried=e.tried)
                return self._finish(result)

            result.steps_executed += 1
            if outcome is not None:
                sig, screen = outcome
                if sig.klass is OutcomeClass.STUCK:
                    return self._finish(self._escalation(
                        result, step, reason=sig.message, code=sig.code
                    ))
                if sig.klass is OutcomeClass.BUSINESS:
                    result.status = "business_outcome"
                    result.outcome = {"code": sig.code, "class": sig.klass.value,
                                      "message": sig.message}
                    # A business outcome can still carry data worth returning --
                    # a duplicate write hands back the original confirmation.
                    result.outputs = self._collect(cap.outputs, screen)
                    self.log.event("business_outcome", code=sig.code, step=step.index)
                    return self._finish(result)
                result.status = "failed"
                result.failure = {"stage": f"step {step.index}", "intent": step.intent,
                                  "code": sig.code, "observed": sig.message,
                                  "kind": "app_fault"}
                self.log.event("hard_failure", code=sig.code, step=step.index)
                return self._finish(result)

        ok, frame, screen, sig = self._await(cap.checkpoint, "final")
        self.log.frame(frame, "final")
        if not ok:
            if sig is not None and sig.klass is OutcomeClass.BUSINESS:
                result.status = "business_outcome"
                result.outcome = {"code": sig.code, "class": sig.klass.value,
                                  "message": sig.message}
                result.outputs = self._collect(cap.outputs, screen)
                return self._finish(result)
            result.failure = {
                "stage": "final checkpoint",
                "expected": cap.checkpoint.text or cap.checkpoint.pattern,
                "observed": _excerpt(screen),
                "kind": "checkpoint",
            }
            self.log.event("checkpoint_failed", expected=cap.checkpoint.text)
            return self._finish(result)

        result.outputs = self._collect(cap.outputs, screen)
        missing = [o.name for o in cap.outputs if o.name not in result.outputs]
        if missing:
            # Reaching the right screen but failing to read the value off it is
            # a real failure: the caller asked for data, not for a visit.
            result.failure = {"stage": "extraction", "kind": "output_missing",
                              "expected": missing, "observed": _excerpt(screen)}
            self.log.event("extraction_failed", missing=missing)
            return self._finish(result)

        result.status = "success"
        self.log.event("replay_succeeded", outputs=sorted(result.outputs))
        return self._finish(result)

    def _run_step(self, step: Step, values: dict[str, str], cap: Capability):
        """Execute one step. Returns a terminal signature, or None to continue."""
        self.log.event("step_started", index=step.index, intent=step.intent,
                       action=step.action.kind.value, risk=step.risk.value)

        frame, screen, sig = self._settle(f"step {step.index} pre")
        if sig is not None and sig.klass in (
            OutcomeClass.BUSINESS, OutcomeClass.HARD, OutcomeClass.STUCK
        ):
            self.log.frame(frame, f"step{step.index:02d}-{sig.code.lower()}")
            return sig, screen

        if step.target is not None:
            res = anchor_mod.resolve(step.target, screen, frame.png)
            self.log.event("target_resolved", index=step.index, tier=res.tier.value,
                           detail=res.detail)
            if res.is_degraded:
                self.drift.append({"step": step.index, "intent": step.intent,
                                   "tier": res.tier.value, "detail": res.detail})
            point = res.point
        else:
            point = None

        kind = step.action.kind
        value = _interpolate(step.action.value, values) if step.action.value else None

        match kind:
            case ActionKind.NAVIGATE:
                # Checked again here even though the proxy enforces it: a clear
                # refusal beats a mysterious blocked page.
                if value and not self.policy.check_url(value).allowed:
                    raise SurfaceError(f"navigation to {value!r} is off the allowlist")
                self.surface.navigate(value or cap.app.entry_url)
            case ActionKind.CLICK:
                self.surface.click(point) if point else None
            case ActionKind.TYPE:
                if point:
                    self.surface.click(point)
                    # Legacy inputs arrive pre-populated; replacing beats
                    # appending to whatever was already there.
                    self.surface.key("ctrl+a")
                self.surface.type_text(value or "")
            case ActionKind.KEY:
                self.surface.key(value or "Return")
            case ActionKind.ACCEPT_DIALOG:
                self.surface.key("Return")
            case ActionKind.WAIT_FOR | ActionKind.EXTRACT:
                pass

        if step.expect is not None:
            ok, frame, screen, sig = self._await(step.expect, f"step {step.index}")
            self.log.frame(frame, f"step{step.index:02d}")
            if not ok:
                if sig is not None and sig.klass in (
                    OutcomeClass.BUSINESS, OutcomeClass.HARD, OutcomeClass.STUCK
                ):
                    return sig, screen
                raise _checkpoint_failure(step, screen)
        return None

    def _collect(self, outputs: list[Output], screen: ocr.Screen) -> dict[str, str]:
        found: dict[str, str] = {}
        for out in outputs:
            value = self._extract(out, screen)
            if value is not None:
                found[out.name] = value
                # Sensitive outputs are returned to the caller in-process but
                # never named in the log with their value attached.
                self.log.event("output_extracted", name=out.name,
                               value="[REDACTED]" if out.sensitive else value)
        return found

    def _escalation(self, result: ReplayResult, step: Step, reason: str, code: str):
        result.status = "escalated"
        result.outcome = {"code": code, "class": "stuck", "message": reason,
                          "step": step.index, "intent": step.intent}
        frame, _ = self._look()
        shot = self.log.frame(frame, f"step{step.index:02d}-escalated")
        self.log.event("escalated", step=step.index, code=code, reason=reason)
        if self._escalate is not None:
            self._escalate({
                "capability": result.capability, "run_id": self.log.run_id,
                "step": step.index, "intent": step.intent, "reason": reason,
                "code": code, "frame": str(shot),
            })
        return result

    def _finish(self, result: ReplayResult) -> ReplayResult:
        result.recovered = self.recovered
        result.drift = self.drift
        self.log.result(result.to_dict())
        return result


def _interpolate(template: str, values: dict[str, str]) -> str:
    """Substitute {param} placeholders. Unknown names are left alone."""
    def sub(m: re.Match[str]) -> str:
        return values.get(m.group(1), m.group(0))
    return re.sub(r"\{([a-z_][a-z0-9_]*)\}", sub, template)


def _excerpt(screen: ocr.Screen, limit: int = 240) -> str:
    text = " / ".join(b.text for b in screen.blocks[:12])
    return text[:limit]


def _checkpoint_failure(step: Step, screen: ocr.Screen) -> SurfaceError:
    check = step.expect
    expected = (check.text or check.pattern or "?") if check else "?"
    return SurfaceError(
        f"step {step.index} ({step.intent}): expected {expected!r} "
        f"but saw: {_excerpt(screen)}"
    )


__all__ = ["ReplayEngine", "ReplayResult", "validate_params", "ParamError"]
