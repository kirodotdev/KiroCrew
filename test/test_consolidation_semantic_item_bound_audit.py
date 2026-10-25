"""A consolidation pass audits each value the size bound refuses in ``memory_events``.

Each value ``_refuse_oversized_semantic`` refuses is one ``consolidation_value_size`` row,
written by ``_audit_oversized_semantic`` inside a publication hold the pass takes without the
bound too: on the V1 path the hold of the refused value's own turn, on the member path the
hold ``apply_consolidation`` runs under, before that call opens its transaction. So writes
turned off before the run's first commit, or a lost lease, write no row, and after that commit
writes turned off still let it land. The passes and stores are the ones
``test_consolidation_semantic_item_bound.py`` builds, shared so each file stays under the
line cap.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import patch

import pytest
from test_consolidation_semantic_item_bound import (
    _BOUND,
    _CODE,
    _LARGE,
    _LARGE_KEY,
    _SMALL,
    _SMALL_KEY,
    _audited,
    _delete,
    _item,
    _run,
    _store_for,
)

from kiro_crew import history_consolidation
from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED, _LEASE_TOKEN
from kiro_crew.testing.wait import finished


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1", "member"])
async def test_refusal_is_audited_in_memory_events(
    tmp_path, monkeypatch: pytest.MonkeyPatch, opened, path: str
) -> None:
    store, c = _store_for(path, tmp_path, monkeypatch, opened)

    outcome, _ = await _run(
        c,
        _item(_SMALL_KEY, _SMALL),
        _item(_LARGE_KEY, _LARGE),
        # The same key again, still over the bound: a second refused value, a second row.
        _item(_LARGE_KEY, _LARGE + "L"),
    )

    assert outcome is None
    rows = _audited(store)
    assert [(r["memory_type"], r["memory_key"], r["source"]) for r in rows] == [
        ("semantic", _LARGE_KEY, "consolidation:k")
    ] * 2
    # The shape every audited reject has: no old value, the new one cut to 200.
    assert [r["old_value"] for r in rows] == [None] * 2
    assert [r["new_value"] for r in rows] == [json.dumps(_LARGE)[:200]] * 2
    assert store.get_rejection_stats().get(_CODE) == 2
    assert store.get_semantic(_LARGE_KEY) is None
    assert store.get_semantic(_SMALL_KEY) is not None, "the rest of the answer still lands"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1", "member"])
async def test_a_refused_non_ascii_value_is_audited_as_written(
    tmp_path, monkeypatch: pytest.MonkeyPatch, opened, path: str
) -> None:
    # Like every audited reject, the row holds the value as JSON with non-ASCII as
    # written, cut to 200 characters.
    value = "я" * (_BOUND + 1)
    store, c = _store_for(path, tmp_path, monkeypatch, opened)

    outcome, _ = await _run(c, _item("project.cyrillic", value))

    assert outcome is None
    assert [(r["memory_key"], r["new_value"]) for r in _audited(store)] == [
        ("project.cyrillic", json.dumps(value, ensure_ascii=False)[:200])
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1", "member"])
@pytest.mark.parametrize("cause", ["writes-off", "lease-lost"])
async def test_no_audit_when_writes_are_off_or_the_lease_is_lost(
    tmp_path, monkeypatch: pytest.MonkeyPatch, opened, path: str, cause: str
) -> None:
    store, c = _store_for(path, tmp_path, monkeypatch, opened)
    disabled = False
    monkeypatch.setattr(history_consolidation, "_persistence_disabled", lambda: disabled)

    def while_thinking() -> None:
        nonlocal disabled
        if cause == "writes-off":
            disabled = True
        else:
            # A peer whose takeover landed on the session's metadata line.
            c._log.update_metadata_if("k", {_LEASE_TOKEN: "a-peer-token"}, lambda _meta: True)

    outcome, prompts = await _run(
        c, _item(_SMALL_KEY, _SMALL), _item(_LARGE_KEY, _LARGE), while_thinking=while_thinking
    )

    assert len(prompts) == 1, "the pass reached the model, so the refusal was screened"
    assert outcome is _CONSOLIDATION_REFUSED
    assert _audited(store) == []
    assert store.get_rejection_stats().get(_CODE) is None
    assert store.get_semantic(_SMALL_KEY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1", "member"])
async def test_the_audit_row_takes_the_refused_writes_turn(
    tmp_path, monkeypatch: pytest.MonkeyPatch, opened, path: str
) -> None:
    # Writes turn off right after the audit row lands. The row is written under the hold
    # the refused value's write takes without the bound (V1), or the one the whole answer
    # is written under (member), so the pass meets the run as it would without the
    # bound: that write would have committed it, and the item within the bound lands.
    # The row is written in that turn, so before the write of the item after it.
    store, c = _store_for(path, tmp_path, monkeypatch, opened)
    disabled = False
    monkeypatch.setattr(history_consolidation, "_persistence_disabled", lambda: disabled)
    log_reject_event = store.log_reject_event
    order: list[str] = []

    def audit_then_turn_writes_off(*args, **kwargs) -> None:
        nonlocal disabled
        order.append("audit")
        log_reject_event(*args, **kwargs)
        disabled = True

    monkeypatch.setattr(store, "log_reject_event", audit_then_turn_writes_off)
    writer = "set_semantic" if path == "v1" else "apply_consolidation"
    write = getattr(store, writer)

    def recorded_write(*args, **kwargs):
        order.append("write")
        return write(*args, **kwargs)

    monkeypatch.setattr(store, writer, recorded_write)

    outcome, _ = await _run(c, _item(_LARGE_KEY, _LARGE), _item(_SMALL_KEY, _SMALL))

    assert order == ["audit", "write"]
    assert [r["memory_key"] for r in _audited(store)] == [_LARGE_KEY]
    assert outcome is None
    assert store.get_semantic(_SMALL_KEY) is not None
    assert store.get_semantic(_LARGE_KEY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1", "member"])
async def test_the_audit_row_is_written_inside_a_publication_hold(
    tmp_path, monkeypatch: pytest.MonkeyPatch, opened, path: str
) -> None:
    # The row is written while a hold the pass takes without the bound is held: on V1
    # the hold of the refused value's own turn, on the member path the one
    # apply_consolidation runs under, so that hold's lease and write checks cover it.
    # Written once the hold is let go, it could land after a peer took the lease over.
    store, c = _store_for(path, tmp_path, monkeypatch, opened)
    hold = c._log.publication_hold
    held = 0
    held_at_audit: list[int] = []

    @contextlib.contextmanager
    def counted_hold(session_key: str):
        nonlocal held
        with hold(session_key):
            held += 1
            try:
                yield
            finally:
                held -= 1

    c._log.publication_hold = counted_hold
    log_reject_event = store.log_reject_event

    def audit(*args, **kwargs) -> None:
        held_at_audit.append(held)
        log_reject_event(*args, **kwargs)

    monkeypatch.setattr(store, "log_reject_event", audit)

    outcome, _ = await _run(c, _item(_LARGE_KEY, _LARGE), _item(_SMALL_KEY, _SMALL))

    assert outcome is None
    assert held_at_audit == [1]
    assert [r["memory_key"] for r in _audited(store)] == [_LARGE_KEY]


@pytest.mark.asyncio
async def test_a_member_pass_audits_before_apply_consolidation_opens_its_transaction(
    tmp_path, monkeypatch: pytest.MonkeyPatch, opened
) -> None:
    # The rows are written under the hold, before apply_consolidation opens the span's
    # transaction, so an attempt whose write fails keeps them, like every audited reject
    # on main; the retry audits the value again.
    store, c = _store_for("member", tmp_path, monkeypatch, opened)

    def apply_consolidation(*_args, **_kwargs) -> dict:
        raise RuntimeError("the span's transaction failed")

    monkeypatch.setattr(store, "apply_consolidation", apply_consolidation)

    with pytest.raises(RuntimeError, match="the span's transaction failed"):
        await _run(c, _item(_LARGE_KEY, _LARGE))

    assert [r["memory_key"] for r in _audited(store)] == [_LARGE_KEY]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1", "member"])
@pytest.mark.parametrize(
    "answer", [[_delete("project.x")], []], ids=["one-delete", "an-empty-list"]
)
async def test_a_pass_that_refuses_nothing_takes_no_hold_of_its_own(
    tmp_path, monkeypatch: pytest.MonkeyPatch, opened, path: str, answer: list
) -> None:
    # Writes turn off while the model works. A pass whose answer the bound refuses
    # nothing in writes nothing, so no hold refuses it, with the bound or without.
    outcomes = {}
    for bound in (True, False):
        where = tmp_path / ("bound" if bound else "unbound")
        where.mkdir()
        store, c = _store_for(path, where, monkeypatch, opened)
        disabled = False

        def while_thinking() -> None:
            nonlocal disabled
            disabled = True

        with monkeypatch.context() as patched:
            patched.setattr(history_consolidation, "_persistence_disabled", lambda: disabled)
            if not bound:
                patched.setattr(
                    history_consolidation,
                    "_refuse_oversized_semantic",
                    lambda result, *_args, **_kwargs: (result, ()),
                )
            outcomes[bound], _ = await _run(c, *answer, while_thinking=while_thinking)

    assert outcomes[True] is outcomes[False]


@pytest.mark.asyncio
async def test_a_v1_pass_audits_after_its_history_entry_under_the_commit_it_made(
    tmp_path, monkeypatch: pytest.MonkeyPatch, opened
) -> None:
    # On the V1 path the history entry is appended before the writer runs, and that
    # append is the run's first commit. Writes turn off right after it, so the audit row
    # lands, like the item within the bound, only because the run has committed.
    store, c = _store_for("v1", tmp_path, monkeypatch, opened)
    disabled = False
    monkeypatch.setattr(history_consolidation, "_persistence_disabled", lambda: disabled)
    order: list[str] = []

    def append_history(entry: str) -> None:
        nonlocal disabled
        order.append("history")
        disabled = True

    c._memory.append_history.side_effect = append_history
    log_reject_event = store.log_reject_event

    def audit(*args, **kwargs) -> None:
        order.append("audit")
        log_reject_event(*args, **kwargs)

    monkeypatch.setattr(store, "log_reject_event", audit)

    async def fake_llm(prompt: str, *, memory_store: str = "", session_key: str = "") -> dict:
        return {
            "history_entry": "Talked about two project facts.",
            "semantic": [_item(_SMALL_KEY, _SMALL), _item(_LARGE_KEY, _LARGE)],
        }

    with patch.object(c, "_call_llm", side_effect=fake_llm):
        outcome = await finished(c._consolidate("k", include_history=True), "consolidation pass")

    assert order == ["history", "audit"]
    assert outcome is None
    assert [r["memory_key"] for r in _audited(store)] == [_LARGE_KEY]
    assert store.get_semantic(_SMALL_KEY) is not None
