"""Nothing Kiro Crew opens on ``memory.db`` may drop the gateway's SQLite locks on it.

On POSIX, closing ANY descriptor of a file releases every record lock the process
holds on that file. The gateway holds ``<data home>/memory.db`` open through its memory
store, so a plain open and close of it inside the gateway (the dashboard file viewer
reading a ``memory.db`` path) leaves the store writing with no lock another process can
see. That process's next close may then checkpoint and delete the ``-wal`` the store
still writes through, and the database splits.

Every test uses a real SQLite file under the per-test data home. Lock state is read by
a separate process, because a process never sees its own record locks as a conflict.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import config_dir
from kiro_crew.dashboard import handlers
from kiro_crew.dashboard.handlers import files
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS
from kiro_crew.vector_memory import VectorMemoryStore

fcntl = pytest.importorskip("fcntl", reason="POSIX record locks")

#: SQLite's shared-lock range on the database file (PENDING_BYTE + 2, 510 bytes),
#: which a WAL connection holds while it is open.
_DB_SHARED = (0x40000002, 510)
#: The ``-shm`` byte every connection of a WAL database holds a read lock on.
_SHM_DMS = (128, 1)

#: Exits 0 when another process holds a lock in the range: an exclusive lock there
#: cannot be taken. A lock it does get is released when it exits.
_PROBE = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, int(sys.argv[3]), int(sys.argv[2]))
except OSError:
    sys.exit(0)
sys.exit(1)
"""


def _held_by_another_process(path: Path, span: tuple[int, int]) -> bool:
    start, length = span
    probe = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(path), str(start), str(length)],
        cwd=path.parent,
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    return probe.returncode == 0


def _locks_held(db: Path) -> dict[str, bool]:
    return {
        "memory.db": _held_by_another_process(db, _DB_SHARED),
        "memory.db-shm": _held_by_another_process(Path(f"{db}-shm"), _SHM_DMS),
    }


@pytest.fixture()
def gateway_store(opened) -> VectorMemoryStore:
    """The gateway's memory store, open on the data home's ``memory.db``."""
    store = opened(VectorMemoryStore())
    store.init()
    store.set_semantic("pref.editor", "vim", 0.9, "user_explicit")
    assert store.get_semantic("pref.editor") is not None
    return store


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.sel.sel") as sel:
        sel.return_value = MagicMock()
        yield sel.return_value


_ENDPOINTS = {
    "/api/file-read": (handlers.api_file_read, 400),
    "/api/file-raw": (handlers.api_file_raw, 403),
    "/api/file-download": (handlers.api_file_download, 403),
    "/api/file-stream": (handlers.api_file_stream, 403),
    "/api/file-watch": (handlers.api_file_watch, 403),
}


async def _get(route: str, path: Path) -> int:
    handler, _ = _ENDPOINTS[route]
    app = web.Application()
    app.router.add_get(route, handler)
    async with TestClient(TestServer(as_owner(app))) as client:
        async with client.get(route, params={"path": str(path)}) as resp:
            return resp.status


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(_ENDPOINTS))
async def test_the_file_api_refuses_the_live_memory_db_and_keeps_its_locks(
    gateway_store, mock_sel, route: str
) -> None:
    db = config_dir() / "memory.db"
    assert _locks_held(db) == {"memory.db": True, "memory.db-shm": True}

    status = await _get(route, db)

    assert status == _ENDPOINTS[route][1]
    assert _locks_held(db) == {"memory.db": True, "memory.db-shm": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
async def test_the_file_viewer_refuses_the_live_memory_db_sidecars(
    gateway_store, mock_sel, suffix: str
) -> None:
    db = config_dir() / "memory.db"
    assert Path(f"{db}{suffix}").is_file()

    assert await _get("/api/file-read", Path(f"{db}{suffix}")) == 400
    assert _locks_held(db) == {"memory.db": True, "memory.db-shm": True}


@pytest.mark.asyncio
async def test_the_file_viewer_still_reads_another_database_in_the_data_home(
    gateway_store, mock_sel
) -> None:
    other = config_dir() / "notes.db"
    other.write_bytes(b"SQLite format 3\x00")

    assert await _get("/api/file-read", other) == 200


@pytest.mark.asyncio
async def test_the_python_grep_engine_searches_the_data_home_and_keeps_the_locks(
    gateway_store, mock_sel, monkeypatch
) -> None:
    """The engine a host without a usable ripgrep runs, walking the data home itself."""
    db = config_dir() / "memory.db"
    (config_dir() / "notes.txt").write_text("the editor is vim\n", encoding="utf-8")
    monkeypatch.setattr(files, "_grep_rg_executable", lambda: None)
    assert _locks_held(db) == {"memory.db": True, "memory.db-shm": True}

    app = web.Application()
    app.router.add_post("/api/file-grep", handlers.api_file_grep)
    state = MagicMock()
    state.file_indexes.get.return_value = None
    state.owner_id = ""
    app["state"] = state
    async with TestClient(TestServer(as_owner(app))) as client:
        async with client.post(
            "/api/file-grep", json={"root": str(config_dir()), "q": "vim"}
        ) as resp:
            status, payload = resp.status, await resp.json()

    assert status == 200, payload
    assert payload["engine"] == "python"
    assert [Path(hit["file"]).name for hit in payload["results"]] == ["notes.txt"]
    assert _locks_held(db) == {"memory.db": True, "memory.db-shm": True}
