"""Configuration loader for KiroCrew.

Config location: ~/.kirocrew/config.json (overridden by KIROCREW_HOME)
Credentials:    ~/.kirocrew/.env (overridden by KIROCREW_HOME)

KiroCrew is KiroACP-only: the sole provider is the ACP adapter driving the
kiro-cli backend. This module handles session timeouts, hook rules, and the
dashboard URL via the config file. (The dashboard *port* is set with the
``KIROCREW_PORT`` env var, not a config key.)
"""

from __future__ import annotations

import json
import logging
import math
import os
import re as _re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew import __version__, model_registry

# Pure path primitives live in the leaf module ``config.paths`` (stdlib-only,
# no ``kiro_crew`` imports) so the modules that only need ``config_dir()`` can
# import them from there without transitively pulling in the full loader (DTOs,
# schema validation, the process-global cache, and the provider factory).
# Re-exported here for backward compatibility — existing callers keep importing
# these from ``kiro_crew.config.loader``.
#
# The *dir-derived* helpers (config_path, workspace_root, workspace_dir_for, …)
# stay defined below in this module, not in the leaf, so their ``config_dir()``
# calls resolve in this namespace and remain redirectable via
# ``patch("kiro_crew.config.loader.config_dir", ...)`` (used across the suite).
from kiro_crew.config.paths import (  # noqa: F401
    _WORKSPACE_DIR_NAME,
    CONFIG_DIR_NAME,
    OUTBOX_DIR_NAME,
    _default_workspace_base,
    _safe_dir_name,
    config_dir,
    config_package_dir,
    ensure_data_home,
    kiro_agents_dir,
)

# Schema validation + the validated-data cache live in ``config.validation``.
# Re-exported here for backward compatibility — callers and tests still
# reference these as ``kiro_crew.config.loader.X`` (e.g. the cache tests patch
# ``kiro_crew.config.loader._validate_config_data``). ``validate_config_data``
# is aliased to the historical private name ``_validate_config_data``. The cache
# fingerprint (``_config_fingerprint``) deliberately stays in this module — see
# its definition below.
from kiro_crew.config.validation import (  # noqa: F401
    _CONFIG_CACHE,
    _CONFIG_CACHE_LOCK,
    _HAS_JSONSCHEMA,
    _actual_type_name,
    _apply_field_default,
    _dot_path_from_json_path,
    _get_help_text,
    _is_deprecated_path,
    _is_sensitive_path,
    _lookup_schema_node,
    _mask_value,
)
from kiro_crew.config.validation import validate_config_data as _validate_config_data  # noqa: F401
from kiro_crew.effort import is_valid_effort, model_supports_effort
from kiro_crew.instances.constants import DEFAULT_MAX_RECOVERY_ATTEMPTS as _DEFAULT_MAX_RECOVERY
from kiro_crew.instances.constants import DEFAULT_PROBE_FAILURE_THRESHOLD as _DEFAULT_PROBE_FAILS
from kiro_crew.instances.constants import DEFAULT_RECOVER_BACKOFF_MAX_SECS as _DEFAULT_BACKOFF_MAX
from kiro_crew.instances.constants import DEFAULT_SSH_COMPRESSION as _DEFAULT_SSH_COMPRESSION
from kiro_crew.instances.constants import DEFAULT_TUNNEL_BASE_PORT as _DEFAULT_TUNNEL_BASE_PORT
from kiro_crew.instances.constants import DEFAULT_WARM_SET_CAP as _DEFAULT_WARM_SET_CAP
from kiro_crew.instances.constants import MAX_RECOVERY_ATTEMPTS_CEILING as _MAX_RECOVERY_CEILING
from kiro_crew.instances.constants import (
    RECOVER_BACKOFF_MAX_CEILING_SECS as _RECOVER_BACKOFF_CEILING,
)
from kiro_crew.mcp_gateway.rewriter import default_overlay_dir, default_socket_path

logger = logging.getLogger(__name__)

# Top-level config.json sections this core models AND round-trips through
# to_dict(). Any other top-level key found at load() is captured into
# KiroCrewConfig._extra_sections and re-emitted by to_dict() so an
# edition-contributed section (written by a companion) survives the save()/PATCH
# round-trip instead of being silently dropped.
#
# INVARIANT: this set must equal the top-level keys to_dict() emits (guarded by
# test_config_extra_sections_roundtrip's parity test). It is the *emitted* set,
# not merely the *parsed* set: a section this core parses into a field must ALSO
# be emitted by to_dict() to be listed here — otherwise it would be excluded
# from _extra_sections capture yet dropped by to_dict(), losing it on save().
_KNOWN_CONFIG_SECTIONS: frozenset = frozenset(
    {
        "agent",
        "session",
        "memory",
        "slack",
        "publish",
        "telegram",
        "discord",
        "webex",
        "dashboard",
        "tunnel",
        "hooks",
        "agents",
        "default_agent",
        "workspaces",
        "default_workspace",
        "memory_stores",
        "default_memory_store",
        "stt",
        "instances",
        "mcp_gateway",
        "taskrunner",
        "orchestrator",
        "watchdog",
        "messaging",
        "cron_history",
        "knowledge",
        "heartbeat",
        "skills",
        "telemetry",
        "snapshot_dir",
        "timezone",
        "auto_update",
        "registries",
    }
)

# Credential keys loaded from .env / environment
CRED_SLACK_APP_TOKEN = "SLACK_APP_TOKEN"
CRED_SLACK_BOT_TOKEN = "SLACK_BOT_TOKEN"
CRED_OWNER_ID = "KIROCREW_OWNER_ID"
CRED_WECOM_BOT_ID = "WECOM_BOT_ID"
CRED_WECOM_SECRET = "WECOM_SECRET"
CRED_TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
CRED_DISCORD_BOT_TOKEN = "DISCORD_BOT_TOKEN"
CRED_WEBEX_BOT_TOKEN = "WEBEX_BOT_TOKEN"
_CREDENTIAL_KEYS = (
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
    CRED_OWNER_ID,
    CRED_WECOM_BOT_ID,
    CRED_WECOM_SECRET,
    CRED_TELEGRAM_BOT_TOKEN,
    CRED_DISCORD_BOT_TOKEN,
    CRED_WEBEX_BOT_TOKEN,
)

DEFAULT_MODEL = "auto"
DEFAULT_SESSION_TIMEOUT = 3600  # 60 min
DEFAULT_MAX_PARALLEL_STEPS = (
    0  # 0 = auto: derive from agent.subagent_auto_max via compute_max_subagents
)

_DEFAULT_PORT = 5476

# KIROCREW_PORT is validated at CLI entry (cli.py main()).
# By the time loader.py is imported the env var is a valid int or absent.
DASHBOARD_PORT: int = int(os.environ.get("KIROCREW_PORT", _DEFAULT_PORT))


# Dir-derived path helpers (workspace_root, config_path, workspace_dir_for, …)
# build on the pure primitives imported from ``config.paths`` above. They live
# here — not in the leaf — so their ``config_dir()`` / ``_default_workspace_base()``
# lookups resolve in this module's namespace, keeping the
# ``patch("kiro_crew.config.loader.config_dir", ...)`` test seam working.


def _workspace_dir_file() -> Path:
    """Return the path to the saved workspace_dir file, respecting KIROCREW_HOME."""
    return config_dir() / "workspace_dir"


def _resolve_workspace_root(root: Path) -> Path:
    """Realpath-normalize a workspace root after ensuring it exists.

    On hosts with a symlinked ``$HOME``/workspace path (e.g. ``/home/<u> ->
    /local/home/<u>``, ``/home/<u>/workplace -> /workplace/<u>``) the symlink-form
    root and its resolved form name the same directory via different strings. The
    per-session work_dir built from this root is passed as the spawn cwd and
    persisted as ``cwd`` in session_map.json. If the stored cwd is the symlink form
    while the transcript is written under the resolved form, cold resume misses and
    silently falls back to a fresh session.

    Normalizing here, at the single source, makes the SAME resolved path flow into
    spawn cwd and the persisted session_map cwd so write and resume always agree.
    This mirrors the existing ``os.path.realpath`` in ``default_project_dir``.
    """
    root.mkdir(parents=True, exist_ok=True)
    return Path(os.path.realpath(str(root)))


def workspace_root() -> Path:
    """Return the top-level workspace root for LLM sessions and tasks.

    Resolution order:
    1. ``KIROCREW_WORKSPACE`` env var (used as-is, no subdirectory appended)
    2. Saved path in ``config_dir()/workspace_dir`` (written by ``kirocrew setup``)
    3. Platform default with ``kirocrew-workspace`` subdirectory

    The chosen root is realpath-normalized (see ``_resolve_workspace_root``) so
    sessions resume correctly on hosts with a symlinked home/workspace path.
    """
    override = os.environ.get("KIROCREW_WORKSPACE")
    if override:
        return _resolve_workspace_root(Path(override))
    if _workspace_dir_file().is_file():
        try:
            saved = _workspace_dir_file().read_text(encoding="utf-8").strip()
            if saved:
                return _resolve_workspace_root(Path(saved))
        except OSError:
            pass
    base = _default_workspace_base()
    return _resolve_workspace_root(base / _WORKSPACE_DIR_NAME)


def _safe_int(value: object, default: int) -> int:
    """Convert *value* to int, returning *default* on failure."""
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


def _safe_float(
    value: object,
    default: float,
    lo: float | None = None,
    hi: float | None = None,
) -> float:
    """Convert *value* to float, returning *default* on failure, clamped to [lo, hi].

    Non-finite results (NaN/Infinity) are replaced with *default* — NaN compares
    false against any bound so it would silently bypass clamping (e.g. a
    configured ``tips_cadence_hours: NaN`` would permanently suppress tips).
    """
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        # OverflowError: json parses arbitrarily large ints fine, but float()
        # on a several-hundred-digit int raises — must not crash config load.
        result = default
    if not math.isfinite(result):
        result = default
    if lo is not None and result < lo:
        result = lo
    if hi is not None and result > hi:
        result = hi
    return result


def _session_work_dir(session_key: str | None) -> Path:
    """Return a per-session subdirectory under workspace_root()."""
    root = workspace_root()
    if session_key:
        return root / _safe_dir_name(session_key)
    return root / "_default"


def outbox_dir() -> Path:
    """Return the outbox directory for agent-to-user file delivery."""
    d = workspace_root() / OUTBOX_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "config.json"


def config_local_path() -> Path:
    """Return path to config.local.json — user overrides that survive upgrades."""
    return config_dir() / "config.local.json"


def denied_commands_path() -> Path:
    """Return path to denied_commands.json — the denied-command opt-out state.

    This is a KEYSTONE trust-root file (on ``security._SENSITIVE_HOME_DIRS``):
    it holds ``{disable_all, disabled_ids, user_added}``, the user's opt-out from
    the built-in deny ceiling. It lives OUTSIDE the agent-readable
    ``config.json`` precisely so an auto-approved/YOLO agent shell cannot write
    it (via any shell trick) and disable its own deny ceiling. Only the operator
    edits it out-of-band — through the dashboard ``/api/security/…`` endpoints,
    which do not route through the agent tool gate. Respects ``KIROCREW_HOME``.
    """
    return config_dir() / "denied_commands.json"


