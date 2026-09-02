"""Focused coordinators behind the stable SubagentManager facade."""

from ._component import bind_component_globals, copy_component_docs
from .admission import SpawnAdmissionCoordinator
from .cancellation import CancellationCoordinator
from .continuation import ContinuationCoordinator
from .monitoring import OrphanStallMonitor
from .run import RunEventCoordinator
from .terminal import TerminalCoordinator
from .waves import WaveDigestCoordinator

__all__ = [
    "bind_component_globals",
    "copy_component_docs",
    "OrphanStallMonitor",
    "TerminalCoordinator",
    "SpawnAdmissionCoordinator",
    "ContinuationCoordinator",
    "WaveDigestCoordinator",
    "RunEventCoordinator",
    "CancellationCoordinator",
]
