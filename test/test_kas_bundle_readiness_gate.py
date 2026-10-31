"""The turn-starting readiness gates accept Crew's own sign-in on the KAS bundle route.

On a host whose default harness is KAS on the engine bundle route
(``agent.kas_bundle``) there is no kiro-cli to probe: the engine is Crew's own
Node build and takes its credential from Crew's sign-in vault. The kiro-cli
readiness probe is never ready there, so a gate that consults only it refuses
every regenerate, edit-resend, rewind and ``/v1/chat/completions`` call with 503
``kiro_prerequisite_required`` although the turn would run.

These tests pin the rule: a caller whose only kiro-cli dependency is the turn it
starts passes ``harness_turn=True`` and is authorized by
:func:`kiro_readiness.kas_crew_signin_ready`; a caller that spawns kiro-cli
itself (``/api/models`` off the bundle route) keeps the kiro-cli gate, and the
check judges the session the turn runs on, not the default backend.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.auth.provider import NotAuthenticated
from kiro_crew.auth.refresh import RefreshBlocked, RefreshRejected
from kiro_crew.dashboard import chat_regenerate, chat_rewind, kiro_readiness, openai_compat
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers import agents
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

_LOAD = "kiro_crew.config.loader.KiroCrewConfig.load"
_ROUTE = "kiro_crew.agent_sdk.kas_bundle.kas_bundle_route"
_INSTALLED = "kiro_crew.agent_sdk.kas_bundle.installed_bundle"
_PROVIDER = "kiro_crew.auth.provider.KasAuthProvider"
_STORE = "kiro_crew.auth.bridge.default_token_store"
_RESOLVE_TARGET = "kiro_crew.acp.client._resolve_kiro_bin_for_spawn"
_PIN = "kiro_crew.crewmate_harness.crewmate_backend_pin"
_MEMBER_KEY = "dashboard:member-ann"


@pytest.fixture(autouse=True)
def _reset_refusal_warning():
    kiro_readiness._clear_refusal_warning()
    yield
    kiro_readiness._clear_refusal_warning()


def _config(member_backend: str = "kas", *, default: str = "kas", bundle: bool = True):
    return SimpleNamespace(
        agent=SimpleNamespace(
            acp_backend=default, member_acp_backend=member_backend, kas_bundle=bundle
        ),
        agents={},
    )


class _SignedOutService(KiroPrerequisiteService):
    """A kiro-cli probe that is never ready, as on a host with no kiro-cli."""

    async def session_ready(self) -> bool:
        return False

    async def verified_ready(self, *, max_age_secs: float) -> bool:
        del max_age_secs
        return False


def _signed_out_service() -> KiroPrerequisiteService:
    return object.__new__(_SignedOutService)


def _request(service: object, slots: dict | None = None) -> MagicMock:
    request = MagicMock()
    request.app = {
        "kiro_prerequisite_service": service,
        "state": SimpleNamespace(
            kiro_prerequisite_service=service, _background_tasks=set(), _slots=slots or {}
        ),
    }
    request.match_info = {"slot": "member-ann"}
    request.path = "/api/chat/slots/member-ann/regenerate"
    return request


def _provider(current: AsyncMock) -> MagicMock:
    provider = MagicMock()
    provider.return_value.current = current
    return provider


class TestBundleTurn:
    """``_kas_bundle_turn_sync``: the session's own turn runs on an installed engine bundle."""

    def test_default_session_on_the_installed_bundle_route(self):
        with (
            patch(_LOAD, return_value=_config()),
            patch(_INSTALLED, return_value=object()),
        ):
            assert kiro_readiness._kas_bundle_turn_sync(None) is True

    def test_member_session_on_kas_is_the_bundle_turn(self):
        with (
            patch(_LOAD, return_value=_config("kas")),
            patch(_INSTALLED, return_value=object()),
        ):
            assert kiro_readiness._kas_bundle_turn_sync(_MEMBER_KEY) is True

    def test_member_session_on_kas_under_another_default_is_the_bundle_turn(self):
        # The harness takes the bundle route from agent.kas_bundle once it is KAS,
        # so a member thread on KAS runs no kiro-cli whatever the default backend.
        with (
            patch(_LOAD, return_value=_config("kas", default="claude")),
            patch(_INSTALLED, return_value=object()),
        ):
            assert kiro_readiness._kas_bundle_turn_sync(_MEMBER_KEY) is True

    def test_default_session_under_another_default_is_not(self):
        with (
            patch(_LOAD, return_value=_config("kas", default="claude")),
            patch(_INSTALLED, return_value=object()) as inst,
        ):
            assert kiro_readiness._kas_bundle_turn_sync(None) is False
        inst.assert_not_called()

    def test_member_session_on_another_backend_is_not(self):
        # A member thread runs on agent.member_acp_backend; the default backend
        # being KAS on the bundle says nothing about the harness its rerun starts.
        with (
            patch(_LOAD, return_value=_config("claude")),
            patch(_INSTALLED, return_value=object()) as inst,
        ):
            assert kiro_readiness._kas_bundle_turn_sync(_MEMBER_KEY) is False
        inst.assert_not_called()

    def test_bundle_route_with_nothing_installed_is_not_ready(self):
        # The bundle spawn refuses with nothing installed, so a rerun would
        # rewrite history for a turn that cannot start.
        with (
            patch(_LOAD, return_value=_config()),
            patch(_INSTALLED, return_value=None),
        ):
            assert kiro_readiness._kas_bundle_turn_sync(None) is False

    def test_off_the_bundle_route_the_engine_is_not_read(self):
        # The relay route spawns kiro-cli: the kiro-cli gate decides.
        with patch(_LOAD, return_value=_config(bundle=False)), patch(_INSTALLED) as inst:
            assert kiro_readiness._kas_bundle_turn_sync(None) is False
        inst.assert_not_called()

    def test_an_unreadable_answer_keeps_the_kiro_cli_gate(self):
        with patch(_LOAD, side_effect=OSError("nope")):
            assert kiro_readiness._kas_bundle_turn_sync(None) is False

    def test_a_crewmate_pinned_to_kiro_cli_is_not_the_bundle_turn(self):
        """The pin outranks a KAS member backend, as it does in the provider factory."""
        from kiro_crew.crewmate_harness import StartableCrewPin

        pin = StartableCrewPin(crew="ann", stored="kiro", backend="")
        with (
            patch(_LOAD, return_value=_config()),
            patch(_INSTALLED, return_value=object()),
            patch(_PIN, return_value=pin),
        ):
            assert kiro_readiness._kas_bundle_turn_sync(_MEMBER_KEY) is False

    def test_a_crewmate_pinned_to_kas_under_another_member_backend_is_the_bundle_turn(self):
        from kiro_crew.crewmate_harness import StartableCrewPin

        pin = StartableCrewPin(crew="ann", stored="kas", backend="kas")
        with (
            patch(_LOAD, return_value=_config(member_backend="", default="")),
            patch(_INSTALLED, return_value=object()),
            patch(_PIN, return_value=pin),
        ):
            assert kiro_readiness._kas_bundle_turn_sync(_MEMBER_KEY) is True

    def test_a_pin_that_cannot_start_keeps_the_kiro_cli_gate(self):
        from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable

        with (
            patch(_LOAD, return_value=_config()),
            patch(_INSTALLED, return_value=object()),
            patch(_PIN, side_effect=CrewmateHarnessUnavailable("ann", "goose")),
        ):
            assert kiro_readiness._kas_bundle_turn_sync(_MEMBER_KEY) is False


