"""Conservative import of user-owned data from other local agent tools."""

from __future__ import annotations

import contextlib
import hashlib
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
from contextlib import contextmanager
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
from kiro_crew.learn import _MAX_LESSONS_TOTAL, Lesson, LessonStore
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.security import (
    contains_injection,
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.skills import _BLOCK_SCALAR_INDICATORS
from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)


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


SOURCE_IDS = ("codex", "claude_code", "meshclaw", "openclaw", "hermes")
# Conflict strategies. ``skip`` is the default and the only non-destructive one;
# the other two require an explicit user choice per apply request. See
# docs/system-specs/modules/onboarding-import.md -> "Conflict strategy".
STRATEGY_SKIP = "skip"
STRATEGY_RENAME = "rename"
STRATEGY_OVERWRITE = "overwrite"
CONFLICT_STRATEGIES = (STRATEGY_SKIP, STRATEGY_RENAME, STRATEGY_OVERWRITE)
# Categories whose destination collisions a strategy can actually resolve. The
# rest are merge-only (instructions, memories, denied_commands, settings) or
# semantically deduplicated (schedules), so a strategy has nothing to act on.
STRATEGY_CATEGORIES = frozenset({"skills", "mcp_servers", "workspaces"})
# Categories whose destination holds exactly ONE item per identity and can be
# REPLACED after a first import (via ``overwrite``). Their ledger records cannot
# be trusted as a fast path, because the destination may have moved on since.
_REPLACEABLE_CATEGORIES = frozenset({"skills", "mcp_servers"})

