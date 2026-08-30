# PortalAutomation

A record-once / replay-many computer-use system for legacy bank back-office software —
built on the premise that if you want to automate a *machine*, you should be driving a
machine, not a browser tab.

---

## What's different about this one

Most solutions to this brief will be Playwright driving a headless Chrome on the host,
with CSS selectors as the locator strategy. That works, and it is the wrong shape for
the environment the brief actually describes.

**This one runs the agent inside a disposable virtual machine and acts at the OS level —
screen, keyboard, mouse.** That single decision is what the rest of the design falls out
of, and it is what the rest of this README is about:

- **The DOM is an optimization, not the interface.** The brief says the real surfaces
  include native desktop apps and that you should bias toward approaches that survive
  *no clean DOM*. A screen-and-input contract survives it by construction. A selector
  strategy does not.
- **Determinism goes below the locator.** Replay is byte-reproducible because the whole
  machine is — same snapshot, same fonts, same DPI, same resolution, same clock, same
  network. Screenshot-anchored targeting is only replayable if the screen is.
- **Guardrails are enforced by the boundary, not honored by the agent.** The allowlist is
  a property of the VM's network, not a function the agent politely calls before
  navigating. A bug cannot escape a route that doesn't exist.
- **Escalation hands over the actual machine.** The brief permits mocking the operator
  console. This one doesn't need to: a human takes control of the live session by
  attaching to the same VM the agent is driving, mid-run, and detaching when done. Same
  session, same screen, no fork.
- **The hostile target was built first.** [`mock_teller/`](mock_teller/README.md) is a
  deliberately legacy bank surface — nested tables, no test IDs, ASP.NET identifiers,
  balances hidden in an iframe — where every runtime condition class in the brief is
  reachable by one documented command.

> **[ gif — `docs/media/overview.gif` ]**
> 40s end-to-end: goal in, agent drives the VM, artifact emitted, replayed without the
> model, one injected failure escalated to a human who takes the screen and hands it back.

---

## The bet: the surface is a machine, not a browser

There is a real fork in this assignment, and most of the interesting consequences follow
from which way you take it.

| | Host + Playwright | Host + OS automation | **VM + OS automation** |
|---|---|---|---|
| Native desktop apps | ✗ out of reach | ✓ | ✓ |
| Survives no-clean-DOM | ✗ it *is* the DOM bet | ✓ | ✓ |
| Usable while it runs | ✓ headless | ✗ steals your mouse | ✓ it isn't your machine |
| Reproducible screen | ✗ host DPI/fonts/size | ✗ | ✓ snapshot-pinned |
| Allowlist enforced where | in-process check | in-process check | ✓ network boundary |
| Live-session handoff | mocked console | your own desktop | ✓ attach to the VM |
| Regulated data lands on | your laptop | your laptop | ✓ ephemeral disk |

The middle column is the honest-but-unusable one: OS-level control on the host means the
run fights you for the pointer, any stray keystroke corrupts it, and the screen it sees
is whatever your window manager happened to be doing. That's why most people retreat to
the left column, and buy the DOM assumption to get there.

**The VM makes the middle column usable.** You get OS-level fidelity *and* an isolated,
snapshottable, network-fenced environment that has no opinion about what you're doing on
your laptop at the time. That's the whole insight; everything below is a consequence.

> **[ screenshot — `docs/media/vm-console.png` ]**
> The sandbox VM console mid-run: the mock teller's member detail screen, the agent's
> cursor mid-click, the host desktop untouched around it.

---

## What that buys, against the parts of the brief that are hard

### Determinism, below the locator

The brief asks how determinism is achieved on replay and treats it as a locator problem —
stable targeting, fallbacks, waits. Those matter, and this system has them. But a
locator strategy is only as reproducible as the pixels it resolves against, and on a host
those pixels depend on your monitor, your scaling factor, your installed fonts, and
whether you had a notification banner up.

Pinning the machine moves the guarantee down a layer:

- **Snapshot-pinned base state.** Replay restores a known VM snapshot, so the run starts
  from an identical desktop every time.
- **Fixed display geometry, DPI, and font set**, baked into the image — coordinates and
  visual anchors mean the same thing on every run and every reviewer's machine.
- **Frozen clock and no outbound network** beyond the target, so nothing drifts underneath
  the run.
- **A target that is itself deterministic.** The mock teller has no randomness anywhere:
  every variant is a pure function of `(profile, knobs, route, request counter)`, session
  ids and confirmation numbers are sequential counters rather than UUIDs, and two runs
  separated by a reset are byte-identical. Randomness would make a passing replay
  unfalsifiable.

Determinism you can *check* rather than assert is the point. Both halves — the machine and
the target — are pinned, so a replay diff that comes back non-empty means the system
changed, not the weather.

### Guardrails that are enforced rather than honored

An allowlist implemented as `if not allowed(url): raise` is an honor system. It protects
you from an agent that decides to misbehave and not at all from a bug, a prompt injection
in the page, or a step recorded against the wrong route.

Inside a VM the same policy has teeth:

- **Network fence.** The guest can reach the target's host/port and nothing else. Routes
  outside the allowlist aren't refused — they aren't reachable.
- **Blast radius is the disk image.** The guest is disposable and reset between runs, so a
  wrong action's worst case is bounded by something you throw away.
