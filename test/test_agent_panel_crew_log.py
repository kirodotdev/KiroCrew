"""The member panel as a crew-log entry type and a slot fold.

A publish writes the durable `crew-panels/<slug>.json` (covered in
``test_agent_panel_store.py``) and a ``panel/published`` line in the member's DM session
log, folded by ``panel``, which gives the panel a history and one record per crew.
These tests cover that line and its fold; per-type payload validation lives in
``test_crew_log_types.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

import pytest
from crew_log_drain import assert_drained

from kiro_crew import agent_panel
from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store
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
    assert_drained()


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


def _rewrite_log(unit_id: str, edit: "Callable[[list[str]], list[str]]") -> None:
    """Read a unit's log, map its lines through *edit*, and write them back.

    Held under the unit's append lock: the writer thread still appends and rolls back
    failed appends after a publish returns, and an unlocked rewrite can interleave with
    those. Bracketed as a cut (``_open_cut``/``_close_cut``) because a warm fold trusts
    what it consumed until the unit's cut counter changes.
    """
    lock_path = store._lock_path(lg.KIND_SESSION, unit_id)
    path = lg.crew_log_path(lg.KIND_SESSION, unit_id)
    with store._open_lock(lock_path):
        lines = path.read_text(encoding="utf-8").splitlines()
        store._open_cut(path.parent)
        path.write_text("\n".join(edit(lines)) + "\n", encoding="utf-8")
        store._close_cut(path.parent)


# emitter


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
    monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
    crew_log_emit.reset_caches()

    _publish()

    assert [e for e in _lines() if e.get("type") == PANEL_ENTRY_TYPE] == []


# fold


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
    """The newest publish replaces the panel whole, the fold's defining property.

    Older-last fails the values, a merge fails exact ``data`` equality, and dropping
    the earlier entry fails the history row and the count.
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


def test_eviction_ranks_on_fold_order_not_on_a_stamp(monkeypatch):
    """A planted unreadable ``time`` must not decide whose panel is evicted.

    ``_panel_iso`` gives "" for such a time. The first crew republishes last with a
    damaged stamp: a stamp key would evict it, fold order evicts the true oldest.
    """
    _unit()
    keys = [chr(ord("c") + i) * 40 for i in range(PANEL_OWNER_LIMIT + 1)]

    # Fill the slot to its cap without evicting anything.
    for i, key in enumerate(keys[:PANEL_OWNER_LIMIT]):
        _publish(crew_key=key, title=f"crew-{i}", data={"cycle": i})

    # The first crew publishes again, so it is newest by fold order.
    with monkeypatch.context() as patch:
        patch.setattr(store, "now_ms", lambda: 10**19)
        _publish(crew_key=keys[0], title="republished", data={"cycle": 99})

    # One more owner than the cap allows, which forces exactly one eviction.
    _publish(crew_key=keys[PANEL_OWNER_LIMIT], title="newcomer", data={"cycle": 7})

    value = _folded()
    assert value["owners_omitted"] == 1
    assert value["owners"][keys[0]]["published_at"] == "", "fixture lost the damaged stamp"
    assert value["owners"][keys[0]]["data"] == {"cycle": 99}, "the republish was not folded"
    # The crew that published least recently is the one evicted, not the one whose
    # stamp is unreadable.
    assert keys[1] not in value["owners"], "fold order did not decide the eviction"


def test_a_file_rewrite_waits_for_the_unit_append_lock():
    """:func:`_rewrite_log` waits for the unit's append lock.

    Without it a rewrite can interleave with the writer thread's appends or rollbacks,
    and the fold reads a log short by whole entries.
    """
    import threading

    _unit()
    _publish(data={"cycle": 1})

    started = threading.Event()
    finished = threading.Event()

    def _rewrite() -> None:
        started.set()
        _rewrite_log(UNIT, lambda lines: lines)
        finished.set()

    lock_path = store._lock_path(lg.KIND_SESSION, UNIT)
    with store._open_lock(lock_path):
        worker = threading.Thread(target=_rewrite)
        worker.start()
        assert started.wait(timeout=5.0), "the rewrite thread never started"
        # The lock is held here, so the rewrite must be parked waiting for it.
        assert not finished.wait(timeout=0.5), "the rewrite did not wait for the lock"
    worker.join(timeout=5.0)
    assert finished.is_set(), "the rewrite never completed once the lock was free"


