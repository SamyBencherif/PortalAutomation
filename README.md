# Computer-Use Automation System

An LLM discovers how to drive a legacy bank back-office screen, the run is
recorded as a typed capability artifact, and that artifact is replayed
deterministically with no model in the loop — which is how an AI agent invokes
it in production.

The target is `mock_teller/`, a deliberately legacy application built for this
project: table layouts, no test IDs, ASP.NET-style control names, balances in
an iframe, and every runtime condition the brief names reachable on demand. See
[`mock_teller/README.md`](mock_teller/README.md) for its condition catalogue.

**Perception is screenshots only.** No DOM is ever read, no selector is ever
queried. That is the brief's "must still work with no clean DOM" taken at face
value, and it forces the central design problem — see
[`REPORT.md`](REPORT.md) §3.

---

## Setup

### What you need

- Docker (for the end-to-end path)
- An `ANTHROPIC_API_KEY` (for the discovery run only)

```bash
cp .env.example .env      # repo root, NOT docker/; .env is gitignored
```

It has to be the repo root. Compose's `${VAR}` interpolation would read `.env`
from the directory holding the compose file (`docker/`), so the service uses an
explicit `env_file: ../.env` instead. Every command except `discover` runs
without a key.

### Run without Docker or a key

Most of the system is testable with neither. The perception layer runs against
a real screenshot fixture, and the replay engine runs against a scripted fake
surface that renders real PNGs and is read with real OCR:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-cua.txt
sudo apt-get install tesseract-ocr      # or: pacman -S tesseract tesseract-data-eng

.venv/bin/python -m pytest tests/ tests_cua/ -q     # 91 automation + 30 target tests
```

`tests/` covers the target application; `tests_cua/` covers the automation.
Neither needs a browser, a container, or a model.

### Run the whole thing

```bash
docker compose -f docker/compose.yaml up --build
```

Two services: `target` is the application on its own host, `workbench` is the
desktop the agent operates. Once up:

| | |
|---|---|
| the application, for your own eyes | <http://localhost:8800> |
| **the agent's live screen** (noVNC) | <http://localhost:6080/vnc.html> |
| operator takeover console | <http://localhost:8080> |

The noVNC view is not a mirror. It is the same X display the automation is
driving, which is what makes the human handoff real rather than staged.

---

## Demo path

All commands run inside the workbench:

```bash
docker compose -f docker/compose.yaml exec workbench bash
```

### 1. Discovery — one real LLM-driven run

```bash
python -m cua.cli discover \
  --goal "Look up member 10001 and read their current savings account balance." \
  --checkpoint "Account Positions" \
  --param member_no=10001 \
  --output 'savings_balance:money:Savings:([0-9,]+\.[0-9]{2})' \
  --save member.read_savings_balance \
  --title "Read a member's savings balance" \
  --reset
```

The model gets screenshots and a mouse, nothing else — no DOM, no selectors, no
hints about the app. It signs on *nothing*: the session is established first by
the harness, because an automation agent should never handle credentials (see
[`cua/bootstrap.py`](cua/bootstrap.py)).

`--param` both parameterises the literal in the recorded steps and declares it
in the contract; `--output` declares what the capability returns. On success
this writes `capabilities/member.read_savings_balance@1.0.0.json` as a **draft**
and prints any step whose targeting could not be validated, for review.

### 2. Replay — the production path, no model

```bash
python -m cua.cli replay member.read_savings_balance@1.0.0 \
  --param member_no=10001 --reset
```

```
[OK] member.read_savings_balance@1.0.0  (3 steps)
  outputs:
    savings_balance = 4,182.55
  evidence: evidence/runs/replay-20260830-173725
```

That balance is inside an iframe. Screenshot perception never notices, because
pixels have no document tree.

### 3. The interesting replays

Each is a different *kind* of result, not a different error message:

```bash
# A legitimate answer, not a failure.
python -m cua.cli replay member.read_savings_balance@1.0.0 --param member_no=99999 --reset
#   [OUTCOME] RECORD_NOT_FOUND

# A permission denial is also an answer.
python -m cua.cli replay member.read_savings_balance@1.0.0 --param member_no=10003 --reset
#   [OUTCOME] PERMISSION_DENIED

