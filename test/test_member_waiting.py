"""What a crewmate waits on the user for (``dashboard.member_waiting``).

Pins the Dashboard tab's "Waiting on you" list:

* a crewmate message ending in an ``[OPTIONS:]`` line is an item until the
  person speaks, and a gateway wake in between does not clear it;
* an ``ask_question`` card still waiting is an item;
* newest first, bounded, redacted.
"""

from __future__ import annotations

from kiro_crew.dashboard import member_waiting
from kiro_crew.history import HUMAN_TURN_META_KEY

ASK = "Open a PR for the docs fix? [OPTIONS: Open the PR | Not yet]"


class _Slot:
    key = "member-dm:atlas"

    def __init__(self, rows, pending=None):
        self.rows = rows
        self._question_pending = pending or {}

    def sidebar_messages(self):
        return self.rows


class _State:
    def __init__(self, slot, questions=None):
        self.slot = slot
        self._pending_questions = questions or {}

    def get_slot(self, key):
        return self.slot if key == self.slot.key else None


def _user(text, ts):
    return {"role": "user", "content": text, "ts": ts, "meta": {HUMAN_TURN_META_KEY: True}}


def _said(text, ts):
    return {"role": "assistant", "content": text, "ts": ts}


def _items(rows, **kw):
    slot = _Slot(rows, kw.get("pending"))
    return member_waiting.waiting_items(_State(slot, kw.get("questions")), slot.key)


def test_an_unanswered_options_question_waits_on_the_user():
    rows = [
        _user("Fix the docs.", "2026-10-18T18:00:00+00:00"),
        _said(ASK, "2026-10-18T18:40:00+00:00"),
        # A heartbeat wake in between that says nothing keeps it listed.
        {"role": "tool", "content": "", "ts": "2026-10-18T18:55:00+00:00"},
        _said("", "2026-10-18T18:55:01+00:00"),
    ]
    (item,) = _items(rows)
    assert item["kind"] == "options"
    assert item["text"] == "Open a PR for the docs fix?"
    assert item["options"] == ["Open the PR", "Not yet"]
    assert item["asked_at"] == "2026-10-18T18:40:00+00:00"


def test_the_persons_reply_clears_it():
    rows = [
        _said(ASK, "2026-10-18T18:40:00+00:00"),
        _user("Open the PR", "2026-10-18T18:41:00+00:00"),
        _said("Opened it.", "2026-10-18T18:42:00+00:00"),
    ]
    assert _items(rows) == []


def test_a_message_without_options_is_not_a_question():
    assert _items([_said("All four milestones are met.", "2026-10-18T18:40:00+00:00")]) == []


def test_an_ask_question_card_waits_too_and_the_list_is_newest_first():
    rows = [_said(ASK, "2026-10-18T18:40:00+00:00")]
    pending = {
        "c1": {
            "questions": [
                {"question": "Which tracker?", "options": [{"label": "SIM"}, {"label": "Taskei"}]}
            ],
            "ts": 1792349000.0,  # 2026-10-18T19:23:20Z, after the options message
        }
    }
    items = _items(rows, pending=pending)
    assert [i["kind"] for i in items] == ["question", "options"]
    assert items[0]["options"] == ["SIM", "Taskei"]


def test_an_older_card_sorts_after_a_newer_message():
    rows = [_said(ASK, "2026-10-18T19:30:00+00:00")]
    pending = {"c1": {"questions": [{"question": "Which tracker?"}], "ts": 1792349000.0}}
    assert [i["kind"] for i in _items(rows, pending=pending)] == ["options", "question"]


def test_the_list_is_bounded_and_redacted():
    token = "ghp_" + "a" * 36
    rows = [
        _said(f"Use {token}? [OPTIONS: Yes | No]", f"2026-10-18T18:{m:02d}:00+00:00")
        for m in range(10)
    ]
    pending = {
        f"c{n}": {"questions": [{"question": f"Pick {token} #{n}?"}], "ts": 1792340000.0 + n}
        for n in range(10)
    }
    items = _items(rows, pending=pending)
    assert len(items) == member_waiting.MAX_ITEMS
    assert all(token not in i["text"] for i in items)
    assert items[0]["asked_at"] == "2026-10-18T18:09:00+00:00"
    # Only the newest reply's ask is listed; the nine older ones are not.
    assert [i["kind"] for i in items].count("options") == 1


def test_a_quiet_reply_retires_the_ask_as_the_sidebar_does():
    # The sidebar's has_options reads the newest conversation row with text: a quiet
    # zero-width reply is that row, so the older ask is not waiting.
    rows = [_said(ASK, "2026-10-18T18:40:00+00:00"), _said("\u200b", "2026-10-18T18:55:01+00:00")]
    assert _items(rows) == []


