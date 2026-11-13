"""The warm brief keeps the owner's pending option buttons as working chips."""

from __future__ import annotations

import pytest

from kiro_crew.dashboard import member_waiting, warm_task_brief


def _row(role, content, **meta):
    return {"role": role, "content": content, "cls": "", "meta": meta}


_ASK = "Land it? [OPTIONS: Merge it | Rebase first | Hold]"


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        # The newest reply's choices are copied, across brief rows, notices and tools.
        (
            [
                _row("assistant", _ASK),
                _row("tool_call", "x"),
                _row("notice", "", kind="warm_task_brief_claim"),
                _row("assistant", "Welcome back.", kind="warm_task_brief_result"),
                _row("assistant", "compacted", kind="compaction"),
                _row("assistant", "\u200b"),
            ],
            "[OPTIONS: Merge it | Rebase first | Hold]",
        ),
        # A note row with options is a source; a newer marker-less reply is not.
        (
            [{"role": "inject", "content": _ASK, "cls": "reconcile-note", "meta": {}}],
            "[OPTIONS: Merge it | Rebase first | Hold]",
        ),
        ([_row("assistant", _ASK), _row("assistant", "Done.")], ""),
        # An answered or stopped choice is never copied.
        ([_row("assistant", _ASK), _row("user", "Hold")], ""),
        ([_row("assistant", _ASK), _row("queued", "Hold")], ""),
        ([_row("assistant", _ASK), _row("system", "Stopped", kind="stop_event")], ""),
        # A live Stop card carries its kind only as JSON in ``cls``, with no meta.
        (
            [
                _row("assistant", _ASK),
                {"role": "system", "content": "x", "cls": '{"kind": "stop_event"}'},
            ],
            "",
        ),
        ([], None),
        # Without a pipe the chip bar splits on commas; the copy joins with pipes.
        ([_row("assistant", "[OPTIONS: Merge, Rebase, Hold]")], "[OPTIONS: Merge | Rebase | Hold]"),
        # A piped marker keeps comma-bearing labels whole, even a lone one.
        (
            [_row("assistant", "[OPTIONS: Rebase, then merge | Hold]")],
            "[OPTIONS: Rebase, then merge | Hold]",
        ),
        ([_row("assistant", "[OPTIONS: Rebase, then merge |]")], "[OPTIONS: Rebase, then merge |]"),
        # A newer single-select note ends the scan; older choices are not resurrected.
        (
            [
                _row("assistant", _ASK),
                {
                    "role": "inject",
                    "content": "[OPTION: Open | Dismiss]",
                    "cls": "reconcile-note",
                    "meta": {},
                },
            ],
            "",
        ),
    ],
)
def test_pending_options_copies_only_the_chip_bar_source(rows, expected):
    assert member_waiting.pending_options(rows) == expected


def test_brief_row_carries_the_copied_marker_as_its_last_line(monkeypatch):
    from kiro_crew.dashboard import state as state_mod

    posted: list[str] = []
    monkeypatch.setattr(
        state_mod,
        "append_and_surface",
        lambda _state, _slot, _role, content, _cls, meta: posted.append(content) or meta,
    )
    kind = "warm_task_brief_result"
    warm_task_brief._post_message(
        None, None, "x", kind=kind, fingerprint="f", options="[OPTIONS: A]"
    )
    warm_task_brief._post_message(None, None, "x", kind=kind, fingerprint="f")

    assert posted == ["x\n\n[OPTIONS: A]", "x"]


def test_waiting_item_keeps_the_ask_as_its_source_under_a_brief_with_copied_buttons():
    from kiro_crew.dashboard import member_waiting

    rows = [
        {"role": "assistant", "content": _ASK, "ts": "2026-11-02T10:00:00Z", "meta": {}},
        {
            "role": "assistant",
            "content": "Nothing new since 10:00.\n\n[OPTIONS: Merge it | Rebase first | Hold]",
            "ts": "2026-11-02T11:00:00Z",
            "meta": {"kind": "warm_task_brief_result"},
        },
    ]
    (item,) = member_waiting._option_items(rows)

    assert item["text"] == "Land it?"
    assert item["asked_at"] == "2026-11-02T10:00:00Z"

    # The brief's copied (ledger-checked) choices are the item's choices.
    rows[1]["content"] = "Nothing new since 10:00.\n\n[OPTIONS: Rebase first | Hold]"
    (item,) = member_waiting._option_items(rows)
    assert item["options"] == ["Rebase first", "Hold"]

    # A comma-separated ask keeps its item under the copied, pipe-joined line.
    comma = [
        {
            "role": "assistant",
            "content": "Land it? [OPTIONS: Merge, Rebase, Hold]",
            "ts": "t1",
            "meta": {},
        },
        {
            "role": "assistant",
            "content": "Back.\n\n[OPTIONS: Merge | Rebase | Hold]",
            "ts": "t2",
            "meta": {"kind": "warm_task_brief_result"},
        },
    ]
    (item,) = member_waiting._option_items(comma)
    assert item["options"] == ["Merge", "Rebase", "Hold"]
    # A pipe on a later line does not change how the marker's own line splits.
    comma[0]["content"] = "Land it? [OPTIONS: Merge, Rebase, Hold]\nNote: a | b"
    (item,) = member_waiting._option_items(comma)
    assert item["options"] == ["Merge", "Rebase", "Hold"]

    # Choices copied from a note do not attach to an older, unrelated ask.
    rows[1]["content"] = "Back.\n\n[OPTIONS: Retry backup | Inspect logs]"
    assert member_waiting._option_items(rows) == []

    # A brief with no copied choices retires the ask, as any newer reply does.
    rows[1]["content"] = "Nothing new since 10:00."
    assert member_waiting._option_items(rows) == []


def test_every_post_copies_from_the_live_window_when_it_decides():
    from types import SimpleNamespace

    settled = SimpleNamespace(pending_options="[OPTIONS: Old]", work_items=())
    note = {"role": "inject", "content": "[OPTIONS: New | Cancel]", "cls": "reconcile-note"}

    # A note in memory, not yet on disk, wins (quiet and model posts alike).
    assert warm_task_brief._live_options(SimpleNamespace(messages=[note]), settled) == (
        "[OPTIONS: New | Cancel]"
    )
    # A window with no deciding row keeps the settled disk read.
    assert warm_task_brief._live_options(SimpleNamespace(messages=[]), settled) == "[OPTIONS: Old]"
