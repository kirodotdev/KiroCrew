"""A value the consolidation size bound refused is listed once in the session's next prompt.

The pass marks its span consolidated whatever the bound refused, so the messages that held
the fact are not read again. The refused value is recorded whole in the store's
``memory_meta`` (``consolidation_refusal_notes``) and the session's next consolidation
prompt lists it, so the model can propose it again as separate keys within the bound. A
pass that showed the note and wrote its answer clears it; a value refused again unchanged
is not shown a third time. The passes and stores are the ones
``test_consolidation_semantic_item_bound.py`` builds.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from test_consolidation_semantic_item_bound import (
    _BOUND,
    _LARGE,
    _LARGE_KEY,
    _SMALL,
    _SMALL_KEY,
    _item,
    _store_for,
)

from kiro_crew import consolidation_refusal_notes as notes
from kiro_crew.history import HistoryConsolidator
from kiro_crew.testing.wait import finished
from kiro_crew.vector_memory import VectorMemoryStore

_HEADING = notes.REFUSAL_NOTICE_HEADING


async def _pass(
    c: HistoryConsolidator, generation: int, *items: object, session: str = "k", answer=True
) -> list[str]:
    """One prefs-only pass over a fresh span of *session*; return its prompts.

    Each pass reads a different generation, so a member store's receipt for an earlier
    pass is not replayed in its place. *answer* False makes the model turn unusable.
    """
    c._log.snapshot_for_consolidation.return_value = (
        [{"role": "user", "content": f"turn {generation}"}],
        1,
        generation,
        0,
    )
    prompts: list[str] = []

    async def fake_llm(prompt: str, *, memory_store: str = "", session_key: str = ""):
        prompts.append(prompt)
        return {"semantic": list(items)} if answer else None

    with patch.object(c, "_call_llm", side_effect=fake_llm):
        await finished(c._consolidate(session, include_history=False), "consolidation pass")
    return prompts


def _notice(prompt: str) -> list[dict] | None:
    """The entries the prompt's refusal section lists, or ``None`` without one."""
    if _HEADING not in prompt:
        return None
    section = prompt.split(_HEADING, 1)[1]
    start = section.index("\n[")
    decoded, _ = json.JSONDecoder().raw_decode(section[start + 1 :])
    return decoded


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1", "member"])
async def test_a_refused_value_is_listed_whole_in_the_next_prompt_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch, opened, path: str
) -> None:
    store, c = _store_for(path, tmp_path, monkeypatch, opened)

    first = await _pass(c, 1, _item(_SMALL_KEY, _SMALL), _item(_LARGE_KEY, _LARGE))
    assert _notice(first[0]) is None, "nothing was refused before the first pass"
    assert store.get_semantic(_LARGE_KEY) is None

    second = await _pass(c, 2)
    assert _notice(second[0]) == [{"key": _LARGE_KEY, "chars": len(_LARGE), "value": _LARGE}]
    assert f"over the {_BOUND}-character bound" in second[0]

    third = await _pass(c, 3)
    assert _notice(third[0]) is None, "the note is cleared once a pass has shown it"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1", "member"])
async def test_the_fact_proposed_again_split_is_stored(
    tmp_path, monkeypatch: pytest.MonkeyPatch, opened, path: str
) -> None:
    store, c = _store_for(path, tmp_path, monkeypatch, opened)
    await _pass(c, 1, _item(_LARGE_KEY, _LARGE))

    half = len(_LARGE) // 2
    await _pass(
        c,
        2,
        _item(_LARGE_KEY + "_a", _LARGE[:half]),
        _item(_LARGE_KEY + "_b", _LARGE[half:]),
    )

    assert store.get_semantic(_LARGE_KEY + "_a") is not None
    assert store.get_semantic(_LARGE_KEY + "_b") is not None
    assert _notice((await _pass(c, 3))[0]) is None


@pytest.mark.asyncio
async def test_a_value_refused_again_unchanged_is_not_listed_a_third_time(
    tmp_path, monkeypatch: pytest.MonkeyPatch, opened
) -> None:
    _, c = _store_for("v1", tmp_path, monkeypatch, opened)
    await _pass(c, 1, _item(_LARGE_KEY, _LARGE))

    second = await _pass(c, 2, _item(_LARGE_KEY, _LARGE))
    assert _notice(second[0]) is not None

    assert _notice((await _pass(c, 3))[0]) is None


@pytest.mark.asyncio
async def test_a_shortened_value_still_over_the_bound_is_listed_again(
    tmp_path, monkeypatch: pytest.MonkeyPatch, opened
) -> None:
    _, c = _store_for("v1", tmp_path, monkeypatch, opened)
    await _pass(c, 1, _item(_LARGE_KEY, _LARGE))
    shorter = _LARGE[:-100]

    await _pass(c, 2, _item(_LARGE_KEY, shorter))

    assert _notice((await _pass(c, 3))[0]) == [
        {"key": _LARGE_KEY, "chars": len(shorter), "value": shorter}
    ]


@pytest.mark.asyncio
async def test_a_pass_whose_answer_was_unusable_keeps_the_note(
    tmp_path, monkeypatch: pytest.MonkeyPatch, opened
) -> None:
    _, c = _store_for("v1", tmp_path, monkeypatch, opened)
    await _pass(c, 1, _item(_LARGE_KEY, _LARGE))

    unusable = await _pass(c, 2, answer=False)
    assert _notice(unusable[0]) is not None

    assert _notice((await _pass(c, 3))[0]) is not None, "no answer was written, so it stays"


@pytest.mark.asyncio
async def test_only_the_session_that_refused_a_value_is_shown_it(
    tmp_path, monkeypatch: pytest.MonkeyPatch, opened
) -> None:
    _, c = _store_for("v1", tmp_path, monkeypatch, opened)
    await _pass(c, 1, _item(_LARGE_KEY, _LARGE), session="alpha")

    assert _notice((await _pass(c, 2, session="beta"))[0]) is None
    assert _notice((await _pass(c, 3, session="alpha"))[0]) is not None


def test_the_record_keeps_only_the_newest_entries(tmp_path, opened) -> None:
    store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
    store.init()
    count = notes.MAX_REFUSAL_NOTES + 3

    for index in range(count):
        notes.record_refusals(store, "k", [(f"project.f{index}", "v" * 3000, 3000)])

    kept = [entry["key"] for entry in notes.pending_refusals(store, "k")]
    assert kept == [f"project.f{index}" for index in range(3, count)]


def test_an_unreadable_record_is_no_note_and_is_replaced(tmp_path, opened) -> None:
    store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
    store.init()
    store._write_meta(notes.REFUSAL_NOTES_META_KEY, "{not json")

    assert notes.pending_refusals(store, "k") == ()
    notes.record_refusals(store, "k", [("project.f", "v" * 3000, 3000)])
    assert [e["key"] for e in notes.pending_refusals(store, "k")] == ["project.f"]


def test_no_entries_render_no_section() -> None:
    assert notes.render_refusal_notice((), _BOUND) == ""
