"""Local machine: enroll with a remote gateway over the SSH the user already set up.

Enrollment runs ``kirocrew connected-computer enroll --stdin`` on the remote
gateway's host through the Remote Crew instance's own SSH target, so the SSH
login -- the same one the Remote Crew tunnel rides -- is what proves the owner.
The request goes on the remote command's stdin and the credential comes back on
its stdout: neither is ever in an argv, a log line or an error message. The
subcommand is a fixed literal; nothing the user typed is spliced into the remote
command line (the label and ids travel as JSON on stdin).

The remote gateway's dashboard bearer and the internal secret are never asked
for, sent or used: the result is a connector credential only
(:mod:`.credentials`).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Final

ENROLL_SUBCOMMAND: Final[str] = "connected-computer enroll --stdin"
FORGET_SUBCOMMAND: Final[str] = "connected-computer forget --stdin"
#: Largest remote stdout read for an answer.
_MAX_ANSWER_BYTES: Final[int] = 64 * 1024
#: A refusal code in the remote's answer (as ``protocol``'s refusal frame).
_CODE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z_]{1,64}$")
#: ssh's own exit status when it could not connect or authenticate.
_SSH_CONNECT_FAILED: Final[int] = 255


class EnrollmentFailed(RuntimeError):
    """The remote gateway did not enroll this computer; the message is safe to show.

    ``code`` is the remote's refusal code when it gave one (``revoked``,
    ``removed``, ``superseded``, ...), else "".
    """

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code

    @property
    def terminal(self) -> bool:
        """The remote, over the owner's SSH, said this computer's access was ended."""
        from kiro_crew.connected_computers.protocol import TERMINAL_REFUSALS

        return self.code in TERMINAL_REFUSALS


class EnrollmentUnavailable(EnrollmentFailed):
    """The remote gateway was not reached or gave no answer: try again later.

    Unlike its base class this is not the remote's refusal: ssh could not start
    or connect (exit 255), or no answer line came back.
    """


@dataclass(frozen=True)
class Enrolled:
    device_id: str
    gateway_id: str
    gateway_label: str
    credential: str
    expires: float
    #: Lets the connector check the gateway before it sends ``credential`` (R6).
    peer_key: str = ""


def _answer(stdout: bytes) -> dict[str, Any]:
    """The last JSON object line of the remote stdout (banners may come first)."""
    for line in reversed(stdout[-_MAX_ANSWER_BYTES:].decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    raise EnrollmentUnavailable("the remote gateway gave no answer; is Kiro Crew up to date there?")


async def run_remote(
    instance: Any, subcommand: str, payload: dict[str, Any], *, timeout: float
) -> dict:
    """Run a fixed ``kirocrew`` subcommand on ``instance``'s host with JSON on stdin."""
    from kiro_crew.instances.token_mint import (
        _build_ssh_argv,
        build_remote_command,
        ssh_spawn_argv_env,
    )
    from kiro_crew.platform_compat import kill_and_reap

    if subcommand not in (ENROLL_SUBCOMMAND, FORGET_SUBCOMMAND):
        raise ValueError("unknown remote subcommand")
    ssh_host = getattr(instance, "ssh_host", "")
    if not ssh_host:
        raise EnrollmentFailed("this Remote Crew connection has no SSH host")
    remote_command = build_remote_command(
        getattr(instance, "remote_bin", ""),
        subcommand,
        marker_port=getattr(instance, "remote_port", None) or None,
    )
    # The shared builder passes ``-n`` (no stdin); this call writes its request there.
    argv = [a for a in _build_ssh_argv(ssh_host, remote_command) if a != "-n"]
    argv, env = await asyncio.to_thread(ssh_spawn_argv_env, argv)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise EnrollmentUnavailable(f"could not start ssh: {type(exc).__name__}") from exc
    request = json.dumps(payload).encode("utf-8") + b"\n"
    try:
        out, _err = await asyncio.wait_for(proc.communicate(request), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        await kill_and_reap(proc)
        raise
    if proc.returncode == _SSH_CONNECT_FAILED:
        raise EnrollmentUnavailable("could not reach the remote gateway over SSH")
    answer = _answer(out)
    if proc.returncode != 0 or answer.get("ok") is not True:
        reason = str(answer.get("error") or "the remote gateway refused")[:300]
        code = answer.get("code")
        valid = isinstance(code, str) and bool(_CODE_RE.fullmatch(code))
        raise EnrollmentFailed(reason, str(code) if valid else "")
    return answer


async def enroll(instance: Any, *, base: str, fingerprint: str, label: str) -> Enrolled:
    """Enroll this computer after a native consent; :func:`renew` only extends."""
    return await _enroll(
        instance, {"base": base, "fingerprint": fingerprint, "label": label, "renew": False}
    )


async def renew(
    instance: Any, *, base: str, fingerprint: str, label: str, credential: str
) -> Enrolled:
    """Extend an active enrollment by presenting the credential it replaces.

    The remote verifies that credential in full except its expiry, so it renews
    before or after it ran out, while a credential from before a Disconnect, a
    Remove or a re-enrollment never renews (``connected-computer enroll --stdin``).
    """
    if not credential:
        raise EnrollmentFailed("this computer has no credential to renew; share it again")
    return await _enroll(
        instance,
        {
            "base": base,
            "fingerprint": fingerprint,
            "label": label,
            "renew": True,
            "credential": credential,
        },
    )


async def _enroll(instance: Any, request: dict[str, Any]) -> Enrolled:
    answer = await run_remote(instance, ENROLL_SUBCOMMAND, request, timeout=90.0)
    from kiro_crew.connected_computers.protocol import valid_nonce

    try:
        enrolled = Enrolled(
            device_id=str(answer["device_id"]),
            gateway_id=str(answer["gateway_id"]),
            gateway_label=str(answer.get("gateway_label") or ""),
            credential=str(answer["credential"]),
            expires=float(answer["expires"]),
            peer_key=str(answer["peer_key"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EnrollmentFailed("the remote gateway's answer was incomplete") from exc
    if not valid_nonce(enrolled.peer_key):
        # Without it the connector could not tell the gateway from a squatter.
        raise EnrollmentFailed("the remote gateway's answer was incomplete")
    return enrolled


async def forget(instance: Any, *, device_id: str, fingerprint: str) -> None:
    """Revoke this computer on the remote gateway (best effort after a local Off)."""
    await run_remote(
        instance,
        FORGET_SUBCOMMAND,
        {"device_id": device_id, "fingerprint": fingerprint},
        timeout=60.0,
    )
