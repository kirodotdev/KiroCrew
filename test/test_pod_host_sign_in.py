"""A pod's bundled KAS engine borrows the host's Crew sign-in (``pod_host_sign_in``).

The host half runs as a real aiohttp app on a real unix socket in a temp
directory, and the pod half (:func:`kiro_crew.pod_host_sign_in.fetch_answer`)
dials it with its own unix-only client, so the kernel's peer credentials are
the real ones: this test process is both the pod's gateway and the host. What
names "the pod's gateway" -- the pod unit's main pid -- is the one seam patched,
since no service manager runs here. The vault is a real :class:`TokenStore`.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from aiohttp import ClientSession, web

from kiro_crew import pod_host_sign_in as phs
from kiro_crew.acp import kas_host_auth, kas_route
from kiro_crew.acp.kas_host_auth import HostAuthCallbackError, answer_get_access_token
from kiro_crew.apps.core_route_grants import RouteGrant
from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard.handlers import pod_host_sign_in as handler
from kiro_crew.dashboard.token_auth_paths import _exact_method_bypass
from kiro_crew.testing.ids import UNALLOCATABLE_PID

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="the host answers on a unix socket only"
)

POD = "p6-test"

#: The only keys a lent answer may carry.
FIVE_KEYS = {"accessToken", "expiresAt", "profileArn", "provider", "authMethod"}

#: Secrets a stored sign-in holds that must never be lent.
_SECRETS = dict(client_id="host-client-id", client_secret="host-client-secret")


def _token(**overrides) -> KasToken:
    kwargs = dict(
        access_token="host-access-token",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        provider="Google",
        identity="social",
        refresh_token="host-refresh-token",
        profile_arn="arn:aws:codewhisperer:us-east-1:1:profile/x",
    )
    kwargs.update(overrides)
    return KasToken(**kwargs)


@pytest.fixture
def data_home(tmp_path):
    home = tmp_path / "data-home"
    home.mkdir()
    with patch("kiro_crew.auth.bridge.data_home", return_value=home):
        yield home


@pytest.fixture
def host_on():
    """The host side's switches all on, and this process as pod POD's gateway."""
    with (
        patch.object(phs, "live_preview_on", return_value=True),
        patch(
            "kiro_crew.apps.core_route_grants.core_route_grant",
            return_value=RouteGrant(holder="dev-fleet"),
        ),
        patch("kiro_crew.pod.runtime.main_pid", return_value=os.getpid()),
        # This one process plays the pod too, so its KIROCREW_POD says nothing here.
        patch.object(handler, "_this_gateway_is_a_pod", return_value=False),
    ):
        yield


@pytest_asyncio.fixture
async def host_socket(short_sock_dir):
    """The two host routes on a real unix socket, as the gateway registers them."""
    path = short_sock_dir / "dashboard-5476.sock"
    app = web.Application()
    app.router.add_post(phs.TOKEN_ROUTE, handler.api_pod_host_sign_in_token)
    app.router.add_post(phs.REFUSED_ROUTE, handler.api_pod_host_sign_in_refused)
    runner = web.AppRunner(app)
    try:
        await runner.setup()
        await web.UnixSite(runner, str(path)).start()
        yield path
    finally:
        await runner.cleanup()


@pytest.fixture
def in_pod(monkeypatch, tmp_path, host_socket):
    """This process as a pod gateway named POD, given the host's socket."""
    monkeypatch.setenv("KIROCREW_POD", "1")
    monkeypatch.setenv(phs.SOCKET_ENV, str(host_socket))
    monkeypatch.setattr(phs, "_pod_name", lambda: POD)
    return host_socket


async def _fetch() -> dict:
    return await asyncio.to_thread(phs.fetch_answer)


