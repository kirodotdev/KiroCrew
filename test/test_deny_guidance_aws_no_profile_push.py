"""No AWS advice a session gets pushes it onto a named profile.

The refusal text, its suggested commands, the shipped prompt, the skill, the user
doc, the setting's help and the ``[AWS ACCESS]`` note once told an agent to list
profiles, pass ``--profile`` and treat a ``credential_process`` profile as the
setup to have. Where the owner's AWS files are hidden from the chat none of that
can work, and the agent kept retrying it.

The three words may still appear, but only inside the lines that say when each one
applies: the one case for a profile flag, what each sandbox tier shows, and what a
failed lookup means. Those lines live once, in :mod:`kiro_crew.deny_guidance`, and
every surface quotes them.

Left out on purpose: ``credential_tool_hint`` and the skill's section on a host
that vends credentials through an MCP tool. Both are advice about a tool an
outside sandbox provides, and they stay as they are until Kiro Crew has a route of
its own to replace them. ``docs/configuration.md`` is the owner's reference for a
setting, not advice to a session; the setting's help text is checked here.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from kiro_crew import deny_guidance as dg
from kiro_crew import sandbox_tiers
from kiro_crew.agent_sdk.tool_gate import ADAPTER_EXPOSED_CREDENTIAL_LEAVES
from kiro_crew.config.integration_sections import AwsConfig

_REPO = Path(__file__).resolve().parents[1]
_ROOT = _REPO / "src" / "kiro_crew"

_PUSH_WORDS = ("--profile", "list-profiles", "credential_process")


def _shared_lines() -> tuple[str, ...]:
    """The lines that may hold one of the words, on every surface."""
    return (dg.AWS_PROFILE_LINE, dg.AWS_TIER_LINE, dg.AWS_HIDDEN_LINE)


def _note():
    """The ``[AWS ACCESS]`` note's module, imported when a test runs."""
    from kiro_crew.context_assembly import aws_access

    return aws_access


#: The refusal's last sentence, about a host's own credential-vending tool. It
#: tells the agent NOT to pass the flag.
_VENDING_TOOL_SENTENCE = "there, run the command plainly and do not pass `--profile`."

#: The skill's section on that same tool, from its heading to the next one.
_SKILL_VENDING_HEADING = "#### If this host vends credentials through an MCP tool"
_SKILL_NEXT_HEADING = "#### If the credential tool appears to be missing entirely"

#: What ``kirocrew doctor`` reports, in the skill and in the user doc. A fact
#: about the report, not a setup to adopt.
_DOCTOR_LINES = (
    "whether a `credential_process` is in play",
    "whether the profile in use has a `credential_process` entry",
)

#: The user doc's table says per tier what the shared tier line says in one run.
_DOC_TABLE_CELL = "The supported route is a `credential_process` in that file."


def _flat(text: str) -> str:
    """*text* on one line, so a wrapped paragraph compares with a constant."""
    return " ".join(text.split())


def _skill() -> str:
    text = (_ROOT / "builtin_skills" / "blocked-by-policy" / "SKILL.md").read_text(encoding="utf-8")
    start = text.index(_SKILL_VENDING_HEADING)
    end = text.index(_SKILL_NEXT_HEADING)
    assert start < end
    return text[:start] + text[end:]


def _setting(path: str) -> dict:
    """The committed schema snapshot's entry for *path* (pinned to the code elsewhere)."""
    entries = json.loads((_REPO / "config-baseline.json").read_text(encoding="utf-8"))["entries"]
    (entry,) = [e for e in entries if e.get("path") == path]
    return entry


def _surfaces() -> dict[str, tuple[str, tuple[str, ...]]]:
    """Each surface's text, with the extra lines only that surface may keep."""
    prompt = (_ROOT / "config" / "prompt.md").read_text(encoding="utf-8")
    doc = (_ROOT / "docs" / "blocked-commands.md").read_text(encoding="utf-8")
    return {
        "REMEDIATION[aws_credential]": (
            dg.REMEDIATION[dg.DENY_CLASS_AWS_CREDENTIAL],
            (_VENDING_TOOL_SENTENCE,),
        ),
        "REMEDIATION[secret_file]": (dg.REMEDIATION[dg.DENY_CLASS_SECRET_FILE], ()),
        "SUGGESTED_COMMANDS": (
            " ".join(c for commands in dg.SUGGESTED_COMMANDS.values() for c in commands),
            (),
        ),
        "config/prompt.md": (prompt, ()),
        "blocked-by-policy/SKILL.md": (_skill(), _DOCTOR_LINES),
        "docs/blocked-commands.md": (doc, (*_DOCTOR_LINES, _DOC_TABLE_CELL)),
        "aws.project_profiles help": (_setting("aws.project_profiles")["help"], ()),
        "[AWS ACCESS] note": (_note().note_text(bound_folders=True), ()),
        "[AWS ACCESS] Bot line": (_note().bot_note(), ()),
        "[AWS ACCESS] sub-agent line": (_note().subagent_line(), ()),
    }


