"""Tests for the terminal panel's delete and list routes and the spawn reservation.

Covers ``api_terminal_delete``, the spawn reservation's ownership across
create, delete and the reaper, and ``api_terminal_list``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from terminal_handler_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_enabled_cache,
    _make_request,
    _make_session,
)

from kiro_crew import platform_compat
from kiro_crew.dashboard.handlers import terminal
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import await_until


class TestApiTerminalDelete:
    @pytest.mark.asyncio
    async def test_rejects_unauthenticated(self):
        req = _make_request(user=None)
        resp = await terminal.api_terminal_delete(req)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_session(self):
        req = _make_request(session_id="nonexistent")
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_delete(req)
        assert resp.status == 404
        mock_sel.return_value.log_api_access.assert_called_once_with(
            caller="testuser",
            operation="terminal.session.delete",
            outcome="denied",
            source="dashboard",
            resources="unknown_session",
        )

    @pytest.mark.asyncio
    async def test_opening_reservation_refusal_is_audited_without_releasing_it(self):
        registry = {"abc123": None}
        request = _make_request(registry=registry)
        with patch.object(terminal, "_sel") as audit:
            response = await terminal.api_terminal_delete(request)
        assert response.status == 409
        assert registry == {"abc123": None}
        audit.return_value.log_api_access.assert_called_once_with(
            caller="testuser",
            operation="terminal.session.delete",
            outcome="denied",
            source="dashboard",
            resources="session=abc123,reservation_in_flight=1",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("session_id", ["", "x" * 65], ids=["empty", "overlong"])
    async def test_rejects_an_id_the_create_route_could_never_have_minted(self, session_id):
        """Reject at the same bound the WS-open route applies, before the lookup.

        ``api_terminal_ws`` refuses ``not session_id or len(session_id) > 64``
        when a session is created, so an id outside that bound provably keys
        nothing in the registry. Today this route does the ``registry.pop``
        first and reports 404 — correct, but it answers "no such session" to a
        request that was malformed, and it is the only one of this file's two
        ``session_id`` readers without the guard.
        """
        sess = _make_session()
        registry = {"abc123": sess}
        req = _make_request(session_id=session_id, registry=registry)
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_delete(req)
        assert resp.status == 400
        # The refusal lands before any registry mutation.
        assert registry == {"abc123": sess}

    @pytest.mark.asyncio
    async def test_deletes_existing_session(self):
        sess = _make_session()
        registry = {"abc123": sess}
        req = _make_request(registry=registry)
        with (
            patch.object(terminal, "_kill_session", new_callable=AsyncMock) as mock_kill,
            patch.object(terminal, "_sel") as mock_sel,
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_delete(req)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["deleted"] == "abc123"
        mock_kill.assert_awaited_once_with(sess)
        assert "abc123" not in registry

    @pytest.mark.asyncio
    async def test_closes_ws_before_kill(self):
        order: list[str] = []
        ws = AsyncMock()
        ws.closed = False
        ws.close = AsyncMock(side_effect=lambda *a, **k: order.append("close"))
        sess = _make_session(ws=ws)
        registry = {"abc123": sess}
        req = _make_request(registry=registry)
        with (
            patch.object(
                terminal,
                "_kill_session",
                AsyncMock(side_effect=lambda _sess: order.append("kill")),
            ),
            patch.object(terminal, "_sel") as mock_sel,
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_delete(req)
        assert resp.status == 200
        assert order == ["close", "kill"]
        assert registry == {}


# ── api_terminal_list ──


class TestTerminalReservationOwnership:
    @pytest.fixture
    def opening(self, tmp_path, monkeypatch, event_loop):
        registry = {}
        request = _make_request(registry=registry)
        request.query = {"cwd": str(tmp_path)}
        tasks = []
        sessions = []
        fds = []
        ws_type = web.WebSocketResponse

        def make_ws():
            ws = MagicMock(spec=ws_type)
            ws.closed = False
            ws.prepare = AsyncMock()
            ws.send_str = AsyncMock()
            ws.send_bytes = AsyncMock()
            ws.close = AsyncMock()
            ws.__aiter__.return_value = []
            return ws

        def allocate():
            pair = os.pipe()
            fds.extend(pair)
            return pair

        session_type = terminal._TerminalSession
        real_kill = terminal._kill_session

        def make_session(**kwargs):
            sess = session_type(**kwargs)
            sessions.append(sess)
            return sess

        def start(coro):
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task

        async def kill(sess):
            if sess.master_fd in fds:  # wokeignore:rule=master
                with contextlib.suppress(OSError):
                    os.close(sess.master_fd)  # wokeignore:rule=master

        ws = make_ws()
        constructor = MagicMock(return_value=ws)
        pty = MagicMock()
        pty.openpty.side_effect = allocate
        spawn = AsyncMock(return_value=_make_session().proc)
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(terminal, "_get_config", lambda _: {"cwd": str(tmp_path)})
        monkeypatch.setattr(
            terminal, "_resolve_shell_with_fence_shells", lambda _: ("/bin/sh", None, {})
        )
        monkeypatch.setattr(terminal.web, "WebSocketResponse", constructor)
        monkeypatch.setattr(terminal, "_TerminalSession", make_session)
        monkeypatch.setattr(terminal, "_pty", pty)
        monkeypatch.setattr(terminal, "fcntl", MagicMock())
        monkeypatch.setattr(terminal, "termios", MagicMock())
        monkeypatch.setattr(terminal, "_pty_child_env", lambda extra: dict(extra))
        monkeypatch.setattr(terminal.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(terminal, "_kill_session", AsyncMock(side_effect=kill))
        monkeypatch.setattr(terminal, "_sel", MagicMock())

        async def drain():
            for task in tasks:
                if not task.done():
                    task.cancel()
            await await_until(
                asyncio.gather(*tasks, return_exceptions=True), what="the started tasks draining"
            )
            readers = [sess.reader_task for sess in sessions if sess.reader_task is not None]
            await await_until(
                asyncio.gather(*readers, return_exceptions=True), what="the reader tasks draining"
            )

        try:
            yield SimpleNamespace(
                request=request,
                registry=registry,
                ws=ws,
                constructor=constructor,
                make_ws=make_ws,
                start=start,
                spawn=spawn,
                pty=pty,
                fds=fds,
                real_kill=real_kill,
            )
        finally:
            try:
                event_loop.run_until_complete(drain())
            finally:
                for fd in set(fds):
                    with contextlib.suppress(OSError):
                        os.close(fd)

    @pytest.mark.parametrize("stage", ["prepare", "close"])
    @pytest.mark.asyncio
    async def test_invalid_cwd_response_cannot_retain_or_erase_a_successor(
        self,
        opening,
        monkeypatch,
        stage,
    ):
        o = opening
        valid_cwd = o.request.query["cwd"]
        o.request.query["cwd"] += "/missing"
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause(*_args, **_kwargs):
            entered.set()
            # flake-ok: pre-existing unbounded Event wait that parks the paused prepare or close step until the test releases it, moved verbatim; tracked for a de-flake pass
            await release.wait()

        if stage == "prepare":
            o.ws.prepare.side_effect = pause
        else:
            monkeypatch.setattr(
                terminal, "_close_terminal_ws_bounded", AsyncMock(side_effect=pause)
            )
        task = o.start(terminal.api_terminal_ws(o.request))
        try:
            await await_until(entered, what="the paused step being entered")
            assert o.registry == {}
            assert (await terminal.api_terminal_delete(o.request)).status == 404
            o.pty.openpty.assert_not_called()
            o.spawn.assert_not_awaited()
            o.request.query["cwd"] = valid_cwd
            replacement_ws = o.make_ws()
            o.constructor.return_value = replacement_ws
            assert await terminal.api_terminal_ws(o.request) is replacement_ws
            successor = o.registry["abc123"]
            assert successor.proc is o.spawn.return_value
        finally:
            release.set()
            await await_until(task, what="the opening handler finishing")
        assert o.registry == {"abc123": successor}
        o.spawn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unrequested_starting_directory_does_not_need_probe_capacity(
        self, opening, monkeypatch
    ):
        probe = AsyncMock(side_effect=terminal._PathProbeBusy())
        monkeypatch.setattr(terminal, "_run_path_probe", probe)
        opening.request.query = {}
        assert await terminal.api_terminal_ws(opening.request) is opening.ws
        probe.assert_not_awaited()
        opening.spawn.assert_awaited_once()

    @pytest.mark.parametrize("stage", ["prepare", "spawn"])
    @pytest.mark.asyncio
    async def test_delete_during_successful_setup_refuses_until_publication(self, opening, stage):
        o = opening
        entered, release = asyncio.Event(), asyncio.Event()
        proc = o.spawn.return_value

        async def pause(*_args, **_kwargs):
            entered.set()
            # flake-ok: pre-existing unbounded Event wait that parks the paused prepare or spawn step until the test releases it, moved verbatim; tracked for a de-flake pass
            await release.wait()
            return proc

        (o.ws.prepare if stage == "prepare" else o.spawn).side_effect = pause
        task = o.start(terminal.api_terminal_ws(o.request))
        try:
            await await_until(entered, what="the paused step being entered")
            assert (await terminal.api_terminal_delete(o.request)).status == 409
            assert (await terminal.api_terminal_ws(o.request)).status == 409
            assert o.registry == {"abc123": None}
        finally:
            release.set()
            await await_until(task, what="the opening handler finishing")
        sess = o.registry["abc123"]
        assert sess.proc is proc
        o.spawn.assert_awaited_once()
        assert (await terminal.api_terminal_delete(o.request)).status == 200
        terminal._kill_session.assert_awaited_once_with(sess)
        assert o.registry == {}

    @pytest.mark.parametrize("successor", ["opening", "published", "absent"])
    @pytest.mark.asyncio
    async def test_stale_cleanup_cannot_remove_a_successor(self, opening, successor):
        o = opening
        dead = _make_session(alive=False)
        o.registry["abc123"] = dead
        entered, release = asyncio.Event(), asyncio.Event()
        next_entered, next_release = asyncio.Event(), asyncio.Event()

        async def kill(sess):
            if sess is dead and not entered.is_set():
                entered.set()
                # flake-ok: pre-existing unbounded Event wait that parks the stale session's kill until the test releases it, moved verbatim; tracked for a de-flake pass
                await release.wait()

        async def prepare(_request):
            next_entered.set()
            # flake-ok: pre-existing unbounded Event wait that parks the successor's prepare until the test releases it, moved verbatim; tracked for a de-flake pass
            await next_release.wait()

        terminal._kill_session.side_effect = kill
        stale = o.start(terminal.api_terminal_ws(o.request))
        await await_until(entered, what="the paused step being entered")
        assert (await terminal.api_terminal_delete(o.request)).status == 200
        if successor != "absent":
            if successor == "opening":
                o.ws.prepare.side_effect = prepare
            replacement = o.start(terminal.api_terminal_ws(o.request))
            if successor == "opening":
                await await_until(next_entered, what="the successor reaching its pause")
            else:
                await await_until(replacement, what="the replacement handler finishing")
        expected = dict(o.registry)
        o.constructor.side_effect = AssertionError("stale opener attempted another handshake")
        try:
            release.set()
            assert (await await_until(stale, what="the stale opener finishing")).status == 409
            assert o.registry == expected
            assert o.spawn.await_count == (1 if successor == "published" else 0)
        finally:
            next_release.set()

    @pytest.mark.parametrize("successor", ["opening", "published", "reconnected"])
    @pytest.mark.asyncio
    async def test_reaper_rechecks_identity_and_current_eligibility(
        self,
        opening,
        monkeypatch,
        successor,
    ):
        o = opening
        first = _make_session(session_id="first", alive=False)
        # flake-ok: pre-existing real-clock stamp 2000 s in the past so the session reads as an orphan the reaper may reap, moved verbatim; tracked for a de-flake pass
        stale = _make_session(disconnect=time.monotonic() - 2000)
        o.registry.update(first=first, abc123=stale)
        entered, release = asyncio.Event(), asyncio.Event()
        next_entered, next_release = asyncio.Event(), asyncio.Event()

        async def kill(sess):
            if sess is first:
                entered.set()
                # flake-ok: pre-existing unbounded Event wait that parks the reaper's kill of the first session until the test releases it, moved verbatim; tracked for a de-flake pass
                await release.wait()

        async def prepare(_request):
            next_entered.set()
            # flake-ok: pre-existing unbounded Event wait that parks the successor's prepare until the test releases it, moved verbatim; tracked for a de-flake pass
            await next_release.wait()

        terminal._kill_session.side_effect = kill
        # flake-ok: pre-existing patch of asyncio.sleep through the handler's binding that ends the reaper loop on its second tick, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(
            terminal.asyncio,
            "sleep",
            AsyncMock(
                side_effect=[
                    None,
                    asyncio.CancelledError,
                ]
            ),
        )
        reaper = o.start(terminal.reap_orphaned_terminals(o.request.app))
        await await_until(entered, what="the paused step being entered")
        if successor == "reconnected":
            assert await terminal._replace_terminal_ws(stale, o.ws)
            assert stale.last_ws_disconnect is None
        else:
            assert (await terminal.api_terminal_delete(o.request)).status == 200
            if successor == "opening":
                o.ws.prepare.side_effect = prepare
            replacement = o.start(terminal.api_terminal_ws(o.request))
            if successor == "opening":
                await await_until(next_entered, what="the successor reaching its pause")
            else:
                await await_until(replacement, what="the replacement handler finishing")
        expected = dict(o.registry)
        terminal._kill_session.reset_mock()
        try:
            release.set()
            await await_until(reaper, what="the reaper finishing")
            assert o.registry == expected
            terminal._kill_session.assert_not_awaited()
        finally:
            next_release.set()

    @pytest.mark.parametrize("stage", ["websocket", "allocation", "windows_env"])
    @pytest.mark.asyncio
    async def test_early_setup_error_releases_reservation(self, opening, monkeypatch, stage):
        o = opening
        if stage == "websocket":
            o.constructor.side_effect = RuntimeError("setup failed")
        elif stage == "allocation":
            o.pty.openpty.side_effect = RuntimeError("setup failed")
        else:
            monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", True)
            monkeypatch.setattr(
                terminal, "_pty_child_env", MagicMock(side_effect=RuntimeError("setup failed"))
            )
        with pytest.raises(RuntimeError, match="setup failed"):
            await terminal.api_terminal_ws(o.request)
        assert o.registry == {}
        o.spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancelled_spawn_releases_reservation_and_allocated_descriptors(self, opening):
        o = opening
        entered = asyncio.Event()

        async def spawn(*_args, **_kwargs):
            entered.set()
            # flake-ok: pre-existing never-set Event that parks the fake spawn until the test cancels the opening handler, moved verbatim; tracked for a de-flake pass
            await asyncio.Event().wait()

        o.spawn.side_effect = spawn
        task = o.start(terminal.api_terminal_ws(o.request))
        await await_until(entered, what="the paused step being entered")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            # flake-ok: pre-existing unbounded await of the cancelled opening handler task to observe its CancelledError, moved verbatim; tracked for a de-flake pass
            await task
        assert o.registry == {}
        assert len(o.fds) == 2
        for fd in o.fds:
            with pytest.raises(OSError):
                os.fstat(fd)

    @pytest.mark.parametrize(
        "outcome",
        [
            "success",
            "environment_error",
            "spawn_error",
            "spawn_close_error",
            "cancel_spawn",
            "cancel_close",
            "close_error",
            "cancel_spawn_close_error",
            "cancel_close_cleanup_error",
        ],
    )
    @pytest.mark.asyncio
    async def test_setup_closes_off_loop_before_publication(self, opening, monkeypatch, outcome):
        o = opening
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        real_close = os.close
        entered, spawning = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        closed = []

        def close(fd):
            if fd not in o.fds:
                return real_close(fd)
            closed.append((fd, threading.get_ident()))
            if fd == o.fds[1]:
                loop.call_soon_threadsafe(entered.set)
            # Fail before waiting if the caller would block the event loop.
            assert threading.get_ident() != loop_thread, "setup close ran on the event loop"
            if fd == o.fds[1]:
                # flake-ok: pre-existing fixed 10 s threading wait that holds the worker descriptor close off-loop until the test releases it, moved verbatim; tracked for a de-flake pass
                assert release.wait(10), "worker close was not released"
            real_close(fd)
            if fd == o.fds[1] and outcome in {
                "close_error",
                "spawn_close_error",
                "cancel_spawn_close_error",
            }:
                raise OSError("worker close failed")

        async def spawn(*_args, **_kwargs):
            spawning.set()
            # flake-ok: pre-existing never-set Event that parks the fake spawn until the test cancels the opening handler, moved verbatim; tracked for a de-flake pass
            await asyncio.Event().wait()

        if outcome in {"spawn_error", "spawn_close_error"}:
            o.spawn.side_effect = RuntimeError("spawn failed")
        elif outcome in {"cancel_spawn", "cancel_spawn_close_error"}:
            o.spawn.side_effect = spawn
        kill_tree = AsyncMock()
        kill = AsyncMock(wraps=o.real_kill)
        if outcome == "cancel_close_cleanup_error":

            async def cleanup_then_fail(sess):
                await o.real_kill(sess)
                raise RuntimeError("cleanup failed after disposal")

            kill.side_effect = cleanup_then_fail
        with ThreadPoolExecutor(max_workers=1) as pool, monkeypatch.context() as scoped:
            scoped.setattr(terminal, "subprocess_executor", lambda: pool)
            scoped.setattr(terminal, "_kill_session", kill)
            scoped.setattr(terminal.platform_compat, "kill_process_tree_async", kill_tree)
            scoped.setattr(terminal.os, "close", close)
            if outcome == "environment_error":
                scoped.setattr(
                    terminal, "_pty_child_env", MagicMock(side_effect=RuntimeError("env failed"))
                )
            task = o.start(terminal.api_terminal_ws(o.request))
            try:
                if outcome in {"cancel_spawn", "cancel_spawn_close_error"}:
                    await await_until(spawning, what="the spawn starting")
                    task.cancel()
                await await_until(entered, what="the paused step being entered")
                assert all(
                    thread != loop_thread for _, thread in closed
                ), "setup close ran on the event loop"
                # Another request progresses while close is held off-loop, and
                # the unpublished shell still belongs to its opening handler.
                assert (await terminal.api_terminal_delete(o.request)).status == 409
                assert o.registry == {"abc123": None}
                o.ws.send_str.assert_not_awaited()
                if outcome in {"cancel_close", "cancel_close_cleanup_error"}:
                    task.cancel()
            finally:
                release.set()
                (result,) = await await_until(
                    asyncio.gather(task, return_exceptions=True), what="the task settling"
                )

            if outcome in {
                "cancel_spawn",
                "cancel_close",
                "cancel_spawn_close_error",
                "cancel_close_cleanup_error",
            }:
                assert isinstance(result, asyncio.CancelledError)
                o.ws.send_str.assert_not_awaited()
            else:
                assert result is o.ws
            worker_fd = o.fds[1]
            assert [fd for fd, _ in closed].count(worker_fd) == 1
            with pytest.raises(OSError):
                os.fstat(worker_fd)
            if outcome == "success":
                sess = o.registry["abc123"]
                assert sess.proc is o.spawn.return_value
                os.fstat(o.fds[0])
                assert [fd for fd, _ in closed] == [worker_fd]
                kill.assert_not_awaited()
                assert any(
                    json.loads(call.args[0])["type"] == "ready"
                    for call in o.ws.send_str.await_args_list
                )
            else:
                assert o.registry == {}
                assert [fd for fd, _ in closed].count(o.fds[0]) == 1
                with pytest.raises(OSError):
                    os.fstat(o.fds[0])
                kill.assert_awaited_once()
                owned_session = kill.await_args.args[0]
                if outcome in {"cancel_close", "close_error", "cancel_close_cleanup_error"}:
                    assert owned_session.proc is o.spawn.return_value
                    assert [call.args for call in kill_tree.await_args_list] == [
                        (o.spawn.return_value.pid, platform_compat.SIGHUP),
                        (o.spawn.return_value.pid, platform_compat.SIGTERM),
                    ]
                    o.spawn.return_value.wait.assert_awaited_once()
                else:
                    assert owned_session.proc is None
                    kill_tree.assert_not_awaited()
                assert not any(
                    json.loads(call.args[0])["type"] == "ready"
                    for call in o.ws.send_str.await_args_list
                )
            if outcome in {"spawn_error", "spawn_close_error"}:
                # The close that follows a failed spawn cannot replace its cause.
                assert {"type": "error", "message": "spawn failed"} in [
                    json.loads(call.args[0]) for call in o.ws.send_str.await_args_list
                ]

    @pytest.mark.parametrize("descriptor", ["worker", "controller"])
    @pytest.mark.parametrize("close_state", ["queued", "running"])
    @pytest.mark.parametrize("cancel", [False, True])
    @pytest.mark.asyncio
    async def test_setup_deadline_keeps_close_owned(
        self,
        opening,
        monkeypatch,
        descriptor,
        close_state,
        cancel,
    ):
        o = opening
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        real_close = os.close
        queued = asyncio.Event()
        expire = asyncio.Event()
        release = threading.Event()
        submitted, closed = [], []
        real_wait_for = asyncio.wait_for

        async def close_deadline(awaitable, timeout):
            if timeout < 900 or not submitted:
                return await real_wait_for(awaitable, timeout)
            # Expire the shield only after the close has entered its held state.
            # Repeated cancellation cannot cancel the submitted executor work.
            try:
                await real_wait_for(expire.wait(), 5)
            except asyncio.CancelledError:
                awaitable.cancel()
                raise
            return await real_wait_for(awaitable, 0)

        def close(fd):
            if fd not in o.fds:
                return real_close(fd)
            if threading.get_ident() == loop_thread:
                queued.set()
            assert threading.get_ident() != loop_thread, "setup close ran on the event loop"
            closed.append(fd)
            target = o.fds[1 if descriptor == "worker" else 0]
            if close_state == "running" and fd == target:
                loop.call_soon_threadsafe(queued.set)
                # flake-ok: pre-existing fixed 10 s threading wait that holds the running descriptor close off-loop until the test releases it, moved verbatim; tracked for a de-flake pass
                assert release.wait(10), "running close was not released"
            real_close(fd)

        def occupy():
            # flake-ok: pre-existing fixed 10 s threading wait that occupies the only pool worker so the close stays queued, moved verbatim; tracked for a de-flake pass
            assert release.wait(10), "queued close was not released"

        if descriptor == "controller":
            o.spawn.side_effect = RuntimeError("spawn failed")
        kill = AsyncMock(wraps=o.real_kill)
        with ThreadPoolExecutor(max_workers=1) as pool, monkeypatch.context() as scoped:
            real_submit = pool.submit

            def submit(fn, *args, **kwargs):
                target = o.fds[1 if descriptor == "worker" else 0] if o.fds else None
                if fn is close and args == (target,):
                    if close_state == "queued":
                        real_submit(occupy)
                    future = real_submit(fn, *args, **kwargs)
                    submitted.append(future)
                    if close_state == "queued":
                        queued.set()
                    return future
                return real_submit(fn, *args, **kwargs)

            scoped.setattr(pool, "submit", submit)
            scoped.setattr(terminal, "subprocess_executor", lambda: pool)
            scoped.setattr(terminal, "_kill_session", kill)
            scoped.setattr(terminal.platform_compat, "kill_process_tree_async", AsyncMock())
            scoped.setattr(terminal.os, "close", close)
            scoped.setattr(terminal, "_TERMINAL_FD_CLOSE_TIMEOUT_S", 997.0)
            # flake-ok: pre-existing patch of asyncio.wait_for via the handler's binding that holds the close deadline until the test expires it, moved verbatim; tracked for a de-flake pass
            scoped.setattr(terminal.asyncio, "wait_for", close_deadline)
            task = o.start(terminal.api_terminal_ws(o.request))
            try:
                await await_until(queued, what="the setup close being queued")
                assert len(submitted) == 1, "setup close was not submitted off-loop"
                # Each cancellation callback runs before this loop-turn marker;
                # the occupied worker cannot race ahead and mask a lost close.
                for _ in range(2 if cancel else 0):
                    task.cancel()
                    cancelled = asyncio.Event()
                    loop.call_soon(cancelled.set)
                    await await_until(cancelled, what="the cancellation's loop turn")
                    assert not submitted[0].cancelled()
                    assert not task.done()
                assert (await terminal.api_terminal_delete(o.request)).status == 409
                expire.set()
                # Observation must not cancel-and-wait for the old unbounded
                # implementation: a regression should fail, not hang the worker.
                (result,) = await await_until(
                    asyncio.gather(task, return_exceptions=True),
                    what="setup finishing at the close deadline",
                )
                assert o.registry == {}
                assert not submitted[0].done()
                assert not release.is_set()
                assert isinstance(result, asyncio.CancelledError) if cancel else result is o.ws
                kill.assert_awaited_once()
                if descriptor == "worker":
                    assert kill.await_args.args[0].proc is o.spawn.return_value
                    o.spawn.return_value.wait.assert_awaited_once()
                frames = [json.loads(call.args[0]) for call in o.ws.send_str.await_args_list]
                errors = [frame["message"] for frame in frames if frame["type"] == "error"]
                if not cancel:
                    # A timed-out close carries no text, yet the owner learns why.
                    assert errors == [
                        (
                            "Failed to start terminal: TimeoutError"
                            if descriptor == "worker"
                            else "spawn failed"
                        )
                    ]
            finally:
                expire.set()
                release.set()
                await await_until(
                    asyncio.gather(task, return_exceptions=True), what="the setup task settling"
                )
                await await_until(
                    loop.run_in_executor(pool, lambda: None), what="the pool worker freeing up"
                )
            assert o.registry == {}
            assert sorted(closed) == sorted(o.fds)
            for fd in o.fds:
                with pytest.raises(OSError):
                    os.fstat(fd)
            kill.assert_awaited_once()
            assert not any(
                json.loads(call.args[0])["type"] == "ready"
                for call in o.ws.send_str.await_args_list
            )

    @pytest.mark.parametrize("stage", ["error_response", "published"])
    @pytest.mark.parametrize("windows", [False, True])
    @pytest.mark.asyncio
    async def test_retired_opener_cannot_release_a_later_reservation(
        self,
        opening,
        monkeypatch,
        stage,
        windows,
    ):
        from kiro_crew import conpty

        o = opening
        entered, release = asyncio.Event(), asyncio.Event()
        next_entered = asyncio.Event()
        win_spawn = MagicMock()
        win_spawn.return_value.read.return_value = b""
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", windows)
        monkeypatch.setattr(conpty, "WindowsPty", win_spawn)

        async def pause(*_args):
            entered.set()
            # flake-ok: pre-existing unbounded Event wait that parks the first opener's send or receive until the test releases it, moved verbatim; tracked for a de-flake pass
            await release.wait()

        async def receive():
            # flake-ok: pre-existing await of the parking fake that holds the receive iterator until the test releases it, moved verbatim; tracked for a de-flake pass
            await pause()
            if False:
                yield

        async def prepare(_request):
            next_entered.set()
            # flake-ok: pre-existing never-set Event that parks the successor's prepare until fixture teardown cancels it, moved verbatim; tracked for a de-flake pass
            await asyncio.Event().wait()

        if stage == "error_response":
            o.spawn.side_effect = RuntimeError("spawn failed")
            win_spawn.side_effect = RuntimeError("spawn failed")
            o.ws.send_str.side_effect = pause
        else:
            o.ws.__aiter__.side_effect = receive
        first = o.start(terminal.api_terminal_ws(o.request))
        await await_until(entered, what="the paused step being entered")
        if stage == "published":
            assert (await terminal.api_terminal_delete(o.request)).status == 200
        else:
            assert o.registry == {}
        next_ws = o.make_ws()
        next_ws.prepare.side_effect = prepare
        o.constructor.return_value = next_ws
        o.start(terminal.api_terminal_ws(o.request))
        await await_until(next_entered, what="the successor reaching its pause")
        release.set()
        await await_until(first, what="the first handler finishing")
        assert o.registry == {"abc123": None}


class TestApiTerminalList:
    @pytest.mark.asyncio
    async def test_rejects_unauthenticated(self):
        req = _make_request(user=None)
        resp = await terminal.api_terminal_list(req)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_returns_empty_list(self):
        req = _make_request()
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_list(req)
        body = json.loads(resp.body)
        assert body == {"enabled": True, "sessions": []}

    @pytest.mark.asyncio
    async def test_lists_sessions_with_details(self):
        ws = MagicMock()
        ws.closed = False
        sess = _make_session(session_id="s1", alive=True, ws=ws)
        sess.cols = 120
        sess.rows = 40
        registry = {"s1": sess}
        req = _make_request(registry=registry)
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_list(req)
        body = json.loads(resp.body)
        assert len(body["sessions"]) == 1
        s = body["sessions"][0]
        assert s["session_id"] == "s1"
        assert s["pid"] == UNALLOCATABLE_PID
        assert s["alive"] is True
        assert s["cols"] == 120
        assert s["rows"] == 40
        assert s["connected"] is True

    @pytest.mark.asyncio
    async def test_shows_disconnected_session(self):
        sess = _make_session(session_id="s1", alive=True, ws=None)
        registry = {"s1": sess}
        req = _make_request(registry=registry)
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_list(req)
        body = json.loads(resp.body)
        assert body["sessions"][0]["connected"] is False
