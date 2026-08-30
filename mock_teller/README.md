# Mock Teller

A deliberately legacy-looking bank back-office app. It is the **target surface**
for the computer-use automation system — the stand-in for the core banking
screens the assignment says we will not be given access to.

It is a test fixture, not a product. Everything here exists so the automation
side has something real to observe, fail against, and escalate from.

## Why it was built first

The automation project is bounded by how many *interesting* conditions its
target can produce on demand. A surface that only does the happy path makes the
error taxonomy, the escalation path, and the guardrail model untestable — you
end up asserting them in prose instead of exercising them. So the mock's job is
to make every condition class reachable by a single documented command.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn mock_teller.app:app --port 8800
.venv/bin/python -m pytest tests/ -q          # 30 tests, ~1.3s
```

Then open <http://localhost:8800/> (redirects to sign-on; any operator ID and
any password of 4+ characters). The control panel is at
<http://localhost:8800/_control>.

## The two flows

**Read flow** — the "look up member 12345 and read their savings balance" goal:

```
/login  →  /members?memberNumber=10001  →  /members/10001  →  iframe /frame/members/10001
```

The balance is **not** in the top-level document. It lives in the iframe, so
anything that only reads the outer DOM finds an empty box.

**Write flow** — the risky, irreversible action, with a confirmation step:

```
/members/10001/subaccounts/new  →  (review screen)  →  POST .../subaccounts/confirm
```

## Condition catalogue

Every row is reachable with one command. This is the contract the automation
codes against.

### Expected business outcomes — always on, no setup

These are *results the caller needs*, not failures. Conflating the two is the
mistake the assignment calls out by name.

| Condition | Trigger | Wire |
|---|---|---|
| Record not found | `?memberNumber=99999` | 200, "No member record was found" |
| Account frozen | member `10002` | 200, `Frozen` + advisory note |
| Permission denied | member `10003` | 403, `E-403-PROFILE` |
| Sub-account limit reached | member `10004` | 409, `E-409-MAXSUB` |
| Ambiguous search | `?lastName=Lee` | 200, three rows + "More than one member matched" |
| Field validation | empty nickname, deposit `abc` or `5` | 200, form redisplayed with the message |
| Duplicate commit | re-run the write flow without a reset | 409, `E-409-DUPLICATE` + the *original* confirmation |

**Why duplicate commit is a business outcome, not a failure.** A replay re-runs
`GET form → POST new → POST confirm`, which stages a fresh draft and therefore
sails straight past the `E-440-NODRAFT` guard — without a dedupe check it opens
a *second real account*. Replaying an irreversible write is the normal case for
this system, so it gets a result of its own: the caller is told the work is
already done and is handed back the original account number and confirmation,
which is the idempotent answer a replay engine needs. `E-440-NODRAFT` still
means what it always meant — the draft was genuinely lost.

### Recoverable conditions

| Condition | Trigger |
|---|---|
| Transient 503 + `Retry-After: 1` | profile `flaky`, or `?_inject=transient_503` |
| Maintenance interstitial (every Nth view) | profile `flaky`, or `?_inject=interstitial` |
| Deferred load — spinner, then real data | profile `slow`, or `?_inject=spinner` |
| Latency (2–5s) | profile `slow`, or `?_inject=slow` |
| Native `confirm()` on commit | profile `flaky`, or `?_inject=native_confirm` |

### Hard failures

| Condition | Trigger |
|---|---|
| 500 with correlation id | profile `broken`, or `?_inject=error_500` |
| Session expiry mid-flow → bounce to login | profile `expired`, or `?_inject=expire` |
| Route never responds | profile `hanging`, or `?_inject=hang` |

### Stuck → escalate

Member `10005`, or profile `locked_down`, demands a **supervisor override
code** on commit. The agent has no way to obtain one, so the only correct
behaviour is to escalate. A human supplies a code starting `SUP-` on the *same
session* and the flow completes.

## How variants are selected

Three layers, highest precedence first. Nothing is random — every variant is a
pure function of `(profile, knobs, route, per-session request counter)`.
Randomness would make deterministic replay unfalsifiable: a passing replay
would tell you nothing, because a rerun might roll different dice.

1. **Per-request injection** — `?_inject=error_500` or an `X-Mock-Inject`
   header. One shot, changes no state. This is what the tests use.
2. **Session scenario** — `POST /_control/scenario {"profile": "flaky"}`, bound
   to the session cookie.
3. **Fixture-encoded outcome** — always on, needs no setup.

All injection happens in one function, `gate()` in `app.py`. Its ordering is
deliberate — hang, hard failure, expiry, transient failure, latency — so that
cheap-to-detect conditions never mask more serious ones.

> **Two ordering gotchas, both real:**
>
> - The transient-503 counter starts when the profile is armed, and sign-on
>   redirects onto the search page. Arm `flaky` *after* signing on, or the
>   sign-on redirect silently consumes the one 503.
> - `?_inject=expire` really does drop the session, so every request after it
>   redirects to login until you sign on again. Injections change no *scenario*
>   state, but an expired session is a genuine consequence, not a leak.

## Control plane — `/_control`

Out-of-band, and **excluded from the automation allowlist by design**: an agent
that can reconfigure its own target is not being tested against anything.

| Endpoint | Purpose |
|---|---|
| `GET /_control` | HTML panel: pick a profile, reset, transfer control |
| `POST /_control/scenario` | `{"profile", "overrides"}` — JSON or form-encoded |
| `POST /_control/reset` | Total reset. **Run between replay runs.** |
| `POST /_control/handoff` | `{"sid", "actor"}` — flip control agent ↔ human |
| `GET /_control/state` | Machine-readable state + audit log |

`/admin/{collection}/{id}/close` is a deliberately dangerous, deliberately
unnecessary route. Nothing in the demo flows needs it; it exists so the
allowlist has a real irreversible action to refuse.

## Handoff evidence

`POST /_control/handoff` does not create a session, swap cookies, or fork
state — it flips the actor on the session already running. Every subsequent
audit entry carries the new actor, so a single continuous log *proves* the
human operated the same live session rather than asserting it:

```
 1 sess-0001  agent  nav     sign on as teller1
 2 sess-0001  agent  submit  draft staged
 3 sess-0001  agent  submit  committed SAV-10001-03 (CNF-000001)
 4 sess-0001  human  control control transferred agent -> human
 5 sess-0001  human  nav     viewed 10005
 6 sess-0001  agent  control control transferred human -> agent
