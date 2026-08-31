# Manual testing

How to satisfy yourself that this system does what the README says, by hand.

Every command here has been run. Where something is counter-intuitive — and one
guardrail check genuinely is — it is called out rather than left to trip you up.

Roughly ordered by cost: the first section needs nothing but Python, the last
needs an API key.

---

## 0. Without Docker or an API key

Most of the system is checkable with neither.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-cua.txt
sudo pacman -S tesseract tesseract-data-eng   # or: apt-get install tesseract-ocr

.venv/bin/python -m pytest tests/ tests_cua/ -q
```

**Expect:** `121 passed` in about 40 seconds — 30 for the target, 91 for the
automation.

These are not mocked-through tests. The perception suite runs against a real
1280×800 screenshot of the real app, and the replay suite drives a fake surface
that renders real PNGs which are then read by real OCR. Only the browser is
substituted.

Worth running individually if you want to see the shape of the argument:

```bash
.venv/bin/python -m pytest tests_cua/test_outcomes_and_safety.py -v   # the taxonomy
.venv/bin/python -m pytest tests_cua/test_proxy.py -v                 # real HTTP
```

---

## 1. Bring the stack up

```bash
docker compose -f docker/compose.yaml up --build -d
```

Two services: `target` is the application on its own host, `workbench` is the
desktop the agent drives. First build takes a few minutes.

**Look at the target yourself first** — <http://localhost:8800>, any operator id
and any password of four or more characters. This is the thing being automated:
nested tables, `ctl00_cph1_*` identifiers, no test IDs, and the savings balance
in an iframe rather than the top-level document. Worth thirty seconds, because
everything after this is about reading *that* without a DOM.

The scenario control panel is at <http://localhost:8800/_control>. The agent is
forbidden from reaching it; you are not.

---

## 2. Watch the agent work

**This is the one to do.** Open <http://localhost:6080/vnc.html> and leave it
visible on screen. Then, in a terminal:

```bash
docker compose -f docker/compose.yaml exec workbench \
  python -m cua.cli replay member.read_savings_balance@1.0.0 \
  --param member_no=10001 --reset
```

**Expect:**

```
[OK] member.read_savings_balance@1.0.0  (3 steps)
  outputs:
    savings_balance = 4,182.55
```

**What to watch for.** The pointer moves, a field fills, pages change. That is
not a recording being played back at fixed coordinates — each step is OCR
locating the words *Member Number*, then *Find*, then *view* on that frame and
computing where to click. And the balance it returns is inside an iframe, which
screenshot perception never notices, because pixels have no document tree.

The noVNC view is not a mirror. It is the same X display the automation is
driving — which is also what makes the human handoff real rather than staged.

---

## 3. The four kinds of result

The point of the taxonomy is that these are *different*, not four spellings of
"error". Same command, different member:

```bash
CAP=member.read_savings_balance@1.0.0
EXEC="docker compose -f docker/compose.yaml exec workbench python -m cua.cli replay $CAP"

# A legitimate answer. The search worked; there is no such member.
$EXEC --param member_no=99999 --reset
#   [OUTCOME] RECORD_NOT_FOUND

# Also an answer, not a crash.
$EXEC --param member_no=10003 --reset
#   [OUTCOME] PERMISSION_DENIED

# Faults injected, absorbed, and reported rather than hidden.
$EXEC --param member_no=10001 --reset --profile flaky
#   [OK] ... recovered: SERVICE_BUSY via retry_after
#                       MAINTENANCE_INTERSTITIAL via dismiss_interstitial

# Genuinely broken. Stops at a named step.
$EXEC --param member_no=10001 --reset --override '{"error_500_on":["search"]}'
#   [FAILED] SERVER_FAULT
```

Watch the third one on noVNC: the maintenance dialog appears over the page and
gets dismissed. A caller can tell "no such member" from "the app is down"
without parsing prose, which is the whole point.

---

## 4. The guardrail — read before testing

**The obvious test gives the wrong answer.** From your own browser:

```bash
curl -o /dev/null -w '%{http_code}\n' http://localhost:8800/admin/members/10001/close
#   200
```

That is expected and not a failure. The proxy guards the **agent's** browser,
not the target. Your host reaches the target directly and is not subject to it.

Test it where it applies — inside the workbench, through the proxy:

```bash
docker compose -f docker/compose.yaml exec workbench sh -c \
  'curl -s -x http://127.0.0.1:8888 -o /dev/null -w "denied : %{http_code}\n" \
     http://target:8800/admin/members/10001/close;
   curl -s -x http://127.0.0.1:8888 -o /dev/null -w "allowed: %{http_code}\n" \
     http://target:8800/login'
