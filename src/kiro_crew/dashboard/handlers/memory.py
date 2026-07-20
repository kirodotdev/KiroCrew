"""Memory API handlers — preferences, projects, history, settings, semantic, episodic, embeddings, graph."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import math
import os
import sys
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.sandbox import cgroup_scope_argv, resource_limit_preexec, wrap_argv
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.vector_memory import SemanticRejectCode

from ._shared import _get_memory, _is_restricted_session

logger = logging.getLogger(__name__)


def _sel():
    """Late-binding sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811
    return _pkg.sel()


async def api_memory_preferences(request: web.Request) -> web.Response:
    """GET/PUT /api/memory/preferences."""
    state: DashboardState = request.app["state"]
    mem = _get_memory(state)
    if request.method == "PUT":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        content = body.get("content", "")
        mem.write_preferences(content)
        return web.json_response({"ok": True})
    return web.json_response({"content": mem.read_preferences()})


async def api_memory_projects(request: web.Request) -> web.Response:
    """GET/PUT /api/memory/projects."""
    state: DashboardState = request.app["state"]
    mem = _get_memory(state)
    if request.method == "PUT":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        content = body.get("content", "")
        mem.write_projects(content)
        return web.json_response({"ok": True})
    return web.json_response({"content": mem.read_projects()})


async def api_memory_history(request: web.Request) -> web.Response:
    """GET/PUT /api/memory/history — recent daily summaries."""
    state: DashboardState = request.app["state"]
    mem = _get_memory(state)
    if request.method == "PUT":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        content = body.get("content", "")
        # Write to today's history file
        today_path = mem._today_history_file()
        today_path.parent.mkdir(parents=True, exist_ok=True)
        today_path.write_text(content, encoding="utf-8")
        return web.json_response({"ok": True})
    return web.json_response({"content": mem.read_recent_history()})


