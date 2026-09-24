"""Off-loop filesystem discipline for the dashboard file endpoints.

The blocking condition is not hypothetical. Every one of these endpoints takes
its path from the request, so which mount it lands on is the caller's choice,
not the gateway's. ``hooks.validate_file_path`` canonicalizes with
``os.path.realpath`` and (on Windows) walks the path's ancestors with one
``lstat`` each; ``os.path.isfile`` / ``isdir`` / ``open`` are the same class of
call. On an unresponsive mount -- a disconnected network share, a wedged FUSE
filesystem -- those syscalls block for however long the kernel takes and are
uninterruptible.

Run on the gateway's single event loop, ONE such request stalls every endpoint in
the process at once (dashboard, tunnel, MCP, crons), and a stall outlasting
``dashboard.loop_stall_exit_after_secs`` makes the loop watchdog kill the
gateway. AUTOSDE ``no-blocking-call-on-event-loop`` covers exactly this shape.

Two guards, mirroring ``test_auto_research_onloop_fs.py``:

1. a static AST ratchet, so a new inline ``realpath`` / ``isfile`` / ``open`` in
   one of these ``async def`` bodies fails here rather than in production;
2. behavioural proof, on the thread the syscall ACTUALLY ran on, for each
   endpoint -- with a control per endpoint that its answer did not change;
3. isolation proof for the DEDICATED probe pool: probes run on it and not on the
   loop's default executor, a saturated pool refuses new probes with a coded 503
   instead of queueing them, and unrelated ``asyncio.to_thread`` work stays
   schedulable while every probe worker is wedged.
"""

from __future__ import annotations

import ast
import asyncio
import errno
import inspect
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import kiro_crew.dashboard.handlers as _HANDLERS_PKG
from kiro_crew import executors
from kiro_crew.dashboard.handlers import files as f

#: The shared open-and-check prefix resolves the validator through the
#: ``handlers`` package at call time -- a documented monkey-patch seam kept for
#: the circular import. A stub on the ``files`` module alone is invisible to it.
_VALIDATOR_SEAM = "kiro_crew.dashboard.handlers._validate_dashboard_path"

#: Thread-name prefixes of the two dedicated pools in ``executors.py``: probes
#: (validation and stats) and transfers (bounded full reads, walks, listings,
#: parses).
_PROBE_THREAD_PREFIX = "mc-pathprobe"
_TRANSFER_THREAD_PREFIX = "mc-pathxfer"
#: The pools a request-path filesystem call may legitimately run on: the two
#: above, and ``security/paths.py``'s ``mc-pathres`` pool, where
#: ``is_sensitive_path`` resolves -- itself bounded (2 s, fail-closed), so it is
#: not the starvation surface either. Anything else -- ``MainThread``, or the
#: default executor's ``asyncio_N`` / ``mc-default`` threads -- is a violation.
_BOUNDED_POOL_PREFIXES = (_PROBE_THREAD_PREFIX, _TRANSFER_THREAD_PREFIX, "mc-pathres")

# --- static ratchet ----------------------------------------------------------

#: Prefixes of the handler families that take a caller-supplied path. The
#: guarded set is DERIVED from the module by these prefixes rather than listed,
#: so a new path-taking handler is guarded the moment it is written. Listing the
#: names is what let ``api_file_office_preview`` sit outside the guard while
#: carrying the same defect: an omission was silent, and only a reviewer's own
#: grep found it. Now an unoffloaded newcomer fails this file instead.
_GUARDED_PREFIXES = ("api_file_", "api_browse_", "api_reveal_")

#: ``os.path`` predicates and ``os`` functions that hit the filesystem. Like the
#: auto_research ratchet next door this is name-based: it over-matches a
#: same-named method on some other object, which fails safe (a spurious
#: offload), and it is the only shape a static check can see without whole-program
#: type inference.
#:
#: ``which`` is here for a reason the ``os``-shaped names do not make obvious:
#: ``shutil.which`` stats every ``$PATH`` entry, so a ``$PATH`` entry on an
#: unresponsive mount parks the loop exactly as ``isfile`` on that mount would.
#: It is filesystem work whose name does not look like it.
_FS_NAMES = frozenset(
    {
        "realpath",
        "which",
        "isfile",
        "isdir",
        "exists",
        "lexists",
        "islink",
        "stat",
        "lstat",
        "listdir",
        "scandir",
        "walk",
        "readlink",
        "makedirs",
        "mkdir",
        "remove",
        "unlink",
        "rmdir",
        "rename",
        "replace",
    }
)

#: Bare builtins that block on the filesystem.
_FS_BUILTINS = frozenset({"open"})

#: Blocking helpers in this module that a handler must not call on the loop --
#: and must not hand to ``asyncio.to_thread`` either. These are the sync seams
#: the offload was introduced for. Directly, they block the loop; via
#: ``to_thread``, they land on the process-wide default executor, where one dead
#: mount can retire a worker per request until unrelated work starves. Both are
#: the same defect; the only sanctioned route is ``_run_path_probe``.
_BLOCKING_HELPERS = frozenset(
    {
        "_validate_dashboard_path",
        "_probe_request_path",
        "_read_request_path",
        "_resolve_project_relative",
        "_resolve_search_root",
        "_resolve_diff_path",
        "_open_checked",
        "_open_checked_file",
        "_open_rb_nofollow",
    }
)


