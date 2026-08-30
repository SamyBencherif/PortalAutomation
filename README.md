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
cp .env.example .env      # then put your key in it; .env is gitignored
```

### Run without Docker or a key

Most of the system is testable with neither. The perception layer runs against
a real screenshot fixture, and the replay engine runs against a scripted fake
surface that renders real PNGs and is read with real OCR:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-cua.txt
sudo apt-get install tesseract-ocr      # or: pacman -S tesseract tesseract-data-eng

.venv/bin/python -m pytest tests/ tests_cua/ -q     # 108 tests, ~19s
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
  --goal "Sign on as teller1, look up member 10001, and read their savings balance" \
  --checkpoint "Account Positions" \
  --param member_no=10001 \
  --save member.read_savings_balance \
  --title "Read a member's savings balance" \
  --reset
```

The model gets screenshots and a mouse, nothing else. On success this writes
`capabilities/member.read_savings_balance@1.0.0.json` and prints any steps
whose targeting could not be validated, for review before approval.

### 2. Replay — the production path, no model

```bash
python -m cua.cli replay member.read_savings_balance@1.0.0 \
  --param member_no=10001 --reset
```

```
[OK] member.read_savings_balance@1.0.0  (5 steps)
  outputs:
    savings_balance = 4,182.55
  evidence: evidence/runs/replay-…
```

### 3. The interesting replays

Each is a different *kind* of result, not a different error message:

```bash
# A legitimate answer, not a failure.
python -m cua.cli replay member.read_savings_balance@1.0.0 --param member_no=99999 --reset
#   [OUTCOME] RECORD_NOT_FOUND

# A permission denial is also an answer.
python -m cua.cli replay member.read_savings_balance@1.0.0 --param member_no=10003 --reset

# Recoverable: a transient 503 and a maintenance interstitial, absorbed.
python -m cua.cli replay member.read_savings_balance@1.0.0 \
  --param member_no=10001 --profile flaky --reset

# A hard failure, with the correlation id needed to debug it.
python -m cua.cli replay member.read_savings_balance@1.0.0 \
  --param member_no=10001 --profile broken --reset
#   [FAILED] SERVER_FAULT

# Stuck -> a human takes over the live session, then hands it back.
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

Honesty about the state of it:

- **Verified by test** (108 passing): the artifact schema, all three targeting
  tiers, the full error taxonomy, parameter validation, the irreversible gate,
  redaction, drift reporting, and proxy enforcement against a real target.
- **Verified by measurement**: tesseract needs `--psm 11` on this surface — at
  1280×800 it recovers every anchor label, while `--psm 6` on the same image
  reads "Operator 1D" and "credentils". The perception tests run against a real
  screenshot for that reason.
- **Written but not yet exercised**: the X11 surface and the container. They
  need a running Docker daemon, which this machine did not have.
- **Not yet run**: the discovery run itself, which needs an API key.

`REPORT.md` §7 lists what was cut deliberately, as opposed to what is merely
unfinished.
