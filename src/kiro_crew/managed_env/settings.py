"""The managed-environment lane's settings: the ``instances.managed_env_*`` keys.

They are read only while the ``crew_client`` feature preview is on
(``edition_locks.preview_on``, so an LTS build reads it as off); off, every value
reads as empty. Every key is empty by default and nothing is guessed: a step that
needs a missing or malformed value raises :class:`SettingsMissing`, which names the
keys to set.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from kiro_crew.shared_crew.ssm import valid_region

#: The feature preview that gates the lane.
PREVIEW = "crew_client"
#: The config section every key lives in.
SECTION = "instances"

KEY_ENDPOINT = "managed_env_endpoint"
KEY_START_URL = "managed_env_start_url"
KEY_PROFILE_ARN = "managed_env_profile_arn"
KEY_SSH_REGION = "managed_env_ssh_region"
KEY_KAS_ENDPOINT = "managed_env_kas_endpoint"
KEY_KAS_CONTROL_PLANE = "managed_env_kas_control_plane_endpoint"
KEY_KAS_REGION = "managed_env_kas_region"
KEY_BOX_TOKEN_PATH = "managed_env_box_token_path"
KEY_BOX_ENV_DROP = "managed_env_box_env_drop"
TEXT_KEYS = (
    KEY_ENDPOINT,
    KEY_START_URL,
    KEY_PROFILE_ARN,
    KEY_SSH_REGION,
    KEY_KAS_ENDPOINT,
    KEY_KAS_CONTROL_PLANE,
    KEY_KAS_REGION,
    KEY_BOX_TOKEN_PATH,
)

_ARN_RE = re.compile(r"arn:[a-z-]+:[a-z0-9-]+:[a-z0-9-]*:[0-9]*:\S+")
#: An environment variable name the image may set (cleared before Crew starts there).
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


def _https_url(url: object) -> bool:
    """An ``https://`` URL with a host and no user info, query or fragment."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(host)
        and "@" not in parts.netloc
        and not parts.query
        and not parts.fragment
    )


class SettingsMissing(LookupError):
    """A step needs values the operator has not set. ``keys`` are their full paths."""

    def __init__(self, keys: Iterable[str]):
        self.keys = tuple(f"{SECTION}.{k}" for k in keys)
        super().__init__(
            "Managed environments are not set up: set "
            + ", ".join(self.keys)
            + " in config.json (see docs/configuration.md)."
        )


@dataclass(frozen=True)
class Cell:
    """What a new managed environment's Crew runs its engine against."""

    endpoint: str
    control_plane_endpoint: str
    region: str
    token_path: str
    env_drop: tuple[str, ...] = ()


@dataclass(frozen=True)
class LaneSettings:
    """The ``instances.managed_env_*`` values, as read. Empty means unset."""

    endpoint: str = ""
    start_url: str = ""
    profile_arn: str = ""
    ssh_region: str = ""
    kas_endpoint: str = ""
    kas_control_plane_endpoint: str = ""
    kas_region: str = ""
    box_token_path: str = ""
    box_env_drop: tuple[str, ...] = ()

    def service_endpoint(self) -> str:
        """The endpoint for a service call, or :class:`SettingsMissing`.

        It must be ``https://`` with a host; plain ``http://`` only to a loopback host.
        """
        if not service_endpoint_ok(self.endpoint):
            raise SettingsMissing([KEY_ENDPOINT])
        return self.endpoint.rstrip("/")

    def ssh_region_or_raise(self) -> str:
        if not valid_region(self.ssh_region):
            raise SettingsMissing([KEY_SSH_REGION])
        return self.ssh_region

    def cell(self) -> Cell:
        """The engine cell a new environment's Crew is given, or :class:`SettingsMissing`
        naming every gap."""
        missing = [
            key
            for key, ok in (
                (KEY_KAS_ENDPOINT, _https_url(self.kas_endpoint)),
                (KEY_KAS_CONTROL_PLANE, _https_url(self.kas_control_plane_endpoint)),
                (KEY_KAS_REGION, valid_region(self.kas_region)),
            )
            if not ok
        ]
        if not self.box_token_path.startswith("/") or any(
            c in self.box_token_path for c in "\n\r\0"
        ):
            missing.append(KEY_BOX_TOKEN_PATH)
        if not all(ENV_NAME_RE.fullmatch(n) for n in self.box_env_drop):
            missing.append(KEY_BOX_ENV_DROP)
        if missing:
            raise SettingsMissing(missing)
        return Cell(
            self.kas_endpoint,
            self.kas_control_plane_endpoint,
            self.kas_region,
            self.box_token_path,
            self.box_env_drop,
        )

    @property
    def canonical_start_url(self) -> str:
        """The start URL in the form Crew's sign-in stores (``https://``, ``/start``), or ``""``."""
        return canonical_start_url(self.start_url)

    def is_lane_start_url(self, url: object) -> bool:
        """Whether a stored sign-in's start URL is the lane's, both in the sign-in's form."""
        mine = self.canonical_start_url
        return bool(mine) and isinstance(url, str) and canonical_start_url(url) == mine


