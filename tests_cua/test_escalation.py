"""Escalation and control transfer.

The brief is explicit that this must be a real mechanism and not a TODO, so
these tests assert on the properties that make it real rather than on the
existence of the code:

- exactly one actor holds control at a time,
- the automation actually blocks while the human has it,
- control comes back to the same run,
- and what the human did is recorded.

The VNC display and the target's control plane are the two things not
exercised here; they need a container and a running app respectively. What is
tested is the state machine that governs them.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cua.escalation.broker import AGENT, HUMAN, Broker
from cua.escalation.console import build
from cua.evidence.run import RunLog


@pytest.fixture
def log(tmp_path: Path) -> RunLog:
    return RunLog.create("test", root=tmp_path)


@pytest.fixture
def broker(log: RunLog) -> Broker:
    # No target reachable: announcing the handoff to the application is
    # best-effort by design, so the transfer must work without it.
    return Broker(target_base="http://127.0.0.1:9", vnc_url="http://vnc/", log=log)


CONTEXT = {
    "capability": "member.open_subaccount@1.0.0",
    "run_id": "replay-test",
    "step": 4,
    "intent": "commit the new sub-account",
    "reason": "A supervisor override code is required to continue.",
    "code": "SUPERVISOR_OVERRIDE_REQUIRED",
    "frame": "frames/004-escalated.png",
}


def test_raising_an_intervention_transfers_control(broker):
    assert broker.controller == AGENT
    req = broker.raise_intervention(CONTEXT)

    assert broker.controller == HUMAN, "the human must hold control while acting"
    assert req.pending
    assert broker.pending == [req]


def test_the_request_carries_enough_context_to_act_on(broker):
    """An operator handed only 'step 4 failed' has to reconstruct the situation."""
    req = broker.raise_intervention(CONTEXT)
    d = req.to_dict()

    assert d["capability"] == "member.open_subaccount@1.0.0"
    assert d["step"] == 4
    assert d["code"] == "SUPERVISOR_OVERRIDE_REQUIRED"
    assert "supervisor override code" in d["reason"].lower()
    assert d["frame"], "the human needs to see what the agent saw"


def test_the_automation_blocks_until_control_comes_back(broker):
    """Any design where it keeps acting produces two actors on one session."""
    req = broker.raise_intervention(CONTEXT)
    resumed: list[bool] = []

    def run():
        resumed.append(broker.wait_for_resume(timeout=5.0))

    waiter = threading.Thread(target=run)
    waiter.start()

    time.sleep(0.2)
    assert waiter.is_alive(), "the run must still be blocked while the human works"
    assert not resumed

    broker.resume(req.id, note="entered supervisor override SUP-4471")
    waiter.join(timeout=5.0)

    assert resumed == [True]
    assert broker.controller == AGENT, "control must return to the automation"


def test_a_resume_that_never_comes_times_out(broker):
    broker.raise_intervention(CONTEXT)
    assert broker.wait_for_resume(timeout=0.2) is False
    # Control stays with the human -- we do not silently seize it back.
    assert broker.controller == HUMAN


def test_what_the_human_did_is_recorded(broker, log):
    """The evidence must explain the gap, not leave an unexplained jump."""
    req = broker.raise_intervention(CONTEXT)
    broker.resume(req.id, note="entered supervisor override SUP-4471")

    assert req.operator_note == "entered supervisor override SUP-4471"
    assert not req.pending
    assert req.resolved_at is not None

    events = {e["event"] for e in log.events()}
    assert "intervention_raised" in events
    assert "control_returned" in events

    returned = next(e for e in log.events() if e["event"] == "control_returned")
    assert "SUP-4471" in returned["note"]
    assert "held_for_s" in returned


def test_resuming_twice_is_refused(broker):
    req = broker.raise_intervention(CONTEXT)
    assert broker.resume(req.id) is True
    assert broker.resume(req.id) is False, "a resolved request must not reopen"


def test_resuming_an_unknown_request_is_refused(broker):
    assert broker.resume("int-999") is False


# ------------------------------------------------------------------ console

def test_the_console_shows_the_pending_request_and_the_live_view(broker):
    broker.raise_intervention(CONTEXT)
    client = TestClient(build(broker))

    page = client.get("/")
    assert page.status_code == 200
    assert "SUPERVISOR_OVERRIDE_REQUIRED" in page.text
    assert "commit the new sub-account" in page.text
    # The live view must be embedded -- the whole point is that the operator
    # acts on the agent's own display.
    assert 'src="http://vnc/"' in page.text


def test_the_console_hands_control_back(broker):
    req = broker.raise_intervention(CONTEXT)
    client = TestClient(build(broker))

    r = client.post("/resume", data={"request_id": req.id, "note": "typed SUP-4471"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert broker.controller == AGENT
    assert not req.pending
    assert req.operator_note == "typed SUP-4471"


def test_the_console_state_endpoint_is_machine_readable(broker):
    req = broker.raise_intervention(CONTEXT)
    client = TestClient(build(broker))

    state = client.get("/state").json()
    assert state["controller"] == HUMAN
    assert len(state["pending"]) == 1
    assert state["pending"][0]["id"] == req.id

    broker.resume(req.id, note="done")
    after = client.get("/state").json()
    assert after["controller"] == AGENT
    assert after["pending"] == []
    assert len(after["all"]) == 1


def test_an_idle_console_says_the_automation_holds_control(broker):
    client = TestClient(build(broker))
    page = client.get("/")
    assert "No interventions pending" in page.text
    assert AGENT in page.text
