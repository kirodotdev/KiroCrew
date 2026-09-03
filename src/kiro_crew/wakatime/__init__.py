"""WakaTime integration: send coding-activity heartbeats and read back stats.

The package talks to the WakaTime REST API (or a self-hosted, API-compatible
backend such as Wakapi or Hackatime via a configurable base URL). The API key
is a vault secret, never plain config; non-secret settings (enabled flag, base
URL) live in ``config.wakatime``.
"""

from __future__ import annotations

from kiro_crew.wakatime.client import WakaTimeAuthError, WakaTimeClient

__all__ = ["WakaTimeClient", "WakaTimeAuthError"]
