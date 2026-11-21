"""The ``crew_client`` feature preview: off is ``main``, on honours the edition hooks.

Each hook an edition can fill (an IAM Identity Center sign-in profile, a KAS engine
credential provider) is registered here, and every test runs with the preview both
off and on. Off, the registration body, the sign-in, the usage refresh and the KAS
spawn plan are the ones ``main`` produces; on, the hooks take effect. Each test
reddens when the gate it names is removed from the code.
"""

from __future__ import annotations

import json
from collections.abc import MutableMapping
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

import pytest

from kiro_crew import crew_client_preview
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.kas_transport import KAS_BUNDLE_AUTH_ARG
from kiro_crew.acp.session_handle import AcpRuntimeError
from kiro_crew.agent_sdk import kas_engine_auth
from kiro_crew.auth import bridge
from kiro_crew.auth.login import builder_id, control_plane, endpoints
from kiro_crew.auth.service import KasLoginService
from kiro_crew.auth.store import TokenStore
from kiro_crew.dashboard.handlers import sessions

_URL = "https://example-org.awsapps.com/start"
_PROFILE = "example-profile-id"
_DEFAULT_BODY = {
    "clientName": "Kiro Crew",
    "clientType": "public",
    "scopes": list(endpoints.OIDC_SCOPES),
}


class _Edition:
    """A fake installed edition: its ``DashboardContributor`` seam answers from here."""

    def __init__(self) -> None:
        self.profiles: dict = {}
        self.auth_factory = lambda: None
        self.dashboard = self

    def idc_sign_in_profiles(self) -> dict:
        return self.profiles

    def kas_engine_auth(self):
        return self.auth_factory()

    def profile(self, start_url, scopes, grants, issuer_url, profile_arn=""):
        self.profiles[start_url] = {
            "scopes": list(scopes),
            "grant_types": list(grants),
            "issuer_url": issuer_url,
            "profile_arn": profile_arn,
        }

    def engine(self, factory) -> None:
        self.auth_factory = factory if factory is not None else (lambda: None)


@pytest.fixture(autouse=True)
def edition(_floor_monkeypatch):
    """The edition the seam reads: none of its hooks set until a test sets them."""
    fake = _Edition()
    _floor_monkeypatch.setattr("kiro_crew.platform.context.installed_context", lambda: fake)
    return fake


@pytest.fixture
def preview(monkeypatch):
    def set_on(on: bool) -> None:
        monkeypatch.setattr(crew_client_preview, "preview_on", lambda cfg=None: on)

    return set_on


def _cfg(previews):
    return SimpleNamespace(dashboard=SimpleNamespace(feature_previews=previews))


@pytest.mark.parametrize(
    "previews,on",
    [({"crew_client": True}, True), ({}, False), ({"crew_client": "yes"}, False), (None, False)],
)
def test_the_switch_is_an_exact_true(previews, on):
    assert crew_client_preview.preview_on(_cfg(previews)) is on


def test_the_preview_is_a_known_flag():
    from kiro_crew.config.sections_dashboard import FEATURE_PREVIEW_FLAGS

    assert crew_client_preview.CREW_CLIENT_PREVIEW in FEATURE_PREVIEW_FLAGS


# ── The IAM Identity Center sign-in profile ────────────────────────────────────


@pytest.mark.parametrize("on", [False, True])
def test_a_registered_profile_reads_only_with_the_preview(edition, preview, on):
    preview(on)
    edition.profile(_URL + "/", ["s:a"], ["device_code"], _URL, _PROFILE)
    found = endpoints.idc_profile(_URL)
    assert (found is not None) is on
    assert endpoints.registered_start_url(_URL) is on
    assert endpoints.registered_start_url(_URL + "x") is False


def test_nothing_registered_reads_no_config(monkeypatch):
    monkeypatch.setattr(
        crew_client_preview, "preview_on", lambda cfg=None: pytest.fail("config read")
    )
    assert endpoints.idc_profile(_URL) is None


