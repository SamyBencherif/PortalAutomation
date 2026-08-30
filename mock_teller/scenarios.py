"""The variant engine: how the mock produces interesting conditions on demand.

Three layers, highest precedence first:

  1. Per-request injection  -- ?_inject=error_500 or X-Mock-Inject header.
     One shot, changes no state. This is what the tests use.
  2. Session scenario       -- a named profile bound to the session cookie via
     POST /_control/scenario. This is what a demo run sets up front.
  3. Fixture-encoded outcome -- always on, needs no setup. See fixtures.py.

Nothing here is random. Every variant is a pure function of
(profile, knobs, route, per-session request counter). Randomness would make
"deterministic replay" unfalsifiable: a replay that passed would tell you
nothing, because a rerun might have rolled different dice.
"""

from dataclasses import dataclass, field, replace

# Route keys the knobs address. Deliberately coarse -- a knob per URL would be
# unusable, a knob per flow stage is what you actually want to reason about.
ROUTE_NAV = "nav"        # login, member detail, frame, form GET
ROUTE_SEARCH = "search"  # the search results page
ROUTE_SUBMIT = "submit"  # sub-account review + commit

ROUTES = (ROUTE_NAV, ROUTE_SEARCH, ROUTE_SUBMIT)


@dataclass
class Knobs:
    """The full set of things that can be made to go wrong."""

    latency_ms: dict[str, int] = field(
        default_factory=lambda: {ROUTE_NAV: 0, ROUTE_SEARCH: 0, ROUTE_SUBMIT: 0}
    )
    # Pages that render a spinner first and swap real content in via JS after a
    # delay. Navigation completes long before the data does -- this is what
    # punishes an automation that waits on load instead of on content.
    spinner_pages: frozenset[str] = frozenset()
    spinner_delay_ms: int = 0
    # Maintenance interstitial on every Nth page view. 0 disables.
    interstitial_every_n: int = 0
    # route -> N. The first N requests to that route 503 with Retry-After, then
    # it succeeds. Recoverable: a correct replay retries and carries on.
    transient_503_first_n: dict[str, int] = field(default_factory=dict)
    session_ttl_s: int = 3600
    # Routes that hard-500 with a stack-trace-looking error page.
    error_500_on: frozenset[str] = frozenset()
    # Routes that never respond. Exercises the client-side timeout path.
    hang_on: frozenset[str] = frozenset()
    # Routes whose primary control is guarded by a native window.confirm().
    # Deliberately distinct from the maintenance interstitial: that one is a
    # positioned <div> and therefore a DOM problem, while this is an out-of-DOM
    # browser event. Automation that handles the div still hangs on this.
    native_confirm_on: frozenset[str] = frozenset()
    # Forces the supervisor-override demand on every sub-account commit, not
    # just for member 10005.
    require_override_code: bool = False

    def copy(self) -> "Knobs":
        return replace(
            self,
            latency_ms=dict(self.latency_ms),
            transient_503_first_n=dict(self.transient_503_first_n),
        )


PROFILES: dict[str, Knobs] = {
    # Everything off. The discovery run and the golden replay use this.
    "clean": Knobs(),

    # Slow but correct. Nothing fails; the automation just has to wait properly.
    "slow": Knobs(
        latency_ms={ROUTE_NAV: 2000, ROUTE_SEARCH: 3500, ROUTE_SUBMIT: 5000},
        spinner_pages=frozenset({"detail"}),
        spinner_delay_ms=4000,
    ),

    # Recoverable conditions: a transient 503 on first search, plus a
    # maintenance modal every third page view.
    "flaky": Knobs(
        latency_ms={ROUTE_NAV: 250, ROUTE_SEARCH: 600, ROUTE_SUBMIT: 400},
        interstitial_every_n=3,
        transient_503_first_n={ROUTE_SEARCH: 1},
        native_confirm_on=frozenset({ROUTE_SUBMIT}),
    ),

    # Hard failure: the commit route 500s. Nothing the automation can do about
    # it; the correct behaviour is to stop and report a debuggable error.
    "broken": Knobs(error_500_on=frozenset({ROUTE_SUBMIT})),

    # Session dies mid-flow, bouncing to login. Distinguishable from a hard
    # failure: the automation could in principle re-auth and resume.
    "expired": Knobs(session_ttl_s=5),

    # The stuck state. Commit demands a supervisor override code the agent
    # does not have and cannot obtain -- so it must escalate to a human.
    "locked_down": Knobs(require_override_code=True),

    # Route that never answers. Separate from "broken" because the failure mode
    # (client timeout, no response at all) is genuinely different to detect.
    "hanging": Knobs(hang_on=frozenset({ROUTE_SEARCH})),
}

DEFAULT_PROFILE = "clean"

# One-shot injections addressable by ?_inject=. Each maps to a knob override
# scoped to the single request being served.
INJECTIONS = {
    "error_500":     lambda k, r: replace(k, error_500_on=frozenset({r})),
    "transient_503": lambda k, r: replace(k, transient_503_first_n={**k.transient_503_first_n, r: 99}),
    "interstitial":  lambda k, r: replace(k, interstitial_every_n=1),
    "slow":          lambda k, r: replace(k, latency_ms={**k.latency_ms, r: 2500}),
    "spinner":       lambda k, r: replace(k, spinner_pages=frozenset({"detail"}), spinner_delay_ms=3000),
    "hang":          lambda k, r: replace(k, hang_on=frozenset({r})),
    "expire":        lambda k, r: replace(k, session_ttl_s=0),
    "override":      lambda k, r: replace(k, require_override_code=True),
    "native_confirm": lambda k, r: replace(k, native_confirm_on=frozenset({r})),
}


def resolve(profile: str, overrides: dict | None, inject: str | None, route: str) -> Knobs:
    """Compose the three layers into the knobs for one request."""
    knobs = PROFILES.get(profile, PROFILES[DEFAULT_PROFILE]).copy()

    if overrides:
        knobs = _apply_overrides(knobs, overrides)

    if inject and inject in INJECTIONS:
        knobs = INJECTIONS[inject](knobs, route)

    return knobs


def _apply_overrides(knobs: Knobs, overrides: dict) -> Knobs:
    """Apply a partial knob dict from /_control/scenario. Unknown keys ignored."""
    data = {}
    for key, value in overrides.items():
        if key == "latency_ms" and isinstance(value, dict):
            merged = dict(knobs.latency_ms)
            merged.update({k: int(v) for k, v in value.items() if k in ROUTES})
            data[key] = merged
        elif key == "transient_503_first_n" and isinstance(value, dict):
            merged = dict(knobs.transient_503_first_n)
            merged.update({k: int(v) for k, v in value.items() if k in ROUTES})
            data[key] = merged
        elif key in ("spinner_pages", "error_500_on", "hang_on", "native_confirm_on"):
            data[key] = frozenset(value)
        elif key in ("interstitial_every_n", "session_ttl_s", "spinner_delay_ms"):
            data[key] = int(value)
        elif key == "require_override_code":
            data[key] = bool(value)
    return replace(knobs, **data)


def profile_names() -> list[str]:
    return list(PROFILES)
