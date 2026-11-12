"""A spec's ``toolsSettings.shell.allowedCommands`` on KAS: a matching command runs
without a prompt, and Crew's own checks still run on it.

On kiro-cli a command one of these patterns matches (anchored at both ends) runs
unprompted. KAS gets no shell allow rule (``kas_permissions.WITHHELD_CAPABILITIES``:
an auto-approved call raises no permission request, so the deny floor would never
see it), so every command reaches Crew's gate as a permission request, and the
``spec-builtin-grant`` tier answers one whose recovered command fullmatches a
pattern -- after every deny tier and governance, and never for a command that
chains, pipes, redirects or substitutes.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from test_kas_spec_builtin_grant import _handle, _permission
from test_tool_call_classification_kas import _kas_builtin_flow

from kiro_crew.acp import kas_agents
from kiro_crew.acp._dispatch import attach_kas_custom_agents
from kiro_crew.acp.kas_permissions import (
    _MAX_ALLOWED_COMMANDS,
    CREW_ANSWERED_COMMANDS_KEY,
    allowed_command_pattern,
    crew_answered_commands,
    tools_settings_to_permissions,
)
from kiro_crew.acp.types import AcpEvent
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.hooks import (
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    HookManager,
    HooksConfig,
    hook_gate_kwargs,
)
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.governance import parse_policy

#: Spelled in two pieces so this file's own text is not one destructive command line.
_DESTRUCTIVE = "rm " + "-rf /"


@pytest.fixture(autouse=True)
def _no_profiles(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def _shell(command: str) -> AcpEvent:
    return _kas_builtin_flow(
        "run_command", title="Run Command", kind="execute", raw_input={"command": command}
    )


def _judge(event: AcpEvent, patterns: list[str], **surface: Any):
    event.spec_granted_commands = tuple(allowed_command_pattern(p) or p for p in patterns)
    return HookManager(HooksConfig(**surface.pop("config", {}))).on_tool_call(
        event.title, **hook_gate_kwargs(event), **surface
    )


# ── a matching command: no prompt ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pattern", "command"),
    [
        ("git status", "git status"),
        ("npm run .*", "npm run build"),
        (r"\Agit diff .*\z", "git diff --stat"),
        ("^make test$", "make test"),
        (r"make -C \./sub", "make -C ./sub"),
        ("git .* --stat", "git diff HEAD --stat"),
        # A grouping character inside a quoted argument is plain text to the shell.
        ("git commit -m .*", 'git commit -m "docs(skills): weekly"'),
        ("git commit -m .*", "git commit -m 'fix(gate): {x}'"),
        ("git (commit|status)( .*)?", "git status"),
    ],
)
def test_a_command_an_allowed_pattern_matches_is_answered_without_a_prompt(
    pattern: str, command: str
) -> None:
    event = _shell(command)
    assert event.kas_builtin_ids and event.mcp_identity_trusted and event.is_shell
    ungranted = _judge(event, [])
    assert (ungranted.action, ungranted.tier) == (TOOL_ALLOW, "")
    granted = _judge(event, [pattern])
    assert (granted.action, granted.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


@pytest.mark.parametrize(
    ("pattern", "command"),
    [
        # Anchored at both ends, as kiro-cli anchors it.
        ("git status", "git status --porcelain"),
        ("npm run .*", "npx npm run build"),
        ("git push", "git pushx"),
    ],
)
def test_a_command_no_pattern_fullmatches_still_prompts(pattern: str, command: str) -> None:
    verdict = _judge(_shell(command), [pattern])
    assert verdict.action == TOOL_ALLOW
    assert verdict.tier != "spec-builtin-grant"


@pytest.mark.parametrize(
    "command",
    [
        "git status > out.txt",
        "git status < in.txt",
        "git status $(whoami)",
        "git status `whoami`",
        "git status ${HOME}",
        "git status & sleep 1",
        "git status (x)",
        "git status {a,b}",
        'git status "(x)" (y)',
        'git status "(x)',
        "git status '(x)\"",
        'git status \\"(x)\\"',
        # PowerShell reads typographic quotes as quotes too: each one closes or
        # reopens the ASCII-quoted string, leaving the subexpression live.
        'git status "x\u201c (calc) \u201cy"',
        "git status 'x\u2019 (calc) \u2018y'",
        'git status "x\u201e (calc) \u201dy"',
        'git status "$(whoami)"',
        "git status '`whoami`'",
        "git status\nmake",
    ],
)
def test_a_chained_or_substituted_command_is_never_granted(command: str) -> None:
    """``.*`` would match each of these; the call still prompts."""
    verdict = _judge(_shell(command), ["git status.*", ".*"])
    assert verdict.action != TOOL_AUTO_APPROVE


# ── a chain: granted only when every command in it is ─────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "cd build && ls -la out.txt",
        "cd build&&ls -la out.txt",
        "ls -la || cd build",
        "cd build; ls",
        "ls -la | cd build",
        "cd build && ls && cd ..",
    ],
)
def test_a_chain_whose_every_command_matches_runs_without_a_prompt(command: str) -> None:
    """kiro-cli asks about each command of a chain; when each is allowed, so is
    the chain."""
    verdict = _judge(_shell(command), ["cd .*", "ls.*"])
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


@pytest.mark.parametrize(
    "command",
    [
        # One command no entry matches.
        "cd build && make",
        "ls; make",
        "ls | make",
        "git status || true",
        # A quote or backslash in a chain: a separator may sit inside one argument.
        'ls "a && make"',
        'ls "a && cd b"',
        "ls 'x; cd' y",
        "ls a\\;cd b",
        'cd "x" && ls',
        # PowerShell's typographic quotes, or any other non-ASCII character.
        "ls \u201cscratch; cd \u201d build",
        "ls \u2018x; cd \u2019 y",
        "cd b\u00e9 && ls",
        # PowerShell's stop-parsing token makes the separator literal text.
        "cd --% build;ls -la",
        # Still never: background, redirect, substitution, newline, empty part.
        "cd build & ls",
        "cd build && ls > out.txt",
        "cd build && ls $(whoami)",
        "cd build && ls `whoami`",
        "cd build\nls",
        "cd build && && ls",
        "cd build ;",
        "|| make build",
        "cd build |& ls",
    ],
)
def test_a_chain_with_any_unmatched_or_unplain_command_still_asks(command: str) -> None:
    verdict = _judge(_shell(command), ["cd .*", "ls.*", "git status", "make build"])
    assert verdict.tier != "spec-builtin-grant"


def test_the_prompt_says_a_quoted_chain_always_asks() -> None:
    """Every part matches, but the quote refuses the chain: the note must not say
    a chain runs whenever every command in it matches."""
    from kiro_crew.platform.allowed_commands import COMPOSITION_NOTICE

    assert "quote" in COMPOSITION_NOTICE
    event = _shell('ls "a" && ls')
    event.spec_shell_allowlist = ("ls.*",)
    verdict = _judge(event, [])
    assert verdict.tier == "spec-shell-allowlist"
    assert "chain" in verdict.reason and "quote" in verdict.reason


@pytest.mark.parametrize(
    ("command", "patterns"),
    [
        ('rm "draft.tmp; echo " important.txt', ["rm .*\\.tmp", "echo .*"]),
        ('rm "x.tmp;ls" important.db', ["rm [^ ]*\\.tmp", "ls.*"]),
    ],
)
def test_a_quote_spanning_a_separator_is_never_granted(command: str, patterns: list[str]) -> None:
    """The split parts each match, but the shell runs one ``rm`` on a file no
    entry allows."""
    from kiro_crew.platform.allowed_commands import chain_commands

    assert chain_commands(command) is None
    assert _judge(_shell(command), patterns).tier != "spec-builtin-grant"


def test_the_deny_floor_still_refuses_a_chain_hiding_a_denied_command() -> None:
    verdict = _judge(_shell("ls && " + _DESTRUCTIVE), ["ls.*", "rm .*"])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "deny-rules")


def test_a_chain_past_the_bound_is_not_granted() -> None:
    from kiro_crew.platform.allowed_commands import MAX_CHAIN_COMMANDS

    at_cap = " && ".join(["make build"] * MAX_CHAIN_COMMANDS)
    assert _judge(_shell(at_cap), ["make build"]).tier == "spec-builtin-grant"
    assert _judge(_shell(at_cap + " && make build"), ["make build"]).action != TOOL_AUTO_APPROVE


def test_deny_by_default_lets_a_fully_listed_chain_through() -> None:
    event = _shell("cd build && ls")
    event.spec_shell_allowlist = ("cd .*", "ls.*")
    assert _judge(event, []).tier != "spec-shell-allowlist"
    event = _shell("cd build && make")
    event.spec_shell_allowlist = ("cd .*", "ls.*")
    assert _judge(event, []).action == TOOL_DENY


# ── a granted command the checks refuse ───────────────────────────────────────


def test_the_deny_floor_still_refuses_a_matching_command() -> None:
    verdict = _judge(_shell(_DESTRUCTIVE), ["rm .*"])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "deny-rules")


def test_a_governance_command_deny_still_refuses_a_matching_command() -> None:
    _install_governance({"tools": {"mode": "deny", "deny": ["execute_bash"]}})
    verdict = _judge(_shell("git status"), ["git status"], session_key="cli_chat")
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


def _install_governance(body: dict[str, Any]) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    base = build_default_context(KiroCrewConfig.load())
    policy = {"version": 1, "boot": {"fail_closed": True}, **body}
    ctx_mod.set_context(dataclasses.replace(base, governance=parse_policy(policy)))


def test_a_ceiling_with_an_opinion_on_the_shell_withholds_the_grant() -> None:
    """``git status`` is permitted by this ceiling, yet a ceiling that speaks about
    commands at all keeps it at the prompt: the grant is the ceiling's to give."""
    _install_governance({"commands": {"mode": "deny", "deny": ["*backdoor*"]}})
    verdict = _judge(_shell("git status --porcelain"), ["git status.*"])
    assert verdict.action == TOOL_ALLOW
    assert verdict.tier != "spec-builtin-grant"


