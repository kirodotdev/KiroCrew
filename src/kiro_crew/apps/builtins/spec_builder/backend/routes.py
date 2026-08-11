"""Spec Builder — builtin backend routes.

Registered at gateway startup by ``dashboard/server.py``'s builtin-route loop
(``for name in BUILTIN_NAMES: _mod.register_routes(app)``), via the app's
``backend.routes`` manifest field (``"backend.routes:register_routes"``) — the
same in-process contract every other builtin app uses (see
``issue_radar``/``code_review_sage``). Handlers register on the gateway's
aiohttp ``Application`` with full ``/api/apps/spec-builder/*`` paths and reach
gateway state via ``request.app['state']``.

Responsibilities:
  * Spec CRUD backed by an app-owned index + the Kiro-standard markdown files.
  * A per-spec agent slot (the "Spec agent") the UI chats with IN-APP: user turns
    are relayed into the slot and the transcript is read back for the embedded chat.
  * Configurable storage: specs default to ``<working_dir>/.kiro/specs/<name>/``
    (portable to Kiro IDE/CLI); an optional absolute base-path override is honored.
  * Handoff: inject an execution instruction into the spec's session and arm an
    autonudge loop so it works through ``tasks.md`` autonomously.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from functools import wraps
from pathlib import Path
from typing import Any, Callable, NamedTuple

from aiohttp import web

from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async

try:
    from kiro_crew.security import (
        is_sensitive_path,
        redact_credentials,
        redact_exfiltration_urls,
    )

    _HAS_SECURITY = True
except Exception:  # pragma: no cover - security module always present in prod
    _HAS_SECURITY = False

    def is_sensitive_path(path: str) -> bool:  # type: ignore[misc]
        """Fail CLOSED when the security module is unavailable.

        Every caller uses this to decide whether a path may be read, written or
        browsed. If the module can't be imported we cannot make that judgement,
        so treat every path as sensitive rather than waving them all through.
        """
        return True

try:
    from kiro_crew.sel import sel
except Exception:  # pragma: no cover
    sel = None  # type: ignore[assignment]

# Gateway internals this app relays through. Module scope per the
# ``top-level-imports`` rule -- a function-local import hides a dependency and
# makes a test's mock patch target the wrong namespace. Guarded because a
# builtin must not break the gateway import if an internal moves: the callers
# fall back (a queued turn, a skipped history read) rather than raising.
try:
    from kiro_crew.constants import CHAT_TURN_TIMEOUT
except Exception:  # pragma: no cover - constant always present in prod
    CHAT_TURN_TIMEOUT = 1800  # type: ignore[assignment]

try:
    from kiro_crew.hooks import safe_read_file_bytes_nolink
except Exception:  # pragma: no cover - hooks always present in prod
    safe_read_file_bytes_nolink = None  # type: ignore[assignment]

try:
    from kiro_crew.sandbox import create_subprocess_limited, sandboxed_spawn_argv
except Exception:  # pragma: no cover - sandbox always present in prod
    create_subprocess_limited = None  # type: ignore[assignment]
    sandboxed_spawn_argv = None  # type: ignore[assignment]

try:
    from kiro_crew.autonudge import get_instance as _autonudge_instance
    from kiro_crew.autonudge_authz import authorize_and_add_nudge
except Exception:  # pragma: no cover - autonudge always present in prod
    _autonudge_instance = None  # type: ignore[assignment]
    authorize_and_add_nudge = None  # type: ignore[assignment]

# circular import: kiro_crew.dashboard.server imports the builtins to register
# their routes, so importing dashboard submodules at module scope here closes
# the cycle. Deferred to call time inside _dispatch_turn / _serialize_messages /
# _teardown_worker_slot, which is the documented exception to top-level-imports.

logger = logging.getLogger("kirocrew.app.spec-builder")

APP_NAME = "spec-builder"

#: Override hooks, None = resolve live. `config_dir()` reads KIROCREW_HOME on
#: every call, so binding these at import time froze whichever home was active
#: when this module first loaded — which breaks pod isolation, the lazy
#: ~/.kirocrew -> ~/.kiro/crew migration, and test isolation (the autouse
#: fixture runs after collection has already imported this module, so it cannot
#: reach a frozen constant). See test/test_lazy_data_home_paths.py and #874.
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


_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_VALID_TYPES = ("feature", "bug", "quick")

#: Every status this app can be in. index.json is agent-writable, so the stored
#: value is untrusted: an unrecognised one is reported as "planning" rather than
#: echoed, which both closes a credential-egress path and is the truth (a spec with
#: no live loop IS planning). Allowlisting beats redacting here because the set is
#: small and closed, so there is nothing to sanitise -- only to recognise.
_VALID_STATUSES = ("planning", "executing")


def _known_status(value: object) -> str:
    """The stored status if this app recognises it, else "planning"."""
    text = str(value or "")
    return text if text in _VALID_STATUSES else "planning"


_STOP_FILE = "STOP"

# The autonomous nudge loop is capped rather than infinite. There is no trust
# TTL any more because this app no longer grants trust — see the create handler.
_EXEC_MAX_CYCLES = 60

#: Cap on a single spec document served to the browser. These are markdown
#: files; an oversized one should not be inlined into a JSON response.
_MAX_SPEC_BYTES = 1 << 20


# ── enablement gate ──────────────────────────────────────────────────────────


def _require_enabled(handler):
    """Deny requests when Spec Builder is disabled (deny-by-default). Routes are
    registered once at gateway startup, so a default-disabled / opt-in app would
    otherwise stay callable. ``is_app_enabled`` is a synchronous installed.json
    read, so it runs off the event loop (same as issue_radar)."""

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response({"code": "app_disabled", "error": "spec-builder is disabled"}, status=403)
        return await handler(request)

    return _wrapped


# ── redaction ──────────────────────────────────────────────────────────────


#: Served in place of any text this app cannot scrub. Everything that flows
#: through _redact is agent- or user-authored (spec documents, transcripts,
#: agent-written state), so it can contain credentials by construction.
_UNSCRUBBABLE = "[unavailable: redaction is not available]"


def _redact(text: str) -> str:
    """Scrub credentials + exfiltration URLs from agent/user text before it
    leaves this backend (transcript, file contents, spec metadata).

    Fails CLOSED. If the security module could not be imported there is no way
    to scrub, and every caller feeds this untrusted content on its way to the
    browser -- so withhold the text rather than serving it raw. The same
    reasoning as the fail-closed ``is_sensitive_path`` fallback above: when the
    judgement cannot be made, refuse instead of waving it through.
    """
    if not isinstance(text, str) or not text:
        return text or ""
    if not _HAS_SECURITY:
        return _UNSCRUBBABLE
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


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


def _load_settings() -> dict:
    """Read settings, treating the file's SHAPE and its FIELDS as untrusted.

    A hand-edited (or agent-edited) ``settings.json`` holding a list, a string or
    ``null`` would otherwise reach ``.get()`` in the handlers and 500 the endpoint.
    Anything that is not an object is the same as "no settings".

    Validating only the OUTER shape was not enough: ``{"base_path": []}`` is a
    dict, so it passed, and every reader then called ``.strip()`` on a list —
    500ing spec creation and the settings read. The field is normalized here, at
    the single read chokepoint, so no caller has to re-check its type.
    """
    try:
        data = json.loads(_settings_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {"base_path": ""}
    if not isinstance(data, dict):
        return {"base_path": ""}
    if not isinstance(data.get("base_path"), str):
        # Copy rather than mutate: the parsed object is this function's own, but
        # returning a normalized view keeps the rule local to the chokepoint.
        data = {**data, "base_path": ""}
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
#: off first, and a fallen-off directory becomes discoverable again (the same
#: outcome as before this file existed).
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
    means the spec may reappear in the list, which is the pre-existing behaviour,
    not data loss -- so it must not fail the delete that already committed.
    """
    if not spec_dir:
        return
    try:
        # Read and write under the lock: two concurrent deletes both read the
        # pre-existing list, and the second write dropped the first spec's
        # tombstone -- so that spec was rediscovered and reappeared in the list
        # after the user deleted it.
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

    Called from BOTH chokepoints -- every read and every write. Read-only was not
    enough: a create commits through ``_mutate_index``, whose internal re-read
    rebuilt this map from the PRE-insert snapshot and so discarded the key the
    create had just minted. Everything that resolved a slot afterwards (the seed
    turn, the embedded chat, teardown) fell back to the legacy name-derived key
    while the index held the unique one, splitting one spec across two slots.

    Whole-dict replacement rather than in-place mutation: both chokepoints run on
    worker threads, and swapping one reference is atomic where an update is not.
    """
    global _SLOT_KEYS
    _SLOT_KEYS = {
        k: v["slot_key"]
        for k, v in index.items()
        if isinstance(v, dict)
        and isinstance(v.get("slot_key"), str)
        and _owns_slot_key(k, v["slot_key"])
    }


def _load_index() -> dict:
    """Read the index, discarding entries whose shape is wrong.

    The top-level object was already guarded, then entries that were not objects.
    Neither was enough: ``{"demo": {}}`` is a dict, so it survived, and handlers
    that index the required fields directly (``meta["spec_dir"]``) then raised
    KeyError and 500ed the request. An entry is only usable if it carries both
    identity fields as non-empty strings, so that is the bar here -- at the single
    read chokepoint, rather than every handler re-checking.

    A malformed entry is unusable either way, so drop it rather than serve a crash
    -- the spec's files stay on disk and rediscovery can re-add it.

    Delete reservations left by a process that is gone are dropped here too. The
    reservation is only meaningful while the request holding it runs, so one that
    outlived its process is not protecting anything -- it is hiding a spec the
    user still has and reserving its name against a re-create, with nothing left
    to release it. Clearing it in the returned copy needs no write: this is the
    read half of ``_mutate_index``, so the next mutation persists the cleanup,
    and until then the entry is simply visible again. A reservation this process
    still owns is left strictly alone, which is what keeps an in-flight delete's
    own concurrent reads from cancelling its reservation underneath it.
    """
    try:
        data = json.loads(_index_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    clean = {
        k: v
        for k, v in data.items()
        if isinstance(k, str) and _usable_name(k) and isinstance(v, dict) and _entry_is_usable(v)
    }
    if len(clean) != len(data):
        logger.warning("spec index had %d malformed entries — ignoring them",
                       len(data) - len(clean))
    stale = [k for k, v in clean.items() if _DELETING in v and not _reservation_is_ours(v)]
    for k in stale:
        clean[k].pop(_DELETING, None)
    if stale:
        logger.info(
            "spec index: released %d delete reservation(s) abandoned by an earlier process",
            len(stale),
        )
    _refresh_slot_keys(clean)
    return clean


def _usable_name(name: str) -> bool:
    """True when this index KEY can be served as a spec name.

    Two reasons an entry is dropped rather than repaired. The key must satisfy the
    same grammar `create` enforces, because it becomes a slot key and a session
    filename downstream. And it must survive `_redact` unchanged: index.json is
    agent-writable, so a credential can be parked in the KEY, and `GET /specs`
    returns the key as `"name"`. Scrubbing it would produce a name that no longer
    matches the directory the entry points at, so the entry goes instead.
    """
    return _valid_name(name) and _redact(name) == name


def _entry_is_usable(meta: dict) -> bool:
    """True when an index entry carries the one field handlers dereference.

    ``spec_dir`` only. Handlers index it directly (``meta["spec_dir"]``), which is
    what turned a shapeless entry into a 500. ``working_dir`` is deliberately NOT
    required here: it is re-validated through ``_safe_dir`` at the slot chokepoint,
    which refuses a missing one outright rather than running the spec unscoped. So
    an entry without it still lists and reads -- it just cannot be given a worker.
    """
    return isinstance(meta.get("spec_dir"), str) and bool(meta["spec_dir"].strip())


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


async def _mutate_index(mutate: Callable[[dict], bool]) -> bool:
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
    """

    def _apply() -> bool:
        with _INDEX_LOCK:
            index = _load_index()
            if not mutate(index):
                return False
            _save_index(index)
            return True

    return await asyncio.to_thread(_apply)


#: Set on an entry whose delete is mid-flight. The entry stays in the index so its
#: NAME stays reserved: a rollback then restores the original entry (and its
#: per-creation slot key, which only that name may own), and a same-name create
#: cannot slip into the window. Hidden from the list while set.
_DELETING = "deleting"

#: Identity of THIS gateway process, stamped into a delete reservation so
#: ``_load_index`` can tell a reservation this process still owns from one left
#: behind by a process that is gone.
#:
#: A reservation is only correct while the request holding it is alive. It is
#: written before the teardown and removed after, so a crash (or any hard exit)
#: in that window persists it forever: the entry stays hidden from the list and
#: its name stays reserved, with no request left to release it. The PID is here
#: for diagnostics; the uuid4 is what makes the comparison sound, since PIDs are
#: reused across boots and a recycled PID would otherwise read as still-ours.
_PROCESS_ID = f"{os.getpid()}:{uuid.uuid4().hex}"


