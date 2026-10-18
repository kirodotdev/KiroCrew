"""Privacy-mode reservations: a mode is held while its row lands, then committed or released.

A reservation takes the durable row, the mark and the transcript header back when
it is released, never loosens a committed mode or another holder's claim, retains
the mode when a clear cannot reach disk, and serialises a modifier that arrives
mid-release; an in-flight commit holds the key restricted without publishing the
mode. The structural pin that publication goes only through the commit primitive,
and the rest of the ``messaging/privacy_mode.py`` contract, are in
``test_messaging_privacy_mode.py``; the shared fixtures are in
``messaging_privacy_mode_helpers.py``.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from messaging_privacy_mode_helpers import _isolate_trackers  # noqa: F401
from messaging_privacy_mode_helpers import audits_fixture  # noqa: F401
from messaging_privacy_mode_helpers import session_map_fixture  # noqa: F401
from messaging_privacy_mode_helpers import _TG_KEY, _land_on_disk, _Recorder, _Sessions

from kiro_crew.messaging import privacy_mode
from kiro_crew.testing.wait import async_wait_until, await_until


class TestReservations:
    """``reserve`` applies strictly ahead of an irreversible step; ``commit`` keeps
    it, ``release`` takes it back -- and only what the reservation itself applied,
    only when no other holder is pending or committed."""

    @staticmethod
    def _seed_header():
        from kiro_crew import history as history_mod
        from kiro_crew.history import ConversationLog

        log = ConversationLog()
        log.init()
        with history_mod.allow_on_loop_persist():
            log.append(_TG_KEY, "user", "a persistent-era turn")
        return log

    @pytest.mark.asyncio
    async def test_release_takes_back_row_mark_and_header_and_audits_it(self, audits, session_map):
        log = self._seed_header()
        sm = session_map()
        res = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await _land_on_disk(sm)
        assert privacy_mode.is_incognito(_TG_KEY) and sm.get_flag(_TG_KEY, "incognito") is True
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "incognito"
        await privacy_mode.release(res, sessions=_Sessions(sm), source="telegram")
        await _land_on_disk(sm)
        assert not privacy_mode.is_incognito(_TG_KEY)
        assert sm.get_flag(_TG_KEY, "incognito") is False
        assert sm.privacy_flagged_entries() == {}, "the row must stop counting against the cap"
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "persistent"
        assert [e["outcome"] for e in audits] == ["allowed", "released"]

    @pytest.mark.asyncio
    async def test_release_restores_the_weaker_mode_the_header_held_before(
        self, audits, session_map
    ):
        """``incognito`` then a reserved ``temporary`` that fails: the header goes
        back to ``incognito``, the incognito flag and mark stay."""
        log = self._seed_header()
        sm = session_map()
        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        res = await privacy_mode.reserve(
            privacy_mode.MODE_TEMPORARY, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "temporary"
        await privacy_mode.release(res, sessions=_Sessions(sm), source="telegram")
        await _land_on_disk(sm)
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "incognito"
        assert privacy_mode.is_incognito(_TG_KEY) and not privacy_mode.is_temporary(_TG_KEY)
        assert sm.privacy_flagged_entries() == {_TG_KEY: ["incognito"]}

    @pytest.mark.asyncio
    async def test_a_session_already_in_the_mode_is_left_alone_by_a_release(
        self, audits, session_map
    ):
        self._seed_header()
        sm = session_map()
        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        res = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await privacy_mode.release(res, sessions=_Sessions(sm), source="telegram")
        assert privacy_mode.is_incognito(_TG_KEY) and sm.get_flag(_TG_KEY, "incognito") is True
        assert [e["outcome"] for e in audits] == [
            "allowed"
        ], "nothing to take back, nothing audited"

    @pytest.mark.asyncio
    async def test_a_second_holder_is_not_loosened_by_the_firsts_failed_step(
        self, audits, session_map
    ):
        """Two modifiers on one thread reserve before either steer lands; the first
        steer fails, the second lands. The mode must survive for the second."""
        self._seed_header()
        sm = session_map()
        first = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        second = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await privacy_mode.release(first, sessions=_Sessions(sm), source="telegram")
        assert privacy_mode.is_incognito(_TG_KEY), "released from under the second holder"
        assert sm.get_flag(_TG_KEY, "incognito") is True
        await privacy_mode.commit(second)
        assert privacy_mode.is_incognito(_TG_KEY) and sm.get_flag(_TG_KEY, "incognito") is True
        assert privacy_mode._pending == {}
        assert [e["outcome"] for e in audits] == ["allowed"]

    @pytest.mark.asyncio
    async def test_when_every_holder_fails_the_last_release_takes_the_mode_back(
        self, audits, session_map
    ):
        self._seed_header()
        sm = session_map()
        first = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        second = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await privacy_mode.release(first, sessions=_Sessions(sm), source="telegram")
        assert privacy_mode.is_incognito(_TG_KEY)
        await privacy_mode.release(second, sessions=_Sessions(sm), source="telegram")
        assert not privacy_mode.is_incognito(_TG_KEY)
        assert sm.get_flag(_TG_KEY, "incognito") is False
        assert privacy_mode._pending == {}
        assert [e["outcome"] for e in audits] == ["allowed", "released"]

    @pytest.mark.asyncio
    async def test_a_committed_reservation_is_never_loosened_by_a_later_release(
        self, audits, session_map
    ):
        self._seed_header()
        sm = session_map()
        first = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        second = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await privacy_mode.commit(first)
        await privacy_mode.release(second, sessions=_Sessions(sm), source="telegram")
        assert privacy_mode.is_incognito(_TG_KEY) and sm.get_flag(_TG_KEY, "incognito") is True
        assert privacy_mode._pending == {}
        assert [e["outcome"] for e in audits] == ["allowed"]

    @pytest.mark.asyncio
    async def test_reserve_refuses_like_apply_mode_and_leaves_no_pending_state(
        self, audits, session_map, monkeypatch
    ):
        sm = session_map()
        monkeypatch.setattr(sm, "set_flag", MagicMock(side_effect=OSError("disk full")))
        with pytest.raises(privacy_mode.PrivacyModeRefused):
            await privacy_mode.reserve(
                privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
            )
        assert privacy_mode._pending == {} and not privacy_mode.is_incognito(_TG_KEY)

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_the_row_is_on_disk_before_apply_mode_returns(self, audits, session_map):
        """The one form awaits the map's write before anything is published -- mark,
        audit, header, hook, notice, return -- so a fresh map reads the row back
        with no flush of the test's own. Mutation: skip the ``aflush`` in ``_land``
        -- the fresh map reads no flag."""
        sm = session_map()
        applied = await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        assert applied is True
        assert session_map().get_flag(_TG_KEY, "incognito") is True, "the row was not durable"

    @pytest.mark.asyncio
    async def test_a_failed_flush_publishes_nothing_and_refuses(
        self, audits, session_map, monkeypatch
    ):
        """The row reached the map's memory but not the disk. Nothing is published:
        no mark (``is_incognito`` False), no header stamp (the transcript header is
        as it was), no mode-on notice -- the user is told the message was NOT
        processed, one ``denied`` record is written and ``PrivacyModeRefused``
        surfaces to the caller. The flag is back out of the map's memory too, so
        a later hydrate cannot resurrect a row that never landed. Mutation: mark
        before ``_land`` (the shape this replaces) -- red on ``is_incognito``, on
        the header, and on the trail (``allowed`` ahead of ``denied``)."""
        log = self._seed_header()
        sm = session_map()
        monkeypatch.setattr(sm, "aflush", AsyncMock(side_effect=OSError("disk full")))
        rec = _Recorder()
        with pytest.raises(privacy_mode.PrivacyModeRefused) as raised:
            await privacy_mode.apply_mode(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                sessions=_Sessions(sm),
                notify=rec.notify,
                on_applied=rec.on_applied,
            )
        assert raised.value.reason == privacy_mode.REFUSAL_PERSIST_FAILED
        assert privacy_mode.is_incognito(_TG_KEY) is False, "a mark was published with no row"
        assert sm.get_flag(_TG_KEY, "incognito") is False
        assert "memory_mode" not in log.get_metadata(_TG_KEY), "the header was stamped"
        assert rec.hooks == []
        assert rec.notices == [privacy_mode.refusal_notice("incognito", "persist_failed")]
        assert [e["outcome"] for e in audits] == ["denied"], "the trail claims an application"
        assert privacy_mode._pending == {}

    @pytest.mark.asyncio
    async def test_a_release_whose_clear_cannot_reach_disk_retains_the_mode(
        self, audits, session_map, monkeypatch
    ):
        """The mirror of the strict apply: a release publishes -- drops the mark,
        reports ``released`` -- only after the flag's clear is on disk. When that
        write fails the mode is RETAINED, fail-closed toward private: the flag is
        back in the map, the mark never left, the header -- loosened a step
        earlier, since the row is cleared LAST -- is re-stamped with the mode, and
        one ``retained`` record reports the failure instead of ``released``; a
        restart hydrates the mode from the row. Mutation: drop the mark and report
        before the flush -- the mark is gone with the clear still owed (red:
        ``is_incognito`` False, trail ``released``); drop the re-stamp -- red, the
        header says ``persistent`` over a row and a mark that say the mode."""
        log = self._seed_header()
        sm = session_map()
        res = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        assert session_map().get_flag(_TG_KEY, "incognito") is True, "premise: the row is on disk"
        monkeypatch.setattr(sm, "aflush", AsyncMock(side_effect=OSError("disk full")))
        released = await privacy_mode.release(res, sessions=_Sessions(sm), source="telegram")
        assert privacy_mode.is_incognito(_TG_KEY), "the mark was dropped with the clear still owed"
        assert released is False
        assert sm.get_flag(_TG_KEY, "incognito") is True
        assert (
            log.get_metadata(_TG_KEY).get("memory_mode") == "incognito"
        ), "the header loosened ahead of the failed clear was not re-stamped"
        assert [e["outcome"] for e in audits] == ["allowed", "retained"]
        assert audits[-1]["resources"] == f"release_failed:persist_failed:{_TG_KEY}"
        assert privacy_mode._pending == {}
        privacy_mode.reset()
        privacy_mode.hydrate(_Sessions(session_map()), _TG_KEY)
        assert privacy_mode.is_incognito(_TG_KEY), "a restart did not hydrate the retained mode"

    async def _reserve_then_release_with(self, session_map, monkeypatch, audits, writer):
        """Reserve incognito (row, header and mark all standing), then release with
        the header restore replaced by *writer*; hand back the release's verdict
        and the state the assertions read."""
        from kiro_crew.history import ConversationLog

        log = self._seed_header()
        sm = session_map()
        res = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await _land_on_disk(sm)
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "incognito", "premise: stamped"
        assert sm.get_flag(_TG_KEY, "incognito") is True, "premise: the row is on disk"
        monkeypatch.setattr(ConversationLog, "update_metadata_if", writer)
        released = await privacy_mode.release(res, sessions=_Sessions(sm), source="telegram")
        await _land_on_disk(sm)
        return log, sm, released

    def _assert_retained(self, log, sm, released, audits, session_map, *, restore_was):
        trail = [e["outcome"] for e in audits]
        row = sm.get_flag(_TG_KEY, "incognito")
        assert (
            released is False and privacy_mode.is_incognito(_TG_KEY) and row is True
        ) and trail == ["allowed", "retained"], (
            f"a header restore that did not land was published as released ({restore_was}): "
            f"released={released}, marked={privacy_mode.is_incognito(_TG_KEY)}, row={row}, "
            f"header={log.get_metadata(_TG_KEY).get('memory_mode')!r}, trail={trail}"
        )
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "incognito"
        assert session_map().get_flag(_TG_KEY, "incognito") is True, "the row was not put back"
        assert audits[-1]["resources"] == f"release_failed:persist_failed:{_TG_KEY}"
        assert privacy_mode._pending == {}

    @pytest.mark.asyncio
    async def test_a_release_whose_header_restore_fails_retains_the_mode(
        self, audits, session_map, monkeypatch
    ):
        """The header restore is the second half of the durable release, not a
        best-effort tail: when it RAISES, the release publishes nothing -- the
        row is put back, the mark never leaves, one ``retained`` record reports
        it -- so the session does not read released to the channel gate while
        every header-only reader still reads private. Mutation: swallow the
        restore's failure and go on to drop the mark and report ``released``
        (the r30 shape) -- red.
        """
        log, sm, released = await self._reserve_then_release_with(
            session_map, monkeypatch, audits, MagicMock(side_effect=OSError("read-only"))
        )
        self._assert_retained(
            log, sm, released, audits, session_map, restore_was="the restore raised"
        )

    @pytest.mark.asyncio
    async def test_a_release_whose_header_restore_does_not_land_retains_the_mode(
        self, audits, session_map, monkeypatch
    ):
        """The restore's writer answers ``True`` and writes nothing (its skip of a
        first line that is not the metadata record). The header still says the
        released mode while the restore's target was ``persistent``, so the
        read-back refuses the release exactly as a raise does. Mutation: trust
        the writer's answer -- red, the same shape.
        """
        log, sm, released = await self._reserve_then_release_with(
            session_map, monkeypatch, audits, MagicMock(return_value=True)
        )
        self._assert_retained(
            log,
            sm,
            released,
            audits,
            session_map,
            restore_was="the writer answered True and wrote nothing",
        )

    @pytest.mark.asyncio
    async def test_a_release_that_fails_twice_leaves_the_row_the_next_boot_hydrates_from(
        self, audits, session_map, monkeypatch
    ):
        """The double fault: the header restore RAISES and the map's next flush
        fails too. With the row cleared first, the header restore's failure put
        the row back through that failing flush, so the header and the mark said
        the mode over a row that said nothing -- and the next boot, which
        hydrates the mark from the row alone, read the private session as
        persistent while every header-only reader still read it private. With
        the header restored first, a restore that fails loosens nothing: the
        clear is never reached, the row stands, and the restart hydrates the
        mode. Mutation: clear the row ahead of the header (the earlier order) --
        red: ``hydrated=False, row=False, header='incognito'``."""
        from kiro_crew.history import ConversationLog

        log = self._seed_header()
        sm = session_map()
        res = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await _land_on_disk(sm)
        assert session_map().get_flag(_TG_KEY, "incognito") is True, "premise: the row is on disk"
        real_flush = sm.aflush
        flushes: list[int] = []

        async def _lands_once_then_fails() -> None:
            flushes.append(len(flushes))
            if len(flushes) == 1:
                await real_flush()
                return
            raise OSError("disk full")

        monkeypatch.setattr(sm, "aflush", _lands_once_then_fails)
        monkeypatch.setattr(
            ConversationLog, "update_metadata_if", MagicMock(side_effect=OSError("read-only"))
        )
        released = await privacy_mode.release(res, sessions=_Sessions(sm), source="telegram")
        assert released is False
        # The restart: process-local trackers start empty, a second map over the
        # same directory shares no memory with the first.
        privacy_mode.reset()
        privacy_mode.hydrate(_Sessions(session_map()), _TG_KEY)
        hydrated = privacy_mode.is_incognito(_TG_KEY)
        row = session_map().get_flag(_TG_KEY, "incognito")
        header = log.get_metadata(_TG_KEY).get("memory_mode")
        assert hydrated and row is True and header == "incognito", (
            f"the release that failed twice loosened the row a restart hydrates from: "
            f"hydrated={hydrated}, row={row}, header={header!r}, flushes={len(flushes)}"
        )
        assert [e["outcome"] for e in audits] == ["allowed", "retained"]
        assert audits[-1]["resources"] == f"release_failed:persist_failed:{_TG_KEY}"

    @pytest.mark.asyncio
    async def test_two_concurrent_reservations_cannot_erase_the_committed_mode(
        self, audits, session_map, monkeypatch
    ):
        """Two ``!incognito`` on one key, the second arriving while the first's
        application is still awaiting its header write. The shared holder is
        registered before the first's first await, so the second JOINS it and waits;
        when the second's step lands (commit) and the first's fails (release), the
        committed mode survives and exactly one row exists. Mutation: register the
        holder after the application -- the second creates its own holder, commits
        and retires it, the first then registers a fresh one and its release erases
        the mode the second committed (red: ``is_incognito`` False)."""
        sm = session_map()
        sessions = _Sessions(sm)
        gate = asyncio.Event()
        real_stamp = privacy_mode._persist_transcript_mode

        async def _slow_stamp(session_key: str, mode: str) -> None:
            # flake-ok: pre-existing unbounded gate wait in the stamp stub, released by the test's gate.set(), moved verbatim; tracked for a de-flake pass
            await gate.wait()  # the first application is mid-await here
            await real_stamp(session_key, mode)

        monkeypatch.setattr(privacy_mode, "_persist_transcript_mode", _slow_stamp)
        first = asyncio.create_task(
            privacy_mode.reserve(
                privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
            )
        )
        await async_wait_until(lambda: privacy_mode.is_incognito(_TG_KEY))
        assert privacy_mode.is_incognito(
            _TG_KEY
        ), "premise: the key is held as restricted before the await (the mark follows the header)"
        assert not first.done(), "premise: the first application is still awaiting its stamp"

        async def _reserve_then_steer_lands() -> privacy_mode.Reservation:
            # The second modifier: its reservation returns and its steer lands at
            # once -- so it commits the moment reserve hands it back.
            res = await privacy_mode.reserve(
                privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
            )
            await privacy_mode.commit(res)
            return res

        second = asyncio.create_task(_reserve_then_steer_lands())
        # flake-ok: pre-existing one-yield barrier before reading whether the second caller still waits, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        second_waited = not second.done()  # asserted last: the ERASE is the finding
        gate.set()
        # flake-ok: pre-existing gather with no timeout of the two reservations the gate.set() above releases, moved verbatim; tracked for a de-flake pass
        first_res, _second_res = await asyncio.gather(first, second)
        await privacy_mode.release(
            first_res, sessions=sessions, source="telegram"
        )  # the first's steer did not land
        assert privacy_mode.is_incognito(
            _TG_KEY
        ), "the committed mode was erased by the loser's release"
        assert sm.get_flag(_TG_KEY, "incognito") is True
        assert sm.privacy_flagged_entries() == {_TG_KEY: ["incognito"]}
        assert privacy_mode._pending == {}
        assert [e["outcome"] for e in audits] == ["allowed"]
        assert second_waited, "the second caller must wait for the first application to settle"

    @pytest.mark.asyncio
    async def test_a_joiner_attempts_its_own_application_when_the_firsts_failed(
        self, audits, session_map, monkeypatch
    ):
        """The first holder's application is refused (the map cannot write the row);
        the joiner does not ride a failed application -- it attempts its own, which
        the same map refuses too, and no pending state is left behind."""
        sm = session_map()
        sessions = _Sessions(sm)
        gate = asyncio.Event()

        real_set_flag = sm.set_flag

        def _slow_failing_set_flag(key, flag, value):
            raise OSError("disk full")

        monkeypatch.setattr(sm, "set_flag", _slow_failing_set_flag)
        real_read = privacy_mode.asyncio.to_thread

        async def _gated_to_thread(fn, *a, **kw):
            # flake-ok: pre-existing unbounded gate wait in the to_thread stub, released by the test's gate.set(), moved verbatim; tracked for a de-flake pass
            await gate.wait()  # hold the first holder in its pre-application header read
            return await real_read(fn, *a, **kw)

        # flake-ok: pre-existing stdlib asyncio.to_thread rebound worker-wide through privacy_mode's asyncio alias, scoped to this test by monkeypatch, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(privacy_mode.asyncio, "to_thread", _gated_to_thread)
        first = asyncio.create_task(
            privacy_mode.reserve(
                privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
            )
        )
        # flake-ok: pre-existing one-yield barrier so the first holder reaches its gated read, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        second = asyncio.create_task(
            privacy_mode.reserve(
                privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
            )
        )
        # flake-ok: pre-existing one-yield barrier before reading whether the joining reservation still waits, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        assert not second.done()
        gate.set()
        # flake-ok: pre-existing gather with no timeout of the two reservations the gate.set() above releases, moved verbatim; tracked for a de-flake pass
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(r, privacy_mode.PrivacyModeRefused) for r in results), results
        assert privacy_mode._pending == {} and not privacy_mode.is_incognito(_TG_KEY)
        monkeypatch.setattr(sm, "set_flag", real_set_flag)

    @pytest.mark.asyncio
    async def test_releasing_one_mode_keeps_the_header_of_the_other_committed_mode(
        self, audits, session_map
    ):
        """Two reservations of DIFFERENT modes on one key -- ``/temporary`` then
        ``/incognito`` -- the incognito one commits, the temporary one releases.
        The header must still record ``incognito``: a header-only reader
        (``is_incognito_transcript``, the consolidator's header source) takes it
        at its word, so a bare ``persistent`` there exposes the committed
        incognito transcript. Mutation: restore the released reservation's own
        ``header_before`` alone (the shape this replaces) -- red, the header reads
        ``persistent`` while the incognito row and mark stand."""
        log = self._seed_header()
        sm = session_map()
        sessions = _Sessions(sm)
        temp = await privacy_mode.reserve(
            privacy_mode.MODE_TEMPORARY, _TG_KEY, source="telegram", sessions=sessions
        )
        inco = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
        )
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "temporary", "premise"
        await privacy_mode.commit(inco)
        released = await privacy_mode.release(temp, sessions=sessions, source="telegram")
        assert released is True
        header = log.get_metadata(_TG_KEY).get("memory_mode")
        assert header == "incognito", f"the committed incognito mode was erased: header={header!r}"
        assert privacy_mode.is_incognito(_TG_KEY) and not privacy_mode.is_temporary(_TG_KEY)
        assert sm.privacy_flagged_entries() == {_TG_KEY: ["incognito"]}
        assert privacy_mode._pending == {}

    @pytest.mark.asyncio
    async def test_releasing_the_weaker_mode_leaves_the_stricter_committed_header(
        self, audits, session_map
    ):
        """The other order: ``/incognito`` reserved first, ``/temporary`` second and
        committed, incognito released. The header records ``temporary`` (the
        stricter stamp) and must keep it -- the restore is guarded by "the header
        still records the released mode" AND targets the strictest claim standing,
        and either alone would do here; both are asserted."""
        log = self._seed_header()
        sm = session_map()
        sessions = _Sessions(sm)
        inco = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
        )
        temp = await privacy_mode.reserve(
            privacy_mode.MODE_TEMPORARY, _TG_KEY, source="telegram", sessions=sessions
        )
        await privacy_mode.commit(temp)
        assert (
            privacy_mode._restore_target(sessions, _TG_KEY, privacy_mode.MODE_INCOGNITO, None)
            == "temporary"
        )
        assert await privacy_mode.release(inco, sessions=sessions, source="telegram") is True
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "temporary"
        assert privacy_mode.is_temporary(_TG_KEY) and not privacy_mode.is_incognito(_TG_KEY)

    def test_the_restore_target_ranks_every_claim_normalized(self, session_map):
        """``header_before`` is normalized before ranking (a mixed-case ``Temporary``
        is ``temporary``, a foreign value is nothing); a mode held in flight for
        the session counts as a claim; ``persistent`` only when nothing stands."""
        sm = session_map()
        sessions = _Sessions(sm)
        target = privacy_mode._restore_target
        assert target(sessions, _TG_KEY, "incognito", None) == "persistent"
        assert target(sessions, _TG_KEY, "incognito", "Persistent") == "persistent"
        assert target(sessions, _TG_KEY, "incognito", "Temporary") == "temporary"
        privacy_mode.mark_incognito(_TG_KEY)
        assert target(sessions, _TG_KEY, "temporary", None) == "incognito"
        privacy_mode._pending[("temporary", _TG_KEY)] = privacy_mode._Pending(
            settled=asyncio.Event()
        )
        assert target(sessions, _TG_KEY, "incognito", None) == "temporary"

    async def _reserve_both_modes(self, session_map):
        """``!temporary`` then ``!incognito`` reserved on one thread (two steers
        in flight); the header records ``temporary`` (the incognito stamp is a
        tighten-only no-op over it). Returns ``(log, sm, sessions, temp, inco)``."""
        log = self._seed_header()
        sm = session_map()
        sessions = _Sessions(sm)
        temp = await privacy_mode.reserve(
            privacy_mode.MODE_TEMPORARY, _TG_KEY, source="telegram", sessions=sessions
        )
        inco = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
        )
        await _land_on_disk(sm)
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "temporary", "premise"
        assert sm.privacy_flagged_entries() == {_TG_KEY: ["temporary", "incognito"]}, "premise"
        return log, sm, sessions, temp, inco

    @staticmethod
    def _gate_the_restore_to(target: str, *, then_raise: Exception | None = None):
        """A ``ConversationLog.update_metadata_if`` stand-in whose write restoring
        the header to *target* blocks in its worker thread -- ``in_write`` set,
        then ``gate`` awaited -- and then runs the real write (or raises
        *then_raise*) and sets ``done``; every other write runs untouched.
        Returns ``(stand_in, in_write, gate, done)``."""
        from kiro_crew.history import ConversationLog

        real = ConversationLog.update_metadata_if
        in_write, gate, done = threading.Event(), threading.Event(), threading.Event()

        def _gated(self, session_key, patch, predicate, **kw):
            if patch.get("memory_mode") != target:
                return real(self, session_key, patch, predicate, **kw)
            in_write.set()
            # flake-ok: pre-existing 10 s bound on the stub's gate wait, whose expiry is silent, moved verbatim; tracked for a de-flake pass
            gate.wait(timeout=10)
            try:
                if then_raise is not None:
                    raise then_raise
                return real(self, session_key, patch, predicate, **kw)
            finally:
                done.set()

        return _gated, in_write, gate, done

    def _assert_nothing_private_remains(self, log, sm, audits, *, after: str) -> None:
        header = log.get_metadata(_TG_KEY).get("memory_mode")
        rows = sm.privacy_flagged_entries()
        marks = [m for m in ("temporary", "incognito") if privacy_mode.is_restricted(_TG_KEY)]
        trail = [e["outcome"] for e in audits]
        assert header == "persistent" and rows == {} and not marks, (
            f"{after}: the header still says a mode nothing on the session claims: "
            f"header={header!r}, rows={rows}, restricted={privacy_mode.is_restricted(_TG_KEY)}, "
            f"trail={trail}"
        )
        assert trail == ["allowed", "allowed", "released", "released"]
        assert privacy_mode._pending == {}

    @pytest.mark.asyncio
    async def test_two_declined_steers_released_in_order_leave_no_private_header(
        self, audits, session_map
    ):
        """``!temporary`` then ``!incognito`` on one thread, both steers declined,
        released in that order. The incognito reservation read ``temporary`` off
        the header when it reserved -- that was the temporary stamp, not a claim
        of its own -- so once temporary is released its release must restore what
        the header held before EITHER: ``persistent``. Mutation: drop
        ``_reroot_claims`` -- red, the header reads ``temporary`` with no row, no
        mark and no group left on the session (the stale private header a
        header-only reader then takes at its word for the thread's life)."""
        log, sm, sessions, temp, inco = await self._reserve_both_modes(session_map)
        assert await privacy_mode.release(temp, sessions=sessions, source="telegram") is True
        assert await privacy_mode.release(inco, sessions=sessions, source="telegram") is True
        await _land_on_disk(sm)
        self._assert_nothing_private_remains(log, sm, audits, after="two releases in order")

    @pytest.mark.asyncio
    async def test_two_declined_steers_released_together_leave_no_private_header(
        self, audits, session_map, monkeypatch
    ):
        """The same two declined steers, their releases INTERLEAVED: the temporary
        release is mid-restore (its header write, targeting the incognito claim
        still standing, blocked in its thread) when the incognito release runs.
        Unserialized, the incognito release reads temporary as still standing
        (tracker and group retire only after the header write), restores nothing
        and retires; the temporary release then writes ``incognito`` over a
        header nothing claims. Under the session's lock the second release waits
        for the first's whole sequence. Mutation: drop the lock -- red, the header
        reads ``incognito`` and ``inco_done_early`` is True."""
        from kiro_crew.history import ConversationLog

        log, sm, sessions, temp, inco = await self._reserve_both_modes(session_map)
        gated, in_write, gate, _done = self._gate_the_restore_to("incognito")
        monkeypatch.setattr(ConversationLog, "update_metadata_if", gated)
        releasing_temp = asyncio.create_task(
            privacy_mode.release(temp, sessions=sessions, source="telegram")
        )
        await async_wait_until(in_write.is_set, off_loop=True)
        releasing_inco = asyncio.create_task(
            privacy_mode.release(inco, sessions=sessions, source="telegram")
        )
        # flake-ok: pre-existing 50 ms barrier before reading whether the second release still waits, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms barrier while the first restore is gated, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.05)
        inco_done_early = releasing_inco.done()  # asserted last: the header is the finding
        gate.set()
        # flake-ok: pre-existing gather with no timeout of the two releases the gate.set() above releases, moved verbatim; tracked for a de-flake pass
        results = await asyncio.gather(releasing_temp, releasing_inco)
        await _land_on_disk(sm)
        self._assert_nothing_private_remains(log, sm, audits, after="two releases interleaved")
        assert results == [True, True]
        assert not inco_done_early, "the second release ran while the first was mid-restore"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", ["restored", "raised"])
    async def test_a_cancellation_during_the_header_restore_completes_the_release(
        self, audits, session_map, monkeypatch, outcome
    ):
        """The release task is cancelled (a gateway shutdown) while its header
        restore is in its worker thread: the thread cannot be stopped and the
        clear is still to come, so the durable steps run on as one shielded task,
        their outcome is awaited, and the sequence ends in one of its two states
        before the cancellation propagates -- header restored, row cleared, mark
        dropped, ``released``; or nothing loosened, row and mark standing,
        ``retained``. Mutation: re-raise at the first ``CancelledError`` (the
        earlier shape) -- red on both: the header is restored while the row and
        the mark still say the mode (``restored``), and the trail records neither
        outcome. The premise pins the ORDER: while the header restore is in
        flight the row still says the mode -- the record a restart hydrates from
        is loosened last."""
        from kiro_crew.history import ConversationLog

        log = self._seed_header()
        sm = session_map()
        sessions = _Sessions(sm)
        res = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
        )
        await _land_on_disk(sm)
        gated, in_write, gate, done = self._gate_the_restore_to(
            "persistent", then_raise=OSError("read-only") if outcome == "raised" else None
        )
        monkeypatch.setattr(ConversationLog, "update_metadata_if", gated)
        releasing = asyncio.create_task(
            privacy_mode.release(res, sessions=sessions, source="telegram")
        )
        await async_wait_until(in_write.is_set, off_loop=True)
        assert sm.get_flag(_TG_KEY, "incognito") is True, (
            "premise: the row is loosened LAST -- it still says the mode while the header "
            "restore is in flight"
        )
        releasing.cancel()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            # flake-ok: pre-existing await with no timeout of the cancelled release task, moved verbatim; tracked for a de-flake pass
            await releasing
        await async_wait_until(done.is_set, off_loop=True)
        await _land_on_disk(sm)
        header = log.get_metadata(_TG_KEY).get("memory_mode")
        row = sm.get_flag(_TG_KEY, "incognito")
        marked = privacy_mode.is_incognito(_TG_KEY)
        trail = [e["outcome"] for e in audits]
        if outcome == "restored":
            whole = header == "persistent" and row is False and not marked
            whole = whole and trail == ["allowed", "released"]
        else:
            whole = header == "incognito" and row is True and marked
            whole = whole and trail == ["allowed", "retained"]
        assert whole, (
            f"a cancellation mid-restore left the records split ({outcome}): "
            f"header={header!r}, row={row}, marked={marked}, trail={trail}"
        )
        assert privacy_mode._pending == {}

    @pytest.mark.asyncio
    async def test_a_modifier_arriving_during_a_release_waits_and_lands_its_own_row(
        self, audits, session_map, monkeypatch
    ):
        """A declined steer releases its reservation; while the release awaits its
        durable clear the mark still stands (it is dropped last). A same-mode
        message arriving in that window must not run under that mark -- it would
        lose the mode when the clear landed under it -- so the group stays
        registered, ``releasing``, the request waits, and once the release has
        settled it lands a row of its own. Mutation: pop the group before the
        awaited clear (the r26 shape) -- red: the request reads "already applied"
        off the mark, returns at once, and the release then drops row, header
        and mark under it (``marked=False, row=False, applied=False``).
        """
        from kiro_crew.history import transcript_privacy_mode

        log = self._seed_header()
        sm = session_map()
        sessions = _Sessions(sm)
        res = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
        )
        await _land_on_disk(sm)
        gate = asyncio.Event()
        in_clear = asyncio.Event()
        real_aflush = sm.aflush

        async def _slow_flush() -> None:
            in_clear.set()
            # flake-ok: pre-existing unbounded gate wait in the flush stub, released by the test's gate.set(), moved verbatim; tracked for a de-flake pass
            await gate.wait()
            await real_aflush()

        monkeypatch.setattr(sm, "aflush", _slow_flush)
        releasing = asyncio.create_task(
            privacy_mode.release(res, sessions=sessions, source="telegram")
        )
        await await_until(in_clear)
        assert privacy_mode.is_incognito(_TG_KEY), "premise: the mark stands during the clear"
        rec = _Recorder()
        arriving = asyncio.create_task(
            privacy_mode.apply_mode(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                sessions=sessions,
                notify=rec.notify,
            )
        )
        # flake-ok: pre-existing 10 ms barrier before reading whether the arriving modifier still waits, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 10 ms barrier while the release is gated, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.01)
        request_waited = not arriving.done()  # asserted last: the lost mode is the finding
        gate.set()
        # flake-ok: pre-existing await with no timeout of the release the gate.set() above lets finish, moved verbatim; tracked for a de-flake pass
        released = await releasing
        # flake-ok: pre-existing await with no timeout of the arriving modifier's task, moved verbatim; tracked for a de-flake pass
        applied = await arriving
        await _land_on_disk(sm)
        marked = privacy_mode.is_incognito(_TG_KEY)
        row = sm.get_flag(_TG_KEY, "incognito")
        assert marked and row and applied is True, (
            "the concurrent modifier ran on a mark the release then dropped: "
            f"marked={marked}, row={row}, applied={applied}"
        )
        assert released is True, "the release itself still took the reservation back"
        assert sm.privacy_flagged_entries() == {_TG_KEY: ["incognito"]}
        assert transcript_privacy_mode(log.get_metadata(_TG_KEY).get("memory_mode")) == "incognito"
        assert privacy_mode._pending == {}
        assert [e["outcome"] for e in audits] == ["allowed", "released", "allowed"]
        assert rec.notices == [privacy_mode.NOTICE_INCOGNITO]
        assert request_waited, "the request must wait for the release to settle, not ride the mark"


