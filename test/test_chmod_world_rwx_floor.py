"""The ``local-destructive-chmod-777`` row is enforced on the argv, not on one spelling.

The row's literal ``chmod 777.*`` recognises the mode change only when the mode
word follows the program word directly.  Every option between them re-spells the
same command, so the row gains an argv-structural floor
(``security.chmod_floor``) that reads the options at any position and the first
operand after them as the mode.  These tests pin the contract from both sides:

* every option spelling of a world-rwx ``chmod`` is DENIED, including through a
  wrapper, a nested payload, a chained segment and re-quoted words;
* every other mode and every inert mention of the verb stays ALLOWED, so the
  floor cannot be a regex-widening in disguise;
* the floor is a UNION with the regex row (the row's own spelling is denied with
  the floor stubbed out), it respects the row's opt-out, and the catalog row,
  pattern and golden fixture are untouched;
* the deny matrix REVERTS to allowed when the floor is stubbed out, which is what
  proves these tests exercise the floor and not some neighbour.

Residuals the floor does not close are pinned to their CURRENT answer so a change
in either direction is a visible decision rather than drift.
"""

from __future__ import annotations

import pytest

from kiro_crew.security import chmod_floor as _chmod_floor
from kiro_crew.security import denied_rules as _denied_rules
from kiro_crew.security import is_denied

RULE_ID = "local-destructive-chmod-777"
ROW_PATTERN = "chmod 777.*"

#: The spellings the row's literal missed, each one the identical permission change.
#: Grouped by the mechanism that defeated the literal; every one is denied now.
FLAG_VARIANTS_DENIED = (
    # a single option between program and mode
    "chmod -R 777 ~",
    "chmod -v 777 ~",
    "chmod -f 777 ~",
    "chmod -c 777 ~",
    "chmod --recursive 777 ~",
    "chmod --verbose 777 ~",
    "chmod --changes 777 ~",
    "chmod --silent 777 ~",
    # packed short options, in either order and at any length
    "chmod -Rv 777 ~",
    "chmod -vR 777 ~",
    "chmod -Rvf 777 ~",
    "chmod -Rvfc 777 ~/project",
    "chmod -cfvR 777 ~/project",
    # split short options, and short mixed with long, in either order
    "chmod -R -v 777 ~",
    "chmod -v -R 777 ~",
    "chmod -R --verbose 777 ~",
    "chmod --recursive -v 777 ~",
    "chmod --recursive --verbose 777 ~",
    "chmod -v --recursive -f 777 ~",
    # GNU long-option abbreviations
    "chmod --rec 777 ~",
    "chmod --verb --rec 777 ~",
    # end-of-options marker before the mode
    "chmod -R -- 777 ~",
    "chmod -- 777 ~",
    # options AFTER the mode (GNU permutes them; the literal happened to catch
    # some of these, the floor catches all of them)
    "chmod -R 777 -v ~",
    # the same mode with a leading special-bits digit, and any run of leading zeros
    # (GNU folds octal digits into one value: ``00777`` is ``777``)
    "chmod -R 0777 ~",
    "chmod 0777 ~",
    "chmod -R 00777 ~",
    "chmod -R 000000777 ~",
    "chmod -R 01777 ~",
    "chmod -R 1777 ~",
    "chmod -R 2777 ~",
    "chmod -R 4777 ~",
    "chmod -R 7777 ~",
    # the operator-prefixed octal GNU accepts: ``=777`` sets, ``+777`` adds these bits
    "chmod -R =777 ~",
    "chmod -R +777 ~",
    "chmod +0777 ~",
    "chmod -R =1777 ~",
    # a redirection between the options and the mode is the shell's, not chmod's
    "chmod -R 2>/dev/null 777 ~",
    "chmod -R > /dev/null 777 ~",
    "chmod -R 2> /dev/null 777 ~",
    "chmod -R &>/dev/null 777 ~",
    "chmod -R >/dev/null 2>&1 777 ~",
    "chmod -R 2>&1 777 ~",
    "chmod -R 777 ~ 2>/dev/null",
    "chmod 2>/dev/null -R 777 ~",
    # a redirection ATTACHED to the mode word: bash splits ``+777>/dev/null`` into
    # the word ``+777`` and a redirect (measured: sets 777)
    "chmod -R +777>/dev/null ./project",
    "chmod -R =777>/dev/null ./project",
    "chmod -R 777 ./project>/dev/null",
    # a glued ``&``/``&&`` is a command boundary: the next command's
    # ``--reference`` is not chmod's
    "chmod -R 777 ./project&echo --reference=template",
    "chmod -R 777 ./project&&echo --reference=template",
    "chmod -R 777 ./project& echo --reference=template",
    "chmod -R 777 ./project &echo --reference=template",
    "chmod -R 777 ./p>out&echo --reference=t",
    "chmod -R 777 ./p 2>&1&echo --reference=t",
    # brace expansion the shell performs before chmod runs
    "chmod -R {777,755} ~",
    "chmod -R 7{7,5}7 ~",
    "chmod -R {7..7}77 ~",
    # a same-line assignment the reader resolves
    "m=777; chmod -R $m ~",
    "M=777 && chmod -R $M ~",
    # re-quoted words: the shell hands the program the same argv
    "chmod -R '777' ~",
    'chmod -R "777" ~',
    '"chmod" -R 777 ~',
    "ch''mod -R 777 ~",
    'ch""mod -R 777 ~',
    "chmod '-R' 777 ~",
    "chmod -R 777 $HOME",
    'chmod -R 777 "$HOME"',
    "chmod -R 777 ${HOME}",
    # the program by path, and a leading environment assignment
    "/bin/chmod -R 777 ~",
    "/usr/bin/chmod -R 777 /srv/www",
    "X=1 chmod -R 777 ~",
    # no file operand at all (xargs feeds it): still the mode change
    "chmod -R 777",
)

