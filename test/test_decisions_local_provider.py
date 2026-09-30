"""Local System One models: the preset catalog, the loopback test, and the provider route.

Three properties are pinned here:

* only a LITERAL loopback address counts as local, because the client withholds the
  Jev key from a local endpoint and a name or a user-info trick must not earn that;
* the provider route writes an endpoint it BUILT, from a preset id and a port, and
  refuses every body that could smuggle a URL in;
* a switch carries a standing consent across only to a local endpoint -- switching
  back to hosted Jev, the direction that starts egress, never inherits one.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config.sections import DECISION_PROVIDER_ENDPOINT_DEFAULT as DEFAULT_ENDPOINT
from kiro_crew.config.sections import DECISION_PROVIDER_MODEL_DEFAULT as DEFAULT_MODEL
from kiro_crew.decisions import consent, local_models

# ---------------------------------------------------------------------------
# The loopback test
# ---------------------------------------------------------------------------


class TestIsLoopbackEndpoint:
    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://127.0.0.1:8102/v1/systemone",
            "http://127.9.9.9:8102/v1/systemone",
            "https://127.0.0.1:8443/v1/systemone",
            "http://[::1]:8104/v1/systemone",
            "http://127.0.0.1/v1/systemone",
            "  http://127.0.0.1:8102/v1/systemone  ",
        ],
    )
    def test_a_literal_loopback_address_is_local(self, endpoint):
        assert local_models.is_loopback_endpoint(endpoint) is True

    @pytest.mark.parametrize(
        "endpoint",
        [
            DEFAULT_ENDPOINT,
            "http://localhost:8102/v1/systemone",
            "http://127.0.0.1:8102@evil.example:80/v1/systemone",
            "http://user@127.0.0.1:8102/v1/systemone",
            "ftp://127.0.0.1:8102/v1/systemone",
            "http://10.0.0.5:8102/v1/systemone",
            "http://0.0.0.0:8102/v1/systemone",
            "http://127.0.0.1:99999/v1/systemone",
            "127.0.0.1:8102",
            "",
            None,
            8102,
        ],
        ids=[
            "hosted-jev",
            "a-name",
            "userinfo-host-swap",
            "userinfo",
            "not-http",
            "private-not-loopback",
            "any-address",
            "port-out-of-range",
            "no-scheme",
            "empty",
            "none",
            "not-a-string",
        ],
    )
    def test_anything_else_is_not(self, endpoint):
        assert local_models.is_loopback_endpoint(endpoint) is False


class TestEndpointFor:
    def test_builds_the_systemone_url_on_ipv4_loopback(self):
        assert local_models.endpoint_for(8102) == "http://127.0.0.1:8102/v1/systemone"

    @pytest.mark.parametrize("port", [0, 80, 1023, 65536, -1, True, "8102", 8102.0, None])
    def test_refuses_a_port_outside_the_range_or_not_an_int(self, port):
        with pytest.raises(ValueError):
            local_models.endpoint_for(port)

    def test_every_built_endpoint_is_local(self):
        for m in local_models.LOCAL_MODELS:
            assert local_models.is_loopback_endpoint(local_models.endpoint_for(m.default_port))


class TestCatalog:
    def test_ids_are_unique_and_none_is_the_hosted_preset(self):
        ids = [m.id for m in local_models.LOCAL_MODELS]
        assert len(ids) == len(set(ids))
        assert local_models.PRESET_JEV not in ids

    def test_thresholds_descend_so_the_first_met_is_the_largest(self):
        """The card recommends the FIRST preset whose threshold the machine meets."""
        thresholds = [m.recommended_total_ram_gb for m in local_models.LOCAL_MODELS]
        assert thresholds == sorted(thresholds, reverse=True)

    def test_a_threshold_leaves_headroom_over_the_measured_peak(self):
        for m in local_models.LOCAL_MODELS:
            assert m.recommended_total_ram_gb > m.peak_ram_gb

    def test_every_model_id_is_one_the_client_will_send(self):
        from kiro_crew.decisions.types import is_model_id

        for m in local_models.LOCAL_MODELS:
            assert is_model_id(m.model), m.model

    def test_every_serve_command_names_the_port(self):
        for m in local_models.LOCAL_MODELS:
            assert "{port}" in m.serve_command

    def test_the_quality_numbers_are_the_measured_ratios(self):
        """Correct answers matched against Jev's 200 of 231 (81 of 111 on hard)."""
        by_id = {m.id: m for m in local_models.LOCAL_MODELS}
        assert by_id["plumb-4b"].jev_relative_pct == round(100 * 206 / 200)
        assert by_id["plumb-4b"].hard_relative_pct == round(100 * 88 / 81)
        assert by_id["laya"].jev_relative_pct == round(100 * 134 / 200)
        assert by_id["laya"].hard_relative_pct == round(100 * 38 / 81)

    def test_get_accepts_only_a_known_id(self):
        assert local_models.get("laya").id == "laya"
        assert local_models.get("nope") is None
        assert local_models.get(None) is None


