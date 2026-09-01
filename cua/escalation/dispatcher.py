"""The queue's HTTP surface: one console for many runs.

The in-process console is served *by* a run and dies with it. This one outlives
every run and the runs come to it, which is the whole difference between "the
person who launched this is watching" and "whoever is on duty gets told".

The signalling is a poll, and the run does the polling. `console.py` says going
cross-process would mean inventing a signalling channel for no benefit at this
scale -- that was true while one console served one run in one process, and
stops being true here. Between a poll and a callback into the run, the poll
wins: the run already reaches out to the target and the dispatcher, and a
callback would need every replay container to be addressable *from* the
dispatcher, which is a firewall conversation rather than a design.

Runs publish and poll; operators claim and resolve. Both go through `Queue`,
which owns the rules -- notably that claiming is exclusive, so two operators
covering a fleet cannot both take over the same display.
"""

from __future__ import annotations

from html import escape
import threading
from typing import Any

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from cua.escalation.queue import Queue, QueuedIntervention, QueueError

# Not .format-ed, so braces inside are safe to write normally.
STYLE = """
 body { font-family: Verdana, sans-serif; font-size: 12px; margin: 0;
        background: #f4f4f4; color: #111; }
 header { background: #14417a; color: #fff; padding: 10px 14px; font-weight: bold;
          display: flex; justify-content: space-between; }
 .wrap { padding: 14px; }
 .card { background: #fff; border: 1px solid #bbb; padding: 12px; margin-bottom: 12px; }
 .mine { border-left: 4px solid #14417a; }
 .held { border-left: 4px solid #c0a000; }
 .none { color: #555; }
 .why { background: #fffbe6; border: 1px solid #c0a000; padding: 8px; margin: 8px 0; }
 .meta { color: #555; margin: 4px 0; }
 iframe { width: 100%; height: 620px; border: 1px solid #999; background: #000; }
 input[type=text] { padding: 4px; }
 input.note { width: 340px; }
 button { padding: 5px 12px; }
 code { background: #eee; padding: 1px 4px; }
 table { border-collapse: collapse; width: 100%; }
 td, th { border-bottom: 1px solid #ddd; padding: 4px 6px; text-align: left; }
"""

SCRIPT = """
// Same reasoning as the single-run console: the tab is a notification surface
// the operator already has open, and a meta refresh would tear down the noVNC
// iframe of whichever run they are in the middle of. Reload only when the set
// of open interventions actually changes.
var RENDERED = document.body.getAttribute("data-open");
setInterval(function () {
  fetch("/state").then(function (r) { return r.json(); }).then(function (s) {
    var n = s.open.length;
    document.title = n ? "(" + n + ") Take over \\u2014 Operator queue"
                       : "Operator queue";
    if (s.open.map(function (i) { return i.id; }).join(",") !== RENDERED) {
      location.reload();
    }
  }).catch(function () {});
}, 3000);
"""

PAGE = """<!doctype html>
<html><head><title>{title}</title><style>{style}</style></head>
<body data-open="{open_ids}">
<header><span>Operator queue &mdash; {waiting} waiting</span>
        <span>{who}</span></header>
<div class="wrap">
<div class="card">
  <form method="post" action="/whoami">
    <label>You are <input type="text" name="operator" value="{operator}"
      placeholder="your name"></label>
    <button type="submit">Set</button>
  </form>
  <p class="none">Claiming is exclusive &mdash; two operators must not take over
  the same display. Whoever hands control back is named in the run's evidence.</p>
</div>
{items}
{history}
</div>
<script>{script}</script>
</body></html>"""

ITEM = """<div class="card {css}">
  <b>{id}</b> &mdash; <code>{code}</code> &mdash; run <code>{run_id}</code>
  <div class="why">{reason}</div>
  <p class="meta">Capability <code>{capability}</code>, step {step}: {intent}<br>
     Waiting {waited_s}s{held}</p>
  {action}
</div>"""

CLAIM = """<form method="post" action="/claim">
    <input type="hidden" name="item_id" value="{id}">
    <button type="submit">Take this over</button>
  </form>"""

WORKING = """<form method="post" action="/resume">
    <input type="hidden" name="item_id" value="{id}">
    <label>What did you do? <input class="note" type="text" name="note"
      placeholder="e.g. entered supervisor override SUP-4471"></label>
    <button type="submit">Hand control back</button>
  </form>
  <p class="none">This is the display that run is driving &mdash; not a copy.</p>
  <iframe src="{vnc_url}"></iframe>"""

NOTHING = """<div class="card none">Nothing waiting. Every run holds its own
control.</div>"""

ERROR = """<div class="card why"><b>{message}</b></div>"""