def _builtin_pins() -> list[str]:
    """The built-in self-protection deny patterns, as an edition pins them in ``commands``."""
    from kiro_crew.security import BUILTIN_DENIED_RULES

    return [r.pattern for r in BUILTIN_DENIED_RULES if r.category == "self-protection"]


def test_a_ceiling_that_only_pins_built_in_rules_keeps_the_grant() -> None:
    """The deny floor already enforces every pinned rule, so the pins say nothing
    about other commands: ``git status`` is still answered without a prompt."""
    _install_governance({"commands": {"mode": "deny", "deny": _builtin_pins()}})
    verdict = _judge(_shell("git status"), ["git status"])
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


def test_a_pinned_command_is_still_refused_under_a_wide_grant() -> None:
    _install_governance({"commands": {"mode": "deny", "deny": _builtin_pins()}})
    pinned = "kirocrew app dev --confirm-" + "out-of-install-root"
    verdict = _judge(_shell(pinned), [".*"])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "deny-rules")


def test_a_pin_beside_any_other_command_deny_withholds_the_grant() -> None:
    _install_governance({"commands": {"mode": "deny", "deny": [*_builtin_pins(), "*backdoor*"]}})
    verdict = _judge(_shell("git status"), ["git status"])
    assert verdict.action == TOOL_ALLOW
    assert verdict.tier != "spec-builtin-grant"


