"""MCP server exposing spawn, learn, and task tools to kiro-cli.

Runs as ``kirocrew mcp-core`` — kiro-cli spawns it as a child process
and calls tools via JSON-RPC over stdio (MCP protocol).

Tools:
    spawn_run       — spawn a background subagent
    spawn_list      — list running/completed subagents
    spawn_status    — retrieve live status and partial/full subagent output
    resource_status — check host resource headroom before heavy work
    learn_add       — save a learned correction
    learn_list      — list one window of lessons, with the total
    learn_remove    — remove lessons by substring
    task_run        — start the autonomous task runner
"""

from __future__ import annotations

import contextlib
import copy
import importlib as _importlib
import json
import re as _re
import socket
import sys as _sys
import threading
import time
import urllib.error
import urllib.request
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew import _facade_mirror
from kiro_crew.agent_discovery import list_agents
from kiro_crew.autonudge import binding_key_for, structured_monitor_binding_key_for
from kiro_crew.config.loader import (  # noqa: F401
    KiroCrewConfig,
    config_dir,
    outbox_dir,
    read_config_text,
    read_local_secret,
    resolve_agent_bindings,
)
from kiro_crew.context_management import summarize_result
from kiro_crew.dashboard.origin import dashboard_socket_path  # noqa: F401
from kiro_crew.history import _SEARCH_SCAN_WINDOW as SEARCH_SCAN_WINDOW  # noqa: F401
from kiro_crew.history import (  # noqa: F401
    ConversationLog,
    is_incognito_transcript,
    snippet_needles,
)

# Bound on the facade for mcp_core_parts/history.py: unbound, the mirror would
# serve these from outside its family (pinned by test_mcp_core_facade.py).
from kiro_crew.history_search import snippet_needle_tiers, tiered_first_hit  # noqa: F401
from kiro_crew.knowledge.dedup import dedup_sweep
from kiro_crew.knowledge.embedder import create_embedder_from_config
from kiro_crew.knowledge.retrieval import HybridRetriever, vector_leg
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.loopback_http import loopback_urlopen  # noqa: F401
from kiro_crew.mcp_caller import (  # noqa: F401
    CallerContext,
    current_caller,
    host_pid_is_shared,
    resolve_own_identity,
    set_current_caller,
)
from kiro_crew.mcp_core_parts.gateway import (
    _REFUSED_RETRY_BACKOFFS,
    _api_urlopen,
    _caller_header,
    _http_error_body,
    _internal_secret,
    _refused_message,
    _replay_target,
    _resolve_api_target,
    _reverify_refused_target,
    _secret_for_base,
    _session_key_header_error,
    _session_token_header,
    _transport_failure,
)
from kiro_crew.mcp_core_parts.governance import _deny_channel_agent_dispatch
from kiro_crew.mcp_core_parts.identity import _resolve_session_key
from kiro_crew.mcp_shared import (  # noqa: F401
    DeferredTool,
    call_tool_with_logging,
    drive_deferred,
    external_client_identity_note,
    internal_caller,
    map_deferred,
    run_mcp_stdio_loop,
    spawned_without_gateway_identity,
)
from kiro_crew.mcp_tool_titles import with_titles
from kiro_crew.mcp_tools import build_tool_list, build_tool_names, dispatch
from kiro_crew.members import record_activity  # noqa: F401
from kiro_crew.memory_stores import UnknownMemoryStore  # noqa: F401
from kiro_crew.messaging.link import is_legacy_slack_key, legacy_key  # noqa: F401
from kiro_crew.platform import redact_via_context as redact  # noqa: F401
from kiro_crew.port_resolution import port_is_gateway_owned, resolve_client_port_src  # noqa: F401
from kiro_crew.security import (  # noqa: F401
    redact_credentials,
    redact_exfiltration_urls,
    redact_local_paths,
)
from kiro_crew.sel import sel
from kiro_crew.session_directive import (
    CORE_MCP_SERVER,
    DIRECTIVE_TOOLS,
    clear_vouch,
    refuse_if_markerless,
)
from kiro_crew.session_token_sig import session_key_from_env_token  # noqa: F401
from kiro_crew.skills import SkillsLoader
from kiro_crew.trigger_match import rank_triggered  # noqa: F401
from kiro_crew.validation import (
    MCP_CORE_SCHEMAS,
    coerce_mcp_tool_args,
    validate_tool_args,
)

# Bindings the tool handlers in ``mcp_tools`` read as attributes of THIS module
# rather than importing themselves, so that a test rebinding one --
# ``patch("kiro_crew.mcp_core.sel")``, ``patch("kiro_crew.mcp_core.time")`` --
# still intercepts the handler. Naming them here keeps that dependency visible
# and stops a future import cleanup from deleting a binding nothing in this file
# appears to use.
_HANDLER_SURFACE = (
    HybridRetriever,
    SkillsLoader,
    dedup_sweep,
    list_agents,
    outbox_dir,
    sel,
    summarize_result,
    time,
    vector_leg,
)


# How often a sleeping `wait` polls /api/session-keepalive.
#
# Two jobs in one round-trip: keeping the session's activity clock warm (the
# staleness watchdog alone would be satisfied by 60s) and collecting an
# early-end request from the dashboard. The second job sets the value -- it is
# the upper bound on how long the "End wait" button appears to do nothing, so
# it is deliberately tightened to the loop's own sleep granularity. The handler
# it calls only touches two timestamps, so a 30-minute wait costs ~360 loopback
# POSTs and no meaningful work.
WAIT_PING_SECS = 5.0

