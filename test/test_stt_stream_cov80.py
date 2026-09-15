"""Coverage for the defensive edges of :mod:`kiro_crew.dashboard.stt_stream`.

Companion to ``test_stt_stream.py``, which owns this module's main behaviour; this file
only closes the coverage gaps left at its edges. New behaviour cases belong
in the sibling, not here.

Untested elsewhere: the two audit helpers whose whole purpose is to swallow a
failure (a raising ``sel()`` must not turn an early return into a 500, and a
broken transport must not turn an already-audited return into one), plus three
``_Endpointer`` edges -- a whitespace-only final, cancellation during the
debounce wait, a send that fails after a COMPLETE verdict, and ``aclose()``
itself, which is what stops a pending judgment from outliving the stream.

No websocket is opened: ``ws`` is a stub and ``run_bg_oneliner`` is patched.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("amazon_transcribe", reason="STT stream tests require amazon-transcribe-streaming-sdk")

from kiro_crew.dashboard import stt_stream  # noqa: E402


def _fake_ws(*, closed: bool = False, close_raises: bool = False):
    ws = MagicMock()
    ws.closed = closed
    ws.send_json = AsyncMock()
    ws.close = AsyncMock(side_effect=OSError("transport gone") if close_raises else None)
    return ws


# ── Audit helpers: never raise, always keep the trail balanced ──


def test_end_audit_swallows_a_failing_sel(monkeypatch, caplog) -> None:
    """A dead audit subsystem must not short-circuit the caller's `return ws`."""

    def _boom():
        raise RuntimeError("SEL not initialized")

    monkeypatch.setattr(stt_stream, "sel", _boom)
    with caplog.at_level("ERROR", logger=stt_stream.logger.name):
        stt_stream._emit_end_audit("1.2.3.4", outcome="ok")  # must not raise
    assert "Failed to emit stt_stream_end SEL audit" in caplog.text


def test_guard_audit_swallows_a_failing_sel(monkeypatch, caplog) -> None:
    """Otherwise the intended 403/503 is replaced by a 500."""

    def _boom():
        raise RuntimeError("SEL not initialized")

    monkeypatch.setattr(stt_stream, "sel", _boom)
    with caplog.at_level("ERROR", logger=stt_stream.logger.name):
        stt_stream._emit_guard_audit("1.2.3.4", outcome="forbidden")  # must not raise
    assert "Failed to emit stt_stream_rejected SEL audit" in caplog.text


@pytest.mark.asyncio
async def test_close_and_end_audit_audits_before_closing(monkeypatch) -> None:
    """Audit-first: ws.close() awaits the peer, so a departed client must not
    hold stt_stream_end back."""
    order: list[str] = []
    logged = MagicMock()
    logged.log_api_access = MagicMock(side_effect=lambda **kw: order.append(kw["operation"]))
    monkeypatch.setattr(stt_stream, "sel", lambda: logged)

    ws = _fake_ws()
    ws.close = AsyncMock(side_effect=lambda: order.append("close"))

    await stt_stream._close_and_end_audit(ws, "caller", outcome="error")

    assert order == ["stt_stream_end", "close"]


@pytest.mark.asyncio
async def test_close_and_end_audit_tolerates_a_broken_transport(
    monkeypatch, caplog
) -> None:
    """The balanced trail is the invariant; the close is best-effort."""
    logged = MagicMock()
    monkeypatch.setattr(stt_stream, "sel", lambda: logged)
    ws = _fake_ws(close_raises=True)

    with caplog.at_level("ERROR", logger=stt_stream.logger.name):
        await stt_stream._close_and_end_audit(ws, "caller", outcome="error")

    assert "Failed to close STT WebSocket on early return" in caplog.text
    logged.log_api_access.assert_called_once()
    assert logged.log_api_access.call_args.kwargs["operation"] == "stt_stream_end"


# ── _Endpointer edges ──


