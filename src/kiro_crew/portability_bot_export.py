"""What a whole-install export carries for each roster Bot, and what it records it did not.

A Bot is more than its config entry: its dashboard record lives under
``members/<slug>/dashboard/``, the package that dashboard pins lives under
``artifacts/<package>/``, and it may have a briefing and a claimed welcome. This
module enumerates exactly those files for :func:`portability_stream._build_export_zip`
and :func:`portability_stream._measure_nonsession_footprint`, through the same pinned,
no-follow walk the other trees use (``portability._walk_contained``).

The archive carries a pinned package under ``bot-artifacts/<package>/``
(:data:`BOT_ARTIFACTS_DIR`), never under ``artifacts/``: ``artifacts`` is a snapshot
component, and an importer that does not know Bot stores clears and refills a
component tree the archive carries on Replace, which would erase every other
artifact on its install. ``members/`` is no component, so no importer copies or
clears it.

What it never enumerates: ``member-records/`` (DM bindings, rules and goals),
``crew-panels/`` (the gateway-owned page snapshots) and anything under the Kiro home
(crewmate files). The import regenerates bindings, page snapshots and minimal
crewmate files from the config and the carried artifact instead of trusting archive
bytes for them. For rules, goals and crewmate files that carried abilities, the
manifest records only that they existed (names, never contents), so the import can
tell the person what did not travel.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.members import MEMBERS_DIR_NAME
from kiro_crew.portability_bot_types import (
    BOT_ARTIFACTS_DIR,
    CARRIED_MEMBER_SINGLE_FILES,
    MAX_CREWMATE_FILE_BYTES,
    MAX_RECORD_BYTES,
    BoundedReadError,
    carried_member_file,
    crewmate_file_state,
    is_reserved_bot_name,
    raw_entry_slug,
    read_fd_at_most,
    safe_path_component,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_LISTED_BOTS",
    "MAX_CONFIG_BYTES",
    "MAX_CREWMATE_FILE_BYTES",
    "ExportBots",
    "bot_manifest_fields",
    "crewmate_files_customised",
    "export_bots",
    "iter_bot_store_files",
    "load_roster",
    "member_records_not_carried",
    "read_config_document",
    "roster",
]

#: At most this many slugs (or names) a manifest list carries; the rest is a count.
MAX_LISTED_BOTS = 500

#: A ``config.json`` larger than this yields no roster (and so no Bot stores).
MAX_CONFIG_BYTES = 16 * 1024 * 1024

ARTIFACTS_DIR = "artifacts"

#: The record kinds an export names per Bot without carrying them.
RECORD_KINDS: tuple[str, ...] = ("member-rules", "member-goals")


def _agents_and_slug(config: Any) -> tuple[Any, Any]:
    """The ``agents`` table of *config* and the slug rule that reads it.

    A loaded config goes through ``members.member_slug``. A raw ``config.json``
    document (what the export reads, so it never runs the loader's write-back or its
    publishes) uses the same rule on the entry itself: the stored ``member_id`` as a
    validated slug, else the slug derived from the name.
    """
    from kiro_crew.members import member_slug

    if isinstance(config, Mapping):
        agents = config.get("agents")

        def _raw_slug(name: str, _config: Any) -> str:
            return raw_entry_slug(name, agents.get(name) if isinstance(agents, Mapping) else None)

        return agents, _raw_slug
    return getattr(config, "agents", None), member_slug


def roster(config: Any) -> list[tuple[str, str]]:
    """``(name, slug)`` for every Bot of the config ``agents`` table, in config order.

    *config* is a loaded config or a raw ``config.json`` document; ``slug`` is
    ``members.member_slug(name, config)`` either way. The reserved ``default`` agent is
    not a Bot and is never listed. An entry whose slug cannot be
    derived (a ``member_id`` that is not a valid slug) is left out: it has no member
    directory an export could name. A second entry deriving an already-listed slug is
    left out too, so no store is carried twice.
    """
    from kiro_crew.members import MemberSlugError

    agents, slug_of = _agents_and_slug(config)
    if not isinstance(agents, Mapping):
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in agents:
        if not isinstance(name, str) or not name or is_reserved_bot_name(name):
            continue
        try:
            slug = slug_of(name, config)
        except (MemberSlugError, ValueError):
            continue
        if slug in seen:
            continue
        seen.add(slug)
        out.append((name, slug))
    return out


def read_config_document(mc_real: str) -> Mapping[str, Any] | None:
    """``<mc_real>/config.json`` as a JSON object; ``None`` when absent or unreadable.

    Read as a document through a no-follow open, bounded at
    :data:`MAX_CONFIG_BYTES`, never through the config loader, so the read neither
    rewrites ``config.json`` nor publishes settings.
    """
    try:
        fd = platform_compat.open_file_no_reparse(
            os.path.join(mc_real, "config.json"), nonblocking=True
        )
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = _read_fd_bounded(fd, MAX_CONFIG_BYTES)
    except OSError:
        return None
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    if raw is None:
        logger.warning("config.json over %d bytes, read as no roster", MAX_CONFIG_BYTES)
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("config.json unreadable, read as no roster")
        return None
    return doc if isinstance(doc, Mapping) else None


def load_roster(mc_real: str) -> list[tuple[str, str]]:
    """:func:`roster` of ``<mc_real>/config.json`` (:func:`read_config_document`).

    Empty when the document cannot be read: an export never fails because the roster
    is unreadable; it then carries no Bot stores, exactly as an install with no Bots.
    """
    doc = read_config_document(mc_real)
    return roster(doc) if doc is not None else []


def _valid_slug(slug: object) -> bool:
    """The artifact store's own slug rule, so a pinned package names one directory."""
    from kiro_crew.artifacts import slug_is_well_formed

    return isinstance(slug, str) and slug_is_well_formed(slug)


