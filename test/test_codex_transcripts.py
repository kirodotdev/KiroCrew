"""A disposable Codex session deletes the rollout codex keeps under ``CODEX_HOME``.

``codex app-server`` writes ``sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl``.
With compression on, the same rollout is rewritten as ``.jsonl.zst``, and
``thread/archive`` moves it to ``archived_sessions/<same name>``. The
incognito/temporary shutdown, the same teardown on the shared runtime, the
subagent prune and ``spawn_release`` delete
every rollout file for the id in both trees, and nothing else: a non-UUID id, a
linked directory or leaf, or a leaf that is not a file deletes nothing and
reports the cleanup as not done.

The store is the one the child used: a session's ``extra_env`` can set
``CODEX_HOME``, so the live teardowns read it from the client or the runtime, and
a subagent run records the resolved root at spawn for the prune and the release.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp.types import ACP_BACKEND_CODEX, ACP_BACKEND_KIRO, PROVIDER_LABEL_CODEX
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.subagent import _cleanup_session_files_sync
from kiro_crew.testing.links import make_dir_link

#: Thread ids in the shape codex 0.164.0 hands out (UUIDv7, lower case).
SID = "01a1c612-b002-72e3-89c4-571c49738762"
OTHER_SID = "01a1c611-b673-7e80-ac7a-9ff336984caf"
_STAMP = "rollout-2026-11-10T15-27-55-"


def _delete(session_id: str, env_overrides: dict[str, str] | None = None) -> bool:
    from kiro_crew.agent_sdk.codex_transcripts import delete_codex_session_files

    if env_overrides is None:
        return delete_codex_session_files(session_id)
    return delete_codex_session_files(session_id, env_overrides)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A ``CODEX_HOME`` with this id's rollout in every name codex gives it."""
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    day = home / "sessions" / "2026" / "11" / "10"
    archive = home / "archived_sessions"
    later_day = home / "sessions" / "2026" / "11" / "11"
    for directory in (day, archive, later_day):
        directory.mkdir(parents=True)
    seeded = [
        day / f"{_STAMP}{SID}.jsonl",
        later_day / f"rollout-2026-11-11T00-01-02-{SID}.jsonl.zst",
        archive / f"{_STAMP}{SID}.jsonl",
    ]
    for path in seeded:
        path.write_text("conversation", encoding="utf-8")
    keep = [
        day / f"{_STAMP}{OTHER_SID}.jsonl",
        archive / f"{_STAMP}{OTHER_SID}.jsonl.zst",
        day / f"{SID}.jsonl",
        day / f"{_STAMP}{SID}.jsonl.bak",
        home / "sessions" / f"{_STAMP}{SID}.jsonl",
        home / "history.jsonl",
    ]
    for path in keep:
        path.write_text("someone else's", encoding="utf-8")
    return {
        "home": home,
        "day": day,
        "archive": archive,
        "seeded": seeded,
        "keep": keep,
    }


def _seeded_gone(store) -> bool:
    return not any(p.exists() for p in store["seeded"])


def _seeded_intact(store) -> bool:
    return all(p.exists() for p in store["seeded"])


def _kept(store) -> bool:
    return all(p.exists() for p in store["keep"])


@pytest.fixture
def override(tmp_path: Path, store):
    """A second store with this id's rollouts, where a session's ``extra_env`` points."""
    home = tmp_path / "override"
    day = home / "sessions" / "2026" / "11" / "10"
    archive = home / "archived_sessions"
    for directory in (day, archive):
        directory.mkdir(parents=True)
    seeded = [day / f"{_STAMP}{SID}.jsonl", archive / f"{_STAMP}{SID}.jsonl.zst"]
    for path in seeded:
        path.write_text("conversation", encoding="utf-8")
    return {"home": home, "seeded": seeded, "env": {"CODEX_HOME": str(home)}}


