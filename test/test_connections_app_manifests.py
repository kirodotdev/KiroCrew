"""Tests for ``GET /api/connections/app-manifests``.

The Slack card's "Before you connect" tip offers **Create the Slack app** and
**Copy manifest**. Both come from this route, so the card names no vendor: the
gateway answers, per card slug, the create link and the manifest it carries.
For Slack that is ``slack_manifest`` with the Slack MCP switch on.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew import slack_manifest
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.connections.registry import get_provider, is_preregistered, is_visible
from kiro_crew.dashboard.handlers import connections_app_manifest

PATH = "/api/connections/app-manifests"
NON_OWNER = {"X-Test-User": "someone-else"}
REGISTRY = (
    Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "connections" / "registry.json"
)


def _use_config(monkeypatch: pytest.MonkeyPatch, cfg: object) -> None:
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))


async def _get(headers: dict[str, str] | None = None) -> tuple[int, dict]:
    app = web.Application()
    app.router.add_get(PATH, connections_app_manifest.api_connections_app_manifests)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get(PATH, headers=headers or {})
        return resp.status, await resp.json()
    finally:
        await client.close()


def _slack_registry_entry() -> dict:
    entries = json.loads(REGISTRY.read_text(encoding="utf-8"))
    rows = entries if isinstance(entries, list) else entries.get("providers", [])
    return next(row for row in rows if row.get("slug") == "slack")


@pytest.mark.asyncio
async def test_slack_gets_the_manifest_link_with_the_slack_mcp_switch_on(monkeypatch):
    _use_config(monkeypatch, SimpleNamespace(slack=SimpleNamespace(command="")))

    status, body = await _get()

    assert status == 200
    assert body["schema_version"] == 1
    slack = body["apps"]["slack"]
    features = {slack_manifest.SLACK_MCP}
    alias = slack_manifest.DEFAULT_ALIAS
    assert slack["create_url"] == slack_manifest.deep_link(alias, features)
    assert slack["manifest"] == slack_manifest.render(alias, strip_comments=True, features=features)


@pytest.mark.asyncio
async def test_the_copied_manifest_is_the_one_the_link_opens(monkeypatch):
    _use_config(monkeypatch, None)

    _, body = await _get()

    slack = body["apps"]["slack"]
    url = urlsplit(slack["create_url"])
    assert (url.scheme, url.netloc, url.path) == ("https", "api.slack.com", "/apps")
    assert parse_qs(url.query)["manifest_yaml"] == [slack["manifest"]]


@pytest.mark.asyncio
async def test_the_manifest_carries_what_the_slack_card_signs_in_with(monkeypatch):
    """MCP on, PKCE on, the card's redirect, and the card's user scopes."""
    _use_config(monkeypatch, None)
    entry = _slack_registry_entry()

    _, body = await _get()

    manifest = yaml.safe_load(body["apps"]["slack"]["manifest"])
    oauth = manifest["oauth_config"]
    assert manifest["settings"]["is_mcp_enabled"] is True
    assert oauth["pkce_enabled"] is True
    auth = entry["auth"]
    assert oauth["redirect_urls"] == [
        f"http://{auth['redirect_host']}:{auth['redirect_port']}/callback"
    ]
    assert sorted(oauth["scopes"]["user"]) == sorted(entry["recommended_scopes"])


@pytest.mark.asyncio
async def test_the_link_keeps_this_installs_alias_and_slack_code_switches(monkeypatch):
    """The created app is one the gateway's bot can also run on."""
    cfg = SimpleNamespace(
        slack=SimpleNamespace(command="kirocrew-team", agent_sessions=True),
    )
    _use_config(monkeypatch, cfg)

    _, body = await _get()

    features = {slack_manifest.SLACK_MCP, slack_manifest.AGENT_SESSIONS}
    assert body["apps"]["slack"]["create_url"] == slack_manifest.deep_link("team", features)


@pytest.mark.asyncio
async def test_a_non_owner_is_refused(monkeypatch):
    _use_config(monkeypatch, None)

    status, body = await _get(NON_OWNER)

    assert status == 403
    assert body["code"] == "owner_only"
    assert "apps" not in body


@pytest.mark.asyncio
async def test_a_missing_template_drops_the_card_not_the_route(monkeypatch):
    _use_config(monkeypatch, None)

    def _missing(*_a, **_k):
        raise FileNotFoundError("slack-manifest.yaml")

    monkeypatch.setattr(slack_manifest, "deep_link", _missing)

    status, body = await _get()

    assert status == 200
    assert body["apps"] == {}


def test_every_generator_names_a_visible_preregistered_card():
    for slug in connections_app_manifest._GENERATORS:
        provider = get_provider(slug)
        assert provider is not None, slug
        assert is_preregistered(provider) and is_visible(provider), slug
