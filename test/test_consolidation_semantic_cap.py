"""The ``## Current Semantic Memory`` block of a consolidation prompt is bounded.

Every consolidation pass hands the model the active semantic table so it can
update or delete the keys it already holds instead of minting near-duplicates.
The table only grows, and before this cap the block was serialised whole on
both the history pass and the preference-only pass: one reporter measured
1,977 rows rendering to about 2 million characters, 97% of the prompt, and the
next pass failed as too large to send. The chat path caps the same data at
``semantic_cap``; this pins the consolidation-side cap, its notice included.

The cap bounds the PROMPT only. The write-side snapshot the consolidator hands
its writers still carries the whole table, so update-versus-create and the
revision checks keep seeing every row. Over the cap the block is picked by
relevance, recency and a rotating slice (``test_consolidation_semantic_selection``
pins the packer); here the pass is driven end to end: every row the block shows
may be deleted, whichever pool showed it, a row it does not show may not, and
the rotation cursor moves only after the model's answer is applied, inside a
publication hold that reads the memory switches and the lease as every other
write's does.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from consolidation_lease_helpers import lease_aware_log

from kiro_crew.config.loader import config_path
from kiro_crew.embeddings_settings import PRIORITY_NORMAL, _work_for_priority
from kiro_crew.history import HistoryConsolidator
from kiro_crew.history_consolidation import (
    _CONSOLIDATION_REFUSED,
    _LEASE_TOKEN,
    _SEMANTIC_OMISSION_NOTICE,
    _SEMANTIC_OMISSION_NOTICE_UNSCORED,
    _SEMANTIC_SNAPSHOT_PAGE,
    _bounded_semantic_table,
    _ConsolidationNotDispatched,
)
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.testing.wait import async_wait_until, finished
from kiro_crew.vector_memory import SemanticRejectCode
from kiro_crew.vector_memory_constants import (
    _MAX_CONSOLIDATION_SEMANTIC_VALUE_CHARS,
    _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION,
    _SEMANTIC_ROTATION_CHARS_PER_CONSOLIDATION,
)
from kiro_crew.vector_memory_runtime import consolidation_relevance

_HEADING = "\n\n## Current Semantic Memory\n"
_NEXT_HEADING = "\n\n## Conversation to Process\n"
# The notice is the one line a model reads to learn the table is partial.
_OMISSION_LINE = re.compile(r"\[Context budget: omitted (\d+) of (\d+) semantic rows\b.*\]")


def _row(index: int, value_len: int, day: int) -> dict:
    # Keys sort unlike their age on purpose: ``key`` order is what the block
    # renders in, ``updated_at`` is what decides which rows survive the cap.
    return {
        "key": f"project.row_{(index * 7919) % 10_000:05d}",
        "value_json": json.dumps(f"v{index}:" + "x" * value_len),
        "confidence": 0.9,
        "source": "consolidation:test",
        "created_at": f"2026-01-{day:02d}T00:00:00",
        "updated_at": f"2026-01-{day:02d}T00:00:00",
        "is_deleted": 0,
    }


def _store_with(rows: list[dict], *, scores: list[float] | None = None) -> MagicMock:
    vector_store = MagicMock()
    vector_store.algorithm_version = "v1"
    # ``get_all_semantic`` reads ``ORDER BY key``; the fake keeps that contract.
    vector_store.get_all_semantic.return_value = [
        dict(r) for r in sorted(rows, key=lambda r: r["key"])
    ]
    # The pass converts all three; a bare MagicMock's answers are pinned apart below.
    vector_store.score_consolidation_rows.return_value = scores
    vector_store.consolidation_rotation_cursor.return_value = None
    vector_store.advance_consolidation_rotation.return_value = True
    return vector_store


def _consolidator(vector_store: MagicMock) -> HistoryConsolidator:
    log = lease_aware_log()
    log.snapshot_for_consolidation.return_value = (
        [{"role": "user", "content": "hi"}],
        1,
        0,
        0,
    )
    log.consolidation_retry_state.return_value = (0, 0.0)
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    return HistoryConsolidator(
        log=log,
        memory=memory,
        sessions=None,
        vector_store=vector_store,
        migrated=True,
    )


async def _prompt_for(rows: list[dict], *, include_history: bool) -> tuple[str, dict]:
    """Run one pass and return the prompt plus the writer's keyword arguments."""
    c = _consolidator(_store_with(rows))
    captured: dict = {}

    async def fake_llm(prompt: str, *, memory_store: str = "", session_key: str = "") -> dict:
        captured["prompt"] = prompt
        return {"semantic": []}

    def fake_write(result, key, vector_store=None, **kwargs):
        captured["write_kwargs"] = kwargs

    with (
        patch.object(c, "_call_llm", side_effect=fake_llm),
        patch.object(c, "_write_structured_memory", side_effect=fake_write),
    ):
        await c._consolidate("k", include_history=include_history)
    assert "prompt" in captured, "the pass must have issued a prompt"
    return captured["prompt"], captured.get("write_kwargs", {})


def _block_of(prompt: str) -> str:
    start = prompt.index(_HEADING) + len(_HEADING)
    end = prompt.index(_NEXT_HEADING, start)
    return prompt[start:end]


def _rendered(rows: list[dict]) -> str:
    """What the block looked like before the cap: every row, key order, indent=1."""
    return json.dumps(
        [
            {"key": r["key"], "value_json": r["value_json"], "confidence": r["confidence"]}
            for r in sorted(rows, key=lambda r: r["key"])
        ],
        indent=1,
    )


def _oversized_table() -> list[dict]:
    # ~1,000 characters per rendered row, 120 rows: about twice the cap, so a
    # bounded block must drop rows while plenty still fit under it.
    rows = [_row(i, 950, day=1 + i % 28) for i in range(120)]
    assert len(_rendered(rows)) > _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize("include_history", [False, True], ids=["prefs-only", "history"])
async def test_oversized_table_is_bounded_and_says_so(include_history: bool) -> None:
    rows = _oversized_table()
    prompt, write_kwargs = await _prompt_for(rows, include_history=include_history)
    block = _block_of(prompt)

    # The cap holds for the whole block, the notice under the table included.
    assert len(block) <= _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION, (
        f"the semantic block is {len(block)} chars, over the "
        f"{_SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION}-char consolidation cap"
    )
    match = _OMISSION_LINE.search(block)
    assert match, "an over-cap table must carry the one-line omission notice"
    assert block.endswith(match.group(0)), "the notice is the block's last line"
    omitted, total = int(match.group(1)), int(match.group(2))
    assert total == len(rows)
    assert 0 < omitted < total

    kept = json.loads(block[: match.start()])
    assert len(kept) == total - omitted
    # The same shape the model was reading before: key, value_json, confidence.
    assert all(set(entry) == {"key", "value_json", "confidence"} for entry in kept)
    # Rendered in key order like the unbounded block, whichever pool kept a row.
    assert [entry["key"] for entry in kept] == sorted(entry["key"] for entry in kept)
    kept_keys = {entry["key"] for entry in kept}

    # The cap is a prompt bound only: the writers still see the whole table,
    # and are told exactly which keys the model saw so the rest stay untouched.
    assert len(write_kwargs["snapshot"]) == len(rows)
    assert write_kwargs["visible_keys"] == kept_keys


@pytest.mark.asyncio
@pytest.mark.parametrize("include_history", [False, True], ids=["prefs-only", "history"])
async def test_table_under_the_cap_renders_byte_identical(include_history: bool) -> None:
    rows = [_row(i, 40, day=1 + i % 28) for i in range(30)]
    assert len(_rendered(rows)) <= _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION

    prompt, write_kwargs = await _prompt_for(rows, include_history=include_history)

    assert _block_of(prompt) == _rendered(rows)
    assert "[Context budget:" not in prompt
    assert len(write_kwargs["snapshot"]) == len(rows)
    assert write_kwargs["visible_keys"] == {r["key"] for r in rows}


@pytest.mark.asyncio
async def test_empty_table_still_renders_the_empty_list() -> None:
    prompt, _ = await _prompt_for([], include_history=False)
    assert _block_of(prompt) == "[]"
    assert "[Context budget:" not in prompt


def _writer_store(rows: list[dict]) -> MagicMock:
    """A store whose writes succeed, for driving ``_write_structured_memory`` directly."""
    vector_store = _store_with(rows)
    vector_store.set_semantic.return_value = None
    vector_store.delete_semantic.return_value = True
    vector_store.space_generation = 1
    return vector_store


