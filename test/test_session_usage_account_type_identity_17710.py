"""An account whose whoami carries only an account type.

``kiro-cli whoami --format json`` on an external identity provider sign-in
answers ``{"accountType":"ExternalIdP","email":""}``: no email, no ``startUrl``
and no trailing ``Profile:`` ARN. A type names a provider, not a user, and
nothing kiro-cli stores for that sign-in is evidenced to name the user (the
credential's stable claims belong to the IdP registration). So the account is
shown and its balance reported unavailable. No API or scrape reading is
published. The API call runs only so a refused credential keeps the
``signin_required`` remedy; when it finds no credential at all, the ``/usage``
scrape runs once and its outcome alone decides between that remedy and the
no-balance state, since an unreadable store is not proof the sign-in broke.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.dashboard.handlers.sessions as sessions_mod

#: The whoami stdout of an external identity provider sign-in.
EXTERNAL_IDP_WHOAMI = b'{"accountType":"ExternalIdP","email":""}\n'

#: The ``auth_kv`` key kiro-cli stores the external identity provider token under.
EXTERNAL_IDP_KEY = "kirocli:external-idp:token"

SAMPLE_USAGE = (
    "Estimated Usage\n"
    "Credits used: 2480.1\n"
    "You have covered in plan (2480.1 of 10000) credits, "
    "resets on 2026-11-01 | KIRO POWER\n"
)

ACCOUNT_ONLY = {"account_type": "ExternalIdP", "available": False, "reason": "account_unproven"}
SIGNIN_REQUIRED = {"account_type": "ExternalIdP", "available": False, "reason": "signin_required"}


@pytest.fixture(autouse=True)
def _reset_usage_globals():
    sessions_mod._usage_cache = {}
    sessions_mod._usage_cache_ts = 0.0
    sessions_mod._usage_fetching = False
    sessions_mod._usage_scrape_failures = 0
    sessions_mod._usage_scrape_backoff_until = 0.0
    yield
    sessions_mod._usage_cache = {}
    sessions_mod._usage_cache_ts = 0.0
    sessions_mod._usage_fetching = False
    sessions_mod._usage_scrape_failures = 0
    sessions_mod._usage_scrape_backoff_until = 0.0


def _proc(stdout: bytes) -> MagicMock:
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    proc.returncode = 0
    return proc


def _spawn_by_argv(
    whoami_outputs: list[bytes],
    usage_stdout: bytes = SAMPLE_USAGE.encode(),
    on_usage=None,
):
    """Fake ``create_subprocess_exec``: whoami calls answer from ``whoami_outputs``
    in order (the last one repeats), the ``/usage`` scrape answers ``usage_stdout``
    after running ``on_usage`` (a sign-in change while the child runs).

    Goes through the real ``_fetch_whoami_or_none`` parse, so the test exercises
    the issue's exact whoami bytes rather than a hand-built identity dict."""
    calls: list[str] = []
    remaining = list(whoami_outputs)

    async def spawn(*argv, **_kwargs):
        if "whoami" in argv:
            calls.append("whoami")
            out = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            return _proc(out)
        calls.append("usage")
        if on_usage is not None:
            on_usage()
        return _proc(usage_stdout)

    return spawn, calls


def _api(usage=None):
    api = sessions_mod.kiro_usage_api
    return api.UsageResult(usage, api.AUTH_OK if usage is not None else api.AUTH_OTHER)


def _patches(spawn, api_result):
    return (
        patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
        patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
        patch.object(
            sessions_mod.kiro_usage_api,
            "fetch_usage_limits",
            MagicMock(return_value=api_result),
        ),
        patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)),
    )


def _publish_spy():
    """Record every payload ``_publish_usage`` receives, still publishing it.

    The final cache alone cannot show that a number was never published: a
    reading published and then overwritten by the marker a few awaits later
    would still have been served to any reader in between."""
    return patch.object(sessions_mod, "_publish_usage", wraps=sessions_mod._publish_usage)


