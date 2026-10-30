"""Credential-safe Coder CLI adapter returning bounded structured records."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.coder.startup import startup_timing
from kiro_crew.sandbox import RLIMIT_PROFILE_SESSION_HOST, create_subprocess_limited

_COMMAND_TIMEOUT_SECS = 600.0
_OUTPUT_MAX_BYTES = 4 * 1024 * 1024
_PIPE_READ_BYTES = 64 * 1024
_WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
logger = logging.getLogger(__name__)


class CoderClientError(RuntimeError):
    """A bounded Coder lifecycle operation failed."""


@dataclass(frozen=True)
class CoderWorkspace:
    uuid: str
    name: str
    owner: str
    template: str
    status: str
    last_used_at: str


Runner = Callable[[list[str], dict[str, str], Path], Awaitable[bytes]]


class CoderClient:
    def __init__(
        self,
        coder_bin: str,
        url: str,
        token: str,
        cwd: Path,
        runner: Runner | None = None,
    ) -> None:
        self.coder_bin = coder_bin
        self.url = url
        self._token = token
        self.cwd = cwd
        self._runner = runner or self._run

    def _env(self) -> dict[str, str]:
        env = {
            key: value
            for key in (
                "PATH",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
            )
            if (value := os.environ.get(key))
        }
        env["CODER_URL"] = self.url
        env["CODER_SESSION_TOKEN"] = self._token
        return env

    async def _run(self, argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        process = await create_subprocess_limited(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=(
                platform_compat.CREATE_NEW_PROCESS_GROUP | platform_compat._SUBPROCESS_NO_WINDOW
            ),
            profile=RLIMIT_PROFILE_SESSION_HOST,
        )
        if process.stdout is None or process.stderr is None:
            raise CoderClientError("Coder lifecycle command pipes are unavailable")
        stdout_task = asyncio.create_task(self._drain_bounded(process.stdout, _OUTPUT_MAX_BYTES))
        # Drain stderr unretained: remote commands may print unlabelled secrets.
        stderr_task = asyncio.create_task(self._drain_bounded(process.stderr, 0))
        wait_task = asyncio.create_task(process.wait())
        tasks = (stdout_task, stderr_task, wait_task)
        try:
            (stdout, stdout_exceeded), _stderr_result, returncode = await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=_COMMAND_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError as exc:
            # Shared tree kill + bounded reap; a CLI child has no runtime lease.
            await platform_compat.kill_and_reap(process)
            raise CoderClientError("Coder lifecycle command timed out") from exc
        except asyncio.CancelledError:
            await platform_compat.kill_and_reap(process)
            raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if returncode:
            logger.warning("Coder lifecycle command failed (exit %d)", returncode)
            raise CoderClientError(f"Coder lifecycle command failed (exit {returncode})")
        if stdout_exceeded:
            raise CoderClientError("Coder lifecycle command returned too much data")
        return stdout

    @staticmethod
    async def _drain_bounded(
        stream: asyncio.StreamReader,
        limit: int,
    ) -> tuple[bytes, bool]:
        retained = bytearray()
        exceeded = False
        while chunk := await stream.read(_PIPE_READ_BYTES):
            remaining = limit - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
            if len(chunk) > remaining:
                exceeded = True
        return bytes(retained), exceeded

    async def _call(self, *args: str) -> bytes:
        # Only the fixed verb is logged. Later arguments can carry user data.
        with startup_timing(logger, "cli_" + args[0]):
            return await self._runner([self.coder_bin, *args], self._env(), self.cwd)

    @staticmethod
    def _owner_of(raw: Mapping[str, Any]) -> Any:
        """Owner of one ``coder list`` record: the immutable owner ID, else (older
        servers omit it) the mutable display name."""
        return raw.get("owner_id", raw.get("owner_name", raw.get("owner", "")))

    @staticmethod
    def _template_of(raw: Mapping[str, Any]) -> Any:
        """Template name out of one ``coder list`` record."""
        return raw.get("template_name", raw.get("template", ""))

    @staticmethod
    def _workspace(raw: object) -> CoderWorkspace | None:
        if not isinstance(raw, dict):
            return None
        latest = raw.get("latest_build")
        status = latest.get("status", "") if isinstance(latest, dict) else raw.get("status", "")
        values = {
            "uuid": raw.get("id", ""),
            "name": raw.get("name", ""),
            "owner": CoderClient._owner_of(raw),
            "template": CoderClient._template_of(raw),
            "status": status,
            "last_used_at": raw.get("last_used_at", ""),
        }
        if not all(isinstance(value, str) for value in values.values()):
            raise CoderClientError("Coder workspace record has an invalid shape")
        if not values["uuid"] or not values["name"]:
            return None
        return CoderWorkspace(**values)

    @staticmethod
    def _json(output: bytes, operation: str) -> Any:
        if not output.strip():
            return []
        try:
            return json.loads(output)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CoderClientError(f"Coder {operation} returned invalid data") from exc

    async def list_workspaces(self) -> tuple[CoderWorkspace, ...]:
        raw = self._json(await self._call("list", "--output", "json"), "workspace query")
        if not isinstance(raw, list):
            raise CoderClientError("Coder workspace query returned an invalid shape")
        records: list[CoderWorkspace] = []
        for value in raw:
            workspace = self._workspace(value)
            if workspace is not None:
                records.append(workspace)
        return tuple(records)

    async def get_workspace(self, name: str) -> CoderWorkspace | None:
        return next(
            (workspace for workspace in await self.list_workspaces() if workspace.name == name),
            None,
        )

    async def current_user(self) -> tuple[str, str]:
        """Return the authenticated Coder username and immutable user id."""
        owner_bytes = await self._call("whoami", "--output", "json")
        if len(owner_bytes) > 1024:
            raise CoderClientError("Coder identity response is too large")
        identity = self._json(owner_bytes, "identity query")
        if not isinstance(identity, list) or len(identity) != 1:
            raise CoderClientError("Coder identity response is invalid")
        record = identity[0]
        if not isinstance(record, dict):
            raise CoderClientError("Coder identity response is invalid")
        owner = record.get("username")
        owner_id = record.get("user_id")
        if not isinstance(owner, str) or not owner or not isinstance(owner_id, str) or not owner_id:
            raise CoderClientError("Coder identity response is invalid")
        return owner, owner_id

    async def probe(self, *, template: str, preset: str) -> dict[str, str]:
        owner, _owner_id = await self.current_user()
        raw = self._json(
            await self._call("templates", "list", "--output", "json"),
            "template query",
        )
        if not isinstance(raw, list):
            raise CoderClientError("Coder template query returned an invalid shape")
        names: set[str] = set()
        for value in raw:
            if not isinstance(value, dict):
                continue
            # Coder's table renderer wraps current JSON records under the
            # display-column label; older releases returned the record itself.
            record = value.get("Template", value)
            if isinstance(record, dict) and isinstance(record.get("name"), str):
                names.add(record["name"])
        if template not in names:
            raise CoderClientError("Coder template is unavailable")
        # Presets apply at create, the authoritative check; this validates only the template.
        _ = preset
        return {"owner": owner, "template": template}

    async def create_workspace(
        self,
        *,
        name: str,
        template: str,
        preset: str,
        stop_after_minutes: int,
    ) -> CoderWorkspace:
        workspace_name = self._validated_workspace_name(name)
        args = ["create", workspace_name, "--template", template]
        if preset:
            args.extend(("--preset", preset))
        args.extend(
            (
                "--stop-after",
                f"{stop_after_minutes}m",
                "--yes",
                "--use-parameter-defaults",
            )
        )
        await self._call(*args)
        workspace = await self.get_workspace(workspace_name)
        if workspace is None:
            raise CoderClientError("Coder did not return the created workspace")
        return workspace

    async def start_workspace(self, name: str) -> CoderWorkspace:
        workspace_name = self._validated_workspace_name(name)
        await self._call("start", workspace_name, "--yes")
        workspace = await self.get_workspace(workspace_name)
        if workspace is None:
            raise CoderClientError("Coder did not return the started workspace")
        return workspace

    async def stop_workspace(self, name: str) -> CoderWorkspace:
        await self._call("stop", self._validated_workspace_name(name), "--yes")
        workspace = await self.get_workspace(name)
        if workspace is None:
            raise CoderClientError("Coder did not return the stopped workspace")
        return workspace

    async def delete_workspace(self, name: str) -> None:
        await self._call("delete", self._validated_workspace_name(name), "--yes")

    @staticmethod
    def _validated_workspace_name(name: str) -> str:
        if not _WORKSPACE_NAME_RE.fullmatch(name):
            raise CoderClientError("Coder workspace name is invalid")
        return name
