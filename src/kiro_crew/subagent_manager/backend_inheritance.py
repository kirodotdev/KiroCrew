"""Which backend a sub-agent takes from its parent chat's backend pick.

A run that names no backend (``spawn_run`` without ``backend=``) starts on its
parent chat's pick. The pick it took is recorded apart from the run's own
``backend`` (which would force a dedicated process) as ``inherited_backend``,
in memory and in the run's saved state, so a ``spawn_continue`` resumes where
the conversation lives even after the parent switches.
"""

from __future__ import annotations

from typing import Any


def inherit_parent_backend(sessions: Any, info: Any, extra_kwargs: dict[str, Any]) -> None:
    """Start *info* on its parent chat's backend pick, when the parent has one.

    Called only for a run with no backend of its own that is not a crewmate's
    worker (which keeps the backend its kit needs) and not a continuation: a
    continuation resumes where its first run started, which the continuation
    entry already resolved into ``info.backend``. A shared child runs in its
    parent's process, so on the parent's pick; a dedicated one takes the same
    pick, so the two arms agree.
    """
    inherited = sessions.allocation_backend_override(info.parent_session_key)
    if isinstance(inherited, str):
        extra_kwargs["backend_override"] = inherited
        info.inherited_backend = inherited


def record_served_backend(info: Any, client: Any) -> None:
    """Record the backend *client* actually serves *info* on.

    An inherited pick the factory's gate swapped for its fallback (a pick the
    backend policy denies) did not serve the run, and a continuation must
    resume where the conversation lives. Kiro's id is ``""``, which is the
    gate's only fallback, so only an unknown backend leaves the record alone.
    """
    if info.inherited_backend is None:
        return
    from kiro_crew.agent_sdk.capabilities import UNKNOWN_BACKEND_CAPABILITIES, capabilities_of

    served = capabilities_of(client)
    if served is not UNKNOWN_BACKEND_CAPABILITIES:
        info.inherited_backend = served.backend


def inherited_backend_state(info: Any, client: Any) -> dict[str, Any]:
    """The state fields that let a continuation after a restart resume there.

    Read once *client* serves the run, so the record first takes the backend
    it actually runs on (:func:`record_served_backend`).
    """
    record_served_backend(info, client)
    if info.inherited_backend is None:
        return {}
    return {"inherited_backend": info.inherited_backend}
