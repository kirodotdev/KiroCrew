"""A disposable OpenCode or pi session leaves no record of itself in the harness's store.

OpenCode keeps sessions in its SQLite ``opencode.db``; the schema below is the part of
opencode 1.19.1's that holds a session (copied from a live store). pi deletes its own
record through ``session/delete``. Every test also proves the rows or calls of a session
Crew must keep are untouched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp.types import METHOD_SESSION_DELETE, JsonRpcMessage
from kiro_crew.agent_sdk import opencode_transcripts
from kiro_crew.agent_sdk.opencode_transcripts import (
    delete_opencode_session_rows,
    opencode_db_path,
)
from kiro_crew.providers import acp_lifecycle

_SCHEMA = """
CREATE TABLE project (id text PRIMARY KEY, worktree text NOT NULL);
CREATE TABLE session (
  id text PRIMARY KEY, project_id text NOT NULL, parent_id text, title text NOT NULL,
  CONSTRAINT fk_session_project FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE
);
CREATE TABLE message (
  id text PRIMARY KEY, session_id text NOT NULL, data text NOT NULL,
  CONSTRAINT fk_message_session FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE
);
CREATE TABLE part (
  id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL, data text NOT NULL,
  CONSTRAINT fk_part_message FOREIGN KEY (message_id) REFERENCES message(id) ON DELETE CASCADE
);
CREATE TABLE todo (session_id text NOT NULL, content text NOT NULL, position integer NOT NULL);
CREATE TABLE event_sequence (aggregate_id text PRIMARY KEY, seq integer NOT NULL);
CREATE TABLE event (
  id text PRIMARY KEY, aggregate_id text NOT NULL, seq integer NOT NULL, data text NOT NULL,
  CONSTRAINT fk_event_seq FOREIGN KEY (aggregate_id) REFERENCES event_sequence(aggregate_id)
    ON DELETE CASCADE
);
"""

_GONE = "ses_e22eb5e2bffewA8zu6ht9LAhtY"
_CHILD = "ses_e22eaf8f6ffdNNbHEolVZvyMWn"
_KEPT = "ses_keepkeepkeepkeepkeepkeepk"


def _add_session(conn: sqlite3.Connection, sid: str, parent: str | None = None) -> None:
    conn.execute("INSERT INTO session VALUES (?, 'global', ?, 't')", (sid, parent))
    conn.execute("INSERT INTO message VALUES (?, ?, ?)", (f"msg_{sid}", sid, f"prompt {sid}"))
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?)", (f"prt_{sid}", f"msg_{sid}", sid, f"text {sid}")
    )
    conn.execute("INSERT INTO todo VALUES (?, 'x', 0)", (sid,))
    conn.execute("INSERT INTO event_sequence VALUES (?, 0)", (sid,))
    conn.execute("INSERT INTO event VALUES (?, ?, 0, ?)", (f"evt_{sid}", sid, f"text {sid}"))


@pytest.fixture(autouse=True)
def _no_real_store(tmp_path: Path, _floor_monkeypatch) -> None:
    """Point every inherited store setting at the test's own folder."""
    _floor_monkeypatch.delenv("OPENCODE_DB", raising=False)
    _floor_monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "unused-data-home"))
    _floor_monkeypatch.setenv("HOME", str(tmp_path / "unused-home"))


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO project VALUES ('global', '/')")
    _add_session(conn, _GONE)
    _add_session(conn, _CHILD, parent=_GONE)
    _add_session(conn, _KEPT)
    conn.commit()
    conn.close()
    return db


def _rows(db: Path, sid: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return sum(
            conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (sid,)).fetchone()[0]
            for table, column in (
                ("session", "id"),
                ("message", "session_id"),
                ("part", "session_id"),
                ("todo", "session_id"),
                ("event_sequence", "aggregate_id"),
                ("event", "aggregate_id"),
            )
        )
    finally:
        conn.close()


def _env(db: Path) -> dict[str, str]:
    return {"XDG_DATA_HOME": str(db.parent.parent)}


def test_deletes_the_session_and_its_children_and_keeps_every_other(store: Path) -> None:
    assert delete_opencode_session_rows(_GONE, _env(store)) is True
    assert _rows(store, _GONE) == 0
    assert _rows(store, _CHILD) == 0
    assert _rows(store, _KEPT) == 6
    conn = sqlite3.connect(store)
    try:
        assert conn.execute("SELECT COUNT(*) FROM project").fetchone()[0] == 1
    finally:
        conn.close()


