"""Which of a Bot's workers conduct (rfc-bot-workers-conduct.md, section 5.2).

Two facts, kept apart: the thread-born LINEAGE mark, written once per session by
``members.note_worker_lineage`` from a gateway-vouched creator (at mint by
``session_create``, at restore by the slot metadata reader from the verified
``created_by``), and the owner's ``worker_conducts`` SWITCH, read from the Bot's record
at every call of ``members.conducting_worker_session`` and never stored on the mark.
A session conducts only when it is marked, known as the Bot's worker and its Bot's
record says so now; a config that cannot be read fails closed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import members
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

THREAD = "member-pixel"
WORKER = "chat-7-1785396512"
GRANDCHILD = "chat-8-1785396600"


def _config(*, on: bool, degraded: frozenset = frozenset()) -> SimpleNamespace:
    return SimpleNamespace(
        agents={
            "pixel": KiroCrewAgentConfig(
                kiro_agent="pixel", member_id="member-id-pixel", worker_conducts=on
            ),
        },
        degraded_sections=degraded,
    )


def _known_worker(key: str = WORKER, member_id: str = "member-id-pixel") -> None:
    members.note_crewmate_worker_session(key, member_id)


class TestTheMark:
    @pytest.mark.parametrize(
        "creator", [THREAD, f"dashboard_{THREAD}", f"dashboard:{THREAD}", "member-pixel--v2store"]
    )
    def test_a_member_creator_marks_the_session_in_every_key_spelling(self, creator):
        members.note_worker_lineage(WORKER, creator)
        assert members.is_thread_born_worker_session(WORKER)
        assert members.is_thread_born_worker_session(f"dashboard:{WORKER}")
        assert members.is_thread_born_worker_session(f"dashboard_{WORKER}")

    @pytest.mark.parametrize("creator", ["", WORKER, "chat-3", "cron:job-1", "unverified:creator"])
    def test_any_other_creator_leaves_it_unmarked_and_drops_a_stale_mark(self, creator):
        members.note_worker_lineage(WORKER, THREAD)
        members.note_worker_lineage(WORKER, creator)
        assert not members.is_thread_born_worker_session(WORKER)

    def test_an_empty_session_key_is_ignored(self):
        members.note_worker_lineage("", THREAD)
        assert not members.is_thread_born_worker_session("")
        assert not members.is_thread_born_worker_session(None)


class TestThePredicate:
    def test_all_three_hold(self, monkeypatch):
        members.note_worker_lineage(WORKER, THREAD)
        _known_worker()
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _config(on=True)))
        assert members.conducting_worker_session(WORKER) is True
        assert members.conducting_worker_session(f"dashboard:{WORKER}") is True

    def test_the_switch_is_read_at_every_call_never_stored(self, monkeypatch):
        """Turning the switch off reaches a marked worker at its next read; the
        mark records lineage only."""
        members.note_worker_lineage(WORKER, THREAD)
        _known_worker()
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _config(on=False)))
        assert members.conducting_worker_session(WORKER) is False
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _config(on=True)))
        assert members.conducting_worker_session(WORKER) is True
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _config(on=False)))
        assert members.conducting_worker_session(WORKER) is False

    def test_a_caller_holding_the_config_passes_it(self, monkeypatch):
        members.note_worker_lineage(WORKER, THREAD)
        _known_worker()

        def _boom(cls):
            raise AssertionError("the loaded config was handed in; no load may run")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
        assert members.conducting_worker_session(WORKER, _config(on=True)) is True
        assert members.conducting_worker_session(WORKER, _config(on=False)) is False

    def test_an_unmarked_worker_never_conducts_and_reads_no_config(self, monkeypatch):
        """A worker a worker created (a ``chat-`` creator) is a plain worker whatever
        the switch says: one conducting level, by lineage."""
        _known_worker(GRANDCHILD)
        members.note_worker_lineage(GRANDCHILD, WORKER)

        def _boom(cls):
            raise AssertionError("an unmarked session must not read the config")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
        assert members.conducting_worker_session(GRANDCHILD) is False

    def test_a_marked_session_this_process_does_not_know_as_a_worker_never_conducts(
        self, monkeypatch
    ):
        members.note_worker_lineage(WORKER, THREAD)

        def _boom(cls):
            raise AssertionError("a session that is no known worker must not read the config")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
        assert members.conducting_worker_session(WORKER) is False

    def test_the_thread_itself_never_conducts_as_a_worker(self, monkeypatch):
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _config(on=True)))
        assert members.conducting_worker_session(THREAD) is False
        assert members.conducting_worker_session(f"dashboard:{THREAD}") is False

    @pytest.mark.parametrize("lost", ["agents", DEGRADED_WHOLE_CONFIG])
    def test_a_degraded_agents_section_fails_closed(self, monkeypatch, caplog, lost):
        members.note_worker_lineage(WORKER, THREAD)
        _known_worker()
        monkeypatch.setattr(
            KiroCrewConfig,
            "load",
            classmethod(lambda cls: _config(on=True, degraded=frozenset({lost}))),
        )
        with caplog.at_level("WARNING", logger="kiro_crew.members"):
            assert members.conducting_worker_session(WORKER) is False
            assert members.conducting_worker_session(WORKER) is False
        rows = [r for r in caplog.records if r.name == "kiro_crew.members"]
        assert len(rows) == 1 and "plain worker" in rows[0].getMessage()

    def test_an_unreadable_config_fails_closed(self, monkeypatch):
        members.note_worker_lineage(WORKER, THREAD)
        _known_worker()

        def _boom(cls):
            raise OSError("unreadable")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
        assert members.conducting_worker_session(WORKER) is False

    def test_a_record_the_member_id_no_longer_names_fails_closed(self, monkeypatch):
        members.note_worker_lineage(WORKER, THREAD)
        _known_worker(member_id="member-id-gone")
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _config(on=True)))
        assert members.conducting_worker_session(WORKER) is False

    def test_only_a_real_true_counts(self, monkeypatch):
        members.note_worker_lineage(WORKER, THREAD)
        _known_worker()
        cfg = _config(on=True)
        cfg.agents["pixel"].worker_conducts = "true"  # type: ignore[assignment]
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
        assert members.conducting_worker_session(WORKER) is False


class TestMountsSessionControl:
    def test_a_thread_mounts_without_reading_config(self, monkeypatch):
        def _boom(cls):
            raise AssertionError("a thread's answer needs no config")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
        assert members.mounts_session_control(THREAD) is True
        assert members.mounts_session_control(f"dashboard:{THREAD}") is True

    def test_a_conducting_worker_mounts_and_a_plain_one_does_not(self, monkeypatch):
        members.note_worker_lineage(WORKER, THREAD)
        _known_worker()
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _config(on=True)))
        assert members.mounts_session_control(WORKER) is True
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _config(on=False)))
        assert members.mounts_session_control(WORKER) is False
        assert members.mounts_session_control(GRANDCHILD) is False
        assert members.mounts_session_control("") is False
        assert members.mounts_session_control(None) is False


class TestRestoreReMarksFromTheVerifiedCreator:
    """Exit criterion 7: a gateway restarted with a marked worker open re-marks it
    from the slot's verified ``created_by``; an unverified claim re-marks nothing.

    Driven through the bulk recent-session restore, the startup path
    (``test_member_session_control.TestRecentSessionRestoreKeepsCreatedBy`` is the
    shape): the transcript line is writable from inside the sandbox, so the claim
    counts only when the crew log's creation records name the same creator;
    ``_creation_parent`` stands in for that record.
    """

    @staticmethod
    def _restore(tmp_path, monkeypatch, *, created_by: str, verified: bool):
        from kiro_crew.dashboard import session_control as sc
        from kiro_crew.dashboard.chat import restore_recent_sessions
        from kiro_crew.dashboard.state import DashboardState
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        meta_line = {
            "_type": "metadata",
            "created_at": "2026-03-23T10:00:00",
            "last_consolidated": 0,
            "title": "Worker",
            "agent": "kirocrew-mate-worker",
            "created_by": created_by,
        }
        rows = [
            json.dumps(meta_line),
            json.dumps({"role": "user", "content": "task", "ts": "2026-03-23T10:00:00"}),
        ]
        path = tmp_path / f"dashboard_{WORKER}.jsonl"
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        path.touch()
        sessions = MagicMock(count=0)
        sessions.get_pid = MagicMock(return_value=None)
        sessions.remove = AsyncMock()
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        parent = created_by if verified else ""
        monkeypatch.setattr(sc, "_creation_parent", lambda key: parent if key == WORKER else "")
        assert restore_recent_sessions(state, window_minutes=60) == 1
        return state._slots[WORKER]

    def test_a_verified_member_creator_re_marks_the_worker(self, tmp_path, monkeypatch):
        slot = self._restore(tmp_path, monkeypatch, created_by=THREAD, verified=True)
        assert slot._created_by == THREAD
        assert members.is_thread_born_worker_session(WORKER)

    def test_an_unverified_claim_re_marks_nothing(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.session_control import UNVERIFIED_CREATOR

        slot = self._restore(tmp_path, monkeypatch, created_by=THREAD, verified=False)
        assert slot._created_by == UNVERIFIED_CREATOR
        assert not members.is_thread_born_worker_session(WORKER)

    def test_a_verified_worker_creator_marks_no_grandchild(self, tmp_path, monkeypatch):
        slot = self._restore(tmp_path, monkeypatch, created_by="chat-3-1785396000", verified=True)
        assert slot._created_by == "chat-3-1785396000"
        assert not members.is_thread_born_worker_session(WORKER)

    @pytest.mark.parametrize("creator, thread_born", [(THREAD, True), ("chat-3-1785396000", False)])
    def test_a_fence_the_tree_lifts_later_marks_from_the_creator_it_confirms(
        self, tmp_path, monkeypatch, creator, thread_born
    ):
        """A restore that runs before the tree holds the creation record fences the
        worker and marks nothing. When the record arrives later in the same process, the
        tree push lifts the fence (``reconfirm_restored_creators``), and the creator the
        record names marks the worker as a restore that found the record would have."""
        from kiro_crew.dashboard import session_control as sc
        from kiro_crew.dashboard.session_control import UNVERIFIED_CREATOR

        slot = self._restore(tmp_path, monkeypatch, created_by=creator, verified=False)
        assert slot._created_by == UNVERIFIED_CREATOR
        assert not members.is_thread_born_worker_session(WORKER)

        monkeypatch.setattr(sc, "_creation_parent", lambda key: creator if key == WORKER else "")
        assert sc.reconfirm_restored_creators(SimpleNamespace(_slots={WORKER: slot})) == 1
        assert slot._created_by == creator
        assert members.is_thread_born_worker_session(WORKER) is thread_born
