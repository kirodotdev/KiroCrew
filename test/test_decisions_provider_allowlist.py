"""The per-model allow list (``capabilities.decisions`` ``providers``) and the off switch.

Every decision model the picker offers has its own entry, asked as
``providers:<id>`` wherever the row is read for the configured provider: the send
path (``gate._consented_for``), the provider and consent writes, the runtime resume
and the settings read. These tests install a REAL policy and run the real
evaluator; only SEL and the local runtime are faked.

Four properties are pinned:

* no policy, or a policy that does not name the list, allows every model, so the
  standalone default and every saved selection keep working after an upgrade;
* a model the list refuses is neither selectable (``403`` on the PUT) nor used (the
  gate sends nothing to it, whichever writer named it in ``config.json``);
* a refusal leaves the recorded consent in place and inert: it is never widened or
  erased, and it counts again once a policy allows the model;
* the edition snippet the decisions spec documents parses and refuses the hosted
  provider alone.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config.sections import DECISION_PROVIDER_ENDPOINT_DEFAULT as HOSTED
from kiro_crew.config.sections import DecisionProviderConfig, DecisionsConfig
from kiro_crew.decisions import capability, consent
from kiro_crew.decisions import gate as gate_mod
from kiro_crew.decisions import local_models
from kiro_crew.decisions.types import Answer, Choice

POINT = "skills.select"
QUESTIONS = [Choice(id="verdict", prompt="Which skill?", options=["NONE", "DUP"])]
PRESET_IDS = [m.id for m in local_models.LOCAL_MODELS]
#: The fragment an edition's bundled ``security_policy.json`` carries to refuse the
#: hosted provider (docs/system-specs/modules/decisions.md section 7.2).
EDITION_DECISIONS = {"scopes": {"providers": {"mode": "deny", "deny": ["jev"]}}}


def _install_policy(monkeypatch, decisions: dict | None) -> None:
    """Install a ceiling whose ``capabilities.decisions`` is *decisions* (None: no policy)."""
    from kiro_crew.platform import context as pc
    from kiro_crew.platform.governance import parse_policy

    ceiling = None
    if decisions is not None:
        ceiling = parse_policy(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"decisions": decisions},
            }
        )
    monkeypatch.setattr(pc, "current_context", lambda: SimpleNamespace(governance=ceiling))


def _allow(*ids: str) -> dict:
    return {"scopes": {"providers": {"mode": "allow", "allow": list(ids)}}}


def _deny(*ids: str) -> dict:
    return {"scopes": {"providers": {"mode": "deny", "deny": list(ids)}}}


@pytest.fixture(autouse=True)
def sel_rows(_floor_monkeypatch):
    """Every ``governance_decision`` row the probes write, as dicts."""
    import kiro_crew.sel as sel_mod

    fake = MagicMock()
    _floor_monkeypatch.setattr(sel_mod, "sel", lambda: fake)
    _floor_monkeypatch.setattr(gate_mod, "_capability_denied_warned", False)

    def _rows() -> list[dict]:
        return [c.kwargs for c in fake.log_governance_decision.call_args_list]

    return _rows


@pytest.fixture(autouse=True)
def keystone(tmp_path, _floor_monkeypatch):
    """A real keystone under *tmp_path*; returns a setter (None removes it)."""
    path = tmp_path / "decisions_consent.json"
    _floor_monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)

    def _set(enabled: bool | None, endpoint: str = HOSTED) -> None:
        if enabled is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps({"enabled": enabled, "endpoint": endpoint}), "utf-8")

    _set.path = path  # type: ignore[attr-defined]
    return _set


class _FakeRuntime:
    """The gateway's local runtime, attesting whatever preset a test names."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.state = {
            "preset": "",
            "state": "idle",
            "port": 0,
            "bytes_done": 0,
            "bytes_total": 0,
            "error": "",
        }

    def status(self):
        return dict(self.state)

    def installed_ids(self):
        return []

    def activate(self, m, port):
        self.calls.append(("activate", m.id, port))

    def deactivate(self, *, wait=False):
        self.calls.append(("deactivate",))

    def serve(self, preset_id: str, port: int) -> None:
        self.state.update(preset=preset_id, state="running", port=port)


@pytest.fixture(autouse=True)
def runtime(_floor_monkeypatch):
    from kiro_crew.decisions import local_runtime

    fake = _FakeRuntime()
    _floor_monkeypatch.setattr(local_runtime, "get_runtime", lambda: fake)
    _floor_monkeypatch.setattr(local_runtime, "free_port", lambda preferred: preferred)
    return fake