class TestAnInFlightCommitIsHeldNotPublished:
    """Between the modifier and its row landing on disk, the key is HELD: the
    predicates answer restricted (a concurrent message runs restricted, never
    persistent), yet nothing is published -- no mark, no header, no notice -- and
    the hold vanishes with a write that fails."""

    @staticmethod
    def _gated_flush(sm, gate: asyncio.Event, *, fail: bool):
        real = sm.aflush

        async def _aflush() -> None:
            # flake-ok: pre-existing unbounded gate wait in the flush stub, released by the test's gate.set(), moved verbatim; tracked for a de-flake pass
            await gate.wait()
            if fail:
                raise OSError("disk full")
            await real()

        return _aflush

    @pytest.mark.asyncio
    async def test_the_key_is_restricted_while_the_row_lands_and_free_if_it_fails(
        self, audits, session_map, monkeypatch
    ):
        """Mutation: drop the ``_held`` term from the predicates -- red on the first
        block (a message arriving mid-write would run persistent); publish the mark
        ahead of the flush -- red on the second (the tracker holds a key whose row
        never landed)."""
        sm = session_map()
        sessions = _Sessions(sm)
        gate = asyncio.Event()
        monkeypatch.setattr(sm, "aflush", self._gated_flush(sm, gate, fail=True))
        rec = _Recorder()
        task = asyncio.create_task(
            privacy_mode.apply_mode(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                sessions=sessions,
                notify=rec.notify,
            )
        )
        await async_wait_until(lambda: privacy_mode._landing(privacy_mode.MODE_INCOGNITO, _TG_KEY))
        assert privacy_mode._landing("incognito", _TG_KEY), "premise: the write is in flight"
        # Held, not published.
        assert privacy_mode.is_incognito(_TG_KEY) and privacy_mode.is_restricted(_TG_KEY)
        assert _TG_KEY not in privacy_mode._incognito, "the mark was published before the row"
        privacy_mode.hydrate(sessions, _TG_KEY)
        assert _TG_KEY not in privacy_mode._incognito, "hydrate published a row still landing"
        assert rec.notices == [] and audits == []
        gate.set()
        with pytest.raises(privacy_mode.PrivacyModeRefused):
            # flake-ok: pre-existing await with no timeout of the modifier the gate.set() above lets finish, moved verbatim; tracked for a de-flake pass
            await task
        # The hold is gone with the write.
        assert not privacy_mode.is_restricted(_TG_KEY)
        assert sm.get_flag(_TG_KEY, "incognito") is False
        assert privacy_mode._pending == {}
        assert [e["outcome"] for e in audits] == ["denied"]

    @pytest.mark.asyncio
    async def test_the_mark_lands_once_the_row_is_on_disk(self, audits, session_map, monkeypatch):
        """The success half: the mark, the ``allowed`` record and the notice all
        follow the flush, and a fresh map reads the row back."""
        sm = session_map()
        sessions = _Sessions(sm)
        gate = asyncio.Event()
        monkeypatch.setattr(sm, "aflush", self._gated_flush(sm, gate, fail=False))
        rec = _Recorder()
        task = asyncio.create_task(
            privacy_mode.apply_mode(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                sessions=sessions,
                notify=rec.notify,
            )
        )
        await async_wait_until(lambda: privacy_mode._landing(privacy_mode.MODE_INCOGNITO, _TG_KEY))
        assert _TG_KEY not in privacy_mode._incognito and audits == [] and rec.notices == []
        gate.set()
        # flake-ok: pre-existing await with no timeout of the modifier the gate.set() above lets finish, moved verbatim; tracked for a de-flake pass
        assert await task is True
        assert _TG_KEY in privacy_mode._incognito
        assert session_map().get_flag(_TG_KEY, "incognito") is True
        assert [e["outcome"] for e in audits] == ["allowed"]
        assert rec.notices == [privacy_mode.NOTICE_INCOGNITO]
        assert privacy_mode._pending == {}

    @pytest.mark.asyncio
    async def test_a_second_modifier_during_the_write_joins_and_commits_the_group(
        self, audits, session_map, monkeypatch
    ):
        """A concurrent ``apply_mode`` for the same (mode, key) finds the group and
        waits instead of re-committing: one row, one ``allowed`` record, one notice;
        and because its message runs under the mode it COMMITS the group, so a
        reservation riding the same group cannot take the mode back."""
        sm = session_map()
        sessions = _Sessions(sm)
        gate = asyncio.Event()
        monkeypatch.setattr(sm, "aflush", self._gated_flush(sm, gate, fail=False))
        rec = _Recorder()
        first = asyncio.create_task(
            privacy_mode.reserve(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                sessions=sessions,
                notify=rec.notify,
            )
        )
        await async_wait_until(lambda: privacy_mode._landing(privacy_mode.MODE_INCOGNITO, _TG_KEY))
        second = asyncio.create_task(
            privacy_mode.apply_mode(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                sessions=sessions,
                notify=rec.notify,
            )
        )
        # flake-ok: pre-existing one-yield barrier before reading whether the second modifier still waits, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        assert not second.done(), "the second modifier must wait for the write in flight"
        gate.set()
        # flake-ok: pre-existing gather with no timeout of the two modifiers the gate.set() above releases, moved verbatim; tracked for a de-flake pass
        res, applied_by_second = await asyncio.gather(first, second)
        assert applied_by_second is False
        assert [e["outcome"] for e in audits] == ["allowed"]
        assert rec.notices == [privacy_mode.NOTICE_INCOGNITO]
        # The plain modifier's message ran under the mode: the reservation's failed
        # step must not loosen it.
        assert await privacy_mode.release(res, sessions=sessions, source="telegram") is False
        assert privacy_mode.is_incognito(_TG_KEY) and sm.get_flag(_TG_KEY, "incognito") is True
