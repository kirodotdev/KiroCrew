"""The ``crew_client`` preview: an edition's own IAM Identity Center sign-in profile.

Off (the default) is ``main``: the edition is not asked. On, a start URL the edition
lists signs in with its own form and pinned profile, and while it is the stored
sign-in no local KAS agent runs: the spawn refuses, and the turn gate answers 503
before a rerun cuts history, without asking kiro-cli.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from kiro_crew import edition_locks
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.session_handle import AcpRuntimeError
from kiro_crew.auth import bridge
from kiro_crew.auth.login import builder_id, control_plane, endpoints
from kiro_crew.auth.service import KasLoginService
from kiro_crew.auth.store import TokenStore
from kiro_crew.dashboard import kiro_readiness
from kiro_crew.dashboard.handlers import kas_login, sessions

_URL = "https://example-org.awsapps.com/start"
_PROFILE = "example-profile-id"


class _Edition:
    """A fake installed edition: its ``DashboardContributor`` seam answers from here."""

    def __init__(self) -> None:
        self.profiles: dict = {}
        self.dashboard = self

    def idc_sign_in_profiles(self) -> dict:
        return self.profiles


@pytest.fixture(autouse=True)
def edition(_floor_monkeypatch):
    fake = _Edition()
    fake.profiles[_URL + "/"] = {
        "scopes": ["s:a"],
        "grant_types": ["device_code", "refresh_token"],
        "issuer_url": _URL,
        "profile_arn": _PROFILE,
    }
    _floor_monkeypatch.setattr("kiro_crew.platform.context.installed_context", lambda: fake)
    return fake


@pytest.fixture
def preview(monkeypatch):
    def set_on(on: bool) -> None:
        monkeypatch.setattr(edition_locks, "preview_on", lambda name, cfg=None: on)

    set_on(True)
    return set_on


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr(bridge, "vault_identity_claims", lambda: ("fp", {"start_url": _URL}))


@pytest.mark.parametrize(
    "previews,on",
    [({"crew_client": True}, True), ({}, False), ({"crew_client": "yes"}, False), (None, False)],
)
def test_the_switch_is_an_exact_true(previews, on):
    cfg = SimpleNamespace(dashboard=SimpleNamespace(feature_previews=previews))
    assert edition_locks.preview_on("crew_client", cfg) is on


def test_with_the_preview_off_the_edition_is_not_asked(edition, preview, monkeypatch):
    preview(False)
    edition.idc_sign_in_profiles = lambda: pytest.fail("edition asked")
    monkeypatch.setattr(bridge, "vault_identity_claims", lambda: pytest.fail("vault read"))
    assert endpoints.idc_profile(_URL) is None
    assert endpoints.stored_sign_in_registered() is False
    assert kas_login._local_agent_off_start_urls() == []


def test_a_contributor_that_raises_keeps_the_public_sign_in(edition, preview):
    def _broken():
        raise RuntimeError("edition bug")

    edition.idc_sign_in_profiles = _broken
    assert endpoints.idc_profile(_URL) is None


@pytest.mark.asyncio
async def test_the_registration_body(preview):
    resp = mock.MagicMock(status=200)
    resp.__aenter__.return_value = resp
    resp.__aexit__.return_value = False
    resp.json = mock.AsyncMock(return_value={"clientId": "cid", "clientSecret": "csec"})
    session = mock.MagicMock(**{"post.return_value": resp})
    client = await builder_id.register_client(
        "us-east-1", session=session, form=endpoints.idc_profile(_URL)
    )
    assert session.post.call_args.kwargs["json"] == {
        "clientName": "Kiro Crew",
        "clientType": "public",
        "scopes": ["s:a"],
        "grantTypes": ["device_code", "refresh_token"],
        "issuerUrl": _URL,
    }
    assert client.scopes == ("s:a",)
    assert kas_login._local_agent_off_start_urls() == [_URL]


@pytest.mark.asyncio
async def test_the_pinned_profile_asks_no_control_plane(preview, tmp_path, monkeypatch):
    client = builder_id.RegisteredClient(client_id="cid", client_secret="csec")
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    auth = builder_id.DeviceAuthorization("dc", "UC", "https://d", "https://d/?c", expires, 0.0)
    created = {"accessToken": "at", "refreshToken": "rt", "expiresIn": 3600}
    token = builder_id._token_from_create(
        created, client, "us-east-1", "identity_center", "Enterprise"
    )
    monkeypatch.setattr(builder_id, "register_client", mock.AsyncMock(return_value=client))
    monkeypatch.setattr(builder_id, "start_device_authorization", mock.AsyncMock(return_value=auth))
    monkeypatch.setattr(builder_id, "poll_token_once", mock.AsyncMock(return_value=token))
    lookup = mock.AsyncMock(side_effect=LookupError("the control plane was asked"))
    monkeypatch.setattr(control_plane, "list_available_profiles", lookup)
    service = KasLoginService(TokenStore(tmp_path), session=mock.MagicMock())
    begin = await service.begin_device("idc", start_url=_URL, region="us-east-1")
    result = await service.poll_device(begin["login_id"])
    assert result == {"status": "authorized", "provider": "Enterprise"}
    lookup.assert_not_called()
    assert TokenStore(tmp_path).resolve().profile_arn == _PROFILE


@pytest.mark.asyncio
async def test_the_usage_refresh_makes_no_call_on_a_registered_sign_in(
    preview, signed_in, monkeypatch
):
    for name in ("_usage_cache", "_usage_cache_ts", "_usage_cache_vault_fp"):
        monkeypatch.setattr(sessions, name, getattr(sessions, name))
    monkeypatch.setattr(bridge, "default_token_store", lambda: pytest.fail("vault read"))
    await sessions._refresh_from_vault()
    assert sessions._usage_cache == {"available": False}


@pytest.mark.parametrize("bundle", [False, True], ids=["relay", "bundle"])
@pytest.mark.asyncio
async def test_a_local_spawn_refuses_on_a_registered_sign_in(
    preview, signed_in, tmp_path, monkeypatch, bundle
):
    monkeypatch.setattr("kiro_crew.acp.harness.kas._bundle_route_selected", lambda: bundle)
    ctx = SpawnContext(agent="kirocrew", work_dir=tmp_path, model=None, environ={}, home=tmp_path)
    with pytest.raises(AcpRuntimeError, match="chat with the agent on this computer"):
        await KasHarness().resolve_spawn(ctx)


def _rerun(backend: str):
    request = mock.MagicMock()
    applied = SimpleNamespace(agent=SimpleNamespace(acp_backend=backend))
    request.app = {"state": SimpleNamespace(sessions=SimpleNamespace(_cfg=applied))}
    request.path = "/api/chat/rewind"
    return request


@pytest.mark.parametrize("backend", ["kas", "kiro"])
@pytest.mark.asyncio
async def test_the_turn_gate_refuses_before_history_is_cut(
    preview, signed_in, monkeypatch, backend
):
    """A rerun's gate runs before it cuts history. On KAS it answers 503 for this
    sign-in, with no engine installed and kiro-cli signed in, and never asks kiro-cli;
    a turn on kiro-cli keeps the kiro-cli gate."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers.configured_backend_for_session",
        lambda key, cfg: cfg.agent.acp_backend,
    )
    monkeypatch.setattr("kiro_crew.agent_sdk.kas_bundle.installed_bundle", lambda: None)
    monkeypatch.setattr(kiro_readiness, "kas_crew_signin_ready", mock.AsyncMock(return_value=False))
    kiro_cli = mock.AsyncMock(return_value=True)  # kiro-cli is signed in
    monkeypatch.setattr(kiro_readiness, "kiro_verified_ready", kiro_cli)
    resp = await kiro_readiness.reject_if_kiro_unverified(
        _rerun(backend), harness_turn=True, session_key="chat-1", turn_reuses_session=False
    )
    if backend == "kiro":
        assert resp is None
        kiro_cli.assert_awaited_once()
        return
    assert resp is not None and resp.status == 503
    assert json.loads(resp.body)["code"] == kiro_readiness.KAS_TURN_REFUSED_CODE
    kiro_cli.assert_not_called()


