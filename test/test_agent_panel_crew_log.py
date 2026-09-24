"""The member panel as a crew-log entry type and a slot fold.

A publish writes two records. The DURABLE one is `crew-panels/<slug>.json`, which the
store owns and which keeps the panel readable on a gateway with the crew log off --
the default -- and after a session's unit is collected. The ADDITIONAL one is a
``panel/published`` line in that member's own DM session log, folded by ``panel``,
and it is what gives a panel a history and keeps two crews on one slug from hiding
each other's.

These tests cover the additional record: the emitter writes one line of that type,
the fold replaces the panel whole and keeps a bounded history, a slot carrying two
crews answers each with its own, and the two records are written independently
enough to fail separately. The durable record's own behaviour -- ownership, the
lock, replacing an unowned record -- belongs to ``test_agent_panel_store.py``.

Per-type payload validation is not here: the ratchets in
``test_crew_log_types.py`` already run accept, missing-required, undeclared-field and
wrong-type over every declared type, including this one.

The load-bearing test is :func:`test_the_newest_publish_is_the_one_served`, because
whole-document replacement is the property that separates this fold from the session
ledger's: it is written to fail if the fold ever applies an older entry over a newer
one, or merges the two.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import agent_panel
from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log.entry_types import (
    PANEL_CREW_KEY_LIMIT,
    PANEL_ENTRY_TYPE,
    PANEL_FOLD_NAME,
    PANEL_HISTORY_LIMIT,
    PANEL_OWNER_LIMIT,
    PANEL_TEMPLATE_LIMIT,
    PANEL_TITLE_LIMIT,
)

SLOT = "member-fleet-crew"
UNIT = "acp-fleet-crew"
CREW = "fleet-crew"
KEY = "a" * 40
OTHER_KEY = "b" * 40


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own data home, crew log on, and no warm fold carried between tests."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()
    yield
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()


def _unit(unit_id: str = UNIT, *, slot: str = SLOT) -> None:
    """Create one session crew log, then drop the handle so it holds no lease."""
    CrewLog.create(lg.KIND_SESSION, unit_id, owner="owner", agent=CREW, slot=slot)


def _publish(
    *,
    unit_id: str = UNIT,
    template: str = "default",
    data: dict[str, Any] | None = None,
    title: str = "",
    crew: str = CREW,
    crew_key: str = KEY,
) -> None:
    """One publish, appended and flushed so the fold can see it."""
    crew_log_emit.on_panel_published(
        unit_id,
        {
            "template": template,
            "data": {"cycle": 1} if data is None else data,
            "title": title,
            "crew": crew,
            "crew_key": crew_key,
        },
    )
    crew_log_emit.flush(timeout=5.0)


def _folded(slot: str = SLOT) -> dict[str, Any]:
    crew_log.forget_slot_folds()
    return crew_log.read_slot_projection(slot, PANEL_FOLD_NAME).value


def _lines(unit_id: str = UNIT) -> list[dict[str, Any]]:
    """Every entry in one unit's log, as the file holds them."""
    path = lg.crew_log_path(lg.KIND_SESSION, unit_id)
    out: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            out.append(json.loads(raw))
    return out


# --------------------------------------------------------------------- emitter


def test_a_publish_appends_one_line_of_its_own_type():
    """The write half: one publish, one entry, carrying the document whole."""
    _unit()
    _publish(title="fleet", data={"cycle": 47})

    panels = [e for e in _lines() if e.get("type") == PANEL_ENTRY_TYPE]
    assert len(panels) == 1, f"expected one panel entry, got {len(panels)}"
    entry = panels[0]
    assert entry["src"] == "gateway"
    assert entry["data"]["template"] == "default"
    assert entry["data"]["title"] == "fleet"
    assert entry["data"]["crew_key"] == KEY
    # The document WHOLE, not a delta: the payload is the crew's own object.
    assert entry["data"]["data"] == {"cycle": 47}


def test_the_emitter_is_a_no_op_with_the_crew_log_off(monkeypatch):
    """The gate belongs to the caller, so the emitter itself simply writes nothing.

    The route refuses the publish and tells the crew; this half only has to not
    create a log behind the operator's back when the feature is off.
    """
    _unit()
    monkeypatch.delenv("KIROCREW_CREW_LOG", raising=False)
    crew_log_emit.reset_caches()

    _publish()

    assert [e for e in _lines() if e.get("type") == PANEL_ENTRY_TYPE] == []


# ------------------------------------------------------------------------ fold