def _config(endpoint: str = "", model: str | None = None):
    kwargs: dict = {"timeout_ms": 1000}
    if endpoint:
        kwargs["endpoint"] = endpoint
    if model is not None:
        kwargs["model"] = model
    return SimpleNamespace(
        decisions=DecisionsConfig(bucket=100, provider=DecisionProviderConfig(**kwargs))
    )


class _RecordingOracle:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def ask(self, state, questions):
        self.calls.append((state, questions))
        return {q.id: Answer(id=q.id, value="DUP", p=0.9, confidence=0.9) for q in questions}


@pytest.fixture
def oracle(monkeypatch):
    """Answers every decision; records whether anything was sent."""
    import kiro_crew.decisions.impl_systemone as impl_mod

    fake = _RecordingOracle()
    monkeypatch.setattr(impl_mod, "SystemOneProvider", lambda provider: fake)
    monkeypatch.setattr("kiro_crew.decisions.log.append", lambda *a, **k: True)
    return fake


# ---------------------------------------------------------------------------
# The data: one catalog row, one id per decision model
# ---------------------------------------------------------------------------


class TestTheListIsCatalogData:
    def test_the_row_carries_the_providers_list_as_identifiers(self):
        from kiro_crew.platform.governance_parts.catalog import SCOPE_CATALOG

        spec = SCOPE_CATALOG[capability.DECISIONS_SCOPE]
        assert spec.scope_matchers[capability.PROVIDERS_LIST] == "identifier"
        # The lists that shipped before it are unchanged.
        assert {"models", "points", "endpoints"} <= set(spec.scope_matchers)

    def test_every_decision_model_has_one_id(self):
        ids = capability.provider_ids()
        assert len(ids) == len(set(ids))
        assert set(ids) == {"jev", *PRESET_IDS, "sagemaker", "custom"}
        assert capability.PROVIDER_NONE not in ids

    def test_the_ids_are_the_stored_picker_ids(self):
        """Saved selections keep their meaning: the ids are the ones config already spells."""
        from kiro_crew.dashboard.handlers.decisions_sagemaker import PRESET_SAGEMAKER

        assert capability.PROVIDER_HOSTED == local_models.PRESET_HOSTED
        assert capability.PROVIDER_NONE == local_models.PRESET_NONE
        assert capability.PROVIDER_SAGEMAKER == PRESET_SAGEMAKER


class TestProviderId:
    @pytest.mark.parametrize(
        ("endpoint", "model", "expected"),
        [
            (HOSTED, "jev-latest", "jev"),
            ("none", "", "none"),
            ("sagemaker://decision-model-a?region=us-west-2", "jev-latest", "sagemaker"),
            # The scheme alone picks the SageMaker client, so a malformed one is it too.
            ("sagemaker://", "x", "sagemaker"),
            ("https://proxy.example/v1/systemone", "jev-latest", "custom"),
            ("http://127.0.0.1:9999/v1/systemone", "jev-latest", "custom"),
        ],
    )
    def test_each_address_shape_names_its_model(self, endpoint, model, expected):
        assert capability.provider_id(endpoint, model) == expected

    def test_a_route_built_preset_address_is_that_preset(self):
        for m in local_models.LOCAL_MODELS:
            endpoint = local_models.endpoint_for(m.default_port)
            assert capability.provider_id(endpoint, m.model) == m.id


# ---------------------------------------------------------------------------
# Use time: the gate sends nothing to a refused model
# ---------------------------------------------------------------------------


def _decide(config) -> object:
    return asyncio.run(gate_mod.decide(POINT, "hi", QUESTIONS, config=config))


