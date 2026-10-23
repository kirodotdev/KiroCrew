"""The session gate and the body check on the memory dashboard's mutating routes.

This file pins, for ``kiro_crew.dashboard.handlers.memory``, the session gate on the
routes in its shared table (preferences, projects, history, migrate, import and promote)
and on the settings PUT, how that gate recognises a subagent's session, and the 400 every
converted handler answers on a non-object body. The family's style and its other files
are described in ``test_handlers_memory_coverage.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from handlers_memory_coverage_helpers import (  # noqa: F401 -- the autouse fixtures register by import
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
# the memory-mutation session gate, on every route that carries it
# ---------------------------------------------------------------------------


def _mutating_cases() -> list[tuple[str, str, str, Any, str]]:
    """(id, handler name, HTTP method, body, name of the store/memory mutator).

    Every route here mutates durable memory, so each one runs the same two-step
    gate: session recognition first, then the restricted-mode check. The mutator
    name is what the assertions use to prove the refusal happened BEFORE any write.
    """
    return [
        ("preferences", "api_memory_preferences", "PUT", {"content": "x"}, "write_preferences"),
        ("projects", "api_memory_projects", "PUT", {"content": "x"}, "write_projects"),
        ("history", "api_memory_history", "PUT", {"content": "x"}, "write_today_history"),
        ("migrate", "api_memory_migrate", "POST", None, "migrate_from_markdown"),
        ("import", "api_memory_import", "POST", {"semantic": []}, "import_memory"),
        ("promote", "api_memory_promote", "POST", None, "promote_episodic_patterns"),
    ]


_MARKDOWN_ROUTES = {"preferences", "projects", "history"}


def _gate_fixture(case_id: str, mutator: str, *, restricted: bool) -> tuple[Any, Any]:
    """A state plus the mock whose *mutator* must never be called."""
    if case_id in _MARKDOWN_ROUTES:
        target = MagicMock()
        state = _make_state(
            memory=target,
            vector_store=_store(),
            restricted_keys={"dashboard:ghost"} if restricted else None,
        )
    else:
        target = _store()
        state = _make_state(
            vector_store=target,
            restricted_keys={"dashboard:ghost"} if restricted else None,
        )
    return state, getattr(target, mutator)


class TestMemoryMutationSessionGate:
    """Each mutating memory route refuses an unrecognised or restricted session.

    The two halves are separate controls and neither substitutes for the other:
    ``_is_restricted_session`` answers False for a key it has never seen, so
    without the recognition probe a forged ``X-Session-Key`` walked straight into
    a durable write (and, on promote, into an episodic tombstone).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case_id,handler,method,body,mutator", _mutating_cases(), ids=lambda v: v
    )
    async def test_missing_session_key_is_refused(
        self, case_id: str, handler: str, method: str, body: Any, mutator: str
    ) -> None:
        state, blocked = _gate_fixture(case_id, mutator, restricted=False)
        # Explicitly empty: the helper defaults to the trusted key, so a caller with
        # no header at all is the thing this case has to state rather than inherit.
        req = _make_request(state, method=method, json_body=body, session_key="")
        resp = await getattr(mem_mod, handler)(req)
        assert resp.status == 400
        assert _body(resp)["code"] == "missing_session_key"
        blocked.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case_id,handler,method,body,mutator", _mutating_cases(), ids=lambda v: v
    )
    async def test_unrecognized_session_is_refused(
        self, case_id: str, handler: str, method: str, body: Any, mutator: str
    ) -> None:
        # No slot, no restricted-key entry, no transcript on disk (KIROCREW_HOME is
        # pinned per test), so the recognition probe finds nothing to accept.
        state, blocked = _gate_fixture(case_id, mutator, restricted=False)
        req = _make_request(state, method=method, json_body=body, session_key="dashboard:forged")
        resp = await getattr(mem_mod, handler)(req)
        assert resp.status == 400
        assert _body(resp)["code"] == "unknown_session"
        blocked.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case_id,handler,method,body,mutator", _mutating_cases(), ids=lambda v: v
    )
    async def test_restricted_session_is_refused(
        self, case_id: str, handler: str, method: str, body: Any, mutator: str
    ) -> None:
        state, blocked = _gate_fixture(case_id, mutator, restricted=True)
        req = _make_request(state, method=method, json_body=body, session_key="dashboard:ghost")
        resp = await getattr(mem_mod, handler)(req)
        assert resp.status == 403
        assert _body(resp)["code"] == "restricted_session"
        blocked.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case_id,handler,method,body,mutator", _mutating_cases(), ids=lambda v: v
    )
    async def test_a_denial_is_audited(
        self,
        case_id: str,
        handler: str,
        method: str,
        body: Any,
        mutator: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A refusal is an authorization decision, so it lands in the SEL log."""
        log = MagicMock()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.sel", lambda: SimpleNamespace(log_api_access=log)
        )
        state, _blocked = _gate_fixture(case_id, mutator, restricted=True)
        req = _make_request(state, method=method, json_body=body, session_key="dashboard:ghost")
        assert (await getattr(mem_mod, handler)(req)).status == 403
        denials = [c.kwargs for c in log.call_args_list if c.kwargs.get("outcome") == "denied"]
        assert denials, log.call_args_list
        assert denials[-1]["resources"] == "restricted_session_block"
        assert denials[-1]["caller"] == "dashboard:ghost"
        assert denials[-1]["source"] == "dashboard"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "session_key,status,code",
        [
            ("", 400, "missing_session_key"),
            ("dashboard:forged", 400, "unknown_session"),
            ("dashboard:ghost", 403, "restricted_session"),
        ],
        ids=["missing", "unrecognized", "restricted"],
    )
    async def test_the_settings_put_is_gated(
        self, session_key: str, status: int, code: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The settings PUT carries the same gate, because it writes ``migrated``.

        That is the identical install-wide flag ``/api/memory/migrate`` flips, so a
        caller refused on one route must not reach it through the other. Its mutator
        is a config write rather than a store method, which is why it is asserted
        here instead of through the shared route table.
        """
        writer = MagicMock()
        monkeypatch.setattr(mem_mod, "run_config_write", writer)
        state = _make_state(restricted_keys={"dashboard:ghost"})
        req = _make_request(
            state, method="PUT", json_body={"migrated": True}, session_key=session_key
        )
        with patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()):
            resp = await mem_mod.api_memory_settings(req)
        assert resp.status == status
        assert _body(resp)["code"] == code
        writer.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case_id,handler",
        [
            ("preferences", "api_memory_preferences"),
            ("projects", "api_memory_projects"),
            ("history", "api_memory_history"),
        ],
    )
    async def test_the_markdown_get_stays_ungated(self, case_id: str, handler: str) -> None:
        """Only the PUT is a write. Gating the read would break the Memory tab for
        every session the gate cannot recognise, and this PR changes no read path."""
        mem = MagicMock()
        mem.read_preferences.return_value = ""
        mem.read_projects.return_value = ""
        mem.read_editable_history.return_value = ""
        state = _make_state(memory=mem)
        resp = await getattr(mem_mod, handler)(_make_request(state))
        assert resp.status == 200
        assert _body(resp) == {"content": "", "content_redacted": False}