async def api_memory_settings(request: web.Request) -> web.Response:
    """GET/PUT /api/memory/settings — memory consolidation config."""
    from kiro_crew.config.loader import KiroCrewConfig, config_path  # noqa: F811

    cfg = KiroCrewConfig.load()
    if request.method == "PUT":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        # Read existing config, update memory section only
        from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

        async with _get_config_lock():
            path = config_path()
            try:
                data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except Exception:
                data = {}
            mem = data.setdefault("memory", {})
            if "history_idle_hours" in body:
                try:
                    mem["history_idle_hours"] = max(0.5, float(body["history_idle_hours"]))
                except (ValueError, TypeError):
                    return web.json_response({"error": "history_idle_hours must be numeric"}, status=400)
            if "history_max_days" in body:
                try:
                    mem["history_max_days"] = max(7, int(body["history_max_days"]))
                except (ValueError, TypeError):
                    return web.json_response({"error": "history_max_days must be an integer"}, status=400)
            if "migrated" in body:
                mem["migrated"] = bool(body["migrated"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        # Apply to running consolidator
        state: DashboardState = request.app["state"]
        if state.consolidator:
            new_cfg = KiroCrewConfig.load()
            state.consolidator._history_idle_secs = new_cfg.memory.history_idle_hours * 3600
            state.consolidator._migrated = new_cfg.memory.migrated
        return web.json_response({"ok": True})
    return web.json_response(
        {
            "history_idle_hours": cfg.memory.history_idle_hours,
            "history_max_days": cfg.memory.history_max_days,
            "migrated": cfg.memory.migrated,
        }
    )


def _redact_memory_field(val: object) -> object:
    """Redact credentials and exfiltration URLs from a memory field."""
    if isinstance(val, (bytes, memoryview)):
        return None
    if isinstance(val, str):
        val, _ = redact_exfiltration_urls(val)
        val, _ = redact_credentials(val)
        return val
    if isinstance(val, list):
        return [_redact_memory_field(item) for item in val]
    if isinstance(val, dict):
        return {k: _redact_memory_field(v) for k, v in val.items()}
    return val


def _get_vector_store(state: DashboardState):
    """Get VectorMemoryStore from context_builder's memory, or create standalone."""
    mem = _get_memory(state)
    if mem.vector_store:
        return mem.vector_store
    # Fallback: create standalone
    if not hasattr(state, "_standalone_vector"):
        from kiro_crew.config.loader import KiroCrewConfig  # noqa: F811
        from kiro_crew.vector_memory import VectorMemoryStore  # noqa: F811

        cfg = KiroCrewConfig.load()
        store = VectorMemoryStore(embedding_dim=cfg.memory.embedding_dim)
        store.init()
        state._standalone_vector = store  # type: ignore[attr-defined]
        mem.vector_store = store
    return state._standalone_vector  # type: ignore[attr-defined]


async def api_memory_semantic(request: web.Request) -> web.Response:
    """GET /api/memory/semantic — list semantic memory entries (paginated).

    Server-capped via ``min(limit, 1000)`` + ``offset`` so a single GET can't
    serialize the whole (continuously-written) semantic table (CWE-770). The
    bound is generous (≈4 MB worst case at the 4 KB per-value limit) so the
    dashboard memory card's client-side filter keeps full coverage for typical
    single-user stores; a store larger than this needs server-side search.
    """
    store = _get_vector_store(request.app["state"])
    try:
        limit = min(int(request.query.get("limit", "1000")), 1000)
        offset = int(request.query.get("offset", "0"))
    except (ValueError, TypeError):
        return web.json_response({"error": "limit/offset must be integers"}, status=400)
    entries = []
    for e in store.get_all_semantic(limit=limit, offset=offset):
        d = {k: v for k, v in dict(e).items() if not isinstance(v, (bytes, memoryview))}
        entries.append(_redact_memory_field(d))
    return web.json_response({"entries": entries})


async def api_memory_semantic_write(request: web.Request) -> web.Response:
    """PUT /api/memory/semantic — create/update a semantic entry."""
    if _is_restricted_session(request.app["state"], request):
        sk = request.headers.get("X-Session-Key", "")
        _sel().log_api_access(
            caller=sk, operation="semantic.write", outcome="denied",
            source="dashboard", resources="restricted_session_block",
        )
        return web.json_response({"error": "Memory writes are not allowed in this session mode."}, status=403)
    store = _get_vector_store(request.app["state"])
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    key = body.get("key", "")
    value = body.get("value")
    confidence = float(body.get("confidence", 1.0)) if isinstance(body.get("confidence"), (int, float)) else 1.0
    source = body.get("source", "user_explicit")
    if not key or value is None:
        return web.json_response({"error": "key and value required"}, status=400)
    # set_semantic may embed via a blocking urllib call to Ollama; offload so a
    # slow/hung endpoint can't stall the dashboard event loop.
    err = await asyncio.to_thread(store.set_semantic, key, value, confidence, source)
    if err is not None:
        code, message = err
        sk = request.headers.get("X-Session-Key", "")
        _sel().log_api_access(
            caller=sk, operation="semantic.write", outcome="rejected",
            source="dashboard", resources=f"{code.value}:{key}",
        )
        status = 409 if code == SemanticRejectCode.CONFLICT else 422
        msg, _ = redact_exfiltration_urls(message)
        msg, _ = redact_credentials(msg)
        return web.json_response({"error": msg}, status=status)
    sk = request.headers.get("X-Session-Key", "")
    _sel().log_api_access(
        caller=sk, operation="semantic.write", outcome="success",
        source="dashboard", resources=key,
    )
    return web.json_response({"ok": True})


async def api_memory_semantic_delete(request: web.Request) -> web.Response:
    """DELETE /api/memory/semantic/{key} — tombstone a semantic entry."""
    if _is_restricted_session(request.app["state"], request):
        sk = request.headers.get("X-Session-Key", "")
        _sel().log_api_access(
            caller=sk, operation="semantic.delete", outcome="denied",
            source="dashboard", resources="restricted_session_block",
        )
        return web.json_response({"error": "Memory writes are not allowed in this session mode."}, status=403)
    store = _get_vector_store(request.app["state"])
    key = request.match_info["key"]
    ok = store.delete_semantic(key, source="user_explicit")
    if not ok:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"ok": True})


async def api_memory_events(request: web.Request) -> web.Response:
    """GET /api/memory/events — paginated audit trail."""
    store = _get_vector_store(request.app["state"])
    try:
        limit = min(int(request.query.get("limit", "50")), 200)
        offset = int(request.query.get("offset", "0"))
    except (ValueError, TypeError):
        return web.json_response({"error": "limit/offset must be integers"}, status=400)
    return web.json_response({"events": store.get_events(limit=limit, offset=offset)})


_embedding_setup_status: dict[str, object] = {"step": "idle", "error": ""}
_faiss_install_lock = asyncio.Lock()
_migrate_lock: asyncio.Lock | None = None


