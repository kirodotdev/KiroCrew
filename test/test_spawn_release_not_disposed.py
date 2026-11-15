"""A release that disposed nothing says so instead of claiming ``released``.

A backend that owns its own session store (opencode, ...) has no delete route
in ``_cleanup_session_files_sync``, which answers ``False`` for it. Claude
(``test_claude_transcripts.py``) and Codex (``test_codex_transcripts.py``) have
one, and each reads what the trusted cleanup record holds: Claude's cwd, Codex's
store. The manager answers ``not_disposed`` for a ``False``, the HTTP route carries it as
a status, and the tool tells the parent the session may still resume.
Nothing here deletes the backend's own store.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import subagent as sa
from kiro_crew.dashboard.messaging_api import run_control
from kiro_crew.mcp_tools import spawn as spawn_tools
from kiro_crew.subagent import PROVIDER_LABEL_DEFAULT, SubagentManager

#: A thread id in the shape codex hands out; the Codex route refuses any other.
_CODEX_SID = "01a1c612-b002-72e3-89c4-571c49738762"


def _manager(provider: str, sid: str = "sid-1") -> SubagentManager:
    sessions = MagicMock()
    sessions.forget_conversation = MagicMock(return_value=sid)
    sessions.conversation_provider = MagicMock(return_value=provider)
    sessions.reset = AsyncMock()
    return SubagentManager(sessions=sessions, ctx_builder=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["opencode"])
async def test_a_harness_owned_store_is_reported_not_disposed(provider: str) -> None:
    # The real cleanup runs: it has no route for these providers and says False.
    mgr = _manager(provider)
    with patch.object(sa, "update_state") as update:
        ok, detail = await mgr.release_conversation_async("c1")
    assert ok is True
    assert detail.startswith("not_disposed:")
    assert provider in detail
    update.assert_called_once_with("c1", keep=False)


@pytest.mark.asyncio
async def test_a_codex_release_deletes_its_rollout_and_says_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real cleanup runs against the store the run recorded, which is not the
    # gateway's: a run on a cron parent's runtime takes the job's CODEX_HOME.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "gateway"))
    store = tmp_path / "job"
    day = store / "sessions" / "2026" / "11" / "10"
    day.mkdir(parents=True)
    rollout = day / f"rollout-2026-11-10T15-27-55-{_CODEX_SID}.jsonl"
    rollout.write_text("conversation", encoding="utf-8")
    record = {"session_id": _CODEX_SID, "provider": "codex", "codex_home": str(store)}
    mgr = _manager("codex", _CODEX_SID)
    with (
        patch.object(sa, "update_state") as update,
        patch(
            "kiro_crew.subagent_persistence.trusted_cleanup_identity_record",
            return_value=record,
        ) as lookup,
    ):
        assert await mgr.release_conversation_async("c1") == (True, "released")
    lookup.assert_called_once_with("c1", _CODEX_SID, "subagent:c1")
    assert not rollout.exists()
    update.assert_called_once_with("c1", keep=False)


@pytest.mark.asyncio
async def test_a_codex_release_without_a_recorded_store_is_not_disposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The gateway's store holds nothing for the id, so a fallback to it would say released.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "gateway"))
    record = {"session_id": _CODEX_SID, "provider": "codex"}
    mgr = _manager("codex", _CODEX_SID)
    with (
        patch.object(sa, "update_state"),
        patch(
            "kiro_crew.subagent_persistence.trusted_cleanup_identity_record",
            return_value=record,
        ),
    ):
        ok, detail = await mgr.release_conversation_async("c1")
    assert ok is True and detail.startswith("not_disposed:")


def test_the_sync_release_reports_not_disposed_too() -> None:
    mgr = _manager("opencode")
    with patch.object(sa, "update_state"):
        ok, detail = mgr.release_conversation("c1")
    assert ok is True and detail.startswith("not_disposed:")


@pytest.mark.asyncio
async def test_a_cleanup_that_raises_is_not_reported_released() -> None:
    mgr = _manager(PROVIDER_LABEL_DEFAULT)
    with (
        patch.object(sa, "update_state"),
        patch.object(sa, "_cleanup_session_files_sync", side_effect=OSError("disk")),
    ):
        ok, detail = await mgr.release_conversation_async("c1")
    assert ok is True and detail.startswith("not_disposed:")


@pytest.mark.asyncio
async def test_a_kiro_cleanup_that_succeeds_still_says_released() -> None:
    mgr = _manager(PROVIDER_LABEL_DEFAULT)
    with (
        patch.object(sa, "update_state"),
        patch.object(sa, "_cleanup_session_files_sync", return_value=True) as cleanup,
    ):
        assert await mgr.release_conversation_async("c1") == (True, "released")
    cleanup.assert_called_once_with("sid-1", PROVIDER_LABEL_DEFAULT)


@pytest.mark.asyncio
async def test_the_route_answers_not_disposed_as_a_status() -> None:
    subagents = MagicMock()
    subagents.get = MagicMock(return_value=object())
    subagents.release_conversation_async = AsyncMock(
        return_value=(True, "not_disposed: Crew forgot this conversation")
    )
    app = web.Application()
    app["state"] = MagicMock(subagents=subagents)
    req = make_mocked_request(
        "POST", "/api/spawn/c1/release", app=app, match_info={"agent_id": "c1"}
    )
    resp = await run_control.api_spawn_release(req)
    assert resp.status == 200
    assert b'"status": "not_disposed"' in resp.body


def test_the_tool_does_not_claim_a_not_disposed_release(monkeypatch) -> None:
    monkeypatch.setattr(
        spawn_tools.mcp_core,
        "_post",
        lambda path, body: {"conversation": "abc123", "status": "not_disposed"},
    )
    out = spawn_tools.spawn_release("spawn_release", {"conversation": "abc123"})
    assert out.startswith("Not disposed: conversation abc123")
    assert "can no longer be continued" not in out
