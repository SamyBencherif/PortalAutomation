# Design write-up

## 1. Architecture

Five components behind one seam.

```
        discovery (once)                    replay (every invocation)
   goal ─► AgentLoop ─► Recorder ─► Capability ─► ReplayEngine ─► Result
             │  model in the loop      (JSON)         │  no model
             └──────────────┬────────────────────────-┘
                         Surface
              observe() -> Frame ;  click / type / key / scroll
                            │
                     X11Surface (Xvfb + xdotool)
```

`Surface` is the only thing the upper layers know about the world. It offers
the *human* action vocabulary — look, point, click, type, press — and
deliberately no `query_selector`. Its absence is the design: a surface that
could offer one would tempt every layer above into depending on it, and the
desktop implementation would then be impossible rather than merely unwritten.

**The load-bearing decision is that perception is screenshots only.** No DOM,
no accessibility tree, no CDP. The brief says to bias for an approach that
still works with no clean DOM, and this is that approach taken literally. It
costs speed and it costs the ability to read anything the user cannot see. It
buys the thing that actually matters here: the port to a native desktop client
is a new `Surface` and nothing else.

One consequence worth stating because it cuts against the target's design: the
mock hides balances in an iframe specifically to punish automation that reads
only the top-level DOM. Screenshot perception sidesteps that obstacle entirely
— not by cleverness, but because pixels do not have a document tree. I would
rather say that than claim credit for beating it.

Trade-offs I would defend: single process, files not a database (capabilities
are reviewed and diffed by humans, so git gives version history, blame and
rollback for free); subprocess calls to tesseract and xdotool rather than
bindings; ~40 lines of numpy instead of OpenCV.

## 2. Artifact schema

`Capability` = identity + `app` + typed `params` + typed `outputs` + `steps` +
`checkpoint` + `approval` + `provenance`.

It is shaped as an **API, not a macro**. A calling agent needs to know what to
supply, what it gets back, and how to tell success from a legitimate "no such
member" — so those are first-class fields rather than things you infer by
reading the step list. Parameters are validated before a single action is
taken, so a malformed call fails at the boundary instead of four screens deep.

The part I spent the most care on is `Target`, because it carries the central
tension: perception is pixels, but §3.3 demands *stable* targeting, and a pixel
coordinate is the opposite of stable. The resolution is **perceive in pixels,
never record them**. Each target holds an ordered chain:

1. **`TextAnchor`** — a label, a spatial relation, an occurrence index, and a
   list of aliases. "The box to the right of `Nickname`."
2. **`TemplateAnchor`** — a patch, matched by normalised cross-correlation,
   searched only near the recorded coordinate.
3. **`absolute`** — the coordinate, kept as a last resort *and* as tier 2's
   search hint, which is the second job that justifies recording it at all.

Concretely why tier 1 must lead: the two tenants order the same four
sub-account fields differently while rendering byte-identical inputs. An
absolute coordinate lands on the wrong field; a patch match cannot tell the
fields apart, because they look the same. Only the label survives.

`aliases` carries the other axis of drift — one institution's "Member Number"
is another's "Customer Number" — so one artifact serves both rather than being
re-recorded per tenant. `occurrence` disambiguates labels that legitimately
repeat, like `Balance` once per row of the account grid.

Two properties make the artifact trustworthy rather than hopeful:

- **Recorded targets are self-validated.** Each derived anchor is immediately
  re-resolved against the frame it was derived from; if it does not land back
  on the control it came from, the step is flagged `low_confidence` and the CLI
  prints it for review.
- **Discovery always emits `draft`.** A model proposing a flow and a human
  accepting it are different events.

Self-validation has a limit I found by testing rather than by reasoning: it
checks *consistency*, not *meaning*. A click in empty space happily anchored to
a stray one-character OCR artifact 680px away in the masthead, and validation
passed, because the recorded relation faithfully reproduced the click. Meaning
therefore has to be constrained separately — a maximum anchor distance and a
minimum anchor length.

## 3. Determinism & error handling

**Determinism.** Replay consults no model. The target is deterministic by
construction (no randomness anywhere; every variant is a pure function of
profile, route and a per-session counter), and `--reset` between runs is what
makes two runs comparable at all. Waits are always on *content*, never on a
load event or a sleep — the target renders a spinner and swaps real data in
afterwards precisely to punish anything that assumes navigation means arrival.

**The error taxonomy** is the part the brief says is most often botched, and it
is four-way rather than two-way:

| | meaning | replay does |
|---|---|---|
| `business_outcome` | a legitimate final answer | returns it, with outputs |
| recoverable | a transient | absorbs it within a bounded budget, logs it |
| `escalated` | a human could finish this | routes an intervention, pauses |
| `failed` | genuinely wrong | stops with step, expected, observed |