class TestTheHostLendsOnlyAnAccessToken:
    @pytest.mark.asyncio
    async def test_the_pod_gets_the_token_its_expiry_and_its_account_metadata_only(
        self, data_home, host_on, in_pod
    ):
        TokenStore(data_home).save(_token(**_SECRETS))
        answer = await _fetch()
        assert set(answer) <= FIVE_KEYS
        assert {"accessToken", "expiresAt", "profileArn", "provider"} <= set(answer)
        assert answer["accessToken"] == "host-access-token"
        assert answer["profileArn"] == "arn:aws:codewhisperer:us-east-1:1:profile/x"
        for secret in ("host-refresh-token", "host-client-secret"):
            assert secret not in repr(answer)

    @pytest.mark.asyncio
    async def test_the_host_body_carries_only_the_five_keys(self, data_home, host_on, in_pod):
        """Even an answer builder that offered more is cut to the five keys on the host."""
        offered = {
            "accessToken": "a",
            "expiresAt": "2030-01-01T00:00:00+00:00",
            "profileArn": "arn",
            "provider": "Enterprise",
            "authMethod": "IdC",
            "refreshToken": "host-refresh-token",
            "clientSecret": "host-client-secret",
            "clientId": "client",
            "extra": "x",
        }
        with patch("kiro_crew.agent_sdk.kas_vault_answer.answer_from_vault", return_value=offered):
            status, body = await asyncio.to_thread(phs._post, phs.TOKEN_ROUTE, {"pod": POD})
        assert status == 200
        assert set(body) == FIVE_KEYS

    def test_the_pod_side_cut_keeps_the_same_five_keys(self):
        assert set(phs.ANSWER_KEYS) == FIVE_KEYS
        cut = phs.filter_answer({k: "v" for k in (*FIVE_KEYS, "refreshToken", "clientSecret")})
        assert set(cut) == FIVE_KEYS

    @pytest.mark.asyncio
    async def test_a_host_with_no_crew_sign_in_refuses_and_names_kas_login(
        self, data_home, host_on, in_pod
    ):
        status, body = await asyncio.to_thread(phs._post, phs.TOKEN_ROUTE, {"pod": POD})
        assert status == 409 and body["code"] == "host_not_signed_in"
        assert "kirocrew kas login" in body["error"]
        with pytest.raises(phs.HostSignInError, match="kirocrew kas login"):
            await _fetch()

    @pytest.mark.asyncio
    async def test_a_company_sign_in_is_never_lent(self, data_home, host_on, in_pod):
        """No local agent may run on an edition's sign-in, so no pod is lent one."""
        TokenStore(data_home).save(_token())
        with (
            patch("kiro_crew.auth.login.endpoints.stored_sign_in_registered", return_value=True),
            patch("kiro_crew.agent_sdk.kas_vault_answer.answer_from_vault") as vault,
        ):
            status, body = await asyncio.to_thread(phs._post, phs.TOKEN_ROUTE, {"pod": POD})
        assert status == 403 and body["code"] == "host_sign_in_not_lendable"
        assert "accessToken" not in body
        vault.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_switch_to_a_company_sign_in_mid_answer_is_not_lent(
        self, data_home, host_on, in_pod
    ):
        """The check runs again once the vault has answered, so a switch in between is refused."""
        TokenStore(data_home).save(_token())
        with patch(
            "kiro_crew.auth.login.endpoints.stored_sign_in_registered", side_effect=[False, True]
        ):
            status, body = await asyncio.to_thread(phs._post, phs.TOKEN_ROUTE, {"pod": POD})
        assert status == 403 and body["code"] == "host_sign_in_not_lendable"
        assert "accessToken" not in body

    @pytest.mark.asyncio
    async def test_a_host_kiro_cli_sign_in_or_api_key_is_never_lent(
        self, data_home, host_on, in_pod, monkeypatch
    ):
        monkeypatch.setenv("KIRO_API_KEY", "ambient-key")
        status, body = await asyncio.to_thread(phs._post, phs.TOKEN_ROUTE, {"pod": POD})
        assert status == 409 and "ambient-key" not in repr(body)


