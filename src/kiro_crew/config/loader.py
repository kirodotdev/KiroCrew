"""Configuration loader for KiroCrew.

Config location: ~/.kiro/crew/config.json (overridden by KIROCREW_HOME)
Credentials:    ~/.kiro/crew/.env (overridden by KIROCREW_HOME)

KiroCrew is KiroACP-only: the sole provider is the ACP adapter driving the
kiro-cli backend. This module handles session timeouts, hook rules, and the
dashboard URL via the config file. (The dashboard *port* is set with the
``KIROCREW_PORT`` env var, not a config key.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re as _re
import shutil
import stat as _stat
import threading
import uuid
from collections.abc import Callable, Iterable, MutableMapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit as _urlsplit

from kiro_crew import __version__, model_registry, platform_compat

# Leaf module (stdlib + platform_compat only) — no import cycle with config.
from kiro_crew.atomic_write import atomic_write

# Computer-use defaults/ceilings come from the feature's constants module rather
# than being re-spelled here (AGENTS.md: no hardcoded values in business logic).
# ``computer_use.types`` is deliberately dependency-free — it imports nothing from
# ``kiro_crew`` — so this cannot create an import cycle with the loader, and the
# ``computer_use`` package's ``__init__`` pulls in only ``platform_compat`` /
# ``executors`` (both stdlib-only), never ``config``.
from kiro_crew.computer_use.types import DEFAULT_ATTACH_SCREENSHOT as _CU_DEFAULT_ATTACH_SCREENSHOT
from kiro_crew.computer_use.types import DEFAULT_MAX_TREE_DEPTH as _CU_DEFAULT_MAX_TREE_DEPTH
from kiro_crew.computer_use.types import DEFAULT_MAX_TREE_NODES as _CU_DEFAULT_MAX_TREE_NODES
from kiro_crew.computer_use.types import (
    DEFAULT_SCREENSHOT_JPEG_QUALITY as _CU_DEFAULT_SCREENSHOT_JPEG_QUALITY,
)
from kiro_crew.computer_use.types import DEFAULT_SCREENSHOT_MAX_PX as _CU_DEFAULT_SCREENSHOT_MAX_PX
from kiro_crew.computer_use.types import DEFAULT_TEXT_LIMIT as _CU_DEFAULT_TEXT_LIMIT
from kiro_crew.computer_use.types import MAX_SCREENSHOT_MAX_PX as _CU_MAX_SCREENSHOT_MAX_PX
from kiro_crew.computer_use.types import MAX_TEXT_LIMIT as _CU_MAX_TEXT_LIMIT
from kiro_crew.computer_use.types import MAX_TREE_DEPTH_LIMIT as _CU_MAX_TREE_DEPTH
from kiro_crew.computer_use.types import MAX_TREE_NODES_LIMIT as _CU_MAX_TREE_NODES
from kiro_crew.computer_use.types import MIN_SCREENSHOT_MAX_PX as _CU_MIN_SCREENSHOT_MAX_PX

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
from kiro_crew.config.paths import (  # noqa: F401, kiro_agents_dir
    _WORKSPACE_DIR_NAME,
    CONFIG_DIR_NAME,
    OUTBOX_DIR_NAME,
    _default_workspace_base,
    _safe_dir_name,
    config_dir,
    config_package_dir,
    data_home,
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
from kiro_crew.effort import EFFORT_LEVELS, is_valid_effort, model_supports_effort
from kiro_crew.instances.constants import CONNECT_TIMEOUT_CEILING_SECS as _CONNECT_TIMEOUT_CEILING
from kiro_crew.instances.constants import DEFAULT_CONNECT_TIMEOUT_SECS as _DEFAULT_CONNECT_TIMEOUT
from kiro_crew.instances.constants import DEFAULT_MAX_RECOVERY_ATTEMPTS as _DEFAULT_MAX_RECOVERY
from kiro_crew.instances.constants import DEFAULT_MINT_TIMEOUT_SECS as _DEFAULT_MINT_TIMEOUT
from kiro_crew.instances.constants import DEFAULT_PROBE_FAILURE_THRESHOLD as _DEFAULT_PROBE_FAILS
from kiro_crew.instances.constants import DEFAULT_RECOVER_BACKOFF_MAX_SECS as _DEFAULT_BACKOFF_MAX
from kiro_crew.instances.constants import DEFAULT_SSH_COMPRESSION as _DEFAULT_SSH_COMPRESSION
from kiro_crew.instances.constants import DEFAULT_TUNNEL_BASE_PORT as _DEFAULT_TUNNEL_BASE_PORT
from kiro_crew.instances.constants import DEFAULT_WARM_SET_CAP as _DEFAULT_WARM_SET_CAP
from kiro_crew.instances.constants import MAX_RECOVERY_ATTEMPTS_CEILING as _MAX_RECOVERY_CEILING
from kiro_crew.instances.constants import MINT_TIMEOUT_CEILING_SECS as _MINT_TIMEOUT_CEILING
from kiro_crew.instances.constants import MINT_TIMEOUT_FLOOR_SECS as _MINT_TIMEOUT_FLOOR
from kiro_crew.instances.constants import (
    RECOVER_BACKOFF_MAX_CEILING_SECS as _RECOVER_BACKOFF_CEILING,
)
from kiro_crew.mcp_gateway.rewriter import default_overlay_dir, default_socket_path

logger = logging.getLogger(__name__)

# Top-level config.json keys that save() stamps itself rather than modelling as
# a section. They are neither parsed into a field nor round-tripped through
# to_dict(), so every consumer that classifies top-level keys — the
# _extra_sections capture below and validation.py's unrecognized-key warning —
# must exclude them, or KiroCrew warns the user about a key it wrote itself.
CONFIG_RESERVED_TOP_KEYS: frozenset = frozenset({"meta"})

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
        "wecom",
        "weixin",
        "teams",
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
        "computer_use",
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
        "session_summary",
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
CRED_MICROSOFT_APP_ID = "MICROSOFT_APP_ID"
CRED_MICROSOFT_APP_PASSWORD = "MICROSOFT_APP_PASSWORD"
CRED_MICROSOFT_APP_TENANT_ID = "MICROSOFT_APP_TENANT_ID"
CRED_WEIXIN_TOKEN = "WEIXIN_TOKEN"  # iLink bot credential from the Settings QR flow
CRED_JIRA_API_TOKEN = "JIRA_API_TOKEN"  # Jira Cloud/Server API token (resolved from .env)
# kiro-cli's OWN model credential. Unlike the gateway-owned channel tokens
# above, its rightful consumer is the agent subprocess itself (and the whoami
# identity probe), so it is deliberately NOT in sandbox._AGENT_DENIED_ENV_KEYS:
# the spawn paths re-inject it from the .env file after the Docker entrypoint
# scrubs it out of the gateway's /proc/<pid>/environ.
CRED_KIRO_API_KEY = "KIRO_API_KEY"
_CREDENTIAL_KEYS = (
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
    CRED_OWNER_ID,
    CRED_WECOM_BOT_ID,
    CRED_WECOM_SECRET,
    CRED_TELEGRAM_BOT_TOKEN,
    CRED_DISCORD_BOT_TOKEN,
    CRED_WEBEX_BOT_TOKEN,
    CRED_MICROSOFT_APP_ID,
    CRED_MICROSOFT_APP_PASSWORD,
    CRED_MICROSOFT_APP_TENANT_ID,
    CRED_WEIXIN_TOKEN,
    CRED_JIRA_API_TOKEN,
    CRED_KIRO_API_KEY,
)

# Per-host Jira tokens use a hex-encoded host suffix: JIRA_TOKEN_<HEX>.
# Only hex chars are valid — restricting the pattern prevents forged key names
# injected via multiline env values from reaching the eval-based value reader
# in the Docker entrypoint.
_JIRA_TOKEN_RE = _re.compile(r"^JIRA_TOKEN_[0-9A-Fa-f]+$")

# Keys from .env that were already warned about (fire once per gateway boot).
_warned_env_keys: set[str] = set()

DEFAULT_MODEL = "auto"
DEFAULT_SESSION_TIMEOUT = 3600  # 60 min
DEFAULT_MAX_PARALLEL_STEPS = (
    0  # 0 = auto: derive from agent.subagent_auto_max via compute_max_subagents
)


def normalize_agent_model(model: object) -> str:
    """Collapse an "inherit" model spelling to ``""``.

    ``""`` (never set) and ``DEFAULT_MODEL`` ("auto") both mean "do not pin a
    model here, defer to the next tier down". Callers store and compare the
    single ``""`` spelling so a tier set to "auto" keeps inheriting instead of
    hard-pinning the backend's own default and shadowing the tier below it.

    Total on purpose: this is the chokepoint for values that arrive from
    hand-edited config and from request bodies, so a non-string is treated as
    "no pin" rather than raising out of a resolver.
    """
    if not isinstance(model, str):
        return ""
    m = model.strip()
    return "" if m == DEFAULT_MODEL else m


# Per-task-class model overrides (agent.role_models). These are the ONLY
# sanctioned place to pin a model for a class of work — never hardcode a model
# id in code. Every role defaults to "" ("inherit"), which resolves down to
# agent.model and finally to DEFAULT_MODEL ("auto"), so an unpinned role is
# entitlement-safe on every subscription tier (the provider picks a served
# model). An operator who deliberately wants a cheaper model for background /
# sub-agent work pins it here without changing the interactive chat default.
ROLE_MODEL_KEYS: tuple[str, ...] = ("background", "subagent")


def coerce_role_models(raw: object) -> dict[str, str]:
    """Normalize the per-role model map from hand-edited config / request bodies.

    Only the known :data:`ROLE_MODEL_KEYS` are kept; each value passes through
    :func:`normalize_agent_model`, so an ``"auto"`` or non-string entry collapses
    to ``""`` ("inherit the next tier down"). Empty results are dropped so the
    stored map only ever carries real pins — a role absent from the map and a
    role explicitly set to ``"auto"`` behave identically (both inherit).
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for role in ROLE_MODEL_KEYS:
        val = normalize_agent_model(raw.get(role))
        if val:
            out[role] = val
    return out


