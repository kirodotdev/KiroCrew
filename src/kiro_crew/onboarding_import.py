"""Conservative import of user-owned data from other local agent tools."""

from __future__ import annotations

import hashlib
import heapq
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

try:
    import tomllib as _toml
except ImportError:  # pragma: no cover - Python 3.9/3.10 compatibility
    try:
        import tomli as _toml  # type: ignore[no-redef,import-not-found]
    except ImportError:
        _toml = None  # type: ignore[assignment]

import yaml  # type: ignore[import-untyped]
from croniter import croniter  # type: ignore[import-untyped]

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.embeddings import make_sync_embed_fn
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.security import (
    contains_injection,
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)


class _NoAliasSafeLoader(yaml.SafeLoader):
    """SafeLoader that refuses YAML anchors/aliases.

    Foreign-agent config files are untrusted. Plain ``yaml.safe_load`` still
    expands ``*alias`` references into a shared-reference graph, so a tiny
    "billion-laughs" config would explode when the downstream secret/leaf
    traversal re-walks it — a DoS the previous line-based parser was immune to
    (it stored ``&a``/``*a`` as literal strings). Rejecting aliases at compose
    time keeps the amplification vector closed while preserving full
    indentation support. A lone anchor with no alias is harmless (nothing to
    amplify) and is allowed.
    """

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.events.AliasEvent):
            event = self.get_event()
            raise yaml.composer.ComposerError(
                None, None, "found alias, which is not allowed", event.start_mark
            )
        return super().compose_node(parent, index)


SOURCE_IDS = ("codex", "claude_code", "meshclaw", "openclaw", "hermes")
CATEGORY_IDS = (
    "sessions",
    "memories",
    "workspaces",
    "mcp_servers",
    "skills",
    "schedules",
    "settings",
)