# Ping cadence for a sleep that cannot publish (identity not authoritative, so no
# countdown and no button). Only the staleness watchdog cares at that point, and
# 60s satisfies it -- the same interval spawn_sub_agents' blocking poll uses.
WAIT_STALENESS_PING_SECS = 60.0

# Context cap for one `skill_fetch` body. The gateway's preview endpoint
# already caps at 64 KiB for the dashboard's detail panel; a tool result is
# spent from the conversation's context budget instead, so cap it again
# lower. 32 KiB (~8k tokens) covers essentially every real SKILL.md while
# keeping one fetch from dominating the window.
_SKILL_FETCH_MAX_CHARS = 32 * 1024


def _list_tools() -> list[dict[str, Any]]:
    """Tool descriptors served for ``tools/list``.

    Declared per domain under :mod:`kiro_crew.mcp_tools`; this stays the
    entry point kiro-cli and in-process discovery both read.
    """
    return with_titles(CORE_MCP_SERVER, build_tool_list())


def _list_tool_names() -> list[str]:
    """Tool NAMES only, without assembling descriptions.

    The names-only read path ``mcp_discovery._managed_tools_in_process`` prefers
    when it keeps only names. It returns exactly the names ``_list_tools`` would,
    in the same order, but ``build_tool_names`` takes the ``names_only`` path so
    the two descriptions that reach for a live value (the sub-agent cap and
    agents-directory scan in ``mcp_tools.spawn``, the config reads in
    ``mcp_tools.control``) are never performed. ``with_titles`` is intentionally
    not applied: it only decorates descriptions, which a names-only caller
    discards.
    """
    return build_tool_names()


# ── Knowledge-search store/embedder cache ──
#
# local_knowledge_search runs per LLM tool call in a long-lived MCP server.
# Rebuilding KnowledgeStore every call re-runs the schema DDL, an orphan-cleanup
# DELETE transaction, and a full SELECT of all entities/relations into the
# in-memory graph; rebuilding the embedder re-runs the model availability probe
# (up to 3s when configured). We cache both, keyed on a signature of the DB
# files (main + -wal, since WAL commits land in -wal) and config.json, so
# out-of-band dashboard ingestion or config edits trigger a rebuild on the next
# call. The MCP stdio loop services calls serially, but a lock keeps this safe
# if that ever changes.
_KNOWLEDGE_CACHE_LOCK = threading.Lock()
# (signature_tuple, KnowledgeStore, embedder_or_None)
_KNOWLEDGE_CACHE: tuple[tuple, Any, Any] | None = None


def _knowledge_db_signature(db_path: Path, cfg_path: Path) -> tuple:
    """Cheap fingerprint of the knowledge DB (+WAL) and config files.

    Any ingestion (which writes the main DB or its -wal sidecar) or config edit
    changes this, busting the cache so a fresh search sees new data / embedder.
    """
    sig: list = []
    wal_path = db_path.with_name(db_path.name + "-wal")
    for p in (db_path, wal_path, cfg_path):
        try:
            st = p.stat()
            sig.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((str(p), None))
    return tuple(sig)


def _get_knowledge_search(db_path: Path, cfg_path: Path) -> tuple[Any, Any]:
    """Return a cached ``(KnowledgeStore, embedder)`` pair, rebuilding on change.

    Rebuilds (and closes the prior connection) only when the DB/WAL/config
    signature changes; otherwise reuses the live store + embedder, avoiding the
    per-call schema/migrate/graph-load and embedder availability probe.
    """
    global _KNOWLEDGE_CACHE
    sig = _knowledge_db_signature(db_path, cfg_path)
    with _KNOWLEDGE_CACHE_LOCK:
        if _KNOWLEDGE_CACHE is not None and _KNOWLEDGE_CACHE[0] == sig:
            return _KNOWLEDGE_CACHE[1], _KNOWLEDGE_CACHE[2]
        # Rebuild. Build the new store FIRST; only close the stale connection
        # after the build succeeds. If KnowledgeStore.__init__ raises (locked or
        # corrupt DB, disk-full during the migrate DELETE), we leave the existing
        # cache entry — and its still-open connection — intact rather than
        # stranding a closed connection in the cache for the next caller.
        prev = _KNOWLEDGE_CACHE
        store = KnowledgeStore(str(db_path))
        try:
            cfg = json.loads(read_config_text(cfg_path)) if cfg_path.exists() else {}
        except Exception:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        embedder = create_embedder_from_config(cfg)
        # Close the stale connection only AFTER the full rebuild (store + cfg +
        # embedder) succeeds. If any step above raised, the existing cache entry
        # — and its open connection — is left intact and usable for the next call.
        if prev is not None:
            with contextlib.suppress(Exception):
                prev[1].db.close()
        # Re-fingerprint AFTER building: KnowledgeStore.__init__ creates/migrates
        # the DB (writing the file + -wal), so the pre-build signature no longer
        # matches the on-disk state. Caching under the post-build signature lets
        # the next idle call hit the cache instead of rebuilding every time.
        post_sig = _knowledge_db_signature(db_path, cfg_path)
        _KNOWLEDGE_CACHE = (post_sig, store, embedder)
        return store, embedder