#: Spelled out, so collecting this file reads no surface.
_SURFACE_LABELS = (
    "REMEDIATION[aws_credential]",
    "REMEDIATION[secret_file]",
    "SUGGESTED_COMMANDS",
    "[AWS ACCESS] Bot line",
    "[AWS ACCESS] note",
    "[AWS ACCESS] sub-agent line",
    "aws.project_profiles help",
    "blocked-by-policy/SKILL.md",
    "config/prompt.md",
    "docs/blocked-commands.md",
)


def test_every_surface_is_checked():
    assert sorted(_surfaces()) == sorted(_SURFACE_LABELS)


@pytest.mark.parametrize("label", _SURFACE_LABELS)
def test_no_surface_pushes_a_named_profile(label):
    text, extra = _surfaces()[label]
    rest = _flat(text)
    for line in (*_shared_lines(), *extra):
        rest = rest.replace(_flat(line), " ")
    for word in _PUSH_WORDS:
        assert word not in rest, (
            f"{label} holds {word!r} outside the lines that say when it applies: "
            f"...{rest[max(0, rest.index(word) - 80) : rest.index(word) + 80]}..."
        )


def test_the_shared_lines_reach_the_refusal_the_skill_and_the_note():
    """A surface that dropped the lines would pass the test above by saying nothing."""
    for label in (
        "REMEDIATION[aws_credential]",
        "blocked-by-policy/SKILL.md",
        "[AWS ACCESS] note",
    ):
        flat = _flat(_surfaces()[label][0])
        for line in _shared_lines():
            assert line in flat, f"{label} lost: {line[:60]}"


def test_the_tier_line_says_what_the_tier_lists_do():
    """The line states facts about the sandbox, so it is held to the lists it reads."""
    line = dg.AWS_TIER_LINE
    assert ".aws" not in sandbox_tiers._STANDARD_DIRS
    assert "On `standard`, the default tier, `~/.aws` is visible" in line
    assert sandbox_tiers._SANDBOX_MODE_ALIASES["auto"] == "standard"
    assert ".aws" in sandbox_tiers._STRICT_DIRS
    assert "On `strict` `~/.aws` is hidden" in line
    assert ".aws" in sandbox_tiers._CC_DIRS
    assert sandbox_tiers._CC_EXPOSE_FILES == [".aws/config"]
    assert "On `cc` only `~/.aws/config` is shown" in line
    # A chat reads its own tier from the variable the launcher sets.
    assert f"`{sandbox_tiers._IN_SANDBOX_LEVEL_VAR}`" in line
    # ``cc`` is on the governance scale and is not a value of the owner's setting.
    assert sorted(_setting("agent.sandbox")["enumValues"]) == ["auto", "off", "strict"]
    assert "`cc` only from such a floor" in line
    assert ADAPTER_EXPOSED_CREDENTIAL_LEAVES == {"codex": (".aws/config",)}
    assert "on `codex` it shows `~/.aws/config` only" in line


def test_the_profile_setting_says_it_sets_a_name_not_a_credential():
    """The help once said a bound folder "reaches the intended account"."""
    (field,) = [f for f in dataclasses.fields(AwsConfig) if f.name == "project_profiles"]
    text = field.metadata["help"]

    assert "sets a profile NAME, not a credential" in text
    assert "could not be found" in text
    assert "intended account" not in text
    assert text == _setting("aws.project_profiles")["help"]


def test_no_added_line_names_a_route_that_is_not_built():
    """Nothing here hands a chat credentials yet, so no line may say that it does."""
    for line in (
        *_shared_lines(),
        _note().note_text(bound_folders=True),
        _note().bot_note(),
        _note().subagent_line(),
    ):
        lowered = line.lower()
        for word in ("vend", "mcp", "ask for access", "tool list"):
            assert word not in lowered, f"{word!r} in: {line[:60]}"
