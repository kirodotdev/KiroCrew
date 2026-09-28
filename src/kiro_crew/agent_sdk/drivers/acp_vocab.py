"""The ACP driver's vocabulary: names application code reads by VALUE.

Stop classes and the stop-reason classifier, the structured-status frame and
its wait reasons, the session-start timeout and the native-child marker are
backend vocabulary a consumer compares against or raises on. They reach
application code from here, so a consumer never names ``kiro_crew.acp`` itself
(``scripts/check_agent_sdk_boundary.py``).

Unlike :mod:`kiro_crew.agent_sdk.drivers.acp`, the imports here are at MODULE
scope on purpose: a constant has to exist when the importing module binds it.
The package ``__init__`` does not import this module, so the boot path stays
ACP-free (``test_the_boot_path_does_not_import_acp_at_module_scope``); every
consumer listed below already loaded the ACP package at module scope through
the import this module replaces.
"""

from __future__ import annotations

from kiro_crew.acp.client import AcpProcessDied
from kiro_crew.acp.session_handle import NATIVE_CHILD_NOT_RESUMABLE, AcpRequestTimeout
from kiro_crew.acp.types import (
    EVENT_STRUCTURED_STATUS,
    STATUS_EXTENSION_VERSION,
    STATUS_PHASE_WAITING,
    STOP_CLASS_CANCELLED,
    STOP_CLASS_FAILED,
    STOP_CLASS_RECOVERING,
    STOP_CLASS_STALLED,
    STOP_CLASS_SUCCEEDED,
    STOP_RECOVERY_MAX_RETRIES,
    WAIT_REASON_INPUT,
    StructuredStatus,
    classify_stop_reason,
)

__all__ = [
    "is_runtime_death",
    "EVENT_STRUCTURED_STATUS",
    "NATIVE_CHILD_NOT_RESUMABLE",
    "STATUS_EXTENSION_VERSION",
    "STATUS_PHASE_WAITING",
    "STOP_CLASS_CANCELLED",
    "STOP_CLASS_FAILED",
    "STOP_CLASS_RECOVERING",
    "STOP_CLASS_STALLED",
    "STOP_CLASS_SUCCEEDED",
    "STOP_RECOVERY_MAX_RETRIES",
    "WAIT_REASON_INPUT",
    "AcpRequestTimeout",
    "StructuredStatus",
    "classify_stop_reason",
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
