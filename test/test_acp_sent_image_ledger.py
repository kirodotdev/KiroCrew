"""A picture a session's conversation already holds is not inlined again.

The backend keeps every image block a session was sent and replays it on every
later turn. Text that names the same local picture on every cycle (a work-ledger
snapshot, a nudge body, a quoted reply) must not add one more full-size block
per turn: the replayed images would pass the backend's request ceiling and
every turn, a human's included, would be refused.

These pin the session record (``SentImageLedger``) at three levels: the builder
that reads and stages it, the session handle that commits it when the backend
answers a prompt (the live path), and the direct client that does the same.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import threading
import time
from contextlib import aclosing
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import prompt_blocks
from kiro_crew.acp import session_handle as session_handle_module
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.prompt_blocks import (
    ALREADY_SENT_NOTE,
    SentImageLedger,
    build_prompt_blocks,
)
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    METHOD_CLEAR_STATUS,
    METHOD_COMPACTION_STATUS,
    METHOD_PROMPT,
    METHOD_SESSION_UPDATE,
    JsonRpcMessage,
)
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
)

# Smallest valid 1x1 PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

#: Generous on purpose: nothing here waits on a real backend, and a short
#: deadline only adds a way to fail on a slow runner.
_TURN_TIMEOUT = 60.0


def _png(tmp_path, name="shot.png"):
    p = tmp_path / name
    p.write_bytes(_PNG)
    return p


def _other_png_bytes() -> bytes:
    pil = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    pil.new("RGB", (3, 1), (10, 200, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _image_count(blocks: list[dict]) -> int:
    return sum(1 for b in blocks if b.get("type") == "image")


# ── The builder ──────────────────────────────────────────────────────────────


class TestBuilderReadsTheLedger:
    def test_a_held_picture_stays_a_path_with_a_note(self, tmp_path):
        p = _png(tmp_path)
        ledger = SentImageLedger()

        staged = ledger.staging()
        first = build_prompt_blocks(f"artifact shot: {p}", sent_images=staged)
        staged.commit()
        second = build_prompt_blocks(f"artifact shot: {p}", sent_images=ledger.staging())

        assert _image_count(first) == 1
        assert first[0]["text"] == f"artifact shot: [image: {p.name}]"
        assert second == [{"type": "text", "text": f"artifact shot: {p} {ALREADY_SENT_NOTE}"}]

    def test_an_uncommitted_prompt_inlines_its_picture_again(self, tmp_path):
        p = _png(tmp_path)
        ledger = SentImageLedger()

        build_prompt_blocks(f"look {p}", sent_images=ledger.staging())
        retry = build_prompt_blocks(f"look {p}", sent_images=ledger.staging())

        assert _image_count(retry) == 1

    def test_new_bytes_at_the_same_path_are_a_new_picture(self, tmp_path):
        p = _png(tmp_path)
        ledger = SentImageLedger()
        staged = ledger.staging()
        build_prompt_blocks(f"look {p}", sent_images=staged)
        staged.commit()

        p.write_bytes(_other_png_bytes())
        again = build_prompt_blocks(f"look {p}", sent_images=ledger.staging())

        assert _image_count(again) == 1

    def test_the_same_bytes_under_another_path_are_held(self, tmp_path):
        first_path = _png(tmp_path, "a.png")
        second_path = _png(tmp_path, "b.png")
        ledger = SentImageLedger()
        staged = ledger.staging()
        build_prompt_blocks(f"look {first_path}", sent_images=staged)
        staged.commit()

        blocks = build_prompt_blocks(f"look {second_path}", sent_images=ledger.staging())

        assert _image_count(blocks) == 0
        assert blocks[0]["text"] == f"look {second_path} {ALREADY_SENT_NOTE}"

    def test_forget_inlines_a_held_picture_again(self, tmp_path):
        p = _png(tmp_path)
        ledger = SentImageLedger()
        staged = ledger.staging()
        build_prompt_blocks(f"look {p}", sent_images=staged)
        staged.commit()

        ledger.forget()

        assert _image_count(build_prompt_blocks(f"look {p}", sent_images=ledger.staging())) == 1

    def test_a_staging_taken_before_a_forget_commits_nothing(self, tmp_path):
        """A prompt answered after the conversation was compacted or cleared
        does not record its pictures into the conversation that replaced it."""
        p = _png(tmp_path)
        ledger = SentImageLedger()
        staged = ledger.staging()
        build_prompt_blocks(f"look {p}", sent_images=staged)

        ledger.forget()
        staged.commit()

        assert _image_count(build_prompt_blocks(f"look {p}", sent_images=ledger.staging())) == 1

    def test_an_abandoned_build_cannot_commit_through_the_next_prompt(self, tmp_path):
        """A prompt cancelled while its picture was still being encoded leaves
        the build running on its worker thread. When that build finishes after
        the next prompt's build, it must not make the next prompt's answer
        record the cancelled prompt's picture as sent."""
        cancelled_pic = _png(tmp_path, "a.png")
        ledger = SentImageLedger()

        cancelled = ledger.staging()
        nxt = ledger.staging()
        build_prompt_blocks("plain words", sent_images=nxt)
        # The cancelled prompt's build lands last, as an orphaned thread would.
        build_prompt_blocks(f"look {cancelled_pic}", sent_images=cancelled)
        nxt.commit()

        later = build_prompt_blocks(f"look {cancelled_pic}", sent_images=ledger.staging())
        assert _image_count(later) == 1

    def test_without_a_ledger_every_prompt_inlines(self, tmp_path):
        p = _png(tmp_path)

        counts = [_image_count(build_prompt_blocks(f"look {p}")) for _ in range(3)]

        assert counts == [1, 1, 1]

    def test_the_note_lands_after_a_markdown_destination(self, tmp_path):
        p = _png(tmp_path)
        ledger = SentImageLedger()
        staged = ledger.staging()
        build_prompt_blocks(f"![shot]({p})", sent_images=staged)
        staged.commit()

        blocks = build_prompt_blocks(f"![shot]({p})", sent_images=ledger.staging())

        assert blocks[0]["text"] == f"![shot]({p}) {ALREADY_SENT_NOTE}"

    def test_a_typed_already_sent_note_is_escaped(self):
        blocks = build_prompt_blocks(
            f"x {ALREADY_SENT_NOTE}", sent_images=SentImageLedger().staging()
        )

        assert blocks[0]["text"] == f"x \\{ALREADY_SENT_NOTE}"

    def test_a_new_picture_past_the_record_limit_still_inlines(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prompt_blocks, "MAX_RECORDED_IMAGES", 1)
        held = _png(tmp_path, "held.png")
        new = tmp_path / "new.png"
        new.write_bytes(_other_png_bytes())
        ledger = SentImageLedger()
        staged = ledger.staging()
        build_prompt_blocks(f"look {held}", sent_images=staged)
        staged.commit()

        staged = ledger.staging()
        blocks = build_prompt_blocks(f"look {held} and {new}", sent_images=staged)
        staged.commit()

        # The record is full, so the new picture is sent as it would be with no
        # record at all: never refused, never noted as held.
        assert _image_count(blocks) == 1
        assert blocks[0]["text"] == f"look {held} {ALREADY_SENT_NOTE} and [image: {new.name}]"
        assert not ledger.holds(hashlib.sha256(new.read_bytes()).hexdigest())

    def test_a_picture_the_full_record_left_out_inlines_again(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prompt_blocks, "MAX_RECORDED_IMAGES", 1)
        held = _png(tmp_path, "held.png")
        new = tmp_path / "new.png"
        new.write_bytes(_other_png_bytes())
        ledger = SentImageLedger()
        counts = []
        for text in (f"look {held}", f"look {new}", f"look {new}", f"look {held}"):
            staged = ledger.staging()
            counts.append(_image_count(build_prompt_blocks(text, sent_images=staged)))
            staged.commit()

        # The held picture keeps its row: nothing is evicted to make room.
        assert counts == [1, 1, 1, 0]

    def test_the_record_never_holds_more_than_its_limit(self, monkeypatch, caplog):
        monkeypatch.setattr(prompt_blocks, "MAX_RECORDED_IMAGES", 2)
        ledger = SentImageLedger()
        staged = ledger.staging()
        staged.stage(["a" * 64, "b" * 64, "c" * 64])

        with caplog.at_level("WARNING", logger=prompt_blocks.logger.name):
            staged.commit()

        assert [ledger.holds(d * 64) for d in "abc"] == [True, True, False]
        assert "1 sent picture(s) not recorded" in caplog.text


