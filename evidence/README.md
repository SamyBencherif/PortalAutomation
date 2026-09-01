# Evidence

Real runs against the containerised target, driven through screenshots and
xdotool with no DOM access anywhere. Each directory holds:

- `run.jsonl` — every decision in order: step, which tier resolved each target,
  recoveries applied, and the final verdict.
- `frames/*.png` — what the screen actually looked like, **redacted**.
- `result.json` — the machine-readable result the caller receives.
- `target_audit.json` — the *application's own* audit log, saved alongside ours
  so a reviewer can check our account against one we did not write.

`proxy.jsonl` is the allowlist audit: every HTTP request the browser made, with
the verdict applied to it. The engine logs what it meant to do; this logs what
the network saw.

## The discovery run

`discovery-20260830-173557` is the LLM-driven run the brief requires. Claude
Opus 5 was given the computer toolset and nothing else — screenshots and a
mouse, no DOM, no selectors, no hints about the app — and found the flow in
**9 actions**, reporting:

> Savings account SAV-10001-01 (Active, opened 2019-04-02): current balance
> 4,182.55 … No error banners or maintenance notices appeared during the
> lookup, and I made no changes to the record.

The recorder then converted that run into
`capabilities/member.read_savings_balance@1.0.0.json`: **three** steps, every
target a tier-1 label anchor, none flagged low-confidence, and the literal
`10001` parameterised to `{member_no}`.

Two things the provenance records:

- `recorded_by: llm_discovery`, `model: claude-opus-5`, and notes reading
  `9 model actions; stop_reason='end_turn'` — with **no** refusal-fallback
  note, because this run needed none. Driving a bank UI sits close enough to
  the `cyber` policy that the classifier declines intermittently: an earlier
  recording of the same goal had three of its turns served by
  `claude-opus-4-8` through the server-side fallback, and its provenance said
  so. The fallback is still enabled; this run simply did not hit it. The
  distinction is recorded rather than assumed, because a capability partly
  authored by a different model is a materially different provenance claim.
- `approval: draft`. A model proposing a flow is not a human accepting one.

**The agent never handles credentials.** Sign-on is a harness precondition
(`cua/bootstrap.py`), performed deterministically before the model is involved.
That started as a response to a refusal and turned out to be the better design
— see `REPORT.md` §6.

## The replay runs

All five below replay the **discovered** artifact, with no model in
the loop.

| Directory | Condition | Result |
|---|---|---|
| `replay-20260830-173725` | member 10001 | `success`, `savings_balance = 4,182.55` |
| `replay-20260830-173755` | member 99999 | `business_outcome` `RECORD_NOT_FOUND` |
| `replay-20260830-173822` | member 10003 | `business_outcome` `PERMISSION_DENIED` |
| `replay-20260830-173850` | `--profile flaky` | `success` after absorbing 2 faults |
| `replay-20260830-173926` | 500 armed on search | `failed` `SERVER_FAULT` at step 2 |

Four different *kinds* of result, which is the point. A caller can tell "the
member does not exist" from "the app is broken" without parsing prose.

The `flaky` run is the most informative: it retried a transient 503 and
dismissed a maintenance interstitial, then returned the correct balance. Both
recoveries are listed in `result.json` rather than hidden — a run that silently
papered over two faults would be worse than one that failed.

## What the frames show

Open `replay-20260830-173725/frames/002-final.png`. It is the member detail
screen, and:

- the SSN and date of birth are **blacked out in pixels**, because this is the
  boundary where regulated data would otherwise become durable;
- the account opening dates are blacked out too. That is the date rule
  over-matching, on purpose: from pixels alone a date of birth and an opening
  date are indistinguishable, and the wrong error to make is the one that
  leaks;
- the balances survive, which is what the capability was asked for.

`redacted_regions` in `run.jsonl` records how many boxes were masked per frame,
so a reviewer staring at a black rectangle can tell *withheld* from *absent*.

## The proxy log

The strongest safety evidence in here, because it shows the allowlist catching
things no application-level check would see. Across every run in this
directory, 737 requests were logged and **557 were denied** — and
almost none of them came from the automation:

