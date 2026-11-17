"""Tests for how a trusted pattern judges each segment of a compound command."""

from __future__ import annotations

from trust_patterns_helpers import (
    _extract_base_command,
    _legacy_title_command,
    _matches_trusted_pattern,
)

from kiro_crew.trust_patterns import redirect_outside_grant


class TestMatchesTrustedPatternPiped:
    """Piped commands: each segment checked independently, ALL must match."""

    def test_all_segments_match(self):
        patterns = {"cat /etc/*", "grep /Users/a*"}
        cmd = "Running: cat /etc/hosts | grep /Users/alice"
        assert _matches_trusted_pattern(cmd, patterns) is not None

    def test_one_segment_fails(self):
        patterns = {"cat /etc/*", "grep /Users/a*"}
        cmd = "Running: cat /etc/hosts | grep /Users/bob"
        assert _matches_trusted_pattern(cmd, patterns) is None

    def test_first_segment_fails(self):
        patterns = {"cat /etc/*", "grep /Users/a*"}
        cmd = "Running: cat /var/log/syslog | grep /Users/alice"
        assert _matches_trusted_pattern(cmd, patterns) is None

    def test_both_segments_fail(self):
        patterns = {"cat /etc/*", "grep /Users/a*"}
        cmd = "Running: cat /var/log/syslog | grep /Users/bob"
        assert _matches_trusted_pattern(cmd, patterns) is None

    def test_single_command_still_works(self):
        patterns = {"cat /etc/*"}
        cmd = "Running: cat /etc/hosts"
        assert _matches_trusted_pattern(cmd, patterns) == "cat /etc/*"

    def test_glob_patterns_per_segment(self):
        patterns = {"cat *", "wc *"}
        cmd = "Running: cat /anything | wc -l"
        assert _matches_trusted_pattern(cmd, patterns) is not None

    def test_glob_one_side_missing(self):
        patterns = {"cat *"}
        cmd = "Running: cat /etc/hosts | wc -l"
        assert _matches_trusted_pattern(cmd, patterns) is None

    def test_three_segments_all_match(self):
        patterns = {"cat *", "grep *", "wc *"}
        cmd = "Running: cat /etc/hosts | grep foo | wc -l"
        assert _matches_trusted_pattern(cmd, patterns) is not None

    def test_three_segments_one_fails(self):
        patterns = {"cat *", "grep *"}
        cmd = "Running: cat /etc/hosts | grep foo | wc -l"
        assert _matches_trusted_pattern(cmd, patterns) is None

    def test_and_chain_all_match(self):
        patterns = {"make *", "echo *"}
        cmd = "Running: make build && echo done"
        assert _matches_trusted_pattern(cmd, patterns) is not None

    def test_and_chain_one_fails(self):
        patterns = {"make *"}
        cmd = "Running: make build && rm -rf dist"
        assert _matches_trusted_pattern(cmd, patterns) is None

    def test_semicolon_chain(self):
        patterns = {"mkdir *", "cp *"}
        cmd = "Running: mkdir /tmp/out ; cp file.txt /tmp/out/"
        assert _matches_trusted_pattern(cmd, patterns) is not None

    def test_independent_patterns_per_segment(self):
        # cat trusted for .z* paths, grep trusted for /Users/a* paths
        patterns = {"cat /Users/alice/.z*", "grep /Users/a*"}
        good = "Running: cat /Users/alice/.zshrc | grep /Users/alice"
        bad = "Running: cat /Users/alice/.zshrc | grep /Users/bob"
        assert _matches_trusted_pattern(good, patterns) is not None
        assert _matches_trusted_pattern(bad, patterns) is None

    def test_background_operator_split(self):
        patterns = {"cat *"}
        cmd = "Running: cat foo & rm -rf /"
        assert _matches_trusted_pattern(cmd, patterns) is None

    def test_background_operator_both_match(self):
        patterns = {"cat *", "echo *"}
        cmd = "Running: cat foo & echo done"
        assert _matches_trusted_pattern(cmd, patterns) is not None

    def test_redirect_ampersand_not_split(self):
        patterns = {"ls *", "grep *"}
        cmd = "Running: ls /tmp 2>&1 | grep foo"
        assert _matches_trusted_pattern(cmd, patterns) is not None

    def test_redirect_stderr_to_file_not_split(self):
        patterns = {"ls *"}
        cmd = "Running: ls /tmp 2>&1"
        assert _matches_trusted_pattern(cmd, patterns) == "ls *"

    def test_redirect_1_to_2_not_split(self):
        patterns = {"echo *"}
        cmd = "Running: echo error 1>&2"
        assert _matches_trusted_pattern(cmd, patterns) == "echo *"

    def test_background_no_space_after(self):
        patterns = {"cat *"}
        cmd = "Running: cat foo &rm -rf /"
        assert _matches_trusted_pattern(cmd, patterns) is None

    def test_background_no_space_disown(self):
        patterns = {"cat *"}
        cmd = "Running: cat foo &disown"
        assert _matches_trusted_pattern(cmd, patterns) is None

    def test_redirect_then_background(self):
        patterns = {"cat *"}
        cmd = "Running: cat foo 2>&1& rm -rf /"
        assert _matches_trusted_pattern(cmd, patterns) is None

    def test_redirect_stdout_stderr_not_split(self):
        patterns = {"ls *"}
        cmd = "Running: ls /tmp &> /dev/null"
        assert _matches_trusted_pattern(cmd, patterns) == "ls *"

    def test_redirect_append_both_not_split(self):
        """``&>>`` stays inside its segment, and that segment writes a file.

        The matcher still reads the line as one ``ls`` segment; the write it
        carries is what the grant does not cover.
        """
        patterns = {"ls *"}
        cmd = "Running: ls /tmp &>> /var/log/out"
        assert _matches_trusted_pattern(cmd, patterns) == "ls *"
        assert redirect_outside_grant(_legacy_title_command(cmd), patterns) is True

    def test_newline_split(self):
        patterns = {"ls *"}
        cmd = "Running: ls /tmp\nrm -rf /"
        assert _matches_trusted_pattern(cmd, patterns) is None

    def test_newline_both_match(self):
        patterns = {"ls *", "echo *"}
        cmd = "Running: ls /tmp\necho done"
        assert _matches_trusted_pattern(cmd, patterns) is not None

    def test_returns_matched_patterns_for_audit(self):
        patterns = {"cat *", "grep *"}
        cmd = "Running: cat /etc/hosts | grep foo"
        result = _matches_trusted_pattern(cmd, patterns)
        assert result is not None
        assert "cat *" in result
        assert "grep *" in result

    def test_command_substitution_dollar_paren_denied(self):
        patterns = {"cat *"}
        cmd = "Running: cat $(echo /etc/passwd)"
        assert _matches_trusted_pattern(cmd, patterns) is None

    def test_command_substitution_backtick_denied(self):
        patterns = {"cat *"}
        cmd = "Running: cat `echo /etc/passwd`"
        assert _matches_trusted_pattern(cmd, patterns) is None

    def test_process_substitution_denied(self):
        patterns = {"diff *"}
        cmd = "Running: diff <(cat /etc/hosts) <(cat /tmp/hosts)"
        assert _matches_trusted_pattern(cmd, patterns) is None

    def test_wildcard_star_matches_all_segments(self):
        patterns = {"*"}
        cmd = "Running: cat /etc/hosts | grep foo | wc -l"
        assert _matches_trusted_pattern(cmd, patterns) is not None


