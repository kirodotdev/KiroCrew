"""``kirocrew kas login | whoami | logout | route``: the vault sign-in from a shell.

These verbs are what the cloud sign-in runs on a bundle-route box over SSM, so their
OUTPUT is a contract: the device prompt must parse through ``parse_login_output``
(URL + ``code:`` line), ``whoami --format json`` through ``parse_whoami_output``,
``route`` must spell the constant the remote scripts compare against, and the exit
codes must follow kiro-cli's. The login service is faked at its class seam; nothing
here reaches an issuer.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import pytest

from kiro_crew import cli_kas, cli_kas_login
from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.cloud import login_vault
from kiro_crew.cloud.login import parse_login_output
from kiro_crew.cloud.login_target import parse_whoami_output

_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kirocrew kas")
    cli_kas.add_kas_parser(parser)
    return parser.parse_args(list(argv))


def _token(identity="builder_id", provider="BuilderId") -> KasToken:
    return KasToken(
        access_token="atk-secret",
        expires_at=_FUTURE,
        provider=provider,
        identity=identity,
        refresh_token="rtk-secret",
        profile_arn="arn:aws:kiro:us-east-1:1:profile/p1",
    )


@pytest.fixture
def home_store(tmp_path, monkeypatch) -> TokenStore:
    """Point the verbs' ``default_token_store`` at a vault under tmp_path."""
    from kiro_crew.auth import bridge

    store = TokenStore(tmp_path)
    monkeypatch.setattr(bridge, "default_token_store", lambda: store)
    return store


class TestParser:
    def test_the_sign_in_verbs_are_reachable_under_kas(self):
        args = _parse("login", "--provider", "idc", "--start-url", "https://a.b/start")
        assert (args.kas_action, args.provider, args.start_url) == (
            "login",
            "idc",
            "https://a.b/start",
        )
        assert _parse("login").provider == "builder_id"
        assert _parse("whoami", "--format", "json").format == "json"
        assert _parse("logout").kas_action == "logout"
        assert _parse("route").kas_action == "route"

    def test_the_usage_line_names_every_verb(self, capsys):
        assert cli_kas.kas_cmd(argparse.Namespace(kas_action=None)) == 2
        err = capsys.readouterr().err
        for verb in ("login", "whoami", "logout", "route"):
            assert verb in err


class TestRoute:
    def test_the_spelling_the_remote_scripts_compare_against(self):
        """The box prints it, the SSM script tests ``$KC_ROUTE`` against it: one constant."""
        assert cli_kas_login.ROUTE_KAS_BUNDLE == login_vault.ROUTE_KAS_BUNDLE
        assert cli_kas_login.ROUTE_KAS_BUNDLE != cli_kas_login.ROUTE_KIRO_CLI
        # The "unknown" exit must stay apart from argparse's 2 (verb absent) and the
        # shell's 127 (kirocrew absent), which the remote scripts read as "no answer".
        assert cli_kas_login.ROUTE_UNKNOWN_EXIT == login_vault.ROUTE_UNKNOWN_EXIT
        assert cli_kas_login.ROUTE_UNKNOWN_EXIT not in (0, 2, 127)
        # A no-action `kas` call is argparse's 2 territory, never the unknown exit.
        assert cli_kas.kas_cmd(argparse.Namespace(kas_action=None)) == 2

    @pytest.mark.parametrize(
        "backend, bundle, expected",
        [("kas", True, "kas-bundle"), ("kas", False, "kiro-cli"), ("kiro", True, "kiro-cli")],
    )
    def test_route_prints_the_owner(self, monkeypatch, capsys, backend, bundle, expected):
        from kiro_crew.config.loader import KiroCrewConfig

        class _Agent:
            acp_backend = backend
            kas_bundle = bundle

        class _Cfg:
            agent = _Agent()

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _Cfg()))
        assert cli_kas.kas_cmd(_parse("route")) == 0
        assert capsys.readouterr().out.strip() == expected

    def test_an_unreadable_config_is_a_readable_error_not_kiro_cli(self, monkeypatch, capsys):
        """Printed from this verb, a silent `kiro-cli` would send a bundle-route box
        down a branch it cannot run; the owner is UNKNOWN and the verb says why."""
        from kiro_crew.config.loader import KiroCrewConfig

        def _boom(cls):
            raise ValueError("Expecting value: line 1 column 1")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
        assert cli_kas.kas_cmd(_parse("route")) == cli_kas_login.ROUTE_UNKNOWN_EXIT == 1
        out = capsys.readouterr()
        assert out.out == ""
        assert "could not read the Kiro Crew config" in out.err
        assert "Expecting value" in out.err
        assert "fix the config" in out.err

    @pytest.mark.parametrize("flag", ["_base_unreadable", "_overlay_unreadable"])
    def test_a_degraded_load_is_refused_the_same_way(self, monkeypatch, capsys, flag):
        """The loader does not raise on a malformed config.json: it returns DEFAULTS
        with a flag set. Defaults say "kiro-cli" about a host that may be on the
        bundle route, and a sign-out acting on that would empty the wrong store."""
        from kiro_crew.config.loader import KiroCrewConfig

        class _Agent:
            acp_backend = "kas"
            kas_bundle = True

        class _Cfg:
            agent = _Agent()
            _base_unreadable = False
            _overlay_unreadable = False

        cfg = _Cfg()
        setattr(cfg, flag, True)
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
        assert cli_kas.kas_cmd(_parse("route")) == 1
        out = capsys.readouterr()
        assert out.out == ""
        assert "unparseable" in out.err
        assert ("config.json" if flag == "_base_unreadable" else "config.local.json") in out.err


