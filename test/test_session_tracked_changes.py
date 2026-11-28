"""A session's stored tracked-changes list (``dashboard/tracked_changes.py``).

Storage only: the list is written through ``track_change``, persisted on the slot's
metadata line, restored (eagerly and on a sidebar row whose window has not loaded),
carried forward while unread, reset by a rebind, copied by a fork with its
tombstones, trimmed by a ``/clear`` that ran, and read back through
``GET /api/chat/slots/{slot}/source-links?tracked=1``. Nothing renders it.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from chat_test_helpers import _make_state

from kiro_crew.dashboard import tracked_changes as tc
from kiro_crew.dashboard.chat_handlers import api_chat_slot_source_links
from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    _save_slot_to_history,
)
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.source_providers.contract import source_ref_identity_key
from kiro_crew.dashboard.state import _ChatSlot

PR = "https://github.com/acme/widgets/pull/7"
ISSUE = "https://github.com/acme/widgets/issues/8"
MR = "https://gitlab.acme.internal/team/api/-/merge_requests/9"


def _key(number: int, *, kind: str = "change", repo: str = "widgets") -> str:
    return source_ref_identity_key(("github", "github.com", "acme", repo, number, "", kind, ""))


def _entry(number: int, *, added_at: str = "2026-01-01T00:00:00+00:00", **over) -> dict:
    entry = {
        "identity": _key(number),
        "url": f"https://github.com/acme/widgets/pull/{number}",
        "sources": [{"kind": "created", "ref": "tc-1"}],
        "added_at": added_at,
    }
    entry.update(over)
    return entry


def _stamp(i: int) -> str:
    return f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00"


def _fresh(slot: _ChatSlot) -> _ChatSlot:
    slot._dirty = False
    return slot


@pytest.fixture
def no_gitlab(monkeypatch):
    from kiro_crew.dashboard.handlers import source_providers as sp

    monkeypatch.setattr(sp, "_gitlab_hosts_snapshot", frozenset())
    monkeypatch.setattr(sp, "_gitlab_hosts_loaded_at", 0.0)
    monkeypatch.setattr(sp, "_gitlab_hosts_generation", 0)
    yield sp
    sp._publish_provider_hosts(frozenset(), frozenset())


class TestWrite:
    def test_a_change_is_added_once_and_a_repeat_marks_nothing_dirty(self):
        slot = _fresh(_ChatSlot("s1"))
        assert tc.record_tracked_change(slot, PR, "created", "tc-1") is True
        assert slot._dirty
        gen = slot._dirty_gen
        _fresh(slot)
        # Same identity, same URL, a kind already recorded: no membership change,
        # so the periodic flush has nothing to write.
        assert tc.record_tracked_change(slot, PR, "created", "tc-2") is False
        assert not slot._dirty and slot._dirty_gen == gen
        assert [e["url"] for e in slot._tracked_changes.values()] == [PR]

    def test_two_spellings_of_one_pull_request_are_one_entry_with_the_newest_url(self):
        slot = _fresh(_ChatSlot("s1"))
        assert tc.record_tracked_change(slot, "https://github.com/Acme/Widgets/pull/7", "acted")
        assert tc.record_tracked_change(slot, PR, "acted") is True  # the URL changed
        assert list(slot._tracked_changes) == [_key(7)]
        assert slot._tracked_changes[_key(7)]["url"] == PR
        _fresh(slot)
        assert tc.record_tracked_change(slot, PR + "/files", "acted") is False
        assert not slot._dirty

    def test_a_new_source_kind_is_recorded_and_kinds_stay_bounded(self):
        slot = _ChatSlot("s1")
        for kind in ("created", "acted", "work_report", "monitor", "agent"):
            assert tc.record_tracked_change(slot, PR, kind)
        sources = slot._tracked_changes[_key(7)]["sources"]
        assert [s["kind"] for s in sources] == ["acted", "work_report", "monitor", "agent"]

    @pytest.mark.parametrize(
        "url, kind, ref",
        [
            (ISSUE, "created", ""),  # issues stay out of the list
            ("http://github.com/acme/widgets/pull/7", "created", ""),
            ("https://evil.example/acme/widgets/pull/7", "created", ""),
            (PR, "made_up", ""),
            (PR, "created", "not a ref\n"),
            (PR + "x" * 2100, "created", ""),
        ],
    )
    def test_a_write_outside_the_contract_is_refused(self, url, kind, ref):
        slot = _fresh(_ChatSlot("s1"))
        assert tc.record_tracked_change(slot, url, kind, ref) is False
        assert slot._tracked_changes == {} and not slot._dirty

    def test_a_change_whose_identity_a_restore_would_refuse_is_refused(self):
        # A deep gitlab.com group path parses, but its identity key is past the bound
        # the restore checks; storing it would lose it at the next restart.
        url = "https://gitlab.com/" + "/".join(["g" * 80] * 3) + "/repo/-/merge_requests/7"
        slot = _fresh(_ChatSlot("s1"))
        assert tc.record_tracked_change(slot, url, "created") is False
        assert slot._tracked_changes == {} and not slot._dirty

    def test_an_unlinked_change_is_not_brought_back(self):
        slot = _ChatSlot("s1")
        slot._dismissed_source_links = {_key(7)}
        assert tc.record_tracked_change(
            slot, "https://github.com/ACME/widgets/pull/7", "acted"
        ) is (False)
        # A tentative unlink still in flight counts too, and so does an unread set.
        other = _ChatSlot("s2")
        other._dismissed_txn_pending = {_key(7)}
        assert tc.record_tracked_change(other, PR, "acted") is False
        unread = _ChatSlot("s3")
        unread._dismissed_hydrated = False
        assert tc.record_tracked_change(unread, PR, "acted") is False

    def test_a_list_not_read_back_refuses_writes(self):
        slot = _ChatSlot("s1")
        tc.mark_tracked_changes_unread(slot)
        assert tc.record_tracked_change(slot, PR, "created") is False

    def test_past_the_cap_the_oldest_entry_goes(self):
        slot = _ChatSlot("s1")
        slot._tracked_changes = {
            _key(n): _entry(n, added_at=_stamp(n)) for n in range(tc.MAX_TRACKED_CHANGES)
        }
        assert tc.record_tracked_change(slot, "https://github.com/acme/widgets/pull/500", "agent")
        assert len(slot._tracked_changes) == tc.MAX_TRACKED_CHANGES
        assert _key(0) not in slot._tracked_changes  # the oldest
        assert _key(1) in slot._tracked_changes and _key(500) in slot._tracked_changes

    @pytest.mark.asyncio
    async def test_track_change_writes_under_the_source_link_lock(self):
        slot = _ChatSlot("s1")
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.ensure_gitlab_hosts_loaded",
            return_value=None,
        ):
            assert await tc.track_change(slot, PR, "created", "tc-1") is True
        assert list(slot._tracked_changes) == [_key(7)]


class TestRestore:
    def test_a_tampered_line_keeps_only_entries_that_check_out(self, caplog):
        raw = [
            _entry(1),
            "not a dict",
            _entry(2, url="https://github.com/acme/widgets/pull/3"),  # identity mismatch
            _entry(4, url="http://github.com/acme/widgets/pull/4"),
            _entry(5, sources=[{"kind": "bogus", "ref": ""}]),
            _entry(6, added_at="yesterday"),
            _entry(7, identity="not-a-key"),
            {**_entry(8), "identity": _key(8, kind="issue"), "url": ISSUE},  # an issue
            _entry(1),  # duplicate
            _entry(9, url="https://github.com/acme/widgets/pull/9" + "x" * 2100),
        ]
        slot = _ChatSlot("s1")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.tracked_changes"):
            tc.restore_tracked_changes(slot, {tc.LINE_KEY: raw})
        assert list(slot._tracked_changes) == [_key(1)]
        assert slot._tracked_hydrated is True
        warnings = [r for r in caplog.records if "tracked_changes restore" in r.getMessage()]
        assert len(warnings) == 1 and "dropped 9 entries" in warnings[0].getMessage()

    @pytest.mark.parametrize("kind", [[], {}, 3, None])
    def test_a_source_kind_of_the_wrong_type_drops_the_entry_not_the_restore(self, kind):
        slot = _ChatSlot("s1")
        line = {tc.LINE_KEY: [_entry(1, sources=[{"kind": kind, "ref": ""}]), _entry(2)]}
        tc.restore_tracked_changes(slot, line)
        assert list(slot._tracked_changes) == [_key(2)]
        assert tc.record_tracked_change(slot, PR, kind) is False

    def test_a_mismatched_entry_neither_evicts_nor_shadows_a_valid_one(self):
        full = [_entry(n, added_at=_stamp(n)) for n in range(tc.MAX_TRACKED_CHANGES)]
        newer_mismatch = _entry(900, added_at=_stamp(3000), url=full[0]["url"])
        entries, dropped = tc.sanitize_tracked_changes([*full, newer_mismatch])
        assert len(entries) == tc.MAX_TRACKED_CHANGES and dropped == 1
        assert _key(0) in entries
        # A mismatched entry under a valid identity, ahead of it, does not shadow it.
        shadow = _entry(5, url="https://github.com/acme/widgets/pull/6")
        entries, dropped = tc.sanitize_tracked_changes([shadow, _entry(5)])
        assert list(entries) == [_key(5)] and dropped == 1
        assert entries[_key(5)]["url"].endswith("/pull/5")

    def test_an_issue_url_under_a_change_identity_neither_evicts_nor_shadows(self):
        full = [_entry(n, added_at=_stamp(n)) for n in range(tc.MAX_TRACKED_CHANGES)]
        newer_issue = _entry(900, added_at=_stamp(3000), url=ISSUE)
        entries, dropped = tc.sanitize_tracked_changes([*full, newer_issue])
        assert len(entries) == tc.MAX_TRACKED_CHANGES and dropped == 1
        assert _key(0) in entries
        shadow = _entry(5, url=ISSUE)
        entries, dropped = tc.sanitize_tracked_changes([shadow, _entry(5)])
        assert list(entries) == [_key(5)] and dropped == 1
        assert entries[_key(5)]["url"].endswith("/pull/5")

    def test_a_long_line_never_holds_more_than_the_cap(self, monkeypatch):
        held: list[int] = []
        evict = tc._evict
        monkeypatch.setattr(
            tc, "_evict", lambda entries: (held.append(len(entries)), evict(entries))[1]
        )
        raw = [_entry(n, added_at=_stamp(n % 3600)) for n in range(1000)]
        entries, dropped = tc.sanitize_tracked_changes(raw)
        assert len(entries) == tc.MAX_TRACKED_CHANGES and dropped == 1000 - tc.MAX_TRACKED_CHANGES
        assert max(held) == tc.MAX_TRACKED_CHANGES + 1

    def test_a_line_without_the_key_clears_a_reused_slots_list(self):
        slot = _ChatSlot("s1")
        slot._tracked_changes = {_key(1): _entry(1)}
        tc.restore_tracked_changes(slot, {})
        assert slot._tracked_changes == {}

    def test_a_list_past_the_cap_keeps_the_newest_and_says_so_once(self, caplog):
        raw = [_entry(n, added_at=_stamp(n)) for n in range(tc.MAX_TRACKED_CHANGES + 6)]
        slot = _ChatSlot("s1")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.tracked_changes"):
            tc.restore_tracked_changes(slot, {tc.LINE_KEY: raw})
        assert len(slot._tracked_changes) == tc.MAX_TRACKED_CHANGES
        assert _key(0) not in slot._tracked_changes and _key(69) in slot._tracked_changes
        warnings = [r for r in caplog.records if "tracked_changes restore" in r.getMessage()]
        assert len(warnings) == 1 and "dropped 6 entries" in warnings[0].getMessage()

    def test_a_self_managed_host_outside_the_allowlist_is_kept_but_hidden(self, no_gitlab):
        identity = source_ref_identity_key(
            ("gitlab", "gitlab.acme.internal", "team", "api", 9, "team/api", "change", "")
        )
        no_gitlab._publish_provider_hosts(frozenset({"gitlab.acme.internal"}), frozenset())
        written = _ChatSlot("w")
        assert tc.record_tracked_change(written, MR, "created")
        assert list(written._tracked_changes) == [identity]
        line = {tc.LINE_KEY: tc.serialize_tracked_changes(written._tracked_changes)}
        # Restored while the host is not allowed (a cold allowlist, or one revoked).
        no_gitlab._publish_provider_hosts(frozenset(), frozenset())
        slot = _ChatSlot("s1")
        tc.restore_tracked_changes(slot, line)
        assert list(slot._tracked_changes) == [identity]
        assert tc.tracked_changes_view(slot) == []
        no_gitlab._publish_provider_hosts(frozenset({"gitlab.acme.internal"}), frozenset())
        assert [link["url"] for link in tc.tracked_changes_view(slot)] == [MR]


class TestFold:
    def test_an_unread_list_carries_the_disk_value_forward(self):
        slot = _ChatSlot("s1")
        tc.mark_tracked_changes_unread(slot)
        disk = {tc.LINE_KEY: [_entry(1), "junk"]}
        for merge in (False, True):
            entries = tc.tracked_changes_fold(slot, disk, merge=merge)
            assert [e["identity"] for e in entries] == [_key(1)]

    def test_an_emptied_list_clears_by_absence_or_by_an_empty_value(self):
        slot = _ChatSlot("s1")
        disk = {tc.LINE_KEY: [_entry(1)]}
        assert tc.tracked_changes_fold(slot, disk, merge=False) is None
        assert tc.tracked_changes_fold(slot, disk, merge=True) == []
        # A line that never had the key gets none from either form.
        assert tc.tracked_changes_fold(slot, {}, merge=True) is None


class TestClearAndFork:
    def test_a_clear_drops_every_entry_and_keeps_the_tombstones(self):
        slot = _ChatSlot("s1")
        slot._tracked_changes = {_key(1): _entry(1), _key(2): _entry(2)}
        slot._dismissed_source_links = {_key(4)}
        assert tc.drop_on_clear(_fresh(slot)) is True
        assert slot._tracked_changes == {}
        assert slot._dismissed_source_links == {_key(4)}  # tombstones stay
        assert slot._dirty
        assert tc.drop_on_clear(_fresh(slot)) is False
        assert not slot._dirty

    def test_a_fork_takes_the_list_less_the_unlinked_and_no_tombstones(self):
        parent = _ChatSlot("p")
        parent._tracked_changes = {_key(n): _entry(n) for n in (1, 2, 3)}
        parent._dismissed_source_links = {_key(2), _key(5, kind="issue")}
        parent._dismissed_txn_pending = {_key(3)}  # an unlink still in flight
        child = _ChatSlot("c")
        tc.copy_tracked_for_fork(child, parent)
        assert list(child._tracked_changes) == [_key(1)]
        child._tracked_changes[_key(1)]["sources"].append({"kind": "agent", "ref": ""})
        assert len(parent._tracked_changes[_key(1)]["sources"]) == 1
        # The fork's own unlinked set is left as it is today (S5 changes that).
        assert child._dismissed_source_links == set()

    def test_a_fork_of_a_parent_whose_tombstones_are_unread_takes_no_list(self):
        parent = _ChatSlot("p")
        parent._tracked_changes = {_key(1): _entry(1)}
        parent._dismissed_hydrated = False
        child = _ChatSlot("c")
        tc.copy_tracked_for_fork(child, parent)
        assert child._tracked_changes == {}


class TestView:
    def test_newest_first_and_a_committed_unlink_hides(self):
        slot = _ChatSlot("s1")
        slot._tracked_changes = {
            _key(1): _entry(1, added_at=_stamp(1)),
            _key(2): _entry(2, added_at=_stamp(2)),
            _key(3): _entry(3, added_at=_stamp(3), sources=[{"kind": "agent", "ref": ""}]),
            _key(4): _entry(4, added_at=_stamp(4)),
            _key(5): _entry(5, added_at=_stamp(5)),
        }
        slot._dismissed_source_links = {_key(4), _key(5)}
        slot._dismissed_txn_pending = {_key(5)}  # in flight: still shown, like a chip
        view = tc.tracked_changes_view(slot)
        assert [link["number"] for link in view] == [5, 3, 2, 1]
        third = view[1]
        assert third["origin"] == "agent" and third["kind"] == "change"
        assert third["identity"] == _key(3) and third["label"]

    def test_nothing_shows_while_the_tombstones_are_unread(self):
        slot = _ChatSlot("s1")
        slot._tracked_changes = {_key(1): _entry(1)}
        slot._dismissed_hydrated = False
        assert tc.tracked_changes_view(slot) == []


# ── Through the real save, restore and read route ────────────────────────────


def _saved_slot(state, name: str):
    slot = state.get_or_create_slot(name)
    slot._titled = True
    slot.append("user", "babysit the pull request")
    slot.drain()
    _save_slot_to_history(state, slot, closed=False)
    return slot


def _line(state, slot) -> dict:
    return state.conversation_log.get_metadata(slot_history_key(slot))


def _request(slot_key: str, state, *, tracked: str = "1"):
    request = MagicMock(spec=web.Request)
    request.method = "GET"
    request.match_info = {"slot": slot_key}
    request.query = {"tracked": tracked}
    request.get = lambda key, default=None: default
    request.app = {"state": state}
    return request


async def _get(slot_key: str, state, **kw) -> dict:
    with (
        patch(
            "kiro_crew.dashboard.source_providers.chip_refresh.schedule_expanded_check_refresh",
            MagicMock(),
        ),
        patch(
            "kiro_crew.dashboard.handlers.source_providers.ensure_gitlab_hosts_loaded",
            return_value=None,
        ),
        patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=True,
        ),
        patch("kiro_crew.dashboard.chat_handlers.sel"),
    ):
        resp = await api_chat_slot_source_links(_request(slot_key, state, **kw))
    assert resp.status == 200
    return json.loads(resp.text)


class TestPersisted:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("lazy", [False, True], ids=["eager", "sidebar-row"])
    async def test_a_tracked_change_survives_a_restart_and_reads_back(
        self, lazy, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _saved_slot(state, "rt")
        # The transcript mentions a different PR: the read is the list, not a scan.
        slot.append("assistant", "see https://github.com/acme/widgets/pull/99")
        slot.drain()
        assert tc.record_tracked_change(slot, PR, "created", "tc-1")
        _save_slot_to_history(state, slot, closed=False)
        assert [e["url"] for e in _line(state, slot)[tc.LINE_KEY]] == [PR]

        key = slot_history_key(slot)
        del state._slots["rt"]
        tail = state.conversation_log.read_messages_chained(key)[-2:] if lazy else None
        restored = _rehydrate_slot_from_history(state, "rt", _lazy_tail=tail)
        assert restored is not None and restored.window_pending is lazy

        body = await _get("rt", state)
        assert [link["url"] for link in body["links"]] == [PR] and body["total"] == 1
        # The source kinds go out, their stored pointers do not.
        assert body["links"][0]["sources"] == ["created"]
        assert "tc-1" not in json.dumps(body)
        assert restored.window_pending is lazy  # the read loaded no window

    @pytest.mark.asyncio
    async def test_without_the_flag_the_route_still_scans_the_transcript(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _saved_slot(state, "scan")
        slot.append("assistant", "see https://github.com/acme/widgets/pull/99")
        assert tc.record_tracked_change(slot, PR, "created")
        body = await _get("scan", state, tracked="")
        assert [link["number"] for link in body["links"]] == [99]

    def test_a_repeat_write_does_not_rewrite_the_transcript(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _saved_slot(state, "rep")
        tc.record_tracked_change(slot, PR, "created")
        _save_slot_to_history(state, slot, closed=False)
        slot._dirty = False
        path = state.conversation_log._path(slot_history_key(slot))
        before = path.read_bytes()
        assert tc.record_tracked_change(slot, PR, "created", "tc-9") is False
        # The periodic flush saves only a dirty slot (dashboard_persistence), so an
        # unchanged dirty bit means the file is not rewritten.
        assert not slot._dirty
        assert path.read_bytes() == before

    def test_an_unreadable_line_is_carried_forward_not_emptied(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _saved_slot(state, "carry")
        tc.record_tracked_change(slot, PR, "created")
        _save_slot_to_history(state, slot, closed=False)
        # A rebind that could not read the line: memory is empty, disk is not.
        tc.mark_tracked_changes_unread(slot)
        slot.append("user", "another turn")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        assert [e["url"] for e in _line(state, slot)[tc.LINE_KEY]] == [PR]
        # A readable restore then takes it back and writes are allowed again.
        tc.restore_tracked_changes(slot, _line(state, slot))
        assert tc.record_tracked_change(slot, "https://github.com/acme/widgets/pull/8", "agent")

    def test_a_cleared_list_is_cleared_on_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _saved_slot(state, "clr")
        tc.record_tracked_change(slot, PR, "created")
        _save_slot_to_history(state, slot, closed=False)
        assert tc.drop_on_clear(slot)
        _save_slot_to_history(state, slot, closed=False)
        assert tc.LINE_KEY not in _line(state, slot)


class TestRebind:
    def test_a_cron_rebind_resets_a_reused_slots_list_until_its_line_is_read(self):
        from kiro_crew.dashboard import cron_inject

        slot = MagicMock()
        slot.linked_session_key = ""
        slot.messages = []
        slot._dismissed_source_links = set()
        slot._tracked_changes = {_key(1): _entry(1)}  # the colliding transcript's
        slot._tracked_hydrated = True
        state = MagicMock()
        state.get_or_create_slot.return_value = slot
        job = MagicMock(id="job9", member_id="", agent_id="agent", memory_store="")
        job.name = "job9"
        with patch.object(cron_inject, "hydrate_slot_from_history", lambda s, h, hidden: None):
            cron_inject._bind_cron_slot(state, job, [], [])
        assert slot._tracked_changes == {} and slot._tracked_hydrated is False
        assert cron_inject._cron_line_unread(slot)
        own = {tc.LINE_KEY: [_entry(2)]}
        assert tc.apply_tracked_changes_if_unread(slot, own) is True
        assert list(slot._tracked_changes) == [_key(2)]
        # A list hydrated while a read ran is newer than that read's line.
        assert tc.apply_tracked_changes_if_unread(slot, {tc.LINE_KEY: [_entry(3)]}) is False
        assert list(slot._tracked_changes) == [_key(2)]