def test_the_prune_deletes_every_rollout_for_the_id_and_nothing_else(store) -> None:
    recorded = str(store["home"])
    assert _cleanup_session_files_sync(SID, PROVIDER_LABEL_CODEX, codex_home=recorded) is True
    assert _seeded_gone(store)
    assert _kept(store)
    # Only files go: the date directories codex made stay where they are.
    assert store["day"].is_dir() and store["archive"].is_dir()


def test_an_extra_env_codex_home_is_the_store_the_delete_searches(store, override) -> None:
    """The child's ``CODEX_HOME`` is the session's override, not the gateway's."""
    assert _delete(SID, override["env"]) is True
    assert _seeded_gone(override)
    assert _seeded_intact(store)
    # Without the override the gateway's store is searched, and the override's stays.
    for path in override["seeded"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("conversation", encoding="utf-8")
    assert _delete(SID) is True
    assert _seeded_gone(store)
    assert _seeded_intact(override)


def test_an_override_wins_in_the_spawn_merge_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``{**os.environ, **extra_env}``: an empty override unsets, a relative one is unknown."""
    from kiro_crew.agent_sdk.codex_transcripts import codex_home

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "gateway"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert codex_home({"CODEX_HOME": str(tmp_path / "job")}) == tmp_path / "job"
    assert codex_home({"CODEX_HOME": ""}) == tmp_path / ".codex"
    assert codex_home({"CODEX_HOME": "codex"}) is None
    assert codex_home({"OTHER": "x"}) == tmp_path / "gateway"
    assert codex_home(None) == tmp_path / "gateway"


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="codex reads HOME on POSIX only")
def test_an_extra_env_home_moves_the_default_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent_sdk.codex_transcripts import codex_home

    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "gateway-home"))
    assert codex_home({"HOME": str(tmp_path / "job-home")}) == tmp_path / "job-home" / ".codex"
    assert codex_home({"HOME": "relative"}) is None
    assert codex_home({"HOME": ""}) is None
    # A CODEX_HOME in either environment still decides before HOME.
    both = {"HOME": str(tmp_path / "job-home"), "CODEX_HOME": str(tmp_path / "job")}
    assert codex_home(both) == tmp_path / "job"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "gateway"))
    assert codex_home({"HOME": str(tmp_path / "job-home")}) == tmp_path / "gateway"


def test_a_codex_record_without_a_store_deletes_nothing(store) -> None:
    """A prune or release that cannot say which store the child used is not done."""
    assert _cleanup_session_files_sync(SID, PROVIDER_LABEL_CODEX) is False
    assert _seeded_intact(store)


def test_the_prune_deletes_under_the_recorded_store(store, override) -> None:
    recorded = str(override["home"])
    assert _cleanup_session_files_sync(SID, PROVIDER_LABEL_CODEX, codex_home=recorded) is True
    assert _seeded_gone(override)
    assert _seeded_intact(store)


def _aged_codex_run(run_id: str, **identity: str) -> Path:
    """A finished Codex run whose tombstone is past the prune's age, with its identity."""
    from kiro_crew import subagent_persistence as sp

    sp.create_agent_folder(run_id, task="t")
    sp.update_state(run_id, session_id=SID, provider=PROVIDER_LABEL_CODEX, keep=False)
    sp.remember_live_cleanup_identity(
        run_id, session_id=SID, provider=PROVIDER_LABEL_CODEX, keep=False, **identity
    )
    sp.write_tombstone(run_id, cause="timeout", recovery_action="delivered")
    folder = sp._agent_dir(run_id)
    tombstone = folder / "tombstone.json"
    record = json.loads(tombstone.read_text(encoding="utf-8"))
    record["died"] = time.time() - 8 * 86400
    tombstone.write_text(json.dumps(record), encoding="utf-8")
    return folder


def test_the_tombstone_prune_deletes_under_the_store_the_run_recorded(store, override) -> None:
    from kiro_crew import subagent_persistence as sp

    folder = _aged_codex_run("codex-override-run", codex_home=str(override["home"]))
    assert sp.prune_stale_tombstones(max_age_days=7) >= 1
    assert _seeded_gone(override)
    assert _seeded_intact(store)
    assert not folder.exists()


def test_a_run_with_no_recorded_store_keeps_its_retry_record(store) -> None:
    from kiro_crew import subagent_persistence as sp

    folder = _aged_codex_run("codex-unrecorded-run")
    sp.prune_stale_tombstones(max_age_days=7)
    assert _seeded_intact(store)
    assert folder.exists()
    assert sp._tombstone_cleanup_identities("codex-unrecorded-run")


def test_a_subagent_run_records_the_store_its_child_uses(tmp_path: Path) -> None:
    """A run on the shared runtime takes the runtime's ``extra_env`` (a cron parent's)."""
    from kiro_crew.subagent_manager import turn_session

    job_home = str(tmp_path / "job-codex")
    coordinator = SimpleNamespace(
        _manager=SimpleNamespace(_provider_label_of=lambda client: PROVIDER_LABEL_CODEX),
        _publish_identity=MagicMock(),
    )
    info = SimpleNamespace(id="run-1", cwd="", keep=False)
    client = SimpleNamespace(
        session_id=SID,
        cwd=str(tmp_path),
        _runtime=SimpleNamespace(_extra_env={"CODEX_HOME": job_home}),
    )
    turn_session.capture_cleanup_identity(coordinator, info, "subagent:run-1", client, is_cc=False)
    assert coordinator._publish_identity.call_args.kwargs["codex_home"] == job_home
    assert info._session_codex_home == job_home


def test_the_store_is_found_on_each_shape_a_codex_session_takes(tmp_path: Path) -> None:
    """A dedicated provider's client, the runtime it rides, a shared-runtime provider."""
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.agent_sdk.codex_transcripts import recorded_codex_home

    job_home = str(tmp_path / "job-codex")
    runtime = MagicMock()
    runtime._extra_env = {"CODEX_HOME": job_home}
    shared = AcpSessionProvider(MagicMock(), runtime, session_key="subagent:run-1")
    assert recorded_codex_home(shared) == job_home
    dedicated = _provider(ACP_BACKEND_CODEX, "incognito")
    dedicated._client._extra_env = {"CODEX_HOME": job_home}
    assert recorded_codex_home(dedicated) == job_home
    dedicated._client = shared
    assert recorded_codex_home(dedicated) == job_home
    # A holder with no overlay cannot name the store, so nothing is recorded.
    assert recorded_codex_home(SimpleNamespace()) == ""
    assert recorded_codex_home(SimpleNamespace(_extra_env={"CODEX_HOME": "rel"})) == ""


def test_a_session_already_gone_is_a_completed_cleanup(store) -> None:
    assert _delete(SID) is True
    assert _delete(SID) is True


def test_a_home_with_no_store_is_a_completed_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "never-made"))
    assert _delete(SID) is True


