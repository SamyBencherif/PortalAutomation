# Roadmap

What is built, what is next, and what is deliberately not being built. This is the
durable source; `scripts/create_issues.py` mirrors it into the GitHub tracker so the
two do not drift.

Ordering is by *what it would teach us*, not by effort. The first milestone exists
because every item in it is a claim the system currently makes but has not
demonstrated — and an undemonstrated claim is the most expensive kind of debt in a
project whose whole argument is "we can prove this".

---

## Shipped

The end-to-end loop, evidenced in [`evidence/`](evidence/):

- Target surface (`mock_teller/`) with every runtime condition class in the brief
  reachable by one command, and no randomness anywhere.
- Screenshot-only perception: OCR with a three-tier locator chain, no DOM read anywhere.
- LLM discovery (Opus 5, 9 actions) → recorded artifact (3 steps, all tier-1 anchors) →
  deterministic replay across five conditions, plus three more with a human in the
  middle.
- Four-way result contract: success / business outcome / escalated / failed.
- Allowlist enforced at the network edge by a proxy; pixel redaction at the evidence
  boundary; two-gate approval for irreversible steps.
- Escalation mechanism: control transfer, operator console, live-session handoff,
  a tab-title indicator that tells the operator it is their turn, and a resume
  that continues from the step the human unblocked rather than re-driving the
  run from the top.
- Reaching an operator who is not already watching: a cross-run queue that
  outlives any run, exclusive claiming under a named identity that reaches the
  run's evidence, and a webhook off the machine.
- Three live handoffs in `evidence/`, forced by policy: one resumed, one that
  hands off twice, one nobody answers. The target's own audit log carries both
  actors under a single session id.
- 164 tests.

---

## M1 — Demonstrate what is already claimed

Each of these is supported by code and by the target, and none has been run end to end
under the condition it was designed for. They are first because the gap between "the
mechanism exists and is unit-tested" and "here is a run of it" is exactly the gap a
reviewer is entitled to be sceptical about.

### 1. Discover the write flow (`member.open_subaccount`)
The read flow is discovered and replayed. The write flow is the interesting one: it is
irreversible, has a confirmation step, and is the precondition for almost everything
else in this milestone. Blocks items 2, 3 and 4.

### 2. Run the escalation path end to end
Half done, and the remaining half is the interesting one. The handoff itself is
evidenced — three runs in `evidence/`, a human working the agent's live session over
noVNC, the target's own audit log showing one continuous session with both actors — but
all three are forced by denying the recorded read flow an action it needs. That raises
`POLICY_BLOCKED` *before* a step runs, so resume skips the step on the assumption the
human did it. Member `10005` demands a supervisor override the agent cannot obtain,
which is raised on a screen the step could not get past, so resume re-attempts it
instead: agent stalls → intervention raised with context → human enters `SUP-…` →
control returns → the step runs again and completes. That branch has unit tests and no
live run. Depends on item 1.

### 3. Exercise the native `window.confirm()` dialog
The target raises a real browser dialog on the irreversible commit — deliberately a
*different* problem from the DOM interstitial, since it is not in the page at all. The
engine has an `ACCEPT_DIALOG` recovery; it has never fired against the live surface.
Depends on item 1.

### 4. Replay a duplicate irreversible write
Replaying an irreversible action is this system's normal case, and the target returns
the original confirmation on a duplicate (`E-409-DUPLICATE`). The taxonomy classifies it
as a business outcome — "already done, here is the receipt" — but only in unit tests.
Depends on item 1.

### 5. Replay one artifact against both tenants
The cross-tenant claim is the one most likely to be taken on faith. The two tenants
differ in exactly the ways designed to break a positional strategy: different nouns
(*Member* / *Customer*), different query parameters, different form field order.
`TextAnchor.aliases` exists for this. Replaying the NorthStar artifact against Pinebank
unchanged either works or reveals what else the schema needs.

---

## M2 — Make it trustworthy to run unattended

