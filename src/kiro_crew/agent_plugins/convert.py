"""Convert a client-specific plugin into a standard Agent Plugin.

Follows the official Agent Plugins migration guide: the portable core
(``plugin.json``, ``skills/``, ``mcp.json``) is generated from what a Claude Code,
Codex or Cursor package ships, the client's own files are kept beside it, and
everything with no portable destination (hooks, commands, agents, rules, UI) is
named in the report instead of being converted.

This is a pure file transform. It reads the source, writes a NEW folder outside
it, never changes the source, and runs nothing from the package. Package
content is read only through paths that contain no link and stay inside the
source. The output is validated with the 1.0.0 loader before it is moved into
place, so a conversion either produces a plugin the loader accepts or writes
nothing. Design: ``docs/system-specs/modules/agent-plugins.md`` §4.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from kiro_crew.agent_plugins.manifest import (
    MANIFEST_FILE,
    MANIFEST_SCHEMAS,
    MAX_MANIFEST_BYTES,
    MCP_SCHEMA_FOR,
    SCHEMA_PREFIX,
    ManifestError,
    load_manifest,
)
from kiro_crew.agent_plugins.mcp_config import (
    MAX_MCP_BYTES,
    MCP_FILE,
    load_mcp_config,
    parse_mcp_config,
)
from kiro_crew.agent_plugins.model import Issue
from kiro_crew.agent_plugins.paths import resolves_within
from kiro_crew.agent_plugins.skills import (
    MAX_SKILL_BYTES,
    SKILL_FILE,
    SKILLS_DIR,
    discover_skills,
    frontmatter_name,
    is_valid_skill_name,
)
from kiro_crew.platform_compat_links import is_link_or_junction, unlink_link_or_junction
from kiro_crew.user_json import loads_user_json

#: Client manifests, in the order one is chosen when several are present.
CLIENT_MANIFESTS = (
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".cursor-plugin/plugin.json",
    ".plugin/plugin.json",
    ".github/plugin/plugin.json",
)
#: Where a client keeps skills when its manifest names no path.
DEFAULT_SKILL_ROOTS = ("skills", ".claude/skills", ".agents/skills")
#: Where a client keeps MCP servers when its manifest names none.
DEFAULT_MCP_FILES = (".mcp.json", "mcp.json")
SPEC_VERSION = "1.0.0"

#: Upper bounds on what each copy step of one conversion copies (counted separately).
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_FILES = 20_000
#: Upper bound on each JSON file read from the package (manifests, MCP files).
MAX_JSON_BYTES = 256 * 1024

_STRING_FIELDS = ("version", "description", "homepage", "repository", "license")
_AUTHOR_FIELDS = ("name", "email", "url")
#: Manifest keys copied or deliberately consumed; any other key is reported.
_CONSUMED_KEYS = frozenset(
    {"$schema", "name", "author", "keywords", "skills", "mcpServers", *_STRING_FIELDS}
)
_CLIENT_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"
_ROOT_VAR = "${PLUGIN_ROOT}"
_DATA_VAR = "${PLUGIN_DATA}"
#: Placeholder names a converted entry may still carry; the client's own root
#: name has been mapped to ``PLUGIN_ROOT`` by then.
_PORTABLE_VARS = frozenset({"PLUGIN_ROOT", "PLUGIN_DATA"})
#: Characters a §5.5 plugin name is made of.
_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-")
_SKIP_DIRS = frozenset({".git", "node_modules"})


class ConversionError(Exception):
    """The source cannot be converted; nothing was written."""


@dataclass
class ConversionReport:
    """What a conversion produced and what it left behind."""

    name: str
    source_manifest: str
    #: Artifacts with no portable destination, e.g. ``"hooks"``.
    not_converted: list[str] = field(default_factory=list)
    #: Server name -> why it was not converted.
    skipped_servers: dict[str, str] = field(default_factory=dict)
    #: Field-level changes worth telling the owner about.
    notes: list[str] = field(default_factory=list)
    #: What the 1.0.0 loader still reports about the output.
    issues: list[Issue] = field(default_factory=list)


def find_client_manifest(src: Path) -> str | None:
    """The relative path of the manifest a conversion would read, or ``None``.

    ``None`` also when the folder is already a standard plugin. A manifest
    reached through a link, or resolving outside *src*, is never chosen.
    """
    src = Path(os.path.realpath(src))
    root = _package_file(src, MANIFEST_FILE)
    if root is not None:
        try:
            data = _read_json(root)
        except ConversionError:
            data = None
        schema = data.get("$schema") if isinstance(data, dict) else None
        if isinstance(schema, str) and schema.startswith(SCHEMA_PREFIX):
            return None
    for rel in CLIENT_MANIFESTS:
        if _package_file(src, rel) is not None:
            return rel
    if root is not None:
        return MANIFEST_FILE
    return None


def convert_plugin(src: Path, dest: Path) -> ConversionReport:
    """Convert the package at *src* into a standard plugin at *dest*.

    *dest* must not exist and must not lie inside *src*. Raises
    :class:`ConversionError` when the package has no client manifest, its name
    cannot be made valid, it is too large, or the output would not pass the
    1.0.0 loader. On any failure the source is unchanged and nothing is left
    at or around *dest*.
    """
    src = Path(os.path.realpath(src))
    if not src.is_dir():
        raise ConversionError("the source is not a folder")
    if os.path.lexists(dest):
        raise ConversionError("the destination already exists")
    if _inside(dest, src):
        raise ConversionError("the destination is inside the source")
    rel = find_client_manifest(src)
    if rel is None:
        raise ConversionError("no client plugin manifest found (or it is already standard)")
    manifest_path = _package_file(src, rel)
    if manifest_path is None:
        raise ConversionError(f"{rel} could not be read")
    raw = _read_json(manifest_path)
    if not isinstance(raw, dict):
        raise ConversionError(f"{rel} is not a JSON object")

    manifest, name, notes = _convert_manifest(raw)
    report = ConversionReport(name=name, source_manifest=rel, notes=notes)
    report.not_converted = sorted(k for k in raw if k not in _CONSUMED_KEYS)
    servers = _load_client_servers(src, raw.get("mcpServers"), report)

    created = _make_parents(dest.parent)
    staging: Path | None = None
    scratch: Path | None = None
    try:
        staging = Path(
            os.path.realpath(tempfile.mkdtemp(prefix=f".{name}.converting-", dir=dest.parent))
        )
        scratch = Path(
            os.path.realpath(tempfile.mkdtemp(prefix=f".{name}.skills-", dir=dest.parent))
        )
        _copy_package(src, staging, report)
        _place_skills(staging, scratch, name, raw.get("skills"), report)
        _write_servers(servers, staging, report)
        _write_json(staging / MANIFEST_FILE, manifest, MAX_MANIFEST_BYTES)
        _validate(staging, report)
        os.replace(staging, dest)
    except BaseException as exc:
        for tmp in (staging, scratch):
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
        for path in reversed(created):
            try:
                path.rmdir()
            except OSError:
                break
        if isinstance(exc, OSError):
            raise ConversionError(
                f"the package could not be copied: {exc.__class__.__name__}"
            ) from exc
        raise
    shutil.rmtree(scratch, ignore_errors=True)
    return report


def _make_parents(parent: Path) -> list[Path]:
    """Create *parent* and its missing ancestors; return those created, outermost first."""
    missing: list[Path] = []
    cur = parent
    while not os.path.lexists(cur):
        missing.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    created: list[Path] = []
    try:
        for path in reversed(missing):
            path.mkdir()
            created.append(path)
    except OSError as exc:
        for path in reversed(created):
            try:
                path.rmdir()
            except OSError:
                break
        raise ConversionError(
            f"the destination folder could not be created: {exc.__class__.__name__}"
        ) from exc
    return created


def _convert_manifest(raw: dict[str, Any]) -> tuple[dict[str, Any], str, list[str]]:
    notes: list[str] = []
    name = normalize_name(raw.get("name"))
    if name is None:
        raise ConversionError("the plugin name cannot be made into a valid Agent Plugins name")
    if name != raw.get("name"):
        notes.append(f"name {raw.get('name')!r} became {name!r}")
    out: dict[str, Any] = {"$schema": MANIFEST_SCHEMAS[SPEC_VERSION], "name": name}
    for key in _STRING_FIELDS:
        value = raw.get(key)
        if isinstance(value, str):
            out[key] = value
        elif key in raw:
            notes.append(f"{key} dropped: not a string")
    author = raw.get("author")
    if isinstance(author, str) and author:
        out["author"] = {"name": author}
    elif isinstance(author, dict):
        kept = {k: v for k, v in author.items() if k in _AUTHOR_FIELDS and isinstance(v, str)}
        if kept:
            out["author"] = kept
        if len(kept) != len(author):
            notes.append("author fields other than name, email and url dropped")
    keywords = raw.get("keywords")
    if isinstance(keywords, list):
        out["keywords"] = [k for k in keywords if isinstance(k, str)]
    return out, name, notes


def normalize_name(value: object) -> str | None:
    """*value* turned into a §5.5 plugin name, or ``None`` when that is impossible."""
    if not isinstance(value, str):
        return None
    chars: list[str] = []
    for char in value.strip().lower():
        if char not in _NAME_CHARS:
            char = "-"
        if char in ".-" and chars and chars[-1] == char:
            continue
        chars.append(char)
    name = "".join(chars).strip(".-")[:64].strip(".-")
    return name or None


def _copy_package(src: Path, dest: Path, report: ConversionReport) -> None:
    """Copy the regular files of *src* into *dest*; links are never followed.

    A ``.git`` or ``node_modules`` folder is skipped at any depth.
    """
    budget = _CopyBudget()
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel_dir = os.path.relpath(dirpath, src)
        keep = []
        for d in sorted(dirnames):
            if d in _SKIP_DIRS:
                continue
            path = os.path.join(dirpath, d)
            if is_link_or_junction(path):
                report.notes.append(f"link not copied: {os.path.join(rel_dir, d)}")
                continue
            keep.append(d)
        dirnames[:] = keep
        target_dir = dest / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        for f in sorted(filenames):
            path = os.path.join(dirpath, f)
            if is_link_or_junction(path):
                report.notes.append(f"link not copied: {os.path.join(rel_dir, f)}")
                continue
            if not os.path.isfile(path):
                continue
            budget.copy(path, target_dir / f)


class _CopyBudget:
    """Counts the files one step copies against :data:`MAX_FILES` and :data:`MAX_TOTAL_BYTES`."""

    def __init__(self) -> None:
        self.files = 0
        self.total = 0

    def copy(self, source: Any, target: Any, *, follow_symlinks: bool = True) -> Any:
        self.files += 1
        self.total += os.path.getsize(source)
        if self.files > MAX_FILES or self.total > MAX_TOTAL_BYTES:
            raise ConversionError("the package is too large to convert")
        return shutil.copy2(source, target, follow_symlinks=follow_symlinks)


def _place_skills(
    staging: Path, scratch: Path, plugin_name: str, declared: Any, report: ConversionReport
) -> None:
    """Make every skill an immediate child of ``skills/`` named by its frontmatter.

    Every claimed skill is first copied into a fresh folder under *scratch*,
    which lies outside *staging*, and that folder then replaces ``skills/``. So
    no copy's source contains its destination, and no skill folder is removed
    while another skill still has to be read from it. A skill at the package
    root is the whole package except its top-level ``skills/``; when its
    frontmatter names no valid skill it takes *plugin_name*. Names are compared
    case-insensitively so the result is the same on a case-insensitive file
    system. These copies are bounded like :func:`_copy_package`.
    """
    roots: list[str] = []
    if isinstance(declared, str):
        roots = [declared]
    elif isinstance(declared, list):
        roots = [r for r in declared if isinstance(r, str)]
    declared_roots = bool(roots)
    if not roots:
        roots = [r for r in DEFAULT_SKILL_ROOTS if _package_dir(staging, r) is not None]
    target = staging / SKILLS_DIR
    claimed: dict[str, str] = {}
    moves: list[tuple[Path, str]] = []
    sources: set[Path] = set()
    dropped: set[Path] = set()
    for root in roots:
        base = _package_dir(staging, root)
        if base is None:
            report.notes.append(f"skills path not found: {root}")
            continue
        candidates: list[tuple[Path, Path]] = []
        skill_md = _package_file(staging, _rel(base / SKILL_FILE, staging))
        if skill_md is not None:
            candidates.append((base, skill_md))
        else:
            for child in sorted(base.iterdir()):
                if is_link_or_junction(child) or not child.is_dir():
                    continue
                skill_md = _package_file(staging, _rel(child / SKILL_FILE, staging))
                if skill_md is not None:
                    candidates.append((child, skill_md))
        if declared_roots and not candidates:
            report.notes.append(f"skills path yielded no skill: {root}")
        for child, skill_md in candidates:
            if child in sources:
                continue
            where = _rel(child, staging)
            name = _skill_name(skill_md, where, report)
            if not is_valid_skill_name(name):
                name = plugin_name if child == staging else child.name
            if child == staging and not is_valid_skill_name(name):
                report.notes.append("the skill at the package root was not placed: no valid name")
                continue
            assert isinstance(name, str)
            key = name.casefold()
            if key in claimed:
                report.notes.append(
                    f"skill {name!r} in {where} was not placed: {claimed[key]} has that name"
                )
                dropped.add(child)
                continue
            claimed[key] = where
            sources.add(child)
            moves.append((child, name))
    if not moves:
        return

    fresh = scratch / SKILLS_DIR
    fresh.mkdir()
    budget = _CopyBudget()
    for child, name in moves:
        _copy_skill(child, fresh / name, staging, budget)
    if os.path.lexists(target):
        if not is_link_or_junction(target) and target.is_dir():
            if target not in sources and target not in dropped:
                for entry in sorted(target.iterdir()):
                    if entry in sources or entry in dropped:
                        continue
                    if entry.name.casefold() in claimed:
                        where = _rel(entry, staging)
                        report.notes.append(f"{where} was replaced by a placed skill of that name")
                    else:
                        os.replace(entry, fresh / entry.name)
            shutil.rmtree(target)
        else:
            report.notes.append(f"{SKILLS_DIR} was not a folder and was replaced")
            unlink_link_or_junction(target)
    os.replace(fresh, target)


def _copy_skill(source: Path, target: Path, staging: Path, budget: _CopyBudget) -> None:
    """Copy the skill folder *source* to *target*, which must lie outside it.

    For the package root, the top-level ``skills/`` is left out: its entries are
    placed separately.
    """
    if _inside(target, source):
        raise ConversionError("a skill folder cannot be copied into itself")

    def ignore(folder: str, names: list[str]) -> list[str]:
        if os.fspath(folder) == os.fspath(staging):
            return [n for n in names if n == SKILLS_DIR]
        return []

    shutil.copytree(source, target, symlinks=True, ignore=ignore, copy_function=budget.copy)


def _skill_name(skill_md: Path, where: str, report: ConversionReport) -> str | None:
    if skill_md.stat().st_size > MAX_SKILL_BYTES:
        report.notes.append(f"{where}/{SKILL_FILE} is too large to read; kept its folder name")
        return None
    return frontmatter_name(skill_md.read_text(encoding="utf-8", errors="replace"))


def _client_server_map(doc: Any) -> dict[str, Any] | None:
    """Return servers from a wrapped document or a direct map of object entries."""
    if not isinstance(doc, dict):
        return None
    wrapped = doc.get("mcpServers")
    if isinstance(wrapped, dict):
        return wrapped
    if "mcpServers" not in doc and all(isinstance(entry, dict) for entry in doc.values()):
        return doc
    return None


def _load_client_servers(src: Path, declared: Any, report: ConversionReport) -> dict[str, Any]:
    if isinstance(declared, dict):
        servers = _client_server_map(declared)
    else:
        candidates = [declared] if isinstance(declared, str) else list(DEFAULT_MCP_FILES)
        servers = None
        for cand in candidates:
            path = _package_file(src, cand)
            if path is None:
                if isinstance(declared, str):
                    report.notes.append(f"MCP file not found in the package: {cand}")
                continue
            doc = _read_json(path)
            servers = _client_server_map(doc)
            if servers is not None:
                break
            report.notes.append(
                f"{cand} is neither an mcpServers document nor a direct server map; "
                "no MCP servers read from it"
            )
    if servers is None:
        if isinstance(declared, dict):
            report.notes.append("mcpServers is not an object; no MCP servers converted")
        return {}
    return servers


def _write_servers(servers: dict[str, Any], staging: Path, report: ConversionReport) -> None:
    """Write ``mcp.json`` holding exactly the servers the loader accepts, or no file.

    A client file already at ``mcp.json`` is never kept, since the standard
    gives that name its own meaning.
    """
    target = staging / MCP_FILE
    converted: dict[str, Any] = {}
    for name, entry in servers.items():
        try:
            converted[name] = convert_server(entry, staging)
        except ConversionError as exc:
            report.skipped_servers[name] = str(exc)
    schema = MCP_SCHEMA_FOR[MANIFEST_SCHEMAS[SPEC_VERSION]]
    doc: dict[str, Any] = {"$schema": schema, "mcpServers": converted}
    root_real = os.path.realpath(staging)
    accepted, issues = parse_mcp_config(
        doc, root_real, os.path.join(root_real, ".data-check"), schema
    )
    kept = {s.name for s in accepted}
    reasons = {i.entry: i.message for i in issues if i.entry is not None}
    whole = next((i.message for i in issues if i.entry is None), None)
    for name in [n for n in converted if n not in kept]:
        report.skipped_servers[name] = reasons.get(name) or whole or "the loader rejects it"
        del converted[name]
    if os.path.lexists(target):
        verb = "replaced by the converted one" if converted else "removed: no server converted"
        report.notes.append(f"the package's own {MCP_FILE} was {verb}")
        if not is_link_or_junction(target) and target.is_dir():
            shutil.rmtree(target)
        else:
            unlink_link_or_junction(target)
    if converted:
        _write_json(target, doc, MAX_MCP_BYTES)


def convert_server(entry: Any, root: Path) -> dict[str, Any]:
    """One client MCP entry as a 1.0.0 ``mcp.json`` entry.

    *root* is the converted package. A command or ``cwd`` that names a path is
    kept only when it stays inside *root*; a path command must also be a file
    there. Raises :class:`ConversionError` when the entry cannot be expressed.
    """
    if not isinstance(entry, dict):
        raise ConversionError("the entry is not an object")
    root_real = os.path.realpath(root)
    kind = entry.get("type")
    if kind is None:
        if "command" in entry:
            kind = "stdio"
        elif "url" in entry:
            kind = "streamable-http"
    elif kind == "http":
        kind = "streamable-http"
    if kind == "stdio":
        out: dict[str, Any] = {"type": "stdio"}
        command = _convert_command(entry.get("command"), root_real)
        out["command"] = command
        args = entry.get("args")
        if args is not None:
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise ConversionError("args is not a list of strings")
            out["args"] = [_map_root_var(a) for a in args]
        env = entry.get("env")
        if env is not None:
            if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
                raise ConversionError("env is not a map of strings")
            out["env"] = {k: _map_root_var(v) for k, v in env.items()}
        if entry.get("cwd") is not None:
            out["cwd"] = _convert_cwd(entry["cwd"], root_real)
    elif kind in ("streamable-http", "sse"):
        out = {"type": kind}
        if not isinstance(entry.get("url"), str):
            raise ConversionError("url is missing")
        out["url"] = entry["url"]
        headers = entry.get("headers")
        if headers is not None:
            if not isinstance(headers, dict) or not all(
                isinstance(v, str) for v in headers.values()
            ):
                raise ConversionError("headers is not a map of strings")
            out["headers"] = dict(headers)
    else:
        raise ConversionError(f"no portable transport for type {entry.get('type')!r}")
    unknown = sorted({n for s in _strings(out) for n in placeholder_names(s)} - _PORTABLE_VARS)
    if unknown:
        needs = ", ".join("${" + n + "}" for n in unknown)
        raise ConversionError(f"needs {needs}, which a standard plugin cannot carry")
    return out


class PlaceholderError(ConversionError):
    """A ``${`` in a converted value has no closing ``}``."""


def placeholder_names(value: str) -> list[str]:
    """The name inside each ``${...}`` of *value*, in order.

    Raises :class:`PlaceholderError` when a ``${`` is never closed.
    """
    names: list[str] = []
    start = value.find("${")
    while start != -1:
        end = value.find("}", start + 2)
        if end == -1:
            raise PlaceholderError("a ${ has no closing }")
        names.append(value[start + 2 : end])
        start = value.find("${", end + 1)
    return names


def _strings(node: Any) -> list[str]:
    """Every key and string value in *node*, a converted server entry."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(node, list):
        return [s for item in node for s in _strings(item)]
    return []


def _map_root_var(value: str) -> str:
    return value.replace(_CLIENT_ROOT_VAR, _ROOT_VAR)


def _convert_command(command: Any, root_real: str) -> str:
    if not isinstance(command, str) or not command:
        raise ConversionError("command is missing")
    for prefix in (_CLIENT_ROOT_VAR + "/", _ROOT_VAR + "/"):
        if command.startswith(prefix):
            command = "./" + command[len(prefix) :]
    if os.path.isabs(command) or command.startswith("~"):
        raise ConversionError("command is an absolute path, which is not portable")
    if not command.startswith("./") and "/" not in command and "\\" not in command:
        return command
    rel = command.removeprefix("./").replace("\\", "/")
    resolved = resolves_within(os.path.join(root_real, rel), root_real)
    if resolved is None:
        raise ConversionError("command points outside the package")
    if not os.path.isfile(resolved):
        raise ConversionError("command names a path that is not a file in the package")
    return "./" + rel


def _convert_cwd(cwd: Any, root_real: str) -> str:
    if not isinstance(cwd, str):
        raise ConversionError("cwd is not a string")
    cwd = _map_root_var(cwd)
    if cwd.startswith(_DATA_VAR):
        return cwd
    if os.path.isabs(cwd) or cwd.startswith("~"):
        raise ConversionError("cwd is an absolute path, which is not portable")
    if cwd in (".", "./", _ROOT_VAR):
        return _ROOT_VAR
    if cwd.startswith(_ROOT_VAR + "/"):
        rest, out = cwd[len(_ROOT_VAR) + 1 :], cwd
    else:
        rest = cwd.removeprefix("./")
        out = "./" + rest
    if resolves_within(os.path.join(root_real, rest), root_real) is None:
        raise ConversionError("cwd points outside the package")
    return out


def _validate(staging: Path, report: ConversionReport) -> None:
    root_real = os.path.realpath(staging)
    try:
        manifest, issues = load_manifest(root_real)
    except ManifestError as exc:
        raise ConversionError(f"the converted plugin.json is not valid: {exc}") from exc
    _, skill_issues = discover_skills(root_real)
    _, mcp_issues = load_mcp_config(
        root_real, os.path.join(root_real, ".data-check"), MCP_SCHEMA_FOR[manifest.schema]
    )
    report.issues = issues + skill_issues + mcp_issues


def _package_file(root: Path, rel: str) -> Path | None:
    """*root*/*rel* when it is a regular file reached through no link inside *root*.

    *root* must already be resolved.
    """
    path = _package_path(root, rel)
    if path is None or not path.is_file():
        return None
    return path


def _package_dir(root: Path, rel: str) -> Path | None:
    """Like :func:`_package_file`, for a folder; ``rel`` ``"."`` names *root*."""
    path = _package_path(root, rel)
    if path is None or not path.is_dir():
        return None
    return path


def _package_path(root: Path, rel: str) -> Path | None:
    parts = [p for p in PurePosixPath(rel.replace("\\", "/")).parts if p not in ("", ".")]
    if os.path.isabs(rel) or rel.startswith("~") or ".." in parts:
        return None
    path = root
    for part in parts:
        path = path / part
        if is_link_or_junction(path):
            return None
    if resolves_within(path, str(root)) is None:
        return None
    return path


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == str(
            os.path.realpath(root)
        )
    except ValueError:
        return False


def _read_json(path: Path) -> Any:
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ConversionError(f"{path.name} is too large")
        return loads_user_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ConversionError(f"{path.name} could not be read as JSON") from exc


def _write_json(path: Path, data: Any, max_bytes: int) -> None:
    text = json.dumps(data, indent=2) + "\n"
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        text = json.dumps(data, separators=(",", ":")) + "\n"
        encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ConversionError(f"converted {path.name} is too large")
    path.write_bytes(encoded)