@pytest.mark.parametrize("below", [False, True], ids=["root_is_the_link", "root_below_the_link"])
def test_a_store_behind_a_dangling_link_is_not_a_completed_cleanup(
    store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, below: bool
) -> None:
    """The rollouts come back with the link's target, so the retry must stay."""
    seeded = [p.relative_to(store["home"]) for p in store["seeded"]]
    volume = tmp_path / "volume"
    link = tmp_path / "mounted"
    if below:
        volume.mkdir()
        store["home"].rename(volume / "codex")
        home = link / "codex"
    else:
        store["home"].rename(volume)
        home = link
    make_dir_link(link, volume)
    monkeypatch.setenv("CODEX_HOME", str(home))
    away = tmp_path / "unmounted"
    volume.rename(away)
    assert _delete(SID) is False
    away.rename(volume)
    assert all((home / rel).exists() for rel in seeded)
    assert _delete(SID) is True
    assert not any((home / rel).exists() for rel in seeded)


def test_the_default_home_is_dot_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.agent_sdk.codex_transcripts import codex_home

    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert codex_home() == tmp_path / ".codex"
    monkeypatch.setenv("CODEX_HOME", "")
    assert codex_home() == tmp_path / ".codex"


def test_a_relative_codex_home_deletes_nothing(store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", "codex")
    assert _delete(SID) is False
    assert _seeded_intact(store)


@pytest.mark.parametrize(
    "sid",
    [
        pytest.param(f"../{SID}", id="parent_traversal"),
        pytest.param(f"{SID}\x00", id="nul"),
        pytest.param(".", id="dot"),
        pytest.param("..", id="dotdot"),
        pytest.param("", id="empty"),
        pytest.param("not-a-uuid", id="non_uuid"),
        pytest.param(f"{SID}/../{OTHER_SID}", id="embedded_traversal"),
        pytest.param(SID.upper(), id="upper_case"),
        pytest.param("*", id="glob"),
        pytest.param(f"{SID[:-1]}.", id="dot_in_place_of_hex"),
        pytest.param(f"{SID[:8]}{SID[9]}-{SID[10:]}", id="dash_moved"),
        pytest.param("0" * 36, id="no_dashes"),
    ],
)
def test_a_session_id_that_is_not_a_uuid_deletes_nothing(store, sid: str) -> None:
    assert _delete(sid) is False
    assert _seeded_intact(store)
    assert _kept(store)


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param(
            f"sessions/\u00b2\u2070\u00b2\u2076/11/10/{_STAMP}{SID}.jsonl", id="superscript_year"
        ),
        pytest.param(f"sessions/202/11/10/{_STAMP}{SID}.jsonl", id="short_year"),
        pytest.param(f"sessions/2026/1/10/{_STAMP}{SID}.jsonl", id="short_month"),
        pytest.param(f"sessions/2026/11/1x/{_STAMP}{SID}.jsonl", id="letter_in_day"),
        pytest.param(
            f"sessions/2026/11/10/rollout-2026-11-10T15-27-5\u00b2-{SID}.jsonl",
            id="superscript_in_stamp",
        ),
        pytest.param(
            f"sessions/2026/11/10/rollout-2026-11-10 15-27-55-{SID}.jsonl", id="space_for_t"
        ),
        pytest.param(
            f"sessions/2026/11/10/rollout-2026-11-10T15.27.55-{SID}.jsonl", id="dots_in_stamp"
        ),
        pytest.param(
            f"sessions/2026/11/10/rollout-2026-11-10T15-27-555-{SID}.jsonl", id="long_stamp"
        ),
        pytest.param(f"sessions/2026/11/10/rollout--{SID}.jsonl", id="no_stamp"),
        pytest.param(
            f"archived_sessions/rollout-2026-11-10T15-27-55{SID}.jsonl", id="no_dash_before_id"
        ),
        pytest.param(f"archived_sessions/{_STAMP}{SID}.jsonl.zst.tmp", id="extra_suffix"),
        pytest.param(f"archived_sessions/x{_STAMP}{SID}.jsonl", id="text_before_rollout"),
    ],
)
def test_a_name_off_the_codex_shape_is_kept(store, relative: str) -> None:
    """Only codex's exact date directories and rollout names are deleted through."""
    near_miss = store["home"] / relative
    near_miss.parent.mkdir(parents=True, exist_ok=True)
    near_miss.write_text("someone else's", encoding="utf-8")
    assert _delete(SID) is True
    assert _seeded_gone(store)
    assert _kept(store)
    assert near_miss.exists()