# Recoverable: transient 503s and maintenance interstitials, absorbed.
python -m cua.cli replay member.read_savings_balance@1.0.0 \
  --param member_no=10001 --profile flaky --reset
#   [OK] ... recovered: 3x MAINTENANCE_INTERSTITIAL, 2x SERVICE_BUSY

# A hard failure. `broken` faults the commit route, which the READ flow never
# touches, so arm the search route instead:
python -m cua.cli replay member.read_savings_balance@1.0.0 --param member_no=10001 \
  --reset --override '{"error_500_on":["search"]}'
#   [FAILED] SERVER_FAULT at step 2

# Stuck -> a human takes over the live session, then hands it back.
# Needs the write-flow capability, which needs the discovery run first.
python -m cua.cli replay member.open_subaccount@1.0.0 \
  --param member_no=10005 --allow-irreversible --operator-console --reset
#   [ESCALATED] -> open http://localhost:8080, act in the embedded live view,
#   click "Hand control back", and the run resumes on the same session.
```

### 4. The catalogue an agent would call

```bash
python -m cua.cli catalog --json
```

Returns each capability's id, typed parameters, typed outputs and approval
state — the contract a calling agent needs, without reading the step list.

### Approval

Discovery always emits `draft`. Replaying an irreversible step needs both a
human approval and an explicit flag on the run:

```bash
python -m cua.cli approve member.open_subaccount@1.0.0
```

---

## Layout

```
cua/
  surface/      the seam: a Surface is observe() + human-shaped actions
    base.py       protocol — no query_selector, deliberately
    x11.py        Xvfb screenshots via ImageMagick, input via xdotool
  perception/   reading a screen with no DOM
    ocr.py        tesseract --psm 11 -> word boxes grouped into labels
    anchor.py     the three-tier resolution chain
    template.py   bounded normalised cross-correlation, numpy only
  artifact/     schema.py (the contract) + store.py (versioned JSON)
  agent/        loop.py (observe→decide→act) + recorder.py (run -> artifact)
  replay/       engine.py (no model) + outcomes.py (the taxonomy)
  safety/       policy.py (rules, redaction) + proxy.py (enforcement)
  escalation/   broker.py (control transfer) + console.py (operator UI)
  evidence/     run.py — JSONL + redacted frames
  cli.py
docker/         two services: target, workbench
capabilities/   recorded artifacts, one file per version
evidence/       run logs, frames, proxy audit
```

## What is verified, and what is not

- **Verified by test** (91 passing): the artifact schema, all three targeting
  tiers, the full error taxonomy, parameter validation, the irreversible gate,
  redaction, drift reporting, and proxy enforcement driven as real HTTP against
  a real target.
- **Verified end to end in the container** — see [`evidence/`](evidence/). The
  read flow replays against the live app with every control resolved by label
  (tier 1), returns `savings_balance = 4,182.55`, and produces four distinct
  result kinds across five runs: success, two business outcomes, a success that
  absorbed five recoverable faults, and a hard failure at a named step.
- **Verified as a complete loop.** Claude Opus 5, given screenshots and a mouse
  and nothing else, found the flow in 9 actions; the recorder turned that into
  a 3-step artifact with every target a tier-1 label anchor; and that artifact
  replays deterministically with no model. See [`evidence/`](evidence/).
- **Not run: the escalation path end to end.** It needs the write flow, whose
  capability has not been discovered yet. The mechanism is covered by tests.

Three things were only true after measurement, and each changed the code:

- **Screen scale is load-bearing.** The target renders 11px Verdana, which sits
  at tesseract's limit — recognition flips on sub-pixel layout shifts, so the
  same page read cleanly once and as `'Or'` / `'Pas'` the next time. The
  browser runs at `--force-device-scale-factor=1.5` for that reason alone.
- **Whole-frame OCR silently loses text.** Handed a 1600×1000 screenshot,
  tesseract returns the bold panel header and omits "Member Number", "Surname"
  and "Find" — in *every* page-segmentation mode. Cropped to the region holding
  them, all three read at 96% confidence. The discriminator is text density,
  not legibility. `ocr.read` therefore crops to content before reading.
- **Browser chrome is part of the surface.** Chromium's password-save bubble
  covered the results grid's `view` link, so the automation went blind to a
  control it needed.

`REPORT.md` §7 separates what was cut deliberately from what is merely
unfinished.
