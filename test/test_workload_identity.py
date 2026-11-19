"""A Crew on its own workload identity: signed in, ready and leasing with no person.

The JWT-SVID comes from a file a sidecar writes (``kiro_crew.workload_identity``).
Each test writes a synthetic one, so no real issuer is needed. Every person
sign-in reader is replaced by one that fails the test, so a pass also proves the
vault was never read or renewed.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kiro_prerequisite_helpers import _agents_dir_never_the_real_home  # noqa: F401
from kiro_prerequisite_helpers import _isolate_agent_specs  # noqa: F401
from kiro_prerequisite_helpers import _FakeRuntime, _make_executable, _no_audit
from test_relay_client_lease import FakeLeaseService, answer

from kiro_crew import workload_identity as wi
from kiro_crew.acp import kas_host_auth
from kiro_crew.auth import bridge
from kiro_crew.auth.provider import KasAuthProvider
from kiro_crew.relay_client import lease as lease_module
from kiro_crew.relay_client import lease_signin
from kiro_crew.relay_client.provider import (
    RelaySettings,
    RelaySettingsError,
    lease_token_source,
    public_tunnel_provider,
)
from kiro_crew.testing.links import make_dir_link

AUDIENCE = "https://runtime.us-east-1.example.test"
PROFILE_ARN = "arn:aws:codewhisperer:us-east-1:000000000000:profile/WORKLOADTEST"
SUBJECT = "spiffe://example.test/ns/crew/sa/crew"


def _b64(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


#: Expiries far from any test run's clock, so no test reads the clock.
LATER, LONG_AGO = 4_000_000_000, 1_000_000_000


def make_svid(*, sub: str = SUBJECT, aud: Any = AUDIENCE, exp: int = LATER) -> str:
    claims = {"sub": sub, "aud": aud, "exp": exp}
    return f"{_b64({'alg': 'ES256', 'typ': 'JWT'})}.{_b64(claims)}.c2lnbmF0dXJl"


@pytest.fixture
def svid_file(tmp_path: Path) -> Path:
    path = tmp_path / "jwt_svid.token"
    path.write_text(make_svid() + "\n", encoding="ascii")
    return path


@pytest.fixture
def workload(monkeypatch: pytest.MonkeyPatch, svid_file: Path) -> Path:
    """The switch on with good settings, and every person sign-in reader poisoned."""
    monkeypatch.setenv(wi.ENV_MODE, "spiffe")
    monkeypatch.setenv(wi.ENV_SVID_FILE, str(svid_file))
    monkeypatch.setenv(wi.ENV_AUDIENCE, AUDIENCE)
    monkeypatch.setenv(wi.ENV_PROFILE_ARN, PROFILE_ARN)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a person sign-in was read")

    async def aboom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a person sign-in was read")

    monkeypatch.setattr(bridge, "default_token_store", boom)
    monkeypatch.setattr(KasAuthProvider, "current", aboom)
    monkeypatch.setattr(lease_signin, "read_lease_sign_in", aboom)
    from kiro_crew import lts_sign_in

    monkeypatch.setattr(lts_sign_in, "select", boom)
    monkeypatch.setattr(lts_sign_in, "resolve", boom)
    return svid_file


def _settings(path: Path) -> wi.Settings:
    return wi.Settings(svid_file=str(path), audience=AUDIENCE, profile_arn=PROFILE_ARN)


# ── settings and the file ──


def test_the_switch_is_off_unless_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(wi.ENV_MODE, raising=False)
    assert wi.active() is False
    # Any value counts, so a typo fails closed instead of signing in as a person.
    assert wi.active({wi.ENV_MODE: "spife"}) is True


@pytest.mark.parametrize(
    "change, names",
    [
        ({wi.ENV_MODE: "spife"}, wi.ENV_MODE),
        ({wi.ENV_SVID_FILE: "relative/jwt"}, wi.ENV_SVID_FILE),
        ({wi.ENV_AUDIENCE: "http://plain"}, wi.ENV_AUDIENCE),
        ({wi.ENV_PROFILE_ARN: "not-an-arn"}, wi.ENV_PROFILE_ARN),
    ],
)
def test_a_bad_setting_is_named(change: dict, names: str, tmp_path: Path) -> None:
    env = {
        wi.ENV_MODE: "spiffe",
        wi.ENV_SVID_FILE: str(tmp_path / "jwt"),
        wi.ENV_AUDIENCE: AUDIENCE,
        wi.ENV_PROFILE_ARN: PROFILE_ARN,
        **change,
    }
    with pytest.raises(wi.WorkloadIdentityError, match=names):
        wi.Settings.from_env(env)
    assert wi.ready(env) is False


def test_a_good_file_reads_its_subject_and_hides_the_token(svid_file: Path) -> None:
    svid = wi.read_svid(_settings(svid_file))
    assert svid.subject == SUBJECT and svid.token == svid_file.read_text().strip()
    assert svid.token not in repr(svid)


@pytest.mark.parametrize(
    "content, why",
    [
        (make_svid(aud="https://someone-else.example.test"), "audience"),
        (make_svid(aud=[AUDIENCE, "https://other.example.test"]), None),
        (make_svid(sub="https://not-spiffe.example.test"), "subject"),
        (make_svid(exp=LONG_AGO), "expire"),
        ("not a jwt", "no JWT"),
        ("a." + base64.urlsafe_b64encode(b"[1]").decode() + ".b", "parse"),
        ("x" * (wi.SVID_MAX_BYTES + 1), "too large"),
    ],
)
def test_a_file_that_cannot_sign_in_says_why_without_the_token(
    tmp_path: Path, content: str, why: Any
) -> None:
    path = tmp_path / "jwt"
    path.write_text(content, encoding="ascii")
    if why is None:
        assert wi.read_svid(_settings(path)).subject == SUBJECT
        return
    with pytest.raises(wi.WorkloadIdentityError, match=why) as caught:
        wi.read_svid(_settings(path))
    assert content not in str(caught.value)


def test_a_missing_file_names_only_the_error_type(tmp_path: Path) -> None:
    with pytest.raises(wi.WorkloadIdentityError, match="FileNotFoundError"):
        wi.read_svid(_settings(tmp_path / "absent"))


def test_the_file_is_read_again_for_each_use(tmp_path: Path) -> None:
    # The sidecar rewrites the file before the token ends; nothing is cached.
    path = tmp_path / "jwt"
    first, second = make_svid(exp=LATER), make_svid(exp=LATER + 1)
    path.write_text(first, encoding="ascii")
    assert wi.read_svid(_settings(path)).token == first
    path.write_text(second, encoding="ascii")
    assert wi.read_svid(_settings(path)).token == second


# ── KAS sign-in ──


@pytest.mark.asyncio
async def test_the_engine_gets_the_svid_with_no_auth_method(workload: Path) -> None:
    got = await kas_host_auth.answer_get_access_token()
    assert got["accessToken"] == workload.read_text().strip()
    assert got["profileArn"] == PROFILE_ARN and got["provider"] == "Enterprise"
    # No authMethod, so the engine sends no TokenType header.
    assert "authMethod" not in got and got["expiresAt"].endswith("+00:00")


@pytest.mark.asyncio
async def test_an_unusable_file_fails_closed_and_logs_no_token(
    workload: Path, caplog: pytest.LogCaptureFixture
) -> None:
    token = make_svid(aud="https://someone-else.example.test")
    workload.write_text(token, encoding="ascii")
    with caplog.at_level(logging.WARNING):
        with pytest.raises(kas_host_auth.HostAuthCallbackError, match="workload identity"):
            await kas_host_auth.answer_get_access_token()
    assert "audience" in caplog.text and token not in caplog.text


@pytest.mark.asyncio
async def test_a_bad_switch_value_never_falls_back_to_the_vault(
    workload: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(wi.ENV_MODE, "spife")
    with pytest.raises(kas_host_auth.HostAuthCallbackError, match="workload identity"):
        await kas_host_auth.answer_get_access_token()
    assert bridge.vault_holds_identity() is True


@pytest.mark.asyncio
async def test_the_spawn_is_crew_owned_and_no_sign_in_is_needed(workload: Path) -> None:
    assert bridge.vault_holds_identity() is True
    assert await kas_host_auth.vault_holds_identity_off_loop() is True
    assert await kas_host_auth.sign_in_refused("any-digest") is False


@pytest.mark.asyncio
async def test_the_sign_in_refresher_does_not_run(workload: Path) -> None:
    class _NeverSet:
        def is_set(self) -> bool:
            return False

        async def wait(self) -> bool:
            raise AssertionError("the refresher loop ran")

    from kiro_crew.dashboard import sign_in_refresher

    await sign_in_refresher.run_sign_in_refresher(_NeverSet())


def test_the_model_catalog_is_keyed_by_the_profile(
    workload: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent_sdk import kas_model_catalog

    key = kas_model_catalog.vault_identity()
    assert key and key == wi.catalog_identity()
    monkeypatch.setenv(wi.ENV_PROFILE_ARN, PROFILE_ARN.replace("WORKLOADTEST", "OTHER"))
    assert kas_model_catalog.vault_identity() not in ("", key)


@pytest.mark.asyncio
async def test_the_turn_gate_asks_the_file(workload: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.dashboard import kiro_readiness

    monkeypatch.setattr(kiro_readiness, "_kas_bundle_turn_sync", lambda *a: True)
    assert await kiro_readiness.kas_crew_signin_ready(None) is True
    workload.unlink()
    assert await kiro_readiness.kas_crew_signin_ready(None) is False


def test_the_kas_route_counts_the_workload_as_crews_sign_in(
    workload: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.acp import client, kas_route

    monkeypatch.setattr(client, "_resolve_kiro_bin", lambda **kw: "/opt/kiro/kiro-cli")
    monkeypatch.setattr(kas_route, "crew_entry_skips_launcher", lambda *a: True)
    # The dashboard serves the engine's list on the relay Crew's sign-in starts.
    assert kas_route.crew_sign_in_starts_spawn() is True
    # A bundle spawn is never told to sign in: the workload is its sign-in.
    assert kas_route.bundle_route_lacks_sign_in() is False


@pytest.mark.asyncio
async def test_models_come_from_the_engine_never_kiro_cli(
    workload: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard.handlers import agents, models_cold_probe

    def no(*a: Any, **kw: Any) -> Any:
        raise AssertionError("kiro-cli or a probe was asked")

    agent = SimpleNamespace(provider="acp", acp_backend="kas", kas_bundle=False, model="")
    monkeypatch.setattr(agents.KiroCrewConfig, "load", lambda: SimpleNamespace(agent=agent))
    monkeypatch.setattr("kiro_crew.crewmate_harness.models_route", lambda *a: ("kas", False))
    monkeypatch.setattr(agents, "reject_if_kiro_unverified", no)
    monkeypatch.setattr(agents, "_fetch_kiro_catalog", no)
    monkeypatch.setattr(models_cold_probe, "start_cold_model_probe", no)
    request = SimpleNamespace(app={}, query={})
    resp = await agents.api_models(request)
    assert resp.status == 200 and json.loads(resp.body)[0]["model_name"] == "auto"


@pytest.mark.asyncio
async def test_the_usage_poll_reads_no_person_token(
    workload: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard.handlers import sessions

    class _StatOnly:
        def write_stamp(self) -> str:
            return "stamp"

    # Only the vault's write stamp (a stat) may be taken; any token read fails.
    monkeypatch.setattr(bridge, "default_token_store", lambda: _StatOnly())
    published: list = []
    monkeypatch.setattr(sessions, "_publish_usage", published.append)
    await sessions._refresh_from_vault()
    assert published and published[-1]["available"] is False


# ── relay lease ──


def _cfg(url: str, credential: str) -> Any:
    tunnel = SimpleNamespace(
        relay_enabled=True,
        relay_lease_url=url,
        relay_lease_credential=credential,
        relay_lease_token_type="",
    )
    return SimpleNamespace(tunnel=tunnel)


@pytest.fixture
def no_agent_identity_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform seam reports none, so only the workload switch can refuse."""
    from kiro_crew.platform import context as context_module

    seam = SimpleNamespace(agent_identity=SimpleNamespace(enabled=lambda: False))
    monkeypatch.setattr(context_module, "current_context", lambda: seam)


