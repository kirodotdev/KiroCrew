"""Credential masks an app declares in its manifest, under its OWN data directory.

An app that keeps a credential on disk (a provider token, a session cookie) declares
the files in ``app.json``::

    "credentialMasks": ["secrets.json", "tokens/oauth.json"]

Each entry is a path relative to the app's data directory,
``<data home>/apps/<name>/data/``. The sandbox then treats
``apps/<name>/data/<entry>`` exactly like a core ``HIDDEN`` crew-home leaf: it is
bind-masked in every sandbox mode, a symlink at the leaf or at a component below the
data home refuses the spawn, and a regular file with a second hard link refuses it
too. An entry names a FILE: a directory at a declared path refuses the spawn. The app's
own spawned backend gets its declared files back, each only while it is absent or a
regular file with one name, and nothing else does.

**A directory entry.** An entry of ONE segment ending in ``/`` (``"state/"``) names a
DIRECTORY, masked whole, so every file the app writes inside it is covered, including the
temp file a write stages beside its target and renames over it. A file mask cannot cover
that temp, and a rename over a masked file detaches the mask from every running session,
so an app that rewrites its credential by rename keeps it in a declared directory. Before
each Linux spawn the sandbox creates the directory when absent inside the app's existing
``data/`` (``sandbox_staging._materialize_declared_mask_dirs``), so the mask is in place
before the backend first writes into it; a file or a link at the name refuses the spawn. Its backend gets the directory back only
while it is absent or a real directory. A declared directory gets the protection a core
``HIDDEN`` directory has: its files are not judged one by one for a second hard link,
because nothing outside the app's backend can name them. A directory entry needs App Kit
level 8, and an older host refuses a manifest carrying one (its grammar has no ``/``
ending), so it never runs the app with the directory unmasked.

**Wherever the data is.** The app lifecycle moves ``data/`` aside to
``apps/.<name>-data-tmp/`` while an install or update replaces the app's files, and a
default uninstall removes the manifest and keeps the data. So each entry is masked at
both locations, and the declaration is also kept in a record beside the app directory,
``apps/.<name>-credential-masks.json``, which :func:`remember_declaration` refreshes
whenever the gateway records the installed app and before an update or uninstall moves
the data. The record is read with the manifests and only ever adds masks, so a data
directory with no manifest beside it stays masked, and only a manifest that reads
cleanly rewrites or removes it.

**Tightening only.** A declaration can add masks and never remove one: no seal reads
it, the tool gate reads only the shipped built-ins' (:func:`declared_crew_leaves`), as
more refusals, and the one carve-out that reads it (the app's own backend,
``sandbox_tiers.app_declared_mask_targets``) drops every core leaf, so no core leaf's
disposition depends on a manifest. An entry is refused when it is absolute, empty, holds a
``.`` / ``..`` / empty segment, a backslash, a colon, a control character or a lone
surrogate (no filesystem path can encode one), ends a segment in a space or a dot (Windows drops both, so ``a.`` would name ``a``), or is
over the length and depth caps. The app's name comes from where its manifest lives,
so an app cannot declare a leaf under another app's directory: a built-in's manifest
must name its own package, and an installed app's manifest must name the directory
it is installed in.

Where the declarations are read, per spawn and cached on each manifest's identity:
every shipped built-in package's ``app.json``, every installed app's
``<data home>/apps/<name>/app.json`` and every retained record. A manifest that is
absent, unreadable, not JSON or nested past the parser's depth declares nothing; an invalid entry is dropped and logged once, and the install
validation refuses it (``AppManifest.validate``).

The installed tree is sandbox-writable, so a masked leaf is only as strong as the
manifest that declares it: an agent that rewrites an installed app's ``app.json``
drops that app's masks from the next spawn, as it could rewrite the app's code. The
mask stops an agent that reads files; it does not make an installed app's tree
tamper-proof. Sealing the tree is the operator's decision, not this seam's.

**One engine, two declarations.** ``agentReadOnlyDirs``
(:mod:`kiro_crew.apps.agent_readonly_dirs`) is read by the same functions with its own
:class:`DeclarationKind`: the same grammar, the same name-from-location rule, the same
sources, cache and retained record (under its own suffix). Only ``credentialMasks`` has
directory entries (``DeclarationKind.directory_entries``); an ``agentReadOnlyDirs`` entry
ending in ``/`` is still refused as an empty segment. Every function below takes
``kind`` and defaults to :data:`CREDENTIAL_MASKS`.

Process state only (a cache of parsed manifests); no per-caller data.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
from pathlib import Path
from typing import NamedTuple

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.user_json import loads_user_json

logger = logging.getLogger(__name__)

#: The ``app.json`` key.
MANIFEST_KEY = "credentialMasks"
#: At most this many entries per app.
MAX_ENTRIES = 32
#: An entry's UTF-8 length cap.
MAX_ENTRY_BYTES = 240
#: An entry's segment-count cap.
MAX_SEGMENTS = 16
#: Where an app's data directory sits under the data home, joined with its name.
APPS_LEAF = "apps"
DATA_LEAF = "data"
#: The manifest grammar's path separator, on every OS: an entry is a manifest string,
#: not a host path, and the sandbox joins it to the data home itself.
ENTRY_SEPARATOR = "/"
#: An entry ending in this names a directory (one segment, masked whole).
DIRECTORY_SUFFIX = ENTRY_SEPARATOR
#: The lifecycle's move-aside name for ``data/`` is ``.<name>`` plus this, beside the app
#: directory (``apps/manager_install.py``, ``manager_uninstall.py``, ``manager_adopt.py``).
STAGED_DATA_SUFFIX = "-data-tmp"
#: The retained declaration beside the app directory is ``.<name>`` plus this.
RECORD_SUFFIX = "-credential-masks.json"
#: A manifest larger than this declares nothing (no shipped manifest comes close).
_MAX_MANIFEST_BYTES = 1024 * 1024

_APP_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_FORBIDDEN_CHARS_RE = re.compile(r"[\x00-\x1f\x7f\\:\ud800-\udfff]")


class DeclarationKind(NamedTuple):
    """One manifest declaration this engine reads: its ``app.json`` key and record suffix."""

    #: The ``app.json`` key.
    key: str
    #: The retained record beside the app directory is ``.<name>`` plus this.
    record_suffix: str
    #: Whether an entry ending in :data:`DIRECTORY_SUFFIX` names a directory. Without it
    #: the ``/`` ending is an empty segment and refused, as it always was.
    directory_entries: bool = False


#: The ``credentialMasks`` declaration.
CREDENTIAL_MASKS = DeclarationKind(MANIFEST_KEY, RECORD_SUFFIX, directory_entries=True)

_lock = threading.Lock()
#: ``(manifest key, manifest path) -> ((dev, ino, size, mtime_ns), entries)``.
_cache: dict[tuple[str, str], tuple[tuple[int, int, int, int], tuple[str, ...]]] = {}


def entry_error(entry: object, *, kind: DeclarationKind = CREDENTIAL_MASKS) -> str | None:
    """Why *entry* cannot be a *kind* entry, or ``None`` when it can."""
    if not isinstance(entry, str):
        return "must be a string"
    if not entry:
        return "must not be empty"
    if len(entry.encode("utf-8", errors="surrogatepass")) > MAX_ENTRY_BYTES:
        return f"must be at most {MAX_ENTRY_BYTES} bytes"
    if _FORBIDDEN_CHARS_RE.search(entry):
        return "must not contain a backslash, a colon, a control character or a lone surrogate"
    if entry.startswith(ENTRY_SEPARATOR):
        return "must be relative to the app's data directory"
    directory = kind.directory_entries and is_directory_entry(entry)
    segments = (entry_path(entry) if directory else entry).split(ENTRY_SEPARATOR)
    if directory and len(segments) > 1:
        return "a directory entry must be one name ending in '/'"
    if len(segments) > MAX_SEGMENTS:
        return f"must have at most {MAX_SEGMENTS} path segments"
    for segment in segments:
        if segment in ("", ".", ".."):
            return "must not contain an empty, '.' or '..' segment"
        if segment != segment.strip() or segment.endswith("."):
            return "must not start or end a segment with a space, or end one with a dot"
    return None


def is_directory_entry(entry: str) -> bool:
    """Whether *entry* names a directory (it ends in :data:`DIRECTORY_SUFFIX`)."""
    return entry.endswith(DIRECTORY_SUFFIX)


def entry_path(entry: str) -> str:
    """*entry*'s path below the data directory, without a directory entry's ``/``."""
    return entry[: -len(DIRECTORY_SUFFIX)] if is_directory_entry(entry) else entry


def _overlap_error(entries: list[str]) -> str | None:
    """Why two valid *entries* cannot both be declared, or ``None``.

    An entry at or beneath a directory entry is already masked by it, and the two kinds
    disagree on what may sit at the name, so the pair is refused rather than merged.
    """
    directories = {entry_path(e) for e in entries if is_directory_entry(e)}
    for entry in entries:
        path = entry_path(entry)
        for directory in directories:
            if is_directory_entry(entry) and path == directory:
                continue
            if path == directory or path.startswith(directory + ENTRY_SEPARATOR):
                return f"{entry!r} is at or beneath the directory entry {directory + '/'!r}"
    return None


def declaration_errors(value: object, *, kind: DeclarationKind = CREDENTIAL_MASKS) -> list[str]:
    """Validation errors for a manifest's *kind* value (``None`` is fine)."""
    if value is None:
        return []
    if not isinstance(value, list):
        return [f"{kind.key} must be a list of paths"]
    errors: list[str] = []
    if len(value) > MAX_ENTRIES:
        errors.append(f"{kind.key} must have at most {MAX_ENTRIES} entries")
    for index, entry in enumerate(value):
        reason = entry_error(entry, kind=kind)
        if reason is not None:
            errors.append(f"{kind.key}[{index}] {reason}")
    if kind.directory_entries:
        overlap = _overlap_error(
            [e for e in value if isinstance(e, str) and entry_error(e, kind=kind) is None]
        )
        if overlap is not None:
            errors.append(f"{kind.key}: {overlap}")
    return errors