class TestOnlyThatPodsGatewayIsAnswered:
    @pytest.mark.asyncio
    async def test_another_pods_name_is_refused(self, data_home, host_on, in_pod):
        """This process is POD's gateway; it is no other pod's."""
        TokenStore(data_home).save(_token())
        with patch(
            "kiro_crew.pod.runtime.main_pid",
            side_effect=lambda cfg, name: os.getpid() if name == POD else os.getpid() + 1,
        ):
            status, body = await asyncio.to_thread(phs._post, phs.TOKEN_ROUTE, {"pod": "other-pod"})
        assert status == 403 and body["code"] == "not_pod_gateway"

    @pytest.mark.asyncio
    async def test_a_pod_that_is_down_gets_nothing(self, data_home, host_on, in_pod):
        TokenStore(data_home).save(_token())
        with patch("kiro_crew.pod.runtime.main_pid", return_value=None):
            status, body = await asyncio.to_thread(phs._post, phs.TOKEN_ROUTE, {"pod": POD})
        assert status == 403 and body["code"] == "pod_not_running"

    @pytest.mark.asyncio
    async def test_an_unreadable_pod_unit_is_a_coded_refusal(self, data_home, host_on, in_pod):
        """launchd's reader raises a RuntimeError, not a PodError: still a 403, not a 500."""
        TokenStore(data_home).save(_token())
        with patch("kiro_crew.pod.runtime.main_pid", side_effect=RuntimeError("launchctl")):
            status, body = await asyncio.to_thread(phs._post, phs.TOKEN_ROUTE, {"pod": POD})
        assert status == 403 and body["code"] == "pod_unit_unreadable"

    @pytest.mark.asyncio
    async def test_a_process_in_another_namespace_is_refused(self, data_home, host_on, in_pod):
        """A sandboxed shell inside the pod runs in its own namespaces."""
        TokenStore(data_home).save(_token())
        with (
            patch("kiro_crew.platform_compat.process_namespaces_match", return_value=False),
            patch("kiro_crew.platform_compat.process_is_sandboxed", return_value=True),
        ):
            status, body = await asyncio.to_thread(phs._post, phs.TOKEN_ROUTE, {"pod": POD})
        assert status == 403 and body["code"] == "sandboxed_peer"

    @pytest.mark.asyncio
    async def test_without_the_dev_fleet_grant_nothing_is_lent(self, data_home, host_on, in_pod):
        TokenStore(data_home).save(_token())
        with patch("kiro_crew.apps.core_route_grants.core_route_grant", return_value=RouteGrant()):
            status, body = await asyncio.to_thread(phs._post, phs.TOKEN_ROUTE, {"pod": POD})
        assert status == 403 and body["code"] == "app_not_enabled"

    @pytest.mark.asyncio
    async def test_a_tcp_request_is_refused(self, data_home, host_on):
        TokenStore(data_home).save(_token())
        app = web.Application()
        app.router.add_post(phs.TOKEN_ROUTE, handler.api_pod_host_sign_in_token)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        try:
            async with ClientSession() as session:
                url = f"http://127.0.0.1:{port}{phs.TOKEN_ROUTE}"
                async with session.post(url, json={"pod": POD}) as resp:
                    assert resp.status == 403
                    assert (await resp.json())["code"] == "unix_socket_only"
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_the_rate_limit_bounds_a_looping_pod(self, data_home, host_on, in_pod):
        TokenStore(data_home).save(_token())
        with patch.dict(create_rate_limit._BUDGETS, {create_rate_limit.POD_HOST_SIGN_IN_TOKEN: 2}):
            codes = [
                (await asyncio.to_thread(phs._post, phs.TOKEN_ROUTE, {"pod": POD}))[0]
                for _ in range(3)
            ]
        assert codes == [200, 200, 429]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", [phs.TOKEN_ROUTE, phs.REFUSED_ROUTE])
    async def test_a_refused_peer_spends_only_its_own_budget(
        self, data_home, host_on, in_pod, route
    ):
        TokenStore(data_home).save(_token())
        budgets = {verb: 2 for verb in handler._RATE_VERBS.values()}
        with patch.dict(create_rate_limit._BUDGETS, budgets):
            with (
                patch.object(handler, "_peer_pid", return_value=UNALLOCATABLE_PID),
                patch.object(handler, "_not_pod_gateway", return_value="not_pod_gateway") as check,
            ):
                replies = [
                    await asyncio.to_thread(phs._post, route, {"pod": POD}) for _ in range(3)
                ]
            assert [(status, body["code"]) for status, body in replies] == [
                (403, "not_pod_gateway"),
                (403, "not_pod_gateway"),
                (429, "rate_limited"),
            ]
            assert check.call_count == 2
            codes = [(await asyncio.to_thread(phs._post, route, {"pod": POD}))[0] for _ in range(3)]
        assert codes == [200, 200, 429]

    def test_each_route_has_its_own_budget_in_the_shared_limiter(self):
        verbs = handler._RATE_VERBS
        assert set(verbs) == {"token", "refused"}
        assert len(set(verbs.values())) == 2
        assert all(create_rate_limit._BUDGETS.get(verb) for verb in verbs.values())

    @pytest.mark.asyncio
    async def test_every_lend_is_audited(self, data_home, host_on, in_pod):
        TokenStore(data_home).save(_token())
        with patch.object(handler, "_audit") as audit:
            await _fetch()
        audit.assert_called_once()
        assert audit.call_args.args[:3] == ("token", POD, "granted")
        assert "host-access-token" not in repr(audit.call_args)