# ── The session handle (the live path) ───────────────────────────────────────

_REQ_ID = 21


class _Runtime:
    """Records every prompt and answers it with the next scripted reply.

    A reply is ``(frames, answer)``: the frames are queued before the answer,
    both AFTER the send, so the pre-turn stale-frame drain cannot eat them.
    """

    def __init__(self, queue: asyncio.Queue, replies: list[tuple[list, JsonRpcMessage]]) -> None:
        self.pid = None
        self.is_alive = MagicMock(return_value=True)
        self.send_notification = AsyncMock()
        self.supports_image_prompt = True
        self.acp_backend = ACP_BACKEND_KIRO
        self.prompts: list[list[dict]] = []
        self._queue = queue
        self._replies = list(replies)
        self._last_activity = time.monotonic()
        self._agent_capabilities: dict = {}

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        pass

    async def send_request(self, method: str, params: dict) -> int:
        assert method == METHOD_PROMPT
        self.prompts.append(params["prompt"])
        frames, answer = self._replies.pop(0)
        for frame in frames:
            self._queue.put_nowait(frame)
        self._queue.put_nowait(answer)
        return _REQ_ID


def _answered() -> tuple[list, JsonRpcMessage]:
    return [], JsonRpcMessage(id=_REQ_ID, result={"stopReason": "end_turn"})


