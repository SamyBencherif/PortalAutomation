"""The operator surface. Deliberately the thinnest real thing that works.

The brief says a full co-browsing console is out of scope and that mocking the
operator UI is fine so long as the handoff mechanism is real. So this is one
page with a live view and a Resume button -- and everything behind it is
genuine: the iframe is noVNC attached to the agent's actual X display, and
Resume releases the Event the automation is blocked on.

What is missing here is product, not mechanism: no queue across runs, no
operator identity, no audit of who clicked what beyond the note they type. Each
of those is a real gap and they are listed in the write-up rather than papered
over.
"""

from __future__ import annotations

import threading

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from cua.escalation.broker import Broker

PAGE = """<!doctype html>
<html><head><title>Operator console</title>
<style>
 body {{ font-family: Verdana, sans-serif; font-size: 12px; margin: 0;
        background: #f4f4f4; color: #111; }}
 header {{ background: #14417a; color: #fff; padding: 10px 14px; font-weight: bold; }}
 .wrap {{ padding: 14px; }}
 .card {{ background: #fff; border: 1px solid #bbb; padding: 12px; margin-bottom: 12px; }}
 .none {{ color: #555; }}
 .why {{ background: #fffbe6; border: 1px solid #c0a000; padding: 8px; margin: 8px 0; }}
 iframe {{ width: 100%; height: 620px; border: 1px solid #999; background: #000; }}
 input[type=text] {{ width: 340px; padding: 4px; }}
 button {{ padding: 5px 12px; }}
 code {{ background: #eee; padding: 1px 4px; }}
</style></head>
<body>
<header>Operator console &mdash; control: {controller}</header>
<div class="wrap">
{requests}
<div class="card">
  <b>Live session</b>
  <p class="none">This is the same X display the automation is driving &mdash;
  not a copy. Anything done here happens in the agent's browser, in its
  session.</p>
  <iframe src="{vnc}"></iframe>
</div>
</div></body></html>"""

REQUEST = """<div class="card">
  <b>Intervention {id}</b> &mdash; <code>{code}</code>
  <div class="why">{reason}</div>
  <p>Capability <code>{capability}</code>, step {step}: {intent}<br>
     Run <code>{run_id}</code></p>
  <form method="post" action="/resume">
    <input type="hidden" name="request_id" value="{id}">
    <label>What did you do? <input type="text" name="note"
      placeholder="e.g. entered supervisor override SUP-4471"></label>
    <button type="submit">Hand control back</button>
  </form>
</div>"""

NOTHING = """<div class="card none">No interventions pending. The automation
holds control.</div>"""


def build(broker: Broker) -> FastAPI:
    app = FastAPI(title="Operator console", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        pending = broker.pending
        body = "".join(REQUEST.format(**r.to_dict()) for r in pending) or NOTHING
        return PAGE.format(controller=broker.controller, requests=body,
                           vnc=broker.vnc_url)

    @app.post("/resume")
    def resume(request_id: str = Form(...), note: str = Form("")) -> RedirectResponse:
        broker.resume(request_id, note.strip() or None)
        return RedirectResponse("/", status_code=303)

    @app.get("/state")
    def state() -> JSONResponse:
        """Machine-readable, for tests and for the evidence bundle."""
        return JSONResponse({
            "controller": broker.controller,
            "pending": [r.to_dict() for r in broker.pending],
            "all": [r.to_dict() for r in broker.requests.values()],
        })

    return app


def serve_in_background(broker: Broker, port: int = 8080) -> threading.Thread:
    """Run the console alongside a replay run.

    Same process on purpose: resume must release the very Event the run is
    blocked on, and going cross-process would mean inventing a signalling
    channel for no benefit at this scale.
    """
    import uvicorn

    config = uvicorn.Config(build(broker), host="0.0.0.0", port=port,
                            log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="operator-console")
    thread.start()
    return thread


__all__ = ["build", "serve_in_background"]
