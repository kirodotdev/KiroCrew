"""The codebase session's scan (``dashboard/codebase_scan.py``) and its route."""

from __future__ import annotations

import os
import subprocess

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_worktree_create import _sandbox_exec_reason

from kiro_crew.dashboard import codebase_scan
from kiro_crew.dashboard.handlers.codebase_scan import register_codebase_routes


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _unwrapped_spawn(argv, mode="standard", **kw):
    """``sandboxed_spawn_argv`` with no sandbox: the argv as given, the caller's env."""
    return list(argv), dict(os.environ), None


@pytest.fixture(autouse=True)
def _scan_git(monkeypatch):
    """The scan's git: sandboxed where the host can run it, plain otherwise.

    Hosted CI runners pass the backend probe but deny ``unshare(NEWNS)`` at exec
    time, and Windows has no backend at all; skipping there would leave the scan
    untested, so those hosts run git unwrapped. The chokepoint itself is pinned by
    the two routing tests below on every host.
    """
    if _sandbox_exec_reason():
        monkeypatch.setattr(codebase_scan, "sandboxed_spawn_argv", _unwrapped_spawn)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    files = {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "import os\n\ndef run():\n    return 1\n",
        "src/pkg/cli.py": "from pkg.core import run\nfrom . import util\n",
        "src/pkg/util.py": "x = 1",
        "web/app.ts": "import { a } from './lib/a'\nimport b from './b.js'\nimport 'react'\n",
        "web/lib/a.ts": "export const a = 1\n",
        "web/b.ts": "export default 2\n",
        "web/lib/index.tsx": "export * from './a'\n",
        "logo.png": "\0\0binary",
        "web/package-lock.json": "{}\n" * 10,
        "big.json": "1\n" * 3001,
        "small.json": "1\n" * 10,
    }
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (root / "ignored.txt").write_text("nope\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")
    return root


def _files(model):
    """Rows decoded to ``{path: {"lines", "lang"}}``."""
    return {p: {"lines": n, "lang": model["langs"][li]} for p, n, li, _ in model["files"]}


def _edges(model):
    paths = [row[0] for row in model["files"]]
    return {(paths[a], paths[b]) for a, b in model["edges"]}


def test_it_lists_files_with_lines_and_language_and_skips_ignored(repo):
    model = codebase_scan.scan(str(repo))
    by_path = _files(model)
    assert "ignored.txt" not in by_path
    assert by_path["src/pkg/core.py"]["lines"] == 4
    assert by_path["src/pkg/util.py"]["lines"] == 1  # no trailing newline
    assert by_path["src/pkg/core.py"]["lang"] == "python"
    assert by_path["web/app.ts"]["lang"] == "typescript"
    assert by_path["logo.png"]["lines"] == 0
    # A lock file, and a data file too long to be hand-written, are drawn as generated.
    assert by_path["web/package-lock.json"]["lang"] == "generated"
    assert by_path["big.json"]["lang"] == "generated"
    assert by_path["small.json"]["lang"] == "json"
    assert model["branch"] == "main" and model["head"]
    assert model["root"] == str(repo.resolve())


def test_it_finds_python_and_relative_js_imports_and_drops_the_rest(repo):
    edges = _edges(codebase_scan.scan(str(repo)))
    assert ("src/pkg/cli.py", "src/pkg/core.py") in edges
    assert ("src/pkg/cli.py", "src/pkg/util.py") in edges  # from . import util
    assert ("web/app.ts", "web/lib/a.ts") in edges
    assert ("web/app.ts", "web/b.ts") in edges  # './b.js' names the .ts file
    assert ("web/lib/index.tsx", "web/lib/a.ts") in edges
    # `import os` and `import 'react'` name nothing in the tree.
    assert not any(a == "src/pkg/core.py" for a, _ in edges)
    assert len([e for e in edges if e[0] == "web/app.ts"]) == 2


def test_an_unchanged_tree_answers_unchanged_and_a_new_file_shows_up(repo):
    first = codebase_scan.scan(str(repo))
    assert codebase_scan.scan(str(repo), since=first["sig"]) == {
        "unchanged": True,
        "sig": first["sig"],
    }
    (repo / "src/pkg/new.py").write_text("a\nb\n", encoding="utf-8")  # untracked, not ignored
    again = codebase_scan.scan(str(repo), since=first["sig"])
    assert again["sig"] != first["sig"]
    assert "src/pkg/new.py" in _files(again)