def _assert_only_markers_published(published) -> None:
    allowed = set(ACCOUNT_ONLY)
    assert published.call_args_list, "nothing was published"
    for call in published.call_args_list:
        assert set(call.args[0]) <= allowed, call.args[0]


class TestWhoamiShapeFromTheIssue:
    @pytest.mark.asyncio
    async def test_parses_to_an_account_type_and_nothing_else(self):
        spawn, _ = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        with (
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)),
        ):
            identity = await sessions_mod._fetch_whoami_or_none("/bin/kiro")
        assert identity == {"account_type": "ExternalIdP"}


class TestAccountTypeOnly:
    def test_the_issue_shape_is_type_only(self):
        assert sessions_mod._account_type_only({"account_type": "ExternalIdP"})

    @pytest.mark.parametrize("spelling", ["ExternalIdp", "external_idp", "EXTERNALIDP"])
    def test_the_token_store_spellings_count(self, spelling):
        assert sessions_mod._account_type_only({"account_type": spelling})

    @pytest.mark.parametrize(
        "identity",
        [
            {},
            {"account_type": ""},
            # Only ExternalIdP: another type reporting only itself takes the
            # ordinary path and keeps that path's remedy.
            {"account_type": "BuilderId"},
            {"account_type": "IamIdentityCenter"},
            {"account_type": "SocialGitHub"},
            {"account_type": "SomeFutureProvider"},
            {"account_type": "ExternalIdP", "email": "a@example.com"},
            {"account_type": "IamIdentityCenter", "email": "a@example.com"},
            {"account_type": "IamIdentityCenter", "start_url": "https://x.example/start"},
            {"account_type": "IamIdentityCenter", "_profile_arn": "arn:aws:x"},
        ],
    )
    def test_anything_naming_a_user_or_no_type_is_not(self, identity):
        assert not sessions_mod._account_type_only(identity)