@pytest.mark.asyncio
async def test_an_empty_lease_credential_is_refused_not_the_sign_in(
    workload: Path, no_agent_identity_seam: None
) -> None:
    with pytest.raises(RelaySettingsError, match="tunnel.relay_lease_credential is not set"):
        RelaySettings.from_config(_cfg("", "")).validate()
    # The person's sign-in named outright is refused too: no personal lease on a workload host.
    with pytest.raises(RelaySettingsError, match="runs on its workload identity"):
        RelaySettings.from_config(_cfg("", "kiro-sign-in")).validate()
    provider = public_tunnel_provider(_cfg("", ""))
    await provider.start()
    try:
        snapshot = provider.status_snapshot()
        assert snapshot is not None and "relay_lease_credential is not set" in snapshot["error"]
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_the_lease_carries_the_svid_file_with_no_token_type_or_profile(
    workload: Path, no_agent_identity_seam: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lease_module, "_data_home", lambda: tmp_path)
    token = workload.read_text().strip()
    async with FakeLeaseService({token}) as lease:
        settings = RelaySettings.from_config(_cfg(lease.url, str(workload)))
        settings.validate()
        source = lease_token_source(settings)
        assert source is not None
        lease.queue(200, answer(0))
        try:
            assert (await source.current()).token == answer(0)["hostToken"]
        finally:
            await source.aclose()
    (renew,), (release,) = lease.requests, lease.releases
    assert renew["auth"] == release["auth"] == f"Bearer {token}"
    assert renew["token_type"] is None and release["token_type"] is None
    assert "profileArn" not in renew["body"] and "profileArn" not in release["body"]


