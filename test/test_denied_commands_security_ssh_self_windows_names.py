"""The ssh-to-self floor reads the Windows spellings of this host's own name.

PowerShell's ``$env:COMPUTERNAME`` and cmd's ``%COMPUTERNAME%`` expand to the
same name bash's ``$hostname`` does, so ``sandbox-escape-ssh-self`` refuses
them as a destination the way it refuses ``$hostname``.  A
different variable (``$env:TARGET_HOST``) stays the documented run-time
residual and is not refused.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _denied_by

from kiro_crew import security
from kiro_crew.security import argv_floor as _argv_floor

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_RULE = "sandbox-escape-ssh-self"


@pytest.fixture(autouse=True)
def _pin_own_host_cache(request, _floor_monkeypatch):
    # Same pin as ``TestSandboxEscapeSshSelf``: a fixed own-host set with the
    # resolved-once latch set (no resolver thread) and a "not self" DNS
    # verdict, so remote allow cases never resolve a real name.
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
        "ssh $env:COMPUTERNAME id",
        "ssh ${env:COMPUTERNAME} id",
        'ssh "$env:COMPUTERNAME" id',
        "ssh user@$env:COMPUTERNAME id",
        "ssh $env:COMPUTERNAME.corp.example.com id",
        "ssh %COMPUTERNAME% id",
        "ssh user@%COMPUTERNAME% id",
        "scp f.txt %COMPUTERNAME%:/tmp/f.txt",
        "scp f.txt $env:COMPUTERNAME:/tmp/f.txt",
        "sftp $env:computername",
        "rsync -av src/ %COMPUTERNAME%:/dst/",
        # The bash spellings stay refused.
        "ssh $hostname id",
        "ssh user@$hostname id",
        "ssh $(hostname) id",
    ],
)
def test_windows_own_name_variables_are_denied(cmd):
    assert _denied_by(cmd) == _RULE, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "ssh buildbox id",
        "ssh 10.20.30.40 id",
        # A longer variable name is a different variable, as $HOSTNAME_BACKUP is.
        "ssh $env:COMPUTERNAME_BACKUP id",
        "ssh %COMPUTERNAME%_BACKUP id",
        "ssh $HOSTNAME_BACKUP id",
        # Any other variable is the documented run-time residual.
        "ssh $env:TARGET_HOST id",
        "ssh %TARGET_HOST% id",
    ],
)
def test_other_hosts_and_variables_stay_allowed(cmd):
    assert _denied_by(cmd) is None, cmd