class TestActiveId:
    def test_the_hosted_default_is_jev(self):
        assert local_models.active_id(DEFAULT_ENDPOINT, DEFAULT_MODEL) == "jev"

    def test_a_local_endpoint_with_a_preset_model_is_that_preset(self):
        endpoint = local_models.endpoint_for(9000)
        assert local_models.active_id(endpoint, "plumb-4b") == "plumb-4b"
        assert local_models.active_id(endpoint, "english") == "laya"

    def test_anything_else_is_custom(self):
        assert local_models.active_id("https://proxy.example/v1/systemone", "x") == "custom"
        assert local_models.active_id(local_models.endpoint_for(9000), "other") == "custom"


# ---------------------------------------------------------------------------
# The provider route
# ---------------------------------------------------------------------------


def _request(*, app: str = "", user: str = "owner-1", owner: str = "owner-1", body=None):
    """A request shaped like a real DASHBOARD OWNER call (see test_decisions_consent.py)."""
    req = MagicMock()
    req.path = "/api/decisions/provider"
    store = {"app": app, "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = owner
    req.app = {"state": state}
    req.json = AsyncMock(return_value=body if body is not None else {})
    return req


@pytest.fixture
def audit(monkeypatch):
    import kiro_crew.dashboard.handlers as handlers_pkg

    rows: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: rows.append(kw)
    monkeypatch.setattr(handlers_pkg, "sel", lambda: fake)
    return rows


@pytest.fixture
def keystone(tmp_path, monkeypatch):
    path = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)
    return path


@pytest.fixture
def config_file():
    """The config.json the pinned data home resolves to, and a reader for its provider."""
    from kiro_crew.config.loader import config_path

    path = config_path()

    def _provider() -> dict:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return data.get("decisions", {}).get("provider", {})

    return path, _provider


@pytest.fixture
def not_denied(monkeypatch):
    from kiro_crew.decisions import capability

    monkeypatch.setattr(capability, "is_decisions_denied", lambda *a, **k: False)


class TestProviderRouteOwnerOnly:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb", ["get", "put"])
    async def test_a_non_owner_is_refused(self, verb, audit, config_file):
        from kiro_crew.dashboard.handlers import decisions as mod

        handler = (
            mod.api_decisions_provider_get if verb == "get" else mod.api_decisions_provider_put
        )
        resp = await handler(_request(user="someone-else", body={"preset": "laya"}))
        assert resp.status == 403
        assert config_file[1]() == {}, "a refused write wrote nothing"

    @pytest.mark.asyncio
    async def test_an_app_token_is_refused(self, audit, config_file):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        resp = await api_decisions_provider_put(_request(app="some-app", body={"preset": "laya"}))
        assert resp.status == 403


class TestProviderRouteGet:
    @pytest.mark.asyncio
    async def test_lists_every_preset_and_reports_hosted_jev_by_default(self, audit):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_get

        resp = await api_decisions_provider_get(_request())
        payload = json.loads(resp.text)
        assert resp.status == 200
        assert [p["id"] for p in payload["presets"]] == [m.id for m in local_models.LOCAL_MODELS]
        assert payload["active"] == "jev"
        assert payload["configured_endpoint"] == DEFAULT_ENDPOINT