def _write(
    rows: list[dict],
    items: list[dict],
    *,
    visible: set[str],
    caplog: pytest.LogCaptureFixture,
) -> MagicMock:
    vector_store = _writer_store(rows)
    c = _consolidator(vector_store)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
        c._write_structured_memory(
            {"semantic": items},
            "k",
            vector_store,
            snapshot={r["key"]: r for r in rows},
            visible_keys=frozenset(visible),
        )
    return vector_store


def test_a_delete_of_a_key_the_model_never_saw_is_refused(caplog: pytest.LogCaptureFixture) -> None:
    rows = [_row(i, 40, day=1 + i) for i in range(4)]
    seen, unseen = rows[0]["key"], rows[3]["key"]
    # The unseen key is named twice: the refusal is logged once per key.
    items = [
        {"key": unseen, "delete": True},
        {"key": seen, "delete": True},
        {"key": unseen, "delete": True},
    ]

    vector_store = _write(rows, items, visible={seen}, caplog=caplog)

    assert [c.args[0] for c in vector_store.delete_semantic.call_args_list] == [seen]
    refusals = [r for r in caplog.records if unseen in r.getMessage()]
    assert len(refusals) == 1, "one refusal line per unseen key, naming it"
    assert "not in the rendered table" in refusals[0].getMessage()
    assert seen not in refusals[0].getMessage()


def test_an_update_of_a_key_below_the_cut_is_written(caplog: pytest.LogCaptureFixture) -> None:
    # A user's correction for a key the cap cut from the table ("my work email
    # is now X") must reach the store: the span is marked consolidated either
    # way, so a refused update would lose the correction for good. Whether the
    # new value replaces the old one is the store's arbitration, not the table's.
    rows = [_row(i, 40, day=1 + i) for i in range(4)]
    seen, unseen = rows[0]["key"], rows[3]["key"]
    items = [
        {"key": unseen, "value": "a correction for a row below the cut", "confidence": 0.9},
        {"key": seen, "value": "an update of a row it read", "confidence": 0.9},
        {"key": "project.brand_new", "value": "a key the table never held", "confidence": 0.9},
    ]

    vector_store = _write(rows, items, visible={seen}, caplog=caplog)

    written = [c.kwargs["key"] for c in vector_store.set_semantic.call_args_list]
    assert written == [unseen, seen, "project.brand_new"]
    assert not [r for r in caplog.records if "not in the rendered table" in r.getMessage()]