def test_a_single_publish_folds_to_the_record_the_drawer_consumes():
    """The shape is the store's own document, so no reader had to learn a new one."""
    _unit()
    _publish(title="fleet", data={"cycle": 47})

    value = _folded()
    assert value["template"] == "default"
    assert value["title"] == "fleet"
    assert value["crew"] == CREW
    assert value["crew_key"] == KEY
    assert value["data"] == {"cycle": 47}
    assert value["publishes"] == 1
    # Nothing was superseded, so there is no history row: a row describing the
    # empty start state would read as a publish that never happened.
    assert value["history"] == []
    assert value["published_at"], "the entry's own time must surface as a stamp"


def test_the_newest_publish_is_the_one_served():
    """WHOLE-DOCUMENT REPLACEMENT, which is this fold's defining property.

    Discriminating on three counts at once, so no single wrong implementation
    passes: applying the older entry last fails the value assertions, merging the
    two fails the exact-equality on ``data`` (the first cycle's key would survive),
    and dropping the earlier one fails the history row and the count.
    """
    _unit()
    _publish(title="first", data={"cycle": 1, "only_in_first": True})
    _publish(title="second", data={"cycle": 2})

    value = _folded()
    assert value["title"] == "second"
    assert value["publishes"] == 2
    # EXACT equality: a merge would leave ``only_in_first`` beside ``cycle``, which
    # is the mixed record whole-replacement exists to prevent.
    assert value["data"] == {"cycle": 2}
    # The superseded panel is kept as a history row describing what was REPLACED.
    assert [row["title"] for row in value["history"]] == ["first"]


def test_the_history_is_bounded_and_keeps_the_newest():
    """A crew publishing every cycle cannot grow the record without limit."""
    _unit()
    total = PANEL_HISTORY_LIMIT + 5
    for i in range(total):
        _publish(title=f"cycle-{i}", data={"cycle": i})

    value = _folded()
    assert value["title"] == f"cycle-{total - 1}"
    assert len(value["history"]) == PANEL_HISTORY_LIMIT
    # The OLDEST rows aged out, and the newest superseded panel is the last row.
    assert value["history"][-1]["title"] == f"cycle-{total - 2}"
    assert value["history"][0]["title"] == f"cycle-{total - PANEL_HISTORY_LIMIT - 1}"


def test_a_trimmed_history_says_how_much_it_dropped():
    """The bound speaks, so a reader can tell a trim from a complete history.

    Without the count, a history sliced at its cap reads exactly like a crew that
    never published those cycles.
    """
    _unit()
    _publish(title="first", data={"cycle": 0})
    value = _folded()
    assert value["history_omitted"] == 0, "nothing was dropped yet"

    for i in range(1, PANEL_HISTORY_LIMIT + 4):
        _publish(title=f"cycle-{i}", data={"cycle": i})

    value = _folded()
    assert len(value["history"]) == PANEL_HISTORY_LIMIT
    # Rows appended = publishes - 1 (the first publish supersedes nothing).
    appended = (PANEL_HISTORY_LIMIT + 4) - 1
    assert value["history_omitted"] == appended - PANEL_HISTORY_LIMIT
    assert value["history_omitted"] > 0, "the fixture must actually overflow the cap"


def test_an_evicted_owner_is_counted_not_silently_dropped():
    """A slot that evicted a crew must not read like one that crew never used."""
    _unit()
    keys = [chr(ord("c") + i) * 40 for i in range(PANEL_OWNER_LIMIT + 2)]
    for i, key in enumerate(keys[:PANEL_OWNER_LIMIT]):
        _publish(crew_key=key, title=f"crew-{i}", data={"cycle": i})
    assert _folded()["owners_omitted"] == 0, "nothing was evicted yet"

    for i, key in enumerate(keys[PANEL_OWNER_LIMIT:], start=PANEL_OWNER_LIMIT):
        _publish(crew_key=key, title=f"crew-{i}", data={"cycle": i})

    value = _folded()
    assert len(value["owners"]) == PANEL_OWNER_LIMIT
    assert value["owners_omitted"] == 2


