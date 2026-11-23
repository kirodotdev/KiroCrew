"""Choosing an HTTPS endpoint signed with AWS SigV4 as the decision model, from the Decisions card.

Pinned here:

* the provider route BUILDS the ``aws-https://`` address from checked fields and
  refuses every body that could add anything else to it;
* the write is owner-only and audited, and it carries no consent to the new address;
* the GET names a configured endpoint by its parts, and the option has its own
  allow-list entry;
* the ceiling's ``endpoints`` list is asked about the ``https://`` URL the request reaches;
* the connection test sends one fixed question through the decision client and
  reports a fixed word.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config.sections import DECISION_PROVIDER_ENDPOINT_DEFAULT as DEFAULT_ENDPOINT
from kiro_crew.config.sections import DECISION_PROVIDER_MODEL_DEFAULT as DEFAULT_MODEL
from kiro_crew.decisions import consent, impl_signed_https

URL = "https://decider.example.com/v1/decisions"
GOOD = {
    "preset": "aws_https",
    "url": URL,
    "service": "execute-api",
    "region": "us-west-2",
    "model": "decider-model-v2",
}
GOOD_ADDRESS = "aws-https://decider.example.com/v1/decisions?service=execute-api&region=us-west-2"


def _request(*, app: str = "", user: str = "owner-1", owner: str = "owner-1", body=None):
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
def provider_config():
    from kiro_crew.config.loader import config_path

    path = config_path()

    def _provider() -> dict:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return data.get("decisions", {}).get("provider", {})

    return _provider


@pytest.fixture(autouse=True)
def runtime(_floor_monkeypatch):
    from kiro_crew.decisions import local_runtime

    calls: list = []
    fake = MagicMock()
    fake.status = lambda: {
        "preset": "",
        "state": "idle",
        "port": 0,
        "bytes_done": 0,
        "bytes_total": 0,
        "error": "",
    }
    fake.installed_ids = lambda: []
    fake.activate = lambda m, port: calls.append(("activate", m.id))
    fake.deactivate = lambda **_: calls.append(("deactivate",))
    _floor_monkeypatch.setattr(local_runtime, "get_runtime", lambda: fake)
    return calls


@pytest.fixture(autouse=True)
def aws_profiles(tmp_path, _floor_monkeypatch):
    config = tmp_path / "aws-config"
    config.write_text("[default]\nregion = us-west-2\n\n[profile research]\n", encoding="utf-8")
    _floor_monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    _floor_monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))


@pytest.fixture
def not_denied(monkeypatch):
    from kiro_crew.decisions import capability

    monkeypatch.setattr(capability, "is_decisions_denied", lambda *a, **k: False)
    monkeypatch.setattr(capability, "targets_denied", lambda *a, **k: False)


def _seed_endpoint(endpoint: str, **extra) -> None:
    from kiro_crew.config.loader import config_path, update_config_locked

    def _seed(data):
        data["decisions"] = {"provider": {"endpoint": endpoint, **extra}}
        return data

    update_config_locked(config_path(), mutate=_seed)


def _ceiling_policy(monkeypatch, scopes: dict) -> None:
    """A real parsed ceiling whose ``capabilities.decisions`` row carries *scopes*."""
    from types import SimpleNamespace

    from kiro_crew.platform import context as pc
    from kiro_crew.platform.governance import parse_policy

    ceiling = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "capabilities": {"decisions": {"enabled": True, "scopes": scopes}},
        }
    )
    monkeypatch.setattr(pc, "current_context", lambda: SimpleNamespace(governance=ceiling))


async def _put(body, **kw):
    from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

    resp = await api_decisions_provider_put(_request(body=body, **kw))
    return resp.status, json.loads(resp.text)


class TestPut:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("patch", "field"),
        [
            ({"url": "http://decider.example.com/v1/decisions"}, "url"),
            ({"url": "https://decider.example.com/v1/decisions?x=1"}, "url"),
            ({"url": "https://user@decider.example.com/v1"}, "url"),
            ({"url": "https://127.0.0.1/v1"}, "url"),
            ({"url": "https://decider.example.com/../v1"}, "url"),
            ({"url": "not a url"}, "url"),
            ({"url": 7}, "url"),
            ({"service": "Execute API"}, "service"),
            ({"service": "execute-api&region=x"}, "service"),
            ({"region": "evil.example"}, "region"),
            ({"profile": "research&region=us-east-1"}, "profile"),
            ({"model": "has space"}, "model"),
        ],
    )
    async def test_a_bad_field_is_refused_by_name_and_nothing_is_written(
        self, patch, field, audit, provider_config, runtime, not_denied
    ):
        status, body = await _put({**GOOD, **patch})
        assert status == 400
        assert body["code"] == "decisions_provider_aws_https_invalid"
        assert body["field"] == field
        assert provider_config() == {}
        assert runtime == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {**GOOD, "endpoint": "https://elsewhere.example.com"},
            {"preset": "aws_https", "url": URL, "region": "us-west-2", "model": "m"},
            {"preset": "aws_https", "url": URL, "service": "execute-api", "model": "m"},
            {"preset": "aws_https"},
        ],
        ids=["an-endpoint", "no-service", "no-region", "bare"],
    )
    async def test_a_body_of_another_shape_is_refused(self, body, audit, provider_config):
        status, payload = await _put(body)
        assert status == 400
        assert payload["code"] == "decisions_provider_invalid_body"
        assert provider_config() == {}

    @pytest.mark.asyncio
    async def test_an_unknown_profile_is_refused(self, audit, provider_config, not_denied):
        status, body = await _put({**GOOD, "profile": "nobody"})
        assert status == 400
        assert (body["code"], body["field"]) == (
            "decisions_provider_aws_https_unknown_profile",
            "profile",
        )

    @pytest.mark.asyncio
    async def test_without_botocore_the_route_says_so(self, audit, provider_config, monkeypatch):
        monkeypatch.setattr(impl_signed_https, "botocore_installed", lambda: False)
        status, body = await _put(GOOD)
        assert status == 409
        assert body["code"] == "decisions_provider_aws_https_unavailable"
        assert provider_config() == {}

    @pytest.mark.asyncio
    async def test_the_route_writes_the_address_it_built(
        self, audit, provider_config, keystone, not_denied, runtime
    ):
        status, payload = await _put({**GOOD, "profile": "research", "model": "decider-model-v2"})
        assert status == 200
        address = f"{GOOD_ADDRESS}&profile=research"
        assert provider_config() == {
            "endpoint": address,
            "model": "decider-model-v2",
            "timeout_ms": 5000,
        }
        assert payload["active"] == "aws_https"
        assert payload["aws_https"] == {
            "url": URL,
            "service": "execute-api",
            "region": "us-west-2",
            "profile": "research",
            "model": "decider-model-v2",
        }
        assert payload["provider_permitted"]["aws_https"] is True
        assert runtime == [("deactivate",)]
        row = [r for r in audit if r["operation"] == "decisions_provider_put"][-1]
        assert row["outcome"] == "allowed"
        assert f"endpoint={address}" in row["resources"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", [None, "", "   "], ids=["absent", "empty", "blank"])
    async def test_without_a_model_nothing_is_written(
        self, model, audit, provider_config, keystone, not_denied, runtime
    ):
        """There is no default model for this endpoint: the hosted id would be refused there."""
        body = {k: v for k, v in GOOD.items() if k != "model"}
        if model is not None:
            body["model"] = model
        status, payload = await _put(body)
        assert status == 400
        assert (payload["code"], payload["field"]) == (
            "decisions_provider_aws_https_invalid",
            "model",
        )
        assert payload["error"] == "the model is required"
        assert provider_config() == {}
        assert runtime == []

    @pytest.mark.asyncio
    async def test_a_hand_set_model_is_not_kept_over_a_blank_one(
        self, audit, provider_config, keystone, not_denied
    ):
        _seed_endpoint(GOOD_ADDRESS, model="my-model", timeout_ms=8000)
        status, _ = await _put({**GOOD, "model": ""})
        assert status == 400
        assert provider_config()["model"] == "my-model"

    @pytest.mark.asyncio
    async def test_changing_only_the_model_saves_it_and_keeps_the_timeout(
        self, audit, provider_config, keystone, not_denied
    ):
        _seed_endpoint(GOOD_ADDRESS, model="decider-model-v2", timeout_ms=8000)
        status, payload = await _put({**GOOD, "model": "decider-model-v3"})
        assert status == 200
        assert provider_config() == {
            "endpoint": GOOD_ADDRESS,
            "model": "decider-model-v3",
            "timeout_ms": 8000,
        }
        assert payload["aws_https"]["model"] == "decider-model-v3"
        assert DEFAULT_MODEL not in json.dumps(provider_config())

    @pytest.mark.asyncio
    async def test_a_standing_consent_is_not_carried_to_the_endpoint(
        self, audit, provider_config, keystone, not_denied
    ):
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT}), encoding="utf-8"
        )
        status, _ = await _put(GOOD)
        assert status == 200
        state = consent.load_state()
        assert consent.consented_endpoint(state) == DEFAULT_ENDPOINT
        assert consent.permits(GOOD_ADDRESS, state) is False

    @pytest.mark.asyncio
    async def test_the_endpoints_list_is_asked_about_the_https_url(
        self, audit, provider_config, monkeypatch
    ):
        from kiro_crew.decisions import capability

        asked: list = []
        monkeypatch.setattr(capability, "is_decisions_denied", lambda *a, **k: False)

        def _targets(*_a, endpoint=None, **_k):
            asked.append(endpoint)
            return endpoint == URL

        monkeypatch.setattr(capability, "targets_denied", _targets)
        status, body = await _put(GOOD)
        assert status == 403
        assert body["code"] == "decisions_capability_denied"
        assert URL in asked
        assert provider_config() == {}

    @pytest.mark.asyncio
    async def test_the_providers_list_can_refuse_the_option(
        self, audit, provider_config, monkeypatch
    ):
        from kiro_crew.decisions import capability

        monkeypatch.setattr(
            capability,
            "is_decisions_denied",
            lambda *a, provider=None, **k: provider == capability.PROVIDER_SIGNED_HTTPS,
        )
        monkeypatch.setattr(capability, "targets_denied", lambda *a, **k: False)
        status, _ = await _put(GOOD)
        assert status == 403
        assert provider_config() == {}

    @pytest.mark.asyncio
    async def test_a_non_owner_is_refused(self, audit, provider_config):
        status, _ = await _put(GOOD, user="someone-else")
        assert status == 403
        assert provider_config() == {}


class TestGet:
    @pytest.mark.asyncio
    async def test_a_configured_endpoint_is_named_by_its_parts(self, audit):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_get

        _seed_endpoint(f"{GOOD_ADDRESS}&profile=research", model="decider-model-v2")
        payload = json.loads((await api_decisions_provider_get(_request())).text)
        assert payload["active"] == "aws_https"
        assert payload["aws_https"]["url"] == URL
        assert payload["aws_https"]["model"] == "decider-model-v2"
        assert payload["aws_https_available"] is True
        assert payload["sagemaker"] is None

    @pytest.mark.asyncio
    async def test_a_sagemaker_endpoint_still_reads_as_sagemaker(self, audit):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_get

        _seed_endpoint("sagemaker://decision-model-a?region=us-west-2")
        payload = json.loads((await api_decisions_provider_get(_request())).text)
        assert payload["active"] == "sagemaker"
        assert payload["aws_https"] is None

    @pytest.mark.asyncio
    async def test_an_address_that_does_not_parse_stays_custom(self, audit):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_get

        _seed_endpoint("aws-https://decider.example.com/v1?region=us-west-2")
        payload = json.loads((await api_decisions_provider_get(_request())).text)
        assert (payload["active"], payload["aws_https"]) == ("custom", None)

    @pytest.mark.asyncio
    async def test_a_configured_endpoint_whose_host_is_refused_reads_none(
        self, audit, provider_config, monkeypatch
    ):
        """The ``endpoints`` list is asked about the ``https://`` host; the config is kept."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_get

        _ceiling_policy(
            monkeypatch,
            {"endpoints": {"mode": "allow", "allow": ["other.example.com"]}},
        )
        _seed_endpoint(GOOD_ADDRESS, model="decider-model-v2")
        before = provider_config()
        payload = json.loads((await api_decisions_provider_get(_request())).text)
        assert payload["active"] == "none"
        assert payload["configured_refused"] is True
        assert payload["configured_endpoint"] == GOOD_ADDRESS
        assert provider_config() == before

    @pytest.mark.asyncio
    async def test_a_configured_endpoint_the_providers_list_refuses_reads_none(
        self, audit, provider_config, monkeypatch
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_get

        _ceiling_policy(monkeypatch, {"providers": {"mode": "allow", "allow": ["sagemaker"]}})
        _seed_endpoint(GOOD_ADDRESS)
        before = provider_config()
        payload = json.loads((await api_decisions_provider_get(_request())).text)
        assert payload["provider_permitted"]["aws_https"] is False
        assert payload["provider_permitted"]["sagemaker"] is True
        assert (payload["active"], payload["configured_refused"]) == ("none", True)
        assert provider_config() == before

    @pytest.mark.asyncio
    async def test_a_permitted_configured_endpoint_is_not_refused(self, audit, monkeypatch):
        """The control: the same reads with the host and the id allowed."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_get

        _ceiling_policy(
            monkeypatch,
            {
                "providers": {"mode": "allow", "allow": ["aws_https"]},
                "endpoints": {"mode": "allow", "allow": ["decider.example.com"]},
            },
        )
        _seed_endpoint(GOOD_ADDRESS)
        payload = json.loads((await api_decisions_provider_get(_request())).text)
        assert (payload["active"], payload["configured_refused"]) == ("aws_https", False)


class TestConsent:
    """Turning consent on for a configured ``aws-https://`` address asks its host."""

    @pytest.fixture(autouse=True)
    def _sel(self, monkeypatch):
        # The audited governance probe writes an SEL row; without one it fails closed,
        # which would make every refusal below pass for the wrong reason.
        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: MagicMock())

    @pytest.fixture(autouse=True)
    def _configured(self, monkeypatch):
        # The gate reads the live config snapshot; pin it to the address under test.
        from kiro_crew.decisions import gate

        monkeypatch.setattr(gate, "configured_endpoint", lambda config=None: GOOD_ADDRESS)

    async def _enable(self):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        resp = await api_decisions_consent_put(
            _request(body={"enabled": True, "endpoint": GOOD_ADDRESS})
        )
        return resp.status, json.loads(resp.text)

    @pytest.mark.asyncio
    async def test_a_refused_host_records_no_consent(self, audit, keystone, monkeypatch):
        _ceiling_policy(
            monkeypatch, {"endpoints": {"mode": "allow", "allow": ["other.example.com"]}}
        )
        _seed_endpoint(GOOD_ADDRESS)
        status, body = await self._enable()
        assert status == 403
        assert body["code"] == "decisions_capability_denied"
        assert not keystone.exists()

    @pytest.mark.asyncio
    async def test_a_permitted_host_records_consent_for_the_address(
        self, audit, keystone, monkeypatch
    ):
        _ceiling_policy(
            monkeypatch,
            {
                "providers": {"mode": "allow", "allow": ["aws_https"]},
                "endpoints": {"mode": "allow", "allow": ["decider.example.com"]},
            },
        )
        _seed_endpoint(GOOD_ADDRESS)
        status, _ = await self._enable()
        assert status == 200
        assert consent.permits(GOOD_ADDRESS) is True


