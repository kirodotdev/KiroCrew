"""The help names ``--opt=`` as the way to clear an option, never a separate empty argument.

Windows PowerShell 5.1, and PowerShell 7 calling the ``kirocrew.cmd`` launcher, drop a
separate ``""``: ``--channel ""`` reaches argparse as a bare ``--channel`` and exits 2.
The attached form reaches it in every shell, as the empty value the handlers clear on.
"""

import argparse

import pytest

from kiro_crew import cli_parser_agent

# (register function, subcommand path, its positionals, the option whose help names a clear)
_CLEARS = [
    ("register_cron_parser", ["cron", "list"], [], "--folder"),
    ("register_cron_parser", ["cron", "update"], ["job1"], "--channel"),
    ("register_cron_parser", ["cron", "update"], ["job1"], "--agent"),
    ("register_cron_parser", ["cron", "update"], ["job1"], "--project-path"),
    ("register_cron_parser", ["cron", "update"], ["job1"], "--folder"),
    ("register_cron_parser", ["cron", "update"], ["job1"], "--model"),
    ("register_cron_parser", ["cron", "update"], ["job1"], "--timezone"),
    ("register_learn_parser", ["learn", "remove"], ["rule"], "--repo-scope"),
    ("register_agent_parser", ["agent", "update"], ["oncall"], "--display-name"),
    ("register_agent_parser", ["agent", "update"], ["oncall"], "--model"),
]


@pytest.mark.parametrize(
    ("register", "path", "positionals", "flag"),
    _CLEARS,
    ids=["-".join(path) + flag for _, path, _, flag in _CLEARS],
)
def test_the_documented_clear_is_the_attached_form(register, path, positionals, flag):
    root = argparse.ArgumentParser(prog="kirocrew")
    getattr(cli_parser_agent, register)(root.add_subparsers(dest="command"))
    parser = root
    for name in path:
        (sub,) = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        parser = sub.choices[name]
    (action,) = [a for a in parser._actions if flag in a.option_strings]
    assert f"{flag}=" in action.help
    assert '""' not in action.help
    args = root.parse_args([*path, *positionals, f"{flag}="])
    assert getattr(args, action.dest) == ""
