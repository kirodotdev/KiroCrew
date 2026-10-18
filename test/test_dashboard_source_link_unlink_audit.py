"""Every rejection of the source-link unlink handler leaves a SEL record.

A refusal is a failed invocation of a permission-class tool, so each one is audited
with its phase, matching the persist-failure and lock-rebind paths. The same class
also holds the persist-time alias mirroring, the failed-confirm compensation and the
tentative-dismissal tests filed under it. Shared links, slots and request builders
live in ``dashboard_source_link_unlink_helpers.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from dashboard_source_link_unlink_helpers import (
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
        assert resp.status == 200
        assert key in primary._dismissed_source_links
        assert key in joiner._dismissed_source_links  # mirrored onto the late joiner

    @pytest.mark.asyncio
    async def test_a_failed_confirm_accepts_the_committed_first_write(self):
        # When a late joiner triggers a CONFIRM write: the first write commits
        # (disk gains the dismissal), then the confirm FAILS. A dismissal is a
        # grow-only tombstone, so the committed set is already durable and valid.
        # The handler does NOT compensate it with a strip (a strip could erase
        # another gateway's independently-committed same-key dismissal on a
        # shared data home); it accepts the committed state, re-mirrors the
        # dismissal onto live aliases, and returns 200 — matching disk.
        primary = _slot()
        joiner = _slot()
        key = _identity_key(PR_A)
        slots = {"s1": primary}
        calls: list[list[str]] = []

        def _writes(_hkey, fields, guard):
            calls.append(fields["dismissed_source_links"])
            if len(calls) == 1:
                slots["s2"] = joiner  # first write commits; a joiner appears
                return True
            return False  # the confirm fails -> accept the committed first write

        state = MagicMock(_slots=slots)
        _pin_ok(state)
        state.conversation_log.update_metadata_if.side_effect = _writes
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
        assert resp.status == 200  # committed first write accepted as terminal
        assert key in primary._dismissed_source_links  # re-mirrored to match disk
        assert key in joiner._dismissed_source_links
        # Only two writes: first (committed) + confirm (failed). NO third
        # compensation/strip write — the committed grow-only tombstone stands.
        assert len(calls) == 2

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_compensation_preserves_a_departed_aliass_committed_tombstone(self):
        # First write commits this request's key; the confirm FAILS. Under the
        # terminal-on-first-commit contract the handler does NOT compensate a
        # committed write with a strip (a strip could erase another gateway's
        # independently-committed same-key dismissal on a shared data home).
        # Instead it accepts the committed grow-only tombstone as durable and
        # returns 200. A departed alias's UNIQUE committed tombstone on disk is
        # untouched — nothing is stripped — so it trivially survives.
        primary = _slot()
        joiner = _slot()
        key = _identity_key(PR_A)
        other = _identity_key(PR_B)  # a DIFFERENT alias's unique committed tombstone
        slots = {"s1": primary}
        writes = {"c": 0}

        def _writes(_hkey, fields, guard):
            writes["c"] += 1
            if writes["c"] == 1:
                slots["s2"] = joiner  # first write commits; a joiner appears -> confirm
                # Run the merge guard against the locked line so the first write
                # reflects on-disk state (key + other).
                guard({"_type": "metadata", "created_at": "t", "dismissed_source_links": [other]})
                return True
            return False  # confirm fails -> accept the committed first write (no compensate)

        # Pin read at request start (call 1): only the departed alias's
        # pre-existing tombstone is on disk — this request INTRODUCES ``key``
        # (durably_dismissed=False). Identity re-check read (call 2+): after the
        # first write committed, disk carries both ``key`` and ``other``.
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
        assert resp.status == 200  # committed first write accepted as terminal
        # No compensation/strip write ran: only the first write and the failed
        # confirm. The committed key and the departed alias's tombstone both
        # remain on disk untouched.
        assert writes["c"] == 2

    @pytest.mark.asyncio
    async def test_failed_retry_does_not_erase_a_pre_existing_durable_dismissal(self):
        # ``key`` is ALREADY durably dismissed at request start (a legitimate
        # prior commit). This request re-dismisses it but enters the persist path
        # over a non-durable/stale in-memory read; the first write commits, a
        # joiner appears, the confirm fails. Under terminal-on-first-commit the
        # handler accepts the committed write (no strip), so the pre-existing
        # dismissal is trivially preserved — nothing is ever subtracted — and a
        # failed retry cannot erase a legitimately-committed prior dismissal.
        primary = _slot()
        joiner = _slot()
        key = _identity_key(PR_A)
        slots = {"s1": primary}
        writes = {"c": 0}

        def _writes(_hkey, fields, guard):
            writes["c"] += 1
            if writes["c"] == 1:
                slots["s2"] = joiner  # first write commits; joiner appears -> confirm
                return True
            return False  # confirm fails -> accept the committed write (no strip)

        # ``key`` is on disk from the START (durably_dismissed=True) and stays.
        def _meta_status(_hkey):
            return ({"_type": "metadata", "created_at": "t", "dismissed_source_links": [key]}, True)

        state = MagicMock(_slots=slots)
        state.conversation_log.get_metadata_status.side_effect = _meta_status
        state.conversation_log.update_metadata_if.side_effect = _writes
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
        # No compensation/strip write ran (only first + failed confirm), so the
        # pre-existing on-disk dismissal is untouched; the committed write is
        # accepted (200) and the key stays dismissed.
        assert resp.status == 200
        assert writes["c"] == 2
        assert key in primary._dismissed_source_links
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
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
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
    async def test_a_foreign_tentative_key_never_authorizes_a_write(self):
        # The rebind hazard: a concurrent unlink on a since-rebound slot leaves a
        # FOREIGN identity (one THIS transcript never mentioned) tentatively in
        # ``_dismissed_source_links``. A DELETE for that key — omitting ``expect``
        # — must NOT be authorized to persist it: the pinned transcript does not
        # carry it (not derived, not raw-mentioned) and it is not durably
        # dismissed, so it 404s and NO write lands. Otherwise it would durably
        # tombstone a link on a transcript that never had it (silent, grow-only,
        # non-self-correcting).
        slot = _slot(PR_A)  # this transcript mentions ONLY PR_A
        foreign = _identity_key(PR_C)  # a link this transcript never mentioned
        slot._dismissed_source_links.add(foreign)  # tentative, from a rebind; NOT on disk
        state = MagicMock(_slots={"s1": slot})
        _pin_ok(state)  # on-disk metadata has NO dismissed_source_links
        state.conversation_log.update_metadata_if.return_value = True
        req = MagicMock(spec=web.Request)
        req.method = "DELETE"
        req.match_info = {"slot": "s1", "identity": foreign}
        req.app = {"state": state}
        req.rel_url.query = {"expect": _expect_for_req(req)}
        req.get = lambda k, d=None: d
        req.app = {"state": state}
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 404  # not authorized against the pinned transcript
        # No durable tombstone was written for the foreign key.
        state.conversation_log.update_metadata_if.assert_not_called()

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
