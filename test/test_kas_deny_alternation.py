"""A ``deniedCommands`` entry with alternation reaches KAS as one deny per branch.

kiro-cli anchors ``(touch|mkdir) .*`` at both ends, so it denies ``mkdir d``; the
KAS engine splits it into ``touch *`` and ``mkdir *`` when it reads an agent
profile itself. The projection sends the same globs, since the gate cannot read
alternation and would otherwise let ``mkdir d`` run.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_permissions import shell_deny_globs, tools_settings_to_permissions
from kiro_crew.acp.kas_spec_denies import uncarried_denied_commands


def _spec(denied: list[str], **over: Any) -> dict[str, Any]:
    return {
        "name": "a",
        "prompt": "p",
        "tools": ["*"],
        "includeMcpJson": False,
        "toolsSettings": {"shell": {"deniedCommands": denied}},
        **over,
    }


def _shell_globs(denied: list[str]) -> set[str]:
    out = kas_agents.to_client_custom_agent("a", _spec(denied), "p")
    return shell_deny_globs(out.get("permissions"))


def _named(denied: list[str]) -> list[str]:
    keys = kas_agents.spec_keys_without_carrier(_spec(denied), translates_tools_settings=True)
    return [k for k in keys if "deniedCommand" in k]


@pytest.mark.parametrize(
    ("regex", "globs"),
    [
        ("(touch|mkdir) .*", {"touch *", "mkdir *"}),
        ("(rm|shred) .*", {"rm *", "shred *"}),
        ("^(rm|shred) .*$", {"rm *", "shred *"}),
        (r"\A(?:rm|shred) .*\z", {"rm *", "shred *"}),
        ("(git|hg) (push|pull).*", {"git push*", "git pull*", "hg push*", "hg pull*"}),
        ("rm(| -rf) .*", {"rm *", "rm -rf *"}),
    ],
)
def test_a_group_alternation_reaches_kas_as_one_deny_per_branch(regex: str, globs: set[str]):
    assert globs <= _shell_globs([regex])
    assert _named([regex]) == []
    settings = {"shell": {"deniedCommands": [regex]}}
    block, lost = tools_settings_to_permissions(settings)
    assert lost == []
    assert uncarried_denied_commands(settings, block) == []


@pytest.mark.parametrize(
    ("regex", "globs"),
    [
        ("touch .*", {"touch *"}),
        (r"rm \.env", {"rm .env"}),
        (r"echo \(a\|b\) .*", {"echo (a|b) *"}),
    ],
)
def test_an_entry_with_no_group_travels_as_before(regex: str, globs: set[str]):
    assert _shell_globs([regex]) == globs
    assert _named([regex]) == []


@pytest.mark.parametrize(
    "regex",
    [
        "(rm|shred)+ .*",  # a repeated group
        "(a(rm|shred)) .*",  # a group holding more than its inner group
        "(?i:rm|shred) .*",  # a flag group
        "([r]m|shred) .*",  # a class inside the group
        "(rm|shred .*",  # never closed
    ],
)
def test_an_alternation_the_split_cannot_read_exactly_is_still_named(regex: str):
    assert _shell_globs([regex]) == set()
    assert len(_named([regex])) == 1


def test_a_class_deny_stays_held_by_the_gate():
    settings = {"shell": {"deniedCommands": ["[rm]m .*"]}}
    block, _ = tools_settings_to_permissions(settings)
    assert uncarried_denied_commands(settings, block) == ["[rm]m .*"]


def test_a_command_no_branch_names_is_not_denied():
    globs = _shell_globs(["(touch|mkdir) .*"])
    assert not any(g.split()[0] in {"ls", "rm", "cat"} for g in globs)


def test_the_split_is_bounded():
    from kiro_crew.acp.kas_deny_alternatives import MAX_ALTERNATIVES, deny_alternatives

    wide = "(a|b)" * 7  # 128 combinations
    assert 2**7 > MAX_ALTERNATIVES
    assert deny_alternatives(wide) == [wide]
    assert len(deny_alternatives("(a|b)" * 6)) == 64


def test_a_split_deny_lifts_no_shell_grant():
    """The deny now travels, but Crew still cannot read it, so a shell grant beside
    it stays at the prompt exactly as before: nothing new runs unasked."""
    from kiro_crew.acp.kas_permissions import CREW_SPEC_DENIES_KEY

    out = kas_agents.to_client_custom_agent(
        "a", _spec(["(rm|shred) .*"], allowedTools=["shell"]), "p"
    )
    assert "execute_bash" not in out.get("_crewAnsweredBuiltins", ())
    assert CREW_SPEC_DENIES_KEY not in out


def test_a_bot_worker_holds_its_shell_only_for_an_unsplit_deny():
    """The worker's hold is for an entry nobody enforces; a split one KAS enforces."""
    from kiro_crew import mate_spec_policy

    spec = {"toolsSettings": {"shell": {"deniedCommands": ["(rm|shred) .*", "a.*|b.*"]}}}
    assert mate_spec_policy.unjudged_denies(spec) == ["a.*|b.*"]


