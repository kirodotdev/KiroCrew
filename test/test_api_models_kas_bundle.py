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

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from test_kas_login_api import _FakeRequest

from kiro_crew import model_registry
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk import kas_model_catalog as catalog
from kiro_crew.auth.service import KasLoginService
from kiro_crew.auth.store import KasToken, TokenStore, TokenStoreError
from kiro_crew.dashboard.handlers import agents, kas_login
from kiro_crew.dashboard.handlers import models_kas_bundle as bundle


@pytest.fixture(autouse=True)
def _isolated_registry(_floor_monkeypatch):
    # The floor's own MonkeyPatch (D11): a test's ``monkeypatch.undo()`` cannot
    # lift these, so every test starts cold and no registry write reaches disk.
    _floor_monkeypatch.setattr(catalog, "_recorded", None)
    _floor_monkeypatch.setattr(catalog, "_generation", 0)
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


def _record(*rows: dict) -> None:
    options = []
    for row in rows:
        option = {"value": row["id"], "name": row.get("name", row["id"]), "description": ""}
        if "rate" in row:
            option["_meta"] = {"kiro": {"rateMultiplier": row["rate"]}}
        options.append(option)
    assert catalog.record_catalog(
        {"configOptions": [{"id": "model", "type": "select", "options": options}]},
        ACP_BACKEND_KAS,
        generation=catalog.sign_in_generation(),
    )


class TestTheRoutePredicate:
    @pytest.mark.parametrize(
        ("backend", "flag", "expected"),
        [
            (ACP_BACKEND_KAS, True, True),
            (ACP_BACKEND_KAS, False, False),
            (ACP_BACKEND_KIRO, True, False),
            ("codex", True, False),
        ],
    )
    def test_both_facts_are_needed(self, backend, flag, expected):
        assert bundle.kas_bundle_route(_cfg(backend, flag)) is expected

    def test_a_config_without_an_agent_section_is_not_the_route(self):
        assert bundle.kas_bundle_route(SimpleNamespace()) is False


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
class TestTheRecordingFollowsTheSignIn:
    """A select is what ONE account is served. The vault's only writer, the login
    service, drops the recording the moment the identity changes -- right after a
    credential is saved and right after a sign-out's deletes, under its own lock
    with no await between -- so account B is never offered account A's models and
    prices, and a runtime that launched under A and answers late is refused."""

    @staticmethod
    def _token() -> KasToken:
        return KasToken(
            access_token="at",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            provider="Google",
            identity="social",
        )

    async def test_a_saved_credential_forgets_the_recording_and_moves_the_generation(
        self, tmp_path
    ):
        service = KasLoginService(TokenStore(tmp_path), on_identity_change=catalog.forget_catalog)
        _record({"id": "auto"}, {"id": "model-a"})
        before = catalog.sign_in_generation()
        result = await service._save_token(self._token())
        assert result["status"] == "authorized"
        assert catalog.recorded_catalog() is None
        assert catalog.sign_in_generation() == before + 1

    async def test_a_sign_out_forgets_the_recording(self, tmp_path, ready):
        store = TokenStore(tmp_path)
        store.save(self._token())
        service = KasLoginService(store, on_identity_change=catalog.forget_catalog)
        _record({"id": "auto"}, {"id": "model-a"})
        await service.logout("social")
        assert catalog.recorded_catalog() is None
        (row,) = _body(await bundle.api_models_kas_bundle(_request()))
        assert row["model_name"] == "auto"

    async def test_a_failed_save_changes_nothing(self, tmp_path):
        class _Unwritable(TokenStore):
            def save(self, token, *, hold_refresh_lock=True):
                raise TokenStoreError("disk full")

        service = KasLoginService(_Unwritable(tmp_path), on_identity_change=catalog.forget_catalog)
        _record({"id": "auto"})
        before = catalog.sign_in_generation()
        result = await service._save_token(self._token())
        assert result["code"] == "token_store_failed"
        assert catalog.recorded_catalog() is not None
        assert catalog.sign_in_generation() == before

    async def test_a_hook_that_raises_does_not_undo_the_sign_in(self, tmp_path):
        def _explode() -> None:
            raise RuntimeError("hook exploded")

        service = KasLoginService(TokenStore(tmp_path), on_identity_change=_explode)
        result = await service._save_token(self._token())
        assert result["status"] == "authorized"

    def test_the_dashboard_wires_the_forget_into_its_service(self):
        service = kas_login._service(_FakeRequest(None, {}))
        assert service is not None
        assert service._on_identity_change is catalog.forget_catalog
