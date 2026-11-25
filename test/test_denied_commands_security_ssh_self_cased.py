"""Tests for how the ssh-to-self floor reads a substitution body in the case it was typed.

A ``$( )``, backtick or double-quoted substitution body is judged as a command line of
its own. It gets the line's own reading of a wrapper's options as typed (``sudo -H``
takes no value, ``sudo -h`` does), and keeps the lowered reading where the typed text
does not read to the same spans and bodies or holds a here-document, so a body never
gets the here-document data view.
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
        # ``sudo -H`` takes no value, so ``ls`` is the program and the globs are its
        # operands, as on the plain line.
        ("echo $(sudo -H ls s* l*)", False),
        ("echo `sudo -H ls s* l*`", False),
        ("echo $(echo hi; sudo -H ls s* l*)", False),
        ('echo "$(echo hi; sudo -H ls s* l*)"', False),
        # ``sudo -h`` takes ``ls`` and runs ``s*``; an allowed body that lowers to the
        # same text must not stand in for it.
        ("echo $(echo hi; sudo -h ls s* l*)", True),
        ("echo `sudo -H ls s* l*` `sudo -h ls s* l*`", True),
        ("echo $(echo hi; sudo -H ls s* l*) $(echo hi; sudo -h ls s* l*)", True),
        # Where the typed text lowers to another length, a body keeps the lowered reading.
        ("echo İ $(echo hi; sudo -H ls s* l*)", True),
        # The line's own double-quoted reader still reads a lone body lowered.
        ('echo "$(sudo -H ls s* l*)"', True),
        # ``sudo -e`` edits its operands and runs no program, as on the plain line.
        ("echo $(echo hi; sudo -e s?h local*)", False),
        # An upper-case option that takes a value still takes the next word.
        ("echo $(echo hi; sudo -T 5 s* l*)", True),
        ("echo $(echo id | xargs -I{} -S 5 s* l* {})", True),
        ("echo $(echo hi; env -U root s* l*)", True),
        # A body does not get the line's here-document data view: what the heredoc
        # prints is run here.
        ('x="`cat <<EOF\n""\nssh -p 22 localhost id\nEOF\n`"; eval "$x"', True),
        ('eval "`python3 - <<EOF\nprint("""# say "hi\nssh -p 22 localhost id""")\nEOF\n`"', True),
    ],
)
def test_a_substitution_body_reads_wrapper_options_as_written(cmd, refused):
    assert _denied_by(cmd) == (_RULE if refused else None), cmd
