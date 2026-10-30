"""The embedding-model routes of ``kiro_crew.dashboard.handlers.memory`` and its config writes.

This file pins the embedding-model apply endpoint, the status of a custom model,
``_write_embed_model_config``, the pip bootstrap, enabling and disabling embeddings, the
apply worker's install and rollback, and the config read-modify-write transactions that
run on one worker off the event loop. The family's style and its other files are
described in ``test_handlers_memory_coverage.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from handlers_memory_coverage_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _BadJSON,
    _body,
    _fake_sel,
    _make_request,
    _make_state,
    _reset_setup_status,
    _store,
)

import kiro_crew.dashboard.handlers.memory as mem_mod
from kiro_crew.embeddings import CustomModelSpec
from kiro_crew.embeddings_shared import ReembedProgress
from kiro_crew.testing.wait import await_until

_MOD = "kiro_crew.dashboard.handlers.memory"


# ---------------------------------------------------------------------------
# embedding-model apply endpoint (request boundary only)
# ---------------------------------------------------------------------------


def _prog(active: bool = False) -> Any:
    prog = MagicMock()
    prog.is_active.return_value = active
    prog.snapshot.return_value = {"state": "idle"}
    return prog


class TestEmbeddingModelEndpoint:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim and no test in the memory handler coverage files calls monkeypatch.undo()
    def _no_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_EMBED_MODEL_PATH", raising=False)

    @pytest.mark.asyncio
    async def test_denied_for_restricted_session(self) -> None:
        state = _make_state(restricted_keys={"dashboard:ghost"})
        req = _make_request(
            state, method="POST", json_body={"path": ""}, session_key="dashboard:ghost"
        )
        resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 403
        assert _body(resp)["code"] == "restricted_session"

    @pytest.mark.asyncio
    async def test_rejects_unparseable_body(self) -> None:
        state = _make_state()
        req = _make_request(state, method="POST", json_body=_BadJSON())
        resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_rejects_valid_json_that_is_not_an_object(self) -> None:
        state = _make_state()
        payloads: list[Any] = [[], "str", 5]
        for payload in payloads:
            req = _make_request(state, method="POST", json_body=payload)
            resp = await mem_mod.api_memory_embedding_model(req)
            assert resp.status == 400
            # The shared guard distinguishes "unparseable" from "parsed, wrong
            # shape"; this handler must not answer invalid_json for both.
            assert _body(resp)["code"] == "body_not_object"

    @pytest.mark.asyncio
    async def test_validation_error_is_400_with_code(self) -> None:
        state = _make_state()
        req = _make_request(state, method="POST", json_body={"path": "/nope.gguf"})
        with patch(
            f"{_MOD}.validate_custom_model_path",
            return_value=(Path("/nope.gguf"), "not a file", "not_found"),
        ):
            resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 400
        assert _body(resp) == {"ok": False, "error": "not a file", "code": "not_found"}

    @pytest.mark.asyncio
    async def test_validate_only_reports_size_without_applying(self, tmp_path: Path) -> None:
        model = tmp_path / "m.gguf"
        model.write_bytes(b"abcd")
        state = _make_state()
        req = _make_request(
            state, method="POST", json_body={"path": str(model), "validate_only": True}
        )
        with (
            patch(f"{_MOD}.validate_custom_model_path", return_value=(model, "", "")),
            patch(f"{_MOD}._apply_embedding_model") as apply_mock,
        ):
            resp = await mem_mod.api_memory_embedding_model(req)
        assert _body(resp) == {"ok": True, "size_bytes": 4}
        apply_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_path_checks_run_off_the_event_loop(self, tmp_path: Path) -> None:
        # The path is the caller's choice, so on a dead mount its stats would
        # park the gateway's one loop. Both must run on a probe-pool worker.
        model = tmp_path / "m.gguf"
        model.write_bytes(b"abcd")
        loop_thread = threading.get_ident()
        seen: list[int] = []
        real_stat = Path.stat

        def record_validate(raw: str, _origin: str) -> tuple[Path, str, str]:
            seen.append(threading.get_ident())
            return Path(raw), "", ""

        def record_stat(self: Path, *a: Any, **kw: Any) -> Any:
            if self == model:
                seen.append(threading.get_ident())
            return real_stat(self, *a, **kw)

        state = _make_state()
        req = _make_request(
            state, method="POST", json_body={"path": str(model), "validate_only": True}
        )
        with (
            patch(f"{_MOD}.validate_custom_model_path", side_effect=record_validate),
            patch.object(Path, "stat", record_stat),
        ):
            resp = await mem_mod.api_memory_embedding_model(req)
        assert _body(resp) == {"ok": True, "size_bytes": 4}
        assert len(seen) == 2 and loop_thread not in seen

    @pytest.mark.asyncio
    async def test_busy_probe_pool_answers_a_coded_503(self) -> None:
        from kiro_crew.dashboard.handlers import memory_embeddings

        state = _make_state()
        req = _make_request(state, method="POST", json_body={"path": "/m.gguf"})
        busy = AsyncMock(side_effect=memory_embeddings._PathProbeBusy())
        with patch.object(memory_embeddings, "_run_path_probe", busy):
            resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 503
        assert _body(resp)["code"] == "path_probe_busy"

    @pytest.mark.asyncio
    async def test_validate_only_tolerates_unstatable_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone.gguf"
        state = _make_state()
        req = _make_request(
            state, method="POST", json_body={"path": str(missing), "validate_only": True}
        )
        with patch(f"{_MOD}.validate_custom_model_path", return_value=(missing, "", "")):
            resp = await mem_mod.api_memory_embedding_model(req)
        assert _body(resp) == {"ok": True, "size_bytes": 0}

    @pytest.mark.asyncio
    async def test_env_override_blocks_config_write_with_409(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_EMBED_MODEL_PATH", "/env/model.gguf")
        state = _make_state()
        req = _make_request(state, method="POST", json_body={"path": ""})
        resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 409
        assert _body(resp)["code"] == "env_override_active"

    @pytest.mark.asyncio
    async def test_apply_in_progress_is_409(self) -> None:
        state = _make_state()
        req = _make_request(state, method="POST", json_body={"path": ""})
        with patch(f"{_MOD}.reembed_progress", return_value=_prog(active=True)):
            resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 409
        assert _body(resp)["code"] == "model_change_in_progress"

    @pytest.mark.asyncio
    async def test_unavailable_vector_store_is_503_and_arms_nothing(self) -> None:
        state = _make_state()
        prog = _prog()
        req = _make_request(state, method="POST", json_body={"path": ""})
        with (
            patch(f"{_MOD}.reembed_progress", return_value=prog),
            patch(f"{_MOD}._get_vector_store", side_effect=RuntimeError("db gone")),
        ):
            resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 503
        assert _body(resp)["code"] == "vector_store_unavailable"
        prog.begin_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_armed_during_store_await_is_409_and_arms_nothing(self) -> None:
        """The awaited store acquisition can yield to the loop, so a
        concurrent apply may arm between the single-flight gate and
        ``begin_apply()`` — the post-await re-check must refuse it."""
        state = _make_state()
        prog = _prog()
        req = _make_request(state, method="POST", json_body={"path": ""})

        async def _arm_mid_await(_state: Any) -> Any:
            prog.is_active.return_value = True
            return _store()

        with (
            patch(f"{_MOD}.reembed_progress", return_value=prog),
            patch(f"{_MOD}._get_vector_store_async", side_effect=_arm_mid_await),
        ):
            resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 409
        assert _body(resp)["code"] == "model_change_in_progress"
        prog.begin_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_revert_to_bundled_dispatches_worker(self) -> None:
        state = _make_state(vector_store=_store())
        prog = _prog()
        req = _make_request(state, method="POST", json_body={"path": ""})
        with (
            patch(f"{_MOD}.reembed_progress", return_value=prog),
            patch(f"{_MOD}._apply_embedding_model") as apply_mock,
        ):
            resp = await mem_mod.api_memory_embedding_model(req)
            assert _body(resp) == {"ok": True, "size_bytes": 0, "status": "applying"}
            prog.begin_apply.assert_called_once()
            # flake-ok: pre-existing await of the apply worker the patched apply returns from at once; moved verbatim and tracked for a de-flake pass
            await asyncio.wrap_future(state._embed_model_apply_task)
        apply_mock.assert_called_once()


# ---------------------------------------------------------------------------
# embedding-status custom-model branch
# ---------------------------------------------------------------------------


class TestEmbeddingStatusCustomModel:
    def _patches(self, custom: Any, model_present: bool) -> Any:
        embedder = MagicMock(model_id="custom.gguf", dim=768)
        embedder.is_ready.return_value = True
        mgr = MagicMock()
        mgr.status = {"step": "idle", "error": "", "attempt": 0}
        return (
            patch(f"{_MOD}.get_shared_embedder", return_value=embedder),
            patch(f"{_MOD}.model_download_manager", return_value=mgr),
            patch(f"{_MOD}.model_file_present", return_value=model_present),
            patch(f"{_MOD}.resolve_custom_model", return_value=custom),
        )

    @pytest.mark.asyncio
    async def test_healthy_custom_model_reports_done_and_no_retry(self) -> None:
        model_path = Path("/models/custom.gguf")
        custom = CustomModelSpec(model_path, "custom.gguf", 768, "")
        a, b, c, d = self._patches(custom, model_present=True)
        with a, b, c, d:
            body = _body(await mem_mod.api_memory_embedding_status(_make_request(_make_state())))
        assert body["model_source"] == "custom"
        # Compare against str(Path) rather than the POSIX literal: the property
        # is "the handler surfaces the resolved path", not which separator the
        # host uses. Windows renders this as \models\custom.gguf.
        assert body["model_path"] == str(model_path)
        assert body["setup_step"] == "done"
        assert body["setup_error"] == ""
        assert body["can_retry"] is False
        assert body["model_dim"] == 768

    @pytest.mark.asyncio
    async def test_broken_custom_model_reports_error_without_retry(self) -> None:
        custom = CustomModelSpec(Path("/models/custom.gguf"), "custom.gguf", 768, "unreadable")
        a, b, c, d = self._patches(custom, model_present=False)
        with a, b, c, d:
            body = _body(await mem_mod.api_memory_embedding_status(_make_request(_make_state())))
        assert body["setup_step"] == "error"
        assert body["setup_error"] == "unreadable"
        assert body["can_retry"] is False

    @pytest.mark.asyncio
    async def test_missing_custom_file_reports_error_naming_the_path(self) -> None:
        model_path = Path("/models/custom.gguf")
        custom = CustomModelSpec(model_path, "custom.gguf", 768, "")
        a, b, c, d = self._patches(custom, model_present=False)
        with a, b, c, d:
            body = _body(await mem_mod.api_memory_embedding_status(_make_request(_make_state())))
        assert body["setup_step"] == "error"
        # str(Path), not the POSIX literal -- see the note above.
        assert str(model_path) in body["setup_error"]


# ---------------------------------------------------------------------------
# embedding-status runtime-load fault: a failed llama.cpp runtime load must not
# read as a healthy idle model.
# ---------------------------------------------------------------------------


class TestEmbeddingStatusRuntimeLoad:
    # The handler reads the runtime load result from two primitives:
    # ``_load_llama_class`` (the import result) and ``_platform_libs_dirname``
    # (whether vendored libs ship for the platform). The three cases below drive
    # those directly.
    _FAILED = ("failed", None, "linux_x86_64")  # import failed, libs ship -> fault
    _IMPORTABLE = ("importable", object(), "linux_x86_64")  # import ok
    _UNSUPPORTED = ("unsupported", None, None)  # no libs ship -> designed degradation

    def _patches(self, runtime_case: Any, *, model_present: bool = True) -> Any:
        _name, llama, libs_dir = runtime_case
        # A healthy-IDLE embedder: the model file is present and the backend is
        # not serving yet (is_ready() False) — the state a failed runtime load is
        # indistinguishable from unless the status reads the load result.
        embedder = MagicMock(model_id="bundled", dim=1024)
        embedder.is_ready.return_value = False
        mgr = MagicMock()
        mgr.status = {"step": "idle", "error": "", "attempt": 0}
        return (
            patch(f"{_MOD}.get_shared_embedder", return_value=embedder),
            patch(f"{_MOD}.model_download_manager", return_value=mgr),
            patch(f"{_MOD}.model_file_present", return_value=model_present),
            patch(f"{_MOD}.resolve_custom_model", return_value=None),
            patch(f"{_MOD}.embedding_backend_serving", return_value=False),
            patch(f"{_MOD}._load_llama_class", return_value=llama),
            patch(f"{_MOD}._platform_libs_dirname", return_value=libs_dir),
        )

    @pytest.mark.asyncio
    async def test_failed_runtime_load_reports_fault_not_idle(self) -> None:
        with contextlib.ExitStack() as stack:
            for p in self._patches(self._FAILED):
                stack.enter_context(p)
            body = _body(await mem_mod.api_memory_embedding_status(_make_request(_make_state())))
        # The fault is distinct from the healthy-idle payload.
        assert body["server_healthy"] is False
        assert body["setup_error_code"] == "model_error_runtime_load"
        # setup_step is left as the download manager reports it (idle), NOT forced
        # to the download-flow "error" state whose card copy says "check network".
        assert body["setup_step"] == "idle"
        assert body["setup_error"]  # names the keyword-search cost
        assert "keyword search" in body["setup_error"]

    @pytest.mark.asyncio
    async def test_importable_runtime_keeps_neutral_idle_state(self) -> None:
        with contextlib.ExitStack() as stack:
            for p in self._patches(self._IMPORTABLE):
                stack.enter_context(p)
            body = _body(await mem_mod.api_memory_embedding_status(_make_request(_make_state())))
        # Healthy idle model: unchanged neutral state, no fault, no runtime code.
        assert body["server_healthy"] is True
        assert body["setup_error_code"] == ""
        assert body["setup_step"] == "idle"
        assert body["setup_error"] == ""

    @pytest.mark.asyncio
    async def test_unsupported_platform_is_not_a_fault(self) -> None:
        # No vendored libs ship for the platform: keyword search is the designed
        # degradation, not a fault, so the status must not flip to an error code
        # or report unhealthy on that account alone.
        with contextlib.ExitStack() as stack:
            for p in self._patches(self._UNSUPPORTED):
                stack.enter_context(p)
            body = _body(await mem_mod.api_memory_embedding_status(_make_request(_make_state())))
        assert body["server_healthy"] is True
        assert body["setup_error_code"] == ""
        assert body["setup_step"] == "idle"


# ---------------------------------------------------------------------------
# _write_embed_model_config
# ---------------------------------------------------------------------------


class TestWriteEmbedModelConfig:
    @pytest.mark.asyncio
    async def test_writes_path_and_dim_preserving_other_sections(self, tmp_path: Path) -> None:
        model = tmp_path / "new.gguf"
        model.write_bytes(b"model weights")
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"agent": {"provider": "acp"}, "memory": {"embed_model_id": "old"}}),
            encoding="utf-8",
        )
        with patch(f"{_MOD}.config_path", return_value=cfg_path):
            await mem_mod._write_embed_model_config(str(model), 1024)
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["memory"]["embed_model_path"] == str(model)
        assert data["memory"]["embedding_dim"] == 1024
        assert data["memory"]["embed_model_id"] == mem_mod._custom_model_id(model, "")
        assert data["memory"]["embed_model_id"] != "old"
        assert data["agent"]["provider"] == "acp"

    @pytest.mark.asyncio
    async def test_empty_path_reverts_by_removing_the_key(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"memory": {"embed_model_path": "/m/old.gguf"}}), encoding="utf-8"
        )
        with patch(f"{_MOD}.config_path", return_value=cfg_path):
            await mem_mod._write_embed_model_config("", 0)
        memory = json.loads(cfg_path.read_text(encoding="utf-8"))["memory"]
        assert "embed_model_path" not in memory
        # dim <= 0 means "unknown width" and must not be persisted.
        assert "embedding_dim" not in memory

    @pytest.mark.asyncio
    async def test_missing_config_is_created(self, tmp_path: Path) -> None:
        model = tmp_path / "new.gguf"
        model.write_bytes(b"model weights")
        cfg_path = tmp_path / "config.json"
        with patch(f"{_MOD}.config_path", return_value=cfg_path):
            await mem_mod._write_embed_model_config(str(model), 512)
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["memory"]["embedding_dim"] == 512

    @pytest.mark.asyncio
    async def test_unparseable_config_raises_and_is_left_intact(self, tmp_path: Path) -> None:
        model = tmp_path / "new.gguf"
        model.write_bytes(b"model weights")
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{ broken", encoding="utf-8")
        with patch(f"{_MOD}.config_path", return_value=cfg_path):
            with pytest.raises(ValueError, match="could not be parsed"):
                await mem_mod._write_embed_model_config(str(model), 512)
        assert cfg_path.read_text(encoding="utf-8") == "{ broken"


# ---------------------------------------------------------------------------
# _ensure_pip_available — sandbox refusal + timeout
# ---------------------------------------------------------------------------


class TestEnsurePipEdgeCases:
    @pytest.mark.asyncio
    async def test_sandbox_unavailable_is_a_normal_not_ok_result(self) -> None:
        from kiro_crew.sandbox import SandboxUnavailableError

        def _refuse(argv: Any, **kw: Any) -> Any:
            raise SandboxUnavailableError("no backend", kind="no_backend", detail="not Linux")

        with (
            patch.dict("sys.modules", {"pip": None}),
            patch(f"{_MOD}.wrap_argv", side_effect=_refuse),
            patch(f"{_MOD}.create_subprocess_limited") as spawn,
        ):
            ok, err = await mem_mod._ensure_pip_available()
        assert ok is False
        assert "sandbox" in err
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_reaps_the_child_and_reports_not_ok(self) -> None:
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        async def _timeout(coro: Any, *, timeout: Any = None) -> Any:
            coro.close()
            raise asyncio.TimeoutError

        with (
            patch.dict("sys.modules", {"pip": None}),
            patch(f"{_MOD}.wrap_argv", side_effect=lambda argv, **kw: (argv, None)),
            patch(f"{_MOD}.create_subprocess_limited", AsyncMock(return_value=proc)),
            patch("asyncio.wait_for", side_effect=_timeout),
        ):
            ok, err = await mem_mod._ensure_pip_available()
        assert ok is False
        assert "timed out" in err
        proc.kill.assert_called_once()
        # The critical pin: the reap drains pipes via a SECOND communicate();
        # a bare wait() on a killed child blocked writing into a full stderr
        # pipe would hang the handler forever.
        assert proc.communicate.call_count == 2
        proc.wait.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_temp_wrapper_is_unlinked_even_when_already_gone(self, tmp_path: Path) -> None:
        """The cleanup path tolerates an OSError from a vanished wrapper file."""
        ghost = tmp_path / "wrapper.sh"  # never created
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))

        with (
            patch.dict("sys.modules", {"pip": None}),
            patch(f"{_MOD}.wrap_argv", side_effect=lambda argv, **kw: (argv, str(ghost))),
            patch(f"{_MOD}.create_subprocess_limited", AsyncMock(return_value=proc)),
        ):
            ok, err = await mem_mod._ensure_pip_available()
        assert (ok, err) == (True, "")


# ---------------------------------------------------------------------------
# enable / disable embeddings — guards and the download done-callback
# ---------------------------------------------------------------------------


def _dl_mgr(step: str = "idle", ensure: Any = None) -> Any:
    mgr = MagicMock()
    mgr.status = {"step": step, "error": "download failed", "attempt": 0}
    mgr.ensure_model = ensure if ensure is not None else AsyncMock(return_value=True)
    return mgr


async def _drain(state: Any) -> None:
    """Let the retained background download task finish its callbacks."""
    tasks = list(state.__dict__.get("_bg_embed_tasks", set()))
    for task in tasks:
        try:
            # flake-ok: pre-existing drain of the download task the test cancelled or completed; moved verbatim and tracked for a de-flake pass
            await task
        except BaseException:
            pass
    # flake-ok: pre-existing single loop yield for the done callbacks moved verbatim; tracked for a de-flake pass
    await asyncio.sleep(0)


class TestEnableDisableEmbeddings:
    @pytest.mark.asyncio
    async def test_disable_is_a_deliberate_410(self) -> None:
        resp = await mem_mod.api_memory_disable_embeddings(_make_request(_make_state()))
        assert resp.status == 410
        assert "always-on" in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_concurrent_setup_is_409(self) -> None:
        mem_mod._embedding_setup_status = {"step": "installing_faiss", "error": ""}
        with patch(f"{_MOD}.model_download_manager", return_value=_dl_mgr()):
            resp = await mem_mod.api_memory_enable_embeddings(
                _make_request(_make_state(), method="POST")
            )
        assert resp.status == 409
        assert "installing_faiss" in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_previous_error_is_cleared_before_retry(self) -> None:
        mem_mod._embedding_setup_status = {"step": "error", "error": "boom"}
        state = _make_state()
        with (
            patch(f"{_MOD}.model_download_manager", return_value=_dl_mgr()),
            patch(f"{_MOD}.model_file_present", return_value=False),
        ):
            resp = await mem_mod.api_memory_enable_embeddings(_make_request(state, method="POST"))
            assert resp.status == 200
            await _drain(state)
        assert mem_mod._embedding_setup_status == {"step": "idle", "error": ""}

    @pytest.mark.asyncio
    async def test_download_failure_surfaces_as_failed_status(self) -> None:
        state = _make_state()
        mgr = _dl_mgr(ensure=AsyncMock(return_value=False))
        with (
            patch(f"{_MOD}.model_download_manager", return_value=mgr),
            patch(f"{_MOD}.model_file_present", return_value=False),
        ):
            resp = await mem_mod.api_memory_enable_embeddings(_make_request(state, method="POST"))
            assert _body(resp) == {"ok": True, "status": "downloading"}
            await _drain(state)
        assert mem_mod._embedding_setup_status["step"] == "failed"
        assert mem_mod._embedding_setup_status["error"] == "download failed"

    @pytest.mark.asyncio
    async def test_download_exception_surfaces_as_failed_status(self) -> None:
        state = _make_state()
        mgr = _dl_mgr(ensure=AsyncMock(side_effect=RuntimeError("no disk space")))
        with (
            patch(f"{_MOD}.model_download_manager", return_value=mgr),
            patch(f"{_MOD}.model_file_present", return_value=False),
        ):
            assert (
                await mem_mod.api_memory_enable_embeddings(_make_request(state, method="POST"))
            ).status == 200
            await _drain(state)
        assert mem_mod._embedding_setup_status["step"] == "failed"
        assert "no disk space" in str(mem_mod._embedding_setup_status["error"])

    @pytest.mark.asyncio
    async def test_download_cancellation_surfaces_as_failed_status(self) -> None:
        state = _make_state()
        started = asyncio.Event()

        async def _never_finishes(**_kw: Any) -> bool:
            started.set()
            # flake-ok: pre-existing download stand-in that never finishes moved verbatim; the test cancels it once it has started
            await asyncio.Event().wait()
            return True

        mgr = _dl_mgr(ensure=_never_finishes)
        with (
            patch(f"{_MOD}.model_download_manager", return_value=mgr),
            patch(f"{_MOD}.model_file_present", return_value=False),
        ):
            assert (
                await mem_mod.api_memory_enable_embeddings(_make_request(state, method="POST"))
            ).status == 200
            # flake-ok: pre-existing wait for the download stand-in to start moved verbatim; tracked for a de-flake pass
            await started.wait()
            for task in list(state.__dict__.get("_bg_embed_tasks", set())):
                task.cancel()
            await _drain(state)
        assert mem_mod._embedding_setup_status == {"step": "failed", "error": "cancelled"}

    @pytest.mark.asyncio
    async def test_unexpected_error_in_download_kick_is_500(self) -> None:
        state = _make_state()
        with (
            patch(f"{_MOD}.model_download_manager", return_value=_dl_mgr()),
            patch(f"{_MOD}.model_file_present", side_effect=RuntimeError("stat exploded")),
        ):
            resp = await mem_mod.api_memory_enable_embeddings(_make_request(state, method="POST"))
        assert resp.status == 500
        assert "Click Enable to retry" in _body(resp)["error"]
        assert mem_mod._embedding_setup_status["step"] == "idle"

    @pytest.mark.asyncio
    async def test_config_unreadable_after_setup_is_500(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{ broken", encoding="utf-8")
        store = _store()
        state = _make_state(vector_store=store)
        with (
            patch(f"{_MOD}.model_download_manager", return_value=_dl_mgr("ready")),
            patch(f"{_MOD}.model_file_present", return_value=True),
            patch(f"{_MOD}.config_path", return_value=cfg_path),
            patch(f"{_MOD}.make_sync_embed_fn", return_value=lambda t: [0.0]),
            patch.dict("sys.modules", {"faiss": MagicMock(), "pip": MagicMock()}),
            patch(f"{_MOD}._get_vector_store", return_value=store),
        ):
            resp = await mem_mod.api_memory_enable_embeddings(_make_request(state, method="POST"))
        assert resp.status == 500
        assert _body(resp)["code"] == "config_unreadable"
        assert mem_mod._embedding_setup_status["step"] == "error"
        assert cfg_path.read_text(encoding="utf-8") == "{ broken"


# ---------------------------------------------------------------------------
# _apply_embedding_model — the worker's rollback protocol, executed
# ---------------------------------------------------------------------------


def _apply_store(previous_dim: int = 512, retargeted: bool = True) -> Any:
    store = MagicMock()
    store._embedding_dim = previous_dim
    store.set_embedding_dim.return_value = retargeted
    store.recorded_embedding_space.return_value = "sig"
    store.recorded_rebuild_generation.return_value = "test-request"
    store.embedding_repair_state.return_value = (False, 7)

    def backfill(**kwargs: Any) -> int:
        store.embedding_repair_state.return_value = (False, 0)
        return 7

    store.backfill_missing_embeddings.side_effect = backfill
    return store


class _ApplyHarness:
    """Patched module surface for one ``_apply_embedding_model`` run."""

    def __init__(
        self,
        *,
        ready: bool = True,
        has_wait_ready: bool = True,
        recorded: str = "sig",
        validate: tuple[Path, str, str] = (Path("/m/new.gguf"), "", ""),
        write_error: Exception | None = None,
        reconcile_error: Exception | None = None,
        serving: bool = False,
    ) -> None:
        # Wraps the real progress object: its snapshot is what the status endpoint serves.
        self.prog = MagicMock(wraps=ReembedProgress())
        self.embedder = MagicMock(model_id="m", dim=1024)
        self.embedder.is_ready.return_value = ready
        if has_wait_ready:
            self.embedder.wait_ready = MagicMock(return_value=ready)
        else:
            del self.embedder.wait_ready
        self.reset = MagicMock()
        self.activate = MagicMock()
        self.install = MagicMock()
        self.candidate = MagicMock()
        self.bundled = MagicMock()
        self.rollback = AsyncMock(return_value=None)
        self.write = AsyncMock(side_effect=write_error, return_value=(self.rollback, False))
        self._recorded = recorded
        self._validate = validate
        self._reconcile_error = reconcile_error
        self._serving = serving
        self._stack: Any = None

    def __enter__(self) -> "_ApplyHarness":
        from contextlib import ExitStack

        self._stack = ExitStack()
        enter = self._stack.enter_context
        enter(patch(f"{_MOD}.reembed_progress", return_value=self.prog))
        enter(patch(f"{_MOD}.validate_custom_model_path", return_value=self._validate))
        enter(patch(f"{_MOD}.install_shared_embedder", self.install))
        enter(patch(f"{_MOD}.build_gated_candidate", return_value=self.candidate))
        enter(patch(f"{_MOD}.build_gated_bundled", return_value=self.bundled))
        enter(patch(f"{_MOD}.get_shared_embedder", return_value=self.embedder))
        enter(patch(f"{_MOD}.reset_shared_embedder", self.reset))
        enter(patch(f"{_MOD}.activate_shared_embedder", self.activate))
        enter(patch(f"{_MOD}.make_sync_embed_fn", return_value=lambda t: [0.0]))
        enter(
            patch(
                f"{_MOD}.reconcile_store_embedding_space",
                side_effect=self._reconcile_error,
            )
        )
        enter(patch(f"{_MOD}.active_embedding_space_signature", return_value="sig"))
        enter(patch(f"{_MOD}.embedding_rebuild_generation", return_value="test-request"))
        enter(patch(f"{_MOD}.validated_cached_vector_stores", return_value=()))
        enter(patch(f"{_MOD}.embedding_backend_serving", return_value=self._serving))
        enter(patch(f"{_MOD}._write_embed_model_config", self.write))
        enter(patch(f"{_MOD}._read_memory_config", return_value={"embed_model_id": "m"}))
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stack.close()


async def _run_apply(store: Any, raw: str) -> None:
    loop = asyncio.get_running_loop()
    await asyncio.to_thread(mem_mod._apply_embedding_model, store, raw, loop)


def _progress(step: str, error: str = "", done: int = 0, total: int = 0) -> dict[str, object]:
    return {"step": step, "done": done, "total": total, "error": error}


class TestApplyEmbeddingModelWorker:
    @pytest.mark.asyncio
    async def test_revalidation_failure_installs_nothing(self) -> None:
        store = _apply_store()
        with _ApplyHarness(validate=(Path("/x"), "sensitive path", "denied")) as h:
            await _run_apply(store, "/x")
        assert h.prog.snapshot() == _progress("failed", "sensitive path")
        h.prog.fail.assert_called_once_with("sensitive path")
        h.install.assert_not_called()
        store.begin_space_change.assert_not_called()

    @pytest.mark.asyncio
    async def test_custom_path_installs_a_gated_candidate(self) -> None:
        store = _apply_store()
        with _ApplyHarness() as h:
            await _run_apply(store, "/m/new.gguf")
        assert h.prog.snapshot() == _progress("done", done=7, total=7)
        h.install.assert_called_once_with(h.candidate)
        store.begin_space_change.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_path_reverts_through_the_bundled_candidate(self) -> None:
        store = _apply_store()
        with _ApplyHarness() as h:
            await _run_apply(store, "")
        assert h.prog.snapshot() == _progress("done", done=7, total=7)
        h.install.assert_called_once_with(h.bundled)
        h.activate.assert_called_once()
        h.prog.finish.assert_called_once_with(7)
        store.backfill_missing_embeddings.assert_called_once()
        h.prog.begin_run.assert_called_once_with(7)
        h.prog.advance.assert_called_once_with(7, 7)

    @pytest.mark.asyncio
    async def test_unhandled_rebuild_request_refuses_activation(self) -> None:
        store = _apply_store()
        store.recorded_rebuild_generation.return_value = "previous-request"
        with _ApplyHarness() as h:
            await _run_apply(store, "")
        h.activate.assert_not_called()
        store.backfill_missing_embeddings.assert_not_called()
        h.rollback.assert_awaited_once()
        h.reset.assert_called_once()
        assert "could not be removed" in h.prog.fail.call_args[0][0]
        assert h.prog.snapshot()["step"] == "failed"
        assert "could not be removed" in str(h.prog.snapshot()["error"])

    @pytest.mark.asyncio
    async def test_load_failure_drops_the_candidate_and_fails(self) -> None:
        store = _apply_store()
        with _ApplyHarness(ready=False) as h:
            await _run_apply(store, "")
        h.reset.assert_called_once()
        assert "did not load" in h.prog.fail.call_args[0][0]
        assert h.prog.snapshot()["step"] == "failed"
        assert "did not load" in str(h.prog.snapshot()["error"])
        h.activate.assert_not_called()

    @pytest.mark.asyncio
    async def test_embedder_without_wait_ready_falls_back_to_is_ready(self) -> None:
        store = _apply_store()
        with _ApplyHarness(has_wait_ready=False) as h:
            await _run_apply(store, "")
        h.embedder.is_ready.assert_called_once()
        h.activate.assert_called_once()
        assert h.prog.snapshot() == _progress("done", done=7, total=7)

    @pytest.mark.asyncio
    async def test_unreconciled_space_rolls_back_and_restores_the_width(self) -> None:
        store = _apply_store(previous_dim=512)
        store.recorded_embedding_space.return_value = "stale-sig"
        with _ApplyHarness() as h:
            await _run_apply(store, "")
        h.reset.assert_called_once()
        # Width restored to the model being restored, not left on the new one.
        assert store.set_embedding_dim.call_args_list[-1][0][0] == 512
        assert "could not be removed" in h.prog.fail.call_args[0][0]
        assert h.prog.snapshot()["step"] == "failed"
        assert "could not be removed" in str(h.prog.snapshot()["error"])
        h.write.assert_awaited_once_with("", 1024)
        h.rollback.assert_awaited_once()
        h.activate.assert_not_called()

    @pytest.mark.asyncio
    async def test_unwritable_config_rolls_back_before_activation(self) -> None:
        store = _apply_store(previous_dim=512)
        with _ApplyHarness(write_error=ValueError("config.json could not be parsed")) as h:
            await _run_apply(store, "")
        h.reset.assert_called_once()
        store.set_embedding_dim.assert_not_called()
        store.backfill_missing_embeddings.assert_not_called()
        h.rollback.assert_not_awaited()
        h.prog.fail.assert_called_once_with("config.json could not be parsed")
        assert h.prog.snapshot() == _progress("failed", "config.json could not be parsed")
        h.activate.assert_not_called()

    @pytest.mark.asyncio
    async def test_unexpected_failure_before_activation_rolls_back(self) -> None:
        store = _apply_store(previous_dim=512)
        with _ApplyHarness(reconcile_error=RuntimeError("disk full")) as h:
            await _run_apply(store, "")
        h.reset.assert_called_once()
        assert store.set_embedding_dim.call_args_list[-1][0][0] == 512
        h.prog.fail.assert_called_once_with("disk full")
        assert h.prog.snapshot() == _progress("failed", "disk full")

    @pytest.mark.asyncio
    async def test_blank_exception_message_falls_back_to_the_class_name(self) -> None:
        store = _apply_store()
        with _ApplyHarness(reconcile_error=RuntimeError()) as h:
            await _run_apply(store, "")
        h.prog.fail.assert_called_once_with("RuntimeError")
        assert h.prog.snapshot() == _progress("failed", "RuntimeError")

    @pytest.mark.asyncio
    async def test_failure_after_activation_keeps_the_serving_model(self) -> None:
        """Once the backend is serving, a later failure must not drop it."""
        store = _apply_store()
        store.backfill_missing_embeddings.side_effect = RuntimeError("backfill died")
        with _ApplyHarness(serving=True) as h:
            await _run_apply(store, "")
        h.activate.assert_called_once()
        h.reset.assert_not_called()
        h.prog.fail.assert_called_once_with("backfill died")
        # The run had started over all 7 rows before the backfill failed.
        assert h.prog.snapshot() == _progress("failed", "backfill died", total=7)

    @pytest.mark.asyncio
    async def test_no_retarget_means_no_width_restore(self) -> None:
        store = _apply_store(previous_dim=1024, retargeted=False)
        with _ApplyHarness(reconcile_error=RuntimeError("boom")) as h:
            await _run_apply(store, "")
        h.reset.assert_called_once()
        # set_embedding_dim was called once (the retarget attempt), never again.
        store.set_embedding_dim.assert_called_once_with(1024)
        assert h.prog.fail.call_args[0][0] == "boom"
        assert h.prog.snapshot() == _progress("failed", "boom")


class TestConfigWritesRunOffTheEventLoop:
    """The config read-modify-write must not run on the gateway loop.

    Reading ``config.json``, parsing it, and writing it back through
    ``write_config_atomically`` (a tmp-file write plus a rename, which can
    fsync) is all synchronous file I/O. Inline it stalls every other session for
    its duration -- the class the repo has been closing site by site.

    The load-bearing property is not merely "off the loop" but **the whole
    transaction on ONE worker**. Offloading only the read would put a suspension
    point between the read and the write-back while the file is unguarded on
    disk, so an external editor or CLI landing in that gap would be silently
    overwritten by a write derived from state nobody re-checked. The gap is zero
    today because the sequence is synchronous, and these tests are what keep it
    zero.

    Every seam here is one BOTH shapes reach -- ``write_config_atomically`` is
    called by the hand-rolled version too -- and each wrapper delegates to the
    real function, so the assertions are about where real work ran rather than
    about which helper happens to be patched.
    """

    def _instrument(self, monkeypatch, tmp_path, initial: str = '{"existing": "kept"}'):
        """Record which thread each half of the transaction runs on.

        Wrapped at ``update_config_locked`` -- the designated locked primitive --
        and at the ``write_config_atomically`` it calls internally, so both the
        read and the write are observed inside the SAME critical section. Both
        wrappers delegate to the real function, so the file really is written.
        """
        import threading

        from kiro_crew.config import loader as loader_mod

        cfg = tmp_path / "config.json"
        cfg.write_text(initial, encoding="utf-8")

        seen: dict[str, Any] = {"read": [], "write": [], "data": None}
        real_locked = mem_mod.update_config_locked
        real_write = loader_mod.write_config_atomically

        def _locked(path=None, **kwargs):
            seen["read"].append(threading.get_ident())
            return real_locked(path, **kwargs)

        def _write(path, data, **kwargs):
            seen["write"].append(threading.get_ident())
            seen["data"] = data
            return real_write(path, data, **kwargs)

        monkeypatch.setattr(mem_mod, "update_config_locked", _locked)
        monkeypatch.setattr(loader_mod, "write_config_atomically", _write)
        monkeypatch.setattr(mem_mod, "config_path", lambda: cfg)
        return seen, cfg

    @staticmethod
    def _assert_one_worker(seen, loop_thread) -> None:
        assert seen["write"], "the config write never ran"
        assert seen["write"][0] != loop_thread, (
            "the config write ran on the gateway loop, stalling every other "
            "session for a tmp-write plus rename"
        )
        assert seen["read"], "the transaction did not go through update_config_locked"
        assert seen["read"][0] != loop_thread, "the config read ran on the gateway loop"
        assert seen["read"][0] == seen["write"][0], (
            "the read and the write ran on different threads, so the file was "
            "unguarded between them -- an external writer landing in that gap "
            "would be silently overwritten"
        )

    @pytest.mark.asyncio
    async def test_set_migrated_runs_its_whole_transaction_on_one_worker(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        import threading

        seen, cfg = self._instrument(monkeypatch, tmp_path)

        await mem_mod._set_migrated(True)

        self._assert_one_worker(seen, threading.get_ident())
        on_disk = json.loads(cfg.read_text(encoding="utf-8"))
        assert on_disk["memory"]["migrated"] is True
        assert on_disk["existing"] == "kept", "the rest of the config was dropped"

    @pytest.mark.asyncio
    async def test_settings_put_runs_its_whole_transaction_on_one_worker(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        import threading

        seen, cfg = self._instrument(monkeypatch, tmp_path)
        monkeypatch.setattr(mem_mod.KiroCrewConfig, "load", staticmethod(lambda: MagicMock()))

        state = _make_state(consolidator=None)
        req = _make_request(state, method="PUT", json_body={"history_max_days": 30})
        resp = await mem_mod.api_memory_settings(req)

        assert resp.status == 200
        self._assert_one_worker(seen, threading.get_ident())
        on_disk = json.loads(cfg.read_text(encoding="utf-8"))
        assert on_disk["memory"]["history_max_days"] == 30
        assert on_disk["existing"] == "kept"

    @pytest.mark.asyncio
    async def test_embed_model_config_runs_its_whole_transaction_on_one_worker(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        import threading

        seen, cfg = self._instrument(monkeypatch, tmp_path)
        model = tmp_path / "e5.gguf"
        model.write_bytes(b"model weights")

        await mem_mod._write_embed_model_config(str(model), 768)

        self._assert_one_worker(seen, threading.get_ident())
        on_disk = json.loads(cfg.read_text(encoding="utf-8"))
        assert on_disk["memory"]["embed_model_path"] == str(model)
        assert on_disk["memory"]["embedding_dim"] == 768
        assert on_disk["existing"] == "kept"

    # ── fail-closed contracts: preservation, green on both shapes ────────────

    @pytest.mark.asyncio
    async def test_set_migrated_still_refuses_to_write_an_unreadable_config(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A ``{}`` baseline written back would destroy every other setting.

        Driven with a genuinely unparseable file rather than a raising stub, so
        it exercises the same refusal in either shape.
        """
        seen, cfg = self._instrument(monkeypatch, tmp_path, initial="{oops")

        await mem_mod._set_migrated(True)  # must not raise: boot retries next time

        assert seen["write"] == [], "an unreadable config was overwritten anyway"
        assert cfg.read_text(encoding="utf-8") == "{oops", "the file was modified"

    @pytest.mark.asyncio
    async def test_settings_put_still_fails_closed_on_an_unreadable_config(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        seen, cfg = self._instrument(monkeypatch, tmp_path, initial="{oops")
        monkeypatch.setattr(mem_mod.KiroCrewConfig, "load", staticmethod(lambda: MagicMock()))

        state = _make_state(consolidator=None)
        req = _make_request(state, method="PUT", json_body={"history_max_days": 30})
        resp = await mem_mod.api_memory_settings(req)

        assert resp.status == 500
        assert json.loads(resp.body)["code"] == "config_unreadable"
        assert seen["write"] == []
        assert cfg.read_text(encoding="utf-8") == "{oops"

    @pytest.mark.asyncio
    async def test_embed_model_config_still_raises_on_an_unreadable_config(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        seen, cfg = self._instrument(monkeypatch, tmp_path, initial="{oops")
        model = tmp_path / "e5.gguf"
        model.write_bytes(b"model weights")

        with pytest.raises(ValueError, match="could not be parsed"):
            await mem_mod._write_embed_model_config(str(model), 768)

        assert seen["write"] == []
        assert cfg.read_text(encoding="utf-8") == "{oops"

    @pytest.mark.asyncio
    async def test_settings_put_rejects_a_bad_body_without_touching_the_config(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Validation runs ahead of the transaction; a 400 must not write."""
        seen, cfg = self._instrument(monkeypatch, tmp_path)
        monkeypatch.setattr(mem_mod.KiroCrewConfig, "load", staticmethod(lambda: MagicMock()))

        state = _make_state(consolidator=None)
        req = _make_request(state, method="PUT", json_body={"history_max_days": "abc"})
        resp = await mem_mod.api_memory_settings(req)

        assert resp.status == 400
        assert seen["write"] == [], "a rejected body still wrote the config"
        assert cfg.read_text(encoding="utf-8") == '{"existing": "kept"}'

    @pytest.mark.asyncio
    async def test_cancelling_the_caller_does_not_release_the_lock_early(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A cancelled caller must not hand the lock over mid-transaction.

        A thread cannot be cancelled. Without the shield, cancelling this
        coroutine unwinds ``async with _get_config_lock()`` while the worker is
        still inside its read-modify-write, so the next writer enters the
        critical section against a file the previous one is still rewriting and
        lands a config derived from a pre-write snapshot -- the first caller's
        settings silently revert.

        Ordering is forced with events, never slept for: the worker parks inside
        the transaction, the caller is cancelled while it is parked, and a second
        writer then tries to take the lock. It must not get it until the first
        worker has finished.
        """
        import threading

        from kiro_crew.dashboard.chat_utils import run_config_write

        in_txn = threading.Event()
        finish = threading.Event()
        order: list[str] = []

        def _slow_writer():
            order.append("worker-start")
            in_txn.set()
            finish.wait(timeout=10)
            order.append("worker-end")

        async def _second_writer():
            order.append("second-waiting")
            await run_config_write(lambda: order.append("second-ran"))

        first = asyncio.create_task(run_config_write(_slow_writer))
        assert await asyncio.to_thread(in_txn.wait, 10), "the worker never entered"

        first.cancel()
        second = asyncio.create_task(_second_writer())
        # Yield generously rather than sleeping: if the lock had been released
        # the second writer would have run by now.
        # flake-ok: pre-existing bounded run of loop yields moved verbatim; a released lock lets the second writer run within them
        for _ in range(200):
            await asyncio.sleep(0)

        assert "second-ran" not in order, (
            "the config lock was handed to the next writer while the cancelled "
            "caller's worker was still inside the transaction: %r" % (order,)
        )

        finish.set()
        with contextlib.suppress(asyncio.CancelledError):
            # flake-ok: pre-existing await of the cancelled writer after its worker is released; moved verbatim and tracked for a de-flake pass
            await first
        # flake-ok: pre-existing 10 s lost-run bound on the second writer moved verbatim; its expiry fails the test at this line
        await await_until(second, what="the second writer running after the release")

        assert order.index("worker-end") < order.index(
            "second-ran"
        ), "the second writer entered before the first worker finished: %r" % (order,)
        assert first.cancelled(), "the cancellation must be re-raised, never swallowed"

    @pytest.mark.asyncio
    async def test_an_empty_settings_body_does_not_crash_on_a_non_object_section(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """``{"memory": []}`` plus ``PUT {}`` stays a successful no-op.

        ``read_config_for_update`` validates only that the TOP level is an
        object, so a hand-edited ``memory`` that is a list reaches the mutate.
        Reaching into it unconditionally calls ``list.update`` -> AttributeError
        -> 500, where the pre-existing contract answered 200 and changed nothing.

        Scope note: a NON-empty update against the same malformed section still
        raises, exactly as it did before this PR. That is pre-existing and is
        deliberately not papered over here -- fixing it would hide which
        behaviour this change is responsible for.
        """
        seen, cfg = self._instrument(monkeypatch, tmp_path, initial='{"memory": []}')
        monkeypatch.setattr(mem_mod.KiroCrewConfig, "load", staticmethod(lambda: MagicMock()))

        state = _make_state(consolidator=None)
        req = _make_request(state, method="PUT", json_body={})
        resp = await mem_mod.api_memory_settings(req)

        assert (
            resp.status == 200
        ), "an empty body against a non-object memory section stopped being a " "no-op: %r" % (
            resp.body,
        )
        assert seen["write"] == [], "a no-op PUT rewrote the config"
        assert json.loads(cfg.read_text(encoding="utf-8")) == {
            "memory": []
        }, "the malformed section was rewritten"
