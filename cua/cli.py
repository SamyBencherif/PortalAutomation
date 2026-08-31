"""Command line: discover, replay, catalog, approve, operator.

`replay` is the interesting one -- it is the production entry point, the thing
an AI agent would invoke by name with typed arguments. Everything it prints is
also written to `evidence/runs/<run_id>/`, because a result a human read once
in a terminal is not evidence.

Note which commands talk to the target's control plane. `--profile` and
`--reset` do, and they are HARNESS operations: arming a fault or resetting
state is the test rig setting up a scenario. The agent itself is forbidden from
reaching `/_control` by the allowlist, and that asymmetry is deliberate --
an agent that can reconfigure its own target is not being tested against
anything.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

from cua.artifact.schema import (
    Approval, Extraction, Output, Param, ParamType, Relation, TextAnchor,
)
from cua.artifact.store import CapabilityNotFound, Store
from cua.bootstrap import BootstrapError, sign_on
from cua.escalation.broker import Broker
from cua.escalation.console import serve_in_background
from cua.evidence.run import RunLog
from cua.replay.engine import ReplayEngine
from cua.safety.policy import DEFAULT_POLICY, Policy

TARGET = os.environ.get("CUA_TARGET", "http://target:8800")
VNC_URL = os.environ.get("CUA_VNC_URL", "http://localhost:6080/vnc.html")
CONSOLE_PORT = int(os.environ.get("CUA_CONSOLE_PORT", "8080"))


def _surface():
    """Built lazily: the X11 surface needs a display, which unit tests lack."""
    from cua.surface.x11 import X11Surface
    return X11Surface(browser_window=os.environ.get("CUA_BROWSER_WINDOW", ""))


def _policy(args) -> Policy:
    policy = Policy.load(args.policy) if getattr(args, "policy", None) else DEFAULT_POLICY
    if getattr(args, "allow_irreversible", False):
        policy = Policy(**{**policy.__dict__, "allow_irreversible": True})
    return policy


def _parse_output(spec: str) -> Output:
    """`name:type:anchor:regex[:span]` -- what the capability returns.

    Declared by a human rather than inferred from the goal text. What a
    capability returns is part of its contract with the calling agent, and a
    contract guessed from prose is not one worth relying on.
    """
    parts = spec.split(":")
    if len(parts) < 4:
        raise SystemExit(
            f"--output {spec!r} must be name:type:anchor:regex[:span_px]"
        )
    name, type_, anchor_text, pattern = parts[0], parts[1], parts[2], parts[3]
    span = int(parts[4]) if len(parts) > 4 else 1150
    return Output(
        name=name, type=ParamType(type_),
        extract=Extraction(
            anchor=TextAnchor(text=anchor_text, relation=Relation.RIGHT_OF),
            span_px=span, pattern=pattern,
        ),
    )


# --------------------------------------------------------------- harness ops

def _control(path: str, payload: dict[str, Any] | None = None,
             cookies: dict[str, str] | None = None) -> Any:
    """Talk to the target's control plane. Harness-side only, never the agent."""
    url = f"{TARGET.rstrip('/')}/_control{path}"
    try:
        if payload is None:
            return httpx.get(url, timeout=5.0).json()
        r = httpx.post(url, json=payload, headers={"content-type": "application/json"},
                       cookies=cookies or {}, timeout=5.0)
        return r.json() if r.status_code == 200 else {"error": r.status_code, "body": r.text}
    except httpx.HTTPError as e:
        return {"error": str(e)}


def _browser_sid() -> str | None:
    """The session id the BROWSER is using, per the target's own state."""
    state = _control("/state")
    for session in (state.get("sessions") or []):
        if session.get("user"):
            return session["sid"]
    return None


def _arm_scenario(profile: str, overrides: dict[str, Any] | None = None) -> Any:
    """Arm a fault profile against the session the browser is actually using.

    The target binds a scenario to the *caller's* cookie, and this harness
    talks to it over httpx, which has none -- so a naive call mints a fresh
    session and arms that one, leaving the browser untouched. It appeared to
    work only because session ids are sequential: after a reset the browser's
    stale `sess-0001` cookie sometimes collided with the newly minted one.
    Reading the real session id and presenting it makes the intent explicit
    instead of accidental.

    Must therefore run AFTER sign-on, once the browser's session exists.
    """
    sid = _browser_sid()
    cookies = {"CORETELLERSESSID": sid} if sid else None
    return _control("/scenario", {"profile": profile, "overrides": overrides or {}},
                    cookies=cookies)