def test_the_deleted_text_is_not_left_in_the_store_files(store: Path) -> None:
    assert delete_opencode_session_rows(_GONE, _env(store)) is True
    blob = b"".join(p.read_bytes() for p in store.parent.iterdir() if p.is_file())
    assert f"prompt {_GONE}".encode() not in blob
    assert f"prompt {_KEPT}".encode() in blob


def test_an_id_that_is_not_opencodes_deletes_nothing(store: Path) -> None:
    assert delete_opencode_session_rows("' OR 1=1 --", _env(store)) is False
    assert _rows(store, _GONE) == 6


def test_a_missing_store_is_not_created(tmp_path: Path) -> None:
    env = {"XDG_DATA_HOME": str(tmp_path)}
    assert delete_opencode_session_rows(_GONE, env) is True
    assert not (tmp_path / "opencode").exists()


def test_a_store_that_is_not_a_regular_file_is_refused(store: Path, tmp_path: Path) -> None:
    link_home = tmp_path / "linked"
    (link_home / "opencode").mkdir(parents=True)
    leaf = link_home / "opencode" / "opencode.db"
    try:
        leaf.symlink_to(store)
    except OSError:
        # No symlink privilege (unelevated Windows): a folder at the store's name
        # takes the same refusal branch, so the assertions still run.
        leaf.mkdir()
    assert delete_opencode_session_rows(_GONE, {"XDG_DATA_HOME": str(link_home)}) is False
    assert _rows(store, _GONE) == 6


def test_the_store_follows_home_when_no_data_home_is_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "gw-home"))
    default = tmp_path / "gw-home" / ".local" / "share" / "opencode" / "opencode.db"
    assert opencode_db_path(None) == default
    run_home = {"HOME": str(tmp_path / "run-home")}
    if platform_compat.IS_WINDOWS:
        assert opencode_db_path(run_home) == default
    else:
        assert opencode_db_path(run_home) == (
            tmp_path / "run-home" / ".local" / "share" / "opencode" / "opencode.db"
        )
        assert opencode_db_path({"HOME": "relative"}) is None


def test_an_in_memory_or_unresolved_store_deletes_nothing() -> None:
    assert delete_opencode_session_rows(_GONE, {"OPENCODE_DB": ":memory:"}) is True
    assert delete_opencode_session_rows(_GONE, {"XDG_DATA_HOME": "relative"}) is False


def test_a_store_without_opencodes_tables_is_left_as_it_is(tmp_path: Path) -> None:
    db = tmp_path / "opencode" / "opencode.db"
    db.parent.mkdir()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE other (name text)")
    conn.execute("INSERT INTO other VALUES ('kept')")
    conn.commit()
    conn.close()
    assert delete_opencode_session_rows(_GONE, {"XDG_DATA_HOME": str(tmp_path)}) is True
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT name FROM other").fetchall() == [("kept",)]
    finally:
        conn.close()


def test_a_store_that_is_not_a_database_is_reported_not_done(tmp_path: Path) -> None:
    db = tmp_path / "opencode" / "opencode.db"
    db.parent.mkdir()
    db.write_bytes(b"not a database, just text" * 100)
    assert delete_opencode_session_rows(_GONE, {"XDG_DATA_HOME": str(tmp_path)}) is False


def test_a_store_that_cannot_be_inspected_is_not_done(store: Path, monkeypatch) -> None:
    real_lstat = opencode_transcripts.os.lstat

    def _refuse(path, *args, **kwargs):
        if Path(path) == store:
            raise PermissionError(13, "denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(opencode_transcripts.os, "lstat", _refuse)
    assert delete_opencode_session_rows(_GONE, _env(store)) is False


def test_a_store_that_cannot_be_opened_is_not_done(store: Path, monkeypatch) -> None:
    def _no_open(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(opencode_transcripts.sqlite3, "connect", _no_open)
    assert delete_opencode_session_rows(_GONE, _env(store)) is False


def test_a_driver_without_setconfig_still_closes_keeping_the_wal(store: Path, monkeypatch) -> None:
    closed: list[object] = []
    monkeypatch.setattr(opencode_transcripts, "keep_wal_on_close", lambda conn: False)
    monkeypatch.setattr(
        opencode_transcripts, "close_keeping_wal", lambda conn: closed.append(conn) or conn.close()
    )
    assert delete_opencode_session_rows(_GONE, _env(store)) is True
    assert len(closed) == 1
    assert _rows(store, _GONE) == 0


def test_the_store_follows_the_spawn_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "gw"))
    assert opencode_db_path(None) == tmp_path / "gw" / "opencode" / "opencode.db"
    assert opencode_db_path({"XDG_DATA_HOME": str(tmp_path / "run")}) == (
        tmp_path / "run" / "opencode" / "opencode.db"
    )
    assert opencode_db_path({"OPENCODE_DB": str(tmp_path / "x.db")}) == tmp_path / "x.db"
    assert opencode_db_path({"OPENCODE_DB": "other.db"}) == (
        tmp_path / "gw" / "opencode" / "other.db"
    )
    assert opencode_db_path({"OPENCODE_DB": ":memory:"}) == ":memory:"
    assert opencode_db_path({"XDG_DATA_HOME": "relative"}) is None


