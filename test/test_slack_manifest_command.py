"""The Slack manifest's slash command is unique per install.

Slack resolves slash-command names across a whole Enterprise Grid org, so two
installs that both register ``/kirocrew`` collide and Slack sends the command
to the wrong app. The manifest therefore names the command after the alias, the
same way it names the app, and every reader of that name (the exfil validator,
``kirocrew manifest`` and the dashboard's manifest endpoint) must agree with the
render.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import fields
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest
import yaml
from aiohttp.test_utils import make_mocked_request

from kiro_crew import cli_setup, slack_manifest
from kiro_crew.config.sections import SlackConfig
from kiro_crew.dashboard.handlers import messaging as dashboard_messaging
from kiro_crew.security import scan_exfiltration_urls
from kiro_crew.slack import events as slack_events
from kiro_crew.slack.scope_probe import warn_unreadable_tracked_channels

_SLACK_COMMAND_RE = re.compile(r"\A/[a-z0-9_-]{1,31}\Z")


def _rendered_command(alias: str) -> str:
    parsed = yaml.safe_load(slack_manifest.render(alias))
    commands = parsed["features"]["slash_commands"]
    assert len(commands) == 1
    return str(commands[0]["command"])


class TestRenderedCommand:
    def test_the_command_is_named_after_the_alias(self) -> None:
        assert _rendered_command("jdoe") == "/kirocrew-jdoe"

    def test_two_installs_get_two_commands(self) -> None:
        assert _rendered_command("alice") != _rendered_command("bob")

    def test_the_command_is_lowercase_and_fits_slack_limit(self) -> None:
        """A mixed-case alias at ALIAS_MAX still renders a name Slack accepts."""
        alias = "Mixed_Case-" + "Z" * (slack_manifest.ALIAS_MAX - len("Mixed_Case-"))
        assert len(alias) == slack_manifest.ALIAS_MAX
        command = _rendered_command(alias)
        assert _SLACK_COMMAND_RE.match(command), command
        assert command == "/" + slack_manifest.slash_command(alias)

    def test_the_generic_alias_keeps_the_generic_command(self) -> None:
        """The dashboard's non-identifying default alias renders ``/kirocrew``.

        That path saves no ``slack.command``, so the gateway keeps listening
        for the default; a renamed command there would never be answered.
        """
        assert _rendered_command(slack_manifest.DEFAULT_ALIAS) == "/kirocrew"


class TestDeepLinkValidator:
    def test_a_mixed_case_alias_link_is_not_redacted(self) -> None:
        alias = "First-Last_" + "Q" * (slack_manifest.ALIAS_MAX - len("First-Last_"))
        assert scan_exfiltration_urls(slack_manifest.deep_link(alias)) == []

    def test_a_command_that_does_not_match_the_alias_is_redacted(self) -> None:
        payload = (
            slack_manifest.stripped_template()
            .replace(slack_manifest.COMMAND_PLACEHOLDER, "kirocrew-other")
            .replace(slack_manifest.ALIAS_PLACEHOLDER, "real")
        )
        url = "https://api.slack.com/apps?new_app=1&manifest_yaml=" + quote(payload, safe="")
        assert scan_exfiltration_urls(url) != []


class TestSetupWizardDefault:
    def test_an_unconfigured_install_keeps_the_generic_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wizard cannot know which alias the app was created with, so it
        keeps ``kirocrew``; ``kirocrew manifest`` prints the matching line."""
        target = tmp_path / "config.json"
        monkeypatch.setattr("kiro_crew.cli_setup.config_path", lambda: target)
        monkeypatch.setattr("kiro_crew.cli_setup._input_or_skip", lambda prompt: None)
        monkeypatch.setenv("USER", "JDoe")
        cli_setup._setup_slash_command()
        assert json.loads(target.read_text(encoding="utf-8"))["slack"]["command"] == "kirocrew"


def test_manifest_cli_names_the_command_to_configure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_setup._manifest(alias="jdoe", url=True)
    out = capsys.readouterr().out
    assert "/kirocrew-jdoe" in out
    assert "kirocrew config set slack.command kirocrew-jdoe" in out


