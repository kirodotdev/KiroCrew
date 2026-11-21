"""A fake ssh runner for the managed-environment lane: answers by script content, records every exec.

It sits at the ``(argv, stdin, timeout)`` level under :func:`remote.run_script`, so the
tests see exactly what would travel to ``bash -s``, including the exports. Nothing here
touches ssh, a network or a real sign-in.
"""

from __future__ import annotations

import functools
import subprocess
from dataclasses import dataclass, field

from kiro_crew.managed_env import remote

#: The box's Crew in machine mode (``kas whoami --format json``).
MACHINE_WHOAMI = '{"accountType": "machine"}'
#: A box whose Crew reports a person's sign-in, not the machine token.
IC_WHOAMI = (
    '{"accountType":"IamIdentityCenter","startUrl":"https://example-org.example.test/start"}'
)
HEALTH_OK = 'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{"ok": true}'
#: ssh's own failure: its noise line (dropped by run_script) and its error.
SSH_255_STDERR = (
    "Warning: Permanently added 'crew-sbx-env-001' (ED25519) to the list of known hosts.\n"
    "mint-and-attach: GetSandbox answered 409: the sandbox is not RUNNING\n"
    "kex_exchange_identification: Connection closed by remote host\n"
)


@dataclass
class FakeSandboxSsh:
    #: the box's ``kas whoami --format json`` answer: "" = no machine token, else the JSON line
    whoami: str = MACHINE_WHOAMI
    #: health answers in order; the last one repeats
    health: list = field(default_factory=lambda: [HEALTH_OK])
    #: raise this from the runner (a transport failure) when set
    raises: Exception | None = None
    #: per exec kind: (stdout, rc) answered as is, instead of the modelled answer
    raw: dict = field(default_factory=dict)
    #: per exec kind: "timeout" (the runner's wait ran out) or "ssh255" (ssh itself failed)
    transport: dict = field(default_factory=dict)
    #: what ssh prints on stderr for an "ssh255" exec
    ssh255_stderr: str = SSH_255_STDERR
    execs: list = field(default_factory=list)  # (host, stdin)

    @property
    def run(self):
        return functools.partial(remote.run_script, runner=self)

    def kinds(self) -> list:
        return [kind(s) for _, s in self.execs]

    def __call__(self, argv, stdin, timeout):
        if self.raises is not None:
            raise self.raises
        self.execs.append((argv[-2], stdin))
        k = kind(stdin)
        how = self.transport.get(k)
        if how == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if how == "ssh255":
            return subprocess.CompletedProcess(argv, 255, "", self.ssh255_stderr)
        if k in self.raw:
            out, rc = self.raw[k]
            return subprocess.CompletedProcess(argv, rc, out, "")
        out, rc = "", 0
        if k == "health":
            out = self.health.pop(0) if len(self.health) > 1 else self.health[0]
            rc = 0 if out else 1
        elif k == "whoami":
            out, rc = (self.whoami + "\n", 0) if self.whoami else ("machine_token_missing\n", 1)
        return subprocess.CompletedProcess(argv, rc, out, "")


def kind(script: str) -> str:
    if "/api/health" in script:
        return "health"
    if "whoami --format json" in script:
        return "whoami"
    return "other"
