"""Why an allowlisted command asks: a spec's ``shell.allowedCommands`` entry never
grants a command that chains, pipes, redirects or substitutes, and the prompt says so.

The grant itself is unchanged (``test_kas_allowed_commands``): these tests pin only
the note the gate's fall-through ``allow`` carries for the person answering, and
the denyByDefault refusal's wording for the same command.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_kas_allowed_commands import _judge, _shell

from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_crew.platform import allowed_commands
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.allowed_commands import COMPOSITION_NOTICE

_PATTERN = "node task-db.js .*"


@pytest.fixture(autouse=True)
def _no_profiles(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


_COMPOSED = [
    "node task-db.js list | jq .",
    "node task-db.js list > out.json",
    "node task-db.js list < in.json",
    "node task-db.js list && echo ok",
    "node task-db.js list; echo ok",
    "node task-db.js get $(cat id.txt)",
    "node task-db.js get `cat id.txt`",
]


@pytest.mark.parametrize("command", _COMPOSED)
def test_a_composed_allowlisted_command_asks_and_says_why(command: str) -> None:
    verdict = _judge(_shell(command), [_PATTERN])
    assert verdict.action == TOOL_ALLOW
    assert verdict.reason == COMPOSITION_NOTICE


def test_an_exact_entry_matched_by_the_leading_command_also_explains() -> None:
    verdict = _judge(_shell("node task-db.js list | jq ."), ["node task-db.js list"])
    assert (verdict.action, verdict.reason) == (TOOL_ALLOW, COMPOSITION_NOTICE)


@pytest.mark.parametrize(
    "command",
    ["python other.py | jq .", "node other.js list > out.json", "make build && make test"],
)
def test_a_composed_command_no_entry_matches_carries_no_note(command: str) -> None:
    verdict = _judge(_shell(command), [_PATTERN])
    assert verdict.action == TOOL_ALLOW
    assert verdict.reason == ""


def test_a_plain_command_no_entry_matches_carries_no_note() -> None:
    verdict = _judge(_shell("python other.py list"), [_PATTERN])
    assert (verdict.action, verdict.reason) == (TOOL_ALLOW, "")


def test_a_plain_allowlisted_command_still_runs_without_a_prompt() -> None:
    verdict = _judge(_shell("node task-db.js list"), [_PATTERN])
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")
    assert verdict.reason == ""


def test_a_composed_command_without_any_allowlist_carries_no_note() -> None:
    verdict = _judge(_shell("node task-db.js list | jq ."), [])
    assert (verdict.action, verdict.reason) == (TOOL_ALLOW, "")


@pytest.mark.parametrize("command", _COMPOSED)
def test_the_note_never_grants(command: str) -> None:
    assert allowed_commands.composition_blocks_match((_PATTERN,), command)
    assert not allowed_commands.plain_command(command)


def test_deny_by_default_names_composition_instead_of_no_match() -> None:
    event = _shell("node task-db.js list | jq .")
    event.spec_shell_allowlist = (_PATTERN,)
    verdict = _judge(event, [_PATTERN])
    assert verdict.action == TOOL_DENY
    assert "never match" in verdict.reason
    assert "matches none" not in verdict.reason


def test_deny_by_default_keeps_its_message_for_an_unlisted_command() -> None:
    event = _shell("python other.py | jq .")
    event.spec_shell_allowlist = (_PATTERN,)
    verdict = _judge(event, [_PATTERN])
    assert verdict.action == TOOL_DENY
    assert "matches none of its allowedCommands" in verdict.reason