#: The same change reached through a program that EXECUTES its arguments.
WRAPPED_DENIED = (
    "sudo chmod -R 777 ~",
    "sudo -u www-data chmod -R 777 /srv/www",
    "env chmod -R 777 ~",
    "env -i chmod -R 777 ~",
    "nice -n 5 chmod -R 777 ~",
    "nohup chmod -R 777 ~",
    "time chmod -R 777 ~",
    "command chmod -R 777 ~",
    "exec chmod -R 777 ~",
    "busybox chmod -R 777 ~",
    "find ~ -exec chmod -R 777 {} \\;",
    "find ~ -type d -exec chmod -Rv 777 {} +",
    "find ~ -type d | xargs chmod -R 777",
    "find ~ -print0 | xargs -0 chmod -R 777",
    "docker exec web chmod -R 777 /var/www",
)

#: The same change inside a nested shell payload or a chained segment.
NESTED_DENIED = (
    "bash -c 'chmod -R 777 ~'",
    'sh -c "chmod -R 777 ~"',
    "bash -c \"chmod -Rv 777 '$HOME'\"",
    "eval 'chmod -R 777 ~'",
    "echo $(chmod -R 777 ~)",
    "cat <(chmod -R 777 ~)",
    "ls; chmod -R 777 ~",
    "ls && chmod -R 777 ~",
    "ls || chmod -R 777 ~",
    "ls;chmod -R 777 ~",
    "ls|chmod -R 777 ~",
    "grep -c x f|chmod -R 777 ~",
    "true && sudo chmod -R 777 ~ && echo done",
    "chmod -R 777 ~;ls",
    "chmod -R 777;ls ~",
    "chmod -R 777 ~ # make it writable",
)

DENIED_MATRIX = FLAG_VARIANTS_DENIED + WRAPPED_DENIED + NESTED_DENIED