@pytest.mark.asyncio
async def test_whitespace_only_final_schedules_nothing(monkeypatch) -> None:
    """A final of pure whitespace has nothing to classify, so no model call."""
    bg = AsyncMock(return_value="COMPLETE")
    monkeypatch.setattr(stt_stream, "run_bg_oneliner", bg)
    ep = stt_stream._Endpointer(_fake_ws(), object(), debounce=0.0, timeout=1.0)

    ep.note_final("   ")

    assert ep._tasks == set()
    bg.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_during_the_debounce_wait_is_clean(monkeypatch) -> None:
    """Teardown mid-debounce must not classify, send, or raise out of the task."""
    bg = AsyncMock(return_value="COMPLETE")
    monkeypatch.setattr(stt_stream, "run_bg_oneliner", bg)
    ws = _fake_ws()
    ep = stt_stream._Endpointer(ws, object(), debounce=5.0, timeout=1.0)

    ep.note_final("cancel me mid wait")
    (task,) = list(ep._tasks)
    await asyncio.sleep(0)  # let the task reach its debounce sleep
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    bg.assert_not_awaited()
    ws.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_frame_send_failure_is_swallowed(monkeypatch) -> None:
    """The client can vanish between the verdict and the frame; that is not fatal."""
    monkeypatch.setattr(
        stt_stream, "run_bg_oneliner", AsyncMock(return_value="COMPLETE")
    )
    ws = _fake_ws()
    ws.send_json = AsyncMock(side_effect=ConnectionResetError("client gone"))
    ep = stt_stream._Endpointer(ws, object(), debounce=0.0, timeout=1.0)

    ep.note_final("all done")
    await asyncio.gather(*list(ep._tasks))  # must not raise

    ws.send_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_aclose_cancels_a_pending_judgment(monkeypatch) -> None:
    """A judgment must not outlive the stream: it would send on a closing ws."""
    bg = AsyncMock(return_value="COMPLETE")
    monkeypatch.setattr(stt_stream, "run_bg_oneliner", bg)
    ws = _fake_ws()
    ep = stt_stream._Endpointer(ws, object(), debounce=5.0, timeout=1.0)

    ep.note_final("still thinking about it")
    (task,) = list(ep._tasks)
    await asyncio.sleep(0)

    await ep.aclose()

    assert task.done()
    bg.assert_not_awaited()
    ws.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_aclose_with_no_tasks_is_a_noop() -> None:
    ep = stt_stream._Endpointer(_fake_ws(), object(), debounce=0.0, timeout=1.0)
    await ep.aclose()  # must not raise or block
    assert ep._tasks == set()


# ── _make_handler: the Transcribe -> websocket relay ──


def _event(*results):
    """A TranscriptEvent-shaped stub: only .transcript.results is read."""
    return MagicMock(transcript=MagicMock(results=list(results)))


def _result(text: str, *, is_partial: bool):
    return MagicMock(alternatives=[MagicMock(transcript=text)], is_partial=is_partial)


def _alt_less_result():
    return MagicMock(alternatives=[])


def _handler_for(ws, endpointer=None):
    return stt_stream._make_handler(ws, endpointer)(MagicMock())


@pytest.mark.asyncio
async def test_handler_drops_everything_once_the_ws_is_closed() -> None:
    ws = _fake_ws(closed=True)
    await _handler_for(ws).handle_transcript_event(_event(_result("hi", is_partial=True)))
    ws.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_skips_results_without_alternatives() -> None:
    """Transcribe emits alternative-less results; they carry no text to relay."""
    ws = _fake_ws()
    await _handler_for(ws).handle_transcript_event(_event(_alt_less_result()))
    ws.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_feeds_the_endpointer_before_sending() -> None:
    """A partial invalidates a pending verdict, a final schedules a new one --
    both BEFORE the awaited send, or a stale COMPLETE can slip through."""
    ws = _fake_ws()
    endpointer = MagicMock()
    handler = _handler_for(ws, endpointer)

    await handler.handle_transcript_event(_event(_result("deploy the", is_partial=True)))
    await handler.handle_transcript_event(
        _event(_result("deploy the service", is_partial=False))
    )

    endpointer.note_partial.assert_called_once_with("deploy the")
    endpointer.note_final.assert_called_once_with("deploy the service")
    assert [c.args[0] for c in ws.send_json.await_args_list] == [
        {"type": "partial", "text": "deploy the"},
        {"type": "final", "text": "deploy the service"},
    ]


@pytest.mark.asyncio
async def test_handler_redacts_before_the_text_leaves_the_process() -> None:
    """A partial is flashed into the browser DOM, so it is an external surface."""
    ws = _fake_ws()
    endpointer = MagicMock()
    await _handler_for(ws, endpointer).handle_transcript_event(
        _event(_result("the key is AKIAIOSFODNN7EXAMPLE ok", is_partial=True))
    )

    sent = ws.send_json.await_args.args[0]["text"]
    assert "AKIAIOSFODNN7EXAMPLE" not in sent
    # The endpointer sees the SAME redacted text that went on the wire.
    assert endpointer.note_partial.call_args.args[0] == sent


@pytest.mark.asyncio
async def test_handler_stops_processing_after_a_failed_send() -> None:
    """A disconnected client must not produce one traceback per event."""
    ws = _fake_ws()
    ws.send_json = AsyncMock(side_effect=ConnectionResetError("client gone"))

    await _handler_for(ws).handle_transcript_event(
        _event(_result("one", is_partial=True), _result("two", is_partial=False))
    )

    assert ws.send_json.await_count == 1
