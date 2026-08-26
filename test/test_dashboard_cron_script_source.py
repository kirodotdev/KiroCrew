"""Tests for GET /api/crons/{id}/script (api_cron_script_source).

The endpoint renders a script cron's source read-only in the dashboard. The
security contract under test: the file path is derived exclusively from the
job's own stored ``script`` field (the job id is the only client input), the
read is contained to ``<config_dir>/crons/`` through the nolink chokepoint, a
path that resolves outside that root is refused with a 4xx (never a 500), and
the response is size-capped rather than streaming an unbounded file.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.dashboard.handlers import cron as cron_handlers
from kiro_crew.dashboard.handlers.cron import (
    _SCRIPT_SOURCE_MAX_BYTES,
    api_cron_script_source,
)

# The endpoint degrades with 501 on Windows (the nolink chokepoint has no
# implementation there), so every test that exercises an actual file read is
# POSIX-only; the 404 guards and the gate test itself run everywhere.
posix_only = pytest.mark.skipif(
    os.name == "nt", reason="script source endpoint returns 501 on Windows"
)

SCRIPT_BODY = "def run(ctx):\n    ctx.notify('hello')\n"


def _make_job(job_id: str = "j1", script: str = "") -> CronJob:
    return CronJob(
        id=job_id,
        name="script job",
        message="",
        schedule=CronSchedule(kind="every", every_secs=300),
        created_ts=time.time(),
        script=script,
    )


def _make_app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/crons/{job_id}/script", api_cron_script_source)
    return app


def _make_state(job: CronJob | None):
    state = MagicMock()
    state.crons = MagicMock()
    # The handler must use the freshness-guaranteed async lookup so a job
    # minted by another process is visible immediately; leave the cache-only
    # list_jobs empty so a regression to it goes red.
    state.crons.list_jobs.return_value = []
    state.crons.get_job_async = AsyncMock(return_value=job)
    return state


@pytest.fixture
def crons_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point both config_dir seams (resolver + handler) at a temp home."""
    (tmp_path / "crons").mkdir()
    monkeypatch.setattr("kiro_crew.cron_script.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.cron.config_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def stub_sel():
    """Stub the SEL recorder so tests don't write real audit events.

    Autouse: the endpoint now emits an audit event on every allowed and
    refused read, so every test in this file crosses the SEL seam. Exposed
    so the audit-event tests can assert on the recorded calls.
    """
    with patch("kiro_crew.dashboard.handlers.cron._sel") as sel_fn:
        recorder = MagicMock()
        sel_fn.return_value = recorder
        yield recorder


class TestApiCronScriptSource:
    @posix_only
    @pytest.mark.asyncio
    async def test_happy_path(self, crons_home: Path) -> None:
        script = crons_home / "crons" / "monitor.py"
        script.write_text(SCRIPT_BODY)
        state = _make_state(_make_job(script=f"{script}:run"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 200
            data = await resp.json()
        assert data["source"] == SCRIPT_BODY
        assert data["file"] == "monitor.py"
        assert data["function"] == "run"
        assert data["truncated"] is False

    @pytest.mark.asyncio
    async def test_unknown_job_404(self, crons_home: Path) -> None:
        state = _make_state(None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/ghost/script")
            assert resp.status == 404
            assert (await resp.json())["code"] == "job_not_found"

    @pytest.mark.asyncio
    async def test_job_without_script_404(self, crons_home: Path) -> None:
        state = _make_state(_make_job(script=""))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 404
            assert (await resp.json())["code"] == "no_script"

    @posix_only
    @pytest.mark.asyncio
    async def test_missing_file_404(self, crons_home: Path) -> None:
        ghost = crons_home / "crons" / "ghost.py"
        state = _make_state(_make_job(script=f"{ghost}:run"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 404
            assert (await resp.json())["code"] == "script_not_found"

    @posix_only
    @pytest.mark.asyncio
    async def test_escape_outside_crons_root_refused(self, crons_home: Path) -> None:
        # A stored spec pointing outside <config_dir>/crons/ must be refused
        # with a 4xx, not read and not a 500.
        outside = crons_home / "outside.py"
        outside.write_text(SCRIPT_BODY)
        state = _make_state(_make_job(script=f"{outside}:run"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 422
            assert (await resp.json())["code"] == "script_path_refused"

    @posix_only
    @pytest.mark.asyncio
    async def test_symlink_escape_refused(self, crons_home: Path) -> None:
        # A symlink under crons/ whose target lives outside the root resolves
        # outside the allowed dir and must be refused.
        target = crons_home / "secret.py"
        target.write_text(SCRIPT_BODY)
        link = crons_home / "crons" / "link.py"
        link.symlink_to(target)
        state = _make_state(_make_job(script=f"{link}:run"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 422
            assert (await resp.json())["code"] == "script_path_refused"

    @posix_only
    @pytest.mark.asyncio
    async def test_symlink_loop_refused_not_500(self, crons_home: Path) -> None:
        # A self-referential symlink makes path resolution raise (RuntimeError
        # or OSError/ELOOP depending on the Python version). That is a refusal,
        # never a 500.
        loop = crons_home / "crons" / "loop.py"
        loop.symlink_to(loop)
        state = _make_state(_make_job(script=f"{loop}:run"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status in (404, 422)
            assert (await resp.json())["code"] in ("script_not_found", "script_path_refused")

    @posix_only
    @pytest.mark.asyncio
    async def test_non_string_script_refused_not_500(self, crons_home: Path) -> None:
        # crons.json is agent- and hand-editable JSON, so a persisted ``script``
        # can be any JSON type. A truthy non-string value passes the handler's
        # ``if not job.script`` gate and must be refused by the reader, never
        # crash into a 500 (the resolver would raise AttributeError on it).
        job = _make_job()
        job.script = 12345  # type: ignore[assignment]
        state = _make_state(job)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 422
            assert (await resp.json())["code"] == "script_path_refused"

    @posix_only
    @pytest.mark.asyncio
    async def test_malformed_spec_refused(self, crons_home: Path) -> None:
        state = _make_state(_make_job(script="no-function-part"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 422
            assert (await resp.json())["code"] == "script_path_refused"

    @posix_only
    @pytest.mark.asyncio
    async def test_oversize_source_truncated(self, crons_home: Path) -> None:
        script = crons_home / "crons" / "big.py"
        body = "# " + "x" * _SCRIPT_SOURCE_MAX_BYTES + "\n"
        script.write_text(body)
        state = _make_state(_make_job(script=f"{script}:run"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 200
            data = await resp.json()
        assert data["truncated"] is True
        assert len(data["source"].encode()) <= _SCRIPT_SOURCE_MAX_BYTES

    @posix_only
    @pytest.mark.asyncio
    async def test_credentials_redacted(self, crons_home: Path) -> None:
        # Scripts are LLM-writeable, so their content is agent-influenced text:
        # raw credential patterns must not reach the dashboard verbatim.
        script = crons_home / "crons" / "leaky.py"
        script.write_text('KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        state = _make_state(_make_job(script=f"{script}:run"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 200
            data = await resp.json()
        assert "AKIAIOSFODNN7EXAMPLE" not in data["source"]

    @posix_only
    @pytest.mark.asyncio
    async def test_metadata_fields_redacted(self, crons_home: Path) -> None:
        # The file and function names come from the same stored spec as the
        # content, so a credential-shaped name must not ride out unredacted on
        # the metadata fields either. A credential pattern is a legal Python
        # identifier and a legal file name, so both fields are reachable.
        script = crons_home / "crons" / "AKIAIOSFODNN7EXAMPLE.py"
        script.write_text(SCRIPT_BODY)
        state = _make_state(_make_job(script=f"{script}:AKIAIOSFODNN7EXAMPLE"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 200
            data = await resp.json()
        assert "AKIAIOSFODNN7EXAMPLE" not in data["file"]
        assert "AKIAIOSFODNN7EXAMPLE" not in data["function"]

    @pytest.mark.asyncio
    async def test_windows_gate_501(
        self, crons_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = crons_home / "crons" / "monitor.py"
        script.write_text(SCRIPT_BODY)
        monkeypatch.setattr(cron_handlers, "_SCRIPT_SOURCE_WIN_UNSUPPORTED", True)
        state = _make_state(_make_job(script=f"{script}:run"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 501
            assert (await resp.json())["code"] == "unsupported_platform"

    @posix_only
    @pytest.mark.asyncio
    async def test_allowed_read_emits_sel_audit(
        self, crons_home: Path, stub_sel: MagicMock
    ) -> None:
        # An allowed read of an on-disk script must leave an SEL record so the
        # guarded-path decision is auditable, not silent.
        script = crons_home / "crons" / "monitor.py"
        script.write_text(SCRIPT_BODY)
        state = _make_state(_make_job(script=f"{script}:run"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 200
        stub_sel.log_api_access.assert_called_once()
        kw = stub_sel.log_api_access.call_args.kwargs
        assert kw["operation"] == "cron.script_source"
        assert kw["outcome"] == "ok"
        assert "job_id=j1" in kw["resources"]

    @posix_only
    @pytest.mark.asyncio
    async def test_refused_read_emits_sel_audit(
        self, crons_home: Path, stub_sel: MagicMock
    ) -> None:
        # A refusal (containment escape) is a permission decision and must be
        # audited with the refusal code, same as an allowed read.
        outside = crons_home / "outside.py"
        outside.write_text(SCRIPT_BODY)
        state = _make_state(_make_job(script=f"{outside}:run"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 422
        stub_sel.log_api_access.assert_called_once()
        kw = stub_sel.log_api_access.call_args.kwargs
        assert kw["operation"] == "cron.script_source"
        assert kw["outcome"] == "denied"
        assert "code=script_path_refused" in kw["resources"]
