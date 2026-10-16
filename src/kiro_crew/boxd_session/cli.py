"""The ``boxd`` CLI as the gateway uses it: lifecycle, commands and the dial.

Only what a session environment needs, all under the gateway machine's own
``boxd auth login`` (the CLI's stored credential; Kiro Crew never reads it):

* ``boxd machine new|get|remove --json`` for the machine's lifecycle;
* ``boxd machine hibernate`` and ``machine config set <m> delete-after`` for an
  archived session (cold now, deleted by boxd itself when retention ends);
* ``boxd machine exec <m> -- sh -c <command>``, which forwards stdin and stdout
  as streams and the remote exit code as its own, so it carries both one-shot
  guest commands and the long-lived dial to the guest worker;
* ``boxd machine connect <m>`` for the dashboard terminal (it needs a TTY).

Every machine name is validated and passed as its own argv element, never
through a shell.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
from typing import Any, Optional

from kiro_crew.session_environment import SessionEnvironmentUnavailable

#: Names this gateway mints: ``kc-`` plus 16 hex digits.
MACHINE_NAME_RE = re.compile(r"kc-[0-9a-f]{16}")
_TIMEOUT = 180.0
_OUTPUT_MAX = 1024 * 1024


class BoxdError(SessionEnvironmentUnavailable):
    """A boxd CLI call failed. The message is the CLI's own last line, bounded."""


def resolve_boxd_bin() -> str:
    """Absolute path to ``boxd``: the gateway may run with a minimal PATH."""
    from kiro_crew.env import augmented_path

    found = shutil.which("boxd") or shutil.which("boxd", path=augmented_path())
    if found:
        return found
    raise BoxdError(
        "the boxd CLI is not installed on the gateway machine; install it and run boxd auth login"
    )


def check_machine_name(name: str) -> str:
    if not isinstance(name, str) or not MACHINE_NAME_RE.fullmatch(name):
        raise BoxdError("not a boxd machine this gateway created")
    return name


class BoxdCLI:
    """Lifecycle calls through one ``boxd`` binary."""

    def __init__(self, boxd_bin: Optional[str] = None) -> None:
        self.bin = boxd_bin or resolve_boxd_bin()

    async def _run(self, *args: str, timeout: float = _TIMEOUT) -> tuple[int, str]:
        from kiro_crew.coder.remote_worker import _scrubbed_env

        try:
            process = await asyncio.create_subprocess_exec(
                self.bin,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_scrubbed_env(),
                start_new_session=True,
            )
        except OSError as exc:
            raise BoxdError(f"could not run the boxd CLI ({type(exc).__name__})") from exc
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            if isinstance(exc, asyncio.TimeoutError):
                raise BoxdError("the boxd CLI did not finish in time") from exc
            raise
        if len(out) > _OUTPUT_MAX:
            raise BoxdError("the boxd CLI returned too much data")
        text = out.decode("utf-8", "replace")
        if process.returncode:
            lines = (err or out).decode("utf-8", "replace").strip().splitlines()
            return int(process.returncode), (lines[-1] if lines else "")[:200]
        return 0, text

    @staticmethod
    def _json(text: str) -> Any:
        try:
            return json.loads(text)
        except ValueError as exc:
            raise BoxdError("the boxd CLI returned invalid JSON") from exc

    async def list_machines(self) -> list[dict[str, Any]]:
        code, text = await self._run("machine", "list", "--json", timeout=60)
        if code:
            raise BoxdError(self._signed_out(text) or f"boxd machine list failed: {text}")
        data = self._json(text)
        return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []

    async def get(self, name: str) -> Optional[dict[str, Any]]:
        code, text = await self._run(
            "machine", "get", check_machine_name(name), "--json", timeout=60
        )
        if code:
            if "not found" in text.lower():
                return None
            raise BoxdError(self._signed_out(text) or f"boxd machine get failed: {text}")
        data = self._json(text)
        return data if isinstance(data, dict) else None

    async def create(self, name: str, *, snapshot: str = "", vcpu: int = 0) -> dict[str, Any]:
        args = ["machine", "new", check_machine_name(name), "--json"]
        if snapshot:
            args += ["--from-snapshot", snapshot]
        elif vcpu:
            args += ["--vcpu", str(int(vcpu))]
        code, text = await self._run(*args)
        if code:
            raise BoxdError(self._signed_out(text) or f"boxd machine new failed: {text}")
        data = self._json(text)
        return data if isinstance(data, dict) else {}

    async def remove(self, name: str) -> bool:
        """True when deleted, False when it was already gone."""
        code, text = await self._run("machine", "remove", check_machine_name(name), "-y", "--json")
        if code:
            if "not found" in text.lower():
                return False
            raise BoxdError(self._signed_out(text) or f"boxd machine remove failed: {text}")
        return True

    async def hibernate(self, name: str) -> None:
        code, text = await self._run("machine", "hibernate", check_machine_name(name), "--json")
        if code and "not found" not in text.lower():
            raise BoxdError(f"boxd machine hibernate failed: {text}")

    async def set_delete_after(self, name: str, value: str) -> None:
        """Arm (``"3d"``) or clear (``"off"``) boxd's own deletion timer."""
        if not re.fullmatch(r"off|\d{1,3}[dhm]", value):
            raise ValueError("delete-after must be off or a duration like 3d")
        code, text = await self._run(
            "machine", "config", "set", check_machine_name(name), "delete-after", value, "--json"
        )
        if code:
            raise BoxdError(f"boxd machine config set failed: {text}")

    @staticmethod
    def _signed_out(text: str) -> str:
        lowered = text.lower()
        if "login" in lowered or "unauthor" in lowered or "not logged" in lowered:
            return "the gateway is not signed in to boxd; run boxd auth login there"
        return ""