async def _set_migrated(value: bool) -> None:
    """Set memory.migrated in config.json."""
    from kiro_crew.config.loader import config_path  # noqa: F811
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_config_lock():
        path = config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            data = {}
        data.setdefault("memory", {})["migrated"] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


async def api_memory_embedding_status(request: web.Request) -> web.Response:
    """GET /api/memory/embedding-status — embedding system status + setup progress."""
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F811
    from kiro_crew.embeddings import EmbeddingClient, OllamaManager  # noqa: F811

    cfg = KiroCrewConfig.load()
    mgr = OllamaManager(cfg.memory.embedding_url, model=cfg.memory.embedding_model)
    enabled = cfg.memory.embedding_provider == "ollama"
    healthy = False
    model_available = False
    needs_docker = mgr._use_docker
    docker_available = bool(mgr._docker_bin()) if needs_docker else True
    if enabled and docker_available:
        _allow_remote = cfg.memory.allow_remote_embedding
        client = EmbeddingClient(cfg.memory.embedding_url, allow_remote=_allow_remote, model=cfg.memory.embedding_model)
        healthy = await client.health()
        model_available = await mgr.model_available()
    return web.json_response(
        {
            "enabled": enabled,
            "provider": cfg.memory.embedding_provider,
            "ollama_installed": mgr.ollama_binary is not None,
            "model_available": model_available,
            "server_healthy": healthy,
            "needs_docker": needs_docker,
            "docker_available": docker_available,
            "setup_step": _embedding_setup_status["step"],
            "setup_error": _embedding_setup_status["error"],
            "can_retry": _embedding_setup_status["step"] == "idle"
            and bool(_embedding_setup_status["error"]),
        }
    )