class TestPreviewOff:
    @pytest.mark.asyncio
    async def test_the_host_answers_not_found_with_the_preview_off(
        self, data_home, host_on, in_pod
    ):
        TokenStore(data_home).save(_token())
        with patch.object(phs, "live_preview_on", return_value=False):
            status, body = await asyncio.to_thread(phs._post, phs.TOKEN_ROUTE, {"pod": POD})
        assert status == 404 and "accessToken" not in (body or {})

    @pytest.mark.parametrize("on", [True, False])
    def test_token_auth_lets_the_routes_through_only_with_the_preview_on(self, on):
        with patch.object(phs, "live_preview_on", return_value=on):
            assert _exact_method_bypass(phs.TOKEN_ROUTE, "POST") is on
            assert _exact_method_bypass(phs.REFUSED_ROUTE, "POST") is on
            assert _exact_method_bypass(phs.TOKEN_ROUTE, "GET") is False

    def test_an_lts_build_reads_the_preview_off(self):
        from types import SimpleNamespace

        cfg = SimpleNamespace(dashboard=SimpleNamespace(feature_previews={phs.PREVIEW_NAME: True}))
        assert phs.preview_on(cfg) is True
        with patch("kiro_crew.edition_locks.local_enable_locked", return_value=True):
            assert phs.preview_on(cfg) is False

    @pytest.mark.asyncio
    async def test_a_pod_given_no_host_socket_answers_as_before(self, data_home, monkeypatch):
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.delenv(phs.SOCKET_ENV, raising=False)
        with patch.object(phs, "fetch_answer") as fetch:
            with pytest.raises(HostAuthCallbackError, match="not signed in to Kiro Crew"):
                await answer_get_access_token(bundle=True)
        fetch.assert_not_called()
        assert kas_route._crew_signed_in() is False


