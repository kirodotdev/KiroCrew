"""On Windows the ssh-to-self floor reads the line after a trailing backslash as its own.

PowerShell and cmd end a statement at a newline that follows a backslash, where bash
joins the two lines. On Windows the floor also reads the submitted command with that
newline kept, outside single quotes, and refuses when either reading refuses; elsewhere
it reads the line joined, as bash runs it.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _denied_by

from kiro_crew.security import argv_floor as _argv_floor

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_RULE = "sandbox-escape-ssh-self"

_SECOND_LINE_TO_SELF = [
    "echo a \\\nssh -p 22 localhost id",
    "echo a \\\nssh -l u localhost id",
    "echo a \\\nscp -P 22 f localhost:/tmp/f",
    # A ``<<`` in a comment opens no here-document.
    "echo a \\\nssh -p 22 localhost id #<<",
]


@pytest.fixture(autouse=True)
def _remote_names_are_not_self(_floor_monkeypatch):
    # Steady state with no DNS: the own-host set is resolved, the address table has
    # published, and a name the textual layers cannot place is not this host.
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", frozenset())
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
    _floor_monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
    _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)


@pytest.mark.parametrize("cmd", _SECOND_LINE_TO_SELF)
def test_on_windows_the_line_after_a_trailing_backslash_is_refused(cmd, _floor_monkeypatch):
    _floor_monkeypatch.setattr("kiro_crew.platform_compat.IS_WINDOWS", True)
    assert _denied_by(cmd) == _RULE


@pytest.mark.parametrize(
    "windows, cmd",
    [(False, cmd) for cmd in _SECOND_LINE_TO_SELF]
    + [
        (True, "echo a \\\nssh -p 22 example.com id"),
        (True, "echo 'a \\\nssh -p 22 localhost id'"),
        # Single quotes hand the script to bash, which joins the lines.
        (True, "bash -c 'echo a \\\nssh -p 22 localhost id'"),
        # Git runs this value through its own POSIX shell, which joins the lines.
        (True, "git -c core.sshCommand='echo a \\\nssh -p 22 localhost id' fetch origin"),
        # PowerShell has no here-document, so it runs no line of this command.
        (True, "cat > note.md \\\n  <<'EOF'\nssh -p 22 localhost id\nEOF"),
    ],
)
def test_a_line_that_runs_no_connection_here_stays_allowed(windows, cmd, _floor_monkeypatch):
    _floor_monkeypatch.setattr("kiro_crew.platform_compat.IS_WINDOWS", windows)
    assert _denied_by(cmd) is None
