"""The managed-environment lane's settings: the ``instances.managed_env_*`` keys.

They are read only while the ``crew_client`` feature preview is on
(``edition_locks.preview_on``, so an LTS build reads it as off); off, every value
reads as empty. Every key is empty by default and nothing is guessed: a step that
needs a missing or malformed value raises :class:`SettingsMissing`, which names the
key to set.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterable

#: The feature preview that gates the lane.
PREVIEW = "crew_client"
#: The config section every key lives in.
SECTION = "instances"

KEY_ENDPOINT = "managed_env_endpoint"


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
class LaneSettings:
    """The ``instances.managed_env_*`` values, as read. Empty means unset."""

    endpoint: str = ""

    def service_endpoint(self) -> str:
        """The endpoint for a service call, or :class:`SettingsMissing`.

        It must be ``https://`` with a host; plain ``http://`` only to a loopback host.
        """
        if not service_endpoint_ok(self.endpoint):
            raise SettingsMissing([KEY_ENDPOINT])
        return self.endpoint.rstrip("/")


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


def read_settings(cfg: Any = None) -> LaneSettings:
    """The lane's settings from *cfg* (else the live config); all empty while the preview is off."""
    from kiro_crew.edition_locks import preview_on

    if cfg is None:
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = live.snapshot() or KiroCrewConfig.load()
    if not preview_on(PREVIEW, cfg):
        return LaneSettings()
    endpoint = getattr(getattr(cfg, SECTION, None), KEY_ENDPOINT, "")
    return LaneSettings(endpoint=endpoint if isinstance(endpoint, str) else "")