class TestExternalIdpRefresh:
    @pytest.mark.asyncio
    async def test_the_account_is_shown_with_no_balance_and_nothing_is_published(self):
        # Issue measurement: the usage API answers 403 and the scrape would parse
        # a 10000-credit plan. Nothing ties either to the user: the API runs only
        # for its auth_state, the scrape never runs, and the account is shown
        # with no balance.
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        p = _patches(spawn, _api(None))
        with p[0], p[1], p[2] as api, p[3]:
            await sessions_mod._fetch_usage_bg()
        assert calls == ["whoami"], calls
        api.assert_called_once_with(expected_arn=None)
        assert sessions_mod._usage_cache == ACCOUNT_ONLY

    @pytest.mark.asyncio
    async def test_a_reading_the_api_returns_is_never_published(self):
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        plan = {"credits_used": 10.0, "credits_plan": 10000.0, "source": "api"}
        p = _patches(spawn, _api(plan))
        with p[0], p[1], p[2], p[3], _publish_spy() as published:
            await sessions_mod._fetch_usage_bg()
        assert "usage" not in calls
        _assert_only_markers_published(published)
        assert sessions_mod._usage_cache == ACCOUNT_ONLY

    @pytest.mark.asyncio
    async def test_an_earlier_reading_is_not_kept(self):
        # The earlier reading may be another user's: it is not kept on screen
        # under an account the refresh cannot tie it to.
        sessions_mod._usage_cache = {
            "credits_plan": 10000.0,
            "credits_used": 10.0,
            "account_type": "ExternalIdP",
        }
        spawn, _ = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        p = _patches(spawn, _api(None))
        with p[0], p[1], p[2], p[3]:
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == ACCOUNT_ONLY

    @pytest.mark.asyncio
    async def test_the_state_holds_while_the_scrape_is_parked(self):
        # A parked or failing scrape cannot turn the state back into a bare
        # failure, because the scrape is never consulted for this shape.
        sessions_mod._usage_scrape_backoff_until = 9e18
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        p = _patches(spawn, _api(None))
        with p[0], p[1], p[2], p[3]:
            await sessions_mod._fetch_usage_bg()
        assert "usage" not in calls
        assert sessions_mod._usage_cache == ACCOUNT_ONLY

    @pytest.mark.asyncio
    async def test_the_scrape_back_off_is_untouched(self):
        sessions_mod._usage_scrape_failures = 2
        spawn, _ = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        p = _patches(spawn, _api(None))
        with p[0], p[1], p[2], p[3]:
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_scrape_failures == 2

    @pytest.mark.asyncio
    async def test_a_refused_credential_keeps_the_signin_remedy_without_a_scrape(self):
        # A 401 from GetUsageLimits is the server refusing the credential: a
        # sign-in problem, not "this sign-in never shows a balance". No scrape
        # is needed to corroborate it.
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        api = sessions_mod.kiro_usage_api
        p = _patches(spawn, api.UsageResult(None, api.AUTH_REJECTED))
        with p[0], p[1], p[2] as fetch, p[3]:
            await sessions_mod._fetch_usage_bg()
        fetch.assert_called_once_with(expected_arn=None)
        assert calls == ["whoami"], calls
        assert sessions_mod._usage_cache == SIGNIN_REQUIRED

    @pytest.mark.asyncio
    async def test_no_credential_with_a_working_scrape_stays_account_unproven(self):
        # The reader saw no unexpired credential, but kiro-cli still answers
        # /usage (it renews its own store, or reads one this reader does not
        # enumerate): the sign-in works, so "sign in again" would be wrong. The
        # scrape's 10000-credit plan is never published.
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        api = sessions_mod.kiro_usage_api
        p = _patches(spawn, api.UsageResult(None, api.AUTH_NO_CREDENTIAL))
        with p[0], p[1], p[2], p[3], _publish_spy() as published:
            await sessions_mod._fetch_usage_bg()
        assert calls == ["whoami", "usage"], calls
        assert sessions_mod._usage_cache == ACCOUNT_ONLY
        assert sessions_mod._usage_scrape_failures == 0
        _assert_only_markers_published(published)

    @pytest.mark.asyncio
    async def test_no_credential_with_a_failing_scrape_asks_for_a_sign_in(self):
        # No credential AND kiro-cli cannot answer /usage either: that pair is
        # the evidence a lapsed sign-in leaves, so the remedy is named. The
        # failed attempt feeds the back-off like any other scrape.
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI], usage_stdout=b"")
        api = sessions_mod.kiro_usage_api
        p = _patches(spawn, api.UsageResult(None, api.AUTH_NO_CREDENTIAL))
        with p[0], p[1], p[2], p[3], _publish_spy() as published:
            await sessions_mod._fetch_usage_bg()
        assert calls == ["whoami", "usage"], calls
        assert sessions_mod._usage_cache == SIGNIN_REQUIRED
        assert sessions_mod._usage_scrape_failures == 1
        _assert_only_markers_published(published)

    @pytest.mark.asyncio
    async def test_a_corroborating_scrape_that_parks_reports_the_park(self):
        # The third consecutive miss parks the scrape. The refresh tells its
        # caller so, as the ordinary path does, instead of reporting nothing.
        sessions_mod._usage_scrape_failures = sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD - 1
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI], usage_stdout=b"")
        api = sessions_mod.kiro_usage_api
        p = _patches(spawn, api.UsageResult(None, api.AUTH_NO_CREDENTIAL))
        with p[0], p[1], p[2], p[3]:
            result = await sessions_mod._fetch_usage_bg()
        assert calls == ["whoami", "usage"], calls
        assert result == sessions_mod._SKIPPED_SCRAPE_PARKED
        assert sessions_mod._usage_cache == SIGNIN_REQUIRED

    @pytest.mark.asyncio
    async def test_no_credential_while_the_scrape_is_parked_asks_for_a_sign_in(self):
        # A parked scrape has already failed repeatedly; that is the evidence,
        # as on the ordinary path's parked marker, and nothing is spawned.
        sessions_mod._usage_scrape_backoff_until = 9e18
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        api = sessions_mod.kiro_usage_api
        p = _patches(spawn, api.UsageResult(None, api.AUTH_NO_CREDENTIAL))
        with p[0], p[1], p[2], p[3]:
            await sessions_mod._fetch_usage_bg()
        assert calls == ["whoami"], calls
        assert sessions_mod._usage_cache == SIGNIN_REQUIRED

    @pytest.mark.asyncio
    async def test_a_non_auth_failure_stays_account_unproven(self):
        # A 403 (the issue's measurement) is not auth-class, so the sign-in-type
        # state stands and no "sign in again" is claimed.
        spawn, _ = _spawn_by_argv([EXTERNAL_IDP_WHOAMI], usage_stdout=b"")
        api = sessions_mod.kiro_usage_api
        p = _patches(spawn, api.UsageResult(None, api.AUTH_OTHER))
        with p[0], p[1], p[2], p[3]:
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == ACCOUNT_ONLY

    @pytest.mark.asyncio
    async def test_an_email_on_both_sides_still_publishes(self):
        # The healthy side: a per-user field proven on both sides publishes the
        # scrape exactly as before.
        whoami = b'{"accountType":"IamIdentityCenter","email":"a@example.com"}\n'
        spawn, calls = _spawn_by_argv([whoami])
        p = _patches(spawn, _api(None))
        with p[0], p[1], p[2], p[3]:
            await sessions_mod._fetch_usage_bg()
        assert "usage" in calls
        cache = sessions_mod._usage_cache
        assert cache.get("credits_plan") == 10000.0, cache
        assert cache.get("email") == "a@example.com"