def _refused() -> tuple[list, JsonRpcMessage]:
    return [], JsonRpcMessage(
        id=_REQ_ID, error={"code": -32603, "message": "Improperly formed request"}
    )


def _compacted_then_answered() -> tuple[list, JsonRpcMessage]:
    frame = JsonRpcMessage(
        method=METHOD_COMPACTION_STATUS,
        params={"sessionId": "sK", "status": {"type": "completed"}},
    )
    return [frame], JsonRpcMessage(id=_REQ_ID, result={"stopReason": "end_turn"})


def _cleared_then_answered(*, co_tenant: bool = False) -> tuple[list, JsonRpcMessage]:
    """A native clear lands, then the turn answers.  ``co_tenant`` marks the
    clear as one the runtime fanned out from another session on it."""
    frame = JsonRpcMessage(method=METHOD_CLEAR_STATUS, params={"sessionId": "sK"})
    frame.fanout_no_owner = co_tenant
    return [frame], JsonRpcMessage(id=_REQ_ID, result={"stopReason": "end_turn"})


def _handle(replies) -> tuple[AcpSessionHandle, _Runtime]:
    queue: asyncio.Queue = asyncio.Queue()
    rt = _Runtime(queue, replies)
    return AcpSessionHandle("sK", queue, rt), rt


async def _turn(handle: AcpSessionHandle, text: str) -> None:
    async for _event in handle.prompt(text, timeout=_TURN_TIMEOUT):
        pass