def test_the_projection_records_the_patterns_under_built_in_pins() -> None:
    _install_governance({"commands": {"mode": "deny", "deny": _builtin_pins()}})
    spec = _spec(toolsSettings={"shell": {"allowedCommands": ["git status"]}})
    out = kas_agents.to_client_custom_agent("a", spec, "p")
    assert out[CREW_ANSWERED_COMMANDS_KEY] == ["git status"]


def test_confirm_tools_withholds_the_grant() -> None:
    verdict = _judge(_shell("git status"), ["git status"], config={"confirm_tools": ["*"]})
    assert verdict.action == TOOL_ALLOW


def test_an_unattended_read_only_surface_ignores_the_grant() -> None:
    verdict = _judge(_shell("make build"), ["make build"], classifier_only=True)
    assert verdict.action != TOOL_AUTO_APPROVE


def test_a_pattern_grants_only_the_shell_tool() -> None:
    event = _kas_builtin_flow("read_file", raw_input={"path": "/tmp/probe-note.txt"})
    assert _judge(event, [".*"]).tier != "spec-builtin-grant"


@pytest.mark.parametrize(
    "change",
    [{"mcp_identity_trusted": False}, {"kas_builtin_ids": False}],
    ids=["untrusted-identity", "not-kas"],
)
def test_only_a_host_identified_shell_call_is_granted(change: dict[str, Any]) -> None:
    event = _shell("git status")
    for name, value in change.items():
        setattr(event, name, value)
    assert _judge(event, ["git status"]).action != TOOL_AUTO_APPROVE


