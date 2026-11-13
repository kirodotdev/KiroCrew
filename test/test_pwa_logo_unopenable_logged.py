"""``pwa_file`` and ``logo`` name a file that exists but cannot be opened.

Both routes resolve a path and check it is a file before answering. When the
open itself then fails (``EIO``, ``ENOSYS``, or a permission error), the answer
is a 404 (403 for a permission error) that looks exactly like a missing file.
These tests drive the real handlers through a real ``TestClient`` and check the
answer keeps its status while one rate-limited warning names the path and the
OS error, and that a file that is simply absent stays a quiet 404.
"""

from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import dist_file_response
from kiro_crew.dashboard.handlers import core as core_mod

_ERRORS = [(errno.EIO, 404), (errno.ENOSYS, 404), (errno.EACCES, 403)]


def _fail_open_for(monkeypatch: pytest.MonkeyPatch, target: Path, err: int) -> None:
    """Make the response's open of ``target`` fail with ``err`` while ``stat`` works."""
    real_open = open
    resolved = target.resolve()

    def _open(file: object, *args: object, **kwargs: object) -> object:
        if Path(str(file)).resolve() == resolved:
            if err in (errno.EACCES, errno.EPERM):
                raise PermissionError(err, os.strerror(err), str(file))
            raise OSError(err, os.strerror(err), str(file))
        return real_open(file, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(dist_file_response, "open", _open, raising=False)


def _unopenable_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == dist_file_response.logger.name
        and r.levelno == logging.WARNING
        and "cannot be read" in r.getMessage()
    ]


@pytest.fixture
def _fresh_open_log(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(dist_file_response, "_UNOPENABLE_LAST_LOGGED", {})
    caplog.set_level(logging.WARNING, logger=dist_file_response.logger.name)


def _app() -> web.Application:
    app = web.Application()
    app.router.add_get("/logo.png", core_mod.logo)
    app.router.add_get("/{name}", core_mod.pwa_file)
    return app


def _avatar_config(monkeypatch: pytest.MonkeyPatch, avatar: str) -> None:
    cfg = SimpleNamespace(dashboard=SimpleNamespace(avatar=avatar))
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.KiroCrewConfig", SimpleNamespace(load=lambda: cfg)
    )
    monkeypatch.setattr("kiro_crew.dashboard.handlers.is_sensitive_path", lambda _p: False)
    monkeypatch.setattr("kiro_crew.hooks.validate_file_path", lambda p: p or None)


def _assert_one_warning(caplog: pytest.LogCaptureFixture, target: Path, err: int) -> None:
    warnings = _unopenable_warnings(caplog)
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    message = warnings[0].getMessage()
    assert str(target.resolve().name) in message
    assert f"[Errno {err}]" in message


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fresh_open_log")
@pytest.mark.parametrize(("err", "status"), _ERRORS)
async def test_an_unopenable_pwa_file_logs_once_and_keeps_its_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    err: int,
    status: int,
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    target = dist / "sw.js"
    target.write_text("self.addEventListener('fetch', () => {})\n", encoding="utf-8")
    monkeypatch.setattr(core_mod, "_DIST_DIR", dist)
    _fail_open_for(monkeypatch, target, err)
    async with TestClient(TestServer(_app())) as client:
        first = await client.get("/sw.js")
        second = await client.get("/sw.js")
        assert first.status == second.status == status
    _assert_one_warning(caplog, target, err)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fresh_open_log")
@pytest.mark.parametrize(("err", "status"), _ERRORS)
async def test_an_unopenable_custom_avatar_logs_once_and_keeps_its_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    err: int,
    status: int,
) -> None:
    avatar = tmp_path / "avatar.png"
    avatar.write_bytes(b"png")
    _avatar_config(monkeypatch, str(avatar))
    _fail_open_for(monkeypatch, avatar, err)
    async with TestClient(TestServer(_app())) as client:
        first = await client.get("/logo.png")
        second = await client.get("/logo.png")
        assert first.status == second.status == status
    _assert_one_warning(caplog, avatar, err)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fresh_open_log")
@pytest.mark.parametrize(("err", "status"), _ERRORS)
async def test_an_unopenable_default_logo_logs_once_and_keeps_its_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    err: int,
    status: int,
) -> None:
    logo = tmp_path / "kirocrew-logo.png"
    logo.write_bytes(b"png")
    _avatar_config(monkeypatch, "")
    monkeypatch.setattr("kiro_crew.dashboard.handlers._STATIC_DIR", tmp_path)
    monkeypatch.setattr("kiro_crew.__version__", "9.9.9")
    _fail_open_for(monkeypatch, logo, err)
    async with TestClient(TestServer(_app())) as client:
        first = await client.get("/logo.png")
        second = await client.get("/logo.png")
        assert first.status == second.status == status
    _assert_one_warning(caplog, logo, err)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fresh_open_log")
async def test_served_and_missing_pwa_files_and_logos_log_no_open_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "manifest.webmanifest").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(core_mod, "_DIST_DIR", dist)
    static = tmp_path / "static"
    static.mkdir()
    (static / "kirocrew-logo.png").write_bytes(b"png")
    monkeypatch.setattr("kiro_crew.dashboard.handlers._STATIC_DIR", static)
    monkeypatch.setattr("kiro_crew.__version__", "9.9.9")
    _avatar_config(monkeypatch, "")
    async with TestClient(TestServer(_app())) as client:
        manifest = await client.get("/manifest.webmanifest")
        assert manifest.status == 200
        assert await manifest.text() == "{}"
        assert (await client.get("/absent.js")).status == 404
        served_logo = await client.get("/logo.png")
        assert served_logo.status == 200
        assert await served_logo.read() == b"png"
    monkeypatch.setattr("kiro_crew.dashboard.handlers._STATIC_DIR", tmp_path / "empty")
    async with TestClient(TestServer(_app())) as client:
        assert (await client.get("/logo.png")).status == 404
    assert _unopenable_warnings(caplog) == []
