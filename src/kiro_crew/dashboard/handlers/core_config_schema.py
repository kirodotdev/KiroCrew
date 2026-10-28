"""The config paths the dashboard may edit, and the rule each value must meet.

``_EDITABLE_CONFIG`` maps every editable dotted path to its type, bounds, allowed
values or validator; ``_editable_config_spec`` answers the spec of one path, an
app's model pin included.

Served through :mod:`kiro_crew.dashboard.handlers.core`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import kiro_crew.config.session_environments as _session_environments
from kiro_crew.acp_backends import selectable_backend_values, selectable_stored_backend_values
from kiro_crew.apps.manifest import KEBAB_RE
from kiro_crew.computer_use.types import MAX_SCREENSHOT_MAX_PX as _CU_MAX_SCREENSHOT_MAX_PX
from kiro_crew.computer_use.types import MAX_TREE_NODES_LIMIT as _CU_MAX_TREE_NODES_LIMIT
from kiro_crew.computer_use.types import MIN_SCREENSHOT_MAX_PX as _CU_MIN_SCREENSHOT_MAX_PX
from kiro_crew.config.loader import (
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    CHAT_TURN_TIMEOUT_MAX,
    CHAT_TURN_TIMEOUT_MIN,
    COMPLETION_KEEP_CHARS_MIN,
    DEDUP_EVERY_N_SWEEPS_MAX,
    EMBED_RATE_LIMIT_MAX,
    EXTRACTION_POOL_SIZE_MAX,
    EXTRACTION_POOL_SIZE_MIN,
    FOLDER_INGEST_CHUNK_BUDGET_MAX,
    IMPORT_CHUNK_BUDGET_MAX,
    MCP_PROBE_TIMEOUT_MAX,
    MCP_PROBE_TIMEOUT_MIN,
    POOL_TTL_SECS_MAX,
    POOL_TTL_SECS_MIN,
    RECENT_TINT_COUNT_MAX,
    RECENT_TINT_COUNT_MIN,
    SESSION_TIMEOUT_MAX,
    SESSION_TIMEOUT_MIN,
    SOFT_STOP_BUDGET_MAX,
    SOFT_STOP_BUDGET_MIN,
    SWEEP_CHUNK_BUDGET_MAX,
    _clamp_compact_wait_secs,
)
from kiro_crew.config.sections import (
    COMPACT_WAIT_SECS_MAX,
    COMPACT_WAIT_SECS_MIN,
    DECISION_BUCKET_MAX,
    DECISION_BUCKET_MIN,
    DECISION_MODEL_ROUTE_TIERS,
    EXTERNAL_TERMINAL_APPS,
    FOLDER_SORT_MODES,
    JUDGE_PROVIDERS,
    SESSION_SORT_MODES,
    TITLE_REFRESH_EVERY_TURNS_MAX,
)
from kiro_crew.config.service_sections import ON_BATTERY_MODES
from kiro_crew.context_management import RESULT_FILE_MAX_BYTES
from kiro_crew.dashboard.handlers.core_config_models import _validate_role_model
from kiro_crew.effort import EFFORT_LEVELS
from kiro_crew.instances.crew_ref import MAX_CREW_REF_LEN
from kiro_crew.validation import QUALIFIED_MODEL_PATTERN


# Allowed editable config paths and their validators
def _agent_values() -> set[str]:
    """Return allowed pool_agent values: empty string + all configured agent names."""
    from kiro_crew.config.loader import KiroCrewConfig

    return {"", *KiroCrewConfig.load().agents}


def _selectable_acp_backends() -> list[str]:
    """The ``agent.acp_backend`` values this build can actually be switched to.

    A thin alias for ``acp_backends.selectable_backend_values`` so the allowlist
    entry below reads in this module's vocabulary; the answer itself comes from the
    one code owner, which the config load path and the schema endpoint also use —
    three independent derivations is how the old literal list drifted.

    Deployment POLICY needs no second derivation here: the ``agent_backend``
    governance scope is applied once at boot by narrowing the registry itself
    (``agent_backend_governance.narrow_selectable_backends``), so a policy-denied
    harness is already absent from the answer this returns.
    """
    return selectable_backend_values()


def _acp_backend_patch_values() -> list[str]:
    """What a PATCH may store in ``agent.acp_backend``: the config-file vocabulary.

    Every selectable backend in its stored spelling (Kiro CLI is ``"kiro"``),
    plus ``""``, which stores "no choice" so the install follows the default.
    """
    return [*selectable_stored_backend_values(), ""]


# The grammar of every model PIN a Settings picker writes and `_validate_role_model`
# then checks against the account's entitlement. The pickers list the advertised
# model names, which OpenCode spells `provider/model`, so a pin accepts a bare id
# OR a provider-qualified id (`validation.QUALIFIED_MODEL_PATTERN`, the branch the
# spawn, task-batch and cron model-name check shares); the empty value (INHERIT /
# disabled, per key) still matches. Malformed segments (`a//b`, `/a`, `a/`) and
# shell metacharacters are refused. One constant so a picker cannot offer an id a
# sibling pin refuses.
_MODEL_PIN_PATTERN = rf"(?:[A-Za-z0-9][A-Za-z0-9._\-\[\]]*|{QUALIFIED_MODEL_PATTERN})?\Z"


_EDITABLE_CONFIG: dict[str, dict] = {
    "agent.provider": {"type": "enum", "values": ["acp"]},
    # Which ACP agent drives a session, in the config-file vocabulary: "kiro" =
    # kiro-cli, "kas" = kiro-agent, "" = no choice (the default).
    # ``values_fn`` rather than a literal, because the set WIDENS after this module
    # is imported: an edition registers a backend from
    # ``ProviderRegistry.register_acp_backends`` at boot, and a literal would
    # reject it here with a misleading "invalid value". Resolved per request
    # against the one code owner, so this cannot drift from what ``AcpProvider``
    # will actually serve.
    "agent.acp_backend": {"type": "enum", "values_fn": _acp_backend_patch_values},
    # The KAS bundle route switch (Settings → Agent Harness → KAS). Boot-only on the
    # schema; the card says so after a save. No install is implied by turning it on:
    # a true value with nothing installed refuses the next KAS spawn by name.
    "agent.kas_bundle": {"type": "bool"},
    # Default model for new sessions. Membership can NOT be validated against a
    # fixed list: the real vocabulary is whatever the live kiro-cli advertises
    # (/api/models spawns it to find out), and it spans both canonical registry
    # keys ("opus-4.8-1m") and kiro's own ids ("claude-opus-4.8"). So this is a
    # grammar check instead — `_MODEL_PIN_PATTERN`, the grammar every model pin
    # shares, since the Settings → Chat picker lists the same advertised names
    # (provider-qualified on OpenCode) — and an unknown-but-well-formed id is
    # rejected downstream by kiro itself rather than silently accepted here.
    # "auto"/"" = defer to the agent config / kiro's own default.
    "agent.model": {"type": "str", "max_len": 64, "pattern": _MODEL_PIN_PATTERN},
    # Per-task-class model overrides. The model-pin grammar (the real
    # vocabulary is whatever the backend advertises, provider-qualified on
    # OpenCode). "" / "auto" defers to the chat default. `validate_fn`
    # additionally rejects a well-formed id the active provider or the
    # account's entitlement cannot honor.
    "agent.role_models.background": {
        "type": "str",
        "max_len": 64,
        "pattern": _MODEL_PIN_PATTERN,
        "validate_fn": _validate_role_model,
    },
    "agent.role_models.subagent": {
        "type": "str",
        "max_len": 64,
        "pattern": _MODEL_PIN_PATTERN,
        "validate_fn": _validate_role_model,
    },
    # Throttle-exhaustion fallback model. Single value: "auto" (default) defers
    # to the backend's availability-aware routing; a concrete id is tried first
    # with "auto" as the final fallthrough; "" disables the feature. Same
    # grammar + entitlement validation as the role-model pins ("" / "auto"
    # always allow), so the dropdown and the wire cannot disagree.
    "agent.fallback_model": {
        "type": "str",
        "max_len": 64,
        "pattern": _MODEL_PIN_PATTERN,
        "validate_fn": _validate_role_model,
    },
    # Content-filter (refusal) fallback model. Single value: "" (default)
    # disables the single-message retry; "auto" retries on the model the
    # provider's refusal envelope recommends; a concrete id retries on it.
    # Same grammar + entitlement validation as the role-model pins ("" /
    # "auto" always allow), so the dropdown and the wire cannot disagree.
    "agent.refusal_fallback_model": {
        "type": "str",
        "max_len": 64,
        "pattern": _MODEL_PIN_PATTERN,
        "validate_fn": _validate_role_model,
    },
    "agent.reasoning_effort": {"type": "enum", "values": ["", *EFFORT_LEVELS]},
    # Per-role reasoning effort, paired with role_models. Same enum as the chat
    # default; "" = inherit. Applies only on reasoning-capable models.
    "agent.role_efforts.background": {"type": "enum", "values": ["", *EFFORT_LEVELS]},
    "agent.role_efforts.subagent": {"type": "enum", "values": ["", *EFFORT_LEVELS]},
    "agent.approval_mode": {"type": "enum", "values": ["auto", "interactive"]},
    # How long an AD-HOC auto-approve grant lasts. Editable from Settings because
    # every value here still ends: the timed ones are capped at the SafetyOverride
    # 24h ceiling and "until_shutdown" dies with the process. The never-expiring
    # DECLARED grant (agent.dangerously_skip_permissions) is deliberately NOT
    # here — it stays config-file-only so it cannot be switched on from the UI.
    "agent.yolo_duration": {
        "type": "enum",
        "values": ["30m", "1h", "6h", "12h", "24h", "until_shutdown"],
    },
    # Kept equal to the ``agent.sandbox`` enum in ``config/sections_agent.py`` (pinned by
    # test_sandbox_strict_selectable): a tier the CLI admits must be selectable
    # here too, or Settings silently offers fewer tiers than ``config set``.
    "agent.sandbox": {"type": "enum", "values": ["auto", "strict", "off"]},
    "agent.sandbox_allow_no_isolation": {"type": "bool"},
    "agent.tool_search": {"type": "bool"},
    "agent.completion_keep": {"type": "enum", "values": ["head", "tail", "both"]},
    "agent.completion_keep_chars": {
        "type": "int",
        "min": COMPLETION_KEEP_CHARS_MIN,
        "max": RESULT_FILE_MAX_BYTES,
    },
    "agent.soft_stop_budget_secs": {
        "type": "float",
        "min": SOFT_STOP_BUDGET_MIN,
        "max": SOFT_STOP_BUDGET_MAX,
    },
    # Same bounds as the load-time clamp, so Settings cannot save a ceiling the
    # loader would then rewrite. Read on every turn, so the next turn follows it.
    "agent.chat_turn_timeout_secs": {
        "type": "int",
        "min": CHAT_TURN_TIMEOUT_MIN,
        "max": CHAT_TURN_TIMEOUT_MAX,
    },
    "session.timeout_secs": {"type": "int", "min": SESSION_TIMEOUT_MIN, "max": SESSION_TIMEOUT_MAX},
    # Range shared with the load-time clamp in config/loader_builders.py — one constant
    # pair, so the write gate and the load path cannot drift.
    "session.autocompact_pct": {
        "type": "float",
        "min": AUTOCOMPACT_PCT_MIN,
        "max": AUTOCOMPACT_PCT_MAX,
    },
    # ``0`` (the built-in budget) or 60-3600 s. Not one contiguous range, so the
    # write CLAMPS through the load path's own coercion instead of rejecting:
    # the stored value is exactly what a load of it would produce, and the
    # response's ``clamp_notice`` lets the Settings row say what was saved.
    # ``min``/``max`` are the positive band, reported in that notice.
    "session.compact_wait_secs": {
        "type": "float",
        "min": COMPACT_WAIT_SECS_MIN,
        "max": COMPACT_WAIT_SECS_MAX,
        "clamp_fn": lambda v: _clamp_compact_wait_secs(v, 0.0),
    },
    "session.pool_size": {"type": "int", "min": 0, "max": 10},
    "session.pool_agent": {"type": "str", "values_fn": _agent_values},
    "session.pool_ttl_secs": {"type": "int", "min": POOL_TTL_SECS_MIN, "max": POOL_TTL_SECS_MAX},
    # Intent-level session summaries in the chat right panel. Only the boolean
    # enable is editable here: it spends tokens on turns the user did not ask to
    # pay for, so it is off by default and the Settings toggle is the single
    # opt-in. The cadence/cap fields (min_user_turns, max_intents, …) stay
    # config-file-only — they are power-user knobs, not first-run choices.
    "session_summary.enabled": {"type": "bool"},
    # Which monitoring path a session arms by default. Safe on this generic
    # route for the reason ``computer_use.enabled`` is NOT: this key grants no
    # capability. Both monitoring paths are armable with it off, so flipping it
    # cannot open an unattended path -- it only changes which of the two the
    # monitor tool descriptions name as the default.
    "monitoring.prefer_structured_arming": {"type": "bool"},
    "auto_update": {"type": "bool"},
    "dashboard.mcp_probe_timeout_secs": {
        "type": "int",
        "min": MCP_PROBE_TIMEOUT_MIN,
        "max": MCP_PROBE_TIMEOUT_MAX,
    },
    "dashboard.recent_tint_count": {
        "type": "int",
        "min": RECENT_TINT_COUNT_MIN,
        "max": RECENT_TINT_COUNT_MAX,
    },
    # Auto-title refresh cadence (Settings → Chat → Sessions). The gate admits
    # the loader's whole domain, 0 (the built-in schedule) through MAX, and the
    # loader raises a stored 1-3 to MIN, the same as it does for a value
    # `kirocrew config set` wrote. The row reads back the LOADED value, so it
    # shows the cadence that runs. chat_title reads the key on every turn, so a
    # save applies from the next turn with no restart.
    "dashboard.title_refresh_every_turns": {
        "type": "int",
        "min": 0,
        "max": TITLE_REFRESH_EVERY_TURNS_MAX,
    },
    # The sidebar's folder sort mode. A view preference the sidebar menu writes and
    # the kirocrew-dashboard MCP server reads back, so the two draw the tree in
    # the same order; the enum is the loader's own list, spelled once.
    "dashboard.folder_sort": {"type": "enum", "values": list(FOLDER_SORT_MODES)},
    # The sidebar's session sort key, written by the sort menu; the enum is the
    # loader's own list, spelled once.
    "dashboard.session_sort": {"type": "enum", "values": list(SESSION_SORT_MODES)},
    # Per-version snooze/skip verdict for the proactive update popup, written
    # as ONE atomic record: the three fields only mean anything together, so
    # per-field writes would open both a crash window (old verdict paired
    # with a new version) and a two-client interleave that reassembles a
    # verdict nobody expressed. Persisted in gateway config (not browser
    # storage) so the decision holds across browsers and the desktop app's
    # embedded dashboard.
    "dashboard.update_nudge": {
        "type": "dict",
        "keys": {
            "version": {"type": "str", "max_len": 128},
            "snoozed_until": {"type": "float", "min": 0.0, "max": 4102444800.0},
            "skipped": {"type": "bool"},
        },
    },
    # Default shell for the built-in terminal panel (Settings → Display →
    # Terminal). "" = unset, use $SHELL / the platform default. The executable
    # check lives as an off-loop special case in the PATCH handler (a PATH
    # scan must not run inline on the event loop, and validate_fn is called
    # synchronously); the spawn path re-validates at open time and falls back
    # rather than failing, so a stale value can never cost the user their
    # terminal — the save-time check exists to surface a typo immediately in
    # the Settings field.
    "dashboard.terminal.shell": {"type": "str", "max_len": 512},
    # The Terminal tab's completion popup (Settings → Display → Terminal).
    # Default on; the completion route reads it per request (handlers/
    # terminal_session.py `_completion_disabled`), so a toggle takes effect on the
    # next keystroke with no restart. The whole-panel `terminal.enabled`
    # stays config-file-only: it also kills the PTY, which is not a display
    # preference.
    "dashboard.terminal.completion.enabled": {"type": "bool"},
    # Run-in-terminal focuses the selected terminal tab and copies the command
    # for a manual paste instead of minting a fresh PTY (Settings → Display →
    # Terminal). Off by default so the fresh-shell default is unchanged; read by
    # the dashboard's run-in-terminal handler, so a toggle takes effect on the
    # next click with no restart. Only a literal `true` turns it on — a
    # hand-edited non-boolean stays off.
    "dashboard.terminal.reuse_current": {"type": "bool"},
    # The terminal app "Open in external terminal" starts (Settings → Display →
    # Terminal). "" = unset, the default, which withholds the action. Limited
    # to the desktop shell's launch allowlist; the launch gate in
    # handlers/external_terminal.py re-reads it and treats anything else as
    # unset.
    "dashboard.terminal.external_app": {
        "type": "enum",
        "values": ["", *EXTERNAL_TERMINAL_APPS],
    },
    # Keep the host awake while the agent is running a task. Gateway-host
    # behavior (not a display pref), read by the prevent-sleep poll in
    # dashboard/server.py; off by default.
    "dashboard.prevent_sleep": {"type": "bool"},
    # Reply threads on crewmate chat messages. Read live by
    # ``dashboard/chat_threads.py`` (routes) and ``dashboard/ws.py`` (the
    # thread frame); off by default, and the Settings toggle under Crewmates is
    # the only dashboard door to it.
    "dashboard.crewmate_threads": {"type": "bool"},
    "dashboard.dynamic_dashboard_cards": {"type": "bool"},
    # User profile (onboarding step 2 + Settings > General > About You).
    # Structured slugs, not free text: context.py maps them to prompt-ready
    # descriptions in its [USER PROFILE] block. "" = unspecified/cleared.
    "dashboard.user_role": {
        "type": "enum",
        "values": ["", "developer", "designer", "product-manager", "data-ml", "it-ops", "other"],
    },
    # The one free-text escape hatch: what the user typed after picking "other".
    # Bounded hard (60 chars) and stripped of prompt-structural characters by
    # context.py before it is quoted into [USER PROFILE] — it is the only value
    # in that block the user authors rather than picks.
    "dashboard.user_role_other": {"type": "str", "max_len": 60},
    "dashboard.user_technical_level": {
        "type": "enum",
        "values": ["", "codes", "somewhat-technical", "non-technical"],
    },
    # Anonymous usage beacon — the in-product opt-out (Settings → Privacy
    # toggle), the GUI twin of `kirocrew telemetry disable`. Only the boolean
    # enable is editable here: beacon_endpoint stays CLI/config-file-only so a
    # dashboard caller cannot redirect the heartbeat to an arbitrary host.
    # Nothing about this key is sensitive to read back, so the masked GET
    # already surfaces it for the toggle's initial state.
    "telemetry.beacon_enabled": {"type": "bool"},
    # Tailnet-derived dashboard origin (RFC §4). Only the boolean enable is
    # editable: there is no companion key here for a hand-written tailnet name,
    # because the name is *derived from the local daemon and validated against the
    # tailnet's own MagicDNS suffix* — accepting one from an API caller would hand
    # the CSRF origin allowlist an attacker-chosen value, which is the whole thing
    # ``tailnet._valid_magicdns_name`` exists to prevent. Enabling takes effect on
    # the next gateway start (the origin set is built once during startup), and an
    # enterprise ceiling can refuse the enabling write outright — see the
    # ``capabilities.tailnet_origin`` gate in ``core_config_patch.py``.
    "dashboard.tailscale.enabled": {"type": "bool"},
    # Identity trust for tailnet peers (RFC §2–§3.1). Only the boolean opt-in
    # and the pin scope are editable here; ``allowed_logins`` is a list and is
    # deliberately config-file-only — the PATCH write surface has no list type,
    # and the allowlist is the control that decides who gets in, so it should
    # be an explicit file edit rather than an API-reachable value. Loader-side
    # validation keeps every bad combination narrowing-only (trust with an
    # empty allowlist stays off; an unrecognised pin_scope falls back to node).
    "dashboard.tailscale.trust_identity": {"type": "bool"},
    "dashboard.tailscale.pin_scope": {"type": "str", "max_len": 8},
    # Refresh-chain peer binding. Editable here because the only
    # direction a caller can move it is the one an operator may legitimately
    # need for roaming, and the loader resolves anything non-boolean back to the
    # bound default — so a malformed write cannot reopen the replay path.
    "dashboard.tailscale.bind_refresh_chains": {"type": "bool"},
    # Local OTEL metric collection — the Privacy panel's recording switch. Safe
    # to expose where beacon_endpoint is not: turning this on writes JSONL under
    # ~/.kiro/crew/metrics. It is NOT unconditionally local, though —
    # `_build_recorder` attaches an OTLP reader for every destination the active
    # telemetry provider supplies (the default provider supplies one when
    # `telemetry.otlp_endpoint` is set) — so the PATCH route's gate refuses the ENABLE on a
    # host where egress would start, which is what keeps the switch's local-only
    # promise true for every state it can reach. The endpoint itself stays
    # config-file-only, so a
    # dashboard caller can neither choose a destination nor start sending to one.
    "telemetry.enabled": {"type": "bool"},
    # SSO login flags for an edition that supplies a real sso_login_handler.
    # Bounded to a short string here; the companion login handler re-validates
    # each token against its own flag allowlist before spawning the login PTY
    # (defense in depth — this gate only stores the value). Inert in public build.
    "dashboard.sso_login_flags": {"type": "str", "max_len": 256},
    # Instances (multi-instance management). Toggling enabled needs a gateway
    # restart to take effect (the SSH manager + CSP relaxation init at startup),
    # so the Instances settings panel surfaces a "restart required" hint.
    "instances.enabled": {"type": "bool"},
    # The crew *Send a copy to* lists first (Settings → Remote Crew → Default
    # destination crew). It only orders the menu: nothing is sent until the
    # person clicks. Capped like a folder's default_destination_crew.
    "instances.default_destination": {"type": "str", "max_len": MAX_CREW_REF_LEN},
    # Skills: opt in to automatic skill generation (Settings → Skills). Both
    # default OFF/ON respectively in SkillsConfig; generated candidates still
    # require approval unless approval_required is turned off (scripts always
    # require approval regardless — enforced in the generation path).
    "skills.auto_create_from_sessions": {"type": "bool"},
    "skills.approval_required": {"type": "bool"},
    # Extra skill directories (Settings → Skills). A list of strings, bounded in count
    # and item length (every ``str_list`` entry declares both). The SkillsLoader owns
    # path expansion, sensitive-path exclusion and existence, so a path it declines is
    # skipped there rather than refused here.
    "skills.extra_paths": {"type": "str_list", "max_items": 64, "max_item_len": 4096},
    # Knowledge Library ingestion. Chunk budget max mirrors the point past which
    # a single sweep stops being a trickle; dedup cadence max is ~a day of sweeps.
    "knowledge.auto_add_documents": {"type": "bool"},
    "knowledge.auto_ingest_artifacts": {"type": "bool"},
    "knowledge.folder_ingest_chunk_budget": {
        "type": "int",
        "min": 0,
        "max": FOLDER_INGEST_CHUNK_BUDGET_MAX,
    },
    "knowledge.dedup_every_n_sweeps": {"type": "int", "min": 0, "max": DEDUP_EVERY_N_SWEEPS_MAX},
    "knowledge.sweep_chunk_budget": {"type": "int", "min": 0, "max": SWEEP_CHUNK_BUDGET_MAX},
    "knowledge.import_chunk_budget": {"type": "int", "min": 0, "max": IMPORT_CHUNK_BUDGET_MAX},
    "knowledge.embed_rate_limit": {"type": "int", "min": 0, "max": EMBED_RATE_LIMIT_MAX},
    # Background work on battery. Explicit adds never run through the sweeps it gates.
    "power.on_battery": {"type": "enum", "values": list(ON_BATTERY_MODES)},
    "knowledge.extraction_model": {"type": "str"},
    "knowledge.extraction_effort": {"type": "enum", "values": ["", *EFFORT_LEVELS]},
    "knowledge.extraction_pool_size": {
        "type": "int",
        "min": EXTRACTION_POOL_SIZE_MIN,
        "max": EXTRACTION_POOL_SIZE_MAX,
    },
    # Computer use — BUDGET KNOBS ONLY. There is deliberately no
    # "computer_use.enabled" key here: the primary enable lives on the keystone
    # ``computer_use.json`` (see config.loader.computer_use_state_path) so the
    # agent cannot reach it, and this generic PATCH route writes config.json.
    # Adding an enable key here would reintroduce exactly the hole the keystone
    # exists to close. The ComputerUsePanel drives these through
    # PUT /api/computer-use/config; they are also exposed here so the command
    # palette's generic config path can reach them. Bounds mirror
    # computer_use.types' *_LIMIT ceilings, which the loader re-clamps at load.
    "computer_use.max_tree_nodes": {
        "type": "int",
        "min": 1,
        "max": _CU_MAX_TREE_NODES_LIMIT,
    },
    "computer_use.screenshot_max_px": {
        "type": "int",
        "min": _CU_MIN_SCREENSHOT_MAX_PX,
        "max": _CU_MAX_SCREENSHOT_MAX_PX,
    },
    # Decision seam (src/kiro_crew/decisions/). The sampling rate is the one
    # value this route writes for it. The ENABLE is not a config path at all: it
    # is the keystone `decisions_consent.json`, written only by the browser-only
    # `PUT /api/decisions/consent` (handlers/decisions.py), because config.json
    # is agent-writable and consent to egress must not be. `provider.*` is
    # deliberately NOT here either. The endpoint would let a dashboard caller
    # choose where the state a decision point collects is sent, and `api_key` is
    # schema-`sensitive`, so the masked GET returns the sentinel for it — a PATCH
    # offered next to that would let a caller overwrite a key it cannot read
    # back. Both stay config-file-only, the same split telemetry.beacon_endpoint
    # already has.
    #
    # Bounds come from the config section itself, so this write gate and the
    # load-time clamp in `DecisionsConfig.from_raw` cannot drift.
    "decisions.bucket": {
        "type": "int",
        "min": DECISION_BUCKET_MIN,
        "max": DECISION_BUCKET_MAX,
    },
    # Session Environments holds credential-free profiles only, so the masked GET
    # may return it. ``normalize`` is the loader's own validator, so this gate
    # refuses what a load would drop; leaf paths update one scalar key alone.
    # Profiles have no leaf path: only the whole section writes them.
    "session_environments": {
        "type": "normalized",
        "normalize": _session_environments.validate_session_environments,
    },
    "session_environments.coder_url": {
        "type": "normalized",
        "normalize": _session_environments.validate_coder_url,
    },
    "session_environments.default_profile": {
        "type": "normalized",
        "normalize": _session_environments.validate_default_profile,
    },
    "session_environments.stop_on_shutdown": {
        "type": "normalized",
        "normalize": _session_environments.validate_stop_on_shutdown,
    },
}


# The tier-to-model map `model.route` routes a turn with. Registered from the tier
# tuple the point itself closes over, so this gate and the answer domain cannot
# drift: a tier added there becomes editable here, and a path naming a tier the
# question never offers stays a "field not editable" refusal.
#
# These are in the editable set where `provider.*` is not, and the config section
# states the difference: a tier value is an ordinary model id that "grants nothing
# on its own" -- the point validates it against what the provider advertises to
# this account and keeps the session's model when it is not there -- whereas the
# endpoint chooses WHERE collected state is sent and `api_key` is schema-sensitive,
# so the masked GET returns a sentinel for it. The same entitlement validation as
# the `agent.role_models.*` pins next to it, because the vocabulary is identically
# unknowable up front: `""` INHERITS (the turn keeps its session's model) and no
# concrete id is named here. The grammar is `_MODEL_PIN_PATTERN`, shared with the
# role-model and fallback pins.
for _tier in DECISION_MODEL_ROUTE_TIERS:
    _EDITABLE_CONFIG[f"decisions.model_route.{_tier}"] = {
        "type": "str",
        "max_len": 64,
        # OpenCode's picker advertises provider/model ids. Allow those while
        # retaining the empty INHERIT value and rejecting malformed segments.
        "pattern": _MODEL_PIN_PATTERN,
        "validate_fn": _validate_role_model,
    }


# The wake judge's two per-point settings. In the editable set where
# ``provider.*`` is not, and the difference is what each one decides: the endpoint
# chooses WHERE collected state is sent and ``api_key`` is schema-sensitive, while
# neither of these widens anything. Which judge answers is a choice between two
# destinations the machine is already authorized for -- Jev behind the keystone,
# or the model provider every turn already uses -- and the point itself is armed by
# its own consent scope, not by either of these keys.
#
# ``provider`` is a closed enum, so a typo is a refusal rather than a silently
# different judge. ``llm_model`` takes the same entitlement validation as the
# ``agent.role_models.*`` pins and the ``decisions.model_route`` tiers, because the
# vocabulary is identically unknowable up front: the id must be one the provider
# advertises to this account, and ``JUDGE_MODEL_DEFAULT`` INHERITS (the judge keeps
# the model its agent already resolves). Its grammar stays the bare id, NOT
# ``_MODEL_PIN_PATTERN``: the decision gate scrubs any model that fails
# ``decisions.types.is_model_id`` (no ``/``), so a saved ``provider/model`` pin
# would never reach the judge.
_EDITABLE_CONFIG["decisions.nudge_wake.provider"] = {
    "type": "enum",
    "values": list(JUDGE_PROVIDERS),
}


_EDITABLE_CONFIG["decisions.nudge_wake.llm_model"] = {
    "type": "str",
    "max_len": 64,
    "pattern": r"^[A-Za-z0-9._\-\[\]]*$",
    "validate_fn": _validate_role_model,
}


# ``apps.<name>.model``, the model each app's row in Settings → Apps writes. Apps
# install and uninstall while the gateway runs, so the path is matched by the
# manifest's own name grammar (``KEBAB_RE``) rather than listed. The value is the
# same kind of pin as the ``agent.role_models.*`` rows, with the same grammar and
# entitlement validation; ``""`` INHERITS. It grants nothing: the session layer
# withholds an id the account cannot run, as it does for every other pin.
_APP_MODEL_SPEC: dict = {
    "type": "str",
    "max_len": 64,
    "pattern": _MODEL_PIN_PATTERN,
    "validate_fn": _validate_role_model,
}


def _editable_config_spec(path_key: object) -> dict | None:
    """The PATCH spec for *path_key*: a listed field, or a per-app model path."""
    if not isinstance(path_key, str):
        return None
    spec = _EDITABLE_CONFIG.get(path_key)
    if spec is None:
        parts = path_key.split(".")
        # ``fullmatch``: ``KEBAB_RE``'s ``$`` also matches before a trailing newline.
        if (
            len(parts) == 3
            and parts[0] == "apps"
            and parts[2] == "model"
            and KEBAB_RE.fullmatch(parts[1])
        ):
            return _APP_MODEL_SPEC
    return spec
