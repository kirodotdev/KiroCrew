#!/usr/bin/env python3
"""Fake A2A remote agent for offline E2E testing and demos.

The A2A twin of :mod:`kiro_crew.testing.fake_acp_backend`. Where that module
stands in for ``kiro-cli`` behind the ACP provider, this one stands in for a
remote agent behind :class:`kiro_crew.providers.a2a.A2AProvider`, speaking the
minimal subset of A2A v1.0 the provider drives:

    GET  /.well-known/agent-card.json  -> the Agent Card (capabilities.streaming)
    POST /                             -> JSON-RPC 2.0 ``SendStreamingMessage``,
                                          answered as an SSE stream of
                                          ``{"result": {...}}`` frames
    POST /                             -> JSON-RPC 2.0 ``CancelTask``

Every ``SendStreamingMessage`` mints a task. A message that carries no
``contextId`` starts a NEW conversation and the server mints one; a message that
carries one continues THAT conversation, and the first frame echoes it back. The
per-conversation state is what lets a test prove continuity: a codeword told in
one task is recalled in a later task only if the client really sent the retained
``contextId``.

Prompt-driven behaviour on the task text (the terminal frame is always sent last
unless a fault says otherwise):

* Default -> stream ONE artifact chunk with the canned reply, then ``COMPLETED``.
  The canned reply honours a few instruction shapes so scenarios can assert on
  exact output: ``Reply with only the word X`` / ``Reply only with X`` /
  ``Reply with the codeword X`` -> ``X``; ``Remember the codeword X`` -> stores
  ``X`` on the conversation and replies ``OK``; ``What was the codeword`` ->
  the stored codeword, or ``I have no codeword for this conversation``.
* ``[[SLOW]]`` -> stream ``SLOW_CHUNKS`` numbered lines, one every
  ``SLOW_INTERVAL`` seconds (about 90 s in total), checking for ``CancelTask``
  between chunks. A cancel ends the task ``CANCELED``. Long enough that a
  pixel-driven tester can act several times while the turn is provably in flight.
* ``[[DROP]]`` -> stream ``DROP_CHUNKS`` lines and then CLOSE the connection
  without a terminal state, which is what a crashed remote agent or a lost
  network path looks like on the wire. The client must fail the turn, not
  complete it.
* ``[[NEVER_TERMINAL]]`` -> stream one line, then end the SSE body cleanly but
  without any terminal status. Distinct from ``[[DROP]]``: the HTTP exchange is
  well-formed, only the protocol contract is broken.
* ``[[FAIL]]`` -> a terminal ``FAILED`` status carrying an error message.
* ``[[ERROR]]`` -> a JSON-RPC error frame instead of a result.

Deterministic, loopback-only, no auth. Uses ``aiohttp`` (already a runtime
dependency of the provider it exercises), so it is importable in-process by
tests (:func:`serve`) and runnable as a sidecar by the GUI user-test boot::

    python -m kiro_crew.testing.fake_a2a_server --port 8790

It is never selected by the product: a remote agent reaches it only because a
test seed registers its card URL under ``a2a_agents``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

logger = logging.getLogger("kiro_crew.testing.fake_a2a_server")

__all__ = [
    "AGENT_NAME",
    "DROP_CHUNKS",
    "SLOW_CHUNKS",
    "SLOW_INTERVAL",
    "FakeA2AServer",
    "agent_card",
    "canned_reply",
    "make_app",
    "serve",
]

AGENT_NAME = "remote-demo"

#: ``[[SLOW]]`` streams this many lines, one per ``SLOW_INTERVAL`` seconds. The
#: product is ~90 s: the GUI tester's own screenshot->model->action loop is ~17 s,
#: so a turn must outlast several of its actions to be interruptible on purpose.
SLOW_CHUNKS = 18
SLOW_INTERVAL = 5.0
#: ``[[DROP]]`` streams this many lines before the connection is cut.
DROP_CHUNKS = 3

_STATE_WORKING = "TASK_STATE_WORKING"
_STATE_COMPLETED = "TASK_STATE_COMPLETED"
_STATE_FAILED = "TASK_STATE_FAILED"
_STATE_CANCELED = "TASK_STATE_CANCELED"

_REMEMBER_RE = re.compile(r"remember the codeword\s+([A-Za-z0-9-]+)", re.IGNORECASE)
_RECALL_RE = re.compile(r"what was the codeword", re.IGNORECASE)
_REPLY_ONLY_RE = re.compile(
    r"reply (?:with only the word|only with|with the codeword|with just the word)\s+([A-Za-z0-9-]+)",
    re.IGNORECASE,
)
_DIRECTIVE_RE = re.compile(r"\[\[[A-Z_]+\]\]")


@dataclass
class _Conversation:
    context_id: str
    codeword: str = ""
    turns: list[str] = field(default_factory=list)


@dataclass
class _Task:
    task_id: str
    context_id: str
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)


def agent_card(base_url: str, *, require_bearer: bool = False) -> dict[str, Any]:
    """The Agent Card the fake serves; ``base_url`` is where SendStreamingMessage lives.

    With ``require_bearer`` the card declares an HTTP bearer security scheme and
    requires it (spec 7.3), the shape a hosted agent behind an OAuth/JWT
    authorizer publishes -- so a client that reads the card must send
    ``Authorization: Bearer`` or refuse to start.
    """
    card: dict[str, Any] = {
        "name": AGENT_NAME,
        "description": "Kiro Crew test fixture: a deterministic remote agent over A2A.",
        "version": "1.0.0",
        "protocolVersion": "1.0",
        "supportedInterfaces": [
            {"url": base_url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {"streaming": True, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "echo",
                "name": "Deterministic reply",
                "description": "Answers instruction-shaped prompts exactly; remembers a codeword per conversation.",
                "tags": ["test"],
            }
        ],
    }
    if require_bearer:
        card["securitySchemes"] = {
            "bearer": {"httpAuthSecurityScheme": {"scheme": "bearer", "bearerFormat": "opaque"}}
        }
        card["securityRequirements"] = [{"schemes": {"bearer": {"list": []}}}]
    return card


def canned_reply(text: str, conv: _Conversation) -> str:
    """The deterministic answer for one task's text, updating conversation state."""
    clean = _DIRECTIVE_RE.sub("", text).strip()
    m = _REMEMBER_RE.search(clean)
    if m:
        conv.codeword = m.group(1)
        return "OK"
    if _RECALL_RE.search(clean):
        return conv.codeword or "I have no codeword for this conversation"
    m = _REPLY_ONLY_RE.search(clean)
    if m:
        return m.group(1)
    return f"fake-a2a reply #{len(conv.turns)}: {clean[:120]}"


