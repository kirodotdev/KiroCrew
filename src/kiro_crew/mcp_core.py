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
    learn_update    — change one lesson's always/on_topic tier
    task_run        — start the autonomous task runner
"""

from __future__ import annotations

import contextlib
import copy
import importlib as _importlib
import json
import re as _re
import sys as _sys
import threading
import time
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
from kiro_crew.config.sections_defaults import (  # noqa: F401
    DEFAULT_SEARCH_SCAN_WINDOW as SEARCH_SCAN_WINDOW,
)
from kiro_crew.context_management import summarize_result
from kiro_crew.dashboard.origin import dashboard_socket_path  # noqa: F401
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
from kiro_crew.mcp_core_parts.gateway import (  # noqa: F401
    _REFUSED_RETRY_BACKOFFS,
    _api_urlopen,
    _caller_header,
    _delete,
    _get,
    _http_error_body,
    _internal_secret,
    _patch,
    _post,
    _put,
    _refused_message,
    _replay_target,
    _resolve_api_target,
    _reverify_refused_target,
    _secret_for_base,
    _send,
    _session_key_header_error,
    _session_token_header,
    _transport_failure,
)
from kiro_crew.mcp_core_parts.governance import _deny_channel_agent_dispatch
from kiro_crew.mcp_core_parts.identity import _resolve_session_key
from kiro_crew.mcp_core_tool_list import SERVER, _list_tool_names, _list_tools  # noqa: F401
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
from kiro_crew.mcp_tool_titles import with_titles  # noqa: F401
from kiro_crew.mcp_tools import build_tool_list, build_tool_names, dispatch  # noqa: F401
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
    if schema is None:
        # A tool an installed app declares is validated against its declared schema,
        # through the same field engine; a core name never resolves to an app's.
        from kiro_crew.mcp_tools import app_tools

        schema = app_tools.tool_schema(name)
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
    chan_err = _deny_channel_agent_dispatch(name)
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
ADVERTISE_CALLER_IDENTITY = SERVER.advertise_caller_identity


def run_mcp_core_server() -> None:
    """Run MCP stdio server for core agent tools."""
    # Session search asks the gateway for Chinese dictionary words instead of
    # loading the dictionary in every core process.
    from kiro_crew.history_search import register_cjk_segmenter
    from kiro_crew.mcp_tools.sessions import gateway_cjk_words

    register_cjk_segmenter(gateway_cjk_words)
    # Before the first request (a no-op when the server already answered its first
    # two before importing this module): see ``mcp_core_tool_list._redeem_pickup``.
    if SERVER.before_first_request is not None:
        SERVER.before_first_request()
    run_mcp_stdio_loop(
        SERVER.name,
        SERVER.version,
        _list_tools,
        # Deferrable: a waiting tool hands the loop a DeferredTool instead of
        # holding the worker (see mcp_shared.DeferredTool).
        _call_tool_deferrable,
        # What the server advertises, and why, is on its declaration.
        **SERVER.loop_options(),
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
        _SNIPPET_MAX_LEN,
        _SNIPPET_RADIUS,
        _caller_workspace,
        _casefold_match_span,
        _extract_history_snippet,
        _history_is_incognito,
        _parse_iso_date_epoch,
        _redact_history_output,
        _search_history_scan,
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
