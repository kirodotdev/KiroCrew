"""One fake sandbox behind both of the lane's ssh runners, recording every exec in order.

The launch reaches a sandbox two ways: the install and the boot hook through an
``install.Runner`` (``run(host, command, stdin)``), and the sign-in and the health check
through :func:`remote.run_script`'s ``(argv, stdin, timeout)`` runner. :class:`LaunchBox` is
both, over one :class:`test_managed_env_install.FakeBox`, so a test can assert the order of
``probe``, ``install-public``, ``hook-*``, ``signin-*`` and ``health`` (and the fake service's
operations, when it is given one) in a single list. Nothing here touches ssh or a network.
"""

from __future__ import annotations

import functools
import subprocess

from managed_env_fake_ssh import HEALTH_OK, FakeSandboxSsh, kind
from test_managed_env_install import HOST as BOX_HOST
from test_managed_env_install import FakeBox

from kiro_crew.managed_env import install as ins
from kiro_crew.managed_env import remote

SIGNIN_IDS = {
    "whoami": "signin-whoami",
    "health": "health",
}


class LaunchBox(FakeBox):
    """A :class:`FakeBox` that is also the sign-in/health runner. ``health`` answers OK only once
    the hook has started the gateway. ``curl_tail`` is appended to a failed ``install-public``'s
    output (an installer's last stderr line)."""

    def __init__(self, *, signin=None, bff=None, curl_tail="", probe_rc=0, installed=False, **kw):
        kw.setdefault("gateway_up_after", 0)
        super().__init__(**kw)
        if installed:  # Crew's launcher on /projects runs, on the bundle route
            self.tools |= {"kirocrew", "crew-wrapper", ins.HAVE_KAS_BUNDLE}
        self.signin = signin if signin is not None else FakeSandboxSsh()
        self.bff = bff
        self.curl_tail = curl_tail
        self.probe_rc = probe_rc
        self.order: list[str] = []
        self.hosts: set[str] = set()
        self.scripts: list[str] = []  # every sign-in/health body as run_script sent it
        self._bff_seen = 0

    def mark(self, label: str) -> None:
        self._sync()
        self.order.append(label)

    def _sync(self) -> None:
        if self.bff is not None:
            ops = self.bff.ops()
            self.order += ops[self._bff_seen :]
            self._bff_seen = len(ops)

    # -- install.Runner ---------------------------------------------------------------------
    def run(self, host, command, stdin=None):
        self._sync()
        self.hosts.add(host)
        res = super().run(BOX_HOST, command, stdin)
        self.order.append(self.calls[-1][0])
        return res

    def _probe(self, command, stdin):
        if self.probe_rc:
            return ins.RunResult(self.probe_rc, "ssh: connect to host: Connection reset\n")
        return super()._probe(command, stdin)

    def _install_public(self, command, stdin):
        res = super()._install_public(command, stdin)
        if self.curl_tail and res.rc:
            res = ins.RunResult(res.rc, res.out + self.curl_tail + "\n")
        return res

    def with_hook(self) -> "LaunchBox":
        """The hook as provision leaves it, for tests that start at register."""
        ins.write_hook(self, BOX_HOST)
        self.order.clear()
        return self

    # -- remote.run_script's runner -------------------------------------------------------
    def __call__(self, argv, stdin, timeout):
        self._sync()
        self.hosts.add(argv[-2])
        self.scripts.append(stdin)
        k = kind(stdin)
        self.order.append(SIGNIN_IDS.get(k, "signin-other"))
        if k == "health":
            out = HEALTH_OK if self.gateway else ""
            return subprocess.CompletedProcess(argv, 0 if out else 1, out, "")
        return self.signin(argv, stdin, timeout)

    @property
    def ssh(self):
        return functools.partial(remote.run_script, runner=self)

    def ids(self, prefix: str = "") -> list[str]:
        return [i for i in self.order if i.startswith(prefix)]