def valid_entries(value: object, *, kind: DeclarationKind = CREDENTIAL_MASKS) -> tuple[str, ...]:
    """The admissible entries of *value*, deduplicated in declaration order.

    Past :data:`MAX_ENTRIES` nothing is taken, the same answer validation gives.
    """
    if not isinstance(value, list) or len(value) > MAX_ENTRIES:
        return ()
    entries = list(
        dict.fromkeys(e for e in value if isinstance(e, str) and entry_error(e, kind=kind) is None)
    )
    directories = {entry_path(e) for e in entries if is_directory_entry(e)}
    # An entry an overlapping directory entry already covers is dropped; the directory
    # stays, so the whole tree is still masked.
    return tuple(
        e
        for e in entries
        if is_directory_entry(e)
        or not any(e == d or e.startswith(d + ENTRY_SEPARATOR) for d in directories)
    )


def crew_leaf(app: str, entry: str) -> str:
    """The crew-home-relative leaf *entry* names for *app*: ``apps/<app>/data/<entry>``.

    A directory entry's leaf carries no trailing ``/``: it is a path, joined like any other.
    """
    return f"{APPS_LEAF}/{app}/{DATA_LEAF}/{entry_path(entry)}"


def staged_crew_leaf(app: str, entry: str) -> str:
    """The leaf *entry* names while the lifecycle holds *app*'s data aside."""
    return f"{APPS_LEAF}/.{app}{STAGED_DATA_SUFFIX}/{entry_path(entry)}"


