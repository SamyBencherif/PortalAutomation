"""Per-session state and the audit log.

The audit log is not decoration. The assignment requires that a human operator
take control of *the same live session* the automation was using, and that the
handoff be evidenced rather than asserted. Because every entry here is keyed by
session id and stamped with an actor, the automation's evidence bundle can show
agent actions and human actions interleaved on one session id -- which is proof
of continuity, not a claim of it.

Time is injectable so tests can expire a session without sleeping.
"""

import itertools
import time
from dataclasses import dataclass, field

from .scenarios import DEFAULT_PROFILE

ACTOR_AGENT = "agent"
ACTOR_HUMAN = "human"
ACTOR_SYSTEM = "system"


@dataclass
class AuditEntry:
    seq: int
    session_id: str
    actor: str
    route: str
    path: str
    detail: str
    at: float

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "session_id": self.session_id,
            "actor": self.actor,
            "route": self.route,
            "path": self.path,
            "detail": self.detail,
            "at": round(self.at, 3),
        }


@dataclass
class Session:
    sid: str
    user: str | None = None
    created_at: float = 0.0
    last_seen: float = 0.0
    profile: str = DEFAULT_PROFILE
    overrides: dict = field(default_factory=dict)
    # Who is currently driving. Flipped by the control plane during a handoff so
    # the app -- and the evidence -- always know who is in control.
    actor: str = ACTOR_AGENT
    # Per-route request counts, which is what makes "fail the first N times"
    # and "modal every Nth view" deterministic instead of time-based.
    route_hits: dict[str, int] = field(default_factory=dict)
    page_views: int = 0
    # Sub-account draft parked between the form and the confirm screen.
    draft: dict | None = None

    def bump(self, route: str) -> int:
        self.route_hits[route] = self.route_hits.get(route, 0) + 1
        return self.route_hits[route]


class Store:
    """All mutable server state, in one resettable place."""

    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self._sessions: dict[str, Session] = {}
        self._audit: list[AuditEntry] = []
        self._ids = itertools.count(1)
        self._audit_seq = itertools.count(1)

    def reset(self) -> None:
        self._sessions.clear()
        self._audit.clear()
        self._ids = itertools.count(1)
        self._audit_seq = itertools.count(1)

    def new_session(self) -> Session:
        # Sequential, not random: two identical runs across a reset produce the
        # same session ids, so evidence bundles diff cleanly.
        sid = f"sess-{next(self._ids):04d}"
        now = self.clock()
        s = Session(sid=sid, created_at=now, last_seen=now)
        self._sessions[sid] = s
        return s

    def get(self, sid: str | None) -> Session | None:
        return self._sessions.get(sid) if sid else None

    def is_expired(self, s: Session, ttl_s: int) -> bool:
        return (self.clock() - s.last_seen) > ttl_s

    def touch(self, s: Session) -> None:
        s.last_seen = self.clock()

    def drop(self, sid: str) -> None:
        self._sessions.pop(sid, None)

    def audit(self, session_id: str, actor: str, route: str, path: str, detail: str = "") -> None:
        self._audit.append(
            AuditEntry(next(self._audit_seq), session_id, actor, route, path, detail, self.clock())
        )

    def audit_log(self, session_id: str | None = None) -> list[dict]:
        rows = self._audit
        if session_id:
            rows = [e for e in rows if e.session_id == session_id]
        return [e.as_dict() for e in rows]

    def sessions(self) -> list[dict]:
        return [
            {
                "sid": s.sid,
                "user": s.user,
                "actor": s.actor,
                "profile": s.profile,
                "overrides": s.overrides,
                "page_views": s.page_views,
                "route_hits": dict(s.route_hits),
                "has_draft": s.draft is not None,
            }
            for s in self._sessions.values()
        ]
