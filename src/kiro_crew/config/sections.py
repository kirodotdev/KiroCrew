"""Configuration section DTOs, defaults, and field-level coercion.

The loader imports and re-exports this module's names as its compatibility
facade.  Keep this module one-way: it must not import the loader, schema, or
validation modules.
"""

from __future__ import annotations

import logging
import math
import re as _re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit as _urlsplit

from kiro_crew import model_registry

# Leaf module (stdlib only) owning "which ACP backend can this build serve": the
# registry an edition extends at boot. Importable at module scope precisely because
# it does NOT reach ``kiro_crew.acp`` — the package init (client + runtime) imports
# config models, which is the cycle the old ``acp.types`` import had to defer for.
#
# The one gate stays inside ``_normalize_acp_backend`` on the way out of
# config.json. Only what it reads changed: the registry, instead of a frozen
# literal.
from kiro_crew.acp_backends import resolve_selected_backend
from kiro_crew.computer_use.types import DEFAULT_ATTACH_SCREENSHOT as _CU_DEFAULT_ATTACH_SCREENSHOT
from kiro_crew.computer_use.types import DEFAULT_MAX_TREE_DEPTH as _CU_DEFAULT_MAX_TREE_DEPTH
from kiro_crew.computer_use.types import DEFAULT_MAX_TREE_NODES as _CU_DEFAULT_MAX_TREE_NODES
from kiro_crew.computer_use.types import (
    DEFAULT_SCREENSHOT_JPEG_QUALITY as _CU_DEFAULT_SCREENSHOT_JPEG_QUALITY,
)
from kiro_crew.computer_use.types import DEFAULT_SCREENSHOT_MAX_PX as _CU_DEFAULT_SCREENSHOT_MAX_PX
from kiro_crew.computer_use.types import DEFAULT_TEXT_LIMIT as _CU_DEFAULT_TEXT_LIMIT
from kiro_crew.config.resolution import _OBSERVED_DEGRADED_SECTIONS, DEGRADED_TAILSCALE
from kiro_crew.effort import EFFORT_LEVELS, is_valid_effort
from kiro_crew.instances.constants import CONNECT_TIMEOUT_CEILING_SECS as _CONNECT_TIMEOUT_CEILING
from kiro_crew.instances.constants import DEFAULT_MAX_RECOVERY_ATTEMPTS as _DEFAULT_MAX_RECOVERY
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
from kiro_crew.instances.constants import WARM_SET_CAP_AUTO as _WARM_SET_CAP_AUTO
from kiro_crew.stt.limits import DEFAULT_IDLE_EVICT_SECS as _STT_DEFAULT_IDLE_EVICT_SECS
from kiro_crew.stt.limits import DEFAULT_PARTIAL_INTERVAL_MS as _STT_DEFAULT_PARTIAL_INTERVAL_MS
from kiro_crew.stt.limits import DEFAULT_SILENCE_MS as _STT_DEFAULT_SILENCE_MS
from kiro_crew.stt.models import CATALOG as _STT_CATALOG
from kiro_crew.stt.models import DEFAULT_MODEL as _STT_DEFAULT_MODEL
from kiro_crew.stt.models import resolve as _resolve_stt_model

logger = logging.getLogger("kiro_crew.config.loader")


DEFAULT_MODEL = "auto"
DEFAULT_SESSION_TIMEOUT = 3600  # 60 min
# Auto-compaction threshold, as a percentage of the context window. Named
# because two code paths need it — the dataclass field default (used only when
# there is no config file) and the dict-load fallback in ``load()`` (used when
# a config file omits the key). Restating the number in both lets them disagree
# with nothing on disk to show it, which is why ``pool_size`` is named the same
# way (``DEFAULT_POOL_SIZE``) rather than written twice.
DEFAULT_AUTOCOMPACT_PCT = 70.0
# Margin BELOW the configured compaction threshold at which the "context is
# getting large" warning fires. A margin rather than an absolute percentage
# because both consumers test compaction FIRST in an if/elif chain
# (``session.check_context_usage`` and the ``cli_chat`` REPL loop), so an
# absolute warn level at or above the configured threshold makes the warning arm
# unreachable and the early signal disappears for whoever did not change the
# default. Kept here rather than in either consumer so the two cannot drift.
#
# 10 points, so the warning carries one fixed meaning — "within 10 points of
# compaction" — whatever threshold the operator configures. Width is what makes
# the signal readable: at 20 the warning covers the top 20 of the 70 usable
# points on the default threshold and fires on every turn from half the context
# window onward, which is where an always-on warning stops being read.
# ``test_the_warning_stays_a_minority_of_the_usable_range`` holds the band under
# a quarter of the range so it cannot widen back into noise.
CONTEXT_WARN_MARGIN_PCT = 10.0
# session.pool_size — warm pool OFF by default. Each pooled slot is a full
# kiro-cli process plus the MCP stdio servers its agent spec spawns (~109 MB per
# backend), and a non-zero value is also reserved out of the memory term that
# sizes the subagent cap (subagent.compute_max_subagents), so the cost is paid on
# every host whether or not the pool is ever claimed. Cold start is instead
# hidden by session.eager_spawn, which is on by default and pre-creates a slot's
# session behind user think-time.
#
# Read by BOTH the SessionConfig field default and load()'s file-parse fallback,
# because those are two independent paths to the same value: a home with no
# config.json takes the field default, and a config.json that omits the key takes
# the parse fallback. A literal in either place lets the two disagree, which is
# invisible on disk — this constant is the only place the value is written.
DEFAULT_POOL_SIZE = 0
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

# The kiro agents that run the "background" role: auto-titles, memory
# consolidation, heartbeat polls. Named here rather than inline at the one place
# that branched on them, because the effort chain is now read by two callers (the
# provider factory and the crews API's readout) and a second copy of the pair
# would let them disagree about which agents take the role default.
BACKGROUND_WORKER_AGENTS: tuple[str, ...] = ("kirocrew-lite", "kirocrew-heartbeat")


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


def coerce_effort(raw: object) -> str:
    """Normalize ONE reasoning-effort value to a level, or ``""`` for inherit.

    The single-value counterpart of :func:`coerce_role_efforts`, for the crew
    pin (``agents.<name>.reasoning_effort``). ``config.json`` is hand-editable,
    so anything that is not a concrete level collapses to ``""`` — inherit the
    tier below — rather than reaching the provider as a level kiro-cli would
    reject. The API validates instead of coercing, so a caller that sends a
    typo is told; only the file-load path silently falls back.
    """
    if isinstance(raw, str):
        val = raw.strip()
        if val and is_valid_effort(val):
            return val
    return ""


def coerce_fallback_model(raw: object) -> str:
    """Normalize the throttle-fallback model (agent.fallback_model).

    Single value with three shapes: ``"auto"`` (the default — defer to the
    backend's availability-aware routing when the active model stays
    throttled), ``""`` (feature explicitly disabled: fail loudly, pre-feature
    behavior), or a concrete model id normalized through
    :func:`model_registry.to_provider_id` for the ``acp`` provider (registry
    canonical keys and aliases land as the kiro-cli id the wire needs;
    unregistered ids pass through unchanged — existing registry behavior).
    Absent/junk input (``None``, non-string) collapses to the ``"auto"``
    default. ``"auto"`` is matched case-insensitively; an unregistered id that
    the registry maps to ``""`` also collapses to ``"auto"`` rather than
    silently disabling the feature.
    """
    if raw is None or not isinstance(raw, str):
        return "auto"
    s = raw.strip()
    if not s:
        return ""
    if s.lower() == "auto":
        return "auto"
    return model_registry.to_provider_id(s, "acp") or "auto"


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


def _safe_nonnegative_int(value: object, default: int, hi: int | None = None) -> int:
    """Convert a legacy integer value and reject negative results.

    *hi* caps the result. Deliberately a ceiling only, with no matching floor
    argument: a negative value still returns *default* rather than clamping up to
    0, because 0 is MEANINGFUL for the budgets this guards (a zero chunk budget
    turns that sweep off). Clamping -1 to 0 would silently disable a sweep the
    operator never asked to disable, where returning the default keeps it running.
    The ceiling has no such ambiguity, and it is where the exposure was: an absurd
    hand-edited budget loaded verbatim and became real scheduled work.
    """
    result = _safe_int(value, default)
    if result < 0:
        return default
    return result if hi is None else min(hi, result)


def _port_or_unset(value: object) -> int:
    """A TCP port, or 0 (unset) when the value is malformed or out of range.

    Deliberately NOT the clamp convention used for bounded knobs: a clamped
    port is as wrong as a malformed one — a tunnel that forwards 8080 does not
    forward 65535 either — so anything outside 1..65535 falls back to unset
    (ephemeral) rather than becoming a live pin the operator never named.
    """
    result = _safe_int(value, 0)
    return result if 0 < result <= 65535 else 0


#: Bounds of a context-threshold percentage, and the single statement of the range.
#: The floor is 1, not 0, because a 0% threshold means "always over" and would fire the
#: notice/compaction on every turn. Public because the dashboard's channel-config
#: handlers validate an inbound percentage against exactly this range, and a validator
#: that restated the numbers would drift from what the loader will actually accept.
THRESHOLD_PCT_MIN = 1
THRESHOLD_PCT_MAX = 100


def _clamp_pct(value: int) -> int:
    """Clamp an integer context-threshold percentage to the shared range."""
    return max(THRESHOLD_PCT_MIN, min(THRESHOLD_PCT_MAX, value))


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


#: Outbound services the iMessage bridge accepts. Anything else is a typo that
#: would be rejected per send rather than at load time. Shared with the settings
#: API so the form's choices and the loader's clamp cannot drift apart.
IMESSAGE_SERVICES = frozenset(("imessage", "sms", "auto"))


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


