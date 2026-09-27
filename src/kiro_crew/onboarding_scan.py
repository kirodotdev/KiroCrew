"""Safe reads of a foreign agent's install, and the content screens they feed.

The bottom layer of foreign-agent import (see
docs/system-specs/modules/onboarding-import.md). Everything that touches a
foreign file, and the shared screens that decide whether foreign text is safe
to carry, live here:

* the scan accumulator (:class:`_Scan`) and the candidate item it collects
  (:class:`_Item`), plus the category vocabulary both are keyed by;
* probes that answer "absent" rather than raise on a foreign-supplied path, and
  the symlink / reparse-point test every read passes;
* bounded, symlink-safe, sensitive-path-refusing file and directory reads;
* parsers for the foreign config formats (JSON, JSON5, TOML, and YAML through a
  loader that refuses aliases);
* private read-only SQLite snapshots;
* the shared content screens: credential redaction and secret counting, the
  secret-shaped-field detector, the decoded-value screen, and the skill
  auto-activation screen -- including the bounded, screened read of one
  SKILL.md package (:func:`_skill_package`). Category-specific refusals (the
  MCP field allowlist, the persona identity guard, schedule semantics) sit
  with their projection in :mod:`kiro_crew.onboarding_plan`.

This module is the ONLY one in the engine that calls the credential redactor,
and it is registered as the "Onboarding import" sink in
``security_posture._REDACTION_SINKS``. It imports nothing else from the engine;
the projections, adapters, plan and apply owners all build on it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

try:
    import tomllib as _toml
except ImportError:  # pragma: no cover - Python 3.9/3.10 compatibility
    try:
        import tomli as _toml  # type: ignore[no-redef,import-not-found]
    except ImportError:
        _toml = None  # type: ignore[assignment]

import yaml  # type: ignore[import-untyped]

from kiro_crew.frontmatter import ONBOARDING_IMPORT, parse_block_scalar_header, split_frontmatter
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.security import (
    contains_injection,
    is_sensitive_path,
    redact_with_findings,
)


class _NoAliasSafeLoader(yaml.SafeLoader):
    """SafeLoader that refuses YAML anchors/aliases.

    Foreign-agent config files are untrusted. Plain ``yaml.safe_load`` still
    expands ``*alias`` references into a shared-reference graph, so a tiny
    "billion-laughs" config would explode when the downstream secret/leaf
    traversal re-walks it. Rejecting aliases at compose time keeps the
    amplification vector closed while preserving full indentation support. A
    lone anchor with no alias is harmless (nothing to amplify) and is allowed.
    """

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.events.AliasEvent):
            event = self.get_event()
            raise yaml.composer.ComposerError(
                None, None, "found alias, which is not allowed", event.start_mark
            )
        return super().compose_node(parent, index)


def _load_no_alias_yaml(text: str) -> Any:
    """Parse ONE YAML document with :class:`_NoAliasSafeLoader`.

    Driving the loader instance is what ``yaml.load`` does with an explicit
    ``Loader=``, so the parse is identical — but the SafeLoader subclass is the
    only construction path here, with no ``yaml.load`` call whose safety a
    reader (or a scanner keyed on the call name) has to infer from the
    ``Loader=`` argument.
    """
    loader = _NoAliasSafeLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


CATEGORY_IDS = (
    "instructions",
    "memories",
    "workspaces",
    "mcp_servers",
    "skills",
    "schedules",
    "settings",
)


_MAX_FILES = 500


_MAX_FILE_BYTES = 8 * 1024 * 1024


_MAX_YAML_BYTES = 1024 * 1024


_MAX_TOTAL_BYTES = 64 * 1024 * 1024


_MAX_TEXT_CHARS = 100_000


_MAX_DB_BYTES = 64 * 1024 * 1024


_MAX_WALK_ENTRIES = 10_000


_MAX_SKILL_PACKAGE_BYTES = 1024 * 1024


_SQLITE_TABLE_NAMES_QUERY = "SELECT name FROM sqlite_schema WHERE type='table'"


_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|token|secret|password|credential|authorization|headers?|env)",
    re.IGNORECASE,
)


@dataclass
class _Item:
    source_id: str
    category: str
    key: str
    payload: Any

    @property
    def fingerprint(self) -> str:
        material = f"{self.source_id}\0{self.category}\0{self.key}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()


def _managed_mcp_names_unwired() -> frozenset[str]:
    """Default for :attr:`_Scan.managed_mcp_names`: refuse rather than guess.

    ``_scan_source`` always wires the registry's own lookup. A scan built any
    other way that reaches the MCP projection is a programming error, and an
    empty or core-only answer would silently import an edition's managed
    servers -- so it raises instead.
    """
    raise RuntimeError("scan has no registry: pass managed_mcp_names")


@dataclass
class _Scan:
    source_id: str
    root: Path
    user_home: Path
    config_paths: tuple[Path, ...] = ()
    workspace_paths: tuple[Path, ...] = ()
    items: dict[str, list[_Item]] = field(
        default_factory=lambda: {category: [] for category in CATEGORY_IDS}
    )
    skipped: list[dict[str, Any]] = field(default_factory=list)
    secret_count: int = 0
    unsupported_count: int = 0
    bytes_read: dict[str, int] = field(default_factory=dict)
    files_seen: dict[str, int] = field(default_factory=dict)
    truncated_roots: set[str] = field(default_factory=set)
    _diagnostic_keys: dict[tuple[str, str, str], int] = field(default_factory=dict)
    #: The registry's managed-MCP-name lookup (``onboarding_sources._managed_mcp_names``),
    #: wired by ``_scan_source``. A callable rather than a set, so the MCP
    #: projection reads the registry exactly when and as often as it always has.
    managed_mcp_names: Callable[[], frozenset[str]] = _managed_mcp_names_unwired

    def diagnostic(
        self,
        category: str,
        reason: str,
        *,
        unsupported: bool = False,
        count: int | None = None,
    ) -> None:
        key = (self.source_id, category, reason)
        if key in self._diagnostic_keys:
            if count is not None:
                existing_diagnostic = self.skipped[self._diagnostic_keys[key]]
                existing_diagnostic["count"] = max(int(existing_diagnostic.get("count", 0)), count)
            return
        diagnostic: dict[str, Any] = {
            "source_id": self.source_id,
            "category_id": category,
            "reason": reason,
        }
        if count is not None:
            diagnostic["count"] = count
        self._diagnostic_keys[key] = len(self.skipped)
        self.skipped.append(diagnostic)
        if unsupported:
            self.unsupported_count += 1

    def add(self, category: str, key: str, payload: Any) -> None:
        self.items[category].append(_Item(self.source_id, category, key, payload))


def _exists_safe(path: Path) -> bool:
    """``Path.exists()`` that cannot raise, for a foreign-supplied path.

    pathlib re-raises any errno it does not read as "absent"; its ignored set is
    ENOENT/ENOTDIR/EBADF/ELOOP and does **not** include ENAMETOOLONG. A foreign
    config names its own workspace paths, so a single component over NAME_MAX
    (255 on both macOS and Linux) reaches these probes and takes the whole scan
    down — surfacing as HTTP 500 from ``/api/onboarding/import/scan`` for EVERY
    source, not just the one that supplied the path. Note the trigger is
    per-component, not total length: a 301-char path is far below ``PATH_MAX``
    (1024 on macOS, 4096 on Linux) and still raises, so a total-length guard
    alone cannot close this.

    An unprobeable candidate is exactly a candidate to skip, so "absent" is the
    correct answer rather than an error.
    """
    try:
        return path.exists()
    except (OSError, ValueError):
        return False


def _is_file_safe(path: Path) -> bool:
    """``Path.is_file()`` counterpart of ``_exists_safe`` — see that docstring."""
    try:
        return path.is_file()
    except (OSError, ValueError):
        return False


def _is_dir_safe(path: Path) -> bool:
    """``Path.is_dir()`` counterpart of ``_exists_safe`` — see that docstring."""
    try:
        return path.is_dir()
    except (OSError, ValueError):
        return False


def _stat_is_link_like(file_stat: Any) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(file_stat.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _is_link_like(path: Path, file_stat: Any | None = None) -> bool:
    if file_stat is None:
        try:
            file_stat = path.lstat()
        except (OSError, ValueError):
            # ValueError, not just OSError: an embedded NUL makes the syscall
            # unreachable rather than failing, and this guard runs BEFORE every
            # other probe, so letting it raise would 500 the whole scan.
            return False
    return _stat_is_link_like(file_stat)


def _expand_root(raw: str, home: Path) -> Path:
    if raw == "~":
        return home
    if raw.startswith("~/") or raw.startswith("~\\"):
        return home / raw[2:]
    return Path(raw)


def _stat_kind(path: Path) -> str:
    """Return ``"dir"``, ``"file"`` or ``""`` without ever raising.

    ``Path.is_dir()`` swallows only the errnos ``pathlib`` deems "does not
    exist" — ENOENT, ENOTDIR, EBADF, ELOOP. ENAMETOOLONG is NOT among them, so
    a root whose final component exceeds the filesystem's limit raises
    ``OSError(36)`` instead of answering False. That root is reachable from two
    directions the engine does not control: a registered descriptor's
    ``home_dir`` and an env var naming the source's home. Either one crashing
    the existence probe denies the user EVERY other source, so an unanswerable
    stat is treated as "not present" — the same fail-closed reading the rest of
    discovery applies to input it cannot read.
    """
    try:
        if path.is_dir():
            return "dir"
        if path.is_file():
            return "file"
    except (OSError, ValueError):
        return ""
    return ""


def _safe_regular_file(
    path: Path,
    anchor: Path,
    scan: _Scan,
    category: str,
    *,
    max_bytes: int = _MAX_FILE_BYTES,
) -> bool:
    try:
        relative = path.relative_to(anchor)
    except ValueError:
        scan.diagnostic(category, "outside_source_root")
        return False
    current = anchor
    for part in relative.parts:
        current = current / part
        try:
            component_stat = current.lstat()
        except OSError:
            return False
        if _is_link_like(current, component_stat):
            scan.diagnostic(category, "symlink_rejected")
            return False
    try:
        file_stat = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(file_stat.st_mode):
        return False
    if is_sensitive_path(str(path)):
        scan.diagnostic(category, "sensitive_path_rejected")
        return False
    if file_stat.st_size > max_bytes:
        scan.diagnostic(category, "file_too_large")
        return False
    if scan.bytes_read.get(category, 0) + file_stat.st_size > _MAX_TOTAL_BYTES:
        scan.diagnostic(category, "source_byte_limit")
        return False
    return True


def _walk_files(
    base: Path,
    scan: _Scan,
    category: str,
    *,
    suffixes: tuple[str, ...] = (),
    names: tuple[str, ...] = (),
    excluded_parts: frozenset[str] = frozenset(),
    excluded_category: str = "",
    excluded_reason: str = "",
    count_files: bool = True,
) -> list[Path]:
    if not _exists_safe(base):
        return []
    if _is_link_like(base):
        scan.diagnostic(category, "symlink_rejected")
        return []
    if not _is_dir_safe(base):
        return []
    remaining = max(0, _MAX_FILES - scan.files_seen.get(category, 0)) if count_files else _MAX_FILES
    candidates: list[Path] = []
    omitted = 0
    excluded_count = 0
    visited_entries = 0
    traversal_omitted = 0
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        parent = Path(dirpath)
        if visited_entries >= _MAX_WALK_ENTRIES:
            traversal_omitted += len(dirnames) + len(filenames)
            dirnames[:] = []
            break
        kept_dirs: list[str] = []
        exhausted = False
        for dirname in sorted(dirnames):
            if visited_entries >= _MAX_WALK_ENTRIES:
                traversal_omitted += len(dirnames) - len(kept_dirs) + len(filenames)
                exhausted = True
                break
            visited_entries += 1
            candidate = parent / dirname
            if _is_link_like(candidate):
                scan.diagnostic(category, "symlink_rejected")
            elif dirname in (".git", "__pycache__", "node_modules"):
                continue
            else:
                kept_dirs.append(dirname)
        dirnames[:] = [] if exhausted else kept_dirs
        if exhausted:
            dirnames[:] = []
            break
        for index, filename in enumerate(sorted(filenames)):
            if visited_entries >= _MAX_WALK_ENTRIES:
                traversal_omitted += len(filenames) - index
                exhausted = True
                dirnames[:] = []
                break
            visited_entries += 1
            if names and filename not in names:
                continue
            if suffixes and not filename.lower().endswith(suffixes):
                continue
            candidate = parent / filename
            if _is_link_like(candidate):
                scan.diagnostic(category, "symlink_rejected")
                continue
            if excluded_parts:
                try:
                    parts = {part.casefold() for part in candidate.relative_to(base).parts}
                except ValueError:
                    parts = set()
                if parts & excluded_parts:
                    excluded_count += 1
                    continue
            if len(candidates) < remaining:
                candidates.append(candidate)
            else:
                omitted += 1
        if exhausted:
            break
    if excluded_count and excluded_category and excluded_reason:
        scan.diagnostic(excluded_category, excluded_reason, count=excluded_count)
    candidates.sort(key=lambda path: str(path).casefold())
    if omitted and count_files:
        scan.diagnostic(category, "file_count_limit", count=omitted)
    if traversal_omitted:
        scan.diagnostic(category, "walk_entry_limit", count=traversal_omitted)
    if omitted or traversal_omitted:
        scan.truncated_roots.add(os.path.normcase(os.path.abspath(str(base))))
    if count_files:
        scan.files_seen[category] = scan.files_seen.get(category, 0) + len(candidates)
    found: list[Path] = []
    for candidate in candidates:
        if _safe_regular_file(candidate, base, scan, category):
            found.append(candidate)
    return found


def _read_bytes(path: Path, anchor: Path, scan: _Scan, category: str) -> bytes | None:
    remaining_bytes = _MAX_TOTAL_BYTES - scan.bytes_read.get(category, 0)
    if remaining_bytes <= 0:
        scan.diagnostic(category, "source_byte_limit")
        return None
    read_limit = min(_MAX_FILE_BYTES, remaining_bytes)
    if not _safe_regular_file(path, anchor, scan, category, max_bytes=read_limit):
        return None
    try:
        content = safe_read_file_bytes_nolink(
            str(path),
            within_root=str(anchor),
            max_bytes=read_limit,
        )
    except FileTooLargeError:
        scan.diagnostic(category, "file_too_large")
        return None
    if content is None:
        scan.diagnostic(category, "read_failed")
        return None
    if len(content) > _MAX_FILE_BYTES:
        scan.diagnostic(category, "file_too_large")
        return None
    if scan.bytes_read.get(category, 0) + len(content) > _MAX_TOTAL_BYTES:
        scan.diagnostic(category, "source_byte_limit")
        return None
    scan.bytes_read[category] = scan.bytes_read.get(category, 0) + len(content)
    return content


def _read_text(
    path: Path,
    anchor: Path,
    scan: _Scan,
    category: str,
    *,
    max_bytes: int = _MAX_FILE_BYTES,
) -> str | None:
    content = _read_bytes(path, anchor, scan, category)
    if content is None:
        return None
    if len(content) > max_bytes:
        scan.diagnostic(category, "file_too_large")
        return None
    return content.decode("utf-8", errors="replace")


def _sanitize_text(text: str, scan: _Scan) -> str:
    bounded = text[:_MAX_TEXT_CHARS]
    cleaned, credential_warnings, url_warnings = redact_with_findings(bounded)
    scan.secret_count += len(credential_warnings) + len(url_warnings)
    return cleaned.strip()


def _count_secret_fields(value: Any) -> int:
    if isinstance(value, dict):
        count = 0
        for key, child in value.items():
            if _SECRET_KEY_RE.search(str(key)):
                count += max(1, _leaf_count(child))
            else:
                count += _count_secret_fields(child)
        return count
    if isinstance(value, list):
        return sum(_count_secret_fields(item) for item in value)
    return 0


def _leaf_count(value: Any) -> int:
    if isinstance(value, dict):
        return sum(max(1, _leaf_count(child)) for child in value.values())
    if isinstance(value, list):
        return sum(max(1, _leaf_count(child)) for child in value)
    return 1


def _strip_json5_comments(text: str) -> str:
    output: list[str] = []
    index = 0
    quote = ""
    while index < len(text):
        char = text[index]
        if quote:
            output.append(char)
            if char == "\\" and index + 1 < len(text):
                index += 1
                output.append(text[index])
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in ('"', "'"):
            quote = char
            output.append(char)
            index += 1
            continue
        if text[index : index + 2] == "//":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if text[index : index + 2] == "/*":
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _parse_json5(text: str) -> Any:
    stripped = _strip_json5_comments(text)
    output: list[str] = []
    index = 0
    while index < len(stripped):
        char = stripped[index]
        if char != "'":
            output.append(char)
            index += 1
            continue
        output.append('"')
        index += 1
        while index < len(stripped):
            char = stripped[index]
            if char == "'":
                output.append('"')
                index += 1
                break
            if char == "\\" and index + 1 < len(stripped):
                next_char = stripped[index + 1]
                if next_char == "'":
                    output.append("'")
                else:
                    output.extend(("\\", next_char))
                index += 2
                continue
            if char == '"':
                output.append('\\"')
            else:
                output.append(char)
            index += 1
    stripped = "".join(output)
    stripped = re.sub(
        r"(?P<prefix>[{,]\s*)(?P<key>[A-Za-z_$][A-Za-z0-9_$.-]*)(?P<colon>\s*:)",
        r'\g<prefix>"\g<key>"\g<colon>',
        stripped,
    )
    stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    return json.loads(stripped)


def _read_json(
    path: Path,
    anchor: Path,
    scan: _Scan,
    category: str,
    *,
    json5: bool = False,
) -> Any:
    text = _read_text(path, anchor, scan, category)
    if text is None:
        return None
    try:
        return _parse_json5(text) if json5 else json.loads(text)
    except (ValueError, RecursionError):
        scan.diagnostic(category, "invalid_config")
        return None


def _read_toml(path: Path, anchor: Path, scan: _Scan) -> dict[str, Any]:
    content = _read_bytes(path, anchor, scan, "settings")
    if content is None:
        return {}
    if _toml is None:
        scan.diagnostic("settings", "toml_parser_unavailable", unsupported=True)
        return {}
    try:
        result = _toml.loads(content.decode("utf-8", errors="strict"))
    except ValueError:
        scan.diagnostic("settings", "invalid_config")
        return {}
    return result if isinstance(result, dict) else {}


def _read_simple_yaml(path: Path, anchor: Path, scan: _Scan) -> dict[str, Any]:
    # PyYAML is a hard dependency; the SafeLoader base blocks arbitrary object
    # construction and parses full YAML (the previous hand-rolled parser silently
    # dropped MCP servers on any indentation other than 0/2 spaces).
    # _NoAliasSafeLoader additionally refuses anchors/aliases so a "billion-laughs"
    # foreign config cannot amplify into an exponential downstream traversal.
    # Bound the input with an explicit YAML cap and catch every parser failure
    # mode — a malformed, alias-bearing, or pathologically nested config must
    # degrade to a diagnostic, never raise out of the off-loop scan (deeply nested
    # flow input raises RecursionError, which is neither YAMLError nor ValueError).
    text = _read_text(path, anchor, scan, "settings", max_bytes=_MAX_YAML_BYTES)
    if text is None:
        return {}
    try:
        result = _load_no_alias_yaml(text)
    except (yaml.YAMLError, RecursionError, ValueError):
        scan.diagnostic("settings", "invalid_config")
        return {}
    return result if isinstance(result, dict) else {}


def _skill_package(
    scan: _Scan,
    root: Path,
    manifest: Path,
) -> dict[str, str] | None:
    package_root = manifest.parent
    files: dict[str, str] = {}
    package_bytes = 0
    for path in _walk_files(package_root, scan, "skills"):
        content = _read_bytes(path, package_root, scan, "skills")
        if content is None:
            return None
        package_bytes += len(content)
        if package_bytes > _MAX_SKILL_PACKAGE_BYTES:
            scan.diagnostic("skills", "skill_package_too_large")
            return None
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            scan.diagnostic("skills", "binary_skill_asset_excluded", unsupported=True)
            return None
        screened, credential_warnings, url_warnings = redact_with_findings(text)
        scan.secret_count += len(credential_warnings) + len(url_warnings)
        if credential_warnings or url_warnings or screened != text:
            scan.diagnostic("skills", "credential_bearing_skill")
            return None
        if path.name == "SKILL.md":
            metadata, _ = _frontmatter(text)
            # ``_frontmatter`` maps ANY ``key:`` line (indented prose included,
            # last write wins) and stops at an indented ``---``, while the
            # loader honors only column-0 keys and closes only at a column-0
            # ``---`` — so its collapsed map must not decide activation.
            # ``_column0_activation_declared`` mirrors the loader's region and
            # key rules; the ``triggers`` presence check on the map is kept as
            # an extra conservative layer (an indented mention only ever makes
            # the gate stricter).
            if _column0_activation_declared(text) or "triggers" in metadata:
                scan.diagnostic(
                    "skills",
                    "automatic_activation_excluded",
                    unsupported=True,
                )
                return None
        relative = path.relative_to(package_root)
        if relative.is_absolute() or ".." in relative.parts:
            scan.diagnostic("skills", "outside_source_root")
            return None
        files[relative.as_posix()] = text
    if os.path.normcase(os.path.abspath(str(package_root))) in scan.truncated_roots:
        scan.diagnostic("skills", "skill_package_truncated", unsupported=True)
        return None
    if "SKILL.md" not in files:
        return None
    return files


def _named_descendant_dirs(
    base: Path,
    scan: _Scan,
    category: str,
    names: frozenset[str],
) -> list[Path]:
    if not _exists_safe(base) or not _is_dir_safe(base):
        return []
    if _is_link_like(base):
        scan.diagnostic(category, "symlink_rejected")
        return []
    found: list[Path] = []
    visited_entries = 0
    traversal_omitted = 0
    for dirpath, dirnames, _filenames in os.walk(base, followlinks=False):
        parent = Path(dirpath)
        if visited_entries >= _MAX_WALK_ENTRIES:
            traversal_omitted += len(dirnames)
            dirnames[:] = []
            break
        kept: list[str] = []
        for index, dirname in enumerate(sorted(dirnames)):
            if visited_entries >= _MAX_WALK_ENTRIES:
                traversal_omitted += len(dirnames) - index
                dirnames[:] = []
                break
            visited_entries += 1
            candidate = parent / dirname
            if _is_link_like(candidate):
                scan.diagnostic(category, "symlink_rejected")
                continue
            if dirname.casefold() in names:
                found.append(candidate)
                continue
            kept.append(dirname)
        else:
            dirnames[:] = kept
    if traversal_omitted:
        scan.diagnostic(category, "walk_entry_limit", count=traversal_omitted)
    return found


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split frontmatter for the import screen (``frontmatter.ONBOARDING_IMPORT``).

    Deliberately lenient on the opener and on indented keys — narrowing what
    this map reads would weaken the diagnostics built on it. Its grammar
    diverges from the loader's (indented prose can overwrite a real value,
    and the closer must be an exact ``---`` line, not a ``---`` prefix), so
    the activation DECISION does not ride on this map alone:
    ``_column0_activation_declared`` mirrors the loader's region and key
    rules separately.
    """
    return split_frontmatter(text, ONBOARDING_IMPORT)