async def _ensure_pip_available() -> tuple[bool, str]:
    """Ensure pip is importable in the runtime interpreter.

    Some packaged or minimal Python runtimes ship without pip, so a bare
    ``sys.executable -m pip install`` fails with "No module named pip" and the
    faiss-cpu install below never runs. Bootstrap pip via ``ensurepip`` (shipped
    with CPython) first. No-op when pip already imports. Returns
    ``(ok, error_message)`` — ``error_message`` is empty on success.
    """
    try:
        import pip  # noqa: F401
        return True, ""
    except ImportError:
        pass
    sandboxed_argv, cleanup = wrap_argv(
        [sys.executable, "-m", "ensurepip", "--upgrade"],
        mode="standard",
    )
    sandboxed_argv = cgroup_scope_argv(sandboxed_argv)  # cgroup DoS ceiling (Talos bdf0d7e5)
    try:
        proc = await asyncio.create_subprocess_exec(
            *sandboxed_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=resource_limit_preexec(),
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("ensurepip bootstrap timed out")
            return False, "pip bootstrap (ensurepip) timed out"
        if proc.returncode != 0:
            logger.warning("ensurepip bootstrap failed: %s", stderr.decode()[:500])
            return False, "pip bootstrap (ensurepip) failed"
        importlib.invalidate_caches()
        logger.info("Bootstrapped pip via ensurepip")
        return True, ""
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass


async def api_memory_enable_embeddings(request: web.Request) -> web.Response:
    """POST /api/memory/enable-embeddings — install Ollama if needed, start, pull model, update config."""
    global _embedding_setup_status
    from kiro_crew.config.loader import KiroCrewConfig, config_path  # noqa: F811
    from kiro_crew.embeddings import OllamaManager  # noqa: F811

    cfg = KiroCrewConfig.load()

    # Allow retry — reset any previous error state
    if _embedding_setup_status["step"] == "error":
        _embedding_setup_status = {"step": "idle", "error": ""}

    # Prevent concurrent setup attempts
    if _embedding_setup_status["step"] not in ("idle", "done"):
        return web.json_response(
            {"error": f"Setup already in progress: {_embedding_setup_status['step']}"},
            status=409,
        )

    mgr = OllamaManager(cfg.memory.embedding_url, model=cfg.memory.embedding_model)
    _embedding_setup_status = {"step": "checking", "error": ""}

    try:
        if mgr._use_docker:
            docker = mgr._docker_bin()
            if not docker:
                _embedding_setup_status = {"step": "installing_docker", "error": ""}
                ok = await mgr._install_docker_ollama()
                if not ok:
                    _embedding_setup_status = {
                        "step": "idle",
                        "error": "Docker + Ollama install failed — click Enable to retry",
                    }
                    return web.json_response(
                        {
                            "error": "Docker install failed. Click Enable to retry, or run: sudo yum install docker && sudo systemctl start docker && sudo usermod -aG docker $USER"
                        },
                        status=400,
                    )
        elif not mgr.ollama_binary:
            _embedding_setup_status = {"step": "installing_ollama", "error": ""}
            ok = await mgr.install_ollama()
            if not ok or not mgr.ollama_binary:
                import platform as _plat  # noqa: F811

                system = _plat.system()
                if system == "Darwin":
                    hint = "Run: brew install ollama (or kirocrew doctor to auto-fix)"
                else:
                    hint = "Run: curl -fsSL https://ollama.com/install.sh | sh"
                _embedding_setup_status = {
                    "step": "idle",
                    "error": f"Ollama install failed — {hint}",
                }
                return web.json_response(
                    {"error": f"Ollama install failed. {hint}"},
                    status=400,
                )

        _embedding_setup_status = {"step": "starting", "error": ""}
        if not await mgr.start_server():
            _embedding_setup_status = {
                "step": "idle",
                "error": "Server failed to start — click Enable to retry",
            }
            return web.json_response(
                {"error": "Ollama server failed to start. Click Enable to retry."}, status=500
            )

        _embedding_setup_status = {"step": "downloading", "error": ""}
        if not await mgr.pull_model():
            _embedding_setup_status = {
                "step": "idle",
                "error": "Model download failed — click Enable to retry",
            }
            return web.json_response(
                {"error": "Model load failed. Click Enable to retry."}, status=500
            )

    except Exception:
        logger.exception("Embedding setup failed")
        _embedding_setup_status = {
            "step": "idle",
            "error": "Unexpected error — click Enable to retry",
        }
        return web.json_response(
            {"error": "Setup failed unexpectedly. Click Enable to retry."}, status=500
        )

    # Ensure faiss-cpu is installed (required for FAISS vector index).
    # Security note: uses wrap_argv(mode="standard") for OS-level sandbox,
    # matching the existing pip install pattern in apps/backend.py.
    async with _faiss_install_lock:
        try:
            import faiss  # noqa: F401
        except ImportError:
            _embedding_setup_status = {"step": "installing_faiss", "error": ""}
            # Some packaged/minimal Python runtimes ship without pip — bootstrap
            # it first, else the install below fails with "No module named pip".
            pip_ok, pip_err = await _ensure_pip_available()
            if not pip_ok:
                _embedding_setup_status = {
                    "step": "idle",
                    "error": f"{pip_err} — click Enable to retry",
                }
                return web.json_response(
                    {"error": f"{pip_err}. Click Enable to retry."}, status=500
                )
            sandboxed_argv, cleanup = wrap_argv(
                [sys.executable, "-m", "pip", "install", "-q",
                 "faiss-cpu", "--only-binary=:all:"],
                mode="standard",
            )
            sandboxed_argv = cgroup_scope_argv(
                sandboxed_argv
            )  # cgroup DoS ceiling (Talos bdf0d7e5)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *sandboxed_argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    preexec_fn=resource_limit_preexec(),
                )
                try:
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    logger.warning("faiss-cpu install timed out")
                    _embedding_setup_status = {
                        "step": "idle",
                        "error": "faiss-cpu install timed out — click Enable to retry",
                    }
                    return web.json_response(
                        {"error": "faiss-cpu install timed out."}, status=500,
                    )
                if proc.returncode != 0:
                    logger.warning("faiss-cpu install failed: %s", stderr.decode()[:500])
                    _embedding_setup_status = {
                        "step": "idle",
                        "error": "faiss-cpu installation failed — click Enable to retry",
                    }
                    return web.json_response(
                        {"error": "faiss-cpu installation failed. Click Enable to retry."},
                        status=500,
                    )
                else:
                    importlib.invalidate_caches()
                    logger.info("Installed faiss-cpu for vector indexing")
            finally:
                if cleanup:
                    try:
                        os.unlink(cleanup)
                    except OSError:
                        pass

    path = config_path()
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_config_lock():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            data = {}
        embed_url = data.get("memory", {}).get("embedding_url", "http://localhost:11434")
        from kiro_crew.config.loader import KiroCrewConfig  # noqa: F811
        from kiro_crew.embeddings import _validate_url, make_sync_embed_fn  # noqa: F811

        cfg = KiroCrewConfig.load()
        try:
            _validate_url(embed_url, allow_remote=cfg.memory.allow_remote_embedding)
        except ValueError as exc:
            _embedding_setup_status = {"step": "idle", "error": str(exc)}
            return web.json_response({"error": str(exc)}, status=400)
        embed_fn = make_sync_embed_fn(embed_url, model=cfg.memory.embedding_model)

        store = _get_vector_store(request.app["state"])
        store.embed_fn = embed_fn

        # Build FAISS index for any existing episodic memories with embeddings
        try:
            store.load_faiss_index()
        except Exception:
            logger.exception("Failed to load FAISS index")
            _embedding_setup_status = {
                "step": "idle",
                "error": "FAISS index load failed — click Enable to retry",
            }
            return web.json_response(
                {"error": "FAISS index load failed. Click Enable to retry."},
                status=500,
            )

        # Persist config only after store is successfully wired up
        data.setdefault("memory", {})["embedding_provider"] = "ollama"
        data["memory"].setdefault("embedding_url", "http://localhost:11434")
        data["memory"]["embedding_dim"] = 1024
        data["memory"]["migrated"] = True
        data["memory"]["embedding_runtime"] = "docker" if mgr._use_docker else "native"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    # Apply migrated to running consolidator
    state: DashboardState = request.app["state"]
    if state.consolidator:
        state.consolidator._migrated = True
    _embedding_setup_status = {"step": "done", "error": ""}
    return web.json_response({"ok": True})


