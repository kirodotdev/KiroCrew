"""Tests for the grouped top-level ``kirocrew`` help.

The listing a user reads is rendered from ``cli_help.COMMAND_GROUPS``, not from
argparse's own subcommand block, so the risk this file exists to catch is a
command that is registered but never listed (or listed but not registered).
"""

import re
import sys

import pytest

from kiro_crew import cli_help


def _capture_cli(monkeypatch, tmp_path, capsys, argv):
    """Run ``kirocrew <argv>`` far enough to render help, and return (out, err).

    ``KIROCREW_PROJECT_DIR`` is pinned so ``main()``'s project auto-detection
    does not walk (and then export) the checkout it happens to run in.
    """
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["kirocrew", *argv])
    from kiro_crew.cli import main

    with pytest.raises(SystemExit):
        main()
    captured = capsys.readouterr()
    return captured.out, captured.err


def _offered_commands(monkeypatch, tmp_path, capsys) -> list[str]:
    """The command names argparse offers after an unknown one, in order.

    This is deliberately the error message rather than an attribute of the
    subparsers action: the message is what a user reads, and argparse's
    ``_choices_actions`` is not a public API.
    """
    _out, err = _capture_cli(monkeypatch, tmp_path, capsys, ["definitely-not-a-command"])
    match = re.search(r"choose from ([^)]+)\)", err)
    assert match, f"no invalid-choice list in stderr: {err!r}"
    return [name.strip().strip("'\"") for name in match.group(1).split(",")]


class TestGroupingCoversEveryCommand:
    def test_offered_and_grouped_sets_match(self, monkeypatch, tmp_path, capsys):
        offered = set(_offered_commands(monkeypatch, tmp_path, capsys))
        assert offered == set(cli_help.SUMMARIES), (
            "top-level help and the registered commands have drifted: "
            f"registered-but-unlisted={sorted(offered - set(cli_help.SUMMARIES))}, "
            f"listed-but-unregistered={sorted(set(cli_help.SUMMARIES) - offered)}"
        )

    def test_each_command_appears_in_exactly_one_group(self):
        seen: list[str] = [
            name for _section, commands in cli_help.COMMAND_GROUPS for name, _summary in commands
        ]
        duplicates = {name for name in seen if seen.count(name) > 1}
        assert not duplicates, f"listed in more than one group: {sorted(duplicates)}"

    def test_add_command_refuses_an_ungrouped_name(self):
        """The guard that makes the drift above impossible for new commands."""
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        with pytest.raises(KeyError):
            cli_help.add_command(sub, "not-a-real-command")


class TestInternalCommandsStayHidden:
    """``mcp-*`` are MCP servers the agent backend spawns, not commands."""

    def test_absent_from_the_listing_and_the_invalid_choice_error(
        self, monkeypatch, tmp_path, capsys
    ):
        assert not [name for name in cli_help.SUMMARIES if name.startswith("mcp-")]
        out, _err = _capture_cli(monkeypatch, tmp_path, capsys, ["--help"])
        assert "mcp-" not in out
        offered = _offered_commands(monkeypatch, tmp_path, capsys)
        assert not [name for name in offered if name.startswith("mcp-")]

    def test_still_dispatchable_despite_being_hidden(self, monkeypatch, tmp_path, capsys):
        """Hiding filters what argparse PRINTS, never what it accepts."""
        out, _err = _capture_cli(monkeypatch, tmp_path, capsys, ["mcp-core", "--help"])
        assert out.startswith("usage: kirocrew mcp-core")

    def test_error_lists_the_starting_commands_first(self, monkeypatch, tmp_path, capsys):
        """The offer is ordered like the help, not like the registration order."""
        offered = _offered_commands(monkeypatch, tmp_path, capsys)
        assert offered[:3] == ["gateway", "service", "doctor"]

    def test_choices_view_keeps_membership_complete(self):
        commands = {"gateway": object(), "mcp-core": object()}
        view = cli_help._VisibleCommandChoices(commands)
        assert "mcp-core" in view and view["mcp-core"] is commands["mcp-core"]
        assert list(view) == ["gateway"]
        # A command registered after the view is installed is still recognised.
        commands["doctor"] = object()
        assert "doctor" in view and list(view) == ["gateway", "doctor"]


class TestTopLevelHelpLayout:
    def test_start_here_leads_with_gateway_service_doctor(self, monkeypatch, tmp_path, capsys):
        out, _err = _capture_cli(monkeypatch, tmp_path, capsys, ["--help"])
        lines = out.splitlines()
        start = lines.index("Start here:")
        listed = [line.split()[0] for line in lines[start + 1 : start + 4]]
        assert listed == ["gateway", "service", "doctor"]
        # Nothing may be listed above it: the sections after it are the long tail.
        assert not any(line.endswith(":") and line[0].isupper() for line in lines[:start] if line)

    def test_help_drops_the_argparse_choice_blob_and_suppress_markers(
        self, monkeypatch, tmp_path, capsys
    ):
        out, _err = _capture_cli(monkeypatch, tmp_path, capsys, ["--help"])
        assert "==SUPPRESS==" not in out
        assert "{chat," not in out
        assert out.startswith("usage: kirocrew [-h] [--version] [-v] [--no-jail] <command>")

    def test_subcommand_usage_is_not_prefixed_with_the_top_level_usage(
        self, monkeypatch, tmp_path, capsys
    ):
        """argparse derives a subcommand's prog from the parent's ``usage=``."""
        out, _err = _capture_cli(monkeypatch, tmp_path, capsys, ["service", "--help"])
        assert out.startswith("usage: kirocrew service")

    def test_orientation_explains_both_lifetimes_and_the_default_port(
        self, monkeypatch, tmp_path, capsys
    ):
        from kiro_crew.config.loader import _DEFAULT_PORT

        out, _err = _capture_cli(monkeypatch, tmp_path, capsys, ["--help"])
        assert "kirocrew service install" in out
        assert "foreground" in out
        # The help text spells the port out; keep it honest against the binder.
        assert str(_DEFAULT_PORT) in out
        assert cli_help._DEFAULT_PORT_TEXT == str(_DEFAULT_PORT)


class TestPodApiMethodParsing:
    @pytest.mark.parametrize("spelling", ["GET", "Get", "get"])
    def test_method_is_case_insensitive_and_canonicalized(self, spelling, monkeypatch, tmp_path):
        import kiro_crew.cli_commands as cli_commands

        captured = []
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            sys,
            "argv",
            ["kirocrew", "pod", "api", "wt", spelling, "health"],
        )
        monkeypatch.setattr(cli_commands, "_pod", lambda args: captured.append(args))
        from kiro_crew.cli import main

        main()
        assert len(captured) == 1
        assert captured[0].method == "GET"

    def test_the_six_canonical_methods_are_advertised_in_help(self, monkeypatch, tmp_path, capsys):
        """The method surface is documented in help, not enforced by argparse.

        `choices=` used to reject an unknown verb here, which meant argparse
        answered with its own usage prose and exit 2 — escaping the fixed-key
        JSON envelope `pod api` promises on every exit. Validation moved to
        `pod.runtime.pod_api`, which reports through that envelope, so the
        canonical list has to remain discoverable somewhere: the argument's help.
        """
        out, err = _capture_cli(
            monkeypatch,
            tmp_path,
            capsys,
            ["pod", "api", "--help"],
        )
        rendered = out + err
        for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
            assert method in rendered, method
        # argparse must no longer be the thing that refuses a bad verb.
        assert "choose from" not in rendered
