"""Ask an opencode session's remote MCP servers, with no credentials, whether they want a sign-in.

opencode answers a remote server's ``401`` with a sign-in of its own and sends no ACP
frame about it, so the session's MCP report never lists the server as waiting and
the chat says nothing. After the session opens, this probe sends each remote element
that sends no header of its own (so none that could carry a credential) one MCP
``initialize``, and a ``401`` whose ``WWW-Authenticate`` is a Bearer challenge (read
by ``mcp_discovery_remote._is_bearer_challenge``, as the remote MCP row probe reads it)
lists that server in the report's advisory ``sign_in_suspected``
(:meth:`McpSessionReport.record_sign_in_asked`), which the chat's one notice reads.
It is never put in ``awaiting_auth`` or ``not_ready()``: opencode may already hold
a sign-in the probe cannot see, so no reader may call the server unusable, and it
stays loaded.
Anything else -- a ``200``, another status, a refused URL, a timeout, an error --
records nothing.

The fetch is the remote MCP row probe's (:mod:`kiro_crew.mcp_discovery_remote`):
the URL is SSRF-vetted (``link_unfurl.vet_unfurl_url``, any port, private hosts
allowed, link-local and metadata refused), the socket is pinned to the vetted
address, and redirects are not followed. No credential is sent and no body is read.
It runs in the background (:func:`schedule_sign_in_probe`), so it never delays a
session start, and each request is bounded by :data:`PROBE_TIMEOUT_SECS`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any

from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.mcp_discovery_probe_state import MCP_CLIENT_PROTOCOL_VERSION

logger = logging.getLogger(__name__)

#: The whole budget of one server's probe: the DNS vet and the request together.
PROBE_TIMEOUT_SECS = 5.0
#: How many remote servers one session probes; the rest are not asked.
MAX_PROBED_SERVERS = 16

_PENDING_TASKS: set[asyncio.Task[Any]] = set()


def _carries_headers(element: Mapping[str, Any]) -> bool:
    """Whether a remote *element* sends any header of its own.

    Any header, not only ``Authorization``: a server may authenticate with a key
    header of its own (``X-API-Key``), and a probe that drops it would read that
    server's refusal as a sign-in request.
    """
    pairs = element.get("headers")
    return isinstance(pairs, list) and any(isinstance(pair, Mapping) for pair in pairs)


def sign_in_candidates(servers: Any) -> list[tuple[str, str]]:
    """``(name, url)`` of each ``http``/``sse`` element that sends no header of its own.

    At most :data:`MAX_PROBED_SERVERS`, in array order.
    """
    out: list[tuple[str, str]] = []
    for element in servers if isinstance(servers, list) else ():
        if not isinstance(element, dict) or element.get("type") not in ("http", "sse"):
            continue
        name, url = element.get("name"), element.get("url")
        if not isinstance(name, str) or not name or not isinstance(url, str) or not url:
            continue
        if _carries_headers(element):
            continue
        out.append((name, url))
        if len(out) >= MAX_PROBED_SERVERS:
            break
    return out


async def asks_for_sign_in(url: str, *, timeout: float = PROBE_TIMEOUT_SECS) -> bool:
    """Whether *url* answers a credential-free ``initialize`` with ``401`` + a Bearer challenge.

    The challenge is judged by the remote MCP row probe's own reader,
    ``mcp_discovery_remote._is_bearer_challenge`` (a ``Bearer`` scheme with an https
    ``resource_metadata`` or a ``scope``), so the two probes agree on what counts.
    ``False`` on every other outcome, including a URL the vet refuses and any error.
    """
    import aiohttp

    from kiro_crew.link_unfurl import UnfurlRejected, pinned_connector, vet_unfurl_url
    from kiro_crew.mcp_discovery_remote import _is_bearer_challenge

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_CLIENT_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "kirocrew-probe", "version": "1.0.0"},
        },
    }

    async def _ask() -> bool:
        vetted = await asyncio.to_thread(
            vet_unfurl_url, url, allow_any_port=True, allow_private=True
        )
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), connector=pinned_connector(vetted)
        ) as session:
            async with session.post(
                url,
                json=body,
                headers={"Accept": "application/json, text/event-stream"},
                allow_redirects=False,
            ) as resp:
                return resp.status == 401 and _is_bearer_challenge(
                    resp.headers.get("WWW-Authenticate", "")
                )

    try:
        return await asyncio.wait_for(_ask(), timeout=timeout)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, UnfurlRejected):
        return False


async def run_sign_in_probe(
    report: McpSessionReport,
    servers: Any,
    *,
    alive: Callable[[], bool] | None = None,
    ask: Callable[[str], Any] = asks_for_sign_in,
) -> list[str]:
    """Probe *servers*' remote elements and record the ones asking for a sign-in.

    Records nothing when the report has moved on to another attempt or the session
    closed while the probes ran. Returns the names recorded.
    """
    candidates = sign_in_candidates(servers)
    if not candidates:
        return []
    generation = report.generation
    answers = await asyncio.gather(*(ask(url) for _name, url in candidates))
    if report.generation != generation or (alive is not None and not alive()):
        return []
    asking = [name for (name, _url), yes in zip(candidates, answers) if yes is True]
    recorded = report.record_sign_in_asked(asking)
    if recorded:
        logger.info(
            "opencode session MCP: %d remote server(s) answered a credential-free probe "
            "with a sign-in challenge; noted as possibly needing a sign-in: %s",
            len(recorded),
            ", ".join(recorded),
        )
    return recorded


def schedule_sign_in_probe(
    report: McpSessionReport, servers: Any, *, alive: Callable[[], bool] | None = None
) -> None:
    """Start :func:`run_sign_in_probe` in the background. Never raises.

    No-ops without a running loop or when *servers* holds no remote candidate.
    """
    if not sign_in_candidates(servers):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _guarded() -> None:
        try:
            await run_sign_in_probe(report, servers, alive=alive)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("session MCP sign-in probe failed", exc_info=True)

    task = loop.create_task(_guarded(), name="acp-mcp-sign-in-probe")
    _PENDING_TASKS.add(task)
    task.add_done_callback(_PENDING_TASKS.discard)
