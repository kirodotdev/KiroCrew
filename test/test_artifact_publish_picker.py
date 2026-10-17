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


class TestPickerPublicReachable:
    """Each row carries `public_reachable` so the FE can decide whether the
    public-exposure warning and acknowledgment belong in front of the confirm."""

    @pytest.mark.asyncio
    async def test_declared_false_is_carried_as_false(self, monkeypatch):
        import json

        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.handlers import artifacts as handlers

        private = _fake_provider("private", available=True, installable=False)
        private.public_reachable = False
        public = _fake_provider("public", available=True, installable=False)
        public.public_reachable = True
        monkeypatch.setattr(handlers, "list_providers", lambda: [private, public])

        req = make_mocked_request("GET", "/api/artifacts/publish-providers?kind=markdown")
        resp = await handlers.api_artifact_publish_providers(req)

        rows = {r["name"]: r for r in json.loads(resp.text)["providers"]}
        assert rows["private"]["public_reachable"] is False
        assert rows["public"]["public_reachable"] is True

    @pytest.mark.asyncio
    async def test_undeclared_provider_defaults_to_reachable(self, monkeypatch):
        """A provider that never declares the field is reported reachable: the
        wrong default here would be a public link with no warning."""
        import json

        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.handlers import artifacts as handlers
        from kiro_crew.publish_provider import PublishProvider

        class Undeclared(PublishProvider):
            name = "undeclared"

            def available(self) -> bool:
                return True

            def view_url_for(self, external_id: str) -> str:
                return f"https://example.com/{external_id}"

            async def publish(self, **kwargs):  # pragma: no cover
                raise NotImplementedError

            async def push_version(self, **kwargs):  # pragma: no cover
                raise NotImplementedError

            async def update_sharing(self, **kwargs):  # pragma: no cover
                raise NotImplementedError

            async def unpublish(self, **kwargs):  # pragma: no cover
                raise NotImplementedError

        assert PublishProvider.public_reachable is True
        monkeypatch.setattr(handlers, "list_providers", lambda: [Undeclared()])

        req = make_mocked_request("GET", "/api/artifacts/publish-providers?kind=markdown")
        resp = await handlers.api_artifact_publish_providers(req)

        (row,) = json.loads(resp.text)["providers"]
        assert row["public_reachable"] is True
