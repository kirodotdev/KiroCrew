"""Tests for the code-intel app's routes: the gate, validation, the error mapping, and
how a manager answer becomes the host's `EditorBindingResult`.

The language-server pool is replaced with a stub; ``test_code_intel.py`` covers it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.code_intel import CodeIntelError
from kiro_crew.apps.builtins.code_intel.backend import routes

pytestmark = pytest.mark.asyncio

BINDING = "/api/apps/code-intel/binding"
POOL = "/api/apps/code-intel/pool"
_OWNER = "owner-user"


@web.middleware
async def _as_owner(request: web.Request, handler):
    """Stamp the dashboard owner's identity, as the token middleware would."""
    request["user"] = _OWNER
    request["app"] = ""
    return await handler(request)


def _make_app(owner: bool = True) -> web.Application:
    app = web.Application(middlewares=[_as_owner] if owner else [])
    routes.register_routes(app)
    state = MagicMock()
    state.owner_id = _OWNER
    app["state"] = state
    return app


@pytest.fixture(autouse=True)
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


@pytest.fixture(autouse=True)
def app_enabled():
    with patch.object(routes, "is_app_enabled", return_value=True) as m:
        yield m


@pytest.fixture
def pool():
    stub = MagicMock()
    with patch("kiro_crew.apps.builtins.code_intel.code_intel_manager", return_value=stub):
        yield stub


async def _post(
    route: str, body: dict, owner: bool = True, lang: str | None = None
) -> tuple[int, dict]:
    if lang is not None:
        body = {**body, "lang": lang}
    async with TestClient(TestServer(_make_app(owner))) as client:
        resp = await client.post(route, json=body)
        return resp.status, await resp.json()


def _at(path, **extra) -> dict:
    return {
        "item_id": "definition",
        "path": str(path),
        "line": 0,
        "character": 1,
        "dirty": False,
        "sha256": "a" * 64,
        "trigger": "f12",
        **extra,
    }


# ── the binding endpoint ────────────────────────────────────────────────────


async def test_a_single_definition_opens_at_once(tmp_path, pool, mock_sel):
    target = tmp_path / "a.py"
    target.write_text("x = 1\n")
    pool.handle.return_value = {
        "locations": [{"path": "/p/b.py", "line": 3, "character": 4, "preview": "def b(): ..."}],
        "server": "python",
        "document": str(target.resolve()),
    }
    status, body = await _post(BINDING, _at(target))
    assert status == 200
    assert body == {
        "title": "Definitions",
        "open": {
            "path": "/p/b.py",
            "line": 3,
            "character": 4,
            "label": "b.py:3",
            "detail": "def b(): ...",
        },
    }
    pool.handle.assert_called_once_with("definition", str(target.resolve()), 0, 1, None, "a" * 64)
    assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "allowed"


async def test_several_references_become_a_picker_with_footnotes(tmp_path, pool):
    target = tmp_path / "a.py"
    target.write_text("")
    pool.handle.return_value = {
        "locations": [
            {"path": "/p/a.py", "line": 1, "character": 0, "preview": "x"},
            {"path": "/p/c.py", "line": 9, "character": 2, "preview": "y"},
        ],
        "truncated": True,
        "external": 2,
        "server": "python",
    }
    status, body = await _post(BINDING, _at(target, item_id="references", trigger="shift+f12"))
    assert status == 200
    assert body["title"] == "References"
    assert [r["label"] for r in body["open"]] == ["a.py:1", "c.py:9"]
    assert "Only the first results are shown." in body["markdown"]
    assert "2 results outside this machine's files" in body["markdown"]


async def test_the_outline_is_a_list_even_with_one_row_and_keeps_its_depth(tmp_path, pool):
    target = tmp_path / "a.py"
    target.write_text("")
    pool.handle.return_value = {
        "symbols": [
            {"name": "A", "kind": "Class", "detail": "", "line": 1, "depth": 0},
            {"name": "run", "kind": "Method", "detail": "() -> None", "line": 2, "depth": 1},
        ],
        "server": "python",
    }
    status, body = await _post(BINDING, _at(target, item_id="symbols", trigger="primary+shift+o"))
    assert status == 200 and body["title"] == "Outline"
    assert [r["label"] for r in body["open"]] == ["A", "\u2007\u2007run"]
    assert body["open"][1] == {
        "path": str(target.resolve()),
        "line": 2,
        "label": "\u2007\u2007run",
        "detail": "Method () -> None",
    }
    pool.handle.return_value = {
        "symbols": [{"name": "A", "kind": "Class", "detail": "", "line": 1, "depth": 0}],
        "server": "python",
    }
    status, body = await _post(BINDING, _at(target, item_id="symbols"))
    assert isinstance(body["open"], list) and len(body["open"]) == 1


