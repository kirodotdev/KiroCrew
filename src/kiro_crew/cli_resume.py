"""``kirocrew resume``: resume chats on the running gateway.

- no argument: the chats the last graceful stop cut
  (``restart_resume.read_last_stop``), through the sidebar's Resume-all route
  (``POST /api/chat/slots/resume-all``), so its bound on concurrent turns and its
  per-chat refusals apply. The record is never cleared here: the route refuses a
  chat that is not interrupted and idle, so a second run is safe, and the next
  stop rewrites it;
- ``SESSION...``: each session through its tab's own Resume button route
  (``POST /api/chat/slots/{slot}/continue``, no body), one request per session,
  so the bulk route's exclusions (such as an interruption older than its window)
  do not apply; like the button, it also carries on a chat that finished
  cleanly. Each session's answer or refusal is printed;
- ``--all``: every interrupted session, as the sidebar's "Resume N sessions".

The owner session is ``cli_chat_headless.GatewayClient``'s: an owner token minted
with the gateway's local secret over the peer-verified transport, as ``kirocrew
token`` mints one.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from typing import Any

from kiro_crew.cli_chat_headless import GatewayClient, NoGateway
from kiro_crew.dashboard import restart_resume
from kiro_crew.port_resolution import resolve_client_port
from kiro_crew.terminal_safe import safe_terminal_line

RESUME_ALL_PATH = "/api/chat/slots/resume-all"
_SIGN_IN_POLL_SECS = 0.5
#: How long ``kirocrew resume`` waits for a booting gateway to report ready.
_RESUME_READY_WAIT_SECS = 15.0
_HTTP_REFUSAL = re.compile(r"HTTP (\d+) (.*)", re.DOTALL)
_clock = time.monotonic
_sleep = time.sleep


class GatewayNotReady(NoGateway):
    """The gateway answers but has not reported ready (``/api/ready`` is not 200)."""


def continue_path(session: str) -> str:
    """The tab's Resume button route for *session*."""
    return f"/api/chat/slots/{urllib.parse.quote(session, safe='')}/continue"


def _ready_status(port: int) -> int:
    from kiro_crew.cli_server_restart import _probe_gateway_ready

    return _probe_gateway_ready(port)


def _wait_ready(port: int, deadline: float) -> None:
    """Return once ``/api/ready`` answers 200; raise past *deadline*, or at once when unreachable.

    The route answers as soon as the socket serves, before the boot restores the
    chat tabs, so a press before ready finds nothing to resume.
    """
    while True:
        status = _ready_status(port)
        if status == 200:
            return
        if status == 0:
            raise NoGateway(f"gateway on port {port} not reachable")
        if _clock() >= deadline:
            raise GatewayNotReady(f"gateway on port {port} not ready (HTTP {status})")
        _sleep(_SIGN_IN_POLL_SECS)


def _signed_in(port: int) -> GatewayClient:
    """An owner client, once the gateway is ready."""
    deadline = _clock() + _RESUME_READY_WAIT_SECS
    _wait_ready(port, deadline)
    client = GatewayClient(port)
    while True:
        try:
            client.sign_in()
            return client
        except NoGateway:
            if _clock() >= deadline:
                raise
            _sleep(_SIGN_IN_POLL_SECS)


def _press(port: int, body: dict[str, Any]) -> dict[str, Any]:
    """POST *body* to the Resume-all route once the gateway is ready."""
    answer = _signed_in(port).call("POST", RESUME_ALL_PATH, body)
    return answer if isinstance(answer, dict) else {}


def _refusal(exc: RuntimeError) -> str:
    """``HTTP <status> <code>: <error>`` from a refused request, else its text."""
    text = str(exc)
    match = _HTTP_REFUSAL.search(text)
    if match is None:
        return _failure(exc)
    status, detail = match.groups()
    try:
        body = json.loads(detail)
    except ValueError:
        body = None
    if isinstance(body, dict) and ("code" in body or "error" in body):
        return safe_terminal_line(f"HTTP {status} {body.get('code', '')}: {body.get('error', '')}")
    return safe_terminal_line(f"HTTP {status} {detail}")


def _continue_each(port: int, sessions: list[str]) -> bool:
    """Press each session's own Resume; print each answer. ``False`` unless every request got an HTTP answer."""
    client = _signed_in(port)
    reached = True
    for session in sessions:
        name = safe_terminal_line(session)
        try:
            client.call("POST", continue_path(session))
        except NoGateway as exc:
            reached = False
            print(f"❌ {name}: no answer from the gateway ({_failure(exc)})", file=sys.stderr)
        except RuntimeError as exc:
            if _HTTP_REFUSAL.search(str(exc)) is None:
                reached = False
                print(f"❌ {name}: request failed ({_failure(exc)})", file=sys.stderr)
            else:
                print(f"   {name}: left alone ({_refusal(exc)})")
        else:
            print(f"✅ {name}: resuming; its result shows in its tab.")
    return reached


def _report(answer: dict[str, Any], asked: int | None) -> None:
    total = answer.get("total")
    total = total if isinstance(total, int) else 0
    if total:
        print(f"✅ Resuming {total} chat(s); each one's result shows in its tab.")
    elif not asked:
        print("Nothing to resume: no chat is interrupted and idle now.")
    if asked is not None and asked > total:
        print(
            f"{'   ' if total else 'Nothing to resume: '}{asked - total} of the {asked} chat(s) "
            "asked for were left alone: running, resumed, closed, left to its conductor, or cut too long ago."
        )


def _failure(exc: Exception) -> str:
    return safe_terminal_line(str(exc))


def resume_cmd(args: argparse.Namespace) -> None:
    """``kirocrew resume [SESSION...] [--all] [--port N]``."""
    sessions = list(dict.fromkeys(args.sessions or []))
    if args.all and sessions:
        print("❌ Pass session ids or --all, not both.", file=sys.stderr)
        sys.exit(2)
    port = resolve_client_port(args.port)
    asked: int | None = None
    body: dict[str, Any] = {}
    if args.all:
        body = {"crew_mode": True}
    elif not sessions:
        try:
            names = restart_resume.read_last_stop()
        except OSError as exc:
            print(f"❌ Could not read the last stop's record: {_failure(exc)}", file=sys.stderr)
            sys.exit(1)
        if not names:
            print("No chats were cut by the last stop: nothing to resume.")
            return
        body = {"crew_mode": True, "slots": names}
        asked = len(names)
    try:
        if sessions:
            if not _continue_each(port, sessions):
                sys.exit(1)
            return
        answer = _press(port, body)
    except GatewayNotReady as exc:
        print(
            f"❌ The gateway on port {port} is not ready yet ({_failure(exc)}). "
            "Run kirocrew resume again once it serves.",
            file=sys.stderr,
        )
        sys.exit(1)
    except NoGateway as exc:
        print(
            f"❌ No gateway answered on port {port} ({_failure(exc)}). "
            "Start it, then run kirocrew resume again.",
            file=sys.stderr,
        )
        sys.exit(1)
    except RuntimeError as exc:
        print(f"❌ Resume failed: {_failure(exc)}", file=sys.stderr)
        sys.exit(1)
    _report(answer, asked)