class TestTheGateHonoursTheList:
    def test_no_policy_sends_to_every_saved_selection(self, monkeypatch, keystone, oracle):
        """Upgrade compatibility: no policy reads exactly as before."""
        _install_policy(monkeypatch, None)
        keystone(True)
        assert _decide(_config()) is not None
        assert len(oracle.calls) == 1

    def test_a_policy_without_the_list_sends(self, monkeypatch, keystone, oracle):
        _install_policy(monkeypatch, {"enabled": True})
        keystone(True)
        assert _decide(_config()) is not None

    def test_the_hosted_entry_allowed_sends(self, monkeypatch, keystone, oracle):
        _install_policy(monkeypatch, _allow("jev"))
        keystone(True)
        assert _decide(_config()) is not None
        items = [r.get("item") for r in self._rows(monkeypatch)]
        assert "providers:jev" in items

    @pytest.mark.parametrize("policy", [_deny("jev"), _allow("laya"), _allow()])
    def test_a_refused_model_is_sent_nothing_and_its_consent_stays_inert(
        self, monkeypatch, keystone, oracle, policy
    ):
        _install_policy(monkeypatch, policy)
        keystone(True)
        recorded = keystone.path.read_bytes()
        assert gate_mod.is_enabled(POINT, config=_config()) is False
        assert _decide(_config()) is None
        assert oracle.calls == [], "a refused model must not be reached"
        assert keystone.path.read_bytes() == recorded, "the consent is left in place"

    def test_the_consent_counts_again_once_a_policy_allows_the_model(
        self, monkeypatch, keystone, oracle
    ):
        keystone(True)
        _install_policy(monkeypatch, _deny("jev"))
        assert _decide(_config()) is None
        _install_policy(monkeypatch, _deny("laya"))
        assert _decide(_config()) is not None
        assert len(oracle.calls) == 1

    def test_a_refused_model_written_into_config_by_hand_is_not_used(
        self, monkeypatch, keystone, oracle
    ):
        """A config.json writer cannot route around the list: the custom address is
        asked about its own entry, not the hosted one."""
        custom = "https://proxy.example/v1/systemone"
        _install_policy(monkeypatch, _allow("jev"))
        keystone(True, endpoint=custom)
        assert _decide(_config(endpoint=custom)) is None
        assert oracle.calls == []

    def test_a_local_preset_answers_to_its_own_entry(self, monkeypatch, keystone, oracle, runtime):
        laya = local_models.get("laya")
        endpoint = local_models.endpoint_for(laya.default_port)
        runtime.serve(laya.id, laya.default_port)
        keystone(True, endpoint=endpoint)
        _install_policy(monkeypatch, _allow("plumb-4b"))
        assert _decide(_config(endpoint=endpoint, model=laya.model)) is None
        _install_policy(monkeypatch, _allow("laya"))
        assert _decide(_config(endpoint=endpoint, model=laya.model)) is not None

    def test_the_off_switch_refuses_every_model(self, monkeypatch, keystone, oracle):
        _install_policy(monkeypatch, {"enabled": False, **_allow("jev")})
        keystone(True)
        assert _decide(_config()) is None
        assert oracle.calls == []

    def test_a_policy_swap_between_the_row_and_the_entry_denies(self, monkeypatch):
        _install_policy(monkeypatch, None)
        generations = iter([1, 2])
        monkeypatch.setattr(capability, "_answer_generation", lambda: next(generations))
        assert capability.is_decisions_denied(provider="jev") is True

    def test_no_model_is_never_asked_about(self, monkeypatch, sel_rows):
        _install_policy(monkeypatch, _allow())
        assert capability.is_decisions_denied(provider="none") is False
        assert not any("providers:" in str(r.get("item")) for r in sel_rows())

    @staticmethod
    def _rows(monkeypatch) -> list[dict]:
        import kiro_crew.sel as sel_mod

        return [c.kwargs for c in sel_mod.sel().log_governance_decision.call_args_list]


# ---------------------------------------------------------------------------
# Selection: the provider route and the consent route
# ---------------------------------------------------------------------------


def _request(*, body=None):
    req = MagicMock()
    req.path = "/api/decisions/provider"
    store = {"app": "", "user": "owner-1"}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = "owner-1"
    req.app = {"state": state}
    req.json = AsyncMock(return_value=body if body is not None else {})
    return req


@pytest.fixture
def audit(monkeypatch):
    import kiro_crew.dashboard.handlers as handlers_pkg

    fake = MagicMock()
    monkeypatch.setattr(handlers_pkg, "sel", lambda: fake)
    return fake


def _provider_config() -> dict:
    from kiro_crew.config.loader import config_path

    path = config_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return data.get("decisions", {}).get("provider", {})


async def _get() -> dict:
    from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_get

    return json.loads((await api_decisions_provider_get(_request())).text)


async def _put(body) -> tuple[int, dict]:
    from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

    resp = await api_decisions_provider_put(_request(body=body))
    return resp.status, json.loads(resp.text)