def _reservation_is_ours(meta: dict) -> bool:
    """True when a delete reservation belongs to a request in THIS process.

    A pre-existing reservation from an older build stores a bare timestamp rather
    than a mapping; it has no owner, so it reads as foreign -- which is the right
    answer, because this process demonstrably did not write it.
    """
    held = meta.get(_DELETING)
    return isinstance(held, dict) and held.get("owner") == _PROCESS_ID


async def _mark_deleting(name: str, *, expect_spec_dir: str, expect_slot_key: str) -> bool:
    """Reserve *name* for a delete in flight. Identity-pinned like every mutation."""

    def _apply(index: dict) -> bool:
        meta = index.get(name)
        if meta is None or str(meta.get("spec_dir", "")) != expect_spec_dir:
            return False
        actual_key = str(meta.get("slot_key", ""))
        if expect_slot_key and actual_key and actual_key != expect_slot_key:
            return False
        meta[_DELETING] = {"owner": _PROCESS_ID, "at": time.time()}
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
    The marker used to be honoured only by the list filter, so a message landing
    mid-delete stamped the doomed entry, got a non-None return -- which every
    caller reads as "the spec is live" -- and dispatched a turn into a slot the
    delete had already captured past. The agent then kept editing the user's
    files after the DELETE returned 200. Refusing here covers every mutation at
    once instead of asking each caller to remember the marker.
    """
    fresh: dict = {}

    def _apply(index: dict) -> bool:
        meta = index.get(name)
        if meta is None:
            return False
        if meta.get(_DELETING):
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

    Previously only the browse endpoint applied the sensitivity test, so a
    direct create call could name e.g. ``~/.ssh`` as its working_dir and get a
    spec tree — and an agent with that cwd — inside it.
    """
    if not raw or not raw.strip():
        return None
    expanded = os.path.expanduser(raw.strip())
    # Absoluteness is tested on the EXPANDED INPUT, before realpath. realpath
    # resolves a relative value against the gateway's own cwd and always returns
    # an absolute path, so testing it afterwards can never fail -- the guarantee
    # this function documents was not actually enforced. It matters because
    # index.json is agent-writable (see _load_index): a `working_dir` of "."
    # normalized to the gateway's checkout, and the spec's worktree and its agent
    # were then pointed at it.
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
    and os.open in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
)

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


#: A slot key is a history-file identity: it becomes a session filename and flows
#: into core's session-key parsing, so a persisted one is validated before use.
_SLOT_KEY_RE = re.compile(r"^spec-builder-[A-Za-z0-9_-]{1,96}$")

#: A per-creation suffix: eight lowercase hex, as minted by _new_slot_key.
_SLOT_SUFFIX_RE = re.compile(r"^[0-9a-f]{8}$")


def _owns_slot_key(name: str, key: str) -> bool:
    """True when *key* is a slot key THIS spec may claim.

    The grammar alone was not enough. index.json is agent-writable, so an entry
    could carry another spec's perfectly valid key -- and `_ensure_worker_slot`
    would then adopt that spec's live session, delivering this spec's messages and
    approval cards into the other conversation. Ownership is therefore structural:
    the key must encode the indexed name, either as the per-creation
    ``spec-builder-<name>-<8hex>`` or the legacy name-derived
    ``spec-builder-<name>`` (kept so specs created before per-creation keys keep
    the transcript they already have).
    """
    if not _valid_name(name) or not _SLOT_KEY_RE.match(key):
        return False
    legacy = f"spec-builder-{name}"
    if key == legacy:
        return True
    prefix = legacy + "-"
    return key.startswith(prefix) and bool(_SLOT_SUFFIX_RE.match(key[len(prefix):]))


#: name -> persisted slot key, rebuilt from every index read (see _load_index).
#: Replaced WHOLESALE rather than mutated: _load_index runs in worker threads, and
#: swapping one dict reference is atomic where an in-place update is not.
_SLOT_KEYS: dict[str, str] = {}


def _slot_key(name: str) -> str:
    """This spec's chat-slot key.

    Prefers the key PERSISTED when the spec was created. Deriving it from the name
    alone made two different specs that happened to share a name share one history
    file: deleting a spec and recreating the name appended the new conversation to
    the old one's archive, and a restart rehydrated both interleaved. A per-creation
    key keeps each spec's transcript its own file for good.

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


_PHASE_FILES = [("tasks", "tasks.md"), ("design", "design.md"), ("requirements", "requirements.md")]


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

    Reads through ``safe_read_file_bytes_nolink``, which opens with
    ``O_NOFOLLOW`` FIRST and then validates the DESCRIPTOR (``fstat`` for
    regular-file + link count, and the fd's real path against ``within_root``
    and the sensitive-path set). That closes a genuine TOCTOU: the previous
    shape validated the path with ``_spec_file`` and then called
    ``p.read_text()`` by name, so the agent — which writes into this very
    directory — could swap ``requirements.md`` for a symlink or hardlink to a
    credential file in the window between the check and the open, during the
    UI's 2.5s poll. The inode validated is now exactly the inode read.

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


def _collect_spec_documents(spec_dir: Path) -> tuple[str, dict, dict | None]:
    """Gather everything the detail endpoint needs off the filesystem.

    BLOCKING — call via ``asyncio.to_thread``. Bundled into one function so the
    detail handler makes a single thread hop instead of three, and so no future
    edit can reintroduce an inline read: derive the phase, read the three spec
    documents, and read + normalize the agent-authored state file.
    """
    phase = _derive_phase(spec_dir)
    files = _read_spec_files(spec_dir)
    state: dict | None = None
    raw_text = _read_spec_text(spec_dir, ".spec-state.json")
    if raw_text is not None:
        try:
            state = _normalize_spec_state(json.loads(raw_text))
        except json.JSONDecodeError:
            state = None
    return phase, files, state


def _verified_spec_dir(spec_dir: Path) -> Path | None:
    """Return *spec_dir* only if it is still EXACTLY itself, else ``None``.

    Fails closed when the indexed path (or any component of it) is a symlink,
    i.e. when ``realpath`` disagrees with the path the index recorded. Every
    stored spec_dir is written fully resolved (``_safe_dir`` + ``_resolve_spec_dir``
    both realpath/resolve), so a disagreement means the directory was REPLACED
    after indexing.

    Why this matters: the sentinel helpers used to operate on
    ``realpath(spec_dir)``, so an agent that swapped its own spec directory for a
    symlink to a PAUSED spec's directory could make the handoff endpoint delete
    that other spec's STOP file and resume a run the user had stopped. Following
    the link was the bug; refusing it is the fix.
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
            dir_fd = os.open(
                real_dir, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            )
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
            dir_fd = os.open(
                real_dir, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            )
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


def _write_stop_sentinel_for_spec(spec_dir: Path, name: str = "", expect_slot_key: str = "") -> bool:
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


def _prepare_handoff(
    spec_dir: Path, name: str = "", expect_slot_key: str = ""
) -> tuple[bool, str]:
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
    ready = tasks is not None and tasks.is_file()
    return ready, sentinel


async def _restore_worker_transcript(state: Any, name: str, *, adopt_closed: bool) -> None:
    """Bring this spec's persisted conversation back into a cold worker slot.

    Slots are in-memory: a gateway restart (or the idle-slot cleanup that
    archives a quiet session with ``closed=True``) drops the worker's chat while
    the transcript stays on disk under the same key. Without this, the app's own
    read endpoints materialized an EMPTY slot on the first poll -- which also
    defeated the user's manual escape hatch, because core's resume returns early
    when a slot already exists.

    ``adopt_closed`` is the CALLER's decision, not a constant. For a spec already
    in the index it is True: the worker is not a tab the user closed, its lifecycle
    belongs to the spec, and idle-slot cleanup marks it closed on idleness alone.
    For a spec being CREATED it must be False -- a delete leaves the archived
    transcript on disk under a key derived from the name, so creating a new spec
    with a previously used name would hand the fresh agent the deleted spec's
    conversation.

    Best-effort by design. A missing, malformed or foreign transcript must leave
    the app working: the caller falls through to creating a fresh slot, and the
    ownership check it applies afterwards is what keeps a foreign transcript from
    being adopted.
    """
    try:
        restored = await rehydrate_slot_from_history_async(
            state, _slot_key(name), adopt_closed=adopt_closed
        )
    except Exception:
        logger.warning("spec %s: restoring the worker transcript failed", name, exc_info=True)
        return
    if restored is not None:
        _audit("spec_transcript_restored", name)


def _slot_identity_moved(name: str, slot_key: str) -> bool:
    """True when ``name`` no longer resolves to the key this request captured.

    ``_slot_key`` reads the module-global ``_SLOT_KEYS``, which a delete +
    same-name recreate rewrites to a fresh per-creation key. Any resolution taken
    AFTER an await can therefore name a different spec than the one the request
    began with, so the captured key is the identity and this is the check that it
    still holds. A moved mapping means our spec was replaced while we waited: the
    request must touch nothing rather than adopt the replacement's slot and stamp
    its own project onto it.
    """
    if _slot_key(name) == slot_key:
        return False
    _audit("spec_slot_replaced_midflight", name, outcome="denied")
    logger.warning(
        "spec %s was replaced while its slot was being acquired — refusing the stale request",
        name,
    )
    return True


async def _ensure_worker_slot(
    state: Any, name: str, meta: dict, *, adopt_closed: bool = True
) -> Any:
    """Materialize this spec's worker slot, SCOPED, and return it.

    The single place a spec slot comes into existence. It exists because
    ``get_or_create_slot`` only stamps ``app`` on NEWLY created slots, and
    because a slot created by any OTHER path is unscoped: a spec discovered on
    disk (created by the Kiro CLI/IDE) has no slot until something makes one,
    and if the embedded chat's ``POST /api/chat`` got there first the slot came
    up with no ``_app`` (so it surfaced in the main sidebar) and no ``project``
    (so approved tools ran from the gateway's own working directory instead of
    the user's project). Creating it HERE, from the indexed metadata, means the
    first thing that touches a spec's slot always scopes it.

    Refuses a slot that ANOTHER app already owns. ``get_or_create_slot`` keys off
    the name, so a foreign app holding ``spec-builder-<name>`` would otherwise be
    silently re-owned here -- its ``_app`` overwritten and its ``project``
    repointed at our spec's directory, taking the slot (and its transcript) away
    from the app that created it. Mirrors the ownership check
    ``_teardown_worker_slot`` already applies before deleting a slot.
    """
    if state is None:
        return None
    # The NAME is untrusted here for the same reason the indexed working_dir is:
    # handlers reach this with a key read back from index.json, which is app state
    # on disk that the agent this app runs can be talked into rewriting. From here
    # the name becomes a slot key and then a history key, so an unbounded value
    # would flow into core's session-key parsing (CodeQL flagged exactly that
    # path once this function started resolving transcripts). Re-assert the same
    # admission predicate creation and discovery enforce (_usable_name, which is
    # the grammar plus redaction-stability -- see _load_index).
    if not _usable_name(name):
        _audit("spec_slot_name_denied", _redact(name[:64]), outcome="denied")
        logger.warning("refusing a spec slot for a name that fails the grammar")
        return None
    # Resolved ONCE, before any await below. Recomputing it afterwards let a
    # concurrent delete + same-name recreate swap the identity mid-flight (see
    # _slot_identity_moved), so this local IS the slot identity from here on.
    slot_key = _slot_key(name)
    existing = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
    if existing is None:
        # Cold slot. Slots live in memory, so a gateway restart drops the
        # worker's conversation even though the whole transcript is still on
        # disk -- the chat column came back empty ("Session ready. Type a
        # message to start.") for a spec mid-build, and the next message
        # started a context-free turn. Pull the transcript back BEFORE anything
        # creates an empty slot under this key. A restored slot lands in
        # state._slots, so the ownership check below governs it exactly as it
        # governs a live one: a transcript whose metadata says another app owns
        # it is refused, not adopted.
        await _restore_worker_transcript(state, name, adopt_closed=adopt_closed)
        if _slot_identity_moved(name, slot_key):
            return None
        existing = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
    if existing is not None:
        owner = getattr(existing, "_app", None)
        # Only a slot ALREADY owned by this app may be adopted. An UNSCOPED slot
        # under our key is somebody else's conversation -- a main-chat session
        # that happens to be named `spec-builder-<x>` -- and adopting it
        # rewrote its ownership, repointed its project and pulled its transcript
        # into this app. Round 16 removed the reason we used to adopt those: the
        # embedded chat no longer mounts before our own endpoint has created and
        # scoped the slot, so nothing legitimate arrives here unscoped.
        if owner != APP_NAME:
            _audit(
                "spec_slot_foreign_denied",
                f"{name}: owned by {owner or 'nobody'}",
                outcome="denied",
            )
            logger.warning(
                "spec slot %s is owned by %s — refusing to take it over", name, owner or "nobody"
            )
            return None
        slot = existing
    else:
        slot = state.get_or_create_slot(name=slot_key, app=APP_NAME)
    # The indexed working_dir is NOT trusted input. It is app state on disk, and
    # the agent this app runs can be talked into rewriting files -- so a rewritten
    # index entry would become the worker's cwd on the next message, and relative
    # reads from a credential directory would sidestep every per-path check this
    # app makes. Re-validate through the same chokepoint every caller-supplied
    # directory passes, off the event loop, and REFUSE the slot if it no longer
    # holds: a spec whose working dir is unusable must not run at all.
    #
    # ABSENT counts as unusable, which is why this is not gated on `wd` being
    # truthy. `create` rejects an empty or relative working_dir with a 400 and
    # discovery always stamps the root it scanned, so no legitimate entry reaches
    # here without one -- but deleting the key is exactly the edit the agent can
    # make, and skipping the check for it left the slot with no project at all.
    # An unscoped slot is worse than a mis-scoped one: chat_runner passes
    # cwd=slot.project, so the worker's CLI would inherit the GATEWAY's working
    # directory and run every approved relative tool from there.
    wd = str(meta.get("working_dir", ""))
    safe_wd = await asyncio.to_thread(_safe_dir, wd) if wd else None
    if safe_wd is None:
        _audit("spec_working_dir_denied", f"{name}: {_redact(wd)}", outcome="denied")
        logger.warning("spec %s has no usable indexed working_dir — refusing", name)
        return None
    # Second window: _safe_dir ran off-loop, so re-assert the identity before
    # stamping ownership and the project onto the slot. Without this a stale
    # request repointed a replacement spec's worker at ITS OWN directory.
    if _slot_identity_moved(name, slot_key):
        return None
    try:
        slot._app = APP_NAME
        # cwd for the worker's CLI process (chat_runner: cwd=slot.project).
        # Without it the agent must `cd <project>` before every command, which
        # turns every tool pill in the chat into identical cd-noise -- and for a
        # discovered spec it would edit files outside the project entirely.
        if safe_wd is not None:
            slot.project = str(safe_wd)
        if not getattr(slot, "_titled", False):
            slot.title = f"Spec: {name}"
            slot._titled = True
            if hasattr(state, "push_slot_title"):
                state.push_slot_title(slot.key, slot.title)
    except Exception:
        logger.debug("slot scoping failed for %s", name, exc_info=True)
    return slot


