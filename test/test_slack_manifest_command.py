"""The Slack manifest's slash command is unique per install.

Slack resolves slash-command names across a whole Enterprise Grid org, so two
installs that both register ``/kirocrew`` collide and Slack sends the command
to the wrong app. The manifest therefore names the command after the alias, the
same way it names the app, and every reader of that name (the exfil validator
and ``kirocrew manifest``) must agree with the render.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml

from kiro_crew import cli_setup, slack_manifest
from kiro_crew.security import scan_exfiltration_urls

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
