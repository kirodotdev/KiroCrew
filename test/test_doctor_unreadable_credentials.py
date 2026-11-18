"""`kirocrew doctor` reports a credentials file it may not read instead of crashing.

The credentials read can raise PermissionError (inside an agent sandbox, or on
a file with the wrong owner). The Configuration section keeps going with the
environment's credentials only, and the shared data-home helper reports the
denial: a skip inside a sandbox, an issue outside one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.config import loader
from kiro_crew.config.loader_credentials import CREDENTIAL_KEYS
from kiro_crew.doctor_checks import confinement, services


def _cfg(url: str, exc: BaseException | None = None):
    def _denied():
        raise exc if exc is not None else PermissionError(1, "Operation not permitted")

    return SimpleNamespace(
        agent=SimpleNamespace(provider="kiro", model="auto", approval_mode="auto"),
        dashboard=SimpleNamespace(url=url),
        load_credentials=_denied,
    )


@pytest.fixture(autouse=True)
def _clean_env(_floor_monkeypatch):
    _floor_monkeypatch.delenv("KIROCREW_PORT", raising=False)
    for key in CREDENTIAL_KEYS:
        _floor_monkeypatch.delenv(key, raising=False)


def _confined(monkeypatch, value):
    def _probe():
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(confinement, "_doctor_vantage_confined", _probe)


def test_unreadable_credentials_outside_a_sandbox_are_an_issue(monkeypatch, capsys):
    _confined(monkeypatch, False)
    issues: list[str] = []

    binding = services._doctor_configuration(_cfg("http://localhost:8080"), issues)

    out = capsys.readouterr().out
    assert "credentials: ❌ a file it reads is not readable (Operation not permitted)" in out
    assert issues == ["credentials: a file it reads is not readable"]
    assert binding.creds == {}
    assert binding.has_slack is False


def test_unreadable_credentials_inside_a_sandbox_are_a_skip(monkeypatch, capsys):
    _confined(monkeypatch, True)
    issues: list[str] = []

    services._doctor_configuration(_cfg("http://localhost:8080"), issues)

    assert "credentials: ⏭  skipped" in capsys.readouterr().out
    assert issues == []


def test_a_broken_confinement_probe_counts_the_issue(monkeypatch, capsys):
    _confined(monkeypatch, RuntimeError("probe failed"))
    issues: list[str] = []

    services._doctor_configuration(_cfg("http://localhost:8080"), issues)

    assert issues == ["credentials: a file it reads is not readable"]


def test_unreadable_credentials_keep_the_environment_tokens(monkeypatch, capsys):
    _confined(monkeypatch, True)
    monkeypatch.setenv("SLACK_APP_TOKEN", "app-token-value")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "bot-token-value")

    binding = services._doctor_configuration(_cfg("http://localhost:8080"), [])

    assert binding.creds["SLACK_APP_TOKEN"] == "app-token-value"
    assert binding.creds["SLACK_BOT_TOKEN"] == "bot-token-value"
    assert binding.has_slack is True


def test_unreadable_credentials_resolve_environment_secret_references(monkeypatch, capsys):
    _confined(monkeypatch, True)
    monkeypatch.setenv("SLACK_APP_TOKEN", "secret://missing-name")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "bot-token-value")
    seen: list[dict[str, str]] = []

    def _resolve(values, **_kwargs):
        seen.append(dict(values))
        return {k: v for k, v in values.items() if not v.startswith("secret://")}

    monkeypatch.setattr(loader, "resolve_env_credentials", _resolve)
    binding = services._doctor_configuration(_cfg("http://localhost:8080"), [])

    assert seen and seen[0]["SLACK_APP_TOKEN"] == "secret://missing-name"
    assert "SLACK_APP_TOKEN" not in binding.creds
    assert binding.has_slack is False


def test_unreadable_credentials_without_strerror_still_report(monkeypatch, capsys):
    _confined(monkeypatch, False)

    services._doctor_configuration(_cfg("http://localhost:8080", PermissionError()), [])

    assert "credentials: ❌ a file it reads is not readable" in capsys.readouterr().out


# ── Checks that read the credentials report "unknown", never "missing" ──
#
# With the credentials file unreadable, a token absent from the environment may
# still be in that file. Reporting it missing (and counting an issue) would fail
# doctor on something it never measured.


def test_unreadable_credentials_flag_the_binding(monkeypatch, capsys):
    _confined(monkeypatch, True)

    binding = services._doctor_configuration(_cfg("http://localhost:8080"), [])

    assert binding.creds_unreadable is True


def test_readable_credentials_do_not_flag_the_binding(monkeypatch, capsys):
    cfg = _cfg("http://localhost:8080")
    cfg.load_credentials = lambda: {}

    binding = services._doctor_configuration(cfg, [])

    assert binding.creds_unreadable is False


def _remote_bind(monkeypatch):
    # The public build always binds loopback; a managed proxy is the one case
    # that binds all interfaces, so the remote branch is reached by patching.
    from kiro_crew import cli_doctor

    monkeypatch.setattr(cli_doctor, "is_local_only", lambda _host, _slack: False)


def test_remote_bind_with_unreadable_credentials_adds_no_slack_issue(monkeypatch, capsys):
    _confined(monkeypatch, True)
    _remote_bind(monkeypatch)
    issues: list[str] = []

    services._doctor_configuration(_cfg("http://0.0.0.0:8080"), issues)

    out = capsys.readouterr().out
    assert "Slack unknown — credentials not readable" in out
    assert "dashboard auth: remote bind without Slack" not in issues


def test_remote_bind_with_readable_credentials_still_flags_missing_slack(monkeypatch, capsys):
    _remote_bind(monkeypatch)
    cfg = _cfg("http://0.0.0.0:8080")
    cfg.load_credentials = lambda: {}
    issues: list[str] = []

    services._doctor_configuration(cfg, issues)

    assert issues == ["dashboard auth: remote bind without Slack"]


def _discord_cfg():
    return SimpleNamespace(
        discord=SimpleNamespace(
            enabled=True,
            bot_token="",
            allowed_user_ids=["1"],
            allowed_thread_ids=[],
            allowed_channel_ids=[],
        )
    )


@pytest.mark.parametrize(
    ("unreadable", "want_issue"), [(True, False), (False, True)], ids=["unreadable", "readable"]
)
def test_discord_token_unknown_when_credentials_unreadable(
    monkeypatch, capsys, unreadable, want_issue
):
    from kiro_crew import cli_doctor
    from kiro_crew.doctor_checks import channels

    monkeypatch.setattr(
        cli_doctor,
        "_discord_intent_grants",
        lambda _token: SimpleNamespace(
            message_content=None, server_members=None, presence=None, application_id=None
        ),
    )
    monkeypatch.setattr(channels, "_discord_msg_content_line", lambda *a, **k: None)
    monkeypatch.setattr(channels, "_discord_unused_intent_line", lambda *a, **k: None)
    monkeypatch.setattr(channels, "_discord_live_state", lambda _port: None)
    monkeypatch.setattr(channels, "_discord_install_line", lambda *a, **k: None)
    issues: list[str] = []

    channels._doctor_discord(_discord_cfg(), {}, None, issues, creds_unreadable=unreadable)

    out = capsys.readouterr().out
    assert ("discord: enabled without a bot token" in issues) is want_issue
    assert ("token:       ⏭  unknown" in out) is unreadable


@pytest.mark.parametrize("unreadable", [True, False], ids=["unreadable", "readable"])
def test_slack_unknown_when_credentials_unreadable(capsys, unreadable):
    from kiro_crew.doctor_checks import channels

    channels._doctor_slack(SimpleNamespace(), {}, False, [], creds_unreadable=unreadable)

    out = capsys.readouterr().out
    assert ("status:      ⏭  unknown" in out) is unreadable
    assert ("not configured (optional)" in out) is not unreadable


@pytest.mark.parametrize(
    ("missing_config", "want_issues"),
    [((), []), (("account_id",), ["telegram: missing telegram.account_id"])],
    ids=["credentials-only", "credentials-and-config"],
)
def test_other_channel_credentials_unknown_when_unreadable(
    monkeypatch, capsys, missing_config, want_issues
):
    from kiro_crew import channels as channel_pkg
    from kiro_crew.doctor_checks import channels

    row = SimpleNamespace(
        channel_type="telegram",
        enabled=True,
        ready=False,
        missing_credentials=("TELEGRAM_BOT_TOKEN",),
        missing_config=missing_config,
    )
    monkeypatch.setattr(channel_pkg, "channel_readiness", lambda _cfg, _creds: [row])
    issues: list[str] = []

    channels._doctor_other_channels(SimpleNamespace(), {}, issues, creds_unreadable=True)

    out = capsys.readouterr().out
    assert "telegram:    credentials ⏭  unknown" in out
    assert issues == want_issues


def test_other_channel_missing_credentials_still_count_when_readable(monkeypatch, capsys):
    from kiro_crew import channels as channel_pkg
    from kiro_crew.doctor_checks import channels

    row = SimpleNamespace(
        channel_type="telegram",
        enabled=True,
        ready=False,
        missing_credentials=("TELEGRAM_BOT_TOKEN",),
        missing_config=(),
    )
    monkeypatch.setattr(channel_pkg, "channel_readiness", lambda _cfg, _creds: [row])
    issues: list[str] = []

    channels._doctor_other_channels(SimpleNamespace(), {}, issues)

    assert issues == ["telegram: missing TELEGRAM_BOT_TOKEN"]
