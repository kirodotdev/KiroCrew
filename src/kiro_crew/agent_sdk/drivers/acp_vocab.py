"""The ACP driver's vocabulary: names application code reads by VALUE.

Stop classes and the stop-reason classifier, the tool-call, tool-call-update and
tool-result event kinds, the stop reasons of a turn the backend did not finish, the
structured-status frame and its wait reasons, the terminal tool-call statuses, the session-start and prompt timeouts, the
native-child marker, the notice a harness reports for the person, the mark a dashboard
turn's stream draws background work under and the background cards its harness still
runs (and loses) are backend
vocabulary a consumer compares against or raises on, and so is the continuation that hands a timed-out call back to the
model. The in-flight tool-call tracker is here too: a sub-agent's run
record builds one as a dataclass field default, so the class has to be bound
when that module is imported. They reach application code from here, so a
consumer never names ``kiro_crew.acp`` itself
(``scripts/check_agent_sdk_boundary.py``).

Unlike :mod:`kiro_crew.agent_sdk.drivers.acp`, the imports here are at MODULE
scope on purpose: a constant has to exist when the importing module binds it.
The package ``__init__`` does not import this module, so the boot path stays
ACP-free (``test_the_boot_path_does_not_import_acp_at_module_scope``); every
consumer listed below already loaded the ACP package at module scope through
the import this module replaces.
"""

from __future__ import annotations

from kiro_crew.acp.client import AcpError, AcpProcessDied, AcpSandboxInitFailed, AcpTimeoutError
from kiro_crew.acp.engine_fallback import (
    EngineFallbackWalk,
    ModelFallbackNotice,
    engine_served_model,
    follow_engine_promotion,
    is_unpinned_pick,
    same_model,
    tracker_of,
    walk_outcome,
)
from kiro_crew.acp.harness_notices import (
    NOTICE_KIND_ADAPTER,
    NOTICE_KIND_MODE,
    SEVERITY_WARNING,
    HarnessNotice,
    title_ends_sentence,
)
from kiro_crew.acp.liveness import InFlightToolTracker
from kiro_crew.acp.raw_sdk_tasks import (
    END_COMPLETED,
    END_FAILED,
    END_STOPPED,
    NOTICE_KIND_API_RETRY,
    NOTICE_KIND_TASK_ENDED,
    expect_dashboard_turn,
)
from kiro_crew.acp.raw_sdk_tasks import live_card_ids as live_background_card_ids
from kiro_crew.acp.raw_sdk_tasks import on_cards_lost as on_background_cards_lost
from kiro_crew.acp.raw_sdk_workflow_cards import BackgroundCardRow
from kiro_crew.acp.session_handle import (
    NATIVE_CHILD_NOT_RESUMABLE,
    AcpRequestTimeout,
    AcpScopeAdmissionTimeout,
)
from kiro_crew.acp.timed_out_continuation import continue_timed_out_calls, timed_out_continuation
from kiro_crew.acp.types import (
    EVENT_HARNESS_NOTICE,
    EVENT_MODEL_FALLBACK,
    EVENT_STRUCTURED_STATUS,
    PROVIDER_LABEL_DEFAULT,
    PROVIDER_LABEL_KAS,
    STATUS_EXTENSION_VERSION,
    STATUS_PHASE_WAITING,
    STOP_CLASS_CANCELLED,
    STOP_CLASS_FAILED,
    STOP_CLASS_RECOVERING,
    STOP_CLASS_STALLED,
    STOP_CLASS_SUCCEEDED,
    STOP_REASON_CANCEL_UNACKED,
    STOP_REASON_CANCELLED,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TIMEOUT,
    STOP_RECOVERY_MAX_RETRIES,
    TERMINAL_TOOL_STATUSES,
    WAIT_REASON_INPUT,
    StructuredStatus,
    classify_stop_reason,
)
from kiro_crew.providers.base import EVENT_TOOL_CALL, EVENT_TOOL_CALL_UPDATE, EVENT_TOOL_RESULT

