"""Disconnect when Kiro Crew holds the sign-in (``connections.crew_oauth``).

Reuses the handler harness of ``test_connections_disconnect.py``: every side
effect but the decision under test is neutralized there.
"""

from __future__ import annotations

import pytest
from test_connections_disconnect import _SLUG, _client, _entry, _provider_url, _wire


@pytest.mark.asyncio
async def test_a_failed_crew_held_cleanup_is_a_coded_failure_not_a_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Crew-held sign-in that could not be removed must not read as disconnected."""
    from kiro_crew.connections import crew_oauth

    purged: list[str] = []
    audits: list[dict] = []
    _wire(
        monkeypatch,
        removed=[],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=purged,
        audits=audits,
    )

    async def _vault_broken(_slug: str) -> dict:
        raise OSError("vault unreadable")

    monkeypatch.setattr(crew_oauth, "manages", lambda _slug: True)
    monkeypatch.setattr(crew_oauth, "disconnect", _vault_broken)
    client = await _client()
    try:
        resp = await client.post("/api/connections/disconnect", json={"slug": _SLUG})
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 502
    assert body["code"] == "crew_cleanup_failed" and body["entryRemoved"] is True
    assert purged == [_SLUG], "the rest of Disconnect still ran"
    assert [a["outcome"] for a in audits if a.get("operation") == "connections_disconnect"] == [
        "partial"
    ]


@pytest.mark.asyncio
async def test_disconnect_always_revokes_the_crew_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disconnect = Crew grant revoked, even when another entry keeps the native grant."""
    from kiro_crew.connections import crew_oauth, crew_oauth_proxy

    _wire(
        monkeypatch,
        removed=[],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url()), _entry("notion-work", _provider_url())],
        purged=[],
        revoked=[],
    )
    called: list[str] = []

    async def _disconnect(slug: str) -> dict:
        called.append("disconnect")
        return {"removed": True, "revoked": True}

    monkeypatch.setattr(crew_oauth, "manages", lambda _slug: True)
    monkeypatch.setattr(crew_oauth, "configured_entry_routed", lambda _slug: None)
    monkeypatch.setattr(crew_oauth, "disconnect", _disconnect)
    monkeypatch.setattr(crew_oauth_proxy, "drop_slug", lambda slug: called.append("drop"))
    client = await _client()
    try:
        resp = await client.post("/api/connections/disconnect", json={"slug": _SLUG})
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    # Option B: the card reads "grant removed", never "kept for a sharer".
    assert body["grantSharedWith"] == [] and body["grantRemoved"] is True
    assert body["grantCensusIncomplete"] is False
    assert body["crewGrantRemoved"] is True and body["upstreamRevoked"] is True
    assert "crewGrantKept" not in body
    assert called == ["drop", "disconnect"], "Crew's grant was not revoked and cut"


class _UnreadableVault:
    def __init__(self, *_a: object) -> None:
        pass

    def present(self, _slug: str) -> bool:
        raise OSError("vault unreadable")


@pytest.mark.asyncio
@pytest.mark.parametrize("vault", ["empty", "unreadable"])
async def test_a_flag_off_disconnect_answers_as_on_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path, vault: str
) -> None:
    """Flag off: an empty or unreadable vault is no Crew pair, so Disconnect is main's 200."""
    from kiro_crew.connections import crew_oauth
    from kiro_crew.secrets import SecretVault

    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=[],
    )
    called: list[str] = []

    async def _disconnect(slug: str) -> dict:
        called.append(slug)
        return {"removed": False, "revoked": False}

    real_store = crew_oauth.TokenStore
    store = (lambda: real_store(SecretVault(tmp_path))) if vault == "empty" else _UnreadableVault
    if vault == "empty":
        assert real_store(SecretVault(tmp_path)).present(_SLUG) is False  # healthy, empty
    monkeypatch.setattr(crew_oauth, "manages", lambda _slug: False)
    monkeypatch.setattr(crew_oauth, "TokenStore", store)
    monkeypatch.setattr(crew_oauth, "SUPPORTED_SLUGS", frozenset({_SLUG}))
    monkeypatch.setattr(crew_oauth, "disconnect", _disconnect)
    client = await _client()
    try:
        resp = await client.post("/api/connections/disconnect", json={"slug": _SLUG})
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200 and body["ok"] is True
    assert "crewGrantRemoved" not in body and called == []


@pytest.mark.asyncio
async def test_a_leftover_crew_pair_does_not_hide_a_kept_native_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag now off: the old Crew pair goes, and the native sharing answer still shows."""
    from kiro_crew.connections import crew_oauth

    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=["token", "registration"],
        inventory=[_entry(_SLUG, _provider_url()), _entry("notion-work", _provider_url())],
        purged=[],
        revoked=[],
    )

    async def _disconnect(slug: str) -> dict:
        return {"removed": True, "revoked": True}

    monkeypatch.setattr(crew_oauth, "manages", lambda _slug: False)
    monkeypatch.setattr(crew_oauth, "holds_grant", lambda _slug: True)
    monkeypatch.setattr(crew_oauth, "disconnect", _disconnect)
    client = await _client()
    try:
        resp = await client.post("/api/connections/disconnect", json={"slug": _SLUG})
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    assert body["crewGrantRemoved"] is True
    assert body["grantSharedWith"] == ["notion-work"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("routed", "crew"), [(True, True), (None, True), (False, False)])
async def test_connect_uses_crews_sign_in_only_where_routing_does(
    monkeypatch: pytest.MonkeyPatch, routed: bool | None, crew: bool
) -> None:
    """Connect agrees with status and routing: an entry kept native mints natively."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from dashboard_owner_helpers import as_owner

    from kiro_crew.connections import crew_oauth, warm
    from kiro_crew.dashboard.handlers import connections

    started: list[str] = []

    async def _crew_start(slug: str) -> str:
        started.append(slug)
        return "crew-tok"

    async def _adopt(slug: str, url: str) -> str:
        return "native-tok"

    monkeypatch.setattr(connections, "_oauth_client_configured", lambda provider: True)
    monkeypatch.setattr(crew_oauth, "manages", lambda slug: True)
    monkeypatch.setattr(crew_oauth, "configured_entry_routed", lambda slug: routed)
    monkeypatch.setattr(crew_oauth, "start_mint_row", _crew_start)
    monkeypatch.setattr(warm, "adopt_shared_mint", _adopt)
    app = web.Application()
    app.router.add_post("/api/connections/mint", connections.api_connections_mint)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/connections/mint", json={"slug": "github"})
        body = await resp.json()
    finally:
        await client.close()
    assert resp.status == 200
    assert (body["token"] == "crew-tok") is crew and (started == ["github"]) is crew