def _column0_activation_declared(text: str) -> bool:
    """True if any column-0 auto-activation declaration is in the frontmatter.

    The activation decision must mirror what ``SkillsLoader._parse_frontmatter``
    can conclude after install, not ``_frontmatter``'s collapsed map (where an
    indented prose line like ``  always: false`` overwrites the real value, and
    an indented ``---`` inside a block scalar truncates the scan). Region and
    key rules therefore match the loader exactly: the frontmatter closes at the
    first line that STARTS with ``---`` (the loader's ``\\n---`` regex), and
    only column-0 keys count. A column-0 ``always`` activates on a truthy plain
    value or ANY block-scalar header the loader can resolve (fail-closed: this
    parser cannot see the continuation lines the loader resolves, so it assumes
    the worst). That set is read from
    :func:`~kiro_crew.frontmatter.parse_block_scalar_header` rather than
    re-listed here, because the gate is fail-closed only while its detected set
    is a SUPERSET of what the loader resolves. It once held its own list of six
    bare indicators, and widening the loader to the full header grammar without
    it turned this screen fail-OPEN: ``always: |2-`` over a ``true``
    continuation was not detected, was installed verbatim, and then read as
    ``always == "true"`` -- external content self-activating into every session,
    the exact hazard ``automatic_activation_excluded`` exists to reject. One
    matcher means widening the loader can only ever make this stricter.
    A column-0 ``triggers`` key
    activates by presence. ANY activating declaration rejects — stricter than
    the loader's last-wins on duplicate keys, which only ever diverges in the
    conservative direction.
    """
    if not text.startswith("---"):
        return False
    for line in text.splitlines()[1:]:
        if line.startswith("---"):
            break
        if ":" not in line or line[:1].isspace():
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        if key == "triggers":
            return True
        if key != "always":
            continue
        # Strip whitespace AFTER removing the quotes as well as before: the
        # loader's consumers compare ``.strip().lower() == "true"``, so
        # ``always: " true "`` activates a skill -- while stripping only on the
        # outside leaves ``" true "`` -> `` true ``, which matches no truthy
        # word here and let that spelling through the screen. This run-strip is
        # deliberately WIDER than the loader's unquote (one matched wrapping
        # level; see ``frontmatter.FrontmatterDialect.strip_quotes``): every
        # spelling the loader reads as truthy is run-strip truthy too, so the
        # divergence only ever detects MORE spellings as activating -- the
        # fail-closed direction this gate must err on.
        value = raw.strip().strip("\"'").strip().casefold()
        if value in {"1", "true", "yes"} or parse_block_scalar_header(value) is not None:
            return True
    return False


