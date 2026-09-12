"""Tests for unlinking a PR/issue/Jira source-link chip from a chat session.

The chips are DERIVED by re-scanning the transcript, so a naive delete is undone
by the next re-scan. Unlinking instead records the link's serialized identity in
a per-slot dismissed set that the derivation filters against, persists it so a
restart cannot resurrect the chip, and touches no remote provider.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.dashboard import channel_slots
from kiro_crew.dashboard.chat_handlers import api_chat_slot_source_link_unlink
from kiro_crew.dashboard.handlers.source_providers import (
    is_valid_source_identity_key,
    parse_source_url,
    source_ref_identity_key,
)
from kiro_crew.dashboard.state import _ChatSlot

PR_A = "https://github.com/acme/widgets/pull/11"
PR_B = "https://github.com/acme/widgets/pull/12"
ISSUE_A = "https://github.com/acme/widgets/issues/21"


def _identity_key(url: str) -> str:
    return source_ref_identity_key(parse_source_url(url).identity)


def _slot(*urls: str) -> _ChatSlot:
    slot = _ChatSlot("s1")
    slot.append("assistant", "\n".join(urls or (PR_A, PR_B, ISSUE_A)), ts="t1")
    return slot


class TestDerivationFilter:
    def test_dismiss_suppresses_a_derived_link(self):
        slot = _slot()
        before = {link["url"] for link in slot.to_dict()["source_links"]}
        assert PR_A in before

        assert slot.dismiss_source_link(_identity_key(PR_A)) is True
        after = {link["url"] for link in slot.to_dict()["source_links"]}
        assert PR_A not in after

    def test_a_non_dismissed_link_is_unaffected(self):
        slot = _slot()
        slot.dismiss_source_link(_identity_key(PR_A))
        surviving = {link["url"] for link in slot.to_dict()["source_links"]}
        # Only the dismissed change is gone; its siblings stay.
        assert PR_B in surviving
        assert ISSUE_A in surviving

    def test_dismiss_matches_the_object_across_url_shapes(self):
        """A trailing-slash re-mention is the same object, so a dismiss keyed on
        the identity suppresses it whichever spelling re-derived it."""
        slot = _slot(PR_A + "/", PR_B)
        # The canonical identity is spelling-independent.
        assert slot.dismiss_source_link(_identity_key(PR_A)) is True
        surviving = {link["url"] for link in slot.to_dict()["source_links"]}
        assert not any("/pull/11" in url for url in surviving)

    def test_repeated_dismiss_is_idempotent(self):
        slot = _slot()
        key = _identity_key(PR_A)
        assert slot.dismiss_source_link(key) is True
        assert slot.dismiss_source_link(key) is False

    def test_dismiss_invalidates_the_cache(self):
        slot = _slot()
        # Prime the cache.
        first = slot._pr_source_links()
        assert any(link["url"] == PR_A for link in first)
        rev_before = slot._source_links_revision
        slot.dismiss_source_link(_identity_key(PR_A))
        # The revision moved, so the next read re-derives rather than serving the
        # stale cached list that still holds the dismissed link.
        assert slot._source_links_revision == rev_before + 1
        second = slot._pr_source_links()
        assert not any(link["url"] == PR_A for link in second)


class TestPersistenceRoundTrip:
    def test_dismiss_survives_a_simulated_reload(self):
        """The dismissed set is written to durable slot metadata and rehydrated,
        so a gateway restart does not resurrect a chip the user unlinked."""
        from kiro_crew.dashboard.chat_persistence import _restore_dismissed_source_links

        slot = _slot()
        key = _identity_key(PR_A)
        slot.dismiss_source_link(key)

        # What the save path serializes for this field (sorted, JSON-scalar keys).
        persisted = sorted(slot._dismissed_source_links)
        assert persisted == [key]

        # A fresh slot rehydrating from that metadata reconstructs the set and
        # keeps suppressing the link.
        reloaded = _slot()
        _restore_dismissed_source_links(reloaded, persisted)
        assert reloaded._dismissed_source_links == {key}
        assert not any(link["url"] == PR_A for link in reloaded.to_dict()["source_links"])

    def test_reload_drops_a_tampered_identity_key(self):
        """History JSONL is disk-tamperable; a malformed key can never match a
        real identity, so it is dropped on restore rather than stored as junk."""
        from kiro_crew.dashboard.chat_persistence import _restore_dismissed_source_links

        slot = _slot()
        _restore_dismissed_source_links(
            slot, [_identity_key(PR_A), "not-json", "{}", ['{"nested":[1]}']]
        )
        assert slot._dismissed_source_links == {_identity_key(PR_A)}

    @pytest.mark.asyncio
    async def test_unhydrated_full_save_carries_the_on_disk_dismissed_line(
        self, tmp_path, monkeypatch
    ):
        # A slot bound to a transcript whose dismissed set could not be read is
        # marked _dismissed_hydrated=False; its FULL save must carry the on-disk
        # dismissed line forward, not erase it with the empty in-memory set.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
        from kiro_crew.dashboard.state import _ChatSlot

        state = _make_state(tmp_path)
        key = _identity_key(PR_A)
        hkey = "cron:job99"
        # Seed the transcript metadata with a persisted dismissal.
        state.conversation_log.update_metadata(hkey, {"dismissed_source_links": [key]})

        # A fresh slot bound to that transcript that could NOT read the set.
        slot = _ChatSlot("cron-job99")
        slot.linked_session_key = hkey
        slot.append("assistant", "cron result", ts="t1")
        assert slot._dismissed_source_links == set()  # empty in-memory
        slot._dismissed_hydrated = False  # bound but dismissed-unhydrated

        await save_slot_off_loop(state, slot, force=True)

        meta = state.conversation_log._read_metadata(hkey) or {}
        assert meta.get("dismissed_source_links") == [key]  # carried forward, NOT erased

    @pytest.mark.asyncio
    async def test_unhydrated_empty_window_save_carries_the_on_disk_dismissed_line(
        self, tmp_path, monkeypatch
    ):
        # The EMPTY-WINDOW (merge-writer) save path must ALSO carry the on-disk
        # dismissed line forward for an unhydrated slot, not erase it with []. A
        # slot with no durable window rows routes through that branch.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
        from kiro_crew.dashboard.state import _ChatSlot

        state = _make_state(tmp_path)
        key = _identity_key(PR_A)
        hkey = "cron:jobEW"
        state.conversation_log.update_metadata(hkey, {"dismissed_source_links": [key]})

        # Bound, unhydrated, EMPTY window (no messages) -> empty-window save branch.
        slot = _ChatSlot("cron-jobEW")
        slot.linked_session_key = hkey
        assert slot._dismissed_source_links == set()
        slot._dismissed_hydrated = False

        await save_slot_off_loop(state, slot, force=True)

        meta = state.conversation_log._read_metadata(hkey) or {}
        assert meta.get("dismissed_source_links") == [key]  # carried forward, NOT erased

    @pytest.mark.asyncio
    async def test_txn_in_flight_full_save_carries_the_on_disk_dismissed_line(
        self, tmp_path, monkeypatch
    ):
        # While an unlink transaction holds an uncommitted TENTATIVE dismissal
        # (_dismissed_txn_depth > 0), a periodic full save must carry the
        # on-disk dismissed line forward, NOT persist the tentative in-memory set
        # — the guarded write may still fail and roll it back, and a flush that
        # committed the tentative tombstone would survive a 409'd DELETE.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
        from kiro_crew.dashboard.state import _ChatSlot

        state = _make_state(tmp_path)
        on_disk = _identity_key(PR_A)
        tentative = _identity_key(PR_B)
        hkey = "cron:jobTX"
        state.conversation_log.update_metadata(hkey, {"dismissed_source_links": [on_disk]})

        slot = _ChatSlot("cron-jobTX")
        slot.linked_session_key = hkey
        slot.append("assistant", "cron result", ts="t1")
        slot._dismissed_hydrated = True
        slot._dismissed_source_links = {on_disk, tentative}  # tentative not yet committed
        slot._dismissed_txn_depth = 1  # a transaction in flight

        await save_slot_off_loop(state, slot, force=True)

        meta = state.conversation_log._read_metadata(hkey) or {}
        # ONLY the on-disk line survives — the tentative key is NOT persisted.
        assert meta.get("dismissed_source_links") == [on_disk]

    @pytest.mark.asyncio
    async def test_stale_hydrated_full_save_does_not_erase_a_newer_on_disk_tombstone(
        self, tmp_path, monkeypatch
    ):
        # A HYDRATED slot whose in-memory set is STALE — it was bound from an
        # off-loop prefetch that predates a concurrent unlink's committed
        # tombstone (workflow/cron fallback). A bare replacement full save would
        # shrink the on-disk set and erase the newer tombstone (chip reappears on
        # restart). The save must UNION memory with the on-disk line so the
        # committed tombstone survives.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
        from kiro_crew.dashboard.state import _ChatSlot

        state = _make_state(tmp_path)
        stale_known = _identity_key(PR_A)  # in the prefetched (stale) set
        committed_newer = _identity_key(PR_B)  # committed on disk AFTER the prefetch
        hkey = "cron:jobSTALE"
        # Disk already carries BOTH: the stale-known one and a newer committed one.
        state.conversation_log.update_metadata(
            hkey, {"dismissed_source_links": [stale_known, committed_newer]}
        )

        slot = _ChatSlot("cron-jobSTALE")
        slot.linked_session_key = hkey
        slot.append("assistant", "cron result", ts="t1")
        slot._dismissed_hydrated = True  # bound from a readable (but stale) prefetch
        slot._dismissed_source_links = {stale_known}  # MISSING committed_newer

        await save_slot_off_loop(state, slot, force=True)

        meta = state.conversation_log._read_metadata(hkey) or {}
        # The newer committed tombstone is PRESERVED (union), not erased.
        assert set(meta.get("dismissed_source_links") or []) == {stale_known, committed_newer}

    def test_a_real_identity_key_validates(self):
        assert is_valid_source_identity_key(_identity_key(PR_A))

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "not-json",
            "{}",
            "[]",
            '{"a":1}',
            '["a", {"b": 1}]',  # nested container member
            "[1]",  # canonical JSON but wrong arity (was accepted before)
            '["p","h","o","r","5","","change",""]',  # number slot is a string
            '["github","github.com","acme","private",12,"","change"]',  # 7 members
            '["github","github.com","acme","private",12,"","change","",""]',  # 9 members
            "x" * 4096,  # oversized
            123,
            None,
        ],
    )
    def test_malformed_keys_are_rejected(self, bad):
        assert is_valid_source_identity_key(bad) is False


def _pin_ok(state, created_at: str = "t"):
    """Configure a mock state's conversation_log so the unlink handler's
    mandatory identity-pin read (get_metadata_status) returns a readable
    transcript — production transcripts are readable, so this mirrors it. The
    write guard requires meta.created_at == this value, so side_effects that
    invoke the guard should pass a meta with the same created_at."""
    state.conversation_log.get_metadata_status.return_value = (
        {"_type": "metadata", "created_at": created_at},
        True,
    )
    return state


def _request(slot_key: str, identity: str, slots: dict, *, app: str = ""):
    request = MagicMock(spec=web.Request)
    request.method = "DELETE"
    request.match_info = {"slot": slot_key, "identity": identity}
    request.get = lambda key, default=None: app if key == "app" else default
    request.app = {"state": _pin_ok(MagicMock(_slots=slots))}
    return request


async def _delete(slot_key: str, identity: str, slots: dict, *, app: str = "") -> web.Response:
    with patch("kiro_crew.dashboard.chat_handlers.sel"):
        return await api_chat_slot_source_link_unlink(_request(slot_key, identity, slots, app=app))


class TestUnlinkEndpoint:
    @pytest.mark.asyncio
    async def test_delete_records_the_dismissal_and_broadcasts(self):
        slot = _slot()
        state_slots = {"s1": slot}
        key = _identity_key(PR_A)
        req = _request("s1", key, state_slots)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            resp = await api_chat_slot_source_link_unlink(req)

        assert resp.status == 200
        assert json.loads(resp.text) == {"ok": True, "dismissed": True}
        assert key in slot._dismissed_source_links
        # The chip disappears immediately (slots push) and is persisted through a
        # field-scoped update_metadata merge of ONLY dismissed_source_links (never
        # a full-slot save, which would rebuild title/tags/folder and could revert
        # a sibling alias's committed rename on a shared transcript).
        req.app["state"].push_slots_update.assert_called_once()
        um = req.app["state"].conversation_log.update_metadata_if
        assert um.call_count == 1
        _hkey, fields, _guard = um.call_args.args
        assert set(fields.keys()) == {"dismissed_source_links"}
        assert key in fields["dismissed_source_links"]

    @pytest.mark.asyncio
    async def test_identity_is_not_double_decoded(self):
        # aiohttp already percent-decodes the route segment into match_info. The
        # handler must NOT unquote it again: a second decode would collapse two
        # identities that differ only by encoding (e.g. a raw "%61" vs "a") onto
        # the same key and dismiss the WRONG source link. Feed a match_info
        # identity that still contains a "%61" sequence and assert the persisted
        # dismissal carries it VERBATIM (not decoded to "a").
        encoded_identity = '["github","acme","widgets","%61","pull","11","change"]'
        slot = _slot()
        req = _request("s1", encoded_identity, {"s1": slot})
        req.app["state"].conversation_log.update_metadata_if.return_value = True
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.handlers.source_providers.is_valid_source_identity_key",
                new=lambda k: True,
            ),
            patch.object(
                type(slot),
                "_pr_source_links",
                new=lambda self: [{"identity": encoded_identity}],
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        # The "%61" survived into the in-memory set AND the persisted write — it
        # was NOT decoded to "a" (which would dismiss a different chip).
        assert encoded_identity in slot._dismissed_source_links
        um = req.app["state"].conversation_log.update_metadata_if
        _hkey, fields, _guard = um.call_args.args
        assert encoded_identity in fields["dismissed_source_links"]

    @pytest.mark.asyncio
    async def test_delete_invalidates_the_derivation_cache(self):
        slot = _slot()
        slot._pr_source_links()  # prime cache
        rev_before = slot._source_links_revision
        await _delete("s1", _identity_key(PR_A), {"s1": slot})
        assert slot._source_links_revision == rev_before + 1
        assert not any(link["url"] == PR_A for link in slot._pr_source_links())

    @pytest.mark.asyncio
    async def test_malformed_identity_is_400_with_a_code(self):
        resp = await _delete("s1", "not-a-real-key", {"s1": _slot()})
        assert resp.status == 400
        assert json.loads(resp.text) == {
            "error": "invalid source-link identity",
            "code": "invalid_source_identity",
        }

    @pytest.mark.asyncio
    async def test_unknown_slot_is_404_with_a_code(self):
        resp = await _delete("nope", _identity_key(PR_A), {"s1": _slot()})
        assert resp.status == 404
        assert json.loads(resp.text) == {"error": "not found", "code": "slot_not_found"}

    @pytest.mark.asyncio
    async def test_a_valid_but_never_derived_identity_is_404(self):
        # Bounds durable-state growth: a format-valid identity that is NOT one of
        # the slot's derived chips (nor already dismissed) must be rejected, not
        # stored -- otherwise a caller could grow the dismissed set unboundedly.
        slot = _slot(PR_A)  # only PR_A is a derived chip on this slot
        resp = await _delete("s1", _identity_key(PR_B), {"s1": slot})
        assert resp.status == 404
        assert json.loads(resp.text) == {
            "error": "not found",
            "code": "source_link_not_found",
        }
        assert slot._dismissed_source_links == set()  # nothing stored

    @pytest.mark.asyncio
    async def test_a_refused_persist_returns_409_and_rolls_back(self):
        # The dismissal is persisted by a field-scoped update_metadata merge. If
        # that write raises (session gone / lock timeout), acknowledging 200
        # would show a chip gone that reappears on restart — so the in-memory
        # dismissal is rolled back and the request 409s.
        slot = _slot()
        key = _identity_key(PR_A)
        state = MagicMock(_slots={"s1": slot})
        _pin_ok(state)
        state.conversation_log.update_metadata_if.side_effect = OSError("lock timeout")
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 409
        assert json.loads(resp.text) == {
            "error": "session was deleted or rebound",
            "code": "session_gone",
        }
        assert key not in slot._dismissed_source_links  # rolled back

    @pytest.mark.asyncio
    async def test_unhydrated_alias_folds_the_on_disk_dismissed_set(self):
        # A slot bound to a transcript whose dismissed set could not be read is
        # _dismissed_hydrated=False with an empty in-memory set. An unlink must
        # NOT write a union computed from that empty set — it would drop the
        # transcript's durable tombstones. The handler folds the on-disk set in
        # (read under the lock) so the write is a superset, never a replacement.
        slot = _slot()
        slot._dismissed_hydrated = False  # bound but dismissed-unread
        key = _identity_key(PR_A)
        pre_existing = _identity_key(PR_B)  # already on disk, NOT in memory
        state = MagicMock(_slots={"s1": slot})
        state.conversation_log.get_metadata_status.return_value = (
            {"_type": "metadata", "created_at": "t", "dismissed_source_links": [pre_existing]},
            True,
        )
        state.conversation_log.update_metadata_if.return_value = True
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        # The written union includes BOTH the new key and the pre-existing
        # on-disk dismissal (folded in), not just the new key.
        written = state.conversation_log.update_metadata_if.call_args.args[1]
        assert set(written["dismissed_source_links"]) == {key, pre_existing}
        assert slot._dismissed_hydrated is True  # now hydrated

    @pytest.mark.asyncio
    async def test_unhydrated_alias_with_unreadable_fold_read_aborts(self):
        # An unreadable transcript at authorization fails the mandatory identity
        # pin read (before any mutation), so the unlink declines (409) and writes
        # nothing rather than persist against a transcript it cannot identify.
        # This subsumes the unhydrated-fold-unreadable case: no readable identity
        # => no write.
        slot = _slot()
        slot._dismissed_hydrated = False
        key = _identity_key(PR_A)
        state = MagicMock(_slots={"s1": slot})
        state.conversation_log.get_metadata_status.return_value = ({}, False)  # unreadable
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 409
        state.conversation_log.update_metadata_if.assert_not_called()  # no write
        assert key not in slot._dismissed_source_links  # rolled back

    @pytest.mark.asyncio
    async def test_persist_writes_only_the_dismissed_field_never_a_full_save(self):
        # GPT 5.6: a full-slot save rebuilds title/tags/folder from the requesting
        # slot's live fields, so on a shared transcript it can revert a sibling
        # alias's committed rename. The unlink must persist ONLY the
        # dismissed_source_links field via update_metadata (a merge that leaves
        # every other field intact) and must NOT call save_slot_off_loop.
        slot = _slot()
        key = _identity_key(PR_A)
        state = MagicMock(_slots={"s1": slot})
        _pin_ok(state)
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
                new=AsyncMock(return_value=True),
            ) as saver,
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        saver.assert_not_awaited()  # no full-slot save -> no metadata clobber
        um = state.conversation_log.update_metadata_if
        assert um.call_count == 1
        _hkey, fields, _guard = um.call_args.args
        assert set(fields.keys()) == {"dismissed_source_links"}

    @pytest.mark.asyncio
    async def test_sibling_alias_shares_the_union_in_one_write(self):
        # dismissed_source_links lives on the SHARED transcript line that every
        # alias on the history key points at, so ONE update_metadata write of the
        # UNION covers all aliases at once — the in-memory sets are mirrored onto
        # live aliases (so the UI reflects the change) and the union is what a
        # racing alias flush would also carry, so it can only re-persist a value
        # already on disk. No per-slot mirror, no full-slot save.
        primary = _slot(PR_A, PR_B)
        sibling = _slot(PR_A, PR_B)  # same transcript, already dismissed PR_B
        kb = _identity_key(PR_B)
        sibling.dismiss_source_link(kb)
        ka = _identity_key(PR_A)
        state = MagicMock(_slots={"s1": primary, "s2": sibling})
        _pin_ok(state)
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": ka}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "shared-history-key",
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        assert ka in primary._dismissed_source_links
        assert ka in sibling._dismissed_source_links  # mirrored in memory
        um = state.conversation_log.update_metadata_if
        assert um.call_count == 1  # ONE write covers both aliases
        _hkey, fields, _guard = um.call_args.args
        written = set(fields["dismissed_source_links"])
        assert ka in written and kb in written  # the UNION across aliases

    @pytest.mark.asyncio
    async def test_a_refused_persist_rolls_back_every_alias(self):
        # On a failed persist, the rollback must clear the dismissal from the
        # requesting slot AND every alias it was mirrored onto, so acknowledged
        # in-memory state matches disk (which the failed merge left unchanged).
        primary = _slot()
        sibling = _slot()
        key = _identity_key(PR_A)
        state = MagicMock(_slots={"s1": primary, "s2": sibling})
        _pin_ok(state)
        state.conversation_log.update_metadata_if.side_effect = OSError("gone")
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "shared-history-key",
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 409
        assert key not in primary._dismissed_source_links
        assert key not in sibling._dismissed_source_links

    @pytest.mark.asyncio
    async def test_a_guard_refused_persist_is_not_acknowledged(self):
        # update_metadata_if returns False (no raise) when the transcript's
        # metadata line is unreadable/not a metadata line — the merge wrote
        # NOTHING. Acknowledging 200 would show a chip gone that reappears on
        # restart, so a False result is treated as a failed persist: rollback + 409.
        slot = _slot()
        key = _identity_key(PR_A)
        state = MagicMock(_slots={"s1": slot})
        _pin_ok(state)
        state.conversation_log.update_metadata_if.return_value = False
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 409
        assert key not in slot._dismissed_source_links  # rolled back
        # The guard passed to update_metadata_if is pinned to the authorized
        # transcript's created_at (read before mutation; "t" via _pin_ok). It
        # accepts only that identity and rejects a deleted/recreated transcript.
        guard = state.conversation_log.update_metadata_if.call_args.args[2]
        assert guard({"_type": "metadata", "created_at": "t"}) is True  # pinned identity
        assert guard({"_type": "metadata", "created_at": "other"}) is False  # recreated
        assert guard({}) is False  # deleted transcript — do NOT resurrect
        assert guard({"foo": "bar"}) is False  # not a metadata line

    @pytest.mark.asyncio
    async def test_txn_depth_survives_a_concurrent_unlink_and_clears_on_failed_persist(self):
        # A periodic full-save flush that fires between the in-memory mutate and
        # the guarded write must NOT persist the tentative tombstone: the slot's
        # _dismissed_txn_depth is >0 so the save carries the on-disk line
        # forward. When the guarded write then fails (409), the depth is
        # decremented back to 0 and the in-memory set is rolled back — a restart
        # must not hide a chip for a DELETE that failed. The depth is a COUNTER,
        # not a bool: a CONCURRENT unlink's own increment (simulated here) must
        # survive this request's rollback, so the flush still carries-forward for
        # the concurrent transaction.
        slot = _slot()
        key = _identity_key(PR_A)
        state = MagicMock(_slots={"s1": slot})
        _pin_ok(state)
        slot._dismissed_txn_depth = 1  # a concurrent unlink already in flight
        seen = {}

        def _persist(_hkey, _fields, _guard):
            # Snapshot the depth AS A CONCURRENT FLUSH WOULD SEE IT: mid-transaction.
            seen["depth_during_write"] = slot._dismissed_txn_depth
            return False  # guarded write fails -> rollback + 409

        state.conversation_log.update_metadata_if.side_effect = _persist
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 409
        assert seen["depth_during_write"] == 2  # our +1 on top of the concurrent +1
        assert slot._dismissed_txn_depth == 1  # OUR increment undone; concurrent one survives
        assert key not in slot._dismissed_source_links  # rolled back

    @pytest.mark.asyncio
    async def test_guard_rejects_a_recreated_transcript_identity(self):
        # The write guard is pinned to the authorized transcript's created_at,
        # read ONCE before any mutation. EVERY write (first, confirm, compensate)
        # must observe that same identity; a deleted+recreated transcript (fresh
        # created_at) fails the guard, so a stale dismissal cannot land in the
        # replacement session.
        slot = _slot()
        key = _identity_key(PR_A)
        state = MagicMock(_slots={"s1": slot})
        _pin_ok(state, created_at="orig-2026")  # pin the authorized identity
        state.conversation_log.update_metadata_if.return_value = False
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 409
        guard = state.conversation_log.update_metadata_if.call_args.args[2]
        assert guard({"_type": "metadata", "created_at": "orig-2026"}) is True  # pinned identity
        assert (
            guard({"_type": "metadata", "created_at": "new-2026"}) is False
        )  # recreated -> reject
        assert guard({"_type": "metadata"}) is False  # no created_at on a recreated line -> reject

    @pytest.mark.asyncio
    async def test_a_failed_persist_keeps_an_aliases_pre_existing_dismissal(self):
        # The rollback must clear the identity ONLY from slots THIS request newly
        # dismissed. An alias that had already committed this dismissal earlier
        # keeps it — rolling it back would resurrect a chip that alias legitimately
        # removed. Here the sibling already had PR_A dismissed; the requesting
        # slot's unlink of PR_A then fails to persist, and the sibling must retain it.
        primary = _slot()
        sibling = _slot()
        key = _identity_key(PR_A)
        sibling.dismiss_source_link(key)  # pre-existing, committed earlier
        state = MagicMock(_slots={"s1": primary, "s2": sibling})
        _pin_ok(state)
        state.conversation_log.update_metadata_if.side_effect = OSError("gone")
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "shared-history-key",
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 409
        assert key not in primary._dismissed_source_links  # requesting slot rolled back
        assert key in sibling._dismissed_source_links  # pre-existing dismissal KEPT
        # The per-transcript transaction lock must be acquired and released
        # cleanly per call: two unlinks of different chips on the same slot
        # (same transcript key) both go through and both end up dismissed.
        slot = _slot(PR_A, PR_B)
        ka, kb = _identity_key(PR_A), _identity_key(PR_B)
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
                new=AsyncMock(return_value=True),
            ),
        ):
            r1 = await api_chat_slot_source_link_unlink(_request("s1", ka, {"s1": slot}))
            r2 = await api_chat_slot_source_link_unlink(_request("s1", kb, {"s1": slot}))
        assert r1.status == 200 and r2.status == 200
        assert {ka, kb} <= slot._dismissed_source_links

    @pytest.mark.asyncio
    async def test_a_slot_rebound_during_persist_does_not_carry_the_dismissal(self):
        # A slot's linked_session_key can be rebound (cron/workflow injection)
        # during the persist await. A slot dismissed by THIS request that rebinds
        # to a DIFFERENT transcript mid-await must have the dismissal stripped —
        # otherwise it rides into the new conversation and suppresses an unrelated
        # matching link. The requesting slot stays authorized and keeps it.
        primary = _slot()
        sibling = _slot()
        key = _identity_key(PR_A)
        # Per-slot history key; the sibling rebinds away the instant the persist
        # write runs (side_effect mutates the map before returning success).
        hkeys = {id(primary): "authorized", id(sibling): "authorized"}

        def _rebind_sibling_then_persist(*_a, **_k):
            hkeys[id(sibling)] = "rebound-elsewhere"
            return True

        state = MagicMock(_slots={"s1": primary, "s2": sibling})
        _pin_ok(state)
        state.conversation_log.update_metadata_if.side_effect = _rebind_sibling_then_persist
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: hkeys[id(s)],
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200  # authorized transcript persisted
        assert key in primary._dismissed_source_links  # requesting slot keeps it
        assert key not in sibling._dismissed_source_links  # rebound slot stripped

    @pytest.mark.asyncio
    async def test_rebound_slot_keeps_a_dismissal_its_new_transcript_already_holds(self):
        # A slot dismissed by THIS request rebinds to a DIFFERENT transcript mid-
        # await, but that NEW transcript already has this identity dismissed (a
        # concurrent unlink committed it there). The rollback must NOT discard the
        # key from the rebound slot — doing so would erase the dismissal the other
        # request legitimately committed and make its unlinked chip reappear. The
        # rebound read finds the key on the new transcript, so it is kept.
        primary = _slot()
        sibling = _slot()
        key = _identity_key(PR_A)
        hkeys = {id(primary): "authorized", id(sibling): "authorized"}

        def _rebind_sibling_then_persist(*_a, **_k):
            hkeys[id(sibling)] = "rebound-target"
            return True

        # The rebound target ("rebound-target") already carries this dismissal;
        # the authorized transcript's pin read carries created_at "t".
        def _meta_status(hkey):
            if hkey == "rebound-target":
                return (
                    {"_type": "metadata", "created_at": "t", "dismissed_source_links": [key]},
                    True,
                )
            return ({"_type": "metadata", "created_at": "t"}, True)

        state = MagicMock(_slots={"s1": primary, "s2": sibling})
        state.conversation_log.get_metadata_status.side_effect = _meta_status
        state.conversation_log.update_metadata_if.side_effect = _rebind_sibling_then_persist
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: hkeys[id(s)],
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        assert key in primary._dismissed_source_links  # requesting slot keeps it
        # The rebound slot KEEPS the key — its new transcript already holds it, so
        # this request must not erase the concurrent request's committed dismissal.
        assert key in sibling._dismissed_source_links

    @pytest.mark.asyncio
    async def test_rebound_slot_discards_when_new_transcript_is_unreadable(self):
        # A slot dismissed by THIS request rebinds to a DIFFERENT transcript mid-
        # await and that new transcript's metadata is UNREADABLE. We cannot tell
        # whether the key is the target's own or a stray from this request, so we
        # DISCARD it (the safe default): keeping a foreign key would let the
        # union-on-save guard persist it into the target and hide the target's own
        # chip, whereas discarding a genuinely target-owned key is re-added from
        # the target's on-disk line on its next save.
        primary = _slot()
        sibling = _slot()
        key = _identity_key(PR_A)
        hkeys = {id(primary): "authorized", id(sibling): "authorized"}

        def _rebind_sibling_then_persist(*_a, **_k):
            hkeys[id(sibling)] = "rebound-unreadable"
            return True

        # Authorized pin read is readable ("t"); the rebound target is UNREADABLE.
        def _meta_status(hkey):
            if hkey == "rebound-unreadable":
                return ({}, False)
            return ({"_type": "metadata", "created_at": "t"}, True)

        state = MagicMock(_slots={"s1": primary, "s2": sibling})
        state.conversation_log.get_metadata_status.side_effect = _meta_status
        state.conversation_log.update_metadata_if.side_effect = _rebind_sibling_then_persist
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: hkeys[id(s)],
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        assert key in primary._dismissed_source_links  # requesting slot keeps it
        # Unreadable target: DISCARD, so no foreign tombstone rides into it.
        assert key not in sibling._dismissed_source_links

    @pytest.mark.asyncio
    async def test_late_alias_joiner_that_rebinds_during_confirm_is_reconciled(self):
        # A slot that binds INTO the authorized transcript during the FIRST
        # persist await is mirrored (a "late-alias joiner") and appended to
        # ``newly_added``, then a confirm write re-asserts the union. That joiner
        # can rebind AWAY during the confirm await. Without a post-confirm
        # reconciliation its in-memory dismissal would ride into the replacement
        # transcript's next save (a foreign tombstone). The joiner's new
        # transcript is UNREADABLE here, so the reconciliation must DISCARD it.
        primary = _slot()
        joiner = _slot()
        key = _identity_key(PR_A)
        # ``joiner`` sits on the authorized transcript at the mirror scan (so it
        # is picked up as a late joiner, mirrored, and a confirm write follows),
        # then rebinds away on that confirm (2nd) write.
        hkeys = {id(primary): "authorized", id(joiner): "other-transcript"}
        writes = {"n": 0}

        def _on_write(*_a, **_k):
            writes["n"] += 1
            if writes["n"] == 1:  # first persist: joiner binds INTO authorized
                hkeys[id(joiner)] = "authorized"
            elif writes["n"] >= 2:  # confirm write: joiner rebinds AWAY
                hkeys[id(joiner)] = "rebound-unreadable"
            return True

        def _meta_status(hkey):
            if hkey == "rebound-unreadable":
                return ({}, False)
            return ({"_type": "metadata", "created_at": "t"}, True)

        state = MagicMock(_slots={"s1": primary, "s2": joiner})
        state.conversation_log.get_metadata_status.side_effect = _meta_status
        state.conversation_log.update_metadata_if.side_effect = _on_write
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: hkeys[id(s)],
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        assert key in primary._dismissed_source_links
        # Joiner rebound away during confirm on an unreadable target -> discarded.
        assert key not in joiner._dismissed_source_links

    @pytest.mark.asyncio
    async def test_broadcast_failure_rolls_back_the_in_memory_dismissal(self):
        # The optimistic push_slots_update() broadcast fires right after the
        # in-memory dismissal but before persistence. If it RAISES (an
        # unserializable slot), the tentative dismissal must be rolled back off
        # the requesting slot — otherwise a later periodic full-save would
        # persist a dismissal the request never durably authorized, hiding a chip
        # with no committed authorization.
        primary = _slot()
        key = _identity_key(PR_A)
        state = MagicMock(_slots={"s1": primary})
        _pin_ok(state)
        state.push_slots_update.side_effect = RuntimeError("unserializable slot")
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "authorized",
            ),
        ):
            with pytest.raises(RuntimeError, match="unserializable slot"):
                await api_chat_slot_source_link_unlink(req)
        # The tentative dismissal was rolled back — memory carries nothing the
        # request did not durably commit.
        assert key not in primary._dismissed_source_links

    @pytest.mark.asyncio
    async def test_unlink_carries_forward_a_departed_aliases_durable_tombstone(self):
        # A prior unlink committed a UNIQUE tombstone (key B) via an alias that
        # has since DEPARTED this transcript (rebound away), so B lives only on
        # disk — no live alias holds it. Unlinking a DIFFERENT key (A) rebuilds
        # the write set from the live aliases; without an unconditional on-disk
        # fold that set omits B and the write SHRINKS the durable line, so B's
        # dismissed chip reappears after restart. The write must union the
        # on-disk line so B is carried forward alongside the new A.
        primary = _slot()
        ka = _identity_key(PR_A)
        kb = _identity_key(PR_B)
        state = MagicMock(_slots={"s1": primary})
        # Every live alias is fully hydrated (default), so a fold gated on
        # "any unhydrated alias" would NOT fire — the bug this locks.
        captured = {}

        def _meta_status(_hkey):
            # On-disk line already carries the departed alias's unique tombstone B.
            return ({"_type": "metadata", "created_at": "t", "dismissed_source_links": [kb]}, True)

        def _capture_write(_hkey, fields, _guard):
            captured["dismissed"] = set(fields.get("dismissed_source_links", []))
            return True

        state.conversation_log.get_metadata_status.side_effect = _meta_status
        state.conversation_log.update_metadata_if.side_effect = _capture_write
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": ka}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "authorized",
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        # The persisted set carries BOTH the new A and the departed alias's B.
        assert {ka, kb} <= captured["dismissed"]

    @pytest.mark.asyncio
    async def test_app_token_cannot_unlink_a_dashboard_owned_slot(self):
        slot = _slot()
        slot._app = ""  # dashboard-owned
        resp = await _delete("s1", _identity_key(PR_A), {"s1": slot}, app="design_critique")
        assert resp.status == 404
        assert json.loads(resp.text) == {"error": "not found", "code": "slot_not_found"}
        # The dismissal must NOT have been recorded on a denied request.
        assert slot._dismissed_source_links == set()

    @pytest.mark.asyncio
    async def test_repeat_delete_is_idempotent_and_skips_the_extra_write(self):
        slot = _slot()
        key = _identity_key(PR_A)
        slot.dismiss_source_link(key)  # already dismissed
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
                new=AsyncMock(return_value=True),
            ) as saver,
        ):
            req = _request("s1", key, {"s1": slot})
            # A genuine idempotent repeat: the key is ALREADY durable on disk, so
            # the fast path (no re-write, no re-broadcast) is valid. Reflect that
            # in the pin/durability read the handler consults.
            req.app["state"].conversation_log.get_metadata_status.return_value = (
                {"_type": "metadata", "created_at": "t", "dismissed_source_links": [key]},
                True,
            )
            resp = await api_chat_slot_source_link_unlink(req)

        assert resp.status == 200
        assert json.loads(resp.text) == {"ok": True, "dismissed": True}
        # A no-op repeat neither re-broadcasts nor re-persists.
        req.app["state"].push_slots_update.assert_not_called()
        saver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refused_save_emits_a_failed_sel_audit(self):
        # A post-authorization failure return must still
        # leave an audit trail. The persist-failure 409 path early-returns before
        # the trailing allowed audit, so it must emit its OWN failed event -- an
        # attempted-and-refused unlink cannot vanish from the SEL log.
        slot = _slot()
        key = _identity_key(PR_A)
        sel_mock = MagicMock()
        state = MagicMock(_slots={"s1": slot})
        _pin_ok(state)
        state.conversation_log.update_metadata_if.side_effect = OSError("gone")
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with patch("kiro_crew.dashboard.chat_handlers.sel", new=lambda: sel_mock):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 409
        calls = sel_mock.log_tool_invocation.call_args_list
        assert len(calls) == 1
        kwargs = calls[0].kwargs
        assert kwargs["tool_name"] == "source_link_unlink"
        assert kwargs["outcome"] == "failed"
        assert kwargs["error"] == "session_gone"
        assert kwargs["metadata"]["phase"] == "metadata_persist"

    @pytest.mark.asyncio
    async def test_lock_rebind_emits_a_failed_sel_audit(self):
        # The other post-authorization 409 -- a rebind
        # detected between the lock-key read and the lock acquisition -- must
        # also emit a failed audit before early-returning.
        slot = _slot()
        key = _identity_key(PR_A)
        sel_mock = MagicMock()
        # slot_history_key returns a DIFFERENT value on the second call (inside
        # the lock, after acquisition) than the first (used as the lock key), so
        # authorized_history_key != locked_history_key trips the rebind 409.
        keys = iter(["locked-key", "rebound-key", "rebound-key", "rebound-key"])
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel", new=lambda: sel_mock),
            patch(
                "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: next(keys),
            ),
            patch(
                "kiro_crew.dashboard.chat_handlers._reauthorize_after_await",
                new=MagicMock(return_value=None),
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(_request("s1", key, {"s1": slot}))
        assert resp.status == 409
        assert json.loads(resp.text)["code"] == "session_gone"
        calls = sel_mock.log_tool_invocation.call_args_list
        assert len(calls) == 1
        kwargs = calls[0].kwargs
        assert kwargs["outcome"] == "failed"
        assert kwargs["error"] == "session_gone"
        assert kwargs["metadata"]["phase"] == "lock_rebind"


class TestAlternateHydratorsRestoreDismissals:
    """The dismissed-source-link tombstones must be
    restored on EVERY hydration path, not only the two persistence loaders.

    ``surface_channel_session`` (and ``api_chat_slot_resume``) re-apply metadata
    by hand rather than routing through ``_rehydrate_slot_from_history``. Before
    the fix they skipped ``dismissed_source_links``, so a re-surfaced channel
    session showed a chip the user had unlinked, and the next save -- serializing
    an empty dismissed set -- erased the persisted tombstone for good.
    """

    def test_surface_channel_session_restores_the_dismissed_set(self, tmp_path):
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path)
        key = _identity_key(PR_A)
        session_key = "weixin:kirocrew-research:direct:u1"
        info = {
            "key": "weixin_kirocrew-research_direct_u1",
            "title": "t",
            "modified": 0.0,
        }
        # The transcript mentions PR_A, so absent a restore the chip would derive.
        messages = [
            {"role": "assistant", "content": PR_A, "ts": "2026-09-01T00:00:00+00:00"},
        ]
        meta = {"dismissed_source_links": [key]}

        slot = channel_slots.surface_channel_session(
            state, info, meta, messages, session_key=session_key
        )
        assert slot is not None
        # The tombstone was restored from meta ...
        assert key in slot._dismissed_source_links
        # ... so the derived chip stays suppressed rather than resurrected.
        assert not any(link["url"] == PR_A for link in slot._pr_source_links())


class TestRebindResetsDismissals:
    """A slot's dismissed set is scoped to the transcript it is bound to. When
    the binding changes (a cron/workflow rebind of a live slot), the set belongs
    to the old transcript and must not ride into the new one, or the slot's next
    save persists those tombstones onto the new transcript and suppresses
    unrelated links there.
    """

    def test_hydration_with_no_key_clears_a_stale_set(self):
        # Hydration is authoritative: loading a transcript that records NO
        # dismissals must clear a set left over from a previous binding of a
        # reused slot object, not leave it in place.
        from kiro_crew.dashboard.chat_persistence import _restore_dismissed_source_links

        slot = _slot()
        slot.dismiss_source_link(_identity_key(PR_A))
        assert slot._dismissed_source_links
        _restore_dismissed_source_links(slot, None)  # new transcript has no key
        assert slot._dismissed_source_links == set()

    def test_hydration_restores_the_new_transcripts_dismissals(self):
        from kiro_crew.dashboard.chat_persistence import _restore_dismissed_source_links

        slot = _slot()
        slot.dismiss_source_link(_identity_key(PR_A))
        _restore_dismissed_source_links(slot, [_identity_key(PR_B)])
        assert slot._dismissed_source_links == {_identity_key(PR_B)}  # replaced, not merged

    def test_cron_bind_restores_persisted_dismissals(self):
        # _bind_cron_slot hydrates a cron slot from its transcript. Message
        # hydration carries rows only and get_or_create_slot clears the set on
        # the binding change, so the cron transcript's persisted dismissals must
        # be restored from the OFF-LOOP-prefetched value passed in — otherwise
        # the slot's next full save serializes an empty set and erases them.
        from kiro_crew.dashboard import cron_inject

        slot = _slot()
        state = MagicMock()
        state.get_or_create_slot.return_value = slot
        slot.linked_session_key = ""  # unbound -> triggers hydration branch
        job = MagicMock(id="job42", agent_id="")
        with (
            patch.object(cron_inject, "hydrate_slot_from_history"),
            patch("kiro_crew.dashboard.chat_utils._sync_dashboard_slots"),
            patch.object(cron_inject, "_safe_job_name", return_value="job42"),
        ):
            cron_inject._bind_cron_slot(state, job, [], dismissed=[_identity_key(PR_A)])
        assert slot._dismissed_source_links == {_identity_key(PR_A)}

    def test_cron_bind_defers_dismissed_write_when_metadata_unreadable(self):
        # On an unreadable read (default sentinel) the slot still BINDS to the
        # canonical cron transcript (routing/continuity must not split), but is
        # marked _dismissed_hydrated=False so its full save carries the on-disk
        # dismissed line forward instead of erasing it with an empty set.
        from kiro_crew.dashboard import cron_inject

        slot = _slot()
        state = MagicMock()
        state.get_or_create_slot.return_value = slot
        slot.linked_session_key = ""
        job = MagicMock(id="job42", agent_id="")
        with (
            patch.object(cron_inject, "hydrate_slot_from_history") as hyd,
            patch("kiro_crew.dashboard.chat_utils._sync_dashboard_slots"),
            patch.object(cron_inject, "_safe_job_name", return_value="job42"),
        ):
            cron_inject._bind_cron_slot(state, job, [])  # no dismissed arg -> sentinel
        assert slot.linked_session_key == "cron:job42"  # BOUND (routing preserved)
        hyd.assert_called_once()  # hydration moved with the link
        assert slot._dismissed_hydrated is False  # dismissed WRITE deferred


class TestRejectionsAreAudited:
    """Every rejection of the unlink handler is a failed invocation of a
    permission-class tool and must leave a SEL trail, matching the
    persist-failure and lock-rebind paths.
    """

    @pytest.mark.asyncio
    async def test_invalid_identity_emits_a_failed_sel_audit(self):
        sel_mock = MagicMock()
        with patch("kiro_crew.dashboard.chat_handlers.sel", new=lambda: sel_mock):
            resp = await api_chat_slot_source_link_unlink(
                _request("s1", "not-a-valid-key", {"s1": _slot()})
            )
        assert resp.status == 400
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "failed"
        assert kwargs["error"] == "invalid_source_identity"
        assert kwargs["metadata"]["phase"] == "validate"

    @pytest.mark.asyncio
    async def test_absent_identity_emits_a_failed_sel_audit(self):
        sel_mock = MagicMock()
        # A format-valid but not-derived identity -> source_link_not_found.
        absent = _identity_key("https://github.com/acme/widgets/pull/999")
        with patch("kiro_crew.dashboard.chat_handlers.sel", new=lambda: sel_mock):
            resp = await api_chat_slot_source_link_unlink(_request("s1", absent, {"s1": _slot()}))
        assert resp.status == 404
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "failed"
        assert kwargs["error"] == "source_link_not_found"
        assert kwargs["metadata"]["phase"] == "derive"

    @pytest.mark.asyncio
    async def test_missing_slot_emits_a_failed_sel_audit(self):
        sel_mock = MagicMock()
        with patch("kiro_crew.dashboard.chat_handlers.sel", new=lambda: sel_mock):
            resp = await api_chat_slot_source_link_unlink(
                _request("s1", _identity_key(PR_A), {})  # no such slot
            )
        assert resp.status == 404
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "failed"
        assert kwargs["error"] == "slot_not_found"
        assert kwargs["metadata"]["phase"] == "lookup"

    @pytest.mark.asyncio
    async def test_an_alias_that_joins_during_persist_is_mirrored(self):
        # An alias binding INTO the authorized transcript during the persist
        # await missed the pre-await mirror. After a successful persist the
        # handler must re-scan and mirror the dismissal onto it, or its own full
        # save would serialize a set WITHOUT this identity and overwrite the
        # acknowledged tombstone.
        primary = _slot()
        joiner = _slot()  # will "join" the transcript during the persist
        key = _identity_key(PR_A)
        slots = {"s1": primary}

        def _add_joiner_then_persist(*_a, **_k):
            slots["s2"] = joiner  # binds in mid-await
            return True

        state = MagicMock(_slots=slots)
        _pin_ok(state)
        state.conversation_log.update_metadata_if.side_effect = _add_joiner_then_persist
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "shared-history-key",
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        assert key in primary._dismissed_source_links
        assert key in joiner._dismissed_source_links  # mirrored onto the late joiner

    @pytest.mark.asyncio
    async def test_a_failed_confirm_compensates_the_committed_first_write(self):
        # When a late joiner triggers a CONFIRM write: the first write commits
        # (disk gains the dismissal), then the confirm FAILS. The handler returns
        # 409 and rolls back memory — and must also COMPENSATE disk, writing the
        # post-rollback (pre-request) union back so a restart does not hide a chip
        # for a request that reported failure.
        primary = _slot()
        joiner = _slot()
        key = _identity_key(PR_A)
        slots = {"s1": primary}
        calls: list[list[str]] = []

        def _writes(_hkey, fields, _guard):
            calls.append(fields["dismissed_source_links"])
            if len(calls) == 1:
                slots["s2"] = joiner  # first write commits; a joiner appears
                return True
            if len(calls) == 2:
                return False  # the confirm fails
            return True  # the compensation write lands

        state = MagicMock(_slots=slots)
        _pin_ok(state)
        state.conversation_log.update_metadata_if.side_effect = _writes
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "shared-history-key",
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 409
        assert key not in primary._dismissed_source_links  # memory rolled back
        assert key not in joiner._dismissed_source_links
        # Three writes: first (committed), confirm (failed), compensation. The
        # compensation must NOT contain the rolled-back key.
        assert len(calls) == 3
        assert key not in calls[2]

    @pytest.mark.asyncio
    async def test_failed_compensation_accepts_the_committed_dismissal(self):
        # First write commits (disk gains the dismissal), confirm FAILS, and the
        # compensation write ALSO fails — so disk still carries the dismissal.
        # Reporting 409 would desync (restart hides the chip for a "failed"
        # request); instead the handler accepts the committed state: re-mirror
        # the dismissal and return 200.
        primary = _slot()
        joiner = _slot()
        key = _identity_key(PR_A)
        slots = {"s1": primary}
        n = {"c": 0}

        def _writes(_hkey, _fields, _guard):
            n["c"] += 1
            if n["c"] == 1:
                slots["s2"] = joiner  # first write commits; a joiner appears
                return True
            return False  # confirm fails AND compensation fails

        state = MagicMock(_slots=slots)
        _pin_ok(state)
        state.conversation_log.update_metadata_if.side_effect = _writes
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "shared-history-key",
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200  # committed dismissal accepted, not 409
        assert key in primary._dismissed_source_links  # re-mirrored to match disk
        assert key in joiner._dismissed_source_links

    @pytest.mark.asyncio
    async def test_compensation_preserves_a_departed_aliass_committed_tombstone(self):
        # First write commits this request's key, confirm FAILS, so the handler
        # compensates by restoring the pre-request durable set. Another alias had
        # a UNIQUE, already-committed tombstone (only IT dismissed it) and rebinds
        # AWAY before the compensation runs, so it is NOT among the live aliases.
        # Compensation must NOT rebuild from the remaining in-memory aliases
        # (which would omit and thus ERASE that tombstone) — it reads the
        # AUTHORITATIVE on-disk set and removes only THIS request's key, so the
        # departed alias's committed tombstone survives on disk.
        primary = _slot()
        joiner = _slot()
        key = _identity_key(PR_A)
        other = _identity_key(PR_B)  # a DIFFERENT alias's unique committed tombstone
        slots = {"s1": primary}
        writes = {"c": 0}
        compensate_written = {}

        def _writes(_hkey, fields, _guard):
            writes["c"] += 1
            if writes["c"] == 1:
                slots["s2"] = joiner  # first write commits; a joiner appears -> confirm
                return True
            if writes["c"] == 2:
                return False  # confirm fails -> compensate
            # third call is the compensation write; capture what it persists.
            compensate_written["set"] = set(fields["dismissed_source_links"])
            return True  # compensation succeeds

        # Pin read at request start (call 1): only the departed alias's
        # pre-existing tombstone is on disk — this request INTRODUCES ``key``
        # (durably_dismissed=False). Compensation read (call 2+): after the first
        # write committed, disk carries both ``key`` and ``other``.
        meta_reads = {"c": 0}

        def _meta_status(_hkey):
            meta_reads["c"] += 1
            dismissed = [other] if meta_reads["c"] == 1 else [key, other]
            return (
                {"_type": "metadata", "created_at": "t", "dismissed_source_links": dismissed},
                True,
            )

        state = MagicMock(_slots=slots)
        state.conversation_log.get_metadata_status.side_effect = _meta_status
        state.conversation_log.update_metadata_if.side_effect = _writes
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "shared-history-key",
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 409  # this request failed
        # Compensation restored on-disk MINUS this request's key: the departed
        # alias's unique tombstone is PRESERVED, this request's key is removed.
        assert compensate_written["set"] == {other}

    @pytest.mark.asyncio
    async def test_failed_retry_does_not_erase_a_pre_existing_durable_dismissal(self):
        # ``key`` is ALREADY durably dismissed at request start (a legitimate
        # prior commit). This request re-dismisses it but enters the persist path
        # over a non-durable/stale in-memory read; the first write commits, a
        # joiner appears, the confirm fails, and compensation runs. Compensation
        # must NOT strip ``key`` — this request did not introduce it — or a failed
        # retry would erase a pre-existing, legitimately-committed dismissal.
        primary = _slot()
        joiner = _slot()
        key = _identity_key(PR_A)
        slots = {"s1": primary}
        writes = {"c": 0}
        compensate_written = {}

        def _writes(_hkey, fields, _guard):
            writes["c"] += 1
            if writes["c"] == 1:
                slots["s2"] = joiner  # first write commits; joiner appears -> confirm
                return True
            if writes["c"] == 2:
                return False  # confirm fails -> compensate
            compensate_written["set"] = set(fields["dismissed_source_links"])
            return True

        # ``key`` is on disk from the START (durably_dismissed=True) and stays.
        def _meta_status(_hkey):
            return ({"_type": "metadata", "created_at": "t", "dismissed_source_links": [key]}, True)

        state = MagicMock(_slots=slots)
        state.conversation_log.get_metadata_status.side_effect = _meta_status
        state.conversation_log.update_metadata_if.side_effect = _writes
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "shared-history-key",
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        # Compensation KEEPS the pre-existing dismissal (did not strip key).
        assert compensate_written["set"] == {key}
        # First write commits, confirm + compensation BOTH fail, AND the
        # transcript was deleted + recreated (its created_at changed) before the
        # accept-committed re-check. The committed write went with the OLD
        # transcript and is GONE, so accepting it would mirror a stale dismissal
        # into the REPLACEMENT session. The handler must re-read the identity,
        # see the mismatch, and 409 WITHOUT mirroring onto the replacement.
        primary = _slot()
        joiner = _slot()
        key = _identity_key(PR_A)
        slots = {"s1": primary}
        n = {"c": 0}

        def _writes(_hkey, _fields, _guard):
            n["c"] += 1
            if n["c"] == 1:
                slots["s2"] = joiner  # first write commits; a joiner appears
                return True
            return False  # confirm fails AND compensation fails

        # created_at "t" for the pin read (before mutation), then "recreated" for
        # the accept-committed re-check: the transcript was replaced mid-flight.
        meta_reads = {"c": 0}

        def _meta_status(_hkey):
            meta_reads["c"] += 1
            created = "t" if meta_reads["c"] == 1 else "recreated"
            return ({"_type": "metadata", "created_at": created}, True)

        state = MagicMock(_slots=slots)
        state.conversation_log.get_metadata_status.side_effect = _meta_status
        state.conversation_log.update_metadata_if.side_effect = _writes
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "shared-history-key",
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 409  # recreated transcript: committed write is gone
        # The replacement session was NOT contaminated with the stale dismissal.
        assert key not in primary._dismissed_source_links
        assert key not in joiner._dismissed_source_links

    @pytest.mark.asyncio
    async def test_a_tentative_in_memory_dismissal_is_not_acknowledged_off_disk(self):
        # The key is in the slot's IN-MEMORY set but NOT on disk — the state a
        # concurrent unlink on a since-rebound slot leaves behind before its own
        # guarded write commits (or rolls back). A DELETE arriving now must NOT
        # fast-return 200 off that uncommitted presence; it must persist the
        # dismissal authoritatively under its own guard.
        slot = _slot()
        key = _identity_key(PR_A)
        slot.dismiss_source_link(key)  # in memory only (tentative), NOT on disk
        state = MagicMock(_slots={"s1": slot})
        _pin_ok(state)  # on-disk metadata has NO dismissed_source_links
        state.conversation_log.update_metadata_if.return_value = True
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        # It PERSISTED rather than fast-returning: a guarded write landed.
        state.conversation_log.update_metadata_if.assert_called()
        written = state.conversation_log.update_metadata_if.call_args.args[1]
        assert key in written["dismissed_source_links"]

    @pytest.mark.asyncio
    async def test_post_lock_stale_reauth_emits_a_failed_sel_audit(self):
        # A DELETE that waited on the transaction lock can find its slot replaced
        # by the time it acquires it; _reauthorize_after_await returns a stale
        # response. That rejection must also leave a SEL trail (phase=reauth).
        slot = _slot()
        key = _identity_key(PR_A)
        sel_mock = MagicMock()
        stale_resp = web.json_response({"code": "session_gone"}, status=409)
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel", new=lambda: sel_mock),
            patch(
                "kiro_crew.dashboard.chat_handlers._reauthorize_after_await",
                new=MagicMock(return_value=stale_resp),
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(_request("s1", key, {"s1": slot}))
        assert resp.status == 409
        # The reauth rejection is the LAST audited event on this path.
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "failed"
        assert kwargs["metadata"]["phase"] == "reauth"