def test_a_shell_call_whose_command_was_not_recovered_is_never_granted() -> None:
    event = _kas_builtin_flow("run_command", title="Run Command", kind="execute", raw_input={})
    assert _judge(event, [".*"]).action != TOOL_AUTO_APPROVE


# ── the patterns and the projection ───────────────────────────────────────────


def test_only_the_syntax_read_like_kiro_cli_is_kept() -> None:
    assert allowed_command_pattern(r"\Agit status\z") == r"\Agit status\z"
    # Verbatim: a pattern's own spaces are part of what it grants.
    assert allowed_command_pattern("ls -la ") == "ls -la "
    # Rust's own syntax, read as Rust reads it (test_allowed_commands_rust_syntax).
    for good in (
        "rm [[:alpha:]]+",
        "rm [[:alpha:]]",
        r"ls \d+",
        r"ls \w",
        r"ls \D+",
        r"ls \W",
        r"ls \b",
        "make a{2}",
        "make a{2,}",
        "make a{2}?",
        "make a{257}",
        "make a*?",
        "make a+*",
        "(?i)make",
        "(?P<n>make)",
        "make]",
        "a^b",
        "a$b",
    ):
        assert allowed_command_pattern(good) == good, good
    for bad in (
        "",
        "x" * 1025,
        "(unclosed",
        7,
        None,
        "*make",
        "make (*a)",
        "make (a|*b)",
        "make)",
        "make a***",
        "make a{,2}",
        "make a{3,2}",
        "make [\\d-z]",
        "(?x)make",
        r"ls \p{L}",
        "[]a]",
        "[]",
        "[^]",
        "[a-c-e]",
        "[z-a]",
        "[a&&b]",
        "[a--b]",
        "[a~~b]",
        "[ab",
        "x" + "(" * 33 + ")" * 33,
        # Rust escapes only ASCII punctuation, and reads \< \> as word boundaries.
        "touch [^\\<]+",
        "touch (\\>|x)",
        "touch [^\\\u2603]+",
        "touch (\\\u201c|x)",
        # A literal-plus-``.*`` entry with a non-ASCII escape: kiro-cli grants nothing.
        "echo \\\u201chi\\\u201d",
        "git commit -m \\\u201c.*",
        "rm scratch\u00a0",
        "git\tstatus",
        "ls\x00",
    ):
        assert allowed_command_pattern(bad) is None, bad
    # The same character unescaped is a literal in both engines.
    assert allowed_command_pattern("echo \u201chi\u201d") == "echo \u201chi\u201d"


def test_a_posix_class_is_read_as_rust_reads_it() -> None:
    """Python would read ``[[:alpha:]]`` as a set of the characters ``[:alph``
    followed by a ``]``, wider than kiro-cli does."""
    event = _shell("rm a]")
    event.spec_granted_commands = ("rm [[:alpha:]]+",)
    verdict = HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))
    assert verdict.action != TOOL_AUTO_APPROVE