#: Distinguishes "caller did not capture an identity" (legacy, unpinned) from
#: "caller captured NOTHING, so there is nothing of ours to act on". Passing
#: ``None`` for a pin must not silently degrade to unpinned.
_UNPINNED: Any = object()


def _exec_loop_id(name: str) -> str | None:
    """The id of this spec's live autonudge loop, or ``None``.

    Captured by stop/delete BEFORE they await, so the removal can be pinned to
    the loop that existed when the request arrived.
    """
    if _autonudge_instance is None:
        return None
    try:
        svc = _autonudge_instance()
        if svc is None:
            return None
        loop = svc.get_by_slot(_slot_key(name))
        return str(getattr(loop, "id", "")) or None if loop else None
    except Exception:
        logger.debug("autonudge lookup failed for %s", name, exc_info=True)
        return None


def _exec_loop_active(name: str) -> bool:
    """True while this spec's autonudge loop is still live.

    The loop is CAPPED (``_EXEC_MAX_CYCLES``): when it runs out of cycles the
    service deactivates it on its own, without telling this app. So the index's
    ``status`` cannot be trusted by itself -- the live loop is the authority.
    """
    if _autonudge_instance is None:
        return False
    try:
        svc = _autonudge_instance()
        if svc is None:
            return False
        loop = svc.get_by_slot(_slot_key(name))
        return bool(loop) and bool(getattr(loop, "active", True))
    except Exception:
        logger.debug("autonudge lookup failed for %s", name, exc_info=True)
        return False