- **Regulated data never lands on the host.** The mock teller renders SSN- and DOB-shaped
  fields on the detail screen precisely so they show up in screenshots and the redaction
  rules have something real to bite on. Frames stay in the guest; redaction happens on the
  way out, so what reaches the artifact and the evidence log is already scrubbed.
- **Irreversible actions stay a policy decision, not an accident.** The target ships
  `/admin/{collection}/{id}/close` — a genuinely destructive route that no demo flow
  needs — so the allowlist has something real to refuse rather than a hypothetical.

> **[ screenshot — `docs/media/guardrail-refusal.png` ]**
> A blocked irreversible action: the policy decision, the reason, and the structured
> result handed back to the caller.

### Handoff that is a handoff

The brief scopes a real-time co-browsing operator console out, and says a mocked operator
surface is fine as long as the control-transfer model is real. The VM makes most of the
mock unnecessary, because "let the human operate the same live session" stops being a
metaphor: the human attaches to the running guest's display, works, and detaches.
It is the same machine, the same session, the same browser process, the same cookies.

The transfer model on the application side is real too, and auditable. `POST
/_control/handoff` doesn't create a session, swap cookies, or fork state — it flips the
actor on the session already running, so a single continuous audit log *proves* the human
operated the live session instead of asserting it:

```
 1 sess-0001  agent  nav     sign on as teller1
 2 sess-0001  agent  submit  draft staged
 3 sess-0001  agent  submit  committed SAV-10001-03 (CNF-000001)
 4 sess-0001  human  control control transferred agent -> human
 5 sess-0001  human  nav     viewed 10005
 6 sess-0001  agent  control control transferred human -> agent
```

There is a condition in the target that exists only to force this path: member `10005`
demands a supervisor override code on commit, and the agent has no way to obtain one.
Escalating is not a fallback there — it is the single correct move.

> **[ gif — `docs/media/handoff.gif` ]**
> Agent stalls on the supervisor-code gate, raises an intervention request with context,
> a human attaches to the VM and enters the code, control returns, the run completes.

### Heterogeneity, mostly for free

The brief asks how the design extends to legacy web *and* desktop surfaces, and across
hundreds of tenants running the same vendor product configured differently.

Half that answer is already structural: when the action contract is screen, keyboard, and
mouse, a desktop app is not a new backend — it's a different-looking window. The seam
between "how we perceive and act on a surface" and "the recorded flow" sits at the VM
boundary rather than inside a browser driver.

The other half is a locator-strategy question, and the target is built to test it
honestly. Two tenants are mounted in one process from a single config — same handlers,
same data, different nouns (*Member* vs *Customer*), different query parameters, different
form field order, different product versions. Those differences are chosen to be exactly
the ones that break a positional or hard-coded strategy and survive a label-anchored one.
Cross-tenant reuse is therefore something this repo can demonstrate rather than promise.

---

## The target surface

[`mock_teller/`](mock_teller/README.md) — a deliberately hostile stand-in for the core
banking screens the brief says we won't get access to. It was built first, on purpose:
the automation side is bounded by how many *interesting* conditions its target can produce
on demand, and a happy-path surface makes the error taxonomy, the escalation path, and the
guardrail model untestable — you end up asserting them in prose.

Every condition class in the brief is reachable with one documented command: validation
errors, record-not-found, permission denial, unexpected dialogs (both a DOM interstitial
*and* a native `window.confirm()`, which are deliberately different problems), session
expiry, transient 503s, deferred loads, hard 500s, a hanging route, duplicate commits of an
irreversible write, and a stuck state whose only correct resolution is escalation.

Full catalogue, determinism argument, and the hostility inventory: **[mock_teller/README.md](mock_teller/README.md)**.

> **[ screenshot — `docs/media/mock-teller.png` ]**
> The member detail screen: nested tables, `ctl00_cph1_*` identifiers, no test IDs, and
> the savings balance sitting in an iframe rather than the top-level document.

---

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn mock_teller.app:app --port 8800     # the target surface
.venv/bin/python -m pytest tests/ -q                  # 30 tests, ~1.3s
```

Then open <http://localhost:8800/> — any operator ID, any password of 4+ characters.
The scenario control panel is at <http://localhost:8800/_control>.

<!-- TODO: VM image build/fetch, agent + replay entry points, and API key config land here. -->

### Demo path

<!-- TODO: exact commands — discovery run on a goal, then replay of the emitted artifact,
     then the injected-failure replay that escalates. -->

---

## Status

| Piece | State |
|---|---|
| Target surface (`mock_teller/`) | ✅ built — 30 tests, byte-identical across resets |
| Sandbox VM image + lifecycle | 🚧 in progress |
| Agent loop (observe → decide → act) | 🚧 in progress |
| Artifact schema | 🚧 in progress |
| Deterministic replay engine | 🚧 in progress |
| Guardrails (network fence, allowlist, redaction) | 🚧 in progress |
| Escalation + live-session handoff | 🚧 target side ✅, agent side in progress |
| Evidence (`evidence/`) | 🚧 in progress |
| `REPORT.md` | 🚧 in progress |

---

## Layout

```
mock_teller/     the hostile target surface (see its README)
tests/           tests for the target
docs/media/      screenshots and recordings
evidence/        artifacts + logs from a real discovery run and a replay run  [pending]
REPORT.md        architecture, artifact schema, determinism, safety, and the cut list  [pending]
```

The design reasoning — every decision above, its trade-offs, and what was deliberately cut —
lives in **[REPORT.md](REPORT.md)**.
