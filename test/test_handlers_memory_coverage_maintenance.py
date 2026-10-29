"""The stats, maintenance and diagnostics routes of the memory dashboard handlers.

This file pins, for ``kiro_crew.dashboard.handlers.memory``, stats, migrate and import, the
context preview and observability, promote, consolidate (including which sessions a
consolidation may target) and the graph error path. The family's style and its other
files are described in ``test_handlers_memory_coverage.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
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

_MOD = "kiro_crew.dashboard.handlers.memory"


# ---------------------------------------------------------------------------
# stats / migrate / import / context-preview / observability / promote
# ---------------------------------------------------------------------------


class TestStatsMigrateImport:
    @pytest.mark.asyncio
    async def test_stats_merges_config_and_legacy_flags(self) -> None:
        store = _store()
        store.memory_stats.return_value = {"semantic_count": 4}
        state = _make_state(vector_store=store)
        cfg = _cfg(migrated=True)
        cfg.memory.embedding_provider = "llama_cpp"
        with (
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.memory.legacy_memory_present", return_value=True),
        ):
            body = _body(await mem_mod.api_memory_stats(_make_request(state)))
        assert body == {
            "semantic_count": 4,
            "embedding_provider": "llama_cpp",
            "migrated": True,
            "has_legacy_memory": True,
        }

    @pytest.mark.asyncio
    async def test_migrate_restores_previous_embed_fn(self, tmp_path: Path) -> None:
        def _prev(text: str) -> list[float]:
            return [0.0]

        store = _store(embed_fn=_prev)
        state = _make_state(vector_store=store, consolidator=None)
        with (
            patch(f"{_MOD}.make_sync_embed_fn", return_value=lambda t: [1.0]),
            patch(f"{_MOD}.config_path", return_value=tmp_path / "config.json"),
        ):
            body = _body(
                await mem_mod.api_memory_migrate(
                    _make_request(state, method="POST", session_key="dashboard:ui")
                )
            )
        assert body == {"semantic": 0, "episodic": 0}
        # Nothing migrated -> migrated flag untouched, embed_fn restored.
        assert store.embed_fn is _prev
        assert not (tmp_path / "config.json").exists()

    @pytest.mark.asyncio
    async def test_migrate_sets_migrated_when_entries_produced(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        store = _store()
        store.migrate_from_markdown.return_value = {"semantic": 3, "episodic": 0}
        consolidator = MagicMock()
        state = _make_state(vector_store=store, consolidator=consolidator)
        with (
            patch(f"{_MOD}.make_sync_embed_fn", return_value=lambda t: [1.0]),
            patch(f"{_MOD}.config_path", return_value=cfg_path),
        ):
            body = _body(
                await mem_mod.api_memory_migrate(
                    _make_request(state, method="POST", session_key="dashboard:ui")
                )
            )
        assert body["semantic"] == 3
        assert json.loads(cfg_path.read_text(encoding="utf-8"))["memory"]["migrated"] is True
        assert consolidator._migrated is True

    @pytest.mark.asyncio
    async def test_import_denied_for_restricted_session(self) -> None:
        store = _store()
        state = _make_state(vector_store=store, restricted_keys={"dashboard:ghost"})
        req = _make_request(
            state, method="POST", json_body={"semantic": []}, session_key="dashboard:ghost"
        )
        assert (await mem_mod.api_memory_import(req)).status == 403
        store.import_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_import_rejects_invalid_json(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, method="POST", json_body=_BadJSON(), session_key="dashboard:ui")
        assert (await mem_mod.api_memory_import(req)).status == 400
        store.import_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_import_returns_counts(self) -> None:
        store = _store()
        store.import_memory.return_value = {"semantic": 2, "episodic": 1}
        state = _make_state(vector_store=store)
        req = _make_request(
            state,
            method="POST",
            json_body={"semantic": [{"key": "k"}]},
            session_key="dashboard:ui",
        )
        assert _body(await mem_mod.api_memory_import(req)) == {"semantic": 2, "episodic": 1}


class TestContextPreviewAndObservability:
    @pytest.mark.asyncio
    async def test_preview_without_query_skips_episodic(self) -> None:
        store = _store()
        store.get_semantic_context.return_value = "line one"
        state = _make_state(vector_store=store)
        body = _body(await mem_mod.api_memory_context_preview(_make_request(state)))
        assert body == {"semantic_context": "line one", "episodic_context": ""}
        store.get_episodic_context.assert_not_called()

    @pytest.mark.asyncio
    async def test_preview_filters_semantic_lines_by_query(self) -> None:
        store = _store()
        store.get_semantic_context.return_value = "[Semantic]\nprefers dark mode\nlikes tea"
        store.get_episodic_context.return_value = "episode"
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"q": "dark"})
        body = _body(await mem_mod.api_memory_context_preview(req))
        assert "prefers dark mode" in body["semantic_context"]
        assert "likes tea" not in body["semantic_context"]
        assert body["episodic_context"] == "episode"

    @pytest.mark.asyncio
    async def test_preview_blanks_semantic_when_only_headers_match(self) -> None:
        store = _store()
        store.get_semantic_context.return_value = "[Semantic]\nlikes tea"
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"q": "zzz-no-match"})
        body = _body(await mem_mod.api_memory_context_preview(req))
        assert body["semantic_context"] == ""

    @pytest.mark.asyncio
    async def test_observability_returns_stats_rejections_and_preview(self) -> None:
        store = _store()
        store.memory_stats.return_value = {"semantic_count": 1}
        store.get_rejection_stats.return_value = {"allowlist_reject": 2}
        store.get_context_preview.return_value = {"semantic": "s"}
        store.read_counters.return_value = {"semantic_full_scans": 3}
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"q": "q" * 900})
        body = _body(await mem_mod.api_memory_observability(req))
        assert body["stats"] == {"semantic_count": 1}
        assert body["rejections"] == {"allowlist_reject": 2}
        assert body["context_preview"] == {"semantic": "s"}
        assert body["reads"] == {"semantic_full_scans": 3}
        assert len(store.get_context_preview.call_args.kwargs["query_text"]) == 500

    @pytest.mark.asyncio
    async def test_observability_reads_the_counters_after_the_preview(self) -> None:
        """Ordering is the contract: `reads` must include THIS request's scans.

        The preview is what reaches the full-read branch, so counters resolved
        before it would under-report by exactly the scan the caller is probing
        for. Own-suite coverage of the real numbers lives in
        ``test_memory_read_counters.py``; here only the order is pinned.
        """
        calls: list[str] = []
        store = _store()
        store.get_context_preview.side_effect = lambda **_: calls.append("preview") or {}
        store.read_counters.side_effect = lambda: calls.append("reads") or {}
        state = _make_state(vector_store=store)
        await mem_mod.api_memory_observability(_make_request(state, query={"q": "tea"}))
        assert calls == ["preview", "reads"]

    @pytest.mark.asyncio
    async def test_preview_reads_the_selected_legacy_named_store(self) -> None:
        global_store = _store(algorithm_version="v1")
        global_store.get_semantic_context.return_value = "Global tea preference"
        named_store = _store(algorithm_version="v1")
        named_store.get_semantic_context.return_value = "[Semantic]\nTeam tea preference"
        named_store.get_episodic_context.return_value = "Team tea discussion"
        state = _make_state(vector_store=global_store)
        req = _make_request(state, query={"store": "crew", "q": "tea"})
        with (
            patch(f"{_MOD}.resolve_requested_memory_store", AsyncMock(return_value=("crew", None))),
            patch(
                f"{_MOD}.vector_memory_for_store", AsyncMock(return_value=named_store)
            ) as acquire,
        ):
            response = await mem_mod.api_memory_context_preview(req)
        assert response.status == 200
        assert _body(response) == {
            "semantic_context": "[Semantic]\nTeam tea preference",
            "episodic_context": "Team tea discussion",
        }
        acquire.assert_awaited_once_with(state, "crew")
        global_store.get_semantic_context.assert_not_called()
        global_store.get_episodic_context.assert_not_called()
        named_store.get_episodic_context.assert_called_once_with(query_text="tea")

    @pytest.mark.asyncio
    async def test_observability_reads_the_selected_legacy_named_store(self) -> None:
        global_store = _store(algorithm_version="v1")
        global_store.memory_stats.return_value = {"semantic_count": 999}
        named_store = _store(algorithm_version="v1")
        named_store.memory_stats.return_value = {"semantic_count": 1}
        named_store.get_context_preview.return_value = {"semantic": "Team tea preference"}
        state = _make_state(vector_store=global_store)
        req = _make_request(state, query={"store": "crew", "q": "tea"})
        with (
            patch(f"{_MOD}.resolve_requested_memory_store", AsyncMock(return_value=("crew", None))),
            patch(
                f"{_MOD}.vector_memory_for_store", AsyncMock(return_value=named_store)
            ) as acquire,
        ):
            response = await mem_mod.api_memory_observability(req)
        assert response.status == 200
        body = _body(response)
        assert body["stats"] == {"semantic_count": 1}
        assert body["context_preview"] == {"semantic": "Team tea preference"}
        acquire.assert_awaited_once_with(state, "crew")
        global_store.memory_stats.assert_not_called()
        global_store.get_context_preview.assert_not_called()
        named_store.get_context_preview.assert_called_once_with(query_text="tea")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler", [mem_mod.api_memory_context_preview, mem_mod.api_memory_observability]
    )
    async def test_selected_store_refusal_does_not_acquire_any_vectors(self, handler) -> None:
        state = _make_state(vector_store=_store(algorithm_version="v1"))
        req = _make_request(state, query={"store": "crew"})
        refusal = web.json_response(
            {"error": "Owner access required", "code": "owner_only"}, status=403
        )
        with (
            patch(f"{_MOD}.resolve_requested_memory_store", AsyncMock(return_value=("", refusal))),
            patch(f"{_MOD}.vector_memory_for_store", AsyncMock()) as acquire,
            patch(f"{_MOD}._get_vector_store_async", AsyncMock()) as global_acquire,
        ):
            response = await handler(req)
        assert response is refusal
        acquire.assert_not_awaited()
        global_acquire.assert_not_awaited()


class TestPromote:
    @pytest.mark.asyncio
    async def test_absent_body_uses_defaults(self) -> None:
        # Every field has a default, so a bodyless POST is legitimate and must
        # still run the default promotion -- that is what allow_absent buys.
        store = _store(promote_episodic_patterns=MagicMock(return_value=2))
        state = _make_state(vector_store=store)
        req = _make_request(state, method="POST", session_key="dashboard:ui")
        assert _body(await mem_mod.api_memory_promote(req)) == {"ok": True, "promoted": 2}
        store.promote_episodic_patterns.assert_called_once_with(5, 0.75)

    @pytest.mark.asyncio
    async def test_malformed_body_is_400_not_silent_defaults(self) -> None:
        # A body that is PRESENT but unparseable is a client mistake, not an
        # absent body: answering 200-with-defaults would run a different
        # promotion than the caller asked for and tell them nothing.
        store = _store(promote_episodic_patterns=MagicMock(return_value=2))
        state = _make_state(vector_store=store)
        req = _make_request(state, method="POST", json_body=_BadJSON(), session_key="dashboard:ui")
        resp = await mem_mod.api_memory_promote(req)
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_json"
        store.promote_episodic_patterns.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_object_body_is_400(self) -> None:
        # `[]` / `"str"` / `5` are valid JSON but not objects; the .get() calls
        # below would raise AttributeError into a 500 without the shape guard.
        store = _store(promote_episodic_patterns=MagicMock(return_value=2))
        state = _make_state(vector_store=store)
        for payload in ([], "str", 5):
            req = _make_request(state, method="POST", json_body=payload, session_key="dashboard:ui")
            resp = await mem_mod.api_memory_promote(req)
            assert resp.status == 400, payload
            assert _body(resp)["code"] == "body_not_object", payload
        store.promote_episodic_patterns.assert_not_called()

    @pytest.mark.asyncio
    async def test_explicit_thresholds_are_forwarded(self) -> None:
        store = _store(promote_episodic_patterns=MagicMock(return_value=0))
        state = _make_state(vector_store=store)
        req = _make_request(
            state,
            method="POST",
            json_body={"min_count": 3, "min_sim": 0.9},
            session_key="dashboard:ui",
        )
        assert (await mem_mod.api_memory_promote(req)).status == 200
        store.promote_episodic_patterns.assert_called_once_with(3, 0.9)

    @pytest.mark.asyncio
    async def test_non_numeric_thresholds_are_400(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(
            state, method="POST", json_body={"min_count": "five"}, session_key="dashboard:ui"
        )
        resp = await mem_mod.api_memory_promote(req)
        assert resp.status == 400
        assert "min_count/min_sim" in _body(resp)["error"]
        store.promote_episodic_patterns.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw",
        [
            # json.loads reads these as +/-inf; int(inf) raises OverflowError.
            '{"min_count": 1e999}',
            '{"min_count": -1e999}',
            '{"min_count": [3]}',
            '{"min_count": null}',
            # A JSON integer past float range: float() raises OverflowError.
            '{"min_sim": 1' + "0" * 400 + "}",
        ],
    )
    async def test_infinite_or_non_number_min_count_is_400(self, raw: str) -> None:
        store = _store(promote_episodic_patterns=MagicMock(return_value=0))
        state = _make_state(vector_store=store)
        req = _make_request(
            state, method="POST", json_body=json.loads(raw), session_key="dashboard:ui"
        )
        resp = await mem_mod.api_memory_promote(req)
        assert resp.status == 400
        assert "min_count/min_sim" in _body(resp)["error"]
        store.promote_episodic_patterns.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ('{"min_count": -2}', (-2, 0.75)),
            ('{"min_count": 99999999999999999999999}', (99999999999999999999999, 0.75)),
        ],
    )
    async def test_finite_min_count_is_forwarded_unchanged(
        self, raw: str, expected: tuple[int, float]
    ) -> None:
        store = _store(promote_episodic_patterns=MagicMock(return_value=0))
        state = _make_state(vector_store=store)
        req = _make_request(
            state, method="POST", json_body=json.loads(raw), session_key="dashboard:ui"
        )
        assert (await mem_mod.api_memory_promote(req)).status == 200
        store.promote_episodic_patterns.assert_called_once_with(*expected)


# ---------------------------------------------------------------------------
# consolidate
# ---------------------------------------------------------------------------


def _consolidator() -> Any:
    c = MagicMock()
    c._running = set()
    c._busy = lambda key: key in c._running
    c._tasks = set()
    c._consolidate = AsyncMock(return_value=None)
    return c


def _write_incognito_transcript(log: Any, key: str) -> None:
    log.init()
    log.append(key, "user", "keep this off the record")
    log.update_metadata(key, {"memory_mode": "incognito"})


async def _seed_incognito_transcript(log: Any, key: str) -> None:
    """Write a closed incognito transcript for *key*, off the event loop.

    The header rewrite ends in ``atomic_write.replace_with_retry``. That helper
    retries a Windows sharing violation (a scanner or indexer briefly holding
    the file just written) only when NO event loop runs in the calling thread.
    On the test's own loop the first ``PermissionError`` is raised at once, so
    the write runs in a worker thread, as the product's own callers do.
    """
    await asyncio.to_thread(_write_incognito_transcript, log, key)


class TestConsolidate:
    @pytest.mark.asyncio
    async def test_the_seed_survives_a_windows_sharing_violation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pin the seeding helper above to the retrying path.

        A seed run on the loop thread re-raises the first simulated violation.
        """
        from windows_sim import replace_sharing_violation

        from kiro_crew import atomic_write, platform_compat
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(atomic_write, "_REPLACE_BACKOFF_SECONDS", 0)
        log = ConversationLog(base_dir=tmp_path / "sessions")
        with replace_sharing_violation(match="dashboard_ghost-1.jsonl", times=1) as sim:
            await _seed_incognito_transcript(log, "dashboard:ghost-1")
        assert sim["n"] == 2
        assert log.get_metadata("dashboard:ghost-1")["memory_mode"] == "incognito"

    @pytest.mark.asyncio
    async def test_denied_for_restricted_session(self) -> None:
        state = _make_state(consolidator=_consolidator(), restricted_keys={"dashboard:ghost"})
        req = _make_request(
            state, method="POST", json_body={"key": "s1"}, session_key="dashboard:ghost"
        )
        assert (await mem_mod.api_memory_consolidate(req)).status == 403

    @pytest.mark.asyncio
    async def test_unrecognized_session_key_cannot_bill_a_consolidation(self) -> None:
        """A forged key must not reach the dispatch, which spends an LLM turn.

        ``_is_restricted_session`` answers False for a key it has never seen, so
        the restricted-mode check alone lets an unknown caller trigger a billed
        consolidation against a session it does not own. The recognition probe
        in front of it is what refuses.
        """
        cons = _consolidator()
        state = _make_state(consolidator=cons)
        req = _make_request(
            state, method="POST", json_body={"key": "s1"}, session_key="dashboard:forged"
        )
        # Patched in ``cron``, whose globals ``_recognize_session`` reads.
        with patch(
            "kiro_crew.dashboard.handlers.cron._probe_persisted_session",
            return_value=(False, None),
        ):
            resp = await mem_mod.api_memory_consolidate(req)
        assert resp.status == 400
        assert _body(resp)["code"] == "unknown_session"
        cons._consolidate.assert_not_called()
        assert not cons._tasks

    @pytest.mark.asyncio
    async def test_missing_session_key_header_is_refused(self) -> None:
        """No X-Session-Key at all is refused with a machine-readable code."""
        cons = _consolidator()
        state = _make_state(consolidator=cons)
        # Explicitly empty: the helper defaults to the trusted key, so "no header at
        # all" is what this case has to state rather than inherit.
        req = _make_request(state, method="POST", json_body={"key": "s1"}, session_key="")
        resp = await mem_mod.api_memory_consolidate(req)
        assert resp.status == 400
        assert _body(resp)["code"] == "missing_session_key"
        cons._consolidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_503_without_consolidator(self) -> None:
        state = _make_state(consolidator=None)
        req = _make_request(
            state, method="POST", json_body={"key": "s1"}, session_key="dashboard:ui"
        )
        resp = await mem_mod.api_memory_consolidate(req)
        assert resp.status == 503
        assert _body(resp) == {"error": "consolidator not available"}

    @pytest.mark.asyncio
    async def test_rejects_invalid_json(self) -> None:
        state = _make_state(consolidator=_consolidator())
        req = _make_request(state, method="POST", json_body=_BadJSON(), session_key="dashboard:ui")
        assert (await mem_mod.api_memory_consolidate(req)).status == 400

    @pytest.mark.asyncio
    async def test_requires_non_blank_session_key(self) -> None:
        state = _make_state(consolidator=_consolidator())
        req = _make_request(
            state, method="POST", json_body={"key": "   "}, session_key="dashboard:ui"
        )
        resp = await mem_mod.api_memory_consolidate(req)
        assert resp.status == 400
        assert _body(resp) == {"error": "session key required"}

    @pytest.mark.asyncio
    async def test_already_running_is_409(self) -> None:
        from kiro_crew.history import ConversationLog

        cons = _consolidator()
        cons._running.add("s1")
        state = _make_state(consolidator=cons)
        state.conversation_log = ConversationLog()
        req = _make_request(
            state, method="POST", json_body={"key": "s1"}, session_key="dashboard:ui"
        )
        resp = await mem_mod.api_memory_consolidate(req)
        assert resp.status == 409
        cons._consolidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_schedules_background_consolidation(self) -> None:
        from kiro_crew.history import ConversationLog

        cons = _consolidator()
        state = _make_state(consolidator=cons)
        state.conversation_log = ConversationLog()
        req = _make_request(
            state,
            method="POST",
            json_body={"key": "s1", "include_history": False},
            session_key="dashboard:ui",
        )
        assert _body(await mem_mod.api_memory_consolidate(req)) == {"ok": True, "key": "s1"}
        assert "s1" in cons._running
        # Drain the spawned task so nothing is left pending at loop teardown.
        for task in list(cons._tasks):
            # flake-ok: pre-existing await of a task this test drives to completion moved verbatim; tracked for a de-flake pass
            await task
        cons._consolidate.assert_awaited_once_with("s1", False)

    # -- the target's memory mode, not only the caller's ---------------------------
    #
    # ``_memory_write_gate`` tests the CALLER (X-Session-Key). The body's ``key``
    # names the TARGET, and a persistent caller consolidating a temporary or
    # incognito session would persist memory for a conversation documented to
    # leave no durable trace. These pin the target-side refusal in front of every
    # piece of consolidation work: no running claim, no task, no dispatch.

    @staticmethod
    def _live_slot(memory_mode: str) -> Any:
        """The three slot fields the live mode resolver reads."""
        return SimpleNamespace(
            memory_mode=memory_mode,
            is_restricted=memory_mode != "persistent",
            blocks_reads=memory_mode == "temporary",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["temporary", "incognito"])
    async def test_private_live_target_is_refused_before_any_work(self, mode: str) -> None:
        """A persistent caller naming a live temporary/incognito slot gets a coded 403.

        A handler that tests only the caller answers ``200 {"ok": true}`` here and
        dispatches ``_consolidate`` for the private key.
        """
        from kiro_crew.history import ConversationLog

        cons = _consolidator()
        state = _make_state(consolidator=cons, restricted_keys={f"dashboard:{mode[:3]}1"})
        state.conversation_log = ConversationLog()
        state._slots[f"{mode[:3]}1"] = self._live_slot(mode)
        req = _make_request(
            state,
            method="POST",
            json_body={"key": f"dashboard:{mode[:3]}1", "include_history": False},
            session_key="dashboard:ui",
        )
        with patch("kiro_crew.dashboard.handlers.memory._sel") as sel:
            resp = await mem_mod.api_memory_consolidate(req)
        assert resp.status == 403
        body = _body(resp)
        assert body["code"] == "restricted_target_session"
        assert mode in body["error"]
        # The mode as a field: the Memory tab names it in its skipped tally
        # ("1 skipped: incognito session") without parsing the sentence.
        assert body["mode"] == mode
        # Refused before the running claim: nothing to release, nothing scheduled.
        cons._consolidate.assert_not_called()
        assert not cons._running
        assert not cons._tasks
        sel().log_api_access.assert_called_once_with(
            caller="dashboard:ui",
            operation="memory.consolidate",
            outcome="denied",
            source="dashboard",
            resources=f"restricted_target_session:{mode}",
        )

    @pytest.mark.asyncio
    async def test_private_persisted_target_is_refused_by_its_header(self) -> None:
        """A closed incognito transcript named by its file stem is refused too.

        "Consolidate all" hands the endpoint ``list_sessions`` stems, which have no
        live slot; the mode then comes from the transcript's own ``memory_mode``.
        """
        from kiro_crew.config.paths import config_dir
        from kiro_crew.history import ConversationLog

        log = ConversationLog(base_dir=config_dir() / "sessions")
        await _seed_incognito_transcript(log, "dashboard:ghost-13077")
        cons = _consolidator()
        state = _make_state(consolidator=cons)
        state.conversation_log = log
        req = _make_request(
            state,
            method="POST",
            json_body={"key": "dashboard_ghost-13077", "include_history": True},
            session_key="dashboard:ui",
        )
        resp = await mem_mod.api_memory_consolidate(req)
        assert resp.status == 403
        assert _body(resp)["code"] == "restricted_target_session"
        cons._consolidate.assert_not_called()
        assert not cons._running

    @pytest.mark.asyncio
    async def test_private_channel_thread_named_by_its_stem_is_refused(self) -> None:
        """A Slack thread flagged ``!incognito`` is refused when named by its stem.

        ``list_sessions`` hands out the filename stem (``slack_<ts>``) while the
        thread's durable flag lives under the live key (``slack:<ts>``) in the
        session map and its transcript header carries no ``memory_mode`` at all,
        so a handler that resolves the raw stem reads the thread as persistent.
        The flag is seeded in a real ``SessionMap`` and the process-local privacy
        trackers start empty, so the stem has to be unfolded by the map and the
        flag hydrated from disk -- the path a gateway restart leaves behind.
        """
        from kiro_crew.history import ConversationLog
        from kiro_crew.messaging import privacy_mode
        from kiro_crew.session_map import SessionMap

        live_key = "slack:1785861252.833429"
        stem = "slack_1785861252.833429"
        sm = SessionMap()
        sm.set_flag(live_key, "incognito", True)
        await sm.aflush()
        privacy_mode.reset()
        assert sm.channel_key_for_stem(stem) == live_key, "premise: the map unfolds the stem"
        assert privacy_mode.is_incognito(live_key) is False, "premise: trackers start empty"
        cons = _consolidator()
        state = _make_state(consolidator=cons)
        state.conversation_log = ConversationLog()
        state.sessions = SimpleNamespace(
            _session_map=sm, channel_key_for_stem=sm.channel_key_for_stem
        )
        try:
            req = _make_request(
                state,
                method="POST",
                json_body={"key": stem, "include_history": True},
                session_key="dashboard:ui",
            )
            resp = await mem_mod.api_memory_consolidate(req)
        finally:
            privacy_mode.reset()
        assert resp.status == 403
        assert _body(resp)["code"] == "restricted_target_session"
        assert "incognito" in _body(resp)["error"]
        cons._consolidate.assert_not_called()
        assert not cons._running

    @pytest.mark.asyncio
    async def test_persistent_live_target_still_dispatches(self) -> None:
        """Pin: a target that positively resolves as persistent is untouched."""
        from kiro_crew.history import ConversationLog

        cons = _consolidator()
        state = _make_state(consolidator=cons)
        state.conversation_log = ConversationLog()
        state._slots["p1"] = self._live_slot("persistent")
        req = _make_request(
            state,
            method="POST",
            json_body={"key": "dashboard:p1", "include_history": False},
            session_key="dashboard:ui",
        )
        assert _body(await mem_mod.api_memory_consolidate(req)) == {
            "ok": True,
            "key": "dashboard:p1",
        }
        for task in list(cons._tasks):
            # flake-ok: pre-existing await of a task this test drives to completion moved verbatim; tracked for a de-flake pass
            await task
        cons._consolidate.assert_awaited_once_with("dashboard:p1", False)

    @pytest.mark.asyncio
    async def test_private_channel_stem_the_map_cannot_unfold_is_refused_by_its_header(
        self,
    ) -> None:
        """A channel stem with no session-map entry is refused from its header.

        The live resolution answers ``persistent`` for every channel key the map
        does not flag, without probing the transcript; a stem whose entry the map
        does not hold would therefore dispatch and answer ``200 {"ok": true}``
        for a pass ``_consolidate`` refuses on the header -- and the Memory tab
        would count it as summarized. The header is the modifier's own record, so
        the route probes it too.
        """
        from kiro_crew.config.paths import config_dir
        from kiro_crew.history import ConversationLog
        from kiro_crew.messaging import privacy_mode
        from kiro_crew.session_map import SessionMap

        live_key = "slack:1785861252.833429"
        stem = "slack_1785861252.833429"
        log = ConversationLog(base_dir=config_dir() / "sessions")
        await _seed_incognito_transcript(log, live_key)
        sm = SessionMap()  # holds nothing for this thread
        assert sm.channel_key_for_stem(stem) == "", "premise: the map cannot unfold the stem"
        privacy_mode.reset()
        cons = _consolidator()
        state = _make_state(consolidator=cons)
        state.conversation_log = log
        state.sessions = SimpleNamespace(
            _session_map=sm, channel_key_for_stem=sm.channel_key_for_stem
        )
        try:
            req = _make_request(
                state,
                method="POST",
                json_body={"key": stem, "include_history": True},
                session_key="dashboard:ui",
            )
            resp = await mem_mod.api_memory_consolidate(req)
        finally:
            privacy_mode.reset()
        assert resp.status == 403
        assert _body(resp)["code"] == "restricted_target_session"
        assert "incognito" in _body(resp)["error"]
        cons._consolidate.assert_not_called()
        assert not cons._running


# ---------------------------------------------------------------------------
# graph error path
# ---------------------------------------------------------------------------


class TestMemoryGraphFailure:
    @pytest.mark.asyncio
    async def test_builder_failure_is_reported_as_500(self) -> None:
        state = _make_state()
        state.lessons.load_all.side_effect = RuntimeError("lesson store gone")
        resp = await mem_mod.api_memory_graph(_make_request(state))
        assert resp.status == 500
        assert _body(resp) == {"error": "failed to build memory graph"}