@pytest.mark.asyncio
async def test_a_live_relay_rewind_is_refused_before_history_is_cut(
    preview, signed_in, monkeypatch
):
    """A rewind that may keep a live KAS relay process (no engine bundle) still runs
    the refusal: its replacement spawn would refuse this sign-in, so the gate answers
    503 before the rewind cuts history, with kiro-cli signed in and never asked."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers.configured_backend_for_session",
        lambda key, cfg: cfg.agent.acp_backend,
    )
    kiro_cli = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(kiro_readiness, "kiro_verified_ready", kiro_cli)
    relay = SimpleNamespace(acp_backend=ACP_BACKEND_KAS, _kas_engine_bundle=False)
    request = _rerun("kas")
    request.app["state"].sessions.get_provider = lambda key: SimpleNamespace(_runtime=relay)
    assert kiro_readiness.live_turn_route(request.app["state"], "chat-1") is False
    resp = await kiro_readiness.reject_if_kiro_unverified(
        request, harness_turn=True, session_key="chat-1", may_keep_live_process=True
    )
    assert resp is not None and resp.status == 503
    assert json.loads(resp.body)["code"] == kiro_readiness.KAS_TURN_REFUSED_CODE
    kiro_cli.assert_not_called()


@pytest.mark.asyncio
async def test_a_retained_kiro_cli_session_keeps_the_kiro_cli_gate(preview, signed_in, monkeypatch):
    """A turn that only reuses a live kiro-cli session is judged by that session's own
    harness: switching the configured backend to KAS does not refuse it."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers.configured_backend_for_session",
        lambda key, cfg: cfg.agent.acp_backend,
    )
    kiro_cli = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(kiro_readiness, "kiro_verified_ready", kiro_cli)
    live = SimpleNamespace(acp_backend=ACP_BACKEND_KIRO)
    request = _rerun("kas")  # the configured default is now KAS
    request.app["state"].sessions.get_provider = lambda key: SimpleNamespace(_runtime=live)
    resp = await kiro_readiness.reject_if_kiro_unverified(
        request, harness_turn=True, session_key="chat-1"
    )
    assert resp is None
    kiro_cli.assert_awaited_once()