def test_an_item_counts_the_choices_it_cut():
    many = " | ".join(f"c{n}" for n in range(member_waiting.MAX_OPTIONS + 2))
    (item,) = _items([_said(f"Pick one? [OPTIONS: {many}]", "2026-10-18T18:40:00+00:00")])
    assert len(item["options"]) == member_waiting.MAX_OPTIONS
    assert item["options_total"] == member_waiting.MAX_OPTIONS + 2


def test_the_producer_shares_the_folds_bounds():
    from kiro_crew.crew_log import entry_types as et

    assert member_waiting.TEXT_MAX_CHARS == et.CREWMATE_TEXT_LIMIT
    assert member_waiting.MAX_OPTIONS == et.CREWMATE_OPTION_LIMIT
    assert member_waiting.MAX_ITEMS == et.CREWMATE_WAITING_LIMIT


def test_a_newer_plain_reply_retires_an_older_options_ask():
    # The "options are read from the newest reply only" entry in docs/decisions.
    rows = [
        _said(ASK, "2026-10-18T18:40:00+00:00"),
        _said("Still working on the docs fix.", "2026-10-18T18:50:00+00:00"),
    ]
    assert _items(rows) == []


def test_a_thread_that_is_not_open_has_nothing_waiting():
    assert member_waiting.waiting_items(_State(_Slot([])), "member-dm:other") == []


class _Cards:
    def __init__(self, cards):
        self.cards = cards

    def pending(self, _slot_key):
        return self.cards

    def last_finished_at(self, _slot_key, statuses):
        done = [c.get("finished_at", 0.0) for c in self.cards if c.get("status") in statuses]
        return float(max(done, default=0.0))


def _with_cards(monkeypatch, cards):
    from kiro_crew.dashboard import change_cards

    monkeypatch.setattr(change_cards, "card_store_for", lambda _s: _Cards(cards))


ASKED = "2026-10-18T18:40:00+00:00"
ASKED_S = 1792348800.0  # 2026-10-18T18:40:00Z
SLOT = "member-dm:atlas"


def test_pressing_a_card_after_the_question_clears_it(monkeypatch):
    for status in ("applied", "cancelled", "partial", "failed"):
        _with_cards(monkeypatch, [{"status": status, "finished_at": ASKED_S + 60}])
        assert _items([_said(ASK, ASKED)]) == [], status


def test_a_card_that_only_expired_or_was_pressed_earlier_keeps_the_question(monkeypatch):
    _with_cards(monkeypatch, [{"status": "expired", "finished_at": ASKED_S + 60}])
    assert len(_items([_said(ASK, ASKED)])) == 1
    _with_cards(monkeypatch, [{"status": "applied", "finished_at": ASKED_S - 60}])
    assert len(_items([_said(ASK, ASKED)])) == 1


def test_a_press_still_answers_once_its_card_is_dismissed_or_past_its_ttl(tmp_path, monkeypatch):
    """``pending`` hides a dismissed card and one past its display TTL; the press on it
    still answered the question, so the question does not come back."""
    from collections import OrderedDict

    from kiro_crew.dashboard import change_cards

    store = change_cards.CardStore(tmp_path / "cards.json", key=lambda: b"k" * 32)
    store._loaded = True
    store._cards = OrderedDict(
        {
            "c1": {"id": "c1", "slot_key": SLOT, "status": "applied", "dismissed": True,
                   "finished_at": ASKED_S + 60},
            "c2": {"id": "c2", "slot_key": "member-dm:other", "status": "applied",
                   "finished_at": ASKED_S + 90},
        }
    )  # fmt: skip
    assert store.last_finished_at(SLOT, frozenset({"applied"})) == ASKED_S + 60
    assert store.last_finished_at(SLOT, frozenset({"failed"})) == 0.0
    monkeypatch.setattr(change_cards.CardStore, "pending", lambda self, slot: [])
    monkeypatch.setattr(change_cards, "card_store_for", lambda _s: store)
    assert _items([_said(ASK, ASKED)]) == []


def test_a_failed_card_still_waits_on_the_user(monkeypatch):
    """A failed card offers Retry and Dismiss, so it stays in the list."""
    _with_cards(
        monkeypatch,
        [{"status": "failed", "title": "Set the milestones", "created_at": ASKED_S + 60}],
    )
    items = _items([_said(ASK, ASKED)])
    assert ("card", "Set the milestones") in [(i["kind"], i["text"]) for i in items]


def test_a_pending_change_card_waits_on_the_user(monkeypatch):
    _with_cards(
        monkeypatch,
        [{"status": "pending", "title": "Set the milestones", "created_at": ASKED_S + 60}],
    )
    items = _items([_said(ASK, ASKED)])
    assert [(i["kind"], i["text"]) for i in items] == [
        ("card", "Set the milestones"),
        ("options", "Open a PR for the docs fix?"),
    ]