async def api_memory_disable_embeddings(request: web.Request) -> web.Response:
    """POST /api/memory/disable-embeddings — update config to disable embeddings."""
    from kiro_crew.config.loader import config_path  # noqa: F811
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    path = config_path()
    async with _get_config_lock():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            data = {}
        data.setdefault("memory", {})["embedding_provider"] = "none"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    store = _get_vector_store(request.app["state"])
    store.embed_fn = None
    return web.json_response({"ok": True})


async def api_memory_episodic_search(request: web.Request) -> web.Response:
    """GET /api/memory/episodic/search?q=...&tags=t1,t2 — search episodic memories."""
    store = _get_vector_store(request.app["state"])
    query = request.query.get("q", "")[:500]
    try:
        limit = min(int(request.query.get("limit", "20")), 50)
    except (ValueError, TypeError):
        limit = 20
    tag_filter = [t.strip() for t in request.query.get("tags", "").split(",") if t.strip()] or None
    # _try_embed issues a blocking urllib call to Ollama; offload to keep the
    # dashboard event loop responsive if the embedding endpoint is slow.
    emb = (
        await asyncio.to_thread(store._try_embed, query)
        if store.embed_fn and query
        else None
    )
    results = []
    for e in store.search_episodic(
        query_embedding=emb, query_text=query, limit=limit, tag_filter=tag_filter
    ):
        d = {k: v for k, v in dict(e).items() if not isinstance(v, (bytes, memoryview))}
        results.append(_redact_memory_field(d))
    return web.json_response({"results": results})


async def api_memory_episodic_list(request: web.Request) -> web.Response:
    """GET /api/memory/episodic?tags=t1,t2 — paginated list of episodic memories."""
    store = _get_vector_store(request.app["state"])
    try:
        limit = min(int(request.query.get("limit", "50")), 100)
        offset = int(request.query.get("offset", "0"))
    except (ValueError, TypeError):
        return web.json_response({"error": "limit/offset must be integers"}, status=400)
    tag_filter = [t.strip() for t in request.query.get("tags", "").split(",") if t.strip()] or None
    entries = [
        _redact_memory_field(dict(e))
        for e in store.get_episodic_list(limit=limit, offset=offset, tag_filter=tag_filter)
    ]
    return web.json_response({"entries": entries})


async def api_memory_episodic_delete(request: web.Request) -> web.Response:
    """DELETE /api/memory/episodic/{id} — tombstone an episodic memory."""
    store = _get_vector_store(request.app["state"])
    mem_id = request.match_info["id"]
    ok = store.delete_episodic(mem_id)
    if not ok:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"ok": True})


