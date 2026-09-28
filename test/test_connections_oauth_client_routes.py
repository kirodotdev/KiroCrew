"""Tests for the three ``/api/connections/oauth-clients`` routes.

One record per pre-registered provider, two halves with different custody: the
PUBLIC client id lives in ``config.json`` and is echoed back, the SECRET lives in
the vault and is reported only as ``client_secret_set``. GET is readable by any
dashboard user; PUT and DELETE are owner-only. Everything runs against a scratch
``KIROCREW_HOME`` so neither the operator's config nor their vault is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard.handlers import connections
from kiro_crew.secrets import SecretVault

BASE = "/api/connections/oauth-clients"
GITHUB_VAULT_NAME = "CONNECTIONS_GITHUB_CLIENT_SECRET"
ASANA_VAULT_NAME = "CONNECTIONS_ASANA_CLIENT_SECRET"
GITHUB_ENV_ID = "KIROCREW_CONNECTIONS_GITHUB_CLIENT_ID"
GITHUB_ENV_SECRET = "KIROCREW_CONNECTIONS_GITHUB_CLIENT_SECRET"
NON_OWNER = {"X-Test-User": "someone-else"}
SECRET = "s3cr3t-value-that-must-never-be-echoed"


@pytest.fixture()
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A scratch config dir + vault; no operator env values leak in."""
    scratch = tmp_path / "home"
    scratch.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(scratch))
    for name in (
        GITHUB_ENV_ID,
        GITHUB_ENV_SECRET,
        "KIROCREW_CONNECTIONS_ASANA_CLIENT_ID",
        "KIROCREW_CONNECTIONS_ASANA_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    return scratch


@pytest.fixture(autouse=True)
def refreshed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record projection refreshes instead of rebuilding a real agent spec.

    The handler's ``_refresh_client_projections`` cancels the slug's in-flight
    mint and rebuilds ``~/.kiro/agents/kirocrew.json``; neither belongs in a route
    test's scratch home. The recorded slugs let a test assert that a mutation DID
    ask for the refresh, which is the contract the review pinned (a rotated or
    deleted client must not keep authorizing through a stale projection).
    """
    calls: list[str] = []

    async def _record(slug: str) -> bool:
        calls.append(slug)
        return True

    monkeypatch.setattr(connections, "_refresh_client_projections", _record)
    return calls


async def _client() -> TestClient:
    app = web.Application()
    app.router.add_get(BASE, connections.api_connections_oauth_clients)
    app.router.add_put(BASE + "/{slug}", connections.api_connections_oauth_client_put)
    app.router.add_delete(BASE + "/{slug}", connections.api_connections_oauth_client_delete)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _config(home: Path) -> dict:
    path = home / "config.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _client_id_record(home: Path, slug: str) -> dict | None:
    return _config(home).get("connections", {}).get("oauth_clients", {}).get(slug)


# ── GET ──


@pytest.mark.asyncio
async def test_get_lists_every_preregistered_provider_unconfigured(home):
    client = await _client()
    try:
        resp = await client.get(BASE)
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()

    assert body["schema_version"] == 1
    by_slug = {c["slug"]: c for c in body["clients"]}
    assert set(by_slug) == {"github", "asana"}
    for slug, port in (("github", 48101), ("asana", 48102)):
        record = by_slug[slug]
        assert record["configured"] is False
        assert record["confidential"] is True
        assert record["client_id"] is None
        assert record["client_id_source"] is None
        assert record["client_secret_set"] is False
        assert record["client_secret_source"] is None
        assert record["redirect_uri"] == f"http://127.0.0.1:{port}/callback"
        assert record["registration_guide"] == f"oauth-app-registration/{slug}.md"
        assert "client_secret" not in record


@pytest.mark.asyncio
async def test_get_never_carries_a_secret_value(home):
    SecretVault(home).set_sync(GITHUB_VAULT_NAME, SECRET)
    client = await _client()
    try:
        resp = await client.get(BASE)
        body = await resp.json()
    finally:
        await client.close()

    assert SECRET not in json.dumps(body)
    github = next(c for c in body["clients"] if c["slug"] == "github")
    assert github["client_secret_set"] is True
    assert github["client_secret_source"] == "vault"


@pytest.mark.asyncio
async def test_get_is_readable_by_a_non_owner(home):
    """The payload is what the gallery needs; it exposes nothing a consent URL
    would not."""
    client = await _client()
    try:
        resp = await client.get(BASE, headers=NON_OWNER)
        assert resp.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_reports_env_sourced_values(home, monkeypatch):
    monkeypatch.setenv(GITHUB_ENV_ID, "Iv1.from-env")
    monkeypatch.setenv(GITHUB_ENV_SECRET, SECRET)
    client = await _client()
    try:
        body = await (await client.get(BASE)).json()
    finally:
        await client.close()

    github = next(c for c in body["clients"] if c["slug"] == "github")
    assert (github["client_id"], github["client_id_source"]) == ("Iv1.from-env", "env")
    assert (github["client_secret_set"], github["client_secret_source"]) == (True, "env")
    assert github["configured"] is True
    assert SECRET not in json.dumps(body)


# ── PUT: the gate and the validators ──


@pytest.mark.asyncio
async def test_put_is_owner_only(home):
    client = await _client()
    try:
        resp = await client.put(f"{BASE}/github", json={"client_id": "Iv1.x"}, headers=NON_OWNER)
        assert resp.status == 403
        body = await resp.json()
    finally:
        await client.close()

    assert body["code"] == "owner_only"
    assert _client_id_record(home, "github") is None
    assert not (home / "config.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["notion", "not-a-provider", "GITHUB!", ""])
async def test_put_rejects_anything_but_a_preregistered_provider(home, slug):
    """Notion is a real provider but a DCR one: there is no client to configure."""
    client = await _client()
    try:
        resp = await client.put(f"{BASE}/{slug}", json={"client_id": "Iv1.x"})
        # An empty slug does not match the route at all.
        assert resp.status in (400, 404)
        if resp.status == 400:
            assert (await resp.json())["code"] == "unknown_provider"
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   ", "has space", "ctrl\x01", "a" * 513, 42, None, ["x"]])
async def test_put_rejects_an_unusable_client_id(home, bad):
    client = await _client()
    try:
        resp = await client.put(f"{BASE}/github", json={"client_id": bad})
        assert resp.status == 400
        body = await resp.json()
    finally:
        await client.close()

    assert body["code"] == "invalid_client_id"
    assert _client_id_record(home, "github") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   ", "two\nlines", "cr\rhere", "nul\x00", "x" * 4097, 1])
async def test_put_rejects_an_unusable_client_secret(home, bad):
    client = await _client()
    try:
        resp = await client.put(f"{BASE}/github", json={"client_secret": bad})
        assert resp.status == 400
        body = await resp.json()
    finally:
        await client.close()

    assert body["code"] == "invalid_client_secret"
    assert SecretVault(home).list_names() == []


@pytest.mark.asyncio
async def test_put_rejects_secret_and_clear_together(home):
    client = await _client()
    try:
        resp = await client.put(
            f"{BASE}/github", json={"client_secret": SECRET, "client_secret_clear": True}
        )
        assert resp.status == 400
        body = await resp.json()
    finally:
        await client.close()

    assert body["code"] == "invalid_body"
    assert "mutually exclusive" in body["error"]
    assert SecretVault(home).list_names() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["true", 1, None, "yes"])
async def test_put_rejects_a_non_boolean_clear_flag(home, bad):
    client = await _client()
    try:
        resp = await client.put(f"{BASE}/github", json={"client_secret_clear": bad})
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_body"
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"client_secret_clear": False}, {"unrelated": 1}])
async def test_put_with_nothing_to_update_is_a_client_error(home, body):
    client = await _client()
    try:
        resp = await client.put(f"{BASE}/github", json=body)
        assert resp.status == 400
        payload = await resp.json()
    finally:
        await client.close()

    assert payload["code"] == "invalid_body"
    assert "nothing to update" in payload["error"]


@pytest.mark.asyncio
async def test_put_rejects_a_non_object_or_non_json_body(home):
    client = await _client()
    try:
        as_list = await client.put(f"{BASE}/github", json=["client_id"])
        assert as_list.status == 400
        assert (await as_list.json())["code"] == "invalid_body"

        not_json = await client.put(
            f"{BASE}/github", data=b"{not json", headers={"Content-Type": "application/json"}
        )
        assert not_json.status == 400
        assert (await not_json.json())["code"] == "invalid_body"
    finally:
        await client.close()


# ── PUT: the writes ──


@pytest.mark.asyncio
async def test_put_client_id_writes_config_and_stays_unconfigured_for_confidential(home):
    # Pre-existing settings must survive the read-modify-write.
    (home / "config.json").write_text(
        json.dumps({"agent": {"model": "keep-me"}, "connections": {"other": True}}),
        encoding="utf-8",
    )
    client = await _client()
    try:
        resp = await client.put(f"{BASE}/github", json={"client_id": "  Iv1.operator  "})
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()

    assert body["ok"] is True
    record = body["client"]
    assert record["slug"] == "github"
    assert record["client_id"] == "Iv1.operator"  # trimmed
    assert record["client_id_source"] == "config"
    assert record["client_secret_set"] is False
    assert record["configured"] is False  # GitHub is confidential: the id alone is not enough

    config = _config(home)
    assert config["connections"]["oauth_clients"]["github"] == {"client_id": "Iv1.operator"}
    assert config["agent"]["model"] == "keep-me"
    assert config["connections"]["other"] is True
    assert SecretVault(home).list_names() == []


@pytest.mark.asyncio
async def test_put_client_id_overwrites_a_previous_value(home):
    client = await _client()
    try:
        await client.put(f"{BASE}/github", json={"client_id": "Iv1.first"})
        resp = await client.put(f"{BASE}/github", json={"client_id": "Iv1.second"})
        body = await resp.json()
    finally:
        await client.close()

    assert body["client"]["client_id"] == "Iv1.second"
    assert _client_id_record(home, "github") == {"client_id": "Iv1.second"}


@pytest.mark.asyncio
async def test_put_client_secret_stores_in_the_vault_and_configures(home):
    client = await _client()
    try:
        await client.put(f"{BASE}/github", json={"client_id": "Iv1.operator"})
        resp = await client.put(f"{BASE}/github", json={"client_secret": SECRET})
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()

    record = body["client"]
    assert record["client_secret_set"] is True
    assert record["client_secret_source"] == "vault"
    assert record["configured"] is True
    assert SECRET not in json.dumps(body)

    vault = SecretVault(home)
    assert vault.list_names() == [GITHUB_VAULT_NAME]
    held = vault.get(GITHUB_VAULT_NAME)
    assert held is not None and held.reveal() == SECRET
    # The secret never touches config.json.
    assert SECRET not in (home / "config.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_put_client_secret_is_stored_verbatim(home):
    client = await _client()
    try:
        await client.put(f"{BASE}/github", json={"client_secret": "  padded  "})
    finally:
        await client.close()

    held = SecretVault(home).get(GITHUB_VAULT_NAME)
    assert held is not None and held.reveal() == "  padded  "


@pytest.mark.asyncio
async def test_put_secret_alone_does_not_configure_without_an_id(home):
    client = await _client()
    try:
        body = await (await client.put(f"{BASE}/github", json={"client_secret": SECRET})).json()
    finally:
        await client.close()

    assert body["client"]["client_secret_set"] is True
    assert body["client"]["client_id"] is None
    assert body["client"]["configured"] is False


@pytest.mark.asyncio
async def test_put_both_halves_at_once_configures(home):
    client = await _client()
    try:
        resp = await client.put(
            f"{BASE}/asana", json={"client_id": "asana-app", "client_secret": SECRET}
        )
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()

    assert body["client"]["slug"] == "asana"
    assert body["client"]["configured"] is True
    assert _client_id_record(home, "asana") == {"client_id": "asana-app"}
    assert SecretVault(home).list_names() == [ASANA_VAULT_NAME]


@pytest.mark.asyncio
async def test_put_clear_removes_the_secret_and_unconfigures(home):
    client = await _client()
    try:
        await client.put(
            f"{BASE}/github", json={"client_id": "Iv1.operator", "client_secret": SECRET}
        )
        resp = await client.put(f"{BASE}/github", json={"client_secret_clear": True})
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()

    assert body["client"]["client_secret_set"] is False
    assert body["client"]["client_secret_source"] is None
    assert body["client"]["configured"] is False
    assert body["client"]["client_id"] == "Iv1.operator"  # the id half is untouched
    assert SecretVault(home).list_names() == []


@pytest.mark.asyncio
async def test_put_clear_on_an_absent_secret_is_idempotent(home):
    client = await _client()
    try:
        resp = await client.put(f"{BASE}/github", json={"client_secret_clear": True})
        assert resp.status == 200
    finally:
        await client.close()
    assert SecretVault(home).list_names() == []


@pytest.mark.asyncio
async def test_put_response_reports_env_precedence_over_the_written_value(home, monkeypatch):
    """A stored id is written but an env value still outranks it in the record."""
    monkeypatch.setenv(GITHUB_ENV_ID, "Iv1.from-env")
    client = await _client()
    try:
        body = await (await client.put(f"{BASE}/github", json={"client_id": "Iv1.cfg"})).json()
    finally:
        await client.close()

    assert body["client"]["client_id"] == "Iv1.from-env"
    assert body["client"]["client_id_source"] == "env"
    assert _client_id_record(home, "github") == {"client_id": "Iv1.cfg"}


# ── DELETE ──


@pytest.mark.asyncio
async def test_delete_is_owner_only(home):
    SecretVault(home).set_sync(GITHUB_VAULT_NAME, SECRET)
    client = await _client()
    try:
        resp = await client.delete(f"{BASE}/github", headers=NON_OWNER)
        assert resp.status == 403
        assert (await resp.json())["code"] == "owner_only"
    finally:
        await client.close()
    assert SecretVault(home).list_names() == [GITHUB_VAULT_NAME]


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["notion", "not-a-provider"])
async def test_delete_rejects_anything_but_a_preregistered_provider(home, slug):
    client = await _client()
    try:
        resp = await client.delete(f"{BASE}/{slug}")
        assert resp.status == 400
        assert (await resp.json())["code"] == "unknown_provider"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_removes_both_halves_and_leaves_other_providers_alone(home):
    client = await _client()
    try:
        await client.put(
            f"{BASE}/github", json={"client_id": "Iv1.operator", "client_secret": SECRET}
        )
        await client.put(
            f"{BASE}/asana", json={"client_id": "asana-app", "client_secret": "asana-s"}
        )
        resp = await client.delete(f"{BASE}/github")
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()

    assert body["ok"] is True
    record = body["client"]
    assert record["slug"] == "github"
    assert record["client_id"] is None
    assert record["client_secret_set"] is False
    assert record["configured"] is False

    assert _client_id_record(home, "github") is None
    assert _client_id_record(home, "asana") == {"client_id": "asana-app"}
    assert SecretVault(home).list_names() == [ASANA_VAULT_NAME]


@pytest.mark.asyncio
async def test_delete_with_nothing_stored_is_idempotent(home):
    client = await _client()
    try:
        resp = await client.delete(f"{BASE}/github")
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()

    assert body["client"]["configured"] is False
    # No record to drop, so nothing was written.
    assert _client_id_record(home, "github") is None


@pytest.mark.asyncio
async def test_delete_does_not_touch_env_supplied_values(home, monkeypatch):
    """Environment values are not ours to delete; the record says where they come from."""
    monkeypatch.setenv(GITHUB_ENV_ID, "Iv1.from-env")
    monkeypatch.setenv(GITHUB_ENV_SECRET, SECRET)
    client = await _client()
    try:
        await client.put(
            f"{BASE}/github", json={"client_id": "Iv1.cfg", "client_secret": "vault-s"}
        )
        body = await (await client.delete(f"{BASE}/github")).json()
    finally:
        await client.close()

    record = body["client"]
    assert (record["client_id"], record["client_id_source"]) == ("Iv1.from-env", "env")
    assert (record["client_secret_set"], record["client_secret_source"]) == (True, "env")
    assert record["configured"] is True
    assert _client_id_record(home, "github") is None
    assert SecretVault(home).list_names() == []


# ── projections and atomicity ──


@pytest.mark.asyncio
async def test_every_mutation_refreshes_the_runtime_projections(home, refreshed):
    """A PUT (either half) and a DELETE each ask for the projection refresh, so a
    rotated or deleted client cannot keep authorizing through a stale agent spec or
    an in-flight mint minted from the old record."""
    client = await _client()
    try:
        await client.put(f"{BASE}/github", json={"client_id": "Iv1.operator"})
        await client.put(f"{BASE}/github", json={"client_secret": SECRET})
        await client.delete(f"{BASE}/github")
    finally:
        await client.close()

    assert refreshed == ["github", "github", "github"]


@pytest.mark.asyncio
async def test_a_failed_client_id_write_rolls_the_secret_back(home, monkeypatch):
    """Secret first, id second, and a failed id write restores the secret: the
    stored pair afterwards is the pair that existed BEFORE the request, never a new
    secret next to an old id."""
    vault = SecretVault(home)
    vault.set_sync(GITHUB_VAULT_NAME, "old-secret")

    async def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.run_config_write", _boom)

    client = await _client()
    try:
        resp = await client.put(
            f"{BASE}/github", json={"client_id": "Iv1.new", "client_secret": "new-secret"}
        )
        assert resp.status == 500
        body = await resp.json()
        assert body["code"] == "config_write_failed"
    finally:
        await client.close()

    held = vault.get(GITHUB_VAULT_NAME)
    assert held is not None and held.reveal() == "old-secret"
    assert _client_id_record(home, "github") is None


@pytest.mark.asyncio
async def test_a_failed_client_id_write_removes_a_secret_that_did_not_exist_before(
    home, monkeypatch
):
    """The rollback restores ABSENCE too: with no prior secret, the new one is
    deleted rather than left orphaned without its id."""

    async def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.run_config_write", _boom)

    client = await _client()
    try:
        resp = await client.put(
            f"{BASE}/github", json={"client_id": "Iv1.new", "client_secret": "new-secret"}
        )
        assert resp.status == 500
    finally:
        await client.close()

    assert SecretVault(home).list_names() == []


# ── the mint route honours the same predicate ──


@pytest.mark.asyncio
async def test_mint_refuses_an_unconfigured_preregistered_provider(home):
    """No client on record means nothing to authorize against: the mint route
    answers 409 ``client_not_configured`` instead of spawning a kiro-cli process
    that can only come back with the vendor's "unknown client" error."""
    app = web.Application()
    app.router.add_post("/api/connections/mint", connections.api_connections_mint)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/connections/mint", json={"slug": "github"})
        assert resp.status == 409
        body = await resp.json()
        assert body["code"] == "client_not_configured"
        assert body["slug"] == "github"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_failed_projection_refresh_is_reported_not_hidden(home, monkeypatch):
    """The record commits (the vault is the source of truth and reverting it would
    keep the retired credential authoritative), but the response says the agent
    spec did not follow, so the operator does not assume the rotation took."""

    async def _broken(slug: str) -> bool:
        return False

    monkeypatch.setattr(connections, "_refresh_client_projections", _broken)

    client = await _client()
    try:
        resp = await client.put(f"{BASE}/github", json={"client_secret": SECRET})
        assert resp.status == 500
        body = await resp.json()
        assert body["code"] == "projection_refresh_failed"
        assert body["client"]["client_secret_set"] is True
        assert SECRET not in json.dumps(body)
    finally:
        await client.close()

    # Committed regardless.
    held = SecretVault(home).get(GITHUB_VAULT_NAME)
    assert held is not None and held.reveal() == SECRET


@pytest.mark.asyncio
async def test_delete_restores_the_secret_when_the_config_drop_fails(home, monkeypatch):
    """DELETE mirrors the PUT: vault first, config second, and a failed config write
    puts the secret back so the pair on disk is the pair from before the request."""
    vault = SecretVault(home)
    vault.set_sync(GITHUB_VAULT_NAME, "keep-me")

    async def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.run_config_write", _boom)

    client = await _client()
    try:
        resp = await client.delete(f"{BASE}/github")
        assert resp.status == 500
        assert (await resp.json())["code"] == "config_write_failed"
    finally:
        await client.close()

    held = vault.get(GITHUB_VAULT_NAME)
    assert held is not None and held.reveal() == "keep-me"


@pytest.mark.asyncio
async def test_rollback_leaves_a_secret_someone_else_wrote_in_between(home, monkeypatch):
    """The rollback is scoped to THIS request's write: if the vault entry changed
    underneath (a Secrets-panel write), the newer value stays rather than being
    replaced with the request's stale snapshot."""
    vault = SecretVault(home)
    vault.set_sync(GITHUB_VAULT_NAME, "old-secret")

    async def _boom_after_side_write(*_args, **_kwargs):
        # Simulate a concurrent writer landing between the vault write and the
        # config write of the request under test.
        vault.set_sync(GITHUB_VAULT_NAME, "newer-from-elsewhere")
        raise OSError("disk full")

    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.run_config_write", _boom_after_side_write)

    client = await _client()
    try:
        resp = await client.put(
            f"{BASE}/github", json={"client_id": "Iv1.new", "client_secret": "mine"}
        )
        assert resp.status == 500
    finally:
        await client.close()

    held = vault.get(GITHUB_VAULT_NAME)
    assert held is not None and held.reveal() == "newer-from-elsewhere"
