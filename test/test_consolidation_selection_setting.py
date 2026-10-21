"""``memory.consolidation_selection`` picks how a pass fills an over-cap semantic block.

``newest`` (the default) is the newest-first cut: no row is scored and the rotation cursor
is neither read nor moved, and the block is :func:`_newest_semantic_table`'s, under
:data:`_SEMANTIC_OMISSION_NOTICE_NEWEST`. ``relevance`` scores the rows, reads the cursor
and moves it once the answer is applied. The pools themselves are pinned in
``test_consolidation_semantic_cap.py`` and ``test_consolidation_semantic_selection.py``.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from consolidation_lease_helpers import lease_aware_log

from kiro_crew.config.loader import KiroCrewConfig, config_path
from kiro_crew.config.memory_sections import (
    CONSOLIDATION_SELECTION_MODES,
    consolidation_selection_mode_from,
)
from kiro_crew.history import HistoryConsolidator
from kiro_crew.history_consolidation_selection import (
    _SEMANTIC_OMISSION_NOTICE_NEWEST,
    _consolidation_selection,
    _newest_semantic_table,
)
from kiro_crew.vector_memory_constants import _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION

_HEADING = "## Current Semantic Memory"
_LOGGER = logging.getLogger("kiro_crew.history")


def _write_selection(value: object) -> None:
    """Write the isolated home's config with ``memory.consolidation_selection`` set."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"memory": {"consolidation_selection": value}}), encoding="utf-8")


def _rows() -> list[dict]:
    # 120 rows of ~1,000 rendered characters: about twice the cap, with keys that sort
    # unlike their age, so the newest-first cut is not a key-order prefix.
    return sorted(
        (
            {
                "key": f"project.row_{(index * 7919) % 10_000:05d}",
                "value_json": json.dumps(f"v{index}:" + "x" * 950),
                "confidence": 0.9,
                "updated_at": f"2026-01-{1 + index % 28:02d}T00:00:{index % 60:02d}",
            }
            for index in range(120)
        ),
        key=lambda row: row["key"],
    )


def _store(rows: list[dict]) -> MagicMock:
    store = MagicMock()
    store.algorithm_version = "v1"
    store.space_generation = 1
    store.get_all_semantic.return_value = [dict(row) for row in rows]
    store.score_consolidation_rows.side_effect = lambda found, *_a, **_k: [1.0] * len(found)
    store.consolidation_rotation_cursor.return_value = "project.row_05000"
    store.advance_consolidation_rotation.return_value = True
    store.set_semantic.return_value = None
    return store


async def _block_of_a_pass(store: MagicMock) -> str:
    block, _ = await _block_and_visible_keys(store)
    return block


async def _block_and_visible_keys(store: MagicMock) -> tuple[str, frozenset[str]]:
    """One pass's semantic block, and the keys the pass tells its writer the model saw."""
    log = lease_aware_log()
    log.snapshot_for_consolidation.return_value = ([{"role": "user", "content": "hi"}], 1, 0, 0)
    log.consolidation_retry_state.return_value = (0, 0.0)
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    consolidator = HistoryConsolidator(
        log=log, memory=memory, sessions=None, vector_store=store, migrated=True
    )
    seen: dict[str, object] = {}

    async def fake_llm(prompt: str, **_kwargs: object) -> dict:
        start = prompt.index(_HEADING) + len(_HEADING)
        seen["block"] = prompt[start : prompt.index("\n## ", start)]
        return {"semantic": []}

    def fake_write(_result: object, _key: str, vector_store: object = None, **kwargs: object):
        seen["visible"] = kwargs["visible_keys"]

    with (
        patch.object(consolidator, "_call_llm", side_effect=fake_llm),
        patch.object(consolidator, "_write_structured_memory", side_effect=fake_write),
    ):
        await consolidator._consolidate("k", include_history=False)
    return str(seen["block"]), frozenset(seen["visible"])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("stored", "read"),
    [
        (None, "newest"),
        ("newest", "newest"),
        ("relevance", "relevance"),
        ("Relevance", "newest"),
        ("", "newest"),
        (1, "newest"),
        (["relevance"], "newest"),
    ],
)
def test_the_setting_reads_newest_unless_it_says_relevance(stored: object, read: str) -> None:
    assert consolidation_selection_mode_from(stored) == read
    if stored is not None:
        _write_selection(stored)
    assert KiroCrewConfig.load().memory.consolidation_selection == read
    assert _consolidation_selection(_LOGGER) == read