@pytest.mark.parametrize(
    ("pattern", "command", "granted"),
    [
        ("make (all|test)", "make test", True),
        ("make (all|test)", "make tests", False),
        ("make (?:all|test)", "make all", True),
        ("make ?", "make", True),
        ("make ?", "make  ", False),
        ("a.b", "axb", True),
        ("a.b", "ab", False),
        ("ls [A-Za-z0-9_./-]+", "ls src/kiro_crew-x.py", True),
        ("ls [A-Za-z0-9_./-]+", "ls src/kiro crew", False),
        ("ls [^ ]+", "ls a b", False),
        (r"ls [\]\-]+", "ls -]", True),
        ("ls [-a]+", "ls a-a", True),
        ("ls [a-]+", "ls -a", True),
        ("ls (-a|-l)*", "ls -a-l-a", True),
        ("ls (-a|-l)*", "ls ", True),
        ("git status|ls", "ls", True),
        ("git status|ls", "git status", True),
        ("git status|ls", "git status ls", False),
        ("make (a+)+", "make aaaa", True),
        (r"git log -n \d+", "git log -n 12", True),
        (r"git log -n \d+", "git log -n x", False),
        (r"rm\s+-f scratch", "rm  -f scratch", True),
        (r"rm\s+-f scratch", "rm-f scratch", False),
        (r"npm run [\w.-]+", "npm run build.prod", True),
        (r"npm run [\w.-]+", "npm run a b", False),
        (r"ls [^\s]+", "ls a b", False),
        ("make a{2}", "make aa", True),
        ("make a{2}", "make aaa", False),
        ("make a{2,3}", "make a", False),
        ("make a{2,3}", "make aaa", True),
        ("make a{2,3}", "make aaaa", False),
        ("make a{2,}", "make aaaaa", True),
        ("make (ab){0,2}c", "make ababc", True),
        ("make (ab){0,2}c", "make abababc", False),
    ],
)
def test_classes_groups_alternation_and_repeats_are_read_like_kiro_cli(
    pattern: str, command: str, granted: bool
) -> None:
    from kiro_crew.platform.allowed_commands import matches, parse

    parsed = parse(pattern)
    assert parsed is not None and parsed.program is not None
    assert matches(parsed, command) is granted
    verdict = _judge(_shell(command), [pattern])
    assert (verdict.tier == "spec-builtin-grant") is granted


