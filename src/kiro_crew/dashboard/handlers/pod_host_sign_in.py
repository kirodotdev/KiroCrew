"""The host half of :mod:`kiro_crew.pod_host_sign_in`: lend a pod's gateway an access token.

* ``POST /api/pods/host-sign-in/token``: ``{"pod": <name>}`` -> the access token,
  its expiry and its account metadata (``pod_host_sign_in.ANSWER_KEYS``) from this
  host's own Crew vault, refreshed here.
* ``POST /api/pods/host-sign-in/refused``: ``{"pod": <name>, "servedDigest"?: <hex>}``
  -> ``{"refused": bool}``, the host's answer to ``kas_host_auth.sign_in_refused``
  for a token it lent.

Token auth lets both through only while the preview is on in the live
configuration (``token_auth_paths._exact_method_bypass``); every request is then
decided here, fail closed, in this order:

1. The preview is on (an LTS build reads it off) and this gateway is not itself a pod.
2. The request came on this gateway's own dashboard unix socket (never TCP, never
   the agent socket), from a peer the kernel reports as this user.
3. An enabled app holds an owner-approved ``permissions.api`` entry for these
   routes under its sealed host-exec grant
   (:func:`kiro_crew.apps.core_route_grants.core_route_grant`): the owner's consent
   to the app that runs pods. Core names no app here.
4. The peer's pid is the named pod unit's main process, which is the pod's
   gateway itself (the unit ``exec``s it), and it shares this gateway's
   namespaces (Linux) or runs unsandboxed (macOS). A sandboxed shell inside the
   pod is a different process in another namespace and is refused, and a pod
   that is down has no main process, so ``pod down`` ends its access.
5. A per-peer-pid rate limit in the dashboard's shared limiter
   (:mod:`kiro_crew.dashboard.create_rate_limit`), keyed by the kernel-attested pid from check 2, counted
   before checks 3 and 4 so a refused caller is bounded too. The claimed pod name
   does not select the budget.

Every decision is written to the Security Event Log. The answer is cut to
``pod_host_sign_in.ANSWER_KEYS``: no refresh token, client secret or other field leaves.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from aiohttp import web

from kiro_crew import pod_host_sign_in as phs
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard.handlers.core import _sel

logger = logging.getLogger(__name__)

#: Each route's budget in the dashboard's shared per-caller limiter.
_RATE_VERBS = {
    "token": create_rate_limit.POD_HOST_SIGN_IN_TOKEN,
    "refused": create_rate_limit.POD_HOST_SIGN_IN_REFUSED,
}

_MAX_BODY = 4096

#: The routes' namespace for :func:`kiro_crew.apps.core_route_grants.core_route_grant`.
NAMESPACE = phs.GRANT_NAMESPACE

_NOT_SIGNED_IN = (
    "The host has no Kiro Crew sign-in, so it has none to lend this pod. "
    "Run `kirocrew kas login` on the host."
)


def _audit(op: str, pod: str, outcome: str, reason: str = "") -> None:
    try:
        _sel().log_api_access(
            caller=f"pod:{pod}" if pod else "pod",
            operation=f"pod.host_sign_in.{op}",
            outcome=outcome,
            source="pod-host-sign-in",
            resources=pod,
            error=reason,
        )
    except Exception:  # noqa: BLE001 -- the decision stands either way
        logger.warning("pod host sign-in: could not record a security event", exc_info=True)


def _refuse(op: str, pod: str, status: int, code: str, message: str) -> web.Response:
    _audit(op, pod, "denied", code)
    return web.json_response({"code": code, "error": message}, status=status)


def _this_gateway_is_a_pod() -> bool:
    """A pod lends nothing on: it holds no sign-in of the host's to lend."""
    return os.environ.get("KIROCREW_POD") == "1"


def _peer_pid(request: web.Request) -> int | None:
    """The pid of a same-user peer on this gateway's dashboard socket, else ``None``."""
    from kiro_crew.dashboard.agent_ipc import request_on_agent_socket
    from kiro_crew.dashboard.token_auth import _unix_request_socket
    from kiro_crew.mcp_gateway.socketsec import PeerCredResult, check_peer_is_self, get_peer_pid

    if request_on_agent_socket(request):
        return None
    sock = _unix_request_socket(request)
    if sock is None or check_peer_is_self(sock) is not PeerCredResult.MATCH:
        return None
    pid = get_peer_pid(sock)
    return pid if isinstance(pid, int) and pid > 0 else None


