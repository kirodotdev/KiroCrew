"""Where the MCP config files are, and reading, refusing and writing them.

The server-name check, the Kiro-global ``mcp.json`` path and the Kiro Crew one, the extra
provider scopes an edition adds, the refusal of a commented (JSONC) file, the atomic write
and its off-loop form, resolving which key of a file names a server, and the synchronous
config lock the cleanup sweep takes.

Part of the ``kiro_crew.dashboard.handlers.mcp`` family: callers and tests reach every
name here as ``kiro_crew.dashboard.handlers.mcp.<name>``, and a patch there reaches this
module too (:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from functools import partial
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.agent import _atomic_json_write
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.user_json import has_json_comments, loads_user_jsonc

logger = logging.getLogger("kiro_crew.dashboard.handlers.mcp")


# Allowlist pattern for MCP server names.  Matches the convention used
# in AIM / kiro-cli (alphanumerics, dashes, underscores, slashes, dots,
# ``@`` for scoped names like ``@org/server``, and ``:`` for app-provided
# keys like ``<app>:<server>`` as enumerated from ``~/.kiro/agents/*.json``)
# and defends against command-injection into subprocess calls that pass the
# name as an argv element (e.g. a capability-manager `uninstall <name>` argv).
# A colon is not a shell metacharacter and names only ever travel as
# list-form argv elements, so admitting it does not widen that surface.
#
# The leading char must be alphanumeric or ``@`` so a name can't begin
# with ``.``, ``/``, or ``:``.  Path-traversal sequences (``..``) are
# rejected separately at validation time below.
_VALID_MCP_NAME_RE = re.compile(r"^[@a-zA-Z0-9][@a-zA-Z0-9/_.:-]*$")
_MAX_MCP_NAME_LEN = 128


def _is_valid_mcp_name(name: str) -> bool:
    """Return True if ``name`` is a well-formed, non-traversal MCP name."""
    if not name or len(name) > _MAX_MCP_NAME_LEN:
        return False
    if ".." in name:  # reject path traversal even if it matches the charset
        return False
    return bool(_VALID_MCP_NAME_RE.match(name))


_GLOBAL_MCP_JSON = Path.home() / ".kiro" / "settings" / "mcp.json"
# Surface label carried into the provenance decision so a declined rewrite names
# the file it declined to touch.
_KIRO_GLOBAL_SURFACE = "~/.kiro/settings/mcp.json"


# File-based lock for mcp.json — shared with bridges_mcp_config.py so that app
# registration and dashboard MCP handlers coordinate properly.
# Uses fcntl.flock on a sidecar .lock file (works cross-process too).
_MCP_LOCK_PATH = _GLOBAL_MCP_JSON.with_suffix(".lock")


class _McpFileLockSync:
    """Synchronous sibling of :class:`_McpFileLock` for the guaranteed-cleanup
    sweep.

    The sweep is dispatched to a worker thread via ``run_in_executor`` (see the
    ``api_mcp_apply`` finally), so this blocking ``acquire_lock`` runs OFF the
    event loop — it must not, and does not, block the loop thread. Running it on
    a worker thread is what makes the cleanup both deadlock-free and
    run-to-completion: (a) the event loop stays free, so any other task that
    currently holds the MCP lock can resume and release it (a loop-blocking
    acquire here would wedge that release → deadlock); and (b) a worker thread is
    not cancelled when the request task is, so the purge finishes even when the
    apply was cancelled mid-flight.
    """

    def __enter__(self) -> None:
        try:
            _GLOBAL_MCP_JSON.parent.mkdir(parents=True, exist_ok=True)
            _MCP_LOCK_PATH.touch(exist_ok=True)
            # Non-truncating "r+", kept inline for the same reason as
            # :class:`_McpFileLock` in handlers/mcp.py.
            fd = open(_MCP_LOCK_PATH, "r+")
        except OSError as exc:
            # Same report, same rationale as :class:`_McpFileLock`: the sweep
            # that takes this lock runs under a request's finally, so its
            # refusal is otherwise swallowed with the rest of the cleanup.
            logger.warning(
                "cannot open the mcp config lock %s (%s) -- writes to %s cannot be "
                "serialized, so this update is being skipped",
                _MCP_LOCK_PATH,
                exc.strerror or exc,
                _GLOBAL_MCP_JSON,
            )
            raise
        try:
            platform_compat.acquire_lock(fd.fileno(), exclusive=True)
        except BaseException as exc:
            fd.close()
            # OSError only, as in :class:`_McpFileLock`: the lock's own
            # refusal, never this caller's lifecycle.
            if isinstance(exc, OSError):
                logger.warning("mcp config lock %s: %s", _MCP_LOCK_PATH, exc)
            raise
        self._fd = fd

    def __exit__(self, *args: Any) -> None:
        try:
            platform_compat.release_lock(self._fd.fileno())
        finally:
            self._fd.close()


def _get_mcp_lock_sync() -> _McpFileLockSync:
    """Return a SYNCHRONOUS MCP config file lock (used by the cleanup sweep)."""
    return _McpFileLockSync()


class AmbiguousServerKey(LookupError):
    """*name* is the alias of two or more raw keys in one config map, none exact.

    Raised by :func:`_config_key_for` so that no caller can act on a guess. A
    write that picked one spelling would edit a configuration the user did not
    name; a write that took every spelling would delete a DISTINCT server --
    ``team/foo`` and ``other:team/foo`` both alias to ``team-foo`` and can be two
    servers. Readers treat it as "no entry in this map"; writers refuse and say so.
    """

    def __init__(self, name: str, count: int) -> None:
        super().__init__(
            f"server {name!r} is the alias of {count} entries in this config and none is "
            f"keyed {name!r}; edit the file directly"
        )
        self.name = name
        self.count = count


AmbiguousServerKey.__module__ = "kiro_crew.dashboard.handlers.mcp"


def _resolve_key_from_read(servers: Any, name: str, preferred: str | None = None) -> str | None:
    """The ONE key a writer acts on, computed from the read it is about to write.

    :func:`_config_key_for` on *servers* -- the map the caller has just loaded
    and will write back -- so presence, absence and spelling all come from the
    same read; nothing decided earlier is trusted about what the file holds now.
    *preferred* is the key the request-time preflight resolved for this store
    and it breaks exactly one tie: when the read is ambiguous (two raw keys
    alias to *name*, none exact -- a colliding sibling landed since the
    preflight) and *preferred* is one of them, that entry is the one the user's
    row stood for and it is the one acted on. It never asserts presence: a
    *preferred* key absent from the read is simply not there.
    """
    try:
        return _config_key_for(servers, name)
    except AmbiguousServerKey:
        if (
            preferred is not None
            and isinstance(servers, dict)
            and preferred in servers
            and mcp_server_alias(preferred) == name
        ):
            return preferred
        raise


def _config_key_for(servers: Any, name: str) -> str | None:
    """The ONE key of a ``mcpServers`` map that the row *name* stands for.

    A row is listed under its canonical alias (``list_servers`` step 3b folds
    ``npm:@playwright/mcp`` into ``playwright-mcp``), but the scope files keep
    whatever key the user or the installer wrote, so a lookup by the row's name
    alone MISSES a slash-named entry: the table then stamps the row from nothing
    (a shared disable reads as ``kirocrewManaged: false`` with no file to name)
    and an Uninstall reports ``removed`` while the definition stays in the file.

    Resolution is to exactly one key, never a set. The exact key wins when the
    map holds it: that is the entry step 3b keeps as the row's representative,
    and a raw key beside it (``team/foo`` beside ``team-foo``) is another
    server's configuration, which an Uninstall of this row must not touch. With
    no exact key, the single raw key whose alias is *name* is the row's source.
    Two or more raw keys and no exact key raise :class:`AmbiguousServerKey`;
    ``None`` when nothing names the server. A slash-free name is its own alias,
    so ordinary rows resolve to their own key.
    """
    if not isinstance(servers, dict):
        return None
    if name in servers:
        return name
    matches = [key for key in servers if isinstance(key, str) and mcp_server_alias(key) == name]
    if len(matches) > 1:
        raise AmbiguousServerKey(name, len(matches))
    return matches[0] if matches else None


def _config_entry_for(servers: Any, name: str) -> dict[str, Any] | None:
    """The entry :func:`_config_key_for` resolves *name* to, for a READ.

    ``None`` when no key names the server, when the resolved value is not a
    mapping, or when the name is ambiguous -- a read has nothing to stamp from
    a map it cannot pin to one entry; the row's own aggregate flags, which
    ``list_servers`` computed over every raw key, still carry the disable.
    """
    try:
        key = _config_key_for(servers, name)
    except AmbiguousServerKey:
        return None
    if key is None:
        return None
    entry = servers[key]
    return entry if isinstance(entry, dict) else None


def _string_identifier(body: dict, field: str) -> tuple[str, web.Response | None]:
    """Read one mutation identifier the dashboard's forms post.

    The field must be a STRING before normalization: a truthy non-string
    (array/object/number from a malformed client) otherwise reaches ``.strip()``
    and surfaces as HTTP 500 before any validation runs — past the point where
    such a handler would already hold the config lock or have touched
    persistence. Missing or blank leaves the handlers' required-field responses
    alone; only the TYPE contract is enforced here, and its 400 carries a stable
    machine-readable ``code``.
    """
    raw = body.get(field)
    if raw is None:
        raw = ""
    if isinstance(raw, str):
        return raw.strip(), None
    return "", web.json_response(
        {"error": f"{field} must be a string", "code": f"mcp.{field}_not_string"},
        status=400,
    )


# ─── The Kiro Crew mcp.json, provider scopes, reads and writes ──────────

# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage_store.py is the reference implementation.
_KIROCREW_MCP_JSON: Path | None = None


def _kirocrew_mcp_json() -> Path:
    """Kiro Crew-scope ``mcp.json`` path, resolved against the live data home."""
    return _KIROCREW_MCP_JSON if _KIROCREW_MCP_JSON is not None else data_home() / "mcp.json"


def _extra_mcp_scopes() -> list:
    """Edition-contributed provider MCP config scopes (CPP seam).

    Public Default returns ``[]`` so the core writes the Kiro global only; a
    companion contributes additional provider scopes (e.g. Claude Code's
    ``~/.claude.json``). Deferred context read so this module never imports the
    platform package at load; fails closed to no extra scopes.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    return safe_context_call(
        lambda: list(current_context().mcp_tooling.extra_mcp_scopes()),
        fallback_factory=list,
        log_message="extra_mcp_scopes lookup failed; using none",
    )