#: Other modes, other tools, and inert mentions of the verb: all allowed, before
#: and after, so the floor is provably not a wider regex.
ALLOWED_MATRIX = (
    # other numeric modes, with the same option spellings
    "chmod 775 ~",
    "chmod -R 775 ~",
    "chmod -R 755 ~/project",
    "chmod -Rv 700 ~/.ssh",
    "chmod --recursive 644 ~/notes",
    "chmod 0755 ~",
    "chmod 1755 /tmp/x",
    "chmod 2775 /srv/shared",
    "chmod 600 ~/.ssh/id_ed25519",
    "chmod -R 177 ~",
    "chmod 77 ~",
    # symbolic modes, with options
    "chmod +x script.sh",
    "chmod -R +x bin/",
    "chmod -v u+x script.sh",
    "chmod u+rwx,go-rwx secret",
    "chmod -R u=rwx,g=rx,o= dir",
    "chmod -x script.sh",
    "chmod -R -w ~/archive",
    # the mode word is a FILE, not the mode
    "chmod 644 777",
    "chmod -R 755 ./777",
    "chmod --reference=/srv/template 777",
    "chmod --reference /srv/template 777",
    "chmod --ref=/srv/template 777",
    "chmod -v 777 --reference=/srv/template f",
    "chmod -v 777 --reference /srv/template f",
    "chmod -v 2>/dev/null 777 --reference=/srv/template f",
    # ``-777`` REMOVES all permission bits (measured: mode 000), not a grant; and
    # an all-digit word glued before ``>`` is a file descriptor to the shell, so no
    # mode reaches chmod (measured: ``chmod: missing operand``)
    "chmod -R -777 ~",
    "chmod -R 777>/dev/null ~",
    "chmod -R 0777>/dev/null ~",
    # other modes with the spellings the deny matrix admits
    "chmod -R 00755 ~",
    "chmod -R =755 ~",
    "chmod -R +755 ~",
    "chmod -R 2>/dev/null 755 ~",
    "chmod -R {755,750} ~",
    # other tools with the same numbers
    "chown -R 777 ~",
    "chgrp -R 777 ~",
    "mkdir -m 777 /tmp/scratch",
    "install -m 777 build/app /tmp/app",
    "git update-index --chmod=+x script.sh",
    "echo 777 > count.txt",
    "ls -l /bin/chmod",
    "stat /bin/chmod",
    "which chmod",
    "man chmod",
    # inert mentions: the verb as DATA of a search or a message
    "grep -rn 'chmod -R 777' src/",
    "grep -rn chmod -R 777 src/",
    "grep -n 'chmod 777' src/kiro_crew/security/denied_rules.py",
    "rg -n 'chmod -R 777' .",
    "rg -n 'chmod -Rvfc 777' test/",
    "rg -n chmod *",
    "git grep -n chmod -- src/ test/",
    "git log --grep 'chmod -R 777'",
    "git commit -m 'chmod -R 777 is denied now'",
    "cat notes.txt | grep 'chmod -R 777'",
    "git show HEAD:src/x.py | grep -nE 'chmod|chown|/etc/' | head -50",
    "sed -i 's/chmod -R 777/chmod -R 755/' Dockerfile",
    "cat chmod",
    "head -n 5 ~/chmod",
    "python -c \"print('chmod -R 777 ~')\"",
    # the legitimate chmod work the security-scope lane's candidate corpus names
    "find . -type f -perm 0777 -exec chmod 644 {} +",
    "find . -type d -perm -0777 -exec chmod -R 755 {} \\;",
    "git ls-files -z '*.sh' | xargs -0 chmod 755",
    "docker exec web chmod 644 /var/www/html/index.html",
    "chmod -R u+rwX,go-w ~/project",
    "chmod --reference=pyproject.toml setup.cfg",
    "chmod 755 {bin,scripts}/*.sh",
    "chmod -R 2775 /srv/shared 2>/dev/null || true",
    "m=750; chmod -R $m ~/project/build",
    "bash -c 'chmod +x scripts/*.sh && ./scripts/build.sh'",
    "chmod -v 0640 ~/.config/kirocrew/config.toml",
    "stat -c '%a %n' dist/cli.js | grep -q 755 || chmod 755 dist/cli.js",
    "wsl chmod -R 755 ~/project",
    "chmod 755 /c/Users/dev/project/scripts/build.sh",
)

#: Spellings the floor does NOT read, pinned to the answer they get today.  Each
#: is a different grammar from a numeric mode operand of a shell-level ``chmod``;
#: closing one is a decision with its own test, not a drift of this one.
RESIDUAL_ALLOWED_TODAY = (
    # symbolic world-rwx: relative to the current bits and the umask, and the issue
    # this floor closes asks that symbolic modes stay untouched
    "chmod -R a+rwx ~",
    "chmod a=rwx ~",
    "chmod -R ugo=rwx ~",
    "chmod -R +rwx ~",
    # a comma LIST is evaluated clause by clause (``+777,u-w`` is 577; ``u+x,=777``
    # is 777): reading it means composing the clauses, which this floor does not
    "chmod -R u+x,=777 ~",
    "chmod -R =600,+777 ~",
    # a value that needs the line RUN to be known: arithmetic, a substitution's
    # output -- the documented limit of every static floor in this package
    "chmod -R $((777)) ~",
    "chmod -R $(echo 777) ~",
    # a glob or expansion in the PROGRAM word: the filesystem or the environment
    # decides what runs, and the floor reads only a resolved program name
    "ch?od -R 777 ~",
    "${CH}mod -R 777 ~",
    # a mode change inside an interpreter payload is not a shell argv at all
    "python -c \"import os; os.chmod('/tmp/x', 0o777)\"",
    "perl -e 'chmod 0777, \"/tmp/x\"'",
)