def _send(
    path: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str],
    method: str = "GET",
    timeout: float = 30,
    mark_transport_error: bool = False,
) -> dict:
    """Send one gateway request, recovering from a refused connection.

    Two layers, in order. A refused connection may mean the resolved base is
    stale: the gateway came up, or moved to another port, after this tool server
    booted, and that port is recorded only in the run marker — so the base is
    re-resolved and the request replayed once, but only when re-resolution
    actually produced a different base (the rule lives in
    :func:`_replay_target`). When there is no moved gateway to chase, the base
    that refused is the only candidate, and a gateway RESTARTING on its own port
    lands there: it gets a short bounded retry of the same target
    (``_retry_refused``) before the caller is told, actionably, that the gateway
    is unreachable.

    Every verb goes through here. Keeping both layers in one place is what stops
    PATCH-shaped calls from staying pinned to a base that POST already learned
    was wrong.
    """

    def _once(target: tuple[str, str], hdrs: dict[str, str] | None = None) -> dict:
        base, socket_path = target
        req = urllib.request.Request(
            f"{base}{path}", data=data, headers=hdrs if hdrs is not None else headers, method=method
        )
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- URL is the loopback gateway (_resolve_api_target(): 127.0.0.1 plus a port from config/env or a run-marker whose ownership is re-verified per request) + a fixed internal path; never user-controlled  # noqa: E501
        with _api_urlopen(req, timeout=timeout, unix_socket_path=socket_path) as resp:
            return json.loads(resp.read())

    def _refreshed_headers(base: str) -> dict[str, str]:
        """*headers* with the credential re-read for *base*, caller's dict intact.

        A copy, never an in-place edit: ``headers`` belongs to the caller and is
        reused for its own error reporting. An unreadable secret leaves the
        original value rather than sending an empty one, so a transient read
        failure cannot turn a retry into a guaranteed 403.
        """
        fresh = dict(headers)
        secret = _secret_for_base(base)
        if secret:
            fresh["X-Internal-Secret"] = secret
        return fresh

    def _retry_refused(refused: tuple[str, str], refusal: urllib.error.URLError) -> dict:
        """Re-dial the SAME refused target a few times, then report it actionably.

        A refusal is the one failure that is safe to replay: the connect itself
        never completed, so nothing was handed to the gateway and the request
        cannot have been executed — an identical retry therefore cannot
        double-execute a verb. That invariant is why this loop retries ONLY
        ``ConnectionRefusedError`` / ``socket.gaierror``; every other failure
        keeps its existing route, because an ``HTTPError`` carries a real
        response and any post-connect failure leaves acceptance undetermined,
        and spawn_run's reconcile depends on that ambiguity being reported
        rather than retried.

        The window this covers is a gateway restarting on its own port, which
        :func:`_replay_target` deliberately declines to chase — there is no
        moved base to find, so before this the caller got the raw errno with no
        retry at all. On exhaustion it gets the port and the restart hypothesis
        instead, with the raw error appended.

        Sleeping does NOT let the target go unproven. The ownership proof is per
        ATTEMPT, so every re-dial re-resolves through
        :func:`_reverify_refused_target` and proceeds only while positive
        evidence still names this same base — otherwise the port could have been
        taken by another local process during the backoff, and the retry would
        hand it the internal secret.

        A check that cannot prove the target consumes one step of the budget
        rather than ending it. "Not proven" is the EXPECTED reading mid-restart:
        between the old process releasing the port and the new one binding it
        nothing is listening, so no ownership can be shown — and that is the very
        window this retry exists to cover. Abandoning the schedule on the first
        such reading would have limited recovery to a gateway that rebinds inside
        the FIRST backoff, discarding the longer half of the budget for the case
        it was sized for. The schedule stays the bound: when the last step is
        still unproven, the caller gets the same actionable message as a
        dialled-and-refused exhaustion.
        """
        for backoff in _REFUSED_RETRY_BACKOFFS:
            time.sleep(backoff)
            # Re-prove ownership AFTER the sleep, never before it: the whole
            # point is that the gateway may have gone during the pause.
            proven = _reverify_refused_target(refused[0])
            if proven is None:
                # Spend the next step rather than the whole schedule: a gateway
                # part-way through rebinding is unprovable but about to be back.
                continue
            # The target was re-proven; the CREDENTIAL has to be re-read for it
            # too. A gateway that restarted on this port is a new generation with
            # a new per-listener secret, so replaying the header this request was
            # built with would earn a 403 instead of the retry this exists for.
            try:
                return _once(proven, _refreshed_headers(proven[0]))
            except urllib.error.HTTPError as exc:
                return _http_error_body(exc)
            except urllib.error.URLError as exc:
                if not isinstance(exc.reason, (ConnectionRefusedError, socket.gaierror)):
                    return _transport_failure(str(exc), mark_transport_error)
                refusal = exc
            except Exception as exc:
                return _transport_failure(str(exc), mark_transport_error)
        # ``refused`` is the one failure that proves nothing reached a gateway,
        # so a caller may fall back to a local path without risking a replay.
        return {"error": _refused_message(refused[0], refusal), "refused": True}

    # ONE resolution for this attempt; both transports derive from it.
    target = _resolve_api_target()
    base = target[0]
    try:
        return _once(target)
    except urllib.error.HTTPError as e:
        # urlopen raises HTTPError on 4xx/5xx; str(e) is only "HTTP Error 400:
        # Bad Request" — the structured {"error": ...} body lives in e.read().
        # Surface it so callers can act on the backend's actual error (e.g.
        # the learn_add "unknown session" mapping) instead of an opaque code.
        return _http_error_body(e)
    except urllib.error.URLError as e:
        if not isinstance(e.reason, (ConnectionRefusedError, socket.gaierror)):
            return _transport_failure(str(e), mark_transport_error)
        # THE replay rule lives in _replay_target, shared with
        # mcp_computer._invoke: None means "do not replay" (no evidence, or the
        # re-resolution named the same dead base). Nothing was handed to a live
        # gateway in either case, so no refusal below carries transport
        # ambiguity — the outcome is a definite rejection once the retry window
        # for the refused target is spent.
        retry_target = _replay_target(base)
        if retry_target is None:
            # No moved gateway to chase, so the base that refused is the only
            # candidate — the same-port restart case. Give it the short retry
            # window rather than surfacing a bare errno.
            return _retry_refused(target, e)
        try:
            return _once(retry_target)
        except urllib.error.HTTPError as retry_exc:
            return _http_error_body(retry_exc)
        except urllib.error.URLError as retry_exc:
            if isinstance(retry_exc.reason, (ConnectionRefusedError, socket.gaierror)):
                # Both bases refused. The re-resolved one is the fresher
                # evidence of where the gateway lives, so the retry window
                # applies to it.
                return _retry_refused(retry_target, retry_exc)
            return _transport_failure(str(retry_exc), mark_transport_error)
        except Exception as retry_exc:
            # The replay reached the gateway and failed afterwards (a read timeout
            # after a spawn was accepted, say). Acceptance is undetermined, so this
            # must carry the same ambiguity flag as a first-attempt post-connect
            # failure — otherwise spawn_run reconciles a still-running member down
            # and orphans it.
            return _transport_failure(str(retry_exc), mark_transport_error)
    except Exception as e:
        # The request may have reached the gateway before the response failed
        # (for example, a read timeout after spawn acceptance). Callers must
        # not present this as a definite rejection or retry automatically.
        return _transport_failure(str(e), mark_transport_error)


