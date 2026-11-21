"""Every global consolidation job starts on an empty conversation of the same runtime.

All global jobs share the ``_consolidate`` session, whose semaphore queues them.
``background_turn(fresh_conversation=True)`` ends each job's conversation before
the permit is released: ``new_conversation()`` opens a fresh one on the running
process and the old session's teardown deletes its backend record. Without that,
one ~80 KB job after another stacks in a single conversation until a small-window
model refuses it or a large-window one draws HTTP 429.

The first tests run the REAL path: a real ``SessionManager`` and provider factory
spawning the packaged fake ACP backend as a real child process, with its native
store emulation on (``fake_acp_backend.NATIVE_STORE_ENV``). That records the
request each turn would send -- the whole conversation, as a model sees it -- with
the serving process's pid, and deletes a record only on KAS's
``_kiro/session/delete``. The rest pin the seams one at a time.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew import history_consolidation
from kiro_crew import history_consolidation_pass as consolidation_pass
from kiro_crew import history_consolidation_skills as skills
from kiro_crew.llm_helpers import background_turn, stream_and_collect
from kiro_crew.session import CONSOLIDATE_KEY
from kiro_crew.testing import fake_acp_backend
from kiro_crew.testing.harness import fake_acp_backend_launcher

#: These tests start background helper turns, which are skipped while the host's
#: memory posture is critical (see ``ample_host_free_memory``).
pytestmark = pytest.mark.usefixtures("ample_host_free_memory")

_USAGE_TARGET = "kiro_crew.dashboard.handlers.usage.persist_token_record_async"


# ── real child process ──


def _real_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[object, Path]:
    from kiro_crew import agent as agent_mod
    from kiro_crew.acp import session_handle
    from kiro_crew.agent_materialization.service_agents import _install_lite_agent_fallback
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.config.loader import build_provider_factory
    from kiro_crew.session import SessionManager

    # The fake backend reports no MCP server, so each start would wait out the full
    # no-report drain; the conversation handling under test runs after it either way.
    # Through the facade: the ceiling is bound in more than one owner.
    monkeypatch.setattr(
        session_handle, "_MCP_DRAIN_NO_REPORT_CEILING", 0.05  # flake-ok: unreported by design
    )
    store = tmp_path / "native-store"
    store.mkdir()
    monkeypatch.setenv("KIROCREW_KIRO_BIN", str(fake_acp_backend_launcher(tmp_path)))
    monkeypatch.setenv(fake_acp_backend.NATIVE_STORE_ENV, str(store))
    agent_mod.kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    _install_lite_agent_fallback()
    cfg = KiroCrewConfig.load()
    # The fake backend is the only child these tests spawn, and CI hosts have no
    # OS sandbox backend (no user namespaces on Linux, none on Windows), so this
    # test's own config runs it unsandboxed. The product default is untouched.
    cfg.agent.sandbox = "off"
    return SessionManager(cfg, provider_factory=build_provider_factory(cfg)), store


def _requests(store: Path) -> list[dict]:
    log = store / fake_acp_backend.NATIVE_REQUEST_LOG
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _records(store: Path) -> list[str]:
    return sorted(p.name for p in store.iterdir() if p.name.startswith("sess_"))


def _consolidator(sessions: object) -> SimpleNamespace:
    return SimpleNamespace(_sessions=sessions, _logger=logging.getLogger("test.consolidation"))


async def _run_jobs(sessions: object, store: Path, jobs: list[str]) -> list[list[str]]:
    """Run *jobs* through ``_call_llm``; the live records after each job."""
    seen = []
    for prompt in jobs:
        # The fake answers in prose, so the reply is unusable; the request it
        # sent is what these tests are about.
        with contextlib.suppress(history_consolidation._ConsolidationReplyUnusable):
            await consolidation_pass._call_llm(_consolidator(sessions), prompt)
        seen.append(_records(store))
    return seen


@pytest.mark.asyncio
async def test_back_to_back_jobs_each_send_only_their_own_prompt_on_one_runtime(
    tmp_path, monkeypatch, caplog
):
    sessions, store = _real_manager(tmp_path, monkeypatch)
    jobs = ["JOB-ONE transcript about apples", "JOB-TWO transcript about pears"]
    try:
        with caplog.at_level(logging.INFO, logger="kiro_crew.llm_helpers"):
            records_after = await _run_jobs(sessions, store, jobs)
    finally:
        await sessions.close_all()

    first, second = _requests(store)
    assert [first["turn"], second["turn"]] == [1, 1]
    assert first["sid"] != second["sid"]
    assert jobs[0] in first["request"]
    assert jobs[0] not in second["request"]
    # One process served both jobs: the context was cleared, the runtime kept.
    assert first["pid"] == second["pid"]
    # Each job's record is gone once the job ends; only the empty conversation
    # the next job will use is left.
    assert first["sid"] not in records_after[0]
    assert len(records_after[0]) == 1
    assert second["sid"] not in records_after[1]
    assert first["sid"] not in records_after[1]
    paths = [r.getMessage() for r in caplog.records if "fresh conversation" in r.getMessage()]
    assert len(paths) == 2 and all("same runtime" in m for m in paths)


@pytest.mark.asyncio
async def test_a_failed_new_conversation_falls_back_to_a_fresh_runtime(
    tmp_path, monkeypatch, caplog
):
    from kiro_crew.providers.acp import AcpProvider

    monkeypatch.setattr(
        AcpProvider, "new_conversation", AsyncMock(side_effect=RuntimeError("no warm reset"))
    )
    sessions, store = _real_manager(tmp_path, monkeypatch)
    jobs = ["FALLBACK-ONE apples", "FALLBACK-TWO pears"]
    try:
        with caplog.at_level(logging.INFO, logger="kiro_crew.llm_helpers"):
            await _run_jobs(sessions, store, jobs)
    finally:
        await sessions.close_all()

    first, second = _requests(store)
    assert second["turn"] == 1
    assert jobs[0] not in second["request"]
    assert first["pid"] != second["pid"]
    assert any("reset to a new runtime" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_shared_key_without_a_fresh_conversation_stacks_jobs(tmp_path, monkeypatch):
    """The harness can see stacking: the same key with no fresh conversation keeps
    one conversation, so the second request carries the first job's text and its
    record survives. If this stops growing, the tests above prove nothing."""
    sessions, store = _real_manager(tmp_path, monkeypatch)
    jobs = ["STACK-ONE apples", "STACK-TWO pears"]
    try:
        for prompt in jobs:
            async with background_turn(
                sessions, task="consolidation", agent="kirocrew-lite", session_key=CONSOLIDATE_KEY
            ) as client:
                await stream_and_collect(client, prompt)
    finally:
        await sessions.close_all()

    first, second = _requests(store)
    assert [first["turn"], second["turn"]] == [1, 2]
    assert first["sid"] == second["sid"]
    assert jobs[0] in second["request"]
    assert _records(store) == [first["sid"]]


# ── skill judges on _bg ──


async def _judges_then_a_title(sessions: object) -> None:
    c = _consolidator(sessions)
    await skills._dedupe_judge(c, "DEDUPE apples")
    await skills._merge_skill_update(c, "MERGE-BODY pears", "d", "t", "steps")
    async with background_turn(sessions, task="chat_title") as client:
        await stream_and_collect(client, "TITLE prompt")


@pytest.mark.asyncio
async def test_skill_judges_each_send_only_their_own_prompt_on_bg(tmp_path, monkeypatch):
    sessions, store = _real_manager(tmp_path, monkeypatch)
    try:
        await _judges_then_a_title(sessions)
    finally:
        await sessions.close_all()

    dedupe, merge, title = _requests(store)
    assert [r["turn"] for r in (dedupe, merge, title)] == [1, 1, 1]
    assert "DEDUPE" not in merge["request"]
    assert "DEDUPE" not in title["request"] and "MERGE-BODY" not in title["request"]
    assert dedupe["pid"] == merge["pid"] == title["pid"]


@pytest.mark.asyncio
async def test_the_reset_fallback_on_bg_does_not_resume_the_judges_history(tmp_path, monkeypatch):
    """``_bg`` is a persistent key, unlike ``_consolidate``: its reset must not reload the old one."""
    from kiro_crew.providers.acp import AcpProvider

    monkeypatch.setattr(
        AcpProvider, "new_conversation", AsyncMock(side_effect=RuntimeError("no warm reset"))
    )
    sessions, store = _real_manager(tmp_path, monkeypatch)
    try:
        await _judges_then_a_title(sessions)
    finally:
        await sessions.close_all()

    dedupe, merge, title = _requests(store)
    assert [merge["turn"], title["turn"]] == [1, 1]
    assert "DEDUPE" not in title["request"] and "MERGE-BODY" not in title["request"]
    assert dedupe["pid"] != merge["pid"]


# ── background_turn seams ──


class _Client:
    def __init__(self, calls: list, *, fail: bool = False) -> None:
        self.last_prompt_stats = None
        self._calls = calls
        self._fail = fail

    async def new_conversation(self) -> None:
        self._calls.append("new_conversation")
        if self._fail:
            raise RuntimeError("refused")


class _Sessions:
    def __init__(self, client: object | None = None) -> None:
        self.calls: list = []
        self.client = client if client is not None else _Client(self.calls)

    async def get_or_create(self, key: str, **_kw: object):
        self.calls.append(("acquire", key))
        return self.client, False, False

    async def end_children_for(self, key: str) -> None:
        self.calls.append(("end_children", key))

    async def reset(self, key: str, **kw: object) -> bool:
        self.calls.append(("reset", key, kw.get("ends_conversation")))
        return True

    def release(self, key: str) -> None:
        self.calls.append(("release", key))

    async def recycle_background(self, session_key=None, **_kw: object) -> None:
        self.calls.append(("recycle", session_key or "_bg"))


async def _one_turn(sessions: _Sessions, *, fresh: bool = True, failing: bool = False) -> None:
    with patch(_USAGE_TARGET):
        with contextlib.suppress(RuntimeError):
            async with background_turn(
                sessions,
                task="consolidation",
                session_key=CONSOLIDATE_KEY,
                fresh_conversation=fresh,
            ):
                if failing:
                    raise RuntimeError("turn failed")


@pytest.mark.asyncio
async def test_the_fresh_conversation_runs_before_the_permit_is_released():
    sessions = _Sessions()
    await _one_turn(sessions)
    assert sessions.calls == [
        ("acquire", CONSOLIDATE_KEY),
        ("end_children", CONSOLIDATE_KEY),
        "new_conversation",
        ("release", CONSOLIDATE_KEY),
    ]


@pytest.mark.asyncio
async def test_a_failed_turn_still_gets_a_fresh_conversation():
    sessions = _Sessions()
    await _one_turn(sessions, failing=True)
    assert "new_conversation" in sessions.calls
    assert sessions.calls[-1] == ("release", CONSOLIDATE_KEY)


@pytest.mark.asyncio
async def test_a_raising_new_conversation_falls_back_to_reset():
    calls: list = []
    sessions = _Sessions(_Client(calls, fail=True))
    sessions.calls = calls
    await _one_turn(sessions)
    assert calls[-3:] == [
        "new_conversation",
        ("reset", CONSOLIDATE_KEY, True),
        ("release", CONSOLIDATE_KEY),
    ]


@pytest.mark.asyncio
async def test_a_provider_without_new_conversation_falls_back_to_reset():
    sessions = _Sessions(SimpleNamespace(last_prompt_stats=None))
    await _one_turn(sessions)
    assert sessions.calls[-2:] == [("reset", CONSOLIDATE_KEY, True), ("release", CONSOLIDATE_KEY)]


class _CodexLikeClient:
    """A provider whose live process cannot swap conversations, as on codex.

    Each reset moves it onto a new session id, the way the next job's cold start
    does; ``cleanup_session`` is the provider's delete of one native record.
    """

    def __init__(self, calls: list) -> None:
        self.last_prompt_stats = None
        self._calls = calls
        self._n = 1
        self.session_id = "thread-1"

    async def new_conversation(self) -> None:
        self._calls.append("new_conversation")
        raise NotImplementedError("needs a new process")

    async def cleanup_session(self, session_id: str) -> None:
        self._calls.append(("cleanup", session_id))

    def next_session(self) -> None:
        self._n += 1
        self.session_id = f"thread-{self._n}"


class _CodexLikeSessions(_Sessions):
    def __init__(self, *, reset_fails: bool = False) -> None:
        super().__init__()
        self.client = _CodexLikeClient(self.calls)
        self._reset_fails = reset_fails

    async def reset(self, key: str, **kw: object) -> bool:
        await super().reset(key, **kw)
        if self._reset_fails:
            raise RuntimeError("shutdown failed")
        self.client.next_session()
        return True


@pytest.mark.asyncio
async def test_each_reset_job_deletes_only_its_own_conversation_record():
    sessions = _CodexLikeSessions()
    for _ in range(3):
        await _one_turn(sessions)
    cleanups = [c for c in sessions.calls if isinstance(c, tuple) and c[0] == "cleanup"]
    assert cleanups == [("cleanup", "thread-1"), ("cleanup", "thread-2"), ("cleanup", "thread-3")]
    # Deleted after the reset retired the process, before the permit is released.
    assert sessions.calls[-3:] == [
        ("reset", CONSOLIDATE_KEY, True),
        ("cleanup", "thread-3"),
        ("release", CONSOLIDATE_KEY),
    ]


@pytest.mark.asyncio
async def test_a_failed_reset_keeps_the_record_of_a_process_that_may_still_run():
    sessions = _CodexLikeSessions(reset_fails=True)
    await _one_turn(sessions)
    assert not [c for c in sessions.calls if isinstance(c, tuple) and c[0] == "cleanup"]


@pytest.mark.asyncio
async def test_a_raising_record_delete_still_releases_the_permit(caplog):
    sessions = _CodexLikeSessions()
    sessions.client.cleanup_session = AsyncMock(side_effect=OSError("store unreadable"))
    with caplog.at_level(logging.WARNING, logger="kiro_crew.llm_helpers"):
        await _one_turn(sessions)
    sessions.client.cleanup_session.assert_awaited_once_with("thread-1")
    assert sessions.calls[-1] == ("release", CONSOLIDATE_KEY)
    assert any(
        r.levelno == logging.WARNING and "thread-1" in r.getMessage() for r in caplog.records
    )


def _rollout(codex_home: Path, thread_id: str) -> Path:
    day = codex_home / "sessions" / "2026" / "11" / "21"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-11-21T10-00-00-{thread_id}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_codex_consolidation_passes_leave_no_rollout_and_keep_the_chats(tmp_path):
    """The provider's real codex delete, on rollout files under a private ``CODEX_HOME``.

    Rollout-cleanup coverage only: no Codex process runs, so neither what a Codex
    client lists nor a resume of the kept chat is exercised here.
    """
    import types

    from kiro_crew.providers import acp_lifecycle

    codex_home = tmp_path / "codex"
    helpers = [f"0000000{n}-0000-4000-8000-000000000000" for n in range(1, 4)]
    chat = _rollout(codex_home, "11111111-1111-4111-8111-111111111111")
    helper_rollouts = [_rollout(codex_home, sid) for sid in helpers]

    sessions = _CodexLikeSessions()
    client = sessions.client
    client._client = SimpleNamespace(_extra_env={"CODEX_HOME": str(codex_home)})
    client.is_kas_backend = client.is_claude_backend = client.is_opencode_backend = False
    client.is_codex_backend = True
    client.cleanup_session = types.MethodType(acp_lifecycle.cleanup_session, client)
    for sid in helpers:
        client.session_id = sid
        await _one_turn(sessions)

    assert not [p for p in helper_rollouts if p.exists()]
    assert chat.exists()


@pytest.mark.asyncio
async def test_without_the_flag_the_key_is_recycled_as_before():
    sessions = _CodexLikeSessions()
    await _one_turn(sessions, fresh=False)
    assert "new_conversation" not in sessions.calls
    assert not [c for c in sessions.calls if isinstance(c, tuple) and c[0] == "cleanup"]
    assert sessions.calls[-1] == ("recycle", CONSOLIDATE_KEY)


@pytest.mark.asyncio
async def test_a_bg_provider_that_cannot_swap_in_place_is_recycled_not_reset():
    """There ``_bg``'s one-liners wait on the entry itself; a reset would pop it under them."""
    sessions = _Sessions()
    with patch(_USAGE_TARGET):
        async with background_turn(sessions, task="skill_dedupe", fresh_conversation=True):
            pass
    assert "new_conversation" not in sessions.calls
    assert not [c for c in sessions.calls if c[0] == "reset"]
    assert sessions.calls[-1] == ("recycle", "_bg")


