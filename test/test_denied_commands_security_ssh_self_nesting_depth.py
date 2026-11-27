"""Tests for the ssh-to-self floor's reading bound on a deep nest of double-quoted ``$( )``.

A nest past ``argv_floor_ssh._MAX_SSH_READING_DEPTH`` is refused, not a RecursionError,
and a deep nest that names no ssh-family verb is allowed.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _denied_by

from kiro_crew import security
from kiro_crew.security import argv_floor as _argv_floor

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


def _nested_double_quoted(levels: int, inner: str) -> str:
    return 'echo "$(' * levels + inner + ')"' * levels


class TestSshFloorNestingDepth:
    """A deep nest of double-quoted ``$( )`` is refused, not a RecursionError."""

    def test_the_issue_command_is_refused_without_an_exception(self):
        from kiro_crew.security import argv_floor_ssh as ssh

        real = ssh._double_quoted_substitution_bodies
        calls = [0]

        def counting(text):
            calls[0] += 1
            return real(text)

        cmd = _nested_double_quoted(1000, "true; echo ssh")
        with patch.object(ssh, "_double_quoted_substitution_bodies", counting):
            assert _argv_floor._reaches_self_over_ssh(cmd.lower()) is True
        assert calls[0] == ssh._MAX_SSH_READING_DEPTH

    def test_the_gate_refuses_a_nest_past_the_bound_without_an_exception(self):
        from kiro_crew.security import argv_floor_ssh as ssh

        # The gate's own substitution-depth cap (32) lets this depth through, so
        # the floor's reading bound is what refuses it.
        at_cap = _nested_double_quoted(ssh._MAX_SSH_READING_DEPTH, "true; echo ssh")
        assert _denied_by(at_cap) == "sandbox-escape-ssh-self"
        # Deeper than that cap, the gate refuses before any floor reads the command.
        verdict = security.is_denied(_nested_double_quoted(128, "true; echo ssh"))
        assert verdict is not None and "substitution nesting deeper than 32" in verdict

    def test_the_readings_open_at_once_stop_at_the_bound(self):
        from kiro_crew.security import argv_floor_ssh as ssh

        real = ssh._double_quoted_substitution_bodies
        deepest = [0]

        def counting(text):
            deepest[0] = max(deepest[0], ssh._ssh_reading_depth.get())
            return real(text)

        with patch.object(ssh, "_double_quoted_substitution_bodies", counting):
            assert (
                _argv_floor._reaches_self_over_ssh(_nested_double_quoted(200, "echo ssh")) is True
            )
        assert deepest[0] == ssh._MAX_SSH_READING_DEPTH
        assert ssh._ssh_reading_depth.get() == 0

    def test_a_shallow_nest_is_read_as_before(self):
        assert _denied_by(_nested_double_quoted(3, "ssh localhost id")) == (
            "sandbox-escape-ssh-self"
        )
        assert _denied_by(_nested_double_quoted(3, "true; echo hi")) is None

    def test_a_deep_nest_that_names_no_ssh_verb_is_allowed(self):
        cmd = _nested_double_quoted(1000, "true; echo hi")
        assert _argv_floor._reaches_self_over_ssh(cmd.lower()) is False
