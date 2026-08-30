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

from cua.artifact.schema import Approval
from cua.artifact.store import CapabilityNotFound, Store
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


# --------------------------------------------------------------- harness ops

def _control(path: str, payload: dict[str, Any] | None = None) -> Any:
    """Talk to the target's control plane. Harness-side only, never the agent."""
    url = f"{TARGET.rstrip('/')}/_control{path}"
    try:
        if payload is None:
            return httpx.get(url, timeout=5.0).json()
        r = httpx.post(url, json=payload, headers={"content-type": "application/json"},
                       timeout=5.0)
        return r.json() if r.status_code == 200 else {"error": r.status_code, "body": r.text}
    except httpx.HTTPError as e:
        return {"error": str(e)}


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
    if args.profile:
        _control("/scenario", {"profile": args.profile})

    log = RunLog.create("replay", root=args.evidence)
    broker = Broker(target_base=TARGET, vnc_url=VNC_URL, log=log)
    engine = ReplayEngine(
        _surface(), _policy(args), log,
        credentials=(args.operator, args.password),
        escalate=broker.raise_intervention,
    )

    if args.operator_console:
        serve_in_background(broker, CONSOLE_PORT)
        print(f"operator console: http://localhost:{CONSOLE_PORT}/", file=sys.stderr)

    result = engine.run(cap, params)

    if result.status == "escalated" and args.operator_console:
        print("\n  ESCALATED -- waiting for a human to take control.", file=sys.stderr)
        print(f"  {result.outcome['message']}", file=sys.stderr)
        print(f"  Take over at http://localhost:{CONSOLE_PORT}/\n", file=sys.stderr)
        if broker.wait_for_resume(timeout=args.escalation_timeout):
            # Resume on the SAME session the human just operated.
            print("  control returned; resuming\n", file=sys.stderr)
            result = engine.run(cap, params)
        else:
            print("  nobody took over within the timeout", file=sys.stderr)

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
    loop = AgentLoop(_surface(), log, model=args.model, max_steps=args.max_steps)
    result = loop.run(args.goal, args.entry_url or f"{TARGET}/login")

    print(f"[{result.status}] {result.steps} actions -- {result.stop_reason}")
    if result.final_text:
        print(f"  model: {result.final_text[:500]}")
    print(f"  evidence: {log.root}")

    if result.status != "succeeded":
        return 1
    if not args.save:
        print("  (not saved -- pass --save <capability.id> to record an artifact)")
        return 0

    cap = record(
        result,
        cap_id=args.save,
        title=args.title or args.save,
        description=args.goal,
        app=AppRef(product="coreteller", version=args.app_version,
                   tenant_variant=args.tenant, entry_url=args.entry_url or f"{TARGET}/login"),
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
                   help="k=v used in this run, so it can be parameterised")
    d.add_argument("--model", default="claude-opus-5")
    d.add_argument("--max-steps", type=int, default=40)
    d.add_argument("--reset", action="store_true")
    d.add_argument("--overwrite", action="store_true")
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("replay", help="deterministic replay, no model in the loop")
    r.add_argument("ref")
    r.add_argument("--param", action="append", default=[])
    r.add_argument("--policy")
    r.add_argument("--allow-irreversible", action="store_true")
    r.add_argument("--profile", help="arm a target fault profile (harness-side)")
    r.add_argument("--reset", action="store_true",
                   help="reset target state first; required for comparable runs")
    r.add_argument("--operator", default="teller1")
    r.add_argument("--password", default="hunter2")
    r.add_argument("--operator-console", action="store_true",
                   help="serve the takeover console and block on escalation")
    r.add_argument("--escalation-timeout", type=float, default=600.0)
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
