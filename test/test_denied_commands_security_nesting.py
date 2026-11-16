"""A command nested past the gate's substitution cap is refused before any floor runs.

The floors re-walk every substitution body as its own string, so on main their work grew
with depth times length: 120,930 ``_iter_shell_chars`` steps at depth 100 and 471,830 at
depth 200. Counts, never a clock.
"""

from __future__ import annotations

import cProfile

import pytest

from kiro_crew.security import is_denied
from kiro_crew.security import shell_normalizer_walk as _walk

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_CAP = 32  # deny_gate._MAX_SUBSTITUTION_DEPTH, spelled out so main collects
_DEPTH_REFUSAL = "substitution nesting deeper than 32"


def _nest(depth: int, inner: str = "true") -> str:
    """The issue's shape: each level a double-quoted ``$( )`` inside the last."""
    return 'echo "$(' * depth + inner + ')"' * depth


def _shell_char_steps(command: str) -> "tuple[str | None, int]":
    """``is_denied(command)`` and how many steps the shared quote machine took."""
    profile = cProfile.Profile()
    profile.enable()
    try:
        verdict = is_denied(command)
    finally:
        profile.disable()
    code = _walk._iter_shell_chars.__code__
    steps = sum(
        entry.callcount
        for entry in profile.getstats()
        if getattr(entry.code, "co_filename", None) == code.co_filename
        and getattr(entry.code, "co_firstlineno", None) == code.co_firstlineno
    )
    return verdict, steps


class TestSubstitutionDepthCap:
    @pytest.mark.parametrize("depth", [100, 200])
    def test_a_deep_nest_is_refused_without_walking_it(self, depth):
        verdict, steps = _shell_char_steps(_nest(depth))
        assert verdict is not None and _DEPTH_REFUSAL in verdict
        assert "flatten it" in verdict
        assert steps == 0  # main took 120,930 at 100 and 471,830 at 200

    def test_the_cap_itself_reaches_the_floors(self):
        verdict, steps = _shell_char_steps(_nest(_CAP))
        assert verdict is None
        assert steps > 0  # the floors ran

    def test_a_floor_still_refuses_at_the_cap(self):
        verdict = is_denied(_nest(_CAP, "git push origin main"))
        assert verdict is not None and _DEPTH_REFUSAL not in verdict

    def test_one_past_the_cap_is_refused_with_the_way_out(self):
        verdict = is_denied(_nest(_CAP + 1))
        assert verdict == (
            "Blocked by security policy: substitution nesting deeper than 32\n"
            "a command nested more than 32 substitutions deep is refused; flatten it"
        )

    @pytest.mark.parametrize(
        "command, past",
        [
            # Each $( opens a fresh quote context, so alternating quotes still nest.
            ("echo " + '"$(echo ' * 33 + "x" + ')"' * 33, True),
            ("(" * 33 + "true" + ")" * 33, False),  # no floor re-walks a subshell
            ("$( (" * 33 + "true" + ") )" * 33, True),  # a group's ) closes the group
            ("`true`" * 100, False),  # siblings, never deeper than one
            ("echo # '\n" + "echo $(" * 40 + "x" + ")" * 40, True),  # a comment's quote
            ("echo " + "$(case x in a) echo " * 40 + "x", True),  # a pattern's ) closes nothing
            ("$(case x in a) true;; esac)" * 40, False),  # esac ends the case, ) closes
            ("x=$(grep -c case f) " * 40, False),  # case as an argument opens no case
            ("echo " + "\\$(" * 40, False),  # an escaped $ opens nothing
            ("echo $# " + "$(" * 33 + ")" * 33, True),  # $# is not a comment
            ("echo " + "`" * 80, False),  # an unclosed backtick never raises the depth
            ('`echo "`' * 40, False),  # a backtick in a quote inside a span closes it
            ("echo '" + "$(" * 50 + "'", False),  # single-quoted text is data
            ('echo "' + "\\$(" * 50 + '"', False),  # an escaped $ opens nothing
            ("$(" * 32 + ")" * 32 + ";" + "$(" * 32 + ")" * 32, False),
            ("$(" * 33 + ")" * 33, True),
            ("<(" * 33 + ")" * 33, True),
            ("echo $'\\'" + "$(" * 40 + "'", False),  # ANSI-C escape keeps the quote open
        ],
    )
    def test_the_depth_scan_reads_the_shell_contexts(self, command, past):
        assert (_DEPTH_REFUSAL in (is_denied(command) or "")) is past


_PS_JSON = (
    "{" + ",".join(f'`"k{i}`":`"v{i}`"' for i in range(10)) + "}"
)  # 40 backtick-escaped quotes, nothing nested


class TestStringsThatAreNotPosixShell:
    """The scan reads posix quoting; strings in another syntax must not trip the cap."""

    @pytest.mark.parametrize(
        "command",
        [
            # The Security Scope Review's two confirmed rows (one repo value renamed).
            r'Invoke-RestMethod -Method Post -Uri http://localhost:8765/api/tasks -ContentType application/json -Body "{`"title`":`"nightly audit`",`"agent`":`"security-conductor`",`"priority`":`"high`",`"status`":`"queued`",`"source`":`"cron`",`"owner`":`"ops`",`"branch`":`"main`",`"repo`":`"demo`",`"note`":`"retry`"}"',
            r'Set-Content -Path .kiro\settings.json -Value "{`"model`":`"claude-opus-5-5`",`"region`":`"us-west-2`",`"profile`":`"dev`",`"timeout`":`"600`",`"retries`":`"3`",`"log`":`"info`",`"theme`":`"dark`",`"shell`":`"pwsh`",`"editor`":`"code`"}"',
            f'Set-Content -Path out.json -Value "{_PS_JSON}"',
            'Write-Host "status: `"$(Get-Date)`" ' + '`"$($i)`" ' * 20 + '"',
            'cmd /c "dir C:\\Users\\dev\\proj\\" && cmd /c "type C:\\tmp\\a.txt\\"',
            "C:\\Users\\dev\\workplace\\proj\\src\\main.py",
        ],
    )
    def test_a_windows_command_is_not_refused_by_the_cap(self, command):
        assert _DEPTH_REFUSAL not in (is_denied(command) or "")

    @pytest.mark.parametrize(
        "text",
        [
            "(defun f (x) " + "(g " * 60 + "x" + ")" * 61,  # code nesting parens
            "don't (stop " * 40,  # prose: an apostrophe and unclosed parens
            "s/(a|(b|(c)))/x/ " * 40,  # a regex
        ],
    )
    def test_a_tool_string_that_is_not_a_command_is_not_refused_by_the_cap(self, text):
        assert _DEPTH_REFUSAL not in (is_denied(text) or "")

    def test_the_issue_command_is_still_refused(self):
        assert _DEPTH_REFUSAL in (is_denied(_nest(1000)) or "")
