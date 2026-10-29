"""Load-time field readers, the security clamp, and the loader-owned section builders.

An owner of the :mod:`kiro_crew.config.loader` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Mapping, MutableMapping
from datetime import datetime, timezone

from kiro_crew import platform_compat
from kiro_crew.config import sections as _sections
from kiro_crew.config.loader_document import (
    _raw_agent_section,
    _raw_stt_provider,
    unsandboxed_exec_platform_default,
)
from kiro_crew.config.loader_locations import config_path
from kiro_crew.config.sections import (
    _DEFAULT_CHAT_TURN_TIMEOUT_SECS,
    _MANAGED_SERVICE_ENV,
    APPROVAL_TURN_MARGIN_SECS,
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    CHAT_ENTRY_CACHE_BYTES_MAX,
    CHAT_ENTRY_CACHE_BYTES_MIN,
    CHAT_ENTRY_CACHE_ENTRIES_MAX,
    CHAT_ENTRY_CACHE_ENTRIES_MIN,
    CHAT_TURN_TIMEOUT_MAX,
    CHAT_TURN_TIMEOUT_MIN,
    COMPLETION_KEEP_CHARS_MAX,
    COMPLETION_KEEP_CHARS_MIN,
    DEFAULT_POOL_SIZE,
    LOOP_STALL_EXIT_AFTER_DEFAULT,
    LOOP_STALL_EXIT_AFTER_MANAGED_DEFAULT,
    LOOP_STALL_EXIT_AFTER_MAX,
    LOOP_STALL_EXIT_AFTER_MIN,
    MAX_SUBAGENTS_FIXED_FLOOR,
    MCP_PROBE_TIMEOUT_MAX,
    MCP_PROBE_TIMEOUT_MIN,
    POOL_SIZE_MAX,
    POOL_TTL_SECS_MAX,
    POOL_TTL_SECS_MIN,
    RECENT_TINT_COUNT_MAX,
    RECENT_TINT_COUNT_MIN,
    SESSION_START_TIMEOUT_MAX,
    SESSION_START_TIMEOUT_MIN,
    SESSION_TIMEOUT_MAX,
    SESSION_TIMEOUT_MIN,
    SOFT_STOP_BUDGET_MAX,
    SOFT_STOP_BUDGET_MIN,
    SUBAGENT_AUTO_MAX_CEILING,
    SUBAGENT_MAX_TURNS_CEILING,
    TOOL_APPROVAL_TIMEOUT_MAX,
    TOOL_APPROVAL_TIMEOUT_MIN,
    AgentConfig,
    DashboardConfig,
    ExternalRegistryConfig,
    JiraAuthEntry,
    SessionConfig,
    TelemetryConfig,
    _archive_retention_days,
    _coerce_gitlab_hosts,
    _coerce_jira_hosts,
    _normalize_acp_backend,
    _normalize_jail,
    _normalize_yolo_duration,
    _port_or_unset,
    _read_skip_permissions,
    _safe_bool,
    _safe_float,
    _safe_int,
    _safe_list,
    _sanitize_bot_name,
    _tailscale_config_from,
    _validated_completion_keep,
    coerce_fallback_model,
    coerce_role_efforts,
    coerce_role_models,
)
from kiro_crew.constants import SUBAGENT_TIMEOUT_MAX, SUBAGENT_TIMEOUT_MIN
from kiro_crew.session_start_sizing import AUTO as _SESSION_START_AUTO
from kiro_crew.session_start_sizing import is_auto as _session_start_is_auto

logger = logging.getLogger("kiro_crew.config.loader")


# JSON Schema type → Python type names for log messages
_JSON_TYPE_LABELS: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}


def resolve_loop_stall_exit_after(
    dashboard_data: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Resolve the launch-class default while preserving explicit config.

    The distinction between an absent key and an explicit value exists only at
    config load. Managed services widen the absent-key default; every explicit
    operator value, including 25 seconds, is retained.
    """
    data = dashboard_data or {}
    if data.get("loop_stall_exit_after_secs") is not None:
        return _safe_int(
            data.get("loop_stall_exit_after_secs"),
            LOOP_STALL_EXIT_AFTER_DEFAULT,
            LOOP_STALL_EXIT_AFTER_MIN,
            LOOP_STALL_EXIT_AFTER_MAX,
        )
    source = os.environ if environ is None else environ
    # The generated service definition is the sole launch-class authority.
    # Inferring from systemd metadata is ambiguous because descendants inherit
    # INVOCATION_ID; old definitions are reported by ``kirocrew doctor`` with
    # the one-time regeneration command instead.
    managed = source.get(_MANAGED_SERVICE_ENV) == "1"
    return LOOP_STALL_EXIT_AFTER_MANAGED_DEFAULT if managed else LOOP_STALL_EXIT_AFTER_DEFAULT


