"""Tests for unlinking a PR/issue/Jira source-link chip from a chat session.

The chips are DERIVED by re-scanning the transcript, so a naive delete is undone
by the next re-scan. Unlinking instead records the link's serialized identity in
a per-slot dismissed set that the derivation filters against, persists it so a
restart cannot resurrect the chip, and touches no remote provider.

This module pins the derivation filter, the dismissed set's persistence and the unlink
endpoint. The dismissed set across hydration and rebind lives in
``test_dashboard_source_link_unlink_hydration.py``, the audited rejections in
``test_dashboard_source_link_unlink_audit.py``, and the binding rechecks across the
handler's awaits with what it publishes in ``test_dashboard_source_link_unlink_publication.py``;
the shared links, slots and request builders in ``dashboard_source_link_unlink_helpers.py``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from dashboard_source_link_unlink_helpers import (
    ISSUE_A,
    PR_A,
    PR_B,
    PR_C,
    _expect_for_req,
    _identity_key,
    _pin_ok,
    _request,
    _slot,
)

from kiro_crew.dashboard.chat_handlers import api_chat_slot_source_link_unlink
from kiro_crew.dashboard.source_providers.contract import is_valid_source_identity_key


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

    def test_restore_caps_and_reports_overflow(self, caplog):
        """The restore path bounds retention to the same ceiling as the write
        side, and — per the ``a-bound-bounds-every-field-it-retains`` contract —
        SAYS the drop out loud: an oversized on-disk line is capped in memory and
        logs one warning naming how many valid keys were dropped, so a silent
        discard can never hide that the restored set is incomplete."""
        import logging

        from kiro_crew.dashboard.chat_persistence import _restore_dismissed_source_links
        from kiro_crew.dashboard.state import _MAX_DISMISSED_SOURCE_LINKS

        overflow = 7
        keys = [
            _identity_key(f"https://github.com/acme/widgets/pull/{i}")
            for i in range(_MAX_DISMISSED_SOURCE_LINKS + overflow)
        ]
        slot = _slot()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_persistence"):
            _restore_dismissed_source_links(slot, keys)
        assert len(slot._dismissed_source_links) == _MAX_DISMISSED_SOURCE_LINKS
        # Exactly one warning, naming the dropped count and the cap.
        warnings = [r.getMessage() for r in caplog.records if "truncated" in r.getMessage()]
        assert len(warnings) == 1
        assert str(overflow) in warnings[0]

    def test_save_side_cap_bounds_an_oversized_dismissed_line(self):
        """A bound is applied at the point the field is RETAINED, not only on
        restore: the save/merge paths run every write through
        ``_capped_dismissed_line``, so an oversized on-disk line (tampered, or
        grown by an older build) cannot round-trip an unbounded set back to
        disk. The helper drops invalid keys, sorts, then keeps only the first
        cap entries."""
        from kiro_crew.dashboard.chat_persistence import _capped_dismissed_line
        from kiro_crew.dashboard.state import _MAX_DISMISSED_SOURCE_LINKS

        # Genuinely valid, distinct identity keys past the ceiling.
        valid = [
            _identity_key(f"https://github.com/acme/widgets/pull/{i}")
            for i in range(_MAX_DISMISSED_SOURCE_LINKS + 50)
        ]
        capped = _capped_dismissed_line(valid)
        assert len(capped) == _MAX_DISMISSED_SOURCE_LINKS
        # Deterministic: the sorted prefix, so the same over-limit line always
        # truncates to the same retained set.
        assert capped == sorted(valid)[:_MAX_DISMISSED_SOURCE_LINKS]

    def test_save_side_cap_drops_invalid_keys(self):
        """The carry-forward callers pass the raw on-disk list, so
        ``_capped_dismissed_line`` must re-validate each key against the identity
        grammar — a tampered/oversized string is dropped, never re-written."""
        from kiro_crew.dashboard.chat_persistence import _capped_dismissed_line

        good = _identity_key(PR_A)
        result = _capped_dismissed_line([good, "not-json", "{}", ['{"nested":[1]}'], "x" * 100000])
        assert result == [good]

    def test_restore_caps_an_oversized_line_during_iteration(self):
        """The restore path bounds the dismissed set DURING iteration, not by
        materializing the whole (possibly oversized/tampered) valid-key list and
        slicing — so a hostile on-disk line cannot force an unbounded list into
        memory before the cap applies. The in-memory set is exactly the ceiling."""
        from kiro_crew.dashboard.chat_persistence import _restore_dismissed_source_links
        from kiro_crew.dashboard.state import _MAX_DISMISSED_SOURCE_LINKS

        oversized = [
            _identity_key(f"https://github.com/acme/widgets/pull/{i}")
            for i in range(_MAX_DISMISSED_SOURCE_LINKS + 200)
        ]
        slot = _slot()
        _restore_dismissed_source_links(slot, oversized)
        assert len(slot._dismissed_source_links) == _MAX_DISMISSED_SOURCE_LINKS
        assert slot._dismissed_hydrated is True
        # Every retained key is a real, valid key from the input (never junk).
        assert slot._dismissed_source_links <= set(oversized)

    def test_bounded_valid_identities_caps_during_iteration_and_drops_junk(self):
        """The shared collector every on-disk reader uses stops at the cap during
        iteration (so an oversized/tampered list never materializes in full) and
        drops invalid keys. A non-list yields the empty set."""
        from kiro_crew.dashboard.source_providers.contract import bounded_valid_identities

        good = [_identity_key(f"https://github.com/acme/widgets/pull/{i}") for i in range(50)]
        # Oversized valid input caps at the requested bound.
        assert len(bounded_valid_identities(good, 10)) == 10
        assert bounded_valid_identities(good, 10) <= set(good)
        # Junk is dropped; only the one valid key survives.
        assert bounded_valid_identities([good[0], "not-json", "{}", "x" * 90000], 512) == {good[0]}
        # A non-list records nothing.
        assert bounded_valid_identities(None, 512) == set()
        assert bounded_valid_identities("a string", 512) == set()

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


async def _delete(
    slot_key: str,
    identity: str,
    slots: dict,
    *,
    app: str = "",
    expect: str | None = None,
    omit_expect: bool = False,
) -> web.Response:
    with patch("kiro_crew.dashboard.chat_handlers.sel"):
        return await api_chat_slot_source_link_unlink(
            _request(slot_key, identity, slots, app=app, expect=expect, omit_expect=omit_expect)
        )


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
        body = json.loads(resp.text)
        assert body["ok"] is True and body["dismissed"] is True
        # The DELETE returns the AUTHORITATIVE post-unlink total so the client
        # assigns it rather than decrementing locally (a local ``- 1`` double-
        # counts on an idempotent retry after a peer tab already reduced it). It
        # equals the projection's count after the dismissal is applied.
        assert body["source_links_total"] == len(slot._summary_source_links())
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

    @pytest.mark.asyncio
    async def test_matching_expect_identity_proceeds(self):
        """When the client sends the session identity it targeted and the slot
        still carries it, the unlink proceeds normally."""
        slot = _slot()
        state_slots = {"s1": slot}
        key = _identity_key(PR_A)
        # Local slot: resolved_row_identity is its key; created_at is the stamp;
        # linked_session_key is empty (unbound). The gate mirrors this triple.
        expect = f"{slot.key}|{slot.created_at}|"
        resp = await _delete("s1", key, state_slots, expect=expect)
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["ok"] is True and body["dismissed"] is True
        assert body["source_links_total"] == len(slot._summary_source_links())
        assert key in slot._dismissed_source_links

    @pytest.mark.asyncio
    async def test_absent_expect_is_refused(self):
        """``expect`` is REQUIRED: without it the identity gate is skipped and a
        same-key recreation finishing before the entry lookup would let a
        dismissal land on the wrong session permanently (no un-dismiss route).
        The endpoint is new and its only caller always sends ``expect``, so an
        absent one is refused 400 before any mutation."""
        slot = _slot()
        state_slots = {"s1": slot}
        key = _identity_key(PR_A)
        resp = await _delete("s1", key, state_slots, omit_expect=True)  # no expect
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "expected_identity_required"
        assert key not in slot._dismissed_source_links

    @pytest.mark.asyncio
    async def test_mismatched_expect_identity_is_rejected_before_mutation(self):
        """When the client's expected identity differs from the identity the
        slot behind the key currently carries (a same-key recreation stands in
        its place), the DELETE is rejected 409 at the entry lookup and nothing
        is dismissed."""
        slot = _slot()
        state_slots = {"s1": slot}
        key = _identity_key(PR_A)
        # The session the client meant to unlink from is gone; the slot now
        # carries a DIFFERENT identity (fresh created_at on recreation).
        expect = f"{slot.key}|a-different-created-stamp"
        resp = await _delete("s1", key, state_slots, expect=expect)
        assert resp.status == 409
        assert json.loads(resp.text)["code"] == "session_gone"
        # Rejected before any mutation: the link is NOT dismissed.
        assert key not in slot._dismissed_source_links

    @pytest.mark.asyncio
    async def test_rebound_transcript_is_rejected_even_when_identity_and_stamp_match(self):
        """A live slot can be REBOUND to a different transcript (a cron/workflow
        injector assigning ``linked_session_key`` on an already-existing slot)
        without changing ``row_identity`` or ``created_at``. The client staged
        its confirm against the OLD binding, so the ``expect`` triple carries the
        old (empty) ``linked_session_key``; the slot now carries a new one. The
        gate must reject 409 rather than land the dismissal on the replacement
        transcript the user never chose. Without the transcript binding in the
        triple the row_identity+created_at would match and this would wrongly
        pass — this is the GPT F1 rebind finding."""
        slot = _slot()
        state_slots = {"s1": slot}
        key = _identity_key(PR_A)
        # Client staged against the unbound slot (linked_session_key == "").
        expect = f"{slot.key}|{slot.created_at}|"
        # A rebind lands before the confirm: same key, same created_at, same
        # row_identity — only the transcript binding changed.
        slot.linked_session_key = "dashboard:some-other-transcript"
        resp = await _delete("s1", key, state_slots, expect=expect)
        assert resp.status == 409
        assert json.loads(resp.text)["code"] == "session_gone"
        assert key not in slot._dismissed_source_links

    @pytest.mark.asyncio
    async def test_dismiss_at_cap_is_refused_without_an_oversized_write(self):
        """When the slot's dismissed set is already at the ceiling, unlinking a
        NEW derived link is refused 409 dismissed_source_links_full — never
        persisted as a ceiling+1 line that restore would tail-truncate,
        resurrecting whichever chip falls off. The cap gates the union write, not
        only the in-memory add, so entering the persist path on ``not
        durably_dismissed`` cannot commit an oversized set."""
        from kiro_crew.dashboard.state import _MAX_DISMISSED_SOURCE_LINKS

        slot = _slot()
        # Fill the in-memory dismissed set to the ceiling with synthetic keys the
        # transcript never derived, and mark it hydrated so the handler reads a
        # full set rather than treating it as an empty stand-in.
        slot._dismissed_source_links = {
            f'["synthetic","{i}"]' for i in range(_MAX_DISMISSED_SOURCE_LINKS)
        }
        slot._dismissed_hydrated = True
        key = _identity_key(PR_A)  # a genuinely new, derived link
        req = _request("s1", key, {"s1": slot})
        um = req.app["state"].conversation_log.update_metadata_if
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 409
        assert json.loads(resp.text)["code"] == "dismissed_source_links_full"
        # The cap add-site refused it, so it is not in the set...
        assert key not in slot._dismissed_source_links
        # ...and crucially NO oversized union was ever written to disk.
        assert um.call_count == 0

    @pytest.mark.asyncio
    async def test_write_recomputes_the_union_against_the_locked_line(self):
        """The write payload is re-folded against the LOCKED on-disk metadata,
        so a concurrent gateway's dismissal committed in the window between this
        request's pre-lock read and its guarded write is preserved (grow-only),
        not erased by a last-writer-wins replacement. ``update_metadata_if`` runs
        the guard while holding the cross-process lock, with the freshly-read
        meta; the guard rewrites the payload before it is applied."""
        slot = _slot()
        key = _identity_key(PR_A)
        sibling = _identity_key(PR_B)  # committed by another gateway mid-write
        state = MagicMock(_slots={"s1": slot})
        _pin_ok(state)

        # When the handler calls update_metadata_if, run the passed guard against
        # a locked meta that already carries the sibling's just-committed key,
        # exactly as a concurrent gateway would have left it on disk.
        def _run_guard(_hkey, fields, guard):
            locked_meta = {
                "_type": "metadata",
                "created_at": "t",
                "dismissed_source_links": [sibling],
            }
            return guard(locked_meta)

        state.conversation_log.update_metadata_if.side_effect = _run_guard
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        # The payload the guard rewrote in place carries BOTH this request's key
        # and the sibling's concurrently-committed dismissal — the union, not a
        # replacement that would have dropped the sibling.
        written = state.conversation_log.update_metadata_if.call_args.args[1]
        assert set(written["dismissed_source_links"]) == {key, sibling}

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
                "kiro_crew.dashboard.source_providers.contract.is_valid_source_identity_key",
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
        await _delete(
            "s1", _identity_key(PR_A), {"s1": slot}, expect=f"{slot.key}|{slot.created_at}|"
        )
        # The dismissal advances the derivation revision so the cache is rebuilt
        # and the chip disappears. The unlink transaction now invalidates more
        # than once (marking the key tentative-pending before the guarded write,
        # then clearing that fence on commit), so assert the revision ADVANCED
        # rather than a brittle exact +1 -- the contract is "cache invalidated,
        # chip gone", not the number of invalidations.
        assert slot._source_links_revision > rev_before
        assert not any(link["url"] == PR_A for link in slot._pr_source_links())

    @pytest.mark.asyncio
    async def test_malformed_identity_is_400_with_a_code(self):
        slot = _slot()
        resp = await _delete(
            "s1", "not-a-real-key", {"s1": slot}, expect=f"{slot.key}|{slot.created_at}|"
        )
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
        resp = await _delete(
            "s1", _identity_key(PR_B), {"s1": slot}, expect=f"{slot.key}|{slot.created_at}|"
        )
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
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
    async def test_write_carries_committed_dismissals_not_tentative_alias_keys(self):
        # ``dismissed_source_links`` lives on the SHARED transcript line every
        # alias on the history key points at, so ONE update_metadata write covers
        # all aliases. The write set is this request's identity UNION the
        # transcript's ON-DISK committed line — NOT the raw in-memory sets of the
        # live aliases. So a sibling's COMMITTED dismissal (kb, already on disk)
        # is carried forward, while a TENTATIVE key a rebound alias still carries
        # in memory but that is NOT on disk (kc — a concurrent unlink's
        # uncommitted tombstone) is NOT leaked onto this transcript.
        primary = _slot(PR_A, PR_B)
        sibling = _slot(PR_A, PR_B)
        kb = _identity_key(PR_B)  # committed on disk
        kc = _identity_key(PR_C)  # foreign, tentative, in a rebound alias's memory only
        ka = _identity_key(PR_A)
        # The sibling arrives carrying BOTH a committed (kb) and a tentative
        # foreign (kc) key in memory; only kb is on the durable line.
        sibling.dismiss_source_link(kb)
        sibling.dismiss_source_link(kc)
        state = MagicMock(_slots={"s1": primary, "s2": sibling})
        # Pin read + fold-in read both return the committed on-disk line = [kb].
        state.conversation_log.get_metadata_status.return_value = (
            {"_type": "metadata", "created_at": "t", "dismissed_source_links": [kb]},
            True,
        )
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": ka}
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.source_providers.contract.is_valid_source_identity_key",
                return_value=True,
            ),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "shared-history-key",
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        assert ka in primary._dismissed_source_links
        um = state.conversation_log.update_metadata_if
        assert um.call_count == 1  # ONE write covers both aliases
        _hkey, fields, _guard = um.call_args.args
        written = set(fields["dismissed_source_links"])
        assert ka in written  # this request's own dismissal
        assert kb in written  # the sibling's COMMITTED dismissal (from the on-disk fold)
        assert kc not in written  # the tentative foreign key is NOT leaked onto this transcript

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
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
    async def test_a_failed_confirm_after_a_committed_first_write_never_publishes_a_restore(self):
        # persist-before-publish: when the FIRST write commits but a slow CONFIRM
        # write then FAILS, the committed dismissal is durably on disk. The
        # rollback path must NOT publish a chip-RESTORED frame before the
        # accept-committed path re-asserts it — that would flicker a chip disk
        # never un-dismissed. Every broadcast fired after the first write commits
        # must therefore show the chip GONE; the request accepts-committed (200).
        primary = _slot()
        joiner = _slot()  # a late joiner forces the confirm write to run
        key = _identity_key(PR_A)
        hkeys = {id(primary): "authorized", id(joiner): "other-transcript"}
        writes = {"n": 0}

        def _on_write(*_a, **_k):
            writes["n"] += 1
            if writes["n"] == 1:
                hkeys[id(joiner)] = "authorized"  # joiner binds in -> confirm follows
                return True  # FIRST write commits (first_committed = True)
            return False  # CONFIRM write FAILS

        state = MagicMock(_slots={"s1": primary, "s2": joiner})
        # Readable, same pinned created_at -> accept-committed re-verify passes.
        state.conversation_log.get_metadata_status.return_value = (
            {"_type": "metadata", "created_at": "t"},
            True,
        )
        state.conversation_log.update_metadata_if.side_effect = _on_write
        pushes_show_chip: list[bool] = []

        def record_push():
            pushes_show_chip.append(any(link["url"] == PR_A for link in primary._pr_source_links()))

        state.push_slots_update.side_effect = record_push
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
        # Committed first write is accepted as terminal (200), chip stays gone.
        assert resp.status == 200
        assert key in primary._dismissed_source_links
        assert not any(link["url"] == PR_A for link in primary._pr_source_links())
        # No published frame ever restored the chip: the failed-confirm rollback
        # is suppressed because the first write committed.
        assert pushes_show_chip and not any(pushes_show_chip)

    @pytest.mark.asyncio
    async def test_broadcast_failure_after_persist_keeps_the_dismissal(self):
        # Persist-before-publish: the durable write lands FIRST, then the
        # broadcast announces the removal. A broadcast that RAISES now happens
        # AFTER the guarded write committed, so the dismissal is authoritatively
        # on disk. The non-aborting publish swallows the delivery error (flagging
        # a later retry) instead of rolling the committed dismissal back or
        # turning a durable success into a raised failure. Memory keeps the key
        # because disk already records it.
        primary = _slot()
        key = _identity_key(PR_A)
        state = MagicMock(_slots={"s1": primary})
        _pin_ok(state)
        state.conversation_log.get_metadata_status.side_effect = lambda _h: (
            {"_type": "metadata", "created_at": "t", "dismissed_source_links": []},
            True,
        )
        state.conversation_log.update_metadata_if.side_effect = lambda _h, _f, _g: True
        state.push_slots_update.side_effect = RuntimeError("unserializable slot")
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": key}
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "authorized",
            ),
        ):
            # No RuntimeError propagates: the persist committed and the
            # post-persist broadcast is non-aborting.
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        # The dismissal stays — disk durably recorded it before the broadcast.
        assert key in primary._dismissed_source_links

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
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
        body = json.loads(resp.text)
        assert body["ok"] is True and body["dismissed"] is True
        # Even on an idempotent repeat the DELETE returns the authoritative total
        # (the count after the already-applied dismissal), so the client assigns
        # it rather than decrementing a second time and understating the overflow.
        assert body["source_links_total"] == len(slot._summary_source_links())
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
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