def read_local_secret() -> str:
    """Read ``<config_dir>/.local_secret`` (the gateway IPC secret), or ``""``.

    Single home for the secret-file read that callers (cron scripts, MCP tool
    bridges, CLI) need to authenticate to the gateway's internal API. Returns
    empty string if the file is absent/unreadable.
    """
    try:
        return (config_dir() / ".local_secret").read_text().strip()
    except OSError:
        return ""


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge *overlay* into *base*, returning a new dict.

    - Dict values are merged recursively
    - All other types in overlay replace base values
    - Keys in overlay not in base are added
    """
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _subtract_overlay(merged: dict, overlay: dict) -> dict:
    """Remove leaf values from *merged* that are owned by the overlay.

    For nested dicts, recurse. For leaf keys present in both overlay and
    merged with the same value, remove from the result so they only live
    in config.local.json.
    """
    result = dict(merged)
    for key, ov_value in overlay.items():
        if key not in result:
            continue
        if isinstance(ov_value, dict) and isinstance(result[key], dict):
            cleaned = _subtract_overlay(result[key], ov_value)
            if cleaned:
                result[key] = cleaned
            else:
                del result[key]
        elif result[key] == ov_value:
            del result[key]
    return result


def _raw_config() -> dict:
    """Load raw config.json as dict (cached per process)."""
    p = config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def workspace_dir_for(workspace: str | None = None) -> Path:
    """Resolve a named workspace to its directory path.

    Reads the ``dir`` field from ``WorkspaceConfig`` objects (new structured
    format) or falls back to raw string values (legacy flat format).

    Values starting with ``/`` or ``~`` are treated as absolute paths.
    Otherwise the value is relative to ``config_dir()`` (``~/.kirocrew/``).
    Unmapped workspace names fall back to ``"workspace"``.
    """
    data = _raw_config()
    ws = workspace or data.get("default_workspace", "default")
    mapping = data.get("workspaces", {})
    raw_value = mapping.get(ws, "workspace")

    # Extract the directory string from either format
    if isinstance(raw_value, dict):
        dirname = raw_value.get("dir", "workspace")
    elif isinstance(raw_value, str):
        dirname = raw_value
    else:
        dirname = "workspace"

    p = Path(dirname).expanduser()
    if p.is_absolute():
        return p
    return config_dir() / dirname


def default_project_dir(workspace: str | None = None) -> str:
    """Resolve the default project directory for a workspace.

    Returns the realpath of ``workspace_dir_for(workspace)`` if it exists and
    is not a sensitive path, otherwise returns ``""``.

    Used by chat_handlers (slot.project fallback) and session.py (pool cwd)
    to avoid duplicating the same resolution + validation logic.
    """
    from kiro_crew.security import is_sensitive_path  # circular import

    try:
        ws_dir = os.path.realpath(str(workspace_dir_for(workspace)))
        if os.path.isdir(ws_dir) and not is_sensitive_path(ws_dir):
            return ws_dir
    except Exception:
        pass
    return ""


def env_path() -> Path:
    return config_dir() / ".env"


def resolve_agent_config_path() -> Path:
    """Return defaults.json, preferring project-dir override for development.

    All modules that need the agent config path should call this instead
    of reimplementing the resolution chain.
    """
    proj = os.environ.get("KIROCREW_PROJECT_DIR")
    if proj:
        p = Path(proj) / "agents" / "defaults.json"
        if p.exists():
            return p
    return config_package_dir() / "defaults.json"


def _meta(label: str, help: str, **kwargs: object) -> dict:
    """Helper to build field metadata dicts with safe defaults."""
    return {"label": label, "help": help, **kwargs}


_BOT_NAME_MAX = 50
_BOT_NAME_RE = _re.compile(r"[^a-zA-Z0-9 _\-.]")


def _sanitize_bot_name(raw: str) -> str:
    """Sanitize bot_name: strip markdown, braces, limit length."""
    if not isinstance(raw, str):
        return ""
    name = raw.strip()[:_BOT_NAME_MAX]
    name = name.replace("{", "").replace("}", "")
    return _BOT_NAME_RE.sub("", name)


def _archive_retention_days(session_data: dict) -> int:
    """Resolve session.archive_retention_days, normalizing the disable sentinel.

    ``null`` (absent/None in JSON) and any negative value both mean "disable
    automatic cleanup"; both normalize to ``-1``.  A non-negative integer is the
    retention window in days.  Defaults to 30 when unset.
    """
    raw = session_data.get("archive_retention_days", 30)
    if raw is None:
        return -1
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return 30
    return val if val >= 0 else -1


# Process-isolation jail modes (``agent.jail``).  Single source of truth shared by
# ``_normalize_jail``, the ``AgentConfig.jail`` field metadata enum, and tests —
# a new mode added in one place can't silently normalize back to the default.
JAIL_MODE_AUTO = "auto"
JAIL_MODE_ON = "on"
JAIL_MODE_OFF = "off"
_VALID_JAIL_MODES = (JAIL_MODE_AUTO, JAIL_MODE_ON, JAIL_MODE_OFF)


@dataclass
class AgentConfig:
    approval_mode: str = field(
        default="auto",
        metadata=_meta("Approval Mode", "Tool approval mode.", enum=["auto", "interactive"]),
    )
    streaming: bool = field(
        default=True,
        metadata=_meta("Streaming", "Enable streaming responses."),
    )
    model: str = field(
        default=DEFAULT_MODEL,
        metadata=_meta("Model", "LLM model identifier. 'auto' resolves from agent config."),
    )
    provider: str = field(
        default="acp",
        metadata=_meta("Provider", "LLM provider backend (KiroACP / kiro-cli).", enum=["acp"]),
    )
    default_agent: str = field(
        default="",
        metadata=_meta("Default Agent", "Default agent name for new sessions."),
    )
    sandbox: str = field(
        default="off",
        metadata=_meta(
            "Sandbox",
            "Sandbox mode for ACP provider. Default 'off' defers isolation to "
            "kiro-cli's internal agent sandbox (kiro-cli >= 2.13). Set to 'auto' "
            "to re-enable KiroCrew's OS-level sandbox (namespace on Linux, "
            "sandbox-exec on macOS). The two layers are mutually exclusive on "
            "macOS (nested seatbelt causes EPERM).",
            enum=["auto", "off"],
        ),
    )
    sandbox_allow_no_isolation: bool = field(
        default=False,
        metadata=_meta(
            "Allow No-Isolation Fallback",
            "Acknowledge running the agent subprocess WITHOUT OS-level credential "
            "isolation when no sandbox backend is available (e.g. macOS >= 26, or "
            "Linux without user namespaces). When false (default), that fallback is "
            "logged as a loud SECURITY warning. When true, the operator has accepted "
            "the risk and it is logged at info level.",
        ),
    )
    sandbox_allow_unsandboxed_exec: bool = field(
        default=False,
        metadata=_meta(
            "Allow Unsandboxed Execution",
            "When true, allow agent subprocesses to execute without any sandbox "
            "backend (fail-open). When false (default), wrap_argv raises a "
            "RuntimeError if no sandbox backend is available and mode is not 'off', "
            "preventing unsandboxed execution entirely (fail-closed). This is "
            "distinct from sandbox_allow_no_isolation which only controls warning "
            "severity — this field controls whether execution proceeds at all.",
        ),
    )
    apps_allow_third_party: bool = field(
        default=True,
        metadata=_meta(
            "Allow Third-Party Apps",
            "Allow running third-party (non-builtin) app Python. App code runs with "
            "FULL gateway privileges (filesystem, network, in-memory credentials) and "
            "is NOT sandboxed — the permission system gates only the SDK tool surface. "
            "Defaults to true (apps are operator-installed). Set false to refuse both "
            "in-process module loads AND out-of-process backend spawns for any app "
            "outside apps/builtins/ until out-of-process isolation ships (CSE SEC-012).",
        ),
    )
    jail: str = field(
        default=JAIL_MODE_AUTO,
        metadata=_meta(
            "Jail",
            "Process-isolation jail mode for agent-bearing commands. 'auto' uses a "
            "jail when the active edition supplies a working backend (the public "
            "edition has none, so 'auto' and 'on' are no-ops there); 'off' disables "
            "it. Disable per-invocation with --no-jail or KIROCREW_NO_JAIL=1.",
            enum=list(_VALID_JAIL_MODES),
        ),
    )
    yolo: bool = field(
        default=False,
        metadata=_meta("YOLO Mode", "Skip tool approval confirmations."),
    )
    notify_override_expiry: bool = field(
        default=True,
        metadata=_meta(
            "Notify on Override Expiry",
            "DM the Slack owner when the time-limited safety override (YOLO) expires. "
            "Disable to silence the recurring expiry DM; the dashboard banner still shows.",
        ),
    )
    bot_name: str = field(
        default="",
        metadata=_meta(
            "Bot Name",
            "Custom name the bot identifies as in conversations. Leave empty for default.",
        ),
    )
    conductor_skill: bool = field(
        default=False,
        metadata=_meta(
            "Conductor Skill",
            "Enable agent delegation — loads conductor skill with agent roster.",
        ),
    )
    tool_search: bool = field(
        default=True,
        metadata=_meta(
            "MCP Tool Search",
            "Load MCP tool specs on demand (search-and-call) instead of sending "
            "every tool definition each turn, keeping the context window clear "
            "when many MCP servers are configured. kiro-cli backend only. When "
            "enabled, KiroCrew forces deferral always-on (minPct=0/minTokens=0) "
            "via the per-session kiro settings overlay; disabling reverts to "
            "sending full tool specs. No effect on an alternate ACP backend.",
        ),
    )
    session_sharing: bool = field(
        default=True,
        metadata=_meta(
            "Session Sharing",
            "Subagents reuse a shared ACP runtime instead of spawning a fresh "
            "kiro-cli process per subagent. Reduces startup from ~3-5s to ~200ms "
            "and memory from ~400MB to near-zero per subagent. Default ON for the "
            "kiro-cli backend; always off / ignored for an alternate ACP backend "
            "(which uses AcpClient). Set false to opt kiro back onto per-subagent "
            "processes.",
        ),
    )
    max_subagents: int = field(
        default=0,
        metadata=_meta(
            "Max SubAgents",
            "Maximum amount of subagents at one time. 0 = auto-size the cap at "
            "startup from host memory/CPU and a learned per-agent cost "
            "(see dynamic-subagent-sizing docs). Default; set a fixed cap by "
            "pinning an integer >= 3 (values of 1 or 2 are raised to 3 — a pin "
            "below 3 would disable auto-sizing and run under the default).",
        ),
    )
    spawn_min_memory_gb: float = field(
        default=4.0,
        metadata=_meta(
            "Spawn Min Memory GB",
            "Minimum available memory (GB) required to spawn a subagent. 0 disables the check.",
        ),
    )
    subagent_mem_buffer_pct: int = field(
        default=20,
        metadata=_meta(
            "SubAgent Memory Buffer %",
            "Percent of available memory and CPU reserved for the OS and other "
            "processes when auto-sizing the subagent cap (max_subagents=0).",
        ),
    )
    subagent_cost_gb: float = field(
        default=0.5,
        metadata=_meta(
            "SubAgent Memory Cost (GB)",
            "First-boot per-agent memory-cost fallback (GB) used to auto-size the "
            "cap until a learned value accumulates.",
        ),
    )
    subagent_cpu_cost_cores: float = field(
        default=1.0,
        metadata=_meta(
            "SubAgent CPU Cost (cores)",
            "First-boot per-agent CPU-cost fallback (cores) used to auto-size the "
            "cap until a learned value accumulates.",
        ),
    )
    subagent_auto_max: int = field(
        default=32,
        metadata=_meta(
            "SubAgent Auto-Size Max",
            "Ceiling on the auto-sized subagent cap (only applies when "
            "max_subagents=0). Stands in for the LLM-provider concurrency limit "
            "the local memory/CPU formula does not model. Ignored when "
            "max_subagents is set explicitly.",
        ),
    )
    subagent_spawn_stagger_secs: float = field(
        default=2.0,
        metadata=_meta(
            "SubAgent Spawn Stagger (seconds)",
            "Delay between successive subagent spawns (initial fill and queued "
            "drain) to bound cold-start CPU/memory spikes.",
        ),
    )
    subagent_max_turns: int = field(
        default=100,
        metadata=_meta("SubAgent Max Turns", "Default tool-call budget per subagent."),
    )
    subagent_timeout_secs: int = field(
        default=1800,
        metadata=_meta(
            "SubAgent Timeout (seconds)",
            "Wall-clock timeout per subagent execution. 0 uses hardcoded default (1800s).",
        ),
    )
    subagent_stall_idle_secs: int = field(
        default=120,
        metadata=_meta(
            "SubAgent Stall Idle (seconds)",
            "Seconds with no stream activity before a running subagent is surfaced "
            "as 'stalled' in the running-card. 0 uses hardcoded default (120s).",
        ),
    )
    completion_keep: str = field(
        default="head",
        metadata=_meta(
            "Completion Keep",
            "Which end of the subagent transcript to keep in the completion event "
            "injected into the parent session. Three values: 'head' (first N chars), "
            "'tail' (last N chars), 'both' (head + middle marker + tail). The full "
            "transcript stays in result.txt until cleanup; use spawn_status MCP tool "
            "to read it.",
            enum=["head", "tail", "both"],
        ),
    )
    completion_keep_chars: int = field(
        default=3000,
        metadata=_meta(
            "Completion Keep Chars",
            "Maximum characters retained in the completion event after applying "
            "completion_keep. 0 disables truncation entirely. Default 3000.",
        ),
    )
    subagent_result_ttl_secs: int = field(
        default=3600,
        metadata=_meta(
            "SubAgent Result TTL (seconds)",
            "How long a delivered subagent's result.txt is retained before the "
            "reaper prunes it. The completion event returns a summary plus this "
            "file path; the parent reads the full transcript on demand (read / "
            "grep / spawn_status) within this window instead of re-running the "
            "subagent. 0 prunes on the next reaper sweep. Default 3600 (1h).",
        ),
    )
    subagent_cwd_allowed_roots: list[str] = field(
        default_factory=lambda: ["~/workspace", "~/workplace"],
        metadata=_meta(
            "SubAgent CWD Allowed Roots",
            "Directory roots under which spawn_run's cwd parameter is permitted. "
            "Values support ~ expansion. Empty list disables cwd overrides.",
        ),
    )
    max_channels: int = field(
        default=1,
        metadata=_meta("Max Channels", "Maximum concurrent agent channels (1-5)."),
    )
    max_channel_agents: int = field(
        default=3,
        metadata=_meta("Max Channel Agents", "Maximum agents per channel (1-10)."),
    )
    log_level: str = field(
        default="WARNING",
        metadata=_meta(
            "Log Level",
            "Persistent log level for the kiro_crew logger. "
            "Applied at startup; overridden by --verbose CLI flag.",
            enum=["DEBUG", "INFO", "WARNING", "ERROR"],
        ),
    )
    soft_stop_budget_secs: float = field(
        default=10.0,
        metadata=_meta(
            "Soft-Stop Budget",
            "Seconds to wait for cooperative cancel before hard-killing the session.",
        ),
    )

    def __post_init__(self) -> None:
        self.max_channels = max(1, min(5, self.max_channels))
        self.max_channel_agents = max(1, min(10, self.max_channel_agents))
        # Clamp to [0.5, 60.0] to match ``KiroCrewConfig.load()`` behavior
        # (dashboard PATCH and YAML loader both clamp rather than raise).
        clamped = max(0.5, min(60.0, float(self.soft_stop_budget_secs)))
        if clamped != self.soft_stop_budget_secs:
            logger.warning(
                "soft_stop_budget_secs=%s out of range [0.5, 60.0]; clamped to %s",
                self.soft_stop_budget_secs,
                clamped,
            )
            self.soft_stop_budget_secs = clamped


@dataclass
class SessionConfig:
    timeout_secs: int = field(
        default=DEFAULT_SESSION_TIMEOUT,
        metadata=_meta("Session Timeout", "Idle session timeout in seconds."),
    )
    empty_response_auto_continue: bool = field(
        default=True,
        metadata=_meta(
            "Auto-Continue on Empty Response",
            "After the model returns an empty response twice in a row, "
            "automatically send one 'continue' nudge on the same session "
            "(transcript-visible, bounded to once per user message).",
        ),
    )
    autocompact_pct: float = field(
        default=90.0,
        metadata=_meta(
            "Auto-Compact Threshold",
            "Context usage percentage at which auto-compaction triggers (5-90).",
        ),
    )
    pool_size: int = field(
        default=0,
        metadata=_meta(
            "Warm Pool Size",
            "Number of pre-spawned kiro-cli processes kept ready for instant session start. 0 disables.",
        ),
    )
    pool_agent: str = field(
        default="",
        metadata=_meta(
            "Warm Pool Agent",
            "Agent name for warm pool processes. Empty string uses agent.default_agent.",
        ),
    )
    pool_ttl_secs: int = field(
        default=1800,
        metadata=_meta(
            "Warm Pool TTL",
            "Max age in seconds for pooled processes. Stale processes are discarded at claim time. 0 disables.",
        ),
    )
    archive_retention_days: int = field(
        default=30,
        metadata=_meta(
            "Archive Retention (days)",
            "Days to keep compacted/rotated session archives before auto-cleanup. "
            "-1 disables cleanup (manage deletion manually).",
            nullable=True,
        ),
    )
    watchdog_rss_max_mb: int = field(
        default=0,
        metadata=_meta(
            "Watchdog RSS Limit (MiB)",
            "Recycle a session when its process tree resident memory exceeds "
            "this many MiB. 0 disables (default). Busy sessions (turn in "
            "flight) are never recycled.",
        ),
    )


@dataclass
class TaskRunnerConfig:
    max_parallel_steps: int = field(
        default=DEFAULT_MAX_PARALLEL_STEPS,
        metadata=_meta(
            "Max Parallel Steps",
            "Maximum task steps to run in parallel. 0 = auto (the host-safe cap from agent.subagent_auto_max, clamped to memory/CPU). A positive value only *lowers* concurrency — it is capped at the auto maximum and can never exceed the host-safe limit.",
        ),
    )
    workspace_dir: str = field(
        default="",
        metadata=_meta(
            "Workspace Folder",
            "Absolute path where task runner executions run. When set, "
            "every execution operates in this folder instead of a per-run scratch "
            "directory, so the task runner works on the intended target location. "
            "Empty = use the default per-run workspace directory.",
        ),
    )


@dataclass
class OrchestratorConfig:
    stage_timeout_seconds: int = field(
        default=1800,
        metadata=_meta(
            "Stage Timeout", "Max seconds per stage before auto-run stops. Default 30 min."
        ),
    )


@dataclass
class MessagingConfig:
    use_transport: bool = field(
        default=True,
        metadata=_meta(
            "Use Transport",
            "Route inbound Slack messages through the SlackTransport → TurnDriver → "
            "SlackRenderer channel-neutral path instead of the native handle_message "
            "monolith. Default ON in KiroCrew (the transport abstraction is the canonical "
            "path, shared with future channels). Set to false to fall back to the legacy "
            "native handler.",
        ),
    )
    dm_scope: str = field(
        default="per-channel-peer",
        metadata=_meta(
            "DM Session Scope",
            "How direct-message conversations map to sessions. 'per-channel-peer' "
            "(default) keeps one session per (channel, user), so the same person on "
            "Telegram vs WeCom stays isolated. 'unified' collapses all DMs into one "
            "shared session per agent for cross-surface continuity.",
        ),
    )
    idle_reset_minutes: int = field(
        default=0,
        metadata=_meta(
            "DM Idle Reset (minutes)",
            "Start a fresh session generation when a DM arrives after this many "
            "minutes of inactivity. 0 (default) disables idle reset.",
        ),
    )
    daily_reset_hour: int = field(
        default=-1,
        metadata=_meta(
            "DM Daily Reset Hour",
            "Local-time hour (0-23) at which the next DM starts a fresh session "
            "generation once per day. -1 (default) disables daily reset.",
        ),
    )
    queue_mode: str = field(
        default="steer",
        metadata=_meta(
            "DM Queue Mode",
            "How a DM that arrives while a turn is running is handled. 'steer' "
            "(default) folds it into the running reply; 'queue' holds it and runs "
            "it after the current turn finishes.",
        ),
    )

    def __post_init__(self) -> None:
        # Fail safe on hand-edited values (mirrors WeComConfig): an unknown scope
        # or mode falls back to the safe default, and the reset windows clamp to
        # valid ranges so a bad config can't wedge dispatch.
        if self.dm_scope not in ("per-channel-peer", "unified"):
            self.dm_scope = "per-channel-peer"
        if self.queue_mode not in ("steer", "queue"):
            self.queue_mode = "steer"
        self.idle_reset_minutes = max(0, self.idle_reset_minutes)
        if not 0 <= self.daily_reset_hour <= 23:
            self.daily_reset_hour = -1


@dataclass
class CronHistoryConfig:
    cron_summary_cap: int = field(
        default=200,
        metadata=_meta("Summary Cap", "Max characters for run summary field."),
    )
    cron_trace_cap_kb: int = field(
        default=50,
        metadata=_meta("Trace Cap KB", "Max kilobytes for run trace field."),
    )
    cron_max_records_per_job: int = field(
        default=100,
        metadata=_meta("Max Records Per Job", "Max history records kept per job file."),
    )
    cron_max_index_records: int = field(
        default=2000,
        metadata=_meta("Max Index Records", "Max records in the global index."),
    )


@dataclass
class MemoryConfig:
    embedding_provider: str = field(
        default="llama_cpp",
        metadata=_meta(
            "Embedding Provider",
            "Vector embedding backend (always-on). In-process via vendored llama-cpp-python. "
            "Legacy configs with 'ollama' or 'none' are auto-migrated to 'llama_cpp'.",
            enum=["llama_cpp"],
        ),
    )
    embedding_dim: int = field(
        default=1024,
        metadata=_meta("Embedding Dimension", "Dimensionality of embedding vectors."),
    )
    embed_model_url: str = field(
        default="",
        metadata=_meta(
            "Embedding Model URL",
            "Override HTTPS URL for the embedding model GGUF download (mirrored/airgapped "
            "deployments). Empty uses the public KiroCrew CDN default; the "
            "KIROCREW_EMBED_MODEL_URL env var wins over both. The download is "
            "sha256-verified regardless of source.",
        ),
    )
    semantic_confidence_threshold: float = field(
        default=0.8,
        metadata=_meta(
            "Semantic Confidence Threshold",
            "Minimum similarity score for semantic search results.",
        ),
    )
    episodic_dedup_threshold: float = field(
        default=0.88,
        metadata=_meta(
            "Episodic Dedup Threshold",
            "Similarity threshold for deduplicating episodic memories.",
        ),
    )
    episodic_max_results: int = field(
        default=8,
        metadata=_meta("Episodic Max Results", "Maximum episodic memory results per query."),
    )
    episodic_max_count: int = field(
        default=10_000,
        metadata=_meta("Episodic Max Count", "Maximum total episodic memories stored."),
    )
    semantic_keys: list[str] = field(
        default_factory=list,
        metadata=_meta("Semantic Keys", "Keys to index for semantic search."),
    )
    history_idle_hours: float = field(
        default=3.0,
        metadata=_meta(
            "History Idle Hours",
            "Hours of inactivity before history consolidation.",
        ),
    )
    history_max_days: int = field(
        default=365,
        metadata=_meta("History Max Days", "Maximum days of history to retain."),
    )
    migrated: bool = field(
        default=False,
        metadata=_meta("Migrated", "Whether memory has been migrated to vector store."),
    )


#: Default artifact kinds eligible for Knowledge Library auto-ingest. These are
#: the substantial-document kinds whose content the KB file reader can extract
#: (routed through the same reader as folders/uploads): markdown/text/json read
#: as text, and html goes through HTML prose extraction. ``widget`` is excluded
#: -- widgets/dashboards are UI, not documents (and a remote widget round-trips
#: back to kind="widget" via the publish/clone unwrap, so this also skips cloned
#: widgets). ``svg`` is excluded because ``.svg`` is not in
#: ``FileReader.SUPPORTED``.
DEFAULT_AUTO_INGEST_ARTIFACT_KINDS = ["markdown", "text", "html", "json"]


def _coerce_embedding_provider(raw: str) -> str:
    """Normalize legacy or unknown embedding_provider values.

    Embeddings are always-on: every value coerces to ``"llama_cpp"``. Old configs
    may carry ``"ollama"`` (previous runtime) or ``"none"`` (previously-disabled);
    both are transparently upgraded. Unknown values also coerce so a config file
    from a newer/older version never crashes.
    """
    return "llama_cpp"


@dataclass
class KnowledgeConfig:
    """Knowledge Library ingestion settings.

    Embedding/retrieval settings live under :class:`MemoryConfig` (shared with
    the memory subsystem via ``create_embedder_from_config``); this section
    holds Knowledge-Library-specific ingestion toggles.
    """

    auto_ingest_artifacts: bool = field(
        default=True,
        metadata=_meta(
            "Auto-Ingest Artifacts",
            "Automatically ingest content-bearing local artifacts (markdown/text "
            "documents you save and iterate) into the Knowledge Library so they "
            "become searchable, keep them in sync as the artifact changes, and "
            "remove them from the Library when the artifact is deleted. They "
            "appear as a single aggregate 'Artifacts' source. On by default.",
        ),
    )
    auto_ingest_artifact_kinds: list[str] = field(
        default_factory=lambda: list(DEFAULT_AUTO_INGEST_ARTIFACT_KINDS),
        metadata=_meta(
            "Auto-Ingest Artifact Kinds",
            "Artifact kinds eligible for auto-ingest. Defaults to substantial "
            "document kinds (markdown, text, html, json); widget is excluded "
            "(UI/dashboards, not documents) and svg has no reader support.",
        ),
    )
    max_ingest_file_mb: float = field(
        default=100.0,
        metadata=_meta(
            "Max Ingest File Size (MB)",
            "Per-file size cap for Knowledge Library ingestion. Oversized files "
            "are skipped with a WARNING naming the file instead of being chunked "
            "-- chunking a very large file (e.g. a tens-of-MB CSV->MD conversion) "
            "is CPU-bound and previously hung gateway startup. Set 0 to disable "
            "the cap.",
        ),
    )
    embed_timeout_secs: float = field(
        default=10.0,
        metadata=_meta(
            "Embed Timeout (seconds)",
            "Per-request timeout for the Knowledge-Library embedder. Raise it "
            "when a large chunk times out on a cold Ollama model load (the embed "
            "then never completes and the item is retried every maintenance "
            "pass). 0 or unset keeps the built-in 10s default.",
        ),
    )
    embed_content_budget: int = field(
        default=0,
        metadata=_meta(
            "Embed Content Budget (chars)",
            "Safety bound (chars) on chunk content folded into an item embedding. "
            "0 or unset keeps the built-in default (a generous backstop for "
            "pathological un-chunked input); raise/lower only to tune truncation.",
        ),
    )
    pool_idle_ttl_secs: int = field(
        default=300,
        metadata=_meta(
            "Pool Idle TTL (secs)",
            "Seconds the document-extraction worker pool may sit fully idle "
            "before it is scaled to zero (all workers shut down, freeing ~1GB "
            "of held process trees); the next ingest respawns them lazily. "
            "0 keeps the workers warm indefinitely.",
        ),
    )
    auto_ingest_doc_links: bool = field(
        default=False,
        metadata=_meta(
            "Auto-Ingest Doc Links",
            "Automatically ingest documents referenced by links pasted into chat "
            "whose host is in the doc-ingest allowlist. Off by default. An edition "
            "that supplies a doc-link scanner (via the dashboard on_user_message "
            "seam) uses this + the host allowlist below; inert in the public build "
            "unless a link scanner is wired.",
        ),
    )
    doc_ingest_hosts: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Doc-Ingest Host Allowlist",
            "Exact hostnames whose links may be auto-ingested when "
            "auto_ingest_doc_links is on. Empty = ingest nothing (SSRF-safe "
            "deny-by-default): a link is only fetched if its host is an exact "
            "member of this list. Prevents a pasted link to an internal metadata "
            "endpoint or arbitrary host from being fetched.",
        ),
    )


@dataclass
class SlackConfig:
    allowed_users: list[dict] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Users",
            "List of Slack users allowed to interact. Each entry: {slack_id, name}.",
        ),
    )
    tracking_channels: list[dict] = field(
        default_factory=list,
        metadata=_meta(
            "Tracking Channels",
            "Slack channels to monitor. Each entry: {channel_id, name}.",
        ),
    )
    open_channels: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Open Channels",
            "Channel IDs where all users are authorized without allowlist.",
        ),
    )
    command: str = field(
        default="kirocrew",
        metadata=_meta("Command", "Slack slash command trigger word."),
    )
    forward_to_agent_callback: str = field(
        default="",
        metadata=_meta(
            "Forward to Agent Callback",
            "Callback ID for the 'Forward to Agent' message shortcut. "
            "Must match the callback_id configured in your Slack app manifest. "
            "Leave empty to disable the feature.",
            tags=["slack"],
        ),
    )
    trusted_bot_ids: set[str] = field(
        default_factory=set,
        metadata=_meta(
            "Trusted Bot IDs",
            "Bot IDs allowed to bypass the bot filter for multi-node mesh communication.",
            tags=["slack"],
        ),
    )
    allowed_enterprise_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Enterprise IDs",
            "Slack Enterprise Grid org IDs to allow. Empty list allows all orgs (default-open).",
            tags=["slack"],
        ),
    )
    reactions: dict[str, str | None] = field(
        default_factory=dict,
        metadata=_meta(
            "Reactions",
            "Override phase reaction emojis. Valid keys: queued, thinking, coding, browsing, tool, done, error. "
            "Set a value to null to suppress that phase entirely.",
            tags=["slack"],
        ),
    )
    reactions_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Reactions Enabled",
            "Show phase-aware emoji reactions on Slack messages during processing.",
            tags=["slack"],
        ),
    )
    show_thinking: bool = field(
        default=True,
        metadata=_meta(
            "Show Thinking",
            "Post the model's thinking/reasoning as a thread reply in Slack. "
            "Disable to keep responses concise.",
            tags=["slack"],
        ),
    )
    home_tab_sessions_per_kind: int = field(
        default=5,
        metadata=_meta(
            "Home Tab Sessions Per Kind",
            "Max sessions shown per category (main chat / autopilot) in the Slack Home Tab.",
            tags=["slack"],
        ),
    )
    use_tunnel_url: bool = field(
        default=False,
        metadata=_meta(
            "Use Tunnel URL in Slack",
            "When true, dashboard links posted to Slack (e.g. via /kirocrew dashboard) "
            "use the tunnel URL if one is active. When false (default), "
            "Slack links always use the configured dashboard origin or host:port. "
            "Disabled by default until the tunnel mechanism is scaled for general use.",
            tags=["slack"],
        ),
    )


@dataclass
class PublishConfig:
    """Operator-facing controls for artifact publishing.

    Publishing an artifact to an external destination is provided by a
    ``publish_provider`` registered through the ``platform`` CPP seam
    (``PublishRegistry``). The public edition registers NO provider, so
    publishing is unavailable regardless of these settings; a companion edition
    registers a concrete destination.

    This ``allowed_destinations`` list is the STANDALONE operator's narrowing
    knob (default-open, mirroring ``SlackConfig.allowed_enterprise_ids``): empty
    means "allow every registered destination". It is enforced at the publish
    handler chokepoint IN ADDITION TO the governance ceiling
    (``capabilities.publish``) — like the Slack allowlist, config can only
    NARROW, never widen: a destination denied by the enterprise policy cannot be
    re-permitted here (the security policy is never merged from ``config.json``).
    """

    allowed_destinations: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Publish Destinations",
            "Publish-provider ids the operator permits (e.g. 'artifactory'). "
            "Empty list allows all registered destinations (default-open). "
            "Cannot widen past the enterprise governance ceiling.",
            tags=["publish"],
        ),
    )
    #: Extra filesystem roots (beyond the user's home dir) that an artifact may
    #: be relocated to point at (``artifact_relocate`` / the ``artifact_move`` MCP
    #: tool). Relocate is confined to the user home by default so an agent cannot
    #: aim an artifact at ``/etc/passwd`` or another user's files and exfiltrate
    #: them via a later artifact GET; each entry here widens the allowed set to an
    #: additional absolute root (e.g. a shared project dir). Paths are expanded +
    #: realpath-resolved; a relocate target must resolve under the home dir OR one
    #: of these roots (AND still pass the sensitive-path denylist).
    relocate_roots: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Artifact Relocate Roots",
            "Extra absolute filesystem roots an artifact may be relocated into, "
            "beyond your home directory. Empty = home-only (the secure default). "
            "The sensitive-path denylist (~/.aws, ~/.ssh, ~/.kirocrew, …) still "
            "applies inside every allowed root.",
            tags=["artifacts"],
        ),
    )


@dataclass
class DashboardConfig:
    url: str = field(
        default="",
        metadata=_meta(
            "Dashboard URL",
            "Public URL for the dashboard (used in Slack links).",
        ),
    )
    restore_sessions: bool = field(
        default=False,
        metadata=_meta(
            "Restore Sessions",
            "Re-open recently active sessions on startup.",
        ),
    )
    restore_window_minutes: int = field(
        default=30,
        metadata=_meta(
            "Restore Window Minutes",
            "Time window (minutes) for session restoration (0-1440). 0 = restore all.",
        ),
    )
    bot_name: str = field(
        default="",
        metadata=_meta(
            "Bot Name",
            "Custom bot display name for the dashboard UI.",
        ),
    )
    avatar: str = field(
        default="",
        metadata=_meta(
            "Avatar",
            "Path to custom avatar image for the dashboard UI.",
        ),
    )
    merge_queued_messages: bool = field(
        default=False,
        metadata=_meta(
            "Merge Queued Messages",
            "Concatenate follow-up messages while the agent is busy instead of queueing them separately.",
        ),
    )
    mcp_probe_timeout_secs: int = field(
        default=15,
        metadata=_meta(
            "MCP Probe Timeout",
            "Seconds to wait for MCP server handshake during probe (5-120).",
        ),
    )
    widget_density: str = field(
        default="more",
        metadata=_meta(
            "Widget Density",
            "How aggressively the agent uses inline widgets. "
            "'more' encourages widgets for any visual content; "
            "'less' limits to only when markdown is clearly insufficient.",
            enum=["more", "less"],
        ),
    )
    tail_fork_enabled: bool = field(
        default=False,
        metadata=_meta(
            "Tail-only Fork",
            "When forking, keep only the messages after the chosen point. The "
            "earlier messages are dropped.",
        ),
    )
    auto_open_browser: bool = field(
        default=True,
        metadata=_meta(
            "Auto Open Browser",
            "Open the dashboard URL in the default browser on gateway startup.",
        ),
    )
    quick_send: bool = field(
        default=False,
        metadata=_meta(
            "Quick Send",
            "Click a suggested reply to send it instantly. Shift+Click to select multiple.",
        ),
    )
    session_grid: bool = field(
        default=False,
        metadata=_meta(
            "Session Grid (Split View)",
            "Opt-in: enable terminal-style split view to run multiple chat sessions side by side.",
        ),
    )
    terminal: dict = field(
        default_factory=lambda: {"enabled": True},
        metadata=_meta(
            "Terminal",
            "Terminal panel configuration. Set enabled=false to hide the CLI panel in the dashboard.",
        ),
    )
    default_project: str = field(
        default="",
        metadata=_meta(
            "Default Project",
            "Directory path used as the project for new chat tabs. Empty = workspace dir.",
        ),
    )
    theme_mode: str = field(
        default="",
        metadata=_meta(
            "Theme Mode",
            "Dashboard color mode preference: 'dark', 'light', or 'system'. "
            "Empty = unset (frontend falls back to localStorage or 'system').",
            enum=["", "dark", "light", "system"],
        ),
    )
    sso_login_flags: str = field(
        default="",
        metadata=_meta(
            "SSO Login Flags",
            "Flags passed to the SSO login command by an edition that supplies a "
            "real login handler (DashboardContributor.sso_login_handler). Empty = "
            "the edition default. Inert in the public build (the core /api/sso-login "
            "is a no-op stub); the companion validates the token allowlist when it "
            "uses them.",
        ),
    )
    theme_color: str = field(
        default="",
        metadata=_meta(
            "Theme Color",
            "Dashboard color theme slug (e.g. 'kiro', 'emerald', 'monokai'). "
            "Empty = unset (frontend falls back to localStorage or 'kiro').",
        ),
    )
    recent_tint_count: int = field(
        default=0,
        metadata=_meta(
            "Recent Session Tint Count",
            "Number of most-recently-active sessions to highlight in the sidebar with a "
            "graded accent stripe (0-10; 0 = off).",
        ),
    )
    onboarded: bool = field(
        default=False,
        metadata=_meta(
            "Onboarded",
            "Whether the user has completed the dashboard onboarding flow. "
            "When true, the 'Choose your look' modal is skipped on first load.",
        ),
    )
    tips_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Tips Enabled",
            "Show feature tip cards while the agent is thinking.",
        ),
    )
    tips_cadence_hours: float = field(
        default=6.0,
        metadata=_meta(
            "Tips Cadence Hours",
            "Minimum hours between showing a new tip.",
        ),
    )
    tips_snooze_hours: float = field(
        default=48.0,
        metadata=_meta(
            "Tips Snooze Hours",
            "Hours before a snoozed tip becomes eligible again.",
        ),
    )
    tips_recency_decay: float = field(
        default=0.6,
        metadata=_meta(
            "Tips Recency Decay",
            "Decay factor for weighted-random selection (0-1). Lower = stronger bias to newer tips.",
        ),
    )
    tips_model: str = field(
        default="claude-haiku-4.5",
        metadata=_meta(
            "Tips Model",
            "Model ID for tips generation (pinned to Haiku-class for cost efficiency).",
        ),
    )
    tips_explore_ratio: float = field(
        default=0.2,
        metadata=_meta(
            "Tips Explore Ratio",
            "Probability of picking a random catalog tip instead of personalized (0-1). Higher = more general discovery.",
        ),
    )


@dataclass
class KiroCrewAgentConfig:
    kiro_agent: str = field(
        default="",
        metadata=_meta("Kiro Agent", "Kiro agent name (modeId for session/set_mode)."),
    )
    workspace: str = field(
        default="default",
        metadata=_meta("Workspace", "Named workspace from the workspaces section."),
    )
    memory_store: str = field(
        default="default",
        metadata=_meta("Memory Store", "Named memory store from the memory_stores section."),
    )
    description: str = field(
        default="",
        metadata=_meta("Description", "Human-readable agent description."),
    )
    source: str = field(
        default="kirocrew",
        metadata=_meta("Source", "Agent origin: kirocrew or builtin."),
    )


@dataclass
class WorkspaceConfig:
    dir: str = field(
        default="workspace",
        metadata=_meta("Directory", "Workspace directory path."),
    )


@dataclass
class MemoryStoreConfig:
    description: str = field(
        default="",
        metadata=_meta("Description", "Human-readable purpose of this memory store."),
    )
    embedding_provider: str = field(
        default="",
        metadata=_meta(
            "Embedding Provider",
            "Override embedding backend for this store. Empty inherits from top-level memory "
            "(embeddings are always-on; per-store disable is not supported).",
            enum=["", "llama_cpp"],
        ),
    )


@dataclass
class ExternalRegistryConfig:
    """An external app registry source (org-owned repo with app.json files)."""

    name: str = field(
        default="",
        metadata=_meta("Name", "Human-readable registry name (e.g. 'identityservices')."),
    )
    repo: str = field(
        default="",
        metadata=_meta("Repo", "Git URL of the repo containing apps (https or ssh)."),
    )
    branch: str = field(
        default="mainline",
        metadata=_meta("Branch", "Git branch to read from."),
    )


@dataclass
class SkillsConfig:
    max_triggered: int = field(
        default=3,
        metadata=_meta("Max Triggered", "Maximum number of skills to load per message (≥1)."),
    )
    # ── Lazy skill injection (opt-in, like MCP prewarm) ──
    lazy_load: bool = field(
        default=False,
        metadata=_meta(
            "Lazy Skill Injection",
            "When true, the session-start skills block injects only a usage-ranked "
            "top-K of on-demand skills (bounded by its own section budget) and leaves "
            "the long tail discoverable via the skill_search tool / $skillname / "
            "triggers; each context section also gets its own independent char cap so "
            "the global ceiling becomes their sum (~190k) and a large skills set can "
            "never crowd out memory/lessons. Disabled by default (0-impact upgrade, "
            "like prewarm_count=0): off means the legacy full skills dump under a "
            "single shared 165k budget — unchanged behavior.",
        ),
    )
    # ── Auto skill creation ──
    # All fields default to OFF so upgrades are zero-impact. Enable via
    # ``kirocrew config set skills.auto_create_from_sessions true`` or the
    # dashboard Settings → Skills panel (future).
    auto_create_from_sessions: bool = field(
        default=False,
        metadata=_meta(
            "Auto-Create Skills",
            "When true, analyze each session after completion and synthesize a reusable "
            "SKILL.md when a non-trivial multi-step procedure is detected. Generated "
            "skills live under skills/auto/ so they never collide with hand-authored "
            "skills. Disabled by default.",
        ),
    )
    auto_refine_on_deviation: bool = field(
        default=False,
        metadata=_meta(
            "Auto-Refine Skills",
            "When true, update an existing auto-created skill if the agent succeeds "
            "via a different tool sequence than documented. Requires "
            "auto_create_from_sessions. Disabled by default.",
        ),
    )
    auto_min_tool_calls: int = field(
        default=5,
        metadata=_meta(
            "Auto Min Tool Calls",
            "Minimum tool calls in a session for it to qualify for skill extraction "
            "(≥2). Lower values produce more skills but reduce quality.",
        ),
    )
    auto_similarity_threshold: float = field(
        default=0.85,
        metadata=_meta(
            "Auto Similarity Threshold",
            "Skip creation when an existing skill's description has keyword overlap "
            "≥ this fraction with the synthesized description (0.0-1.0). Prevents "
            "near-duplicate skills.",
        ),
    )
    extra_paths: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Extra Skill Paths",
            "Additional directories to scan for skills. Supports ~ expansion. "
            "Skills from extra_paths are read-only (trigger matching + loading). "
            "Local ~/.kirocrew/skills/ takes precedence for duplicate names.",
        ),
    )

    def __post_init__(self) -> None:
        if self.max_triggered < 1:
            logger.warning("max_triggered %d < 1, using 1", self.max_triggered)
            object.__setattr__(self, "max_triggered", 1)
        if self.auto_min_tool_calls < 2:
            logger.warning("auto_min_tool_calls %d < 2, using 2", self.auto_min_tool_calls)
            object.__setattr__(self, "auto_min_tool_calls", 2)
        if not 0.0 <= self.auto_similarity_threshold <= 1.0:
            logger.warning(
                "auto_similarity_threshold %.2f out of range [0.0, 1.0], using 0.85",
                self.auto_similarity_threshold,
            )
            object.__setattr__(self, "auto_similarity_threshold", 0.85)
        if self.auto_refine_on_deviation and not self.auto_create_from_sessions:
            logger.warning(
                "auto_refine_on_deviation requires auto_create_from_sessions; "
                "disabling auto_refine_on_deviation"
            )
            object.__setattr__(self, "auto_refine_on_deviation", False)


@dataclass
class TelemetryConfig:
    """Metrics telemetry settings (Wave 0 trunk).

    Default OFF: when disabled, metric call sites are cheap no-ops and nothing is
    written or exported (byte-identical to no telemetry), mirroring the
    ``mcp_gateway.enabled`` / ``skills.lazy_load`` opt-in convention. When
    enabled, a local-first JSONL sink under ``~/.kirocrew/metrics`` is activated;
    remote / OTLP egress is a separate opt-in requiring ``kirocrew[otlp]``.
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Main switch for KiroCrew metrics telemetry. Off by default: metric "
            "call sites are no-ops and nothing is written. When on, a local-first "
            "JSONL sink under ~/.kirocrew/metrics is enabled (no network egress).",
        ),
    )
    local_dir: str = field(
        default="",
        metadata=_meta(
            "Local Metrics Dir",
            "Directory for local JSONL metric shards. Empty = ~/.kirocrew/metrics. "
            "Supports ~ expansion.",
        ),
    )
    export_interval_seconds: int = field(
        default=60,
        metadata=_meta(
            "Export Interval (s)",
            "How often the local exporter flushes aggregated metrics to disk (>=1).",
        ),
    )
    retention_days: int = field(
        default=0,
        metadata=_meta(
            "Retention (days)",
            "Prune local JSONL metric shards older than this many days on each "
            "export cycle. 0 disables age-based pruning. Bounds on-disk telemetry "
            "growth (rec #14: bounded retention).",
        ),
    )
    max_total_mb: int = field(
        default=0,
        metadata=_meta(
            "Max Total Size (MB)",
            "Opportunistic directory budget for local metric shards. Closed shards "
            "are pruned oldest-first; protected active writers can temporarily exceed "
            "the budget. 0 disables the size cap (rec #14: bounded retention).",
        ),
    )
    otlp_endpoint: str = field(
        default="",
        metadata=_meta(
            "OTLP Endpoint",
            "Opt-in OpenTelemetry OTLP/HTTP metrics endpoint (e.g. "
            "http://localhost:4318/v1/metrics). EMPTY = no network egress "
            "(default). When set, aggregated metrics are ALSO pushed to this "
            "collector in addition to the local JSONL sink; requires the "
            "kirocrew[otlp] package extra to be installed "
            "(rec #1: OTLP opt-in only, no egress by default).",
            sensitive=True,
        ),
    )

    def __post_init__(self) -> None:
        if self.export_interval_seconds < 1:
            logger.warning("export_interval_seconds %d < 1, using 1", self.export_interval_seconds)
            object.__setattr__(self, "export_interval_seconds", 1)
        if self.retention_days < 0:
            logger.warning("retention_days %d < 0, using 0 (no age pruning)", self.retention_days)
            object.__setattr__(self, "retention_days", 0)
        if self.max_total_mb < 0:
            logger.warning("max_total_mb %d < 0, using 0 (no size cap)", self.max_total_mb)
            object.__setattr__(self, "max_total_mb", 0)