def test_eviction_ranks_on_fold_order_not_on_a_stamp():
    """A planted unreadable ``time`` must not decide whose panel is evicted.

    ``_panel_iso`` answers the empty string for a ``time`` no ``datetime`` can hold,
    so ranking owners on ``published_at`` would sort every such record first and
    evict a live crew on the strength of one damaged line.

    The fixture makes the two orderings disagree: the first crew republishes LAST,
    which makes it the most recent owner by fold order, and that final entry is the
    damaged one, so its stamp is the empty string. A stamp key would evict it; a
    fold-order key evicts the crew that really did publish least recently.
    """
    _unit()
    keys = [chr(ord("c") + i) * 40 for i in range(PANEL_OWNER_LIMIT + 1)]

    # Fill the slot to its cap without evicting anything.
    for i, key in enumerate(keys[:PANEL_OWNER_LIMIT]):
        _publish(crew_key=key, title=f"crew-{i}", data={"cycle": i})

    # The first crew publishes again, so it is newest by fold order.
    _publish(crew_key=keys[0], title="republished", data={"cycle": 99})

    # Damage ONLY that last entry, so the empty stamp is the one it keeps.
    path = lg.crew_log_path(lg.KIND_SESSION, UNIT)
    lines = path.read_text(encoding="utf-8").splitlines()
    last = max(
        i
        for i, raw in enumerate(lines)
        if raw.strip() and json.loads(raw).get("type") == PANEL_ENTRY_TYPE
    )
    entry = json.loads(lines[last])
    entry["time"] = 10**19
    lines[last] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # One more owner than the cap allows, which forces exactly one eviction.
    _publish(crew_key=keys[PANEL_OWNER_LIMIT], title="newcomer", data={"cycle": 7})

    value = _folded()
    assert value["owners_omitted"] == 1
    assert value["owners"][keys[0]]["published_at"] == "", "fixture lost the damaged stamp"
    assert value["owners"][keys[0]]["data"] == {"cycle": 99}, "the republish was not folded"
    # The crew that published least recently is the one evicted, not the one whose
    # stamp is unreadable.
    assert keys[1] not in value["owners"], "fold order did not decide the eviction"


def test_the_recorded_order_beats_a_backward_header_clock(monkeypatch):
    """A later publish stays current even when its unit header carries an older clock.

    A slot owns one session id at a time, so a reset spreads its publishes across a
    unit per id, and the fold has to join them in the order they happened. Ordering by
    the header's ``createdAt`` assumes that clock only moves forward. It does not -- an
    NTP correction or a manual set steps it backward, this repo already treats that as
    a live hazard, and a header is stamped once at create and never rewritten, so the
    inversion is permanent.

    That is worse for this fold than for most: it takes the newest entry WHOLE, so a
    retired session's publish would become the current panel for good, with the history
    rows built against the wrong predecessor. The read route does not save it either --
    the history always comes from the fold, and so does the panel when the file is gone
    or its digest belongs to another crew.

    So the order comes from what the appends RECORDED, the way the work fold's does.
    """
    from kiro_crew.crew_log import store
    from kiro_crew.crew_log.store import session_units_for_slot

    older = "acp-fleet-clock-older"
    newer = "acp-fleet-clock-newer"

    # The retired unit is created first and publishes first, carrying the LATER clock.
    monkeypatch.setattr(store, "now_ms", lambda: 2_000)
    _unit(older)
    _publish(unit_id=older, title="retired", data={"cycle": 1})

    # Its replacement is created and publishes afterwards, but a backward step gives it
    # the EARLIER header stamp.
    monkeypatch.setattr(store, "now_ms", lambda: 1_000)
    _unit(newer)
    _publish(unit_id=newer, title="live", data={"cycle": 2})

    # The premise: header order really is inverted, so this fixture exercises the bug.
    assert session_units_for_slot(SLOT) == (
        newer,
        older,
    ), "fixture no longer inverts the header clock"

    record = _folded()
    assert record["data"] == {"cycle": 2}, "the retired session's publish became current"
    assert record["title"] == "live"
    assert [row["title"] for row in record["history"]] == [
        "retired"
    ], "the history row was built against the wrong predecessor"


def test_a_line_missing_its_template_or_data_is_skipped_not_half_applied():
    """A damaged or planted line must not splice its half over a good panel.

    Appended through the ordinary writer with the required field emptied, which is
    what a truncated or hand-edited log looks like to the fold.
    """
    _unit()
    _publish(title="good", data={"cycle": 47})
    # Empty template: accepted by the format (a string), rejected by the fold.
    _publish(template="", title="damaged", data={"cycle": 999})
    crew_log_emit.on_panel_published(
        UNIT, {"template": "default", "data": "not-an-object", "title": "also damaged"}
    )
    crew_log_emit.flush(timeout=5.0)

    value = _folded()
    assert value["title"] == "good"
    assert value["data"] == {"cycle": 47}
    assert value["publishes"] == 1, "a skipped line must not count as a publish"


