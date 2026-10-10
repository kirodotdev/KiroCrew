"""An account whose whoami carries only an account type.

``kiro-cli whoami --format json`` on an external identity provider sign-in
answers ``{"accountType":"ExternalIdP","email":""}``: no email, no ``startUrl``
and no trailing ``Profile:`` ARN. A type names a provider, not a user, and
nothing kiro-cli stores for that sign-in is evidenced to name the user (the
credential's stable claims belong to the IdP registration). So the account is
shown and its balance reported unavailable, and neither the API read nor the
``/usage`` scrape is spent on a reading that could not be published.
"""

from __future__ import annotations

import json
import sqlite3
import sys
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
    async def test_the_account_is_shown_with_no_balance_and_nothing_is_read(self):
        # Issue measurement: the usage API answers 403 and the scrape would parse
        # a 10000-credit plan. Nothing ties either to the user, so neither runs:
        # one whoami, then the account with no balance.
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        p = _patches(spawn, _api(None))
        with p[0], p[1], p[2] as api, p[3]:
            await sessions_mod._fetch_usage_bg()
        assert calls == ["whoami"], calls
        api.assert_not_called()
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
    async def test_the_message_is_specific_to_the_type_whoami_printed(self):
        # The new state still reaches an ExternalIdP sign-in when the API and
        # the scrape would both fail as a lapsed sign-in does, so the scoping
        # below does not hand ExternalIdP the "sign in again" remedy instead.
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI], usage_stdout=b"")
        api = sessions_mod.kiro_usage_api
        p = _patches(spawn, api.UsageResult(None, api.AUTH_NO_CREDENTIAL))
        with p[0], p[1], p[2] as fetch, p[3]:
            await sessions_mod._fetch_usage_bg()
        fetch.assert_not_called()
        assert calls == ["whoami"], calls
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
