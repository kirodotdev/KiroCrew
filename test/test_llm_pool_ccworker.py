"""Tests for ``CCWorker``, the LLM pool's ``claude -p`` subprocess worker, on mocked processes.

This file pins how the worker reads the CLI's stream-json output (one reply per prompt,
stray and oversize lines skipped, an error result failing only its own message), when it
retires itself (a reply that breaks off, a cancelled send, a drain that never returns, a
failed respawn), and that it spawns with process-group isolation so shutdown and a
conversation reset reap the whole tree through ``communicate()``. The pool's own mechanics
live in ``test_llm_pool.py``, reasoning effort in ``test_llm_pool_effort.py``, and idle
reaping and conversation recycling in ``test_llm_pool_lifecycle.py``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from llm_pool_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _config_dir_tracks_patched_home,
)
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew.knowledge import llm_pool
from kiro_crew.knowledge.llm_pool import CCWorker
from kiro_crew.testing.wait import await_until

# ---------------------------------------------------------------------------
# Tests: CCWorker (mocked subprocess)
# ---------------------------------------------------------------------------


class TestCCWorker:
    @pytest.mark.asyncio
    async def test_is_alive_no_proc(self):
        worker = CCWorker()
        assert worker.is_alive() is False

    @pytest.mark.asyncio
    async def test_shutdown_no_proc(self):
        worker = CCWorker()
        await worker.shutdown()  # should not raise

    @pytest.mark.asyncio
    async def test_start_raises_without_claude(self):
        with patch("kiro_crew.knowledge.llm_pool.shutil.which", return_value=None):
            worker = CCWorker()
            with pytest.raises(RuntimeError, match="claude CLI not found"):
                await worker.start()


def _stream_json(*events: Any) -> list[bytes]:
    return [(json.dumps(e) + "\n").encode("utf-8") for e in events]


def _assistant(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _result(text: str) -> dict:
    """The real CLI's success result: ``result`` is the final text, a STRING."""
    return {"type": "result", "subtype": "success", "is_error": False, "result": text}


async def _fed_worker(
    monkeypatch, lines: list[bytes], *, limit: int = 1 << 20, eof: bool = True
) -> CCWorker:
    """A CCWorker over a fake process whose stdout already holds *lines*."""
    reader = asyncio.StreamReader(limit=limit)
    for line in lines:
        reader.feed_data(line)
    if eof:
        reader.feed_eof()
    stdin = MagicMock()
    stdin.drain = AsyncMock()
    worker = CCWorker(sandbox_mode="off")
    worker._proc = cast(
        Any,
        SimpleNamespace(stdout=reader, stdin=stdin, returncode=None, pid=_UNALLOCATABLE_PID),
    )
    worker._event_queue = asyncio.Queue()
    worker._reader_task = asyncio.create_task(worker._stdout_reader())
    monkeypatch.setattr(llm_pool.platform_compat, "kill_and_reap", AsyncMock())
    return worker