async def api_mcp_global_scopes(request: web.Request) -> web.Response:
    """GET /api/mcp/scopes — extra provider global MCP scopes (CPP seam).

    Returns the provider-specific global scopes an edition contributes via
    ``extra_mcp_scopes()``, so the dashboard's Installed-Integrations "Globals"
    column can render a badge per scope instead of hardcoding a provider name.
    The public Default returns ``[]`` (the core manages only the Kiro global,
    which the UI renders unconditionally); a companion returns e.g.
    ``[{"id": "ccGlobal", "label": "Claude"}]``. ``id`` is the presence/apply
    key (``f"{scope.id}Global"``) the UI uses for toggle + apply.
    """
    scopes = [{"id": f"{s.id}Global", "label": s.label or s.id} for s in _extra_mcp_scopes()]
    return web.json_response({"scopes": scopes})


#: Error code for a write refused because the file holds JSONC (see below).
MCP_CONFIG_HAS_COMMENTS = "mcp_config_has_comments"


def _has_json_comments_on_disk(path: Path) -> bool:
    """Whether *path* parses only as JSONC (comments or trailing commas).

    Readers accept such a file (``loads_user_jsonc``), but every writer here
    emits plain JSON, so writing it back would drop the user's comments and
    commented-out servers. Writers refuse instead of rewriting it.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return has_json_comments(text)


class McpConfigHasComments(web.HTTPConflict):
    """A write refused because its target file holds JSONC.

    An HTTP 409 with a coded JSON body, so a handler that lets it propagate
    answers the client directly.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(
            text=json.dumps(
                {
                    "error": (
                        f"{path} has comments or trailing commas; saving would rewrite "
                        "it as plain JSON and drop them. Remove them, or edit the file "
                        "directly."
                    ),
                    "code": MCP_CONFIG_HAS_COMMENTS,
                }
            ),
            content_type="application/json",
        )
        self.path = path


