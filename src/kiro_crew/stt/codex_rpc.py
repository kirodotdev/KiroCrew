"""A dedicated Codex app-server connection for dictation, never agent turns."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import tempfile
from typing import Any

from kiro_crew import platform_compat, sandbox

RPC_TIMEOUT_SECS = 30
CODE_LOGIN = "stt_codex_login_required"
CODE_UNAVAILABLE = "stt_codex_voice_unavailable"
CODE_FAILED = "stt_codex_failed"
CODE_SANDBOX = "stt_codex_sandbox_unavailable"
_MAX_LINE_BYTES = 1024 * 1024
_MAX_EVENTS = 1024
#: Queued by the reader when the child dies or breaks protocol. Codex events are
#: only forwarded under ``thread/realtime/``, so the child cannot forge this one.
TRANSPORT_FAILED = "kiro_crew/transport_failed"
_MINIMUM_VOICE_VERSION = (0, 163, 0)
_STRICTER_MODES = frozenset({"cc", "strict"})
_VERSION_RE = re.compile(r"kiro_crew_dictation/(\d+)\.(\d+)\.(\d+)(?:\s|$)")
_METHODS = frozenset(
    {"initialize", "account/read", "thread/start", "thread/realtime/start", "thread/realtime/stop"}
)
_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "PROGRAMDATA",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
    }
)


def sandbox_mode() -> str:
    """The tier the Codex child runs at: the operator's when stricter, else ``standard``."""
    configured = sandbox.configured_sandbox_mode()
    return configured if configured in _STRICTER_MODES else "standard"


class CodexError(RuntimeError):
    """A coded, credential-free failure safe to send to the microphone UI."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CodexRpc:
    """Own one JSONL process and reject every server request for tools or approval."""

    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_MAX_EVENTS)
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.reader: asyncio.Task[None] | None = None
        self.directory: tempfile.TemporaryDirectory[str] | None = None
        self.counter = 0
        self.failure: CodexError | None = None
        self.launcher: str | None = None

    async def start(self, executable: str) -> None:
        creating = asyncio.create_task(
            asyncio.to_thread(tempfile.TemporaryDirectory, prefix="codex-stt-")
        )
        try:
            self.directory = await asyncio.shield(creating)
            # Audio never reaches argv, the cwd holds no project, and the MCP
            # configuration is empty. The binary comes from the operator's PATH,
            # which an agent can write to, so the child runs inside the same OS
            # sandbox as an agent tool: a planted ``codex`` gains nothing an agent
            # does not already have. ``~/.codex`` is not a masked credential home,
            # so Codex's own login still works in there.
            # The operator's tier when it is stricter than ``standard``, so a host
            # that hides ``~/.aws`` from agents hides it from this child too. Never
            # looser: ``off`` and ``auto`` still get ``standard``, because ``off``
            # can mean the agent is confined by its own harness instead.
            mode = await asyncio.to_thread(sandbox_mode)
            try:
                argv, env, self.launcher = await sandbox.sandboxed_spawn_argv_async(
                    [
                        executable,
                        "app-server",
                        "--listen",
                        "stdio://",
                        "--disable",
                        "shell_tool",
                        "-c",
                        "mcp_servers={}",
                    ],
                    mode=mode,
                    env={key: value for key, value in os.environ.items() if key in _ENV_NAMES},
                    strip_python_env=True,
                    require_confinement=True,
                )
            except RuntimeError as exc:
                raise CodexError(
                    CODE_SANDBOX, "Codex dictation needs the agent sandbox on this host."
                ) from exc
            self.process = await sandbox.create_subprocess_limited(
                *argv,
                cwd=self.directory.name,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=_MAX_LINE_BYTES,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
            self.reader = asyncio.create_task(self._read())
            initialized = await self.call(
                "initialize",
                {
                    "clientInfo": {"name": "kiro_crew_dictation", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            version = _VERSION_RE.match(str(initialized.get("userAgent", "")))
            if version is None or tuple(map(int, version.groups())) < _MINIMUM_VOICE_VERSION:
                raise CodexError(
                    CODE_UNAVAILABLE, "Codex dictation needs Codex CLI 0.163.0 or later."
                )
            await self.write({"method": "initialized"})
            account = (await self.call("account/read", {})).get("account") or {}
            if account.get("type") not in {"chatgpt", "chatgptAuthTokens"}:
                raise CodexError(CODE_LOGIN, "Sign in to Codex with ChatGPT using codex login.")
        except BaseException:
            if self.directory is None and not creating.cancelled():
                self.directory = await creating
            await self.close()
            raise

    async def write(self, message: dict[str, Any]) -> None:
        proc = self.process
        if proc is None or proc.returncode is not None or proc.stdin is None:
            raise CodexError(CODE_FAILED, "The Codex dictation process stopped.")
        payload = (json.dumps(message) + "\n").encode("utf-8")
        try:
            proc.stdin.write(payload)
            await asyncio.wait_for(proc.stdin.drain(), RPC_TIMEOUT_SECS)
        except asyncio.CancelledError:
            raise
        except CodexError:
            raise
        except Exception as exc:
            raise CodexError(CODE_FAILED, "The Codex dictation connection failed.") from exc

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        if method not in _METHODS:
            raise ValueError("Dictation cannot start agent turns or append task handoffs")
        if self.failure:
            raise self.failure
        self.counter += 1
        ident = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.write({"id": ident, "method": method, "params": params})
            try:
                return await asyncio.wait_for(future, RPC_TIMEOUT_SECS)
            except TimeoutError as exc:
                raise CodexError(
                    CODE_FAILED, "Codex did not answer the dictation request."
                ) from exc
        finally:
            self.pending.pop(ident, None)

    async def _read(self) -> None:
        try:
            assert self.process is not None and self.process.stdout is not None
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("Invalid Codex protocol frame")
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(
                                CodexError(
                                    CODE_UNAVAILABLE,
                                    "This Codex build or account cannot start voice dictation.",
                                )
                            )
                        else:
                            future.set_result(message.get("result", {}))
                elif "id" in message:
                    await self.write(
                        {
                            "id": message["id"],
                            "error": {
                                "code": -32601,
                                "message": "Dictation does not execute tools or approve tasks",
                            },
                        }
                    )
                elif str(message.get("method", "")).startswith("thread/realtime/"):
                    self.events.put_nowait(message)
            raise EOFError("Codex exited")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failure = CodexError(CODE_FAILED, "The Codex dictation connection failed.")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(self.failure)
            if self.events.full():
                self.events.get_nowait()
            self.events.put_nowait({"method": TRANSPORT_FAILED, "params": {}})

    async def close(self) -> None:
        if self.reader is not None:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
            self.reader = None
        if self.process is not None:
            await platform_compat.kill_and_reap(self.process)
            self.process = None
        for future in self.pending.values():
            if not future.done():
                future.cancel()
        if self.launcher is not None:
            # The sandbox launcher file is ours; the child that read it has exited.
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.unlink, self.launcher)
            self.launcher = None
        if self.directory is not None:
            await asyncio.to_thread(self.directory.cleanup)
            self.directory = None