def _parse_configs(
    scan: _Scan,
    configs: list[tuple[Path, Path, str]],
) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path, anchor, kind in configs:
        marker = os.path.normcase(os.path.abspath(str(path)))
        if marker in seen or not _exists_safe(path):
            continue
        seen.add(marker)
        data: Any
        if kind == "toml":
            data = _read_toml(path, anchor, scan)
        elif kind == "yaml":
            data = _read_simple_yaml(path, anchor, scan)
        else:
            data = _read_json(path, anchor, scan, "settings", json5=kind == "json5")
        if isinstance(data, dict):
            # MCP entries are counted and diagnosed once by _add_mcp_configs.
            # Exclude them here so a credential-bearing server does not inflate
            # the aggregate skipped count through both config and MCP paths.
            secret_data = dict(data)
            secret_data.pop("mcpServers", None)
            secret_data.pop("mcp_servers", None)
            nested_mcp = secret_data.get("mcp")
            if isinstance(nested_mcp, dict) and "servers" in nested_mcp:
                nested_mcp = dict(nested_mcp)
                nested_mcp.pop("servers", None)
                secret_data["mcp"] = nested_mcp
            scan.secret_count += _count_secret_fields(secret_data)
            parsed.append(data)
    return parsed


def _sqlite_database_is_safe(
    path: Path,
    anchor: Path,
    scan: _Scan,
    category: str,
) -> bool:
    try:
        main_stat = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(main_stat.st_mode) or main_stat.st_nlink != 1:
        scan.diagnostic(category, "hardlink_rejected")
        return False
    if main_stat.st_size > _MAX_DB_BYTES:
        scan.diagnostic(category, "database_too_large")
        return False

    sidecars: list[Path] = []
    total_bytes = main_stat.st_size
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        try:
            sidecar_stat = sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            scan.diagnostic(category, "unsafe_database_sidecar")
            return False
        if not stat.S_ISREG(sidecar_stat.st_mode) or sidecar_stat.st_nlink != 1:
            scan.diagnostic(category, "unsafe_database_sidecar")
            return False
        sidecars.append(sidecar)
        total_bytes += sidecar_stat.st_size
    if total_bytes > _MAX_DB_BYTES:
        scan.diagnostic(category, "database_too_large")
        return False

    return _safe_regular_file(
        path,
        anchor,
        scan,
        category,
        max_bytes=_MAX_DB_BYTES,
    ) and all(
        _safe_regular_file(
            sidecar,
            anchor,
            scan,
            category,
            max_bytes=_MAX_DB_BYTES,
        )
        for sidecar in sidecars
    )