@pytest.mark.parametrize("on", [False, True])
@pytest.mark.asyncio
async def test_the_registration_body(edition, preview, on):
    preview(on)
    resp = mock.MagicMock(status=200)
    resp.__aenter__.return_value = resp
    resp.__aexit__.return_value = False
    resp.json = mock.AsyncMock(return_value={"clientId": "cid", "clientSecret": "csec"})
    session = mock.MagicMock(**{"post.return_value": resp})
    edition.profile(_URL, ["s:a"], ["device_code", "refresh_token"], _URL)
    client = await builder_id.register_client(
        "us-east-1", session=session, form=endpoints.idc_profile(_URL)
    )
    body = session.post.call_args.kwargs["json"]
    if on:
        extra = {
            "scopes": ["s:a"],
            "grantTypes": ["device_code", "refresh_token"],
            "issuerUrl": _URL,
        }
        assert body == {**_DEFAULT_BODY, **extra}
        assert client.scopes == ("s:a",)
    else:
        assert json.dumps(body) == json.dumps(_DEFAULT_BODY)


@pytest.mark.parametrize("on", [False, True])
@pytest.mark.asyncio
async def test_the_sign_in_start_passes_the_form_only_with_the_preview(
    edition, preview, on, tmp_path, monkeypatch
):
    preview(on)
    edition.profile(_URL, ["s:a"], ["device_code"], _URL)
    seen = []

    async def _register(region, *, session, scopes, form=None):
        seen.append(form)
        raise LookupError

    monkeypatch.setattr(builder_id, "register_client", _register)
    service = KasLoginService(TokenStore(tmp_path), session=mock.MagicMock())
    for url in (_URL, "https://other.awsapps.com/start"):
        with pytest.raises(LookupError):
            await service.begin_device("idc", start_url=url)
    assert seen == ([endpoints.idc_profile(_URL), None] if on else [None, None])


@pytest.mark.parametrize("on", [False, True])
@pytest.mark.asyncio
async def test_the_pinned_profile_asks_no_control_plane(
    edition, preview, on, tmp_path, monkeypatch
):
    preview(on)
    edition.profile(_URL, ["s:a"], ["device_code", "refresh_token"], _URL, _PROFILE)
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
    if not on:
        # main's path: the profile is resolved from the control plane.
        with pytest.raises(Exception):
            await service.poll_device(begin["login_id"])
        lookup.assert_called()
        return
    result = await service.poll_device(begin["login_id"])
    assert result == {"status": "authorized", "provider": "Enterprise"}
    lookup.assert_not_called()
    assert TokenStore(tmp_path).resolve().profile_arn == _PROFILE


# ── The fences on a registered sign-in ─────────────────────────────────────────


@pytest.fixture
def signed_in(edition, monkeypatch):
    monkeypatch.setattr(bridge, "vault_identity_claims", lambda: ("fp", {"start_url": _URL}))
    edition.profile(_URL, ["s:a"], ["device_code"], _URL)


@pytest.mark.parametrize("on", [False, True])
def test_the_fence_predicate(preview, signed_in, on):
    preview(on)
    assert endpoints.stored_sign_in_registered() is on


@pytest.mark.asyncio
async def test_the_usage_refresh_makes_no_call_on_a_registered_sign_in(
    preview, signed_in, monkeypatch
):
    preview(True)
    for name in ("_usage_cache", "_usage_cache_ts", "_usage_cache_vault_fp"):
        monkeypatch.setattr(sessions, name, getattr(sessions, name))
    monkeypatch.setattr(bridge, "default_token_store", lambda: pytest.fail("vault read"))
    await sessions._refresh_from_vault()
    assert sessions._usage_cache == {"available": False}


