"""Convert a manifest-declared plugin package into an installable Kiro Crew app.

The input is a directory whose manifest declares its resources as root-relative
paths: skills, MCP server configuration, connector directories and hook files.
The mapping from each declared kind to a Kiro Crew extension point -- and the
reason for every kind that has no mapping -- is
``docs/system-specs/modules/harness-plugin-mapping.md``. The converter's own
contract is ``docs/system-specs/modules/plugin-import.md``.

Two properties are load-bearing and are what the tests pin:

**No foreign code runs during conversion.** Conversion reads JSON and copies
files. Nothing in the source package is imported, executed, or spawned while it
runs. What the emitted app later does is the ordinary app contract's business: a
converted MCP server entry names a command, and registering that app launches it
like any other.

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

import collections
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from fnmatch import fnmatch
from itertools import islice
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from kiro_crew.apps.manifest import (
    KEBAB_RE,
    RESERVED_APP_NAMES,
    RESERVED_APP_PATH_SEGMENTS,
    RESERVED_ROUTE_APP_NAMES,
    SEMVER_RE,
    AppManifest,
)
from kiro_crew.platform_compat import first_linked_ancestor, is_link_or_junction
from kiro_crew.terminal_safe import safe_terminal_line

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
MAX_RESOURCE_BYTES = 32 * 1024 * 1024

#: What ONE skill's tree may contribute. The per-file bound above answers "is this
#: file too big"; these answer "is this tree too much", which is a different
#: question and was unbounded: depth caps how far the walk descends, not how wide,
#: so a directory of a hundred thousand small files, or of files each just under
#: the per-file cap, copied without limit. A budget is shared across the whole
#: recursion rather than applied per directory, because a per-directory cap is not
#: a cap on the tree.
MAX_SKILL_FILES = 2000

#: How many entries one directory listing will CONSUME. `sorted()` has to exhaust
#: its iterator before it can order anything, so every cap applied to the result
#: runs after the whole directory is already in memory -- a package that ships a
#: directory of a million names is materialised in full by the sort, whatever the
#: later limit says. Bounded at the iterator instead, which is the only place a
#: listing can be bounded at all.
MAX_DIR_ENTRIES = 5000
MAX_SKILL_TREE_BYTES = 128 * 1024 * 1024
#: Upper bound on the DIRECTORIES the skill search may hold queued at once. The
#: search already stops at :data:`MAX_SKILLS`, but that counter advances only when
#: a directory carries a skill entry file, so a wide tree with no markers anywhere
#: never advances it and the frontier grows by one entry per subdirectory found.
#: Depth alone does not bound it either: each level may contribute
#: :data:`MAX_DIR_ENTRIES` directories, so the population multiplies per level.
#: The frontier is therefore bounded directly.
MAX_SKILL_TREE_DIRS = 20000
# Upper bound on the retained skip-description list: a pathologically wide tree
# would otherwise accumulate one string per skipped entry with no ceiling. Past
# the cap the list stops growing and records that it was truncated.
MAX_SKIP_DESCRIPTIONS = 1000
# Bounds on what a FOREIGN manifest can make this converter retain. Everything
# below comes from a package the operator did not write, and `mcpServers` entries
# are copied into the emitted app.json rather than merely inspected, so a large
# or deeply-nested manifest would otherwise grow the output without limit. Each
# over-limit field is dropped and reported, like every other entry this module
# declines, rather than refusing the whole conversion.
MAX_MCP_SERVERS = 100
MAX_MANIFEST_KEY_CHARS = 200
MAX_MANIFEST_STRING_CHARS = 4096
MAX_MANIFEST_CONTAINER_ITEMS = 200
MAX_MANIFEST_DEPTH = 8
#: How many `hooks` entries a manifest may declare. Each one is opened, parsed and
#: retained whole, and nothing rejects a repeat, so the per-file byte budget bounds
#: one read while the total stays open. Generous next to any real manifest, which
#: declares one hooks file or a handful.
MAX_HOOK_DOCUMENTS = 50

#: How large a manifest may be before it is refused UNREAD. The bounds above all
#: apply to values already parsed, which cannot help with the allocation the read
#: itself makes: a manifest is read whole in one call, so this is the only bound
#: that can run before the memory is spent. Generous next to any real manifest --
#: the emitted fields are capped far below it -- because its job is to refuse a
#: pathological input, not to police a large one.
MAX_MANIFEST_BYTES = 4 * 1024 * 1024

#: Suffixes that make a separator-less token a FILE rather than a command looked
#: up on PATH. Deliberately the program shapes an MCP server is declared with, not
#: every suffix in existence: the set decides whether a server is dropped, so a
#: value that is merely dotted (``python3.11``) must not read as a path.
_PROGRAM_SUFFIXES = frozenset(
    {
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".py",
        ".rb",
        ".php",
        ".pl",
        ".lua",
        ".sh",
        ".bash",
        ".zsh",
        ".jar",
        ".exe",
        ".bat",
        ".cmd",
        ".ps1",
        ".com",
    }
)

#: Characters in a derived app name. KEBAB_RE constrains the alphabet and the
#: hyphen placement but not the length, and this name becomes a directory segment
#: as well as a retained manifest field.
MAX_APP_NAME_CHARS = 120

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
        """Render the report for a terminal.

        Every field below is derived from the FOREIGN manifest -- names, targets and
        the details and reasons quoting it -- and each is printed on its own
        indented line. Untouched, a field carrying escapes or a newline could move
        the cursor, repaint, or open a line that reads as this tool's own output, so
        each goes through the repo's shared one-line sanitizer. That helper exists
        for exactly this shape and its docstring says so, which is why nothing here
        strips controls by hand.
        """
        t = safe_terminal_line
        lines = [
            f"source:   {t(str(self.source_root))}",
            f"manifest: {t(str(self.manifest_path))} ({t(self.source_format)})",
            f"app:      {t(self.app_name)}",
            "",
            "mapped:",
        ]
        if self.mapped:
            for m in self.mapped:
                suffix = f" -- {t(m.detail)}" if m.detail else ""
                lines.append(f"  {t(m.kind)} -> {t(m.target)}{suffix}")
        else:
            lines.append("  (nothing)")
        lines.append("")
        lines.append("not mapped:")
        if self.unmapped:
            for u in self.unmapped:
                suffix = f" -- {t(u.detail)}" if u.detail else ""
                lines.append(f"  {t(u.kind)} [{t(u.bucket)}] {t(u.reason)}{suffix}")
        else:
            lines.append("  (nothing)")
        if self.warnings:
            lines.append("")
            lines.append("warnings:")
            for w in self.warnings:
                lines.append(f"  {t(w)}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Manifest discovery
# ---------------------------------------------------------------------------


def _read_json_object(path: Path, what: str) -> dict[str, Any]:
    # Size checked BEFORE the read, not after. Every other bound in this module
    # applies to a value already in memory, which is the right place for a bound on
    # what is RETAINED -- but a manifest is third-party input read whole in one
    # call, so a bound that runs after the read has already let the allocation
    # happen. Asked of the filesystem rather than measured from the string for the
    # same reason.
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PluginImportError("manifest_unreadable", f"cannot read {what}: {exc}") from exc
    if size > MAX_MANIFEST_BYTES:
        raise PluginImportError(
            "manifest_unreadable",
            f"{what} is larger than {MAX_MANIFEST_BYTES} bytes ({size}), so it is not read",
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PluginImportError("manifest_unreadable", f"cannot read {what}: {exc}") from exc
    except UnicodeDecodeError as exc:
        # Not an OSError: the read succeeds and the DECODE fails, so this escaped
        # the coded boundary as a traceback. A manifest that is not UTF-8 is
        # unreadable in the only sense this converter cares about.
        raise PluginImportError("manifest_unreadable", f"{what} is not valid UTF-8: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        # ValueError, not JSONDecodeError. JSONDecodeError SUBCLASSES ValueError, so
        # this arm still answers a syntax error the same way, and it also catches the
        # refusals the parser raises that are not syntax errors at all: an integer
        # past the interpreter's digit limit raises a plain ValueError from valid
        # JSON, which escaped the narrower arm as a traceback. Keying on the parser's
        # own error base rather than on one of its subclasses is what stops the next
        # such refusal escaping too.
        raise PluginImportError("manifest_not_json", f"{what} is not valid JSON: {exc}") from exc
    except RecursionError as exc:
        # json.loads RECURSES, so deeply nested input raises past JSONDecodeError.
        # Refused rather than parsed with a raised limit: the emitted manifest has
        # its own depth bound, so input this deep cannot be converted anyway.
        raise PluginImportError("manifest_not_json", f"{what} nests too deeply to parse") from exc
    if not isinstance(data, dict):
        raise PluginImportError("manifest_not_object", f"{what} must be a JSON object")
    return data


def _manifest_list(data: dict[str, Any], field: str) -> list[Any]:
    """The manifest's *field* as a list, refusing any other JSON type.

    An absent field reads as empty, which is what every caller wants. A present
    one of the wrong type is a REFUSAL rather than a coercion: iterating it
    raises ``TypeError`` for a number and silently iterates characters or keys for
    a string or an object, so the caller would either abort with a traceback or
    convert something the manifest never said.
    """
    value = data.get(field)
    if value is None:
        return []
    if not isinstance(value, list):
        raise PluginImportError(
            "invalid_manifest_field",
            f"the manifest's {field!r} must be a list, not {type(value).__name__}",
        )
    return value


def read_manifest_name(path: Path) -> str | None:
    """The declared app name in the manifest at *path*, or None when unusable.

    The manifest is foreign input, so its shape and its field types are both
    checked here rather than by each caller: a document that is valid JSON but
    not an object refuses with ``manifest_not_object``, and a ``name`` of any
    other type reads as absent, which callers already have a fallback for.

    Exists so a caller needing only the name does not re-implement the parse and
    lose those checks -- the CLI did, and a non-object manifest reached ``.get``
    and aborted with a traceback.
    """
    data = _read_json_object(path, "the plugin manifest")
    name = data.get("name")
    return name if isinstance(name, str) else None


def _bounded_sorted_entries(path: Path) -> tuple[list[Path], bool]:
    """Up to :data:`MAX_DIR_ENTRIES` of *path*'s children, sorted, and whether that
    was all of them.

    Raises :class:`OSError` like ``iterdir`` does, so each caller keeps the arm it
    already had for an unreadable directory.
    """
    it = path.iterdir()
    taken = list(islice(it, MAX_DIR_ENTRIES))
    # One more step decides whether anything was left, without consuming the rest.
    complete = next(it, None) is None
    return sorted(taken), complete


def _is_schema_qualified(path: Path) -> bool:
    """True when a root manifest declares the published schema namespace."""
    if is_link_or_junction(path) or not path.is_file():
        return False
    try:
        # The SIZE bound is part of matching the sibling, not a separate
        # precaution. This probe runs on a root `plugin.json` on every import,
        # including one that turns out not to be a plugin at all, so an unbounded
        # read here is reached by more inputs than the sibling's is -- and the
        # bound has to precede the read for the same reason it does there: the file
        # is taken whole in one call, so a check afterwards has already paid for it.
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        # ValueError, not JSONDecodeError, for the reason _read_json_object gives:
        # JSONDecodeError subclasses it, and the parser also raises a PLAIN
        # ValueError from valid JSON when an integer is past the interpreter's digit
        # limit. This probe is named in that function's own comment as carrying the
        # same escapes, so the two arms have to be the same width or the narrower one
        # is where the traceback comes out. The probe answers a BOOLEAN rather than
        # raising, so a file it cannot read is simply not qualified.
        return False
    if not isinstance(data, dict):
        return False
    schema = data.get("$schema")
    return isinstance(schema, str) and schema.startswith(SCHEMA_NAMESPACE_PREFIX)


def find_plugin_manifest(root: Path) -> tuple[Path, str]:
    """Locate the package manifest.

    A schema-qualified root manifest wins. Otherwise the first vendor-prefixed
    directory holding one, in sorted order, so a package carrying several
    resolves the same way on every machine.
    """
    # The link check comes FIRST, before anything probes the root. A probe RESOLVES
    # the path, so on Windows a junction whose target is a UNC share makes the OS
    # authenticate to a host the PACKAGE chose, and a refusal that runs afterwards
    # is too late -- the credentials have already been offered. This is the one path
    # ``resolve_declared_path`` cannot cover: it walks each declared segment from
    # the root down and refuses a linked component before resolving it, which is
    # what protects the declared skills roots, the mcpServers file and the hooks
    # files -- but the ROOT is what those are resolved AGAINST, so it has to be
    # checked here. Answered with the same code as a non-directory, because for this
    # function's purpose a link is not a usable source and a new code would have to
    # be registered in the spec for no distinction a caller can act on.
    #
    # ANCESTORS first, then the leaf, which is the order the sibling check in
    # ``member_essential_context`` uses and the order that makes this safe rather
    # than merely thorough. A leaf-only test still resolves through a linked PARENT,
    # and the ``root.is_dir()`` two lines down is the probe that does it: an ancestor
    # junction whose target is a UNC share turns that innocent-looking call on a
    # local-looking path into an outbound SMB connection authenticating as this
    # process, to a host the PACKAGE chose. ``first_linked_ancestor`` walks
    # root-first and stops at the first hit, so each lstat runs only after every
    # ancestor above it is known not to be a link -- the walk itself never
    # traverses one. The offending ancestor is deliberately left out of the message:
    # which ancestor is a link is filesystem layout, and the caller supplied the
    # path, so naming it adds nothing the caller cannot see.
    if first_linked_ancestor(root) or is_link_or_junction(root):
        raise PluginImportError(
            "source_not_a_directory", f"the source root is a link, not a directory: {root}"
        )
    if not root.is_dir():
        raise PluginImportError("source_not_a_directory", f"not a directory: {root}")

    root_manifest = root / MANIFEST_FILENAME
    if _is_schema_qualified(root_manifest):
        return root_manifest, FORMAT_SCHEMA_QUALIFIED

    # The glob is bounded the same way a listing is: it walks the directory, so an
    # unbounded sort over it materialises the whole thing before any candidate is
    # examined.
    try:
        vendor_dirs, _vendor_complete = _bounded_sorted_entries(root)
    except OSError:
        vendor_dirs = []
    for candidate_dir in [d for d in vendor_dirs if fnmatch(d.name, VENDOR_MANIFEST_DIR_GLOB)]:
        if is_link_or_junction(candidate_dir) or not candidate_dir.is_dir():
            continue
        candidate = candidate_dir / MANIFEST_FILENAME
        if not is_link_or_junction(candidate) and candidate.is_file():
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

    # Refuse a linked component BEFORE anything resolves it. ``.resolve()``
    # traverses a junction on Windows, and a junction whose target is a UNC share
    # makes the OS authenticate to that host as it resolves -- so a containment
    # check that runs afterwards refuses a path whose damage is already done, with
    # the operator's credentials already offered to a host the package chose.
    #
    # Walked one segment at a time from the root down, so each ``is_link_or_junction``
    # call runs with every ancestor already proven unlinked: the lstat it performs
    # cannot itself be lured through a link this loop has not yet checked.
    probe = root_resolved
    for segment in re.split(r"[\\/]", body):
        if not segment or segment == ".":
            continue
        probe = probe / segment
        if is_link_or_junction(probe):
            raise PluginImportError(
                "resource_outside_root",
                f"declared resource {text!r} is reached through a link ({probe}); "
                "the package root is the authority boundary",
            )

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


def _copy_tree_without_links(
    src: Path, dst: Path, depth: int = 0, budget: dict[str, int] | None = None
) -> tuple[int, list[str]]:
    """Copy a directory tree, skipping every link rather than following it.

    Returns ``(files_copied, skipped_descriptions)``. Following a link would let a
    file outside the package root land inside the emitted app, which is the one
    thing ``resolve_declared_path`` exists to prevent -- so the same rule applies
    to the tree walk, not just to the declared path.

    ``budget`` carries what the WHOLE tree may still contribute, in files and in
    bytes, and is created by the top call and shared with every recursion. It is
    shared rather than per-directory because a per-directory cap does not bound a
    tree: depth limits how deep the walk goes, never how wide, so an unbounded
    breadth copied without limit. Exhausting it skips the remainder and records it,
    the same answer this walk gives every entry it declines.

    "Link" means ``platform_compat.is_link_or_junction``, not ``Path.is_symlink``:
    a Windows directory junction is not a symlink by that test, so an
    ``is_symlink`` check reads a junction as an ordinary directory and copies
    whatever it points at -- a junction to the user's ``.ssh`` puts private keys
    in the emitted app. Every link check in this module asks the same helper for
    that reason.

    ``depth`` bounds the recursion the same way ``_discover_skill_dirs`` bounds
    its walk: an unbounded recurse on a pathologically deep tree raises
    ``RecursionError`` mid-copy and leaves a partial import, so a subtree past
    the limit is skipped (and recorded) rather than descended.
    """
    copied = 0
    skipped: list[str] = []
    if budget is None:
        budget = {
            "files": MAX_SKILL_FILES,
            "bytes": MAX_SKILL_TREE_BYTES,
            "skips": MAX_SKIP_DESCRIPTIONS,
        }
    budget.setdefault("skips", MAX_SKIP_DESCRIPTIONS)

    def note(description: str) -> None:
        """Record one declined entry, against the tree's shared skip allowance.

        The allowance is spent HERE rather than by trimming the finished list,
        because a cap that runs after the list is built is not a bound on the
        list: every description is a formatted string carrying a full path, and
        a tree that is wide as well as deep produces one per declined entry, so
        the peak the cap exists to prevent has already been reached by the time
        the trim can see it. This walk is reachable from a third-party package,
        which chooses that width. Same reason the entry listing takes at most
        ``MAX_DIR_ENTRIES`` rather than sorting the whole directory first.

        The overflow is COUNTED past the allowance, not silently dropped, so the
        caller can still say how many entries it declined to describe.
        """
        if budget["skips"] > 0:
            budget["skips"] -= 1
            skipped.append(description)
        else:
            budget["skip_overflow"] = budget.get("skip_overflow", 0) + 1

    if depth > MAX_SKILL_TREE_DEPTH:
        note(f"tree deeper than {MAX_SKILL_TREE_DEPTH} levels, skipped: {src}")
        return copied, skipped
    # The two calls that were unguarded, and they are NOT the same failure. A
    # destination this walk cannot create means the OUTPUT filesystem is unusable,
    # which no amount of skipping recovers, so it is refused with the code this
    # module already uses for an unwritable staging area. A source directory it
    # cannot read is one unreadable subtree: skipped and recorded, which is the
    # answer this walk gives every entry it declines, and the answer
    # ``_discover_skill_dirs`` already gives for the same call.
    try:
        dst.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PluginImportError(
            "staging_unwritable", f"cannot create {dst} while copying a skill: {exc}"
        ) from exc
    try:
        entries, entries_complete = _bounded_sorted_entries(src)
    except OSError as exc:
        note(f"unreadable, skipped: {src} ({exc.strerror or exc})")
        return copied, skipped
    if not entries_complete:
        note(f"listing truncated at {MAX_DIR_ENTRIES} entries: {src}")
    for entry in entries:
        target = dst / entry.name
        if is_link_or_junction(entry):
            note(f"link skipped: {entry}")
            continue
        if entry.is_dir():
            sub_copied, sub_skipped = _copy_tree_without_links(entry, target, depth + 1, budget)
            copied += sub_copied
            skipped.extend(sub_skipped)
            continue
        if not entry.is_file():
            note(f"not a regular file, skipped: {entry}")
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            note(f"unreadable, skipped: {entry}")
            continue
        if size > MAX_RESOURCE_BYTES:
            note(f"over {MAX_RESOURCE_BYTES} bytes, skipped: {entry}")
            continue
        # Charged BEFORE the copy, so an exhausted budget writes nothing further.
        if budget["files"] <= 0:
            note(f"over {MAX_SKILL_FILES} files in this skill, skipped: {entry}")
            continue
        if size > budget["bytes"]:
            note(f"over {MAX_SKILL_TREE_BYTES} bytes in this skill, skipped: {entry}")
            continue
        try:
            shutil.copy2(entry, target)
        except OSError as exc:
            # The stat() above can succeed and the copy still fail: the file may be
            # locked, unreadable, or gone by now. The CLI catches PluginImportError
            # and nothing else, so an OSError here reaches the operator as a
            # traceback. Skipped and reported instead, like every other entry this
            # walk declines to copy.
            note(f"unreadable, skipped: {entry} ({exc.strerror or exc})")
            continue
        # Decremented only on a copy that LANDED, so a refused or failed entry does
        # not spend the tree's allowance.
        budget["files"] -= 1
        budget["bytes"] -= size
        copied += 1
    # Reported by the call that OWNS the allowance -- the top of the walk, the one
    # that created the budget -- because the counter is shared: a recursion adding
    # its own summary line would emit one per directory, each naming the same
    # running total.
    if depth == 0 and budget.get("skip_overflow"):
        skipped.append(
            f"... and {budget['skip_overflow']} more skipped "
            f"(descriptions capped at {MAX_SKIP_DESCRIPTIONS})"
        )
    return copied, skipped


def _discover_skill_dirs(root: Path) -> list[Path]:
    """Directories under ``root`` holding a skill entry file, breadth-first.

    Stops at :data:`MAX_SKILLS`. The cap belongs HERE and not only at the caller
    that emits them: a package can be wide as well as deep, and a walk that
    discovers the whole population before anyone counts it has already spent the
    memory the cap exists to bound. Depth alone does not help, for the same reason
    it does not bound the copy walk.

    :data:`MAX_SKILLS` bounds the ANSWER, not the search. It advances only when a
    directory carries an entry file, so a tree with no marker anywhere never
    advances it while every subdirectory still joins the frontier -- the same
    retained-versus-inspected distinction the manifest converter is written to.
    The frontier therefore carries its own bound, and the depth limit is applied
    where a directory is ENQUEUED rather than where it is popped, so an over-deep
    subtree is never held in memory to be discarded later.
    """
    found: list[Path] = []
    frontier: collections.deque[tuple[Path, int]] = collections.deque([(root, 0)])
    enqueued = 1
    while frontier:
        if len(found) >= MAX_SKILLS:
            break
        # A deque, popped from the LEFT. A list popped at index 0 is O(n) per step,
        # so a wide tree turned a breadth-first walk quadratic in its own frontier.
        current, depth = frontier.popleft()
        try:
            entries, entries_complete = _bounded_sorted_entries(current)
        except OSError:
            continue
        # Not reported here: this function answers with directories alone, and a
        # listing wide enough to truncate is already far past MAX_SKILLS, which the
        # caller does report. Adding a channel for it would change this signature to
        # carry a message the operator already gets.
        _ = entries_complete
        # Same link-check-first rule the directory walk below applies, and for the
        # same reason: is_file() RESOLVES the path, so a SKILL.md symlinked at a UNC
        # target makes Windows authenticate to a host the PACKAGE chose. The
        # directory case was already guarded; the entry file is the other probe in
        # this loop and needs it too, or the guard covers one of two doors.
        marker = current / SKILL_ENTRY_FILENAME
        if not is_link_or_junction(marker) and marker.is_file():
            found.append(current)
            continue
        if depth >= MAX_SKILL_TREE_DEPTH:
            continue
        for entry in entries:
            if enqueued >= MAX_SKILL_TREE_DIRS:
                return found
            # Link check FIRST. On Windows a probe resolves the path, so asking
            # is_dir() of a junction whose target is a UNC share makes the OS
            # authenticate to a host the PACKAGE chose before the refusal that
            # follows can run.
            if not is_link_or_junction(entry) and entry.is_dir():
                frontier.append((entry, depth + 1))
                enqueued += 1
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
    # KEBAB_RE bounds the SHAPE, not the length: a 100k-character kebab string
    # matches it. The name is retained in app.json, is a directory segment, and is
    # an IDENTITY, so it is REFUSED rather than shortened -- a truncated name
    # silently designates a different app, and could collide with a real one.
    if len(collapsed) > MAX_APP_NAME_CHARS:
        raise PluginImportError(
            "invalid_app_name",
            f"derived app name is {len(collapsed)} characters, over the "
            f"{MAX_APP_NAME_CHARS} character limit",
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
        if not is_link_or_junction(default_root) and default_root.is_dir():
            roots = [default_root]
    if not roots:
        return []

    emitted: list[str] = []
    seen: set[str] = set()
    for skills_root in roots:
        if not skills_root.is_dir():
            report.warnings.append(f"declared skills root is not a directory: {skills_root}")
            continue
        for skill_dir in _discover_skill_dirs(skills_root):
            if len(emitted) >= MAX_SKILLS:
                report.warnings.append(f"more than {MAX_SKILLS} skills found; the rest are skipped")
                break
            name = skill_dir.name
            if name.casefold() in seen:
                report.warnings.append(f"duplicate skill directory name, skipped: {skill_dir}")
                continue
            # Case-FOLDED, because the destination decides what counts as the same
            # name: macOS and Windows alias `Search` and `search` to one directory,
            # so a case-sensitive source shipping both would have the second copy
            # overwrite the first while this check saw two distinct skills. Folding
            # here refuses the pair on every host, so the emitted app does not
            # depend on which filesystem the conversion happened to run on.
            seen.add(name.casefold())
            rel = f"{DEFAULT_SKILLS_DIR}/{name}"
            _, skipped = _copy_tree_without_links(skill_dir, out_dir / rel)
            report.warnings.extend(skipped)
            # The copy walk declines an entry rather than raising, so a skill whose
            # SKILL.md was locked, unreadable or removed mid-copy leaves a warning
            # among possibly hundreds and an output directory with no entry file.
            # Declaring it anyway ships an app.json naming a skill the loader cannot
            # read, and counts it in "N skill(s) copied" -- a false report of the one
            # thing the operator checks. Verified with the SAME predicate
            # _discover_skill_dirs applies when it calls a directory a skill, so the
            # input and output sides cannot disagree about what a skill is.
            if not (out_dir / rel / SKILL_ENTRY_FILENAME).is_file():
                report.warnings.append(
                    f"skill not emitted, {SKILL_ENTRY_FILENAME} did not copy: {skill_dir}"
                )
                continue
            emitted.append(rel)
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
    emitting such a server would register one that cannot start. A value counts
    as a path when it carries a separator and is not absolute, plus ``.`` and
    ``..`` which carry none, plus any non-absolute ``cwd``. A bare command name,
    a flag, an npm scope specifier and a URL are excluded, so none of them is
    mistaken for a path.
    """

    def relative(value: object) -> bool:
        if not isinstance(value, str):
            return False
        text = value.strip()
        if not text:
            return False
        # A SEPARATOR is what makes a value a path, not a leading dot. Keying on
        # "./" and ".\\" left the ordinary spelling "bin/server" unflagged, and a
        # relative program resolves against the SESSION's working directory
        # rather than the package -- so it launches whatever sits at that path in
        # the user's workspace. It also cannot start after conversion, because
        # the package root is not preserved and the program is not a declared
        # resource, so dropping it is the right answer on both counts.
        #
        # The exclusions are the three shapes that carry a slash without naming a
        # filesystem path, so widening the rule does not start dropping servers
        # that convert correctly:
        if text.startswith("-"):
            # A bare flag names no path, but a flag that CARRIES one does: the
            # value of ``--config=./local/thing`` resolves against the session's
            # working directory exactly as the bare spelling would, so excluding
            # every token that starts with a dash let the package-relative shape
            # back in through its own option syntax. The value after the first
            # ``=`` is re-tested by the same rule, which keeps the three
            # non-path shapes excluded there too (``--pkg=@scope/x``,
            # ``--src=git+https://...``). A flag with no ``=`` carries no value
            # to test and stays what it looks like.
            flag_value = text.split("=", 1)[1] if "=" in text else ""
            return bool(flag_value) and relative(flag_value)
        if text.startswith("@"):
            return False  # an npm scope specifier, e.g. @scope/pkg
        if "://" in text:
            return False  # a URL, e.g. a git+https package source
        if text in (".", ".."):
            return True  # a directory with no separator at all
        if _is_absolute_path(text):
            return False  # absolute resolves against nothing the session owns
        if "/" in text or "\\" in text:
            return True
        # A bare token with no separator still names a file when it carries a
        # script suffix, and that is the ordinary way an MCP server is declared:
        # ``node server.js``. The two positions resolve differently, which is why
        # the suffix and not the position is the test here. A bare COMMAND is
        # looked up on PATH, because the exec family only consults PATH for a
        # string with no slash, so ``node`` is safe. A bare ARG is resolved by the
        # program that receives it, and a script runner resolves it against the
        # SESSION's working directory -- so ``server.js`` launches whatever sits
        # at that name in the user's workspace. Flagging it in either position
        # costs at most a server whose program is a script literally on PATH,
        # which is dropped with its reason recorded; missing it runs foreign code.
        return PurePosixPath(text).suffix.lower() in _PROGRAM_SUFFIXES

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