class TestCrewSigninReady:
    """``kas_crew_signin_ready``: the bundle route AND a credential from Crew's sign-in."""

    @pytest.mark.asyncio
    async def test_a_resolvable_credential_is_ready(self):
        current = AsyncMock(return_value=object())
        with (
            patch.object(kiro_readiness, "_kas_bundle_turn_sync", return_value=True),
            patch(_STORE),
            patch(_PROVIDER, _provider(current)) as provider,
        ):
            assert await kiro_readiness.kas_crew_signin_ready(None) is True
        current.assert_awaited_once()
        # Vault only: an ambient KIRO_API_KEY must not stand in for a sign-in.
        assert provider.call_args.kwargs == {"allow_env_api_key": False}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [NotAuthenticated("none"), RefreshRejected("refused"), RefreshBlocked("blocked")],
    )
    async def test_no_credential_is_not_ready(self, exc):
        with (
            patch.object(kiro_readiness, "_kas_bundle_turn_sync", return_value=True),
            patch(_STORE),
            patch(_PROVIDER, _provider(AsyncMock(side_effect=exc))),
        ):
            assert await kiro_readiness.kas_crew_signin_ready(None) is False

    @pytest.mark.asyncio
    async def test_off_the_bundle_route_the_vault_is_not_read(self):
        current = AsyncMock(return_value=object())
        with (
            patch.object(kiro_readiness, "_kas_bundle_turn_sync", return_value=False),
            patch(_PROVIDER, _provider(current)),
        ):
            assert await kiro_readiness.kas_crew_signin_ready(None) is False
        current.assert_not_called()