def _denied_by(cmd: str) -> str | None:
    reason = is_denied(cmd)
    return None if reason is None else reason.splitlines()[0]


class TestFlagVariantsAreDenied:
    @pytest.mark.parametrize("cmd", DENIED_MATRIX)
    def test_every_spelling_of_the_world_rwx_change_is_denied(self, cmd: str) -> None:
        first_line = _denied_by(cmd)
        assert first_line is not None, cmd
        # The refusal names the ROW's pattern, so the refusal and its SEL event
        # map back to the rule id exactly as a regex-tier hit would.
        assert first_line.endswith(ROW_PATTERN), (cmd, first_line)

    @pytest.mark.parametrize("cmd", DENIED_MATRIX)
    def test_the_floor_itself_reads_each_spelling(self, cmd: str) -> None:
        """The predicate, asked directly, on the lowered text ``is_denied`` hands it."""
        assert _chmod_floor._is_chmod_world_rwx(cmd.lower()), cmd

    @pytest.mark.parametrize("cmd", DENIED_MATRIX)
    def test_the_pre_filter_is_a_necessary_condition_for_every_hit(self, cmd: str) -> None:
        """A hit the pre-filter would skip is a bypass, so every denied spelling must
        pass it."""
        assert _chmod_floor._chmod_floor_can_fire(cmd.lower()), cmd

    def test_a_floor_hit_carries_the_structural_note_and_the_diagnostic(self) -> None:
        reason = is_denied("chmod -R 777 ~")
        assert reason is not None
        lines = reason.splitlines()
        assert lines[0] == f"{_denied_rules.DENY_REASON_PREFIX}{ROW_PATTERN}"
        assert lines[1] == _denied_rules._CHMOD_WORLD_RWX_FLOOR_NOTE
        assert lines[-1].startswith("Refusal diagnostic: ")
        assert f"rule={RULE_ID}" in lines[-1]
        assert "component=argv-floor" in lines[-1]

    def test_the_literal_spelling_is_still_denied(self) -> None:
        """The spelling the row always caught keeps being caught."""
        for cmd in ("chmod 777 ~", "chmod 777 /tmp/x", "chmod 777 -R ~"):
            assert _denied_by(cmd) == f"{_denied_rules.DENY_REASON_PREFIX}{ROW_PATTERN}", cmd


class TestOtherModesAndMentionsStayAllowed:
    @pytest.mark.parametrize("cmd", ALLOWED_MATRIX)
    def test_allowed(self, cmd: str) -> None:
        assert is_denied(cmd) is None, cmd

    @pytest.mark.parametrize("cmd", ALLOWED_MATRIX)
    def test_the_floor_does_not_read_an_allowed_command(self, cmd: str) -> None:
        assert not _chmod_floor._is_chmod_world_rwx(cmd.lower()), cmd

    @pytest.mark.parametrize("cmd", RESIDUAL_ALLOWED_TODAY)
    def test_a_documented_residual_is_pinned_to_its_current_answer(self, cmd: str) -> None:
        """Flips when the residual is closed -- deliberately, with its own test."""
        assert is_denied(cmd) is None, cmd
        assert not _chmod_floor._is_chmod_world_rwx(cmd.lower()), cmd


