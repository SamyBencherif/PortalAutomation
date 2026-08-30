"""Mock teller app: routes, and the points where scenarios are injected.

Read this file alongside scenarios.py. Every handler resolves knobs for its
route class first, then calls `gate()`, which is the single place where a
request can be delayed, 503'd, 500'd, hung, expired, or interstitialed. Keeping
all injection in one function is what makes the failure taxonomy auditable
rather than sprinkled through the handlers.
"""

import asyncio
import time
import zlib
from pathlib import Path

from fastapi import APIRouter, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import fixtures as fx
from .fixtures import MemberStore
from .scenarios import (
    DEFAULT_PROFILE, PROFILES, ROUTE_NAV, ROUTE_SEARCH, ROUTE_SUBMIT,
    profile_names, resolve,
)
from .state import ACTOR_AGENT, ACTOR_HUMAN, Store
from .tenants import NORTHSTAR, PINEBANK, TENANTS, Tenant

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

COOKIE = "CORETELLERSESSID"
PURPOSES = ["Vacation", "Emergency Fund", "Education", "Home Improvement"]
OVERRIDE_HINT = "Supervisor override code required."
# How long a "hanging" route stalls. Long enough to be a hang from any
# client's point of view; bounded so tests can wait it out rather than
# leaking a task that outlives the event loop.
HANG_SECONDS = 600

# Mutable server state, in exactly two objects so reset is trivial and total.
sessions = Store()
members = MemberStore()


class Halt(Exception):
    """Raised by gate() to abort a handler with a pre-built response."""

    def __init__(self, response: Response) -> None:
        self.response = response


# --------------------------------------------------------------------------
# session + scenario plumbing
# --------------------------------------------------------------------------

def _session(request: Request):
    return sessions.get(request.cookies.get(COOKIE))


def _knobs(request: Request, route: str):
    s = _session(request)
    profile = s.profile if s else DEFAULT_PROFILE
    overrides = s.overrides if s else {}
    inject = request.query_params.get("_inject") or request.headers.get("x-mock-inject")
    return resolve(profile, overrides, inject, route)


def _ctx(request: Request, tenant: Tenant, **extra) -> dict:
    s = _session(request)
    base = {
        "request": request,
        "tenant": tenant,
        "session": s,
        "today": fx.TODAY.isoformat(),
        "page_title": extra.get("page_title", tenant.noun + " Servicing"),
        "interstitial": False,
        "native_confirm": False,
        "dismiss_url": str(request.url.replace(scheme="", netloc="")) or request.url.path,
    }
    base.update(extra)
    return base


async def gate(request: Request, tenant: Tenant, route: str):
    """The single injection point. Returns (session, knobs) or raises Halt.

    Order matters and is deliberate: hang, then hard failure, then session
    expiry, then transient failure, then latency. Cheapest-to-detect conditions
    are not allowed to mask more serious ones.
    """
    knobs = _knobs(request, route)
    s = _session(request)

    # 1. Never responds. The client's own timeout is the only thing that ends it.
    if route in knobs.hang_on:
        await asyncio.sleep(HANG_SECONDS)

    # 2. Hard failure: a 500 the automation cannot recover from.
    if route in knobs.error_500_on:
        if s:
            sessions.audit(s.sid, s.actor, route, request.url.path, "server fault 500")
        raise Halt(_fault(request, tenant, route))

    # 3. Session expiry -> bounced to login. Distinct from a hard failure:
    #    a caller could re-authenticate and resume.
    if s and sessions.is_expired(s, knobs.session_ttl_s):
        sessions.audit(s.sid, s.actor, route, request.url.path, "session expired")
        sessions.drop(s.sid)
        resp = RedirectResponse(
            f"{tenant.path('login')}?expired=1&next={request.url.path}", status_code=303
        )
        resp.delete_cookie(COOKIE)
        raise Halt(resp)

    # 4. Recoverable: transient 503 with Retry-After for the first N hits.
    if s is not None:
        limit = knobs.transient_503_first_n.get(route, 0)
        if limit:
            hits = s.bump(f"503:{route}")
            if hits <= limit:
                sessions.audit(s.sid, s.actor, route, request.url.path, f"transient 503 ({hits}/{limit})")
                raise Halt(
                    HTMLResponse(
                        "<html><body><h1>503 Service Unavailable</h1>"
                        "<p>The servicing host is temporarily busy. Please retry.</p>"
                        "</body></html>",
                        status_code=503,
                        headers={"Retry-After": "1"},
                    )
                )

    # 5. Latency last, so a slow profile does not delay a request that was
    #    going to fail instantly anyway.
    delay = knobs.latency_ms.get(route, 0)
    if delay:
        await asyncio.sleep(delay / 1000)

    if s:
        sessions.touch(s)
    return s, knobs


