"""The cross-run queue: reaching an operator who is not already watching.

The single-run console is reachable only by whoever launched the run, on the
machine they launched it from. These cover the three things that have to be
true for an operator covering a fleet instead:

- work from many runs lands in one list, with the right display attached to
  each item,
- exactly one operator can hold an intervention, and their name reaches the
  run's evidence,
- and somebody is told, over a channel that leaves the machine.

One test drives a real HTTP server rather than a TestClient, because the claim
this module makes is specifically that a *different process* can unblock a run.
Faking the transport would test everything except that.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cua.escalation import notify
from cua.escalation.broker import AGENT, HUMAN, Broker
from cua.escalation.dispatcher import build, serve_in_background
from cua.escalation.queue import Queue, QueueError
from cua.evidence.run import RunLog

RAISED = {
    "capability": "member.open_subaccount@1.0.0",
    "run_id": "replay-northstar-1",
    "step": 4,
    "intent": "commit the new sub-account",
    "reason": "A supervisor override code is required to continue.",
    "code": "SUPERVISOR_OVERRIDE_REQUIRED",
    "vnc_url": "http://localhost:6080/vnc.html",
}


@pytest.fixture
def queue() -> Queue:
    return Queue()


@pytest.fixture
def log(tmp_path: Path) -> RunLog:
    return RunLog.create("test", root=tmp_path)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------- queue

def test_work_from_different_runs_does_not_collide(queue):
    """Every run numbers its own interventions from 1."""
    a = queue.add({**RAISED, "run_id": "replay-a"})
    b = queue.add({**RAISED, "run_id": "replay-b"})

    assert a.id != b.id
    assert {i.run_id for i in queue.open} == {"replay-a", "replay-b"}
    assert [i.run_id for i in queue.for_run("replay-a")] == ["replay-a"]


def test_each_item_carries_its_own_display(queue):
    """Two runs are two containers, so sending an operator to the wrong
    display is worse than sending them nowhere."""
    a = queue.add({**RAISED, "run_id": "replay-a", "vnc_url": "http://vnc-a/"})
    b = queue.add({**RAISED, "run_id": "replay-b", "vnc_url": "http://vnc-b/"})
    assert (a.vnc_url, b.vnc_url) == ("http://vnc-a/", "http://vnc-b/")


def test_the_longest_blocked_run_is_listed_first(queue):
    first = queue.add(RAISED)
    first.raised_at -= 60
    second = queue.add(RAISED)
    assert [i.id for i in queue.open] == [first.id, second.id]


def test_two_operators_cannot_take_the_same_display(queue):
    item = queue.add(RAISED)
    queue.claim(item.id, "sam")

    with pytest.raises(QueueError, match="already being handled by sam"):
        queue.claim(item.id, "alex")
    assert queue.get(item.id).claimed_by == "sam"


def test_reclaiming_your_own_intervention_is_fine(queue):
    """A reload must not lock an operator out of what they are holding."""
    item = queue.add(RAISED)
    queue.claim(item.id, "sam")
    assert queue.claim(item.id, "sam").claimed_by == "sam"


def test_only_the_claimant_hands_control_back(queue):
    item = queue.add(RAISED)
    queue.claim(item.id, "sam")
    with pytest.raises(QueueError, match="held by sam"):
        queue.resolve(item.id, "alex", note="looks fine to me")
    assert queue.get(item.id).pending


def test_control_cannot_be_handed_back_before_it_is_taken(queue):
    item = queue.add(RAISED)
    with pytest.raises(QueueError, match="not been taken over"):
        queue.resolve(item.id, "sam", note="did nothing")


def test_an_anonymous_operator_is_refused(queue):
    """A note signed by nobody is an anecdote, not an audit trail."""
    item = queue.add(RAISED)
    with pytest.raises(QueueError, match="say who you are"):
        queue.claim(item.id, "   ")


def test_an_intervention_is_only_handed_back_once(queue):
    item = queue.add(RAISED)
    queue.claim(item.id, "sam")
    queue.resolve(item.id, "sam", note="SUP-4471")
    with pytest.raises(QueueError, match="already handed back"):
        queue.resolve(item.id, "sam", note="again")
    assert queue.open == []


# ------------------------------------------------------------ notification

def test_raising_announces_it_off_the_machine():
    sent: list[dict] = []

    class Response:
        status_code = 200

    def post(url, json, headers, timeout):
        sent.append({"url": url, "body": json})
        return Response()

    hook = notify.Webhook("https://hooks.example/abc",
                          console_url="http://queue:8090", post=post)
    Queue(notify=hook).add(RAISED)

    assert len(sent) == 1
    body = sent[0]["body"]
    assert sent[0]["url"] == "https://hooks.example/abc"
    # A message that does not say why, or where to go, makes the reader go and
    # find out both -- which is the thing being automated away.
    assert "supervisor override code" in body["reason"].lower()
    assert body["console_url"].startswith("http://queue:8090/#")
    assert body["run_id"] == "replay-northstar-1"
    assert body["vnc_url"] == RAISED["vnc_url"]


def test_a_dead_notification_channel_does_not_lose_the_intervention(log):
    """Failing to tell someone is bad; dropping the request is worse."""
    def post(url, json, headers, timeout):
        raise httpx.ConnectError("no route to host")

    hook = notify.Webhook("https://hooks.example/abc", log=log, post=post)
    queue = Queue(notify=hook)
    item = queue.add(RAISED)

    assert queue.open == [item]
    assert any(e["event"] == "notify_failed" for e in log.events())


def test_no_configured_channel_is_no_channel_rather_than_a_broken_one(monkeypatch):
    monkeypatch.delenv("CUA_NOTIFY_WEBHOOK", raising=False)
    assert notify.from_env() is None
    monkeypatch.setenv("CUA_NOTIFY_WEBHOOK", "https://hooks.example/abc")
    assert notify.from_env(console_url="http://q").url == "https://hooks.example/abc"


# -------------------------------------------------------------- dispatcher

def test_a_run_publishes_and_an_operator_works_the_queue(queue):
    client = TestClient(build(queue))

    posted = client.post("/interventions", json=RAISED)
    assert posted.status_code == 201
    item_id = posted.json()["id"]

    page = client.get("/").text
    assert "(1) Take over" in page.split("</title>")[0]
    assert "supervisor override code" in page.lower()
    assert "Take this over" in page

    client.post("/claim", data={"item_id": item_id, "operator": "sam"},
                follow_redirects=False)
    working = client.get("/", params={"operator": "sam"}).text
    assert f'src="{RAISED["vnc_url"]}"' in working, "the operator needs the display"
    assert "Hand control back" in working

    client.post("/resume", data={"item_id": item_id, "operator": "sam",
                                 "note": "entered SUP-4471"},
                follow_redirects=False)
    seen = client.get(f"/interventions/{item_id}").json()
    assert seen["pending"] is False
    assert seen["resolved_by"] == "sam"
    assert seen["note"] == "entered SUP-4471"


def test_an_operator_is_told_when_someone_else_holds_it(queue):
    client = TestClient(build(queue))
    item_id = client.post("/interventions", json=RAISED).json()["id"]
    client.post("/claim", data={"item_id": item_id, "operator": "sam"},
                follow_redirects=False)

    bumped = client.post("/claim", data={"item_id": item_id, "operator": "alex"},
                         follow_redirects=False)
    assert bumped.status_code == 303
    assert "already+being+handled+by+sam" in bumped.headers["location"]
    assert "already being handled by sam" in client.get(
        "/", params={"operator": "alex", "error": "already being handled by sam"}).text


def test_polling_an_unknown_intervention_says_so(queue):
    assert TestClient(build(queue)).get("/interventions/q-9999").status_code == 404


def test_an_empty_queue_does_not_claim_attention(queue):
    page = TestClient(build(queue)).get("/").text
    assert "<title>Operator queue</title>" in page
    assert "Nothing waiting" in page


def test_handed_back_work_stays_visible_with_who_did_it(queue):
    client = TestClient(build(queue))
    item_id = client.post("/interventions", json=RAISED).json()["id"]
    client.post("/claim", data={"item_id": item_id, "operator": "sam"},
                follow_redirects=False)
    client.post("/resume", data={"item_id": item_id, "operator": "sam",
                                 "note": "entered SUP-4471"},
                follow_redirects=False)

    page = client.get("/").text
    assert "Handed back" in page
    assert "sam" in page and "entered SUP-4471" in page


# ------------------------------------------- a different process, for real

@pytest.fixture
def dispatcher() -> str:
    """A queue on a real port. The claim under test is cross-process."""
    port = _free_port()
    serve_in_background(Queue(), port)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/state", timeout=1.0).status_code == 200:
                return base
        except httpx.HTTPError:
            time.sleep(0.05)
    pytest.fail("the dispatcher never came up")


def test_an_operator_elsewhere_unblocks_a_run(dispatcher, log):
    """The whole point: nobody is watching the terminal that started this."""
    broker = Broker(target_base="http://127.0.0.1:9", vnc_url="http://vnc-a/",
                    log=log, dispatcher=dispatcher, poll_interval=0.1)
    req = broker.raise_intervention(RAISED)

    assert req.queue_id, "the run never reached the queue"
    assert broker.controller == HUMAN

    listed = httpx.get(f"{dispatcher}/state").json()["open"]
    assert [i["run_id"] for i in listed] == [RAISED["run_id"]]
    assert listed[0]["vnc_url"] == "http://vnc-a/", "wrong display would be worse"

    resumed: list[bool] = []
    waiter = threading.Thread(target=lambda: resumed.append(
        broker.wait_for_resume(timeout=10.0)))
    waiter.start()
    time.sleep(0.3)
    assert waiter.is_alive(), "the run must block while the operator works"

    httpx.post(f"{dispatcher}/claim",
               data={"item_id": req.queue_id, "operator": "sam"},
               follow_redirects=False)
    httpx.post(f"{dispatcher}/resume",
               data={"item_id": req.queue_id, "operator": "sam",
                     "note": "entered supervisor override SUP-4471"},
               follow_redirects=False)
    waiter.join(timeout=10.0)

    assert resumed == [True]
    assert broker.controller == AGENT
    assert req.operator == "sam"
    assert req.operator_note == "entered supervisor override SUP-4471"

    returned = next(e for e in log.events() if e["event"] == "control_returned")
    assert returned["operator"] == "sam", "the evidence must name who decided"
    assert next(e for e in log.events()
                if e["event"] == "resume_wait_finished")["via"] == "queue"


def test_a_run_survives_a_queue_that_is_not_there(log):
    """A dispatcher that is down must not take the run down with it."""
    broker = Broker(target_base="http://127.0.0.1:9", log=log,
                    dispatcher="http://127.0.0.1:9", poll_interval=0.1)
    req = broker.raise_intervention(RAISED)

    assert req.queue_id is None
    assert broker.controller == HUMAN, "the handoff still happened locally"
    # "Nobody came" and "nobody was told" are different facts and the evidence
    # must not conflate them.
    assert any(e["event"] == "queue_unreachable" for e in log.events())

    # And the run's own console still works, which is where it would have been
    # without a queue at all.
    assert broker.resume(req.id, note="took over locally") is True
    assert broker.wait_for_resume(timeout=1.0) is True