def consume_managed_service_launch_environment(
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Remove and return the one-shot managed-service launch marker.

    The generated service definition sets this marker for the gateway itself.
    Consuming it before the dashboard starts app backends or child terminals
    prevents those descendants from being misclassified as managed services.
    """
    source = os.environ if environ is None else environ
    value = source.pop(_MANAGED_SERVICE_ENV, None)
    if value is None:
        return {}
    if environ is None:
        # Consumed for descendants only: the gateway's own exec successor (an
        # in-app restart) is the same service launch and gets it back.
        platform_compat.keep_for_reexec(_MANAGED_SERVICE_ENV, value)
    return {_MANAGED_SERVICE_ENV: value}


def launched_as_managed_service() -> bool:
    """Whether a generated service definition launched this process.

    Reads the marker where it still is, or where
    :func:`consume_managed_service_launch_environment` kept it after taking it
    out of the environment, so the answer does not change once the dashboard
    has started.
    """
    marker = os.environ.get(_MANAGED_SERVICE_ENV)
    if marker is None:
        marker = platform_compat.kept_for_reexec().get(_MANAGED_SERVICE_ENV)
    return marker == "1"


def _subagent_timeout_from(raw: object, default: int) -> int:
    """Coerce ``agent.subagent_timeout_secs``, preserving its ``0`` sentinel.

    ``0`` means "use the default" and is normalized by the manager, so it must
    survive coercion: running it through the ``[MIN, MAX]`` clamp turns a
    documented sentinel into a 60-second deadline that kills healthy subagents.
    Coercion still happens here as well as in ``_clamp_security_bounds``, because
    that clamp skips non-int values and a numeric STRING (``"30"``) reaches this
    site unbounded. A value it cannot read takes *default*, the field's.
    """
    value = _safe_int(raw, default, 0, SUBAGENT_TIMEOUT_MAX)
    return value if value == 0 else max(SUBAGENT_TIMEOUT_MIN, value)


def _clamp_compact_wait_secs(raw: object, default: float) -> float:
    """Coerce ``session.compact_wait_secs``, preserving its ``0`` sentinel.

    ``0`` means "use the built-in budget" and the resolver falls back to
    ``COMPACT_WAIT_TIMEOUT_SECS`` for it, so it must survive coercion. A
    positive value is lifted to at least ``COMPACT_WAIT_SECS_MIN`` and capped
    at ``COMPACT_WAIT_SECS_MAX``: ``_safe_float`` with ``lo=0`` collapses a
    negative to the sentinel, then the floor keeps a hand-edited near-zero
    value from arming a budget that restarts every compaction. A value it cannot
    read takes *default*, the field's.
    """
    value = _safe_float(raw, default, lo=0.0, hi=_sections.COMPACT_WAIT_SECS_MAX)
    return value if value == 0 else max(_sections.COMPACT_WAIT_SECS_MIN, value)


_DEFAULT_MEMORY_MODES = frozenset({"persistent", "incognito", "temporary"})


def _default_memory_mode_from(raw: object) -> str:
    """Normalize a stored dashboard default without failing open on corruption."""
    return raw if isinstance(raw, str) and raw in _DEFAULT_MEMORY_MODES else "temporary"


def _folder_sort_from(raw: object, default: str) -> str:
    """Normalize the sidebar folder sort mode; anything unknown is *default*, ``custom``.

    ``custom`` is the stored-order behaviour every install had before the field
    existed, so a missing, hand-edited or downgraded value changes nothing the
    person sees. The sidebar's own reader makes the same choice.
    """
    if isinstance(raw, str) and raw in _sections.FOLDER_SORT_MODES:
        return raw
    return default


def _translucent_panels_from(raw: object) -> str:
    """Normalize the Translucent panels choice; anything unknown reads as ``""``.

    ``""`` is "never chosen on this workspace": the frontend then keeps the
    browser's own stored choice, which is what every install did before the key
    existed, so a missing or hand-edited value changes nothing the person sees.
    """
    if isinstance(raw, str) and raw in _sections.TRANSLUCENT_PANELS_VALUES:
        return raw
    return ""


def _session_sort_from(raw: object, default: str) -> str:
    """Normalize the sidebar session sort key; anything unknown is *default*, ``date-desc``.

    ``date-desc`` (latest activity first) is the order the sidebar draws with no
    stored choice, so a missing or hand-edited value changes nothing the person
    sees. The sidebar's own reader makes the same choice.
    """
    if isinstance(raw, str) and raw in _sections.SESSION_SORT_MODES:
        return raw
    return default


# (section, key, min, max) for each bounded field clamped at load time. The
# mins match the runtime floors: subagent_auto_max has a floor of 3
# (``subagent._LEGACY_DEFAULT_MAX`` — the auto-size minimum), so a value < 3 is
# clamped UP to 3 with a warning, mirroring the > ceiling clamp. max_subagents
# keeps a 0 floor here (0 = auto sentinel) — its 0-or-(>=3) rule is applied as a
# special case after the generic loop. subagent_timeout_secs keeps a 0 floor for
# the same reason: 0 is its documented "use the default" sentinel, which the
# manager normalizes, so a MIN floor in the generic loop would turn it into a
# 60-second deadline. Only out-of-range values are altered.
_SECURITY_BOUNDED_FIELDS: tuple[tuple[str, str, int, int], ...] = (
    ("agent", "subagent_auto_max", 3, SUBAGENT_AUTO_MAX_CEILING),
    ("agent", "max_subagents", 0, SUBAGENT_AUTO_MAX_CEILING),
    ("agent", "subagent_max_turns", 1, SUBAGENT_MAX_TURNS_CEILING),
    ("agent", "subagent_timeout_secs", 0, SUBAGENT_TIMEOUT_MAX),
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
    (
        "dashboard",
        "chat_entry_cache_max_entries",
        CHAT_ENTRY_CACHE_ENTRIES_MIN,
        CHAT_ENTRY_CACHE_ENTRIES_MAX,
    ),
    (
        "dashboard",
        "chat_entry_cache_max_bytes",
        CHAT_ENTRY_CACHE_BYTES_MIN,
        CHAT_ENTRY_CACHE_BYTES_MAX,
    ),
    ("session", "pool_size", 0, POOL_SIZE_MAX),
    # A kill budget belongs in this sweep for the reason the sweep exists: the
    # value authorizes signals, and a hand-edited config.json never passes the
    # dashboard's write gate. The floor is 0 because 0 is this field's OFF value, so
    # a negative clamps toward observe-only rather than toward killing.
    ("session", "reconcile_max_kills", 0, _sections.RECONCILE_MAX_KILLS_MAX),
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

    # subagent_timeout_secs special case, and the reason its table floor is 0:
    # 0 is the field's documented "use the default" sentinel, which the manager
    # normalizes (``SubagentManager.__init__``). A MIN floor in the generic loop
    # would rewrite that 0 to 60 and hand a healthy subagent a one-minute
    # deadline, so 0 is preserved here and only a NON-zero value below the floor
    # is raised to it.
    if isinstance(agent, dict):
        st = agent.get("subagent_timeout_secs")
        if isinstance(st, int) and not isinstance(st, bool) and 0 < st < SUBAGENT_TIMEOUT_MIN:
            agent["subagent_timeout_secs"] = SUBAGENT_TIMEOUT_MIN
            logger.warning(
                "config agent.subagent_timeout_secs=%d is below the floor of %d "
                "(0 = use the default); clamped UP to %d",
                st,
                SUBAGENT_TIMEOUT_MIN,
                SUBAGENT_TIMEOUT_MIN,
            )
            _log_config_clamp_event(
                "agent.subagent_timeout_secs",
                st,
                SUBAGENT_TIMEOUT_MIN,
                SUBAGENT_TIMEOUT_MIN,
                SUBAGENT_TIMEOUT_MAX,
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


def _session_start_concurrency(raw: object, ceiling: int = 64) -> int | str:
    """A start width: ``"auto"`` or an int clamped to ``1..ceiling``.

    ``agent.session_start_concurrency`` uses the default ceiling of 64;
    ``agent.cold_start_concurrency`` and ``agent.runtime_spawn_concurrency`` pass
    32. Anything that is neither (missing, a bool, a non-integral float, a
    non-numeric string) falls back to the ``"auto"`` default, as a junk value of
    any other int knob falls back to its default.
    """
    if _session_start_is_auto(raw) or isinstance(raw, bool):
        return _SESSION_START_AUTO
    if isinstance(raw, float) and not raw.is_integer():
        return _SESSION_START_AUTO
    try:
        value = int(raw)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        return _SESSION_START_AUTO
    return max(1, min(ceiling, value))


def _read_backend_choice(section: _sections.SectionReader, agent_data: dict, key: str) -> str:
    """Resolve a stored harness field to a runtime backend id at the one gate.

    The config-file vocabulary is translated first
    (:func:`~kiro_crew.acp_backends.backend_from_stored`): ``"kiro"`` is the Kiro
    CLI choice, while absent and ``""`` are "no choice" and read the field default.
    A default is not a pick, so when the registry does not serve it (a policy
    denies KAS) the gate gets ``None`` and takes the floor silently instead of
    warning on every load about a value nobody wrote. Any other stored value goes
    to the gate as is, so an unusable explicit pick still degrades with its
    warning (harness-parity H3). ``validate_config_data`` replaces a present
    non-string with the stored floor before this runs.

    For ``agent.member_acp_backend`` a stored ``"kiro"`` reads as KAS: crewmates
    run on KAS, the Kiro backend that carries the per-session dispatch tools a
    member thread needs. Mapping here, before the gate, gives every reader of the
    field the backend the member thread actually gets, and the mapped value still
    crosses the gate like any other pick.
    """
    from kiro_crew.acp_backends import (
        ACP_BACKEND_KAS,
        ACP_BACKEND_KIRO,
        backend_from_stored,
        selectable_backends,
    )

    value = backend_from_stored(agent_data.get(key))
    if value is None:
        default = section.default(key)
        value = default if default in selectable_backends() else None
    elif key == _MEMBER_HARNESS_KEY and value == ACP_BACKEND_KIRO:
        value = ACP_BACKEND_KAS
        _note_member_remap(agent_data.get(key), value)
    return _normalize_acp_backend(value)


#: Whether the member-backend remap was logged in this process (see below).
_member_remap_logged = False


def _note_member_remap(stored: object, used: str) -> None:
    """Say once per process that a stored ``member_acp_backend`` runs as another backend."""
    global _member_remap_logged
    if _member_remap_logged:
        return
    _member_remap_logged = True
    logger.info(
        "agent.member_acp_backend is stored as %r; crewmate DMs run on %r, the Kiro "
        "backend that carries their dispatch tools (config.json is left as it is)",
        stored,
        used,
    )


#: The harness field crewmate DM sessions run on (see :func:`_read_backend_choice`).
_MEMBER_HARNESS_KEY = "member_acp_backend"


#: The ``agent`` fields that hold a harness id, spelled in the config-file
#: vocabulary on disk and in ``to_dict()`` and as runtime ids on the dataclass.
_HARNESS_CHOICE_KEYS: tuple[str, ...] = ("acp_backend", "member_acp_backend")


def _stored_agent_dict(agent: AgentConfig) -> dict:
    """``asdict(agent)`` with each harness field in the config-file vocabulary.

    ``to_dict()`` is what ``config get``, the dashboard config response and every
    full-document write read, so a resolved Kiro CLI harness reads back as
    ``"kiro"`` rather than the runtime id ``""``, which on disk means "no choice".
    """
    from dataclasses import asdict

    from kiro_crew.acp_backends import backend_to_stored

    d = asdict(agent)
    for key in _HARNESS_CHOICE_KEYS:
        if isinstance(d.get(key), str):
            d[key] = backend_to_stored(d[key])
    return d


def _restore_stored_harness_fields(doc: dict) -> None:
    """In place: set *doc*'s harness fields to the raw ``config.json`` values.

    A key the base file does not hold is removed, so a full-document write never
    invents a choice; a key it holds is written back verbatim, so a resolved
    floor never replaces a stored pick. Read from disk for the same reason as
    :func:`unsandboxed_exec_declared`.
    """
    stored_agent = _raw_agent_section(config_path()) or {}
    agent_section = doc.get("agent")
    if not isinstance(agent_section, dict):
        if not any(key in stored_agent for key in _HARNESS_CHOICE_KEYS):
            return
        agent_section = doc["agent"] = {}
    for key in _HARNESS_CHOICE_KEYS:
        if key in stored_agent:
            agent_section[key] = stored_agent[key]
        else:
            agent_section.pop(key, None)


def _restore_stored_stt_provider(doc: dict) -> None:
    """In place: keep a stored edition STT provider the snapshot could not see.

    A config loaded before boot, or by a command that never boots, resolves
    ``stt.provider`` against the built-ins alone, so a stored edition name such as
    ``acme`` reads as ``off``. Publishing that snapshot would turn a valid choice
    into ``off`` for good. So when the stored value is such a name (a string that
    is neither built-in nor retired), the snapshot holds the loader's degraded
    ``off``, and the edition registry is still pending or registers the name,
    the stored value is written back instead.

    A standalone host has no pending registry and no registered names, so this
    never changes what it writes: an unknown name there still saves as ``off``.
    """
    stt_section = doc.get("stt")
    if not isinstance(stt_section, dict) or stt_section.get("provider") != "off":
        return
    stored = _raw_stt_provider(config_path())
    if not isinstance(stored, str) or not stored:
        return
    if stored in _sections._VALID_STT_PROVIDERS or stored in _sections._RETIRED_STT_PROVIDERS:
        return
    # Imported here: this module sits below the platform context it reads.
    from kiro_crew import speech_providers

    if speech_providers.registry_pending() or stored in speech_providers.registered_stt_names():
        stt_section["provider"] = stored


# Build each section in its own frame: the large inline constructor amplifies
# line-tracing cost on every load. These helpers create fresh values, never
# cache configuration, and leave resolution and admission checks unchanged.
def _crew_mode_agent_or_default(value: object) -> str:
    """``agent.crew_mode_agent`` as a trimmed name; the shipped conductor for anything else.

    An empty or non-string value must not turn Crew Mode into "no agent", which the
    slot switch reads as the default template: Crew Mode would then be a plain chat.
    """
    from kiro_crew.crew_mode import DEFAULT_CREW_MODE_AGENT

    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_CREW_MODE_AGENT


def _build_agent_config(agent_data: dict) -> AgentConfig:
    section = _sections.SectionReader(AgentConfig, agent_data)
    return AgentConfig(
        approval_mode=section.get("approval_mode"),
        streaming=section.get("streaming"),
        model=section.get("model"),
        role_models=coerce_role_models(section.get("role_models")),
        role_efforts=coerce_role_efforts(section.get("role_efforts")),
        model_context_tokens=section.get("model_context_tokens"),
        fallback_model=coerce_fallback_model(section.get("fallback_model")),
        refusal_fallback_model=_sections.coerce_refusal_fallback_model(
            section.get("refusal_fallback_model")
        ),
        reasoning_effort=section.get("reasoning_effort"),
        provider=section.get("provider"),
        mcp_registry_mode=section.read("mcp_registry_mode", _safe_bool),
        mcp_quarantine_after_failures=section.read("mcp_quarantine_after_failures", _safe_int),
        acp_backend=_read_backend_choice(section, agent_data, "acp_backend"),
        member_acp_backend=_read_backend_choice(section, agent_data, "member_acp_backend"),
        kas_bundle=section.read("kas_bundle", _safe_bool),
        kas_persist_mcp_sign_in=section.read("kas_persist_mcp_sign_in", _safe_bool),
        kas_bundle_url=_sections.coerce_kas_bundle_url(section.get("kas_bundle_url")),
        native_skill_projection=section.read("native_skill_projection", _safe_bool),
        default_agent=section.get("default_agent"),
        crew_mode_agent=_crew_mode_agent_or_default(section.get("crew_mode_agent")),
        # Through the module alias rather than a new top-level import: the loader's
        # ``from ... import`` list is a FROZEN pre-split compatibility snapshot
        # (``test_config_module_boundaries.test_loader_reexports_historical_snapshot_by_identity``),
        # so a new name joins it only by being an old one. Same shape as
        # ``coerce_refusal_fallback_model`` above.
        deepseek_env=_sections.coerce_deepseek_env(section.get("deepseek_env")),
        sweep_agents_backups=section.read("sweep_agents_backups", _safe_bool),
        sandbox=section.get("sandbox"),
        sandbox_allow_no_isolation=bool(section.get("sandbox_allow_no_isolation")),
        sandbox_allow_unsandboxed_exec=bool(
            agent_data.get(
                "sandbox_allow_unsandboxed_exec",
                unsandboxed_exec_platform_default(),
            )
        ),
        apps_allow_third_party=section.read("apps_allow_third_party", _safe_bool),
        apps_trusted=(
            [a for a in _trusted if isinstance(a, str) and a]
            if isinstance(_trusted := section.get("apps_trusted"), list)
            else section.default("apps_trusted")
        ),
        apps_trusted_local=(
            [a for a in _trusted_local if isinstance(a, str) and a]
            if isinstance(_trusted_local := section.get("apps_trusted_local"), list)
            else section.default("apps_trusted_local")
        ),
        apps_trusted_repositories=(
            {
                name: repository
                for name, repository in _trusted_repositories.items()
                if isinstance(name, str) and isinstance(repository, str) and name and repository
            }
            if isinstance(
                _trusted_repositories := section.get("apps_trusted_repositories"),
                dict,
            )
            else section.default("apps_trusted_repositories")
        ),
        apps_default_installs_applied=(
            [a for a in _applied if isinstance(a, str) and a]
            if isinstance(_applied := section.get("apps_default_installs_applied"), list)
            else section.default("apps_default_installs_applied")
        ),
        apps_ui_stream_timeout_secs=section.read("apps_ui_stream_timeout_secs", _safe_int, 5, 600),
        jail=_normalize_jail(section.get("jail")),
        dangerously_skip_permissions=_read_skip_permissions(agent_data),
        yolo_duration=section.read("yolo_duration", _normalize_yolo_duration),
        notify_override_expiry=section.get("notify_override_expiry"),
        tool_search=bool(section.get("tool_search")),
        tool_search_min_pct=section.read("tool_search_min_pct", _safe_int),
        tool_search_min_tokens=section.read("tool_search_min_tokens", _safe_int),
        session_sharing=bool(section.get("session_sharing")),
        max_subagents=section.read("max_subagents", _safe_int, 0, SUBAGENT_AUTO_MAX_CEILING),
        max_stop_hook_nudges=section.read("max_stop_hook_nudges", _safe_int, 0),
        subagent_mem_buffer_pct=section.read("subagent_mem_buffer_pct", _safe_int),
        chat_turn_timeout_secs=section.read(
            "chat_turn_timeout_secs", _safe_int, CHAT_TURN_TIMEOUT_MIN, CHAT_TURN_TIMEOUT_MAX
        ),
        session_start_timeout_secs=section.read(
            "session_start_timeout_secs",
            _safe_int,
            SESSION_START_TIMEOUT_MIN,
            SESSION_START_TIMEOUT_MAX,
        ),
        tool_approval_timeout_secs=section.read(
            "tool_approval_timeout_secs",
            _safe_int,
            TOOL_APPROVAL_TIMEOUT_MIN,
            TOOL_APPROVAL_TIMEOUT_MAX,
        ),
        # Absent means ON. The grant that decides who may reach a peer
        # session is the AGENT CONFIG, not this switch: the tools come
        # from the `kirocrew-dashboard` MCP server, so an agent that does
        # not mount it never has them -- the same rule as every other MCP
        # server. This stays as a single withdrawal for an operator who
        # wants the capability gone from every agent at once without
        # editing each spec, so an EXPLICIT `false` must still disable it.
        # A present-but-malformed value -- the quoted `"false"` an operator
        # writes by mistake -- is coerced to False upstream, BEFORE schema
        # validation, so it cannot ride the missing-field default back to
        # true; see the normalization above the `_validate_config_data`
        # call. `_safe_bool` here is the final guard for a real bool.
        session_control=section.read("session_control", _safe_bool),
        # Default false: a missing key and a malformed one both leave it off.
        session_manager_chain=section.read("session_manager_chain", _safe_bool),
        # Default true preserves the zero-configuration member-dispatch
        # grant (today's behaviour) for a MISSING key. A present-but-
        # malformed value was already coerced to False upstream, BEFORE
        # schema validation, so it cannot ride the missing-field default
        # back to true — see the `member_dispatch` normalization above
        # the `_validate_config_data` call. `_safe_bool` here is the
        # final guard for a real bool.
        member_dispatch=section.read("member_dispatch", _safe_bool),
        # Default true is the zero-configuration panel grant, and the guard is the
        # one above: a present-but-malformed value was already coerced to False
        # upstream, BEFORE schema validation, so it cannot ride the missing-field
        # default back to true. `_safe_bool` here is the final guard for a real
        # bool.
        crew_panel=section.read("crew_panel", _safe_bool),
        subagent_cost_gb=section.read("subagent_cost_gb", _safe_float),
        subagent_cpu_cost_cores=section.read("subagent_cpu_cost_cores", _safe_float),
        subagent_auto_max=section.read(
            "subagent_auto_max", _safe_int, 3, SUBAGENT_AUTO_MAX_CEILING
        ),
        subagent_spawn_stagger_secs=section.read("subagent_spawn_stagger_secs", _safe_float),
        spawn_min_memory_gb=section.read("spawn_min_memory_gb", _safe_float),
        resource_pressure_gb=section.read("resource_pressure_gb", _safe_float),
        resource_critical_gb=section.read("resource_critical_gb", _safe_float),
        admission_gate=section.read("admission_gate", _safe_bool),
        # Durable task queue keys, adjacent to admission_gate because a
        # gated spawn is what the queue defers instead of refusing.
        task_queue_enabled=section.read("task_queue_enabled", _safe_bool),
        task_dispatch_window=section.read("task_dispatch_window", _safe_int, 1, 4096),
        task_store_journal_mode=(
            journal_mode
            if (
                journal_mode := str(
                    section.get("task_store_journal_mode")
                    or section.default("task_store_journal_mode")
                ).lower()
            )
            in ("auto", "wal", "delete")
            else section.default("task_store_journal_mode")
        ),
        admit_wait_secs=section.read("admit_wait_secs", _safe_int, 1, 3600),
        subagent_queue_max_wait_secs=section.read(
            "subagent_queue_max_wait_secs", _safe_int, 0, 86400
        ),
        start_collect_timeout_secs=section.read("start_collect_timeout_secs", _safe_int, 10, 3600),
        # Fairness lanes (taskq/lanes.py): weights shape the share of
        # picks; the reserve keeps children startable under full parents.
        lane_weights=(
            {
                lane: _safe_int(weight, 1, 1, 64)
                for lane, weight in _lane_weights.items()
                if isinstance(lane, str) and lane
            }
            if isinstance(_lane_weights := section.get("lane_weights"), dict)
            else section.default("lane_weights")
        ),
        child_reserve=section.read("child_reserve", _safe_int, 0, 8),
        # Shared recovery ladder schedule (recovery/policy.py bounds).
        recovery_backoff_base_secs=section.read(
            "recovery_backoff_base_secs", _safe_float, 0.1, 60.0
        ),
        recovery_backoff_max_secs=section.read(
            "recovery_backoff_max_secs", _safe_float, 1.0, 3600.0
        ),
        # Session-start gate (acp/runtime_start.py SessionStartGate).
        # "auto" (default) is sized from the host once per process by
        # session_start_sizing; an explicit integer keeps the 1..64 clamp.
        session_start_concurrency=_session_start_concurrency(
            agent_data.get("session_start_concurrency", "auto")
        ),
        # Env defaults for spawned kiro-cli children (acp/child_env_defaults.py).
        # Through the module alias: the loader's import list is a frozen snapshot.
        child_env_defaults=_sections.coerce_child_env_defaults(section.get("child_env_defaults")),
        # Operator env for every agent child (acp/agent_env.py), same alias reason.
        env=_sections.coerce_agent_env(section.get("env")),
        # Cold-start queues (session_allocation _start_sem, acp/runtime_start
        # spawn admission); resolved by cold_start_sizing.
        cold_start_concurrency=_session_start_concurrency(
            agent_data.get("cold_start_concurrency"), ceiling=32
        ),
        runtime_spawn_concurrency=_session_start_concurrency(
            agent_data.get("runtime_spawn_concurrency"), ceiling=32
        ),
        # Adaptive controller (adaptive/policy.py params_from_config).
        adaptive_concurrency=section.read("adaptive_concurrency", _safe_bool),
        adaptive_concurrency_mode=(
            "fixed"
            if section.get("adaptive_concurrency_mode") == "fixed"
            else section.default("adaptive_concurrency_mode")
        ),
        adaptive_floor=section.read("adaptive_floor", _safe_int, 1, 64),
        adaptive_initial=section.read("adaptive_initial", _safe_int, 1, 64),
        adaptive_slow_start=section.read("adaptive_slow_start", _safe_bool),
        controller_sample_secs=section.read("controller_sample_secs", _safe_int, 1, 300),
        # Dependency coordinator (taskq/dependency.py coordinator_from_config).
        dependency_max_attempts=section.read("dependency_max_attempts", _safe_int, 1, 1000),
        dependency_wait_deadline_secs=section.read(
            "dependency_wait_deadline_secs", _safe_int, 0, 86400
        ),
        dependency_wake_per_tick=section.read("dependency_wake_per_tick", _safe_int, 0, 4096),
        dependency_wake_spacing_secs=section.read(
            "dependency_wake_spacing_secs", _safe_float, 0.0, 60.0
        ),
        # Tool-stall watchdog (acp/session_handle_watchdog.py WatchdogSettings).
        interactive_command_policy=(
            "wait"
            if section.get("interactive_command_policy") == "wait"
            else section.default("interactive_command_policy")
        ),
        subagent_max_turns=section.read(
            "subagent_max_turns", _safe_int, 1, SUBAGENT_MAX_TURNS_CEILING
        ),
        subagent_timeout_secs=section.read("subagent_timeout_secs", _subagent_timeout_from),
        subagent_stall_idle_secs=section.read("subagent_stall_idle_secs", _safe_int),
        completion_keep=_validated_completion_keep(section.get("completion_keep")),
        completion_keep_chars=section.read(
            "completion_keep_chars", _safe_int, COMPLETION_KEEP_CHARS_MIN, COMPLETION_KEEP_CHARS_MAX
        ),
        subagent_result_ttl_secs=section.read("subagent_result_ttl_secs", _safe_int),
        # Same band workflows/service.py clamp_run_timeout enforces, so a
        # hand-edited file and the live-bound setter agree.
        workflow_run_timeout_secs=section.read("workflow_run_timeout_secs", _safe_int, 60, 21600),
        subagent_cwd_allowed_roots=(
            [r for r in _roots if isinstance(r, str)]
            if isinstance(_roots := agent_data.get("subagent_cwd_allowed_roots"), list)
            else section.default("subagent_cwd_allowed_roots")
        ),
        log_level=(
            lvl.upper()
            if isinstance(lvl := section.get("log_level"), str)
            else section.default("log_level")
        ),
        bot_name=_sanitize_bot_name(section.get("bot_name")),
        max_channels=section.get("max_channels"),
        max_channel_agents=section.get("max_channel_agents"),
        soft_stop_budget_secs=max(
            SOFT_STOP_BUDGET_MIN,
            min(
                SOFT_STOP_BUDGET_MAX,
                section.read("soft_stop_budget_secs", _safe_float),
            ),
        ),
    )


def _load_idle_exempt_keys(raw: object) -> list[str]:
    """Return the configured keys, stripped because the idle sweep matches them exactly.

    A non-list loads as no exemptions. An over-long key is dropped rather than
    truncated, because a truncated key could match a different session.
    """
    if not isinstance(raw, list):
        return []

    idle_exempt_keys: list[str] = []
    overlong_count = 0
    overflow_count = 0
    for candidate in raw:
        if not isinstance(candidate, str):
            continue
        key = candidate.strip()
        if not key:
            continue
        if len(key) > _sections.IDLE_EXEMPT_KEY_MAX_CHARS:
            overlong_count += 1
            continue
        if len(idle_exempt_keys) >= _sections.IDLE_EXEMPT_KEYS_MAX:
            overflow_count += 1
            continue
        idle_exempt_keys.append(key)

    if overlong_count or overflow_count:
        logger.warning(
            "session.idle_exempt_keys: dropped %d key(s) longer than %d characters "
            "and %d past the %d-key limit; kept %d",
            overlong_count,
            _sections.IDLE_EXEMPT_KEY_MAX_CHARS,
            overflow_count,
            _sections.IDLE_EXEMPT_KEYS_MAX,
            len(idle_exempt_keys),
        )
    return idle_exempt_keys


def _build_session_config(session_data: dict) -> SessionConfig:
    section = _sections.SectionReader(SessionConfig, session_data)
    return SessionConfig(
        # The only field in this group whose site had no `_safe_int` at all, so
        # it is added here for consistency -- but NOT because the type was
        # unhandled. Verified: on the base revision a hand-edited `"abc"` or
        # `true` already loaded as the 3600 default, because
        # `_validate_config_data` runs over the raw dict before section
        # extraction and owns type handling. What was missing for this field, as
        # for the other ten, is the RANGE: an int of 999999999 loaded verbatim.
        timeout_secs=section.read(
            "timeout_secs", _safe_int, SESSION_TIMEOUT_MIN, SESSION_TIMEOUT_MAX
        ),
        idle_exempt_keys=_load_idle_exempt_keys(section.get("idle_exempt_keys")),
        empty_response_auto_continue=bool(section.get("empty_response_auto_continue")),
        # RANGE-clamped like the other session ints: a hand-edited 0
        # or 999 must load as a sane budget, never disable recovery or
        # arm an unbounded ladder. Type handling is owned by
        # `_validate_config_data` upstream of section extraction. The
        # bounds are referenced via the module handle rather than
        # imported: this module's top-level names are a FROZEN
        # compatibility facade (test_loader_reexports_historical
        # _snapshot_by_identity), so a new re-export may not be added.
        empty_response_max_continues=section.read(
            "empty_response_max_continues",
            _safe_int,
            _sections.EMPTY_RESPONSE_MAX_CONTINUES_MIN,
            _sections.EMPTY_RESPONSE_MAX_CONTINUES_MAX,
        ),
        autocompact_pct=section.read(
            "autocompact_pct", _safe_float, lo=AUTOCOMPACT_PCT_MIN, hi=AUTOCOMPACT_PCT_MAX
        ),
        context_cap=bool(section.get("context_cap")),
        # Clamped on the read, like the sibling floats: 0 is the sentinel for
        # "use the built-in budget" and any positive value is the wait, so a
        # hand-edited negative collapses to 0 (fallback) and an oversized value
        # is capped. Bounds are referenced via the module handle, not imported:
        # this module's top-level names are a frozen compatibility facade.
        compact_wait_secs=section.read("compact_wait_secs", _clamp_compact_wait_secs),
        pool_size=_safe_int(
            session_data.get("pool_size", DEFAULT_POOL_SIZE),
            DEFAULT_POOL_SIZE,
            0,
            POOL_SIZE_MAX,
        ),
        pool_agent=str(section.get("pool_agent")),
        pool_ttl_secs=section.read(
            "pool_ttl_secs", _safe_int, POOL_TTL_SECS_MIN, POOL_TTL_SECS_MAX
        ),
        eager_spawn=bool(section.get("eager_spawn")),
        archive_retention_days=_archive_retention_days(session_data),
        # Bounds via the module handle: this module's names are a frozen facade.
        file_changes_keep_turns=section.read(
            "file_changes_keep_turns",
            _safe_int,
            _sections.FILE_CHANGES_KEEP_TURNS_MIN,
        ),
        watchdog_rss_max_mb=section.read("watchdog_rss_max_mb", _safe_int),
        # Clamped HERE as well as in the `_SECURITY_BOUNDED_FIELDS` sweep, for the
        # reason `_safe_int` states: that sweep runs over the raw dict and skips
        # non-int values, so a numeric STRING passes it and coerces here.
        reconcile_max_kills=section.read(
            "reconcile_max_kills", _safe_int, 0, _sections.RECONCILE_MAX_KILLS_MAX
        ),
    )


def _build_telemetry_config(telemetry_data: dict) -> TelemetryConfig:
    section = _sections.SectionReader(TelemetryConfig, telemetry_data)
    return TelemetryConfig(
        enabled=bool(section.get("enabled")),
        local_dir=str(section.get("local_dir")),
        export_interval_seconds=section.read("export_interval_seconds", _safe_int),
        retention_days=section.read("retention_days", _safe_int),
        max_total_mb=section.read("max_total_mb", _safe_int),
        otlp_endpoint=str(section.get("otlp_endpoint")),
        beacon_enabled=bool(section.get("beacon_enabled")),
        beacon_endpoint=str(section.get("beacon_endpoint")),
    )


def _title_refresh_every_turns(value: object, default: int) -> int:
    """Parse ``dashboard.title_refresh_every_turns``: 0, or MIN..MAX.

    0 and every negative value parse to 0, the built-in schedule. Any value
    ``_safe_int`` rejects (a bool, a fractional float, unparseable text) takes
    *default*, the field's, which is that same 0. A value from 1 to MIN - 1 is
    raised to MIN, so a user who asked for frequent refreshes gets the most
    frequent cadence allowed rather than the built-in schedule. A value above MAX
    is capped at MAX.
    """
    every = _safe_int(value, default, 0, _sections.TITLE_REFRESH_EVERY_TURNS_MAX)
    return every if every == 0 else max(every, _sections.TITLE_REFRESH_EVERY_TURNS_MIN)


def _session_folder_root_or_empty(value: object) -> str:
    """``dashboard.session_folder_root`` as a trimmed string; "" (off) for any non-string.

    A hand-edited number or list must read as "off", never as a path built from its
    ``str()``: the value names the directory new session folders are created in.
    """
    return value.strip() if isinstance(value, str) else ""


def _default_session_folder_or_empty(value: object) -> str:
    """``dashboard.default_session_folder`` as a trimmed string; "" (unfiled) for a non-string.

    Whether the value names a folder is decided at each create, against the
    folder store, so a value that names none reads as unfiled there.
    """
    return value.strip() if isinstance(value, str) else ""


def _build_dashboard_config(_degraded: set[str], dashboard_data: dict) -> DashboardConfig:
    section = _sections.SectionReader(DashboardConfig, dashboard_data)
    gitlab_hosts = _coerce_gitlab_hosts(section.get("gitlab_hosts"))
    jira_hosts = _coerce_jira_hosts(section.get("jira_hosts"))
    github_hosts = _sections._coerce_github_hosts(
        section.get("github_hosts"), claimed=(*gitlab_hosts, *jira_hosts)
    )
    bitbucket_server_hosts = _sections._coerce_bitbucket_server_hosts(
        section.get("bitbucket_server_hosts"),
        claimed=(*gitlab_hosts, *jira_hosts, *github_hosts),
    )
    return DashboardConfig(
        url=section.get("url"),
        tailscale=_tailscale_config_from(
            dashboard_data.get("tailscale"),
            _degraded,
            key_present="tailscale" in dashboard_data,
        ),
        shared_crew=_sections.shared_crew_config_from(dashboard_data.get("shared_crew")),
        restore_sessions=section.get("restore_sessions"),
        crewmate_threads=section.read("crewmate_threads", _safe_bool),
        crewmates_in_agent_picker=section.read("crewmates_in_agent_picker", _safe_bool),
        dynamic_dashboard_cards=section.read("dynamic_dashboard_cards", _safe_bool),
        qr_session_until_restart=section.read("qr_session_until_restart", _safe_bool),
        qr_session_persist_across_restart=section.read(
            "qr_session_persist_across_restart", _safe_bool
        ),
        restore_window_minutes=section.get("restore_window_minutes"),
        idle_slot_sweep_days=section.read("idle_slot_sweep_days", _safe_int, 0, 3650),
        surface_channel_sessions=section.get("surface_channel_sessions"),
        bot_name=section.get("bot_name"),
        avatar=section.get("avatar"),
        merge_queued_messages=section.get("merge_queued_messages"),
        title_refresh_every_turns=section.read(
            "title_refresh_every_turns", _title_refresh_every_turns
        ),
        title_ticket_prefix=section.read("title_ticket_prefix", _safe_bool),
        upload_max_mb=section.read(
            "upload_max_mb",
            _safe_int,
            _sections.UPLOAD_MAX_MB_MIN,
            _sections.UPLOAD_MAX_MB_MAX,
        ),
        mcp_probe_timeout_secs=section.read(
            "mcp_probe_timeout_secs", _safe_int, MCP_PROBE_TIMEOUT_MIN, MCP_PROBE_TIMEOUT_MAX
        ),
        loop_stall_exit_after_secs=(
            None
            if (loop_stall := section.get("loop_stall_exit_after_secs")) is None
            else _safe_int(
                loop_stall,
                LOOP_STALL_EXIT_AFTER_DEFAULT,
                LOOP_STALL_EXIT_AFTER_MIN,
                LOOP_STALL_EXIT_AFTER_MAX,
            )
        ),
        chat_entry_cache_max_entries=section.read(
            "chat_entry_cache_max_entries",
            _safe_int,
            CHAT_ENTRY_CACHE_ENTRIES_MIN,
            CHAT_ENTRY_CACHE_ENTRIES_MAX,
        ),
        chat_entry_cache_max_bytes=section.read(
            "chat_entry_cache_max_bytes",
            _safe_int,
            CHAT_ENTRY_CACHE_BYTES_MIN,
            CHAT_ENTRY_CACHE_BYTES_MAX,
        ),
        cautious_boot=section.read("cautious_boot", _safe_bool),
        auto_open_browser=section.get("auto_open_browser"),
        prevent_sleep=section.read("prevent_sleep", _safe_bool),
        quick_send=section.get("quick_send"),
        model_picker_configured=(
            section.read("model_picker_configured", _safe_bool)
            if "model_picker_configured" in dashboard_data
            else any(
                isinstance(raw, str) and raw.strip() not in ("", "auto")
                for raw in _safe_list(section.get("model_picker_hidden_models"))
            )
        ),
        model_picker_hidden_models=list(
            dict.fromkeys(
                model
                for raw in _safe_list(section.get("model_picker_hidden_models"))
                if isinstance(raw, str) and (model := raw.strip()) and model != "auto"
            )
        ),
        session_grid=section.get("session_grid"),
        mcp_app_panel=section.get("mcp_app_panel"),
        auto_open_git_panel=section.read("auto_open_git_panel", _safe_bool),
        side_panel_always_open=section.read("side_panel_always_open", _safe_bool),
        side_panel_pin_summary=section.read("side_panel_pin_summary", _safe_bool),
        session_card_source_links=section.read("session_card_source_links", _safe_bool),
        default_memory_mode=_default_memory_mode_from(section.get("default_memory_mode")),
        widget_density=section.get("widget_density"),
        use_builtin_browser=section.read("use_builtin_browser", _safe_bool),
        browser_local_origins=_sections.coerce_browser_local_origins(
            section.get("browser_local_origins")
        ),
        browser_view_port=_port_or_unset(section.get("browser_view_port")),
        verbosity=_sections.normalize_verbosity(section.get("verbosity")),
        link_previews=section.read("link_previews", _safe_bool),
        tail_fork_enabled=section.get("tail_fork_enabled"),
        terminal=section.get("terminal"),
        default_project=section.get("default_project"),
        session_folder_root=_session_folder_root_or_empty(section.get("session_folder_root")),
        default_session_folder=_default_session_folder_or_empty(
            section.get("default_session_folder")
        ),
        theme_mode=section.get("theme_mode"),
        sso_login_flags=str(section.get("sso_login_flags")),
        theme_color=section.get("theme_color"),
        language=str(section.get("language")),
        recent_tint_count=section.read(
            "recent_tint_count", _safe_int, RECENT_TINT_COUNT_MIN, RECENT_TINT_COUNT_MAX
        ),
        show_unread_dot=section.read("show_unread_dot", _safe_bool),
        folder_sort=section.read("folder_sort", _folder_sort_from),
        session_sort=section.read("session_sort", _session_sort_from),
        update_nudge=(
            nudge
            if isinstance(nudge := section.get("update_nudge"), dict)
            else section.default("update_nudge")
        ),
        translucent_panels=_translucent_panels_from(section.get("translucent_panels")),
        onboarded=bool(section.get("onboarded")),
        import_onboarded=_safe_bool(
            dashboard_data.get("import_onboarded"),
            section.read("onboarded", _safe_bool),
        ),
        # Falls back to `onboarded`: a user who finished first run before
        # this chapter existed has already reached the product, and
        # re-gating their heartbeat on a screen they will never be shown
        # would suppress it forever.
        privacy_acked=_safe_bool(
            dashboard_data.get("privacy_acked"),
            section.read("onboarded", _safe_bool),
        ),
        crewmates_onboarded=section.read("crewmates_onboarded", _safe_bool),
        user_role=str(section.get("user_role")),
        user_role_other=str(section.get("user_role_other")),
        user_technical_level=str(section.get("user_technical_level")),
        tips_enabled=bool(section.get("tips_enabled")),
        feature_videos_enabled=section.read("feature_videos_enabled", _safe_bool),
        feature_videos_cache_max_mb=section.read(
            "feature_videos_cache_max_mb", _safe_float, lo=0.0
        ),
        folder_suggestions_enabled=bool(section.get("folder_suggestions_enabled")),
        topic_tags_enabled=section.read("topic_tags_enabled", _safe_bool),
        tips_cadence_hours=section.read("tips_cadence_hours", _safe_float, lo=0.0),
        tips_snooze_hours=section.read("tips_snooze_hours", _safe_float, lo=0.0),
        tips_recency_decay=section.read("tips_recency_decay", _safe_float, lo=0.0, hi=1.0),
        tips_model=str(section.get("tips_model")),
        tips_explore_ratio=section.read("tips_explore_ratio", _safe_float, lo=0.0, hi=1.0),
        github_hosts=github_hosts,
        gitlab_hosts=gitlab_hosts,
        jira_hosts=jira_hosts,
        bitbucket_server_hosts=bitbucket_server_hosts,
        gerrit_hosts=_sections._coerce_gerrit_hosts(
            section.get("gerrit_hosts"),
            claimed=(*gitlab_hosts, *jira_hosts, *github_hosts, *bitbucket_server_hosts),
        ),
        # These two parse raw JSON entries into DTOs, so they are handed only a
        # stored value; an omitted key is the field default as built.
        link_patterns=(
            _sections._coerce_link_patterns(dashboard_data["link_patterns"])
            if "link_patterns" in dashboard_data
            else section.default("link_patterns")
        ),
        jira_auth=(
            [
                _jira_auth_entry(entry)
                for entry in (dashboard_data["jira_auth"] or [])
                if isinstance(entry, dict) and entry.get("host")
            ]
            if "jira_auth" in dashboard_data
            else section.default("jira_auth")
        ),
    )


def _jira_auth_entry(raw: dict) -> JiraAuthEntry:
    """One ``dashboard.jira_auth`` row, read against :class:`JiraAuthEntry`'s defaults."""
    entry = _sections.SectionReader(JiraAuthEntry, raw)
    return JiraAuthEntry(
        host=str(entry.get("host")),
        # ``user`` is accepted as an alias; ``email`` wins when both are set.
        email=str(raw.get("email") or raw.get("user") or entry.default("email")),
    )


def _registry_entry(raw: dict) -> ExternalRegistryConfig:
    """One ``registries`` row, read against :class:`ExternalRegistryConfig`'s defaults."""
    entry = _sections.SectionReader(ExternalRegistryConfig, raw)
    return ExternalRegistryConfig(
        name=str(entry.get("name")),
        repo=str(entry.get("repo")),
        # Backward-compat: an entry that OMITS ``branch`` is a legacy config
        # written before URL registries defaulted new entries to ``main`` (the
        # registries PUT API now always persists an explicit branch). Such an
        # entry relied on the historical ``mainline`` default, so preserve it
        # here — silently retargeting it to ``main`` on upgrade would break any
        # registry whose content still lives on ``mainline``. The one registry
        # read whose default is deliberately NOT the field's.
        branch=str(raw.get("branch", "mainline")),
        # A credential-posture decision, so it is read back verbatim and
        # validated downstream rather than here: an unrecognised value must
        # resolve to the restrictive tier, which ``registry._registry_trust_tier``
        # does. Absent -> the field's "index", so a config written before the
        # field existed keeps the credential-free posture it had.
        trust=str(entry.get("trust")),
    )