def _interstitial_due(s, knobs) -> bool:
    """Maintenance modal on every Nth page view, counted per session.

    There is no separate "dismissed" flag, and there does not need to be:
    Dismiss is a link back to the current URL, which advances page_views to
    N+1. Consecutive integers cannot both be multiples of N (for N > 1), so
    the reload never re-raises the modal it just dismissed.
    """
    if not s or knobs.interstitial_every_n < 2:
        return False
    return s.page_views > 0 and s.page_views % knobs.interstitial_every_n == 0


def _render(request, tenant, name, ctx, s=None, knobs=None, status=200):
    if s is not None:
        s.page_views += 1
        if knobs is not None and _interstitial_due(s, knobs):
            ctx["interstitial"] = True
    # Set alongside the interstitial so both dialog conditions are injected in
    # exactly one place. The native confirm guards the commit control, which is
    # a ROUTE_SUBMIT action wherever it happens to be rendered.
    if knobs is not None:
        ctx["native_confirm"] = ROUTE_SUBMIT in knobs.native_confirm_on
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def _fault(request: Request, tenant: Tenant, route: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "fault.html",
        {"request": request, "tenant": tenant, "route": route,
         # crc32, not hash(): Python salts string hashing per process, so hash()
         # would give this page a different correlation id after every server
         # restart -- which is exactly the determinism the README claims.
         "correlation_id": f"CT-{zlib.crc32(request.url.path.encode()) % 10**8:08d}"},
        status_code=500,
    )


def _require_login(request: Request, tenant: Tenant, s):
    if s is None or s.user is None:
        raise Halt(RedirectResponse(
            f"{tenant.path('login')}?next={request.url.path}", status_code=303))


def _msg(request, tenant, s, knobs, heading, message, cls="msg-warn", code=None, status=200):
    return _render(request, tenant, "message.html",
                   _ctx(request, tenant, page_title=heading, heading=heading,
                        message=message, message_class=cls, code=code,
                        back_url=tenant.path(tenant.collection)),
                   s, knobs, status=status)


# --------------------------------------------------------------------------
# tenant routes -- one router, mounted twice
# --------------------------------------------------------------------------