# ---------------------------------------------------------------------------
# Validation helpers — used by KiroCrewConfig.load()
# ---------------------------------------------------------------------------

# JSON Schema type → Python type names for log messages
_JSON_TYPE_LABELS: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}


# ---------------------------------------------------------------------------
# Security-relevant resource-limit ceilings
# ---------------------------------------------------------------------------
# SINGLE SOURCE OF TRUTH for the upper bounds on the config knobs that govern
# host resource consumption. These same ceilings are enforced by the dashboard
# config API (``dashboard/handlers/core.py`` for the agent knobs,
# ``session.py`` for ``pool_size``); they live HERE so the API-write gate and
# the load-time clamp below cannot drift apart.
#
# Why the loader must also clamp (pentest — config-loader bound bypass): the
# REST API rejects out-of-range writes, but a direct edit of ``config.json``
# (any process running as the same OS user — including a prompt-injected agent
# with file-write access) bypassed that gate entirely. Each of these knobs
# controls a resource-consumption dimension — concurrent subagent processes
# (each a separate kiro-cli process), per-agent turn budget (unbounded LLM
# calls + context growth), and pre-warmed pool processes spawned at startup —
# so an inflated on-disk value can exhaust host memory / CPU / the process
# table (denial of service). Clamping at load time makes the on-disk value
# untrusted above range no matter which consumer reads it, and also means the
# GET /api/config/kirocrew response (which serializes a freshly loaded config)
# reports the clamped value rather than the tampered one.
SUBAGENT_AUTO_MAX_CEILING = 64  # agent.subagent_auto_max — concurrent subagent ceiling
SUBAGENT_MAX_TURNS_CEILING = 200  # agent.subagent_max_turns — per-subagent turn budget
POOL_SIZE_MAX = 10  # session.pool_size — pre-warmed process pool

