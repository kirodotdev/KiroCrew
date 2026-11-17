"""One claude session's own setup on a claude-agent-acp process other sessions share.

On ``AcpClient`` a claude session owns its process, so its setup and its checks ride
the process: the settings seed is written at spawn, the starting-mode pin talks on
the process's own pipe, and a failed pin ends the process. On the shared runtime
(:class:`kiro_crew.acp.harness.claude.ClaudeHarness`) one process serves several
sessions, so each of those steps has to be one session's, not the process's.

:class:`ClaudeSharedSession` is that session's state. It is a ``ClaudeAcpClient``
that never starts a process: the seed, the side-session deny rules, the floor
refusal, the envelope, the pin and the seed's teardown are the same methods the
per-session client runs, so the two paths cannot drift. What differs is where the
two transport steps of the pin go, and that is the whole of this class:

* ``_claude_set_mode`` sends ``session/set_mode`` for THIS session on the shared
  connection;
* ``_claude_stop_session`` ends THIS session with ``session/close`` and leaves the
  process, which other sessions are running on, alone.

Built per session by :meth:`ClaudeHarness.prepare_session`, in the session's own
working directory, so every session writes the seed its own folder reads.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

from kiro_crew.acp.client_claude import ClaudeAcpClient
from kiro_crew.acp.harness import claude as claude_mod
from kiro_crew.acp.session_handle_runtime_contract import AcpRequestTimeout, AcpRuntimeError
from kiro_crew.acp.spec_tools_allowlists import (
    session_allowlist_spec,
    spec_shell_patterns,
    spec_write_globs,
)
from kiro_crew.acp.transport_errors import AcpError, AcpTimeoutError
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, METHOD_SET_MODE
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV


class ClaudeSharedSession(ClaudeAcpClient):
    """The state of one claude session on a shared claude-agent-acp process."""

    # The three overrides below carry ``type: ignore[override]``: the base members are
    # owner functions whose ``self`` is annotated with the base class, which mypy reads
    # as a narrower receiver than a method's. The signatures are the same.

    def __init__(
        self,
        *,
        work_dir: str | Path,
        model: str,
        agent: str | None,
        session_key: str,
        channel_id: str,
        permission_mode: str | None = None,
        side_read_only: bool = False,
        include_memory: bool = True,
    ) -> None:
        super().__init__(
            work_dir=work_dir,
            model=model or None,
            agent=agent or "kirocrew",
            session_key=session_key or None,
            channel_id=channel_id or None,
            acp_backend=ACP_BACKEND_CLAUDE,
            permission_mode=permission_mode,
            side_read_only=side_read_only,
            include_memory=include_memory,
        )
        self._shared_send: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None
        self._shared_close: Callable[[], Awaitable[None]] | None = None
        # Set by :meth:`adopt_allowlist_spec`; the envelope is refused until then.
        self._spec_allowlists_adopted = False

    async def prepare(self, argv: list[str]) -> None:
        """Write this session's seed and side-session deny rules, before its request.

        The same steps, in the same order, as ``harness.claude.prepare_claude_session``
        runs for a session that owns its process, except the MCP array: on the shared
        runtime the runtime's mirror builds it, told by
        :meth:`ClaudeHarness.permission_surface_owned` whether this seed governs the
        session. The exclusion record the envelope reads is taken here, beside the
        seed that decided it.
        """
        self._agent_version_read = False
        self._claude_adapter_argv = tuple(argv)
        installed = await asyncio.to_thread(claude_mod._claude_adapter_installed_version, argv)
        self._claude_adapter_disk_version = (
            installed or claude_mod._claude_adapter_handshake_versions.get(tuple(argv), "")
        )
        await self._seed_session_settings()
        await self._prepare_claude_side_read_denies()
        excludes_local, inline_settings = self._local_settings_exclusion()
        self._session_mcp_excludes_local = excludes_local
        self._session_mcp_inline_settings = inline_settings
        self._refuse_claude_session_below_floor_before_spawn(installed)

    async def adopt_allowlist_spec(self, spec: dict[str, Any] | None) -> None:
        """Take the spec's ``toolsSettings`` shell and write allowlists from *spec*.

        Claude never reads them: the per-chat client takes them in its MCP-array step
        (``client_mcp_array``) from the spec that step projected, which the runtime's
        mirror replaces here, so *spec* is the one the mirror's projection read. A
        withheld array carries none, and the native shell and write tools still run,
        so the spec is read once through the same resolution that step falls back
        on. The envelope's ask rules (``session_meta``) and this session's permission
        requests (``ClaudeHarness.bind_session_handle``) both read what is set here.
        """
        if spec is None:
            spec = await asyncio.to_thread(
                session_allowlist_spec, self._agent or "kirocrew", self._work_dir
            )
        cwd = str(self._work_dir or "")
        self._spec_shell_allowlist = spec_shell_patterns(spec)
        self._spec_write_allowlist = spec_write_globs(spec, cwd)
        self._spec_write_cwd = cwd
        self._spec_allowlists_adopted = True

    def session_identity_env(self, stub_token: str) -> dict[str, str]:
        """This session's identity, as a per-chat client puts it on its process env
        (``AcpClient._apply_session_identity_env``): the stub token that names its
        brokered servers, and its session key when it has one."""
        env: dict[str, str] = {}
        if stub_token:
            env[STUB_SESSION_TOKEN_ENV] = stub_token
        if self._session_key:
            env["KIROCREW_SESSION_KEY"] = self._session_key
        return env

    def bind_started(
        self,
        session_id: str,
        agent_version: str,
        *,
        send_request: Callable[[str, dict[str, Any]], Awaitable[Any]],
        close_session: Callable[[], Awaitable[None]],
    ) -> None:
        """Bind the session the request just created, and the two ways to reach it."""
        self._session_id = session_id
        self._agent_version = agent_version
        self._agent_version_read = True
        self._shared_send = send_request
        self._shared_close = close_session

    async def _claude_set_mode(self, session_id: str | None, mode_id: str) -> None:  # type: ignore[override]
        """``session/set_mode`` for this session, on the shared connection.

        The shared connection raises ``AcpRequestTimeout`` and ``AcpRuntimeError``;
        they are raised here as the per-chat client's own request raises them
        (``AcpTimeoutError``, ``AcpError``), which the starting-mode pin reads as a
        failed pin: it then closes this session and refuses it.
        """
        if self._shared_send is None:
            raise AcpError("the shared claude session is not bound to its connection yet")
        try:
            await self._shared_send(METHOD_SET_MODE, {"sessionId": session_id, "modeId": mode_id})
        except AcpRequestTimeout as exc:
            raise AcpTimeoutError(message=f"session/set_mode timed out: {exc}") from exc
        except AcpRuntimeError as exc:
            raise AcpError(str(exc)) from exc

    async def _claude_stop_session(self) -> None:  # type: ignore[override]
        """End THIS session only: other sessions share the process."""
        if self._shared_close is not None:
            await self._shared_close()

    async def _kill_process(self, *, force: bool = False, exit_grace: float = 0.0) -> None:  # type: ignore[override]
        """Never the process: this state owns none. A step that would end it ends
        this session instead."""
        await self._claude_stop_session()
