"""Workflows builtin-app backend (M3 backend slice).

A small stdlib-only HTTP server exposing the dynamic-workflows engine to the
dashboard "Workflows" tab. KiroCrew proxies ``/apps/workflows/api/*`` to this
process (the server sees the paths at the root — the prefix is stripped).

Endpoints:
  GET  /health                  → {"status": "ok"}
  POST /validate  {source}      → {"ok", "errors": [...], "meta": {...}|null}
  POST /run       {source, args?, name?, budget_total?}
                                → {"ok", "result", "error", "events": [<event>...]}
  GET  /examples                → [{"name","description","source"}]  (the shipped DSL examples)

The run path drives ``WorkflowRunner`` (M1/M2, fully gated) with a stub
``agent_fn`` for now — real agent wiring (subagent-by-default / ``session=`` to
SubagentManager/SessionManager) is the M3/M4 provider-hookup unit and slots in
behind the same ``agent_fn`` boundary the runner already uses, with NO change to
the frozen ``ctx`` contract.

The handler logic is factored into pure functions (``handle_validate`` /
``handle_run``) so it is unit-testable without binding a socket — the live HTTP
wiring + the dashboard tab + WS progress are covered by the M3 Playwright gates
(E1–E4) against the dev instance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from kiro_crew.apps.proxy_auth import verify_proxy_request
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.workflows.runner import WorkflowRunner
from kiro_crew.workflows.validate import validate

logger = logging.getLogger("kirocrew.app.workflows")


def _redact_obj(obj: Any) -> Any:
    """Recursively redact credentials + exfiltration URLs from a response payload.

    Workflow results/events carry LLM-generated text (agent responses, log lines)
    that can contain leaked credentials or exfiltration URLs; this builtin-app
    backend must redact before serving to the dashboard, mirroring the gateway
    handlers (dashboard/handlers/workflows.py::_redact_obj). Applied centrally in
    ``_send`` so every response surface is covered.
    """
    if isinstance(obj, str):
        s, _ = redact_exfiltration_urls(obj)
        s, _ = redact_credentials(s)
        return s
    if isinstance(obj, list):
        return [_redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    return obj


PORT = int(os.environ.get("PORT", 9120))
APP_NAME = os.environ.get("KIROCREW_APP_NAME", "workflows")

# A fixed run-start stamp is supplied per run by the gateway in production; for the
# standalone backend we accept it from the request or fall back to a constant so
# the event stream stays deterministic (never `time` in the script's scope).
_DEFAULT_NOW = "1970-01-01T00:00:00Z"


async def _stub_agent(prompt: str, opts: dict) -> Any:
    """Placeholder agent executor until the real provider hookup (M3/M4).

    Echoes a deterministic stand-in so the UI/run path is exercisable end-to-end
    without a live model. Replaced by a SubagentManager/SessionManager-backed
    ``agent_fn`` behind this same boundary; the frozen ctx contract is unchanged.
    """
    return f"[stub agent reply to: {prompt[:80]}]"


def handle_validate(body: dict) -> dict:
    """Pure handler: validate a workflow script's source."""
    source = body.get("source", "")
    if not isinstance(source, str):
        return {"ok": False, "errors": ["'source' must be a string"], "meta": None}
    result = validate(source)
    return {"ok": result.ok, "errors": result.errors, "meta": result.meta}


def handle_run(body: dict, *, runner: WorkflowRunner | None = None) -> dict:
    """Pure handler: run a workflow script and return result + event stream.

    ``runner`` is injectable for tests (defaults to a stub-agent runner). Returns a
    JSON-serializable dict; the events are each ``WorkflowEvent.to_json()``.
    """
    source = body.get("source", "")
    if not isinstance(source, str) or not source.strip():
        return {"ok": False, "result": None, "error": "missing 'source'", "events": []}

    # Substitute the default only when no run_id was supplied (absent/None) — a
    # falsy-but-valid caller id (0, "") must be preserved, else events get stamped
    # "wf_ui" and a caller correlating by the supplied id finds none.
    raw_run_id = body.get("run_id")
    run_id = "wf_ui" if raw_run_id is None else str(raw_run_id)
    now = str(body.get("now") or _DEFAULT_NOW)
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    budget_total = body.get("budget_total")
    if not isinstance(budget_total, int):
        budget_total = None

    runner = runner or WorkflowRunner(agent_fn=_stub_agent)
    res = asyncio.run(
        runner.run(source, run_id=run_id, now=now, args=args, budget_total=budget_total)
    )
    return {
        "ok": res.ok,
        "result": res.result,
        "error": res.error,
        "events": [e.to_json() for e in res.events],
    }


def _examples_dir() -> str:
    # docs/dynamic-workflows/research/examples relative to the repo root.
    here = os.path.dirname(os.path.abspath(__file__))
    root = here
    for _ in range(8):
        cand = os.path.join(root, "docs", "dynamic-workflows", "research", "examples")
        if os.path.isdir(cand):
            return cand
        root = os.path.dirname(root)
    return ""


def handle_examples() -> list[dict]:
    """List the shipped example workflows (name + description + source)."""
    out: list[dict] = []
    ex_dir = _examples_dir()
    if not ex_dir:
        return out
    for fname in sorted(os.listdir(ex_dir)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(ex_dir, fname)
        try:
            with open(path, encoding="utf-8") as fh:
                source = fh.read()
        except OSError:
            continue
        meta = validate(source).meta or {}
        out.append(
            {
                "name": meta.get("name", fname[:-3]),
                "description": meta.get("description", ""),
                "source": source,
            }
        )
    return out


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002 - quiet default logging
        logger.debug("workflows-app: " + fmt, *args)

    def _send(self, code: int, payload: Any) -> None:
        # Redact credentials / exfiltration URLs from all LLM-derived content
        # before it leaves this backend (parity with the gateway handlers).
        data = json.dumps(_redact_obj(payload)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _authorized(self, method: str, raw_body: bytes) -> bool:
        """Verify the gateway's X-KiroCrew-Proxy HMAC before dispatch (CWE-306).

        Health stays unauthenticated (the gateway probes the backend directly,
        unsigned). The signature binds the raw request body.
        """
        route = self.path.rstrip("/")
        if route in ("", "/health", "/api", "/api/health"):
            return True
        if verify_proxy_request(
            self.headers.get("X-KiroCrew-Proxy", ""),
            method=method,
            target=self.path,
            body=raw_body,
        ):
            return True
        self._send(401, {"error": "unauthorized"})
        return False

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        route = self.path.rstrip("/")
        if route in ("/health", "/api/health"):
            self._send(200, {"status": "ok"})
            return
        if not self._authorized("GET", b""):
            return
        if route in ("/examples", "/api/examples"):
            self._send(200, handle_examples())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not self._authorized("POST", raw):
            return
        try:
            obj = json.loads(raw) if raw else {}
            body = obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            body = {}
        route = self.path.rstrip("/")
        if route in ("/validate", "/api/validate"):
            self._send(200, handle_validate(body))
        elif route in ("/run", "/api/run"):
            self._send(200, handle_run(body))
        else:
            self._send(404, {"error": "not found"})


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    logger.info("workflows app backend on 127.0.0.1:%d", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