async def test_hover_is_markdown_and_an_empty_answer_says_why(tmp_path, pool):
    target = tmp_path / "a.py"
    target.write_text("")
    pool.handle.return_value = {"contents": "**int** x", "server": "python"}
    status, body = await _post(BINDING, _at(target, item_id="hover", trigger="alt+f12"))
    assert status == 200 and body == {"title": "Symbol info", "markdown": "**int** x"}
    pool.handle.return_value = {
        "locations": [],
        "server": "python",
        "notices": [{"message": "index incomplete"}],
        "hint": "run build first",
    }
    status, body = await _post(BINDING, _at(target))
    assert status == 200 and "open" not in body
    assert body["markdown"].splitlines() == [
        "The language server reported a problem. It may be why nothing was found.",
        "- index incomplete",
        "- run build first",
    ]
    pool.handle.return_value = {"locations": [], "server": "python"}
    status, body = await _post(BINDING, _at(target))
    assert body["markdown"] == "Nothing found"


async def test_a_mismatched_hash_answers_stale_and_the_text_is_used_on_the_retry(tmp_path, pool):
    target = tmp_path / "a.py"
    target.write_text("x = 1\n")
    pool.handle.side_effect = CodeIntelError("needs text", "text_required")
    status, body = await _post(BINDING, _at(target))
    assert status == 200 and body == {"stale": True}
    pool.handle.side_effect = None
    pool.handle.return_value = {"contents": "x", "server": "python"}
    status, _ = await _post(BINDING, _at(target, item_id="hover", text="x = 2\n"))
    assert status == 200
    pool.handle.assert_called_with("hover", str(target.resolve()), 0, 1, "x = 2\n", "a" * 64)


async def test_sentences_come_from_the_catalog_in_the_dashboards_language(
    tmp_path, pool, monkeypatch
):
    from kiro_crew.apps.builtins.code_intel.backend import routes

    configured = ""
    monkeypatch.setattr(routes, "_configured_language", lambda: configured)
    target = tmp_path / "a.py"
    target.write_text("")
    pool.handle.side_effect = CodeIntelError(
        "nope", "no_server", server="python", install=["pip install pyright"]
    )
    status, body = await _post(BINDING, _at(target))
    assert status == 422
    assert (
        body["error"]
        == "No python language server is installed. Install one of: pip install pyright."
    )
    assert body["code"] == "no_server" and body["install"] == ["pip install pyright"]
    status, body = await _post(BINDING, _at(target), lang="de")
    assert (
        status == 422
        and "python" in body["error"]
        and body["error"]
        != "No python language server is installed. Install one of: pip install pyright."
    )
    # A tag core does not ship, or one not tag-shaped, is refused like an unset one.
    for refused in ("xx", "de-DE,de;q=0.9", "<b>"):
        status, body = await _post(BINDING, _at(target), lang=refused)
        assert body["error"].startswith("No python language server"), refused
    # Without a usable hint the configured dashboard language applies.
    configured = "de"
    status, german = await _post(BINDING, _at(target))
    assert (
        german["error"]
        != "No python language server is installed. Install one of: pip install pyright."
    )
    status, body = await _post(BINDING, _at(target), lang="fr")
    assert body["error"] != german["error"] and "python" in body["error"]


async def test_non_owner_and_disabled_app_are_refused(tmp_path, pool, app_enabled):
    status, _ = await _post(BINDING, _at(tmp_path / "a.py"), owner=False)
    assert status in (401, 403)
    app_enabled.return_value = False
    status, body = await _post(BINDING, _at(tmp_path / "a.py"))
    assert status == 403 and body["code"] == "app_disabled"
    status, body = await _post(POOL, {"op": "servers"})
    assert status == 403 and body["code"] == "app_disabled"
    pool.handle.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {"path": "/x/a.py", "line": 0, "character": 0},
        {"item_id": "hover"},
        {"item_id": "hover", "path": "/x/a.py", "line": -1},
        {"item_id": "hover", "path": "/x/a.py", "character": "3"},
        {"item_id": "hover", "path": "/x/a.py", "line": True},
        {"item_id": "hover", "path": "/x/a.py", "text": 5},
        {"item_id": "hover", "path": "/x/a.py", "sha256": "A" * 64},
        {"item_id": "hover", "path": "/x/a.py", "sha256": "a" * 63},
        {"item_id": "format", "path": "/x/a.py"},
    ],
)
async def test_bad_request(body, pool):
    status, answer = await _post(BINDING, body)
    assert status == 400 and answer["code"] == "bad_request"
    pool.handle.assert_not_called()