CATEGORY_IDS = (
    "instructions",
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
    "instructions": "Instructions",
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
# Directory names a foreign agent's OWN importer uses for skills it pulled in
# from a third agent (Hermes: ``hermes import-agent`` / ``hermes claw migrate``).
_FOREIGN_REIMPORT_SKILL_DIRS = (
    "claude-code-imports",
    "codex-imports",
    "openclaw-imports",
)
_OPENCLAW_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_HERMES_SKILL_EXCLUDED_PARTS = frozenset(
    {
        ".archive",
        ".hub",
        "dependency",
        "dependencies",
        "cache",
        ".cache",
        # Hermes ships its own importer, which writes FOREIGN skills into these
        # dirs. Importing them from Hermes would duplicate what the original
        # source already contributes, and neither dedupe layer can catch it: the
        # fingerprint is source-scoped (``hermes`` != ``claude_code``) and the
        # destination differs too (``skills/imported/hermes/`` vs
        # ``skills/imported/claude_code/``), so not even a conflict is reported.
        # The originals are still on disk, so excluding these loses nothing.
        *_FOREIGN_REIMPORT_SKILL_DIRS,
    }
)
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
_LEDGER_VERSION = 1
_PLAN_VERSION = 1
_LEDGER_RELATIVE_PATH = Path("imports") / "foreign-agent-imports.json"
# ``overwrite`` never destroys without a restore copy. One dir per apply run so a
# user can find everything a single import replaced together.
_REPLACED_RELATIVE_DIR = Path("imports") / "replaced"
_MAX_FILES = 500
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_SKILL_BYTES = 256 * 1024
_MAX_YAML_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
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
# Foreign memory stores carry a workspace/scope column even for single-workspace
# installs, where it holds a SENTINEL rather than a real workspace identity.
# Treating a sentinel as "scoped" drops every row: MeshClaw stamps ``default`` on
# all of them, so the whole store read as workspace-scoped and imported nothing.
_UNSCOPED_WORKSPACE_IDS = frozenset({"", "default", "global", "main", "none", "null"})
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
        "kirocrew-computer",
        "meshclaw-core",
        "meshclaw-cron",
        "meshclaw-computer",
        "openclaw-core",
        "openclaw-cron",
        "openclaw-computer",
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


@dataclass(frozen=True)
class _WriteOutcome:
    """One writer's result plus the details the API must report back.

    ``status`` is the four-value writer vocabulary (imported/existing/conflict/
    rejected). ``renamed_to`` and ``restored_to`` are set only when a strategy
    actually took effect, so a plain ``skip`` apply reports exactly what it did
    before this existed.
    """

    status: str
    renamed_to: str = ""
    restored_to: str = ""
    # The destination identity this item now occupies (currently only an MCP
    # server name). Lets the ledger keep one record per single-occupancy
    # destination instead of one per source.
    destination_key: str = ""


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
        result = _load_no_alias_yaml(text)
    except (yaml.YAMLError, RecursionError, ValueError):
        scan.diagnostic("settings", "invalid_config")
        return {}
    return result if isinstance(result, dict) else {}


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
    """Project user-authored instruction documents onto KiroCrew's memory tiers.

    ``CLAUDE.md`` / ``AGENTS.md`` and the DIRECTIVE body of a persona document
    (OpenClaw / Hermes ``SOUL.md``) are the least replaceable thing a user owns,
    so they land in ``lessons.jsonl`` — the highest-priority durable tier
    (see docs/system-specs/modules/onboarding-import.md). The persona *role* is
    deliberately NOT imported: KiroCrew's persona surface is theme-pack persona,
    governed by ``capabilities.theme_persona``, and no foreign text may become
    system-prompt identity through this path.

    ``preferences.md`` / ``projects.md`` are NOT valid destinations — the memory
    consolidator replaces both wholesale, so an import there is destroyed on the
    next consolidation run.
    """

    seen: set[str] = set()
    for path, anchor in paths:
        marker = os.path.normcase(os.path.abspath(str(path)))
        if marker in seen or not path.is_file():
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


def _column0_activation_declared(text: str) -> bool:
    """True if any column-0 auto-activation declaration is in the frontmatter.

    The activation decision must mirror what ``SkillsLoader._parse_frontmatter``
    can conclude after install, not ``_frontmatter``'s collapsed map (where an
    indented prose line like ``  always: false`` overwrites the real value, and
    an indented ``---`` inside a block scalar truncates the scan). Region and
    key rules therefore match the loader exactly: the frontmatter closes at the
    first line that STARTS with ``---`` (the loader's ``\\n---`` regex), and
    only column-0 keys count. A column-0 ``always`` activates on a truthy plain
    value or a bare block-scalar indicator (fail-closed: this parser cannot see
    the continuation lines the loader resolves); a column-0 ``triggers`` key
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
        value = raw.strip().strip("\"'").casefold()
        if value in {"1", "true", "yes"} or value in _BLOCK_SCALAR_INDICATORS:
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
    _add_instruction_files(scan, [(root / "AGENTS.md", root)])
    _scan_codex_automations(scan)
    settings: dict[str, Any] = {}
    for config in configs:
        _merge_missing(settings, _settings_from(config, "codex"))
    if settings:
        scan.add("settings", json.dumps(settings, sort_keys=True), settings)


def _scan_claude(scan: _Scan) -> None:
    root = scan.root
    # Workspaces come from explicit configuration ONLY. Session transcripts are
    # not imported (see docs/system-specs/modules/onboarding-import.md), so the
    # root configs are parsed FIRST to learn the workspaces, then each
    # workspace's own config files are parsed in a second pass.
    root_configs = _parse_configs(
        scan,
        [
            (root / "settings.local.json", root, "json"),
            (root / "settings.json", root, "json"),
            (root / ".claude.json", root, "json"),
            (root.parent / ".claude.json", root.parent, "json"),
        ],
    )
    workspaces: set[str] = set()
    for config in root_configs:
        workspaces.update(_collect_project_paths(config))
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
    configs = root_configs + _parse_configs(scan, project_configs)
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
    instruction_paths: list[tuple[Path, Path]] = [(root / "CLAUDE.md", root)]
    instruction_paths += [
        (path, root)
        for path in _walk_files(
            root / "rules",
            scan,
            "instructions",
            suffixes=(".md", ".markdown"),
        )
    ]
    instruction_paths += [
        (Path(workspace) / "CLAUDE.md", Path(workspace)) for workspace in sorted(workspaces)
    ]
    _add_instruction_files(scan, instruction_paths)
    settings: dict[str, Any] = {}
    for config in configs:
        _merge_missing(settings, _settings_from(config, "claude_code"))
    if settings:
        scan.add("settings", json.dumps(settings, sort_keys=True), settings)


def _scan_meshclaw(scan: _Scan) -> None:
    root = scan.root
    workspaces: set[str] = set()
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
    # MeshClaw's workspace holds arbitrary user documents, so only the canonical
    # instruction filenames are read — never a blind sweep of every .md there.
    _add_instruction_files(
        scan,
        [
            (base / filename, base)
            for base in (root / "workspace", *(Path(w) for w in sorted(workspaces)))
            for filename in ("AGENTS.md", "CLAUDE.md")
        ],
    )
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
            scan.diagnostic("workspaces", "symlink_rejected")
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
        scan.diagnostic("workspaces", "agent_count_limit", count=1)
    agent_dirs: list[Path] = []
    for child in sorted(children, key=lambda path: path.name.casefold()):
        if _is_link_like(child):
            scan.diagnostic("workspaces", "symlink_rejected")
            continue
        if child.is_dir():
            agent_dirs.append(child)
    return agent_dirs


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
    # SOUL.md's DIRECTIVE text becomes lessons; its persona ROLE is not imported
    # (see _add_instruction_files). AGENTS.md is a plain instruction document.
    _add_instruction_files(
        scan,
        [
            (workspace / filename, workspace)
            for workspace in ordered_workspaces
            for filename in ("SOUL.md", "AGENTS.md")
        ],
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


def _row_is_workspace_scoped(value: Any) -> bool:
    """Return whether a memory row belongs to ONE foreign workspace.

    KiroCrew's own memory tables have no workspace column, so a genuinely
    workspace-scoped row has no faithful destination and is reported unsupported.
    A SENTINEL value is not scoping, though: a single-workspace install stamps
    every row with the same placeholder (``default`` in MeshClaw's case), so
    reading that as scoped discarded 100% of the store.
    """
    if value is None:
        return False
    return str(value).strip().casefold() not in _UNSCOPED_WORKSPACE_IDS


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
                        if _row_is_workspace_scoped(values.get("workspace_id")):
                            scan.diagnostic(
                                "memories",
                                "scoped_memory_unsupported",
                                unsupported=True,
                            )
                            continue
                        # A directive is a RULE, not a fact, so semantic memory is
                        # the wrong tier -- but dropping it would discard exactly
                        # the least replaceable rows (MeshClaw stores every learned
                        # lesson this way). Route it to the instruction tier, which
                        # is where an imported rule belongs, instead.
                        is_directive = str(values.get("kind", "")).casefold() == "directive"
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
                        # Re-screen the DECODED value: the screens above ran on the
                        # raw JSON text, which hides both patterns behind escapes.
                        unsafe = _decoded_value_is_unsafe(value, scan)
                        if unsafe:
                            scan.diagnostic("memories", unsafe)
                            continue
                        numeric_confidence = (
                            float(confidence)
                            if isinstance(confidence, (int, float))
                            and not isinstance(confidence, bool)
                            else 0.9
                        )
                        if is_directive:
                            _add_db_directive(scan, key, value)
                            continue
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
                        if _row_is_workspace_scoped(values.get("workspace_id")):
                            scan.diagnostic(
                                "memories",
                                "scoped_memory_unsupported",
                                unsupported=True,
                            )
                            continue
                        is_directive = str(values.get("kind", "")).casefold() == "directive"
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
                        # A directive stored as an episode is still a rule: route it
                        # to the lesson tier rather than dropping it (see
                        # _add_db_directive). Checked before the episodic length
                        # bound so a directive is measured against the instruction
                        # limits, not the episodic ones.
                        if is_directive:
                            _add_db_directive(scan, str(memory_id), cleaned)
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
    _add_memory_files(
        scan,
        [
            (root / "memories" / filename, root)
            for root in roots
            for filename in ("MEMORY.md", "USER.md")
        ],
    )
    _add_instruction_files(scan, [(root / "SOUL.md", root) for root in roots])
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
    if destination_key:
        # A destination that holds exactly ONE item per key (an MCP server name)
        # can only be described by one ledger record. Without this, two sources
        # overwriting the same name leave two live fingerprints: when the first
        # source's definition later changes, its new fingerprint overwrites the
        # destination while the SECOND source's stale fingerprint still
        # deduplicates — so the definition the user selected silently vanishes.
        stale = [
            fingerprint
            for fingerprint, existing in records.items()
            if isinstance(existing, dict)
            and existing.get("category_id") == item.category
            and existing.get("destination_key") == destination_key
            and fingerprint != item.fingerprint
        ]
        for fingerprint in stale:
            del records[fingerprint]
    record = {
        "source_id": item.source_id,
        "category_id": item.category,
        "imported_at": datetime.now(timezone.utc).isoformat(),
    }
    if destination_key:
        record["destination_key"] = destination_key
    records[item.fingerprint] = record


def _normalize_strategy(value: Any) -> str:
    """Coerce a client-supplied strategy to a known one, defaulting to skip."""

    candidate = str(value or "").strip().lower()
    return candidate if candidate in CONFLICT_STRATEGIES else STRATEGY_SKIP


def _rename_candidates(base: str, item: _Item) -> list[str]:
    """Derived non-colliding names, most readable first.

    A user who renames wants to recognize the result, so the source-suffixed
    form is tried before the digest-suffixed fallback.
    """

    suffixed = f"{base}-{item.source_id}"
    digest = f"{base}-{item.fingerprint[:8]}"
    return [name for name in (suffixed, digest) if name != base]


def _restore_dir(data_home: Path, run_stamp: str, category: str) -> Path:
    return data_home / _REPLACED_RELATIVE_DIR / run_stamp / category


def _preserve_replaced_tree(source: Path, destination: Path) -> str:
    """Copy a directory aside before it is replaced. Returns the restore path.

    Raises so the caller can refuse to overwrite: losing the restore copy is the
    one failure that makes ``overwrite`` unrecoverable.

    The run stamp has one-second resolution, so two overwrites of the same item
    inside one second would collide. Suffix on collision rather than refusing (a
    refusal here reads to the user as an unresolvable conflict) and never
    overwrite an existing restore copy, which would defeat the point of keeping
    one.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    target = destination
    for attempt in range(1, 100):
        if not target.exists():
            break
        target = destination.with_name(f"{destination.name}-{attempt}")
    shutil.copytree(source, target, symlinks=True, dirs_exist_ok=False)
    return str(target)


def _preserve_replaced_json(payload: Any, destination: Path) -> str:
    """Write a replaced JSON fragment aside. Returns the restore path.

    Suffixes on collision for the same reason as ``_preserve_replaced_tree``: the
    run stamp is second-resolution, and clobbering an earlier restore copy would
    defeat the point of keeping one.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    target = destination
    for attempt in range(1, 100):
        if not target.exists():
            break
        target = destination.with_name(f"{destination.stem}-{attempt}{destination.suffix}")
    _write_json(target, payload)
    return str(target)


def _lessons_overlap(incoming: str, existing: str) -> bool:
    """Whether two lesson rules are close enough to treat as the same lesson.

    Mirrors ``VectorMemoryStore.write_lesson``'s own dedupe (substring, then
    >50% significant-word overlap) so import RECOGNIZES the same collisions --
    but reports them instead of replacing, which is what that writer would do.
    """

    left = incoming.lower().strip()
    right = existing.lower().strip()
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    left_words = {word for word in re.findall(r"[a-z0-9]{4,}", left)}
    right_words = {word for word in re.findall(r"[a-z0-9]{4,}", right)}
    if not left_words or not right_words:
        return False
    shared = left_words & right_words
    return len(shared) / min(len(left_words), len(right_words)) > 0.5


def _write_instruction(
    item: _Item,
    lesson_store: Any,
    vector_store: VectorMemoryStore | None = None,
) -> _WriteOutcome:
    """Append one imported directive to the highest-priority durable tier.

    ``LessonStore.save`` is itself exact-rule deduplicating, so a re-import is
    naturally idempotent; this reports ``existing`` for that case so the ledger
    still records it and the outcome is ``deduplicated`` rather than a false
    ``accepted``.
    """

    rule = str(item.payload.get("rule", "")).strip()
    if not rule:
        return _WriteOutcome("rejected")

    # ContextBuilder reads lesson.* from the VECTOR store when it holds any, and
    # then never reads lessons.jsonl (context.py: `if memory.vector_store and
    # memory.vector_store.get_lessons()`). Writing only the JSONL there would
    # record the item as imported while the agent never sees it, so route through
    # whichever store is actually authoritative.
    # Route on AVAILABILITY, not current emptiness. An empty vector store still
    # becomes authoritative the moment any native lesson lands, and ContextBuilder
    # then stops reading lessons.jsonl -- so a JSONL write made while the store
    # happened to be empty would silently disappear later, with the ledger
    # preventing a re-import.
    if vector_store is not None:
        # NOT ``write_lesson``: it deletes an existing lesson on exact-substring
        # OR >50% topic overlap ("newer replaces older"), which for an import
        # means a foreign directive can delete a correction the USER taught the
        # agent. Import is merge-only, so overlap yields ``existing`` (nothing
        # written, nothing deleted) and only a genuinely new rule is inserted --
        # via the absent-only writer, which cannot replace anything.
        for existing in vector_store.get_lessons():
            try:
                stored = json.loads(str(existing.get("value_json", "")))
            except (TypeError, ValueError):
                continue
            stored_rule = str(stored.get("rule", "")) if isinstance(stored, dict) else str(stored)
            if _lessons_overlap(rule, stored_rule):
                return _WriteOutcome("existing")
        key = f"lesson.{hashlib.sha256(rule.encode()).hexdigest()[:16]}"
        outcome = vector_store.set_semantic_if_absent(
            key,
            {"rule": rule, "category": "preference", "negative": None},
            1.0,
            "import",
        )
        return _WriteOutcome("imported" if outcome == "imported" else "existing")

    if lesson_store is None:
        return _WriteOutcome("rejected")
    load_all = getattr(lesson_store, "load_all", None)
    if callable(load_all):
        existing_lessons = list(load_all())
        normalized = rule.lower()
        for existing in existing_lessons:
            if str(getattr(existing, "rule", "")).lower().strip() == normalized:
                return _WriteOutcome("existing")
        # ``LessonStore.save`` prunes OLDEST-first once the store passes its own
        # ceiling, and the user's own corrections are the oldest entries. A
        # per-import cap alone does not protect them: 151 existing + 50 imported
        # still evicts one. Refuse to write past the store's REMAINING capacity
        # so an import can never delete a lesson the user taught the agent.
        if len(existing_lessons) >= _MAX_LESSONS_TOTAL:
            return _WriteOutcome("rejected")
    lesson_store.save(
        Lesson(
            ts=datetime.now(timezone.utc).isoformat(),
            rule=rule,
            category="preference",
        )
    )
    return _WriteOutcome("imported")


def _write_memory(
    item: _Item,
    data_home: Path,
    vector_store: VectorMemoryStore | None,
) -> _WriteOutcome:
    if isinstance(item.payload, dict) and item.payload.get("kind") == "semantic":
        if vector_store is None:
            return _WriteOutcome("rejected")
        key = str(item.payload["key"])
        value = item.payload["value"]
        outcome = vector_store.set_semantic_if_absent(
            key,
            value,
            float(item.payload["confidence"]),
            "import",
        )
        if outcome == "imported":
            return _WriteOutcome("imported")
        existing = vector_store.get_semantic(key)
        if existing is not None:
            try:
                same = json.loads(existing["value_json"]) == value
                return _WriteOutcome("existing" if same else "conflict")
            except (KeyError, TypeError, json.JSONDecodeError, RecursionError):
                return _WriteOutcome("conflict")
        return _WriteOutcome("rejected")
    if isinstance(item.payload, dict) and item.payload.get("kind") == "episodic":
        if vector_store is None:
            return _WriteOutcome("rejected")
        text = str(item.payload["text"])
        if vector_store.has_episodic_text(text):
            return _WriteOutcome("existing")
        # Embed OFF the request. Inference cost grows with text length (~0.4s per
        # 2000-char chunk on CPU), and import writes hundreds of chunks, so an
        # inline embed makes the user watch a spinner for minutes. The row is
        # keyword-searchable immediately and the caller schedules the backfill
        # sweep that fills the vector in (see ``schedule_embedding_backfill``).
        # Batching is NOT the alternative: measured on real import text,
        # ``embed_batch`` is ~25% SLOWER than looping ``embed`` because one
        # 2000-char chunk already fills the model's micro-batch.
        written = vector_store.write_episodic(
            text,
            tags=["imported", item.source_id],
            importance=float(item.payload["importance"]),
            source="import",
            preserve_existing=True,
            defer_embedding=True,
        )
        if written:
            return _WriteOutcome("imported")
        present = vector_store.has_episodic_text(text)
        return _WriteOutcome("existing" if present else "rejected")

    return _WriteOutcome("rejected")


def _write_workspace(
    item: _Item,
    data_home: Path,
    *,
    strategy: str = STRATEGY_SKIP,
) -> _WriteOutcome:
    workspace = Path(str(item.payload))
    try:
        workspace = workspace.resolve(strict=True)
    except OSError:
        # A configured workspace that no longer exists is a normal skip, not a
        # write failure.
        return _WriteOutcome("rejected")
    destination = data_home.resolve()
    if (
        not workspace.is_dir()
        or is_sensitive_path(str(workspace))
        or workspace == destination
        or destination in workspace.parents
    ):
        return _WriteOutcome("rejected")

    path = data_home / "config.json"
    data = _load_json_dict(path, fail_closed=True)
    workspaces = data.get("workspaces")
    if workspaces is None:
        workspaces = {}
        data["workspaces"] = workspaces
    if not isinstance(workspaces, dict):
        return _WriteOutcome("conflict")

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
                return _WriteOutcome("existing")
        except (OSError, RuntimeError):
            continue

    base_name = _SAFE_NAME_RE.sub("-", workspace.name).strip("-._").lower()
    base_name = base_name[:64] or f"imported-{item.source_id}"
    if base_name not in workspaces:
        workspaces[base_name] = {"dir": canonical}
        _write_json(path, data)
        return _WriteOutcome("imported")

    # The name is taken by a DIFFERENT directory. Deriving a suffixed name is a
    # rename, so it now requires the user to have asked for one; a plain skip
    # reports the collision instead of quietly inventing a name.
    if strategy != STRATEGY_RENAME:
        return _WriteOutcome("conflict")
    for candidate in (
        f"{base_name}-{item.source_id}"[:64],
        f"{base_name[:55]}-{item.fingerprint[:8]}",
    ):
        if candidate not in workspaces:
            workspaces[candidate] = {"dir": canonical}
            _write_json(path, data)
            return _WriteOutcome("imported", renamed_to=candidate)
    return _WriteOutcome("conflict")


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


def _write_mcp(
    item: _Item,
    data_home: Path,
    user_home: Path,
    *,
    strategy: str = STRATEGY_SKIP,
    run_stamp: str = "",
) -> _WriteOutcome:
    path = data_home / "mcp.json"
    with _mcp_lock(path):
        data = _load_json_dict(path, fail_closed=True)
        if "mcpServers" not in data:
            servers: dict[str, Any] = {}
            data["mcpServers"] = servers
        else:
            servers = data["mcpServers"]
            if not isinstance(servers, dict):
                return _WriteOutcome("conflict")
        name = str(item.payload["name"])
        spec = item.payload["spec"]
        from kiro_crew.mcp_discovery import configured_mcp_aliases

        reserved = configured_mcp_aliases(data_home=data_home, user_home=user_home)

        def _install(target_name: str) -> None:
            servers[target_name] = spec
            _write_json(path, data)

        if name not in servers:
            # An alias collision means some OTHER effective MCP source already
            # owns this name, so writing it here would shadow a server the user
            # did not import. A rename is a legitimate way out.
            if mcp_server_alias(name) not in reserved:
                _install(name)
                return _WriteOutcome("imported", destination_key=name)
        elif servers[name] == spec:
            return _WriteOutcome("existing", destination_key=name)

        if strategy == STRATEGY_RENAME:
            for candidate in _rename_candidates(name, item):
                if candidate in servers:
                    if servers[candidate] == spec:
                        return _WriteOutcome(
                            "existing",
                            renamed_to=candidate,
                            destination_key=candidate,
                        )
                    continue
                if mcp_server_alias(candidate) in reserved:
                    continue
                _install(candidate)
                return _WriteOutcome(
                    "imported",
                    renamed_to=candidate,
                    destination_key=candidate,
                )
            return _WriteOutcome("conflict")

        if strategy == STRATEGY_OVERWRITE:
            # Only an entry WE can see in this file is replaceable; a name
            # reserved by another source is not ours to overwrite.
            if name not in servers:
                return _WriteOutcome("conflict")
            try:
                restored = _preserve_replaced_json(
                    {name: servers[name]},
                    _restore_dir(data_home, run_stamp, "mcp_servers")
                    # Scope by source + fingerprint: two selected sources can
                    # define the SAME server name, and a bare-name file would let
                    # the second overwrite clobber the user's only restore copy.
                    / item.source_id
                    / f"{_SAFE_NAME_RE.sub('-', name)}-{item.fingerprint[:8]}.json",
                )
            except OSError:
                logger.warning(
                    "Could not preserve the MCP server being replaced; refusing to overwrite",
                    exc_info=True,
                )
                return _WriteOutcome("conflict")
            _install(name)
            return _WriteOutcome(
                "imported",
                restored_to=restored,
                destination_key=name,
            )

        return _WriteOutcome("conflict")


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


def _skill_destination_key(source_id: str, name: str) -> str:
    """The single-occupancy identity a skill package occupies.

    A skill dir holds exactly ONE package per (source, name), so the ledger must
    keep one record for it. Without this, importing V1, overwriting with V2, then
    reverting the source to V1 leaves V1's stale fingerprint deduplicating the
    revert while V2 stays installed.
    """

    return f"skills:{source_id}/{name}"


def _skill_files_are_valid(files: Any) -> bool:
    if not isinstance(files, dict) or "SKILL.md" not in files:
        return False
    for relative, content in files.items():
        if not isinstance(relative, str) or not isinstance(content, str):
            return False
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return False
    return True


def _skill_tree_state(
    destination: Path,
    files: dict[str, str],
    data_home: Path,
) -> str:
    """Classify a candidate skill destination: absent / existing / conflict."""

    present = 0
    for relative, content in files.items():
        target = destination / Path(relative)
        if _has_symlink_component(target, data_home):
            return "rejected"
        if not target.exists():
            continue
        present += 1
        try:
            if target.read_bytes() != content.encode("utf-8"):
                return "conflict"
        except OSError:
            return "conflict"
    if present == len(files):
        # Every file we carry is present and identical -- but the destination may
        # also hold files we DON'T carry, i.e. ones the upstream source deleted.
        # Reporting "existing" there would leave the stale file installed forever,
        # so treat an extra file as a conflict the user resolves (overwrite
        # replaces the whole tree, which removes it).
        try:
            installed = {
                path.relative_to(destination).as_posix()
                for path in destination.rglob("*")
                if path.is_file()
            }
        except OSError:
            return "conflict"
        expected = {Path(relative).as_posix() for relative in files}
        return "existing" if installed == expected else "conflict"
    if present:
        return "conflict"
    # A destination dir that exists but holds none of our files is still occupied.
    if destination.exists() or _is_link_like(destination):
        return "conflict"
    return "absent"


def _install_skill_tree(destination: Path, files: dict[str, str], data_home: Path) -> str:
    """Stage the package in a sibling temp dir and move it into place atomically."""

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
        # Re-check immediately before the move: the plan-time check is a TOCTOU
        # window, and this is the last moment we can still refuse.
        if _has_symlink_component(destination, data_home):
            return "rejected"
        if destination.exists() or _is_link_like(destination):
            return "conflict"
        os.replace(staging, destination)
        return "imported"
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _write_skill(
    item: _Item,
    data_home: Path,
    *,
    strategy: str = STRATEGY_SKIP,
    run_stamp: str = "",
) -> _WriteOutcome:
    files = item.payload.get("files")
    if not _skill_files_are_valid(files):
        return _WriteOutcome("rejected")
    name = str(item.payload["name"])
    root = data_home / "skills" / "imported" / item.source_id
    destination = root / name
    if _has_symlink_component(destination, data_home):
        return _WriteOutcome("rejected")

    state = _skill_tree_state(destination, files, data_home)
    if state == "rejected":
        return _WriteOutcome(state)
    if state == "existing":
        return _WriteOutcome(
            state,
            destination_key=_skill_destination_key(item.source_id, name),
        )
    if state == "absent":
        return _WriteOutcome(
            _install_skill_tree(destination, files, data_home),
            destination_key=_skill_destination_key(item.source_id, name),
        )

    # state == "conflict": a different package already occupies this name.
    if strategy == STRATEGY_RENAME:
        for candidate in _rename_candidates(name, item):
            alternate = root / candidate
            if _has_symlink_component(alternate, data_home):
                continue
            alternate_state = _skill_tree_state(alternate, files, data_home)
            if alternate_state == "existing":
                # The renamed copy is already installed and identical.
                return _WriteOutcome(
                    "existing",
                    renamed_to=candidate,
                    destination_key=_skill_destination_key(item.source_id, candidate),
                )
            if alternate_state == "absent":
                status = _install_skill_tree(alternate, files, data_home)
                return _WriteOutcome(
                    status,
                    renamed_to=candidate if status == "imported" else "",
                    destination_key=(
                        _skill_destination_key(item.source_id, candidate)
                        if status == "imported"
                        else ""
                    ),
                )
        return _WriteOutcome("conflict")

    if strategy == STRATEGY_OVERWRITE:
        # The restore copy is written FIRST and its failure aborts the
        # overwrite: an unrecoverable replace is worse than a reported conflict.
        try:
            restored = _preserve_replaced_tree(
                destination,
                _restore_dir(data_home, run_stamp, "skills") / item.source_id / name,
            )
        except (OSError, shutil.Error):
            logger.warning(
                "Could not preserve the skill being replaced; refusing to overwrite",
                exc_info=True,
            )
            return _WriteOutcome("conflict")
        # MOVE the old tree aside rather than deleting it in place. A partial
        # delete (a locked file on Windows) would otherwise leave the installed
        # skill mangled AND the install failing, so the user ends up with
        # neither version. Renaming is atomic: it either frees the name
        # completely or fails with the old tree still whole.
        # Pick an UNUSED retired path rather than clearing one. A leftover
        # retired tree from an interrupted overwrite is the only surviving copy of
        # that earlier version, so deleting it to make room would destroy exactly
        # what the move-aside exists to preserve. (Same reasoning as the
        # suffix-on-collision restore paths above.)
        base = destination.with_name(f".{destination.name}.replaced-{item.fingerprint[:8]}")
        retired = base
        for attempt in range(1, 100):
            if not (retired.exists() or _is_link_like(retired)):
                break
            retired = base.with_name(f"{base.name}-{attempt}")
        try:
            if retired.exists() or _is_link_like(retired):
                # 99 leftovers means something is badly wrong; refuse rather than
                # delete someone else's copy.
                return _WriteOutcome("conflict")
            os.replace(destination, retired)
        except OSError:
            logger.warning(
                "Could not move the skill being replaced out of the way; " "refusing to overwrite",
                exc_info=True,
            )
            return _WriteOutcome("conflict")

        def _restore_retired() -> None:
            # Put the original back so a failed replace is a no-op, not data loss.
            with contextlib.suppress(OSError):
                if not destination.exists():
                    os.replace(retired, destination)

        try:
            status = _install_skill_tree(destination, files, data_home)
        except BaseException:
            # A RAISE (disk full, permissions, cancellation) must restore too --
            # handling only the non-"imported" return left the original stranded
            # under its retired name with nothing installed.
            _restore_retired()
            raise
        if status != "imported":
            _restore_retired()
            return _WriteOutcome(status)
        shutil.rmtree(retired, ignore_errors=True)
        return _WriteOutcome(
            status,
            restored_to=restored,
            destination_key=_skill_destination_key(item.source_id, name),
        )

    return _WriteOutcome("conflict")


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


def _write_schedule(item: _Item, cron_service: Any) -> _WriteOutcome:
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
        return _WriteOutcome("existing" if job is None else "imported")
    for job in cron_service.list_jobs(include_disabled=True):
        if _same_schedule(job, payload):
            return _WriteOutcome("existing")
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
    return _WriteOutcome("imported")


def _write_settings(item: _Item, data_home: Path) -> _WriteOutcome:
    path = data_home / "config.json"
    data = _load_json_dict(path, fail_closed=True)
    changed = _merge_missing(data, item.payload)
    if not changed:
        return _WriteOutcome("existing")
    _write_json(path, data)
    return _WriteOutcome("imported")


def apply_import(
    plan: dict[str, Any],
    *,
    data_home: Path | None = None,
    cron_service: Any = None,
    vector_store: VectorMemoryStore | None = None,
    lesson_store: Any = None,
    conflict_strategy: str = STRATEGY_SKIP,
) -> dict[str, Any]:
    """Apply selected source/category pairs with merge-only, idempotent writes.

    ``conflict_strategy`` decides what happens when a destination already holds a
    DIFFERENT item under the same identity. ``skip`` (the default) leaves it
    alone and reports a conflict; ``rename`` installs alongside it under a
    derived name; ``overwrite`` replaces it after writing a restore copy under
    ``imports/replaced/<timestamp>/``. Only ``skills``, ``mcp_servers``, and
    ``workspaces`` have resolvable collisions — the rest are merge-only.
    """
    destination = Path(data_home) if data_home is not None else config_dir()
    destination.mkdir(parents=True, exist_ok=True)
    selected = _selected_pairs(plan)
    roots = _plan_roots(plan)
    user_homes = _plan_user_homes(plan)
    config_paths = _plan_private_paths(plan, "_config_paths")
    workspace_paths = _plan_private_paths(plan, "_workspace_paths")
    strategy = _normalize_strategy(conflict_strategy)
    # One restore dir per apply run, so everything a single import replaced is
    # found together. Stamped once here rather than per item.
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ledger_path = destination / _LEDGER_RELATIVE_PATH
    ledger = _load_ledger(ledger_path)
    records = ledger["records"]
    # The ledger is rewritten WHOLE (atomic temp-file + rename), so flushing it
    # once per item is O(n**2) in serialization and rename cost for a large
    # import. Flush once per source/category instead, and once more in the
    # ``finally`` below, so an interrupted apply still cannot re-import an item
    # it already wrote.
    ledger_dirty = False
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

    if cron_service is None and any(category == "schedules" for _source, category in selected):
        from kiro_crew.cron import CronService

        cron_service = CronService(base_dir=destination)
    if lesson_store is None and any(category == "instructions" for _source, category in selected):
        lesson_store = LessonStore(base_dir=destination)

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

    def _flush_ledger() -> None:
        nonlocal ledger_dirty
        if ledger_dirty:
            _write_json(ledger_path, ledger)
            ledger_dirty = False

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
                # The ledger is a fast path, NOT the authority: it says "this
                # exact item was imported once", which is only equivalent to
                # "the destination still holds it" for categories that cannot be
                # replaced afterwards. For a single-occupancy destination an
                # overwrite (or a later revert) moves the destination out from
                # under an older fingerprint, so the writer's own destination
                # check has to decide. It reports ``existing`` when the item
                # really is already there, which lands as ``deduplicated`` all
                # the same.
                if item.fingerprint in records and category not in _REPLACEABLE_CATEGORIES:
                    already_imported += 1
                    item_outcomes.append({**outcome, "outcome": "deduplicated"})
                    continue
                written = _WriteOutcome("skipped")
                try:
                    if category == "instructions":
                        written = _write_instruction(item, lesson_store, vector_store)
                    elif category == "memories":
                        written = _write_memory(item, destination, vector_store)
                    elif category == "workspaces":
                        written = _write_workspace(
                            item,
                            destination,
                            strategy=strategy,
                        )
                    elif category == "mcp_servers":
                        written = _write_mcp(
                            item,
                            destination,
                            scan.user_home,
                            strategy=strategy,
                            run_stamp=run_stamp,
                        )
                    elif category == "skills":
                        written = _write_skill(
                            item,
                            destination,
                            strategy=strategy,
                            run_stamp=run_stamp,
                        )
                    elif category == "schedules":
                        written = _write_schedule(item, cron_service)
                    elif category == "settings":
                        written = _write_settings(item, destination)
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
                status = written.status
                # Only set when a strategy actually took effect, so a plain skip
                # apply reports exactly the shape it did before strategies existed.
                details = {
                    key: value
                    for key, value in (
                        ("renamed_to", written.renamed_to),
                        ("restored_to", written.restored_to),
                    )
                    if value
                }
                if status in ("imported", "existing"):
                    _record_ledger(
                        ledger,
                        item,
                        destination_key=written.destination_key,
                    )
                    ledger_dirty = True
                    if status == "imported":
                        imported[category] += 1
                        item_outcomes.append({**outcome, **details, "outcome": "accepted"})
                    else:
                        already_imported += 1
                        item_outcomes.append({**outcome, **details, "outcome": "deduplicated"})
                elif status == "conflict":
                    conflicts.append(
                        {
                            "source_id": source_id,
                            "category_id": category,
                            "reason": "destination_conflict",
                            # Tell the client which strategies could resolve this
                            # one, so a retry is an informed choice.
                            "resolvable": category in STRATEGY_CATEGORIES,
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
            _flush_ledger()
    finally:
        _flush_ledger()
        if owned_vector_store is not None:
            # This store is ours alone, so no caller can schedule the sweep that
            # fills the deferred vectors — run it here before closing. Blocking is
            # correct on this path: it is the non-interactive one (CLI, tests),
            # with no user watching a spinner.
            if imported["memories"]:
                with contextlib.suppress(Exception):
                    owned_vector_store.backfill_missing_embeddings()
            owned_vector_store.close()

    return {
        "imported": imported,
        "imported_count": sum(imported.values()),
        "already_imported": already_imported,
        # Episodic rows are written with a NULL embedding (see _write_memory), so
        # a caller holding a shared store MUST schedule
        # ``backfill_missing_embeddings`` off the request. Zero when this run
        # owned its store and already swept above.
        "embedding_backfill_pending": (imported["memories"] if owned_vector_store is None else 0),
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
        "conflict_strategy": strategy,
    }