@pytest.mark.parametrize("pattern", ["make (a+)+", "make (a*)*(a|aa)*", "make ((a|aa)*)*"])
def test_nested_repetition_matches_in_linear_time(
    pattern: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python's engine backtracks on these without bound; the automaton visits a
    number of states linear in the command, so a budget of a few states per
    character is enough to reach the answer both ways."""
    from kiro_crew.platform import allowed_commands

    parsed = allowed_commands.parse(pattern)
    assert parsed is not None and parsed.program is not None
    command = "make " + "a" * 4000
    monkeypatch.setattr(
        allowed_commands, "_MAX_MATCH_STEPS", (len(parsed.program) + 1) * (len(command) + 1)
    )
    assert allowed_commands.matches(parsed, command)
    assert not allowed_commands.matches(parsed, command + "!")


@pytest.mark.parametrize("pattern", [r"git log -n \d+", r"ls [\w]+", r"ls\s.*"])
def test_a_class_escape_does_not_judge_a_non_ascii_command(pattern: str) -> None:
    """Rust reads ``\\d \\s \\w`` over Unicode and Crew over ASCII, so an entry using
    one judges no non-ASCII command: it grants nothing, without spending the budget
    the entries after it share, and a deny reader reads it as "cannot judge"."""
    from kiro_crew.platform import allowed_commands

    parsed = allowed_commands.parse(pattern)
    assert parsed is not None and parsed.ascii_only
    budget = [allowed_commands._MAX_MATCH_STEPS]
    assert not allowed_commands._match(parsed, "git log -n \uff11\uff12", budget)
    assert budget[0] == allowed_commands._MAX_MATCH_STEPS
    assert allowed_commands.denied_match([pattern], "ls caf\u00e9") is None
    assert not allowed_commands.matches_any([pattern], "ls caf\u00e9")
    # Skipped in a list: the entries after it still judge the command, in any order.
    for entries in ([pattern, "ls caf(\u00e9|e)"], ["ls caf(\u00e9|e)", pattern]):
        assert allowed_commands.matches_any(entries, "ls caf\u00e9")
        # The gate's chain matcher reads a plain command the same way.
        assert allowed_commands.chain_matches_all(entries, "ls caf\u00e9")
        assert _judge(_shell("ls caf\u00e9"), entries).tier == "spec-builtin-grant"


@pytest.mark.parametrize(
    ("pattern", "refused"),
    [
        ("make (a{256}){256}", True),
        ("git status(((){0,256}){256}){256}", True),
        ("git status((){256}){256}", True),
        # Copies of an empty group add no state, so this one compiles, and small.
        ("git status(){256}(){256}" * 20, False),
    ],
)
def test_counted_repeats_are_bounded_when_expanded(pattern: str, refused: bool) -> None:
    """No short entry expands into a program past the state bound: its expansion
    is refused, or it adds no state (an empty group), as in Rust."""
    from kiro_crew.platform import allowed_commands

    assert allowed_commands.parse("make (a{32}){32}") is not None
    parsed = allowed_commands.parse(pattern)
    if refused:
        assert parsed is None
    else:
        assert parsed is not None and parsed.program is not None
        assert len(parsed.program) <= allowed_commands._MAX_PROGRAM_STATES


def test_a_match_past_the_step_budget_grants_nothing() -> None:
    """A costly entry spends one shared budget; past it nothing more matches."""
    from kiro_crew.platform import allowed_commands

    costly = "(" + "|".join(["."] * 500) + ")*x"
    command = "a" * 4000 + "x"
    assert allowed_commands.parse(costly) is not None
    assert not allowed_commands.matches_any([costly, "a+x"], command)
    # Alone, the cheap entry matches; a literal entry never draws on the budget.
    assert allowed_commands.matches_any(["a+x"], command)
    assert allowed_commands.matches_any([costly, "a.*x"], command)


def test_a_wide_class_draws_on_the_shared_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each range a class compares costs a step, so 64 wide classes cannot run
    unbounded work under a budget that counts only states."""
    from kiro_crew.platform import allowed_commands

    wide = "echo ([^" + "a-b" * 330 + "])*"
    command = "echo " + "z" * 100
    parsed = allowed_commands.parse(wide)
    assert parsed is not None and allowed_commands.matches(parsed, command)
    # 100 class tests of 330 ranges each need about 33,000 steps; 10,000 is short.
    monkeypatch.setattr(allowed_commands, "_MAX_MATCH_STEPS", 10_000)
    assert not allowed_commands.matches(parsed, command)


@pytest.mark.parametrize(
    "command",
    [
        "rm scratch\u00a0",
        "\u00a0rm scratch",
        " rm scratch",
        "rm scratch ",
        "rm\tscratch",
        "rm scratch\x07",
    ],
)
def test_a_command_is_matched_verbatim(command: str) -> None:
    """Stripping or folding the command would approve a path the grant never named."""
    verdict = _judge(_shell(command), ["rm scratch"])
    assert verdict.action != TOOL_AUTO_APPROVE or verdict.tier != "spec-builtin-grant"


@pytest.mark.parametrize("command", ["rm scratch\u00a0", "rm a\tb", "rm a\x07"])
def test_a_wildcard_never_covers_an_invisible_character(command: str) -> None:
    verdict = _judge(_shell(command), ["rm .*"])
    assert verdict.action != TOOL_AUTO_APPROVE or verdict.tier != "spec-builtin-grant"


def test_matching_is_greedy_wildcards_between_literals() -> None:
    from kiro_crew.platform.allowed_commands import matches, parse

    segs = parse("git .* --stat")
    assert segs.segments == ("git ", " --stat")
    assert matches(segs, "git diff --stat")
    assert not matches(segs, "git --stat")
    assert matches(parse("a.*b.*c"), "axbyc")
    assert not matches(parse("a.*b.*c"), "acb")
    assert matches(parse("npm run .*"), "npm run ")
    assert not matches(parse("git status"), "git status ")


def test_crew_answered_commands_reads_both_shell_keys() -> None:
    settings = {
        "shell": {"allowedCommands": ["git status", "(bad", "git status"]},
        "execute_bash": {"allowedCommands": ["make .*"]},
        "read": {"allowedCommands": ["ignored"]},
    }
    assert crew_answered_commands(settings) == ["make .*", "git status"]
    assert crew_answered_commands(None) == []


def test_allowed_commands_are_named_only_when_a_pattern_cannot_be_used() -> None:
    assert tools_settings_to_permissions({"shell": {"allowedCommands": ["git status"]}}) == (
        None,
        [],
    )
    assert tools_settings_to_permissions({"shell": {"allowedCommands": ["(bad"]}})[1] == [
        "shell.allowedCommands"
    ]


def test_an_allow_list_over_the_bound_keeps_its_first_entries_and_is_named() -> None:
    patterns = [f"make target{i}" for i in range(_MAX_ALLOWED_COMMANDS + 1)]
    settings = {"shell": {"allowedCommands": patterns}}
    assert crew_answered_commands(settings) == patterns[:_MAX_ALLOWED_COMMANDS]
    assert tools_settings_to_permissions(settings)[1] == ["shell.allowedCommands"]


@pytest.mark.parametrize(
    "extra",
    [
        {"permissions": {"rules": [{"capability": "shell", "effect": "ask"}]}},
        # A deny Crew cannot read itself (`\p`) keeps them withheld; one it can
        # read lets the gate answer them (test_kas_spec_denies).
        {
            "toolsSettings": {
                "shell": {"allowedCommands": ["git status"], "deniedCommands": ["x\\pL"]}
            }
        },
    ],
    ids=["authored-ask", "unreadable-denied-commands"],
)
def test_the_notice_names_allowed_commands_the_session_withholds(extra: dict[str, Any]) -> None:
    spec = _spec(**{"toolsSettings": {"shell": {"allowedCommands": ["git status"]}}, **extra})
    lost = kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True)
    assert "toolsSettings.shell.allowedCommands" in lost