def record_path(app: str, *, kind: DeclarationKind = CREDENTIAL_MASKS) -> Path:
    """Where *app*'s retained declaration lives: ``apps/.<app><record suffix>``."""
    return config_dir() / APPS_LEAF / f".{app}{kind.record_suffix}"


def _record_app_name(filename: str, kind: DeclarationKind) -> str | None:
    """The app a retained *kind* record named *filename* is for, or ``None``."""
    if not (filename.startswith(".") and filename.endswith(kind.record_suffix)):
        return None
    name = filename[1 : -len(kind.record_suffix)]
    return name if _APP_NAME_RE.fullmatch(name) else None


def _load_json(path: Path) -> object:
    """*path*'s parsed JSON, or ``None`` when it cannot be read or parsed."""
    try:
        return loads_user_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # RecursionError: a body nested past the parser's depth, well under the size cap.
        return None


def _clean_declaration(
    path: Path, expected_name: str, kind: DeclarationKind = CREDENTIAL_MASKS
) -> tuple[str, ...] | None:
    """*path*'s declared entries when the whole manifest reads cleanly, else ``None``.

    Cleanly means a regular file within the size cap, parsed as a JSON object whose
    ``name`` is *expected_name* and whose *kind* value passes validation. Unlike
    :func:`_read_entries`, a failure is ``None`` rather than ``()``, so the caller can tell
    "declares nothing" from "could not be read".
    """
    try:
        info = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_MANIFEST_BYTES:
        return None
    data = _load_json(path)
    if not isinstance(data, dict) or data.get("name") != expected_name:
        return None
    declared = data.get(kind.key)
    if declaration_errors(declared, kind=kind):
        return None
    return valid_entries(declared, kind=kind)