def _post(
    path: str,
    body: dict | None = None,
    *,
    timeout: float = 30,
    session_key: str | None = None,
) -> dict:
    """POST a gateway endpoint.

    ``session_key``: as in :func:`_patch`, and it matters for the same reason.
    The default resolution is :func:`_resolve_session_key`, which includes the
    ``/proc`` ancestor walk, so a caller that already gated itself on
    :func:`_resolve_session_key_strict` and then let this helper re-resolve
    would CHECK one identity and WRITE under another — the walk can land on an
    ancestor slot, which for an app-owned session means the write arrives
    looking like the unconfined person. Such a caller must pass the key it
    verified. It is still validated by ``_session_key_header_error``.
    """
    data = json.dumps(body or {}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Secret": _internal_secret(),
        **_caller_header(),
        **_session_token_header(),
    }
    sk = _resolve_session_key() if session_key is None else session_key
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    # ``transport_error`` means acceptance is unknown. A caller that reads it
    # (spawn_run's batch reconcile, the cron tool proxy, learn_add) must not
    # report a definite failure; other _post callers treat the payload as a
    # normal error.
    return _send(
        path,
        data=data,
        headers=headers,
        method="POST",
        timeout=timeout,
        mark_transport_error=True,
    )


def _get(path: str, session_key: str | None = None, *, timeout: float = 10) -> dict:
    """GET a loopback gateway path with the internal-secret handshake.

    ``session_key`` exists so a caller that has ALREADY verified its identity can
    send the key it verified, instead of having this helper resolve one again. The
    default resolution is :func:`_resolve_session_key`, which includes the ``/proc``
    ancestor walk — fine for read-only telemetry, but for a caller gated on
    :func:`_resolve_session_key_strict` re-resolving here is a check-then-use
    split: the gate proves a strict identity exists, and this then attaches
    whatever the lenient walk answers at request time, which need not be the same
    session. Passing the verified key makes the value that was checked the value
    that is used. It is still validated by ``_session_key_header_error``.

    ``timeout`` defaults to the 10s a telemetry read needs. A route that does real
    work behind the GET raises it — the Dev Fleet pod reads spawn a
    ``python -m kiro_crew pod`` subprocess in the gateway, so they pay a cold
    interpreter start that 10s cannot cover on a loaded host.
    """
    headers = {
        "X-Internal-Secret": _internal_secret(),
        **_caller_header(),
        **_session_token_header(),
    }
    sk = _resolve_session_key() if session_key is None else session_key
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    return _send(path, headers=headers, timeout=timeout)


def _patch(path: str, body: dict | None = None, *, session_key: str | None = None) -> dict:
    """PATCH a loopback gateway path with the internal-secret handshake.

    ``session_key``: as in :func:`_put`. A caller gated on
    :func:`_resolve_session_key_strict` must send the key it verified —
    re-resolving through the lenient walk here would let the request carry a
    different session's authority than the one the gate approved.
    """
    data = json.dumps(body or {}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Secret": _internal_secret(),
        **_caller_header(),
        **_session_token_header(),
    }
    sk = _resolve_session_key() if session_key is None else session_key
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    return _send(path, data=data, headers=headers, method="PATCH")


def _put(path: str, body: dict | None = None, session_key: str | None = None) -> dict:
    """PUT to a loopback gateway path with the internal-secret handshake.

    Same trust model as :func:`_post` / :func:`_patch` — the target path must be
    listed in ``dashboard.server._MIXED_INTERNAL_API_PATHS`` (or the strict set)
    or the gateway answers 403 ``Token required``.

    ``session_key``: as in :func:`_get`, and it matters more here because this is
    the WRITE. A caller gated on :func:`_resolve_session_key_strict` must send the
    key it verified; re-resolving through the lenient walk would let the request
    carry a different session's authority than the one the gate approved, which on
    this path means writing another crew's work item and public ledger.
    """
    data = json.dumps(body or {}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Secret": _internal_secret(),
        **_caller_header(),
        **_session_token_header(),
    }
    sk = _resolve_session_key() if session_key is None else session_key
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    return _send(path, data=data, headers=headers, method="PUT")


def _delete(path: str, body: dict | None = None, *, session_key: str | None = None) -> dict:
    """DELETE a loopback gateway path with the internal-secret handshake.

    ``session_key``: as in :func:`_patch`. A caller gated on
    :func:`_resolve_session_key_strict` must send the key it verified.
    """
    data = json.dumps(body or {}).encode() if body else None
    headers = {
        "X-Internal-Secret": _internal_secret(),
        **_caller_header(),
        **_session_token_header(),
    }
    sk = _resolve_session_key() if session_key is None else session_key
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    if data:
        headers["Content-Type"] = "application/json"
    return _send(path, data=data, headers=headers, method="DELETE", timeout=10)