async def api_memory_stats(request: web.Request) -> web.Response:
    """GET /api/memory/stats — memory system statistics."""
    store = _get_vector_store(request.app["state"])
    stats = store.memory_stats()
    # Add embedding status
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F811

    cfg = KiroCrewConfig.load()
    stats["embedding_provider"] = cfg.memory.embedding_provider
    stats["migrated"] = cfg.memory.migrated
    # Check if legacy markdown memory has real content (for showing Migrate button)
    from kiro_crew.memory import memory_dir  # noqa: F811

    md = memory_dir()
    has_legacy = False
    for f in [md / "preferences.md", md / "projects.md"]:
        if f.is_file():
            has_legacy = any(
                line.strip().startswith("- ") for line in f.read_text(encoding="utf-8", errors="replace").splitlines()
            )
            if has_legacy:
                break
    if not has_legacy and (md / "history").is_dir():
        has_legacy = any((md / "history").glob("*.md"))
    # Also check lessons.jsonl
    lessons_path = Path.home() / ".kirocrew" / "lessons.jsonl"
    if not has_legacy and lessons_path.is_file() and lessons_path.stat().st_size > 5:
        has_legacy = True
    stats["has_legacy_memory"] = has_legacy
    return web.json_response(stats)


async def api_memory_migrate(request: web.Request) -> web.Response:
    """POST /api/memory/migrate — migrate legacy markdown memory to vector store."""
    store = _get_vector_store(request.app["state"])
    # Wire up embedding function so migration generates FAISS vectors
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F811

    cfg = KiroCrewConfig.load()

    global _migrate_lock
    if _migrate_lock is None:
        _migrate_lock = asyncio.Lock()
    async with _migrate_lock:
        prev_embed_fn = store.embed_fn
        if cfg.memory.embedding_provider == "ollama":
            from kiro_crew.embeddings import make_sync_embed_fn  # noqa: F811

            store.embed_fn = make_sync_embed_fn(cfg.memory.embedding_url, timeout=15.0, model=cfg.memory.embedding_model)

        # Run in executor to avoid blocking event loop (can take 30+ seconds)
        loop = asyncio.get_running_loop()
        try:
            counts = await loop.run_in_executor(None, store.migrate_from_markdown)
        finally:
            store.embed_fn = prev_embed_fn  # restore previous, don't clobber
    # Auto-set migrated=true if migration produced entries
    if counts.get("semantic", 0) > 0 or counts.get("episodic", 0) > 0:
        await _set_migrated(True)
        state: DashboardState = request.app["state"]
        if state.consolidator:
            state.consolidator._migrated = True
    return web.json_response(counts)


async def api_memory_import(request: web.Request) -> web.Response:
    """POST /api/memory/import — import memory from JSON (export format)."""
    if _is_restricted_session(request.app["state"], request):
        sk = request.headers.get("X-Session-Key", "")
        _sel().log_api_access(
            caller=sk, operation="memory.import", outcome="denied",
            source="dashboard", resources="restricted_session_block",
        )
        return web.json_response({"error": "Memory writes are not allowed in this session mode."}, status=403)
    store = _get_vector_store(request.app["state"])
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # import_memory embeds each imported entry via blocking urllib to Ollama
    # (unbounded — one per entry); offload so a large import can't stall the
    # gateway event loop.
    counts = await run_in_embed_pool(store.import_memory, data)
    return web.json_response(counts)


async def api_memory_context_preview(request: web.Request) -> web.Response:
    """GET /api/memory/context-preview?q=... — preview what gets injected into prompts."""
    store = _get_vector_store(request.app["state"])
    query = request.query.get("q", "")[:500]
    semantic_ctx = store.get_semantic_context()
    # Filter semantic context by query if provided
    if query and semantic_ctx:
        lines = semantic_ctx.split("\n")
        q_lower = query.lower()
        filtered = [ln for ln in lines if q_lower in ln.lower() or ln.startswith("[")]
        semantic_ctx = "\n".join(filtered) if any(not ln.startswith("[") for ln in filtered) else ""
    # get_episodic_context embeds the query via blocking urllib to Ollama;
    # offload to keep the dashboard event loop responsive.
    episodic_ctx = (
        await run_in_embed_pool(store.get_episodic_context, query_text=query) if query else ""
    )
    return web.json_response(
        {
            "semantic_context": semantic_ctx,
            "episodic_context": episodic_ctx,
        }
    )


