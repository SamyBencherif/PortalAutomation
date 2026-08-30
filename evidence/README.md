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

Two things the provenance records honestly:

- `recorded_by: llm_discovery`, `model: claude-opus-5` — but the notes also say
  *"one or more turns served by a refusal fallback model"*. Three of the turns
  were served by `claude-opus-4-8` after the safety classifier declined. That
  is recorded rather than glossed, because a capability partly authored by a
  different model is a materially different provenance claim.
- `approval: draft`. A model proposing a flow is not a human accepting one.

**The agent never handles credentials.** Sign-on is a harness precondition
(`cua/bootstrap.py`), performed deterministically before the model is involved.
That started as a response to a refusal and turned out to be the better design
— see `REPORT.md` §6.

## The replay runs

All five replay the **discovered** artifact, with no model in the loop.

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
things no application-level check would see. 124 requests were denied in this
set, and almost none of them came from the automation:

| Denied | What it was |
|---|---|
| 76 | `CONNECT www.google.com:443` — Chromium's own connectivity probing |
| 45 | `GET /favicon.ico` — off the allowlisted paths |
| 3 | `CONNECT` to `optimizationguide-pa.googleapis.com`, `android.clients.google.com` |

The browser phones home constantly, and none of it left the container. No
application-level check would have seen any of these, because no application
code initiated them.


## Reading a target resolution

```json
{"event": "target_resolved", "index": 1, "tier": "label",
 "detail": "text -> 'Find' @ 688,287"}
```

`tier` is the thing to watch. `label` means the robust strategy worked;
`template` means it fell back to matching pixels and the artifact is drifting.
`absolute` is refused outright — a recorded coordinate identifies nothing, and
acting on one produced a false success in testing (see `REPORT.md` §3).

## Not here

The escalation run. It needs the write flow — opening a sub-account, which is
where the supervisor-override stuck state lives — and that capability has not
been discovered yet. The mechanism is tested in `tests_cua/test_escalation.py`;
what is missing is an end-to-end run of it against the live surface.
