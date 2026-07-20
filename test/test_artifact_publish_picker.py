"""Tests for the publish-providers picker filter + `available` annotation.

`GET /api/artifacts/publish-providers` offers installable-but-not-yet-installed
providers instead of hiding them (they self-install on first publish via
`ensure_ready`), and annotates each row with `available` so the FE can hint
install-on-first-use.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _fake_provider(name: str, *, available: bool, installable: bool):
    from kiro_crew.publish_provider import (
        Capability,
        DiscoveryModel,
        KindSupport,
        SharingModel,
        SyncModel,
    )

    p = MagicMock()
    p.name = name
    p.display_name = name.title()
    p.available.return_value = available
    p.installable.return_value = installable
    p.capabilities.return_value = {Capability.CONTENT_VERSIONS}
    p.kind_support.return_value = KindSupport.NATIVE
    p.sharing_model.return_value = SharingModel()
    p.sync_model.return_value = SyncModel()
    p.discovery_model.return_value = DiscoveryModel()
    return p


class TestPickerIncludesInstallable:
    @pytest.mark.asyncio
    async def test_filter_and_available_flag(self, monkeypatch):
        import json

        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.handlers import artifacts as handlers

        ready = _fake_provider("ready", available=True, installable=False)
        heals = _fake_provider("heals", available=False, installable=True)
        hidden = _fake_provider("hidden", available=False, installable=False)
        monkeypatch.setattr(handlers, "list_providers", lambda: [ready, heals, hidden])

        req = make_mocked_request("GET", "/api/artifacts/publish-providers?kind=markdown")
        resp = await handlers.api_artifact_publish_providers(req)

        data = json.loads(resp.text)
        rows = {r["name"]: r for r in data["providers"]}
        assert set(rows) == {"ready", "heals"}  # 'hidden' filtered out
        assert rows["ready"]["available"] is True
        assert rows["heals"]["available"] is False