def _autonudge_binding_key(sk: str) -> str | None:
    """Map a session key to its AutoNudge binding key, or None if unsupported.

    ``dashboard:chat-N-TS`` → bare slot key ``chat-N-TS`` (the autonudge REST
    layer keys dashboard loops on the bare slot key); ``slack:``/``discord:``
    session keys pass through unchanged (channel-bound loops). Anything else
    (``cron:``, ``hook:``, ``subagent:`` ...) is not a nudge-able session.

    Delegates to ``autonudge.binding_key_for`` so the MCP tool and the workflow
    ``ctx.nudge`` port share one definition of "nudge-able".
    """
    return binding_key_for(sk)


def _structured_monitor_binding_key(sk: str) -> str | None:
    """Map a session key only when structured wake delivery is supported."""
    return structured_monitor_binding_key_for(sk)


def _artifact_ref_link(slug: str, name: str) -> str:
    """Render a clickable ``[<name>](/artifacts/<slug>)`` markdown link.

    The chat renderer turns this into an anchor the frontend intercepts to open
    the artifact in the side panel; ``/artifacts/<slug>`` is the canonical
    full-page route, so it also degrades to a normal navigation if interception
    is absent. Used for non-widget kinds, which (unlike widgets) don't
    round-trip via ``<mcwidget>`` and otherwise have no clickable form in chat.
    """
    # name/slug are LLM-influenced and rendered verbatim on the dashboard, so
    # scrub for credential / exfiltration patterns (same guard as other
    # tool-result paths).
    label = name or slug
    label, _ = redact_exfiltration_urls(label)
    label, _ = redact_credentials(label)
    # Unescaped ']' would break the markdown link syntax.
    label = label.replace("[", "(").replace("]", ")")
    # A literal newline in the label splits the link text across lines, breaking
    # the single-line markdown anchor — collapse CR/LF to spaces so a crafted
    # name can't fragment the rendered link.
    label = label.replace("\r", " ").replace("\n", " ")
    safe_slug, _ = redact_exfiltration_urls(slug or "")
    safe_slug, _ = redact_credentials(safe_slug)
    # Constrain to the slug charset so a crafted value can't inject ')'/markdown
    # out of the URL.
    safe_slug = _re.sub(r"[^a-z0-9-]", "", safe_slug.lower())
    # If sanitization leaves no slug (e.g. the '?' fallback or an all-redacted
    # value), a link would dangle at /artifacts/ with no target — degrade to
    # plain text so the name still surfaces without a broken anchor.
    if not safe_slug:
        return label
    return f"[{label}](/artifacts/{safe_slug})"


def _resolve_artifact_folder_id(ref: str) -> tuple[str, str | None]:
    """Resolve an artifact-folder reference (id or human path) to a folder id.

    Read-only: fetches ``/api/artifact-folders`` and matches by id, then walks
    ``/``-separated path segments against folder names (case-insensitive). Used
    by the rename/move/delete MCP tools, which must address an existing folder
    (no auto-create — that only happens on save/move to an artifact folder,
    handled server-side). Returns ``(folder_id, error)``; ``""`` = root.
    """
    ref = str(ref or "").strip()
    if not ref or ref.lower() == "root":
        return "", None
    d = _get("/api/artifact-folders")
    if d.get("error"):
        return "", d["error"]
    folders = d.get("folders", [])
    by_id = {f.get("id"): f for f in folders if isinstance(f, dict) and f.get("id")}
    if ref in by_id:
        return ref, None
    segments = [s.strip().lower() for s in ref.split("/") if s.strip()]
    if not segments:
        return "", None
    parent = ""
    cur = ""
    for seg in segments:
        match = next(
            (
                f
                for f in folders
                if str(f.get("parent_id") or "") == parent
                and str(f.get("name", "")).strip().lower() == seg
            ),
            None,
        )
        if match is None:
            safe_ref, _ = redact_exfiltration_urls(ref)
            safe_ref, _ = redact_credentials(safe_ref)
            return "", f"folder not found: {safe_ref}"
        cur = str(match.get("id") or "")
        parent = cur
    return cur, None