@pytest.mark.asyncio
async def test_the_usage_refresh_reads_the_vault_with_the_preview_off(
    preview, signed_in, monkeypatch
):
    preview(False)
    for name in ("_usage_cache", "_usage_cache_ts", "_usage_cache_vault_fp"):
        monkeypatch.setattr(sessions, name, getattr(sessions, name))

    class _Read(Exception):
        pass

    def _vault():
        raise _Read

    monkeypatch.setattr(bridge, "default_token_store", _vault)
    with pytest.raises(_Read):  # main's path: the vault is read
        await sessions._refresh_from_vault()


def _ctx(tmp_path):
    return SpawnContext(agent="kirocrew", work_dir=tmp_path, model=None, environ={}, home=tmp_path)


@pytest.fixture
def bundle_installed(monkeypatch, tmp_path):
    installed = SimpleNamespace(
        version="0.0.0",
        node_bin=str(tmp_path / "node"),
        acp_server=str(tmp_path / "acp-server.js"),
        node_version="0",
    )
    monkeypatch.setattr("kiro_crew.acp.kas_bundle.installed_bundle", lambda: installed)
    monkeypatch.setattr("kiro_crew.acp.harness.kas.ensure_engine_bundle_cache", lambda argv: None)
    monkeypatch.setattr("kiro_crew.acp.harness.kas._chat_sharing_preview_on", lambda: False)
    monkeypatch.setattr("kiro_crew.acp.harness.kas._bundle_route_selected", lambda: True)
    return installed


@pytest.mark.parametrize("bundle", [False, True], ids=["relay", "bundle"])
@pytest.mark.asyncio
async def test_a_local_spawn_refuses_on_a_registered_sign_in(preview, signed_in, tmp_path, bundle):
    preview(True)
    with patch("kiro_crew.acp.harness.kas._bundle_route_selected", return_value=bundle):
        with pytest.raises(AcpRuntimeError, match="isn't available on this sign-in"):
            await KasHarness().resolve_spawn(_ctx(tmp_path))


@pytest.mark.asyncio
async def test_a_registered_sign_in_spawns_as_main_with_the_preview_off(
    preview, signed_in, bundle_installed, tmp_path
):
    preview(False)
    plan = await KasHarness().resolve_spawn(_ctx(tmp_path))
    assert KAS_BUNDLE_AUTH_ARG in plan.argv and plan.host_auth is True


# ── The KAS engine credential hook ─────────────────────────────────────────────


class _Auth:
    relay_refusal = "this host's credential runs on the bundle route only"

    def __init__(self, refuse: str = "") -> None:
        self.refuse = refuse
        self.preflights = 0

    def preflight(self) -> None:
        self.preflights += 1
        if self.refuse:
            raise kas_engine_auth.EngineAuthRefused(self.refuse)

    def engine_args(self) -> list[str]:
        return ["--example-auth=host", "--example-file=/run/example"]

    def scrub_env(self, env: MutableMapping[str, str]) -> None:
        env.pop("EXAMPLE_TOKEN", None)

    def start_refusal(self, text: str, code: object) -> str:
        return "the host credential was refused" if "Refused" in text else ""


@pytest.mark.parametrize("on", [False, True])
@pytest.mark.asyncio
async def test_the_bundle_plan(edition, preview, bundle_installed, tmp_path, on):
    preview(on)
    auth = _Auth()
    edition.engine(lambda: auth)
    harness = KasHarness()
    plan = await harness.resolve_spawn(_ctx(tmp_path))
    if on:
        assert KAS_BUNDLE_AUTH_ARG not in plan.argv
        at = plan.argv.index("--example-auth=host")
        assert plan.argv[at : at + 2] == auth.engine_args()
        assert plan.host_auth is False and auth.preflights == 1
    else:
        assert KAS_BUNDLE_AUTH_ARG in plan.argv and plan.host_auth is True
        assert "--example-auth=host" not in plan.argv and auth.preflights == 0
    env = {"EXAMPLE_TOKEN": "x", "KEEP": "y"}
    harness.apply_spawn_env(env, spawned_binary=None)
    assert ("EXAMPLE_TOKEN" in env) is (not on) and env["KEEP"] == "y"