def _sqlite_snapshot(
    path: Path,
    anchor: Path,
    scan: _Scan,
    category: str,
) -> Path | None:
    """Copy an opened, validated SQLite database and sidecars to a private tree."""
    if not _sqlite_database_is_safe(path, anchor, scan, category):
        return None
    sidecars = [
        sidecar
        for suffix in ("-wal", "-shm")
        for sidecar in (Path(f"{path}{suffix}"),)
        if sidecar.exists()
    ]
    snapshot_dir = Path(tempfile.mkdtemp(prefix="kirocrew-import-sqlite-"))
    try:
        for source in (path, *sidecars):
            content = safe_read_file_bytes_nolink(
                str(source),
                within_root=str(anchor),
                max_bytes=_MAX_DB_BYTES,
            )
            if content is None:
                scan.diagnostic(category, "database_read_failed")
                raise OSError(f"could not snapshot {source}")
            if scan.bytes_read.get(category, 0) + len(content) > _MAX_TOTAL_BYTES:
                scan.diagnostic(category, "source_byte_limit")
                raise OSError("SQLite snapshot exceeds source byte limit")
            target = snapshot_dir / source.name
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            scan.bytes_read[category] = scan.bytes_read.get(category, 0) + len(content)
        return snapshot_dir / path.name
    except (OSError, FileTooLargeError):
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        return None


