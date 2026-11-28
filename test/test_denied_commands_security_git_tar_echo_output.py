"""The git/tar self floor reads every word a flat ``echo`` substitution prints.

Same rule, own-host pin and helpers as ``test_denied_commands_security_git_tar_self.py``.
A line whose substitution prints a transport to this host gets the refusal of the line
with that output written out; a line whose output dials nothing stays allowed.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _denied_by

from kiro_crew import security
from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import is_denied

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_STARS = " ".join(f"d{i}/*" for i in range(1, 101))
_FILES = " ".join(f"f{i}.txt" for i in range(400))


@pytest.fixture(autouse=True)
def _pin_own_host_cache(request, _floor_monkeypatch):
    # The sibling suite's pin: a fixed own-host cache with the resolved-once latch
    # set, and the DNS-alias layer stubbed to "not self", so nothing here resolves.
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
    ("cmd", "written_out"),
    [
        (
            'GIT_SSH_COMMAND="$(echo ssh localhost)" git fetch origin',
            'GIT_SSH_COMMAND="ssh localhost" git fetch origin',
        ),
        (
            'git -c core.sshCommand="$(echo ssh localhost)" fetch o',
            'git -c core.sshCommand="ssh localhost" fetch o',
        ),
        (
            "git ls-remote `echo far.example.com localhost`:x",
            "git ls-remote far.example.com localhost:x",
        ),
        ("git $(echo clone localhost:x)", "git clone localhost:x"),
        ("git -C . $(echo fetch localhost:x)", "git -C . fetch localhost:x"),
        ("env git $(echo ls-remote localhost:x)", "env git ls-remote localhost:x"),
        ("g?t $(echo ls-remote localhost:x)", "g?t ls-remote localhost:x"),
        ('sh -c "git clone $(echo y localhost:x)"', 'sh -c "git clone y localhost:x"'),
        ('bash -c "$(echo git clone localhost:x)"', 'bash -c "git clone localhost:x"'),
        ("tar $(echo -cf localhost:a.tar) .", "tar -cf localhost:a.tar ."),
        ("eval $(echo git clone localhost:x)", "eval git clone localhost:x"),
        ("eval `echo git ls-remote localhost:x`", "eval git ls-remote localhost:x"),
        ("git $(echo -ne clone localhost:x)", "git clone localhost:x"),
        ("git $(echo -E clone localhost:x)", "git clone localhost:x"),
        ("git $(echo clone$() localhost:x)", "git clone localhost:x"),
        ("git $(echo clone`` localhost:x)", "git clone localhost:x"),
        ("bash -c $'git $(echo clone localhost:x)'", "bash -c 'git clone localhost:x'"),
        ("bash -c $'git \\x24(echo clone localhost:x)'", "bash -c 'git clone localhost:x'"),
        ("git $(echo clone localhost\\:x)", "git clone localhost\\:x"),
        ("git $(echo clone\\ localhost:x)", "git clone localhost:x"),
        ('git $(echo clone l"ocal"host:x)', 'git clone l"ocal"host:x'),
    ],
)
def test_printed_transport_is_refused_like_the_line_written_out(cmd, written_out):
    assert _denied_by(written_out) is not None, written_out
    assert _denied_by(cmd) == _denied_by(written_out), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "git $(echo clone github.com:x)",
        "eval $(echo git clone github.com:x)",
        "git $(echo clone localhost:x | tr -d :)",
        'git commit -m "$(echo fix the dev server on localhost:3000)"',
        "git commit -m 'git $(echo clone localhost:x)'",
        "cat > notes.md <<'EOF'\nRun `echo hello world` first\nEOF",
        "git commit -m \"$(cat <<'EOF'\nfix: x\n\nRun `echo hello world` to check.\nEOF\n)\"",
        f"git add $(echo {_FILES})",
        f"tar -czf b.tgz $(echo {_STARS})",
        "tar -czf $(echo out dir).tgz src",
        "echo $(echo git clone localhost:x)",
    ],
)
def test_printed_words_that_dial_nothing_stay_allowed(cmd):
    assert is_denied(cmd) is None, cmd