def _parts_text(message: dict[str, Any]) -> str:
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    return "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))


class FakeA2AServer:
    """State + handlers. One instance per server process (or per test)."""

    def __init__(self, *, require_bearer: str = "") -> None:
        #: When set, every request (the card fetch included) must carry exactly
        #: ``Authorization: Bearer <require_bearer>`` or is answered 401 -- the
        #: behaviour of a hosted agent behind an authorizer, so a test can prove
        #: the client sends its credential on the wire rather than only having
        #: read it from config.
        self.require_bearer = require_bearer
        self.unauthorized: int = 0  # requests refused for a missing/wrong credential
        self.conversations: dict[str, _Conversation] = {}
        self.tasks: dict[str, _Task] = {}
        self.requests: list[dict[str, Any]] = []  # every JSON-RPC body seen, for assertions

    # ---- HTTP handlers -------------------------------------------------

    def _authorized(self, request: web.Request) -> bool:
        if not self.require_bearer:
            return True
        ok = request.headers.get("Authorization", "") == f"Bearer {self.require_bearer}"
        if not ok:
            self.unauthorized += 1
        return ok

    @staticmethod
    def _unauthorized() -> web.Response:
        return web.json_response(
            {"code": "unauthorized", "error": "missing or invalid bearer credential"},
            status=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    async def card(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        base = f"{request.scheme}://{request.host}/"
        return web.json_response(agent_card(base, require_bearer=bool(self.require_bearer)))

    async def rpc(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            return self._unauthorized()
        try:
            body = await request.json()
        except Exception:
            # A dict literal with a top-level ``code`` (not a helper call) so the
            # error-code ratchet can read this 4xx body. The JSON-RPC ``error``
            # object is unchanged -- that is what an A2A client parses.
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                    "code": "jsonrpc_parse_error",
                },
                status=400,
            )
        self.requests.append(body)
        method = body.get("method")
        req_id = body.get("id")
        if request.headers.get("A2A-Version") != "1.0":
            return web.json_response(_rpc_error(req_id, -32009, "unsupported A2A version"))
        if method == "CancelTask":
            return await self._cancel(req_id, body.get("params") or {})
        if method == "SendStreamingMessage":
            return await self._send_streaming(request, req_id, body.get("params") or {})
        return web.json_response(_rpc_error(req_id, -32601, f"method not found: {method}"))

    # ---- RPC methods ---------------------------------------------------

    async def _cancel(self, req_id: Any, params: dict[str, Any]) -> web.Response:
        task_id = str(params.get("id") or params.get("taskId") or "")
        task = self.tasks.get(task_id)
        if task is None:
            return web.json_response(_rpc_error(req_id, -32001, "task not found"))
        task.cancelled.set()
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "id": task.task_id,
                    "contextId": task.context_id,
                    "status": {"state": _STATE_CANCELED},
                },
            }
        )

    async def _send_streaming(
        self, request: web.Request, req_id: Any, params: dict[str, Any]
    ) -> web.StreamResponse:
        message = params.get("message") or {}
        text = _parts_text(message)
        ctx_id = str(message.get("contextId") or "") or uuid.uuid4().hex
        conv = self.conversations.setdefault(ctx_id, _Conversation(context_id=ctx_id))
        task = _Task(task_id=uuid.uuid4().hex, context_id=ctx_id)
        self.tasks[task.task_id] = task
        conv.turns.append(text)

        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"},
        )
        await resp.prepare(request)

        async def frame(result: dict[str, Any]) -> None:
            await resp.write(
                b"data: "
                + json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}).encode()
                + b"\n\n"
            )

        async def artifact(chunk: str) -> None:
            await frame(
                {
                    "artifactUpdate": {
                        "taskId": task.task_id,
                        "contextId": ctx_id,
                        "append": True,
                        "artifact": {"artifactId": "out", "parts": [{"text": chunk}]},
                    }
                }
            )

        async def status(state: str, *, final: bool, text_: str = "") -> None:
            su: dict[str, Any] = {
                "taskId": task.task_id,
                "contextId": ctx_id,
                "final": final,
                "status": {"state": state},
            }
            if text_:
                su["status"]["message"] = {"role": "ROLE_AGENT", "parts": [{"text": text_}]}
            await frame({"statusUpdate": su})

        if "[[ERROR]]" in text:
            await resp.write(
                b"data: "
                + json.dumps(_rpc_error(req_id, -32603, "fake-a2a: [[ERROR]] requested")).encode()
                + b"\n\n"
            )
            await resp.write_eof()
            return resp

        # First frame: the Task itself. This is where the client adopts ids.
        await frame(
            {"task": {"id": task.task_id, "contextId": ctx_id, "status": {"state": _STATE_WORKING}}}
        )

        if "[[FAIL]]" in text:
            await status(_STATE_FAILED, final=True, text_="fake-a2a: [[FAIL]] requested")
            await resp.write_eof()
            return resp

        if "[[SLOW]]" in text:
            for i in range(1, SLOW_CHUNKS + 1):
                if task.cancelled.is_set():
                    await status(_STATE_CANCELED, final=True)
                    await resp.write_eof()
                    return resp
                await artifact(f"{i}. slow fact number {i}\n")
                try:
                    await asyncio.wait_for(task.cancelled.wait(), timeout=SLOW_INTERVAL)
                except asyncio.TimeoutError:
                    pass
            if task.cancelled.is_set():
                await status(_STATE_CANCELED, final=True)
            else:
                await status(_STATE_COMPLETED, final=True)
            await resp.write_eof()
            return resp

        if "[[DROP]]" in text:
            for i in range(1, DROP_CHUNKS + 1):
                await artifact(f"{i}. fact number {i}, delivered before the drop\n")
            # Cut the connection: no terminal state, no clean end of body.
            # force_close() drops the TCP connection so the client sees a
            # truncated transfer, exactly like a remote that died mid-stream.
            request.transport.close() if request.transport is not None else None
            return resp

        if "[[NEVER_TERMINAL]]" in text:
            await artifact("1. a line, and then the stream ends without a terminal state\n")
            await resp.write_eof()
            return resp

        await artifact(canned_reply(text, conv))
        await status(_STATE_COMPLETED, final=True)
        await resp.write_eof()
        return resp