class TestASwitchWithinOneRegistration:
    """User A signed in when the refresh starts, user B of the SAME IdP
    registration by the time any read would land. kiro-cli's real auth store is
    used, so the registration-level claims it keeps (``client_id``, ``scopes``)
    are identical for A and B and only per-user fields differ."""

    @staticmethod
    def _write_store(home, user: str) -> None:
        from kiro_crew import identity_stores

        db = identity_stores.selected_store(sys.platform, home)
        db.parent.mkdir(parents=True, exist_ok=True)
        blob = {
            "access_token": f"token-{user}",
            "refresh_token": f"refresh-{user}",
            "expires_at": "2026-10-08T01:00:00Z",
            "client_id": "org-registration",
            "scopes": ["openid", "offline_access"],
            "issuer_url": "https://idp.example",
            "token_endpoint": "https://idp.example/token",
            "sub": f"user-{user}",
        }
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE IF NOT EXISTS auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("DELETE FROM auth_kv")
        con.execute("INSERT INTO auth_kv VALUES (?, ?)", (EXTERNAL_IDP_KEY, json.dumps(blob)))
        con.commit()
        con.close()

    @pytest.mark.asyncio
    async def test_a_switch_from_a_to_b_does_not_publish_as_b(self, tmp_path, monkeypatch):
        from kiro_crew import kiro_prerequisite

        for var in ("XDG_DATA_HOME", "LOCALAPPDATA", "APPDATA"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(kiro_prerequisite.Path, "home", lambda: tmp_path)
        self._write_store(tmp_path, "a")
        a_plan = {"credits_used": 10.0, "credits_plan": 10000.0, "source": "api"}
        spawn, calls = _spawn_by_argv(
            [EXTERNAL_IDP_WHOAMI], on_usage=lambda: self._write_store(tmp_path, "b")
        )
        p = _patches(spawn, _api(dict(a_plan)))
        with (
            p[0],
            p[1],
            p[2],
            p[3],
            patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True),
        ):
            # The switch lands while the refresh runs; a second refresh then
            # reads B's store.
            await sessions_mod._fetch_usage_bg()
            self._write_store(tmp_path, "b")
            await sessions_mod._fetch_usage_bg()
        cache = sessions_mod._usage_cache
        assert cache.get("credits_plan") is None, "A's balance was published while B is signed in"
        assert cache == ACCOUNT_ONLY


