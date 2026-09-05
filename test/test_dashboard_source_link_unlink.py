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


class TestIdentityKeyValidation:
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


def _request(slot_key: str, identity: str, slots: dict, *, app: str = ""):
    request = MagicMock(spec=web.Request)
    request.method = "DELETE"
    request.match_info = {"slot": slot_key, "identity": identity}
    request.get = lambda key, default=None: app if key == "app" else default
    request.app = {"state": MagicMock(_slots=slots)}
    return request


async def _delete(slot_key: str, identity: str, slots: dict, *, app: str = "") -> web.Response:
    with (
        patch("kiro_crew.dashboard.chat_handlers.sel"),
        patch(
            "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
            new=AsyncMock(return_value=True),
        ),
    ):
        return await api_chat_slot_source_link_unlink(_request(slot_key, identity, slots, app=app))


class TestUnlinkEndpoint:
    @pytest.mark.asyncio
    async def test_delete_records_the_dismissal_and_broadcasts(self):
        slot = _slot()
        state_slots = {"s1": slot}
        key = _identity_key(PR_A)
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
                new=AsyncMock(return_value=True),
            ) as saver,
        ):
            req = _request("s1", key, state_slots)
            resp = await api_chat_slot_source_link_unlink(req)

        assert resp.status == 200
        assert json.loads(resp.text) == {"ok": True, "dismissed": True}
        assert key in slot._dismissed_source_links
        # The chip disappears immediately (slots push) and is persisted.
        req.app["state"].push_slots_update.assert_called_once()
        saver.assert_awaited_once()
        # Regression guard: the dismissal is durable state NOT tracked by
        # ``slot._dirty``, so on a freshly-restored session (no new messages)
        # the plain save path takes its resumed-slot no-op skip and the
        # dismissal never reaches disk -- a restart would resurrect the chip.
        # The endpoint must force the write so "stays gone across restart" holds.
        assert saver.await_args.kwargs.get("force") is True

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
    async def test_a_refused_save_returns_409_and_rolls_back(self):
        # A forced save that returns False (routing rebound / session gone) must
        # NOT be acknowledged as success: returning 200 would tell the user a
        # chip is gone that reappears on restart. The in-memory dismissal is
        # rolled back so the acknowledged state matches disk.
        slot = _slot()
        key = _identity_key(PR_A)
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
                new=AsyncMock(return_value=False),
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(_request("s1", key, {"s1": slot}))
        assert resp.status == 409
        assert json.loads(resp.text) == {
            "error": "session was deleted or rebound",
            "code": "session_gone",
        }
        assert key not in slot._dismissed_source_links  # rolled back

    @pytest.mark.asyncio
    async def test_a_refused_save_leaves_no_sibling_mirrored(self):
        # Ordering guard (GPT 5.6): the sibling mirror must happen AFTER the
        # primary save is confirmed durable, never before. If mirroring ran
        # first, a sibling alias's own periodic flush (which runs outside this
        # handler's transaction lock) could land the mirrored dismissal on disk;
        # then a primary save refusal would roll back memory but not those
        # already-written bytes, hiding the link across restart despite the 409.
        # So on a refused save no sibling may carry the dismissal at all.
        primary = _slot()
        sibling = _slot()  # same transcript content
        sibling._dirty = False  # baseline: only a mirror would flip this True
        key = _identity_key(PR_A)
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "shared-history-key",
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(
                _request("s1", key, {"s1": primary, "s2": sibling})
            )
        assert resp.status == 409
        assert key not in primary._dismissed_source_links  # primary rolled back
        assert key not in sibling._dismissed_source_links  # never mirrored
        assert sibling._dirty is False  # sibling not marked for its own flush

    @pytest.mark.asyncio
    async def test_sibling_alias_receives_the_mirrored_dismissal(self):
        # The dismissed set persists into the SHARED transcript metadata, so a
        # sibling slot aliasing the same history key must receive the dismissal
        # too -- otherwise its next ordinary flush writes its stale set back and
        # resurrects the chip. Mirrors api_chat_slot_autocompact's cross-alias fix.
        primary = _slot()
        sibling = _slot()  # same transcript content
        sibling._dirty = False  # baseline: prove the mirror does not set it
        key = _identity_key(PR_A)
        save_mock = AsyncMock(return_value=True)
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
                new=save_mock,
            ),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "shared-history-key",
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(
                _request("s1", key, {"s1": primary, "s2": sibling})
            )
        assert resp.status == 200
        assert key in primary._dismissed_source_links
        assert key in sibling._dismissed_source_links  # mirrored
        # Two saves: the primary durable commit, THEN a second pinned save that
        # persists the mirrored sibling so a stale alias flush cannot resurrect
        # the dismissal (GPT: mirror must be durably written, not just _dirty).
        assert save_mock.await_count == 2
        # The mirrored sibling must NOT be dirtied: dirtying it makes it eligible
        # for its OWN flush, which would serialize its entire (possibly stale)
        # metadata -- e.g. a title renamed in memory -- back onto the shared
        # transcript. The pinned mirror save above already persists the
        # dismissal, so the dirty mark is unnecessary and unsafe.
        assert sibling._dirty is False

    @pytest.mark.asyncio
    async def test_a_failed_mirror_save_keeps_the_durable_primary(self):
        # GPT 5.6 (heads 7a8a15556 → 06dbd3811): the mirror save is
        # best_effort=False, but if it FAILS the PRIMARY commit already durably
        # wrote the dismissal into the SHARED transcript metadata — and the
        # sibling aliases point at that same transcript. So a sibling still bound
        # to the authorized key is CONSISTENT with disk and must KEEP the
        # tombstone (discarding it would let its flush erase the durable
        # tombstone and resurrect the chip); it is re-armed (_dirty) so a racing
        # flush is corrected. The request still 200s (the user's unlink is
        # durable). Only a rebound alias would be cleared.
        primary = _slot()
        sibling = _slot()  # same transcript content, stays on the authorized key
        sibling._dirty = False
        key = _identity_key(PR_A)
        # Primary save succeeds; the mirror save (2nd call) fails.
        save_mock = AsyncMock(side_effect=[True, False])
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", new=save_mock),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "shared-history-key",
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(
                _request("s1", key, {"s1": primary, "s2": sibling})
            )
        assert resp.status == 200  # primary is durable -> acknowledged
        assert key in primary._dismissed_source_links  # kept (matches disk)
        # Sibling on the authorized key is consistent with the durable shared
        # transcript write, so it KEEPS the dismissal and is re-armed for its own
        # flush rather than discarding a tombstone that is already on disk.
        assert key in sibling._dismissed_source_links
        assert sibling._dirty is True
        assert save_mock.await_count == 2  # primary + failed mirror

    @pytest.mark.asyncio
    async def test_the_mirror_never_dirties_the_alias(self):
        # Regression (GPT 5.6, head fc2b13bba): a rename + unlink on a
        # shared-history alias must not revert the rename. Dirtying the sibling
        # during the mirror let its periodic flush write its stale title over
        # the shared transcript. The mirror now mutates ONLY the sibling's
        # dismissed set and relies on the pinned mirror save; the sibling is
        # never marked _dirty, so its own flush is not provoked.
        primary = _slot()
        sibling = _slot()  # same transcript content
        sibling._dirty = False
        # A field the sibling holds stale in memory (an un-flushed rename). If
        # the mirror dirtied the sibling, its flush would persist THIS.
        sibling.title = "renamed-not-yet-flushed"
        key = _identity_key(PR_A)
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "shared-history-key",
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(
                _request("s1", key, {"s1": primary, "s2": sibling})
            )
        assert resp.status == 200
        assert key in sibling._dismissed_source_links  # dismissal still mirrored
        assert sibling._dirty is False  # but the alias is NOT queued for a flush

    @pytest.mark.asyncio
    async def test_rebind_after_primary_save_mirrors_siblings_still_on_the_key(self):
        # Post-save reauthorization guard: the primary save is an await, so
        # routing can rebind the REQUESTING slot to a foreign transcript while
        # suspended. The primary commit already landed durably against the OLD
        # (authorized) transcript, so the request still 200s. Two things must
        # then happen (GPT 5.6, heads fc2b13bba + 342f2abab):
        #   • the rebound requesting slot must DROP the dismissal (it is now on a
        #     foreign key; keeping it would leak the suppression on its next save)
        #   • sibling aliases STILL bound to the authorized key MUST receive the
        #     dismissal + a confirm-save through one of them, or their next flush
        #     overwrites the durable tombstone and resurrects the chip. The
        #     requesting slot can no longer save that transcript, so the save must
        #     go through a still-bound sibling.
        primary = _slot()
        sibling = _slot()  # a live alias that stays on the authorized transcript
        sibling._dirty = False
        key = _identity_key(PR_A)
        # The rebind is signalled purely by the post-save reauth returning stale
        # (the `stale_after is None` short-circuits before the key comparison),
        # and the rebound requesting slot is excluded from the sibling mirror by
        # object identity (`other is not slot`), not by key -- so all slots can
        # report the one authorized key here.
        authorized = "shared-history-key"

        def _hk(s):
            return authorized

        # None pre-mutation (proceed); stale 409 on the post-save reauth (rebind).
        reauth = MagicMock(
            side_effect=[None, web.json_response({"code": "session_gone"}, status=409)]
        )
        save_mock = AsyncMock(return_value=True)
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", new=save_mock),
            patch("kiro_crew.dashboard.chat_handlers.slot_history_key", new=_hk),
            patch("kiro_crew.dashboard.chat_handlers._reauthorize_after_await", new=reauth),
        ):
            resp = await api_chat_slot_source_link_unlink(
                _request("s1", key, {"s1": primary, "s2": sibling})
            )
        assert resp.status == 200  # primary commit stands
        # Rebound requesting slot: dismissal dropped (no leak into the new key).
        assert key not in primary._dismissed_source_links
        # Sibling still on the authorized key: MUST carry the dismissal so its
        # next flush cannot overwrite the durable tombstone.
        assert key in sibling._dismissed_source_links
        assert sibling._dirty is False  # mirrored via confirm-save, not _dirty
        # Two saves: the primary durable commit + the rebind confirm-save through
        # the sibling (both pinned to the authorized key).
        assert save_mock.await_count == 2

    @pytest.mark.asyncio
    async def test_rebind_after_primary_save_emits_no_stray_mirror_but_audits_allowed(self):
        # Companion to the rebind fix: a post-save rebind is still an ALLOWED
        # unlink from the user's perspective (their commit is durable), so the
        # SEL audit records outcome="allowed" -- the request is not a failure.
        # With NO sibling on the authorized key there is nothing to mirror, so
        # only the primary save fires. The distinct failure audits are the two
        # 409 paths below.
        primary = _slot()
        key = _identity_key(PR_A)
        reauth = MagicMock(
            side_effect=[None, web.json_response({"code": "session_gone"}, status=409)]
        )
        sel_mock = MagicMock()
        # Requesting slot rebinds away (reauth stale); no other slot shares the
        # authorized key, so the else branch finds no mirror target.
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel", new=lambda: sel_mock),
            patch(
                "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: "shared-history-key",
            ),
            patch(
                "kiro_crew.dashboard.chat_handlers._reauthorize_after_await",
                new=reauth,
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(_request("s1", key, {"s1": primary}))
        assert resp.status == 200
        outcomes = [c.kwargs.get("outcome") for c in sel_mock.log_tool_invocation.call_args_list]
        assert outcomes == ["allowed"]

    @pytest.mark.asyncio
    async def test_two_sequential_unlinks_on_one_transcript_both_persist(self):
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
            resp = await api_chat_slot_source_link_unlink(req)

        assert resp.status == 200
        assert json.loads(resp.text) == {"ok": True, "dismissed": True}
        # A no-op repeat neither re-broadcasts nor re-persists.
        req.app["state"].push_slots_update.assert_not_called()
        saver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refused_save_emits_a_failed_sel_audit(self):
        # Finding #3 (GPT 5.6): a post-authorization failure return must still
        # leave an audit trail. The persist-failure 409 path early-returns before
        # the trailing allowed audit, so it must emit its OWN failed event -- an
        # attempted-and-refused unlink cannot vanish from the SEL log.
        slot = _slot()
        key = _identity_key(PR_A)
        sel_mock = MagicMock()
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel", new=lambda: sel_mock),
            patch(
                "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
                new=AsyncMock(return_value=False),
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(_request("s1", key, {"s1": slot}))
        assert resp.status == 409
        calls = sel_mock.log_tool_invocation.call_args_list
        assert len(calls) == 1
        kwargs = calls[0].kwargs
        assert kwargs["tool_name"] == "source_link_unlink"
        assert kwargs["outcome"] == "failed"
        assert kwargs["error"] == "session_gone"
        assert kwargs["metadata"]["phase"] == "primary_persist"

    @pytest.mark.asyncio
    async def test_lock_rebind_emits_a_failed_sel_audit(self):
        # Finding #3 (GPT 5.6): the other post-authorization 409 -- a rebind
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
    """Finding #1 (GPT 5.6): the dismissed-source-link tombstones must be
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