# ── a top-level alternation ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("regex", "globs"),
    [
        # kiro-cli's own upgrade writes `deny shell ["rm *", "shred *"]` for this one.
        ("rm .*|shred .*", {"rm *", "shred *"}),
        ("^rm .*|shred .*$", {"rm *", "shred *"}),
        (r"\Arm .*|shred .*\z", {"rm *", "shred *"}),
        ("rm .*|(touch|mkdir) .*", {"rm *", "touch *", "mkdir *"}),
        ("(?:rm|shred) .*|cp .*", {"rm *", "shred *", "cp *"}),
        ("curl|wget", {"curl", "wget"}),
        # An empty branch matches only an empty command line, so it needs no rule.
        ("rm .*|", {"rm *"}),
    ],
)
def test_a_top_level_alternation_reaches_kas_as_one_deny_per_branch(regex: str, globs: set[str]):
    assert _shell_globs([regex]) == globs
    assert _named([regex]) == []
    settings = {"shell": {"deniedCommands": [regex]}}
    block, lost = tools_settings_to_permissions(settings)
    assert lost == []
    assert uncarried_denied_commands(settings, block) == []


@pytest.mark.parametrize(
    ("regex", "alternatives"),
    [
        (r"rm \| .*", [r"rm \| .*"]),  # an escaped pipe is a literal
        ("rm [|] .*", ["rm [|] .*"]),  # a pipe inside a class is a literal
        ("(a(rm|shred)) .*|cp .*", ["(a(rm|shred)) .*", "cp .*"]),  # nested group kept whole
        ("rm .*)|(cp .*", ["rm .*)|(cp .*"]),  # unbalanced
    ],
)
def test_only_a_pipe_outside_every_group_and_class_splits(regex: str, alternatives: list[str]):
    from kiro_crew.acp.kas_deny_alternatives import deny_alternatives

    assert deny_alternatives(regex) == alternatives


@pytest.mark.parametrize(
    ("regex", "globs"),
    [
        ("rm .*|shred +x", {"rm *"}),  # a branch with no glob
        ("(a(rm|shred)) .*|cp .*", {"cp *"}),  # a branch the split keeps whole
    ],
)
def test_a_branch_with_no_glob_leaves_the_entry_named(regex: str, globs: set[str]):
    """The branches that have a glob still travel, and the entry stays named as not
    carried, so nothing that reads "carried" treats it as enforced."""
    assert _shell_globs([regex]) == globs
    assert len(_named([regex])) == 1


def test_the_top_level_split_is_bounded():
    from kiro_crew.acp.kas_deny_alternatives import MAX_ALTERNATIVES, deny_alternatives

    wide = "|".join(f"c{i}" for i in range(MAX_ALTERNATIVES + 1))
    assert deny_alternatives(wide) == [wide]
    assert len(deny_alternatives("|".join(f"c{i}" for i in range(MAX_ALTERNATIVES)))) == 64