def test_an_empty_template_is_how_the_fold_says_nothing_was_published():
    """The empty state must be tellable from a published but empty panel."""
    _unit()
    value = _folded()
    assert value["template"] == ""
    assert value["data"] == {}
    assert value["owners"] == {}


def test_the_folded_stamp_carries_a_zone_offset():
    """A zone-less stamp is read as the BROWSER's local time by the drawer.

    ``new Date('2026-09-04T22:30:18')`` is local-time in JS. On the loopback
    dashboard that is the same clock that wrote it, so the skew is invisible; from a
    remote browser every age is off by the offset between the two zones. Pinned
    because it is invisible in exactly the configuration a developer tests in.

    The stamp is derived by the fold from the entry's own envelope ``time``, so this
    is where the property lives: one clock, and no way for an entry to claim a
    publish time the log disagrees with.
    """
    from datetime import datetime

    _unit()
    _publish(data={"cycle": 47})

    stamp = str(_folded()["published_at"])
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None, f"{stamp!r} has no zone; a remote drawer will skew it"
    assert parsed.utcoffset() is not None


def test_an_unreadable_time_costs_the_stamp_not_the_panel():
    """A damaged ``time`` must not turn every read of that slot into a crash.

    The entry stays on disk and nothing rewrites it, so raising here would deny the
    crew its panel permanently. Losing one display value is the cheaper failure.
    """
    _unit()
    _publish(data={"cycle": 47})
    path = lg.crew_log_path(lg.KIND_SESSION, UNIT)
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, raw in enumerate(lines):
        entry = json.loads(raw) if raw.strip() else {}
        if entry.get("type") == PANEL_ENTRY_TYPE:
            entry["time"] = 10**19  # outside what a datetime can hold
            lines[i] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    value = _folded()
    assert value["published_at"] == "", "an unreadable time must answer the empty stamp"
    assert value["data"] == {"cycle": 47}, "the panel itself must still be served"


def test_a_damaged_stored_file_cannot_stop_the_history_being_recorded():
    """The additional record must not depend on the durable one being readable.

    The store owns what happens to a forged or unowned file -- it replaces such a
    record rather than refusing, so one planted file cannot hold a slug forever.
    This half is the other side of that independence: the append reads nothing from
    the file, so a file that is forged, truncated or unparseable still leaves the
    crew a history, and the two records fail separately rather than together.
    """
    _unit()
    path = agent_panel.panel_dir() / "fleet-crew.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    assert agent_panel.read("fleet-crew") is None, "the fixture must be unreadable"

    _publish(title="mine", data={"cycle": 47})

    value = _folded()
    assert value is not None, "a damaged file blocked the history append"
    assert value["title"] == "mine"
    assert value["data"] == {"cycle": 47}


def test_an_append_that_times_out_is_final_and_never_lands_later():
    """A publish the crew was told failed must not reappear on the next fold.

    The route answers 503 on a ``False``, so an entry that landed afterwards would
    leave the drawer stale AND give the crew's retry a second history row for one
    publish. The waiter abandons the job it gave up on, and the job re-checks that
    under the same gate before it appends.

    The writer's submit is intercepted so the job is HELD rather than run, which is
    the condition the timeout exists for; it is then run by hand afterwards, so what
    this asserts is the abandonment gate and not merely a job that never ran.
    """
    _unit()
    record = {
        "template": "default",
        "data": {"cycle": 47},
        "title": "never-lands",
        "crew": CREW,
        "crew_key": KEY,
    }
    held: list[Any] = []

    def _hold(job, what, session_id, nbytes=0, **kwargs):
        held.append(job)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(crew_log_emit, "_submit", _hold)
        assert (
            crew_log_emit.on_panel_published(UNIT, record, timeout=0.1) is False
        ), "an append that never started must not be reported as landed"

    assert held, "the append was never handed to the writer"
    # The writer runs the job it was holding. The waiter has already given up, so the
    # job must decline to append rather than landing a publish reported as failed.
    held[0]()
    crew_log_emit.flush(timeout=5.0)

    titles = [e["data"].get("title") for e in _lines() if e.get("type") == PANEL_ENTRY_TYPE]
    assert "never-lands" not in titles, "an abandoned append came back on a later drain"
    value = _folded()
    assert value["template"] == "", "the fold gained a publish the crew was told had failed"
    assert value["owners"] == {}