def test_editing_an_already_dirty_file_again_changes_the_sig_and_its_lines(repo):
    """git status reads the same for a file dirtied twice, so the file's own stat
    must move the sig; and the per-file cache must not hand back the old count."""
    target = repo / "src/pkg/util.py"
    target.write_text("a\nb\n", encoding="utf-8")
    os.utime(target, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    first = codebase_scan.scan(str(repo))
    assert _files(first)["src/pkg/util.py"]["lines"] == 2
    target.write_text("a\nb\nc\n", encoding="utf-8")
    os.utime(target, ns=(1_000_000_001_000_000_000, 1_000_000_001_000_000_000))
    again = codebase_scan.scan(str(repo), since=first["sig"])
    assert "unchanged" not in again
    assert _files(again)["src/pkg/util.py"]["lines"] == 3


def test_a_deleted_file_drops_out_with_its_edges(repo):
    first = codebase_scan.scan(str(repo))
    (repo / "src/pkg/core.py").unlink()
    again = codebase_scan.scan(str(repo), since=first["sig"])
    assert "src/pkg/core.py" not in _files(again)
    assert not any("src/pkg/core.py" in e for e in _edges(again))


def test_a_subdirectory_scans_its_whole_work_tree(repo):
    assert codebase_scan.scan(str(repo / "web"))["root"] == str(repo.resolve())


@pytest.mark.parametrize("bad", ["", "relative/path", "/definitely/not/here"])
def test_a_bad_root_is_refused(bad):
    with pytest.raises(codebase_scan.ScanError) as caught:
        codebase_scan.scan(bad)
    assert caught.value.code == "invalid_root"


def test_a_directory_outside_git_is_refused(tmp_path):
    with pytest.raises(codebase_scan.ScanError) as caught:
        codebase_scan.scan(str(tmp_path))
    assert caught.value.code == "not_a_git_tree"


def test_every_git_call_goes_through_the_sandbox_chokepoint(repo, monkeypatch):
    wrapped: list[list[str]] = []

    def record(argv, mode="standard", **kw):
        wrapped.append(list(argv))
        return _unwrapped_spawn(argv, mode, **kw)

    monkeypatch.setattr(codebase_scan, "sandboxed_spawn_argv", record)
    codebase_scan.scan(str(repo))
    assert wrapped
    assert all(argv[:2] == ["git", "-C"] for argv in wrapped)
    assert ["rev-parse", "--show-toplevel"] in [argv[3:] for argv in wrapped]


def test_no_sandbox_backend_refuses_the_scan(repo, monkeypatch):
    def unavailable(argv, mode="standard", **kw):
        raise RuntimeError("no OS sandbox backend")

    monkeypatch.setattr(codebase_scan, "sandboxed_spawn_argv", unavailable)
    with pytest.raises(codebase_scan.ScanError) as caught:
        codebase_scan.scan(str(repo))
    assert caught.value.code == "sandbox_unavailable"


def _scan_app(*, user: str = "local-app", app_name: str = "") -> web.Application:
    @web.middleware
    async def caller(request, handler):
        request["user"] = user
        request["app"] = app_name
        return await handler(request)

    app = web.Application(middlewares=[caller])
    app["state"] = type("S", (), {"owner_id": ""})()
    register_codebase_routes(app)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [{"app_name": "notes"}, {"user": ""}, {"user": "someone"}])
async def test_anyone_but_the_owner_is_refused(repo, caller):
    async with TestClient(TestServer(_scan_app(**caller))) as client:
        refused = await client.get("/api/codebase/scan", params={"root": str(repo)})
        assert refused.status == 403


@pytest.mark.asyncio
async def test_the_owner_gets_the_model_and_a_bad_root_is_a_400(repo):
    async with TestClient(TestServer(_scan_app())) as client:
        ok = await client.get("/api/codebase/scan", params={"root": str(repo)})
        assert ok.status == 200
        assert "web/app.ts" in _files(await ok.json())
        bad = await client.get("/api/codebase/scan", params={"root": "nope"})
        assert bad.status == 400
        assert (await bad.json())["code"] == "invalid_root"