def _read_fd_bounded(fd: int, limit: int) -> bytes | None:
    """:func:`read_fd_at_most` from the start of *fd*, rewound after; ``None`` when it fails.

    Raises ``OSError`` only from the rewind, which the callers answer as unreadable.
    """
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        return read_fd_at_most(fd, limit)
    except BoundedReadError:
        return None
    finally:
        os.lseek(fd, 0, os.SEEK_SET)


def _pinned_package(fd: int) -> str:
    """The package slug a dashboard ``instance.json`` behind *fd* pins, or ``""``.

    Read up to ``portability_bot_types.MAX_RECORD_BYTES``, the cap the import reads
    the staged record with.
    """
    from kiro_crew.dashboard_templates.instance import _package_field

    try:
        raw = _read_fd_bounded(fd, MAX_RECORD_BYTES)
    except OSError:
        return ""
    if raw is None:
        return ""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(doc, Mapping):
        return ""
    slug, _version, _digest, released = _package_field(doc)
    if released or not _valid_slug(slug):
        return ""
    return slug


def _keep_member_file(rel: PurePath) -> bool:
    """The walk filter for ``members/<slug>/``: the carried allowlist, then the export's own."""
    from kiro_crew import portability as _pf

    return carried_member_file(rel.parts[2:]) and _pf._keep_for_export(rel)


def _open_member_file(mc_real: str, rel: PurePath) -> int | None:
    """A descriptor for the one export file *rel*, or ``None`` when it is not carried.

    Opened through ``portability._open_contained_file`` (every ancestor pinned, the
    leaf not followed and containment-checked, as the walk opens a file) and
    duplicated, so the caller owns and closes it as with ``_walk_contained``.
    """
    from kiro_crew import portability as _pf

    if not _keep_member_file(rel):
        return None
    with _pf._open_contained_file(mc_real, rel) as fd:
        return None if fd is None else os.dup(fd)


def _member_store_files(mc_real: str, slug: str) -> Iterator[tuple[PurePath, int]]:
    """``members/<slug>/``'s carried files: the record's own, its versions, the single files.

    Only ``dashboard/`` and ``dashboard/versions/`` are entered; the rest of the
    member folder (host state, Bot-written history) is never listed.
    """
    from kiro_crew import portability as _pf

    member = PurePath(MEMBERS_DIR_NAME, slug)
    for parts in CARRIED_MEMBER_SINGLE_FILES:
        fd = _open_member_file(mc_real, member.joinpath(*parts))
        if fd is not None:
            yield member.joinpath(*parts), fd
    yield from _pf._walk_contained(mc_real, member / "dashboard" / "versions", _keep_member_file)


def iter_bot_store_files(
    mc_real: str, bots: Sequence[tuple[str, str]]
) -> Iterator[tuple[PurePath, int]]:
    """Yield ``(arcrel, fd)`` for every Bot store file a whole-install export carries.

    For each ``(name, slug)`` of *bots*: the files under ``members/<slug>/`` that
    ``carried_member_file`` admits (:func:`_member_store_files`: the record's files
    opened by name, ``dashboard/versions/`` walked, ``briefing.md`` and
    ``welcome_claimed.json`` opened by name), then every regular file under
    ``artifacts/<P>/`` that ``portability._keep_for_export`` admits, where ``P`` is
    the package that Bot's ``dashboard/instance.json`` pins. *arcrel* is the archive
    path: a member file keeps its data-home path, a package file is renamed to
    ``bot-artifacts/<P>/...`` (:data:`BOT_ARTIFACTS_DIR`). Every open pins the
    ancestors and refuses a link (``portability._walk_contained`` and
    ``_open_contained_file``), so a linked ``members/<slug>`` or ``artifacts/<P>`` is
    refused rather than followed and every descriptor is containment-checked against
    *mc_real*. A package two Bots pin is carried once.

    The caller closes each descriptor, as with ``_walk_contained``. ``instance.json``
    is read (bounded) before it is yielded and its descriptor is rewound, so the caller
    archives the same bytes the package was read from.
    """
    from kiro_crew import portability as _pf
    from kiro_crew.members import MemberSlugError, validate_slug

    carried_packages: set[str] = set()
    for _name, slug in bots:
        try:
            validate_slug(slug)
        except (MemberSlugError, ValueError):
            continue
        package = ""
        for rel, fd in _member_store_files(mc_real, slug):
            if rel.parts[2:] == ("dashboard", "instance.json"):
                package = _pinned_package(fd)
            yield rel, fd
        if not package or package in carried_packages:
            continue
        carried_packages.add(package)
        for rel, fd in _pf._walk_contained(
            mc_real, PurePath(ARTIFACTS_DIR, package), _pf._keep_for_export
        ):
            yield PurePath(BOT_ARTIFACTS_DIR, *rel.parts[1:]), fd