class TestCCWorkerStreamJson:
    """One stray line costs that line, and one bad reply costs that reply."""

    @pytest.mark.asyncio
    async def test_the_real_cli_shape_answers_each_prompt_with_its_own_reply(self, monkeypatch):
        worker = await _fed_worker(
            monkeypatch,
            _stream_json(
                {"type": "system", "subtype": "init"},
                _assistant("reply-1"),
                _result("reply-1"),
                _assistant("reply-2"),
                _result("reply-2"),
            ),
        )
        assert await worker.send_message("p1", timeout=10) == "reply-1"
        assert await worker.send_message("p2", timeout=10) == "reply-2"
        await worker.shutdown()

    @pytest.mark.asyncio
    async def test_a_string_result_is_the_reply_when_no_assistant_text_arrived(self, monkeypatch):
        worker = await _fed_worker(monkeypatch, _stream_json(_result("only the result")))
        assert await worker.send_message("p1", timeout=10) == "only the result"
        await worker.shutdown()

    @pytest.mark.asyncio
    async def test_a_multi_turn_reply_is_the_result_not_the_narration(self, monkeypatch):
        """A fetch reply narrates a tool call in turn 1; the CLI's string
        ``result`` is the final answer, and the narration must not prefix it."""
        worker = await _fed_worker(
            monkeypatch,
            _stream_json(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "I'll fetch that page for you."},
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "fetch",
                                "input": {},
                            },
                        ],
                    },
                },
                {
                    "type": "user",
                    "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]},
                },
                _assistant("PAGE BODY"),
                _result("PAGE BODY"),
            ),
        )
        assert await worker.send_message("p1", timeout=10) == "PAGE BODY"
        await worker.shutdown()

    @pytest.mark.asyncio
    async def test_stray_lines_and_bad_shapes_are_skipped(self, monkeypatch):
        from stray_line_helpers import STRAY_LINES

        lines = [
            *(make() for make in STRAY_LINES.values()),
            *_stream_json(
                {"type": "assistant", "message": "x"},
                {
                    "type": "assistant",
                    "message": {"content": [5, {"type": "text", "text": 7}]},
                },
                _assistant("tail-of-prompt-1"),
                {"type": "result", "result": ["not", "text"]},
                _assistant("reply-2"),
                _result("reply-2"),
            ),
        ]
        worker = await _fed_worker(monkeypatch, lines)
        assert await worker.send_message("p1", timeout=10) == "tail-of-prompt-1"
        assert await worker.send_message("p2", timeout=10) == "reply-2"
        await worker.shutdown()

    @pytest.mark.asyncio
    async def test_a_line_over_the_limit_is_skipped_and_the_reader_lives(self, monkeypatch):
        """A fetched page echoed in a ``user`` tool_result is one long line."""
        oversize = _stream_json(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "x" * 4000}]},
            }
        )
        worker = await _fed_worker(
            monkeypatch,
            oversize + _stream_json(_assistant("reply-1"), _result("reply-1")),
            limit=1024,
            eof=False,
        )
        assert await worker.send_message("p1", timeout=10) == "reply-1"
        assert worker.is_alive()
        await worker.shutdown()

    @pytest.mark.asyncio
    async def test_an_error_result_fails_that_message_and_keeps_the_worker(self, monkeypatch):
        worker = await _fed_worker(
            monkeypatch,
            _stream_json(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "boom",
                },
                _assistant("reply-2"),
                _result("reply-2"),
            ),
            eof=False,
        )
        with pytest.raises(llm_pool.CCWorkerReplyError, match="boom"):
            await worker.send_message("p1", timeout=10)
        assert worker.is_alive()
        assert await worker.send_message("p2", timeout=10) == "reply-2"
        await worker.shutdown()

    @pytest.mark.asyncio
    async def test_a_reply_that_breaks_off_retires_the_worker(self, monkeypatch):
        """Reused, the next prompt would read this one's leftovers as its reply."""
        worker = await _fed_worker(monkeypatch, _stream_json(_assistant("half a reply")))
        with pytest.raises(RuntimeError, match="died during message"):
            await worker.send_message("p1", timeout=10)
        assert not worker.is_alive()
        cast(AsyncMock, llm_pool.platform_compat.kill_and_reap).assert_awaited()

    def test_the_line_limit_is_the_acp_transports_ceiling(self):
        """knowledge.md says so; a copy could drift from it without a test."""
        from kiro_crew.acp import transport_framing

        assert llm_pool.STDOUT_LINE_LIMIT == transport_framing._STDOUT_BUFFER_LIMIT

    @pytest.mark.asyncio
    async def test_a_cancelled_send_retires_the_worker(self, monkeypatch):
        """Cancellation is a BaseException: an ``except Exception`` retirement
        skipped it, and the next prompt was answered with this one's reply."""
        worker = await _fed_worker(monkeypatch, _stream_json(_assistant("answer-to-p1")), eof=False)
        taken = asyncio.Event()
        real_get = worker._event_queue.get

        async def _get():
            event = await real_get()
            taken.set()
            return event

        monkeypatch.setattr(worker._event_queue, "get", _get)
        send = asyncio.create_task(worker.send_message("p1", timeout=10))
        await await_until(taken, what="the reply being taken from the queue")
        send.cancel()
        with pytest.raises(asyncio.CancelledError):
            await send
        assert not worker.is_alive()
        cast(AsyncMock, llm_pool.platform_compat.kill_and_reap).assert_awaited()

    @pytest.mark.asyncio
    async def test_a_send_cancelled_while_the_prompt_drains_retires_the_worker(self, monkeypatch):
        """A prompt over the pipe's high-water mark is already written while
        drain() waits; reused, the worker would hand its reply to the next caller."""
        worker = await _fed_worker(monkeypatch, [], eof=False)
        draining = asyncio.Event()

        async def _drain():
            draining.set()
            # flake-ok: pre-existing park on an Event nothing sets, in a fake drain the test cancels; moved verbatim and tracked for a de-flake pass
            await asyncio.Event().wait()

        assert worker._proc is not None
        cast(Any, worker._proc.stdin).drain = _drain
        send = asyncio.create_task(worker.send_message("p1", timeout=10))
        await await_until(draining, what="the fake drain being entered")
        send.cancel()
        with pytest.raises(asyncio.CancelledError):
            await send
        assert not worker.is_alive()
        cast(AsyncMock, llm_pool.platform_compat.kill_and_reap).assert_awaited()

    @pytest.mark.asyncio
    async def test_a_drain_that_never_returns_is_bounded_by_the_timeout(self, monkeypatch):
        worker = await _fed_worker(monkeypatch, [], eof=False)

        async def _drain():
            # flake-ok: pre-existing park on an Event nothing sets, in a fake drain the send's own timeout cancels; moved verbatim and tracked for a de-flake pass
            await asyncio.Event().wait()

        assert worker._proc is not None
        cast(Any, worker._proc.stdin).drain = _drain
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(worker.send_message("p1", timeout=0.05), timeout=10)
        assert not worker.is_alive()

    @pytest.mark.asyncio
    async def test_a_failed_respawn_leaves_the_worker_dead(self, monkeypatch):
        """The respawn drops the old reader first, so the old process it keeps
        can answer nothing; reporting it alive handed it to the next caller."""
        worker = await _fed_worker(monkeypatch, [], eof=False)
        old = worker._proc
        worker._claude_bin = "claude"

        async def _wrap(cmd, **kwargs):
            raise FileNotFoundError("claude replaced mid-update")

        monkeypatch.setattr(llm_pool, "wrap_argv_async", _wrap)
        with pytest.raises(FileNotFoundError):
            await worker.reset_conversation()
        assert not worker.is_alive()
        cast(AsyncMock, llm_pool.platform_compat.kill_and_reap).assert_awaited_with(old)

    @pytest.mark.asyncio
    async def test_a_worker_whose_reader_ended_is_not_alive(self, monkeypatch):
        worker = await _fed_worker(monkeypatch, [])
        assert worker._reader_task is not None
        await await_until(worker._reader_task, what="the reader task ending")
        assert worker._proc is not None and worker._proc.returncode is None
        assert not worker.is_alive()

    @pytest.mark.asyncio
    async def test_the_stream_is_opened_with_the_stream_json_line_limit(self, monkeypatch):
        captured: dict = {}

        async def _spawn(*argv, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                stdout=asyncio.StreamReader(), stdin=MagicMock(), returncode=None
            )

        async def _wrap(cmd, **kwargs):
            return (cmd, None)

        monkeypatch.setattr(llm_pool, "create_subprocess_limited", _spawn)
        monkeypatch.setattr(llm_pool, "wrap_argv_async", _wrap)
        monkeypatch.setattr(llm_pool, "cgroup_scope_argv", lambda argv: argv)
        worker = CCWorker(sandbox_mode="off")
        worker._claude_bin = "claude"
        await worker._spawn()
        assert captured["limit"] == llm_pool.STDOUT_LINE_LIMIT > 64 * 1024
        # The limit sizes every pipe asyncio reads; an unread stderr pipe would
        # hold up to twice it per worker and stall the CLI once full.
        assert captured["stderr"] == asyncio.subprocess.DEVNULL
        assert worker._reader_task is not None
        worker._reader_task.cancel()
        await asyncio.gather(worker._reader_task, return_exceptions=True)


