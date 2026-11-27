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

_FAKE_OWN_HOST = "kc-test-host"


@pytest.fixture(autouse=True)
def _pin_own_host_cache(request, _floor_monkeypatch):
    # A fixed fake own-host name (never the real machine's, which could equal
    # a remote allow case such as ``buildbox``), the resolved-once latch set
    # (no resolver thread) and a "not self" DNS verdict, so remote allow cases
    # never resolve a real name.
    own_mp = pytest.MonkeyPatch()
    request.addfinalizer(own_mp.undo)
    own_mp.setattr(security.socket, "gethostname", lambda: _FAKE_OWN_HOST)
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", frozenset({_FAKE_OWN_HOST}))
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
        # OpenSSH reads the host after the LAST ``@`` of userinfo.
        "ssh $env:USERNAME@$env:COMPUTERNAME id",
        "ssh %USERNAME%@%COMPUTERNAME% id",
        "ssh user:pw@%COMPUTERNAME% id",
        "scp f.txt $env:USERNAME@$env:COMPUTERNAME:/tmp/f.txt",
        # A URI authority is a host: ssh://[user@]HOST[:port].
        "ssh ssh://%COMPUTERNAME% id",
        "ssh ssh://user@$env:COMPUTERNAME:22 id",
        "ssh ssh://${env:COMPUTERNAME}.corp.example.com id",
        "scp f.txt scp://%COMPUTERNAME%/tmp/f.txt",
        "sftp sftp://user@%COMPUTERNAME%",
        "scp f.txt $env:USERNAME@$env:COMPUTERNAME:job@x.txt",
        # The bash spellings stay refused.
        "ssh $hostname id",
        "scp -r $HOSTNAME backup:/archives/",
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


@pytest.mark.parametrize(
    "cmd",
    [
        # A local file named after the machine is not a connection to it.
        "scp %COMPUTERNAME%.log backup:/logs/",
        "scp $env:COMPUTERNAME.zip backup:/archives/",
        "rsync -av $env:COMPUTERNAME.d/ backup:/snapshots/",
        "scp backup:/logs/app.log %COMPUTERNAME%.app.log",
        # An ``@`` inside a remote path is path data, not userinfo.
        "scp app.log backup:/logs/app@%COMPUTERNAME%.log",
        "scp build.zip deploy@backup:/drops/ci@%COMPUTERNAME%.zip",
        "scp app.log backup:/logs/app@$HOSTNAME.log",
        "rsync -av backup:/srv/snapshots/ ./snap@$HOSTNAME.d/",
        # For scp and rsync an operand with no ``host:`` colon is a local path.
        "scp -r $env:COMPUTERNAME backup:/archives/",
        "scp -r ${env:COMPUTERNAME} backup:/archives/",
        "scp -r %COMPUTERNAME% backup:/archives/",
        "rsync -av backup:/srv/snapshots/ %COMPUTERNAME%",
        "rsync -av backup:/srv/snapshots/ $env:COMPUTERNAME",
        # A file operand's host ends at its first colon: the host here is backup.
        "scp f.txt backup:job@%COMPUTERNAME%",
        # Git and tar read a word with no host colon as a name or a local file;
        # the colon in ``$env:`` is not a host colon.
        "git fetch origin $env:COMPUTERNAME",
        "git remote add $env:COMPUTERNAME https://git.example.com/team/repo.git",
        'git clone "$env:COMPUTERNAME.bundle" restored',
        'tar -cf "$env:COMPUTERNAME.tar" logs',
        'tar -czf "${env:COMPUTERNAME}.tar.gz" logs',
        'tar -xf "$env:COMPUTERNAME.tar" -C restore',
    ],
)
def test_a_machine_named_file_or_remote_path_is_not_this_host(cmd):
    assert _denied_by(cmd) is None, cmd


def test_a_dotted_name_before_a_path_colon_is_this_host():
    assert _denied_by("scp f.txt %COMPUTERNAME%.corp.example.com:/tmp/f.txt") == _RULE


@pytest.mark.parametrize(
    "cmd",
    [
        # The scp/rsync local-path reading does not reach command values: these
        # match main's refusal of the same lines spelled with $HOSTNAME.
        'ssh -o "ProxyCommand=nc %COMPUTERNAME% 22" buildbox',
        'ssh -o "LocalCommand=echo connected from %COMPUTERNAME%" buildbox',
        "ssh -F %COMPUTERNAME%.ssh_config buildbox",
    ],
)
def test_a_windows_name_in_an_ssh_option_value_stays_refused(cmd):
    assert _denied_by(cmd) == _RULE, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # Git counts a Windows name only in clear transport syntax: a URL
        # authority or an scp-like ``user@host:path``.
        "git clone ssh://%COMPUTERNAME%/srv/repo.git",
        "git clone ssh://git@$env:COMPUTERNAME:22/srv/repo.git",
        "git ls-remote git+ssh://${env:COMPUTERNAME}/srv/repo.git",
        "git clone git@%COMPUTERNAME%:team/repo.git",
        "git fetch $env:USERNAME@$env:COMPUTERNAME:/srv/repo.git main",
        "git push git@%COMPUTERNAME%:/srv/repo.git feature",
        "git push -o ci.skip git@%COMPUTERNAME%:/srv/repo.git feature",
        "git push ssh://%COMPUTERNAME%/srv/repo.git feature",
        "git pull git@%COMPUTERNAME%.corp.example.com:/srv/repo.git main",
        "tar -cf %COMPUTERNAME%:/backup/logs.tar logs",
    ],
)
def test_a_windows_name_as_a_git_or_tar_remote_host_is_refused(cmd):
    assert _denied_by(cmd) == "sandbox-escape-git-tar-self", cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # A refspec or a remote name is never a transport, whatever options
        # come first or however the remote is spelled.
        "git push origin %COMPUTERNAME%:refs/heads/ci/%COMPUTERNAME%",
        "git push origin %COMPUTERNAME%",
        "git push --force origin %COMPUTERNAME%:ci/%COMPUTERNAME% build:ci/build",
        "git fetch origin $env:COMPUTERNAME:$env:COMPUTERNAME",
        # The six refspec rows Security Scope Review listed.
        "git push %COMPUTERNAME% %COMPUTERNAME%:ci/%COMPUTERNAME%",
        "git push %CI_REMOTE% %COMPUTERNAME%:refs/heads/ci/%COMPUTERNAME%",
        "git fetch --depth 1 origin %COMPUTERNAME%:refs/remotes/ci/%COMPUTERNAME%",
        "git fetch -p origin %COMPUTERNAME%:refs/remotes/ci/%COMPUTERNAME%",
        "git pull --rebase origin %COMPUTERNAME%:%COMPUTERNAME%",
        "git pull --ff-only origin $env:COMPUTERNAME:$env:COMPUTERNAME",
        # A bare scp-like word is read like a refspec; main allows it too.
        "git ls-remote %COMPUTERNAME%:/srv/repo.git",
    ],
)
def test_a_windows_name_in_a_refspec_or_remote_name_is_not_a_host(cmd):
    assert _denied_by(cmd) is None, cmd