_SOURCE_NAMES = {
    "codex": "Codex",
    "claude_code": "Claude Code",
    "meshclaw": "MeshClaw",
    "openclaw": "OpenClaw",
    "hermes": "Hermes Agent",
}
_CATEGORY_LABELS = {
    "sessions": "Sessions",
    "memories": "Memories",
    "workspaces": "Workspaces",
    "mcp_servers": "MCP servers",
    "skills": "Skills",
    "schedules": "Schedules",
    "settings": "Settings",
}
_SOURCE_ROOTS = {
    "codex": (("CODEX_HOME",), ".codex"),
    "claude_code": (("CLAUDE_CONFIG_DIR", "CLAUDE_HOME"), ".claude"),
    "meshclaw": (("MESHCLAW_HOME",), ".meshclaw"),
    "hermes": (("HERMES_HOME", "HERMES_AGENT_HOME", "HERMES_CONFIG_DIR"), ".hermes"),
}
_OPENCLAW_LEGACY_ROOTS = (".clawdbot",)
_CLAUDE_RUNTIME_PARTS = frozenset({"subagents", "subagent", "runtime", "tool-results"})
_OPENCLAW_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_OPENCLAW_CREATED_VIA = frozenset({"operator", "channel", "talk"})
_OPENCLAW_RUNTIME_NAMESPACES = frozenset(
    {
        "cron",
        "subagent",
        "acp",
        "acp-bridge",
        "hook",
        "node",
        "heartbeat",
        "internal-session-effects",
    }
)
_OPENCLAW_SESSION_OWNERSHIP_FIELDS = frozenset(
    {
        "completionownersessionkey",
        "forkedfromparent",
        "forksource",
        "pluginownerid",
    }
)
_OPENCLAW_CHECKPOINT_RE = re.compile(
    r"\.checkpoint\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-" r"[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$",
    re.IGNORECASE,
)
_HERMES_RUNTIME_SESSION_SOURCES = frozenset({"subagent", "tool", "cron"})
_HERMES_SKILL_EXCLUDED_PARTS = frozenset(
    {".archive", ".hub", "dependency", "dependencies", "cache", ".cache"}
)
_LEDGER_VERSION = 1
_PLAN_VERSION = 1
_LEDGER_RELATIVE_PATH = Path("imports") / "foreign-agent-imports.json"
_MAX_FILES = 500
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_SKILL_BYTES = 256 * 1024
_MAX_YAML_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_JSONL_LINES = 10_000
_MAX_LINE_BYTES = 256 * 1024
_MAX_MESSAGES_PER_SESSION = 1_000
_MAX_TEXT_CHARS = 100_000
_MAX_DB_BYTES = 64 * 1024 * 1024
_MAX_DB_ROWS = 10_000
_MAX_WALK_ENTRIES = 10_000
_MAX_WORKSPACES = 500
_MAX_MCP_SERVERS = 200
_MAX_SCHEDULES = 500
_MAX_SKILL_PACKAGE_BYTES = 1024 * 1024
_SQLITE_TABLE_NAMES_QUERY = "SELECT name FROM sqlite_schema WHERE type='table'"
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|token|secret|password|credential|authorization|headers?|env)",
    re.IGNORECASE,
)
_SENSITIVE_ARG_RE = re.compile(
    r"(?:--?(?:api[_-]?key|token|secret|password|credential|header|env)"
    r"|authorization\s*:|^[A-Za-z_][A-Za-z0-9_]*=)",
    re.IGNORECASE,
)
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
_SAFE_THEME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_SEMANTIC_KEY_RE = re.compile(r"^[a-z][a-z0-9_.]*[a-z0-9]$")
_SEMANTIC_PREFIXES = ("pref.", "project.", "user.", "lesson.")
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
_HERMES_SCHEDULE_RUNTIME_FIELDS = frozenset(
    {
        "id",
        "enabled",
        "created_at",
        "updated_at",
        "last_run_at",
        "next_run_at",
        "last_error",
        "last_result",
        "last_status",
        "last_delivery_error",
        "status",
        "run_count",
        "schedule_display",
        "state",
        "paused_at",
        "paused_reason",
    }
)
_HERMES_INERT_SCHEDULE_FIELDS = frozenset(
    {
        "skills",
        "skill",
        "model",
        "provider",
        "provider_snapshot",
        "model_snapshot",
        "base_url",
        "script",
        "context_from",
        "enabled_toolsets",
        "workdir",
    }
)
_HERMES_SCHEDULE_FIELDS = (
    frozenset(
        {
            "name",
            "prompt",
            "schedule",
            "timezone",
            "repeat",
            "origin",
            "deliver",
            "no_agent",
        }
    )
    | _HERMES_SCHEDULE_RUNTIME_FIELDS
    | _HERMES_INERT_SCHEDULE_FIELDS
)
_MANAGED_MCP_NAMES = frozenset(
    {
        "kirocrew-core",
        "kirocrew-cron",
        "meshclaw-core",
        "meshclaw-cron",
        "openclaw-core",
        "openclaw-cron",
    }
)
_VISIBLE_ROLES = frozenset({"user", "assistant"})
_VISIBLE_TEXT_TYPES = frozenset(
    {"text", "input_text", "output_text", "user_message", "assistant_message"}
)
_NON_TEXT_TYPES = frozenset(
    {
        "thinking",
        "reasoning",
        "tool",
        "tool_call",
        "tool_use",
        "tool_result",
        "function_call",
        "function_result",
        "computer_initialize_state",
    }
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


def _stat_is_link_like(file_stat: Any) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(file_stat.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _is_link_like(path: Path, file_stat: Any | None = None) -> bool:
    if file_stat is None:
        try:
            file_stat = path.lstat()
        except OSError:
            return False
    return _stat_is_link_like(file_stat)


def _home_from(home: Path | None, env: Mapping[str, str]) -> Path:
    if home is not None:
        return Path(home)
    home_keys = ("USERPROFILE", "HOME") if platform_compat.IS_WINDOWS else ("HOME", "USERPROFILE")
    for key in home_keys:
        value = env.get(key, "").strip()
        if value:
            return Path(value)
    drive = env.get("HOMEDRIVE", "")
    tail = env.get("HOMEPATH", "")
    if drive and tail:
        return Path(drive + tail)
    return Path.home()


def _expand_root(raw: str, home: Path) -> Path:
    if raw == "~":
        return home
    if raw.startswith("~/") or raw.startswith("~\\"):
        return home / raw[2:]
    return Path(raw)


def _openclaw_profile(env: Mapping[str, str]) -> str:
    profile = env.get("OPENCLAW_PROFILE", "").strip().casefold()
    if profile == "default" or not _OPENCLAW_PROFILE_RE.fullmatch(profile):
        return ""
    return profile


def _source_roots(
    home: Path | None,
    env: Mapping[str, str] | None,
) -> tuple[Path, dict[str, Path]]:
    env_map = os.environ if env is None else env
    base_home = _home_from(home, env_map)
    roots: dict[str, Path] = {}
    for source_id in SOURCE_IDS:
        if source_id == "openclaw":
            state_override = env_map.get("OPENCLAW_STATE_DIR", "").strip()
            openclaw_home = env_map.get("OPENCLAW_HOME", "").strip()
            profile = _openclaw_profile(env_map)
            state_name = f".openclaw-{profile}" if profile else ".openclaw"
            if state_override:
                roots[source_id] = _expand_root(state_override, base_home)
                continue
            if openclaw_home:
                roots[source_id] = _expand_root(openclaw_home, base_home) / state_name
                continue
            candidates = [base_home / state_name]
            if not profile:
                candidates.extend(base_home / name for name in _OPENCLAW_LEGACY_ROOTS)
            roots[source_id] = next(
                (candidate for candidate in candidates if candidate.exists()),
                candidates[0],
            )
            continue
        env_names, default_name = _SOURCE_ROOTS[source_id]
        override = next(
            (env_map.get(name, "").strip() for name in env_names if env_map.get(name, "").strip()),
            "",
        )
        if override:
            roots[source_id] = _expand_root(override, base_home)
            continue
        if source_id == "hermes":
            local_app_data = env_map.get("LOCALAPPDATA", "").strip()
            windows_root = Path(local_app_data) / "hermes" if local_app_data else None
            if windows_root is not None and windows_root.exists():
                roots[source_id] = windows_root
                continue
        roots[source_id] = base_home / default_name
    return base_home, roots


def _openclaw_context(
    root: Path,
    home: Path,
    env: Mapping[str, str],
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    config_candidates: list[Path] = []
    explicit_config = env.get("OPENCLAW_CONFIG_PATH", "").strip()
    if explicit_config:
        config_candidates.append(_expand_root(explicit_config, home))
    config_candidates.append(root / "openclaw.json")
    if root == home / ".clawdbot":
        config_candidates.append(root / "clawdbot.json")
    config_paths: list[Path] = []
    seen: set[str] = set()
    for path in config_candidates:
        marker = os.path.normcase(os.path.abspath(str(path)))
        if marker not in seen:
            seen.add(marker)
            config_paths.append(path)

    workspace_paths: list[Path] = []
    workspace_override = env.get("OPENCLAW_WORKSPACE_DIR", "").strip()
    if workspace_override:
        workspace_paths.append(_expand_root(workspace_override, home))
    profile = _openclaw_profile(env)
    if profile:
        workspace_paths.append(home / ".openclaw" / f"workspace-{profile}")
    if (root / "workspace").is_dir():
        workspace_paths.append(root / "workspace")
    if (root / "workspace-main").is_dir():
        workspace_paths.append(root / "workspace-main")
    return tuple(config_paths), tuple(workspace_paths)


def _source_exists(source_id: str, root: Path) -> bool:
    if _is_link_like(root):
        return False
    if root.is_dir():
        return True
    if source_id == "claude_code":
        global_config = root.parent / ".claude.json"
        return global_config.is_file() and not _is_link_like(global_config)
    return False


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
    if not base.exists():
        return []
    if _is_link_like(base):
        scan.diagnostic(category, "symlink_rejected")
        return []
    if not base.is_dir():
        return []
    remaining = max(0, _MAX_FILES - scan.files_seen.get(category, 0)) if count_files else _MAX_FILES
    candidates: list[Path] = []
    session_candidates: list[tuple[float, str, Path]] = []
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
        for dirname in sorted(dirnames, reverse=category == "sessions"):
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
        for index, filename in enumerate(sorted(filenames, reverse=category == "sessions")):
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
            if category == "sessions":
                try:
                    candidate_mtime = candidate.stat().st_mtime
                except OSError:
                    candidate_mtime = 0
                entry = (candidate_mtime, str(candidate), candidate)
                if len(session_candidates) < remaining:
                    heapq.heappush(session_candidates, entry)
                elif remaining and entry > session_candidates[0]:
                    heapq.heapreplace(session_candidates, entry)
                else:
                    omitted += 1
            else:
                if len(candidates) < remaining:
                    candidates.append(candidate)
                else:
                    omitted += 1
        if exhausted:
            break
    if category == "sessions":
        candidates = [entry[2] for entry in session_candidates]
    if excluded_count and excluded_category and excluded_reason:
        scan.diagnostic(excluded_category, excluded_reason, count=excluded_count)
    if category == "sessions":

        def _mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0

        candidates.sort(key=_mtime, reverse=True)
    else:
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
    cleaned, warnings = redact_credentials(bounded)
    scan.secret_count += len(warnings)
    cleaned, url_warnings = redact_exfiltration_urls(cleaned)
    scan.secret_count += len(url_warnings)
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
    except (json.JSONDecodeError, ValueError, RecursionError):
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
        result = yaml.load(text, Loader=_NoAliasSafeLoader)
    except (yaml.YAMLError, RecursionError, ValueError):
        scan.diagnostic("settings", "invalid_config")
        return {}
    return result if isinstance(result, dict) else {}


def _extract_visible_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for block in value:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type", "")).lower()
        if block_type in _NON_TEXT_TYPES:
            continue
        if block_type and block_type not in _VISIBLE_TEXT_TYPES:
            continue
        text = block.get("text")
        if not isinstance(text, str) and block_type in _VISIBLE_TEXT_TYPES:
            text = block.get("content")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(part for part in parts if part)


def _message_from_record(record: Any, scan: _Scan) -> tuple[str, str] | None:
    if not isinstance(record, dict):
        return None
    record_type = str(record.get("type", "")).lower()
    if record_type in _NON_TEXT_TYPES:
        return None
    candidates = [record]
    for key in ("payload", "message"):
        child = record.get(key)
        if isinstance(child, dict):
            candidates.insert(0, child)
    for candidate in candidates:
        candidate_type = str(candidate.get("type", "")).lower()
        if candidate_type in _NON_TEXT_TYPES:
            continue
        role = candidate.get("role")
        if not isinstance(role, str) or role.lower() not in _VISIBLE_ROLES:
            fallback_type = record.get("type")
            role = fallback_type if isinstance(fallback_type, str) else ""
        role = role.lower()
        if role not in _VISIBLE_ROLES:
            continue
        content = candidate.get("content")
        if content is None:
            content = candidate.get("text")
        text = _extract_visible_content(content)
        if not text and isinstance(content, str):
            text = content
        cleaned = _sanitize_text(text, scan) if text else ""
        if cleaned:
            return role, cleaned
    return None


def _claude_record_is_excluded(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("isMeta") is True or record.get("isSidechain") is True:
        return True
    if "toolUseResult" in record:
        return True
    if "userType" in record:
        user_type = record.get("userType")
        return not isinstance(user_type, str) or user_type.casefold() != "external"
    return False


def _record_workspaces(record: Any) -> list[str]:
    if not isinstance(record, dict):
        return []
    workspaces: list[str] = []
    for container in (record, record.get("payload"), record.get("message")):
        if not isinstance(container, dict):
            continue
        for key in ("cwd", "project", "project_path", "workspace_path", "projectPath"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                workspaces.append(value.strip())
        roots = container.get("workspace_roots")
        if isinstance(roots, list):
            workspaces.extend(
                value.strip() for value in roots if isinstance(value, str) and value.strip()
            )
    return workspaces


def _jsonl_session_items(
    paths: list[Path],
    anchor: Path,
    scan: _Scan,
) -> tuple[list[_Item], set[str]]:
    items: list[_Item] = []
    workspaces: set[str] = set()
    for path in paths:
        content = _read_bytes(path, anchor, scan, "sessions")
        if content is None:
            continue
        groups: dict[str, list[tuple[str, str]]] = {}
        file_workspaces: set[str] = set()
        incomplete_file = False
        capped_groups: set[str] = set()
        for line_number, raw_line in enumerate(content.splitlines()):
            if line_number >= _MAX_JSONL_LINES:
                scan.diagnostic("sessions", "line_count_limit")
                incomplete_file = True
                break
            if not raw_line.strip():
                continue
            if len(raw_line) > _MAX_LINE_BYTES:
                scan.diagnostic("sessions", "line_too_large")
                incomplete_file = True
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                scan.diagnostic("sessions", "invalid_jsonl_record")
                incomplete_file = True
                continue
            if scan.source_id == "claude_code" and _claude_record_is_excluded(record):
                continue
            for record_workspace in _record_workspaces(record):
                if len(workspaces) + len(file_workspaces) >= _MAX_WORKSPACES:
                    break
                file_workspaces.add(record_workspace)
            message = _message_from_record(record, scan)
            if message is None:
                continue
            group_value = ""
            if isinstance(record, dict):
                for key in ("session_id", "sessionId", "conversation_id", "conversationId"):
                    if isinstance(record.get(key), (str, int)):
                        group_value = str(record[key])
                        break
            group = group_value or "file"
            messages = groups.setdefault(group, [])
            if len(messages) < _MAX_MESSAGES_PER_SESSION:
                messages.append(message)
            else:
                scan.diagnostic("sessions", "message_count_limit")
                capped_groups.add(group)
        if incomplete_file:
            continue
        workspaces.update(file_workspaces)
        try:
            relative = str(path.relative_to(anchor))
        except ValueError:
            relative = path.name
        for group, messages in groups.items():
            if not messages or group in capped_groups:
                continue
            key = f"session\0{group}" if group != "file" else f"file\0{relative}"
            items.append(_Item(scan.source_id, "sessions", key, messages))
    return items, workspaces


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


def _safe_mcp_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    name = mcp_server_alias(value.strip())
    if (
        not name
        or len(name) > 128
        or name.casefold() in _MANAGED_MCP_NAMES
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
                name = _safe_mcp_name(raw_name)
                if not name:
                    if isinstance(raw_name, str) and raw_name in _MANAGED_MCP_NAMES:
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
        screened, credential_warnings = redact_credentials(text)
        screened, url_warnings = redact_exfiltration_urls(screened)
        scan.secret_count += len(credential_warnings) + len(url_warnings)
        if credential_warnings or url_warnings or screened != text:
            scan.diagnostic("skills", "credential_bearing_skill")
            return None
        if path.name == "SKILL.md":
            metadata, _ = _frontmatter(text)
            always = metadata.get("always", "").casefold() in {"1", "true", "yes"}
            if always or "triggers" in metadata:
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
        if marker in seen or not path.is_file():
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


def _named_descendant_dirs(
    base: Path,
    scan: _Scan,
    category: str,
    names: frozenset[str],
) -> list[Path]:
    if not base.exists() or not base.is_dir():
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
        expected_kind = "cron" if family == "cron" else "at" if family == "at" else "every"
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


def _hermes_schedule_has_unsupported_semantics(record: dict[str, Any]) -> bool:
    fields = set(record)
    if fields - _HERMES_SCHEDULE_FIELDS:
        return True
    if any(key.casefold().replace("_", "").startswith(("claim", "execution")) for key in fields):
        return True
    if any(
        record.get(key) not in (None, "", [], {}) for key in fields & _HERMES_INERT_SCHEDULE_FIELDS
    ):
        return True
    if "no_agent" in record and record["no_agent"] is not False:
        return True
    repeat = record.get("repeat")
    if repeat is not None:
        schedule = record.get("schedule")
        raw_kind = schedule.get("kind", "") if isinstance(schedule, dict) else ""
        kind = raw_kind.casefold() if isinstance(raw_kind, str) else ""
        expected_times = 1 if kind == "once" else None
        if repeat != {"times": expected_times, "completed": 0}:
            return True
    origin = record.get("origin")
    if origin not in (None, ""):
        return True
    deliver = record.get("deliver")
    if isinstance(deliver, str):
        if deliver.casefold() not in ("", "local"):
            return True
    elif isinstance(deliver, dict):
        if set(deliver) - {"mode"} or str(deliver.get("mode", "")).casefold() != "local":
            return True
    elif deliver is not None:
        return True
    return False


def _hermes_schedule_from_record(
    record: Any,
    scan: _Scan,
    *,
    default_timezone: str = "",
) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
        return None
    if _hermes_schedule_has_unsupported_semantics(record):
        scan.diagnostic("schedules", "unsupported_schedule_semantics", unsupported=True)
        return None
    name = record.get("name")
    prompt = record.get("prompt")
    schedule = record.get("schedule")
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(prompt, str)
        or not isinstance(schedule, dict)
    ):
        scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
        return None
    kind = schedule.get("kind")
    if not isinstance(kind, str):
        scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
        return None
    kind = kind.casefold()
    allowed_schedule_fields = {
        "cron": {"kind", "expr", "timezone", "display"},
        "interval": {"kind", "minutes", "display"},
        "once": {"kind", "run_at", "timezone", "display"},
    }.get(kind)
    if allowed_schedule_fields is None or set(schedule) - allowed_schedule_fields:
        scan.diagnostic("schedules", "unsupported_schedule_semantics", unsupported=True)
        return None

    timezone_value = schedule.get("timezone", record.get("timezone", default_timezone))
    if kind == "cron" and not timezone_value:
        scan.diagnostic("schedules", "timezone_required", unsupported=True)
        return None
    if kind == "once":
        run_at = schedule.get("run_at")
        if isinstance(run_at, str):
            try:
                parsed = datetime.fromisoformat(run_at.strip().replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is not None and parsed.tzinfo is None and not timezone_value:
                scan.diagnostic("schedules", "timezone_required", unsupported=True)
                return None

    projected_schedule = {key: value for key, value in schedule.items() if key != "display"}
    if timezone_value and "timezone" not in projected_schedule:
        projected_schedule["timezone"] = timezone_value
    projected = {
        "name": name,
        "prompt": prompt,
        "schedule": projected_schedule,
    }
    return _schedule_from_record(projected, scan)


def _add_hermes_json_schedules(
    scan: _Scan,
    paths: list[Path],
    anchor: Path,
    *,
    default_timezone: str = "",
) -> None:
    for path in paths:
        data = _read_json(path, anchor, scan, "schedules")
        records: Any = data.get("jobs", []) if isinstance(data, dict) else data
        if not isinstance(records, list):
            scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
            continue
        for record in records[:_MAX_SCHEDULES]:
            payload = _hermes_schedule_from_record(
                record,
                scan,
                default_timezone=default_timezone,
            )
            if payload is not None:
                scan.add("schedules", json.dumps(payload, sort_keys=True), payload)


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    metadata: dict[str, str] = {}
    end = 0
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = index
            break
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip("\"'")
    if not end:
        return {}, text
    return metadata, "\n".join(lines[end + 1 :]).strip()


def _parse_configs(
    scan: _Scan,
    configs: list[tuple[Path, Path, str]],
) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path, anchor, kind in configs:
        marker = os.path.normcase(os.path.abspath(str(path)))
        if marker in seen or not path.exists():
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


def _add_sessions_and_workspaces(
    scan: _Scan,
    paths: list[Path],
    anchor: Path,
) -> set[str]:
    items, workspaces = _jsonl_session_items(paths, anchor, scan)
    scan.items["sessions"].extend(items)
    accepted: set[str] = set()
    for workspace in sorted(workspaces)[:_MAX_WORKSPACES]:
        canonical = _workspace_item(scan, workspace)
        if canonical:
            accepted.add(canonical)
    return accepted


def _without_runtime_sessions(scan: _Scan, paths: list[Path], anchor: Path) -> list[Path]:
    accepted: list[Path] = []
    excluded = 0
    for path in paths:
        try:
            parts = {part.casefold() for part in path.relative_to(anchor).parts}
        except ValueError:
            parts = set()
        if parts & {"subagents", "subagent", "runtime", "tool-results"}:
            excluded += 1
            continue
        accepted.append(path)
    if excluded:
        scan.diagnostic("runtime", "runtime_sessions_excluded", count=excluded)
    return accepted


def _scan_codex_automations(scan: _Scan) -> None:
    path = scan.root / "sqlite" / "codex-dev.db"
    if not path.is_file():
        return
    with _open_snapshot_db(path, scan.root, scan, "schedules") as connection:
        if connection is None:
            return
        try:
            tables = {
                str(row[0]) for row in connection.execute(_SQLITE_TABLE_NAMES_QUERY).fetchall()
            }
            if "automations" not in tables:
                return
            columns = _sqlite_columns(connection, "automations")
            if "rrule" not in columns:
                scan.diagnostic(
                    "schedules",
                    "unsupported_schedule_database",
                    unsupported=True,
                )
                return
            count = connection.execute(
                'SELECT COUNT(*) FROM "automations" '
                'WHERE "rrule" IS NOT NULL AND TRIM("rrule") <> ""'
            ).fetchone()[0]
            if isinstance(count, int) and count:
                scan.diagnostic(
                    "schedules",
                    "unsupported_schedule_semantics",
                    unsupported=True,
                    count=count,
                )
        except sqlite3.Error:
            scan.diagnostic(
                "schedules",
                "unsupported_schedule_database",
                unsupported=True,
            )


def _scan_codex(scan: _Scan) -> None:
    root = scan.root
    session_paths = _walk_files(root / "sessions", scan, "sessions", suffixes=(".jsonl",))
    session_paths += _walk_files(
        root / "archived_sessions",
        scan,
        "sessions",
        suffixes=(".jsonl",),
    )
    _add_sessions_and_workspaces(scan, session_paths, root)
    configs = _parse_configs(scan, [(root / "config.toml", root, "toml")])
    _diagnose_unsupported_config(scan, configs)
    for config in configs:
        for workspace in _collect_project_paths(config):
            _workspace_item(scan, workspace)
    _add_mcp_configs(scan, configs)
    _add_skills(
        scan,
        [root / "skills"],
        excluded_parts=frozenset({".system"}),
    )
    if any(root.glob("memories*.sqlite*")):
        scan.diagnostic("memories", "unstable_memory_store", unsupported=True)
    if (root / "memories_extensions" / "chronicle").exists():
        scan.diagnostic("memories", "unstable_memory_store", unsupported=True)
    if (root / "hooks.json").exists():
        scan.diagnostic("hooks", "unsupported_category", unsupported=True)
    if (root / "agents").exists():
        scan.diagnostic("agents", "unsupported_category", unsupported=True)
    if (root / "AGENTS.md").exists():
        scan.diagnostic("instructions", "unsupported_category", unsupported=True)
    _scan_codex_automations(scan)
    settings: dict[str, Any] = {}
    for config in configs:
        _merge_missing(settings, _settings_from(config, "codex"))
    if settings:
        scan.add("settings", json.dumps(settings, sort_keys=True), settings)


def _scan_claude(scan: _Scan) -> None:
    root = scan.root
    sessions = _walk_files(
        root / "projects",
        scan,
        "sessions",
        suffixes=(".jsonl",),
        excluded_parts=_CLAUDE_RUNTIME_PARTS,
        excluded_category="runtime",
        excluded_reason="runtime_sessions_excluded",
    )
    workspaces = _add_sessions_and_workspaces(scan, sessions, root)
    project_configs: list[tuple[Path, Path, str]] = []
    for workspace_value in sorted(workspaces):
        workspace_path = Path(workspace_value)
        project_configs.extend(
            [
                (workspace_path / ".claude" / "settings.local.json", workspace_path, "json"),
                (workspace_path / ".claude" / "settings.json", workspace_path, "json"),
                (workspace_path / ".mcp.json", workspace_path, "json"),
            ]
        )
    configs = _parse_configs(
        scan,
        project_configs
        + [
            (root / "settings.local.json", root, "json"),
            (root / "settings.json", root, "json"),
            (root / ".claude.json", root, "json"),
            (root.parent / ".claude.json", root.parent, "json"),
        ],
    )
    _diagnose_unsupported_config(scan, configs)
    for config in configs:
        for configured_workspace in _collect_project_paths(config):
            _workspace_item(scan, configured_workspace)
    _add_mcp_configs(scan, configs)
    skill_roots = [root / "skills"]
    skill_roots.extend(Path(workspace) / ".claude" / "skills" for workspace in workspaces)
    _add_skills(scan, skill_roots)
    memory_roots = [root / "memory"]
    memory_roots += _named_descendant_dirs(
        root / "projects",
        scan,
        "memories",
        frozenset({"memory", "memories"}),
    )
    _add_memories(scan, memory_roots)
    if (root / "tasks").exists():
        scan.diagnostic("runtime", "runtime_state_excluded")
    instruction_count = int((root / "CLAUDE.md").is_file())
    instruction_count += len(
        _walk_files(
            root / "rules",
            scan,
            "instructions",
            suffixes=(".md", ".markdown"),
        )
    )
    instruction_count += sum(
        1 for workspace in workspaces if (Path(workspace) / "CLAUDE.md").is_file()
    )
    if instruction_count:
        scan.diagnostic(
            "instructions",
            "unsupported_category",
            unsupported=True,
            count=instruction_count,
        )
    settings: dict[str, Any] = {}
    for config in configs:
        _merge_missing(settings, _settings_from(config, "claude_code"))
    if settings:
        scan.add("settings", json.dumps(settings, sort_keys=True), settings)


def _scan_meshclaw(scan: _Scan) -> None:
    root = scan.root
    sessions = _walk_files(root / "sessions", scan, "sessions", suffixes=(".jsonl",))
    workspaces = _add_sessions_and_workspaces(scan, sessions, root)
    configs = _parse_configs(
        scan,
        [
            (root / "config.json", root, "json"),
            (root / "mcp.json", root, "json"),
        ],
    )
    _diagnose_unsupported_config(scan, configs)
    recent = root / "recent_projects.json"
    if recent.is_file():
        data = _read_json(recent, root, scan, "workspaces")
        if isinstance(data, list):
            for recent_workspace in data[:_MAX_WORKSPACES]:
                if isinstance(recent_workspace, str):
                    canonical = _workspace_item(scan, recent_workspace)
                    if canonical:
                        workspaces.add(canonical)
    for pointer_name in ("workspace_dir", "project_dir"):
        workspace_file = root / pointer_name
        if workspace_file.is_file():
            workspace_value = _read_text(workspace_file, root, scan, "workspaces")
            if workspace_value:
                canonical = _workspace_item(scan, workspace_value.strip())
                if canonical:
                    workspaces.add(canonical)
    for config in configs:
        for configured_workspace in _collect_project_paths(config):
            canonical = _workspace_item(scan, configured_workspace)
            if canonical:
                workspaces.add(canonical)
    _add_mcp_configs(scan, configs)
    skill_roots = [root / "workspace" / "skills"]
    skill_roots.extend(Path(workspace) / "skills" for workspace in sorted(workspaces))
    _add_skills(scan, skill_roots)
    has_memory_db = _scan_meshclaw_memory_db(scan)
    _add_memories(scan, [root / "workspace" / "memory"])
    if not has_memory_db:
        _add_memories(scan, [root / "memory"])
    schedule_paths = [
        path for path in (root / "crons.json", root / "cron" / "jobs.json") if path.is_file()
    ]
    _add_json_schedules(scan, schedule_paths, root)
    settings: dict[str, Any] = {}
    for config in configs:
        _merge_missing(settings, _settings_from(config, "meshclaw"))
    if settings:
        scan.add("settings", json.dumps(settings, sort_keys=True), settings)


def _openclaw_agent_entries(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    agents = config.get("agents")
    if not isinstance(agents, dict):
        return {}
    entries = agents.get("entries")
    if not isinstance(entries, dict):
        return {}
    return {
        agent_id: entry
        for agent_id, entry in entries.items()
        if isinstance(agent_id, str)
        and agent_id
        and "/" not in agent_id
        and "\\" not in agent_id
        and isinstance(entry, dict)
    }


def _openclaw_workspace_values(config: dict[str, Any]) -> set[str]:
    values = _collect_project_paths(config)
    agents = config.get("agents")
    if isinstance(agents, dict):
        defaults = agents.get("defaults")
        default_workspace = (
            defaults.get("workspace")
            if isinstance(defaults, dict) and isinstance(defaults.get("workspace"), str)
            else ""
        )
        entries = _openclaw_agent_entries(config)
        if entries:
            for agent_id, entry in entries.items():
                workspace = entry.get("workspace")
                if isinstance(workspace, str):
                    values.add(workspace)
                elif default_workspace:
                    values.add(str(Path(default_workspace) / agent_id))
        elif default_workspace:
            values.add(default_workspace)
        configured_agents = agents.get("list")
        if isinstance(configured_agents, list):
            for agent in configured_agents:
                if isinstance(agent, dict) and isinstance(agent.get("workspace"), str):
                    values.add(agent["workspace"])
    profiles = config.get("profiles")
    profile_values: Iterable[Any]
    if isinstance(profiles, dict):
        profile_values = profiles.values()
    elif isinstance(profiles, (list, tuple)):
        profile_values = profiles
    else:
        profile_values = ()
    for profile in profile_values:
        if isinstance(profile, dict) and isinstance(profile.get("workspace"), str):
            values.add(profile["workspace"])
    return values


def _openclaw_agent_dirs(scan: _Scan) -> list[Path]:
    agents_root = scan.root / "agents"
    if not agents_root.is_dir() or _is_link_like(agents_root):
        if _is_link_like(agents_root):
            scan.diagnostic("sessions", "symlink_rejected")
        return []
    children: list[Path] = []
    truncated = False
    try:
        for index, child in enumerate(agents_root.iterdir()):
            if index >= _MAX_FILES:
                truncated = True
                break
            children.append(child)
    except OSError:
        return []
    if truncated:
        scan.diagnostic("sessions", "agent_count_limit", count=1)
    agent_dirs: list[Path] = []
    for child in sorted(children, key=lambda path: path.name.casefold()):
        if _is_link_like(child):
            scan.diagnostic("sessions", "symlink_rejected")
            continue
        if child.is_dir():
            agent_dirs.append(child)
    return agent_dirs


def _openclaw_session_artifact(path: Path) -> bool:
    name = path.name.casefold()
    return (
        name.endswith(".trajectory.jsonl")
        or _OPENCLAW_CHECKPOINT_RE.search(name) is not None
        or ".deleted." in name
        or name.endswith(".deleted.jsonl")
        or ".reset." in name
        or name.endswith(".reset.jsonl")
    )


def _openclaw_registry_map(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    sessions = data.get("sessions")
    if isinstance(sessions, dict):
        return sessions
    return data


def _openclaw_entry_matches_file(entry: dict[str, Any], path: Path) -> bool:
    references: list[Path] = []
    session_id = entry.get("sessionId")
    if isinstance(session_id, str) and session_id:
        references.append(path.parent / f"{session_id}.jsonl")
    session_file = entry.get("sessionFile")
    if isinstance(session_file, str) and session_file:
        referenced_file = Path(session_file)
        references.append(
            referenced_file if referenced_file.is_absolute() else path.parent / referenced_file
        )
    if not references:
        return False
    path_marker = os.path.normcase(os.path.abspath(str(path)))
    return all(
        os.path.normcase(os.path.abspath(str(reference))) == path_marker for reference in references
    )


def _openclaw_session_provenance_is_user_owned(
    session_key: str,
    entry: dict[str, Any],
) -> bool:
    namespaces = {part for part in re.split(r"[:/]", session_key.casefold()) if part}
    if namespaces & _OPENCLAW_RUNTIME_NAMESPACES:
        return False
    created_via = entry.get("createdVia")
    if not isinstance(created_via, str) or created_via.casefold() not in _OPENCLAW_CREATED_VIA:
        return False
    actor = entry.get("createdActor")
    if not isinstance(actor, dict) or str(actor.get("type", "")).casefold() != "human":
        return False
    for key, value in entry.items():
        folded = key.casefold().replace("_", "")
        if (
            folded.startswith("parent")
            or folded.startswith("spawn")
            or folded.startswith("runtime")
            or folded in _OPENCLAW_SESSION_OWNERSHIP_FIELDS
        ) and value not in (None, "", False, [], {}):
            return False
    return True


def _openclaw_session_paths(scan: _Scan, agent_dirs: list[Path]) -> list[Path]:
    accepted: list[Path] = []
    remaining_entries = _MAX_FILES
    for agent_dir in agent_dirs:
        if remaining_entries <= 0:
            break
        sessions_root = agent_dir / "sessions"
        if not sessions_root.is_dir() or _is_link_like(sessions_root):
            if _is_link_like(sessions_root):
                scan.diagnostic("sessions", "symlink_rejected")
            continue
        registry_path = sessions_root / "sessions.json"
        registry: dict[str, Any] = {}
        if registry_path.is_file():
            registry = _openclaw_registry_map(
                _read_json(registry_path, scan.root, scan, "sessions")
            )
        candidates: list[Path] = []
        truncated = False
        try:
            for child in sessions_root.iterdir():
                if remaining_entries <= 0:
                    truncated = True
                    break
                remaining_entries -= 1
                if child.name.casefold().endswith(".jsonl"):
                    candidates.append(child)
        except OSError:
            continue
        if truncated:
            scan.diagnostic("sessions", "file_count_limit", count=1)
        for path in sorted(candidates, key=lambda path: path.name.casefold()):
            if _openclaw_session_artifact(path):
                scan.diagnostic("sessions", "session_artifact_excluded")
                continue
            matches = [
                (session_key, entry)
                for session_key, entry in registry.items()
                if isinstance(session_key, str)
                and isinstance(entry, dict)
                and _openclaw_entry_matches_file(entry, path)
            ]
            if len(matches) != 1:
                scan.diagnostic(
                    "sessions",
                    "session_provenance_missing_or_ambiguous",
                )
                continue
            session_key, entry = matches[0]
            if not _openclaw_session_provenance_is_user_owned(session_key, entry):
                scan.diagnostic("sessions", "session_provenance_rejected")
                continue
            if _safe_regular_file(path, scan.root, scan, "sessions"):
                accepted.append(path)
    if remaining_entries == 0:
        scan.diagnostic("sessions", "file_count_limit", count=1)
    return accepted


def _openclaw_workspace_source(scan: _Scan, raw_path: str | Path) -> Path | None:
    raw_value = str(raw_path)
    path = _expand_root(raw_value, scan.user_home)
    if not path.is_absolute():
        scan.diagnostic("workspaces", "workspace_not_absolute")
        return None
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        scan.diagnostic("workspaces", "workspace_unavailable")
        return None
    if not resolved.is_dir() or is_sensitive_path(str(resolved)):
        return None
    try:
        source_root = scan.root.resolve(strict=True)
    except (OSError, RuntimeError):
        source_root = scan.root.resolve()
    if resolved != source_root and source_root not in resolved.parents:
        canonical = _workspace_item(scan, str(resolved))
        if canonical is None:
            return None
    return resolved


def _diagnose_openclaw_database(
    scan: _Scan,
    path: Path,
    category: str,
    reason: str,
) -> None:
    if not os.path.lexists(path):
        return
    if _sqlite_database_is_safe(path, scan.root, scan, category):
        scan.diagnostic(category, reason, unsupported=True)


def _scan_openclaw(scan: _Scan) -> None:
    root = scan.root
    agent_dirs = _openclaw_agent_dirs(scan)
    sessions = _openclaw_session_paths(scan, agent_dirs)
    _add_sessions_and_workspaces(scan, sessions, root)
    for agent_dir in agent_dirs:
        _diagnose_openclaw_database(
            scan,
            agent_dir / "agent" / "openclaw-agent.sqlite",
            "sessions",
            "unsupported_session_database",
        )
    _diagnose_openclaw_database(
        scan,
        root / "openclaw.sqlite",
        "schedules",
        "unsupported_schedule_database",
    )
    configs = _parse_configs(
        scan,
        [
            (
                path,
                path.parent,
                "json5",
            )
            for path in scan.config_paths
        ],
    )
    _diagnose_unsupported_config(scan, configs)
    workspace_roots: set[Path] = set()
    for workspace_path in scan.workspace_paths:
        resolved = _openclaw_workspace_source(scan, workspace_path)
        if resolved is not None:
            workspace_roots.add(resolved)
    agent_ids = {"main"}
    agent_ids.update(agent_dir.name for agent_dir in agent_dirs)
    for config in configs:
        agent_ids.update(_openclaw_agent_entries(config))
        for configured_workspace in _openclaw_workspace_values(config):
            resolved = _openclaw_workspace_source(scan, configured_workspace)
            if resolved is not None:
                workspace_roots.add(resolved)
    for agent_id in agent_ids:
        default_workspace = root / f"workspace-{agent_id}"
        if not os.path.lexists(default_workspace):
            continue
        resolved = _openclaw_workspace_source(scan, default_workspace)
        if resolved is not None:
            workspace_roots.add(resolved)
    _add_mcp_configs(scan, configs)
    ordered_workspaces = sorted(workspace_roots)
    _add_skills(scan, [workspace / "skills" for workspace in ordered_workspaces])
    _add_memories(scan, [workspace / "memory" for workspace in ordered_workspaces])
    _add_memory_files(
        scan,
        [(workspace / "MEMORY.md", workspace) for workspace in ordered_workspaces],
    )
    if (root / "agents").exists():
        scan.diagnostic("agents", "unsupported_category", unsupported=True)
    schedule_paths = [path for path in (root / "cron" / "jobs.json",) if path.is_file()]
    _add_json_schedules(scan, schedule_paths, root)
    settings: dict[str, Any] = {}
    for config in configs:
        _merge_missing(settings, _settings_from(config, "openclaw"))
    if settings:
        scan.add("settings", json.dumps(settings, sort_keys=True), settings)


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


def _scan_meshclaw_memory_db(scan: _Scan) -> bool:
    path = scan.root / "memory.db"
    if not path.is_file():
        return False
    with _open_snapshot_db(path, scan.root, scan, "memories") as connection:
        if connection is None:
            return True
        try:
            tables = {
                str(row[0]) for row in connection.execute(_SQLITE_TABLE_NAMES_QUERY).fetchall()
            }
            required_columns = {
                "semantic_memory": {"key", "value_json", "confidence", "is_deleted"},
                "episodic_memories": {"id", "text", "importance", "is_deleted"},
            }
            table_columns = {
                table: _sqlite_columns(connection, table)
                for table in required_columns
                if table in tables
            }
            active_rows = 0
            for table, required in required_columns.items():
                if required <= table_columns.get(table, set()):
                    remaining = _MAX_DB_ROWS - active_rows
                    rows = connection.execute(
                        f'SELECT 1 FROM "{table}" WHERE "is_deleted" = 0 LIMIT ?',
                        (remaining + 1,),
                    ).fetchall()
                    active_rows += len(rows)
                    if active_rows > _MAX_DB_ROWS:
                        scan.diagnostic("memories", "row_count_limit")
                        return True
            supported = False
            if "semantic_memory" in tables:
                columns = table_columns["semantic_memory"]
                if {"key", "value_json", "confidence", "is_deleted"} <= columns:
                    supported = True
                    extra_columns = [name for name in ("workspace_id", "kind") if name in columns]
                    selected_columns = ["key", "value_json", "confidence", *extra_columns]
                    rows = connection.execute(
                        "SELECT "
                        + ", ".join(f'"{name}"' for name in selected_columns)
                        + ' FROM "semantic_memory" WHERE "is_deleted" = 0 LIMIT ?',
                        (_MAX_DB_ROWS,),
                    ).fetchall()
                    for row in rows:
                        values = dict(zip(selected_columns, row))
                        key = values["key"]
                        value_json = values["value_json"]
                        confidence = values["confidence"]
                        if values.get("workspace_id") not in (None, ""):
                            scan.diagnostic(
                                "memories",
                                "scoped_memory_unsupported",
                                unsupported=True,
                            )
                            continue
                        if str(values.get("kind", "")).casefold() == "directive":
                            scan.diagnostic(
                                "memories",
                                "directive_memory_unsupported",
                                unsupported=True,
                            )
                            continue
                        if (
                            not isinstance(key, str)
                            or len(key) > 100
                            or not _SEMANTIC_KEY_RE.fullmatch(key)
                            or not key.startswith(_SEMANTIC_PREFIXES)
                            or not isinstance(value_json, str)
                        ):
                            scan.diagnostic("memories", "unsupported_semantic_memory")
                            continue
                        cleaned = _sanitize_text(value_json, scan)
                        if cleaned != value_json.strip():
                            scan.diagnostic("memories", "credential_bearing_memory")
                            continue
                        if contains_injection(cleaned):
                            scan.diagnostic("memories", "injection_memory_excluded")
                            continue
                        try:
                            value = json.loads(value_json)
                        except (json.JSONDecodeError, RecursionError):
                            scan.diagnostic("memories", "invalid_memory_record")
                            continue
                        if _count_secret_fields(value):
                            scan.diagnostic("memories", "secret_fields_omitted")
                            continue
                        numeric_confidence = (
                            float(confidence)
                            if isinstance(confidence, (int, float))
                            and not isinstance(confidence, bool)
                            else 0.9
                        )
                        payload = {
                            "kind": "semantic",
                            "key": key,
                            "value": value,
                            "confidence": max(0.8, min(1.0, numeric_confidence)),
                        }
                        scan.add("memories", f"sqlite\0semantic\0{key}", payload)
                else:
                    scan.diagnostic(
                        "memories",
                        "unsupported_memory_database_schema",
                        unsupported=True,
                    )
            if "episodic_memories" in tables:
                columns = table_columns["episodic_memories"]
                if {"id", "text", "importance", "is_deleted"} <= columns:
                    supported = True
                    extra_columns = [name for name in ("workspace_id", "kind") if name in columns]
                    selected_columns = ["id", "text", "importance", *extra_columns]
                    rows = connection.execute(
                        "SELECT "
                        + ", ".join(f'"{name}"' for name in selected_columns)
                        + ' FROM "episodic_memories" WHERE "is_deleted" = 0 LIMIT ?',
                        (_MAX_DB_ROWS,),
                    ).fetchall()
                    for row in rows:
                        values = dict(zip(selected_columns, row))
                        memory_id = values["id"]
                        text = values["text"]
                        importance = values["importance"]
                        if values.get("workspace_id") not in (None, ""):
                            scan.diagnostic(
                                "memories",
                                "scoped_memory_unsupported",
                                unsupported=True,
                            )
                            continue
                        if str(values.get("kind", "")).casefold() == "directive":
                            scan.diagnostic(
                                "memories",
                                "directive_memory_unsupported",
                                unsupported=True,
                            )
                            continue
                        if not isinstance(text, str):
                            scan.diagnostic("memories", "invalid_memory_record")
                            continue
                        cleaned = _sanitize_text(text, scan)
                        if cleaned != text.strip():
                            scan.diagnostic("memories", "credential_bearing_memory")
                            continue
                        if contains_injection(cleaned):
                            scan.diagnostic("memories", "injection_memory_excluded")
                            continue
                        if not 10 <= len(cleaned) <= 2000:
                            scan.diagnostic("memories", "unsupported_memory_length")
                            continue
                        numeric_importance = (
                            float(importance)
                            if isinstance(importance, (int, float))
                            and not isinstance(importance, bool)
                            else 0.5
                        )
                        payload = {
                            "kind": "episodic",
                            "text": cleaned,
                            "importance": max(0.0, min(1.0, numeric_importance)),
                        }
                        scan.add("memories", f"sqlite\0episodic\0{memory_id}", payload)
                else:
                    scan.diagnostic(
                        "memories",
                        "unsupported_memory_database_schema",
                        unsupported=True,
                    )
            if not supported:
                scan.diagnostic(
                    "memories",
                    "unsupported_memory_database_schema",
                    unsupported=True,
                )
        except sqlite3.Error:
            scan.diagnostic(
                "memories",
                "unsupported_memory_database_schema",
                unsupported=True,
            )
    return True


def _sqlite_visible_text(content: str) -> str:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content
    except RecursionError:
        return ""
    if isinstance(parsed, str):
        return parsed
    if isinstance(parsed, list):
        return _extract_visible_content(parsed)
    if isinstance(parsed, dict):
        block_type = str(parsed.get("type", "")).lower()
        if block_type in _NON_TEXT_TYPES:
            return ""
        if block_type in _VISIBLE_TEXT_TYPES:
            return _extract_visible_content([parsed])
        value = parsed.get("content")
        if isinstance(value, (str, list)):
            return _extract_visible_content(value) if isinstance(value, list) else value
    return ""


def _sqlite_workspace_values(
    connection: sqlite3.Connection,
    table: str,
    columns: set[str],
    candidates: tuple[str, ...],
    scan: _Scan,
) -> None:
    selected = [name for name in candidates if name in columns]
    for column in selected:
        rows = connection.execute(
            f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT ?',
            (_MAX_WORKSPACES,),
        ).fetchall()
        for (workspace,) in rows:
            if isinstance(workspace, str):
                _workspace_item(scan, workspace)


def _scan_hermes_db(scan: _Scan, root: Path) -> None:
    candidates = [
        path
        for path in (root / "state.db", root / "hermes.db", root / "sessions.db")
        if path.is_file()
    ]
    if not candidates:
        return
    path = candidates[0]
    with _open_snapshot_db(path, root, scan, "sessions") as connection:
        if connection is None:
            return
        try:
            tables = {
                str(row[0]) for row in connection.execute(_SQLITE_TABLE_NAMES_QUERY).fetchall()
            }
            if "messages" not in tables:
                scan.diagnostic("sessions", "unsupported_database_schema", unsupported=True)
                return
            message_columns = _sqlite_columns(connection, "messages")
            if not {"session_id", "role", "content"} <= message_columns:
                scan.diagnostic("sessions", "missing_session_provenance", unsupported=True)
                return
            if "sessions" not in tables:
                scan.diagnostic("sessions", "missing_session_provenance", unsupported=True)
                return
            session_columns = _sqlite_columns(connection, "sessions")
            if not {"id", "source", "parent_session_id"} <= session_columns:
                scan.diagnostic("sessions", "missing_session_provenance", unsupported=True)
                return

            workspace_columns = [
                name
                for name in ("cwd", "git_repo_root", "project_path", "workdir", "workspace")
                if name in session_columns
            ]
            selected_session_columns = ["id", "source", "parent_session_id", *workspace_columns]
            connection.execute("BEGIN")
            session_rows = connection.execute(
                "SELECT "
                + ", ".join(f'"{name}"' for name in selected_session_columns)
                + ' FROM "sessions" ORDER BY "id" LIMIT ?',
                (_MAX_DB_ROWS + 1,),
            ).fetchall()
            if len(session_rows) > _MAX_DB_ROWS:
                scan.diagnostic("sessions", "row_count_limit")
                return

            filters = ['"session_id" IS ?']
            if "active" in message_columns:
                filters.append('"active" = 1')
            where = " WHERE " + " AND ".join(filters)
            ordering = [
                name for name in ("timestamp", "created_at", "id") if name in message_columns
            ]
            order_by = (
                " ORDER BY " + ", ".join(f'"{name}"' for name in ordering) if ordering else ""
            )
            remaining_rows = _MAX_DB_ROWS
            for row in session_rows:
                values = dict(zip(selected_session_columns, row))
                session_id = values["id"]
                source = values["source"]
                parent_session_id = values["parent_session_id"]
                if not isinstance(source, str) or not source.strip():
                    scan.diagnostic("sessions", "runtime_session_excluded")
                    continue
                if source.strip().casefold() in _HERMES_RUNTIME_SESSION_SOURCES:
                    scan.diagnostic("sessions", "runtime_session_excluded")
                    continue
                if parent_session_id is not None:
                    scan.diagnostic("sessions", "parented_session_excluded")
                    continue

                for column in workspace_columns:
                    workspace = values.get(column)
                    if isinstance(workspace, str):
                        _workspace_item(scan, workspace)

                rows = connection.execute(
                    f'SELECT "role", "content" FROM "messages"{where}{order_by} LIMIT ?',
                    (session_id, remaining_rows + 1),
                ).fetchall()
                if len(rows) > remaining_rows:
                    scan.diagnostic("sessions", "row_count_limit")
                    continue
                remaining_rows -= len(rows)
                messages: list[tuple[str, str]] = []
                capped = False
                for role, content in rows:
                    if not isinstance(role, str) or role.casefold() not in _VISIBLE_ROLES:
                        continue
                    if not isinstance(content, str):
                        continue
                    cleaned = _sanitize_text(_sqlite_visible_text(content), scan)
                    if not cleaned:
                        continue
                    if len(messages) >= _MAX_MESSAGES_PER_SESSION:
                        capped = True
                        continue
                    messages.append((role.casefold(), cleaned))
                if capped:
                    scan.diagnostic("sessions", "message_count_limit")
                    continue
                if messages:
                    scan.add("sessions", f"sqlite\0{session_id}", messages)
        except sqlite3.Error:
            scan.diagnostic("sessions", "unsupported_database_schema", unsupported=True)


def _scan_hermes_projects_db(scan: _Scan, root: Path) -> None:
    path = root / "projects.db"
    if not path.is_file():
        return
    with _open_snapshot_db(path, root, scan, "workspaces") as connection:
        if connection is None:
            return
        try:
            tables = {
                str(row[0]) for row in connection.execute(_SQLITE_TABLE_NAMES_QUERY).fetchall()
            }
            if "projects" in tables:
                _sqlite_workspace_values(
                    connection,
                    "projects",
                    _sqlite_columns(connection, "projects"),
                    ("primary_path", "path", "cwd", "root"),
                    scan,
                )
            if "project_folders" in tables:
                _sqlite_workspace_values(
                    connection,
                    "project_folders",
                    _sqlite_columns(connection, "project_folders"),
                    ("path",),
                    scan,
                )
        except sqlite3.Error:
            scan.diagnostic("workspaces", "unsupported_database_schema", unsupported=True)


def _hermes_roots(scan: _Scan) -> list[Path]:
    roots = [scan.root]
    profiles = scan.root / "profiles"
    if profiles.is_dir() and not _is_link_like(profiles):
        try:
            children = list(islice(profiles.iterdir(), 51))
        except OSError:
            scan.diagnostic("profiles", "read_failed")
            return roots
        if len(children) > 50:
            scan.diagnostic("profiles", "profile_count_limit", count=1)
        for child in sorted(children[:50], key=lambda path: path.name.casefold()):
            if child.is_dir() and not _is_link_like(child):
                roots.append(child)
    return roots


def _hermes_skill_lock_names(data: Any, skills_root: Path) -> set[str]:
    if not isinstance(data, dict):
        return set()
    containers: list[dict[Any, Any] | list[Any]] = []
    for key in ("skills", "installed"):
        container_value = data.get(key)
        if isinstance(container_value, dict):
            containers.append(container_value)
        elif isinstance(container_value, list):
            containers.append(container_value)
    names: set[str] = set()
    for container in containers:
        entries: Iterable[tuple[Any, Any]]
        if isinstance(container, dict):
            entries = container.items()
        else:
            entries = ((None, item) for item in container)
        for raw_name, value in entries:
            candidates = [raw_name]
            if isinstance(value, dict):
                candidates.extend((value.get("name"), value.get("install_path")))
            for candidate in candidates:
                if not isinstance(candidate, str) or not candidate.strip():
                    continue
                path = Path(candidate)
                if path.is_absolute():
                    try:
                        path = path.relative_to(skills_root)
                    except ValueError:
                        continue
                if path.parts and path.parts[0].casefold() == "skills":
                    path = Path(*path.parts[1:])
                name = _safe_skill_name(path)
                if name:
                    names.add(name.casefold())
    return names


def _hermes_managed_skill_names(scan: _Scan, root: Path) -> frozenset[str]:
    skills_root = root / "skills"
    names: set[str] = set()
    manifest = skills_root / ".bundled_manifest"
    if manifest.is_file():
        text = _read_text(manifest, root, scan, "skills")
        if text is not None:
            for line in text.splitlines():
                raw_name = line.strip().split(":", 1)[0]
                name = _safe_skill_name(Path(raw_name))
                if name:
                    names.add(name.casefold())
    lock_path = skills_root / ".hub" / "lock.json"
    if lock_path.is_file():
        names.update(
            _hermes_skill_lock_names(
                _read_json(lock_path, root, scan, "skills"),
                skills_root,
            )
        )
    return frozenset(names)


def _scan_hermes(scan: _Scan) -> None:
    roots = _hermes_roots(scan)
    for root in roots:
        _scan_hermes_db(scan, root)
    _add_memory_files(
        scan,
        [
            (root / "memories" / filename, root)
            for root in roots
            for filename in ("MEMORY.md", "USER.md")
        ],
    )
    unsupported_memory_databases = sum(
        int(os.path.lexists(root / "memory_store.db")) for root in roots
    )
    if unsupported_memory_databases:
        scan.diagnostic(
            "memories",
            "unsupported_memory_database",
            unsupported=True,
            count=unsupported_memory_databases,
        )
    configs = _parse_configs(
        scan,
        [
            config
            for root in roots
            for config in (
                (root / "config.yaml", root, "yaml"),
                (root / "config.yml", root, "yaml"),
            )
        ],
    )
    _diagnose_unsupported_config(scan, configs)
    _add_mcp_configs(scan, configs)
    for root in roots:
        _add_skills(
            scan,
            [root / "skills"],
            excluded_parts=_HERMES_SKILL_EXCLUDED_PARTS,
            excluded_names=_hermes_managed_skill_names(scan, root),
        )
    schedule_paths = [
        path for root in roots for path in (root / "cron" / "jobs.json",) if path.is_file()
    ]
    default_timezone = ""
    for config in configs:
        timezone_value = config.get("timezone")
        if isinstance(timezone_value, str) and timezone_value:
            default_timezone = timezone_value
            break
    _add_hermes_json_schedules(
        scan,
        schedule_paths,
        scan.root,
        default_timezone=default_timezone,
    )
    settings: dict[str, Any] = {}
    for config in configs:
        _merge_missing(settings, _settings_from(config, "hermes"))
    if settings:
        scan.add("settings", json.dumps(settings, sort_keys=True), settings)


def _deduplicate_items(scan: _Scan) -> None:
    for category in CATEGORY_IDS:
        items = scan.items[category]
        if category == "sessions":
            canonical_by_transcript: dict[str, _Item] = {}
            for item in items:
                transcript = hashlib.sha256(
                    json.dumps(item.payload, ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    )
                ).hexdigest()
                existing = canonical_by_transcript.get(transcript)
                if existing is None or item.key < existing.key:
                    canonical_by_transcript[transcript] = item
            canonical_ids = {id(item) for item in canonical_by_transcript.values()}
            items = [item for item in items if id(item) in canonical_ids]
        unique: list[_Item] = []
        seen: set[str] = set()
        for item in items:
            if item.fingerprint in seen:
                continue
            seen.add(item.fingerprint)
            unique.append(item)
        scan.items[category] = unique


def _scan_source(
    source_id: str,
    root: Path,
    user_home: Path,
    *,
    config_paths: tuple[Path, ...] = (),
    workspace_paths: tuple[Path, ...] = (),
) -> _Scan:
    scan = _Scan(
        source_id=source_id,
        root=root,
        user_home=user_home,
        config_paths=config_paths,
        workspace_paths=workspace_paths,
    )
    if _is_link_like(root):
        scan.diagnostic("settings", "symlink_rejected")
        return scan
    scanners = {
        "codex": _scan_codex,
        "claude_code": _scan_claude,
        "meshclaw": _scan_meshclaw,
        "openclaw": _scan_openclaw,
        "hermes": _scan_hermes,
    }
    scanners[source_id](scan)
    _deduplicate_items(scan)
    return scan


def _source_summary(scan: _Scan) -> dict[str, Any]:
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
        "name": _SOURCE_NAMES[scan.source_id],
        "root": str(scan.root),
        "user_home": str(scan.user_home),
        "categories": categories,
    }
    if scan.config_paths:
        summary["_config_paths"] = [str(path) for path in scan.config_paths]
    if scan.workspace_paths:
        summary["_workspace_paths"] = [str(path) for path in scan.workspace_paths]
    return summary


def _preview(
    source_ids: list[str] | None,
    home: Path | None,
    env: Mapping[str, str] | None,
) -> dict[str, Any]:
    requested = list(SOURCE_IDS) if source_ids is None else list(dict.fromkeys(source_ids))
    unknown = [source_id for source_id in requested if source_id not in SOURCE_IDS]
    requested = [source_id for source_id in requested if source_id in SOURCE_IDS]
    base_home, roots = _source_roots(home, env)
    env_map = os.environ if env is None else env
    scans = []
    for source_id in requested:
        root = roots[source_id]
        config_paths: tuple[Path, ...] = ()
        workspace_paths: tuple[Path, ...] = ()
        if source_id == "openclaw":
            config_paths, workspace_paths = _openclaw_context(root, base_home, env_map)
        if (
            _source_exists(source_id, root)
            or _is_link_like(root)
            or any(path.is_file() for path in config_paths)
        ):
            scans.append(
                _scan_source(
                    source_id,
                    root,
                    base_home,
                    config_paths=config_paths,
                    workspace_paths=workspace_paths,
                )
            )
    skipped = [diagnostic for scan in scans for diagnostic in scan.skipped]
    skipped.extend(
        {
            "source_id": source_id,
            "category_id": "",
            "reason": "unknown_source",
        }
        for source_id in unknown
    )
    sources = [_source_summary(scan) for scan in scans]
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


def detect_sources(
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Detect supported foreign-agent homes and summarize importable categories."""
    return _preview(None, home, env)


def preview_import(
    source_ids: list[str] | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a content-free, selectable import plan."""
    return _preview(source_ids, home, env)


def _merge_missing(destination: dict[str, Any], incoming: dict[str, Any]) -> bool:
    changed = False
    for key, value in incoming.items():
        if key not in destination:
            destination[key] = value
            changed = True
        elif isinstance(destination[key], dict) and isinstance(value, dict):
            changed = _merge_missing(destination[key], value) or changed
    return changed


def _load_json_dict(path: Path, *, fail_closed: bool = False) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError:
        if fail_closed:
            raise
        return {}
    except (UnicodeError, json.JSONDecodeError) as exc:
        if fail_closed:
            raise ValueError("invalid destination JSON") from exc
        return {}
    if isinstance(data, dict):
        return data
    if fail_closed:
        raise ValueError("destination JSON must contain an object")
    return {}


def _write_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _load_ledger(path: Path) -> dict[str, Any]:
    data = _load_json_dict(path)
    if data.get("version") != _LEDGER_VERSION or not isinstance(data.get("records"), dict):
        return {"version": _LEDGER_VERSION, "records": {}}
    return data


def _selected_pairs(plan: dict[str, Any]) -> set[tuple[str, str]]:
    # The only producers of plan["selection"] (the backend _preview and the API
    # handler's _select_fresh_plan) always emit the canonical list of
    # {"source_id", "category_id"} dicts, so that is the sole shape parsed here.
    # The SOURCE_IDS/CATEGORY_IDS filter is a real guard and is retained.
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
    return {pair for pair in selected if pair[0] in SOURCE_IDS and pair[1] in CATEGORY_IDS}


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
        if source_id in SOURCE_IDS and isinstance(root, str) and root:
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
        if source_id in SOURCE_IDS and isinstance(user_home, str) and user_home:
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
        if source_id not in SOURCE_IDS or not isinstance(values, list):
            continue
        paths[str(source_id)] = tuple(Path(value) for value in values if isinstance(value, str))
    return paths


def _record_ledger(
    ledger: dict[str, Any],
    item: _Item,
    *,
    destination_key: str = "",
) -> None:
    records = ledger.setdefault("records", {})
    record = {
        "source_id": item.source_id,
        "category_id": item.category,
        "imported_at": datetime.now(timezone.utc).isoformat(),
    }
    if destination_key:
        record["destination_key"] = destination_key
    records[item.fingerprint] = record


def _session_destination_key(item: _Item) -> str:
    return f"imported-{item.source_id}-{item.fingerprint[:16]}"


def _write_session(item: _Item, conversation_log: Any) -> str:
    key = _session_destination_key(item)
    update_metadata = getattr(conversation_log, "update_metadata", None)
    if not callable(update_metadata):
        raise TypeError("conversation log does not support metadata updates")
    lock_factory = getattr(conversation_log, "_locked", None)
    lock = lock_factory(key) if callable(lock_factory) else nullcontext()
    with lock:
        has_log = getattr(conversation_log, "has_log", None)
        if callable(has_log) and has_log(key):
            read_messages = getattr(conversation_log, "read_messages", None)
            rewrite_session = getattr(conversation_log, "rewrite_session", None)
            if not callable(read_messages) or not callable(rewrite_session):
                update_metadata(key, {"closed": True})
                return "existing"
            existing = read_messages(key)
            expected = [{"role": role, "content": content} for role, content in item.payload]
            normalized = [
                {"role": message.get("role"), "content": message.get("content")}
                for message in existing
                if isinstance(message, dict)
            ]
            if normalized == expected:
                update_metadata(key, {"closed": True})
                return "existing"
            if len(normalized) < len(expected) and normalized == expected[: len(normalized)]:
                rewrite_session(key, expected)
                update_metadata(key, {"closed": True})
                return "imported"
            return "conflict"
        init = getattr(conversation_log, "init", None)
        if callable(init):
            init()
        try:
            for role, content in item.payload:
                conversation_log.append(key, role, content)
            update_metadata(key, {"closed": True})
        except BaseException:
            delete_session = getattr(conversation_log, "delete_session", None)
            if callable(delete_session):
                try:
                    delete_session(key)
                except Exception:
                    logger.warning("Failed to roll back imported session", exc_info=True)
            raise
        return "imported"


def _write_memory(
    item: _Item,
    data_home: Path,
    vector_store: VectorMemoryStore | None,
) -> str:
    if isinstance(item.payload, dict) and item.payload.get("kind") == "semantic":
        if vector_store is None:
            return "rejected"
        key = str(item.payload["key"])
        value = item.payload["value"]
        outcome = vector_store.set_semantic_if_absent(
            key,
            value,
            float(item.payload["confidence"]),
            "import",
        )
        if outcome == "imported":
            return "imported"
        existing = vector_store.get_semantic(key)
        if existing is not None:
            try:
                return "existing" if json.loads(existing["value_json"]) == value else "conflict"
            except (KeyError, TypeError, json.JSONDecodeError, RecursionError):
                return "conflict"
        return "rejected"
    if isinstance(item.payload, dict) and item.payload.get("kind") == "episodic":
        if vector_store is None:
            return "rejected"
        text = str(item.payload["text"])
        if vector_store.has_episodic_text(text):
            return "existing"
        written = vector_store.write_episodic(
            text,
            tags=["imported", item.source_id],
            importance=float(item.payload["importance"]),
            source="import",
            preserve_existing=True,
        )
        if written:
            return "imported"
        return "existing" if vector_store.has_episodic_text(text) else "rejected"

    return "rejected"


def _write_workspace(item: _Item, data_home: Path) -> str:
    workspace = Path(str(item.payload)).resolve(strict=True)
    destination = data_home.resolve()
    if (
        not workspace.is_dir()
        or is_sensitive_path(str(workspace))
        or workspace == destination
        or destination in workspace.parents
    ):
        return "rejected"

    path = data_home / "config.json"
    data = _load_json_dict(path, fail_closed=True)
    workspaces = data.get("workspaces")
    if workspaces is None:
        workspaces = {}
        data["workspaces"] = workspaces
    if not isinstance(workspaces, dict):
        return "conflict"

    canonical = str(workspace)
    for existing in workspaces.values():
        existing_dir = (
            existing.get("dir")
            if isinstance(existing, dict)
            else existing if isinstance(existing, str) else None
        )
        if not isinstance(existing_dir, str):
            continue
        try:
            if str(Path(existing_dir).expanduser().resolve()) == canonical:
                return "existing"
        except (OSError, RuntimeError):
            continue

    base_name = _SAFE_NAME_RE.sub("-", workspace.name).strip("-._").lower()
    base_name = base_name[:64] or f"imported-{item.source_id}"
    name = base_name
    if name in workspaces:
        name = f"{base_name}-{item.source_id}"[:64]
    if name in workspaces:
        suffix = item.fingerprint[:8]
        name = f"{base_name[:55]}-{suffix}"
    if name in workspaces:
        return "conflict"
    workspaces[name] = {"dir": canonical}
    _write_json(path, data)
    return "imported"


@contextmanager
def _mcp_lock(_path: Path) -> Iterator[None]:
    """Coordinate with dashboard and app writers of the KiroCrew MCP file."""
    # The dashboard's MCP handlers write the same data-home file while holding
    # the global Kiro MCP sidecar lock. Reuse that lock here so import cannot
    # race a manual enable/edit operation. This is imported lazily because the
    # dashboard handler imports this module during gateway startup.
    from kiro_crew.dashboard.handlers.mcp import _get_mcp_lock_sync

    with _get_mcp_lock_sync():
        yield


def _write_mcp(item: _Item, data_home: Path, user_home: Path) -> str:
    path = data_home / "mcp.json"
    with _mcp_lock(path):
        data = _load_json_dict(path, fail_closed=True)
        if "mcpServers" not in data:
            servers: dict[str, Any] = {}
            data["mcpServers"] = servers
        else:
            servers = data["mcpServers"]
            if not isinstance(servers, dict):
                return "conflict"
        name = item.payload["name"]
        spec = item.payload["spec"]
        if name in servers:
            return "existing" if servers[name] == spec else "conflict"
        from kiro_crew.mcp_discovery import configured_mcp_aliases

        if mcp_server_alias(name) in configured_mcp_aliases(
            data_home=data_home,
            user_home=user_home,
        ):
            return "conflict"
        servers[name] = spec
        _write_json(path, data)
    return "imported"


def _has_symlink_component(path: Path, anchor: Path) -> bool:
    try:
        relative = path.relative_to(anchor)
    except ValueError:
        return True
    current = anchor
    for part in relative.parts:
        current = current / part
        try:
            component_stat = current.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if _is_link_like(current, component_stat):
            return True
    return False


def _write_skill(item: _Item, data_home: Path) -> str:
    destination = data_home / "skills" / "imported" / item.source_id / item.payload["name"]
    files = item.payload.get("files")
    if not isinstance(files, dict) or "SKILL.md" not in files:
        return "rejected"
    if _has_symlink_component(destination, data_home):
        return "rejected"
    existing_count = 0
    for relative, content in files.items():
        if not isinstance(relative, str) or not isinstance(content, str):
            return "rejected"
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return "rejected"
        target = destination / relative_path
        if _has_symlink_component(target, data_home):
            return "rejected"
        if not target.exists():
            continue
        existing_count += 1
        try:
            if target.read_bytes() != content.encode("utf-8"):
                return "conflict"
        except OSError:
            return "conflict"
    if existing_count == len(files):
        return "existing"
    if existing_count:
        return "conflict"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if _has_symlink_component(destination, data_home):
        return "rejected"
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.import-",
            dir=str(destination.parent),
        )
    )
    try:
        for relative, content in files.items():
            target = staging / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        if _has_symlink_component(destination, data_home):
            return "rejected"
        if destination.exists() or _is_link_like(destination):
            return "conflict"
        os.replace(staging, destination)
        return "imported"
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _same_schedule(job: Any, payload: dict[str, Any]) -> bool:
    if getattr(job, "name", "") != payload["name"]:
        return False
    if getattr(job, "message", "") != payload["message"]:
        return False
    if getattr(job, "timezone", "") != payload.get("timezone", ""):
        return False
    schedule = getattr(job, "schedule", None)
    if schedule is None:
        return False
    if "cron_expr" in payload:
        return getattr(schedule, "cron_expr", None) == payload["cron_expr"]
    if "every_secs" in payload:
        return getattr(schedule, "every_secs", None) == payload["every_secs"]
    return getattr(schedule, "at_ts", None) == payload.get("at_ts")


def _write_schedule(item: _Item, cron_service: Any) -> str:
    payload = item.payload
    add_if_absent = getattr(cron_service, "add_job_if_absent", None)
    if callable(add_if_absent) and "add_job" not in vars(cron_service):
        job = add_if_absent(
            lambda candidate: _same_schedule(candidate, payload),
            name=payload["name"],
            message=payload["message"],
            every_secs=payload.get("every_secs"),
            at_ts=payload.get("at_ts"),
            cron_expr=payload.get("cron_expr"),
            created_by=f"import:{item.source_id}",
            enabled=False,
            timezone=payload.get("timezone", ""),
        )
        return "existing" if job is None else "imported"
    for job in cron_service.list_jobs(include_disabled=True):
        if _same_schedule(job, payload):
            return "existing"
    cron_service.add_job(
        name=payload["name"],
        message=payload["message"],
        every_secs=payload.get("every_secs"),
        at_ts=payload.get("at_ts"),
        cron_expr=payload.get("cron_expr"),
        created_by=f"import:{item.source_id}",
        enabled=False,
        timezone=payload.get("timezone", ""),
    )
    return "imported"


def _write_settings(item: _Item, data_home: Path) -> str:
    path = data_home / "config.json"
    data = _load_json_dict(path, fail_closed=True)
    changed = _merge_missing(data, item.payload)
    if not changed:
        return "existing"
    _write_json(path, data)
    return "imported"


def apply_import(
    plan: dict[str, Any],
    *,
    data_home: Path | None = None,
    conversation_log: Any = None,
    cron_service: Any = None,
    vector_store: VectorMemoryStore | None = None,
) -> dict[str, Any]:
    """Apply selected source/category pairs with merge-only, idempotent writes."""
    destination = Path(data_home) if data_home is not None else config_dir()
    destination.mkdir(parents=True, exist_ok=True)
    selected = _selected_pairs(plan)
    roots = _plan_roots(plan)
    user_homes = _plan_user_homes(plan)
    config_paths = _plan_private_paths(plan, "_config_paths")
    workspace_paths = _plan_private_paths(plan, "_workspace_paths")
    ledger_path = destination / _LEDGER_RELATIVE_PATH
    ledger = _load_ledger(ledger_path)
    records = ledger["records"]
    imported = {category: 0 for category in CATEGORY_IDS}
    already_imported = 0
    item_outcomes: list[dict[str, str]] = []
    conflicts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = [
        item for item in plan.get("skipped", []) if isinstance(item, dict)
    ]
    scans: dict[str, _Scan] = {}
    for source_id, category in sorted(selected):
        root = roots.get(source_id)
        source_configs = config_paths.get(source_id, ())
        if root is None or (
            not _source_exists(source_id, root)
            and not any(path.is_file() for path in source_configs)
        ):
            skipped.append(
                {
                    "source_id": source_id,
                    "category_id": category,
                    "reason": "source_unavailable",
                }
            )
            continue
        if source_id not in scans:
            scans[source_id] = _scan_source(
                source_id,
                root,
                user_homes.get(source_id, root.parent),
                config_paths=source_configs,
                workspace_paths=workspace_paths.get(source_id, ()),
            )
            for diagnostic in scans[source_id].skipped:
                if diagnostic not in skipped:
                    skipped.append(diagnostic)

    if conversation_log is None and any(category == "sessions" for _source, category in selected):
        from kiro_crew.history import ConversationLog

        conversation_log = ConversationLog(destination / "sessions")
    if cron_service is None and any(category == "schedules" for _source, category in selected):
        from kiro_crew.cron import CronService

        cron_service = CronService(base_dir=destination)

    owned_vector_store: VectorMemoryStore | None = None
    needs_vector_store = any(
        isinstance(item.payload, dict) and item.payload.get("kind") in ("semantic", "episodic")
        for scan in scans.values()
        for item in scan.items["memories"]
    )
    if vector_store is None and needs_vector_store:
        owned_vector_store = VectorMemoryStore(db_path=destination / "memory.db")
        owned_vector_store.embed_fn_factory = make_sync_embed_fn
        owned_vector_store.embed_fn = make_sync_embed_fn()
        owned_vector_store.init()
        vector_store = owned_vector_store

    try:
        for source_id, category in sorted(selected):
            scan = scans.get(source_id)
            if scan is None:
                continue
            for item in scan.items[category]:
                outcome = {
                    "source_id": source_id,
                    "category_id": category,
                    "item_hash": item.fingerprint,
                }
                if item.fingerprint in records:
                    already_imported += 1
                    item_outcomes.append({**outcome, "outcome": "deduplicated"})
                    continue
                status = "skipped"
                try:
                    if category == "sessions":
                        status = _write_session(item, conversation_log)
                    elif category == "memories":
                        status = _write_memory(item, destination, vector_store)
                    elif category == "workspaces":
                        status = _write_workspace(item, destination)
                    elif category == "mcp_servers":
                        status = _write_mcp(item, destination, scan.user_home)
                    elif category == "skills":
                        status = _write_skill(item, destination)
                    elif category == "schedules":
                        status = _write_schedule(item, cron_service)
                    elif category == "settings":
                        status = _write_settings(item, destination)
                except (OSError, ValueError, TypeError, sqlite3.Error):
                    logger.warning(
                        "Foreign-agent import failed for %s/%s",
                        source_id,
                        category,
                        exc_info=True,
                    )
                    skipped.append(
                        {
                            "source_id": source_id,
                            "category_id": category,
                            "reason": "write_failed",
                        }
                    )
                    item_outcomes.append({**outcome, "outcome": "rejected"})
                    continue
                if status in ("imported", "existing"):
                    _record_ledger(
                        ledger,
                        item,
                        destination_key=(
                            _session_destination_key(item) if category == "sessions" else ""
                        ),
                    )
                    _write_json(ledger_path, ledger)
                    if status == "imported":
                        imported[category] += 1
                        item_outcomes.append({**outcome, "outcome": "accepted"})
                    else:
                        already_imported += 1
                        item_outcomes.append({**outcome, "outcome": "deduplicated"})
                elif status == "conflict":
                    conflicts.append(
                        {
                            "source_id": source_id,
                            "category_id": category,
                            "reason": "destination_conflict",
                        }
                    )
                    item_outcomes.append({**outcome, "outcome": "rejected"})
                else:
                    skipped.append(
                        {
                            "source_id": source_id,
                            "category_id": category,
                            "reason": "destination_rejected",
                        }
                    )
                    item_outcomes.append({**outcome, "outcome": "rejected"})
    finally:
        if owned_vector_store is not None:
            owned_vector_store.close()

    return {
        "imported": imported,
        "imported_count": sum(imported.values()),
        "already_imported": already_imported,
        "item_outcomes": item_outcomes,
        "conflicts": conflicts,
        "skipped": skipped,
        "secret_count": max(
            int(plan.get("secret_count", 0)),
            sum(scan.secret_count for scan in scans.values()),
        ),
        "unsupported_count": max(
            int(plan.get("unsupported_count", 0)),
            sum(scan.unsupported_count for scan in scans.values()),
        ),
        "ledger": str(_LEDGER_RELATIVE_PATH).replace("\\", "/"),
    }
