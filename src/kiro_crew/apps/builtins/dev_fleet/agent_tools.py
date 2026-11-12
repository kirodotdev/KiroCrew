"""Dev Fleet's agent tools, served from the gateway through the manifest ``agentTools`` seam.

``app.json`` declares the five pod tools (``pod_up``, ``pod_restart``, ``pod_down``,
``pod_status``, ``pod_ls``) under the namespace ``pod``, so they keep the names agents,
skills and policies already know. ``kirocrew-core`` lists them and forwards each call
to ``POST /api/app-tools/call``, which hands it to the ONE route registered here,
``POST /api/apps/dev-fleet/agent-tools``. This app has a backend process too, but that
process is sandboxed: it cannot reach the systemd user bus a pod verb needs, and a
pod refuses a token minted from inside it. So the route is registered in the gateway
(``backend.hooks.routes``), and the seam serves a tool from an app's own gateway route
whenever the app registered one at the declared route.

Each tool calls the same verb ``agent_pod_api`` serves on its own routes, and formats
the answer the model reads exactly as the core tool these replaced did. The verb's
response is first read the way the core's gateway client read it (a refusal becomes
``{"error", "code"}`` with its message scrubbed), so a refusal reads the same too.

The tools are declared ``result: "text"``, and each answer is the text the core tool
rendered, scrubbed here where the core tool scrubbed it. The core redacts it again as
it relays it. The token a pod start returns is the deliverable, so a pod start's answer
names it as its ``credential`` (:func:`_answer`): the core keeps that one string
verbatim and redacts the rest. It is a credential for that pod's own gateway, not the
live one.
"""

from __future__ import annotations

import json
import re
from typing import Any, TypeGuard

from aiohttp import web

from kiro_crew.apps.builtins.dev_fleet import agent_pod_api
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

#: The route ``app.json`` names in ``agentTools.route``, relative to the app's namespace.
TOOL_ROUTE = "/agent-tools"

#: A refusal's ``code`` survives only in this shape, as the gateway client keeps it.
_CODE_RE = re.compile(r"[a-z0-9_.-]{1,64}")


def _client_view(resp: web.Response) -> dict[str, Any]:
    """*resp* as the core's gateway client handed it to the tool it served.

    A 2xx answer is its JSON body. A refusal is ``{"error": <message>}`` plus its
    ``code`` when that is identifier-shaped, with the message scrubbed of exfiltration
    URLs and credentials, as ``mcp_core_parts.gateway._http_error_body`` does.
    """
    raw = resp.body.decode("utf-8", "replace") if isinstance(resp.body, bytes) else ""
    try:
        parsed: Any = json.loads(raw) if raw else {}
    except ValueError:
        parsed = None
    if resp.status < 400:
        return parsed if isinstance(parsed, dict) else {}
    message = raw or f"HTTP {resp.status}"
    code = ""
    if isinstance(parsed, dict) and "error" in parsed:
        message = str(parsed["error"])
        raw_code = parsed.get("code")
        if isinstance(raw_code, str) and _CODE_RE.fullmatch(raw_code):
            code = raw_code
    message, _ = redact_exfiltration_urls(message)
    message, _ = redact_credentials(message)
    out: dict[str, Any] = {"error": message}
    if code:
        out["code"] = code
    return out


def _pod_failure(payload: dict[str, Any], verb: str) -> str | None:
    """The error line for a refused pod call, or None when it succeeded.

    Two failure shapes arrive here and both must read as errors: a refusal read by
    :func:`_client_view` as ``{"error": ..., "code": ...}``, and an answer that is not
    ``ok``. Reporting only the first would let a refused pod boot read as a success
    with no handle in it.
    """
    if payload.get("error") and not payload.get("ok"):
        _code = payload.get("code")
        _suffix = f" [{_code}]" if _code else ""
        return f"Error: {verb} failed{_suffix}: {redact(str(payload['error']))}"
    if not payload.get("ok"):
        return f"Error: {verb} returned no result: {redact(json.dumps(payload, default=str))}"
    return None


def _answer(text: str, credential: str = "") -> dict[str, Any]:
    """The wire answer for *text*: ``{"text": <str>}``, or the text cut at *credential*.

    A *credential* the text carries is named so the core keeps it verbatim:
    ``{"text": [<the pieces around it>], "credential": <it>}``.
    """
    if credential and credential in text:
        return {"text": text.split(credential), "credential": credential}
    return {"text": text}


