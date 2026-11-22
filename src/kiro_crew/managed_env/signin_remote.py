"""The sign-in step of a managed-environment launch: the box's machine token, nothing else.

The box's Crew runs on the environment's machine token (:mod:`.machine`), so this step
starts no device sign-in and runs no kiro-cli: it asks the box's Crew ``kas whoami``
over ssh, and a machine account reads as signed in. Anything else stops the step with a
line that names what to check on the box.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from kiro_crew.managed_env import machine
from kiro_crew.managed_env.remote import SSH_FAILED_EXIT, RemoteResult

logger = logging.getLogger(__name__)


MACHINE_ONLY = (
    "A managed environment's Crew runs on its machine sign-in; it cannot sign in as "
    "another identity. Launch it without choosing one."
)


def asks_another_identity(target: Any) -> bool:
    """Whether *target* names an identity other than the default."""
    return target is not None and not getattr(target, "is_default", False)


def not_machine_text(host: str) -> str:
    return (
        f"{host} reports no machine sign-in ({machine.TOKEN_MISSING}): the environment's "
        "token file is missing, empty or unreadable, or its Crew does not run machine mode "
        "yet. Check the environment, then Connect again."
    )


class SandboxSigninHandle:
    """The core's ``SigninHandle`` shape. ``url`` and ``code`` are always empty: there is
    never a code to show. Every failure is ``error``, never a raise: this runs after the
    environment exists, where a raise would leave it billing with no row."""

    def __init__(self, host: str, run: Callable[..., RemoteResult], target: Any = None) -> None:
        self.login_target = target
        self.already_logged_in = False
        self.url = ""
        self.code = ""
        self.ports: list = []
        self.error = ""
        if asks_another_identity(target):
            # Preflight refuses this (login_target_refusal); a retry that reaches here
            # must not report the machine sign-in as the identity asked for.
            self.error = MACHINE_ONLY
            return
        try:
            res = run(host, machine.WHOAMI_SCRIPT, timeout=90, retry=True)
        except Exception as exc:  # noqa: BLE001
            self.error = f"could not read the sign-in on {host} ({type(exc).__name__})"
            return
        if res.timed_out or res.returncode == SSH_FAILED_EXIT:
            self.error = (
                f"could not reach {host} over ssh; check the environment is running, then "
                "Connect again."
            )
        elif machine.account_type(res.stdout) == machine.MACHINE_ACCOUNT_TYPE:
            self.already_logged_in = True
        else:
            self.error = not_machine_text(host)

    def wait(self, cancel: threading.Event) -> bool:
        return self.already_logged_in

    def close(self) -> None:
        """Nothing to release: no login was started."""

    def abort(self) -> bool:
        """Nothing was started on the box, so nothing is left polling."""
        return True