class TestTheFloorIsAUnionWithTheRow:
    def test_the_catalog_row_is_untouched(self) -> None:
        """The literal, the id and the pin map are what governance policies persist;
        the floor adds enforcement without rewriting any of them."""
        row = _denied_rules._RULES_BY_ID[RULE_ID]
        assert row.pattern == ROW_PATTERN
        assert row.category == "local-destructive"
        assert _denied_rules._CHMOD_WORLD_RWX_FLOOR_PATTERN == ROW_PATTERN
        assert _denied_rules._CHMOD_WORLD_RWX_FLOOR_RULE_ID == RULE_ID
        assert _denied_rules._rule_id_for_pattern(ROW_PATTERN) == RULE_ID
        assert ROW_PATTERN not in _denied_rules._LEGACY_RULE_ID_BY_PATTERN
        # The row stays in the regex tier: it is NOT floor-enforced-only.
        assert RULE_ID not in _denied_rules.floor_enforced_builtin_command_ids()

    def test_the_row_stays_in_the_regex_tier_when_the_floor_is_stubbed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A floor that REPLACED the regex would fail open on a reader hiccup; this
        one is a union, so the literal spelling is denied by the row alone."""
        monkeypatch.setattr(_chmod_floor, "_is_chmod_world_rwx", lambda _text: False)
        assert _denied_by("chmod 777 ~") == f"{_denied_rules.DENY_REASON_PREFIX}{ROW_PATTERN}"
        assert _denied_by("sudo chmod 777 /srv/www") is not None

    def test_a_raising_floor_is_a_decision_not_an_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The predicate catches its own reader errors; the gate never raises."""

        def boom(_text: str) -> bool:
            raise RuntimeError("reader hiccup")

        monkeypatch.setattr(_chmod_floor, "_shell_payload_walk", boom)
        assert _chmod_floor._is_chmod_world_rwx("chmod -r 777 ~") is False
        # ...and the row's own spelling is still refused by the regex tier.
        assert is_denied("chmod 777 ~") is not None

    @pytest.mark.parametrize("cmd", ("chmod -R 777 ~", "sudo chmod -Rv 777 ~", "chmod 777 ~"))
    def test_the_floor_respects_the_rows_opt_out(self, cmd: str) -> None:
        """An operator who disables the row disables the floor with it: the floor
        runs only while the row's pattern is in the effective set."""
        without_row = [p for p in _denied_rules.BUILTIN_DENY_PATTERNS if p != ROW_PATTERN]
        assert is_denied(cmd, denied_regexes=without_row) is None, cmd
        # And an effective set that holds ONLY the row is enough for the floor.
        assert is_denied(cmd, denied_regexes=[ROW_PATTERN]) is not None, cmd


class TestNonShellSubjectsReachTheFloor:
    """Every producer that hands the literal row a non-shell subject hands the
    floor the same subject, because all of them call ``is_denied``: a tool-input
    string, a tool-call title, and computer-use typed text."""

    def test_a_tool_input_string_is_refused_in_the_flag_spelling(self) -> None:
        from kiro_crew.llm_helpers import _first_tool_input_denial

        denial = _first_tool_input_denial(
            ["deploy.sh", "chmod -R 777 /srv/www"], _denied_rules.BUILTIN_DENY_PATTERNS
        )
        assert denial is not None
        kind, reason, matched = denial
        assert kind == "regex"
        assert reason.splitlines()[0].endswith(ROW_PATTERN)
        assert matched == "chmod -R 777 /srv/www"
        assert _first_tool_input_denial(["chmod -R 755 /srv/www"], None) is None

    def test_a_tool_title_is_refused_in_the_flag_spelling(self) -> None:
        from kiro_crew.llm_helpers import _title_denial

        denial = _title_denial("sudo chmod -Rv 777 ~", _denied_rules.BUILTIN_DENY_PATTERNS)
        assert denial is not None
        assert denial[0] == "regex"
        assert denial[1].splitlines()[0].endswith(ROW_PATTERN)

    def test_computer_use_typed_text_is_refused_in_the_flag_spelling(self) -> None:
        from kiro_crew.computer_use import policy
        from kiro_crew.computer_use.types import AppRef

        editor = AppRef(name="TextEdit", pid=1, bundle_id="com.apple.TextEdit")
        cfg = policy.PolicyConfig()
        refusal = policy.check_input_target(editor, None, "chmod --recursive 777 ~", cfg)
        assert refusal is not None
        assert ROW_PATTERN in refusal
        assert policy.check_input_target(editor, None, "chmod --recursive 755 ~", cfg) is None