def _live_sessions(acp_backend: str, *, engine_bundle: bool) -> SimpleNamespace:
    runtime = SimpleNamespace(acp_backend=acp_backend, _kas_engine_bundle=engine_bundle)
    provider = SimpleNamespace(_runtime=runtime)
    return SimpleNamespace(get_provider=lambda key: provider if key == _MEMBER_KEY else None)


class TestLiveTurnRoute:
    """``live_turn_route``: a live session is judged by the process it runs, not the config."""

    def test_no_key_or_no_session_map_is_a_cold_start(self):
        assert kiro_readiness.live_turn_route(SimpleNamespace(), None) is None
        assert kiro_readiness.live_turn_route(SimpleNamespace(), _MEMBER_KEY) is None

    def test_no_live_provider_is_a_cold_start(self):
        state = SimpleNamespace(sessions=_live_sessions("kas", engine_bundle=True))
        assert kiro_readiness.live_turn_route(state, "dashboard:other") is None

    def test_live_bundle_process(self):
        state = SimpleNamespace(sessions=_live_sessions("kas", engine_bundle=True))
        assert kiro_readiness.live_turn_route(state, _MEMBER_KEY) is True

    @pytest.mark.parametrize(
        ("backend", "bundle"),
        [("", False), ("kas", False)],
        ids=["live-kiro-cli-session", "live-kas-relay-session"],
    )
    def test_live_session_off_the_bundle(self, backend, bundle):
        state = SimpleNamespace(sessions=_live_sessions(backend, engine_bundle=bundle))
        assert kiro_readiness.live_turn_route(state, _MEMBER_KEY) is False

    @pytest.mark.parametrize("bundle", [True, False])
    def test_the_registered_acp_provider_wrapper_is_unwrapped(self, bundle):
        """The session map holds an ``AcpProvider``; its runtime is on ``client``."""
        from kiro_crew.acp.session_provider import AcpSessionProvider
        from kiro_crew.providers.acp import AcpProvider

        inner = object.__new__(AcpSessionProvider)
        inner._runtime = SimpleNamespace(acp_backend="kas", _kas_engine_bundle=bundle)
        wrapper = object.__new__(AcpProvider)
        wrapper._client = inner
        state = SimpleNamespace(
            sessions=SimpleNamespace(get_provider=lambda key: wrapper),
        )
        assert kiro_readiness.live_turn_route(state, _MEMBER_KEY) is bundle

    def test_a_provider_with_no_readable_runtime_keeps_the_gate(self):
        state = SimpleNamespace(
            sessions=SimpleNamespace(get_provider=lambda key: SimpleNamespace())
        )
        assert kiro_readiness.live_turn_route(state, _MEMBER_KEY) is False

    def test_an_unreadable_session_map_keeps_the_gate(self):
        def boom(key):
            raise RuntimeError("closing")

        state = SimpleNamespace(sessions=SimpleNamespace(get_provider=boom))
        assert kiro_readiness.live_turn_route(state, _MEMBER_KEY) is False


