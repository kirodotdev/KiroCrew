"""The self-nudge-loop skill must hand an agent instructions that actually run.

``skills/self-nudge-loop/scaffold.sh`` writes the ``LOOP.md`` a user follows to
arm a goal loop from the dashboard, and the ready-to-paste nudge inside it drives
a kanban-md board on every cycle. ``SKILL.md`` carries the same board commands,
and ``skills/goal-loop/SKILL.md``, which wraps this scaffold, summarizes them.
Two things must hold:

* The popover steps name exactly the inputs the "Set a goal" popover renders. A
  step naming an input the popover does not have leaves the user hunting for it.
* Every kanban-md command uses a subcommand, flags and arguments that kanban-md
  accepts. A command kanban-md rejects (``unknown flag``) fails on every cycle
  of a loop that follows the nudge.

Both checks read the files as text, so they run on every CI platform without
bash or kanban-md installed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "self-nudge-loop"
SCAFFOLD = SKILL_DIR / "scaffold.sh"
LOCALES = REPO_ROOT / "website" / "src" / "i18n" / "locales"

# kanban-md 0.37.0's flags (`kanban-md <subcommand> --help`) for the subcommands
# this skill tells an agent to run. Persistent flags are accepted by every one.
# Pinned to the version the skill's commands were checked against: moving the
# skill to a newer kanban-md means re-reading its help and updating this table.
_GLOBAL_FLAGS = frozenset({"--dir", "--json", "--table", "--compact", "--oneline", "--no-color"})
_SUBCOMMAND_FLAGS: dict[str, frozenset[str]] = {
    "create": frozenset(
        {
            "--title",
            "--status",
            "--priority",
            "--assignee",
            "--tags",
            "--due",
            "--estimate",
            "--parent",
            "--depends-on",
            "--body",
            "--class",
            "--claim",
        }
    ),
    "edit": frozenset(
        {
            "--title",
            "--status",
            "--priority",
            "--assignee",
            "--add-tag",
            "--remove-tag",
            "--due",
            "--clear-due",
            "--estimate",
            "--body",
            "--append-body",
            "-a",
            "--timestamp",
            "-t",
            "--started",
            "--clear-started",
            "--completed",
            "--clear-completed",
            "--parent",
            "--clear-parent",
            "--add-dep",
            "--remove-dep",
            "--block",
            "--unblock",
            "--claim",
            "--release",
            "--class",
        }
    ),
    "handoff": frozenset({"--claim", "--note", "--timestamp", "-t", "--block", "--release"}),
    "init": frozenset({"--name", "--statuses", "--wip-limit"}),
    "list": frozenset(
        {
            "--status",
            "--priority",
            "--assignee",
            "--tag",
            "--sort",
            "--reverse",
            "-r",
            "--limit",
            "-n",
            "--blocked",
            "--not-blocked",
            "--parent",
            "--unblocked",
            "--unclaimed",
            "--claimed-by",
            "--class",
            "--search",
            "-s",
            "--archived",
            "--group-by",
        }
    ),
    "move": frozenset({"--next", "--prev", "--claim"}),
    "pick": frozenset({"--claim", "--status", "--move", "--tags", "--no-body"}),
    "show": frozenset(),
}
# Subcommands whose first positional argument is the task ID.
_TAKES_ID = frozenset({"edit", "handoff", "move", "show"})
# `pick` and `handoff` refuse to run without a claimant, and a card `pick`
# claimed refuses an `edit` that does not name the same claimant. Every edit
# this skill describes targets the card the cycle just picked. The one
# exception is `edit --release`, which drops a claim without naming it:
# kanban-md refuses `--claim` and `--release` together on `edit` ("cannot use
# --claim and --release together"), while `handoff` accepts the pair.
_NEEDS_CLAIM = frozenset({"edit", "handoff", "pick"})

# Where a command ends inside prose or a code comment: a closing backtick or
# parenthesis, a shell `;`, or a trailing `# comment`.
_COMMAND_END = re.compile(r"`|;|\)|\s#")


def _kanban_commands(text: str) -> list[tuple[str, list[str]]]:
    """Every kanban-md command in *text*, as ``(subcommand, arguments)``.

    A command is ``kanban-md --dir <board> <subcommand> ...`` or, without
    ``--dir``, a subcommand this table knows. Prose that only names the tool ("a
    kanban-md board") matches neither and is skipped; ``board`` is a real
    kanban-md subcommand, so the bare word cannot be the test.
    """
    commands: list[tuple[str, list[str]]] = []
    # A line ending in `\` continues on the next one, as in a shell command
    # split across lines: join them, so flags on the continuation are checked.
    for line in re.sub(r"\\\n[ \t]*", " ", text).splitlines():
        for match in re.finditer(r"kanban-md\b", line):
            rest = _COMMAND_END.split(line[match.end() :], maxsplit=1)[0]
            tokens = rest.split()
            has_dir = tokens[:1] == ["--dir"]
            if has_dir:
                tokens = tokens[2:]
            if tokens and (has_dir or tokens[0] in _SUBCOMMAND_FLAGS):
                commands.append((tokens[0], tokens[1:]))
    return commands


def _command_problems(sub: str, args: list[str]) -> list[str]:
    if sub not in _SUBCOMMAND_FLAGS:
        return [f"unknown subcommand {sub!r}"]
    problems = [
        f"unknown flag {arg!r}"
        for arg in args
        if arg.startswith("-") and arg not in _SUBCOMMAND_FLAGS[sub] | _GLOBAL_FLAGS
    ]
    if sub in _TAKES_ID and (not args or args[0].startswith("-")):
        problems.append("missing the task ID")
    releasing_edit = sub == "edit" and "--release" in args
    if releasing_edit and "--claim" in args:
        problems.append("edit refuses --claim together with --release")
    elif sub in _NEEDS_CLAIM and not releasing_edit and "--claim" not in args:
        problems.append("missing --claim")
    return problems


@pytest.mark.parametrize(
    ("path", "expected_subcommands"),
    [
        (SCAFFOLD, {"list", "pick", "move", "edit", "create", "init"}),
        (SKILL_DIR / "SKILL.md", {"list", "pick", "move", "show", "edit", "handoff"}),
        # goal-loop runs this skill's scaffold and summarizes the same cycle.
        (REPO_ROOT / "skills" / "goal-loop" / "SKILL.md", {"list", "pick", "create", "handoff"}),
    ],
    ids=["scaffold.sh", "SKILL.md", "goal-loop SKILL.md"],
)
def test_every_kanban_md_command_is_one_kanban_md_accepts(
    path: Path, expected_subcommands: set[str]
) -> None:
    commands = _kanban_commands(path.read_text(encoding="utf-8"))
    found = {sub for sub, _ in commands}

    assert expected_subcommands <= found, "the command scan missed commands it must check"
    problems = [
        f"kanban-md {sub} {' '.join(args)}: {problem}"
        for sub, args in commands
        for problem in _command_problems(sub, args)
    ]
    assert problems == []


@pytest.mark.parametrize(
    "path",
    [SCAFFOLD, SKILL_DIR / "SKILL.md", REPO_ROOT / "skills" / "goal-loop" / "SKILL.md"],
    ids=["scaffold.sh", "SKILL.md", "goal-loop SKILL.md"],
)
def test_claimant_is_stable_across_commands_and_cycles(path: Path) -> None:
    commands = _kanban_commands(path.read_text(encoding="utf-8"))
    claimants = [args[args.index("--claim") + 1] for _, args in commands if "--claim" in args]

    assert claimants, "the command scan must find claimants"
    assert len(set(claimants)) == 1, "every command must use the same loop claimant"
    assert all("cycle_n" not in claimant and "<n>" not in claimant for claimant in claimants)
    held_claimants = [
        args[args.index("--claimed-by") + 1]
        for sub, args in commands
        if sub == "list" and "--claimed-by" in args
    ]
    assert held_claimants, "each loop must look for held work before picking a new card"
    assert set(held_claimants) == set(claimants)
    # Every handoff releases the claim, so next cycle's --claimed-by scan does not
    # resume a card already moved to Review.
    for sub, args in commands:
        if sub == "handoff":
            assert "--release" in args, f"handoff must release the claim: {args}"
    # A held card the loop walks away from as blocked must also leave the claim,
    # or the --claimed-by scan resumes it every cycle: the only other release is
    # the handoff on completion.
    blocked_releases = [
        args
        for sub, args in commands
        if sub == "edit" and "--block" in args and "--release" in args
    ]
    assert blocked_releases, "a blocked held card must be released with edit --block --release"


def test_scaffold_claimants_are_double_quoted() -> None:
    # PROJECT expands in the heredoc; spaces must stay in one shell argument.
    claimants = re.findall(
        r'--claim(?:ed-by)?\s+("[^"]*"|\S+)', SCAFFOLD.read_text(encoding="utf-8")
    )
    assert claimants, "the command scan must find claimants"
    assert all(value.startswith('"') and value.endswith('"') for value in claimants)


def test_edit_release_and_claim_are_mutually_exclusive() -> None:
    # kanban-md 0.37.0: `edit --claim C --release` fails with "cannot use --claim
    # and --release together"; `edit --block ... --release` without --claim clears
    # the claim; any other edit on a claimed card still needs the claimant.
    assert _command_problems("edit", ["1", "--block", "x", "--release"]) == []
    assert _command_problems("edit", ["1", "--claim", "loop-p", "--release"]) == [
        "edit refuses --claim together with --release"
    ]
    assert _command_problems("edit", ["1", "--append-body", "note"]) == ["missing --claim"]
    handoff = ["1", "--claim", "loop-p", "--note", "x", "--release"]
    assert _command_problems("handoff", handoff) == []


def test_a_command_continued_on_the_next_line_is_scanned_whole() -> None:
    # scaffold.sh's `kanban-md init` puts --statuses on a continuation line.
    text = 'kanban-md init --dir "$B" --name demo \\\n  --statuses todo --bogus x\n'
    commands = _kanban_commands(text)

    assert commands == [
        ("init", ["--dir", '"$B"', "--name", "demo", "--statuses", "todo", "--bogus", "x"])
    ]
    assert _command_problems(*commands[0]) == ["unknown flag '--bogus'"]


def _popover_strings() -> dict[str, str]:
    """The English "Set a goal" popover strings, merged the way the dashboard does.

    ``en.manual.json`` holds hand-authored strings and wins on a collision with
    the generated ``en.json``.
    """
    strings: dict[str, str] = {}
    for name in ("en.json", "en.manual.json"):
        catalog = json.loads((LOCALES / name).read_text(encoding="utf-8"))
        strings.update(catalog.get("components", {}).get("autoNudgePopover", {}))
    return strings


def _ui_section() -> str:
    text = SCAFFOLD.read_text(encoding="utf-8")
    start = text.index("## Start the loop (UI)")
    return text[start : text.index("## Start the loop (REST)", start)]


def test_popover_steps_name_exactly_the_popover_inputs() -> None:
    popover = _popover_strings()
    section = _ui_section()
    named = [
        line.strip()[2:].split(":", 1)[0]
        for line in section.splitlines()
        if line.startswith("   - ")
    ]

    assert named == [
        popover["goal_description"],
        popover["seconds_between_nudges"],
        popover["max_cycles_0"],
    ]
    assert f"**{popover['start_loop']}**" in section