@pytest.mark.asyncio
async def test_the_member_store_is_handed_no_delete_of_a_key_below_the_cut(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The member (v2) path reads the model's answer through the same fence.

    ``apply_consolidation`` arbitrates an update itself (a conflicting one
    becomes an owner proposal), so only the delete of a row the model never
    read is dropped; the correction for a row below the cut and the brand-new
    key travel through, and the one refusal is logged with the key.
    """
    from kiro_crew.context import ContextBuilder
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

    rows = _oversized_table()
    vector_store = _store_with(rows)
    vector_store.algorithm_version = "v2"
    vector_store.consolidation_receipt.return_value = None
    vector_store.with_record_metadata.side_effect = lambda entries: entries
    vector_store.apply_consolidation.return_value = {"semantic": 0, "episodic": 0, "lessons": 0}
    execution = ExecutionContext(
        "member-id",
        MemoryStoreRef("member-store", "member-id"),
        "member",
        "kirocrew",
        "persistent",
    )
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution", lambda _key: execution
    )
    monkeypatch.setattr("kiro_crew.memory_stores.memory_store_version", lambda _store: 2)
    monkeypatch.setattr(ContextBuilder, "ensure_store", AsyncMock(return_value=vector_store))
    c = _consolidator(vector_store)
    monkeypatch.setattr(ContextBuilder, "get_memory_for", lambda **_kwargs: c._memory)
    named: dict[str, str] = {}

    async def fake_llm(prompt: str, *, memory_store: str = "", session_key: str = "") -> dict:
        # One key the table shows and one the cap cut, read off the prompt itself.
        block = _block_of(prompt)
        notice = _OMISSION_LINE.search(block)
        assert notice, "the oversized table must render partially"
        shown = {entry["key"] for entry in json.loads(block[: notice.start()])}
        named["seen"] = next(r["key"] for r in rows if r["key"] in shown)
        named["unseen"] = next(r["key"] for r in rows if r["key"] not in shown)
        return {
            "semantic": [
                {"key": named["unseen"], "delete": True},
                {"key": named["seen"], "delete": True},
                {"key": named["unseen"], "value": "a correction for a row below the cut"},
                {"key": "project.brand_new", "value": "a key the table never held"},
            ]
        }

    with (
        patch.object(c, "_call_llm", side_effect=fake_llm),
        caplog.at_level(logging.WARNING, logger="kiro_crew.history"),
    ):
        await c._consolidate("k", include_history=False)

    vector_store.apply_consolidation.assert_called_once()
    handed = vector_store.apply_consolidation.call_args.kwargs["result"]["semantic"]
    assert [(item["key"], bool(item.get("delete"))) for item in handed] == [
        (named["seen"], True),
        (named["unseen"], False),
        ("project.brand_new", False),
    ]
    refusals = [r for r in caplog.records if "not in the rendered table" in r.getMessage()]
    assert [r.getMessage().count(named["unseen"]) for r in refusals] == [1]


def _entries(rows: list[dict]) -> list[dict]:
    return [{"key": r["key"], "value_json": r["value_json"], "confidence": 0.9} for r in rows]


def _cap_for(entries: list[dict], total: int, *, slack: int) -> int:
    """A cap that fits exactly *entries* plus the widest notice and *slack* characters."""
    table = len(json.dumps(entries, indent=1))
    notice = max(
        len(line.format(count=total, total=total, limit=99_999))
        for line in (_SEMANTIC_OMISSION_NOTICE, _SEMANTIC_OMISSION_NOTICE_UNSCORED)
    )
    return table + 1 + notice + slack


def test_recency_ranks_mixed_timestamp_formats_by_instant_not_by_text() -> None:
    # Same day, written by two clocks: a space-separated stamp at noon and a
    # ``T``-separated one at 01:00. As text the space sorts before the ``T``, so
    # a string sort would keep the OLDER row; parsed, noon is the newer one.
    # Each row is wider than the notice, so the table cannot fit in the room the
    # notice takes when the cap is not reached.
    noon = dict(_row(1, 400, day=5), updated_at="2026-01-05 12:00:00")
    one_am = dict(_row(2, 400, day=5), updated_at="2026-01-05T01:00:00+00:00")
    unparseable = dict(_row(3, 400, day=5), updated_at="yesterday")
    # The rotation takes its first row whenever it fits the block, so give it one
    # wider than its own allowance: it takes that row alone, and the recency pool
    # picks the rest.
    filler = dict(
        _row(4, _SEMANTIC_ROTATION_CHARS_PER_CONSOLIDATION, day=1), key="project.aaa_filler"
    )
    rows = sorted([noon, one_am, unparseable, filler], key=lambda r: r["key"])
    entries = _entries(rows)
    by_key = {e["key"]: e for e in entries}

    cap = _cap_for([by_key[filler["key"]], by_key[noon["key"]]], 4, slack=8)
    text, omitted, visible, rotation_last = _bounded_semantic_table(rows, entries, cap)

    assert len(text) <= cap
    assert omitted == 2
    assert visible == {filler["key"], noon["key"]}
    assert rotation_last == filler["key"]
    # Unparseable stamps rank last: with room for two rows the parsed pair survives.
    cap = _cap_for([by_key[filler["key"]], by_key[noon["key"]], by_key[one_am["key"]]], 4, slack=8)
    _, omitted, visible, _ = _bounded_semantic_table(rows, entries, cap)
    assert omitted == 1
    assert visible == {filler["key"], noon["key"], one_am["key"]}


@pytest.mark.asyncio
async def test_a_single_row_over_the_cap_renders_the_notice_alone() -> None:
    # One row wider than the whole budget: a truncated value would read as the
    # fact itself, so the table is empty and the notice says every row is gone.
    rows = [_row(1, _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION + 100, day=1)]
    prompt, write_kwargs = await _prompt_for(rows, include_history=False)
    block = _block_of(prompt)

    match = _OMISSION_LINE.search(block)
    assert match and (int(match.group(1)), int(match.group(2))) == (1, 1)
    assert block[: match.start()].rstrip("\n") == "[]"
    # The writer still holds the row and knows the model saw none of it.
    assert len(write_kwargs["snapshot"]) == 1
    assert write_kwargs["visible_keys"] == frozenset()


# ── whichever pool shows a row, the model may delete it; an unshown row it may not ──

#: A value this long renders a row of about 970 characters, so each of the three
#: fixed allowances holds eight rows and the table below is about 1.3 caps.
_POOLED_VALUE_CHARS = 900
#: The rotation resumes after this cursor, at the first ``project.a_rot_*`` key.
_POOLED_CURSOR = "project.a"


def _stamp(day: int, second: int = 0) -> str:
    return f"2026-01-{day:02d}T00:00:{second:02d}"


def _fact(key: str, day: int, second: int = 0) -> dict:
    return {
        "key": key,
        "value_json": json.dumps(f"{key}: " + "x" * _POOLED_VALUE_CHARS),
        "confidence": 0.9,
        "source": "consolidation:test",
        "created_at": _stamp(day, second),
        "updated_at": _stamp(day, second),
        "is_deleted": 0,
    }


def _lesson(key: str, day: int) -> dict:
    """An ``on_topic`` lesson, the tier a consolidation delete may retire."""
    rule = f"{key}: " + "y" * _POOLED_VALUE_CHARS
    return dict(_fact(key, day), value_json=json.dumps({"rule": rule, "applies": "on_topic"}))


def _pooled_table() -> tuple[list[dict], list[float], dict[str, str]]:
    """Rows, their scores in key order, and for each pool a row only that pool shows.

    Oldest first: one row nothing shows, ten the rotation reaches (eight fit its
    allowance), thirty relevant facts, twenty relevant lessons (eight fit theirs),
    twenty plain facts the backfill reaches newest first, and the eight newest
    facts, which fill the recency allowance. Relevance is the reverse of recency
    for the facts it ranks: the weakest relevant fact is still shown, by relevance.
    """
    rows = [_fact("project.z_unshown", day=1)]
    rows += [_fact(f"project.a_rot_{i:02d}", day=2) for i in range(10)]
    rows += [_fact(f"project.r_{i:02d}", day=3) for i in range(30)]
    rows += [_lesson(f"lesson.l_{i:02d}", day=4) for i in range(20)]
    rows += [_fact(f"project.b_{i:02d}", day=5, second=i) for i in range(20)]
    rows += [_fact(f"project.n_{i:02d}", day=28, second=i) for i in range(8)]
    rows.sort(key=lambda r: r["key"])
    score = {f"project.r_{i:02d}": 0.5 - i / 100 for i in range(30)}
    score |= {f"lesson.l_{i:02d}": 0.9 - i / 100 for i in range(20)}
    shown_by = {
        "rotation": "project.a_rot_00",
        "lessons": "lesson.l_00",
        "recency": "project.n_07",
        "relevance": "project.r_29",
        "backfill": "project.b_19",
    }
    return rows, [score.get(r["key"], 0.0) for r in rows], shown_by


#: A fact and a lesson the pooled block does not show.
_UNSHOWN = ("project.z_unshown", "lesson.l_19")


def _deleting_model(shown_by: dict[str, str], seen: dict):
    """A model that deletes one row of every pool plus the unshown rows, read off the block."""

    async def fake_llm(prompt: str, *, memory_store: str = "", session_key: str = "") -> dict:
        block = _block_of(prompt)
        notice = _OMISSION_LINE.search(block)
        assert notice, "the pooled table must render partially"
        seen["shown"] = {entry["key"] for entry in json.loads(block[: notice.start()])}
        # The notice is the semantic block's last line; on the member path the
        # anchors and preferences sections follow it before the conversation.
        seen["length"] = notice.end()
        seen["notice"] = notice.group(0)
        return {
            "semantic": [{"key": key, "delete": True} for key in [*shown_by.values(), *_UNSHOWN]]
        }

    return fake_llm


def _assert_pools_shown(seen: dict, shown_by: dict[str, str]) -> str:
    """Check the block, and return the last key the rotation showed.

    Only the rotation reaches the ``project.a_rot_*`` rows (score 0, older than
    everything the backfill gets to), so the slice is exactly the ones shown:
    a contiguous run from the first, whose length depends on each row's width.
    """
    assert seen["length"] <= _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION
    # The rows were ranked by their scores, and the notice says so.
    assert "chosen for relevance to this conversation" in seen["notice"]
    missing = {pool: key for pool, key in shown_by.items() if key not in seen["shown"]}
    assert missing == {}, "every pool must show its row"
    assert set(_UNSHOWN).isdisjoint(seen["shown"])
    rotated = sorted(key for key in seen["shown"] if key.startswith("project.a_rot_"))
    assert rotated == [f"project.a_rot_{i:02d}" for i in range(len(rotated))]
    assert 1 < len(rotated) < 10, "the slice must stop at the rotation's allowance"
    return rotated[-1]


def _refusals(caplog: pytest.LogCaptureFixture) -> list[str]:
    return sorted(
        next(key for key in _UNSHOWN if key in r.getMessage())
        for r in caplog.records
        if "not in the rendered table" in r.getMessage()
    )


@pytest.mark.asyncio
async def test_the_v1_writer_deletes_a_row_of_every_pool_and_withholds_the_unshown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rows, scores, shown_by = _pooled_table()
    vector_store = _writer_store(rows)
    vector_store.score_consolidation_rows.return_value = scores
    vector_store.consolidation_rotation_cursor.return_value = _POOLED_CURSOR
    by_key = {r["key"]: r for r in rows}
    # The lesson tier check reads the stored row, as the real store answers it.
    vector_store.get_semantic.side_effect = lambda key: by_key.get(key)
    c = _consolidator(vector_store)
    seen: dict = {}

    with (
        patch.object(c, "_call_llm", side_effect=_deleting_model(shown_by, seen)),
        caplog.at_level(logging.WARNING, logger="kiro_crew.history"),
    ):
        await c._consolidate("k", include_history=True)

    reached = _assert_pools_shown(seen, shown_by)
    deleted = [call.args[0] for call in vector_store.delete_semantic.call_args_list]
    assert deleted == list(shown_by.values())
    # The lesson was retired against the body its tier was read from.
    lesson_delete = vector_store.delete_semantic.call_args_list[1]
    assert lesson_delete.kwargs["expect_value_json"] == by_key["lesson.l_00"]["value_json"]
    assert _refusals(caplog) == sorted(_UNSHOWN), "one refusal line per unshown key"
    vector_store.advance_consolidation_rotation.assert_called_once_with(_POOLED_CURSOR, reached)


def _member_store(rows: list[dict], monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A V2 member store the pass reaches through its execution context."""
    from kiro_crew.context import ContextBuilder
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

    vector_store = _store_with(rows)
    vector_store.algorithm_version = "v2"
    vector_store.consolidation_receipt.return_value = None
    vector_store.with_record_metadata.side_effect = lambda entries: entries
    vector_store.apply_consolidation.return_value = {"semantic": 0, "episodic": 0, "lessons": 0}
    execution = ExecutionContext(
        "member-id",
        MemoryStoreRef("member-store", "member-id"),
        "member",
        "kirocrew",
        "persistent",
    )
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution", lambda _key: execution
    )
    monkeypatch.setattr("kiro_crew.memory_stores.memory_store_version", lambda _store: 2)
    monkeypatch.setattr(ContextBuilder, "ensure_store", AsyncMock(return_value=vector_store))
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    monkeypatch.setattr(ContextBuilder, "get_memory_for", lambda **_kwargs: memory)
    return vector_store


@pytest.mark.asyncio
async def test_the_member_store_gets_a_delete_of_every_pool_and_none_of_the_unshown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rows, scores, shown_by = _pooled_table()
    vector_store = _member_store(rows, monkeypatch)
    vector_store.score_consolidation_rows.return_value = scores
    vector_store.consolidation_rotation_cursor.return_value = _POOLED_CURSOR
    c = _consolidator(vector_store)
    seen: dict = {}

    with (
        patch.object(c, "_call_llm", side_effect=_deleting_model(shown_by, seen)),
        caplog.at_level(logging.WARNING, logger="kiro_crew.history"),
    ):
        await c._consolidate("k", include_history=True)

    reached = _assert_pools_shown(seen, shown_by)
    handed = vector_store.apply_consolidation.call_args.kwargs["result"]["semantic"]
    assert [item["key"] for item in handed] == list(shown_by.values())
    assert _refusals(caplog) == sorted(_UNSHOWN)
    vector_store.advance_consolidation_rotation.assert_called_once_with(_POOLED_CURSOR, reached)


# ── the rotation cursor moves only after an applied answer ───────────────────────────