@pytest.mark.asyncio
async def test_a_refused_preflight_starts_no_install(edition, preview, monkeypatch, tmp_path):
    preview(True)
    edition.engine(lambda: _Auth(refuse="no host credential file"))
    monkeypatch.setattr("kiro_crew.acp.harness.kas._chat_sharing_preview_on", lambda: False)
    monkeypatch.setattr("kiro_crew.acp.harness.kas._bundle_route_selected", lambda: True)
    monkeypatch.setattr(
        "kiro_crew.acp.kas_bundle.installed_bundle", lambda: pytest.fail("install asked")
    )
    with pytest.raises(AcpRuntimeError, match="no host credential file"):
        await KasHarness().resolve_spawn(_ctx(tmp_path))


@pytest.mark.parametrize("sharing", [False, True], ids=["relay", "sharing-preview-bundle"])
@pytest.mark.asyncio
async def test_the_relay_refuses_under_an_engine_credential(
    edition, preview, monkeypatch, tmp_path, sharing
):
    """Off ``agent.kas_bundle`` an engine credential is refused on every other route, the
    sharing preview's bundle included, as the turn gate's ``cold_spawn_refused`` says."""
    preview(True)
    edition.engine(_Auth)
    monkeypatch.setattr("kiro_crew.acp.harness.kas._chat_sharing_preview_on", lambda: sharing)
    monkeypatch.setattr("kiro_crew.acp.harness.kas._bundle_route_selected", lambda: False)
    monkeypatch.setattr(
        "kiro_crew.acp.kas_route.sharing_preview_selects_bundle", lambda **_: sharing
    )
    with pytest.raises(AcpRuntimeError, match="bundle route only"):
        await KasHarness().resolve_spawn(_ctx(tmp_path))
    assert kas_engine_auth.cold_spawn_refused(
        kas_engine_auth.active_engine_auth(),
        SimpleNamespace(agent=SimpleNamespace(kas_bundle=False)),
    )


@pytest.mark.parametrize("on", [False, True])
def test_a_start_refusal_is_worded_by_the_provider_only_with_the_preview(edition, preview, on):
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    preview(on)
    edition.engine(_Auth)
    kas_engine_auth.note_spawn(kas_engine_auth.active_engine_auth())
    said = kas_engine_auth.start_refusal(ACP_BACKEND_KAS, "TokenRefused", -32000)
    assert said == ("the host credential was refused" if on else "")
    assert kas_engine_auth.start_refusal("kiro", "TokenRefused", -32000) == ""


def test_a_provider_that_raises_keeps_crews_sign_in(edition, preview):
    preview(True)

    def _broken():
        raise RuntimeError("edition bug")

    edition.engine(_broken)
    assert kas_engine_auth.active_engine_auth() is None


@pytest.mark.asyncio
async def test_a_refused_preflight_is_not_hidden_by_the_sharing_preview(
    edition, preview, monkeypatch, tmp_path
):
    """With the sharing preview's bundle route, a preflight refusal reaches the user as
    written and arms no bundle cooldown, so fixing the cause takes effect at once."""
    preview(True)
    edition.engine(lambda: _Auth(refuse="no host credential file"))
    monkeypatch.setattr("kiro_crew.acp.harness.kas._chat_sharing_preview_on", lambda: True)
    monkeypatch.setattr("kiro_crew.acp.harness.kas._bundle_route_selected", lambda: False)
    monkeypatch.setattr("kiro_crew.acp.kas_route.sharing_preview_selects_bundle", lambda **_: True)
    failed = []
    monkeypatch.setattr("kiro_crew.acp.kas_route.note_preview_bundle_failed", failed.append)
    with pytest.raises(AcpRuntimeError, match="no host credential file"):
        await KasHarness().resolve_spawn(_ctx(tmp_path))
    assert failed == []