def coerce_role_efforts(raw: object) -> dict[str, str]:
    """Normalize the per-role reasoning-effort map (agent.role_efforts).

    Same role keys as :data:`ROLE_MODEL_KEYS`. Each value must be a concrete,
    valid effort level; ``""`` / an invalid / non-string entry is dropped so the
    stored map carries only real pins — an absent role and an empty one both
    mean "inherit the chat default effort, then the provider/model default".
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for role in ROLE_MODEL_KEYS:
        val = raw.get(role)
        if isinstance(val, str) and val.strip() and is_valid_effort(val.strip()):
            out[role] = val.strip()
    return out


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


def _safe_int(value: object, default: int, lo: int | None = None, hi: int | None = None) -> int:
    """Convert a legacy numeric config value or return *default* on failure.

    Existing config files may contain numeric strings or integral floats from
    older writers. Preserve that compatibility while rejecting booleans.

    *lo*/*hi* clamp the result, mirroring :func:`_safe_float`. Pass them for any
    bounded knob: ``_clamp_security_bounds`` runs over the raw dict and skips
    non-int values, so a numeric STRING (``"1"``) slips past it and then
    coerces here — clamping at the coercion site is what actually enforces the
    declared range.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, float) and not value.is_integer():
        return default
    try:
        result = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        result = default
    if lo is not None:
        result = max(lo, result)
    if hi is not None:
        result = min(hi, result)
    return result


def _safe_nonnegative_int(value: object, default: int) -> int:
    """Convert a legacy integer value and reject negative results."""
    result = _safe_int(value, default)
    return result if result >= 0 else default


def _clamp_pct(value: int) -> int:
    """Clamp an integer context-threshold percentage to 1..100.

    The single statement of the range: the floor is 1, not 0, because a 0%
    threshold means "always over" and would fire the notice/compaction on
    every turn.
    """
    return max(1, min(100, value))


def _threshold_pct(raw: object, default: int) -> int:
    """Coerce a transport context-threshold percentage and clamp it to 1..100.

    The single coercion for every ``soft_threshold_pct`` / ``hard_threshold_pct``
    read, so a hand-edited config can never load an out-of-range threshold on
    any channel.
    """
    return _clamp_pct(_safe_int(raw, default))


def _normalize_threshold_pair(soft: int, hard: int) -> tuple[int, int]:
    """Normalize a soft/hard context-threshold pair to a valid ordering.

    Clamp both to the shared range and pull the soft threshold down to the
    hard one when it exceeds it, so a misconfig (e.g. hard=50, soft=95) can't
    make the soft nudge unreachable — the transports check ``pct >= hard``
    first.
    """
    soft = _clamp_pct(soft)
    hard = _clamp_pct(hard)
    if soft > hard:
        soft = hard
    return soft, hard


def _safe_bool(value: object, default: bool) -> bool:
    """Return *value* only when it is a real bool, else *default*."""
    return value if isinstance(value, bool) else default


def _safe_list(value: object) -> list:
    """Return *value* if it is a list, else []. Guards list()/comprehensions in
    config parse against a malformed (non-list) config value that would either
    crash (int/None) or silently mis-coerce (a string char-splits) — config
    load must degrade to the default, never raise."""
    return value if isinstance(value, list) else []


def _safe_dict(value: object) -> dict:
    """Return *value* if it is a dict, else {}. Guards .items()/dict() in config
    parse against a non-dict config value (which would raise AttributeError)."""
    return value if isinstance(value, dict) else {}


def _resolve_stub_servers(mcp_gateway_data: dict) -> list[str]:
    """Which MCP servers are given a stub.

    ``poolable_servers`` is the deprecated spelling and is consulted ONLY when
    ``stub_servers`` is absent from the file. Key presence, not truthiness, is
    the test: an operator who wrote ``stub_servers: []`` chose to stub nothing,
    and silently falling back to a stale ``poolable_servers`` would re-stub
    servers they had just cleared.

    The migration reproduces the stub set the operator was ALREADY RUNNING, which
    is why it is also conditional on ``enabled``. Before the stub became its own
    per-server decision, the broker was gated on ``enabled`` alone, so a config
    with ``enabled: false`` produced no broker, no overlay and no stub no matter
    what ``poolable_servers`` held. Migrating that list unconditionally would
    hand such an install a daemon and a stub process per server on upgrade —
    inventing the very topology change this design exists to make optional. An
    operator whose gateway was off keeps nothing running and opts in per server.
    """
    if "stub_servers" in mcp_gateway_data:
        source = mcp_gateway_data.get("stub_servers")
    elif _safe_bool(mcp_gateway_data.get("enabled", False), False):
        source = mcp_gateway_data.get("poolable_servers")
    else:
        source = None
    return [s for s in _safe_list(source) if isinstance(s, str) and s]


def _safe_float(
    value: object,
    default: float,
    lo: float | None = None,
    hi: float | None = None,
) -> float:
    """Return a real JSON number or *default*, clamped to [lo, hi].

    Non-finite results (NaN/Infinity) are replaced with *default* — NaN compares
    false against any bound so it would silently bypass clamping (e.g. a
    configured ``tips_cadence_hours: NaN`` would permanently suppress tips).
    """
    # Keep compatibility with config files written by older CLI versions while
    # excluding booleans, which Python otherwise treats as numeric values.
    if isinstance(value, bool):
        return default
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


def _write_migration_backup(path: Path) -> None:
    """Copy the pre-migration config aside, but ONLY inside our own data home.

    ``load()`` reads whatever ``config_path()`` resolves to, and callers can
    redirect that at a file they own — tests and embedders point it at a
    ``tempfile`` entry in the shared ``TMPDIR``. Writing ``<path>.bak`` beside
    such a path leaks a file nobody collects: the caller unlinks the path it
    created and never learns a sibling appeared. One dev host accumulated 72k
    orphaned ``tmpXXXXXXXX.json.bak`` files this way, 7% of a tmpfs inode
    budget whose exhaustion fails every process on the box.

    So the copy is gated on the config living in ``config_dir()``, the one
    directory whose contents we own. In production that is always true
    (``config_path()`` is ``config_dir() / "config.json"``), which keeps the
    real backup exactly where it has always been; for a redirected path we
    write nothing rather than litter a directory belonging to someone else.

    Only the LOCATION decision is contained here. A failing copy still
    propagates, because the caller's ``except`` is what skips the migration
    ``save()`` -- so a config we could not copy aside is not rewritten either,
    and the migration retries on the next load.
    """
    try:
        inside_data_home = path.parent.resolve() == config_dir().resolve()
    except OSError:
        # Containment is unprovable (symlink loop, vanished parent): treat the
        # path as foreign, since writing on a failed check is the worse error.
        inside_data_home = False
    if not inside_data_home:
        # info, not debug: the migration save that follows rewrites this
        # caller-owned file in place, and that now happens with no backup.
        logger.info("Config migrated; no backup written for %s (outside the data home)", path)
        return
    # NOT with_suffix(".json.bak"): that REPLACES the final suffix, so a
    # config path which is not *.json would be renamed rather than backed up.
    backup = Path(str(path) + ".bak")
    shutil.copy2(path, backup)
    logger.info("Config migrated — backup saved to %s", backup)


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


def computer_use_state_path() -> Path:
    """Return path to computer_use.json — the computer-use primary enable.

    Same KEYSTONE reasoning as :func:`denied_commands_path`, and the leaf is on
    ``security._CREW_SECRET_LEAVES`` for the same reason: enabling computer use
    grants full desktop observation plus input synthesis into the operator's real
    applications, which is a security ceiling, not a preference. Keeping it out
    of the agent-readable ``config.json`` is what makes it un-flippable by a
    prompt-injected agent — ``is_sensitive_path`` blocks the tool path and
    ``is_sensitive_bash_command`` blocks the shell forms (``cat``, ``>``,
    ``tee``, archive extraction into the trust root).

    Holds ``{enabled, allowed_apps, extra_denied_apps}``; every read fails soft
    to DISABLED (see ``computer_use.enable_state``). The only writer is the
    dashboard ``/api/computer-use/config`` PUT, which does not route through the
    agent tool gate. Respects ``KIROCREW_HOME``.

    Note the deliberate asymmetry with the ``computer_use`` section of
    ``config.json``: that section carries display/limit knobs ONLY and has no
    ``enabled`` field, precisely so there is exactly one place the feature can be
    turned on and it is not one the agent can reach.
    """
    return config_dir() / "computer_use.json"


def oauth_endpoints_path() -> Path:
    """Return path to oauth_endpoints.json — the operator OAuth-endpoint extension.

    Same KEYSTONE reasoning as :func:`denied_commands_path` and
    :func:`computer_use_state_path`, and the leaf is on
    ``security._CREW_SECRET_LEAVES`` for the same reason: each listed endpoint
    widens the banner-only OAuth entropy carve-out (``security.py``'s
    ``_OAUTH_AUTHORIZATION_ENDPOINTS``), so an agent that could write this file
    could exempt an attacker-controlled host from the exfiltration heuristics —
    it is a trust boundary, not a preference. ``is_sensitive_path`` blocks the
    tool path and ``is_sensitive_bash_command`` blocks the shell forms.

    Holds ``{"additional_authorization_endpoints": [{"host": …, "path": …}]}``;
    every read fails soft to an EMPTY extension set (see
    ``security._load_operator_oauth_endpoints``). There is no dashboard writer:
    the operator hand-edits the file out-of-band. Respects ``KIROCREW_HOME``.
    """
    return config_dir() / "oauth_endpoints.json"


def read_local_secret(port: int) -> str:
    """Read the internal-API credential for the gateway on *port*.

    Single home for the secret read that callers (cron scripts, MCP tool bridges,
    CLI) need to authenticate to the gateway's internal API. Returns empty string
    when no credential can be read.

    Resolution is per LISTENER first: ``run/gateway-<port>.secret``, then the
    shared ``.local_secret``. That order is the invariant, and it lives here rather
    than in each reader because the credential identifies ONE gateway generation
    while the shared file has one slot per data home, last-writer-wins. A caller
    that reads the shared file while a different generation owns the port it dials
    gets 403 on every internal call.

    *port* is REQUIRED, and deliberately so: the credential is a function of the
    dial target, so inferring the target here would let a caller dial one gateway
    while authenticating for another -- the exact desync this helper exists to
    close, reintroduced one call site at a time and invisible at the call site. A
    caller with no port must resolve one explicitly and pass it, where the choice
    is reviewable.
    """
    # Function-local: port_resolution imports this module, so a module-level
    # import would be circular.
    from kiro_crew.instances import run_marker

    try:
        per_port = run_marker.read_secret(int(port))
    except Exception:
        per_port = ""
    if per_port:
        return per_port
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


class ConfigReadError(Exception):
    """``config.json`` exists but could not be read as a config object.

    Raised only by :func:`read_config_for_update`, whose callers are about to
    write the value back. It deliberately does NOT inherit from ``OSError`` or
    ``ValueError`` so an existing broad ``except OSError`` around a write cannot
    swallow it and resume the clobbering path.
    """


def read_config_for_update(path: Path | None = None) -> dict:
    """Read ``config.json`` for a read-modify-write, failing CLOSED.

    Every partial config update (flip one toggle, persist one channel) has to
    read the whole file, mutate one key, and write it all back. The obvious
    ``try: json.loads(...) except Exception: data = {}`` is a **data-loss bug**
    in that shape: the fallback is indistinguishable from "the user has no
    settings", so the write-back replaces a fully populated config with a
    single-key one. Every setting the user ever chose is gone, silently, and
    the endpoint still reports success.

    The read fails for mundane reasons — most commonly a *torn read*: several
    config writers still truncate-then-write, so a concurrent reader can
    observe a half-written file. That window is small, which is exactly what
    makes the resulting loss so hard to reproduce and report.

    So: an **absent** file returns ``{}`` (a genuine empty starting point), and
    an unreadable or non-object file raises :class:`ConfigReadError`. Callers
    must let that abort the update — leaving the existing file untouched is
    always better than overwriting it with defaults.

    Pair this with :func:`kiro_crew.atomic_write.atomic_write` on the way out so
    the write cannot create the torn window for the next reader.
    """
    p = path if path is not None else config_path()
    try:
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        # UnicodeDecodeError is a ValueError, NOT an OSError, so it needs naming
        # explicitly: a config containing invalid UTF-8 (a truncated multi-byte
        # sequence from a torn write, or a mojibake'd hand edit) would otherwise
        # escape this controlled path and crash the caller instead of returning
        # the clean "config unreadable" refusal.
        raise ConfigReadError(f"could not read config at {p}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigReadError(f"config at {p} is not a JSON object (got {type(raw).__name__})")
    return raw


def write_config_atomically(path: Path, data: dict, *, fsync: bool = False) -> None:
    """Write a config dict to *path* atomically, PRESERVING its permissions.

    The companion to :func:`read_config_for_update`. Two properties matter:

    * **Atomic** (tmp+rename) so a concurrent reader can never observe a
      half-written file. A truncate-then-write leaves a window in which a reader
      sees invalid JSON; a reader that mistakes that for "no settings" will write
      the emptiness back and destroy the user's config.
    * **Mode-preserving.** Because tmp+rename creates a NEW inode, the umask
      default (typically ``0644``) would silently replace an operator's tightened
      ``0600``. ``config.json`` can hold inline credentials, so a settings write
      must never widen who can read it. An existing file's mode is carried over;
      a newly created one defaults to owner-only.

    ``atomic_write``'s ``mode`` routes through ``fchmod_safe``, which applies the
    mode on POSIX and is a documented no-op on Windows.

    **This deliberately does NOT call ``platform_compat.restrict_to_owner``.**
    That helper shells out to ``icacls`` on Windows (``subprocess.run``, 10s
    timeout), and this function is called from ``async`` request handlers and from
    ``KiroCrewConfig.save()`` — so invoking it here would put a blocking subprocess
    on the gateway's asyncio event loop, freezing every task including the liveness
    heartbeat (the ``no-blocking-call-on-event-loop`` rule; the repo offloads that
    helper via ``asyncio.to_thread`` everywhere else for exactly this reason).
    Omitting it is no worse than the truncate-then-write this replaced, which
    applied no DACL either, while ``mode`` still tightens the POSIX case and new
    files are created ``0600``. A caller that needs a hard owner-only guarantee on
    Windows must offload ``restrict_to_owner`` itself, off the loop.

    **Symlinks are followed, not replaced.** ``os.replace`` renames over the link
    itself, turning a symlinked ``config.json`` into a regular file and orphaning
    its target — whereas the ``write_text`` this replaced followed the link and
    updated the target. Symlinking the config into a dotfiles repo is a normal
    setup, so the target is resolved first to preserve that behavior.
    """
    # Resolve BEFORE stat/write so a symlinked config keeps pointing at its
    # target (and the mode preserved is the target's, not the link's).
    try:
        if path.is_symlink():
            path = path.resolve()
    except OSError:
        pass
    try:
        mode = _stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    except OSError:
        mode = 0o600
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(data, indent=2) + "\n", fsync=fsync, mode=mode)


def update_config_locked(
    path: Path | None = None,
    *,
    mutate: Callable[[dict], dict | None],
    fsync: bool = False,
    stamp_meta: bool = True,
    on_corrupt: Literal["fail", "reset"] = "fail",
) -> dict:
    """Perform an atomic read-modify-write of a config file under an advisory lock.

    The locked primitive for the converted config.json writers and the required
    path for new config.json mutations.  Legacy writers that pre-date this
    function (dashboard agents endpoint, updates.py, memory.py, security.py,
    messaging.py, mcp.py, core.py STT) still use
    :func:`write_config_atomically` directly and rely on the in-process asyncio
    ``_get_config_lock()`` only.

    Contract:

    * **Isolation.** An advisory file lock is held for the entire
      read-modify-write, so two concurrent callers are serialized: neither can
      land between the other's read and write.
    * **Sidecar lockfile.** The lock lives on ``<path>.lock``, NOT on the
      config file's own fd.  ``write_config_atomically`` replaces the inode
      (tmp + rename), so a lock taken on the config file's fd would not
      serialize against the rename — a second opener after the rename gets a
      NEW fd on the NEW inode and takes the lock instantly, defeating the
      purpose.
    * **Fail-closed read (default).** :func:`read_config_for_update` is used
      inside the critical section; with ``on_corrupt="fail"`` (the default), an
      unreadable or malformed config raises :class:`ConfigReadError`, aborts
      the update, and the lockfile is released.  The existing file is never
      overwritten with defaults.
    * **Reset-on-corrupt (opt-in).** With ``on_corrupt="reset"``, a
      :class:`ConfigReadError` inside the critical section is caught WHILE THE
      LOCK IS STILL HELD and the *mutate* callback is invoked with ``{}``.
      The caller's write therefore happens in the same lock hold as the read
      attempt, closing any window for a concurrent writer to land between.
      The resulting file is written with mode ``0o600`` (no existing mode to
      preserve from a corrupt file).
    * **Mode-preserving write.** :func:`write_config_atomically` preserves the
      existing file's permission bits, so a tightened ``0600`` is not widened.
    * **Cross-platform.** Locking goes through
      :func:`platform_compat.file_lock`, which uses ``fcntl.flock`` on POSIX
      and a bounded ``msvcrt.locking`` spin on Windows.
    * **Symlink-safe.** The target path is resolved before locking, so a
      symlinked config is updated in place (matching
      ``write_config_atomically``'s behavior).

    Parameters
    ----------
    path : Path | None
        Config file path; defaults to :func:`config_path`.
    mutate : (dict) -> dict | None
        Called with the current config data (possibly ``{}`` for a new file).
        Must return the updated dict to write, or ``None`` to skip the write
        (useful when the mutate discovers no change is needed).
    fsync : bool
        Passed through to :func:`write_config_atomically`.
    stamp_meta : bool
        If True (default), stamps the ``meta`` block via
        :func:`stamp_config_meta` before writing.
    on_corrupt : "fail" | "reset"
        Behavior when :func:`read_config_for_update` raises
        :class:`ConfigReadError`.  ``"fail"`` (default) re-raises, aborting the
        update.  ``"reset"`` catches the error inside the lock hold and invokes
        *mutate* with ``{}``; the caller's write proceeds in the same critical
        section so no concurrent writer can land between.

    Returns
    -------
    dict
        The final config dict (after mutation), whether or not a write occurred.

    Raises
    ------
    ConfigReadError
        If the existing config is unreadable or malformed and
        ``on_corrupt="fail"``.
    OSError
        If the lockfile cannot be opened/created or the lock cannot be acquired.
    """
    p = path if path is not None else config_path()
    # Resolve symlinks before locking (same logic as write_config_atomically)
    # so the sidecar sits beside the ACTUAL file, not the symlink.
    try:
        if p.is_symlink():
            p = p.resolve()
    except OSError:
        pass
    lock_path = p.parent / (p.name + ".lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with platform_compat.file_lock(fd, exclusive=True):
            try:
                data = read_config_for_update(p)
            except ConfigReadError:
                if on_corrupt == "fail":
                    raise
                # on_corrupt="reset": treat as empty inside the same lock hold.
                data = {}
            result = mutate(data)
            if result is None:
                return data
            if stamp_meta:
                result = stamp_config_meta(result)
            write_config_atomically(p, result, fsync=fsync)
            return result
    finally:
        os.close(fd)


def stamp_config_meta(data: dict) -> dict:
    """Return *data* with a freshly stamped ``meta`` block in front.

    ``meta.lastTouchedVersion`` names the build that wrote the bytes now on
    disk, which is the first thing to check when a ``config.json`` looks like
    it came from an older schema. An existing stamp is therefore replaced
    rather than merged.

    Every writer that rebuilds the whole file from a dataclass round-trip has
    to stamp through here: ``to_dict()`` models only the schema, so such a
    write drops any top-level key the dataclass does not carry — ``meta``
    among them. Writers that mutate the raw dict they read keep the block
    without help.

    Only ``config.json`` carries the block. ``config.local.json``, agent
    specs, and the other JSON that shares :func:`write_config_atomically` do
    not, so the stamping is deliberately separate from that function.
    """
    return {
        "meta": {
            "lastTouchedVersion": __version__,
            "lastTouchedAt": datetime.now(timezone.utc).isoformat(),
        },
        **{k: v for k, v in data.items() if k != "meta"},
    }


def refresh_config_meta_stamp() -> bool:
    """Re-stamp ``config.json``'s ``meta`` block when it names another build.

    The stamp is only ever written as a side effect of a config write, so an
    upgrade that never touches ``config.json`` leaves ``lastTouchedVersion``
    naming the *previous* build indefinitely. That contradicts the field's
    documented meaning ("the build that wrote the bytes now on disk") and
    sends anyone debugging a version question chasing a build that is no
    longer installed (#3102). Called once per gateway start, off the boot
    path: a version check on one small file, a rewrite only when it differs.

    Deliberately a plain field refresh, not a migration hook: the stamp is
    replaced, every other key is preserved, and nothing else changes. When
    the stored version already matches, the file is not rewritten at all
    (no mtime churn, no ``lastTouchedAt`` bump).

    The read-modify-write goes through :func:`update_config_locked` — the
    required path for new ``config.json`` mutations — so the refresh holds
    the sidecar advisory lock and can never revert a concurrent settings
    write with its own earlier snapshot. Callers that run while the
    dashboard serves requests must ALSO hold the in-process asyncio config
    lock (``_get_config_lock``) around the call, because the legacy writers
    serialize on that lock alone.

    Best-effort by design — a stale stamp is a diagnostic blemish, never
    worth failing a boot over. Returns ``True`` when a refresh was written,
    ``False`` when nothing needed doing (absent/empty file, current stamp)
    or the file could not be safely read (an unreadable/torn config must
    never be replaced with a stamped-but-empty one).
    """
    path = config_path()
    if not path.exists():
        return False

    wrote = False

    def _stamp_if_stale(data: dict) -> dict | None:
        nonlocal wrote
        if not data:
            # Absent or emptied between the exists() check and the lock hold:
            # there is nothing to refresh, and writing would CREATE a config
            # holding only a meta block.
            return None
        meta = data.get("meta")
        stored = meta.get("lastTouchedVersion") if isinstance(meta, dict) else None
        if stored == __version__:
            return None  # current: skip the write entirely
        wrote = True
        return data  # update_config_locked stamps the meta block itself

    try:
        update_config_locked(path, mutate=_stamp_if_stale)
    except ConfigReadError:
        logger.debug(
            "config meta stamp refresh skipped: %s unreadable; leaving it untouched",
            path,
            exc_info=True,
        )
        return False
    except OSError:
        logger.debug(
            "config meta stamp refresh failed: could not lock or write %s",
            path,
            exc_info=True,
        )
        return False
    if wrote:
        _invalidate_config_cache()
    return wrote


def workspace_dir_for(workspace: str | None = None) -> Path:
    """Resolve a named workspace to its directory path.

    Reads the ``dir`` field from ``WorkspaceConfig`` objects (new structured
    format) or falls back to raw string values (legacy flat format).

    Values starting with ``/`` or ``~`` are treated as absolute paths.
    Otherwise the value is relative to ``config_dir()`` (``~/.kiro/crew/``).
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


def read_env_file_credential(key: str, env_file: Path | None = None) -> str:
    """Best-effort read of one ``KEY=VALUE`` entry from the data home's ``.env``.

    Same line format :meth:`KiroCrewConfig.load_credentials` parses (one pair
    per line, ``#`` comments, no quotes required, last occurrence wins).
    Returns ``""`` when the file is absent or unreadable — callers treat the
    credential as unset rather than failing.

    Blocking file IO: call via ``asyncio.to_thread`` from async paths.
    """
    ep = env_file if env_file is not None else env_path()
    try:
        text = ep.read_text()
    except OSError:
        return ""
    value = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip() == key:
                value = v.strip()
    return value


def inject_kiro_cli_api_key(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Ensure *env* carries kiro-cli's own model credential (``KIRO_API_KEY``).

    The Docker entrypoint scrubs :data:`_CREDENTIAL_KEYS` out of the gateway's
    process environment into the data home's ``.env`` (mode 600) so they never
    reside in a long-lived ``/proc/<pid>/environ``. Every other credential is
    consumed in-process from :meth:`KiroCrewConfig.load_credentials`, but this
    one authenticates the kiro-cli CHILD, which reads it from its own
    environment — so kiro-cli spawn paths call this to hand the child exactly
    the one variable it owns, without re-widening the parent's environ. A value
    already present in *env* wins (same precedence as ``load_credentials``);
    outside Docker nothing changes because the variable is still inherited.

    Mutates *env* in place and returns it for convenience. Blocking file IO:
    call via ``asyncio.to_thread`` from async paths.
    """
    if not env.get(CRED_KIRO_API_KEY):
        val = read_env_file_credential(CRED_KIRO_API_KEY)
        if val:
            env[CRED_KIRO_API_KEY] = val
    return env


def strip_kiro_cli_api_key(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Remove kiro-cli's model credential from a foreign child's environment.

    Counterpart to :func:`inject_kiro_cli_api_key` for the non-kiro-cli ACP
    backends (the dormant Claude seam, KAS): the credential is kiro-cli's
    alone, and it is deliberately NOT in ``sandbox._AGENT_DENIED_ENV_KEYS``, so
    without this an inherited copy in the raw ``os.environ`` snapshot would
    ride into a foreign agent process. Matches the platform env-key convention
    (exact on POSIX, case-folded on Windows) so a differently-cased Windows
    spelling cannot slip past. Mutates *env* in place and returns it.
    """
    matched = [
        k for k in env if platform_compat.env_key_allowed(k, _KIRO_API_KEY_ONLY)
    ]
    for k in matched:
        del env[k]
    return env


# Single-key allowlist for strip_kiro_cli_api_key's platform-aware matching.
_KIRO_API_KEY_ONLY = frozenset({CRED_KIRO_API_KEY})


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

# Default endpoint for the anonymous usage beacon (see kiro_crew/beacon.py).
# Lives here with the other config defaults so beacon.py adds no import edge
# into the config package. Setting the field to "" disables the beacon outright.
_DEFAULT_BEACON_ENDPOINT = "https://d175o3ylxqum0e.cloudfront.net"


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

# Standard work-tree roots for ``agent.subagent_cwd_allowed_roots``.  Single
# source of truth shared by the field default and the fallback in ``from_dict``.
# Both use the same four roots.  The fallback is the value real configs get:
# ``from_dict`` always passes an explicit value and an absent key reaches the
# same branch as a malformed one.  Four is what the product ships; narrowing to
# two would revoke ~/workspaces and ~/workplaces from every config that omits
# the field.
DEFAULT_CWD_ALLOWED_ROOTS = [
    "~/workspace",
    "~/workspaces",
    "~/workplace",
    "~/workplaces",
]


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
    role_models: dict[str, str] = field(
        default_factory=dict,
        metadata=_meta(
            "Per-role models",
            "Optional per-task-class model overrides. Keys: 'background' "
            "(lite / heartbeat background workers) and 'subagent' (spawned "
            "sub-agents). An empty value or 'auto' defers to the chat default "
            "(agent.model) and then to the provider default, so an unpinned "
            "role stays usable on every subscription tier. Pin a cheaper model "
            "here to run background / sub-agent work on it without changing the "
            "interactive chat default.",
        ),
    )
    role_efforts: dict[str, str] = field(
        default_factory=dict,
        metadata=_meta(
            "Per-role reasoning effort",
            "Optional per-task-class reasoning effort, paired with role_models "
            "(keys: 'background', 'subagent'). Empty for a role inherits the chat "
            "default (agent.reasoning_effort) and then the provider/model default. "
            "Only applies on reasoning-capable models.",
        ),
    )
    reasoning_effort: str = field(
        default="",
        metadata=_meta(
            "Reasoning Effort",
            "Default reasoning effort for new sessions on models that support it. "
            "Empty defers to the provider/model default. Per-session overrides win.",
            enum=["", *EFFORT_LEVELS],
        ),
    )
    provider: str = field(
        default="acp",
        metadata=_meta("Provider", "LLM provider backend (KiroACP / kiro-cli).", enum=["acp"]),
    )
    mcp_registry_mode: bool = field(
        default=False,
        metadata=_meta(
            "Enterprise MCP Registry Mode",
            "Set true when this Kiro account is governed by an enterprise MCP "
            "registry (Kiro console -> Shared settings -> MCP Registry URL, which "
            "applies to IAM Identity Center and API-key sign-ins). In registry "
            "mode the client connects ONLY to mcpServers entries carrying "
            "'type': \"registry\" that resolve to a catalog entry of the same "
            "name, so Kiro Crew stamps that marker on the servers it manages. "
            "Leave false on a personal account: with no registry configured the "
            "filter inverts and registry-marked entries are the ones dropped. "
            "The administrator must also allow-list kirocrew-core, kirocrew-cron "
            "and kirocrew-computer in the registry by those exact names.",
        ),
    )
    acp_backend: str = field(
        default="",
        metadata=_meta(
            "ACP Backend",
            "Which ACP agent to drive: '' = kiro-cli (default), 'kas' = kiro-agent. "
            "KAS runs chat but has no native subagent progress reporting yet.",
            enum=["", "kas"],
        ),
    )
    default_agent: str = field(
        default="",
        metadata=_meta("Default Agent", "Default agent name for new sessions."),
    )
    sandbox: str = field(
        default="auto",
        metadata=_meta(
            "Sandbox",
            "Sandbox mode for ACP provider. Default 'auto' engages OS-level "
            "isolation (namespace on Linux, sandbox-exec on macOS) and "
            "automatically defers to kiro-cli's internal sandbox on macOS when "
            "it is enabled (kiro-cli >= 2.13; nested seatbelt causes EPERM). "
            "Set to 'off' to skip Kiro Crew's own OS-level sandbox — delegation "
            "to kiro-cli's internal sandbox still fires on macOS if it is "
            "enabled, and a SECURITY warning is logged when neither layer is "
            "active.",
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
            "severity — this field controls whether execution proceeds at all. "
            "The default is platform-independent: on a host with no backend (any "
            "Windows host, a Linux kernel refusing user namespaces) `kirocrew "
            "setup` OFFERS this opt-in interactively and writes it only on an "
            "explicit yes, so unconfined execution stays operator-declared and is "
            "never enabled implicitly by the platform.",
        ),
    )
    apps_allow_third_party: bool = field(
        default=False,
        metadata=_meta(
            "Allow Third-Party Apps",
            "Explicitly allow executable code from third-party (non-builtin) apps. "
            "Defaults to false. Only the JSON boolean true admits in-process Python "
            "hooks, backend processes, lifecycle/install scripts, and openCommand. "
            "App code can access the filesystem, network, and in-memory credentials; "
            "enable this only for apps you trust (CSE SEC-012). Prefer "
            "apps_trusted, which grants the same admission to ONE named app.",
        ),
    )
    apps_trusted: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Trusted Apps",
            "Per-app grants for third-party execution — the narrow form of "
            "apps_allow_third_party. An app whose manifest name appears here is "
            "admitted to run Python hooks, its backend, lifecycle scripts, and "
            "openCommand; every other third-party app stays blocked. Only a JSON "
            "array of app-name strings is honoured, and no wildcard entry is "
            "accepted (use apps_allow_third_party to trust all).",
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
    dangerously_skip_permissions: bool = field(
        default=False,
        metadata=_meta(
            "Dangerously Skip Permissions",
            "Skip EVERY tool approval confirmation, permanently. Declaring it here "
            "is a standing instruction: the grant does not expire and is "
            "re-established on every startup. This is the advanced, "
            "config-file-only escape hatch — there is deliberately no dashboard "
            "toggle for it. An enterprise policy can forbid it, which falls back "
            "to the ad-hoc duration below.",
        ),
    )
    yolo_duration: str = field(
        default="6h",
        metadata=_meta(
            "Ad-hoc Auto-approve Duration",
            "How long auto-approve (YOLO) lasts when it is enabled AD HOC — from "
            "the dashboard picker, Slack, or the API. Every one of those surfaces "
            "uses this same duration. Accepts 30m / 1h / 6h / 12h / 24h, or "
            "until_shutdown to keep it on with no timed expiry until Kiro Crew "
            "restarts. Timed values are capped at 24h. Does NOT apply to a grant "
            "declared via 'dangerously_skip_permissions' above, which persists.",
            enum=["30m", "1h", "6h", "12h", "24h", "until_shutdown"],
        ),
    )
    notify_override_expiry: bool = field(
        default=True,
        metadata=_meta(
            "Notify on Override Expiry",
            "DM the Slack owner when a time-limited safety override (YOLO) expires. "
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
            "when many MCP servers are configured. kiro-cli backend only. "
            "Deferral only starts once the specs cross tool_search_min_pct or "
            "tool_search_min_tokens; disabling reverts to sending full tool "
            "specs. No effect on an alternate ACP backend.",
        ),
    )
    tool_search_min_pct: int = field(
        default=5,
        metadata=_meta(
            "Tool Search threshold (% of context)",
            "Start deferring MCP tool specs once they exceed this percentage of "
            "the context window. Paired with tool_search_min_tokens — whichever "
            "is crossed first wins. Below both thresholds every spec is sent "
            "directly, so the agent never pays a tool_search round-trip for a "
            "small tool set. 0 with tool_search_min_tokens 0 defers always. "
            "Clamped to 0-100; matches the kiro-cli default.",
        ),
    )
    tool_search_min_tokens: int = field(
        default=50000,
        metadata=_meta(
            "Tool Search threshold (tokens)",
            "Start deferring MCP tool specs once they exceed this many tokens. "
            "Paired with tool_search_min_pct — whichever is crossed first wins. "
            "0 with tool_search_min_pct 0 defers always. Matches the kiro-cli "
            "default.",
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
    max_stop_hook_nudges: int = field(
        default=100,
        metadata=_meta(
            "Max Stop-hook nudges",
            "Maximum consecutive Stop-hook block continuations before the run "
            "halts and surfaces a halt card instead of dispatching another turn. "
            "Bounds a buggy always-block hook in an unattended session. 0 = "
            "uncapped (opt-in for genuinely unbounded feedback loops).",
        ),
    )
    spawn_min_memory_gb: float = field(
        default=4.0,
        metadata=_meta(
            "Spawn Min Memory GB",
            "Minimum available memory (GB) required to spawn a subagent. 0 disables the check.",
        ),
    )
    resource_pressure_gb: float = field(
        default=4.0,
        metadata=_meta(
            "Resource Pressure Threshold (GB)",
            "Available memory (GB) at or below which the agent is told host memory "
            "is 'tight' via a compact [RESOURCES] context line, so it can prefer "
            "the lighter path for heavy work (targeted tests, smaller sub-agent "
            "waves). Advisory only — not enforced. 0 disables the context line. "
            "Lower this on small-memory hosts / memory-limited containers (e.g. a "
            "2-4 GB pod) so the advisory only fires under genuine pressure.",
        ),
    )
    resource_critical_gb: float = field(
        default=2.0,
        metadata=_meta(
            "Resource Critical Threshold (GB)",
            "Available memory (GB) at or below which the [RESOURCES] context line "
            "escalates to 'critically low' and advises against starting heavy work "
            "at all. Should be <= resource_pressure_gb. 0 disables the critical tier.",
        ),
    )
    admission_gate: bool = field(
        default=True,
        metadata=_meta(
            "Posture Admission Gate",
            "While available memory is at or below resource_critical_gb, defer "
            "scheduled cron firings to the next tick and refuse new subagent "
            "spawns until memory frees. Manually triggered cron runs, in-flight "
            "subagents, and direct chat turns are never gated; an unreadable "
            "probe admits (fail-open). Set false to make the critical posture "
            "advisory-only.",
        ),
    )
    workflow_run_timeout_secs: int = field(
        default=3600,
        metadata=_meta(
            "Workflow Run Timeout (secs)",
            "Wall-clock ceiling for one dynamic-workflow run. This is a runaway "
            "backstop, so it is clamped to 60s..21600s (6h) — raise it for long "
            "multi-phase investigations, but it can never be disabled. Reaching "
            "the ceiling is no longer a data-loss event: every agent result "
            "completed before the cutoff is preserved on the run record.",
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
    chat_turn_timeout_secs: int = field(
        default=7200,
        metadata=_meta(
            "Chat Turn Timeout (secs)",
            "Wall-clock ceiling for one chat turn. This is a runaway backstop, "
            "so it is clamped to 300s..86400s (24h) and can never be disabled. "
            "Raise it above the 2h default for long unattended turns (full test "
            "suites, long builds); the ACP transport's prompt wait follows it. "
            "Hitting the ceiling is visible: the turn ends with a card naming "
            "the limit. For work spanning days, prefer monitor/goal loops — "
            "they end the turn between cycles and survive restarts.",
        ),
    )
    session_start_timeout_secs: int = field(
        default=90,
        metadata=_meta(
            "Session Start Timeout (secs)",
            "Budget for ACP session/new and session/load on the shared "
            "runtime. kiro-cli blocks the response while it initializes the "
            "agent's MCP servers, so session start scales with server count "
            "and per-server cold-start cost (sandboxed launchers, remote "
            "servers, loaded hosts). Raise this when a large agent "
            "legitimately needs longer than the 90s default. The floor is "
            "the default itself: the budget must stay comfortably above the "
            "backend's 30s OAuth authorization wait, so values below 90 are "
            "clamped up.",
        ),
    )
    tool_approval_timeout_secs: int = field(
        default=600,
        metadata=_meta(
            "Tool Approval Timeout (secs)",
            "How long a chat turn waits for a human to answer a tool-approval "
            "prompt before declining it and telling the user to resend. Kept "
            "well below the chat-turn ceiling on purpose: a window at or above "
            "it can never fire, so an unattended turn burns the whole ceiling "
            "and is then misreported as a turn timeout. Clamped to 30s..7200s, "
            "and additionally to 60s below the turn ceiling at load time.",
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
        default_factory=lambda: list(DEFAULT_CWD_ALLOWED_ROOTS),
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
        # Keep only known role keys, each normalized ("auto"/non-str -> "").
        # Defensive for directly-constructed instances; the load() path already
        # feeds coerced input.
        self.role_models = coerce_role_models(self.role_models)
        self.role_efforts = coerce_role_efforts(self.role_efforts)

    def resolve_model(self, role: str) -> str:
        """Effective model id for a task ``role`` — INDEPENDENT of the chat model.

        Returns the role's own pin (``role_models[role]``) or :data:`DEFAULT_MODEL`
        (``"auto"``). It deliberately does NOT inherit ``agent.model``: background
        workers (lite / heartbeat) run unattended, so riding the interactive chat
        flagship on every cycle would be a silent cost regression. ``"auto"`` lets
        the provider pick a served model, entitlement-safe on every tier. Callers
        that write a kiro agent spec / cc_model store this verbatim.
        """
        return normalize_agent_model(self.role_models.get(role, "")) or DEFAULT_MODEL

    def resolve_effort(self, role: str) -> str:
        """Effective reasoning effort for a task ``role`` — INDEPENDENT of the chat
        default.

        Returns ``role_efforts[role]`` or ``""`` (the provider/model default). It
        does not inherit ``agent.reasoning_effort``, for the same reason
        :meth:`resolve_model` does not inherit ``agent.model``. Effort only takes
        effect on reasoning-capable models; on others it is ignored downstream.
        """
        return self.role_efforts.get(role, "")


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
    eager_spawn: bool = field(
        default=True,
        metadata=_meta(
            "Eager Session Spawn",
            "Speculatively create a chat slot's session when the slot is created, "
            "its agent is switched, or its project directory changes, instead of "
            "on first message. Hides the multi-second session handshake behind "
            "user think-time.",
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
            "monolith. Default ON in Kiro Crew (the transport abstraction is the canonical "
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
    embedding_threads: int = field(
        default=4,
        metadata=_meta(
            "Embedding Threads",
            "CPU threads llama.cpp may use per embedding call. Left unset, llama.cpp "
            "sizes its batch pool from the host core count, so even a few-token embed "
            "fans out across every core and competes with the rest of the gateway. "
            "Embedding a short query does not need many threads; raise this only if "
            "bulk re-embedding throughput matters more than interactive latency. "
            "Clamped to the machine's core count.",
        ),
    )
    embed_model_url: str = field(
        default="",
        metadata=_meta(
            "Embedding Model URL",
            "Override HTTPS URL for the embedding model GGUF download (mirrored/airgapped "
            "deployments). Empty uses the public Kiro Crew CDN default; the "
            "KIROCREW_EMBED_MODEL_URL env var wins over both. The download is "
            "sha256-verified regardless of source.",
        ),
    )
    embed_model_path: str = field(
        default="",
        metadata=_meta(
            "Embedding Model Path",
            "Absolute path to a local GGUF embedding model to use INSTEAD of the bundled "
            "Qwen3-Embedding-0.6B. When set, the default model is never downloaded or "
            "installed, so a custom model survives a default-model version change. Set "
            "embedding_dim to the model's output width. Changing the model changes the "
            "vector space, so stored embeddings are regenerated automatically. The "
            "KIROCREW_EMBED_MODEL_PATH env var wins over this.",
        ),
    )
    embed_model_id: str = field(
        default="",
        metadata=_meta(
            "Embedding Model ID",
            "Optional stable identifier for a custom model's vector space. Defaults to "
            "'custom:<filename>:<size>', which changes when a different model file is "
            "used. Set this explicitly if you swap between models of identical byte size, "
            "which the default derivation cannot distinguish.",
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
        default=False,
        metadata=_meta(
            "Auto-Ingest Artifacts",
            "Automatically ingest content-bearing local artifacts (markdown/text "
            "documents you save and iterate) into the Knowledge Library so they "
            "become searchable, keep them in sync as the artifact changes, and "
            "remove them from the Library when the artifact is deleted. They "
            "appear as a single aggregate 'Artifacts' source. Off by default: "
            "every ingested chunk costs an LLM extraction call, so a library "
            "grows and spends only once you ask for it.",
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
    auto_add_documents: bool = field(
        default=False,
        metadata=_meta(
            "Auto-Add Documents",
            "Let the agent add documents it comes across during normal work to the "
            "Knowledge Library, so they become searchable later. The agent reads the "
            "document with its own tools, under your approval, and hands over the "
            "text -- Kiro Crew fetches nothing itself, so the doc-ingest host "
            "allowlist below does not apply. Added documents appear in a single "
            "aggregate 'Auto-added' source you can remove in one click. Off by "
            "default: the Library should only hold what you asked it to hold. "
            "Renamed from auto_ingest_doc_links, which is still accepted.",
        ),
    )
    auto_register_project_docs: bool = field(
        default=False,
        metadata=_meta(
            "Auto-Register Project Documents",
            "Register the documents of each project you work in as a Knowledge "
            "source automatically, so a project's design docs, specs and READMEs "
            "become searchable without adding the folder by hand. Only documents "
            "are taken (.md/.pdf/.docx/.org above a small size floor, excluding "
            "agent instructions, generated files and repository boilerplate) -- "
            "never source code. There is no confirmation step once enabled: the "
            "document filter and the per-sweep chunk budget below bound the cost, "
            "and deleting the source keeps it deleted. Off by default, because "
            "registering a repository is a decision to spend extraction calls on "
            "it -- turning this on opts in every project you open.",
        ),
    )
    auto_ingest_chunk_budget: int = field(
        default=150,
        metadata=_meta(
            "Auto-Ingest Chunk Budget",
            "Chunks an automatically-registered source may ingest per watcher "
            "sweep. Each chunk costs one LLM extraction call, so this is what "
            "actually bounds the cost of auto-registration -- file filters bound "
            "pollution, not spend. Newest documents land first and the rest "
            "trickle in on later sweeps, so a new project never arrives as a "
            "burst. 0 removes the bound.",
        ),
    )
    folder_ingest_chunk_budget: int = field(
        default=300,
        metadata=_meta(
            "Folder Ingest Chunk Budget",
            "Chunks a folder you add by hand may ingest per watcher sweep. Adding "
            "a source-code repository discovers thousands of files, and each "
            "chunk costs an LLM extraction call on a pool of billed sessions, so "
            "an unpaced first scan can spend a large amount unattended. Nothing "
            "is skipped: newest files land first and the rest continue on later "
            "sweeps. Higher than the auto-ingest budget because you asked for the "
            "folder explicitly. 0 removes the bound; a per-source chunk_budget "
            "property overrides it for one folder.",
        ),
    )
    dedup_every_n_sweeps: int = field(
        default=12,
        metadata=_meta(
            "De-duplicate Every N Sweeps",
            "Run a full duplicate-collapsing pass every Nth watcher sweep. The "
            "per-write gate refuses a byte-identical document, but only a full "
            "pass catches a near-duplicate (the same document edited slightly "
            "between two sources) or duplicates that already existed. At the "
            "default 300s sweep interval, 12 is roughly hourly. 0 disables it.",
        ),
    )
    doc_ingest_hosts: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Doc-Ingest Host Allowlist",
            "Exact hostnames whose links may be fetched by KIROCREW ITSELF and "
            "ingested, for an edition that wires a server-side doc-link scanner. "
            "Empty = fetch nothing (SSRF-safe deny-by-default). This governs only "
            "that server-fetch path -- it does NOT gate 'Auto-Add Documents' "
            "above, where the agent has already fetched the content under its own "
            "approval and Kiro Crew fetches nothing. Applying it there would make "
            "the feature ingest nothing on a default config while its toggle "
            "reads on.",
        ),
    )
    sweep_chunk_budget: int = field(
        default=500,
        metadata=_meta(
            "Global Sweep Chunk Budget",
            "Maximum chunks ingested across ALL sources in a single watcher "
            "sweep. Each chunk costs one LLM extraction call, so this is the "
            "primary global cost control. Once reached, remaining sources are "
            "deferred to the next sweep. "
            "0 removes the bound.",
        ),
    )
    max_sources: int = field(
        default=50,
        metadata=_meta(
            "Max Sources",
            "Maximum number of Knowledge sources that may be registered. "
            "Prevents unbounded auto-discovery from registering hundreds of "
            "sources when many projects are open. Registration attempts past "
            "the cap are skipped (auto) or rejected (manual). 0 removes the "
            "bound.",
        ),
    )
    embed_rate_limit: int = field(
        default=120,
        metadata=_meta(
            "Embedding Rate Limit (items/min)",
            "Maximum embedding generations per minute across all sources. "
            "Back-pressures the ingestion pipeline when a large backlog builds "
            "up, preventing memory/CPU saturation from parallel embed batches. "
            "0 removes the bound.",
        ),
    )
    extraction_model: str = field(
        default="",
        metadata=_meta(
            "Extraction Model",
            "LLM model used for document extraction and summarization. Empty "
            "uses the default model (agent.model). Set to a specific model id "
            "(e.g. 'claude-haiku-4.5') to use a cheaper model for extraction "
            "without changing your chat default.",
        ),
    )
    extraction_pool_size: int = field(
        default=3,
        metadata=_meta(
            "Extraction Pool Size",
            "Number of concurrent LLM workers for document extraction. More "
            "workers = faster ingestion but higher peak cost. Each worker holds "
            "a long-lived session. Requires restart to take effect.",
        ),
    )
    auto_discover_folder: bool = field(
        default=False,
        metadata=_meta(
            "Auto-Discover Documents Folder",
            "Watch for a documents folder inside the active workspace and "
            "register it as a Knowledge source automatically, so files dropped "
            "there become searchable without adding the source by hand. The "
            "folder is never created for you: its absence means you have not "
            "opted in, and it is picked up within one watcher sweep of being "
            "created -- no restart needed. Off by default because ingestion "
            "spends LLM extraction on every supported file in the folder.",
        ),
    )
    auto_discover_dirname: str = field(
        default="knowledge-docs",
        metadata=_meta(
            "Documents Folder Name",
            "Name of the folder inside the workspace that auto-discovery looks "
            "for. A single path segment -- separators and traversal are rejected "
            "so the source cannot be redirected outside the workspace. Avoid "
            "'knowledge': that is where the Library's own SQLite store lives and "
            "it always exists, which would defeat discovery.",
        ),
    )


def _read_auto_add_documents(knowledge_data: dict) -> bool:
    """Read the auto-add-documents toggle, honouring the older spelling.

    Accepts the older ``auto_ingest_doc_links`` spelling so an existing config's
    value carries over instead of silently reverting to the default on upgrade.
    Canonical spelling is ``auto_add_documents``, which is what ``save()`` writes,
    so a save/load round-trip settles on it.

    Absent both keys the feature is OFF: auto-ingest is opt-in, so a config that
    never mentioned it must not start adding documents.
    """
    for key in ("auto_add_documents", "auto_ingest_doc_links"):
        if key in knowledge_data:
            return bool(knowledge_data.get(key))
    return False


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
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
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
            "Publish-provider ids the operator permits (registry keys). "
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
            "The sensitive-path denylist (~/.aws, ~/.ssh, ~/.kiro/crew, …) still "
            "applies inside every allowed root.",
            tags=["artifacts"],
        ),
    )


@dataclass
class TailscaleConfig:
    """Tailnet access for the dashboard (RFC: rfc-tailnet-dashboard-access)."""

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Tailnet Access",
            "Accept this machine's own MagicDNS name as a dashboard origin, so "
            "`tailscale serve` works without hand-writing dashboard.url. Reads "
            "the local Tailscale daemon once at startup; contributes nothing if "
            "Tailscale is absent, stopped, or MagicDNS is off. Does NOT widen the "
            "network bind and does NOT change authentication — every request "
            "still needs a dashboard session.",
        ),
    )
    trust_identity: bool = field(
        default=False,
        metadata=_meta(
            "Trust Tailnet Identity",
            "Pin dashboard sessions arriving via `tailscale serve` to the "
            "daemon-verified tailnet peer instead of the tunnel's shared "
            "loopback address, and record that identity in the audit trail. "
            "Explicit opt-in, never inferred, and requires a non-empty "
            "allowed_logins — enabling it with an empty allowlist is refused at "
            "load. Every failure to verify a peer falls back to the ordinary "
            "token path. POSIX only. Takes effect on the next gateway "
            "start (the trust settings are read once at startup).",
        ),
    )
    allowed_logins: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Tailnet Logins",
            "Tailscale logins permitted when trust_identity is on. Mandatory: "
            "a shared tailnet can have hundreds of members, so identity trust "
            "without an allowlist would hand each of them the dashboard. A "
            "verified peer whose login is not listed is denied.",
        ),
    )
    pin_scope: str = field(
        default="node",
        metadata=_meta(
            "Pin Scope",
            "What an identity-pinned session binds to: 'node' (default — a "
            "leaked cookie is usable only from the original device) or 'login' "
            "(usable from any device carrying that Tailscale identity). An "
            "unrecognised value falls back to 'node'. An ACL-tagged node is "
            "always pinned at node scope regardless of this setting. Takes "
            "effect on the next gateway start.",
        ),
    )


def _tailscale_config_from(raw: object) -> TailscaleConfig:
    """Build the validated :class:`TailscaleConfig` (RFC §3/§3.1 load rules).

    Two rules, both narrowing-only so a typo can never widen access:

    * ``trust_identity: true`` with an empty ``allowed_logins`` is a
      configuration error — refused with a logged reason, identity trust stays
      OFF. Never a silently-permissive default: "any tailnet member" on a
      shared corporate tailnet would hand the dashboard to all of them.
    * An unrecognised ``pin_scope`` falls back to ``"node"`` (the narrower
      scope) with a logged warning — never to ``"login"``.
    """
    data = _safe_dict(raw)
    enabled = _safe_bool(data.get("enabled"), False)
    trust_identity = _safe_bool(data.get("trust_identity"), False)
    raw_logins = data.get("allowed_logins")
    allowed_logins = [
        entry.strip()
        for entry in (raw_logins if isinstance(raw_logins, list) else [])
        if isinstance(entry, str) and entry.strip()
    ]
    pin_scope = str(data.get("pin_scope") or "node").strip().lower()
    if pin_scope not in ("node", "login"):
        logger.warning(
            "dashboard.tailscale.pin_scope %r is not recognised; falling back to "
            "'node' (the narrower scope)",
            pin_scope,
        )
        pin_scope = "node"
    if trust_identity and not allowed_logins:
        logger.error(
            "dashboard.tailscale.trust_identity is on but allowed_logins is "
            "empty — identity trust requires an explicit login allowlist and "
            "stays OFF. Add the Tailscale logins you want to admit."
        )
        trust_identity = False
    return TailscaleConfig(
        enabled=enabled,
        trust_identity=trust_identity,
        allowed_logins=allowed_logins,
        pin_scope=pin_scope,
    )


@dataclass
class JiraAuthEntry:
    """Connection metadata for one Jira instance (Cloud or Server/DC).

    The API token is NOT stored here — it lives in the protected .env file
    as JIRA_API_TOKEN (same isolation pattern as Slack/Discord/Telegram tokens).
    This dataclass holds only non-sensitive connection metadata.
    """

    host: str = field(
        default="",
        metadata=_meta(
            "Host",
            "Jira instance hostname (e.g. 'myorg.atlassian.net' or "
            "'jira.internal.corp:8443'). Must match the host in the issue URL.",
        ),
    )
    email: str = field(
        default="",
        metadata=_meta(
            "Email",
            "Atlassian account email for Cloud instances (used in Basic auth "
            "header). Leave empty for Server/DC instances that use a PAT.",
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
    tailscale: TailscaleConfig = field(
        default_factory=TailscaleConfig,
        metadata=_meta(
            "Tailscale",
            "Reach the dashboard over your tailnet via `tailscale serve`.",
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
            "Time window (minutes) for session restoration, and for surfacing "
            "channel conversations in the chat list (0-1440). 0 = no limit.",
        ),
    )
    surface_channel_sessions: bool = field(
        default=True,
        metadata=_meta(
            "Show Channel Conversations In Chat List",
            "Show recently active Slack/Discord/Teams (etc.) conversations in the "
            "dashboard's chat list instead of only under History. Uses the same "
            "recency window as session restoration.",
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
    loop_stall_exit_after_secs: int = field(
        default=25,
        metadata=_meta(
            "Loop-stall Hard-exit Budget (secs)",
            "Seconds the gateway's event loop may go silent before it dumps all "
            "thread stacks and exits so systemd can restart it. Raise it on a "
            "host that does heavy subprocess work (long builds, test suites, "
            "many child reaps), which can wedge the loop briefly without being "
            "genuinely dead. Clamped to 10s..300s. Note the desktop app's "
            "liveness probe kills at roughly 20s independently, so a value "
            "above that only takes effect for a headless gateway — the desktop "
            "probe wins first and the stack dump is lost.",
        ),
    )
    cautious_boot: bool = field(
        default=True,
        metadata=_meta(
            "Cautious Boot After Crash",
            "When the gateway starts and finds a recent loop-stall crash dump "
            "(under 30 minutes old) from the previous instance, stagger the "
            "startup burst — MCP servers, cron scheduler, app backends, "
            "session restores — with short pauses instead of launching "
            "everything at once, so a host still under memory pressure is "
            "not pushed straight back into the same collapse.",
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
    use_builtin_browser: bool = field(
        default=True,
        metadata=_meta(
            "Use Built-in Browser",
            "When on, the browser tool opens pages in Kiro Crew's built-in panel "
            "(desktop app only). When off, the agent browses via playwright-cli.",
        ),
    )
    verbosity: str = field(
        default="default",
        metadata=_meta(
            "Response Verbosity",
            "Controls how terse the agent's prose is. 'default' is normal; "
            "'concise' injects brevity guidelines (lead with the answer, cut "
            "filler, keep code/errors verbatim); 'ultra' writes for an ADHD "
            "reader — the answer lands in a 3-sentence opening, and any detail "
            "after it must be scannable bullets rather than prose. Both levels "
            "preserve full detail for security warnings, irreversible-action "
            "confirmations, and ordered multi-step instructions.",
            enum=["default", "concise", "ultra"],
        ),
    )
    link_previews: bool = field(
        default=False,
        metadata=_meta(
            "Link Previews",
            "Render http(s) links in assistant messages as favicon + page title "
            "instead of a raw URL. Off by default because it is a network "
            "decision, not a display one: this machine fetches every link the "
            "model outputs, so each linked site sees a request from your IP "
            "address. When false the /api/link-meta endpoint fetches nothing and "
            "returns 403.",
        ),
    )
    usage_text_scrape_enabled: bool = field(
        default=False,
        metadata=_meta(
            "Spend Credits To Read The Credit Meter",
            "Let the credit pill fall back to a `kiro-cli /usage` chat turn when "
            "the free usage API returns no plan. That fallback is a REAL billed "
            "LLM turn on whichever model the lite agent resolves, and it repeats "
            "on every refresh interval for as long as any dashboard tab is open, "
            "so it is off by default: a meter that reports spending must not "
            "itself spend. While it is off the pill shows whatever the free API "
            "returned and hides when the API has nothing to show.",
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
    prevent_sleep: bool = field(
        default=False,
        metadata=_meta(
            "Prevent Sleep While Running",
            "Keep this computer awake while the agent is running a task, so a long "
            "task is not interrupted by the machine going to sleep. Off by default. "
            "Uses caffeinate on macOS, systemd-inhibit on Linux, and "
            "SetThreadExecutionState on Windows; on a host with no keep-awake "
            "backend it is a no-op.",
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
    mcp_app_panel: bool = field(
        default=False,
        metadata=_meta(
            "Open MCP Apps in the side panel",
            "Render interactive MCP Apps (such as Excalidraw diagrams) in the right "
            "side panel instead of inline in the chat bubble. The panel opens "
            "automatically and can be expanded; the chat keeps a compact "
            "placeholder linking to it.",
        ),
    )
    # Off by default because the panel's dismissal marker is keyed by slot and a
    # new session inherits `dashboard.default_project`: with this on, every new
    # chat in a git project opens the panel, which is not the once-per-project
    # nudge the behaviour looks like. That reasoning is the flag's rationale, not
    # something a user reading the setting needs, so it stays out of `help`.
    auto_open_git_panel: bool = field(
        default=False,
        metadata=_meta(
            "Auto-open Git in the side panel",
            "Expand the chat's right side panel to its Git tab each time a session "
            "starts in a project directory that is a git repository. The Git tab "
            "itself is always created either way, so it is one click away.",
        ),
    )
    terminal: dict = field(
        default_factory=lambda: {"enabled": True},
        metadata=_meta(
            "Terminal",
            "Terminal panel configuration. Set enabled=false to hide the CLI panel in the dashboard.",
            # Declared sub-keys become first-class schema entries
            # (dashboard.terminal.<key>) so Settings controls can reference
            # them by configKey. The field stays a plain dict — undeclared
            # keys (max_sessions, completion.commands, cwd) remain valid via
            # additionalProperties and round-trip untouched.
            properties={
                "shell": {
                    "type": "string",
                    "default": "",
                    "x-meta": {
                        "label": "Default shell",
                        "help": (
                            "Shell the built-in terminal launches — an absolute path or a "
                            "command on PATH. Empty = the system default ($SHELL)."
                        ),
                    },
                },
            },
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
    language: str = field(
        default="",
        metadata=_meta(
            "Language",
            "Dashboard UI language as a BCP-47 tag (e.g. 'en', 'zh-CN'). "
            "Empty = auto-detect from the browser's preferred languages, "
            "falling back to English. Persisted here (not only in the browser) "
            "so the choice follows the user across browsers and the desktop app.",
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
    import_onboarded: bool = field(
        default=False,
        metadata=_meta(
            "Import Onboarded",
            "Whether the user has completed or skipped foreign-agent import onboarding.",
        ),
    )
    privacy_acked: bool = field(
        default=False,
        metadata=_meta(
            "Privacy Acknowledged",
            "Whether the user has seen the mandatory first-run Privacy chapter, which "
            "discloses the anonymous heartbeat and offers the opt-out. Server-backed "
            "rather than browser-local because the gateway gates the very FIRST "
            "heartbeat on it: until this is true the user has not yet been shown the "
            "opt-out, and a ping sent before the offer makes the offer meaningless.",
        ),
    )
    user_role: str = field(
        default="",
        metadata=_meta(
            "User Role",
            "The user's professional background, collected during onboarding "
            "(developer, designer, product-manager, data-ml, it-ops, other). "
            "Injected into the agent prompt so responses match the user's "
            "domain vocabulary. Empty = unspecified.",
        ),
    )
    user_role_other: str = field(
        default="",
        metadata=_meta(
            "User Role (Custom)",
            "Free-text role the user typed when they picked 'other' during "
            "onboarding (e.g. 'solutions architect'). Consulted ONLY while "
            "user_role == 'other'; quoted verbatim into the agent prompt. "
            "Retained (not cleared) when another role is picked, so it is "
            "inert rather than contradictory and survives switching back. "
            "Empty = 'other' contributes nothing.",
        ),
    )
    user_technical_level: str = field(
        default="",
        metadata=_meta(
            "User Technical Level",
            "How technical the user is (codes, somewhat-technical, non-technical), "
            "collected during onboarding. Injected into the agent prompt to "
            "calibrate explanation depth. Empty = unspecified.",
        ),
    )
    tips_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Tips Enabled",
            "Show feature tip cards while the agent is thinking.",
        ),
    )
    folder_suggestions_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Folder Suggestions Enabled",
            "Offer to file a newly-titled, unfiled chat session into a matching folder.",
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
        default="auto",
        metadata=_meta(
            "Tips Model",
            'Model ID for tips generation. Defaults to "auto" so it inherits the '
            "account's governed model; a hardcoded id can be rejected on accounts "
            "or partitions that do not serve it.",
        ),
    )
    tips_explore_ratio: float = field(
        default=0.2,
        metadata=_meta(
            "Tips Explore Ratio",
            "Probability of picking a random catalog tip instead of personalized (0-1). Higher = more general discovery.",
        ),
    )
    gitlab_hosts: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Self-Hosted GitLab Hosts",
            "Exact hostnames (optionally host:port) of self-managed GitLab "
            "instances whose merge-request URLs the Changes panel may load. "
            "Empty = gitlab.com only (deny-by-default): a merge-request URL is "
            "only sent to the glab CLI if its host is an exact member of this "
            "list, so a pasted link cannot aim the credential-bearing CLI at an "
            "arbitrary or internal host. Suffixes and wildcards are not matched. "
            "Adding an entry authorizes the local glab CLI, with its token, to "
            "reach that host, including hosts only resolvable on your network.",
        ),
    )
    jira_hosts: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Self-Hosted Jira Hosts",
            "Exact hostnames (optionally host:port) of self-managed Jira or "
            "Jira Data Center instances whose issue URLs the Issues panel may "
            "recognize. Atlassian Cloud instances (*.atlassian.net) are always "
            "accepted without listing. Empty = Cloud-only (deny-by-default): a "
            "Jira issue URL is only recognized if its host matches an entry "
            "here. Suffixes and wildcards are not matched.",
        ),
    )
    jira_auth: list[JiraAuthEntry] = field(
        default_factory=list,
        metadata=_meta(
            "Jira Authentication",
            "Per-host credentials for the Jira REST API so the Issues panel "
            "can fetch issue details inline. Each entry pairs a host with an "
            "API token. Atlassian Cloud (*.atlassian.net) uses email + API "
            "token (Basic auth); Jira Server/Data Center uses a Personal "
            "Access Token (Bearer). When no entry matches the issue host, the "
            "panel falls back to the link-out 'Open in Jira' behavior.",
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
    model: str = field(
        default="",
        metadata=_meta(
            "Model",
            "Default model for sessions on this agent. Empty inherits: the bound "
            "kiro agent's own pinned model first, then the global agent.model "
            "fallback. A per-session pick still overrides this.",
        ),
    )
    description: str = field(
        default="",
        metadata=_meta("Description", "Human-readable agent description."),
    )
    triggers: str = field(
        default="",
        metadata=_meta(
            "Triggers",
            "Routing intent for orchestrator crew selection: free-text 'when to "
            "use this crew' guidance the main agent reads via select_crew. A crew "
            "with no triggers is not offered for selection.",
        ),
    )
    source: str = field(
        default="kirocrew",
        metadata=_meta("Source", "Agent origin: kirocrew or builtin."),
    )
    # Per-agent watchdog window overrides. The global ``watchdog.tool_stall_*``
    # defaults (1h) are build-scale forbearance; an agent that never runs a long
    # build (a pure-LLM reviewer, read-only git) can declare much lower windows
    # here. 0 (the default) inherits the global value — mirrors the
    # empty-inherits convention of ``model`` above.
    watchdog_tool_stall_suspect_secs: float = field(
        default=0.0,
        metadata=_meta(
            "Tool stall suspect override (s)",
            "Per-agent override for watchdog.tool_stall_suspect_secs on sessions "
            "running this agent. 0 inherits the global window (default 1h, tuned "
            "for long builds). Set low (e.g. 900) for a pure-LLM agent whose "
            "longest legitimate silent gap is minutes, not hours.",
        ),
    )
    watchdog_tool_stall_hard_cap_secs: float = field(
        default=0.0,
        metadata=_meta(
            "Tool stall hard cap override (s)",
            "Per-agent override for watchdog.tool_stall_hard_cap_secs on sessions "
            "running this agent. 0 inherits the global cap (default 1h). Applies "
            "ONLY to UNKNOWN verdicts — a WORKING session is never acted on.",
        ),
    )
    telegram_account: str = field(
        default="",
        metadata=_meta(
            "Telegram Account",
            "Deprecated and inert: a binding to a named telegram.accounts entry "
            "no longer routes anything, because named accounts no longer start a "
            "bot. Preserved on load and save so an existing config is not "
            "rewritten out from under the operator.",
            deprecated=True,
        ),
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
        default="main",
        metadata=_meta("Branch", "Git branch to read from."),
    )
    trust: str = field(
        default="index",
        metadata=_meta(
            "Trust",
            "How much a registry's INDEX is trusted, which selects the credential "
            "posture for cloning the apps it lists. 'index' (the default) treats the "
            "index as untrusted content: every app it lists is cloned credential-free "
            "so a hostile entry cannot read a private sibling repo with this machine's "
            "git identity. 'owner' means the index is under change control the build "
            "owns, so its apps may clone with this machine's credentials. Setting it "
            "HERE has no effect: the trusted tier is honoured only for registries the "
            "build supplies, because this file is agent-writable and a tier read from "
            "it would not be your assertion. A value other than 'index' on a "
            "configured registry is read as 'index'.",
        ),
    )


@dataclass
class SkillsConfig:
    max_triggered: int = field(
        default=0,
        metadata=_meta(
            "Max Triggered",
            "Maximum number of skills a single message may flag as relevant (≥0). "
            "Each match injects that skill's full content, unless the skill sets "
            "inject_on_trigger: false (pointer-only; requires max_triggered > 0 to "
            "have any effect). Defaults to 0 (disabled): the agent discovers skills "
            "from the Available Skills index and reads them on demand via cat, "
            "$skillname, or skill_search. Set to a positive integer to re-enable "
            "per-turn word-overlap trigger matching.",
        ),
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
    # dashboard Settings → Skills toggle.
    auto_create_from_sessions: bool = field(
        default=False,
        metadata=_meta(
            "Auto-Create Skills",
            "When true, analyze each session after completion and synthesize a reusable "
            "SKILL.md when the session demonstrates a recurring procedure — one a future "
            "session, working on a different target, would run again. Candidates are staged "
            "for review (see approval_required) rather than going live, and live under "
            "skills/auto/ so they never collide with hand-authored skills. Disabled by "
            "default; enable in Settings → Skills.",
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
            "near-duplicate skills. Used as the lexical fallback when the Haiku "
            "dedupe judge is unavailable.",
        ),
    )
    # ── Staged approval + lifecycle (v2) ──
    approval_required: bool = field(
        default=True,
        metadata=_meta(
            "Skill Approval Required",
            "When true, auto-generated skill candidates land in a pending queue for "
            "human review instead of going live. Prose-only skills may auto-publish "
            "when this is false; skills that bundle scripts ALWAYS require approval "
            "regardless of this flag.",
        ),
    )
    max_auto_skills: int = field(
        default=100,
        metadata=_meta(
            "Max Auto Skills",
            "Hard cap (backstop) on the number of live auto-generated skills. When "
            "exceeded, the least-valuable (by recency + frequency) are archived — "
            "never hard-deleted — down to the cap (≥1).",
        ),
    )
    stale_after_days: int = field(
        default=30,
        metadata=_meta(
            "Skill Stale After (days)",
            "An auto-skill with no recorded use for this many days is marked stale "
            "(≥1). Never-used skills younger than this window are exempt (grace floor).",
        ),
    )
    archive_after_days: int = field(
        default=90,
        metadata=_meta(
            "Skill Archive After (days)",
            "An auto-skill inactive for this many days is archived (recoverable, "
            "never deleted). Must be ≥ stale_after_days.",
        ),
    )
    pending_ttl_days: int = field(
        default=30,
        metadata=_meta(
            "Pending Skill TTL (days)",
            "Unapproved skill candidates older than this are auto-cleaned from the "
            "pending queue (≥1).",
        ),
    )
    generate_scripts: bool = field(
        default=True,
        metadata=_meta(
            "Generate Skill Scripts",
            "When true, deterministic procedures may generate a validated Python "
            "helper script alongside the SKILL.md. Script-bearing skills always "
            "require approval.",
        ),
    )
    judge_model: str = field(
        default="auto",
        metadata=_meta(
            "Skill Judge Model",
            "Model used for the dedupe judge and the advisory pending review. "
            'Defaults to "auto" to inherit the account\'s governed model; the '
            "value only gates whether the judge runs (any truthy value enables "
            "it) — the judge turn itself runs on the shared background session.",
        ),
    )
    extra_paths: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Extra Skill Paths",
            "Additional directories to scan for skills. Supports ~ expansion. "
            "Skills from extra_paths are read-only (trigger matching + loading). "
            "Local ~/.kiro/crew/skills/ takes precedence for duplicate names.",
        ),
    )

    def __post_init__(self) -> None:
        if self.max_triggered < 0:
            logger.warning("max_triggered %d < 0, using 0", self.max_triggered)
            object.__setattr__(self, "max_triggered", 0)
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
        if self.max_auto_skills < 1:
            logger.warning("max_auto_skills %d < 1, using 1", self.max_auto_skills)
            object.__setattr__(self, "max_auto_skills", 1)
        if self.stale_after_days < 1:
            logger.warning("stale_after_days %d < 1, using 1", self.stale_after_days)
            object.__setattr__(self, "stale_after_days", 1)
        if self.archive_after_days < self.stale_after_days:
            logger.warning(
                "archive_after_days %d < stale_after_days %d, using stale_after_days",
                self.archive_after_days,
                self.stale_after_days,
            )
            object.__setattr__(self, "archive_after_days", self.stale_after_days)
        if self.pending_ttl_days < 1:
            logger.warning("pending_ttl_days %d < 1, using 1", self.pending_ttl_days)
            object.__setattr__(self, "pending_ttl_days", 1)


@dataclass
class SessionSummaryConfig:
    """Intent-level session summaries shown in the chat right panel.

    Summarizing spends tokens on a turn the user did not ask to pay for, so every
    field defaults to off/conservative and the feature is inert until ``enabled``.
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Session Summaries",
            "When true, summarize each session by intent after a turn completes so "
            "the chat right panel can show what the session is about, what has "
            "happened, and what to do next. Costs tokens on turns that change the "
            "session; an unchanged session is served from cache for free. Disabled "
            "by default; enable in Settings.",
        ),
    )
    min_user_turns: int = field(
        default=2,
        metadata=_meta(
            "Minimum User Turns",
            "Skip summarization until the session has at least this many user "
            "messages (>=1). A one-exchange session has no intent structure worth "
            "extracting, and the session title already covers it.",
        ),
    )
    regenerate_after_turns: int = field(
        default=1,
        metadata=_meta(
            "Regenerate Every N Turns",
            "How many completed turns must pass before the summary is rebuilt "
            "(>=1). 1 keeps the panel current at the cost of one pass per turn; "
            "raise it to trade freshness for tokens. A cached summary whose "
            "session has not changed is never rebuilt regardless of this value.",
        ),
    )
    max_intents: int = field(
        default=8,
        metadata=_meta(
            "Maximum Intents",
            "Upper bound on intents kept per session (>=1). Beyond this the oldest "
            "closed intents are dropped, since the panel collapses them anyway.",
        ),
    )
    max_constraints: int = field(
        default=5,
        metadata=_meta(
            "Maximum Project Notes",
            "Upper bound on session-level operational notes -- the recurring facts "
            "about how this project is run (>=0). Kept small on purpose: a long "
            "list stops being read, and durable cross-session preferences belong "
            "in lessons rather than here.",
        ),
    )
    assistant_excerpt_chars: int = field(
        default=400,
        metadata=_meta(
            "Assistant Excerpt Size",
            "Characters kept from each end of an assistant message when building "
            "the summarization input (>=80). User messages are always included in "
            "full -- they carry intent and are small -- while assistant output is "
            "excerpted because it holds the progress detail but dominates the "
            "transcript.",
        ),
    )

    def __post_init__(self) -> None:
        if self.min_user_turns < 1:
            logger.warning("min_user_turns %d < 1, using 1", self.min_user_turns)
            object.__setattr__(self, "min_user_turns", 1)
        if self.regenerate_after_turns < 1:
            logger.warning(
                "regenerate_after_turns %d < 1, using 1", self.regenerate_after_turns
            )
            object.__setattr__(self, "regenerate_after_turns", 1)
        if self.max_intents < 1:
            logger.warning("max_intents %d < 1, using 1", self.max_intents)
            object.__setattr__(self, "max_intents", 1)
        if self.max_constraints < 0:
            logger.warning("max_constraints %d < 0, using 0", self.max_constraints)
            object.__setattr__(self, "max_constraints", 0)
        if self.assistant_excerpt_chars < 80:
            logger.warning(
                "assistant_excerpt_chars %d < 80, using 80",
                self.assistant_excerpt_chars,
            )
            object.__setattr__(self, "assistant_excerpt_chars", 80)


@dataclass
class TelemetryConfig:
    """Metrics telemetry settings (Wave 0 trunk).

    Default OFF: when disabled, metric call sites are cheap no-ops and nothing is
    written or exported (byte-identical to no telemetry), mirroring the
    ``mcp_gateway.enabled`` / ``skills.lazy_load`` opt-in convention. When
    enabled, a local-first JSONL sink under ``~/.kiro/crew/metrics`` is activated;
    remote / OTLP egress is a separate opt-in requiring ``kirocrew[otlp]``.
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Main switch for Kiro Crew metrics telemetry. Off by default: metric "
            "call sites are no-ops and nothing is written. When on, a local-first "
            "JSONL sink under ~/.kiro/crew/metrics is enabled (no network egress).",
        ),
    )
    local_dir: str = field(
        default="",
        metadata=_meta(
            "Local Metrics Dir",
            "Directory for local JSONL metric shards. Empty = ~/.kiro/crew/metrics. "
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
    beacon_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Anonymous Usage Beacon",
            "Anonymous daily heartbeat so maintainers can see how many "
            "copies are actively running, which versions are in use, and "
            "which distribution channels they came from. Sends "
            "EXACTLY five fields, at most once per day: a random installation "
            "id, app release (major.minor.patch only — build stamps are "
            "stripped), Python minor version, distribution channel, and a "
            "first-run bit. NEVER sends prompts, "
            "model output, file contents, paths, repo names, credentials, "
            "hostname, username, IP address, operating system, CPU "
            "architecture, release channel, or governance posture. "
            "Automatically suppressed in CI "
            "and for a non-default KIROCREW_HOME. Opt out with "
            "KIROCREW_TELEMETRY_DISABLED=1 or by turning this off; an "
            "enterprise policy can also pin it off via the "
            "capabilities.telemetry governance scope, which this switch cannot "
            "override. Independent "
            "of the 'enabled' switch above, which is local-only metrics "
            "collection and still never egresses.",
        ),
    )
    beacon_endpoint: str = field(
        default=_DEFAULT_BEACON_ENDPOINT,
        metadata=_meta(
            "Beacon Endpoint",
            "HTTPS base URL that receives the anonymous heartbeat. EMPTY = no "
            "beacon is ever sent, regardless of the toggle above. Must be "
            "https:// (a plaintext heartbeat would reveal which hosts run this "
            "software to any on-path observer); a non-https value is cleared.",
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
        # Fail CLOSED on an unusable beacon endpoint: clear it rather than send
        # the heartbeat in plaintext or defer a parse failure to the send path.
        # Enforced here so the invariant holds for every consumer of the config.
        # A startswith("https://") test is NOT sufficient — it accepts a host
        # containing whitespace, which urlopen then rejects with
        # http.client.InvalidURL from deep inside the beacon thread. Parse it the
        # same way the send path does, and require a whitespace-free netloc.
        endpoint = self.beacon_endpoint.strip()
        if endpoint:
            try:
                parts = _urlsplit(endpoint)
                usable = (
                    parts.scheme == "https"
                    and bool(parts.netloc)
                    and not any(c.isspace() for c in parts.netloc)
                )
            except ValueError:
                usable = False
            if not usable:
                logger.warning("beacon_endpoint is not a usable https:// URL; beacon disabled")
                endpoint = ""
        if endpoint != self.beacon_endpoint:
            object.__setattr__(self, "beacon_endpoint", endpoint)


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
# Why the loader must also clamp: the
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

# agent.chat_turn_timeout_secs — wall-clock ceiling for one chat turn. The ACP
# transport's per-prompt wait follows this value (acp/client.py
# ``resolve_prompt_timeout``, which adds a margin so the dashboard's visible
# card fires before the transport cut), so the max is no longer pinned to the
# transport's 2h default. It is bounded at 24h because the ceiling is a runaway
# backstop, not a scheduler: a single prompt→response turn longer than a day is
# pathological, and multi-day unattended operation belongs to the loop
# mechanisms (monitor/goal loops, crons), which end the turn between cycles and
# survive restarts — a marathon turn does not. The floor keeps the backstop
# from being set so low it cuts ordinary work.
CHAT_TURN_TIMEOUT_MIN = 300
CHAT_TURN_TIMEOUT_MAX = 86400

# agent.session_start_timeout_secs — budget for ACP session/new + session/load
# on the shared runtime (acp/runtime.py ``_SESSION_NEW_TIMEOUT`` is the built-in
# default). kiro-cli blocks the session/new response while it initializes the
# session's MCP servers, so start time scales with the agent's server count and
# per-server cold-start cost (observed: a 71-server agent with no pending OAuth
# completes in ~14s; a 17-server agent behind a sandboxed per-server launcher on
# a loaded host takes ~50s). The floor IS the default: the budget must stay
# comfortably ABOVE the backend's 30s OAuth authorization wait (issue #2946) —
# a lower value recreates the session-start race the dedicated budget exists to
# prevent, so out-of-range values clamp UP to it. The max bounds a typo'd
# value: a session start slower than 15 minutes is pathological and should
# surface as a timeout, not wait forever.
SESSION_START_TIMEOUT_MIN = 90
SESSION_START_TIMEOUT_MAX = 900

# agent.tool_approval_timeout_secs — how long a chat turn parks waiting for a
# human to answer a tool-approval prompt. The floor keeps the window long enough
# for a human who is actually present to reach the dashboard. The max is pinned
# at 7200 and deliberately DECOUPLED from CHAT_TURN_TIMEOUT_MAX (24h): the
# approval suites hold their own flat 2h runtime window
# (``DashboardState._APPROVAL_TIMEOUT``), so a larger configured window would
# pass validation here and then silently never be honoured at runtime. The
# binding limit below the static max is the cross-field clamp in
# ``_clamp_security_bounds``, which pulls the window APPROVAL_TURN_MARGIN_SECS
# under the configured turn ceiling.
TOOL_APPROVAL_TIMEOUT_MIN = 30
TOOL_APPROVAL_TIMEOUT_MAX = 7200

# The turn ceiling assumed when config omits ``agent.chat_turn_timeout_secs``.
# Read from the dataclass default so the two cannot drift apart.
_DEFAULT_CHAT_TURN_TIMEOUT_SECS = int(
    AgentConfig.__dataclass_fields__["chat_turn_timeout_secs"].default  # type: ignore[arg-type]
)

# Minimum slack between the approval window and the turn ceiling. Two things
# need it: the approval deadline must land inside the turn so its own "nobody
# approved, resend" card renders instead of the generic turn-timeout card, and a
# late approval must leave the turn some time to actually run the tool. A window
# flush against the ceiling satisfies neither.
APPROVAL_TURN_MARGIN_SECS = 60

# dashboard.loop_stall_exit_after_secs — event-loop silence tolerated before the
# gateway dumps all thread stacks and hard-exits for systemd to restart. The
# floor keeps a stall from being declared faster than ordinary GC/IO pauses; the
# ceiling keeps a wedged gateway from sitting unrecoverable for minutes. Above
# ~20s the desktop app's own liveness probe kills first and the dump is lost,
# which is a documented trade-off rather than a bound (a headless gateway has no
# such probe), so it is not enforced here.
LOOP_STALL_EXIT_AFTER_MIN = 10
LOOP_STALL_EXIT_AFTER_MAX = 300

# agent.max_subagents fixed-pin floor. 0 is the "auto-size" sentinel; any other
# (explicit) value must be >= this floor. A pin of 1 or 2 would silently DISABLE
# auto-sizing and run below today's default of 3, so such values are normalized
# UP to the floor at load time (see _clamp_security_bounds) and rejected by the
# dashboard API. Mirrors ``subagent._LEGACY_DEFAULT_MAX`` (kept as a local
# constant to avoid a config→subagent import cycle).
MAX_SUBAGENTS_FIXED_FLOOR = 3

# session.autocompact_pct — context-usage percentage at which the backend
# autocompactor fires. SINGLE SOURCE OF TRUTH for the documented 5-90 range:
# the dashboard config API (``dashboard/handlers/core.py``) validates writes
# against these same constants, and the load read clamps a hand-edited
# config.json value into them, so the two ranges cannot drift as separate
# literals. The autocompactor is the backstop that keeps a session's context
# window from overflowing — above the ceiling the trigger
# (``pct >= autocompact_pct``) never fires before the window overflows, and
# at/below zero it fires on every turn. Floats are outside the int-only
# ``_SECURITY_BOUNDED_FIELDS`` sweep, so the clamp lives on the ``_safe_float``
# read instead.
AUTOCOMPACT_PCT_MIN = 5.0
AUTOCOMPACT_PCT_MAX = 90.0

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
    ("agent", "chat_turn_timeout_secs", CHAT_TURN_TIMEOUT_MIN, CHAT_TURN_TIMEOUT_MAX),
    (
        "agent",
        "session_start_timeout_secs",
        SESSION_START_TIMEOUT_MIN,
        SESSION_START_TIMEOUT_MAX,
    ),
    (
        "agent",
        "tool_approval_timeout_secs",
        TOOL_APPROVAL_TIMEOUT_MIN,
        TOOL_APPROVAL_TIMEOUT_MAX,
    ),
    (
        "dashboard",
        "loop_stall_exit_after_secs",
        LOOP_STALL_EXIT_AFTER_MIN,
        LOOP_STALL_EXIT_AFTER_MAX,
    ),
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

    # tool_approval_timeout_secs cross-field case: the approval window must end
    # inside the turn that opened it. At or above the turn ceiling it can never
    # fire — the turn is cut first, so the user is told "this turn timed out"
    # while the real cause (nobody answered the approval prompt) is never named,
    # and an unattended run burns the entire ceiling on every prompt. Clamp to
    # APPROVAL_TURN_MARGIN_SECS below the ceiling. Runs after the generic range
    # clamp above, so both operands are already inside their declared bounds.
    if isinstance(agent, dict):
        window = agent.get("tool_approval_timeout_secs")
        ceiling = agent.get("chat_turn_timeout_secs", _DEFAULT_CHAT_TURN_TIMEOUT_SECS)
        if not isinstance(ceiling, int) or isinstance(ceiling, bool):
            ceiling = _DEFAULT_CHAT_TURN_TIMEOUT_SECS
        budget = max(TOOL_APPROVAL_TIMEOUT_MIN, ceiling - APPROVAL_TURN_MARGIN_SECS)
        if isinstance(window, int) and not isinstance(window, bool) and window > budget:
            agent["tool_approval_timeout_secs"] = budget
            logger.warning(
                "config agent.tool_approval_timeout_secs=%d leaves less than %ds "
                "under the %ds turn ceiling; clamped to %d. A window that outlives "
                "the turn can never fire: the turn is cut first and reports itself "
                "as a turn timeout, hiding the unanswered approval.",
                window,
                APPROVAL_TURN_MARGIN_SECS,
                ceiling,
                budget,
            )
            _log_config_clamp_event(
                "agent.tool_approval_timeout_secs",
                window,
                budget,
                TOOL_APPROVAL_TIMEOUT_MIN,
                budget,
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


_VALID_STT_PROVIDERS = ("whisper", "mlx", "apple", "parakeet", "transcribe")
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


_YOLO_DURATION_SECS: dict[str, int] = {
    "30m": 1800,
    "1h": 3600,
    "6h": 21600,
    "12h": 43200,
    "24h": 86400,
}
_YOLO_DURATION_DEFAULT = "6h"
# Not a timed value: an ad-hoc grant that stays on with no expiry until the
# gateway process stops. In-memory only, so it cannot survive a restart.
YOLO_UNTIL_SHUTDOWN = "until_shutdown"


def _read_skip_permissions(agent_data: dict) -> bool:
    """Read the standing auto-approve declaration, honouring older spellings.

    The key was renamed from ``yolo`` so the config itself warns about what it
    does. Canonical spelling is ``dangerously_skip_permissions`` — snake_case
    like every other key in this file, which is also what ``save()`` writes, so
    a save/load round-trip preserves it.

    Two other spellings are accepted on read, most-specific first:
    ``dangerouslySkipPermissions`` (the camelCase form used by other agent tools,
    so a config copied from one still works) and the legacy ``yolo`` (so no
    existing config silently loses auto-approve on upgrade).

    Requires a REAL ``bool``, not Python truthiness: a stringly-typed value
    from a templated/generated config — ``"false"``, ``"0"``, ``"no"``, or any
    other non-empty string a hand-edit or a config generator might write — is
    truthy in Python, so a bare ``bool(...)`` here would silently turn
    "explicitly disabled" into the standing, unattended tool-auto-approve
    grant this key controls. A non-bool value is never treated as an
    affirmative grant; it falls through to check the next spelling, then to
    the ``False`` default.
    """
    for key in ("dangerously_skip_permissions", "dangerouslySkipPermissions", "yolo"):
        if key in agent_data:
            value = agent_data[key]
            if isinstance(value, bool):
                return value
            logger.warning(
                "agent.%s must be a real boolean, got %r — treating as unset",
                key,
                value,
            )
    return False


def _normalize_yolo_duration(value: object) -> str:
    """Coerce ``agent.yolo_duration`` to a supported ad-hoc duration label.

    Anything unrecognised (typo, removed value, wrong type) falls back to the
    default rather than failing the whole config load — the value only widens or
    narrows an already-bounded ad-hoc grant, and the 24h ceiling on timed values
    is enforced independently in ``SafetyOverride``.
    """
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _YOLO_DURATION_SECS or v == YOLO_UNTIL_SHUTDOWN:
            return v
    return _YOLO_DURATION_DEFAULT


def yolo_duration_to_secs(label: str) -> int:
    """Seconds for a ``yolo_duration`` label; 0 means "no timed expiry"."""
    if label == YOLO_UNTIL_SHUTDOWN:
        return 0
    return _YOLO_DURATION_SECS.get(label, _YOLO_DURATION_SECS[_YOLO_DURATION_DEFAULT])


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


def _normalize_acp_backend(value: object) -> str:
    """Coerce a persisted ``agent.acp_backend`` to a selectable backend.

    Anything not selectable — an unknown value, or a backend the code understands
    but cannot yet serve a session with — normalizes to the default (kiro-cli)
    with a warning rather than propagating: ``AcpProvider`` rejects an unknown
    backend by raising, and a value that is merely incomplete would instead fail
    on the operator's first message. Both must degrade to the working default at
    startup, with the reason in the log.

    The import is deferred because it cannot be done at module scope: reaching
    ``kiro_crew.acp.types`` executes the ``kiro_crew.acp`` package init, which
    imports the ACP client and runtime, which import this module — and this
    module is imported first by the gateway and desktop entrypoints.
    """
    from kiro_crew.acp.types import (
        ACP_BACKEND_KIRO,
        ACP_BACKENDS_KNOWN,
        ACP_BACKENDS_SELECTABLE,
    )

    if isinstance(value, str) and value in ACP_BACKENDS_SELECTABLE:
        return value
    if value not in (None, ""):
        known_but_unusable = isinstance(value, str) and value in ACP_BACKENDS_KNOWN
        logger.warning(
            "Ignoring agent.acp_backend %r (%s); using the default backend. "
            "Selectable values: %s",
            value,
            "not usable yet" if known_but_unusable else "unknown",
            ", ".join(repr(b) for b in sorted(ACP_BACKENDS_SELECTABLE)),
        )
    return ACP_BACKEND_KIRO


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
    # The KiroCrew agent's own default model, "" when it pins none. Ranks below
    # a per-session pick and above the bound kiro agent's pin / the global
    # agent.model fallback. Defaulted so existing keyword constructions and
    # test doubles built before this field stay valid.
    model: str = ""
    # Whether the REQUESTED agent name was actually honored. False means the
    # resolver fell back to the default agent, so dispatching these bindings runs
    # a different agent than the caller asked for. Callers that store the
    # requested name (chat slots) must not advertise it when this is False.
    # Defaults True so constructions predating this field keep their meaning.
    requested_resolved: bool = True
    # The KiroCrew ALIAS whose bindings these are ("" when no alias applied). A
    # caller replacing an unhonored request must store THIS, not ``kiro_agent``:
    # the stored value is re-resolved later and an alias is matched first, so a
    # physical kiro agent name that also happens to be an alias key would resolve
    # to that alias's target instead — reintroducing the advertised-vs-answering
    # mismatch. An alias key round-trips to itself.
    resolved_alias: str = ""


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
    parakeet_model: str = field(
        default="mlx-community/parakeet-tdt-0.6b-v3",
        metadata=_meta(
            "Parakeet Model",
            "Hugging Face repo for the parakeet-mlx model (parakeet provider only).",
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
            "Stream partial transcripts live to the dashboard input. Supported by the "
            "streaming providers only: `transcribe` (AWS, cloud) and `apple` "
            "(on-device, macOS 26+). The whisper/mlx CLIs have no partial-result "
            "channel.",
        ),
    )
    endpointing: bool = field(
        default=False,
        metadata=_meta(
            "Semantic endpointing",
            "While streaming dictation, run a fast background model on each stable "
            "transcript segment to detect when you have finished a complete request, "
            "then auto-submit. Streaming providers only (transcribe, apple); "
            "off by default.",
        ),
    )
    dictation_panel: bool = field(
        default=True,
        metadata=_meta(
            "Dictation Panel",
            "Show the animated dictation panel while recording instead of the thin status bar. "
            "Ignored when the browser lacks WebGL2 or the OS requests reduced motion — both "
            "fall back to the status bar.",
        ),
    )


@dataclass
class ComputerUseConfig:
    """Computer-use DISPLAY and LIMIT knobs — deliberately no ``enabled`` field.

    The primary enable is NOT here. It lives on the keystone
    ``computer_use.json`` (see :func:`computer_use_state_path`) because turning
    computer use on grants full desktop observation plus input synthesis, which
    is a security ceiling rather than a preference: ``config.json`` is writable
    by an auto-approved agent shell (``is_sensitive_bash_command`` does NOT block
    ``echo … > config.json``), so an enable stored here could be flipped by
    prompt injection. Adding an ``enabled`` field to this dataclass would
    silently re-open that hole — do not.

    Everything modelled here is safe for the agent to read and, at worst,
    annoying for it to change: how many accessibility nodes one walk returns, how
    deep it goes, how much text per node, and the screenshot's size/quality. The
    ceilings (``*_LIMIT`` in ``computer_use.types``) are enforced independently by
    the MCP tool schemas, so a hand-edited config cannot ask for an unbounded
    walk.
    """

    max_tree_nodes: int = field(
        default=_CU_DEFAULT_MAX_TREE_NODES,
        metadata=_meta(
            "Max Tree Nodes",
            "Accessibility nodes one window walk may return before truncating.",
        ),
    )
    max_tree_depth: int = field(
        default=_CU_DEFAULT_MAX_TREE_DEPTH,
        metadata=_meta("Max Tree Depth", "How deep one accessibility walk descends."),
    )
    text_limit: int = field(
        default=_CU_DEFAULT_TEXT_LIMIT,
        metadata=_meta("Text Limit", "Characters kept per element title/value."),
    )
    attach_screenshot: bool = field(
        default=_CU_DEFAULT_ATTACH_SCREENSHOT,
        metadata=_meta(
            "Attach Screenshots",
            "Capture the target window and relay the image path alongside the tree. "
            "The accessibility tree is always the primary channel.",
        ),
    )
    screenshot_max_px: int = field(
        default=_CU_DEFAULT_SCREENSHOT_MAX_PX,
        metadata=_meta(
            "Screenshot Width",
            "Longest edge of the downscaled screenshot, in pixels.",
        ),
    )
    screenshot_jpeg_quality: int = field(
        default=_CU_DEFAULT_SCREENSHOT_JPEG_QUALITY,
        metadata=_meta("Screenshot Quality", "JPEG quality 1-100 for the screenshot."),
    )
    cursor_motion: bool = field(
        default=False,
        metadata=_meta(
            "Cursor Motion",
            "Draw a visible cursor gliding to each target before a real-pointer "
            "click, so the operator can see what the agent is doing. macOS only; "
            "purely visual and never a permit — the drawn cursor is not the pointer, "
            "and turning this on grants no new capability.",
        ),
    )


@dataclass
class McpGatewayConfig:
    """Sidecar MCP broker daemon — shares MCP backends across sessions."""

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Share MCP Backends",
            "Let sessions with an identical server configuration share one MCP "
            "server process instead of each getting its own. Off, every session "
            "gets its own backend — the same process topology as running without "
            "the broker. Either this or MCP Apps starts the broker; see "
            "docs/architecture/design-notes/mcp-stub-decoupling.md. "
            "Default False — opt-in.",
        ),
    )
    apps_enabled: bool = field(
        default=True,
        metadata=_meta(
            "MCP Apps (retired, opt-out still honoured)",
            "RETIRED GOING FORWARD, but a stored `false` KEEPS ITS OPT-OUT. Nothing "
            "writes this key any more and MCP Management does not surface it: MCP "
            "Apps capability follows whether a server gets a stub, because the stub "
            "is what carries the render and callback path, so a preference cannot "
            "grant it. It can still WITHHOLD it — a released version treated "
            "`false` here as a trustworthy opt-out, so an operator who turned MCP "
            "Apps off stays off (tightest-wins: it beats KIROCREW_MCP_APPS=1, and an "
            "unreadable config fails closed). Absent defaults True, so 'not "
            "configured' is not an opt-out. To GET server-authored UI, turn on the "
            "server's stub in MCP Management — and clear a stored `false` here if "
            "you have one. The only other MCP Apps preference is where it renders "
            "(dashboard.mcp_app_panel). "
            "See docs/architecture/design-notes/mcp-stub-decoupling.md.",
        ),
    )
    forward_declared_env: bool = field(
        default=True,
        metadata=_meta(
            "Forward Declared Env",
            "Apply a pooled server's declared env (mcpServers.<name>.env) to the "
            "shared backend. Only non-secret keys are forwarded — rotating-secret "
            "and credential-prefixed keys are never applied to a shared backend, "
            "and gatewayd re-hashes the sidecar at spawn and forwards nothing on "
            "mismatch, so every forwarded key is one all co-tenants of that "
            "backend declared identically. Turn it OFF to make an env-declaring "
            "server run unwrapped (no stub, no pooling) instead.",
        ),
    )
    socket_path: str = field(
        default="",
        metadata=_meta(
            "Socket Path",
            "Local endpoint for the broker. Empty -> "
            "$KIROCREW_HOME/mcp-gateway/gateway.sock. A unix socket at this path "
            "on POSIX; on Windows the path is not created, it only derives the "
            "named-pipe name and locates the lock file beside it.",
        ),
    )
    overlay_dir: str = field(
        default="",
        metadata=_meta(
            "Overlay Dir",
            "Directory of rewritten agent JSON. Broker stubs from these specs are "
            "injected into each kiro-cli session via ACP session/new. "
            "Empty -> $KIROCREW_HOME/mcp-gateway/agents.",
        ),
    )
    idle_timeout_secs: int = field(
        default=300,
        metadata=_meta("Idle Timeout", "Seconds a refcount=0 MCP backend is kept before drain."),
    )
    resolve_once_refresh_hours: int = field(
        default=24,
        metadata=_meta(
            "Pre-resolve Refresh",
            "Hours before an UNPINNED npm-launcher MCP server (an npx spec at "
            "@latest, a range, or no version) is re-resolved from the registry. "
            "Pre-resolving lets a launch exec the installed tree directly, so "
            "session start does no dependency resolution and needs no network; "
            "this is how often that resolution is refreshed so such a spec still "
            "tracks upstream. A spec pinned to an exact version ignores this -- "
            "re-asking about an exact version cannot change the answer. 0 "
            "re-resolves on every prefetch pass; a server with no resolution yet "
            "simply launches the way it does today.",
        ),
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
    stub_servers: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Routed Servers",
            "MCP server names given a stub. The stub interposes a "
            "stub, which is what makes server-authored UI (MCP Apps) and backend "
            "sharing possible for that server — so it is the one per-server "
            "decision. Empty by default: an unstubbed server is launched by the "
            "session itself, the same process topology as running without the "
            "broker, and an empty list means no broker runs at all. Whether "
            "stubbed servers SHARE one backend is the separate global switch "
            "(mcp_gateway.enabled). Managed from MCP Management.",
        ),
    )
    poolable_servers: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Poolable Servers (deprecated)",
            "DEPRECATED alias for stub_servers. Read only when stub_servers "
            "is absent, so a config written before the stub became the per-server "
            "decision keeps working: a server that was pooled already had a stub, "
            "so migrating it to the stub set preserves its behaviour. There is no "
            "per-server sharing switch any more — sharing is global over the "
            "stub set.",
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
            "written to ~/.kiro/crew/mcp_spill/ and truncated inline to 16 KiB + "
            "a file path marker. Default 256 KiB. Set 0 to disable spilling. "
            "Env override: KIROCREW_MCP_SPILL_THRESHOLD.",
        ),
    )


# The forwarding default assumed when config omits
# ``mcp_gateway.forward_declared_env``. Read from the dataclass default so the
# field and every parse-site fallback cannot drift apart: this default is read
# in three places (the field, the loader's ``_safe_bool`` fallback, and the
# dashboard stub-batch reader), and a reader disagreeing with the field makes the
# batch skip servers the rewrite pools perfectly well.
FORWARD_DECLARED_ENV_DEFAULT = bool(
    McpGatewayConfig.__dataclass_fields__[  # type: ignore[arg-type]
        "forward_declared_env"
    ].default
)


@dataclass
class InstancesConfig:
    """Multi-instance management (the *Instances* feature).

    Gates and tunes the gateway's ability to manage/switch between several
    remote KiroCrew instances over SSH tunnels. Off by default — opt-in only,
    since enabling it allows the gateway to open SSH ``-L`` forwards and relaxes
    the dashboard CSP ``frame-src`` for the active loopback tunnel ports.

    Numeric transport defaults and bounds live in
    ``kiro_crew.instances.constants`` so their canonical values cannot drift
    from this dataclass.
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable multi-instance management — lets this gateway open SSH tunnels "
            "to remote Kiro Crews and embed their dashboards. Default off (opt-in). "
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
    connect_timeout_secs: float | None = field(
        default=None,
        metadata=_meta(
            "Connect Timeout (secs)",
            "How long to wait for the local forward port to accept connections "
            "before declaring a connect attempt failed. When unset, SSH uses "
            "15s and SSM uses 25s. Fifteen seconds is sufficient for a direct "
            "ssh TCP connect, but hosts behind a "
            "ProxyCommand or jump host routinely need longer (the proxy handshake "
            "runs before ssh begins the forward). Raise this if connecting a "
            "remote instance times out while the same ssh forward succeeds by hand. "
            "An explicit value applies to both transports. Clamped to [1, 120].",
        ),
    )
    mint_timeout_secs: float | None = field(
        default=None,
        metadata=_meta(
            "Mint Timeout (secs)",
            "How long to wait for the remote `kirocrew token` mint to return "
            "before failing a connect. When unset, SSH uses 30s and SSM uses "
            "90s (its dispatch latency is higher). The mint runs over the same "
            "ssh transport as the tunnel, so a host behind a ProxyCommand or "
            "jump host pays the proxy handshake here too. An explicit value "
            "applies to both transports, so size it for the slowest transport "
            "you use. Clamped to [10, 120].",
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
        if self.connect_timeout_secs is not None and self.connect_timeout_secs < 1.0:
            logger.warning(
                "instances.connect_timeout_secs %s < 1, using the transport default",
                self.connect_timeout_secs,
            )
            object.__setattr__(self, "connect_timeout_secs", None)
        elif (
            self.connect_timeout_secs is not None
            and self.connect_timeout_secs > _CONNECT_TIMEOUT_CEILING
        ):
            logger.warning(
                "instances.connect_timeout_secs %s > %s, clamping to %s",
                self.connect_timeout_secs,
                _CONNECT_TIMEOUT_CEILING,
                _CONNECT_TIMEOUT_CEILING,
            )
            object.__setattr__(self, "connect_timeout_secs", _CONNECT_TIMEOUT_CEILING)
        if self.mint_timeout_secs is not None and self.mint_timeout_secs < _MINT_TIMEOUT_FLOOR:
            logger.warning(
                "instances.mint_timeout_secs %s < %s, using the transport default",
                self.mint_timeout_secs,
                _MINT_TIMEOUT_FLOOR,
            )
            object.__setattr__(self, "mint_timeout_secs", None)
        elif (
            self.mint_timeout_secs is not None
            and self.mint_timeout_secs > _MINT_TIMEOUT_CEILING
        ):
            logger.warning(
                "instances.mint_timeout_secs %s > %s, clamping to %s",
                self.mint_timeout_secs,
                _MINT_TIMEOUT_CEILING,
                _MINT_TIMEOUT_CEILING,
            )
            object.__setattr__(self, "mint_timeout_secs", _MINT_TIMEOUT_CEILING)
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
    """Heartbeat background task queue (~/.kiro/crew/workspace/HEARTBEAT.md)."""

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
        default=3600.0,
        metadata=_meta(
            "Tool stall suspect (s)",
            "Idle seconds before an UNKNOWN-verdict in-flight tool is cancelled and "
            "the turn routed to tool-stall recovery (continue-nudge, no re-run of "
            "the original message). WORKING tools (e.g. a matched live build child) "
            "are never cancelled regardless of duration. Default 1h: generous enough "
            "for long builds and MCP tools on macOS, where the liveness oracle "
            "degrades (no /proc) and cannot distinguish a live build from a stall, "
            "while still landing inside the turn's own ceiling "
            "(agent.chat_turn_timeout_secs) so recovery is reachable. A window at or "
            "past that ceiling is clamped at load with a warning.",
        ),
    )
    tool_stall_hard_cap_secs: float = field(
        default=3600.0,
        metadata=_meta(
            "Hard cap (s)",
            "Absolute ceiling for UNKNOWN-verdict forbearance (e.g. the extended "
            "probably-thinking window). Applies ONLY to UNKNOWN verdicts — never "
            "to a WORKING session. Default 1h, bounded by the turn ceiling like "
            "the suspect window.",
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
            "Enable the WeCom channel via WeCom AI-bot. Requires the WECOM_BOT_ID "
            "and WECOM_SECRET credentials to be set.",
            tags=["wecom"],
        ),
    )
    allowed_users: list[dict] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Users",
            "WeCom users allowed to DM the bot. Each entry: {userid, name}. "
            "The owner is always allowed.",
            tags=["wecom"],
        ),
    )
    allow_all_users: bool = field(
        default=False,
        metadata=_meta(
            "Allow All Users",
            "Let every member of the WeCom organization DM the bot, bypassing "
            "the allow-list. Safe-ish because a WeCom AI bot is reachable only "
            "inside your own org tenant (unlike globally addressable bots), "
            "but it grants agent access to the whole company. Default off.",
            tags=["wecom"],
        ),
    )
    ws_url: str = field(
        default="wss://openws.work.weixin.qq.com",
        metadata=_meta(
            "WebSocket URL",
            "WeCom AI-bot long-connection endpoint.",
            tags=["wecom"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a DM's context passes this, prompt the user to /compact or /new "
            "instead of auto-compacting.",
            tags=["wecom"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this, even without a user "
            "decision, so the window never overflows.",
            tags=["wecom"],
        ),
    )
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
            tags=["wecom"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig (e.g. hard=50, soft=95, or an out-of-range value) can't
        # make the soft nudge unreachable -- _maybe_notice checks ``pct >= hard``
        # first.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )


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


def _coerce_opaque_str_ids(raw: object) -> list[str]:
    """Coerce a config value to a clean, deduped ``list[str]`` of OPAQUE IDs.

    For channels whose user IDs are not numeric — WeChat/iLink uses forms like
    ``wxid_abc123`` and ``<hex>@im.bot`` — so the digit-only filter in
    :func:`_coerce_str_ids` would silently drop every entry. With a
    deny-by-default ``dm_policy`` that would lock out every intended sender.

    Still fails closed on shape: a non-list yields ``[]``, and blank entries are
    dropped.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for u in raw:
        s = str(u).strip()
        if s and s not in out:
            out.append(s)
    return out


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


_GITLAB_HOST_NAME_RE = _re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


def _parse_telegram_accounts(raw: object) -> dict[str, "TelegramAccountConfig"]:
    """Parse the deprecated ``telegram.accounts`` map from raw config JSON.

    Parsing is retained so a config written by an earlier release round-trips
    through :meth:`KiroCrewConfig.save` with its tokens and allow-lists intact;
    no bot is started from the result. Each value is a dict with optional keys
    matching :class:`TelegramAccountConfig`. Invalid entries (non-dict values,
    missing bot_token) are skipped so a hand-edited config never crashes
    gateway startup.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, TelegramAccountConfig] = {}
    for account_id, acct_data in raw.items():
        if not isinstance(account_id, str) or not isinstance(acct_data, dict):
            continue
        # Account IDs are held to the same shape they were accepted under, so a
        # config that round-trips here is byte-comparable to what an earlier
        # release wrote: alphanumeric plus dash and underscore, never empty.
        if not account_id or not account_id.replace("-", "").replace("_", "").isalnum():
            continue
        token = str(acct_data.get("bot_token", "")).strip()
        if not token:
            continue
        out[account_id] = TelegramAccountConfig(
            bot_token=token,
            allowed_user_ids=_coerce_int_ids(acct_data.get("allowed_user_ids")),
            allow_forum=_safe_bool(acct_data.get("allow_forum"), False),
            allowed_forum_chat_ids=_coerce_int_ids(acct_data.get("allowed_forum_chat_ids")),
            soft_threshold_pct=_threshold_pct(acct_data.get("soft_threshold_pct"), 80),
        )
    return out


def _coerce_gitlab_hosts(raw: object) -> list[str]:
    """Coerce the self-hosted GitLab allowlist to clean ``host[:port]`` entries.

    Fails closed: a non-list yields ``[]``, and an entry is dropped unless it is
    a bare lowercase-normalized hostname with an optional numeric port. Anything
    carrying a scheme, userinfo, path, query, or wildcard is rejected rather than
    sanitized, so a hand-edited config cannot smuggle a different target past the
    exact-match check the source-provider handler performs.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        host = entry.strip().lower()
        if not host or len(host) > 255:
            continue
        # Split the optional port BEFORE stripping trailing dots: an absolute-FQDN
        # entry with a port ("gitlab.example.:8443") keeps its dot in the middle of
        # the string, so stripping the whole entry first would leave it there and
        # the URL API's "gitlab.example:8443" could never match.
        name, sep, port_text = host.rpartition(":")
        if not sep:
            name, port_text = host, ""
        name = name.rstrip(".")
        # Hostname-only pattern here: the permissive one allows a trailing port,
        # so validating `name` with it would let a malformed "host:8443:443"
        # entry (whose last colon is split off as the port) silently authorize
        # "host:8443".
        if not name or not _GITLAB_HOST_NAME_RE.fullmatch(name):
            continue
        if sep:
            # A colon was present, so a port MUST follow and it must be a plain
            # run of ASCII digits. Fail closed on anything else rather than
            # authorize a host the operator never wrote:
            #   * "gitlab.example:"      -> empty port; without this it would
            #     fall through to the portless branch and grant the bare host.
            #   * "gitlab.example:+443"  -> int("+443") == 443 silently coerces.
            #   * "gitlab.example:1_000" -> int("1_000") == 1000 (underscores).
            #   * " 443", fullwidth digits, "0x10" -> also coerce or pass isdigit.
            # str.isdigit() alone accepts non-ASCII digit codepoints, so pair it
            # with isascii(); an empty string returns False for both.
            if not (port_text.isascii() and port_text.isdigit()):
                continue
            port = int(port_text)
            if not 0 < port < 65536:
                continue
            # Rebuild the port canonically: a configured "08443" would otherwise
            # be stored verbatim while both the browser URL API and the backend
            # normalize the URL's port to "8443", so the entry could never match.
            # The default HTTPS port is dropped entirely, matching the URL API.
            host = name if port == 443 else f"{name}:{port}"
        else:
            host = name
        # gitlab.com is always accepted and must not need an allowlist entry.
        if host in {"gitlab.com", "www.gitlab.com"} or host in out:
            continue
        out.append(host)
    return out


def _coerce_jira_hosts(raw: object) -> list[str]:
    """Coerce the self-hosted Jira allowlist — identical rules to GitLab hosts."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        host = entry.strip().lower()
        if not host or len(host) > 255:
            continue
        name, sep, port_text = host.rpartition(":")
        if not sep:
            name, port_text = host, ""
        name = name.rstrip(".")
        if not name or not _GITLAB_HOST_NAME_RE.fullmatch(name):
            continue
        if sep:
            if not (port_text.isascii() and port_text.isdigit()):
                continue
            port = int(port_text)
            if not 0 < port < 65536:
                continue
            host = name if port == 443 else f"{name}:{port}"
        else:
            host = name
        if host in out:
            continue
        out.append(host)
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


#: Longest accepted channel session-folder name — matches the 100-char cap the
#: folder CRUD endpoint applies, so a name that round-trips through config can
#: never be longer than one created in the sidebar.
SESSION_FOLDER_NAME_MAX = 100


def _coerce_session_folder(raw: object) -> str:
    """Coerce a channel's ``session_folder`` value to a usable folder name.

    Empty string means the feature is off (the default) — sessions from the
    channel stay unfiled. Anything else is the name of the sidebar folder they
    are filed into. Non-strings, control characters, path separators, and
    over-long values all fail closed to off rather than producing a folder the
    user did not ask for: truncating an over-long hand-edited value would file
    conversations into a real folder whose name nobody chose, which is worse
    than leaving them where they already were.
    """
    if not isinstance(raw, str):
        return ""
    name = raw.strip()
    if len(name) > SESSION_FOLDER_NAME_MAX:
        return ""
    if any(ch in name for ch in ("/", "\\")) or any(ord(ch) < 0x20 for ch in name):
        return ""
    return name


@dataclass
class TelegramAccountConfig:
    """A single named Telegram bot account, retained only to preserve config.

    Deprecated and inert: nothing starts a bot from this entry. It stays
    parseable and serializable so that loading and saving a config written by an
    earlier release round-trips the operator's tokens and allow-lists instead of
    erasing them. To serve one of these bots, move its token to
    ``telegram.bot_token``.
    """

    bot_token: str = field(
        default="",
        metadata=_meta(
            "Bot Token",
            "Telegram Bot API token for this account.",
            tags=["telegram"],
            sensitive=True,
        ),
    )
    allowed_user_ids: list[int] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed User IDs",
            "Numeric Telegram user IDs permitted to DM this bot account.",
            tags=["telegram"],
        ),
    )
    allow_forum: bool = field(
        default=False,
        metadata=_meta(
            "Allow Forum Topics",
            "Serve forum Topics for this account.",
            tags=["telegram"],
        ),
    )
    allowed_forum_chat_ids: list[int] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Forum Chat IDs",
            "Supergroup chat_ids permitted for this account.",
            tags=["telegram"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "Prompt threshold for this account.",
            tags=["telegram"],
        ),
    )


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
    accounts: dict[str, TelegramAccountConfig] = field(
        default_factory=dict,
        metadata=_meta(
            "Accounts",
            "Deprecated and inert: named Telegram bot accounts no longer start a "
            "bot. Multi-bot operation is withdrawn until a bot is a governable "
            "unit (its own enable switch, its own posture ceiling, and honest "
            "audit attribution) rather than a second inbound door that only the "
            "global telegram.enabled can close. The map is still parsed and "
            "written back so an existing config keeps its tokens and allow-lists, "
            "but nothing reads it: move the token you want served to "
            "telegram.bot_token.",
            tags=["telegram"],
            deprecated=True,
        ),
    )
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
            tags=["telegram"],
        ),
    )

    def __post_init__(self) -> None:
        # Telegram carries only the soft nudge threshold; the hard-compaction
        # backstop is the backend autocompactor (session.autocompact_pct).
        self.soft_threshold_pct = _clamp_pct(self.soft_threshold_pct)


@dataclass
class WeixinConfig:
    """Weixin (personal WeChat) channel via Tencent's iLink Bot API.

    Distinct from :class:`WeComConfig` (enterprise WeCom over WebSocket). The
    bot ``token`` + ``account_id`` are obtained through the Settings > Channels
    QR-login flow; prefer the WEIXIN_TOKEN credential over storing the token
    here.
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Weixin (iLink personal WeChat) channel (long-polling). "
            "Requires a bot token + account id from the Settings QR flow.",
            tags=["weixin"],
        ),
    )
    token: str = field(
        default="",
        metadata=_meta(
            "Bot Token",
            "iLink bot token (from QR login). Prefer the WEIXIN_TOKEN credential "
            "(env/.env / cred store) over storing it here.",
            tags=["weixin"],
            sensitive=True,
        ),
    )
    account_id: str = field(
        default="",
        metadata=_meta(
            "Account ID",
            "iLink bot account id captured during QR login.",
            tags=["weixin"],
        ),
    )
    base_url: str = field(
        default="https://ilinkai.weixin.qq.com",
        metadata=_meta(
            "iLink Base URL",
            "iLink API base URL (per-account, returned by QR login).",
            tags=["weixin"],
        ),
    )
    dm_policy: str = field(
        default="allowlist",
        metadata=_meta(
            "DM Policy",
            "Who may DM the bot: 'allowlist' (only allowed_user_ids, the default), "
            "'open' (any sender), or 'disabled'. Defaults to allowlist with an empty "
            "list, so a freshly connected bot authorizes NOBODY until you add an id.",
            tags=["weixin"],
        ),
    )
    allowed_user_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed User IDs",
            "Weixin user ids permitted to DM the bot when dm_policy='allowlist'. "
            "Empty = deny all (fail closed).",
            tags=["weixin"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "Prompt the user to /compact or /new when context passes this percentage.",
            tags=["weixin"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context passes this percentage.",
            tags=["weixin"],
        ),
    )
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
            tags=["weixin"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable -- _maybe_notice
        # checks ``pct >= hard`` first. Mirrors WeComConfig.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
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
    allowed_channel_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Channel IDs",
            "Discord server channels where approved users may start a new agent thread.",
            tags=["discord"],
        ),
    )
    auto_thread: bool = field(
        default=True,
        metadata=_meta(
            "Auto-create Threads",
            "Create one Discord thread per approved message in an allowed channel.",
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
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
            tags=["discord"],
        ),
    )

    def __post_init__(self) -> None:
        # Discord carries only the soft nudge threshold; the hard-compaction
        # backstop is the backend autocompactor (session.autocompact_pct).
        self.soft_threshold_pct = _clamp_pct(self.soft_threshold_pct)


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
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
            tags=["webex"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable -- _maybe_notice
        # checks ``pct >= hard`` first. Mirrors WeComConfig.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )


@dataclass
class TeamsConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Microsoft Teams channel (self-hosted inbound HTTPS "
            "webhook via the Bot Framework). Requires a public HTTPS endpoint "
            "pointing at /api/messaging/teams plus MICROSOFT_APP_ID and "
            "MICROSOFT_APP_PASSWORD (env/.env) or teams.app_id/app_password.",
            tags=["teams"],
        ),
    )
    app_id: str = field(
        default="",
        metadata=_meta(
            "App ID",
            "Microsoft App (Client) ID of the Azure Bot registration. Prefer "
            "the MICROSOFT_APP_ID credential (env/.env) over storing it here.",
            tags=["teams"],
        ),
    )
    app_password: str = field(
        default="",
        metadata=_meta(
            "App Password",
            "Azure Bot client secret. Set ONLY via the MICROSOFT_APP_PASSWORD "
            "credential (env/.env); it is deliberately NOT read from config.json "
            "so the agent-readable config never holds the secret.",
            tags=["teams"],
            sensitive=True,
        ),
    )
    tenant_id: str = field(
        default="",
        metadata=_meta(
            "Tenant ID",
            "Azure AD tenant id for a single-tenant bot. Leave empty for a "
            "multi-tenant bot (uses the botframework.com token authority).",
            tags=["teams"],
        ),
    )
    allowed_emails: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Emails",
            "Azure AD UPNs/emails OR AAD object ids permitted to DM the bot. "
            "Teams activities reliably carry the sender's object id (email is "
            "often absent), so listing object ids works out of the box; emails "
            "are matched when Teams supplies them. Empty = deny all (fail "
            "closed): a Teams bot is reachable by anyone in the org.",
            tags=["teams"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a DM's context passes this, prompt the user to /compact or /new "
            "instead of auto-compacting.",
            tags=["teams"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this, even without a user "
            "decision, so the window never overflows.",
            tags=["teams"],
        ),
    )
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
            tags=["teams"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable. Mirrors
        # WebexConfig.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )


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
    session_summary: SessionSummaryConfig = field(
        default_factory=SessionSummaryConfig,
        metadata=_meta(
            "Session Summary",
            "Intent-level session summaries for the chat right panel. Off by default.",
        ),
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
    computer_use: ComputerUseConfig = field(
        default_factory=ComputerUseConfig,
        metadata=_meta(
            "Computer Use",
            "Desktop automation tree/screenshot budgets. The primary enable is NOT "
            "here — it lives on the keystone computer_use.json.",
        ),
    )
    mcp_gateway: McpGatewayConfig = field(
        default_factory=McpGatewayConfig,
        metadata=_meta("MCP Gateway", "Sidecar MCP broker that shares backends across sessions."),
    )
    instances: InstancesConfig = field(
        default_factory=InstancesConfig,
        metadata=_meta(
            "Instances", "Multi-instance management — manage/switch remote Kiro Crews over SSH."
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
    wecom: WeComConfig = field(
        default_factory=WeComConfig,
        metadata=_meta("WeCom", "WeCom (企业微信) AI-bot integration settings.", tags=["wecom"]),
    )
    telegram: TelegramConfig = field(
        default_factory=TelegramConfig,
        metadata=_meta("Telegram", "Telegram Bot API integration settings.", tags=["telegram"]),
    )
    weixin: WeixinConfig = field(
        default_factory=WeixinConfig,
        metadata=_meta(
            "WeChat", "Weixin (iLink personal WeChat) integration settings.", tags=["weixin"]
        ),
    )
    discord: DiscordConfig = field(
        default_factory=DiscordConfig,
        metadata=_meta("Discord", "Discord bot integration settings.", tags=["discord"]),
    )
    webex: WebexConfig = field(
        default_factory=WebexConfig,
        metadata=_meta("Webex", "Webex Messaging integration settings.", tags=["webex"]),
    )
    teams: TeamsConfig = field(
        default_factory=TeamsConfig,
        metadata=_meta("Teams", "Microsoft Teams integration settings.", tags=["teams"]),
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
        metadata=_meta("Agents", "Named Kiro Crew agent definitions."),
    )
    default_agent: str = field(
        default="",
        metadata=_meta("Default Agent", "Active Kiro Crew agent name from the agents section."),
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
            "Defaults to ~/.kiro/crew/snapshots if empty.",
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
        """Load config from ~/.kiro/crew/config.json, falling back to defaults.

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
        weixin_data = data.get("weixin", {})
        if not isinstance(weixin_data, dict):
            weixin_data = {}
        discord_data = data.get("discord", {})
        if not isinstance(discord_data, dict):
            discord_data = {}
        webex_data = data.get("webex", {})
        if not isinstance(webex_data, dict):
            webex_data = {}
        teams_data = data.get("teams", {})
        if not isinstance(teams_data, dict):
            teams_data = {}
        slack_data = data.get("slack", {})
        if not isinstance(slack_data, dict):
            slack_data = {}
        publish_data = data.get("publish", {})
        if not isinstance(publish_data, dict):
            publish_data = {}
        # Back-compat: this channel's config section was renamed
        # "wechat" -> "wecom". Fall back to the legacy key so existing
        # installs keep their WeCom settings on upgrade (read-only alias;
        # no broader migration machinery).
        wecom_data = data.get("wecom", data.get("wechat", {}))
        if not isinstance(wecom_data, dict):
            wecom_data = {}
        dashboard_data = data.get("dashboard", {})
        if not isinstance(dashboard_data, dict):
            dashboard_data = {}
        stt_data = data.get("stt", {})
        if not isinstance(stt_data, dict):
            stt_data = {}
        computer_use_data = data.get("computer_use", {})
        if not isinstance(computer_use_data, dict):
            computer_use_data = {}
        instances_data = data.get("instances", {})
        if not isinstance(instances_data, dict):
            instances_data = {}
        connect_timeout_raw = instances_data.get("connect_timeout_secs")
        mint_timeout_raw = instances_data.get("mint_timeout_secs")
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
        session_summary_data = data.get("session_summary", {})
        if not isinstance(session_summary_data, dict):
            session_summary_data = {}
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
                    # config.json is hand-editable (and agent-writable), so a
                    # non-string model (e.g. `model: 123`) must not survive the
                    # load — it would reach normalize_agent_model().strip() and
                    # raise AttributeError from the resolver instead of simply
                    # being ignored.
                    raw_model = entry.get("model", "")
                    # Same guard as model: a non-string triggers (e.g. `1`) must
                    # not survive load — select_crew's roster calls .strip() on it.
                    raw_triggers = entry.get("triggers", "")
                    agents[name] = KiroCrewAgentConfig(
                        kiro_agent=entry.get("kiro_agent", ""),
                        workspace=entry.get("workspace", "default"),
                        memory_store=entry.get("memory_store", "default"),
                        model=raw_model if isinstance(raw_model, str) else "",
                        description=entry.get("description", ""),
                        triggers=raw_triggers if isinstance(raw_triggers, str) else "",
                        source=entry.get("source", "kirocrew"),
                        # Same guard family as model/triggers: config.json is
                        # hand-editable, so a junk value must collapse to 0
                        # (inherit the global window), never crash the load.
                        # lo=0 keeps a negative override from arming an
                        # instant-cancel window.
                        watchdog_tool_stall_suspect_secs=_safe_float(
                            entry.get("watchdog_tool_stall_suspect_secs", 0.0), 0.0, lo=0.0
                        ),
                        watchdog_tool_stall_hard_cap_secs=_safe_float(
                            entry.get("watchdog_tool_stall_hard_cap_secs", 0.0), 0.0, lo=0.0
                        ),
                        telegram_account=entry.get("telegram_account", ""),
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
            k: v
            for k, v in data.items()
            if k not in _KNOWN_CONFIG_SECTIONS and k not in CONFIG_RESERVED_TOP_KEYS
        }

        cfg = cls(
            agent=AgentConfig(
                approval_mode=agent_data.get("approval_mode", "auto"),
                streaming=agent_data.get("streaming", True),
                model=agent_data.get("model", DEFAULT_MODEL),
                role_models=coerce_role_models(agent_data.get("role_models")),
                role_efforts=coerce_role_efforts(agent_data.get("role_efforts")),
                reasoning_effort=agent_data.get("reasoning_effort", ""),
                provider=agent_data.get("provider", "acp"),
                mcp_registry_mode=_safe_bool(agent_data.get("mcp_registry_mode", False), False),
                acp_backend=_normalize_acp_backend(agent_data.get("acp_backend")),
                default_agent=agent_data.get("default_agent", ""),
                sandbox=agent_data.get("sandbox", "auto"),
                sandbox_allow_no_isolation=bool(
                    agent_data.get("sandbox_allow_no_isolation", False)
                ),
                sandbox_allow_unsandboxed_exec=bool(
                    agent_data.get("sandbox_allow_unsandboxed_exec", False)
                ),
                apps_allow_third_party=_safe_bool(
                    agent_data.get("apps_allow_third_party", False), False
                ),
                apps_trusted=(
                    [a for a in _trusted if isinstance(a, str) and a]
                    if isinstance(_trusted := agent_data.get("apps_trusted"), list)
                    else []
                ),
                jail=_normalize_jail(agent_data.get("jail", "auto")),
                dangerously_skip_permissions=_read_skip_permissions(agent_data),
                yolo_duration=_normalize_yolo_duration(agent_data.get("yolo_duration")),
                notify_override_expiry=agent_data.get("notify_override_expiry", True),
                conductor_skill=agent_data.get("conductor_skill", False),
                tool_search=bool(agent_data.get("tool_search", True)),
                tool_search_min_pct=_safe_int(agent_data.get("tool_search_min_pct", 5), 5),
                tool_search_min_tokens=_safe_int(
                    agent_data.get("tool_search_min_tokens", 50000), 50000
                ),
                session_sharing=bool(agent_data.get("session_sharing", True)),
                max_subagents=_safe_int(
                    agent_data.get("max_subagents", 0), 0, 0, SUBAGENT_AUTO_MAX_CEILING
                ),
                max_stop_hook_nudges=_safe_int(
                    agent_data.get("max_stop_hook_nudges", 100), 100, 0
                ),
                subagent_mem_buffer_pct=_safe_int(
                    agent_data.get("subagent_mem_buffer_pct", 20), 20
                ),
                chat_turn_timeout_secs=_safe_int(
                    agent_data.get("chat_turn_timeout_secs", 7200),
                    7200,
                    CHAT_TURN_TIMEOUT_MIN,
                    CHAT_TURN_TIMEOUT_MAX,
                ),
                session_start_timeout_secs=_safe_int(
                    agent_data.get("session_start_timeout_secs", 90),
                    90,
                    SESSION_START_TIMEOUT_MIN,
                    SESSION_START_TIMEOUT_MAX,
                ),
                tool_approval_timeout_secs=_safe_int(
                    agent_data.get("tool_approval_timeout_secs", 600),
                    600,
                    TOOL_APPROVAL_TIMEOUT_MIN,
                    TOOL_APPROVAL_TIMEOUT_MAX,
                ),
                subagent_cost_gb=_safe_float(agent_data.get("subagent_cost_gb", 0.5), 0.5),
                subagent_cpu_cost_cores=_safe_float(
                    agent_data.get("subagent_cpu_cost_cores", 1.0), 1.0
                ),
                subagent_auto_max=_safe_int(
                    agent_data.get("subagent_auto_max", 32), 32, 3, SUBAGENT_AUTO_MAX_CEILING
                ),
                subagent_spawn_stagger_secs=_safe_float(
                    agent_data.get("subagent_spawn_stagger_secs", 2.0), 2.0
                ),
                spawn_min_memory_gb=_safe_float(agent_data.get("spawn_min_memory_gb", 4.0), 4.0),
                resource_pressure_gb=_safe_float(agent_data.get("resource_pressure_gb", 4.0), 4.0),
                resource_critical_gb=_safe_float(agent_data.get("resource_critical_gb", 2.0), 2.0),
                admission_gate=_safe_bool(agent_data.get("admission_gate"), True),
                subagent_max_turns=_safe_int(
                    agent_data.get("subagent_max_turns", 100), 100, 1, SUBAGENT_MAX_TURNS_CEILING
                ),
                subagent_timeout_secs=agent_data.get("subagent_timeout_secs", 1800),
                subagent_stall_idle_secs=_safe_int(
                    agent_data.get("subagent_stall_idle_secs", 120), 120
                ),
                completion_keep=_validated_completion_keep(
                    agent_data.get("completion_keep", "head")
                ),
                completion_keep_chars=_safe_int(
                    agent_data.get("completion_keep_chars", 3000), 3000
                ),
                subagent_result_ttl_secs=_safe_int(
                    agent_data.get("subagent_result_ttl_secs", 3600), 3600
                ),
                workflow_run_timeout_secs=_safe_int(
                    agent_data.get("workflow_run_timeout_secs", 3600), 3600
                ),
                subagent_cwd_allowed_roots=(
                    [r for r in _roots if isinstance(r, str)]
                    if isinstance(_roots := agent_data.get("subagent_cwd_allowed_roots"), list)
                    else list(DEFAULT_CWD_ALLOWED_ROOTS)
                ),
                log_level=(
                    lvl.upper()
                    if isinstance(lvl := agent_data.get("log_level", "WARNING"), str)
                    else "WARNING"
                ),
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
                autocompact_pct=_safe_float(
                    session_data.get("autocompact_pct", 90.0),
                    90.0,
                    lo=AUTOCOMPACT_PCT_MIN,
                    hi=AUTOCOMPACT_PCT_MAX,
                ),
                pool_size=_safe_int(session_data.get("pool_size", 2), 2, 0, POOL_SIZE_MAX),
                pool_agent=str(session_data.get("pool_agent", "")),
                pool_ttl_secs=_safe_int(session_data.get("pool_ttl_secs", 1800), 1800),
                eager_spawn=bool(session_data.get("eager_spawn", True)),
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
                cron_max_index_records=_safe_int(
                    cron_history_data.get("cron_max_index_records", 2000), 2000
                ),
            ),
            messaging=MessagingConfig(
                use_transport=bool(messaging_data.get("use_transport", True)),
                dm_scope=str(messaging_data.get("dm_scope", "per-channel-peer")),
                idle_reset_minutes=_coerce_int(messaging_data.get("idle_reset_minutes"), 0),
                daily_reset_hour=_coerce_int(messaging_data.get("daily_reset_hour"), -1),
                queue_mode=str(messaging_data.get("queue_mode", "steer")),
            ),
            # orchestrator/watchdog are advertised in config-baseline.json,
            # served by /api/config/schema, and read by real consumers
            # (acp/session_handle.py, dashboard/chat_orchestrator.py), so load()
            # passes these kwargs — without them config.json values would be
            # silently ignored and the dataclass defaults would always win.
            orchestrator=OrchestratorConfig(
                stage_timeout_seconds=_safe_int(
                    orchestrator_data.get("stage_timeout_seconds", 1800), 1800
                ),
            ),
            watchdog=WatchdogConfig(
                check_after_secs=_safe_float(watchdog_data.get("check_after_secs", 60.0), 60.0),
                stale_window_secs=_safe_float(watchdog_data.get("stale_window_secs", 300.0), 300.0),
                tool_stall_suspect_secs=_safe_float(
                    watchdog_data.get("tool_stall_suspect_secs", 3600.0), 3600.0
                ),
                tool_stall_hard_cap_secs=_safe_float(
                    watchdog_data.get("tool_stall_hard_cap_secs", 3600.0), 3600.0
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
                beacon_enabled=bool(telemetry_data.get("beacon_enabled", True)),
                beacon_endpoint=str(
                    telemetry_data.get("beacon_endpoint", _DEFAULT_BEACON_ENDPOINT)
                ),
            ),
            memory=MemoryConfig(
                embedding_provider=_coerce_embedding_provider(
                    memory_data.get("embedding_provider", "llama_cpp")
                ),
                embedding_dim=memory_data.get("embedding_dim", 1024),
                embedding_threads=_safe_int(memory_data.get("embedding_threads", 4), 4, 1, 256),
                embed_model_url=memory_data.get("embed_model_url", ""),
                embed_model_path=memory_data.get("embed_model_path", ""),
                embed_model_id=memory_data.get("embed_model_id", ""),
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
                auto_ingest_artifacts=bool(knowledge_data.get("auto_ingest_artifacts", False)),
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
                embed_timeout_secs=_safe_float(
                    knowledge_data.get("embed_timeout_secs", 10.0), 10.0
                ),
                embed_content_budget=_safe_int(knowledge_data.get("embed_content_budget", 0), 0),
                pool_idle_ttl_secs=_safe_nonnegative_int(
                    knowledge_data.get("pool_idle_ttl_secs", 300),
                    300,
                ),
                auto_add_documents=_read_auto_add_documents(knowledge_data),
                auto_register_project_docs=bool(
                    knowledge_data.get("auto_register_project_docs", False)
                ),
                auto_ingest_chunk_budget=_safe_nonnegative_int(
                    knowledge_data.get("auto_ingest_chunk_budget", 150), 150
                ),
                folder_ingest_chunk_budget=_safe_nonnegative_int(
                    knowledge_data.get("folder_ingest_chunk_budget", 300), 300
                ),
                dedup_every_n_sweeps=_safe_nonnegative_int(
                    knowledge_data.get("dedup_every_n_sweeps", 12), 12
                ),
                doc_ingest_hosts=[
                    str(h)
                    for h in knowledge_data.get("doc_ingest_hosts", [])
                    if isinstance(h, str) and h.strip()
                ],
                auto_discover_folder=bool(knowledge_data.get("auto_discover_folder", False)),
                auto_discover_dirname=str(
                    knowledge_data.get("auto_discover_dirname", "knowledge-docs")
                ).strip()[:128],
                sweep_chunk_budget=_safe_nonnegative_int(
                    knowledge_data.get("sweep_chunk_budget", 500), 500
                ),
                max_sources=_safe_nonnegative_int(knowledge_data.get("max_sources", 50), 50),
                embed_rate_limit=_safe_nonnegative_int(
                    knowledge_data.get("embed_rate_limit", 120), 120
                ),
                extraction_model=str(knowledge_data.get("extraction_model", "")).strip(),
                extraction_pool_size=max(
                    1,
                    min(
                        10, _safe_nonnegative_int(knowledge_data.get("extraction_pool_size", 3), 3)
                    ),
                ),
            ),
            telegram=TelegramConfig(
                session_folder=_coerce_session_folder(telegram_data.get("session_folder")),
                enabled=bool(telegram_data.get("enabled", False)),
                bot_token=str(telegram_data.get("bot_token", "")),
                allowed_user_ids=_coerce_int_ids(telegram_data.get("allowed_user_ids")),
                soft_threshold_pct=_threshold_pct(telegram_data.get("soft_threshold_pct"), 80),
                allow_forum=bool(telegram_data.get("allow_forum", False)),
                allowed_forum_chat_ids=_coerce_int_ids(telegram_data.get("allowed_forum_chat_ids")),
                accounts=_parse_telegram_accounts(telegram_data.get("accounts")),
            ),
            weixin=WeixinConfig(
                session_folder=_coerce_session_folder(weixin_data.get("session_folder")),
                enabled=bool(weixin_data.get("enabled", False)),
                token=str(weixin_data.get("token", "")),
                account_id=str(weixin_data.get("account_id", "")),
                base_url=str(weixin_data.get("base_url", "") or "https://ilinkai.weixin.qq.com"),
                dm_policy=str(weixin_data.get("dm_policy", "allowlist") or "allowlist"),
                allowed_user_ids=_coerce_opaque_str_ids(weixin_data.get("allowed_user_ids")),
                soft_threshold_pct=_threshold_pct(weixin_data.get("soft_threshold_pct"), 80),
                hard_threshold_pct=_threshold_pct(weixin_data.get("hard_threshold_pct"), 95),
            ),
            discord=DiscordConfig(
                session_folder=_coerce_session_folder(discord_data.get("session_folder")),
                enabled=bool(discord_data.get("enabled", False)),
                bot_token=str(discord_data.get("bot_token", "")),
                # Discord user IDs are numeric snowflakes that exceed 2^53 —
                # keep them as strings (JSON round-trip safe, matches the
                # transport's string comparison).
                allowed_user_ids=_coerce_str_ids(discord_data.get("allowed_user_ids")),
                allowed_thread_ids=_coerce_str_ids(discord_data.get("allowed_thread_ids")),
                allowed_channel_ids=_coerce_str_ids(discord_data.get("allowed_channel_ids")),
                auto_thread=bool(discord_data.get("auto_thread", True)),
                soft_threshold_pct=_threshold_pct(discord_data.get("soft_threshold_pct"), 80),
            ),
            webex=WebexConfig(
                session_folder=_coerce_session_folder(webex_data.get("session_folder")),
                enabled=bool(webex_data.get("enabled", False)),
                bot_token=str(webex_data.get("bot_token", "")),
                allowed_emails=(
                    [e for e in webex_data.get("allowed_emails", []) if isinstance(e, str) and e]
                    if isinstance(webex_data.get("allowed_emails", []), list)
                    else []
                ),
                soft_threshold_pct=_threshold_pct(webex_data.get("soft_threshold_pct"), 80),
                hard_threshold_pct=_threshold_pct(webex_data.get("hard_threshold_pct"), 95),
            ),
            teams=TeamsConfig(
                session_folder=_coerce_session_folder(teams_data.get("session_folder")),
                enabled=bool(teams_data.get("enabled", False)),
                app_id=str(teams_data.get("app_id", "")),
                # Secret is env-only (MICROSOFT_APP_PASSWORD). Never sourced from
                # config.json, which the agent can read — keeps the Azure Bot
                # credential out of any agent-readable file.
                app_password="",
                tenant_id=str(teams_data.get("tenant_id", "")),
                allowed_emails=(
                    [e for e in teams_data.get("allowed_emails", []) if isinstance(e, str) and e]
                    if isinstance(teams_data.get("allowed_emails", []), list)
                    else []
                ),
                soft_threshold_pct=_threshold_pct(teams_data.get("soft_threshold_pct"), 80),
                hard_threshold_pct=_threshold_pct(teams_data.get("hard_threshold_pct"), 95),
            ),
            slack=SlackConfig(
                session_folder=_coerce_session_folder(slack_data.get("session_folder")),
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
                trusted_bot_ids={
                    b for b in _safe_list(slack_data.get("trusted_bot_ids")) if isinstance(b, str)
                },
                allowed_enterprise_ids=[
                    e
                    for e in slack_data.get("allowed_enterprise_ids", [])
                    if isinstance(e, str) and (e.startswith("E") or e.startswith("T"))
                ],
                reactions={
                    k: v
                    for k, v in _safe_dict(slack_data.get("reactions")).items()
                    if isinstance(k, str) and (v is None or (isinstance(v, str) and v))
                },
                reactions_enabled=bool(slack_data.get("reactions_enabled", True)),
                use_tunnel_url=bool(slack_data.get("use_tunnel_url", False)),
                show_thinking=bool(slack_data.get("show_thinking", True)),
                home_tab_sessions_per_kind=_safe_int(
                    slack_data.get("home_tab_sessions_per_kind", 5), 5
                ),
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
            wecom=WeComConfig(
                session_folder=_coerce_session_folder(wecom_data.get("session_folder")),
                enabled=bool(wecom_data.get("enabled", False)),
                allowed_users=[
                    u
                    for u in wecom_data.get("allowed_users", [])
                    if isinstance(u, dict) and u.get("userid")
                ],
                allow_all_users=bool(wecom_data.get("allow_all_users", False)),
                ws_url=str(wecom_data.get("ws_url", "wss://openws.work.weixin.qq.com")),
                soft_threshold_pct=_threshold_pct(wecom_data.get("soft_threshold_pct"), 80),
                hard_threshold_pct=_threshold_pct(wecom_data.get("hard_threshold_pct"), 95),
            ),
            dashboard=DashboardConfig(
                url=dashboard_data.get("url", ""),
                tailscale=_tailscale_config_from(dashboard_data.get("tailscale")),
                restore_sessions=dashboard_data.get("restore_sessions", False),
                restore_window_minutes=dashboard_data.get("restore_window_minutes", 30),
                surface_channel_sessions=dashboard_data.get("surface_channel_sessions", True),
                bot_name=dashboard_data.get("bot_name", ""),
                avatar=dashboard_data.get("avatar", ""),
                merge_queued_messages=dashboard_data.get("merge_queued_messages", False),
                mcp_probe_timeout_secs=_safe_int(
                    dashboard_data.get("mcp_probe_timeout_secs", 15), 15
                ),
                loop_stall_exit_after_secs=_safe_int(
                    dashboard_data.get("loop_stall_exit_after_secs", 25),
                    25,
                    LOOP_STALL_EXIT_AFTER_MIN,
                    LOOP_STALL_EXIT_AFTER_MAX,
                ),
                cautious_boot=_safe_bool(dashboard_data.get("cautious_boot"), True),
                auto_open_browser=dashboard_data.get("auto_open_browser", True),
                prevent_sleep=_safe_bool(dashboard_data.get("prevent_sleep"), False),
                quick_send=dashboard_data.get("quick_send", False),
                session_grid=dashboard_data.get("session_grid", False),
                mcp_app_panel=dashboard_data.get("mcp_app_panel", False),
                auto_open_git_panel=_safe_bool(dashboard_data.get("auto_open_git_panel"), False),
                widget_density=dashboard_data.get("widget_density", "more"),
                use_builtin_browser=_safe_bool(dashboard_data.get("use_builtin_browser"), True),
                verbosity=dashboard_data.get("verbosity", "default"),
                link_previews=_safe_bool(dashboard_data.get("link_previews"), False),
                usage_text_scrape_enabled=_safe_bool(
                    dashboard_data.get("usage_text_scrape_enabled"), False
                ),
                tail_fork_enabled=dashboard_data.get("tail_fork_enabled", False),
                terminal=dashboard_data.get("terminal", {"enabled": True}),
                default_project=dashboard_data.get("default_project", ""),
                theme_mode=dashboard_data.get("theme_mode", ""),
                sso_login_flags=str(dashboard_data.get("sso_login_flags", "")),
                theme_color=dashboard_data.get("theme_color", ""),
                language=str(dashboard_data.get("language", "")),
                recent_tint_count=_safe_int(dashboard_data.get("recent_tint_count", 0), 0),
                onboarded=bool(dashboard_data.get("onboarded", False)),
                import_onboarded=_safe_bool(
                    dashboard_data.get("import_onboarded"),
                    _safe_bool(dashboard_data.get("onboarded"), False),
                ),
                # Falls back to `onboarded`: a user who finished first run before
                # this chapter existed has already reached the product, and
                # re-gating their heartbeat on a screen they will never be shown
                # would suppress it forever.
                privacy_acked=_safe_bool(
                    dashboard_data.get("privacy_acked"),
                    _safe_bool(dashboard_data.get("onboarded"), False),
                ),
                user_role=str(dashboard_data.get("user_role", "")),
                user_role_other=str(dashboard_data.get("user_role_other", "")),
                user_technical_level=str(dashboard_data.get("user_technical_level", "")),
                tips_enabled=bool(dashboard_data.get("tips_enabled", True)),
                folder_suggestions_enabled=bool(
                    dashboard_data.get("folder_suggestions_enabled", True)
                ),
                tips_cadence_hours=_safe_float(
                    dashboard_data.get("tips_cadence_hours", 6.0), 6.0, lo=0.0
                ),
                tips_snooze_hours=_safe_float(
                    dashboard_data.get("tips_snooze_hours", 48.0), 48.0, lo=0.0
                ),
                tips_recency_decay=_safe_float(
                    dashboard_data.get("tips_recency_decay", 0.6), 0.6, lo=0.0, hi=1.0
                ),
                tips_model=str(dashboard_data.get("tips_model", "auto")),
                tips_explore_ratio=_safe_float(
                    dashboard_data.get("tips_explore_ratio", 0.2), 0.2, lo=0.0, hi=1.0
                ),
                gitlab_hosts=_coerce_gitlab_hosts(dashboard_data.get("gitlab_hosts")),
                jira_hosts=_coerce_jira_hosts(dashboard_data.get("jira_hosts")),
                jira_auth=[
                    JiraAuthEntry(
                        host=str(entry.get("host", "")),
                        email=str(entry.get("email", "")),
                    )
                    for entry in (dashboard_data.get("jira_auth") or [])
                    if isinstance(entry, dict) and entry.get("host")
                ],
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
                # Default "turbo" — faster and recommended for most users
                # (809M vs 74M, but much better latency).
                model=stt_data.get("model", "turbo"),
                mlx_model=stt_data.get("mlx_model", "mlx-community/whisper-large-v3-turbo"),
                parakeet_model=stt_data.get(
                    "parakeet_model", "mlx-community/parakeet-tdt-0.6b-v3"
                ),
                device=stt_data.get("device", "cpu"),
                timeout_secs=stt_data.get("timeout_secs", 300),
                transcribe_region=stt_data.get("transcribe_region", "us-east-1"),
                transcribe_profile=stt_data.get("transcribe_profile", ""),
                language_code=stt_data.get("language_code", "en-US"),
                streaming=stt_data.get("streaming", False),
                endpointing=_safe_bool(stt_data.get("endpointing"), False),
                dictation_panel=_safe_bool(stt_data.get("dictation_panel"), True),
            ),
            # Every numeric knob is clamped to the same ceiling the MCP tool
            # schemas enforce, so a hand-edited config.json cannot ask for an
            # unbounded accessibility walk or a full-resolution screenshot.
            # There is deliberately NO ``enabled`` key read here — see
            # ComputerUseConfig's docstring and computer_use_state_path().
            computer_use=ComputerUseConfig(
                max_tree_nodes=min(
                    _CU_MAX_TREE_NODES,
                    max(
                        1,
                        _safe_int(
                            computer_use_data.get("max_tree_nodes", _CU_DEFAULT_MAX_TREE_NODES),
                            _CU_DEFAULT_MAX_TREE_NODES,
                        ),
                    ),
                ),
                max_tree_depth=min(
                    _CU_MAX_TREE_DEPTH,
                    max(
                        1,
                        _safe_int(
                            computer_use_data.get("max_tree_depth", _CU_DEFAULT_MAX_TREE_DEPTH),
                            _CU_DEFAULT_MAX_TREE_DEPTH,
                        ),
                    ),
                ),
                text_limit=min(
                    _CU_MAX_TEXT_LIMIT,
                    max(
                        1,
                        _safe_int(
                            computer_use_data.get("text_limit", _CU_DEFAULT_TEXT_LIMIT),
                            _CU_DEFAULT_TEXT_LIMIT,
                        ),
                    ),
                ),
                attach_screenshot=_safe_bool(
                    computer_use_data.get("attach_screenshot", _CU_DEFAULT_ATTACH_SCREENSHOT),
                    _CU_DEFAULT_ATTACH_SCREENSHOT,
                ),
                screenshot_max_px=min(
                    _CU_MAX_SCREENSHOT_MAX_PX,
                    max(
                        _CU_MIN_SCREENSHOT_MAX_PX,
                        _safe_int(
                            computer_use_data.get(
                                "screenshot_max_px", _CU_DEFAULT_SCREENSHOT_MAX_PX
                            ),
                            _CU_DEFAULT_SCREENSHOT_MAX_PX,
                        ),
                    ),
                ),
                screenshot_jpeg_quality=min(
                    100,
                    max(
                        1,
                        _safe_int(
                            computer_use_data.get(
                                "screenshot_jpeg_quality", _CU_DEFAULT_SCREENSHOT_JPEG_QUALITY
                            ),
                            _CU_DEFAULT_SCREENSHOT_JPEG_QUALITY,
                        ),
                    ),
                ),
                # Default False: a missing or unparseable value must mean "do not
                # draw on the operator's screen", never the reverse.
                cursor_motion=_safe_bool(computer_use_data.get("cursor_motion", False), False),
            ),
            auto_update=data.get("auto_update", True),
            timezone=data.get("timezone", ""),
            snapshot_dir=data.get("snapshot_dir", ""),
            registries=[
                ExternalRegistryConfig(
                    name=str(r.get("name", "")),
                    repo=str(r.get("repo", "")),
                    # Backward-compat: an entry that OMITS ``branch`` is a legacy
                    # config written before URL registries defaulted new entries
                    # to ``main`` (the registries PUT API now always persists an
                    # explicit branch). Such an entry relied on the historical
                    # ``mainline`` default, so preserve it here — silently
                    # retargeting it to ``main`` on upgrade would break any
                    # registry whose content still lives on ``mainline``.
                    branch=str(r.get("branch", "mainline")),
                    # A credential-posture decision, so it is read back verbatim
                    # and validated downstream rather than here: an unrecognised
                    # value must resolve to the restrictive tier, which
                    # ``registry._registry_trust_tier`` does. Absent -> "index",
                    # so a config written before the field existed keeps the
                    # credential-free posture it had.
                    trust=str(r.get("trust", "index")),
                )
                for r in (data.get("registries") or [])
                if isinstance(r, dict) and r.get("repo")
            ],
            mcp_gateway=McpGatewayConfig(
                enabled=bool(mcp_gateway_data.get("enabled", False)),
                # Absent -> True so installs that never configured this keep
                # rendering. A malformed value cannot be distinguished here: the
                # schema validator REMOVES an invalid value before the loader
                # parses (see config/validation.py ``_apply_field_default``), so a
                # hand-edited ``"false"`` arrives as absent and resolves to True,
                # with a warning logged naming the field. ``_safe_bool`` is
                # belt-and-braces for a schema gap, not the acting guard — the
                # acting guard against a truthy string is the validator, since
                # ``bool("false")`` is True. The write path is where an opt-out is
                # actually enforced: the endpoint rejects any non-boolean body.
                apps_enabled=_safe_bool(mcp_gateway_data.get("apps_enabled", True), True),
                # ON by default. The forwarded set is a strict subset of the
                # hashed set and gatewayd re-hashes the sidecar at spawn,
                # forwarding nothing on mismatch, so a forwarded key is one every
                # co-tenant of that backend declared identically. With it off, one
                # ordinary declared key costs the whole server its pooling.
                #
                # Both arguments are True on purpose. A malformed value never
                # reaches this call: ``config.validation`` type-checks first and
                # ``_apply_field_default`` strips a non-boolean so the dataclass
                # default applies, which is why the log says "using default". The
                # fallback here is defence in depth for a bypassed validator, and
                # giving it a different answer than the schema would only put two
                # disagreeing defaults in the file.
                forward_declared_env=_safe_bool(
                    mcp_gateway_data.get(
                        "forward_declared_env", FORWARD_DECLARED_ENV_DEFAULT
                    ),
                    FORWARD_DECLARED_ENV_DEFAULT,
                ),
                socket_path=str(mcp_gateway_data.get("socket_path", "")),
                overlay_dir=str(mcp_gateway_data.get("overlay_dir", "")),
                idle_timeout_secs=max(
                    10, _safe_int(mcp_gateway_data.get("idle_timeout_secs", 300), 300)
                ),
                # 0 is meaningful (re-resolve every pass), so the floor is 0 and
                # not the usual "at least something" clamp.
                resolve_once_refresh_hours=max(
                    0, _safe_int(mcp_gateway_data.get("resolve_once_refresh_hours", 24), 24)
                ),
                max_backends=max(1, _safe_int(mcp_gateway_data.get("max_backends", 64), 64)),
                poolable_servers=[
                    s for s in mcp_gateway_data.get("poolable_servers", []) if isinstance(s, str)
                ],
                stub_servers=_resolve_stub_servers(mcp_gateway_data),
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
                connect_timeout_secs=(
                    _safe_float(connect_timeout_raw, _DEFAULT_CONNECT_TIMEOUT)
                    if connect_timeout_raw is not None
                    else None
                ),
                mint_timeout_secs=(
                    _safe_float(mint_timeout_raw, _DEFAULT_MINT_TIMEOUT)
                    if mint_timeout_raw is not None
                    else None
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
                max_triggered=_safe_int(skills_data.get("max_triggered", 0), 0),
                lazy_load=bool(skills_data.get("lazy_load", False)),
                auto_create_from_sessions=bool(skills_data.get("auto_create_from_sessions", False)),
                auto_refine_on_deviation=bool(skills_data.get("auto_refine_on_deviation", False)),
                auto_min_tool_calls=_safe_int(skills_data.get("auto_min_tool_calls", 5), 5),
                auto_similarity_threshold=_safe_float(
                    skills_data.get("auto_similarity_threshold", 0.85), 0.85
                ),
                approval_required=bool(skills_data.get("approval_required", True)),
                max_auto_skills=_safe_int(skills_data.get("max_auto_skills", 100), 100),
                stale_after_days=_safe_int(skills_data.get("stale_after_days", 30), 30),
                archive_after_days=_safe_int(skills_data.get("archive_after_days", 90), 90),
                pending_ttl_days=_safe_int(skills_data.get("pending_ttl_days", 30), 30),
                generate_scripts=bool(skills_data.get("generate_scripts", True)),
                judge_model=str(skills_data.get("judge_model", "auto") or "auto"),
                extra_paths=[
                    p for p in _safe_list(skills_data.get("extra_paths")) if isinstance(p, str)
                ],
            ),
            session_summary=SessionSummaryConfig(
                enabled=bool(session_summary_data.get("enabled", False)),
                min_user_turns=_safe_int(
                    session_summary_data.get("min_user_turns", 2), 2),
                regenerate_after_turns=_safe_int(
                    session_summary_data.get("regenerate_after_turns", 1), 1),
                max_intents=_safe_int(
                    session_summary_data.get("max_intents", 8), 8),
                max_constraints=_safe_int(
                    session_summary_data.get("max_constraints", 5), 5),
                assistant_excerpt_chars=_safe_int(
                    session_summary_data.get("assistant_excerpt_chars", 400), 400),
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
                _write_migration_backup(path)
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
            "wecom": asdict(self.wecom),
            "weixin": asdict(self.weixin),
            "teams": asdict(self.teams),
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
            "computer_use": asdict(self.computer_use),
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
            "session_summary": asdict(self.session_summary),
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
        """Write current config to ~/.kiro/crew/config.json.

        Stamps a ``meta`` block with the current version and timestamp
        so we can tell which build last touched the file.

        Values that exist in ``config.local.json`` are stripped from the
        output to prevent overlay settings from leaking into the base file.
        """

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

        # Atomic + mode-preserving: a concurrent reader must never observe a
        # half-written config, and the write must not widen who can read a file
        # that may hold inline credentials. See write_config_atomically.
        write_config_atomically(config_path(), stamp_config_meta(d))
        # Drop the validated-data cache so the next load() re-reads this write.
        # mtime-keying already detects the change; this makes it immediate even
        # if the filesystem mtime resolution is coarse.
        _invalidate_config_cache()

    @staticmethod
    def _resolve_agent_model() -> str:
        """Read model from installed agent config, falling back to bundled defaults."""
        # Installed agent config (generated by kirocrew setup)
        agent_json = kiro_agents_dir() / "kirocrew.json"
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
        """Load credentials from ~/.kiro/crew/.env and environment variables.

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

            # Warn once per boot about keys not in the recognised allowlist.
            # These keys still propagate (operators use them for proxy/feature
            # settings), but the warning makes the behavior visible rather than
            # silently surprising.  The encrypted vault (PR 1+) will provide a
            # proper agent-isolated path for secrets.
            unknown = set(creds) - set(_CREDENTIAL_KEYS) - _warned_env_keys
            if unknown:
                _warned_env_keys.update(unknown)
                for uk in sorted(unknown):
                    logger.warning(
                        "Unknown key %s in .env is not a recognised credential"
                        " -- it will propagate to child processes but is NOT"
                        " agent-isolated. Recognised keys: %s",
                        uk,
                        ", ".join(sorted(_CREDENTIAL_KEYS)),
                    )

        for key in _CREDENTIAL_KEYS:
            val = os.environ.get(key)
            if val:
                creds[key] = val

        # Propagate credentials into the process environment so spawned children
        # (sandboxed agents, MCP servers, cron-fired subprocesses) inherit them
        # via Popen's default env=os.environ.copy() — even when their view of
        # ~/.kiro/crew/.env is a bind-mounted empty file. setdefault() preserves
        # any value the caller already set explicitly.
        #
        # EXCEPTION: when the Docker entrypoint has deliberately scrubbed
        # credentials from the process environ (setting _KIROCREW_CREDS_SCRUBBED=1),
        # re-injecting them here would leak into /proc/<pid>/environ — the exact
        # attack surface the entrypoint closed. The scrub covers only credential
        # keys, so the skip is scoped to _CREDENTIAL_KEYS: every other .env entry
        # (operator-added settings such as proxy or feature variables) still
        # propagates so children behave identically in and out of Docker.
        # Children that need the withheld credentials get them via their own
        # .env read or via an explicit env= kwarg on Popen (the sandbox and ACP
        # spawners already do this).
        scrubbed = bool(os.environ.get("_KIROCREW_CREDS_SCRUBBED"))
        for k, v in creds.items():
            if not v:
                continue
            if scrubbed and (
                k in _CREDENTIAL_KEYS or _JIRA_TOKEN_RE.match(k)
            ):
                continue
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
        tool_search_min_pct = self.agent.tool_search_min_pct
        tool_search_min_tokens = self.agent.tool_search_min_tokens
        # Global default effort for new sessions. A per-slot override always
        # wins; this only fills in when the slot carries none, so a session that
        # has never touched the effort control still starts at the user's
        # configured default instead of the provider/model default.
        default_effort = self.agent.reasoning_effort

        # MCP gateway: resolve overlay + socket once, iff some server is stubbed
        # through the gateway. Routing is what puts a stub in the path, and the
        # stub is what carries both the render/callback path and any sharing —
        # so an empty stub set means no stub, no daemon, and no gateway in the
        # path at all (AcpClient falls through to per-session MCP). Sharing
        # (``enabled``) is not consulted here: it decides how a stubbed server's
        # backend is ACQUIRED, and on its own routes nothing.
        _gw = self.mcp_gateway
        if _gw.stub_servers:
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
            crew_agent: str | None = None,
            **_kwargs: object,
        ) -> AcpProvider:
            wdir = Path(cwd) if cwd else _session_work_dir(session_key)
            # Canonical crew identity for the session (keys per-agent watchdog
            # windows on the handle) — one shared resolution rule, see
            # resolve_crew_identity.
            crew_agent = resolve_crew_identity(self, agent, crew_agent)
            # Resolve the model, highest tier first:
            #   1. model_override — the caller's explicit pick. The dashboard
            #      passes the slot's own model, else the KiroCrew agent's
            #      configured default (see chat_runner._run_chat).
            #   2. the bound kiro agent's own pinned model, for a named agent.
            #      Custom agents MUST resolve here because the ACP
            #      session/set_mode path switches prompt/tools but not the model,
            #      so an unset model makes kiro fall back to cli.json's
            #      chat.defaultModel. Use _resolve_named_agent_model (the kiro
            #      model slot) to match this backend.
            #   3. ``model`` — the global agent.model default, already collapsed
            #      through _resolve_agent_model() at factory-build time. It
            #      applies to every agent, not just "kirocrew": an agent that
            #      pins nothing inherits the user's configured default instead of
            #      silently falling through to the backend's own choice.
            # "" at the end means nothing is pinned anywhere; AcpClient
            # normalizes "" to DEFAULT_MODEL, same as None.
            if model_override:
                m = model_override
            elif not agent or agent == "kirocrew":
                m = model
            else:
                m = self._resolve_named_agent_model(agent) or model
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
            # Role-aware effort default: background worker agents (lite /
            # heartbeat) resolve the "background" role effort; everything else
            # uses the chat default. An explicit override (the dashboard slot's
            # effort, or a sub-agent's resolved "subagent" effort) still wins.
            if agent in ("kirocrew-lite", "kirocrew-heartbeat"):
                base_effort = self.agent.resolve_effort("background")
            else:
                base_effort = default_effort
            _eff = reasoning_effort_override or base_effort
            if m and _eff and is_valid_effort(_eff) and model_supports_effort(m):
                _eff_per_model[m] = _eff
            return AcpProvider(
                work_dir=wdir,
                model=m,
                agent=agent,
                crew_agent=crew_agent,
                sandbox_mode=sandbox,
                session_key=session_key,
                channel_id=channel_id,
                extra_env=extra_env,
                acp_backend=self.agent.acp_backend,
                effort_per_model=_eff_per_model,
                tool_search=tool_search,
                tool_search_min_pct=tool_search_min_pct,
                tool_search_min_tokens=tool_search_min_tokens,
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


_MATERIALIZED_AGENTS: frozenset[str] = frozenset()
_MATERIALIZED_AGENTS_READY = False
# Bumped by every publish. A refresh samples it before scanning and, if it moved
# while the scan was in flight, unions instead of replacing — otherwise a scan
# that globbed the directory BEFORE a registration wrote into it would assign its
# stale view over the just-published names and un-dispatch a freshly enabled app.
_MATERIALIZED_AGENTS_GENERATION = 0
# Monotonic refresh sequencing. A refresh takes a ticket when it STARTS and, on
# completion, discards its result if a refresh that started later already applied:
# two scans race by completion order, not by start order, so an older scan
# finishing second would otherwise overwrite a newer one and resurrect an agent
# that was deleted in between.
_MATERIALIZED_REFRESH_ISSUED = 0
_MATERIALIZED_REFRESH_APPLIED = 0
# Guards the three globals above. Held only for the rebind, never for the scan or
# for a lookup: the read path stays lock-free, which is the whole point of the
# snapshot.
_MATERIALIZED_AGENTS_LOCK = threading.Lock()


def _scan_materialized_agents(agents_dir: Path) -> frozenset[str]:
    """Every agent name declared by the kiro agent configs in *agents_dir*.

    Both spellings are emitted: the config's ``name`` field and the filename stem
    (mirroring :meth:`_resolve_named_agent_model`), since an app's agent is
    registered under a namespaced filename while its config keeps the app's bare
    name. Unreadable or non-object entries are skipped. Performs the glob and the
    per-file reads, so callers must invoke it OFF the event loop.
    """
    names: set[str] = set()
    # Deferred import: `hooks` reaches back into this module for config paths, so
    # the edge must resolve lazily. A failure here propagates to
    # refresh_materialized_agents, which logs and leaves the snapshot untouched —
    # fail-closed, rather than falling back to an unguarded read.
    from kiro_crew.hooks import safe_read_file

    try:
        candidates = sorted(agents_dir.glob("*.json"))
    except OSError:
        return frozenset()
    for af in candidates:
        try:
            # Through the sensitive-path gate, not a bare read: this directory is
            # user-writable, so a symlink planted there (`evil.json` ->
            # `~/.aws/credentials`) would otherwise be read verbatim by a boot
            # refresh. safe_read_file re-checks the RESOLVED target and raises
            # PermissionError for a refused path — an OSError subclass, so a
            # refused entry is skipped by the same handler as an unreadable one.
            data = json.loads(safe_read_file(str(af)))
        except (ValueError, OSError):
            continue
        # Skip stray non-object JSON a user may have dropped in the dir. The
        # filename stem is only trusted AFTER the file parses as an agent config:
        # naming an unparseable file dispatchable would hand kiro-cli a name it
        # cannot load, and it would fall back to its own default silently — the
        # same invisible mismatch this whole change removes.
        if not isinstance(data, dict):
            continue
        # Trust the config's DECLARED `name`, not the filename. `kiro-cli agent
        # list` enumerates agents by their declared name — an app agent written to
        # `mochi--mochi.json` with `"name": "mochi"` is listed as `mochi`, and
        # `mochi--mochi` is not listed at all. Treating the stem as dispatchable
        # would hand kiro-cli a name it does not know, which falls back to its own
        # default silently: the exact invisible mismatch this change removes. The
        # stem is used ONLY when the config declares no name, where it is the only
        # identifier available.
        declared = data.get("name")
        if isinstance(declared, str) and declared:
            names.add(declared)
        else:
            names.add(af.stem)
    return frozenset(names)


def refresh_materialized_agents() -> None:
    """Rescan the kiro agents directory into the in-memory snapshot.

    MUST be called off the event loop — it globs a directory and reads every
    config in it, which scales with agent count. Callers on the loop must use
    :func:`schedule_materialized_agents_refresh` instead.

    Placing the cost on the WRITER is the point: the read path
    (:func:`_materialized_kiro_agent`, reached from ``_run_chat`` ->
    :func:`resolve_agent_bindings` on every turn of an app-bound session) then
    does zero filesystem work. Never raises.

    Consequence worth stating plainly: editing an existing config IN PLACE — say
    renaming its ``name`` field by hand — refreshes nothing, so that new name
    stays undispatchable until the next registration or gateway boot. Hand-editing
    is not how an app agent is meant to appear (``_register_agents`` is), and the
    alternative is filesystem work on the loop, so the staleness is accepted
    rather than papered over with a per-file stat.
    """
    global _MATERIALIZED_AGENTS, _MATERIALIZED_AGENTS_READY, _MATERIALIZED_REFRESH_ISSUED
    global _MATERIALIZED_REFRESH_APPLIED
    with _MATERIALIZED_AGENTS_LOCK:
        generation_at_start = _MATERIALIZED_AGENTS_GENERATION
        _MATERIALIZED_REFRESH_ISSUED += 1
        my_ticket = _MATERIALIZED_REFRESH_ISSUED
    try:
        snapshot = _scan_materialized_agents(kiro_agents_dir())
    except Exception:  # noqa: BLE001 — a refresh failure only costs a fallback
        logger.debug("Failed to refresh materialized agent names", exc_info=True)
        return
    with _MATERIALIZED_AGENTS_LOCK:
        if my_ticket < _MATERIALIZED_REFRESH_APPLIED:
            # A refresh that started AFTER this one already applied, so this view
            # is older than what is installed. Assigning it would undo the newer
            # scan — resurrecting an agent deleted in between, whose config is gone
            # from disk. Drop it; the newer snapshot already reflects reality.
            logger.debug("Discarding out-of-order materialized agent refresh")
            return
        if _MATERIALIZED_AGENTS_GENERATION != generation_at_start:
            # A registration published while this scan was in flight, so the scan
            # may have globbed the directory before that write landed. Replacing
            # would erase the published names and un-dispatch a freshly enabled
            # app; union instead and let the refresh scheduled by that
            # registration apply the authoritative view (including removals).
            snapshot = frozenset(snapshot | _MATERIALIZED_AGENTS)
        _MATERIALIZED_AGENTS = snapshot
        _MATERIALIZED_AGENTS_READY = True
        _MATERIALIZED_REFRESH_APPLIED = my_ticket


def publish_materialized_agents(names: Iterable[str]) -> None:
    """Add *names* to the snapshot immediately, with no filesystem access.

    A pure set union — safe to call from anywhere, including the event loop.
    ``apps.bridges._register_agents`` uses it to publish the agents it just wrote
    BEFORE scheduling the full rescan, because the rescan can be delayed
    arbitrarily when the default executor is saturated, and the window is not
    merely cosmetic: a slot created in it is normalized to the agent that answers
    (the default) and that substitution is STORED, so the slot would stay bound to
    the default agent rather than recovering on the next turn.

    The snapshot is marked ready, which is safe in both contexts: on the loop the
    scheduled rescan fills in everything else moments later, and in a synchronous
    context the scheduler rescans inline, so the union is immediately superseded
    by a complete snapshot.
    """
    global _MATERIALIZED_AGENTS, _MATERIALIZED_AGENTS_READY, _MATERIALIZED_AGENTS_GENERATION
    fresh = {n for n in names if isinstance(n, str) and n}
    if not fresh:
        return
    with _MATERIALIZED_AGENTS_LOCK:
        _MATERIALIZED_AGENTS = frozenset(_MATERIALIZED_AGENTS | fresh)
        _MATERIALIZED_AGENTS_READY = True
        # Signals any in-flight refresh that its view predates this write, so it
        # unions rather than replacing (see refresh_materialized_agents).
        _MATERIALIZED_AGENTS_GENERATION += 1


def schedule_materialized_agents_refresh() -> None:
    """Refresh the snapshot from ANY context without blocking an event loop.

    ``apps.bridges._register_agents`` is the writer that must trigger this. The
    dashboard enable/update handlers dispatch ``register_app`` to an executor
    thread, so from those paths this runs in a synchronous context (no running
    loop) and refreshes inline — the scan is already off the loop, serialized
    inside the awaited registration, so no stale-snapshot window exists there.
    The same inline branch covers the CLI, tests, and the boot warm already on
    an executor. For a caller that does hold a live loop, scanning inline would
    be the same directory-walk-per-agent-file stall the neighbouring prune
    comment warns about, so the scan is handed to the default executor and this
    returns immediately; that offloaded refresh lands a few milliseconds later,
    and a turn dispatched in that window sees the previous snapshot for one
    turn, then self-heals — strictly better than staying stale until the next
    gateway boot. Never raises; the scan itself swallows its errors.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        refresh_materialized_agents()
        return
    try:
        # Fire-and-forget on purpose: nothing awaits this, and
        # refresh_materialized_agents never raises, so the discarded future
        # cannot surface an unretrieved exception.
        loop.run_in_executor(None, refresh_materialized_agents)
    except Exception:  # noqa: BLE001 — a scheduling failure only costs a fallback
        logger.debug("Failed to schedule materialized agent refresh", exc_info=True)


def _materialized_kiro_agent(agent_name: str | None, project_dir: str | None = None) -> str:
    """Return *agent_name* when a materialized kiro agent config declares it.

    An APP's agents are copied into ``~/.kiro/agents/`` by
    ``apps.bridges._register_agents`` under a namespaced FILENAME
    (``<app>--<agent>.json``) while the config inside keeps the app's own bare
    ``name``. Nothing adds them to ``config.agents`` — that mapping is authored
    by setup / the user — so an app agent is resolvable by kiro-cli but is NOT a
    KiroCrew alias. Without this lookup :func:`resolve_agent_bindings` would fall
    all the way back to ``default_agent`` and silently dispatch the DEFAULT kiro
    agent for a session the user explicitly bound to an app's agent: the slot
    still shows the requested name (it is stored verbatim, unvalidated), so the
    UI claims "mochi" while the default agent answers, without the app's MCP
    tools.

    A pure in-memory set membership test — NO filesystem I/O, not even a stat.
    This is reached from ``_run_chat`` -> :func:`resolve_agent_bindings` on EVERY
    turn of an app-bound session (an app agent is never an alias, so it always
    takes this path), and a scan there would stall chat, WebSocket and heartbeat
    processing. The snapshot is refreshed only off-loop, by the gateway at boot
    and by ``_register_agents`` / ``_deregister_agents`` around their writes (see
    :func:`refresh_materialized_agents`).

    CONTRACT, stated deliberately because it is wider than the bug it fixes: this
    honors ANY parseable agent config in the directory, not only app-registered
    ones, and grafts the DEFAULT agent's workspace and memory bindings onto it. An
    agent created by kiro-cli's own flow, or dropped in by hand, therefore becomes
    dispatchable with default bindings — it is not restricted to
    ``bridges._register_agents`` output. That is intentional: the directory is the
    kiro-cli agent registry, every entry in it is a real agent kiro-cli can load,
    and narrowing to app-registered names would mean tracking provenance the
    directory does not record. It is safe inside the single-user trust boundary,
    and reads go through the sensitive-path gate (see
    :func:`_scan_materialized_agents`), but it IS a wider surface than "app agents
    dispatch" and should be read as such.

    When no snapshot exists yet, one is built lazily ONLY in a synchronous
    context (the CLI, tests) — never while an event loop is running, where an
    unwarmed lookup falls back to the default rather than block. Returns ``""``
    for a blank name or when nothing declares it, so a genuinely unknown agent
    still falls back to the default.

    *project_dir* adds the session's own ``<project>/.kiro/agents`` scope, which
    kiro-cli searches BEFORE the user-level directory (it resolves ``--agent``
    against its cwd, and Kiro Crew spawns it with the project dir as cwd). It
    deliberately does NOT use the snapshot: that is one process-wide set, while the
    project scope differs per session, so sharing it would leak one checkout's
    agents into another's.

    The project lookup reads the filesystem, so like the user-level scan it is
    NEVER performed on the event loop — see :func:`_project_declares_agent`. Callers
    that need a project agent resolved must therefore invoke this off the loop;
    ``chat_runner`` and the side-turn handler do so through the discovery pool. An
    on-loop call degrades to the default agent for that turn rather than stalling
    the gateway, which is the same trade the user-level scope already makes.
    """
    if not agent_name:
        return ""
    if _MATERIALIZED_AGENTS_READY and agent_name in _MATERIALIZED_AGENTS:
        return agent_name
    if not _MATERIALIZED_AGENTS_READY:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop on this thread: scanning here blocks nothing.
            refresh_materialized_agents()
            if agent_name in _MATERIALIZED_AGENTS:
                return agent_name
        else:
            # On the event loop with a cold snapshot: never scan. The boot warm
            # normally precedes any turn; falling back for one turn is strictly
            # preferable to stalling the gateway.
            logger.debug("Materialized agent snapshot cold on the event loop; falling back")
    if project_dir and _project_declares_agent(agent_name, project_dir):
        return agent_name
    return ""


def _project_declares_agent(agent_name: str, project_dir: str) -> bool:
    """Whether *project_dir* declares a dispatchable agent called *agent_name*.

    Delegates to ``agent_discovery``, which owns the scan, its sensitive-path guards,
    and the stat-signature cache.

    Splits on whether an event loop is running, because that decides what is safe:

    * **Off the loop** (the CLI, tests, and the discovery-pool thread the dashboard
      call sites use) — scan and revalidate normally.
    * **On the loop** — read the cache and nothing else, via a helper that makes no
      syscalls whatsoever. Even one directory's worth of reads is unbounded in
      LATENCY, and this runs on EVERY turn of a project-agent-bound session, so a
      network or otherwise slow checkout would become a recurring gateway stall the
      loop-stall watchdog blames on chat. A cold cache reports "not declared" and the
      caller falls back, exactly as the user-level cold-snapshot path does.

    The dashboard call sites warm the cache through the discovery pool immediately
    before resolving, so the on-loop read is a hit rather than a fallback. Only the
    WARM is offloaded, never ``resolve_agent_bindings`` itself: that function can
    raise ``StopIteration`` on a malformed config, and ``StopIteration`` cannot be
    delivered through a ``Future`` — asyncio rejects it, and the awaiting caller
    hangs instead of seeing the error.

    Deferred import so this module keeps its leaf-level import graph. Best-effort — a
    lookup failure means "not declared here", never an exception into turn handling.
    """
    try:
        # circular import: agent_discovery imports kiro_crew.hooks (the hardened
        # file-read gate), whose import closure reaches back into config.loader —
        # verified by importing agent_discovery in a fresh interpreter and finding
        # kiro_crew.config.loader in sys.modules. A module-scope import here would
        # therefore be a cycle; the deferral is load-bearing, not stylistic.
        from kiro_crew.agent_discovery import (
            cached_project_agent_names,
            project_agent_names,
        )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return agent_name in project_agent_names(project_dir)
        names = cached_project_agent_names(project_dir)
        if names is None:
            logger.debug(
                "Project agent cache cold on the event loop for %r; falling back "
                "(warm it off-loop before resolving to dispatch a project agent)",
                agent_name,
            )
            return False
        return agent_name in names
    except Exception:  # noqa: BLE001 — a probe failure only costs a fallback
        logger.debug("Project agent probe failed for %r", agent_name, exc_info=True)
        return False


def resolve_crew_identity(
    config: "KiroCrewConfig", agent: str | None, crew_agent: str | None
) -> str:
    """Canonical Kiro Crew identity (a ``config.agents`` key) for a session.

    One rule shared by every session-granting path (provider factory, warm-pool
    claim) so cold starts and claims can never disagree. An explicit
    ``crew_agent`` wins verbatim — including "" ("no crew"), which is how the
    dashboard, the one kiro-name-passing surface, opts out of the fallback.
    When absent, the surface convention documented on
    :func:`_resolve_model_for_agent` applies: Slack threads, cron jobs and
    spawned agents pass a CREW name as ``agent``, so crew-namespace membership
    makes it canonical — a membership check on names the surface owns, not a
    cross-namespace match.
    """
    if crew_agent is not None:
        return crew_agent
    if agent and agent in config.agents:
        # DEBUG, not INFO: every Slack/cron session resolves here routinely.
        # The line exists so a kiro-template name that collides with a crew
        # key (which would silently inherit that crew's watchdog windows) is
        # diagnosable from logs.
        logger.debug(
            "crew_agent %r resolved by crew-namespace fallback", agent
        )
        return agent
    return ""


def resolve_agent_bindings(
    config: KiroCrewConfig,
    agent_name: str | None = None,
    project_dir: str | None = None,
) -> ResolvedBindings:
    """Resolve workspace, memory store, and kiro agent for a session.

    Resolution:
    1. If agent_name is given and exists in config.agents → use its bindings
    2. Otherwise use config.default_agent (guaranteed to exist by load()), but
       keep dispatching *agent_name* itself when a materialized kiro agent
       declares it (see :func:`_materialized_kiro_agent`) — an app's agents are
       registered in ``~/.kiro/agents/`` and never added to ``config.agents``, so
       this is the only thing that stops an app-bound session from silently
       running the default agent.

    *project_dir* is the session's active project directory, which widens step 2 to
    that project's own ``.kiro`` scope. It must be the same directory Kiro Crew
    passes as the kiro-cli cwd, so an agent found through it is one the backend
    will genuinely resolve; passing a directory the session does not run in would
    reintroduce the silent-substitution bug this lookup exists to prevent.
    """
    import dataclasses as _dc

    # An app agent is resolvable by kiro-cli but is not a KiroCrew alias, so it
    # takes the default's workspace/memory bindings while still dispatching
    # ITSELF. Computed only when the name is not an alias — the lookup touches
    # the filesystem.
    alias_hit = bool(agent_name) and agent_name in config.agents
    passthrough = "" if alias_hit else _materialized_kiro_agent(agent_name, project_dir)
    # A non-empty name that matched NEITHER an alias nor a materialized config is
    # about to be answered by the default agent. Reported so callers that store
    # the requested name never advertise a binding that is not running.
    requested_resolved = (not agent_name) or alias_hit or bool(passthrough)

    # Step 1: explicit agent_name
    if agent_name and agent_name in config.agents:
        agent_cfg = config.agents[agent_name]
        resolved_alias = agent_name
    elif config.default_agent and config.default_agent in config.agents:
        # Step 2: default_agent (guaranteed valid by load())
        agent_cfg = config.agents[config.default_agent]
        resolved_alias = config.default_agent
    elif config.agents:
        # Defensive: default_agent not in agents, use first available
        first_name = next(iter(config.agents))
        logger.warning(
            "default_agent '%s' not found in agents, using '%s'",
            config.default_agent,
            first_name,
        )
        agent_cfg = config.agents[first_name]
        resolved_alias = first_name
    else:
        # No agents at all — return safe defaults
        logger.warning("No agents configured, using bare defaults")
        return ResolvedBindings(
            workspace_dir=Path("workspace"),
            memory_store_name=config.default_memory_store,
            effective_memory_config=_dc.asdict(config.memory),
            kiro_agent=passthrough or config.agent.default_agent,
            requested_resolved=requested_resolved,
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

    kiro_agent = passthrough or agent_cfg.kiro_agent

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
        model=normalize_agent_model(agent_cfg.model),
        requested_resolved=requested_resolved,
        resolved_alias=resolved_alias,
    )


def resolve_effective_model(
    config: KiroCrewConfig,
    agent_name: str | None = None,
) -> str:
    """Return the model a new session on *agent_name* would start with.

    Single source of truth for the default-model precedence, so the display
    path (the dashboard's model chip) and the execution path
    (``create_provider_factory._acp``) cannot drift apart. Tiers, highest first:

    1. the KiroCrew agent's own ``model``
    2. the bound kiro agent's pinned ``model`` (skipped for the built-in
       ``kirocrew`` agent, which tracks the global by design)
    3. the global ``agent.model`` default
    4. the installed ``kirocrew.json`` / bundled ``defaults.json`` model

    A per-session pick outranks all of these and is NOT considered here — the
    caller holds it. Returns ``""`` when every tier defers, meaning the backend
    picks (kiro-cli's own ``chat.defaultModel``).
    """
    bindings = resolve_agent_bindings(config, agent_name)
    if bindings.model:
        return bindings.model

    kiro_agent = bindings.kiro_agent
    if kiro_agent and kiro_agent != "kirocrew":
        pinned = normalize_agent_model(config._resolve_named_agent_model(kiro_agent))
        if pinned:
            return pinned

    configured = normalize_agent_model(config.agent.model)
    if configured:
        return configured
    # agent.model is "auto"/unset: fall through to the installed agent file the
    # factory would read, so the chip shows what will actually be used.
    return normalize_agent_model(config._resolve_agent_model())


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