def _rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def make_app(server: FakeA2AServer | None = None) -> web.Application:
    """The aiohttp application; ``server`` is exposed as ``app["fake"]`` for tests."""
    server = server or FakeA2AServer()
    app = web.Application()
    app["fake"] = server
    app.router.add_get("/.well-known/agent-card.json", server.card)
    app.router.add_post("/", server.rpc)
    return app


async def serve(
    port: int = 0, host: str = "127.0.0.1", *, require_bearer: str = ""
) -> tuple[web.AppRunner, str, FakeA2AServer]:
    """Start the fake in-process. Returns ``(runner, card_url, server)``; ``port=0`` picks a free one."""
    app = make_app(FakeA2AServer(require_bearer=require_bearer))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    bound = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    card_url = f"http://{host}:{bound}/.well-known/agent-card.json"
    return runner, card_url, app["fake"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument(
        "--require-bearer-env",
        default="",
        help=(
            "NAME of an environment variable; when set, every request must carry "
            "Authorization: Bearer <its value> (the value itself never appears on argv)."
        ),
    )
    args = p.parse_args(argv)
    required = os.environ.get(args.require_bearer_env, "") if args.require_bearer_env else ""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

    async def _run() -> None:
        runner, card_url, _ = await serve(args.port, args.host, require_bearer=required)
        # Machine-readable READY line, mirroring the gateway's own convention.
        print(f"FAKE_A2A_READY:{json.dumps({'card_url': card_url})}", flush=True)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await runner.cleanup()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
