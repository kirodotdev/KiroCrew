"""Spec Builder persistence, safe filesystem, and workspace adapters."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.platform_compat import RENAME_NOREPLACE_AVAILABLE, rename_noreplace

from .parsers import (
    _EDITABLE_DOCS,
    _PHASE_FILES,
    _SHA256_RE,
    _SLOT_KEY_RE,
    _decision_key,
    _entry_is_usable,
    _has_open_task,
    _owns_slot_key,
    _parse_tasks,
    _redact,
    _sha256_text,
    _usable_name,
    is_sensitive_path,
)

try:
    from kiro_crew.sel import sel
except Exception:  # pragma: no cover
    sel = None  # type: ignore[assignment]

try:
    from kiro_crew.hooks import safe_read_file_bytes_nolink
    from kiro_crew.pinned_fs import fd_real_path
except Exception:  # pragma: no cover - hooks always present in prod
    fd_real_path = None  # type: ignore[assignment]
    safe_read_file_bytes_nolink = None  # type: ignore[assignment]

try:
    from kiro_crew.sandbox import (
        create_subprocess_limited,
        sandboxed_spawn_argv,
        shielded_prepare_off_loop,
    )
except Exception:  # pragma: no cover - sandbox always present in prod
    create_subprocess_limited = None  # type: ignore[assignment]
    sandboxed_spawn_argv = None  # type: ignore[assignment]
    shielded_prepare_off_loop = None  # type: ignore[assignment]

logger = logging.getLogger("kirocrew.app.spec-builder")
APP_NAME = "spec-builder"

#: Override hooks; ``None`` resolves the data home on every call. Import-time
#: caching would break per-process home isolation and lazy data-home migration.
_STATE_DIR: Path | None = None
_INDEX_PATH: Path | None = None
_DELETED_PATH: Path | None = None
_SETTINGS_PATH: Path | None = None


def _state_dir() -> Path:
    """Where this app keeps its own state. Resolved per call, never cached."""
    return _STATE_DIR if _STATE_DIR is not None else config_dir() / "workspace" / APP_NAME


def _index_path() -> Path:
    return _INDEX_PATH if _INDEX_PATH is not None else _state_dir() / "index.json"


def _deleted_path() -> Path:
    """Spec directories the user deleted.

    Discovery adopts any spec-shaped directory under a known project root, so
    deleting a spec while leaving its markdown on disk (the documented behaviour
    — the .md files are the user's project files) made the next list scan adopt
    it straight back, as long as ANOTHER spec kept that root in the index.
    Deleting is a decision; this file remembers it.
    """
    return _DELETED_PATH if _DELETED_PATH is not None else _state_dir() / "deleted.json"


def _settings_path() -> Path:
    return _SETTINGS_PATH if _SETTINGS_PATH is not None else _state_dir() / "settings.json"


_STOP_FILE = "STOP"

#: Cap on a single spec document served to the browser. These are markdown
#: files; an oversized one should not be inlined into a JSON response.
_MAX_SPEC_BYTES = 1 << 20


# ── duplicate recovery state ─────────────────────────────────────────────────


_DuplicateRecoveryState = dict[str, asyncio.Task[None] | None]
_DUPLICATE_RECOVERY_STATE: web.AppKey[_DuplicateRecoveryState] = web.AppKey(
    "spec_builder_duplicate_recovery", dict
)


def _audit(operation: str, resources: str = "", outcome: str = "success") -> None:
    if sel is None:
        return
    try:
        sel().log_api_access(
            caller=APP_NAME, operation=operation, outcome=outcome, resources=resources
        )
    except Exception:
        logger.debug("SEL audit failed for %s", operation, exc_info=True)


def _audit_tool(
    outcome: str,
    subcommand: str,
    cwd: str,
    *,
    error: str = "",
    rc: int | None = None,
    critical: bool = False,
) -> bool:
    """Record a tool-invocation lifecycle event for a process this app spawns.

    BLOCKING when ``critical`` — call it via ``asyncio.to_thread``.

    Coarse by design: the git SUBCOMMAND and working directory, never the full
    argv (a branch name derives from user input).

    Returns False when the event could NOT be recorded. The "invoked" event is a
    precondition for spawning git, not a nice-to-have: with SEL missing or its log
    unwritable, a swallowed failure meant this app ran git on the user's repository
    with no tool-invocation trail at all. Outcome events stay best-effort — the
    process has already run by then, and losing the outcome must not turn a
    successful command into an error.

    ``critical`` is what makes the gate real. The default path ENQUEUES the event and
    a background writer flushes it, so a truthy return only proved the enqueue did not
    raise -- the record could still be dropped when the log is unwritable, leaving git
    to run unaudited. ``critical=True`` writes synchronously and re-raises a
    filesystem failure (see ``SecurityEventLog.log_tool_invocation``), so False here
    means the record genuinely did not land.
    """
    if sel is None:
        return False
    try:
        sel().log_tool_invocation(
            session_key="",
            source=f"app:{APP_NAME}",
            tool_name="git",
            tool_kind="subprocess",
            outcome=outcome,
            resources=_redact(cwd),
            error=error,
            metadata={"subcommand": subcommand, **({"rc": rc} if rc is not None else {})},
            critical=critical,
        )
    except Exception:
        logger.warning("SEL tool audit failed for git %s", subcommand, exc_info=True)
        return False
    return True


# ── settings + index (app-owned bookkeeping) ─────────────────────────────────

#: Longest model id the settings file stores.
#: The write handler REJECTS an over-length id (a sliced id is a *different*
#: string that is never served, so truncating would trade a clear 400 for a
#: silent fallback); the read chokepoint below degrades one to inherit instead,
#: because a load has nobody to hand a 400 to.
_MAX_MODEL_LEN = 128


def _load_settings() -> dict:
    """Read settings, treating the file's SHAPE and its FIELDS as untrusted.

    A hand-edited (or agent-edited) ``settings.json`` holding a list, a string or
    ``null`` would otherwise reach ``.get()`` in the handlers and 500 the endpoint.
    Anything that is not an object is the same as "no settings".

    Validating only the OUTER shape was not enough: ``{"base_path": []}`` is a
    dict, so it passed, and every reader then called ``.strip()`` on a list —
    500ing spec creation and the settings read. The field is normalized here, at
    the single read chokepoint, so no caller has to re-check its type.

    ``model`` gets the same treatment: a non-string or over-length value loads
    as ``""`` (= inherit the session layer's resolution), never as an error. An
    UNKNOWN model name is deliberately kept: no advertised-model list exists
    outside a live session, and the session layer's withhold
    (``_pinned_model_verdict`` in chat_runner) already keeps the pin, runs the
    worker on the backend default and surfaces a notice when a pick stops being
    served.
    """
    try:
        data = json.loads(_settings_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {"base_path": "", "model": ""}
    if not isinstance(data, dict):
        return {"base_path": "", "model": ""}
    if not isinstance(data.get("base_path"), str):
        data = {**data, "base_path": ""}
    raw_model = data.get("model")
    if not isinstance(raw_model, str) or len(raw_model.strip()) > _MAX_MODEL_LEN:
        data = {**data, "model": ""}
    else:
        model = raw_model.strip()
        # A value the redactor would alter is credential-shaped: slot.model is
        # serialized into dashboard payloads RAW (it is an id, not prose, so no
        # sink scrubs it), and settings.json is agent-writable -- so a credential
        # planted here would ride the stamp to the browser. Degrade to inherit;
        # this also fails closed when the security module is unavailable, same
        # as _redact itself. The write path rejects the same shape with a 400.
        if model and _redact(model) != model:
            model = ""
        data = {**data, "model": model}
    return data


def _save_settings(settings: dict) -> None:
    # atomic_write, not write_text: a truncating write that is interrupted (SIGTERM
    # during a gateway restart, a full disk) leaves invalid JSON behind, and both
    # loaders treat a JSONDecodeError as "empty" -- so the settings would silently
    # reset, or EVERY indexed spec would disappear from the app.
    _state_dir().mkdir(parents=True, exist_ok=True)
    atomic_write(_settings_path(), json.dumps(settings, indent=2))


#: Serializes every index read-modify-write. The transactions run on worker
#: threads (the file I/O must stay off the event loop), so an ``asyncio.Lock``
#: would not exclude them from each other -- two concurrent creates would read
#: the same index and the second write would silently drop the first. A
#: threading lock is the one that actually holds, and blocking on it happens on
#: a worker thread, never on the loop. The deletion tombstones share it: they are
#: the same shape of transaction on a second state file, and a delete mutates
#: both -- so one lock keeps a concurrent pair from interleaving either write.
_INDEX_LOCK = threading.Lock()

#: Cap on remembered deletions. Bounded so the file cannot grow without limit on
#: an instance that creates and deletes specs repeatedly; the oldest entries fall
#: off first, after which that directory may become discoverable again.
_MAX_TOMBSTONES = 500


def _load_deleted() -> list[str]:
    """Spec directories the user deleted, newest last. BLOCKING.

    Shape is treated as untrusted, like every other file this app reads.
    """
    try:
        data = json.loads(_deleted_path().read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, str) and d.strip()][-_MAX_TOMBSTONES:]


def _remember_deleted(spec_dir: str) -> None:
    """Record a deletion so discovery does not adopt the directory again.

    BLOCKING -- call via ``asyncio.to_thread``. Best-effort: failing to record it
    may make the spec discoverable again, but must not fail an already committed
    delete because project documents are not removed.
    """
    if not spec_dir:
        return
    try:
        # Keep read-modify-write under one lock so concurrent deletes cannot lose
        # either tombstone.
        with _INDEX_LOCK:
            current = [d for d in _load_deleted() if d != spec_dir]
            current.append(spec_dir)
            _state_dir().mkdir(parents=True, exist_ok=True)
            atomic_write(_deleted_path(), json.dumps(current[-_MAX_TOMBSTONES:], indent=2))
    except OSError:
        logger.warning("could not record the deletion of %s", _redact(spec_dir), exc_info=True)


def _forget_deleted(spec_dir: str) -> None:
    """Drop a tombstone because the user deliberately created this spec again.

    BLOCKING -- call via ``asyncio.to_thread``.
    """
    if not spec_dir:
        return
    try:
        # Same transaction, same lock: a concurrent remember/forget pair would
        # otherwise lose whichever write landed first.
        with _INDEX_LOCK:
            current = _load_deleted()
            if spec_dir not in current:
                return
            _state_dir().mkdir(parents=True, exist_ok=True)
            atomic_write(
                _deleted_path(), json.dumps([d for d in current if d != spec_dir], indent=2)
            )
    except OSError:
        logger.warning("could not clear the tombstone for %s", _redact(spec_dir), exc_info=True)


def _refresh_slot_keys(index: dict) -> None:
    """Rebuild the name -> slot-key map from an index snapshot.

    Called from both chokepoints -- every read and every write -- so a committed
    mutation immediately publishes its per-creation slot identity.

    Whole-dict replacement rather than in-place mutation: both chokepoints run on
    worker threads, and swapping one reference is atomic where an update is not.
    """
    global _SLOT_KEYS, _INDEXED_SPEC_DIRS, _INDEXED_SPEC_IDENTITIES, _INDEXED_SPEC_NAMES
    global _OBSERVED_SLOT_KEYS, _OBSERVED_SPEC_DIRS
    observed_slot_keys = dict(_OBSERVED_SLOT_KEYS)
    observed_spec_dirs = dict(_OBSERVED_SPEC_DIRS)
    _SLOT_KEYS = {}
    _INDEXED_SPEC_NAMES = {
        name
        for name, meta in index.items()
        if isinstance(name, str) and isinstance(meta, dict) and _usable_name(name)
    }
    _INDEXED_SPEC_DIRS = {
        _decision_key(str(meta.get("spec_dir", "")))
        for name, meta in index.items()
        if name in _INDEXED_SPEC_NAMES and isinstance(meta, dict) and meta.get("spec_dir")
    }
    indexed_identities: set[tuple[str, str, str]] = set()
    for name, meta in index.items():
        if not isinstance(meta, dict):
            continue
        observed = observed_slot_keys.get(name, "")
        slot_key = meta.get("slot_key")
        spec_dir = str(meta.get("spec_dir", ""))
        if isinstance(slot_key, str) and _owns_slot_key(name, slot_key):
            indexed_identities.add((name, _decision_key(spec_dir), slot_key))
        if observed and observed != slot_key:
            # Keep resolving the authenticated live creation. The raw entry stays
            # visible so alias scans can still find and block on its old worker,
            # while dispatch chokepoints reject the mismatched persisted key.
            _SLOT_KEYS[name] = observed
            continue
        if not isinstance(slot_key, str) or not _owns_slot_key(name, slot_key):
            continue
        _SLOT_KEYS[name] = slot_key
        if spec_dir:
            observed_spec_dirs.setdefault(slot_key, _decision_key(spec_dir))
    _INDEXED_SPEC_IDENTITIES = indexed_identities
    # Never forget a per-creation identity during this process. If an agent later
    # removes its persisted key, the ordinary resolver must stop using it because the
    # index no longer authenticates that mapping, but legacy migration must also not
    # reinterpret the same entry as a genuine pre-key spec and mint a second slot.
    for name, slot_key in _SLOT_KEYS.items():
        legacy_key = f"spec-builder-{name}"
        observed = observed_slot_keys.get(name, "")
        if not observed or observed == legacy_key:
            observed_slot_keys[name] = slot_key
    # Event-loop handlers iterate these witnesses without taking the blocking
    # index lock. Publish complete copies so a worker can never resize an object
    # while a handler is traversing it.
    _OBSERVED_SLOT_KEYS = observed_slot_keys
    _OBSERVED_SPEC_DIRS = observed_spec_dirs


def _forget_observed_slot_identity(name: str, *slot_keys: str) -> None:
    """Release slot identities only after this process deletes that creation."""
    global _SLOT_KEYS, _OBSERVED_SLOT_KEYS, _OBSERVED_SPEC_DIRS
    released = {slot_key for slot_key in slot_keys if slot_key}
    observed_slot_keys = dict(_OBSERVED_SLOT_KEYS)
    resolved_slot_keys = dict(_SLOT_KEYS)
    observed_spec_dirs = dict(_OBSERVED_SPEC_DIRS)
    # A fully rewritten index can remove the old name as well as its directory and
    # slot key. Teardown captures the old creation by its process-monotonic witness,
    # so successful deletion must release every name that still points at one of
    # those captured keys. Limiting this to the current name leaves the old name
    # permanently pinned to a worker the app itself just removed.
    for observed_name, observed_key in list(observed_slot_keys.items()):
        if observed_key in released:
            observed_slot_keys.pop(observed_name, None)
    for resolved_name, resolved_key in list(resolved_slot_keys.items()):
        if resolved_key in released:
            resolved_slot_keys.pop(resolved_name, None)
    # Keep the explicit name cleanup for a malformed empty spelling.
    if not observed_slot_keys.get(name):
        observed_slot_keys.pop(name, None)
    if not resolved_slot_keys.get(name):
        resolved_slot_keys.pop(name, None)
    for slot_key in released:
        observed_spec_dirs.pop(slot_key, None)
    _OBSERVED_SLOT_KEYS = observed_slot_keys
    _SLOT_KEYS = resolved_slot_keys
    _OBSERVED_SPEC_DIRS = observed_spec_dirs


def _load_index_snapshot() -> tuple[dict, bool]:
    """Read the index and report whether its top-level state is authoritative.

    A missing file is an authoritative empty index. Read failures, invalid JSON,
    and a non-object top level are not: callers that remove orphaned workers or
    write a replacement snapshot must fail closed rather than treating corruption
    as proof that no bindings exist. Those failures also preserve the last resolver
    map, so a transient read error cannot detach a live worker in this process.

    The top-level object was already guarded, then entries that were not objects.
    Neither was enough: ``{"demo": {}}`` is a dict, so it survived, and handlers
    that index the required fields directly (``meta["spec_dir"]``) then raised
    KeyError and 500ed the request. An entry is only usable if it carries both
    identity fields as non-empty strings, so that is the bar here -- at the single
    read chokepoint, rather than every handler re-checking.

    A malformed entry is unusable either way, so drop it rather than serve a crash
    -- the spec's files stay on disk and rediscovery can re-add it.

    Delete reservations left by a process that is gone are dropped here too unless
    their durable teardown boundary is committed. Before that boundary, an orphaned
    reservation protects nothing and only hides a live spec. After it, clearing the
    reservation would resurrect a spec whose conversation may already be archived
    and whose queued work may already be gone; it remains hidden until DELETE is
    retried. Clearing an ordinary stale reservation in the returned copy needs no
    write: this is the read half of ``_mutate_index``, so the next mutation persists
    the cleanup. A reservation this process still owns is left strictly alone.

    Duplicate recovery is deliberately NOT part of this read path. It renames or
    removes files and must run once at app startup under the index lock, rather
    than repeating those side effects on every list/detail poll until some later
    mutation happens to persist the cleaned reservation.
    """
    try:
        data = json.loads(_index_path().read_text())
    except FileNotFoundError:
        clean: dict = {}
        _refresh_slot_keys(clean)
        return clean, True
    except (OSError, json.JSONDecodeError):
        return {}, False
    if not isinstance(data, dict):
        return {}, False
    clean = {
        k: v
        for k, v in data.items()
        if isinstance(k, str) and _usable_name(k) and isinstance(v, dict) and _entry_is_usable(v)
    }
    if len(clean) != len(data):
        logger.warning(
            "spec index had %d malformed entries — ignoring them", len(data) - len(clean)
        )
    stale = [
        k for k, v in clean.items() if _DELETING in v and not _reservation_is_ours(v, _DELETING)
    ]
    for k in stale:
        clean[k].pop(_DELETING, None)
    if stale:
        logger.info(
            "spec index: released %d delete reservation(s) abandoned by an earlier process",
            len(stale),
        )
    _refresh_slot_keys(clean)
    return clean, True


def _load_index() -> dict:
    """Read the usable index entries, degrading an unreadable file to empty."""
    return _load_index_snapshot()[0]


def _save_index(index: dict) -> None:
    """Persist the index. Atomic (temp file + rename) -- see ``_save_settings``:
    a torn write here loses the user's whole spec list."""
    _state_dir().mkdir(parents=True, exist_ok=True)
    atomic_write(_index_path(), json.dumps(index, indent=2))
    # The written snapshot is now the truth, so the resolver map follows it here as
    # well as on read -- otherwise a just-committed slot key stays invisible until
    # something happens to re-read the file.
    _refresh_slot_keys(index)


async def _aload_index() -> dict:
    """Read the index off the event loop. THE ONLY way a handler may read it.

    ``_load_index`` is a file read plus a JSON parse: on a stalled data home (or
    simply a large index) doing that inline froze the gateway -- and the detail
    endpoint is polled every 2.5s during a build, so it froze it repeatedly. Takes
    the index lock so a read cannot observe a half-applied transaction.
    """

    def _read() -> dict:
        with _INDEX_LOCK:
            return _load_index()

    return await asyncio.to_thread(_read)


async def _aload_index_snapshot() -> tuple[dict, bool]:
    """Read the index and its authoritative-state bit off the event loop."""

    def _read() -> tuple[dict, bool]:
        with _INDEX_LOCK:
            return _load_index_snapshot()

    return await asyncio.to_thread(_read)


async def _aload_index_with_slot_identity(name: str) -> tuple[dict, str, str]:
    """Read an index snapshot and its effective slot identity in one lock hold.

    BLOCKING work is off-loop.

    ``_load_index`` refreshes the process resolver maps. Returning to the event
    loop before reading those maps lets another index worker replace them, pairing
    stale metadata with a different creation's runtime key.
    """

    def _read() -> tuple[dict, str, str]:
        with _INDEX_LOCK:
            index = _load_index()
            return index, _slot_key(name), _OBSERVED_SLOT_KEYS.get(name, "")

    return await asyncio.to_thread(_read)


async def _mutate_index(
    mutate: Callable[[dict], bool], *, on_commit: Callable[[], None] | None = None
) -> bool:
    """Read-modify-write the index atomically w.r.t. the event loop AND threads.

    THE ONLY sanctioned way for a request handler to write the index. A handler
    that loads the index, awaits (authorization, a body read, a subprocess, a
    slot teardown) and then writes back its *stale* snapshot resurrects entries
    a concurrent DELETE removed and drops entries a concurrent CREATE added --
    the whole file is overwritten, so every intervening change is lost.

    ``mutate`` runs on a worker thread against a FRESHLY read index and returns
    True to commit or False to abort (typically: the spec is gone, so this
    request must not recreate it). Read, mutation and write happen inside one
    ``to_thread`` hop under ``_INDEX_LOCK``, so neither an await nor a second
    worker thread can interleave: offloading alone would still let two
    concurrent creates read the same index and drop one of them.

    ``on_commit`` updates process-local identity state while that same lock is
    still held. This keeps a same-name create from observing a committed delete
    before the old creation's in-memory identity has been released.
    """

    def _apply() -> bool:
        with _INDEX_LOCK:
            index = _load_index()
            if not mutate(index):
                return False
            _save_index(index)
            if on_commit is not None:
                on_commit()
            return True

    return await asyncio.to_thread(_apply)


#: Set on an entry whose delete is mid-flight. The entry stays in the index so its
#: NAME stays reserved: a rollback then restores the original entry (and its
#: per-creation slot key, which only that name may own), and a same-name create
#: cannot slip into the window. Hidden from the list while set.
_DELETING = "deleting"
_DELETE_TEARDOWN_COMMITTED = "teardown_committed"

#: Set on a destination entry before duplicate starts writing its files. Keeping
#: the entry in the index reserves the name against a concurrent create, while
#: list/detail/mutation paths hide the not-yet-complete copy.
_DUPLICATING = "duplicating"

#: Provenance marker carried inside a duplicate's hidden staging directory.
#: The directory is renamed into place only after every document is durable, so
#: this marker lets a restarted gateway distinguish its complete publication
#: from an unrelated directory at the same path.
_DUPLICATE_MARKER = ".kirocrew-duplicate"
_DUPLICATE_TOKEN_RE = re.compile(r"[0-9a-f]{32}")

#: Identity of THIS gateway process, stamped into a delete reservation so
#: ``_load_index`` can tell a reservation this process still owns from one left
#: behind by a process that is gone.
#:
#: A reservation is ordinarily correct only while its request is alive. Once the
#: durable ``_DELETE_TEARDOWN_COMMITTED`` boundary is set, at least one destructive
#: slot teardown may follow and the reservation instead survives a restart until a
#: retry completes deletion. The PID is here for diagnostics; the uuid4 is what
#: makes ordinary pre-teardown ownership sound across PID reuse.
_PROCESS_ID = f"{os.getpid()}:{uuid.uuid4().hex}"


def _reservation_is_ours(meta: dict, field: str = _DELETING) -> bool:
    """True when a reservation is live here or durably destructive.

    A pre-existing reservation from an older build stores a bare timestamp rather
    than a mapping; it has no owner, so it reads as foreign -- which is the right
    answer, because this process demonstrably did not write it.
    """
    held = meta.get(field)
    return isinstance(held, dict) and (
        held.get("owner") == _PROCESS_ID
        or (field == _DELETING and held.get(_DELETE_TEARDOWN_COMMITTED) is True)
    )


async def _mark_deleting(name: str, *, expect_spec_dir: str, expect_slot_key: str) -> bool:
    """Reserve *name* for a delete in flight. Identity-pinned like every mutation."""

    def _apply(index: dict) -> bool:
        meta = index.get(name)
        if meta is None or str(meta.get("spec_dir", "")) != expect_spec_dir:
            return False
        actual_key = str(meta.get("slot_key", ""))
        if expect_slot_key and actual_key and actual_key != expect_slot_key:
            return False
        previous = meta.get(_DELETING)
        committed = isinstance(previous, dict) and previous.get(_DELETE_TEARDOWN_COMMITTED) is True
        meta[_DELETING] = {
            "owner": _PROCESS_ID,
            "at": time.time(),
            _DELETE_TEARDOWN_COMMITTED: committed,
        }
        return True

    return await _mutate_index(_apply)


async def _commit_delete_teardown(name: str, *, expect_spec_dir: str, expect_slot_key: str) -> bool:
    """Make the reservation restart-durable before destroying any worker slot."""

    def _apply(index: dict) -> bool:
        meta = index.get(name)
        if meta is None or str(meta.get("spec_dir", "")) != expect_spec_dir:
            return False
        actual_key = str(meta.get("slot_key", ""))
        if expect_slot_key and actual_key and actual_key != expect_slot_key:
            return False
        held = meta.get(_DELETING)
        if not isinstance(held, dict) or held.get("owner") != _PROCESS_ID:
            return False
        held[_DELETE_TEARDOWN_COMMITTED] = True
        return True

    return await _mutate_index(_apply)


async def _unmark_deleting(name: str, *, expect_spec_dir: str) -> bool:
    """Release the reservation, leaving the entry exactly as it was."""

    def _apply(index: dict) -> bool:
        meta = index.get(name)
        if meta is None or str(meta.get("spec_dir", "")) != expect_spec_dir:
            return False
        return meta.pop(_DELETING, None) is not None

    return await _mutate_index(_apply)


async def _touch_spec(
    name: str,
    *,
    expect_spec_dir: str | None = None,
    expect_slot_key: str | None = None,
    **fields: Any,
) -> dict | None:
    """Stamp ``fields`` + ``updated_at`` on a spec, re-reading the index first.

    Returns the updated entry (a copy, safe to read after the hop) or ``None``
    if the spec no longer exists -- which the caller MUST treat as "deleted
    while this request was in flight" and abort, not as a reason to recreate it.

    ``expect_spec_dir`` additionally pins the spec's IDENTITY. A name is not an
    identity: delete-and-recreate under the same name (pointing somewhere else)
    leaves the entry present, so a "still exists" check passes while the request
    is now operating on a different spec -- pairing documents read from the old
    directory with the new metadata, or dispatching a run whose prompt names the
    old project. Passing the ``spec_dir`` the request captured makes the mismatch
    a refusal instead.

    An entry RESERVED for deletion (``_DELETING``) is treated as already gone.
    Once teardown begins, no mutation may publish into the captured worker slot;
    enforcing that here protects every caller of this shared mutation path.
    """
    fresh: dict = {}

    def _apply(index: dict) -> bool:
        meta = index.get(name)
        if meta is None:
            return False
        if meta.get(_DELETING) or meta.get(_DUPLICATING):
            return False
        if expect_spec_dir is not None and str(meta.get("spec_dir", "")) != expect_spec_dir:
            return False
        if expect_slot_key:
            actual_key = str(meta.get("slot_key", ""))
            if actual_key and actual_key != expect_slot_key:
                return False
        meta.update(fields)
        meta["updated_at"] = time.time()
        fresh.update(meta)
        return True

    return fresh if await _mutate_index(_apply) else None


# ── path resolution ──────────────────────────────────────────────────────────


def _safe_dir(raw: str, *, must_exist: bool = True) -> Path | None:
    """Sanitize a caller-supplied directory path.

    Returns a fully-normalized absolute ``Path``, or ``None`` if the value is
    not usable. This is the single chokepoint every caller-supplied directory
    must pass through, so the guarantees hold uniformly:

      * ``~`` expanded and symlinks resolved BEFORE the sensitivity test, so a
        symlink planted inside a benign directory cannot smuggle the target past
        it;
      * must be absolute -- asserted on the expanded input, BEFORE ``realpath``,
        which would otherwise make every value absolute and the test vacuous;
      * must not be a sensitive path (credential stores, ``.ssh``, ``.aws``,
        policy files) per ``kiro_crew.security.is_sensitive_path``;
      * with ``must_exist`` (the default) it must already be a directory.

    ``must_exist=False`` supports a storage destination the app will create.
    Sensitivity is then also checked against the nearest EXISTING ancestor, so
    naming a not-yet-created subdirectory of a credential directory is still
    refused rather than slipping through on a stat miss.

    """
    if not raw or not raw.strip():
        return None
    expanded = os.path.expanduser(raw.strip())
    # Test absoluteness before realpath, which would resolve every relative value
    # against the gateway cwd. Agent-writable values such as "." must not inherit
    # that checkout as their working directory.
    if not os.path.isabs(expanded):
        return None
    resolved = Path(os.path.realpath(expanded))
    if is_sensitive_path(str(resolved)):
        return None
    if must_exist:
        if not resolved.is_dir():
            return None
        return resolved
    # Destination may not exist yet: validate the nearest existing ancestor.
    ancestor = resolved
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir() or is_sensitive_path(str(ancestor)):
        return None
    return resolved


def _safe_dir_optional(raw: str) -> Path | None:
    """``_safe_dir(raw, must_exist=False)`` as a positional-only callable, so it
    can be handed to ``asyncio.to_thread`` without a lambda."""
    return _safe_dir(raw, must_exist=False)


def _contained(child: Path, root: Path) -> bool:
    """True when ``child`` is ``root`` or lies beneath it, after normalization.

    Belt-and-braces against traversal: ``_NAME_RE`` already forbids ``.`` and
    ``/`` in spec names, but the containment test makes the invariant explicit
    at the point of use rather than implied by a regex three functions away.
    """
    try:
        Path(os.path.realpath(child)).relative_to(Path(os.path.realpath(root)))
        return True
    except ValueError:
        return False


#: Non-hidden build/VCS noise to hide from the folder picker. Hidden entries
#: need no listing here — _scan_subdirs skips everything starting with "." —
#: and spelling them out both duplicated that rule and put a literal internal
#: path marker in the source, which the repo's scrub lint rejects.
#: True when this platform can pin a directory and operate relative to it.
#: The confinement in the sentinel helpers depends on ``open``, ``unlink`` and the
#: rename family all accepting a directory descriptor, and Windows has none of
#: them, so the capability is resolved once here rather than guessed per call.
#: Probed via ``os.rename``: CPython registers the rename family under that name,
#: so ``os.replace in os.supports_dir_fd`` is False even where the pinned
#: ``os.replace(..., src_dir_fd=, dst_dir_fd=)`` call works (verified on Linux).
_CAN_PIN_DIR = (
    hasattr(os, "O_DIRECTORY")
    and os.mkdir in os.supports_dir_fd
    and os.open in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
)

# Publishing a complete staging directory must be one atomic no-replace step.
# A separate existence check plus os.rename() is not equivalent: another writer
# can create an empty destination in between and POSIX rename then replaces it.
_CAN_PUBLISH_DIR_NOREPLACE = _CAN_PIN_DIR and RENAME_NOREPLACE_AVAILABLE

_BROWSE_SKIP = {"node_modules", "__pycache__", "venv", "env"}
#: Cap on subdirectories returned by one browse call. A directory with tens of
#: thousands of entries would otherwise produce a response the picker can't use
#: and a payload the browser has to parse.
_BROWSE_MAX_DIRS = 500


def _scan_subdirs(base: str) -> list[dict[str, str]]:
    """List browsable subdirectories of *base*. BLOCKING — call via to_thread.

    Skips build/VCS noise and hidden entries, and resolves symlinks BEFORE the
    sensitivity test so a link inside a benign directory can't point at a
    credential directory and be listed.
    """
    out: list[dict[str, str]] = []
    try:
        with os.scandir(base) as it:
            entries = sorted(it, key=lambda e: e.name.lower())
        for entry in entries:
            if len(out) >= _BROWSE_MAX_DIRS:
                break
            if entry.name in _BROWSE_SKIP or entry.name.startswith("."):
                continue
            try:
                if not entry.is_dir(follow_symlinks=True):
                    continue
                if is_sensitive_path(os.path.realpath(entry.path)):
                    continue
            except OSError:
                continue
            out.append({"name": entry.name, "path": entry.path})
    except (PermissionError, OSError):
        pass
    return out


def _resolve_spec_dir(working_dir: str, name: str) -> Path:
    """Default: ``<working_dir>/.kiro/specs/<name>``. When settings.base_path is
    an absolute path, use ``<base_path>/<name>`` instead (still per-spec)."""
    base = _load_settings().get("base_path", "").strip()
    if base:
        return (Path(base) / name).resolve()
    return (Path(working_dir) / ".kiro" / "specs" / name).resolve()


#: name -> persisted slot key, rebuilt from every index read (see _load_index).
#: Replaced WHOLESALE rather than mutated: _load_index runs in worker threads, and
#: swapping one dict reference is atomic where an in-place update is not.
_SLOT_KEYS: dict[str, str] = {}

#: Valid raw identities in the latest complete index snapshot. Durable execution
#: loops use this to distinguish a current creation from an orphan whose name,
#: directory and slot were all rewritten while the gateway was down.
_INDEXED_SPEC_IDENTITIES: set[tuple[str, str, str]] = set()

# Usable rows remain control endpoints even when their agent-written slot key is
# absent or invalid. These cold-start sets preserve name/directory reachability
# without treating a malformed slot key as an authenticated identity.
_INDEXED_SPEC_NAMES: set[str] = set()
_INDEXED_SPEC_DIRS: set[str] = set()

#: Last ownership-valid identity observed for each name during this process. This
#: is deliberately not cleared by a later index read: absence after a per-creation
#: key was seen is tampering/corruption, not evidence that the entry is legacy.
_OBSERVED_SLOT_KEYS: dict[str, str] = {}

#: Monotonic creation -> directory witnesses for this process. An in-flight slot
#: remains reachable after the agent rewrites both its index name and slot key.
_OBSERVED_SPEC_DIRS: dict[str, str] = {}


def _observed_slot_keys_for_dir(dir_key: str) -> set[str]:
    """Creation keys this process authenticated on the canonical directory."""
    normalized_dir = _decision_key(dir_key)
    return {
        slot_key
        for slot_key, observed_dir in _OBSERVED_SPEC_DIRS.items()
        if observed_dir == normalized_dir
    }


def _unindexed_observed_slot_keys() -> set[str]:
    """Authenticated creation keys no longer represented anywhere in the index."""
    # The resolver retains an authenticated K1 for a surviving name even when
    # the agent removes or corrupts the raw slot_key.  Such a name remains a
    # valid Stop/Delete endpoint, so it is not a global orphan merely because
    # the stricter raw-identity set quite correctly excludes its malformed row.
    indexed_names = _INDEXED_SPEC_NAMES
    indexed = {slot_key for _name, _dir_key, slot_key in _INDEXED_SPEC_IDENTITIES}
    # A raw K1 -> K2 rewrite does not remove the creation's control endpoint:
    # the observed name still resolves to K1 and Stop/Delete on that name can
    # reach it. Only keys whose observed names all disappeared are globally
    # endpoint-less and safe for an unrelated recovery action to capture.
    controlled = {
        slot_key
        for observed_name, slot_key in _OBSERVED_SLOT_KEYS.items()
        if observed_name in indexed_names and slot_key
    }
    return (
        (
            set(_OBSERVED_SPEC_DIRS)
            | {slot_key for slot_key in _OBSERVED_SLOT_KEYS.values() if slot_key}
        )
        - indexed
        - controlled
    )


def _slot_key(name: str) -> str:
    """This spec's chat-slot key.

    Prefers the key PERSISTED when the spec was created. A per-creation key keeps
    same-name specs from sharing a transcript.

    Falls back to the name-derived form for entries written before that key existed
    (and for a persisted value that fails the grammar), so existing specs keep the
    transcript they already have.
    """
    persisted = _SLOT_KEYS.get(name)
    if persisted and _SLOT_KEY_RE.match(persisted):
        return persisted
    return f"spec-builder-{name}"


def _new_slot_key(name: str) -> str:
    """A fresh, unique slot key for a spec being created."""
    return f"spec-builder-{name}-{uuid.uuid4().hex[:8]}"


async def _pin_legacy_slot_identity(name: str, meta: dict) -> dict | None:
    """Persist a genuine pre-key spec's legacy identity before dispatch.

    Missing ``slot_key`` has two meanings that must not be conflated: an index
    written before per-creation keys existed, or an agent deleting the key of a
    live per-creation worker. The latter was already observed by this process and
    therefore fails closed. A never-observed missing entry is upgraded atomically;
    every later alias scan can then apply the same strict persisted-key rule.
    """
    persisted = meta.get("slot_key")
    observed = _OBSERVED_SLOT_KEYS.get(name, "")
    if isinstance(persisted, str) and persisted:
        return (
            meta
            if _owns_slot_key(name, persisted) and (not observed or persisted == observed)
            else None
        )
    legacy_key = f"spec-builder-{name}"
    if observed and observed != legacy_key:
        return None
    expected_dir = str(meta.get("spec_dir", ""))
    pinned: dict = {}

    def _apply(index: dict) -> bool:
        current = index.get(name)
        if current is None or str(current.get("spec_dir", "")) != expected_dir:
            return False
        current_key = current.get("slot_key")
        if isinstance(current_key, str) and current_key:
            return False
        seen = _OBSERVED_SLOT_KEYS.get(name, "")
        if seen and seen != legacy_key:
            return False
        current["slot_key"] = legacy_key
        pinned.update(current)
        return True

    return pinned if await _mutate_index(_apply) else None


def _spec_file(spec_dir: Path, fname: str) -> Path | None:
    """Resolve ``spec_dir/fname`` for reading, or ``None`` if it isn't safe.

    The spec directory is agent- and user-writable, so a *file inside it* is
    untrusted input even though the directory itself passed ``_safe_dir``. A
    symlink planted at ``requirements.md`` -> ``~/.aws/credentials`` would
    otherwise be read and served to the browser, and a symlink at ``STOP``
    would let a write land on an arbitrary target — both bypassing the
    directory-level ``is_sensitive_path`` test entirely.

    Refuses when: the entry (or any parent inside the spec dir) is a symlink,
    the realpath escapes the spec dir, or the realpath is sensitive.
    """
    p = spec_dir / fname
    try:
        if p.is_symlink():
            return None
        real = Path(os.path.realpath(p))
        # Containment is checked against the REAL spec dir so a symlinked
        # ancestor can't widen the allowed set.
        if not _contained(real, Path(os.path.realpath(spec_dir))):
            return None
        if is_sensitive_path(str(real)):
            return None
    except OSError:
        return None
    return p


def _read_spec_text(spec_dir: Path, fname: str) -> str | None:
    """Read one spec file safely, or ``None`` when absent/unsafe/unreadable.

    ``safe_read_file_bytes_nolink`` opens with ``O_NOFOLLOW`` and validates the
    descriptor itself. The spec-writing agent can replace a path between a name
    check and an open, so the inode validated must be exactly the inode read.

    Capped at ``_MAX_SPEC_BYTES``: these are markdown documents, and an
    oversized file should not be inlined into a JSON response.
    """
    if safe_read_file_bytes_nolink is None:  # pragma: no cover - fail closed
        return None
    try:
        raw = safe_read_file_bytes_nolink(
            str(spec_dir / fname),
            within_root=str(spec_dir),
            max_bytes=_MAX_SPEC_BYTES,
        )
    except Exception:  # pragma: no cover - helper is defensive; fail closed
        return None
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


def _verified_spec_dir(spec_dir: Path) -> Path | None:
    """Return *spec_dir* only if it is still EXACTLY itself, else ``None``.

    Fails closed when the indexed path (or any component of it) is a symlink,
    i.e. when ``realpath`` disagrees with the path the index recorded. Every
    stored spec_dir is written fully resolved (``_safe_dir`` + ``_resolve_spec_dir``
    both realpath/resolve), so a disagreement means the directory was REPLACED
    after indexing.

    Sentinel operations must not follow a replaced directory: otherwise a
    symlink to another paused spec could remove that spec's STOP file.
    """
    try:
        if not spec_dir.is_absolute():
            return None
        # normcase for Windows, where the same directory can be spelled with a
        # different case or separator without being a different directory.
        if os.path.normcase(os.path.realpath(spec_dir)) != os.path.normcase(str(spec_dir)):
            return None
        if not spec_dir.is_dir() or is_sensitive_path(str(spec_dir)):
            return None
        return spec_dir
    except OSError:
        return None


def _open_verified_dir(spec_dir: Path) -> tuple[Path, int] | None:
    """Open *spec_dir* and prove the descriptor still names that exact path.

    ``O_NOFOLLOW`` covers only the final component. An agent can replace an
    ancestor with a symlink after pathname validation but before ``os.open``;
    descriptor-relative writes would then be pinned safely to the wrong tree.
    Resolving the opened descriptor closes that window because all subsequent
    mutations use the same descriptor whose identity was authorized here.
    """
    real_dir = _verified_spec_dir(spec_dir)
    if real_dir is None or not _CAN_PIN_DIR or fd_real_path is None:
        return None
    try:
        dir_fd = os.open(
            real_dir,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        return None
    try:
        opened_path = fd_real_path(dir_fd)
        expected = os.path.normcase(str(real_dir))
        if opened_path is None or os.path.normcase(os.path.normpath(opened_path)) != expected:
            os.close(dir_fd)
            return None
        return real_dir, dir_fd
    except (OSError, ValueError):
        os.close(dir_fd)
        return None


def _create_open_verified_dir(spec_dir: Path) -> tuple[Path, int, int] | None:
    """Create one child and retain verified descriptors for it and its parent."""
    if not spec_dir.is_absolute() or spec_dir.name in {"", ".", ".."}:
        return None
    opened_parent = _open_verified_dir(spec_dir.parent)
    if opened_parent is None:
        return None
    _real_parent, parent_fd = opened_parent
    dir_fd = -1
    try:
        os.mkdir(spec_dir.name, 0o700, dir_fd=parent_fd)
        dir_fd = os.open(
            spec_dir.name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened_path = fd_real_path(dir_fd) if fd_real_path is not None else None
        expected = os.path.normcase(str(spec_dir))
        if opened_path is None or os.path.normcase(os.path.normpath(opened_path)) != expected:
            os.close(dir_fd)
            return None
        retained_parent_fd = parent_fd
        parent_fd = -1
        return spec_dir, dir_fd, retained_parent_fd
    except OSError:
        if dir_fd >= 0:
            os.close(dir_fd)
        return None
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _create_spec_doc(
    spec_dir: Path,
    fname: str,
    text: str,
    expected_dir_identity: tuple[int, int] | None = None,
) -> tuple[str, tuple[int, int, int, int] | None]:
    """Create one absent spec document and return a rollback identity.

    Duplication owns an empty destination, so ``O_EXCL`` gives it a real atomic
    boundary: an IDE or agent that creates the same file first wins and is never
    overwritten. The returned stat tuple lets failure cleanup remove only the
    exact file this call created; a file replaced or modified by another writer
    is left alone.

    BLOCKING -- call via ``asyncio.to_thread``.
    """
    if fname not in _EDITABLE_DOCS:
        return "not_editable", None
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_SPEC_BYTES:
        return "too_large", None
    if not _CAN_PIN_DIR:
        return "unsupported_platform", None
    opened_dir = _open_verified_dir(spec_dir)
    if opened_dir is None:
        return "unsafe_dir", None
    _real_dir, dir_fd = opened_dir
    if expected_dir_identity is not None:
        try:
            dir_info = os.fstat(dir_fd)
            if (dir_info.st_dev, dir_info.st_ino) != expected_dir_identity:
                os.close(dir_fd)
                return "identity_mismatch", None
        except OSError:
            os.close(dir_fd)
            return "identity_mismatch", None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(fname, flags, 0o600, dir_fd=dir_fd)
        try:
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("document write made no progress")
                remaining = remaining[written:]
            os.fsync(fd)
            stat = os.fstat(fd)
            return "", (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        except OSError:
            # Return the exact partial inode to the duplicate transaction. Its
            # rollback removes it only if no other writer replaced or modified it.
            try:
                stat = os.fstat(fd)
                identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            except OSError:
                identity = None
            return "write_failed", identity
        finally:
            os.close(fd)
            fd = -1
    except FileExistsError:
        return "conflict", None
    except OSError:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        return "write_failed", None
    finally:
        os.close(dir_fd)


def _rollback_staged_docs(spec_dir: Path, created: dict[str, tuple[int, int, int, int]]) -> bool:
    """Remove unchanged files created by a failed duplicate.

    Cleanup deliberately leaves the empty hidden stage directory. POSIX has no
    portable inode-bound rmdir, so removing it by name would reopen a race where
    an attacker swaps in a different directory after descriptor validation.

    Returns true only when no editable document remains. The provenance marker
    stays in place until the caller durably releases the index reservation, so a
    crash during rollback still leaves recovery authority for any residue.
    """
    opened_dir = _open_verified_dir(spec_dir)
    if opened_dir is None:
        return False
    _, dir_fd = opened_dir
    try:
        for fname in _EDITABLE_DOCS:
            try:
                stat = os.stat(fname, dir_fd=dir_fd, follow_symlinks=False)
                identity = created.get(fname)
                current = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                if identity is not None and current == identity:
                    os.unlink(fname, dir_fd=dir_fd)
            except OSError:
                continue
        for fname in _EDITABLE_DOCS:
            try:
                os.stat(fname, dir_fd=dir_fd, follow_symlinks=False)
                return False
            except FileNotFoundError:
                continue
            except OSError:
                return False
        return True
    finally:
        os.close(dir_fd)


def _write_duplicate_marker_at(dir_fd: int, token: str) -> bool:
    """Create the provenance marker relative to an already verified directory."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(_DUPLICATE_MARKER, flags, 0o600, dir_fd=dir_fd)
        remaining = memoryview(token.encode("ascii"))
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                return False
            remaining = remaining[written:]
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        if fd >= 0:
            os.close(fd)


def _write_duplicate_marker(stage_dir: Path, token: str) -> bool:
    """Create the provenance marker in a descriptor-pinned staging directory."""
    opened_dir = _open_verified_dir(stage_dir)
    if opened_dir is None:
        return False
    _real_dir, dir_fd = opened_dir
    try:
        return _write_duplicate_marker_at(dir_fd, token)
    finally:
        os.close(dir_fd)


def _duplicate_marker_matches_at(dir_fd: int, token: str) -> bool:
    """Read a duplicate marker relative to an already verified directory."""
    fd = -1
    try:
        fd = os.open(
            _DUPLICATE_MARKER,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=dir_fd,
        )
        return os.read(fd, 256).decode("ascii", errors="strict") == token
    except (OSError, UnicodeError):
        return False
    finally:
        if fd >= 0:
            os.close(fd)


def _duplicate_marker_matches(spec_dir: Path, token: str) -> bool:
    """Read a duplicate provenance marker without following directory links."""
    opened_dir = _open_verified_dir(spec_dir)
    if opened_dir is None:
        return False
    _real_dir, dir_fd = opened_dir
    try:
        return _duplicate_marker_matches_at(dir_fd, token)
    finally:
        os.close(dir_fd)


def _duplicate_stage_identity(stage_dir: Path, token: str) -> tuple[int, int] | None:
    """Return the inode identity of a descriptor-pinned, matching stage."""
    opened_dir = _open_verified_dir(stage_dir)
    if opened_dir is None:
        return None
    _real_dir, dir_fd = opened_dir
    try:
        if not _duplicate_marker_matches_at(dir_fd, token):
            return None
        info = os.fstat(dir_fd)
        return info.st_dev, info.st_ino
    except OSError:
        return None
    finally:
        os.close(dir_fd)


def _create_duplicate_stage(stage_dir: Path, token: str) -> str:
    """Create and durably mark a hidden stage before its index reservation."""
    if not _CAN_PUBLISH_DIR_NOREPLACE:
        return "unsupported_platform"
    opened_stage = _create_open_verified_dir(stage_dir)
    if opened_stage is None:
        return "write_failed"
    _, stage_fd, parent_fd = opened_stage
    try:
        if _write_duplicate_marker_at(stage_fd, token):
            try:
                # The marker must survive before the index can name this stage.
                # Persist both the marker entry and the stage's parent entry.
                os.fsync(stage_fd)
                os.fsync(parent_fd)
                return ""
            except OSError:
                pass
        try:
            os.unlink(_DUPLICATE_MARKER, dir_fd=stage_fd)
            os.fsync(stage_fd)
        except OSError:
            pass
        return "unsupported_platform" if not _CAN_PIN_DIR else "write_failed"
    finally:
        os.close(stage_fd)
        os.close(parent_fd)


def _remove_duplicate_marker(
    spec_dir: Path, token: str, expected_identity: tuple[int, int] | None = None
) -> None:
    """Remove only the matching marker from a descriptor-pinned directory."""
    opened_dir = _open_verified_dir(spec_dir)
    if opened_dir is None:
        return
    _real_dir, dir_fd = opened_dir
    try:
        if expected_identity is not None:
            info = os.fstat(dir_fd)
            if (info.st_dev, info.st_ino) != expected_identity:
                return
        if _duplicate_marker_matches_at(dir_fd, token):
            os.unlink(_DUPLICATE_MARKER, dir_fd=dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _duplicate_manifest_is_valid(documents: object) -> bool:
    """True when recovery metadata names only complete document digests."""
    if not isinstance(documents, dict) or not documents:
        return False
    for fname, digest in documents.items():
        if fname not in _EDITABLE_DOCS or not isinstance(digest, str) or len(digest) != 64:
            return False
        if _SHA256_RE.fullmatch(digest) is None:
            return False
    return True


def _duplicate_documents_match_at(dir_fd: int, documents: object) -> bool:
    """Validate the complete reserved payload through one pinned directory."""
    if not _duplicate_manifest_is_valid(documents):
        return False
    assert isinstance(documents, dict)
    for fname, digest in documents.items():
        fd = -1
        try:
            fd = os.open(
                fname,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=dir_fd,
            )
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > _MAX_SPEC_BYTES
            ):
                return False
            remaining = info.st_size + 1
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(fd, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) != info.st_size or hashlib.sha256(raw).hexdigest() != digest:
                return False
        except OSError:
            return False
        finally:
            if fd >= 0:
                os.close(fd)
    return True


def _clear_duplicate_stage_documents_at(dir_fd: int, token: str, documents: object) -> bool:
    """Remove marker-provenanced documents through their proven stage inode.

    The marker proves ownership of the directory, while the manifest digest
    proves ownership of each document. Recovery must preserve the reservation
    if a present document no longer matches; a project writer may have moved an
    unrelated file into an abandoned stage before recovery runs.

    The marker remains until the matching index transition is durably saved.
    The empty stage remains because directory removal cannot be bound to its
    already-open inode across the final pathname-based rmdir syscall.
    """
    if not _duplicate_manifest_is_valid(documents) or not _duplicate_marker_matches_at(
        dir_fd, token
    ):
        return False
    assert isinstance(documents, dict)
    opened: dict[str, tuple[int, int, int]] = {}
    owned_fds: list[int] = []
    try:
        for fname, digest in documents.items():
            fd = -1
            try:
                fd = os.open(
                    fname,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=dir_fd,
                )
            except FileNotFoundError:
                continue
            except OSError:
                return False
            owned_fds.append(fd)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > _MAX_SPEC_BYTES
            ):
                return False
            opened[fname] = (fd, info.st_dev, info.st_ino)
            remaining = info.st_size + 1
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(fd, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) != info.st_size or hashlib.sha256(raw).hexdigest() != digest:
                return False

        if not _duplicate_marker_matches_at(dir_fd, token):
            return False
        for fname, (_fd, expected_dev, expected_ino) in opened.items():
            current = os.stat(fname, dir_fd=dir_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (expected_dev, expected_ino):
                return False
        for fname in opened:
            os.unlink(fname, dir_fd=dir_fd)
        os.fsync(dir_fd)
        for fname in opened:
            try:
                os.stat(fname, dir_fd=dir_fd, follow_symlinks=False)
                return False
            except FileNotFoundError:
                continue
            except OSError:
                return False
        return _duplicate_marker_matches_at(dir_fd, token)
    except OSError:
        return False
    finally:
        for fd in owned_fds:
            try:
                os.close(fd)
            except OSError:
                pass


def _publish_staged_copy(stage_dir: Path, target_dir: Path) -> str:
    """Atomically publish a sibling staging directory without replacement."""
    if not _CAN_PUBLISH_DIR_NOREPLACE or stage_dir.parent != target_dir.parent:
        return "unsupported_platform"
    real_stage = _verified_spec_dir(stage_dir)
    real_parent = _safe_dir(str(stage_dir.parent))
    if real_stage is None or real_parent is None or real_stage.parent != real_parent:
        return "unsafe_dir"
    opened_parent = _open_verified_dir(real_parent)
    if opened_parent is None:
        return "unsafe_dir"
    _real_parent, parent_fd = opened_parent
    try:
        try:
            rename_noreplace(
                stage_dir.name,
                target_dir.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except FileExistsError:
            return "conflict"
        except NotImplementedError:
            return "unsupported_platform"
        except OSError:
            return "write_failed"
        try:
            os.fsync(parent_fd)
        except OSError:
            # The atomic rename already completed. Treating a filesystem that
            # rejects directory fsync as failure would orphan the published copy.
            logger.debug("duplicate parent directory fsync unavailable", exc_info=True)
        return ""
    finally:
        os.close(parent_fd)


def _publish_pinned_staged_copy(
    stage_dir: Path, target_dir: Path, stage_fd: int, token: str
) -> str:
    """Publish and prove the renamed name still identifies the pinned stage."""
    try:
        expected = os.fstat(stage_fd)
    except OSError:
        return "identity_mismatch"
    if not _duplicate_marker_matches_at(stage_fd, token):
        return "identity_mismatch"
    result = _publish_staged_copy(stage_dir, target_dir)
    if result:
        return result
    opened_target = _open_verified_dir(target_dir)
    if opened_target is None:
        return "identity_mismatch"
    _, target_fd = opened_target
    try:
        published = os.fstat(target_fd)
        if (published.st_dev, published.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ) or not _duplicate_marker_matches_at(target_fd, token):
            return "identity_mismatch"
        return ""
    except OSError:
        return "identity_mismatch"
    finally:
        os.close(target_fd)


_DUPLICATE_RECOVERY_ADOPT = "adopt"
_DUPLICATE_RECOVERY_DISCARD = "discard"
_DUPLICATE_RECOVERY_RELEASE = "release"
_DUPLICATE_RECOVERY_RETRY = "retry"


def _recover_abandoned_copy(name: str, meta: dict) -> tuple[str, Path | None]:
    """Resolve a duplicate and identify any marker removable after index save."""
    held = meta.get(_DUPLICATING)
    if not isinstance(held, dict):
        return _DUPLICATE_RECOVERY_RELEASE, None
    owner = held.get("owner")
    reserved_at = held.get("at")
    token = held.get("token")
    stage_raw = held.get("stage_dir")
    stage_dev = held.get("stage_dev")
    stage_ino = held.get("stage_ino")
    documents = held.get("documents")
    target_raw = meta.get("spec_dir")
    slot_key = meta.get("slot_key")
    if (
        not isinstance(owner, str)
        or not owner
        or not isinstance(reserved_at, (int, float))
        or not isinstance(token, str)
        or not token
        or not isinstance(stage_raw, str)
        or not stage_raw
        or type(stage_dev) is not int
        or stage_dev < 0
        or type(stage_ino) is not int
        or stage_ino < 0
        or not isinstance(target_raw, str)
        or not target_raw
        or not isinstance(slot_key, str)
        or not _owns_slot_key(name, slot_key)
        or not _duplicate_manifest_is_valid(documents)
    ):
        return _DUPLICATE_RECOVERY_RELEASE, None
    if _DUPLICATE_TOKEN_RE.fullmatch(token) is None:
        return _DUPLICATE_RECOVERY_RELEASE, None
    target_dir = Path(target_raw)
    stage_dir = Path(stage_raw)
    expected_stage = target_dir.parent / f".{name}.duplicate-{token}"
    if (
        not target_dir.is_absolute()
        or target_dir.name != name
        or stage_dir != expected_stage
        or stage_dir.parent != target_dir.parent
    ):
        return _DUPLICATE_RECOVERY_RELEASE, None
    # Before publication a genuine duplicate has no target directory. Any target
    # without our marker is user data (or a concurrent writer's winning create),
    # never transaction residue that recovery may delete from the index.
    if target_dir.exists():
        opened_target = _open_verified_dir(target_dir)
        if opened_target is not None:
            _, target_fd = opened_target
            try:
                target_info = os.fstat(target_fd)
                if (target_info.st_dev, target_info.st_ino) == (
                    stage_dev,
                    stage_ino,
                ) and _duplicate_marker_matches_at(target_fd, token):
                    return _DUPLICATE_RECOVERY_ADOPT, target_dir
            except OSError:
                pass
            finally:
                os.close(target_fd)
        opened_stage = _open_verified_dir(stage_dir)
        if opened_stage is not None:
            _, stage_fd = opened_stage
            try:
                stage_info = os.fstat(stage_fd)
                if (stage_info.st_dev, stage_info.st_ino) == (
                    stage_dev,
                    stage_ino,
                ) and _duplicate_marker_matches_at(stage_fd, token):
                    if not _clear_duplicate_stage_documents_at(stage_fd, token, documents):
                        return _DUPLICATE_RECOVERY_RETRY, None
                    return _DUPLICATE_RECOVERY_DISCARD, stage_dir
            except OSError:
                pass
            finally:
                os.close(stage_fd)
        # The recorded transaction inode is no longer reachable at either name.
        # Keep the reservation hidden: clearing it would adopt the unrelated
        # target after a crash in the post-rename identity-check window.
        return _DUPLICATE_RECOVERY_RETRY, None
    opened_stage = _open_verified_dir(stage_dir)
    if opened_stage is None:
        return _DUPLICATE_RECOVERY_RETRY, None
    _, stage_fd = opened_stage
    try:
        stage_info = os.fstat(stage_fd)
        if (stage_info.st_dev, stage_info.st_ino) != (
            stage_dev,
            stage_ino,
        ) or not _duplicate_marker_matches_at(stage_fd, token):
            return _DUPLICATE_RECOVERY_RETRY, None
        if not _duplicate_documents_match_at(stage_fd, documents):
            if not _clear_duplicate_stage_documents_at(stage_fd, token, documents):
                return _DUPLICATE_RECOVERY_RETRY, None
            return _DUPLICATE_RECOVERY_DISCARD, stage_dir
        publish_result = _publish_pinned_staged_copy(stage_dir, target_dir, stage_fd, token)
        if publish_result == "":
            return _DUPLICATE_RECOVERY_ADOPT, target_dir
        if publish_result == "identity_mismatch":
            # The still-open descriptor proves the renamed directory was not the
            # validated transaction. Never finalize the index around its files.
            return _DUPLICATE_RECOVERY_DISCARD, None
        if not _clear_duplicate_stage_documents_at(stage_fd, token, documents):
            return _DUPLICATE_RECOVERY_RETRY, None
        return _DUPLICATE_RECOVERY_DISCARD, stage_dir
    except OSError:
        return _DUPLICATE_RECOVERY_RETRY, None
    finally:
        os.close(stage_fd)


def _recover_abandoned_reservations() -> None:
    """Recover duplicate transactions once and persist their terminal state.

    BLOCKING -- first enabled use runs this through ``asyncio.to_thread``.
    The index lock serializes the filesystem recovery with every index mutation,
    and the cleaned index is saved in the same critical section so a later poll
    never repeats a rename/unlink transaction that startup already resolved.
    """
    with _INDEX_LOCK:
        index = _load_index()
        abandoned = [
            name
            for name, meta in index.items()
            if _DUPLICATING in meta and not _reservation_is_ours(meta, _DUPLICATING)
        ]
        if not abandoned:
            return
        recovered = 0
        released = 0
        markers_to_remove: list[tuple[Path, str, tuple[int, int]]] = []
        for name in abandoned:
            meta = index[name]
            held = meta.get(_DUPLICATING)
            marker_token = held.get("token", "") if isinstance(held, dict) else ""
            marker_identity = (
                (held.get("stage_dev"), held.get("stage_ino"))
                if isinstance(held, dict)
                else (None, None)
            )
            outcome, marker_dir = _recover_abandoned_copy(name, meta)
            if outcome == _DUPLICATE_RECOVERY_ADOPT:
                index[name].pop(_DUPLICATING, None)
                recovered += 1
            elif outcome == _DUPLICATE_RECOVERY_DISCARD:
                index.pop(name, None)
                released += 1
            elif outcome == _DUPLICATE_RECOVERY_RELEASE:
                # Malformed/unproven metadata is not authority to delete a real
                # spec record, its approvals, or its conversation linkage.
                index[name].pop(_DUPLICATING, None)
                released += 1
            else:
                # Keep both reservation and marker when cleanup cannot prove a
                # safe terminal state. A later process retries the transaction.
                continue
            if (
                marker_dir is not None
                and type(marker_identity[0]) is int
                and type(marker_identity[1]) is int
            ):
                markers_to_remove.append(
                    (
                        marker_dir,
                        str(marker_token),
                        (marker_identity[0], marker_identity[1]),
                    )
                )
        _save_index(index)
        _refresh_slot_keys(index)
        # Marker removal is deliberately after the durable index transition.
        # A crash from here can strand only a harmless marker in an empty stage
        # or committed target; it cannot create markerless transaction metadata.
        for marker_dir, marker_token, marker_identity in markers_to_remove:
            _remove_duplicate_marker(marker_dir, marker_token, marker_identity)
    logger.info(
        "spec index: recovered %d and released %d duplicate reservation(s) "
        "abandoned by an earlier process",
        recovered,
        released,
    )


async def _recover_abandoned_reservations_on_first_use() -> None:
    """Run recovery off-loop without making an abandoned copy disable the app."""
    try:
        await asyncio.to_thread(_recover_abandoned_reservations)
    except Exception:
        # A reservation remains hidden and keeps its name reserved when recovery
        # cannot prove a safe terminal state. Retry on the next gateway process,
        # rather than failing every poll or taking unrelated app routes down.
        logger.exception("spec index: abandoned duplicate recovery failed")


async def _ensure_duplicate_recovery(app: web.Application) -> None:
    """Recover once on first enabled use, after the gateway is already ready."""
    recovery = app[_DUPLICATE_RECOVERY_STATE]
    task = recovery["task"]
    if task is None:
        # Request handlers for one Application share an event loop. There is no
        # await between checking and publishing the task, so concurrent first
        # requests cannot start two filesystem transactions.
        task = asyncio.create_task(_recover_abandoned_reservations_on_first_use())
        recovery["task"] = task
    await task


def _write_and_publish_duplicate(
    stage_dir: Path,
    target_dir: Path,
    docs: dict[str, str | None],
    token: str,
    expected_stage_identity: tuple[int, int] | None = None,
) -> tuple[str, dict[str, tuple[int, int, int, int]]]:
    """Populate a hidden sibling directory, then atomically publish it. BLOCKING."""

    created: dict[str, tuple[int, int, int, int]] = {}
    if not _CAN_PUBLISH_DIR_NOREPLACE:
        return "unsupported_platform", created
    opened_stage = _open_verified_dir(stage_dir)
    if opened_stage is None:
        return "write_failed", created
    _, stage_fd = opened_stage
    try:
        stage_info = os.fstat(stage_fd)
        opened_identity = (stage_info.st_dev, stage_info.st_ino)
        if expected_stage_identity is None:
            expected_stage_identity = opened_identity
        elif opened_identity != expected_stage_identity:
            return "identity_mismatch", created
        if not _duplicate_marker_matches_at(stage_fd, token):
            return "identity_mismatch", created
    except OSError:
        return "identity_mismatch", created
    finally:
        os.close(stage_fd)
    for fname, text in docs.items():
        if text is None:
            continue
        result, identity = _create_spec_doc(stage_dir, fname, text, expected_stage_identity)
        if identity is not None:
            created[fname] = identity
        if result:
            return result, created
    opened_stage = _open_verified_dir(stage_dir)
    if opened_stage is None:
        return "write_failed", created
    _, stage_fd = opened_stage
    try:
        stage_info = os.fstat(stage_fd)
        if (
            stage_info.st_dev,
            stage_info.st_ino,
        ) != expected_stage_identity or not _duplicate_marker_matches_at(stage_fd, token):
            return "identity_mismatch", created
        # Each document inode is already fsynced. Persist their directory entries
        # before the atomic rename makes this directory visible at the target.
        os.fsync(stage_fd)
        return _publish_pinned_staged_copy(stage_dir, target_dir, stage_fd, token), created
    except OSError:
        return "write_failed", created
    finally:
        os.close(stage_fd)


def _write_stop_sentinel(spec_dir: Path) -> bool:
    """Write the STOP sentinel atomically, never following a symlink.

    The directory is PINNED with a non-following descriptor and both the temp
    create and the rename happen relative to it. Verifying the directory and then
    operating on PATHS left the same window the clear path had: an agent that
    swaps its verified directory for a symlink between the check and the open
    redirects both steps, so ANOTHER active spec receives the STOP file and halts.
    Pinning means the sentinel lands in the directory that was verified or not at
    all.

    ``os.replace`` swaps the link itself rather than writing through it, so a
    planted ``STOP`` symlink is destroyed instead of honoured.

    POSIX-only pinning (see ``_CAN_PIN_DIR``): Windows has neither ``O_NOFOLLOW``
    nor ``dir_fd``, so there it keeps the path-based form.

    BLOCKING -- call via ``asyncio.to_thread``. Every step touches the
    filesystem (``realpath``, ``is_sensitive_path``, ``open``, ``write``,
    ``close``, ``replace``), so on unresponsive storage this stalls whatever
    thread runs it; on the event loop that is the whole gateway.
    """
    real_dir = _verified_spec_dir(spec_dir)
    if real_dir is None:
        return False
    tmp_name = f".{_STOP_FILE}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    if _CAN_PIN_DIR:
        try:
            dir_fd = os.open(real_dir, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return False
        try:
            fd = os.open(tmp_name, flags, 0o600, dir_fd=dir_fd)
            try:
                os.write(fd, str(time.time()).encode())
            finally:
                os.close(fd)
            os.replace(tmp_name, _STOP_FILE, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            return True
        except OSError:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except OSError:
                pass  # nothing to clean up
            return False
        finally:
            os.close(dir_fd)
    # No pinning available (Windows): FAIL CLOSED rather than writing by path. The
    # agent runs inside the user's project and can swap this directory for a junction
    # between the verification above and the write, and a path-based write then lands
    # the STOP file in whatever the replacement points at -- halting an unrelated
    # spec's run. Pause does not depend on this file: removing the nudge loop and
    # cancelling the in-flight turn are the authoritative stops, and both are
    # in-process. The sentinel only guards a FUTURE nudge, so its absence narrows
    # defense in depth instead of leaving the run going.
    logger.warning(
        "stop sentinel unavailable on this platform (no dir_fd pinning); "
        "relying on loop removal and turn cancellation to halt %s",
        _redact(str(real_dir)),
    )
    return False


def _clear_stop_sentinel(spec_dir: Path) -> None:
    """Remove a stale STOP sentinel belonging to THIS spec.

    Refuses a spec_dir that no longer resolves to itself (see
    ``_verified_spec_dir``). Verification alone was not enough: between the check
    and the ``unlink`` the agent this app runs can replace the verified directory
    with a symlink, and a path-based unlink then resolves through the replacement
    and deletes a STOP file outside the spec. The directory is therefore PINNED
    with a non-following descriptor and the unlink is relative to it, so the
    delete lands in the directory that was verified or not at all.

    POSIX-only pinning: where ``dir_fd`` is unavailable (Windows) this does
    NOTHING and logs, because a path-based unlink can be redirected into another
    spec by a directory swapped under it.

    BLOCKING -- call via ``asyncio.to_thread`` (see ``_arm_stop_sentinel``).
    """
    real_dir = _verified_spec_dir(spec_dir)
    if real_dir is None:
        return
    if _CAN_PIN_DIR:
        try:
            dir_fd = os.open(real_dir, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return
        try:
            os.unlink(_STOP_FILE, dir_fd=dir_fd)
        except OSError:
            pass  # absent, or a directory in its place — nothing to clear
        finally:
            os.close(dir_fd)
        return
    # Same reasoning as _write_stop_sentinel: without pinning, a path-based unlink can
    # be redirected by a directory swapped underneath it, deleting another spec's STOP
    # file and letting THAT run resume. A stale sentinel of our own is the lesser
    # failure -- it makes this spec refuse to start until it is cleared, which is
    # visible and recoverable, rather than silently un-pausing someone else.
    logger.warning(
        "cannot clear the stop sentinel on this platform (no dir_fd pinning): %s",
        _redact(str(real_dir)),
    )


def _arm_stop_sentinel(spec_dir: Path) -> str:
    """Clear this spec's stale STOP sentinel and return the sentinel path.

    BLOCKING -- call via ``asyncio.to_thread``. Bundles the ``unlink`` with the
    path the autonudge arm needs so the handoff handler makes one thread hop
    instead of two filesystem round-trips on the event loop. Returns ``""`` when
    the spec dir does not verify, which the caller must treat as a refusal.
    """
    real_dir = _verified_spec_dir(spec_dir)
    if real_dir is None:
        return ""
    _clear_stop_sentinel(real_dir)
    return str(real_dir / _STOP_FILE)


def _write_stop_sentinel_for_spec(
    spec_dir: Path, name: str = "", expect_slot_key: str = ""
) -> bool:
    """``_write_stop_sentinel`` with the spec's identity pinned to the write.

    BLOCKING -- call via ``asyncio.to_thread``. The counterpart to the gate in
    ``_prepare_handoff``, for the opposite act: arming REMOVES a STOP, this one
    CREATES one, and both are destructive to whichever spec currently owns the
    directory. A same-name delete plus re-import between the caller's identity
    check and this write lands the STOP in the REPLACEMENT's directory, halting a
    run the user has only just started.

    Same critical-section reasoning, and the same safety argument, as
    ``_prepare_handoff``: identity and act inside one ``_INDEX_LOCK`` hold, in a
    worker thread, so the event loop never waits on it. Callers without an
    identity to pin (no *name* / *expect_slot_key*) still get the plain write --
    the gate cannot refuse what it cannot identify.
    """
    with _INDEX_LOCK:
        if name and expect_slot_key:
            current = _load_index().get(name) or {}
            if str(current.get("slot_key", "")) != expect_slot_key:
                return False
        return _write_stop_sentinel(spec_dir)


def _prepare_handoff(spec_dir: Path, name: str = "", expect_slot_key: str = "") -> tuple[bool, str]:
    """Everything the handoff endpoint needs off the filesystem, in one hop.

    BLOCKING -- call via ``asyncio.to_thread``. Returns ``(ready, sentinel
    path)``; ``ready`` is False both when ``tasks.md`` is missing AND when the
    spec dir fails verification, so a replaced-by-symlink directory cannot start
    a run (nor touch another spec's sentinel on the way).

    With *name* and *expect_slot_key*, the identity is re-checked under the index
    lock and the sentinel is armed WITHIN THE SAME critical section, and a
    mismatch refuses. Arming is destructive -- it removes the STOP that a Pause
    wrote -- so it must not happen for a spec this request no longer refers to: a
    stale same-name, same-path execute would otherwise clear a REPLACEMENT's stop
    and let the persisted loop resume after a restart. Gating the act itself is
    what covers a request carrying no client claim, which no claim comparison can
    refuse.

    The check and the act are ONE critical section rather than two statements,
    because a same-name delete plus re-import landing between them leaves the
    check passing for the spec that is already gone while the arm lands on its
    replacement -- correct ordering alone does not close that window, only
    holding the lock across both does.

    Holding ``_INDEX_LOCK`` across filesystem work is safe HERE specifically
    because this function is BLOCKING by contract and only ever runs in a worker
    thread, so the critical section cannot stall the event loop. The lock is a
    plain non-reentrant ``threading.Lock`` and nothing reachable from
    ``_arm_stop_sentinel`` re-acquires it, so the wider section cannot deadlock.
    Do NOT widen it further into anything that awaits or that touches the index.
    """
    with _INDEX_LOCK:
        if name and expect_slot_key:
            current = _load_index().get(name) or {}
            if str(current.get("slot_key", "")) != expect_slot_key:
                return False, ""
        sentinel = _arm_stop_sentinel(spec_dir)
    if not sentinel:
        return False, ""
    # Through _spec_file, not a bare is_file(): is_file() FOLLOWS a symlink, so a
    # planted tasks.md -> <somewhere else> satisfied the gate and the autonomous
    # run then edited the link target outside the spec directory. _spec_file
    # refuses a symlink, a realpath that escapes the spec dir, and a sensitive
    # target; the extra is_file() keeps the "not written yet" case honest.
    tasks = _spec_file(spec_dir, "tasks.md")
    if tasks is None or not tasks.is_file():
        return False, sentinel
    # Existence is not a plan. The prompt this gate arms tells the agent to work
    # through each UNCHECKED task in order, so a zero-byte or half-written
    # tasks.md gave the autonomous loop nothing to act on while still reading as
    # a finished Tasks phase. Read through _read_spec_text rather than by name:
    # it validates the descriptor it read, and the agent writes into this very
    # directory, so the inode can change after the is_file() above.
    text = _read_spec_text(spec_dir, "tasks.md")
    return bool(text and _has_open_task(text)), sentinel


def _derive_phase(spec_dir: Path) -> str:
    for phase, fname in _PHASE_FILES:
        if _spec_file(spec_dir, fname) is not None and (spec_dir / fname).is_file():
            return phase
    return "new"


def _read_spec_files(spec_dir: Path) -> tuple[dict, dict, list[dict]]:
    """Read the documents once, returning ``(files, docs, tasks)``.

    ``files`` is what the browser renders: the text with credentials REDACTED.
    ``docs`` carries the ON-DISK hash used to bind approvals to the version that
    was reviewed. ``tasks`` carries redacted labels but raw-text identity hashes.
    Documents remain read-only here because the agent and IDE write the same files
    without participating in a dashboard lock; no portable compare-and-swap can
    prevent a direct write between a hash check and replace.
    """
    files: dict[str, str | None] = {}
    docs: dict[str, dict] = {}
    tasks: list[dict] = []
    for _phase, fname in _PHASE_FILES:
        text = _read_spec_text(spec_dir, fname)
        if text is None:
            files[fname] = None
            continue
        files[fname] = _redact(text)
        docs[fname] = {"hash": _sha256_text(text)}
        if fname == "tasks.md":
            tasks = _parse_tasks(text)
    return files, docs, tasks


# ── git / worktree helpers ────────────────────────────────────────────────────


#: rc returned when git could not be executed at all (not installed, or the
#: sandbox refused the spawn). Distinct from git's own exit codes so a caller can
#: tell "not a repo" (rc 128) from "no git here".
#: How long to wait for a killed git process to actually exit before giving up
#: on the reap and logging it. SIGKILL is not negotiable, so this only ever
#: elapses when the process is stuck in an uninterruptible syscall.
_GIT_HALT_SECS = 5.0

_GIT_UNAVAILABLE = 127


def _prepare_git_spawn(argv: list[str]) -> tuple[list[str], Any, str | None]:
    """Build everything the sandboxed git spawn needs.

    BLOCKING -- call via ``asyncio.to_thread``. Returns
    ``(argv, env, cleanup_path)``. Still its own thread hop because
    ``sandboxed_spawn_argv`` probes the sandbox host and writes the scrubbed-env
    temp file; the resource limits are no longer built here, because
    ``create_subprocess_limited`` applies them after exec.
    """
    sandbox_argv, env, cleanup = sandboxed_spawn_argv(argv)
    return sandbox_argv, env, cleanup


async def _halt_git(proc: Any, subcommand: str) -> None:
    """Stop a git process this app spawned, and reap it.

    Awaiting ``communicate()`` is the only thing that ties the child's lifetime to
    the request. Drop that await -- gateway shutdown, a client disconnect, any
    cancellation -- and git keeps running to completion detached from the handler
    that asked for it. For a read-only subcommand that only wastes a process, but
    ``worktree add`` MUTATES the user's repository: the worktree and branch appear
    after the request they belonged to is gone, and nothing reports them.

    kill() first and unconditionally, because it is synchronous: whatever happens to
    this coroutine next, the mutation is already stopped. The reap is shielded for
    the reason the kill is not -- the usual trigger here IS cancellation, and an
    unshielded await would be cancelled at once, leaving behind the zombie it came
    to collect.
    """
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        return  # already gone; nothing to reap
    try:
        await asyncio.shield(asyncio.wait_for(proc.wait(), timeout=_GIT_HALT_SECS))
    except asyncio.TimeoutError:
        logger.warning("git %s did not exit after kill", subcommand)
    except ProcessLookupError:
        pass


async def _git(cwd: str, *args: str) -> tuple[int, str, str]:
    """Run a git command (argv exec, no shell) in *cwd*. Returns (rc, out, err).

    Routed through ``sandboxed_spawn_argv`` with a scrubbed env and the resource
    -limit preexec, mirroring ``git_coord._git``. The working directory here is
    caller-supplied (and the branch name derives from a spec name), so this is
    an agent-influenced spawn in the sense of the spawn-audit tripwire — it must
    stay routed rather than being added to the benign allowlist.

    Every invocation and every outcome is recorded in SEL through
    ``_audit_tool``. A process this app spawns on the user's repository must be
    reconstructable from the audit log: without it, a worktree create/remove left
    no tool-invocation trail at all, only the app-level ``spec_worktree_*``
    entries, which say nothing about what git actually ran or whether it failed.
    """
    subcommand = args[0] if args else ""
    # Off-loop because a critical audit is a synchronous write, and audit-or-deny:
    # git is only spawned once the record has actually landed.
    if not await asyncio.to_thread(_audit_tool, "invoked", subcommand, cwd, critical=True):
        # Fail closed: no audit record, no spawn. Callers already treat a non-zero
        # rc as "not a git repo", so this degrades the feature (no worktree, no
        # branch detection) instead of running an unaudited process.
        logger.warning("refusing to run git %s: invocation could not be audited", subcommand)
        return _GIT_UNAVAILABLE, "", "git unavailable: audit unavailable"
    try:
        # Off-loop: the sandbox backend probe can shell out (subprocess.run) the
        # first time it runs on a host, and it writes the scrubbed-env temp file.
        # Neither is the cheap in-memory call it looks like.
        argv, env, cleanup = await shielded_prepare_off_loop(
            functools.partial(_prepare_git_spawn, ["git", "-C", cwd, *args])
        )
    except Exception as exc:
        # Sandbox unavailable / argv build failure: report it, do not 500 the
        # caller. Every caller already treats a non-zero rc as "not a git repo".
        _audit_tool("error", subcommand, cwd, error=type(exc).__name__)
        return _GIT_UNAVAILABLE, "", f"git unavailable: {type(exc).__name__}"
    proc: Any = None
    try:
        # Limits are applied after exec by a shim, avoiding preexec_fn in the
        # multithreaded gateway process.
        proc = await create_subprocess_limited(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out, err = await proc.communicate()
    except FileNotFoundError:
        # No git on this host. Browsing a folder calls _repo_info, so letting
        # this propagate turned the project picker's first request into an HTTP
        # 500 on any machine without git installed — the app is usable without
        # it (the worktree option simply isn't offered), so degrade instead.
        # (the finally below removes the temp env file)
        _audit_tool("error", subcommand, cwd, error="FileNotFoundError")
        return _GIT_UNAVAILABLE, "", "git is not installed"
    except BaseException as exc:  # spawn failure, cancellation, timeout
        _audit_tool("error", subcommand, cwd, error=type(exc).__name__)
        await _halt_git(proc, subcommand)
        raise
    finally:
        if cleanup:
            # Off-loop too: same class as the probe above, and this one runs on
            # EVERY git call. Shielded so a cancelled turn still removes the
            # temp env file (it holds the scrubbed environment) instead of
            # leaking it into the user's temp dir.
            await asyncio.shield(asyncio.to_thread(_unlink_quietly, cleanup))
    rc = proc.returncode or 0
    _audit_tool("success" if rc == 0 else "failure", subcommand, cwd, rc=rc)
    return (
        rc,
        out.decode(errors="replace").strip(),
        err.decode(errors="replace").strip(),
    )


async def _repo_info(path: str) -> dict:
    """Probe *path*: is it inside a git repo? Return root + branch details."""
    rc, out, _ = await _git(path, "rev-parse", "--show-toplevel")
    if rc != 0 or not out:
        return {"is_git": False}
    root = out
    _, branch, _ = await _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    # Default base: origin/main, then the legacy default-branch name, then HEAD.
    # The legacy ref has to be spelled literally to resolve in a user's own repo
    # that still uses it, so the inclusive-language rule is suppressed here the
    # same way security.py suppresses it for the protected-branch patterns.
    base = ""
    for cand in ("origin/main", "origin/master"):  # wokeignore:rule=master
        rc2, _, _ = await _git(root, "rev-parse", "--verify", "--quiet", cand)
        if rc2 == 0:
            base = cand
            break
    return {"is_git": True, "root": root, "branch": branch, "default_base": base or branch}


def _unlink_quietly(path: str) -> None:
    """Remove a file, ignoring absence and errors.

    BLOCKING -- call via ``asyncio.to_thread``.
    """
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


async def _rollback_worktree_if_ours(
    name: str,
    *,
    was_ours: bool,
    repo_root: str,
    created_worktree: str,
    worktree_branch: str,
) -> bool:
    """Undo a created worktree ONLY while this request still owns the name.

    ``_remove_worktree`` is ``worktree remove --force`` plus ``branch -D``, so a
    rollback that fires after a concurrent delete + same-name recreate would
    discard the REPLACEMENT spec's uncommitted work and hard-delete its branch.

    ``was_ours`` is the identity-pinned index pop's own answer. A False pop means
    the name no longer refers to our create, and the worktree path is derived
    from the name (``<repo>-wt-<name>``), so it is not ours to remove either.
    Leaving it is the safe failure: an orphaned worktree is recoverable by hand,
    deleted work is not.

    Returns True when the worktree was actually removed.
    """
    if not created_worktree:
        return False
    if not was_ours:
        logger.warning(
            "spec %s: leaving worktree %s in place -- the index entry is no longer ours",
            name,
            created_worktree,
        )
        return False
    await _remove_worktree(repo_root, created_worktree, worktree_branch)
    return True


async def _remove_worktree(repo_root: str, worktree_path: str, branch: str = "") -> None:
    """Best-effort rollback of a worktree this request just created.

    Called only on a create path that already succeeded in making the worktree
    and then failed a later validation — without this the request 400s and
    leaves an orphaned worktree + branch behind for the user to clean up by
    hand. Prunes before deleting the branch, since a leftover registration
    keeps the branch checked-out from git's point of view. ``branch`` is passed
    in rather than derived: the worktree dir is ``<repo>-wt-<name>`` while the
    branch is ``spec/<name>``, so deriving one from the other is wrong.
    """
    if not repo_root or not worktree_path:
        return
    try:
        await _git(repo_root, "worktree", "remove", "--force", worktree_path)
        await _git(repo_root, "worktree", "prune")
        if branch:
            await _git(repo_root, "branch", "-D", branch)
    except Exception:  # pragma: no cover - rollback must never mask the real error
        logger.debug("worktree rollback failed for %s", worktree_path, exc_info=True)


async def _create_worktree(repo_root: str, spec_name: str) -> tuple[str, str] | str:
    """Create a dedicated worktree + branch for a spec off the repo's default base.

    Returns (worktree_path, branch) on success, or an error string. The worktree
    lands as a SIBLING of the repo (``<repo>-wt-<spec>``), branch ``spec/<name>``,
    mirroring the worktree-per-feature convention.
    """
    root = Path(repo_root)
    wt_path = root.parent / f"{root.name}-wt-{spec_name}"
    branch = f"spec/{spec_name}"
    # Off-loop: a stat against a caller-chosen repo root, which can sit on a
    # stalled network mount. It is the last filesystem call in this module that
    # still ran on the event loop -- every other one is inside a helper marked
    # BLOCKING and invoked through to_thread.
    if await asyncio.to_thread(wt_path.exists):
        return f"worktree path already exists: {wt_path}"
    info = await _repo_info(repo_root)
    base = info.get("default_base") or "HEAD"
    rc, _, err = await _git(repo_root, "worktree", "add", str(wt_path), "-b", branch, base)
    if rc != 0:
        return _redact(err.splitlines()[-1] if err else f"git worktree add failed (rc={rc})")
    return (str(wt_path), branch)


def _read_recent_projects() -> list[str]:
    """The dashboard's recent-projects list, filtered to existing directories.

    BLOCKING -- call via ``asyncio.to_thread``.
    """
    try:
        data = json.loads((config_dir() / "recent_projects.json").read_text())
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, str) and Path(p).is_dir()][:10]


def _discover_folder_specs(index: dict) -> bool:
    """Scan known project folders' ``.kiro/specs/`` for specs created outside
    the app (Kiro CLI/IDE, other tools) and auto-register them in the index.

    Candidate roots are the working dirs the app already knows. A directory
    counts as a spec when it contains any of the three Kiro markdown files.
    Returns True when new entries were added (caller persists).
    """
    roots: set[str] = {str(meta.get("working_dir", "")) for meta in index.values()}
    known_dirs: set[str] = {str(meta.get("spec_dir", "")) for meta in index.values()}
    # A directory the user deleted is not a discovery candidate. Without this, a
    # delete that (by design) leaves the .md files in place was undone by the very
    # next list scan whenever a sibling spec kept the project root indexed.
    known_dirs |= set(_load_deleted())
    added = False
    for root in filter(None, roots):
        # The indexed working_dir is app state on disk, so it is untrusted (same
        # reasoning as _ensure_worker_slot): a tampered entry pointing at a
        # credential tree would otherwise be statted and ENUMERATED here, outside
        # the sensitive-path gate, and any spec-shaped directory inside it would be
        # adopted into the index. Validate the derived scan root itself, so a
        # symlinked `.kiro/specs` cannot redirect the walk either.
        safe_root = _safe_dir(root)
        if safe_root is None:
            logger.warning("skipping discovery for unusable indexed root %s", _redact(root))
            continue
        specs_base = _safe_dir(str(safe_root / ".kiro" / "specs"))
        if specs_base is None:
            continue
        try:
            children = sorted(specs_base.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or str(child) in known_dirs:
                continue
            if not any((child / f).is_file() for f in ("requirements.md", "design.md", "tasks.md")):
                continue
            name = child.name
            # _usable_name for the same reason as create: discovery WRITES
            # index[name] below, so admitting on the grammar alone would re-add an
            # entry that the next load drops, rediscovering it on every call.
            if name in index or not _usable_name(name):
                continue
            try:
                created = child.stat().st_mtime
            except OSError:
                created = time.time()
            index[name] = {
                "working_dir": root,
                "spec_dir": str(child),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": _new_slot_key(name),
                "worktree_branch": "",
                "repo_root": "",
                "discovered": True,
                "created_at": created,
                "updated_at": created,
            }
            known_dirs.add(str(child))
            added = True
    return added


def _prepare_spec_dir(
    working_dir: str,
    safe_wd: Path,
    name: str,
    import_existing: bool,
    *,
    create: bool = True,
    expected_dir: Path | None = None,
) -> tuple[Path, str]:
    """Resolve + validate + create the spec directory. BLOCKING -- one hop.

    Returns ``(spec_dir, refusal)``; ``refusal`` is ``""`` on success, else
    ``"escape"``, ``"moved"``, ``"existing:<files>"`` or ``"mkdir:<reason>"``.
    """
    spec_dir = _resolve_spec_dir(working_dir, name)
    # Duplication reserves this exact path before any files are copied. Refuse
    # if a concurrent settings change resolves the destination elsewhere.
    if expected_dir is not None and os.path.normcase(str(spec_dir)) != os.path.normcase(
        str(expected_dir)
    ):
        return spec_dir, "moved"
    # The spec dir must land under its declared root -- either the settings
    # base_path or the validated working dir (which is the WORKTREE when one was
    # just created). _NAME_RE already forbids '.' and '/', so this can only fail
    # if one of those invariants regresses; assert it here rather than trusting a
    # regex defined elsewhere.
    settings_base = _safe_dir_optional(_load_settings().get("base_path", ""))
    expected_root = settings_base if settings_base else safe_wd
    if not _contained(spec_dir, expected_root):
        return spec_dir, "escape"
    # Containment alone is not enough: it only says "under the declared root".
    # If that root is (or grows) a symlink into a credential tree, BOTH paths
    # resolve through it, so the containment test passes while the spec files
    # would be created inside the credential directory. Re-validate the RESOLVED
    # destination through the same chokepoint every caller-supplied directory
    # goes through -- must_exist=False, because the spec dir is what we are about
    # to create, and that variant also tests the nearest existing ancestor.
    if _safe_dir_optional(str(spec_dir)) is None:
        return spec_dir, "escape"
    # Refuse to adopt-by-overwrite: a spec dir that already holds Kiro markdown
    # was created by the IDE/CLI or another tool, and handing it to an agent
    # would let it rewrite files the index never knew about. Opting in is
    # explicit.
    if not import_existing:
        existing = [f for _p, f in _PHASE_FILES if (spec_dir / f).is_file()]
        if existing:
            return spec_dir, "existing:" + ", ".join(sorted(existing))
    if create:
        try:
            spec_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return spec_dir, f"mkdir:{exc}"
    return spec_dir, ""


def _load_index_with_discovery() -> tuple[dict, dict[str, str]]:
    """Load the index, fold in specs found on disk, and derive every phase --
    all in ONE thread hop, under the index lock.

    BLOCKING -- call via ``asyncio.to_thread``. The list endpoint is polled every
    15s, while discovery walks every known project root and phase derivation stats
    up to three files per spec. Returns ``(index, {name: phase})``.
    """
    with _INDEX_LOCK:
        index = _load_index()
        if _discover_folder_specs(index):
            _save_index(index)
    phases = {name: _derive_phase(Path(m.get("spec_dir", ""))) for name, m in index.items()}
    return index, phases