class TestSessionHandle:
    @pytest.mark.asyncio
    async def test_a_path_named_every_cycle_is_inlined_once(self, tmp_path):
        """The reported loop: the same snapshot line rides every nudge cycle."""
        p = _png(tmp_path)
        handle, rt = _handle([_answered(), _answered(), _answered()])

        for _ in range(3):
            await _turn(handle, f"[ledger] artifact shot: {p}\nnext cycle")

        assert [_image_count(blocks) for blocks in rt.prompts] == [1, 0, 0]
        assert ALREADY_SENT_NOTE in rt.prompts[2][0]["text"]

    @pytest.mark.asyncio
    async def test_a_refused_prompt_inlines_its_picture_on_the_retry(self, tmp_path):
        p = _png(tmp_path)
        handle, rt = _handle([_refused(), _answered(), _answered()])

        with pytest.raises(Exception, match="Improperly formed request"):
            await _turn(handle, f"look {p}")
        await _turn(handle, f"look {p}")
        await _turn(handle, f"look {p}")

        assert [_image_count(blocks) for blocks in rt.prompts] == [1, 1, 0]

    @pytest.mark.asyncio
    async def test_a_completed_compaction_inlines_the_picture_again(self, tmp_path):
        p = _png(tmp_path)
        handle, rt = _handle([_answered(), _compacted_then_answered(), _answered()])

        for _ in range(3):
            await _turn(handle, f"look {p}")

        assert [_image_count(blocks) for blocks in rt.prompts] == [1, 0, 1]

    @pytest.mark.asyncio
    async def test_a_native_clear_inlines_the_picture_again(self, tmp_path):
        p = _png(tmp_path)
        handle, rt = _handle([_answered(), _cleared_then_answered(), _answered()])

        for _ in range(3):
            await _turn(handle, f"look {p}")

        assert [_image_count(blocks) for blocks in rt.prompts] == [1, 0, 1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("backend", "expected"),
        [(ACP_BACKEND_OPENCODE, [1, 0, 1]), (ACP_BACKEND_KIRO, [1, 0, 0])],
    )
    async def test_an_inline_compact_turn_inlines_the_picture_again(
        self, tmp_path, backend, expected
    ):
        """A backend that compacts inside the ``/compact`` turn sends no status
        frame; kiro's compaction is still to come and its status forgets."""
        p = _png(tmp_path)
        handle, rt = _handle([_answered(), _answered(), _answered()])
        rt.acp_backend = backend

        await _turn(handle, f"look {p}")
        await _turn(handle, "/compact")
        await _turn(handle, f"look {p}")

        assert [_image_count(blocks) for blocks in rt.prompts] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("backend", "expected"),
        [
            (ACP_BACKEND_GOOSE, [1, 0, 1]),
            (ACP_BACKEND_CLAUDE, [1, 0, 1]),
            (ACP_BACKEND_OPENCODE, [1, 0, 0]),
            (ACP_BACKEND_KIRO, [1, 0, 0]),
        ],
    )
    async def test_a_clear_the_harness_runs_inlines_the_picture_again(
        self, tmp_path, backend, expected
    ):
        """goose and claude run ``/clear`` in their own prompt handler and send
        no clear status. opencode's ``/clear`` reaches the model as text and the
        conversation stays; kiro reports its clear with a status that forgets."""
        p = _png(tmp_path)
        handle, rt = _handle([_answered(), _answered(), _answered()])
        rt.acp_backend = backend

        await _turn(handle, f"look {p}")
        await _turn(handle, "/clear")
        await _turn(handle, f"look {p}")

        assert [_image_count(blocks) for blocks in rt.prompts] == expected

    @pytest.mark.asyncio
    async def test_a_co_tenant_clear_leaves_this_session_holding(self, tmp_path):
        p = _png(tmp_path)
        handle, rt = _handle([_answered(), _cleared_then_answered(co_tenant=True), _answered()])

        for _ in range(3):
            await _turn(handle, f"look {p}")

        assert [_image_count(blocks) for blocks in rt.prompts] == [1, 0, 0]

    @pytest.mark.asyncio
    async def test_another_session_holds_nothing_of_this_one(self, tmp_path):
        p = _png(tmp_path)
        first, first_rt = _handle([_answered()])
        second, second_rt = _handle([_answered()])

        await _turn(first, f"look {p}")
        await _turn(second, f"look {p}")

        assert _image_count(first_rt.prompts[0]) == 1
        assert _image_count(second_rt.prompts[0]) == 1

    @pytest.mark.asyncio
    async def test_a_cancelled_build_cannot_commit_through_the_next_prompt(
        self, tmp_path, monkeypatch
    ):
        """A prompt cancelled while its picture is still being encoded leaves
        the build running on a worker thread. If that build finishes after the
        next prompt was built but before the backend answers it, the answer must
        still record only the next prompt's own pictures, so a later prompt
        naming the cancelled prompt's picture sends it."""
        pic = _png(tmp_path)
        cancelled_text = f"look {pic}"
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        real_build = session_handle_module.build_prompt_blocks

        def build(message, **kwargs):
            if message == cancelled_text and not started.is_set():
                started.set()
                release.wait(_TURN_TIMEOUT)
                try:
                    return real_build(message, **kwargs)
                finally:
                    finished.set()
            return real_build(message, **kwargs)

        monkeypatch.setattr(session_handle_module, "build_prompt_blocks", build)
        handle, rt = _handle([_answered(), _answered()])
        real_send = rt.send_request

        async def send_request(method: str, params: dict) -> int:
            if not finished.is_set():
                # The next prompt is built and on the wire: now the orphaned
                # build lands, before the backend's answer.
                release.set()
                await asyncio.to_thread(finished.wait, _TURN_TIMEOUT)
            return await real_send(method, params)

        rt.send_request = send_request

        task = asyncio.create_task(_turn(handle, cancelled_text))
        await asyncio.to_thread(started.wait, _TURN_TIMEOUT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await _turn(handle, "plain words")
        await _turn(handle, cancelled_text)

        assert [_image_count(blocks) for blocks in rt.prompts] == [0, 1]


# ── The direct client ────────────────────────────────────────────────────────


class _ClientWire:
    """Stands in for the client's stdio: records each prompt and answers the
    request the test names."""

    def __init__(self, client: AcpClient) -> None:
        self.prompts: list[list[dict]] = []
        self._client = client
        client._session_id = "s1"
        client._is_process_alive = MagicMock(return_value=True)
        client._send_request = self._send_request

    async def _send_request(self, method: str, params: dict) -> int:
        assert method == METHOD_PROMPT
        self.prompts.append(params["prompt"])
        return len(self.prompts)

    async def answer(self, req_id: int, reply: JsonRpcMessage) -> None:
        self._client._read_message = AsyncMock(side_effect=[reply])
        async with aclosing(self._client._prompt_loop(req_id, _TURN_TIMEOUT)) as loop:
            async for action, _msg in loop:
                if action in ("complete", "error"):
                    break


class TestDirectClient:
    @pytest.mark.asyncio
    async def test_an_answered_prompt_holds_its_picture(self, tmp_path):
        p = _png(tmp_path)
        client = AcpClient(work_dir=tmp_path)
        wire = _ClientWire(client)

        for _ in range(3):
            req_id = await client._send_prompt(f"look {p}")
            await wire.answer(req_id, JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"}))

        assert [_image_count(blocks) for blocks in wire.prompts] == [1, 0, 0]

    @pytest.mark.asyncio
    async def test_an_error_answer_inlines_the_picture_on_the_retry(self, tmp_path):
        p = _png(tmp_path)
        client = AcpClient(work_dir=tmp_path)
        wire = _ClientWire(client)

        req_id = await client._send_prompt(f"look {p}")
        await wire.answer(req_id, JsonRpcMessage(id=req_id, error={"code": -32603, "message": "x"}))
        req_id = await client._send_prompt(f"look {p}")
        await wire.answer(req_id, JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"}))
        await client._send_prompt(f"look {p}")

        assert [_image_count(blocks) for blocks in wire.prompts] == [1, 1, 0]

    @pytest.mark.asyncio
    async def test_a_completed_compaction_inlines_the_picture_again(self, tmp_path):
        p = _png(tmp_path)
        client = AcpClient(work_dir=tmp_path)
        wire = _ClientWire(client)
        req_id = await client._send_prompt(f"look {p}")
        await wire.answer(req_id, JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"}))

        client._handle_compaction_status(
            JsonRpcMessage(
                method=METHOD_COMPACTION_STATUS, params={"status": {"type": "completed"}}
            )
        )
        await client._send_prompt(f"look {p}")

        assert [_image_count(blocks) for blocks in wire.prompts] == [1, 1]

    @pytest.mark.asyncio
    async def test_a_native_clear_inlines_the_picture_again(self, tmp_path):
        p = _png(tmp_path)
        client = AcpClient(work_dir=tmp_path)
        wire = _ClientWire(client)
        req_id = await client._send_prompt(f"look {p}")
        await wire.answer(req_id, JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"}))

        req_id = await client._send_prompt("/clear")
        client._read_message = AsyncMock(
            side_effect=[
                JsonRpcMessage(method=METHOD_CLEAR_STATUS, params={"sessionId": "s1"}),
                JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"}),
            ]
        )
        async with aclosing(client._dispatch_events(req_id, _TURN_TIMEOUT)) as events:
            async for _event in events:
                pass
        await client._send_prompt(f"look {p}")

        assert [_image_count(blocks) for blocks in wire.prompts] == [1, 0, 1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("backend", "expected"),
        [(ACP_BACKEND_OPENCODE, [1, 0, 1]), (ACP_BACKEND_KIRO, [1, 0, 0])],
    )
    async def test_an_inline_compact_turn_inlines_the_picture_again(
        self, tmp_path, backend, expected
    ):
        p = _png(tmp_path)
        client = AcpClient(work_dir=tmp_path, acp_backend=backend)
        wire = _ClientWire(client)

        for text in (f"look {p}", "/compact", f"look {p}"):
            req_id = await client._send_prompt(text)
            await wire.answer(req_id, JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"}))

        assert [_image_count(blocks) for blocks in wire.prompts] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("backend", "expected"),
        [
            (ACP_BACKEND_GOOSE, [1, 0, 1]),
            (ACP_BACKEND_CLAUDE, [1, 0, 1]),
            (ACP_BACKEND_OPENCODE, [1, 0, 0]),
            (ACP_BACKEND_KIRO, [1, 0, 0]),
        ],
    )
    async def test_a_clear_the_harness_runs_inlines_the_picture_again(
        self, tmp_path, backend, expected
    ):
        p = _png(tmp_path)
        client = AcpClient(work_dir=tmp_path, acp_backend=backend)
        wire = _ClientWire(client)

        for text in (f"look {p}", "/clear", f"look {p}"):
            req_id = await client._send_prompt(text)
            await wire.answer(req_id, JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"}))

        assert [_image_count(blocks) for blocks in wire.prompts] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop_reason", ["end_turn", "cancelled"])
    async def test_a_claude_auto_compaction_inlines_the_picture_again(self, tmp_path, stop_reason):
        # claude's automatic compaction sends "Compacting..." and no terminal.
        # A turn that ends naturally settles it as completed; one cut off by a
        # Stop settles nothing, yet the backend may have summarized either way.
        p = _png(tmp_path)
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        wire = _ClientWire(client)
        req_id = await client._send_prompt(f"look {p}")
        await wire.answer(req_id, JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"}))

        req_id = await client._send_prompt("keep going")
        client._read_message = AsyncMock(
            side_effect=[
                JsonRpcMessage(
                    method=METHOD_SESSION_UPDATE,
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "Compacting..."},
                        },
                    },
                ),
                JsonRpcMessage(id=req_id, result={"stopReason": stop_reason}),
            ]
        )
        async with aclosing(client._dispatch_events(req_id, _TURN_TIMEOUT)) as events:
            async for _event in events:
                pass
        await client._send_prompt(f"look {p}")

        assert [_image_count(blocks) for blocks in wire.prompts] == [1, 0, 1]
