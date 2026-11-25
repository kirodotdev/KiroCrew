"""Hosting by default: a personal sign-in, a team key or a workload identity hosts unchosen.

``tunnel.relay_enabled`` unset lets the Crew's credential decide; a stored value
always wins; a personal sign-in hosts; a sign-in that can never host holds
quietly; the owner cannot turn it off over the relay; a test or seeded gateway
never registers; and the first default connect says so once.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from test_relay_client_lease import FakeLeaseService, answer

from kiro_crew.config import section_builders
from kiro_crew.relay_client import lease as lease_module
from kiro_crew.relay_client import lease_signin
from kiro_crew.relay_client.lease import LeaseNotSignedIn, LeaseSignInCannotHost
from kiro_crew.relay_client.lease_signin import LeaseSignIn
from kiro_crew.relay_client.provider import RelaySettings, lease_token_source
from kiro_crew.relay_client.token_source import HostTokenRetryLater
from kiro_crew.remote_control import hosting

KEY_FILE = "/srv/crew/team.key"


def _tunnel(enabled: Any = None, credential: str = "", token_type: str = "") -> Any:
    return SimpleNamespace(
        relay_enabled=enabled,
        relay_lease_credential=credential,
        relay_lease_token_type=token_type,
    )


@pytest.fixture(autouse=True)
def _no_workload_identity(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.delenv(hosting.WORKLOAD_IDENTITY_ENV, raising=False)


# ── the rule ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tunnel", "hosts"),
    [
        # Unset: a team key and a personal sign-in host.
        (_tunnel(None, KEY_FILE, "API_KEY"), True),
        (_tunnel(None, "secret://team-key", "API_KEY"), True),
        (_tunnel(None), True),
        (_tunnel(None, "kiro-sign-in"), True),
        (_tunnel(None, KEY_FILE), False),
        (_tunnel(None, KEY_FILE, "EXTERNAL_IDP"), False),
        (_tunnel(None, "", "API_KEY"), False),
        # A stored value wins over the credential.
        (_tunnel(False, KEY_FILE, "API_KEY"), False),
        (_tunnel(True), True),
        # A hand edit that is not a boolean is off, never truthy or unset.
        (_tunnel("yes", KEY_FILE, "API_KEY"), False),
        (_tunnel(1, KEY_FILE, "API_KEY"), False),
    ],
)
def test_relay_requested(tunnel: Any, hosts: bool) -> None:
    assert hosting.relay_requested(tunnel) is hosts


def test_a_stand_in_config_object_is_off() -> None:
    assert hosting.relay_requested(MagicMock()) is False


def test_a_workload_identity_with_a_credential_hosts_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(hosting.WORKLOAD_IDENTITY_ENV, "spiffe")
    assert hosting.relay_requested(_tunnel(None, "/run/svid/jwt"))
    assert hosting.hosting_by_default(_tunnel(None, "/run/svid/jwt"))
    # It still needs the credential, and a stored off still wins.
    assert not hosting.relay_requested(_tunnel(None))
    assert not hosting.relay_requested(_tunnel(False, "/run/svid/jwt"))


def test_hosting_by_default_is_only_the_unset_case() -> None:
    assert hosting.hosting_by_default(_tunnel(None, KEY_FILE, "API_KEY"))
    assert not hosting.hosting_by_default(_tunnel(True, KEY_FILE, "API_KEY"))
    assert hosting.hosting_by_default(_tunnel(None))
    assert not hosting.hosting_by_default(_tunnel(True))


@pytest.mark.parametrize(
    ("tunnel", "workload", "hosts"),
    [
        # A personal sign-in, empty or kiro-sign-in, hosts when nobody chose.
        (_tunnel(None), False, True),
        (_tunnel(None, "kiro-sign-in"), False, True),
        # Not a personal sign-in: a token type, or a workload identity named.
        (_tunnel(None, "", "API_KEY"), False, False),
        (_tunnel(None, "", "EXTERNAL_IDP"), False, False),
        (_tunnel(None, "kiro-sign-in", "API_KEY"), False, False),
        (_tunnel(None), True, False),
        (_tunnel(None, "kiro-sign-in"), True, False),
        # A stored value still wins.
        (_tunnel(False), False, False),
        (_tunnel(False, "kiro-sign-in"), False, False),
        (_tunnel(True), False, True),
    ],
)
def test_a_personal_sign_in_hosts_by_default(
    monkeypatch: pytest.MonkeyPatch, tunnel: Any, workload: bool, hosts: bool
) -> None:
    if workload:
        monkeypatch.setenv(hosting.WORKLOAD_IDENTITY_ENV, "spiffe")
    assert hosting.relay_requested(tunnel) is hosts
    assert hosting.hosting_by_default(tunnel) is (hosts and tunnel.relay_enabled is None)


# ── the quiet rule: a default-only personal Crew that cannot host yet ───────


def test_only_a_default_personal_crew_is_quiet() -> None:
    assert hosting.personal_by_default(_tunnel(None))
    assert hosting.personal_by_default(_tunnel(None, "kiro-sign-in"))
    # Someone chose, or it is a team key: today's errors.
    assert not hosting.personal_by_default(_tunnel(True))
    assert not hosting.personal_by_default(_tunnel(None, KEY_FILE, "API_KEY"))
    assert not hosting.personal_by_default(_tunnel(None, "", "API_KEY"))


#: The text of the sign-in hold (lease._hold_cannot_host), an eligibility wait.
_NOT_SIGNED_IN = "the Crew is not signed in to Kiro"
_TIMED_OUT = "the lease service could not be reached (TimeoutError); next try in 4s"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "credential", "token_type", "relay", "state", "error", "held", "reason"),
    [
        # Cannot host YET, an eligibility hold it leaves on its own or at its next start.
        (None, "", "", True, "reconnecting", _NOT_SIGNED_IN, True, "personal_default"),
        (
            None,
            "kiro-sign-in",
            "",
            True,
            "stopped",
            "bound to '192.0.2.10'",
            True,
            "personal_default",
        ),
        # Not failing yet: starting, or stopped with no error.
        (None, "kiro-sign-in", "", True, "starting", "", False, "personal_default"),
        (None, "", "", True, "stopped", "", False, "personal_default"),
        # Loud: a fault on the way to the lease service, the same as an opted-in Crew.
        (None, "", "", True, "reconnecting", _TIMED_OUT, False, ""),
        # A final failure (a relay that refuses this version) and connected: shown.
        (None, "", "", True, "error", _NOT_SIGNED_IN, False, ""),
        (None, "", "", True, "connected", "", False, ""),
        # Loud: an explicit on, a team key, or an edition's own tunnel.
        (True, "", "", True, "reconnecting", _NOT_SIGNED_IN, True, ""),
        (None, KEY_FILE, "API_KEY", True, "reconnecting", _NOT_SIGNED_IN, True, ""),
        (None, "", "", False, "reconnecting", _NOT_SIGNED_IN, True, ""),
    ],
)
async def test_the_tunnel_status_marks_a_default_personal_crew_quiet(
    monkeypatch: pytest.MonkeyPatch,
    enabled: Any,
    credential: str,
    token_type: str,
    relay: bool,
    state: str,
    error: str,
    held: bool,
    reason: str,
) -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers import tunnel as handler
    from kiro_crew.tunnel.manager import TunnelState, TunnelStatus

    cfg = SimpleNamespace(tunnel=_tunnel(enabled, credential, token_type))
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    provider = SimpleNamespace(kiro_relay=True) if relay else SimpleNamespace()
    context = SimpleNamespace(tunnel=provider)
    monkeypatch.setattr("kiro_crew.platform.context.current_context", lambda: context)
    mgr = MagicMock()
    mgr.status = TunnelStatus(state=TunnelState(state), error=error, cannot_host_yet=held)
    request = MagicMock()
    request.app = {"state": SimpleNamespace(tunnel_manager=mgr)}
    body = json.loads((await handler.api_tunnel_status(request)).body)
    assert body["reason"] == reason
    # The reason never hides why: the error text is the same either way.
    assert body["error"] == error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "bind", "level", "state"),
    [
        # A default personal Crew on a bind the relay cannot reach: stopped, quietly.
        (None, True, logging.WARNING, "stopped"),
        # Someone chose: today's error.
        (True, True, logging.ERROR, "error"),
        # Any other unusable setting stays an error, by default or not.
        (None, False, logging.ERROR, "error"),
    ],
)
async def test_a_bind_the_relay_cannot_reach_is_quiet_only_by_default(
    caplog: pytest.LogCaptureFixture, enabled: Any, bind: bool, level: int, state: str
) -> None:
    from kiro_crew.relay_client.provider import (
        RelayBindUnreachable,
        RelaySettingsError,
        RelayTunnelProvider,
    )

    problem = "the gateway is bound to '192.0.2.10'"

    def refuse() -> Any:
        raise (RelayBindUnreachable if bind else RelaySettingsError)(problem)

    settings = RelaySettings.from_config(SimpleNamespace(tunnel=_tunnel(enabled)))
    provider = RelayTunnelProvider(settings, ingress_factory=refuse)
    caplog.set_level(logging.DEBUG, logger="kiro_crew.relay_client.provider")
    await provider.start()
    said = [r for r in caplog.records if "Remote Control relay not started" in r.getMessage()]
    assert [r.levelno for r in said] == [level]
    # The status keeps the reason either way.
    # Only the quiet bind is marked as a wait on this Crew rather than a fault.
    assert provider.status_snapshot() == {
        "state": state,
        "url": "",
        "error": problem,
        "cannot_host_yet": state == "stopped",
    }


def test_the_gateway_listener_refuses_a_specific_bind_as_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.relay_client.provider import RelayBindUnreachable, gateway_listener

    monkeypatch.setenv("KIROCREW_BOUND_PORT", "6123")
    monkeypatch.setenv("KIROCREW_BOUND_HOST", "192.0.2.10")
    with pytest.raises(RelayBindUnreachable, match="bound to '192.0.2.10'"):
        gateway_listener()


def test_the_leaf_names_match_the_lease_client() -> None:
    assert hosting.SIGN_IN_CREDENTIAL == lease_module.SIGN_IN_CREDENTIAL
    assert hosting.TOKEN_TYPE_API_KEY == lease_module.TOKEN_TYPE_API_KEY


def test_the_leaf_names_the_workload_identity_switch_its_module_reads() -> None:
    from kiro_crew import workload_identity

    assert hosting.WORKLOAD_IDENTITY_ENV == workload_identity.ENV_MODE


@pytest.mark.parametrize(
    ("stored", "loaded"),
    [({}, None), ({"relay_enabled": None}, None), ({"relay_enabled": True}, True)]
    + [({"relay_enabled": v}, False) for v in (False, "true", 1, [])],
)
def test_the_section_builder_keeps_three_values(stored: dict, loaded: Any) -> None:
    assert section_builders._build_tunnel_config(stored).relay_enabled is loaded


def test_relay_selected_follows_the_rule_and_lts_still_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew import edition, edition_locks

    team = SimpleNamespace(tunnel=_tunnel(None, KEY_FILE, "API_KEY"))
    personal = SimpleNamespace(tunnel=_tunnel(None, "kiro-sign-in"))
    assert edition_locks.relay_selected(team)
    assert edition_locks.relay_selected(personal)
    monkeypatch.setattr(edition, "is_lts", lambda: True)
    assert not edition_locks.relay_selected(team)
    assert not edition_locks.relay_selected(personal)


def test_relay_settings_enable_an_unset_team_crew() -> None:
    cfg = SimpleNamespace(tunnel=_tunnel(None, KEY_FILE, "API_KEY"))
    assert RelaySettings.from_config(cfg).enabled
    cfg = SimpleNamespace(tunnel=_tunnel(None, "", "API_KEY"))
    assert not RelaySettings.from_config(cfg).enabled
    cfg = SimpleNamespace(tunnel=_tunnel(None))
    assert RelaySettings.from_config(cfg).enabled


def test_a_fresh_config_stores_unset_and_off_writes_false(tmp_path, monkeypatch) -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.remote_control import setting

    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    monkeypatch.setattr("kiro_crew.config.loader.config_local_path", lambda: tmp_path / "l.json")
    cfg = KiroCrewConfig.load()
    cfg.tunnel.relay_lease_credential = KEY_FILE
    cfg.tunnel.relay_lease_token_type = "API_KEY"
    cfg.save()
    stored = json.loads(path.read_text(encoding="utf-8"))["tunnel"]
    # A save writes what is stored, never what the rule answers.
    assert stored["relay_enabled"] is None
    assert setting.requested() and setting.by_default()
    setting.turn_off()
    assert json.loads(path.read_text(encoding="utf-8"))["tunnel"]["relay_enabled"] is False
    assert not setting.requested() and not setting.by_default()


def test_status_says_when_it_is_on_only_by_default(tmp_path, monkeypatch, capsys) -> None:
    from kiro_crew.remote_control.cli import BY_DEFAULT_NOTICE, run

    path = tmp_path / "config.json"
    tunnel = {"relay_lease_credential": KEY_FILE, "relay_lease_token_type": "API_KEY"}
    path.write_text(json.dumps({"tunnel": tunnel}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    monkeypatch.setattr("kiro_crew.config.loader.config_local_path", lambda: tmp_path / "l.json")
    args = argparse.Namespace(remote_control_action="status", json=False)
    with pytest.raises(SystemExit):
        run(args)
    assert capsys.readouterr().out == f"Remote Control: on\n   {BY_DEFAULT_NOTICE}\n"


def _workload_host(monkeypatch: pytest.MonkeyPatch, region: str) -> None:
    monkeypatch.setenv("KIROCREW_WORKLOAD_IDENTITY", "spiffe")
    monkeypatch.setenv("KIROCREW_WORKLOAD_IDENTITY_SVID_FILE", "/run/stand-in/svid")
    monkeypatch.setenv("KIROCREW_WORKLOAD_IDENTITY_AUDIENCE", "https://runtime.example.test")
    monkeypatch.setenv(
        "KIROCREW_WORKLOAD_IDENTITY_PROFILE_ARN",
        f"arn:aws:codewhisperer:{region}:000000000000:profile/STANDINPROFILE",
    )


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_status_says_a_crew_key_whose_region_differs_from_the_profile_is_on(
    tmp_path, monkeypatch, capsys, as_json: bool
) -> None:
    # The profile ARN is the JWT-SVID lease's; a crew key leases from its setting.
    from kiro_crew.remote_control.cli import BY_DEFAULT_NOTICE, run

    _workload_host(monkeypatch, "eu-central-1")
    path = tmp_path / "config.json"
    tunnel = {"relay_lease_credential": KEY_FILE, "relay_lease_region": "us-east-1"}
    path.write_text(json.dumps({"tunnel": tunnel}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    monkeypatch.setattr("kiro_crew.config.loader.config_local_path", lambda: tmp_path / "l.json")
    with pytest.raises(SystemExit):
        run(argparse.Namespace(remote_control_action="status", json=as_json))
    out = capsys.readouterr().out
    if as_json:
        assert json.loads(out) == {"requested": True, "locked": False, "by_default": True}
    else:
        assert out == f"Remote Control: on\n   {BY_DEFAULT_NOTICE}\n"


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_status_says_a_jwt_svid_crew_whose_region_differs_from_its_profile_is_on(
    tmp_path, monkeypatch, capsys, as_json: bool
) -> None:
    # A JWT-SVID lease leases from its setting too: the profile ARN never picks it.
    from kiro_crew.remote_control.cli import BY_DEFAULT_NOTICE, run

    _workload_host(monkeypatch, "eu-central-1")
    path = tmp_path / "config.json"
    tunnel = {"relay_lease_credential": "/run/stand-in/svid", "relay_lease_region": "us-east-1"}
    path.write_text(json.dumps({"tunnel": tunnel}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    monkeypatch.setattr("kiro_crew.config.loader.config_local_path", lambda: tmp_path / "l.json")
    with pytest.raises(SystemExit):
        run(argparse.Namespace(remote_control_action="status", json=as_json))
    out = capsys.readouterr().out
    if as_json:
        assert json.loads(out) == {"requested": True, "locked": False, "by_default": True}
    else:
        assert out == f"Remote Control: on\n   {BY_DEFAULT_NOTICE}\n"


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_status_says_a_jwt_svid_crew_with_an_unusable_region_cannot_start(
    tmp_path, monkeypatch, capsys, as_json: bool
) -> None:
    from kiro_crew.auth.login.control_plane import CONTROL_PLANE_REGIONS
    from kiro_crew.remote_control.cli import BY_DEFAULT_NOTICE, run

    _workload_host(monkeypatch, "eu-central-1")
    path = tmp_path / "config.json"
    tunnel = {"relay_lease_credential": "/run/stand-in/svid", "relay_lease_region": "xx-test-9"}
    path.write_text(json.dumps({"tunnel": tunnel}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    monkeypatch.setattr("kiro_crew.config.loader.config_local_path", lambda: tmp_path / "l.json")
    with pytest.raises(SystemExit):
        run(argparse.Namespace(remote_control_action="status", json=as_json))
    out = capsys.readouterr().out
    why = (
        "tunnel.relay_lease_region names no Region the built-in lease service covers "
        f"({', '.join(CONTROL_PLANE_REGIONS)}): name one, or set tunnel.relay_lease_url"
    )
    if as_json:
        assert json.loads(out) == {
            "requested": True,
            "locked": False,
            "by_default": True,
            "problem": why,
        }
    else:
        assert out == f"Remote Control: can't start\n   {why}\n   {BY_DEFAULT_NOTICE}\n"


# ── test and seeded gateways ─────────────────────────────────────────────────


def _gateway_args(**overrides: Any) -> argparse.Namespace:
    base = dict(
        port=None,
        json_ready=False,
        approval=None,
        no_open=False,
        test_mode=False,
        slack_only=False,
        no_crons=False,
        no_tunnel=False,
        seed=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.mark.parametrize(
    ("overrides", "no_tunnel"),
    [
        ({}, False),
        ({"no_tunnel": True}, True),
        ({"test_mode": True}, True),
        ({"seed": "minimal"}, True),
    ],
)
def test_test_mode_and_seed_never_publish(overrides: dict, no_tunnel: bool) -> None:
    from kiro_crew.cli_gateway_boot import _resolve_gateway_args

    assert _resolve_gateway_args(_gateway_args(**overrides))["no_tunnel"] is no_tunnel


# ── a sign-in that can never host ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refusal", "level"),
    [
        (LeaseSignInCannotHost("the sign-in is Builder ID"), logging.WARNING),
        # A Crew that was never signed in is held the same way, said at INFO.
        (LeaseNotSignedIn("the Crew is not signed in to Kiro"), logging.INFO),
    ],
)
async def test_a_sign_in_that_cannot_host_holds_quietly_and_a_new_sign_in_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    refusal: Exception,
    level: int,
) -> None:
    from kiro_crew.platform.context import current_context

    monkeypatch.setattr(current_context().agent_identity, "enabled", lambda: False)
    monkeypatch.setattr(lease_module, "_data_home", lambda: tmp_path)
    fail: list[Exception] = [refusal]

    async def read(rejected: Any = None) -> LeaseSignIn:
        if fail:
            raise fail[0]
        return LeaseSignIn(bearer="signin-bearer-7", token_type=None, profile_arn="arn:p")

    monkeypatch.setattr(lease_signin, "read_lease_sign_in", read)
    now = [1000.0]
    caplog.set_level(logging.DEBUG, logger=lease_module.__name__)
    async with FakeLeaseService({"signin-bearer-7"}) as lease:
        cfg = SimpleNamespace(tunnel=_tunnel(True, "kiro-sign-in"))
        cfg.tunnel.relay_lease_url = lease.url
        source = lease_token_source(RelaySettings.from_config(cfg))
        source._monotonic = lambda: now[0]
        try:
            for _ in range(5):
                with pytest.raises(HostTokenRetryLater, match=str(refusal)) as later:
                    await source.current()
                # A wait on this Crew's own sign-in, not a fault on the way.
                assert later.value.cannot_host_yet is True
                # Asked again inside the hold: still the Crew's own wait.
                with pytest.raises(HostTokenRetryLater, match="next try in") as again:
                    await source.current()
                assert again.value.cannot_host_yet is True
                now[0] += lease_module.SIGN_IN_CANNOT_HOST_RECHECK_SECONDS
            # No request reached the service, and the state was said once.
            assert lease.requests == [] and not source.stopped_reason
            held = [r for r in caplog.records if "Remote Control held" in r.getMessage()]
            assert len(held) == 1 and held[0].levelno == level
            if level == logging.INFO:
                # A Crew that was never signed in logs no WARNING at all.
                assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
            # Within one step of a new sign-in, hosting resumes.
            fail.clear()
            lease.queue(200, answer(0))
            assert (await source.current()).token == answer(0)["hostToken"]
        finally:
            await source.aclose()


@pytest.mark.asyncio
async def test_a_builder_id_sign_in_raises_cannot_host() -> None:
    token = SimpleNamespace(
        identity="builder_id", profile_arn="arn:p", access_token="t", auth_method="social"
    )

    class _Provider:
        async def current(self, rejected_access_token: Any = None) -> Any:
            return token

    with pytest.raises(LeaseSignInCannotHost):
        await lease_signin.read_lease_sign_in(provider=_Provider())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "never"),
    [
        ("NotAuthenticated", True),
        ("IdentitySignedOut", True),
        ("RefreshRejected", False),
        ("RefreshBlocked", False),
    ],
)
async def test_no_sign_in_holds_like_builder_id(kind: str, never: bool) -> None:
    from kiro_crew.auth import provider, refresh

    failure: Exception = getattr(provider if kind == "NotAuthenticated" else refresh, kind)("x")

    class _Provider:
        async def current(self, rejected_access_token: Any = None) -> Any:
            raise failure

    with pytest.raises(LeaseSignInCannotHost) as held:
        await lease_signin.read_lease_sign_in(provider=_Provider())
    # Only a Crew with no sign-in at all is said at INFO.
    assert isinstance(held.value, LeaseNotSignedIn) is never


@pytest.mark.asyncio
async def test_a_sign_in_with_no_kiro_profile_holds_like_builder_id() -> None:
    token = SimpleNamespace(
        identity="identity_center", profile_arn="", access_token="t", auth_method="sso"
    )

    class _Provider:
        async def current(self, rejected_access_token: Any = None) -> Any:
            return token

    with pytest.raises(LeaseSignInCannotHost, match="no Kiro profile") as held:
        await lease_signin.read_lease_sign_in(provider=_Provider())
    assert not isinstance(held.value, LeaseNotSignedIn)


# ── off over the relay ───────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def relayed_client(monkeypatch, tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.config import live
    from kiro_crew.dashboard.handlers import remote_control as handler

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    monkeypatch.setattr("kiro_crew.config.loader.config_local_path", lambda: tmp_path / "l.json")
    monkeypatch.setattr(live, "_WATCH", None)

    async def _owner(_request, _op):
        return None

    audit = MagicMock()
    monkeypatch.setattr(handler, "require_owner_dashboard_request", _owner)
    monkeypatch.setattr(handler, "sel", lambda: audit)
    relayed = {"on": True}
    monkeypatch.setattr(
        handler, "published_relay_caller", lambda _r: object() if relayed["on"] else None
    )
    app = web.Application()
    app.router.add_get("/api/remote-control", handler.api_remote_control_get)
    app.router.add_put("/api/remote-control", handler.api_remote_control_put)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client, path, audit, relayed


@pytest.mark.asyncio
async def test_off_over_the_relay_is_refused_and_audited_but_get_answers(relayed_client) -> None:
    client, path, audit, relayed = relayed_client
    body = await (await client.get("/api/remote-control")).json()
    assert body["requested"] is True and body["relayed"] is True
    resp = await client.put("/api/remote-control", json={"enabled": False})
    assert resp.status == 403
    assert (await resp.json())["code"] == "relay_remote_control_refused"
    assert json.loads(path.read_text(encoding="utf-8"))["tunnel"]["relay_enabled"] is True
    (call,) = audit.log_api_access.call_args_list
    assert call.kwargs["outcome"] == "denied" and "reason=relay" in call.kwargs["resources"]
    # The same owner at the machine can turn it off.
    relayed["on"] = False
    assert (await client.put("/api/remote-control", json={"enabled": False})).status == 200
    assert json.loads(path.read_text(encoding="utf-8"))["tunnel"]["relay_enabled"] is False


# ── the one-time notice ──────────────────────────────────────────────────────


class _Bus:
    def __init__(self) -> None:
        self.notes: list[Any] = []

    def push(self, payload: Any) -> dict:
        self.notes.append(payload)
        return {}


@pytest.mark.asyncio
async def test_the_default_notice_is_said_once(tmp_path, monkeypatch, caplog) -> None:
    from kiro_crew.remote_control import default_notice, setting

    monkeypatch.setattr(default_notice, "record_path", lambda: tmp_path / "notice.json")
    by_default = {"on": True}
    monkeypatch.setattr(setting, "by_default", lambda: by_default["on"])
    state = SimpleNamespace(notification_bus=_Bus())
    caplog.set_level(logging.WARNING, logger=default_notice.__name__)

    assert await default_notice.announce(state)
    (note,) = state.notification_bus.notes
    assert note.key == default_notice.KEY and "remote-control off" in note.body
    assert any("reachable through the Kiro relay" in r.getMessage() for r in caplog.records)
    # A restart, a later connect, or a deleted note: never again.
    assert not await default_notice.announce(state)
    assert len(state.notification_bus.notes) == 1


@pytest.mark.asyncio
async def test_no_notice_when_someone_chose(tmp_path, monkeypatch) -> None:
    from kiro_crew.remote_control import default_notice, setting

    monkeypatch.setattr(default_notice, "record_path", lambda: tmp_path / "notice.json")
    monkeypatch.setattr(setting, "by_default", lambda: False)
    state = SimpleNamespace(notification_bus=_Bus())
    assert not await default_notice.announce(state)
    assert state.notification_bus.notes == [] and not (tmp_path / "notice.json").exists()


@pytest.mark.asyncio
async def test_the_guard_runs_the_first_connect_callback_once() -> None:
    from kiro_crew.remote_control.guard import RemoteControlGuard

    class _Provider:
        state = "starting"

        async def ensure_available(self, *, install: bool = True) -> str:
            return self.state

    provider = _Provider()
    guard = RemoteControlGuard(MagicMock(), provider)
    calls: list[int] = []

    async def _cb() -> None:
        calls.append(1)

    guard.on_first_connect(_cb)
    await guard._maybe_first_connect()
    assert calls == []
    provider.state = "connected"
    await guard._maybe_first_connect()
    await guard._maybe_first_connect()
    assert calls == [1]


@pytest.mark.asyncio
async def test_a_note_that_was_not_stored_is_said_again(tmp_path, monkeypatch) -> None:
    import asyncio

    from kiro_crew.remote_control import default_notice, setting

    monkeypatch.setattr(default_notice, "record_path", lambda: tmp_path / "notice.json")
    monkeypatch.setattr(setting, "by_default", lambda: True)
    stored = {"ok": False}

    class _PersistBus(_Bus):
        def push(self, payload: Any) -> dict:
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            fut.set_result(stored["ok"])
            state.last_notification_persist = fut
            return super().push(payload)

    state = SimpleNamespace(notification_bus=_PersistBus(), last_notification_persist=None)
    assert not await default_notice.announce(state)
    assert not (tmp_path / "notice.json").exists()
    stored["ok"] = True
    assert await default_notice.announce(state)
    assert (tmp_path / "notice.json").exists()
