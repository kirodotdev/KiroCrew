"""Configuration section DTOs, defaults, and field-level coercion.

This module owns the sections other specs and tests anchor here: the agent,
crew-record, workspace, session, dashboard (with its tailnet sub-section),
channel, speech-to-text, telemetry, decisions and resource-limit DTOs. It also
re-exports every name of the domain owners it composes: ``config.fields`` (field
metadata and primitive coercion), ``config.memory_sections``,
``config.integration_sections`` and ``config.service_sections``.

The loader imports and re-exports this module's names as its compatibility
facade.  Keep this module one-way: it must not import the loader, schema, or
validation modules, and the owners it re-exports never import it.
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
from kiro_crew.appearance_packs import safe_pack_id as _safe_pack_id
from kiro_crew.config.fields import (  # noqa: F401
    _COLOR_HEX_RE,
    _coerce_int,
    _meta,
    _port_or_unset,
    _safe_bool,
    _safe_color,
    _safe_dict,
    _safe_float,
    _safe_int,
    _safe_list,
    _safe_nonnegative_int,
)
from kiro_crew.config.integration_sections import (  # noqa: F401
    _CONNECT_TIMEOUT_CEILING,
    _CU_DEFAULT_ATTACH_SCREENSHOT,
    _CU_DEFAULT_MAX_TREE_DEPTH,
    _CU_DEFAULT_MAX_TREE_NODES,
    _CU_DEFAULT_SCREENSHOT_JPEG_QUALITY,
    _CU_DEFAULT_SCREENSHOT_MAX_PX,
    _CU_DEFAULT_TEXT_LIMIT,
    _DEFAULT_BACKOFF_MAX,
    _DEFAULT_MAX_RECOVERY,
    _DEFAULT_PROBE_FAILS,
    _DEFAULT_SSH_COMPRESSION,
    _DEFAULT_TUNNEL_BASE_PORT,
    _DEFAULT_WARM_SET_CAP,
    _MAX_RECOVERY_CEILING,
    _MINT_TIMEOUT_CEILING,
    _MINT_TIMEOUT_FLOOR,
    _RECOVER_BACKOFF_CEILING,
    _WARM_SET_CAP_AUTO,
    FORWARD_DECLARED_ENV_DEFAULT,
    ComputerUseConfig,
    ExternalRegistryConfig,
    InstancesConfig,
    McpConfig,
    McpGatewayConfig,
    PublishConfig,
    TunnelConfig,
    _resolve_stub_overrides,
    _resolve_stub_roster,
    _resolve_stub_servers,
)
from kiro_crew.config.memory_sections import (  # noqa: F401
    DEFAULT_AUTO_INGEST_ARTIFACT_KINDS,
    KnowledgeConfig,
    MemoryConfig,
    MemoryStoreConfig,
    SessionSummaryConfig,
    SkillsConfig,
    _coerce_embedding_provider,
    _read_auto_add_documents,
    resolve_memory_store_config,
)
from kiro_crew.config.resolution import _OBSERVED_DEGRADED_SECTIONS, DEGRADED_TAILSCALE
from kiro_crew.config.service_sections import (  # noqa: F401
    DEFAULT_MAX_PARALLEL_STEPS,
    DEFAULT_RUNTIME_CEILING_SECS,
    MAX_RUNTIME_CEILING_SECS,
    CronHistoryConfig,
    HeartbeatConfig,
    MessagingConfig,
    MonitoringConfig,
    TaskRunnerConfig,
    WatchdogConfig,
)
from kiro_crew.constants import DEFAULT_SUBAGENT_MAX_TURNS as _DEFAULT_SUBAGENT_MAX_TURNS
from kiro_crew.constants import SUBAGENT_TIMEOUT_MAX as _SUBAGENT_TIMEOUT_MAX
from kiro_crew.constants import SUBAGENT_TIMEOUT_MIN as _SUBAGENT_TIMEOUT_MIN
from kiro_crew.constants import SUBAGENT_TIMEOUT_SECS as _SUBAGENT_TIMEOUT_SECS
from kiro_crew.effort import EFFORT_LEVELS, is_valid_effort
from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX
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
# Per-session process-tree RSS ceiling (MiB) the cleanup watchdog recycles an
# idle session at. Non-zero by default so a runaway session tree is bounded
# out of the box: fleet gateways were observed at several hundred MB with
# nothing bounding them. 1536 leaves a healthy kiro-cli plus its MCP servers
# (typically 300-600 MiB) a wide margin while still catching a leak before
# it takes the host with it. 0 disables.
DEFAULT_WATCHDOG_RSS_MAX_MB = 1536
# session.reconcile_max_kills — root candidates the runtime reconciler may signal
# the tree of in one pass. Defaults to the budget the arm already ships with, so an
# unconfigured host behaves exactly as before; the field's ceiling equals that same
# value, which makes the knob purely SUBTRACTIVE -- it can withhold signals and
# cannot authorize any the product does not already authorize.
#
# Subtractive on purpose, because the value governs host-side signals and
# ``config.json`` is agent-writable and never passes the dashboard's write gate. A
# knob whose reachable range sat above the shipped default would let a write turn
# killing up; this one cannot.
#
# An operator needs to turn it DOWN because the arm's evidence of abandonment is
# "no record on this data home claims this pid", and that evidence is only as wide
# as the records one process can read. The agent slice is named from a hash of the
# config directory, so every install sharing a data home shares the slice, and a
# runtime whose owner is a different process is claimed only by records that
# process holds. Measured on such a host: 250-504 unowned pids per pass against 4
# genuine strays in 6.5 hours. Setting 0 takes the reading without the signal.
DEFAULT_RECONCILE_MAX_KILLS = 5


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


def deepseek_env_plaintext_keys(raw: object) -> tuple[str, ...]:
    """Env-var names in an ``agent.deepseek_env`` shape whose value is NOT a reference.

    The one rule about this mapping that is enforced at WRITE time rather than at
    spawn: a value is a ``secret://<vault name>`` reference, and anything else is a
    provider key about to be persisted in ``config.json`` — the exposure the whole
    route exists to avoid. ``write_config_atomically`` refuses to publish a document
    whose write INTRODUCES one, or changes the mapping while one stays in it
    (:class:`kiro_crew.config.loader.ConfigWriteRefused`), so a plaintext typed into
    ``config set`` never reaches disk; a plaintext an older build already landed,
    left untouched by a write to some other field, publishes with the key named on
    the log, and the spawn-time validator repeats the check as the second line for
    a file edited by hand.

    Returns NAMES only, sorted, never values: the result is destined for an error
    message that must be safe on a terminal and in a log. A non-dict shape has no
    entries and returns empty; :func:`coerce_deepseek_env` is what drops it.
    """
    if not isinstance(raw, dict):
        return ()
    return tuple(
        sorted(
            str(key)
            for key, value in raw.items()
            if not (isinstance(value, str) and value.startswith(SECRET_URI_PREFIX))
        )
    )


def coerce_deepseek_env(raw: object) -> dict[str, str]:
    """Normalize ``agent.deepseek_env`` to a plain env-var-name -> value mapping.

    TYPE coercion ONLY, and that split is deliberate. What a name may BE for this
    mapping — a POSIX identifier, inside the harness's own child-environment scrub
    class, not a name Kiro Crew or the harness owns — is checked at SPAWN, in the
    DeepSeek arm, where a bad entry REFUSES the session with a message naming the
    offending env-var key (``acp/client.py``). Dropping such an entry here instead
    would hand the operator a harness with no provider key, a config file whose
    entry silently disappeared on the next write, and no error naming why. So the
    only thing refused here is a shape the dataclass cannot hold. The VALUE rule —
    a ``secret://`` reference, never a plaintext key — is the exception, enforced
    earlier still, at the publish floor (:func:`deepseek_env_plaintext_keys`),
    because a plaintext that reaches disk is already the defect.

    Nothing is stripped either: a name with surrounding whitespace is not a POSIX
    identifier, and the spawn-time refusal says so by name rather than quietly
    repairing it into a different variable than the operator wrote.
    """
    if not isinstance(raw, dict):
        return {}
    return {
        key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)
    }


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


def coerce_refusal_fallback_model(raw: object) -> str:
    """Normalize the content-filter fallback model (agent.refusal_fallback_model).

    Same three shapes as :func:`coerce_fallback_model` but with the OPPOSITE
    junk default: ``""`` (the default) disables the feature — a refusal then
    surfaces exactly as it does today — so absent/junk input (``None``,
    non-string) and an id the registry maps to ``""`` all collapse to ``""``
    (off), never to a silently-enabled value. ``"auto"`` means "retry on the
    model the provider's refusal envelope recommends, when it names one"; a
    concrete id is normalized through :func:`model_registry.to_provider_id`
    for the ``acp`` provider.
    """
    if raw is None or not isinstance(raw, str):
        return ""
    s = raw.strip()
    if not s:
        return ""
    if s.lower() == "auto":
        return "auto"
    return model_registry.to_provider_id(s, "acp") or ""


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
#: Agent lifecycle states a crew face may react to. A per-state override keys
#: on one of these exactly; any other key is dropped, so a version-skewed or
#: typo'd state name cannot smuggle an unbounded key set into config.json.
_AVATAR_STATES = ("working", "done", "error")
#: Built-in reaction motions a GHOST may play, per state. The vocabulary is
#: pinned here rather than left open like a trait value because a motion is a
#: named animation the frontend implements: an unknown name has no rendering to
#: resolve to, so it carries nothing and is dropped. ``"none"`` is a real value
#: (explicit stillness), distinct from an absent state, so one state can opt out
#: of a motion the others use. ``working`` has no entry -- the ghost's working
#: animation is its idle breathing, and a reaction fires on a transition.
_AVATAR_MOTIONS: dict[str, tuple[str, ...]] = {
    "done": ("none", "bounce", "nod", "sparkle"),
    "error": ("none", "shake", "cross-eyes", "droop"),
}
#: Preset cue names a per-state sound may select. `"none"` is a real value
#: (explicit silence), distinct from an absent state (the default, also
#: silent) -- so a crew can opt one state out of a fleet-wide cue.
_AVATAR_SOUNDS = ("none", "chime", "ding", "blip", "pop", "pulse")


#: The only trait axes a per-state ghost expression may move. The identity axes
#: (brows/accessory/prop/tile/blush/flip) are excluded: a crew must stay
#: recognisable as itself while its expression changes.
_AVATAR_EXPRESSION_AXES = ("eyes", "mouth")


def _safe_expressions(value: object) -> dict:
    """Return validated per-state ghost eyes/mouth picks, or ``{}``.

    The key ``motions`` supersedes, kept round-tripping for exactly as long as the
    shipped renderer still draws it: the frontend writes and paints a ghost's
    per-state eyes/mouth today, so a validator that dropped the key would erase a
    visible choice on an unrelated save with no way back. The sibling frontend
    change that removes the picker is where this retires. Same forgiveness as the
    trait coercer -- junk is dropped, never refused.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for state in _AVATAR_STATES:
        raw = value.get(state)
        if not isinstance(raw, dict):
            continue
        axes = {}
        for axis in _AVATAR_EXPRESSION_AXES:
            v = raw.get(axis)
            if isinstance(v, str) and v:
                axes[axis] = v[:_AVATAR_TRAIT_MAX_LEN]
        if axes:
            out[state] = axes
    return out


