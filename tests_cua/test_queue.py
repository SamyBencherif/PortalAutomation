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


@pytest.mark.parametrize("step", [None, "not-a-number"])
def test_a_malformed_step_uses_the_unknown_default(queue, step):
    assert queue.add({**RAISED, "step": step}).step == -1


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


def test_a_malformed_notification_url_does_not_lose_the_intervention(log):
    hook = notify.Webhook("http://example.com:not-a-port", log=log)
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


def test_dispatcher_escapes_queue_and_query_values(queue):
    client = TestClient(build(queue))
    item = queue.add({
        **RAISED,
        "run_id": '<script>alert("run")</script>',
        "reason": '<img src=x onerror="alert(1)">',
        "vnc_url": 'https://vnc.example/" onload="alert(2)',
    })

    page = client.get(
        "/", params={"operator": '"><script>alert("operator")</script>',
                     "error": "<b>failed</b>"},
    ).text

    assert "<script>alert" not in page
    assert "<img src=x" not in page
    assert 'onload="alert' not in page
    assert "<b>failed</b>" not in page
    assert "&lt;script&gt;alert" in page
    assert f'value="{item.id}"' in page


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


def test_local_console_defers_to_an_operator_holding_the_item(dispatcher, log):
    """Two doors onto one display, and somebody is already through the other."""
    broker = Broker(target_base="http://127.0.0.1:9", log=log,
                    dispatcher=dispatcher, poll_interval=0.1)
    req = broker.raise_intervention(RAISED)
    assert req.queue_id

    httpx.post(f"{dispatcher}/claim",
               data={"item_id": req.queue_id, "operator": "sam"},
               follow_redirects=False)

    assert broker.resume(req.id, note="local handback") is False
    assert req.pending
    assert broker.controller == HUMAN

    refused = next(e for e in log.events() if e["event"] == "resume_refused")
    assert refused["held_by"] == "sam", "a run that stays blocked must say why"


def test_local_console_answers_an_unclaimed_item_and_clears_it(dispatcher, log):
    """The other half of the same rule, and the one that regressed.

    Publishing to a queue must not disable the console the run is serving.
    Nobody has claimed this, so whoever is at the terminal is the first
    answer -- and the card has to leave the work list with them, or an operator
    picks up a run that resumed minutes ago.
    """
    broker = Broker(target_base="http://127.0.0.1:9", log=log,
                    dispatcher=dispatcher, poll_interval=0.1)
    req = broker.raise_intervention(RAISED)
    assert req.queue_id

    assert broker.resume(req.id, note="typed the override at the terminal")
    assert broker.controller == AGENT
    assert req.operator_note == "typed the override at the terminal"

    listed = httpx.get(f"{dispatcher}/state").json()
    assert listed["open"] == [], "a card for a run that moved on is a trap"
    withdrawn = next(i for i in listed["all"] if i["id"] == req.queue_id)
    assert not withdrawn["pending"]
    assert withdrawn["resolved_by"] == "", "no operator did this; none is named"


def test_a_run_cannot_take_back_work_an_operator_holds(queue):
    item = queue.add(RAISED)
    queue.claim(item.id, "sam")

    with pytest.raises(QueueError, match="being handled by sam"):
        queue.withdraw(item.id)

    assert queue.open == [item], "the operator keeps the display"


def test_withdrawing_answers_the_run_rather_than_erroring(queue):
    client = TestClient(build(queue))
    item_id = client.post("/interventions", json=RAISED).json()["id"]

    assert client.post("/interventions/q-9999/withdraw").status_code == 404

    r = client.post(f"/interventions/{item_id}/withdraw",
                    json={"note": "handed back on the run's own console"})
    assert r.status_code == 200
    assert r.json()["note"] == "handed back on the run's own console"

    # Second time round somebody would have to be holding it, which is a
    # conflict rather than a retry.
    assert client.post(f"/interventions/{item_id}/withdraw").status_code == 409


def test_local_console_recovers_after_dispatcher_forgets_item(log, monkeypatch):
    broker = Broker(target_base="http://127.0.0.1:9", log=log)
    req = broker.raise_intervention(RAISED)
    broker.dispatcher = "http://dispatcher"
    req.queue_id = "q-forgotten"

    class Missing:
        status_code = 404

    real_get = httpx.get
    monkeypatch.setattr(
        httpx, "get",
        lambda url, **kwargs: Missing() if url.startswith(broker.dispatcher)
        else real_get(url, **kwargs),
    )

    assert broker.resume(req.id, note="handled locally") is True
    assert req.operator_note == "handled locally"