def _provider(backend: str, memory_mode: str, client: object, **extra: object) -> SimpleNamespace:
    provider = SimpleNamespace(
        is_kas_backend=False,
        is_claude_backend=False,
        is_codex_backend=False,
        is_opencode_backend=backend == "opencode",
        is_pi_backend=backend == "pi",
        memory_mode=memory_mode,
        session_id=extra.pop("session_id", _GONE),
        _client=client,
        _disposable_work_dir=False,
        **extra,
    )
    provider.cleanup_session = lambda sid: acp_lifecycle.cleanup_session(provider, sid)
    return provider


@pytest.mark.asyncio
async def test_an_incognito_opencode_chat_deletes_its_rows_at_shutdown(store: Path) -> None:
    client = SimpleNamespace(shutdown=AsyncMock(), _extra_env=_env(store))
    await acp_lifecycle.shutdown(_provider("opencode", "incognito", client))
    client.shutdown.assert_awaited_once()
    assert _rows(store, _GONE) == 0
    assert _rows(store, _KEPT) == 6


@pytest.mark.asyncio
async def test_a_persistent_opencode_chat_keeps_its_rows(store: Path) -> None:
    client = SimpleNamespace(shutdown=AsyncMock(), _extra_env=_env(store))
    await acp_lifecycle.shutdown(_provider("opencode", "persistent", client))
    assert _rows(store, _GONE) == 6


def _pi_client(*, delete_advertised: bool = True, alive: bool = True) -> MagicMock:
    caps = {"delete": {}, "list": {}} if delete_advertised else {"list": {}}
    client = MagicMock()
    client.initialize_result = {"agentCapabilities": {"sessionCapabilities": caps}}
    client._is_process_alive.return_value = alive
    order: list[str] = []
    client._send_request = AsyncMock(side_effect=lambda *a, **k: order.append("delete") or 7)
    client._claim_stdout = AsyncMock()
    answer = JsonRpcMessage(id=7, result={})
    client._read_message = AsyncMock(side_effect=[JsonRpcMessage(method="session/update"), answer])
    client.shutdown = AsyncMock(side_effect=lambda: order.append("shutdown"))
    client.order = order
    return client


_PI_SID = "01a1dd15-716d-7661-90bc-18720499a6f0"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_a_disposable_pi_chat_asks_pi_to_delete_before_the_process_ends(mode: str) -> None:
    client = _pi_client()
    await acp_lifecycle.shutdown(_provider("pi", mode, client, session_id=_PI_SID))
    client._send_request.assert_awaited_once_with(METHOD_SESSION_DELETE, {"sessionId": _PI_SID})
    assert client._read_message.await_count == 2
    assert client.order == ["delete", "shutdown"]
    client._release_stdout.assert_called_once()


@pytest.mark.asyncio
async def test_the_pi_delete_is_awaited_while_the_gateway_shuts_down(monkeypatch) -> None:
    import kiro_crew

    monkeypatch.setattr(kiro_crew, "shutdown_event", SimpleNamespace(is_set=lambda: True))
    client = _pi_client()
    await acp_lifecycle.shutdown(_provider("pi", "incognito", client, session_id=_PI_SID))
    assert client._read_message.await_count == 2
    assert client.order == ["delete", "shutdown"]


@pytest.mark.asyncio
async def test_a_persistent_pi_chat_sends_no_delete() -> None:
    client = _pi_client()
    await acp_lifecycle.shutdown(_provider("pi", "persistent", client, session_id=_PI_SID))
    client._send_request.assert_not_called()
    client.shutdown.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("advertised", "alive"), [(False, True), (True, False)], ids=["not-advertised", "dead"]
)
async def test_pi_without_the_verb_or_a_live_process_still_shuts_down(
    advertised: bool, alive: bool
) -> None:
    client = _pi_client(delete_advertised=advertised, alive=alive)
    await acp_lifecycle.shutdown(_provider("pi", "incognito", client, session_id=_PI_SID))
    client._send_request.assert_not_called()
    client.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_pi_delete_still_shuts_down() -> None:
    client = _pi_client()
    client._read_message.side_effect = RuntimeError("boom")
    await acp_lifecycle.shutdown(_provider("pi", "incognito", client, session_id=_PI_SID))
    client.shutdown.assert_awaited_once()