# ── readiness ──


@pytest.mark.asyncio
async def test_the_kiro_cli_probe_stays_kiro_clis_own_answer(
    tmp_path: Path, svid_file: Path
) -> None:
    """A usable JWT-SVID never makes kiro-cli read as signed in: a kiro-cli turn
    cannot use it, and a destructive rerun trusts this answer."""
    from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

    executable = tmp_path / "toolbox" / "bin" / "kiro-cli"
    _make_executable(executable)
    runtime = _FakeRuntime(executable)
    runtime.installed = True
    runtime.authenticated = False
    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={
            "HOME": str(tmp_path),
            "PATH": str(executable.parent),
            wi.ENV_MODE: "spiffe",
            wi.ENV_SVID_FILE: str(svid_file),
            wi.ENV_AUDIENCE: AUDIENCE,
            wi.ENV_PROFILE_ARN: PROFILE_ARN,
        },
        home=tmp_path,
        process_runner=runtime.run,
        audit_writer=_no_audit,
    )
    status = await service.snapshot(force=True)
    assert status["installed"] is True
    assert status["authenticated"] is False and status["ready"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "usable", "bundle", "installed", "ready"),
    [
        ("kas", True, False, False, True),
        ("kas", True, True, True, True),
        ("kas", True, True, False, False),
        ("kas", False, False, False, False),
        ("kiro", True, False, False, False),
    ],
    ids=["relay", "bundle", "bundle-not-installed", "svid-expired", "kiro-cli"],
)
async def test_a_kas_turn_is_ready_on_a_usable_svid_and_only_a_kas_turn(
    workload: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    usable: bool,
    bundle: bool,
    installed: bool,
    ready: bool,
) -> None:
    from kiro_crew import crewmate_harness, members
    from kiro_crew.agent_sdk import kas_bundle
    from kiro_crew.config import loader
    from kiro_crew.dashboard import chat_handlers, kiro_readiness

    if not usable:
        workload.write_text(make_svid(exp=LONG_AGO), encoding="ascii")
    monkeypatch.setattr(chat_handlers, "configured_backend_for_session", lambda key, cfg: backend)
    monkeypatch.setattr(
        loader.KiroCrewConfig, "load", classmethod(lambda cls: SimpleNamespace(agent=None))
    )
    monkeypatch.setattr(members, "session_backend_pick", lambda key: None)
    monkeypatch.setattr(crewmate_harness, "route_pin", lambda cfg, key, pick: None)
    monkeypatch.setattr(kas_bundle, "kas_spawn_takes_bundle", lambda agent, pin: bundle)
    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: object() if installed else None)
    config = SimpleNamespace()
    assert await kiro_readiness.kas_crew_signin_ready("s1", None, config=config) is ready
    # A live kiro-cli session keeps the kiro-cli gate.
    assert await kiro_readiness.kas_crew_signin_ready("s1", False, config=config) is False


