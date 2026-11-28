"""Which servers of a folder's own MCP files the person approved, and the check at start.

kiro-cli and KAS read ``<folder>/.kiro/settings/mcp.json`` and Claude Code reads
``<folder>/.mcp.json`` (:mod:`kiro_crew.project_mcp_files`) when a session starts in
that folder. Each session checks the record the gateway keeps of the servers the
person approved.

* :func:`check_folder` reads the folder's two files and returns each enabled server
  whose launch (:func:`fingerprint`) the record does not hold for that folder and file,
  plus each file that exists but cannot be read, which is held whole.
  The session holds those back (``agent_sdk.project_mcp_holds``) and the chat shows a
  ``mcp.project_server`` card for each server (``change_card_project_mcp``).
* :func:`approve` is the card's Apply, through the dashboard's owner-only route. It
  records the launch only when the file still holds the one the card showed.
* Gateway boot records the servers of known folders once
  (:func:`begin_boot_migration`): it reads the folder list at boot and scans the
  folders in the background. A check waits up to :data:`MIGRATION_WAIT_SECS` for
  that scan; until it is written, nothing counts as approved.

The record sits beside the shared-server launch approvals
(:data:`kiro_crew.mcp_gateway.launch_approval.APPROVALS_DIR`), so the same read-only
sandbox seal and file-tool refusal cover it: only gateway code writes it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from kiro_crew.config.paths import config_dir
from kiro_crew.mcp_cleanup import NOT_LAUNCH_KEYS
from kiro_crew.mcp_gateway.launch_approval import APPROVALS_DIR
from kiro_crew.project_mcp_files import KIRO_PROJECT_MCP_FILE, PROJECT_MCP_FILES

logger = logging.getLogger(__name__)

#: The record, inside the sealed launch-approvals directory.
PROJECT_APPROVALS_LEAF = f"{APPROVALS_DIR}/project-servers.json"
_VERSION = 1
#: The entry fields the card shows in their own rows. :func:`fingerprint` covers
#: these and every other field outside :data:`NOT_LAUNCH_KEYS` (the card shows
#: those by name, :func:`extra_fields`).
LAUNCH_KEYS = ("command", "args", "env", "url", "type", "headers", "cwd")
#: Bounds on the stored launch display and folder count, not on held servers.
_MAX_LAUNCH_CHARS = 16384
_MAX_FOLDERS = 4096
MAX_FOLDER_CHARS = 4096
MAX_SERVER_NAME_CHARS = 256
MAX_SERVERS_PER_FILE_RECORDED = 256
#: Bounds on the env and headers keys the record stores per approved entry.
_MAX_PAIR_KEYS = 256
_MAX_PAIR_KEY_CHARS = 256

STATUS_NEW = "new"
STATUS_CHANGED = "changed"

_LOCK = threading.Lock()
#: How long a session check waits for the boot scan before it holds every server.
MIGRATION_WAIT_SECS = 5.0
#: Set while no boot scan is running.
_MIGRATION_SETTLED = threading.Event()
_MIGRATION_SETTLED.set()


class ApprovalStale(Exception):
    """The file holds a launch other than the one the card showed."""


class RecordFull(Exception):
    """The record holds as many folders, or servers of one file, as it keeps."""


@dataclass(frozen=True)
class ProjectServer:
    """One enabled server of a folder's project MCP file."""

    folder: str
    file: str
    name: str
    entry: Mapping[str, Any]
    fingerprint: str
    status: str = STATUS_NEW
    #: What the record held for this name when it was last approved, for the card.
    approved: Mapping[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> str:
        return os.path.join(self.folder, self.file)


@dataclass(frozen=True)
class FolderCheck:
    """What a session-start check found in a folder's project MCP files."""

    #: The servers the person has not approved, new or changed.
    pending: list[ProjectServer] = field(default_factory=list)
    #: The paths of the files that exist but cannot be read; each is held whole.
    unreadable: tuple[str, ...] = ()


def store_path(home: str | os.PathLike[str] | None = None) -> Path:
    """``<crew home>/`` :data:`PROJECT_APPROVALS_LEAF`, following ``KIROCREW_HOME``."""
    if home is not None:
        return Path(home) / PROJECT_APPROVALS_LEAF
    env_home = os.environ.get("KIROCREW_HOME")
    return (Path(env_home) if env_home else config_dir()) / PROJECT_APPROVALS_LEAF


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(entry: Mapping[str, Any]) -> str:
    """The launch an entry names: one digest over every field but :data:`NOT_LAUNCH_KEYS`."""
    return _digest({k: v for k, v in entry.items() if k not in NOT_LAUNCH_KEYS})


def extra_fields(entry: Mapping[str, Any]) -> dict[str, Any]:
    """The fingerprinted fields outside :data:`LAUNCH_KEYS`, which the card shows by name."""
    return {
        str(k): v for k, v in entry.items() if k not in NOT_LAUNCH_KEYS and k not in LAUNCH_KEYS
    }


def launch_digest(entry: Mapping[str, Any]) -> str:
    """The full command, arguments and URL, independent of display text."""
    return _digest({k: entry[k] for k in ("command", "args", "url") if k in entry})


def launch_rows(entry: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """The card's launch rows, each ``(label, record key, text)``.

    A non-empty ``url`` string gets a ``URL`` row. A ``command`` that is not a blank
    string gets a ``Runs`` row, the full command line with its arguments; so does
    an entry with no URL row, so its arguments still show. An entry with both
    shows both, as which one the host uses depends on its ``type``.
    """
    import shlex

    command, url = entry.get("command"), entry.get("url")
    has_url = isinstance(url, str) and bool(url)
    rows: list[tuple[str, str, str]] = []
    blank = command is None or (isinstance(command, str) and not command.strip())
    if not blank or not has_url:
        parts = [] if command is None else [str(command)]
        args = entry.get("args")
        if isinstance(args, list):
            parts += [str(a) for a in args]
        rows.append(("Runs", "launch", shlex.join(parts)))
    if has_url:
        rows.append(("URL", "url", str(url)))
    return rows


def _pairs_digest(value: Any) -> dict[str, str]:
    """``{key: digest}`` of an env or headers map, so no value is copied into the record.

    Bounded by :data:`_MAX_PAIR_KEYS` and :data:`_MAX_PAIR_KEY_CHARS`; a key past
    either bound is left out of the record only, as :func:`fingerprint` covers the
    whole entry.
    """
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    overflow = False
    for k, v in value.items():
        key = str(k)
        if len(out) >= _MAX_PAIR_KEYS or len(key) > _MAX_PAIR_KEY_CHARS:
            overflow = True
            continue
        out[key] = _digest(v)
    if overflow:
        logger.warning(
            "project MCP approvals: an env, headers or field map exceeds %d keys or %d-char keys; "
            "the record keeps the keys within those bounds",
            _MAX_PAIR_KEYS,
            _MAX_PAIR_KEY_CHARS,
        )
    return out


def summary(entry: Mapping[str, Any]) -> dict[str, Any]:
    """What the record keeps of an approved entry: its launch, never an env value."""
    return {
        "fingerprint": fingerprint(entry),
        **{key: text[:_MAX_LAUNCH_CHARS] for _label, key, text in launch_rows(entry)},
        "launch_digest": launch_digest(entry),
        "env": _pairs_digest(entry.get("env")),
        "headers": _pairs_digest(entry.get("headers")),
        "extra": _pairs_digest(extra_fields(entry)),
        **{k: str(entry[k])[:_MAX_LAUNCH_CHARS] for k in ("type", "cwd") if k in entry},
    }


def folder_key(folder: str | os.PathLike[str] | None) -> str | None:
    """The record's key for *folder*: its resolved path, or ``None`` for no folder."""
    if not folder:
        return None
    text = os.fspath(folder)
    if not os.path.isabs(text) or not os.path.isdir(text):
        return None
    return os.path.realpath(text)


def _file_servers(
    folder: str, name: str, refusal: BaseException | None = None
) -> dict[str, Mapping[str, Any]] | None:
    """The enabled servers one project file names: ``{}`` when absent, ``None`` when unreadable.

    Read through the credential-gated settings reader every session-start read uses,
    so a link in the checkout does not make the gateway read what it points at. A
    file that reader refuses is read again for its server entries only
    (``read_server_entries``), so its servers are still held by name; ``None``
    only when that read fails too. kiro-cli skips an entry its ``disabled`` field
    mutes; Claude Code has no such field and starts every ``.mcp.json`` entry, so
    each one there counts as enabled.
    """
    from kiro_crew.agent_sdk.project_mcp_holds import read_server_entries, read_settings_file
    from kiro_crew.mcp_cleanup import mcp_entry_is_muted

    path = Path(folder) / name
    if not path.exists():
        return {}
    settings: dict[str, Any] = {}
    if refusal is None:
        try:
            settings = read_settings_file(path)
        except (OSError, ValueError) as exc:
            refusal = exc
    if refusal is not None:
        lenient = read_server_entries(folder, path, refusal)
        if lenient is None:
            logger.warning("project MCP check: %s could not be read; holding the file", path)
            return None
        logger.warning("project MCP check: %s read for its server names only", path)
        settings = lenient
    declared = settings.get("mcpServers") if isinstance(settings, dict) else None
    if not isinstance(declared, dict):
        return {}
    honours_mute = name == KIRO_PROJECT_MCP_FILE
    out: dict[str, Mapping[str, Any]] = {}
    for server, entry in declared.items():
        if not isinstance(server, str) or not server:
            continue
        if isinstance(entry, Mapping) and not (honours_mute and mcp_entry_is_muted(entry)):
            out[server] = entry
    return out


def folder_servers(
    folder: str | os.PathLike[str] | None, *, files: Iterable[str] = PROJECT_MCP_FILES
) -> list[ProjectServer]:
    """Every enabled server of *folder*'s selected project MCP files, in file order."""
    return _scan(folder, files)[0]


def _scan(
    folder: str | os.PathLike[str] | None, files: Iterable[str]
) -> tuple[list[ProjectServer], list[str]]:
    """:func:`folder_servers`, plus the paths of the selected files that cannot be read."""
    key = folder_key(folder)
    if key is None:
        return [], []
    return _scan_files(key, files)


def _scan_files(
    key: str, files: Iterable[str], refusal: BaseException | None = None
) -> tuple[list[ProjectServer], list[str]]:
    """Scan selected files, using only the names reader after a failed check."""
    selected = tuple(files)
    servers: list[ProjectServer] = []
    unreadable: list[str] = []
    for name in PROJECT_MCP_FILES:
        if name not in selected:
            continue
        declared = _file_servers(key, name, refusal)
        if declared is None:
            unreadable.append(os.path.join(key, name))
            continue
        for server, entry in declared.items():
            try:
                fp = fingerprint(entry)
            except (TypeError, ValueError, RecursionError):
                # An empty fingerprint holds the server but cannot authorize a launch.
                fp = ""
                logger.warning("project MCP check: an entry cannot be fingerprinted; holding it")
            servers.append(ProjectServer(key, name, server, entry, fp))
    return servers, unreadable


def hold_files(folder: str, files: Iterable[str], refusal: BaseException) -> FolderCheck:
    """Hold readable names after a failed check, or the whole file. Never raises."""
    pending: list[ProjectServer] = []
    unreadable: list[str] = []
    for name in files:
        path = os.path.join(folder, name)
        if not os.path.lexists(path):
            continue
        try:
            servers, failed = _scan_files(folder, (name,), refusal)
        except Exception:  # noqa: BLE001 - a failed names read must not stop a session
            logger.warning("project MCP names check failed; holding the file", exc_info=True)
            unreadable.append(path)
            continue
        pending.extend(servers)
        if not servers:
            unreadable.extend(failed or [path])
    return FolderCheck(pending, tuple(unreadable))


def _read_store(path: Path) -> dict[str, Any] | None:
    """The record, ``None`` when absent; an unreadable one reads as approving nothing."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        logger.warning("project MCP approvals: record unreadable; approving no project server")
        return {"version": _VERSION, "folders": {}}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("project MCP approvals: record is not valid JSON; approving none")
        return {"version": _VERSION, "folders": {}}
    if not isinstance(raw, dict) or raw.get("version") != _VERSION:
        return {"version": _VERSION, "folders": {}}
    if not isinstance(raw.get("folders"), dict):
        raw["folders"] = {}
    raw["folders"] = {
        folder: {file: servers for file, servers in files.items() if isinstance(servers, dict)}
        for folder, files in raw["folders"].items()
        if isinstance(files, dict)
    }
    return raw


def _write_store(path: Path, doc: Mapping[str, Any]) -> None:
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.owner_only_files import mkdirs_owner_only

    mkdirs_owner_only(path.parent)
    atomic_write(path, json.dumps(doc, indent=2, sort_keys=True) + "\n", mode=0o600)


def _record(doc: dict[str, Any], server: ProjectServer) -> bool:
    """Add *server* to *doc*; whether it was written.

    At the folder bound, the folders that are gone from disk are dropped first.
    """
    if (
        not server.fingerprint
        or len(server.folder) > MAX_FOLDER_CHARS
        or len(server.name) > MAX_SERVER_NAME_CHARS
    ):
        return False
    folders = doc.setdefault("folders", {})
    if server.folder not in folders and len(folders) >= _MAX_FOLDERS:
        for gone in [f for f in folders if not os.path.isdir(f)]:
            del folders[gone]
    if server.folder not in folders and len(folders) >= _MAX_FOLDERS:
        logger.warning(
            "project MCP approvals: record holds %d folders; not adding more", _MAX_FOLDERS
        )
        return False
    files = folders.setdefault(server.folder, {})
    servers = files.setdefault(server.file, {})
    if server.name not in servers and len(servers) >= MAX_SERVERS_PER_FILE_RECORDED:
        return False
    servers[server.name] = summary(server.entry)
    return True


def _known_folders() -> list[str]:
    """The folders Kiro Crew already ran chats in: the project picker's recent list."""
    try:
        raw = json.loads((config_dir() / "recent_projects.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [d for d in raw if isinstance(d, str) and d] if isinstance(raw, list) else []


def begin_boot_migration() -> Callable[[], None] | None:
    """Freeze the known folders for the one-time record; the scan to run in the background.

    ``None`` when a record already exists. Reads only the record's presence and
    ``recent_projects.json`` here, so a project picked after boot is never
    recorded as approved. Until the returned scan ends, a check waits up to
    :data:`MIGRATION_WAIT_SECS` for it (:func:`check_folder`).
    """
    path = store_path()
    with _LOCK:
        if os.path.exists(path):
            return None
    folders = list(dict.fromkeys(_known_folders()))
    _MIGRATION_SETTLED.clear()

    def scan() -> None:
        try:
            _migrate(path, folders)
        finally:
            _MIGRATION_SETTLED.set()

    return scan


def _migrate(path: Path, folders: list[str]) -> None:
    """Record the servers of *folders*. Blocking; never raises."""
    try:
        servers = [s for folder in folders for s in folder_servers(folder)]
        with _LOCK:
            # An approval made during the scan wrote a record first; it is kept.
            doc = _read_store(path) or {"version": _VERSION, "folders": {}, "migrated": True}
            for server in servers:
                recorded = doc["folders"].get(server.folder, {}).get(server.file, {})
                if server.name not in recorded:
                    _record(doc, server)
            _write_store(path, doc)
    except Exception:  # noqa: BLE001 - a failed scan approves nothing
        logger.warning("project MCP approvals: could not write the first record", exc_info=True)
    else:
        logger.info(
            "project MCP approvals: recorded the servers already in %d folder(s)",
            len(doc["folders"]),
        )


def _load(path: Path) -> dict[str, Any]:
    if not _MIGRATION_SETTLED.wait(MIGRATION_WAIT_SECS):
        logger.warning("project MCP check: the first record is not written yet; holding")
    with _LOCK:
        return _read_store(path) or {"version": _VERSION, "folders": {}}


def pending_servers(
    folder: str | os.PathLike[str] | None, *, files: Iterable[str] = PROJECT_MCP_FILES
) -> list[ProjectServer]:
    """The servers of *folder*'s *files* the person has not approved, new or changed."""
    return check_folder(folder, files=files).pending


def check_folder(
    folder: str | os.PathLike[str] | None, *, files: Iterable[str] = PROJECT_MCP_FILES
) -> FolderCheck:
    """The servers of *folder*'s *files* to hold, and the files to hold whole.

    Blocking (file reads): call it off the event loop. Never raises, so a session
    always starts: a failure holds every selected file that exists, logged.
    """
    key = folder_key(folder)
    if key is None:
        return FolderCheck()
    selected = tuple(files)
    try:
        doc = _load(store_path())
        servers, unreadable = _scan(key, selected)
    except Exception as exc:  # noqa: BLE001 - a check must never stop a session starting
        logger.warning("project MCP check failed; holding the folder's files", exc_info=True)
        return hold_files(key, (name for name in PROJECT_MCP_FILES if name in selected), exc)
    out: list[ProjectServer] = []
    recorded = doc.get("folders", {}).get(key, {})
    for server in servers:
        held = (recorded.get(server.file) or {}).get(server.name)
        held = held if isinstance(held, dict) else None
        if (
            server.fingerprint
            and held is not None
            and held.get("fingerprint") == server.fingerprint
        ):
            continue
        status = STATUS_NEW if held is None else STATUS_CHANGED
        out.append(
            ProjectServer(
                server.folder,
                server.file,
                server.name,
                server.entry,
                server.fingerprint,
                status,
                held or {},
            )
        )
    return FolderCheck(out, tuple(unreadable))


def approve(folder: str, file: str, name: str, expected: str) -> ProjectServer:
    """Record *name* of *folder*'s *file* as approved, if it still launches *expected*.

    Raises :class:`ApprovalStale` when the file holds another launch, so a
    rewrite between the card and the press is not approved. Raises
    :class:`RecordFull` when the record is at its bounds. Raises ``ValueError``
    for a file that is not a project MCP file. Blocking.
    """
    if file not in PROJECT_MCP_FILES:
        raise ValueError("not a project MCP file")
    servers = folder_servers(folder, files=(file,))
    current = next((s for s in servers if s.name == name), None)
    if current is None or not current.fingerprint or current.fingerprint != expected:
        raise ApprovalStale(name)
    path = store_path()
    with _LOCK:
        doc = _read_store(path) or {"version": _VERSION, "folders": {}}
        recorded = doc.get("folders", {}).get(current.folder, {}).get(file, {})
        names = {s.name for s in servers}
        for recorded_name in list(recorded):
            if recorded_name not in names:
                del recorded[recorded_name]
        if not _record(doc, current):
            raise RecordFull(name)
        _write_store(path, doc)
    return current