# ------------------------------------------------------------------ replay

def cmd_replay(args) -> int:
    store = Store(args.store)
    try:
        cap = store.load(args.ref)
    except CapabilityNotFound:
        print(f"no such capability: {args.ref}", file=sys.stderr)
        return 2

    params = dict(p.split("=", 1) for p in args.param)

    if args.reset:
        # Between replay runs, so one run cannot inherit another's state.
        # This is what makes two runs comparable at all.
        _control("/reset", {})

    log = RunLog.create("replay", root=args.evidence)
    surface = _surface()
    if not args.no_login:
        try:
            sign_on(surface, f"{TARGET}/login", args.operator, args.password)
            log.event("session_bootstrapped", operator=args.operator)
        except BootstrapError as e:
            print(f"could not establish a session: {e}", file=sys.stderr)
            return 1

    # Armed AFTER sign-on, so it binds to the browser's session rather than a
    # throwaway one this process would otherwise mint.
    if args.profile or args.override:
        overrides = json.loads(args.override) if args.override else {}
        armed = _arm_scenario(args.profile or "clean", overrides)
        log.event("scenario_armed", profile=args.profile or "clean",
                  overrides=overrides,
                  session=armed.get("session") if isinstance(armed, dict) else None)

    broker = Broker(target_base=TARGET, vnc_url=VNC_URL, log=log)
    engine = ReplayEngine(
        surface, _policy(args), log,
        credentials=(args.operator, args.password),
        escalate=broker.raise_intervention,
    )

    if args.operator_console:
        serve_in_background(broker, CONSOLE_PORT)
        print(f"operator console: http://localhost:{CONSOLE_PORT}/", file=sys.stderr)

    result = engine.run(cap, params)

    # A run may need a human more than once -- a flow can be blocked on policy
    # and then get stuck -- but it must not be able to ping-pong forever, so
    # the number of handoffs is bounded and an escalation that repeats at the
    # same step with the same code stops rather than being handed back again.
    handoffs = 0
    seen: set[tuple[int, str]] = set()
    while result.status == "escalated" and args.operator_console:
        signature = (result.outcome["step"], result.outcome["code"])
        if signature in seen:
            print("  escalated again at the same step -- not handing back "
                  "a second time", file=sys.stderr)
            break
        if handoffs >= args.max_handoffs:
            print(f"  escalated after {handoffs} handoff(s); stopping at the "
                  "limit", file=sys.stderr)
            break
        seen.add(signature)
        handoffs += 1

        print("\n  ESCALATED -- waiting for a human to take control.", file=sys.stderr)
        print(f"  {result.outcome['message']}", file=sys.stderr)
        print(f"  Take over at http://localhost:{CONSOLE_PORT}/\n", file=sys.stderr)
        if not broker.wait_for_resume(timeout=args.escalation_timeout):
            print("  nobody took over within the timeout", file=sys.stderr)
            break

        # Resume on the SAME session the human just operated, and from the step
        # they unblocked -- not from the top, which would re-drive everything
        # the run already did.
        note = broker.last_resolved.operator_note if broker.last_resolved else None
        print(f"  control returned; resuming from step "
              f"{result.outcome['step']}\n", file=sys.stderr)
        result = engine.resume(cap, params, result, operator_note=note)

    # The target's own audit log, saved beside ours. It is independent evidence
    # that a handoff really happened on one continuous session.
    audit = broker.target_audit()
    if audit:
        (log.root / "target_audit.json").write_text(json.dumps(audit, indent=2) + "\n")

    _print_result(result, log)
    return 0 if result.status in ("success", "business_outcome") else 1


