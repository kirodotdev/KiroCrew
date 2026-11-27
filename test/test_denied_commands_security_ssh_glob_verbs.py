"""Tests for how the ssh-to-self floor reads a glob program word.

A glob that can name several ssh-family verbs (``s??`` names ``scp`` and ``ssh``)
is judged as every one of them, in sorted order, so its verdict never follows
the per-process hash seed. The rest of the floor is pinned by
``TestSandboxEscapeSshSelf`` in ``test_denied_commands_security_ssh_self.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _denied_by

from kiro_crew import security
from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

# Same handle-cache isolation as every test of the test_denied_commands_security family.
pytestmark = [
    pytest.mark.usefixtures("fresh_darwin_libc_handles"),
    # The seed probe starts interpreter children; keep them off other workers' starts.
    pytest.mark.xdist_group(name="ssh_glob_verb_seed_probe"),
]

#: Lines judged by the floor alone, with DNS answering "not this host".
_FLOOR_SEED_ROWS = [
    "/usr/bin/s?? x localhost",
    "s* far.example localhost",
    "git add s* README.md",
    "/usr/bin/s?? remote.example.com id",
]
#: Lines judged by the public gate, with every DNS lookup answering "this host".
_GATE_SEED_ROWS = [
    "git add s* notes.txt",
    "git add s* README.md",
    "s* far.example localhost",
    "/usr/bin/s?? x localhost",
]
_SEED_PROBE = (
    "import json, sys\n"
    "from kiro_crew.security import argv_floor, is_denied\n"
    "argv_floor._OWN_HOST_NAMES_CACHE = frozenset()\n"
    "argv_floor._OWN_HOST_RESOLVE_DONE = True\n"
    "argv_floor._NETLINK_ADDRS_PUBLISHED = True\n"
    "argv_floor._OWN_HOST_RESOLVE_NEXT_TRY = float('inf')\n"
    "rows = json.loads(sys.argv[1])\n"
    "argv_floor._resolved_host_verdict = lambda host, **_kw: False\n"
    "floor = [argv_floor._is_ssh_to_self(c) for c in rows['floor']]\n"
    "argv_floor._resolved_host_verdict = lambda host, **_kw: True\n"
    "gate = [is_denied(c) is None for c in rows['gate']]\n"
    "print(json.dumps({'floor': floor, 'gate_allowed': gate}))\n"
)


@pytest.fixture(scope="module")
def seed_verdicts(tmp_path_factory):
    """Both seed sweeps' verdicts, from ONE fresh interpreter per PYTHONHASHSEED.

    frozenset order follows the seed, which is fixed per process, so only fresh
    interpreters show an order-dependent verdict. The six children run one after
    another, once per module, and the module is its own xdist group, so they
    never start beside another worker's interpreter children.
    """
    cwd = tmp_path_factory.mktemp("seed_probe")
    rows = json.dumps({"floor": _FLOOR_SEED_ROWS, "gate": _GATE_SEED_ROWS})
    out = {}
    for seed in range(6):
        result = subprocess.run(
            [sys.executable, "-c", _SEED_PROBE, rows],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=FALLBACK_TIMEOUT_SECS,
            cwd=str(cwd),
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
        )
        assert result.returncode == 0, result.stderr
        out[seed] = json.loads(result.stdout.strip().splitlines()[-1])
    return out


class TestSshGlobVerbs:
    _RULE = "sandbox-escape-ssh-self"

    @pytest.fixture(autouse=True)
    def _pin_own_host_cache(self, request, _floor_monkeypatch):
        # The same pins as ``TestSandboxEscapeSshSelf._pin_own_host_cache``: a
        # fixed own-host set with the resolved-once latch set (no resolver
        # thread), the kernel address table published, and the DNS-alias layer
        # stubbed to "not self" so no real name is resolved.
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
        "cmd",
        [
            # ``s??`` names scp and ssh: as scp, ``localhost`` is a target.
            "/usr/bin/s?? x localhost",
            "s?? x localhost",
            "s??.exe x localhost",
            # ``s*`` names scp, sftp and ssh.
            "/usr/bin/s* x ::1",
            "s* far.example localhost",
            # ``[rs]s*`` names rsync and ssh: as rsync, every operand is checked.
            "/usr/bin/[rs]s* x localhost",
            # A copy of a multi-verb glob binary carries every verb it can name.
            "cp /usr/bin/s?? /tmp/q && /tmp/q x localhost",
        ],
    )
    def test_glob_naming_several_verbs_is_judged_as_every_verb(self, cmd):
        # The verbs check different operands, so reading the glob as one
        # arbitrary verb would let the others through.
        assert _denied_by(cmd) == self._RULE

    @pytest.mark.parametrize(
        "cmd",
        [
            "/usr/bin/s?? remote.example.com id",
            "/usr/bin/s* remote.example.com uptime",
            "ls *",
            "cp * /backup/",
        ],
    )
    def test_glob_naming_several_verbs_stays_allowed_off_this_host(self, cmd):
        assert _denied_by(cmd) is None

    def test_glob_verbs_are_every_verb_in_sorted_order(self):
        assert _argv_floor._ssh_family_verbs("/usr/bin/s??") == ("scp", "ssh")
        assert _argv_floor._ssh_family_verbs("*") == ("rsync", "scp", "sftp", "ssh")
        assert _argv_floor._ssh_family_verbs("/usr/bin/ssh.exe") == ("ssh",)
        assert _argv_floor._ssh_family_verbs("l?") == ()
        # The single-verb answer is the first in sorted order, never a set's.
        assert _argv_floor._ssh_family_verb("s??") == "scp"
        assert _argv_floor._ssh_family_verb("l?") is None

    def test_glob_verb_verdict_does_not_depend_on_the_hash_seed(self, seed_verdicts):
        # The floor alone, DNS answering "not this host". Before the fix, seeds
        # 0 and 3 disagreed on the first two lines.
        for seed, verdicts in seed_verdicts.items():
            assert verdicts["floor"] == [True, True, False, False], seed

    def test_git_add_glob_is_allowed_and_ssh_glob_refused_under_every_hash_seed(
        self, seed_verdicts
    ):
        # Through the public gate, which carries the line the git add exemption
        # reads, with every DNS lookup answering "this host": a ``git add`` row
        # is allowed only because its file names are never looked up, and the
        # ``s*`` verb rows are refused under every verb order the seed picks.
        for seed, verdicts in seed_verdicts.items():
            assert verdicts["gate_allowed"] == [True, True, False, False], seed

    @pytest.mark.parametrize(
        "cmd",
        [
            "cd ~/.ssh && xargs s??>/dev/null <<< localhost",
            "cd ~/.ssh && xargs -I{} /usr/bin/s??>/dev/null {} <<< localhost",
            'cp /usr/bin/ssh "/tmp/[s ]*" && xargs "/tmp/[s ]*" <<< localhost',
        ],
    )
    def test_xargs_glob_program_is_denied_however_its_word_is_written(self, cmd):
        # The rebuilt xargs command names each reading's verb, so neither a
        # redirect glued to the glob nor a space inside it hides the verb.
        assert _denied_by(cmd) == self._RULE