@pytest.mark.parametrize("configured", ["kiro", "kas"])
@pytest.mark.asyncio
async def test_a_dead_kas_runtime_is_judged_by_the_configured_backend(
    preview, signed_in, monkeypatch, configured
):
    """``get_or_create`` replaces a dead provider from the config, so the gate does too:
    switched to kiro-cli, the turn keeps the kiro-cli gate; still on KAS, it is refused
    with 503 before history is cut, and kiro-cli is not asked."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers.configured_backend_for_session",
        lambda key, cfg: cfg.agent.acp_backend,
    )
    kiro_cli = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(kiro_readiness, "kiro_verified_ready", kiro_cli)
    dead = SimpleNamespace(acp_backend=ACP_BACKEND_KAS, _kas_engine_bundle=False)
    request = _rerun(configured)
    sessions = request.app["state"].sessions
    sessions.get_provider = lambda key: SimpleNamespace(_runtime=dead)
    sessions.is_provider_alive = mock.AsyncMock(return_value=False)
    resp = await kiro_readiness.reject_if_kiro_unverified(
        request, harness_turn=True, session_key="chat-1"
    )
    if configured == "kiro":
        assert resp is None
        kiro_cli.assert_awaited_once()
        return
    assert resp is not None and resp.status == 503
    assert json.loads(resp.body)["code"] == kiro_readiness.KAS_TURN_REFUSED_CODE
    kiro_cli.assert_not_called()


@pytest.mark.asyncio
async def test_with_the_preview_off_the_gate_asks_no_liveness(preview, monkeypatch):
    """Off, the gate is ``main``'s: the dead-provider read is the refusal's alone."""
    preview(False)
    kiro_cli = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(kiro_readiness, "kiro_verified_ready", kiro_cli)
    request = _rerun("kas")
    request.app["state"].sessions.get_provider = lambda key: None
    request.app["state"].sessions.is_provider_alive = mock.AsyncMock(side_effect=AssertionError)
    await kiro_readiness.reject_if_kiro_unverified(request, harness_turn=True, session_key="chat-1")
    request.app["state"].sessions.is_provider_alive.assert_not_called()


@pytest.mark.asyncio
async def test_the_profile_is_read_off_the_event_loop(preview, tmp_path, monkeypatch):
    import threading

    from kiro_crew.auth import service

    threads = []

    def _profile(url):
        threads.append(threading.current_thread() is threading.main_thread())
        return None

    monkeypatch.setattr(service, "idc_profile", _profile)
    monkeypatch.setattr(builder_id, "register_client", mock.AsyncMock(side_effect=LookupError))
    svc = KasLoginService(TokenStore(tmp_path), session=mock.MagicMock())
    with pytest.raises(LookupError):
        await svc.begin_device("idc", start_url=_URL)
    assert threads == [False]
