"""The out-of-band control plane.

Everything here lives under /_control and is NOT part of the surface under
test. The automation's allowlist must exclude it: an agent that can reconfigure
its own target is not being tested against anything.

It exists so a human -- or a test -- can put the app into a known state, and so
the evidence bundle can read back who did what on which session.
"""

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .scenarios import DEFAULT_PROFILE, PROFILES, profile_names
from .state import ACTOR_AGENT, ACTOR_HUMAN
from .tenants import TENANTS

router = APIRouter()

PROFILE_HELP = (
    "clean = happy path · slow = latency + deferred load · flaky = transient 503 "
    "+ interstitial · broken = 500 on commit · expired = session dies mid-flow · "
    "locked_down = supervisor override required · hanging = search never responds"
)


def _t():
    from . import app as appmod
    return appmod


def _templates():
    return _t().templates


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def panel(request: Request):
    m = _t()
    s = m.sessions.get(request.cookies.get(m.COOKIE))
    return _templates().TemplateResponse(
        request, "control.html",
        {"request": request,
         "profiles": profile_names(),
         "active_profile": s.profile if s else DEFAULT_PROFILE,
         "profile_help": PROFILE_HELP,
         "sessions": m.sessions.sessions(),
         "audit": m.sessions.audit_log(),
         "tenants": list(TENANTS.values())},
    )


@router.post("/scenario")
async def set_scenario(request: Request):
    """Bind a profile (+ optional knob overrides) to the caller's session.

    Accepts JSON or form-encoded, so both curl and the HTML panel work. If the
    caller has no session yet one is minted, so a scenario can be armed *before*
    signing on -- which is the normal order for a scripted run.
    """
    m = _t()
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        body = await request.json()
    else:
        raw = await request.body()
        try:
            body = json.loads(raw) if raw.strip().startswith(b"{") else {}
        except Exception:
            body = {}
        if not body:
            form = await request.form()
            body = dict(form)
            if isinstance(body.get("overrides"), str):
                body["overrides"] = json.loads(body["overrides"])

    profile = body.get("profile", DEFAULT_PROFILE)
    if profile not in PROFILES:
        return JSONResponse(
            {"error": f"unknown profile {profile!r}", "known": profile_names()},
            status_code=400)

    sid = request.cookies.get(m.COOKIE)
    s = m.sessions.get(sid)
    if s is None:
        s = m.sessions.new_session()
    s.profile = profile
    s.overrides = body.get("overrides") or {}
    # Counters are part of the scenario: "fail the first N" must mean the first
    # N *after arming*, or re-arming a profile mid-run would be a no-op.
    s.route_hits.clear()
    m.sessions.audit(s.sid, "system", "control", "/_control/scenario",
                     f"profile={profile} overrides={s.overrides}")

    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in ctype:
        resp = RedirectResponse("/_control", status_code=303)
    else:
        resp = JSONResponse({"ok": True, "session": s.sid, "profile": profile,
                             "overrides": s.overrides})
    resp.set_cookie(m.COOKIE, s.sid, httponly=False, samesite="lax")
    return resp


@router.post("/reset")
async def reset(request: Request):
    """Total reset. This is what makes two runs comparable."""
    m = _t()
    m.sessions.reset()
    m.members.reset()
    if "text/html" in request.headers.get("accept", ""):
        resp = RedirectResponse("/_control", status_code=303)
    else:
        resp = JSONResponse({"ok": True, "reset": True})
    resp.delete_cookie(m.COOKIE)
    return resp


@router.post("/handoff")
async def handoff(request: Request):
    """Transfer control of a live session between agent and human.

    This does not create a session, switch cookies, or fork state -- it flips
    the actor on the session that is already running. That is the whole point:
    the human takes over *the same* session, and every subsequent audit entry is
    stamped with the new actor, so the handoff is evidenced in one continuous log.
    """
    m = _t()
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        body = await request.json()
    else:
        body = dict(await request.form())

    sid = body.get("sid") or request.cookies.get(m.COOKIE)
    actor = body.get("actor", ACTOR_HUMAN)
    if actor not in (ACTOR_AGENT, ACTOR_HUMAN):
        return JSONResponse({"error": f"unknown actor {actor!r}"}, status_code=400)

    s = m.sessions.get(sid)
    if s is None:
        return JSONResponse({"error": f"no such session {sid!r}"}, status_code=404)

    previous, s.actor = s.actor, actor
    m.sessions.audit(s.sid, actor, "control", "/_control/handoff",
                     f"control transferred {previous} -> {actor}")

    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/_control", status_code=303)
    return JSONResponse({"ok": True, "session": s.sid,
                         "previous_actor": previous, "actor": actor})


@router.get("/state")
async def state(request: Request):
    """Machine-readable state + audit log, for evidence bundles and assertions."""
    m = _t()
    sid = request.query_params.get("session")
    return JSONResponse({
        "profiles": profile_names(),
        "sessions": m.sessions.sessions(),
        "audit": m.sessions.audit_log(sid),
        "members": [
            {"member_no": mem.member_no, "name": mem.name,
             "outcome": mem.outcome, "accounts": len(mem.accounts),
             "sub_accounts": mem.sub_count}
            for mem in sorted(m.members._members.values(), key=lambda x: x.member_no)
        ],
    })