# agent.max_subagents fixed-pin floor. 0 is the "auto-size" sentinel; any other
# (explicit) value must be >= this floor. A pin of 1 or 2 would silently DISABLE
# auto-sizing and run below today's default of 3, so such values are normalized
# UP to the floor at load time (see _clamp_security_bounds) and rejected by the
# dashboard API. Mirrors ``subagent._LEGACY_DEFAULT_MAX`` (kept as a local
# constant to avoid a config→subagent import cycle).
MAX_SUBAGENTS_FIXED_FLOOR = 3

# (section, key, min, max) for each bounded field clamped at load time. The
# mins match the runtime floors: subagent_auto_max has a floor of 3
# (``subagent._LEGACY_DEFAULT_MAX`` — the auto-size minimum), so a value < 3 is
# clamped UP to 3 with a warning, mirroring the > ceiling clamp. max_subagents
# keeps a 0 floor here (0 = auto sentinel) — its 0-or-(>=3) rule is applied as a
# special case after the generic loop. Only out-of-range values are altered.
_SECURITY_BOUNDED_FIELDS: tuple[tuple[str, str, int, int], ...] = (
    ("agent", "subagent_auto_max", 3, SUBAGENT_AUTO_MAX_CEILING),
    ("agent", "max_subagents", 0, SUBAGENT_AUTO_MAX_CEILING),
    ("agent", "subagent_max_turns", 1, SUBAGENT_MAX_TURNS_CEILING),
    ("session", "pool_size", 0, POOL_SIZE_MAX),
)


def _log_config_clamp_event(field: str, file_value: int, clamped: int, lo: int, hi: int) -> None:
    """Emit a best-effort SEL security event for a clamped (tampered) config value.

    Recorded so tampering is detectable after the fact even though the loader
    self-heals by clamping. Lazily imports the SEL to avoid an import cycle and
    to keep the hot load() path free of SEL cost on the normal (in-range) path —
    this only fires when a value was actually out of range. Wrapped so a SEL
    failure can never make config loading raise.
    """
    try:
        from kiro_crew.sel import SecurityEvent, sel

        sel().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type="config_bounds_clamped",
                caller_identity="config_loader",
                agent="",
                source="background",
                operation="config.load",
                outcome="clamped",
                resources=field,
                metadata={
                    "file_value": file_value,
                    "clamped_to": clamped,
                    "min": lo,
                    "max": hi,
                },
            )
        )
    except Exception:
        logger.debug("SEL config-clamp event failed", exc_info=True)


def _clamp_security_bounds(data: dict) -> None:
    """Clamp security-relevant bounded integers in *data* in place.

    Applies the same ceilings the dashboard API enforces at write time to the
    values read from disk (see ``_SECURITY_BOUNDED_FIELDS`` and the module-level
    ceiling constants for the rationale). Called once on the actual disk-read
    path (cache miss) BEFORE the validated dict is cached, so:

    * subsequent cache hits already serve clamped values (consistent), and
    * the tamper warning / SEL event fires once per file change — enough to
      detect tampering without spamming the hot load() path.

    Only real integers are clamped; ``bool`` (a JSON ``true``/``false``) and any
    non-int are left untouched for the dataclass construction path to
    coerce/default. A clamp is logged at WARNING and recorded as a SEL security
    event; both are best-effort and never fatal (config loading must not raise).
    """
    for section, key, lo, hi in _SECURITY_BOUNDED_FIELDS:
        sect = data.get(section)
        if not isinstance(sect, dict) or key not in sect:
            continue
        val = sect[key]
        # bool is an int subclass; a JSON true/false is not a real bound value.
        if isinstance(val, bool) or not isinstance(val, int):
            continue
        if val < lo or val > hi:
            clamped = max(lo, min(hi, val))
            sect[key] = clamped
            logger.warning(
                "config %s.%s=%d out of range [%d, %d]; clamped to %d "
                "(possible config tampering — a direct file edit cannot exceed "
                "the API-enforced ceiling)",
                section,
                key,
                val,
                lo,
                hi,
                clamped,
            )
            _log_config_clamp_event(f"{section}.{key}", val, clamped, lo, hi)

    # max_subagents special case: 0 is the auto-size sentinel; any explicit pin
    # must be >= MAX_SUBAGENTS_FIXED_FLOOR. A stray 1/2 silently disables
    # auto-sizing AND runs below today's default, so clamp it UP to the floor
    # (0 is left intact). Runs after the generic [0, ceiling] range clamp above.
    agent = data.get("agent")
    if isinstance(agent, dict):
        ms = agent.get("max_subagents")
        if isinstance(ms, int) and not isinstance(ms, bool) and 0 < ms < MAX_SUBAGENTS_FIXED_FLOOR:
            agent["max_subagents"] = MAX_SUBAGENTS_FIXED_FLOOR
            logger.warning(
                "config agent.max_subagents=%d is below the fixed-pin floor of %d "
                "(0 = auto-size; an explicit pin must be >= %d); clamped UP to %d",
                ms,
                MAX_SUBAGENTS_FIXED_FLOOR,
                MAX_SUBAGENTS_FIXED_FLOOR,
                MAX_SUBAGENTS_FIXED_FLOOR,
            )
            _log_config_clamp_event(
                "agent.max_subagents",
                ms,
                MAX_SUBAGENTS_FIXED_FLOOR,
                MAX_SUBAGENTS_FIXED_FLOOR,
                SUBAGENT_AUTO_MAX_CEILING,
            )


def _config_fingerprint() -> tuple:
    """Cheap signature of the config files — changes whenever either is edited.

    Uses st_mtime_ns + st_size + st_mode for both config.json and
    config.local.json so any edit, truncation, or replacement busts the cache.
    A missing file contributes a sentinel so create/delete also busts it.
    """
    sig: list = []
    for p in (config_path(), config_local_path()):
        try:
            st = p.stat()
            sig.append((str(p), st.st_mtime_ns, st.st_size, st.st_mode))
        except OSError:
            sig.append((str(p), None))
    return tuple(sig)


def _cached_validated_data() -> dict | None:
    """Return a deep copy of the cached validated config dict, or None on miss.

    Thin wrapper over the :class:`~kiro_crew.config.validation.ConfigCache`:
    the fingerprint is computed here (``_config_fingerprint`` stays in this
    module because it reads ``config_path()``/``config_local_path()``, which the
    test suite patches as ``kiro_crew.config.loader.config_path``).
    """
    return _CONFIG_CACHE.get(_config_fingerprint())


def _store_validated_data(data: dict, fp: tuple) -> None:
    """Cache a deep copy of *data* under fingerprint *fp* (see ConfigCache.store)."""
    _CONFIG_CACHE.store(data, fp)


def _invalidate_config_cache() -> None:
    """Drop the cached validated config (called after save()/write-back)."""
    _CONFIG_CACHE.clear()


# Channel activation modes
ACTIVATION_ALWAYS = "always"  # Process every message
ACTIVATION_MENTION = "mention"  # Only respond when @mentioned
ACTIVATION_OBSERVE = "observe"  # Record messages, respond only when @mentioned (deep context)
ACTIVATION_REVIEW = "review"  # Generate response, show ephemeral draft for owner approval
ACTIVATION_OFF = "off"  # Ignore all messages completely — no history recorded
_VALID_ACTIVATIONS = frozenset(
    {ACTIVATION_ALWAYS, ACTIVATION_MENTION, ACTIVATION_OBSERVE, ACTIVATION_REVIEW, ACTIVATION_OFF}
)


@dataclass
class ChannelConfig:
    """Per-channel Slack configuration."""

    activation: str = field(
        default=ACTIVATION_MENTION,
        metadata=_meta(
            "Activation",
            "Channel activation mode.",
            enum=["always", "mention", "observe", "review", "off"],
        ),
    )
    agent: str = field(
        default="",
        metadata=_meta("Agent", "Agent override for this channel (empty = default)."),
    )
    thread_follow: bool = field(
        default=True,
        metadata=_meta(
            "Thread Follow",
            "Respond to all messages in threads where bot was previously @mentioned.",
        ),
    )

    @classmethod
    def from_dict(cls, data: dict) -> ChannelConfig:
        activation = data.get("activation", ACTIVATION_MENTION)
        if activation not in _VALID_ACTIVATIONS:
            activation = ACTIVATION_MENTION
        return cls(
            activation=activation,
            agent=data.get("agent", ""),
            thread_follow=data.get("thread_follow", True),
        )


_VALID_STT_PROVIDERS = ("whisper", "mlx", "transcribe")
_VALID_CHANNEL_PREFIXES = ("C", "D", "G")


def _validated_stt_provider(value: str) -> str:
    """Return *value* if recognised, else warn and default to whisper."""
    if value in _VALID_STT_PROVIDERS:
        return value
    logger.warning("Unknown STT provider '%s', falling back to whisper", value)
    return "whisper"


_VALID_COMPLETION_KEEP = ("head", "tail", "both")


def _validated_completion_keep(value: object) -> str:
    """Return *value* if it is one of head/tail/both, else raise ValueError."""
    if isinstance(value, str) and value in _VALID_COMPLETION_KEEP:
        return value
    raise ValueError(
        f"agent.completion_keep must be one of {list(_VALID_COMPLETION_KEEP)}, " f"got {value!r}"
    )


def _normalize_jail(value: object) -> str:
    """Coerce a persisted ``agent.jail`` value to a valid mode, deny-by-default.

    Valid persisted modes are ``auto`` / ``on`` / ``off``.  An unknown or
    non-string value normalizes to ``auto`` (the safe default — let the active
    edition decide; the public edition's jail provider is a no-op regardless).
    ``off`` per-invocation is expressed via ``--no-jail`` / ``KIROCREW_NO_JAIL``,
    not persisted config.
    """
    if isinstance(value, str) and value in _VALID_JAIL_MODES:
        return value
    return JAIL_MODE_AUTO


def _validate_activation(value: str) -> str:
    """Return *value* if it is a valid activation mode, else ``mention`` (deny-by-default)."""
    return value if value in _VALID_ACTIVATIONS else ACTIVATION_MENTION


def _validate_tracking_channels(raw: list) -> list[dict]:
    """Validate and coerce tracking_channels entries.

    Accepted formats:
    - ``{"channel_id": "C...", "name": "..."}`` — passed through
    - ``"C..."`` (bare string) — auto-coerced to ``{"channel_id": "C..."}`` with a warning

    Rejects entries that are neither strings starting with C/D/G nor dicts with channel_id.
    """
    if not raw:
        return []
    result: list[dict] = []
    coerced = 0
    rejected = 0
    for entry in raw:
        if isinstance(entry, dict) and entry.get("channel_id"):
            result.append(entry)
        elif isinstance(entry, str) and len(entry) > 1 and entry[0] in _VALID_CHANNEL_PREFIXES:
            result.append({"channel_id": entry})
            coerced += 1
        else:
            rejected += 1
    if coerced:
        logger.warning(
            "Config: slack.tracking_channels has %d bare string(s) — auto-coerced to "
            '{"channel_id": "..."} format. Prefer: [{"channel_id": "C...", "name": "..."}]',
            coerced,
        )
    if rejected:
        logger.warning(
            "Config: slack.tracking_channels has %d invalid entries (expected objects with "
            '"channel_id" field or bare channel ID strings starting with C/D/G). '
            "These entries were ignored.",
            rejected,
        )
    return result


def _migrate_workspaces(raw_workspaces: dict) -> dict[str, WorkspaceConfig]:
    """Auto-migrate workspaces from flat or structured format.

    - String values → WorkspaceConfig(dir=value)
    - Dict values with ``dir`` key → WorkspaceConfig(dir=value["dir"])
    - Non-string/non-dict values → default WorkspaceConfig()
    - Empty input → {"default": WorkspaceConfig(dir="workspace")}
    """
    result: dict[str, WorkspaceConfig] = {}
    for name, value in raw_workspaces.items():
        if isinstance(value, str):
            result[name] = WorkspaceConfig(dir=value)
        elif isinstance(value, dict):
            result[name] = WorkspaceConfig(dir=value.get("dir", "workspace"))
        else:
            result[name] = WorkspaceConfig()
    if not result:
        result["default"] = WorkspaceConfig(dir="workspace")
    return result


def resolve_memory_store_config(
    top_level_memory: dict,
    store_overrides: dict,
) -> dict:
    """Deep-merge store overrides onto top-level memory defaults.

    Merge happens at the raw dict level BEFORE dataclass construction.
    A store that only sets embedding_provider inherits all other memory
    settings from the top-level config, not from MemoryConfig defaults.
    """
    merged = dict(top_level_memory)
    for key, value in store_overrides.items():
        if key == "description":
            continue  # description is store-only metadata, not a memory setting
        if value != "" and value is not None:
            merged[key] = value
    return merged


@dataclass
class ResolvedBindings:
    """Resolved workspace, memory store, and kiro agent for a session."""

    workspace_dir: Path
    memory_store_name: str
    effective_memory_config: dict
    kiro_agent: str


@dataclass
class SttConfig:
    """Speech-to-text configuration (opt-in, disabled by default)."""

    enabled: bool = field(
        default=True,
        metadata=_meta("Enabled", "Enable voice memo transcription."),
    )
    provider: str = field(
        default="whisper",
        metadata=_meta("Provider", "STT provider.", enum=list(_VALID_STT_PROVIDERS)),
    )
    whisper_path: str = field(
        default="",
        metadata=_meta("Whisper Path", "Path to whisper binary (auto-detected if empty)."),
    )
    model: str = field(
        default="turbo",
        metadata=_meta("Model", "Whisper model size.", enum=["turbo"]),
    )
    mlx_model: str = field(
        default="mlx-community/whisper-large-v3-turbo",
        metadata=_meta(
            "MLX Model",
            "Hugging Face repo for the mlx_whisper model (mlx provider only).",
        ),
    )
    device: str = field(
        default="cpu",
        metadata=_meta("Device", "Computation device.", enum=["cpu", "cuda"]),
    )
    timeout_secs: int = field(
        default=300,
        metadata=_meta("Timeout", "Transcription timeout in seconds."),
    )
    transcribe_region: str = field(
        default="us-east-1",
        metadata=_meta("Transcribe Region", "AWS region for Transcribe API."),
    )
    transcribe_profile: str = field(
        default="",
        metadata=_meta("Transcribe Profile", "AWS profile for Transcribe API."),
    )
    language_code: str = field(
        default="en-US",
        metadata=_meta(
            "Language Code", "Language for speech recognition (e.g. en-US, fr-FR, es-ES)."
        ),
    )
    streaming: bool = field(
        default=False,
        metadata=_meta(
            "Streaming",
            "Stream partial transcripts live to the dashboard input (transcribe provider only).",
        ),
    )


