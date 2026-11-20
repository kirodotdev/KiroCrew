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
import re
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
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
    assert "restart the gateway" not in out


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
    """``GET /api/slack/manifest`` writes no config and returns the command its
    manifest registers, which the gateway answers as it is."""

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


class _Session:
    """Records the bodies posted to a payload's ``response_url``."""

    posted: list[dict]

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, **kwargs: dict) -> None:
        type(self).posted.append(kwargs["json"])


async def _slash(
    command: str, *, owner: bool, text: str = "", orch: MagicMock | None = None
) -> tuple[list[dict], MagicMock]:
    """Send *command* to a gateway whose ``slack.command`` is the default.

    Returns what the person who typed it sees in Slack (the bodies posted to
    the ``response_url``) and the SEL audit mock.
    """
    _Session.posted = []
    audit = MagicMock()
    orch = orch or MagicMock()
    orch.slack_command = "kirocrew"
    orch._owner_id = "U_OWNER"
    orch._handler_tasks = set()
    payload = {
        "command": command,
        "text": text,
        "user_id": "U_OWNER",
        "response_url": "https://hooks.example.invalid/x",
    }
    with (
        patch("kiro_crew.slack.events.aiohttp.ClientSession", _Session),
        patch("kiro_crew.slack.events.is_allowed_user", return_value=owner),
        patch("kiro_crew.slack.events.sel", return_value=audit),
        patch("kiro_crew.slack.events_slash.code_channel_slash", return_value=False),
    ):
        await slack_events._handle_slash(orch, payload)
        await asyncio.gather(*list(orch._handler_tasks))
    return _Session.posted, audit


class TestAnyManifestCommandIsAnswered:
    """Slack delivers only commands this app registered, so ``/kirocrew-<alias>``
    is answered while ``slack.command`` is still ``kirocrew``: no config set,
    no restart."""

    @pytest.mark.asyncio
    async def test_an_alias_command_runs_and_its_help_names_it(self) -> None:
        posted, audit = await _slash("/kirocrew-alice", owner=True, text="nonsense")
        assert len(posted) == 1
        assert "/kirocrew-alice" in posted[0]["text"]
        assert "config set" not in posted[0]["text"]
        outcomes = [c.kwargs["outcome"] for c in audit.log_api_access.call_args_list]
        assert outcomes == ["allowed"]

    @pytest.mark.asyncio
    async def test_a_sub_command_usage_reply_names_the_typed_command(self) -> None:
        """Usage strings read ``orch.slack_command``; the owner's typed name is
        adopted first, so they never point at an unregistered ``/kirocrew``."""
        orch = MagicMock()
        posted, _ = await _slash("/kirocrew-alice", owner=True, text="dashboard bad", orch=orch)
        assert [p["text"] for p in posted] == ["Usage: `/kirocrew-alice dashboard [<N>h|<N>m]`"]
        assert orch.slack_command == "kirocrew-alice"

    @pytest.mark.asyncio
    async def test_a_custom_command_still_runs_after_an_alias_was_adopted(self) -> None:
        """An app may register a custom ``slack.command`` beside its alias
        command; adopting the alias as the shown name must not drop the custom
        one, so the router also matches the live configured value."""
        orch = MagicMock()
        orch._cfg = SimpleNamespace(slack=SimpleNamespace(command="crew"))
        orch.slack_command = "crew"
        await _slash("/kirocrew-alice", owner=True, text="dashboard bad", orch=orch)
        assert orch.slack_command == "kirocrew-alice"
        posted, _ = await _slash("/crew", owner=True, text="dashboard bad", orch=orch)
        assert [p["text"] for p in posted] == ["Usage: `/crew dashboard [<N>h|<N>m]`"]

    @pytest.mark.asyncio
    async def test_a_caller_who_is_not_the_owner_is_refused(self) -> None:
        orch = MagicMock()
        posted, audit = await _slash(_rendered_command("jdoe"), owner=False, orch=orch)
        assert [p["text"] for p in posted] == ["⛔ You are not authorized to use this command."]
        assert audit.log_api_access.call_args.kwargs["outcome"] == "denied"
        assert orch.slack_command == "kirocrew"  # a refused caller renames nothing

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/other", "/kirocrew-x;id", "/kirocrewx"])
    async def test_a_name_the_manifest_cannot_render_is_dropped(self, command: str) -> None:
        posted, audit = await _slash(command, owner=True)
        assert posted == []
        audit.log_api_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_reinstall_notice_keeps_the_name_without_a_restart(self) -> None:
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
        assert "then restart" not in bodies[0]
