"""A file read refused for an untrusted UNC path answers its own ``unc_path_refused`` code."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew import platform_compat
from kiro_crew.dashboard import handlers
from kiro_crew.dashboard.handlers import files

_WSL_PATH = "//wsl.localhost/Ubuntu/home/u/repo/cli/build.mjs"


@pytest.mark.parametrize(
    "raw", [_WSL_PATH, "\\\\wsl.localhost\\Ubuntu\\home\\u\\x.mjs", "\\\\wsl$\\Ubuntu\\x"]
)
def test_an_untrusted_share_is_named_as_refused_on_windows(monkeypatch, raw):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert files._unc_path_refused(raw)


def test_a_local_path_or_a_posix_host_is_not_named_as_a_unc_refusal(monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert not files._unc_path_refused("/home/u/repo/x.mjs")
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    # On POSIX a doubled leading slash is an ordinary local path.
    assert not files._unc_path_refused(_WSL_PATH)


_ROUTES = (
    ("file-read", handlers.api_file_read),
    ("file-raw", handlers.api_file_raw),
    ("file-download", handlers.api_file_download),
    ("file-stream", handlers.api_file_stream),
    ("file-sheet", handlers.api_file_sheet),
    ("file-office-preview", handlers.api_file_office_preview),
    ("file-office-slides", handlers.api_file_office_slides),
)
#: The routes that answer any other invalid path with ``forbidden_path``, not ``invalid_path``.
_OFFICE_ROUTES = ("file-office-preview", "file-office-slides")


def _make_app() -> web.Application:
    app = web.Application()
    for route, handler in _ROUTES:
        app.router.add_get(f"/api/{route}", handler)
    return as_owner(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [True, False])
async def test_every_file_viewer_read_codes_a_unc_refusal(refused):
    with (
        patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=None),
        patch("kiro_crew.dashboard.handlers.files._unc_path_refused", return_value=refused),
        patch("kiro_crew.sel.sel") as sel,
    ):
        sel.return_value = MagicMock()
        async with TestClient(TestServer(_make_app())) as client:
            for route, _handler in _ROUTES:
                path = _WSL_PATH + (".pptx" if route in _OFFICE_ROUTES else "")
                resp = await client.get(f"/api/{route}", params={"path": path})
                assert resp.status == 400, route
                body = await resp.json()
                if refused:
                    assert body["code"] == "unc_path_refused", route
                    assert body["error"] == files._UNC_PATH_REFUSED_ERROR, route
                else:
                    other = "forbidden_path" if route in _OFFICE_ROUTES else "invalid_path"
                    assert body.get("code", "invalid_path") == other, route
                    assert body["error"] == "invalid or forbidden path", route