__all__ = [
    "is_runtime_death",
    "is_sandbox_init_failure",
    "is_user_worded_failure",
    "EVENT_MODEL_FALLBACK",
    "EVENT_STRUCTURED_STATUS",
    "EVENT_TOOL_CALL",
    "EVENT_TOOL_CALL_UPDATE",
    "EVENT_TOOL_RESULT",
    "NATIVE_CHILD_NOT_RESUMABLE",
    "PROVIDER_LABEL_DEFAULT",
    "PROVIDER_LABEL_KAS",
    "STATUS_EXTENSION_VERSION",
    "STATUS_PHASE_WAITING",
    "STOP_CLASS_CANCELLED",
    "STOP_CLASS_FAILED",
    "STOP_CLASS_RECOVERING",
    "STOP_CLASS_STALLED",
    "STOP_CLASS_SUCCEEDED",
    "STOP_REASON_CANCEL_UNACKED",
    "STOP_REASON_CANCELLED",
    "STOP_REASON_STALE_RECOVER",
    "STOP_REASON_TIMEOUT",
    "STOP_RECOVERY_MAX_RETRIES",
    "TERMINAL_TOOL_STATUSES",
    "WAIT_REASON_INPUT",
    "AcpRequestTimeout",
    "AcpScopeAdmissionTimeout",
    "AcpTimeoutError",
    "InFlightToolTracker",
    "StructuredStatus",
    "classify_stop_reason",
    # The engine's own in-prompt model fallback (``acp/engine_fallback.py``): the
    # per-turn walk a surface ends with one line, the verdict it reads, and the
    # readers a surface that shows the model uses to follow the engine.
    "EngineFallbackWalk",
    "ModelFallbackNotice",
    "engine_served_model",
    "follow_engine_promotion",
    "is_unpinned_pick",
    "same_model",
    "tracker_of",
    "walk_outcome",
    # A notice a harness reported for the person (``acp/harness_notices.py``):
    # the event, its payload, the payload's kinds and warning severity, and the
    # rule that joins a title to its description in every form of the notice.
    "EVENT_HARNESS_NOTICE",
    "HarnessNotice",
    "NOTICE_KIND_ADAPTER",
    "NOTICE_KIND_MODE",
    "SEVERITY_WARNING",
    "title_ends_sentence",
    # Claude Code's background work (``acp/raw_sdk_tasks.py``): the kinds of the two
    # notices it makes, how a task ended, the row type of a Workflow agent's card, the
    # mark that has a dashboard turn's stream draw them, the cards the harness still
    # runs, and the watch that says when the client lost them.
    "BackgroundCardRow",
    "END_COMPLETED",
    "END_FAILED",
    "END_STOPPED",
    "NOTICE_KIND_API_RETRY",
    "NOTICE_KIND_TASK_ENDED",
    "expect_dashboard_turn",
    "live_background_card_ids",
    "on_background_cards_lost",
    # A call whose own silence timer ended the turn, handed back to the model on
    # the same session (``acp/timed_out_continuation.py``) by a surface with no
    # tool-stall recovery queue of its own.
    "continue_timed_out_calls",
    "timed_out_continuation",
]


def is_runtime_death(exc: BaseException) -> bool:
    """Whether *exc* is the runtime dying under a session's in-flight prompt.

    ``AcpSessionHandle._died`` raises ``AcpProcessDied`` into the prompt that
    was streaming when the runtime was killed, and the session provider maps
    the runtime's own ``AcpRuntimeDead`` onto the same class. The sub-agent
    run loop asks this of an exception it caught while a reap it started was
    in flight -- "is this the echo of my own teardown?" -- and the answer is
    a class test that application code must not spell itself.
    """
    return isinstance(exc, AcpProcessDied)


def is_sandbox_init_failure(exc: BaseException) -> bool:
    """Whether *exc* is an OS sandbox refusing to start the agent child.

    The transport raises ``AcpSandboxInitFailed`` when a backend exists but the
    host's sandbox will not initialize, so no chat can start until someone fixes
    the host. The dashboard's host-sandbox notice walks a turn's cause chain with
    this test, which application code must not spell itself.
    """
    return isinstance(exc, AcpSandboxInitFailed)


def is_user_worded_failure(exc: BaseException) -> bool:
    """Whether *exc* is a backend failure whose message Kiro Crew wrote for a person.

    True for an ``AcpError`` with ``user_worded`` set (a curated formatter
    branch, or a raise site with fixed recovery wording such as an unavailable
    model or a signed-out harness) whose ``transient`` verdict is not True. An
    ``AcpPromptBusy`` follows the same bit: its text is the formatter's busy
    wording only when that curated branch matched, and the fallback's provider
    text otherwise. A retryable failure keeps a chat surface's generic "try
    again" reply.

    ``transient`` alone is not the test: it is a retry verdict set on every
    error frame, including the formatter's fallback that passes the provider's
    own text or the raw error dict through. Timeouts and process deaths are
    excluded by type, because their messages are internal detail whatever they
    carry. The one exception is ``AcpScopeAdmissionTimeout``, an
    ``AcpRuntimeError`` whose text is always the fixed diagnosis from
    ``scope_admission.timeout_message``, so it is True. A caller that shows the
    text still redacts, caps and escapes it for its own platform.
    """
    if isinstance(exc, AcpScopeAdmissionTimeout):
        return True
    if not isinstance(exc, AcpError) or isinstance(exc, (AcpTimeoutError, AcpProcessDied)):
        return False
    return exc.user_worded and exc.transient is not True