def _has_transport(config: dict[str, Any]) -> bool:
    """Whether the entry says how to REACH the server at all.

    The two spellings an MCP server is declared with: a program to run
    (``command``) or an endpoint to connect to (``url``). An entry carrying
    neither is transportless -- there is no server behind the name -- and this is
    asked separately from whether the transport is USABLE, which
    :func:`_package_relative_fields` answers for the command case.
    """
    command = config.get("command")
    if isinstance(command, str) and command.strip():
        return True
    url = config.get("url")
    return isinstance(url, str) and bool(url.strip())


def _bounded_manifest_value(
    value: object, path: str, report: ImportReport, depth: int = 0
) -> tuple[object, bool]:
    """A copy of *value* with every retained string and container bounded.

    Returns ``(bounded, kept)``. ``kept`` is False when the value cannot be
    retained at all -- past the depth limit, or a non-finite float -- because at
    that point there is no shortened form that still means anything.

    Why a copy rather than a size check on the original: this converter WRITES what
    it retains into the emitted app.json, so the bound has to constrain the thing
    that gets written, not merely reject an input. Over-limit pieces are dropped
    and reported, matching how the rest of this module declines an entry, so one
    oversized field costs that field instead of the whole import.
    """
    if depth > MAX_MANIFEST_DEPTH:
        report.warnings.append(f"{path} nested deeper than {MAX_MANIFEST_DEPTH} levels; dropped")
        return None, False
    if isinstance(value, float) and not math.isfinite(value):
        # json.loads ACCEPTS the bare words NaN, Infinity and -Infinity, and
        # json.dumps re-emits them, but RFC 8259 defines none of them -- so a
        # manifest carrying one produces an app.json that a strict reader refuses
        # to parse, which costs the whole file rather than the one field. Unlike an
        # over-long string there is no shorter form to retain: the value has no
        # JSON spelling at all, so it is dropped the way an over-deep one is.
        # ``bool`` is not a ``float`` instance, so True/False do not reach here.
        report.warnings.append(f"{path} is not a finite number; dropped")
        return None, False
    if isinstance(value, str):
        if len(value) > MAX_MANIFEST_STRING_CHARS:
            report.warnings.append(
                f"{path} is longer than {MAX_MANIFEST_STRING_CHARS} characters; truncated"
            )
            return value[:MAX_MANIFEST_STRING_CHARS], True
        return value, True
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        inspected = 0
        for key, item in value.items():
            # Counted per ENTRY INSPECTED, before the validity checks, exactly as
            # ``_convert_mcp_servers`` counts its own: every branch below that
            # DROPS an entry still appends a warning, so a cap on ``out`` bounds
            # nothing those branches produce. A container of nothing but dropped
            # entries leaves ``out`` empty forever and grows ``report.warnings``
            # once per input entry.
            inspected += 1
            if inspected > MAX_MANIFEST_CONTAINER_ITEMS:
                report.warnings.append(
                    f"{path} has more than {MAX_MANIFEST_CONTAINER_ITEMS} entries; "
                    "the rest are dropped"
                )
                break
            if not isinstance(key, str):
                report.warnings.append(f"{path} has a non-string key; dropped")
                continue
            if len(key) > MAX_MANIFEST_KEY_CHARS:
                report.warnings.append(
                    f"{path}.{key[:40]}... key is longer than "
                    f"{MAX_MANIFEST_KEY_CHARS} characters; dropped"
                )
                continue
            bounded, kept = _bounded_manifest_value(item, f"{path}.{key}", report, depth + 1)
            if kept:
                out[key] = bounded
        return out, True
    if isinstance(value, list):
        items: list[Any] = []
        inspected = 0
        for index, item in enumerate(value):
            # Counted per ITEM INSPECTED, for the same reason as the dict branch:
            # ``kept`` is False for a non-finite number and for an over-deep value,
            # each of which appends a warning, so ``len(items)`` does not advance
            # on precisely the inputs that grow the report.
            inspected += 1
            if inspected > MAX_MANIFEST_CONTAINER_ITEMS:
                report.warnings.append(
                    f"{path} has more than {MAX_MANIFEST_CONTAINER_ITEMS} items; "
                    "the rest are dropped"
                )
                break
            bounded, kept = _bounded_manifest_value(item, f"{path}[{index}]", report, depth + 1)
            if kept:
                items.append(bounded)
        return items, True
    # bool/int/None and finite floats are fixed-size, so they carry no growth to
    # bound. A non-finite float is a different refusal and is handled above.
    return value, True


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
    inspected = 0
    for name, config in servers.items():
        # Counted per ENTRY INSPECTED, before the validity checks, not per entry
        # emitted. Every branch below retains something for the operator to read --
        # a warning, or an unmapped record carrying the server's name and reason --
        # so a cap on ``cleaned`` alone bounds nothing a refused entry produces: a
        # manifest of nothing but package-relative servers leaves ``cleaned`` empty
        # forever and grows the report without limit. The bound has to sit on what
        # is read, because that is what decides how much is retained.
        inspected += 1
        if inspected > MAX_MCP_SERVERS:
            report.warnings.append(
                f"more than {MAX_MCP_SERVERS} mcpServers declared; the rest are dropped"
            )
            break
        if not isinstance(name, str) or not name.strip():
            report.warnings.append("mcpServers entry with a non-string name was dropped")
            continue
        if not isinstance(config, dict):
            report.warnings.append(f"mcpServers[{name}] is not an object; dropped")
            continue
        if len(name) > MAX_MANIFEST_KEY_CHARS:
            report.warnings.append(
                f"mcpServers name longer than {MAX_MANIFEST_KEY_CHARS} characters; dropped"
            )
            continue
        # An entry that names no way to REACH the server is not a server. Nothing
        # above catches it: an empty or transportless object is a dict, carries no
        # package-relative field to refuse, and the bounding pass keeps it because
        # there is nothing over-limit in it -- so it was written into app.json as a
        # server with no command and no url, which no reader can launch and no
        # message says anything about. A manifest author producing one is an
        # ordinary mistake, so it is declined the way every other invalid entry
        # here is declined: the entry is dropped and named, and the import goes on.
        if not _has_transport(config):
            report.warnings.append(
                f"mcpServers[{name}] declares neither a command nor a url; dropped"
            )
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
        bounded, kept = _bounded_manifest_value(config, f"mcpServers[{name}]", report)
        if kept:
            cleaned[name] = bounded
    if cleaned:
        report.mapped.append(
            MappedKind("mcpServers", "app.json mcpServers", f"{len(cleaned)} server(s)")
        )
    return cleaned


