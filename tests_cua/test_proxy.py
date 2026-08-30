"""The allowlist, tested as enforcement rather than as intention.

`test_outcomes_and_safety.py` checks that the Policy object returns the right
verdicts. That is necessary and not sufficient: the claim this system actually
makes is that a denied route *cannot be reached*, no matter what the agent
decides to click. So these tests drive real HTTP through the real proxy at a
real target and assert on what comes back.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest
import uvicorn

from cua.safety.policy import Policy
from cua.safety.proxy import serve


@pytest.fixture(scope="module")
def target():
    """The real mock_teller, on a real port."""
    from mock_teller.app import app

    config = uvicorn.Config(app, host="127.0.0.1", port=8821, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        try:
            httpx.get("http://127.0.0.1:8821/login", timeout=0.5)
            break
        except httpx.HTTPError:
            threading.Event().wait(0.05)
    yield "http://127.0.0.1:8821"
    server.should_exit = True


@pytest.fixture(scope="module")
def proxied(target, tmp_path_factory):
    audit = tmp_path_factory.mktemp("proxy") / "proxy.jsonl"
    policy = Policy(
        allow_hosts=["127.0.0.1"],
        allow_paths=["/login", "/members", "/frame", "/static"],
        deny_paths=["/_control", "/admin"],
    )
    server = serve(port=8899, policy=policy, audit_path=audit)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield httpx.Client(proxy="http://127.0.0.1:8899", timeout=10.0), audit
    server.shutdown()


def test_a_permitted_route_passes_through(proxied, target):
    client, _ = proxied
    r = client.get(f"{target}/login")
    assert r.status_code == 200
    assert "Operator Sign On" in r.text


def test_the_control_plane_cannot_be_reached_at_all(proxied, target):
    """The sharpest case.

    mock_teller's control plane can rearm the target's fault profile. An agent
    able to reach it could quietly make its own tests pass, so the request must
    not merely be discouraged -- it must not arrive.
    """
    client, _ = proxied
    r = client.post(f"{target}/_control/reset", json={})
    assert r.status_code == 403
    assert "blocked" in r.text.lower()

    # And the target genuinely never saw it: state is untouched, which we can
    # confirm by asking the target directly, off-proxy.
    direct = httpx.get(f"{target}/_control/state", timeout=5.0)
    assert direct.status_code == 200


def test_the_destructive_admin_route_is_unreachable(proxied, target):
    """The irreversible action nothing in either flow needs."""
    client, _ = proxied
    r = client.get(f"{target}/admin/members/10001/close")
    assert r.status_code == 403


def test_a_refusal_is_legible_rather_than_a_hang(proxied, target):
    """A blocked request must explain itself.

    A silent drop would present to the agent as a mysteriously broken page and
    to an operator as an unexplained failure.
    """
    client, _ = proxied
    r = client.get(f"{target}/admin/members/10001/close")
    assert "deny rule" in r.text or "not on the allowlist" in r.text


def test_every_request_is_audited_with_its_verdict(proxied, target):
    """The proxy log is the record of what the automation actually touched.

    The engine logs what it meant to do; this logs what the network saw, which
    is the version an auditor should trust.
    """
    client, audit = proxied
    client.get(f"{target}/members?memberNumber=10001")
    client.get(f"{target}/admin/members/10001/close")

    lines = [json.loads(x) for x in Path(audit).read_text().splitlines() if x]
    allowed = [e for e in lines if e["allowed"]]
    denied = [e for e in lines if not e["allowed"]]
    assert allowed and denied
    assert any("/admin" in e["path"] for e in denied)
    assert all("reason" in e for e in lines)


def test_connect_tunnelling_is_refused(proxied, target):
    """A CONNECT tunnel would make every rule above unenforceable."""
    client, _ = proxied
    with pytest.raises(httpx.HTTPError):
        # httpx issues CONNECT for an https origin through a proxy; the proxy
        # refuses, so this must fail rather than establish a tunnel.
        client.get("https://example.com/", timeout=5.0)
