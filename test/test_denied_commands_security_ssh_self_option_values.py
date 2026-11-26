"""The ssh-to-self floor checks an option value that holds a quoted ``>`` or ``<``.

Inside quotes, ``>`` and ``<`` are part of the word, so ``-o proxycommand='ssh localhost
>x'`` still runs ``ssh localhost`` on this machine. The floor checks such a value the
same way it checks the value without them.
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
        "scp -o proxycommand='ssh localhost >x' far.example.com:",
        "scp -o proxycommand='ssh localhost <x' far.example.com:",
        "scp -oproxycommand='ssh localhost >x' far.example.com:",
        "scp -o 'proxycommand ssh localhost >x' far.example.com:",
        "ssh -o proxycommand='ssh localhost >x' far.example.com",
        "sftp -o proxycommand='ssh localhost >x' far.example.com",
        "ssh -o localcommand='ssh localhost >x' far.example.com",
        "ssh -o proxycommand='nc 127.0.0.1 22 >x' far.example.com",
        "ssh -o hostname=localhost>/dev/null far.example.com",
    ],
)
def test_an_option_value_aimed_here_with_a_quoted_redirect_is_refused(cmd):
    assert _denied_by(cmd) == _RULE
    # The same line without the ``>``/``<`` was refused already.
    assert _denied_by(cmd.replace(" >x", "").replace(" <x", "").replace(">/dev/null", "")) == _RULE


@pytest.mark.parametrize(
    "cmd",
    [
        "ssh -o proxycommand='ssh -W %h:%p jump.example.com 2>/dev/null' far.example.com id",
        "scp -o proxycommand='nc %h %p 2>/dev/null' a.txt far.example.com:b",
        "ssh -o proxycommand='nc %h %p' far.example.com id > out.txt",
        "ssh far.example.com 'hostname localhost >x'",
        "ssh far.example.com uptime >log 2>&1",
    ],
)
def test_a_redirect_in_a_far_value_or_after_the_command_is_allowed(cmd):
    assert _denied_by(cmd) is None
