"""``mochi/watchlist_file.py``: the readers, update operations and finders other suites skip.

Pure functions over parsed JSON with an explicit ``now_ms``, so every time-dependent case pins
the clock. Files live under ``tmp_path``, and the size limit is lowered instead of writing 10 MiB.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from kiro_crew.apps.builtins.mochi import queue_file as qf
from kiro_crew.apps.builtins.mochi import watchlist_file as wf

NOW = 1_800_000_000_000  # epoch milliseconds, on a whole second
STAMP = qf._iso(NOW)
EMPTY = {"version": 1, "items": []}


def _w(item_id: str, **fields: Any) -> dict[str, Any]:
    return {"id": item_id, "status": "watching", **fields}


def _ids(items: list[dict[str, Any]]) -> list[str]:
    return [item["id"] for item in items]


def _update(items: list[dict[str, Any]], *patches: dict[str, Any]) -> list[dict[str, Any]]:
    params = {"update": list(patches)}
    return wf.apply_watchlist_update({"items": items}, params, now_ms=NOW)["items"]


@pytest.mark.parametrize("reader", [wf.read_watchlist, wf.read_archive])
@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '{"items": {}}'])
def test_a_bad_file_is_backed_up_and_read_as_empty(tmp_path: Path, reader, text) -> None:
    path = tmp_path / "w.json"
    path.write_text(text, encoding="utf-8")
    assert reader(str(path), now_ms=NOW) == EMPTY
    assert not path.exists()
    assert (tmp_path / f"w.json.bak.{NOW}").read_text(encoding="utf-8") == text


def test_read_watchlist_resets_an_oversized_file(tmp_path: Path, caplog, monkeypatch) -> None:
    path = tmp_path / "w.json"
    path.write_text('{"version": 1, "items": []}', encoding="utf-8")
    monkeypatch.setattr(wf, "_MAX_WATCHLIST_BYTES", 8)
    assert wf.read_watchlist(str(path), now_ms=NOW) == EMPTY
    assert "File too large" in caplog.text
    assert (tmp_path / f"w.json.bak.{NOW}").exists()


def test_unreadable_files_read_as_empty_and_the_active_list_warns(tmp_path: Path, caplog) -> None:
    folder = tmp_path / "w.json"
    folder.mkdir()  # stat works on a directory, reading it does not
    assert wf.read_watchlist(str(folder), now_ms=NOW) == EMPTY
    assert wf.read_archive(str(folder), now_ms=NOW) == EMPTY
    for reader in (wf.read_watchlist, wf.read_archive):
        assert reader(str(tmp_path / "absent.json"), now_ms=NOW) == EMPTY
    with mock.patch.object(wf.os, "stat", side_effect=PermissionError("denied")):
        denied = wf.read_watchlist(str(folder), now_ms=NOW)
    assert denied == EMPTY
    assert caplog.text.count("Read error") == 2


def test_a_file_written_under_the_sidecar_lock_reads_back_through_both_readers(
    tmp_path: Path,
) -> None:
    path, document = tmp_path / "w.json", {"version": 1, "items": [_w("a")], "extra": 1}
    with wf.watchlist_mutation(path):
        assert (tmp_path / "w.json.lock").exists()
        assert not path.exists()  # the data file itself is never the locked inode
        wf.write_atomic(str(path), document)
    assert [r(str(path), now_ms=NOW) for r in (wf.read_watchlist, wf.read_archive)] == [
        document
    ] * 2


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"add": "x"}, "'add' must be a list"),
        ({"cancel": "abc"}, "'cancel' must be a list"),
        ({"remove": ["a", 1]}, "every 'remove' entry must be an id string"),
        ({"update": ["x"]}, "every 'update' entry must be an object"),
        ({"add": [{"checkIntervalMins": "abc"}]}, "'add' checkIntervalMins must be a number"),
        ({"update": [{"checkIntervalMins": True}]}, "'update' checkIntervalMins must be a number"),
    ],
)
def test_malformed_operations_are_rejected(params, message) -> None:
    with pytest.raises(ValueError, match=message):
        wf.apply_watchlist_update({"items": [_w("a")]}, params, now_ms=NOW)


def test_adds_stop_at_the_active_limit_with_a_warning() -> None:
    cap = wf.MAX_ACTIVE_ITEMS
    items = [_w(f"a{i}") for i in range(cap - 1)] + [_w("old", status="done")]  # not counted
    out = wf.apply_watchlist_update({"items": items}, {"add": [{"label": "x"}] * 3}, now_ms=NOW)
    assert sum(i["status"] == "watching" for i in out["items"]) == cap
    assert out["warning"] == f"Only added 1 of 3 items (limit: {cap} active)."
    full = wf.apply_watchlist_update(out, {"add": [{"label": "y"}]}, now_ms=NOW)
    assert full["items"] == out["items"]
    assert full["warning"] == (
        f"Active items limit reached ({cap}). Remove or complete some items first."
    )


def test_cancel_closes_open_items_and_remove_deletes_by_id() -> None:
    items = [_w("a"), _w("b"), _w("c"), _w("d", status="done", completedAt="long ago")]
    params = {"cancel": ["a", "d", "ghost"], "remove": ["c"]}
    out = wf.apply_watchlist_update({"items": items}, params, now_ms=NOW)["items"]
    assert _ids(out) == ["a", "b", "d"]
    assert out[0] == _w("a", status="cancelled", completedAt=STAMP, completionReason="cancelled")
    assert (out[1], out[2]) == (items[1], items[3])  # open stays open, finished stays as it was


def test_update_assigns_present_fields_clamps_the_interval_and_skips_unknown_ids() -> None:
    patch = {"id": "a", "lastResult": "ok", "checkCount": 3, "failCount": 1, "priority": "high"}
    patch |= {"notes": None, "checkIntervalMins": 10.5}
    (item,) = _update([_w("a", notes="old")], patch, {"id": "ghost", "notes": "x"})
    assert (item["lastResult"], item["checkCount"], item["failCount"]) == ("ok", 3, 1)
    assert (item["priority"], item["notes"]) == ("high", None)  # a present null is assigned
    assert item["checkIntervalMins"] == 11  # half rounds up, like Math.round
    (low,) = _update([item], {"id": "a", "checkIntervalMins": 1})
    assert low["checkIntervalMins"] == wf.MIN_CHECK_INTERVAL_MINS


@pytest.mark.parametrize(
    ("before", "new_status", "expected"),
    [
        ({}, "done", ("done", STAMP, "completed")),
        ({}, "failed", ("failed", STAMP, "failed")),
        ({"completedAt": ""}, "expired", ("expired", STAMP, "expired")),  # empty counts as unset
        (
            {"status": "done", "completedAt": "T0", "completionReason": "why"},
            "cancelled",
            ("cancelled", "T0", "why"),
        ),
        ({}, "watching", ("watching", None, None)),  # no change, no metadata
    ],
)
def test_a_status_change_sets_completion_metadata_only_once(before, new_status, expected) -> None:
    (item,) = _update([_w("a", **before)], {"id": "a", "status": new_status})
    assert (item["status"], item.get("completedAt"), item.get("completionReason")) == expected


def test_rewatching_a_finished_item_starts_a_new_lifecycle_under_a_new_id() -> None:
    done = _w("old", status="done", completedAt="T0", completionReason="completed")
    done |= {"checkCount": 9, "failCount": 2, "checkIntervalMins": 20, "createdAt": "T-1"}
    (item,) = _update([done], {"id": "old", "status": "watching"})
    assert re.fullmatch(rf"w-{NOW}-[0-9a-z]{{4}}", item["id"])
    assert (item["previousId"], item["status"], item["createdAt"]) == ("old", "watching", STAMP)
    assert (item["checkCount"], item["failCount"]) == (0, 0)
    assert "completedAt" not in item and "completionReason" not in item
    assert item["nextCheckAfter"] == qf._iso(NOW + 20 * 60_000)


def test_a_history_entry_is_appended_and_reschedules_a_watching_item() -> None:
    item = _w("a", checkIntervalMins=15, lastResult="before", history=[])
    patch = {"id": "a", "historyEntry": {"result": "now 5", "changed": True}, "notified": True}
    (got,) = _update([item], patch)
    assert got["history"] == [{"checkedAt": STAMP, "result": "now 5", "changed": True}]
    assert (got["lastChecked"], got["lastResult"]) == (STAMP, "now 5")
    assert (got["lastChangedAt"], got["lastNotifiedAt"]) == (STAMP, STAMP)
    assert got["nextCheckAfter"] == qf._iso(NOW + 15 * 60_000)


def test_a_history_entry_without_a_result_erases_it_and_an_explicit_next_check_wins() -> None:
    item = _w("a", checkIntervalMins=15, lastResult="before")
    patch = {"id": "a", "historyEntry": {"changed": False}, "nextCheckAfter": None}
    (got,) = _update([item], patch)
    assert got["history"] == [{"checkedAt": STAMP, "changed": False}]
    assert "lastResult" not in got and "lastChangedAt" not in got
    assert got["nextCheckAfter"] is None  # the explicit null beats the recomputed time
    finished = _w("b", status="done", checkIntervalMins=15)
    (done,) = _update([finished], {"id": "b", "historyEntry": {"result": "x"}})
    assert "nextCheckAfter" not in done  # a finished item is not rescheduled


def test_every_tenth_history_entry_compresses_the_history() -> None:
    stale = {"checkedAt": qf._iso(NOW - wf.HISTORY_CHANGED_RETENTION_MS - 1_000)}
    recent = {"checkedAt": qf._iso(NOW - 1_000)}
    patch = {"id": "a", "historyEntry": {"result": "r"}}
    (got,) = _update([_w("a", history=[stale] * 2 + [recent] * 7)], patch)
    assert got["history"] == [recent] * 7 + [{"checkedAt": STAMP, "result": "r"}]  # 2 stale dropped


def test_compress_history_keeps_recent_entries_and_older_ones_only_when_changed() -> None:
    day, week = wf.HISTORY_FULL_RETENTION_MS, wf.HISTORY_CHANGED_RETENTION_MS

    def entry(age_ms: int, **extra: Any) -> dict[str, Any]:
        return {"checkedAt": qf._iso(NOW - age_ms), **extra}

    keep = [entry(week, changed=True), entry(day + 1_000, changed=True), entry(day), entry(0)]
    drop = [entry(week + 1_000, changed=True), entry(day + 1_000), {"checkedAt": "soon"}, {}]
    assert wf.compress_history(drop[:2] + keep[:2] + drop[2:] + keep[2:], now_ms=NOW) == keep
    capped = [{"checkedAt": qf._iso(NOW - 1_000 * (505 - n)), "n": n} for n in range(505)]
    assert [e["n"] for e in wf.compress_history(capped, now_ms=NOW)] == list(range(5, 505))


def test_only_terminal_items_completed_over_a_day_ago_move_to_the_archive() -> None:
    over = qf._iso(NOW - wf.ARCHIVE_AFTER_MS - 1_000)
    items = [
        _w("done", status="done", completedAt=over),
        _w("failed", status="failed", completedAt=over),
        _w("edge", status="done", completedAt=qf._iso(NOW - wf.ARCHIVE_AFTER_MS)),
        _w("open", completedAt=over),
        _w("unstamped", status="done"),
        _w("garbled", status="expired", completedAt="soon"),
    ]
    out = wf.partition_for_archive({"items": items}, now_ms=NOW)
    assert _ids(out["toArchive"]) == ["done", "failed"]
    assert _ids(out["remaining"]) == ["edge", "open", "unstamped", "garbled"]


def test_merge_into_archive_dedups_prunes_old_entries_and_caps_by_append_order() -> None:
    recent = qf._iso(NOW - 1_000)
    archive = {"items": [{"id": "a", "archivedAt": recent}, {"id": "none"}]}
    archive["items"] += [{"id": "old", "archivedAt": qf._iso(NOW - wf.ARCHIVE_MAX_AGE_MS)}]
    archive["items"] += [{"id": "garbled", "archivedAt": "soon"}]
    merged = wf.merge_into_archive(archive, [{"id": "a", "label": "dup"}, {"id": "b"}], now_ms=NOW)
    assert merged["version"] == 1
    assert [(i["id"], i["archivedAt"]) for i in merged["items"]] == [("a", recent), ("b", STAMP)]
    with mock.patch.object(wf, "ARCHIVE_MAX_ITEMS", 2):
        capped = wf.merge_into_archive(merged, [{"id": "c"}, {"id": "d"}], now_ms=NOW)
    assert _ids(capped["items"]) == ["c", "d"]


def test_search_archive_filters_by_label_and_since_and_lists_newest_archived_first() -> None:
    def item(item_id: str, label: str, archived: int, completed: int | None = None) -> dict:
        got = {"id": item_id, "label": label, "archivedAt": qf._iso(NOW + archived)}
        return got | ({"completedAt": qf._iso(NOW + completed)} if completed is not None else {})

    archive = {
        "items": [
            item("a", "Deploy Prod", 1_000, 100),
            item("b", "deploy staging", 3_000, 300),
            item("c", "Backup", 2_000),
            item("d", "deploy old", 4_000, 50),
        ]
    }
    assert _ids(wf.search_archive(archive, "")) == ["d", "b", "c", "a"]
    assert _ids(wf.search_archive(archive, "DEPLOY")) == ["d", "b", "a"]
    assert _ids(wf.search_archive(archive, "deploy", limit=1)) == ["d"]
    since = qf._iso(NOW + 200)  # items finished earlier are out; one with no completedAt stays
    assert _ids(wf.search_archive(archive, "", since=since)) == ["b", "c"]
    for off in ("garbled", qf._iso(0)):  # an unparseable or epoch-zero since disables the filter
        assert len(wf.search_archive(archive, "", since=off)) == 4
    bare = {"items": [{"id": "x", "label": "q"}, {"id": "y", "label": "q"}]}
    assert _ids(wf.search_archive(bare, "q")) == ["x", "y"]  # no archivedAt: insertion order


def test_find_expired_items_flags_watching_non_reminders_past_their_duration() -> None:
    made = qf._iso(NOW - 2 * 3_600_000)
    items = [
        _w("expired", createdAt=made, maxWatchDurationHours=1),
        _w("meeting", kind="meeting", createdAt=made, maxWatchDurationHours=1),
        _w("exact", createdAt=made, maxWatchDurationHours=2),
        _w("reminder", kind="reminder", createdAt=made, maxWatchDurationHours=1),
        _w("done", status="done", createdAt=made, maxWatchDurationHours=1),
        _w("garbled", createdAt="soon", maxWatchDurationHours=1),
        _w("no-limit", createdAt=made),
        _w("text-limit", createdAt=made, maxWatchDurationHours="1"),
    ]
    assert _ids(wf.find_expired_items({"items": items}, now_ms=NOW)) == ["expired", "meeting"]


def test_find_due_watch_items_skips_reminders_and_unusable_times() -> None:
    past, future = qf._iso(NOW - 1_000), qf._iso(NOW + 1_000)
    items = [
        _w("due", nextCheckAfter=past),
        _w("now", nextCheckAfter=STAMP),
        _w("later", nextCheckAfter=future),
        _w("reminder", kind="reminder", nextCheckAfter=past),
        _w("meeting", kind="meeting", nextCheckAfter=past),
        _w("done", status="done", nextCheckAfter=past),
        _w("empty", nextCheckAfter=""),
        _w("garbled", nextCheckAfter="soon"),
        _w("unscheduled"),
    ]
    assert _ids(wf.find_due_watch_items({"items": items}, now_ms=NOW)) == ["due", "now"]


def test_find_due_reminders_returns_watching_reminders_and_meetings_at_their_trigger() -> None:
    past, future = qf._iso(NOW - 1_000), qf._iso(NOW + 1_000)
    items = [
        _w("reminder", kind="reminder", triggerAt=past),
        _w("meeting", kind="meeting", triggerAt=STAMP),
        _w("later", kind="reminder", triggerAt=future),
        _w("url", kind="url", triggerAt=past),
        _w("done", kind="reminder", status="done", triggerAt=past),
        _w("empty", kind="reminder", triggerAt=""),
        _w("garbled", kind="reminder", triggerAt="soon"),
    ]
    assert _ids(wf.find_due_reminders({"items": items}, now_ms=NOW)) == ["reminder", "meeting"]


def test_find_overdue_items_needs_more_than_two_missed_cycles() -> None:
    def late(minutes: int) -> str:
        return qf._iso(NOW - minutes * 60_000)

    items = [
        _w("overdue", nextCheckAfter=late(21), checkIntervalMins=10),
        _w("meeting", kind="meeting", nextCheckAfter=late(99), checkIntervalMins=10),
        _w("two-cycles", nextCheckAfter=late(20), checkIntervalMins=10),
        _w("reminder", kind="reminder", nextCheckAfter=late(99), checkIntervalMins=10),
        _w("done", status="done", nextCheckAfter=late(99), checkIntervalMins=10),
        _w("empty", nextCheckAfter="", checkIntervalMins=10),
        _w("garbled", nextCheckAfter="soon", checkIntervalMins=10),
        _w("no-interval", nextCheckAfter=late(99)),
    ]
    assert _ids(wf.find_overdue_items({"items": items}, now_ms=NOW)) == ["overdue", "meeting"]
