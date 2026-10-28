"""The seam a harness-native workflow engine's frames leave the runtime through.

A harness that brings its own workflow engine (KAS, over ``_kiro/workflow/*``)
sends two kinds of frame the session demux cannot place:

- Run lifecycle NOTIFICATIONS. They name a run and its parent session, not a
  ``sessionId`` the demux routes by, so without a sink they are broadcast to
  every registered session (or, for ``node_start``, which names the STEP's
  session, dropped).
- Permission REQUESTS on a step's session. KAS opens that session itself, so it
  is registered nowhere and the demux would auto-reject the request.

A runtime with a sink installed offers both to it first. Every runtime on a
backend with a native engine gets one at construction (:func:`sink_for`), bound
to the gateway's router (:mod:`kiro_crew.kas_workflows.router`); a runtime on any
other backend has none and behaves exactly as before.

Contract, which the reader loop relies on:

- Every method is synchronous and returns promptly: it runs ON the reader loop,
  the single stdout demux for every multiplexed session. A sink that must wait
  (on a person, on I/O) schedules its own task.
- ``watches_notification`` returning True does not take the frame: the sink
  sees it, then the demux routes it to its session as it would without a sink.
- ``claims_step_request`` returning True transfers the protocol obligation to
  answer that request to the sink. A claimed request is never also answered by
  the runtime, so a sink that claims and then forgets strands the step.
- Exceptions are the sink's bug, not the runtime's: the reader logs and drops
  a notification whose handler raised, and auto-rejects a request whose handler
  raised, so one bad frame cannot cost the demux.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class WorkflowFrameSink(Protocol):
    """Receives the workflow frames a runtime cannot route to a session."""

    def claims_notification(self, method: str) -> bool:
        """Whether a session-less or step-session notification is the sink's."""

    def on_notification(self, method: str, params: dict[str, Any]) -> None:
        """Take one claimed notification. Must not block."""

    def watches_notification(self, method: str) -> bool:
        """Whether a session-routed notification is also shown to the sink.

        Unlike a claim, a watch does not take the frame: the runtime offers it
        to :meth:`on_watched_notification` and then routes it to its session as
        usual.
        """

    def on_watched_notification(self, method: str, params: dict[str, Any]) -> None:
        """See one watched notification, which still goes to its session. Must not block."""

    def claims_step_request(self, session_id: str, method: str) -> bool:
        """Whether the sink will answer *method* for unregistered *session_id*."""

    def on_step_request(
        self, request_id: int | str, session_id: str, method: str, params: dict[str, Any]
    ) -> None:
        """Take one claimed request; the sink now owes it exactly one answer."""


def sink_for(runtime: Any, backend: str) -> WorkflowFrameSink | None:
    """The sink a new runtime for *backend* installs, or None.

    Every runtime whose harness brings its own workflow engine gets one, so a run
    parented by any session on it -- a chat the dashboard opened for the run, or
    a chat whose model started it -- routes the same way. The router is imported
    lazily: it is gateway state, and a runtime on a backend without the engine
    never touches it. A router that cannot be built leaves the runtime on its
    pre-sink behaviour rather than failing the spawn.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_NATIVE_WORKFLOWS

    if backend not in ACP_BACKENDS_NATIVE_WORKFLOWS:
        return None
    try:
        from kiro_crew.kas_workflows.router import get_router

        return get_router().sink_for(runtime)
    except Exception:  # noqa: BLE001 -- a spawn must not fail on the router
        logger.warning("KAS workflow router unavailable; runtime has no sink", exc_info=True)
        return None
