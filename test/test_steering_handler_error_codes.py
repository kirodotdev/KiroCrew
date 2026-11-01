"""Every error body from the steering handlers carries a stable ``code``.

``error-code-baseline.json`` listed ``dashboard/handlers/steering.py`` with 20
``missing_code`` sites: error bodies that held English prose and nothing a
client could switch on. This suite pins the contract each branch keeps now:

* the status code is unchanged;
* the ``error`` string is unchanged, so a screen that shows ``res.error`` still
  shows the same words;
* the body gains a stable lower_snake ``code``.

``$HOME`` is pinned to ``tmp_path``; nothing touches the real ``~/.kiro``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.steering as steering_mod
from kiro_crew.dashboard.handlers.steering import (
    STEERING_FILE_MAX_BYTES,
    api_steering_create,
    api_steering_detail,
)

_OWNER = "U0OWNER0000"
_RESTRICTED_KEY = "dashboard:incognito"
_TOO_BIG = "x" * (STEERING_FILE_MAX_BYTES + 1)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".kiro" / "steering").mkdir(parents=True)
    (home / ".kiro" / "steering" / "a.md").write_text("# A\nbody\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@web.middleware
async def _owner_identity(request: web.Request, handler: Any) -> web.StreamResponse:
    request["user"] = _OWNER
    request["app"] = ""
    return await handler(request)


def _app() -> web.Application:
    slot = MagicMock(project="", is_restricted=False)
    state = MagicMock(_slots={"default": slot}, _restricted_keys={_RESTRICTED_KEY}, owner_id=_OWNER)
    app = web.Application(middlewares=[_owner_identity])
    app["state"] = state
    app.router.add_post("/api/steering", api_steering_create)
    app.router.add_get("/api/steering/{key:.+}", api_steering_detail)
    app.router.add_put("/api/steering/{key:.+}", api_steering_detail)
    app.router.add_delete("/api/steering/{key:.+}", api_steering_detail)
    return app


def _patch(name: str, result: Any) -> Any:
    """A setup step that makes one blocking transaction answer *result*."""

    def _apply(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(steering_mod, name, lambda *_a, **_kw: result)

    return _apply


_RESTRICTED = {"X-Session-Key": _RESTRICTED_KEY}

# (id, method, path, request kwargs, setup, status, error, code)
_CASES: list[tuple[str, str, str, dict[str, Any], Any, int, str, str]] = [
    (
        "create-restricted",
        "post",
        "/api/steering",
        {"json": {"name": "b.md", "content": "x"}, "headers": _RESTRICTED},
        None,
        403,
        "restricted session cannot modify steering files",
        "restricted_session",
    ),
    (
        "create-bad-json",
        "post",
        "/api/steering",
        {"data": "{not json"},
        None,
        400,
        "invalid JSON",
        "invalid_json",
    ),
    (
        "create-no-content",
        "post",
        "/api/steering",
        {"json": {"name": "b.md"}},
        None,
        400,
        "content is required",
        "content_required",
    ),
    (
        "create-too-large",
        "post",
        "/api/steering",
        {"json": {"name": "b.md", "content": _TOO_BIG}},
        None,
        413,
        f"content too large (cap {STEERING_FILE_MAX_BYTES} bytes)",
        "content_too_large",
    ),
    (
        "create-bad-source",
        "post",
        "/api/steering",
        {"json": {"name": "b.md", "content": "x", "source": "nope"}},
        None,
        400,
        "invalid source",
        "invalid_source",
    ),
    (
        "create-no-name",
        "post",
        "/api/steering",
        {"json": {"name": "", "content": "x", "source": "user"}},
        None,
        400,
        "name is required",
        "name_required",
    ),
    (
        "create-bad-path",
        "post",
        "/api/steering",
        {"json": {"name": "b.md", "content": "x", "source": "user"}},
        _patch("_resolve_blocking", None),
        400,
        "invalid steering path",
        "invalid_steering_path",
    ),
    (
        "create-exists",
        "post",
        "/api/steering",
        {"json": {"name": "a.md", "content": "x", "source": "user"}},
        None,
        409,
        "'a.md' already exists",
        "steering_exists",
    ),
    (
        "create-write-failed",
        "post",
        "/api/steering",
        {"json": {"name": "b.md", "content": "x", "source": "user"}},
        _patch("_create_file_blocking", ("writefailed", "")),
        500,
        "write failed",
        "write_failed",
    ),
    (
        "read-too-large",
        "get",
        "/api/steering/user/a.md",
        {},
        _patch("_resolve_and_read_blocking", ("", "", "toolarge:999")),
        413,
        f"file too large (999 bytes; cap {STEERING_FILE_MAX_BYTES})",
        "file_too_large",
    ),
    (
        "read-unknown",
        "get",
        "/api/steering/user/ghost.md",
        {},
        None,
        404,
        "not found",
        "steering_not_found",
    ),
    (
        "read-withheld",
        "get",
        "/api/steering/user/a.md",
        {},
        _patch("_resolve_and_read_blocking", ("", "", "readfailed")),
        404,
        "not found",
        "steering_not_found",
    ),
    (
        "delete-restricted",
        "delete",
        "/api/steering/user/a.md",
        {"headers": _RESTRICTED},
        None,
        403,
        "restricted session cannot modify steering files",
        "restricted_session",
    ),
    (
        "delete-unresolved",
        "delete",
        "/api/steering/user/a.md",
        {},
        _patch("_resolve_blocking", None),
        404,
        "not found",
        "steering_not_found",
    ),
    (
        "delete-unknown",
        "delete",
        "/api/steering/user/ghost.md",
        {},
        None,
        404,
        "not found",
        "steering_not_found",
    ),
    (
        "delete-failed",
        "delete",
        "/api/steering/user/a.md",
        {},
        _patch("_delete_file_blocking", "deletefailed"),
        500,
        "delete failed",
        "delete_failed",
    ),
    (
        "update-restricted",
        "put",
        "/api/steering/user/a.md",
        {"json": {"content": "y"}, "headers": _RESTRICTED},
        None,
        403,
        "restricted session cannot modify steering files",
        "restricted_session",
    ),
    (
        "update-bad-json",
        "put",
        "/api/steering/user/a.md",
        {"data": "{not json"},
        None,
        400,
        "invalid JSON",
        "invalid_json",
    ),
    (
        "update-no-content",
        "put",
        "/api/steering/user/a.md",
        {"json": {"content": " "}},
        None,
        400,
        "content is required",
        "content_required",
    ),
    (
        "update-too-large",
        "put",
        "/api/steering/user/a.md",
        {"json": {"content": _TOO_BIG}},
        None,
        413,
        f"content too large (cap {STEERING_FILE_MAX_BYTES} bytes)",
        "content_too_large",
    ),
    (
        "update-unresolved",
        "put",
        "/api/steering/user/a.md",
        {"json": {"content": "y"}},
        _patch("_resolve_blocking", None),
        404,
        "not found",
        "steering_not_found",
    ),
    (
        "update-unknown",
        "put",
        "/api/steering/user/ghost.md",
        {"json": {"content": "y"}},
        None,
        404,
        "not found",
        "steering_not_found",
    ),
    (
        "update-write-failed",
        "put",
        "/api/steering/user/a.md",
        {"json": {"content": "y"}},
        _patch("_update_file_blocking", "writefailed"),
        500,
        "write failed",
        "write_failed",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "kwargs", "setup", "status", "error", "code"),
    [case[1:] for case in _CASES],
    ids=[case[0] for case in _CASES],
)
async def test_error_reply_keeps_status_and_text_and_adds_code(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    kwargs: dict[str, Any],
    setup: Any,
    status: int,
    error: str,
    code: str,
) -> None:
    if setup is not None:
        setup(monkeypatch)
    async with TestClient(TestServer(_app())) as client:
        resp = await getattr(client, method)(path, **kwargs)
        body = await resp.json()
    assert resp.status == status
    assert body == {"error": error, "code": code}


@pytest.mark.asyncio
async def test_withheld_read_answers_the_unknown_key_body(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A withheld read and an unknown key share one body, code included."""
    async with TestClient(TestServer(_app())) as client:
        unknown = await (await client.get("/api/steering/user/ghost.md")).json()
        monkeypatch.setattr(
            steering_mod, "_resolve_and_read_blocking", lambda *_a: ("", "", "readfailed")
        )
        withheld = await (await client.get("/api/steering/user/a.md")).json()
    assert withheld == unknown
