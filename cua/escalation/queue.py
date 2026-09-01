"""The queue an operator works from, spanning runs.

The in-process console answers one question well -- "this run needs you, here
is its screen" -- and it answers it only for whoever launched that run, on that
machine. The deployment this system is aimed at has the opposite shape: an
operator who did not start anything, covering several runs at once, who needs
to be told which one wants them and to be stopped from colliding with a
colleague on the same display.

Three things are missing and they are useless apart, which is why they are one
module:

- **A queue.** Interventions from many runs, outliving any one of them. The
  run-side `Broker` still owns control transfer for its own run; this owns the
  work list.
- **An identity.** Claiming is exclusive. Two operators covering a fleet must
  not both take over the same X display, and "who resolved this" belongs in the
  evidence next to "what they did" -- a note signed by nobody is an anecdote.
- **An outbound channel.** See `notify.py`. A queue nobody is told about is the
  tab title again, with more steps.

What is deliberately *not* here: durability. The queue is in memory, so a
dispatcher restart forgets its work list. Runs blocked on it recover -- they
poll, get a 404 and time out the way they would have anyway -- but the operator
loses the list. Persisting it is a database decision, and the brief does not
reward one; the seam is `Queue` itself, so a persistent implementation replaces
this class without touching the dispatcher or the broker.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

UNCLAIMED = ""


def _as_int(value: Any, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class QueueError(Exception):
    """An operator asked for something the queue will not do."""


@dataclass
class QueuedIntervention:
    """One run's request for a human, as the queue sees it.

    Carries the run's own context unchanged plus the queue-side facts the run
    does not know: who picked it up, and who handed it back.
    """

    id: str
    capability: str
    run_id: str
    step: int
    intent: str
    reason: str
    code: str
    # Per intervention rather than per queue: two runs are two containers and
    # therefore two displays, and sending an operator to the wrong one is worse
    # than sending them nowhere.
    vnc_url: str = ""
    frame: str | None = None
    raised_at: float = field(default_factory=time.time)
    claimed_by: str = UNCLAIMED
    claimed_at: float | None = None
    resolved_by: str = UNCLAIMED
    resolved_at: float | None = None
    note: str | None = None

    @property
    def pending(self) -> bool:
        return self.resolved_at is None

    @property
    def claimed(self) -> bool:
        return self.claimed_by != UNCLAIMED

    @property
    def waited_s(self) -> float:
        return round((self.resolved_at or time.time()) - self.raised_at, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "capability": self.capability, "run_id": self.run_id,
            "step": self.step, "intent": self.intent, "reason": self.reason,
            "code": self.code, "vnc_url": self.vnc_url, "frame": self.frame,
            "raised_at": self.raised_at, "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at, "resolved_by": self.resolved_by,
            "resolved_at": self.resolved_at, "note": self.note,
            "pending": self.pending, "waited_s": self.waited_s,
        }


class Queue:
    """Work waiting for a human, across every run that can reach it."""

    def __init__(self, notify: Callable[[QueuedIntervention], None] | None = None) -> None:
        self._items: dict[str, QueuedIntervention] = {}
        self._lock = threading.Lock()
        self._seq = 0
        self._notify = notify

    # ------------------------------------------------------------ the runs

    def add(self, payload: dict[str, Any]) -> QueuedIntervention:
        """A run asks for a human.

        The id is minted here rather than taken from the run: two runs number
        their own interventions from 1 and would collide in a shared list.
        """
        with self._lock:
            self._seq += 1
            item = QueuedIntervention(
                id=f"q-{self._seq:04d}",
                capability=str(payload.get("capability", "?")),
                run_id=str(payload.get("run_id", "?")),
                step=_as_int(payload.get("step", -1)),
                intent=str(payload.get("intent", "")),
                reason=str(payload.get("reason", "")),
                code=str(payload.get("code", "STUCK")),
                vnc_url=str(payload.get("vnc_url", "")),
                frame=payload.get("frame"),
            )
            self._items[item.id] = item

        # Outside the lock on purpose: an outbound call is the one thing here
        # that can block on a network, and holding the queue shut while a
        # webhook times out would stall every other operator.
        if self._notify is not None:
            self._notify(item)
        return item

    def get(self, item_id: str) -> QueuedIntervention | None:
        return self._items.get(item_id)

    # ------------------------------------------------------- the operators

    def claim(self, item_id: str, operator: str) -> QueuedIntervention:
        """Take an intervention. Exclusive: two people, one display."""
        operator = operator.strip()
        if not operator:
            raise QueueError("say who you are before taking a run over")
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                raise QueueError(f"no such intervention: {item_id}")
            if not item.pending:
                raise QueueError(f"{item_id} was already handed back")
            if item.claimed and item.claimed_by != operator:
                raise QueueError(
                    f"{item_id} is already being handled by {item.claimed_by}")
            item.claimed_by = operator
            item.claimed_at = time.time()
            return item

    def resolve(self, item_id: str, operator: str, note: str | None = None
                ) -> QueuedIntervention:
        """Hand control back, on the record.

        Only the claimant may. Not ceremony: the run is about to resume on a
        display someone else is still looking at, and the evidence needs the
        name of the person who decided it was safe to.
        """
        operator = operator.strip()
        if not operator:
            raise QueueError("say who you are before handing control back")
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                raise QueueError(f"no such intervention: {item_id}")
            if not item.pending:
                raise QueueError(f"{item_id} was already handed back")
            if not item.claimed:
                raise QueueError(f"{item_id} has not been taken over yet")
            if item.claimed_by != operator:
                raise QueueError(
                    f"{item_id} is held by {item.claimed_by}, not {operator}")
            item.resolved_by = operator
            item.resolved_at = time.time()
            item.note = note
            return item

    def withdraw(self, item_id: str, note: str | None = None
                 ) -> QueuedIntervention:
        """The run answers its own intervention somewhere else.

        A run that publishes here still serves its own console, so one blocked
        display has two doors. Whichever opens first has to close the other, or
        this list keeps offering a card for a run that stopped waiting minutes
        ago -- and the operator who takes it gets a display nobody needs them
        at.

        Refused once somebody holds it. They are the one at the display, and a
        run does not get to overrule the person it asked for.
        """
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                raise QueueError(f"no such intervention: {item_id}")
            if not item.pending:
                raise QueueError(f"{item_id} was already handed back")
            if item.claimed:
                raise QueueError(
                    f"{item_id} is being handled by {item.claimed_by}")
            # resolved_by stays empty on purpose: no operator did this, and a
            # name invented here would be the one thing the evidence must not
            # carry.
            item.resolved_at = time.time()
            item.note = note
            return item

    # ------------------------------------------------------------- reading

    @property
    def open(self) -> list[QueuedIntervention]:
        """Oldest first: the run that has been blocked longest goes first."""
        with self._lock:
            items = list(self._items.values())
        return sorted((i for i in items if i.pending),
                      key=lambda i: i.raised_at)

    @property
    def all(self) -> list[QueuedIntervention]:
        with self._lock:
            items = list(self._items.values())
        return sorted(items, key=lambda i: i.raised_at)

    def for_run(self, run_id: str) -> list[QueuedIntervention]:
        return [i for i in self.all if i.run_id == run_id]


__all__ = ["Queue", "QueuedIntervention", "QueueError", "UNCLAIMED"]