"No such member" is an **answer**. A caller that receives it as an exception has
been told nothing useful and will pointlessly retry. Equally, a supervisor-code
demand is not a failure — the run is one human action from completing, and
collapsing it into `failed` would throw away the escalation path.

Detection is by OCR **screen signature**, keyed on reference codes
(`E-403-PROFILE`) ahead of prose, because copy gets reworded between releases
and reference codes do not. The catalogue is data, not control flow, so a second
vendor product is a new table rather than a new code path.

A case worth calling out: replaying an irreversible write is this system's
*normal* case, and the target returns the original confirmation number on a
duplicate. That is classified `ALREADY_EXISTS` / business outcome — "already
done, here is the receipt" — which is the idempotent answer a replay engine
needs, not a crash.

**Drift**, secondarily. Replay reports which tier resolved each target. A step
that has quietly started resolving via `absolute` still passes today but has
lost its robustness; surfacing the tier turns invisible decay into a signal.
Recovery budgets are bounded per-settle *and* per-run, because an unbounded
retry loop is indistinguishable from a hang to the caller.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `Surface`: `observe() -> Frame` plus
human-shaped actions. A legacy frameset app needs no change at all — frames are
a document concept and we photograph the screen. A native desktop client is a
new `Surface` implementation (screenshot plus OS-level input, or the
accessibility API where it is richer than pixels); the artifact schema, the
recorder, the taxonomy and the replay engine are untouched, because a flow is
expressed in labels and intents rather than in selectors. That is the entire
argument for accepting screenshot-only perception's costs.

**Multi-tenant.** One artifact should serve many institutions running the same
vendor product. Three mechanisms, all in the schema:

- `TextAnchor.aliases` absorbs relabelling (Member → Customer).
- Label-anchored targeting absorbs re-ordering — which is exactly the
  difference between the two tenants here, and exactly what a positional
  strategy cannot survive.
- `AppRef` records the product, version and tenant variant an artifact was
  recorded against, so a cross-variant replay is a known act rather than an
  accident.

**Drift management**, at scale, is the tier signal. A fleet-wide report of
"which capabilities are resolving below tier 1, on which tenants" tells you
where to re-record *before* a break, rather than after. I would build the
per-tenant override layer (a base artifact plus a thin variant patch) next; the
schema is shaped for it but I did not implement it.

## 5. Escalation & handoff

**Detecting stuck** is not a heuristic: it is three explicit conditions — a
`STUCK`-class signature (the supervisor-code demand), a policy refusal of an
irreversible step, and an unresolvable target after the tier chain is exhausted.

**Taking control** is the part that is easy to fake, so it is made structurally
true. The agent drives an X display inside the container; `x11vnc` publishes
*that same display*; noVNC puts it in the operator's browser. When they move the
mouse they move the agent's mouse, in the agent's browser, holding the agent's
session cookie. There is no second session to diverge because there is only one.
Control is a single flag with one owner, and the automation genuinely blocks on
an Event while the human holds it — any design where it keeps acting produces
two actors racing on one session.

**The evidence is independent.** The broker also announces the handoff to the
target's own control plane, so the *application's* audit log shows one
continuous session id with the actor changing partway down. Our log could be
generous; a log we do not write cannot be. Note the deliberate asymmetry: the
harness may reach `/_control`, the agent may not — arranging a handoff and
letting an agent reconfigure its own target are different acts.

The operator console itself is deliberately thin: one page, a live view, a
Resume button and a note field. What is missing is product, not mechanism — no
cross-run queue, no operator identity, no per-action audit of what the human
did beyond their note.

## 6. Safety

**The allowlist is enforced where the agent cannot argue with it.** This falls
directly out of screenshot-only perception: the agent cannot reliably read its
own address bar, so "the agent checks before clicking" would be enforcement by
good intentions. Instead the browser is launched behind a filtering proxy, so a
denied route never leaves the container regardless of what the model decides to
click — including redirects no Python code initiated. `/_control` and `/admin`
are denied; the target ships a deliberately destructive
`/admin/{id}/close` route so the refusal is real rather than hypothetical.
CONNECT is refused outright, because a tunnel would make every rule above
unenforceable.

**Irreversible actions need two independent gates**: the capability must be
human-`approved`, and the run must pass `--allow-irreversible`. Either alone is
one mistake away from opening real accounts. A blocked irreversible step
escalates rather than fails.