def _positive_int(value: object) -> TypeGuard[int]:
    """True for a real positive int; a bool, a float or a numeric string is not one."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _format_window(secs: int) -> str:
    """A token window as a person reads it: '5 min', '2h', or seconds."""
    if secs >= 3600 and secs % 3600 == 0:
        return f"{secs // 3600}h"
    if secs >= 60 and secs % 60 == 0:
        return f"{secs // 60} min"
    return f"{secs}s"


def _pod_token_limits(resp: dict[str, Any]) -> str:
    """Both limits of a pod token, as the route read them from its claims.

    The link window and the session lifetime differ by design (minutes versus
    hours), and naming only one of them tells the agent the wrong thing about the
    other. When the route sent no usable window the line says to use the token
    at once and names no duration, because a guessed one is the defect itself.
    """
    link, session = resp.get("link_ttl_secs"), resp.get("session_ttl_secs")
    if _positive_int(link) and _positive_int(session):
        return (
            f"use within {_format_window(link)}; "
            f"the session it opens lasts {_format_window(session)}"
        )
    return "use it straight away: the link window is short"


def _pod_limit_body(args: dict[str, Any]) -> dict[str, str]:
    """The optional ``memory`` / ``cpu`` fields a pod start forwards."""
    return {k: args[k] for k in ("memory", "cpu") if args.get(k)}


def _pod_start_reply(args: dict[str, Any], _pu_resp: dict[str, Any], verb: str) -> dict[str, Any]:
    """The answer for a pod that ``up`` or ``restart`` brought up: its prose handle."""
    _pu_worktree = args["worktree"]
    _pu_err = _pod_failure(_pu_resp, f"pod {verb} {_pu_worktree!r}")
    if _pu_err:
        return _answer(_pu_err)
    # The token is the DELIVERABLE, so it is the one string here that must survive
    # verbatim -- redacting it would return a handle that cannot be used. Scope is
    # what makes that safe: it is a credential for this pod's own gateway, not the
    # live one. The answer names it, so the core redacts everything else.
    _pu_port = _pu_resp.get("port")
    _pu_base = _pu_resp.get("base_url") or (f"http://127.0.0.1:{_pu_port}" if _pu_port else "")
    _pu_token = _pu_resp.get("token") or ""
    _pu_state = "restarted with its data kept" if verb == "restart" else "up"
    _pu_lines = [
        f"Pod `{_pu_worktree}` is {_pu_state} on port {_pu_port}.",
        f"base_url: {_pu_base}",
    ]
    if _pu_resp.get("checkout"):
        _pu_lines.append(f"checkout: {_pu_resp['checkout']}")
    if _pu_resp.get("memory") or _pu_resp.get("cpu"):
        _pu_lines.append(f"limits: memory={_pu_resp.get('memory')} cpu={_pu_resp.get('cpu')}")
    if _pu_token:
        _pu_lines.append(f"token: {_pu_token}  ({_pod_token_limits(_pu_resp)})")
        _pu_lines.append(f"open: {_pu_base}/?token={_pu_token}")
        _pu_lines.append(
            "Past its window the pod refuses the token as expired; call pod_up "
            "again (the pod keeps running) for a fresh one."
        )
    else:
        # `pod up --json` emits an empty token when it could not PROVE it owns the
        # port, and withholding the credential is the correct outcome there. Say so:
        # a missing token read as an oversight sends the agent hunting for a bug.
        _pu_lines.append(
            "token: (withheld -- the CLI could not prove this port belongs to the "
            "pod it just started; check `pod_status` before trusting the port)"
        )
    _pu_lines.append(f"Stop it with pod_down when you are done: {_pu_worktree}")
    return _answer("\n".join(_pu_lines), str(_pu_token))


async def pod_up(args: dict[str, Any]) -> dict[str, Any]:
    body = {"worktree": args["worktree"], **_pod_limit_body(args)}
    if args.get("env"):
        body["env"] = args["env"]
    resp = await agent_pod_api.pod_up_response(body)
    return _pod_start_reply(args, _client_view(resp), "up")


async def pod_restart(args: dict[str, Any]) -> dict[str, Any]:
    resp = await agent_pod_api.pod_restart_response(
        {"worktree": args["worktree"], **_pod_limit_body(args)}
    )
    return _pod_start_reply(args, _client_view(resp), "restart")


async def pod_down(args: dict[str, Any]) -> dict[str, Any]:
    _pd_worktree = args["worktree"]
    resp = await agent_pod_api.pod_down_response({"worktree": _pd_worktree})
    _pd_err = _pod_failure(_client_view(resp), f"pod down {_pd_worktree!r}")
    if _pd_err:
        return _answer(_pd_err)
    return _answer(f"Pod `{_pd_worktree}` is stopped and its data directory reclaimed.")


async def pod_status(args: dict[str, Any]) -> dict[str, Any]:
    _ps_worktree = args["worktree"]
    _ps_resp = _client_view(await agent_pod_api.pod_status_response(_ps_worktree))
    _ps_err = _pod_failure(_ps_resp, f"pod status {_ps_worktree!r}")
    if _ps_err:
        return _answer(_ps_err)
    _ps_limits = (
        f" memory={_ps_resp.get('memory')} cpu={_ps_resp.get('cpu')}"
        if _ps_resp.get("memory") or _ps_resp.get("cpu")
        else ""
    )
    return _answer(
        redact(
            f"Pod `{_ps_resp.get('name') or _ps_worktree}`: "
            f"{_ps_resp.get('status') or 'unknown'} "
            f"port={_ps_resp.get('port')} health={_ps_resp.get('health')}{_ps_limits}"
        )
    )


async def pod_ls(args: dict[str, Any]) -> dict[str, Any]:
    _pl_resp = _client_view(await agent_pod_api.pod_list_response())
    _pl_err = _pod_failure(_pl_resp, "pod ls")
    if _pl_err:
        return _answer(_pl_err)
    _pl_pods = _pl_resp.get("pods") or []
    if not _pl_pods:
        return _answer("No pods are active on this host.")
    _pl_lines = [f"{len(_pl_pods)} pod(s) active:"]
    for _pl_row in _pl_pods:
        if not isinstance(_pl_row, dict):
            continue
        _pl_lines.append(
            f"  {_pl_row.get('name')}  port={_pl_row.get('port')} "
            f"health={_pl_row.get('health')}"
            + (
                f" memory={_pl_row.get('memory')} cpu={_pl_row.get('cpu')}"
                if _pl_row.get("memory") or _pl_row.get("cpu")
                else ""
            )
        )
    return _answer(redact("\n".join(_pl_lines)))


#: The verbs ``app.json`` declares, by tool name.
TOOLS = {
    "pod_up": pod_up,
    "pod_restart": pod_restart,
    "pod_down": pod_down,
    "pod_status": pod_status,
    "pod_ls": pod_ls,
}

#: The verb a refusal names before the grant check: the one each tool's reply names.
_VERBS = {
    "pod_up": "pod up",
    "pod_restart": "pod restart",
    "pod_down": "pod down",
    "pod_status": "pod status",
    "pod_ls": "pod ls",
}


@agent_pod_api._machine_only
async def handle_agent_tool(request: web.Request) -> web.Response:
    """``POST /api/apps/dev-fleet/agent-tools`` -- one declared pod tool call.

    The body is ``{"tool", "arguments"}``, already checked against the declaration by
    the gateway's ``/api/app-tools/call``. The caller gate is the pod routes' own
    (local origin plus ``internal_auth``), so a dashboard cookie that reaches this
    path directly is refused. Every outcome a tool has, a refusal included, is its
    text, answered as :func:`_answer` shapes it.
    """
    # Read the body as cached bytes, never through ``can_read_body``: the forwarding
    # handler already read this request's payload, which leaves it at EOF.
    try:
        body: Any = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return web.json_response(
            {"ok": False, "code": "invalid_body", "error": "body must be a JSON object"},
            status=400,
        )
    tool, args = body.get("tool"), body.get("arguments")
    if not isinstance(tool, str):
        tool = ""
    verb = TOOLS.get(tool)
    if verb is None or not isinstance(args, dict):
        return web.json_response(
            {"ok": False, "code": "unknown_tool", "error": "not a Dev Fleet agent tool"},
            status=404,
        )
    refusal = await agent_pod_api.enabled_refusal()
    if refusal is not None:
        worktree = args.get("worktree")
        target = f" {worktree!r}" if isinstance(worktree, str) else ""
        text = _pod_failure(_client_view(refusal), f"{_VERBS[tool]}{target}") or ""
        return web.json_response(_answer(text))
    return web.json_response(await verb(args))


async def _route_handler(request: web.Request, _ctx: Any) -> web.Response:
    return await handle_agent_tool(request)


def register_routes(ctx: Any) -> list[Any]:
    """``backend.hooks.routes``: the one route the declared tools are served on."""
    from kiro_crew.apps.route_registry import AppRoute

    return [AppRoute(method="POST", path=TOOL_ROUTE, handler=_route_handler)]