@pytest.mark.parametrize(
    ("backend", "route"), [("kas", True), ("", False)], ids=["kas-relay", "kiro-cli"]
)
def test_on_a_workload_host_a_live_kas_relay_session_is_served_by_crew(
    workload: Path, backend: str, route: bool
) -> None:
    from kiro_crew.dashboard import kiro_readiness

    runtime = SimpleNamespace(acp_backend=backend, _kas_engine_bundle=False)
    provider = SimpleNamespace(_runtime=runtime)
    state = SimpleNamespace(sessions=SimpleNamespace(get_provider=lambda key: provider))
    assert kiro_readiness.live_turn_route(state, "s1") is route


# ── the expiry and the file kind ──


def _svid_with_raw_exp(raw_exp: str) -> str:
    """A JWT-SVID whose ``exp`` is written as *raw_exp* in the claims JSON."""
    body = f'{{"sub": "{SUBJECT}", "aud": "{AUDIENCE}", "exp": {raw_exp}}}'
    payload = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    return f"{_b64({'alg': 'ES256'})}.{payload}.c2lnbmF0dXJl"


@pytest.mark.parametrize(
    "raw_exp, why",
    [
        ("NaN", "finite"),
        ("Infinity", "finite"),
        ("-Infinity", "finite"),
        ("1e400", "finite"),
        ("1" + "0" * 400, "finite"),
        ("1e20", "9999"),
        ("1e12", "9999"),
        ("253402300800", "9999"),
        ("253402300799", None),
    ],
)
def test_an_expiry_that_is_not_a_usable_time_is_refused_before_the_engine_sees_it(
    tmp_path: Path, raw_exp: str, why: Any
) -> None:
    path = tmp_path / "jwt"
    path.write_text(_svid_with_raw_exp(raw_exp), encoding="ascii")
    env = {
        wi.ENV_MODE: "spiffe",
        wi.ENV_SVID_FILE: str(path),
        wi.ENV_AUDIENCE: AUDIENCE,
        wi.ENV_PROFILE_ARN: PROFILE_ARN,
    }
    now = LATER - 3600
    if why is None:
        assert wi.read_svid(_settings(path), now=now).expires_at == float(raw_exp)
        assert wi.kas_access_token_answer(env)["expiresAt"].startswith("9999-12-31")
        return
    with pytest.raises(wi.WorkloadIdentityError, match=why):
        wi.read_svid(_settings(path), now=now)
    # ready() and the engine's answer agree: both refuse, neither raises another error.
    assert wi.ready(env) is False
    with pytest.raises(wi.WorkloadIdentityError):
        wi.kas_access_token_answer(env)


