"""Shared LLM interaction helpers — stream collection, JSON parsing, history saving.

Eliminates duplicate code across gateway, handler, dashboard, taskrunner,
subagent, and history modules.
"""

from __future__ import annotations

import importlib as _importlib
import logging
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# Each owner by name too, so a type checker loads it before this facade: modules
# in one import cycle with it read owner names at import time.
from kiro_crew.llm_helpers_parts import backend_errors as _backend_errors  # noqa: F401
from kiro_crew.llm_helpers_parts import background_turns as _background_turns  # noqa: F401
from kiro_crew.llm_helpers_parts import model_fallback as _model_fallback  # noqa: F401
from kiro_crew.llm_helpers_parts import tool_approval as _tool_approval  # noqa: F401
from kiro_crew.llm_helpers_parts import tool_input_denials as _tool_input_denials  # noqa: F401
from kiro_crew.llm_helpers_parts import turn_results as _turn_results  # noqa: F401
from kiro_crew.llm_helpers_parts import turn_stream as _turn_stream  # noqa: F401
from kiro_crew.llm_helpers_parts import turn_usage as _turn_usage  # noqa: F401

logger = logging.getLogger("kiro_crew.llm_helpers")


# This module is the facade of the shared LLM turn helpers: every name is reached through
# it as ``kiro_crew.llm_helpers.<name>``. A read is served by the owner that binds the
# name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every owner
# holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. New work
# goes to the owner of its responsibility in ``kiro_crew.llm_helpers_parts``:
#
#   ``backend_errors`` -- which backend failures a turn retries, and how long it waits
#   ``turn_usage`` -- what a turn cost, which backend served it, the interaction event
#   ``model_fallback`` -- the fallback chain and switching a live session's model
#   ``tool_input_denials`` -- the deny checks a tool call's title and input run through
#   ``tool_approval`` -- the approval policy and resolving a permission request
#   ``turn_results`` -- parsing JSON out of model text, saving a turn to history
#   ``turn_stream`` -- running one prompt to its answer (``stream_and_collect``)
#   ``background_turns`` -- unattended one-shot and background turns
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.llm_helpers_parts.backend_errors",
    "kiro_crew.llm_helpers_parts.turn_usage",
    "kiro_crew.llm_helpers_parts.model_fallback",
    "kiro_crew.llm_helpers_parts.tool_input_denials",
    "kiro_crew.llm_helpers_parts.tool_approval",
    "kiro_crew.llm_helpers_parts.turn_results",
    "kiro_crew.llm_helpers_parts.turn_stream",
    "kiro_crew.llm_helpers_parts.background_turns",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.llm_helpers_parts.backend_errors import (  # noqa: F401
        _CONNECTION_MARKERS,
        _FRAME_DATA_TAIL_RE,
        _JITTER_RNG,
        _PROMPT_BUSY_DELAY,
        _PROMPT_BUSY_RETRIES,
        _PROVIDER_ANSWER_MARKERS,
        _RE_CONTEXT_OVERFLOW_TEXT,
        _TRANSIENT_DELAY,
        _TRANSIENT_MARKERS,
        _TRANSIENT_RETRIES,
        CONNECTION_RECOVERY_WINDOW_SECS,
        CONNECTION_RETRY_MAX_DELAY,
        CREDENTIAL_RELOAD_RETRY_NOTICE,
        SESSION_NOT_FOUND_GIVE_UP_TEXT,
        SESSION_NOT_FOUND_NOT_REPLAYED_TEXT,
        SESSION_NOT_FOUND_RETRY_NOTICE,
        THROTTLE_RETRY_BASE_DELAY,
        TOOL_ACTIVITY_ATTR,
        TRANSIENT_RETRIES,
        PromptBusyExhaustedError,
        _is_transient_acp_error,
        acp_error_after_tool_activity,
        acp_error_is_connection_failure,
        acp_error_is_session_not_found,
        acp_error_is_throttle,
        acp_error_is_transient,
        acp_error_needs_fresh_credentials,
        connection_recovery_open,
        connection_retry_delay,
        credential_give_up_text,
        is_context_overflow_error,
        is_prompt_busy,
        is_transient_backend_error,
        throttle_retry_delay,
        transient_retry_delay,
    )
    from kiro_crew.llm_helpers_parts.background_turns import (  # noqa: F401
        _background_crew_log_owner,
        _cleanup_memory_consolidation_session,
        _lite_spec_unconfirmed,
        _raise_if_unfinished,
        _record_background_crew_log,
        _start_fresh_conversation,
        background_turn,
        run_bg_oneliner,
    )
    from kiro_crew.llm_helpers_parts.model_fallback import (  # noqa: F401
        _FALLBACK_STORY_CAP,
        FALLBACK_CANDIDATE_ATTEMPTS,
        FALLBACK_STORY_ATTR,
        ROUTE_BASELINE_ATTR,
        TURN_FALLBACK_ATTR,
        FallbackState,
        _bound_to_other_loop,
        _fallback_wire_spelling,
        _latch_identity,
        _slot_switch_session_locks,
        advance_fallback_candidate,
        annotate_model_fallback,
        append_fallback_story,
        configured_fallback_chain,
        fallback_rewound_transient_budget,
        fallback_story_of,
        first_advertised_fallback,
        latch_route_baseline,
        next_fallback_candidate,
        pick_epoch_host,
        probe_fallback_restore,
        provider_active_model,
        provider_advertised_ids,
        provider_backend,
        provider_fallback_active,
        provider_model_pin_partial,
        provider_model_pin_refused,
        provider_raw_model,
        resolve_substitute_set_model,
        route_baseline_latch,
        slot_switch_session_lock,
    )
    from kiro_crew.llm_helpers_parts.tool_approval import (  # noqa: F401
        ToolApprovalPolicy,
        _orphan_reject_done,
        _orphan_rejects,
        _resolve_permission,
        _steer_host_deny,
    )
    from kiro_crew.llm_helpers_parts.tool_input_denials import (  # noqa: F401
        _MAX_SCANNABLE_TOOL_INPUT_CHARS,
        _edit_target_denial,
        _extract_tool_input_strings,
        _first_tool_input_denial,
        _is_exempt_command_text,
        _path_tier_exempt,
        _title_denial,
    )
    from kiro_crew.llm_helpers_parts.turn_results import (  # noqa: F401
        _JSON_DECODER,
        _extract_json_of_type,
        _parse_llm,
        parse_llm_json,
        parse_llm_json_list,
        save_conversation_turn,
        save_conversation_turn_off_loop,
    )
    from kiro_crew.llm_helpers_parts.turn_stream import (  # noqa: F401
        _UNFINISHED_TURN_STOP_REASONS,
        stream_and_collect,
        stream_and_collect_json,
        unfinished_turn_error,
    )
    from kiro_crew.llm_helpers_parts.turn_usage import (  # noqa: F401
        _BILLING_STAT_HOLDERS,
        _NO_PRIOR_STATS,
        _TURN_BILLED_ATTR,
        _WRAPPER_WALK_MAX_NODES,
        _attempt_usage,
        _billing_stat_holders,
        _billing_stats,
        _provider_label,
        _sum_usage,
        provider_last_turn_usage,
        record_interaction_event,
        usage_has_billing,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew import (  # noqa: F401
        name_grant,
        permission_floor,
    )
    from kiro_crew.agent_sdk import (  # noqa: F401
        CONTEXT_EVENT_COMPACTION,
    )
    from kiro_crew.agent_sdk.drivers.acp import (  # noqa: F401
        provider_error_client,
        resolve_pin_spelling_on,
    )
    from kiro_crew.agent_sdk.drivers.acp_turn_vocab import (  # noqa: F401
        EVENT_COMPLETE,
        EVENT_PERMISSION_REQUEST,
        EVENT_STEER_CONSUMED,
        EVENT_TEXT_CHUNK,
        EVENT_TOOL_CALL,
        EVENT_TOOL_RESULT,
        AcpError,
        AcpPromptBusy,
        LLMEvent,
        LLMProvider,
        TurnUsage,
        advertised_model_ids,
        resolve_billing_stats,
    )
    from kiro_crew.agent_sdk.drivers.acp_vocab import (  # noqa: F401
        STOP_REASON_STALE_RECOVER,
        STOP_REASON_TIMEOUT,
        AcpTimeoutError,
    )
    from kiro_crew.config.loader import (  # noqa: F401
        KiroCrewConfig,
    )
    from kiro_crew.constants import (  # noqa: F401
        DENY_CAUSE_INVALID_NAME,
        DENY_CAUSE_POLICY,
        DENY_CAUSE_SURFACE_POLICY,
    )
    from kiro_crew.credential_errors import (  # noqa: F401
        is_credential_propagation_delay,
    )
    from kiro_crew.deny_notice import (  # noqa: F401
        steer_refusal_notice,
    )
    from kiro_crew.executors import (  # noqa: F401
        run_in_tool_gate_pool,
    )
    from kiro_crew.hooks import (  # noqa: F401
        _EDIT_TOOL_KIND,
        _normalize_tool_name,
        fire_tool_hooks,
        get_global_hook_store,
        hook_gate_kwargs,
        title_is_trusted_mcp_identity,
        untruncated_shell_title,
    )
    from kiro_crew.image_refs import (  # noqa: F401
        strip_image_refs,
    )
    from kiro_crew.messaging.link import (  # noqa: F401
        canonical_key,
    )
    from kiro_crew.permission_floor import (  # noqa: F401
        OUTCOME_REJECTED_TRANSPORT_FLOOR,
    )
    from kiro_crew.platform.context import redact_via_context  # noqa: F401
    from kiro_crew.platform.tool_paths import WRITE_PLANE_KINDS as _WRITE_PLANE_KINDS  # noqa: F401
    from kiro_crew.platform.tool_paths import (  # noqa: F401
        command_shaped_strings,
        edit_target_candidates,
        is_document_writing_tool,
        mcp_document_body_keys,
        split_document_bodies,
    )
    from kiro_crew.security import (  # noqa: F401
        MAX_SCANNABLE_COMMAND_CHARS,
        is_denied,
        is_denied_synthesized_target,
        is_sensitive_bash_command,
        is_unverifiable_path_refusal,
        redact_credentials,
        redact_exfiltration_urls,
        sensitive_path_refusal,
        sensitive_write_path_refusal,
    )
    from kiro_crew.security.readonly_bash import (  # noqa: F401
        command_invokes_git,
        is_read_only_bash,
    )
    from kiro_crew.sel import sel as _sel  # noqa: F401
    from kiro_crew.start_priority import (  # noqa: F401
        StartPriority,
    )
    from kiro_crew.tool_gate_busy import (  # noqa: F401
        hook_gate_busy,
    )