def _safe_motions(value: object) -> dict:
    """Return validated per-state ghost motions, or ``{}``.

    Same forgiveness as the trait coercer: junk is dropped silently rather than
    refused, because config.json is hand-editable and a malformed motion must
    never cost the crew its otherwise-valid avatar. Only the states
    :data:`_AVATAR_MOTIONS` names carry a motion, and only a value from that
    state's own tuple survives -- ``{"done": "shake"}`` is dropped, because
    ``shake`` is the error vocabulary and a bounce-on-error is a different
    reaction than the author wrote.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for state, allowed in _AVATAR_MOTIONS.items():
        v = value.get(state)
        if isinstance(v, str) and v in allowed:
            out[state] = v
    return out


def _safe_sounds(value: object) -> dict:
    """Return validated per-state sound cues, or ``{}``.

    Unlike a trait value, a cue name IS pinned to a vocabulary: it selects a
    shipped preset rather than naming an option the renderer can resolve to
    absent, so an unknown name has no meaning to carry and is dropped.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for state in _AVATAR_STATES:
        v = value.get(state)
        if isinstance(v, str) and v in _AVATAR_SOUNDS:
            out[state] = v
    return out


def _safe_avatar(value: object) -> dict:
    """Return a validated per-crew avatar override, or ``{}`` on junk.

    Each tier owns its own source of motion and sound, so what a record may
    carry depends on its ``kind``:

    - ``{"kind": "ghost", "traits"?: {...}, "motions"?: {...}, "sounds"?: {...},
      "expressions"?: {...}}`` — the built-in face. ``traits`` pins it trait-by-trait instead of deriving
      it from the crew name, and may be absent (or empty) when the override
      carries only reactions: that spelling means "name-derived face, plus these
      per-state reactions", and the record omits the key entirely rather than
      storing ``{}``. ``motions`` picks a built-in reaction animation per state
      from :data:`_AVATAR_MOTIONS`; ``sounds`` picks a synthesized preset cue per
      state from :data:`_AVATAR_SOUNDS`; ``expressions`` (a per-state eyes/mouth
      pick, which ``motions`` supersedes) still round-trips because the shipped
      renderer still draws it -- see :func:`_safe_expressions`.
    - ``{"kind": "image"}`` (optional int ``v``, optional ``file``, optional
      ``sounds``) — the crew wears an uploaded picture, served from
      ``GET /api/agents/{name}/avatar``. A picture has no animation to play, so it
      carries no ``motions``; it does still carry a cue, because the shipped
      renderer plays a crew's cue whatever face it wears, and dropping the key
      here would silence a crew on an unrelated save with no way to restore it.
      The file itself lives under the data home's agent-fenced ``run/avatars/``
      dir; the config field only marks the choice. ``v`` is the upload's cache-busting stamp (file
      mtime, nanoseconds): the frontend appends it as ``?v=`` so a replaced
      picture is re-fetched without waiting out the browser cache. ``file`` pins
      the exact committed file — a ``<digest>.<ext>`` suffix under the crew's
      stem. Every install lands at a digest-named path, so a replacement never
      overwrites the committed file before the config save commits it, and
      serving resolves only the pinned file.
    - ``{"kind": "pack", "id": "<pack id>"}`` — the crew wears an appearance
      pack from the crew library (``GET /api/appearances``). ``id`` is
      validated by :func:`kiro_crew.appearance_packs.safe_pack_id`, the same
      rule the pack store applies to a directory name, so a value stored here
      can always be looked up. A junk id collapses the WHOLE override to
      ``{}``: an unrenderable pack reference is worse than the default face.
      Whether the pack still EXISTS is deliberately not checked — config load
      must not touch the disk — so a dangling id renders as the name-derived
      ghost on the client. A pack carries its own per-state art
      (``GET /api/appearances/{id}/slot/{slot}``) and its own per-state audio
      (``GET /api/appearances/{id}/sound/{state}``), so it needs no ``motions``
      here. It keeps accepting ``sounds`` for now, for the same reason the picture
      tier does: the shipped renderer plays a crew-record cue on every tier, so
      retiring the key before that renderer stops reading it would take away a
      sound the user chose. Once the pack's own audio is what plays, the crew
      record has no cue to hold and the key retires with that change.

    ``<state>`` is one of ``working``, ``done``, ``error``. A key is omitted
    from the record when validation leaves it empty, so a stored avatar never
    carries ``{}`` for one, and a key illegal on this tier is DROPPED rather
    than refused — the same forgiveness every other field here has, because a
    hand-written or version-skewed record must not cost the crew its face. Two
    keys are deliberately NOT retired that way even though ``motions`` supersedes
    one of them: ``sounds`` is audible on every tier today, and a ghost's
    ``expressions`` is drawn today, and a value a user can see or hear is not
    dropped ahead of the renderer that shows it. ``expressions`` round-trips on
    picture and pack too, even though neither has a face to move: the shipped
    builder still SUBMITS it there, and a crew that switches from ghost to
    picture and back would otherwise lose the picks in between. Only ``motions``
    is tier-gated, because nothing has ever written it outside the ghost.

    Empty means "no override" — the frontend keeps rendering the name-seeded
    face. config.json is hand-editable (and agent-writable), so junk collapses
    to ``{}`` rather than crashing the load.

    Trait *values* are deliberately not checked against the frontend's trait
    vocabulary: the renderer resolves an unknown option to "absent"
    (``EYES[k] ?? ''``), and keeping the vocabulary in one place (the style
    module) means a new hat needs no backend release. ``tile`` is the one
    exception — it is interpolated into SVG markup, so it is pinned to a hex
    color by the same validator session_color uses. A motion or cue name is
    pinned, because unlike a trait it names an animation or a synthesizer
    preset rather than an option a renderer can resolve to nothing.
    """
    if not isinstance(value, dict):
        return {}
    # Legal on every tier: the shipped renderer reads a crew-record cue
    # kind-agnostically, so this is the one reaction key that is not the ghost's
    # alone. `motions` IS ghost-only -- a picture has no face to move and a pack
    # animates from its own files.
    sounds = _safe_sounds(value.get("sounds"))
    expressions = _safe_expressions(value.get("expressions"))
    if value.get("kind") == "image":
        out: dict[str, object] = {"kind": "image"}
        v = value.get("v")
        # bool is an int subclass; a hand-written `"v": true` must not pass.
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            out["v"] = v
        f = value.get("file")
        if isinstance(f, str) and _AVATAR_FILE_PIN_RE.fullmatch(f):
            out["file"] = f
        if expressions:
            out["expressions"] = expressions
        if sounds:
            out["sounds"] = sounds
        return out
    if value.get("kind") == "pack":
        ident = _safe_pack_id(value.get("id"))
        if ident is None:
            # No canonical "pack with no id" spelling exists: a pack avatar IS
            # its id, so a missing or malformed one leaves nothing to render.
            return {}
        pack: dict[str, object] = {"kind": "pack", "id": ident}
        if expressions:
            pack["expressions"] = expressions
        if sounds:
            pack["sounds"] = sounds
        return pack
    if value.get("kind") != "ghost":
        return {}
    raw = value.get("traits")
    traits: dict[str, object] = {}
    if isinstance(raw, dict):
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
        # builder cannot produce it (Apply always carries the seeded
        # defaults), so it only arrives via hand-written config or direct API
        # use — drop it to the one canonical "no traits" spelling instead of
        # storing a third state.
        if all(not v for v in traits.values()):
            traits = {}
    ghost: dict[str, object] = {"kind": "ghost"}
    if traits:
        ghost["traits"] = traits
    motions = _safe_motions(value.get("motions"))
    if motions:
        ghost["motions"] = motions
    if expressions:
        ghost["expressions"] = expressions
    if sounds:
        ghost["sounds"] = sounds
    # `kind` alone carries no override — a bare ghost is the name-derived face,
    # which is what an absent field already means.
    if len(ghost) == 1:
        return {}
    return ghost


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
        metadata=_meta(
            "Approval Mode",
            "Tool approval mode. Every channel dispatcher resolves it once at start "
            "(with the CLI --approval override), so a change takes effect at the "
            "next restart.",
            enum=["auto", "interactive"],
            restart=True,
        ),
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
    refusal_fallback_model: str = field(
        default="",
        metadata=_meta(
            "Refusal fallback model",
            "Model the current message is retried on ONCE when the active "
            "model's content filter declines it (refusal / CONTENT_FILTERED). "
            "Empty ('', the default) disables the retry: the refusal card "
            "surfaces exactly as before. A concrete model id (as advertised "
            "by the provider, e.g. 'claude-opus-4.8') retries that single "
            "message on it and restores the primary model on the next turn; "
            "'auto' retries on the model the provider's refusal envelope "
            "recommends, when it names one. The retry is announced in chat — "
            "never silent — and a refusal from the fallback too is terminal.",
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
    deepseek_env: dict[str, str] = field(
        default_factory=dict,
        metadata=_meta(
            "DeepSeek Harness provider keys",
            "Provider keys handed to the DeepSeek Harness ('deepseek' backend) as "
            "environment variables at spawn, mapping an environment-variable NAME "
            "to a 'secret://<vault name>' reference. The harness resolves a "
            "provider credential from its inherited environment above its own "
            "credential files, so this is how it reaches a hosted model without "
            "Kiro Crew leaving those files readable inside the sandbox. Store the "
            "key under Settings > Secrets, then map it here, e.g. "
            '{"DEEPSEEK_API_KEY": "secret://my-dsh-key"}. Any provider name the '
            "harness knows works (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...). A "
            "plaintext value is REFUSED at write time -- the config write itself "
            "fails, so no credential is ever stored in config.json -- and so, at "
            "spawn, is a name the harness would forward to its own shell "
            "children, a name Kiro Crew owns, or one Kiro Crew's agent "
            "environment scrub strips; the DeepSeek session is refused before it "
            "starts with a message naming the offending key. Empty means no key: "
            "a model served locally on this machine needs none. Ignored by every "
            "other backend.",
        ),
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
            "isolation (namespace on Linux, sandbox-exec on macOS) at the "
            "standard tier and automatically defers to kiro-cli's internal "
            "sandbox on macOS when it is enabled (kiro-cli >= 2.13; nested "
            "seatbelt causes EPERM). The standard tier deliberately leaves "
            "~/.aws, ~/.ssh and ~/.kube visible to the agent's shell so the aws "
            "CLI, boto3 credential_process, git-over-SSH and kubectl keep "
            "working; the file tools still refuse those paths. Set to 'strict' "
            "to also hide ~/.aws (including ~/.aws/sso/cache, kiro-cli's grant "
            "store for OAuth-connected remote MCP servers), ~/.ssh (except "
            "known_hosts), ~/.kube and ~/.config/gh, plus the credential files "
            "~/.npmrc, ~/.pypirc, ~/.netrc and ~/.git-credentials, from every "
            "agent subprocess -- opt-in, and inside the agent it breaks the aws "
            "CLI, boto3, git-over-SSH, gh, kubectl, npm/pip registry auth, "
            ".netrc HTTPS auth, the git credential store and remote-MCP OAuth "
            "for the same reason. Like every value of this key, a change applies "
            "to sessions started after it; a session already running keeps the "
            "tier it was spawned with until it ends. 'strict' changes "
            "nothing where Kiro Crew applies no sandbox of its own: Windows has "
            "no OS backend, and a macOS spawn delegated to kiro-cli's internal "
            "sandbox is confined by that profile instead. Set to 'off' to skip "
            "Kiro Crew's own OS-level sandbox -- delegation to kiro-cli's "
            "internal sandbox still fires on macOS if it is enabled, and a "
            "SECURITY warning is logged when neither layer is active.",
            enum=["auto", "strict", "off"],
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
            "backend (fail-open). When false, wrap_argv raises if no sandbox "
            "backend is available and mode is not 'off', preventing unsandboxed "
            "execution entirely (fail-closed). This is distinct from "
            "sandbox_allow_no_isolation which only controls warning severity — "
            "this field controls whether execution proceeds at all. "
            "A LOADED config carries the effective policy: the loader resolves an "
            "undeclared key through config.loader.unsandboxed_exec_platform_default() "
            "— allow on Windows, which has no backend that any operator action could "
            "install, and fail-closed everywhere else, where a missing backend is "
            "broken or one profile away from working. A declared value always wins in "
            "both directions, and a governance sandbox.min_level floor outranks the "
            "declaration. The resolution is folded into the VALUE rather than keyed on "
            "key presence so a full-document save() — which serializes every field — "
            "cannot turn 'never decided' into a declared lockdown. This dataclass "
            "default stays platform-independent so the committed config-schema "
            "snapshot is identical on every platform; the one caller that writes a "
            "document from a directly constructed config (the first-run default write "
            "in cli_server) resolves the platform default explicitly before saving. "
            "`kirocrew setup` surfaces the decision on a backend-less host, offering "
            "the opt-in where the default is fail-closed and stating the exposure plus "
            "offering the opt-out where it is allow, and writes nothing unless the "
            "operator answers yes.",
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
    apps_ui_stream_timeout_secs: int = field(
        default=30,
        metadata=_meta(
            "App UI Stream Timeout",
            "Total transfer deadline, in seconds, for ONE response body on the "
            "unauthenticated /apps/{name}/ui/ route. Clamped to 5..600; a value "
            "that is not a whole number loads as 30. Read per request, so an "
            "edit applies without a restart. There is no off switch: the route "
            "holds eight descriptor permits, and this deadline is what stops a "
            "client that quits draining its socket from holding one for as long "
            "as it stays connected — eight such clients would otherwise stop "
            "every app UI on the host. Raise it only when a real transfer on "
            "this host needs longer than the default allows.",
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
            restart=True,
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
            restart=True,
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
    tool_search: bool = field(
        default=True,
        metadata=_meta(
            "MCP Tool Search",
            "Load MCP tool specs on demand (search-and-call) instead of sending "
            "every tool definition each turn, keeping the context window clear "
            "when many MCP servers are configured. kiro-cli backend only. "
            "Deferral only starts once the specs cross tool_search_min_pct or "
            "tool_search_min_tokens; disabling reverts to sending full tool "
            "specs. Kiro Crew's OWN servers are exempt and always send full "
            "specs, whatever this is set to: loading one mid-turn would change "
            "the tools list a thinking block's signature is bound to and the "
            "provider would reject the conversation. No effect on an alternate "
            "ACP backend.",
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
            "scheduled cron firings to the next tick and defer new subagent "
            "spawns (they stay queued in the task store and are re-checked "
            "after admit_wait_secs) until memory frees. Manually triggered cron "
            "runs, in-flight subagents, and direct chat turns are never gated; "
            "an unreadable probe admits (fail-open). Set false to make the "
            "critical posture advisory-only.",
        ),
    )
    task_queue_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Durable Task Queue",
            "Persist every accepted subagent spawn to $KIROCREW_HOME/tasks/tasks.db "
            "before its id is returned, so accepted work survives a gateway crash "
            "and memory pressure defers a spawn instead of refusing it. Set false "
            "to fall back to the in-memory spawn queue for one release; tasks.db "
            "is left in place and unread.",
        ),
    )
    task_dispatch_window: int = field(
        default=64,
        metadata=_meta(
            "Task Dispatch Window",
            "Maximum number of queued spawns the gateway keeps in memory at once; "
            "the rest wait as rows in tasks.db and are read in FIFO order as the "
            "window drains. 2000 accepted tasks are 2000 rows and this many "
            "Python objects. Clamped to 1..4096.",
            restart=True,
        ),
    )
    task_store_journal_mode: str = field(
        default="auto",
        metadata=_meta(
            "Task Store Journal Mode",
            "SQLite journal mode for tasks.db: 'auto' picks WAL only on a volume "
            "DETECTED local, and DELETE both on a detected network filesystem and "
            "when detection cannot tell -- WAL needs local shared memory, which "
            "SMB/NFS does not provide, so an undecidable volume takes the slower "
            "correct mode rather than risking the store. 'wal' or 'delete' force "
            "one and skip the detection, so a local disk whose mount table is "
            "unreadable keeps WAL by declaring it. Unknown values read as 'auto'.",
            restart=True,
        ),
    )
    admit_wait_secs: int = field(
        default=30,
        metadata=_meta(
            "Admit Wait (seconds)",
            "How long an admitted task may wait for its resources before it goes "
            "back to queued, and how long a spawn deferred by the memory posture "
            "gate waits before it is re-checked. Clamped to 1..3600.",
            restart=True,
        ),
    )
    start_collect_timeout_secs: int = field(
        default=300,
        metadata=_meta(
            "Start Collect Timeout (seconds)",
            "After a session start times out, how long the start collector keeps "
            "the task in 'recovering' to adopt a late session/new response before "
            "the start is retried. Reserved for the session-start gate; clamped "
            "to 10..3600.",
            restart=True,
        ),
    )
    lane_weights: dict[str, int] = field(
        default_factory=dict,
        metadata=_meta(
            "Lane Weights",
            "Per-lane weight overrides for the task dispatcher, keyed by lane "
            "(a root session key, or 'system'). Unlisted lanes weigh 1. Values "
            "are clamped to 1..64. Weights shape the share of picks, never a hard cap: a "
            "lane with nothing pending costs the others nothing.",
        ),
    )
    child_reserve: int = field(
        default=1,
        metadata=_meta(
            "Child Reserve",
            "Execution slots a top-level (depth-0) task may never take while a "
            "nested task is queued or a parent is waiting on its children. Only "
            "children and resuming parents may use them, so a fleet of parents "
            "can never hold every slot with no child able to start; while a "
            "parent waits, an adaptive cap is also lifted to at least "
            "adaptive_floor + child_reserve (never above max_subagents). 0 "
            "disables the reserve. Clamped to 0..8.",
        ),
    )
    recovery_backoff_base_secs: float = field(
        default=2.0,
        metadata=_meta(
            "Recovery Backoff Base (seconds)",
            "First retry delay of the shared recovery ladder (tool call, backend, "
            "ACP runtime) and of a coordinated dependency wait; each further "
            "attempt doubles it with jitter. One schedule for every layer, so "
            "layers never retry in lock-step; snapshotted once at gateway start. "
            "The gateway-daemon supervisor keeps its own pinned floor. "
            "Clamped to 0.1..60.",
            restart=True,
        ),
    )
    recovery_backoff_max_secs: float = field(
        default=120.0,
        metadata=_meta(
            "Recovery Backoff Cap (seconds)",
            "Longest delay between two recovery attempts on the shared ladder or "
            "a dependency wait; a server-stated retry hint is honoured up to this "
            "cap. Snapshotted once at gateway start. Clamped to 1..3600 and never "
            "below the base.",
            restart=True,
        ),
    )
    session_start_concurrency: int = field(
        default=2,
        metadata=_meta(
            "Session Start Concurrency",
            "How many ACP session/new requests may be outstanding at once per "
            "gateway event loop (the SessionStartGate). session/new blocks while "
            "the backend initializes the session's MCP servers, so a burst of "
            "subagent starts on one shared runtime slows every start until the "
            "budget is hit; queued starts wait in FIFO order and their queue time "
            "is not counted against the start budget or the startup watchdog. A "
            "fixed bound, not adaptive: the adaptive loop is the MCP gateway spawn "
            "gate and the execution-cap controller. Clamped to 1..64.",
            restart=True,
        ),
    )
    adaptive_concurrency: bool = field(
        default=True,
        metadata=_meta(
            "Adaptive Concurrency",
            "Run the adaptive concurrency controller: a runtime execution cap "
            "beneath max_subagents (the ceiling, never written) that halves on "
            "corroborated host pressure (event-loop lag, low memory, fd/process "
            "counts, attributable start timeouts, slow starts on several MCP "
            "servers) and earns +1 back per clean window, plus the same shaping "
            "for the MCP gateway daemon's spawn gate. A fresh gateway starts at "
            "min(max_subagents, adaptive_initial) and earns its way up. Set false "
            "to run at the user cap only.",
        ),
    )
    adaptive_concurrency_mode: str = field(
        default="aimd",
        metadata=_meta(
            "Adaptive Concurrency Mode",
            "'aimd': multiplicative decrease / additive increase with pause-and-"
            "probe. 'fixed': both caps pinned at their initial values -- a plain "
            "semaphore -- the one-flip reversal if the controller is seen to "
            "oscillate.",
            enum=["aimd", "fixed"],
        ),
    )
    adaptive_floor: int = field(
        default=1,
        metadata=_meta(
            "Adaptive Floor",
            "Lowest execution cap the controller may shrink to under sustained "
            "pressure (a pause takes new grants to 0 temporarily). Clamped to "
            "1..64.",
        ),
    )
    adaptive_initial: int = field(
        default=4,
        metadata=_meta(
            "Adaptive Initial Cap",
            "Execution cap a fresh gateway starts at, bounded by max_subagents. "
            "Healthy work and queued demand let the controller raise it; see "
            "adaptive_slow_start for growth rules. Clamped to 1..64.",
        ),
    )
    adaptive_slow_start: bool = field(
        default=True,
        metadata=_meta(
            "Adaptive Slow Start",
            "Until the gateway first meets corroborated host pressure, let the "
            "execution cap DOUBLE per clear 5-second window (on one completion "
            "and real demand) instead of climbing +1 per clear 30-second window, "
            "bounded by max_subagents and by what this host's memory and CPU "
            "size the cap at. The first pressure ends slow start for the life "
            "of the process. Set false to climb +1 per clear 30-second window "
            "from the start. Slow start earns an increase on one completion plus "
            "real demand; congestion avoidance earns each +1 after one full wave "
            "of the CURRENT cap completes (at most 20 runs). Alternatively, "
            "fresh stream progress with queued work and measured headroom can "
            "earn one probe slot per clear window without a completion; this "
            "never earns doubling or relaxes the initialization gate.",
        ),
    )
    controller_sample_secs: int = field(
        default=5,
        metadata=_meta(
            "Controller Sample Interval (seconds)",
            "How often the adaptive controller samples the host and the spawn "
            "gate. Clamped to 1..300.",
        ),
    )
    dependency_max_attempts: int = field(
        default=20,
        metadata=_meta(
            "Dependency Max Attempts",
            "Coordinated retries a dependency scope gets before every task "
            "waiting on it is failed with the reason. One probe per attempt for "
            "the whole scope, not one per waiting task. Clamped to 1..1000.",
        ),
    )
    dependency_wait_deadline_secs: int = field(
        default=3600,
        metadata=_meta(
            "Dependency Wait Deadline (seconds)",
            "Wall-clock ceiling a task may wait on one dependency scope before "
            "it is failed with the reason; 0 disables the clock and leaves only "
            "the attempts cap. Clamped to 0..86400.",
        ),
    )
    dependency_wake_per_tick: int = field(
        default=0,
        metadata=_meta(
            "Dependency Wake Per Tick",
            "How many waiting tasks a recovered dependency releases per wake "
            "tick, after the single probe that confirms recovery. 0 = the "
            "current effective admission capacity, so a recovered dependency "
            "never replays every waiter at once. Clamped to 0..4096.",
        ),
    )
    dependency_wake_spacing_secs: float = field(
        default=1.0,
        metadata=_meta(
            "Dependency Wake Spacing (seconds)",
            "Pause between wake ticks while a recovered dependency's waiters are "
            "released in capacity-sized batches. Clamped to 0..60.",
        ),
    )
    interactive_command_policy: str = field(
        default="cancel",
        metadata=_meta(
            "Interactive Command Policy",
            "What the tool-stall watchdog does when a stalled shell command is "
            "classified as waiting for input (a pager, editor, REPL or confirm "
            "prompt). 'cancel' ends that tool call non-lethally and re-drives the "
            "turn with a non-interactive hint; 'wait' announces a waiting_input "
            "status once and keeps the turn open for real input, bounded by the "
            "turn's own ceiling. Neither ever answers the prompt itself. Read "
            "when a session handle is created and on config hot-apply.",
            enum=["cancel", "wait"],
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
        default=14400,
        metadata=_meta(
            "Chat Turn Timeout (secs)",
            "Wall-clock ceiling for one chat turn. This is a runaway backstop, "
            "so it is clamped to 300s..86400s (24h) and can never be disabled. "
            "The 4h default covers the longest single turn the shipped budgets "
            "produce (a 90-minute test command plus a fix and a re-run, or a "
            "blocking subagent wave at its 2h wait cap); the ACP transport's "
            "prompt wait follows it. Hitting the ceiling is visible: the turn ends "
            "with a card naming the limit. For work spanning many hours or days, "
            "prefer monitor/goal loops — they end the turn between cycles and "
            "survive restarts, which a single marathon turn cannot.",
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
    member_dispatch: bool = field(
        default=True,
        metadata=_meta(
            "Member Dispatch",
            "Let a crew member's DM session open and drive worker sessions it "
            "creates, even when Session Control is off. On by default, because "
            "dispatching work into workers is the crew-member operating model, "
            "not an opt-in: this is the zero-configuration contract. Turn this "
            "off to put member callers back under the Session Control switch, so "
            "an operator who withdrew session control keeps member DM threads "
            "chat-only without disabling the member. When on, a member's reach is "
            "still bounded to sessions it created itself (creator ownership), the "
            "same fence that binds it when Session Control is on.",
        ),
    )
    crew_panel: bool = field(
        default=True,
        metadata=_meta(
            "Crew Dashboard",
            "Let a crew member publish its own webview, shown in that member's "
            "drawer on the Crew page. A member sends a JSON object and names a "
            "template that renders it, so a long-running crew can say what it is "
            "holding, which worker is stuck and what needs a decision, to someone "
            "who is not reading its transcript. On by default, because a crew that "
            "cannot be watched is the thing this surface exists to fix. The tools "
            "come from the kirocrew-panel MCP server, so the grant follows the "
            "same rule as every other MCP server: an agent that does not mount it "
            "never has them. Turn this off to withdraw the capability from every "
            "member at once without editing each spec, and the withdrawal is "
            "immediate: the publish route reads this switch on every call, so a "
            "member whose session was already running loses the panel too. A "
            "member writes only its OWN panel: the server resolves the publishing "
            "crew from the calling session and takes no crew or session argument, "
            "and a subagent has no panel to write.",
        ),
    )
    subagent_cost_gb: float = field(
        default=0.5,
        metadata=_meta(
            "SubAgent Memory Cost (GB)",
            "Free memory (GB) each sub-agent start must find on top of the "
            "admission floor; also the per-agent fallback used to auto-size the "
            "cap until a learned value accumulates. Raise it on hosts whose "
            "runtimes settle heavier.",
        ),
    )
    subagent_cpu_cost_cores: float = field(
        default=1.0,
        metadata=_meta(
            "SubAgent CPU Cost (cores)",
            "Deprecated and inert: the subagent cap is sized from host memory "
            "only, because over-committing memory is an unrecoverable OOM while "
            "over-committing CPU only slows work the adaptive controller already "
            "backs off from. Preserved on load and save so an existing config is "
            "not rewritten out from under the operator.",
            deprecated=True,
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
        default=0.25,
        metadata=_meta(
            "SubAgent Spawn Stagger (seconds)",
            "Delay between successive subagent spawns (initial fill and queued "
            "drain) to bound cold-start CPU/memory spikes. Starts stay "
            "serialized; the interval only decides how fast a wide fan-out "
            "fills. Raise it if this "
            "host or the model provider is the bottleneck -- a spawn still has "
            "to clear spawn_min_memory_gb and the host budget, and the adaptive "
            "controller cuts the cap on real pressure, so this is a smoothing "
            "interval rather than the memory guard.",
        ),
    )
    subagent_max_turns: int = field(
        default=_DEFAULT_SUBAGENT_MAX_TURNS,
        metadata=_meta("SubAgent Max Turns", "Default tool-call budget per subagent."),
    )
    subagent_timeout_secs: int = field(
        default=_SUBAGENT_TIMEOUT_SECS,
        metadata=_meta(
            "SubAgent Timeout (seconds)",
            "Wall-clock timeout per subagent execution. 0 uses the same default. "
            f"Clamped to {_SUBAGENT_TIMEOUT_MIN}s..{_SUBAGENT_TIMEOUT_MAX}s at load "
            "(0 is preserved). Raising it past 2 hours only helps the "
            "fire-and-forget spawn_run path: a BLOCKING spawn_sub_agents call "
            "collects for at most 2 hours whatever this is set to, so use "
            "spawn_run for work longer than that.",
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
        # Same defensive TYPE coercion for the DeepSeek provider-key map. What its
        # names and values may BE is refused at spawn, by name -- see
        # coerce_deepseek_env.
        self.deepseek_env = coerce_deepseek_env(self.deepseek_env)
        # Same defensive coercion for the throttle-fallback model: normalize to
        # ""/"auto"/acp id, so consumers can trust the stored shape.
        self.fallback_model = coerce_fallback_model(self.fallback_model)
        # And for the content-filter fallback model — same shapes, junk
        # collapses to "" (off) rather than "auto".
        self.refusal_fallback_model = coerce_refusal_fallback_model(self.refusal_fallback_model)

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
            "automatically send a 'continue' nudge on the same session "
            "(transcript-visible, bounded by Max Auto-Continues on Empty "
            "Response).",
        ),
    )
    empty_response_max_continues: int = field(
        default=1,
        metadata=_meta(
            "Max Auto-Continues on Empty Response",
            "How many 'continue' nudges may run back to back before the "
            "runner gives up and asks for a message (1-10). Above 1 the "
            "recovery notice shows progress ('recovery 2 of 3'). Useful "
            "during provider-instability windows where each continuation "
            "makes real progress before failing the same way. Only applies "
            "while Auto-Continue on Empty Response is enabled.",
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
            "-1 disables cleanup (manage deletion manually). The same window collects "
            "a closed session's crew log, and with the crew log on that is where "
            "Issue Radar keeps each repository's shared skip memory and its crews' "
            "open work items.",
            nullable=True,
        ),
    )
    watchdog_rss_max_mb: int = field(
        default=DEFAULT_WATCHDOG_RSS_MAX_MB,
        metadata=_meta(
            "Watchdog RSS Limit (MiB)",
            "Recycle a session when its process tree resident memory exceeds "
            "this many MiB (default 1536). 0 disables. Busy sessions (turn in "
            "flight) are never recycled.",
        ),
    )
    reconcile_max_kills: int = field(
        default=DEFAULT_RECONCILE_MAX_KILLS,
        metadata=_meta(
            "Reconciler Kill Budget (per pass)",
            "Unowned root candidates the runtime reconciler may signal the process "
            "tree of in one pass, at most 5 -- one candidate can signal several "
            "processes. Lower it where more than one install shares this data "
            "home, since a runtime owned by another install has no record here and "
            "reads as unowned. 0 makes the arm observe-only: it still publishes the "
            "leak reading and audits each candidate it would have signalled.",
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
        metadata=_meta(
            "Command",
            "Slack slash command trigger word.",
            # Boot-read: the trigger is registered in the Slack app MANIFEST, so
            # a local reload cannot make Slack route a new word. Applying it
            # in-process would only change the help text and report success for a
            # command that still does not exist on Slack's side.
            restart=True,
        ),
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
    dm_single_session: bool = field(
        default=False,
        metadata=_meta(
            "DM Single Session",
            "Treat each 1:1 DM as one continuous conversation instead of starting "
            "a new session per top-level message. Replies post at channel root "
            "rather than in a thread. Threaded replies, group channels and group "
            "DMs are unaffected. Off by default: turning it on routes the next DM "
            "to a different session than the previous one.",
            tags=["slack"],
        ),
    )
    home_tab_sessions_per_kind: int = field(
        default=5,
        metadata=_meta(
            "Home Tab Sessions Per Kind",
            "Max sessions shown per category (main chat / task runner) in the Slack Home Tab.",
            tags=["slack"],
        ),
    )
    sessions_limit: int = field(
        default=10,
        metadata=_meta(
            "Sessions List Length",
            "Max sessions listed by the Slack 'sessions' DM keyword and the "
            "'sessions' slash command. Raise it on an install with many "
            "background sessions, where the ones worth resuming are pushed past "
            "the end of the list.",
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
            restart=True,
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
            restart=True,
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
            restart=True,
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
            restart=True,
        ),
    )
    bind_refresh_chains: bool = field(
        default=True,
        metadata=_meta(
            "Bind Refresh Chains To The Device",
            "Bind a session's 30-day refresh chain to the tailnet peer that "
            "opened it, so a stolen refresh cookie cannot be replayed from a "
            "different allowed device. On by default. Only turn it off if you "
            "need one session to roam between your DEVICES while pin_scope is "
            "'node' — with pin_scope 'login' the pin is your identity, not the "
            "device, so roaming already works. Turning it off means a stolen "
            "refresh cookie renews from any allowed node. Existing chains keep "
            "the binding they were opened with; the change applies to sessions "
            "started after the next gateway start.",
            restart=True,
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
    tailnet peer (the shape that reopens the publish allowlist).

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
        # Defaults TRUE, and a non-boolean resolves to TRUE as well: this is a
        # narrowing-only field like the two rules above, so an operator typo may
        # only ever leave the binding ON, never silently reopen the replay path
        # the binding closes.
        bind_refresh_chains=_safe_bool(data.get("bind_refresh_chains"), True),
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


@dataclass
class LinkPatternRule:
    """One text-to-link rewrite rule for chat transcripts.

    Rendering-only: the dashboard rewrites matching plain text into links at
    display time; stored messages are never modified. The pattern is compiled
    by the BROWSER (JavaScript regex dialect), so the backend validates only
    shape and size, never regex semantics.
    """

    pattern: str = field(
        default="",
        metadata=_meta(
            "Pattern",
            "JavaScript regular expression matched against transcript text "
            "(e.g. '\\\\bPROJ-\\\\d+\\\\b'). A rule that matches the empty string is "
            "ignored.",
        ),
    )
    url: str = field(
        default="",
        metadata=_meta(
            "URL Template",
            "Link target for each match: an absolute http(s) URL in which "
            "'{match}' inserts the matched text, percent-encoded (e.g. "
            "'https://tracker.example.com/browse/{match}'). The renderer "
            "additionally requires the placeholder outside the host and "
            "refuses userinfo.",
        ),
    )


# dashboard.link_patterns -- bounds on operator-supplied transcript link rules.
# The count cap bounds per-message scan work (each rule is one regex pass over
# every rendered markdown block); the length caps bound pathological patterns
# and keep the config API payload small.
LINK_PATTERNS_MAX = 50
LINK_PATTERN_PATTERN_MAX_LEN = 300
LINK_PATTERN_URL_MAX_LEN = 2000


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
            restart=True,
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
            restart=True,
        ),
    )
    dynamic_dashboard_cards: bool = field(
        default=False,
        metadata=_meta(
            "Automatic session cards",
            "Use the background model to update session-owned HTML cards after activity. "
            "Off by default. One call at a time, at most one per session every two minutes "
            "and 60 per gateway hour. Live status and native decisions work without it.",
        ),
    )
    crewmate_threads: bool = field(
        default=False,
        metadata=_meta(
            "Reply threads on crewmate chat messages",
            "Let any message in a crewmate's chat carry its own reply thread, "
            "opened in the side panel while the main chat stays visible. Off by "
            "default: the thread routes answer not-found, no thread frame is sent, "
            "and the dashboard draws no Reply in thread control. Takes effect on "
            "the next request; no restart.",
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
            restart=True,
        ),
    )
    surface_channel_sessions: bool = field(
        default=True,
        metadata=_meta(
            "Show Channel Conversations In Chat List",
            "Show recently active Slack/Discord/Teams (etc.) conversations in the "
            "dashboard's chat list instead of only under History. Uses the same "
            "recency window as session restoration.",
            restart=True,
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
    title_refresh_every_turns: int = field(
        default=0,
        metadata=_meta(
            "Refresh Auto Title Every N Turns",
            "Re-examine a session's auto-generated title every N user turns "
            "(at turns N, 2N, 3N, ...) from its last ten messages, and rename the "
            "session when the topic has moved. 0 keeps the built-in schedule: "
            "turns 8 and 24 only. Either way, a title that began as a bare link "
            "or ticket key also gets one refresh after the first turn. A value "
            "from 1 to 3 is raised to 4, and the ceiling is 1000. Each refresh is "
            "one background LLM call. Turns are counted over the messages held "
            "for the session. A session reloaded by a gateway restart or "
            "reopened from History holds its latest 500 plus the new ones, and "
            "the cadence continues from the restored count; if the reload keeps "
            "fewer user turns than the latest built-in turn the session had "
            "already reached (the first turn, 8 or 24), the cadence resumes "
            "after that turn instead. Once 10,000 are held, the oldest drop off "
            "as new ones arrive, so refreshes slow down or stop until the next "
            "reload. A title you renamed by hand is never refreshed. Takes "
            "effect on the next turn; no restart.",
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
            "Raise it on a many-slot host. Clamped to 256..262144. Applies to "
            "the next chat save; no restart needed.",
        ),
    )
    chat_entry_cache_max_bytes: int = field(
        default=CHAT_ENTRY_CACHE_BYTES_DEFAULT,
        metadata=_meta(
            "Chat Entry Cache Max Bytes",
            "Memory ceiling in bytes for the chat save path's persisted-message "
            "entry memo. Evicted alongside the entry-count bound; raise it "
            "together with the entry bound when a many-slot host needs a "
            "larger cache. Clamped to 4 MiB..512 MiB. Applies to the next chat "
            "save; no restart needed.",
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
            restart=True,
        ),
    )
    default_memory_mode: str = field(
        default="persistent",
        metadata=_meta(
            "Default Memory Mode",
            "Memory mode for new dashboard chat sessions. 'persistent' reads "
            "and writes memory; 'incognito' reads but does not write; "
            "'temporary' neither reads nor writes. Explicit per-session choices "
            "still win.",
            enum=["persistent", "incognito", "temporary"],
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
            "'answer_only' drops explanation altogether — the answer alone, drawn "
            "as a picture when it has a shape, in sentences of at most twelve "
            "plain words; detail only when the user asks for it, plus one undo "
            "line for a destructive command and one risk line for anything "
            "touching security, data or spend. At every level security warnings and "
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
            restart=True,
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
    model_picker_configured: bool = field(
        default=False,
        metadata=_meta(
            "Model Picker Visibility Saved",
            "Internal marker set after the user saves the interactive model "
            "picker visibility list. It lets the dashboard distinguish a "
            "never-configured picker from one intentionally saved with no "
            "hidden models.",
        ),
    )
    model_picker_hidden_models: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Selectable Models",
            "Model IDs hidden from the interactive chat model picker. Empty shows "
            "every advertised model; 'auto' is always shown. This preference does "
            "not change entitlement, provider model discovery, defaults, role "
            "models, fallback models, bulk switching, or app-specific model lists.",
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
            # them by configKey and `kirocrew config set` accepts them on a
            # config that has never written one (the CLI's key check consults
            # SCHEMA_REGISTRY - see cli_config._declared_entry). `enabled` needs
            # no declaration for that: it rides on the default_factory below, so
            # it is always in the document that check walks. The field stays a
            # plain dict, so a key added by a future release still round-trips
            # untouched via additionalProperties.
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
                "max_sessions": {
                    "type": "integer",
                    "default": 12,
                    "x-meta": {
                        "label": "Max terminal sessions",
                        "help": (
                            "Ceiling on concurrent terminal sessions across every chat, "
                            "server-wide. Each chat's activity bar caps its own terminals "
                            "below this; a session beyond the ceiling is refused."
                        ),
                    },
                },
                "cwd": {
                    "type": "string",
                    "default": "",
                    "x-meta": {
                        "label": "Default working directory",
                        "help": (
                            "Directory a terminal opens in when the chat passes no project "
                            "directory of its own. Empty = $HOME."
                        ),
                    },
                },
                "completion": {
                    "type": "object",
                    "additionalProperties": True,
                    "x-meta": {
                        "label": "Command completion",
                        "help": "The Terminal tab's inline completion popup.",
                    },
                    "properties": {
                        "enabled": {
                            "type": "boolean",
                            "default": True,
                            "x-meta": {
                                "label": "Command completion",
                                "help": (
                                    "Show the completion popup while typing in the "
                                    "Terminal tab. Off = no popup; the shell's own Tab "
                                    "completion still works."
                                ),
                            },
                        },
                        # Left open like `completion` itself: a typed
                        # `additionalProperties` would flatten into a
                        # `commands.*` registry entry, which is not reachable
                        # from the dataclass hierarchy the schema mirrors. The
                        # values are protocol names and a value that is not one
                        # is ignored by `terminal_commands.protocol_for`.
                        "commands": {
                            "type": "object",
                            "additionalProperties": True,
                            "default": {},
                            "x-meta": {
                                "label": "Completion protocol overrides",
                                "help": (
                                    "Re-point an already-allowlisted command at a different "
                                    'completion protocol, e.g. {"docker": "cobra"}. It can '
                                    "only change the protocol of a command the release "
                                    "already knows - it cannot add one, because the "
                                    "allowlist is the set of tools whose probe argv is "
                                    "known to be inert."
                                ),
                            },
                        },
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
    # Literal enum rather than FOLDER_SORT_MODES: that constant is defined below
    # this class (with the other write/load bounds) and a class body is evaluated
    # top to bottom. test_config_patch.py::TestFolderSortRoundTrip::
    # test_the_allowlist_enum_is_the_loader_list_spelled_once pins the two
    # spellings equal.
    folder_sort: str = field(
        default="custom",
        metadata=_meta(
            "Sidebar Folder Order",
            "How the chat sidebar orders session folders: 'custom' keeps the stored "
            "positions (set by dragging a folder or by chat_folder_move), 'name' is an "
            "ASCII-case-insensitive natural order (01. < 02. < 10.; A-Z fold to a-z, "
            "other letters compare as written), 'created' is newest "
            "first. A view preference only -- choosing a mode never rewrites the "
            "stored positions, so switching back to 'custom' restores them exactly. "
            "Read by the sidebar and by chat_folder_tree, which lists folders in the "
            "order the sidebar draws them.",
            enum=["custom", "name", "created"],
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
    crewmates_onboarded: bool = field(
        default=False,
        metadata=_meta(
            "Crewmates Onboarded",
            "Whether the user has finished or dismissed the first-run Meet CrewMates "
            "flow (the four-step introduction that creates the first crewmate). "
            "Server-backed like the other first-run flags so a second machine does "
            "not replay it. Also set when the flow is re-run from the Crewmates page.",
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
    feature_videos_enabled: bool = field(
        default=False,
        metadata=_meta(
            "Feature Videos Enabled",
            "Show short feature-intro clips for features this install has not used "
            "yet. Instance-wide kill switch.",
        ),
    )
    feature_videos_cache_max_mb: float = field(
        default=500.0,
        metadata=_meta(
            "Feature Videos Cache Size (MB)",
            "Disk budget for downloaded clips. Whole release folders are evicted "
            "oldest-first to fit; the running release is never evicted. 0 = no cap.",
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
    link_patterns: list[LinkPatternRule] = field(
        default_factory=list,
        metadata=_meta(
            "Transcript Link Patterns",
            "Rewrite matching plain text in chat transcripts into clickable "
            "links at display time (e.g. ticket ids like PROJ-123 to your "
            "tracker). Each rule pairs a JavaScript regex with an absolute "
            "http(s) URL template in which '{match}' inserts the matched "
            "text, percent-encoded. Rendering-only: stored messages never change. Text "
            "inside code blocks, existing links, and raw HTML is not rewritten; "
            "an inline code span whose whole text matches becomes a link chip.",
        ),
    )


@dataclass
class KiroCrewAgentConfig:
    member_id: str = field(
        default="",
        metadata=_meta("Member ID", "Immutable identity assigned when member memory is created."),
    )
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
    display_name: str = field(
        default="",
        metadata=_meta(
            "Display Name",
            "Optional label the dashboard shows instead of the agent's name. "
            "Purely presentational: the name stays the immutable identity — it "
            "keys this record, addresses /api/agents/{name}, and is what "
            "dispatch, crons and spawn resolve — so renaming the label never "
            "breaks a binding. Empty means the dashboard shows the name itself.",
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
    starred: bool = field(
        default=False,
        metadata=_meta(
            "Starred",
            "Marks this crew as a favourite on the Crew Members page. Purely a "
            "roster preference: the page's star filter shows only starred "
            "crews, so the dozens of package-installed crews the agent sync "
            "writes here can be collapsed behind the few you actually drive. "
            "Never read by routing, spawning, or the orchestrator.",
        ),
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
SUBAGENT_MAX_TURNS_CEILING = 1000  # agent.subagent_max_turns — per-subagent turn budget
POOL_SIZE_MAX = 10  # session.pool_size — pre-warmed process pool

# agent.chat_turn_timeout_secs — wall-clock ceiling for one chat turn. The ACP
# transport's per-prompt wait follows this value (acp/client.py
# ``resolve_prompt_timeout``, which adds a margin so the dashboard's visible
# card fires before the transport cut), so the max is not pinned to the
# transport's default. The default is 4h: the longest single turn the shipped
# budgets can legitimately produce is a 90-minute test command plus a fix and a
# re-run, or a blocking subagent wave at its 2h wait cap plus synthesis, and
# the watchdog's UNKNOWN-verdict windows must sit well inside the ceiling so its
# non-lethal recovery is reachable before the turn is cut. It is bounded at 24h
# because the ceiling is a runaway backstop, not a scheduler: a single
# prompt→response turn longer than a day is pathological, and multi-day
# unattended operation belongs to the loop mechanisms (monitor/goal loops,
# crons), which end the turn between cycles and survive restarts — a marathon
# turn does not. The floor keeps the backstop from being set so low it cuts
# ordinary work.
CHAT_TURN_TIMEOUT_MIN = 300
CHAT_TURN_TIMEOUT_MAX = 86400

# agent.session_start_timeout_secs — budget for ACP session/new + session/load
# on the shared runtime (acp/runtime_start.py ``_SESSION_NEW_TIMEOUT`` is the built-in
# default). kiro-cli blocks the session/new response while it initializes the
# session's MCP servers, so start time scales with the agent's server count and
# per-server cold-start cost (observed: a 71-server agent with no pending OAuth
# completes in ~14s; a 17-server agent behind a sandboxed per-server launcher on
# a loaded host takes ~50s). The floor IS the default: the budget must stay
# comfortably ABOVE the backend's 30s OAuth authorization wait —
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
# Ranges for bounded numeric fields the LOAD path clamps, while `_EDITABLE_CONFIG`
# rejects the same values at write time. A hand-edited config.json goes nowhere
# near the dashboard API, so without this every one of these would load verbatim --
# the same load/write asymmetry the security-relevant knobs also close.
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
# ``dashboard.title_refresh_every_turns``: 0 is "built-in schedule"; any other
# value is at least MIN, so a typo of 1 cannot spend an LLM call on every turn.
TITLE_REFRESH_EVERY_TURNS_MIN = 4
TITLE_REFRESH_EVERY_TURNS_MAX = 1000
RECENT_TINT_COUNT_MIN = 0
RECENT_TINT_COUNT_MAX = 10
# The sidebar's folder sort modes, spelled once for the same reason as the bounds
# above: the loader normalizes to this set, the PATCH allowlist accepts exactly
# it, and the ``kirocrew-dashboard`` MCP server reads the stored value back
# through it. ``custom`` is the stored ``order`` positions (today's behaviour and
# the default), ``name`` an ASCII-case-insensitive natural order, ``created`` newest
# first. The frontend's ``readFolderSortMode`` mirrors this list.
FOLDER_SORT_MODES: tuple[str, ...] = ("custom", "name", "created")
FOLDER_SORT_DEFAULT = "custom"
SESSION_TIMEOUT_MIN = 0
SESSION_TIMEOUT_MAX = 86400
POOL_TTL_SECS_MIN = 0
POOL_TTL_SECS_MAX = 7200
SOFT_STOP_BUDGET_MIN = 0.5
SOFT_STOP_BUDGET_MAX = 60.0
EXTRACTION_POOL_SIZE_MIN = 1
EXTRACTION_POOL_SIZE_MAX = 10
# Load-only bounds. Unlike the parity block above these are NOT consumed by
# `_EDITABLE_CONFIG` — the field is config-file-only (no dashboard write path),
# so the only clamp site is the loader. Kept out of the shared block so its
# "every bound is shared with the write gate" claim stays true.
EMPTY_RESPONSE_MAX_CONTINUES_MIN = 1
EMPTY_RESPONSE_MAX_CONTINUES_MAX = 10
# Ceiling on ``session.reconcile_max_kills``, equal to the arm's own shipped budget
# (``runtime_reconcile.DEFAULT_MAX_KILLS``, pinned equal by
# ``test_the_configured_ceiling_cannot_exceed_the_shipped_budget``). Equal rather
# than higher is what makes the field subtractive: every reachable value is at or
# below what the product already does, so a write to this agent-writable file can
# withhold signals and cannot authorize one the arm would not already send.
#
# 0 is meaningful (observe-only) and is the floor, so a negative clamps DOWN to it
# and disables the arm rather than enabling it.
RECONCILE_MAX_KILLS_MAX = 5
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
# Ceiling on the explicit-import cross-file chunk budget, same rationale as the
# folder/sweep budgets above: clamp a negative to 0 (0 == unbounded) and cap an
# absurd hand-edited value so it cannot load verbatim into real work.
IMPORT_CHUNK_BUDGET_MAX = 50000


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


#: The default provider, and the one a RETIRED ``stt.provider`` degrades to. It is
#: the only recogniser with no precondition: recognition runs in this process on
#: every supported OS, with no account, no platform floor, and no separate install.
STT_PROVIDER_LOCAL = "local"

#: No recogniser at all. Selectable, so that "turn speech off" has a value a user
#: can write from the CLI, and the value an UNKNOWN ``stt.provider`` degrades to.
#: The distinction from ``enabled=False`` is only where it is set: both leave
#: every speech path answering "disabled", nothing is loaded and nothing bills.
#:
#: Degrading an unknown value onto ``local`` instead would put a typo, or a value
#: a bot guessed at, onto the one provider that links a native library into this
#: process: a user told to set ``stt.provider off`` while that library is
#: crashing on model load gets the crashing engine back, and learns it only from
#: a WARNING line (kirodotdev/KiroCrew#13179). A value the loader cannot honour
#: fails closed: whatever was meant, "run nothing" is the one reading that cannot
#: make things worse.
STT_PROVIDER_OFF = "off"

#: Local Whisper can detect the spoken language; forcing English corrupts
#: multilingual dictation before the recogniser can choose the right tokens.
STT_LANGUAGE_AUTO = "auto"
STT_LANGUAGE_FALLBACK = "en-US"

#: The recognisers a user can select. ``local`` runs whisper.cpp in-process,
#: ``apple`` uses macOS 26+ on-device recognition, and ``transcribe`` sends audio
#: to AWS Transcribe (billed, and gated on the AWS consent prompt). All three
#: produce partial results, so streaming is not a per-provider capability.
#: ``off`` selects no recogniser; it is deliberately absent from
#: ``stt_stream._STREAMING_PROVIDERS``, which grants by positive membership.
_VALID_STT_PROVIDERS = (STT_PROVIDER_LOCAL, "apple", "transcribe", STT_PROVIDER_OFF)

#: Providers a stored config may still name. Each of these needed an out-of-band
#: install the user had to perform themselves (a whisper CLI on ``PATH``, or an
#: ``mlx``/``faster-whisper`` wheel), which is precisely the cost the resident
#: local engine removes, so a stored value degrades to ``local`` instead of
#: leaving voice input pointing at something that is not dispatchable.
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


def stt_provider_resolution(value: object) -> str:
    """The provider a stored ``stt.provider`` of *value* runs as. Pure; never logs.

    Where an unusable value degrades TO depends on what is known about it. A
    retired name lands on ``local``: that user had local recognition and keeps
    it. Anything else lands on :data:`STT_PROVIDER_OFF`: a value nobody can
    account for must not select the provider that links a native library into
    the gateway. A JSON ``null`` names nothing, so it is the ABSENT key, not a
    wrong one: it takes the default the way a missing key does.

    Separate from :func:`_validated_stt_provider` so a surface that only needs
    the answer -- ``kirocrew config defaults --adopt`` deciding what to write --
    can ask without triggering, or having to suppress, the load-time notice.
    """
    if value in _VALID_STT_PROVIDERS:
        return str(value)
    if value is None or value in _RETIRED_STT_PROVIDERS:
        return STT_PROVIDER_LOCAL
    return STT_PROVIDER_OFF


def _validated_stt_provider(value: object) -> str:
    """:func:`stt_provider_resolution`, with the load-time notice for a degrade.

    Degrades and logs; never raises. This value arrives from ``config.json``, so
    an unusable one must leave the load working the way
    :func:`_normalize_acp_backend` degrades an unusable persisted backend, rather
    than failing the load that read it.

    The notice names the command that removes the dead value. A load never writes,
    so without that pointer the line repeats on every invocation forever -- and
    unlike a superseded default there is nothing here to preserve, since the stored
    value cannot take effect either way. A ``null`` says nothing: it is the absent
    key, not a wrong one.
    """
    resolved = stt_provider_resolution(value)
    if value in _VALID_STT_PROVIDERS or value is None:
        return resolved
    seen = repr(value)
    if seen in _WARNED_STT_PROVIDERS:
        return resolved
    _WARNED_STT_PROVIDERS.add(seen)
    if value in _RETIRED_STT_PROVIDERS:
        logger.warning(
            "STT provider %r is retired; using %r instead. It needed a separate "
            "out-of-band install, which the bundled local engine removes while "
            "recognising the same speech. Run 'kirocrew config defaults --adopt' "
            "to drop the stored value and this notice.",
            value,
            resolved,
        )
    else:
        logger.warning(
            "Unknown STT provider %r; using %r instead, so no recogniser runs until "
            "the value is fixed. Selectable providers: %s. Run "
            "'kirocrew config set stt.provider <provider>' to choose one, or "
            "'kirocrew config defaults --adopt' to drop the stored value.",
            value,
            resolved,
            ", ".join(_VALID_STT_PROVIDERS),
        )
    return resolved


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


@dataclass
class ResolvedBindings:
    """Resolved workspace, memory store, and kiro agent for a session."""

    workspace_dir: Path
    memory_store_name: str
    effective_memory_config: dict
    kiro_agent: str
    execution_context: object | None = None
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
    # Positive selection provenance. A later discovered member with the same
    # name must not change a conversation that selected the provider template.
    selection_kind: str = ""
    # Revision observed before resolution: "" means no protected record;
    # None means no observation was made. Only automatic publication uses it.
    selection_revision: str | None = None

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
        ``selection_kind`` (the namespace is retained separately for later
        resolution; identical current targets may still share a slot) or
        ``selection_revision`` (a publication guard, not a dispatch target) or
        ``requested_resolved``/``effective_memory_config`` (the former is
        request metadata the caller checks separately; the latter is derived
        from ``memory_store_name`` plus global config shared by both sides).
        ``execution_context`` carries the admitted session's identity and mode;
        session selection checks those separately before comparing these targets.
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
            "account. `off` runs no recogniser at all, the same as turning speech "
            "input off.",
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
            "are being misheard, `large-v3-turbo` for the best accuracy available "
            "-- though on a CPU-only build it can recognise slower than you speak: "
            "an 11-second clip took 13.6 s on a 16-thread aarch64 CPU, 1.24x the "
            "audio. The Voice panel says so beside the choice when the build it "
            "measured has no acceleration.",
            enum=list(_VALID_STT_MODELS),
        ),
    )
    language_code: str = field(
        default=STT_LANGUAGE_AUTO,
        metadata=_meta(
            "Language Code",
            "Language for speech recognition (e.g. zh-CN, en-US). The local provider "
            "defaults to auto-detect; choosing a language can improve short dictation.",
        ),
    )
    polish: bool = field(
        default=False,
        metadata=_meta(
            "AI Cleanup",
            "After dictation finishes, have your configured model fix punctuation "
            "and capitalisation. Your WORDS are never changed: a "
            "reply that altered one is discarded, so the worst case is that nothing "
            "happens. Off by default because it sends the TRANSCRIPT (never the "
            "audio) to that model, so a local-only setup stays local-only until you "
            "turn this on.",
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
        metadata=_meta(
            "Timeout",
            "Maximum transcription time in seconds. Local streaming uses the same budget "
            "for pending audio after recording stops.",
        ),
    )
    transcribe_region: str = field(
        default="us-east-1",
        metadata=_meta("Transcribe Region", "AWS region for Transcribe API."),
    )
    transcribe_profile: str = field(
        default="",
        metadata=_meta("Transcribe Profile", "AWS profile for Transcribe API."),
    )

    def __post_init__(self) -> None:
        language = self.language_code
        if not isinstance(language, str) or not language.strip():
            language = STT_LANGUAGE_AUTO
        else:
            language = language.strip()
        if language.lower() == STT_LANGUAGE_AUTO:
            language = STT_LANGUAGE_AUTO
        self.language_code = language

    @property
    def effective_language_code(self) -> str:
        """Resolve a provider locale without changing the stored preference."""
        if self.language_code == STT_LANGUAGE_AUTO and self.provider != STT_PROVIDER_LOCAL:
            return STT_LANGUAGE_FALLBACK
        return self.language_code


# Sampling bucket bounds, as a percentage of sessions. 0 admits nothing, 100
# admits everything; both the parse below and the gate clamp to this range rather
# than treating an out-of-range value as a second way to disable the seam.
DECISION_BUCKET_MIN = 0
DECISION_BUCKET_MAX = 100

DECISION_PROVIDER_ENDPOINT_DEFAULT = "https://api.typesafe.ai/v1/systemone"
DECISION_PROVIDER_MODEL_DEFAULT = "jev-latest"

# How much PRIOR CONVERSATION one decision may carry, as a char budget rather than
# a message count, because what it bounds is the size of the request that leaves
# the machine -- a count bounds neither.
#
# The default covers the last two or three turns, which is what a request like "do
# the B comparison drawer" needs for the oracle to know what "B" names. It widens
# nothing on its own: the gate sends ``min(this, the consent keystone's ceiling)``,
# and that ceiling is 0 until the owner reviews a prior-turn number, so an install
# whose consent names only the message excerpt and the candidate descriptions
# carries the current message alone.
DECISION_HISTORY_BUDGET_DEFAULT = 2000

# The tiers ``model.route`` may answer with, and the model each maps to by default.
# The keys are the point's CLOSED answer domain
# (``decisions.points.model_route.TIERS``): a key outside it is dropped, because a
# tier the question never offers can never be answered and a map that accepted one
# would read as configured while routing nothing.
#
# The values are ordinary model ids and grant nothing on their own -- the point
# validates each against what the provider advertises to this account and keeps the
# session's current model when an id is not there -- so this stays a config value
# rather than a keystone one.
DECISION_MODEL_ROUTE_TIERS: tuple[str, ...] = ("simple", "medium", "complex")

# Every tier defaults to ``""`` -- INHERIT, i.e. the turn keeps the model its
# session is already on. No concrete model id is named here, and none may be: a
# hardcoded id fails at runtime -- silently, until the first prompt -- for every
# account not entitled to it, so
# ``docs/system-specs/common/model-selection.md`` allows ids to be pinned only in
# an operator-written map and keeps code defaults at ``""`` / ``"auto"``.
# ``code-review.yml`` gates on it.
#
# The three keys are PRESENT and empty rather than absent, which is the same
# shape ``agent.role_models``'s roles take: "this tier exists and is unpinned" is
# a state the log and the strip report ("complex -> (unpinned)"), so it needs a
# spelling of its own rather than being inferred from a missing key.
DECISION_MODEL_ROUTE_DEFAULT: dict[str, str] = {tier: "" for tier in DECISION_MODEL_ROUTE_TIERS}


def coerce_model_route(raw: object) -> dict[str, str]:
    """Normalize ``decisions.model_route`` from a hand-edited config.

    Always returns all three tiers. Each value goes through
    :func:`normalize_agent_model`, exactly as :func:`coerce_role_models` does, so
    ``"auto"`` and a non-string both collapse to ``""`` -- "inherit" has ONE
    spelling, and a tier set to ``"auto"`` keeps inheriting instead of hard-pinning
    the backend's own default.

    A tier outside the three is dropped: the question never offers it, so it could
    never be answered, and a map that accepted one would read as configured while
    routing nothing.
    """
    section = raw if isinstance(raw, dict) else {}
    return {tier: normalize_agent_model(section.get(tier)) for tier in DECISION_MODEL_ROUTE_TIERS}


# The providers ``decisions.nudge_wake.provider`` may name. ``auto`` resolves at
# decision time -- Jev when the keystone consents to it, the LLM lane otherwise --
# so a machine that later gains or loses a Jev key needs no config edit.
JUDGE_PROVIDER_AUTO = "auto"
JUDGE_PROVIDER_JEV = "jev"
JUDGE_PROVIDER_LLM = "llm"
JUDGE_PROVIDERS = (JUDGE_PROVIDER_AUTO, JUDGE_PROVIDER_JEV, JUDGE_PROVIDER_LLM)


@dataclass
class NudgeWakeConfig:
    """Per-point settings for the wake judge (``decisions`` point ``nudge.wake``).

    Deliberately carries NO ``enabled``. There is no feature toggle, because the two
    lanes are authorized by different things and neither of them is a toggle:

    * The Jev lane needs the Decisions keystone in full -- the main switch AND this
      point's own ``nudge_evidence`` scope, because that scope names a category of
      egress to the Jev endpoint. Config cannot grant it and this section cannot
      widen it.
    * The ``llm`` lane needs no consent row, so ``provider = llm`` plus a ``judge``
      spec on the loop is what runs it. What makes that safe is not that config is
      trusted: the lane adds no destination and no data class. It sends to the model
      provider the owner's sessions already send to every turn, carrying a scrubbed,
      bounded subset of the owner's own children's transcripts, which that provider
      already received when those sessions ran. The judge only chooses QUIET against
      firing and is fail-open, so the worst case is one delayed wake, bounded by the
      quiet-streak floor.

    Hot-applied: the gate reads the live snapshot per call.
    """

    provider: str = field(
        default=JUDGE_PROVIDER_AUTO,
        metadata=_meta(
            "Judge provider",
            "Which judge answers at nudge.wake: 'jev' (the System One model this "
            "card's consent switch covers), 'llm' (a small text-only model on the "
            "provider this machine already uses, no extra key needed), or 'auto' "
            "-- Jev when consent stands for it, otherwise the small model. "
            "Anything else reads as 'auto'. Choosing 'jev' without that consent "
            "sends nothing and every tick fires as it does today.",
        ),
    )
    llm_model: str = field(
        default="",
        metadata=_meta(
            "Judge model (LLM lane)",
            "The model id the 'llm' lane runs on, spelled as your provider "
            "advertises it. EMPTY INHERITS: the judge keeps the model its "
            "background agent already resolves, which is the default. A value that "
            "is not a short model id is ignored rather than sent.",
        ),
    )

    quiet_streak_floor: int = field(
        default=0,
        metadata=_meta(
            "Quiet ticks before firing anyway",
            "How many QUIET verdicts in a row end the run of skipped turns. The Nth "
            "consecutive QUIET tick FIRES regardless of its own verdict, so this many "
            "quiet verdicts skip one fewer turn than the number suggests, and a judge "
            "that is wrong about a subject costs a late turn rather than silence. 0, "
            "the default, means inherit the shipped floor -- the same number the typed "
            "probe path uses, spelled once in the loop engine so the two cannot drift. "
            "A negative value also reads as inherit. This knob only ever SHORTENS the "
            "silence window: the shipped floor is the ceiling, and a larger value is "
            "clamped back down to it, because config.json is writable by an "
            "auto-approved agent shell and a raised floor would silence a watch with "
            "no new code, only a number.",
        ),
    )

    @classmethod
    def from_raw(cls, section: object) -> "NudgeWakeConfig":
        """Normalize rather than reject, the posture the whole section takes.

        Every unreadable value resolves to the shipped default. Neither key can reach
        a destination the session does not already send to, so a hand-edit that fails
        to parse costs a preference, not a permission.
        """
        if not isinstance(section, dict):
            return cls()
        raw_provider = section.get("provider")
        provider = raw_provider.strip().lower() if isinstance(raw_provider, str) else ""
        raw_model = section.get("llm_model")
        return cls(
            # An unknown name reads as ``auto`` rather than as an error: a typo must
            # not become a third lane and must not stop the gateway booting.
            provider=provider if provider in JUDGE_PROVIDERS else JUDGE_PROVIDER_AUTO,
            # Kept verbatim (stripped) and validated where it is USED, against
            # ``decisions.types.MODEL_ID_RE``: storing "" for an id this build
            # cannot use would make the saved config disagree with what the operator
            # wrote, and the bound that matters is at the call that names a model.
            llm_model=raw_model.strip() if isinstance(raw_model, str) else "",
            # Absent, malformed and negative all read as 0, which the engine resolves
            # to its shipped floor. The ceiling is NOT clamped here: it is the engine's
            # own constant, and importing it would invert this module's dependency on
            # the loop engine (which imports ``config.loader`` at module scope). The
            # engine clamps on every read, so an over-large value never takes effect.
            quiet_streak_floor=_safe_int(section.get("quiet_streak_floor", 0), 0, 0),
        )


@dataclass
class DecisionProviderConfig:
    """Where the System One provider lives and what one call may cost."""

    endpoint: str = field(
        default=DECISION_PROVIDER_ENDPOINT_DEFAULT,
        metadata=_meta(
            "Endpoint",
            "Full URL of the evaluation endpoint. Override only to point at a "
            "compatible proxy — the request and response field names are fixed by "
            "the TypeSafe API, not by this setting.",
        ),
    )
    api_key: str = field(
        default="secret://TYPESAFE_API_KEY",
        metadata=_meta(
            "API Key",
            "The provider credential reference. Only 'secret://TYPESAFE_API_KEY' is "
            "honoured: it reads that one entry from the dashboard secrets vault "
            "(Settings › Secrets), so no key is ever stored in config.json. A literal "
            "key here is NOT used, and no other vault entry is readable through this "
            "field, because config.json is agent-writable. With no usable key the "
            "seam logs a row saying so and returns None — it never sends an empty "
            "bearer credential.",
            sensitive=True,
        ),
    )
    model: str = field(
        default=DECISION_PROVIDER_MODEL_DEFAULT,
        metadata=_meta(
            "Model",
            "Provider model id. 'jev-latest' is TypeSafe's flagship System One model. "
            "Letters, digits, dots, dashes and underscores, up to 64 characters; "
            "anything else is refused before a request is sent, because this field "
            "travels in the request and config.json is agent-writable.",
        ),
    )
    timeout_ms: int = field(
        default=1000,
        metadata=_meta(
            "Timeout (ms)",
            "Total budget for one decision, in milliseconds. Exceeding it logs "
            "error='timeout' and returns None, so this is the ceiling the seam "
            "adds to the path it sits in — not a target. Values at or below zero "
            "are floored to 1ms rather than disabling the timeout.",
        ),
    )


@dataclass
class DecisionsConfig:
    """Decision seam (``src/kiro_crew/decisions/``). Off by default.

    There is deliberately NO ``enabled`` field here. The switch that lets
    conversation state leave the machine is an authorization, not a preference,
    and ``config.json`` is writable by an auto-approved agent shell -- so it lives
    on the KEYSTONE leaf ``decisions_consent.json`` (``decisions.consent``), the
    same placement as ``computer_use.json`` and ``aws_service_consent.json``. This
    section carries only the knobs that grant nothing on their own: the sampling
    share, the prior-conversation budget (0 by default, so raising it is a choice),
    the tier-to-model map ``model.route`` reads, and the provider. There is no
    per-point arm and no shadow mode: two points ship (``skills.select``,
    ``model.route``), each reached only through its own owner-made choice --
    a non-zero ``skills.max_triggered`` and the picker's ``Auto (Jev)`` entry.

    Every field is hot-applied (no ``restart=True`` anywhere): the gate reads the
    live snapshot per call, so a bucket change takes effect on the next decision
    without a gateway restart.
    """

    bucket: int = field(
        default=DECISION_BUCKET_MAX,
        metadata=_meta(
            "Bucket (%)",
            "Percentage of sessions the seam fires for, 0-100, decided by a hash "
            "of the session key so a session is consistently in or out. 0 fires "
            "for nobody, 100 for everybody. Out-of-range numbers are clamped; a "
            "value that is not a whole number reads as 0 (nobody sampled), never "
            "as everybody. To switch the seam off, withdraw consent in Settings > "
            "Developer > Feature Previews, not a zero bucket.",
        ),
    )
    history_budget_chars: int = field(
        default=DECISION_HISTORY_BUDGET_DEFAULT,
        metadata=_meta(
            "History budget (chars)",
            "How many characters of PRIOR conversation one decision may carry, on "
            "top of the current message. Earlier user and assistant turns are added "
            "newest-first until this many characters are spent and the last one is "
            "clipped to fit; tool output is never sent. The default is 2000, about "
            "the last two or three turns, and the consent keystone caps it: the gate "
            "sends the smaller of the two, so an install whose owner reviewed no "
            "prior-turn ceiling sends no prior turns at all. A negative value reads "
            "as 0.",
        ),
    )
    model_route: dict[str, str] = field(
        default_factory=lambda: dict(DECISION_MODEL_ROUTE_DEFAULT),
        metadata=_meta(
            "Model per difficulty tier",
            "Which model answers a chat turn Jev put in each difficulty tier, for "
            "a session whose model is set to 'Auto (Jev)' in the chat model "
            "picker. Keys are the three tiers the question offers -- 'simple', "
            "'medium', 'complex' -- and each value is a model id the provider "
            "advertises to your account, exactly as the chat model picker spells "
            "it. Every tier is EMPTY by default, which means inherit: the turn "
            "keeps the model its session is already on, and the decision is still "
            "recorded so you can see which tier Jev chose before you pin anything. "
            "No model id is named for you, because an id your account is not "
            "offered would fail on the first prompt. 'auto' means the same as empty. "
            "An id your account cannot run keeps the session's model too and "
            "records why in the decision log. Routing a turn to a dearer model "
            "costs more, which is why it happens only for a session whose owner "
            "picked 'Auto (Jev)' -- a manual model choice is never overridden.",
        ),
    )
    provider: DecisionProviderConfig = field(
        default_factory=DecisionProviderConfig,
        metadata=_meta("Provider", "Where decisions are sent and what they may cost."),
    )
    nudge_wake: NudgeWakeConfig = field(
        default_factory=NudgeWakeConfig,
        metadata=_meta(
            "Wake judge",
            "Per-point settings for nudge.wake: which judge answers, the model "
            "id for the small-model lane, and how long a judge may keep a loop "
            "quiet before one fires anyway. The Jev lane still needs this point's "
            "consent scope on the Decisions card; the small-model lane needs no "
            "consent row, because it sends to the model provider your sessions "
            "already use, so picking it here is what runs it.",
        ),
    )

    @classmethod
    def from_raw(cls, section: object) -> "DecisionsConfig":
        """Build from a raw ``decisions`` dict -- the ONE parse site.

        Lives here rather than in the loader because the bucket bounds are
        declared a few lines above, and a normalizer that read them from another
        module would need those names re-exported across a frozen module boundary
        (``test_config_module_boundaries``).

        Every value is NORMALIZED rather than validated-and-rejected: this
        section gates a seam that is off by default, so the fail-closed reading
        of any unreadable value is the default, and a hand-edited config.json
        must not stop the gateway booting.

        Accepts whatever ``json.loads`` produced, including ``None`` and a
        non-dict, for the same reason ``ResourceLimitsConfig.from_raw`` does. A
        config carrying the earlier ``preview``/``points``/``enabled`` spelling
        is read for its bucket and provider only: consent is never inferred from
        ``config.json``, whatever key it carries, because that file is what the
        keystone exists to keep the decision out of.
        """
        if not isinstance(section, dict):
            return cls()

        raw_provider = section.get("provider")
        raw_provider = raw_provider if isinstance(raw_provider, dict) else {}

        def _text(key: str, default: str) -> str:
            """A non-empty stripped string, else *default*.

            An empty or blank value resolves to the DEFAULT rather than to ``""``:
            the implementation falls back to the documented endpoint and model
            anyway, so storing ``""`` would leave the saved config disagreeing
            with what is in force -- the same reason ``bucket`` is clamped here.
            """
            raw = raw_provider.get(key)
            return raw.strip() if isinstance(raw, str) and raw.strip() else default

        provider = DecisionProviderConfig(
            endpoint=_text("endpoint", DecisionProviderConfig.endpoint),
            api_key=_text("api_key", DecisionProviderConfig.api_key),
            model=_text("model", DecisionProviderConfig.model),
            timeout_ms=_safe_int(
                raw_provider.get("timeout_ms", DecisionProviderConfig.timeout_ms),
                DecisionProviderConfig.timeout_ms,
            ),
        )

        return cls(
            # Clamped here as well as in the gate. The gate clamps because it
            # must never trust a value it did not parse; clamping here is what
            # makes the SAVED config say what is in force, so an operator who
            # wrote 500 sees 100 come back rather than a number that behaves as
            # 100 while reading as 500.
            #
            # ABSENT reads as the default (every session); MALFORMED reads as 0.
            # The two must not share a fallback: this number decides how much
            # conversation state leaves the machine, so a hand-edit that fails to
            # parse must fail closed to "nobody", never open to "everybody".
            bucket=_safe_int(
                section.get("bucket", DECISION_BUCKET_MAX),
                DECISION_BUCKET_MIN,
                DECISION_BUCKET_MIN,
                DECISION_BUCKET_MAX,
            ),
            # Unreadable reads as the DEFAULT, the same direction a malformed
            # bucket takes. That cannot fail open for this key: the gate holds the
            # configured number against the consent keystone's ceiling, so an
            # unreadable value still sends at most what the owner reviewed. Floored
            # at 0 so a negative number cannot read as unbounded.
            history_budget_chars=_safe_int(
                section.get("history_budget_chars", DECISION_HISTORY_BUDGET_DEFAULT),
                DECISION_HISTORY_BUDGET_DEFAULT,
                0,
            ),
            # Per-TIER fallback rather than per-map: see `coerce_model_route`. An
            # absent section and one naming no known tier both read as the shipped
            # map, since this key cannot widen anything -- every id is still held
            # against the provider's advertised list at routing time.
            model_route=coerce_model_route(section.get("model_route")),
            provider=provider,
            nudge_wake=NudgeWakeConfig.from_raw(section.get("nudge_wake")),
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
      That silent reinterpretation is the trap, so it is refused.
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
    has a schema: without it every consumer parses the raw dict itself, leaving
    the incompatible domains written down nowhere and free to drift apart.

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


def _coerce_link_patterns(raw: object) -> list[LinkPatternRule]:
    """Coerce the transcript link rules, skipping malformed entries.

    Same fail-open-per-entry discipline as the host allowlists: a hand-edited
    bad entry is dropped rather than failing the whole config load. Regex
    VALIDITY is deliberately not checked here -- the pattern is compiled by the
    browser in the JavaScript dialect, and Python's ``re`` accepts/rejects a
    different language; the frontend skips rules that fail to compile.
    """
    if not isinstance(raw, list):
        return []
    out: list[LinkPatternRule] = []
    seen: set[str] = set()
    for entry in raw:
        if len(out) >= LINK_PATTERNS_MAX:
            break
        if not isinstance(entry, dict):
            continue
        pattern = entry.get("pattern")
        url = entry.get("url")
        if not isinstance(pattern, str) or not isinstance(url, str):
            continue
        # Whitespace in a regex is load-bearing (`PROJ-\d+ ` and `PROJ-\d+`
        # match different text), so the pattern is stored EXACTLY as authored;
        # strip() decides only whether it is blank. URLs are the opposite:
        # the validator below refuses whitespace in the authority and the
        # template is expanded, not matched, so edge-trimming cannot change
        # meaning.
        url = url.strip()
        if not pattern.strip() or len(pattern) > LINK_PATTERN_PATTERN_MAX_LEN:
            continue
        if len(url) > LINK_PATTERN_URL_MAX_LEN:
            continue
        # http(s) only: the rewrite mints anchors into every transcript, so a
        # javascript:/file: template must never survive to the renderer even
        # though the frontend re-checks. link_pattern_url_ok also mirrors the
        # renderer's origin-stability rule (no userinfo, no '{match}' in the
        # authority) so what the config stores is what actually renders.
        if not link_pattern_url_ok(url):
            continue
        # First-wins on duplicate patterns. The settings PUT rejects
        # duplicates outright, so this only meets hand-edited config files —
        # where dropping the shadowed twin beats dropping the whole list.
        if pattern in seen:
            continue
        seen.add(pattern)
        out.append(LinkPatternRule(pattern=pattern, url=url))
    return out


def link_pattern_url_ok(url: str) -> bool:
    """True when *url* is a link-pattern template the renderer will accept.

    Mirrors the frontend's ``normaliseHref``: beyond the http(s) + ``{match}``
    floor, the renderer substitutes two DIFFERENT canary tokens and requires
    the same origin both times, which rejects a ``{match}`` sitting in the
    authority (where the token could steer the host) and any userinfo (which
    ``safeHttpUrl`` refuses outright). Enforcing the same rules here keeps the
    save honest: without them a template like ``https://{match}.example/x`` or
    ``https://u:p@host/{match}`` stores fine, renders nothing, and shows no
    warning anywhere.
    """
    # Scheme case-insensitively, like the browser's URL parser the editor
    # validates with: `HTTPS://x/{match}` must not pass the inline check and
    # then die at the PUT with no warning. urlsplit below lowercases the
    # scheme itself, so the origin comparison already agrees.
    if not url.lower().startswith(("https://", "http://")) or "{match}" not in url:
        return False
    try:
        origins = set()
        for canary in ("aaa", "bbb"):
            parts = _urlsplit(url.replace("{match}", canary))
            # Userinfo never survives the renderer's safeHttpUrl, and the
            # browser's URL parser refuses whitespace in the authority that
            # Python's urlsplit tolerates — either way the rule would store
            # fine and silently never linkify.
            if "@" in parts.netloc or not parts.hostname:
                return False
            if any(ch.isspace() for ch in parts.netloc):
                return False
            origins.add((parts.scheme, parts.hostname, parts.port))
        return len(origins) == 1
    except ValueError:
        return False


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
    bot ``token`` + ``account_id`` are obtained through the Settings > Messaging Channels
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
    data home (``whatsapp/session.db``), created by the Settings > Messaging Channels QR
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
            "WhatsApp Web protocol). Pair a device from Settings > Messaging Channels; "
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
            "Phone numbers (digits only, country code, no '+') permitted to "
            "address the agent besides the linked account: in direct chats when "
            "dm_policy='allowlist', and in every configured group regardless of "
            "dm_policy (a group member not listed here is dropped silently, even "
            "when they @-mention the agent or the group is in 'rules' mode). Empty "
            "adds nobody beyond the linked account.",
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
            "(default 120)}. Groups not listed are ignored entirely. Listing a "
            "group lets the agent speak there; it does not admit its members: "
            "only you and the numbers in allowed_wa_ids can make the agent reply.",
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
            restart=True,
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
    send_heartbeats: bool = field(
        default=False,
        metadata=_meta(
            "Send coding-activity heartbeats",
            "Send a heartbeat to WakaTime after each agent turn that edited "
            "files or ran a command, so your Kiro Crew coding time shows up in "
            "WakaTime alongside your editor. Off by default: sending activity "
            "outward is a separate opt-in from reading your own stats. Requires "
            "the WakaTime integration to be enabled. Covers dashboard chat and "
            "dashboard-linked Slack threads; the other messaging channels "
            "(Telegram, Discord, Teams, WhatsApp and the rest), the task "
            "runner, subagents, installed apps, and the standalone kirocrew "
            "chat CLI each run their own turn loop and do not emit heartbeats "
            "yet. Incognito and temporary sessions never send one: they keep "
            "no record of the chat, and a heartbeat is an external record of "
            "it.",
            tags=["wakatime"],
        ),
    )
