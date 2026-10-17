"""``.env`` credential readers resolve ``secret://`` references from the vault.

Covers the shared resolver in the config loader, ``load_credentials``, the
dashboard channel writer that stores new channel tokens in the vault, the Teams
saver's direct environment read, and the CLI Slack wizard's token check. Every
token here is an obvious placeholder.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import kiro_crew.config.loader as loader
import kiro_crew.dashboard.handlers.messaging as mod
from kiro_crew.secrets import SecretVault

_PLACEHOLDER = "placeholder-channel-token"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A temp data home: ``.env`` and ``.vault`` live in *tmp_path*.

    Every credential key is set then removed through *monkeypatch*, so whatever
    a save or ``load_credentials`` writes to os.environ is undone at teardown.
    """
    monkeypatch.setattr(loader, "env_path", lambda: tmp_path / ".env")
    for key in loader.CREDENTIAL_KEYS:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    loader._warned_unresolved_env_refs.clear()
    yield tmp_path
    loader._warned_unresolved_env_refs.clear()


def _store(home: Path, name: str, value: str) -> None:
    SecretVault(home).set_sync(name, value)


def _vault_value(home: Path, name: str) -> str | None:
    stored = SecretVault(home).get(name)
    return stored.reveal() if stored is not None else None


def _load() -> dict[str, str]:
    return loader.KiroCrewConfig().load_credentials()


# ── The shared resolver ────────────────────────────────────────────────────


def test_a_reference_resolves_and_a_plaintext_passes_through(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _store(home, "TELEGRAM_BOT_TOKEN", _PLACEHOLDER)
    with caplog.at_level(logging.WARNING, logger=loader.logger.name):
        resolved = loader.resolve_env_credentials(
            {
                "TELEGRAM_BOT_TOKEN": "secret://TELEGRAM_BOT_TOKEN",
                "DISCORD_BOT_TOKEN": "plaintext-placeholder",
                "HTTP_PROXY": "secret://TELEGRAM_BOT_TOKEN",
            },
            home=home,
        )
    assert caplog.text == ""
    assert resolved["TELEGRAM_BOT_TOKEN"] == _PLACEHOLDER
    assert resolved["DISCORD_BOT_TOKEN"] == "plaintext-placeholder"
    # Only credential keys resolve: an operator setting keeps its literal value.
    assert resolved["HTTP_PROXY"] == "secret://TELEGRAM_BOT_TOKEN"


def test_a_reference_may_name_any_vault_secret(home: Path) -> None:
    _store(home, "my-slack-bot", _PLACEHOLDER)
    resolved = loader.resolve_env_credentials(
        {"SLACK_BOT_TOKEN": "secret://my-slack-bot"}, home=home
    )
    assert resolved["SLACK_BOT_TOKEN"] == _PLACEHOLDER


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("secret://NOT_STORED", "the vault holds no secret by that name"),
        ("secret://", "malformed secret:// reference"),
        ("secret:// padded ", "malformed secret:// reference"),
    ],
)
def test_an_unresolvable_reference_fails_closed_without_echoing(
    home: Path, caplog: pytest.LogCaptureFixture, value: str, reason: str
) -> None:
    _store(home, "SOMETHING_ELSE", _PLACEHOLDER)
    with caplog.at_level(logging.WARNING, logger=loader.logger.name):
        resolved = loader.resolve_env_credentials({"WEBEX_BOT_TOKEN": value}, home=home)
    # Unset, never the literal reference.
    assert "WEBEX_BOT_TOKEN" not in resolved
    assert "WEBEX_BOT_TOKEN" in caplog.text
    assert reason in caplog.text
    assert "NOT_STORED" not in caplog.text
    assert _PLACEHOLDER not in caplog.text


def test_an_unreadable_vault_fails_closed(home: Path, caplog: pytest.LogCaptureFixture) -> None:
    _store(home, "SLACK_APP_TOKEN", _PLACEHOLDER)
    (home / ".vault" / "secrets.enc").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=loader.logger.name):
        resolved = loader.resolve_env_credentials(
            {"SLACK_APP_TOKEN": "secret://SLACK_APP_TOKEN"}, home=home
        )
    assert "SLACK_APP_TOKEN" not in resolved
    assert "the secrets vault could not be read" in caplog.text
    assert _PLACEHOLDER not in caplog.text


