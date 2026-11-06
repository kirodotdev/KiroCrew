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

**Wherever the data is.** The app lifecycle moves ``data/`` aside to
``apps/.<name>-data-tmp/`` while an install or update replaces the app's files, and a
default uninstall removes the manifest and keeps the data. So each entry is masked at
both locations, and the declaration is also kept in a record beside the app directory,
``apps/.<name>-credential-masks.json``, which :func:`remember_declaration` refreshes
whenever the gateway records the installed app and before an update or uninstall moves
the data. The record is read with the manifests and only ever adds masks, so a data
directory with no manifest beside it stays masked, and only a manifest that reads
cleanly rewrites or removes it.

**Tightening only.** A declaration can add masks and never remove one: nothing here
is read by a seal or the tool gate, and the one carve-out that reads it (the app's
own backend, ``sandbox_tiers.app_declared_mask_targets``) drops every core leaf, so no
core leaf's disposition depends on a manifest. An entry is refused when it is absolute, empty, holds a
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
#: The lifecycle's move-aside name for ``data/`` is ``.<name>`` plus this, beside the app
#: directory (``apps/manager_install.py``, ``manager_uninstall.py``, ``manager_adopt.py``).
STAGED_DATA_SUFFIX = "-data-tmp"
#: The retained declaration beside the app directory is ``.<name>`` plus this.
RECORD_SUFFIX = "-credential-masks.json"
#: A manifest larger than this declares nothing (no shipped manifest comes close).
_MAX_MANIFEST_BYTES = 1024 * 1024

_APP_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_RECORD_NAME_RE = re.compile(r"\.([a-z0-9]+(?:-[a-z0-9]+)*)" + re.escape(RECORD_SUFFIX))
_FORBIDDEN_CHARS_RE = re.compile(r"[\x00-\x1f\x7f\\:\ud800-\udfff]")

_lock = threading.Lock()
#: ``manifest path -> ((dev, ino, size, mtime_ns), entries)``.
_cache: dict[str, tuple[tuple[int, int, int, int], tuple[str, ...]]] = {}


def entry_error(entry: object) -> str | None:
    """Why *entry* cannot be a credential mask, or ``None`` when it can."""
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
    segments = entry.split(ENTRY_SEPARATOR)
    if len(segments) > MAX_SEGMENTS:
        return f"must have at most {MAX_SEGMENTS} path segments"
    for segment in segments:
        if segment in ("", ".", ".."):
            return "must not contain an empty, '.' or '..' segment"
        if segment != segment.strip() or segment.endswith("."):
            return "must not start or end a segment with a space, or end one with a dot"
    return None


def declaration_errors(value: object) -> list[str]:
    """Validation errors for a manifest's ``credentialMasks`` value (``None`` is fine)."""
    if value is None:
        return []
    if not isinstance(value, list):
        return [f"{MANIFEST_KEY} must be a list of paths"]
    errors: list[str] = []
    if len(value) > MAX_ENTRIES:
        errors.append(f"{MANIFEST_KEY} must have at most {MAX_ENTRIES} entries")
    for index, entry in enumerate(value):
        reason = entry_error(entry)
        if reason is not None:
            errors.append(f"{MANIFEST_KEY}[{index}] {reason}")
    return errors


def valid_entries(value: object) -> tuple[str, ...]:
    """The admissible entries of *value*, deduplicated in declaration order.

    Past :data:`MAX_ENTRIES` nothing is taken, the same answer validation gives.
    """
    if not isinstance(value, list) or len(value) > MAX_ENTRIES:
        return ()
    return tuple(dict.fromkeys(e for e in value if isinstance(e, str) and entry_error(e) is None))


def crew_leaf(app: str, entry: str) -> str:
    """The crew-home-relative leaf *entry* names for *app*: ``apps/<app>/data/<entry>``."""
    return f"{APPS_LEAF}/{app}/{DATA_LEAF}/{entry}"


def staged_crew_leaf(app: str, entry: str) -> str:
    """The leaf *entry* names while the lifecycle holds *app*'s data aside."""
    return f"{APPS_LEAF}/.{app}{STAGED_DATA_SUFFIX}/{entry}"


def record_path(app: str) -> Path:
    """Where *app*'s retained declaration lives: ``apps/.<app>-credential-masks.json``."""
    return config_dir() / APPS_LEAF / f".{app}{RECORD_SUFFIX}"


def _load_json(path: Path) -> object:
    """*path*'s parsed JSON, or ``None`` when it cannot be read or parsed."""
    try:
        return loads_user_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # RecursionError: a body nested past the parser's depth, well under the size cap.
        return None


