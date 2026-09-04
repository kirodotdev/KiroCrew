"""KiroCrew computer use — read and drive desktop app windows via accessibility.

Public surface of the package. Importing it is **side-effect free**: no native
framework is loaded, no ``CDLL`` runs, no file is read, and no platform branch is
taken until :func:`get_shared_backend` is actually called. That is what lets the
Linux and Windows CI shards import the whole package and exercise its
platform-free logic without a driver.

Architecture in one paragraph: the ``kirocrew-computer`` MCP sidecar dispatches
tool calls into one synchronous chokepoint, which checks the keystone primary
enable, then governance, then the target policy, then the snapshot index's
freshness and fingerprint, and only then calls a
:class:`~kiro_crew.computer_use.backend.ComputerUseBackend`. The accessibility
tree is the primary channel; a screenshot is compressed, persisted to an
owner-only temp dir, and relayed only as a path.

See ``docs/system-specs/modules/computer-use.md``.
"""

from __future__ import annotations

from kiro_crew.computer_use.backend import (
    ComputerUseBackend,
    UnsupportedBackend,
    get_shared_backend,
    platform_id_for_current_os,
    register_computer_use_backend,
    reset_shared_backend,
    select_default_backend,
)
from kiro_crew.computer_use.index import SnapshotIndex, get_shared_index, reset_shared_index
from kiro_crew.computer_use.types import (
    AppRef,
    BackendStatus,
    ComputerUseDenied,
    ComputerUseError,
    ComputerUseUnsupported,
    DriverResult,
    ElementRec,
    KeyParseError,
    PermissionProbe,
    PolicyConfig,
    Snapshot,
    SnapshotRequest,
    StaleIndex,
)

__all__ = [
    "AppRef",
    "BackendStatus",
    "ComputerUseBackend",
    "ComputerUseDenied",
    "ComputerUseError",
    "ComputerUseUnsupported",
    "DriverResult",
    "ElementRec",
    "KeyParseError",
    "PermissionProbe",
    "PolicyConfig",
    "Snapshot",
    "SnapshotIndex",
    "SnapshotRequest",
    "StaleIndex",
    "UnsupportedBackend",
    "get_shared_backend",
    "get_shared_index",
    "platform_id_for_current_os",
    "register_computer_use_backend",
    "reset_shared_backend",
    "reset_shared_index",
    "select_default_backend",
]
