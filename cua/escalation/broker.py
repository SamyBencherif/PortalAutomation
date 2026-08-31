"""Bringing a human into the loop, on the same live session.

The requirement that shapes this file is that the human must take over *the
session the automation was using*, not a fresh one. That is easy to claim and
easy to fake, so the design makes it structurally true rather than asserted:

- The agent drives an X display inside the container. `x11vnc` publishes that
  same display, and noVNC puts it in a browser tab. When the operator moves the
  mouse they are moving the agent's mouse, in the agent's browser, holding the
  agent's session cookie. There is no second session to get out of sync,
  because there is only one.

- Control is a single flag with one owner at a time. The automation blocks on
  an Event while the human holds it, so the two can never both be acting.

- The handoff is also announced to the target application, through the
  control-plane endpoint it already provides. That is what turns "the human
  took over" from a claim in our log into a fact in *the application's* audit
  trail: one continuous session id, with the actor changing partway down. Our
  own log could be wrong or generous; the target's cannot be, and a reviewer
  can check it independently.

Note the asymmetry, which is deliberate: the broker talks to `/_control`, and
the *agent* is forbidden from doing so by the allowlist. The harness arranging
a handoff and the agent reconfiguring its own target are different acts, and
only one of them is legitimate.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

AGENT = "agent"
HUMAN = "human"


@dataclass
class InterventionRequest:
    """Everything a human needs to act, without reading the code.

    Carries the *why* and a screenshot, because an operator handed only "step 4
    failed" has to reconstruct the situation before they can help.
    """

    id: str
    capability: str
    run_id: str
    step: int
    intent: str
    reason: str
    code: str
    frame: str | None = None
    raised_at: float = field(default_factory=time.time)
    resolved_at: float | None = None
    # What the human did, in their words. Recorded so the run's evidence
    # explains the gap rather than leaving an unexplained jump in the log.
    operator_note: str | None = None
    # Who did it. Empty when the run was resumed through its own console,
    # which has no identity to offer -- the difference between "a human" and a
    # named one is exactly what the shared queue adds.
    operator: str = ""
    # This intervention's id on the cross-run queue, when there is one. The
    # run and the queue number their interventions independently, so the two
    # ids differ and the run needs to keep the mapping to poll.
    queue_id: str | None = None

    @property
    def pending(self) -> bool:
        return self.resolved_at is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "capability": self.capability, "run_id": self.run_id,
            "step": self.step, "intent": self.intent, "reason": self.reason,
            "code": self.code, "frame": self.frame,
            "raised_at": self.raised_at, "resolved_at": self.resolved_at,
            "operator_note": self.operator_note, "operator": self.operator,
            "queue_id": self.queue_id,
        }


class Broker:
    """Owns who is in control, and the transfer between them."""

    def __init__(
        self,
        target_base: str = "http://target:8800",
        vnc_url: str = "http://localhost:6080/vnc.html",
        log: Any = None,
        dispatcher: str | None = None,
        poll_interval: float = 2.0,
    ) -> None:
        self.target_base = target_base.rstrip("/")
        self.vnc_url = vnc_url
        self.log = log
        # The cross-run queue, when this run has one to publish to. Optional
        # by design: a run started with its own console is still a complete
        # escalation path, and the recorded evidence depends on that staying
        # true. The queue adds reach, it does not replace the mechanism.
        self.dispatcher = dispatcher.rstrip("/") if dispatcher else None
        self.poll_interval = poll_interval
        self.controller = AGENT
        self.requests: dict[str, InterventionRequest] = {}
        self._resume = threading.Event()
        self._seq = 0

    # --------------------------------------------------- control transfer

    def _tell_target(self, actor: str) -> dict[str, Any] | None:
        """Announce the actor change to the application itself.

        Best-effort: a target that does not offer a control plane simply does
        not get told, and the handoff still works. But when it does -- as this
        one does -- its audit log becomes independent evidence that one session
        was operated by two actors in sequence.

        Deliberately NOT routed through the policy proxy. This is the harness
        arranging a handoff, not the agent reaching for its own controls.
        """
        try:
            sid = self._current_session()
            if sid is None:
                return None
            r = httpx.post(
                f"{self.target_base}/_control/handoff",
                json={"sid": sid, "actor": actor},
                headers={"content-type": "application/json"},
                timeout=5.0,
            )
            return r.json() if r.status_code == 200 else None
        except httpx.HTTPError:
            return None

    def _current_session(self) -> str | None:
        try:
            r = httpx.get(f"{self.target_base}/_control/state", timeout=5.0)
            sessions = r.json().get("sessions") or []
            return sessions[0]["sid"] if sessions else None
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            return None

    # ------------------------------------------------------------ raising

    def raise_intervention(self, context: dict[str, Any]) -> InterventionRequest:
        """Route a stuck run to a human and hand them control."""
        self._seq += 1
        req = InterventionRequest(
            id=f"int-{self._seq:03d}",
            capability=context.get("capability", "?"),
            run_id=context.get("run_id", "?"),
            step=int(context.get("step", -1)),
            intent=context.get("intent", ""),
            reason=context.get("reason", ""),
            code=context.get("code", "STUCK"),
            frame=context.get("frame"),
        )
        self.requests[req.id] = req
        self._resume.clear()

        self.controller = HUMAN
        handoff = self._tell_target(HUMAN)
        queued = self._publish(req)
        if self.log is not None:
            self.log.event(
                "intervention_raised", id=req.id, code=req.code, step=req.step,
                reason=req.reason, vnc=self.vnc_url,
                target_ack=bool(handoff), queued_as=req.queue_id,
            )
            if self.dispatcher and not queued:
                # Worth its own line. The run is about to block for the whole
                # escalation timeout on a queue that never heard about it, and
                # "nobody came" would otherwise be indistinguishable from
                # "nobody was told".
                self.log.event("queue_unreachable", id=req.id,
                               dispatcher=self.dispatcher)
        return req

    def _publish(self, req: InterventionRequest) -> bool:
        """Put this request on the cross-run queue, if there is one.

        Best-effort, like the target announcement: a dispatcher that is down
        must not take the run down with it. The run falls back to its own
        console, which is exactly the situation it would have been in without
        a queue at all.
        """
        if self.dispatcher is None:
            return False
        try:
            r = httpx.post(
                f"{self.dispatcher}/interventions",
                json={**req.to_dict(), "vnc_url": self.vnc_url},
                headers={"content-type": "application/json"},
                timeout=5.0,
            )
            if r.status_code != 201:
                return False
            req.queue_id = r.json().get("id")
            return req.queue_id is not None
        except (httpx.HTTPError, ValueError):
            return False

    def wait_for_resume(self, timeout: float | None = None) -> bool:
        """Block while the human works. Returns False if they never came back.

        The automation genuinely stops here. Any design where it keeps polling
        and acting while a human is typing produces two actors racing on one
        session, which is worse than either alone.
        """
        got_it = (self._await_queue(timeout) if self.dispatcher
                  else self._resume.wait(timeout))
        if self.log is not None:
            self.log.event("resume_wait_finished", resumed=got_it,
                           via="queue" if self.dispatcher else "console")
        return got_it

    def _await_queue(self, timeout: float | None) -> bool:
        """Poll the cross-run queue until someone hands control back.

        The run does the polling rather than the dispatcher calling back,
        because a callback would need every replay container to be addressable
        *from* the dispatcher -- a firewall conversation rather than a design.

        It waits on the local Event between polls rather than sleeping, so a
        resume through this run's own console still ends the wait immediately.
        Both routes stay live and whichever fires first wins.
        """
        deadline = None if timeout is None else time.time() + timeout
        while True:
            if self._resume.wait(self.poll_interval):
                return True
            for req in self.pending:
                done = self._fetch(req)
                if done is not None:
                    self.resume(req.id, note=done.get("note"),
                                operator=done.get("resolved_by", ""))
                    return True
            if deadline is not None and time.time() >= deadline:
                return False

    def _fetch(self, req: InterventionRequest) -> dict[str, Any] | None:
        """The queue's view of one request, once it has been handed back."""
        if self.dispatcher is None or req.queue_id is None:
            return None
        try:
            r = httpx.get(f"{self.dispatcher}/interventions/{req.queue_id}",
                          timeout=5.0)
            if r.status_code != 200:
                return None
            item = r.json()
            return None if item.get("pending", True) else item
        except (httpx.HTTPError, ValueError):
            return None

    def resume(self, request_id: str, note: str | None = None,
               operator: str = "") -> bool:
        """The human hands control back."""
        req = self.requests.get(request_id)
        if req is None or not req.pending:
            return False
        req.resolved_at = time.time()
        req.operator_note = note
        req.operator = operator

        self.controller = AGENT
        self._tell_target(AGENT)
        if self.log is not None:
            self.log.event("control_returned", id=req.id, note=note,
                           operator=operator or None,
                           held_for_s=round(req.resolved_at - req.raised_at, 1))
        self._resume.set()
        return True

    @property
    def pending(self) -> list[InterventionRequest]:
        return [r for r in self.requests.values() if r.pending]

    @property
    def last_resolved(self) -> InterventionRequest | None:
        """The handoff that just ended, if one has.

        The run resuming behind it wants the operator's note, so that the
        evidence says what a human did in the gap rather than leaving an
        unexplained jump between two steps.
        """
        done = [r for r in self.requests.values() if not r.pending]
        return max(done, key=lambda r: r.resolved_at or 0.0) if done else None

    def target_audit(self) -> list[dict[str, Any]]:
        """The application's own view of who did what.

        Fetched for the evidence bundle so a reviewer can verify the handoff
        against a log this system does not write.
        """
        try:
            r = httpx.get(f"{self.target_base}/_control/state", timeout=5.0)
            return r.json().get("audit", [])
        except (httpx.HTTPError, ValueError):
            return []


__all__ = ["Broker", "InterventionRequest", "AGENT", "HUMAN"]