def build_router(tenant: Tenant) -> APIRouter:
    r = APIRouter()
    col = tenant.collection

    @r.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        s, knobs = await gate(request, tenant, ROUTE_NAV)
        return _render(request, tenant, "login.html",
                       _ctx(request, tenant, page_title="Sign On", message=None,
                            expired=request.query_params.get("expired"),
                            next_url=request.query_params.get("next")), s, knobs)

    @r.post("/login")
    async def login_submit(request: Request):
        s, knobs = await gate(request, tenant, ROUTE_NAV)
        form = await request.form()
        operator = (form.get("ctl00$cph1$txtOperator") or "").strip()
        password = (form.get("ctl00$cph1$txtPassword") or "").strip()
        next_url = form.get("ctl00$cph1$hidNext") or tenant.path(col)

        if len(password) < 4 or not operator:
            # A validation error, not a crash: an expected business outcome.
            return _render(request, tenant, "login.html",
                           _ctx(request, tenant, page_title="Sign On",
                                message="Operator ID and a password of at least "
                                        "4 characters are required.",
                                expired=None, next_url=next_url), s, knobs, status=200)

        if s is None:
            s = sessions.new_session()
        s.user = operator
        sessions.touch(s)
        sessions.audit(s.sid, s.actor, ROUTE_NAV, request.url.path, f"sign on as {operator}")
        resp = RedirectResponse(next_url, status_code=303)
        resp.set_cookie(COOKIE, s.sid, httponly=False, samesite="lax")
        return resp

    @r.get("/logout")
    async def logout(request: Request):
        s = _session(request)
        if s:
            sessions.audit(s.sid, s.actor, ROUTE_NAV, request.url.path, "sign off")
            sessions.drop(s.sid)
        resp = RedirectResponse(tenant.path("login"), status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @r.get(f"/{col}", response_class=HTMLResponse)
    async def search(request: Request):
        s, knobs = await gate(request, tenant, ROUTE_SEARCH)
        _require_login(request, tenant, s)

        number = (request.query_params.get(tenant.search_param) or "").strip()
        name = (request.query_params.get(tenant.name_param) or "").strip()
        ctx = _ctx(request, tenant, page_title=f"{tenant.noun} Search",
                   q_number=number, q_name=name, results=None,
                   message=None, message_class="msg-info")

        if number:
            sessions.audit(s.sid, s.actor, ROUTE_SEARCH, request.url.path, f"by number {number}")
            m = members.get(number)
            if m is None:
                # "No such member" is a RESULT the caller needs, not a failure.
                ctx["message"] = (f"No {tenant.noun.lower()} record was found for "
                                  f"{tenant.noun.lower()} number {number}.")
                ctx["message_class"] = "msg-warn"
            else:
                ctx["results"] = [m]
        elif name:
            sessions.audit(s.sid, s.actor, ROUTE_SEARCH, request.url.path, f"by name {name}")
            hits = members.search_by_name(name)
            if not hits:
                ctx["message"] = f"No {tenant.noun.lower()} records matched '{name}'."
                ctx["message_class"] = "msg-warn"
            else:
                ctx["results"] = hits

        return _render(request, tenant, "search.html", ctx, s, knobs)

    @r.get(f"/{col}/{{member_no}}", response_class=HTMLResponse)
    async def detail(request: Request, member_no: str):
        s, knobs = await gate(request, tenant, ROUTE_NAV)
        _require_login(request, tenant, s)

        m = members.get(member_no)
        if m is None:
            return _msg(request, tenant, s, knobs, f"{tenant.noun} Not Found",
                        f"No {tenant.noun.lower()} record exists for number {member_no}.",
                        "msg-warn", code=f"E-404-{member_no}", status=404)

        if m.outcome == fx.OUTCOME_RESTRICTED:
            # Permission denial: a legitimate outcome the caller must be told
            # about, not an error to retry.
            sessions.audit(s.sid, s.actor, ROUTE_NAV, request.url.path, "permission denied")
            return _msg(request, tenant, s, knobs, "Access Restricted",
                        f"Your operator profile is not authorised to view "
                        f"{tenant.noun.lower()} {member_no}. Contact your branch "
                        f"supervisor to request access.",
                        "msg-error", code="E-403-PROFILE", status=403)

        sessions.audit(s.sid, s.actor, ROUTE_NAV, request.url.path, f"viewed {member_no}")
        frame = f"/frame{tenant.prefix}/{col}/{member_no}"
        return _render(request, tenant, "detail.html",
                       _ctx(request, tenant, page_title=f"{tenant.noun} {member_no}",
                            member=m, frame_url=frame), s, knobs)

    @r.get(f"/{col}/{{member_no}}/subaccounts/new", response_class=HTMLResponse)
    async def subaccount_form(request: Request, member_no: str):
        s, knobs = await gate(request, tenant, ROUTE_NAV)
        _require_login(request, tenant, s)
        m = members.get(member_no)
        if m is None:
            return _msg(request, tenant, s, knobs, f"{tenant.noun} Not Found",
                        f"No {tenant.noun.lower()} record exists for number {member_no}.",
                        "msg-warn", status=404)
        return _render(request, tenant, "subaccount_new.html",
                       _ctx(request, tenant, page_title="Open Sub-Account", member=m,
                            purposes=PURPOSES, values={}, message=None,
                            action_url=tenant.path(col, member_no, "subaccounts/new")),
                       s, knobs)

    @r.post(f"/{col}/{{member_no}}/subaccounts/new", response_class=HTMLResponse)
    async def subaccount_review(request: Request, member_no: str):
        s, knobs = await gate(request, tenant, ROUTE_SUBMIT)
        _require_login(request, tenant, s)
        m = members.get(member_no)
        if m is None:
            return _msg(request, tenant, s, knobs, f"{tenant.noun} Not Found",
                        f"No {tenant.noun.lower()} record exists for number {member_no}.",
                        "msg-warn", status=404)

        form = await request.form()
        values = {
            "nickname": (form.get("ctl00$cph1$txtNickname") or "").strip(),
            "deposit": (form.get("ctl00$cph1$txtDeposit") or "").strip(),
            "purpose": (form.get("ctl00$cph1$ddlPurpose") or "").strip(),
            "statements": (form.get("ctl00$cph1$ddlStatements") or "Electronic").strip(),
        }

        def reject(msg):
            return _render(request, tenant, "subaccount_new.html",
                           _ctx(request, tenant, page_title="Open Sub-Account",
                                member=m, purposes=PURPOSES, values=values, message=msg,
                                action_url=tenant.path(col, member_no, "subaccounts/new")),
                           s, knobs)

        # Field validation -- expected business outcomes, every one of them.
        if not values["nickname"]:
            return reject("Nickname is required.")
        if not values["purpose"]:
            return reject("Purpose must be selected.")
        try:
            amount = float(values["deposit"].replace(",", ""))
        except ValueError:
            return reject("Initial Deposit must be a numeric amount, e.g. 100.00")
        if amount < 25:
            return reject("Initial Deposit must be at least 25.00 USD.")

        if m.sub_count >= fx.MAX_SUBACCOUNTS:
            sessions.audit(s.sid, s.actor, ROUTE_SUBMIT, request.url.path, "max sub-accounts")
            return _msg(request, tenant, s, knobs, "Limit Reached",
                        f"{tenant.noun} {member_no} already holds the maximum of "
                        f"{fx.MAX_SUBACCOUNTS} sub-accounts. No further sub-accounts "
                        f"may be opened.", "msg-warn", code="E-409-MAXSUB", status=409)

        s.draft = dict(values, member_no=member_no)
        sessions.audit(s.sid, s.actor, ROUTE_SUBMIT, request.url.path, "draft staged")
        needs = knobs.require_override_code or m.outcome == fx.OUTCOME_NEEDS_OVERRIDE
        return _render(request, tenant, "subaccount_review.html",
                       _ctx(request, tenant, page_title="Confirm Sub-Account",
                            member=m, draft=s.draft, needs_override=needs, message=None,
                            confirm_url=tenant.path(col, member_no, "subaccounts/confirm"),
                            back_url=tenant.path(col, member_no, "subaccounts/new")),
                       s, knobs)

    @r.post(f"/{col}/{{member_no}}/subaccounts/confirm", response_class=HTMLResponse)
    async def subaccount_confirm(request: Request, member_no: str):
        s, knobs = await gate(request, tenant, ROUTE_SUBMIT)
        _require_login(request, tenant, s)
        m = members.get(member_no)
        if m is None or not s.draft or s.draft.get("member_no") != member_no:
            return _msg(request, tenant, s, knobs, "Session Data Lost",
                        "The sub-account request could not be found. Please start again.",
                        "msg-error", code="E-440-NODRAFT", status=409)

        form = await request.form()
        override = (form.get("ctl00$cph1$txtOverride") or "").strip()
        needs = knobs.require_override_code or m.outcome == fx.OUTCOME_NEEDS_OVERRIDE

        if needs and not override:
            # The stuck state. The agent has no way to obtain this code, so the
            # correct behaviour is to escalate rather than guess.
            sessions.audit(s.sid, s.actor, ROUTE_SUBMIT, request.url.path,
                           "blocked: supervisor override required")
            return _render(request, tenant, "subaccount_review.html",
                           _ctx(request, tenant, page_title="Confirm Sub-Account",
                                member=m, draft=s.draft, needs_override=True,
                                message=OVERRIDE_HINT,
                                confirm_url=tenant.path(col, member_no, "subaccounts/confirm"),
                                back_url=tenant.path(col, member_no, "subaccounts/new")),
                           s, knobs, status=403)

        if needs and not override.startswith("SUP-"):
            return _render(request, tenant, "subaccount_review.html",
                           _ctx(request, tenant, page_title="Confirm Sub-Account",
                                member=m, draft=s.draft, needs_override=True,
                                message="The supervisor override code is not valid.",
                                confirm_url=tenant.path(col, member_no, "subaccounts/confirm"),
                                back_url=tenant.path(col, member_no, "subaccounts/new")),
                           s, knobs, status=403)

        # A replayed commit is the normal case for this system, so it gets a
        # result of its own rather than falling through to E-440-NODRAFT (which
        # means the draft was genuinely lost -- a different condition). The
        # caller is handed the ORIGINAL confirmation back, which is the
        # idempotent answer a replay engine needs. The draft is deliberately
        # left in place so repeat attempts keep reporting the same thing.
        prior = members.find_commit(member_no, s.draft["nickname"])
        if prior is not None:
            prior_acct, prior_confirmation = prior
            sessions.audit(s.sid, s.actor, ROUTE_SUBMIT, request.url.path,
                           f"duplicate: already committed as {prior_acct} "
                           f"({prior_confirmation})")
            return _msg(request, tenant, s, knobs, "Sub-Account Already Exists",
                        f"A sub-account nicknamed '{s.draft['nickname']}' is already "
                        f"open for {tenant.noun.lower()} {member_no} as "
                        f"{prior_acct}, confirmation {prior_confirmation}. "
                        f"No new account was opened.",
                        "msg-warn", code="E-409-DUPLICATE", status=409)

        acct, confirmation = members.add_subaccount(
            member_no, s.draft["nickname"], s.draft["deposit"])
        sessions.audit(s.sid, s.actor, ROUTE_SUBMIT, request.url.path,
                       f"committed {acct.number} ({confirmation})"
                       + (f" via override by {s.actor}" if needs else ""))
        s.draft = None
        return _render(request, tenant, "subaccount_done.html",
                       _ctx(request, tenant, page_title="Sub-Account Opened",
                            member=m, account=acct, confirmation=confirmation,
                            back_url=tenant.path(col, member_no)), s, knobs)

    return r


def build_frame_router(tenant: Tenant) -> APIRouter:
    """The iframe body. Separate router so its path never collides with a page."""
    r = APIRouter()

    @r.get(f"{tenant.prefix}/{tenant.collection}/{{member_no}}", response_class=HTMLResponse)
    async def frame_detail(request: Request, member_no: str):
        s, knobs = await gate(request, tenant, ROUTE_NAV)
        m = members.get(member_no)
        if m is None:
            return HTMLResponse("<html><body>No account data.</body></html>", status_code=404)

        ready = request.query_params.get("_ready") == "1"
        spinner = ("detail" in knobs.spinner_pages) and not ready
        note = None
        if m.outcome == fx.OUTCOME_FROZEN:
            note = ("One or more accounts are frozen. Balances are shown for "
                    "reference only and cannot be transacted against.")
        return templates.TemplateResponse(
            request, "frame_detail.html",
            {"request": request, "tenant": tenant, "accounts": m.accounts,
             "spinner": spinner, "spinner_delay_ms": knobs.spinner_delay_ms,
             "frozen_note": note,
             "ready_url": f"{request.url.path}?_ready=1"},
        )

    return r


# --------------------------------------------------------------------------
# app assembly
# --------------------------------------------------------------------------

app = FastAPI(title="Mock Teller", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.exception_handler(Halt)
async def _halt_handler(request: Request, exc: Halt):
    return exc.response


@app.middleware("http")
async def halt_middleware(request: Request, call_next):
    try:
        return await call_next(request)
    except Halt as h:
        return h.response


from . import control  # noqa: E402  (imports `app` state above)

app.include_router(control.router, prefix="/_control")

# The dangerous route. Nothing in the demo flows needs it; it exists so the
# automation's allowlist has a real irreversible action to refuse.
@app.get("/admin/{collection}/{member_no}/close", response_class=HTMLResponse)
async def admin_close(request: Request, collection: str, member_no: str):
    return HTMLResponse(
        "<html><body><h1>Close Record</h1>"
        f"<p>Permanently close record {member_no}? This cannot be undone.</p>"
        '<form method="post"><input type="submit" value="Close Permanently"></form>'
        "</body></html>", status_code=200)


for _t in (PINEBANK, NORTHSTAR):  # longest prefix first
    app.include_router(build_frame_router(_t), prefix="/frame")
for _t in (PINEBANK, NORTHSTAR):
    app.include_router(build_router(_t), prefix=_t.prefix)


@app.get("/")
async def root():
    return RedirectResponse(NORTHSTAR.path("login"), status_code=303)