@pytest.mark.asyncio
class TestSubagentMemorySessionRecognition:
    @pytest.mark.parametrize(
        "run_id", ["", "../worker", r"..\worker", ".worker", "C:worker", "worker:stream", "a\x00b"]
    )
    async def test_invalid_subagent_ids_cannot_reach_the_filesystem(self, monkeypatch, run_id):
        from kiro_crew.dashboard.handlers.cron import _recognize_session
        from kiro_crew.history import is_incognito_transcript

        # Model an existing target at every spelling: rejection must come from
        # key validation, not from an absent file (including Windows ADS paths).
        exists = MagicMock(return_value=True)
        with monkeypatch.context() as patched:
            patched.setattr(Path, "exists", exists)
            refusal = await _recognize_session(
                _make_state(),
                f"subagent:{run_id}",
                "test.memory",
                blocks_persisted_mode=is_incognito_transcript,
            )

        assert refusal is not None
        assert refusal.status == 400
        assert _body(refusal)["code"] == "unknown_session"
        exists.assert_not_called()

    async def test_subagent_history_cannot_replace_a_live_allocation(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers.cron import _recognize_session
        from kiro_crew.history import ConversationLog, is_incognito_transcript

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        log = ConversationLog(base_dir=tmp_path / "sessions")
        await asyncio.to_thread(
            log.update_metadata, "subagent:worker-a", {"memory_store": "default"}
        )

        refusal = await _recognize_session(
            _make_state(),
            "subagent:worker-a",
            "test.memory",
            blocks_persisted_mode=is_incognito_transcript,
        )

        assert refusal is not None
        assert refusal.status == 400
        assert _body(refusal)["code"] == "unknown_session"

    @pytest.mark.parametrize(
        "collision", ["live-slot", "restricted-key", "bare", "dashboard", "cron"]
    )
    async def test_a_subagent_cannot_borrow_another_sessions_identity(
        self, tmp_path, monkeypatch, collision
    ):
        from kiro_crew.dashboard.handlers.cron import _recognize_session
        from kiro_crew.history import ConversationLog, is_incognito_transcript

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _make_state()
        if collision == "live-slot":
            state._slots["worker-a"] = SimpleNamespace(memory_mode="persistent")
        elif collision == "restricted-key":
            state._restricted_keys.add("subagent:worker-a")
        else:
            key = "worker-a" if collision == "bare" else f"{collision}:worker-a"
            log = ConversationLog(base_dir=tmp_path / "sessions")
            await asyncio.to_thread(log.update_metadata, key, {"memory_mode": "persistent"})

        refusal = await _recognize_session(
            state,
            "subagent:worker-a",
            "test.memory",
            blocks_persisted_mode=is_incognito_transcript,
        )

        assert refusal is not None
        assert refusal.status == 400
        assert _body(refusal)["code"] == "unknown_session"

    @pytest.mark.parametrize(
        "metadata",
        [
            {"_type": "metadata", "memory_mode": "persistent"},
            {"_type": "metadata", "memory_mode": "incognito"},
            {"_type": "metadata", "memory_mode": "temporary"},
            {"_type": "metadata", "memory_mode": "unknown"},
            {"_type": "metadata", "memory_mode": []},
            {"role": "user", "content": "not metadata"},
        ],
    )
    async def test_subagent_history_mode_cannot_replace_a_live_allocation(
        self, tmp_path, monkeypatch, metadata
    ):
        from kiro_crew.dashboard.handlers.cron import _recognize_session
        from kiro_crew.history import is_incognito_transcript

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "subagent_worker-a.jsonl").write_text(
            json.dumps(metadata) + "\n", encoding="utf-8"
        )
        # Neither a saved mode nor a colliding slot can make this child live.
        state = _make_state()
        state._slots["worker-a"] = SimpleNamespace(memory_mode="persistent")

        refusal = await _recognize_session(
            state,
            "subagent:worker-a",
            "test.memory",
            blocks_persisted_mode=is_incognito_transcript,
        )

        assert refusal is not None
        assert refusal.status == 400
        assert _body(refusal)["code"] == "unknown_session"


