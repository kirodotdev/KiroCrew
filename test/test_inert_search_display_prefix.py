"""A ``Running: ``-prefixed shell title earns the inert-search carve-out its command earns.

kiro-cli titles a shell call ``Running: <command>``, and both deny tiers judge that
title as a subject of its own: the hook gate lists it among its deny targets beside
the prefix-stripped title and the bare command, and the title tier that serves cron,
Slack, side-panel and workflow turns judges it directly. The carve-out on the two
recursive-delete rules is a verb-anchored glob (``grep *``), so the prefixed title
never matched it, and a plain search whose pattern names a recursive delete was
refused although its bare command is exonerated.

The prefixed title must get exactly the verdict of the command it displays: the
benign search passes, and every neighbour that the bare command refuses -- the
delete itself, chaining, a pipe into an interpreter, an expansion, a redirection, a
verb outside the list -- is still refused under the prefix.
"""

from __future__ import annotations

import pytest

from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig, UserDeniedPattern
from kiro_crew.llm_helpers import _title_denial
from kiro_crew.security import is_denied

# Assembled at runtime so this module stays searchable by the rules it exercises.
ROOT_WIPE = "rm -" + "rf /"
HOME_WIPE = "rm -" + "rf ~"

#: kiro-cli's shell-title prefix, and its cut for a command over 200 characters.
PREFIX = "Running: "
_TITLE_CUT = 197


def _title(command: str) -> str:
    """The title kiro-cli sends for *command*."""
    if len(command) > 200:
        return f"{PREFIX}{command[:_TITLE_CUT]}..."
    return f"{PREFIX}{command}"


#: Plain searches whose bare command the carve-out already exonerates.
BENIGN_SEARCHES = [
    f'grep -rn "{ROOT_WIPE}" test/',
    f"grep -rn -e '{ROOT_WIPE}' src/",
    f'egrep -r "{HOME_WIPE}" test/',
    f'fgrep "{HOME_WIPE}" docs/',
    # Long enough that kiro-cli cuts the title; the pattern sits before the cut.
    f'grep -rn "{ROOT_WIPE}" ' + " ".join(f"dir{i}/" for i in range(60)),
]

#: Commands the bare-command verdict refuses; the prefix must not change that.
REFUSED_NEIGHBOURS = [
    ROOT_WIPE,
    HOME_WIPE,
    f"sudo {ROOT_WIPE}",
    *(f'grep -rn "needle" test/ {joiner} {ROOT_WIPE}' for joiner in ("&&", ";", "||", "|", "&")),
    f'grep -rn "needle" test/\n{ROOT_WIPE}',
    f"grep -rl x test/ | xargs {ROOT_WIPE}",
    f"grep '{ROOT_WIPE}' payload.sh | sh",
    f"grep '{ROOT_WIPE}' f && echo done",
    f'grep -rn "$({ROOT_WIPE})" test/',
    f'grep -rn "`{ROOT_WIPE}`" test/',
    f"grep x <({ROOT_WIPE}tmp/victim)",
    f"grep x ${{ {ROOT_WIPE};}}",
    f"grep -rn '{ROOT_WIPE}' test/ > out.txt",
    f'/usr/bin/grep -rn "{ROOT_WIPE}" test/',
    f'rg "{ROOT_WIPE}" src/',
    f"{ROOT_WIPE} /bin/grep x",
    "grep -rn x test/ && mkfs.ext4 /dev/sda1",
]


class TestTheCarveOutReadsThroughThePrefix:
    @pytest.mark.parametrize("command", BENIGN_SEARCHES, ids=range(len(BENIGN_SEARCHES)))
    def test_a_prefixed_plain_search_is_exonerated(self, command: str) -> None:
        assert is_denied(command) is None, "the bare command must already be exonerated"
        assert is_denied(_title(command)) is None

    @pytest.mark.parametrize("command", REFUSED_NEIGHBOURS, ids=range(len(REFUSED_NEIGHBOURS)))
    def test_a_refused_command_stays_refused_under_the_prefix(self, command: str) -> None:
        assert is_denied(command) is not None, "the bare command must already be refused"
        assert is_denied(_title(command)) is not None

    @pytest.mark.parametrize(
        "title",
        [
            # The prefix is read once: a second copy is text in command position.
            f'{PREFIX}{PREFIX}grep -rn "{ROOT_WIPE}" test/',
            # Only the shell prefix: a read title is not a command line.
            f'Reading grep -rn "{ROOT_WIPE}" test/',
            # Exact spelling only, and only at the start.
            f'Running:grep -rn "{ROOT_WIPE}" test/',
            f'Running:  grep -rn "{ROOT_WIPE}" test/',
            f'x {PREFIX}grep -rn "{ROOT_WIPE}" test/',
            f"{ROOT_WIPE} {PREFIX}grep x",
        ],
    )
    def test_only_the_exact_leading_display_prefix_is_read(self, title: str) -> None:
        assert is_denied(title) is not None

    def test_the_prefix_read_is_the_shell_prefix_the_hook_gate_strips(self) -> None:
        # The security package sits below the hook gate in the import graph, so it
        # holds its own copy of the prefix; this pins the two to one spelling.
        from kiro_crew.hooks import _TOOL_TITLE_PREFIXES
        from kiro_crew.security.denied_rules import _SHELL_TITLE_PREFIX

        assert PREFIX in _TOOL_TITLE_PREFIXES
        assert _SHELL_TITLE_PREFIX == PREFIX.lower()


class TestTheHookGate:
    @pytest.mark.parametrize("command", BENIGN_SEARCHES, ids=range(len(BENIGN_SEARCHES)))
    def test_a_prefixed_plain_search_is_not_refused(self, command: str) -> None:
        mgr = HookManager(HooksConfig())
        result = mgr.on_tool_call(_title(command), command=command, is_shell=True)
        assert result.action != TOOL_DENY, result.reason

    @pytest.mark.parametrize(
        "command",
        [ROOT_WIPE, f'grep -rn "needle" test/ && {ROOT_WIPE}', f"grep '{ROOT_WIPE}' f | sh"],
    )
    def test_the_real_delete_is_refused(self, command: str) -> None:
        mgr = HookManager(HooksConfig())
        result = mgr.on_tool_call(_title(command), command=command, is_shell=True)
        assert result.action == TOOL_DENY

    def test_a_rule_written_against_the_prefixed_title_still_binds(self) -> None:
        command = f'grep -rn "{ROOT_WIPE}" test/'
        cfg = HooksConfig(
            denied_commands_user_added=[UserDeniedPattern(id="u1", pattern=r"^Running: grep\b")]
        )
        result = HookManager(cfg).on_tool_call(_title(command), command=command, is_shell=True)
        assert result.action == TOOL_DENY


class TestTheTitleTier:
    @pytest.mark.parametrize("command", BENIGN_SEARCHES, ids=range(len(BENIGN_SEARCHES)))
    def test_a_prefixed_plain_search_is_not_refused(self, command: str) -> None:
        assert _title_denial(_title(command), None) is None

    @pytest.mark.parametrize(
        "command",
        [ROOT_WIPE, f'grep -rn "needle" test/ && {ROOT_WIPE}', f"grep '{ROOT_WIPE}' f | sh"],
    )
    def test_the_real_delete_is_refused(self, command: str) -> None:
        hit = _title_denial(_title(command), None)
        assert hit is not None and hit[0] == "regex"