class TestProviderRoutePut:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"preset": "laya", "endpoint": "https://evil.example/v1/systemone"},
            {"preset": "laya", "url": "http://127.0.0.1:1/v1/systemone"},
            {"preset": "jev", "port": 8102},
            {"preset": "laya", "port": 80},
            {"preset": "laya", "port": "8104"},
            {"preset": "laya", "port": True},
            {"preset": "nope"},
            {"preset": None},
            {},
            ["laya"],
            "laya",
        ],
        ids=[
            "an-endpoint",
            "a-url",
            "port-on-hosted",
            "privileged-port",
            "string-port",
            "bool-port",
            "unknown-preset",
            "null-preset",
            "empty",
            "a-list",
            "a-string",
        ],
    )
    async def test_every_body_that_is_not_a_preset_and_port_is_refused(
        self, body, audit, config_file
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        resp = await api_decisions_provider_put(_request(body=body))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "decisions_provider_invalid_body"
        assert config_file[1]() == {}

    @pytest.mark.asyncio
    async def test_a_local_preset_writes_the_endpoint_it_built(
        self, audit, config_file, keystone, not_denied
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        resp = await api_decisions_provider_put(_request(body={"preset": "plumb-4b", "port": 9001}))
        assert resp.status == 200
        assert config_file[1]() == {
            "endpoint": "http://127.0.0.1:9001/v1/systemone",
            "model": "plumb-4b",
            "timeout_ms": local_models.get("plumb-4b").timeout_ms,
        }
        assert json.loads(resp.text)["active"] == "plumb-4b"

    @pytest.mark.asyncio
    async def test_the_port_defaults_to_the_presets_own(
        self, audit, config_file, keystone, not_denied
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        await api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert config_file[1]()["endpoint"] == local_models.endpoint_for(
            local_models.get("laya").default_port
        )

    @pytest.mark.asyncio
    async def test_the_api_key_reference_is_left_as_it_was(
        self, audit, config_file, keystone, not_denied
    ):
        from kiro_crew.config.loader import update_config_locked
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        path, provider = config_file

        def _seed(data):
            data.setdefault("decisions", {})["provider"] = {"api_key": "secret://TYPESAFE_API_KEY"}
            return data

        update_config_locked(path, mutate=_seed)
        await api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert provider()["api_key"] == "secret://TYPESAFE_API_KEY"

    @pytest.mark.asyncio
    async def test_hosted_jev_restores_the_shipped_defaults(
        self, audit, config_file, keystone, not_denied
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        await api_decisions_provider_put(_request(body={"preset": "laya"}))
        resp = await api_decisions_provider_put(_request(body={"preset": "jev"}))
        assert resp.status == 200
        assert config_file[1]() == {
            "endpoint": DEFAULT_ENDPOINT,
            "model": DEFAULT_MODEL,
            "timeout_ms": 1000,
        }

    @pytest.mark.asyncio
    async def test_the_write_is_audited_with_the_endpoint(
        self, audit, config_file, keystone, not_denied
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        await api_decisions_provider_put(_request(body={"preset": "laya"}))
        row = [r for r in audit if r["operation"] == "decisions_provider_put"][-1]
        assert row["outcome"] == "allowed"
        assert "endpoint=http://127.0.0.1:8104/v1/systemone" in row["resources"]


class TestConsentCarry:
    def _consent(self, keystone, endpoint):
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": endpoint, "tool_args": True}),
            encoding="utf-8",
        )

    @pytest.mark.asyncio
    async def test_a_standing_consent_follows_a_switch_to_a_local_preset(
        self, audit, config_file, keystone, not_denied
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        self._consent(keystone, DEFAULT_ENDPOINT)
        resp = await api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert json.loads(resp.text)["consent_carried"] is True
        state = consent.load_state()
        assert consent.consented_endpoint(state) == local_models.endpoint_for(8104)
        assert consent.consented_tool_args(state) is True, "a recorded scope is kept"

    @pytest.mark.asyncio
    async def test_switching_back_to_hosted_jev_never_inherits_consent(
        self, audit, config_file, keystone, not_denied
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        local = local_models.endpoint_for(8104)
        self._consent(keystone, local)
        resp = await api_decisions_provider_put(_request(body={"preset": "jev"}))
        assert json.loads(resp.text)["consent_carried"] is False
        assert consent.consented_endpoint(consent.load_state()) == local
        assert consent.permits(DEFAULT_ENDPOINT, consent.load_state()) is False

    @pytest.mark.asyncio
    async def test_no_consent_is_created_where_none_stood(
        self, audit, config_file, keystone, not_denied
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        resp = await api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert json.loads(resp.text)["consent_carried"] is False
        assert consent.is_enabled(consent.load_state()) is False

    @pytest.mark.asyncio
    async def test_a_failed_carry_still_reports_the_saved_provider(
        self, audit, config_file, keystone, not_denied, monkeypatch
    ):
        """The provider is written before consent is carried, so a keystone write
        that fails must not turn a saved switch into a "nothing changed" 500."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        self._consent(keystone, DEFAULT_ENDPOINT)

        def _refuse(*_a, **_k):
            raise consent.ConsentCorruptError("unreadable")

        monkeypatch.setattr(consent, "save_enabled", _refuse)
        resp = await api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert resp.status == 200
        payload = json.loads(resp.text)
        assert payload["active"] == "laya"
        assert payload["consent_carried"] is False
        assert config_file[1]()["endpoint"] == local_models.endpoint_for(8104)
        assert consent.consented_endpoint(consent.load_state()) == DEFAULT_ENDPOINT
        errors = [r["error"] for r in audit if r["outcome"] == "error"]
        assert errors == ["consent_carry:ConsentCorruptError"]

    @pytest.mark.asyncio
    async def test_a_fleet_denial_stops_the_carry(self, audit, config_file, keystone, monkeypatch):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put
        from kiro_crew.decisions import capability

        monkeypatch.setattr(capability, "is_decisions_denied", lambda *a, **k: True)
        self._consent(keystone, DEFAULT_ENDPOINT)
        resp = await api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert json.loads(resp.text)["consent_carried"] is False
        assert consent.consented_endpoint(consent.load_state()) == DEFAULT_ENDPOINT
