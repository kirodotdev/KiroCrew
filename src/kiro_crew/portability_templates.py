"""Agent templates in a portability archive, both ways.

Which templates a crew refers to and which of them an export leaves behind or misses,
the opted-in export of installed templates with their MCP secrets replaced by
:data:`AGENT_TEMPLATE_SECRET_PLACEHOLDER`, and, on import, the archive's template
members read, vetted, previewed and installed (:func:`_install_agent_templates`).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import zipfile
import zlib
from collections import deque
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from kiro_crew.agent_discovery import clear_list_agents_cache, parsed_agent_specs
from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.platform.governance import sanitize_agent_config_governance
from kiro_crew.portability_archive import (
    _is_link_entry,
    _mc_dir,
    _unsafe_member_name,
)
from kiro_crew.user_json import exceeds_nesting, strip_utf8_bom

_MANAGED_TEMPLATES = frozenset(Path(name).stem for name in OWNED_KIRO_AGENT_FILES)

#: The template warnings ride a response header (export) and a summary (import), so
#: both are bounded: at most this many names, each cut to this many characters.
MAX_TEMPLATE_WARNINGS = 20
MAX_TEMPLATE_NAME_CHARS = 64


def _clip(name: str) -> str:
    if len(name) <= MAX_TEMPLATE_NAME_CHARS:
        return name
    return name[: MAX_TEMPLATE_NAME_CHARS - 1] + "\u2026"


def _installed_specs(agents_dir: Path | None = None) -> list[tuple[dict[str, Any], Path]]:
    """Every parsed spec in the agents directory, read the one way this module reads
    them (`parsed_agent_specs`, labelled for the SEL denial trail)."""
    return parsed_agent_specs(
        agents_dir if agents_dir is not None else kiro_agents_dir(),
        operation="portability",
        source="dashboard",
    )


def crew_template_refs(config_path: Path) -> list[tuple[str, str]]:
    """``(crew, kiro_agent)`` for each crew row in *config_path* that names a template.

    The two config-level selectors that also name a template, ``agent.default_agent``
    and ``session.pool_agent``, are listed under those keys in place of a crew name.
    A bundle never carries ``<kiro home>/agents``, so every name listed here must
    already exist on whichever machine applies the config. The templates Kiro Crew
    writes itself (``OWNED_KIRO_AGENT_FILES``) are left out: every install
    regenerates them. An unreadable, malformed or pathologically nested file
    answers ``[]``: this feeds a warning, never a refusal.
    """
    try:
        data = json.loads(strip_utf8_bom(config_path.read_text(encoding="utf-8")))
        rows = data.get("agents")
    except (OSError, ValueError, AttributeError, RecursionError):
        return []
    named = [
        (str(crew), row.get("kiro_agent"))
        for crew, row in (rows.items() if isinstance(rows, dict) else ())
        if isinstance(row, dict)
    ]
    for section, key in (("agent", "default_agent"), ("session", "pool_agent")):
        block = data.get(section)
        if isinstance(block, dict):
            named.append((f"{section}.{key}", block.get(key)))
    return sorted(
        (holder, template)
        for holder, template in named
        if isinstance(template, str) and template and template not in _MANAGED_TEMPLATES
    )


def unbundled_agent_templates(bundled_stems: Iterable[str] = ()) -> tuple[list[str], int]:
    """The agent templates this install's crews name that an export does not carry.

    *bundled_stems* are the file stems the export carries; a crew may name one by
    its stem or by the spec's declared ``name``, and both count as carried.

    Returns ``(names, more)``: at most :data:`MAX_TEMPLATE_WARNINGS` clipped names,
    and how many further names were left out.
    """
    names = sorted({template for _crew, template in crew_template_refs(_mc_dir() / "config.json")})
    stems = set(bundled_stems)
    if stems:
        carried = set(stems)
        for data, path in _installed_specs():
            if path.stem in stems and isinstance(data, dict):
                declared = data.get("name")
                if isinstance(declared, str):
                    carried.add(declared)
        names = [n for n in names if n not in carried]
    kept = names[:MAX_TEMPLATE_WARNINGS]
    return [_clip(n) for n in kept], len(names) - len(kept)


def missing_crew_templates(config_path: Path) -> tuple[list[dict[str, str]], int]:
    """Crew rows in *config_path* whose ``kiro_agent`` template is not installed here.

    Returns ``(rows, more)``, bounded like :func:`unbundled_agent_templates`.

    Matches a spec by its ``name`` field or file stem, the same test the config
    loader applies when it resolves a crew's template.
    """
    refs = crew_template_refs(config_path)
    if not refs:
        return [], 0
    installed: set[str] = set()
    for data, path in _installed_specs():
        installed.add(path.stem)
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            installed.add(data["name"])
    missing = [(crew, template) for crew, template in refs if template not in installed]
    kept = missing[:MAX_TEMPLATE_WARNINGS]
    rows = [{"crew": _clip(crew), "kiro_agent": _clip(template)} for crew, template in kept]
    return rows, len(missing) - len(kept)


#: Archive directory (under the export prefix) that carries opted-in agent templates.
AGENT_TEMPLATES_DIR = "agents"

#: Stands in for every ``mcpServers.*.env`` and ``mcpServers.*.headers`` value of a
#: bundled template: those values hold tokens and keys, and an export carries none.
AGENT_TEMPLATE_SECRET_PLACEHOLDER = "<redacted: set on this machine>"

#: Most templates one export carries; the reference walk stops there.
MAX_BUNDLED_AGENT_TEMPLATES = 200

#: A bundled template's file stem: one path segment, no separators, no leading dot.
_TEMPLATE_STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: The spec keys under ``toolsSettings.subagent`` that name further templates.
_SUBAGENT_REF_KEYS = ("availableAgents", "trustedAgents")


def _template_refs_in_spec(data: dict[str, Any]) -> list[str]:
    """The template names *data* hands work to (its subagent allow lists).

    Glob patterns are left out: they name no single template.
    """
    tools_settings = data.get("toolsSettings")
    sub = tools_settings.get("subagent") if isinstance(tools_settings, dict) else None
    if not isinstance(sub, dict):
        return []
    names: list[str] = []
    for key in _SUBAGENT_REF_KEYS:
        value = sub.get(key)
        for name in value if isinstance(value, list) else ():
            if isinstance(name, str) and name and not any(ch in name for ch in "*?["):
                names.append(name)
    return names


def placeholder_mcp_values(data: dict[str, Any]) -> dict[str, Any]:
    """A copy of template *data* with every MCP server ``env`` and ``headers`` value,
    and its ``oauth.clientSecret``, replaced by :data:`AGENT_TEMPLATE_SECRET_PLACEHOLDER`.
    The keys stay, so the receiving machine sees which values it has to fill in."""
    out = json.loads(json.dumps(data))
    servers = out.get("mcpServers")
    for server in servers.values() if isinstance(servers, dict) else ():
        if not isinstance(server, dict):
            continue
        for field in ("env", "headers"):
            block = server.get(field)
            if isinstance(block, dict):
                server[field] = {key: AGENT_TEMPLATE_SECRET_PLACEHOLDER for key in block}
            elif field in server:
                server[field] = {}
        oauth = server.get("oauth")
        if isinstance(oauth, dict) and "clientSecret" in oauth:
            oauth["clientSecret"] = AGENT_TEMPLATE_SECRET_PLACEHOLDER
    return out


def bundled_agent_templates() -> tuple[list[tuple[str, dict[str, Any]]], int]:
    """``(file stem, redacted spec)`` for each template an opted-in export carries,
    and how many templates the walk reached past the cap without a crew naming them.

    Starts from the templates this install's crews name (:func:`crew_template_refs`)
    and follows each template's subagent allow lists in turn, visiting every name
    once so a cycle ends the walk. A name is matched against a spec's ``name`` field
    or file stem, the test the config loader applies. Templates Kiro Crew writes
    itself are skipped (every install regenerates them), as are specs reached
    through a link and specs whose stem is not a plain file name.

    At most :data:`MAX_BUNDLED_AGENT_TEMPLATES` are carried. The walk still finishes
    past the cap so the count is exact: a crew-named template left out already
    shows in :func:`unbundled_agent_templates`, so only the others are counted.
    """
    by_name: dict[str, tuple[str, dict[str, Any]]] = {}
    for data, path in _installed_specs():
        if not isinstance(data, dict) or path.is_symlink():
            continue
        if not _TEMPLATE_STEM_RE.match(path.stem) or path.stem in _MANAGED_TEMPLATES:
            continue
        by_name.setdefault(path.stem, (path.stem, data))
        spec_name = data.get("name")
        if isinstance(spec_name, str) and spec_name:
            by_name.setdefault(spec_name, (path.stem, data))

    named = {t for _holder, t in crew_template_refs(_mc_dir() / "config.json")}
    pending = deque(sorted(named))
    seen: set[str] = set()
    chosen: dict[str, dict[str, Any]] = {}
    left_out: dict[str, dict[str, Any]] = {}
    while pending:
        name = pending.popleft()
        if name in seen or name in _MANAGED_TEMPLATES:
            continue
        seen.add(name)
        found = by_name.get(name)
        if found is None:
            continue
        stem, data = found
        if stem in chosen or stem in left_out:
            continue
        if len(chosen) < MAX_BUNDLED_AGENT_TEMPLATES:
            chosen[stem] = placeholder_mcp_values(data)
        else:
            left_out[stem] = data
        pending.extend(_template_refs_in_spec(data))

    def _named_by_a_crew(stem: str, data: dict[str, Any]) -> bool:
        declared = data.get("name")
        return stem in named or (isinstance(declared, str) and declared in named)

    unnamed = [stem for stem, data in left_out.items() if not _named_by_a_crew(stem, data)]
    return sorted(chosen.items()), len(unnamed)


#: Largest bundled template an import reads.
MAX_AGENT_TEMPLATE_BYTES = 1024 * 1024


def is_agent_template_stem(name: str) -> bool:
    """Whether *name* can name a bundled template: one plain file-name segment."""
    return bool(_TEMPLATE_STEM_RE.match(name))


#: What reading one damaged archive member can raise: a corrupt or truncated deflate
#: stream (``zlib.error``, ``EOFError``), a bad CRC or header (``BadZipFile``), an
#: encrypted member (``RuntimeError``), an unsupported compression method
#: (``NotImplementedError``), and undecodable or malformed text (``ValueError``).
_UNREADABLE_MEMBER = (
    OSError,
    ValueError,
    RecursionError,
    EOFError,
    RuntimeError,
    NotImplementedError,
    zipfile.BadZipFile,
    zlib.error,
)


def _archive_agent_templates(
    zf: zipfile.ZipFile, want: frozenset[str] = frozenset()
) -> tuple[dict[str, zipfile.ZipInfo], int, set[str]]:
    """The bundled template members of *zf* by file stem, and what the cap left out.

    Only ``<prefix>/agents/<stem>.json`` regular files whose stem is a plain file name
    count. At most :data:`MAX_BUNDLED_AGENT_TEMPLATES` are kept; returns
    ``(kept, more, beyond)`` where *more* counts the distinct stems past the cap and
    *beyond* is those of them named in *want*, so a pick past the cap can say so
    instead of reading as absent.
    """
    found: dict[str, zipfile.ZipInfo] = {}
    # Exact, and bounded by the archive's own member cap (`_MAX_IMPORT_MEMBERS`),
    # which both the validator and the importer enforce before this runs.
    past_cap: set[str] = set()
    for info in zf.infolist():
        parts = PurePosixPath(info.filename).parts
        if len(parts) != 3 or parts[1] != AGENT_TEMPLATES_DIR or info.is_dir():
            continue
        if _unsafe_member_name(info.filename) or _is_link_entry(info):
            continue
        stem, dot, ext = parts[2].rpartition(".")
        if not dot or ext != "json" or not _TEMPLATE_STEM_RE.match(stem):
            continue
        if stem in found:
            continue
        if len(found) < MAX_BUNDLED_AGENT_TEMPLATES:
            found[stem] = info
        else:
            past_cap.add(stem)
    return found, len(past_cap), past_cap & want


def _installed_template_names() -> set[str]:
    """Every template name an import must not write over: each file stem in the
    agents directory (parseable or not), each declared ``name``, and the templates
    Kiro Crew writes itself."""
    names = set(_MANAGED_TEMPLATES)
    agents = kiro_agents_dir()
    with contextlib.suppress(OSError):
        with os.scandir(agents) as entries:
            for entry in entries:
                if entry.name.lower().endswith(".json"):
                    names.add(entry.name[: -len(".json")])
    for data, path in _installed_specs(agents):
        names.add(path.stem)
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            names.add(data["name"])
    return names


def _vet_bundled_template(
    zf: zipfile.ZipFile, stem: str, info: zipfile.ZipInfo, installed: set[str]
) -> tuple[dict[str, Any] | None, str | None]:
    """``(spec, None)`` when bundled template *stem* may be installed, else
    ``(None, reason)``: ``exists`` (the stem or the declared name is taken here) or
    ``invalid`` (too large, unreadable from the archive, not JSON, not an object, or
    nested too deep). Never raises for a damaged member: an import calls this after
    it has applied everything else."""
    if stem in installed:
        return None, "exists"
    if info.file_size > MAX_AGENT_TEMPLATE_BYTES:
        return None, "invalid"
    try:
        data = json.loads(strip_utf8_bom(zf.read(info).decode("utf-8")))
    except _UNREADABLE_MEMBER:
        return None, "invalid"
    if not isinstance(data, dict) or exceeds_nesting(data):
        return None, "invalid"
    declared = data.get("name")
    if isinstance(declared, str) and declared in installed:
        return None, "exists"
    return data, None


def preview_agent_templates(zip_path: Path) -> tuple[list[dict[str, object]], int]:
    """The templates *zip_path* carries, each with ``exists``: whether one by that
    name is already installed here, so an import would skip it. Also returns how
    many further templates the archive holds past :data:`MAX_BUNDLED_AGENT_TEMPLATES`,
    which an import does not install."""
    with zipfile.ZipFile(str(zip_path), "r") as zf:
        members, more, _beyond = _archive_agent_templates(zf)
        if not members:
            return [], more
        installed = _installed_template_names()
        rows: list[dict[str, object]] = []
        for stem, info in sorted(members.items()):
            _data, reason = _vet_bundled_template(zf, stem, info, installed)
            if reason != "invalid":
                rows.append({"name": stem, "exists": reason == "exists"})
        return rows, more


def _install_agent_templates(zip_path: Path, picked: list[str], summary: dict) -> None:
    """Install the bundled templates the user picked into the agents directory.

    A template is written only where no template of that name exists: the file is
    created exclusively, so even one that appears mid-import is left alone. Each
    one not written is named in ``agent_templates_skipped`` with its reason; a pick
    past :data:`MAX_BUNDLED_AGENT_TEMPLATES` reads ``over_limit``, not absent.
    """
    written: list[str] = []
    skipped: list[dict[str, str]] = []
    with zipfile.ZipFile(str(zip_path), "r") as zf:
        members, _more, beyond = _archive_agent_templates(zf, frozenset(picked))
        installed = _installed_template_names()
        agents = kiro_agents_dir()
        for stem in picked:
            info = members.get(stem)
            if info is None:
                absence = "over_limit" if stem in beyond else "not_in_archive"
                skipped.append({"name": _clip(stem), "reason": absence})
                continue
            data, reason = _vet_bundled_template(zf, stem, info, installed)
            if data is None:
                skipped.append({"name": _clip(stem), "reason": reason or "invalid"})
                continue
            # A bundled spec carries its source's allowedTools and autoApprove
            # verbatim, and both skip the PreToolUse gate, so this host's ceiling
            # filters them before the spec is written, like every whole-spec writer.
            sanitize_agent_config_governance(data)
            target = agents / f"{stem}.json"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                agents.mkdir(parents=True, exist_ok=True)
                fd = os.open(target, flags | getattr(os, "O_BINARY", 0), 0o600)
            except FileExistsError:
                skipped.append({"name": _clip(stem), "reason": "exists"})
                continue
            except OSError:
                skipped.append({"name": _clip(stem), "reason": "unwritable"})
                continue
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2)
            except OSError:
                with contextlib.suppress(OSError):
                    target.unlink()
                skipped.append({"name": _clip(stem), "reason": "unwritable"})
                continue
            installed.add(stem)
            if isinstance(data.get("name"), str):
                installed.add(data["name"])
            written.append(stem)
    if written:
        clear_list_agents_cache()
        summary["items"].append(f"agent templates ({len(written)} installed)")
    summary["agent_templates_installed"] = written
    summary["agent_templates_skipped"] = skipped
