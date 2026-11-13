"""What a recorder build produced, and the config key its resource was read from.

Split out of :mod:`kiro_crew.metrics.provider` (that module's line budget), which
re-exports both names and stays the import path callers and tests use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Optional

from kiro_crew.metrics.recorder import MetricsRecorder

if TYPE_CHECKING:  # real types for annotations; never imported at runtime
    from opentelemetry.sdk.metrics import MeterProvider as _MeterProviderT


class _Build(NamedTuple):
    """What a build produced, for a caller to install under ``_lock``.

    Returned rather than assigned so the expensive part can run OFF the lock and
    off the event loop: only the install is a critical section.
    """

    recorder: MetricsRecorder
    provider: Optional["_MeterProviderT"]
    consent: Optional[bool]
    agent_key: object = None
    endpoints_key: object = None
    # ``outbound_consented`` at build time, or None when there was no destination.
    outbound: Optional[bool] = None


def _agent_key(cfg: object) -> object:
    """The config values the ``kirocrew.harness`` resource is built from; never raises.

    The raw ``agent.acp_backend`` / ``agent.provider`` pair of a loaded config,
    compared as-is, so a recheck can tell the resource is stale without mapping
    it. ``None`` when the agent section cannot be read.
    """
    try:
        agent = getattr(cfg, "agent", None)
        if agent is None:
            return None
        return (getattr(agent, "acp_backend", None), getattr(agent, "provider", None))
    except Exception:
        return None