def test_the_recorded_order_beats_a_backward_header_clock(monkeypatch):
    """A later publish stays current even when its unit header carries an older clock.

    A reset spreads a slot's publishes over several units. Header ``createdAt`` can step
    backward (NTP, manual set) and is never rewritten, so the fold orders by what the
    appends recorded; otherwise a retired session's panel would win for good.
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
    assert_drained()

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

    JS ``new Date('2026-09-04T22:30:18')`` is local time, so a remote browser skews every
    age while loopback hides it. The fold derives the stamp from the envelope ``time``.
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

    def _damage_every_panel(lines: list[str]) -> list[str]:
        for i, raw in enumerate(lines):
            entry = json.loads(raw) if raw.strip() else {}
            if entry.get("type") == PANEL_ENTRY_TYPE:
                entry["time"] = 10**19  # outside what a datetime can hold
                lines[i] = json.dumps(entry)
        return lines

    _rewrite_log(UNIT, _damage_every_panel)

    value = _folded()
    assert value["published_at"] == "", "an unreadable time must answer the empty stamp"
    assert value["data"] == {"cycle": 47}, "the panel itself must still be served"


def test_a_damaged_stored_file_cannot_stop_the_history_being_recorded():
    """The log append reads nothing from the stored file, so a damaged file still
    leaves the crew a history.
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

    The route answers 503, so a late landing would double the retry's history row. The
    held job is run by hand afterwards, so this pins the abandonment gate itself.
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
    assert_drained()

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


# owners


def test_two_crews_on_one_slot_each_keep_their_own_record():
    """One slug can be held by two crews, so one record per slot is not enough.

    Memory provisioning suffixes a persisted ``member_id``, so a name-derived slug can
    belong to a crew of another name.
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


# what the sanctioned reader sees


#: The reference page's own reader hint, as the paragraph it is written in. The
#: pins below derive what a reader is promised from THIS text rather than from a
#: list kept here, so a hint that grows a field the record does not carry reds.
#: Read inside the panel type's own subsection, so a type documented after it does
#: not move the anchor.
_READER_HINT_HEAD = "**Reader hint**"
_PANEL_SUBSECTION = "### `panel/published`"


def _reader_hint() -> str:
    """The panel type's reader-hint paragraph from the crew-log reference page."""
    page = (
        Path(__file__).resolve().parents[1] / "docs" / "reference" / "crew-log" / "session-types.md"
    )
    text = page.read_text(encoding="utf-8")
    # The hint inside the panel type's subsection, sliced to the blank line that
    # ends the paragraph.
    start = text.index(_PANEL_SUBSECTION)
    head = text.index(_READER_HINT_HEAD, start)
    para = text[head:].split("\n\n", 1)[0]
    assert "crew_key" in para, "the panel type's reader hint moved; re-anchor this"
    return para


def test_the_slot_wide_count_is_on_the_top_level_alone():
    """``owners_omitted`` belongs to the fold, not to any owner's record.

    Beside ``history_omitted`` it would read as "my crew was evicted N times". The two
    shapes are compared rather than listed, so a new one-sided field fails this.
    """
    _unit()
    _publish(title="fleet", data={"cycle": 47})

    value = _folded()
    mine = value["owners"][KEY]
    assert "owners_omitted" in value, "the fold must still report its own truncation"
    assert "owners_omitted" not in mine, "the slot's count is not the owner's to carry"
    top_only = set(value) - set(mine)
    owner_only = set(mine) - set(value)
    assert top_only == {"owners", "owners_omitted"}, f"top level alone: {sorted(top_only)}"
    assert not owner_only, f"on the per-owner record alone: {sorted(owner_only)}"


def test_every_field_the_reader_hint_names_is_reachable_along_the_hints_own_path():
    """Every backticked field in the hint is on the fold's top level or the owner record.

    Not "on the owner record" alone: an evicted owner has no record, so its count can
    only live on the top level.
    """
    _unit()
    _publish(title="fleet", data={"cycle": 47})
    value = _folded()
    reachable = set(value) | set(value["owners"][KEY])

    named = set(re.findall(r"`([a-z_]+)`", _reader_hint()))
    assert named, "the hint names no field in backticks; re-anchor this pin"
    missing = sorted(name for name in named if name not in reachable)
    assert not missing, f"the hint names {len(named)} fields; unreachable: {missing}"


def test_an_evicted_crew_has_no_record_and_reads_the_count_from_the_top_level():
    """The evicted reader's path: absence under its own key, count at the top level.

    Eviction is ``del owners[oldest]``; a placeholder record would break the reader.
    """
    _unit()
    keys = [chr(ord("c") + i) * 40 for i in range(PANEL_OWNER_LIMIT + 1)]
    for i, key in enumerate(keys):
        _publish(crew_key=key, title=f"crew-{i}", data={"cycle": i})

    value = _folded()
    evicted = keys[0]
    assert len(value["owners"]) == PANEL_OWNER_LIMIT
    # Absent, not empty: there is no record here to carry a count of its own.
    assert evicted not in value["owners"]
    assert value["owners"].get(evicted) is None
    # So the top level is the only thing that can tell this reader it was evicted.
    assert value["owners_omitted"] == 1