class _MemberSessions:
    def __init__(self, calls: list, *, remove_fails: bool = False) -> None:
        self.calls = calls
        self._remove_fails = remove_fails

    async def remove(self, key: str) -> None:
        self.calls.append(("remove", key))
        if self._remove_fails:
            raise RuntimeError("still running")

    async def destroy(self, key: str) -> None:
        self.calls.append(("destroy", key))


class _MemberLog:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def delete_memory_consolidation_session(self, key: str, store: str) -> None:
        self.calls.append(("delete_log", key))


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_fails", [False, True])
async def test_a_member_consolidation_cleanup_deletes_the_record_once_retired(remove_fails):
    from kiro_crew.llm_helpers_parts.background_turns import (
        _cleanup_memory_consolidation_session,
    )

    calls: list = []
    client = _CodexLikeClient(calls)
    await _cleanup_memory_consolidation_session(
        _MemberSessions(calls, remove_fails=remove_fails), "k", "store", _MemberLog(calls), client
    )
    if remove_fails:
        assert calls == [("remove", "k")]
    else:
        assert calls == [
            ("remove", "k"),
            ("cleanup", "thread-1"),
            ("destroy", "k"),
            ("delete_log", "k"),
        ]


# ── _call_llm ──


@pytest.mark.asyncio
async def test_a_global_job_asks_for_a_fresh_conversation_on_the_shared_key():
    seen: list[dict] = []

    @contextlib.asynccontextmanager
    async def fake_turn(_sessions, **kwargs):
        seen.append(kwargs)
        yield object()

    with (
        patch.object(history_consolidation, "background_turn", fake_turn),
        patch.object(
            consolidation_pass,
            "_facade_stream_and_collect_json",
            new=AsyncMock(return_value={"ok": True}),
        ),
    ):
        await consolidation_pass._call_llm(_consolidator(object()), "p")

    assert seen[0]["session_key"] == CONSOLIDATE_KEY
    assert seen[0]["fresh_conversation"] is True


@pytest.mark.asyncio
async def test_a_failed_turn_logs_the_model_and_the_prompt_size(caplog):
    @contextlib.asynccontextmanager
    async def fake_turn(_sessions, **_kwargs):
        yield SimpleNamespace(served_model="tiny-window-model")

    consolidator = SimpleNamespace(_sessions=object(), _logger=logging.getLogger("t.fail"))
    with (
        patch.object(history_consolidation, "background_turn", fake_turn),
        patch.object(
            consolidation_pass,
            "_facade_stream_and_collect_json",
            new=AsyncMock(side_effect=RuntimeError("prompt is too long")),
        ),
        caplog.at_level(logging.WARNING, logger="t.fail"),
    ):
        assert await consolidation_pass._call_llm(consolidator, "x" * 1234) is None

    message = "\n".join(r.getMessage() for r in caplog.records)
    assert "model=tiny-window-model" in message
    assert "prompt_chars=1234" in message
