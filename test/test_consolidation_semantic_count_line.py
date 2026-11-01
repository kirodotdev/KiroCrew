"""Each over-cap consolidation pass logs one line of what its semantic block was built from.

The line is INFO on `kiro_crew.history`, logged once the block is selected and before
the billed turn: the table's rows and characters, the cap, what the block shows, each
pool's rows and characters, what became of the relevance scores, and where the rotation
began, as ordinals. It holds counts and a closed set of words only, never a memory
key, value, span text or exception message; like every consolidation line it names
the pass by its session key. Every expected number here is read off what the pass
rendered into the prompt and handed its scorer, not off the store rows, which the
pass renders differently (a lesson as its rule text).
"""

from __future__ import annotations

import json
import logging
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from test_consolidation_semantic_cap import (
    _CURSOR,
    _POOLED_CURSOR,
    _block_of,
    _consolidator,
    _oversized_table,
    _pooled_table,
    _row,
    _until_no_scorer_runs,
    _writer_store,
)
from test_consolidation_semantic_selection import _Table, _uniform

from kiro_crew.history import HistoryConsolidator
from kiro_crew.history_consolidation import (
    _CONSOLIDATION_BUSY,
    _CONSOLIDATION_REFUSED,
    _SEMANTIC_OMISSION_NOTICE,
    _select_semantic_rows,
)
from kiro_crew.vector_memory_constants import _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION
from kiro_crew.vector_memory_runtime import consolidation_relevance

_POOLS = ("rotation", "lessons", "recency", "relevance", "backfill")
_FIELDS = (
    "table_rows",
    "table_chars",
    "cap",
    "shown_rows",
    "shown_chars",
    "scores",
    "cursor",
    "rotation_start",
    "rotation_wrapped",
    *(f"{pool}_{unit}" for pool in _POOLS for unit in ("rows", "chars")),
)
#: Every word the line may carry besides the session key.
_WORDS = frozenset(
    {"used", "no_positive", "no_signal", "timeout", "error", "space_changed"}
    | {"set", "none", "yes", "no"}
)
#: In the pooled table each pool shows only the rows of one prefix, given its scores.
_POOLED_PREFIXES = ("project.a_rot_", "lesson.", "project.n_", "project.r_", "project.b_")


def _selection_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "kiro_crew.history" and "table_rows=" in r.getMessage()
    ]


