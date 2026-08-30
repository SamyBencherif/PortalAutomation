"""Guardrails: where the agent may go, what it may do, what may be written down.

One structural decision shapes this file. Because perception is pixels-only,
the agent has no reliable idea what URL it is on -- it can read an address bar
about as well as a human squinting at a screenshot. So a route allowlist
implemented as "the agent checks before clicking" would be enforcement by good
intentions.

Instead the allowlist is enforced at the **network edge**, by a proxy the
browser is launched behind (see cua/safety/proxy.py). The agent cannot reach a
denied route regardless of what it decides to click, because the request never
leaves the container. This module holds the rules and the decision function;
the proxy is the thing that cannot be talked out of them.

The same split applies to redaction. Rather than trusting every call site to
remember, frames are scrubbed on the way into evidence -- the one place where
regulated data would otherwise become durable.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageDraw

from cua.artifact.schema import Capability, Risk, Step
from cua.perception import ocr


# ---------------------------------------------------------------- allowlist

@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


@dataclass
class Policy:
    """What this run is permitted to touch.

    Deny beats allow. Both lists are path prefixes matched against the request
    path, plus a host allowlist, because a capability that can be pointed at an
    arbitrary host is not a capability, it is a browser.
    """

    allow_hosts: list[str] = field(default_factory=list)
    allow_paths: list[str] = field(default_factory=list)
    deny_paths: list[str] = field(default_factory=list)
    allow_actions: list[str] = field(default_factory=list)
    # Unattended replay of an irreversible step needs this AND an approved
    # artifact. Two independent gates, because either one alone is one
    # accident away from opening real accounts.
    allow_irreversible: bool = False

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        return cls(**json.loads(Path(path).read_text()))

    def check_url(self, url: str) -> Decision:
        parts = urlsplit(url)
        host = parts.hostname or ""
        path = parts.path or "/"

        if self.allow_hosts and host not in self.allow_hosts:
            return Decision(False, f"host {host!r} is not on the allowlist")

        # Deny first and unconditionally. The control plane is the sharpest
        # example of why: mock_teller's /_control can rearm the target's
        # failure profile, and an agent that can reconfigure its own target
        # is not being tested against anything.
        for denied in self.deny_paths:
            if path.startswith(denied):
                return Decision(False, f"path {path!r} matches deny rule {denied!r}")

        if self.allow_paths and not any(path.startswith(p) for p in self.allow_paths):
            return Decision(False, f"path {path!r} is not on the allowlist")

        return Decision(True, "permitted")

    def check_action(self, kind: str) -> Decision:
        if self.allow_actions and kind not in self.allow_actions:
            return Decision(False, f"action {kind!r} is not permitted")
        return Decision(True, "permitted")

    def check_step(self, step: Step, capability: Capability) -> Decision:
        """Gate one step before it runs."""
        verdict = self.check_action(step.action.kind.value)
        if not verdict.allowed:
            return verdict

        if step.risk is Risk.IRREVERSIBLE:
            # Deliberately conservative: an irreversible step needs a human to
            # have approved the flow AND this run to have been explicitly
            # authorised. Failing this is not an error -- it escalates.
            if capability.approval.value != "approved":
                return Decision(
                    False,
                    f"step {step.index} is irreversible and "
                    f"{capability.ref} is {capability.approval.value}, not approved",
                )
            if not self.allow_irreversible:
                return Decision(
                    False,
                    f"step {step.index} is irreversible and this run did not "
                    f"pass --allow-irreversible",
                )
        return Decision(True, "permitted")


DEFAULT_POLICY = Policy(
    allow_hosts=["target", "localhost", "127.0.0.1"],
    allow_paths=["/login", "/logout", "/members", "/frame", "/pb", "/static"],
    # /_control would let the agent rearm its own target; /admin holds a
    # deliberately destructive route that nothing in either flow needs.
    deny_paths=["/_control", "/admin"],
    allow_actions=[
        "navigate", "click", "type", "key", "wait_for", "extract", "accept_dialog",
    ],
    allow_irreversible=False,
)


# ---------------------------------------------------------------- redaction

# Shaped like the real thing so they bite on realistic fixtures. mock_teller
# renders invented SSN- and DOB-shaped values on the detail screen precisely so
# these rules have something to catch.
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

REDACTION_PATTERNS: tuple[re.Pattern[str], ...] = (SSN, DATE)
MASK = "[REDACTED]"


def redact_text(s: str) -> str:
    for pattern in REDACTION_PATTERNS:
        s = pattern.sub(MASK, s)
    return s


@dataclass(frozen=True)
class RedactionReport:
    """What was masked. Recorded so a reviewer can tell withheld from absent.

    Over-redaction is the safe direction for regulated data, but silent
    over-redaction is not: an investigator staring at a blacked-out box needs
    to know the system put it there deliberately.
    """

    regions_masked: int
    values_masked: int


def redact_frame(png: bytes) -> tuple[bytes, RedactionReport]:
    """Black out PII-shaped text in a screenshot before it becomes durable.

    This runs on the way into evidence/, which is the only place a frame is
    written to disk. Doing it at that boundary rather than at every call site
    means a new caller cannot forget.

    The date rule deliberately over-matches: it masks account opening dates as
    well as dates of birth, because from pixels alone the two are
    indistinguishable and the wrong error to make is the one that leaks.
    """
    screen = ocr.read(png)
    img = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(img)

    def mask(b) -> None:
        # Pad slightly: OCR boxes hug the glyphs and can leave a legible
        # sliver of the first and last character.
        draw.rectangle([b.x - 2, b.y - 2, b.right + 2, b.bottom + 2], fill=(0, 0, 0))

    regions = 0
    values = 0
    for block in screen.blocks:
        hits = [w for w in block.words if any(p.search(w.text) for p in REDACTION_PATTERNS)]
        if hits:
            for word in hits:
                mask(word.box)
                regions += 1
            values += len(hits)
            continue

        # Nothing matched word-by-word, but OCR splits tokens unpredictably --
        # an SSN can arrive as "412-55-" + "9080", where neither half matches
        # and the value is still perfectly legible on screen. Re-test the
        # block's text with spacing removed, and if it hides a match, mask the
        # whole block. Over-masking a label is the right way to be wrong here.
        joined = "".join(w.text for w in block.words)
        if any(p.search(joined) for p in REDACTION_PATTERNS):
            mask(block.box)
            regions += 1
            values += 1

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue(), RedactionReport(regions_masked=regions, values_masked=values)


__all__ = [
    "Decision", "Policy", "DEFAULT_POLICY", "redact_text", "redact_frame",
    "RedactionReport", "SSN", "DATE", "MASK",
]