def _print_result(result, log: RunLog) -> None:
    mark = {"success": "OK", "business_outcome": "OUTCOME",
            "escalated": "ESCALATED", "failed": "FAILED"}.get(result.status, "?")
    print(f"[{mark}] {result.capability}  ({result.steps_executed} steps)")

    if result.outputs:
        print("  outputs:")
        for k, v in result.outputs.items():
            print(f"    {k} = {v}")
    if result.outcome:
        print(f"  outcome: {result.outcome['code']} -- {result.outcome['message']}")
    if result.failure:
        print("  failure:")
        for k, v in result.failure.items():
            print(f"    {k}: {v}")
    if result.recovered:
        print("  recovered:")
        for r in result.recovered:
            print(f"    {r['code']} via {r['recovery']} ({r['context']})")
    if result.drift:
        # Not an error, but the thing to act on before it becomes one.
        print("  drift -- these steps no longer resolve by label:")
        for d in result.drift:
            print(f"    step {d['step']} fell back to {d['tier']}: {d['intent']}")
    print(f"  evidence: {log.root}")


# ---------------------------------------------------------------- discover

def cmd_discover(args) -> int:
    from cua.agent.loop import AgentLoop
    from cua.agent.recorder import low_confidence_steps, record
    from cua.artifact.schema import AppRef, Checkpoint

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; the discovery run needs it.",
              file=sys.stderr)
        return 2

    if args.reset:
        _control("/reset", {})

    log = RunLog.create("discovery", root=args.evidence)
    surface = _surface()

    # Authenticate before the model is involved. Credentials are a harness
    # precondition, not something an agent should ever handle -- see
    # cua/bootstrap.py for why that is a design decision and not a workaround.
    start_url = args.entry_url or f"{TARGET}/{args.collection}"
    if not args.no_login:
        try:
            sign_on(surface, f"{TARGET}/login", args.operator, args.password)
            log.event("session_bootstrapped", operator=args.operator)
        except BootstrapError as e:
            print(f"could not establish a session: {e}", file=sys.stderr)
            return 1

    loop = AgentLoop(surface, log, model=args.model, max_steps=args.max_steps)
    result = loop.run(args.goal, start_url)

    print(f"[{result.status}] {result.steps} actions -- {result.stop_reason}")
    if result.final_text:
        print(f"  model: {result.final_text[:500]}")
    print(f"  evidence: {log.root}")

    if result.status != "succeeded":
        return 1
    if not args.save:
        print("  (not saved -- pass --save <capability.id> to record an artifact)")
        return 0

    # A capability that uses {member_no} without declaring it is not
    # invocable: replay validates inputs against the contract and would reject
    # the very parameter the steps interpolate. The names come from --param,
    # which is also what the recorder used to parameterise the literals.
    declared = [
        Param(name=name, type=ParamType.STRING, example=value)
        for name, value in (p.split("=", 1) for p in args.param)
    ]

    outputs = [_parse_output(spec) for spec in args.output]

    cap = record(
        result,
        params=declared,
        outputs=outputs,
        cap_id=args.save,
        title=args.title or args.save,
        description=args.goal,
        app=AppRef(product="coreteller", version=args.app_version,
                   tenant_variant=args.tenant, entry_url=start_url),
        checkpoint=Checkpoint(kind="text_present", text=args.checkpoint),
        param_values=dict(p.split("=", 1) for p in args.param),
        model=args.model,
        run_id=log.run_id,
    )
    path = Store(args.store).save(cap, overwrite=args.overwrite)
    print(f"  saved {cap.ref} -> {path}")

    weak = low_confidence_steps(cap)
    if weak:
        # Surfaced here rather than buried in JSON, because an artifact whose
        # weak points nobody read will be approved anyway.
        print(f"  {len(weak)} step(s) need review before approval:")
        for step in weak:
            print(f"    step {step.index}: {step.intent}")
    print(f"  approval: {cap.approval.value} "
          f"(irreversible replay requires 'approved')")
    return 0


# ----------------------------------------------------------------- catalog

def cmd_catalog(args) -> int:
    store = Store(args.store)
    caps = store.list()
    if not caps:
        print("no capabilities recorded yet")
        return 0

    if args.json:
        # The shape an agent would consume to discover what it can invoke.
        print(json.dumps([
            {"id": c.id, "version": c.version, "title": c.title,
             "description": c.description, "approval": c.approval.value,
             "irreversible": c.is_irreversible,
             "params": [{"name": p.name, "type": p.type.value,
                         "required": p.required, "example": p.example}
                        for p in c.params],
             "outputs": [{"name": o.name, "type": o.type.value} for o in c.outputs]}
            for c in caps
        ], indent=2))
        return 0

    for c in caps:
        flags = [c.approval.value]
        if c.is_irreversible:
            flags.append("irreversible")
        print(f"{c.ref}  [{', '.join(flags)}]")
        print(f"  {c.title} -- {c.description}")
        if c.params:
            print("  params:  " + ", ".join(
                f"{p.name}:{p.type.value}{'' if p.required else '?'}" for p in c.params))
        if c.outputs:
            print("  returns: " + ", ".join(
                f"{o.name}:{o.type.value}" for o in c.outputs))
    return 0


