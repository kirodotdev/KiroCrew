"""Tests that naming a program is not running it.

The product name or a kill verb in a data command's argv is a mention; an executor
wrapper still gets the refusal. The permission-verb rows are covered by
``test_argv_rules.py``, since they are matched on argv rather than as text.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _KA, _NAME, _PK, _TOK, _denied_by

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


def _named(origin, *cases):
    """Lead each case with the test name it came from, so the old name stays in its id."""
    return [(origin, *c) if isinstance(c, tuple) else (origin, c) for c in cases]


class TestNameAsDataIsNotAnInvocation:
    """The product name in a DATA command's argv is a mention, not an invocation.

    ``echo <name> <verb>`` prints two words. Both halves of the union were
    position-blind about this: the regex matched the two words co-occurring, and the
    argv predicate treated the name as a program wherever it appeared in an argv.

    The classification is a DENYLIST of data consumers rather than an ALLOWLIST of
    executors, on purpose. Many commands hand their remaining argv to an executor
    (``ssh``, ``docker exec``, ``sudo``, ``env``, ``nohup``, ``timeout``,
    ``runuser``, ``chroot``, ``pkexec``, ``xargs``), so enumerating THOSE would make
    a forgotten entry a silent bypass; enumerating data consumers makes a forgotten
    entry a false positive instead — visible and safe.
    """

    @pytest.mark.parametrize(
        ("origin", "cmd"),
        [
            *_named(
                "test_name_and_verb_as_data_allowed",
                f"echo {_NAME} {_TOK}",
                f"echo 'the {_NAME} {_TOK} command mints a credential'",
                f"printf '%s' {_NAME} {_TOK}",
                f"cat notes.md | grep {_NAME} {_TOK}",
                f"git commit -m 'note: {_NAME} {_TOK} rule'",
            ),
            *_named(
                "test_kill_verb_as_data_allowed",
                f"echo {_PK} {_NAME}",
                f"echo 'run {_PK} {_NAME} to stop it'",
            ),
        ],
    )
    def test_names_as_data_allowed(self, origin, cmd):
        assert _denied_by(cmd) is None

    @pytest.mark.parametrize(
        "cmd",
        [
            f"ssh remote-host {_NAME} {_TOK}",
            f"docker exec c {_NAME} {_TOK}",
            f"sudo {_KA} {_NAME}",
            f"KIROCREW_HOME=/tmp/h {_NAME} {_TOK}",
            f"env FOO=1 {_NAME} {_TOK}",
            f"nohup {_NAME} {_TOK}",
            f"timeout 5 {_NAME} {_TOK}",
        ],
    )
    def test_executor_wrappers_still_blocked(self, cmd):
        # These pass their remaining argv to something that runs it, so the name IS
        # reachable as a program.  An executor allowlist would have to name every
        # one of them; the denylist shape means an unrecognised program defaults to
        # "this could execute the name".
        assert _denied_by(cmd) is not None