class TestBundleOnly:
    @pytest.fixture
    def pod_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv(phs.SOCKET_ENV, str(tmp_path / "dashboard-5476.sock"))

    @pytest.mark.asyncio
    async def test_a_bundle_engine_in_a_pod_with_an_empty_vault_asks_the_host(
        self, data_home, pod_env
    ):
        lent = {"accessToken": "a", "expiresAt": "2030-01-01T00:00:00+00:00"}
        with patch.object(phs, "fetch_answer", return_value=lent) as fetch:
            assert await answer_get_access_token(bundle=True) == lent
        fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_relay_engine_never_asks_the_host(self, data_home, pod_env):
        with patch.object(phs, "fetch_answer") as fetch:
            with pytest.raises(HostAuthCallbackError, match="not signed in to Kiro Crew"):
                await answer_get_access_token(bundle=False)
        fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_pods_own_sign_in_wins(self, data_home, pod_env):
        TokenStore(data_home).save(_token(access_token="pod-own"))
        with patch.object(phs, "fetch_answer") as fetch:
            assert (await answer_get_access_token(bundle=True))["accessToken"] == "pod-own"
        fetch.assert_not_called()

    def test_the_bundle_route_sees_a_sign_in_and_the_relay_does_not(self, data_home, pod_env):
        from kiro_crew.auth.bridge import vault_holds_identity

        assert kas_route._crew_holds_usable_sign_in() is True
        # The relay's owner probe is unchanged: it still spawns with kiro-cli's sign-in.
        assert vault_holds_identity() is False
        with pytest.raises(Exception):
            kas_route._crew_vault_holds_usable_sign_in()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("lends", [True, False])
    @pytest.mark.parametrize("live_route", [None, True])
    async def test_readiness_checks_the_host_off_the_loop(
        self, data_home, pod_env, lends, live_route
    ):
        from kiro_crew.dashboard import kiro_readiness

        def host_answer(route, body):
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            assert route == phs.TOKEN_ROUTE
            if lends:
                return 200, {"accessToken": "lent", "expiresAt": "2030-01-01T00:00:00+00:00"}
            return 409, {"code": "host_not_signed_in"}

        with (
            patch.object(kiro_readiness, "_kas_bundle_turn_sync", return_value=True),
            patch.object(phs, "_post", side_effect=host_answer) as post,
        ):
            assert await kiro_readiness.kas_crew_signin_ready(None, live_route) is lends
        post.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("signed_in", [True, False])
    async def test_readiness_outside_a_pod_still_uses_only_the_vault(
        self, data_home, pod_env, monkeypatch, signed_in
    ):
        from kiro_crew.dashboard import kiro_readiness

        monkeypatch.delenv("KIROCREW_POD")
        if signed_in:
            TokenStore(data_home).save(_token())
        with patch.object(phs, "fetch_answer") as fetch:
            assert await kiro_readiness.kas_crew_signin_ready(None, True) is signed_in
        fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_readiness_keeps_the_pods_own_identity(self, data_home, pod_env):
        from kiro_crew.dashboard import kiro_readiness

        TokenStore(data_home).save(_token())
        with patch.object(phs, "fetch_answer") as fetch:
            assert await kiro_readiness.kas_crew_signin_ready(None, True) is True
        fetch.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("live_route", [None, False])
    async def test_readiness_keeps_the_plain_relay_gate(self, data_home, pod_env, live_route):
        from kiro_crew.dashboard import kiro_readiness

        with (
            patch.object(kiro_readiness, "_kas_bundle_turn_sync", return_value=False),
            patch.object(phs, "fetch_answer") as fetch,
        ):
            assert await kiro_readiness.kas_crew_signin_ready(None, live_route) is False
        fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_readiness_does_not_borrow_after_a_vault_failure(self, data_home, pod_env):
        from kiro_crew.auth.refresh import RefreshRejected
        from kiro_crew.dashboard import kiro_readiness

        with (
            patch("kiro_crew.auth.provider.KasAuthProvider.current", side_effect=RefreshRejected),
            patch.object(phs, "fetch_answer") as fetch,
        ):
            assert await kiro_readiness.kas_crew_signin_ready(None, True) is False
        fetch.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("active", [True, False])
    async def test_model_availability_uses_the_borrowed_source(
        self, data_home, pod_env, monkeypatch, active
    ):
        import json
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers import models_kas_bundle as models

        if not active:
            monkeypatch.delenv(phs.SOCKET_ENV)
        with (
            patch.object(models, "_bundle_installed", return_value=True),
            patch("kiro_crew.dashboard.handlers.models_cold_probe.start_cold_model_probe") as probe,
            patch.object(phs, "fetch_answer") as fetch,
        ):
            response = await models.api_models_kas_bundle(SimpleNamespace(app={}))
        assert response.status == (200 if active else 503)
        body = json.loads(response.body)
        if active:
            assert [row["model_name"] for row in body] == ["auto"]
            probe.assert_called_once()
        else:
            assert body["code"] == models.KAS_BUNDLE_SIGN_IN_REQUIRED_CODE
            probe.assert_not_called()
        # Availability is a presence check; only readiness or a turn asks for a token.
        fetch.assert_not_called()

    @pytest.mark.parametrize("signed_in", [True, False])
    @pytest.mark.parametrize("active", [True, False])
    def test_catalog_uses_the_borrowed_key_only_for_an_empty_vault(
        self, data_home, pod_env, monkeypatch, signed_in, active
    ):
        from kiro_crew.agent_sdk import kas_model_catalog as catalog
        from kiro_crew.auth.bridge import vault_identity_fingerprint

        if not active:
            monkeypatch.delenv(phs.SOCKET_ENV)
        if signed_in:
            TokenStore(data_home).save(_token())
        borrowed = phs.catalog_identity()
        assert bool(borrowed) is active
        assert borrowed == phs.catalog_identity()
        assert catalog.vault_identity() == (vault_identity_fingerprint() if signed_in else borrowed)

    @pytest.mark.asyncio
    async def test_a_catalog_stamped_with_the_borrowed_key_is_served(
        self, data_home, pod_env, monkeypatch
    ):
        import json
        from types import SimpleNamespace

        from kiro_crew.agent_sdk import kas_model_catalog as catalog
        from kiro_crew.dashboard.handlers import models_kas_bundle as models

        monkeypatch.setattr(catalog, "_recorded", None)
        monkeypatch.setattr(catalog, "_recorded_identity", "")
        identity = catalog.vault_identity()
        assert identity.startswith("pod-host-sign-in:")
        assert catalog.record_catalog(
            {"configOptions": [{"id": "model", "type": "select", "options": [{"value": "auto"}]}]},
            "kas",
            identity=identity,
        )
        with (
            patch.object(models, "_bundle_installed", return_value=True),
            patch("kiro_crew.model_registry.refresh_kiro_catalog", return_value=(False, False)),
            patch("kiro_crew.dashboard.handlers.models_cold_probe.start_cold_model_probe") as probe,
        ):
            response = await models.api_models_kas_bundle(SimpleNamespace(app={}))
        assert response.status == 200
        assert [row["model_name"] for row in json.loads(response.body)] == ["auto"]
        probe.assert_not_called()
        monkeypatch.setenv(phs.SOCKET_ENV, str(data_home / "dashboard-5477.sock"))
        assert catalog.vault_identity() != identity
        assert catalog.recorded_catalog_snapshot(catalog.vault_identity()) is None

    @pytest.mark.asyncio
    async def test_the_refused_question_goes_to_the_host_for_a_bundle_only(
        self, data_home, pod_env
    ):
        with patch.object(phs, "ask_refused", return_value=True) as ask:
            assert await kas_host_auth.sign_in_refused("0" * 64, bundle=True) is True
            ask.assert_called_once_with("0" * 64)
            ask.reset_mock()
            assert await kas_host_auth.sign_in_refused("0" * 64, bundle=False) is True
        ask.assert_not_called()