def test_claims_nested_too_deep_are_refused_not_raised(tmp_path: Path) -> None:
    payload = base64.urlsafe_b64encode(b"[" * 6000).decode().rstrip("=")
    path = tmp_path / "jwt"
    path.write_text(f"a.{payload}.b", encoding="ascii")
    with pytest.raises(wi.WorkloadIdentityError, match="parse"):
        wi.read_svid(_settings(path))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are a POSIX file kind")
def test_a_fifo_at_the_path_is_refused_without_waiting_for_a_writer(tmp_path: Path) -> None:
    fifo = tmp_path / "jwt"
    os.mkfifo(fifo)
    outcome: list = []

    def read() -> None:
        try:
            wi.read_svid(_settings(fifo))
        except wi.WorkloadIdentityError as exc:
            outcome.append(str(exc))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(10)
    if reader.is_alive():
        # Release the blocked open so the thread ends, then fail.
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        reader.join(10)
        pytest.fail("reading the JWT-SVID file blocked on a FIFO")
    assert outcome and "NotRegularFileError" in outcome[0]


def test_a_folder_at_the_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(wi.WorkloadIdentityError, match="could not be read"):
        wi.read_svid(_settings(tmp_path))


def test_a_linked_folder_is_followed_like_a_mounted_secret(tmp_path: Path, svid_file: Path) -> None:
    # A Kubernetes secret or projected volume reaches its file through a linked
    # ``..data`` folder; a junction on Windows, which needs no privilege.
    mounted = tmp_path / "mounted"
    make_dir_link(mounted, svid_file.parent)
    assert wi.read_svid(_settings(mounted / svid_file.name)).subject == SUBJECT