class TestWhoami:
    def test_signed_out_exits_one_like_kiro_cli(self, home_store, capsys):
        assert cli_kas.kas_cmd(_parse("whoami", "--format", "json")) == 1
        out = capsys.readouterr()
        assert out.out == ""
        assert "Not signed in" in out.err

    def test_json_parses_through_the_cloud_whoami_parser(self, home_store, capsys):
        home_store.save(_token())
        assert cli_kas.kas_cmd(_parse("whoami", "--format", "json")) == 0
        out = capsys.readouterr().out
        assert parse_whoami_output(out) == {"account_type": "BuilderId"}
        assert "atk-secret" not in out and "rtk-secret" not in out

    def test_text_names_the_kind_and_never_the_token(self, home_store, capsys):
        home_store.save(_token("social", "Google"))
        assert cli_kas.kas_cmd(_parse("whoami")) == 0
        out = capsys.readouterr().out
        assert "SocialGoogle" in out
        assert "atk-secret" not in out and "rtk-secret" not in out


class TestLogout:
    def test_signed_out_is_a_clean_no_op_that_still_moves_the_generation(self, home_store, capsys):
        """A dashboard login may be pending over an empty vault; this sign-out must
        still refuse it when its code is approved, so the generation moves."""
        before = home_store.signout_generation()
        assert cli_kas.kas_cmd(_parse("logout")) == 0
        assert "nothing to sign out" in capsys.readouterr().out
        assert home_store.signout_generation() != before

    def test_logout_empties_every_slot(self, home_store, capsys):
        home_store.save(_token())
        home_store.save(_token("social", "Google"))
        assert cli_kas.kas_cmd(_parse("logout")) == 0
        assert home_store.resolve() is None
        assert "Signed out" in capsys.readouterr().out


class _FakeService:
    """Scripted stand-in for KasLoginService: begin answers once, poll replays a list."""

    polls: list[dict] = []
    begun: list[dict] = []
    closed = 0

    def __init__(self, store):
        self.store = store

    async def begin_device(self, provider, *, start_url="", region="", replaces=""):
        _FakeService.begun.append(
            {"provider": provider, "start_url": start_url, "region": region, "replaces": replaces}
        )
        return {
            "login_id": "lid-1",
            "user_code": "WXYZ-1234",
            "verification_uri_complete": "https://device.sso.us-east-1.amazonaws.com/?user_code=WXYZ-1234",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }

    async def poll_device(self, login_id):
        assert login_id == "lid-1"
        return _FakeService.polls.pop(0)

    async def close(self):
        _FakeService.closed += 1


@pytest.fixture
def fake_service(monkeypatch):
    from kiro_crew.auth import service

    _FakeService.polls = []
    _FakeService.begun = []
    _FakeService.closed = 0
    monkeypatch.setattr(service, "KasLoginService", _FakeService)
    # No real waiting between polls: the cadence is the issuer's concern, not this test's.
    monkeypatch.setattr(cli_kas_login, "POLL_INTERVAL_SECS", 0)
    return _FakeService


