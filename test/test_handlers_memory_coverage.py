"""Coverage tests for ``kiro_crew.dashboard.handlers.memory``.

Focused on the request/response contract of the memory HTTP handlers: method
dispatch, body and query validation, the restricted-session 403 gate, the
not-found 404s, the single-flight 409s, and the fail-closed 500s. Handlers are
invoked directly with a faked ``web.Request`` (the style ``test_memory_graph.py``
uses) so no socket is bound; the vector store, memory store, embedder and
download manager are all mocks, so nothing here touches the network, a real
GGUF, or a sandbox.

``_apply_embedding_model`` is deliberately NOT exercised here — it is owned by
``test_embedding_model_apply.py``.

This file pins the preferences, projects and history documents, the settings route and
the config lock it writes under, value redaction and vector-store resolution, and the
semantic, events and episodic routes. ``test_handlers_memory_coverage_maintenance.py``
pins stats, migrate and import, the context preview and observability, promote,
consolidate and the graph error path, ``test_handlers_memory_coverage_request_contract.py``
the session gate and the non-object-body 400, and
``test_handlers_memory_coverage_embeddings.py`` the embedding-model routes, the pip
bootstrap, the apply worker's install and rollback, and the config writes that run off
the event loop.
``handlers_memory_coverage_helpers.py`` holds the builders and fixtures they share.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from handlers_memory_coverage_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _BadJSON,
    _body,
    _cfg,
    _fake_sel,
    _make_request,
    _make_state,
    _reset_setup_status,
    _store,
)

import kiro_crew.dashboard.handlers.memory as mem_mod
from kiro_crew.testing.wait import await_until

_MOD = "kiro_crew.dashboard.handlers.memory"

_CRED = "AKIAIOSFODNN7EXAMPLE"


# ---------------------------------------------------------------------------
# preferences / projects / history — GET + PUT + malformed body
# ---------------------------------------------------------------------------


class TestPreferencesProjectsHistory:
    @pytest.mark.asyncio
    async def test_preferences_get_returns_stored_content(self) -> None:
        mem = MagicMock()
        mem.read_preferences.return_value = "- dark mode"
        state = _make_state(memory=mem)
        resp = await mem_mod.api_memory_preferences(_make_request(state))
        assert resp.status == 200
        assert _body(resp) == {"content": "- dark mode", "content_redacted": False}

    @pytest.mark.asyncio
    async def test_preferences_put_writes_content(self) -> None:
        mem = MagicMock()
        mem.read_preferences.return_value = "- before"
        mem.write_preferences.return_value = True
        state = _make_state(memory=mem)
        req = _make_request(
            state, method="PUT", json_body={"content": "- new"}, session_key="dashboard:ui"
        )
        resp = await mem_mod.api_memory_preferences(req)
        assert _body(resp) == {"ok": True}
        mem.write_preferences.assert_called_once_with("- new", expected_baseline="- before")

    @pytest.mark.asyncio
    async def test_preferences_put_defaults_missing_content_to_empty(self) -> None:
        mem = MagicMock()
        mem.read_preferences.return_value = "- before"
        mem.write_preferences.return_value = True
        state = _make_state(memory=mem)
        req = _make_request(state, method="PUT", json_body={}, session_key="dashboard:ui")
        assert (await mem_mod.api_memory_preferences(req)).status == 200
        mem.write_preferences.assert_called_once_with("", expected_baseline="- before")

    @pytest.mark.asyncio
    async def test_preferences_put_rejects_invalid_json(self) -> None:
        mem = MagicMock()
        state = _make_state(memory=mem)
        req = _make_request(state, method="PUT", json_body=_BadJSON(), session_key="dashboard:ui")
        resp = await mem_mod.api_memory_preferences(req)
        assert resp.status == 400
        assert _body(resp) == {"error": "invalid JSON", "code": "invalid_json"}
        mem.write_preferences.assert_not_called()

    @pytest.mark.asyncio
    async def test_preferences_put_write_runs_off_the_event_loop_thread(self) -> None:
        """The atomic write is synchronous file I/O; run inline it stalls every
        other gateway task on a slow filesystem, so the handler must offload it."""
        import threading

        loop_thread = threading.get_ident()
        write_threads: list[int] = []
        mem = MagicMock()
        mem.read_preferences.return_value = "before"
        mem.write_preferences.side_effect = lambda _c, **_kw: (
            write_threads.append(threading.get_ident()) or True
        )
        state = _make_state(memory=mem)
        req = _make_request(
            state, method="PUT", json_body={"content": "- new"}, session_key="dashboard:ui"
        )
        resp = await mem_mod.api_memory_preferences(req)
        assert resp.status == 200
        assert write_threads and write_threads[0] != loop_thread

    @pytest.mark.asyncio
    async def test_projects_put_write_runs_off_the_event_loop_thread(self) -> None:
        import threading

        loop_thread = threading.get_ident()
        write_threads: list[int] = []
        mem = MagicMock()
        mem.read_projects.return_value = "before"
        mem.write_projects.side_effect = lambda _c, **_kw: (
            write_threads.append(threading.get_ident()) or True
        )
        state = _make_state(memory=mem)
        req = _make_request(
            state, method="PUT", json_body={"content": "## P"}, session_key="dashboard:ui"
        )
        resp = await mem_mod.api_memory_projects(req)
        assert resp.status == 200
        assert write_threads and write_threads[0] != loop_thread

    @pytest.mark.asyncio
    async def test_projects_get_returns_stored_content(self) -> None:
        mem = MagicMock()
        mem.read_projects.return_value = "## Proj"
        state = _make_state(memory=mem)
        resp = await mem_mod.api_memory_projects(_make_request(state))
        assert _body(resp) == {"content": "## Proj", "content_redacted": False}

    @pytest.mark.asyncio
    async def test_projects_put_writes_content(self) -> None:
        mem = MagicMock()
        mem.read_projects.return_value = "## Before"
        mem.write_projects.return_value = True
        state = _make_state(memory=mem)
        req = _make_request(
            state, method="PUT", json_body={"content": "## New"}, session_key="dashboard:ui"
        )
        assert (await mem_mod.api_memory_projects(req)).status == 200
        mem.write_projects.assert_called_once_with("## New", expected_baseline="## Before")

    @pytest.mark.asyncio
    async def test_projects_put_over_the_startup_cap_says_so(self) -> None:
        """The file keeps every byte, but sessions load only the head: tell the user."""
        from kiro_crew.context_assembly.budget import _MEMORY_PROJECTS_CAP

        mem = MagicMock()
        mem.read_projects.return_value = "## Before"
        mem.write_projects.return_value = True
        state = _make_state(memory=mem)
        content = "# Active Projects\n" + "p" * _MEMORY_PROJECTS_CAP
        req = _make_request(
            state, method="PUT", json_body={"content": content}, session_key="dashboard:ui"
        )
        resp = await mem_mod.api_memory_projects(req)
        body = _body(resp)
        assert resp.status == 200 and body["ok"] is True
        assert body["overflow_chars"] == len(content) + 1 - _MEMORY_PROJECTS_CAP
        assert f"{body['overflow_chars']} chars" in body["warning"]

    @pytest.mark.asyncio
    async def test_projects_put_within_the_cap_has_no_warning(self) -> None:
        mem = MagicMock()
        mem.read_projects.return_value = "## Before"
        mem.write_projects.return_value = True
        state = _make_state(memory=mem)
        req = _make_request(
            state, method="PUT", json_body={"content": "## New"}, session_key="dashboard:ui"
        )
        assert _body(await mem_mod.api_memory_projects(req)) == {"ok": True}

    @pytest.mark.asyncio
    async def test_projects_put_rejects_invalid_json(self) -> None:
        mem = MagicMock()
        state = _make_state(memory=mem)
        req = _make_request(state, method="PUT", json_body=_BadJSON(), session_key="dashboard:ui")
        resp = await mem_mod.api_memory_projects(req)
        assert resp.status == 400
        mem.write_projects.assert_not_called()

    @pytest.mark.asyncio
    async def test_history_get_returns_recent(self) -> None:
        mem = MagicMock()
        mem.read_editable_history.return_value = "# 2026-01-01"
        state = _make_state(memory=mem)
        resp = await mem_mod.api_memory_history(_make_request(state))
        assert _body(resp) == {"content": "# 2026-01-01", "content_redacted": False}

    @pytest.mark.asyncio
    async def test_history_put_writes_today_file_creating_parents(self, tmp_path: Path) -> None:
        target = tmp_path / "history" / "2026-01-01.md"
        mem = MagicMock()
        mem._today_history_file.return_value = target
        # The handler routes through the store's lock-owning, atomic writer.
        mem.read_editable_history.return_value = "before"
        mem.write_today_history.side_effect = lambda c, **_kw: (
            target.parent.mkdir(parents=True, exist_ok=True),
            target.write_text(c, encoding="utf-8"),
            True,
        )[-1]
        state = _make_state(memory=mem)
        req = _make_request(
            state, method="PUT", json_body={"content": "entry"}, session_key="dashboard:ui"
        )
        assert (await mem_mod.api_memory_history(req)).status == 200
        mem.write_today_history.assert_called_once_with(
            "entry",
            expected_baseline="before",
            validate_current=mem_mod._require_editable_memory_document,
        )
        assert target.read_text(encoding="utf-8") == "entry"

    @pytest.mark.asyncio
    async def test_history_put_rejects_invalid_json(self, tmp_path: Path) -> None:
        mem = MagicMock()
        mem._today_history_file.return_value = tmp_path / "h.md"
        state = _make_state(memory=mem)
        req = _make_request(state, method="PUT", json_body=_BadJSON(), session_key="dashboard:ui")
        resp = await mem_mod.api_memory_history(req)
        assert resp.status == 400
        assert not (tmp_path / "h.md").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("content", [None, False, 42, 1.5, [], {}])
    async def test_history_put_rejects_non_string_without_changing_history(
        self, tmp_path: Path, content: Any
    ) -> None:
        target = tmp_path / "history.md"
        target.write_text("existing history", encoding="utf-8")
        mem = MagicMock()
        mem._today_history_file.return_value = target
        state = _make_state(memory=mem)
        request = _make_request(state, method="PUT", json_body={"content": content})

        response = await mem_mod.api_memory_history(request)

        assert response.status == 400
        assert _body(response)["code"] == "invalid_memory_content"
        mem._today_history_file.assert_not_called()
        mem._atomic_write_text.assert_not_called()
        assert target.read_text(encoding="utf-8") == "existing history"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,reader",
        [
            ("api_memory_preferences", "read_preferences"),
            ("api_memory_projects", "read_projects"),
            ("api_memory_history", "read_editable_history"),
        ],
    )
    async def test_document_get_redacts_sensitive_content_without_writing(
        self, handler: str, reader: str
    ) -> None:
        raw = f"keep this {_CRED} unchanged"
        mem = MagicMock()
        getattr(mem, reader).return_value = raw

        response = await getattr(mem_mod, handler)(_make_request(_make_state(memory=mem)))

        body = _body(response)
        assert response.status == 200
        assert body["content_redacted"] is True
        assert _CRED not in body["content"]
        getattr(mem, reader).assert_called_once_with()
        mem.write_preferences.assert_not_called()
        mem.write_projects.assert_not_called()
        mem.write_today_history.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,reader,writer",
        [
            ("api_memory_preferences", "read_preferences", "write_preferences"),
            ("api_memory_projects", "read_projects", "write_projects"),
            ("api_memory_history", "read_editable_history", "write_today_history"),
        ],
    )
    async def test_document_put_refuses_to_overwrite_hidden_sensitive_content(
        self, handler: str, reader: str, writer: str
    ) -> None:
        mem = MagicMock()
        getattr(mem, reader).return_value = f"keep {_CRED} exactly"
        request = _make_request(
            _make_state(memory=mem), method="PUT", json_body={"content": "unrelated edit"}
        )

        response = await getattr(mem_mod, handler)(request)

        assert response.status == 409
        assert _body(response)["code"] == "memory_document_redacted"
        getattr(mem, writer).assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,reader,writer",
        [
            ("api_memory_preferences", "read_preferences", "write_preferences"),
            ("api_memory_projects", "read_projects", "write_projects"),
            ("api_memory_history", "read_editable_history", "write_today_history"),
        ],
    )
    async def test_document_put_reports_a_concurrent_change_without_replacing_it(
        self, handler: str, reader: str, writer: str
    ) -> None:
        mem = MagicMock()
        getattr(mem, reader).return_value = "clean baseline"
        getattr(mem, writer).return_value = False
        request = _make_request(
            _make_state(memory=mem), method="PUT", json_body={"content": "owner edit"}
        )

        response = await getattr(mem_mod, handler)(request)

        assert response.status == 409
        assert _body(response)["code"] == "memory_document_changed"
        expected = {"expected_baseline": "clean baseline"}
        if handler == "api_memory_history":
            expected["validate_current"] = mem_mod._require_editable_memory_document
        getattr(mem, writer).assert_called_once_with("owner edit", **expected)


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


class TestRunConfigWriteCancellation:
    """``run_config_write`` must not hand the config lock on mid-write.

    The worker is a thread and cannot be cancelled, so the only question is
    whether the lock outlives it. Ordering is forced with events, never slept
    for.
    """

    @staticmethod
    def _run(coro):
        return asyncio.run(coro)

    def test_a_repeated_cancellation_still_drains_before_unlocking(self):
        """A SECOND cancel while draining must not release the lock either.

        Awaiting the drain is itself a suspension point. A graceful shutdown
        that escalates after its timeout cancels twice -- and that is exactly
        when a config write is most likely to be in flight -- so a drain that
        absorbs only the first cancellation unwinds the ``async with`` with
        the thread still inside its read-modify-write.

        The assertion is on ORDER, not on the number of cancels: the second
        writer must not enter the critical section until the worker returns.
        """
        from kiro_crew.dashboard.chat_utils import run_config_write
        from kiro_crew.dashboard.handlers.agents import _get_config_lock

        in_write = threading.Event()
        finish = threading.Event()
        order: list[str] = []

        def _slow_write(*_a, **_k):
            order.append("worker-start")
            in_write.set()
            finish.wait(timeout=10)
            order.append("worker-end")

        async def _first():
            # `run_config_write` acquires the lock itself; wrapping the call in a
            # second acquire would deadlock on a non-reentrant asyncio.Lock.
            await run_config_write(_slow_write)

        async def _second():
            async with _get_config_lock():
                order.append("second-ran")

        async def _drive():
            first = asyncio.create_task(_first())
            assert await asyncio.to_thread(in_write.wait, 10), "worker never entered"

            # Cancel TWICE: the second lands while the drain is awaiting.
            first.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            first.cancel()

            second = asyncio.create_task(_second())
            # Yield generously rather than sleeping: had the lock been released,
            # the second writer would have run by now.
            for _ in range(200):
                await asyncio.sleep(0)
            early = "second-ran" in order

            finish.set()
            cancelled = False
            try:
                await first
            except asyncio.CancelledError:
                cancelled = True
            await await_until(second, what="the second writer running after the release")
            return early, cancelled, list(order)

        early, cancelled, seen = self._run(_drive())

        assert not early, (
            "the config lock was handed to the next writer while the twice-cancelled "
            "one's worker was still writing: %r" % (seen,)
        )
        assert seen.index("worker-end") < seen.index(
            "second-ran"
        ), "the second writer entered before the worker finished: %r" % (seen,)
        assert cancelled, "the cancellation must propagate, not be swallowed"

    def test_an_uncancelled_call_returns_the_worker_result(self):
        """The loop must not change the ordinary path: the value still comes back."""
        from kiro_crew.dashboard.chat_utils import run_config_write

        async def _drive():
            return await run_config_write(lambda a, b=0: a + b, 40, b=2)

        assert self._run(_drive()) == 42


class TestMemorySettings:
    @pytest.mark.asyncio
    async def test_get_reports_config_values(self) -> None:
        state = _make_state()
        with patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg(2.5, 14, True)):
            resp = await mem_mod.api_memory_settings(_make_request(state))
        assert _body(resp) == {
            "history_idle_hours": 2.5,
            "history_max_days": 14,
            "migrated": True,
        }

    @pytest.mark.asyncio
    async def test_put_rejects_invalid_json(self, tmp_path: Path) -> None:
        state = _make_state()
        req = _make_request(state, method="PUT", json_body=_BadJSON())
        with (
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()),
            patch(f"{_MOD}.config_path", return_value=tmp_path / "config.json"),
        ):
            resp = await mem_mod.api_memory_settings(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_clamps_and_persists(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"agent": {"provider": "acp"}}), encoding="utf-8")
        state = _make_state()
        state.consolidator = None
        req = _make_request(
            state,
            method="PUT",
            json_body={"history_idle_hours": 0.1, "history_max_days": 1, "migrated": 1},
        )
        with (
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()),
            patch(f"{_MOD}.config_path", return_value=cfg_path),
        ):
            resp = await mem_mod.api_memory_settings(req)
        assert _body(resp) == {"ok": True}
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        # Floors applied: 0.5 hours minimum, 7 days minimum.
        assert data["memory"]["history_idle_hours"] == 0.5
        assert data["memory"]["history_max_days"] == 7
        assert data["memory"]["migrated"] is True
        # Unrelated sections survive the read-modify-write.
        assert data["agent"]["provider"] == "acp"

    @pytest.mark.asyncio
    async def test_put_rejects_non_numeric_idle_hours(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        state = _make_state()
        req = _make_request(state, method="PUT", json_body={"history_idle_hours": "soon"})
        with (
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()),
            patch(f"{_MOD}.config_path", return_value=cfg_path),
        ):
            resp = await mem_mod.api_memory_settings(req)
        assert resp.status == 400
        assert "history_idle_hours" in _body(resp)["error"]
        assert not cfg_path.exists()

    @pytest.mark.asyncio
    async def test_put_rejects_non_integer_max_days(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        state = _make_state()
        req = _make_request(state, method="PUT", json_body={"history_max_days": "many"})
        with (
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()),
            patch(f"{_MOD}.config_path", return_value=cfg_path),
        ):
            resp = await mem_mod.api_memory_settings(req)
        assert resp.status == 400
        assert "history_max_days" in _body(resp)["error"]

    @pytest.mark.parametrize("raw", ["1e999", "-1e999"])
    @pytest.mark.asyncio
    async def test_put_rejects_infinite_max_days(self, tmp_path: Path, raw: str) -> None:
        """JSON reads ``1e999`` as an infinite float: the same 400, never a 500."""
        cfg_path = tmp_path / "config.json"
        state = _make_state()
        body = json.loads(f'{{"history_max_days": {raw}}}')
        req = _make_request(state, method="PUT", json_body=body)
        with (
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()),
            patch(f"{_MOD}.config_path", return_value=cfg_path),
        ):
            resp = await mem_mod.api_memory_settings(req)
        assert resp.status == 400
        assert _body(resp) == {"error": "history_max_days must be an integer"}
        assert not cfg_path.exists()

    @pytest.mark.asyncio
    async def test_put_fails_closed_on_unreadable_config(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{ not json", encoding="utf-8")
        state = _make_state()
        req = _make_request(state, method="PUT", json_body={"migrated": True})
        with (
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()),
            patch(f"{_MOD}.config_path", return_value=cfg_path),
        ):
            resp = await mem_mod.api_memory_settings(req)
        assert resp.status == 500
        assert _body(resp)["code"] == "config_unreadable"
        # The user's file is left byte-for-byte intact.
        assert cfg_path.read_text(encoding="utf-8") == "{ not json"

    @pytest.mark.asyncio
    async def test_put_applies_to_running_consolidator(self, tmp_path: Path) -> None:
        # The route pushes the reloaded config through the same ``reconfigure``
        # seam the config watcher uses, so the answer is in force when it returns.
        cfg_path = tmp_path / "config.json"
        consolidator = MagicMock()
        state = _make_state(consolidator=consolidator)
        req = _make_request(state, method="PUT", json_body={"history_idle_hours": 3})
        reloaded = _cfg(idle=3.0, migrated=True)
        with (
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=reloaded),
            patch(f"{_MOD}.config_path", return_value=cfg_path),
        ):
            assert (await mem_mod.api_memory_settings(req)).status == 200
        consolidator.reconfigure.assert_called_once_with(reloaded)


# ---------------------------------------------------------------------------
# _redact_memory_field / _get_vector_store
# ---------------------------------------------------------------------------


class TestRedactAndStoreResolution:
    def test_bytes_are_dropped_not_serialized(self) -> None:
        assert mem_mod._redact_memory_field(b"\x00\x01") is None
        assert mem_mod._redact_memory_field(memoryview(b"ab")) is None

    def test_nested_containers_are_redacted(self) -> None:
        out = mem_mod._redact_memory_field({"a": [{"b": _CRED}], "n": 3})
        assert isinstance(out, dict)
        assert _CRED not in json.dumps(out)
        assert out["n"] == 3

    def test_non_string_scalars_pass_through(self) -> None:
        assert mem_mod._redact_memory_field(None) is None
        assert mem_mod._redact_memory_field(True) is True
        assert mem_mod._redact_memory_field(1.5) == 1.5

    def test_existing_vector_store_is_reused(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        assert mem_mod._get_vector_store(state) is store

    def test_standalone_store_created_and_cached_when_absent(self) -> None:
        mem = SimpleNamespace(vector_store=None)
        state: Any = SimpleNamespace(context_builder=SimpleNamespace(memory=mem))
        created = MagicMock()
        with (
            patch("kiro_crew.vector_memory.VectorMemoryStore", return_value=created) as ctor,
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()),
        ):
            first = mem_mod._get_vector_store(state)
            second = mem_mod._get_vector_store(state)
        assert first is created
        # Cached on state AND wired back onto the memory store, so only one build.
        assert second is created
        ctor.assert_called_once()
        created.init.assert_called_once()
        assert mem.vector_store is created


# ---------------------------------------------------------------------------
# _get_vector_store_async — the standalone fallback's ``init()`` must
# never run on the event loop (VectorMemoryStore's caller contract: init is
# blocking file IO whose Windows DACL writes can block on a network volume
# round-trip and would freeze the loop).
# ---------------------------------------------------------------------------


class TestGetVectorStoreAsync:
    @staticmethod
    def _fallback_state() -> Any:
        """A state whose only resolution path is the standalone fallback."""
        mem = SimpleNamespace(vector_store=None)
        return SimpleNamespace(context_builder=SimpleNamespace(memory=mem))

    @pytest.mark.asyncio
    async def test_standalone_fallback_init_runs_off_event_loop(self, monkeypatch) -> None:
        """Fail-before: with the sync call reinstated, init runs on this thread."""
        import kiro_crew.vector_memory as vm_mod

        init_threads: list[threading.Thread] = []

        class RecordingStore:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def init(self) -> None:
                init_threads.append(threading.current_thread())

        monkeypatch.setattr(vm_mod, "VectorMemoryStore", RecordingStore)
        state = self._fallback_state()
        with patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()):
            store = await mem_mod._get_vector_store_async(state)

            assert isinstance(store, RecordingStore)
            assert len(init_threads) == 1
            assert init_threads[0] is not threading.current_thread()

            # Cached: the second call takes the sync fast path — same store,
            # no second ``init()``, and no thread hop at all.
            with patch(f"{_MOD}.asyncio.to_thread", new_callable=AsyncMock) as hop:
                assert await mem_mod._get_vector_store_async(state) is store
            hop.assert_not_awaited()
        assert len(init_threads) == 1

    @pytest.mark.asyncio
    async def test_cancelled_first_caller_does_not_double_init(self, monkeypatch) -> None:
        """Cancelling the first caller (e.g. an aiohttp client disconnect)
        must not let a racing request arm a second ``init()``: the shared
        shielded task keeps the in-flight init as the single flight."""
        import kiro_crew.vector_memory as vm_mod

        init_calls: list[threading.Thread] = []
        started = threading.Event()
        release = threading.Event()

        class SlowStore:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def init(self) -> None:
                init_calls.append(threading.current_thread())
                started.set()
                release.wait(timeout=5)

        monkeypatch.setattr(vm_mod, "VectorMemoryStore", SlowStore)
        state = self._fallback_state()
        with patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()):
            first = asyncio.ensure_future(mem_mod._get_vector_store_async(state))
            # Wait until init() is genuinely running in the worker thread,
            # then cancel the caller mid-init.
            await asyncio.to_thread(started.wait, 5)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            # A request landing in the cancellation window must join the
            # orphaned-but-alive init, not start a second one.
            second = asyncio.ensure_future(mem_mod._get_vector_store_async(state))
            await asyncio.sleep(0.05)
            release.set()
            store = await second
        assert isinstance(store, SlowStore)
        assert len(init_calls) == 1

    @pytest.mark.asyncio
    async def test_concurrent_first_calls_init_once(self, monkeypatch) -> None:
        """Single-flight: the lock restores the serialization the sync call
        sites get for free from the event loop."""
        import kiro_crew.vector_memory as vm_mod

        init_calls: list[threading.Thread] = []
        release = threading.Event()

        class SlowStore:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def init(self) -> None:
                init_calls.append(threading.current_thread())
                release.wait(timeout=5)

        monkeypatch.setattr(vm_mod, "VectorMemoryStore", SlowStore)
        state = self._fallback_state()
        with patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()):
            t1 = asyncio.ensure_future(mem_mod._get_vector_store_async(state))
            t2 = asyncio.ensure_future(mem_mod._get_vector_store_async(state))
            # Let both tasks pass the fast-path check and reach the lock while
            # the first ``init()`` is still blocked in its worker thread.
            await asyncio.sleep(0.05)
            release.set()
            first, second = await asyncio.gather(t1, t2)
        assert first is second
        assert len(init_calls) == 1

    @pytest.mark.asyncio
    async def test_supplied_store_fast_path_pays_no_thread_hop(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        with patch(f"{_MOD}.asyncio.to_thread", new_callable=AsyncMock) as hop:
            assert await mem_mod._get_vector_store_async(state) is store
        hop.assert_not_awaited()

    def test_no_async_handler_calls_sync_get_vector_store(self) -> None:
        """Tripwire: every ``async def`` in the handler module and its owners must
        route through ``_get_vector_store_async`` (the one place allowed to call the
        sync helper, because it offloads the init-bearing path)."""
        home = Path(mem_mod.__file__).parent
        owners = [home / f"{name.split('.')[-1]}.py" for name in mem_mod._OWNERS]
        trees = [ast.parse(inspect.getsource(mem_mod))]
        trees += [ast.parse(path.read_text(encoding="utf-8")) for path in owners]
        offenders: list[str] = []
        for node in (node for tree in trees for node in ast.walk(tree)):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            if node.name == "_get_vector_store_async":
                continue
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "_get_vector_store"
                ):
                    offenders.append(f"{node.name}:{call.lineno}")
        assert not offenders, (
            "async handlers must await _get_vector_store_async(...) instead of "
            f"calling the sync helper on the event loop (#5221): {offenders}"
        )


# ---------------------------------------------------------------------------
# semantic list / write / delete, events
# ---------------------------------------------------------------------------


class TestSemanticEndpoints:
    @pytest.mark.asyncio
    async def test_list_caps_limit_at_1000(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"limit": "99999", "offset": "7"})
        assert (await mem_mod.api_memory_semantic(req)).status == 200
        store.get_all_semantic.assert_called_once_with(limit=1000, offset=7)

    @pytest.mark.asyncio
    async def test_list_rejects_non_integer_paging(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"limit": "abc"})
        resp = await mem_mod.api_memory_semantic(req)
        assert resp.status == 400
        assert "integers" in _body(resp)["error"]
        store.get_all_semantic.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_drops_binary_columns(self) -> None:
        store = _store()
        store.get_all_semantic.return_value = [
            {"key": "k", "value_json": "v", "embedding": b"\x00\x01"}
        ]
        state = _make_state(vector_store=store)
        entry = _body(await mem_mod.api_memory_semantic(_make_request(state)))["entries"][0]
        assert "embedding" not in entry

    @pytest.mark.asyncio
    async def test_write_denied_for_restricted_session(self) -> None:
        store = _store()
        state = _make_state(vector_store=store, restricted_keys={"dashboard:ghost"})
        req = _make_request(
            state,
            method="PUT",
            json_body={"key": "k", "value": "v"},
            session_key="dashboard:ghost",
        )
        resp = await mem_mod.api_memory_semantic_write(req)
        assert resp.status == 403
        store.set_semantic.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_rejects_invalid_json(self) -> None:
        state = _make_state(vector_store=_store())
        req = _make_request(state, method="PUT", json_body=_BadJSON(), session_key="dashboard:ui")
        assert (await mem_mod.api_memory_semantic_write(req)).status == 400

    @pytest.mark.asyncio
    async def test_write_requires_key_and_value(self) -> None:
        state = _make_state(vector_store=_store())
        for body in ({"value": "v"}, {"key": "k"}, {}):
            req = _make_request(state, method="PUT", json_body=body, session_key="dashboard:ui")
            resp = await mem_mod.api_memory_semantic_write(req)
            assert resp.status == 400
            assert _body(resp)["error"] == "key and value required"

    @pytest.mark.asyncio
    async def test_write_success_forwards_confidence_and_source(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(
            state,
            method="PUT",
            json_body={"key": "k", "value": "v", "confidence": 0.5, "source": "agent"},
            session_key="dashboard:ui",
        )
        assert _body(await mem_mod.api_memory_semantic_write(req)) == {"ok": True}
        store.set_semantic.assert_called_once_with("k", "v", 0.5, "agent")

    @pytest.mark.asyncio
    async def test_write_non_numeric_confidence_falls_back_to_one(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(
            state,
            method="PUT",
            json_body={"key": "k", "value": "v", "confidence": "high"},
            session_key="dashboard:ui",
        )
        assert (await mem_mod.api_memory_semantic_write(req)).status == 200
        assert store.set_semantic.call_args[0][2] == 1.0

    @pytest.mark.asyncio
    async def test_write_conflict_reject_is_409(self) -> None:
        from kiro_crew.vector_memory import SemanticRejectCode

        store = _store()
        store.set_semantic.return_value = (SemanticRejectCode.CONFLICT, "already set")
        state = _make_state(vector_store=store)
        req = _make_request(
            state,
            method="PUT",
            json_body={"key": "k", "value": "v"},
            session_key="dashboard:ui",
        )
        resp = await mem_mod.api_memory_semantic_write(req)
        assert resp.status == 409
        assert _body(resp)["error"] == "already set"

    @pytest.mark.asyncio
    async def test_write_other_reject_is_422_and_redacted(self) -> None:
        from kiro_crew.vector_memory import SemanticRejectCode

        store = _store()
        store.set_semantic.return_value = (
            SemanticRejectCode.VALUE_SIZE,
            f"value too large for {_CRED}",
        )
        state = _make_state(vector_store=store)
        req = _make_request(
            state,
            method="PUT",
            json_body={"key": "k", "value": "v"},
            session_key="dashboard:ui",
        )
        resp = await mem_mod.api_memory_semantic_write(req)
        assert resp.status == 422
        assert _CRED not in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_delete_denied_for_restricted_session(self) -> None:
        store = _store()
        state = _make_state(vector_store=store, restricted_keys={"dashboard:ghost"})
        req = _make_request(
            state, method="DELETE", match_info={"key": "k"}, session_key="dashboard:ghost"
        )
        assert (await mem_mod.api_memory_semantic_delete(req)).status == 403
        store.delete_semantic.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_missing_key_is_404(self) -> None:
        store = _store(delete_semantic=MagicMock(return_value=False))
        state = _make_state(vector_store=store)
        req = _make_request(
            state, method="DELETE", match_info={"key": "nope"}, session_key="dashboard:ui"
        )
        resp = await mem_mod.api_memory_semantic_delete(req)
        assert resp.status == 404
        assert _body(resp) == {"error": "not found"}

    @pytest.mark.asyncio
    async def test_delete_success(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(
            state, method="DELETE", match_info={"key": "k"}, session_key="dashboard:ui"
        )
        assert _body(await mem_mod.api_memory_semantic_delete(req)) == {"ok": True}
        store.delete_semantic.assert_called_once_with("k", source="user_explicit")

    @pytest.mark.asyncio
    async def test_events_caps_limit_at_200(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"limit": "10000", "offset": "3"})
        assert (await mem_mod.api_memory_events(req)).status == 200
        store.get_events.assert_called_once_with(limit=200, offset=3)

    @pytest.mark.asyncio
    async def test_events_rejects_non_integer_paging(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"offset": "x"})
        assert (await mem_mod.api_memory_events(req)).status == 400
        store.get_events.assert_not_called()


# ---------------------------------------------------------------------------
# episodic endpoints
# ---------------------------------------------------------------------------


class TestEpisodicEndpoints:
    @pytest.mark.asyncio
    async def test_search_without_embed_fn_skips_embedding(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"q": "hello", "tags": "a, b ,"})
        assert (await mem_mod.api_memory_episodic_search(req)).status == 200
        store._try_embed.assert_not_called()
        kwargs = store.search_episodic.call_args.kwargs
        assert kwargs["query_embedding"] is None
        assert kwargs["tag_filter"] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_search_embeds_query_when_embed_fn_present(self) -> None:
        store = _store(embed_fn=lambda t: [0.1])
        store._try_embed.return_value = [0.1]
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"q": "hello"})
        assert (await mem_mod.api_memory_episodic_search(req)).status == 200
        store._try_embed.assert_called_once_with("hello")
        assert store.search_episodic.call_args.kwargs["query_embedding"] == [0.1]

    @pytest.mark.asyncio
    async def test_search_bad_limit_falls_back_to_default(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"q": "x", "limit": "lots"})
        assert (await mem_mod.api_memory_episodic_search(req)).status == 200
        assert store.search_episodic.call_args.kwargs["limit"] == 20

    @pytest.mark.asyncio
    async def test_search_caps_limit_at_50_and_truncates_query(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"q": "z" * 900, "limit": "500"})
        assert (await mem_mod.api_memory_episodic_search(req)).status == 200
        kwargs = store.search_episodic.call_args.kwargs
        assert kwargs["limit"] == 50
        assert len(kwargs["query_text"]) == 500

    @pytest.mark.asyncio
    async def test_search_results_drop_binary_and_redact_strings(self) -> None:
        store = _store()
        store.search_episodic.return_value = [
            {"id": "1", "text": f"saw {_CRED}", "embedding": b"\x00", "score": 0.5}
        ]
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"q": "saw"})
        result = _body(await mem_mod.api_memory_episodic_search(req))["results"][0]
        assert "embedding" not in result
        assert _CRED not in result["text"]
        assert result["score"] == 0.5

    @pytest.mark.asyncio
    async def test_list_entries_are_redacted(self) -> None:
        store = _store()
        store.get_episodic_list.return_value = [{"id": "1", "text": f"key {_CRED}"}]
        state = _make_state(vector_store=store)
        entry = _body(await mem_mod.api_memory_episodic_list(_make_request(state)))["entries"][0]
        assert _CRED not in entry["text"]

    @pytest.mark.asyncio
    async def test_list_caps_limit_at_100(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"limit": "900", "offset": "2", "tags": "t1"})
        assert (await mem_mod.api_memory_episodic_list(req)).status == 200
        store.get_episodic_list.assert_called_once_with(limit=100, offset=2, tag_filter=["t1"])

    @pytest.mark.asyncio
    async def test_list_rejects_non_integer_paging(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"limit": "nope"})
        assert (await mem_mod.api_memory_episodic_list(req)).status == 400
        store.get_episodic_list.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_missing_id_is_404(self) -> None:
        store = _store(delete_episodic=MagicMock(return_value=False))
        state = _make_state(vector_store=store)
        req = _make_request(
            state, method="DELETE", match_info={"id": "42"}, session_key="dashboard:ui"
        )
        resp = await mem_mod.api_memory_episodic_delete(req)
        assert resp.status == 404
        assert _body(resp) == {"error": "not found"}

    @pytest.mark.asyncio
    async def test_delete_success(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(
            state, method="DELETE", match_info={"id": "42"}, session_key="dashboard:ui"
        )
        assert _body(await mem_mod.api_memory_episodic_delete(req)) == {"ok": True}
        store.delete_episodic.assert_called_once_with("42")
