"""Tests for the mock teller.

These assert the *surface contract* the automation project will code against:
which conditions are reachable, what each one looks like on the wire, and that
two identical runs really do produce identical output. If a test here changes,
the automation's recorded artifacts may need re-recording -- that is the point
of pinning them.
"""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from mock_teller import app as appmod
from mock_teller.app import app
from mock_teller.tenants import NORTHSTAR, PINEBANK

OPERATOR = {"ctl00$cph1$txtOperator": "teller1", "ctl00$cph1$txtPassword": "hunter2"}
DRAFT = {
    "ctl00$cph1$txtNickname": "Vacation Fund",
    "ctl00$cph1$txtDeposit": "250.00",
    "ctl00$cph1$ddlPurpose": "Vacation",
    "ctl00$cph1$ddlStatements": "Electronic",
}


@pytest.fixture(autouse=True)
def clean_state():
    """Total reset around every test. Order-independence is not optional here:
    the whole value proposition is that a run starts from a known state."""
    appmod.sessions.reset()
    appmod.members.reset()
    yield
    appmod.sessions.reset()
    appmod.members.reset()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def signon(c, tenant=NORTHSTAR):
    r = c.post(tenant.path("login"), data=OPERATOR, follow_redirects=True)
    assert r.status_code == 200
    return c


def arm(c, profile, overrides=None):
    r = c.post("/_control/scenario",
               json={"profile": profile, "overrides": overrides or {}},
               headers={"content-type": "application/json"})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- happy path

def test_happy_path_reaches_the_savings_balance(client):
    signon(client)
    r = client.get("/members", params={"memberNumber": "10001"})
    assert r.status_code == 200
    assert "Dana Reyes" in r.text
    assert "/members/10001" in r.text

    r = client.get("/members/10001")
    assert r.status_code == 200
    # The balance is NOT on this page -- it is in the child document.
    assert "4,182.55" not in r.text
    assert 'src="/frame/members/10001"' in r.text

    r = client.get("/frame/members/10001")
    assert r.status_code == 200
    assert "4,182.55" in r.text
    assert "SAV-10001-01" in r.text