def _hook_files(root: Path, declared: object, report: ImportReport) -> list[dict[str, Any]]:
    """Collect hook documents from the path and inline shapes, without running them.

    Bounded per ENTRY INSPECTED. Nothing here refuses a repeat, so a manifest may
    declare one file any number of times and each declaration is opened, parsed
    and retained again -- the per-file byte budget bounds one read, never the
    total. A cap on the retained list would not help either: a missing file
    appends a warning and retains no document, so a manifest of nothing but
    missing paths grows the report while the list stays empty.
    """
    if declared is None:
        return []
    candidates = declared if isinstance(declared, list) else [declared]
    documents: list[dict[str, Any]] = []
    inspected = 0
    for item in candidates:
        inspected += 1
        if inspected > MAX_HOOK_DOCUMENTS:
            report.warnings.append(
                f"more than {MAX_HOOK_DOCUMENTS} hooks entries declared; the rest are dropped"
            )
            break
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
    # Every value below comes from a foreign manifest and is WRITTEN into the
    # emitted app.json, which is read at every runtime load, so it carries the same
    # growth as the mcpServers path and goes through the same bound. Bounding only
    # the mcpServers side left the invariant half-kept: a large interface blob or a
    # deep link structure grows the output exactly as a large server config would.
    carried: dict[str, Any] = {}
    for key, value in interface.items():
        if key in _CONSUMED_INTERFACE_KEYS:
            continue
        if value or value == 0:
            bounded, kept = _bounded_manifest_value(value, f"interface.{key}", report)
            if kept:
                carried[key] = bounded
    for key in _CARRIED_MANIFEST_KEYS:
        value = data.get(key)
        if value or value == 0:
            bounded, kept = _bounded_manifest_value(value, key, report)
            if kept:
                carried[key] = bounded
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
    """
    source = Path(source).expanduser()
    out_dir = Path(out_dir).expanduser()

    # Refuse a LINKED output path before anything resolves it. Two reasons, and the
    # ordering serves both. First, the publish step replaces out_dir by rename, so a
    # link reaching it is destroyed rather than followed: the checks below admit a
    # dangling symlink and an empty-target junction, and os.replace then removes the
    # pointer. Second, the check must run BEFORE .resolve(): on Windows resolving a
    # junction traverses it, so a junction whose target is a UNC share makes the OS
    # authenticate to a host that the path named but the operator did not choose to
    # contact. This is the same rule resolve_declared_path applies on the source
    # side, applied here to the destination.
    if is_link_or_junction(out_dir):
        raise PluginImportError(
            "output_is_a_link",
            f"{out_dir} is a symlink or junction; publishing replaces the output path "
            "by rename, which would destroy the link, so choose a real directory",
        )

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
    # the copy walk below overwrites files by name via shutil.copy2, so a
    # pre-existing sibling (a skill dir, a stray file) would be silently
    # clobbered even though no manifest is present. The message already promises
    # an EMPTY directory; enforce that.
    existing = out_dir / "app.json"
    if out_dir.exists() and not out_dir.is_dir():
        # Asked before the emptiness check below, which calls iterdir(): on a
        # regular file that raises NotADirectoryError, an OSError the CLI does not
        # catch, so the command aborts with a traceback instead of this sentence.
        raise PluginImportError(
            "output_not_a_directory",
            f"{out_dir} exists and is not a directory; choose an empty output directory",
        )
    if out_dir.exists():
        # ``iterdir()`` faults on more than a non-directory: a directory the
        # process cannot read raises PermissionError, and a path whose parent
        # went away mid-run raises OSError. The CLI catches PluginImportError and
        # nothing else, so any of those reaches the operator as a traceback
        # instead of the sentence this function exists to produce. An unreadable
        # directory is refused rather than treated as empty, because "empty" is
        # the one conclusion that would let the copy walk below overwrite files
        # it could not see.
        try:
            occupied = any(out_dir.iterdir())
        except OSError as exc:
            raise PluginImportError(
                "output_not_readable",
                f"{out_dir} cannot be read ({exc.strerror or exc}); "
                "choose an empty output directory",
            ) from exc
        if occupied:
            detail = (
                f"{existing} already exists; choose an empty output directory"
                if existing.exists()
                else f"{out_dir} is not empty; choose an empty output directory"
            )
            raise PluginImportError("output_not_empty", detail)

    # Everything below writes into a STAGING directory, and ``out_dir`` gets it
    # only once the whole conversion succeeds. That is what makes the order of
    # the steps below stop mattering: skills are copied early and several
    # manifest fields are parsed after, so an ordinary malformed one (a non-list
    # ``keywords``, say) raises with files already written. Staged, such a failure
    # leaves ``out_dir`` exactly as it was, so the retry of the same command is
    # accepted rather than refused by the empty-directory precondition above.
    #
    # The staging dir is a sibling, so the publish is a same-filesystem rename,
    # and it is removed on the way out whether or not the conversion worked.
    # Creating the staging dir is itself two filesystem writes beside ``out_dir``,
    # and a read-only mount, an unwritable parent or a parent component that is a
    # regular file each raises an OSError the CLI does not catch. Reported with a
    # code so the operator reads a sentence naming the path instead of a
    # traceback. Distinct from the publish failure below because NOTHING has been
    # converted yet and the remedy is the output location, not whatever holds it.
    try:
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.partial-", dir=out_dir.parent))
    except OSError as exc:
        raise PluginImportError(
            "staging_unwritable",
            f"cannot create a staging directory beside {out_dir} "
            f"({exc.strerror or exc}); choose an output directory on writable storage",
        ) from exc
    try:
        report = _convert_into(root, data, interface, app_name, staging, report)
        # Publish. An empty ``out_dir`` the caller already created is removed
        # first: ``os.replace`` onto an existing directory fails, and the
        # precondition above has already proven this one holds nothing.
        removed_empty_out_dir = False
        try:
            if out_dir.exists():
                out_dir.rmdir()
                removed_empty_out_dir = True
            os.replace(staging, out_dir)
        except OSError as exc:
            if removed_empty_out_dir:
                # Put it back. A failed import must not consume something that was
                # already there: the caller created this directory, the move is what
                # failed, and leaving it absent turns a refusal into a deletion the
                # operator did not ask for. Best-effort, because the reason the move
                # failed may also stop the recreate, and the refusal below is the
                # answer either way.
                # Swallowed deliberately: this module reports through
                # PluginImportError and carries no logger, and the refusal raised
                # just below is the answer whether or not the restore worked.
                try:
                    out_dir.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
            # The conversion SUCCEEDED and only the move failed, so this is a
            # different report from a staging failure: someone wrote into
            # ``out_dir`` after the emptiness check, a handle holds it, or it is a
            # mount point so the sibling rename crosses devices. The CLI catches
            # PluginImportError and nothing else, so an OSError here would abort
            # with a traceback. ``out_dir`` is left absent or as it was and the
            # staged tree is removed below, so re-running the same command is
            # accepted by the precondition above.
            raise PluginImportError(
                "output_publish_failed",
                f"converted the package but could not move it into {out_dir} "
                f"({exc.strerror or exc}); check what holds that path and re-run",
            ) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return report


def _convert_into(
    root: Path,
    data: dict,
    interface: dict,
    app_name: str,
    out_dir: Path,
    report: ImportReport,
) -> ImportReport:
    """The conversion itself, writing only inside *out_dir*.

    Split out so :func:`convert_plugin_package` can hand it a staging directory
    and publish atomically; it makes no other decision.
    """
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

    keywords = [
        k.strip() for k in _manifest_list(data, "keywords") if isinstance(k, str) and k.strip()
    ]
    # _manifest_list refuses a wrong TYPE but bounds neither the item count nor each
    # string's length, and these land in app.json like every other carried field.
    bounded_keywords, kept_keywords = _bounded_manifest_value(keywords, "keywords", report)
    keywords = (
        list(bounded_keywords) if kept_keywords and isinstance(bounded_keywords, list) else []
    )
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

    # Bounded HERE, over the assembled dict, rather than at each field as it is
    # read. Three review rounds found three different unbounded fields in this
    # same structure -- mcpServers, then the carried block and keywords, then
    # displayName and description -- because a per-field bound only ever covers
    # the fields someone remembered, and every one of them is foreign text this
    # converter WRITES into app.json, which is read on every runtime load. One
    # pass over the finished dict covers a field added later too.
    #
    # `name` is excluded deliberately: normalize_app_name owns it, and it is an
    # IDENTITY -- truncating it would silently emit a different app rather than a
    # smaller one, so its validator refuses instead of shortening.

    # Bounded HERE, over the COMPLETE dict, as the last thing before it is
    # validated and written. Placing the pass earlier is what let the provenance
    # block escape it: the pass covered the fields assembled above it and nothing
    # added below. Bounding the finished object is the only placement where a
    # field added later is inside it by construction.
    #
    # `name` is excluded because it is an IDENTITY -- truncating it would emit a
    # DIFFERENT app rather than a smaller one -- and normalize_app_name now
    # REFUSES an over-long one, which is the correct treatment for an identity.
    for _field in [k for k in emitted if k != "name"]:
        _bounded, _kept = _bounded_manifest_value(emitted[_field], _field, report)
        if _kept:
            emitted[_field] = _bounded
        else:
            del emitted[_field]

    # Provenance is assembled AFTER that pass, not before it. The pass APPENDS
    # warnings (a dropped container item, an over-long string), so a provenance
    # snapshot taken ahead of it persists a warning list that is missing exactly
    # the truncations the pass just performed -- and those are the ones a reader
    # needs, because a silently shortened deny-list is the dangerous direction.
    provenance = report.to_dict()
    if carried:
        provenance["carried"] = carried
    _warned_before = len(report.warnings)
    _prov_bounded, _prov_kept = _bounded_manifest_value(provenance, "importedPlugin", report)
    # The bound returns ``object``; a dict in gives a dict out, but the key write
    # below is only safe once that is checked rather than assumed.
    if _prov_kept and isinstance(_prov_bounded, dict):
        if len(report.warnings) > _warned_before:
            # Bounding the provenance can only warn ABOUT the provenance, and that
            # text cannot be inside the thing it describes. A flag says it happened
            # without the recursion; the warnings themselves are on the report.
            _prov_bounded["bounded"] = True
        emitted["importedPlugin"] = _prov_bounded

    errors = AppManifest.from_dict(emitted).validate(out_dir)
    if errors:
        raise PluginImportError(
            "emitted_manifest_invalid",
            "the converted manifest did not validate: " + "; ".join(errors),
        )

    try:
        (out_dir / "app.json").write_text(json.dumps(emitted, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        # Reuses the staging code rather than minting a new one: this write lands
        # in the staging directory that code already names, so an operator reading
        # the refusal is pointed at the same remedy.
        raise PluginImportError(
            "staging_unwritable",
            f"cannot write the converted app.json under {out_dir} "
            f"({exc.strerror or exc}); choose an output directory on writable storage",
        ) from exc
    return report