def _clean_declaration(path: Path, expected_name: str) -> tuple[str, ...] | None:
    """*path*'s declared entries when the whole manifest reads cleanly, else ``None``.

    Cleanly means a regular file within the size cap, parsed as a JSON object whose
    ``name`` is *expected_name* and whose ``credentialMasks`` passes validation. Unlike
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
    declared = data.get(MANIFEST_KEY)
    if declaration_errors(declared):
        return None
    return valid_entries(declared)


def _read_entries(path: Path, expected_name: str) -> tuple[str, ...]:
    """*path*'s admissible entries when its ``name`` is *expected_name*, else ``()``.

    Cached on the file's identity, so a spawn re-parses only a manifest that changed.
    Never raises: a manifest this cannot read declares nothing.
    """
    key = str(path)
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
        declared = data.get(MANIFEST_KEY)
        errors = declaration_errors(declared)
        if errors:
            logger.warning(
                "app %s: ignoring invalid %s entries in %s: %s",
                expected_name,
                MANIFEST_KEY,
                key,
                "; ".join(errors),
            )
        entries = valid_entries(declared)
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


def _installed_manifests() -> list[tuple[str, Path]]:
    """``(installed name, file)`` for every app directory and retained record in the live home.

    An app directory contributes its ``app.json``; a retained record
    (``.<name>-credential-masks.json``) contributes itself, under the name it is for.
    """
    try:
        apps = config_dir() / APPS_LEAF
        names: list[str] = []
        records: list[tuple[str, Path]] = []
        for entry in os.scandir(apps):
            if entry.is_dir():
                names.append(entry.name)
            elif (match := _RECORD_NAME_RE.fullmatch(entry.name)) is not None:
                records.append((match.group(1), apps / entry.name))
    except FileNotFoundError:
        return []
    except Exception:
        # A data home this cannot list declares nothing here; the sandbox's own pass over
        # the same home refuses the spawn when the home itself cannot be resolved.
        logger.warning(
            "could not list the installed apps for their credential masks", exc_info=True
        )
        return []
    manifests = [(name, apps / name / "app.json") for name in names if _APP_NAME_RE.fullmatch(name)]
    return sorted([*manifests, *records])


def declared_masks(*, builtin: bool = True, installed: bool = True) -> dict[str, tuple[str, ...]]:
    """Every app's declared entries on this host, ``{app name: entries}``.

    Only apps that declare at least one entry appear. A built-in and an installed tree
    under the same name contribute the union, since both masks protect the same
    directory. *builtin* and *installed* choose which manifests are read.
    """
    sources = [
        *(_builtin_manifests() if builtin else ()),
        *(_installed_manifests() if installed else ()),
    ]
    out: dict[str, dict[str, None]] = {}
    for name, path in sources:
        if not _APP_NAME_RE.fullmatch(name):
            continue
        entries = _read_entries(path, name)
        if entries:
            out.setdefault(name, {}).update(dict.fromkeys(entries))
    return {name: tuple(entries) for name, entries in sorted(out.items())}


def declared_crew_leaves() -> tuple[str, ...]:
    """Every declared mask as a crew-home-relative leaf, sorted and deduplicated.

    Each entry at both places its app's data can be: ``data/`` and the lifecycle's
    move-aside directory.
    """
    return tuple(
        sorted(
            {
                leaf
                for app, entries in declared_masks().items()
                for entry in entries
                for leaf in (crew_leaf(app, entry), staged_crew_leaf(app, entry))
            }
        )
    )


def declared_crew_leaves_for(app: str, *, builtin: bool) -> tuple[str, ...]:
    """*app*'s declared masks as crew-home-relative leaves, from ONE manifest source.

    *builtin* reads the shipped package's manifest, otherwise the installed tree's: the
    caller names the code it runs, so a backend gets back what its own manifest declared.
    """
    masks = declared_masks(builtin=builtin, installed=not builtin)
    return tuple(crew_leaf(app, entry) for entry in masks.get(app, ()))


def remember_declaration(app: str) -> None:
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
    entries = _clean_declaration(manifest, app)
    if entries is None:
        logger.warning(
            "app %s: keeping its retained %s record as it is, because %s does not read " "cleanly",
            app,
            MANIFEST_KEY,
            manifest,
        )
        return
    record = record_path(app)
    if entries:
        body = json.dumps({"name": app, MANIFEST_KEY: list(entries)}, indent=2) + "\n"
        atomic_write(record, body, restrict_to_owner=True)
    else:
        record.unlink(missing_ok=True)


def clear_cache() -> None:
    """Forget every parsed manifest (tests)."""
    with _lock:
        _cache.clear()