def _module_tree() -> ast.Module:
    src = Path(inspect.getsourcefile(f)).read_text(encoding="utf-8")
    return ast.parse(src)


def _is_to_thread(fn: ast.expr) -> bool:
    return isinstance(fn, ast.Attribute) and fn.attr == "to_thread"


def _scan_own_body(node: ast.AsyncFunctionDef) -> list[str]:
    """Blocking calls in *node*'s OWN body.

    A nested ``def`` is skipped: its body runs in whatever worker the handler
    hands it to, which is the fix, not the defect. What is flagged is a call
    the coroutine makes itself.
    """
    violations: list[str] = []
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Call):
            fn = n.func
            # ``asyncio.to_thread(<probe>, ...)`` runs the probe off the loop,
            # but on the SHARED default executor, which a dead mount can starve.
            # A probe helper or an ``os.path`` predicate as its first argument is
            # therefore flagged; the sanctioned route is ``_run_path_probe``.
            # A nested sync helper (the search walk, the write transaction) is
            # not a probe and passes through.
            if _is_to_thread(fn) and n.args:
                target = n.args[0]
                if isinstance(target, ast.Name) and target.id in _BLOCKING_HELPERS:
                    violations.append(
                        f"{node.name}:{n.lineno} hands {target.id} to asyncio.to_thread"
                    )
                elif isinstance(target, ast.Attribute) and target.attr in _FS_NAMES:
                    violations.append(
                        f"{node.name}:{n.lineno} hands .{target.attr} to asyncio.to_thread"
                    )
            if isinstance(fn, ast.Attribute) and fn.attr in _FS_NAMES:
                violations.append(f"{node.name}:{n.lineno} calls .{fn.attr}()")
            elif isinstance(fn, ast.Name) and fn.id in _FS_BUILTINS:
                violations.append(f"{node.name}:{n.lineno} calls {fn.id}()")
            elif isinstance(fn, ast.Name) and fn.id in _BLOCKING_HELPERS:
                violations.append(f"{node.name}:{n.lineno} calls {fn.id}()")
        stack.extend(ast.iter_child_nodes(n))
    return violations


def _guarded_endpoints() -> dict[str, ast.AsyncFunctionDef]:
    """Every path-taking handler in the module, by name.

    No exemption hatch: there is nothing this ratchet should not watch, and an
    unused one is a hole waiting for a future edit to widen. A handler that
    genuinely must keep a filesystem call on the loop needs a reason in code, not
    a name in a list here.
    """
    return {
        node.name: node
        for node in ast.walk(_module_tree())
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith(_GUARDED_PREFIXES)
    }


class TestStaticRatchet:
    def test_no_guarded_endpoint_touches_the_filesystem_on_the_loop(self):
        guarded = _guarded_endpoints()
        assert len(guarded) >= 13, (
            "the prefix scan found only "
            f"{sorted(guarded)} -- a rename likely moved the handlers out of the "
            "guarded families, which would silently empty this ratchet"
        )
        violations: list[str] = []
        for node in guarded.values():
            violations.extend(_scan_own_body(node))

        assert not violations, (
            "blocking filesystem call(s) on the event loop or on the shared default "
            "executor. Route the call through _run_path_probe (transfer=True for a "
            "full read, walk, listing or parse), or move it into a sync helper "
            "handed to one:\n" + "\n".join(violations)
        )

    def test_the_ratchet_can_actually_fail(self):
        """Guard against a scan that passes because it sees nothing: the same
        walk must flag the exact pre-fix shape."""
        bad = ast.parse(
            "async def api_file_read(request):\n"
            "    path = _validate_dashboard_path(request.query['path'])\n"
            "    if not os.path.isfile(path):\n"
            "        return None\n"
            "    engine = shutil.which('rg')\n"
            "    probe = await asyncio.to_thread(_probe_request_path, path)\n"
            "    ok = await asyncio.to_thread(os.path.isdir, path)\n"
            "    with open(path) as fh:\n"
            "        return fh.read()\n"
        )
        node = bad.body[0]
        assert isinstance(node, ast.AsyncFunctionDef)
        found = _scan_own_body(node)
        assert len(found) == 6, found
        assert any("_validate_dashboard_path" in v for v in found)
        assert any("isfile" in v for v in found)
        assert any("which" in v for v in found)
        assert any("open" in v for v in found)
        assert any("hands _probe_request_path to asyncio.to_thread" in v for v in found)
        assert any("hands .isdir to asyncio.to_thread" in v for v in found)


# --- behavioural proof -------------------------------------------------------


class _State:
    file_indexes: dict = {}


def _app() -> web.Application:
    app = web.Application()
    app["state"] = _State()
    return app