```

**Expect:** `denied : 403` and `allowed: 200`.

Or navigate the container's own browser there over noVNC and read the refusal
page, which names the rule that stopped it.

See what else it caught:

```bash
grep '"allowed": false' evidence/proxy.jsonl | head
```

Mostly Chromium's own telemetry — `CONNECT www.google.com:443` and friends.
Nothing in the application initiated those, so no in-process check would have
seen them.

**The honest limit:** anyone with network access to the target can still reach
`/admin`. This bounds what the *automation* can do. A real deployment fences the
target as well.

---

## 5. Read the evidence

```bash
cat evidence/runs/replay-*/result.json | head -40
```

Then open a frame:

```bash
xdg-open evidence/runs/replay-20260830-173725/frames/002-final.png
```

**What to look for:** the SSN and date of birth are blacked out *in pixels* —
redaction happens at the boundary where a screenshot becomes a file. The account
opening dates are masked too, which is the date rule over-matching on purpose:
from pixels alone a date of birth and an opening date are indistinguishable, and
the wrong error to make is the one that leaks. The balances survive, because
that is what the capability was asked for.

The run log records how many regions were masked per frame, so a black box is
distinguishable from missing data:

```bash
grep redacted_regions evidence/runs/replay-20260830-173725/run.jsonl
```

Watch the targeting tiers too:

```bash
grep target_resolved evidence/runs/replay-*/run.jsonl
```

`"tier": "label"` means the robust strategy worked. `template` means it fell
back to matching pixels and the artifact is drifting. `absolute` never appears,
because replay refuses to act on a bare coordinate.

---

## 6. Discovery — needs an API key

```bash
cp .env.example .env          # REPO ROOT, not docker/. Add ANTHROPIC_API_KEY.
docker compose -f docker/compose.yaml up -d

docker compose -f docker/compose.yaml exec workbench \
  python -m cua.cli discover \
    --goal "Look up member 10001 and read their current savings account balance." \
    --checkpoint "Account Positions" \
    --param member_no=10001 \
    --output 'savings_balance:money:Savings:([0-9,]+\.[0-9]{2})' \
    --save member.read_savings_balance \
    --reset --overwrite
```

Watch noVNC. The model gets screenshots and a mouse and nothing else — no DOM,
no selectors, no hints about the app. Expect roughly 9 actions over a couple of
minutes, ending with it stating the balance.

Then look at what the recorder made of it:

```bash
cat capabilities/member.read_savings_balance@1.0.0.json | python3 -m json.tool | head -40
```

Three steps, each target a label anchor rather than a coordinate. Then replay
what it just recorded — section 2 again. That round trip is the whole system.

**Note:** the classifier declines this kind of goal intermittently, since driving
a bank UI sits near the `cyber` policy. The server-side refusal fallback is
enabled, so a declined turn is served by another model rather than killing the
run; when that happens the artifact's provenance records it. If a run stops with
`[stopped] ... refusal`, re-running usually succeeds.

---

## Troubleshooting

**Everything through the proxy returns an empty reply, or the browser cannot
load anything.** A git operation on the host — `rebase`, `reset --hard`, a
branch switch — replaced a bind-mounted directory, and the container is still
pointed at the deleted inode. The symptom surfaces somewhere unrelated, possibly
hours later.

```bash
docker compose -f docker/compose.yaml up -d --force-recreate workbench
```

**Evidence files are owned by root.** The container runs as root, so files it
writes are root-owned on the host. Tracked as issue #10.

```bash
docker compose -f docker/compose.yaml exec workbench chown -R 1000:1000 /app/evidence
```

**Sign-on fails with "signed on but did not reach 'Member Search'".** A previous
run left a fault armed. Reset the target:

```bash
docker compose -f docker/compose.yaml exec workbench python -c \
  "import httpx; httpx.post('http://target:8800/_control/reset', json={}, \
   headers={'content-type':'application/json'}, timeout=5)"
```

**A replay fails with "refusing to click blind".** Working as intended: no
control could be found by label or by patch, and acting on the recorded
coordinate would be clicking at a position with no evidence the right screen is
even present. Usually means the browser is not where the capability expects to
start. Re-run with `--reset`.