```

## Determinism

Two identical runs separated by `POST /_control/reset` produce byte-identical
output — same session ids (`sess-0001`), same confirmation numbers
(`CNF-000001`), same account numbers. Session ids and confirmations are
sequential counters, not UUIDs or timestamps, and the clock is fixed at
`2026-03-02`. Asserted by `test_two_runs_across_a_reset_are_byte_identical`.

The 500 page's correlation id is a `zlib.crc32` of the path, **not** Python's
`hash()` — string hashing is salted per process, so a hash-derived id would
change on every server restart and quietly break the claim above.

One known gap: audit entries stamp `time.time()` (`state.py`), so the JSON from
`GET /_control/state` is *not* byte-reproducible even though every page is.
`Store(clock=…)` is already injectable; nothing injects it yet. No current claim
depends on the audit log being reproducible, so this is flagged, not fixed.

## What makes it hostile

Chosen to break naive automation without being unusable:

- Nested `<table>` layout; **no test IDs anywhere**.
- ASP.NET-style churny identifiers — `ctl00_cph1_txtMemberNo`, form fields
  named `ctl00$cph1$txtNickname`.
- Balances in an **iframe**, not the main document.
- The interstitial is a positioned `<div>`, not `<dialog>` — no `.close()`, no
  `::backdrop`, no escape-key handling, and the shade really does eat clicks.
- The commit button can also be guarded by a **native `window.confirm()`**,
  which is a deliberately *different* problem: the interstitial is a DOM node
  you can find and click, this is a browser-level event with no element at all.
  Automation that handles the div stalls here with the form never submitted.
  Note the tests can only assert the handler is in the markup — `TestClient`
  runs no JS, so the dialog itself needs a browser to verify.
- Labels are adjacent table cells, not `<label for=…>`.
- Some controls are `<a href>` with inline handlers, not `<button>`.
- Under `slow`, the navigation completes long before the data arrives, and the
  data is genuinely absent from the DOM — not merely hidden.

## Two tenants, one vendor product

Both mounted in one process from a single config dict (`tenants.py`), standing
in for "many tenants running the same vendor product configured differently".

| | NorthStar Core | Pinebank Servicing |
|---|---|---|
| Entry | `/login` | `/pb/login` |
| Collection | `/members` | `/pb/customers` |
| Noun | Member | Customer |
| ID param | `memberNumber` | `q` |
| Surname param | `lastName` | `surname` |
| Product | CoreTeller 7.2.1 | CoreTeller 6.9.4 |
| Form field order | nickname, deposit, purpose, statements | purpose, nickname, statements, deposit |

Same handlers, same templates, same underlying data. The differences are picked
to be exactly the ones that break a positional or hard-coded locator strategy
and survive a label-anchored one — which is the cross-tenant reuse question the
assignment asks about.

## Deliberate omissions

- **No real auth.** Any 4+ character password. Credentials are not the subject.
- **No database.** In-process dicts, because total reset must be trivial.
- **Fake PII by design.** Member records carry SSN- and DOB-shaped fields that
  render on the detail screen and appear in any screenshot, so the automation's
  redaction rules have something to actually bite on. All values are invented.
- **The control panel is not styled like the app.** It is operator tooling and
  should never be mistaken for the surface under test.