@dataclass
class McpGatewayConfig:
    """Sidecar MCP broker daemon — shares MCP backends across sessions."""

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Route MCP traffic through the shared sidecar broker. Default False — opt-in.",
        ),
    )
    socket_path: str = field(
        default="",
        metadata=_meta(
            "Socket Path",
            "Unix socket for the broker. Empty -> $KIROCREW_HOME/mcp-gateway/gateway.sock.",
        ),
    )
    overlay_dir: str = field(
        default="",
        metadata=_meta(
            "Overlay Dir",
            "Directory of rewritten agent JSON, bind-mounted over ~/.kiro/agents per session. "
            "Empty -> $KIROCREW_HOME/mcp-gateway/agents.",
        ),
    )
    idle_timeout_secs: int = field(
        default=300,
        metadata=_meta("Idle Timeout", "Seconds a refcount=0 MCP backend is kept before drain."),
    )
    max_backends: int = field(
        default=64,
        metadata=_meta(
            "Max Backends",
            "Max concurrent pooled MCP backends before the pool refuses a new one. "
            "Must be >= the number of distinct (agent x server) backends that can be "
            "live at once: each agent keeps its own backend per server, so N concurrent "
            "agents with ~S servers each need N*S slots. Bounded by design: idle "
            "backends drain after idle_timeout_secs, so steady-state RAM tracks real "
            "concurrency, not this ceiling.",
        ),
    )
    poolable_servers: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Poolable Servers",
            "MCP server names allowed to share a pooled backend across sessions. "
            "A stdio server is pooled when its name appears here OR its agent-JSON "
            "entry sets poolable:true. Safe by default — non-listed servers run "
            "per-session. Managed from Settings -> Shared MCP gateway.",
        ),
    )
    prewarm_count: int = field(
        default=0,
        metadata=_meta(
            "Prewarm Count",
            "Number of hottest observed (agent x server x channel) MCP backends "
            "to spawn at gateway startup, before the first session connects. "
            "Removes the cold-start latency on the first new-chat after a "
            "gateway restart or after all backends have idled out — the steady "
            "state already reuses warm backends within the idle timeout. The "
            "hot set is learned from prior registers and persisted beside the "
            "socket; channel_id is a stable id, so a prewarmed backend is "
            "reused by every later new-chat in that channel. 0 (default) "
            "disables prewarming — no hot-key file is read or written.",
        ),
    )
    read_buffer_limit_bytes: int = field(
        default=64 * 1024 * 1024,
        metadata=_meta(
            "Read Buffer Limit",
            "Maximum bytes for a single MCP response line before asyncio drops it. "
            "Default 64 MiB. Responses exceeding this are fast-failed with -32000. "
            "Env override: KIROCREW_MCP_READ_LIMIT.",
        ),
    )
    response_spill_threshold_bytes: int = field(
        default=256 * 1024,
        metadata=_meta(
            "Response Spill Threshold",
            "Tool-call responses larger than this (bytes) have their text content "
            "written to ~/.kirocrew/mcp_spill/ and truncated inline to 16 KiB + "
            "a file path marker. Default 256 KiB. Set 0 to disable spilling. "
            "Env override: KIROCREW_MCP_SPILL_THRESHOLD.",
        ),
    )


@dataclass
class InstancesConfig:
    """Multi-instance management (the *Instances* feature).

    Gates and tunes the gateway's ability to manage/switch between several
    remote KiroCrew instances over SSH tunnels. Off by default — opt-in only,
    since enabling it allows the gateway to open SSH ``-L`` forwards and relaxes
    the dashboard CSP ``frame-src`` for the active loopback tunnel ports.

    The numeric tunables default to constants defined in
    ``kiro_crew.instances.constants`` so the canonical default lives in one
    place and cannot drift from this dataclass.
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable multi-instance management — lets this gateway open SSH tunnels "
            "to remote KiroCrews and embed their dashboards. Default off (opt-in). "
            "Enabling also scopes a CSP frame-src relaxation to active tunnel ports.",
        ),
    )
    warm_set_cap: int = field(
        default=_DEFAULT_WARM_SET_CAP,
        metadata=_meta(
            "Warm Set Cap",
            "Max number of remote instances kept warm (iframe mounted + tunnel live) "
            "at once. Least-recently-used instances beyond this are evicted and "
            "reconnected on demand. Bounds memory/socket use (each warm instance is a "
            "full dashboard SPA).",
        ),
    )
    tunnel_base_port: int = field(
        default=_DEFAULT_TUNNEL_BASE_PORT,
        metadata=_meta(
            "Tunnel Base Port",
            "First local loopback port used for an SSH -L forward. The allocator "
            "increments from here, skipping ports already in use.",
        ),
    )
    ssh_compression: bool = field(
        default=_DEFAULT_SSH_COMPRESSION,
        metadata=_meta(
            "SSH Compression",
            "Enable SSH transport compression (ssh -C) on instance tunnels. The "
            "remote dashboard SPA bundle plus all API/WebSocket traffic travel over "
            "this forwarded stream and are highly compressible; the gateway does not "
            "gzip HTTP responses, so this is the only compression in the path. "
            "Default on (best for a dedicated remote host over a slow link); turn off "
            "on a fast/local link where compression CPU outweighs the bandwidth win.",
        ),
    )
    max_recovery_attempts: int = field(
        default=_DEFAULT_MAX_RECOVERY,
        metadata=_meta(
            "Max Recovery Attempts",
            "Consecutive self-heal attempts before a dropped tunnel is left "
            "disconnected. With the capped-exponential backoff, the default 8 spans a "
            "~2 min recovery window, enough to outlast a transient drop (screen lock, "
            "proxy warmup) before giving up.",
        ),
    )
    recover_backoff_max_secs: float = field(
        default=_DEFAULT_BACKOFF_MAX,
        metadata=_meta(
            "Recover Backoff Cap (secs)",
            "Cap on the per-attempt backoff between self-heal attempts. The wait grows "
            "1, 2, 4, 8, 16 then holds at this cap; raising it spaces retries further "
            "across a slow reconnect.",
        ),
    )
    probe_failure_threshold: int = field(
        default=_DEFAULT_PROBE_FAILS,
        metadata=_meta(
            "Probe Failure Threshold",
            "Consecutive health-probe failures before a connected-but-not-forwarding "
            "(zombie) tunnel is torn down to trigger self-heal.",
        ),
    )

    def __post_init__(self) -> None:
        if self.warm_set_cap < 1:
            logger.warning("instances.warm_set_cap %d < 1, using 1", self.warm_set_cap)
            object.__setattr__(self, "warm_set_cap", 1)
        if not (1 <= self.tunnel_base_port <= 65535):
            logger.warning(
                "instances.tunnel_base_port %d out of range [1, 65535], using %d",
                self.tunnel_base_port,
                _DEFAULT_TUNNEL_BASE_PORT,
            )
            object.__setattr__(self, "tunnel_base_port", _DEFAULT_TUNNEL_BASE_PORT)
        if self.max_recovery_attempts < 1:
            logger.warning(
                "instances.max_recovery_attempts %d < 1, using %d",
                self.max_recovery_attempts,
                _DEFAULT_MAX_RECOVERY,
            )
            object.__setattr__(self, "max_recovery_attempts", _DEFAULT_MAX_RECOVERY)
        elif self.max_recovery_attempts > _MAX_RECOVERY_CEILING:
            logger.warning(
                "instances.max_recovery_attempts %d > %d, clamping to %d "
                "(guards against a near-infinite self-heal loop on a dead connection)",
                self.max_recovery_attempts,
                _MAX_RECOVERY_CEILING,
                _MAX_RECOVERY_CEILING,
            )
            object.__setattr__(self, "max_recovery_attempts", _MAX_RECOVERY_CEILING)
        if self.recover_backoff_max_secs <= 0:
            logger.warning(
                "instances.recover_backoff_max_secs %s <= 0, using %s",
                self.recover_backoff_max_secs,
                _DEFAULT_BACKOFF_MAX,
            )
            object.__setattr__(self, "recover_backoff_max_secs", _DEFAULT_BACKOFF_MAX)
        elif self.recover_backoff_max_secs > _RECOVER_BACKOFF_CEILING:
            logger.warning(
                "instances.recover_backoff_max_secs %s > %s, clamping to %s "
                "(guards against a multi-day self-heal window on a dead connection)",
                self.recover_backoff_max_secs,
                _RECOVER_BACKOFF_CEILING,
                _RECOVER_BACKOFF_CEILING,
            )
            object.__setattr__(self, "recover_backoff_max_secs", _RECOVER_BACKOFF_CEILING)
        if self.probe_failure_threshold < 1:
            logger.warning(
                "instances.probe_failure_threshold %d < 1, using %d",
                self.probe_failure_threshold,
                _DEFAULT_PROBE_FAILS,
            )
            object.__setattr__(self, "probe_failure_threshold", _DEFAULT_PROBE_FAILS)


@dataclass
class HeartbeatConfig:
    """Heartbeat background task queue (~/.kirocrew/workspace/HEARTBEAT.md)."""

    default_deliver: str = field(
        default="slack",
        metadata=_meta(
            "Default delivery",
            "Where a heartbeat completion with no inline <!-- deliver:... --> tag is "
            "routed: 'slack' (Slack DM + dashboard bell, the default) or 'dashboard' "
            "(dashboard slot + bell only, no Slack). Per-task deliver tags always "
            "override this.",
        ),
    )


@dataclass
class WatchdogConfig:
    """ACP per-session watchdog / liveness-oracle tuning (acp/session_handle.py).

    Wellness (the liveness oracle) is the primary detector; these windows govern
    only the UNKNOWN-verdict backstop class. A WORKING verdict is never acted on
    at any elapsed time, and every watchdog action is non-lethal (auto-recovery,
    never a silent kill).
    """

    check_after_secs: float = field(
        default=60.0,
        metadata=_meta(
            "Check after (s)",
            "Idle seconds on a turn before the liveness oracle is consulted at all. "
            "Below this, the dispatch loop does no watchdog work.",
        ),
    )
    stale_window_secs: float = field(
        default=300.0,
        metadata=_meta(
            "Stale probe window (s)",
            "Idle seconds before an UNKNOWN-verdict model-wait turn is safe-probed "
            "via session/cancel. Probes are non-lethal: a live turn auto-recovers.",
        ),
    )
    tool_stall_suspect_secs: float = field(
        default=10800.0,
        metadata=_meta(
            "Tool stall suspect (s)",
            "Idle seconds before an UNKNOWN-verdict in-flight tool is cancelled and "
            "the turn routed to tool-stall recovery (continue-nudge, no re-run of "
            "the original message). WORKING tools (e.g. a matched live build child) "
            "are never cancelled regardless of duration. Default 3h to accommodate "
            "long-running builds and MCP tools on macOS where the liveness oracle "
            "degrades (no /proc) and cannot distinguish live builds from stalls.",
        ),
    )
    tool_stall_hard_cap_secs: float = field(
        default=10800.0,
        metadata=_meta(
            "Hard cap (s)",
            "Absolute ceiling for UNKNOWN-verdict forbearance (e.g. the extended "
            "probably-thinking window). Applies ONLY to UNKNOWN verdicts — never "
            "to a WORKING session. Default 3h.",
        ),
    )
    model_silent_probe_secs: float = field(
        default=900.0,
        metadata=_meta(
            "Silent-think probe window (s)",
            "Extended probe window for a model-wait with an established backend "
            "connection but flat counters (non-streamed server-side reasoning, "
            "e.g. long xhigh thinks). Probing a live think cancels and regenerates "
            "it, so this window is deliberately generous.",
        ),
    )
    wellness_sample_secs: float = field(
        default=3.0,
        metadata=_meta(
            "Wellness sample interval (s)",
            "Minimum spacing between CPU/IO counter samples used for movement "
            "deltas in the liveness oracle.",
        ),
    )


@dataclass
class TunnelConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta("Enabled", "Enable a tunnel to expose the dashboard for remote access."),
    )
    name_mode: str = field(
        default="username",
        metadata=_meta(
            "Name Mode",
            "Tunnel naming: 'username' uses 'kirocrew', "
            "'hash' uses 'kirocrew-<hostHash>' for multi-host disambiguation.",
            enum=["username", "hash"],
        ),
    )
    name_override: str = field(
        default="",
        metadata=_meta(
            "Name Override",
            "Explicit tunnel name (overrides name_mode). "
            "Note: some tunnel providers prefix your username (e.g. 'foo' becomes '<user>-foo').",
        ),
    )


@dataclass
class WeComConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the WeChat channel via WeCom AI-bot. Requires the WECOM_BOT_ID "
            "and WECOM_SECRET credentials to be set.",
            tags=["wechat"],
        ),
    )
    allowed_users: list[dict] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Users",
            "WeCom users allowed to DM the bot. Each entry: {userid, name}. "
            "The owner is always allowed.",
            tags=["wechat"],
        ),
    )
    ws_url: str = field(
        default="wss://openws.work.weixin.qq.com",
        metadata=_meta(
            "WebSocket URL",
            "WeCom AI-bot long-connection endpoint.",
            tags=["wechat"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a DM's context passes this, prompt the user to /compact or /new "
            "instead of auto-compacting.",
            tags=["wechat"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this, even without a user "
            "decision, so the window never overflows.",
            tags=["wechat"],
        ),
    )

    def __post_init__(self) -> None:
        # Clamp thresholds to [0, 100] and guarantee soft <= hard so a misconfig
        # (e.g. hard=50, soft=95, or an out-of-range value) can't make the soft
        # nudge unreachable -- _maybe_notice checks ``pct >= hard`` first.
        self.soft_threshold_pct = max(0, min(100, self.soft_threshold_pct))
        self.hard_threshold_pct = max(0, min(100, self.hard_threshold_pct))
        if self.soft_threshold_pct > self.hard_threshold_pct:
            self.soft_threshold_pct = self.hard_threshold_pct


def _coerce_int_ids(raw: object) -> list[int]:
    """Coerce a config value to a clean ``list[int]``, dropping anything invalid.

    Fail closed against a hand-edited config: a non-list (e.g. the string
    ``"12345"``) yields ``[]`` instead of iterating char-by-char, and any entry
    that isn't a clean base-10 integer (``"--100"``, ``"1.5"``, unicode digits,
    booleans) is skipped rather than raising in ``int()`` and crashing config
    load / gateway startup.
    """
    if not isinstance(raw, list):
        return []
    ids: list[int] = []
    for u in raw:
        try:
            ids.append(int(str(u)))
        except (TypeError, ValueError):
            continue
    return ids


def _coerce_str_ids(raw: object) -> list[str]:
    """Coerce a config value to a clean, deduped ``list[str]`` of digit IDs.

    Used for Discord snowflakes, which exceed 2^53 and therefore stay strings
    (JSON round-trip safe). Fails closed like :func:`_coerce_int_ids`: a
    non-list yields ``[]`` and non-digit entries are dropped.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for u in raw:
        s = str(u).strip()
        if s.isdigit() and s not in out:
            out.append(s)
    return out


def _coerce_int(raw: object, default: int) -> int:
    """Return ``int(raw)`` or *default* if *raw* isn't a clean base-10 integer.

    Fail closed against a hand-edited non-numeric config value (e.g. ``"abc"``)
    that would otherwise raise in ``int()`` and crash config load.
    """
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


@dataclass
class TelegramConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Telegram Bot API channel (long-polling). Requires "
            "TELEGRAM_BOT_TOKEN (env/.env) or telegram.bot_token.",
            tags=["telegram"],
        ),
    )
    bot_token: str = field(
        default="",
        metadata=_meta(
            "Bot Token",
            "Telegram Bot API token from @BotFather. Prefer the TELEGRAM_BOT_TOKEN "
            "credential (env/.env) over storing it here.",
            tags=["telegram"],
            sensitive=True,
        ),
    )
    allowed_user_ids: list[int] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed User IDs",
            "Numeric Telegram user IDs permitted to DM the bot. Empty = deny all "
            "(fail closed): a Telegram bot is globally reachable by @username.",
            tags=["telegram"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "Prompt the user to /compact or /new when context passes this percentage.",
            tags=["telegram"],
        ),
    )
    allow_forum: bool = field(
        default=False,
        metadata=_meta(
            "Allow Forum Topics",
            "Serve Telegram supergroup forum Topics as per-topic sessions "
            "(Slack-thread style). Fail-closed: also requires the supergroup's "
            "chat_id in allowed_forum_chat_ids.",
            tags=["telegram"],
        ),
    )
    allowed_forum_chat_ids: list[int] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Forum Chat IDs",
            "Numeric supergroup chat_ids permitted to run forum-topic sessions. "
            "Empty = deny all groups (fail closed).",
            tags=["telegram"],
        ),
    )


@dataclass
class DiscordConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Discord channel (Gateway WebSocket, DMs plus optional "
            "allow-listed server threads). Requires DISCORD_BOT_TOKEN (env/.env) "
            "or discord.bot_token.",
            tags=["discord"],
        ),
    )
    bot_token: str = field(
        default="",
        metadata=_meta(
            "Bot Token",
            "Discord bot token from the Developer Portal (Bot page). Prefer the "
            "DISCORD_BOT_TOKEN credential (env/.env) over storing it here.",
            tags=["discord"],
            sensitive=True,
        ),
    )
    allowed_user_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed User IDs",
            "Discord user IDs (snowflakes) permitted to message the bot. Empty = "
            "deny all (fail closed).",
            tags=["discord"],
        ),
    )
    allowed_thread_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Thread IDs",
            "Discord server thread IDs where approved users may run the agent. "
            "Empty = DMs only. Normal server channels are always denied.",
            tags=["discord"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "Prompt the user to !compact or !new when context passes this percentage.",
            tags=["discord"],
        ),
    )