class TestMatchesTrustedPatternQuoted:
    """Separators inside quotes must NOT split the command (fail-closed regression)."""

    def test_quoted_pipe_not_split(self):
        # `grep "a|b"` is a single trusted command; the quoted | must not be
        # treated as a pipe boundary that mis-segments and denies it.
        patterns = {'grep "a|b" *'}
        assert _matches_trusted_pattern('Running: grep "a|b" file.txt', patterns) is not None

    def test_quoted_pipe_with_real_pipe(self):
        # Quoted | stays literal; the real unquoted | still splits — both
        # segments must match.
        patterns = {'grep "x|y" *', "wc *"}
        cmd = 'Running: grep "x|y" f | wc -l'
        assert _matches_trusted_pattern(cmd, patterns) is not None

    def test_quoted_ampersand_not_background_split(self):
        patterns = {'echo "a&b" *'}
        assert _matches_trusted_pattern('Running: echo "a&b" done', patterns) is not None

    def test_quoted_semicolon_not_split(self):
        patterns = {"echo *"}
        # The quoted ; is part of echo's arg; single segment, matches echo.
        assert _matches_trusted_pattern('Running: echo "a;b"', patterns) is not None

    def test_real_pipe_outside_quotes_still_requires_all_segments(self):
        # Regression guard: quote-masking must NOT weaken the per-segment rule
        # for genuinely unquoted pipes — an unmatched segment still denies.
        patterns = {"cat /etc/*"}
        assert _matches_trusted_pattern("Running: cat /etc/hosts | rm -rf /", patterns) is None