def _req(path: str, query: str = "", method: str = "GET") -> web.Request:
    req = make_mocked_request(method, f"{path}?{query}" if query else path, app=_app())
    req["user"] = "test-user"
    return req


def _body(resp: web.Response) -> Any:
    return json.loads(resp.text or "")


async def _grep_req(query: str, root: Path | str) -> web.Response:
    """One ``POST /api/file-grep`` with the body the handler reads."""
    req = make_mocked_request("POST", "/api/file-grep", app=_app())
    req["user"] = "test-user"
    with mock.patch.object(
        f, "read_bounded_json", mock.AsyncMock(return_value=({"q": query, "root": str(root)}, None))
    ):
        return await f.api_file_grep(req)


class _ThreadSpy:
    """Records which thread each ``os.path`` call against the REQUEST path ran on.

    Scoped to one path on purpose. An unscoped spy also catches the config
    loader's and the audit log's own ``resolve`` calls, which are a different
    surface with its own offload story; widening this fixture to them would make
    it fail for reasons that have nothing to do with the request path.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.seen: list[str] = []
        self._target = ""

    def watch(self, target: Path | str) -> None:
        """Install the spy for calls whose first argument is under *target*."""
        self._target = str(target)
        for name in ("realpath", "isfile", "isdir"):
            self._install(name)

    def _install(self, name: str) -> None:
        original = getattr(os.path, name)
        target = self._target
        seen = self.seen

        def _wrapper(path: Any, *args: object, **kwargs: object) -> object:
            if isinstance(path, (str, Path)) and str(path).startswith(target):
                seen.append(threading.current_thread().name)
            return original(path, *args, **kwargs)

        self._monkeypatch.setattr(os.path, name, _wrapper)

    def assert_off_loop(self) -> None:
        """Every observed call ran on the dedicated probe pool.

        Stricter than "not MainThread" on purpose: ``asyncio.to_thread`` also
        leaves the main thread, but lands on the shared default executor
        (``asyncio_N`` threads), which is the starvation surface this module
        exists to keep probes off. The pool's thread-name prefix is the
        witness.
        """
        assert self.seen, "no filesystem call on the request path was observed"
        astray = [t for t in self.seen if not t.startswith(_BOUNDED_POOL_PREFIXES)]
        assert not astray, (
            f"{len(astray)} of {len(self.seen)} filesystem call(s) on the request "
            f"path ran outside a bounded pool: {sorted(set(astray))}"
        )


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _ThreadSpy:
    """The real functions still run -- the endpoints must keep working, not
    merely be offloaded -- so every control below exercises the wrapped code."""
    return _ThreadSpy(monkeypatch)


@pytest.fixture
def a_file(tmp_path: Path) -> Path:
    p = tmp_path / "note.md"
    p.write_text("hello", encoding="utf-8")
    return p


class TestFileRead:
    @pytest.mark.asyncio
    async def test_validation_and_read_run_off_the_event_loop(self, a_file: Path, spy: _ThreadSpy):
        spy.watch(a_file)

        resp = await f.api_file_read(_req("/api/file-read", f"path={a_file}"))

        assert resp.status == 200
        assert resp.text == "hello", "the file must still be served, not just offloaded"
        spy.assert_off_loop()

    @pytest.mark.asyncio
    async def test_a_directory_is_still_reported_as_one(self, tmp_path: Path):
        """Control: the isdir answer now comes from the shared probe, and must
        still distinguish a directory from a missing path."""
        resp = await f.api_file_read(_req("/api/file-read", f"path={tmp_path}"))

        assert resp.status == 404
        assert resp.headers["X-Path-Kind"] == "dir"
        assert _body(resp)["error"] == "is a directory"

    @pytest.mark.asyncio
    async def test_a_missing_path_is_still_missing(self, tmp_path: Path):
        resp = await f.api_file_read(_req("/api/file-read", f"path={tmp_path / 'gone.md'}"))

        assert resp.status == 404
        assert resp.headers["X-Path-Kind"] == "missing"

    @pytest.mark.asyncio
    async def test_a_refused_path_is_still_refused(self, a_file: Path):
        """Control: routing the read through the shared prefix must not lose the
        refusal. A ``None`` from the validator is still a 400.

        Stubbed on the ``handlers`` package, which is the prefix's own documented
        late-binding seam and the spelling its four sibling endpoints' tests
        already use.
        """
        with mock.patch(_VALIDATOR_SEAM, return_value=None):
            resp = await f.api_file_read(_req("/api/file-read", f"path={a_file}"))

        assert resp.status == 400
        assert _body(resp)["error"] == "invalid or forbidden path"

    @pytest.mark.asyncio
    async def test_a_sensitive_path_is_refused_the_same_way(self, a_file: Path):
        """The prefix re-checks ``is_sensitive_path`` after validation. That rung
        is a re-check, not a new gate -- ``validate_file_path`` already applies
        the same predicate -- so it must answer with the endpoint's existing 400
        rather than inventing a status."""
        with (
            mock.patch(_VALIDATOR_SEAM, return_value=str(a_file)),
            mock.patch.object(f, "is_sensitive_path", return_value=True),
        ):
            resp = await f.api_file_read(_req("/api/file-read", f"path={a_file}"))

        assert resp.status == 400
        assert _body(resp)["error"] == "invalid or forbidden path"

    @pytest.mark.asyncio
    async def test_the_read_cap_counts_characters_not_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The decode is a TextIOWrapper over the checked descriptor, so the cap
        still counts CHARACTERS. Counting bytes would mark a multi-byte file
        truncated at a quarter of its real length."""
        monkeypatch.setattr(f, "_FILE_READ_CAP", 8)
        wide = tmp_path / "wide.txt"
        wide.write_text("\u00e9" * 8, encoding="utf-8")  # 8 chars, 16 bytes

        resp = await f.api_file_read(_req("/api/file-read", f"path={wide}"))

        assert resp.status == 200
        assert "X-Truncated" not in resp.headers
        assert resp.text == "\u00e9" * 8

    @pytest.mark.asyncio
    async def test_validation_and_open_share_one_worker_transaction(
        self, a_file: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Splitting validation from the open is a symlink TOCTOU: between the
        two, the validated name can be swapped for a link the validator would
        have refused. Two things make that unreachable, and both are asserted --
        the open is the O_NOFOLLOW primitive, not a bare ``open``, and it happens
        on the SAME worker thread as the validation, so no await separates them.
        """
        threads: dict[str, str] = {}
        original_validate = f._validate_dashboard_path
        original_open = f._open_rb_nofollow

        def _validate(raw: str) -> str | None:
            threads["validate"] = threading.current_thread().name
            return original_validate(raw)

        def _open(path: str) -> int:
            threads["open"] = threading.current_thread().name
            return original_open(path)

        monkeypatch.setattr(_HANDLERS_PKG, "_validate_dashboard_path", _validate)
        monkeypatch.setattr(f, "_open_rb_nofollow", _open)

        resp = await f.api_file_read(_req("/api/file-read", f"path={a_file}"))

        assert resp.status == 200
        assert resp.text == "hello"
        assert "open" in threads, "the read must go through the no-follow open"
        assert threads["validate"] == threads["open"], (
            "validation and the open ran on different threads, so an await "
            f"separates them: {threads}"
        )
        # A GET reads the body, so it is a TRANSFER and rides the transfer pool.
        assert threads["open"].startswith(_TRANSFER_THREAD_PREFIX), threads

    @pytest.mark.asyncio
    async def test_head_opens_nothing(self, a_file: Path, monkeypatch: pytest.MonkeyPatch):
        """HEAD answers from the stat. Opening for it would read a file whose
        bytes are then discarded."""
        opened: list[str] = []
        monkeypatch.setattr(
            f, "_open_rb_nofollow", lambda path: opened.append(path) or 0  # type: ignore[func-returns-value]
        )

        resp = await f.api_file_read(_req("/api/file-read", f"path={a_file}", method="HEAD"))

        assert resp.status == 200
        assert opened == []

    @pytest.mark.asyncio
    async def test_a_lost_race_refuses_rather_than_following_a_symlink(
        self, a_file: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The residual window is inside one transaction, so the only way to lose
        it is for the open itself to hit the swapped link -- which O_NOFOLLOW
        refuses with ELOOP. That must not become a served body."""

        def _eloop(path: str) -> int:
            raise OSError(errno.ELOOP, "symlinks not allowed", path)

        monkeypatch.setattr(f, "_open_rb_nofollow", _eloop)

        resp = await f.api_file_read(_req("/api/file-read", f"path={a_file}"))

        assert resp.status == 500
        assert _body(resp)["error"] == "failed to read file"

    @pytest.mark.asyncio
    async def test_head_still_answers_without_a_body(self, a_file: Path, spy: _ThreadSpy):
        spy.watch(a_file)

        resp = await f.api_file_read(_req("/api/file-read", f"path={a_file}", method="HEAD"))

        assert resp.status == 200
        assert resp.headers["X-Path-Kind"] == "file"
        spy.assert_off_loop()


class TestFileWrite:
    @pytest.mark.asyncio
    async def test_validation_runs_off_the_event_loop(self, a_file: Path, spy: _ThreadSpy):
        spy.watch(a_file)
        req = make_mocked_request("POST", "/api/file-write", app=_app())
        # The owner: /api/file-write is owner-gated, and this test is about the
        # probe, not the gate.
        req["user"] = "local-app"
        req["app"] = ""
        with mock.patch.object(
            f,
            "read_bounded_json",
            mock.AsyncMock(return_value=({"path": str(a_file), "content": "bye"}, None)),
        ):
            resp = await f.api_file_write(req)

        assert resp.status == 200, resp.text
        assert a_file.read_text(encoding="utf-8") == "bye", "the write must still happen"
        spy.assert_off_loop()


class TestFileDiff:
    @pytest.mark.asyncio
    async def test_path_resolution_runs_off_the_event_loop(self, a_file: Path, spy: _ThreadSpy):
        spy.watch(a_file)

        resp = await f.api_file_diff(_req("/api/file-diff", f"path={a_file}"))

        assert resp.status == 200
        spy.assert_off_loop()

    @pytest.mark.asyncio
    async def test_a_missing_path_is_still_an_empty_diff(self, tmp_path: Path):
        """Control: not-found stays a 200 with empty strings, not a 404."""
        resp = await f.api_file_diff(_req("/api/file-diff", f"path={tmp_path / 'gone.md'}"))

        assert resp.status == 200
        assert _body(resp) == {"diff": "", "original": ""}

    @pytest.mark.asyncio
    async def test_an_empty_path_still_short_circuits(self):
        resp = await f.api_file_diff(_req("/api/file-diff", "path="))

        assert _body(resp) == {"diff": "", "original": ""}


class TestFileSearch:
    @pytest.mark.asyncio
    async def test_root_resolution_runs_off_the_event_loop(self, tmp_path: Path, spy: _ThreadSpy):
        (tmp_path / "alpha.txt").write_text("x", encoding="utf-8")
        spy.watch(tmp_path)

        resp = await f.api_file_search(_req("/api/file-search", f"q=alpha&project={tmp_path}"))

        assert resp.status == 200
        names = [r["name"] for r in _body(resp)["results"]]
        assert "alpha.txt" in names, "the search must still return results"
        spy.assert_off_loop()

    @pytest.mark.asyncio
    async def test_a_missing_project_root_is_still_a_404(self, tmp_path: Path):
        resp = await f.api_file_search(
            _req("/api/file-search", f"q=alpha&project={tmp_path / 'nope'}")
        )

        assert resp.status == 404
        assert _body(resp)["error"] == "Project directory not found"

    @pytest.mark.asyncio
    async def test_the_fallback_roots_are_still_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Control: with no ?project= / ?workspace=, the project dir from the
        environment is still a search root."""
        (tmp_path / "beta.txt").write_text("x", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))

        resp = await f.api_file_search(_req("/api/file-search", "q=beta"))

        assert resp.status == 200
        assert "beta.txt" in [r["name"] for r in _body(resp)["results"]]


class TestFileGrep:
    @pytest.mark.asyncio
    async def test_root_validation_and_the_search_run_off_the_event_loop(
        self, tmp_path: Path, spy: _ThreadSpy
    ):
        """Content search takes the path twice -- once to validate the root, once
        for the walk that reads every candidate file -- so both have to be off
        the loop, and the search must still answer."""
        (tmp_path / "alpha.txt").write_text("a needle here\n", encoding="utf-8")
        spy.watch(tmp_path)

        resp = await _grep_req("needle", tmp_path)

        assert resp.status == 200
        assert [r["line"] for r in _body(resp)["results"]] == [1]
        spy.assert_off_loop()

    @pytest.mark.asyncio
    async def test_the_walk_runs_on_the_transfer_pool_not_the_probe_pool(self, tmp_path: Path):
        """The search holds its worker for the length of the walk, so it must
        queue behind transfers rather than in front of millisecond validation."""
        (tmp_path / "alpha.txt").write_text("a needle here\n", encoding="utf-8")
        threads: list[str] = []
        original = f._grep_python

        def record(*args, **kwargs):
            threads.append(threading.current_thread().name)
            return original(*args, **kwargs)

        with (
            mock.patch.object(f, "_grep_python", record),
            mock.patch.object(f, "_grep_rg_executable", lambda: None),
        ):
            resp = await _grep_req("needle", tmp_path)

        assert resp.status == 200
        assert threads and all(t.startswith(_TRANSFER_THREAD_PREFIX) for t in threads), threads

    @pytest.mark.asyncio
    async def test_a_file_root_is_still_a_404(self, tmp_path: Path):
        """Control: the offload did not turn a bad root into a search."""
        target = tmp_path / "alpha.txt"
        target.write_text("a needle here\n", encoding="utf-8")

        resp = await _grep_req("needle", target)

        assert resp.status == 404


class TestOfficePreview:
    @pytest.mark.asyncio
    async def test_relative_resolution_runs_off_the_event_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The endpoint the first sweep missed: ``resolve=1`` resolves the path
        against the project dir, which is two realpath calls."""
        (tmp_path / "deck.docx").write_bytes(b"PK\x03\x04")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        threads: list[str] = []
        original = f._resolve_project_relative

        def _resolve(raw: str) -> tuple[str, str | None]:
            threads.append(threading.current_thread().name)
            return original(raw)

        monkeypatch.setattr(f, "_resolve_project_relative", _resolve)

        await f.api_file_office_preview(
            _req("/api/file-office-preview", "path=deck.docx&resolve=1")
        )

        assert threads, "the resolution did not run"
        assert all(t.startswith(_PROBE_THREAD_PREFIX) for t in threads), threads

    @pytest.mark.asyncio
    async def test_an_unresolvable_relative_path_is_still_a_400(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)

        resp = await f.api_file_office_preview(
            _req("/api/file-office-preview", "path=deck.docx&resolve=1")
        )

        assert resp.status == 400
        assert _body(resp)["code"] == "no_project_dir"


class TestBrowseAndReveal:
    @pytest.mark.asyncio
    async def test_browse_dirs_root_resolution_runs_off_the_event_loop(
        self, tmp_path: Path, spy: _ThreadSpy
    ):
        (tmp_path / "sub").mkdir()
        spy.watch(tmp_path)

        resp = await f.api_browse_dirs(_req("/api/browse-dirs", f"path={tmp_path}"))

        assert resp.status == 200
        assert [d["name"] for d in _body(resp)["dirs"]] == ["sub"]
        spy.assert_off_loop()

    @pytest.mark.asyncio
    async def test_browse_files_root_resolution_runs_off_the_event_loop(
        self, tmp_path: Path, spy: _ThreadSpy
    ):
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        spy.watch(tmp_path)

        resp = await f.api_browse_files(_req("/api/browse-files", f"path={tmp_path}"))

        assert resp.status == 200
        assert [x["name"] for x in _body(resp)["files"]] == ["a.txt"]
        spy.assert_off_loop()

    @pytest.mark.asyncio
    async def test_a_non_directory_root_is_still_a_400(self, a_file: Path):
        resp = await f.api_browse_dirs(_req("/api/browse-dirs", f"path={a_file}"))

        assert resp.status == 400
        assert _body(resp)["error"] == "Not a directory"

    @pytest.mark.asyncio
    async def test_an_unnamed_root_still_falls_back_to_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Control: an unnamed root means ``$HOME``, and the shared resolver is
        what supplies it.

        ``HOME`` and ``USERPROFILE`` are both redirected at ``tmp_path``: without
        that, the fallback resolves to the real home and the endpoint ``scandir``s
        it. ``USERPROFILE`` as well as ``HOME`` because that is the one
        ``expanduser`` reads on Windows.
        """
        home = tmp_path / "home"
        (home / "visible").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))

        resp = await f.api_browse_dirs(_req("/api/browse-dirs", "path="))

        assert resp.status == 200
        assert _body(resp)["path"] == os.path.realpath(home)
        assert [d["name"] for d in _body(resp)["dirs"]] == ["visible"]

    @pytest.mark.asyncio
    async def test_reveal_open_stat_runs_off_the_event_loop(
        self, a_file: Path, spy: _ThreadSpy, monkeypatch: pytest.MonkeyPatch
    ):
        spy.watch(a_file)
        monkeypatch.setattr(f, "is_direct_local_request", lambda request: True)
        monkeypatch.setattr(f.platform_compat, "open_with_default_app", lambda path: True)
        req = make_mocked_request("POST", "/api/reveal", app=_app())
        req["user"] = "test-user"
        with mock.patch.object(
            f,
            "read_bounded_json",
            mock.AsyncMock(return_value=({"path": str(a_file), "action": "open"}, None)),
        ):
            resp = await f.api_reveal_path(req)

        assert resp.status == 200
        spy.assert_off_loop()

    @pytest.mark.asyncio
    async def test_reveal_stat_and_launch_share_one_transaction(
        self, a_file: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The launcher takes a path, not the descriptor the stat looked at, so
        the two are a check-then-use pair. An ``await`` between them is a
        scheduler yield inside that window -- long enough to swap the path for a
        symlink the sensitive-path gate already refused, which the launcher then
        follows. Both must run on ONE worker thread.
        """
        threads: dict[str, str] = {}
        monkeypatch.setattr(f, "is_direct_local_request", lambda request: True)

        original_isfile = os.path.isfile

        def _isfile(p: Any, *a: object, **k: object) -> object:
            if str(p) == str(a_file):
                threads["stat"] = threading.current_thread().name
            return original_isfile(p, *a, **k)

        def _launch(p: str) -> bool:
            threads["launch"] = threading.current_thread().name
            return True

        monkeypatch.setattr(os.path, "isfile", _isfile)
        monkeypatch.setattr(f.platform_compat, "open_with_default_app", _launch)
        req = make_mocked_request("POST", "/api/reveal", app=_app())
        req["user"] = "test-user"
        with mock.patch.object(
            f,
            "read_bounded_json",
            mock.AsyncMock(return_value=({"path": str(a_file), "action": "open"}, None)),
        ):
            resp = await f.api_reveal_path(req)

        assert resp.status == 200
        assert "launch" in threads, "the launcher must still be called"
        assert (
            threads["stat"] == threads["launch"]
        ), f"an await separates the stat from the launch: {threads}"
        assert threads["launch"].startswith(_PROBE_THREAD_PREFIX), threads

    @pytest.mark.asyncio
    async def test_reveal_action_runs_off_the_event_loop(
        self, a_file: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The reveal action spawns a file-manager process, which is blocking work
        of its own. No stat pairs with it, so there is no window to hold -- only a
        thread to move it to."""
        seen: list[str] = []
        monkeypatch.setattr(f, "is_direct_local_request", lambda request: True)
        monkeypatch.setattr(
            f.platform_compat,
            "reveal_in_file_manager",
            lambda p: seen.append(threading.current_thread().name) or True,  # type: ignore[func-returns-value]
        )
        req = make_mocked_request("POST", "/api/reveal", app=_app())
        req["user"] = "test-user"
        with mock.patch.object(
            f,
            "read_bounded_json",
            mock.AsyncMock(return_value=({"path": str(a_file), "action": "reveal"}, None)),
        ):
            resp = await f.api_reveal_path(req)

        assert resp.status == 200
        assert seen and all(t != "MainThread" for t in seen)

    @pytest.mark.asyncio
    async def test_a_launcher_that_refuses_still_degrades_to_the_clipboard(
        self, a_file: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Control: the launch verdict now travels out of a worker thread as a
        tuple. A host with no working launcher must still get the clipboard
        fallback rather than a bare ok."""
        monkeypatch.setattr(f, "is_direct_local_request", lambda request: True)
        monkeypatch.setattr(f.platform_compat, "open_with_default_app", lambda p: False)
        req = make_mocked_request("POST", "/api/reveal", app=_app())
        req["user"] = "test-user"
        with mock.patch.object(
            f,
            "read_bounded_json",
            mock.AsyncMock(return_value=({"path": str(a_file), "action": "open"}, None)),
        ):
            resp = await f.api_reveal_path(req)

        assert resp.status == 200
        assert _body(resp) == {"ok": True, "copy": str(a_file)}

    @pytest.mark.asyncio
    async def test_reveal_open_of_a_non_file_is_still_a_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(f, "is_direct_local_request", lambda request: True)
        req = make_mocked_request("POST", "/api/reveal", app=_app())
        req["user"] = "test-user"
        with mock.patch.object(
            f,
            "read_bounded_json",
            mock.AsyncMock(return_value=({"path": str(tmp_path), "action": "open"}, None)),
        ):
            resp = await f.api_reveal_path(req)

        assert resp.status == 400
        assert _body(resp)["error"] == "not a regular file"


# --- 3. the bounded probe pool ----------------------------------------------


@pytest.fixture
def small_pool(monkeypatch: pytest.MonkeyPatch):
    """One two-worker pool standing in for BOTH executors.py pools, with a short
    admission window, so a test can saturate it in milliseconds. One pool for
    both on purpose: the saturation tests are about the gate, and a probe and a
    transfer must be refused the same way when their pool is full."""
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix=_PROBE_THREAD_PREFIX)
    monkeypatch.setattr(f, "_PATH_PROBE_ADMIT_TIMEOUT_SECS", 0.2)
    monkeypatch.setattr(executors, "path_probe_executor", lambda: pool)
    monkeypatch.setattr(executors, "path_transfer_executor", lambda: pool)
    release = threading.Event()
    try:
        yield pool, release
    finally:
        release.set()
        pool.shutdown(wait=True)


def _wedge(release: threading.Event) -> str:
    """Stand-in for a probe whose mount never answers: parks until released."""
    release.wait()
    return threading.current_thread().name


class TestBoundedProbePool:
    @pytest.mark.asyncio
    async def test_saturated_pool_refuses_with_503_and_default_executor_stays_free(
        self, a_file: Path, small_pool
    ):
        """The maintainer's bar, verbatim: saturate the probe workers, prove
        unrelated default-executor work is still schedulable, and prove a new
        probe is refused with the endpoint's coded answer rather than queued."""
        _pool, release = small_pool
        wedged = [asyncio.ensure_future(f._run_path_probe(_wedge, release)) for _ in range(2)]
        await asyncio.sleep(0.05)  # both workers now parked in _wedge

        # Unrelated work on the DEFAULT executor is untouched: it completes well
        # inside the admission window while every probe worker is gone.
        unrelated = await asyncio.wait_for(asyncio.to_thread(lambda: 42), timeout=1.0)
        assert unrelated == 42

        # A real endpoint request is refused, not queued behind the wedge.
        resp = await f.api_file_read(_req("/api/file-read", f"path={a_file}"))
        assert resp.status == 503
        assert _body(resp) == {
            "error": "file system probe capacity exhausted; retry shortly",
            "code": "path_probe_busy",
        }

        # And the refusal is a property of the SATURATED pool, not of the file:
        # once the workers return, the same request succeeds.
        release.set()
        names = await asyncio.gather(*wedged)
        assert all(n.startswith(_PROBE_THREAD_PREFIX) for n in names), names
        resp = await f.api_file_read(_req("/api/file-read", f"path={a_file}"))
        assert resp.status == 200
        assert resp.text == "hello"

    @pytest.mark.asyncio
    async def test_a_cancelled_awaiter_does_not_free_a_wedged_worker(self, small_pool):
        """A client that gives up on a wedged probe cancels its await; the worker
        thread is still parked in the syscall. Capacity must follow the THREAD,
        not the await: with the pool's own worker count as the only accounting
        there is nothing to drift, and this pins that no bookkeeping was added
        in front of it that could."""
        _pool, release = small_pool
        abandoned = [asyncio.ensure_future(f._run_path_probe(_wedge, release)) for _ in range(2)]
        await asyncio.sleep(0.05)
        for task in abandoned:
            task.cancel()
        await asyncio.gather(*abandoned, return_exceptions=True)

        # Both workers are still wedged, so the pool must still refuse.
        with pytest.raises(f._PathProbeBusy):
            await f._run_path_probe(lambda: None)

        release.set()
        await asyncio.sleep(0.05)  # the parked threads return their workers
        assert await f._run_path_probe(lambda: "free") == "free"

    @pytest.mark.asyncio
    async def test_busy_is_a_coded_503_on_every_probe_taking_endpoint(
        self, a_file: Path, tmp_path: Path, small_pool, monkeypatch: pytest.MonkeyPatch
    ):
        """Each endpoint maps the refusal onto ONE answer, and the error-code
        contract needs the code on all of them, not just the one exercised above."""
        _pool, release = small_pool
        wedged = [asyncio.ensure_future(f._run_path_probe(_wedge, release)) for _ in range(2)]
        await asyncio.sleep(0.05)
        monkeypatch.setattr(f, "is_direct_local_request", lambda request: True)

        async def post(handler, body: dict) -> web.Response:
            req = make_mocked_request("POST", "/api/x", app=_app())
            # The owner, so the owner-gated file_write reaches its probe.
            req["user"] = "local-app"
            req["app"] = ""
            with mock.patch.object(
                f, "read_bounded_json", mock.AsyncMock(return_value=(body, None))
            ):
                return await handler(req)

        answers = {
            "file_read": await f.api_file_read(_req("/api/file-read", f"path={a_file}")),
            "file_raw": await f.api_file_raw(_req("/api/file-raw", f"path={a_file}")),
            "file_download": await f.api_file_download(
                _req("/api/file-download", f"path={a_file}")
            ),
            "file_watch": await f.api_file_watch(_req("/api/file-watch", f"path={a_file}")),
            "file_diff": await f.api_file_diff(_req("/api/file-diff", f"path={a_file}")),
            "file_search": await f.api_file_search(
                _req("/api/file-search", f"q=note&project={tmp_path}")
            ),
            "file_grep": await _grep_req("note", tmp_path),
            "browse_dirs": await f.api_browse_dirs(_req("/api/browse-dirs", f"path={tmp_path}")),
            "browse_files": await f.api_browse_files(_req("/api/browse-files", f"path={tmp_path}")),
            "file_write": await post(f.api_file_write, {"path": str(a_file), "content": "x"}),
            "reveal_open": await post(f.api_reveal_path, {"path": str(a_file), "action": "open"}),
            "file_office_preview": await f.api_file_office_preview(
                _req("/api/file-office-preview", f"path={a_file}")
            ),
        }
        for name, resp in answers.items():
            assert resp.status == 503, (name, resp.status, resp.text)
            assert _body(resp)["code"] == "path_probe_busy", name

        release.set()
        await asyncio.gather(*wedged)


class TestExecutorPools:
    def test_probe_and_transfer_pools_are_distinct_bounded_and_named(self):
        """Two pools, not one: a burst of large transfers must queue behind
        transfers, never in front of the millisecond validation probes."""
        probe = executors.path_probe_executor()
        transfer = executors.path_transfer_executor()
        assert probe is not transfer
        assert probe is executors.path_probe_executor()
        assert transfer is executors.path_transfer_executor()
        assert probe._max_workers == executors._MAX_PATH_PROBE_WORKERS
        assert transfer._max_workers == executors._MAX_PATH_TRANSFER_WORKERS
        assert probe._thread_name_prefix == _PROBE_THREAD_PREFIX
        assert transfer._thread_name_prefix == _TRANSFER_THREAD_PREFIX
        assert probe is not executors.path_resolve_executor()

    def test_shutdown_resets_both_pools(self):
        probe = executors.path_probe_executor()
        transfer = executors.path_transfer_executor()
        executors.shutdown_maintenance_executor()
        assert executors.path_probe_executor() is not probe
        assert executors.path_transfer_executor() is not transfer


class TestFileWatch:
    @pytest.mark.asyncio
    async def test_a_refused_path_is_still_refused(self, a_file: Path):
        with mock.patch.object(f, "_validate_dashboard_path", return_value=None):
            resp = await f.api_file_watch(_req("/api/file-watch", f"path={a_file}"))

        assert resp.status == 400
        assert _body(resp)["error"] == "invalid or forbidden path"

    @pytest.mark.asyncio
    async def test_a_missing_path_is_still_a_404(self, tmp_path: Path, spy: _ThreadSpy):
        spy.watch(tmp_path)

        resp = await f.api_file_watch(_req("/api/file-watch", f"path={tmp_path / 'gone.md'}"))

        assert resp.status == 404
        spy.assert_off_loop()