def _read_entries(
    path: Path, expected_name: str, kind: DeclarationKind = CREDENTIAL_MASKS
) -> tuple[str, ...]:
    """*path*'s admissible entries when its ``name`` is *expected_name*, else ``()``.

    Cached on the file's identity, so a spawn re-parses only a manifest that changed.
    Never raises: a manifest this cannot read declares nothing.
    """
    key = (kind.key, str(path))
    try:
        info = os.stat(path)
    except OSError:
        with _lock:
            _cache.pop(key, None)
        return ()
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_MANIFEST_BYTES:
        return ()
    ident = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    with _lock:
        hit = _cache.get(key)
    if hit is not None and hit[0] == ident:
        return hit[1]
    entries: tuple[str, ...] = ()
    data = _load_json(path)
    if isinstance(data, dict) and data.get("name") == expected_name:
        declared = data.get(kind.key)
        errors = declaration_errors(declared, kind=kind)
        if errors:
            logger.warning(
                "app %s: ignoring invalid %s entries in %s: %s",
                expected_name,
                kind.key,
                path,
                "; ".join(errors),
            )
        entries = valid_entries(declared, kind=kind)
    with _lock:
        _cache[key] = (ident, entries)
    return entries


def _builtin_manifests() -> list[tuple[str, Path]]:
    """``(installed name, app.json)`` for every shipped built-in package."""
    root = Path(__file__).resolve().parent / "builtins"
    try:
        packages = sorted(entry.name for entry in os.scandir(root) if entry.is_dir())
    except OSError:
        return []
    return [
        (package.replace("_", "-"), root / package / "app.json")
        for package in packages
        if not package.startswith(("_", "."))
    ]


def _installed_manifests(kind: DeclarationKind = CREDENTIAL_MASKS) -> list[tuple[str, Path]]:
    """``(installed name, file)`` for every app directory and retained record in the live home.

    An app directory contributes its ``app.json``; a retained *kind* record
    (``.<name><record suffix>``) contributes itself, under the name it is for.
    """
    try:
        apps = config_dir() / APPS_LEAF
        names: list[str] = []
        records: list[tuple[str, Path]] = []
        for entry in os.scandir(apps):
            if entry.is_dir():
                names.append(entry.name)
            elif (record_app := _record_app_name(entry.name, kind)) is not None:
                records.append((record_app, apps / entry.name))
    except FileNotFoundError:
        return []
    except Exception:
        # A data home this cannot list declares nothing here; the sandbox's own pass over
        # the same home refuses the spawn when the home itself cannot be resolved.
        logger.warning("could not list the installed apps for their %s", kind.key, exc_info=True)
        return []
    manifests = [(name, apps / name / "app.json") for name in names if _APP_NAME_RE.fullmatch(name)]
    return sorted([*manifests, *records])


def declared_masks(
    *, builtin: bool = True, installed: bool = True, kind: DeclarationKind = CREDENTIAL_MASKS
) -> dict[str, tuple[str, ...]]:
    """Every app's declared entries on this host, ``{app name: entries}``.

    Only apps that declare at least one entry appear. A built-in and an installed tree
    under the same name contribute the union, since both masks protect the same
    directory. *builtin* and *installed* choose which manifests are read.
    """
    sources = [
        *(_builtin_manifests() if builtin else ()),
        *(_installed_manifests(kind) if installed else ()),
    ]
    out: dict[str, dict[str, None]] = {}
    for name, path in sources:
        if not _APP_NAME_RE.fullmatch(name):
            continue
        entries = _read_entries(path, name, kind)
        if entries:
            out.setdefault(name, {}).update(dict.fromkeys(entries))
    return {name: tuple(entries) for name, entries in sorted(out.items())}


