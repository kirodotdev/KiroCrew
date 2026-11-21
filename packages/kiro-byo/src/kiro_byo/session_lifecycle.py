"""ACP session registry, cancellation and retirement lifecycle.

Live sessions are admitted by the ACP host; their runtime resources may only
close once the previous turn has quiesced.  Never infer that a completed
model turn commits or closes a KiroCrew-owned task.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .acp_session_state import Session
from .errors import SessionBusyError


class SessionLifecycleMixin:
    def _get_session(self, session_id: str) -> Session | None:
        with self._sessions_lock:
            return self.sessions.get(session_id)

    def _signal_cancel(self, session: "Session", source: str) -> None:
        """Set a session's cancel event while recording *why* it fired.

        Every path that interrupts an in-flight turn (client cancel, session
        replacement, close/delete, runtime teardown, process shutdown) MUST go
        through here so the terminal TurnTelemetry row carries a labelled
        ``cancel_source`` instead of an empty string. Only the first source in a
        teardown wins: a later shutdown sweep must not overwrite the specific
        reason that already explained the cancel.
        """
        emit = False
        now = time.perf_counter()
        with session.state_lock:
            if not session.cancel_source:
                session.cancel_source = source
                if session.cancel_requested_at <= 0.0:
                    session.cancel_requested_at = now
                emit = True
            already_set = session.cancel_event.is_set()
            session.cancel_event.set()
        if emit and not already_set:
            active = bool(getattr(session, "active_turn", False))
            started = float(getattr(session, "turn_started_at", 0.0) or 0.0)
            elapsed_ms = max(0.0, (now - started) * 1000.0) if started > 0.0 else -1.0
            self._runtime_event(
                "control.cancel.requested",
                session,
                data={"source": source, "active_turn": active, "elapsed_ms": elapsed_ms},
            )

    def _put_session(self, session: Session, *, restoring: bool = False) -> None:
        """Atomically replace an idle session; never cancel a running turn.

        The old turn_lock is acquired non-blockingly *while* holding the
        registry lock. A prompt that fetched the old object but has not yet
        acquired its turn_lock sees retired=True after acquisition and refuses
        to execute. No MCP/native runtime is closed before ownership is proven.
        """
        old: Session | None = None
        old_locked = False
        with self._sessions_lock:
            claimed = session.session_id in self._restoring_sessions
            if session.session_id in self._retiring_sessions or claimed != restoring:
                raise SessionBusyError("session lifecycle transition in progress")
            old = self.sessions.get(session.session_id)
            if old is not None and old is not session:
                if old.retired or old.active_turn or (old.replacing and not restoring):
                    raise SessionBusyError("session has an active turn; replacement refused")
                old_locked = old.turn_lock.acquire(blocking=False)
                if not old_locked:
                    raise SessionBusyError("session has an active turn; replacement refused")
                old.retired = True
            self.sessions[session.session_id] = session
        if old is not None and old is not session:
            try:
                self._signal_cancel(old, "session_replaced")
                if old.mcp is not None:
                    old.mcp.close()
                if old.native is not None:
                    old.native.close()
            finally:
                if old_locked:
                    old.turn_lock.release()

    def _pop_session(self, session_id: str) -> Session | None:
        with self._sessions_lock:
            if session_id in self._restoring_sessions or session_id in self._retiring_sessions:
                raise SessionBusyError("session lifecycle transition in progress")
            old = self.sessions.pop(session_id, None)
            if old is not None:
                old.retired = True
                self._retiring_sessions[session_id] = old
            return old

    def _quiesce_and_close_runtime(
        self, session: "Session", *, timeout: float = 30.0,
        on_quiesced: Callable[[], None] | None = None,
        source: str = "runtime_quiesce",
    ) -> bool:
        """Cancel then close only after exclusive ownership of the turn_lock.

        A timed-out close is deferred, NEVER forced; retirement blocks re-load
        of the same ID until the old runtime and session data are finalized.
        """
        self._signal_cancel(session, source)
        acquired = session.turn_lock.acquire(timeout=timeout)
        if not acquired:
            self.trace(f"runtime close deferred: turn still active session={session.session_id}")
            worker = threading.Thread(
                target=self._finish_deferred_runtime_close,
                args=(session, on_quiesced),
                name="kiro-byo-session-retirement",
                daemon=True,
            )
            with self._sessions_lock:
                self._retirement_workers.add(worker)
            worker.start()
            return False
        try:
            self._close_quiesced_runtime(session, on_quiesced)
        finally:
            session.turn_lock.release()
        return True

    def _finish_deferred_runtime_close(
        self, session: "Session", on_quiesced: Callable[[], None] | None,
    ) -> None:
        # This Session is already retired and can never admit another turn.
        try:
            with session.turn_lock:
                self._close_quiesced_runtime(session, on_quiesced)
        finally:
            with self._sessions_lock:
                self._retirement_workers.discard(threading.current_thread())

    def _close_quiesced_runtime(
        self, session: "Session", on_quiesced: Callable[[], None] | None,
    ) -> None:
        try:
            if on_quiesced is not None:
                on_quiesced()
        finally:
            try:
                if session.mcp is not None:
                    session.mcp.close()
                if session.native is not None:
                    session.native.close()
            finally:
                with self._sessions_lock:
                    if self._retiring_sessions.get(session.session_id) is session:
                        self._retiring_sessions.pop(session.session_id, None)

    def _handle_session_close_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        try:
            closed = self._pop_session(session_id)
        except SessionBusyError as exc:
            self.error(req_id, -32002, str(exc))
            return
        if closed is not None:
            self._signal_cancel(closed, "client_session_close")
            self._clear_steers(closed)

            def finalize_close() -> None:
                # Called only while holding the retired session's turn_lock.
                self._shutdown_agent_runtime(closed)
                self._persist_session(closed)

            self._quiesce_and_close_runtime(
                closed, on_quiesced=finalize_close, source="client_session_close",
            )
        self.result(req_id, {})

    def _handle_session_delete_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        try:
            closed = self._pop_session(session_id)
        except SessionBusyError as exc:
            self.error(req_id, -32002, str(exc))
            return
        if closed is not None:
            self._signal_cancel(closed, "client_session_delete")
            self._clear_steers(closed)

            def finalize_delete() -> None:
                self._shutdown_agent_runtime(closed)
                self._delete_persisted_session(session_id)

            self._quiesce_and_close_runtime(
                closed, on_quiesced=finalize_delete, source="client_session_delete",
            )
        else:
            self._delete_persisted_session(session_id)
        self.result(req_id, {})
