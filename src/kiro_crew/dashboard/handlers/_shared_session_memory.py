"""A session's memory mode, and whether its caller may read memory at all.

The live mode of a slot, the mode a child session inherits, the mode recorded in a
persisted transcript's header, and the restricted-session test the memory and lesson
routes refuse on.

Served through :mod:`kiro_crew.dashboard.handlers._shared`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from kiro_crew.config.loader import config_dir
from kiro_crew.dashboard.state import VALID_MEMORY_MODES, DashboardState
from kiro_crew.history import TRANSCRIPT_HEADER_MAX_BYTES, memory_mode_from_header_line
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.messaging.privacy_mode import hydrate as _hydrate_conv_flags
from kiro_crew.messaging.privacy_mode import is_incognito as is_thread_incognito
from kiro_crew.messaging.privacy_mode import is_temporary as is_thread_temporary


def _read_session_key(request: "Any") -> str:
    """Read and normalize the ``X-Session-Key`` header for authz comparisons.

    Strips surrounding whitespace so the authorization gate matches the
    canonical stored key form and the routing endpoints (which already
    ``.strip()``). A trailing space / stray whitespace must not let a
    restricted or read-blocked session slip past the restricted-key set or the
    slot lookup (CWE-178/180 — inconsistent normalization in an auth context).
    """
    return request.headers.get("X-Session-Key", "").strip()


def inherited_session_memory_mode(state: DashboardState, key: str) -> str | None:
    """Read only restrictions captured by trusted child creation in this process."""
    from kiro_crew.messaging.privacy_mode import strictest

    modes = []
    admitted = getattr(getattr(state, "context_builder", None), "_session_memory_modes", None)
    if isinstance(admitted, dict) and key in admitted:
        modes.append(admitted[key])
    manager = getattr(state, "subagents", None)
    if manager is not None:
        for info in getattr(manager, "running", ()):
            if (info.conversation_key or f"subagent:{info.id}") == key:
                if not info._memory_mode_ready:
                    return "temporary"
                modes.append(info.memory_mode)
    if any(mode not in VALID_MEMORY_MODES for mode in modes):
        return "temporary"
    return (strictest(modes) or "persistent") if modes else None


def live_session_memory_mode(state: DashboardState, key: str) -> str | None:
    """Snapshot gateway-owned policy without filesystem I/O or namespace grants."""
    if key in ("", "dashboard:ui"):
        return "persistent"
    inherited = inherited_session_memory_mode(state, key)
    if inherited is not None:
        return inherited
    # A headless key must not borrow an unrelated dashboard slot's suffix.
    slot = (
        state._slots.get(key.removeprefix("dashboard:")) if key.startswith("dashboard:") else None
    )
    from kiro_crew.validation import SLACK_THREAD_TS_RE

    channel = is_channel_session_key(key) or bool(SLACK_THREAD_TS_RE.fullmatch(key))
    if channel:
        _hydrate_conv_flags(state.sessions, key)
        if is_thread_temporary(key):
            return "temporary"
        if is_thread_incognito(key):
            return "incognito"
    request = SimpleNamespace(headers={"X-Session-Key": key})
    if slot is not None or channel:
        if _blocks_reads_session(state, request):
            return "temporary"
        if _is_restricted_session(state, request):
            return "incognito"
        return "persistent"
    return None


def require_live_session_memory_mode(state: DashboardState, key: str) -> str:
    mode = live_session_memory_mode(state, key)
    if not isinstance(mode, str) or mode not in VALID_MEMORY_MODES:
        raise ValueError("The originating session's memory mode is unavailable")
    return mode


async def resolve_session_memory_mode(state: DashboardState, key: str) -> str:
    """Resolve admission policy, retaining live snapshots across off-loop work."""
    mode = live_session_memory_mode(state, key)
    if mode is not None:
        if mode not in VALID_MEMORY_MODES:
            raise ValueError("The originating session's memory mode is invalid")
        return mode
    from kiro_crew.execution_context import read_session_execution

    execution = await asyncio.to_thread(read_session_execution, key)
    if execution is not None:
        return execution.memory_mode
    if key.startswith("subagent:"):
        from kiro_crew.subagent_persistence import read_run_memory_mode

        return await asyncio.to_thread(read_run_memory_mode, key.removeprefix("subagent:"))
    if key.startswith(("wf:", "wf-pool:", "wf-unpooled:", "wf-worker:", "wf-author:")):
        from kiro_crew.workflow_memory import WorkflowMemoryError, read_binding

        try:
            row = await asyncio.to_thread(read_binding, key.split(":", 2)[1], required=True)
        except WorkflowMemoryError as exc:
            raise ValueError("The originating session's memory mode is unavailable") from exc
        mode = row.get("memory_mode") if row is not None else None
    else:
        from kiro_crew.subagent_persistence import read_session_memory_mode

        mode = await asyncio.to_thread(read_session_memory_mode, key)
        if mode is not None:
            return mode
        if key.startswith("taskrunner:"):
            raise ValueError("The task runtime's protected memory mode is unavailable")
        exists, mode = await asyncio.to_thread(_probe_persisted_session, key.split(":", 1)[-1])
        if not exists:
            mode = None
    if not isinstance(mode, str) or mode not in VALID_MEMORY_MODES:
        raise ValueError("The originating session's memory mode is unavailable")
    return mode


def _is_restricted_session(state: DashboardState, request: "Any") -> bool:
    """Check if request comes from an ephemeral (incognito) or temporary (guest) session.

    Reads X-Session-Key header (set by browser and MCP subprocesses).
    Returns True if the session should be blocked from memory operations.
    """
    return is_restricted_session_key(state, _read_session_key(request))


def is_restricted_session_key(state: DashboardState, sk: str) -> bool:
    """The same judgement as :func:`_is_restricted_session`, over a KEY.

    Split out so a caller holding a session key but no request reaches this
    predicate instead of re-deriving it. An in-process producer that delivers
    session-scoped state into a session's own turn is such a caller: it has the
    key off the record it is serving and no request to read a header from, and a
    second copy of a four-way restricted test is how one of the two drifts open.

    *sk* is expected ALREADY NORMALIZED (the request form strips it through
    ``_read_session_key``, for the inconsistent-normalization reason that function
    gives); a caller holding a stored key has one that is normalized by
    construction.
    """
    if not sk:
        return False
    if sk == "dashboard:ui":
        return False
    inherited = inherited_session_memory_mode(state, sk)
    if inherited is not None and inherited != "persistent":
        return True
    if sk in state._restricted_keys:
        return True
    slot_name = sk.split(":", 1)[-1] if ":" in sk else sk
    slot = state._slots.get(slot_name)
    if slot and slot.is_restricted:
        return True
    if is_channel_session_key(sk):
        # Restore the DURABLE flags before consulting the in-memory maps. The
        # privacy trackers are process-local and are only populated by
        # ``privacy_mode.hydrate`` on an INBOUND channel message, so a turn that
        # no inbound message drove — a cron with session="origin", a
        # webhook-resumed session, a monitor/autonudge re-injection, a subagent —
        # reaches this gate with empty maps after a gateway restart even though
        # the user's !incognito is on disk. Calling the canonical restore (rather
        # than reading the SessionMap directly) keeps one source of truth and
        # self-heals the process-local view. Idempotent and allocation-free for
        # unflagged keys.
        #
        # Namespace-agnostic on purpose. A ``startswith("slack:")`` test made this
        # branch structurally unreachable for every other channel, so a
        # ``telegram:{agent}:direct:{user}`` session the user marked incognito
        # could never enter it and the ~30 dashboard mutations gated on this
        # predicate stayed open for it.
        _hydrate_conv_flags(state.sessions, sk)
        if is_thread_temporary(sk) or is_thread_incognito(sk):
            return True
    # NOTE: deliberately no disk fallback for an absent slot. This predicate is
    # a SYNC helper with ~49 call sites reachable from async handlers, so reading
    # the persisted mode here would put blocking file I/O on the event loop
    # (AUTOSDE ``no-blocking-call-on-event-loop``). The archived-session recovery
    # is done off-loop instead, by the one caller that needs it —
    # ``api_lessons_create`` — via ``_probe_persisted_session``.
    return False


def _blocks_reads_session(state: DashboardState, request: "Any") -> bool:
    """Check if request comes from a temporary session that blocks memory reads."""
    sk = _read_session_key(request)
    if not sk or sk == "dashboard:ui":
        return False
    inherited = inherited_session_memory_mode(state, sk)
    if inherited is not None and inherited not in {"persistent", "incognito"}:
        return True
    slot_name = sk.split(":", 1)[-1] if ":" in sk else sk
    slot = state._slots.get(slot_name)
    if slot and slot.blocks_reads:
        return True
    if is_channel_session_key(sk):
        # Same durable-flag restore, and the same namespace-agnostic reach, as
        # _is_restricted_session: a temporary conversation whose flags this
        # process never hydrated must not serve reads, on any channel.
        _hydrate_conv_flags(state.sessions, sk)
        if is_thread_temporary(sk):
            return True
    # NOTE: deliberately no disk fallback for an absent slot. This predicate is
    # a SYNC helper with ~49 call sites reachable from async handlers, so reading
    # the persisted mode here would put blocking file I/O on the event loop
    # (AUTOSDE ``no-blocking-call-on-event-loop``). The archived-session recovery
    # is done off-loop instead, by the one caller that needs it —
    # ``api_lessons_create`` — via ``_probe_persisted_session``.
    return False


# Byte ceiling for the session-metadata head read, shared with the whole-install
# export so both read the same amount of a first line before judging it.
_METADATA_HEAD_MAX_BYTES = TRANSCRIPT_HEADER_MAX_BYTES


def _persisted_session_paths(slot_name: str) -> list["Path"]:
    """Every existing session transcript that *slot_name* could name.

    Returns more than one entry only when the key is genuinely ambiguous — see
    :func:`_probe_persisted_session`, which treats that as unknown rather than
    picking a winner.
    """
    if (
        not slot_name
        or "/" in slot_name
        or "\\" in slot_name
        or "\x00" in slot_name
        or ":" in slot_name
        or slot_name.startswith(".")
    ):
        # Defence-in-depth against path traversal; ``KIROCREW_SESSION_KEY``
        # normally has no path separators, but ``X-Session-Key`` is
        # attacker-controlled in principle even behind the secret
        # middleware. Reject forward slash (Linux/macOS) and backslash
        # (Windows) path separators, null bytes that can truncate C-level
        # path parsing, and leading dots that could target hidden
        # per-directory files outside the intended session namespace.
        #
        # The colon is rejected for Windows, where it is not an ordinary
        # character: ``WindowsPath("…/sessions") / "D:foo.jsonl"`` evaluates to
        # ``D:foo.jsonl`` — a DRIVE-RELATIVE path that silently escapes the
        # sessions directory entirely (verified; POSIX joins it literally and is
        # unaffected). It also spells an NTFS alternate data stream
        # (``file:stream``). A dashboard slot key never contains a colon: the
        # transport prefix is stripped by the caller before this point, and
        # ``_normalize_slot_key`` folds the key to ``[\\w\\-.]`` anyway.
        return []
    sess_dir = config_dir() / "sessions"
    if not sess_dir.exists():
        return []
    # Match the resolution order used by slack/interactions.py when
    # linking Slack threads to existing sessions: bare stem first, then
    # the ``dashboard_`` prefix fallback for dashboard slots. Cron sessions
    # persist under different names: ``history._safe_key`` folds ``:`` to
    # ``_``, so ``cron:{id}`` writes ``cron_{id}.jsonl`` and its linked
    # dashboard slot ``dashboard:cron-{id}`` writes ``dashboard_cron-{id}.jsonl``.
    # Probe those too so an idle-evicted cron session is recognised rather
    # than misclassified as forged.
    candidates = [sess_dir / f"{slot_name}.jsonl"]
    if not slot_name.startswith("dashboard_"):
        candidates.append(sess_dir / f"dashboard_{slot_name}.jsonl")
    candidates.append(sess_dir / f"cron_{slot_name}.jsonl")
    candidates.append(sess_dir / f"dashboard_cron-{slot_name}.jsonl")
    return [p for p in candidates if p.exists()]


def _persisted_session_path(slot_name: str) -> "Path | None":
    """First existing transcript for *slot_name*, or None.

    Existence only. When more than one candidate exists the answer is still
    "yes, a session exists" — which is all the establish-vs-forged check needs.
    Anything making an AUTHORIZATION decision must use
    :func:`_probe_persisted_session`, which refuses to guess between them.
    """
    matches = _persisted_session_paths(slot_name)
    return matches[0] if matches else None


def _session_has_persisted_history(slot_name: str) -> bool:
    """Return True iff the slot has a JSONL file in the data home's sessions/.

    A positive signal that the session has been **established** — i.e.
    that the key belongs to a real session rather than being forged or stale.
    It says nothing about the session's ``memory_mode``: every mode writes its
    transcript to disk (``_save_slot_to_history`` has no ``memory_mode`` gate,
    by design, so incognito/temporary tabs still survive a reload). Callers
    gating *memory writes* must therefore consult
    :func:`_persisted_session_memory_mode` as well — file existence alone is
    not evidence that writes are permitted.

    Used by ``api_lessons_create`` (in ``handlers/cron_lessons.py``) to distinguish
    between:

    * A legitimate MCP subprocess whose in-memory slot was evicted by the
      idle-sweep loop (``session.py``'s 30-minute timeout) or archived by a
      tab close. The subprocess still holds the original
      ``KIROCREW_SESSION_KEY`` env var, so it keeps sending the same
      ``X-Session-Key``, but ``state._slots`` has moved on. Without this
      check such calls return HTTP 400 ``unknown session`` even though the
      user is actively typing in the thread.

    * A forged or stale key from a context that never had a real session
      backing it — which should continue to be rejected.

    Only checks existence, not contents. Authentication of the caller is
    still enforced by the ``X-Internal-Secret`` middleware upstream; this
    check only governs the *established vs forged* distinction.
    """
    return _persisted_session_path(slot_name) is not None


def _persisted_session_memory_mode(slot_name: str) -> str | None:
    """Return the ``memory_mode`` recorded in *slot_name*'s session metadata.

    Three distinct outcomes, and the distinction IS the security property:

    * ``"persistent"`` / ``"incognito"`` / ``"temporary"`` — read from the
      metadata line. A metadata line that parses but carries no ``memory_mode``
      is reported as ``"persistent"``: the field postdates the feature, so a
      valid header without it is genuinely a legacy persistent session.
    * ``None`` — **unknown**. No file, or no parseable metadata object as the
      first line. Callers gating memory writes MUST deny on ``None`` rather
      than treat it as persistent. Denying is safe: ``ConversationLog.append``
      writes the metadata line when it creates the file, before any message is
      appended, so a session file whose first line is not metadata was not
      produced by a normal session and is no evidence that writes are allowed.

    This is the recovery path for a session whose in-memory state is gone but
    whose transcript is still on disk. Both in-memory signals a restricted
    session normally carries are dropped when a tab is archived
    (``api_chat_slot_close`` removes the slot from ``state._slots`` *and*
    discards its key from ``state._restricted_keys``), while the transcript —
    including its ``memory_mode`` marker — persists. Without reading that
    marker back, an archived incognito session whose MCP subprocess is still
    alive presents as an ordinary established session and its memory writes
    are allowed.

    Only the FIRST line is consulted, and only ``_METADATA_HEAD_MAX_BYTES`` of
    it: a later ``_type: metadata`` object is message content, not the header,
    and must not be able to redefine the mode. Byte-bounding keeps an enormous
    or adversarial first line from pinning memory.

    Blocking file I/O — call from a worker thread, never on the event loop
    (AUTOSDE ``no-blocking-call-on-event-loop``); prefer
    :func:`_probe_persisted_session`. Deliberately uncached: a cache would need
    invalidation on every mode change and could itself go stale, which is the
    exact failure class this closes.
    """
    path = _persisted_session_path(slot_name)
    if path is None:
        return None
    return _read_memory_mode(path)


def _read_memory_mode(path: "Path") -> str | None:
    """Read the ``memory_mode`` out of *path*'s metadata line. See above."""
    try:
        with open(path, "rb") as f:
            head = f.read(_METADATA_HEAD_MAX_BYTES)
    except OSError:
        return None
    first, _sep, _rest = head.partition(b"\n")
    # The shared header parse (allowlisted, fails closed on anything it does not
    # recognise) -- the same one the whole-install export and import apply.
    return memory_mode_from_header_line(first)


def _probe_persisted_session(slot_name: str) -> tuple[bool, str | None]:
    """``(file_exists, memory_mode_or_None)`` for *slot_name*.

    Refuses to guess when the key is **ambiguous**. ``slot_name`` reaches this
    function with its transport namespace already stripped
    (``sk.split(":", 1)[-1]``), so one stem can match several real transcripts —
    e.g. a legacy Slack thread at ``<ts>.jsonl`` and an archived dashboard slot
    named after that same ts at ``dashboard_<ts>.jsonl``. Taking the first
    candidate would let a *persistent* file answer for an *incognito* session and
    permit the write. Existence stays true (a session really does exist), but the
    mode is reported as ``None`` = unknown, which the caller denies on.

    Blocking I/O: hand this to a worker thread from an async caller
    (``await asyncio.to_thread(_probe_persisted_session, slot_name)``). It is a
    single composed call so one thread hop covers the whole probe.
    """
    matches = _persisted_session_paths(slot_name)
    if not matches:
        return False, None
    if len(matches) > 1:
        return True, None
    return True, _read_memory_mode(matches[0])