class TestHeredocBodiesAreReadAsCommands:
    """A heredoc body's lines become commands of the frame that carries them: the
    shared walk folds a newline to ``;`` and cannot tell ``bash <<EOF`` (which runs
    the body) from ``cat <<EOF`` (which prints it) without attributing the body to
    its consumer.  Reading the body is the fail-closed direction, and it is the
    reading every floor in this package already has -- the credential-mint floor
    refuses the same prose shapes for its own verb.  So a heredoc that merely
    MENTIONS the flag spelling in prose is refused, over-strictly.  Pinned here as
    a residual, beside the proof that the floor is not alone in it: a change to
    the walk's heredoc attribution flips both halves together."""

    PROSE_HEREDOCS = (
        "cat > notes.md <<'EOF'\nNever run chmod -R 777 ~\nEOF",
        "git commit -F - <<EOF\nfix: deny chmod -R 777\nEOF",
        "gh pr create --body \"$(cat <<'EOF'\nthis denies chmod -R 777 in any spelling\nEOF\n)\"",
    )

    @pytest.mark.parametrize("cmd", PROSE_HEREDOCS)
    def test_prose_in_a_heredoc_is_refused_today(self, cmd: str) -> None:
        assert _denied_by(cmd) == f"{_denied_rules.DENY_REASON_PREFIX}{ROW_PATTERN}", cmd
        # The literal spelling in the same prose is refused by the row itself, so the
        # floor makes the flag spelling behave as the literal already does here.
        literal = cmd.replace("chmod -R 777", "chmod 777")
        assert _denied_by(literal) == f"{_denied_rules.DENY_REASON_PREFIX}{ROW_PATTERN}", literal

    @pytest.mark.parametrize("cmd", PROSE_HEREDOCS)
    def test_the_credential_mint_floor_reads_the_same_prose_the_same_way(self, cmd: str) -> None:
        mint = cmd.replace("chmod -R 777", "kirocrew token")
        reason = is_denied(mint)
        assert reason is not None, mint
        assert "rule=credential-exfil-kirocrew-token component=argv-floor" in reason

    def test_a_heredoc_to_a_shell_runs_its_body_and_is_refused(self) -> None:
        for cmd in ("bash <<'EOF'\nchmod -R 777 ~\nEOF", "sh <<EOF\nchmod -R 777 ~\nEOF"):
            assert _denied_by(cmd) == f"{_denied_rules.DENY_REASON_PREFIX}{ROW_PATTERN}", cmd

    def test_a_quoted_message_is_data_and_stays_allowed(self) -> None:
        """The same prose as ONE quoted word is an argument of ``git``, not a
        command of its own: the shell hands it over as one token whose program
        reading is not ``chmod``."""
        assert is_denied("git commit -m 'never chmod -R 777 anything'") is None


class TestTheMatrixExercisesTheFloor:
    """Stub the floor out and the deny matrix reverts to the gate's prior answer.

    Without this the deny matrix could be green for a reason unrelated to the
    floor (a neighbouring rule, a view the regex tier already had).  The commands
    the row's literal catches on its own are excluded from the revert check and
    asserted separately, so the two sets together partition the matrix.
    """

    #: Members of the deny matrix the regex row catches WITHOUT the floor.  Both
    #: are the ``find | xargs chmod`` pipeline, for which Pass 2 already renders
    #: the command xargs would run against the find roots as an extra view, and
    #: that view spells the literal.  Measured with the floor stubbed out.
    CAUGHT_BY_THE_ROW_ALONE = frozenset(
        {
            "find ~ -type d | xargs chmod -R 777",
            "find ~ -print0 | xargs -0 chmod -R 777",
        }
    )

    @pytest.mark.parametrize("cmd", DENIED_MATRIX)
    def test_stubbing_the_floor_reverts_the_spelling_to_allowed(
        self, cmd: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_chmod_floor, "_is_chmod_world_rwx", lambda _text: False)
        reverted = is_denied(cmd)
        if cmd in self.CAUGHT_BY_THE_ROW_ALONE:
            assert reverted is not None, cmd
        else:
            assert reverted is None, (cmd, reverted)