def test_a_missing_audience_says_to_set_it(tmp_path: Path) -> None:
    env = {
        wi.ENV_MODE: "spiffe",
        wi.ENV_SVID_FILE: str(tmp_path / "jwt"),
        wi.ENV_PROFILE_ARN: PROFILE_ARN,
    }
    with pytest.raises(wi.WorkloadIdentityError, match=f"{wi.ENV_AUDIENCE} is not set"):
        wi.Settings.from_env(env)


# ── the setup gate for the people a relay brings in ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "usable", "kas_turn", "blocker", "ready"),
    [
        (False, True, True, {}, True),
        (True, True, True, {}, True),
        (False, False, True, {}, False),
        (False, True, False, {}, False),
        (False, True, True, {"repair_required": True}, False),
        (False, True, True, {"missing_agent_specs": ["kirocrew"]}, False),
        (False, True, True, {"acp_supported": False}, False),
        (False, True, True, {"sandbox_unavailable": True}, False),
        (False, True, True, {"sandbox_backend_available": False}, False),
        (
            False,
            True,
            True,
            {"sandbox_backend_available": False, "unsandboxed_exec_permitted": True},
            True,
        ),
        (False, True, True, {"sandbox_blocked_backends": ["kas"]}, False),
    ],
    ids=[
        "teammate",
        "owner",
        "svid-expired",
        "kiro-cli-turn",
        "repair",
        "missing-spec",
        "cli-too-old",
        "sandbox-refused",
        "no-sandbox-backend",
        "unsandboxed-opt-in",
        "kas-blocked",
    ],
)
async def test_a_fresh_pod_lets_teammates_in_when_chat_would_work(
    workload: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: bool,
    usable: bool,
    kas_turn: bool,
    blocker: dict,
    ready: bool,
) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard import kiro_readiness
    from kiro_crew.dashboard.handlers.kiro_prerequisite import api_kiro_prerequisite_status
    from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

    if not usable:
        workload.write_text(make_svid(exp=LONG_AGO), encoding="ascii")
    monkeypatch.setattr(kiro_readiness, "_kas_turn_sync", lambda *a: kas_turn)
    # A fresh pod: kiro-cli never signed in, first-run setup never written.
    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={"HOME": str(tmp_path), "PATH": ""},
        home=tmp_path,
        audit_writer=_no_audit,
    )

    async def signed_out(*, force: bool = False, coalesce: bool = False) -> dict:
        base = {"installed": True, "authenticated": False, "ready": False}
        return {**base, "sandbox_backend_available": True, **blocker}

    monkeypatch.setattr(service, "snapshot", signed_out)

    @web.middleware
    async def identity(request: web.Request, handler: Any) -> web.StreamResponse:
        request["user"] = "owner" if owner else "teammate"
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = SimpleNamespace(owner_id="owner")
    app["kiro_prerequisite_service"] = service
    app.router.add_get("/api/kiro-prerequisite", api_kiro_prerequisite_status)
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/kiro-prerequisite")).json()
    assert body["ready"] is ready
    assert body["authenticated"] is False
    assert body["setup_allowed"] is owner


@pytest.mark.asyncio
async def test_off_a_workload_host_the_gate_keeps_kiro_clis_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard import kiro_readiness

    monkeypatch.delenv(wi.ENV_MODE, raising=False)

    def no(*a: Any) -> bool:
        raise AssertionError("the turn route was read off a workload host")

    monkeypatch.setattr(kiro_readiness, "_kas_turn_sync", no)
    assert await kiro_readiness.workload_turns_ready() is False
