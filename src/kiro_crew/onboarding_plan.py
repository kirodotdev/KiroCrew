"""The normalized import plan: what each foreign item becomes, and the plan document.

The middle layer of foreign-agent import (see
docs/system-specs/modules/onboarding-import.md). Source adapters hand parsed
foreign data to the category projections here, and each projection normalizes
it into :class:`~kiro_crew.onboarding_scan._Item` payloads through the content
gates in :mod:`kiro_crew.onboarding_scan`:

* ``instructions`` / ``memories`` -- chunking, the persona identity guard, the
  lesson ceiling, and database directives routed to the lesson tier;
* ``mcp_servers`` -- the field allowlist and the credential refusals, with the
  registry's managed names excluded;
* ``skills`` -- naming and deduplicating the packages
  :func:`~kiro_crew.onboarding_scan._skill_package` reads;
* ``schedules`` -- portable cron / interval / one-shot records;
* ``workspaces`` and ``settings`` -- validated paths and the unambiguous scalars.

It also owns the plan document itself: in-scan deduplication (layer 1 of the
spec's idempotency model), the per-source summary, the document
:func:`_plan_from_scans` builds, and the readers ``apply_import`` uses to take
it apart again. It depends only on :mod:`kiro_crew.onboarding_scan`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from croniter import croniter  # type: ignore[import-untyped]

from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.onboarding_scan import (
    _MAX_TEXT_CHARS,
    CATEGORY_IDS,
    _count_secret_fields,
    _is_file_safe,
    _Item,
    _read_json,
    _read_text,
    _sanitize_text,
    _Scan,
    _skill_package,
    _walk_files,
)
from kiro_crew.security import contains_injection, is_sensitive_path

_CATEGORY_LABELS = {
    "instructions": "Instructions",
    "memories": "Memories",
    "workspaces": "Workspaces",
    "mcp_servers": "MCP servers",
    "skills": "Skills",
    "schedules": "Schedules",
    "settings": "Settings",
}


# Import's own ceiling on lessons. ``LessonStore`` prunes OLDEST-first at 200,
# so an unbounded instruction import would silently evict the user's own
# accumulated corrections. See docs/system-specs/modules/onboarding-import.md.
_MAX_IMPORTED_LESSONS = 50


_MIN_INSTRUCTION_CHARS = 10


# Identity/self-description openers. Anchored at the paragraph start so an
# ordinary directive that merely mentions "you" ("Always tell the user when you
# skip a test") is unaffected.
# Leading Markdown structure: ATX heading, blockquote, unordered/ordered list
# marker, or a task-list checkbox. Stripped one layer at a time so nested forms
# ("> - [ ] You are Aria") reduce to the prose.
_MARKDOWN_PREFIX_RE = re.compile(r"^(?:#{1,6}\s+|>+\s*|[-*+]\s+|\d+[.)]\s+|\[[ xX]\]\s*)")


_IDENTITY_PARAGRAPH_RE = re.compile(
    r"^\s*(?:"
    r"you\s+are\b"
    r"|your\s+(?:name|persona|identity|role)\b"
    r"|i\s+am\b"
    r"|my\s+(?:name|persona|identity|role)\b"
    r"|(?:you|i)\s+(?:will\s+)?(?:act|behave|speak|respond)\s+as\b"
    r"|(?:the\s+)?(?:assistant|agent)\s+is\b"
    # Subjectless imperatives. A persona doc often writes the identity as a
    # command ("Act as Aria") rather than a statement ("You are Aria"), and an
    # imperative reads as a directive, so nothing else would catch it.
    r"|(?:act|behave|speak|respond|roleplay|role-play)\s+as\b"
    r"|pretend\s+(?:to\s+be|you(?:\s+are|\'re)?)\b"
    r"|assume\s+the\s+(?:role|persona|identity)\b"
    r"|adopt\s+the\s+(?:role|persona|identity|voice|tone)\s+of\b"
    r")",
    re.IGNORECASE,
)


_PLAN_VERSION = 1


_MAX_SKILL_BYTES = 256 * 1024


_MAX_WORKSPACES = 500


# Path ceilings applied to a FOREIGN-supplied workspace path before it becomes a
# candidate read. The total mirrors _workspace_item's existing 4096 limit; the
# per-component limit is POSIX NAME_MAX, which is the ceiling that actually
# raises first (255 on macOS and Linux, while PATH_MAX is 1024 / 4096).
_MAX_WORKSPACE_PATH_CHARS = 4096


_MAX_WORKSPACE_COMPONENT_CHARS = 255


_MAX_MCP_SERVERS = 200


_MAX_SCHEDULES = 500


_SENSITIVE_ARG_RE = re.compile(
    r"(?:--?(?:api[_-]?key|token|secret|password|credential|header|env)"
    r"|authorization\s*:|^[A-Za-z_][A-Za-z0-9_]*=)",
    re.IGNORECASE,
)


_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


_SAFE_THEME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


_MCP_RUNTIME_FIELDS = frozenset({"enabled", "disabled"})


_MCP_CONSTRAINT_FIELDS = frozenset(
    {
        "cwd",
        "disabledTools",
        "disabled_tools",
        "enabledTools",
        "enabled_tools",
        "toolFilter",
        "tool_filter",
        "tools",
        "allowedTools",
        "allowed_tools",
        "autoApprove",
        "auto_approve",
        "agent",
        "agents",
        "scope",
    }
)


_MCP_STDIO_FIELDS = frozenset({"command", "args"}) | _MCP_RUNTIME_FIELDS


_MCP_REMOTE_FIELDS = frozenset({"url"}) | _MCP_RUNTIME_FIELDS


_SCHEDULE_RECORD_FIELDS = frozenset(
    {
        "id",
        "name",
        "title",
        "message",
        "prompt",
        "text",
        "payload",
        "schedule",
        "timezone",
        "enabled",
    }
)


_SCHEDULE_PAYLOAD_FIELDS = frozenset({"message", "text"})


_SCHEDULE_SPEC_FIELDS = frozenset(
    {
        "kind",
        "type",
        "cron_expr",
        "cron",
        "expr",
        "every_secs",
        "interval_seconds",
        "interval",
        "minutes",
        "every_ms",
        "interval_ms",
        "milliseconds",
        "at_ts",
        "timestamp",
        "run_at",
        "at",
        "timezone",
    }
)


def _workspace_item(scan: _Scan, workspace: str) -> str | None:
    normalized = workspace.strip()
    if not normalized or "\x00" in normalized:
        return None
    if len(normalized) > 4096:
        scan.diagnostic("workspaces", "workspace_path_too_long")
        return None
    path = Path(os.path.expanduser(normalized))
    if not path.is_absolute():
        scan.diagnostic("workspaces", "workspace_not_absolute")
        return None
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        scan.diagnostic("workspaces", "workspace_unavailable")
        return None
    if not resolved.is_dir():
        scan.diagnostic("workspaces", "workspace_not_directory")
        return None
    if is_sensitive_path(str(resolved)):
        scan.diagnostic("workspaces", "sensitive_workspace_excluded")
        return None
    try:
        source_root = scan.root.resolve(strict=True)
    except (OSError, RuntimeError):
        source_root = scan.root.resolve()
    if resolved == source_root or source_root in resolved.parents:
        scan.diagnostic("workspaces", "source_workspace_excluded")
        return None
    canonical = str(resolved)
    scan.add("workspaces", hashlib.sha256(canonical.encode()).hexdigest(), canonical)
    return canonical


def _collect_project_paths(config: Any) -> set[str]:
    paths: set[str] = set()
    if not isinstance(config, dict):
        return paths
    projects = config.get("projects")
    if isinstance(projects, dict):
        paths.update(str(key) for key in projects if isinstance(key, str))
    elif isinstance(projects, list):
        for item in projects:
            if isinstance(item, str):
                paths.add(item)
            elif isinstance(item, dict):
                for key in ("path", "cwd", "root"):
                    value = item.get(key)
                    if isinstance(value, str):
                        paths.add(value)
    workspaces = config.get("workspaces")
    if isinstance(workspaces, dict):
        for workspace in workspaces.values():
            if isinstance(workspace, str):
                paths.add(workspace)
            elif isinstance(workspace, dict):
                for key in ("dir", "path", "cwd", "root"):
                    value = workspace.get(key)
                    if isinstance(value, str):
                        paths.add(value)
    for key in ("workspace", "workspace_dir", "project_path", "cwd"):
        value = config.get(key)
        if isinstance(value, str):
            paths.add(value)
    return paths


def _settings_from(config: dict[str, Any], _source_id: str) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    timezone_value = config.get("timezone")
    if _source_id == "openclaw":
        agents = config.get("agents")
        defaults = agents.get("defaults") if isinstance(agents, dict) else None
        if isinstance(defaults, dict):
            timezone_value = defaults.get("userTimezone", timezone_value)
    if isinstance(timezone_value, str):
        try:
            ZoneInfo(timezone_value)
            settings["timezone"] = timezone_value
        except (ValueError, KeyError):
            pass

    dashboard = config.get("dashboard")
    if not isinstance(dashboard, dict):
        dashboard = {}
    theme_mode = dashboard.get("theme_mode", config.get("theme_mode", config.get("theme")))
    if _source_id == "openclaw":
        control_ui = config.get("controlUi")
        prefs = control_ui.get("prefs") if isinstance(control_ui, dict) else None
        if isinstance(prefs, dict):
            theme_mode = prefs.get("themeMode", theme_mode)
    if theme_mode in ("dark", "light", "system"):
        settings.setdefault("dashboard", {})["theme_mode"] = theme_mode
    theme_color = dashboard.get("theme_color", config.get("theme_color"))
    if isinstance(theme_color, str) and _SAFE_THEME_RE.fullmatch(theme_color):
        settings.setdefault("dashboard", {})["theme_color"] = theme_color

    return settings


def _safe_mcp_name(value: Any, managed_mcp_names: Callable[[], frozenset[str]]) -> str:
    """The server alias an import may use, or ``""`` when *value* cannot be one.

    *managed_mcp_names* is the registry's lookup of casefolded names that are
    never imported; callers pass the scan's ``_Scan.managed_mcp_names``.
    """
    if not isinstance(value, str):
        return ""
    name = mcp_server_alias(value.strip())
    if (
        not name
        or len(name) > 128
        or name.casefold() in managed_mcp_names()
        or "/" in name
        or "\\" in name
        or name in (".", "..")
    ):
        return ""
    return name


def _url_has_literal_secret(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return True
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return True
    if parsed.username is not None or parsed.password is not None:
        return True
    if parsed.query or parsed.fragment:
        return True
    return False


def _sanitize_mcp_spec(spec: Any, scan: _Scan) -> dict[str, Any] | None:
    if not isinstance(spec, dict):
        scan.diagnostic("mcp_servers", "unsupported_mcp_schema", unsupported=True)
        return None
    omitted_secret_fields = _count_secret_fields(spec)
    if omitted_secret_fields:
        scan.diagnostic("mcp_servers", "credential_bearing_server")
        return None

    fields = set(spec)
    has_command = "command" in fields
    has_url = "url" in fields
    if has_command == has_url:
        scan.diagnostic("mcp_servers", "unsupported_mcp_schema", unsupported=True)
        return None
    allowed_fields = _MCP_STDIO_FIELDS if has_command else _MCP_REMOTE_FIELDS
    unknown_fields = fields - allowed_fields
    if unknown_fields:
        if unknown_fields & _MCP_CONSTRAINT_FIELDS:
            scan.diagnostic("mcp_servers", "unsupported_mcp_constraints", unsupported=True)
        else:
            scan.diagnostic("mcp_servers", "unsupported_mcp_schema", unsupported=True)
        return None

    result: dict[str, Any] = {}
    if has_command:
        command = spec.get("command")
        if not isinstance(command, str) or not command.strip():
            scan.diagnostic("mcp_servers", "unsupported_mcp_schema", unsupported=True)
            return None
        cleaned = _sanitize_text(command.strip(), scan)
        if cleaned != command.strip() or len(cleaned) > 2048:
            scan.diagnostic("mcp_servers", "credential_bearing_server")
            return None
        result["command"] = cleaned
    else:
        url = spec.get("url")
        if not isinstance(url, str) or not url.strip():
            scan.diagnostic("mcp_servers", "unsupported_mcp_schema", unsupported=True)
            return None
        cleaned_url = _sanitize_text(url.strip(), scan)
        if cleaned_url != url.strip() or _url_has_literal_secret(url.strip()):
            scan.secret_count += 1
            scan.diagnostic("mcp_servers", "credential_bearing_server")
            return None
        result["url"] = url.strip()

    args = spec.get("args") if has_command else None
    if has_command and args is not None:
        if not isinstance(args, list) or len(args) > 100:
            scan.diagnostic("mcp_servers", "unsupported_mcp_schema", unsupported=True)
            return None
        safe_args: list[str] = []
        for arg in args:
            if not isinstance(arg, str) or len(arg) > 4096 or _SENSITIVE_ARG_RE.search(arg):
                scan.secret_count += 1
                scan.diagnostic("mcp_servers", "credential_bearing_server")
                return None
            cleaned_arg = _sanitize_text(arg, scan)
            if cleaned_arg != arg:
                scan.diagnostic("mcp_servers", "credential_bearing_server")
                return None
            safe_args.append(arg)
        if safe_args:
            result["args"] = safe_args
    # A copied definition is passive until the user reviews and enables it.
    result["disabled"] = True
    return result


def _mcp_maps(config: Any) -> list[dict[str, Any]]:
    if not isinstance(config, dict):
        return []
    maps: list[dict[str, Any]] = []
    for key in ("mcpServers", "mcp_servers"):
        value = config.get(key)
        if isinstance(value, dict):
            maps.append(value)
    mcp = config.get("mcp")
    if isinstance(mcp, dict):
        nested = mcp.get("servers")
        if isinstance(nested, dict):
            maps.append(nested)
        elif mcp and all(isinstance(value, dict) for value in mcp.values()):
            if any("command" in value or "url" in value for value in mcp.values()):
                maps.append(mcp)
    if not maps and config and all(isinstance(value, dict) for value in config.values()):
        if any("command" in value or "url" in value for value in config.values()):
            maps.append(config)
    return maps


def _add_mcp_configs(scan: _Scan, configs: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    omitted_secret_fields = 0
    for config in configs:
        for servers in _mcp_maps(config):
            for raw_name, raw_spec in servers.items():
                if len(scan.items["mcp_servers"]) >= _MAX_MCP_SERVERS:
                    scan.diagnostic("mcp_servers", "item_count_limit")
                    return
                name = _safe_mcp_name(raw_name, scan.managed_mcp_names)
                if not name:
                    if (
                        isinstance(raw_name, str)
                        and raw_name.casefold() in scan.managed_mcp_names()
                    ):
                        scan.diagnostic("mcp_servers", "managed_server_excluded")
                    else:
                        scan.diagnostic("mcp_servers", "invalid_server_name")
                    continue
                if name in seen:
                    continue
                spec = _sanitize_mcp_spec(raw_spec, scan)
                omitted_secret_fields += _count_secret_fields(raw_spec)
                if spec is None:
                    continue
                seen.add(name)
                key = name + "\0" + json.dumps(spec, sort_keys=True)
                scan.add("mcp_servers", key, {"name": name, "spec": spec})
    if omitted_secret_fields:
        scan.secret_count += omitted_secret_fields
        scan.diagnostic(
            "mcp_servers",
            "secret_fields_omitted",
            count=omitted_secret_fields,
        )


def _safe_skill_name(relative: Path) -> str:
    parts: list[str] = []
    for part in relative.parts:
        safe = _SAFE_NAME_RE.sub("-", part).strip("-._").lower()
        if not safe or safe in (".", ".."):
            return ""
        parts.append(safe[:64])
    return "/".join(parts)


def _add_skills(
    scan: _Scan,
    roots: list[Path],
    *,
    excluded_parts: frozenset[str] = frozenset(),
    excluded_names: frozenset[str] = frozenset(),
) -> None:
    seen_roots: set[str] = set()
    seen_names: set[str] = set()
    for root in roots:
        marker = os.path.normcase(os.path.abspath(str(root)))
        if marker in seen_roots:
            continue
        seen_roots.add(marker)
        for path in _walk_files(
            root,
            scan,
            "skills",
            names=("SKILL.md",),
            excluded_parts=excluded_parts,
            count_files=False,
        ):
            try:
                relative = path.parent.relative_to(root)
            except ValueError:
                continue
            if {part.casefold() for part in relative.parts} & excluded_parts:
                continue
            name = _safe_skill_name(relative)
            if not name or name.casefold() in excluded_names or name in seen_names:
                continue
            if path.lstat().st_size > _MAX_SKILL_BYTES:
                scan.diagnostic("skills", "file_too_large")
                continue
            files = _skill_package(scan, root, path)
            if files is None:
                continue
            if not files["SKILL.md"].strip():
                scan.diagnostic("skills", "empty_skill")
                continue
            seen_names.add(name)
            digest = hashlib.sha256(
                json.dumps(files, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            key = name + "\0" + digest
            scan.add("skills", key, {"name": name, "files": files})


def _memory_chunks(text: str, scan: _Scan) -> list[str]:
    chunks: list[str] = []
    current = ""
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > 2000:
            scan.diagnostic("memories", "unsupported_memory_length")
            continue
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) > 2000:
            if len(current) >= 10:
                chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if len(current) >= 10:
        chunks.append(current)
    return chunks


def _add_memory_files(scan: _Scan, paths: list[tuple[Path, Path]]) -> None:
    seen: set[str] = set()
    for path, anchor in paths:
        marker = os.path.normcase(os.path.abspath(str(path)))
        if marker in seen or not _is_file_safe(path):
            continue
        seen.add(marker)
        content = _read_text(path, anchor, scan, "memories")
        if content is None:
            continue
        cleaned = _sanitize_text(content, scan)
        # _sanitize_text truncates to _MAX_TEXT_CHARS before redacting, so compare
        # against the same truncated baseline: only an actual redaction (credential
        # removed) should drop the file, not the size-cap truncation of a clean one.
        if cleaned != content[:_MAX_TEXT_CHARS].strip():
            scan.diagnostic("memories", "credential_bearing_memory")
            continue
        if contains_injection(cleaned):
            scan.diagnostic("memories", "injection_memory_excluded")
            continue
        try:
            relative = str(path.relative_to(anchor))
        except ValueError:
            relative = path.name
        for index, chunk in enumerate(_memory_chunks(cleaned, scan)):
            digest = hashlib.sha256(chunk.encode()).hexdigest()
            scan.add(
                "memories",
                f"{relative}\0{index}\0{digest}",
                {
                    "kind": "episodic",
                    "text": chunk,
                    "importance": 0.5,
                },
            )


def _strip_markdown_prefix(line: str) -> str:
    """Drop list/quote/emphasis markers so identity matching sees the prose.

    A persona document routinely bullets its identity ("- You are Aria",
    "> **You are Aria**"), and an anchored match would test the marker rather
    than the sentence.
    """

    stripped = line.strip()
    while True:
        candidate = _MARKDOWN_PREFIX_RE.sub("", stripped, count=1).strip()
        if candidate == stripped:
            return stripped.lstrip("*_`~ ").strip()
        stripped = candidate


def _instruction_paragraphs(text: str, scan: _Scan) -> list[str]:
    """Split an instruction document into individually-injectable directives.

    Reuses the memory chunker's paragraph packing so a directive and its
    memory-tier sibling are bounded identically, then keeps only paragraphs that
    read as instructions rather than narrative. A heading-only line carries no
    directive on its own and is dropped.
    """

    directives: list[str] = []
    for chunk in _memory_chunks(text, scan):
        for paragraph in chunk.split("\n\n"):
            candidate = paragraph.strip()
            if len(candidate) < _MIN_INSTRUCTION_CHARS:
                continue
            lines = [line.strip() for line in candidate.splitlines() if line.strip()]
            if not lines or all(line.startswith("#") for line in lines):
                continue
            # Check EVERY non-heading line, not just the paragraph start or its
            # first content line. A persona document writes identity under a
            # heading ("# Persona\nYou are Aria") and also mixes it in after a
            # directive ("Always cite paths.\nYou are Aria."), so any single
            # anchor leaves a hole. One identity line taints the paragraph: it is
            # imported whole, so a partial match would still inject the identity.
            # Check EVERY line, headings included. Each previous narrowing of this
            # scan (paragraph-start, then first content line, then non-heading
            # lines only) left a hole, because the exclusion itself was the bug:
            # "# You are Aria" is a heading AND an identity statement. Normalizing
            # heading markers away and scanning everything removes the last
            # place identity can hide.
            if any(_IDENTITY_PARAGRAPH_RE.match(_strip_markdown_prefix(line)) for line in lines):
                # A persona document mixes IDENTITY ("You are Aria, a laconic
                # assistant") with DIRECTIVES ("Always cite a file path"). Only
                # the directives are in scope: importing an identity statement
                # into an always-injected lesson would make foreign text act as
                # the agent's persona through a path that bypasses
                # capabilities.theme_persona -- exactly what excluding the
                # persona role is meant to prevent.
                scan.diagnostic("instructions", "persona_identity_excluded")
                continue
            directives.append(candidate)
    return directives


def _add_instruction_files(
    scan: _Scan,
    paths: list[tuple[Path, Path]],
) -> None:
    """Project user-authored instruction documents onto Kiro Crew's memory tiers.

    ``CLAUDE.md`` / ``AGENTS.md`` and the DIRECTIVE body of a persona document
    (OpenClaw / Hermes ``SOUL.md``) are the least replaceable thing a user owns,
    so they land in ``lessons.jsonl`` — the highest-priority durable tier
    (see docs/system-specs/modules/onboarding-import.md). The persona *role* is
    deliberately NOT imported: Kiro Crew's persona surface is theme-pack persona,
    governed by ``capabilities.theme_persona``, and no foreign text may become
    system-prompt identity through this path.

    ``preferences.md`` / ``projects.md`` are NOT valid destinations — the memory
    consolidator replaces both wholesale, so an import there is destroyed on the
    next consolidation run.
    """

    seen: set[str] = set()
    for path, anchor in paths:
        marker = os.path.normcase(os.path.abspath(str(path)))
        if marker in seen or not _is_file_safe(path):
            continue
        seen.add(marker)
        content = _read_text(path, anchor, scan, "instructions")
        if content is None:
            continue
        cleaned = _sanitize_text(content, scan)
        # Mirror the memory gate: only an actual redaction drops the file, not
        # the size-cap truncation of an otherwise clean one.
        if cleaned != content[:_MAX_TEXT_CHARS].strip():
            scan.diagnostic("instructions", "credential_bearing_instruction")
            continue
        if contains_injection(cleaned):
            scan.diagnostic("instructions", "injection_instruction_excluded")
            continue
        try:
            relative = str(path.relative_to(anchor))
        except ValueError:
            relative = path.name
        for index, directive in enumerate(_instruction_paragraphs(cleaned, scan)):
            if len(scan.items["instructions"]) >= _MAX_IMPORTED_LESSONS:
                scan.diagnostic("instructions", "instruction_count_limit")
                return
            digest = hashlib.sha256(directive.encode()).hexdigest()
            scan.add(
                "instructions",
                f"{relative}\0{index}\0{digest}",
                {"kind": "lesson", "rule": directive},
            )


def _add_db_directive(scan: _Scan, key: str, value: Any) -> None:
    """Project a foreign memory row typed as a DIRECTIVE onto the lesson tier.

    A directive is a rule the user taught the agent, not a fact, so semantic
    memory (key/value, confidence-gated) is the wrong destination -- the lesson
    tier is. Dropping such a row (as ``directive_memory_unsupported``) would
    discard exactly the least replaceable thing in a foreign store.

    Passes the same gates as a file-sourced directive, and re-runs the content
    screens on the DECODED rule. The caller screened ``value_json`` — the raw JSON
    text — but what lands in the lesson is the ``json.loads`` result, and any JSON
    escape survives a screen applied before decoding: ``"Ignore all previous\\n
    instructions…"`` carries a literal backslash-n on disk, so the injection
    pattern cannot match, yet the decoded string is a real newline and matches.
    Screening pre-decode is therefore not screening at all for this destination —
    and this tier is injected into every session as authoritative, so it is the
    worst place to land unscreened text.
    """
    # The row's value is JSON — a bare string for a rule, or an object wrapping
    # one. Anything else is not a directive we can render as a rule.
    if isinstance(value, str):
        rule = value.strip()
    elif isinstance(value, dict):
        candidate = value.get("rule") or value.get("text") or value.get("value")
        rule = candidate.strip() if isinstance(candidate, str) else ""
    else:
        rule = ""
    if len(rule) < _MIN_INSTRUCTION_CHARS or len(rule) > _MAX_TEXT_CHARS:
        scan.diagnostic("memories", "unsupported_memory_length")
        return
    # Both screens, on the decoded text. A *redaction* means the rule carried a
    # credential, so drop it (mirroring _add_instruction_files); a mere size
    # truncation is not a reason to drop.
    cleaned = _sanitize_text(rule, scan)
    if cleaned != rule[:_MAX_TEXT_CHARS].strip():
        scan.diagnostic("instructions", "credential_bearing_instruction")
        return
    if contains_injection(cleaned):
        scan.diagnostic("instructions", "injection_instruction_excluded")
        return
    rule = cleaned
    lines = [line.strip() for line in rule.splitlines() if line.strip()]
    if any(_IDENTITY_PARAGRAPH_RE.match(_strip_markdown_prefix(line)) for line in lines):
        scan.diagnostic("instructions", "identity_paragraph_excluded")
        return
    if len(scan.items["instructions"]) >= _MAX_IMPORTED_LESSONS:
        scan.diagnostic("instructions", "instruction_count_limit")
        return
    digest = hashlib.sha256(rule.encode()).hexdigest()
    scan.add(
        "instructions", f"sqlite\0directive\0{key}\0{digest}", {"kind": "lesson", "rule": rule}
    )


def _add_memories(scan: _Scan, roots: list[Path]) -> None:
    seen: set[str] = set()
    paths: list[tuple[Path, Path]] = []
    for root in roots:
        marker = os.path.normcase(os.path.abspath(str(root)))
        if marker in seen:
            continue
        seen.add(marker)
        for path in _walk_files(root, scan, "memories", suffixes=(".md", ".markdown")):
            paths.append((path, root))
    _add_memory_files(scan, paths)


def _has_unsupported_schedule_semantics(record: dict[str, Any]) -> bool:
    record_fields = set(record)
    if record_fields - (_SCHEDULE_RECORD_FIELDS | _SCHEDULE_SPEC_FIELDS):
        return True
    payload = record.get("payload")
    if isinstance(payload, dict) and set(payload) - _SCHEDULE_PAYLOAD_FIELDS:
        return True
    schedule = record.get("schedule")
    if isinstance(schedule, dict) and set(schedule) - _SCHEDULE_SPEC_FIELDS:
        return True
    return False


def _interval_seconds(value: Any, multiplier: int, divisor: int = 1) -> int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if isinstance(value, int):
        seconds, remainder = divmod(value * multiplier, divisor)
        return seconds if remainder == 0 else None
    try:
        number = float(value)
        seconds_number = number * multiplier / divisor
    except (OverflowError, ValueError):
        return None
    if (
        not math.isfinite(number)
        or not math.isfinite(seconds_number)
        or not seconds_number.is_integer()
    ):
        return None
    return int(seconds_number)


def _schedule_from_record(record: Any, scan: _Scan) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    if _has_unsupported_schedule_semantics(record):
        scan.diagnostic("schedules", "unsupported_schedule_semantics", unsupported=True)
        return None
    name = record.get("name", record.get("title", "Imported schedule"))
    message = record.get("message", record.get("prompt", record.get("text")))
    record_payload = record.get("payload")
    if not isinstance(message, str) and isinstance(record_payload, dict):
        message = record_payload.get("message", record_payload.get("text"))
    if not isinstance(name, str) or not name.strip() or not isinstance(message, str):
        scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
        return None
    secrets_before = scan.secret_count
    name_clean = _sanitize_text(name, scan)[:200]
    message_clean = _sanitize_text(message, scan)
    if scan.secret_count > secrets_before:
        scan.diagnostic("schedules", "credential_bearing_schedule")
        return None
    if not name_clean or not message_clean:
        return None

    schedule = record.get("schedule", record)
    kind = ""
    cron_expr: str | None = None
    every_secs: int | None = None
    at_ts: float | None = None
    timezone_name = ""
    timezone_value = record.get("timezone")
    if isinstance(schedule, dict):
        timezone_value = schedule.get("timezone", timezone_value)
    if timezone_value is not None and not isinstance(timezone_value, str):
        scan.diagnostic("schedules", "invalid_timezone")
        return None
    if timezone_value:
        try:
            ZoneInfo(timezone_value)
            timezone_name = timezone_value
        except (ValueError, KeyError):
            scan.diagnostic("schedules", "invalid_timezone")
            return None
    if isinstance(schedule, str):
        kind = "cron"
        cron_expr = schedule.strip()
    elif isinstance(schedule, dict):
        kind = str(schedule.get("kind", schedule.get("type", ""))).lower()
        cron_value = schedule.get("cron_expr", schedule.get("cron", schedule.get("expr")))
        trigger_families: set[str] = set()
        if isinstance(cron_value, str):
            cron_expr = cron_value.strip()
            if cron_expr:
                trigger_families.add("cron")
        every_value = schedule.get(
            "every_secs", schedule.get("interval_seconds", schedule.get("interval"))
        )
        if isinstance(every_value, (int, float)) and not isinstance(every_value, bool):
            every_secs = _interval_seconds(every_value, 1)
            if every_secs is None or every_secs <= 0:
                scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
                return None
            if every_secs < 60:
                scan.diagnostic("schedules", "unsupported_sub_minute_interval", unsupported=True)
                return None
            trigger_families.add("interval")
        minutes_value = schedule.get("minutes")
        if isinstance(minutes_value, (int, float)) and not isinstance(minutes_value, bool):
            every_secs = _interval_seconds(minutes_value, 60)
            if every_secs is None or every_secs <= 0:
                scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
                return None
            if every_secs < 60:
                scan.diagnostic("schedules", "unsupported_sub_minute_interval", unsupported=True)
                return None
            trigger_families.add("interval")
        milliseconds_value = schedule.get(
            "every_ms",
            schedule.get("interval_ms", schedule.get("milliseconds")),
        )
        if isinstance(milliseconds_value, (int, float)) and not isinstance(
            milliseconds_value, bool
        ):
            every_secs = _interval_seconds(milliseconds_value, 1, 1000)
            if every_secs is None or every_secs <= 0:
                scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
                return None
            if every_secs < 60:
                scan.diagnostic("schedules", "unsupported_sub_minute_interval", unsupported=True)
                return None
            trigger_families.add("interval")
        at_value = schedule.get(
            "at_ts",
            schedule.get("timestamp", schedule.get("run_at", schedule.get("at"))),
        )
        if isinstance(at_value, (int, float)) and not isinstance(at_value, bool):
            at_ts = float(at_value)
            if not math.isfinite(at_ts) or at_ts <= 0:
                scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
                return None
            trigger_families.add("at")
        if isinstance(at_value, str):
            try:
                parsed = datetime.fromisoformat(at_value.strip().replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    if not timezone_name:
                        raise ValueError
                    parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
                at_ts = parsed.timestamp()
                if not math.isfinite(at_ts) or at_ts <= 0:
                    raise ValueError
                trigger_families.add("at")
            except (ValueError, KeyError):
                scan.diagnostic(
                    "schedules",
                    "unsupported_schedule_schema",
                    unsupported=True,
                )
                return None
        if len(trigger_families) != 1:
            scan.diagnostic("schedules", "ambiguous_schedule_trigger", unsupported=True)
            return None
        family = next(iter(trigger_families))
        # Exactly one family here (guarded above), drawn from the three literals
        # added while scanning the schedule dict. The cron store spells the
        # interval family "every".
        expected_kind = {"cron": "cron", "at": "at", "interval": "every"}[family]
        allowed_kinds = {
            "cron": {"cron"},
            "interval": {"every", "interval"},
            "at": {"at", "once"},
        }[family]
        if kind and kind not in allowed_kinds:
            scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
            return None
        kind = expected_kind
    payload: dict[str, Any] | None = None
    if kind == "cron" and cron_expr and croniter.is_valid(cron_expr):
        payload = {"name": name_clean, "message": message_clean, "cron_expr": cron_expr}
    if kind in ("every", "interval") and every_secs is not None:
        payload = {"name": name_clean, "message": message_clean, "every_secs": every_secs}
    if kind in ("at", "once") and at_ts is not None:
        payload = {"name": name_clean, "message": message_clean, "at_ts": at_ts}
    if payload is not None:
        if timezone_name:
            payload["timezone"] = timezone_name
        return payload
    scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
    return None


def _add_json_schedules(scan: _Scan, paths: list[Path], anchor: Path) -> None:
    for path in paths:
        data = _read_json(path, anchor, scan, "schedules", json5=path.suffix == ".json5")
        records: Any = data
        if isinstance(data, dict):
            records = data.get("jobs", data.get("schedules", data.get("crons", [])))
        if not isinstance(records, list):
            scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
            continue
        for record in records[:_MAX_SCHEDULES]:
            payload = _schedule_from_record(record, scan)
            if payload is not None:
                key = json.dumps(payload, sort_keys=True)
                scan.add("schedules", key, payload)


def _diagnose_unsupported_config(scan: _Scan, configs: list[dict[str, Any]]) -> None:
    for config in configs:
        if _count_secret_fields(config):
            scan.diagnostic("credentials", "credential_fields_excluded")
        if any(key in config for key in ("hooks", "hook", "lifecycle_hooks")):
            scan.diagnostic("hooks", "unsupported_category", unsupported=True)
        if any(key in config for key in ("agents", "personas", "profiles")):
            scan.diagnostic("agents", "unsupported_category", unsupported=True)
        if any(
            key in config for key in ("instructions", "system_prompt", "systemPrompt", "prompt")
        ):
            scan.diagnostic("instructions", "unsupported_category", unsupported=True)
        if any(
            key in config
            for key in (
                "approval_policy",
                "permissions",
                "sandbox",
                "security",
                "governance",
                "yolo",
            )
        ):
            scan.diagnostic("settings", "security_setting_excluded")


def _deduplicate_items(scan: _Scan) -> None:
    for category in CATEGORY_IDS:
        items = scan.items[category]
        unique: list[_Item] = []
        seen: set[str] = set()
        for item in items:
            if item.fingerprint in seen:
                continue
            seen.add(item.fingerprint)
            unique.append(item)
        scan.items[category] = unique


def _source_summary(scan: _Scan, *, display_name: str) -> dict[str, Any]:
    categories = [
        {
            "id": category,
            "label": _CATEGORY_LABELS[category],
            "count": len(scan.items[category]),
            "selected": True,
        }
        for category in CATEGORY_IDS
        if scan.items[category]
    ]
    summary = {
        "id": scan.source_id,
        # `display_name` is REQUIRED, and deliberately: the caller already holds
        # the resolved registry, so a fallback that looked the name up again would
        # be a SECOND read of a snapshot that may have changed — a split-read.
        # The plan is the authority; this function is handed the answer.
        "name": display_name,
        "root": str(scan.root),
        "user_home": str(scan.user_home),
        "categories": categories,
    }
    if scan.config_paths:
        summary["_config_paths"] = [str(path) for path in scan.config_paths]
    if scan.workspace_paths:
        summary["_workspace_paths"] = [str(path) for path in scan.workspace_paths]
    return summary


def _plan_from_scans(
    scans: list[_Scan],
    unknown: list[str],
    display_names: Mapping[str, str],
) -> dict[str, Any]:
    """The plan document for one preview's scans.

    *display_names* comes from the SAME registry snapshot the scans were
    dispatched under, so the plan carries the resolved names and no consumer has
    to look a source up again. *unknown* are requested ids that snapshot does not
    contain; each is reported as an ``unknown_source`` skip after every scan's
    own diagnostics.
    """
    skipped = [diagnostic for scan in scans for diagnostic in scan.skipped]
    skipped.extend(
        {
            "source_id": source_id,
            "category_id": "",
            "reason": "unknown_source",
        }
        for source_id in unknown
    )
    sources = [_source_summary(scan, display_name=display_names[scan.source_id]) for scan in scans]
    selection = [
        {"source_id": source["id"], "category_id": category["id"]}
        for source in sources
        for category in source["categories"]
    ]
    return {
        "version": _PLAN_VERSION,
        "sources": sources,
        "detected_count": len(sources),
        "selection": selection,
        "skipped": skipped,
        "secret_count": sum(scan.secret_count for scan in scans),
        "unsupported_count": sum(scan.unsupported_count for scan in scans),
    }


def _merge_missing(destination: dict[str, Any], incoming: dict[str, Any]) -> bool:
    changed = False
    for key, value in incoming.items():
        if key not in destination:
            destination[key] = value
            changed = True
        elif isinstance(destination[key], dict) and isinstance(value, dict):
            changed = _merge_missing(destination[key], value) or changed
    return changed


def _plan_source_ids(plan: dict[str, Any]) -> frozenset[str]:
    """Source ids the PLAN itself contains.

    The plan is the authority for everything downstream of it. It was produced by
    a preview that already validated every id against one registry snapshot, so
    re-filtering against a fresh fail-closed read would let a transient adapter
    failure between preview and apply silently drop a source the user selected —
    apply would report success having imported nothing for it.
    """
    sources = plan.get("sources")
    if not isinstance(sources, list):
        return frozenset()
    return frozenset(
        source["id"]
        for source in sources
        if isinstance(source, dict) and isinstance(source.get("id"), str) and source["id"]
    )


def _selected_pairs(plan: dict[str, Any]) -> set[tuple[str, str]]:
    # The only producers of plan["selection"] (the backend _preview and the API
    # handler's _select_fresh_plan) always emit the canonical list of
    # {"source_id", "category_id"} dicts, so that is the sole shape parsed here.
    # The source-id/CATEGORY_IDS filter is a real guard and is retained.
    selected: set[tuple[str, str]] = set()
    selection = plan.get("selection")
    if not isinstance(selection, list):
        return selected
    for item in selection:
        if not isinstance(item, dict):
            continue
        source_id = item.get("source_id")
        category = item.get("category_id")
        if isinstance(source_id, str) and isinstance(category, str):
            selected.add((source_id, category))
    known = _plan_source_ids(plan)
    return {pair for pair in selected if pair[0] in known and pair[1] in CATEGORY_IDS}


def _plan_roots(plan: dict[str, Any]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    sources = plan.get("sources")
    if not isinstance(sources, list):
        return roots
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_id = source.get("id")
        root = source.get("root")
        if isinstance(source_id, str) and source_id and isinstance(root, str) and root:
            roots[str(source_id)] = Path(root)
    return roots


def _plan_user_homes(plan: dict[str, Any]) -> dict[str, Path]:
    homes: dict[str, Path] = {}
    sources = plan.get("sources")
    if not isinstance(sources, list):
        return homes
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_id = source.get("id")
        user_home = source.get("user_home")
        if isinstance(source_id, str) and source_id and isinstance(user_home, str) and user_home:
            homes[str(source_id)] = Path(user_home)
    return homes


def _plan_private_paths(
    plan: dict[str, Any],
    key: str,
) -> dict[str, tuple[Path, ...]]:
    paths: dict[str, tuple[Path, ...]] = {}
    sources = plan.get("sources")
    if not isinstance(sources, list):
        return paths
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_id = source.get("id")
        values = source.get(key)
        if not isinstance(source_id, str) or not source_id or not isinstance(values, list):
            continue
        paths[str(source_id)] = tuple(Path(value) for value in values if isinstance(value, str))
    return paths
