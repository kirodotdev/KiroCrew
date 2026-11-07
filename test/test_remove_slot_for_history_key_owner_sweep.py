"""Tests for the history-delete routes' cron-owner sweep and transcript metadata.

``api_session_delete`` and ``api_sessions_clear`` run a strict cron-owner sweep before
and after the unlink. These tests pin which keys a transcript's metadata may name,
how each sweep failure is reported, and that unreadable or malformed metadata aborts
the delete.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any, Collection, Iterator
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from remove_slot_for_history_key_helpers import _make_state

from kiro_crew import cron as cron_module
from kiro_crew.cron import CronService, CronStoreBusy, CronStoreUnreadable
from kiro_crew.dashboard.handlers import api_session_delete, api_sessions_clear
from kiro_crew.dashboard.handlers import sessions as sessions_module
from kiro_crew.dashboard.handlers.sessions import (
    _CRON_RELEASE_ATTEMPTS,
    CRON_OWNERSHIP_UNKNOWN_CODE,
    CRON_STORE_BUSY_CODE,
    CRON_STORE_UNREADABLE_CODE,
)
from kiro_crew.dashboard.state import DashboardState


class TestAJobBornDuringTheDeleteIsNotStranded:
    """An add that commits mid-delete is caught by the post-delete pass.

    The pre-delete sweep sees only what exists when it scans, and a channel
    ``cron_add`` commits under its own store lock. The store's add-time guard
    resolves a ``cron:`` parent id, so a channel-keyed owner passes it untouched
    and the row lands stamped with the key this delete retires.
    """

    def _request(self, state) -> MagicMock:
        request = MagicMock(spec=web.Request)
        request.get = {}.get  # the dashboard user: no app claim
        request.app = {"state": state}
        return request

    def _state(self, crons, log) -> MagicMock:
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        return state

    @pytest.mark.asyncio
    async def test_an_add_between_the_sweep_and_the_unlink_ends_ownerless(
        self, tmp_path, monkeypatch
    ):
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        real_delete = sessions_module._delete_history_session
        born: dict[str, str] = {}

        def _add_then_delete(
            log_arg,
            key,
            claim,
            *,
            skip_pinned=False,
            exact_owner_keys=(),
            exclude=None,
            owner_app="",
        ):
            # The seam itself: the add commits after the sweep has read the store
            # and before the unlink retires the key.
            if not born:
                job = crons.add_job("late", "ping", every_secs=3600, session_key=channel_key)
                born["id"] = job.id
            return real_delete(
                log_arg,
                key,
                claim,
                skip_pinned=skip_pinned,
                exact_owner_keys=exact_owner_keys,
                exclude=exclude,
                owner_app=owner_app,
            )

        monkeypatch.setattr(sessions_module, "_delete_history_session", _add_then_delete)

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert born, "the interleaved add never ran, so the window was not exercised"
        assert CronService(base_dir=tmp_path).get_job(born["id"]).session_key == ""

    @pytest.mark.asyncio
    async def test_an_unreadable_store_after_the_delete_names_the_gap(
        self, tmp_path, monkeypatch, caplog
    ):
        """The row is gone, so the post-pass cannot refuse -- it must say so."""
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        calls = {"n": 0}
        real_sweep = sessions_module._owner_keys_bound_to_transcript

        async def _second_pass_blind(crons_arg, keys):
            calls["n"] += 1
            if calls["n"] == 1:
                return await real_sweep(crons_arg, keys)
            raise CronStoreUnreadable("crons.json is not readable")

        monkeypatch.setattr(sessions_module, "_owner_keys_bound_to_transcript", _second_pass_blind)

        with caplog.at_level(logging.WARNING):
            resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert calls["n"] == 2
        assert "post-delete owner scan could not read" in caplog.text
        assert "kirocrew cron adopt" in caplog.text

    @pytest.mark.asyncio
    async def test_the_bulk_clear_catches_an_add_between_its_sweep_and_the_unlink(
        self, tmp_path, monkeypatch
    ):
        """The bulk twin: ONE pre-sweep, a per-row unlink loop, then ONE re-sweep.

        One scan fronts the whole loop, so an add landing before its row's unlink
        must be released by the batched post-unlink pass.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        log = _FakeTranscriptStore(history_key, "")
        log.list_sessions_indexed = lambda: [{"key": history_key}]  # type: ignore[attr-defined]
        request = self._request(self._state(crons, log))

        real_delete = sessions_module._delete_history_session
        born: dict[str, str] = {}

        def _add_then_delete(
            log_arg,
            key,
            claim,
            *,
            skip_pinned=False,
            exact_owner_keys=(),
            exclude=None,
        ):
            # The seam itself: the batch sweep has read the store; the unlink has
            # not yet retired this row. Same interleaving as the single delete.
            if not born:
                job = crons.add_job("late", "ping", every_secs=3600, session_key=channel_key)
                born["id"] = job.id
            return real_delete(
                log_arg,
                key,
                claim,
                skip_pinned=skip_pinned,
                exact_owner_keys=exact_owner_keys,
                exclude=exclude,
            )

        monkeypatch.setattr(sessions_module, "_delete_history_session", _add_then_delete)

        resp = await api_sessions_clear(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (body["ok"], body["cleared"], body["failed"]) == (True, 1, 0)
        assert born, "the interleaved add never ran, so the window was not exercised"
        assert CronService(base_dir=tmp_path).get_job(born["id"]).session_key == ""

    @pytest.mark.asyncio
    async def test_the_bulk_clear_re_sweeps_once_for_the_whole_batch(self, tmp_path, monkeypatch):
        """Two lock acquisitions for N rows, not N + 1: the post-pass is batched too."""
        keys = [f"slack_178537013{i}.085469" for i in range(3)]
        crons = CronService(base_dir=tmp_path)
        log = _FakeTranscriptStore(keys[0], "")
        for key in keys[1:]:
            log._meta[key] = {}
        log.list_sessions_indexed = lambda: [{"key": key} for key in keys]  # type: ignore[attr-defined]
        request = self._request(self._state(crons, log))

        seen: list[list[str]] = []
        real_sweep = sessions_module._owner_keys_bound_to_transcript

        async def _counting_sweep(crons_arg, sweep_keys):
            seen.append(sorted(sweep_keys))
            return await real_sweep(crons_arg, sweep_keys)

        monkeypatch.setattr(sessions_module, "_owner_keys_bound_to_transcript", _counting_sweep)

        resp = await api_sessions_clear(request)

        assert json.loads(resp.body.decode("utf-8"))["cleared"] == 3
        # The pre-sweep takes the selection; the post-pass takes what was unlinked.
        assert seen == [sorted(keys), sorted(keys)]

    @pytest.mark.asyncio
    async def test_an_unreadable_store_after_the_bulk_clear_names_the_gap(
        self, tmp_path, monkeypatch, caplog
    ):
        """The rows are gone, so the post-pass cannot refuse -- it must say so."""
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, "")
        log.list_sessions_indexed = lambda: [{"key": history_key}]  # type: ignore[attr-defined]
        request = self._request(self._state(crons, log))

        calls = {"n": 0}
        real_sweep = sessions_module._owner_keys_bound_to_transcript

        async def _second_pass_blind(crons_arg, sweep_keys):
            calls["n"] += 1
            if calls["n"] == 1:
                return await real_sweep(crons_arg, sweep_keys)
            raise CronStoreUnreadable("crons.json is not readable")

        monkeypatch.setattr(sessions_module, "_owner_keys_bound_to_transcript", _second_pass_blind)

        with caplog.at_level(logging.WARNING):
            resp = await api_sessions_clear(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (resp.status, body["ok"], body["cleared"], body["failed"]) == (200, True, 1, 0)
        assert calls["n"] == 2
        # What the pre-sweep found is still released; only the late arrival is named.
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""
        assert "post-delete owner scan could not read" in caplog.text
        assert history_key in caplog.text
        assert "cron adopt <id> --release" in caplog.text


class _FakeTranscriptStore:
    """A conversation log whose metadata dies with the transcript.

    ``get_metadata_status`` answers ``({}, True)`` once the row is unlinked, as the
    real store does, so reading ``linked_session_key`` after the delete gets nothing.

    ``unreadable=True`` gives ``({}, False)`` and ``raises=True`` raises, as the real
    store does on corrupt metadata. ``malformed=<payload>`` returns a readable
    non-mapping (or a non-string link), which also models a forged line.
    """

    def __init__(
        self,
        key: str,
        linked_session_key: str,
        *,
        unreadable: bool = False,
        raises: bool = False,
        malformed: Any = None,
    ) -> None:
        meta = {"linked_session_key": linked_session_key} if linked_session_key else {}
        self._meta: dict[str, Any] = {key: meta if malformed is None else malformed}
        self.calls: list[str] = []
        self._unreadable = unreadable
        self._raises = raises

    @contextlib.contextmanager
    def _locked(self, key: str) -> Iterator[None]:
        """Reentrant no-op stand-in for the real history lock."""
        self.calls.append(f"locked:{key}")
        yield

    @contextlib.contextmanager
    def locked_stems(self, stems: Collection[str]) -> Iterator[None]:
        """Stand in for ConversationLog's canonical-plus-legacy lock set."""
        ordered = tuple(stems)
        self.calls.append(f"locked_stems:{','.join(ordered)}")
        with self._locked(ordered[0] if ordered else ""):
            yield

    @contextlib.contextmanager
    def delete_in_flight_window(self, key: str) -> Iterator[None]:
        """No-op stand-in: nothing in these tests races a resume."""
        yield

    def get_metadata_status(self, key: str) -> tuple[Any, bool]:
        self.calls.append(f"get_metadata:{key}")
        if key in self._meta:
            if self._raises:
                raise json.JSONDecodeError("bad first line", "", 0)
            if self._unreadable:
                return {}, False
        payload = self._meta.get(key, {})
        # Copy only a mapping -- a malformed payload is handed back as-is,
        # exactly as a salvaged line would reach the caller.
        return (dict(payload) if isinstance(payload, dict) else payload), True

    def get_metadata(self, key: str) -> Any:
        return self.get_metadata_status(key)[0]

    def delete_session(self, key: str, *, skip_pinned: bool = False) -> bool:
        self.calls.append(f"delete_session:{key}")
        existed = key in self._meta
        self._meta.pop(key, None)
        return existed


class TestALinkedKeyMustNameItsOwnTranscript:
    """The metadata link is untrusted INPUT to a privileged action.

    The transcript is agent-writable and the link is handed to
    ``release_jobs_owned_by``, so an unchecked link lets an agent clear a victim
    session's cron ownership. The route's history key is what the link is checked
    against.
    """

    def _request(self, state) -> MagicMock:
        request = MagicMock(spec=web.Request)
        request.get = {}.get  # the dashboard user: no app claim
        request.app = {"state": state}
        return request

    def _state(self, crons, log) -> MagicMock:
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        return state

    @pytest.mark.asyncio
    async def test_a_foreign_linked_key_is_dropped_not_honoured(self, tmp_path, caplog):
        """The attack: attacker's own row, victim's key in its metadata."""
        victim_key = "slack:1785370133.085469"
        attacker_history_key = "dashboard_chat-9-999"
        crons = CronService(base_dir=tmp_path)
        victim_job = crons.add_job("victim", "ping", every_secs=3600, session_key=victim_key)
        log = _FakeTranscriptStore(attacker_history_key, victim_key)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": attacker_history_key}

        with caplog.at_level(logging.WARNING):
            resp = await api_session_delete(request)

        # The attacker deletes its OWN row -- always within its reach -- but the
        # claim is dropped rather than honoured: the victim keeps its job.
        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert victim_key in caplog.text and "not honoured" in caplog.text
        assert CronService(base_dir=tmp_path).get_job(victim_job.id).session_key == victim_key
        assert f"delete_session:{attacker_history_key}" in log.calls

    @pytest.mark.asyncio
    async def test_another_channel_threads_key_is_dropped_too(self, tmp_path):
        """Valid-SHAPED is not valid: a real channel key for a DIFFERENT thread."""
        victim_key = "slack:1785370133.085469"
        attacker_history_key = "slack_9999999999.000001"
        crons = CronService(base_dir=tmp_path)
        victim_job = crons.add_job("victim", "ping", every_secs=3600, session_key=victim_key)
        log = _FakeTranscriptStore(attacker_history_key, victim_key)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": attacker_history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert f"delete_session:{attacker_history_key}" in log.calls
        assert CronService(base_dir=tmp_path).get_job(victim_job.id).session_key == victim_key

    @pytest.mark.parametrize(
        "history_key,linked",
        [
            pytest.param(
                "slack_1785370133.085469", "slack:1785370133.085469", id="canonical-channel-stem"
            ),
            pytest.param(
                "1785370133.085469", "slack:1785370133.085469", id="legacy-bare-thread-ts"
            ),
            pytest.param("cron_76ef369f", "cron:76ef369f", id="cron-born-tab"),
            pytest.param(
                "discord_kirocrew_direct_123",
                "discord:kirocrew:direct:123",
                id="multi-colon-channel-key",
            ),
            pytest.param(
                "dashboard_chat-1-100", "dashboard:chat-1-100", id="dashboard-session-own-key"
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_link_naming_its_own_transcript_proceeds(self, tmp_path, history_key, linked):
        """Every spelling the store legitimately produces for THIS row still deletes.

        A stem match: ``_safe_key`` folds every ``:`` to ``_``, and an old Slack
        thread logs under its bare ``thread_ts``.
        """
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("owned", "ping", every_secs=3600, session_key=linked)
        log = _FakeTranscriptStore(history_key, linked)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert f"delete_session:{history_key}" in log.calls
        # Proceeding means the link was HONOURED, not merely tolerated.
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""

    @pytest.mark.parametrize(
        "history_key",
        [
            pytest.param("dashboard_slack_1785370133.085469", id="pre-migration-leftover-tab-file"),
            pytest.param(
                "dashboard_dashboard_slack_1785370133.085469", id="stacked-dashboard-prefix"
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_leftover_shaped_name_cannot_claim_the_session_it_resembles(
        self, tmp_path, history_key
    ):
        """A leftover-shaped row is not the channel session it is spelled like.

        A forged link on a lookalike file is indistinguishable from a genuine
        leftover tab, and honouring either releases a live session's jobs. The
        strand stays, recoverable by id.
        """
        linked = "slack:1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        victim_job = crons.add_job("victim", "ping", every_secs=3600, session_key=linked)
        log = _FakeTranscriptStore(history_key, linked)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        # The row DELETES (200): a leftover must not be permanently undeletable,
        # and the mismatch reads identically on every retry, so a refusal here
        # could never be cleared by the remedy the 409 names. The claim is still
        # not honoured -- the session it resembles keeps its job.
        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert f"delete_session:{history_key}" in log.calls
        assert CronService(base_dir=tmp_path).get_job(victim_job.id).session_key == linked

    @pytest.mark.asyncio
    async def test_a_dashboard_rows_stripped_stem_is_not_a_linkable_target(self, tmp_path, caplog):
        """No stem survives a ``dashboard_`` strip, channel-shaped or not.

        ``dashboard_chat-1-100`` does not accept a link to ``chat-1-100``: only the
        row's canonical stem is linkable. The claim is dropped with a warning; what
        is released comes from keys derived from the row itself.
        """
        history_key = "dashboard_chat-1-100"
        crons = CronService(base_dir=tmp_path)
        log = _FakeTranscriptStore(history_key, "chat-1-100")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        with caplog.at_level(logging.WARNING):
            resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert f"delete_session:{history_key}" in log.calls
        assert "not honoured" in caplog.text and "chat-1-100" in caplog.text

    @pytest.mark.asyncio
    async def test_the_bulk_clear_deletes_a_leftover_shaped_row_without_releasing(self, tmp_path):
        """The bulk twin of the leftover case: cleared, not skipped, nothing released."""
        linked = "slack:1785370133.085469"
        history_key = "dashboard_slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        victim_job = crons.add_job("victim", "ping", every_secs=3600, session_key=linked)
        log = _FakeTranscriptStore(history_key, linked)
        log.list_sessions_indexed = lambda: [{"key": history_key}]  # type: ignore[attr-defined]
        request = self._request(self._state(crons, log))

        resp = await api_sessions_clear(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (body["cleared"], body["skipped"], body["undeletable"]) == (1, 0, [])
        assert f"delete_session:{history_key}" in log.calls
        assert CronService(base_dir=tmp_path).get_job(victim_job.id).session_key == linked

    @pytest.mark.asyncio
    async def test_the_bulk_clear_names_an_unreadable_row_by_id_and_code(self, tmp_path):
        """A refused row is reported, not folded into ``skipped`` beside pinned rows.

        The single delete answers 409 ``cron_ownership_unknown`` for this row; the
        clear cannot refuse one row of a batch, so it names the row with the same
        code and leaves it intact -- a bare ``skipped: N`` hid which rows could
        never be cleared, and why.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key, unreadable=True)
        log.list_sessions_indexed = lambda: [{"key": history_key}]  # type: ignore[attr-defined]
        request = self._request(self._state(crons, log))

        resp = await api_sessions_clear(request)

        body = json.loads(resp.body.decode("utf-8"))
        # A row left on disk makes the clear partial, so ``ok`` is false.
        assert (resp.status, body["ok"], body["cleared"], body["skipped"]) == (200, False, 0, 0)
        assert body["undeletable"] == [{"id": history_key, "code": CRON_OWNERSHIP_UNKNOWN_CODE}]
        assert f"delete_session:{history_key}" not in log.calls
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key


def test_the_handlers_call_a_method_the_real_state_has() -> None:
    """The handler tests use a specless mock state, which accepts any attribute."""
    assert callable(getattr(DashboardState, "release_deleted_slot_keys", None))


class _SweepBlockedCrons:
    """A store whose strict owner scan never lands.

    ``owner_keys_async`` is the sweep's only read; counting attempts shows the
    bounded backoff ran before the refusal.

    Faithful only because the real ``owner_keys_async`` raises here too
    (``list_jobs_async`` swallows ``CronStoreBusy``). See
    ``TestTheOwnerSweepMustReadTheStoreStrictly`` for a real held flock.
    """

    def __init__(self) -> None:
        self.attempts = 0

    async def owner_keys_async(self) -> set[str]:
        self.attempts += 1
        raise CronStoreBusy("cron store lock held")

    def list_jobs(self, include_disabled: bool = False) -> list[Any]:
        return []


class TestAnUnrecordedChannelOwnerIsFoundFromTheStore:
    """Absent ``linked_session_key`` does not mean "no owner to lose".

    A delete can land between ``cron_add`` and the slot save that writes the link,
    and legacy transcripts have none. Absent is normal for dashboard sessions, so
    the owner is found from the store via ``transcript_stems``.
    """

    def _request(self, state) -> MagicMock:
        request = MagicMock(spec=web.Request)
        request.get = {}.get  # the dashboard user: no app claim
        request.app = {"state": state}
        return request

    def _state(self, crons, log) -> MagicMock:
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        return state

    @pytest.mark.asyncio
    async def test_an_unrecorded_channel_owner_is_still_released(self, tmp_path):
        """The gap: the job is owned, the transcript never recorded by whom.

        The row is the channel transcript, whose own stem is the owner's. A
        ``dashboard_slack_<ts>`` leftover is not covered (see
        :class:`TestAStrippedNameCannotReleaseAnotherSessionsJobs`).
        """
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        # No linked_session_key on the row -- the ordering window, or a legacy row.
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""

    @pytest.mark.asyncio
    async def test_the_sweep_finds_the_owner_of_a_legacy_bare_thread_ts_row(self, tmp_path):
        """A Slack thread older than the canonical key logs under its bare stem.

        ``ConversationLog._path`` falls back to ``<ts>.jsonl``, so the sweep matches
        every stem the owner occupies; the canonical one alone would strand the job.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""
        """The stem cannot be un-folded, so the exact key must come off the job.

        ``_safe_key`` maps EVERY ``:`` to ``_``, so ``discord_kirocrew_direct_123``
        has several possible session keys and the funnel cannot reconstruct the
        right one. The store already holds it.
        """
        channel_key = "discord:kirocrew:direct:123"
        history_key = "discord_kirocrew_direct_123"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("dm", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""

    @pytest.mark.asyncio
    async def test_an_ordinary_dashboard_delete_releases_nothing_extra(self, tmp_path):
        """The sweep must not widen what a delete touches.

        Absent metadata is the NORMAL state here, and the jobs in the store belong
        to other conversations. A sweep that matched loosely would clear ownership
        the delete has no claim on -- which is the defect it exists to prevent,
        pointed the other way.
        """
        history_key = "dashboard_chat-1-100"
        crons = CronService(base_dir=tmp_path)
        theirs = crons.add_job(
            "theirs", "ping", every_secs=3600, session_key="dashboard:chat-9-999"
        )
        channel = crons.add_job(
            "channel", "ping", every_secs=3600, session_key="slack:1785370133.085469"
        )
        unowned = crons.add_job("cli", "ping", every_secs=3600)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        reloaded = CronService(base_dir=tmp_path)
        assert reloaded.get_job(theirs.id).session_key == "dashboard:chat-9-999"
        assert reloaded.get_job(channel.id).session_key == "slack:1785370133.085469"
        assert reloaded.get_job(unowned.id).session_key == ""

    @pytest.mark.asyncio
    async def test_a_delete_releases_the_rows_drain_hold(self, tmp_path):
        """A dropped tab deleted from History must not keep its unread entry held.

        The restore notice that names dropped tabs is written once per boot, so the
        delete is the only event that can tell the slot list the key is gone for good.
        """
        history_key = "dashboard_chat-9-gone"
        log = _FakeTranscriptStore(history_key, "")
        state = self._state(None, log)
        request = self._request(state)
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        (released,) = state.release_deleted_slot_keys.call_args.args
        assert "chat-9-gone" in list(released)

    @pytest.mark.asyncio
    async def test_a_refused_delete_keeps_the_rows_drain_hold(self, tmp_path):
        history_key = "slack_1785370133.085469"
        log = _FakeTranscriptStore(history_key, "")
        state = self._state(_SweepBlockedCrons(), log)
        request = self._request(state)
        request.match_info = {"key": history_key}

        await api_session_delete(request)

        state.release_deleted_slot_keys.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_bulk_clear_releases_each_cleared_rows_drain_hold(self, tmp_path):
        history_key = "dashboard_chat-9-gone"
        log = _FakeTranscriptStore(history_key, "")
        log.list_sessions_indexed = lambda: [{"key": history_key}]  # type: ignore[attr-defined]
        state = self._state(None, log)

        resp = await api_sessions_clear(self._request(state))

        assert json.loads(resp.body.decode("utf-8"))["cleared"] == 1
        (released,) = state.release_deleted_slot_keys.call_args.args
        assert "chat-9-gone" in list(released)

    @pytest.mark.asyncio
    async def test_a_sweep_that_cannot_run_refuses_the_delete(self, tmp_path, caplog):
        """An unseeable store is not an empty one.

        The transcript is the only place the ownership could still be noticed, so
        deleting it while the store cannot be read strands the job silently. A 409
        naming contention, and the row survives to retry.
        """
        history_key = "slack_1785370133.085469"
        crons = _SweepBlockedCrons()
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        with caplog.at_level(logging.WARNING):
            resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert body["ok"] is False
        assert (resp.status, body["code"]) == (409, CRON_STORE_BUSY_CODE)
        assert "retry" in body["error"]
        # NOT unlinked -- the transcript is still the only record of the binding.
        assert f"delete_session:{history_key}" not in log.calls
        assert crons.attempts == _CRON_RELEASE_ATTEMPTS

    @pytest.mark.asyncio
    async def test_a_missing_cron_service_still_deletes(self, tmp_path):
        """No store to sweep is not a failed sweep."""
        history_key = "slack_1785370133.085469"
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(None, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert f"delete_session:{history_key}" in log.calls


class TestAStrippedNameCannotReleaseAnotherSessionsJobs:
    """Reaching a FOREIGN session's owner from this row's NAME is never done.

    The sweep does not strip ``dashboard_`` to reach ``slack_<ts>``: slot names are
    client-chosen, so a lookalike row would release a live channel session's jobs.
    A marker in the transcript cannot gate it, since the file is agent-writable.

    Provenance for a destructive cross-session action must come from outside
    agent-writable storage. The cost is a stranded job, recoverable with
    ``kirocrew cron adopt <id> --release``; a forged release is not recoverable.
    """

    def _request(self, state) -> MagicMock:
        request = MagicMock(spec=web.Request)
        request.get = {}.get  # the dashboard user: no app claim
        request.app = {"state": state}
        return request

    def _state(self, crons, log) -> MagicMock:
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        return state

    @pytest.mark.parametrize(
        "history_key",
        [
            pytest.param("dashboard_slack_1785370133.085469", id="one-prefix"),
            pytest.param("dashboard_dashboard_slack_1785370133.085469", id="stacked-prefix"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_lookalike_dashboard_row_cannot_release_a_channels_jobs(
        self, tmp_path, history_key
    ):
        """The attack: a dashboard session NAMED like the victim's thread."""
        victim_key = "slack:1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        victim_job = crons.add_job("victim", "ping", every_secs=3600, session_key=victim_key)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        # The attacker's OWN row goes -- it owns nothing, so there is nothing to
        # refuse. What must not happen is the victim losing its job.
        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert CronService(base_dir=tmp_path).get_job(victim_job.id).session_key == victim_key

    @pytest.mark.parametrize(
        "forged",
        [
            pytest.param({"channel_origin": True}, id="forged-channel-origin"),
            pytest.param(
                {"channel_origin": True, "tab_id": "tab-7"}, id="forged-marker-with-tab-id"
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_forged_provenance_marker_does_not_re_enable_the_attack(self, tmp_path, forged):
        """The reason no transcript marker can gate this.

        The metadata line lives in the same agent-writable file as the row's name.
        An agent that can create ``dashboard_slack_<ts>.jsonl`` can write
        ``channel_origin`` onto it, so a marker-gated fold is exactly as reachable
        as an ungated one -- it only adds a step. Nothing here may consult it.
        """
        victim_key = "slack:1785370133.085469"
        history_key = "dashboard_slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        victim_job = crons.add_job("victim", "ping", every_secs=3600, session_key=victim_key)
        log = _FakeTranscriptStore(history_key, "", malformed=dict(forged))
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert CronService(base_dir=tmp_path).get_job(victim_job.id).session_key == victim_key

    @pytest.mark.asyncio
    async def test_the_bulk_clear_is_gated_the_same_way(self, tmp_path):
        """One selector, one sweep -- and the same refusal to infer on both."""
        victim_key = "slack:1785370133.085469"
        history_key = "dashboard_slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        victim_job = crons.add_job("victim", "ping", every_secs=3600, session_key=victim_key)
        log = _FakeTranscriptStore(history_key, "")
        log.list_sessions_indexed = lambda: [{"key": history_key}]  # type: ignore[attr-defined]
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        request = MagicMock(spec=web.Request)
        request.get = {}.get  # the dashboard user: no app claim
        request.app = {"state": state}

        resp = await api_sessions_clear(request)

        assert json.loads(resp.body.decode("utf-8"))["cleared"] == 1
        assert CronService(base_dir=tmp_path).get_job(victim_job.id).session_key == victim_key

    @pytest.mark.asyncio
    async def test_a_genuinely_channel_born_leftover_strands_loudly(self, tmp_path, caplog):
        """The price of the rule, paid where an operator can see it.

        The funnel cannot tell this real leftover from the forged case, so it
        declines and warns with the owner, the job id and the recovery command.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "dashboard_slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        with caplog.at_level(logging.WARNING):
            resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key
        assert channel_key in caplog.text
        assert job.id in caplog.text
        assert "kirocrew cron adopt" in caplog.text

    @pytest.mark.asyncio
    async def test_a_present_link_cannot_release_the_session_it_resembles(self, tmp_path):
        """A claim is checked, and a leftover-shaped claim does not pass the check.

        The value is compared against the route-derived history key on the
        CANONICAL stem only, so a row named like a channel thread cannot reach
        that thread's session by carrying a link to it -- which is what makes the
        agent-writable metadata unable to widen anything.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "dashboard_slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        # Deleted, not refused -- but the claim is dropped, so the session it
        # resembles keeps its job.
        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert f"delete_session:{history_key}" in log.calls
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key

    @pytest.mark.asyncio
    async def test_a_lookalike_row_still_releases_its_own_owner(self, tmp_path):
        """Dropping the fold must not narrow the row's own stem.

        A dashboard row named like a channel thread still owns whatever is stamped
        with ITS key. Only the victim's job is withheld.
        """
        history_key = "dashboard_slack_1785370133.085469"
        own_key = "dashboard:slack_1785370133.085469"
        victim_key = "slack:1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        mine = crons.add_job("mine", "ping", every_secs=3600, session_key=own_key)
        victim_job = crons.add_job("victim", "ping", every_secs=3600, session_key=victim_key)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        reloaded = CronService(base_dir=tmp_path)
        assert reloaded.get_job(mine.id).session_key == ""
        assert reloaded.get_job(victim_job.id).session_key == victim_key


def _break_the_owner_read(crons: CronService) -> None:
    """Make the strict owner scan raise something the funnel never named.

    Not ``CronStoreBusy`` and not ``CronStoreUnreadable`` -- an ``OSError`` off
    the read, the shape a defect or an unanticipated environment failure takes.
    Only ``owner_keys_async`` is replaced; the store underneath is real, so the
    release path still runs and the test reads what stayed owned off the disk.
    """

    async def _broken() -> set[str]:
        raise OSError("EIO reading cron store")

    crons.owner_keys_async = _broken  # type: ignore[method-assign]


class _SweepUnreadableCrons:
    """A store whose strict owner scan answers the store's OWN unreadable verdict."""

    async def owner_keys_async(self) -> set[str]:
        raise cron_module.CronStoreUnreadable("refusing: the last load could not read crons.json")

    def list_jobs(self, include_disabled: bool = False) -> list[Any]:
        return []


class TestTheOwnerSweepFailsByCause:
    """Each way the sweep cannot answer has its own outcome, and each is visible.

    Busy and unreadable stores refuse with a 409 whose code names the cause; the
    dashboard keys on the code, so the codes are the contract.

    An unexpected exception proceeds with a WARNING and releases nothing: refusing
    would let one cron-read fault block every history delete.
    """

    def _request(self, state) -> MagicMock:
        request = MagicMock(spec=web.Request)
        request.get = {}.get  # the dashboard user: no app claim
        request.app = {"state": state}
        return request

    def _state(self, crons, log) -> MagicMock:
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        return state

    @pytest.mark.asyncio
    async def test_an_unexpected_reader_exception_deletes_and_names_the_recovery(
        self, tmp_path, caplog
    ):
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        _break_the_owner_read(crons)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        with caplog.at_level(logging.WARNING):
            resp = await api_session_delete(request)

        assert (resp.status, json.loads(resp.body.decode("utf-8"))) == (200, {"ok": True})
        assert f"delete_session:{history_key}" in log.calls
        # Nothing is released on a read that did not happen: the job stays owned
        # by the departed session -- bounded, and recoverable by id.
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        loud = [w for w in warnings if "owner sweep failed unexpectedly" in w]
        assert loud, warnings
        assert history_key in loud[0] and "OSError" in loud[0]
        assert "kirocrew cron adopt <id> --release" in loud[0]

    @pytest.mark.asyncio
    async def test_a_busy_store_refuses_with_its_own_code(self, tmp_path):
        history_key = "slack_1785370133.085469"
        crons = _SweepBlockedCrons()
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (resp.status, body["ok"], body["code"]) == (409, False, CRON_STORE_BUSY_CODE)
        assert "retry" in body["error"]
        assert f"delete_session:{history_key}" not in log.calls

    @pytest.mark.asyncio
    async def test_an_unreadable_store_refuses_with_its_own_code(self, tmp_path):
        history_key = "slack_1785370133.085469"
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(_SweepUnreadableCrons(), log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (resp.status, body["ok"], body["code"]) == (409, False, CRON_STORE_UNREADABLE_CODE)
        # The store's own wording rides along; the by-id CLI reads the same file,
        # so it is deliberately not the remedy offered here.
        assert "could not read crons.json" in body["error"]
        assert "adopt" not in body["error"]
        assert f"delete_session:{history_key}" not in log.calls

    @pytest.mark.asyncio
    async def test_unreadable_metadata_refuses_with_its_own_code_and_the_job_ids(self, tmp_path):
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key, unreadable=True)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (resp.status, body["ok"], body["code"]) == (409, False, CRON_OWNERSHIP_UNKNOWN_CODE)
        # Here the remedy IS the CLI, and the store-side sweep already bound the
        # row to its owner, so the refusal can name the job to release.
        assert "kirocrew cron adopt <id> --release" in body["error"]
        assert job.id in body["error"]
        assert f"delete_session:{history_key}" not in log.calls
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key

    @pytest.mark.asyncio
    async def test_the_bulk_clear_refuses_the_whole_batch_by_cause(self, tmp_path):
        """The batch takes ONE sweep, so a store that cannot be seen refuses all of it."""
        history_key = "slack_1785370133.085469"
        crons = _SweepBlockedCrons()
        log = _FakeTranscriptStore(history_key, "")
        log.list_sessions_indexed = lambda: [{"key": history_key}]  # type: ignore[attr-defined]
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        request = MagicMock(spec=web.Request)
        request.get = {}.get  # the dashboard user: no app claim
        request.app = {"state": state}

        resp = await api_sessions_clear(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (resp.status, body["ok"], body["code"]) == (409, False, CRON_STORE_BUSY_CODE)
        assert (body["cleared"], body["failed"]) == (0, 0)
        assert f"delete_session:{history_key}" not in log.calls

    @pytest.mark.asyncio
    async def test_the_bulk_clear_proceeds_loudly_on_an_unexpected_error(self, tmp_path, caplog):
        """One cron-read fault does not block the clear; it is named instead."""
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        _break_the_owner_read(crons)
        log = _FakeTranscriptStore(history_key, "")
        log.list_sessions_indexed = lambda: [{"key": history_key}]  # type: ignore[attr-defined]
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        request = MagicMock(spec=web.Request)
        request.get = {}.get  # the dashboard user: no app claim
        request.app = {"state": state}

        with caplog.at_level(logging.WARNING):
            resp = await api_sessions_clear(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (resp.status, body["ok"], body["cleared"]) == (200, True, 1)
        assert f"delete_session:{history_key}" in log.calls
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key
        assert "owner sweep failed unexpectedly" in caplog.text
        assert "kirocrew cron adopt <id> --release" in caplog.text


class TestTheOwnerSweepMustReadTheStoreStrictly:
    """The sweep's read has to RAISE on a store it cannot see, not answer empty.

    An empty answer from an unseen store makes the delete destroy an unchecked
    binding. ``list_jobs`` is cache-only and ``list_jobs_async`` swallows
    ``CronStoreBusy``, so neither raises.

    These drive a real ``CronService`` (a held flock, a corrupt file); only
    ``_file_lock``'s spin is shortened to keep the retries quick.
    """

    def _request(self, state) -> MagicMock:
        request = MagicMock(spec=web.Request)
        request.get = {}.get  # the dashboard user: no app claim
        request.app = {"state": state}
        return request

    def _state(self, crons, log) -> MagicMock:
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        return state

    @staticmethod
    @contextlib.contextmanager
    def _store_lock_held(base_dir) -> Iterator[None]:
        """Hold the cron store's flock from a separate open description.

        The same technique ``test_file_lock_is_bounded_when_contended`` uses: flock
        on a second fd conflicts within one process too, so this is the real
        cross-process contention a CLI ``cron adopt`` or a large atomic save
        produces, not a patched-out lock.
        """
        from kiro_crew import platform_compat

        holder = (base_dir / ".crons.lock").open("w")
        platform_compat.acquire_lock(holder.fileno(), exclusive=True)
        try:
            yield
        finally:
            platform_compat.release_lock(holder.fileno())
            holder.close()

    @staticmethod
    def _with_short_lock_spin(crons: CronService) -> None:
        """Shrink the store lock's bounded wait so three retries stay quick.

        Only the timeout changes -- ``_file_lock``'s own non-blocking spin still
        runs and still raises the real ``CronStoreBusy``. The default 10 s wait
        would make each retry round a ten-second stall.
        """
        real_lock = crons._file_lock

        def _short(*, timeout: float = 0.3, poll: float = 0.02):
            return real_lock(timeout=timeout, poll=poll)

        crons._file_lock = _short  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_a_stale_cache_over_a_contended_store_does_not_strand_the_owner(self, tmp_path):
        """The miss this guards: stale-empty cache, owner on disk.

        Another process wrote the job after this cache loaded, and the lock is held.

        Asserts the invariant: released, or refused with the row intact, never
        unlinked while still owned.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        # Another process (CLI, gateway, MCP) stamps the owner AFTER this service
        # loaded, so `crons` holds a cache that predates the job.
        job = CronService(base_dir=tmp_path).add_job(
            "channel", "ping", every_secs=3600, session_key=channel_key
        )
        assert crons.list_jobs(include_disabled=True) == [], "cache should be stale-empty"
        self._with_short_lock_spin(crons)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        with self._store_lock_held(tmp_path):
            resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        still_owned = CronService(base_dir=tmp_path).get_job(job.id).session_key
        if still_owned:
            assert body["ok"] is False, (
                f"job {job.id} is still owned by {still_owned!r} and the delete reported "
                f"success: the sweep read a stale-empty cache over a contended store"
            )
            assert (resp.status, body["code"]) == (409, CRON_STORE_BUSY_CODE)
            assert (
                f"delete_session:{history_key}" not in log.calls
            ), "refused, so the transcript -- the last record of the binding -- must survive"

    @pytest.mark.asyncio
    async def test_a_persistently_contended_store_refuses_loudly(self, tmp_path, caplog):
        """Busy past the retries is a loud refusal, never a silent proceed.

        Same held lock, but asserted from the operator side: a 409 whose code names
        contention and whose text asks for a retry (the by-id CLI cannot help with a
        held lock), the row survives to retry, and the job is untouched.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        self._with_short_lock_spin(crons)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        with caplog.at_level(logging.WARNING):
            with self._store_lock_held(tmp_path):
                resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert body["ok"] is False, "a store that cannot be read is not an empty one"
        assert resp.status == 409
        assert body["code"] == CRON_STORE_BUSY_CODE
        assert "retry" in body["error"]
        assert f"delete_session:{history_key}" not in log.calls
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key
        assert any(
            "owner sweep not performed" in r.message or "cron store busy" in r.message
            for r in caplog.records
        ), "the refusal must be logged, not just returned"

    @pytest.mark.asyncio
    async def test_an_unreadable_store_refuses_instead_of_reading_it_empty(self, tmp_path):
        """``_load`` flattens a corrupt store to ``[]``; the sweep must not believe it.

        ``_sync()`` does not raise on an unparseable store -- it degrades to an
        empty job list -- so the pre-fix sweep reads "no owners" off a store that
        may hold several, and unlinks the only other record of them.
        ``_sync_for_write`` refuses instead.
        """
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        crons.add_job("channel", "ping", every_secs=3600, session_key="slack:1785370133.085469")
        store = next(p for p in tmp_path.rglob("*.json") if "cron" in p.name)
        store.write_text("{ not json at all", encoding="utf-8")
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert body["ok"] is False, "an unreadable store is not an ownerless one"
        assert resp.status == 409
        assert body["code"] == CRON_STORE_UNREADABLE_CODE
        # The store wording travels with the refusal; the by-id CLI reads the same
        # file, so it is deliberately NOT offered as the remedy here.
        assert "could not read" in body["error"] and "adopt" not in body["error"]
        assert f"delete_session:{history_key}" not in log.calls

    @pytest.mark.asyncio
    async def test_the_strict_scan_sees_a_cross_process_write_the_cache_missed(self, tmp_path):
        """Positive control: uncontended, the strict scan is FRESHER than the cache.

        Without it the refusals above could be satisfied by a scan that always
        fails. Same stale-cache setup, lock free: the locked reload picks up the
        other process's job and the release lands.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = CronService(base_dir=tmp_path).add_job(
            "channel", "ping", every_secs=3600, session_key=channel_key
        )
        assert crons.list_jobs(include_disabled=True) == [], "cache should be stale-empty"
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""

    @pytest.mark.asyncio
    async def test_the_store_scan_includes_a_disabled_jobs_owner(self, tmp_path):
        """A paused job's owner is stamped on disk and strands just the same."""
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        crons.enable_job(job.id, False)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""


class TestSlotlessChannelSessionReleasesItsExactOwnerKey:
    """A channel session's cron is owned under the channel's EXACT key.

    After a restart there is no live slot, so the exact key survives only in
    ``linked_session_key``, which the delete destroys; it must be read first.
    """

    @pytest.mark.asyncio
    async def test_delete_releases_the_cron_and_reads_the_key_before_unlinking(self, tmp_path):
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key)
        # Post-restart: the transcript is on disk, the slot is not.
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        request = MagicMock(spec=web.Request)
        request.get = {}.get  # the dashboard user: no app claim
        request.app = {"state": state}
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert log.calls.index(f"get_metadata:{history_key}") < log.calls.index(
            f"delete_session:{history_key}"
        )
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""

    @pytest.mark.asyncio
    async def test_bulk_clear_releases_the_same_cron(self, tmp_path):
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key)
        log.list_sessions_indexed = lambda: [{"key": history_key}]  # type: ignore[attr-defined]
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        request = MagicMock(spec=web.Request)
        request.get = {}.get  # the dashboard user: no app claim
        request.app = {"state": state}

        resp = await api_sessions_clear(request)

        assert json.loads(resp.body.decode("utf-8"))["cleared"] == 1
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""


class TestUnreadableMetadataAbortsTheDelete:
    """A row whose metadata cannot be READ must not be unlinked.

    ``linked_session_key`` is the only copy of a channel session's cron owner, and
    ``_warn_stranded_cron_ownership`` never fires for a key that was never read.

    An absent line or empty dict deletes normally. A non-mapping payload or a
    non-string link is malformed and refuses like the unreadable case.
    """

    def _request(self, state) -> MagicMock:
        request = MagicMock(spec=web.Request)
        request.get = {}.get  # the dashboard user: no app claim
        request.app = {"state": state}
        return request

    def _state(self, crons, log) -> MagicMock:
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        return state

    @pytest.mark.asyncio
    async def test_metadata_that_raises_refuses_the_delete(self, tmp_path, caplog):
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key, raises=True)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        with caplog.at_level(logging.ERROR):
            resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert body["ok"] is False
        assert resp.status == 409
        assert body["code"] == CRON_OWNERSHIP_UNKNOWN_CODE
        # The refusal has to say what to do about it: an operator cannot release
        # ownership by hand from a bare "ok": false.
        assert "kirocrew cron adopt" in body["error"]
        assert history_key in caplog.text and "kirocrew cron adopt" in caplog.text
        # NOT unlinked -- the row still holds the only copy of the owner key, so
        # the ownership is recoverable rather than stranded.
        assert f"delete_session:{history_key}" not in log.calls
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key

    @pytest.mark.asyncio
    async def test_metadata_reported_unreadable_refuses_the_delete(self, tmp_path):
        """``({}, False)`` -- the real store's answer after its read retries."""
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key, unreadable=True)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (resp.status, body["ok"]) == (409, False)
        assert body["code"] == CRON_OWNERSHIP_UNKNOWN_CODE
        assert f"delete_session:{history_key}" not in log.calls
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key

    @pytest.mark.asyncio
    async def test_absent_metadata_still_deletes(self, tmp_path):
        """No metadata line means no owner key to lose. Unchanged behaviour."""
        history_key = "dashboard_chat-1-100"
        crons = CronService(base_dir=tmp_path)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert f"delete_session:{history_key}" in log.calls

    @pytest.mark.asyncio
    async def test_an_empty_metadata_dict_still_deletes(self, tmp_path):
        """``{}`` is ABSENT, not malformed -- the store's own no-metadata answer.

        ``_read_metadata_status`` returns ``({}, True)`` for a row with no
        metadata line AND for a first line that is not metadata-typed, so
        refusing on it would refuse the ordinary dashboard session.
        """
        history_key = "dashboard_chat-1-100"
        crons = CronService(base_dir=tmp_path)
        log = _FakeTranscriptStore(history_key, "", malformed={})
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert f"delete_session:{history_key}" in log.calls

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("slack:1785370133.085469", id="bare-string"),
            pytest.param(["slack:1785370133.085469"], id="list"),
            pytest.param(17, id="scalar"),
            pytest.param({"linked_session_key": ["slack:1785370133.085469"]}, id="non-str-value"),
            pytest.param({"linked_session_key": {"key": "slack:1785"}}, id="mapping-value"),
        ],
    )
    @pytest.mark.asyncio
    async def test_malformed_metadata_refuses_the_delete(self, tmp_path, payload):
        """A READABLE read is not a TRUSTWORTHY one.

        Treating a bad payload as absent (or ``str()``-ing a non-string link) would
        delete a row that may name a real owner.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key, malformed=payload)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert body["ok"] is False
        assert resp.status == 409
        assert body["code"] == CRON_OWNERSHIP_UNKNOWN_CODE
        assert "kirocrew cron adopt" in body["error"]
        assert f"delete_session:{history_key}" not in log.calls
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key

    @pytest.mark.asyncio
    async def test_the_owner_key_is_read_inside_the_delete_lock(self, tmp_path):
        """One lock hold, not two: a writer cannot land a key in between.

        Two separate holds leave a window in which a concurrent
        ``update_metadata`` publishes the ``linked_session_key`` this funnel just
        read as absent -- and the unlink then destroys exactly the key it needed.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        await api_session_delete(request)

        assert log.calls[0].startswith(f"locked_stems:{history_key},")
        assert log.calls.index(f"locked:{history_key}") < log.calls.index(
            f"get_metadata:{history_key}"
        )
        assert log.calls.index(f"get_metadata:{history_key}") < log.calls.index(
            f"delete_session:{history_key}"
        )
