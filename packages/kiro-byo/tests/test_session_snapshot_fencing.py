"""ACP session snapshot fencing and concurrent-prompt fault regressions."""
from __future__ import annotations

import copy
import json
import multiprocessing as mp
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_byo.acp_server import AcpServer, Session, SessionBusyError
from kiro_byo.session_events import append_message, derive_session_projection
from kiro_byo.state_io import _file_transaction_lock


def _stale_writer(home: str, raw_record: dict, tag: str, start, out) -> None:
    from kiro_byo import acp_server
    acp_server.home_dir = lambda: Path(home)
    server = AcpServer(stdin=StringIO(), stdout=StringIO())
    try:
        session = Session(
            session_id=raw_record["session_id"], cwd=raw_record["cwd"],
            model=raw_record["model"],
            events=copy.deepcopy(raw_record["events"]),
            messages=derive_session_projection(raw_record["events"]).messages,
            storage_loaded=True,
            storage_revision=raw_record["storage_revision"],
        )
        append_message(session, {"role": "user", "content": tag})
        assert start.wait(8)
        try:
            server._persist_session(session)
            out.put("accepted")
        except SessionBusyError:
            out.put("stale")
    finally:
        server._executor.shutdown(wait=True)


def _hold_turn_lock(path: str, ready, done) -> None:
    with _file_transaction_lock(Path(path).with_suffix(".turn")):
        ready.set()
        done.wait(10)


def test_stale_session_snapshot_cannot_overwrite_new_transcript(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with patch("kiro_byo.acp_server.home_dir", return_value=home):
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        try:
            session = Session("shared", str(tmp_path), "echo")
            append_message(session, {"role": "user", "content": "original"})
            server._persist_session(session)
            assert session.storage_revision == 1
            base = json.loads(server._session_path("shared").read_text(encoding="utf-8"))
            ctx = mp.get_context("spawn")
            start = ctx.Event()
            out = ctx.Queue()
            children = [ctx.Process(target=_stale_writer, args=(str(home), base, tag, start, out)) for tag in ("from-A", "from-B")]
            try:
                for process in children:
                    process.start()
                start.set()
                for process in children:
                    process.join(10)
                    assert process.exitcode == 0
                assert sorted([out.get(timeout=2), out.get(timeout=2)]) == ["accepted", "stale"]
                final = server._load_persisted_record("shared")
                assert final is not None
                assert final["storage_revision"] == 2
                assert final["messages"][0]["content"] == "original"
                assert final["messages"][-1]["content"] in {"from-A", "from-B"}
                with pytest.raises(SessionBusyError, match="stale"):
                    server._persist_session(session)
            finally:
                for process in children:
                    if process.is_alive():
                        process.terminate()
                        process.join(3)
        finally:
            server._executor.shutdown(wait=True)


def test_existing_id_cannot_be_overwritten_by_new_session(tmp_path: Path) -> None:
    with patch("kiro_byo.acp_server.home_dir", return_value=tmp_path / "home"):
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        try:
            server._persist_session(Session("same", str(tmp_path), "echo"))
            with pytest.raises(SessionBusyError, match="already exists"):
                server._persist_session(Session("same", str(tmp_path), "echo"))
            assert server._load_persisted_record("same")["storage_revision"] == 1
        finally:
            server._executor.shutdown(wait=True)


def test_cross_process_active_turn_lock_denies_prompt_before_execution(tmp_path: Path) -> None:
    with patch("kiro_byo.acp_server.home_dir", return_value=tmp_path / "home"):
        output = StringIO()
        server = AcpServer(stdin=StringIO(), stdout=output)
        try:
            session = Session("shared-turn", str(tmp_path), "kiro-byo/echo")
            server._persist_session(session)
            server._put_session(session)
            ctx = mp.get_context("spawn")
            ready = ctx.Event()
            done = ctx.Event()
            holder = ctx.Process(target=_hold_turn_lock, args=(str(server._session_path(session.session_id)), ready, done))
            try:
                holder.start()
                assert ready.wait(8)
                server._handle_prompt_request(10, {"sessionId": session.session_id, "prompt": [{"type": "text", "text": "do not run"}]})
                assert '"code": -32002' in output.getvalue() or '"code":-32002' in output.getvalue()
                assert not session.active_turn
                assert not session.turn_lock.locked()
                assert server._load_persisted_record(session.session_id)["storage_revision"] == 1
            finally:
                done.set()
                holder.join(10)
                if holder.is_alive():
                    holder.terminate()
                    holder.join(3)
        finally:
            server._executor.shutdown(wait=True)


def test_legacy_v3_without_revision_migrates_on_next_save(tmp_path: Path) -> None:
    with patch("kiro_byo.acp_server.home_dir", return_value=tmp_path / "home"):
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        try:
            path = server._session_path("legacy-rev")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"schema_version": 3, "session_id": "legacy-rev", "events": []}), encoding="utf-8")
            session = Session("legacy-rev", str(tmp_path), "echo", storage_loaded=True, storage_revision=0)
            server._persist_session(session)
            assert server._load_persisted_record("legacy-rev")["storage_revision"] == 1
        finally:
            server._executor.shutdown(wait=True)


def test_failed_checkpoint_does_not_claim_storage_commit(tmp_path: Path) -> None:
    with patch("kiro_byo.acp_server.home_dir", return_value=tmp_path / "home"):
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        try:
            session = Session("failed-durable-write", str(tmp_path), "echo")
            with patch("kiro_byo.acp_server._atomic_json_write", side_effect=OSError("simulated fsync failure")):
                assert server._persist_session(session) is False
            assert session.storage_revision == 0
            assert not session.storage_loaded
            assert not server._session_path(session.session_id).exists()
            assert server._persist_session(session) is True
            assert session.storage_revision == 1
        finally:
            server._executor.shutdown(wait=True)


def test_legacy_filename_can_migrate_without_duplicating_session_list(tmp_path: Path) -> None:
    with patch("kiro_byo.acp_server.home_dir", return_value=tmp_path / "home"):
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        try:
            session_id = "old/path"
            legacy = server._legacy_session_path(session_id)
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text(json.dumps({
                "schema_version": 3, "session_id": session_id,
                "cwd": str(tmp_path), "model": "echo", "events": [],
            }), encoding="utf-8")
            old = server._load_persisted_record(session_id)
            assert old is not None
            session = Session(
                session_id, str(tmp_path), "echo", storage_revision=0,
                storage_loaded=True, events=old["events"], messages=old["messages"],
            )
            server._assert_session_current(session)
            assert server._persist_session(session)
            assert server._session_path(session_id).exists()
            assert server._load_persisted_record(session_id)["storage_revision"] == 1
            assert sum(row["session_id"] == session_id for row in server._persisted_session_records()) == 1
        finally:
            server._executor.shutdown(wait=True)
