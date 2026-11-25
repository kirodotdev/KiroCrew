"""The provider read names the other surfaces that still send to a model the dashboard refuses.

A profile bound to the dashboard surface can refuse the configured decision model
while the ceiling of another surface permits it; the send path asks under the turn's
own session key, so a recorded consent still sends from that surface. These tests
install a REAL policy and real profiles and run the real evaluator; only SEL and the
local runtime are faked.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config.sections import DECISION_PROVIDER_ENDPOINT_DEFAULT as HOSTED
from kiro_crew.dashboard.handlers import decisions_surfaces
from kiro_crew.decisions import capability

EVERY_SURFACE = [surface for surface, _key in decisions_surfaces.SURFACE_KEYS]
#: The runtime's own sentinels: unbound, they resolve to a deny-all profile.
SENTINEL_SURFACES = {"background", "heartbeat"}


def _deny_hosted() -> dict:
    return {"scopes": {"providers": {"mode": "deny", "deny": ["jev"]}}}


@pytest.fixture(autouse=True)
def sel_rows(_floor_monkeypatch):
    import kiro_crew.sel as sel_mod

    fake = MagicMock()
    _floor_monkeypatch.setattr(sel_mod, "sel", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def runtime(_floor_monkeypatch):
    from kiro_crew.decisions import local_runtime

    state = {"preset": "", "state": "idle", "port": 0, "bytes_done": 0, "bytes_total": 0}
    fake = SimpleNamespace(status=lambda: {**state, "error": ""}, installed_ids=lambda: [])
    _floor_monkeypatch.setattr(local_runtime, "get_runtime", lambda: fake)
    return fake


@pytest.fixture
def ceiling(monkeypatch, tmp_path):
    """Install a policy (``None``: no ``capabilities.decisions``) and profiles by surface."""
    from kiro_crew.platform import context as pc
    from kiro_crew.platform import governance_profiles as gp
    from kiro_crew.platform.governance import parse_policy

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)

    def _install(policy: dict | None = None, **bound: dict) -> None:
        raw: dict = {"version": 1, "boot": {"fail_closed": True}}
        if policy is not None:
            raw["capabilities"] = {"decisions": policy}
        governance = parse_policy(raw)
        monkeypatch.setattr(pc, "current_context", lambda: SimpleNamespace(governance=governance))
        for surface, decisions in bound.items():
            (profiles / f"{surface}.json").write_text(
                json.dumps(
                    {
                        "name": f"{surface}-profile",
                        "bind": {"type": "surface", "id": surface},
                        "capabilities": {"decisions": decisions},
                    }
                ),
                encoding="utf-8",
            )
        gp.reset_store()

    yield _install
    gp.reset_store()


def _request():
    req = MagicMock()
    req.path = "/api/decisions/provider"
    store = {"app": "", "user": "owner-1"}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = "owner-1"
    req.app = {"state": state}
    req.json = AsyncMock(return_value={})
    return req


@pytest.fixture
def audit(monkeypatch):
    import kiro_crew.dashboard.handlers as handlers_pkg

    monkeypatch.setattr(handlers_pkg, "sel", lambda: MagicMock())


async def _get() -> dict:
    from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_get

    return json.loads((await api_decisions_provider_get(_request())).text)


class TestTheSurfaceKeys:
    def test_each_key_classifies_to_its_surface(self):
        from kiro_crew.sel import _infer_source

        assert {
            surface: _infer_source(key) for surface, key in decisions_surfaces.SURFACE_KEYS
        } == {surface: surface for surface in EVERY_SURFACE}

    def test_every_surface_but_the_dashboard_is_asked(self):
        """A surface the taxonomy grows must be asked too, or its sends go unnamed."""
        from kiro_crew.sel import _AUDIT_SOURCES

        assert set(EVERY_SURFACE) == set(_AUDIT_SOURCES) - {"unknown", "host", "dashboard"}
        assert len(EVERY_SURFACE) == len(set(EVERY_SURFACE))


class TestTheProviderRead:
    @pytest.mark.asyncio
    async def test_a_dashboard_refusal_names_the_surfaces_that_still_send(self, audit, ceiling):
        ceiling(None, dashboard=_deny_hosted())
        payload = await _get()
        assert payload["provider_permitted"]["jev"] is False, "the dashboard refuses it"
        assert payload["active"] == "none"
        # Every surface without its own profile reads the policy, which permits; the
        # runtime's sentinels resolve to their deny-all.
        assert set(payload["permitted_surfaces"]) == set(EVERY_SURFACE) - SENTINEL_SURFACES
        assert "slack" in payload["permitted_surfaces"]

    @pytest.mark.asyncio
    async def test_a_surface_whose_own_profile_refuses_is_not_named(self, audit, ceiling):
        ceiling(None, dashboard=_deny_hosted(), slack={"enabled": False}, cron=_deny_hosted())
        payload = await _get()
        assert "slack" not in payload["permitted_surfaces"]
        assert "cron" not in payload["permitted_surfaces"]
        assert "cli" in payload["permitted_surfaces"]

    @pytest.mark.asyncio
    async def test_a_policy_refusal_names_no_surface(self, audit, ceiling):
        ceiling(_deny_hosted())
        payload = await _get()
        assert payload["provider_permitted"]["jev"] is False
        assert payload["permitted_surfaces"] == []

    @pytest.mark.asyncio
    async def test_a_refused_host_on_every_surface_names_none(self, audit, ceiling):
        ceiling({"scopes": {"endpoints": {"mode": "deny", "deny": ["api.typesafe.ai"]}}})
        payload = await _get()
        assert payload["hosted_permitted"] is False
        assert payload["permitted_surfaces"] == []

    @pytest.mark.asyncio
    async def test_a_model_the_models_list_refuses_everywhere_names_none(self, audit, ceiling):
        """The send path asks ``models:<id>`` for the id it sends; so does the reading."""
        from kiro_crew.config.sections import DECISION_PROVIDER_MODEL_DEFAULT as MODEL

        ceiling({"scopes": {"models": {"mode": "deny", "deny": [MODEL]}}}, dashboard=_deny_hosted())
        payload = await _get()
        assert payload["provider_permitted"]["jev"] is False
        assert payload["permitted_surfaces"] == []

    @pytest.mark.parametrize(("state", "named"), [("idle", False), ("running", True)])
    def test_a_preset_the_runtime_does_not_run_is_sent_nothing(
        self, ceiling, runtime, state, named
    ):
        """Every surface sends to a preset only while the runtime attests it."""
        from kiro_crew.decisions import local_models

        ceiling(None)
        preset = local_models.get("laya")
        runtime.status = lambda: {"preset": "laya", "state": state, "port": 8123, "error": ""}
        surfaces = decisions_surfaces.permitted_surfaces(
            local_models.endpoint_for(8123), preset.model
        )
        assert ("cli" in surfaces) is named

    @pytest.mark.asyncio
    async def test_a_permitted_model_asks_no_surface(self, audit, ceiling, monkeypatch):
        ceiling(None, slack={"enabled": False})

        def _never(*_args, **_kwargs):
            raise AssertionError("a model the dashboard permits needs no per-surface probe")

        monkeypatch.setattr(decisions_surfaces, "permitted_surfaces", _never)
        payload = await _get()
        assert payload["provider_permitted"]["jev"] is True
        assert payload["permitted_surfaces"] == []

    @pytest.mark.asyncio
    async def test_the_reading_writes_no_governance_row(self, audit, ceiling, sel_rows):
        ceiling(None, dashboard=_deny_hosted())
        assert capability.is_decisions_denied("slack:x", provider="jev", audit=False) is False
        assert capability.is_decisions_denied("dashboard:ui", provider="jev", audit=False) is True
        assert sel_rows.log_governance_decision.call_count == 0


class TestTheConsentPayload:
    @pytest.mark.parametrize(
        ("recorded", "covers"), [(HOSTED, True), ("https://other.example", False)]
    )
    def test_a_denial_keeps_whether_consent_covers_the_configured_address(
        self, monkeypatch, recorded, covers
    ):
        """``withdrawn`` says nothing about the address: a refused model at an address the
        consent does not cover is sent nothing from any surface."""
        from kiro_crew.dashboard.handlers import decisions as handler
        from kiro_crew.decisions import gate

        monkeypatch.setattr(gate, "configured_endpoint", lambda *_args: HOSTED)
        body = handler._payload({"enabled": True, "endpoint": recorded}, denied=True)
        assert (body["permits"], body["withdrawn"], body["covers_configured"]) == (
            False,
            True,
            covers,
        )


class TestTheProbeMatchesTheSendPath:
    def test_the_offer_agrees_with_the_audited_check(self, ceiling):
        ceiling(None, dashboard=_deny_hosted(), slack={"enabled": False})
        for surface, key in decisions_surfaces.SURFACE_KEYS:
            assert capability.is_decisions_denied(key, provider="jev", audit=False) is (
                capability.is_decisions_denied(key, provider="jev")
            ), surface


class TestTheGenerationFence:
    """One offer is answered by ONE policy: a swap mid-read, or a generation nobody can
    read, refuses the offer; a stable generation leaves a permitted one permitted."""

    @pytest.mark.parametrize(
        ("generations", "denied"),
        [((1, 1), False), ((1, 2), True), ((None, None), True)],
        ids=["stable", "moved", "unreadable"],
    )
    def test_the_offer_follows_the_generation(self, ceiling, monkeypatch, generations, denied):
        ceiling(None)
        answers = iter(generations)
        monkeypatch.setattr(capability, "_answer_generation", lambda: next(answers))
        assert capability.is_decisions_denied("slack:x", provider="jev", audit=False) is denied

    def test_a_moved_generation_refuses_every_surface(self, ceiling, monkeypatch):
        ceiling(None)
        counter = iter(range(1000))
        monkeypatch.setattr(capability, "_answer_generation", lambda: next(counter))
        assert decisions_surfaces.permitted_surfaces(HOSTED, "") == []

    def test_a_swap_between_the_row_and_the_host_probe_names_no_surface(self, ceiling, monkeypatch):
        """Each offer's own bracket holds, yet the row and the host were answered by two
        policies: the reading as a whole is discarded."""
        ceiling(None)
        generation = {"value": 1}
        monkeypatch.setattr(capability, "_answer_generation", lambda: generation["value"])

        def _host_probe_swaps_the_policy(*_args, **_kwargs):
            generation["value"] += 1
            return False

        monkeypatch.setattr(decisions_surfaces, "endpoint_refused", _host_probe_swaps_the_policy)
        assert decisions_surfaces.permitted_surfaces(HOSTED, "") == []

    def test_a_stable_generation_keeps_the_reading(self, ceiling, monkeypatch):
        ceiling(None)
        monkeypatch.setattr(capability, "_answer_generation", lambda: 7)
        assert set(decisions_surfaces.permitted_surfaces(HOSTED, "")) == (
            set(EVERY_SURFACE) - SENTINEL_SURFACES
        )