def test_an_unreadable_config_reads_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> object:
        raise OSError("config unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", broken)
    assert _consolidation_selection(_LOGGER) == "newest"


def test_the_modes_are_the_two_the_selection_owner_handles() -> None:
    assert CONSOLIDATION_SELECTION_MODES == ("newest", "relevance")


@pytest.mark.asyncio
async def test_the_default_shows_the_newest_rows_and_touches_no_signal() -> None:
    rows = _rows()
    store = _store(rows)

    block = await _block_of_a_pass(store)

    entries = [
        {"key": row["key"], "value_json": row["value_json"], "confidence": row["confidence"]}
        for row in rows
    ]
    expected = _newest_semantic_table(rows, entries, _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION)
    assert expected.omitted > 0, "the table must be over the cap for the cut to show"
    assert block.strip() == expected.text
    assert block.rstrip().endswith(
        _SEMANTIC_OMISSION_NOTICE_NEWEST.format(
            count=expected.omitted,
            total=len(rows),
            limit=_SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION,
        )
    )
    # The newest row is shown and the oldest is cut, whatever their keys.
    newest = max(rows, key=lambda row: row["updated_at"])
    oldest = min(rows, key=lambda row: row["updated_at"])
    assert newest["key"] in expected.visible_keys
    assert oldest["key"] not in expected.visible_keys
    store.score_consolidation_rows.assert_not_called()
    store.consolidation_rotation_cursor.assert_not_called()
    store.advance_consolidation_rotation.assert_not_called()


@pytest.mark.asyncio
async def test_relevance_scores_the_rows_and_moves_the_cursor() -> None:
    _write_selection("relevance")
    store = _store(_rows())

    block = await _block_of_a_pass(store)

    assert "chosen for relevance" in block
    store.score_consolidation_rows.assert_called_once()
    store.consolidation_rotation_cursor.assert_called_once()
    store.advance_consolidation_rotation.assert_called_once()
    assert store.advance_consolidation_rotation.call_args.args[0] == "project.row_05000"


@pytest.mark.asyncio
async def test_a_table_that_fits_renders_whole_under_either_setting() -> None:
    rows = _rows()[:3]
    blocks = []
    for value in CONSOLIDATION_SELECTION_MODES:
        _write_selection(value)
        store = _store(rows)
        blocks.append(await _block_of_a_pass(store))
        store.score_consolidation_rows.assert_not_called()
        store.advance_consolidation_rotation.assert_not_called()
    assert blocks[0] == blocks[1]
    assert "[Context budget" not in blocks[0]


# ── the default's cut, pinned against expectations built here ──────────────────────


def test_the_newest_notice_keeps_its_wording() -> None:
    # Written out, not imported: the default prompt must not change, word for word.
    assert _SEMANTIC_OMISSION_NOTICE_NEWEST.format(count=3, total=9, limit=100) == (
        "[Context budget: omitted 3 of 9 semantic rows above the 100-character "
        "consolidation budget; the least recently updated rows were left out. The table "
        "above is PARTIAL: a key you do not see may still exist, so update or delete only "
        "keys listed above and treat a missing key as unknown, not absent.]"
    )


def _entries(rows: list[dict]) -> list[dict]:
    return [
        {"key": r["key"], "value_json": r["value_json"], "confidence": r["confidence"]}
        for r in rows
    ]


@pytest.mark.asyncio
async def test_the_default_keeps_as_many_of_the_newest_rows_as_fit() -> None:
    rows = _rows()
    store = _store(rows)

    block = await _block_of_a_pass(store)

    # Found by walking the newest-first order one row at a time, not by bisection: the
    # most rows that render, in key order, within the cap.
    newest_first = sorted(rows, key=lambda r: r["updated_at"], reverse=True)
    kept: list[dict] = []
    for row in newest_first:
        candidate = sorted([*kept, row], key=lambda r: r["key"])
        if len(json.dumps(_entries(candidate), indent=1)) > _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION:
            break
        kept = candidate
    assert 0 < len(kept) < len(rows)
    table, notice = block.strip().rsplit("\n", 1)
    assert table == json.dumps(_entries(kept), indent=1)
    assert notice == (
        f"[Context budget: omitted {len(rows) - len(kept)} of {len(rows)} semantic rows "
        f"above the {_SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION}-character consolidation "
        "budget; the least recently updated rows were left out. The table above is "
        "PARTIAL: a key you do not see may still exist, so update or delete only keys "
        "listed above and treat a missing key as unknown, not absent.]"
    )