class TestTheArgvReader:
    """The option/operand reading, asked directly of the words after ``chmod``."""

    reads = staticmethod(_chmod_floor._chmod_argv_sets_world_rwx)

    def test_the_first_non_option_word_is_the_mode(self) -> None:
        assert self.reads(["777", "~"])
        assert self.reads(["-R", "777", "~"])
        assert self.reads(["-R", "-v", "--changes", "777", "~"])
        assert not self.reads(["-R", "775", "~"])
        assert not self.reads(["-R", "755", "777"])

    def test_the_end_of_options_marker_ends_options(self) -> None:
        assert self.reads(["--", "777", "~"])
        assert self.reads(["-R", "--", "777", "~"])
        # after ``--`` a dash-led word is an operand (the mode), not an option
        assert not self.reads(["--", "-R", "777", "~"])

    def test_a_reference_option_means_there_is_no_mode_operand(self) -> None:
        assert not self.reads(["--reference=/srv/t", "777"])
        assert not self.reads(["--reference", "/srv/t", "777"])
        assert not self.reads(["--ref=/srv/t", "777"])
        assert not self.reads(["--ref", "/srv/t", "777"])
        # ``--re`` is ambiguous with ``--recursive`` and is NOT read as reference
        assert self.reads(["--re", "777", "~"])

    def test_a_reference_option_after_the_mode_word_still_exempts_it(self) -> None:
        """GNU permutes options, so the option that changes the reading can come
        AFTER the word it changes: ``777`` is a file here, not the mode."""
        assert not self.reads(["-v", "777", "--reference=/srv/t", "f"])
        assert not self.reads(["-v", "777", "--reference", "/srv/t", "f"])
        assert not self.reads(["777", "--ref", "/srv/t"])
        # ...and a redirection between ``--reference`` and its file does not eat it
        assert not self.reads(["--reference", "2>/dev/null", "/srv/t", "777"])
        assert not self.reads(["--reference", ">", "/dev/null", "/srv/t", "777"])

    def test_a_redirection_is_not_an_operand(self) -> None:
        """Glued (``2>/dev/null``) or spaced (``> /dev/null``, operator then target),
        duplicating (``2>&1``) or both-streams (``&>/dev/null``): none reaches chmod."""
        assert self.reads(["-R", "2>/dev/null", "777", "~"])
        assert self.reads(["-R", ">", "/dev/null", "777", "~"])
        assert self.reads(["-R", "2>", "/dev/null", "777", "~"])
        assert self.reads(["-R", "&>/dev/null", "777", "~"])
        assert self.reads(["-R", "&>", "/dev/null", "777", "~"])
        assert self.reads(["-R", ">>", "log", "777", "~"])
        assert self.reads(["-R", "2>&1", "777", "~"])
        assert self.reads(["-R", ">&2", "777", "~"])
        assert self.reads(["-R", "<", "in", "777", "~"])
        assert self.reads(["2>/dev/null", "-R", "777", "~"])
        assert self.reads(["-R", "777", "~", "2>/dev/null"])
        # the redirect's own target is never the mode
        assert not self.reads(["-R", ">", "777", "~"])
        assert not self.reads(["-R", "2>", "777", "755", "~"])
        # a digit run glued before ``>`` is a file descriptor to the shell, and a
        # word glued before ``>`` is the word (bash's lexing, measured)
        assert not self.reads(["-R", "777>/dev/null", "~"])
        assert self.reads(["-R", "+777>/dev/null", "~"])
        assert self.reads(["-R", "=777>log", "~"])
        assert not self.reads(["-R", "0777>/dev/null", "~"])  # all digits: fd 0777
        assert self.reads(["-R", "777", "~>/dev/null"])

    def test_a_glued_control_operator_ends_the_argv_and_keeps_the_head(self) -> None:
        assert self.reads(["-R", "777;ls", "~"])
        assert self.reads(["-R", "777", "~;ls", "-la"])
        assert not self.reads(["-R", "775;chmod", "777", "~"])
        assert not self.reads(["-R", ";", "777", "~"])

    def test_a_glued_ampersand_is_a_command_boundary(self) -> None:
        """``./project&echo --reference=t`` runs ``echo`` as a NEW command (measured:
        the mode lands), so its ``--reference`` is not chmod's.  ``_ends_argv``
        declines to cut at a glued ``&`` because a redirection's ``>&`` carries one;
        here the redirection is split off first, so what is left of an ``&`` is
        bash's boundary."""
        assert self.reads(["-R", "777", "./project&echo", "--reference=t"])
        assert self.reads(["-R", "777", "./project&&echo", "--reference=t"])
        assert self.reads(["-R", "777", "./project&", "echo", "--reference=t"])
        assert self.reads(["-R", "777", "./project", "&echo", "--reference=t"])
        assert self.reads(["-R", "777", "./p>out&echo", "--reference=t"])
        assert self.reads(["-R", "777", "./p", "2>&1&echo", "--reference=t"])
        assert self.reads(["-R", "777", "./p", "2>&1", "&echo", "--reference=t"])
        # a descriptor duplication's ``&`` is the redirection's, not a boundary
        assert not self.reads(["-R", "777", "./p", "2>&1", "--reference=t"])
        assert not self.reads(["-R", "777", "./p", ">&2", "--reference", "t"])

    def test_split_word_mirrors_bash(self) -> None:
        split = _chmod_floor._split_word
        assert split("777") == ("777", False)
        assert split("+777>/dev/null") == ("+777", False)
        assert split("777>/dev/null") == ("", False)
        assert split("2>&1") == ("", False)
        assert split("x2>f") == ("x2", False)
        assert split("./project&echo") == ("./project", True)
        assert split("./project&&echo") == ("./project", True)
        assert split("./p>out&echo") == ("./p", True)
        assert split("2>&1&echo") == ("", True)
        assert split("777;ls") == ("777", True)
        assert split("f|cat") == ("f", True)

    def test_a_comment_ends_the_argv(self) -> None:
        assert self.reads(["-R", "777", "~", "#", "comment"])
        assert not self.reads(["-R", "#", "777"])

    def test_the_mode_class_matches_what_gnu_chmod_grants(self) -> None:
        """Measured against the real binary: every member below sets the low nine
        bits to ``rwxrwxrwx``; every non-member either sets something else or is
        refused by chmod as an invalid mode."""
        for mode in (
            "777",
            "0777",
            "00777",
            "000000777",
            "1777",
            "2777",
            "3777",
            "4777",
            "5777",
            "6777",
            "7777",
            "01777",
            "=777",
            "+777",
            "=0777",
            "+0777",
            "=1777",
            "+1777",
            "=7777",
        ):
            assert self.reads([mode, "f"]), mode
        for mode in (
            "775",
            "7770",
            "77",
            "77777",  # over 07777: chmod refuses it
            "-777",  # removes the bits
            "a+rwx",
            "a=777",  # a letter clause cannot take octal: chmod refuses it
            "u+x,=777",  # a comma list, out of this floor
            "+x",
            "777x",
            "x777",
            "=",
            "+",
            "",
        ):
            assert not self.reads([mode, "f"]), repr(mode)

    def test_a_brace_or_glob_mode_word_is_read_as_its_expansion(self) -> None:
        assert self.reads(["{777,755}", "f"])
        assert self.reads(["{755,777}", "f"])  # over-strict by design, see the reader
        assert self.reads(["7{7,5}7", "f"])
        assert self.reads(["{7..7}77", "f"])
        assert self.reads(["7?7", "f"])
        assert not self.reads(["{755,750}", "f"])
        assert not self.reads(["7{5,0}7", "f"])
        # expansions whose value needs the line run stay literal
        assert not self.reads(["$((777))", "f"])
        assert not self.reads(["$(echo", "777)", "f"])
        assert not self.reads(["$m", "f"])


