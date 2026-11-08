"""Tests for the program a stdin-reading interpreter is judged on.

A stdin-reading interpreter is judged on its program rather than on its neighbours, an
output redirect is never mistaken for its script path, and a bare opener inside a redirect
target is not grammar.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestStdinProgramTextScoping:
    """A stdin-reading interpreter is judged on its PROGRAM, not on its neighbours.

    ``normalize_shell_command`` does not split a frame on a
    newline, so a multi-line script arrives as ONE token frame.  The stdin branch of
    ``_has_self_importing_inline_program`` must not search that whole frame for the
    mint surface, or an unrelated neighbour's text would satisfy the check.

    The REAL_STDIN_REACH payload is ``import kiro_crew.cli`` -- the smallest program
    that reaches the mint.  A bare ``import kiro_crew`` is not one: the gate is the
    mint surface (``TestInlinePayloadNamesTheMintSurface``), and the bare package
    import reaches nothing (``kiro_crew/__init__`` imports no CLI).  What these
    fixtures pin is the CARRIER walk -- every place the shell can put a program on
    stdin -- which does not depend on the payload rule.
    """

    # Every one of these is read-only or a formatter run, and none carries the mint
    # verb.  The product name appears ONLY as a file path handed to another program.
    BENIGN_NEIGHBOUR = (
        # The report's own case 2, reduced: format two source files, then edit one
        # through a heredoc whose payload does not import anything.
        "isort src/kiro_crew/mcp_core.py\npython3 - <<'PY'\nprint(1)\nPY",
        # The same shape behind the other two separators a frame preserves.
        "isort src/kiro_crew/x.py && python3 -",
        "black src/kiro_crew/security.py; python3 - <<'PY'\nprint(2)\nPY",
        # Order does not matter: the neighbour may follow the interpreter too.
        "python3 - <<PY\nprint(1)\nPY\nisort src/kiro_crew/x.py",
        # A here-string whose payload is harmless, next to a product-named path.
        "isort src/kiro_crew/x.py\npython3 - <<<'print(1)'",
        # A substitution operand whose text is harmless, next to a product-named path.
        "isort src/kiro_crew/x.py\npython3 - <<<$(printf %s 'print(1)')",
        # A stdin redirect belonging to ANOTHER command, with no interpreter in play.
        "isort src/kiro_crew/x.py\ncat < notes.txt",
        # A pipe that does NOT feed this interpreter (it consumes its output).
        "python3 - <<PY\nprint(1)\nPY\n| grep kiro_crew",
    )

    # Every way the shell can put a PROGRAM on a simple command's stdin, at every
    # position it is allowed to appear.  Enumerated from the shell grammar rather than
    # grown one spelling at a time: a partial set covering only the heredoc,
    # here-string and post-program spellings, and every omission was a real bypass.
    # The program is the minimal mint reach, an import of the CLI module.
    REAL_STDIN_REACH = (
        # Heredoc body, in every spelling of the marker.
        "python3 - <<'PY'\nimport kiro_crew.cli\nPY",
        "python3 - <<-PY\nimport kiro_crew.cli\nPY",
        "python3 - << PY\nimport kiro_crew.cli\nPY",
        "python << 'PY'\nimport kiro_crew.cli\nPY",
        # An unterminated heredoc runs to the end of the frame (over-block, not under).
        "python3 - <<PY\nimport kiro_crew.cli\n",
        # A body LINE that merely CONTAINS the tag word is not a closing delimiter:
        # bash closes only on a line holding it ALONE, and line structure does not
        # survive tokenizing, so the body must end at the LAST occurrence of the tag.
        # `# EOF` is an ordinary Python comment and was enough to close it early.
        "python3 - <<EOF\n# EOF\nimport kiro_crew.cli\nEOF",
        "python3 - <<EOF\nx = 1  # EOF\nimport kiro_crew.cli\nEOF",
        "python3 - <<PY\nprint('PY')\nimport kiro_crew.cli\nPY",
        # A command AFTER the closing tag is a NEW command, not this interpreter's
        # script argument -- reading it as one made the detector answer False and
        # skipped the branch entirely, leaving the heredoc payload unscanned.
        "python3 <<PY\nimport kiro_crew.cli\nPY\necho ok",
        "python3 - <<PY\nimport kiro_crew.cli\nPY; echo ok",
        "python3 - <<PY\nimport kiro_crew.cli\nPY && echo ok",
        # HERE-STRING: the operand itself is the program on stdin. `<<<` also starts with
        # `<<`, so reading it as a heredoc made the payload a delimiter and dropped it.
        "python3 - <<<'import kiro_crew.cli'",
        "python3 -<<<'import kiro_crew.cli'",
        "python3 <<<'import kiro_crew.cli'",
        "python3 - <<< 'import kiro_crew.cli'",
        "python3 - <<<$'import kiro_crew.cli'",
        # Pipe producer -- the left side writes this interpreter's stdin.  Every
        # spacing spelling, because the tokenizer splits on whitespace only, so the
        # operator glues into a neighbouring word and `|` is often NOT its own token.
        "echo 'import kiro_crew.cli' | python3 -",
        "echo 'import kiro_crew.cli'|python3 -",
        "echo 'import kiro_crew.cli' |python3 -",
        "echo 'import kiro_crew.cli'| python3 -",
        "cat src/kiro_crew/cli.py | python3 -",
        "cat src/kiro_crew/cli.py|python3 -",
        "printf 'import kiro_crew.cli'|python3",
        "echo 'import kiro_crew.cli' | python3",
        # Stdin redirect -- the file's CONTENT becomes the program.
        "python3 - < src/kiro_crew/cli.py",
        "python3 -<src/kiro_crew/cli.py",
        "python3 - 0< src/kiro_crew/cli.py",
        # Process substitution and command substitution -- the operand is one shell WORD
        # whose text carries whitespace, so it spans tokens to its closing delimiter.
        "python3 - < <(echo 'import kiro_crew.cli')",
        'python3 - <<<$(printf %s "import kiro_crew.cli")',
        "python3 - <<<`printf %s 'import kiro_crew.cli'`",
        'python3 - <<<"${x:-import kiro_crew.cli}"',
        'python3 - < $(printf %s "src/kiro_crew/cli.py")',
        "python3 - <<<$(cat src/kiro_crew/cli.py)",
        # A QUOTED delimiter inside the substitution: quoting is stripped before this
        # code sees the tokens, so balancing the count is not decidable and the operand
        # must span to the LAST closer.
        "python3 - <<<$(true ')'; printf %s \"import kiro_crew.cli\")",
        'python3 - <<<$(echo ")" ; printf %s "import kiro_crew.cli")',
        # A split operand with NO `-`, where the detector must consume the whole operand
        # rather than read the substitution's second token as a script path.
        'python <<< $(printf %s "import kiro_crew.cli")',
        'python3 <<< $(printf %s "import kiro_crew.cli")',
        'python3 < $(printf %s "src/kiro_crew/cli.py")',
        # A redirection may appear ANYWHERE in a simple command, before the program
        # name included.  These are ordinary bash and reach the identical mint.
        "<<'PY' python -\nimport kiro_crew.cli\nPY",
        "<<PY python3 -\nimport kiro_crew.cli\nPY",
        "<src/kiro_crew/cli.py python3 -",
        "< src/kiro_crew/cli.py python3 -",
        "<<<'import kiro_crew.cli' python3 -",
        # ... a marker and its BODY may straddle the program name, so the carrier walk
        # cannot be split per side of the interpreter without losing the association.
        "<<EOF python -\nimport kiro_crew.cli\nEOF",
        "<<EOF python3 -\nimport kiro_crew.cli\nEOF",
        "<< EOF python -\nimport kiro_crew.cli\nEOF",
        # ... including GLUED to the program name with no space at all, which is one
        # single token: `python3<<<'…'`.  Excluding the interpreter's own token from the
        # walk is what missed these.
        'python3<<<"import kiro_crew.cli"',
        "python3<<<'import kiro_crew.cli'",
        "python3<src/kiro_crew/cli.py",
        "python3<<PY\nimport kiro_crew.cli\nPY",
        "python3<<-PY\nimport kiro_crew.cli\nPY",
        "python<<<'import kiro_crew.cli'",
        "python<<EOF\nimport kiro_crew.cli\nEOF",
        "python3<<EOF\nimport kiro_crew.cli\nEOF",
    )

    def test_benign_neighbour_no_longer_reads_as_a_mint(self):
        from kiro_crew import security

        for cmd in self.BENIGN_NEIGHBOUR:
            assert not security._is_credential_mint(cmd.lower()), f"frame contamination: {cmd!r}"
            assert security.is_denied(cmd) is None, f"frame contamination: {cmd!r}"

    def test_real_stdin_reach_stays_denied(self):
        from kiro_crew import security

        for cmd in self.REAL_STDIN_REACH:
            assert security.is_denied(cmd) is not None, f"stdin reach not blocked: {cmd!r}"

    def test_carriers_are_the_only_search_space(self):
        """The helper yields program text and nothing else.

        Asserted on the helper directly, so the SCOPE is pinned rather than only its
        effect on one deny verdict.
        """
        from kiro_crew import security

        tokens = security.normalize_shell_command(
            "isort src/kiro_crew/mcp_core.py\npython3 - <<'PY'\nprint(1)\nPY"
        )
        i = tokens.index("python3")
        assert list(security._stdin_program_text(tokens, i)) == ["print(1)"]

        piped = security.normalize_shell_command("echo 'import kiro_crew' | python3 -")
        j = piped.index("python3")
        assert "import kiro_crew" in list(security._stdin_program_text(piped, j))

    def test_bare_interpreter_with_a_heredoc_is_recognised_as_reading_stdin(self):
        """``python << 'PY' … PY`` (no ``-``) really does read its program from stdin.

        ``_python_reads_stdin`` classified this FALSE: it consulted
        ``_normalize_operand``, which strips a redirection to the empty string, so its
        heredoc branch was unreachable and the first word of the BODY read as a script
        path.  The form was denied anyway, but only by accident -- the closing tag
        ``PY`` matched ``_PYTHON_PROGRAM_RE`` and the old frame-wide scan then found
        the import anywhere in the frame.  Once the scan is scoped to real carriers
        that accident stops covering it, so the detector has to be right.
        """
        from kiro_crew import security

        for cmd, expect_stdin in (
            ("python << 'PY'\nimport kiro_crew\nPY", True),
            ("python <<PY\nimport kiro_crew\nPY", True),
            ("python <<-PY\nimport kiro_crew\nPY", True),
            ("python <<<'import kiro_crew'", True),
            ("python <<< 'import kiro_crew'", True),
            ('python <<< $(printf %s "import kiro_crew")', True),
            ("python < prog.py", True),
            ("python script.py", False),
            ("python script.py < input.txt", False),
            ("python -c 'print(1)'", False),
            ("python -m kiro_crew gateway", False),
        ):
            frame = security.normalize_shell_command(cmd)
            i = next(
                k
                for k, t in enumerate(frame)
                if security._PYTHON_PROGRAM_RE.match(security._program_basename(t.lower()))
            )
            assert security._python_reads_stdin(frame[i + 1 :]) is expect_stdin, cmd

    def test_a_pipe_anywhere_left_is_a_known_over_block(self):
        """The producer branch over-yields on a pipe that does not feed the interpreter.

        ``a | b; python -`` pipes into ``b``, not into the interpreter, yet the whole
        left side is still treated as program text.  Pinned as a KNOWN over-block
        rather than tightened: the alternative -- requiring the pipe to be adjacent --
        is what let all four no-space spellings through, because the tokenizer glues
        the operator into a neighbouring word.  A missed producer is a bypass; an extra
        token is a visible refusal.  If this assertion ever flips, the tightening that
        did it must be checked against the no-space spellings above.
        """
        from kiro_crew import security

        # The over-yield is still there: the left side of the pipe is handed over as
        # program text ...
        frame = security.normalize_shell_command("grep kiro_crew.cli src | head; python3 -")
        i = frame.index("python3")
        assert any("kiro_crew.cli" in t for t in security._stdin_program_text(frame, i))
        # ... and when that text names the mint surface the command is denied.
        assert security.is_denied("grep kiro_crew.cli src | head; python3 -") is not None
        # A left side that only MENTIONS the package is not a reach, so the same
        # over-yield costs nothing: the payload gate asks for the mint surface,
        # not for the package name.
        assert security.is_denied("grep kiro_crew src | head; python3 -") is None

    def test_rule_does_not_fire_on_its_own_pattern_text(self):
        """Quoting this rule must not trip it.

        ``credential-exfil-kirocrew-token``'s code comment claims this exemption
        ("a regex LITERAL quoting this very rule ... from reading as a mint"), and
        This pins that the exemption holds in practice, so discussing,
        documenting or testing the rule by quoting it stays possible.
        """
        from kiro_crew import security

        rule = next(
            r for r in security.BUILTIN_DENIED_RULES if r.id == "credential-exfil-kirocrew-token"
        )
        for cmd in (
            f'grep -n "{rule.pattern}" notes.txt',
            f"echo {rule.pattern!r} >> notes.txt",
        ):
            assert security.is_denied(cmd) is None, f"rule fires on its own text: {cmd!r}"


class TestPythonStdinDetectorStepsOverOutputRedirects:
    """An OUTPUT redirect must not be mistaken for the interpreter's script path.

    ``_python_reads_stdin`` decides whether a ``python`` invocation takes its PROGRAM
    from stdin, and the credential-mint floor uses that to know whether to scan the
    stdin carriers (here-string, heredoc, redirect, pipe producer) for a payload that
    imports our CLI.  It read the raw token for ``<`` and for heredocs but had no branch
    for the ``>`` family at all, so those tokens fell through to "a positional that is
    not ``-`` is a script path" and the answer became False.

    The unnumbered glued form survived by accident: ``_normalize_operand`` reduces
    ``>out.txt`` to the empty string and the loop skips empties.  ``2>&1`` reduces to
    ``2`` -- a perfectly good file name -- so the interpreter looked like it was running
    a script called ``2``, and the program on its stdin went unscanned.  Eight spellings
    reached the floor that way, verified against bash to actually run the here-string:

        python 2>&1 <<< '<program>'          python 2>> log <<< '<program>'
        python 1>&2 <<< '<program>'          python >& out <<< '<program>'
        python 2> /dev/null <<< '<program>'  python 3>&1 <<< '<program>'
        python > out.txt <<< '<program>'     python <<< '<program>' 2>&1

    The last one is worth its own note: the here-string is consumed correctly there, and
    a redirect AFTER it still flipped the verdict, because the walk continues past the
    carrier and met the leftover ``2``.  So this was not only about redirects preceding
    the payload.
    """

    # Program-on-stdin shapes: True. Bash was measured for each -- every one runs the
    # here-string program.
    READS_STDIN: "list[list[str]]" = [
        ["2>&1", "<<<", "prog"],
        ["1>&2", "<<<", "prog"],
        ["2>", "/dev/null", "<<<", "prog"],
        [">", "out.txt", "<<<", "prog"],
        [">out.txt", "<<<", "prog"],
        ["2>>", "log", "<<<", "prog"],
        [">&", "out", "<<<", "prog"],
        ["3>&1", "<<<", "prog"],
        ["&>/dev/null", "<<<", "prog"],
        ["&>>", "log", "<<<", "prog"],
        ["12>&1", "<<<", "prog"],
        ["2>&-", "<<<", "prog"],
        ["2>&1-", "<<<", "prog"],
        # The noclobber override and the {name} automatic descriptor (bash 4.1+).
        # Measured in bash 5.2: every one runs the here-string.
        ["2>|", "/dev/null", "<<<", "prog"],
        ["2>|/dev/null", "<<<", "prog"],
        [">|", "f", "<<<", "prog"],
        ["{fd}>", "f", "<<<", "prog"],
        ["{fd}>f", "<<<", "prog"],
        ["{fd}>&1", "<<<", "prog"],
        ["{fd}>>", "f", "<<<", "prog"],
        ["{fd}>|", "f", "<<<", "prog"],
        # A following operator glued into the SAME word starts a new redirect, so the
        # target must stop there. Taking all of `/dev/null<<EOF` as the target swallows
        # the heredoc marker and loses the program on stdin. Measured in bash: both run.
        ["2>/dev/null<<EOF", "prog", "EOF"],
        [">out<<EOF", "prog", "EOF"],
        ["2>&1<<<prog"],
        ["2>/dev/null<<<prog"],
        ["2>>log<<<prog"],
        ["&>/dev/null<<<prog"],
        ["{fd}>f<<<prog"],
        ["2>a>b<<<prog"],
        # A redirect INSIDE a substitution belongs to that inner command and is not a
        # boundary of this word: after the shell runs it, `2>$(printf /dev/null)` is just
        # `2>/dev/null`. Measured in bash: all of these run the here-string.
        ["2>$(echo>/dev/null;printf", "/dev/null)", "<<<", "prog"],
        ["2>`echo>/dev/null;printf", "/dev/null`", "<<<", "prog"],
        [">$(echo>x;printf", "out)", "<<<", "prog"],
        ["2>${x:-/dev/null}", "<<<", "prog"],
        ["2>$(printf", "/dev/null)", "<<<", "prog"],
        # A subshell or brace group NESTED in the substitution closes with its own `)`
        # or `}`. Depth must count those too, and the word must reach the scan with its
        # delimiters intact -- the tokenizer splits on the space, so this arrives as the
        # word `2>$(`, and `_SHELL_WRAPPER_CHARS` would otherwise strip the opener off.
        ["2>$(", "(true);", "printf", "/dev/null)", "<<<", "prog"],
        ["2>$(", "(true)", ";", "printf", "/dev/null", ")", "<<<", "prog"],
        ["2>$(", "{", "true;", "printf", "/dev/null;", "}", ")", "<<<", "prog"],
        # PowerShell's all-streams redirect. Included on the floor's fail-closed rule:
        # under PowerShell `*>` is the operator and the program arrives on stdin, while
        # under bash `*` is a glob whose first match becomes the script. Answering True
        # over-triggers under bash and under-triggers under neither.
        ["*>", "token.txt", "<<<", "prog"],
        ["*>>", "token.txt", "<<<", "prog"],
        ["*>token.txt", "<<<", "prog"],
        # zsh's `!` noclobber override, the third modifier in the set. Measured with real
        # zsh: `python >! out <<< '<program>'` runs the here-string.
        [">!", "out", "<<<", "prog"],
        [">>!", "out", "<<<", "prog"],
        ["2>!", "out", "<<<", "prog"],
        ["&>!", "out", "<<<", "prog"],
        ["2>>!", "out", "<<<", "prog"],
        [">!out", "<<<", "prog"],
        # A redirect needs no whitespace in front of it, so it can ride on the back of a
        # FLAG. Measured in bash: `python -u> out <<< '<program>'` runs the here-string.
        ["-u>", "/dev/null", "<<<", "prog"],
        ["-u>/dev/null", "<<<", "prog"],
        ["-B>", "out", "<<<", "prog"],
        ["-u2>", "err", "<<<", "prog"],
        ["-u>>", "out", "<<<", "prog"],
        ["-u>!", "out", "<<<", "prog"],
        ["2>&1", "1>&2", "<<<", "prog"],
        ["<<<", "prog", "2>&1"],
        ["-u", "2>&1", "<<<", "prog"],
        ["2>&1", "-u", "<<<", "prog"],
        # No carrier at all: a bare interpreter still reads its program from stdin.
        ["2>&1"],
        ["2>&1", "-"],
        # The redirect TARGET must be consumed, not run: `python 2> script.py`
        # redirects into that file and still reads its program from stdin.
        ["2>", "script.py"],
        [">", "script.py"],
        ["2>script.py"],
    ]

    # The program comes from somewhere else: False, redirect or no redirect.
    SUPPLIES_PROGRAM_ELSEWHERE: "list[list[str]]" = [
        ["2>&1", "script.py"],
        ["script.py", "2>&1"],
        ["2>", "/dev/null", "script.py"],
        [">", "out.txt", "script.py"],
        ["2>&1", "-c", "code"],
        ["-c", "code", "2>&1"],
        ["2>&1", "-m", "mod"],
        ["-m", "mod", "2>&1"],
        # Measured in bash: after these redirects a real script still supplies the
        # program, so stepping over the redirect must not mean ignoring what follows.
        ["2>|", "f", "script.py"],
        ["{fd}>&1", "script.py"],
        ["{fd}>", "f", "script.py"],
        # A redirect glued to a POSITIONAL: the script still supplies the program, so the
        # word must be split and its prefix classified rather than skipped. Measured in
        # bash: `python script.py> out <<< '<program>'` runs the script.
        ["script.py>", "out"],
        ["script.py>out"],
        ["-c>", "out", "code"],
    ]

    def test_the_glue_point_is_only_a_trailing_redirect(self):
        """None when the word has no `>`, or already starts with one -- a leading file
        descriptor belongs to the redirect, and the shell reads digits as an fd only when
        they are the whole prefix (`2>err` is fd 2; `x2>err` is the word `x2`)."""
        from kiro_crew import security

        assert security._redirect_glue_point("-u>") == 2
        assert security._redirect_glue_point("-u>/dev/null") == 2
        assert security._redirect_glue_point("-u2>err") == 3
        assert security._redirect_glue_point("script.py>out") == 9
        for token in (">out", "2>err", "&>f", "*>f", "{fd}>f", "-u", "script.py", ""):
            assert security._redirect_glue_point(token) is None, token

    def test_a_brace_expansion_is_not_read_as_a_descriptor(self):
        """``{fd}>`` is an automatic descriptor; ``{a,b}`` is a brace EXPANSION the shell
        resolves before redirect parsing. Only an identifier may sit in the braces, or an
        ordinary argument could be swallowed as a redirect."""
        from kiro_crew import security

        assert security._output_redirect_scan("{fd}>&1") == ("1", 7)
        assert security._output_redirect_scan("{fd}>") == ("", 5)
        for token in ("{a,b}>x", "{1..3}>x", "{}>x", "{a b}>x"):
            assert security._output_redirect_scan(token) is None, token

    def test_a_program_on_stdin_is_detected_through_an_output_redirect(self):
        from kiro_crew import security

        for tokens in self.READS_STDIN:
            assert security._python_reads_stdin(list(tokens)) is True, tokens

    def test_a_script_or_inline_program_still_wins(self):
        from kiro_crew import security

        for tokens in self.SUPPLIES_PROGRAM_ELSEWHERE:
            assert security._python_reads_stdin(list(tokens)) is False, tokens

    def test_the_redirect_helper_reports_target_and_end_position(self):
        """Three distinct answers. A glued target ends at the word's end; an empty target
        at the word's end means the target is the NEXT token; an end short of the word
        means another operator followed and must be re-examined, not eaten."""
        from kiro_crew import security

        assert security._output_redirect_scan("2>&1") == ("1", 4)
        assert security._output_redirect_scan(">out.txt") == ("out.txt", 8)
        assert security._output_redirect_scan("&>/dev/null") == ("/dev/null", 11)
        assert security._output_redirect_scan("2>") == ("", 2)
        assert security._output_redirect_scan(">&") == ("", 2)
        assert security._output_redirect_scan("2>>") == ("", 3)
        # An end short of len() is where the glued-heredoc bypass lived.
        assert security._output_redirect_scan("2>/dev/null<<EOF") == ("/dev/null", 11)
        assert security._output_redirect_scan("2>&1<f") == ("1", 4)
        assert security._output_redirect_scan(">a>b") == ("a", 2)
        assert security._output_redirect_scan("2></dev/null") == ("", 2)
        # Scanning from an offset is how a chain is walked in one pass.
        assert security._output_redirect_scan(">a>b", 2) == ("b", 4)
        # A redirect is a boundary only at substitution depth ZERO. Inside `$(...)`,
        # `${...}` or backticks it belongs to the inner command, and cutting there left
        # the tail of the substitution to be read as a script path.
        assert security._output_redirect_scan("2>$(echo>/dev/null;printf") == (
            "$(echo>/dev/null;printf",
            25,
        )
        assert security._output_redirect_scan("2>`echo>x`") == ("`echo>x`", 10)
        assert security._output_redirect_scan("2>${x:->}") == ("${x:->}", 9)
        # Depth counts EVERY opener, not just a `$`-prefixed one: a nested subshell
        # closes with its own `)`, and ignoring it drops the depth to zero early.
        assert security._output_redirect_scan("2>$( (x)>y )") == ("$( (x)>y )", 12)
        assert security._output_redirect_scan("2>$(") == ("$(", 4)
        # PowerShell's all-streams descriptor, and the glob spellings it must NOT eat.
        assert security._output_redirect_scan("*>") == ("", 2)
        assert security._output_redirect_scan("*>>") == ("", 3)
        assert security._output_redirect_scan("*>token.txt") == ("token.txt", 11)
        for token in ("*", "*.py", "*.txt"):
            assert security._output_redirect_scan(token) is None, token

    def test_the_descriptor_and_modifier_sets_are_the_enumerated_ones(self):
        """The two sets are enumerated from the shells' grammars, not grown one spelling
        at a time. Asserted here so the boundary is a test rather than a comment:
        descriptors are digits, ``&``, ``{name}`` and ``*``; modifiers are ``&``, ``|``
        and ``!``."""
        from kiro_crew import security

        for descriptor in ("", "2", "12", "&", "*", "{fd}"):
            for operator in (">", ">>"):
                for modifier in ("", "&", "|", "!"):
                    token = f"{descriptor}{operator}{modifier}"
                    assert security._output_redirect_scan(token) is not None, token
        # A modifier outside the set is part of the TARGET, not the operator.
        assert security._output_redirect_scan(">?x") == ("?x", 3)
        assert security._output_redirect_scan(">^x") == ("^x", 3)
        # ...and the boundary still applies once the substitution has closed.
        assert security._output_redirect_scan("2>$(printf x)>b") == ("$(printf x)", 13)
        # Not output redirects, and must not be swallowed as such.
        for token in ("script.py", "-u", "-", "<<<", "<<PY", "<f", "2", "", "-c"):
            assert security._output_redirect_scan(token) is None, token

    def test_a_chain_of_glued_redirects_is_linear(self, monkeypatch):
        """One word may hold many operators (``>a>a>a...``). Re-slicing the word per
        operator was quadratic in its length -- on a floor that runs for every command,
        and in a module that pins linearity elsewhere, so it is pinned here too.

        Asserted DETERMINISTICALLY, not by timing: a doubling ratio false-reds on a
        starved shared runner whose scheduler noise exceeds the ratio's slack (see
        ``test_the_scan_is_linear_not_quadratic`` for the observed case), so what
        makes the walk linear is asserted structurally instead. The contract is
        that a chain word is walked ONCE, IN PLACE: ``_output_redirect_scan`` returns
        an index precisely so the caller can advance through the same string rather
        than re-slice it. A regression has to break one of these:

          1. ONE SCAN PER OPERATOR -- the ``_output_redirect_scan`` invocation
             count grows as an exact arithmetic progression in the chain length
             (equal size steps produce equal call increments; per-operator
             re-injection or retry work makes the increments themselves grow);
          2. THE FULL WORD EVERY TIME -- every invocation receives a string of the
             chain word's full length. Re-slicing the remainder per operator (the
             quadratic) hands the scan progressively shorter COPIES, each of which
             costs the slice that made it;
          3. THE START INDEX ADVANCES -- strictly increasing within the word, never
             reset to 0, so each character is visited once.

        The absolute budget stays as the machine-independent catastrophic-blowup
        backstop for cost added outside the scan, where the trace cannot see it.
        """
        import time

        from kiro_crew import security

        # Backstop budget, measured BEFORE instrumenting (the tracing wrapper below
        # would bill its own overhead against it). The input is built OUTSIDE the
        # timed window, and one untimed small-size call warms the path first, so
        # first-call cost is not billed against the budget when this test runs
        # alone.
        security._python_reads_stdin([">a" * 200, "<<<", "prog"])
        tokens = [">a" * 1600, "<<<", "prog"]
        start = time.perf_counter()
        assert security._python_reads_stdin(tokens) is True
        large = time.perf_counter() - start
        # flake-ok: pre-existing 0.2 s backstop over a warmed path; the linearity is pinned by the traced scan starts below; moved verbatim, tracked for a de-flake pass
        assert large < 0.2, f"1600 glued redirects took {large:.4f}s"

        real_scan = security._output_redirect_scan
        trace: "list[tuple[int, int]]" = []  # (len(raw), start)

        def tracing_scan(raw: str, start: int = 0) -> "tuple[str, int] | None":
            trace.append((len(raw), start))
            return real_scan(raw, start)

        monkeypatch.setattr(security, "_output_redirect_scan", tracing_scan)

        def walked(k: int) -> "list[tuple[int, int]]":
            trace.clear()
            word = ">a" * k
            assert security._python_reads_stdin([word, "<<<", "prog"]) is True
            return list(trace)

        sizes = (400, 800, 1200)
        walks = [walked(k) for k in sizes]

        # (1) One scan per operator: equal size steps, equal call increments.
        # The progression form (rather than exact doubling) is deliberately
        # immune to a constant per-word offset, so a benign refactor that adds
        # one trailing probe call does not false-red this test.
        calls = [len(w) for w in walks]
        assert calls[0] >= sizes[0], (
            "the instrument is not observing the path under test -- fewer scans "
            "than operators means the chain was never walked"
        )
        assert calls[1] - calls[0] == calls[2] - calls[1], (
            f"scan counts {calls} for chain sizes {sizes} are not an arithmetic "
            "progression -- per-operator work that scales with the chain is the "
            "re-slicing quadratic the in-place walk exists to prevent"
        )

        # (2) + (3) The walk is in place: every scan sees the FULL word and the
        # start index only ever advances.
        for walk, k in zip(walks, sizes):
            word_len = len(">a" * k)
            assert {length for length, _ in walk} == {word_len}, (
                "a scan received a string shorter than the chain word -- the "
                "remainder is being re-sliced per operator, which is quadratic "
                "in the word's length"
            )
            starts = [position for _, position in walk]
            assert all(a < b for a, b in zip(starts, starts[1:])), (
                "the scan's start index went backwards or repeated -- the walk "
                "restarted inside the word instead of advancing through it once"
            )

    def test_the_floor_denies_the_stdin_program_behind_a_redirect(self):
        """The end-to-end property: these are credential-mint attempts whose program
        rides in on stdin, and each was ALLOWED before this change."""
        from kiro_crew import security

        payload = "from kiro_crew.cli import main; main()"
        for cmd in (
            f"python 2>&1 <<< '{payload}'",
            f"python 1>&2 <<< '{payload}'",
            f"python 2> /dev/null <<< '{payload}'",
            f"python > out.txt <<< '{payload}'",
            f"python 2>> log <<< '{payload}'",
            f"python >& out <<< '{payload}'",
            f"python 3>&1 <<< '{payload}'",
            f"python 2>&1 1>&2 <<< '{payload}'",
            f"python <<< '{payload}' 2>&1",
            f"echo '{payload}' | python 2>&1",
            f"python 2>&1 << 'PY'\n{payload}\nPY",
            # Measured in bash 5.2.
            f"python 2>| /dev/null <<< '{payload}'",
            f"python >| out <<< '{payload}'",
            f"python {{fd}}>&1 <<< '{payload}'",
            f"python {{fd}}> out <<< '{payload}'",
            # Glued mixed operators, measured in bash.
            f"python 2>/dev/null<<EOF\n{payload}\nEOF",
            f"python >out<<EOF\n{payload}\nEOF",
            f"python 2>&1<<<'{payload}'",
            # A redirect nested in a substitution, measured in bash.
            f"python 2>$(echo>/dev/null;printf /dev/null) <<< '{payload}'",
            f"python 2>`echo>/dev/null;printf /dev/null` <<< '{payload}'",
            f"python 2>$( (true); printf /dev/null) <<< '{payload}'",
            f"python 2>$( {{ true; printf /dev/null; }} ) <<< '{payload}'",
            # PowerShell's all-streams redirect with the program on a pipe.
            f"echo '{payload}' | python *> token.txt",
            f"echo '{payload}' | python *>> token.txt",
            # A redirect glued to a flag, measured in bash.
            f"python -u> /dev/null <<< '{payload}'",
            f"python -B> out <<< '{payload}'",
        ):
            assert security._is_credential_mint(cmd.lower()), cmd

    def test_the_floor_still_allows_the_ordinary_shapes(self):
        from kiro_crew import security

        payload = "from kiro_crew.cli import main; main()"
        for cmd in (
            "python script.py",
            f"python script.py <<< '{payload}'",
            "python -m json.tool",
            "python 2>&1 script.py",
            "ls -la 2>&1",
            "pytest test/test_x.py 2>&1 | tail -5",
            # Reading `*>` as a redirect must not start denying ordinary commands: with
            # no payload-bearing carrier there is nothing for the floor to fire on.
            "python *> out",
            "python *.py > out",
            "pytest tests/ *> out",
        ):
            assert not security._is_credential_mint(cmd.lower()), cmd


class TestOutputRedirectScanQuoting:
    """A bare opener in a redirect target is not grammar.

    ``_output_redirect_scan``'s span walk counted every ``(``/``{`` as a depth
    opener. The tokenizer that feeds it resolves quoting, so a QUOTED ``(`` --
    one filename character to bash -- arrived bare, opened a span that never
    closed, and the target ran past the ``<<<``/``<<`` that should have ended
    it; a bare ``{`` needs no quoting at all. The stdin program then went
    unscanned -- the same consequence the scan's own docstring describes for a
    glued heredoc marker. The rule that closes
    it: at depth zero only a ``$``-prefixed opener starts a substitution span.
    Quote characters that reach the scan are DATA (the tokenizer already
    resolved quoting), so the walk must not read them as grammar either --
    the inverse defect.
    """

    def test_the_quoted_paren_bypass_is_denied_end_to_end(self):
        """The public consequence, through the production tokenizer (which strips
        the quotes; the scan sees ``2>a)(b<<<``). Measured in bash: each spelling
        runs the here-string program. On the uncorrected walk ``is_denied``
        returned None for all three."""
        from kiro_crew import security

        prog = "'from kiro_crew.cli import main; main()'"
        assert security.is_denied(f"python3 2>'a)(b'<<< {prog}") is not None
        assert security.is_denied(f"python3 2>a{{b<<<{prog}") is not None
        assert security.is_denied(f'python3 > "a{{b" <<< {prog}') is not None
        # The balanced spelling was already denied and must stay denied.
        assert security.is_denied(f"python3 > ab <<< {prog}") is not None

    def test_a_bare_opener_in_a_dequoted_target_is_not_a_delimiter(self):
        """The walk's own boundary, on the de-quoted form the production path
        hands it. An unquoted bare ``(`` cannot reach execution (bash syntax
        error), and a bare ``{`` is an ordinary filename character (measured:
        ``python 2>a{b<<<'<program>'`` runs the program), so neither may hold
        the span open past the operator."""
        from kiro_crew import security

        assert security._output_redirect_scan("2>a)(b<<<") == ("a)(b", 6)
        assert security._output_redirect_scan("2>a{b<<<x") == ("a{b", 5)
        # `$((arith))` still spans: the opener is `$`-prefixed.
        assert security._output_redirect_scan("2>$((1+2))<<<x") == ("$((1+2))", 10)

    def test_a_quoted_opener_does_not_swallow_the_stdin_operator(self):
        """Positive controls on quote-bearing text -- the quote characters are
        DATA the walk steps over, and the parens inside them are bare, so the
        depth-zero rule ends the target at the operator. Each boundary here
        reached past the operator on the unfixed walk. Measured in bash:
        ``python3 > 'a(b' <<< '<program>'`` creates the file ``a(b`` and RUNS
        the here-string program (and the glued ``2>'a)(b'<<<'<program>'``
        likewise), so the target must end before the operator, where bash ends
        it."""
        from kiro_crew import security

        # The measured case: the end stops at 8, before the `<<<` at 9.
        assert security._output_redirect_scan("> 'a(b' <<< payload") == (" 'a(b' ", 8)
        assert security._output_redirect_scan('> "a{b" <<< payload') == (' "a{b" ', 8)
        # Glued heredoc after a quoted opener: the end stops at 7, before the marker.
        assert security._output_redirect_scan("2>'a(b'<<EOF") == ("'a(b'", 7)
        assert security._output_redirect_scan('2>"a{b"<<EOF') == ('"a{b"', 7)
        # An ESCAPED delimiter is one filename character too (`a\(b` is `a(b`).
        assert security._output_redirect_scan("2>a\\(b<<<x") == ("a\\(b", 6)
        # A quoted `)` at depth zero plus a quoted `(`: the unquoted walk opened a
        # span that never closed and ran the target to the end of the text.
        assert security._output_redirect_scan("2>'a)(b'<<<x") == ("'a)(b'", 8)
        # ANSI-C: `$'` is not `$(`/`${`, so nothing opens and the paren is data.
        assert security._output_redirect_scan("2>$'a(b'<<<x") == ("$'a(b'", 8)
        assert security._output_redirect_scan("2>$'a\\')'<<<x") == ("$'a\\')'", 9)

    def test_a_data_quote_is_not_read_as_grammar(self):
        """The inverse direction: the
        tokenizer resolves quoting, so a quote character that SURVIVES it is
        literal filename text (``2>"a'b"`` tokenizes to ``2>a'b``). A quoting
        state opened on that data quote consumed the ``<<<`` to the end of the
        text and hid the operator -- measured in bash, the spelling runs the
        program, so the boundary must stay at the operator. The same rule keeps
        the PID-parameter spelling (``$$'`` is not ANSI-C) and a quote inside
        backticks (bash tolerates it unterminated there) at their pre-fix
        boundaries."""
        from kiro_crew import security

        assert security._output_redirect_scan("2>a'b<<<x") == ("a'b", 5)
        assert security._python_reads_stdin(["2>a'b<<<", "'prog'"]) is True
        assert security._output_redirect_scan("2>$$'\\'<<<X") == ("$$'\\'", 7)
        assert security._output_redirect_scan("2>`'`a<<<X") == ("`'`a", 6)

    def test_the_unmoved_boundaries_do_not_move(self):
        """Inverse controls whose boundaries the quote handling must not move: a real
        substitution span still holds the walk open, a real backtick region
        still toggles, and an unterminated quote or trailing backslash is
        ordinary data to the end of the text."""
        from kiro_crew import security

        assert security._output_redirect_scan("2>$( (x)>y )") == ("$( (x)>y )", 12)
        assert security._output_redirect_scan("2>`echo>x`") == ("`echo>x`", 10)
        assert security._output_redirect_scan("2>${x:->}") == ("${x:->}", 9)
        assert security._output_redirect_scan("2>'a(b") == ("'a(b", 6)
        assert security._output_redirect_scan("2>\\") == ("\\", 3)

    def test_the_stdin_program_is_detected_through_a_quoted_target(self):
        """The detector-level consequence, on quote-bearing tokens and on the
        de-quoted tokens the production frame actually carries: with the target
        absorbing the glued ``<<<``, ``_python_reads_stdin`` answered False and
        the program on stdin went unscanned."""
        from kiro_crew import security

        assert security._python_reads_stdin(["2>'a)(b'<<<", "'prog'"]) is True
        assert security._python_reads_stdin(["2>'a(b'<<<", "'prog'"]) is True
        assert security._python_reads_stdin(['2>"a{b"<<<', "'prog'"]) is True
        # De-quoted, as `_self_token_frames` hands them over.
        assert security._python_reads_stdin(["2>a)(b<<<", "prog"]) is True
        assert security._python_reads_stdin(["2>a{b<<<", "prog"]) is True
