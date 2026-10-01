"""The ``## Current Semantic Memory`` block of a consolidation prompt is bounded.

Every consolidation pass hands the model the active semantic table so it can
update or delete the keys it already holds instead of minting near-duplicates.
The table only grows, and before this cap the block was serialised whole on
both the history pass and the preference-only pass: one reporter measured
1,977 rows rendering to about 2 million characters, 97% of the prompt, and the
next pass failed as too large to send. The chat path caps the same data at
``semantic_cap``; this pins the consolidation-side cap.

The cap bounds the PROMPT only. The write-side snapshot the consolidator hands
its writers still carries the whole table, so update-versus-create and the
revision checks keep seeing every row.
"""

from __future__ import annotations

import json
import logging
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.history import HistoryConsolidator
from kiro_crew.history_consolidation import _bounded_semantic_table
from kiro_crew.vector_memory_constants import _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION

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


def _store_with(rows: list[dict]) -> MagicMock:
    vector_store = MagicMock()
    vector_store.algorithm_version = "v1"
    # ``get_all_semantic`` reads ``ORDER BY key``; the fake keeps that contract.
    vector_store.get_all_semantic.return_value = [
        dict(r) for r in sorted(rows, key=lambda r: r["key"])
    ]
    return vector_store


def _consolidator(vector_store: MagicMock) -> HistoryConsolidator:
    log = MagicMock()
    log.snapshot_for_consolidation.return_value = (
        [{"role": "user", "content": "hi"}],
        1,
        0,
    )
    log.get_metadata.return_value = {}
    log.get_metadata_status.return_value = ({}, True)
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

    match = _OMISSION_LINE.search(block)
    table = block[: match.start()].rstrip("\n") if match else block
    assert len(table) <= _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION, (
        f"the semantic block is {len(table)} chars, over the "
        f"{_SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION}-char consolidation cap"
    )
    assert match, "an over-cap table must carry the one-line omission notice"
    omitted, total = int(match.group(1)), int(match.group(2))
    assert total == len(rows)
    assert 0 < omitted < total

    kept = json.loads(table)
    assert len(kept) == total - omitted
    # The same shape the model was reading before: key, value_json, confidence.
    assert all(set(entry) == {"key", "value_json", "confidence"} for entry in kept)
    # Rendered in key order like the unbounded block, chosen newest first like
    # the chat path's ``semantic_cap`` does without a query.
    assert [entry["key"] for entry in kept] == sorted(entry["key"] for entry in kept)
    kept_keys = {entry["key"] for entry in kept}
    # Newest first; within one timestamp the key order breaks the tie.
    by_recency = sorted(rows, key=lambda r: r["key"])
    by_recency.sort(key=lambda r: r["updated_at"], reverse=True)
    newest_kept = [r["key"] for r in by_recency if r["key"] in kept_keys]
    assert newest_kept == [
        r["key"] for r in by_recency[: len(kept)]
    ], "the rows that survive the cap must be the most recently updated ones"

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


def test_recency_ranks_mixed_timestamp_formats_by_instant_not_by_text() -> None:
    # Same day, written by two clocks: a space-separated stamp at noon and a
    # ``T``-separated one at 01:00. As text the space sorts before the ``T``, so
    # a string sort would keep the OLDER row; parsed, noon is the newer one.
    noon = dict(_row(1, 40, day=5), updated_at="2026-01-05 12:00:00")
    one_am = dict(_row(2, 40, day=5), updated_at="2026-01-05T01:00:00+00:00")
    unparseable = dict(_row(3, 40, day=5), updated_at="yesterday")
    rows = sorted([noon, one_am, unparseable], key=lambda r: r["key"])
    entries = [{"key": r["key"], "value_json": r["value_json"], "confidence": 0.9} for r in rows]
    one_row = len(json.dumps([entries[0]], indent=1))

    text, omitted, visible = _bounded_semantic_table(rows, entries, cap=one_row + 8)

    assert omitted == 2
    assert [e["key"] for e in json.loads(text)] == [noon["key"]]
    assert visible == {noon["key"]}
    # Unparseable stamps rank last: with room for two rows the parsed pair survives.
    two_rows = len(json.dumps(entries[:2], indent=1)) + 64
    _, omitted, visible = _bounded_semantic_table(rows, entries, cap=two_rows)
    assert omitted == 1
    assert visible == {noon["key"], one_am["key"]}


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