**Redaction happens at the boundary where data becomes durable** — the one
place a frame is written to disk — rather than being trusted to every call
site. SSN- and date-shaped values are masked in *pixels*, and the count of
masked regions is logged so a reviewer can tell "withheld" from "absent". The
date rule over-matches deliberately: from pixels alone a date of birth and an
account opening date are indistinguishable, and the wrong error to make is the
one that leaks.

**Limits, plainly.** The proxy is path-prefix matching, not semantic
understanding — it cannot tell a safe POST from a dangerous one to the same
route. Redaction depends on OCR, so a value tesseract misreads is a value that
is not masked; it is defence in depth, not a guarantee. The API key is a process
environment variable, which is appropriate here and not appropriate in
production. And `--allow-irreversible` is per-run and coarse: real deployment
wants per-capability, per-tenant authorisation with an approver identity.

## 7. Cuts

**Deliberately cut**, with the seam left real:

- **A second `Surface`.** The abstraction is the argument; a desktop
  implementation would demonstrate it but is not needed to make it credible.
- **Per-tenant override layers.** The schema supports the idea (`AppRef`,
  aliases); the base-plus-patch machinery is unbuilt. This is what I would do
  next — it is the highest-value remaining item for the real environment.
- **A real operator console.** Mechanism real, product minimal, as the brief
  permits.
- **LLM-assisted replay recovery.** Attractive and dangerous: a bounded,
  policy-checked single-step fallback is the right shape, but an open-ended one
  destroys the determinism that is the whole point.
- **Queues, workers, multi-tenant plumbing.** The brief explicitly does not
  reward these and I did not build them.
- **Pagination, `__VIEWSTATE` round-tripping, dirty fixture data** in the
  target. Each is realistic; none is load-bearing for the requirements. The
  `__VIEWSTATE` token is the interesting one — it would *prove* replay drives
  the UI rather than forging HTTP requests.

**Unfinished rather than cut**: the escalation path end to end. Its mechanism is
tested, but the live run needs the write flow, whose capability has not been
discovered. Also: the container runs as root, so evidence it writes is
root-owned on the host.

The loop itself is complete and in `evidence/`: Opus 5 found the flow in 9
actions from screenshots alone, the recorder produced a 3-step artifact of
tier-1 label anchors, and that artifact replays with no model across five
conditions. Two honesty notes on it. Three of the discovery turns were served
by `claude-opus-4-8` through the refusal fallback, which the artifact's
provenance records rather than glossing. And the agent never handles
credentials — sign-on is a harness precondition, which began as a response to a
policy refusal and turned out to be the better design.

Five findings from running it that I would not have predicted, and that the
write-up would be dishonest without:

- **Screen scale is load-bearing, not cosmetic.** 11px Verdana sits at
  tesseract's limit: the same page read cleanly once and as `'Or'` / `'Pas'`
  the next time, flipping on sub-pixel layout shifts. I had retired this risk
  early on one lucky sample, which was wrong.
- **Whole-frame OCR loses text silently and partially.** On a 1600×1000 frame
  tesseract returns the bold header and omits the form labels beside it, in
  every segmentation mode; cropped to their region the same pixels read at 96%
  confidence. Density, not legibility, is the discriminator. This is the worst
  kind of failure — the frame looks successfully read and the control you
  needed is simply absent — and it is why `ocr.read` crops to content first.
- **A signature fragment must be unsayable in passing.** `VALIDATION_ERROR`
  keyed on "is required", and the maintenance interstitial says "No action is
  required." A recoverable condition was therefore reported as a final business
  outcome. Two other bugs had the same shape: a run inheriting the previous
  run's screen as its own result, twice, because a terminal outcome was read
  before the browser had repainted.
- **A modal's shade defeats OCR, and that is correct.** The interstitial dims
  the page to 45% black, which puts everything beneath it — including the
  notice's own heading — below what tesseract can read, while its Dismiss link
  stays legible. Keying detection on the heading meant the modal went
  unrecognised exactly when it was covering something. A page being unreadable
  under a modal is not a bug to engineer around: a human cannot act on it
  either. The job is to notice the modal and clear it.
- **A blind click can manufacture a false success.** A run whose session had
  gone clicked recorded coordinates through a stale page the previous run had
  left on screen, matched its checkpoint against that page, and reported
  success with a balance it never fetched. A coordinate is a *position*, not an
  *identification*: unlike a label or a matched patch it carries no evidence
  the right screen is even present. Replay now refuses to act on one. A wrong
  answer presented as a right one is the worst thing a replay engine can do.

**What I would do next, in order**: discover the write flow, which unlocks the
escalation run; then per-tenant overrides; then a multi-run stability score, since replaying N times
and reporting flakiness is the cheapest way to earn trust in an artifact before
letting it run unattended.