class TestTheProviderRoute:
    @pytest.mark.asyncio
    async def test_no_policy_permits_every_model(self, audit, monkeypatch):
        _install_policy(monkeypatch, None)
        payload = await _get()
        assert set(payload["provider_permitted"]) == set(capability.provider_ids())
        assert all(payload["provider_permitted"].values())
        assert payload["hosted_permitted"] is True

    @pytest.mark.asyncio
    async def test_an_allow_list_of_one_permits_that_one_only(self, audit, monkeypatch):
        _install_policy(monkeypatch, _allow("laya"))
        payload = await _get()
        permitted = {k for k, v in payload["provider_permitted"].items() if v}
        assert permitted == {"laya"}
        # The hosted default the list refuses reads as "No model", config untouched.
        assert payload["hosted_permitted"] is False
        assert payload["active"] == "none"
        assert payload["configured_endpoint"] == HOSTED

    @pytest.mark.asyncio
    async def test_the_off_switch_permits_no_model(self, audit, monkeypatch):
        _install_policy(monkeypatch, {"enabled": False})
        payload = await _get()
        assert not any(payload["provider_permitted"].values())

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("policy", "preset"),
        [(_deny("jev"), "jev"), (_allow("laya"), "plumb-4b"), ({"enabled": False}, "laya")],
    )
    async def test_a_refused_model_cannot_be_selected(self, audit, monkeypatch, policy, preset):
        _install_policy(monkeypatch, policy)
        status, body = await _put({"preset": preset})
        assert status == 403
        assert body["code"] == "decisions_capability_denied"
        assert "administrator" in body["error"]
        assert _provider_config() == {}, "a refused selection writes nothing"

    @pytest.mark.asyncio
    async def test_an_allowed_model_is_selected(self, audit, monkeypatch, runtime):
        _install_policy(monkeypatch, _allow("laya"))
        status, payload = await _put({"preset": "laya"})
        assert status == 200
        assert payload["active"] == "laya"
        assert ("activate", "laya", local_models.get("laya").default_port) in runtime.calls

    @pytest.mark.asyncio
    async def test_no_model_is_never_refused(self, audit, monkeypatch):
        _install_policy(monkeypatch, {"enabled": False})
        status, _ = await _put({"preset": "none"})
        assert status == 200
        assert _provider_config()["endpoint"] == local_models.ENDPOINT_NONE

    @pytest.mark.asyncio
    async def test_a_refused_sagemaker_endpoint_cannot_be_selected(self, audit, monkeypatch):
        pytest.importorskip("boto3")
        _install_policy(monkeypatch, _deny("sagemaker"))
        status, body = await _put(
            {"preset": "sagemaker", "endpoint_name": "decision-model-a", "region": "us-west-2"}
        )
        assert status == 403
        assert body["code"] == "decisions_capability_denied"
        assert _provider_config() == {}


class TestTheConsentRoute:
    @staticmethod
    async def _consent(body) -> tuple[int, dict]:
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        resp = await api_decisions_consent_put(_request(body=body))
        return resp.status, json.loads(resp.text)

    @pytest.mark.asyncio
    async def test_consent_to_a_refused_model_is_refused(self, audit, monkeypatch, keystone):
        keystone(None)
        _install_policy(monkeypatch, _deny("jev"))
        status, body = await self._consent({"enabled": True, "endpoint": HOSTED})
        assert status == 403
        assert body["code"] == "decisions_capability_denied"
        assert not keystone.path.exists(), "nothing is recorded for a refused model"

    @pytest.mark.asyncio
    async def test_turning_consent_off_is_never_refused(self, audit, monkeypatch, keystone):
        keystone(True)
        _install_policy(monkeypatch, _deny("jev"))
        status, body = await self._consent({"enabled": False})
        assert status == 200
        assert consent.is_enabled(consent.load_state()) is False

    @pytest.mark.asyncio
    async def test_the_get_reports_a_refused_model_as_not_sending(
        self, audit, monkeypatch, keystone
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_get

        keystone(True)
        _install_policy(monkeypatch, _deny("jev"))
        body = json.loads((await api_decisions_consent_get(_request())).text)
        assert body["enabled"] is True, "the recorded consent is shown as it stands"
        assert body["permits"] is False, "and it sends nothing"


class TestTheRuntimeResume:
    def test_a_refused_preset_is_not_started(self, monkeypatch, runtime):
        from kiro_crew.config import live
        from kiro_crew.decisions import local_runtime

        laya = local_models.get("laya")
        endpoint = local_models.endpoint_for(laya.default_port)
        monkeypatch.setattr(live, "snapshot", lambda: _config(endpoint=endpoint, model=laya.model))
        _install_policy(monkeypatch, _deny("laya"))
        assert local_runtime.resume_configured() == ""
        assert runtime.calls == []
        _install_policy(monkeypatch, _deny("jev"))
        assert local_runtime.resume_configured() == "laya"


class TestTheEditionSnippet:
    """An edition refusing the hosted provider refuses it alone, and nothing else."""

    def test_it_refuses_the_hosted_provider_and_allows_every_other(self, monkeypatch):
        _install_policy(monkeypatch, EDITION_DECISIONS)
        listed = capability.permitted_providers()
        assert listed.pop("jev") is False
        assert all(listed.values()), listed

    def test_the_off_switch_snippet_refuses_every_model(self, monkeypatch):
        _install_policy(monkeypatch, {"enabled": False})
        assert not any(capability.permitted_providers().values())
