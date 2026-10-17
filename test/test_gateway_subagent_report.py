"""What a sub-agent completion says, pinned through ``on_done``.

The completion consumer composes one report per terminal record: the status line,
the detail (a stop, an error with its partial output, a summary of a truncated
result, or the result itself), the redacted task, the usage line and the card
facts. A wave member is folded into its wave's digest instead: counted, held for
the next chunk or flushed with it, and the wave closed once its last member is in.

These tests pin the text and facts that leave the consumer, and the wave
decisions a flush-only record or a failing manager probe takes, so the report
reads the same wherever it is composed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_gateway_subagent_wiring import _Done, harness, make_info

from kiro_crew.slack import gateway as gw_mod


@pytest.fixture
def done(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Done]:
    yield from harness(monkeypatch)


def _busy_tab(done: _Done) -> Any:
    """A tab that stays busy, so a completion is queued with its card facts."""
    slot = done.tab("main")
    slot.running = True
    return slot


def _wave_record(total: int, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "total": total,
        "done": 0,
        "ok": 0,
        "err": 0,
        "stopped": 0,
        "not_run": 0,
        "fail_lines": [],
        "ok_lines": [],
        "held_ok_deliveries": [],
        "held_infos": [],
        "flushed": 0,
        "chunks": 0,
    }
    record.update(fields)
    return record


# ── the single report ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stop_by_the_parent_names_its_origin(done: _Done) -> None:
    info = make_info(parent="subagent:p1", result="half done")
    info.user_stopped = True
    info._stop_origin = "parent ended"

    await done.done(info)

    ((args, _kw),) = done.notices()
    assert args[1] == "Subagent `a1` ⏹"
    assert "Stopped (parent ended) before completing." in args[2]
    assert "Partial output:\nhalf done" in args[2]
    assert "Stopped by the user" not in args[2]


@pytest.mark.asyncio
async def test_a_failure_carries_its_error_and_its_flagged_partial_output(
    done: _Done,
) -> None:
    info = make_info(parent="subagent:p1", error="boom", result="streamed so far")
    info.partial = True

    await done.done(info)

    ((args, _kw),) = done.notices()
    assert args[1] == "Subagent `a1` ❌"
    assert args[2].endswith(
        "Error: boom\n\nPartial output (the run did NOT finish — do not treat this as a "
        "completed result):\nstreamed so far"
    )


@pytest.mark.asyncio
async def test_a_truncated_result_is_summarised_with_its_path(
    done: _Done, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []

    def _summary(result: str, path: str) -> str:
        seen.append((result, path))
        return "SUMMARY"

    monkeypatch.setattr(gw_mod, "summarize_result", _summary)
    info = make_info(parent="subagent:p1", result="long result")
    info.result_path = "/r/a1/result.txt"
    info.result_truncated = True

    await done.done(info)

    assert seen == [("long result", "/r/a1/result.txt")]
    ((args, _kw),) = done.notices()
    assert args[2].endswith("\n\nSUMMARY")


@pytest.mark.asyncio
async def test_an_empty_result_reads_as_no_response(done: _Done) -> None:
    await done.done(make_info(parent="subagent:p1", result=""))

    ((args, _kw),) = done.notices()
    assert args[2].endswith("\n\n_No response._")


@pytest.mark.asyncio
async def test_detail_task_and_title_are_redacted_before_the_task_is_cut(
    done: _Done,
) -> None:
    task = "x" * 97 + "SECRET" + " and more"
    info = make_info(agent_id="idSECRET", parent="subagent:p1", task=task, result="EXFIL r")

    await done.done(info)

    ((args, _kw),) = done.notices()
    assert args[1] == "Subagent `id[R]` ✅"
    task_text = "x" * 97 + "[R]"
    assert args[2] == f"{task_text}\n\nUsage: {gw_mod.format_subagent_usage(0.25, 12.0)}\n\n[U] r"


@pytest.mark.asyncio
async def test_a_queued_completion_carries_its_card_facts(done: _Done) -> None:
    slot = _busy_tab(done)
    info = make_info(agent_id="a7", task="card task")
    info.agent = "helper"
    info.model = "m-requested"
    info.resolved_model = "m-served"

    await done.done(info)

    ((text, kind, meta),) = slot.queued
    assert kind == gw_mod.SUBAGENT_COMPLETION_KIND
    assert text.startswith(f"{gw_mod.SUBAGENT_COMPLETION_PREFIX}\nAgent `a7` (helper) completed ✅")
    assert meta == {
        gw_mod.SUBAGENT_COMPLETION_META_KEY: gw_mod.single_completion_meta(
            agent_id="a7",
            outcome=gw_mod.OUTCOME_OK,
            agent_name="helper",
            task="card task",
            requested_model="m-requested",
            resolved_model="m-served",
        )
    }


@pytest.mark.asyncio
async def test_a_lone_run_without_tool_calls_carries_the_claim_note(done: _Done) -> None:
    slot = _busy_tab(done)
    info = make_info()
    info.tool_count = 0

    await done.done(info)

    ((text, _kind, _meta),) = slot.queued
    assert text.endswith(f"\n\n⚠ Agent `a1` {gw_mod.NO_TOOL_CALLS_NOTE}")


# ── the wave ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_one_member_wave_is_reported_as_its_own_completion(done: _Done) -> None:
    slot = _busy_tab(done)

    await done.done(make_info(batch_id="w1", batch_total=1))

    ((text, _kind, meta),) = slot.queued
    assert text.startswith(gw_mod.SUBAGENT_COMPLETION_PREFIX)
    assert meta[gw_mod.SUBAGENT_COMPLETION_META_KEY] == gw_mod.single_completion_meta(
        agent_id="a1",
        outcome=gw_mod.OUTCOME_OK,
        agent_name="",
        task="do the thing",
        requested_model="",
        resolved_model="",
    )
    done.mgr.finalize_batch.assert_called_once_with("w1")
    assert done.frames("batch_finished") == [
        {"batch_id": "w1", "slot": "main", "total": 1, "ok": 1, "err": 0, "stopped": 0}
    ]
    assert "w1" not in done.orch._batch_progress


@pytest.mark.asyncio
async def test_a_wave_line_names_the_served_model_after_display_redaction(
    done: _Done, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[str] = []

    def _display(text: str, _redact: Any) -> tuple[str, int]:
        shown.append(text)
        return text.replace("SECRET", "[D]"), 0

    monkeypatch.setattr(gw_mod, "redact_for_display", _display)
    slot = _busy_tab(done)
    first = make_info("w0", batch_id="w2", batch_total=2)
    first.resolved_model = "mSECRET"
    second = make_info("w1", batch_id="w2", batch_total=2)

    await done.done(first)
    await done.done(second)

    assert shown == ["mSECRET"]
    ((text, _kind, meta),) = slot.queued
    assert text.startswith(gw_mod.SUBAGENT_BATCH_COMPLETION_PREFIX)
    assert "— `w0` ✅ do the thing · model m[D] · " in text
    assert "— `w1` ✅ do the thing · " in text and "`w1` ✅ do the thing · model" not in text
    assert meta[gw_mod.SUBAGENT_COMPLETION_META_KEY] == gw_mod.wave_final_meta(
        chunk=1, chunks=1, ok=2, failed=0, stopped=0, total=2
    )


@pytest.mark.asyncio
async def test_a_failure_line_cuts_its_detail_at_four_hundred(done: _Done) -> None:
    slot = _busy_tab(done)

    await done.done(make_info("w0", batch_id="w3", batch_total=2, error="e" * 500))
    await done.done(make_info("w1", batch_id="w3", batch_total=2))

    ((text, _kind, _meta),) = slot.queued
    detail = "Error: " + "e" * 500
    assert f"  {detail[:400]}…\n— `w1` ✅" in text


@pytest.mark.asyncio
async def test_a_digest_past_sixty_thousand_characters_is_cut(done: _Done) -> None:
    slot = _busy_tab(done)
    first = make_info("w0", batch_id="w4", batch_total=2)
    first.result_path = "p" * 70_000

    await done.done(first)
    await done.done(make_info("w1", batch_id="w4", batch_total=2))

    ((text, _kind, _meta),) = slot.queued
    body = text.split("do NOT re-run completed agents.\n\n", 1)[1]
    assert body.endswith("\n…(digest truncated)")
    assert len(body) == 60_000 + len("\n…(digest truncated)")


@pytest.mark.asyncio
async def test_a_member_whose_count_closes_the_wave_skips_the_membership_lookup(
    done: _Done, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookups: list[str] = []

    async def _pending(_mgr: Any, batch_id: str) -> bool:
        lookups.append(batch_id)
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(gw_mod, "_subagent_batch_pending", _pending)
    slot = _busy_tab(done)

    for i in range(3):
        await done.done(make_info(f"w{i}", batch_id="w5", batch_total=3))

    # A failed lookup counts the wave as still pending: the first two are held.
    assert lookups == ["w5", "w5"]
    ((text, _kind, _meta),) = slot.queued
    assert text.startswith(f"{gw_mod.SUBAGENT_BATCH_COMPLETION_PREFIX}\nBatch results 1/1")
    assert "wave finished: 3 ✅ · 0 ❌ · 0 ⏹ of 3 agents." in text


@pytest.mark.asyncio
async def test_a_failing_in_flight_probe_keeps_the_wave_open(
    done: _Done, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gw_mod, "_subagent_batch_pending", AsyncMock(return_value=False))
    done.mgr.batch_reports_in_flight = MagicMock(side_effect=RuntimeError("probe"))
    slot = _busy_tab(done)

    await done.done(make_info("w0", batch_id="w6", batch_total=3))

    assert slot.queued == []
    assert done.orch._batch_progress["w6"]["done"] == 1
    done.mgr.finalize_batch.assert_not_called()


@pytest.mark.asyncio
async def test_a_failing_finalize_or_frame_still_delivers_the_closing_digest(
    done: _Done,
) -> None:
    done.mgr.finalize_batch = MagicMock(side_effect=RuntimeError("finalize"))
    done.state.broadcast_ws = MagicMock(side_effect=RuntimeError("socket"))
    slot = _busy_tab(done)

    await done.done(make_info("w0", batch_id="w7", batch_total=2))
    await done.done(make_info("w1", batch_id="w7", batch_total=2))

    ((text, _kind, _meta),) = slot.queued
    assert text.startswith(f"{gw_mod.SUBAGENT_BATCH_COMPLETION_PREFIX}\nBatch results 1/1")
    assert "w7" not in done.orch._batch_progress


@pytest.mark.asyncio
async def test_a_mid_wave_chunk_carries_its_progress_facts(
    done: _Done, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gw_mod, "SUBAGENT_DIGEST_CHUNK_SIZE", 2)
    slot = _busy_tab(done)

    await done.done(make_info("w0", batch_id="w8", batch_total=3))
    await done.done(make_info("w1", batch_id="w8", batch_total=3))

    ((text, _kind, meta),) = slot.queued
    assert text.startswith(
        f"{gw_mod.SUBAGENT_BATCH_COMPLETION_PREFIX}\nBatch results 1/2 — "
        "2 of 3 delivered, 1 still running.\nProcess these results now"
    )
    assert meta[gw_mod.SUBAGENT_COMPLETION_META_KEY] == gw_mod.wave_chunk_meta(
        chunk=1, chunks=2, delivered=2, total=3, running=1
    )
    record = done.orch._batch_progress["w8"]
    assert (record["flushed"], record["ok_lines"], record["held_ok_deliveries"]) == (2, [], [])


@pytest.mark.asyncio
async def test_a_flush_only_record_on_a_one_member_wave_routes_nothing(done: _Done) -> None:
    slot = _busy_tab(done)
    done.orch._batch_progress["w9"] = _wave_record(1, done=1)
    info = make_info("flush", batch_id="w9", batch_total=1, result="")
    info._digest_flush_only = True

    await done.done(info)

    assert slot.queued == [] and done.chats == [] and done.notices() == []
    assert done.orch._batch_progress["w9"]["done"] == 1


@pytest.mark.asyncio
async def test_a_forced_flush_into_a_closed_tab_titles_the_wave_partial(
    done: _Done,
) -> None:
    done.orch._batch_progress["w10"] = _wave_record(3, done=1, ok=1, ok_lines=["— `w0` ✅ held"])
    info = make_info("flush", batch_id="w10", batch_total=3, task="wave w10", result="")
    info._digest_flush_only = True

    await done.done(info)

    ((args, kwargs),) = done.notices()
    assert args == ("subagent", "Wave results (partial)", "wave w10")
    assert kwargs == {"meta": {"slot": "main"}}
    assert done.orch._batch_progress["w10"]["flushed"] == 1
    assert done.frames("subagent_status") == []
