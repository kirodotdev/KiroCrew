"""The managed-environment lane's switch, its settings and its platform seams.

Off (the ``crew_client`` preview off, or any LTS build) is ``main``: no launch row, no engine,
no sign-in profile, every lane route a 404. On, every value the lane sends or writes comes from
``instances.managed_env_*``, and a missing one is refused by name before any call.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from managed_env_fake_bff import FakeBff, FakeTime
from managed_env_helpers import (  # noqa: F401
    FULL_SETTINGS,
    REAL_READ_SETTINGS,
    fresh_shared_client,
    lane_values,
    no_real_install_ssh,
)

from kiro_crew.managed_env import PROVISIONER_ID, settings
from kiro_crew.managed_env.client import BffClient, SandboxError
from kiro_crew.managed_env.engine import SandboxEngineError, SandboxLaunchEngine
from kiro_crew.managed_env.guard import SandboxGuard
from kiro_crew.managed_env.provider import (
    ManagedEnvDashboardContributor,
    ManagedEnvProvisionerProvider,
)
from kiro_crew.platform.defaults import DefaultRemoteProvisionerProvider


def _cfg(previews=None, **instances):
    return SimpleNamespace(
        dashboard=SimpleNamespace(feature_previews=previews),
        instances=SimpleNamespace(**instances),
    )


@pytest.mark.parametrize(
    "previews,on",
    [({"crew_client": True}, True), ({}, False), ({"crew_client": "yes"}, False), (None, False)],
)
def test_the_lane_is_on_only_with_the_crew_client_preview_exactly_true(previews, on):
    assert settings.lane_enabled(_cfg(previews)) is on


def test_an_lts_build_reads_the_lane_off_even_when_the_config_stores_true(monkeypatch):
    monkeypatch.setattr(settings, "read_settings", REAL_READ_SETTINGS)
    monkeypatch.setattr("kiro_crew.edition.is_lts", lambda: True)
    assert settings.lane_enabled(_cfg({"crew_client": True})) is False
    profile_cfg = _cfg(
        {"crew_client": True},
        managed_env_start_url="https://example-org.example.test/start",
        managed_env_profile_arn=FULL_SETTINGS.profile_arn,
    )
    assert settings.sign_in_profile(profile_cfg) is None


def test_lane_off_is_main_no_row_no_engine_no_profile(monkeypatch):
    monkeypatch.setattr(settings, "lane_enabled", lambda cfg=None: False)
    base = DefaultRemoteProvisionerProvider()
    provider = ManagedEnvProvisionerProvider(base)
    assert [p.id for p in provider.provisioners()] == [p.id for p in base.provisioners()]
    with pytest.raises(KeyError):
        provider.engine_for(PROVISIONER_ID)
    assert ManagedEnvDashboardContributor().idc_sign_in_profiles() == {}


def test_every_lane_route_answers_404_with_the_preview_off(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import _shared
    from kiro_crew.managed_env.routes import SandboxRoutes, register_sandbox_routes

    async def owner(request, operation):
        return None

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", owner)
    monkeypatch.setattr(settings, "lane_enabled", lambda cfg=None: False)

    def no_engine():
        raise AssertionError("the lane is off: no engine is built")

    calls = [
        ("GET", "/api/managed-environments/list"),
        ("GET", "/api/managed-environments/row-lifecycle"),
        ("GET", "/api/managed-environments/signed-in-user"),
        ("GET", "/api/managed-environments/network-step/status"),
        ("POST", "/api/managed-environments/network-step/apply"),
        ("POST", "/api/managed-environments/env-1/stop"),
        ("POST", "/api/managed-environments/env-1/ssh-session"),
        ("POST", "/api/managed-environments/env-1/idle-timeout"),
        ("POST", "/api/managed-environments/env-1/rename"),
        ("POST", "/api/managed-environments/env-1/start-crew"),
        ("POST", "/api/managed-environments/env-1/set-up-crew"),
        ("GET", "/api/managed-environments/env-1/connect-phase"),
    ]

    async def run():
        app = web.Application()
        register_sandbox_routes(app, SandboxRoutes(no_engine, run_in_thread=False))
        async with TestClient(TestServer(app)) as client:
            for method, path in calls:
                resp = await client.request(method, path, json={})
                assert resp.status == 404, (method, path)

    asyncio.run(run())


def test_the_sign_in_profile_needs_the_lane_on_and_both_values(monkeypatch):
    monkeypatch.setattr(settings, "read_settings", REAL_READ_SETTINGS)
    on = {"crew_client": True}
    url, arn = "https://example-org.example.test/start", FULL_SETTINGS.profile_arn
    assert settings.sign_in_profile(_cfg(on)) is None
    assert settings.sign_in_profile(_cfg(on, managed_env_start_url=url)) is None
    assert (
        settings.sign_in_profile(_cfg({}, managed_env_start_url=url, managed_env_profile_arn=arn))
        is None
    )
    profile = settings.sign_in_profile(
        _cfg(on, managed_env_start_url=url, managed_env_profile_arn=arn)
    )
    from kiro_crew.auth.login.endpoints import OIDC_SCOPES

    assert profile == {
        "scopes": list(OIDC_SCOPES),
        "grant_types": ["urn:ietf:params:oauth:grant-type:device_code", "refresh_token"],
        "issuer_url": url,
        "profile_arn": arn,
    }
    scoped = settings.sign_in_profile(
        _cfg(on, managed_env_start_url=url, managed_env_profile_arn=arn, managed_env_scopes=["a:b"])
    )
    assert scoped is not None and scoped["scopes"] == ["a:b"]


def test_the_contributor_hands_the_profile_to_the_core_by_start_url(monkeypatch):
    profile = {"scopes": ["s"], "grant_types": ["g"], "issuer_url": "u", "profile_arn": "a"}
    monkeypatch.setattr(settings, "sign_in_profile", lambda cfg=None: profile)
    got = ManagedEnvDashboardContributor().idc_sign_in_profiles()
    assert got == {FULL_SETTINGS.start_url.rstrip("/"): profile}


def test_the_config_keys_load_empty_by_default_and_as_written(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "read_settings", REAL_READ_SETTINGS)
    from kiro_crew.config.section_builders import _build_instances_config

    empty = _build_instances_config(None, {}, None)
    assert settings.read_settings(SimpleNamespace(instances=empty)) == settings.LaneSettings()
    written = _build_instances_config(
        None,
        {
            "managed_env_endpoint": " https://managed-env.example.test/ ",
            "managed_env_scopes": ["a:b", "", 3],
            "managed_env_mcp_registry_mode": "true",  # only the JSON true turns it on
            "managed_env_box_env_drop": "NOT_A_LIST",
            "managed_env_kas_region": 7,
        },
        None,
    )
    got = settings.read_settings(SimpleNamespace(instances=written))
    assert got.endpoint == "https://managed-env.example.test"
    assert got.scopes == ("a:b",) and got.mcp_registry_mode is False
    assert got.box_env_drop == () and got.kas_region == ""


@pytest.mark.parametrize(
    "missing,keys",
    [
        ({"endpoint": ""}, ["instances.managed_env_endpoint"]),
        ({"endpoint": "http://managed-env.example.test"}, ["instances.managed_env_endpoint"]),
        ({"profile_arn": "not-an-arn"}, ["instances.managed_env_profile_arn"]),
        (
            {"kas_endpoint": "", "kas_region": "nowhere", "box_token_path": "relative/token"},
            [
                "instances.managed_env_kas_endpoint",
                "instances.managed_env_kas_region",
                "instances.managed_env_box_token_path",
            ],
        ),
    ],
)
def test_a_launch_with_a_value_missing_is_refused_by_name_before_any_call(tmp_path, missing, keys):
    bff = FakeBff()
    try:
        ft = FakeTime()
        values = {**FULL_SETTINGS.__dict__, **missing}
        lane = settings.LaneSettings(**values)
        client = BffClient(
            lambda: "t",
            SandboxGuard(tmp_path / "state", now=ft.now),
            endpoint=bff.endpoint,
            profile_arn=lane.profile_arn,
            sleep=ft.sleep,
            clock=ft.clock,
        )
        engine = SandboxLaunchEngine(client=client, settings=lambda: lane)
        with pytest.raises(SandboxEngineError) as ei:
            engine.preflight("crew-one", "")
        assert ei.value.code == "not_configured"
        for key in keys:
            assert key in str(ei.value)
        with pytest.raises(SandboxEngineError):
            engine.provision(tag="kc-0a1b2c", profile="crew-one")
        assert engine.teardown(tag="kc-0a1b2c", profile="crew-one", region="") is True
        assert bff.requests == []  # nothing was sent, nothing was created
    finally:
        bff.close()


def test_the_client_sends_nothing_without_an_endpoint_and_a_profile(tmp_path, monkeypatch):
    ft = FakeTime()
    client = BffClient(
        lambda: "t",
        SandboxGuard(tmp_path / "state", now=ft.now),
        settings=lambda: settings.LaneSettings(),
        sleep=ft.sleep,
        clock=ft.clock,
    )
    with pytest.raises(SandboxError) as ei:
        client.list_sandboxes()
    assert ei.value.kind == "not_configured"
    assert "instances.managed_env_endpoint" in str(ei.value)
    assert "instances.managed_env_profile_arn" in str(ei.value)
    assert client.call_seq == 0 and client._guard.stopped == ""


def test_the_ssh_region_is_refused_by_name_when_unset():
    with pytest.raises(settings.SettingsMissing) as ei:
        settings.LaneSettings(ssh_region="").require_ssh()
    assert ei.value.keys == ("instances.managed_env_ssh_region",)
    assert FULL_SETTINGS.require_ssh() == "zz-example-1"


def test_the_machine_values_reach_machine_config_as_written():
    from kiro_crew.managed_env import install

    cell = FULL_SETTINGS.machine_cell()
    mc = install.MachineConfig(
        cell=cell,
        token_path=FULL_SETTINGS.box_token_path,
        token_env=FULL_SETTINGS.box_token_env,
        env_drop=FULL_SETTINGS.box_env_drop,
        registry_mode=FULL_SETTINGS.mcp_registry_mode,
    )
    args = dict(install.machine_config_args(mc))
    assert args["agent.kas_endpoint"] == FULL_SETTINGS.kas_endpoint
    assert args["agent.kas_control_plane_endpoint"] == FULL_SETTINGS.kas_control_plane_endpoint
    assert args["agent.kas_region"] == FULL_SETTINGS.kas_region
    assert args["agent.kas_token_path"] == FULL_SETTINGS.box_token_path
    assert args["agent.kas_auth"] == "machine"
