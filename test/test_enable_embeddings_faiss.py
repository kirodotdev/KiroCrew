"""Tests for faiss-cpu installation block in enable-embeddings handler.

The enable flow no longer boots Ollama: when the GGUF is absent it kicks (or
adopts) a background ``ensure_model`` download task and returns 200
"downloading" immediately; when the model file is present it pip-installs
faiss-cpu (flow unchanged), wires ``make_sync_embed_fn()`` onto the vector
store, and loads the FAISS index. Model download and embedder are fully
faked here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.memory as mem_mod
from kiro_crew.embeddings import DOWNLOAD_ATTEMPTS_INTERACTIVE

_MOD = "kiro_crew.dashboard.handlers.memory"
_EMB = "kiro_crew.embeddings"


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/api/memory/enable-embeddings", mem_mod.api_memory_enable_embeddings)
    app.router.add_get("/api/memory/embedding-status", mem_mod.api_memory_embedding_status)
    app["state"] = MagicMock(consolidator=None)
    return app


def _mock_mgr(ensure_ok: bool = True):
    mgr = MagicMock()
    mgr.ensure_model = AsyncMock(return_value=ensure_ok)
    mgr.status = {"step": "ready" if ensure_ok else "failed", "error": "", "attempt": 1}
    return mgr


def _mock_proc(rc: int = 0, stderr: bytes = b""):
    proc = MagicMock()
    proc.returncode = rc
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    return proc


@pytest.fixture(autouse=True)
def _reset_status():
    mem_mod._embedding_setup_status = {"step": "idle", "error": ""}
    yield
    mem_mod._embedding_setup_status = {"step": "idle", "error": ""}


def _common_patches(cfg_path, faiss_available=False, proc_rc=0, proc_stderr=b"",
                    model_present=True, ensure_ok=True):
    """Return a dict of context managers for the common mocks."""
    store = MagicMock()
    store.embed_fn = None
    store.load_faiss_index = MagicMock()

    proc = _mock_proc(proc_rc, proc_stderr)
    faiss_mod = MagicMock() if faiss_available else None
    mgr = _mock_mgr(ensure_ok)

    patches = {
        "mgr": patch(f"{_MOD}.model_download_manager", return_value=mgr),
        "model_present": patch(f"{_MOD}.model_file_present", return_value=model_present),
        "cfg_load": patch(f"{_MOD}.KiroCrewConfig.load", return_value=MagicMock()),
        "cfg_path": patch(f"{_MOD}.config_path", return_value=cfg_path),
        "subprocess": patch("asyncio.create_subprocess_exec", return_value=proc),
        "embed_fn": patch(f"{_MOD}.make_sync_embed_fn", return_value=lambda t: [0.0]),
        # Inject a fake ``pip`` so ``_ensure_pip_available`` short-circuits and
        # these faiss-focused tests see exactly one subprocess (the faiss install).
        "faiss": patch.dict("sys.modules", {"faiss": faiss_mod, "pip": MagicMock()}),
        "store": patch(f"{_MOD}._get_vector_store", return_value=store),
        "wrap_argv": patch(f"{_MOD}.wrap_argv", side_effect=lambda argv, **kw: (argv, None)),
    }
    return patches, store, proc, mgr


class TestFaissInstallSuccess:
    @pytest.mark.asyncio
    async def test_pip_install_runs_when_faiss_missing(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc, mgr = _common_patches(cfg_path, faiss_available=False, proc_rc=0)

        with patches["mgr"], patches["model_present"], patches["cfg_load"], \
             patches["cfg_path"], patches["subprocess"] as mock_exec, \
             patches["embed_fn"], patches["faiss"], patches["store"], \
             patches["wrap_argv"]:
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 200
                assert (await resp.json()).get("ok") is True

            mock_exec.assert_called_once()
            args = mock_exec.call_args[0]
            assert "faiss-cpu" in args
            assert "--only-binary=:all:" in args
            # Model already present — no download attempted.
            mgr.ensure_model.assert_not_awaited()


class TestFaissInstallFailure:
    @pytest.mark.asyncio
    async def test_returns_500_and_resets_status(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc, mgr = _common_patches(
            cfg_path, faiss_available=False, proc_rc=1, proc_stderr=b"No matching distribution"
        )

        with patches["mgr"], patches["model_present"], patches["cfg_load"], \
             patches["cfg_path"], patches["subprocess"], patches["faiss"], \
             patches["store"], patches["wrap_argv"]:
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 500
                body = await resp.json()
                assert "faiss-cpu installation failed" in body["error"]

        assert mem_mod._embedding_setup_status["step"] == "idle"
        assert "faiss-cpu" in str(mem_mod._embedding_setup_status["error"])


class TestFaissAlreadyInstalled:
    @pytest.mark.asyncio
    async def test_skips_pip_when_faiss_importable(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc, mgr = _common_patches(cfg_path, faiss_available=True)

        with patches["mgr"], patches["model_present"], patches["cfg_load"], \
             patches["cfg_path"], patches["subprocess"] as mock_exec, \
             patches["embed_fn"], patches["faiss"], patches["store"], \
             patches["wrap_argv"]:
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 200

            mock_exec.assert_not_called()


class TestFaissSandboxUnavailableDoesNotWedge:
    """On a host with no sandbox backend (every Windows host), the on-demand
    faiss install can't run. That must NOT latch the non-terminal
    'installing_faiss' status — otherwise the 2s poll shows it forever and every
    later Enable click 409s until the gateway restarts. It skips the install and
    continues (episodic recall uses the stdlib cosine fallback)."""

    @pytest.mark.asyncio
    async def test_returns_200_and_status_not_wedged(self, tmp_path: Path) -> None:
        from kiro_crew.sandbox import SandboxUnavailableError

        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc, mgr = _common_patches(cfg_path, faiss_available=False)

        def _refuse(argv, **kw):
            raise SandboxUnavailableError(
                "no backend; set agent.sandbox_allow_unsandboxed_exec=true",
                kind="no_backend",
                detail="not Linux",
            )

        with patches["mgr"], patches["model_present"], patches["cfg_load"], \
             patches["cfg_path"], patches["subprocess"] as mock_exec, \
             patches["embed_fn"], patches["faiss"], patches["store"], \
             patch(f"{_MOD}.wrap_argv", side_effect=_refuse):
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 200
                assert (await resp.json()).get("ok") is True

            # No install subprocess was ever spawned.
            mock_exec.assert_not_called()

        # Setup completed, so the status is the terminal "done" — crucially NOT
        # the non-terminal "installing_faiss" that latched the 409 guard forever.
        assert mem_mod._embedding_setup_status["step"] == "done"
        assert mem_mod._embedding_setup_status["step"] != "installing_faiss"
        assert not mem_mod._embedding_setup_status["error"]


class TestModelDownloadFlow:
    """Model absent → the endpoint kicks a background ensure_model task and
    returns 200 "downloading" immediately (never awaits the download; failures
    surface via the embedding-status endpoint)."""

    @pytest.mark.asyncio
    async def test_downloads_model_when_absent(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc, mgr = _common_patches(
            cfg_path, faiss_available=True, model_present=False, ensure_ok=True
        )
        mgr.status = {"step": "idle", "error": "", "attempt": 0}

        app = _make_app()
        with patches["mgr"], patches["model_present"], patches["cfg_load"], \
             patches["cfg_path"], patches["subprocess"] as mock_exec, \
             patches["embed_fn"], patches["faiss"], patches["store"], \
             patches["wrap_argv"]:
            async with TestClient(TestServer(app)) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 200
                body = await resp.json()
                assert body == {"ok": True, "status": "downloading"}

                # Let the spawned background task tick (ensure_model is an
                # AsyncMock, so one loop iteration completes it).
                await asyncio.sleep(0)
                mgr.ensure_model.assert_awaited_once_with(attempts=DOWNLOAD_ATTEMPTS_INTERACTIVE)

                # Cleanup: cancel anything still retained on state.
                for task in list(app["state"].__dict__.get("_bg_embed_tasks", set())):
                    task.cancel()

            # Returned immediately — no faiss install / embed_fn wiring yet.
            mock_exec.assert_not_called()
            assert store.embed_fn is None

    @pytest.mark.asyncio
    async def test_download_in_flight_returns_without_new_task(self, tmp_path: Path) -> None:
        """A download already in flight is adopted — no second ensure_model task."""
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc, mgr = _common_patches(
            cfg_path, faiss_available=True, model_present=False, ensure_ok=True
        )
        mgr.status = {"step": "downloading", "error": "", "attempt": 1}

        with patches["mgr"], patches["model_present"], patches["cfg_load"], \
             patches["cfg_path"], patches["subprocess"] as mock_exec, \
             patches["embed_fn"], patches["faiss"], patches["store"], \
             patches["wrap_argv"]:
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 200
                body = await resp.json()
                assert body == {"ok": True, "status": "downloading", "setup_step": "downloading"}
                await asyncio.sleep(0)
                mgr.ensure_model.assert_not_awaited()

            mock_exec.assert_not_called()
            assert store.embed_fn is None


class TestLoadFaissIndexCalled:
    @pytest.mark.asyncio
    async def test_called_after_successful_setup(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc, mgr = _common_patches(cfg_path, faiss_available=True)

        with patches["mgr"], patches["model_present"], patches["cfg_load"], \
             patches["cfg_path"], patches["subprocess"], patches["embed_fn"], \
             patches["faiss"], patches["store"], patches["wrap_argv"]:
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 200

            store.load_faiss_index.assert_called_once()

    @pytest.mark.asyncio
    async def test_persists_llama_cpp_provider(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc, mgr = _common_patches(cfg_path, faiss_available=True)

        with patches["mgr"], patches["model_present"], patches["cfg_load"], \
             patches["cfg_path"], patches["subprocess"], patches["embed_fn"], \
             patches["faiss"], patches["store"], patches["wrap_argv"]:
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 200

        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["memory"]["embedding_provider"] == "llama_cpp"
        assert data["memory"]["embedding_dim"] == 1024
        assert data["memory"]["migrated"] is True


class TestLoadFaissIndexFailure:
    @pytest.mark.asyncio
    async def test_returns_500_when_load_faiss_raises(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc, mgr = _common_patches(cfg_path, faiss_available=True)
        store.load_faiss_index.side_effect = RuntimeError("corrupted index")

        with patches["mgr"], patches["model_present"], patches["cfg_load"], \
             patches["cfg_path"], patches["subprocess"], patches["embed_fn"], \
             patches["faiss"], patches["store"], patches["wrap_argv"]:
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 500
                body = await resp.json()
                assert "FAISS index load failed" in body["error"]

        assert mem_mod._embedding_setup_status["step"] == "idle"
        assert "FAISS index load failed" in str(mem_mod._embedding_setup_status["error"])


class TestFaissInstallTimeout:
    @pytest.mark.asyncio
    async def test_returns_500_on_timeout(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc, mgr = _common_patches(cfg_path, faiss_available=False, proc_rc=0)
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        async def _timeout_wait_for(coro, *, timeout=None):
            coro.close()  # clean up the coroutine
            raise asyncio.TimeoutError

        with patches["mgr"], patches["model_present"], patches["cfg_load"], \
             patches["cfg_path"], patches["subprocess"], patches["faiss"], \
             patches["store"], patches["wrap_argv"], \
             patch("asyncio.wait_for", side_effect=_timeout_wait_for):
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 500
                body = await resp.json()
                assert "timed out" in body["error"]

        proc.kill.assert_called_once()
        # The critical pin: the reap drains pipes via a SECOND communicate();
        # a bare wait() on a killed pip blocked writing into a full stderr
        # pipe would hang the handler forever (#5989).
        assert proc.communicate.call_count == 2
        proc.wait.assert_not_awaited()
        assert mem_mod._embedding_setup_status["step"] == "idle"
        assert "timed out" in str(mem_mod._embedding_setup_status["error"])


class TestEmbeddingStatusEndpoint:
    """embedding-status reports download-manager state + legacy field names."""

    @pytest.mark.asyncio
    async def test_status_reports_manager_and_embedder_state(self) -> None:
        cfg = MagicMock()
        cfg.memory.embedding_provider = "llama_cpp"
        embedder = MagicMock()
        embedder.is_ready.return_value = True
        embedder.model_id = "qwen3-embedding:0.6b"
        embedder.dim = 1024
        mgr = MagicMock()
        mgr.status = {"step": "downloading", "error": "", "attempt": 2}

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg), \
             patch(f"{_MOD}.get_shared_embedder", return_value=embedder), \
             patch(f"{_MOD}.model_download_manager", return_value=mgr), \
             patch(f"{_MOD}.model_file_present", return_value=False):
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.get("/api/memory/embedding-status")
                assert resp.status == 200
                body = await resp.json()

        assert body["enabled"] is True
        # Legacy token: the shipped frontend hard-checks provider === "ollama"
        # to render the healthy state; kept until KiroCrewWebsite ships its
        # companion change.
        assert body["provider"] == "ollama"
        assert body["model_available"] is False
        # Model disclosure: the Memory tab surfaces exactly which embedding
        # model runs locally + its vector dimensionality.
        assert body["model_id"] == "qwen3-embedding:0.6b"
        assert body["model_dim"] == 1024
        assert body["server_healthy"] is True
        # "downloading" maps to itself in the legacy setup_step vocabulary;
        # download_step/download_attempt expose the raw manager state.
        assert body["setup_step"] == "downloading"
        assert body["download_step"] == "downloading"
        assert body["download_attempt"] == 2
        # Legacy field names kept for frontend compatibility.
        assert body["ollama_installed"] is True
        assert body["needs_docker"] is False
        assert body["docker_available"] is True

    @pytest.mark.asyncio
    async def test_status_can_retry_after_failure(self) -> None:
        cfg = MagicMock()
        cfg.memory.embedding_provider = "llama_cpp"
        embedder = MagicMock()
        embedder.is_ready.return_value = False
        embedder.model_id = "qwen3-embedding:0.6b"
        embedder.dim = 1024
        mgr = MagicMock()
        mgr.status = {"step": "failed", "error": "sha256 mismatch", "attempt": 6}

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg), \
             patch(f"{_MOD}.get_shared_embedder", return_value=embedder), \
             patch(f"{_MOD}.model_download_manager", return_value=mgr), \
             patch(f"{_MOD}.model_file_present", return_value=False):
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.get("/api/memory/embedding-status")
                body = await resp.json()

        # Embeddings are always-on — "enabled" is unconditionally True.
        assert body["enabled"] is True
        # Raw "failed" maps to the legacy "error" token the frontend
        # polling loop terminates on; the raw step stays on download_step.
        assert body["setup_step"] == "error"
        assert body["download_step"] == "failed"
        assert body["setup_error"] == "sha256 mismatch"
        assert body["can_retry"] is True


class TestEnsurePipBootstrap:
    """Some packaged/minimal Python runtimes have no pip; ensure it is
    bootstrapped via ensurepip before the faiss-cpu install (else 'No module
    named pip')."""

    @pytest.mark.asyncio
    async def test_noop_when_pip_importable(self) -> None:
        # pip present -> no subprocess spawned, returns ok with empty error.
        with patch.dict("sys.modules", {"pip": MagicMock()}):
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                ok, err = await mem_mod._ensure_pip_available()
        assert ok is True
        assert err == ""
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_bootstraps_pip_when_missing(self) -> None:
        # pip absent -> ensurepip runs; success returns ok.
        proc = _mock_proc(rc=0)
        with patch.dict("sys.modules", {"pip": None}), \
             patch(f"{_MOD}.wrap_argv", side_effect=lambda argv, **kw: (argv, None)), \
             patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            ok, err = await mem_mod._ensure_pip_available()
        assert ok is True
        assert err == ""
        argv = mock_exec.call_args[0]
        assert "ensurepip" in argv
        assert "--upgrade" in argv

    @pytest.mark.asyncio
    async def test_returns_error_when_ensurepip_fails(self) -> None:
        # pip absent and ensurepip exits non-zero -> ok=False with a message.
        proc = _mock_proc(rc=1, stderr=b"ensurepip is not available")
        with patch.dict("sys.modules", {"pip": None}), \
             patch(f"{_MOD}.wrap_argv", side_effect=lambda argv, **kw: (argv, None)), \
             patch("asyncio.create_subprocess_exec", return_value=proc):
            ok, err = await mem_mod._ensure_pip_available()
        assert ok is False
        assert "ensurepip" in err

    @pytest.mark.asyncio
    async def test_enable_returns_500_when_pip_bootstrap_fails(self, tmp_path: Path) -> None:
        # End-to-end: faiss missing AND pip bootstrap fails -> handler 500s
        # before attempting the faiss install, with status reset to idle.
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        # faiss_available=False, but do NOT inject a fake pip -> force the
        # bootstrap path; make ensurepip (the only subprocess) fail.
        store = MagicMock()
        store.embed_fn = None
        store.load_faiss_index = MagicMock()
        proc = _mock_proc(rc=1, stderr=b"ensurepip is not available")

        with patch(f"{_MOD}.model_download_manager", return_value=_mock_mgr()), \
             patch(f"{_MOD}.model_file_present", return_value=True), \
             patch(f"{_MOD}.KiroCrewConfig.load", return_value=MagicMock()), \
             patch(f"{_MOD}.config_path", return_value=cfg_path), \
             patch("asyncio.create_subprocess_exec", return_value=proc), \
             patch.dict("sys.modules", {"faiss": None, "pip": None}), \
             patch(f"{_MOD}._get_vector_store", return_value=store), \
             patch(f"{_MOD}.wrap_argv", side_effect=lambda argv, **kw: (argv, None)):
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 500
                body = await resp.json()
                assert "pip bootstrap" in body["error"]

        assert mem_mod._embedding_setup_status["step"] == "idle"
        assert "pip bootstrap" in str(mem_mod._embedding_setup_status["error"])


class TestSetMigratedFailClosed:
    """_set_migrated must not clobber an unparseable config.json.

    Boot-time auto-migration calls _set_migrated(True) on every startup while
    memory.migrated is false, so a malformed config must fail closed (skip the
    write, preserve the file) rather than overwrite it with only the flag.
    """

    @pytest.mark.asyncio
    async def test_malformed_config_is_not_overwritten(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{ this is not valid json ", encoding="utf-8")
        with patch(f"{_MOD}.config_path", return_value=cfg_path):
            await mem_mod._set_migrated(True)
        # File is left byte-for-byte intact (no destructive rewrite).
        assert cfg_path.read_text(encoding="utf-8") == "{ this is not valid json "

    @pytest.mark.asyncio
    async def test_valid_config_preserves_other_sections(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"agent": {"provider": "acp"}, "slack": {"command": "/kc"}}),
            encoding="utf-8",
        )
        with patch(f"{_MOD}.config_path", return_value=cfg_path):
            await mem_mod._set_migrated(True)
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["memory"]["migrated"] is True
        # Other sections survive the write.
        assert data["agent"]["provider"] == "acp"
        assert data["slack"]["command"] == "/kc"

    @pytest.mark.asyncio
    async def test_missing_config_writes_fresh(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"  # does not exist
        with patch(f"{_MOD}.config_path", return_value=cfg_path):
            await mem_mod._set_migrated(True)
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["memory"]["migrated"] is True


class TestPipStderrRedaction:
    """Regression for issue #7279: pip/ensurepip stderr reached the gateway log
    unredacted. A private index configured with userinfo credentials leaks the
    token into pip's stderr on an auth failure; both warning sites must route
    the decoded stderr through ``redact_and_truncate`` (redact the FULL text
    first, then bound) so the token never lands in the log."""

    _SECRET = "LEAKY7279TOKEN"
    _MASK = "[REDACTED: credential]"

    def _auth_failure_stderr(self) -> bytes:
        return (
            "ERROR: HTTP error 401 while getting "
            "https://ci-bot:" + self._SECRET + "@pypi.corp.example/simple/faiss-cpu/\n"
            "ERROR: Could not install requirement faiss-cpu"
        ).encode()

    @pytest.mark.asyncio
    async def test_faiss_install_failure_log_masks_userinfo_credential(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc, mgr = _common_patches(
            cfg_path, faiss_available=False, proc_rc=1, proc_stderr=self._auth_failure_stderr()
        )

        with patches["mgr"], patches["model_present"], patches["cfg_load"], \
             patches["cfg_path"], patches["subprocess"], patches["faiss"], \
             patches["store"], patches["wrap_argv"], \
             caplog.at_level(logging.WARNING, logger=_MOD):
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 500

        messages = [r.getMessage() for r in caplog.records if "faiss-cpu install failed" in r.getMessage()]
        assert messages, "expected the faiss-cpu install-failed warning to be logged"
        for msg in messages:
            assert self._SECRET not in msg
            assert self._MASK in msg

    @pytest.mark.asyncio
    async def test_ensurepip_failure_log_masks_userinfo_credential(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        proc = _mock_proc(rc=1, stderr=self._auth_failure_stderr())
        with patch.dict("sys.modules", {"pip": None}), \
             patch(f"{_MOD}.wrap_argv", side_effect=lambda argv, **kw: (argv, None)), \
             patch("asyncio.create_subprocess_exec", return_value=proc), \
             caplog.at_level(logging.WARNING, logger=_MOD):
            ok, err = await mem_mod._ensure_pip_available()

        assert ok is False
        messages = [r.getMessage() for r in caplog.records if "ensurepip bootstrap failed" in r.getMessage()]
        assert messages, "expected the ensurepip bootstrap-failed warning to be logged"
        for msg in messages:
            assert self._SECRET not in msg
            assert self._MASK in msg

    @pytest.mark.asyncio
    async def test_non_utf8_stderr_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A pip failure on a non-UTF-8 console (Windows codepage, mangled index
        # response) must not raise UnicodeDecodeError on the failure path:
        # decode(errors="replace") keeps the warning best-effort.
        proc = _mock_proc(rc=1, stderr=b"\xff\xfe broken " + self._auth_failure_stderr())
        with patch.dict("sys.modules", {"pip": None}), \
             patch(f"{_MOD}.wrap_argv", side_effect=lambda argv, **kw: (argv, None)), \
             patch("asyncio.create_subprocess_exec", return_value=proc), \
             caplog.at_level(logging.WARNING, logger=_MOD):
            ok, err = await mem_mod._ensure_pip_available()

        assert ok is False
        assert "ensurepip" in err
        messages = [r.getMessage() for r in caplog.records if "ensurepip bootstrap failed" in r.getMessage()]
        assert messages
        for msg in messages:
            assert self._SECRET not in msg

    @pytest.mark.asyncio
    async def test_credential_straddling_log_bound_does_not_leak_fragment(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Redact-before-bound invariant: plant the credential so it straddles
        # the log bound. The old ``stderr.decode()[:500]`` head slice
        # would have emitted a raw unredacted prefix of the token; redacting the
        # full text first means no fragment of the secret can survive.
        bound = mem_mod._PIP_STDERR_LOG_CHARS
        prefix = "E" * (bound - len("https://ci-bot:") - 5)  # token crosses the bound
        stderr = (
            prefix + "https://ci-bot:" + self._SECRET + "@pypi.corp.example/simple/\n"
        ).encode()
        proc = _mock_proc(rc=1, stderr=stderr)
        with patch.dict("sys.modules", {"pip": None}), \
             patch(f"{_MOD}.wrap_argv", side_effect=lambda argv, **kw: (argv, None)), \
             patch("asyncio.create_subprocess_exec", return_value=proc), \
             caplog.at_level(logging.WARNING, logger=_MOD):
            ok, _ = await mem_mod._ensure_pip_available()

        assert ok is False
        messages = [r.getMessage() for r in caplog.records if "ensurepip bootstrap failed" in r.getMessage()]
        assert messages
        for msg in messages:
            # No prefix of the token may appear (the old slice leaked one).
            for n in range(3, len(self._SECRET) + 1):
                assert self._SECRET[:n] not in msg