async def _pass_with(
    vector_store: MagicMock,
    answer,
    *,
    log_overrides: dict | None = None,
    include_history: bool = True,
) -> HistoryConsolidator:
    c = _consolidator(vector_store)
    for name, value in (log_overrides or {}).items():
        setattr(c._log, name, value)
    with patch.object(c, "_call_llm", side_effect=answer):
        await c._consolidate("k", include_history=include_history)
    return c


def _rotating_store() -> MagicMock:
    vector_store = _writer_store(_oversized_table())
    vector_store.consolidation_rotation_cursor.return_value = "project.row_05000"
    return vector_store


@pytest.mark.asyncio
@pytest.mark.parametrize("include_history", [False, True], ids=["prefs-only", "history"])
async def test_an_applied_answer_moves_the_cursor_to_the_slice_it_read(
    include_history: bool,
) -> None:
    vector_store = _rotating_store()

    await _pass_with(vector_store, [{"semantic": []}], include_history=include_history)

    vector_store.advance_consolidation_rotation.assert_called_once()
    expected, reached = vector_store.advance_consolidation_rotation.call_args.args
    assert expected == "project.row_05000"
    # The slice resumed past the cursor and was shown, so the cursor moves past it.
    assert reached > "project.row_05000"
    vector_store.score_consolidation_rows.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("include_history", [False, True], ids=["prefs-only", "history"])
@pytest.mark.parametrize("answer", [None, {}], ids=["none", "empty"])
async def test_an_empty_answer_leaves_the_cursor(answer, include_history: bool) -> None:
    # A preference-only pass completes on ``{}`` and keeps its window pending on no
    # answer at all; neither applies an answer, so neither moves past the slice.
    vector_store = _rotating_store()

    await _pass_with(vector_store, [answer], include_history=include_history)

    vector_store.score_consolidation_rows.assert_called_once()
    vector_store.advance_consolidation_rotation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("include_history", [False, True], ids=["prefs-only", "history"])
async def test_an_unsent_prompt_leaves_the_cursor(include_history: bool) -> None:
    vector_store = _rotating_store()

    await _pass_with(
        vector_store,
        _ConsolidationNotDispatched("no session manager"),
        include_history=include_history,
    )

    vector_store.score_consolidation_rows.assert_called_once()
    vector_store.advance_consolidation_rotation.assert_not_called()


class _OneTurnProvider:
    """Streams *text* as one consolidation turn, on fresh stats that bill *credits*."""

    served_model = "test-model"
    # A backend whose zero billing means nothing was billed (ACP_BACKENDS_REPORT_USAGE).
    reports_turn_usage = True

    def __init__(self, text: str, credits: float) -> None:
        self._text = text
        self._credits = credits
        self.last_prompt_stats = SimpleNamespace(credits=0.0)

    async def stream(self, _message: str, *, allow_image: bool = True):
        self.last_prompt_stats = SimpleNamespace(credits=self._credits)
        if self._text:
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=self._text)
        yield LLMEvent(kind=EVENT_COMPLETE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "credits", "charged"),
    [
        ("", 0.0, None),
        ("", 2.0, "empty reply"),
        ("no json here", 2.0, "unparsable reply (12 chars, no closing brace)"),
        ('{"semantic": []}', 2.0, None),
    ],
    ids=["billed-nothing", "billed-empty", "billed-unparsable", "billed-answer"],
)
async def test_only_the_applied_answer_of_a_billed_turn_moves_the_cursor(
    text: str, credits: float, charged: str | None
) -> None:
    """The turn runs through ``_call_llm``. An empty reply that billed nothing spent
    nothing and only arms the backoff; an empty or unparsable reply that billed is
    charged. Neither is applied, so neither moves the cursor past the slice."""
    from kiro_crew import history_consolidation

    @contextlib.asynccontextmanager
    async def turn_on(_sessions, **_kwargs):
        yield _OneTurnProvider(text, credits)

    vector_store = _rotating_store()
    c = _consolidator(vector_store)
    c._sessions = object()
    backoff, charge = AsyncMock(), AsyncMock(return_value=True)

    with (
        patch.object(history_consolidation, "background_turn", turn_on),
        patch.object(c, "_note_environment_failure", backoff),
        patch.object(c, "_note_failed_attempt", charge),
    ):
        await c._consolidate("k", include_history=True)

    # Over the cap with a cursor read: every case had a slice to record.
    vector_store.score_consolidation_rows.assert_called_once()
    applied = text.startswith("{")
    if applied:
        vector_store.advance_consolidation_rotation.assert_called_once()
        expected, reached = vector_store.advance_consolidation_rotation.call_args.args
        assert expected == "project.row_05000" and reached > expected
    else:
        vector_store.advance_consolidation_rotation.assert_not_called()
    if charged is None and not applied:
        backoff.assert_awaited_once_with("k", "provider returned no output")
    else:
        backoff.assert_not_awaited()
    assert [call.args[2] for call in charge.await_args_list] == ([charged] if charged else [])


@pytest.mark.asyncio
async def test_a_discarded_answer_leaves_the_cursor() -> None:
    vector_store = _rotating_store()
    span = [{"role": "user", "content": "hi"}]
    # The user spoke again while the model was thinking: the answer is discarded.
    snapshots = [
        (span, 1, 0, 0),
        (span + [{"role": "user", "content": "one more thing"}], 2, 0, 0),
    ]
    snapshot = MagicMock(side_effect=snapshots)

    await _pass_with(
        vector_store,
        [{"semantic": [{"key": "project.row_05000", "delete": True}]}],
        log_overrides={"snapshot_for_consolidation": snapshot},
    )

    assert snapshot.call_count == 2
    vector_store.delete_semantic.assert_not_called()
    vector_store.advance_consolidation_rotation.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_write_leaves_the_cursor() -> None:
    vector_store = _rotating_store()
    c = _consolidator(vector_store)

    with (
        patch.object(c, "_call_llm", side_effect=[{"semantic": []}]),
        patch.object(c, "_write_structured_memory", side_effect=OSError("disk full")),
        pytest.raises(OSError),
    ):
        await c._consolidate("k", include_history=True)

    vector_store.advance_consolidation_rotation.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_member_apply_leaves_the_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    vector_store = _member_store(_oversized_table(), monkeypatch)
    vector_store.consolidation_rotation_cursor.return_value = "project.row_05000"
    vector_store.apply_consolidation.side_effect = OSError("disk full")
    c = _consolidator(vector_store)

    with (
        patch.object(c, "_call_llm", side_effect=[{"semantic": []}]),
        pytest.raises(OSError),
    ):
        await c._consolidate("k", include_history=True)

    vector_store.apply_consolidation.assert_called_once()
    vector_store.advance_consolidation_rotation.assert_not_called()


@pytest.mark.asyncio
async def test_a_receipt_replay_leaves_the_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.vector_memory import consolidation_source_digest

    vector_store = _member_store(_oversized_table(), monkeypatch)
    span = [{"role": "user", "content": "hi"}]
    vector_store.consolidation_receipt.return_value = {
        "source_count": 1,
        "source_digest": consolidation_source_digest(span),
        "source_total": 1,
    }
    c = _consolidator(vector_store)
    llm = AsyncMock(return_value={"semantic": []})

    with patch.object(c, "_call_llm", llm):
        await c._consolidate("k", include_history=True)

    llm.assert_not_called()
    vector_store.apply_consolidation.assert_not_called()
    vector_store.consolidation_rotation_cursor.assert_not_called()
    vector_store.advance_consolidation_rotation.assert_not_called()


# ── the cursor write is held, gated and fenced like the pass's other writes ───────────


def _store_for(path: str, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """The V1 writer's store, or a member store the pass reaches, over a table that rotates."""
    if path == "v1":
        return _rotating_store()
    vector_store = _member_store(_oversized_table(), monkeypatch)
    vector_store.consolidation_rotation_cursor.return_value = "project.row_05000"
    return vector_store


def _turn_off(switch: str) -> None:
    """Write the isolated home's config with one memory switch off."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"memory": {switch: False}}), encoding="utf-8")


def _take_over(log: MagicMock, key: str = "k") -> None:
    """A peer claims the session's lease, as it may once the holder's renewals stop."""
    assert log.update_metadata_if(key, {_LEASE_TOKEN: "peer-token"}, lambda _meta: True)


@pytest.mark.asyncio
async def test_a_switch_read_off_at_the_cursor_write_refuses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch reads on as the pass starts and off by the time the cursor moves.

    ``{"semantic": []}`` takes no hold of the writer's, so the cursor's hold is the
    run's first commit and reads the switch as any first commit does.
    """
    from kiro_crew import history_consolidation

    reads = 0

    def off_after_the_first_read() -> bool:
        nonlocal reads
        reads += 1
        return reads > 1

    monkeypatch.setattr(history_consolidation, "_persistence_disabled", off_after_the_first_read)
    vector_store = _rotating_store()
    c = _consolidator(vector_store)

    with patch.object(c, "_call_llm", side_effect=[{"semantic": []}]):
        outcome = await c._consolidate("k", include_history=False)

    assert outcome is _CONSOLIDATION_REFUSED
    assert reads == 2, "read as the pass starts and again in the cursor's hold"
    vector_store.advance_consolidation_rotation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["persistence_enabled", "writes_enabled"])
