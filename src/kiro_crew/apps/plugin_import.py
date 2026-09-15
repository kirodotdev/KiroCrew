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
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
MAX_RESOURCE_BYTES = 32 * 1024 * 1024

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


def _read_json_object(path: Path, what: str) -> dict[str, Any]:
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
    if not path.is_file() or path.is_symlink():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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
    if not root.is_dir():
        raise PluginImportError("source_not_a_directory", f"not a directory: {root}")

    root_manifest = root / MANIFEST_FILENAME
    if _is_schema_qualified(root_manifest):
        return root_manifest, FORMAT_SCHEMA_QUALIFIED

    for candidate_dir in sorted(root.glob(VENDOR_MANIFEST_DIR_GLOB)):
        if not candidate_dir.is_dir() or candidate_dir.is_symlink():
            continue
        candidate = candidate_dir / MANIFEST_FILENAME
        if candidate.is_file() and not candidate.is_symlink():
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


def _copy_tree_without_symlinks(src: Path, dst: Path) -> tuple[int, list[str]]:
    """Copy a directory tree, skipping every symlink rather than following it.

    Returns ``(files_copied, skipped_descriptions)``. Following a symlink would
    let a file outside the package root land inside the emitted app, which is
    the one thing ``resolve_declared_path`` exists to prevent -- so the same rule
    applies to the tree walk, not just to the declared path.
    """
    copied = 0
    skipped: list[str] = []
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        target = dst / entry.name
        if entry.is_symlink():
            skipped.append(f"symlink skipped: {entry}")
            continue
        if entry.is_dir():
            sub_copied, sub_skipped = _copy_tree_without_symlinks(entry, target)
            copied += sub_copied
            skipped.extend(sub_skipped)
            continue
        if not entry.is_file():
            skipped.append(f"not a regular file, skipped: {entry}")
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            skipped.append(f"unreadable, skipped: {entry}")
            continue
        if size > MAX_RESOURCE_BYTES:
            skipped.append(f"over {MAX_RESOURCE_BYTES} bytes, skipped: {entry}")
            continue
        shutil.copy2(entry, target)
        copied += 1
    return copied, skipped


def _discover_skill_dirs(root: Path) -> list[Path]:
    """Directories under ``root`` holding a skill entry file, breadth-first."""
    found: list[Path] = []
    frontier = [(root, 0)]
    while frontier:
        current, depth = frontier.pop(0)
        if depth > MAX_SKILL_TREE_DEPTH:
            continue
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        if (current / SKILL_ENTRY_FILENAME).is_file():
            found.append(current)
            continue
        for entry in entries:
            if entry.is_dir() and not entry.is_symlink():
                frontier.append((entry, depth + 1))
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
        if default_root.is_dir() and not default_root.is_symlink():
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
            if name in seen:
                report.warnings.append(f"duplicate skill directory name, skipped: {skill_dir}")
                continue
            seen.add(name)
            rel = f"{DEFAULT_SKILLS_DIR}/{name}"
            _, skipped = _copy_tree_without_symlinks(skill_dir, out_dir / rel)
            report.warnings.extend(skipped)
            emitted.append(rel)
    if emitted:
        report.mapped.append(
            MappedKind("skills", "app.json skills", f"{len(emitted)} skill(s) copied")
        )
    return emitted


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
    if isinstance(cwd, str) and cwd.strip() and not Path(cwd).is_absolute():
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
    """
    source = Path(source).expanduser()
    out_dir = Path(out_dir).expanduser()

    manifest_path, source_format = find_plugin_manifest(source)
    root = source.resolve()
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

    existing = out_dir / "app.json"
    if existing.exists():
        raise PluginImportError(
            "output_not_empty", f"{existing} already exists; choose an empty output directory"
        )
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