| Denied | What it was |
|---|---|
| 430 | `CONNECT` to `www.google.com`, `optimizationguide-pa.googleapis.com`, `accounts.google.com`, `android.clients.google.com`, `safebrowsing.googleapis.com`, `update.googleapis.com` — Chromium phoning home |
| 120 | `GET /favicon.ico` — off the allowlisted paths |
| 4 | `GET http://127.0.0.1:8888/` — the proxy asked about itself |
| 3 | `GET /admin/members/10001/close` — the deny rule, doing its job |

The browser phones home constantly, and none of it left the container. No
application-level check would have seen any of these, because no application
code initiated them.

The last row is the only one the automation caused, and it is the guardrail
demonstration from `MANUAL_TESTING.md` §4: an operator asked for a path
`deny_paths` forbids and the proxy refused it three times, with the reason
recorded as `path '/admin/members/10001/close' matches deny rule '/admin'`.


## Reading a target resolution

```json
{"event": "target_resolved", "index": 1, "tier": "label",
 "detail": "text -> 'Find' @ 688,287"}
```

`tier` is the thing to watch. `label` means the robust strategy worked;
`template` means it fell back to matching pixels and the artifact is drifting.
`absolute` is refused outright — a recorded coordinate identifies nothing, and
acting on one produced a false success in testing (see `REPORT.md` §3).

## The escalation runs

Three more runs of the same read capability, with a human in the middle. The
condition this machinery exists for — member `10005` demanding a supervisor
override on an irreversible write — lives in the write flow, whose capability
has not been discovered yet, so these deny the *recorded* flow an action it
needs instead. Same path through `Policy.check_step` → `POLICY_BLOCKED` →
`Broker`, and the same handoff, queue, resume and evidence.
`MANUAL_TESTING.md` §6 walks it by hand.

| Directory | Condition | Result |
|---|---|---|
| `replay-20260901-001848` | `type` denied; the operator types the member number | `success` in **2 steps**, `resumed_from: [0]` |
| `replay-20260831-105509` | `click` denied; the operator clicks Find and opens the record | `success` in **1 step**, `resumed_from: [1, 2]` — two handoffs in one run |
| `replay-20260831-235620` | `type` denied and nobody comes | `escalated` `POLICY_BLOCKED`, after waiting 120s |

Three things to read in them:

- `intervention_raised` carries `queued_as` — the queue's own id, which differs
  from the run's, because two runs both numbering their interventions from 1
  would collide on a shared list — and `target_ack: true`, meaning the
  application was told control had moved before a human touched it.
- `replay_resumed` carries the operator's note and
  `step_delegated_to_operator: true`: the step was skipped because a human did
  it, not because a gate was lifted. Hence two steps rather than three — for a
  flow that escalates on an irreversible write, that is the difference between
  one resumed step and a second pass over the write.
- `resume_wait_finished` says `via: "queue"`, so control came back through the
  cross-run dispatcher — whoever was on duty — rather than the console of the
  terminal that launched the run. In `235620` the same event says
  `resumed: false`: nobody came, the run gave up, and it says so rather than
  reporting a result it does not have.

Then `target_audit.json`, which this system does not write. From `001848`:

```
1 sess-0001 agent nav     sign on as teller1
2 sess-0001 human control control transferred agent -> human
3 sess-0001 human search  by number 10001
4 sess-0001 agent control control transferred human -> agent
5 sess-0001 agent search  by number 10001
6 sess-0001 agent nav     viewed 10001
```

One session id throughout, with the actor changing partway down: the human
worked in the agent's own session, not a copy of it. That is the handoff as a
fact in someone else's log rather than a claim in ours. Row 3 is the operator's
own search and row 5 is the automation redoing it on resume, which is what
`resumed_from: [0]` means — the *typing* was delegated, the click after it was
not.

## Not here

The stuck state the escalation path was built for. `POLICY_BLOCKED` is raised
*before* a step runs, so resume skips it; a runtime signature like
`SUPERVISOR_OVERRIDE_REQUIRED` is raised on a screen the step could not get past
and so re-attempts it. Both branches are in `ReplayEngine.resume()` and both are
covered in `tests_cua/test_escalation.py`, but only the first is reachable until
the write flow is discovered.
