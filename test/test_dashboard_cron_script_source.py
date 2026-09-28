"""Tests for GET /api/crons/{id}/script (api_cron_script_source).

The endpoint renders a script cron's source read-only in the dashboard. The
security contract under test: the file path is derived exclusively from the
job's own stored ``script`` field (the job id is the only client input), the
read is contained to ``<config_dir>/crons/`` through the nolink chokepoint, a
path that resolves outside that root is refused with a 4xx (never a 500), and
the response is size-capped rather than streaming an unbounded file.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.dashboard.handlers.cron import (
    _SCRIPT_SOURCE_MAX_BYTES,
    api_cron_script_source,
)

# The chokepoint reads on both platforms, so the file-reading tests run
# everywhere. Only the two tests that CREATE a symlink are POSIX-only: on
# Windows that needs SeCreateSymbolicLinkPrivilege, which an unelevated test
# runner does not hold (WinError 1314).
needs_symlinks = pytest.mark.skipif(
    os.name == "nt", reason="creating a symlink on Windows requires a privilege tests lack"
)

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-specific read path")

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
    @pytest.mark.asyncio
    async def test_happy_path(self, crons_home: Path) -> None:
        script = crons_home / "crons" / "monitor.py"
        # Explicit newline and encoding: the assertions below compare the served
        # body and its digest against SCRIPT_BODY byte-for-byte, and a text-mode
        # write would store CRLF on Windows and make the digest a different file's.
        script.write_text(SCRIPT_BODY, encoding="utf-8", newline="\n")
        state = _make_state(_make_job(script=f"{script}:run"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 200
            data = await resp.json()
        assert data["source"] == SCRIPT_BODY
        assert data["file"] == "monitor.py"
        assert data["function"] == "run"
        assert data["truncated"] is False
        # The digest the approval flow echoes back: over the raw bytes read,
        # so an approver's view and the promotion's pin snapshot compare equal.
        assert data["sha256"] == hashlib.sha256(SCRIPT_BODY.encode()).hexdigest()
        # Verbatim display: what the operator reads IS the code, so approvable.
        assert data["reviewable"] is True

    @pytest.mark.asyncio
    async def test_undecodable_bytes_are_not_reviewable(self, crons_home: Path) -> None:
        # Invalid UTF-8 is rendered with replacement characters, so the display
        # cannot equal the raw body: unreviewable, even though it is served.
        script = crons_home / "crons" / "binary.py"
        script.write_bytes(b'def run(ctx): return b"\xff\xfe"\n')
        state = _make_state(_make_job(script=f"{script}:run"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 200
            data = await resp.json()
        assert data["reviewable"] is False

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

    @pytest.mark.asyncio
    async def test_missing_file_404(self, crons_home: Path) -> None:
        ghost = crons_home / "crons" / "ghost.py"
        state = _make_state(_make_job(script=f"{ghost}:run"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 404
            assert (await resp.json())["code"] == "script_not_found"

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

    @needs_symlinks
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

    @needs_symlinks
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

    @pytest.mark.asyncio
    async def test_malformed_spec_refused(self, crons_home: Path) -> None:
        state = _make_state(_make_job(script="no-function-part"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 422
            assert (await resp.json())["code"] == "script_path_refused"

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
        # The display does not equal the raw body, so the approval flow must
        # treat this script as unreviewable (the operator cannot read the span).
        assert data["reviewable"] is False

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

    @windows_only
    @pytest.mark.asyncio
    async def test_windows_serves_source(self, crons_home: Path) -> None:
        # The read needs "open this file and prove what the descriptor points
        # at", not a dir_fd/openat walk: pinned_fs.fd_real_path answers on
        # Windows via GetFinalPathNameByHandleW, so the route serves the source
        # there rather than refusing. CRLF and a 0x1A byte are in the body on
        # purpose -- a descriptor left in the CRT's text mode would translate
        # the line endings and stop at Ctrl-Z, so the digest the approval flow
        # echoes back would not be the digest of the bytes on disk.
        raw = b"def run(ctx):\r\n    ctx.notify('hi')\r\n# tail \x1a after\n"
        script = crons_home / "crons" / "monitor.py"
        script.write_bytes(raw)
        state = _make_state(_make_job(script=f"{script}:run"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 200
            data = await resp.json()
        assert data["source"] == raw.decode("utf-8")
        assert data["sha256"] == hashlib.sha256(raw).hexdigest()
        assert data["truncated"] is False
        assert data["reviewable"] is True

    @windows_only
    @pytest.mark.asyncio
    async def test_windows_hardlink_alias_refused(self, crons_home: Path) -> None:
        # Windows has no O_NOFOLLOW, so the inode-pinned guards are what keep
        # the read honest there. os.fstat reports st_nlink on Windows, so a
        # second name for the same inode is refused on the open descriptor --
        # a 4xx with a code, never a served body and never a 500.
        script = crons_home / "crons" / "monitor.py"
        script.write_text(SCRIPT_BODY, encoding="utf-8")
        alias = crons_home / "crons" / "alias.py"
        try:
            os.link(script, alias)
        except OSError as exc:
            pytest.skip(f"filesystem does not support hard links: {exc}")
        state = _make_state(_make_job(script=f"{alias}:run"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/crons/j1/script")
            assert resp.status == 422
            assert (await resp.json())["code"] == "script_read_refused"

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