def test_plain_manifest_names_the_command_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stdout stays a clean manifest; the hint still reaches the user."""
    cli_setup._manifest(alias="jdoe")
    captured = capsys.readouterr()
    assert (
        yaml.safe_load(captured.out)["features"]["slash_commands"][0]["command"] == "/kirocrew-jdoe"
    )
    assert "kirocrew config set slack.command kirocrew-jdoe" in captured.err


def _dashboard_manifest(path: str) -> dict:
    resp = asyncio.run(dashboard_messaging.api_slack_manifest(make_mocked_request("GET", path)))
    assert resp.status == 200
    return dict(json.loads(resp.text))


class TestDashboardManifest:
    """``GET /api/slack/manifest`` writes no config, so it returns the command
    its manifest registers: the ``slack.command`` value a caller that passes
    ``?alias=`` must set before the gateway answers that app."""

    def test_a_named_alias_returns_the_command_its_manifest_registers(self) -> None:
        body = _dashboard_manifest("/api/slack/manifest?alias=JDoe")
        assert body["command"] == "kirocrew-jdoe"
        commands = yaml.safe_load(body["manifest"])["features"]["slash_commands"]
        assert [c["command"] for c in commands] == ["/" + body["command"]]

    def test_the_settings_link_is_answered_by_the_default_gateway(self) -> None:
        """Settings → Slack requests no alias, and an unconfigured gateway
        answers the default ``slack.command``, so its one-click app works on
        the first try."""
        default = next(f.default for f in fields(SlackConfig) if f.name == "command")
        assert _dashboard_manifest("/api/slack/manifest")["command"] == default


async def _slash_on_an_old_install(command: str, *, owner: bool) -> list[dict]:
    """Send *command* to a gateway with no ``slack.command`` set (``/kirocrew``).

    Returns the bodies posted to the payload's ``response_url``: what the person
    who typed the command sees in Slack. Every caller sends a name that is not
    ``/kirocrew``, which must never run, so nothing may be audited as invoked.
    """
    posted: list[dict] = []
    audit = MagicMock()

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def post(self, url: str, **kwargs: dict) -> None:
            posted.append(kwargs["json"])

    orch = MagicMock()
    orch.slack_command = "kirocrew"
    orch._handler_tasks = set()
    payload = {
        "command": command,
        "text": "",
        "user_id": "U_OWNER",
        "response_url": "https://hooks.example.invalid/x",
    }
    with (
        patch("kiro_crew.slack.events.aiohttp.ClientSession", _Session),
        patch("kiro_crew.slack.events.is_allowed_user", return_value=owner),
        patch("kiro_crew.slack.events.sel", return_value=audit),
    ):
        await slack_events._handle_slash(orch, payload)
        await asyncio.gather(*list(orch._handler_tasks))
    audit.log_api_access.assert_not_called()
    return posted


class TestAnOldInstallThatReimportsTheManifest:
    """An install made before the rename answers ``/kirocrew``. The scope-probe
    hint and the setup guide's upgrade note both send it back to the bundled
    manifest, and re-importing that renames the app's command to
    ``/kirocrew-<alias>`` while ``slack.command`` stays ``kirocrew``."""

    @pytest.mark.asyncio
    async def test_the_owner_is_told_which_command_to_configure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        renamed = _rendered_command("jdoe")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.slack.events"):
            posted = await _slash_on_an_old_install(renamed, owner=True)
        fix = "kirocrew config set slack.command kirocrew-jdoe"
        assert len(posted) == 1
        assert posted[0]["response_type"] == "ephemeral"
        assert fix in posted[0]["text"]
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any(fix in message for message in warnings), warnings

    @pytest.mark.asyncio
    async def test_a_caller_who_is_not_the_owner_is_told_nothing(self) -> None:
        assert await _slash_on_an_old_install(_rendered_command("jdoe"), owner=False) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/other", "/kirocrew-x;id"])
    async def test_a_name_the_manifest_cannot_render_gets_no_reply(self, command: str) -> None:
        assert await _slash_on_an_old_install(command, owner=True) == []

    @pytest.mark.asyncio
    async def test_the_reinstall_notice_names_the_rename_before_it_happens(self) -> None:
        class _PrivateChannelOutsideTheGrant:
            async def probe_channel_history(self, channel_id: str) -> str:
                return "missing_scope"

        bodies: list[str] = []
        await warn_unreadable_tracked_channels(
            _PrivateChannelOutsideTheGrant(),
            {"C_PRIV"},
            notify=lambda kind, title, body, **kwargs: bodies.append(body),
        )
        assert len(bodies) == 1
        assert "/kirocrew-<alias>" in bodies[0]
        assert "kirocrew config set slack.command" in bodies[0]