def test_the_default_ranks_mixed_timestamp_formats_by_instant_not_by_text() -> None:
    # Same day, written by two clocks: a space-separated stamp at noon and a ``T`` one at
    # 01:00. As text the space sorts first, so a string sort would keep the older row.
    def row(key: str, stamp: str) -> dict:
        return {"key": key, "value_json": '"x"', "confidence": 0.9, "updated_at": stamp}

    noon = row("project.a", "2026-01-05 12:00:00")
    one_am = row("project.b", "2026-01-05T01:00:00+00:00")
    unparseable = row("project.c", "yesterday")
    rows = [noon, one_am, unparseable]
    entries = _entries(rows)
    one_row = len(json.dumps([entries[0]], indent=1))

    result = _newest_semantic_table(rows, entries, one_row + 8)
    assert result.omitted == 2
    assert result.visible_keys == {"project.a"}
    assert json.loads(result.text.split("\n[Context budget")[0]) == [entries[0]]
    # An unparseable stamp ranks last: with room for two rows the parsed pair stays.
    result = _newest_semantic_table(rows, entries, len(json.dumps(entries[:2], indent=1)) + 8)
    assert result.omitted == 1
    assert result.visible_keys == {"project.a", "project.b"}


@pytest.mark.asyncio
async def test_under_the_default_a_row_wider_than_the_cap_leaves_the_empty_table() -> None:
    wide = {
        "key": "project.wide",
        "value_json": json.dumps("x" * (_SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION + 100)),
        "confidence": 0.9,
        "updated_at": "2026-01-01T00:00:00",
    }
    block = await _block_of_a_pass(_store([wide]))

    table, notice = block.strip().split("\n", 1)
    assert table == "[]"
    assert notice.startswith("[Context budget: omitted 1 of 1 semantic rows")


@pytest.mark.asyncio
async def test_the_default_breaks_a_tie_by_key_and_ranks_an_unknown_stamp_last() -> None:
    # Every parsed stamp is the same instant, so only the key can pick among them, and
    # rows whose stamp is missing, not text or unparseable rank after all of them.
    rows = [
        {
            "key": f"project.t_{index:03d}",
            "value_json": json.dumps("x" * 950),
            "confidence": 0.9,
            "updated_at": "2026-01-05T00:00:00",
        }
        for index in range(100)
    ]
    for key, stamp in (
        ("project.a_none", None),
        ("project.a_bad", "soon"),
        ("project.a_list", [1]),
    ):
        rows.append({"key": key, "value_json": '"y"', "confidence": 0.9, "updated_at": stamp})
    rows.sort(key=lambda r: r["key"])

    block, visible = await _block_and_visible_keys(_store(rows))

    # Built here: the tied rows in key order, then the unknown stamps in key order,
    # taking rows while the key-ordered table still renders within the cap.
    ranked = sorted(
        (r for r in rows if r["updated_at"] == "2026-01-05T00:00:00"), key=lambda r: r["key"]
    )
    ranked += sorted(
        (r for r in rows if r["updated_at"] != "2026-01-05T00:00:00"), key=lambda r: r["key"]
    )
    kept: list[dict] = []
    for row in ranked:
        candidate = sorted([*kept, row], key=lambda r: r["key"])
        if len(json.dumps(_entries(candidate), indent=1)) > _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION:
            break
        kept = candidate
    assert 0 < len(kept) < 100, "the cut must fall inside the tied rows"
    assert visible == {r["key"] for r in kept}
    assert block.strip().rsplit("\n", 1)[0] == json.dumps(_entries(kept), indent=1)
    assert not visible & {"project.a_none", "project.a_bad", "project.a_list"}
