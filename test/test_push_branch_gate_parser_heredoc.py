"""Heredoc bodies inside a substitution span, pinned against the rm floor.

Split out of ``test_push_branch_gate_parser.py`` (2,000-line cap). Each test here
is a way bash ends -- or does not end -- a heredoc body inside ``$(...)``: an
unquoted body's ``\\``-newline fold, and a body line that starts with a
delimiter and carries the span's ``)``. The span reader does not parse heredocs;
a span a heredoc body starts inside is read as ``main`` reads it, so every
home/root row is refused as on ``main`` and the ``/tmp`` twins stay allowed.
"""

from kiro_crew import security


class TestAHeredocBodyCannotMaskAHomeRm:
    """A heredoc body inside a substitution cannot hide the ``rm`` operand after
    the span."""

    def test_an_unquoted_heredoc_line_continuation_cannot_mask_a_home_rm(self) -> None:
        """An unquoted heredoc body line ending in a backslash joins the next line
        before bash matches the terminator. ``main`` refuses every home/root row; the
        rm floor reads a span a heredoc body starts inside as ``main`` does. The
        quoted-delimiter ``/tmp`` twins stay allowed."""
        for home in ("~", "$HOME", "${HOME}", "/"):
            cmd = "rm -r -f \"$(cat <<E\n'\\\nE\n'\nE\n)\" " + home + ' ; : "\'\n)"'
            assert security.is_denied(cmd) is not None, cmd
        # A quoted delimiter suppresses line continuation (allowed, as on main).
        for delim in ("'E'", '"E"', "\\E"):
            cmd = 'rm -r -f "$(cat <<' + delim + '\n\'\\\nE\n)" /tmp/keep ; : "\'\n)"'
            assert security.is_denied(cmd) is None, cmd

    def test_a_glued_close_is_measured_on_the_physical_body_line(self) -> None:
        """A body line ``E)`` glued to the span's ``)`` that ends in a continuation
        backslash: bash closes at that physical ``)``. ``main`` refuses every
        home/root row; the rm floor reads a span a heredoc body starts inside as
        ``main`` does."""
        nl = "\n"
        for home in ("~", "$HOME", "${HOME}", "/"):
            for mid in ("", "a", "abc", "a\\" + nl + "bc"):
                cmd = (
                    'rm -r -f "$(cat <<E'
                    + nl
                    + "E)\\"
                    + nl
                    + mid
                    + '" '
                    + home
                    + ' ; : "'
                    + nl
                    + ')"'
                )
                assert security.is_denied(cmd) is not None, cmd
            # tab-stripped <<-E form of the same glued-close shape
            cmd = (
                'rm -r -f "$(cat <<-E' + nl + "\tE)\\" + nl + 'abc" ' + home + ' ; : "' + nl + ')"'
            )
            assert security.is_denied(cmd) is not None, cmd

    def test_a_glued_close_reached_through_a_fold_is_a_real_close(self) -> None:
        """A body line ``E\\`` then ``)`` folds to ``E)`` and closes the substitution
        (bash 5.2 runs ``rm -r -f "" ~``). ``main`` refuses every home/root row; the
        rm floor reads a span a heredoc body starts inside as ``main`` does. The
        ``/tmp`` twin stays allowed."""
        nl = "\n"
        tail = ' ; : "' + nl + "E" + nl + ')"'
        for home in ("~", "$HOME", "${HOME}", "/"):
            for body in ("E\\" + nl + ")", "E\\" + nl + "\\" + nl + ")"):
                cmd = 'rm -r -f "$(cat <<E' + nl + body + '" ' + home + tail
                assert security.is_denied(cmd) is not None, cmd
            for body in ("\tE\\" + nl + ")", "\tE\\" + nl + "\t)"):
                cmd = 'rm -r -f "$(cat <<-E' + nl + body + '" ' + home + tail
                assert security.is_denied(cmd) is not None, cmd
        cmd = 'rm -r -f "$(cat <<E' + nl + "E\\" + nl + ')" /tmp/keep' + tail
        assert security.is_denied(cmd) is None, cmd

    def test_a_body_line_that_starts_with_the_delimiter_closes_the_span(self) -> None:
        """Inside ``$(...)`` bash ends a heredoc body at a line that STARTS with the
        delimiter and carries the span's ``)`` (``E )``, ``Ex)``, ``EOFX)``).
        ``main`` refuses every home/root row; the rm floor reads a span a heredoc
        body starts inside as ``main`` does. The ``/tmp`` twins stay allowed."""
        nl = "\n"
        tail = ' ; : "' + nl + "E" + nl + ')"'
        etail = ' ; : "' + nl + "EOF" + nl + ')"'
        for home in ("~", "$HOME", "${HOME}", "/"):
            # delimiter word, then separated text, then the span's ``)``
            for last in ("E )", "E\t)", "Ex)", "E foo)"):
                cmd = 'rm -r -f "$(cat <<E' + nl + last + '" ' + home + tail
                assert security.is_denied(cmd) is not None, cmd
            # a longer delimiter with trailing text before the ``)``
            cmd = 'rm -r -f "$(cat <<EOF' + nl + 'EOFX)" ' + home + etail
            assert security.is_denied(cmd) is not None, cmd
            # ``<<-`` strips the leading tab of the delimiter-start line too
            cmd = 'rm -r -f "$(cat <<-E' + nl + '\tE )" ' + home + nl + "\tE" + nl + ')"'
            assert security.is_denied(cmd) is not None, cmd
            # folded: ``E\`` then ``x)`` / three continuations then `` )``
            cmd = 'rm -r -f "$(cat <<E' + nl + "E\\" + nl + 'x)" ' + home + tail
            assert security.is_denied(cmd) is not None, cmd
            cmd = (
                'rm -r -f "$(cat <<E'
                + nl
                + "E\\"
                + nl
                + "x\\"
                + nl
                + "y\\"
                + nl
                + ' )" '
                + home
                + tail
            )
            assert security.is_denied(cmd) is not None, cmd
        # a line that starts with the delimiter but has NO ``)`` stays body
        cmd = 'rm -r -f "$(cat <<E' + nl + "Ebody" + nl + "E" + nl + ')" ~'
        assert security.is_denied(cmd) is not None, cmd  # operand read, home refused
        # the ``/tmp`` twins stay allowed, as on main
        for last in ("E )", "Ex)"):
            cmd = 'rm -r -f "$(cat <<E' + nl + last + '" /tmp/x' + tail
            assert security.is_denied(cmd) is None, cmd

    def test_an_earlier_body_line_that_starts_with_its_delimiter_closes_the_span(
        self,
    ) -> None:
        """With several heredocs pending (``cat <<E <<F``) bash ends an EARLIER body the
        same way. ``main`` refuses every home/root row; the rm floor reads a span a
        heredoc body starts inside as ``main`` does. The ``/tmp`` twins stay allowed."""
        nl, tab = "\n", "\t"
        cases = (
            ("<<E <<F", ["E {t}", "E", "F"]),
            ("<<E <<F", ["Ex{t}", "E", "F"]),
            ("<<E <<F", ["E{t}", "E", "F"]),
            ("<<E <<F", ["E\\", "x{t}", "E", "F"]),
            ("<<E <<F", ["E\\", "\\", " {t}", "E", "F"]),
            ("<<E <<F <<G", ["E {t}", "E", "F", "G"]),
            ("<<E <<F <<G", ["E", "F {t}", "F", "G"]),
            ("<<-E <<F", [tab + "E {t}", tab + "E", "F"]),
            ("<<E <<'F'", ["E {t}", "E", "F"]),
        )
        for ops, body in cases:
            for target, denied in (("~", True), ("$HOME", True), ("/", True), ("/tmp/x", False)):
                tail = ')" ' + target + ' ; : "'
                lines = [b.replace("{t}", tail) for b in body]
                cmd = "r" + 'm -r -f "$(cat ' + ops + nl + nl.join(lines) + nl + ')"'
                assert (security.is_denied(cmd) is not None) is denied, cmd