def declared_crew_leaves(
    *, installed: bool = True, kind: DeclarationKind = CREDENTIAL_MASKS
) -> tuple[str, ...]:
    """Every declared mask as a crew-home-relative leaf, sorted and deduplicated.

    Each entry at both places its app's data can be: ``data/`` and the lifecycle's
    move-aside directory.

    ``installed=False`` reads only the shipped built-ins, for the agent tool gate:
    ``security._CREW_SECRET_LEAVES`` adds those once, at import, so the agent's file
    tools refuse a built-in's credential file as they refuse a core one, and no core row
    has to name the app. The shipped manifests are part of the installed product, so
    that set is fixed for the process; an installed app's manifest is sandbox-writable
    and read per spawn, which a static gate list cannot follow.
    """
    return tuple(
        sorted(
            {
                leaf
                for app, entries in declared_masks(installed=installed, kind=kind).items()
                for entry in entries
                for leaf in (crew_leaf(app, entry), staged_crew_leaf(app, entry))
            }
        )
    )


def declared_crew_dir_leaves(*, installed: bool = True) -> frozenset[str]:
    """The subset of :func:`declared_crew_leaves` that directory entries name."""
    return frozenset(
        leaf
        for app, entries in declared_masks(installed=installed).items()
        for entry in entries
        if is_directory_entry(entry)
        for leaf in (crew_leaf(app, entry), staged_crew_leaf(app, entry))
    )


def declared_crew_leaves_for(
    app: str, *, builtin: bool, kind: DeclarationKind = CREDENTIAL_MASKS
) -> tuple[str, ...]:
    """*app*'s declared masks as crew-home-relative leaves, from ONE manifest source.

    *builtin* reads the shipped package's manifest, otherwise the installed tree's: the
    caller names the code it runs, so a backend gets back what its own manifest declared.
    """
    masks = declared_masks(builtin=builtin, installed=not builtin, kind=kind)
    return tuple(crew_leaf(app, entry) for entry in masks.get(app, ()))


def declared_crew_dir_leaves_for(app: str, *, builtin: bool) -> frozenset[str]:
    """The directory entries among :func:`declared_crew_leaves_for`, from the same source."""
    masks = declared_masks(builtin=builtin, installed=not builtin)
    return frozenset(crew_leaf(app, e) for e in masks.get(app, ()) if is_directory_entry(e))


def remember_declaration(app: str, *, kind: DeclarationKind = CREDENTIAL_MASKS) -> None:
    """Make *app*'s retained record match its installed ``app.json``.

    Called by the gateway whenever it records the installed app and before an update or
    uninstall moves the app's data, so the masks outlive the manifest beside the data.
    Only a manifest that reads cleanly (:func:`_clean_declaration`) changes the record: one
    declaring nothing removes it. A missing, unreadable, unparseable, misnamed or invalid
    ``app.json`` (a built-in's record, a tree mid-copy, a hand edit) leaves the record as
    it is, because the data beside it may still hold the files the last clean manifest
    declared. Raises ``OSError`` when the record cannot be written or removed: an
    uninstall that keeps the data refuses on it rather than leave a credential unmasked.
    """
    if not _APP_NAME_RE.fullmatch(app):
        return
    manifest = config_dir() / APPS_LEAF / app / "app.json"
    if not manifest.is_file():
        return
    entries = _clean_declaration(manifest, app, kind)
    if entries is None:
        logger.warning(
            "app %s: keeping its retained %s record as it is, because %s does not read " "cleanly",
            app,
            kind.key,
            manifest,
        )
        return
    record = record_path(app, kind=kind)
    if entries:
        body = json.dumps({"name": app, kind.key: list(entries)}, indent=2) + "\n"
        atomic_write(record, body, restrict_to_owner=True)
    else:
        record.unlink(missing_ok=True)


def clear_cache() -> None:
    """Forget every parsed manifest (tests)."""
    with _lock:
        _cache.clear()