McpConfigHasComments.__module__ = "kiro_crew.dashboard.handlers.mcp"


def _user_mcp_config_paths() -> list[Path]:
    """The MCP config files a person edits by hand, which may hold JSONC."""
    return [
        _kirocrew_mcp_json(),
        _GLOBAL_MCP_JSON,
        *(scope.global_json for scope in _extra_mcp_scopes()),
    ]


def _refuse_commented_config(*paths: Path) -> None:
    """Raise :class:`McpConfigHasComments` when any of *paths* holds JSONC.

    The write helpers call it, so no writer can rewrite such a file; a handler
    with other side effects also calls it before the first of them, so its
    work is refused whole instead of stopping part way.
    """
    for path in paths:
        if _has_json_comments_on_disk(path):
            raise McpConfigHasComments(path)


def _load_json_or_empty(path: Path) -> dict[str, Any]:
    """Load JSON from a path; return empty dict on missing/malformed/unreadable.

    Catches the broad ``OSError`` (not just ``FileNotFoundError``) so a
    ``PermissionError`` or ``IsADirectoryError`` on a user-owned file like
    ``~/.claude.json`` won't crash ``api_mcp_apply`` mid-batch and leave
    partially-applied changes without a rebuild.
    """
    try:
        data = loads_user_jsonc(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


async def _offload_config_write(fn, /, *args, **kwargs):
    """Run a store-writing helper in a worker thread, surviving cancellation.

    A worker thread cannot be cancelled, so the write always runs to completion;
    the job here is to keep the CALLER from unwinding before it does. Awaiting
    the future through ``asyncio.shield`` and, on ``CancelledError``, re-awaiting
    it guarantees the write has finished before the cancellation propagates.
    Without this, a cancelled request task would release the MCP lock (or begin
    teardown) while the thread is still mutating the store, letting a concurrent
    purge interleave with the stale write.

    The drain is a LOOP, not a single re-await, because the drain is itself
    cancellable: a second cancellation arriving while it is in flight would
    cancel the drain and unwind the caller with the worker still writing —
    exactly the window this function exists to close. Each re-shield absorbs one
    more cancellation, so the guarantees hold under REPEATED cancellation:

    * the write always runs to completion before this returns or raises;
    * if cancelled, ``CancelledError`` is re-raised AFTER the drain;
    * an exception from the write still propagates (and, as before, takes
      precedence over a pending cancellation).

    Same pattern as the dangling-uninstall sweep in ``mcp_config_purge``.
    """
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, partial(fn, *args, **kwargs))
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(future)
            break
        except asyncio.CancelledError as exc:
            # Remember the FIRST cancellation and keep draining. Once the future
            # is done, ``await shield(...)`` returns without suspending, so this
            # cannot spin: the loop turns only on an actual new cancellation.
            if cancelled is None:
                cancelled = exc
    if cancelled is not None:
        raise cancelled
    return result