class TestOtherTypesKeepTheirRemedy:
    """A Builder ID or Identity Center whoami that prints only its type (an
    Identity Center sign-in with no readable start URL is an expected shape)
    is not "this sign-in type": its sign-in has lapsed, and the ordinary path
    names that remedy. It must not be marked ``account_unproven``."""

    @pytest.mark.parametrize("account_type", ["BuilderId", "IamIdentityCenter"])
    @pytest.mark.asyncio
    async def test_type_only_non_external_idp_keeps_signin_remedy(self, account_type):
        whoami = ('{"accountType":"%s","email":""}\n' % account_type).encode()

        async def spawn(*argv, **_kwargs):
            if "whoami" in argv:
                return _proc(whoami)
            failed = _proc(b"")
            failed.wait = AsyncMock(return_value=1)
            failed.returncode = 1
            return failed

        api = sessions_mod.kiro_usage_api
        p = _patches(spawn, api.UsageResult(None, api.AUTH_NO_CREDENTIAL))
        with p[0], p[1], p[2], p[3]:
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False
        assert (
            sessions_mod._usage_cache.get("reason") == "signin_required"
        ), sessions_mod._usage_cache


class TestAFailureInTheBranch:
    """A timeout or error inside the account-type-only branch leaves that
    branch's no-balance marker, not the "no reading yet" one, whose wording
    promises a reading this sign-in never gets."""

    @pytest.mark.asyncio
    async def test_an_error_in_the_api_call_leaves_account_unproven(self):
        spawn, _ = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        p = _patches(spawn, _api(None))
        with p[0], p[1], p[2] as fetch, p[3]:
            fetch.side_effect = RuntimeError("executor shut down")
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == ACCOUNT_ONLY

    @pytest.mark.asyncio
    async def test_a_timeout_leaves_account_unproven(self):
        # The API call hangs past the refresh deadline (a wedged TLS handshake):
        # it is held on an event released only after the refresh returned, so
        # the deadline is what ends it. The deadline expires only once the hang
        # has been entered, so setup taking any amount of time cannot fire it
        # early and land the refresh on the ordinary failure path instead.
        spawn, _ = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        p = _patches(spawn, _api(None))
        release = threading.Event()
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        real_wait_for = asyncio.wait_for
        deadline = object()

        def _hang(**_kwargs):
            loop.call_soon_threadsafe(entered.set)
            release.wait(30)
            return _api(None)

        async def _deadline_after_entry(aw, timeout):
            if timeout is not deadline:
                return await real_wait_for(aw, timeout)
            task = asyncio.ensure_future(aw)
            entry = asyncio.ensure_future(entered.wait())
            done, _ = await asyncio.wait(
                {task, entry}, timeout=10, return_when=asyncio.FIRST_COMPLETED
            )
            entry.cancel()
            assert done, "neither the refresh nor the API call finished within 10 s"
            assert not task.done(), "the refresh ended before the API call hung"
            # The real wait_for, already out of time: it cancels the refresh
            # and raises TimeoutError exactly as the expired deadline does.
            return await real_wait_for(task, 0)

        try:
            with (
                p[0],
                p[1],
                p[2] as fetch,
                p[3],
                patch.object(sessions_mod, "_USAGE_FETCH_DEADLINE_SECS", deadline),
                patch.object(sessions_mod.asyncio, "wait_for", _deadline_after_entry),
            ):
                fetch.side_effect = _hang
                await sessions_mod._fetch_usage_bg()
        finally:
            release.set()
        assert entered.is_set()
        assert sessions_mod._usage_cache == ACCOUNT_ONLY

    @pytest.mark.asyncio
    async def test_a_switch_seen_after_the_error_is_judged_as_usual(self):
        # The whoami adjacent to the failure names a user: the branch's marker
        # does not describe that sign-in, so the ordinary failure path runs.
        named = b'{"accountType":"IamIdentityCenter","email":"a@example.com"}\n'
        spawn, _ = _spawn_by_argv([EXTERNAL_IDP_WHOAMI, named])
        p = _patches(spawn, _api(None))
        with p[0], p[1], p[2] as fetch, p[3]:
            fetch.side_effect = RuntimeError("executor shut down")
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("reason") != "account_unproven"
        assert sessions_mod._usage_cache.get("available") is False