async def test_sensitive_path_is_refused(tmp_path, pool, mock_sel):
    target = tmp_path / "a.py"
    target.write_text("")
    with patch("kiro_crew.security.is_sensitive_path", return_value=True):
        status, answer = await _post(BINDING, _at(target))
    assert status == 403 and answer["code"] == "sensitive_path"
    assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"
    pool.handle.assert_not_called()


async def test_missing_file(tmp_path, pool):
    status, answer = await _post(BINDING, _at(tmp_path / "gone.py"))
    assert status == 404 and answer["code"] == "not_found"


@pytest.mark.parametrize(
    "code,status",
    [
        ("no_server", 422),
        ("not_running", 409),
        ("server_starting", 503),
        ("timeout", 504),
        ("server_failed", 502),
    ],
)
async def test_pool_errors_map_to_status(tmp_path, pool, code, status):
    target = tmp_path / "a.py"
    target.write_text("")
    pool.handle.side_effect = CodeIntelError(
        "nope", code, server="python", root="/proj", install=["pyright"]
    )
    got, answer = await _post(BINDING, _at(target))
    assert got == status
    assert answer["code"] == code and answer["server"] == "python" and answer["root"] == "/proj"
    assert answer["install"] == ["pyright"] and answer["detail"] == "nope"


async def test_error_text_is_redacted(tmp_path, pool):
    """A start error or JSON-RPC message is the server's own words: scrubbed like `servers`."""
    target = tmp_path / "a.py"
    target.write_text("")
    key = "AKIAIOSFODNN7EXAMPLE"  # the example key the redaction tests use
    pool.handle.side_effect = CodeIntelError(f"failed: AWS_ACCESS_KEY_ID={key}", "server_failed")
    status, answer = await _post(BINDING, _at(target))
    assert status == 502
    assert key not in answer["error"] and answer["error"].startswith("failed:")
    assert key not in answer["detail"]


async def test_the_tab_answers_while_every_request_worker_waits_on_a_server(tmp_path, pool):
    """Status, Stop and the path checks run on their own pool, not behind slow requests."""
    import asyncio
    import threading

    from kiro_crew.apps.builtins.code_intel import pools

    release = threading.Event()
    pool.handle.side_effect = lambda *a: (release.wait(10), {"server": "python", "contents": "x"})[
        1
    ]
    pool.servers.return_value = {"servers": [], "maxServers": 4, "idleShutdownSecs": 1}
    pool.stop.return_value = True
    target = tmp_path / "a.py"
    target.write_text("")
    async with TestClient(TestServer(_make_app())) as client:
        slow = [
            asyncio.ensure_future(client.post(BINDING, json=_at(target, item_id="hover")))
            for _ in range(pools._MAX_REQUEST_WORKERS)
        ]
        while pool.handle.call_count < pools._MAX_REQUEST_WORKERS:
            await asyncio.sleep(0.01)
        try:
            for body in (
                {"op": "servers"},
                {"op": "stop", "server": "python", "root": str(tmp_path)},
            ):
                resp = await asyncio.wait_for(client.post(POOL, json=body), 3)
                assert resp.status == 200
        finally:
            release.set()
            for fut in slow:
                assert (await fut).status == 200


# ── the pool endpoint ───────────────────────────────────────────────────────


async def test_servers_needs_no_path(pool):
    pool.servers.return_value = {"servers": [], "maxServers": 4, "idleShutdownSecs": 900}
    status, body = await _post(POOL, {"op": "servers"})
    assert status == 200 and body["maxServers"] == 4


async def test_status_and_symbols_take_a_file(tmp_path, pool):
    target = tmp_path / "a.py"
    target.write_text("")
    pool.handle.return_value = {"server": "python", "available": True}
    status, body = await _post(POOL, {"op": "status", "path": str(target)})
    assert status == 200 and body["available"] is True
    pool.handle.assert_called_with("status", str(target.resolve()), 0, 0, None, None)
    pool.handle.return_value = {"symbols": [], "server": "python"}
    status, body = await _post(POOL, {"op": "symbols", "path": str(target)})
    assert status == 200 and body["symbols"] == []
    status, _ = await _post(POOL, {"op": "symbols"})
    assert status == 400