def _atomic_write(path: Path, data: dict) -> None:
    """Atomic JSON write; secret-aware for the store owned by Kiro Crew.

    That store is secret-bearing by construction (``env`` values and remote
    ``headers`` carry credentials), so it is published through
    :func:`kiro_crew.atomic_write.atomic_write` with ``restrict_to_owner=True``
    — the owner-only lockdown lands on the temp file BEFORE any payload byte,
    so the credential never exists in a file readable under the parent
    directory's inherited permissions.  The writer's default fail-closed
    policy is kept deliberately: a store this surface cannot protect is not
    written, and the caller's request fails visibly rather than publishing a
    credential another OS user could read.  On Windows the lockdown rewrites
    the file's DACL, so async callers hand the whole write to a worker
    thread rather than call this on the event loop.  Other paths (the shared
    global file, agent files) keep the mode-preserving helper — their
    lifecycles are owned by other tools.

    Raises :class:`McpConfigHasComments` when *path* is a hand-edited MCP config
    (:func:`_user_mcp_config_paths`) and the file on disk holds JSONC.
    """
    if path in _user_mcp_config_paths():
        _refuse_commented_config(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path == _kirocrew_mcp_json():
        atomic_write(
            path,
            (json.dumps(data, indent=2) + "\n").encode("utf-8"),
            restrict_to_owner=True,
        )
    else:
        _atomic_json_write(path, data)
