"""Tests for how the ssh-to-self floor reads an escaped substitution opener one level down.

Inside double quotes bash removes the backslash before ``$`` and a backtick, so the
payload an ``eval``, ``bash -c`` or ``sh -c`` word hands its nested shell holds a live
``$( )`` or backtick span; and a backtick body loses the backslash before ``$``, a
backtick or a backslash before it is read. The floor judges that text too. Single-quoted
text keeps its backslashes, as bash passes it literally.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _denied_by

from kiro_crew import security
from kiro_crew.security import argv_floor as _argv_floor

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_RULE = "sandbox-escape-ssh-self"


@pytest.fixture(autouse=True)
def _pin_own_host_cache(request, _floor_monkeypatch):
    """The own-host pin of ``TestSandboxEscapeSshSelf``: a fixed name cache with the
    resolve-once latch set, so no resolver thread starts, and a DNS alias verdict
    of "not self", so no test resolves a real name."""
    own_mp = pytest.MonkeyPatch()
    request.addfinalizer(own_mp.undo)
    own = security.socket.gethostname().strip().lower()
    pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
    _floor_monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
    own_mp.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
    _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)


@pytest.mark.parametrize(
    ("cmd", "refused"),
    [
        # A double-quoted payload: bash hands the nested shell a live opener.
        ('bash -c "echo \\$(ssh -p 22 localhost id)"', True),
        ('bash -c "echo \\$(echo hi; ssh -p 22 localhost id)"', True),
        ('eval "echo \\$(ssh -p 22 localhost id)"', True),
        ('eval "echo \\$(echo hi; ssh -p 22 localhost id)"', True),
        ('bash -c "echo \\`echo hi; ssh -p 22 localhost id\\`"', True),
        ('eval "ssh -p 22 \\`printf localhost\\` id"', True),
        ('echo $(echo hi; eval "ssh -p 22 \\`printf localhost\\`")', True),
        # A backtick body: the escaped backtick inside it is a live span one level down.
        ("echo `echo hi; ssh -p 22 \\`printf localhost\\` id`", True),
        # Single-quoted text stays literal, and a double-quoted word no shell reads is data.
        ("bash -c 'echo \\$(ssh -p 22 localhost id)'", False),
        ("eval 'echo \\$(ssh -p 22 localhost id)'", False),
        ("bash -c \"echo '\\$(ssh -p 22 localhost id)'\"", False),
        ('echo "\\$(ssh -p 22 localhost id)"', False),
        ("echo `echo '\\$(ssh -p 22 localhost id)'`", False),
        # Ordinary payloads that escape an opener for the nested shell stay allowed.
        ('bash -c "echo \\$(date) \\`whoami\\`"', False),
        ('eval "x=\\$(git rev-parse HEAD); echo \\$x"', False),
        ('docker run --rm img sh -c "echo \\$(uname -a)"', False),
        ("echo `echo hi; printf '%s' \\`date\\``", False),
        # A quoted heredoc's text is written to a file, not run.
        ("cat > run.sh <<'EOF'\nbash -c \"echo \\$(echo hi; ssh -p 22 localhost id)\"\nEOF", False),
        (
            "cat > run.sh <<'EOF'\nbash -c \"echo \\`echo hi; ssh -p 22 localhost id\\`\"\nEOF",
            False,
        ),
        ("cat <<EOF\nit's\nEOF\nbash -c 'echo \"\\$(ssh -p 22 localhost id)\"'", False),
        # An escaped ``$`` that opens no substitution keeps its backslash (here an ANSI-C tag).
        ('sh -c "git commit -m \\"\\$(cat <<\\$\'E\'\nssh -p 22 localhost id\nE\n)\\""', False),
        # The text a ``cat`` prints from a quoted heredoc stays data one level down, as it
        # does in the same script single-quoted.
        ("bash -c \"echo \\$(cat <<'EOF'\nssh -p 22 localhost id\nEOF\n)\"", False),
        ("bash -c 'echo $(cat <<'\"'\"'EOF'\"'\"'\nssh -p 22 localhost id\nEOF\n)'", False),
    ],
)
def test_an_escaped_opener_is_read_one_quoting_level_down(cmd, refused):
    assert _denied_by(cmd) == (_RULE if refused else None), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        'echo bash -c "echo \\$(echo hi; ssh -p 22 localhost id)"',
        'echo eval "echo \\$(ssh -p 22 localhost id)"',
    ],
)
def test_a_script_echo_only_prints_stays_a_known_residual(cmd):
    """Bash runs no ssh here, yet the floor refuses it, as it refuses the same script
    written live in single quotes (``echo bash -c '...'``): the payload readers take a
    script ``echo`` only prints as a payload. A fix to that reading flips this test."""
    assert _denied_by(cmd) == _RULE