async def test_pool_ops_are_owner_only(tmp_path, pool):
    for op in (
        "servers",
        "detect",
        "restart",
        "stop",
        "workspace_symbols",
        "workspace_symbols_all",
        "open_class",
        "status",
    ):
        status, _ = await _post(
            POOL,
            {
                "op": op,
                "path": str(tmp_path),
                "root": str(tmp_path),
                "server": "java",
                "query": "x",
                "uri": "jdt://contents/a.jar/A.class",
            },
            owner=False,
        )
        assert status in (401, 403)
    pool.servers.assert_not_called()
    pool.restart.assert_not_called()


async def test_an_unknown_op_is_a_bad_request(pool):
    status, body = await _post(POOL, {"op": "format"})
    assert status == 400 and body["code"] == "bad_request"


async def test_merged_symbol_search_and_open_class(tmp_path, pool, mock_sel):
    pool.workspace_symbols_all.return_value = {"symbols": [], "servers": 2, "errors": []}
    status, body = await _post(POOL, {"op": "workspace_symbols_all", "query": "Stage"})
    assert status == 200 and body["servers"] == 2
    pool.workspace_symbols_all.assert_called_once_with("Stage")
    for bad in ("", "   ", 7, "x" * 257):
        status, _ = await _post(POOL, {"op": "workspace_symbols_all", "query": bad})
        assert status == 400
    uri = "jdt://contents/a.jar/x/A.class"
    pool.open_class.return_value = {"path": "/cache/A.java"}
    status, body = await _post(
        POOL, {"op": "open_class", "server": "java", "root": str(tmp_path), "uri": uri}
    )
    assert status == 200 and body["path"] == "/cache/A.java"
    pool.open_class.assert_called_once_with("java", str(tmp_path.resolve()), uri)
    status, _ = await _post(
        POOL, {"op": "open_class", "server": "java", "root": str(tmp_path), "uri": 3}
    )
    assert status == 400


async def test_detect_takes_a_directory(tmp_path, pool):
    pool.detect.return_value = {"projects": []}
    status, _ = await _post(POOL, {"op": "detect", "path": str(tmp_path)})
    assert status == 200
    pool.detect.assert_called_once_with(str(tmp_path.resolve()))
    (tmp_path / "f.py").write_text("")
    status, body = await _post(POOL, {"op": "detect", "path": str(tmp_path / "f.py")})
    assert status == 404 and body["code"] == "not_found"


async def test_workspace_symbols_start_restart_and_stop(tmp_path, pool, mock_sel):
    root = str(tmp_path.resolve())
    pool.workspace_symbols.return_value = {"symbols": []}
    status, _ = await _post(
        POOL, {"op": "workspace_symbols", "root": root, "server": "python", "query": "help"}
    )
    assert status == 200
    pool.workspace_symbols.assert_called_once_with("python", root, "help")
    status, body = await _post(POOL, {"op": "start", "root": root, "server": "python"})
    assert status == 200 and body == {"ok": True}
    pool.start.assert_called_once_with("python", root)
    status, body = await _post(POOL, {"op": "restart", "root": root, "server": "python"})
    assert status == 200 and body == {"ok": True}
    pool.restart.assert_called_once_with("python", root)
    pool.stop.return_value = False
    status, body = await _post(POOL, {"op": "stop", "root": root, "server": "python"})
    assert status == 200 and body == {"ok": False}
    resources = [c.kwargs["resources"] for c in mock_sel.log_api_access.call_args_list]
    assert any("op=restart" in r for r in resources)


async def test_stop_works_once_the_project_directory_is_gone(tmp_path, pool):
    gone = tmp_path / "gone"
    pool.stop.return_value = True
    status, body = await _post(POOL, {"op": "stop", "root": str(gone), "server": "python"})
    assert status == 200 and body == {"ok": True}
    pool.stop.assert_called_once_with("python", str(gone.resolve()))
    status, body = await _post(POOL, {"op": "start", "root": str(gone), "server": "python"})
    assert status == 404 and body["code"] == "not_found"


def test_catalog_lookup_falls_back_to_english_then_the_key():
    from kiro_crew.apps.builtins.code_intel.i18n import t

    assert t("en", "no_server", server="go", commands="brew install gopls") == (
        "No go language server is installed. Install one of: brew install gopls."
    )
    assert t("en", "missing_key") == "missing_key"
