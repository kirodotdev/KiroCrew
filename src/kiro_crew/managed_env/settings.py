"""The managed-environment lane's switch and settings.

The lane is on only while the ``crew_client`` feature preview is on
(:func:`kiro_crew.crew_client_preview.preview_on`, read through
``edition_locks.feature_previews``, so an LTS build reads it as off whatever
``config.json`` stores). Off, the lane offers no launch row, every route answers
404 and nothing here is read.

Every value the lane sends or writes comes from the ``instances.managed_env_*``
keys, each empty by default. Nothing is guessed: a step that needs a missing value
raises :class:`SettingsMissing`, whose text names the keys to set.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

#: The config section every key below lives in.
SECTION = "instances"

KEY_ENDPOINT = "managed_env_endpoint"
KEY_START_URL = "managed_env_start_url"
KEY_PROFILE_ARN = "managed_env_profile_arn"
KEY_SSH_REGION = "managed_env_ssh_region"
KEY_KAS_ENDPOINT = "managed_env_kas_endpoint"
KEY_KAS_CONTROL_PLANE = "managed_env_kas_control_plane_endpoint"
KEY_KAS_REGION = "managed_env_kas_region"
KEY_BOX_TOKEN_PATH = "managed_env_box_token_path"

#: An AWS Region name's shape (``<area>-<place>-<n>``, with an optional partition word).
_REGION_RE = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]{1,2}")
_ARN_RE = re.compile(r"arn:[a-z-]+:[a-z0-9-]+:[a-z0-9-]*:[0-9]*:\S+")


class SettingsMissing(LookupError):
    """A lane step needs values the operator has not set. ``keys`` are their full paths."""

    def __init__(self, keys: Iterable[str]):
        self.keys = tuple(f"{SECTION}.{k}" for k in keys)
        super().__init__(
            "The managed-environment lane is not set up: set "
            + ", ".join(self.keys)
            + " in config.json (see docs/configuration.md, Remote Crew managed environments)."
        )


@dataclass(frozen=True)
class MachineCell:
    """The three engine values machine mode writes on a new managed environment."""

    endpoint: str
    control_plane_endpoint: str
    region: str


@dataclass(frozen=True)
class LaneSettings:
    """The ``instances.managed_env_*`` values, as read. Empty means unset."""

    endpoint: str = ""
    start_url: str = ""
    profile_arn: str = ""
    scopes: tuple[str, ...] = ()
    ssh_region: str = ""
    kas_endpoint: str = ""
    kas_control_plane_endpoint: str = ""
    kas_region: str = ""
    mcp_registry_mode: bool = False
    box_token_path: str = ""
    box_token_env: str = ""
    box_env_drop: tuple[str, ...] = field(default=())
    box_unset_env: tuple[str, ...] = field(default=())
    box_openssl: str = ""

    def require_service(self) -> None:
        """The service calls need an https endpoint and a profile ARN."""
        missing = []
        if not _https(self.endpoint, loopback_http=True):
            missing.append(KEY_ENDPOINT)
        if not _ARN_RE.fullmatch(self.profile_arn):
            missing.append(KEY_PROFILE_ARN)
        if missing:
            raise SettingsMissing(missing)

    def require_ssh(self) -> str:
        """The Session Manager plugin's Region, or :class:`SettingsMissing`."""
        if not _REGION_RE.fullmatch(self.ssh_region):
            raise SettingsMissing([KEY_SSH_REGION])
        return self.ssh_region

    def machine_cell(self) -> MachineCell:
        """What machine mode writes on the box, or :class:`SettingsMissing` naming every gap."""
        missing = []
        if not _https(self.kas_endpoint):
            missing.append(KEY_KAS_ENDPOINT)
        if not _https(self.kas_control_plane_endpoint):
            missing.append(KEY_KAS_CONTROL_PLANE)
        if not _REGION_RE.fullmatch(self.kas_region):
            missing.append(KEY_KAS_REGION)
        if not self.box_token_path.startswith("/"):
            missing.append(KEY_BOX_TOKEN_PATH)
        if missing:
            raise SettingsMissing(missing)
        return MachineCell(self.kas_endpoint, self.kas_control_plane_endpoint, self.kas_region)

    def is_lane_start_url(self, url: object) -> bool:
        """Whether a stored sign-in's start URL is the lane's (a trailing ``/`` ignored)."""
        mine = self.start_url.rstrip("/")
        return bool(mine) and isinstance(url, str) and url.rstrip("/") == mine


def _https(url: str, *, loopback_http: bool = False) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    return loopback_http and parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")


def _config(cfg: Any = None) -> Any:
    """*cfg*, else the live snapshot, else the config file; None when unreadable."""
    if cfg is not None:
        return cfg
    try:
        from kiro_crew.config import live

        snap = live.snapshot()
        if snap is not None:
            return snap
        from kiro_crew.config.loader import KiroCrewConfig

        return KiroCrewConfig.load()
    except Exception:  # noqa: BLE001 -- unread means unset
        return None


def lane_enabled(cfg: Any = None) -> bool:
    """Whether the lane is on: the ``crew_client`` preview, as the edition permits it."""
    from kiro_crew.crew_client_preview import preview_on

    return preview_on(cfg)


def read_settings(cfg: Any = None) -> LaneSettings:
    """The lane's values from *cfg* (else the live config). Unreadable reads as unset."""
    section = getattr(_config(cfg), SECTION, None)

    def text(name: str) -> str:
        value = getattr(section, name, "")
        return value.strip() if isinstance(value, str) else ""

    def names(name: str) -> tuple[str, ...]:
        value = getattr(section, name, ())
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(v for v in value if isinstance(v, str) and v)

    return LaneSettings(
        endpoint=text("managed_env_endpoint").rstrip("/"),
        start_url=text("managed_env_start_url"),
        profile_arn=text("managed_env_profile_arn"),
        scopes=names("managed_env_scopes"),
        ssh_region=text("managed_env_ssh_region"),
        kas_endpoint=text("managed_env_kas_endpoint"),
        kas_control_plane_endpoint=text("managed_env_kas_control_plane_endpoint"),
        kas_region=text("managed_env_kas_region"),
        mcp_registry_mode=getattr(section, "managed_env_mcp_registry_mode", False) is True,
        box_token_path=text("managed_env_box_token_path"),
        box_token_env=text("managed_env_box_token_env"),
        box_env_drop=names("managed_env_box_env_drop"),
        box_unset_env=names("managed_env_box_unset_env"),
        box_openssl=text("managed_env_box_openssl"),
    )


def sign_in_profile(cfg: Any = None) -> Optional[dict]:
    """The lane's Identity Center sign-in profile for the platform seam, or None.

    None while the lane is off, or while the start URL or the profile ARN is unset.
    Scopes default to Crew's own sign-in scopes.
    """
    if not lane_enabled(cfg):
        return None
    s = read_settings(cfg)
    if not _https(s.start_url) or not _ARN_RE.fullmatch(s.profile_arn):
        return None
    from kiro_crew.auth.login.endpoints import OIDC_SCOPES

    return {
        "scopes": list(s.scopes or OIDC_SCOPES),
        "grant_types": ["urn:ietf:params:oauth:grant-type:device_code", "refresh_token"],
        "issuer_url": s.start_url,
        "profile_arn": s.profile_arn,
    }