def test_a_bot_worker_still_holds_its_shell_for_a_top_level_alternation():
    """The branches travel, but a Bot's worker held every command that is not
    read-only for this entry, and keeps doing so: kiro-cli's narrower reading must not
    let a command run there that was refused."""
    from kiro_crew import mate_spec_policy
    from kiro_crew.acp.kas_spec_denies import with_unjudged_denies

    spec = _spec(["rm .*|shred .*", "(touch|mkdir) .*"])
    assert mate_spec_policy.unjudged_denies(spec) == ["rm .*|shred .*"]
    entry = kas_agents.to_client_custom_agent("a", spec, "p")
    [held] = with_unjudged_denies([entry], "a", spec)
    records = held["_crewShellUnjudgedDenies"]
    assert [(r["entry"], r["conditional"]) for r in records] == [("rm .*|shred .*", False)]


# ── a branch keeps its edge spaces ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("regex", "globs"),
    [
        # kiro-cli's own conversion writes `["rm * ", " shred *"]` for this entry.
        ("rm .* | shred .*", ["rm * ", " shred *"]),
        ("^rm .* | shred .*$", ["rm * ", " shred *"]),
        ("rm .* |(touch|mkdir) .*", ["rm * ", "touch *", "mkdir *"]),
    ],
)
def test_a_top_level_branch_keeps_its_edge_spaces(regex: str, globs: list[str]):
    out = kas_agents.to_client_custom_agent("a", _spec([regex]), "p")
    [rule] = [r for r in out["permissions"]["rules"] if r["capability"] == "shell"]
    assert rule["match"] == globs
    assert _named([regex]) == []


@pytest.mark.parametrize(
    ("regex", "globs"),
    [
        # kiro-cli writes `[" rm  *", " shred  *"]` and `["nice rm *", " rm *"]`.
        ("( rm | shred ) .*", [" rm  *", " shred  *"]),
        ("(sudo|) rm .*", ["sudo rm *", " rm *"]),
        ("(nice|) rm .*", ["nice rm *", " rm *"]),
        ("(sudo |)rm .*", ["sudo rm *", "rm *"]),
    ],
)
def test_a_group_branch_keeps_its_edge_spaces(regex: str, globs: list[str]):
    out = kas_agents.to_client_custom_agent("a", _spec([regex]), "p")
    [rule] = [r for r in out["permissions"]["rules"] if r["capability"] == "shell"]
    assert rule["match"] == globs
    assert _named([regex]) == []


def test_an_edge_spaced_split_still_keeps_the_allowlist():
    from kiro_crew.acp.kas_permissions import CREW_SHELL_ALLOWLIST_KEY

    shell = {
        "denyByDefault": True,
        "allowedCommands": ["git status.*"],
        "deniedCommands": ["touch .* | mkdir .*"],
    }
    spec = {**_spec([]), "toolsSettings": {"shell": shell}, "allowedTools": ["execute_bash"]}
    out = kas_agents.to_client_custom_agent("a", spec, "p")
    assert out[CREW_SHELL_ALLOWLIST_KEY] == ["git status.*"]


@pytest.mark.parametrize(
    ("regex", "globs"),
    [
        # kiro-cli writes `["rm *", "shred *"]` for this entry and refuses both.
        ("((rm|shred)) .*", {"rm *", "shred *"}),
        ("(((rm|shred))) .*", {"rm *", "shred *"}),
        ("(?:(rm|shred)) .*", {"rm *", "shred *"}),
        ("((rm|shred)) (a|b)", {"rm a", "rm b", "shred a", "shred b"}),
    ],
)
def test_a_group_wrapping_one_group_reads_as_that_group(regex: str, globs: set[str]):
    assert _shell_globs([regex]) == globs
    assert _named([regex]) == []


@pytest.mark.parametrize(
    "regex",
    ["((rm|shred)|cp) .*", "((rm|shred)c) .*", "((rm|shred)+) .*", "((rm|shred))+ .*"],
)
def test_a_group_holding_more_than_one_group_stays_whole(regex: str):
    from kiro_crew.acp.kas_deny_alternatives import deny_alternatives

    assert deny_alternatives(regex) == [regex]
    assert _shell_globs([regex]) == set()