def _numeric(value: object) -> float:
    """An index timestamp as a JSON-representable float, or 0.0.

    index.json is agent-writable, so a timestamp is untrusted input like every other
    field: returning it verbatim let a credential parked in `created_at` reach the
    dashboard, and mixing types broke the list sort. One coercion serves both.

    NaN and the infinities have to go the same way as a non-number. `float()` accepts
    them, and `json.dumps` then writes them as bare `NaN` / `Infinity`, which is not
    JSON -- `JSON.parse` throws on the whole document, so one poisoned timestamp
    takes out the entire spec list rather than the one spec that carries it.
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


#: Outcomes of _claim_execution, so the caller can tell "someone else is already
#: building" from "the spec is gone" without re-reading the index.
_CLAIM_OK = ""
_CLAIM_TAKEN = "taken"
_CLAIM_GONE = "gone"


async def _claim_execution(
    name: str,
    *,
    expect_spec_dir: str,
    expect_slot_key: str,
    live_running: bool,
) -> tuple[str, dict]:
    """Compare-and-set ``planning`` -> ``executing`` for one spec, atomically.

    Reading the status and then committing it in a separate step is not a guard:
    two concurrent execute requests both read ``planning``, both pass, and both
    dispatch -- so Pause cancels one prompt while the other drains and keeps
    editing the user's files. The decision and the write have to be the SAME index
    mutation, which is what this does: ``_mutate_index`` re-reads under its lock,
    so exactly one caller can observe ``planning`` and claim it.

    Identity is checked in the same breath, for the same reason: a delete plus a
    re-import at the same name and path is a different creation, and the claim must
    not land on it.
    """
    outcome = {"reason": _CLAIM_GONE}
    entry: dict = {}

    def _apply(index: dict) -> bool:
        meta = index.get(name)
        if meta is None or str(meta.get("spec_dir", "")) != expect_spec_dir:
            return False
        actual_key = str(meta.get("slot_key", ""))
        if expect_slot_key and actual_key and actual_key != expect_slot_key:
            return False
        # Three signals, because any one of them can be the live one: the recorded
        # status, the nudge loop, and the slot's own running flag.
        if str(meta.get("status", "")) == "executing" or live_running or _exec_loop_active(name):
            outcome["reason"] = _CLAIM_TAKEN
            return False
        now = time.time()
        meta["status"] = "executing"
        meta["exec_started_at"] = now
        # Marks the pre-arm window so a concurrent poll does not reconcile the
        # state away before the loop exists. Cleared once the loop is armed.
        meta["exec_arming_at"] = now
        meta["updated_at"] = now
        entry.update(meta)
        outcome["reason"] = _CLAIM_OK
        return True

    await _mutate_index(_apply)
    return outcome["reason"], entry


#: How long a spec may sit in the pre-arm window before the reconciler stops
#: believing it. Arming is one authorization call plus one index write; a minute is
#: far beyond that, and bounding it matters because a process that dies mid-arm
#: would otherwise mask the reconciliation forever.
_ARMING_GRACE_SECS = 60.0


async def _effective_status(name: str, meta: dict, slot: Any) -> str:
    """The spec's status, reconciled against the live nudge loop.

    Without this, an execution that reached the cycle cap left ``executing``
    persisted forever: the UI showed "building" and offered Pause on a run that
    had already finished, and there was no way back to planning short of a
    restart. Reconciles ONCE and persists, identity-pinned so a recreated spec is
    not stamped by a stale request.
    """
    status = _known_status(meta.get("status"))
    if status != "executing":
        return status
    if _exec_loop_active(name) or bool(getattr(slot, "running", False)):
        return "executing"
    # The handoff records "executing" BEFORE it arms the loop (see the ordering
    # note in _handle_handoff), so between those two steps there is legitimately
    # no loop and no running turn. A poll landing in that window used to reconcile
    # the state away, which hid Pause for the whole run that followed. The handoff
    # stamps exec_arming_at for exactly this window and clears it once the loop is
    # armed, so a value that is still set and still fresh means "arming, not
    # finished".
    try:
        arming_at = float(meta.get("exec_arming_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        arming_at = 0.0
    if arming_at and (time.time() - arming_at) < _ARMING_GRACE_SECS:
        return "executing"
    # BOTH pins, from the same snapshot the caller validated. spec_dir alone
    # cannot tell our spec from a replacement: a delete + re-import at the same
    # name AND path leaves it identical (the rule _unwind_create states).
    #
    # The three guards above do NOT close this. A replacement mid-ARMING has
    # written status=executing but not yet armed its loop, so _exec_loop_active
    # is False and no turn is running -- and the arming grace cannot save it,
    # because `arming_at` is read from the STALE `meta` (this caller's snapshot
    # of the original spec), not from the replacement's fresh entry. Without the
    # slot_key pin the stamp lands on the replacement and hides Pause for the
    # whole run that follows -- exactly the symptom the grace window exists for.
    await _touch_spec(
        name,
        expect_spec_dir=str(meta.get("spec_dir", "")),
        expect_slot_key=str(meta.get("slot_key", "")) or None,
        status="planning",
    )
    _audit("spec_execution_settled", f"{name}: nudge loop no longer active")
    return "planning"


async def _remove_nudge_loop(name: str, *, only_loop_id: Any = _UNPINNED) -> None:
    """Remove this spec's autonudge loop, if any. Single site for the lookup so
    halt / delete / handoff-abort cannot drift apart.

    ``only_loop_id`` pins it to a loop the caller CAPTURED: the lookup is by slot
    key, which is derived from the name, so an unpinned removal on an abort path
    would cancel the loop belonging to a same-name spec created in the meantime.
    """
    if _autonudge_instance is None:  # pragma: no cover - present in prod
        return
    if only_loop_id is None:
        return  # pinned, but nothing was captured -> nothing of ours to remove
    # Failures PROPAGATE. Swallowing them reported success while the loop stayed
    # persisted: an unwritable autonudge store during DELETE returned 200 with the
    # spec gone from the index, and the surviving loop could rearm after a restart
    # against a re-imported spec of the same name. Callers that must stay
    # best-effort (the handoff unwind, where an earlier failure is the real story)
    # catch it explicitly and say so.
    svc = _autonudge_instance()
    if svc is None:
        return
    loop = svc.get_by_slot(_slot_key(name))
    if loop and (only_loop_id is _UNPINNED or getattr(loop, "id", None) == only_loop_id):
        await svc.remove(loop.id)


# Bounds for the agent-authored state file. It is LLM output, so every field is
# treated as hostile: unknown keys dropped, types enforced, lists capped.
_MAX_DECISIONS = 50
_MAX_OPTIONS = 20
_MAX_FIELD = 2000


def _clean_str(v: Any) -> str:
    """Redact and length-cap a value that must be a string. Non-strings -> ''."""
    return _redact(v)[:_MAX_FIELD] if isinstance(v, str) else ""


def _normalize_spec_state(raw: Any) -> dict | None:
    """Project agent-authored ``.spec-state.json`` onto the documented schema.

    Returns ``None`` unless the payload is a dict. Every value is redacted and
    capped, and **keys are redacted too** — a credential placed in an object
    *key* would otherwise be served verbatim, since the previous recursive
    scrub only walked values. Malformed entries (e.g. ``decisions: [null]``,
    which crashed SpecStatePanel) are dropped rather than forwarded.
    """
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}

    decisions: list[dict[str, Any]] = []
    for item in (raw.get("decisions") or [])[:_MAX_DECISIONS] if isinstance(raw.get("decisions"), list) else []:
        if not isinstance(item, dict):
            continue
        did = _clean_str(item.get("id")) or _clean_str(item.get("title"))
        title = _clean_str(item.get("title"))
        if not did or not title:
            continue
        opts_raw = item.get("options")
        options = [
            _clean_str(o) for o in (opts_raw[:_MAX_OPTIONS] if isinstance(opts_raw, list) else []) if isinstance(o, str)
        ]
        decisions.append(
            {
                "id": did,
                "title": title,
                "options": [o for o in options if o],
                "recommended": _clean_str(item.get("recommended")),
                "answer": _clean_str(item.get("answer")),
            }
        )
    out["decisions"] = decisions
    out["blocking"] = _clean_str(raw.get("blocking"))
    ctx = raw.get("context")
    out["context"] = {"template": _clean_str(ctx.get("template")) if isinstance(ctx, dict) else ""}
    return out


def _discard_queued_work(slot: Any) -> None:
    """Drop everything that would start a SUCCESSOR turn on this slot.

    Ending a turn is not the same as stopping the work. ``_run_chat`` swallows
    its ``CancelledError`` instead of re-raising, so its end-of-turn block runs
    on a cancel exactly as it does on a clean finish -- and that block requeues
    unconsumed steers, then starts the next queued message, and otherwise hands
    a pending synthesis to ``_run_pending_synthesis``. So a Pause or a Delete
    that only stopped the turn handed the agent its next prompt: it kept editing
    the user's spec files after the click, and for Delete it kept writing into a
    directory the request was about to archive.

    Three sources can each relaunch, so all three are dropped:
    ``_queue`` (queued messages), ``_pending_steers`` (requeued to the HEAD of
    the queue by the end-of-turn block, so they become queue items) and
    ``_pending_synthesis`` (a subagent-synthesis turn).

    Call this BEFORE any stop -- cooperative or cancel. A cooperative
    ``stop_turn`` ends the turn too, so clearing after it races the successor.

    Attribute-tolerant on purpose: a foreign or partially-built slot may not
    carry these, and failing to discard must never be what breaks teardown.
    """
    for attr in ("_queue", "_pending_steers"):
        seq = getattr(slot, attr, None)
        if seq is None:
            continue
        try:
            seq.clear()
        except Exception:
            logger.debug("could not clear %s during stop", attr, exc_info=True)
    try:
        slot._pending_synthesis = False
    except Exception:
        logger.debug("could not clear _pending_synthesis during stop", exc_info=True)


async def _teardown_worker_slot(
    state: Any, name: str, *, only_slot: Any = _UNPINNED, require_archive: bool = False
) -> bool:
    """Remove this spec's worker slot, cancelling any in-flight turn.

    Mirrors the gateway's own slot-delete sequence: pop from the registry BEFORE
    any await (so nothing can re-enter it mid-teardown), then cancel the running
    task and await it with a bounded shield, then persist the slot as closed.

    Only ever touches a slot this app owns (``slot._app == APP_NAME``) — a
    foreign or unscoped slot is left alone rather than deleted by name collision.

    ``only_slot`` pins it to the exact slot OBJECT the caller captured. The
    registry is keyed by name, so an abort path that tears down "by name" would
    destroy the slot of a same-name spec created while the request was in flight.

    Returns False ONLY when ``require_archive`` was asked for and persisting the
    conversation failed. Every refusal path returns True: there is no transcript of
    OURS at risk (no slot, a replacement, or a foreign owner), so a caller must not
    treat it as data loss and abort.
    """
    if state is None:
        return True
    if only_slot is None:
        return True  # pinned, but nothing was captured -> nothing of ours to tear down
    # The captured slot's own key wins when the caller pinned one: recomputing from
    # the name would look up a DIFFERENT slot once keys are per-creation.
    slot_key = getattr(only_slot, "key", None) or _slot_key(name)
    if not isinstance(slot_key, str) or not _SLOT_KEY_RE.match(slot_key):
        slot_key = _slot_key(name)
    try:
        slot = state.get_slot(slot_key)
    except Exception:
        slot = None
    if slot is None:
        return True
    if only_slot is not _UNPINNED and slot is not only_slot:
        logger.warning("refusing to tear down slot %s: replaced since capture", slot_key)
        return True
    if getattr(slot, "_app", None) != APP_NAME:
        logger.warning("refusing to tear down slot %s: not owned by %s", slot_key, APP_NAME)
        return True
    # Before the cancel below: _run_chat's end-of-turn block would otherwise
    # start the next queued prompt, so the agent would keep writing into a spec
    # directory this request is about to archive.
    _discard_queued_work(slot)
    try:
        state._slots.pop(slot_key, None)
    except Exception:
        logger.debug("slot registry pop failed for %s", slot_key, exc_info=True)
    task = getattr(slot, "task", None)
    if getattr(slot, "running", False) and task is not None:
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            logger.debug("worker task raised during teardown of %s", slot_key, exc_info=True)
    # circular import (see module header): dashboard.server imports this module.
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    try:
        await save_slot_off_loop(
            state, slot, closed=True, best_effort=not require_archive
        )
    except Exception:
        # The transcript is the user's data. A caller that is about to drop the
        # spec from the index (delete) asks for require_archive, because reporting
        # success here would discard a conversation that was never written. The
        # slot is put back so the caller can restore the entry and the user can
        # retry; callers that do not require the archive keep the old
        # best-effort behaviour (an abort path has already lost the race).
        logger.warning("closing save failed for %s", slot_key, exc_info=True)
        if require_archive:
            try:
                state._slots[slot_key] = slot
            except Exception:
                logger.warning("could not restore slot %s after a failed archive", slot_key)
            _audit("spec_slot_archive_failed", name, outcome="denied")
            return False
    _audit("spec_slot_teardown", name)
    return True


async def _halt_execution(
    state: Any,
    name: str,
    spec_dir: Path,
    *,
    reason: str,
    only_loop_id: Any = _UNPINNED,
    only_slot: Any = _UNPINNED,
    expect_slot_key: str = "",
) -> None:
    """Stop an autonomous run: sentinel the loop, then remove it.

    Deliberately does NOT touch ``slot._trust``. This app no longer grants
    trust, so there is nothing of ours to revoke — and if the USER trusted the
    session from the approval card, Stop must not silently undo their decision.
    """
    # Off-loop: the sentinel write is six filesystem syscalls, and a spec dir on
    # unresponsive network storage would otherwise freeze the gateway loop for
    # the duration of a Stop click. The identity travels WITH the write rather
    # than being checked by the caller beforehand: the caller's check and this
    # write are separated by a thread hop, which is exactly the window a same-name
    # delete plus re-import needs to redirect the STOP onto a replacement.
    if not await asyncio.to_thread(
        _write_stop_sentinel_for_spec, spec_dir, name, expect_slot_key
    ):
        # Not fatal: the two stops below are what actually end the run. Logged so an
        # operator can tell "no sentinel" from "sentinel ignored".
        logger.warning("spec %s: no stop sentinel written; halting by loop + turn", name)
    await _remove_nudge_loop(name, only_loop_id=only_loop_id)
    # ...and stop the turn that is running RIGHT NOW. The sentinel and the loop
    # removal only prevent FUTURE nudges: the in-flight _run_chat kept going, so
    # Pause flipped the status to "planning" and returned ok while the agent
    # carried on editing the user's files. Cooperative stop first (the gateway's
    # own stop_turn), then a bounded cancel of the slot task as the fallback.
    await _halt_active_turn(state, name, only_slot=only_slot)
    _audit("spec_execution_halted", f"{name}: {reason}")


async def _halt_active_turn(state: Any, name: str, *, only_slot: Any = _UNPINNED) -> bool:
    """Stop the spec slot's in-flight turn, keeping the slot and its transcript.

    Unlike ``_teardown_worker_slot`` (used by DELETE) this does not remove the
    slot -- Pause must leave the conversation intact so the user can resume.
    Returns True when a running turn was stopped.
    """
    if only_slot is None:
        return False  # pinned, but nothing was captured
    slot = state.get_slot(_slot_key(name)) if state is not None else None
    if slot is None or not getattr(slot, "running", False):
        return False
    if only_slot is not _UNPINNED and slot is not only_slot:
        logger.warning("refusing to stop slot %s: replaced since capture", _slot_key(name))
        return False
    # Ownership must be EXACT, as it is in _ensure_worker_slot and
    # _teardown_worker_slot. Tolerating an unscoped owner here meant a plain
    # `POST /api/chat` on slot `spec-builder-<name>` -- somebody else's
    # conversation that merely shares the key -- could be cancelled mid-turn by
    # this app's Stop button, losing that turn's response.
    if getattr(slot, "_app", None) != APP_NAME:
        return False
    # Before BOTH stops below. The cooperative stop_turn also ends the turn, so
    # clearing after it would race _run_chat's end-of-turn block into starting
    # the next queued prompt -- Pause would return ok while the agent carried on.
    _discard_queued_work(slot)
    try:
        # circular import (see module header): dashboard.server imports us.
        from kiro_crew.dashboard.chat_utils import _history_key_for

        await state.sessions.stop_turn(_history_key_for(slot.key), force=False)
    except Exception:
        logger.debug("cooperative stop failed for %s", name, exc_info=True)
    task = getattr(slot, "task", None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            logger.debug("worker task raised while pausing %s", name, exc_info=True)
    return True


def _derive_phase(spec_dir: Path) -> str:
    for phase, fname in _PHASE_FILES:
        if _spec_file(spec_dir, fname) is not None and (spec_dir / fname).is_file():
            return phase
    return "new"


def _read_spec_files(spec_dir: Path) -> dict:
    out: dict[str, str | None] = {}
    for _phase, fname in _PHASE_FILES:
        text = _read_spec_text(spec_dir, fname)
        out[fname] = _redact(text) if text is not None else None
    return out


# ── validation / auth ────────────────────────────────────────────────────────


def _require_auth(request: web.Request) -> web.Response | None:
    """Trust only the middleware-set user (mirrors auto_research). Returns a 401
    response when unauthenticated, else None."""
    if request.get("user") is not None:
        return None
    return web.json_response({"code": "unauthorized", "error": "Unauthorized"}, status=401)


async def _read_json(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"code": "invalid_json", "error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"code": "body_not_object", "error": "body must be a JSON object"}, status=400)
    return body


def _valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


# ── seed / execution prompts ─────────────────────────────────────────────────


#: Per-type deliverables. `quick` deliberately skips design.md: the previous seed
#: demanded all three documents for every type, which contradicted the spec type
#: the user had chosen.
_TYPE_PLAN: dict[str, tuple[str, ...]] = {
    "feature": ("requirements.md", "design.md", "tasks.md"),
    "bug": ("requirements.md", "design.md", "tasks.md"),
    "quick": ("requirements.md", "tasks.md"),
}

_TYPE_GUIDANCE: dict[str, str] = {
    "feature": (
        "FEATURE spec: full Requirements -> Design -> Tasks. requirements.md states "
        "user-visible behaviour with acceptance criteria; design.md states the technical "
        "approach; tasks.md is an ordered, checkable task list."
    ),
    "bug": (
        "BUG spec: requirements.md is the investigation -- symptoms, reproduction, root "
        "cause, expected behaviour. design.md is the fix approach. tasks.md is the "
        "ordered fix plus the regression test that would have caught it."
    ),
    "quick": (
        "QUICK spec: keep it light. requirements.md is a short goal plus acceptance "
        "bullets, then tasks.md is the ordered task list. Do NOT write design.md unless "
        "the user asks for it."
    ),
}


def _seed_prompt(spec_type: str, name: str, spec_dir: Path, working_dir: str, description: str) -> str:
    """The opening turn for a new spec.

    SELF-CONTAINED by necessity: this app ships a ``spec-workflow`` skill in its
    manifest, but builtin apps are not run through ``bridges.register_app`` (that
    path symlinks from ``~/.kiro/crew/apps/<name>/``, which a wheel-shipped
    builtin does not have), so the skill is NOT on the agent's skill path. The
    old prompt told the agent to "follow the `spec-workflow` skill exactly" --
    a dangling reference -- and listed all three documents regardless of the spec
    type the user picked. Everything the agent needs is now stated here.
    """
    desc = f"\n\nThe user's initial description:\n{description.strip()}" if description.strip() else ""
    files = _TYPE_PLAN.get(spec_type, _TYPE_PLAN["feature"])
    guidance = _TYPE_GUIDANCE.get(spec_type, _TYPE_GUIDANCE["feature"])
    paths = "\n".join(f"  - {spec_dir / f}" for f in files)
    return (
        f"You are the Kiro Spec agent for spec **{name}** (type: **{spec_type}**).\n\n"
        f"{guidance}\n\n"
        f"Write ONLY to these EXACT absolute paths (never invent another location):\n"
        f"{paths}\n"
        f"WORKING_DIR (the codebase this spec is for): {working_dir}\n\n"
        f"How to work:\n"
        f"- ONE phase at a time. After writing a file, STOP and ask the user to review; do "
        f"not start the next phase until they approve.\n"
        f"- Ask focused clarifying questions in chat (1-3 at a time, with your recommended "
        f"answer) only when the answer would materially change the output. Never ask what "
        f"you can find by reading {working_dir} yourself.\n"
        f"- Keep every document self-contained and concrete: no placeholders, no TODOs.\n\n"
        f"Also maintain {spec_dir / '.spec-state.json'} -- the app renders it as UI, so it "
        f"is plumbing: never mention it in chat and never list it as a deliverable. Shape:\n"
        f'  {{"decisions": [{{"id": "<stable-id>", "title": "<question>", '
        f'"options": ["A", "B"], "recommended": "A", "answer": null}}], '
        f'"blocking": "<one sentence: what you are waiting on, or null>", '
        f'"context": {{"template": "<the module you are modelling this on>"}}}}\n'
        f"Update it every time you ask a decision, receive an answer, or change phase; set "
        f"a decision's `answer` when the user picks one and keep the entry.\n\n"
        f"Begin with {files[0]}: draft it, then STOP and ask the user to review before "
        f"moving on.{desc}"
    )


def _exec_prompt(name: str, spec_dir: Path, working_dir: str) -> str:
    return (
        f"EXECUTION HANDOFF for spec '{name}'. The plan is approved. Read "
        f"{spec_dir / 'tasks.md'} and work through each unchecked task IN ORDER, "
        f"operating inside {working_dir} (your shell already starts there — no cd needed). After each task: "
        f"mark its checkbox [x] in tasks.md, run the relevant build/tests to verify, "
        f"then continue. Stop when all tasks are checked or you hit a blocker that needs "
        f"me, and summarize what was done and what remains."
    )


# ── slot turn relay (embedded chat) ──────────────────────────────────────────


def _dispatch_turn(state: Any, slot: Any, message: str) -> None:
    """Relay a user (or system) turn into the spec's agent slot, mirroring the
    dashboard's origin-injection path (chat_runner._run_chat)."""
    if getattr(slot, "running", False):
        try:
            slot.queue_append(message)
        except Exception:
            logger.debug("queue_append failed", exc_info=True)
        try:
            # _redact, not the raw message: `queued` is NOT one of the roles
            # _ChatSlot.append suppresses the global SSE push for (only "chunk",
            # "done" and "user" are), so this text goes to every connected
            # dashboard client. The host sanitizes the stored value on its own
            # steer/queue paths for the same reason -- raw content must not reach
            # an external surface -- and _redact is this module's copy of that
            # chain, failing closed when the security module is unavailable.
            slot.append("queued", _redact(message))
        except Exception:
            pass
        state.push_slots_update()
        return
    # circular import (see module header): dashboard.server imports this module.
    from kiro_crew.dashboard.chat_runner import _run_chat

    try:
        # Deferred like the other dashboard imports; the resolver follows a
        # raised agent.chat_turn_timeout_secs above the 2h default and runs
        # OFF the event loop (inside the task, via asyncio.to_thread).
        from kiro_crew.dashboard.turn_dispatch import bounded_chat_turn
    except Exception:  # pragma: no cover - resolver always present in prod
        bounded_chat_turn = None  # type: ignore[assignment]

    slot.append("user", message)
    if bounded_chat_turn is not None:
        task = asyncio.create_task(bounded_chat_turn(_run_chat(state, slot, message)))
    else:
        task = asyncio.create_task(
            asyncio.wait_for(_run_chat(state, slot, message), timeout=float(CHAT_TURN_TIMEOUT))
        )
    slot.task = task
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    state.push_slots_update()