@pytest.mark.parametrize("path", ["v1", "member"])
async def test_a_switch_turned_off_during_the_turn_leaves_the_cursor(
    path: str, switch: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    vector_store = _store_for(path, monkeypatch)
    c = _consolidator(vector_store)

    async def answer_with_the_switch_off(prompt: str, **_kwargs) -> dict:
        _turn_off(switch)
        return {"semantic": []}

    with patch.object(c, "_call_llm", side_effect=answer_with_the_switch_off):
        outcome = await c._consolidate("k", include_history=False)

    assert outcome is _CONSOLIDATION_REFUSED
    vector_store.advance_consolidation_rotation.assert_not_called()
    # The member path's advance shares the apply's hold, which refused both.
    vector_store.apply_consolidation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1", "member"])
async def test_a_lease_lost_during_the_turn_leaves_the_cursor(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    vector_store = _store_for(path, monkeypatch)
    c = _consolidator(vector_store)

    async def answer_after_a_takeover(prompt: str, **_kwargs) -> dict:
        _take_over(c._log)
        return {"semantic": []}

    with patch.object(c, "_call_llm", side_effect=answer_after_a_takeover):
        outcome = await c._consolidate("k", include_history=False)

    assert outcome is _CONSOLIDATION_REFUSED
    vector_store.advance_consolidation_rotation.assert_not_called()
    vector_store.apply_consolidation.assert_not_called()
    assert c._log.get_metadata("k")[_LEASE_TOKEN] == "peer-token"


@pytest.mark.asyncio
async def test_a_peer_waiting_on_the_member_apply_takes_the_lease_after_the_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The member advance shares the apply's hold, so it lands under the lease that
    admitted the apply: a peer that claims the lease as the hold is released finds
    the cursor already moved, and the marker write after it is refused."""
    vector_store = _store_for("member", monkeypatch)
    c = _consolidator(vector_store)
    leases: list[object] = []

    @contextlib.contextmanager
    def hold_a_peer_waits_on(key: str):
        try:
            yield
        finally:
            if vector_store.apply_consolidation.called:
                _take_over(c._log, key)

    def advance(*_args) -> bool:
        leases.append(c._log.get_metadata("k")[_LEASE_TOKEN])
        return True

    c._log.publication_hold = hold_a_peer_waits_on
    vector_store.advance_consolidation_rotation.side_effect = advance

    with patch.object(c, "_call_llm", side_effect=[{"semantic": []}]):
        outcome = await c._consolidate("k", include_history=True)

    vector_store.apply_consolidation.assert_called_once()
    assert len(leases) == 1 and leases[0] != "peer-token", leases
    assert outcome is _CONSOLIDATION_REFUSED


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1", "member"])
async def test_the_cursor_moves_inside_a_publication_hold(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    vector_store = _store_for(path, monkeypatch)
    c = _consolidator(vector_store)
    open_holds: list[str] = []
    held_at_advance: list[bool] = []

    @contextlib.contextmanager
    def publication_hold(key: str):
        open_holds.append(key)
        try:
            yield
        finally:
            open_holds.remove(key)

    def advance(*_args) -> bool:
        held_at_advance.append(bool(open_holds))
        return True

    c._log.publication_hold = publication_hold
    vector_store.advance_consolidation_rotation.side_effect = advance

    with patch.object(c, "_call_llm", side_effect=[{"semantic": []}]):
        outcome = await c._consolidate("k", include_history=True)

    assert outcome is None
    assert held_at_advance == [True]


# ── a value the consolidation size bound refuses is part of an applied answer ─────────

#: The rule the prompt states for the size bound, in the rules above the block.
_BOUND_RULE = f"Each value is at most {_MAX_CONSOLIDATION_SEMANTIC_VALUE_CHARS} characters"


async def _pass_refusing_for_size(
    path: str, monkeypatch: pytest.MonkeyPatch, *, refuse: bool
) -> tuple[MagicMock, dict]:
    """One over-cap pass whose answer has no semantic items (``{"semantic": []}``), or
    holds only a delete of a key the block did not show and a value over the size
    bound for a key it did."""
    vector_store = _store_for(path, monkeypatch)
    # The store's own check accepts the value, so the bound alone refuses it.
    vector_store.validate_semantic.return_value = None
    table = [row["key"] for row in vector_store.get_all_semantic.return_value]
    c = _consolidator(vector_store)
    seen: dict = {}

    async def fake_llm(prompt: str, *, memory_store: str = "", session_key: str = "") -> dict:
        # The block is one JSON array and its notice line; the next section, the member
        # anchors or the conversation, starts at the first blank line after it.
        start = prompt.index(_HEADING) + len(_HEADING)
        block = prompt[start : prompt.index("\n\n", start)]
        notice = _OMISSION_LINE.search(block)
        assert notice and block.endswith(notice.group(0)), "the table must render partially"
        seen["block"] = block
        seen["rules"] = prompt[: prompt.index(_HEADING)]
        shown = {entry["key"] for entry in json.loads(block[: notice.start()])}
        seen["shown"] = min(shown)
        seen["unshown"] = next(key for key in table if key not in shown)
        if not refuse:
            return {"semantic": []}
        over = "x" * (_MAX_CONSOLIDATION_SEMANTIC_VALUE_CHARS + 1)
        return {
            "semantic": [
                {"key": seen["unshown"], "delete": True},
                {"key": seen["shown"], "value": over, "confidence": 0.9},
            ]
        }

    with patch.object(c, "_call_llm", side_effect=fake_llm):
        seen["outcome"] = await c._consolidate("k", include_history=True)
    return vector_store, seen


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1", "member"])
async def test_an_answer_the_size_bound_refuses_is_applied_like_one_with_no_semantic_items(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound refuses a value inside the write, after the fence, on the answer the
    model gave to the block it was shown. The block keeps its cap with the bound's
    rule in the rules above it, the unshown delete is withheld, the value is audited
    and never written, and the answer is applied all the same: the cursor moves past
    the slice the model read, exactly as it does for an answer with no semantic items."""
    no_items_store, no_items = await _pass_refusing_for_size(path, monkeypatch, refuse=False)
    store, seen = await _pass_refusing_for_size(path, monkeypatch, refuse=True)

    assert no_items["outcome"] is None and seen["outcome"] is None
    assert len(seen["block"]) <= _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION
    assert _BOUND_RULE in seen["rules"] and _BOUND_RULE not in seen["block"]
    store.delete_semantic.assert_not_called()
    store.set_semantic.assert_not_called()
    if path == "member":
        assert store.apply_consolidation.call_args.kwargs["result"]["semantic"] == [None]
    assert [call.args[:2] for call in store.log_reject_event.call_args_list] == [
        (SemanticRejectCode.CONSOLIDATION_VALUE_SIZE, seen["shown"])
    ]
    no_items_store.log_reject_event.assert_not_called()
    no_items_store.advance_consolidation_rotation.assert_called_once()
    store.advance_consolidation_rotation.assert_called_once_with(
        *no_items_store.advance_consolidation_rotation.call_args.args
    )


_IMAGE_ROW = "look at ![rack](/tmp/kayak_rack.png) first"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("span", "query"),
    [
        (
            [
                {"role": "user", "content": "plan the kayak trip", "ts": "2026-01-01T09:00"},
                {"role": "assistant", "content": "the weather looks fine", "ts": "x"},
                {"role": "user", "content": _IMAGE_ROW},
            ],
            None,
        ),
        ([{"role": "assistant", "content": "the build is green"}], "the build is green"),
    ],
    ids=["user-rows", "assistant-only"],
)
async def test_the_relevance_query_is_what_the_span_said(span: list[dict], query) -> None:
    """The user rows' own words: no ``[ts] ROLE:`` prefix, no image path and no marker
    in its place. A span with no user row is scored on what it does say."""
    from kiro_crew.image_refs import STRIPPED_IMAGE_MARKER, strip_image_refs

    lesson = _lesson("lesson.kayak_straps", day=3)
    vector_store = _writer_store([*_oversized_table(), lesson])
    snapshot = MagicMock(return_value=(span, len(span), 0, 0))

    await _pass_with(
        vector_store,
        [{"semantic": []}],
        log_overrides={"snapshot_for_consolidation": snapshot},
    )

    rows, texts, sent = vector_store.score_consolidation_rows.call_args.args
    if query is None:
        stripped = strip_image_refs(_IMAGE_ROW)
        assert STRIPPED_IMAGE_MARKER in stripped
        query = "plan the kayak trip\n" + stripped.replace(STRIPPED_IMAGE_MARKER, " ")
    assert sent == query
    assert "image" not in sent and "kayak_rack" not in sent
    # The store's own rows, gathered page by page, which carry the stored vectors the
    # rendered entries lack.
    stored = vector_store.get_all_semantic.return_value
    assert len(rows) == len(stored) and all(row is own for row, own in zip(rows, stored))
    assert len(rows) == len(texts) == len(_oversized_table()) + 1
    # Each row is scored on the text the block renders: a lesson on its rule prose.
    rule = json.loads(lesson["value_json"])["rule"]
    assert texts == [rule if r is rows[0] else r["value_json"] for r in rows]
    assert rows[0]["key"] == "lesson.kayak_straps"


@pytest.mark.asyncio
async def test_a_table_that_fits_reads_no_signal_and_leaves_the_cursor() -> None:
    vector_store = _writer_store([_row(i, 40, day=1 + i % 28) for i in range(30)])

    await _pass_with(vector_store, [{"semantic": []}])

    vector_store.score_consolidation_rows.assert_not_called()
    vector_store.consolidation_rotation_cursor.assert_not_called()
    vector_store.advance_consolidation_rotation.assert_not_called()


# ── a store whose signal is broken still gets a bounded block ────────────────────────


def _rendered_block_of(seen: dict):
    async def fake_llm(prompt: str, *, memory_store: str = "", session_key: str = "") -> dict:
        seen["block"] = _block_of(prompt)
        return {"semantic": []}

    return fake_llm


_CURSOR = "project.row_05000"


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["both-raise", "bare-mock", "scores-raise", "space-changed"])
async def test_a_broken_signal_degrades_to_the_slice_and_recency(broken: str) -> None:
    rows = sorted(_oversized_table(), key=lambda r: r["key"])
    vector_store = _writer_store(rows)
    # Scores that would change the block, had they been kept.
    relevant = [1.0] * len(rows)
    cursor = _CURSOR
    vector_store.consolidation_rotation_cursor.return_value = _CURSOR
    if broken == "both-raise":
        vector_store.score_consolidation_rows.side_effect = RuntimeError("embedder down")
        vector_store.consolidation_rotation_cursor.side_effect = RuntimeError("store down")
        cursor = None
    elif broken == "bare-mock":
        # What an older test double answers: MagicMocks, not scores or a key.
        vector_store.score_consolidation_rows.return_value = MagicMock()
        vector_store.consolidation_rotation_cursor.return_value = MagicMock()
        cursor = None
    elif broken == "scores-raise":
        vector_store.score_consolidation_rows.side_effect = RuntimeError("embedder down")
    else:

        def scored_across_a_model_swap(*_args, **_kwargs):
            vector_store.space_generation += 1
            return relevant

        vector_store.score_consolidation_rows.side_effect = scored_across_a_model_swap
    seen: dict = {}

    await _pass_with(vector_store, _rendered_block_of(seen))

    entries = [
        {"key": r["key"], "value_json": r["value_json"], "confidence": r["confidence"]}
        for r in rows
    ]
    cap = _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION
    no_signal = _bounded_semantic_table(rows, entries, cap, rotation_after=cursor)
    assert seen["block"] == no_signal.text
    assert no_signal.text != (
        _bounded_semantic_table(rows, entries, cap, scores=relevant, rotation_after=cursor).text
    )
    # Nothing was ranked by relevance, and the notice does not claim it was.
    notice = seen["block"].rsplit("\n", 1)[1]
    assert notice == _SEMANTIC_OMISSION_NOTICE_UNSCORED.format(
        count=no_signal.omitted, total=len(rows), limit=cap
    )
    assert "relevance" not in notice
    # A readable cursor is kept whatever became of the scores, so the rotation
    # resumes where it stopped; without one the slice starts at the first key.
    vector_store.advance_consolidation_rotation.assert_called_once_with(
        cursor, no_signal.rotation_last
    )


