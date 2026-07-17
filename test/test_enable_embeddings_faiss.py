"""Tests for faiss-cpu installation block in enable-embeddings handler."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.memory as mem_mod

_MOD = "kiro_crew.dashboard.handlers.memory"


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/api/memory/enable-embeddings", mem_mod.api_memory_enable_embeddings)
    app["state"] = MagicMock(consolidator=None)
    return app


def _mock_mgr():
    mgr = MagicMock()
    mgr._use_docker = False
    mgr.ollama_binary = "/usr/bin/ollama"
    mgr.start_server = AsyncMock(return_value=True)
    mgr.pull_model = AsyncMock(return_value=True)
    return mgr


def _mock_cfg():
    cfg = MagicMock()
    cfg.memory.embedding_url = "http://localhost:11434"
    cfg.memory.embedding_model = "test"
    cfg.memory.allow_remote_embedding = False
    return cfg


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


def _common_patches(cfg_path, faiss_available=False, proc_rc=0, proc_stderr=b""):
    """Return a list of context managers for the common mocks."""
    store = MagicMock()
    store.embed_fn = None
    store.load_faiss_index = MagicMock()

    cfg = _mock_cfg()
    proc = _mock_proc(proc_rc, proc_stderr)
    faiss_mod = MagicMock() if faiss_available else None

    patches = {
        "ollama": patch("kiro_crew.embeddings.OllamaManager", return_value=_mock_mgr()),
        "cfg_load": patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg),
        "cfg_path": patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
        "subprocess": patch("asyncio.create_subprocess_exec", return_value=proc),
        "validate": patch("kiro_crew.embeddings._validate_url"),
        "embed_fn": patch("kiro_crew.embeddings.make_sync_embed_fn", return_value=lambda t: [0.0]),
        # Inject a fake ``pip`` so ``_ensure_pip_available`` short-circuits and
        # these faiss-focused tests see exactly one subprocess (the faiss install).
        "faiss": patch.dict("sys.modules", {"faiss": faiss_mod, "pip": MagicMock()}),
        "store": patch(f"{_MOD}._get_vector_store", return_value=store),
        "wrap_argv": patch(f"{_MOD}.wrap_argv", side_effect=lambda argv, **kw: (argv, None)),
    }
    return patches, store, proc


class TestFaissInstallSuccess:
    @pytest.mark.asyncio
    async def test_pip_install_runs_when_faiss_missing(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc = _common_patches(cfg_path, faiss_available=False, proc_rc=0)

        with patches["ollama"], patches["cfg_load"], patches["cfg_path"], \
             patches["subprocess"] as mock_exec, patches["validate"], \
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


class TestFaissInstallFailure:
    @pytest.mark.asyncio
    async def test_returns_500_and_resets_status(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc = _common_patches(
            cfg_path, faiss_available=False, proc_rc=1, proc_stderr=b"No matching distribution"
        )

        with patches["ollama"], patches["cfg_load"], patches["cfg_path"], \
             patches["subprocess"], patches["faiss"], patches["store"], \
             patches["wrap_argv"]:
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 500
                body = await resp.json()
                assert "faiss-cpu installation failed" in body["error"]

        assert mem_mod._embedding_setup_status["step"] == "idle"
        assert "faiss-cpu" in mem_mod._embedding_setup_status["error"]


class TestFaissAlreadyInstalled:
    @pytest.mark.asyncio
    async def test_skips_pip_when_faiss_importable(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc = _common_patches(cfg_path, faiss_available=True)

        with patches["ollama"], patches["cfg_load"], patches["cfg_path"], \
             patches["subprocess"] as mock_exec, patches["validate"], \
             patches["embed_fn"], patches["faiss"], patches["store"], \
             patches["wrap_argv"]:
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 200

            mock_exec.assert_not_called()


class TestLoadFaissIndexCalled:
    @pytest.mark.asyncio
    async def test_called_after_successful_setup(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc = _common_patches(cfg_path, faiss_available=True)

        with patches["ollama"], patches["cfg_load"], patches["cfg_path"], \
             patches["subprocess"], patches["validate"], \
             patches["embed_fn"], patches["faiss"], patches["store"], \
             patches["wrap_argv"]:
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 200

            store.load_faiss_index.assert_called_once()


class TestLoadFaissIndexFailure:
    @pytest.mark.asyncio
    async def test_returns_500_when_load_faiss_raises(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc = _common_patches(cfg_path, faiss_available=True)
        store.load_faiss_index.side_effect = RuntimeError("corrupted index")

        with patches["ollama"], patches["cfg_load"], patches["cfg_path"], \
             patches["subprocess"], patches["validate"], \
             patches["embed_fn"], patches["faiss"], patches["store"], \
             patches["wrap_argv"]:
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 500
                body = await resp.json()
                assert "FAISS index load failed" in body["error"]

        assert mem_mod._embedding_setup_status["step"] == "idle"
        assert "FAISS index load failed" in mem_mod._embedding_setup_status["error"]


class TestFaissInstallTimeout:
    @pytest.mark.asyncio
    async def test_returns_500_on_timeout(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "kirocrew.json"
        cfg_path.write_text("{}", encoding="utf-8")
        patches, store, proc = _common_patches(cfg_path, faiss_available=False, proc_rc=0)
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        async def _timeout_wait_for(coro, *, timeout=None):
            coro.close()  # clean up the coroutine
            raise asyncio.TimeoutError

        with patches["ollama"], patches["cfg_load"], patches["cfg_path"], \
             patches["subprocess"], patches["faiss"], patches["store"], \
             patches["wrap_argv"], \
             patch("asyncio.wait_for", side_effect=_timeout_wait_for):
            async with TestClient(TestServer(_make_app())) as c:
                resp = await c.post("/api/memory/enable-embeddings")
                assert resp.status == 500
                body = await resp.json()
                assert "timed out" in body["error"]

        proc.kill.assert_called_once()
        assert mem_mod._embedding_setup_status["step"] == "idle"
        assert "timed out" in mem_mod._embedding_setup_status["error"]


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

        with patch("kiro_crew.embeddings.OllamaManager", return_value=_mock_mgr()), \
             patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=_mock_cfg()), \
             patch("kiro_crew.config.loader.config_path", return_value=cfg_path), \
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
        assert "pip bootstrap" in mem_mod._embedding_setup_status["error"]