class _ReapProbe:
    """A PIPE-stdio child double that records how it is reaped.

    Mirrors the pin ``test_source_providers_provider_cli`` uses for the same class. A killed
    child blocked writing into a full pipe -- or a surviving descendant still
    holding the pipes open -- makes a bare ``await proc.wait()`` hang the caller
    forever, so the bounded reap must drain via ``communicate()`` and never touch
    ``wait()``.
    """

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: "int | None" = None
        self.kill_calls = 0
        self.wait_calls = 0
        self.communicate_calls = 0

    async def communicate(self):
        self.communicate_calls += 1
        self.returncode = -9
        return b"", b""

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> int:
        self.wait_calls += 1
        return -9


def _patch_tree_kill(monkeypatch, sink):
    """Fake pid + patched tree kill so no test can reach a real killpg.

    Both the async helper (used by ``kill_and_reap``) and the legacy sync entry
    point are patched, so the pin stays safe even when run against unmodified
    code that never calls either.
    """
    from kiro_crew import platform_compat

    async def _fake_tree(pid, sig):
        sink.append((pid, sig))
        return True

    monkeypatch.setattr(platform_compat, "kill_process_tree_async", _fake_tree)
    monkeypatch.setattr(platform_compat, "kill_process_tree", lambda *a, **k: sink.append(a))
    # The child must not look like it shares this process's group, or
    # kill_and_reap correctly skips the tree kill.
    monkeypatch.setattr(platform_compat, "IS_POSIX", True, raising=False)
    monkeypatch.setattr(platform_compat.os, "getpgid", lambda pid: 999_000 + pid, raising=False)