# ── a scorer that never returns does not hold the pass ───────────────────────────────

#: Each of the two relevance allowances while ``_bound_expires_at_the_scorer`` holds: a
#: backstop the hang never reaches, never the trigger.
_ALLOWANCE_BACKSTOP_SECS = 30.0
#: The test's own bound on the pass: a lost-run ceiling that names a hang, under the
#: suite's 120 s timeout and below the parked scorer's own ceiling.
_PASS_LOST_RUN_SECS = 30.0
_SCORER_PARK_CEILING_SECS = 60.0


def _bound_expires_at_the_scorer(monkeypatch) -> Callable[[], None]:
    """Make the pass's wait for its relevance scores expire once the scorer has parked.

    Both allowances are backstops, and the returned ``expire()``, called from the
    scorer's thread as it parks, fires that ``wait_for``'s own ``asyncio.Timeout`` on
    the loop. A short real allowance would race the pool's pickup of the job: expired
    first, the pass bills without scores and the hang is never reached.
    """
    loop = asyncio.get_running_loop()
    bounds: list[asyncio.Timeout] = []
    real_timeout = asyncio.timeouts.timeout
    whole = 2 * _ALLOWANCE_BACKSTOP_SECS

    def recording_timeout(delay):
        bound = real_timeout(delay)
        if delay == whole:
            bounds.append(bound)
        return bound

    monkeypatch.setattr(asyncio.timeouts, "timeout", recording_timeout)
    for name in (
        "_SEMANTIC_RELEVANCE_EMBED_SECS_PER_CONSOLIDATION",
        "_SEMANTIC_RELEVANCE_SCAN_SECS_PER_CONSOLIDATION",
    ):
        monkeypatch.setattr(f"kiro_crew.history_consolidation.{name}", _ALLOWANCE_BACKSTOP_SECS)

    def expire() -> None:
        def fire() -> None:
            if bounds:
                bounds.pop().reschedule(loop.time())

        loop.call_soon_threadsafe(fire)

    return expire