class TestLiveRouteSignin:
    """``kas_crew_signin_ready`` with a live route ignores the configured backend."""

    @pytest.mark.asyncio
    async def test_live_route_off_the_bundle_refuses_without_reading_config(self):
        current = AsyncMock(return_value=object())
        with (
            patch.object(kiro_readiness, "_kas_bundle_turn_sync", return_value=True) as cfg,
            patch(_PROVIDER, _provider(current)),
        ):
            assert await kiro_readiness.kas_crew_signin_ready(_MEMBER_KEY, False) is False
        cfg.assert_not_called()
        current.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_bundle_route_still_needs_crew_signin(self):
        current = AsyncMock(return_value=object())
        with (
            patch.object(kiro_readiness, "_kas_bundle_turn_sync", return_value=False) as cfg,
            patch(_STORE),
            patch(_PROVIDER, _provider(current)),
        ):
            assert await kiro_readiness.kas_crew_signin_ready(_MEMBER_KEY, True) is True
        cfg.assert_not_called()
        current.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_live_kiro_cli_session_under_a_kas_default_is_refused(self):
        """A retained kiro-cli session under a KAS-bundle default keeps the 503.

        The default switched to KAS while an idle kiro-cli session stayed alive:
        the rerun reuses that session, so Crew's sign-in cannot authorize it.
        """
        request = _request(_signed_out_service())
        request.app["state"].sessions = _live_sessions("", engine_bundle=False)
        current = AsyncMock(return_value=object())
        with (
            patch(_LOAD, return_value=_config()),
            patch(_INSTALLED, return_value=object()),
            patch(_STORE),
            patch(_PROVIDER, _provider(current)),
        ):
            resp = await kiro_readiness.reject_if_kiro_unverified(
                request, harness_turn=True, session_key=_MEMBER_KEY
            )
        assert resp is not None and resp.status == 503
        assert json.loads(resp.body)["code"] == "kiro_prerequisite_required"
        current.assert_not_called()


class TestDiscardingRerun:
    """A rerun that discards the live session is judged on the configured route."""

    @pytest.mark.asyncio
    async def test_live_bundle_session_after_the_default_left_the_bundle_is_refused(self):
        """Edit-resend/rewind tear the live bundle process down; the turn cold-starts on kiro-cli."""
        request = _request(_signed_out_service())
        request.app["state"].sessions = _live_sessions("kas", engine_bundle=True)
        current = AsyncMock(return_value=object())
        with (
            patch(_LOAD, return_value=_config(default="", member_backend="")),
            patch(_STORE),
            patch(_PROVIDER, _provider(current)),
        ):
            resp = await kiro_readiness.reject_if_kiro_unverified(
                request, harness_turn=True, session_key=_MEMBER_KEY, turn_reuses_session=False
            )
        assert resp is not None and resp.status == 503
        current.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_kiro_cli_session_under_a_bundle_default_passes(self):
        """The discarded kiro-cli process does not run the turn; the bundle cold start does."""
        request = _request(_signed_out_service())
        request.app["state"].sessions = _live_sessions("", engine_bundle=False)
        current = AsyncMock(return_value=object())
        with (
            patch(_LOAD, return_value=_config()),
            patch(_INSTALLED, return_value=object()),
            patch(_STORE),
            patch(_PROVIDER, _provider(current)),
        ):
            resp = await kiro_readiness.reject_if_kiro_unverified(
                request, harness_turn=True, session_key=_MEMBER_KEY, turn_reuses_session=False
            )
        assert resp is None
        current.assert_awaited_once()


class TestGate:
    """``reject_if_kiro_unverified`` consults the Crew sign-in only for a harness turn."""

    @pytest.mark.asyncio
    async def test_harness_turn_passes_on_crew_signin_without_probing_kiro_cli(self):
        service = _signed_out_service()
        ready = AsyncMock(return_value=True)
        with (
            patch.object(kiro_readiness, "kas_crew_signin_ready", ready),
            patch.object(_SignedOutService, "verified_ready", AsyncMock(return_value=False)) as vr,
        ):
            assert (
                await kiro_readiness.reject_if_kiro_unverified(
                    _request(service), harness_turn=True, session_key=_MEMBER_KEY
                )
                is None
            )
        vr.assert_not_called()
        # The session the turn runs on is what the sign-in check judges; with no
        # live session for it, the cold-start route decides (live route None).
        ready.assert_awaited_once_with(_MEMBER_KEY, None)

    @pytest.mark.asyncio
    async def test_harness_turn_without_crew_signin_falls_back_to_kiro_cli(self):
        with patch.object(kiro_readiness, "kas_crew_signin_ready", AsyncMock(return_value=False)):
            resp = await kiro_readiness.reject_if_kiro_unverified(
                _request(_signed_out_service()), harness_turn=True
            )
        assert resp is not None and resp.status == 503
        assert json.loads(resp.body)["code"] == "kiro_prerequisite_required"

    @pytest.mark.asyncio
    async def test_default_caller_never_consults_the_crew_signin(self):
        ready = AsyncMock(return_value=True)
        with patch.object(kiro_readiness, "kas_crew_signin_ready", ready):
            resp = await kiro_readiness.reject_if_kiro_unverified(_request(_signed_out_service()))
        assert resp is not None and resp.status == 503
        ready.assert_not_called()