class TestCCWorkerReapsTheProcessTree:
    """``claude -p`` forks helpers, so a pid-scoped kill re-parents them to init.

    ``spine/agent_runner.py``'s ``_terminate_group`` already records this for the
    same argv ("agents kept costing money after Stop"), and
    ``apps/registry_pipeline/checkout.py``'s ``_communicate_with_timeout`` states the rule that a
    caller MUST spawn with ``start_new_session`` for the group signal to land.
    These pin both halves: without the spawn flag the child shares the gateway's
    group and ``kill_and_reap`` deliberately skips its tree kill, so the flag is
    load-bearing rather than cosmetic.
    """

    @pytest.mark.asyncio
    async def test_spawn_requests_its_own_session(self):
        from kiro_crew import platform_compat

        captured: "dict[str, object]" = {}

        async def _fake_spawn(*argv, **kwargs):
            captured.update(kwargs)
            return _ReapProbe()

        worker = CCWorker()
        worker._claude_bin = "/usr/bin/true"
        # `**k` is required, not defensive: _spawn reaches wrap_argv through
        # sandbox.wrap_argv_async, which forwards its sandbox options (always at
        # least `mode`) into the `_prepare` seam. A positional-only stub raises
        # TypeError from inside wrap_argv_async instead of exercising the spawn.
        with (
            patch("kiro_crew.knowledge.llm_pool.wrap_argv", lambda cmd, **k: (cmd, None)),
            patch("kiro_crew.knowledge.llm_pool.cgroup_scope_argv", lambda argv: argv),
            patch(
                "kiro_crew.knowledge.llm_pool.create_subprocess_limited",
                _fake_spawn,
            ),
        ):
            await worker._spawn()
            worker._reader_task.cancel()

        # Both halves of the platform_compat recipe, asserted the way
        # test_source_providers_provider_cli does: the POSIX flag is IS_POSIX (not an
        # unconditional True -- on Windows setsid does not exist and the value is
        # legitimately False), and the Windows flag is what makes the child tree
        # `taskkill /T`-reapable there. Asserting membership separately keeps this
        # load-bearing on Windows too, where `is IS_POSIX` alone would also be
        # satisfied by the kwarg being absent.
        assert "start_new_session" in captured and "creationflags" in captured, (
            "the worker must be spawned with process-group isolation, or its forked "
            "helpers are unreachable as a tree and get re-parented to init on cleanup"
        )
        assert captured["start_new_session"] is platform_compat.IS_POSIX
        assert captured["creationflags"] == platform_compat.CREATE_NEW_PROCESS_GROUP

    @pytest.mark.asyncio
    async def test_shutdown_reaps_the_tree_via_communicate_not_wait(self, monkeypatch):
        tree_kills: "list[tuple]" = []
        _patch_tree_kill(monkeypatch, tree_kills)

        worker = CCWorker()
        proc = _ReapProbe()
        worker._proc = proc

        await worker.shutdown()

        assert tree_kills and tree_kills[0][0] == proc.pid
        assert proc.communicate_calls == 1
        assert proc.wait_calls == 0
        assert worker._proc is None

    @pytest.mark.asyncio
    async def test_reset_conversation_reaps_the_stale_tree(self, monkeypatch):
        tree_kills: "list[tuple]" = []
        _patch_tree_kill(monkeypatch, tree_kills)

        worker = CCWorker()
        old = _ReapProbe(pid=5150)
        worker._proc = old

        replacement = _ReapProbe(pid=5151)

        async def _fake_respawn():
            worker._proc = replacement
            worker._event_queue = asyncio.Queue()

        monkeypatch.setattr(worker, "_spawn", _fake_respawn)

        await worker.reset_conversation()

        assert (
            tree_kills and tree_kills[0][0] == old.pid
        ), "the stale worker's whole tree must be reaped on recycle"
        assert old.communicate_calls == 1
        assert old.wait_calls == 0
        assert replacement.kill_calls == 0
        assert worker.calls_since_reset == 0