def _history(items: list[QueuedIntervention]) -> str:
    done = [i for i in items if not i.pending][-10:]
    if not done:
        return ""
    rows = "".join(
        f"<tr><td>{escape(i.id)}</td><td><code>{escape(i.run_id)}</code></td>"
        f"<td>{escape(i.code)}</td><td>{escape(i.resolved_by)}</td>"
        f"<td>{escape(i.note or '')}</td><td>{escape(str(i.waited_s))}s</td></tr>"
        for i in reversed(done)
    )
    return ("<div class='card'><b>Handed back</b><table>"
            "<tr><th>id<th>run<th>why<th>operator<th>note<th>waited</tr>"
            f"{rows}</table></div>")


def build(queue: Queue) -> FastAPI:
    app = FastAPI(title="Operator queue", docs_url=None, redoc_url=None)

    # ---------------------------------------------------------- the runs

    @app.post("/interventions")
    def raise_one(payload: dict[str, Any]) -> JSONResponse:
        """A run asks for a human."""
        return JSONResponse(queue.add(payload).to_dict(), status_code=201)

    @app.get("/interventions/{item_id}")
    def poll_one(item_id: str) -> JSONResponse:
        """A blocked run asks whether it may proceed yet."""
        item = queue.get(item_id)
        if item is None:
            return JSONResponse({"error": "unknown intervention"}, status_code=404)
        return JSONResponse(item.to_dict())

    @app.post("/interventions/{item_id}/withdraw")
    def withdraw_one(item_id: str,
                     payload: dict[str, Any] | None = None) -> JSONResponse:
        """A run that was unblocked on its own console takes its item back.

        409 rather than an error the run should retry: a claimed item is not a
        transient condition, it is an operator standing at the display.
        """
        try:
            item = queue.withdraw(item_id, (payload or {}).get("note"))
        except QueueError as e:
            unknown = "no such intervention" in str(e)
            return JSONResponse({"error": str(e)},
                                status_code=404 if unknown else 409)
        return JSONResponse(item.to_dict())

    # ----------------------------------------------------- the operators

    @app.get("/", response_class=HTMLResponse)
    def index(operator: str = "", error: str = "") -> str:
        open_items = queue.open
        cards = []
        for item in open_items:
            mine = item.claimed_by == operator and bool(operator)
            fields = {
                key: escape(str(value), quote=True)
                for key, value in item.to_dict().items()
            }
            cards.append(ITEM.format(
                css="mine" if mine else ("held" if item.claimed else ""),
                held=(f" &mdash; held by {escape(item.claimed_by)}"
                      if item.claimed else ""),
                action=(WORKING if mine else CLAIM).format(**fields),
                **fields,
            ))
        escaped_operator = escape(operator, quote=True)
        return PAGE.format(
            style=STYLE, script=SCRIPT,
            title=(f"({len(open_items)}) Take over — Operator queue"
                   if open_items else "Operator queue"),
            waiting=len(open_items), operator=escaped_operator,
            who=(f"signed in as {escaped_operator}"
                 if operator else "not signed in"),
            open_ids=escape(",".join(i.id for i in open_items), quote=True),
            items=(ERROR.format(message=escape(error)) if error else "")
                  + ("".join(cards) or NOTHING),
            history=_history(queue.all),
        )

    @app.post("/whoami")
    def whoami(operator: str = Form("")) -> RedirectResponse:
        return _back(operator.strip())

    @app.post("/claim")
    def claim(item_id: str = Form(...), operator: str = Form("")) -> RedirectResponse:
        try:
            queue.claim(item_id, operator)
        except QueueError as e:
            return _back(operator, error=str(e))
        return _back(operator)

    @app.post("/resume")
    def resume(item_id: str = Form(...), operator: str = Form(""),
               note: str = Form("")) -> RedirectResponse:
        try:
            queue.resolve(item_id, operator, note.strip() or None)
        except QueueError as e:
            return _back(operator, error=str(e))
        return _back(operator)

    @app.get("/state")
    def state() -> JSONResponse:
        return JSONResponse({
            "open": [i.to_dict() for i in queue.open],
            "all": [i.to_dict() for i in queue.all],
        })

    return app


def _back(operator: str, error: str = "") -> RedirectResponse:
    """Redirect home, carrying who you are.

    In the query string rather than a cookie: the operator's name is not a
    credential and pretending otherwise would invite someone to treat it as
    one. It identifies who acted for the audit trail; it does not authorise.
    """
    from urllib.parse import urlencode

    q = urlencode({k: v for k, v in
                   (("operator", operator), ("error", error)) if v})
    return RedirectResponse(f"/?{q}" if q else "/", status_code=303)


def serve_in_background(queue: Queue, port: int = 8090) -> threading.Thread:
    import uvicorn

    config = uvicorn.Config(build(queue), host="0.0.0.0", port=port,
                            log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="operator-queue")
    thread.start()
    return thread


__all__ = ["build", "serve_in_background"]
