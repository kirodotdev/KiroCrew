"""``dashboard.upload_max_mb``: one configurable per-file upload ceiling.

The chat composer's ``POST /api/upload/file`` and the Knowledge page's
``POST /api/knowledge/ingest`` both read the ceiling per request and refuse an
over-cap file with a 413 that names the limit; the Knowledge route is also
bounded by ``knowledge.max_ingest_file_mb``. ``GET /api/dashboard/config``
serves both figures read-only so each page pre-checks and advertises the one its
route enforces.
"""

from __future__ import annotations

import contextlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import UPLOAD_MAX_MB_MAX, UPLOAD_MAX_MB_MIN, DashboardConfig
from kiro_crew.dashboard import upload_limits

MB = 1024 * 1024


def _zip_bytes(pad: int = 0) -> bytes:
    """A valid ZIP, optionally padded with a stored member of *pad* bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("readme.txt", "hello")
        if pad:
            zf.writestr("pad.bin", b"\x00" * pad)
    return buf.getvalue()


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


def _set_limit(cfg_file: Path, value: object) -> None:
    cfg_file.write_text(json.dumps({"dashboard": {"upload_max_mb": value}}), encoding="utf-8")


# ---------- config ----------


def test_default_is_100_mb() -> None:
    assert DashboardConfig().upload_max_mb == 100
    assert upload_limits.DEFAULT_UPLOAD_MAX_BYTES == 100 * MB


@pytest.mark.parametrize(
    ("stored", "loaded"),
    [
        (250, 250),
        ("300", 300),
        (0, UPLOAD_MAX_MB_MIN),
        (-5, UPLOAD_MAX_MB_MIN),
        (10**9, UPLOAD_MAX_MB_MAX),
        ("lots", 100),
        (True, 100),
    ],
)
def test_loader_bounds_the_stored_value(cfg_file, stored, loaded) -> None:
    _set_limit(cfg_file, stored)
    assert KiroCrewConfig.load().dashboard.upload_max_mb == loaded


def test_upload_max_bytes_reads_the_config(cfg_file) -> None:
    _set_limit(cfg_file, 7)
    assert upload_limits.upload_max_bytes() == 7 * MB


def test_upload_max_bytes_falls_back_when_config_is_unreadable() -> None:
    with patch.object(KiroCrewConfig, "load", side_effect=OSError("boom")):
        assert upload_limits.upload_max_bytes() == upload_limits.DEFAULT_UPLOAD_MAX_BYTES


# ---------- chat composer upload ----------


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "uploads"
    monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", target)
    return target


@pytest.fixture
def files_sel():
    with patch("kiro_crew.dashboard.handlers.files._sel") as sel:
        sel.return_value = MagicMock()
        yield sel.return_value


async def _post_upload(payload: bytes, filename: str) -> tuple[int, dict]:
    from kiro_crew.dashboard.handlers.files import api_upload_file

    app = web.Application()
    app["state"] = MagicMock()
    app.router.add_post("/api/upload/file", api_upload_file)
    form = aiohttp.FormData()
    form.add_field("file", payload, filename=filename, content_type="application/zip")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/upload/file", data=form)
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_zip_over_the_configured_cap_is_413_naming_the_limit(
    cfg_file, upload_dir, files_sel
) -> None:
    _set_limit(cfg_file, 1)
    status, body = await _post_upload(_zip_bytes(pad=MB + 1024), "project.zip")
    assert status == 413, body
    assert body["code"] == "file_too_large"
    assert "max_mb" not in body
    assert "(max 1 MB)" in body["error"]
    assert "Type @ and the file's path to share it without uploading." in body["error"]
    assert "project.zip" in body["error"]
    assert list(upload_dir.glob("*")) == []


@pytest.mark.asyncio
async def test_zip_over_the_old_50_mb_line_is_accepted_under_the_new_default(
    cfg_file, upload_dir, files_sel, monkeypatch
) -> None:
    """The reported case: a file past the old fixed cap now lands. Scaled down --
    the old fixed cap is shrunk to 1 MB and the configured default to 2 MB -- so
    the test proves WHICH ceiling the handler consults without a 50 MB body."""
    monkeypatch.setattr("kiro_crew.dashboard.handlers.files._MAX_UPLOAD_BYTES", MB)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.files.upload_max_bytes", lambda: 2 * MB)
    payload = _zip_bytes(pad=MB + 1024)
    status, body = await _post_upload(payload, "project.zip")
    assert status == 200, body
    assert Path(body["paths"][0]).read_bytes() == payload


@pytest.mark.asyncio
async def test_upload_diagnostic_hashes_off_the_event_loop(
    cfg_file, upload_dir, files_sel, monkeypatch
) -> None:
    """A ZIP near the raised ceiling must not be hashed and read back on the
    gateway's event loop: every sha256 the handler takes runs on a worker
    thread, and the diagnostic still compares the received and stored bytes."""
    import hashlib
    import sys
    import threading

    from kiro_crew.dashboard.file_api import uploads as uploads_owner
    from kiro_crew.dashboard.handlers import files as files_mod

    real_sha256 = hashlib.sha256
    loop_thread = threading.get_ident()
    calls: list[bool] = []

    def recording_sha256(*args, **kwargs):
        # Only the upload handler's own hashing is judged, not a library's.
        if sys._getframe(1).f_code.co_filename == uploads_owner.__file__:
            calls.append(threading.get_ident() == loop_thread)
        return real_sha256(*args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", recording_sha256)
    payload = _zip_bytes(pad=4096)
    with patch.object(files_mod.logger, "info") as info:
        status, body = await _post_upload(payload, "project.zip")
    assert status == 200, body
    assert calls, "the diagnostic took no hash"
    assert not any(calls), "a sha256 ran on the event loop thread"
    diag = [c for c in info.call_args_list if "upload.file diagnostic" in str(c.args[0])]
    assert len(diag) == 1
    args = diag[0].args
    assert args[3] == args[4] == len(payload)  # sent_size, disk_size
    assert args[5] == args[6] == real_sha256(payload).hexdigest()
    assert args[7] is True


@pytest.mark.asyncio
async def test_an_edit_takes_effect_on_the_next_upload(cfg_file, upload_dir, files_sel) -> None:
    payload = _zip_bytes(pad=MB + 1024)
    _set_limit(cfg_file, 1)
    status, _ = await _post_upload(payload, "a.zip")
    assert status == 413
    _set_limit(cfg_file, 2)
    status, body = await _post_upload(payload, "a.zip")
    assert status == 200, body


# ---------- dashboard config ----------


@pytest.fixture()
def config_app(cfg_file):
    from kiro_crew.dashboard.handlers.files import api_dashboard_config

    m = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sel", return_value=m):
        app = web.Application()
        app.router.add_get("/api/dashboard/config", api_dashboard_config)
        app.router.add_put("/api/dashboard/config", api_dashboard_config)
        yield as_owner(app)


@pytest.mark.asyncio
async def test_dashboard_config_serves_the_limit_read_only(config_app, cfg_file) -> None:
    _set_limit(cfg_file, 250)
    async with TestClient(TestServer(config_app)) as client:
        resp = await client.get("/api/dashboard/config")
        body = await resp.json()
        assert body["upload_max_mb"] == 250
        # knowledge.max_ingest_file_mb defaults to 100, below the 250 cap.
        assert body["knowledge_upload_max_mb"] == 100
        # The settings UI spreads the whole GET body into its PUT: the
        # round-tripped read-only field must not 400 an unrelated save, and a
        # caller-supplied value must not be adopted.
        body["quick_send"] = True
        body["upload_max_mb"] = 1024
        body["knowledge_upload_max_mb"] = 1024
        resp = await client.put("/api/dashboard/config", json=body)
        assert resp.status == 200, await resp.text()
    assert KiroCrewConfig.load().dashboard.upload_max_mb == 250


# ---------- knowledge ingest ----------


class _FakeStore:
    def __init__(self) -> None:
        self.db = MagicMock()
        self._rows: dict[str, dict] = {}

    def get_source_by_uri(self, uri):
        return self._rows.get(uri)

    def add_source(self, *, name, source_type, uri, properties):
        self._rows[uri] = {"id": "sid1"}
        return "sid1"


async def _post_ingest(payload: bytes, filename: str) -> tuple[int, dict]:
    from kiro_crew.dashboard.handlers.knowledge import ingest_file

    app = web.Application()
    app["state"] = SimpleNamespace(knowledge_store=_FakeStore())
    app["knowledge_pipeline"] = SimpleNamespace(
        ingest_file=AsyncMock(),
        reserve_import_budget=AsyncMock(return_value=None),
        release_import_budget=MagicMock(),
        ingestion_in_flight=contextlib.nullcontext,
    )
    app.router.add_post("/api/knowledge/ingest", ingest_file)
    form = aiohttp.FormData()
    form.add_field("file", payload, filename=filename, content_type="text/plain")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/knowledge/ingest", data=form)
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_knowledge_ingest_enforces_and_names_the_configured_limit(cfg_file) -> None:
    _set_limit(cfg_file, 1)
    with patch("kiro_crew.dashboard.handlers.knowledge.sel") as sel:
        sel.return_value = MagicMock()
        status, body = await _post_ingest(b"a" * (MB + 1), "notes.txt")
    assert status == 413, body
    assert body["error"] == "file too large (max 1 MB)"


@pytest.mark.parametrize(
    ("upload_mb", "ingest_mb", "expected"),
    [
        (200, 100.0, 100 * MB),  # the ingestion cap is the smaller
        (50, 100.0, 50 * MB),  # the upload cap is the smaller
        (200, 0.0, 200 * MB),  # 0 disables the ingestion cap
        (100, 0.5, MB // 2),  # a fractional ingestion cap is honoured
    ],
)
def test_knowledge_ceiling_is_the_smaller_cap(upload_mb, ingest_mb, expected) -> None:
    assert upload_limits.knowledge_ceiling_bytes(upload_mb, ingest_mb) == expected


def test_bytes_to_mb_figure() -> None:
    assert upload_limits.bytes_to_mb_figure(100 * MB) == 100
    assert upload_limits.bytes_to_mb_figure(MB // 2) == 0.5
    assert upload_limits.bytes_to_mb_figure(1) == 0.1


def test_knowledge_upload_max_bytes_falls_back_when_config_is_unreadable() -> None:
    with patch("kiro_crew.config.loader.KiroCrewConfig.load", side_effect=RuntimeError("boom")):
        assert upload_limits.knowledge_upload_max_bytes() == upload_limits.DEFAULT_UPLOAD_MAX_BYTES


@pytest.mark.asyncio
async def test_knowledge_ingest_is_bounded_by_max_ingest_file_mb(cfg_file) -> None:
    # A raised upload cap must not let the Knowledge upload accept a file that
    # ingestion then refuses: the route enforces the smaller ingestion cap.
    cfg_file.write_text(
        json.dumps({"dashboard": {"upload_max_mb": 200}, "knowledge": {"max_ingest_file_mb": 1}}),
        encoding="utf-8",
    )
    with patch("kiro_crew.dashboard.handlers.knowledge.sel") as sel:
        sel.return_value = MagicMock()
        status, body = await _post_ingest(b"a" * (MB + 1), "notes.txt")
    assert status == 413, body
    assert body["error"] == "file too large (max 1 MB)"