def _selection_line(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    """The one selection line the capture holds; none, or two, fails the test."""
    lines = _selection_lines(caplog)
    assert len(lines) == 1, f"{len(lines)} selection lines"
    return lines[0]


def _fields(record: logging.LogRecord) -> dict[str, str]:
    pairs = re.findall(r"(\w+)=(\S+)", record.getMessage().split(": ", 1)[1])
    assert tuple(name for name, _ in pairs) == _FIELDS
    return dict(pairs)


def _cost(entry: dict) -> int:
    return len(json.dumps([entry], indent=1)) - 2


async def _logged_pass(vector_store: MagicMock, caplog: pytest.LogCaptureFixture, **log) -> dict:
    """Run one history pass at INFO and return what its turn was shown.

    `seen["lines"]` counts the selection lines logged by the time the turn was sent;
    `seen["sentinel"]` proves the capture saw an INFO line in the same window.
    """
    c = _consolidator(vector_store)
    for name, value in log.items():
        setattr(c._log, name, value)
    seen: dict = {}

    async def model(prompt: str, **_kwargs) -> dict:
        seen["block"] = _block_of(prompt)
        seen["lines"] = len(_selection_lines(caplog))
        return {"semantic": []}

    with (
        patch.object(c, "_call_llm", side_effect=model),
        caplog.at_level(logging.INFO, logger="kiro_crew.history"),
    ):
        c._logger.info("sentinel")
        seen["outcome"] = await c._consolidate("k", include_history=True)
    seen["sentinel"] = any(r.getMessage() == "sentinel" for r in caplog.records)
    return seen


def _rendered_entries(vector_store: MagicMock) -> list[dict]:
    """The table as the pass rendered it: each row with the text it handed the scorer.

    This is the v1 entry shape; a v2 store's entries also carry ``record_revision``
    and ``metadata`` (``history_consolidation_pass.py``, where the pass builds
    ``semantic_entries``).
    """
    rows, texts, _query = vector_store.score_consolidation_rows.call_args.args
    return [
        {"key": r["key"], "value_json": text, "confidence": r["confidence"]}
        for r, text in zip(rows, texts)
    ]


@pytest.mark.asyncio
async def test_an_over_cap_pass_logs_what_each_pool_showed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rows, scores, _ = _pooled_table()
    vector_store = _writer_store(rows)
    vector_store.score_consolidation_rows.return_value = scores
    vector_store.consolidation_rotation_cursor.return_value = _POOLED_CURSOR

    seen = await _logged_pass(vector_store, caplog)

    record = _selection_line(caplog)
    assert record.levelno == logging.INFO
    assert seen["lines"] == 1, "logged before the billed turn"
    fields = _fields(record)
    entries = _rendered_entries(vector_store)
    body, _, notice = seen["block"].rpartition("\n")
    shown = json.loads(body)
    by_key = {entry["key"]: entry for entry in entries}
    assert shown == [by_key[entry["key"]] for entry in shown]
    expected = {
        "table_rows": len(entries),
        "table_chars": len(json.dumps(entries, indent=1)),
        "cap": _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION,
        "shown_rows": len(shown),
        "shown_chars": len(body) - 2,
        "rotation_start": sum(entry["key"] <= _POOLED_CURSOR for entry in entries),
    }
    pooled = [[e for e in shown if e["key"].startswith(p)] for p in _POOLED_PREFIXES]
    assert sum(map(len, pooled)) == len(shown)
    for pool, mine in zip(_POOLS, pooled):
        expected |= {f"{pool}_rows": len(mine), f"{pool}_chars": sum(map(_cost, mine))}
    assert {name: int(fields[name]) for name in expected} == expected
    # Every pool showed rows, and no two pools showed as many rows and characters.
    counts = {(fields[f"{pool}_rows"], fields[f"{pool}_chars"]) for pool in _POOLS}
    assert len(counts) == 5 and ("0", "0") not in counts
    assert (fields["scores"], fields["cursor"], fields["rotation_wrapped"]) == ("used", "set", "no")
    assert notice == _SEMANTIC_OMISSION_NOTICE.format(
        count=len(entries) - len(shown),
        total=len(entries),
        limit=_SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION,
    )


def _scorer(outcome: str, vector_store: MagicMock, count: int):
    """What the scorer does for *outcome*, as ``configure_mock`` keywords."""
    if outcome == "space_changed":

        def scored_across_a_model_swap(*_args, **_kwargs):
            vector_store.space_generation += 1
            return [1.0] * count

        return {"side_effect": scored_across_a_model_swap}
    return {
        "used": {"return_value": [1.0] * count},
        "no_positive": {"return_value": [0.0] * count},
        "no_signal": {"return_value": None},
        "no_signal-mock": {"return_value": MagicMock()},
        # Every row would score, had the scorer run.
        "no_signal-busy": {"return_value": [1.0] * count},
        "error": {"side_effect": RuntimeError("quokka-marmalade-q7x")},
        "timeout": {"side_effect": TimeoutError("quokka-marmalade-q7x")},
    }[outcome]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        "used",
        "no_positive",
        "no_signal",
        "no_signal-mock",
        "no_signal-busy",
        "error",
        "timeout",
        "space_changed",
    ],
)
async def test_the_selection_line_says_what_became_of_the_scores(
    outcome: str, caplog: pytest.LogCaptureFixture
) -> None:
    rows = _oversized_table()
    vector_store = _writer_store(rows)
    vector_store.score_consolidation_rows.configure_mock(
        **_scorer(outcome, vector_store, len(rows))
    )
    # An older test double answers a MagicMock for the cursor too: not a key, so none.
    mock = outcome == "no_signal-mock"
    vector_store.consolidation_rotation_cursor.return_value = MagicMock() if mock else _CURSOR
    # Another pass's scorer still holds the one-scorer claim, so this pass runs none.
    busy = outcome == "no_signal-busy"
    claim = consolidation_relevance._SCORER_IN_FLIGHT
    if busy:
        await _until_no_scorer_runs()
        assert claim.acquire(blocking=False)
    try:
        seen = await _logged_pass(vector_store, caplog)
    finally:
        # Only while still held, so a pass that let it go fails on what it logged.
        if busy and claim.locked():
            claim.release()

    record = _selection_line(caplog)
    fields = _fields(record)
    assert fields["scores"] == outcome.partition("-")[0]
    assert fields["cursor"] == ("none" if mock else "set")
    assert vector_store.score_consolidation_rows.called is not busy
    ranked = "chosen for relevance" in seen["block"].rpartition("\n")[2]
    assert ranked == (outcome == "used")
    assert ranked or fields["relevance_rows"] == fields["relevance_chars"] == "0"
    assert "quokka" not in record.getMessage()