class TestNonObjectBodiesAcrossConvertedHandlers:
    """Every converted handler answers 400, never 5xx, on a non-object body.

    ``[]`` / ``"s"`` / ``5`` / ``true`` / ``null`` are all VALID JSON, so
    ``request.json()`` returns them and the ``.get()`` each handler performs
    next raised ``AttributeError`` from OUTSIDE the parse ``try`` -- a 500 for
    what is really malformed client input. Enumerated rather than
    written one test per handler so a handler that loses the guard fails by
    construction; the agents.py half of the same invariant lives in
    ``test_json_object_body_guard.py``.
    """

    _HANDLERS = [
        ("api_memory_preferences", "PUT"),
        ("api_memory_projects", "PUT"),
        ("api_memory_history", "PUT"),
        ("api_memory_settings", "PUT"),
        ("api_memory_semantic_write", "PUT"),
        ("api_memory_import", "POST"),
        ("api_memory_consolidate", "POST"),
        ("api_memory_promote", "POST"),
        ("api_memory_embedding_model", "POST"),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [[], "a string", 5, 1.5, True, None], ids=repr)
    @pytest.mark.parametrize("handler_name,method", _HANDLERS, ids=str)
    async def test_non_object_body_is_400_not_500(
        self, handler_name: str, method: str, payload: Any
    ) -> None:
        state = _make_state(
            vector_store=_store(),
            memory=MagicMock(),
            consolidator=MagicMock(),
        )
        req = _make_request(
            state,
            method=method,
            json_body=payload,
            session_key="dashboard:ui",
            # A body IS present in every row -- including the ``null`` one,
            # which must be refused as a non-object rather than read as an
            # absent body by the allow_absent endpoints.
            body_present=True,
        )
        handler = getattr(mem_mod, handler_name)
        with patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()):
            resp = await handler(req)
        assert resp.status == 400, f"{handler_name} on {payload!r}: expected 400"
        assert _body(resp)["code"] == "body_not_object", handler_name
