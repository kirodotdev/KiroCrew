"""Tests for the shared shell reader that every argv-structural floor reads.

The self-protection subcommand floor, the git-publish floor and the ssh self-target
floor all read a command through one layer in ``shell_normalizer``. These tests pin
how the self-subcommand floor reads a redirection glued to an operand word, and a
command separator glued to that redirection, through the public gate
(``is_denied``) and the reader functions themselves.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401

from kiro_crew import security

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


def _refused(cmd: str) -> bool:
    return security.is_denied(cmd) is not None


class TestRedirectGluedToAnOperandWord:
    """``restart>/tmp/log`` is the argv word ``restart`` plus a redirection."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "kirocrew restart>/tmp/log",
            "kirocrew restart>>/tmp/log",
            "kirocrew restart&>/tmp/log",
            "kirocrew restart</dev/null",
            "kirocrew restart>/tmp/log; echo ok",
            "kirocrew update>/tmp/log",
            "kirocrew file-delivery>/tmp/log approve",
            "python -m kiro_crew restart>/tmp/log",
            # A command separator glued in FRONT of the redirect (``restart;>x``):
            # the shell runs ``kirocrew restart`` and the redirect binds the next
            # command. The operand reader must keep ``restart`` and stop at the
            # separator, not let the redirect token swallow the boundary.
            "kirocrew restart;>x",
            "kirocrew restart|>x",
            "kirocrew restart||>x",
            "kirocrew update;>x",
            # A separator glued into the redirect TARGET (``status>x&&<next>``)
            # ends the first command; the next command is read on its own.
            "kirocrew status>x&&kirocrew restart",
            "kirocrew status>x;kirocrew restart",
            "kirocrew status>x|kirocrew restart",
            "kirocrew status>x&kirocrew restart",
            "kirocrew status>x&&kirocrew update",
            "kirocrew status >x&&kirocrew restart",
            "kirocrew status>x&&python -m kiro_crew restart",
        ],
    )
    def test_glued_redirect_keeps_the_subcommand_word(self, cmd):
        assert _refused(cmd), cmd

    def test_operand_reader_keeps_the_word_in_front_of_the_redirect(self):
        tokens = security._self_tokens("kirocrew restart>/tmp/log")
        assert security._self_cli_operands(tokens, 0) == ["restart"]

    @pytest.mark.parametrize(
        ("token", "word"),
        [
            ("restart>/tmp/log", "restart"),
            ("restart&>/tmp/log", "restart"),
            ("restart</dev/null", "restart"),
            ("2>/tmp/log", ""),
            ("{fd}>/tmp/log", ""),
            (">/tmp/log", ""),
            (">", ""),
            ("restart", ""),
            ("$x>/tmp/log", ""),
            ("{a,b}>/tmp/log", ""),
            ("x<(true)", ""),
        ],
    )
    def test_redirect_word_prefix(self, token, word):
        assert security._redirect_word_prefix(token) == word

    @pytest.mark.parametrize(
        "cmd",
        [
            "kirocrew $x>/tmp/log restart",
            "kirocrew {a,b}>/tmp/log restart",
            "kirocrew x<(true) restart",
        ],
    )
    def test_a_non_literal_word_before_a_redirect_is_not_read_as_an_operand(self, cmd):
        assert _refused(cmd), cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            # A separator inside a redirect target may be quoted or escaped text
            # (``shlex`` has removed the quotes), so it does not end the argv and the
            # subcommand after the redirection is still read.
            "kirocrew >'x;y' restart",
            "kirocrew 2>'a|b' restart",
            "kirocrew >x\\;y restart",
            "kirocrew >'a&b' restart",
            "kirocrew --no-jail>'a|b' update",
            "python -m kiro_crew >'x;y' update",
            "kirocrew $(true>x;) restart",
        ],
    )
    def test_a_separator_inside_a_redirect_target_does_not_end_the_argv(self, cmd):
        assert _refused(cmd), cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "kirocrew status>/tmp/log",
            "kirocrew 2>/tmp/log status",
            "kirocrew restart2>/tmp/log",
        ],
    )
    def test_a_descriptor_or_another_subcommand_stays_allowed(self, cmd):
        assert not _refused(cmd), cmd