@dataclass
class WebexConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Webex Messaging channel (device WebSocket, no public "
            "URL needed). Requires WEBEX_BOT_TOKEN (env/.env) or webex.bot_token.",
            tags=["webex"],
        ),
    )
    bot_token: str = field(
        default="",
        metadata=_meta(
            "Bot Token",
            "Webex bot access token from developer.webex.com (My Webex Apps). "
            "Prefer the WEBEX_BOT_TOKEN credential (env/.env) over storing it here.",
            tags=["webex"],
            sensitive=True,
        ),
    )
    allowed_emails: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Emails",
            "Webex account emails permitted to DM the bot. Empty = deny all "
            "(fail closed): anyone in the org can message a Webex bot.",
            tags=["webex"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a DM's context passes this, prompt the user to /compact or /new "
            "instead of auto-compacting.",
            tags=["webex"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this, even without a user "
            "decision, so the window never overflows.",
            tags=["webex"],
        ),
    )

    def __post_init__(self) -> None:
        # Clamp thresholds to [0, 100] and guarantee soft <= hard so a misconfig
        # can't make the soft nudge unreachable -- _maybe_notice checks
        # ``pct >= hard`` first. Mirrors WeComConfig.
        self.soft_threshold_pct = max(0, min(100, self.soft_threshold_pct))
        self.hard_threshold_pct = max(0, min(100, self.hard_threshold_pct))
        if self.soft_threshold_pct > self.hard_threshold_pct:
            self.soft_threshold_pct = self.hard_threshold_pct


