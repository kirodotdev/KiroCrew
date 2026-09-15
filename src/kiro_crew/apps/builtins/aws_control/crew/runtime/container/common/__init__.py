"""Shared base for the three container processes.

Owned centrally, not by any one track. A track that needs something changed here
says so rather than editing it, because all three import it and a local fix
becomes the next disagreement.
"""

from .config import (
    BACKEND_HOST,
    CONTROL_SECRET_HEADER,
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
