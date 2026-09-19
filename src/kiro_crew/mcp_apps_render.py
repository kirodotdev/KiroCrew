"""Dashboard-side interception of MCP Apps render markers.

The gateway daemon (``src/kiro_crew/mcp_gateway/``) writes a UI payload to a
spool file at ``$KIROCREW_HOME/mcp-apps/<uuid4hex>.json`` and injects an opaque
marker ``[kirocrew-mcp-app:<uuid4hex>]`` into the tool result *text* that flows
back to kiro-cli. That text reaches the dashboard backend as an
``EVENT_TOOL_RESULT`` (``AcpEvent.tool_output``) inside ``_run_chat``.

This module is the tiny, self-contained seam the chat runner calls on every
tool result: it detects the marker, loads the spooled payload (strictly by a
validated 32-hex id — no attacker-controlled path ever touches the filesystem),
pushes an ``mcp_app_render`` websocket event to the slot, and returns the
transcript text with the marker stripped (a cosmetic rewrite, exactly like the
existing redaction passes rewrite displayed text).

Security posture:
  * The id is validated ``^[0-9a-f]{32}$`` and the spool path is built ONLY from
    that validated id joined to the spool dir — path traversal is impossible by
    construction (``../`` etc. never match the regex and are never used to form
    a path).
  * The LLM never reads the payload file; only this deterministic code does.
  * Missing / corrupt / oversized spool files are tolerated (return ``None``) so
    a bad payload can never crash a turn.
  * This side is flag-independent: if a marker appears we handle it. The
    ``KIROCREW_MCP_APPS`` opt-in gate lives on the gateway (producer) side.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, NamedTuple

from kiro_crew import security
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)


class ToolResultRender(NamedTuple):
    """What this seam did with one tool result.

    ``text`` is the transcript text with the marker stripped, which is what
    every caller needs.

    ``app_claimed`` is true when the spool record loaded, passed its
    session-binding check, and had its SINGLE-USE render claim consumed for
    this tool call. That is the fact a transcript needs, and the claim being
    spent is exactly why the app can never appear for this call again.

    It deliberately does NOT assert that a browser received the frame.
    ``broadcast_ws_owners`` drops the payload when no owner socket is attached,
    and an unattended run has no viewer at all -- which is precisely the case
    where the transcript must still be able to say this call produced an app.
    Gating on a delivered count would record nothing there, so a reader opening
    that session later would be told nothing, which is the gap this exists to
    close. Nor does it wait on the dispatch succeeding: the claim is spent
    before the send, so a send that raises leaves an app that can never be
    shown, which the reader still needs to know about.

    It is also the one fact a caller cannot work out for itself. A marker's
    PRESENCE does not mean a record was claimed: a replayed marker is inert, a
    record can be expired or unreadable, and a cross-session marker is refused,
    and every one of those returns stripped text that looks identical.

    A return value rather than an optional callback, because mypy then makes
    every caller confront the flag. A hook defaulting to ``None`` can be
    dropped by the next call site, and the durable flag would quietly stop
    being written with nothing failing.
    """

    text: str
    app_claimed: bool


def _redact_leaves(obj: Any) -> Any:
    """Recursively apply credential + exfiltration-URL redaction to every
    string leaf of *obj*, returning a redacted copy.

    App payloads (``tool_input`` / ``structured_content`` / ``result_content``)
    are delivered into a server-authored iframe that can open network
    connections to its declared CSP origins; a credential or exfil URL that
    leaks through a tool result must be scrubbed before it crosses that trust
    boundary (same discipline the transcript/WS redaction passes apply). Bounded
    by the spool size cap already enforced on load.
    """
    if isinstance(obj, str):
        return security.redact(obj)
    if isinstance(obj, list):
        return [_redact_leaves(x) for x in obj]
    if isinstance(obj, dict):
        # Redact string KEYS too — a credential can appear as a dict key, not
        # just a value.
        return {
            (security.redact(k) if isinstance(k, str) else k): _redact_leaves(v)
            for k, v in obj.items()
        }
    return obj


# Opaque render marker: literal tag carrying a uuid4 hex (32 lowercase hex).
# Anchored to exactly 32 hex chars so a stray "[kirocrew-mcp-app:...]" with the
# wrong shape is simply ignored rather than treated as a spool id.
MARKER_RE = re.compile(r"\[kirocrew-mcp-app:([0-9a-f]{32})\]")

# A spool id is a bare uuid4 hex — validated independently of the marker so
# load_spool cannot be tricked into resolving a non-id string into a path.
_SPOOL_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")

# On-disk spool record schema version. Written by the gateway
# (``mcp_gateway/apps.py`` imports this constant — single source of truth) and
# ENFORCED by every reader: a record whose ``schema`` differs is rejected
# (fail-closed) so a future breaking shape change can never be silently
# mis-read by a stale reader.
SPOOL_SCHEMA_VERSION = 1

# Refuse to parse absurdly large spool files (defense against a runaway/hostile
# payload filling memory). The real HTML lives inside; 8 MiB is far past any
# realistic MCP App bundle while still bounding the read.
_MAX_SPOOL_BYTES = 8 * 1024 * 1024

#: Public alias so the writer (``mcp_gateway/apps.py``) enforces the SAME cap
#: it reads with — a record the reader would refuse is never written.
MAX_SPOOL_BYTES = _MAX_SPOOL_BYTES

#: Capability TTL enforced on read. Matches ``sweep_spool``'s 24h default: a
#: record (and its callback_secret) older than this is refused and reaped even
#: if no sweep has run since.
SPOOL_TTL_SECS = 24 * 3600


def _spool_dir() -> Path:
    """Directory holding spool files. ``KIROCREW_MCP_APPS_SPOOL`` overrides the
    default ``$KIROCREW_HOME/mcp-apps``."""
    override = os.environ.get("KIROCREW_MCP_APPS_SPOOL")
    if override:
        return Path(override).expanduser()
    return config_dir() / "mcp-apps"


def find_marker(text: str | None) -> str | None:
    """Return the spool id embedded in *text*, or ``None`` if no marker is present."""
    if not text:
        return None
    m = MARKER_RE.search(text)
    return m.group(1) if m else None


def strip_marker(text: str | None) -> str:
    """Return *text* with every render marker removed (cosmetic — the marker is
    an internal control token the user should never see in the transcript)."""
    if not text:
        return text or ""
    return MARKER_RE.sub("", text)


def load_spool(spool_id: str) -> dict[str, Any] | None:
    """Load and parse the spool file for *spool_id*.

    Returns the parsed dict, or ``None`` when the id is malformed, the file is
    missing, unreadable, too large, or not valid JSON object. The path is
    constructed STRICTLY from the validated id (``<spool_dir>/<id>.json``); the
    caller-supplied value is never used as a path fragment, so traversal such as
    ``"../../etc/passwd"`` cannot resolve (it fails the id regex first, and even
    if it somehow reached the join it would carry no ``.json`` id shape).
    """
    if not isinstance(spool_id, str) or not _SPOOL_ID_RE.match(spool_id):
        return None
    path = _spool_dir() / f"{spool_id}.json"
    try:
        # Resolve and re-verify containment as belt-and-suspenders: the resolved
        # file's parent must be the spool dir. (Given the id regex this always
        # holds; the check documents and enforces the invariant.)
        base = _spool_dir().resolve()
        resolved = path.resolve()
        if resolved.parent != base:
            logger.warning("mcp-apps spool path escaped spool dir; refusing")
            return None
        if not resolved.is_file():
            return None
        st = resolved.stat()
        if st.st_size > _MAX_SPOOL_BYTES:
            logger.warning("mcp-apps spool file %s exceeds size cap; ignoring", spool_id)
            return None
        # Enforce the documented capability TTL on READ, not only via the
        # startup/opportunistic sweep: a host that renders an app but neither
        # restarts its gateway nor writes another record would otherwise keep a
        # stale record — and its callback_secret — valid indefinitely. Reject
        # (and best-effort reap the record + its .rendered sidecar) past the
        # TTL, matching sweep_spool's 24h window.
        if time.time() - st.st_mtime > SPOOL_TTL_SECS:
            logger.info("mcp-apps spool %s is past its TTL; refusing", spool_id)
            for stale in (resolved, resolved.with_suffix(".rendered")):
                try:
                    stale.unlink()
                except OSError:
                    pass
            return None
        with resolved.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        # Missing, unreadable, or corrupt JSON — tolerate silently (debug only).
        logger.debug("mcp-apps spool load failed for %s", spool_id, exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") != SPOOL_SCHEMA_VERSION:
        # Fail closed on any other version (or a missing field): the version
        # marker exists precisely so a stale reader rejects a record it does
        # not understand instead of silently mis-reading it.
        logger.warning(
            "mcp-apps spool %s has unsupported schema %r; refusing",
            spool_id,
            data.get("schema"),
        )
        return None
    return data


def _load_bound(spool_id: str, producing_session_key: str) -> dict[str, Any] | None:
    """Load the spool record and validate its session binding. Takes NO claim.

    ``producing_session_key`` is the CANONICAL producing-session key (e.g.
    ``"dashboard:<slot>"``) that the gateway recorded on the spool — NOT the
    bare frontend slot key used for WS routing. The two differ (the dashboard
    sets ``KIROCREW_SESSION_KEY`` to the prefixed form while ``slot.key`` is
    bare), so the binding check MUST compare against the canonical form or every
    real render is refused as a mismatch.

    ORDER MATTERS: the binding check runs BEFORE the claim (``_take_claim``,
    which a caller reaches only after this returns). A marker echoed into the
    WRONG session must not consume the record's single render, because a replay
    racing ahead of the legitimate slot would burn the claim and permanently
    suppress the real render (a cheap denial). Only a caller that passes the
    binding check may take the claim.

    Runs blocking filesystem work, so call via ``asyncio.to_thread``.
    """
    data = load_spool(spool_id)
    if data is None:
        return None
    # Slot binding: the record names the canonical session that produced it. A
    # marker echoed into a DIFFERENT session (transcript replay, cross-channel
    # paste) must not render the app under mismatched attribution. Empty
    # session_key (producer couldn't attribute) is allowed with a log line;
    # the claim below still gates it.
    record_session = data.get("session_key") or ""
    if record_session and record_session != producing_session_key:
        logger.warning(
            "mcp-apps marker %s bound to session %r arrived in %r; refusing render",
            spool_id,
            record_session,
            producing_session_key,
        )
        return None
    if not record_session:
        logger.info(
            "mcp-apps marker %s has no session binding; rendering in %r",
            spool_id,
            producing_session_key,
        )
    return data


def _take_claim(spool_id: str) -> bool:
    """Atomically take the record's ONE render for this caller.

    A record renders at most ONCE: markers travel in tool-result text, which
    the LLM (and any server) can echo into later turns and which persists in
    transcripts, so without a consume gate a replayed marker would re-render
    the app wherever the text lands. The claim is an ``O_CREAT|O_EXCL`` sidecar
    (``<id>.rendered``) next to the record: atomic on POSIX, so exactly one
    caller wins even under concurrent replays. The record itself stays on disk,
    because the app-call capability path deliberately remains valid for the
    rendered app's lifetime (until the TTL sweep reaps both files).

    Separate from ``_load_bound`` so a caller can finish every CANCELLABLE step
    before taking it. Claiming is irreversible and makes every later replay
    inert, so a cancellation landing after the claim but before the caller
    records the app loses that app with nothing left to say it existed. Taking
    the claim LAST means a cancellation instead leaves the record unclaimed and
    a later replay can still render it.

    OFFLOAD this, never call it on the event loop. ``os.close()`` on a file
    descriptor is named in the ``no-blocking-call-on-event-loop`` rule
    (``AUTOSDE.yaml``), which carries ``blocking: true``: the gateway runs every
    task on one loop, so a syscall that stalls there -- a spool on network or
    FUSE storage is enough -- freezes the user's turn AND the liveness
    heartbeat until the watchdog kills the process and the supervisor respawns
    into the same condition. That the syscalls here are cheap on a local path is
    not a judgement this code gets to make; the rule's own closing line is "when
    in doubt, offload", and a frozen loop outranks the cost below.

    The cost the offload reintroduces, and how the caller pays it: an executor
    thread runs to completion whatever happens to the awaiting coroutine, so a
    cancellation delivered mid-``os.open`` creates the sidecar while the caller
    raises and records nothing. ``handle_tool_result`` closes that by shielding
    the call AND draining it through any further cancellation
    (``_drain_shielded``), because a shield protects the inner future rather than
    the awaiting coroutine; it then calls ``_release_claim`` when it cannot
    record the app after all.

    ``_load_bound`` is offloaded for the ordinary reason instead: it reads and
    parses a record up to ``MAX_SPOOL_BYTES``.
    """
    sentinel = _spool_dir() / f"{spool_id}.rendered"
    try:
        fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        logger.info("mcp-apps marker %s already rendered; replay is inert", spool_id)
        return False
    except OSError:
        logger.warning("mcp-apps render claim failed for %s", spool_id, exc_info=True)
        return False
    return True


def _release_claim(spool_id: str) -> None:
    """Give a claim back, for a caller torn down before it could record the app.

    Only ``handle_tool_result``'s cancellation path calls this, and only for a
    claim IT won. A claim nobody can record is worse than no claim: it makes
    every later replay inert, so the app is gone with nothing left to say it
    existed. Releasing it puts the record back where a later replay renders it.

    Offloaded for the same reason as ``_take_claim`` -- ``unlink`` is a metadata
    syscall on the same possibly-stalled filesystem.

    One residue, bounded to the interval between the claim and this unlink: a
    concurrent replay arriving inside it sees the sidecar and is told the record
    is already rendered, so that replay shows the absence notice for an app that
    becomes renderable again a moment later. The window is two metadata syscalls
    wide and the next replay renders for real, which is why this is the accepted
    side of the trade against a claim that can never be given back.
    """
    try:
        (_spool_dir() / f"{spool_id}.rendered").unlink()
    except OSError:
        logger.warning("mcp-apps claim release failed for %s", spool_id, exc_info=True)


async def _drain_shielded(aw: Any) -> Any:
    """Await ``aw`` to completion even if THIS task is cancelled repeatedly.

    ``asyncio.shield`` protects the inner future, not the awaiting coroutine: a
    second ``.cancel()`` on this task raises ``CancelledError`` out of the
    ``await`` itself, so any cleanup that followed it is skipped. A plain shield
    is therefore not enough to make the claim's outcome reachable -- and a
    double cancellation is ordinary operation here, not a contrived one, since a
    turn deadline and a slot deletion can both fire on the same task.

    The loop cannot spin: each iteration either suspends on a pending inner
    future or, once that future is done, returns without a suspension point --
    so an iteration is only ever driven by a further ``.cancel()``, and the
    count of those is bounded by the callers that issue them.

    The caller re-raises the cancellation itself; absorbing it here would
    silently un-cancel the turn.
    """
    inner = asyncio.ensure_future(aw)
    while True:
        try:
            return await asyncio.shield(inner)
        except asyncio.CancelledError:
            if inner.done():
                # Cancellation and completion raced. The worker's outcome is
                # already here, so read it rather than re-entering the await.
                return inner.result()


async def handle_tool_result(
    state: Any,
    *,
    slot_key: str,
    tool_call_id: str,
    text: str,
    producing_session_key: str | None = None,
) -> ToolResultRender:
    """Interception seam for the ``EVENT_TOOL_RESULT`` handler.

    ``slot_key`` is the bare frontend slot key used for WS routing (every other
    chat event routes on it). ``producing_session_key`` is the CANONICAL session
    key the gateway recorded on the spool (``"dashboard:<slot>"``); it is used
    ONLY for the binding check and defaults to ``slot_key`` for callers that
    already pass a canonical key. Keeping the two separate fixes the silent
    render-suppression bug where the bare slot key never matched the prefixed
    recorded session (so no app ever rendered on a real dashboard).

    If *text* carries a render marker, load the spooled payload and broadcast an
    ``mcp_app_render`` websocket event scoped to ``slot_key``, then return *text*
    with the marker stripped. When there is no marker — the overwhelming common
    case — return *text* unchanged after a single cheap check.

    Returns a ``ToolResultRender``: the transcript text, and whether this call
    consumed the record's one render. See that type for what the second field
    does and does not assert, and why a caller cannot derive it.

    The spool read is offloaded via ``asyncio.to_thread``. Never raises for a
    failure of its own: every error path logs and returns the stripped text. It
    DOES propagate ``asyncio.CancelledError``, which is a ``BaseException`` and
    is not a failure of this seam but the caller's turn being torn down. The one
    thing that propagation must not do is leave a claimed record behind, so the
    claim is shielded and given back on that path.
    """
    spool_id = find_marker(text)
    if not spool_id:
        return ToolResultRender(text, False)
    binding_key = producing_session_key if producing_session_key is not None else slot_key
    # Set on the CLAIM, which is the irreversible half: past that point the
    # record's one render belongs to this call and no later frame can use it.
    claimed = False
    try:
        data = await asyncio.to_thread(_load_bound, spool_id, binding_key)
        if data is not None:
            # OWNER-scoped delivery: the frame carries the ``callback_secret``
            # — the capability that authorizes app→gateway callbacks —
            # so a non-owner/guest WebSocket must never receive it. Falls back
            # to broadcast_ws only if the state predates the owner channel
            # (tests with minimal fakes).
            send = getattr(state, "broadcast_ws_owners", None) or state.broadcast_ws
            # Offload redaction: the passes recurse over payloads that can be
            # multi-MB, and this seam runs on the dashboard event loop.
            #
            # BEFORE the claim, deliberately. This await is cancellable, and
            # `CancelledError` is a BaseException that the `except Exception`
            # below does not catch, so a turn cancelled here returns nothing to
            # the caller at all. With the claim already taken, that would leave
            # an app no replay can ever render and no row recording that it
            # existed, which is the exact silence this seam's caller exists to
            # remove. Redacting first costs one wasted pass in the rare case a
            # concurrent replay wins the claim, and buys a cancellation window
            # in which the record is still claimable.
            red_structured, red_input, red_result = await asyncio.to_thread(
                lambda: (
                    _redact_leaves(data.get("structured_content")),
                    _redact_leaves(data.get("tool_input")),
                    _redact_leaves(data.get("result_content")),
                )
            )
            # OFFLOADED, and DRAINED so the offload cannot lose the app.
            # `os.close` on the loop is forbidden by the `blocking: true`
            # `no-blocking-call-on-event-loop` rule; see `_take_claim`. The bare
            # offload would reopen the loss window one step narrower -- the
            # executor thread creating the sidecar after the awaiting coroutine
            # has already raised -- so the claim is shielded to keep the worker's
            # outcome knowable, and the cancellation path drains that outcome
            # through any FURTHER cancellation before giving the claim back.
            claim = asyncio.ensure_future(asyncio.to_thread(_take_claim, spool_id))
            try:
                took_claim = await asyncio.shield(claim)
            except asyncio.CancelledError:
                # A shield keeps the WORKER alive; it does not keep THIS
                # coroutine from being cancelled again at the awaits below. A
                # second cancellation landing here would leave the sidecar taken
                # and the release skipped -- an app no replay can render, which
                # is the exact silence this seam exists to remove. So both awaits
                # DRAIN through repeated cancellation, and the original
                # cancellation propagates afterwards.
                try:
                    if await _drain_shielded(claim):
                        await _drain_shielded(asyncio.to_thread(_release_claim, spool_id))
                except Exception:
                    logger.warning("mcp-apps claim release skipped for %s", spool_id, exc_info=True)
                raise
            if not took_claim:
                return ToolResultRender(strip_marker(text), False)
            claimed = True
            send(
                "mcp_app_render",
                {
                    "session_key": slot_key,
                    "tool_call_id": tool_call_id,
                    "server": data.get("server", ""),
                    "tool": data.get("tool", ""),
                    "html": data.get("html", ""),
                    "csp": data.get("csp", ""),
                    "permissions": data.get("permissions", []),
                    "spool_id": spool_id,
                    # Callback capability — owner-WS ONLY. The iframe
                    # replays this on every callback; the model-visible marker
                    # (spool_id) authorizes nothing without it.
                    "callback_secret": data.get("callback_secret", ""),
                    # App-bound tool data — credential/exfil-URL redacted before
                    # it crosses into the server-authored iframe (the iframe can
                    # reach its declared CSP origins).
                    "structured_content": red_structured,
                    "tool_input": red_input,
                    "result_content": red_result,
                },
            )
        else:
            logger.info("mcp-apps marker %s had no loadable spool payload", spool_id)
    except Exception:
        logger.warning("mcp-apps render broadcast failed for %s", spool_id, exc_info=True)
    return ToolResultRender(strip_marker(text), claimed)