def test_the_notice_names_allowed_commands_the_ceiling_withholds(monkeypatch) -> None:
    spec = _spec(toolsSettings={"shell": {"allowedCommands": ["git status"]}})
    assert kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True) == []
    monkeypatch.setattr(kas_agents, "may_grant_builtin_at_gate_now", lambda ref: False)
    assert kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True) == [
        "toolsSettings.shell.allowedCommands"
    ]


def _spec(**extra: Any) -> dict[str, Any]:
    return {"name": "a", "prompt": "p", "tools": ["*"], **extra}


def test_the_projection_records_the_patterns_and_relays_no_shell_allow() -> None:
    spec = _spec(toolsSettings={"shell": {"allowedCommands": ["git status", r"npm run .*\z"]}})
    out = kas_agents.to_client_custom_agent("a", spec, "p")
    assert out[CREW_ANSWERED_COMMANDS_KEY] == ["git status", r"npm run .*\z"]
    rules = out.get("permissions", {}).get("rules", [])
    assert not [r for r in rules if r["capability"] == "shell" and r["effect"] == "allow"]


@pytest.mark.parametrize(
    "extra",
    [
        {"permissions": {"rules": [{"capability": "shell", "effect": "ask"}]}},
        {"toolsSettings": {"shell": {"allowedCommands": ["git status"], "timeout": 5}}},
        {"toolsSettings": {"shell": {"allowedCommands": ["git status"], "deniedCommands": ["x"]}}},
    ],
    ids=["authored-ask", "other-shell-setting", "denied-commands"],
)
def test_an_authored_shell_restriction_withholds_the_patterns(extra: dict[str, Any]) -> None:
    """The spec's own author restricted the shell, so its commands keep prompting,
    as a shell grant in `allowedTools` does under the same restriction."""
    spec = _spec(**{"toolsSettings": {"shell": {"allowedCommands": ["git status"]}}, **extra})
    assert CREW_ANSWERED_COMMANDS_KEY not in kas_agents.to_client_custom_agent("a", spec, "p")


def test_the_grant_itself_is_not_read_as_a_restriction() -> None:
    from kiro_crew.acp.kas_grant_only_settings import (
        without_allowed_commands,
        without_shell_grant_keys,
        without_write_grant_keys,
    )

    assert without_allowed_commands({"shell": {"allowedCommands": ["x"]}, "read": {"a": 1}}) == {
        "read": {"a": 1}
    }
    # `without_allowed_commands` keeps every other shell key as a restriction.
    assert without_allowed_commands(
        {"execute_bash": {"allowedCommands": ["x"], "denyByDefault": True}}
    ) == {"execute_bash": {"denyByDefault": True}}
    assert without_allowed_commands("bad") == "bad"
    # For the `allowedCommands` reading, `denyByDefault` and a boolean
    # `autoAllowReadonly` never turn a matched command into a prompt on kiro-cli, so
    # they go too; any other key stays.
    assert (
        without_shell_grant_keys(
            {"execute_bash": {"allowedCommands": ["x"], "denyByDefault": True}}
        )
        == {}
    )
    assert without_shell_grant_keys(
        {"shell": {"allowedCommands": ["x"], "autoAllowReadonly": True, "deniedCommands": ["y"]}}
    ) == {"shell": {"deniedCommands": ["y"]}}
    assert without_shell_grant_keys({"shell": {"autoAllowReadonly": "yes"}}) == {
        "shell": {"autoAllowReadonly": "yes"}
    }
    assert without_shell_grant_keys("bad") == "bad"
    # The write `allowedPaths` reading: `fallbackAction` only decides an uncovered
    # write, so it goes; `deniedPaths` stays.
    assert without_write_grant_keys(
        {"write": {"allowedPaths": ["a/**"], "fallbackAction": "deny"}, "shell": {"x": 1}}
    ) == {"shell": {"x": 1}}
    assert without_write_grant_keys(
        {"fs_write": {"allowedPaths": ["a/**"], "deniedPaths": ["a/x"]}}
    ) == {"fs_write": {"deniedPaths": ["a/x"]}}