class TestSplitterPlaceholderForgery:
    """A NUL byte in the title forges the splitter's own placeholders.

    ``_split_command_segments`` masks redirects as ``\\x00REDIR\\x00`` and
    quoted separators as ``\\x00SEP{n}\\x00``. Both schemes assume the input
    carries no NUL of its own. A model-authored title (or a ``tool_input``
    command) containing one breaks that assumption two ways: the
    redirect-restore loop draws more placeholders than it masked and raises
    ``StopIteration`` — an unhandled exception on the approval path, which
    aborts the chat turn — and a forged ``\\x00SEP{n}\\x00`` restores to a
    separator the command never contained. A NUL can reach here because JSON
    encodes it as ``\\u0000`` and Python decodes that to a real ``\\x00``; it is
    never legitimate in a command, since ``execve`` cannot carry it in an
    argument. So every path must deny rather than crash.
    """

    def test_forged_redirect_placeholder_denies_instead_of_crashing(self):
        cmd = "Running: echo hi \x00REDIR\x00 there"
        # Must not raise StopIteration; must not be trusted.
        assert _matches_trusted_pattern(cmd, {"echo *"}) is None
        assert _matches_trusted_pattern(cmd, {"*"}) is None

    def test_forged_separator_placeholder_denies(self):
        cmd = "Running: echo hi \x00SEP0\x00 there"
        assert _matches_trusted_pattern(cmd, {"echo *"}) is None

    def test_bare_nul_anywhere_denies(self):
        assert _matches_trusted_pattern("Running: cat /etc/hosts\x00", {"cat *"}) is None

    def test_grant_path_denies_forged_placeholders(self):
        assert _extract_base_command("Running: echo hi \x00REDIR\x00 there") == ""
        assert _extract_base_command("Running: echo hi \x00SEP0\x00 there") == ""

    def test_grant_path_does_not_offer_forged_extra_segments(self):
        # A forged SEP placeholder must not restore into a separator that
        # widens the offered trust set beyond the first binary.
        assert _extract_base_command("Running: echo ok\x00SEP0\x00 rm -rf /") == ""

    def test_nul_free_commands_are_unaffected(self):
        # Guard against the deny becoming over-broad: normal commands, quoted
        # separators and real redirects all still behave as before.
        assert _matches_trusted_pattern("Running: cat /etc/hosts", {"cat *"}) is not None
        assert _matches_trusted_pattern('Running: grep "a|b" f', {'grep "a|b" *'}) is not None
        assert _matches_trusted_pattern("Running: ls 2>&1", {"ls *"}) is not None
        assert _extract_base_command("Running: cat f | wc -l") == "cat,wc"