def test_a_non_zero_count_does_not_attribute_the_loss_to_the_asking_crew():
    """A crew that never published reads the same as an evicted one.

    ``_panel_step`` counts ANY owner's eviction without reference to the asking key.
    """
    _unit()
    publishers = [chr(ord("c") + i) * 40 for i in range(PANEL_OWNER_LIMIT + 1)]
    for i, key in enumerate(publishers):
        _publish(crew_key=key, title=f"crew-{i}", data={"cycle": i})

    value = _folded()
    evicted = publishers[0]
    never_published = "z" * 40

    # Both read absence, and both read the same count. Indistinguishable by design.
    assert evicted not in value["owners"]
    assert never_published not in value["owners"]
    assert value["owners_omitted"] == 1


def test_no_record_under_owners_is_ever_empty_so_absence_is_the_only_signal():
    """An "empty record under ``owners``" is not a state the fold can produce.

    Every owner record has a non-empty ``template``; the only empty one is the top-level
    fallback with no publish, where the count is zero. So absence is the evicted signal.
    """
    _unit()
    keys = [chr(ord("c") + i) * 40 for i in range(PANEL_OWNER_LIMIT + 1)]
    for i, key in enumerate(keys):
        _publish(crew_key=key, title=f"crew-{i}", data={"cycle": i})

    templates = [record["template"] for record in _folded()["owners"].values()]
    assert templates and all(templates), templates

    # The one empty-template record the fold emits, and its count is zero there.
    fresh = crew_log.read_slot_projection("member-nobody-published", PANEL_FOLD_NAME).value
    assert fresh["template"] == ""
    assert fresh["owners_omitted"] == 0


def test_a_zero_count_speaks_only_about_this_fold_not_about_publishing():
    """``0`` says this FOLD recorded no eviction. It says nothing about publishing.

    ``api_agent_panel_publish`` skips the append (crew log off, no unit, line too big)
    while the file still serves the panel; the fixture appends nothing, like that case.
    """
    _unit()

    value = _folded()
    assert value["owners_omitted"] == 0, "no eviction was recorded by this fold"
    assert value["owners"] == {}, "and the fold holds no record for anyone"
    # What the fold cannot distinguish: a crew that never published from one whose
    # publish landed in the file while its entry was skipped.
    assert value["template"] == "", "the fold says only that it has nothing recorded"


def test_a_slot_that_evicted_nobody_still_reports_the_count_as_zero():
    """Zero is an answer, not an absent field, on a slot that published and evicted

    nobody -- the ordinary case, distinct from the empty fold above. A reader that
    cannot tell "no eviction" from "this fold does not report evictions" is back to
    guessing, so the field is present and zero here.
    """
    _unit()
    _publish(title="fleet", data={"cycle": 47})

    value = _folded()
    assert value["owners_omitted"] == 0
    assert value["owners"][KEY]["template"], "and the publish itself is recorded"


def test_no_panel_prose_claims_the_count_identifies_the_evicted_crew():
    """No fold prose may claim the eviction count identifies the evicted crew.

    Matched on one exact phrase, since the prose wraps and negates across lines. The
    ``history_omitted`` claim ("those cycles") is true: that field is per owner.
    """
    folds = sorted(Path(crew_log.__file__).parent.glob("projection*.py"))
    source = "".join(path.read_text(encoding="utf-8") for path in folds)

    assert "exactly like a crew that never published on this slot" not in source
    # The correction is present, so this pin cannot pass by the prose being deleted.
    assert "exactly what a genuinely evicted crew reads" in source
    # And the true sibling claim is untouched.
    assert "never published those cycles" in source


def test_the_increment_site_says_what_the_count_omits():
    """The eviction site's own comment must not promise per-crew distinguishability.

    It is the site a reader lands on when asking what the count is for, so an
    overclaim there outranks a correct docstring elsewhere.
    """
    import inspect

    body = inspect.getsource(crew_log._panel_step)
    assert "owners_omitted" in body, "re-anchor: the increment moved"
    assert "exactly like a crew that never published" not in body


# the caps


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


# the legacy fallback


def test_the_append_does_not_touch_the_stored_file():
    """The log append never touches the durable file.

    The crew log's writer has no ownership check, so a rewrite could put an
    unauthorised panel on disk.
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