@contextmanager
def _open_snapshot_db(
    path: Path,
    anchor: Path,
    scan: _Scan,
    category: str,
) -> Iterator[sqlite3.Connection | None]:
    """Snapshot a SQLite DB, open the copy read-only, and guarantee cleanup.

    Yields None when the database could not be snapshotted (the snapshot path has
    already emitted its own diagnostic) or could not be opened (emits
    ``database_open_failed`` here), so every caller handles both failure modes
    with a single ``if connection is None`` guard. The private snapshot tree and
    the connection are always released on exit, even when the caller's body
    returns early or raises.
    """
    snapshot = _sqlite_snapshot(path, anchor, scan, category)
    if snapshot is None:
        yield None
        return
    connection: sqlite3.Connection | None = None
    try:
        try:
            connection = sqlite3.connect(snapshot.absolute().as_uri() + "?mode=ro", uri=True)
        except (OSError, sqlite3.Error, ValueError):
            scan.diagnostic(category, "database_open_failed")
            yield None
            return
        yield connection
    finally:
        if connection is not None:
            connection.close()
        shutil.rmtree(snapshot.parent, ignore_errors=True)


def _sqlite_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


_MAX_DECODED_VALUE_DEPTH = 8


class _TooDeepToScreen(Exception):
    """A decoded value nests deeper than the screen will walk."""