async def _serialize_messages(state: Any, slot_key: str) -> list[dict]:
    """Return the spec slot's transcript for the embedded chat view. Prefers the
    live in-memory slot (includes in-progress turns); falls back to the persisted
    session log. Content is redacted before leaving the backend.

    ASYNC because the fallback reads the persisted transcript: a whole JSONL file
    off disk, which is exactly the case that matters (a rehydrated session with no
    in-memory messages, i.e. right after a gateway restart, which is when the user
    opens the spec again). Doing that inline stalled the gateway event loop for
    the length of the file.
    """
    msgs: list[Any] = []
    slot = state.get_slot(slot_key)
    if slot is not None and getattr(slot, "messages", None):
        msgs = list(slot.messages)
    else:
        try:
            # circular import (see module header): dashboard.server imports us.
            from kiro_crew.dashboard.chat_utils import _history_key_for

            if getattr(state, "conversation_log", None) is not None:
                msgs = await asyncio.to_thread(
                    state.conversation_log.read_messages, _history_key_for(slot_key)
                )
        except Exception:
            logger.debug("read_messages failed for %s", slot_key, exc_info=True)
    out: list[dict] = []
    for m in msgs:
        if isinstance(m, dict):
            role, content, ts = m.get("role", ""), m.get("content", ""), m.get("ts", "")
        else:
            role = getattr(m, "role", "")
            content = getattr(m, "content", "")
            ts = getattr(m, "ts", "")
        if role == "system":
            continue
        if role == "tool":
            # Mirror the main chat: surface tool activity as a compact line
            # (first line, bounded) so the embedded chat shows the agent working.
            first = (content or "").strip().splitlines()[0] if content else ""
            out.append({"role": "tool", "content": _redact(first[:200]), "ts": ts})
            continue
        out.append({"role": role, "content": _redact(content or ""), "ts": ts})
    return out


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
        argv, env, cleanup = await asyncio.to_thread(
            _prepare_git_spawn, ["git", "-C", cwd, *args]
        )
    except Exception as exc:
        # Sandbox unavailable / argv build failure: report it, do not 500 the
        # caller. Every caller already treats a non-zero rc as "not a git repo".
        _audit_tool("error", subcommand, cwd, error=type(exc).__name__)
        return _GIT_UNAVAILABLE, "", f"git unavailable: {type(exc).__name__}"
    proc: Any = None
    try:
        # create_subprocess_limited, not create_subprocess_exec + preexec_fn: the
        # limits are applied after exec by a shim, so spawning never forks the
        # gateway's ~100 threads (see kiro_crew.sandbox and issue #935).
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


# ── HTTP handlers ─────────────────────────────────────────────────────────────