class TestWhichWordIsAnInvocation:
    """A ``chmod`` word is read as the invocation when it is the program, an
    argument of a program that executes its arguments, or a program glued after a
    control operator -- and as data when a data consumer owns it."""

    names = staticmethod(_chmod_floor._names_chmod_invocation)

    def test_program_position_and_executing_wrappers(self) -> None:
        assert self.names("chmod", "chmod")
        assert self.names("/bin/chmod", "chmod")
        assert self.names("chmod", "sudo")
        assert self.names("chmod", "xargs")
        assert self.names("chmod", "find")
        assert self.names("chmod", "")  # an unattributed word is read as executing

    def test_data_consumers_own_a_mention(self) -> None:
        for consumer in ("grep", "cat", "ls", "head", "stat", "wc"):
            assert not self.names("chmod", consumer), consumer
            assert not self.names("/bin/chmod", consumer), consumer

    def test_an_emitter_or_helper_spawner_owns_a_mention_too(self) -> None:
        """``rg``, ``sed``, ``awk`` and ``echo`` are excluded from the inert-mention
        narrowing's accepted set because they can EMIT or SPAWN -- but none of them
        hands its argv to ``chmod``, so the words after a ``chmod`` argument are
        theirs, not a mode: ``rg -n chmod *`` searches, and reading its bare ``*``
        as a mode that could expand to ``777`` refused an ordinary search.  The
        regex row keeps catching the literal under them on its own."""
        for program in ("echo", "printf", "cp", "mv", "tee", "rg", "sed", "awk"):
            assert not self.names("chmod", program), program
        assert is_denied("rg -n chmod *") is None
        assert is_denied("sed -i 's/chmod -R 777/chmod -R 755/' Dockerfile") is None
        # ...while the literal under an emitter is the regex row's, unchanged
        assert _denied_by("echo chmod 777 ~") == f"{_denied_rules.DENY_REASON_PREFIX}{ROW_PATTERN}"

    def test_a_glued_operator_makes_the_word_a_program_again(self) -> None:
        assert self.names("f|chmod", "grep")
        assert self.names("x;chmod", "cat")
        assert not self.names("2>&1", "grep")

    def test_a_word_that_is_not_the_program_is_not_read(self) -> None:
        assert not self.names("--chmod=+x", "git")
        assert not self.names("chmodx", "sudo")
        assert not self.names("777", "chmod")
