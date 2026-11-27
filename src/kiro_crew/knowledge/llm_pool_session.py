"""The knowledge pool's worker for KAS: each chunk on the gateway's background runtime.

:class:`~kiro_crew.knowledge.llm_pool.LLMPool` builds a :class:`SessionWorker` for a
backend it cannot spawn its own client for (KAS needs ``AcpRuntime``) when the
gateway's session manager runs its shared background runtime on that backend. The
pool imports this module when it builds one.

The pool's names are read through the ``llm_pool`` module at call time, so a patch
on that module reaches this one too.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from kiro_crew.agent_spec_injections import WRITE_BACK_CAUSE
from kiro_crew.knowledge import llm_pool as _pool

logger = logging.getLogger("kiro_crew.knowledge.llm_pool")

#: The SEL source a refused tool request on a :class:`SessionWorker` turn is logged under.
SESSION_WORKER_SEL_SOURCE = "knowledge_extraction"


class KnowledgeKasSignedOut(_pool.KnowledgeHarnessUnavailable):
    """The pool's KAS call failed because Kiro is not signed in.

    The KAS twin of :class:`~kiro_crew.knowledge.llm_pool.KnowledgeKiroCliSignedOut`,
    so a signed-out KAS stops the batch the same way: the ingest fails with this
    text and nothing is stored.
    """

    code = "knowledge_kas_signed_out"

    def __init__(self) -> None:
        from kiro_crew.agent_sdk.host_auth import KAS_TERMINAL_SIGN_IN

        # Skip the parent's LTS text: this is not a build refusal.
        RuntimeError.__init__(
            self,
            "Knowledge Library extraction runs on Kiro Agent, and Kiro is not signed "
            f"in here, so nothing was added. {KAS_TERMINAL_SIGN_IN}, then add the "
            "document again (a watched-folder file: press Retry on the Knowledge "
            "page). Documents already in the library stay searchable.",
        )


class KnowledgeHostMemoryCritical(_pool.KnowledgeHarnessUnavailable):
    """Extraction was not started because this computer's memory is critical.

    Extraction is bulk background work, so the background memory gate refuses it
    then. Raised as a harness refusal so the batch stops, the ingest fails with this
    text and nothing is stored or hashed: the document is extracted in full when it
    is added or retried later.
    """

    code = "knowledge_host_memory_critical"

    def __init__(self) -> None:
        # Skip the parent's LTS text: this is not a build refusal.
        RuntimeError.__init__(
            self,
            "Knowledge Library extraction did not start because this computer is low "
            "on memory, so nothing was added. Add the document again once memory frees "
            "up (a watched-folder file: press Retry on the Knowledge page). Documents "
            "already in the library stay searchable.",
        )


def _kas_signed_out(exc: BaseException) -> bool:
    """Whether a KAS call failed because Kiro is not signed in (the ACP auth check)."""
    # The SDK's delegation, not kiro_crew.acp: the agent-sdk-boundary gate refuses
    # a new ACP import edge here.
    from kiro_crew.agent_sdk.drivers.acp import is_auth_failure_output

    if any(cls.__name__ == "AcpAuthRequired" for cls in type(exc).__mro__):
        return True
    return is_auth_failure_output(str(exc))


def _lite_spec_verdict(backend: str) -> bool | None:
    """The background agent's spec as *backend* is sent it: True mounts nothing.

    ``authored_spec_toolless_verdict`` on KAS judges the ``kirocrew-lite`` view the
    KAS projection puts on the wire at each ``session/new``. False mounts something;
    None is missing or unreadable. Blocking; never raises.
    """
    from kiro_crew.agent_sdk.agent_specs import authored_spec_toolless_verdict
    from kiro_crew.session_parts.policy import BACKGROUND_AGENT

    try:
        return authored_spec_toolless_verdict(BACKGROUND_AGENT, backend=backend)
    except Exception:
        logger.warning("SessionWorker: background agent spec unreadable", exc_info=True)
        return None


async def _lite_spec_toolless(sessions: Any, backend: str) -> bool:
    """Whether the background agent's spec, as *backend* is sent it, mounts nothing.

    A spec that mounts something is repaired once and judged again. The repair is
    ``background_turns.repair_lite_spec``, the one every background turn uses: it
    counts the rewrite and resets ``_bg`` and ``_consolidate``, which may have loaded
    the old file. A missing or unreadable spec is not confirmed.
    """
    verdict = await asyncio.to_thread(_lite_spec_verdict, backend)
    if verdict is False:
        from kiro_crew.llm_helpers_parts.background_turns import repair_lite_spec

        await repair_lite_spec(sessions)
        verdict = await asyncio.to_thread(_lite_spec_verdict, backend)
    return verdict is True


def _registered_entry_mounts_nothing(session: Any) -> bool:
    """Whether the agent entry this KAS session registered at ``session/new`` mounts nothing.

    Read from the batch the session holds (``kas_tool_view``), not the file, so a
    spec rewritten after the check above cannot slip into the session. Nothing
    means ``tools`` is ``[]`` (KAS grants only what ``tools`` names), no
    ``mcpServers`` and no auto-approved or Crew-answered capability. A session
    with no registered batch, or one running a mode Crew did not register,
    answers False.
    """
    from kiro_crew.agent_sdk.kas_tool_view import kas_tool_view

    try:
        view = kas_tool_view(session)
    except Exception:
        logger.warning("SessionWorker: session projection unreadable", exc_info=True)
        return False
    entry = view.get("entry") if isinstance(view, dict) else None
    if not isinstance(entry, dict):
        return False
    return (
        entry.get("tools") == []
        and not entry.get("servers")
        and not entry.get("approved")
        and not entry.get("crew_answered")
    )


def _sessions_closing(sessions: Any) -> bool:
    """Whether the session manager stopped admitting work (``close_all`` or an update).

    ``close_all()`` closes admission before the knowledge pool's own ``shutdown()``
    runs, and from then on every background session is refused. ``is True``, so a
    manager without the attribute (or a mock's stand-in) never reads as closing.
    """
    return getattr(sessions, "admission_closed", None) is True


async def _confirm_session(session: Any) -> None:
    """``run_bg_oneliner``'s ``confirm_session``: refuse a session that mounts anything."""
    if not _registered_entry_mounts_nothing(session):
        unconfirmed = _pool.KnowledgeSpecUnconfirmed
        raise unconfirmed(
            f"{unconfirmed.code}: the background session KAS opened registered an agent "
            "that mounts a tool, a server or a grant; refusing to send document text to "
            f"it. {WRITE_BACK_CAUSE}. The knowledge pool tries again after "
            f"{int(_pool.SPEC_REFUSAL_COOLDOWN_SECS)} seconds"
        )


class SessionWorker(_pool.Worker):
    """Runs each chunk on the gateway's shared background runtime (``kirocrew-lite``).

    For a backend this pool cannot spawn a client for (KAS: it needs
    ``AcpRuntime``). Each call is one ``run_bg_oneliner``: a fresh session on the
    one shared background process, tool requests rejected and audited, the session
    destroyed afterwards. So the pool's width is how many chunks run at once, and no
    transcript builds up between calls.

    Every chunk goes to untrusted model input, so before each call the background
    agent's spec is judged as this backend is sent it, and the runtime is checked to
    still run this backend. Either failing refuses the call; the spec refusal stops
    the ingest (:class:`~kiro_crew.knowledge.llm_pool.KnowledgeSpecUnconfirmed`).
    """

    def __init__(self, sessions: Any, *, backend: str, effort: str | None = None) -> None:
        self._sessions = sessions
        self._backend = backend
        self._effort = effort
        self._closed = False
        # Set by a spec refusal during a call: the worker then reports dead, so the
        # pool replaces it, and the refusal the pool recorded (``LLMPool.send``)
        # holds the replacement back for the cooldown.
        self._refused = False
        # Calls in flight. Each is shielded from its caller's cancellation (see
        # ``send_message``), so shutdown() is what ends one early.
        self._calls: set[asyncio.Task[str]] = set()
        # Models whose sessions took no effort write: each chunk gets a new session,
        # and asking again would log the same warning once per chunk of a folder
        # sync. shortcut: a failed write is remembered too, so that model keeps the
        # provider default until the pool replaces this worker; retry failures if a
        # transient write error ever matters.
        self._effort_unset: set[str | None] = set()

    async def start(self) -> None:
        self._closed = False
        await self._check()

    def _refuse(self, exc: Exception) -> None:
        self._refused = True

    async def _check(self) -> None:
        if _sessions_closing(self._sessions):
            # The gateway is closing: no reply comes, and an empty one would be stored.
            raise _pool.LLMPoolClosed()
        if not _pool._session_path_serves(self._backend, self._sessions):
            # The background backend changed since this worker was built; the
            # pool replaces a worker that reports dead (``is_alive``).
            raise RuntimeError(
                f"the background runtime no longer runs {_pool._label(self._backend)}; "
                "the knowledge pool starts a new worker"
            )
        if not await _lite_spec_toolless(self._sessions, self._backend):
            unconfirmed = _pool.KnowledgeSpecUnconfirmed
            raise unconfirmed(
                f"{unconfirmed.code}: the background agent's spec could not be "
                f"confirmed to mount nothing on {_pool._label(self._backend)} after Kiro "
                f"Crew's repair; refusing to send document text to it. {WRITE_BACK_CAUSE}. "
                f"The knowledge pool tries again after {int(_pool.SPEC_REFUSAL_COOLDOWN_SECS)} "
                "seconds"
            )

    async def _confirm_session(self, session: Any, model: str | None) -> None:
        """Confirm the tool ban, then apply the pool's effort for the session's *model*."""
        await _confirm_session(session)
        if self._effort is None or model in self._effort_unset:
            return
        applied = await _pool._apply_effort_to(
            session, self._backend, self._effort, who="SessionWorker"
        )
        if applied is None:
            self._effort_unset.add(model)

    async def send_message(self, prompt: str, timeout: float = _pool.DEFAULT_TIMEOUT) -> str:
        if self._closed:
            raise _pool.LLMPoolClosed()
        try:
            await self._check()
        except _pool.KnowledgeSpecUnconfirmed as exc:
            self._refuse(exc)
            raise
        from kiro_crew.agent import _KNOWLEDGE_SYSTEM_PROMPT
        from kiro_crew.llm_helpers import run_bg_oneliner

        model = await asyncio.to_thread(_pool._knowledge_spec_model)
        # kirocrew-lite carries no prompt of the knowledge agent's, so its system
        # prompt leads the message. Each extraction prompt is self-contained.
        call = asyncio.ensure_future(
            run_bg_oneliner(
                self._sessions,
                f"{_KNOWLEDGE_SYSTEM_PROMPT}\n\n{prompt}",
                model=model,
                sel_source=SESSION_WORKER_SEL_SOURCE,
                timeout=timeout,
                # The check above reads the file; this one reads what the session
                # itself registered, before any document text is sent on it.
                confirm_session=functools.partial(self._confirm_session, model=model),
            )
        )
        self._calls.add(call)
        call.add_done_callback(self._calls.discard)
        # Shielded, as the decisions lane does: cancelling ``run_bg_oneliner`` while
        # it acquires its session can kill the shared runtime's respawn or leak a sent
        # ``session/new``. An abandoned call ends under *timeout* and destroys its
        # session itself. shortcut: until it ends, it runs beside the pool's width;
        # bound it by the width if abandoned calls ever pile up.
        try:
            return await asyncio.shield(call)
        except asyncio.CancelledError:
            if self._closed and call.cancelled():
                # shutdown() ended the call, not our caller: no reply comes.
                raise _pool.LLMPoolClosed() from None
            raise
        except Exception as exc:
            # Closing, signed out, or refused by the memory gate: stop the batch,
            # instead of storing every chunk with no entities. Closing comes first:
            # close_all() refuses the session before the pool's shutdown() runs.
            from kiro_crew.llm_helpers import BackgroundAdmissionRefused

            if _sessions_closing(self._sessions):
                raise _pool.LLMPoolClosed() from exc
            if isinstance(exc, _pool.KnowledgeSpecUnconfirmed):
                # The session registered something (``_confirm_session``).
                self._refuse(exc)
                raise
            if isinstance(exc, BackgroundAdmissionRefused):
                raise KnowledgeHostMemoryCritical() from exc
            if _kas_signed_out(exc):
                raise KnowledgeKasSignedOut() from exc
            raise

    async def shutdown(self) -> None:
        self._closed = True
        calls = list(self._calls)
        for call in calls:
            call.cancel()
        if calls:
            # Each call destroys its own session in its ``finally``.
            await asyncio.gather(*calls, return_exceptions=True)

    def is_alive(self) -> bool:
        return (
            not self._closed
            and not self._refused
            and _pool._session_path_serves(self._backend, self._sessions)
        )

    async def reset_conversation(self) -> None:
        """Nothing to drop: every call runs in a session of its own."""
        self.calls_since_reset = 0
