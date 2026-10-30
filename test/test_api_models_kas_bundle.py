"""``GET /api/models`` on the KAS bundle route spawns no kiro-cli.

With ``agent.acp_backend == "kas"`` and ``agent.kas_bundle`` on, the picker is
served from the engine's own ``model`` select (recorded by the session drivers
into ``agent_sdk.kas_model_catalog``), gated on the bundle's readiness (installed
engine + Crew's vault), with the select's ``rateMultiplier`` as the credit
multiplier and a context window only where a source knows one, and without the
kiro-cli catalog's entitlement narrowing. The kiro-cli route is dispatched exactly
as before: its readiness gate still runs and the bundle handler is never reached.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import model_registry
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk import kas_model_catalog as catalog
from kiro_crew.auth.service import KasLoginService
from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.dashboard.handlers import agents
from kiro_crew.dashboard.handlers import models_kas_bundle as bundle


@pytest.fixture(autouse=True)
def _isolated_registry(_floor_monkeypatch):
    # The floor's own MonkeyPatch (D11): a test's ``monkeypatch.undo()`` cannot
    # lift these, so every test starts cold and no registry write reaches disk.
    _floor_monkeypatch.setattr(catalog, "_recorded", None)
    _floor_monkeypatch.setattr(catalog, "_recorded_identity", "")
    _floor_monkeypatch.setattr(model_registry, "_CATALOG_RATES", {})
    _floor_monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
    # The advertised store persists to disk when it changes; never from a test.
    _floor_monkeypatch.setattr(model_registry, "persist_advertised_models", lambda: None)
    yield
    _floor_monkeypatch.setattr(catalog, "_recorded", None)


@pytest.fixture
def ready(monkeypatch):
    """Engine installed and the vault signed in: the gate passes."""
    monkeypatch.setattr(bundle, "_bundle_installed", lambda: True)
    monkeypatch.setattr(bundle, "_vault_signed_in", lambda: True)
    monkeypatch.setattr(catalog, "vault_identity", lambda: _ID)


#: The vault identity fingerprint the ``ready`` vault names, and ``_record`` stamps.
_ID = "fingerprint-of-account-a"


def _recorded() -> list[dict] | None:
    """What the account ``_ID`` would be served."""
    return catalog.recorded_catalog_snapshot(_ID)


def _cfg(backend: str, kas_bundle: bool) -> SimpleNamespace:
    return SimpleNamespace(
        agent=SimpleNamespace(provider="acp", acp_backend=backend, kas_bundle=kas_bundle, model="")
    )


def _request() -> MagicMock:
    request = MagicMock()
    # No live providers: ``_entitled_kiro_models`` fails open and returns the rows.
    request.app = {}
    return request


def _body(resp: web.Response) -> Any:
    return json.loads(resp.body)


def _record(*rows: dict, identity: str = _ID) -> None:
    options = []
    for row in rows:
        option = {"value": row["id"], "name": row.get("name", row["id"]), "description": ""}
        if "rate" in row:
            option["_meta"] = {"kiro": {"rateMultiplier": row["rate"]}}
        options.append(option)
    assert catalog.record_catalog(
        {"configOptions": [{"id": "model", "type": "select", "options": options}]},
        ACP_BACKEND_KAS,
        identity=identity,
    )


@pytest.mark.asyncio
class TestReadiness:
    async def test_no_installed_engine_is_a_coded_503(self, monkeypatch):
        monkeypatch.setattr(bundle, "_bundle_installed", lambda: False)
        monkeypatch.setattr(bundle, "_vault_signed_in", lambda: True)
        resp = await bundle.api_models_kas_bundle(_request())
        assert resp.status == 503
        assert _body(resp)["code"] == bundle.KAS_BUNDLE_NOT_INSTALLED_CODE

    async def test_a_signed_out_vault_is_a_coded_503(self, monkeypatch):
        monkeypatch.setattr(bundle, "_bundle_installed", lambda: True)
        monkeypatch.setattr(bundle, "_vault_signed_in", lambda: False)
        resp = await bundle.api_models_kas_bundle(_request())
        assert resp.status == 503
        body = _body(resp)
        assert body["code"] == bundle.KAS_BUNDLE_SIGN_IN_REQUIRED_CODE
        # Not kiro-cli's code: that one renders a ``kiro-cli login`` remedy.
        assert body["code"] != agents.KIRO_PREREQUISITE_REQUIRED_CODE


@pytest.mark.asyncio
class TestTheList:
    async def test_cold_is_auto_alone_with_200(self, ready):
        resp = await bundle.api_models_kas_bundle(_request())
        assert resp.status == 200
        (row,) = _body(resp)
        assert row["model_name"] == "auto"
        assert row["context_window"] > 0

    async def test_recorded_rows_carry_the_multiplier_and_only_known_windows(self, ready):
        _record({"id": "auto", "name": "Auto", "rate": 1}, {"id": "model-x", "rate": 2})
        known_windows = {"model-x": None}

        def _window(model_id: str, *, live_tokens=None):
            del live_tokens
            return known_windows.get(model_id, 1_000_000)

        with patch.object(model_registry, "model_window", _window):
            resp = await bundle.api_models_kas_bundle(_request())
        assert resp.status == 200
        rows = _body(resp)
        assert [r["model_name"] for r in rows] == ["auto", "model-x"]
        assert rows[0]["rate_multiplier"] == 1.0
        assert rows[1]["rate_multiplier"] == 2.0
        assert rows[0]["context_window"] == 1_000_000
        # No source knows model-x's window: the field is omitted, never invented.
        assert "context_window" not in rows[1]

    async def test_a_row_without_a_price_has_no_multiplier_field(self, ready):
        _record({"id": "auto"})
        (row,) = _body(await bundle.api_models_kas_bundle(_request()))
        assert "rate_multiplier" not in row

    async def test_an_account_switch_during_the_persist_await_turns_the_answer_cold(
        self, ready, monkeypatch
    ):
        """The handler snapshots the rows, then awaits the advertised-store persist.
        A sign-out or sign-in landing in that await -- from any process -- makes the
        snapshot the previous account's, so it must not be served for the dashboard
        to cache again: the vault is read again after the await."""
        _record({"id": "auto", "rate": 1}, {"id": "model-a", "rate": 2})

        def _persist_then_switch() -> None:
            monkeypatch.setattr(catalog, "vault_identity", lambda: "fingerprint-of-account-b")

        with patch.object(model_registry, "persist_advertised_models", _persist_then_switch):
            resp = await bundle.api_models_kas_bundle(_request())
        assert resp.status == 200
        (row,) = _body(resp)
        assert row["model_name"] == "auto"

    async def test_an_unchanged_account_serves_the_snapshot(self, ready):
        _record({"id": "auto", "rate": 1}, {"id": "model-a", "rate": 2})
        persisted = []
        with patch.object(model_registry, "persist_advertised_models", lambda: persisted.append(1)):
            resp = await bundle.api_models_kas_bundle(_request())
        assert persisted == [1]
        assert [r["model_name"] for r in _body(resp)] == ["auto", "model-a"]

    async def test_the_rows_feed_the_acp_vocabulary_and_the_prices(self, ready):
        _record({"id": "auto", "rate": 1}, {"id": "model-x", "rate": 2})
        await bundle.api_models_kas_bundle(_request())
        namespace = agents.model_registry_namespace(ACP_BACKEND_KAS)
        assert set(model_registry.advertised_models(namespace)) >= {"auto", "model-x"}
        assert model_registry.rate_multiplier("model-x", namespace) == 2.0


@pytest.mark.asyncio
class TestDispatchFromApiModels:
    async def test_the_bundle_route_never_reaches_the_kiro_cli_gate_spawn_or_narrowing(self, ready):
        """The engine's select is the signed-in account's own list, so the kiro-cli
        catalog narrowing never runs here: after a hot switch of the backend it
        would read a RETAINED kiro-cli session's (possibly ``auto``-only) list and
        hide models the bundle account is served."""
        _record({"id": "auto", "rate": 1}, {"id": "model-x", "rate": 2})

        async def _gate_must_not_run(*args, **kwargs):
            raise AssertionError("reject_if_kiro_unverified ran on the bundle route")

        async def _spawn_must_not_run():
            raise AssertionError("kiro-cli --list-models spawned on the bundle route")

        async def _narrowing_must_not_run(request, rows):
            raise AssertionError("_entitled_kiro_models ran on the bundle route")

        with (
            patch.object(agents.KiroCrewConfig, "load", return_value=_cfg(ACP_BACKEND_KAS, True)),
            patch.object(agents, "reject_if_kiro_unverified", _gate_must_not_run),
            patch.object(agents, "_fetch_kiro_catalog", _spawn_must_not_run),
            patch.object(agents, "_entitled_kiro_models", _narrowing_must_not_run),
        ):
            resp = await agents.api_models(_request())
        assert resp.status == 200
        assert [r["model_name"] for r in _body(resp)] == ["auto", "model-x"]

    async def test_kas_through_kiro_cli_still_takes_the_kiro_cli_gate(self):
        """The relay route is unchanged: the gate runs before any catalog read."""
        sentinel = web.json_response({"error": "gate", "code": "gate"}, status=503)

        async def _gate(*args, **kwargs):
            return sentinel

        with (
            patch.object(agents.KiroCrewConfig, "load", return_value=_cfg(ACP_BACKEND_KAS, False)),
            patch.object(agents, "reject_if_kiro_unverified", _gate),
        ):
            resp = await agents.api_models(_request())
        assert resp is sentinel

    async def test_the_kiro_backend_with_the_flag_on_still_takes_the_kiro_cli_gate(self):
        sentinel = web.json_response({"error": "gate", "code": "gate"}, status=503)

        async def _gate(*args, **kwargs):
            return sentinel

        with (
            patch.object(agents.KiroCrewConfig, "load", return_value=_cfg(ACP_BACKEND_KIRO, True)),
            patch.object(agents, "reject_if_kiro_unverified", _gate),
        ):
            resp = await agents.api_models(_request())
        assert resp is sentinel


@pytest.mark.asyncio
class TestTheRecordingFollowsTheVault:
    """A select is what ONE account is served. The recording carries the vault
    identity its request went out under and the picker serves it only while the
    vault still names that account -- read from the vault itself, so it holds for
    a sign-in written by the dashboard's service and for ``kirocrew kas login`` /
    ``kas logout`` in a shell (another process, the same vault) alike."""

    @staticmethod
    def _token(provider: str, identity: str) -> KasToken:
        return KasToken(
            access_token="at",
            # A fixed instant far ahead: live for the whole test, no clock read.
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            provider=provider,
            identity=identity,
            refresh_token="rt",
            # A social or IdC entry without one reads back as nothing
            # (``KasToken.is_storable``).
            profile_arn="arn:aws:codewhisperer:us-east-1:1:profile/x",
        )

    @pytest.fixture
    def vault(self, tmp_path, monkeypatch) -> TokenStore:
        """A real vault the handler reads through ``auth.bridge``, engine installed."""
        from kiro_crew.auth import bridge

        store = TokenStore(tmp_path)
        monkeypatch.setattr(bridge, "default_token_store", lambda: store)
        monkeypatch.setattr(bundle, "_bundle_installed", lambda: True)
        return store

    async def _record_under_the_vault(self) -> None:
        identity = await asyncio.to_thread(catalog.vault_identity)
        assert identity
        _record({"id": "auto", "rate": 1}, {"id": "model-a", "rate": 2}, identity=identity)

    async def test_an_account_switch_turns_the_picker_cold(self, vault):
        vault.save(self._token("Google", "social"))
        await self._record_under_the_vault()
        rows = _body(await bundle.api_models_kas_bundle(_request()))
        assert [r["model_name"] for r in rows] == ["auto", "model-a"]
        # The service ``kirocrew kas logout`` / ``kas login`` build, in this test's
        # process: nothing tells the picker, the vault's identity is what it reads.
        shell = KasLoginService(vault)
        await shell.logout("social")
        result = await shell._save_token(self._token("BuilderId", "builder_id"))
        assert result["status"] == "authorized"
        (row,) = _body(await bundle.api_models_kas_bundle(_request()))
        assert row["model_name"] == "auto"
        assert "rate_multiplier" not in row

    async def test_a_sign_out_answers_sign_in_required(self, vault):
        vault.save(self._token("Google", "social"))
        await self._record_under_the_vault()
        await KasLoginService(vault).logout("social")
        resp = await bundle.api_models_kas_bundle(_request())
        assert resp.status == 503
        assert _body(resp)["code"] == bundle.KAS_BUNDLE_SIGN_IN_REQUIRED_CODE

    async def test_a_shell_sign_in_over_an_outranked_slot_turns_the_picker_cold(self, vault):
        """A second sign-in written without a sign-out first: the vault now
        resolves the higher-priority slot, which is another account."""
        vault.save(self._token("Google", "social"))
        await self._record_under_the_vault()
        await KasLoginService(vault)._save_token(self._token("BuilderId", "builder_id"))
        (row,) = _body(await bundle.api_models_kas_bundle(_request()))
        assert row["model_name"] == "auto"

    async def test_the_same_account_keeps_its_rows(self, vault):
        vault.save(self._token("Google", "social"))
        await self._record_under_the_vault()
        # A refresh rewrites the token, not the account: the fingerprint holds.
        vault.save(self._token("Google", "social"))
        rows = _body(await bundle.api_models_kas_bundle(_request()))
        assert [r["model_name"] for r in rows] == ["auto", "model-a"]
