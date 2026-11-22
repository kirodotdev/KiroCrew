"""A finished KAS sub-agent run hands its session record to the engine's delete.

The record belongs to the KAS engine, so the prune queues it in
``released_sessions`` and the cleanup tick sends ``_kiro/session/delete``. These
tests pin the queueing: a due run is pruned with its entry written, a run not yet
due writes nothing, and an entry that cannot be written keeps the run.
"""

from __future__ import annotations

import json
import time

import pytest

import kiro_crew.subagent_persistence as sp
from kiro_crew import released_sessions, sandbox_launch_mark

DAY = 86400
#: The shipped windows: a delivered result for one hour, any other ending for 7 days.
RESULT_TTL = 3600
MAX_AGE_DAYS = 7


@pytest.fixture(autouse=True)
def _not_sandboxed(_floor_monkeypatch):
    _floor_monkeypatch.setattr(sandbox_launch_mark, "launched_in_sandbox", lambda: False)


def _seed(agent_id: str, session_id: str, *, cause: str = "delivered", age_secs: float) -> None:
    """A finished ``kas`` run whose tombstone is *age_secs* old."""
    sp.create_agent_folder(agent_id, task=agent_id)
    sp.update_state(agent_id, session_id=session_id, provider="kas", keep=False)
    sp.remember_live_cleanup_identity(agent_id, session_id=session_id, provider="kas", keep=False)
    sp.write_tombstone(agent_id, cause=cause, recovery_action="none")
    ts_path = sp._agent_dir(agent_id) / "tombstone.json"
    tombstone = json.loads(ts_path.read_text())
    # flake-ok: back-dated on the live clock, far from either window's edge
    tombstone["died"] = time.time() - age_secs
    ts_path.write_text(json.dumps(tombstone))


def _prune() -> None:
    """One sweep with the shipped windows, compared with the back-dated ages above."""
    sp.prune_stale_tombstones(  # flake-ok: retention windows, not a wait
        max_age_days=MAX_AGE_DAYS, delivered_ttl_secs=RESULT_TTL
    )


def _entry(session_id: str) -> dict | None:
    path = released_sessions.released_sessions_dir() / f"{session_id}.json"
    return json.loads(path.read_text()) if path.exists() else None


def test_kas_has_a_cleanup_route():
    assert sp._provider_cleanup_has_route("kas") is True


@pytest.mark.parametrize("cause", ["delivered", "error"])
def test_a_due_kas_run_is_pruned_and_its_session_queued_for_delete(cause):
    _seed("a1", "sess_due", cause=cause, age_secs=8 * DAY)

    _prune()

    assert not sp._agent_dir("a1").exists()
    entry = _entry("sess_due")
    assert entry is not None
    assert (entry["backend"], entry["delete_at"] <= time.time()) == ("kas", True)
    assert released_sessions.due()[0].session_id == "sess_due"


def test_a_delivered_kas_run_is_due_after_the_result_ttl():
    _seed("a1", "sess_hour", age_secs=2 * 3600)

    _prune()

    assert not sp._agent_dir("a1").exists()
    assert _entry("sess_hour") is not None


@pytest.mark.parametrize(
    "cause, age_secs", [("delivered", 60), ("error", 2 * DAY)], ids=["delivered", "error"]
)
def test_a_kas_run_not_yet_due_writes_no_entry(cause, age_secs):
    _seed("a1", "sess_young", cause=cause, age_secs=age_secs)

    _prune()

    assert sp._agent_dir("a1").exists()
    assert _entry("sess_young") is None


@pytest.mark.parametrize("bad", ["..", "a/b"])
def test_an_unsafe_session_id_writes_no_entry_and_is_not_done(bad):
    assert sp._cleanup_session_files_sync(bad, "kas") is False
    assert not released_sessions.released_sessions_dir().exists() or not any(
        released_sessions.released_sessions_dir().iterdir()
    )


def test_a_kas_run_whose_entry_cannot_be_written_is_kept(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(released_sessions, "schedule", refuse)
    _seed("a1", "sess_kept", age_secs=8 * DAY)

    _prune()

    assert sp._agent_dir("a1").exists()


def test_a_sandboxed_process_queues_nothing(monkeypatch):
    monkeypatch.setattr(sandbox_launch_mark, "launched_in_sandbox", lambda: True)

    assert sp._cleanup_session_files_sync("sess_boxed", "kas") is False
    assert _entry("sess_boxed") is None


def _release_manager(sid: str):
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.subagent import SubagentManager

    sessions = MagicMock()
    sessions.forget_conversation = MagicMock(return_value=sid)
    sessions.conversation_provider = MagicMock(return_value="kas")
    sessions.reset = AsyncMock()
    return SubagentManager(sessions=sessions, ctx_builder=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_kas_release_queues_nothing_and_says_not_disposed(close_subagent_managers):
    """A release leaves the KAS record to the prune.

    A continue may still resume the session until the run is pruned, and the
    prune queues the delete in the same pass that removes the run folder.
    """
    _seed("c1", "sess_released", age_secs=60)

    ok, detail = await _release_manager("sess_released").release_conversation_async("c1")

    assert ok is True and detail.startswith("not_disposed:")
    assert _entry("sess_released") is None
    assert sp.read_state("c1")["session_id"] == "sess_released"
