"""Convert a manifest-declared plugin package into an installable Kiro Crew app.

The input is a directory whose manifest declares its resources as root-relative
paths: skills, MCP server configuration, connector directories and hook files.
The mapping from each declared kind to a Kiro Crew extension point -- and the
reason for every kind that has no mapping -- is
``docs/system-specs/modules/harness-plugin-mapping.md``. The converter's own
contract is ``docs/system-specs/modules/plugin-import.md``.

Two properties are load-bearing and are what the tests pin:

**No foreign code runs.** Conversion reads JSON and copies files. Nothing in the
source package is imported, executed, or spawned, at conversion time or after.

**The package root is the authority boundary.** Every declared path must be
written ``./``-relative, must not traverse, and must still resolve under the
package root after symlinks are resolved. A path that escapes is refused with
``resource_outside_root`` rather than clamped, and a symlink found *inside* a
copied resource is skipped rather than followed -- the escape a converter would
otherwise hand a caller is a file outside the package appearing inside an
installed app.

What the converter deliberately does NOT do is emit anything for a kind it
cannot map. An unmapped kind is reported, and recorded as provenance under the
emitted manifest's forward-compatible ``extra`` block, so a reader of the
installed app can see what was left behind instead of assuming the whole package
arrived.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.manifest import (
    KEBAB_RE,
    RESERVED_APP_NAMES,
    RESERVED_APP_PATH_SEGMENTS,
    RESERVED_ROUTE_APP_NAMES,
    SEMVER_RE,
    AppManifest,
)

# The manifest file name is the same in both discovery families.
MANIFEST_FILENAME = "plugin.json"

# A root-level manifest is only the schema-qualified form when its ``$schema``
# names the published plugin-manifest schema namespace. Anything else at the
# root is some other file that happens to share the name, so discovery falls
# through to the vendor-prefixed locations.
SCHEMA_NAMESPACE_PREFIX = "https://agent-plugins.org/schemas/"

# Vendor-prefixed manifest directories are matched by shape, not by an
# allowlist of vendor names: a dot-prefixed ``*-plugin`` directory holding the
# manifest. Sorted iteration makes the pick deterministic when a package
# carries several.
VENDOR_MANIFEST_DIR_GLOB = ".*-plugin"

FORMAT_SCHEMA_QUALIFIED = "schema-qualified-root"
FORMAT_VENDOR_DIRECTORY = "vendor-directory"

# A skill is a directory holding this file. Discovery is recursive under each
# declared skills root, matching the source format's default.
SKILL_ENTRY_FILENAME = "SKILL.md"
DEFAULT_SKILLS_DIR = "skills"

# Bounds. A converted package is third-party input, so every unbounded loop over
# it gets a ceiling; hitting one is a reported warning, never a silent trim.
MAX_SKILLS = 200
MAX_SKILL_TREE_DEPTH = 8

#: Largest number of directory entries skill DISCOVERY may examine. Discovery runs
#: BEFORE the import budget exists -- it is what decides which trees that budget
#: will be spent on -- so the budget cannot bound it and it needs a ceiling of its
#: own. Without one a wide package fills memory with frontier paths before a
#: single file is copied, and a single directory holding millions of entries does
#: it inside one ``iterdir``. Applied to both: the per-directory listing stops at
#: this many, and so does the total across the walk.
MAX_DISCOVERY_ENTRIES = 20_000
MAX_RESOURCE_BYTES = 32 * 1024 * 1024

#: Read size for one step of a bounded file copy. Only a buffering choice -- the
#: number of bytes copied is decided by the entry's own ``fstat``, never by this
#: -- so it trades syscalls against a transient buffer and nothing else.
_COPY_CHUNK_BYTES = 1024 * 1024
# Upper bound on the retained skip-description list: a pathologically wide tree
# would otherwise accumulate one string per skipped entry with no ceiling. Past
# the cap the list stops growing and records that it was truncated.
MAX_SKIP_DESCRIPTIONS = 1000

#: Cumulative ceilings for ONE import, across every skill it copies.
#:
#: ``MAX_RESOURCE_BYTES`` bounds one file and ``MAX_SKILL_TREE_DEPTH`` bounds how
#: deep the walk goes; neither bounds BREADTH, so a package of individually legal
#: files -- up to ``MAX_SKILLS`` trees of them -- could still copy without end
#: and fill the disk. These are the counters that make the import as a whole
#: bounded, which is what this module's own contract asks of every loop over
#: untrusted input.
MAX_IMPORT_FILES = 20_000
MAX_IMPORT_BYTES = 512 * 1024 * 1024


class _ImportBudget:
    """Files and bytes still available to one import. Shared across its trees."""

    __slots__ = ("files", "nbytes", "exhausted")

    def __init__(self) -> None:
        self.files = 0
        self.nbytes = 0
        self.exhausted = False

    def take_item(self) -> bool:
        """Charge one zero-byte ITEM -- a directory. Same count, no bytes."""
        return self.take(0)

    def take(self, size: int) -> bool:
        """Charge one file of *size* bytes, or report the budget spent (and latch it)."""
        if self.files + 1 > MAX_IMPORT_FILES or self.nbytes + size > MAX_IMPORT_BYTES:
            self.exhausted = True
            return False
        self.files += 1
        self.nbytes += size
        return True

    def refund(self, size: int) -> None:
        """Return a charge for a file that was not emitted.

        A charge is taken before the copy, from the size the copy is bounded to.
        A copy that then fails emits nothing, so holding its charge shrinks the
        budget for every later file and a tree of failures can spend the whole
        allowance on nothing. ``exhausted`` stays latched: a refund only ever
        follows a ``take`` that SUCCEEDED, so the latch is not set here, and
        clearing it would let a genuinely spent budget reopen.
        """
        self.files = max(0, self.files - 1)
        self.nbytes = max(0, self.nbytes - size)


# Source hook events that have a same-meaning event on the agent hook surface
# (``agent.kiro_hooks``). The surface is operator configuration and not an app
# contribution, so even a mapped event is NOT emitted into the app manifest --
# it is reported. See mapping doc diffs D1 and D2.
AGENT_HOOK_EVENT_EQUIVALENT = {
    "PreToolUse": "preToolUse",
    "PostToolUse": "postToolUse",
    "UserPromptSubmit": "userPromptSubmit",
    "Stop": "stop",
}

_MANIFEST_KNOWN_KEYS = frozenset(
    {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "license",
        "homepage",
        "repository",
        "keywords",
        "skills",
        "mcpServers",
        "apps",
        "hooks",
        "interface",
    }
)

# Interface keys this converter CONSUMES into a target field. Every other key in
# the block is carried as provenance -- a wholesale remainder rather than an
# allowlist, because an allowlist silently drops the next presentation field the
# source format adds (and it already spells some links two ways).
_CONSUMED_INTERFACE_KEYS = frozenset(
    {"displayName", "shortDescription", "longDescription", "developerName"}
)

# Top-level source fields with real information and no field on an installed
# app's manifest.
_CARRIED_MANIFEST_KEYS = ("homepage", "repository")


class PluginImportError(Exception):
    """A conversion refused. ``code`` is stable and machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class MappedKind:
    """One source kind that reached a Kiro Crew extension point."""

    kind: str
    target: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "target": self.target, "detail": self.detail}


