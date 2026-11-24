"""The lane in the stock build: the provider row, the sign-in profile and the credential."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.managed_env import PROVISIONER_ID, core_signin
from kiro_crew.managed_env import settings as s
from kiro_crew.managed_env.provider import (
    ManagedEnvDashboardContributor,
    ManagedEnvProvisionerProvider,
)

START = "https://start.example.com/start"
ARN = "arn:aws:codewhisperer:ex-test-1:000000000000:profile/EXAMPLE"


def _cfg(on: bool, **inst):
    base = dict(managed_env_start_url=START, managed_env_profile_arn=ARN)
    return SimpleNamespace(
        dashboard=SimpleNamespace(feature_previews={"crew_client": on}),
        instances=SimpleNamespace(**{**base, **inst}),
    )


@pytest.fixture
def config(monkeypatch):
    state = {"cfg": _cfg(True)}
    monkeypatch.setattr(s, "_config", lambda cfg: cfg if cfg is not None else state["cfg"])
    return state


class _Base:
    def provisioners(self):
        return []

    def engine_for(self, pid, *, confirmed_recipient=""):
        raise KeyError(pid)


def test_the_launch_row_and_its_engine_follow_the_preview_and_machine_mode(config, monkeypatch):
    from kiro_crew.managed_env import machine

    shipped = {"on": True}
    monkeypatch.setattr(machine, "shipped", lambda: shipped["on"])
    provider = ManagedEnvProvisionerProvider(_Base())
    assert [p.id for p in provider.provisioners()] == [PROVISIONER_ID]
    assert provider.engine_for(PROVISIONER_ID) is not None
    shipped["on"] = False  # a release without machine mode offers no row
    assert provider.provisioners() == []
    shipped["on"] = True
    config["cfg"] = _cfg(False)
    assert provider.provisioners() == []
    with pytest.raises(KeyError):
        provider.engine_for(PROVISIONER_ID)


def test_machine_mode_is_shipped_only_with_every_key():
    import dataclasses

    from kiro_crew.managed_env import machine

    @dataclasses.dataclass
    class WithAll:
        kas_auth: str = ""
        kas_bundle: bool = False
        kas_endpoint: str = ""
        kas_control_plane_endpoint: str = ""
        kas_region: str = ""
        kas_token_path: str = ""
        kas_machine_env_drop: list = dataclasses.field(default_factory=list)

    assert machine.shipped(WithAll) is True

    @dataclasses.dataclass
    class WithSome:
        kas_bundle: bool = False

    assert machine.shipped(WithSome) is False
    assert machine.shipped() in (True, False)  # the real class reads without error


def test_the_start_url_is_matched_in_the_sign_ins_own_form(config):
    config["cfg"] = _cfg(True, managed_env_start_url="https://example-org.awsapps.com")
    (url,) = ManagedEnvDashboardContributor().idc_sign_in_profiles()
    assert url == "https://example-org.awsapps.com/start"
    lane = s.read_settings()
    assert lane.is_lane_start_url("https://example-org.awsapps.com/start/")
    assert not lane.is_lane_start_url("https://other.awsapps.com/start")
    assert s.canonical_start_url("not a url") == ""


def test_the_sign_in_profile_needs_the_preview_the_start_url_and_the_profile(config):
    profiles = ManagedEnvDashboardContributor().idc_sign_in_profiles()
    assert profiles[START.rstrip("/")]["profile_arn"] == ARN
    for cfg in (
        _cfg(False),
        _cfg(True, managed_env_profile_arn=""),
        _cfg(True, managed_env_start_url="http://x"),
    ):
        config["cfg"] = cfg
        assert ManagedEnvDashboardContributor().idc_sign_in_profiles() == {}


def _token(identity="identity_center", url=START, access="tok"):
    return SimpleNamespace(
        identity=identity,
        extra={"start_url": url + "/"},
        access_token=access,
        refresh_token="r",
        client_id="c",
        profile_arn=ARN,
    )


@pytest.mark.parametrize(
    "token, ok",
    [
        (_token(), True),
        (_token(identity="builder_id"), False),
        (_token(url="https://other.example.com/start"), False),
        (_token(access=""), False),
    ],
)
def test_only_the_lanes_own_sign_in_is_a_credential(config, monkeypatch, token, ok):
    import kiro_crew.lts_sign_in as lts

    monkeypatch.setattr(lts, "run_coroutine", lambda coro: (coro.close(), token)[1])
    monkeypatch.setattr("kiro_crew.auth.provider.kas_profile_arn", lambda t: t.profile_arn)
    if ok:
        cred = core_signin.vault_credential()
        assert cred.bearer == "tok" and cred.profile_arn == ARN and "tok" not in repr(cred)
    else:
        with pytest.raises(core_signin.NotLaneSignIn):
            core_signin.vault_credential()


def test_a_sign_in_that_cannot_be_renewed_is_refused_plainly(config, monkeypatch):
    import kiro_crew.lts_sign_in as lts
    from kiro_crew.auth.refresh import RefreshRejected

    def rejected(coro):
        coro.close()
        raise RefreshRejected("expired")

    monkeypatch.setattr(lts, "run_coroutine", rejected)
    with pytest.raises(core_signin.NotLaneSignIn) as ei:
        core_signin.vault_credential()
    assert str(ei.value) == core_signin.NOT_SIGNED_IN
