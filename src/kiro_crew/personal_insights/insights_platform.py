from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Final

PLATFORM_LINUX: Final[str] = "linux"
PLATFORM_DARWIN: Final[str] = "darwin"
PLATFORM_WINDOWS: Final[str] = "windows"

SERVER_PLATFORMS: Final[frozenset[str]] = frozenset(
    {PLATFORM_LINUX, PLATFORM_DARWIN, PLATFORM_WINDOWS}
)

_CLIENT_FORBIDDEN: Final[tuple[str, ...]] = (
    "platform",
    "execution_platform",
    "account",
    "account_id",
    "workspace_id",
    "project",
)


class PlatformError(Exception):
    pass


@dataclass(frozen=True)
class RuntimePlatform:
    platform: str

    def __post_init__(self) -> None:
        if self.platform not in SERVER_PLATFORMS:
            raise PlatformError(f"unknown server platform: {self.platform}")


def resolve_runtime_platform(server_platform: str | None) -> RuntimePlatform:
    if server_platform is None:
        raise PlatformError("server execution platform must be non-null")
    return RuntimePlatform(platform=server_platform)


def _map_sys_platform(sys_platform: str) -> str:
    if sys_platform.startswith("linux"):
        return PLATFORM_LINUX
    if sys_platform == "darwin":
        return PLATFORM_DARWIN
    if sys_platform.startswith("win"):
        return PLATFORM_WINDOWS
    raise PlatformError(f"unsupported host platform: {sys_platform}")


def current_runtime_platform(_sys_platform: str | None = None) -> RuntimePlatform:
    source = sys.platform if _sys_platform is None else _sys_platform
    return RuntimePlatform(platform=_map_sys_platform(source))


def reject_client_platform(request_fields: dict[str, object]) -> None:
    for forbidden in _CLIENT_FORBIDDEN:
        if forbidden in request_fields:
            raise PlatformError(f"client may not supply {forbidden}")


def compiled_candidate_cache_identity(runtime: RuntimePlatform, base_identity: str) -> str:
    return f"{runtime.platform}\x1f{base_identity}"


def parse_cache_identity(cache_identity: str) -> tuple[str, str]:
    parts = cache_identity.split("\x1f")
    if len(parts) != 2:
        raise PlatformError("cache identity must be exactly platform and identity")
    platform, identity = parts
    if platform not in SERVER_PLATFORMS:
        raise PlatformError(f"unknown platform in cache identity: {platform}")
    if identity == "":
        raise PlatformError("cache identity must carry a non-empty identity")
    return platform, identity
