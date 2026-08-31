"""The outbound channel: telling someone who is not already looking.

Everything else in the escalation path is pull. The tab title reaches an
operator who has the console open; the queue reaches one who thinks to check
it. Neither reaches the person actually on duty at 3am, and a replay that
escalates to nobody is not unattended -- it is unwatched, which is the failure
this module exists to close.

Deliberately a webhook and nothing else. Slack, PagerDuty, email and SMS all
sit behind one somewhere, and picking one of them here would buy a vendor SDK,
a credential to store and a mock to maintain in exchange for nothing the brief
rewards. A URL is the honest seam.

Best-effort, like the target handoff announcement: a notifier that raises would
take down the raise it was reporting on, which is the worst possible trade. A
failure to notify is logged and the intervention still lands on the queue.
"""

from __future__ import annotations

import os
from typing import Any, Callable

import httpx

from cua.escalation.queue import QueuedIntervention


class Webhook:
    """POSTs one JSON body per raised intervention."""

    def __init__(
        self,
        url: str,
        console_url: str = "",
        timeout: float = 5.0,
        log: Any = None,
        post: Callable[..., Any] | None = None,
    ) -> None:
        self.url = url
        self.console_url = console_url.rstrip("/")
        self.timeout = timeout
        self.log = log
        self._post = post or httpx.post

    def __call__(self, item: QueuedIntervention) -> bool:
        """Announce it. Returns whether the far end took it."""
        try:
            r = self._post(self.url, json=self.payload(item),
                           headers={"content-type": "application/json"},
                           timeout=self.timeout)
            ok = 200 <= r.status_code < 300
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            ok, r = False, None
            self._log("notify_failed", id=item.id, error=str(e))
        if r is not None and not ok:
            self._log("notify_rejected", id=item.id, status=r.status_code)
        elif ok:
            self._log("notify_sent", id=item.id, url=self.url)
        return ok

    def payload(self, item: QueuedIntervention) -> dict[str, Any]:
        """What the far end needs to act, not what we happen to have.

        A message reading "run replay-x is stuck" makes the reader go and find
        out which run, what it wanted and where to do something about it. The
        reason and the link are the whole value.
        """
        return {
            "text": (f"{item.capability} needs an operator: {item.reason}"),
            "intervention": item.id,
            "run_id": item.run_id,
            "capability": item.capability,
            "code": item.code,
            "step": item.step,
            "intent": item.intent,
            "reason": item.reason,
            # Where to go and do something about it. Without this the message
            # is a notification that something is wrong somewhere.
            "console_url": f"{self.console_url}/#{item.id}" if self.console_url else "",
            "vnc_url": item.vnc_url,
            "raised_at": item.raised_at,
        }

    def _log(self, event: str, **fields: Any) -> None:
        if self.log is not None:
            self.log.event(event, **fields)


def from_env(console_url: str = "", log: Any = None) -> Webhook | None:
    """Build a notifier if one is configured, else None.

    Absent configuration means no channel rather than a broken one: the queue
    is still served and still works, it just does not reach off the machine.
    """
    url = os.environ.get("CUA_NOTIFY_WEBHOOK", "").strip()
    return Webhook(url, console_url=console_url, log=log) if url else None


__all__ = ["Webhook", "from_env"]
