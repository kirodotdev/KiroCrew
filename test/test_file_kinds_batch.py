"""``POST /api/file-kinds`` -- the batched form of ``HEAD /api/file-read``.

The dashboard classifies every path-like string in a view to decide which become
chips. The batch must answer each path exactly as the per-path probe does
(``X-Path-Kind`` on a 2xx/404, ``missing`` for every refusal), or a chip would
appear or vanish depending on which route the client used -- and a batch that
told a refused path from an absent one would be an existence oracle the per-path
route is not.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard.handlers import files as files_mod


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-read", files_mod.api_file_read)
    app.router.add_post("/api/file-kinds", files_mod.api_file_kinds)
    return as_owner(app)


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.sel.sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


@pytest.fixture()
def home(tmp_path):
    real_realpath = os.path.realpath

    def fake_expanduser(p):
        return p.replace("~", str(tmp_path))

    with (
        patch("os.path.expanduser", side_effect=fake_expanduser),
        patch("os.path.realpath", side_effect=real_realpath),
        patch("pathlib.Path.home", return_value=tmp_path),
    ):
        yield tmp_path


async def _head_kind(client: TestClient, path: str) -> str:
    """What the chip probe concludes from the per-path route today."""
    absolute = path.startswith(("/", "~")) or path[1:3] in (":\\", ":/")
    params = {"path": path} if absolute else {"path": path, "resolve": "1"}
    resp = await client.head("/api/file-read", params=params)
    header = resp.headers.get("X-Path-Kind")
    if header in ("file", "dir"):
        return header
    return "file" if resp.status == 200 else "missing"


@pytest.mark.asyncio
async def test_each_path_gets_the_per_path_routes_answer(tmp_path, home, mock_sel, monkeypatch):
    proj = tmp_path / "project"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "app.py").write_text("x", encoding="utf-8")
    (tmp_path / "notes.md").write_text("n", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("k", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))

    paths = [
        str(tmp_path / "notes.md"),
        str(tmp_path / "folder"),
        str(tmp_path / "gone.md"),
        "src/app.py",
        "src",
        "src/nope.py",
        "../escape.md",
        "~/.ssh/id_rsa",
        "~/.ssh/absent_key",
        "/api/sessions/health",
        "",
    ]
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/file-kinds", json={"paths": paths})
        assert resp.status == 200
        kinds = (await resp.json())["kinds"]
        expected = {p: await _head_kind(client, p) for p in paths}
    assert kinds == expected
    # Spot-check the answers themselves, so a parity bug shared by both routes
    # still fails here.
    assert kinds[str(tmp_path / "notes.md")] == "file"
    assert kinds[str(tmp_path / "folder")] == "dir"
    assert kinds["src/app.py"] == "file"
    assert kinds["src"] == "dir"
    # A refused credential store reads exactly like an absent one.
    assert kinds["~/.ssh/id_rsa"] == kinds["~/.ssh/absent_key"] == "missing"
    assert kinds["../escape.md"] == "missing"


@pytest.mark.asyncio
async def test_a_refusal_is_audited_as_denied_and_an_absence_as_not_found(tmp_path, home, mock_sel):
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("k", encoding="utf-8")
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(
            "/api/file-kinds", json={"paths": ["~/.ssh/id_rsa", str(tmp_path / "gone.md")]}
        )
        assert resp.status == 200
    outcomes = sorted(c.kwargs["outcome"] for c in mock_sel.log_tool_invocation.call_args_list)
    assert outcomes == ["denied", "not_found"]


@pytest.mark.asyncio
async def test_the_whole_list_takes_one_probe_hop(tmp_path, home, mock_sel):
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    real = files_mod._run_path_probe
    hops: list[object] = []

    async def counting(fn, *args, **kwargs):
        hops.append(fn)
        return await real(fn, *args, **kwargs)

    paths = [str(tmp_path / f"{i}.md") for i in range(30)] + [str(tmp_path / "a.md")]
    with patch.object(files_mod, "_run_path_probe", counting):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/file-kinds", json={"paths": paths})
            assert resp.status == 200
            kinds = (await resp.json())["kinds"]
    assert len(hops) == 1
    assert kinds[str(tmp_path / "a.md")] == "file"
    assert sum(1 for k in kinds.values() if k == "missing") == 30


@pytest.mark.asyncio
async def test_duplicates_are_probed_once_and_an_empty_list_probes_nothing(
    tmp_path, home, mock_sel
):
    target = str(tmp_path / "dup.md")
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/file-kinds", json={"paths": [target, target, target]})
        assert (await resp.json())["kinds"] == {target: "missing"}
        assert mock_sel.log_tool_invocation.call_count == 1
        resp = await client.post("/api/file-kinds", json={"paths": []})
        assert resp.status == 200
        assert (await resp.json())["kinds"] == {}
    assert mock_sel.log_tool_invocation.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"paths": "/etc/hosts"},
        {"paths": [1, 2]},
        {"paths": ["/x"] * (files_mod._FILE_KINDS_MAX_PATHS + 1)},
    ],
)
async def test_a_malformed_request_is_refused_with_a_code(body, home, mock_sel):
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/file-kinds", json=body)
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_paths"
    mock_sel.log_tool_invocation.assert_not_called()


@pytest.mark.asyncio
async def test_a_busy_probe_pool_answers_503(home, mock_sel):
    async def busy(*_args, **_kwargs):
        raise files_mod._PathProbeBusy()

    with patch.object(files_mod, "_run_path_probe", busy):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/file-kinds", json={"paths": ["/x/a.md"]})
            assert resp.status == 503
            assert (await resp.json())["code"] == "path_probe_busy"
