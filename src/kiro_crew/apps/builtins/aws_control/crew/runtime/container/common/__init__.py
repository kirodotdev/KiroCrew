"""Shared base for the three container processes.

Owned centrally, not by any one track. A track that needs something changed here
says so rather than editing it, because all three import it and a local fix
becomes the next disagreement.
"""

from .config import (
    BACKEND_DRAIN_SECS,
    BACKEND_HOST,
    BACKUP_MAX_ATTEMPTS,
    BACKUP_REQUEST_TIMEOUT_SECS,
    CONTROL_SECRET_HEADER,
    FRONT_DRAIN_SECS,
    MAX_OBJECT_BYTES,
    MAX_TASK_STOP_TIMEOUT_SECS,
    SIDECAR_DRAIN_SECS,
    TASK_STOP_TIMEOUT_SECS,
    ConfigError,
    Settings,
    load,
    parse_route_prefix,
)
from .secret import (
    HEADER,
    BackendSecretUnavailable,
    auth_header,
    read_boot_secret,
    secret_path,
)

__all__ = [
    "BACKEND_HOST",
    "CONTROL_SECRET_HEADER",
    "MAX_OBJECT_BYTES",
    "BACKUP_MAX_ATTEMPTS",
    "BACKUP_REQUEST_TIMEOUT_SECS",
    "FRONT_DRAIN_SECS",
    "BACKEND_DRAIN_SECS",
    "SIDECAR_DRAIN_SECS",
    "TASK_STOP_TIMEOUT_SECS",
    "MAX_TASK_STOP_TIMEOUT_SECS",
    "ConfigError",
    "Settings",
    "load",
    "parse_route_prefix",
    "HEADER",
    "BackendSecretUnavailable",
    "auth_header",
    "read_boot_secret",
    "secret_path",
]