class TestRefusedRoute:
    @pytest.mark.asyncio
    async def test_the_host_answers_from_its_own_vault(self, data_home, host_on, in_pod):
        TokenStore(data_home).save(_token())
        assert await asyncio.to_thread(phs.ask_refused, None) is False

    @pytest.mark.asyncio
    async def test_an_empty_host_vault_reads_as_refused(self, data_home, host_on, in_pod):
        assert await asyncio.to_thread(phs.ask_refused, None) is True

    @pytest.mark.asyncio
    async def test_a_down_pod_hears_nothing_and_keeps_its_retryable_error(
        self, data_home, host_on, in_pod
    ):
        with patch("kiro_crew.pod.runtime.main_pid", return_value=None):
            assert await asyncio.to_thread(phs.ask_refused, None) is False


class TestPodSideSocket:
    @pytest.mark.parametrize(
        "value",
        ["", "relative/dashboard-5476.sock", "/tmp/other.sock", "/tmp/dashboard-x.sock"],
    )
    def test_anything_but_a_dashboard_socket_path_is_inert(self, monkeypatch, value):
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv(phs.SOCKET_ENV, value)
        assert phs.active() is False

    def test_outside_a_pod_the_socket_is_ignored(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        monkeypatch.setenv(phs.SOCKET_ENV, str(Path("/x/dashboard-5476.sock")))
        assert phs.active() is False


class TestTheHarnessNamesItsRoute:
    """The KAS harness tells the answer which route its engine runs on."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bundle", [True, False])
    async def test_the_spawn_route_decides_whether_a_host_may_answer(self, bundle):
        from types import SimpleNamespace

        from kiro_crew.acp.harness import kas as kas_harness

        harness = SimpleNamespace(spawn_route=kas_route.KasRoute(bundle=bundle))
        with patch.object(kas_harness, "answer_get_access_token", return_value={}) as answer:
            await kas_harness.KasHarness.answer_request(
                harness, kas_harness.METHOD_KAS_AUTH_GET_ACCESS_TOKEN
            )
        answer.assert_called_once_with(bundle=bundle)

    @pytest.mark.asyncio
    async def test_a_harness_with_no_route_yet_is_not_a_bundle(self):
        from types import SimpleNamespace

        from kiro_crew.acp.harness import kas as kas_harness

        with patch.object(kas_harness, "answer_get_access_token", return_value={}) as answer:
            await kas_harness.KasHarness.answer_request(
                SimpleNamespace(spawn_route=None), kas_harness.METHOD_KAS_AUTH_GET_ACCESS_TOKEN
            )
        answer.assert_called_once_with(bundle=False)


class TestTheWiring:
    """The host gives a pod its socket, and Dev Fleet's grant opens the routes."""

    def test_dev_fleet_declares_the_route_grant(self):
        import json

        from kiro_crew.apps.core_route_grants import entry_grants_route

        manifest = Path(__file__).resolve().parent.parent / (
            "src/kiro_crew/apps/builtins/dev_fleet/app.json"
        )
        entries = json.loads(manifest.read_text(encoding="utf-8"))["permissions"]["api"]
        for route in (phs.TOKEN_ROUTE, phs.REFUSED_ROUTE):
            assert any(entry_grants_route(e, route, handler.NAMESPACE) for e in entries)

    @pytest.fixture
    def host_dashboard_socket(self, short_sock_dir, monkeypatch):
        import socket as socket_mod

        path = short_sock_dir / "dashboard-5476.sock"
        sock = socket_mod.socket(socket_mod.AF_UNIX)
        sock.bind(str(path))
        monkeypatch.setattr("kiro_crew.dashboard.urls.dashboard_socket_path", lambda p: path)
        monkeypatch.setattr(
            "kiro_crew.apps.core_route_grants.core_route_grant",
            lambda route, ns: RouteGrant(holder="dev-fleet"),
        )
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        try:
            yield path
        finally:
            sock.close()

    def test_a_pod_env_gets_the_host_socket_only_with_the_preview_on(self, host_dashboard_socket):
        env = {phs.SOCKET_ENV: "/inherited/dashboard-1.sock"}
        with patch.object(phs, "preview_on", return_value=True):
            phs.apply_to_pod_env(env, 5476)
        assert env[phs.SOCKET_ENV] == str(host_dashboard_socket)
        with patch.object(phs, "preview_on", return_value=False):
            phs.apply_to_pod_env(env, 5476)
        assert phs.SOCKET_ENV not in env

    def test_without_an_app_grant_a_pod_gets_no_socket_and_signs_in_by_itself(
        self, host_dashboard_socket, monkeypatch
    ):
        monkeypatch.setattr(
            "kiro_crew.apps.core_route_grants.core_route_grant", lambda route, ns: RouteGrant()
        )
        env = {phs.SOCKET_ENV: "/inherited/dashboard-1.sock"}
        with patch.object(phs, "preview_on", return_value=True):
            phs.apply_to_pod_env(env, 5476)
        assert phs.SOCKET_ENV not in env

    def test_a_pod_never_hands_its_own_pods_a_socket(self, host_dashboard_socket, monkeypatch):
        monkeypatch.setenv("KIROCREW_POD", "1")
        env: dict[str, str] = {}
        with patch.object(phs, "preview_on", return_value=True):
            phs.apply_to_pod_env(env, 5476)
        assert phs.SOCKET_ENV not in env

    def test_build_pod_env_applies_it(self, tmp_path):
        from kiro_crew.pod import runtime
        from kiro_crew.pod.config import PodConfig

        with patch.object(phs, "apply_to_pod_env") as apply:
            runtime.build_pod_env(PodConfig.load(), tmp_path / "home", 7000, tmp_path)
        assert apply.call_count == 1