def test_login_is_required(client):
    r = client.get("/members/10001", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


def test_short_password_is_a_validation_error_not_a_crash(client):
    r = client.post("/login", data={**OPERATOR, "ctl00$cph1$txtPassword": "x"})
    assert r.status_code == 200
    assert "at least" in r.text


# ------------------------------------------- expected business outcomes (§3.3)

def test_member_not_found_is_a_result_not_a_failure(client):
    signon(client)
    r = client.get("/members", params={"memberNumber": "99999"})
    assert r.status_code == 200          # the SEARCH succeeded
    assert "No member record was found" in r.text


def test_frozen_account_is_reported_on_the_positions_pane(client):
    signon(client)
    r = client.get("/frame/members/10002")
    assert r.status_code == 200
    assert "Frozen" in r.text
    assert "cannot be transacted against" in r.text


def test_permission_denial_is_a_403_with_an_explanation(client):
    signon(client)
    r = client.get("/members/10003")
    assert r.status_code == 403
    assert "not authorised" in r.text
    assert "E-403-PROFILE" in r.text


def test_max_subaccounts_is_a_409_business_outcome(client):
    signon(client)
    r = client.post("/members/10004/subaccounts/new", data=DRAFT)
    assert r.status_code == 409
    assert "maximum of 3 sub-accounts" in r.text


def test_ambiguous_surname_returns_all_matches(client):
    signon(client)
    r = client.get("/members", params={"lastName": "Lee"})
    assert r.status_code == 200
    for member_no in ("10006", "10007", "10008"):
        assert f"<td>{member_no}</td>" in r.text
    assert "More than one member matched" in r.text


@pytest.mark.parametrize("field,value,expected", [
    ("ctl00$cph1$txtNickname", "", "Nickname is required"),
    ("ctl00$cph1$ddlPurpose", "", "Purpose must be selected"),
    ("ctl00$cph1$txtDeposit", "abc", "must be a numeric amount"),
    ("ctl00$cph1$txtDeposit", "5", "at least 25.00 USD"),
])
def test_field_validation_returns_the_form_with_a_message(client, field, value, expected):
    signon(client)
    r = client.post("/members/10001/subaccounts/new", data={**DRAFT, field: value})
    assert r.status_code == 200
    assert expected in r.text


# ------------------------------------------------- recoverable conditions (§3.3)

def test_transient_503_then_success(client):
    signon(client)
    arm(client, "flaky")
    r1 = client.get("/members", params={"memberNumber": "10001"})
    assert r1.status_code == 503
    assert r1.headers["retry-after"] == "1"
    r2 = client.get("/members", params={"memberNumber": "10001"})
    assert r2.status_code == 200
    assert "Dana Reyes" in r2.text


def test_interstitial_appears_every_nth_view_and_clears_on_reload(client):
    signon(client)
    arm(client, "clean", {"interstitial_every_n": 2})
    seen = [("ctl00_shade" in client.get("/members/10001").text) for _ in range(4)]
    # Assert the cadence, not the phase: sign-on already consumed a page view,
    # so which parity the modal lands on depends on preceding traffic. What the
    # contract actually promises is "every 2nd view, and a reload clears it".
    assert sum(seen) == 2, seen
    assert all(seen[i] != seen[i + 1] for i in range(3)), seen


def test_spinner_defers_the_data_until_a_second_navigation(client):
    signon(client)
    arm(client, "slow", {"latency_ms": {"nav": 0, "search": 0, "submit": 0}})
    r = client.get("/frame/members/10001")
    assert "Retrieving account positions" in r.text
    assert "4,182.55" not in r.text          # genuinely absent, not just hidden
    assert "_ready=1" in r.text
    r = client.get("/frame/members/10001", params={"_ready": "1"})
    assert "4,182.55" in r.text


# ------------------------------------------------------- hard failures (§3.3)

def test_commit_route_500s_under_the_broken_profile(client):
    signon(client)
    arm(client, "broken")
    r = client.post("/members/10001/subaccounts/new", data=DRAFT)
    assert r.status_code == 500
    assert "Unhandled Exception" in r.text
    assert "Correlation Id" in r.text


def test_expired_session_bounces_to_login_with_a_resume_target(client):
    signon(client)
    r = client.get("/members/10001", params={"_inject": "expire"}, follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "expired=1" in loc and "next=/members/10001" in loc


def test_hanging_route_never_responds(monkeypatch):
    """The hanging route produces no response inside the caller's window.

    ASGITransport is in-process, so httpx's own timeouts do not apply -- there
    is no socket to time out. asyncio.wait_for tests the property that actually
    matters: the application yields nothing before the caller gives up.
    HANG_SECONDS is shortened only so the stalled task cannot outlive the test.
    """
    monkeypatch.setattr(appmod, "HANG_SECONDS", 3)

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await c.post("/login", data=OPERATOR, follow_redirects=True)
            await c.post("/_control/scenario", json={"profile": "hanging"},
                         headers={"content-type": "application/json"})
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    c.get("/members", params={"memberNumber": "10001"}), timeout=0.4)
            # A route NOT in hang_on still answers promptly under the same profile.
            r = await asyncio.wait_for(c.get("/members/10001"), timeout=2.0)
            assert r.status_code == 200

    asyncio.run(go())


# -------------------------------------------------------- escalation (§3.6)

def test_override_member_blocks_commit_until_a_code_is_supplied(client):
    signon(client)
    r = client.post("/members/10005/subaccounts/new", data=DRAFT)
    assert r.status_code == 200
    assert "Supervisor authorisation is required" in r.text

    # The agent cannot obtain this code -- this is the stuck state.
    r = client.post("/members/10005/subaccounts/confirm", data={})
    assert r.status_code == 403

    r = client.post("/members/10005/subaccounts/confirm",
                    data={"ctl00$cph1$txtOverride": "BADCODE"})
    assert r.status_code == 403
    assert "not valid" in r.text

    # A human supplies it on the SAME session and the flow completes.
    r = client.post("/members/10005/subaccounts/confirm",
                    data={"ctl00$cph1$txtOverride": "SUP-4471"})
    assert r.status_code == 200
    assert "CNF-" in r.text


def test_locked_down_profile_demands_override_for_any_member(client):
    signon(client)
    arm(client, "locked_down")
    r = client.post("/members/10001/subaccounts/new", data=DRAFT)
    assert "Supervisor authorisation is required" in r.text


def test_handoff_records_both_actors_on_one_session(client):
    signon(client)
    sid = appmod.sessions.sessions()[0]["sid"]

    client.get("/members/10001")
    r = client.post("/_control/handoff", json={"sid": sid, "actor": "human"},
                    headers={"content-type": "application/json"})
    assert r.json()["previous_actor"] == "agent"
    client.get("/members/10005")
    client.post("/_control/handoff", json={"sid": sid, "actor": "agent"},
                headers={"content-type": "application/json"})

    audit = client.get("/_control/state", params={"session": sid}).json()["audit"]
    assert {e["session_id"] for e in audit} == {sid}   # one continuous session
    actors = [e["actor"] for e in audit]
    assert "agent" in actors and "human" in actors
    # The member viewed while the human held control is attributed to the human.
    human_views = [e for e in audit if e["actor"] == "human" and "10005" in e["detail"]]
    assert human_views


# ------------------------------------------------------------- determinism

def _full_run(c):
    signon(c)
    c.post("/members/10001/subaccounts/new", data=DRAFT)
    r = c.post("/members/10001/subaccounts/confirm", data={})
    assert r.status_code == 200
    return r.text


def test_two_runs_across_a_reset_are_byte_identical(client):
    first = _full_run(client)
    client.post("/_control/reset", headers={"content-type": "application/json"})
    client.cookies.clear()
    second = _full_run(client)
    assert first == second, "replay determinism broken"
    assert "CNF-000001" in first


# ------------------------------------------------- replayed irreversible write

def test_replayed_commit_is_a_duplicate_not_a_second_account(client):
    """The condition this whole system has to get right.

    A replay re-runs GET form -> POST new -> POST confirm, which stages a fresh
    draft and therefore sails straight past the E-440-NODRAFT guard. Without a
    dedupe check that opens a SECOND real account. The caller is handed the
    original confirmation back, which is the idempotent answer a replay needs.
    """
    first = _full_run(client)
    assert "CNF-000001" in first
    assert appmod.members.get("10001").sub_count == 1

    # Replay the same flow on the same state, with no reset in between.
    client.post("/members/10001/subaccounts/new", data=DRAFT)
    r = client.post("/members/10001/subaccounts/confirm", data={})

    assert r.status_code == 409
    assert "E-409-DUPLICATE" in r.text
    assert "CNF-000001" in r.text, "the ORIGINAL confirmation must come back"
    assert "SAV-10001-03" in r.text
    # The point of the whole test: no second account was opened.
    assert appmod.members.get("10001").sub_count == 1

    # And it keeps reporting the same thing rather than degrading to NODRAFT.
    again = client.post("/members/10001/subaccounts/confirm", data={})
    assert again.status_code == 409
    assert "E-409-DUPLICATE" in again.text


def test_a_different_nickname_is_not_a_duplicate(client):
    """The dedupe key is (member, nickname), not the member alone -- otherwise
    it would block legitimate second sub-accounts."""
    _full_run(client)
    assert appmod.members.get("10001").sub_count == 1

    other = dict(DRAFT, **{"ctl00$cph1$txtNickname": "Emergency Fund"})
    client.post("/members/10001/subaccounts/new", data=other)
    r = client.post("/members/10001/subaccounts/confirm", data={})

    assert r.status_code == 200
    assert "CNF-000002" in r.text
    assert appmod.members.get("10001").sub_count == 2


# --------------------------------------------------------- native dialog

def test_native_confirm_guards_the_commit_only_when_armed(client):
    """Asserts the handler is in the markup, NOT that the dialog fires --
    TestClient runs no JS. The dialog itself is a browser check."""
    signon(client)

    # Match the handler itself, not the word "confirm" -- the template carries
    # an explanatory comment that mentions window.confirm() either way.
    handler = 'onclick="return confirm('

    clean = client.post("/members/10001/subaccounts/new", data=DRAFT)
    assert clean.status_code == 200
    assert handler not in clean.text

    armed = client.post("/members/10001/subaccounts/new?_inject=native_confirm",
                        data=DRAFT)
    assert armed.status_code == 200
    assert handler in armed.text
    # It guards the commit control specifically, not some other button.
    assert "ctl00_cph1_btnConfirm" in armed.text


def test_fault_correlation_id_is_stable_across_requests(client):
    """hash() is salted per process, so a hash-derived id would change on every
    server restart -- which is the determinism the README claims. This catches
    the in-process half; surviving a restart needs a real restart to see."""
    import re
    signon(client)

    def fault_id():
        r = client.post("/members/10001/subaccounts/new?_inject=error_500",
                        data=DRAFT)
        assert r.status_code == 500
        m = re.search(r"CT-\d{8}", r.text)
        assert m, r.text
        return m.group(0)

    assert fault_id() == fault_id()


# --------------------------------------------------------------- multi-tenant

def test_tenant_b_serves_the_same_flow_under_different_names(client):
    signon(client, PINEBANK)
    r = client.get("/pb/customers", params={"q": "10001"})
    assert r.status_code == 200
    assert "Pinebank Servicing" in r.text
    assert "Customer Number" in r.text
    assert "/pb/customers/10001" in r.text

    r = client.get("/frame/pb/customers/10001")
    assert "4,182.55" in r.text          # same data, same vendor product


def test_tenants_order_the_same_form_fields_differently(client):
    signon(client, NORTHSTAR)
    a = client.get("/members/10001/subaccounts/new").text
    signon(client, PINEBANK)
    b = client.get("/pb/customers/10001/subaccounts/new").text

    def order(html):
        import re
        return [m for m in re.findall(r"ctl00_cph1_(txt\w+|ddl\w+)", html)]

    assert order(a) != order(b), "tenant field order should differ"
    assert set(order(a)) == set(order(b)), "same fields, different sequence"


# ---------------------------------------------------------- control plane

def test_unknown_profile_is_rejected(client):
    r = client.post("/_control/scenario", json={"profile": "nope"},
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert "known" in r.json()


def test_dangerous_admin_route_exists_for_the_allowlist_to_refuse(client):
    r = client.get("/admin/members/10001/close")
    assert r.status_code == 200
    assert "cannot be undone" in r.text
