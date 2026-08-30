"""Evidence: what happened, why, and what the screen looked like when it did.

Two audiences, and they want different things:

- A human debugging a failed run wants the frame at the moment it broke, and a
  sentence about what was expected versus observed.
- A reviewer auditing the system wants to know that regulated data never became
  durable, and that the guardrails actually fired.

Both are served by the same append-only JSONL plus a frames directory. JSONL
because a run that dies mid-way must still leave a readable log -- a JSON
document that is only valid once closed is useless exactly when you need it.

Every frame goes through redaction on the way in. That is enforced here, at the
single boundary where a screenshot becomes a file, rather than trusted to each
call site: this is the one place regulated data could persist, so it is the one
place worth being paranoid about.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cua.safety.policy import redact_frame, redact_text
from cua.surface.base import Frame


@dataclass
class RunLog:
    """One discovery or replay run's evidence directory."""

    run_id: str
    root: Path
    kind: str = "replay"
    _frame_seq: int = field(default=0, init=False)
    _redactions: int = field(default=0, init=False)

    @classmethod
    def create(cls, kind: str, root: str | Path = "evidence") -> "RunLog":
        # Time-ordered and human-sortable. Deliberately not a UUID: an
        # investigator reading a directory listing should be able to tell which
        # run came first without opening anything.
        run_id = f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}"
        path = Path(root) / "runs" / run_id
        (path / "frames").mkdir(parents=True, exist_ok=True)
        log = cls(run_id=run_id, root=path, kind=kind)
        log.event("run_started", kind=kind, run_id=run_id)
        return log

    # ------------------------------------------------------------- writing

    @property
    def jsonl(self) -> Path:
        return self.root / "run.jsonl"

    def event(self, event_name: str, /, **fields: Any) -> None:
        """Append one structured event.

        The event name is positional-ONLY. Callers naturally want to log fields
        called "kind" and "name", and any named parameter here would collide
        with them at runtime rather than at review time.

        Free text passes through redaction: an OCR'd screen dump can carry an
        SSN into the log just as easily as a screenshot can.
        """
        record = {"t": round(time.time(), 3), "event": event_name}
        for key, value in fields.items():
            record[key] = redact_text(value) if isinstance(value, str) else value
        with self.jsonl.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def frame(self, frame: Frame, label: str) -> Path:
        """Persist a screenshot, redacted, and log that it happened."""
        self._frame_seq += 1
        name = f"{self._frame_seq:03d}-{label}.png"
        path = self.root / "frames" / name

        masked, report = redact_frame(frame.png)
        path.write_bytes(masked)
        self._redactions += report.regions_masked

        self.event(
            "frame_captured",
            file=f"frames/{name}",
            label=label,
            size=[frame.width, frame.height],
            # Recorded so a reviewer can distinguish "withheld" from "absent"
            # when staring at a blacked-out box.
            redacted_regions=report.regions_masked,
        )
        return path

    def result(self, payload: dict[str, Any]) -> Path:
        """The machine-readable verdict, written once at the end."""
        path = self.root / "result.json"
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
        self.event("run_finished", status=payload.get("status"))
        return path

    # ------------------------------------------------------------- reading

    def events(self) -> list[dict[str, Any]]:
        if not self.jsonl.exists():
            return []
        return [json.loads(line) for line in self.jsonl.read_text().splitlines() if line]

    @property
    def redactions(self) -> int:
        return self._redactions


__all__ = ["RunLog"]