def _capped(items: list[Any]) -> tuple[list[Any], int]:
    return items[:MAX_LISTED_BOTS], max(0, len(items) - MAX_LISTED_BOTS)


def _record_names(directory: str) -> set[str]:
    """The entry names in one record-kind directory; empty when it is not a directory."""
    try:
        with os.scandir(directory) as scan:
            return {e.name for e in scan if not e.is_dir(follow_symlinks=False)}
    except OSError:
        return set()


def member_records_not_carried(
    bots: Sequence[tuple[str, str]],
) -> tuple[dict[str, list[str]], int]:
    """Which roster Bots have a rules or goals record this export does not carry.

    ``({slug: [kind, ...]}, more)``: for each kind of :data:`RECORD_KINDS`, whether
    ``member-records/<kind>/<slug>.json`` exists, decided from a directory listing only;
    no record is opened. At most :data:`MAX_LISTED_BOTS` slugs in sorted order, with
    *more* counting the rest.
    """
    from kiro_crew.member_records import records_root

    root = str(records_root())
    listings = {kind: _record_names(os.path.join(root, kind)) for kind in RECORD_KINDS}
    found: dict[str, list[str]] = {}
    for slug in sorted({slug for _name, slug in bots}):
        kinds = [kind for kind in RECORD_KINDS if f"{slug}.json" in listings[kind]]
        if kinds:
            found[slug] = kinds
    keys, more = _capped(sorted(found))
    return {k: found[k] for k in keys}, more


def crewmate_files_customised(bots: Sequence[tuple[str, str]]) -> tuple[list[str], int]:
    """Roster Bot names whose crewmate file carries more than the minimal shape.

    ``(names, more)``: each name whose ``mate_config.mate_config_path(name)`` is not
    exactly the file ``write_mate_config`` writes, or cannot be read (a link, not a
    regular file, over :data:`MAX_CREWMATE_FILE_BYTES`, not JSON). An absent file is
    not listed. A name that could not be a crewmate file name (a path separator, a
    NUL or a leading dot) is never opened. At most :data:`MAX_LISTED_BOTS` names in
    sorted order, with *more* counting the rest.
    """
    names = sorted(
        {
            name
            for name, _slug in bots
            if safe_path_component(name) and crewmate_file_state(name) == "customised"
        }
    )
    return _capped(names)


def bot_manifest_fields(bots: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """The top-level manifest keys a whole-install export adds for its Bots.

    ``bot_stores`` marks an archive that carries Bot stores; the two lists name what
    it does not carry, each with a ``_more`` count only when the list was capped.
    ``contents.bot_store_files`` is written by the caller, which counts the files.
    """
    records, records_more = member_records_not_carried(bots)
    customised, customised_more = crewmate_files_customised(bots)
    fields: dict[str, Any] = {
        "bot_stores": 1,
        "member_records_not_carried": records,
        "crewmate_files_customised": customised,
    }
    if records_more:
        fields["member_records_not_carried_more"] = records_more
    if customised_more:
        fields["crewmate_files_customised_more"] = customised_more
    return fields


@dataclass(frozen=True)
class ExportBots:
    """One export's Bots, read once: the roster and the manifest keys it adds.

    ``roster`` is what :func:`iter_bot_store_files` walks; ``fields`` is
    :func:`bot_manifest_fields` of that same roster.
    """

    roster: tuple[tuple[str, str], ...] = ()
    fields: Mapping[str, Any] = field(default_factory=dict)


def export_bots(mc_real: str) -> ExportBots:
    """Read ``config.json`` once and build the roster and the Bot manifest fields from it.

    The export writes and measures the archive from this one value, so the Bot stores
    it carries and the manifest that describes them come from the same read.
    """
    bots = load_roster(mc_real)
    return ExportBots(roster=tuple(bots), fields=bot_manifest_fields(bots))