@dataclass
class UnmappedKind:
    """One source kind with no target, and why.

    ``bucket`` is the mapping doc's bucket letter, so a reader can go from a
    converted app straight to the row that explains it.
    """

    kind: str
    bucket: str
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "bucket": self.bucket,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class ImportReport:
    """What the conversion did, in full. Returned and also emitted as provenance."""

    source_root: str
    manifest_path: str
    source_format: str
    app_name: str
    mapped: list[MappedKind] = field(default_factory=list)
    unmapped: list[UnmappedKind] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceFormat": self.source_format,
            "sourceManifest": self.manifest_path,
            "appName": self.app_name,
            "mapped": [m.to_dict() for m in self.mapped],
            "unmapped": [u.to_dict() for u in self.unmapped],
            "warnings": list(self.warnings),
        }

    def render_text(self) -> str:
        lines = [
            f"source:   {self.source_root}",
            f"manifest: {self.manifest_path} ({self.source_format})",
            f"app:      {self.app_name}",
            "",
            "mapped:",
        ]
        if self.mapped:
            for m in self.mapped:
                suffix = f" -- {m.detail}" if m.detail else ""
                lines.append(f"  {m.kind} -> {m.target}{suffix}")
        else:
            lines.append("  (nothing)")
        lines.append("")
        lines.append("not mapped:")
        if self.unmapped:
            for u in self.unmapped:
                suffix = f" -- {u.detail}" if u.detail else ""
                lines.append(f"  {u.kind} [{u.bucket}] {u.reason}{suffix}")
        else:
            lines.append("  (nothing)")
        if self.warnings:
            lines.append("")
            lines.append("warnings:")
            for w in self.warnings:
                lines.append(f"  {w}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Manifest discovery
# ---------------------------------------------------------------------------


#: Largest a declared manifest may be before it is refused unread. A manifest is
#: parsed whole into memory, so its size is the bound, and nothing about a
#: legitimate one approaches this.
MAX_MANIFEST_BYTES = 2 * 1024 * 1024


def _has_reparse_attribute(st: object) -> bool:
    """Whether a stat result carries the reparse-point attribute.

    Split out so it can be exercised directly: POSIX cannot create a junction, and
    faking one by patching ``os.lstat`` patches it for the whole process, including
    the temporary-directory teardown that runs after the test.

    Deliberately broader than the mount-point tag ``platform_compat`` matches: any
    reparse tag at all is a boundary here, because the question is whether the copy
    may descend, not which flavour of redirection it is.
    """
    attrs = getattr(st, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & reparse)


def _is_link_or_reparse(path: Path) -> bool:
    """True for a symlink OR any other reparse point, e.g. a Windows junction.

    ``is_symlink()`` alone is not the boundary check: a junction is a reparse
    point that it does not report, so a plugin declaring one walks the copy
    straight out of the package root and pulls outside files into the generated
    app.

    The symlink-or-junction question is answered by
    :func:`platform_compat.is_link_or_junction`, which keeps the reparse-tag
    constants and the ``os.path.isjunction`` fallback for 3.10/3.11 in one place.
    Two properties are added on top of it, and each is what this boundary needs
    rather than what that helper promises:

    * a failed stat answers True -- an entry that cannot be judged is not one to
      descend into, where ``is_link_or_junction`` answers False on ``OSError``;
    * any reparse tag counts, not only ``IO_REPARSE_TAG_MOUNT_POINT``.
    """
    try:
        if platform_compat.is_link_or_junction(path):
            return True
        st = os.lstat(path)
    except OSError:
        return True
    return _has_reparse_attribute(st)


def _read_json_object(path: Path, what: str) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PluginImportError("manifest_unreadable", f"cannot read {what}: {exc}") from exc
    if size > MAX_MANIFEST_BYTES:
        # Refused BEFORE the read: read_text on a declared, externally supplied
        # path pulls the whole file into memory, so checking afterwards is checking
        # after the harm.
        raise PluginImportError(
            "manifest_too_large",
            f"{what} is {size} bytes, over the {MAX_MANIFEST_BYTES}-byte limit",
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PluginImportError("manifest_unreadable", f"cannot read {what}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PluginImportError("manifest_not_json", f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PluginImportError("manifest_not_object", f"{what} must be a JSON object")
    return data


def _is_schema_qualified(path: Path) -> bool:
    """True when a root manifest declares the published schema namespace."""
    if not path.is_file() or _is_link_or_reparse(path):
        return False
    try:
        data = _read_json_object(path, "plugin manifest")
    except PluginImportError:
        # This is a PROBE, so every refusal is an answer of False: unreadable,
        # oversized, not JSON, not an object. Routing it through the bounded reader
        # is what keeps an oversized candidate from being read whole just to learn
        # whether it declares the schema namespace.
        return False
    if not isinstance(data, dict):
        return False
    schema = data.get("$schema")
    return isinstance(schema, str) and schema.startswith(SCHEMA_NAMESPACE_PREFIX)


def read_manifest_name(manifest_path: Path) -> "str | None":
    """The ``name`` a package manifest declares, or None when it declares none.

    Public because a caller deriving the app name needs it BEFORE conversion
    starts, and hand-rolling that read is a second place for the size ceiling and
    the is-it-an-object check to be forgotten. Both live in
    :func:`_read_json_object`, so both apply here: an oversized manifest is
    refused unread, and one whose top level is a list answers ``manifest_not_object``
    rather than crashing on a missing ``get``.

    A ``name`` that is absent, empty or not a string reads as None, which the
    caller answers by naming the app after its source directory.
    """
    data = _read_json_object(manifest_path, "the plugin manifest")
    name = data.get("name")
    return name if isinstance(name, str) and name else None


def find_plugin_manifest(root: Path) -> tuple[Path, str]:
    """Locate the package manifest.

    A schema-qualified root manifest wins. Otherwise the first vendor-prefixed
    directory holding one, in sorted order, so a package carrying several
    resolves the same way on every machine.
    """
    if not root.is_dir():
        raise PluginImportError("source_not_a_directory", f"not a directory: {root}")

    root_manifest = root / MANIFEST_FILENAME
    if _is_schema_qualified(root_manifest):
        return root_manifest, FORMAT_SCHEMA_QUALIFIED

    for candidate_dir in sorted(root.glob(VENDOR_MANIFEST_DIR_GLOB)):
        if not candidate_dir.is_dir() or _is_link_or_reparse(candidate_dir):
            continue
        candidate = candidate_dir / MANIFEST_FILENAME
        if candidate.is_file() and not _is_link_or_reparse(candidate):
            return candidate, FORMAT_VENDOR_DIRECTORY

    raise PluginImportError(
        "manifest_not_found",
        (
            f"no plugin manifest under {root}: expected a schema-qualified "
            f"{MANIFEST_FILENAME} at the root, or {VENDOR_MANIFEST_DIR_GLOB}/"
            f"{MANIFEST_FILENAME}"
        ),
    )


# ---------------------------------------------------------------------------
# Declared-path containment
# ---------------------------------------------------------------------------


def resolve_declared_path(root: Path, raw: object) -> Path:
    """Resolve one declared path against the package root, or refuse it.

    The source format writes every path ``./``-relative. That prefix is required
    here too, because accepting a bare ``skills`` would also accept ``/etc`` on a
    reader that only stripped a leading dot-slash.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PluginImportError("invalid_declared_path", f"declared path is not a string: {raw!r}")
    text = raw.strip()
    if not text.startswith("./"):
        raise PluginImportError(
            "invalid_declared_path", f"declared path must start with './': {text!r}"
        )
    body = text[2:]
    if not body or body in (".", "/"):
        raise PluginImportError("invalid_declared_path", f"declared path is empty: {text!r}")
    if body.startswith("/") or body.startswith("\\"):
        raise PluginImportError("invalid_declared_path", f"declared path is rooted: {text!r}")
    if len(body) >= 2 and body[1] == ":":
        raise PluginImportError("invalid_declared_path", f"declared path is rooted: {text!r}")
    for segment in re.split(r"[\\/]", body):
        if segment == "..":
            raise PluginImportError("invalid_declared_path", f"declared path traverses: {text!r}")

    root_resolved = root.resolve()
    candidate = (root_resolved / body.replace("\\", "/")).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise PluginImportError(
            "resource_outside_root",
            f"declared resource {text!r} resolves outside the package root {root_resolved}",
        )
    return candidate


def _declared_path_list(root: Path, raw: object, kind: str) -> list[Path]:
    """Normalize the path-or-list-of-paths shape both formats use."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [resolve_declared_path(root, raw)]
    if isinstance(raw, list):
        return [resolve_declared_path(root, item) for item in raw]
    raise PluginImportError(
        "invalid_declared_path",
        f"{kind} must be a path or a list of paths, got {type(raw).__name__}",
    )


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------


def _copy_tree_without_symlinks(
    src: Path, dst: Path, depth: int = 0, budget: "_ImportBudget | None" = None
) -> tuple[int, list[str]]:
    """Copy a directory tree, skipping every symlink rather than following it.

    Returns ``(files_copied, skipped_descriptions)``. Following a symlink would
    let a file outside the package root land inside the emitted app, which is
    the one thing ``resolve_declared_path`` exists to prevent -- so the same rule
    applies to the tree walk, not just to the declared path.

    ``depth`` bounds the recursion the same way ``_discover_skill_dirs`` bounds
    its walk: an unbounded recurse on a pathologically deep tree raises
    ``RecursionError`` mid-copy and leaves a partial import, so a subtree past
    the limit is skipped (and recorded) rather than descended.

    ``budget`` bounds BREADTH -- total files and total bytes for the whole
    import, not per file. Once it is spent every further file is skipped and
    recorded, so a package of individually legal files cannot fill the disk
    between them. A caller that passes none gets a fresh budget for this tree
    alone, which is right for a single-tree copy and wrong for a walk over many
    (``convert`` passes one shared budget for exactly that reason).
    """
    if budget is None:
        budget = _ImportBudget()
    copied = 0
    skipped: list[str] = []
    if depth > MAX_SKILL_TREE_DEPTH:
        skipped.append(f"tree deeper than {MAX_SKILL_TREE_DEPTH} levels, skipped: {src}")
        return copied, skipped
    # A directory costs an inode and a dirent whether or not it holds a file, so
    # it is charged like one: a package whose breadth is thousands of EMPTY
    # directories would otherwise create every one of them without spending the
    # budget, and exhaust the destination's inodes with a partly written import.
    if not budget.take_item():
        skipped.append(
            f"import budget spent ({MAX_IMPORT_FILES} items / "
            f"{MAX_IMPORT_BYTES} bytes), directory skipped: {src}"
        )
        return copied, skipped
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        target = dst / entry.name
        if _is_link_or_reparse(entry):
            skipped.append(f"symlink skipped: {entry}")
            continue
        if entry.is_dir():
            sub_copied, sub_skipped = _copy_tree_without_symlinks(entry, target, depth + 1, budget)
            copied += sub_copied
            skipped.extend(sub_skipped)
            continue
        note = _copy_regular_file_nofollow(entry, target, budget)
        if note is not None:
            skipped.append(note)
            continue
        copied += 1
    if len(skipped) > MAX_SKIP_DESCRIPTIONS:
        overflow = len(skipped) - MAX_SKIP_DESCRIPTIONS
        skipped = skipped[:MAX_SKIP_DESCRIPTIONS]
        skipped.append(f"... and {overflow} more skipped (list capped at {MAX_SKIP_DESCRIPTIONS})")
    return copied, skipped


def _copy_regular_file_nofollow(entry: Path, target: Path, budget: "_ImportBudget") -> "str | None":
    """Copy one file through a NO-FOLLOW descriptor. Returns a skip note, or None.

    The kind, the size and the budget are all decided from ``fstat`` on the
    descriptor that is then read, so the object copied is the object judged. A
    path-based ``is_symlink()`` check followed by a path-based copy decides on one
    object and copies another: an agent that swaps the file for a symlink in
    between has the importer read a file outside the package and write it into the
    emitted app, which is the single thing this walk exists to prevent.

    ``O_NOFOLLOW`` is absent on Windows, where the flag degrades to 0. The
    symlink pre-check in the caller still applies there, so that platform keeps
    the protection it had and gains the fstat-decided kind and size.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(entry, flags)
    except OSError:
        # ELOOP is the race being refused; anything else is an unreadable entry.
        return f"unreadable or not a regular file, skipped: {entry}"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return f"not a regular file, skipped: {entry}"
        if st.st_size > MAX_RESOURCE_BYTES:
            return f"over {MAX_RESOURCE_BYTES} bytes, skipped: {entry}"
        if not budget.take(st.st_size):
            return (
                f"import budget spent ({MAX_IMPORT_FILES} items / "
                f"{MAX_IMPORT_BYTES} bytes), skipped: {entry}"
            )
        # The SINK is opened under the same rule as the source, through a
        # descriptor that refuses to follow anything. O_EXCL is what closes the
        # named attack: an agent that plants a symlink -- or any file -- at
        # `target` between the walk and this open would otherwise have the
        # importer truncate and overwrite whatever it points at, with the host's
        # authority. O_EXCL refuses an existing path outright, so no pre-existing
        # object can be written through, and O_NOFOLLOW refuses a symlinked final
        # component on the platforms that define it.
        sink_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        try:
            sink_fd = os.open(target, sink_flags, 0o600)
        except OSError:
            budget.refund(st.st_size)
            return f"output path already exists or is a link, skipped: {entry}"

        note: str | None = None
        with open(fd, "rb", closefd=False) as source, open(sink_fd, "wb") as sink:
            # Exactly the bytes the budget was charged for, not "to EOF". The
            # ceiling and the budget were both decided from the fstat above, and
            # a source still being written grows after it -- copyfileobj would
            # then follow the file past the size that was measured, so the
            # output exceeds the declared ceiling and the budget understates
            # what was spent. Copying st_size bytes makes the charge and the
            # output the same number by construction.
            remaining = st.st_size
            while remaining > 0:
                chunk = source.read(min(_COPY_CHUNK_BYTES, remaining))
                if not chunk:
                    # Shorter than it measured: the entry is being rewritten
                    # under us, so what remains on disk is not the file the
                    # ceiling and the budget were decided from. Refused rather
                    # than published truncated.
                    note = f"shrank while copying, skipped: {entry}"
                    break
                sink.write(chunk)
                remaining -= len(chunk)
        if note is not None:
            # A skip note is a promise that nothing was emitted. The bytes
            # written so far are already on disk and would convert as a valid,
            # shorter skill, so the promise has to be made true here -- and when
            # it cannot be, the note says so rather than claiming a clean skip.
            if not _discard_partial_output(target, st.st_size, budget):
                return f"{note} (partial output could not be removed)"
            return note
        # Times from the SAME fstat, not a second path lookup: `copy2` preserved
        # them, and re-resolving the path to read them would reopen the window.
        os.utime(target, (st.st_atime, st.st_mtime))
    except OSError:
        if not _discard_partial_output(target, st.st_size, budget):
            return f"copy failed, partial output could not be removed: {entry}"
        return f"copy failed, skipped: {entry}"
    finally:
        os.close(fd)
    return None


def _discard_partial_output(target: Path, charged: int, budget: "_ImportBudget") -> bool:
    """Remove a half-written output and give its charge back. Did it go?

    Every failure after the sink exists reaches this. Leaving the file would
    publish a truncated resource as though it had been emitted whole, and
    leaving the charge would let a tree of failures spend the import's whole
    allowance without producing anything.

    Returns whether the target is really gone, because a skip note promises that
    nothing was emitted and the caller must not make that promise when the
    partial file is still on disk.
    """
    gone = True
    try:
        os.unlink(target)
    except FileNotFoundError:
        pass
    except OSError:
        gone = False
    budget.refund(charged)
    return gone


def _discover_skill_dirs(root: Path) -> list[Path]:
    """Directories under ``root`` holding a skill entry file, breadth-first.

    Bounded in the WORK it does, not only in what it returns. Three ceilings, and
    each closes a different way for a hostile package to exhaust memory here:

    * ``MAX_DISCOVERY_ENTRIES`` per directory, consumed from the iterator rather
      than materialized first, so one directory holding millions of entries
      cannot be listed whole;
    * ``MAX_DISCOVERY_ENTRIES`` across the whole walk, so breadth spread over many
      directories cannot do the same thing more slowly;
    * ``MAX_SKILLS`` on the result, so the returned list is finite even if the
      walk is cut short by neither of the others.

    A ceiling reached returns what has been found rather than raising: the caller
    converts a package, and a package too wide to enumerate fully is still worth
    importing the skills that were found in the part that was.
    """
    found: list[Path] = []
    frontier: list[tuple[Path, int]] = [(root, 0)]
    examined = 0
    while frontier:
        current, depth = frontier.pop(0)
        if depth > MAX_SKILL_TREE_DEPTH:
            continue
        try:
            entries: list[Path] = []
            for child in current.iterdir():
                entries.append(child)
                if len(entries) >= MAX_DISCOVERY_ENTRIES:
                    break
            entries.sort()
        except OSError:
            continue
        examined += len(entries)
        if (current / SKILL_ENTRY_FILENAME).is_file():
            found.append(current)
            if len(found) >= MAX_SKILLS:
                return found
            continue
        for entry in entries:
            if entry.is_dir() and not _is_link_or_reparse(entry):
                frontier.append((entry, depth + 1))
        if examined >= MAX_DISCOVERY_ENTRIES:
            return found
    return found


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def normalize_app_name(raw: str) -> str:
    """Fold a source package name into the app-name contract, or refuse it."""
    lowered = "".join(ch if ch.isalnum() else "-" for ch in raw.strip().lower())
    collapsed = "-".join(part for part in lowered.split("-") if part)
    if not collapsed or not KEBAB_RE.fullmatch(collapsed):
        raise PluginImportError(
            "invalid_app_name", f"cannot derive a kebab-case app name from {raw!r}"
        )
    reserved = RESERVED_APP_NAMES | RESERVED_ROUTE_APP_NAMES | RESERVED_APP_PATH_SEGMENTS
    if collapsed in reserved:
        raise PluginImportError(
            "reserved_app_name",
            f"app name {collapsed!r} is reserved; pass an explicit name to override",
        )
    return collapsed


def _first_nonempty(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# ---------------------------------------------------------------------------
# Per-kind conversion
# ---------------------------------------------------------------------------


def _convert_skills(root: Path, declared: object, out_dir: Path, report: ImportReport) -> list[str]:
    roots = _declared_path_list(root, declared, "skills")
    if not roots:
        default_root = root / DEFAULT_SKILLS_DIR
        if default_root.is_dir() and not _is_link_or_reparse(default_root):
            roots = [default_root]
    if not roots:
        return []

    emitted: list[str] = []
    seen: set[str] = set()
    # ONE budget for the whole conversion, not one per tree: the cap has to hold
    # across up to MAX_SKILLS trees, which is the breadth a per-tree counter
    # leaves unbounded.
    budget = _ImportBudget()
    for skills_root in roots:
        if not skills_root.is_dir():
            report.warnings.append(f"declared skills root is not a directory: {skills_root}")
            continue
        for skill_dir in _discover_skill_dirs(skills_root):
            if len(emitted) >= MAX_SKILLS:
                report.warnings.append(f"more than {MAX_SKILLS} skills found; the rest are skipped")
                break
            name = skill_dir.name
            if name in seen:
                report.warnings.append(f"duplicate skill directory name, skipped: {skill_dir}")
                continue
            seen.add(name)
            rel = f"{DEFAULT_SKILLS_DIR}/{name}"
            _, skipped = _copy_tree_without_symlinks(skill_dir, out_dir / rel, budget=budget)
            report.warnings.extend(skipped)
            emitted.append(rel)
    if budget.exhausted:
        # Said once, at the top level: the per-file lines above name every file
        # that was dropped, and this is the fact that explains all of them.
        report.warnings.append(
            f"import budget spent after {budget.files} file(s) / {budget.nbytes} "
            f"bytes (limits {MAX_IMPORT_FILES} / {MAX_IMPORT_BYTES}); "
            "the remaining skill files were not copied"
        )
    if emitted:
        report.mapped.append(
            MappedKind("skills", "app.json skills", f"{len(emitted)} skill(s) copied")
        )
    return emitted


def _is_absolute_path(value: str) -> bool:
    """Whether ``value`` is an absolute path under POSIX *or* Windows rules.

    ``Path(...).is_absolute()`` is host-OS specific: on Windows a POSIX-absolute
    ``/opt/app`` has no drive and reads as relative, and on POSIX a Windows-absolute
    ``C:\\app`` reads as relative. A plugin manifest is portable data -- the cwd it
    declares is absolute on the machine that authored it regardless of where the
    import runs -- so classify it as absolute if EITHER convention would, and never
    misread a genuinely-absolute cwd as package-relative on the other OS.
    """
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _package_relative_fields(config: dict[str, Any]) -> list[str]:
    """Fields in a server config that name a path inside the source package.

    The source format resolves a server's ``command``, ``args`` and ``cwd`` against
    the package root. Conversion does not preserve that root, and the program a
    server points at is not a DECLARED resource, so it is not copied either --
    emitting such a server would register one that cannot start. Detection is
    deliberately narrow (``.``, ``..`` and an explicit ``./`` or ``../`` prefix,
    plus any non-absolute ``cwd``) so a bare command name, a flag and a package
    specifier are never mistaken for a path.
    """

    def relative(value: object) -> bool:
        if not isinstance(value, str):
            return False
        text = value.strip()
        return text in (".", "..") or text.startswith("./") or text.startswith("../")

    found: list[str] = []
    if relative(config.get("command")):
        found.append("command")
    args = config.get("args")
    if isinstance(args, list):
        for index, arg in enumerate(args):
            if relative(arg):
                found.append(f"args[{index}]")
    cwd = config.get("cwd")
    if isinstance(cwd, str) and cwd.strip() and not _is_absolute_path(cwd):
        found.append("cwd")
    return found


def _convert_mcp_servers(root: Path, declared: object, report: ImportReport) -> dict[str, Any]:
    if declared is None:
        return {}
    if isinstance(declared, dict):
        servers = declared
    else:
        paths = _declared_path_list(root, declared, "mcpServers")
        if not paths:
            return {}
        if len(paths) > 1:
            report.warnings.append("mcpServers declared several paths; only the first is read")
        source = paths[0]
        if not source.is_file():
            report.warnings.append(f"declared mcpServers file is missing: {source}")
            return {}
        document = _read_json_object(source, f"mcpServers file {source.name}")
        inner = document.get("mcpServers")
        servers = inner if isinstance(inner, dict) else document

    cleaned: dict[str, Any] = {}
    for name, config in servers.items():
        if not isinstance(name, str) or not name.strip():
            report.warnings.append("mcpServers entry with a non-string name was dropped")
            continue
        if not isinstance(config, dict):
            report.warnings.append(f"mcpServers[{name}] is not an object; dropped")
            continue
        relative_fields = _package_relative_fields(config)
        if relative_fields:
            report.unmapped.append(
                UnmappedKind(
                    kind=f"mcpServers[{name}]",
                    bucket="d",
                    reason=(
                        "the server resolves its program against the source package "
                        "root, which conversion does not preserve, and that program is "
                        "not a declared resource so it is not copied"
                    ),
                    detail=f"package-relative: {', '.join(relative_fields)}",
                )
            )
            continue
        cleaned[name] = config
    if cleaned:
        report.mapped.append(
            MappedKind("mcpServers", "app.json mcpServers", f"{len(cleaned)} server(s)")
        )
    return cleaned


def _hook_files(root: Path, declared: object, report: ImportReport) -> list[dict[str, Any]]:
    """Collect hook documents from the path and inline shapes, without running them."""
    if declared is None:
        return []
    candidates = declared if isinstance(declared, list) else [declared]
    documents: list[dict[str, Any]] = []
    for item in candidates:
        if isinstance(item, dict):
            documents.append(item)
            continue
        path = resolve_declared_path(root, item)
        if not path.is_file():
            report.warnings.append(f"declared hooks file is missing: {path}")
            continue
        documents.append(_read_json_object(path, f"hooks file {path.name}"))
    return documents


def _report_hooks(root: Path, declared: object, report: ImportReport) -> None:
    documents = _hook_files(root, declared, report)
    if not documents:
        return
    equivalent: dict[str, int] = {}
    no_counterpart: dict[str, int] = {}
    for document in documents:
        events = document.get("hooks")
        if not isinstance(events, dict):
            # An EMPTY declaration is a real published shape (`"hooks": {}`): the
            # package reserves the kind and declares no event. That is not a
            # malformed document and must not read as one.
            if document:
                report.warnings.append("hooks document has no 'hooks' object; ignored")
            continue
        for event, groups in events.items():
            count = len(groups) if isinstance(groups, list) else 1
            bucket = equivalent if event in AGENT_HOOK_EVENT_EQUIVALENT else no_counterpart
            bucket[str(event)] = bucket.get(str(event), 0) + count

    detail_parts = []
    if equivalent:
        named = ", ".join(
            f"{event}->{AGENT_HOOK_EVENT_EQUIVALENT[event]}" for event in sorted(equivalent)
        )
        detail_parts.append(f"same-meaning agent events: {named}")
    if no_counterpart:
        detail_parts.append(f"no counterpart: {', '.join(sorted(no_counterpart))}")
    if not detail_parts:
        detail_parts.append("declared with no events")
    report.unmapped.append(
        UnmappedKind(
            kind="hooks",
            bucket="d",
            reason=(
                "an installed app cannot declare an agent hook; the only agent hook "
                "surface is operator configuration"
            ),
            detail="; ".join(detail_parts),
        )
    )


def _report_connectors(root: Path, declared: object, report: ImportReport) -> None:
    if declared is None:
        return
    try:
        _declared_path_list(root, declared, "apps")
    except PluginImportError as exc:
        if exc.code == "resource_outside_root":
            raise
        report.warnings.append(f"connector directory declaration ignored: {exc.message}")
    report.unmapped.append(
        UnmappedKind(
            kind="apps",
            bucket="d",
            reason="connector packages have no Kiro Crew counterpart",
        )
    )


def _carried_fields(
    data: dict[str, Any], interface: dict[str, Any], report: ImportReport
) -> dict[str, Any]:
    """Collect source fields with real information and no target field.

    Empty values are not carried: an empty screenshot list says nothing, and
    recording it would make the provenance block read as though something was
    dropped.
    """
    carried: dict[str, Any] = {}
    for key, value in interface.items():
        if key in _CONSUMED_INTERFACE_KEYS:
            continue
        if value or value == 0:
            carried[key] = value
    for key in _CARRIED_MANIFEST_KEYS:
        value = data.get(key)
        if value or value == 0:
            carried[key] = value
    if carried:
        report.unmapped.append(
            UnmappedKind(
                kind="presentation and links",
                bucket="c",
                reason="carried as provenance; no installed-app manifest field renders these",
                detail=", ".join(sorted(carried)),
            )
        )
    return carried


def _source_author(data: dict[str, Any], interface: dict[str, Any]) -> str:
    """The author, from either the object or the string shape.

    Real packages write ``author`` as an object; the format also allows a plain
    string, and the ``interface`` block carries a display spelling.
    """
    raw = data.get("author")
    if isinstance(raw, dict):
        name = _first_nonempty(raw.get("name"))
        if name:
            return name
    elif isinstance(raw, str) and raw.strip():
        return raw.strip()
    return _first_nonempty(interface.get("developerName"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def convert_plugin_package(
    source: Path,
    out_dir: Path,
    *,
    name_override: str | None = None,
) -> ImportReport:
    """Convert the package at ``source`` into a Kiro Crew app at ``out_dir``.

    ``out_dir`` must not already contain a manifest: overwriting one would make a
    second run silently merge two packages into one app.

    A FAILED conversion leaves nothing behind. ``_convert_skills`` copies files
    into ``out_dir`` before the later declarations are validated, so without a
    rollback a package whose skills are fine and whose manifest is then rejected
    leaves a partial app -- which the emptiness guard below refuses to overwrite,
    making the retry fail with ``output_not_empty`` until an operator deletes the
    directory by hand. The partial output is removed here instead, and only when
    this call is what created or populated the directory.
    """
    out_path = Path(out_dir).expanduser()
    if out_path.exists() and not out_path.is_dir():
        # A coded refusal, not a stray NotADirectoryError out of `iterdir()` below:
        # `--out` naming an existing file is an operator mistake, and the caller
        # renders PluginImportError but lets anything else reach the user as a
        # traceback.
        raise PluginImportError(
            "output_not_a_directory",
            f"{out_path} exists and is not a directory; choose an empty output directory",
        )
    created = not out_path.exists()
    # The names present BEFORE this conversion, so the rollback can delete what
    # it added and nothing else. A single "was it empty" flag cannot: it is read
    # once, while another import writing into the same directory adds files
    # throughout the conversion, and clearing the whole directory then destroys
    # that invocation's output as well as this one's.
    preexisting: set[str] = set()
    if not created:
        try:
            preexisting = {c.name for c in out_path.iterdir()}
        except OSError:
            # Unreadable now means the rollback cannot tell whose files are
            # whose, so it must not delete any of them.
            preexisting = _UNKNOWN_PREEXISTING
    try:
        return _convert_plugin_package(source, out_dir, name_override=name_override)
    except BaseException:
        if preexisting is not _UNKNOWN_PREEXISTING:
            _clear_partial_output(out_path, remove_dir=created, keep=preexisting)
        raise


#: Sentinel for "the pre-existing entries could not be listed". Distinct from an
#: empty set, which means the directory was genuinely empty: with an empty set the
#: rollback may remove everything it finds, and with this it must remove nothing.
_UNKNOWN_PREEXISTING: set[str] = set()


def _clear_partial_output(out_dir: Path, *, remove_dir: bool, keep: set[str]) -> None:
    """Remove a failed conversion's partial output, best effort.

    Best effort on purpose: the caller is already unwinding a real failure, and
    an unremovable leftover must not replace that failure's message with this
    one. A directory child is removed with ``rmtree`` unless it is a symlink, in
    which case only the link is unlinked -- following it would delete a tree
    outside the output directory.

    ``keep`` names the entries that were there before this conversion started.
    They belong to whoever put them there -- an operator, or another import
    running against the same output directory -- and are left alone. The
    directory itself is removed only when this invocation created it AND nothing
    survives in it, so a concurrent writer's file also keeps the directory.

    LIMIT, stated because it is not the whole class: an entry another writer adds
    while this conversion is running is not in ``keep`` and is removed with the
    rest. Closing that needs the conversion to stage into a private directory and
    move its output in on success, so the output directory is never cleared at
    all. The documented contract already expects an empty ``out_dir`` (see
    ``convert_plugin_package``), which is why the narrower guarantee is useful on
    its own: it makes an operator's existing files safe, which is the case that
    actually occurs.
    """
    try:
        if not out_dir.exists():
            return
        for child in out_dir.iterdir():
            if child.name in keep:
                continue
            if child.is_dir() and not _is_link_or_reparse(child):
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        if remove_dir and not any(out_dir.iterdir()):
            out_dir.rmdir()
    except OSError:
        return


def _convert_plugin_package(
    source: Path,
    out_dir: Path,
    *,
    name_override: str | None = None,
) -> ImportReport:
    """The conversion itself. Call ``convert_plugin_package``, which rolls back."""
    source = Path(source).expanduser()
    out_dir = Path(out_dir).expanduser()

    # Resolve the source FIRST, then look for the manifest under the resolved
    # root, so ``manifest_path`` and ``root`` share one absolute base. Passing
    # the unresolved ``source`` here would make ``manifest_path`` relative while
    # ``root`` is absolute, and ``manifest_path.relative_to(root)`` below would
    # raise ``ValueError`` on the ordinary relative-path invocation (and on any
    # symlinked component, e.g. macOS ``/var`` -> ``/private/var``).
    root = source.resolve()

    # Reject an output directory that is the source root or sits beneath it:
    # writing the conversion there would fold the destination back into the
    # source tree that the copy step walks, causing unbounded recursion.
    out_resolved = out_dir.resolve()
    if out_resolved == root or root in out_resolved.parents:
        raise PluginImportError(
            "output_within_source",
            f"output directory {out_resolved} is the source root or inside it; "
            "choose an output directory outside the package being converted",
        )

    manifest_path, source_format = find_plugin_manifest(root)
    data = _read_json_object(manifest_path, "plugin manifest")

    interface = data.get("interface")
    interface = interface if isinstance(interface, dict) else {}

    declared_name = _first_nonempty(data.get("name"), root.name)
    app_name = normalize_app_name(name_override or declared_name)

    report = ImportReport(
        source_root=str(root),
        manifest_path=str(manifest_path.relative_to(root)),
        source_format=source_format,
        app_name=app_name,
    )

    # Refuse any non-empty output dir, not just one already holding an app.json:
    # the copy walk below writes files by name, so a pre-existing sibling (a
    # skill dir, a stray file) would be silently clobbered even though no
    # manifest is present. The message already promises an EMPTY directory;
    # enforce that.
    existing = out_dir / "app.json"
    if out_dir.exists() and any(out_dir.iterdir()):
        detail = (
            f"{existing} already exists; choose an empty output directory"
            if existing.exists()
            else f"{out_dir} is not empty; choose an empty output directory"
        )
        raise PluginImportError("output_not_empty", detail)
    out_dir.mkdir(parents=True, exist_ok=True)

    version = _first_nonempty(data.get("version"))
    if not version or not SEMVER_RE.match(version):
        if version:
            report.warnings.append(f"version {version!r} is not semver; emitted 0.0.0")
        else:
            report.warnings.append("package declared no version; emitted 0.0.0")
        version = "0.0.0"

    display_name = _first_nonempty(interface.get("displayName"), data.get("name"), app_name)
    description = _first_nonempty(
        data.get("description"),
        interface.get("shortDescription"),
        interface.get("longDescription"),
    )
    if not description:
        description = f"Imported plugin package {app_name}"
        report.warnings.append("package declared no description; emitted a placeholder")

    skills = _convert_skills(root, data.get("skills"), out_dir, report)
    mcp_servers = _convert_mcp_servers(root, data.get("mcpServers"), report)
    _report_hooks(root, data.get("hooks"), report)
    _report_connectors(root, data.get("apps"), report)
    carried = _carried_fields(data, interface, report)

    keywords = [k.strip() for k in data.get("keywords", []) if isinstance(k, str) and k.strip()]
    author = _source_author(data, interface)
    license_name = _first_nonempty(data.get("license"))

    unknown = sorted(set(data) - _MANIFEST_KNOWN_KEYS)
    if unknown:
        report.warnings.append(f"manifest keys not read by this converter: {', '.join(unknown)}")

    emitted: dict[str, Any] = {
        "name": app_name,
        "version": version,
        "displayName": display_name,
        "description": description,
    }
    if author:
        emitted["author"] = author
    if license_name:
        emitted["license"] = license_name
    if keywords:
        emitted["tags"] = keywords
    if skills:
        emitted["skills"] = skills
    if mcp_servers:
        emitted["mcpServers"] = mcp_servers

    provenance = report.to_dict()
    if carried:
        provenance["carried"] = carried
    emitted["importedPlugin"] = provenance

    errors = AppManifest.from_dict(emitted).validate(out_dir)
    if errors:
        raise PluginImportError(
            "emitted_manifest_invalid",
            "the converted manifest did not validate: " + "; ".join(errors),
        )

    existing.write_text(json.dumps(emitted, indent=2) + "\n", encoding="utf-8")
    return report
