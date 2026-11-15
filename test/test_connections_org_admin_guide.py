"""``org_admin_guide``: the registry field, and the redirect the status feed reports with it.

The redirect a card shows an org admin must be the one its Connect registers, so
the status row and :func:`crew_oauth_dcr.prepare` are checked against each other
rather than against a literal. No network: the registration answer is stubbed.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from kiro_crew.connections import (
    RegistryValidationError,
    crew_oauth_dcr,
    get_all_registry_providers,
    status,
)
from kiro_crew.connections.registry import _load_registry, get_provider

_DOCS = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "docs"


def _with(tmp_path, slug, **fields):
    payload = get_all_registry_providers()
    for entry in payload:
        if entry["slug"] == slug:
            entry.update(fields)
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _anchors(markdown: str) -> set[str]:
    """GitHub's heading anchors: lowercase, punctuation dropped, spaces to hyphens."""
    out = set()
    for line in markdown.splitlines():
        if line.startswith("#"):
            title = line.lstrip("#").strip().lower()
            out.add(re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", title)))
    return out


@pytest.mark.parametrize(
    "bad",
    ["", "connections.md", "#anchor", "../x.md#a", "https://x.example/a.md#b", "a.md#B", 7],
)
def test_org_admin_guide_must_be_a_docs_section(tmp_path, bad):
    with pytest.raises(RegistryValidationError, match="org_admin_guide must be"):
        _load_registry(_with(tmp_path, "atlassian", org_admin_guide=bad))


def test_org_admin_guide_is_refused_on_a_preregistered_entry(tmp_path):
    path = _with(
        tmp_path, "github", org_admin_guide="connections.md#atlassian-organization-admin-checklist"
    )
    with pytest.raises(RegistryValidationError, match="org_admin_guide is for DCR entries"):
        _load_registry(path)


def test_every_org_admin_guide_names_a_shipped_heading():
    declared = {
        p["slug"]: p["org_admin_guide"]
        for p in get_all_registry_providers()
        if "org_admin_guide" in p
    }
    assert declared == {"atlassian": "connections.md#atlassian-organization-admin-checklist"}
    for guide in declared.values():
        name, anchor = guide.split("#")
        assert anchor in _anchors((_DOCS / name).read_text(encoding="utf-8")), guide


def test_the_troubleshooting_link_to_the_checklist_resolves():
    text = (_DOCS / "troubleshooting.md").read_text(encoding="utf-8")
    assert "connections.md#atlassian-organization-admin-checklist" in text
    assert "atlassian-says-access-denied-your-organization-admin" in _anchors(text)


def _providers():
    return [get_provider("atlassian"), get_provider("notion")]


def test_a_card_without_crew_custody_reports_loopback_origins_on_any_port():
    redirects = status._sign_in_redirects(_providers(), frozenset())
    # Notion names no guide, so it gets no field at all.
    assert redirects == {"atlassian": {"anyPort": ["http://127.0.0.1", "http://localhost"]}}


@pytest.mark.parametrize("origin", ["", "https://gw.example.com"])
def test_a_crew_held_card_reports_the_redirect_its_connect_registers(monkeypatch, tmp_path, origin):
    import kiro_crew.config.paths as paths
    import kiro_crew.mcp_oauth_discovery as disc

    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(crew_oauth_dcr, "_public_origin", lambda: origin)
    monkeypatch.setattr(crew_oauth_dcr, "_audit", lambda *a, **k: None)
    issuer = get_provider("atlassian")["l0_expectations"]["authorization_server"]
    base = "https://auth.example"
    documents = {
        "https://mcp.atlassian.com/rm": {
            "resource": "https://mcp.atlassian.com/v2/mcp",
            "authorization_servers": [issuer],
        },
    }
    registered: list[dict] = []

    async def _fetch(url):
        if url in documents:
            return documents[url]
        if url.endswith("oauth-authorization-server/VCeDsk8ZHncYF1g234fKtc4lNipbBhu3"):
            return {
                "issuer": issuer,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "registration_endpoint": f"{base}/register",
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            }
        raise ValueError(f"{url} returned HTTP 404")

    async def _post(url, *, form=None, body=None, auth=None, headers=None):
        if url == "https://mcp.atlassian.com/v2/mcp":
            return 401, {}, 'Bearer resource_metadata="https://mcp.atlassian.com/rm"'
        registered.append(body)
        return 201, {"client_id": "cid"}, ""

    monkeypatch.setattr(disc, "_fetch_json", _fetch)
    monkeypatch.setattr(crew_oauth_dcr, "_post", _post)

    shown = status._sign_in_redirects(_providers(), frozenset({"atlassian"}))["atlassian"]
    plan = asyncio.run(crew_oauth_dcr.prepare("atlassian"))
    assert shown == {"exact": plan.client.redirect_uri}
    assert registered[0]["redirect_uris"] == [shown["exact"]]
    expected = (
        (origin + "/api/connections/oauth/callback")
        if origin
        else "http://127.0.0.1:48100/callback"
    )
    assert shown["exact"] == expected


def test_the_status_row_carries_the_redirect_for_a_guided_card_only(monkeypatch):
    monkeypatch.setattr(status, "get_visible_providers", _providers)
    monkeypatch.setattr(status, "chat_backend_is_kas", lambda: False)
    monkeypatch.setattr(status, "_grant_presence_maps", lambda providers, kas: ({}, {}))
    monkeypatch.setattr(status, "_crew_managed_slugs", lambda providers: frozenset())
    monkeypatch.setattr(status, "_client_config_map", lambda providers: {})
    monkeypatch.setattr(status, "reconcile_connected_since", lambda *a, **k: {})
    rows = {row["slug"]: row for row in asyncio.run(status.collect_connection_statuses())}
    assert rows["atlassian"]["signInRedirect"] == {
        "anyPort": ["http://127.0.0.1", "http://localhost"]
    }
    assert "signInRedirect" not in rows["notion"]