@dataclass
class KiroCrewConfig:
    agent: AgentConfig = field(
        default_factory=AgentConfig,
        metadata=_meta("Agent", "Agent runtime configuration."),
    )
    session: SessionConfig = field(
        default_factory=SessionConfig,
        metadata=_meta("Session", "Session management settings."),
    )
    taskrunner: TaskRunnerConfig = field(
        default_factory=TaskRunnerConfig,
        metadata=_meta("Task Runner", "Task runner configuration."),
    )
    orchestrator: OrchestratorConfig = field(
        default_factory=OrchestratorConfig,
        metadata=_meta("Orchestrator", "Autopilot/orchestrator settings."),
    )
    messaging: MessagingConfig = field(
        default_factory=MessagingConfig,
        metadata=_meta("Messaging", "Channel-neutral messaging transport settings."),
    )
    cron_history: CronHistoryConfig = field(
        default_factory=CronHistoryConfig,
        metadata=_meta("Cron History", "Cron execution history storage limits."),
    )
    memory: MemoryConfig = field(
        default_factory=MemoryConfig,
        metadata=_meta("Memory", "Memory and embedding configuration."),
    )
    knowledge: KnowledgeConfig = field(
        default_factory=KnowledgeConfig,
        metadata=_meta("Knowledge", "Knowledge Library ingestion settings."),
    )
    skills: SkillsConfig = field(
        default_factory=SkillsConfig,
        metadata=_meta("Skills", "Skill loading and matching configuration."),
    )
    telemetry: TelemetryConfig = field(
        default_factory=TelemetryConfig,
        metadata=_meta(
            "Telemetry",
            "Metrics telemetry (local-first JSONL sink). Off by default.",
        ),
    )
    stt: SttConfig = field(
        default_factory=SttConfig,
        metadata=_meta("STT", "Speech-to-text transcription settings."),
    )
    mcp_gateway: McpGatewayConfig = field(
        default_factory=McpGatewayConfig,
        metadata=_meta("MCP Gateway", "Sidecar MCP broker that shares backends across sessions."),
    )
    instances: InstancesConfig = field(
        default_factory=InstancesConfig,
        metadata=_meta(
            "Instances", "Multi-instance management — manage/switch remote KiroCrews over SSH."
        ),
    )
    heartbeat: HeartbeatConfig = field(
        default_factory=HeartbeatConfig,
        metadata=_meta("Heartbeat", "Heartbeat background task queue delivery defaults."),
    )
    watchdog: WatchdogConfig = field(
        default_factory=WatchdogConfig,
        metadata=_meta("Watchdog", "ACP per-session watchdog / liveness-oracle windows."),
    )

    slack: SlackConfig = field(
        default_factory=SlackConfig,
        metadata=_meta("Slack", "Slack integration settings.", tags=["slack"]),
    )
    publish: PublishConfig = field(
        default_factory=PublishConfig,
        metadata=_meta(
            "Publish", "Artifact publishing controls (destinations allowlist).", tags=["publish"]
        ),
    )
    wechat: WeComConfig = field(
        default_factory=WeComConfig,
        metadata=_meta("WeChat", "WeChat (WeCom AI-bot) integration settings.", tags=["wechat"]),
    )
    telegram: TelegramConfig = field(
        default_factory=TelegramConfig,
        metadata=_meta("Telegram", "Telegram Bot API integration settings.", tags=["telegram"]),
    )
    discord: DiscordConfig = field(
        default_factory=DiscordConfig,
        metadata=_meta("Discord", "Discord bot integration settings.", tags=["discord"]),
    )
    webex: WebexConfig = field(
        default_factory=WebexConfig,
        metadata=_meta("Webex", "Webex Messaging integration settings.", tags=["webex"]),
    )
    dashboard: DashboardConfig = field(
        default_factory=DashboardConfig,
        metadata=_meta("Dashboard", "Dashboard UI settings."),
    )
    tunnel: TunnelConfig = field(
        default_factory=TunnelConfig,
        metadata=_meta("Tunnel", "AEA tunnel settings for remote dashboard access."),
    )
    hooks: dict = field(
        default_factory=dict,
        metadata=_meta("Hooks", "Script hook definitions keyed by hook ID."),
    )
    slack_channels: dict[str, ChannelConfig] = field(
        default_factory=dict,
        metadata=_meta("Slack Channels", "Per-channel activation config."),
    )
    slack_dm_activation: str = field(
        default=ACTIVATION_ALWAYS,
        metadata=_meta("Slack DM Activation", "Default activation mode for DMs."),
    )
    observe_max_messages: int = field(
        default=200,
        metadata=_meta("Observe Max Messages", "Max messages per observe-mode channel."),
    )
    observe_ttl_hours: float = field(
        default=168.0,
        metadata=_meta("Observe TTL Hours", "Hours to keep observe history."),
    )
    agents: dict[str, KiroCrewAgentConfig] = field(
        default_factory=dict,
        metadata=_meta("Agents", "Named KiroCrew agent definitions."),
    )
    default_agent: str = field(
        default="",
        metadata=_meta("Default Agent", "Active KiroCrew agent name from the agents section."),
    )
    workspaces: dict[str, WorkspaceConfig] = field(
        default_factory=dict,
        metadata=_meta("Workspaces", "Named workspace definitions."),
    )
    default_workspace: str = field(
        default="default",
        metadata=_meta("Default Workspace", "Active workspace name."),
    )
    memory_stores: dict[str, MemoryStoreConfig] = field(
        default_factory=dict,
        metadata=_meta("Memory Stores", "Named memory store definitions."),
    )
    default_memory_store: str = field(
        default="default",
        metadata=_meta("Default Memory Store", "Fallback memory store name."),
    )
    auto_update: bool = field(
        default=True,
        metadata=_meta("Auto Update", "Enable automatic update checks."),
    )
    timezone: str = field(
        default="",
        metadata=_meta(
            "Timezone",
            "IANA timezone name (e.g. 'America/Los_Angeles'). "
            "Used to display cron schedules in local time.",
        ),
    )
    snapshot_dir: str = field(
        default="",
        metadata=_meta(
            "Snapshot Directory",
            "Directory for kirocrew snapshot output. "
            "Defaults to ~/.kirocrew/snapshots if empty.",
        ),
    )
    registries: list[ExternalRegistryConfig] = field(
        default_factory=list,
        metadata=_meta(
            "Registries",
            "External app registries (org-owned repos). " "Each entry: {name, repo, branch}.",
        ),
    )
    # Unknown top-level config.json sections captured verbatim at load() and
    # re-emitted by to_dict() so a section this core does not model (e.g. an
    # edition-contributed section written by a companion) is NOT silently
    # dropped on the first save()/PATCH round-trip. Excluded from the JSON
    # schema by the leading underscore (build_json_schema skips private fields);
    # populated only from disk. This is the data-preservation half of the
    # ConfigSchemaContributor seam — a companion writes its section, the core
    # round-trips it untouched.
    _extra_sections: dict = field(default_factory=dict)

    def channel_config(self, channel_id: str) -> ChannelConfig:
        """Return the config for *channel_id*, falling back to defaults.

        DMs (channel IDs starting with ``D``) use ``slack_dm_activation``.
        Group channels use ``mention`` unless overridden in ``slack_channels``.
        """
        if channel_id in self.slack_channels:
            return self.slack_channels[channel_id]
        if channel_id.startswith("D"):
            return ChannelConfig(activation=self.slack_dm_activation)
        return ChannelConfig(activation=ACTIVATION_MENTION)

    @property
    def slack_enterprise_ids(self) -> set[str]:
        """Extra allowed enterprise IDs from ``slack.allowed_enterprise_ids``."""
        return set(self.slack.allowed_enterprise_ids)

    @classmethod
    def load(cls) -> KiroCrewConfig:
        """Load config from ~/.kirocrew/config.json, falling back to defaults.

        If ``config.local.json`` exists alongside ``config.json``, it is
        deep-merged on top. User overrides in the local file survive
        upgrades that regenerate ``config.json``.

        The overlay is applied at load time but NOT persisted back by
        ``save()`` — only the base config is written to ``config.json``.
        """
        path = config_path()

        # Hot-path cache: reuse the validated, merged dict when neither config
        # file has changed since the last load. Skips read + json.loads +
        # _deep_merge + the full jsonschema.validate. A deep copy is returned so
        # in-place mutation by callers (and the write-back migration below) can
        # never corrupt the cached original.
        cached_data = _cached_validated_data()
        if cached_data is not None:
            data = cached_data
        else:
            # Capture the fingerprint BEFORE reading so a write landing during
            # the read is detected: we cache under this pre-read fp, which won't
            # match the post-write on-disk stat, so the next load() re-reads
            # instead of serving the content we read mid-write (read->store
            # TOCTOU). _store_validated_data documents this contract.
            pre_read_fp = _config_fingerprint()
            data = {}
            loaded_base = False
            if path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data = raw
                        loaded_base = True
                    else:
                        logger.warning("Config is not a JSON object, using defaults")
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("Failed to load config from %s: %s", path, e)

            # Deep-merge config.local.json overlay (user-owned, never touched by setup)
            local_data: dict = {}
            local_path = config_local_path()
            if local_path.is_file():
                try:
                    st_mode = local_path.stat().st_mode
                    if st_mode & 0o002:
                        logger.warning(
                            "config.local.json is world-writable (%o); "
                            "consider running: chmod 600 %s",
                            st_mode & 0o777,
                            local_path,
                        )
                    raw_local = json.loads(local_path.read_text(encoding="utf-8"))
                    if isinstance(raw_local, dict):
                        local_data = raw_local
                    else:
                        logger.warning("config.local.json is not a JSON object, ignoring")
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("Failed to load config.local.json: %s", e)

            if local_data:
                data = _deep_merge(data, local_data)

            # Return defaults only if neither file was successfully loaded. Seed
            # the default "kirocrew" agent in-memory (matching the on-disk
            # migration below) so a never-setup home still lists the default
            # agent — but do NOT persist: a plain read (e.g. `agent list`) must
            # not create config files as a side effect. Not cached — there's no
            # file to invalidate against, and the path is already cheap
            # (existence checks only, no read/parse/validate).
            if not loaded_base and not local_data:
                cfg = cls()
                kiro = cfg.agent.default_agent or "kirocrew"
                cfg.agents["default"] = KiroCrewAgentConfig(
                    kiro_agent=kiro,
                    workspace="default",
                    memory_store="default",
                )
                cfg.default_agent = "default"
                return cfg

            # Validate against JSON Schema (advisory — never fatal)
            _validate_config_data(data)
            # Clamp security-relevant resource-limit knobs to their API ceilings
            # BEFORE caching, so a hand-edited/prompt-injected config.json that
            # exceeds a ceiling cannot drive resource exhaustion (DoS). Runs only
            # on the disk-read path; cache hits below already serve clamped values.
            _clamp_security_bounds(data)
            # Cache the validated, merged dict under the PRE-read fingerprint so
            # a mid-read write self-heals (next load misses and re-reads).
            _store_validated_data(data, pre_read_fp)

        agent_data = data.get("agent", {})
        if not isinstance(agent_data, dict):
            agent_data = {}
        session_data = data.get("session", {})
        if not isinstance(session_data, dict):
            session_data = {}
        taskrunner_data = data.get("taskrunner", {})
        if not isinstance(taskrunner_data, dict):
            taskrunner_data = {}
        cron_history_data = data.get("cron_history", {})
        if not isinstance(cron_history_data, dict):
            cron_history_data = {}
        memory_data = data.get("memory", {})
        if not isinstance(memory_data, dict):
            memory_data = {}
        knowledge_data = data.get("knowledge", {})
        if not isinstance(knowledge_data, dict):
            knowledge_data = {}
        telegram_data = data.get("telegram", {})
        if not isinstance(telegram_data, dict):
            telegram_data = {}
        discord_data = data.get("discord", {})
        if not isinstance(discord_data, dict):
            discord_data = {}
        webex_data = data.get("webex", {})
        if not isinstance(webex_data, dict):
            webex_data = {}
        slack_data = data.get("slack", {})
        if not isinstance(slack_data, dict):
            slack_data = {}
        publish_data = data.get("publish", {})
        if not isinstance(publish_data, dict):
            publish_data = {}
        wechat_data = data.get("wechat", {})
        if not isinstance(wechat_data, dict):
            wechat_data = {}
        dashboard_data = data.get("dashboard", {})
        if not isinstance(dashboard_data, dict):
            dashboard_data = {}
        stt_data = data.get("stt", {})
        if not isinstance(stt_data, dict):
            stt_data = {}
        instances_data = data.get("instances", {})
        if not isinstance(instances_data, dict):
            instances_data = {}
        mcp_gateway_data = data.get("mcp_gateway", {})
        if not isinstance(mcp_gateway_data, dict):
            mcp_gateway_data = {}
        heartbeat_data = data.get("heartbeat", {})
        if not isinstance(heartbeat_data, dict):
            heartbeat_data = {}
        heartbeat_default_deliver = (
            str(heartbeat_data.get("default_deliver", "slack")).strip().lower()
        )
        if heartbeat_default_deliver not in ("slack", "dashboard"):
            heartbeat_default_deliver = "slack"
        tunnel_data = data.get("tunnel", {})
        if not isinstance(tunnel_data, dict):
            tunnel_data = {}
        skills_data = data.get("skills", {})
        if not isinstance(skills_data, dict):
            skills_data = {}
        messaging_data = data.get("messaging", {})
        if not isinstance(messaging_data, dict):
            messaging_data = {}
        telemetry_data = data.get("telemetry", {})
        if not isinstance(telemetry_data, dict):
            telemetry_data = {}
        orchestrator_data = data.get("orchestrator", {})
        if not isinstance(orchestrator_data, dict):
            orchestrator_data = {}
        watchdog_data = data.get("watchdog", {})
        if not isinstance(watchdog_data, dict):
            watchdog_data = {}

        # Parse agents section into dict[str, KiroCrewAgentConfig]
        raw_agents = data.get("agents", {})
        agents: dict[str, KiroCrewAgentConfig] = {}
        if isinstance(raw_agents, dict):
            for name, entry in raw_agents.items():
                if isinstance(entry, dict):
                    agents[name] = KiroCrewAgentConfig(
                        kiro_agent=entry.get("kiro_agent", ""),
                        workspace=entry.get("workspace", "default"),
                        memory_store=entry.get("memory_store", "default"),
                        description=entry.get("description", ""),
                        source=entry.get("source", "kirocrew"),
                    )

        # Migrate workspaces from flat or structured format
        raw_workspaces = data.get("workspaces", {})
        if not isinstance(raw_workspaces, dict):
            raw_workspaces = {}
        workspaces = _migrate_workspaces(raw_workspaces)

        # Parse memory_stores; synthesize default if missing
        raw_stores = data.get("memory_stores", {})
        memory_stores: dict[str, MemoryStoreConfig] = {}
        if isinstance(raw_stores, dict) and raw_stores:
            for name, entry in raw_stores.items():
                if isinstance(entry, dict):
                    memory_stores[name] = MemoryStoreConfig(
                        description=entry.get("description", ""),
                        embedding_provider=entry.get("embedding_provider", ""),
                    )
        if not memory_stores:
            memory_stores["default"] = MemoryStoreConfig()

        # Parse top-level default_agent and default_memory_store
        default_agent_val = data.get("default_agent", "")
        if not isinstance(default_agent_val, str):
            default_agent_val = ""
        default_memory_store_val = data.get("default_memory_store", "default")
        if not isinstance(default_memory_store_val, str):
            default_memory_store_val = "default"

        # Capture unknown top-level sections verbatim so a section this core does
        # not model (e.g. an edition-contributed section written by a companion)
        # survives the load()->to_dict()->save() round-trip instead of being
        # silently dropped. ``meta`` is stamped by save() itself, so it is never
        # treated as an unknown section to preserve.
        extra_sections = {
            k: v for k, v in data.items() if k not in _KNOWN_CONFIG_SECTIONS and k != "meta"
        }

        cfg = cls(
            agent=AgentConfig(
                approval_mode=agent_data.get("approval_mode", "auto"),
                streaming=agent_data.get("streaming", True),
                model=agent_data.get("model", DEFAULT_MODEL),
                provider=agent_data.get("provider", "acp"),
                default_agent=agent_data.get("default_agent", ""),
                sandbox=agent_data.get("sandbox", "off"),
                sandbox_allow_no_isolation=bool(
                    agent_data.get("sandbox_allow_no_isolation", False)
                ),
                sandbox_allow_unsandboxed_exec=bool(
                    agent_data.get("sandbox_allow_unsandboxed_exec", False)
                ),
                apps_allow_third_party=bool(agent_data.get("apps_allow_third_party", True)),
                jail=_normalize_jail(agent_data.get("jail", "auto")),
                yolo=agent_data.get("yolo", False),
                notify_override_expiry=agent_data.get("notify_override_expiry", True),
                conductor_skill=agent_data.get("conductor_skill", False),
                tool_search=bool(agent_data.get("tool_search", True)),
                session_sharing=bool(agent_data.get("session_sharing", True)),
                max_subagents=agent_data.get("max_subagents", 0),
                subagent_mem_buffer_pct=_safe_int(agent_data.get("subagent_mem_buffer_pct", 20), 20),
                subagent_cost_gb=_safe_float(agent_data.get("subagent_cost_gb", 0.5), 0.5),
                subagent_cpu_cost_cores=_safe_float(agent_data.get("subagent_cpu_cost_cores", 1.0), 1.0),
                subagent_auto_max=_safe_int(agent_data.get("subagent_auto_max", 32), 32),
                subagent_spawn_stagger_secs=_safe_float(
                    agent_data.get("subagent_spawn_stagger_secs", 2.0), 2.0
                ),
                subagent_max_turns=agent_data.get("subagent_max_turns", 100),
                subagent_timeout_secs=agent_data.get("subagent_timeout_secs", 1800),
                subagent_stall_idle_secs=_safe_int(
                    agent_data.get("subagent_stall_idle_secs", 120), 120
                ),
                completion_keep=_validated_completion_keep(
                    agent_data.get("completion_keep", "head")
                ),
                completion_keep_chars=_safe_int(agent_data.get("completion_keep_chars", 3000), 3000),
                subagent_result_ttl_secs=_safe_int(agent_data.get("subagent_result_ttl_secs", 3600), 3600),
                subagent_cwd_allowed_roots=list(
                    agent_data.get(
                        "subagent_cwd_allowed_roots",
                        ["~/workspace", "~/workspaces", "~/workplace", "~/workplaces"],
                    )
                ),
                log_level=agent_data.get("log_level", "WARNING").upper(),
                bot_name=_sanitize_bot_name(agent_data.get("bot_name", "")),
                max_channels=agent_data.get("max_channels", 1),
                max_channel_agents=agent_data.get("max_channel_agents", 3),
                soft_stop_budget_secs=max(
                    0.5, min(60.0, _safe_float(agent_data.get("soft_stop_budget_secs", 10.0), 10.0))
                ),
            ),
            session=SessionConfig(
                timeout_secs=session_data.get("timeout_secs", DEFAULT_SESSION_TIMEOUT),
                empty_response_auto_continue=bool(
                    session_data.get("empty_response_auto_continue", True)
                ),
                autocompact_pct=_safe_float(session_data.get("autocompact_pct", 90.0), 90.0),
                pool_size=_safe_int(session_data.get("pool_size", 2), 2),
                pool_agent=str(session_data.get("pool_agent", "")),
                pool_ttl_secs=_safe_int(session_data.get("pool_ttl_secs", 1800), 1800),
                archive_retention_days=_archive_retention_days(session_data),
                watchdog_rss_max_mb=_safe_int(session_data.get("watchdog_rss_max_mb", 0), 0),
            ),
            taskrunner=TaskRunnerConfig(
                max_parallel_steps=taskrunner_data.get(
                    "max_parallel_steps", DEFAULT_MAX_PARALLEL_STEPS
                ),
                workspace_dir=str(taskrunner_data.get("workspace_dir", "")),
            ),
            cron_history=CronHistoryConfig(
                cron_summary_cap=_safe_int(cron_history_data.get("cron_summary_cap", 200), 200),
                cron_trace_cap_kb=_safe_int(cron_history_data.get("cron_trace_cap_kb", 50), 50),
                cron_max_records_per_job=_safe_int(
                    cron_history_data.get("cron_max_records_per_job", 100), 100
                ),
                cron_max_index_records=_safe_int(cron_history_data.get("cron_max_index_records", 2000), 2000),
            ),
            messaging=MessagingConfig(
                use_transport=bool(messaging_data.get("use_transport", True)),
                dm_scope=str(messaging_data.get("dm_scope", "per-channel-peer")),
                idle_reset_minutes=_coerce_int(messaging_data.get("idle_reset_minutes"), 0),
                daily_reset_hour=_coerce_int(messaging_data.get("daily_reset_hour"), -1),
                queue_mode=str(messaging_data.get("queue_mode", "steer")),
            ),
            # orchestrator/watchdog were advertised in config-baseline.json and
            # served by /api/config/schema, and real consumers read them
            # (acp/session_handle.py, dashboard/chat_orchestrator.py), but load()
            # never passed these kwargs — so config.json values were silently
            # ignored and the dataclass defaults always won.
            orchestrator=OrchestratorConfig(
                stage_timeout_seconds=_safe_int(
                    orchestrator_data.get("stage_timeout_seconds", 1800), 1800
                ),
            ),
            watchdog=WatchdogConfig(
                check_after_secs=_safe_float(watchdog_data.get("check_after_secs", 60.0), 60.0),
                stale_window_secs=_safe_float(watchdog_data.get("stale_window_secs", 300.0), 300.0),
                tool_stall_suspect_secs=_safe_float(
                    watchdog_data.get("tool_stall_suspect_secs", 10800.0), 10800.0
                ),
                tool_stall_hard_cap_secs=_safe_float(
                    watchdog_data.get("tool_stall_hard_cap_secs", 10800.0), 10800.0
                ),
                model_silent_probe_secs=_safe_float(
                    watchdog_data.get("model_silent_probe_secs", 900.0), 900.0
                ),
                wellness_sample_secs=_safe_float(
                    watchdog_data.get("wellness_sample_secs", 3.0), 3.0
                ),
            ),
            telemetry=TelemetryConfig(
                enabled=bool(telemetry_data.get("enabled", False)),
                local_dir=str(telemetry_data.get("local_dir", "")),
                export_interval_seconds=_safe_int(
                    telemetry_data.get("export_interval_seconds", 60), 60
                ),
                retention_days=_safe_int(telemetry_data.get("retention_days", 0), 0),
                max_total_mb=_safe_int(telemetry_data.get("max_total_mb", 0), 0),
                otlp_endpoint=str(telemetry_data.get("otlp_endpoint", "")),
            ),
            memory=MemoryConfig(
                embedding_provider=_coerce_embedding_provider(
                    memory_data.get("embedding_provider", "llama_cpp")
                ),
                embedding_dim=memory_data.get("embedding_dim", 1024),
                embed_model_url=memory_data.get("embed_model_url", ""),
                semantic_confidence_threshold=memory_data.get("semantic_confidence_threshold", 0.8),
                episodic_dedup_threshold=memory_data.get("episodic_dedup_threshold", 0.88),
                episodic_max_results=memory_data.get("episodic_max_results", 8),
                episodic_max_count=memory_data.get("episodic_max_count", 10_000),
                semantic_keys=memory_data.get("semantic_keys", []),
                history_idle_hours=memory_data.get("history_idle_hours", 3.0),
                history_max_days=memory_data.get("history_max_days", 365),
                migrated=memory_data.get("migrated", False),
            ),
            knowledge=KnowledgeConfig(
                auto_ingest_artifacts=bool(knowledge_data.get("auto_ingest_artifacts", True)),
                auto_ingest_artifact_kinds=[
                    k
                    for k in knowledge_data.get(
                        "auto_ingest_artifact_kinds",
                        DEFAULT_AUTO_INGEST_ARTIFACT_KINDS,
                    )
                    if isinstance(k, str)
                ],
                max_ingest_file_mb=(
                    float(mb)
                    if isinstance(
                        (mb := knowledge_data.get("max_ingest_file_mb", 100.0)),
                        (int, float),
                    )
                    and not isinstance(mb, bool)
                    and mb >= 0
                    else 100.0
                ),
                embed_timeout_secs=_safe_float(knowledge_data.get("embed_timeout_secs", 10.0), 10.0),
                embed_content_budget=_safe_int(knowledge_data.get("embed_content_budget", 0), 0),
                pool_idle_ttl_secs=(
                    ttl
                    if isinstance((ttl := knowledge_data.get("pool_idle_ttl_secs", 300)), int)
                    and not isinstance(ttl, bool)
                    and ttl >= 0
                    else 300
                ),
                auto_ingest_doc_links=bool(knowledge_data.get("auto_ingest_doc_links", False)),
                doc_ingest_hosts=[
                    str(h)
                    for h in knowledge_data.get("doc_ingest_hosts", [])
                    if isinstance(h, str) and h.strip()
                ],
            ),
            telegram=TelegramConfig(
                enabled=bool(telegram_data.get("enabled", False)),
                bot_token=str(telegram_data.get("bot_token", "")),
                allowed_user_ids=_coerce_int_ids(telegram_data.get("allowed_user_ids")),
                soft_threshold_pct=max(
                    1, min(100, _coerce_int(telegram_data.get("soft_threshold_pct"), 80))
                ),
                allow_forum=bool(telegram_data.get("allow_forum", False)),
                allowed_forum_chat_ids=_coerce_int_ids(telegram_data.get("allowed_forum_chat_ids")),
            ),
            discord=DiscordConfig(
                enabled=bool(discord_data.get("enabled", False)),
                bot_token=str(discord_data.get("bot_token", "")),
                # Discord user IDs are numeric snowflakes that exceed 2^53 —
                # keep them as strings (JSON round-trip safe, matches the
                # transport's string comparison).
                allowed_user_ids=_coerce_str_ids(discord_data.get("allowed_user_ids")),
                allowed_thread_ids=_coerce_str_ids(discord_data.get("allowed_thread_ids")),
                soft_threshold_pct=max(
                    1, min(100, _coerce_int(discord_data.get("soft_threshold_pct"), 80))
                ),
            ),
            webex=WebexConfig(
                enabled=bool(webex_data.get("enabled", False)),
                bot_token=str(webex_data.get("bot_token", "")),
                allowed_emails=(
                    [e for e in webex_data.get("allowed_emails", []) if isinstance(e, str) and e]
                    if isinstance(webex_data.get("allowed_emails", []), list)
                    else []
                ),
                soft_threshold_pct=_coerce_int(webex_data.get("soft_threshold_pct"), 80),
                hard_threshold_pct=_coerce_int(webex_data.get("hard_threshold_pct"), 95),
            ),
            slack=SlackConfig(
                allowed_users=[
                    u
                    for u in slack_data.get("allowed_users", [])
                    if isinstance(u, dict) and u.get("slack_id")
                ],
                tracking_channels=_validate_tracking_channels(
                    slack_data.get("tracking_channels", [])
                ),
                open_channels=[
                    c for c in slack_data.get("open_channels", []) if isinstance(c, str)
                ],
                command=slack_data.get("command", "kirocrew"),
                forward_to_agent_callback=str(
                    slack_data.get("forward_to_agent_callback") or ""
                ).strip(),
                trusted_bot_ids=set(slack_data.get("trusted_bot_ids", [])),
                allowed_enterprise_ids=[
                    e
                    for e in slack_data.get("allowed_enterprise_ids", [])
                    if isinstance(e, str) and (e.startswith("E") or e.startswith("T"))
                ],
                reactions={
                    k: v
                    for k, v in slack_data.get("reactions", {}).items()
                    if isinstance(k, str) and (v is None or (isinstance(v, str) and v))
                },
                reactions_enabled=bool(slack_data.get("reactions_enabled", True)),
                use_tunnel_url=bool(slack_data.get("use_tunnel_url", False)),
                show_thinking=bool(slack_data.get("show_thinking", True)),
            ),
            publish=PublishConfig(
                allowed_destinations=[
                    d
                    for d in publish_data.get("allowed_destinations", [])
                    if isinstance(d, str) and d
                ],
                relocate_roots=[
                    r
                    for r in publish_data.get("relocate_roots", [])
                    if isinstance(r, str) and r.strip()
                ],
            ),
            wechat=WeComConfig(
                enabled=bool(wechat_data.get("enabled", False)),
                allowed_users=[
                    u
                    for u in wechat_data.get("allowed_users", [])
                    if isinstance(u, dict) and u.get("userid")
                ],
                ws_url=str(wechat_data.get("ws_url", "wss://openws.work.weixin.qq.com")),
                soft_threshold_pct=int(wechat_data.get("soft_threshold_pct", 80)),
                hard_threshold_pct=int(wechat_data.get("hard_threshold_pct", 95)),
            ),
            dashboard=DashboardConfig(
                url=dashboard_data.get("url", ""),
                restore_sessions=dashboard_data.get("restore_sessions", False),
                restore_window_minutes=dashboard_data.get("restore_window_minutes", 30),
                bot_name=dashboard_data.get("bot_name", ""),
                avatar=dashboard_data.get("avatar", ""),
                merge_queued_messages=dashboard_data.get("merge_queued_messages", False),
                mcp_probe_timeout_secs=_safe_int(
                    dashboard_data.get("mcp_probe_timeout_secs", 15), 15
                ),
                auto_open_browser=dashboard_data.get("auto_open_browser", True),
                quick_send=dashboard_data.get("quick_send", False),
                session_grid=dashboard_data.get("session_grid", False),
                widget_density=dashboard_data.get("widget_density", "more"),
                tail_fork_enabled=dashboard_data.get("tail_fork_enabled", False),
                terminal=dashboard_data.get("terminal", {"enabled": True}),
                default_project=dashboard_data.get("default_project", ""),
                theme_mode=dashboard_data.get("theme_mode", ""),
                sso_login_flags=str(dashboard_data.get("sso_login_flags", "")),
                theme_color=dashboard_data.get("theme_color", ""),
                recent_tint_count=_safe_int(dashboard_data.get("recent_tint_count", 0), 0),
                onboarded=bool(dashboard_data.get("onboarded", False)),
                tips_enabled=bool(dashboard_data.get("tips_enabled", True)),
                tips_cadence_hours=_safe_float(
                    dashboard_data.get("tips_cadence_hours", 6.0), 6.0, lo=0.0
                ),
                tips_snooze_hours=_safe_float(
                    dashboard_data.get("tips_snooze_hours", 48.0), 48.0, lo=0.0
                ),
                tips_recency_decay=_safe_float(
                    dashboard_data.get("tips_recency_decay", 0.6), 0.6, lo=0.0, hi=1.0
                ),
                tips_model=str(dashboard_data.get("tips_model", "claude-haiku-4.5")),
                tips_explore_ratio=_safe_float(
                    dashboard_data.get("tips_explore_ratio", 0.2), 0.2, lo=0.0, hi=1.0
                ),
            ),
            tunnel=TunnelConfig(
                enabled=bool(tunnel_data.get("enabled", False)),
                name_mode=str(tunnel_data.get("name_mode", "username")),
                name_override=str(tunnel_data.get("name_override", "")),
            ),
            hooks=data.get("hooks", {}),
            agents=agents,
            default_agent=default_agent_val,
            workspaces=workspaces,
            default_workspace=data.get("default_workspace", "default"),
            memory_stores=memory_stores,
            default_memory_store=default_memory_store_val,
            stt=SttConfig(
                enabled=stt_data.get("enabled", False),
                provider=_validated_stt_provider(stt_data.get("provider", "whisper")),
                whisper_path=stt_data.get("whisper_path", ""),
                # Default changed from "base" to "turbo" — turbo is faster and
                # recommended for most users (809M vs 74M, but much better latency).
                model=stt_data.get("model", "turbo"),
                mlx_model=stt_data.get("mlx_model", "mlx-community/whisper-large-v3-turbo"),
                device=stt_data.get("device", "cpu"),
                timeout_secs=stt_data.get("timeout_secs", 300),
                transcribe_region=stt_data.get("transcribe_region", "us-east-1"),
                transcribe_profile=stt_data.get("transcribe_profile", ""),
                language_code=stt_data.get("language_code", "en-US"),
                streaming=stt_data.get("streaming", False),
            ),
            auto_update=data.get("auto_update", True),
            timezone=data.get("timezone", ""),
            snapshot_dir=data.get("snapshot_dir", ""),
            registries=[
                ExternalRegistryConfig(
                    name=str(r.get("name", "")),
                    repo=str(r.get("repo", "")),
                    branch=str(r.get("branch", "mainline")),
                )
                for r in (data.get("registries") or [])
                if isinstance(r, dict) and r.get("repo")
            ],
            mcp_gateway=McpGatewayConfig(
                enabled=bool(mcp_gateway_data.get("enabled", False)),
                socket_path=str(mcp_gateway_data.get("socket_path", "")),
                overlay_dir=str(mcp_gateway_data.get("overlay_dir", "")),
                idle_timeout_secs=max(10, _safe_int(mcp_gateway_data.get("idle_timeout_secs", 300), 300)),
                max_backends=max(1, _safe_int(mcp_gateway_data.get("max_backends", 64), 64)),
                poolable_servers=[
                    s for s in mcp_gateway_data.get("poolable_servers", []) if isinstance(s, str)
                ],
                prewarm_count=max(0, _safe_int(mcp_gateway_data.get("prewarm_count", 0), 0)),
                read_buffer_limit_bytes=max(
                    1024,
                    _safe_int(
                        mcp_gateway_data.get("read_buffer_limit_bytes", 64 * 1024 * 1024),
                        64 * 1024 * 1024,
                    ),
                ),
                response_spill_threshold_bytes=max(
                    0,
                    _safe_int(
                        mcp_gateway_data.get("response_spill_threshold_bytes", 256 * 1024),
                        256 * 1024,
                    ),
                ),
            ),
            instances=InstancesConfig(
                enabled=bool(instances_data.get("enabled", False)),
                warm_set_cap=_safe_int(
                    instances_data.get("warm_set_cap", _DEFAULT_WARM_SET_CAP), _DEFAULT_WARM_SET_CAP
                ),
                tunnel_base_port=_safe_int(
                    instances_data.get("tunnel_base_port", _DEFAULT_TUNNEL_BASE_PORT),
                    _DEFAULT_TUNNEL_BASE_PORT,
                ),
                ssh_compression=bool(
                    instances_data.get("ssh_compression", _DEFAULT_SSH_COMPRESSION)
                ),
                max_recovery_attempts=_safe_int(
                    instances_data.get("max_recovery_attempts", _DEFAULT_MAX_RECOVERY),
                    _DEFAULT_MAX_RECOVERY,
                ),
                recover_backoff_max_secs=_safe_float(
                    instances_data.get("recover_backoff_max_secs", _DEFAULT_BACKOFF_MAX),
                    _DEFAULT_BACKOFF_MAX,
                ),
                probe_failure_threshold=_safe_int(
                    instances_data.get("probe_failure_threshold", _DEFAULT_PROBE_FAILS),
                    _DEFAULT_PROBE_FAILS,
                ),
            ),
            heartbeat=HeartbeatConfig(default_deliver=heartbeat_default_deliver),
            skills=SkillsConfig(
                max_triggered=_safe_int(skills_data.get("max_triggered", 3), 3),
                lazy_load=bool(skills_data.get("lazy_load", False)),
                auto_create_from_sessions=bool(skills_data.get("auto_create_from_sessions", False)),
                auto_refine_on_deviation=bool(skills_data.get("auto_refine_on_deviation", False)),
                auto_min_tool_calls=_safe_int(skills_data.get("auto_min_tool_calls", 5), 5),
                auto_similarity_threshold=_safe_float(skills_data.get("auto_similarity_threshold", 0.85), 0.85),
                extra_paths=list(skills_data.get("extra_paths", [])),
            ),
            slack_channels={
                ch_id: ChannelConfig.from_dict(ch_data)
                for ch_id, ch_data in (
                    slack_data.get("channels", {})
                    if isinstance(slack_data.get("channels"), dict)
                    else {}
                ).items()
                if isinstance(ch_data, dict)
            },
            slack_dm_activation=_validate_activation(
                slack_data.get("dm_activation", ACTIVATION_ALWAYS)
            ),
            observe_max_messages=max(
                1, _safe_int(slack_data.get("observe_max_messages", 200), 200)
            ),
            observe_ttl_hours=max(
                0.0, _safe_float(slack_data.get("observe_ttl_hours", 168.0), 168.0)
            ),
            _extra_sections=extra_sections,
        )

        # Write-back migration: if the on-disk config has legacy format
        # (flat workspace strings, missing sections), back up the original
        # and save the migrated version.  One-shot — subsequent loads see
        # the canonical format and skip.
        try:
            needs_migration = False
            # Flat workspace strings → need migration to {"dir": ...}
            for v in raw_workspaces.values():
                if isinstance(v, str):
                    needs_migration = True
                    break

            # One-time migration: create default agent when none exists
            if not cfg.agents:
                kiro = cfg.agent.default_agent or "kirocrew"
                cfg.agents["default"] = KiroCrewAgentConfig(
                    kiro_agent=kiro,
                    workspace="default",
                    memory_store="default",
                )
                needs_migration = True
            if not cfg.default_agent or cfg.default_agent not in cfg.agents:
                # Prefer "default" if it exists, otherwise use first available agent
                if "default" in cfg.agents:
                    cfg.default_agent = "default"
                elif cfg.agents:
                    cfg.default_agent = next(iter(cfg.agents))
                else:
                    cfg.default_agent = "default"
                needs_migration = True

            if needs_migration:
                backup = path.with_suffix(".json.bak")
                import shutil

                shutil.copy2(path, backup)
                logger.info(
                    "Config migrated — backup saved to %s",
                    backup,
                )
                cfg.save()
        except Exception as e:
            # Migration write-back is best-effort; never block startup.
            logger.warning("Config write-back failed: %s", e)

        return cfg

    def to_dict(self) -> dict:
        """Serialize config to the JSON structure used by config.json."""
        from dataclasses import asdict

        d: dict = {
            "agent": asdict(self.agent),
            "session": asdict(self.session),
            "memory": asdict(self.memory),
            "slack": asdict(self.slack),
            "publish": asdict(self.publish),
            "telegram": asdict(self.telegram),
            "discord": asdict(self.discord),
            "webex": asdict(self.webex),
            "dashboard": asdict(self.dashboard),
            "tunnel": asdict(self.tunnel),
            "hooks": self.hooks,
            "agents": {name: asdict(agent_cfg) for name, agent_cfg in self.agents.items()},
            "default_agent": self.default_agent,
            "workspaces": {name: asdict(ws_cfg) for name, ws_cfg in self.workspaces.items()},
            "default_workspace": self.default_workspace,
            "memory_stores": {name: asdict(ms_cfg) for name, ms_cfg in self.memory_stores.items()},
            "default_memory_store": self.default_memory_store,
            "stt": asdict(self.stt),
            "instances": asdict(self.instances),
            "mcp_gateway": asdict(self.mcp_gateway),
            "taskrunner": asdict(self.taskrunner),
            "orchestrator": asdict(self.orchestrator),
            "watchdog": asdict(self.watchdog),
            "messaging": asdict(self.messaging),
            "cron_history": asdict(self.cron_history),
            "knowledge": asdict(self.knowledge),
            "heartbeat": asdict(self.heartbeat),
            "skills": asdict(self.skills),
            "telemetry": asdict(self.telemetry),
            "snapshot_dir": self.snapshot_dir,
            "timezone": self.timezone,
            "auto_update": self.auto_update,
        }
        # External registries (always serialized so save() round-trips the field)
        d["registries"] = [asdict(r) for r in self.registries]
        # Re-emit unknown/edition-contributed top-level sections captured at
        # load() so save()/PATCH does not silently drop them. A known section
        # never appears here (only keys absent from d are restored), so this can
        # never clobber a core section with a stale captured copy.
        for _k, _v in self._extra_sections.items():
            if _k not in d:
                d[_k] = _v
        # Preserve per-channel activation settings on round-trip
        slack_section = d.setdefault("slack", {})
        if self.slack_channels:
            slack_section["channels"] = {
                ch_id: asdict(cfg) for ch_id, cfg in self.slack_channels.items()
            }
        if self.slack_dm_activation != ACTIVATION_ALWAYS:
            slack_section["dm_activation"] = self.slack_dm_activation
        slack_section["observe_max_messages"] = self.observe_max_messages
        if self.slack.trusted_bot_ids:
            slack_section["trusted_bot_ids"] = sorted(self.slack.trusted_bot_ids)
        else:
            slack_section.pop("trusted_bot_ids", None)
        slack_section["observe_ttl_hours"] = self.observe_ttl_hours
        return d

    def save(self) -> None:
        """Write current config to ~/.kirocrew/config.json.

        Stamps a ``meta`` block with the current version and timestamp
        so we can tell which build last touched the file.

        Values that exist in ``config.local.json`` are stripped from the
        output to prevent overlay settings from leaking into the base file.
        """

        meta = {
            "lastTouchedVersion": __version__,
            "lastTouchedAt": datetime.now(timezone.utc).isoformat(),
        }
        d = self.to_dict()

        # Strip overlay-owned values so they don't leak into config.json
        local_path = config_local_path()
        if local_path.is_file():
            try:
                raw_local = json.loads(local_path.read_text(encoding="utf-8"))
                if isinstance(raw_local, dict):
                    d = _subtract_overlay(d, raw_local)
            except (json.JSONDecodeError, OSError):
                pass

        d = {"meta": meta, **d}
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        # Drop the validated-data cache so the next load() re-reads this write.
        # mtime-keying already detects the change; this makes it immediate even
        # if the filesystem mtime resolution is coarse.
        _invalidate_config_cache()

    @staticmethod
    def _resolve_agent_model() -> str:
        """Read model from installed agent config, falling back to bundled defaults."""
        # Installed agent config (generated by kirocrew setup)
        agent_json = Path.home() / ".kiro" / "agents" / "kirocrew.json"
        if agent_json.is_file():
            try:
                data = json.loads(agent_json.read_text(encoding="utf-8"))
                model = data.get("model", "")
                if model:
                    return model
            except (json.JSONDecodeError, OSError):
                pass
        # Bundled defaults.json
        bundled = config_package_dir() / "defaults.json"
        if bundled.is_file():
            try:
                data = json.loads(bundled.read_text(encoding="utf-8"))
                model = data.get("model", "")
                if model:
                    return model
            except (json.JSONDecodeError, OSError):
                pass
        return DEFAULT_MODEL

    @staticmethod
    def _resolve_named_agent_model(agent: str, agents_dir: Path | None = None) -> str:
        """Return a named agent's own kiro ``model`` field, or ``""`` if none.

        Used by :meth:`SessionManager.get_or_create` so an explicit global
        ``agent.model`` does not override an agent that pins its own model — the
        global default must rank *below* a per-agent pin. Returns the kiro
        ``model`` slot only; ``""`` when the agent declares none, so the caller
        falls back to the global. ``agents_dir`` overrides the lookup directory
        (a dependency-injection seam for tests); defaults to ``kiro_agents_dir()``.
        """
        if not agent:
            return ""
        base = agents_dir if agents_dir is not None else kiro_agents_dir()
        for af in base.glob("*.json"):
            try:
                ad = json.loads(af.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            # Skip stray non-object JSON a user may have dropped in the dir.
            if isinstance(ad, dict) and (ad.get("name") == agent or af.stem == agent):
                return ad.get("model") or ""
        return ""

    def load_credentials(self) -> dict[str, str]:
        """Load credentials from ~/.kirocrew/.env and environment variables.

        .env format: KEY=VALUE (one per line, # comments, no quotes required).
        Environment variables override .env values.
        """
        creds: dict[str, str] = {}
        ep = env_path()
        if ep.exists():
            # Enforce restrictive permissions on credential file
            try:
                if ep.stat().st_mode & 0o077:
                    ep.chmod(0o600)
            except OSError:
                logger.warning("Cannot enforce permissions on %s", ep)
            for line in ep.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    creds[k.strip()] = v.strip()

        for key in _CREDENTIAL_KEYS:
            val = os.environ.get(key)
            if val:
                creds[key] = val

        # Propagate credentials into the process environment so spawned children
        # (sandboxed agents, MCP servers, cron-fired subprocesses) inherit them
        # via Popen's default env=os.environ.copy() — even when their view of
        # ~/.kirocrew/.env is a bind-mounted empty file. setdefault() preserves
        # any value the caller already set explicitly.
        for k, v in creds.items():
            if v:
                os.environ.setdefault(k, v)

        return creds

    def create_provider_factory(self) -> Callable:
        """Return a factory that creates LLMProvider instances from config.

        KiroCrew is KiroACP-only: the sole provider is the ACP adapter driving
        the kiro-cli backend. The factory accepts an optional ``session_key`` to
        create a per-session subdirectory under ``workspace_root()``.
        """
        from kiro_crew.providers.acp import (
            AcpProvider,  # circular: acp -> client -> session -> config.loader
        )

        model = self.agent.model
        if model == DEFAULT_MODEL:
            model = self._resolve_agent_model()

        sandbox = self.agent.sandbox
        tool_search = self.agent.tool_search

        # MCP gateway: resolve overlay + socket once when enabled. None when
        # the feature flag is off -> AcpClient falls through to per-session MCP.
        _gw = self.mcp_gateway
        if _gw.enabled:
            _gw_overlay = _gw.overlay_dir or str(default_overlay_dir())
            _gw_socket = _gw.socket_path or str(default_socket_path())
            _gw_settings = str(Path(_gw_overlay).parent / "settings" / "mcp.json")
        else:
            _gw_overlay = None
            _gw_socket = None
            _gw_settings = None

        def _acp(
            session_key: str | None = None,
            agent: str | None = None,
            channel_id: str | None = None,
            model_override: str | None = None,
            cwd: str | None = None,
            extra_env: dict[str, str] | None = None,
            reasoning_effort_override: str | None = None,
            **_kwargs: object,
        ) -> AcpProvider:
            wdir = Path(cwd) if cwd else _session_work_dir(session_key)
            # Resolve the model: slot override, else the default kirocrew model,
            # else the custom agent's own model. Custom agents MUST resolve here
            # because the ACP session/set_mode path switches prompt/tools but not
            # the model, so an unset model makes kiro fall back to cli.json's
            # chat.defaultModel. Use _resolve_named_agent_model (the kiro model
            # slot) to match this backend. Returns "" when none is declared;
            # AcpClient normalizes "" to DEFAULT_MODEL, same as None.
            if model_override:
                m = model_override
            elif not agent or agent == "kirocrew":
                m = model
            else:
                m = self._resolve_named_agent_model(agent)
            # Translation boundary (mirrors the _claude_code factory): the model
            # may be a canonical registry key (e.g. "opus-4.8-1m" — the wire /
            # dropdown value after /api/models canonicalization) OR an already-
            # resolved kiro id. kiro-cli's session/set_model only accepts its own
            # advertised ids (bare dotted, e.g. "claude-opus-4.8"), so translate
            # the canonical key to the "acp" id — otherwise it reaches set_model
            # and kiro rejects it ("The model 'opus-4.8-1m' is not available").
            # to_acp_id (NOT to_provider_id) resolves ONLY canonical keys: kiro's
            # native ids and their aliases (claude-haiku-4.5, claude-sonnet-4.5,
            # …) are DISTINCT real kiro models and must pass through unchanged,
            # not get folded to Sonnet the way the claude_code path downgrades
            # them (the claude backend has no Haiku).
            m = model_registry.to_acp_id(m) if m else m
            # Thread the slot's effort into a per-model override so the kiro
            # cli.json overlay is written from it at spawn — without this, a
            # kiro cold start (or the handler's reset-then-respawn) would only
            # pick up effort already recovered from a pre-existing overlay,
            # never the freshly-set slot value. Mirrors the _claude_code path.
            _eff_per_model: dict[str, str] = {}
            if (
                m
                and reasoning_effort_override
                and is_valid_effort(reasoning_effort_override)
                and model_supports_effort(m)
            ):
                _eff_per_model[m] = reasoning_effort_override
            return AcpProvider(
                work_dir=wdir,
                model=m,
                agent=agent,
                sandbox_mode=sandbox,
                session_key=session_key,
                channel_id=channel_id,
                extra_env=extra_env,
                effort_per_model=_eff_per_model,
                tool_search=tool_search,
                mcp_gateway_overlay=_gw_overlay,
                mcp_gateway_settings_mcp_json=_gw_settings,
                mcp_gateway_socket=_gw_socket,
            )

        return _acp


def build_provider_factory(cfg: "KiroCrewConfig") -> Callable:
    """Return the LLM-provider factory for *cfg*, via the platform seam.

    Routes through ``current_context().providers.create_factory(cfg)`` (the CPP
    ``ProviderRegistry`` extension point) instead of calling
    ``cfg.create_provider_factory()`` directly, so an edition can supply an
    alternate provider factory (e.g. re-registering an extra ACP backend through
    the dormant ``ACP_BACKEND_*`` seam).  The ``Default`` ProviderRegistry returns
    exactly ``cfg.create_provider_factory()``, so the public edition is
    behaviorally identical to calling it directly.

    Fail-closed: a :class:`PlatformCompositionError` (a non-standalone host that
    could not compose its companion) propagates.  Any other transient lookup
    failure degrades to ``cfg.create_provider_factory()`` so an unbooted /
    standalone call site never breaks — it just gets the public factory.

    The fallback is passed as ``fallback_factory`` (a lazy thunk), NOT eagerly:
    ``cfg.create_provider_factory()`` is built ONLY on the degrade path, so the
    standalone happy path builds the factory exactly once (the Default
    ``ProviderRegistry`` already returns ``cfg.create_provider_factory()``, so an
    eager fallback would build it a second time on every session/reload).  A
    failure INSIDE ``cfg.create_provider_factory()`` itself is handled by
    ``safe_context_call`` (which guards the factory call) rather than escaping
    uncaught; with no eager ``fallback`` here there is no usable factory, so a
    composition error propagates (fail-closed) and any other error re-raises —
    a corrupt-config failure surfaces at the factory site, it is not swallowed.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    return safe_context_call(
        lambda: current_context().providers.create_factory(cfg),
        fallback_factory=lambda: cfg.create_provider_factory(),
        log_message="providers.create_factory failed; using cfg.create_provider_factory()",
    )


# ---------------------------------------------------------------------------
# Agent resolver and kiro agent validation
# ---------------------------------------------------------------------------


def _workspace_name_for_dir(config: KiroCrewConfig, ws_dir: Path) -> str:
    """Find the workspace name whose dir matches *ws_dir*."""
    for name, ws_cfg in config.workspaces.items():
        if Path(ws_cfg.dir) == ws_dir:
            return name
    return "default"


def resolve_agent_bindings(
    config: KiroCrewConfig,
    agent_name: str | None = None,
) -> ResolvedBindings:
    """Resolve workspace, memory store, and kiro agent for a session.

    Resolution:
    1. If agent_name is given and exists in config.agents → use its bindings
    2. Otherwise use config.default_agent (guaranteed to exist by load())
    """
    import dataclasses as _dc

    # Step 1: explicit agent_name
    if agent_name and agent_name in config.agents:
        agent_cfg = config.agents[agent_name]
    elif config.default_agent and config.default_agent in config.agents:
        # Step 2: default_agent (guaranteed valid by load())
        agent_cfg = config.agents[config.default_agent]
    elif config.agents:
        # Defensive: default_agent not in agents, use first available
        first_name = next(iter(config.agents))
        logger.warning(
            "default_agent '%s' not found in agents, using '%s'",
            config.default_agent,
            first_name,
        )
        agent_cfg = config.agents[first_name]
    else:
        # No agents at all — return safe defaults
        logger.warning("No agents configured, using bare defaults")
        return ResolvedBindings(
            workspace_dir=Path("workspace"),
            memory_store_name=config.default_memory_store,
            effective_memory_config=_dc.asdict(config.memory),
            kiro_agent=config.agent.default_agent,
        )

    # Resolve workspace
    ws_name = agent_cfg.workspace
    if ws_name in config.workspaces:
        ws_dir = Path(config.workspaces[ws_name].dir)
    else:
        logger.warning(
            "Agent workspace '%s' not found, falling back to default_workspace '%s'",
            ws_name,
            config.default_workspace,
        )
        fallback_ws = config.workspaces.get(config.default_workspace)
        ws_dir = Path(fallback_ws.dir) if fallback_ws else Path("workspace")

    # Resolve memory store
    store_name = agent_cfg.memory_store
    if store_name not in config.memory_stores:
        logger.warning(
            "Agent memory_store '%s' not found, falling back to '%s'",
            store_name,
            config.default_memory_store,
        )
        store_name = config.default_memory_store

    kiro_agent = agent_cfg.kiro_agent

    # Build effective memory config via dict-level merge
    store_cfg = config.memory_stores.get(store_name)
    store_dict = _dc.asdict(store_cfg) if store_cfg else {}
    top_level_memory = _dc.asdict(config.memory)
    effective_memory = resolve_memory_store_config(top_level_memory, store_dict)

    return ResolvedBindings(
        workspace_dir=ws_dir,
        memory_store_name=store_name,
        effective_memory_config=effective_memory,
        kiro_agent=kiro_agent,
    )


def validate_kiro_agent_references(
    config: KiroCrewConfig,
    installed_agents: list[str],
) -> None:
    """Cross-reference kiro_agent values against installed agents.

    Logs warnings for unresolved references. Never raises.
    """
    installed_names = set(installed_agents)
    for mc_name, mc_agent in config.agents.items():
        if mc_agent.kiro_agent and mc_agent.kiro_agent not in installed_names:
            logger.warning(
                "KiroCrew agent '%s' references kiro agent '%s' " "which is not installed",
                mc_name,
                mc_agent.kiro_agent,
            )