def _not_pod_gateway(name: str, pid: int) -> str:
    """Why *pid* is not pod *name*'s running gateway, or ``""`` when it is. Blocking."""
    from kiro_crew import platform_compat
    from kiro_crew.pod import runtime
    from kiro_crew.pod.config import PodConfig
    from kiro_crew.pod.runtime import PodError

    if sys.platform not in ("linux", "darwin"):
        return "unsupported_platform"
    started = platform_compat.get_process_start_id(pid)
    if not started:
        return "peer_gone"
    try:
        main = runtime.main_pid(PodConfig.load(), name)
    # launchd's reader raises LaunchdError, a RuntimeError, not a PodError.
    except (PodError, RuntimeError, OSError):
        logger.warning("pod host sign-in: could not read pod %s's unit", name, exc_info=True)
        return "pod_unit_unreadable"
    if main is None:
        return "pod_not_running"
    if main != pid:
        return "not_pod_gateway"
    # The same process throughout: a pid reused between the two reads is refused.
    if platform_compat.get_process_start_id(pid) != started:
        return "peer_gone"
    if sys.platform == "linux":
        if platform_compat.process_namespaces_match(pid, os.getpid()) is not True:
            return "sandboxed_peer"
    elif platform_compat.process_is_sandboxed(pid) is not False:
        return "sandboxed_peer"
    return ""


async def _admit(request: web.Request, op: str) -> tuple[str, dict[str, Any], web.Response | None]:
    """Run checks 1-5 of the module docstring. ``(pod, body, refusal)``."""
    from kiro_crew.apps.core_route_grants import core_route_grant

    if not phs.live_preview_on() or _this_gateway_is_a_pod():
        return "", {}, _refuse(op, "", 404, "pod_host_sign_in_off", "Not found")
    pid = _peer_pid(request)
    if pid is None:
        return "", {}, _refuse(op, "", 403, "unix_socket_only", "Forbidden")
    if request.content_length is None or request.content_length > _MAX_BODY:
        return "", {}, _refuse(op, "", 400, "invalid_request", "Bad request")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- any unreadable body is the same bad request
        body = None
    name = body.get("pod") if isinstance(body, dict) else None
    from kiro_crew.pod import runtime
    from kiro_crew.pod.runtime import PodError

    try:
        runtime.validate_name(name if isinstance(name, str) else "")
    except PodError:
        return "", {}, _refuse(op, "", 400, "invalid_request", "Bad request")
    assert isinstance(name, str) and isinstance(body, dict)
    # Counted before the costly checks, so a refused caller is bounded too.
    if not create_rate_limit.allow_create(_RATE_VERBS[op], str(pid)):
        return name, body, _refuse(op, name, 429, "rate_limited", "Too many requests")
    grant = await asyncio.to_thread(core_route_grant, request.path, NAMESPACE)
    if not grant.holder:
        return name, body, _refuse(op, name, 403, "app_not_enabled", "No pod app is enabled")
    reason = await asyncio.to_thread(_not_pod_gateway, name, pid)
    if reason:
        return name, body, _refuse(op, name, 403, reason, "Forbidden")
    return name, body, None


async def api_pod_host_sign_in_token(request: web.Request) -> web.Response:
    """POST /api/pods/host-sign-in/token: an access token for a pod's own gateway."""
    from kiro_crew.agent_sdk.kas_vault_answer import (
        HostAuthCallbackError,
        HostNotSignedIn,
        answer_from_vault,
    )
    from kiro_crew.auth.login.endpoints import LOCAL_AGENT_UNAVAILABLE, stored_sign_in_registered

    name, _body, refusal = await _admit(request, "token")
    if refusal is not None:
        return refusal
    # A sign-in on an edition's own start URL is not one a local agent may run on
    # (``KasHarness.resolve_spawn``), so a pod's engine is not lent it either.
    if await asyncio.to_thread(stored_sign_in_registered):
        return _refuse("token", name, 403, "host_sign_in_not_lendable", LOCAL_AGENT_UNAVAILABLE)
    try:
        answer = phs.filter_answer(await answer_from_vault())
    except HostNotSignedIn:
        return _refuse("token", name, 409, "host_not_signed_in", _NOT_SIGNED_IN)
    except HostAuthCallbackError as exc:
        return _refuse("token", name, 503, "host_sign_in_failed", str(exc))
    # Asked again once the answer is in hand: a sign-in switched to a company one
    # while the vault answered is refused, not lent.
    # shortcut: two reads, not one snapshot; a switch to a company sign-in and back
    # inside one answer would pass. Bind the check to the answer's own identity if the
    # vault answer ever carries it.
    if await asyncio.to_thread(stored_sign_in_registered):
        return _refuse("token", name, 403, "host_sign_in_not_lendable", LOCAL_AGENT_UNAVAILABLE)
    if not all(k in answer for k in phs.REQUIRED_KEYS):
        return _refuse("token", name, 503, "host_sign_in_failed", "refresh failed")
    _audit("token", name, "granted", f"expires {answer['expiresAt']}")
    return web.json_response(answer)


async def api_pod_host_sign_in_refused(request: web.Request) -> web.Response:
    """POST /api/pods/host-sign-in/refused: whether the lent sign-in is refused."""
    from kiro_crew.agent_sdk.kas_vault_answer import vault_sign_in_refused

    name, body, refusal = await _admit(request, "refused")
    if refusal is not None:
        return refusal
    served = body.get("servedDigest")
    refused = await vault_sign_in_refused(served if phs.is_digest(served) else None)
    _audit("refused", name, "granted", f"refused={refused}")
    return web.json_response({"refused": refused})