@pytest.mark.parametrize("on", [False, True])
@pytest.mark.asyncio
async def test_the_model_list_follows_an_edition_engine_credential(
    edition, preview, monkeypatch, on
):
    """With an edition credential the model list needs no person sign-in and its rows are
    keyed by the credential's own identity; off, an empty vault is main's signed-out 503."""
    from unittest.mock import MagicMock

    from kiro_crew.agent_sdk import kas_model_catalog
    from kiro_crew.dashboard.handlers import models_kas_bundle

    preview(on)
    auth = _Auth()
    auth.catalog_identity = "edition:example"
    edition.engine(lambda: auth)
    monkeypatch.setattr(models_kas_bundle, "_bundle_installed", lambda: True)
    monkeypatch.setattr(models_kas_bundle, "_vault_signed_in", lambda: False)
    monkeypatch.setattr(models_kas_bundle, "_serve_recording", _served)
    request = MagicMock()
    request.app = {}
    resp = await models_kas_bundle.api_models_kas_bundle(request)
    assert resp.status == (200 if on else 503)
    if on:
        assert kas_model_catalog.vault_identity() == "edition:example"


async def _served(request, **_kwargs):
    from aiohttp import web

    return web.json_response([], status=200)


@pytest.mark.asyncio
async def test_a_cold_turn_gate_refuses_what_the_spawn_refuses(
    edition, preview, monkeypatch, signed_in
):
    """A rerun's gate must refuse every turn the spawn would refuse, before history is cut:
    a registered start URL with no engine credential, and an engine credential off the
    configured bundle route."""
    from kiro_crew.dashboard import kiro_readiness

    preview(True)
    monkeypatch.setattr(kiro_readiness, "_kas_backend_sync", lambda key, config: True)
    monkeypatch.setattr(bridge, "default_token_store", lambda: pytest.fail("vault asked"))
    refused, ready = kiro_readiness.KAS_TURN_REFUSED, kiro_readiness.KAS_TURN_READY
    assert await kiro_readiness.kas_turn_verdict("chat-1", None) == refused

    edition.engine(_Auth)
    cfg = SimpleNamespace(agent=SimpleNamespace(kas_bundle=False))
    assert await kiro_readiness.kas_turn_verdict("chat-1", None, config=cfg) == refused
    cfg = SimpleNamespace(agent=SimpleNamespace(kas_bundle=True))
    assert await kiro_readiness.kas_turn_verdict("chat-1", None, config=cfg) == ready


@pytest.mark.asyncio
async def test_a_registered_start_url_rerun_is_a_503_and_never_asks_kiro_cli(
    edition, preview, monkeypatch, signed_in
):
    """The stored sign-in is on an edition's own start URL, so no local KAS spawn may run,
    on either route and whether or not an engine is installed: the rerun gate refuses
    before history is cut and kiro-cli is not asked."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard import kiro_readiness

    preview(True)
    monkeypatch.setattr(kiro_readiness, "_kas_backend_sync", lambda key, config: True)
    monkeypatch.setattr(bridge, "default_token_store", lambda: pytest.fail("vault asked"))
    kiro_cli = AsyncMock(return_value=True)  # kiro-cli is signed in
    monkeypatch.setattr(kiro_readiness, "kiro_verified_ready", kiro_cli)
    request = MagicMock()
    request.app = {"state": SimpleNamespace(sessions=None)}
    request.path = "/api/chat/edit-resend"
    resp = await kiro_readiness.reject_if_kiro_unverified(
        request, harness_turn=True, session_key="chat-1"
    )
    assert resp is not None and resp.status == 503
    assert json.loads(resp.body)["code"] == kiro_readiness.KAS_TURN_REFUSED_CODE
    kiro_cli.assert_not_called()
