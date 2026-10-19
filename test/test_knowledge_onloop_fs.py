"""Off-loop filesystem discipline for the knowledge handlers' request paths.

``add_source``, ``retry_file`` and ``skip_file`` take a path from the request, and
``_ingest_local_file_task`` re-checks the path ``add_source`` stored. Each one
resolves that path (``add_source`` also types it, and the folder connector's
``validate_config`` calls ``exists`` / ``is_dir`` on it). On a mount that stopped
answering, those syscalls block for as long as the kernel takes. Run inline in a
coroutine, they would stall the gateway's single event loop, so one request
would stall every endpoint, and a stall past
``dashboard.loop_stall_exit_after_secs`` makes the watchdog kill the process.

That work goes through ``files._run_path_probe``, the bounded request-path probe
pool. This file mirrors ``test_dashboard_files_onloop_fs.py``:

1. a static AST ratchet over every ``async def`` in ``knowledge.py``;
2. proof, per endpoint, of the thread each request-path call ACTUALLY ran on;
3. the refusal when the probe pool is full: a coded 503 for a request, and a
   transient 'pending' (never 'error') for the background ingest.

``search_for_context`` reads the gateway's own ``config.json``. That is not a request
path, so it runs with ``asyncio.to_thread`` rather than on the probe pool, and its test
only proves the loop's own thread never touches the file.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.knowledge.connectors.local_folder import LocalFolderConnector
from kiro_crew.knowledge.store import KnowledgeStore

#: Threads a request-path filesystem call may run on: the dedicated probe pool,
#: and ``security/paths.py``'s own bounded ``mc-pathres`` resolver pool.
#: ``MainThread`` is the loop; ``asyncio_N`` is the shared default executor that
#: one dead mount can starve. Both are violations.
_BOUNDED_POOL_PREFIXES = ("mc-pathprobe", "mc-pathres")

# --- static ratchet ----------------------------------------------------------

#: Filesystem calls by name. Name-based on purpose, like the files.py ratchet:
#: it over-matches a same-named method on another object, which fails safe.
#: ``validate_config`` is here because ``LocalFolderConnector``'s implementation
#: stats the request path one frame down, where a lexical scan cannot see it.
_FS_NAMES = frozenset(
    {
        "resolve",
        "is_file",
        "is_dir",
        "exists",
        "stat",
        "lstat",
        "iterdir",
        "realpath",
        "isfile",
        "isdir",
        "listdir",
        "scandir",
        "walk",
        "validate_config",
    }
)
_FS_BUILTINS = frozenset({"open"})

#: The sync helpers the fix introduced. A coroutine must neither call them
#: directly (that blocks the loop) nor hand them to ``asyncio.to_thread`` (that
#: lands on the shared default executor). The one route is ``_run_path_probe``.
_BLOCKING_HELPERS = frozenset(
    {
        "_probe_local_uri",
        "_add_source_path_checks",
        "_file_path_restricted",
        "_resolved_is_sensitive",
    }
)

#: ``(coroutine, call)`` pairs that stay on the loop, each with its reason. The
#: test below fails if an entry stops matching, so this cannot go stale. Empty:
#: ``search_for_context``'s config read runs in ``_read_knowledge_config`` on a
#: worker thread (``TestSearchForContext``).
_ALLOWED: dict[tuple[str, str], str] = {}


def _scan_own_body(node: ast.AsyncFunctionDef) -> list[tuple[str, int, str]]:
    """``(coroutine, line, call)`` for each blocking call in *node*'s OWN body.

    A nested ``def`` / ``lambda`` is skipped: its body runs wherever it is
    handed, which is the fix. A nested ``async def`` is scanned on its own.
    """
    found: list[tuple[str, int, str]] = []
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Call):
            fn = n.func
            if isinstance(fn, ast.Attribute) and fn.attr == "to_thread" and n.args:
                target = n.args[0]
                if isinstance(target, ast.Name) and target.id in _BLOCKING_HELPERS:
                    found.append((node.name, n.lineno, f"to_thread({target.id})"))
                elif isinstance(target, ast.Attribute) and target.attr in _FS_NAMES:
                    found.append((node.name, n.lineno, f"to_thread(.{target.attr})"))
            if isinstance(fn, ast.Attribute) and fn.attr in _FS_NAMES:
                found.append((node.name, n.lineno, fn.attr))
            elif isinstance(fn, ast.Name) and (fn.id in _FS_BUILTINS or fn.id in _BLOCKING_HELPERS):
                found.append((node.name, n.lineno, fn.id))
        stack.extend(ast.iter_child_nodes(n))
    return found


def _owner_files() -> list[Path]:
    """The ``knowledge_<role>.py`` owners the handlers module serves."""
    home = Path(inspect.getsourcefile(kh)).parent
    return [home / f"{name.rpartition('.')[2]}.py" for name in kh._OWNERS]


def _module_coroutines() -> list[ast.AsyncFunctionDef]:
    """Every coroutine of the handlers module and its owners, read as one module.

    The endpoints' bodies live in the owners, and ``knowledge`` is their import path
    and patch surface, so the ratchet reads every one of those files.
    """
    tree = ast.parse(Path(inspect.getsourcefile(kh)).read_text(encoding="utf-8"))
    for path in _owner_files():
        tree.body.extend(ast.parse(path.read_text(encoding="utf-8")).body)
    return [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]


class TestStaticRatchet:
    def test_no_coroutine_touches_the_filesystem_on_the_loop(self):
        coroutines = _module_coroutines()
        names = {c.name for c in coroutines}
        # The scan must see the endpoints this file is about, or it proves nothing.
        assert {"add_source", "retry_file", "skip_file", "_ingest_local_file_task"} <= names
        found = [hit for c in coroutines for hit in _scan_own_body(c)]
        violations = [hit for hit in found if (hit[0], hit[2]) not in _ALLOWED]
        assert not violations, (
            "blocking filesystem call(s) on the event loop or the shared default "
            "executor in knowledge.py. Move the call into a sync helper and run it "
            "through _run_path_probe:\n"
            + "\n".join(f"{name}:{line} {call}" for name, line, call in violations)
        )
        used = {(hit[0], hit[2]) for hit in found}
        stale = set(_ALLOWED) - used
        assert not stale, f"allowance entries that no longer match anything: {sorted(stale)}"

    def test_the_ratchet_can_actually_fail(self):
        """The same walk flags the exact pre-fix shapes."""
        bad = ast.parse(
            "async def add_source(request):\n"
            "    valid, err = connector.validate_config(cfg)\n"
            "    resolved = Path(uri).resolve()\n"
            "    if not resolved.is_file():\n"
            "        return None\n"
            "    probe = await asyncio.to_thread(_probe_local_uri, uri)\n"
            "    return _file_path_restricted(uri)\n"
        )
        node = bad.body[0]
        assert isinstance(node, ast.AsyncFunctionDef)
        calls = sorted(call for _, _, call in _scan_own_body(node))
        assert calls == [
            "_file_path_restricted",
            "is_file",
            "resolve",
            "to_thread(_probe_local_uri)",
            "validate_config",
        ], calls


# --- behavioural proof -------------------------------------------------------


class _ThreadSpy:
    """Records the thread of each ``Path`` filesystem call on the REQUEST path.

    Scoped to one path prefix: the store and config loader resolve their own
    paths, which is a different surface from the one this file guards.
    """

    _METHODS = ("resolve", "exists", "is_file", "is_dir")

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.seen: list[tuple[str, str]] = []

    def watch(self, target: Path | str) -> None:
        prefix = str(target)
        seen = self.seen
        for name in self._METHODS:
            original = getattr(Path, name)

            def _wrapper(self_path: Any, *a: object, _orig=original, _name=name, **kw: object):
                if str(self_path).startswith(prefix):
                    seen.append((_name, threading.current_thread().name))
                return _orig(self_path, *a, **kw)

            self._monkeypatch.setattr(Path, name, _wrapper)

    def assert_off_loop(self, *, expect: set[str]) -> None:
        methods = {m for m, _ in self.seen}
        assert expect <= methods, f"expected {sorted(expect)} on the request path, saw {self.seen}"
        astray = [(m, t) for m, t in self.seen if not t.startswith(_BOUNDED_POOL_PREFIXES)]
        assert not astray, f"request-path filesystem call(s) outside a bounded pool: {astray}"


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _ThreadSpy:
    return _ThreadSpy(monkeypatch)


@pytest.fixture()
def store(tmp_path, opened):
    """Closed on every thread at teardown: the handlers open per-thread connections."""
    return opened(KnowledgeStore(str(tmp_path / "kb.db")))


def _make_app(store) -> web.Application:
    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    app["state"] = state
    # The real folder connector, so its validate_config stats the request path.
    app["knowledge_sync"] = MagicMock(
        get_connector=lambda t: LocalFolderConnector() if t == "local_folder" else None
    )
    r = app.router
    r.add_post("/api/knowledge/sources", kh.add_source)
    r.add_post("/api/knowledge/sources/{id}/files/retry", kh.retry_file)
    r.add_post("/api/knowledge/sources/{id}/files/skip", kh.skip_file)
    r.add_get("/api/knowledge/search-for-context", kh.search_for_context)
    return app


def _seed_source(store, uri: str) -> str:
    return store.add_source(
        name="folder", source_type="local_folder", uri=uri, properties={"sync_status": "active"}
    )


def _sync_status(store, source_id: str) -> str:
    return store.db.execute(
        "SELECT sync_status FROM sources WHERE id = ?", (source_id,)
    ).fetchone()[0]


async def _busy(*_a: object, **_kw: object):
    raise kh._PathProbeBusy()


async def _no_results(*_a: object, **_kw: object) -> list:
    return []


class TestAddSource:
    @pytest.mark.asyncio
    async def test_local_file_probe_runs_off_the_loop(self, store, tmp_path, spy):
        doc = tmp_path / "docs" / "note.md"
        doc.parent.mkdir()
        doc.write_text("hello", encoding="utf-8")
        spy.watch(tmp_path / "docs")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post(
                "/api/knowledge/sources",
                json={"name": "n", "source_type": "local_file", "uri": str(doc)},
            )
            assert resp.status == 201, await resp.text()
        spy.assert_off_loop(expect={"resolve", "is_file"})

    @pytest.mark.asyncio
    async def test_folder_connector_and_probe_run_off_the_loop(self, store, tmp_path, spy):
        vault = tmp_path / "vault"
        vault.mkdir()
        spy.watch(vault)
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post(
                "/api/knowledge/sources",
                json={"name": "v", "source_type": "local_folder", "uri": str(vault)},
            )
            assert resp.status == 201, await resp.text()
            assert (await resp.json())["status"] == "pending_confirmation"
        # exists + is_dir are the connector's; resolve + is_dir are the probe's.
        spy.assert_off_loop(expect={"exists", "is_dir", "resolve"})

    @pytest.mark.asyncio
    async def test_missing_folder_still_answers_the_connector_error(self, store, tmp_path, spy):
        """Control: the connector's refusal still reaches the client unchanged."""
        missing = tmp_path / "gone"
        spy.watch(missing)
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post(
                "/api/knowledge/sources",
                json={"name": "v", "source_type": "local_folder", "uri": str(missing)},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == f"Path does not exist: {missing}"
        spy.assert_off_loop(expect={"exists"})

    @pytest.mark.asyncio
    async def test_busy_probe_pool_answers_a_coded_503(self, store, tmp_path, monkeypatch):
        monkeypatch.setattr(kh, "_run_path_probe", _busy)
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post(
                "/api/knowledge/sources",
                json={"name": "n", "source_type": "local_file", "uri": str(tmp_path / "a.md")},
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "path_probe_busy"


class TestRetryAndSkip:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["retry", "skip"])
    async def test_sensitive_check_runs_off_the_loop(self, store, tmp_path, spy, action):
        sid = await asyncio.to_thread(_seed_source, store, str(tmp_path))
        target = tmp_path / "a.md"
        spy.watch(target)
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{sid}/files/{action}", json={"file_path": str(target)}
            )
            assert resp.status == 200, await resp.text()
        spy.assert_off_loop(expect={"resolve"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["retry", "skip"])
    async def test_busy_probe_pool_answers_a_coded_503(self, store, tmp_path, monkeypatch, action):
        sid = await asyncio.to_thread(_seed_source, store, str(tmp_path))
        monkeypatch.setattr(kh, "_run_path_probe", _busy)
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{sid}/files/{action}",
                json={"file_path": str(tmp_path / "a.md")},
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "path_probe_busy"


class TestIngestTask:
    @pytest.mark.asyncio
    async def test_ingest_time_recheck_runs_off_the_loop(self, store, tmp_path, spy):
        doc = tmp_path / "doc.md"
        doc.write_text("body", encoding="utf-8")
        sid = await asyncio.to_thread(_seed_source, store, str(doc))
        pipeline = MagicMock(ingest_file=AsyncMock())
        spy.watch(doc)
        await kh._ingest_local_file_task(pipeline, store, str(doc), sid)
        pipeline.ingest_file.assert_awaited_once()
        assert await asyncio.to_thread(_sync_status, store, sid) == "synced"
        spy.assert_off_loop(expect={"resolve"})

    @pytest.mark.asyncio
    async def test_busy_probe_pool_defers_to_pending_not_error(self, store, tmp_path, monkeypatch):
        """A full pool is transient: 'error' would drop the file from the sweep."""
        doc = tmp_path / "doc.md"
        doc.write_text("body", encoding="utf-8")
        sid = await asyncio.to_thread(_seed_source, store, str(doc))
        pipeline = MagicMock(ingest_file=AsyncMock())
        monkeypatch.setattr(kh, "_run_path_probe", _busy)
        await kh._ingest_local_file_task(pipeline, store, str(doc), sid)
        pipeline.ingest_file.assert_not_awaited()
        assert await asyncio.to_thread(_sync_status, store, sid) == "pending"


class TestSearchForContext:
    @pytest.mark.asyncio
    async def test_config_read_runs_off_the_loop(self, store, tmp_path, spy, monkeypatch):
        """The gateway's own ``config.json`` is stat'ed and read on a worker thread.

        It is not a request path, so the lane is ``asyncio.to_thread`` rather than the
        probe pool; what matters is that the loop's own thread never waits on it.
        """
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.json").write_text(
            json.dumps({"knowledge": {"fetch_max_tokens": 7}}), encoding="utf-8"
        )
        monkeypatch.setattr(kh, "data_home", lambda: home)
        monkeypatch.setattr(kh, "run_in_embed_pool", _no_results)
        spy.watch(home)
        loop_thread = threading.current_thread().name
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.get("/api/knowledge/search-for-context", params={"q": "z"})
            assert resp.status == 200, await resp.text()
            # The budget came from the file, so the read really happened.
            assert (await resp.json())["max_tokens"] == 7
        assert "exists" in {m for m, _ in spy.seen}, spy.seen
        on_loop = [(m, t) for m, t in spy.seen if t == loop_thread]
        assert not on_loop, f"config.json touched on the loop's thread: {on_loop}"
