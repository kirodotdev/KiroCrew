"""A disposable Claude session deletes Claude Code's own transcript store.

claude-agent-acp's ``claude`` CLI keeps ``<config>/projects/<project>/<sid>.jsonl``,
``<config>/projects/<project>/<sid>/`` and ``<config>/file-history/<sid>/``. The
incognito/temporary shutdown, the subagent prune and ``spawn_release`` delete those
three, and nothing else: a non-UUID id, an empty cwd or a symlinked component
deletes nothing and reports the cleanup as not done.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import subagent as sa
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO, PROVIDER_LABEL_CLAUDE
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.subagent import SubagentManager, _cleanup_session_files_sync

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
OTHER_SID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def _project_name(cwd: str) -> str:
    """The CLI's spelling, restated here so the test pins it independently."""
    return re.sub(r"[^a-zA-Z0-9]", "-", os.path.realpath(cwd))


def _delete(session_id: str, cwd: str) -> bool:
    from kiro_crew.agent_sdk.claude_transcripts import delete_claude_session_files

    return delete_claude_session_files(session_id, cwd)


def _symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symlinks")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A Claude config dir and a work dir reached through a symlinked parent."""
    config = tmp_path / "cc"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    real_parent = tmp_path / "real"
    (real_parent / "repo").mkdir(parents=True)
    _symlink(tmp_path / "linked", real_parent)
    work_dir = str(tmp_path / "linked" / "repo")
    project = config / "projects" / _project_name(work_dir)
    project.mkdir(parents=True)
    paths = [
        project / f"{SID}.jsonl",
        project / SID / "subagents" / "agent-1.jsonl",
        config / "file-history" / SID / "abc@v1",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("conversation", encoding="utf-8")
    keep = [project / f"{OTHER_SID}.jsonl", config / "file-history" / OTHER_SID / "x"]
    for path in keep:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("someone else's", encoding="utf-8")
    return {"config": config, "work_dir": work_dir, "project": project, "keep": keep}


def _seeded_gone(store) -> bool:
    project, config = store["project"], store["config"]
    return not any(
        p.exists() for p in (project / f"{SID}.jsonl", project / SID, config / "file-history" / SID)
    )


def _seeded_intact(store) -> bool:
    project, config = store["project"], store["config"]
    return all(
        p.exists() for p in (project / f"{SID}.jsonl", project / SID, config / "file-history" / SID)
    )


def test_the_project_name_is_spelled_from_the_resolved_path(store) -> None:
    from kiro_crew.agent_sdk.claude_transcripts import claude_project_dir_name

    assert claude_project_dir_name(store["work_dir"]) == store["project"].name
    assert "linked" not in store["project"].name
    assert claude_project_dir_name("") is None


def test_the_prune_deletes_all_three_paths_and_nothing_else(store) -> None:
    assert _cleanup_session_files_sync(SID, PROVIDER_LABEL_CLAUDE, cwd=store["work_dir"]) is True
    assert _seeded_gone(store)
    assert all(p.exists() for p in store["keep"])


def test_a_session_already_gone_is_a_completed_cleanup(store) -> None:
    assert _delete(SID, store["work_dir"]) is True
    assert _delete(SID, store["work_dir"]) is True


def test_an_empty_cwd_deletes_nothing_and_keeps_the_retry(store) -> None:
    assert _cleanup_session_files_sync(SID, PROVIDER_LABEL_CLAUDE, cwd="") is False
    assert _seeded_intact(store)


@pytest.mark.parametrize(
    "sid",
    [
        pytest.param(f"../{SID}", id="parent_traversal"),
        pytest.param(f"{SID}\x00", id="nul"),
        pytest.param(".", id="dot"),
        pytest.param("..", id="dotdot"),
        pytest.param("not-a-uuid", id="non_uuid"),
        pytest.param(f"{SID}/../{OTHER_SID}", id="embedded_traversal"),
    ],
)
def test_a_session_id_that_is_not_a_uuid_deletes_nothing(store, sid: str) -> None:
    assert _cleanup_session_files_sync(sid, PROVIDER_LABEL_CLAUDE, cwd=store["work_dir"]) is False
    assert _seeded_intact(store)
    assert all(p.exists() for p in store["keep"])


def test_a_symlinked_project_dir_deletes_nothing(store, tmp_path: Path) -> None:
    project = store["project"]
    elsewhere = tmp_path / "elsewhere"
    project.rename(elsewhere)
    _symlink(project, elsewhere)
    assert _delete(SID, store["work_dir"]) is False
    assert (elsewhere / f"{SID}.jsonl").exists() and (elsewhere / SID).exists()
    assert (store["config"] / "file-history" / SID).exists()


def test_a_symlinked_leaf_deletes_nothing(store, tmp_path: Path) -> None:
    history = store["config"] / "file-history" / SID
    outside = tmp_path / "outside-history"
    history.rename(outside)
    _symlink(history, outside)
    assert _delete(SID, store["work_dir"]) is False
    assert outside.exists()
    assert (store["project"] / f"{SID}.jsonl").exists()


def test_a_refused_or_failed_cleanup_logs_no_session_id_or_cwd(
    store, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A log line names the component and the reason, never the path."""
    caplog.set_level("DEBUG", logger="kiro_crew.agent_sdk.claude_transcripts")
    denied = PermissionError(13, "Permission denied", str(store["project"] / SID))
    with patch("kiro_crew.agent_sdk.claude_transcripts.shutil.rmtree", side_effect=denied):
        assert _delete(SID, store["work_dir"]) is False
    history = store["config"] / "file-history" / SID
    outside = tmp_path / "outside-history"
    history.rename(outside)
    _symlink(history, outside)
    assert _delete(SID, store["work_dir"]) is False
    assert "failed to delete the session directory (Permission denied)" in caplog.text
    assert "refusing the file-history entry" in caplog.text
    assert SID not in caplog.text
    assert _project_name(store["work_dir"]) not in caplog.text


def test_a_cwd_too_long_to_spell_deletes_nothing(store) -> None:
    assert _delete(SID, "/" + "a" * 250) is False
    assert _seeded_intact(store)


def _provider(backend: str, memory_mode: str, work_dir: str) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend, memory_mode=memory_mode)
    provider._client = MagicMock()
    provider._client.backend = backend
    provider._client._session_id = SID
    provider._client._work_dir = Path(work_dir)
    provider._client.shutdown = AsyncMock()
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode", ["incognito", "temporary"])
async def test_a_disposable_claude_chat_deletes_its_transcript_on_shutdown(
    store, memory_mode: str
) -> None:
    await _provider(ACP_BACKEND_CLAUDE, memory_mode, store["work_dir"]).shutdown()
    assert _seeded_gone(store)
    assert all(p.exists() for p in store["keep"])


@pytest.mark.asyncio
async def test_a_persistent_claude_chat_keeps_its_transcript(store) -> None:
    await _provider(ACP_BACKEND_CLAUDE, "persistent", store["work_dir"]).shutdown()
    assert _seeded_intact(store)


@pytest.mark.asyncio
async def test_a_kiro_chat_never_reaches_the_claude_store(
    store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.providers import acp as acp_mod

    monkeypatch.setattr(acp_mod, "kiro_sessions_dir", lambda: tmp_path / "cli")
    await _provider(ACP_BACKEND_KIRO, "incognito", store["work_dir"]).shutdown()
    assert _seeded_intact(store)


def _manager() -> SubagentManager:
    sessions = MagicMock()
    sessions.forget_conversation = MagicMock(return_value=SID)
    sessions.conversation_provider = MagicMock(return_value=PROVIDER_LABEL_CLAUDE)
    sessions.reset = AsyncMock()
    return SubagentManager(sessions=sessions, ctx_builder=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_spawn_release_disposes_a_claude_session_at_its_recorded_cwd(store) -> None:
    record = {"session_id": SID, "provider": PROVIDER_LABEL_CLAUDE, "cwd": store["work_dir"]}
    with (
        patch.object(sa, "update_state"),
        patch(
            "kiro_crew.subagent_persistence.trusted_cleanup_identity_record",
            return_value=record,
        ) as lookup,
    ):
        assert await _manager().release_conversation_async("c1") == (True, "released")
    lookup.assert_called_once_with("c1", SID, "subagent:c1")
    assert _seeded_gone(store)


@pytest.mark.asyncio
async def test_spawn_release_without_a_trusted_cwd_is_not_disposed(store) -> None:
    with (
        patch.object(sa, "update_state"),
        patch(
            "kiro_crew.subagent_persistence.trusted_cleanup_identity_record",
            return_value=None,
        ),
    ):
        ok, detail = await _manager().release_conversation_async("c1")
    assert ok is True and detail.startswith("not_disposed:")
    assert _seeded_intact(store)