def test_an_unresolvable_reference_logs_once_until_it_resolves(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    refs = {"FEISHU_APP_SECRET": "secret://FEISHU_APP_SECRET"}
    with caplog.at_level(logging.WARNING, logger=loader.logger.name):
        loader.resolve_env_credentials(refs, home=home)
        loader.resolve_env_credentials(refs, home=home)
        assert caplog.text.count("FEISHU_APP_SECRET") == 1
        _store(home, "FEISHU_APP_SECRET", _PLACEHOLDER)
        assert loader.resolve_env_credentials(refs, home=home)["FEISHU_APP_SECRET"]
        (home / ".vault" / "secrets.enc").unlink()
        loader.resolve_env_credentials(refs, home=home)
    assert caplog.text.count("FEISHU_APP_SECRET") == 2


def test_the_single_value_form(home: Path) -> None:
    _store(home, "MICROSOFT_APP_PASSWORD", _PLACEHOLDER)
    ref = "secret://MICROSOFT_APP_PASSWORD"
    assert loader.resolve_env_credential("MICROSOFT_APP_PASSWORD", ref) == _PLACEHOLDER
    assert loader.resolve_env_credential("MICROSOFT_APP_PASSWORD", "plain") == "plain"
    assert loader.resolve_env_credential("MICROSOFT_APP_PASSWORD", "secret://MISSING") == ""


# ── load_credentials ───────────────────────────────────────────────────────


def test_load_credentials_resolves_a_reference_and_propagates_the_reference(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / ".env").write_text(
        "SLACK_BOT_TOKEN=secret://SLACK_BOT_TOKEN\nDISCORD_BOT_TOKEN=plaintext-placeholder\n",
        encoding="utf-8",
    )
    _store(home, "SLACK_BOT_TOKEN", _PLACEHOLDER)

    creds = _load()

    assert creds["SLACK_BOT_TOKEN"] == _PLACEHOLDER
    assert creds["DISCORD_BOT_TOKEN"] == "plaintext-placeholder"
    # A child inherits the line as written: the reference, not the secret.
    assert os.environ["SLACK_BOT_TOKEN"] == "secret://SLACK_BOT_TOKEN"
    assert os.environ["DISCORD_BOT_TOKEN"] == "plaintext-placeholder"


def test_load_credentials_resolves_a_reference_from_the_environment(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store(home, "WECOM_SECRET", _PLACEHOLDER)
    monkeypatch.setenv("WECOM_SECRET", "secret://WECOM_SECRET")
    assert loader.KiroCrewConfig().load_credentials(propagate=False)["WECOM_SECRET"] == (
        _PLACEHOLDER
    )


def test_load_credentials_leaves_an_unresolvable_reference_unset(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / ".env").write_text("WEIXIN_TOKEN=secret://WEIXIN_TOKEN\n", encoding="utf-8")
    creds = _load()
    assert "WEIXIN_TOKEN" not in creds


def test_load_credentials_plaintext_env_is_unchanged(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=plaintext-placeholder\nKIROCREW_OWNER_ID=U0123ABC456\n",
        encoding="utf-8",
    )
    creds = _load()
    assert creds["TELEGRAM_BOT_TOKEN"] == "plaintext-placeholder"
    assert creds["KIROCREW_OWNER_ID"] == "U0123ABC456"
    assert not (home / ".vault" / "secrets.enc").exists()


# ── The dashboard channel writer ───────────────────────────────────────────


def test_a_new_channel_token_lands_in_the_vault_as_a_reference(home: Path) -> None:
    (home / ".env").write_text("# keep\nTELEGRAM_BOT_TOKEN=old-plaintext\n", encoding="utf-8")

    mod._write_env_updates({"TELEGRAM_BOT_TOKEN": _PLACEHOLDER, "KIROCREW_OWNER_ID": "U01"})

    text = (home / ".env").read_text(encoding="utf-8")
    assert text == "# keep\nTELEGRAM_BOT_TOKEN=secret://TELEGRAM_BOT_TOKEN\nKIROCREW_OWNER_ID=U01\n"
    assert _PLACEHOLDER not in text
    assert _vault_value(home, "TELEGRAM_BOT_TOKEN") == _PLACEHOLDER


def test_a_kept_reference_is_written_back_without_touching_the_vault(home: Path) -> None:
    (home / ".env").write_text("SLACK_APP_TOKEN=secret://custom-name\n", encoding="utf-8")
    _store(home, "custom-name", _PLACEHOLDER)

    mod._write_env_updates({"SLACK_APP_TOKEN": "secret://custom-name"})

    assert (home / ".env").read_text(encoding="utf-8") == "SLACK_APP_TOKEN=secret://custom-name\n"
    assert SecretVault(home).list_names() == ["custom-name"]


def test_a_failed_env_write_puts_the_vault_back(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / ".env").write_text("DISCORD_BOT_TOKEN=secret://DISCORD_BOT_TOKEN\n", encoding="utf-8")
    _store(home, "DISCORD_BOT_TOKEN", "previous-placeholder")

    def _boom(ep: Path, updates: dict) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(mod, "_write_env_updates_locked", _boom)
    with pytest.raises(OSError, match="disk full"):
        mod._write_env_updates({"DISCORD_BOT_TOKEN": _PLACEHOLDER, "WEBEX_BOT_TOKEN": "new-wx"})

    assert _vault_value(home, "DISCORD_BOT_TOKEN") == "previous-placeholder"
    assert _vault_value(home, "WEBEX_BOT_TOKEN") is None


def test_a_vault_that_cannot_be_written_refuses_the_save(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / ".env").write_text("FEISHU_APP_SECRET=old-plaintext\n", encoding="utf-8")

    def _refuse(self: SecretVault, name: str, value: str) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(SecretVault, "_replace_entry_sync", _refuse)
    with pytest.raises(OSError) as excinfo:
        mod._write_env_updates({"FEISHU_APP_SECRET": _PLACEHOLDER})

    assert _PLACEHOLDER not in str(excinfo.value)
    # Nothing written: the plaintext never reaches .env.
    assert (home / ".env").read_text(encoding="utf-8") == "FEISHU_APP_SECRET=old-plaintext\n"


def test_a_rollback_keeps_a_value_another_writer_stored_meanwhile(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Secrets panel does not take the .env lock: its write must survive."""
    (home / ".env").write_text("SLACK_BOT_TOKEN=secret://SLACK_BOT_TOKEN\n", encoding="utf-8")
    _store(home, "SLACK_BOT_TOKEN", "previous-placeholder")

    def _concurrent_write_then_fail(ep: Path, updates: dict) -> None:
        SecretVault(home).set_sync("SLACK_BOT_TOKEN", "concurrent-placeholder")
        raise OSError("sharing violation")

    monkeypatch.setattr(mod, "_write_env_updates_locked", _concurrent_write_then_fail)
    with pytest.raises(OSError, match="sharing violation"):
        mod._write_env_updates({"SLACK_BOT_TOKEN": _PLACEHOLDER})

    assert _vault_value(home, "SLACK_BOT_TOKEN") == "concurrent-placeholder"


def test_a_clear_keeps_a_value_another_writer_stored_meanwhile(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / ".env").write_text("TELEGRAM_BOT_TOKEN=secret://TELEGRAM_BOT_TOKEN\n", encoding="utf-8")
    _store(home, "TELEGRAM_BOT_TOKEN", _PLACEHOLDER)
    real_write = mod._write_env_updates_locked

    def _concurrent_write_then_land(ep: Path, updates: dict) -> None:
        SecretVault(home).set_sync("TELEGRAM_BOT_TOKEN", "concurrent-placeholder")
        real_write(ep, updates)

    monkeypatch.setattr(mod, "_write_env_updates_locked", _concurrent_write_then_land)
    mod._write_env_updates({"TELEGRAM_BOT_TOKEN": None})

    assert (home / ".env").read_text(encoding="utf-8") == ""
    assert _vault_value(home, "TELEGRAM_BOT_TOKEN") == "concurrent-placeholder"


@pytest.mark.parametrize("channel", ["slack", "discord", "telegram", "webex", "teams", "wecom"])
def test_the_channel_settings_get_reads_credentials_off_the_loop(
    channel: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving a reference decrypts the vault, which must not run on the loop."""
    import threading

    from aiohttp.test_utils import make_mocked_request

    monkeypatch.setattr(loader, "config_path", lambda: home / "config.json")
    threads: list[int] = []

    class _Stop(Exception):
        pass

    def _record(self: loader.KiroCrewConfig, **_kw: object) -> dict[str, str]:
        threads.append(threading.get_ident())
        raise _Stop

    monkeypatch.setattr(loader.KiroCrewConfig, "load_credentials", _record)
    handler = getattr(mod, f"api_{channel}_config_get")

    async def _run() -> int:
        loop_thread = threading.get_ident()
        with pytest.raises(_Stop):
            await handler(make_mocked_request("GET", f"/api/{channel}/config"))
        return loop_thread

    loop_thread = asyncio.run(_run())
    assert threads and loop_thread not in threads


def test_clearing_a_token_removes_only_its_own_vault_entry(home: Path) -> None:
    (home / ".env").write_text(
        "SLACK_BOT_TOKEN=secret://SLACK_BOT_TOKEN\nSLACK_APP_TOKEN=secret://shared-app\n",
        encoding="utf-8",
    )
    _store(home, "SLACK_BOT_TOKEN", _PLACEHOLDER)
    _store(home, "shared-app", "shared-placeholder")
    _store(home, "SLACK_APP_TOKEN", "unrelated-placeholder")

    mod._write_env_updates({"SLACK_BOT_TOKEN": None, "SLACK_APP_TOKEN": None})

    assert (home / ".env").read_text(encoding="utf-8") == ""
    assert _vault_value(home, "SLACK_BOT_TOKEN") is None
    # The app token's line pointed elsewhere: neither entry is this save's to drop.
    assert _vault_value(home, "shared-app") == "shared-placeholder"
    assert _vault_value(home, "SLACK_APP_TOKEN") == "unrelated-placeholder"


def test_a_landed_save_puts_the_reference_in_the_environment(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBEX_BOT_TOKEN", "old-plaintext")
    monkeypatch.setenv("KIROCREW_OWNER_ID", "U-old")

    asyncio.run(
        mod._write_env_or_roll_back(
            {"WEBEX_BOT_TOKEN": _PLACEHOLDER, "KIROCREW_OWNER_ID": "U01"}, None
        )
    )

    assert os.environ["WEBEX_BOT_TOKEN"] == "secret://WEBEX_BOT_TOKEN"
    assert os.environ["KIROCREW_OWNER_ID"] == "U01"
    assert loader.KiroCrewConfig().load_credentials(propagate=False)["WEBEX_BOT_TOKEN"] == (
        _PLACEHOLDER
    )


# ── Readers outside load_credentials ───────────────────────────────────────


def test_the_teams_saver_verifies_with_the_resolved_password(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A metadata-only Teams save re-verifies the stored pair with the vault secret."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    cfg = home / "config.json"
    cfg.write_text('{"teams": {"enabled": true, "app_id": "app-1"}}', encoding="utf-8")
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    (home / ".env").write_text(
        "MICROSOFT_APP_ID=app-1\nMICROSOFT_APP_PASSWORD=secret://MICROSOFT_APP_PASSWORD\n",
        encoding="utf-8",
    )
    _store(home, "MICROSOFT_APP_PASSWORD", _PLACEHOLDER)
    monkeypatch.setenv("MICROSOFT_APP_PASSWORD", "secret://MICROSOFT_APP_PASSWORD")
    seen: list[str] = []

    async def _verify(app_id: str, password: str, tenant: str) -> None:
        seen.append(password)
        return None

    monkeypatch.setattr(mod, "_validate_teams_app_credentials", _verify)

    async def _run() -> int:
        app = web.Application()
        app.router.add_put("/api/teams/config", mod.api_teams_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/teams/config", json={"tenant_id": "tenant-2"})
            return resp.status

    assert asyncio.run(_run()) == 200
    assert seen == [_PLACEHOLDER]
    assert "MICROSOFT_APP_PASSWORD=secret://MICROSOFT_APP_PASSWORD" in (home / ".env").read_text(
        encoding="utf-8"
    )


def test_the_slack_wizard_checks_a_kept_reference_as_its_secret(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.cli_setup as cli_setup

    _store(home, "SLACK_BOT_TOKEN", _PLACEHOLDER)
    tokens: list[str] = []

    class _Client:
        def __init__(self, token: str, timeout: float) -> None:
            tokens.append(token)

        def auth_test(self) -> dict:
            return {"team": "Example"}

    monkeypatch.setattr(cli_setup, "WebClient", _Client)

    assert cli_setup._verify_slack_secret("SLACK_BOT_TOKEN", "secret://SLACK_BOT_TOKEN") == (
        True,
        "Example",
    )
    assert tokens == [_PLACEHOLDER]

    verdict, detail = cli_setup._verify_slack_secret("SLACK_BOT_TOKEN", "secret://MISSING")
    assert verdict is False and "MISSING" not in detail
    assert tokens == [_PLACEHOLDER]  # an unresolvable reference never reaches Slack


def test_the_gateway_takes_boot_credentials_resolved_off_the_loop() -> None:
    """``run_gateway`` resolves credentials in a worker and hands them to the orchestrator."""
    import inspect
    from unittest.mock import MagicMock

    from kiro_crew.slack import gateway as gw

    cfg = MagicMock()
    cfg.load_credentials.side_effect = AssertionError("credentials were passed in")
    cfg.slack.allowed_users = []
    cfg.slack.tracking_channels = []
    cfg.slack.command = "kirocrew"
    orch = gw.GatewayOrchestrator(
        cfg, credentials={"SLACK_APP_TOKEN": "xapp-placeholder", "SLACK_BOT_TOKEN": "xoxb-ph"}
    )
    assert orch._app_token == "xapp-placeholder"
    assert orch._bot_token == "xoxb-ph"
    source = inspect.getsource(gw.run_gateway)
    assert "await asyncio.to_thread(cfg.load_credentials)" in source
    assert "credentials=credentials" in source


def test_an_unresolvable_teams_reference_never_purges_the_legacy_password(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy config copy is the only password while the reference does not resolve."""
    import json

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    cfg = home / "config.json"
    cfg.write_text(
        '{"teams": {"enabled": true, "app_id": "app-1", "app_password": "legacy-placeholder"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    monkeypatch.setenv("MICROSOFT_APP_PASSWORD", "secret://NOT_STORED")
    seen: list[str] = []

    async def _verify(app_id: str, password: str, tenant: str) -> None:
        seen.append(password)
        return None

    monkeypatch.setattr(mod, "_validate_teams_app_credentials", _verify)

    async def _run() -> int:
        app = web.Application()
        app.router.add_put("/api/teams/config", mod.api_teams_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/teams/config", json={"app_id": "app-2"})
            return resp.status

    assert asyncio.run(_run()) == 200
    assert seen == ["legacy-placeholder"]
    teams = json.loads(cfg.read_text(encoding="utf-8"))["teams"]
    assert teams["app_password"] == "legacy-placeholder"
    assert teams["app_id"] == "app-2"


def test_concurrent_reads_share_the_warning_state_safely(home: Path) -> None:
    """Settings reads resolve on worker threads; the once-per-key log state is shared."""
    import threading

    refs = {f"JIRA_TOKEN_{i:02X}": f"secret://JIRA_TOKEN_{i:02X}" for i in range(16)}
    for i, key in enumerate(refs):
        if i % 2:
            _store(home, key, _PLACEHOLDER)
    errors: list[BaseException] = []
    # A finite timeout releases the waiting readers if fewer than 8 ever arrive.
    start = threading.Barrier(8, timeout=60)

    def _reader() -> None:
        try:
            start.wait()
            for _ in range(40):
                loader.resolve_env_credentials(refs, home=home)
                loader._warn_unresolved_env_credentials({}, resolved_keys=list(refs))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=_reader, daemon=True) for _ in range(8)]
    started: list[threading.Thread] = []
    try:
        for t in threads:
            t.start()
            started.append(t)
    except BaseException:
        # A reader that never started leaves the barrier short: break it so the
        # started readers exit now instead of waiting out the timeout.
        start.abort()
        raise
    finally:
        for t in started:
            t.join(timeout=60)
    assert errors == []
    assert not any(t.is_alive() for t in threads)