def test_a_successful_append_is_acknowledged_as_landed():
    """The other direction, so the pin above cannot pass by always answering False."""
    _unit()
    record = {
        "template": "default",
        "data": {"cycle": 47},
        "title": "lands",
        "crew": CREW,
        "crew_key": KEY,
    }
    assert crew_log_emit.on_panel_published(UNIT, record, timeout=5.0) is True
    assert _folded()["title"] == "lands"


# ---------------------------------------------------------------------- owners


def test_two_crews_on_one_slot_each_keep_their_own_record():
    """One slug can be held by two crews, so one record per slot is not enough.

    Memory provisioning suffixes a persisted ``member_id``, so a name-derived slug
    can belong to a crew of another name. With a single record the later publisher
    would be the only one the fold could answer with, and the other crew's drawer
    showed nothing while its own entries sat in this very log.
    """
    _unit()
    _publish(crew="first-crew", crew_key=KEY, title="mine", data={"cycle": 1})
    _publish(crew="second-crew", crew_key=OTHER_KEY, title="theirs", data={"cycle": 2})

    value = _folded()
    owners = value["owners"]
    assert set(owners) == {KEY, OTHER_KEY}
    assert owners[KEY]["data"] == {"cycle": 1}, "the earlier crew's record was displaced"
    assert owners[OTHER_KEY]["data"] == {"cycle": 2}
    # The top level answers a reader with no digest of its own: the newest publish.
    assert value["crew_key"] == OTHER_KEY
    # Each crew's history is its own, so neither sees the other's supersession.
    assert owners[KEY]["history"] == []
    assert owners[OTHER_KEY]["history"] == []


def test_the_owner_count_is_bounded_and_evicts_the_oldest():
    """The cap bounds retained state; an aged-out reader sees empty, never another's."""
    _unit()
    keys = [chr(ord("c") + i) * 40 for i in range(PANEL_OWNER_LIMIT + 1)]
    for i, key in enumerate(keys):
        _publish(crew_key=key, title=f"crew-{i}", data={"cycle": i})

    owners = _folded()["owners"]
    assert len(owners) == PANEL_OWNER_LIMIT
    assert keys[0] not in owners, "the oldest publish must be the one evicted"
    assert set(owners) == set(keys[1:])


# ------------------------------------------------------------------- the caps


def test_the_folds_ceilings_equal_the_stores_own():
    """A LOWER ceiling here would truncate an ordinary accepted panel on every read.

    The fold re-applies these because it reads bytes a reader does not control, so a
    planted line is exactly the input that ignores the writer's clamp. Equal by
    construction is the property: pinned here rather than restated as a number.
    """
    assert PANEL_TITLE_LIMIT == agent_panel._MAX_TITLE
    # Wide enough for any id the store's own grammar accepts, so a legitimate
    # template name is never shortened into one that resolves to nothing.
    assert PANEL_TEMPLATE_LIMIT >= 32
    # The digest's own exact width, so no real ownership key is ever shortened.
    assert PANEL_CREW_KEY_LIMIT == len(agent_panel.crew_key(CREW))


def test_an_oversized_title_is_clamped_rather_than_dropping_the_panel():
    """Clamping costs a reader a display value; refusing costs it the whole panel."""
    _unit()
    _publish(title="t" * (PANEL_TITLE_LIMIT + 50), data={"cycle": 47})

    value = _folded()
    assert len(value["title"]) == PANEL_TITLE_LIMIT
    assert value["data"] == {"cycle": 47}, "the panel itself must still be served"


# -------------------------------------------------------- the legacy fallback


def test_the_append_does_not_touch_the_stored_file():
    """The two records are written by different halves, and stay that way.

    The route writes the file through the store and appends this entry separately.
    The emitter's half must not touch the file: the file is the DURABLE record, so a
    history append that rewrote it could put a panel on disk that no publish
    authorised, and the crew log's own writer has no ownership check to make that
    safe.
    """
    _unit()
    path = agent_panel.panel_dir() / "fleet-crew.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "template": "default",
                "title": "written by the store",
                "crew": CREW,
                "crew_key": KEY,
                "data": {"stored": True},
            }
        ),
        encoding="utf-8",
    )
    before = path.read_bytes()

    _publish(title="from the log", data={"cycle": 47})

    assert _folded()["title"] == "from the log", "the append did not land"
    assert path.read_bytes() == before, "the append rewrote the durable record"
    assert agent_panel.read("fleet-crew")["title"] == "written by the store"