@pytest.mark.asyncio
async def test_the_selection_line_names_no_key_value_or_span_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary_key, canary_value, span = (
        "project.a_canary_q7x",
        "quokka-marmalade-q7x",
        "remember periwinkle-q7x",
    )
    canary = dict(_row(0, 950, day=28), key=canary_key, value_json=json.dumps(canary_value))
    rows = [*_oversized_table(), canary]
    vector_store = _writer_store(rows)
    vector_store.score_consolidation_rows.return_value = [1.0] * len(rows)
    vector_store.consolidation_rotation_cursor.return_value = canary_key
    snapshot = MagicMock(return_value=([{"role": "user", "content": span}], 1, 0, 0))

    seen = await _logged_pass(vector_store, caplog, snapshot_for_consolidation=snapshot)

    record = _selection_line(caplog)
    message = record.getMessage()
    assert message and seen["lines"] == 1
    # The canaries reached the pass: the span was the query and the row was scored.
    rendered, _texts, query = vector_store.score_consolidation_rows.call_args.args
    assert query == span and canary_key in {r["key"] for r in rendered}
    texts = {
        canary_key,
        canary_value,
        span,
        *(r["key"] for r in rows),
        *(r["value_json"] for r in rows),
    }
    assert [text for text in texts if text in message] == []
    assert all(type(arg) in (int, str) for arg in record.args)
    words = {arg for arg in record.args if isinstance(arg, str)}
    assert words <= _WORDS | {"k"}
    assert [arg for arg in record.args if any(text in str(arg) for text in texts)] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["fits", "persistence-off", "non-persistent", "incognito", "temporary", "busy"]
)
async def test_a_pass_with_no_over_cap_block_logs_no_selection_line(
    case: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    vector_store = _writer_store(_oversized_table())
    vector_store.consolidation_rotation_cursor.return_value = _CURSOR
    log = {}
    if case == "fits":
        vector_store.get_all_semantic.return_value = [_row(i, 40, day=1) for i in range(30)]
    elif case == "persistence-off":
        monkeypatch.setattr("kiro_crew.history_consolidation._persistence_disabled", lambda: True)
    elif case == "non-persistent":
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution",
            lambda _key: SimpleNamespace(memory_mode="temporary"),
        )
    elif case == "busy":
        monkeypatch.setattr(
            HistoryConsolidator, "_acquire_consolidation_lease", lambda self, key: None
        )
    else:
        log["get_metadata"] = MagicMock(return_value={"memory_mode": case})

    seen = await _logged_pass(vector_store, caplog, **log)

    assert seen["sentinel"], "the capture saw an INFO line in the same window"
    assert _selection_lines(caplog) == []
    if case == "fits":
        assert seen["outcome"] is None
        vector_store.get_all_semantic.assert_called_once()
    else:
        assert seen["outcome"] is (
            _CONSOLIDATION_BUSY if case == "busy" else _CONSOLIDATION_REFUSED
        )
        vector_store.get_all_semantic.assert_not_called()


@pytest.mark.parametrize("failure", [None, "timeout", "error", "space_changed"])
@pytest.mark.parametrize("scores", ["none", "zeros", "positive"])
def test_the_selection_line_carries_counts_and_closed_words_only(
    failure: str | None, scores: str, caplog: pytest.LogCaptureFixture
) -> None:
    table = _Table(_uniform(100))
    given = {"none": None, "zeros": [0.0] * 100, "positive": [1.0] * 100}[scores]
    _, selection = _select_semantic_rows(
        table.rows, table.entries, 65_536, scores=given, rotation_after=table.keys[10]
    )
    assert selection is not None

    with caplog.at_level(logging.INFO, logger="kiro_crew.history"):
        selection.log(failure, "k", logging.getLogger("kiro_crew.history"))

    record = _selection_line(caplog)
    fields = _fields(record)
    word = {"none": "no_signal", "zeros": "no_positive", "positive": "used"}[scores]
    assert fields["scores"] == (failure or word)
    assert fields["cursor"] == "set"
    assert all(type(arg) in (int, str) for arg in record.args)
    assert {arg for arg in record.args if isinstance(arg, str)} <= _WORDS | {"k"}


@pytest.mark.parametrize(
    ("after", "placed"),
    [(None, ("none", "no")), ("project.k_0010", ("set", "no")), ("project.k_0097", ("set", "yes"))],
    ids=["no-cursor", "resumed", "came-round"],
)
def test_the_selection_line_places_the_rotation_as_its_selection_does(
    after: str | None, placed: tuple[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    table = _Table(_uniform(100))
    _, selection = _select_semantic_rows(table.rows, table.entries, 65_536, rotation_after=after)
    assert selection is not None
    # Past the third key from the end, the slice runs off the end of the key order.
    assert selection.rotation_wrapped == (placed[1] == "yes")

    with caplog.at_level(logging.INFO, logger="kiro_crew.history"):
        selection.log(None, "k", logging.getLogger("kiro_crew.history"))

    record = _selection_line(caplog)
    fields = _fields(record)
    assert (fields["cursor"], fields["rotation_wrapped"]) == placed
    assert int(fields["rotation_start"]) == selection.rotation_start