def _artifact_reemit_hint(slug: str, name: str, kind: str = "widget") -> str:
    """Render the canonical re-emit-this-artifact-in-chat instruction.

    Appended to artifact_save / artifact_get / artifact_update tool
    responses so the agent has the exact tag string in context at the
    moment it's about to render the artifact in chat. The artifacts
    skill says ``slug=`` is required on every re-emission of a saved
    artifact, but skill rules can be overlooked at emission time —
    session logs confirmed an LLM had the slug in front of
    it twice (artifact_get response + artifact_update response) and
    still emitted ``<mcwidget title="...">`` without the attribute,
    creating a duplicate artifact when the user clicked save.

    The hint reduces this to "copy the tag I just gave you."
    """
    if kind != "widget":
        # Non-widget artifacts (markdown, html, svg, json, text) don't
        # round-trip through `<mcwidget>` — they render via the artifact
        # detail page or MarkdownPanel. No re-emit hint needed.
        return ""
    safe_name = (name or "").replace('"', "'")
    return (
        "When you re-emit this widget in chat, use this exact opening tag\n"
        "(slug attribute is REQUIRED — without it, the user clicking save\n"
        "creates a duplicate artifact):\n\n"
        f'<mcwidget title="{safe_name}" slug="{slug}">'
    )


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate tool arguments against schema. Returns cleaned args."""
    schema = MCP_CORE_SCHEMAS.get(name)
    if schema:
        # Repair an int that an upstream deferred-schema runtime re-typed from a
        # numeric-looking string ("42" -> 42) back to its string form on a
        # string field, BEFORE validation. This lives at the MCP entry point —
        # not in the shared validate_field — so the dashboard HTTP endpoints
        # keep their original contract (see coerce_mcp_tool_args).
        return validate_tool_args(coerce_mcp_tool_args(args, schema), schema)
    return args  # tools without schemas pass through


#: The ``tools/call`` name and RAW (pre-validation) arguments of the call in
#: flight, installed by :func:`_call_tool` and cleared on its exit. Read by
#: ``mcp_tools.control._emit_directive`` to report the CALL to the gateway, which
#: derives the directive from it (see :func:`derive_directive`). ContextVars so a
#: pooled server handling concurrent calls never reports one call under another.
_CURRENT_CALL_NAME: ContextVar[str] = ContextVar("kirocrew_current_call_name", default="")
_CURRENT_CALL_RAW_ARGS: ContextVar[dict[str, Any]] = ContextVar(
    "kirocrew_current_call_raw_args", default={}
)


def current_call_name() -> str:
    """The ``tools/call`` name of the call in flight, or ``""``."""
    return _CURRENT_CALL_NAME.get()


def current_call_raw_args() -> dict[str, Any]:
    """The RAW ``tools/call`` arguments of the call in flight (pre-validation)."""
    return copy.deepcopy(_CURRENT_CALL_RAW_ARGS.get())


#: When set, ``mcp_tools.control._emit_directive`` records its validated
#: ``(kind, args)`` here INSTEAD of POSTing it. This is how the gateway derives a
#: directive's payload itself: it re-runs the directive tool on the raw call
#: arguments the MCP stub reported, so the applied payload is a function of the
#: victim's own call and never of a caller-supplied body. ``None`` = normal mode.
_DIRECTIVE_CAPTURE: ContextVar[list[tuple[str, dict[str, Any]]] | None] = ContextVar(
    "kirocrew_directive_capture", default=None
)


def capture_directive(kind: str, args: dict[str, Any]) -> bool:
    """Record ``(kind, args)`` into the active capture slot; True if captured."""
    sink = _DIRECTIVE_CAPTURE.get()
    if sink is None:
        return False
    sink.append((kind, dict(args)))
    return True


def directive_capture_active() -> bool:
    """True while this call is a GATEWAY-SIDE replay under :func:`derive_directive`.

    A directive tool's handler runs TWICE for one arming call: once in the MCP
    server, where its return text answers the model, and once here, where
    :func:`derive_directive` re-runs it only to intercept the directive it
    publishes and DISCARDS the returned text (see :func:`_call_tool_body`).

    The two runs differ in one way that matters for I/O: this one executes on the
    gateway's OWN event loop, called synchronously from the aiohttp handler, so a
    blocking loopback request back to this same gateway cannot be serviced while
    it waits -- every session stalls until that request times out.

    A handler that reads gateway state only to shape its REFUSAL TEXT must
    therefore skip the read here: the text is discarded, and the turn boundary
    re-decides authoritatively either way.
    """
    return _DIRECTIVE_CAPTURE.get() is not None


def derive_directive(
    tool: str, raw_args: dict[str, Any], session_key: str
) -> tuple[str, dict[str, Any]] | None:
    """Re-run directive tool *tool* on *raw_args* AS *session_key* and return the
    ``(kind, args)`` it would have published, or None if it published nothing
    (refused, not a directive tool, or raised).

    Runs in the GATEWAY process. *session_key* is the caller's ``X-Session-Key``
    as the gateway verified it -- kernel-attested on the unix socket, bearer-token
    ``internal_auth`` on TCP -- and is installed as the call's :class:`CallerContext` for the
    duration -- source 0 of ``_resolve_session_key_strict`` -- so a handler that
    refuses without a strict identity (``monitor_watch``, ``monitor_stop``,
    ``monitor_update``) sees the same session the stub would have and derives
    rather than short-circuiting. That is the identity gatewayd injects for a
    pooled backend, arrived at by the same verification. Every write the handler
    would perform is the POST, and capture mode intercepts exactly that.

    The replay goes through :func:`_call_tool_body`, which in capture mode runs
    validation and the handler DIRECTLY rather than through
    ``call_tool_with_logging``: the stub already wrote the invocation's SEL audit
    row when the model's call ran, and a second "completed" row for the same call
    -- attributed to the same session, from the gateway process -- would double
    every directive in the audit trail. Derivation is a read of what the call
    means, not a second invocation, and leaves no record of its own.
    """
    if tool not in DIRECTIVE_TOOLS or not isinstance(raw_args, dict) or not session_key:
        return None
    sink: list[tuple[str, dict[str, Any]]] = []
    token = _DIRECTIVE_CAPTURE.set(sink)
    prior = current_caller()
    set_current_caller(CallerContext(session_key=session_key, from_gateway=True))
    try:
        # Deep copy for the same reason _call_tool snapshots deeply: the handler
        # may mutate nested objects, and the caller digests *raw_args* after.
        _call_tool(tool, copy.deepcopy(raw_args))
    except Exception:
        return None
    finally:
        set_current_caller(prior)
        _DIRECTIVE_CAPTURE.reset(token)
    return sink[0] if len(sink) == 1 else None


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    """Run one tool call to completion and return its text.

    The entry point every direct caller and test uses. A handler that returns
    a :class:`DeferredTool` (``wait``, ``spawn_sub_agents``) is driven to
    completion here, so from this side a deferred tool is a blocking one. The
    stdio server uses :func:`_call_tool_deferrable` instead, which hands the
    deferred object to the dispatch loop's timer rather than sleeping on it.
    """
    result = _call_tool_deferrable(name, raw_args)
    return drive_deferred(result, clock=time) if isinstance(result, DeferredTool) else result


def _call_tool_deferrable(name: str, raw_args: dict[str, Any]) -> "str | DeferredTool":
    # Call-in-flight record for control._emit_directive: the tool name and the
    # RAW arguments (pre-validation), which is what the gateway is told so it can
    # re-derive the directive itself. Cleared on exit so a direct handler call in
    # the same thread later (a test, a nested helper) never reports a stale call.
    _t_name = _CURRENT_CALL_NAME.set(name)
    # DEEP copy: validation mutates nested argument objects in place
    # (suggest_followup pops an empty ``branch`` off each item), and the
    # gateway must digest what the model SENT -- the same bytes the consumer
    # digests off the tool_call frame -- not what validation left behind.
    _t_args = _CURRENT_CALL_RAW_ARGS.set(
        copy.deepcopy(raw_args) if isinstance(raw_args, dict) else {}
    )
    try:
        return _call_tool_body(name, raw_args)
    finally:
        _CURRENT_CALL_NAME.reset(_t_name)
        _CURRENT_CALL_RAW_ARGS.reset(_t_args)


def _call_tool_body(name: str, raw_args: dict[str, Any]) -> "str | DeferredTool":
    if _DIRECTIVE_CAPTURE.get() is not None:
        # Gateway-side derivation (derive_directive): the handler's only write is
        # the directive it publishes, which capture_directive intercepts, and the
        # returned text is discarded. No SEL row -- the stub logged the real
        # invocation -- and no refusal tagging, since nothing reads the result.
        # A validation error is a refusal here too: the handler never ran, the
        # sink stays empty, and derive_directive answers None.
        return _call_tool_inner(name, _validate_args(name, raw_args))
    # Tag a directive tool's marker-less result as a refusal at the OUTERMOST
    # return, which is the only point that sees every way such a tool can
    # decline — argument validation runs inside the wrapper below, ahead of the
    # handler, so a schema rejection never reaches code that could tag itself.
    # Without the tag the consumer reads a decline as a LOST directive
    # marker and fires a WARNING meant for a transport regression.
    #
    # Clear the vouch FIRST: the gate honours a marker only if this dispatch
    # produced it, so a directive emitted by the PREVIOUS call must not be able
    # to authorize marker-shaped bytes in this one.
    clear_vouch()
    result = call_tool_with_logging(
        name,
        raw_args,
        _validate_args,
        _call_tool_inner,
        # Real caller identity when resolvable (per-call caller context in
        # pooled backends, env/PID otherwise) — a hardcoded "mcp_core" lost
        # attribution for every standard tool audit in shared backends.
        session_key=_resolve_session_key() or "mcp_core",
        downstream_service="kirocrew-core",
    )
    if isinstance(result, DeferredTool):
        # Not a result yet: the loop answers the call when a step settles it,
        # and the tag below is applied to that text then, as it is here.
        return map_deferred(result, lambda text: refuse_if_markerless(name, text))
    return refuse_if_markerless(name, result)


def _format_anchor(anchor: dict) -> str:
    """Format an anchor quote for the artifact_get_comments output.

    Short quotes (≤300 chars) are shown in full. Longer quotes are bookended
    with the first and last 100 chars plus an explicit TRUNCATED marker
    (never ambiguous with literal user text). Offsets are always included
    when available so the agent can locate the range in the document.
    """
    quote = anchor.get("quote", "")
    start = anchor.get("start_offset")
    end = anchor.get("end_offset")
    offset_info = ""
    if start is not None and end is not None:
        offset_info = f", chars {start}:{end}"
    if len(quote) <= 300:
        return f' [on: "{quote}"{offset_info}]'
    head = quote[:100]
    tail = quote[-100:]
    omitted = len(quote) - 200
    return f' [on: "{head}" [TRUNCATED: {omitted} chars omitted' f'{offset_info}] "{tail}"]'


# ── Issue Radar crew ledger helpers ──
#
# Two allowlisted app routes, both FULL paths in
# ``dashboard.server._MIXED_INTERNAL_API_PATHS`` (see the comment there for why
# the ``/api/apps/issue-radar`` prefix must never be admitted).
#
# That listing is necessary but not sufficient: it is matched
# ``path == p or path.startswith(p + "/")`` and carries no method, so the
# ``/crew`` entry also reaches ``/crew/pause`` and
# ``PUT``/``DELETE /crew``. The app closes that itself —
# ``crew_routes._AGENT_REACHABLE`` refuses an internal-secret caller on every
# crew route except the exact two below.
_CREW_READ_PATH = "/api/apps/issue-radar/crew"
_CREW_WORK_PATH = "/api/apps/issue-radar/crew/work"


def _call_tool_inner(name: str, args: dict[str, Any]) -> "str | DeferredTool":
    """Run one tool call and return its text result.

    Handlers live beside their descriptors in :mod:`kiro_crew.mcp_tools`; this
    stays the entry point every caller and test uses. Handlers reach back for
    this module's plumbing (``_post``, the identity resolvers, the governance
    vets) as attributes of ``mcp_core``, so a test that rebinds one still
    intercepts.

    Channel-agent containment sits here rather than in each handler because this
    is the one place every kirocrew-core tool call passes through: a per-handler
    check is a check the next dispatch verb's author has to remember, and the
    boundary this holds is exactly the kind that fails by omission. The
    interactive guard in ``channel.py`` rejects the same verbs at the
    permission-request event, but an auto-approved call emits no such event --
    under global YOLO or channel trust the request is approved with no human in
    the loop -- so the boundary has to hold at MCP dispatch too.
    """
    chan_err = _deny_channel_agent_dispatch(name, args)
    if chan_err:
        return chan_err
    # Second fence behind the unmount, for a caller that reaches the server
    # anyway; only these few names pay the choice-record read.
    from kiro_crew.session_environment import LOCAL_ONLY_CORE_TOOLS, refuse_local_only_tool

    if name in LOCAL_ONLY_CORE_TOOLS:
        remote_err = refuse_local_only_tool(_resolve_session_key(), CORE_MCP_SERVER, name)
        if remote_err:
            return remote_err
    return dispatch(name, args)


#: Whether this server advertises ``kirocrew.caller-identity`` — i.e. whether it
#: consumes the per-call caller block gatewayd injects (see
#: :func:`_resolve_session_key`, whose source 0 is that block) instead of reading
#: identity from its own process. True here because it does.
#:
#: A module-level constant rather than a bare argument below so the value is
#: readable without executing ``run_mcp_core_server``, and so a test can assert it
#: against the argument actually handed to the shim.
#:
#: The shareability assessment does NOT read it: that classification runs on every
#: render and must answer without spawning the server, so importing this module
#: there would execute package code from an editable checkout on a path the
#: sandbox never confines. It answers from ``_MANAGED_SERVERS_CALLER_AWARE`` in
#: ``mcp_discovery`` instead, and ``test/test_mcp_managed_caller_identity.py``
#: keeps the two from drifting by comparing them in the test process.
ADVERTISE_CALLER_IDENTITY = True


def run_mcp_core_server() -> None:
    """Run MCP stdio server for core agent tools."""
    # Session search asks the gateway for Chinese dictionary words instead of
    # loading the dictionary in every core process.
    from kiro_crew.history_search import register_cjk_segmenter
    from kiro_crew.mcp_tools.sessions import gateway_cjk_words

    register_cjk_segmenter(gateway_cjk_words)
    run_mcp_stdio_loop(
        "kirocrew-core",
        "1.0.0",
        _list_tools,
        # Deferrable: a waiting tool hands the loop a DeferredTool instead of
        # holding the worker (see mcp_shared.DeferredTool).
        _call_tool_deferrable,
        # Pooled-operation opt-in: kirocrew-core consumes the per-call
        # ``kirocrew.caller`` identity (see _resolve_session_key*), so it is
        # safe to share one backend across sessions. All four managed servers
        # advertise, and what advertising buys is the injected block, not
        # co-tenancy: a backend that does NOT advertise is
        # pooled all the same (nothing declines to pool one; see
        # ``rewriter.UNPOOLABLE_SERVERS``) and simply never receives an identity.
        advertise_caller_identity=ADVERTISE_CALLER_IDENTITY,
        # The only server that parks DeferredTools, so the only one whose
        # gateway needs the in-flight report (see run_mcp_stdio_loop).
        reports_inflight=True,
    )


# The owner modules, lowest layer first: the first one that binds a name answers a read
# of it. ``kiro_crew.mcp_core.<name>`` stays the import path and the patch surface for
# every name they define: the tool handlers read them as attributes of this module, and a
# patch through it reaches every owner that binds the name (see
# :mod:`kiro_crew._facade_mirror`).
_OWNERS = (
    "kiro_crew.mcp_core_parts.identity",
    "kiro_crew.mcp_core_parts.gateway",
    "kiro_crew.mcp_core_parts.governance",
    "kiro_crew.mcp_core_parts.crew",
    "kiro_crew.mcp_core_parts.history",
    "kiro_crew.mcp_core_parts.radar_crew",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.mcp_core_parts.crew import (  # noqa: F401
        _crew_memory_unavailable_reason,
        _do_route_crew,
        _do_select_crew,
        _key_with_label,
        _with_display_name,
    )
    from kiro_crew.mcp_core_parts.gateway import (  # noqa: F401
        _API,
        _API_PORT,
        _API_UNIX_SOCKET,
        _STABLE_PORT_SOURCES,
        _api_base,
        _api_port,
        _api_unix_socket,
        _invalidate_api_base,
        _resolve_api_port,
        _socket_path_for,
    )
    from kiro_crew.mcp_core_parts.governance import (  # noqa: F401
        _audit_governance_deny,
        _deny_channel_agent_messaging,
        _governance_app,
        _vet_browse_governance,
        _vet_channel_governance,
        _vet_memory_writes_governance,
        _vet_messaging_governance,
    )
    from kiro_crew.mcp_core_parts.history import (  # noqa: F401
        _HISTORY_SNIPPET_ROLES,
        _SEARCH_HISTORY_SCAN,
        _SNIPPET_MAX_LEN,
        _SNIPPET_RADIUS,
        _caller_workspace,
        _casefold_match_span,
        _extract_history_snippet,
        _history_is_incognito,
        _parse_iso_date_epoch,
        _redact_history_output,
        _snippet_contents,
        _ws_bucket,
    )
    from kiro_crew.mcp_core_parts.identity import (  # noqa: F401
        REFLEXIVE_TOOL_MODULES,
        _classify_slack_identity,
        _current_session_thread_ts,
        _resolve_session_key_strict,
        _session_key_from_token,
        require_strict_session_key,
        strict_identity_diagnosis,
    )
    from kiro_crew.mcp_core_parts.radar_crew import (  # noqa: F401
        _CREW_MAX_EVENTS,
        _crew_identity,
        _crew_ledger_view,
        _crew_machine_markers,
        _crew_public_text,
        _redact_json_strings,
    )