def canonical_start_url(raw: str) -> str:
    """*raw* the way Crew's sign-in canonicalizes a start URL, or ``""`` when it is not one."""
    from kiro_crew.cloud.login_target import LoginTargetError, normalize_start_url

    value = (raw or "").strip().split("#", 1)[0]
    if not value:
        return ""
    try:
        return normalize_start_url(value).rstrip("/")
    except LoginTargetError:
        return ""


def is_loopback_url(url: str) -> bool:
    from kiro_crew.dashboard.urls import is_loopback

    try:
        return is_loopback(urllib.parse.urlsplit(url).hostname or "")
    except ValueError:
        return False


def service_endpoint_ok(url: object) -> bool:
    """``https://`` with a host, or ``http://`` to a loopback host (a local test service):
    the same rule as the app market client."""
    from kiro_crew.apps.market_client import _https_or_loopback

    if not isinstance(url, str) or not url:
        return False
    try:
        return _https_or_loopback(url)
    except ValueError:
        return False


def _config(cfg: Any) -> Any:
    if cfg is not None:
        return cfg
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    return live.snapshot() or KiroCrewConfig.load()


def lane_enabled(cfg: Any = None) -> bool:
    """Whether the lane is on: the ``crew_client`` preview, as the edition permits it."""
    from kiro_crew.edition_locks import preview_on

    try:
        cfg = _config(cfg)
    except Exception:  # noqa: BLE001 - an unreadable config is off
        return False
    return preview_on(PREVIEW, cfg)


def read_settings(cfg: Any = None) -> LaneSettings:
    """The lane's settings from *cfg* (else the live config); all empty while the preview is off."""
    cfg = _config(cfg)
    if not lane_enabled(cfg):
        return LaneSettings()
    inst = getattr(cfg, SECTION, None)

    def text(key: str) -> str:
        value = getattr(inst, key, "")
        return value if isinstance(value, str) else ""

    drop = getattr(inst, KEY_BOX_ENV_DROP, None)
    return LaneSettings(
        endpoint=text(KEY_ENDPOINT),
        start_url=text(KEY_START_URL),
        profile_arn=text(KEY_PROFILE_ARN),
        ssh_region=text(KEY_SSH_REGION),
        kas_endpoint=text(KEY_KAS_ENDPOINT),
        kas_control_plane_endpoint=text(KEY_KAS_CONTROL_PLANE),
        kas_region=text(KEY_KAS_REGION),
        box_token_path=text(KEY_BOX_TOKEN_PATH),
        box_env_drop=tuple(v for v in drop if isinstance(v, str)) if isinstance(drop, list) else (),
    )


def sign_in_profile(cfg: Any = None) -> Optional[dict]:
    """The lane's IAM Identity Center sign-in profile for the platform seam, or None.

    None while the lane is off, or while the start URL or the profile ARN is unset.
    The scopes are Crew's own sign-in scopes.
    """
    s = read_settings(cfg)
    url = s.canonical_start_url
    if not url or not _ARN_RE.fullmatch(s.profile_arn):
        return None
    from kiro_crew.auth.login.endpoints import OIDC_SCOPES

    return {
        "scopes": list(OIDC_SCOPES),
        "grant_types": ["urn:ietf:params:oauth:grant-type:device_code", "refresh_token"],
        "issuer_url": url,
        "profile_arn": s.profile_arn,
    }