def cmd_approve(args) -> int:
    store = Store(args.store)
    try:
        cap = store.load(args.ref)
    except CapabilityNotFound:
        print(f"no such capability: {args.ref}", file=sys.stderr)
        return 2

    from cua.agent.recorder import low_confidence_steps
    weak = low_confidence_steps(cap)
    if weak and not args.force:
        print(f"{cap.ref} has {len(weak)} low-confidence step(s):", file=sys.stderr)
        for step in weak:
            print(f"  step {step.index}: {step.intent}", file=sys.stderr)
        print("Review them, then re-run with --force.", file=sys.stderr)
        return 1

    cap.approval = Approval.APPROVED
    store.save(cap, overwrite=True)
    print(f"{cap.ref} approved")
    return 0


def cmd_operator(args) -> int:
    import uvicorn

    from cua.escalation.console import build
    broker = Broker(target_base=TARGET, vnc_url=VNC_URL)
    uvicorn.run(build(broker), host="0.0.0.0", port=args.port, log_level="info")
    return 0


# -------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cua", description=__doc__.split("\n")[0])
    ap.add_argument("--store", default="capabilities")
    ap.add_argument("--evidence", default="evidence")
    sub = ap.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="LLM-driven discovery run")
    d.add_argument("--goal", required=True)
    d.add_argument("--entry-url")
    d.add_argument("--checkpoint", required=True,
                   help="text that must be on screen for the goal to count as met")
    d.add_argument("--save", help="capability id to record, e.g. member.read_balance")
    d.add_argument("--title")
    d.add_argument("--tenant", default="northstar")
    d.add_argument("--app-version", default="7.2.1")
    d.add_argument("--param", action="append", default=[],
                   help="k=v used in this run; parameterised AND declared")
    d.add_argument("--output", action="append", default=[],
                   help="name:type:anchor:regex[:span_px] the capability returns")
    d.add_argument("--model", default="claude-opus-5")
    d.add_argument("--max-steps", type=int, default=40)
    d.add_argument("--reset", action="store_true")
    d.add_argument("--overwrite", action="store_true")
    d.add_argument("--collection", default="members",
                   help="landing path after sign-on, e.g. members or pb/customers")
    d.add_argument("--operator", default="teller1")
    d.add_argument("--password", default="hunter2")
    d.add_argument("--no-login", action="store_true",
                   help="assume a session already exists")
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("replay", help="deterministic replay, no model in the loop")
    r.add_argument("ref")
    r.add_argument("--param", action="append", default=[])
    r.add_argument("--policy")
    r.add_argument("--allow-irreversible", action="store_true")
    r.add_argument("--profile", help="arm a target fault profile (harness-side)")
    r.add_argument("--override",
                   help='JSON knob overrides, e.g. \'{"error_500_on":["search"]}\'')
    r.add_argument("--reset", action="store_true",
                   help="reset target state first; required for comparable runs")
    r.add_argument("--operator", default="teller1")
    r.add_argument("--password", default="hunter2")
    r.add_argument("--no-login", action="store_true",
                   help="assume a session already exists")
    r.add_argument("--operator-console", action="store_true",
                   help="serve the takeover console and block on escalation")
    r.add_argument("--escalation-timeout", type=float, default=600.0)
    r.add_argument("--max-handoffs", type=int, default=2,
                   help="how many times one run may be handed to a human")
    r.set_defaults(func=cmd_replay)

    c = sub.add_parser("catalog", help="what an agent could invoke")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_catalog)

    a = sub.add_parser("approve", help="mark a reviewed capability approved")
    a.add_argument("ref")
    a.add_argument("--force", action="store_true")
    a.set_defaults(func=cmd_approve)

    o = sub.add_parser("operator", help="run the takeover console standalone")
    o.add_argument("--port", type=int, default=CONSOLE_PORT)
    o.set_defaults(func=cmd_operator)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