async def api_memory_consolidate(request: web.Request) -> web.Response:
    """POST /api/memory/consolidate — trigger immediate consolidation for testing."""
    state: DashboardState = request.app["state"]
    if _is_restricted_session(state, request):
        sk = request.headers.get("X-Session-Key", "")
        _sel().log_api_access(
            caller=sk, operation="memory.consolidate", outcome="denied",
            source="dashboard", resources="restricted_session_block",
        )
        return web.json_response({"error": "Memory writes are not allowed in this session mode."}, status=403)
    if not state.consolidator:
        return web.json_response({"error": "consolidator not available"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    key = body.get("key", "").strip()
    if not key:
        return web.json_response({"error": "session key required"}, status=400)
    include_history = body.get("include_history", True)
    # Fire consolidation in background
    if key in state.consolidator._running:
        return web.json_response({"error": "consolidation already running"}, status=409)
    state.consolidator._running.add(key)
    task = asyncio.create_task(state.consolidator._consolidate(key, include_history))
    state.consolidator._tasks.add(task)
    task.add_done_callback(state.consolidator._tasks.discard)
    return web.json_response({"ok": True, "key": key})


async def api_memory_observability(request: web.Request) -> web.Response:
    """GET /api/memory/observability — memory health metrics and context preview."""
    store = _get_vector_store(request.app["state"])
    query = request.query.get("q", "")[:500]
    stats = store.memory_stats()
    rejections = store.get_rejection_stats()
    # get_context_preview with a query embeds the query AND every non-lesson
    # semantic row (blocking urllib per row) — the worst on-loop amplification
    # in the store; offload so it can't stall the gateway event loop.
    preview = await run_in_embed_pool(store.get_context_preview, query_text=query)
    return web.json_response(
        {
            "stats": stats,
            "rejections": rejections,
            "context_preview": preview,
        }
    )


async def api_memory_promote(request: web.Request) -> web.Response:
    """POST /api/memory/promote — promote repeated episodic patterns to semantic facts."""
    store = _get_vector_store(request.app["state"])
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        min_count = int(body.get("min_count", 5))
        min_sim = float(body.get("min_sim", 0.75))
    except (ValueError, TypeError):
        return web.json_response({"error": "min_count/min_sim must be numeric"}, status=400)
    # Run in executor (can take 10+ seconds)
    loop = asyncio.get_running_loop()
    promoted = await loop.run_in_executor(None, store.promote_episodic_patterns, min_count, min_sim)
    return web.json_response({"ok": True, "promoted": promoted})


def _build_memory_graph(mem: Any, lessons: list) -> tuple[list[dict], list[dict]]:
    """Synchronous helper — safe to run in a thread."""
    import hashlib
    import re

    nodes: list[dict] = []
    edges: list[dict] = []
    node_ids: dict[str, str] = {}
    seen_ids: set[str] = set()

    def _id(prefix: str, label: str) -> str:
        return hashlib.md5(f"{prefix}:{label}".encode(), usedforsecurity=False).hexdigest()[:12]

    def _add(prefix: str, label: str, group: str, title: str = "") -> str:
        nid = _id(prefix, label)
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append(
                {"id": nid, "label": label[:60], "group": group, "title": title or label}
            )
            node_ids[f"{prefix}:{label}"] = nid
        return nid

    # --- Preferences ---
    try:
        pref_text = mem.read_preferences() or ""
        for line in pref_text.splitlines():
            line = line.strip().removeprefix("- ").strip()
            if (
                line
                and not line.startswith("#")
                and not line.startswith("<!--")
                and len(line) > 5
            ):
                _add("pref", line[:80], "preference", line)
    except Exception:
        pass

    # --- Projects ---
    try:
        proj_text = mem.read_projects() or ""
        current_project = ""
        for line in proj_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                current_project = stripped[3:].strip()
                _add("proj", current_project, "project", current_project)
            elif stripped.startswith("- ") and current_project:
                detail = stripped[2:].strip()
                if len(detail) > 3:
                    detail_id = _add(
                        "proj_d", f"{current_project}: {detail[:60]}", "project", detail
                    )
                    proj_id = node_ids.get(f"proj:{current_project}")
                    if proj_id:
                        edges.append({"from": proj_id, "to": detail_id})
    except Exception:
        pass

    # --- Semantic Memory (vector store) ---
    vs = mem.vector_store
    if vs:
        try:
            for entry in vs.get_all_semantic():
                key = entry.get("key", "")
                val = entry.get("value_json", "")
                if isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except Exception:
                        pass
                val_str = str(val) if not isinstance(val, str) else val
                _add("sem", key, "semantic", f"{key} = {val_str[:120]}")
        except Exception:
            pass

    # --- Lessons ---
    try:
        lessons_data = None
        try:
            lessons_data = vs.get_lessons() if vs else None
        except Exception:
            pass
        if lessons_data:
            for entry in lessons_data:
                rule = entry.get("value_json", "")
                if isinstance(rule, str):
                    try:
                        rule = json.loads(rule)
                    except Exception:
                        pass
                _add("lesson", str(rule)[:80], "lesson", str(rule))
        else:
            for le in lessons:
                _add("lesson", le.rule[:80], "lesson", le.rule)
    except Exception:
        pass

    # --- History (recent days only) ---
    try:
        hist = mem.read_recent_history(days=14) or ""
        for line in hist.splitlines():
            stripped = line.strip()
            m = re.match(r"^#{1,4}\s+(.+)", stripped)
            if m:
                raw = str(_redact_memory_field(m.group(1).strip()))
                _add("hist", raw[:80], "history", raw)
            elif stripped.startswith("[") and "]" in stripped and len(stripped) > 20:
                raw = str(_redact_memory_field(stripped))
                _add("hist", raw[:80], "history", raw[:200])
    except Exception:
        pass

    # --- Auto-detect edges by keyword overlap ---
    project_names = [
        (node_ids[k], k.split(":", 1)[1].lower())
        for k in node_ids
        if k.startswith("proj:") and ":" not in k.split(":", 1)[1]
    ]
    for n in nodes:
        if n["group"] in ("preference", "semantic", "lesson", "history"):
            title_lower = n["title"].lower()
            for proj_id, proj_name in project_names:
                if (
                    re.search(r"\b" + re.escape(proj_name) + r"\b", title_lower)
                    and n["id"] != proj_id
                ):
                    edges.append({"from": n["id"], "to": proj_id})

    # Pre-compute node positions server-side so the client never runs a layout.
    _assign_layout_coords(nodes)

    return nodes, edges


def _assign_layout_coords(nodes: list[dict]) -> None:
    """Assign each node a deterministic (x, y) — group-partitioned grid, O(n).

    Replaces the frontend's O(n²) force-directed physics (vis-network
    ``forceAtlas2Based`` + 150 stabilization iterations), which ran on the
    browser main thread and froze the UI at thousands of nodes. Each memory
    group gets its own grid block laid out left-to-right, so the graph reads as
    color-coded clusters and renders instantly with physics disabled on the
    client. Mutates ``nodes`` in place (adds ``x``/``y`` keys).

    NOTE: a plain grid is the right tool here precisely because the current
    graph is almost entirely disconnected (edges only form on full-project-name
    overlap, which almost never matches). If a future change introduces real
    edge density, swap this body for a force-directed layout (e.g. a
    numpy-vectorised Barnes-Hut pass, O(n log n)); the frontend already consumes
    server-provided x/y, so only this function changes — no client edit needed.
    """
    # Fixed group order keeps the layout stable across requests.
    group_order = ["preference", "project", "semantic", "lesson", "history"]
    sx, sy = 220, 90  # per-node horizontal / vertical spacing
    group_gap = 400  # blank gutter between adjacent group blocks

    by_group: dict[str, list[dict]] = {}
    for n in nodes:
        by_group.setdefault(n.get("group", "history"), []).append(n)

    # Known groups first (stable), then any unexpected group so nothing is lost.
    ordered_groups = group_order + [g for g in by_group if g not in group_order]

    x_cursor = 0
    for group in ordered_groups:
        members = by_group.get(group)
        if not members:
            continue
        # Slightly wider than tall for a balanced, readable block.
        cols = max(1, math.ceil(math.sqrt(len(members) * 1.6)))
        for idx, node in enumerate(members):
            row, col = divmod(idx, cols)
            node["x"] = x_cursor + col * sx
            node["y"] = row * sy
        x_cursor += cols * sx + group_gap


async def api_memory_graph(request: web.Request) -> web.Response:
    """GET /api/memory/graph — return all memory as nodes + edges for graph visualization."""
    state: DashboardState = request.app["state"]
    mem = _get_memory(state)

    try:
        loop = asyncio.get_running_loop()
        nodes, edges = await loop.run_in_executor(
            None, _build_memory_graph, mem, state.lessons.load_all()
        )

        for n in nodes:
            n["label"] = _redact_memory_field(n["label"])
            n["title"] = _redact_memory_field(n["title"])

        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="memory_graph", outcome="success"
        )
        return web.json_response({"nodes": nodes, "edges": edges})
    except Exception:
        logging.getLogger(__name__).exception("memory_graph failed")
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="memory_graph", outcome="failure"
        )
        return web.json_response({"error": "failed to build memory graph"}, status=500)