async def _handle_repo_info(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    path = (request.query.get("path") or "").strip()
    # Off-loop AND through the same chokepoint as every other caller-supplied
    # directory: the hand-rolled is_absolute()/is_dir() pair both ran a stat on
    # the event loop (an unavailable network path froze the gateway) and skipped
    # the sensitive-path denial that _safe_dir applies.
    safe = await asyncio.to_thread(_safe_dir, path) if path else None
    if safe is None:
        return web.json_response({"is_git": False})
    return web.json_response(await _repo_info(str(safe)))


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


async def _handle_browse(request: web.Request) -> web.Response:
    """GET /browse?path= — unified folder picker feed for the UI.

    Returns ``{path, parent, dirs, is_git, recents}``: subdirectories of
    ``path`` (default: $HOME), whether ``path`` is a git repo, and — on the
    initial empty-path call — the dashboard's recent projects list. Mirrors
    the host ``api_browse_dirs`` security model: realpath + sensitive-path
    denial (including symlink targets), hidden/build dirs skipped, SEL audit.
    """
    if denied := _require_auth(request):
        return denied
    raw = (request.query.get("path") or "").strip()
    initial = not raw
    # Same chokepoint as create/settings — one implementation, one guarantee.
    # Off-loop: _safe_dir expands, realpaths and stats a CALLER-SUPPLIED path
    # (plus the nearest existing ancestor), so an unresponsive mount would freeze
    # the gateway before the scan below ever got its own thread.
    safe = await asyncio.to_thread(_safe_dir, raw or str(Path.home()))
    if safe is None:
        _audit("spec_browse_denied", raw or "~")
        return web.json_response({"code": "access_denied", "error": "Access denied"}, status=403)
    base = str(safe)
    # The scan is genuinely blocking work: scandir + a full sort + a realpath and
    # sensitive-path test PER ENTRY. On a large directory that stalls the whole
    # aiohttp loop (chat streaming, heartbeats, every other app), so it runs in a
    # worker thread. Also bounded, so a pathological directory can't produce an
    # unbounded response.
    dirs = await asyncio.to_thread(_scan_subdirs, base)
    out: dict[str, Any] = {
        "path": base,
        "parent": os.path.dirname(base),
        "dirs": dirs,
        "is_git": (await _repo_info(base)).get("is_git", False),
    }
    if initial:
        # Off-loop: a file read, a JSON parse and an is_dir() per candidate — on
        # stalled home storage that froze the gateway inside the picker's very
        # first request.
        out["recents"] = await asyncio.to_thread(_read_recent_projects)
    _audit("spec_browse", base)
    return web.json_response(out)


async def _handle_get_settings(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    s = await asyncio.to_thread(_load_settings)
    # _redact like every other stored value this module returns (see the list
    # endpoint's working_dir / spec_dir / spec_type). settings.json is
    # agent-writable -- _load_settings says so itself and validates only its
    # SHAPE -- so a credential parked in base_path would otherwise be rendered
    # verbatim in the dashboard.
    return web.json_response({"base_path": _redact(str(s.get("base_path", "")))})


async def _handle_put_settings(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    base = str(body.get("base_path", "")).strip()
    if base:
        if not Path(base).is_absolute():
            return web.json_response({"code": "base_path_not_absolute", "error": "base_path must be an absolute path"}, status=400)
        # Same chokepoint as working_dir: without this, spec storage could be
        # repointed at a credential directory and every subsequent spec would
        # write into it.
        safe_base = await asyncio.to_thread(_safe_dir_optional, base)
        if safe_base is None:
            return web.json_response(
                {"code": "base_path_not_a_directory", "error": "base_path must be an existing, non-sensitive directory"}, status=400
            )
        base = str(safe_base)
    await asyncio.to_thread(_save_settings, {"base_path": base})
    _audit("settings_update", f"base_path={'set' if base else 'default'}")
    return web.json_response({"ok": True, "base_path": base})


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
    working_dir: str, safe_wd: Path, name: str, import_existing: bool
) -> tuple[Path, str]:
    """Resolve + validate + create the spec directory. BLOCKING -- one hop.

    Returns ``(spec_dir, refusal)``; ``refusal`` is ``""`` on success, else
    ``"escape"``, ``"existing:<files>"`` or ``"mkdir:<reason>"``.
    """
    spec_dir = _resolve_spec_dir(working_dir, name)
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
    try:
        spec_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return spec_dir, f"mkdir:{exc}"
    return spec_dir, ""


def _load_index_with_discovery() -> tuple[dict, dict[str, str]]:
    """Load the index, fold in specs found on disk, and derive every phase --
    all in ONE thread hop, under the index lock.

    BLOCKING -- call via ``asyncio.to_thread``. The list endpoint is polled every
    15s, so none of this may run on the event loop: discovery walks every known
    project root's ``.kiro/specs``, and ``_derive_phase`` stats up to three files
    PER SPEC (the response loop used to do that inline, so a large index froze
    the loop on every poll). Returns ``(index, {name: phase})``.
    """
    with _INDEX_LOCK:
        index = _load_index()
        if _discover_folder_specs(index):
            _save_index(index)
    phases = {name: _derive_phase(Path(m.get("spec_dir", ""))) for name, m in index.items()}
    return index, phases


async def _handle_list(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    index, phases = await asyncio.to_thread(_load_index_with_discovery)
    specs = []
    for name, meta in index.items():
        # A delete in flight keeps its entry so the name stays reserved (see
        # _mark_deleting); it is not a spec the user still has.
        if isinstance(meta, dict) and meta.get(_DELETING):
            continue
        spec_dir = Path(meta.get("spec_dir", ""))
        slot = state.get_slot(_slot_key(name)) if (state := request.app.get("state")) else None
        specs.append(
            {
                "name": name,
                # index.json is AGENT-WRITABLE: the worker runs in the user's project
                # and can put anything in these fields, so every string that came out
                # of the index is scrubbed on the way to the browser -- the same
                # treatment transcript and file content already get.
                "working_dir": _redact(str(meta.get("working_dir", ""))),
                "spec_dir": _redact(str(spec_dir)),
                "spec_type": _redact(str(meta.get("spec_type", "feature"))),
                # Reconciled, not raw: a capped nudge loop that ran out of cycles
                # leaves "executing" in the index forever (see _effective_status).
                "status": await _effective_status(name, meta, slot),
                "phase": phases.get(name, "new"),
                "running": bool(getattr(slot, "running", False)),
                # Validated, not passed through: see _numeric.
                "created_at": _numeric(meta.get("created_at")),
                "updated_at": _numeric(meta.get("updated_at")),
            }
        )
    # Timestamps are agent-writable too, so they are not necessarily numbers. Mixing a
    # str and a float in one sort key raises TypeError, which turned a single malformed
    # entry into a 500 on EVERY list request -- the whole app dark, with no way back
    # through the UI. Coerce per entry instead.

    def _sort_key(entry: dict) -> float:
        # The payload already carries validated floats (see _numeric), so this only
        # has to pick which one orders the list.
        return _numeric(entry.get("updated_at")) or _numeric(entry.get("created_at"))

    specs.sort(key=_sort_key, reverse=True)
    return web.json_response({"specs": specs, "default_base": ".kiro/specs"})


def _opted_in(body: dict, field: str) -> bool:
    """True only when *field* is the JSON boolean ``true``.

    ``bool(body.get(field))`` accepted any truthy value, so a client (or an agent
    building the request) sending the STRING ``"false"`` — or ``"0"``, or ``[]``'s
    opposite, any non-empty string — silently opted in. For these two flags that
    meant creating a git worktree and branch, or adopting documents already on
    disk, from a request that said not to. Both are side effects a caller cannot
    undo by retrying, so the check is exact rather than lenient.
    """
    return body.get(field) is True


async def _handle_create(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    name = str(body.get("name", "")).strip()
    working_dir = str(body.get("working_dir", "")).strip()
    spec_type = str(body.get("spec_type", "feature")).strip().lower()
    description = str(body.get("description", ""))
    # _usable_name, not _valid_name: the loader admits an index key only when it
    # ALSO survives _redact unchanged, so accepting on the grammar alone created
    # specs that the very next _load_index discarded, orphaning the directory,
    # worktree and session this handler had already built. Credential-shaped
    # slugs reach here for real -- a description can slugify into one.
    if not _usable_name(name):
        return web.json_response(
            {
                "code": "invalid_name",
                "error": (
                    "name must be 1-64 chars: letters, digits, '-' or '_', "
                    "and must not look like a credential"
                ),
            },
            status=400,
        )
    if spec_type not in _VALID_TYPES:
        return web.json_response({"code": "invalid_spec_type", "error": f"spec_type must be one of {_VALID_TYPES}"}, status=400)
    if not working_dir or not Path(working_dir).is_absolute():
        return web.json_response({"code": "working_dir_not_absolute", "error": "working_dir must be an absolute path"}, status=400)
    safe_wd = await asyncio.to_thread(_safe_dir, working_dir)
    if safe_wd is None:
        # Covers "missing", "not a directory" and "sensitive location" with one
        # response so the endpoint can't be used to probe the filesystem.
        return web.json_response(
            {"code": "working_dir_not_a_directory", "error": "working_dir must be an existing, non-sensitive directory"}, status=400
        )
    working_dir = str(safe_wd)
    index = await _aload_index()
    if name in index:
        return web.json_response({"code": "spec_exists", "error": f"a spec named '{name}' already exists"}, status=409)

    # Optional: create a dedicated worktree + branch off the chosen repo and
    # use IT as the working dir (worktree-per-spec workflow). The spec files
    # then live inside the worktree's .kiro/specs/, traveling with the branch.
    worktree_branch = ""
    repo_root = ""
    created_worktree = ""
    if _opted_in(body, "use_worktree"):
        info = await _repo_info(working_dir)
        if not info.get("is_git"):
            return web.json_response({"code": "worktree_requires_git", "error": "use_worktree requires a git repository"}, status=400)
        repo_root = info["root"]
        wt = await _create_worktree(repo_root, name)
        if isinstance(wt, str):
            return web.json_response({"code": "worktree_creation_failed", "error": f"worktree creation failed: {wt}"}, status=400)
        working_dir, worktree_branch = wt
        created_worktree = working_dir
        _audit("spec_worktree_create", f"{name} -> {working_dir}")
        # The worktree is a SIBLING of the original checkout, so it becomes the
        # new containment root. Re-validate it through the same chokepoint —
        # without this, containment below is still measured against the original
        # checkout and every worktree-mode create fails.
        safe_wt = await asyncio.to_thread(_safe_dir, working_dir)
        if safe_wt is None:
            await _remove_worktree(repo_root, created_worktree, worktree_branch)
            return web.json_response(
                {"code": "worktree_unusable", "error": "created worktree is not a usable directory"}, status=400
            )
        safe_wd = safe_wt
        working_dir = str(safe_wd)

    # One thread hop for the rest of create's filesystem work: resolving the spec
    # dir (which reads settings), the containment check, the adopt-by-overwrite
    # probe and the mkdir. All of it stats caller-supplied paths, so none of it
    # may run on the event loop.
    spec_dir, refusal = await asyncio.to_thread(
        _prepare_spec_dir, working_dir, safe_wd, name, _opted_in(body, "import_existing")
    )
    if refusal:
        kind, _, detail = refusal.partition(":")
        if created_worktree:
            await _remove_worktree(repo_root, created_worktree, worktree_branch)
        if kind == "escape":
            _audit("spec_path_escape_denied", f"{name} -> {spec_dir}")
            return web.json_response(
                {"code": "spec_path_outside_root", "error": "resolved spec path is outside its root"}, status=400
            )
        if kind == "existing":
            return web.json_response(
                {
                    "code": "spec_files_exist",
                    "error": (
                        f"'{name}' already has spec files ({detail}) at "
                        f"{spec_dir}. Re-send with import_existing to adopt them."
                    ),
                },
                status=409,
            )
        return web.json_response({"code": "spec_dir_creation_failed", "error": f"cannot create spec dir: {detail}"}, status=400)

    # Creating this spec is an explicit decision that outranks an earlier delete of
    # the same directory, so the tombstone goes away — otherwise discovery would
    # keep skipping a spec the user just asked for.
    await asyncio.to_thread(_forget_deleted, str(spec_dir))
    # A fresh key per creation, so a name reused after a delete never appends to
    # the previous spec's transcript. Registered in the resolver map immediately:
    # the slot is acquired below, before the next index read repopulates it.
    slot_key = _new_slot_key(name)
    _SLOT_KEYS[name] = slot_key
    now = time.time()
    entry = {
        "working_dir": working_dir,
        "spec_dir": str(spec_dir),
        "spec_type": spec_type,
        "status": "planning",
        "slot_key": slot_key,
        "worktree_branch": worktree_branch,
        "repo_root": repo_root,
        "created_at": now,
        "updated_at": now,
    }

    # Re-reading commit: create awaits git subprocesses and the request body, so
    # the duplicate-name check at the top is stale by now. Insert from a FRESH
    # read (and refuse if the name was taken meanwhile) so two concurrent creates
    # cannot silently overwrite each other, and so writing back the pre-await
    # snapshot cannot resurrect a spec deleted in the window.
    def _insert(index: dict) -> bool:
        if name in index:
            return False
        index[name] = entry
        return True

    if not await _mutate_index(_insert):
        if created_worktree:
            await _remove_worktree(repo_root, created_worktree, worktree_branch)
        return web.json_response({"code": "spec_exists", "error": f"a spec named '{name}' already exists"}, status=409)

    # The slot is acquired and configured ONLY AFTER the index arbitration above
    # decides this create won. get_or_create_slot keys off the name, so two
    # concurrent same-name creates share ONE slot: configuring it before
    # arbitration meant the LOSER stamped its own working_dir onto the shared
    # slot, and the winner's agent then ran in the rejected directory. The loser
    # now returns 409 having touched no slot state.
    state = request.app["state"]

    async def _unwind_create() -> None:
        """Drop what this create inserted -- identity-pinned. The pop keys off the
        NAME, so an unpinned unwind would delete the index entry of a same-name
        spec created while we were validating, leaving the user's new spec's files
        and slot behind with no record of them.

        Pinned on the per-creation slot key as well as the directory: a delete
        followed by a re-import at the same name AND path leaves spec_dir
        identical, so the directory alone cannot tell our insert from the
        replacement's."""
        ours = str(spec_dir)

        def _pop_if_ours(idx: dict) -> bool:
            meta = idx.get(name)
            if meta is None or str(meta.get("spec_dir", "")) != ours:
                return False
            if str(meta.get("slot_key", "")) != slot_key:
                return False
            del idx[name]
            return True

        was_ours = await _mutate_index(_pop_if_ours)
        # Gated on that SAME identity check -- see _rollback_worktree_if_ours for
        # why an ungated force-removal could destroy a replacement spec's work.
        await _rollback_worktree_if_ours(
            name,
            was_ours=was_ours,
            repo_root=repo_root,
            created_worktree=created_worktree,
            worktree_branch=worktree_branch,
        )

    # adopt_closed=False: this spec is being CREATED. A delete leaves the old
    # spec's archived transcript on disk under a key derived from the NAME, so
    # adopting closed history here would hand the fresh agent the deleted
    # conversation. Only already-indexed specs may adopt a closed transcript.
    slot = await _ensure_worker_slot(state, name, entry, adopt_closed=False)
    if slot is None:
        # Another app owns this slot key, or the working dir no longer validates.
        await _unwind_create()
        return web.json_response(
            {"code": "slot_owned_by_another_app", "error": f"a chat session named '{name}' is owned by another app"}, status=409
        )
    # Slot setup AWAITS (the working-dir chokepoint runs off-loop), so a concurrent
    # delete-and-recreate can land in that window. Confirm this is still OUR spec
    # before dispatching a seed prompt that names our spec_dir -- otherwise the
    # turn would drive the replacement spec's agent with our plan.
    current = await _aload_index()
    live = current.get(name) or {}
    # Both fields, because a re-import at the same name AND path keeps spec_dir
    # while being a different creation with a different conversation -- and the
    # seed prompt below would then drive the replacement's agent.
    if (
        str(live.get("spec_dir", "")) != str(spec_dir)
        or str(live.get("slot_key", "")) != slot_key
    ):
        await _unwind_create()
        _audit("spec_create_aborted", f"{name}: deleted or recreated during slot setup")
        return web.json_response(
            {"code": "spec_changed_during_create", "error": "spec was deleted or recreated while being created; retry"},
            status=409,
        )
    # NO auto-approve grant. This app used to stamp slot._trust because a
    # permission prompt was invisible in the embedded chat, so an un-trusted
    # worker stalled silently on its first tool call. That premise is gone: the
    # embed now renders working Approve / Trust / Reject controls
    # (ChatEmbed -> ChatMessageList onApprove -> the slot approve route). Granting trust
    # from the backend cannot be bounded honestly — a wall-clock TTL enforced on
    # the UI's status poll stops being enforced the moment the page is closed —
    # so the decision belongs to the user, through core's own trust mechanism,
    # where "Trust all tools" is one click and is auditable as THEIR choice.
    try:
        slot.title = f"Spec: {name}"
        slot._titled = True
        if hasattr(state, "push_slot_title"):
            state.push_slot_title(slot.key, slot.title)
    except Exception:
        logger.debug("title set failed", exc_info=True)

    _dispatch_turn(state, slot, _seed_prompt(spec_type, name, spec_dir, working_dir, description))
    _audit("spec_create", name)
    return web.json_response(
        {
            "name": name,
            "spec_dir": str(spec_dir),
            "spec_type": spec_type,
            "status": "planning",
            "working_dir": working_dir,
            "worktree_branch": worktree_branch,
        },
        status=201,
    )


async def _handle_get(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    index = await _aload_index()
    meta = index.get(name)
    if not meta:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    spec_dir = Path(meta["spec_dir"])

    state = request.app.get("state")

    # Structured state maintained by the agent (decisions/blocking/context).
    # LLM-authored -> read symlink-safely, then project onto the documented
    # schema (types enforced, keys AND values redacted, lists capped) rather
    # than forwarding whatever shape the model happened to write.
    #
    # ALL of the detail handler's filesystem work happens in ONE worker-thread
    # hop: stat-ing the three phase files, reading up to three 1 MiB documents,
    # and reading .spec-state.json. The UI polls this endpoint every 2.5s while a
    # build runs, so doing it inline froze the gateway's event loop — chat
    # streaming and heartbeats included — for the duration of every poll.
    phase, files, spec_state = await asyncio.to_thread(_collect_spec_documents, spec_dir)

    # Live context counters from the worker slot's transcript. The slot is
    # CREATED here if it does not exist yet (see _ensure_worker_slot): a spec
    # discovered on disk has no slot, and if the embedded chat's /api/chat made
    # the first one it came up unscoped -- no _app, no project -- so approved
    # tools ran from the gateway's working directory, not the user's project.
    # Re-read the index before scoping the slot: the document collection above
    # awaits, so the spec can be deleted and RECREATED (elsewhere) in that
    # window. Scoping from the pre-await snapshot would repoint the new worker's
    # project at the OLD directory, and its agent would edit the old project.
    #
    # The identity check is the other half: an entry under the same NAME is not
    # the same spec. Without it this response would pair documents read from the
    # old directory with the new metadata.
    fresh = await _aload_index()
    meta = fresh.get(name)
    if not meta:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    if str(meta.get("spec_dir", "")) != str(spec_dir):
        return web.json_response(
            {"code": "spec_changed_during_read", "error": "spec was recreated while loading; retry"}, status=409
        )
    turns = tool_calls = 0
    slot = await _ensure_worker_slot(state, name, meta)
    if slot is None and state is not None:
        # A foreign or unscoped slot holds this key (see _ensure_worker_slot).
        # Returning 200 anyway meant ChatEmbed mounted against that unrelated
        # session -- the user could read it, message into it and approve its tool
        # calls from this app. Refuse the whole detail read instead.
        return web.json_response(
            {"code": "slot_owned_by_another_app", "error": "this spec's chat session is owned by another app"}, status=409
        )
    if slot is not None and getattr(slot, "messages", None):
        for m in slot.messages:
            role = m.get("role", "") if isinstance(m, dict) else getattr(m, "role", "")
            if role == "user":
                turns += 1
            elif role == "tool":
                tool_calls += 1

    return web.json_response(
        {
            "name": name,
            # Agent-writable index fields; see the note in _handle_list.
            "working_dir": _redact(str(meta.get("working_dir", ""))),
            "spec_dir": _redact(str(spec_dir)),
            "spec_type": _redact(str(meta.get("spec_type", "feature"))),
            # The chat slot this spec's conversation lives in. The SPA must NOT
            # derive it from the name: keys are per-creation now, so a reused name
            # would mount the embed against the previous spec's transcript. Taken
            # from the live slot when there is one, otherwise resolved from the
            # index, so the value always names the session the app itself scoped.
            "slot_key": getattr(slot, "key", None) or _slot_key(name),
            "status": await _effective_status(name, meta, slot),
            # Live worker state. The SPA drives its working indicator, document
            # skeleton and fast (2.5s) poll off this flag, and the list endpoint
            # already returns it -- omitting it here left every one of those dead
            # for the SELECTED spec, which is the only place they matter.
            "running": bool(getattr(slot, "running", False)) if slot is not None else False,
            "phase": phase,
            "files": files,
            "state": spec_state,
            "context": {
                "worktree_branch": _redact(str(meta.get("worktree_branch", ""))),
                "turns": turns,
                "tool_calls": tool_calls,
            },
        }
    )


async def _handle_messages(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    index = await _aload_index()
    if name not in index:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    state = request.app["state"]
    # Same reason as the detail handler: whichever endpoint touches a spec's slot
    # first must be the one that scopes it, or /api/chat wins the race unscoped.
    slot = await _ensure_worker_slot(state, name, index[name])
    if slot is None and state is not None:
        # Foreign or unscoped slot under our key (see _ensure_worker_slot). The
        # transcript belongs to that session, so serving it here would leak
        # somebody else's conversation into this app -- same refusal the detail
        # endpoint makes.
        return web.json_response(
            {"code": "slot_owned_by_another_app", "error": "this spec's chat session is owned by another app"}, status=409
        )
    return web.json_response(
        {
            "messages": await _serialize_messages(state, _slot_key(name)),
            "running": bool(getattr(slot, "running", False)) if slot else False,
        }
    )


async def _handle_message(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    index = await _aload_index()
    if name not in index:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    text = str(body.get("text", "")).strip()
    if not text:
        return web.json_response({"code": "text_required", "error": "text required"}, status=400)
    state = request.app["state"]
    # Re-reading commit BEFORE dispatch: the body read above awaits, so a
    # concurrent DELETE can land in that window. Stamping through the mutator
    # both refuses to resurrect a deleted spec and hands back the FRESH entry to
    # scope the slot from, instead of the pre-await snapshot.
    # Identity-pinned against the CLIENT'S captured spec_dir, not against the
    # index we just read: comparing the index to itself always matches, so the
    # check was vacuous. The SPA sends the spec_dir it rendered (from the detail
    # payload), which is what makes a stale tab detectable -- if the spec was
    # deleted and recreated elsewhere under the same name, that value no longer
    # matches and the instruction must not reach the replacement's agent. A caller
    # that sends no spec_dir cannot be pinned; it is then treated as unpinned
    # rather than refused, so an older client keeps working.
    # The slot key rides along because a directory does NOT identify a creation:
    # delete leaves the documents on disk, so a re-import at the same name AND
    # path passes a spec_dir check while being a different spec with a different
    # conversation -- and this instruction would land in the replacement's chat.
    claimed_dir = str(body.get("spec_dir", "") or "").strip()
    claimed_key = str(body.get("slot_key", "") or "").strip()
    fresh = await _touch_spec(
        name, expect_spec_dir=claimed_dir or None, expect_slot_key=claimed_key or None
    )
    if fresh is None:
        return web.json_response(
            {"code": "stale_client", "error": "spec was deleted or recreated; reload and retry"}, status=409
        )
    slot = await _ensure_worker_slot(state, name, fresh)
    if slot is None:
        # Another app owns this slot key (see _ensure_worker_slot). Refuse rather
        # than dispatching a turn into a session we do not own.
        return web.json_response(
            {"code": "slot_owned_by_another_app", "error": "this spec's chat session is owned by another app"}, status=409
        )
    # Re-pin immediately before dispatch. _ensure_worker_slot awaits (it revalidates
    # the working dir off the event loop), so a delete can start AND finish between
    # the check above and this line -- handing the turn to a slot whose spec is gone.
    # Nothing awaits between this refusal and _dispatch_turn, which is synchronous.
    #
    # BOTH pins come from `fresh` -- the entry this request already verified -- not
    # from the client body. `slot_key` is optional on the wire (an older client that
    # sends none is treated as unpinned rather than refused), so reusing the CLAIMED
    # value here meant a request without one had no creation pin on the second check:
    # a delete plus a same-path recreate passed it, because spec_dir still matched,
    # and the stale slot wrote into the replacement's files. The captured value is
    # server-side data, so pinning to it is strictly stronger AND still lets an older
    # client through the first check.
    if (
        await _touch_spec(
            name,
            expect_spec_dir=fresh.get("spec_dir"),
            expect_slot_key=str(fresh.get("slot_key", "")) or None,
        )
        is None
    ):
        return web.json_response(
            {"code": "stale_client", "error": "spec was deleted or recreated; reload and retry"}, status=409
        )
    _dispatch_turn(state, slot, text)
    _audit("spec_message", name)
    return web.json_response({"ok": True})


async def _handle_handoff(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    index = await _aload_index()
    meta = index.get(name)
    if not meta:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    spec_dir = Path(meta["spec_dir"])
    working_dir = meta.get("working_dir", "")
    # Captured BEFORE the await below, so the reread can compare against the
    # identity this request started with rather than re-deriving one.
    started_slot_key = str(meta.get("slot_key", ""))
    # Parse and check the CLIENT's claim before the destructive call below, the
    # same ordering _handle_stop_execution documents. _prepare_handoff clears the
    # STOP sentinel, so a stale same-name execute that got this far would disarm a
    # replacement's Pause before any identity comparison had run.
    claimed = await _client_claim(request)
    if _client_identity_mismatch(claimed, spec_dir, started_slot_key):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    # One thread hop for every filesystem touch this handler needs: the identity
    # re-check, the tasks.md gate, clearing a stale STOP sentinel from a prior run
    # (symlink-safe), and resolving the sentinel path the autonudge arm requires.
    # name + started_slot_key make the CLEAR itself conditional on identity, which
    # is the half a claim comparison cannot cover for a claimless request.
    has_tasks, sentinel_path = await asyncio.to_thread(
        _prepare_handoff, spec_dir, name, started_slot_key
    )
    if not has_tasks:
        return web.json_response(
            {"code": "tasks_missing", "error": "tasks.md does not exist yet — finish the Tasks phase first"}, status=409
        )
    # Reread AFTER the await as well: a delete+recreate can land during the thread
    # hop, and a stale request would then capture the REPLACEMENT's slot while its
    # own abort path -- correctly pinned to what it captured -- closed the new
    # session. This is what protects slot acquisition.
    current = await _aload_index()
    meta = current.get(name)
    # Pinned on the per-creation slot key as well as the directory. A delete +
    # re-import at the same name AND path leaves spec_dir identical, so the
    # directory alone cannot distinguish our spec from the replacement -- and the
    # slot_key check below only validates the CLIENT's claim, so a request that
    # carries no claim had no identity check at all.
    if (
        not meta
        or str(meta.get("spec_dir", "")) != str(spec_dir)
        or str(meta.get("slot_key", "")) != started_slot_key
    ):
        return web.json_response(
            {"code": "spec_changed_during_start", "error": "spec was deleted or recreated while starting; retry"}, status=409
        )
    working_dir = meta.get("working_dir", "")
    if _client_identity_mismatch(claimed, spec_dir, str(meta.get("slot_key", ""))):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    state = request.app["state"]
    # FAIL CLOSED. This used to swallow every failure and fall through to
    # _dispatch_turn "running a single turn" — which started an autonomous
    # execution turn WITHOUT passing the authorization chokepoint at all, so the
    # slot-ownership check, the message limit, the sensitive-sentinel refusal and
    # the SEL audit were all skipped precisely when the machinery meant to
    # enforce them was unavailable. An unauthorized run is not a degraded run.
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    if svc is None or authorize_and_add_nudge is None:
        _audit("spec_handoff_denied", f"{name}: autonudge unavailable", outcome="denied")
        return web.json_response(
            {
                "code": "autonudge_unavailable",
                "error": (
                    "autonomous execution is unavailable: the auto-nudge service is not "
                    "running, so the run cannot be authorized or bounded"
                ),
            },
            status=503,
        )

    # CLAIM the run before any side effect: one atomic compare-and-set that both
    # refuses a second handoff and records the execution state. Reading the status
    # here and committing it further down was not a guard at all -- two concurrent
    # requests both read "planning", both passed, and both dispatched, so Pause
    # cancelled one prompt while the other drained and kept editing the user's
    # files. The decision and the write are now the same index mutation.
    #
    # Recording BEFORE arming also matters on its own: the arm is shielded and
    # survives a restart, so arming first left a window where a shutdown persisted
    # a timer with no execution state -- and the restored timer ran something Pause
    # could not stop, because Pause keys off that state.
    captured_slot_key = str(meta.get("slot_key", ""))
    live_slot = state.get_slot(_slot_key(name)) if state is not None else None
    try:
        claim, committed = await _claim_execution(
            name,
            expect_spec_dir=str(spec_dir),
            expect_slot_key=captured_slot_key,
            live_running=bool(getattr(live_slot, "running", False)),
        )
    except Exception:
        # Nothing has been created yet, so there is nothing to unwind -- but the
        # run must not proceed on an unrecorded state, because Pause keys off it.
        logger.warning("could not claim execution for %s", name, exc_info=True)
        return web.json_response(
            {"code": "exec_state_write_failed", "error": "could not record execution state; the run was not started"},
            status=500,
        )
    if claim == _CLAIM_TAKEN:
        return web.json_response(
            {"code": "already_executing", "error": "this spec is already building; pause it before starting again"},
            status=409,
        )
    if claim != _CLAIM_OK:
        return web.json_response(
            {"code": "spec_changed_during_start", "error": "spec was deleted or recreated while starting; retry"}, status=409
        )
    meta = committed or meta
    # Did the slot ALREADY exist? The unwind path below must only close a slot
    # this request created: a pre-existing one carries the user's conversation
    # (and possibly a running turn), and destroying it because a later index
    # write failed loses work the handoff never owned.
    slot_pre_existed = live_slot is not None
    # Tool calls are NOT auto-approved: the user approves (or clicks Trust) from
    # the embedded chat's approval card. The run is bounded by the STOP SENTINEL,
    # the Stop button, and a capped nudge cycle count.
    slot = await _ensure_worker_slot(state, name, meta)
    if slot is None:
        # Another app owns this slot key (see _ensure_worker_slot). Refuse rather
        # than dispatching a turn into a session we do not own -- and give the
        # claim back, or the spec stays marked executing with nothing running.
        await _touch_spec(
            name,
            expect_spec_dir=str(spec_dir),
            expect_slot_key=captured_slot_key or None,
            status="planning",
            exec_started_at=0.0,
            exec_arming_at=0.0,
        )
        return web.json_response(
            {"code": "slot_owned_by_another_app", "error": "this spec's chat session is owned by another app"}, status=409
        )
    prompt = _exec_prompt(name, spec_dir, working_dir)
    # Arm the autonudge loop through the SHARED AUTHORIZATION CHOKEPOINT so this
    # app enforces the same slot-ownership checks, message limits, sensitive
    # stop_sentinel_path refusal and SEL audit as POST /api/autonudge. Calling
    # svc.add directly (as this did) bypassed all of it, and max_cycles=0 meant
    # an unbounded loop. Fails CLOSED: if authorization is refused we do not
    # dispatch the autonomous turn.

    async def _release(reason: str, *, loop_id: str | None = None) -> None:
        """Undo ONLY what this request created, in the reverse order it was created.

        Both the loop and the slot are looked up by name, so an unpinned abort
        would cancel the loop and destroy the slot of a same-name spec that
        replaced ours.
        """
        if loop_id:
            try:
                await _remove_nudge_loop(name, only_loop_id=loop_id)
            except Exception:
                # Best-effort HERE only: this is already an abort path, and the
                # reason that brought us here is the story worth surfacing. Logged
                # loudly because a surviving loop can still nudge.
                logger.warning(
                    "spec %s: could not remove the armed loop while unwinding",
                    name,
                    exc_info=True,
                )
        # Put the recorded state back. A persisted "executing" with nothing armed
        # shows a run that no timer will ever advance, and the already-building
        # refusal would then reject every retry.
        try:
            await _touch_spec(
                name,
                expect_spec_dir=str(spec_dir),
                expect_slot_key=captured_slot_key or None,
                status="planning",
                exec_started_at=0.0,
                exec_arming_at=0.0,
            )
        except Exception:
            logger.warning(
                "spec %s: could not clear the execution state while unwinding",
                name,
                exc_info=True,
            )
        if not slot_pre_existed:
            await _teardown_worker_slot(state, name, only_slot=slot)
        _audit("spec_handoff_aborted", f"{name}: {reason}", outcome="denied")

    try:
        armed_loop, authz_err, _status = await authorize_and_add_nudge(
            svc=svc,
            state=state,
            slot_key=slot.key,
            message=prompt,
            idle_secs=120,
            max_cycles=_EXEC_MAX_CYCLES,
            stop_sentinel_path=sentinel_path,
            source="app:spec-builder",
            caller=str(request.get("user") or ""),
        )
    except Exception:
        logger.warning("autonudge arm raised for %s — refusing handoff", name, exc_info=True)
        await _release("authorization raised")
        _audit("spec_handoff_denied", f"{name}: authorization raised", outcome="denied")
        return web.json_response(
            {"code": "authorization_failed", "error": "could not authorize autonomous execution"}, status=503
        )
    if authz_err:
        # No trust to revoke (we never granted any), and revoking here would undo
        # a trust decision the user made themselves. The recorded execution state
        # IS ours to revoke, and _release does that.
        await _release(f"authorization refused: {authz_err}")
        _audit("spec_handoff_denied", f"{name}: {authz_err}", outcome="denied")
        return web.json_response(
            {"code": "authorization_refused", "error": f"could not start autonomous execution: {authz_err}"}, status=403
        )
    # Arming awaits too, so re-verify the creation once more. A DELETE landing in
    # that window tears down the slot and the loops it can see BY NAME -- ours
    # arrives after, and would be left nudging a spec that no longer exists. The
    # old arm-then-commit order caught this at the commit; the reorder above has to
    # catch it here instead.
    if (
        await _touch_spec(
            name,
            expect_spec_dir=str(spec_dir),
            expect_slot_key=captured_slot_key or None,
            # The loop is armed: the reconciler can see it now, so the pre-arm
            # exemption must end here rather than expire on the grace window.
            exec_arming_at=0.0,
        )
        is None
    ):
        await _release(
            "deleted or recreated during authorization",
            loop_id=getattr(armed_loop, "id", None),
        )
        return web.json_response(
            {"code": "spec_changed_during_start", "error": "spec was deleted or recreated while execution was starting"},
            status=409,
        )
    _dispatch_turn(state, slot, prompt)
    _audit("spec_handoff", name)
    return web.json_response({"ok": True, "status": "executing"})


#: Returned when the client's rendered spec identity no longer matches the index.
_STALE_CLIENT_ERROR = "spec was deleted or recreated; reload and retry"


class _ClientClaim(NamedTuple):
    """What the client believes it is acting on. Both fields are optional."""

    spec_dir: str
    slot_key: str


async def _client_claim(request: web.Request) -> _ClientClaim:
    """The identity the CLIENT rendered, from the JSON body or the query string.

    Carries the per-creation ``slot_key`` as well as ``spec_dir``, because a
    directory does NOT identify a creation: deleting a spec leaves its documents on
    disk by design, so re-importing under the same name AND path produces a
    different spec with the same spec_dir -- and a stale tab's Pause would then
    cancel the replacement's run. The slot key is minted per creation, so it is the
    field that actually distinguishes them.

    Optional by design: a control that sends nothing cannot be pinned (an older tab
    predates these fields), so callers treat "" as unpinned rather than refusing. A
    DELETE carries them as query parameters because it has no body.
    """
    dir_claim = str(request.query.get("spec_dir", "") or "").strip()
    key_claim = str(request.query.get("slot_key", "") or "").strip()
    if not (dir_claim and key_claim) and request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            dir_claim = dir_claim or str(body.get("spec_dir", "") or "").strip()
            key_claim = key_claim or str(body.get("slot_key", "") or "").strip()
    return _ClientClaim(dir_claim, key_claim)


def _client_identity_mismatch(
    claim: _ClientClaim, actual_dir: Path | str, actual_slot_key: str = ""
) -> bool:
    """True when the client named a DIFFERENT spec than the one we resolved.

    Either field is enough to refuse, and the SLOT KEY is the decisive one: two
    specs can share a directory across a delete + re-import, but never a
    per-creation key. A field the client did not send is not compared, so an older
    tab keeps working (unpinned, as before).
    """
    if claim.spec_dir and claim.spec_dir != str(actual_dir):
        return True
    return bool(claim.slot_key) and bool(actual_slot_key) and claim.slot_key != actual_slot_key


async def _handle_stop_execution(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    # Parse the body FIRST. Reading it is an await, so doing it after the index
    # read reopened the very window the capture below is meant to close: a
    # delete+recreate landing while a slow request body arrived left the index
    # snapshot (and the identity check against it) describing the OLD spec while
    # the loop id and slot captured afterwards belonged to the REPLACEMENT, whose
    # run this request would then cancel.
    claimed = await _client_claim(request)
    index = await _aload_index()
    meta = index.get(name)
    if not meta:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    spec_dir = Path(meta["spec_dir"])
    # From here to the capture there is NO await: the halt writes a sentinel,
    # removes the nudge loop and cancels the running turn, and all three are
    # looked up by name.
    if _client_identity_mismatch(claimed, spec_dir, str(meta.get("slot_key", ""))):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    state = request.app.get("state")
    # The creation this request verified, carried to the commit below.
    captured_slot_key = str(meta.get("slot_key", ""))
    captured_loop_id = _exec_loop_id(name)
    captured_slot = state.get_slot(_slot_key(name)) if state is not None else None
    try:
        await _halt_execution(
            state,
            name,
            spec_dir,
            reason="user stop",
            only_loop_id=captured_loop_id,
            only_slot=captured_slot,
            expect_slot_key=captured_slot_key,
        )
    except Exception:
        # A failed loop removal means the run can still nudge itself; saying
        # "stopped" would be false and the user would not retry.
        logger.warning("spec %s: halt failed", name, exc_info=True)
        _audit("spec_stop_failed", name, outcome="denied")
        return web.json_response(
            {"code": "stop_failed", "error": "could not stop the run; it may still be working — retry"}, status=503
        )
    # Re-reading commit: halting awaits, so a concurrent DELETE in that window
    # must not be undone by writing back the snapshot above. The halt itself is
    # idempotent, so nothing is lost by reporting the deletion instead.
    if (
        await _touch_spec(
            name,
            expect_spec_dir=str(spec_dir),
            expect_slot_key=captured_slot_key or None,
            status="planning",
        )
        is None
    ):
        # Gone, or recreated elsewhere under the same name -- in which case the
        # STOP sentinel we just wrote belongs to the OLD spec and this request
        # must not mark the NEW one as stopped.
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    _audit("spec_stop_execution", name)
    return web.json_response({"ok": True, "status": "planning"})


async def _handle_delete(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    # Body first, then the index: see _handle_stop_execution. A body await
    # between the two would let a replacement spec be the thing torn down.
    claimed = await _client_claim(request)
    index = await _aload_index()
    if name not in index:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    doomed_dir = str(index[name].get("spec_dir", ""))
    # The creation we verified, carried to the commit below so the entry that gets
    # dropped is the one this request checked.
    doomed_slot_key = str(index[name].get("slot_key", ""))
    if _client_identity_mismatch(claimed, doomed_dir, str(index[name].get("slot_key", ""))):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    # Tombstone FIRST. Writing it after the teardown left a window in which the
    # entry was already gone while the documents were still on disk and
    # untombstoned: a list poll landing there re-adopted the markdown through
    # discovery, so the DELETE returned 200 with the spec still listed. The
    # tombstone is what discovery consults, so it has to exist before the entry
    # stops being visible. It is cleared again on every path that does not delete.
    await asyncio.to_thread(_remember_deleted, doomed_dir)
    # RESERVE the name rather than dropping the entry. Popping it freed the name for
    # the duration of the teardown, so a same-name create could take it and the
    # rollback had to restore under `<name>-2` -- which carries a per-creation slot
    # key that only the ORIGINAL name may own, leaving the conversation unreachable.
    # Marking keeps the entry (hidden from the list), so the name cannot be taken and
    # a rollback restores the original with its key intact.
    if not await _mark_deleting(
        name, expect_spec_dir=doomed_dir, expect_slot_key=doomed_slot_key
    ):
        await asyncio.to_thread(_forget_deleted, doomed_dir)
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    # RESERVED -- only now capture the runtime. Capturing before the reservation left
    # a window where a message could materialize a NEW slot (or arm a new loop) that
    # this capture had already passed: the teardown below then cancelled a stale
    # handle while the freshly-created session kept running the agent against files
    # the user had just deleted. With the marker set first, _touch_spec refuses that
    # message, so nothing new can appear between here and the teardown.
    state = request.app.get("state")
    doomed_loop_id = _exec_loop_id(name)
    doomed_slot = state.get_slot(_slot_key(name)) if state is not None else None
    # Stop any execution loop; leave the .md files on disk (they are the user's
    # project files under .kiro/specs) — only drop app bookkeeping + the slot.
    try:
        await _remove_nudge_loop(name, only_loop_id=doomed_loop_id)
    except Exception:
        # Fail the delete rather than report success: the entry is still in the
        # index, so a retry is meaningful, and the persisted loop cannot rearm
        # against a same-name spec re-imported later. Release the reservation and
        # the tombstone too -- both were taken above, and leaving either behind
        # would hide a spec the user still has from their own list.
        logger.warning("spec %s: loop removal failed — delete aborted", name, exc_info=True)
        await _unmark_deleting(name, expect_spec_dir=doomed_dir)
        await asyncio.to_thread(_forget_deleted, doomed_dir)
        _audit("spec_delete_aborted", name, outcome="denied")
        return web.json_response(
            {"code": "loop_removal_failed", "error": "could not stop this spec's background loop; nothing was deleted"},
            status=503,
        )
    # NOW tear the worker slot down: removing only the nudge loop left the
    # in-flight turn ALIVE, so the agent kept running and editing the user's files
    # after they deleted the spec, and re-creating the same name resurrected the old
    # transcript (get_or_create_slot keys off the slot name). Mirrors the gateway's own
    # slot-delete order internally: pop from the registry, cancel and await the task,
    # then persist as closed.
    #
    # require_archive: the conversation is the user's data. A failed history write used
    # to be logged at DEBUG while the delete returned 200 -- the transcript silently
    # gone. Now the reservation is released instead, so the spec is still listed with
    # its session intact and the retry is meaningful.
    if not await _teardown_worker_slot(
        state, name, only_slot=doomed_slot, require_archive=True
    ):
        released = await _unmark_deleting(name, expect_spec_dir=doomed_dir)
        # The spec lives again, so the tombstone must go: leaving it would suppress
        # the documents from discovery for a spec that was never deleted.
        await asyncio.to_thread(_forget_deleted, doomed_dir)
        detail = (
            "nothing was deleted"
            if released
            else "nothing was deleted; the spec may need a reload to reappear"
        )
        return web.json_response(
            {
                "code": "archive_failed",
                "error": f"could not archive this spec's conversation; {detail}",
            },
            status=503,
        )

    def _pop_if_same(idx: dict) -> bool:
        # Identity-pinned: a same-name spec cannot exist here (the name was reserved),
        # but the entry is still re-read under the lock, so pin it anyway rather than
        # trusting the snapshot this handler loaded before the awaits.
        meta = idx.get(name)
        if meta is None or str(meta.get("spec_dir", "")) != doomed_dir:
            return False
        actual_key = str(meta.get("slot_key", ""))
        if doomed_slot_key and actual_key and actual_key != doomed_slot_key:
            return False
        del idx[name]
        return True

    if not await _mutate_index(_pop_if_same):
        # The conversation is ALREADY archived, so un-deleting would be the lie the
        # ordering above exists to prevent. The reservation stays, which keeps the
        # spec hidden and makes a retry idempotent: it re-runs a no-op teardown and
        # removes the entry.
        logger.warning("spec %s: archived but the index entry could not be removed", name)
        return web.json_response(
            {
                "code": "index_write_failed",
                "error": (
                    "this spec's conversation was archived but its record could not be "
                    "removed; retry the delete"
                ),
            },
            status=503,
        )
    _audit("spec_delete", name)
    return web.json_response({"ok": True})


def register_routes(app: web.Application) -> None:
    """Register this app's routes on the gateway's aiohttp Application.

    Signature/hardcoded-path convention matches every other builtin app (see
    issue_radar/backend/routes.py:register_routes) — confirmed against the real
    call site in dashboard/server.py (``_mod.register_routes(app)``, single
    argument). Handlers are wrapped in ``_require_enabled`` because builtin
    routes are wired once at startup regardless of the app's enabled state.

    Deliberately creates NOTHING: this runs during ``start_dashboard`` on the
    event loop, and a ``KIROCREW_HOME`` on stalled network storage would freeze
    gateway startup on a directory the app may never need. Every writer
    (``_save_index`` / ``_save_settings``) mkdirs on its own worker thread.
    """
    base = f"/api/apps/{APP_NAME}"
    app.router.add_get(f"{base}/settings", _require_enabled(_handle_get_settings))
    app.router.add_put(f"{base}/settings", _require_enabled(_handle_put_settings))
    # POST alias: the SPA page uses POST for settings writes.
    app.router.add_post(f"{base}/settings", _require_enabled(_handle_put_settings))
    app.router.add_get(f"{base}/repo-info", _require_enabled(_handle_repo_info))
    # Unified folder-picker feed (dirs + is_git + recents) for the SPA page.
    app.router.add_get(f"{base}/browse", _require_enabled(_handle_browse))
    app.router.add_get(f"{base}/specs", _require_enabled(_handle_list))
    app.router.add_post(f"{base}/specs", _require_enabled(_handle_create))
    app.router.add_get(f"{base}/specs/{{name}}", _require_enabled(_handle_get))
    app.router.add_get(f"{base}/specs/{{name}}/messages", _require_enabled(_handle_messages))
    app.router.add_post(f"{base}/specs/{{name}}/message", _require_enabled(_handle_message))
    app.router.add_post(f"{base}/specs/{{name}}/handoff", _require_enabled(_handle_handoff))
    # Alias: the SPA page calls this "execute".
    app.router.add_post(f"{base}/specs/{{name}}/execute", _require_enabled(_handle_handoff))
    app.router.add_post(f"{base}/specs/{{name}}/stop", _require_enabled(_handle_stop_execution))
    app.router.add_delete(f"{base}/specs/{{name}}", _require_enabled(_handle_delete))
    logger.info("spec-builder: registered app routes under %s", base)
