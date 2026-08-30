# Evidence

Real runs against the containerised target, driven through screenshots and
xdotool with no DOM access anywhere. Each directory holds:

- `run.jsonl` — every decision, in order: step, target resolution and which
  tier resolved it, recoveries applied, and the final verdict.
- `frames/*.png` — what the screen actually looked like, **redacted**.
- `result.json` — the machine-readable result the caller receives.
- `target_audit.json` — the *application's own* audit log, saved alongside ours
  so a reviewer can check our account against one we did not write.

`proxy.jsonl` is the allowlist audit: every HTTP request the browser made, with
the verdict applied to it. The engine logs what it meant to do; this logs what
the network saw.

## The runs

| Directory | Condition | Result |
|---|---|---|
| `replay-20260830-155559` | member 10001, clean | `success`, `savings_balance = 4,182.55` |
| `replay-20260830-155639` | member 99999 | `business_outcome` `RECORD_NOT_FOUND` |
| `replay-20260830-155354` | member 10003 | `business_outcome` `PERMISSION_DENIED` |
| `replay-20260830-155920` | member 10001, `--profile flaky` | `success` after absorbing 5 conditions |
| `replay-20260830-160052` | 500 armed on the search route | `failed` `SERVER_FAULT`, at a named step |

Four different *kinds* of result, which is the point. A caller can tell "the
member does not exist" from "the app is broken" without parsing prose.

The `flaky` run is the most informative: it dismissed three maintenance
interstitials and retried two transient 503s, then returned the correct
balance. Those recoveries are listed in `result.json` rather than hidden — a
run that silently papered over five faults would be worse than one that failed.

## What the frames show

`replay-20260830-155920/frames/005-final.png` is worth opening. It is the
member detail screen, and:

- the SSN and date of birth are **blacked out in pixels**, because this is the
  boundary where regulated data would otherwise become durable;
- the account opening dates are blacked out too. That is the date rule
  over-matching, on purpose: from pixels alone a date of birth and an opening
  date are indistinguishable, and the wrong error to make is the one that
  leaks;
- the balances survive, which is what the capability was asked for.

`redacted_regions` in `run.jsonl` records how many boxes were masked per frame,
so a reviewer staring at a black rectangle can tell *withheld* from *absent*.

## Reading a target resolution

```json
{"event": "target_resolved", "index": 4, "tier": "label",
 "detail": "text -> 'Member Number' @ 53,282"}
```

`tier` is the thing to watch. `label` means the robust strategy worked. A run
whose steps start resolving via `template` or `absolute` still passes, but it
has quietly stopped being robust and the artifact should be re-recorded — those
steps are also surfaced as `drift` in `result.json`.

## Not here yet

**The LLM discovery run.** The brief requires one, and it needs an API key that
was not available. The capability replayed above
(`capabilities/member.read_savings_balance@1.0.0.json`) is marked
`recorded_by: "human"` in its provenance and was hand-authored against measured
frames, precisely so the replay engine could be exercised without the model.
It is not a substitute for the discovery run and is not presented as one.

Also absent: the escalation run, which needs the write flow and therefore the
same discovery step first.

## A wart

The container runs as root, so files it writes here are root-owned on the host.
`docker compose exec workbench chown -R 1000:1000 /app/evidence` fixes it after
a run. Running the container as the host UID would be the real fix.