def _resolve_stub_roster(mcp_gateway_data: dict) -> list[str]:
    """The stub set as CONFIGURED, before the operator's own deviations.

    This is the layer a distribution owns: an edition that wants its known
    servers stubbed out of the box ships them here, and keeps shipping them as
    the roster grows. Operator deviations live in ``stub_overrides`` and are
    applied over this by :func:`_resolve_stub_servers` — which is what lets the
    two move independently. Read this directly ONLY to answer "what does the
    roster say"; everything that wants the set actually in effect wants
    :func:`_resolve_stub_servers`.

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


def _resolve_stub_overrides(mcp_gateway_data: dict) -> dict[str, bool]:
    """The operator's per-server stub DECISIONS — what they changed, not the result.

    Sparse by construction, and that is the whole point. A flat resulting list
    can only be REPLACED: an operator who unstubs one server out of a shipped
    roster would have to restate the survivors, and that restated list then
    shadows the roster permanently — the next name the distribution adds never
    reaches them, because their file already answers the question. Recording the
    DECISION instead leaves every server they did not speak about following the
    roster.

    Absent means "no opinion", which is why a key whose value equals the roster's
    answer is pruned on write rather than stored: an override that agrees with
    its base is indistinguishable from silence in effect, but not in future —
    stored, it would freeze that server against a later roster change, which is
    the shadowing this map exists to avoid.

    Non-bool values are dropped rather than coerced. A truthy string here would
    be an operator's typo, and guessing which way they meant it is worse than
    leaving that server on the roster's answer.
    """
    raw = _safe_dict(mcp_gateway_data.get("stub_overrides"))
    return {
        name: value
        for name, value in raw.items()
        if isinstance(name, str) and name and isinstance(value, bool)
    }


def _resolve_stub_servers(mcp_gateway_data: dict) -> list[str]:
    """Which MCP servers are given a stub, roster and operator decisions together.

    The set in EFFECT: :func:`_resolve_stub_roster` supplies the configured base
    and :func:`_resolve_stub_overrides` the operator's deviations from it, so a
    distribution can grow the roster without overwriting a choice the operator
    made, and the operator can turn any single server off without pinning
    themselves to today's roster.

    Roster order is preserved (the resolver has always handed back what the file
    held, duplicates included, and ``_freeze_stub_servers`` is what normalizes on
    write); servers added by an override are appended in sorted order, because
    they have no position in the file to preserve.
    """
    roster = _resolve_stub_roster(mcp_gateway_data)
    overrides = _resolve_stub_overrides(mcp_gateway_data)
    if not overrides:
        return roster
    resolved = [name for name in roster if overrides.get(name, True)]
    already = set(resolved)
    resolved.extend(name for name, on in sorted(overrides.items()) if on and name not in already)
    return resolved


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


_COLOR_HEX_RE = _re.compile(r"^#[0-9a-fA-F]{6}$")


def _safe_color(value: object) -> str:
    """Return a valid lowercase ``#rrggbb`` hex color, or ``""`` on junk.

    config.json is hand-editable, so a non-string or malformed value must
    collapse to empty (no agent color) rather than crash the load or propagate
    to an inline CSS style attribute.
    """
    if not isinstance(value, str) or not value:
        return ""
    v = value.strip().lower()
    if _COLOR_HEX_RE.match(v):
        return v
    return ""


#: String-valued ghost trait axes accepted in a per-crew avatar override.
_AVATAR_GHOST_STR_TRAITS = ("eyes", "brows", "mouth", "accessory", "prop")
#: Boolean-valued ghost trait axes.
_AVATAR_GHOST_BOOL_TRAITS = ("blush", "flip")
#: Cap on a single trait value, so hand-written junk cannot bloat config.json.
_AVATAR_TRAIT_MAX_LEN = 32
#: Formats an uploaded crew picture may be stored in. Shared with the avatar
#: endpoints: the config's ``file`` pin and the files on disk speak this set.
_AVATAR_IMAGE_EXTS = ("png", "jpg", "webp")
#: The config's committed-picture pin: ``<16-hex content digest>.<ext>``.
#: Each install lands at a digest-named path that never collides with the
#: currently committed file, so nothing overwrites a committed picture before
#: the config save that commits its replacement.
_AVATAR_FILE_PIN_RE = _re.compile(r"^[0-9a-f]{16}\.(?:png|jpg|webp)$")


def _safe_avatar(value: object) -> dict:
    """Return a validated per-crew avatar override, or ``{}`` on junk.

    Accepted shapes:

    - ``{"kind": "ghost", "traits": {...}}`` — pins the ghost face
      trait-by-trait instead of deriving it from the crew name.
    - ``{"kind": "image"}`` (optional int ``v``, optional ``file``) — the crew
      wears an uploaded picture, served from ``GET /api/agents/{name}/avatar``.
      The file itself lives under the data home's agent-fenced
      ``run/avatars/`` dir; the config
      field only marks the choice. ``v`` is the upload's cache-busting stamp
      (file mtime, nanoseconds): the frontend appends it as ``?v=`` so a
      replaced picture is re-fetched without waiting out the browser cache.
      ``file`` pins the exact committed file — a ``<digest>.<ext>`` suffix
      under the crew's stem. Every install lands at a digest-named path, so a
      replacement never overwrites the committed file before the config save
      commits it, and serving resolves only the pinned file.

    Empty means "no override" — the frontend keeps rendering the name-seeded
    face. config.json is hand-editable (and agent-writable), so junk collapses
    to ``{}`` rather than crashing the load.

    Trait *values* are deliberately not checked against the frontend's trait
    vocabulary: the renderer resolves an unknown option to "absent"
    (``EYES[k] ?? ''``), and keeping the vocabulary in one place (the style
    module) means a new hat needs no backend release. ``tile`` is the one
    exception — it is interpolated into SVG markup, so it is pinned to a hex
    color by the same validator session_color uses.
    """
    if not isinstance(value, dict):
        return {}
    if value.get("kind") == "image":
        out: dict[str, object] = {"kind": "image"}
        v = value.get("v")
        # bool is an int subclass; a hand-written `"v": true` must not pass.
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            out["v"] = v
        f = value.get("file")
        if isinstance(f, str) and _AVATAR_FILE_PIN_RE.fullmatch(f):
            out["file"] = f
        return out
    if value.get("kind") != "ghost":
        return {}
    raw = value.get("traits")
    if not isinstance(raw, dict):
        return {}
    traits: dict[str, object] = {}
    for key in _AVATAR_GHOST_STR_TRAITS:
        v = raw.get(key, "")
        traits[key] = v[:_AVATAR_TRAIT_MAX_LEN] if isinstance(v, str) else ""
    for key in _AVATAR_GHOST_BOOL_TRAITS:
        # `is True`, not bool(): config.json is hand-editable and
        # bool("false") is True, so a string-typed value would render the
        # opposite of what its author wrote. Only a real boolean counts.
        traits[key] = raw.get(key, False) is True
    traits["tile"] = _safe_color(raw.get("tile", ""))
    # An all-empty trait set (every axis absent) is indistinguishable in
    # intent from "no override" but would render a featureless ghost. The
    # builder cannot produce it (Apply always carries the seeded defaults), so
    # it only arrives via hand-written config or direct API use — collapse it
    # to the one canonical "reset" spelling instead of storing a third state.
    if all(not v for v in traits.values()):
        return {}
    return {"kind": "ghost", "traits": traits}


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
    fallback_model: str = field(
        default="auto",
        metadata=_meta(
            "Fallback model",
            "Model tried when the active model's transient-retry budget is "
            "exhausted (throttle/capacity). Default 'auto' defers to the "
            "backend's availability-aware routing; a concrete model id (as "
            "advertised by the provider, e.g. 'claude-opus-4.8') is tried "
            "first with 'auto' as the final fallthrough; empty ('') disables "
            "fallback entirely (fail loudly, pre-feature behavior). A fallback "
            "swap is announced in chat, sticks until the primary recovers, and "
            "the serving model is recorded in every turn's stats — never "
            "silent.",
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
    mcp_quarantine_after_failures: int = field(
        default=3,
        metadata=_meta(
            "Failing-Probe Threshold",
            "Consecutive failed probes before an MCP server is reported as "
            "persistently failing on its dashboard row. A probe verdict is "
            "otherwise forgotten between rounds, so a server that failed once on a "
            "cold cache looked identical to one that has failed forty times. "
            "Counts only 'error' and 'timeout': a server asking for OAuth sign-in "
            "is working correctly and is never counted, and one success clears the "
            "count. This is a health reading only -- the server stays mounted, and "
            "the dashboard offers a one-click count reset. 0 turns it off.",
        ),
    )
    acp_backend: str = field(
        default="",
        metadata=_meta(
            "ACP Backend",
            "Which ACP agent to drive: '' = kiro-cli (default), 'kas' = kiro-agent. "
            "KAS runs chat but has no native subagent progress reporting yet.",
            # Deliberately NO ``enum``. A literal here was frozen at import and fed
            # two import-time structures (``JSON_SCHEMA`` and ``SCHEMA_REGISTRY``),
            # both strictly earlier than an edition registering a backend at boot.
            # That made the enum actively harmful rather than merely stale —
            # ``validate_config_data`` DELETES an out-of-enum value before the
            # loader ever sees it, so a registered backend was stripped from
            # config.json on the way in. ``resolve_selected_backend`` is now the
            # single gate (it logs the reason it degrades), and
            # ``GET /api/config/schema`` supplies the live values the dashboard
            # renders. See harness-parity H4.
        ),
    )
    member_acp_backend: str = field(
        default="kas",
        metadata=_meta(
            "Crew member ACP backend",
            "Backend for crew-member DM sessions: 'kas' (default) or 'claude'. "
            "Members dispatch work into worker sessions through session-control "
            "tools mounted per session over the wire, which the kiro-cli v2 "
            "backend cannot carry — a value resolving to kiro leaves member "
            "threads as plain chat (no dispatch tools), logged at session start.",
            # Same no-enum reasoning as acp_backend above: the live selectable
            # set comes from the registry via resolve_selected_backend, never a
            # frozen literal.
        ),
    )
    default_agent: str = field(
        default="",
        metadata=_meta("Default Agent", "Default agent name for new sessions."),
    )
    sweep_agents_backups: bool = field(
        default=False,
        metadata=_meta(
            "Sweep foreign agent backups",
            "When true, the agents-directory janitor also deletes aged backup "
            "files (*.bak-<digits> / *.json.bak.<digits>, older than 14 days) "
            "from the shared kiro agents directory. OFF by default: Kiro Crew "
            "does not author those backups, so every one it would delete belongs "
            "to another tool whose retention policy is not ours to decide. The "
            "orphaned atomic-write TEMP sweep (24h) always runs and reclaims most "
            "of the growth at near-zero risk; enable this only if you also want "
            "foreign backups in that directory reaped.",
        ),
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
    apps_trusted_local: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Trusted Local Apps",
            "App names whose per-app execution grant was explicitly reviewed "
            "as local, repository-less code. This internal grant-kind marker "
            "distinguishes current local consent from legacy name-only grants; "
            "it is effective only with the matching apps_trusted entry.",
        ),
    )
    apps_trusted_repositories: dict[str, str] = field(
        default_factory=dict,
        metadata=_meta(
            "Trusted App Repositories",
            "Repository coordinates captured by the per-app trust endpoint. "
            "Each key is an app name from apps_trusted and each value is the "
            "normalized repository shown at consent. Registry installation "
            "refuses if that name later resolves to a different repository. "
            "Legacy repository-backed grants without an entry require one-time "
            "re-consent before code execution.",
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
    session_control: bool = field(
        default=True,
        metadata=_meta(
            "Session Control",
            "Let one chat session open a new session, and stop, read or send to "
            "another session of yours. Reading returns a transcript tail, stopping "
            "cancels an in-flight turn, a created session starts empty for you to "
            "type into, and a send runs text as the target's next turn. On by "
            "default, because the grant that decides who can do this is the agent "
            "config: the tools come from the kirocrew-dashboard MCP server, so an "
            "agent that does not mount it never has them, exactly like any other "
            "MCP server. Turn this off to withdraw the capability from every agent "
            "at once without editing each spec. Sessions can only reach peers in "
            "the same workspace; incognito, app-scoped and scheduled sessions are "
            "never addressable, and a crew member or a scheduled run reaches only "
            "sessions it created itself.",
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
        # Same defensive coercion for the throttle-fallback model: normalize to
        # ""/"auto"/acp id, so consumers can trust the stored shape.
        self.fallback_model = coerce_fallback_model(self.fallback_model)

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
        default=DEFAULT_AUTOCOMPACT_PCT,
        metadata=_meta(
            "Auto-Compact Threshold",
            "Context usage percentage at which auto-compaction triggers (5-90).",
        ),
    )
    pool_size: int = field(
        default=DEFAULT_POOL_SIZE,
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
    embedding_bulk_threads: int = field(
        default=1,
        metadata=_meta(
            "Embedding Threads (bulk)",
            "CPU threads for BACKGROUND corpus embedding — the re-embed sweep that "
            "gives imported memories semantic reach, plus imports and consolidation — "
            "as opposed to a query you are waiting on. Defaults to 1: nothing waits on "
            "this work (those rows are keyword-searchable meanwhile), so it is tuned to "
            "stay invisible rather than finish early. Raise it to get through a large "
            "backlog sooner; interactive search keeps its own pool either way. 0 means "
            "inherit Embedding Threads. Clamped to the machine's core count.",
        ),
    )
    embedding_bulk_duty: float = field(
        default=0.2,
        metadata=_meta(
            "Embedding Duty Cycle (bulk)",
            "Fraction of wall time a background embedding sweep targets for computing. "
            "At the default 0.2 it idles four times as long as it works, so a sweep "
            "over a freshly imported memory costs about a fifth of one core instead of "
            "several — the same total work, spread thin enough that fans never react. "
            "The sweep resumes across restarts, so it need not finish in one session. "
            "A target rather than a ceiling: one unusually slow row is capped at a "
            "30-second pause and so runs hotter than the configured share. 1.0 runs "
            "flat out. Clamped to [0.05, 1.0]; a sweep a user explicitly starts from "
            "Settings is never paced.",
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
    decay_rates: dict[str, float] = field(
        default_factory=dict,
        metadata=_meta(
            "Memory Decay Rates",
            "Per-tag episodic recency decay rates, per day (retrieval score factor "
            "exp(-rate * days_old)). Keys are memory tags (case-insensitive); the "
            "reserved 'default' key replaces the built-in 0.03 for memories matching "
            "no configured tag. A memory carrying several configured tags uses the "
            "slowest (smallest) rate, so a broad tag can never age out a "
            "long-retention one. 0 means never ages out of retrieval ranking; 1 "
            "drops a memory out of retrieval within about a day. Ranking only: "
            "episodic_max_count cap eviction (lowest importance, then oldest) "
            "still applies regardless of decay rate. Values are clamped to 0..10; "
            "non-numeric values are ignored with a logged warning.",
        ),
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
    extraction_effort: str = field(
        default="",
        metadata=_meta(
            "Extraction Effort",
            "Reasoning effort for the document-extraction LLM pool. Empty "
            "inherits the background-role effort (agent.role_efforts.background), "
            "then high. Only applies on reasoning-capable models.",
            enum=["", *EFFORT_LEVELS],
        ),
    )
    fetch_effort: str = field(
        default="",
        metadata=_meta(
            "URL Fetch Effort",
            "Reasoning effort for the Knowledge URL-fetch worker. Empty inherits "
            "the background-role effort (agent.role_efforts.background), then the "
            "provider/model default. Only applies on reasoning-capable models.",
            enum=["", *EFFORT_LEVELS],
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
            "Bot IDs allowed to bypass the bot filter for multi-node mesh communication. "
            "The gateway's own bot ID is never trusted, even if listed "
            "(it would reply to itself in a loop).",
            tags=["slack"],
        ),
    )
    trusted_bot_turn_limit: int = field(
        default=5,
        metadata=_meta(
            "Trusted Bot Turn Limit",
            "Maximum consecutive turns a thread may run on trusted-bot messages "
            "before a human message is required (loop guard for mutually trusted "
            "gateways). A message from an allowed human resets the count. "
            "Minimum 1; values below 1 are treated as 1.",
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
            "token path. Takes effect on the next gateway start (the trust "
            "settings are read once at startup).",
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
    keep_awake: bool = field(
        default=True,
        metadata=_meta(
            "Keep Awake While Published",
            "Keep this machine's SYSTEM awake while the dashboard is published "
            "on the tailnet, so a phone does not lose the dashboard when the "
            "laptop idles. The display is still allowed to sleep. Publishing is "
            "the opt-in — this exists to opt back OUT of the awake half without "
            "unpublishing. Independent of dashboard.prevent_sleep, which keeps "
            "the host awake only while a turn is in flight.",
        ),
    )


def _tailscale_config_from(
    raw: object,
    degraded: set[str] | None = None,
    *,
    key_present: bool = False,
) -> TailscaleConfig:
    """Build the validated :class:`TailscaleConfig` (RFC §3/§3.1 load rules).

    Two rules, both narrowing-only so a typo can never widen access:

    * ``trust_identity: true`` with an empty ``allowed_logins`` is a
      configuration error — refused with a logged reason, identity trust stays
      OFF. Never a silently-permissive default: "any tailnet member" on a
      shared corporate tailnet would hand the dashboard to all of them.
    * An unrecognised ``pin_scope`` falls back to ``"node"`` (the narrower
      scope) with a logged warning — never to ``"login"``.

    Both rules resolve to a *narrower* value, which is right for an operator
    typo and wrong for a value that was LOST: ``allowed_logins`` is the only
    restriction on which tailnet peer may authenticate, so losing it resolves
    to "identity trust off", i.e. no login restriction at all. Absent is
    genuinely unconfigured; MALFORMED is the operator having asked for a
    restriction this load cannot read, and it is recorded in *degraded* under
    :data:`DEGRADED_TAILSCALE` so the gate can deny instead of admitting every
    tailnet peer (the shape that reopened the publish allowlist, #4057).

    ``key_present`` separates the two states a bare value cannot: a MISSING
    ``tailscale`` key and one written as JSON ``null`` both arrive here as
    ``None``. Only the second is the operator having written something, so only
    it degrades -- reading ``None`` alone as malformed would deny every install
    that simply has no tailscale section. Callers that do not know pass nothing
    and get the absent reading, which is what the direct-value tests rely on.
    """
    if (key_present and raw is None) or (raw is not None and not isinstance(raw, dict)):
        # Reached only because "dashboard.tailscale" is a fail-closed path in
        # config/validation.py; without that entry the malformed value is
        # repaired to the default before this runs and there is nothing to see.
        # An explicit null counts: the operator had to write the key to produce
        # it, which is the absent-versus-malformed line this whole fix turns on.
        if degraded is not None:
            degraded.add(DEGRADED_TAILSCALE)
        _OBSERVED_DEGRADED_SECTIONS.add(DEGRADED_TAILSCALE)
        logger.warning(
            "config: 'dashboard.tailscale' is not a JSON object (got %s) — the "
            "tailnet login allowlist is unknown, so tailnet peers are DENIED "
            "until the file is fixed and the gateway restarted",
            type(raw).__name__,
        )
    data = _safe_dict(raw)
    enabled = _safe_bool(data.get("enabled"), False)
    trust_identity = _safe_bool(data.get("trust_identity"), False)
    if "trust_identity" in data and not isinstance(data.get("trust_identity"), bool):
        # The same class as the allowlist itself, one field over, and the field
        # is the restriction's own ON switch -- so it is the most permissive
        # default in the section. ``_safe_bool`` returns the default for
        # anything non-boolean, and that default is False, so `"true"` (a
        # quoted boolean, the commonest hand-edit slip) or `1` reads as "the
        # operator never asked for identity trust" and the perfectly valid
        # allowlist beside it stops being enforced.
        #
        # Recording it enforces the allowlist AS WRITTEN rather than denying
        # everyone: the entries parsed from a readable file are kept, so the
        # operator's own login still works and every peer they did not name is
        # refused. That is the closest honest reading of a config whose intent
        # to enable was garbled but whose list of who to admit was not.
        if degraded is not None:
            degraded.add(DEGRADED_TAILSCALE)
        _OBSERVED_DEGRADED_SECTIONS.add(DEGRADED_TAILSCALE)
        logger.warning(
            "config: 'dashboard.tailscale.trust_identity' is not a boolean (got "
            "%s) — it is the switch for the tailnet login allowlist, so the "
            "allowlist is enforced as written and every peer it does not name "
            "is DENIED until the file is fixed and the gateway restarted",
            type(data.get("trust_identity")).__name__,
        )
    raw_logins = data.get("allowed_logins")
    # The allowlist is only ever CONSULTED when identity trust is on, so a
    # malformed value in it loses nothing when the operator cleanly said off (or
    # never said on). Recording a degradation there would turn a typo in an
    # inert field into a forwarded-tailnet lockout, against a config that -- read
    # correctly -- permits those peers. A malformed FLAG is different: intent is
    # unknown, so the allowlist has to be treated as live.
    #
    # Presence is tested with ``in`` rather than ``is not None`` throughout: a
    # key written as JSON null is the operator having written something
    # unusable, not having left it out, and only the second is consent.
    _allowlist_is_live = trust_identity or (
        "trust_identity" in data and not isinstance(data.get("trust_identity"), bool)
    )
    if _allowlist_is_live and "allowed_logins" in data and not isinstance(raw_logins, list):
        # Same class one level down, and reachable WITHOUT a registry entry:
        # a three-segment path is past _apply_field_default's depth cap, so the
        # malformed value survives validation already. Recorded rather than
        # merely logged, because the log line below only fires when
        # trust_identity happens to be readable AND true — a config whose
        # trust_identity was lost in the same edit would say nothing at all.
        if degraded is not None:
            degraded.add(DEGRADED_TAILSCALE)
        _OBSERVED_DEGRADED_SECTIONS.add(DEGRADED_TAILSCALE)
        logger.warning(
            "config: 'dashboard.tailscale.allowed_logins' is not a list (got "
            "%s) — the tailnet login allowlist is unknown, so tailnet peers "
            "are DENIED until the file is fixed and the gateway restarted",
            type(raw_logins).__name__,
        )
    elif (
        _allowlist_is_live
        and isinstance(raw_logins, list)
        and any(not (isinstance(entry, str) and entry.strip()) for entry in raw_logins)
    ):
        # A LIST whose entries are not usable logins, e.g. [1] or ["a@b", None].
        # The comprehension below silently drops them, so an all-invalid
        # narrowing parses to [] — indistinguishable from "no restriction
        # configured", which is the exact silent widening this fix exists to
        # stop, and the same entry-level shape publish.allowed_destinations
        # already handles. An EMPTY list is NOT this case: that is a readable,
        # if mistaken, statement, and the trust_identity rule below already
        # refuses it with its own error.
        #
        # Unlike publish, the surviving entries are KEPT rather than zeroed.
        # The publish gate denies one whole action, so a partial allowlist
        # there has nowhere safe to land; this gate decides per peer, so
        # keeping the parseable logins narrows access to exactly what the
        # operator demonstrably wrote, while the degradation record still
        # denies every peer they did not name. Zeroing would instead lock out
        # the administrator whose own login parsed fine — a self-inflicted
        # outage in the middle of a config repair.
        if degraded is not None:
            degraded.add(DEGRADED_TAILSCALE)
        _OBSERVED_DEGRADED_SECTIONS.add(DEGRADED_TAILSCALE)
        logger.warning(
            "config: 'dashboard.tailscale.allowed_logins' carries entr(y/ies) "
            "that are not non-empty strings — the tailnet login allowlist is "
            "not what was written, so any peer it does not name is DENIED "
            "until the file is fixed and the gateway restarted",
        )
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
        keep_awake=_safe_bool(data.get("keep_awake"), True),
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


# dashboard.loop_stall_exit_after_secs -- event-loop silence tolerated before
# the gateway dumps all thread stacks and hard-exits. ``None`` is the
# serializable "automatic" sentinel: launch class selects the desktop or
# managed-service default without an unrelated config save pinning either one.
LOOP_STALL_EXIT_AFTER_MIN = 10
LOOP_STALL_EXIT_AFTER_MAX = 300
LOOP_STALL_EXIT_AFTER_DEFAULT = 25
LOOP_STALL_EXIT_AFTER_MANAGED_DEFAULT = 90
_MANAGED_SERVICE_ENV = "KIROCREW_SERVICE_MANAGED"

# dashboard.chat_entry_cache_max_entries / chat_entry_cache_max_bytes -- bounds
# on the persisted-message entry memo in ``dashboard/chat_persistence.py``. The
# right entry count is host-dependent: the cache's working set is roughly
# ``active_slots x window_size``, so a gateway with many concurrent chat slots
# overflows the entry bound while the byte bound still has headroom, and the LRU
# then evicts each slot's window just before its next save (a zero-hit cliff,
# every save re-paying redaction plus key derivation). The defaults match the
# previous hardcoded values; raising the entry bound on a many-slot host is the
# operator's call, with the byte ceiling still bounding memory.
CHAT_ENTRY_CACHE_ENTRIES_MIN = 256
CHAT_ENTRY_CACHE_ENTRIES_MAX = 262144
CHAT_ENTRY_CACHE_ENTRIES_DEFAULT = 4096
CHAT_ENTRY_CACHE_BYTES_MIN = 4 * 1024 * 1024
CHAT_ENTRY_CACHE_BYTES_MAX = 512 * 1024 * 1024
CHAT_ENTRY_CACHE_BYTES_DEFAULT = 32 * 1024 * 1024


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
    qr_session_until_restart: bool = field(
        default=True,
        metadata=_meta(
            "Phone Sign-In Lasts Until Restart",
            "Keep a phone signed in for as long as this gateway process runs. "
            "The QR code still has to be scanned within its short window; after "
            "that the phone is not signed out for being idle in ordinary use, "
            "and a gateway restart signs it out. The one remaining idle limit is "
            "the refresh credential's own 30-day lifetime, which each visit "
            "renews, so a phone that goes untouched for 30 days re-scans. Turn "
            "this OFF to go back to a timed session that expires on a clock "
            "whether or not the gateway is still running. Either way `kirocrew "
            "logout` ends the session immediately, and the session stays pinned "
            "to the peer it was established from.",
        ),
    )
    qr_session_persist_across_restart: bool = field(
        default=False,
        metadata=_meta(
            "Phone Sign-In Survives A Gateway Restart",
            'REQUIRES BOTH: "Phone Sign-In Lasts Until Restart" must also be ON, '
            "and tailnet identity trust must be configured "
            "(`dashboard.tailscale.trust_identity` with a non-empty "
            "`allowed_logins`). Without either one this setting is ignored and a "
            "warning naming the missing prerequisite is logged. Note the first "
            'requirement is NOT a contradiction: "Lasts Until Restart" is what '
            "issues the renewable credential, and this setting then removes the "
            "restart bound from it -- turning that one OFF instead leaves a "
            "session that expires on a fixed clock, with nothing to renew. "
            "What it does: let a scanned phone stay signed in across gateway "
            "restarts, so one scan lasts until the refresh credential's own "
            "30-day lifetime lapses. OFF by default because a restart is "
            "otherwise a hard sign-out that needs no recorded state. The "
            "identity requirement is not optional bookkeeping: behind "
            "`tailscale serve` every request reaches the gateway from 127.0.0.1, "
            "so without a daemon-verified peer identity the session is a bearer "
            "credential any tailnet peer could replay, and outliving the process "
            "is exactly what makes that matter.",
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
    loop_stall_exit_after_secs: int | None = field(
        default=None,
        metadata=_meta(
            "Loop-stall Hard-exit Budget (secs)",
            "Seconds the gateway's event loop may go silent before it dumps all "
            "thread stacks and exits. Leave unset for the automatic default: "
            "25 seconds for desktop/foreground launches and 90 seconds for a "
            "managed systemd/launchd service. An explicit value overrides both. "
            "Raise it on a host that does heavy subprocess work (long builds, "
            "test suites, many child reaps), which can wedge the loop briefly "
            "without being genuinely dead. Clamped to 10s..300s. The desktop app's "
            "liveness probe kills at roughly 20s independently, so a value "
            "above that only takes effect for a headless gateway — the desktop "
            "probe wins first and the stack dump is lost.",
        ),
    )
    chat_entry_cache_max_entries: int = field(
        default=CHAT_ENTRY_CACHE_ENTRIES_DEFAULT,
        metadata=_meta(
            "Chat Entry Cache Max Entries",
            "Maximum number of persisted-message entries the chat save path "
            "memoises. The cache's working set is roughly the number of active "
            "chat slots times their window size, so the right bound is "
            "host-dependent: a gateway with many concurrent slots overflows "
            "this bound while the byte ceiling still has headroom, and the "
            "cache hit rate collapses to zero (every save re-pays redaction). "
            "Raise it on a many-slot host. Clamped to 256..262144. Read once "
            "at first use; a change takes effect on the next gateway restart.",
        ),
    )
    chat_entry_cache_max_bytes: int = field(
        default=CHAT_ENTRY_CACHE_BYTES_DEFAULT,
        metadata=_meta(
            "Chat Entry Cache Max Bytes",
            "Memory ceiling in bytes for the chat save path's persisted-message "
            "entry memo. Evicted alongside the entry-count bound; raise it "
            "together with the entry bound when a many-slot host needs a "
            "larger cache. Clamped to 4 MiB..512 MiB. Read once at first use; "
            "a change takes effect on the next gateway restart.",
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
    browser_view_port: int = field(
        default=0,
        metadata=_meta(
            "Browser Live-View Port",
            "Pin the browser live-view server (playwright-cli show) to this "
            "loopback port. 0 (the default) picks a fresh OS-assigned ephemeral "
            "port on every start. Set a fixed port when the dashboard is viewed "
            "remotely through an SSH tunnel that forwards a fixed set of ports, "
            "so the Browser panel can always reach the view. The server binds "
            "loopback-only either way. A value outside 1-65535 is treated as "
            "unset. A changed pin applies the next time the view server "
            "(re)starts; an already-running server keeps its current port.",
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
            "after it must be scannable bullets rather than prose; "
            "'answer_only' drops explanation altogether — the answer or "
            "artifact alone, with at most one sentence of context, and detail "
            "only when the user asks for it, when the decision is "
            "consequential enough (security, exposure, data loss, spend, "
            "anything hard to undo) that they cannot choose correctly without "
            "the reasoning, or as the undo path that rides along with a "
            "destructive command. At every level security warnings and "
            "irreversible-action confirmations always appear but stay brief, "
            "and ordered multi-step instructions stay complete.",
            enum=["default", "concise", "ultra", "answer_only"],
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
    # Default TRUE: the chip strip shipped unconditionally before this switch
    # existed, so a config that never mentions the key must render exactly what
    # it rendered before.
    session_card_source_links: bool = field(
        default=True,
        metadata=_meta(
            "PR and issue chips on session cards",
            "Show a chip on a session's sidebar card for each pull request, merge "
            "request and issue mentioned anywhere in that session's transcript. "
            "Turning this off reclaims a row per card on the densest surface in "
            "the app, keeps numbers from unrelated work off screen while sharing "
            "it, and stops the periodic credentialed provider calls that keep "
            "those chips' CI and merge status fresh. The in-session Resources and "
            "Changes panels are unaffected.",
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
    update_nudge: dict = field(
        default_factory=dict,
        metadata=_meta(
            "Update Nudge",
            "Per-version state for the proactive update popup. Written by the "
            "dashboard when the user snoozes or skips a release; a record only "
            "suppresses the popup for the version it names. Validated as one "
            "atomic record by the PATCH allowlist (dashboard.update_nudge); "
            "no Settings control reads it, so it carries no schema properties.",
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
    reasoning_effort: str = field(
        default="",
        metadata=_meta(
            "Reasoning Effort",
            "Default reasoning effort for sessions on this crew. Empty inherits: "
            "the global agent.reasoning_effort (or, for a background worker crew, "
            "its role effort). A per-session pick still overrides this. Only "
            "reasoning-capable models accept a level; on any other model the pin "
            "is ignored, exactly as the global default is.",
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
    session_color: str = field(
        default="",
        metadata=_meta(
            "Session Color",
            "Default session color for sessions created by this agent. Accepts "
            "a CSS hex color string (#rrggbb, lowercase). Applied at render time "
            "to any session this agent started that has no color of its own, so "
            "editing it re-tints those sessions live. A color set on the session "
            "itself (a manual pick or the dashboard default-color policy) always "
            "takes precedence. Empty means no agent color.",
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
    avatar: dict = field(
        default_factory=dict,
        metadata=_meta(
            "Avatar",
            "Per-crew avatar override. Empty means the face is derived from "
            "the crew's name. {'kind': 'ghost', 'traits': {...}} pins explicit "
            "ghost traits chosen in the avatar builder; {'kind': 'image'} "
            "means an uploaded picture served from the per-crew avatar "
            "endpoint.",
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
    project_skills_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Project Skills",
            "Whether a chat session may load skills from its own project's "
            "<project>/.kiro/skills directory. Enabled by default, but a project's "
            "skills are still only loaded after the operator grants that specific "
            "directory trust, because a SKILL.md enters the agent's context and can "
            "instruct it to run anything. Set false to make project skills "
            "impossible regardless of any grant already recorded.",
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
        default=50,
        metadata=_meta(
            "Maximum Intents",
            "Safety ceiling on intents stored per session (>=1). Trimming runs "
            "before the summary is saved, so whatever exceeds this is dropped "
            "from the record rather than hidden -- the panel itself withholds "
            "nothing, rendering every intent it receives and collapsing all but "
            "the most recently touched one. The ceiling therefore sits high "
            "enough that reaching it is unusual rather than routine.",
        ),
    )
    max_constraints: int = field(
        default=50,
        metadata=_meta(
            "Maximum Project Notes",
            "Safety ceiling on session-level operational notes -- the recurring facts "
            "about how this project is run (>=0). Whatever exceeds this is dropped "
            "from the record rather than hidden: how many are worth writing at all "
            "is governed by the generation prompt, and the panel bounds the expanded "
            "list's height rather than its length. Durable cross-session preferences "
            "belong in lessons rather than here.",
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
            logger.warning("regenerate_after_turns %d < 1, using 1", self.regenerate_after_turns)
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
            "OTLP exporter from the otlp package extra to be installed "
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
# Security-relevant resource-limit ceilings
# ---------------------------------------------------------------------------
# SINGLE SOURCE OF TRUTH for the upper bounds on the config knobs that govern
# host resource consumption. These same ceilings are enforced by the dashboard
# config API (``dashboard/handlers/core.py`` for the agent knobs,
# ``session.py`` for ``pool_size``); they live HERE so the API-write gate and
# the loader's load-time clamp cannot drift apart.
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

# ── Load/write bound parity ────────────────────────────────────────────────────
# Ranges for bounded numeric fields whose LOAD path previously applied no bounds
# at all, while `_EDITABLE_CONFIG` rejected the same values at write time. A
# hand-edited config.json goes nowhere near the dashboard API, so every one of
# these loaded verbatim -- the same asymmetry #4688 and #4734 closed for the
# security-relevant knobs.
#
# Defined HERE and imported by `_EDITABLE_CONFIG` rather than spelled twice, so
# the write gate and the load clamp cannot drift. Three fields already clamped on
# load but duplicated their literals across the two files; those now read from
# these names too, which is the "two-literal drift" half of the same problem.
#
# Bounds are the ones the write path already declared. This change does not
# re-litigate any range; it makes the load path honour what the API promised.
COMPLETION_KEEP_CHARS_MIN = 0
# Mirrors ``context_management.RESULT_FILE_MAX_BYTES`` (500 KB) rather than importing
# it: ``context_management`` does ``from kiro_crew.config.loader import config_dir``, so
# importing it here is a genuine circular import, not a style preference. The value is
# therefore spelled in both places and pinned equal by
# ``test_the_completion_keep_ceiling_matches_its_owner`` -- a test can import both
# without the cycle, which is the only place the two spellings can be held together.
COMPLETION_KEEP_CHARS_MAX = 512_000
MCP_PROBE_TIMEOUT_MIN = 5
MCP_PROBE_TIMEOUT_MAX = 120
RECENT_TINT_COUNT_MIN = 0
RECENT_TINT_COUNT_MAX = 10
SESSION_TIMEOUT_MIN = 0
SESSION_TIMEOUT_MAX = 86400
POOL_TTL_SECS_MIN = 0
POOL_TTL_SECS_MAX = 7200
SOFT_STOP_BUDGET_MIN = 0.5
SOFT_STOP_BUDGET_MAX = 60.0
EXTRACTION_POOL_SIZE_MIN = 1
EXTRACTION_POOL_SIZE_MAX = 10
# knowledge.* budgets. These share a floor of 0, but 0 is MEANINGFUL for several
# of them (a zero budget disables that sweep), so the floor is deliberately not
# enforced by clamping a negative up to 0 -- see `_safe_nonnegative_int`, which
# keeps returning the default for a negative value. Only the missing CEILING is
# added here, which is where the actual exposure was: an absurd hand-edited
# budget was loaded verbatim and became real work.
FOLDER_INGEST_CHUNK_BUDGET_MAX = 10000
DEDUP_EVERY_N_SWEEPS_MAX = 288
SWEEP_CHUNK_BUDGET_MAX = 50000
EMBED_RATE_LIMIT_MAX = 10000


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


#: The provider an unusable ``stt.provider`` degrades to, and the default. It is
#: the only one with no precondition: recognition runs in this process on every
#: supported OS, with no account, no platform floor, and no separate install.
STT_PROVIDER_LOCAL = "local"

#: The recognisers a user can select. ``local`` runs whisper.cpp in-process,
#: ``apple`` uses macOS 26+ on-device recognition, and ``transcribe`` sends audio
#: to AWS Transcribe (billed, and gated on the AWS consent prompt). All three
#: produce partial results, so streaming is not a per-provider capability.
_VALID_STT_PROVIDERS = (STT_PROVIDER_LOCAL, "apple", "transcribe")

#: Providers a stored config may still name. Each of these needed an out-of-band
#: install the user had to perform themselves (a whisper CLI on ``PATH``, or an
#: ``mlx``/``faster-whisper`` wheel), which is precisely the cost the resident
#: local engine removes, so a stored value degrades to ``local`` instead of
#: leaving voice input pointing at something that is no longer dispatchable.
_RETIRED_STT_PROVIDERS = ("whisper", "mlx", "parakeet", "faster")

#: Model names accepted for ``stt.model``, derived from the catalog that owns the
#: download and its sha256 pin rather than restated here. Restating it is how the
#: advertised menu comes to offer a model that cannot be fetched.
_VALID_STT_MODELS = tuple(m.name for m in _STT_CATALOG)


_VALID_CHANNEL_PREFIXES = ("C", "D", "G")


# Provider values already warned about in this process. The gateway loads config
# repeatedly, so an unusable stored provider is per-install information, not
# per-load: without this the retirement notice repeats several times a second for
# the whole session, which is how it was reported. Keyed on ``repr`` rather than
# the value itself because this arrives from ``config.json`` and may be
# unhashable (a list or dict), which must not raise on the degrade path. Exposed
# for tests to reset.
_WARNED_STT_PROVIDERS: set[str] = set()


def stt_provider_is_coerced(value: object) -> bool:
    """True when a stored ``stt.provider`` cannot take effect and is replaced.

    The single source of truth for "this stored value is inert", so the surface that
    offers to remove it (``kirocrew config defaults``) cannot come to disagree with
    the loader about which providers are dispatchable.
    """
    return value not in _VALID_STT_PROVIDERS


def _validated_stt_provider(value: object) -> str:
    """Return *value* if it is selectable, else degrade to ``local`` with a reason.

    Degrades and logs; never raises. This value arrives from ``config.json``, so
    an unusable one must leave voice input working the way
    :func:`_normalize_acp_backend` degrades an unusable persisted backend, rather
    than failing the load that read it.

    The notice names the command that removes the dead value. A load never writes,
    so without that pointer the line repeats on every invocation forever -- and
    unlike a superseded default there is nothing here to preserve, since the stored
    value cannot take effect either way.
    """
    if value in _VALID_STT_PROVIDERS:
        return str(value)
    seen = repr(value)
    if seen in _WARNED_STT_PROVIDERS:
        return STT_PROVIDER_LOCAL
    _WARNED_STT_PROVIDERS.add(seen)
    if value in _RETIRED_STT_PROVIDERS:
        logger.warning(
            "STT provider %r is retired; using %r instead. It needed a separate "
            "out-of-band install, which the bundled local engine removes while "
            "recognising the same speech. Run 'kirocrew config defaults --adopt' "
            "to drop the stored value and this notice.",
            value,
            STT_PROVIDER_LOCAL,
        )
    else:
        logger.warning(
            "Unknown STT provider %r; using %r instead. Selectable providers: %s. "
            "Run 'kirocrew config defaults --adopt' to drop the stored value.",
            value,
            STT_PROVIDER_LOCAL,
            ", ".join(_VALID_STT_PROVIDERS),
        )
    return STT_PROVIDER_LOCAL


def _validated_stt_model(value: object) -> str:
    """Return the catalog name *value* selects, falling back to the default.

    Canonicalized here rather than passed through, so every consumer sees a name
    that names a real catalog entry: the model becomes a filename under the
    models directory, and an arbitrary string must not reach a path. ``resolve``
    also maps the names older configuration used onto their current entries, so a
    stored ``turbo`` keeps the model it asked for instead of silently moving to
    the default.
    """
    if not isinstance(value, str) or not value:
        logger.warning("Non-string STT model %r; using %r", value, _STT_DEFAULT_MODEL)
        return _STT_DEFAULT_MODEL
    return _resolve_stt_model(value).name


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
    """Coerce a persisted ``agent.acp_backend`` to a backend this build can serve.

    Delegates to :func:`kiro_crew.acp_backends.resolve_selected_backend`, which owns
    the selectable registry, so the load path, the dashboard PATCH allowlist and the
    schema endpoint cannot disagree about which harnesses exist.

    The import is at module scope rather than deferred: ``acp_backends`` is a leaf
    that imports nothing from ``kiro_crew.acp``, so it does not reproduce the
    package-init cycle (``kiro_crew.acp.__init__`` -> client + runtime -> this
    module) that the old local import of ``acp.types`` existed to dodge.
    """
    return resolve_selected_backend(value)


def _validate_activation(value: str) -> str:
    """Return *value* if it is a valid activation mode, else ``mention`` (deny-by-default)."""
    return value if value in _VALID_ACTIVATIONS else ACTIVATION_MENTION


#: The activation modes a Telegram forum Topic can express. A subset of
#: ``_VALID_ACTIVATIONS`` on purpose, and the subset is the point rather than an
#: omission: ``observe`` needs a channel-history buffer only Slack populates, and
#: feeding it would put non-owner prose into the prompt unfenced; ``review`` is a
#: whole second rendering mode built on Slack Block Kit ephemerals, which Telegram
#: has no equivalent for. Declaring either here would advertise a mode that
#: silently behaves like a different one.
TELEGRAM_ACTIVATIONS = frozenset({ACTIVATION_ALWAYS, ACTIVATION_MENTION, ACTIVATION_OFF})


def _validate_telegram_activation(value: str) -> str:
    """*value* if Telegram can express it, else ``mention``.

    Degrades to the NARROWER mode, matching ``WeixinTransport.authorize``'s
    treatment of an unrecognized ``dm_policy``: a malformed value must not resolve
    to the most permissive reading of itself. ``always`` starts a turn for every
    message in an allow-listed Topic, and a Topic is a SHARED space, so agent
    output lands in front of everyone in it. Widening that because a value failed
    to parse would make a typo grant participation the operator never asked for,
    and it fails silently in the direction nobody audits.

    ``mention`` rather than ``off`` because it is fail-safe without being
    fail-dead: an explicit ``@handle`` is an unambiguous request, so the operator
    can still reach the bot while it is refusing to answer unaddressed messages.

    Reached ONLY for a value that was present and unparseable. An ABSENT key is
    resolved to ``always`` by the caller before this runs, and that stays: taking
    the documented default is not the same act as asking for something specific
    and being misunderstood.
    """
    if value in TELEGRAM_ACTIVATIONS:
        return value
    logger.warning(
        "telegram.forum_activation=%r is not one of %s; using %r (the narrower mode, "
        "so an unreadable value cannot widen who the bot answers).",
        value,
        ", ".join(repr(a) for a in sorted(TELEGRAM_ACTIVATIONS)),
        ACTIVATION_MENTION,
    )
    return ACTIVATION_MENTION


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
    # The Kiro Crew agent's own default model, "" when it pins none. Ranks below
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
    # The Kiro Crew ALIAS whose bindings these are ("" when no alias applied). A
    # caller replacing an unhonored request must store THIS, not ``kiro_agent``:
    # the stored value is re-resolved later and an alias is matched first, so a
    # physical kiro agent name that also happens to be an alias key would resolve
    # to that alias's target instead — reintroducing the advertised-vs-answering
    # mismatch. An alias key round-trips to itself.
    resolved_alias: str = ""

    def same_dispatch_binding(self, other: "ResolvedBindings") -> bool:
        """Whether two resolutions name the SAME dispatch target.

        Owned here, next to the field set, so a future dispatch-relevant
        binding field forces the identity question at the layer that defines
        it rather than silently widening a permission check that enumerated
        fields by hand (the dashboard's slot agent-conflict guard uses this to
        decide whether two different NAMES may share a slot). Compares every
        field that changes what answers a turn — the kiro agent, workspace,
        memory store, and model — and deliberately not ``resolved_alias``
        (two names resolving to one alias's target ARE the same binding) or
        ``requested_resolved``/``effective_memory_config`` (the former is
        request metadata the caller checks separately; the latter is derived
        from ``memory_store_name`` plus global config shared by both sides).
        """
        return (
            self.kiro_agent == other.kiro_agent
            and self.workspace_dir == other.workspace_dir
            and self.memory_store_name == other.memory_store_name
            and self.model == other.model
        )


@dataclass
class SttConfig:
    """Speech-to-text configuration.

    Enabled by default. Recognition runs on this machine through the bundled
    engine, so having voice input available costs one model download the first
    time it is used and nothing after that.
    """

    enabled: bool = field(
        default=True,
        metadata=_meta("Enabled", "Turn spoken input into text you can send."),
    )
    provider: str = field(
        default=STT_PROVIDER_LOCAL,
        metadata=_meta(
            "Provider",
            "Where speech is recognised. `local` runs on this machine and needs no "
            "account (it downloads one model the first time you dictate), `apple` "
            "uses the on-device recogniser built into macOS 26 and later, and "
            "`transcribe` sends your audio to AWS Transcribe, which bills your AWS "
            "account.",
            enum=list(_VALID_STT_PROVIDERS),
        ),
    )
    model: str = field(
        default=_STT_DEFAULT_MODEL,
        metadata=_meta(
            "Model",
            "Which speech model the local provider downloads and runs. Bigger is "
            "more accurate and a longer first-time download: `tiny` on a machine "
            "short of memory, `base` for everyone, `small` when accents or jargon "
            "are being misheard, `large-v3-turbo` for the best accuracy available.",
            enum=list(_VALID_STT_MODELS),
        ),
    )
    language_code: str = field(
        default="en-US",
        metadata=_meta(
            "Language Code", "Language for speech recognition (e.g. en-US, fr-FR, es-ES)."
        ),
    )
    streaming: bool = field(
        default=True,
        metadata=_meta(
            "Streaming",
            "Show words in the message box while you are still speaking rather than "
            "only once you stop. Every provider supports it; turning it off spends "
            "less CPU on the local provider and fewer API calls on `transcribe`.",
        ),
    )
    silence_ms: int = field(
        default=_STT_DEFAULT_SILENCE_MS,
        metadata=_meta(
            "End-of-phrase silence",
            "How long a pause has to last, in milliseconds, before what you said is "
            "treated as a finished phrase. Raise it if you are being cut off "
            "mid-sentence; lower it if the text lags behind you.",
        ),
    )
    partial_interval_ms: int = field(
        default=_STT_DEFAULT_PARTIAL_INTERVAL_MS,
        metadata=_meta(
            "Live update interval",
            "How often the live transcript is refreshed while you speak, in "
            "milliseconds. Lower feels more immediate and costs a little more CPU "
            "per second of speech; higher is steadier to read.",
        ),
    )
    idle_evict_secs: int = field(
        default=_STT_DEFAULT_IDLE_EVICT_SECS,
        metadata=_meta(
            "Release model after",
            "How long the local model stays loaded in memory after your last "
            "recording, in seconds. It holds roughly 150 MB at the default model, "
            "and reloading it takes a fraction of a second, so lower this on a "
            "machine short of memory. 0 releases it as soon as you stop speaking.",
        ),
    )
    endpointing: bool = field(
        default=False,
        metadata=_meta(
            "Semantic endpointing",
            "While dictating, run a fast background model on each finished phrase to "
            "detect when you have asked a complete question, then send it without "
            "you pressing anything. Needs streaming; off by default.",
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
    stub_overrides: dict[str, bool] = field(
        default_factory=dict,
        metadata=_meta(
            "Stub Overrides",
            "Per-server deviations from stub_servers: a name mapped to true is "
            "stubbed even when the roster omits it, false leaves it direct even "
            "when the roster carries it. Holds what you CHANGED, not the result, "
            "so a name you never touched keeps following the roster — which is "
            "what lets an edition that ships its own stub_servers grow that list "
            "without overwriting your choices, and lets you turn one server off "
            "without pinning yourself to today's roster. Written by MCP "
            "Management when a toggle disagrees with the roster, and dropped "
            "again when you toggle it back to agree. Empty by default.",
        ),
    )
    #: The roster EXACTLY as the file states it, carried so a full-file rewrite
    #: can put it back.
    #:
    #: :attr:`stub_servers` above holds the EFFECTIVE set, because that is what all
    #: seven of its consumers want (routing, the page's rows, ``stub_count``, the
    #: doctor). But ``save()`` round-trips this dataclass through ``asdict``, so a
    #: field whose value differs from the file's is a landmine: emitting the
    #: effective set would rewrite ``stub_servers`` without the servers the operator
    #: opted out of, turning a reversible deviation into a permanent deletion from a
    #: layer that is not ours to edit -- and it would happen on any unrelated
    #: ``save()``. Carrying the roster lets :meth:`KiroCrewConfig.to_dict` emit the
    #: file's own value instead.
    #:
    #: Excluded from serialization (``repr=False``, popped by ``to_dict``) -- it is
    #: not a config key and must never be written back as one. The leading
    #: underscore keeps it out of the config schema/baseline machinery, which skips
    #: private fields (same convention as ``_degraded_sections``); consumers read
    #: the :attr:`stub_roster` property.
    _stub_roster: list[str] = field(
        default_factory=list,
        repr=False,
        compare=False,
    )

    @property
    def stub_roster(self) -> list[str]:
        """The stub roster as configured, before operator deviations."""
        return self._stub_roster

    pool_identity_env: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Pool Identity Env Keys",
            "Env variable NAMES whose value is part of a shared backend's "
            "identity. Names listed here are folded into the backend's env hash "
            "even when they look like a rotating secret (AWS_SECRET*, "
            "AWS_SESSION*, OAUTH*), which is what makes them safe to apply to a "
            "shared backend: two sessions declaring different values get "
            "different backends instead of colliding onto one. Use it to let a "
            "server that authenticates from such a variable be shared at all — "
            "by default it declares one, so nothing is forwarded and the server "
            "runs unwrapped. The cost is the reason the exclusion exists: "
            "rotating a named value re-partitions that server's pool, so the "
            "next session cold-starts a backend. Exact names, not prefixes. "
            "Names the daemon's own credential scrub removes (AWS_ACCESS*, "
            "AWS_SECRET*, AWS_SESSION*, SSH_AUTH_SOCK*, GNUPGHOME*, "
            "GIT_ASKPASS*) are ignored here — that scrub is a separate, broader "
            "guard this setting does not lift. Empty by default.",
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
    McpGatewayConfig.__dataclass_fields__["forward_declared_env"].default  # type: ignore[arg-type]
)


@dataclass
class McpConfig:
    """MCP server settings that apply whether or not the broker is enabled.

    Distinct from :class:`McpGatewayConfig`, which configures the sharing broker
    itself: these settings govern how MCP servers are FOUND and launched, so
    they matter equally with the broker off.
    """

    extra_path_dirs: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Extra MCP Binary Directories",
            "Additional directories to search for MCP server binaries, ahead of "
            "the built-in locations. Add one when a package manager installs its "
            "MCP launchers somewhere Kiro Crew does not know about: a server "
            "declared by bare name that resolves nowhere never starts, and the "
            "session just comes up short of tools. Each entry must be a single "
            "absolute directory (``~`` is expanded); anything else is ignored "
            "with a warning. These directories are prepended to the search path "
            "used by the MCP probe, the agent-config command resolver, and the "
            "broker's rewriter alike, so a binary found here is found "
            "everywhere. They do NOT join the search for the agent runtime "
            "itself, which must not be shadowable by a configured directory.",
        ),
    )


@dataclass
class InstancesConfig:
    """Multi-instance management (the *Instances* feature).

    Gates and tunes the gateway's ability to manage/switch between several
    remote Kiro Crew instances over SSH tunnels. Off by default — opt-in only,
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
            "full dashboard SPA). 0 (the default) is automatic: the cap follows how "
            "many crews are configured, so up to an internal ceiling no crew you added "
            "is evicted and the cap widens by itself when you add one -- eviction "
            "cold-boots the pane and reads as a disconnect, so a cap below the number "
            "of crews in use makes tab switching look like a connection flap. Past that "
            "ceiling eviction resumes; an explicit value is honoured exactly, including "
            "one below the number of configured crews.",
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
        if self.warm_set_cap < 0:
            # 0 is meaningful here (automatic -- track the connected count), so
            # only a negative value is a misconfiguration, and it falls back to
            # automatic rather than to 1: a caller who wrote a nonsense number
            # wanted "enough", not the tightest possible cap.
            logger.warning(
                "instances.warm_set_cap %d < 0, using 0 (automatic: track the connected count)",
                self.warm_set_cap,
            )
            object.__setattr__(self, "warm_set_cap", _WARM_SET_CAP_AUTO)
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
        elif self.mint_timeout_secs is not None and self.mint_timeout_secs > _MINT_TIMEOUT_CEILING:
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
            "(agent.chat_turn_timeout_secs) so recovery is reachable. Enforcement is "
            "at handle construction, not config load: a window past the headroom "
            "fraction of the transport's per-prompt timeout is clamped with a "
            "warning, while one that merely exceeds agent.chat_turn_timeout_secs is "
            "warned about but left as set, because the same handle also serves "
            "callers that pass a larger prompt timeout (review and cron turns).",
        ),
    )
    tool_stall_hard_cap_secs: float = field(
        default=3600.0,
        metadata=_meta(
            "Hard cap (s)",
            "Absolute ceiling for UNKNOWN-verdict forbearance (e.g. the extended "
            "probably-thinking window). Applies ONLY to UNKNOWN verdicts — never "
            "to a WORKING session, which is deferred before this cap is consulted "
            "and is therefore bounded only by the turn's own ceiling. Default 1h, "
            "clamped against the transport's per-prompt timeout like the suspect "
            "window.",
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


# Keys whose out-of-domain value has already been reported, so a knob read once
# per spawn warns once per process instead of once per agent launch. Same shape
# as ``_OBSERVED_DEGRADED_SECTIONS``; exposed for tests to reset.
_WARNED_RESOURCE_LIMIT_KEYS: set[str] = set()


def _limit_int(value: object, key: str, *, lo: int, hi: int | None = None) -> int | None:
    """Coerce one ``resource_limits`` value, or ``None`` when it is out of domain.

    ``None`` means "no usable value here" and is deliberately NOT a number: each
    mechanism's fallback is its own documented default (``_RLIMIT_DEFAULTS`` for
    the rlimit path, ``_CGROUP_DEFAULT_*`` for the cgroup paths), and those must
    stay where they are rather than being copied into this dataclass as a third
    default set.

    The coercion rules, and why each one is what it is:

    - ``bool`` is not a number here. ``True`` would otherwise coerce to ``1`` and
      set a one-process / one-MB ceiling, which kills the child it limits.
    - A non-integral float TRUNCATES toward zero (``512.5`` -> ``512``), matching
      what every pre-existing reader did, so tightening the parse cannot loosen
      an operator's ceiling.
    - EXCEPT when it truncates to ``0``, either sign: ``0.5`` is not a request to
      disable the limit, but ``int(0.5)`` is exactly the value that means
      "disabled" on the rlimit path and "use the default" on the cgroup path.
      That silent reinterpretation is the trap in #3474, so it is refused.
    - NaN and +/-Infinity are refused before ``int()`` sees them. ``json.loads``
      accepts both literals, and ``int(inf)`` raises ``OverflowError`` --
      uncaught on the rlimit path, which turned a typo into a failure of every
      spawn.
    - Out of range REFUSES rather than clamps, and is checked on the value AS
      WRITTEN rather than on the truncated result. A clamp would silently move a
      confinement ceiling away from the number the operator can read in their own
      file; checking after truncation would let a value below the floor land back
      inside it (``int(-0.5) == 0`` passes a ``>= 0`` floor and then reads as
      "leave inherited", removing the ceiling entirely).

    Every refusal is logged once per key per process: the value is security
    relevant, so an operator must not have to infer it was dropped.
    """

    def _refuse(reason: str) -> None:
        if key in _WARNED_RESOURCE_LIMIT_KEYS:
            return
        _WARNED_RESOURCE_LIMIT_KEYS.add(key)
        logger.warning(
            "config: resource_limits.%s = %r %s — ignoring it and using the "
            "documented default for that mechanism",
            key,
            value,
            reason,
        )

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse("is not a number")
        return None
    if isinstance(value, float) and not math.isfinite(value):
        _refuse("is not a finite number")
        return None
    # Range-check the value AS WRITTEN, before any truncation. Checking the
    # truncated result instead lets a value BELOW the floor land back inside it:
    # ``int(-0.5) == 0`` satisfies a ``>= 0`` floor and then reads as this
    # block's "leave inherited" sentinel, REMOVING the ceiling the operator was
    # trying to set.
    if value < lo or (hi is not None and value > hi):
        _refuse(f"is outside the accepted range [{lo}, {hi if hi is not None else 'unbounded'}]")
        return None
    if isinstance(value, float) and not value.is_integer():
        # A fraction that truncates to zero is refused whatever its sign. Zero
        # is meaningful to every consumer of this block -- "leave inherited",
        # "use the default", "disabled" -- so truncating would silently swap the
        # operator's request for one of those.
        if int(value) == 0:
            _refuse("is a fraction that would truncate to 0, which means something else")
            return None
        logger.debug("config: resource_limits.%s = %r truncated to %d", key, value, int(value))
    return int(value)


@dataclass
class ResourceLimitsConfig:
    """Kernel confinement ceilings for spawned agent processes.

    THREE mechanisms read this one block, and a key shared between two of them
    does NOT mean the same thing on both. That is the whole reason this section
    has a schema (#3474): every consumer used to parse the raw dict itself, so
    the incompatible domains were written down nowhere and drifted apart.

    - ``POSIX rlimits`` (``security.apply_resource_limits``, via ``preexec_fn``
      or the exec shim's ``--rlimits=``). Here ``0`` is a MEANINGFUL, documented
      value: "leave the inherited limit unchanged". Absent falls back to
      ``security._RLIMIT_DEFAULTS``.
    - ``cgroup v2 scope`` (``sandbox.cgroup_scope_argv``, ``TasksMax`` /
      ``MemoryMax`` / ``CPUWeight`` on a transient ``systemd-run --user
      --scope``). Here ``0`` is ILLEGAL -- systemd rejects the property and the
      scope never starts -- so ``0``, absent, or anything out of domain falls
      back to the module default and the ceiling is never left unset. The one
      exception is ``max_cpu_percent``, which is opt-in: unset emits no
      ``CPUQuota`` property at all.
    - ``pytest-xdist worker cap`` (``resource_status``), where ``xdist_auto_cap``
      carries its own three-way sentinel.

    Every field is ``int | None``, and ``None`` means "not configured" -- kept
    distinct from ``0`` precisely because ``0`` is a real value on the rlimit
    path. Values are coerced by :func:`_limit_int`, the ONLY parse site for this
    block; a second one is a defect, and ``test_resource_limits_schema.py``
    fails if one appears.
    """

    max_open_files: int | None = field(
        default=None,
        metadata=_meta(
            "Max open files",
            "RLIMIT_NOFILE: open file descriptors per spawned process. Caps fd "
            "leaks. 0 leaves the inherited limit unchanged; unset uses the "
            "built-in default (1024). Not used by the cgroup path.",
            nullable=True,
        ),
    )
    max_processes: int | None = field(
        default=None,
        metadata=_meta(
            "Max processes",
            "READ BY TWO MECHANISMS with different meanings for 0. As "
            "RLIMIT_NPROC it caps processes for the child's real UID, and 0 "
            "leaves the inherited limit unchanged (the default -- see the "
            "per-UID caveat in security._RLIMIT_DEFAULTS). As the cgroup "
            "TasksMax it counts TASKS (threads) in the scope, where 0 is "
            "rejected by systemd, so 0 or unset means the module default.",
            nullable=True,
        ),
    )
    max_memory_mb: int | None = field(
        default=None,
        metadata=_meta(
            "Max memory (MB)",
            "READ BY TWO MECHANISMS with different meanings for 0. As RLIMIT_AS "
            "it caps virtual address space, and 0 leaves the inherited limit "
            "unchanged (the default -- Node/V8 reserve huge VSZ, see the caveat "
            "in security._RLIMIT_DEFAULTS). As the cgroup MemoryMax it is the "
            "per-scope resident ceiling, where 0 is rejected by systemd, so 0 "
            "or unset means the host-proportional module default.",
            nullable=True,
        ),
    )
    max_cpu_seconds: int | None = field(
        default=None,
        metadata=_meta(
            "Max CPU seconds",
            "RLIMIT_CPU: CPU-seconds per spawned process. 0 leaves the "
            "inherited limit unchanged (the default). Not used by the cgroup "
            "path, which throttles with CPUWeight/CPUQuota instead of killing.",
            nullable=True,
        ),
    )
    cpu_weight: int | None = field(
        default=None,
        metadata=_meta(
            "CPU weight",
            "cgroup CPUWeight for the agent scope: relative CPU share under "
            "contention, not a cap. Accepted range 1-10000; unset or out of "
            "range uses the module default. Emitted only when the cpu "
            "controller is delegated to the user manager.",
            nullable=True,
        ),
    )
    max_cpu_percent: int | None = field(
        default=None,
        metadata=_meta(
            "Max CPU percent",
            "cgroup CPUQuota: a HARD CPU cap, opt-in. Unset or 0 emits no "
            "CPUQuota property at all, because a hard cap slows legitimate "
            "builds. May exceed 100 on a multi-core host (150 = 1.5 cores).",
            nullable=True,
        ),
    )
    max_total_memory_mb: int | None = field(
        default=None,
        metadata=_meta(
            "Max total memory (MB)",
            "cgroup MemoryMax for the whole agents SLICE -- how much every "
            "agent tree may claim together, independent of the per-scope "
            "ceiling. 0 or unset uses the host-proportional module default; "
            "the aggregate ceiling is never left unset.",
            nullable=True,
        ),
    )
    max_total_processes: int | None = field(
        default=None,
        metadata=_meta(
            "Max total processes",
            "cgroup TasksMax for the whole agents SLICE, counting tasks "
            "(threads) across every agent tree. 0 or unset uses the module "
            "default; the aggregate ceiling is never left unset.",
            nullable=True,
        ),
    )
    xdist_auto_cap: int | None = field(
        default=None,
        metadata=_meta(
            "pytest-xdist worker cap",
            "Ceiling for auto-computed pytest-xdist worker counts. -1 (the "
            "default) computes it from available memory, 0 disables the "
            "injection entirely and defers to xdist, and N > 0 pins a fixed "
            "cap.",
            nullable=True,
        ),
    )

    @classmethod
    def from_raw(cls, section: object) -> "ResourceLimitsConfig":
        """Build from a raw ``resource_limits`` dict -- the ONE parse site.

        Accepts whatever ``json.loads`` produced, including ``None`` and a
        non-dict, because the callers are spawn-path readers that must never
        raise: a malformed config has to degrade to defaults, not stop the agent
        from starting. Consumers keep their own interpretation of ``0`` and of
        ``None``; this method only decides what is a usable integer.
        """
        if not isinstance(section, dict):
            return cls()
        return cls(
            max_open_files=_limit_int(section.get("max_open_files"), "max_open_files", lo=0),
            max_processes=_limit_int(section.get("max_processes"), "max_processes", lo=0),
            max_memory_mb=_limit_int(section.get("max_memory_mb"), "max_memory_mb", lo=0),
            max_cpu_seconds=_limit_int(section.get("max_cpu_seconds"), "max_cpu_seconds", lo=0),
            cpu_weight=_limit_int(section.get("cpu_weight"), "cpu_weight", lo=1, hi=10000),
            max_cpu_percent=_limit_int(section.get("max_cpu_percent"), "max_cpu_percent", lo=0),
            max_total_memory_mb=_limit_int(
                section.get("max_total_memory_mb"), "max_total_memory_mb", lo=0
            ),
            max_total_processes=_limit_int(
                section.get("max_total_processes"), "max_total_processes", lo=0
            ),
            xdist_auto_cap=_limit_int(section.get("xdist_auto_cap"), "xdist_auto_cap", lo=-1),
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


@dataclass
class FeishuConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Feishu (Lark/飞书) channel. Requires FEISHU_APP_ID and "
            "FEISHU_APP_SECRET environment variables to be set.",
            tags=["feishu"],
        ),
    )
    allowed_open_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Open IDs",
            "Feishu open_ids allowed to DM the bot (deny-by-default: empty list "
            "authorises nobody). Find your open_id via the Feishu developer console.",
            tags=["feishu"],
        ),
    )
    allow_group: bool = field(
        default=False,
        metadata=_meta(
            "Allow Group Chat",
            "Serve messages from group chats whose chat_id is in allowed_group_ids. "
            "The bot must be @-mentioned in a group to receive the message.",
            tags=["feishu"],
        ),
    )
    allowed_group_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Group IDs",
            "Feishu group chat_ids allowed to drive a turn (requires allow_group=true).",
            tags=["feishu"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a conversation's context passes this, prompt the user to /compact "
            "or /new instead of auto-compacting.",
            tags=["feishu"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this so the window never overflows.",
            tags=["feishu"],
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
            tags=["feishu"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable -- _maybe_notice
        # checks ``pct >= hard`` first. Mirrors WeComConfig. The helper's floor
        # is 1, not 0, because a 0% threshold reads as "always over" and would
        # compact every turn -- a hand-rolled max(0, ...) admits exactly that.
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


_WHATSAPP_GROUP_MODES = ("mention", "rules", "off")
_WHATSAPP_GROUP_COOLDOWN_DEFAULT = 120


def _coerce_whatsapp_groups(raw: object) -> list[dict]:
    """Coerce the whatsapp ``groups`` config value to sanitized rule entries.

    Each entry needs at least a non-empty ``jid``; everything else gets a safe
    default. Unknown ``mode`` values fall back to ``mention`` (never to an
    unprompted-speech mode), and cooldown is clamped to >= 0. Fails closed on
    shape: a non-list yields ``[]``, malformed entries are dropped, duplicate
    JIDs keep the first entry.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        jid = str(entry.get("jid", "")).strip()
        if not jid or jid in seen:
            continue
        seen.add(jid)
        mode = str(entry.get("mode", "mention")).strip().lower()
        if mode not in _WHATSAPP_GROUP_MODES:
            mode = "mention"
        try:
            cooldown = int(entry.get("cooldown_s", _WHATSAPP_GROUP_COOLDOWN_DEFAULT))
        except (TypeError, ValueError):
            cooldown = _WHATSAPP_GROUP_COOLDOWN_DEFAULT
        out.append(
            {
                "jid": jid,
                "name": str(entry.get("name", "")).strip(),
                "mode": mode,
                "rules": str(entry.get("rules", "")).strip(),
                "cooldown_s": max(0, cooldown),
            }
        )
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
    show_thinking: bool = field(
        default=False,
        metadata=_meta(
            "Show Thinking",
            "Post the model's reasoning after each answer as a collapsed, "
            "expandable quote. Off by default: Telegram's rate limit is per chat "
            "and shared with the streaming edits the answer already spends, so "
            "reasoning costs an extra message per turn.",
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
    voice_replies: bool = field(
        default=False,
        metadata=_meta(
            "Voice Replies",
            "Speak each answer as a voice/audio message in addition to the text, "
            "using the global voice_reply provider settings. Off by default: it "
            "costs a second message per turn against Telegram's per-chat rate "
            "budget, and TTS may not be configured. Toggle per conversation with "
            "/voice on|off; this is the default for a new conversation.",
            tags=["telegram"],
        ),
    )
    forum_activation: str = field(
        default=ACTIVATION_ALWAYS,
        metadata=_meta(
            "Forum Activation",
            "When the bot answers inside an allow-listed forum Topic: 'always' "
            "(every message), 'mention' (only when its @handle is used or one of "
            "its own messages is replied to), or 'off' (never). Slack's channel "
            "equivalent defaults to 'mention'; this defaults to 'always' so an "
            "existing forum keeps working after an upgrade instead of going quiet. "
            "Does not apply to a 1:1 DM, which is always served.",
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
class WhatsAppConfig:
    """WhatsApp channel via a QR-linked personal account (WhatsApp Web protocol).

    Pairs as a linked device on the operator's own WhatsApp account — there is
    no bot token. Pairing state lives in a local session database under the
    data home (``whatsapp/session.db``), created by the Settings > Channels QR
    flow. Requires the optional ``whatsapp`` dependency
    (``pip install 'neonize==0.4.3.post0'``; see :mod:`kiro_crew.extras`).

    Uses the unofficial WhatsApp Web protocol; automation on a personal
    account is against WhatsApp's Terms of Service and carries a small risk
    of the linked number being banned. Keep volumes personal-scale.
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the WhatsApp channel (QR-linked personal account over the "
            "WhatsApp Web protocol). Pair a device from Settings > Channels; "
            "needs the 'whatsapp' dependency extra installed.",
            tags=["whatsapp"],
        ),
    )
    dm_policy: str = field(
        default="self",
        metadata=_meta(
            "DM Policy",
            "Who may command the agent in direct chats: 'self' (only the linked "
            "account itself — your own messages, the default), 'allowlist' "
            "(yourself plus allowed_wa_ids), 'open' (any sender), or 'disabled'. "
            "Unknown values deny everyone (fail closed).",
            tags=["whatsapp"],
        ),
    )
    allowed_wa_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed WhatsApp IDs",
            "Phone numbers (digits only, country code, no '+') additionally "
            "permitted to DM the agent when dm_policy='allowlist'. Empty adds "
            "nobody beyond the linked account.",
            tags=["whatsapp"],
        ),
    )
    groups: list[dict] = field(
        default_factory=list,
        metadata=_meta(
            "Group Rules",
            "Per-group participation rules. Each entry: {'jid': group JID "
            "(…@g.us), 'name': display label, 'mode': 'mention' (reply only "
            "when @-mentioned or quoted, the default) | 'rules' (also speak "
            "unprompted when the entry's rules say the agent can genuinely "
            "help) | 'off', 'rules': free-text guidance for when to speak, "
            "'cooldown_s': minimum seconds between unprompted replies "
            "(default 120)}. Groups not listed are ignored entirely.",
            tags=["whatsapp"],
        ),
    )
    db_path: str = field(
        default="",
        metadata=_meta(
            "Session DB Path",
            "Read-only. The pairing session database always lives at "
            "<data home>/whatsapp/session.db, because that path is what the "
            "sensitive-path protection matches: it holds the linked-device keys, "
            "and moving it elsewhere would take the credential out from behind "
            "the one control that stops an agent reading it.",
            tags=["whatsapp"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "Prompt the user to /compact or /new when context passes this percentage.",
            tags=["whatsapp"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context passes this percentage.",
            tags=["whatsapp"],
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
            tags=["whatsapp"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable -- _maybe_notice
        # checks ``pct >= hard`` first. Mirrors WeixinConfig.
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
            "Empty = DMs only. A server channel is denied unless it is listed in "
            "allowed_channel_ids, and a turn there still runs in a thread.",
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
    reactions_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Reactions Enabled",
            "Show phase-aware emoji reactions on Discord messages during processing.",
            tags=["discord"],
        ),
    )
    show_thinking: bool = field(
        default=False,
        metadata=_meta(
            "Show Thinking",
            "Post the model's thinking/reasoning as a subtext note in Discord. "
            "Off by default to keep responses concise.",
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
    allow_group_rooms: bool = field(
        default=False,
        metadata=_meta(
            "Allow Group Spaces",
            "Answer in group spaces as well as direct messages. Off by default: a "
            "reply in a space is visible to every member, including people who are "
            "not on the allow-list, so tool output would leave the DM. A Webex bot "
            "only ever sees messages that @mention it in a space.",
            tags=["webex"],
        ),
    )
    allowed_room_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Room IDs",
            "Webex space IDs the bot may answer in when group spaces are enabled. "
            "Empty = deny all (fail closed), so turning the switch on alone grants "
            "nothing; the sender must ALSO be on the email allow-list.",
            tags=["webex"],
        ),
    )
    reply_in_thread: bool = field(
        default=True,
        metadata=_meta(
            "Reply in Thread",
            "Reply under the message's own thread when it has one, keeping a space "
            "readable. Webex threads are flat, so a reply always attaches to the "
            "thread root.",
            tags=["webex"],
        ),
    )
    wdm_base: str = field(
        default="",
        metadata=_meta(
            "Device Manager Base URL",
            "Override the Webex Device Manager host used for the inbound "
            "WebSocket. Empty (the default) discovers the org's own regional host "
            "per token, which is what a non-US-resident org needs; set this only "
            "to pin a REGIONAL WEBEX host for a network that reaches it but not "
            "the service catalog. Must be an https Webex host (*.wbx2.com, "
            "*.webex.com, *.ciscospark.com) — the bot token rides device "
            "registration, so anything else is refused and discovery is used "
            "instead. An outbound proxy belongs in HTTPS_PROXY, not here.",
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
class IMessageConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the iMessage channel. macOS only, and the gateway must run "
            "on the Mac that is signed in to Messages. Needs no bot and no "
            "token — it drives Messages.app through the local imsg bridge, so "
            "the transport involves no third party. The turn itself still goes "
            "to the configured model provider, as on any channel.",
            tags=["imessage"],
        ),
    )
    db_path: str = field(
        default="",
        metadata=_meta(
            "Messages Database Path",
            "Override the Messages database location. Empty (the default) lets "
            "the bridge use ~/Library/Messages/chat.db. Reading it needs Full "
            "Disk Access for the process the gateway runs as.",
            tags=["imessage"],
        ),
    )
    allowed_handles: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Handles",
            "Phone numbers or Apple ID emails permitted to message the agent. "
            "Empty = deny all (fail closed): anyone who knows this Mac's handle "
            "can send to it. Formatting is ignored, so '+61 400 000 000' and "
            "'+61400000000' are the same handle.",
            tags=["imessage"],
        ),
    )
    service: str = field(
        default="imessage",
        metadata=_meta(
            "Send Service",
            "Which service outbound replies use: 'imessage' (default), 'sms', "
            "or 'auto' to let the bridge fall back to SMS when iMessage is "
            "unavailable. Inbound is unaffected — the channel answers on "
            "whichever service the message arrived over.",
            tags=["imessage"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a conversation's context passes this, prompt the user to "
            "/compact or /new instead of auto-compacting.",
            tags=["imessage"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this, even without a user "
            "decision, so the window never overflows.",
            tags=["imessage"],
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
            tags=["imessage"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable -- _maybe_notice
        # checks ``pct >= hard`` first. Mirrors WebexConfig.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )
        # An unrecognized service would be forwarded to the bridge and rejected
        # per send, turning a typo into a channel that accepts messages and
        # never answers. Fall back to the safe default instead.
        service = (self.service or "").strip().lower()
        self.service = service if service in IMESSAGE_SERVICES else "imessage"


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
class WakaTimeConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the WakaTime integration (send coding-activity heartbeats "
            "and read back stats). Requires the WAKATIME_API_KEY credential "
            "stored in the dashboard secrets vault.",
            tags=["wakatime"],
        ),
    )
    api_base_url: str = field(
        default="",
        metadata=_meta(
            "API Base URL",
            "Override the WakaTime API base URL for a self-hosted, "
            "API-compatible backend (Wakapi, Hackatime). Empty uses the public "
            "WakaTime API at https://wakatime.com/api/v1.",
            tags=["wakatime"],
        ),
    )
