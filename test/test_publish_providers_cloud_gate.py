"""The publish-provider list must not advertise a withheld cloud destination.

Gating the ROW is what makes the Publish panel correct without a frontend change:
the panel renders whatever this endpoint returns, so a deployment that registers
only an internal destination shows only that one.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew.apps import routes as app_routes
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform.bootstrap import build_default_context


class _NoCloud:
    def admits_registry(self, kind: str, name: str, api_base: str) -> bool:
        return True

    def admits_cloud_deployment(self, target: str) -> bool:
        return False


class _AllowCloud:
    def admits_registry(self, kind: str, name: str, api_base: str) -> bool:
        return True

    def admits_cloud_deployment(self, target: str) -> bool:
        return True


def _with_policy(monkeypatch, policy):
    base = build_default_context(KiroCrewConfig())
    monkeypatch.setattr(
        ctx_mod, "current_context", lambda: dataclasses.replace(base, external_access=policy)
    )


async def _provider_ids(monkeypatch) -> list[str]:
    monkeypatch.setattr(app_routes, "list_apps", lambda: [])
    monkeypatch.setattr(app_routes, "collect_publish_providers", lambda _apps: [])
    resp = await app_routes.handle_publish_providers(_FakeRequest())
    import json

    return [p["id"] for p in json.loads(resp.text)["providers"]]


class _FakeRequest:
    """Carries only what the handler reads: the session key for the publish gate.

    #3599 added a second, independent gate to this handler (the publish-governance
    chokepoint), which reads ``X-Session-Key`` to resolve the governance profile.
    An empty mapping means "no session key", i.e. the ungoverned default — which
    is what these cloud-gate tests want, since they exercise the ``external_access``
    control, not the publish ceiling.
    """

    headers: dict = {}


@pytest.mark.asyncio
async def test_aws_row_is_present_by_default(monkeypatch):
    _with_policy(monkeypatch, _AllowCloud())
    assert "deploy-web-aws" in await _provider_ids(monkeypatch)


@pytest.mark.asyncio
async def test_aws_row_is_omitted_when_cloud_deployment_is_withheld(monkeypatch):
    """Otherwise the panel offers a destination whose every route refuses."""
    _with_policy(monkeypatch, _NoCloud())
    assert "deploy-web-aws" not in await _provider_ids(monkeypatch)


@pytest.mark.asyncio
async def test_app_declared_destinations_survive_the_gate(monkeypatch):
    """Withholding CLOUD deployment must not withhold an internal destination.

    This is the property the whole change exists for: an edition that denies AWS
    still publishes to its own registry, and that row must be untouched.
    """
    _with_policy(monkeypatch, _NoCloud())
    monkeypatch.setattr(app_routes, "list_apps", lambda: [])
    monkeypatch.setattr(
        app_routes,
        "collect_publish_providers",
        lambda _apps: [{"id": "artifactory", "label": "Artifactory (internal)"}],
    )

    import json

    resp = await app_routes.handle_publish_providers(_FakeRequest())
    ids = [p["id"] for p in json.loads(resp.text)["providers"]]

    assert ids == ["artifactory"]
