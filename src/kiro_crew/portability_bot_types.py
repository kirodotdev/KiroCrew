"""Shared vocabulary of the instance import's per-Bot restore.

An instance import restores each Bot in parts (its roster entry, its DM-thread
binding, its crewmate file, its dashboard and its briefing) and reports one
:class:`PartOutcome` per part. The statuses, reasons and notices below are the
closed vocabulary the import summary and the Portability tab read; nothing else
may appear in a row. Every function here is pure (``raw_entry_slug`` reads only the
slug rule of ``kiro_crew.members``; :func:`read_fd_at_most` reads only the descriptor
it is given).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The parts of one Bot an import restores, in the order the summary lists them.
PARTS: tuple[str, ...] = ("roster", "binding", "crewmate_file", "dashboard", "briefing")

#: The built-in agent every install has. The Members roster reserves the name
#: (``dashboard.handlers.members_roster.RESERVED_DEFAULT_MEMBER``) and it is never a
#: Bot: no import step adds, keeps or reports it.
RESERVED_DEFAULT_MEMBER = "default"


def is_reserved_bot_name(name: object) -> bool:
    """Whether config key *name* is never a Bot for an instance export or import.

    :data:`RESERVED_DEFAULT_MEMBER` and the mate service agent names
    (``agent_files.is_mate_service_agent``), the one reserved-name rule. Like
    ``default``, no step exports, adds, keeps or reports one: it has no summary row.
    The first crewmate every gateway creates (``agent_files.ASSISTANT_MEMBER_NAME``,
    ``mate``) is an ordinary Bot here, the one a destination has included: an
    import never replaces or adopts it.
    """
    from kiro_crew.agent_files import is_mate_service_agent

    return name == RESERVED_DEFAULT_MEMBER or is_mate_service_agent(name)


#: Statuses that leave a Bot whole.
WHOLE_STATUSES: frozenset[str] = frozenset(
    {"added", "imported", "rebuilt", "written", "restored", "kept", "not_needed", "none"}
)

#: Statuses that mean a part of the Bot is missing after the import.
NOT_WHOLE_STATUSES: frozenset[str] = frozenset(
    {"skipped", "not_rebuilt", "not_written", "not_restored", "page_not_rebuilt"}
)

STATUSES: frozenset[str] = WHOLE_STATUSES | NOT_WHOLE_STATUSES

#: Why a part is not whole (or why a whole part was kept rather than written).
REASONS: tuple[str, ...] = (
    "name_taken",
    "slug_taken",
    "member_id_taken",
    "store_taken",
    "store_not_carried",
    "store_not_owned",
    "workspace_taken",
    "config_rejected",
    "no_member_id",
    "member_id_ambiguous",
    "slug_collision",
    "binding_exists_unusable",
    "archive_binding_disagrees",
    "transcript_predates_import",
    "write_failed",
    "unsafe_name",
    "user_agent_named",
    "foreign_owner",
    "unreadable_record",
    "artifact_missing",
    "artifact_rejected",
    "digest_mismatch",
    "held_by_other_bot",
    "another_package_of_this_bot",
)

#: Information about a Bot that does not make it not whole.
NOTICES: tuple[str, ...] = ("starting_abilities_only", "rules_not_carried", "goals_not_carried")

#: The archive tree an instance export carries a Bot's pinned package under
#: (``bot-artifacts/<P>/``); the import publishes it to the data home's
#: ``artifacts/<P>/``. Never ``artifacts/``: that is a snapshot component, which an
#: importer that does not know Bot stores clears and refills on Replace.
BOT_ARTIFACTS_DIR = "bot-artifacts"

#: Largest dashboard ``instance.json`` read to learn which package it pins: the
#: export reading the source's and the import reading the staged one share it, so
#: a record the import accepts is never one the export dropped its package for.
MAX_RECORD_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ImportedBot:
    """One Bot of the roster the import installed, with its derived DM-thread slot."""

    name: str
    slug: str
    member_id: str
    memory_store: str
    #: The DM-thread generation ``member_thread_session_alias`` takes: the Bot's
    #: memory store name, or ``""`` for a Bot whose store is not version 2.
    generation: str
    slot_key: str
    kiro_agent: str


@dataclass(frozen=True)
class PartOutcome:
    """What the import did with one part of one Bot."""

    bot: str
    part: str
    status: str
    reason: str = ""

    def __post_init__(self) -> None:
        if self.part not in PARTS:
            raise ValueError(f"unknown Bot part {self.part!r}")
        if self.status not in STATUSES:
            raise ValueError(f"unknown part status {self.status!r}")
        if self.reason and self.reason not in REASONS:
            raise ValueError(f"unknown part reason {self.reason!r}")

    @property
    def whole(self) -> bool:
        return self.status in WHOLE_STATUSES


def is_whole(outcomes: Iterable[PartOutcome]) -> bool:
    """True when no outcome has a not-whole status."""
    return all(o.whole for o in outcomes)


_VERSION_FILE = re.compile(r"[0-9]+\.json")

#: The single files below ``members/<slug>/`` an export carries.
_CARRIED_MEMBER_FILES: frozenset[tuple[str, ...]] = frozenset(
    {
        ("dashboard", "instance.json"),
        ("dashboard", "history.json"),
        ("briefing.md",),
        ("welcome_claimed.json",),
    }
)

#: :data:`_CARRIED_MEMBER_FILES` in a fixed order, for an export that opens each by name.
CARRIED_MEMBER_SINGLE_FILES: tuple[tuple[str, ...], ...] = tuple(sorted(_CARRIED_MEMBER_FILES))


def carried_member_file(parts: Sequence[str]) -> bool:
    """Whether the path *parts* below ``members/<slug>/`` is one an export carries.

    Exactly the dashboard record (``instance.json``, ``history.json`` and each
    ``versions/<n>.json`` with ``n`` all digits), ``briefing.md`` and
    ``welcome_claimed.json``. Everything else there is host state or Bot-written
    history an archive must not install: ``activity.jsonl`` would import as trusted
    history, ``welcome_owed.json`` makes the Bot greet again, and lock, preview and
    request files belong to the host that wrote them.
    """
    key = tuple(parts)
    if key in _CARRIED_MEMBER_FILES:
        return True
    return (
        len(key) == 3
        and key[:2] == ("dashboard", "versions")
        and bool(_VERSION_FILE.fullmatch(key[2]))
    )


#: Additions the minimal file (``mate_config.minimal_mate_spec``) does not hold but a
#: crewmate file that adds nothing may, each with the empty value of its type: the boot
#: migration writes ``excludedTools`` only when it lists a restriction
#: (``mate_config_migration.migration_file``), so an empty one restricts nothing.
_EXTRA_EMPTY_ADDITIONS: dict[str, Any] = {"excludedTools": []}


def _empty_additions(name: str) -> dict[str, Any]:
    """Each addition key a crewmate file that adds nothing may hold, with its empty value."""
    from kiro_crew.mate_config import minimal_mate_spec

    empties = {key: value for key, value in minimal_mate_spec(name).items() if key != "name"}
    return {**_EXTRA_EMPTY_ADDITIONS, **empties}


def is_minimal_crewmate_file(doc: Any, name: str) -> bool:
    """True for a crewmate file that adds nothing to the worker it extends.

    Exactly the file ``mate_config.write_mate_config(name)`` writes
    (``mate_config.minimal_mate_spec``), or the same identity with fewer of its keys:
    ``name`` is *name* and every other key is one of the additions a crewmate file
    holds, with the empty value of THAT key's type, the one ``minimal_mate_spec``
    writes for it (``prompt`` ``""``, ``mcpServers`` ``{}``, each list ``[]``;
    :func:`_empty_additions`). An empty value of another type (``"prompt": []``) is
    not what the loader reads for that key, so the file is not minimal. The boot
    migration writes a first crewmate's file in that shorter form when its template
    adds nothing (``mate_config_migration.migration_file``); it grants exactly what the
    minimal file grants.
    """
    if not isinstance(doc, dict) or doc.get("name") != name:
        return False
    empties = _empty_additions(name)
    return all(
        key in empties and type(value) is type(empties[key]) and value == empties[key]
        for key, value in doc.items()
        if key != "name"
    )


#: Largest crewmate file read to tell whether it is the minimal file; a bigger one
#: reads as customised.
MAX_CREWMATE_FILE_BYTES = 1024 * 1024

#: Pass as ``config=`` to have a step load the destination config itself.
LOAD_CONFIG: Any = object()


def _read_crewmate_file(name: str) -> tuple[str, Any]:
    """``(state, document)`` of *name*'s crewmate file, read the one bounded way.

    ``("absent", None)`` when no entry is at the path. ``("unreadable", None)`` when
    the entry cannot be read as a JSON document: a link, not a regular file, more than
    :data:`MAX_CREWMATE_FILE_BYTES`, not UTF-8 or not JSON. Otherwise ``("read", doc)``
    with the parsed document. Opened without following a link and read through
    :func:`read_fd_at_most`.
    """
    import contextlib
    import json
    import stat

    from kiro_crew import platform_compat
    from kiro_crew.mate_config import mate_config_path

    try:
        fd = platform_compat.open_file_no_reparse(mate_config_path(name), nonblocking=True)
    except FileNotFoundError:
        return "absent", None
    except (OSError, ValueError):
        return "unreadable", None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "unreadable", None
        raw = read_fd_at_most(fd, MAX_CREWMATE_FILE_BYTES)
    except (OSError, BoundedReadError):
        return "unreadable", None
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    try:
        return "read", json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "unreadable", None


def crewmate_file_state(name: str) -> str:
    """``absent``, ``minimal`` or ``customised`` for *name*'s crewmate file.

    The one rule an export and an import use to tell whether a crewmate file is the
    minimal file: read by :func:`_read_crewmate_file` and compared with
    ``mate_config.minimal_mate_spec(name)`` (:func:`is_minimal_crewmate_file`), so the
    same document in other whitespace is still minimal. Anything that cannot be read
    that way (a link, not a regular file, too large, not JSON) is ``customised``;
    only a missing entry is ``absent``.
    """
    state, doc = _read_crewmate_file(name)
    if state == "absent":
        return "absent"
    return "minimal" if state == "read" and is_minimal_crewmate_file(doc, name) else "customised"


def raw_entry_slug(name: str, entry: Any) -> str:
    """The slug ``members.member_slug`` gives agent *name* from its raw config entry.

    The stored ``member_id`` as a validated slug when it is a non-empty string, else
    the slug derived from the name. Reads a raw ``config.json`` document (an archive's,
    or the destination's under the config lock) without the loader. Raises
    ``members.MemberSlugError`` (a ``ValueError``) when neither gives a valid slug.
    """
    from kiro_crew.members import slug_for_name, validate_slug

    member_id = entry.get("member_id") if isinstance(entry, Mapping) else None
    if isinstance(member_id, str) and member_id:
        return str(validate_slug(member_id))
    return str(slug_for_name(name))


def raw_entry_identity(entry: Any) -> tuple[str, str] | None:
    """``(member_id, memory_store)`` of a raw ``agents`` entry, as the loader would read it.

    An absent key reads as the ``KiroCrewAgentConfig`` default (``""`` and
    ``"default"``), so a version-1 Bot written before members carried an id has the
    identity ``("", <its store>)``. ``None`` when the entry is not an object or either
    value is present but not a string: such an entry is the same Bot as nothing.
    """
    if not isinstance(entry, Mapping):
        return None
    member_id = entry.get("member_id", "")
    store = entry.get("memory_store", "default")
    if not isinstance(member_id, str) or not isinstance(store, str):
        return None
    return member_id, store


class BoundedReadError(ValueError):
    """A bounded read failed: the file holds more than the limit, or the read raised."""


#: Bytes one ``os.read`` call of :func:`read_fd_at_most` asks for at most.
_READ_CHUNK = 64 * 1024


def read_fd_at_most(fd: int, limit: int) -> bytes:
    """Everything from *fd*'s current offset to its end, when that is at most *limit* bytes.

    Reads no further than one byte past *limit*. Raises :class:`BoundedReadError` when
    there is more, or when a read raises ``OSError`` (chained). The descriptor is
    neither rewound nor closed: the caller owns it and maps the error onto its own
    failure shape.
    """
    chunks: list[bytes] = []
    total = 0
    try:
        while total <= limit:
            chunk = os.read(fd, min(_READ_CHUNK, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    except OSError as exc:
        raise BoundedReadError(f"read failed: {exc}") from exc
    if total > limit:
        raise BoundedReadError(f"more than {limit} bytes")
    return b"".join(chunks)


#: Characters no file name may hold on any platform an install runs on: the path
#: separators, the ones Windows refuses in a name, and every control character.
_UNSAFE_NAME_CHARS: frozenset[str] = frozenset('/\\:<>"|?*') | frozenset(map(chr, range(32)))

#: Device names Windows reserves, with or without an extension, in any case.
_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)


def safe_path_component(name: object) -> bool:
    """Whether *name* can be ONE file name on every platform an install runs on.

    Non-empty, no leading dot, none of :data:`_UNSAFE_NAME_CHARS` (separators, NUL
    and the other control characters, and ``:<>"|?*``), and not a Windows reserved
    device name (``CON``, ``PRN``, ``AUX``, ``NUL``, ``COM1``-``COM9``,
    ``LPT1``-``LPT9``), whatever its case and whether or not an extension follows.
    A plain text rule, the first check before an archive- or config-authored name
    reaches a path. It does not resolve anything on disk.
    """
    if not isinstance(name, str) or not name or name.startswith("."):
        return False
    if any(c in _UNSAFE_NAME_CHARS for c in name):
        return False
    return name.split(".", 1)[0].rstrip(" ").upper() not in _WINDOWS_RESERVED_NAMES