class TestLogin:
    def test_the_prompt_parses_like_kiro_cli_output(self, home_store, fake_service, capsys):
        fake_service.polls = [
            {"status": "pending"},
            {"status": "authorized", "provider": "BuilderId"},
        ]
        assert cli_kas.kas_cmd(_parse("login")) == 0
        out = capsys.readouterr().out
        prompt = parse_login_output(out)
        assert prompt.url == "https://device.sso.us-east-1.amazonaws.com/?user_code=WXYZ-1234"
        assert prompt.code == "WXYZ-1234"
        assert not prompt.already_logged_in
        assert "Signed in to Kiro Crew" in out
        assert fake_service.closed == 1
        assert fake_service.begun == [
            {"provider": "builder_id", "start_url": "", "region": "", "replaces": ""}
        ]

    def test_an_idc_login_forwards_its_start_url_and_region(self, home_store, fake_service):
        fake_service.polls = [{"status": "authorized", "provider": "Enterprise"}]
        args = _parse(
            "login",
            "--provider",
            "idc",
            "--start-url",
            "https://acme.awsapps.com/start",
            "--region",
            "eu-west-1",
        )
        assert cli_kas.kas_cmd(args) == 0
        assert fake_service.begun[0]["start_url"] == "https://acme.awsapps.com/start"
        assert fake_service.begun[0]["region"] == "eu-west-1"

    def test_an_idc_login_without_a_start_url_is_a_usage_error(self, home_store, fake_service):
        assert cli_kas.kas_cmd(_parse("login", "--provider", "idc")) == 2
        assert fake_service.begun == []

    def test_a_live_sign_in_answers_already_signed_in(self, home_store, fake_service, capsys):
        """kiro-cli ignores a login over a live session; so does this, in words the
        cloud sign-in reads as ``already_logged_in`` and then verifies the identity of."""
        home_store.save(_token())
        assert cli_kas.kas_cmd(_parse("login")) == 0
        out = capsys.readouterr().out
        assert parse_login_output(out).already_logged_in
        assert fake_service.begun == []

    def test_a_lapsed_slot_is_named_as_the_one_this_login_replaces(self, home_store, fake_service):
        """A stale higher-priority slot would outrank the fresh sign-in at resolve time."""
        lapsed = KasToken(
            access_token="old",
            expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
            provider="ExternalIdp",
            identity="external_idp",
            refresh_token=None,
        )
        home_store.save(lapsed)
        fake_service.polls = [{"status": "authorized", "provider": "BuilderId"}]
        assert cli_kas.kas_cmd(_parse("login")) == 0
        assert fake_service.begun[0]["replaces"] == "external_idp"

    def test_an_expired_code_is_a_failure_with_the_remedy(self, home_store, fake_service, capsys):
        fake_service.polls = [{"status": "expired"}]
        assert cli_kas.kas_cmd(_parse("login")) == 1
        assert "expired" in capsys.readouterr().err

    def test_a_lost_connection_while_polling_is_a_line_not_a_traceback(
        self, home_store, fake_service, capsys, monkeypatch
    ):
        import aiohttp

        async def _drop(self, login_id):
            raise aiohttp.ClientConnectionError("connection reset")

        monkeypatch.setattr(fake_service, "poll_device", _drop)
        assert cli_kas.kas_cmd(_parse("login")) == 1
        err = capsys.readouterr().err
        assert "lost contact with the sign-in service" in err
        assert "Traceback" not in err

    def test_a_sign_out_during_the_login_is_a_line_not_a_traceback(
        self, home_store, fake_service, capsys, monkeypatch
    ):
        from kiro_crew.auth.service import UnknownLoginError

        async def _gone(self, login_id):
            raise UnknownLoginError(login_id)

        monkeypatch.setattr(fake_service, "poll_device", _gone)
        assert cli_kas.kas_cmd(_parse("login")) == 1
        err = capsys.readouterr().err
        assert "cancelled by a sign-out" in err
        assert "Traceback" not in err

    def test_a_store_failure_is_a_failure_naming_its_code(self, home_store, fake_service, capsys):
        fake_service.polls = [{"status": "error", "code": "token_store_failed"}]
        assert cli_kas.kas_cmd(_parse("login")) == 1
        assert "token_store_failed" in capsys.readouterr().err

    def test_only_one_code_line_is_printed(self, home_store, fake_service, capsys):
        """``parse_login_output`` takes the FIRST ``code: ...`` match, so the waiting
        line must not read as a second code."""
        fake_service.polls = [{"status": "authorized", "provider": "BuilderId"}]
        cli_kas.kas_cmd(_parse("login"))
        out = capsys.readouterr().out
        assert out.lower().count("code:") == 1
