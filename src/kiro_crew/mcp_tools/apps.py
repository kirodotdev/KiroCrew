"""The app-scoped tools reachable only through this server's credential tools: what they advertise and what they do.

``schemas()`` returns the DECLARATION half of each tool -- its name, the
model-facing description, and the JSON Schema a call is validated against.
``HANDLERS`` maps each of those names to the function that runs it. Both halves
of a tool live here so its contract and its behavior are read together, and
``test_mcp_tool_registry`` fails if one arrives without the other.

``advertised()`` is what ``tools/list`` actually emits: the declaration minus
the tools of every app that is switched off. Each tool here belongs to one
built-in app (``TOOL_APPS``) whose gateway routes refuse a call while the app is
disabled, so advertising the tool regardless tells the model about a capability
that can only refuse. The listing and the call read the SAME
``installed.json`` through the same module; ``advertised()`` states the one
state in which the two readers differ.

Handlers reach this server's shared plumbing as attributes of ``mcp_core`` --
``mcp_core._post``, the identity resolvers, the governance vets. That is
deliberate rather than untidy: an attribute lookup resolves at CALL time, so a
test that rebinds one on the module still intercepts the handler. Importing
those names directly here would bind them at import time and silently escape
every existing patch site.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypeGuard
from urllib.parse import quote

from kiro_crew import mcp_core
from kiro_crew.apps.manager import app_enabled_state
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import OPS_MISSION_CONTROL_ALLOWED_CALLS, sanitize_json_values


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the apps tools."""
    return [
        {
            "name": "ops_mission_control_api",
            "description": (
                "Call the Ops Mission Control app's HTTP API with the gateway's "
                "own credential. This is the ONLY way an agent session reaches "
                "that API: raw HTTP has no credential and is refused with 403, "
                "and no CLI or environment variable provides one. Use it for "
                "every call an ops-mission-control SOP asks for — reading "
                "state/signals/incidents/rotation/ledger, claiming and "
                "transitioning incidents, arming the rotation, posting ledger "
                "entries. Paths are rooted at the app base (pass '/state', not "
                "the full URL) and only the SOP surface is reachable: "
                "provider configuration, settings, webhook ingest and the "
                "human proposal-decision routes are deliberately not callable "
                "from here."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST"],
                        "description": "HTTP method",
                    },
                    "path": {
                        "type": "string",
                        # Derived from the frozen allowlist so the advertised
                        # schema cannot drift from what the handler admits.
                        "enum": sorted({p for _, p in OPS_MISSION_CONTROL_ALLOWED_CALLS}),
                        "description": (
                            "API path relative to /api/apps/ops-mission-control. "
                            "GET: /state /signals /incidents /handover /rotation "
                            "/ledger /ledger/contradictions. POST: /dispatch "
                            "/incident/transition /incident/claim /incident/action "
                            "/rotation/arm /ledger /ledger/hygiene."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional query string without the leading '?', GET only "
                            "— e.g. 'status=investigating' or 'id=INV-42' on /incidents"
                        ),
                    },
                    "body_json": {
                        "type": "string",
                        "description": (
                            "JSON object for POST bodies, serialized as a string — "
                            'e.g. \'{"id": "INV-42", "status": "resolved"}\' for '
                            "/incident/transition"
                        ),
                    },
                },
                "required": ["method", "path"],
            },
        },
        {
            "name": "pod_up",
            "description": (
                "Boot a Kiro Crew POD -- a complete preview gateway for one git "
                "worktree, on its own port with its own data directory -- and get "
                "back the {base_url, token} handle for driving it. On a host whose "
                "sandbox denies this session the systemd user bus, this is the only "
                "way to start a pod: a pod is a systemd --user unit, and "
                "`kirocrew pod up` in your shell then fails with `Permission "
                "denied`. Check with `systemctl --user is-system-running` if you "
                "want to know which case you are in; either way this tool works, "
                "because the gateway holds the host bus and does the systemd part "
                "for you. Use it to test an unfinished change end to end without "
                "touching the live gateway. Blocking: it returns once the pod "
                "answers its own health check, a few seconds for a worktree that "
                "is already built. A worktree with no built dist is REFUSED with "
                "the CLI's own remedy in the message -- provisioning (venv + SPA "
                "build) is minutes of work and is not reachable from here; run it "
                "from the Dev Fleet page or `kirocrew pod provision <wt>`. "
                "Requires the Dev Fleet app to be enabled. The returned token is "
                "scoped to that pod alone; it is not the live gateway's, and no "
                "secret of the host is exposed by it. It carries two limits, and "
                "the reply names both: the token itself is accepted only within a "
                "short link window (5 minutes), and the first request that carries "
                "it opens a pod session, kept in a cookie, that lasts 2h. Use the "
                "token straight away and drive the pod with that session; past the "
                "window the pod refuses the token as expired. Calling pod_up again "
                "on a running pod leaves it running and returns a fresh token. "
                "Release the pod with `pod_down` when done -- a pod holds a port "
                "and memory until it is stopped. `memory` and `cpu` set the pod's "
                "ceilings (default 4G and 200%); on a pod that is already running "
                "they are recorded and take effect with `pod_restart` (on Windows, "
                "with `pod_down` then `pod_up`)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "worktree": {
                        "type": "string",
                        "description": (
                            "The worktree to bring up, named the way `pod_ls` and "
                            "the Dev Fleet page name it -- its directory basename, "
                            "e.g. 'kc-wt-10732'. NOT a path and NOT a branch"
                        ),
                    },
                    "memory": {
                        "type": "string",
                        "description": (
                            "Optional memory ceiling for the pod, e.g. '16G' (default "
                            "4G, capped at 80% of host RAM). Kept across restarts"
                        ),
                    },
                    "cpu": {
                        "type": "string",
                        "description": (
                            "Optional CPU ceiling, e.g. '800%' for eight cores "
                            "(default 200%, capped at the host CPU count). Kept "
                            "across restarts"
                        ),
                    },
                },
                "required": ["worktree"],
            },
        },
        {
            "name": "pod_restart",
            "description": (
                "Restart a running pod in place and KEEP its data directory: its "
                "sessions, config, installed apps and downloaded models survive. "
                "Use it to apply a new `memory` / `cpu` ceiling, to pick up a "
                "rebuilt worktree, or to revive a pod that crashed. Unlike "
                "`pod_down` followed by `pod_up`, nothing is deleted. Works only "
                "for a pod that has been started before and not torn down with "
                "`pod_down`. Not available on Windows, where it is refused before "
                "anything is stopped. Blocking until the pod answers its health check; "
                "returns the same {base_url, token} handle as `pod_up`."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "worktree": {
                        "type": "string",
                        "description": "The worktree whose pod should be restarted",
                    },
                    "memory": {
                        "type": "string",
                        "description": (
                            "Optional memory ceiling for the pod, e.g. '16G' (default "
                            "4G, capped at 80% of host RAM). Kept across restarts"
                        ),
                    },
                    "cpu": {
                        "type": "string",
                        "description": (
                            "Optional CPU ceiling, e.g. '800%' for eight cores "
                            "(default 200%, capped at the host CPU count). Kept "
                            "across restarts"
                        ),
                    },
                },
                "required": ["worktree"],
            },
        },
        {
            "name": "pod_down",
            "description": (
                "Stop a pod and reclaim its isolated data directory. Call this when "
                "you are finished with a pod you started: until then it holds its "
                "port, its memory and its unit. Same reason as `pod_up` for why "
                "this is a tool and not a shell command -- the systemd user bus is "
                "not reachable from this session. This DELETES that pod's data "
                "directory, which is by design (a pod is disposable) but is not "
                "undoable: anything you want to keep must be copied out of the pod "
                "first. It does not touch the worktree, its branch, or your commits."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "worktree": {
                        "type": "string",
                        "description": "The worktree whose pod should be stopped",
                    },
                },
                "required": ["worktree"],
            },
        },
        {
            "name": "pod_status",
            "description": (
                "Whether one worktree's pod is up, on which port, and what its "
                "health probe answers. Use it to check a pod you started is still "
                "serving before driving it, and to tell 'my request failed' apart "
                "from 'the pod is not running'. A `health` of 200/401/403 means "
                "this pod is up; 0 means nothing answers on that port; -2 means "
                "another process answers on that port, so this pod is not up."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "worktree": {
                        "type": "string",
                        "description": "The worktree whose pod state you want",
                    },
                },
                "required": ["worktree"],
            },
        },
        {
            "name": "pod_ls",
            "description": (
                "Every pod currently ACTIVE on this host, with its port and health. "
                "Takes no arguments. Read this before starting a pod: it is what "
                "tells you a pod you need is already up (start nothing), or that "
                "someone else's run owns the port yours would derive. The list is "
                "deliberately not filtered to one repository -- a filtered list "
                "would hide exactly the pod that explains a refusal."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


#: Which built-in app each tool reaches, by the app's installed name (the directory
#: under ``apps/`` whose ``installed.json`` the app's gateway routes gate on). Spelled
#: as literals rather than imported from the apps: each app's ``APP_NAME`` lives in
#: a backend module that drags that app's whole import graph into this server
#: process. ``test_mcp_apps_tool_gate`` pins every entry to the real constant. A tool
#: an installed app declares in its manifest is the ``app_tools`` domain's.
TOOL_APPS: dict[str, str] = {
    "ops_mission_control_api": "ops-mission-control",
    "pod_up": "dev-fleet",
    "pod_restart": "dev-fleet",
    "pod_down": "dev-fleet",
    "pod_status": "dev-fleet",
    "pod_ls": "dev-fleet",
}


def advertised() -> list[dict[str, Any]]:
    """The descriptors ``tools/list`` emits now: ``schemas()`` minus disabled apps' tools.

    The listing and the call read ONE file through ONE module -- the app's
    ``installed.json`` via ``kiro_crew.apps.manager`` -- with two readers. Every
    route these tools reach refuses the call with HTTP 403 while the binary
    ``is_app_enabled`` does not prove the app enabled (Dev Fleet's routes and the
    app reverse proxy a declared tool posts through name the refusal
    ``app_not_enabled``, Ops Mission Control ``app_disabled``). This reads the tri-state ``app_enabled_state`` and hides only on a
    readable ``False`` -- not installed, or installed and switched off. So a
    hidden tool is always one whose call would be refused, while a listed tool is
    not always one whose call would succeed: an unreadable record lists tools the
    route refuses. Hiding is a product decision, not a security one -- the route
    stays the enforcement point, as it does for the computer-use tools -- and it
    exists because kiro-cli caches ``tools/list`` once per session, so a disabled
    app's tools would otherwise spend context in every request of every session
    that can never use them.

    Fail-OPEN on a read fault, as the per-session policy filter does: ``None``
    (corrupt JSON, a dangling link, a directory in the file's place) lists the
    tools, and so does a read that raises. The same once-per-session cache that
    motivates hiding makes hiding on a transient fault permanent for the
    session's whole life, while a listed tool whose call the gateway refuses is
    not a hole; an exception escaping this build, by contrast, would withdraw
    EVERY tool of the pooled server for every session that shares it.

    Only the FULL descriptor build reaches this function. The names-only build
    (``mcp_tools.build_tool_names``, which in-process discovery reads on the
    gateway's event loop) takes ``schemas()`` directly -- the same rule that
    keeps ``spawn.schemas`` and ``control.schemas`` from their live reads on that
    path -- so no ``installed.json`` is read there and a disabled app's tools
    still appear by name. The process that serves ``tools/list`` to a model is
    ``mcp_shared.run_mcp_stdio_loop``, which takes the full build, where the gate
    IS applied.
    """
    hidden: set[str] = set()
    for app in sorted(set(TOOL_APPS.values())):
        try:
            state = app_enabled_state(app)
        except Exception:
            # Never let a metadata read break the tool advertisement: the route
            # still refuses the call, while an escaping exception withdraws every tool.
            continue
        if state is False:
            hidden.update(tool for tool, owner in TOOL_APPS.items() if owner == app)
    return [spec for spec in schemas() if spec["name"] not in hidden]


def ops_mission_control_api(name: str, args: dict[str, Any]) -> str:
    _omc_method = args["method"]
    _omc_path = args["path"]
    if (_omc_method, _omc_path) not in OPS_MISSION_CONTROL_ALLOWED_CALLS:
        return (
            f"Error: {_omc_method} {_omc_path} is not part of the "
            "ops-mission-control agent surface."
        )
    _omc_body: dict[str, Any] = {}
    _omc_body_raw = args.get("body_json") or ""
    if _omc_body_raw:
        try:
            _omc_parsed = json.loads(_omc_body_raw)
        except ValueError:
            return "Error: body_json is not valid JSON."
        if not isinstance(_omc_parsed, dict):
            return "Error: body_json must encode a JSON object."
        # Schema sanitization saw body_json as one opaque string, where a
        # ``\u200b`` escape is plain ASCII — the hidden character only
        # materializes on decode. Walk the decoded structure so smuggled
        # invisibles cannot split a credential past downstream redaction.
        # Then redact on the way IN as well: ledger entries persist and
        # sync to a configured remote, so a credential quoted into a body
        # field must be scrubbed BEFORE ``_post``, not only on the
        # response path.
        _omc_body = mcp_core._redact_json_strings(sanitize_json_values(_omc_parsed))
    _omc_query = args.get("query") or ""
    _omc_url = "/api/apps/ops-mission-control" + _omc_path
    if _omc_query:
        _omc_url += "?" + _omc_query
    _omc_resp = (
        mcp_core._get(_omc_url) if _omc_method == "GET" else mcp_core._post(_omc_url, _omc_body)
    )
    # Serialize compactly and redact on the way OUT: signals, incident
    # titles and ledger entries carry text from external monitoring
    # systems and prior LLM turns, so a credential or exfil URL quoted
    # into one would otherwise flow straight into this agent's context.
    # Redact BEFORE truncating: slicing first could cut a credential in
    # half at the cap so the redaction pattern would not match, leaking
    # the surviving fragment.
    _omc_text = redact(json.dumps(_omc_resp, ensure_ascii=False, default=str))
    _omc_cap = 60_000
    if len(_omc_text) > _omc_cap:
        _omc_text = (
            _omc_text[:_omc_cap] + f"\n… truncated ({len(_omc_text)} chars total). Narrow the "
            "call (e.g. query filters) to see the rest."
        )
    return _omc_text


_POD_API_BASE = "/api/apps/dev-fleet/pod"

# Client-side ceilings, each one step above the ceiling the gateway route enforces
# on its own subprocess (180s for a pod boot). Ordered that way on purpose: the
# server's bound is the one that produces a DIAGNOSIS ("systemd refused", "no built
# dist"), so it must be the one that fires. A tighter client timeout would replace
# every real failure with a bare "no response".
_POD_UP_TIMEOUT_S = 210.0
_POD_DOWN_TIMEOUT_S = 60.0
# A read still spawns `python -m kiro_crew pod ...` in the gateway, so it pays a
# cold interpreter start -- more than the 10s a pure telemetry read is given.
_POD_READ_TIMEOUT_S = 45.0


def _pod_failure(payload: dict[str, Any], verb: str) -> str | None:
    """The error line for a refused pod call, or None when it succeeded.

    Two failure shapes arrive here and both must read as errors: a transport-level
    ``{"error": ...}`` from the gateway helper, and the route's own
    ``{"ok": false, "error": ..., "code": ...}``. Reporting only the first would
    let a refused pod boot read as a success with no handle in it.
    """
    if payload.get("error") and not payload.get("ok"):
        _code = payload.get("code")
        _suffix = f" [{_code}]" if _code else ""
        return f"Error: {verb} failed{_suffix}: {redact(str(payload['error']))}"
    if not payload.get("ok"):
        return f"Error: {verb} returned no result: {redact(json.dumps(payload, default=str))}"
    return None


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


def pod_up(name: str, args: dict[str, Any]) -> str:
    _pu_resp = mcp_core._post(
        f"{_POD_API_BASE}/up",
        {"worktree": args["worktree"], **_pod_limit_body(args)},
        timeout=_POD_UP_TIMEOUT_S,
    )
    return _pod_start_reply(args, _pu_resp, "up")


def pod_restart(name: str, args: dict[str, Any]) -> str:
    _pr_resp = mcp_core._post(
        f"{_POD_API_BASE}/restart",
        {"worktree": args["worktree"], **_pod_limit_body(args)},
        timeout=_POD_UP_TIMEOUT_S,
    )
    return _pod_start_reply(args, _pr_resp, "restart")


def _pod_start_reply(args: dict[str, Any], _pu_resp: dict[str, Any], verb: str) -> str:
    """The prose handle for a pod that ``up`` or ``restart`` brought up.

    Each caller posts to its own literal route, which the call-site auth scan
    resolves against the strict internal-path table.
    """
    _pu_worktree = args["worktree"]
    _pu_err = _pod_failure(_pu_resp, f"pod {verb} {_pu_worktree!r}")
    if _pu_err:
        return _pu_err
    # The token is the DELIVERABLE, so it is the one string here that must survive
    # verbatim -- redacting it would return a handle that cannot be used. Scope is
    # what makes that safe: it is a credential for this pod's own gateway, not the
    # live one. Everything else in the payload is echoed as the route sent it.
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
    return "\n".join(_pu_lines)


def pod_down(name: str, args: dict[str, Any]) -> str:
    _pd_worktree = args["worktree"]
    _pd_resp = mcp_core._post(
        f"{_POD_API_BASE}/down", {"worktree": _pd_worktree}, timeout=_POD_DOWN_TIMEOUT_S
    )
    _pd_err = _pod_failure(_pd_resp, f"pod down {_pd_worktree!r}")
    if _pd_err:
        return _pd_err
    return f"Pod `{_pd_worktree}` is stopped and its data directory reclaimed."


def pod_status(name: str, args: dict[str, Any]) -> str:
    _ps_worktree = args["worktree"]
    _ps_resp = mcp_core._get(
        f"{_POD_API_BASE}/status?worktree={quote(_ps_worktree, safe='')}",
        timeout=_POD_READ_TIMEOUT_S,
    )
    _ps_err = _pod_failure(_ps_resp, f"pod status {_ps_worktree!r}")
    if _ps_err:
        return _ps_err
    _ps_limits = (
        f" memory={_ps_resp.get('memory')} cpu={_ps_resp.get('cpu')}"
        if _ps_resp.get("memory") or _ps_resp.get("cpu")
        else ""
    )
    return redact(
        f"Pod `{_ps_resp.get('name') or _ps_worktree}`: "
        f"{_ps_resp.get('status') or 'unknown'} "
        f"port={_ps_resp.get('port')} health={_ps_resp.get('health')}{_ps_limits}"
    )


def pod_ls(name: str, args: dict[str, Any]) -> str:
    _pl_resp = mcp_core._get(f"{_POD_API_BASE}/list", timeout=_POD_READ_TIMEOUT_S)
    _pl_err = _pod_failure(_pl_resp, "pod ls")
    if _pl_err:
        return _pl_err
    _pl_pods = _pl_resp.get("pods") or []
    if not _pl_pods:
        return "No pods are active on this host."
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
    return redact("\n".join(_pl_lines))


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "ops_mission_control_api": ops_mission_control_api,
    "pod_up": pod_up,
    "pod_restart": pod_restart,
    "pod_down": pod_down,
    "pod_status": pod_status,
    "pod_ls": pod_ls,
}