### 6. Multi-run stability score
Replay N times, report flakiness. The cheapest possible way to earn confidence in an
artifact before letting it run without a human, and a stretch goal the brief names. It
also turns the drift signal from an anecdote into a measurement.

### 7. Gate unattended replay on a confidence score
`approval: draft → approved` exists and is enforced. What is missing is anything
informing the human doing the approving. Feed item 6 into it.

### 8. Run the container as the host UID
Evidence written from inside the container is root-owned on the host, which is a
papercut every single run. Currently worked around with `chown`.

### 9. Make the audit log byte-reproducible
`state.py` stamps entries with `time.time()`, so `GET /_control/state` is not
reproducible even though every rendered page is. `Store(clock=…)` is already injectable
and nothing injects it. Small, and it closes the last hole in the determinism claim.

### 10. Second perception source where one exists
OCR is the system's single point of failure and its most-tuned component — screen
scale, content cropping and banding were all forced by it. Where an accessibility tree
is available it is strictly better; the `Surface` seam is the right place to offer it
as an *additional* signal without weakening the no-DOM guarantee elsewhere.

### 11. Give the operator a way to end a run they cannot fix
The handoff has two exits today: hand control back, or time out. An operator who
takes the display, looks at it and concludes the run should not continue — wrong
member, the branch is closed, the override will not be granted — has no way to
say so. They can only walk away and let `--escalation-timeout` expire, which
takes ten minutes by default, leaves control formally held by the human, and
records the outcome as "nobody came" rather than "a human decided not to".
Those are different facts and the evidence should not conflate them. Wants an
Abandon button beside Resume, a reason, and a distinct terminal status.

---

## M3 — Scale to the real environment

### 12. Per-tenant override layers
`REPORT.md` names this as the highest-value remaining item. A base artifact plus a thin
per-variant patch, so hundreds of tenants running one vendor product do not mean
hundreds of recordings. The schema was shaped for it (`AppRef`, aliases); the
base-plus-patch machinery is unbuilt. Informed by item 5.

### 13. Fleet-wide drift reporting
Replay already reports which tier resolved each target. Aggregated across tenants that
becomes "which capabilities are degrading, where" — the signal that tells you to
re-record *before* something breaks rather than after.

### 14. Expose the catalogue as a callable surface
`cua catalog --json` already emits the contract an agent needs. Making it an actual
tool/function-calling endpoint is the difference between "an agent could invoke this"
and "an agent does".

### 15. Bounded, policy-checked LLM recovery for a single step
Attractive and dangerous, so it is late and tightly scoped: one step, policy-checked,
recorded as evidence. Open-ended recovery would destroy the determinism that is the
entire point.

### 16. A second `Surface` implementation
The abstraction is the argument for heterogeneity; an implementation would be the
proof. A native desktop app is the honest test, since it has no DOM to fall back to.

---

## Known limits, accepted rather than scheduled

Listed so they are not mistaken for oversights:

- **The proxy matches path prefixes, not intent.** It cannot distinguish a safe POST
  from a dangerous one to the same route.
- **Redaction depends on OCR.** A value tesseract misreads is a value that is not
  masked. It is defence in depth, not a guarantee.
- **`--allow-irreversible` is per-run and coarse.** Real deployment wants
  per-capability, per-tenant authorisation with an approver identity.
- **The sandbox is a container, not a VM.** No machine snapshot/restore, no hypervisor
  network fence. `README.md` says what a VM would add.
- **The operator queue is in memory.** A dispatcher restart forgets the work
  list. Runs blocked on it time out the way they would have anyway, but the
  operator loses the list. `Queue` is the seam a persistent one replaces.
- **A typed operator name identifies, it does not authenticate.** It says who
  acted, for the audit trail. Anyone who can reach the queue can claim as
  anyone. Real deployment wants the identity to come from wherever the bank's
  already does.
- **`__VIEWSTATE` round-tripping is absent from the target.** It is the most authentic
  legacy mechanism left unbuilt, and would *prove* replay drives the UI rather than
  forging HTTP requests.
