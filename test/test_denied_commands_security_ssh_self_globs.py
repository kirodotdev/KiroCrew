"""The ssh-to-self floor reads a glob that can name several ssh programs as each of them.

A glob bash runs (``*s*``, ``[rs]s*``), or a path a same-line ``cp``/``mv``/``ln``/
``install`` copied such a glob to and runs later, may be any program it names, so
every program's reading is checked rather than one picked in set order.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _denied_by

from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import argv_floor_ssh_verbs as _verbs

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_RULE = "sandbox-escape-ssh-self"


@pytest.fixture(autouse=True)
def _names_resolve_remote(_floor_monkeypatch):
    """A name only DNS could place reads as remote, as in ``TestSandboxEscapeSshSelf``."""
    _floor_monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
    _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)


@pytest.mark.parametrize(
    "cmd",
    [
        "cp *s* /tmp/x; /tmp/x -e 'ssh localhost id #' a b:c",
        "cp [rs]s* /tmp/x && /tmp/x -e 'ssh localhost id #' a b:c",
        "cp rsync /tmp/x; /tmp/x -e 'ssh localhost id #' a b:c",
        "mv *s* /tmp/x; /tmp/x -e 'ssh localhost id #' a b:c",
        "ln -s *s* /tmp/x && /tmp/x -e 'ssh localhost id #' a b:c",
        "install -m 755 [rs]s* /tmp/x; /tmp/x -e 'ssh localhost id #' a b:c",
        "cp *s* ./x; ./x notes.txt localhost:/tmp/",
        "*s* -e 'ssh localhost id #' a b:c",
        "[rs]s* -e 'ssh localhost id #' a b:c",
        "sudo [rs]s* -e 'ssh localhost' a b:c",
    ],
)
def test_a_glob_that_names_several_programs_is_read_as_each(cmd):
    # ``*s*`` is rsync in a folder holding ``rsync``, and rsync runs its ``-e``
    # value here, whatever other program the glob could also name.
    assert _denied_by(cmd) == _RULE


@pytest.mark.parametrize(
    "cmd",
    [
        "cp *.txt /tmp/x; /tmp/x",
        "cp src/* dst/; ls dst",
        "cp s* /tmp/x; cat /tmp/x",
        "cp * out/ && rsync -a out/ far.example.com:/x",
        "*s* -e 'ssh far.example.com id #' a b:c",
        "cp *s* /tmp/x; /tmp/x -e 'ssh far.example.com id #' a b:c",
        "cp *s* /tmp/x; /tmp/x -av src/ far.example.com:/srv/",
        "cp *s* /tmp/x; scp /tmp/x far.example.com:/tmp/",
        "cp *.py /tmp/x; /tmp/x localhost",
        "cp [ab]* /tmp/x && /tmp/x -e 'ssh localhost id #' a b:c",
        "s* --help",
        "* * * * * root /usr/local/bin/backup.sh",
    ],
)
def test_a_glob_read_as_each_program_keeps_benign_lines_allowed(cmd):
    assert _denied_by(cmd) is None


def test_a_glob_names_every_ssh_program_it_can_expand_to():
    assert set(_verbs._ssh_family_verbs("*s*")) == {"ssh", "scp", "sftp", "rsync"}
    assert set(_verbs._ssh_family_verbs("[rs]s*")) == {"ssh", "rsync"}
    assert _verbs._ssh_family_verbs("/usr/bin/ssh") == ("ssh",)
    assert _verbs._ssh_family_verbs("*.py") == ()


def test_the_refused_rows_fail_whatever_order_the_set_iterates(_floor_monkeypatch):
    # The deny rows above pass on main only when the hash seed happens to draw
    # rsync first; pin the order with rsync LAST so a revert is caught in every
    # process. The facade write reaches the owner that binds the name.
    _floor_monkeypatch.setattr(_argv_floor, "_SSH_FAMILY_VERBS", ("ssh", "scp", "sftp", "rsync"))
    assert _verbs._ssh_family_verbs("*s*")[-1] == "rsync"  # the losing order on main
    assert _denied_by("*s* -e 'ssh localhost id #' a b:c") == _RULE
    assert _denied_by("cp *s* /tmp/x; /tmp/x -e 'ssh localhost id #' a b:c") == _RULE


def test_a_glob_bash_does_not_run_gets_exactly_one_reading():
    # A glob that is another program's operand (``git add *s* x``), and a word of
    # only ``*``, keep ONE reading -- the same first pick main makes -- so a benign
    # file-operand glob and a cron field are not multi-read.
    readings = list(_verbs._ssh_verb_readings(["git", "add", "*s*", "x"], {0}, {}))
    assert [r for r in readings if r[0] == 2] == [
        (2, "*s*", _verbs._ssh_family_verbs("*s*")[0], False)
    ]
    star = list(_verbs._ssh_verb_readings(["*", "*"], {0}, {}))
    assert [r for r in star if r[0] == 0] == [(0, "*", _verbs._ssh_family_verbs("*")[0], False)]