def test_a_rollout_the_containment_check_refuses_deletes_nothing(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The containment check is a backstop behind the link screens, and its refusal holds."""
    from kiro_crew.agent_sdk import codex_transcripts

    monkeypatch.setattr(codex_transcripts, "_is_safe_path", lambda path, root: False)
    assert _delete(SID) is False
    assert _seeded_intact(store)
    assert _kept(store)


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("sessions", id="sessions"),
        pytest.param("sessions/2026", id="year"),
        pytest.param("sessions/2026/11/10", id="day"),
        pytest.param("archived_sessions", id="archived_sessions"),
    ],
)
def test_a_linked_directory_deletes_nothing(store, tmp_path: Path, relative: str) -> None:
    linked = store["home"] / relative
    elsewhere = tmp_path / "elsewhere"
    linked.rename(elsewhere)
    make_dir_link(linked, elsewhere)
    behind = list(elsewhere.rglob(f"*{SID}.jsonl*"))
    assert behind
    assert _delete(SID) is False
    assert all(p.exists() for p in behind)
    # Refused before any delete: the rollouts in the other tree are still there.
    assert _seeded_intact(store)


def test_a_linked_leaf_deletes_nothing(
    store, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A directory link at the rollout name: a junction on Windows, so no host skips it.

    The link must be refused as a link. Its target is a directory, so the
    regular-file check would refuse it too, and the log tells the two apart.
    """
    caplog.set_level("ERROR", logger="kiro_crew.agent_sdk.codex_transcripts")
    leaf = store["seeded"][0]
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = outside / leaf.name
    leaf.rename(moved)
    make_dir_link(leaf, outside)
    assert platform_compat.is_link_or_junction(leaf)
    assert _delete(SID) is False
    assert "refusing a linked rollout" in caplog.text
    assert moved.exists()
    assert platform_compat.is_link_or_junction(leaf)
    assert all(p.exists() for p in store["seeded"][1:])


def test_a_leaf_that_is_a_directory_deletes_nothing(store) -> None:
    leaf = store["seeded"][0]
    leaf.unlink()
    (leaf / "inside").mkdir(parents=True)
    assert _delete(SID) is False
    assert (leaf / "inside").is_dir()
    assert all(p.exists() for p in store["seeded"][1:])


def test_a_rollout_rewritten_during_the_cleanup_is_not_reported_done(store) -> None:
    """A live codex can compress the rollout between the scan and the unlink."""
    from kiro_crew.agent_sdk import codex_transcripts

    leaf = store["seeded"][0]
    real_unlink = Path.unlink

    def _compress_then_unlink(self: Path, *args, **kwargs):
        real_unlink(self, *args, **kwargs)
        if self == leaf:
            Path(f"{leaf}.zst").write_text("compressed", encoding="utf-8")

    with patch.object(codex_transcripts.Path, "unlink", _compress_then_unlink):
        assert _delete(SID) is False
    assert Path(f"{leaf}.zst").exists()
    assert _delete(SID) is True


def test_a_refused_or_failed_cleanup_logs_no_session_id(
    store, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A log line names the component and the reason, never the path."""
    caplog.set_level("DEBUG", logger="kiro_crew.agent_sdk.codex_transcripts")
    denied = PermissionError(13, "Permission denied", str(store["seeded"][0]))
    with patch("kiro_crew.agent_sdk.codex_transcripts.Path.unlink", side_effect=denied):
        assert _delete(SID) is False
    archive = store["archive"]
    outside = tmp_path / "outside-archive"
    archive.rename(outside)
    make_dir_link(archive, outside)
    assert _delete(SID) is False
    assert "failed to delete a rollout (Permission denied)" in caplog.text
    assert "refusing a linked archived_sessions directory" in caplog.text
    assert SID not in caplog.text
    assert str(store["home"]) not in caplog.text


def _provider(backend: str, memory_mode: str) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend, memory_mode=memory_mode)
    provider._client = MagicMock()
    provider._client.backend = backend
    provider._client._session_id = SID
    provider._client.shutdown = AsyncMock()
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode", ["incognito", "temporary"])
async def test_a_disposable_codex_chat_deletes_its_rollout_on_shutdown(
    store, memory_mode: str
) -> None:
    await _provider(ACP_BACKEND_CODEX, memory_mode).shutdown()
    assert _seeded_gone(store)
    assert _kept(store)


@pytest.mark.asyncio
async def test_a_persistent_codex_chat_keeps_its_rollout(store) -> None:
    await _provider(ACP_BACKEND_CODEX, "persistent").shutdown()
    assert _seeded_intact(store)


@pytest.mark.asyncio
@pytest.mark.parametrize("on_runtime", [False, True], ids=["on_the_client", "on_its_runtime"])
async def test_a_disposable_codex_chat_deletes_under_its_extra_env_codex_home(
    store, override, on_runtime: bool
) -> None:
    """A cron job's or workflow run's ``env`` moves the store its child writes."""
    provider = _provider(ACP_BACKEND_CODEX, "incognito")
    if on_runtime:
        # After the runtime starts, the client is the provider over the shared runtime.
        provider._client._runtime._extra_env = override["env"]
    else:
        provider._client._extra_env = override["env"]
    await provider.shutdown()
    assert _seeded_gone(override)
    assert _seeded_intact(store)


@pytest.mark.asyncio
async def test_a_kiro_chat_never_reaches_the_codex_store(
    store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.providers import acp as acp_mod

    monkeypatch.setattr(acp_mod, "kiro_sessions_dir", lambda: tmp_path / "cli")
    await _provider(ACP_BACKEND_KIRO, "incognito").shutdown()
    assert _seeded_intact(store)


@pytest.mark.asyncio
async def test_the_codex_delete_runs_off_the_event_loop(store) -> None:
    from kiro_crew.agent_sdk import codex_transcripts

    threads: list[int] = []
    real_unlink = Path.unlink

    def _recording_unlink(self: Path, *args, **kwargs):
        if SID in self.name:
            threads.append(threading.get_ident())
        return real_unlink(self, *args, **kwargs)

    with patch.object(codex_transcripts.Path, "unlink", _recording_unlink):
        await _provider(ACP_BACKEND_CODEX, "incognito").shutdown()
    assert threads and threading.get_ident() not in threads
    assert _seeded_gone(store)


def _shared_handle(memory_mode: str, backend: str = ACP_BACKEND_CODEX):
    """A session on the shared runtime, as a subagent of a chat gets one."""
    from kiro_crew.acp.session_handle import AcpSessionHandle

    with patch.object(AcpSessionHandle, "__init__", lambda self: None):
        handle = AcpSessionHandle()  # type: ignore[call-arg]
    handle._session_id = SID
    handle.keep_transcript = False
    handle.memory_mode = memory_mode
    handle._runtime = MagicMock()
    handle._runtime.acp_backend = backend
    handle._runtime.terminate_session = AsyncMock()
    return handle


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode", ["incognito", "temporary"])
async def test_a_disposable_codex_session_on_the_shared_runtime_deletes_its_rollout(
    store, memory_mode: str
) -> None:
    """It ends through the handle's teardown, which never reaches ``AcpProvider``."""
    await _shared_handle(memory_mode).destroy()
    assert _seeded_gone(store)
    assert _kept(store)


@pytest.mark.asyncio
async def test_a_persistent_codex_session_on_the_shared_runtime_keeps_its_rollout(store) -> None:
    await _shared_handle("persistent").destroy()
    assert _seeded_intact(store)


@pytest.mark.asyncio
async def test_a_shared_runtime_session_deletes_under_the_runtime_extra_env(
    store, override
) -> None:
    """The runtime's child ran with its ``extra_env``, so the teardown reads that store."""
    handle = _shared_handle("incognito")
    handle._runtime._extra_env = override["env"]
    await handle.destroy()
    assert _seeded_gone(override)
    assert _seeded_intact(store)


@pytest.mark.asyncio
async def test_a_kiro_session_on_the_shared_runtime_never_reaches_the_codex_store(
    store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.acp import session_handle

    monkeypatch.setattr(session_handle, "kiro_sessions_dir", lambda: tmp_path / "cli")
    await _shared_handle("incognito", ACP_BACKEND_KIRO).destroy()
    assert _seeded_intact(store)


def test_the_codex_harness_alone_names_a_disposable_record_delete() -> None:
    """The shared runtime asks the harness's teardown, not the backend id (H6)."""
    from kiro_crew.acp.harness import _HARNESSES, harness_for
    from kiro_crew.agent_sdk.codex_transcripts import delete_codex_session_files

    named = {
        backend: harness_for(backend).teardown.delete_disposable_record for backend in _HARNESSES
    }
    assert named.pop(ACP_BACKEND_CODEX) is delete_codex_session_files
    assert set(named.values()) == {None}