@pytest.mark.asyncio
async def test_a_hung_scorer_leaves_the_pass_to_bill_on_the_slice_and_recency(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    rows = sorted(_oversized_table(), key=lambda r: r["key"])
    vector_store = _writer_store(rows)
    vector_store.consolidation_rotation_cursor.return_value = _CURSOR
    c = _consolidator(vector_store)
    expire = _bound_expires_at_the_scorer(monkeypatch)
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    parked: dict = {}

    def hung_scorer(rows, texts, query_text, *, deadline=None):
        entered.set()
        try:
            parked["deadline"] = deadline
            parked["lease"] = c._log.get_metadata("k").get(_LEASE_TOKEN)
            expire()
            # A wedged native embed: held until the test lets it go.
            parked["released"] = release.wait(_SCORER_PARK_CEILING_SECS)
            return [1.0] * len(rows)
        finally:
            returned.set()

    vector_store.score_consolidation_rows.side_effect = hung_scorer
    seen: dict = {}
    asked_from = time.monotonic()
    try:
        with (
            caplog.at_level(logging.WARNING, logger="kiro_crew.history"),
            patch.object(c, "_call_llm", side_effect=_rendered_block_of(seen)),
        ):
            await finished(
                c._consolidate("k", include_history=True),
                "the pass behind a hung scorer",
                timeout=_PASS_LOST_RUN_SECS,
            )
    finally:
        # The parked thread is this test's to end, so no hang outlives it.
        release.set()
        if entered.is_set():
            await async_wait_until(
                returned.is_set,
                timeout=_SCORER_PARK_CEILING_SECS,
                describe=lambda: "the released scorer has not returned",
            )
            await _until_no_scorer_runs()
    assert entered.is_set() and parked["released"]

    # Billed on no scores: the block is the slice and the newest rows alone.
    entries = [
        {"key": r["key"], "value_json": r["value_json"], "confidence": r["confidence"]}
        for r in rows
    ]
    no_signal = _bounded_semantic_table(
        rows, entries, _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION, rotation_after=_CURSOR
    )
    assert seen["block"] == no_signal.text
    vector_store.advance_consolidation_rotation.assert_called_once_with(
        _CURSOR, no_signal.rotation_last
    )
    # The embed inside the scorer was handed its deadline, an allowance from the ask.
    assert isinstance(parked["deadline"], float)
    assert asked_from + _ALLOWANCE_BACKSTOP_SECS <= parked["deadline"]
    assert parked["deadline"] <= time.monotonic() + _ALLOWANCE_BACKSTOP_SECS
    # The lease was held through the hang and released by the pass as normal.
    assert parked["lease"] is not None
    assert c._log.get_metadata("k").get(_LEASE_TOKEN) is None
    timed_out = [
        r.getMessage()
        for r in caplog.records
        if r.name == "kiro_crew.history" and "relevance scores" in r.getMessage()
    ]
    assert len(timed_out) == 1 and "took over 60s" in timed_out[0]


async def _until_no_scorer_runs() -> None:
    """Wait until the pass's one-scorer claim is free again: the claim is given back on
    the pool thread just after the scorer returns, and a later test's pass needs it."""
    await async_wait_until(
        lambda: not consolidation_relevance._SCORER_IN_FLIGHT.locked(),
        timeout=_SCORER_PARK_CEILING_SECS,
        describe=lambda: "a returned scorer still holds the claim",
    )


class _ParkedScorers:
    """A scorer that never returns until the test lets it go, for every pass that calls it.

    Each call fires the pass's relevance bound as it parks (``expire`` from
    ``_bound_expires_at_the_scorer``), so the pass gives up on it and bills.
    """

    def __init__(self, expire: Callable[[], None]) -> None:
        self._expire = expire
        self._counts = threading.Lock()
        self._release = threading.Event()
        self.entered = 0
        self.returned = 0

    def __call__(self, rows, texts, query_text, *, deadline=None):
        with self._counts:
            self.entered += 1
        try:
            self._expire()
            self._release.wait(_SCORER_PARK_CEILING_SECS)
            return [1.0] * len(rows)
        finally:
            with self._counts:
                self.returned += 1

    async def let_go(self) -> None:
        """Release every parked scorer and wait until each has returned."""
        self._release.set()
        await async_wait_until(
            lambda: self.returned == self.entered,
            timeout=_SCORER_PARK_CEILING_SECS,
            describe=lambda: f"{self.entered - self.returned} released scorers have not returned",
        )


#: Passes in a row that meet a scorer that never returns: more than the background
#: pool's admissions.
_PASSES_BEHIND_HUNG_SCORERS = 3


@pytest.mark.asyncio
async def test_scorers_given_up_on_never_keep_a_later_pass_from_its_writes(monkeypatch) -> None:
    """Pass after pass meets a scorer that never returns, and each must still bill,
    append its history entry and finish. A scorer the pass stopped waiting for keeps
    its thread, and with it an admission of the background pool the pass's writes go
    through; the scorers left running must never hold every admission. Once they
    return, the next pass scores again."""
    rows = sorted(_oversized_table(), key=lambda r: r["key"])
    scorers = _ParkedScorers(_bound_expires_at_the_scorer(monkeypatch))
    entry = "- the user said hi"

    async def history_only(prompt: str, *, memory_store: str = "", session_key: str = "") -> dict:
        return {"history_entry": entry}

    async def history_pass(scorer, what: str) -> tuple[HistoryConsolidator, MagicMock]:
        vector_store = _writer_store(rows)
        vector_store.score_consolidation_rows.side_effect = scorer
        c = _consolidator(vector_store)
        with patch.object(c, "_call_llm", side_effect=history_only):
            await finished(
                c._consolidate("k", include_history=True), what, timeout=_PASS_LOST_RUN_SECS
            )
        return c, vector_store

    appended = []
    try:
        for attempt in range(1, _PASSES_BEHIND_HUNG_SCORERS + 1):
            c, _ = await history_pass(
                scorers, f"pass {attempt} of {_PASSES_BEHIND_HUNG_SCORERS} behind hung scorers"
            )
            appended.append([call.args for call in c._memory.append_history.call_args_list])
    finally:
        await scorers.let_go()

    assert scorers.entered >= 1
    assert appended == [[(entry,)]] * _PASSES_BEHIND_HUNG_SCORERS

    await _until_no_scorer_runs()
    c, vector_store = await history_pass(
        lambda rows, *_args, **_kwargs: [1.0] * len(rows), "the pass after the scorers returned"
    )

    vector_store.score_consolidation_rows.assert_called_once()
    c._memory.append_history.assert_called_once_with(entry)


@pytest.mark.asyncio
async def test_a_pass_takes_no_scores_while_another_pass_s_scorer_runs(monkeypatch) -> None:
    """While a scorer a pass gave up on is still running, the next pass does not run
    its own: its block is the slice and the newest rows, under the notice that claims
    no relevance. Once that thread returns, a pass scores again."""
    rows = sorted(_oversized_table(), key=lambda r: r["key"])
    entries = [
        {"key": r["key"], "value_json": r["value_json"], "confidence": r["confidence"]}
        for r in rows
    ]
    cap = _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION
    relevant = [1.0] * len(rows)
    no_signal = _bounded_semantic_table(rows, entries, cap, rotation_after=_CURSOR)
    scored = _bounded_semantic_table(rows, entries, cap, scores=relevant, rotation_after=_CURSOR)
    assert scored.text != no_signal.text
    scorers = _ParkedScorers(_bound_expires_at_the_scorer(monkeypatch))

    async def block_after(scorer) -> tuple[str, MagicMock]:
        vector_store = _writer_store(rows)
        vector_store.consolidation_rotation_cursor.return_value = _CURSOR
        vector_store.score_consolidation_rows.side_effect = scorer
        seen: dict = {}
        await finished(
            _pass_with(vector_store, _rendered_block_of(seen)),
            "a pass beside a parked scorer",
            timeout=_PASS_LOST_RUN_SECS,
        )
        return seen["block"], vector_store

    try:
        given_up, _ = await block_after(scorers)
        beside, second = await block_after(lambda *_args, **_kwargs: relevant)
    finally:
        await scorers.let_go()

    assert scorers.entered == 1
    assert given_up == beside == no_signal.text
    second.score_consolidation_rows.assert_not_called()
    assert beside.rsplit("\n", 1)[1] == _SEMANTIC_OMISSION_NOTICE_UNSCORED.format(
        count=no_signal.omitted, total=len(rows), limit=cap
    )

    await _until_no_scorer_runs()
    after, third = await block_after(lambda *_args, **_kwargs: relevant)

    third.score_consolidation_rows.assert_called_once()
    assert after == scored.text
    assert after.rsplit("\n", 1)[1] == _SEMANTIC_OMISSION_NOTICE.format(
        count=scored.omitted, total=len(rows), limit=cap
    )


# ── the pass reads the table in keyset pages ──────────────────────────────────────────

#: Three snapshot pages: two full ones and a short third.
_PAGED_ROWS = 2 * _SEMANTIC_SNAPSHOT_PAGE + 100
#: Mid-way through the third page.
_PAGED_CURSOR_AT = 2 * _SEMANTIC_SNAPSHOT_PAGE + 49


def _paged_table() -> tuple[list[dict], int]:
    """Short rows over the cap, in key order, and the index of the one only relevance
    can show: mid-way through the second page, and older than every other row."""
    rows = sorted((_row(i, 20, day=2 + i % 27) for i in range(_PAGED_ROWS)), key=lambda r: r["key"])
    target = _SEMANTIC_SNAPSHOT_PAGE + _SEMANTIC_SNAPSHOT_PAGE // 2
    rows[target] = dict(rows[target], updated_at="2025-12-01T00:00:00")
    assert len(_rendered(rows)) > _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION
    return rows, target


def _paged_store(
    rows: list[dict], between_pages: Callable[[MagicMock, dict], None]
) -> tuple[MagicMock, list[tuple[int | None, str | None]]]:
    """A writer store whose ``get_all_semantic`` reads keyset pages as the real one does.

    ``between_pages(store, table)`` runs once, before the second page is read, as a
    write or a model swap landing between two of the snapshot's pages would. Returns
    the store and the ``(limit, after_key)`` of every read.
    """
    vector_store = _writer_store(rows)
    table = {r["key"]: r for r in rows}
    reads: list[tuple[int | None, str | None]] = []

    def get_all_semantic(limit=None, offset=0, *, q="", after_key=None, exclude_lessons=False):
        reads.append((limit, after_key))
        if len(reads) == 2:
            between_pages(vector_store, table)
        keys = sorted(key for key in table if after_key is None or key > after_key)
        return [table[key] for key in keys[offset : None if limit is None else offset + limit]]

    vector_store.get_all_semantic.side_effect = get_all_semantic
    return vector_store, reads


@pytest.mark.asyncio
async def test_the_pools_the_writers_and_the_cursor_read_every_page() -> None:
    """Rows written between two pages: one behind the walk is in no page, so the block
    cannot show it and its delete is withheld; one ahead of it is read like any row."""
    rows, target = _paged_table()
    ordered = [r["key"] for r in rows]
    cursor = ordered[_PAGED_CURSOR_AT]

    def written_between_pages(_store: MagicMock, table: dict) -> None:
        for key in ("project.a_behind", "project.zz_ahead"):
            table[key] = _row(0, 20, day=28) | {"key": key}

    vector_store, reads = _paged_store(rows, written_between_pages)
    vector_store.score_consolidation_rows.side_effect = lambda scored, _texts, _query, **_kw: [
        1.0 if row["key"] == ordered[target] else 0.0 for row in scored
    ]
    vector_store.consolidation_rotation_cursor.return_value = cursor
    c = _consolidator(vector_store)
    seen: dict = {}

    async def fake_llm(prompt: str, *, memory_store: str = "", session_key: str = "") -> dict:
        block = _block_of(prompt)
        notice = _OMISSION_LINE.search(block)
        assert notice and len(block) <= _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION
        seen["shown"] = [entry["key"] for entry in json.loads(block[: notice.start()])]
        keys = [seen["shown"][0], "project.a_behind"]
        return {"semantic": [{"key": key, "delete": True} for key in keys]}

    with (
        patch.object(c, "_call_llm", side_effect=fake_llm),
        patch.object(c, "_write_structured_memory", wraps=c._write_structured_memory) as writer,
    ):
        await c._consolidate("k", include_history=True)

    # Every read is a bounded page, each resuming after the last key of the one before.
    assert [limit for limit, _ in reads] == [_SEMANTIC_SNAPSHOT_PAGE] * 3
    assert [after for _, after in reads] == [
        "",
        rows[_SEMANTIC_SNAPSHOT_PAGE - 1]["key"],
        rows[2 * _SEMANTIC_SNAPSHOT_PAGE - 1]["key"],
    ]
    held = [*ordered, "project.zz_ahead"]
    scored = vector_store.score_consolidation_rows.call_args.args[0]
    assert [row["key"] for row in scored] == held
    assert writer.call_args.kwargs["snapshot"].keys() == set(held)
    shown = seen["shown"]
    assert writer.call_args.kwargs["visible_keys"] == frozenset(shown)
    # The second page's row by relevance; the rotation from the third page's cursor,
    # through the key written ahead of the walk, round to the first page.
    assert ordered[target] in shown
    assert ordered[_PAGED_CURSOR_AT + 1] in shown and "project.zz_ahead" in shown
    vector_store.advance_consolidation_rotation.assert_called_once()
    expected, reached = vector_store.advance_consolidation_rotation.call_args.args
    assert expected == cursor and reached in shown
    assert reached < ordered[_SEMANTIC_SNAPSHOT_PAGE]
    assert "project.a_behind" not in shown
    deleted = [call.args[0] for call in vector_store.delete_semantic.call_args_list]
    assert deleted == [shown[0]]


@pytest.mark.asyncio
async def test_a_vector_space_change_between_two_pages_discards_the_scores() -> None:
    """The generation is read before the first page, so a model swap that lands while
    the table is still being read voids the scores like one that lands after."""
    rows, target = _paged_table()
    cursor = rows[_PAGED_CURSOR_AT]["key"]
    relevant = [1.0 if index == target else 0.0 for index in range(len(rows))]

    def swapped_between_pages(store: MagicMock, _table: dict) -> None:
        store.space_generation += 1

    vector_store, _ = _paged_store(rows, swapped_between_pages)
    vector_store.score_consolidation_rows.return_value = relevant
    vector_store.consolidation_rotation_cursor.return_value = cursor
    seen: dict = {}

    await _pass_with(vector_store, _rendered_block_of(seen))

    vector_store.score_consolidation_rows.assert_called_once()
    entries = [
        {"key": r["key"], "value_json": r["value_json"], "confidence": r["confidence"]}
        for r in rows
    ]
    cap = _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION
    no_signal = _bounded_semantic_table(rows, entries, cap, rotation_after=cursor)
    kept = _bounded_semantic_table(rows, entries, cap, scores=relevant, rotation_after=cursor)
    # Only relevance can show the target, so the two blocks differ on it.
    assert rows[target]["key"] in kept.visible_keys - no_signal.visible_keys
    assert seen["block"] == no_signal.text
    assert seen["block"].rsplit("\n", 1)[1] == _SEMANTIC_OMISSION_NOTICE_UNSCORED.format(
        count=no_signal.omitted, total=len(rows), limit=cap
    )


# ── a real store over the cap, end to end ─────────────────────────────────────────────

#: Shares no word with any row: only its embedding can reach the row it is about.
_CANOE_SPAN = "can we take the canoe out on saturday"


def _axis_embed(text: str) -> list[float]:
    """``kayak`` and ``canoe`` on one axis, every other text on another."""
    vector = [0.0] * 16
    vector[0 if re.search(r"\b(kayak|canoe)\b", text.lower()) else 15] = 1.0
    return vector


def _letters(seed: int, count: int) -> str:
    """*count* five-letter words starting ``aa``-``ab``, no span's and no axis's."""
    words = []
    for index in range(count):
        number, word = seed * 200 + index, ""
        for _ in range(5):
            number, letter = divmod(number, 26)
            word = chr(ord("a") + letter) + word
        words.append(word)
    return " ".join(words)


def _queued_axis_embed(text: str, *, priority: int = PRIORITY_NORMAL) -> list[float] | None:
    """``_axis_embed`` behind a queue that, like the shared embedder's, resolves the
    calling thread's work and answers ``None`` once that work has expired."""
    return None if _work_for_priority(priority).expired() else _axis_embed(text)


_queued_axis_embed.accepts_priority = True  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("embedder", ["embedder", "no-embedder", "expired"])
async def test_a_real_store_shows_the_row_only_the_span_s_vector_reaches(
    tmp_path, opened, monkeypatch, embedder: str
) -> None:
    """The ``expired`` case is the embedder behind a queue with the pass's embed
    allowance at zero: the deadline reaches the scorer's thread, the queue gives up on
    the query, and the block is the one a store with no embedder gets."""
    from kiro_crew.vector_memory import VectorMemoryStore

    ticks = itertools.count()

    def one_second_later() -> str:
        minute, second = divmod(next(ticks), 60)
        return f"2026-01-01T{minute // 60:02d}:{minute % 60:02d}:{second:02d}"

    # Every write a second after the last, so recency orders the rows as written.
    monkeypatch.setattr("kiro_crew.vector_memory._now_iso", one_second_later)
    store = opened(VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=16))
    store.init()
    store.embed_fn = _axis_embed
    # The row the span is about is the oldest, sits before the cursor, and shares
    # no word with the span; ninety newer rows are over the cap without it.
    boat = "paddle a kayak at dawn " + _letters(0, 140)
    assert store.set_semantic("project.c_boat", boat, 0.9, "user_explicit") is None
    for index in range(1, 91):
        value = _letters(index, 150)
        assert store.set_semantic(f"project.r_{index:03d}", value, 0.9, "user_explicit") is None
    assert store.advance_consolidation_rotation(None, "project.m")
    if embedder == "no-embedder":
        store.embed_fn = None
    elif embedder == "expired":
        store.embed_fn = _queued_axis_embed
        monkeypatch.setattr(
            "kiro_crew.history_consolidation._SEMANTIC_RELEVANCE_EMBED_SECS_PER_CONSOLIDATION", 0.0
        )
    span = [{"role": "user", "content": _CANOE_SPAN}]
    snapshot = MagicMock(return_value=(span, 1, 0, 0))
    seen: dict = {}

    await _pass_with(
        store, _rendered_block_of(seen), log_overrides={"snapshot_for_consolidation": snapshot}
    )

    block = seen["block"]
    notice = _OMISSION_LINE.search(block)
    assert notice and len(block) <= _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION
    shown = {entry["key"] for entry in json.loads(block[: notice.start()])}
    assert ("project.c_boat" in shown) is (embedder == "embedder")
    # The real compare-and-set moved the cursor past the slice the model read.
    reached = store.consolidation_rotation_cursor()
    assert reached is not None and reached > "project.m" and reached in shown