def _decoded_value_strings(value: Any, depth: int = 0) -> Iterator[str]:
    """Yield every string leaf (and dict key) of a decoded JSON value.

    Raises :class:`_TooDeepToScreen` past ``_MAX_DECODED_VALUE_DEPTH`` rather than
    returning. Silently stopping the walk would leave the deeper leaves UNSCREENED
    while the caller reported the value clean — a credential nested 12 levels down
    would then reach the lesson tier. An unscreenable value must be refused, not
    partially screened.
    """
    if depth > _MAX_DECODED_VALUE_DEPTH:
        raise _TooDeepToScreen
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child_key, child in value.items():
            if isinstance(child_key, str):
                yield child_key
            yield from _decoded_value_strings(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _decoded_value_strings(child, depth + 1)


def _decoded_value_is_unsafe(value: Any, scan: _Scan) -> str:
    """Return a diagnostic reason if a DECODED DB value fails a content screen.

    The caller screens ``value_json`` — the raw JSON text — but what gets written
    is the ``json.loads`` result, and every JSON escape survives a pre-decode
    screen: a newline is stored as backslash-n, so an injection pattern cannot
    match, and ``\\u0041\\u004b\\u0049\\u0041`` hides a credential outright. So the
    screens must run again here, on the decoded strings.

    This matters beyond ``memories``: ``_SEMANTIC_PREFIXES`` includes ``lesson.``,
    and ``VectorMemoryStore.get_lessons()`` selects ``key LIKE 'lesson.%'`` — so a
    ``lesson.*`` row lands in the tier that ``get_lessons_context()`` injects into
    every session as authoritative, exactly like an ``instructions`` item.

    Fails CLOSED on a value too deeply nested to walk: a partially-screened value
    reported as clean is worse than a refused one.
    """
    try:
        texts = list(_decoded_value_strings(value))
    except _TooDeepToScreen:
        return "unscreenable_memory_record"
    for text in texts:
        if _sanitize_text(text, scan) != text[:_MAX_TEXT_CHARS].strip():
            return "credential_bearing_memory"
        if contains_injection(text):
            return "injection_memory_excluded"
    return ""