_SENTINEL_STATUS = 418


def _member_slot() -> SimpleNamespace:
    return SimpleNamespace(key="member-ann", linked_session_key="")


@pytest.mark.parametrize(
    ("module", "handler"),
    [
        (chat_regenerate, chat_regenerate.api_chat_slot_regenerate),
        (chat_regenerate, chat_regenerate.api_chat_slot_edit_resend),
        (chat_rewind, chat_rewind.api_chat_slot_rewind),
    ],
    ids=["regenerate", "edit-resend", "rewind"],
)
@pytest.mark.asyncio
async def test_destructive_reruns_gate_on_the_slots_own_session(module, handler):
    slot = _member_slot()
    gate = AsyncMock(return_value=web.json_response({}, status=_SENTINEL_STATUS))
    with patch.object(module, "reject_if_kiro_unverified", gate):
        resp = await handler(_request(_signed_out_service(), {"member-ann": slot}))
    assert resp.status == _SENTINEL_STATUS
    assert gate.await_args.kwargs.get("harness_turn") is True
    assert gate.await_args.kwargs.get("session_key") == effective_session_key(slot)
    # Edit-resend and rewind discard the live conversation before their turn, so
    # they are judged on the cold-start route; regenerate reuses the live session.
    reuses = gate.await_args.kwargs.get("turn_reuses_session", True)
    assert reuses is (handler is chat_regenerate.api_chat_slot_regenerate)


@pytest.mark.asyncio
async def test_destructive_rerun_of_an_unknown_slot_gates_on_the_default():
    gate = AsyncMock(return_value=web.json_response({}, status=_SENTINEL_STATUS))
    with patch.object(chat_rewind, "reject_if_kiro_unverified", gate):
        await chat_rewind.api_chat_slot_rewind(_request(_signed_out_service()))
    assert gate.await_args.kwargs.get("session_key") is None


def test_an_unreadable_slot_keeps_the_kiro_cli_gate():
    # A slot whose session cannot be named is not judged as a default-backend
    # session: the Crew sign-in is not consulted for it at all.
    state = SimpleNamespace(_slots={"broken": SimpleNamespace(messages=[])})
    assert kiro_readiness.turn_gate_target(state, "broken") == (False, None)
    assert kiro_readiness.turn_gate_target(SimpleNamespace(), "any") == (False, None)


@pytest.mark.asyncio
async def test_chat_completions_gates_on_the_target_slots_session():
    slot = _member_slot()
    request = _request(_signed_out_service(), {"member-ann": slot})
    request.json = AsyncMock(return_value={"id": "member-ann", "model": "kiro", "messages": []})
    gate = AsyncMock(return_value=web.json_response({}, status=503))
    with patch.object(openai_compat, "reject_if_kiro_unverified", gate):
        resp = await openai_compat.api_completions(request)
    assert resp.status == 503
    assert json.loads(resp.body)["error"]["code"] == "kiro_prerequisite_required"
    assert gate.await_args.kwargs.get("harness_turn") is True
    assert gate.await_args.kwargs.get("session_key") == effective_session_key(slot)


@pytest.mark.asyncio
async def test_chat_completions_for_a_new_slot_gates_on_the_default():
    request = _request(_signed_out_service())
    request.json = AsyncMock(return_value={"model": "kiro", "messages": []})
    gate = AsyncMock(return_value=web.json_response({}, status=503))
    with patch.object(openai_compat, "reject_if_kiro_unverified", gate):
        await openai_compat.api_completions(request)
    assert gate.await_args.kwargs.get("session_key") is None


@pytest.mark.asyncio
async def test_api_models_off_the_bundle_route_never_consults_the_crew_signin():
    # Off the bundle route the catalog is kiro-cli's own --list-models output,
    # so a Crew sign-in must not open the spawn path.
    ready = AsyncMock(return_value=True)
    with (
        patch(_ROUTE, return_value=False),
        patch.object(kiro_readiness, "kas_crew_signin_ready", ready),
        patch(_RESOLVE_TARGET, AsyncMock(return_value="/usr/bin/kiro-cli")) as resolve,
    ):
        resp = await agents.api_models(_request(_signed_out_service()))
    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "kiro_prerequisite_required"
    ready.assert_not_called()
    resolve.assert_not_called()