class TestConnectionTest:
    @pytest.mark.asyncio
    async def test_one_fixed_question_is_sent_and_nothing_is_written(
        self, audit, provider_config, not_denied, monkeypatch
    ):
        from kiro_crew.dashboard.handlers.decisions_signed_https import (
            api_decisions_provider_signed_https_test,
        )

        sent: list = []

        async def _ask(self, state, questions):
            sent.append((self._endpoint, state, [q.id for q in questions]))
            from kiro_crew.decisions.types import Answer

            return {"connection_test": Answer(id="connection_test", value=0.9, p=0.9)}

        monkeypatch.setattr(impl_signed_https.SignedHttpsProvider, "ask", _ask)
        resp = await api_decisions_provider_signed_https_test(_request(body=GOOD))
        result = json.loads(resp.text)
        assert result["ok"] is True and result["error"] == ""
        assert sent == [
            (GOOD_ADDRESS, "Connection test from the Kiro Crew settings page.", ["connection_test"])
        ]
        assert provider_config() == {}

    @pytest.mark.asyncio
    async def test_a_refused_signature_reports_credentials(self, audit, not_denied, monkeypatch):
        from kiro_crew.dashboard.handlers.decisions_signed_https import (
            api_decisions_provider_signed_https_test,
        )

        async def _ask(self, state, questions):
            raise impl_signed_https.SignedHttpsCredentialsError("refused (HTTP 403)")

        monkeypatch.setattr(impl_signed_https.SignedHttpsProvider, "ask", _ask)
        resp = await api_decisions_provider_signed_https_test(_request(body=GOOD))
        result = json.loads(resp.text)
        assert (result["ok"], result["error"]) == (False, "credentials")

    @pytest.mark.asyncio
    async def test_without_a_model_nothing_is_sent(self, audit, not_denied, monkeypatch):
        from kiro_crew.dashboard.handlers.decisions_signed_https import (
            api_decisions_provider_signed_https_test,
        )

        sent: list = []

        async def _ask(self, state, questions):
            sent.append(self._model)

        monkeypatch.setattr(impl_signed_https.SignedHttpsProvider, "ask", _ask)
        body = {k: v for k, v in GOOD.items() if k != "model"}
        resp = await api_decisions_provider_signed_https_test(_request(body=body))
        assert resp.status == 400
        assert json.loads(resp.text)["field"] == "model"
        assert sent == []

    @pytest.mark.asyncio
    async def test_the_typed_model_is_the_one_tested(self, audit, not_denied, monkeypatch):
        from kiro_crew.dashboard.handlers.decisions_signed_https import (
            api_decisions_provider_signed_https_test,
        )
        from kiro_crew.decisions.types import Answer

        sent: list = []

        async def _ask(self, state, questions):
            sent.append(self._model)
            return {"connection_test": Answer(id="connection_test", value=0.9, p=0.9)}

        monkeypatch.setattr(impl_signed_https.SignedHttpsProvider, "ask", _ask)
        resp = await api_decisions_provider_signed_https_test(_request(body=GOOD))
        assert json.loads(resp.text)["ok"] is True
        assert sent == ["decider-model-v2"]

    @pytest.mark.asyncio
    async def test_a_sagemaker_body_is_not_this_test(self, audit):
        from kiro_crew.dashboard.handlers.decisions_signed_https import (
            api_decisions_provider_signed_https_test,
        )

        resp = await api_decisions_provider_signed_https_test(
            _request(body={"preset": "sagemaker", "endpoint_name": "m", "region": "us-west-2"})
        )
        assert resp.status == 400
