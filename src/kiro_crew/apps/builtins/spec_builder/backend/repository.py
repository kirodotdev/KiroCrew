"""Spec Builder persistence and safe filesystem; the ``repository_*`` siblings build on them."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.platform_compat import RENAME_NOREPLACE_AVAILABLE, rename_noreplace  # noqa: F401

from .parsers import (
    _SLOT_KEY_RE,
    _decision_key,
    _entry_is_usable,
    _owns_slot_key,
    _redact,
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
    from kiro_crew.sandbox import (  # noqa: F401
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
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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
        data = json.loads(_deleted_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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
    # index does not authenticate that mapping, but legacy migration must also not
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
        data = json.loads(_index_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        clean: dict = {}
        _refresh_slot_keys(clean)
        return clean, True
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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
    if the spec is gone -- which the caller MUST treat as "deleted
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
    """Authenticated creation keys that no index row represents."""
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


def _read_recent_projects() -> list[str]:
    """The dashboard's recent-projects list, filtered to existing directories.

    BLOCKING -- call via ``asyncio.to_thread``.
    """
    try:
        data = json.loads((config_dir() / "recent_projects.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, str) and Path(p).is_dir()][:10]