def test_a_permissions_shell_allow_is_answered_by_its_globs() -> None:
    """A pure-KAS spec's ``permissions`` shell allow: each glob is recorded
    with ``*`` read as ``.*`` and every other character literal, a glob with other
    glob syntax is left out, and the record answers only a matching plain command."""
    rule = {"capability": "shell", "effect": "allow", "match": ["git fetch *", "make a.b", "ls ?"]}
    out = kas_agents.to_client_custom_agent("a", _spec(permissions={"rules": [rule]}), "p")
    patterns = out.get(CREW_ANSWERED_COMMANDS_KEY)
    assert patterns == ["git fetch .*", r"make a\.b"]
    assert not [r for r in out.get("permissions", {}).get("rules", []) if r["effect"] == "allow"]
    assert _judge(_shell("git fetch origin"), patterns).tier == "spec-builtin-grant"
    for command in ("make axb", "ls x", "git fetch x; echo hi"):
        assert _judge(_shell(command), patterns).tier != "spec-builtin-grant"


def test_a_permissions_shell_allow_with_an_exclude_keeps_prompting() -> None:
    """The gate cannot repeat an ``exclude``, so the allow records nothing."""
    rule = {"capability": "shell", "effect": "allow", "match": ["git *"], "exclude": ["git push *"]}
    out = kas_agents.to_client_custom_agent("a", _spec(permissions={"rules": [rule]}), "p")
    assert CREW_ANSWERED_COMMANDS_KEY not in out


def test_a_ceiling_that_withholds_the_shell_records_no_patterns(monkeypatch) -> None:
    monkeypatch.setattr(kas_agents, "may_grant_builtin_at_gate_now", lambda ref: False)
    spec = _spec(toolsSettings={"shell": {"allowedCommands": ["git status"]}})
    assert CREW_ANSWERED_COMMANDS_KEY not in kas_agents.to_client_custom_agent("a", spec, "p")


def test_the_patterns_never_reach_the_wire() -> None:
    spec = _spec(toolsSettings={"shell": {"allowedCommands": ["git status"]}})
    entry = kas_agents.to_client_custom_agent("a", spec, "p")
    params: dict[str, Any] = {}
    attach_kas_custom_agents(params, [entry])
    (wire,) = params["_meta"]["kiro"]["customAgents"]
    assert CREW_ANSWERED_COMMANDS_KEY not in wire
    assert CREW_ANSWERED_COMMANDS_KEY in entry


def test_the_session_record_follows_the_active_agent_and_a_mode_switch() -> None:
    from kiro_crew.agent_files import kas_wire_agent_id

    batch = [
        {"id": "vibe", "prompt": "p", CREW_ANSWERED_COMMANDS_KEY: ["git status", 7]},
        {"id": "other", "prompt": "p"},
    ]
    assert kas_agents.projected_crew_answered_commands(batch, "vibe") == ("git status",)
    assert kas_agents.projected_crew_answered_commands(batch, "missing") == ()
    assert kas_agents.switched_crew_answered_commands(batch, kas_wire_agent_id("vibe")) == (
        "git status",
    )
    assert kas_agents.switched_crew_answered_commands(batch, "other") == ()


def test_the_handle_stamps_the_patterns_on_its_own_request_only() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    handle.kas_crew_answered_commands = ("git status",)
    own = handle._build_permission_event(_permission("own-sid"))
    child = handle._build_permission_event(_permission("child-sid"))
    assert own is not None and own.spec_granted_commands == ("git status",)
    assert child is not None and child.spec_granted_commands == ()
