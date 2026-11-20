"""The ssh-to-self floor reads a Windows backslash path to ``ssh.exe`` as that program.

On Windows PowerShell runs the line, and a backslash there separates path components
instead of escaping the next character. The floor reads the line a second time with
those backslashes as ``/``, and refuses when either reading refuses.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _denied_by

from kiro_crew.security import argv_floor as _argv_floor

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_RULE = "sandbox-escape-ssh-self"


@pytest.fixture(autouse=True)
def _remote_names_are_not_self(_floor_monkeypatch):
    # Steady state with no DNS: the own-host set is resolved, the address table has
    # published, and a name the textual layers cannot place is not this host.
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", frozenset())
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
    _floor_monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
    _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)


@pytest.mark.parametrize(
    "cmd",
    [
        r"C:\Windows\System32\OpenSSH\ssh.exe -p 22 localhost id",
        r".\ssh.exe -p 22 localhost id",
        r"C:\\Windows\\System32\\OpenSSH\\ssh.exe -p 22 localhost id",
        r"& 'C:\Windows\System32\OpenSSH\ssh.exe' -p 22 localhost id",
        r'"C:\Windows\System32\OpenSSH\ssh.exe" -p 22 localhost id',
        r"C:\Windows\System32\OpenSSH\scp.exe -P 22 localhost:/x .",
        # A glob in a backslash path is read as what it can expand to.
        r"C:\Windows\System32\OpenSS?\ss?.exe -p 22 localhost",
        r"C:\Windows\System32\OpenSSH\ss?.exe localhost",
        r"C:\Windows\System32\OpenSS?\ssh.exe localhost",
    ],
)
def test_a_backslash_path_to_an_ssh_program_aimed_here_is_refused(cmd):
    assert _denied_by(cmd) == _RULE


@pytest.mark.parametrize(
    "cmd",
    [
        r"ssh.exe -p 22 localhost id",
        r"C:/Windows/System32/OpenSSH/ssh.exe -p 22 localhost id",
        r"s\sh localhost id",
    ],
)
def test_the_spellings_refused_without_the_path_reading_stay_refused(cmd):
    assert _denied_by(cmd) == _RULE


@pytest.mark.parametrize(
    "cmd",
    [
        r"ssh.exe user@remote id",
        r"C:\Windows\System32\OpenSSH\ssh.exe -p 22 example.com id",
        r"C:\Windows\System32\OpenSS?\ss?.exe -p 22 example.com",
        r"echo a\ssh -p 22 localhost",
        r"type C:\Users\me\notes.txt",
    ],
)
def test_a_backslash_path_that_does_not_connect_here_is_allowed(cmd):
    assert _denied_by(cmd) is None


def test_the_path_reading_keeps_escapes_before_quotes_and_blanks():
    from kiro_crew.security.argv_floor_ssh_bodies import _with_backslash_path_separators

    assert _with_backslash_path_separators(r".\ssh.exe") == "./ssh.exe"
    assert _with_backslash_path_separators(r"a\ b \" \' \; \\x") == r"a\ b \" \' \; \/x"
