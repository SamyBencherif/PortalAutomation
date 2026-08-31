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

**Expect:** `153 passed` in about 50 seconds — 30 for the target, 123 for the
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

Three services: `target` is the application on its own host, `workbench` is the
desktop the agent drives, and `dispatcher` is the operator queue — the only one
that outlives a run, which is the whole reason it is separate. First build takes
a few minutes.

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

The fourth kind — `[ESCALATED]`, where the run stops because a human could
finish it and the automation cannot — needs an operator, so it has a section of
its own: **§6**.

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

## 6. An operator intervention

The only path here you cannot check by reading output, because the thing under
test is you. Budget ten minutes and have noVNC open.

**Why it has to be forced.** The condition this machinery exists for is member
`10005`, who demands a supervisor override code on sub-account commit — a code
the agent cannot obtain, so escalating is the only correct move rather than a
fallback. That lives in the *write* flow, whose capability has not been
discovered yet (issues #3 and #4), so there is nothing to replay into it today.

Until there is, deny the recorded read flow an action it needs. Same code path —
`Policy.check_step` → `POLICY_BLOCKED` → `Broker` — and the same handoff, queue,
resume and evidence. The one thing it does not rehearse: `POLICY_BLOCKED` is
raised *before* the step runs, so resume skips that step on the assumption the
human did it, whereas a runtime `STUCK` signature like
`SUPERVISOR_OVERRIDE_REQUIRED` is raised on a screen the step could not get past
and so re-attempts it. Both branches are in `ReplayEngine.resume()`; only the
first is reachable by hand.

### Block an action the flow needs

```bash
docker compose -f docker/compose.yaml exec -T workbench bash -lc \
  'cat > /tmp/operator-demo.policy.json <<JSON
{
  "allow_hosts": ["target", "localhost", "127.0.0.1"],
  "allow_paths": ["/login", "/logout", "/members", "/frame", "/pb", "/static"],
  "deny_paths": ["/_control", "/admin"],
  "allow_actions": ["navigate", "click", "key", "wait_for", "extract", "accept_dialog"],
  "allow_irreversible": false
}
JSON'
```

That is the default policy with `type` removed. Step 0 types the member number
into the search field, so the run gets there and cannot proceed. Sign-on is
unaffected — credentials are a harness precondition (`cua/bootstrap.py`) and
never pass through the policy gate.

### Start the run

```bash
docker compose -f docker/compose.yaml exec workbench \
  python -m cua.cli replay member.read_savings_balance@1.0.0 \
  --param member_no=10001 --reset \
  --policy /tmp/operator-demo.policy.json \
  --operator-console --escalation-timeout 300
```

`--operator-console` serves this run's own console on 8080 and makes the run
*wait*. `CUA_DISPATCHER` is already set in compose, so the same intervention is
also published to the cross-run queue on 8090.

**Expect**, within a few seconds:

```
  ESCALATED -- waiting for a human to take control.
  action 'type' is not permitted
  Take over at http://localhost:8080/
  Queued for an operator at http://dispatcher:8090/
```

**Look at the queue tab's title before anything else.** It should now read
`(1) Take over — Operator queue`, from behind your other windows. That title is
the notification surface; if it does not move, nothing else in this path
matters, because a handoff nobody notices is a run that blocks until it times
out.

### Take it over

On <http://localhost:8090>:

1. Type a name into **You are** and press Set. It is not a credential — it is
   who the evidence will name.
2. Press **Take this over**. Claiming is exclusive: a second operator pressing
   it gets `q-0001 is already being handled by <you>`, because two people must
   not take over the same display.
3. The card embeds the agent's live display. In it, click the **Member Number**
   field and type `10001` — the step the policy denied. This is the agent's
   browser holding the agent's session cookie, not a copy.
4. Fill in **What did you do?** and press **Hand control back**.

The bookkeeping is scriptable, from a second terminal, if you would rather see
the seam than the page — the work still has to happen on the display:

```bash
curl -s http://localhost:8090/state | python3 -m json.tool      # find the item id
curl -X POST http://localhost:8090/claim  -d item_id=q-0001 -d operator=samy
curl -X POST http://localhost:8090/resume -d item_id=q-0001 -d operator=samy \
     -d "note=typed member number 10001 into the search field"
```

**Expect** the run to pick itself up:

```
  control returned by samy; resuming from step 0

[OK] member.read_savings_balance@1.0.0  (2 steps)
  outputs:
    savings_balance = 4,182.55
```

Two steps, not three. The human did step 0 and the automation carried on with
the rest, rather than starting over — which for a flow that escalates on an
irreversible write is the difference between one resumed step and a second pass
over the write.

Hand control back *without* doing the work and the run fails at its checkpoint
instead. Worth doing once: it is what tells you the resume is real rather than
theatre.

### What to read afterwards

```bash
cd evidence/runs/replay-<timestamp>
grep -E 'intervention_raised|replay_resumed' run.jsonl
```

`intervention_raised` carries `queued_as` — the queue's own id, which differs
from the run's, because two runs both numbering from 1 would collide on a shared
list — and `target_ack: true`, meaning the application was told. Then
`replay_resumed` carries the operator's note and
`step_delegated_to_operator: true`, which says the step was skipped because a
human did it, not because a gate was lifted.

Then the application's own log, which this system does not write:

```bash
python3 -c "import json; [print(e['seq'], e['session_id'], e['actor'], e['route'], e['detail']) \
  for e in json.load(open('target_audit.json'))]"
```

```
1 sess-0001 agent nav     sign on as teller1
2 sess-0001 human control control transferred agent -> human
3 sess-0001 agent control control transferred human -> agent
4 sess-0001 agent search  by number 10001
5 sess-0001 agent nav     viewed 10001
```

One session id throughout, with the actor changing partway down. That is "the
human operated the live session" as a fact in someone else's log rather than a
claim in ours.

### Variants worth walking

**Put the human's own work in the app's audit.** Typing never reaches the
server, so above the operator appears only in the handoff rows. Drop `"click"`
from `allow_actions` instead of `"type"` and they click Find and view
themselves:

```
2 sess-0001 human control control transferred agent -> human
3 sess-0001 human search  by number 10001
4 sess-0001 agent control control transferred human -> agent
```

That costs two interventions — steps 1 and 2 are both clicks — which is also the
cheapest way to watch one run hand off twice and to feel `--max-handoffs`
(default 2).

**Nobody comes.** Same run with `--escalation-timeout 45`, and just leave it.

```
  nobody took over within the timeout
[ESCALATED] member.read_savings_balance@1.0.0  (0 steps)
  outcome: POLICY_BLOCKED -- action 'type' is not permitted
```

Exit status 1; only `success` and `business_outcome` exit 0. Note the queue item
stays *open* after the run has given up, because the queue is not told that the
run stopped waiting. Hand it back before the next walk or you will be looking at
a stale card.

**The unattended shape.** Drop `--operator-console` and keep the queue. The run
still waits, but the only way to reach it is 8090 — which is the deployment this
is aimed at: nobody is watching the terminal that launched it. Set
`CUA_NOTIFY_WEBHOOK` in the repo-root `.env` and raising an intervention also
POSTs off the machine, which is the difference between an unattended replay and
an unwatched one.

**Losing the queue mid-escalation.**

```bash
docker compose -f docker/compose.yaml restart dispatcher
```

Its work list comes back empty — in memory on purpose — and the blocked run
polls for an intervention that no longer exists, gets a 404, and times out the
way it would have anyway. Losing the queue must not wedge a run. If you started
with `--operator-console`, that route still answers after the restart: the
broker checks its own resume event before each poll, and whichever route fires
first wins.

---

## 7. Discovery — needs an API key

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

**Evidence or capability files written by a run never appear on the host, and
the container insists they exist.** A git operation — `rebase`, `reset --hard`,
a branch switch — deleted and recreated a bind-mounted directory on the host.
Docker binds by **inode, not path**, so the container keeps reading and writing
the old directory while the host looks at a new, empty one. Nothing errors.
Both sides are internally consistent and silently disagree, which is why this is
worth knowing about before it wastes an afternoon.

Verified: renaming the host directory is harmless — the mount follows the inode
and the container carries on with the same data. Deleting and recreating it is
what splits them.

```bash
docker compose -f docker/compose.yaml up -d --force-recreate workbench
```

To check rather than guess:

```bash
stat -c '%i' evidence
docker compose -f docker/compose.yaml exec workbench stat -c '%i' /app/evidence
```

Matching inodes mean the mount is healthy.

**Everything through the proxy returns an empty reply, or the browser cannot
load anything.** Seen once, cause not identified — the proxy was dying while
writing its audit log, before sending any response. It no longer can: an
unwritable audit log is reported once and policy keeps being enforced. If it
recurs, `docker compose logs workbench` will show what the audit write is
failing on, and that is worth reporting rather than working around.

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
