"""Tests for subagent_persistence — agent folder CRUD.

This module pins the folder primitives: creating a folder with its canonical run agent and
app, its state, result and tombstone files, deleting it, listing orphans, the stale
tombstone prune and the delivered mark, plus path-traversal refusal, the slow-command log
and its rotation, a run's memory mode, and a runtime session's memory-mode binding, which
only tightens. The folder a ``SubagentManager`` run writes
from spawn to its end lives in ``test_subagent_persistence_spawn_lifecycle.py``, and what
a restart does with the folders it finds in ``test_subagent_persistence_orphans.py``; the
shared ``agent_root`` fixture in ``subagent_persistence_helpers.py``.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest
from subagent_persistence_helpers import (  # noqa: F401 -- the fixture is requested by name
    agent_root_fixture,
)

from kiro_crew.subagent_persistence import (
    _CLEANUP_IDENTITY_LOCK,
    _LIVE_CLEANUP_IDENTITIES,
    classify_persisted_ending,
    create_agent_folder,
    delete_agent_folder,
    list_orphans,
    mark_delivered,
    prune_stale_tombstones,
    read_run_agent_selection,
    read_run_app,
    read_state,
    read_tombstone,
    record_slow_command,
    remember_live_cleanup_identity,
    update_state,
    write_result_chunk,
    write_run_agent,
    write_tombstone,
)

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


# ── create_agent_folder ──────────────────────────────────────────────


class TestCreateAgentFolder:
    def test_creates_state_json(self, agent_root):
        path = create_agent_folder(
            "abc123",
            task="do stuff",
            agent="kirocrew",
            parent_session="dashboard:default",
            max_turns=100,
        )
        state = json.loads((path / "state.json").read_text(encoding="utf-8"))
        assert state["id"] == "abc123"
        assert state["task"] == "do stuff"
        assert state["agent"] == "kirocrew"
        # No ending writes a run status back to this file, so it carries none
        # that would read as running afterwards (the tombstone records it).
        assert "status" not in state
        assert state["max_turns"] == 100
        assert "started" in state

    def test_idempotent_on_existing_folder(self, agent_root):
        create_agent_folder("abc123", task="t1")
        path = create_agent_folder("abc123", task="t2")
        state = json.loads((path / "state.json").read_text(encoding="utf-8"))
        assert state["task"] == "t2"

    def test_a_stopped_run_does_not_read_as_running(self, agent_root):
        """The folder a user stop leaves: the tombstone records the stop and the
        run record says nothing that contradicts it."""
        path = create_agent_folder("stop0001", task="t", parent_session="dashboard:default")
        write_tombstone("stop0001", cause="user_stop", recovery_action="none", outcome="stopped")
        state = json.loads((path / "state.json").read_text(encoding="utf-8"))
        assert state.get("status") != "running"
        assert classify_persisted_ending(path) == ("stopped", "", True)


class TestCanonicalRunAgent:
    def test_template_uses_same_owner_record(self, agent_root):
        create_agent_folder("selected", agent="worker")
        update_state("selected", agent="display-only")
        assert read_run_agent_selection("selected") == ("template", "worker")
        assert read_state("selected")["execution_context"]["template_id"] == "worker"
        assert not (agent_root.parent / "member-memory-bindings").exists()

    @pytest.mark.parametrize(
        "kind,agent", [("unknown", "worker"), ("member", ""), (None, "worker"), ("template", [])]
    )
    def test_invalid_selection_cannot_replace_execution(self, agent_root, kind, agent):
        create_agent_folder("selected", agent="worker")
        with pytest.raises(ValueError):
            write_run_agent("selected", agent, kind=kind)
        assert read_run_agent_selection("selected") == ("template", "worker")

    def test_missing_owner_record_refuses(self, agent_root):
        folder = create_agent_folder("selected", agent="worker")
        (folder / "state.json").unlink()
        with pytest.raises(ValueError, match="unavailable"):
            read_run_agent_selection("selected")

    def test_template_override_preserves_memory_and_app(self, agent_root):
        create_agent_folder("selected", app="example-app")
        before = read_state("selected")["execution_context"]
        write_run_agent("selected", "worker")
        after = read_state("selected")["execution_context"]
        assert after["store"] == before["store"]
        assert after["app"] == before["app"]
        assert after["template_id"] == "worker"


class TestCanonicalRunApp:
    @pytest.mark.parametrize("app", ["", "example-app"])
    def test_app_attribution_is_in_owner_execution(self, agent_root, app):
        create_agent_folder("app-owner", app=app)
        update_state("app-owner", app="display-only")
        assert read_run_app("app-owner") == app
        assert read_state("app-owner")["execution_context"]["app"] == app

    @pytest.mark.parametrize("app", [None, 42, []])
    def test_invalid_app_cannot_become_person_owned(self, agent_root, app):
        folder = create_agent_folder("app-owner", app="example-app")
        record = read_state("app-owner")
        record["execution_context"]["app"] = app
        (folder / "state.json").write_text(json.dumps(record), encoding="utf-8")
        with pytest.raises(ValueError):
            read_run_app("app-owner")


# ── update_state ─────────────────────────────────────────────────────


class TestUpdateState:
    def test_updates_fields(self, agent_root):
        create_agent_folder("u1", task="t")
        update_state("u1", pid=12345, turns=5, last_tool="read")
        state = json.loads((agent_root / "u1" / "state.json").read_text(encoding="utf-8"))
        assert state["pid"] == 12345
        assert state["turns"] == 5
        assert state["last_tool"] == "read"

    def test_preserves_existing_fields(self, agent_root):
        create_agent_folder("u2", task="original")
        update_state("u2", pid=99)
        state = json.loads((agent_root / "u2" / "state.json").read_text(encoding="utf-8"))
        assert state["task"] == "original"
        assert state["pid"] == 99

    def test_corrupt_or_nonobject_state_skips_update(self, agent_root, monkeypatch):
        import kiro_crew.subagent_persistence as sp

        create_agent_folder("u-corrupt", task="t")
        path = agent_root / "u-corrupt" / "state.json"
        path.write_bytes(b"\xff")
        assert update_state("u-corrupt", pid=1) is False

        path.write_text("[]", encoding="utf-8")
        assert update_state("u-corrupt", pid=1) is False

        path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(sp.json, "loads", lambda _text: (_ for _ in ()).throw(RecursionError()))
        assert update_state("u-corrupt", pid=1) is False

    def test_missing_folder_logs_no_crash(self, agent_root):
        # The skip is reported, and no folder is fabricated for the unknown id.
        assert update_state("nonexistent", pid=1) is False
        assert not (agent_root / "nonexistent").exists()

    @pytest.mark.parametrize("contended", ["read", "rename"])
    def test_a_windows_sharing_violation_is_ridden_out(self, agent_root, monkeypatch, contended):
        """Off the loop, another handle holding state.json as ``update_state``
        reads it, or as its rewrite renames over it, is retried rather than a
        skipped or failed write: what lets a flag with a durability contract
        (``result_complete``) land with no retry loop of its own."""
        from pathlib import Path

        import kiro_crew.atomic_write as aw
        from kiro_crew import platform_compat

        create_agent_folder("u-win", task="t")
        target = agent_root / "u-win" / "state.json"
        contentions: list[str] = []

        def _contend(path) -> None:
            if Path(path) == target and not contentions:
                contentions.append(str(path))
                raise PermissionError(13, "The process cannot access the file")

        if contended == "read":
            real_read = aw._read_bytes

            def _read(path, max_bytes):
                _contend(path)
                return real_read(path, max_bytes)

            monkeypatch.setattr(aw, "_read_bytes", _read)
        else:
            real_replace = os.replace

            def _replace(src, dst):
                _contend(dst)
                return real_replace(src, dst)

            monkeypatch.setattr(aw.os, "replace", _replace)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0.0)

        assert update_state("u-win", result_complete=True) is True
        assert contentions, "the contention was never exercised"
        assert (read_state("u-win") or {}).get("result_complete") is True

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_the_state_file_stays_owner_only(self, agent_root):
        create_agent_folder("u-mode", task="t")
        update_state("u-mode", pid=1)
        assert (agent_root / "u-mode" / "state.json").stat().st_mode & 0o777 == 0o600


# ── read_state ───────────────────────────────────────────────────────


class TestReadState:
    def test_reads_state(self, agent_root):
        create_agent_folder("r1", task="hello")
        state = read_state("r1")
        assert state is not None
        assert state["task"] == "hello"

    def test_missing_returns_none(self, agent_root):
        assert read_state("nope") is None

    def test_corrupt_json_returns_none(self, agent_root):
        folder = agent_root / "bad"
        folder.mkdir()
        (folder / "state.json").write_text("{corrupt")
        assert read_state("bad") is None

    def test_invalid_encoding_depth_and_nonobject_return_none(self, agent_root, monkeypatch):
        import kiro_crew.subagent_persistence as sp

        folder = agent_root / "bad-shapes"
        folder.mkdir()
        path = folder / "state.json"
        path.write_bytes(b"\xff")
        assert read_state("bad-shapes") is None

        path.write_text("[]", encoding="utf-8")
        assert read_state("bad-shapes") is None

        path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            sp.json,
            "loads",
            lambda _text: (_ for _ in ()).throw(RecursionError()),
        )
        assert read_state("bad-shapes") is None


class TestReadTombstone:
    def test_reads_only_valid_tombstone_objects(self, agent_root):
        create_agent_folder("rt1", task="t")
        assert read_tombstone("rt1") is None

        write_tombstone(
            "rt1",
            cause="timeout",
            recovery_action="notified",
            session_id="sid-read",
        )
        assert (read_tombstone("rt1") or {})["session_id"] == "sid-read"

        path = agent_root / "rt1" / "tombstone.json"
        path.write_text("{corrupt", encoding="utf-8")
        assert read_tombstone("rt1") is None
        path.write_text("[]", encoding="utf-8")
        assert read_tombstone("rt1") is None
        path.write_text("[" * 1100 + "0" + "]" * 1100, encoding="utf-8")
        assert read_tombstone("rt1") is None


# ── write_result_chunk ───────────────────────────────────────────────


class TestWriteResultChunk:
    def test_appends_text(self, agent_root):
        create_agent_folder("w1", task="t")
        write_result_chunk("w1", "hello ")
        write_result_chunk("w1", "world")
        content = (agent_root / "w1" / "result.txt").read_text(encoding="utf-8")
        assert content == "hello world"

    def test_a_fresh_chunk_starts_the_file_over_and_nothing_is_translated(self, agent_root):
        """An attempt's first chunk replaces what an earlier attempt left, and
        the bytes land as written, LF on every platform."""
        create_agent_folder("w2", task="t")
        assert write_result_chunk("w2", "earlier attempt ")
        assert write_result_chunk("w2", "line\n", fresh=True)
        assert write_result_chunk("w2", "next\r\n")
        assert (agent_root / "w2" / "result.txt").read_bytes() == b"line\nnext\r\n"

    def test_a_fresh_chunk_that_cannot_be_written_keeps_the_earlier_partial(
        self, agent_root, monkeypatch
    ):
        """The fresh write stages its text before replacing the file, so a disk
        that refuses it (full, quota) costs nothing: the earlier attempt's
        partial is still there to read, and the call says nothing was written."""
        import kiro_crew.atomic_write as aw

        create_agent_folder("w3", task="t")
        assert write_result_chunk("w3", "an earlier attempt's partial")

        def _disk_full(fd, data, path):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(aw, "_write_all", _disk_full)
        assert write_result_chunk("w3", "the respawn's first chunk", fresh=True) is False
        assert (agent_root / "w3" / "result.txt").read_text(
            encoding="utf-8"
        ) == "an earlier attempt's partial"
        assert [p.name for p in (agent_root / "w3").iterdir() if p.suffix == ".tmp"] == []

    def test_a_failed_write_says_so(self, agent_root):
        assert write_result_chunk("never-created", "text") is False
        assert write_result_chunk("never-created", "text", fresh=True) is False
        assert not (agent_root / "never-created").exists()


# ── write_finished_result ────────────────────────────────────────────


class TestWriteFinishedResult:
    def test_a_whole_answer_is_rewritten_from_memory_and_flagged(self, agent_root, monkeypatch):
        import kiro_crew.context_management as cm
        from kiro_crew.subagent_persistence import write_finished_result

        monkeypatch.setattr(cm, "RESULT_FILE_MAX_BYTES", 2_000)
        create_agent_folder("f1", task="t")
        write_result_chunk("f1", "a fragment with a hole")
        answer = "x" * 5_000

        assert write_finished_result("f1", answer, update_state) is True
        assert (agent_root / "f1" / "result.txt").read_bytes() == cm.cap_result_bytes(
            answer.encode("utf-8")
        )
        assert (read_state("f1") or {}).get("result_complete") is True

    def test_an_empty_whole_answer_leaves_no_file(self, agent_root):
        from kiro_crew.subagent_persistence import write_finished_result

        create_agent_folder("f2", task="t")
        write_result_chunk("f2", "an earlier attempt's partial")

        assert write_finished_result("f2", "", update_state) is True
        assert not (agent_root / "f2" / "result.txt").exists()
        assert (read_state("f2") or {}).get("result_complete") is True

    def test_any_other_ending_is_capped_in_place_and_not_flagged(self, agent_root, monkeypatch):
        import kiro_crew.context_management as cm
        from kiro_crew.subagent_persistence import write_finished_result

        monkeypatch.setattr(cm, "RESULT_FILE_MAX_BYTES", 2_000)
        create_agent_folder("f3", task="t")
        write_result_chunk("f3", "y" * 9_000)

        assert write_finished_result("f3", None, update_state) is True
        on_disk = (agent_root / "f3" / "result.txt").read_bytes()
        assert on_disk == cm.cap_result_bytes(b"y" * 9_000)
        assert (read_state("f3") or {}).get("result_complete") is False

    @pytest.mark.parametrize("text", ["z" * 9_000, None], ids=["whole", "partial"])
    def test_a_refused_rename_still_leaves_the_file_capped(self, agent_root, monkeypatch, text):
        """A rename result.txt cannot take -- a Windows reader holding it, a
        full disk refusing the temp file -- still leaves it within the bound:
        the cap is written in place, and the whole answer stays unflagged."""
        from pathlib import Path

        import kiro_crew.context_management as cm
        import kiro_crew.subagent_persistence as sp
        from kiro_crew.subagent_persistence import write_finished_result

        monkeypatch.setattr(cm, "RESULT_FILE_MAX_BYTES", 2_000)
        real_replace = os.replace

        def _refuse_result(src, dst, *a, **kw):
            if Path(dst).name == "result.txt":
                raise PermissionError(13, "The process cannot access the file")
            return real_replace(src, dst, *a, **kw)

        def _refuse_whole(path, *a, **kw):
            if Path(path).name == "result.txt":
                raise PermissionError(13, "The process cannot access the file")
            return real_atomic(path, *a, **kw)

        real_atomic = sp.atomic_write
        monkeypatch.setattr(os, "replace", _refuse_result)
        monkeypatch.setattr(sp, "atomic_write", _refuse_whole)
        create_agent_folder("f4", task="t")
        write_result_chunk("f4", "z" * 9_000)

        assert write_finished_result("f4", text, update_state) is True
        on_disk = (agent_root / "f4" / "result.txt").read_bytes()
        assert on_disk == cm.cap_result_bytes(b"z" * 9_000)
        assert (read_state("f4") or {}).get("result_complete") is False

    def test_a_cleared_folder_is_not_recreated_or_flagged(self, agent_root):
        from kiro_crew.subagent_persistence import write_finished_result

        assert write_finished_result("gone1", "the answer", update_state) is False
        assert not (agent_root / "gone1").exists()


# ── write_tombstone ──────────────────────────────────────────────────


class TestWriteTombstone:
    def test_writes_tombstone_json(self, agent_root):
        create_agent_folder("t1", task="t")
        write_tombstone("t1", cause="timeout", recovery_action="notified_slack")
        ts = json.loads((agent_root / "t1" / "tombstone.json").read_text(encoding="utf-8"))
        assert ts["cause"] == "timeout"
        assert ts["recovery_action"] == "notified_slack"
        assert "died" in ts

    def test_extra_fields_included(self, agent_root):
        create_agent_folder("t2", task="t")
        write_tombstone("t2", cause="reaped", recovery_action="delivered", pid=999, turns=12)
        ts = json.loads((agent_root / "t2" / "tombstone.json").read_text(encoding="utf-8"))
        assert ts["pid"] == 999
        assert ts["turns"] == 12

    def test_live_identity_snapshot_never_reads_sidecar(self, agent_root):
        from unittest.mock import patch

        import kiro_crew.subagent_persistence as sp

        agent_id = "t-memory-only-snapshot"
        create_agent_folder(agent_id, task="t")
        with _CLEANUP_IDENTITY_LOCK:
            _LIVE_CLEANUP_IDENTITIES[agent_id] = [{"session_id": "sid-memory", "provider": "acp"}]
        with patch.object(
            sp,
            "_read_cleanup_identities_file",
            side_effect=AssertionError("event-loop snapshot read sidecar"),
        ):
            write_tombstone(agent_id, cause="error", recovery_action="none")
        assert (read_tombstone(agent_id) or {})["session_id"] == "sid-memory"

    def test_live_identity_snapshot_does_not_wait_and_delete_clears_fallback(self, agent_root):
        agent_id = "t-identity-lock"
        create_agent_folder(agent_id, task="t")
        with _CLEANUP_IDENTITY_LOCK:
            _LIVE_CLEANUP_IDENTITIES[agent_id] = [{"session_id": "sid-lock", "provider": "acp"}]
            write_tombstone(agent_id, cause="error", recovery_action="none")
        assert (read_tombstone(agent_id) or {})["session_id"] == "sid-lock"

        delete_id = "t-delete-fallback"
        create_agent_folder(delete_id, task="t")
        with _CLEANUP_IDENTITY_LOCK:
            _LIVE_CLEANUP_IDENTITIES[delete_id] = [{"session_id": "sid-delete"}]
        delete_agent_folder(delete_id)
        assert delete_id not in _LIVE_CLEANUP_IDENTITIES

    def test_live_identity_published_before_sidecar_read(self, agent_root):
        from unittest.mock import patch

        import kiro_crew.subagent_persistence as sp

        agent_id = "t-prepublish"
        create_agent_folder(agent_id, task="t")
        entered_read = threading.Event()
        allow_read = threading.Event()
        errors: list[BaseException] = []
        original_read = sp._read_cleanup_identities_file

        def blocked_read(run_id: str):
            entered_read.set()
            assert allow_read.wait(timeout=5)
            return original_read(run_id)

        def remember() -> None:
            try:
                sp.remember_live_cleanup_identity(
                    agent_id, session_id="sid-prepublish", provider="acp"
                )
            except BaseException as exc:  # surfaced after unconditional join
                errors.append(exc)

        worker = threading.Thread(target=remember)
        with patch.object(sp, "_read_cleanup_identities_file", blocked_read):
            worker.start()
            assert entered_read.wait(timeout=5)
            try:
                write_tombstone(agent_id, cause="error", recovery_action="none")
                assert (read_tombstone(agent_id) or {})["session_id"] == "sid-prepublish"
                sp.publish_live_cleanup_identity(
                    agent_id,
                    session_id="sid-concurrent",
                    provider="acp",
                )
            finally:
                allow_read.set()
                worker.join(timeout=5)
        assert not worker.is_alive()
        assert errors == []
        durable = json.loads(sp._cleanup_identities_path(agent_id).read_text())
        assert [item["session_id"] for item in durable["identities"]] == [
            "sid-prepublish",
            "sid-concurrent",
        ]

    def test_protected_record_stays_inside_test_temp_root(self, agent_root):
        import kiro_crew.subagent_persistence as sp

        agent_id = "protected-test-root"
        create_agent_folder(agent_id, task="t")
        sp.remember_live_cleanup_identity(
            agent_id,
            session_id="sid-contained",
            provider="acp",
            keep=False,
        )
        protected_path = sp._cleanup_identities_path(agent_id)
        assert protected_path.is_relative_to(agent_root.parent)
        assert protected_path.exists()

    def test_concurrent_publish_during_sidecar_write_is_not_overwritten(self, agent_root):
        from unittest.mock import patch

        import kiro_crew.subagent_persistence as sp

        agent_id = "t-append-only-publish"
        create_agent_folder(agent_id, task="t")
        entered_write = threading.Event()
        allow_write = threading.Event()
        errors: list[BaseException] = []
        original_write = sp._atomic_write

        def blocked_write(path, data):  # type: ignore[no-untyped-def]
            if path.name == sp._CLEANUP_IDENTITIES_FILE:
                entered_write.set()
                assert allow_write.wait(timeout=5)
            return original_write(path, data)

        def remember_first() -> None:
            try:
                sp.remember_live_cleanup_identity(
                    agent_id,
                    session_id="sid-first",
                    provider="acp",
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=remember_first)
        with patch.object(sp, "_atomic_write", side_effect=blocked_write):
            worker.start()
            assert entered_write.wait(timeout=5)
            try:
                sp.publish_live_cleanup_identity(
                    agent_id,
                    session_id="sid-second",
                    provider="acp",
                )
                write_tombstone(agent_id, cause="error", recovery_action="none")
            finally:
                allow_write.set()
                worker.join(timeout=5)

        assert not worker.is_alive()
        assert errors == []
        tombstone = read_tombstone(agent_id) or {}
        assert [item["session_id"] for item in tombstone["cleanup_identities"]] == [
            "sid-first",
            "sid-second",
        ]
        assert [item["session_id"] for item in sp._live_cleanup_identities(agent_id)] == [
            "sid-first",
            "sid-second",
        ]

    def test_snapshots_cleanup_identity_from_readable_state(self, agent_root):
        create_agent_folder("t3", task="t")
        update_state("t3", session_id="sid-state", provider="claude_code", cwd="/project")
        write_tombstone("t3", cause="gateway_restart", recovery_action="delivered")
        ts = json.loads((agent_root / "t3" / "tombstone.json").read_text(encoding="utf-8"))
        assert ts["session_id"] == "sid-state"
        assert ts["provider"] == "claude_code"
        assert ts["cwd"] == "/project"


# ── delete_agent_folder ──────────────────────────────────────────────


class TestDeleteAgentFolder:
    def test_removes_folder(self, agent_root):
        create_agent_folder("d1", task="t")
        assert (agent_root / "d1").exists()
        delete_agent_folder("d1")
        assert not (agent_root / "d1").exists()

    def test_missing_folder_no_crash(self, agent_root):
        create_agent_folder("kept", task="t")
        delete_agent_folder("ghost")
        assert not (agent_root / "ghost").exists()
        assert read_state("kept")["task"] == "t"


# ── list_orphans ─────────────────────────────────────────────────────


class TestListOrphans:
    def test_returns_non_tombstoned_folders(self, agent_root):
        create_agent_folder("alive1", task="t1")
        create_agent_folder("alive2", task="t2")
        create_agent_folder("dead1", task="t3")
        write_tombstone("dead1", cause="timeout", recovery_action="delivered")
        orphans = list_orphans()
        ids = [o["id"] for o in orphans]
        assert "alive1" in ids
        assert "alive2" in ids
        assert "dead1" not in ids

    def test_skips_corrupt_state(self, agent_root):
        folder = agent_root / "corrupt1"
        folder.mkdir()
        (folder / "state.json").write_text("not json")
        orphans = list_orphans()
        assert len(orphans) == 0


# ── prune_stale_tombstones ───────────────────────────────────────────


class TestPruneStaleTombstones:
    def test_prunes_old_tombstones(self, agent_root):
        create_agent_folder("old1", task="t")
        write_tombstone("old1", cause="timeout", recovery_action="delivered")
        # Backdate the tombstone
        ts_path = agent_root / "old1" / "tombstone.json"
        ts = json.loads(ts_path.read_text(encoding="utf-8"))
        ts["died"] = time.time() - (8 * 86400)  # 8 days ago
        ts_path.write_text(json.dumps(ts))

        prune_stale_tombstones(max_age_days=7)
        assert not (agent_root / "old1").exists()

    def test_keeps_recent_tombstones(self, agent_root):
        create_agent_folder("new1", task="t")
        write_tombstone("new1", cause="timeout", recovery_action="delivered")
        prune_stale_tombstones(max_age_days=7)
        assert (agent_root / "new1").exists()

    @pytest.mark.parametrize(
        "died_case",
        ["string", "nan", "infinity", "future", "oversized"],
    )
    def test_invalid_tombstone_died_uses_mtime_fallback(self, agent_root, monkeypatch, died_case):
        agent_id = f"invalid-died-{died_case}"
        create_agent_folder(agent_id, task="t")
        update_state(agent_id, session_id="sid-died", provider="acp", keep=False)
        remember_live_cleanup_identity(
            agent_id,
            session_id="sid-died",
            provider="acp",
            keep=False,
        )
        write_tombstone(
            agent_id,
            cause="delivered",
            recovery_action="notified",
            session_id="sid-died",
        )
        ts_path = agent_root / agent_id / "tombstone.json"
        ts = json.loads(ts_path.read_text(encoding="utf-8"))
        ts["died"] = {
            "string": "invalid",
            "nan": float("nan"),
            "infinity": float("inf"),
            "future": time.time() + 86400,
            "oversized": 10**400,
        }[died_case]
        ts_path.write_text(json.dumps(ts), encoding="utf-8")
        fallback = time.time() - (2 * 86400)
        os.utime(ts_path, (fallback, fallback))

        cleaned: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.subagent_persistence._cleanup_session_files_sync",
            lambda sid, provider, *, cwd="": cleaned.append(sid),
        )
        assert prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1
        assert cleaned == ["sid-died"]
        assert not (agent_root / agent_id).exists()

    def test_mtime_fallback_at_cutoff_is_eligible(self, agent_root, monkeypatch):
        import kiro_crew.subagent_persistence as sp

        agent_id = "mtime-at-cutoff"
        create_agent_folder(agent_id, task="t")
        update_state(agent_id, session_id="sid-cutoff", provider="acp", keep=False)
        remember_live_cleanup_identity(
            agent_id,
            session_id="sid-cutoff",
            provider="acp",
            keep=False,
        )
        write_tombstone(
            agent_id,
            cause="delivered",
            recovery_action="notified",
            session_id="sid-cutoff",
        )
        ts_path = agent_root / agent_id / "tombstone.json"
        ts = json.loads(ts_path.read_text(encoding="utf-8"))
        ts["died"] = 0
        ts_path.write_text(json.dumps(ts), encoding="utf-8")
        os.utime(ts_path, (100.0, 100.0))
        monkeypatch.setattr(sp.time, "time", lambda: 100.0)

        cleaned: list[str] = []
        monkeypatch.setattr(
            sp,
            "_cleanup_session_files_sync",
            lambda sid, provider, *, cwd="": cleaned.append(sid),
        )
        assert prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1
        assert cleaned == ["sid-cutoff"]
        assert not (agent_root / agent_id).exists()

    def test_future_died_and_mtime_preserve_unreadable_state_grace(self, agent_root, monkeypatch):
        """Clock rollback must not turn unknown retention into immediate cleanup."""
        import kiro_crew.subagent_persistence as sp

        agent_id = "future-died-and-mtime"
        create_agent_folder(agent_id, task="t")
        update_state(agent_id, session_id="sid-future", provider="acp", keep=False)
        write_tombstone(
            agent_id,
            cause="delivered",
            recovery_action="notified",
            session_id="sid-future",
        )
        state_path = agent_root / agent_id / "state.json"
        state_path.write_text("{corrupt", encoding="utf-8")
        ts_path = agent_root / agent_id / "tombstone.json"
        ts = json.loads(ts_path.read_text(encoding="utf-8"))
        ts["died"] = 200.0
        ts_path.write_text(json.dumps(ts), encoding="utf-8")
        os.utime(ts_path, (200.0, 200.0))
        monkeypatch.setattr(sp.time, "time", lambda: 100.0)

        cleaned: list[str] = []
        monkeypatch.setattr(
            sp,
            "_cleanup_session_files_sync",
            lambda sid, provider, *, cwd="": cleaned.append(sid),
        )
        assert prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 0
        assert cleaned == []
        assert (agent_root / agent_id).exists()

    @pytest.mark.parametrize(
        ("state_case", "expect_cleanup"),
        [
            pytest.param("partial-nonkeep", True, id="partial-state-cleans"),
            pytest.param(
                "initial-keep-write-missed", True, id="missing-keep-in-readable-state-cleans"
            ),
            pytest.param("corrupt", False, id="corrupt-state-skips-cleanup"),
            pytest.param("non-object", False, id="non-object-state-skips-cleanup"),
            pytest.param("promoted-keep", False, id="promoted-state-retains"),
            pytest.param("string-false", True, id="string-false-cleans"),
        ],
    )
    def test_prune_uses_trusted_identity_with_safe_live_state(
        self, agent_root, monkeypatch, state_case, expect_cleanup
    ):
        """Gateway publication supplies identity; live state owns retention intent."""
        agent_id = "retention-case"
        session_id = "session-retention"
        create_agent_folder(agent_id, task="t")
        if state_case == "initial-keep-write-missed":
            update_state(agent_id, provider="acp")
        else:
            update_state(agent_id, provider="acp", keep=False)
        remember_live_cleanup_identity(
            agent_id,
            session_id=session_id,
            provider="acp",
            keep=False,
        )
        write_tombstone(
            agent_id,
            cause="timeout",
            recovery_action="notified",
            session_id=session_id,
        )

        state_path = agent_root / agent_id / "state.json"
        if state_case == "corrupt":
            state_path.write_text("{corrupt", encoding="utf-8")
        elif state_case == "non-object":
            state_path.write_text("[]", encoding="utf-8")
        elif state_case == "promoted-keep":
            update_state(agent_id, keep=True)
        elif state_case == "string-false":
            update_state(agent_id, keep="false")

        ts_path = agent_root / agent_id / "tombstone.json"
        ts = json.loads(ts_path.read_text(encoding="utf-8"))
        ts["died"] = time.time() - ((7 * 86400) + (12 * 3600))
        ts_path.write_text(json.dumps(ts), encoding="utf-8")
        cleaned: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.subagent_persistence._cleanup_session_files_sync",
            lambda sid, provider, *, cwd="": cleaned.append(sid),
        )

        invalid_state = state_case in {"corrupt", "non-object"}
        bounded_grace = invalid_state
        if bounded_grace:
            assert prune_stale_tombstones(max_age_days=7) == 0
            assert cleaned == []
            assert (agent_root / agent_id).exists()
            quarantined = json.loads(ts_path.read_text(encoding="utf-8"))
            assert "state_unreadable_at" not in quarantined
            quarantined["died"] = time.time() - (9 * 86400)
            ts_path.write_text(json.dumps(quarantined), encoding="utf-8")

            assert prune_stale_tombstones(max_age_days=7) == 1
            assert cleaned == [session_id]
            assert not (agent_root / agent_id).exists()
        elif state_case == "promoted-keep":
            assert prune_stale_tombstones(max_age_days=7) == 0
            assert cleaned == []
            assert (agent_root / agent_id).exists()
            update_state(agent_id, keep=False)
            assert prune_stale_tombstones(max_age_days=7) == 1
            assert cleaned == [session_id]
            assert not (agent_root / agent_id).exists()
        else:
            assert prune_stale_tombstones(max_age_days=7) == 1
            assert cleaned == ([session_id] if expect_cleanup else [])
            assert not (agent_root / agent_id).exists()

    @pytest.mark.parametrize(
        ("state_case", "expected_cleanup"),
        [
            pytest.param("corrupt", [], id="unreadable-without-tombstone-id"),
            pytest.param("readable", ["sid-state-only"], id="state-only-id-no-grace"),
        ],
    )
    def test_extra_grace_requires_trusted_cleanup_identity(
        self, agent_root, monkeypatch, state_case, expected_cleanup
    ):
        agent_id = f"no-tombstone-id-{state_case}"
        create_agent_folder(agent_id, task="t")
        write_tombstone(agent_id, cause="timeout", recovery_action="notified")
        update_state(agent_id, session_id="sid-state-only", provider="acp")
        if state_case == "readable":
            remember_live_cleanup_identity(
                agent_id,
                session_id="sid-state-only",
                provider="acp",
                keep=False,
            )
        state_path = agent_root / agent_id / "state.json"
        if state_case == "corrupt":
            state_path.write_text("{corrupt", encoding="utf-8")

        ts_path = agent_root / agent_id / "tombstone.json"
        ts = json.loads(ts_path.read_text(encoding="utf-8"))
        assert "session_id" not in ts
        ts["died"] = time.time() - (8 * 86400)
        ts_path.write_text(json.dumps(ts), encoding="utf-8")
        cleaned: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.subagent_persistence._cleanup_session_files_sync",
            lambda sid, provider, *, cwd="": cleaned.append(sid),
        )

        assert prune_stale_tombstones(max_age_days=7) == 1
        assert cleaned == expected_cleanup
        assert not (agent_root / agent_id).exists()

    def test_keeps_non_tombstoned_folders(self, agent_root):
        create_agent_folder("running1", task="t")
        prune_stale_tombstones(max_age_days=7)
        assert (agent_root / "running1").exists()

    def test_delivered_pruned_after_ttl(self, agent_root):
        # A delivered result older than the (short) delivered TTL is pruned even
        # though it is far younger than the 7-day default window.
        create_agent_folder("del1", task="t")
        mark_delivered("del1")
        ts_path = agent_root / "del1" / "tombstone.json"
        ts = json.loads(ts_path.read_text(encoding="utf-8"))
        ts["died"] = time.time() - 7200  # 2h ago
        ts_path.write_text(json.dumps(ts))
        prune_stale_tombstones(max_age_days=7, delivered_ttl_secs=3600)
        assert not (agent_root / "del1").exists()

    def test_delivered_kept_within_ttl(self, agent_root):
        create_agent_folder("del2", task="t")
        mark_delivered("del2")
        prune_stale_tombstones(max_age_days=7, delivered_ttl_secs=3600)
        assert (agent_root / "del2").exists()

    def test_non_delivered_kept_past_delivered_ttl(self, agent_root):
        # A non-delivered (timeout) tombstone uses the 7-day window, so a 2h-old
        # one survives even a 1h delivered TTL — proves the per-cause cutoff.
        create_agent_folder("err1", task="t")
        write_tombstone("err1", cause="timeout", recovery_action="notified")
        ts_path = agent_root / "err1" / "tombstone.json"
        ts = json.loads(ts_path.read_text(encoding="utf-8"))
        ts["died"] = time.time() - 7200  # 2h ago
        ts_path.write_text(json.dumps(ts))
        prune_stale_tombstones(max_age_days=7, delivered_ttl_secs=3600)
        assert (agent_root / "err1").exists()


class TestMarkDelivered:
    def test_writes_delivered_tombstone(self, agent_root):
        create_agent_folder("mv1", task="t")
        write_result_chunk("mv1", "final output")
        mark_delivered("mv1")
        ts = json.loads((agent_root / "mv1" / "tombstone.json").read_text(encoding="utf-8"))
        assert ts["cause"] == "delivered"
        assert ts["recovery_action"] == "delivered"
        assert ts["result_available"] is True

    def test_preserves_terminal_usage(self, agent_root):
        create_agent_folder("mv-usage", task="t")
        mark_delivered("mv-usage", elapsed=12.5, credits=0.75)
        ts = json.loads((agent_root / "mv-usage" / "tombstone.json").read_text(encoding="utf-8"))
        assert ts["elapsed"] == 12.5
        assert ts["credits"] == 0.75

    def test_delivered_excluded_from_orphans(self, agent_root):
        create_agent_folder("mv2", task="t")
        mark_delivered("mv2")
        assert "mv2" not in [o["id"] for o in list_orphans()]


# ── Path traversal protection ────────────────────────────────────────


class TestPathTraversal:
    @pytest.mark.parametrize(
        "agent_id",
        [
            pytest.param(".", id="test_dot_agent_id_rejected"),
            pytest.param("..", id="test_dotdot_agent_id_rejected"),
            pytest.param("../etc", id="test_slash_agent_id_rejected"),
        ],
    )
    def test_traversing_agent_id_rejected(self, agent_root, agent_id):
        from kiro_crew.subagent_persistence import _agent_dir

        with pytest.raises(ValueError, match="Invalid agent_id"):
            _agent_dir(agent_id)


# ── record_slow_command ──────────────────────────────────────────────


class TestRecordSlowCommand:
    def test_appends_jsonl_entry(self, agent_root):
        record_slow_command("ag1", last_tool="fs_read", idle_secs=200, turns=2)
        log = agent_root / "slow_commands.jsonl"
        assert log.exists()  # NOT a tombstone — a separate analysis log
        assert not (agent_root / "ag1" / "tombstone.json").exists()
        entry = json.loads(log.read_text(encoding="utf-8").strip())
        assert entry["id"] == "ag1"
        assert entry["last_tool"] == "fs_read"
        assert entry["idle_secs"] == 200
        assert "flagged" in entry

    def test_appends_multiple_lines(self, agent_root):
        record_slow_command("ag1", idle_secs=200)
        record_slow_command("ag2", idle_secs=300)
        lines = (
            (agent_root / "slow_commands.jsonl").read_text(encoding="utf-8").strip().splitlines()
        )
        assert len(lines) == 2
        assert {json.loads(lines[0])["id"], json.loads(lines[1])["id"]} == {"ag1", "ag2"}


# ── record_slow_command rotation ─────────────────────────────────────


def _slow_log_records(path):
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").strip().splitlines() if line
    ]


class TestRecordSlowCommandRotation:
    """The log rotates at the size cap — bounded disk, no lost record, no wait.

    Same shape as the ``stub_fallback.jsonl`` rotation in
    ``mcp_gateway.stub``: O(1) rotate-by-rename before the append, guarded
    by a non-blocking try-lock, all inside the best-effort ``except OSError``.
    """

    CAP = 400  # bytes — small enough to cross with a handful of records

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse size cap left as is; no test in this module calls monkeypatch.undo()
    def small_cap(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.subagent_persistence._SLOW_LOG_MAX_BYTES", self.CAP)

    def test_rotation_keeps_every_record(self, agent_root):
        """One rotation: older records land in ``.jsonl.1``, none are lost."""
        live = agent_root / "slow_commands.jsonl"
        rotated = agent_root / "slow_commands.jsonl.1"
        n = 0
        while not rotated.exists():
            record_slow_command(f"ag{n}", idle_secs=200)
            n += 1
            assert n < 100, "cap never triggered a rotation"
        # The rotated generation holds the pre-rotation records intact...
        old_ids = [r["id"] for r in _slow_log_records(rotated)]
        assert old_ids == [f"ag{i}" for i in range(n - 1)]
        # ...and the live file holds exactly the one record written after.
        new_ids = [r["id"] for r in _slow_log_records(live)]
        assert new_ids == [f"ag{n - 1}"]
        # Live file restarted under the cap.
        assert live.stat().st_size < self.CAP

    def test_total_bytes_stay_bounded(self, agent_root):
        """The property the issue is about: many writes, bounded total disk.

        One rotation alone does not prove boundedness — total bytes across
        BOTH generations must stay bounded no matter how many records land.
        """
        for i in range(300):
            record_slow_command(f"agent-{i:04d}", idle_secs=200, turns=3)
        live = agent_root / "slow_commands.jsonl"
        rotated = agent_root / "slow_commands.jsonl.1"
        # Each generation may overshoot the cap by at most one record (the
        # size check runs before the append), so bound each at CAP plus a
        # generous one-record slack.
        slack = 200
        assert live.stat().st_size <= self.CAP + slack
        assert rotated.exists()
        assert rotated.stat().st_size <= self.CAP + slack
        total = live.stat().st_size + rotated.stat().st_size
        assert total <= 2 * (self.CAP + slack)

    def test_rotation_failure_still_appends(self, agent_root):
        """Best-effort contract: a failing rotation never drops the record
        and never propagates into the (event-loop) caller.

        A directory squatting on the rotation target makes ``os.replace``
        raise a REAL ``OSError`` on both POSIX and Windows — no stdlib
        patching, which would leak process-wide to concurrent renamers.
        """
        live = agent_root / "slow_commands.jsonl"
        live.write_text("x" * (self.CAP + 10), encoding="utf-8")
        (agent_root / "slow_commands.jsonl.1").mkdir()

        record_slow_command("ag-after-fail", idle_secs=200)  # must not raise
        assert (agent_root / "slow_commands.jsonl.1").is_dir()
        assert "ag-after-fail" in live.read_text(encoding="utf-8")

    def test_lock_open_failure_still_appends(self, agent_root):
        """A lock-file open failure (fd exhaustion, restrictive dir ACL)
        degrades to append-without-rotating — never to a dropped record.
        Fd/disk exhaustion is a leading cause of the very stalls this log
        diagnoses, so that is exactly when the record must still land.

        A directory squatting on the lock path makes ``os.open(O_RDWR)``
        raise a REAL ``OSError`` (EISDIR/EACCES) — no stdlib patching."""
        live = agent_root / "slow_commands.jsonl"
        live.write_text("x" * (self.CAP + 10), encoding="utf-8")
        (agent_root / "slow_commands.jsonl.lock").mkdir()

        record_slow_command("ag-no-lock", idle_secs=200)  # must not raise
        assert not (agent_root / "slow_commands.jsonl.1").exists()
        assert "ag-no-lock" in live.read_text(encoding="utf-8")

    def test_held_lock_never_blocks_writer(self, agent_root):
        """A writer that loses the try-lock appends WITHOUT rotating and
        WITHOUT waiting — a blocking acquire here would stall the gateway
        event loop (the caller is async ``_maybe_flag_stall``)."""
        import threading

        from kiro_crew import platform_compat

        live = agent_root / "slow_commands.jsonl"
        live.write_text("x" * (self.CAP + 10), encoding="utf-8")
        lock_fd = os.open(agent_root / "slow_commands.jsonl.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            assert platform_compat.try_acquire_lock(lock_fd, exclusive=True)
            done = threading.Event()

            def write():
                record_slow_command("ag-locked-out", idle_secs=200)
                done.set()

            t = threading.Thread(target=write, daemon=True)
            t.start()
            t.join(timeout=10)
            assert done.is_set(), "writer blocked on a held rotation lock"
            # Lock loser appended (over the cap) but did not rotate.
            assert not (agent_root / "slow_commands.jsonl.1").exists()
            assert "ag-locked-out" in live.read_text(encoding="utf-8")
        finally:
            platform_compat.release_lock(lock_fd)
            os.close(lock_fd)


class TestCanonicalMemoryMode:
    @pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
    def test_recreation_keeps_original_restriction(self, agent_root, mode):
        from kiro_crew.subagent_persistence import read_run_memory_mode

        folder = create_agent_folder("privacy-mode", memory_mode=mode)
        create_agent_folder("privacy-mode", memory_mode="persistent")
        assert read_run_memory_mode("privacy-mode") == mode
        assert (folder / "state.json").exists() == (mode == "persistent")

    def test_tightening_survives_restart_without_new_body(self, agent_root):
        from kiro_crew import subagent_persistence as persistence

        folder = create_agent_folder(
            "privacy-tighten", task="original persisted body", app="example-app"
        )
        assert persistence.tighten_run_memory_mode("privacy-tighten", "temporary") == "temporary"
        update_state("privacy-tighten", task="restricted new body")
        durable = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        assert durable["task"] == "original persisted body"
        persistence._LIVE_RUN_STATES.clear()
        assert persistence.read_run_memory_mode("privacy-tighten") == "temporary"
        assert read_run_app("privacy-tighten") == "example-app"

    def test_malformed_mode_cannot_default(self, agent_root):
        folder = create_agent_folder("privacy-damaged")
        state = read_state("privacy-damaged")
        state["execution_context"]["memory_mode"] = "unknown"
        (folder / "state.json").write_text(json.dumps(state), encoding="utf-8")
        from kiro_crew.subagent_persistence import read_run_memory_mode

        with pytest.raises(ValueError):
            read_run_memory_mode("privacy-damaged")


@pytest.mark.parametrize("original", ["persistent", "incognito", "temporary"])
@pytest.mark.parametrize("requested", ["persistent", "incognito", "temporary"])
def test_runtime_mode_binding_only_tightens(agent_root, original, requested):
    from kiro_crew.messaging.privacy_mode import strictest
    from kiro_crew.subagent_persistence import bind_session_memory_mode, read_session_memory_mode

    key = "taskrunner:mode-composition:runtime"
    assert bind_session_memory_mode(key, original) == original
    expected = strictest((original, requested)) or "persistent"
    assert bind_session_memory_mode(key, requested) == expected
    assert bind_session_memory_mode(key, "persistent") == expected
    assert read_session_memory_mode(key) == expected


def test_restricted_runtime_record_never_calls_disk_writer(agent_root, monkeypatch):
    from kiro_crew.subagent_persistence import bind_session_memory_mode

    def fail(*args, **kwargs):
        raise AssertionError("restricted record attempted a disk write")

    monkeypatch.setattr("kiro_crew.subagent_persistence._atomic_write", fail)
    assert bind_session_memory_mode("taskrunner:restricted:runtime", "temporary") == "temporary"
