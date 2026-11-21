"""The sign-in step of a managed-environment launch: the box's machine token, nothing else.

A new managed environment's Crew runs Crew's own engine on the environment's machine
token (``agent.kas_auth machine``, written by the install step). So this step starts no
device sign-in and runs no kiro-cli: it asks the box's Crew ``kas whoami`` over ssh, and
a machine account reads as signed in. Any other answer stops the step with the
machine-token class, so the user sees what to check on the box.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

from kiro_crew.managed_env.install import WRAPPER
from kiro_crew.managed_env.remote import RemoteResult

logger = logging.getLogger(__name__)

WHOAMI_TIMEOUT_SECS = 60
SSH_FAILED_EXIT = 255
MACHINE_ACCOUNT_TYPE = "machine"
#: The core's class for a machine-mode box with no usable token file (``kas whoami``).
MACHINE_TOKEN_MISSING = "machine_token_missing"
_SECRET_SHAPES = ("://", "user_code", "code:")

WHOAMI_SCRIPT = f"timeout {WHOAMI_TIMEOUT_SECS} {WRAPPER} kas whoami --format json </dev/null"


def machine_signin_refused_text(host: str) -> str:
    """The box's Crew gave no machine answer. The text names the class and the token
    file, never a code or a login command."""
    return (
        f"{host} is in machine-token mode, but its Crew reports no machine sign-in "
        f"({MACHINE_TOKEN_MISSING}): the managed environment's token file is missing, empty "
        "or unreadable. Machine mode never starts a device sign-in. Check the environment's "
        "machine token, then Connect again."
    )


def _account_type(stdout: str) -> str:
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            kind = data.get("accountType")
            return kind if isinstance(kind, str) else ""
    return ""


class SandboxSigninHandle:
    """The core's ``SigninHandle`` shape: already_logged_in/url/code/ports/error and
    wait/close/abort. ``url`` is always empty: there is never a code to show."""

    def __init__(
        self,
        host: str = "",
        target: Any = None,
        *,
        run: Callable[..., RemoteResult] | None = None,
        error: str = "",
    ) -> None:
        self._host = host
        self.login_target = target
        self.already_logged_in = False
        self.url = ""
        self.code = ""
        self.ports: list = []
        self.error = error
        if error or run is None:
            return
        # begin_signin runs after provision and before register, where a raise would
        # strand a billing environment: every failure becomes ``error``.
        try:
            self._check(run)
        except Exception as exc:  # noqa: BLE001 - never raise out of begin_signin
            logger.warning("could not read the sign-in on %s (%s)", host, type(exc).__name__)
            self.error = f"could not read the Kiro sign-in on {host}: {type(exc).__name__}"

    def _check(self, run: Callable[..., RemoteResult]) -> None:
        res = run(self._host, WHOAMI_SCRIPT, timeout=WHOAMI_TIMEOUT_SECS + 15)
        if res.timed_out or res.returncode == SSH_FAILED_EXIT:
            how = "timed out" if res.timed_out else f"exit {SSH_FAILED_EXIT}"
            tail = " | ".join(
                ln.strip()
                for ln in (res.stderr or "").splitlines()
                if ln.strip() and not any(s in ln.lower() for s in _SECRET_SHAPES)
            )[-300:]
            self.error = (
                f"could not read the sign-in on {self._host} over ssh ({how}); check the "
                "managed environment is RUNNING, then Connect." + (f" ssh: {tail}" if tail else "")
            )
            return
        if _account_type(res.stdout) == MACHINE_ACCOUNT_TYPE:
            self.already_logged_in = True
            return
        self.error = machine_signin_refused_text(self._host)

    def wait(self, cancel: threading.Event) -> bool:
        return self.already_logged_in

    def close(self) -> None:
        """Nothing to release: no login was started."""

    def abort(self) -> bool:
        """Nothing was started on the box, so there is nothing left polling."""
        return True
