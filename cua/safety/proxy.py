"""The allowlist, enforced where the agent cannot argue with it.

Everything else in this system is advisory. The agent could decide to click
the "Close Member Record" link; the replay engine could have a bug; a recorded
artifact could name a route nobody reviewed. None of that matters if the
request cannot leave the container, and that is what this is for.

The browser is launched with `--proxy-server` pointing here, so every request
the surface makes -- including ones no Python code initiated, like a redirect
the page issued itself -- is checked against the same `Policy` the engine uses.
A denied request never reaches the target and comes back as a legible refusal
page rather than a mysterious hang.

This is also the only complete record of what the automation actually touched.
The engine logs what it *meant* to do; this logs what the network saw, which
is the version an auditor should trust.

CONNECT is refused outright. The target speaks plaintext HTTP, so tunnelling
has no legitimate use here, and a CONNECT tunnel is exactly the hole that would
make every rule above unenforceable.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from cua.safety.policy import DEFAULT_POLICY, Policy

DENIED_PAGE = """<html><head><title>Blocked by policy</title></head>
<body style="font-family:Verdana,sans-serif;font-size:12px">
<h2>Request blocked</h2>
<p>{reason}</p>
<p>This request was refused by the automation's allowlist and never reached
the target application.</p>
</body></html>"""

# Hop-by-hop headers must not be forwarded (RFC 7230 6.1). Passing `connection`
# or `transfer-encoding` upstream produces corrupted responses that look like
# target bugs and are miserable to diagnose.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}


class PolicyProxy(BaseHTTPRequestHandler):
    policy: Policy = DEFAULT_POLICY
    audit_path: Path | None = None
    client: httpx.Client | None = None
    _audit_broken: bool = False

    protocol_version = "HTTP/1.1"
    server_version = "cua-policy-proxy"

    # ------------------------------------------------------------- logging

    @classmethod
    def audit(cls, **fields) -> None:
        """Record one verdict. Never allowed to break the request.

        Logging is not load-bearing for serving, and wiring it that way is how
        a guardrail stops guarding. This did take the whole proxy down once:
        every request died in here on `FileNotFoundError` for the audit path --
        from an *append* open, which creates the file -- before any response was
        written. The failure direction was at least the safe one, since nothing
        reached the target, but it presented as "the browser cannot load
        anything", which is a miserable place to start debugging from.

        The trigger was not identified. The obvious suspect was the container's
        bind mount going stale after a host-side rebase replaced the directory,
        but that was tested directly and does something else entirely: the
        container keeps writing happily into the old inode and the host sees an
        empty directory. Silent divergence, not an error. So the cause here
        remains open, which is exactly why the handler is defensive rather than
        fixed against one story about what went wrong.
        """
        if cls.audit_path is None:
            return
        try:
            cls.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with cls.audit_path.open("a") as fh:
                fh.write(json.dumps({"t": round(time.time(), 3), **fields}) + "\n")
        except OSError as e:
            # Say so once per process rather than per request, then carry on.
            if not cls._audit_broken:
                cls._audit_broken = True
                print(f"[proxy] audit log unwritable ({e}); still enforcing policy",
                      file=sys.stderr, flush=True)

    def log_message(self, fmt: str, *args) -> None:
        # Silence the default stderr chatter; the audit log is the record.
        pass

    # ------------------------------------------------------------ handling

    def _handle(self) -> None:
        url = self.path
        if not url.startswith("http"):
            # Origin-form request: a client that isn't configured as a proxy.
            host = self.headers.get("Host", "")
            url = f"http://{host}{self.path}"

        # Drain the request body FIRST, before any early return. On a
        # keep-alive connection an unread body stays in the socket and is
        # parsed as the start of the next request -- a denied POST would
        # silently corrupt the request after it. Enforcement that breaks the
        # connection it is protecting is not enforcement.
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else None

        verdict = self.policy.check_url(url)
        parts = urlsplit(url)
        self.audit(
            method=self.command, url=url, host=parts.hostname, path=parts.path,
            allowed=verdict.allowed, reason=verdict.reason,
        )

        if not verdict.allowed:
            body = DENIED_PAGE.format(reason=verdict.reason).encode()
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        headers = {
            k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP
        }

        try:
            upstream = self.client.request(
                self.command, url, headers=headers, content=payload,
                follow_redirects=False,
            )
        except httpx.HTTPError as e:
            self.audit(method=self.command, url=url, allowed=True, error=str(e))
            body = f"upstream error: {e}".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        content = upstream.content
        self.send_response(upstream.status_code)
        for key, value in upstream.headers.items():
            if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_OPTIONS = _handle

    def do_CONNECT(self) -> None:
        """Refused by design -- see the module docstring."""
        self.audit(method="CONNECT", url=self.path, allowed=False,
                   reason="CONNECT tunnelling is not permitted")
        self.send_error(403, "CONNECT is not permitted")


def serve(
    port: int = 8888,
    policy: Policy | None = None,
    audit_path: str | Path | None = None,
) -> ThreadingHTTPServer:
    PolicyProxy.policy = policy or DEFAULT_POLICY
    PolicyProxy.audit_path = Path(audit_path) if audit_path else None
    PolicyProxy.client = httpx.Client(timeout=30.0)
    return ThreadingHTTPServer(("0.0.0.0", port), PolicyProxy)


def main() -> None:
    ap = argparse.ArgumentParser(description="Policy-enforcing forward proxy.")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--policy", help="JSON policy file; omit for the default")
    ap.add_argument("--audit", default="evidence/proxy.jsonl")
    args = ap.parse_args()

    policy = Policy.load(args.policy) if args.policy else DEFAULT_POLICY
    server = serve(args.port, policy, args.audit)
    print(f"policy proxy on :{args.port}  deny={policy.deny_paths}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()


__all__ = ["PolicyProxy", "serve"]
