"""Remote sub-agent project snapshots and the crew-side workspace install."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from remote_subagent_helpers import (
    _PINNED_INSTALL,
    _auto_manager,
    _commit_all,
    _expired_snapshot,
    _git_project,
    _Instances,
    _Manager,
    _names,
    _ownership_request,
    _pax_payload,
    _settle_monitor,
    _spawn_kwargs,
    _spawn_placed,
    _tar_payload,
    _WorkspaceRequest,
    gzip_decompress,
)

from kiro_crew.dashboard import remote_snapshot, remote_subagents, remote_workspaces
from kiro_crew.dashboard.remote_subagents import RemoteSubagentError, RemoteSubagentService
from kiro_crew.dashboard.remote_workspaces import (
    WorkspaceArchiveRejected,
    api_remote_workspace_upload,
    install_workspace,
)

# The local-path cases drive SubagentManager.spawn; pin the host-memory reading
# so a memory-pressured runner does not queue them.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.fixture
def read_policy(tmp_path, monkeypatch):
    """Install a governance ceiling for one test, with no profiles bound."""
    import dataclasses

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform import governance_profiles as gp
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.governance import parse_policy

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()

    def install(*, deny: tuple[str, ...] = (), allow: tuple[str, ...] = ()) -> None:
        rules = (
            {"mode": "allow", "allow": list(allow)}
            if allow
            else {"mode": "deny", "deny": list(deny)}
        )
        body = {"version": 1, "boot": {"fail_closed": True}, "filesystem": {"read": rules}}
        base = build_default_context(KiroCrewConfig.load())
        ctx_mod.set_context(dataclasses.replace(base, governance=parse_policy(body)))

    yield install
    gp.reset_store()
    ctx_mod.reset_context()


@_PINNED_INSTALL
def test_workspace_install_is_content_addressed_and_rejects_unsafe_members(
    tmp_path,
) -> None:
    payload = _tar_payload(
        [
            ("README.md", b"hello\n", 0o644, "file"),
            ("scripts/run.sh", b"#!/bin/sh\n", 0o755, "file"),
        ]
    )
    digest = hashlib.sha256(payload).hexdigest()

    path = install_workspace(payload, digest, "a" * 40, root=tmp_path)

    assert (path / "README.md").read_text(encoding="utf-8") == "hello\n"
    if os.name != "nt":
        # Windows has no POSIX execute bit; the mode is preserved only on POSIX.
        assert (path / "scripts/run.sh").stat().st_mode & 0o111
    # Same digest, fresh tree: a second run never starts in the first one's edits.
    (path / "README.md").write_text("edited by run 1\n", encoding="utf-8")
    second = install_workspace(payload, digest, "a" * 40, root=tmp_path)
    assert second != path and second.name[:24] == path.name[:24] == digest[:24]
    assert (second / "README.md").read_text(encoding="utf-8") == "hello\n"

    for bad in (
        _tar_payload([("../escape", b"x", 0o644, "file")]),
        _tar_payload([("nested//file", b"x", 0o644, "file")]),
        _tar_payload([("nested/./file", b"x", 0o644, "file")]),
        _tar_payload([("../link", b"/etc/passwd", 0o777, "symlink")]),
    ):
        with pytest.raises(WorkspaceArchiveRejected):
            install_workspace(bad, hashlib.sha256(bad).hexdigest(), "b" * 40, root=tmp_path)


def test_an_extended_header_past_the_stream_bound_is_refused_before_it_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GPT F1: the expansion counter only sees member sizes, so the bound has to sit
    on the decompressed stream, and refuse the oversized read before it allocates."""
    import gzip

    from kiro_crew.dashboard import remote_workspaces

    bound = 64 * 1024
    monkeypatch.setattr(remote_workspaces, "_MAX_STREAM_BYTES", bound)
    largest: list[int] = []
    real_read = gzip.GzipFile.read

    def spying_read(self, size=-1):
        largest.append(size)
        return real_read(self, size)

    monkeypatch.setattr(gzip.GzipFile, "read", spying_read)

    remote_workspaces._validate_archive(_pax_payload(1024))  # under the bound: fine
    largest.clear()
    with pytest.raises(WorkspaceArchiveRejected, match="workspace limit"):
        remote_workspaces._validate_archive(_pax_payload(4 * bound))
    # Nothing past the bound ever reached the decompressor.
    assert largest and max(largest) <= bound


@_PINNED_INSTALL
def test_workspace_install_skips_tracked_symlinks_instead_of_rejecting(tmp_path) -> None:
    # build_source_tarball emits a symlink member for every tracked symlink; a
    # repo with one must still sync, and the link must never be created.
    payload = _tar_payload(
        [
            ("README.md", b"hello\n", 0o644, "file"),
            ("link", b"/etc/passwd", 0o777, "symlink"),
        ]
    )
    path = install_workspace(payload, hashlib.sha256(payload).hexdigest(), "a" * 40, root=tmp_path)

    assert (path / "README.md").is_file()
    assert not (path / "link").exists() and not (path / "link").is_symlink()


@_PINNED_INSTALL
@pytest.mark.parametrize("planted", ["file-link", "parent-link"])
def test_extraction_never_writes_through_a_link_planted_in_staging(
    tmp_path, monkeypatch: pytest.MonkeyPatch, planted: str
) -> None:
    # A same-user agent can see the staging directory while the gateway fills
    # it. A link it plants at a name the archive will write must fail the
    # create, not redirect the write onto a protected file.
    base = tmp_path / "workspaces"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "security_policy.json"
    victim.write_text("ceiling\n", encoding="utf-8")
    payload = _tar_payload(
        [
            ("pkg/injected.py", b"overwritten\n", 0o644, "file"),
            ("security_policy.json", b"overwritten\n", 0o644, "file"),
        ]
    )

    def plant(staging: Path) -> None:
        if planted == "file-link":
            (staging / "security_policy.json").symlink_to(victim)
        else:
            (staging / "pkg").symlink_to(outside, target_is_directory=True)

    real = remote_workspaces._extract_pinned

    def _racing(archive: tarfile.TarFile, staging_fd: int) -> None:
        (staging,) = [p for p in base.iterdir() if p.name.startswith(".")]
        plant(staging)
        real(archive, staging_fd)

    monkeypatch.setattr(remote_workspaces, "_extract_pinned", _racing)

    with pytest.raises(WorkspaceArchiveRejected):
        install_workspace(payload, hashlib.sha256(payload).hexdigest(), "a" * 40, root=base)

    assert victim.read_text(encoding="utf-8") == "ceiling\n"
    assert sorted(p.name for p in outside.iterdir()) == ["security_policy.json"]
    assert list(base.iterdir()) == []


@_PINNED_INSTALL
def test_a_staging_tree_swapped_before_publish_is_not_published(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The publish rename is by name, so the name must still be the directory
    # the gateway filled; a link swapped in for it is refused and removed.
    base = tmp_path / "workspaces"
    base.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    payload = _tar_payload([("README.md", b"hello\n", 0o644, "file")])
    real = remote_workspaces._write_marker

    def _swap(staging_fd: int, digest: str, commit: str) -> None:
        real(staging_fd, digest, commit)
        (staging,) = [p for p in base.iterdir() if p.name.startswith(".")]
        staging.rename(tmp_path / "moved")
        staging.symlink_to(elsewhere, target_is_directory=True)

    monkeypatch.setattr(remote_workspaces, "_write_marker", _swap)

    with pytest.raises(WorkspaceArchiveRejected, match="replaced"):
        install_workspace(payload, hashlib.sha256(payload).hexdigest(), "a" * 40, root=base)

    assert list(base.iterdir()) == []
    assert list(elsewhere.iterdir()) == []


def test_a_platform_without_pinned_creates_refuses_the_install(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No by-name fallback: that is the write-through-a-link hole itself.
    monkeypatch.setattr(remote_workspaces, "_can_extract_pinned", lambda: False)
    payload = _tar_payload([("README.md", b"hello\n", 0o644, "file")])

    with pytest.raises(WorkspaceArchiveRejected, match="pinned"):
        install_workspace(payload, hashlib.sha256(payload).hexdigest(), "a" * 40, root=tmp_path)

    assert list(tmp_path.iterdir()) == []


@_PINNED_INSTALL
def test_workspace_prune_spares_snapshots_in_use(tmp_path) -> None:
    from kiro_crew.dashboard.remote_workspaces import _prune_workspaces

    def install(name: bytes) -> Path:
        payload = _tar_payload([("f", name, 0o644, "file")])
        return install_workspace(
            payload, hashlib.sha256(payload).hexdigest(), "a" * 40, root=tmp_path
        )

    busy, idle = install(b"busy"), install(b"idle")
    old = time.time() - 8 * 24 * 60 * 60
    for path in (busy, idle):
        os.utime(path, (old, old))

    assert _prune_workspaces(tmp_path, keep="", in_use={busy.name}) == 1
    assert busy.is_dir() and not idle.exists()


@pytest.mark.asyncio
async def test_a_queued_runs_workspace_counts_as_in_use(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run waiting for memory is in the queued listing, not in ``all_agents``."""
    from kiro_crew.dashboard import remote_workspaces
    from kiro_crew.subagent_manager.admission.types import QueuedRun, QueuedRunListing

    monkeypatch.setattr(remote_workspaces, "_workspace_root", lambda: tmp_path)
    queued_name = "a" * 24 + "-0000aaaa"
    live_name = "b" * 24 + "-0000bbbb"
    listing = QueuedRunListing(
        (
            QueuedRun(
                id="q1", task="t", parent_session_key="", cwd=str(tmp_path / queued_name / "src")
            ),
        )
    )
    manager = SimpleNamespace(
        all_agents=[SimpleNamespace(done=False, cwd=str(tmp_path / live_name))],
        queued_runs_async=AsyncMock(return_value=listing),
    )

    names = await remote_workspaces._workspaces_in_use(_ownership_request(manager))

    assert names == {queued_name, live_name}


@pytest.mark.asyncio
async def test_a_run_admitted_during_the_queued_read_still_counts_as_in_use(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run leaves the queue while the listing is read; it must be seen live."""
    from kiro_crew.dashboard import remote_workspaces
    from kiro_crew.subagent_manager.admission.types import QueuedRunListing

    monkeypatch.setattr(remote_workspaces, "_workspace_root", lambda: tmp_path)
    name = "c" * 24 + "-0000cccc"
    manager = SimpleNamespace(all_agents=[])

    async def admitted_meanwhile(_parent):
        manager.all_agents = [SimpleNamespace(done=False, cwd=str(tmp_path / name))]
        return QueuedRunListing(())

    manager.queued_runs_async = admitted_meanwhile

    assert await remote_workspaces._workspaces_in_use(_ownership_request(manager)) == {name}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["partial", "raises", "no-listing"])
async def test_ownership_that_cannot_be_read_completely_is_none(
    tmp_path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from kiro_crew.dashboard import remote_workspaces
    from kiro_crew.subagent_manager.admission.types import QueuedRunListing

    monkeypatch.setattr(remote_workspaces, "_workspace_root", lambda: tmp_path)
    manager = SimpleNamespace(all_agents=[])
    if failure == "partial":
        manager.queued_runs_async = AsyncMock(return_value=QueuedRunListing((), partial=True))
    elif failure == "raises":
        manager.queued_runs_async = AsyncMock(side_effect=RuntimeError("store down"))

    assert await remote_workspaces._workspaces_in_use(_ownership_request(manager)) is None


@_PINNED_INSTALL
def test_unknown_ownership_prunes_nothing(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_workspaces._WORKSPACE_TTL_SECS", 1)
    first = _tar_payload([("one.txt", b"one", 0o644, "file")])
    first_path = install_workspace(
        first, hashlib.sha256(first).hexdigest(), "a" * 40, root=tmp_path
    )
    old = time.time() - 10
    os.utime(first_path, (old, old))

    second = _tar_payload([("two.txt", b"two", 0o644, "file")])
    install_workspace(
        second, hashlib.sha256(second).hexdigest(), "b" * 40, root=tmp_path, in_use=None
    )

    assert first_path.is_dir()


def test_a_queued_run_keeps_its_cwd_for_the_ownership_read() -> None:
    from unittest.mock import MagicMock

    from kiro_crew.subagent import SubagentManager

    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    run = manager._admission._queued_run("q1", {"task": "t", "cwd": "/w/x"}, "p", 0.0, "")
    assert run.cwd == "/w/x"


@pytest.mark.asyncio
async def test_remote_spawn_syncs_parent_project_when_no_remote_cwd(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    payload = b"tracked-source"
    digest = hashlib.sha256(payload).hexdigest()
    instances = _Instances(
        [
            (
                200,
                {
                    "path": "/srv/peer-snapshots/abc",
                    "sha256": digest,
                    "commit": "a" * 40,
                },
            ),
            (200, {"id": "peer01"}),
            (200, {"done": True, "result": "ok", "error": ""}),
        ],
        connected=("crew-a",),
    )
    manager = _Manager()
    manager.external_agents.clear()
    state = SimpleNamespace(
        instances_manager=instances,
        get_slot=lambda _name: SimpleNamespace(project=str(tmp_path)),
    )
    service = RemoteSubagentService(state, manager)  # type: ignore[arg-type]
    identities: list[tuple[str, ...]] = []

    def archive(_path: object, *identity: str) -> tuple[bytes, str, str]:
        identities.append(identity)
        return payload, digest, "a" * 40

    monkeypatch.setattr(service, "_build_project_archive", archive)

    info = await _spawn_placed(
        service,
        task="review",
        parent_session="dashboard:chat-1",
        parent_agent="researcher",
        agent="",
        max_turns=0,
        cwd="",
        model="",
        reasoning_effort="",
        include_memory=False,
        include_lessons=False,
        include_project=True,
        memory_mode="temporary",
        batch_id="",
        batch_total=0,
    )
    await _settle_monitor(service, info.id)

    assert instances.calls[0][:3] == ("crew-a", "POST", "api/remote-workspaces")
    # The snapshot is read as the parent's agent, whose profile narrows it.
    assert identities == [("dashboard:chat-1", "", "researcher")]
    spawn_body = instances.calls[1][3]
    assert isinstance(spawn_body, dict)
    assert spawn_body["cwd"] == "/srv/peer-snapshots/abc"
    assert info.cwd == "/srv/peer-snapshots/abc"
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent_session", "slot"),
    (
        ("slack:C123:1712793600.123456", None),
        ("cron:job-1", None),
        ("dashboard:chat-1", SimpleNamespace(project="")),
    ),
)
async def test_remote_spawn_without_a_parent_project_runs_without_a_snapshot(
    monkeypatch: pytest.MonkeyPatch, parent_session: str, slot: object
) -> None:
    """``include_project`` is on by default, so a parent with no project (a
    channel, cron or CLI parent, or a project-less chat) must still spawn:
    no snapshot is uploaded and the peer gets no cwd."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    instances = _Instances(
        [
            (200, {"id": "peer01"}),
            (200, {"done": True, "result": "ok", "error": ""}),
        ],
        connected=("crew-a",),
    )
    manager = _Manager()
    manager.external_agents.clear()
    state = SimpleNamespace(instances_manager=instances, get_slot=lambda _name: slot)
    service = RemoteSubagentService(state, manager)  # type: ignore[arg-type]

    def no_archive(_path: object, *_identity: str) -> tuple[bytes, str, str]:
        raise AssertionError("no project, so nothing may be packaged")

    monkeypatch.setattr(service, "_build_project_archive", no_archive)

    info = await _spawn_placed(
        service,
        task="review",
        parent_session=parent_session,
        agent="",
        max_turns=0,
        cwd="",
        model="",
        reasoning_effort="",
        include_memory=False,
        include_lessons=False,
        include_project=True,
        memory_mode="temporary",
        batch_id="",
        batch_total=0,
    )
    await _settle_monitor(service, info.id)

    assert [call[:3] for call in instances.calls][0] == ("crew-a", "POST", "api/spawn")
    assert not any(call[2] == "api/remote-workspaces" for call in instances.calls)
    spawn_body = instances.calls[0][3]
    assert isinstance(spawn_body, dict)
    assert "cwd" not in spawn_body
    assert info.cwd == ""
    await service.close()


@pytest.mark.asyncio
async def test_remote_spawn_refuses_a_parent_project_that_is_gone(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = tmp_path / "removed"
    instances = _Instances([], connected=("crew-a",))
    state = SimpleNamespace(
        instances_manager=instances,
        get_slot=lambda _name: SimpleNamespace(project=str(gone)),
    )
    service = RemoteSubagentService(state, _Manager())  # type: ignore[arg-type]

    with pytest.raises(RemoteSubagentError) as caught:
        await _spawn_placed(
            service,
            task="review",
            parent_session="dashboard:chat-1",
            agent="",
            max_turns=0,
            cwd="",
            model="",
            reasoning_effort="",
            include_memory=False,
            include_lessons=False,
            include_project=True,
            memory_mode="temporary",
            batch_id="",
            batch_total=0,
        )
    assert caught.value.code == "remote_project_unavailable"
    assert instances.calls == []
    await service.close()


@pytest.mark.asyncio
async def test_the_parent_project_is_resolved_off_the_event_loop(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1: resolving the parent's project and checking it is a directory ran on
    the gateway loop, so a project on a stalled mount froze every session until the
    mount answered. Only the slot is read on the loop; the path work runs in the
    archive worker."""
    import threading

    instances = _Instances([], connected=("crew-a",))
    state = SimpleNamespace(
        instances_manager=instances,
        get_slot=lambda _name: SimpleNamespace(project=str(tmp_path / "removed")),
    )
    service = RemoteSubagentService(state, _Manager())  # type: ignore[arg-type]
    real = RemoteSubagentService._resolve_parent_project
    threads: list[threading.Thread] = []

    def resolve(raw: str):
        threads.append(threading.current_thread())
        return real(raw)

    monkeypatch.setattr(RemoteSubagentService, "_resolve_parent_project", staticmethod(resolve))
    loop_thread = threading.current_thread()

    with pytest.raises(RemoteSubagentError) as caught:
        await service._sync_parent_project("crew-a", "dashboard:chat-1")
    assert caught.value.code == "remote_project_unavailable"
    assert len(threads) == 1 and threads[0] is not loop_thread
    await service.close()


@pytest.mark.asyncio
async def test_workspace_route_accepts_only_owner_cookie_requests(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for request in (
        _WorkspaceRequest(user="owner", internal_auth=True),
        _WorkspaceRequest(user="owner", app="notes"),
        _WorkspaceRequest(user="other"),
    ):
        response = await api_remote_workspace_upload(cast(Any, request))
        assert response.status == 403

    installed = tmp_path / "workspace"
    monkeypatch.setattr(
        "kiro_crew.dashboard.remote_workspaces.install_workspace",
        lambda _payload, _digest, _commit, **_kwargs: installed,
    )
    response = await api_remote_workspace_upload(cast(Any, _WorkspaceRequest(user="owner")))
    assert response.status == 200
    assert isinstance(response.body, (bytes, bytearray))
    payload = json.loads(response.body)
    assert payload["path"] == str(installed)
    assert "reused" not in payload


@_PINNED_INSTALL
def test_workspace_install_prunes_only_expired_marked_snapshots(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_workspaces._WORKSPACE_TTL_SECS", 1)
    first = _tar_payload([("one.txt", b"one", 0o644, "file")])
    first_path = install_workspace(
        first,
        hashlib.sha256(first).hexdigest(),
        "a" * 40,
        root=tmp_path,
    )
    old = time.time() - 10
    os.utime(first_path, (old, old))

    unmarked = tmp_path / ("f" * 24 + "-" + "0" * 8)
    unmarked.mkdir()
    os.utime(unmarked, (old, old))

    second = _tar_payload([("two.txt", b"two", 0o644, "file")])
    second_path = install_workspace(
        second,
        hashlib.sha256(second).hexdigest(),
        "b" * 40,
        root=tmp_path,
    )

    assert second_path.is_dir()
    assert not first_path.exists()
    assert unmarked.is_dir()


def test_build_project_archive_uses_the_staged_tarball(tmp_path) -> None:
    import hashlib

    from kiro_crew.dashboard.remote_subagents import RemoteSubagentService

    head = _git_project(tmp_path)

    payload, digest, commit = RemoteSubagentService._build_project_archive(tmp_path)

    assert payload
    assert digest == hashlib.sha256(payload).hexdigest()
    assert commit == head
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        assert any(name.endswith("app.py") for name in archive.getnames())


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs a privilege on Windows")
def test_an_ancestor_swapped_for_a_link_after_resolution_is_refused(tmp_path, monkeypatch) -> None:
    """The project was resolved and screened, then an ancestor became a link into
    a masked directory: the pin refuses it instead of reading through it."""
    from kiro_crew.dashboard.remote_subagents import _snapshot_project

    project = tmp_path / "checkout" / "cache"
    project.mkdir(parents=True)
    _git_project(project)
    masked = tmp_path / "masked" / "cache"
    masked.mkdir(parents=True)
    _git_project(masked)
    (masked / "app.py").write_bytes(b"token\n")
    (tmp_path / "checkout").rename(tmp_path / "checkout.real")
    (tmp_path / "checkout").symlink_to(tmp_path / "masked", target_is_directory=True)
    # Resolution and screening happened before the swap.
    monkeypatch.setattr(remote_subagents.os.path, "realpath", lambda path: str(path))

    with pytest.raises(remote_subagents._SnapshotRefused) as caught:
        _snapshot_project(project)
    assert caught.value.code == "remote_project_moved"


@pytest.mark.skipif(os.name == "nt", reason="the pinned handle blocks the rename on Windows")
def test_a_directory_swapped_in_while_git_runs_is_refused(tmp_path, monkeypatch) -> None:
    """git lists by path, so a listing taken from a swapped-in tree is not packaged."""
    from kiro_crew.dashboard.remote_subagents import _snapshot_project

    project = tmp_path / "project"
    project.mkdir()
    _git_project(project)
    real_git = remote_snapshot._project_git

    def swapping_git(git: str, root: Path, *argv: str) -> Any:
        result = real_git(git, root, *argv)
        if argv[0] == "ls-files":
            (tmp_path / "project").rename(tmp_path / "project.real")
            (tmp_path / "project").mkdir()
        return result

    monkeypatch.setattr(remote_snapshot, "_project_git", swapping_git)
    with pytest.raises(remote_subagents._SnapshotRefused) as caught:
        _snapshot_project(project)
    assert caught.value.code == "remote_project_moved"


@pytest.mark.parametrize("mode", ["deny", "allow"])
def test_the_snapshot_leaves_out_a_file_the_parents_read_policy_denies(
    tmp_path, read_policy, mode, monkeypatch
) -> None:
    """The gateway reads the snapshot, so the parent's read policy is applied here,
    and each denial is written to the SEL like the PreToolUse gate's own."""
    from kiro_crew import sel as sel_mod
    from kiro_crew.dashboard.remote_subagents import RemoteSubagentService

    audited: list[dict] = []

    class _Recorder:
        def log_governance_decision(self, **kwargs: object) -> None:
            audited.append(kwargs)

    monkeypatch.setattr(sel_mod, "sel", lambda: _Recorder())

    project = tmp_path / "project"
    project.mkdir()
    _git_project(project)
    (project / "docs").mkdir()
    (project / "docs" / "private.md").write_bytes(b"keep out\n")
    (project / "docs" / "public.md").write_bytes(b"fine\n")
    _commit_all(project, "docs/private.md", "docs/public.md")
    if mode == "deny":
        # Path globs match the native spelling of the absolute item.
        read_policy(deny=(f"**{os.sep}private.md",))
    else:
        read_policy(allow=(str(project / "app.py"), str(project / "docs" / "public.md")))

    payload, _digest, _commit = RemoteSubagentService._build_project_archive(
        project, "dashboard:parent", ""
    )

    names = _names(payload)
    assert "docs/private.md" not in names
    assert {"app.py", "docs/public.md"} <= names
    denied = [row for row in audited if row["item"].endswith("private.md")]
    assert len(denied) == 1
    row = denied[0]
    assert row["session_key"] == "dashboard:parent"
    assert row["scope"] == "filesystem.read" and row["outcome"] == "denied"
    assert row["tool_name"] == "remote_project_snapshot"
    assert row["rule"] and row["layer"]
    # Only denials are written, one per file, as the gate writes them.
    assert all(row["outcome"] == "denied" for row in audited)
    assert not any(row["item"].endswith("public.md") for row in audited)


def test_the_snapshot_honours_a_read_profile_bound_to_the_parents_agent(
    tmp_path, read_policy, monkeypatch
) -> None:
    """Opus: the parent's own tool gate resolves the profile WITH its agent, so a
    profile bound to that agent narrows the snapshot as it narrows the agent."""
    from kiro_crew import sel as sel_mod
    from kiro_crew.dashboard.remote_subagents import RemoteSubagentService
    from kiro_crew.platform import context as ctx_mod

    class _Quiet:
        def log_governance_decision(self, **_kwargs: object) -> None:
            pass

    monkeypatch.setattr(sel_mod, "sel", lambda: _Quiet())
    ctx_mod.reset_context()  # no ceiling: only the agent's profile governs
    project = tmp_path / "project"
    project.mkdir()
    _git_project(project)
    (project / "private.md").write_bytes(b"keep out\n")
    _commit_all(project, "private.md")
    (tmp_path / "profiles" / "researcher.json").write_text(
        json.dumps(
            {
                "name": "researcher",
                "bind": {"type": "task", "id": "researcher"},
                "filesystem": {"read": {"mode": "deny", "deny": [f"**{os.sep}private.md"]}},
            }
        ),
        encoding="utf-8",
    )

    def names_for(agent: str) -> set[str]:
        payload, _digest, _commit = RemoteSubagentService._build_project_archive(
            project, "dashboard:parent", "", agent
        )
        return _names(payload)

    assert "private.md" not in names_for("researcher")
    # The binding is what narrows it: another agent's snapshot keeps the file.
    assert "private.md" in names_for("writer")


def test_an_ungoverned_parent_has_no_read_check(tmp_path, read_policy) -> None:
    from kiro_crew.dashboard.remote_subagents import _project_read_check
    from kiro_crew.platform import context as ctx_mod

    ctx_mod.reset_context()
    assert _project_read_check("dashboard:parent") is None


def test_an_unevaluable_read_policy_refuses_the_snapshot(
    tmp_path, read_policy, monkeypatch
) -> None:
    """A wrong permit here uploads the file to another host, so an error refuses."""
    from kiro_crew.dashboard.remote_subagents import RemoteSubagentError, RemoteSubagentService
    from kiro_crew.platform import governance

    project = tmp_path / "project"
    project.mkdir()
    _git_project(project)
    read_policy(deny=("**/never.md",))

    def broken(*_args: object) -> object:
        raise RuntimeError("evaluator failed")

    monkeypatch.setattr(governance, "resolve", broken)
    with pytest.raises(RemoteSubagentError) as caught:
        RemoteSubagentService._build_project_archive(project, "dashboard:parent", "")
    assert caught.value.code == "remote_project_policy_unavailable"
    assert caught.value.status == 409


def test_a_planted_fsmonitor_never_runs_on_the_gateway(tmp_path) -> None:
    """An agent-written ``core.fsmonitor`` must not execute while packaging."""
    import subprocess

    from kiro_crew.dashboard.remote_subagents import RemoteSubagentService

    project = tmp_path / "project"
    project.mkdir()
    _git_project(project)
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor.sh"
    hook.write_bytes(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n".encode())
    hook.chmod(0o755)
    subprocess.run(["git", "-C", str(project), "config", "core.fsmonitor", str(hook)], check=True)
    # Dirty a tracked file too: a status refresh is where git would ask the monitor.
    # Bytes, not text: text mode writes CRLF on Windows.
    (project / "app.py").write_bytes(b"print('changed')\n")

    payload, _digest, _commit = RemoteSubagentService._build_project_archive(project)

    assert not marker.exists()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        member = archive.extractfile("app.py")
        assert member is not None and member.read() == b"print('changed')\n"


def test_a_symlinked_parent_cannot_pull_files_from_outside(tmp_path) -> None:
    """``cache/config.json`` is tracked, then ``cache`` becomes a link elsewhere."""
    from kiro_crew.dashboard.remote_subagents import RemoteSubagentService

    project = tmp_path / "project"
    project.mkdir()
    _git_project(project)
    (project / "cache").mkdir()
    (project / "cache" / "config.json").write_text("{}\n", encoding="utf-8")
    (project / "notes.txt").write_text("kept\n", encoding="utf-8")
    (project / "leaf-link").symlink_to(project / "notes.txt")
    _commit_all(project, "cache/config.json", "notes.txt", "leaf-link")

    outside = tmp_path / "credentials"
    outside.mkdir()
    (outside / "config.json").write_text("SECRET\n", encoding="utf-8")
    (project / "cache" / "config.json").unlink()
    (project / "cache").rmdir()
    (project / "cache").symlink_to(outside, target_is_directory=True)

    payload, _digest, _commit = RemoteSubagentService._build_project_archive(project)

    names = _names(payload)
    assert "cache/config.json" not in names
    assert "leaf-link" not in names  # a tracked symlink is never packaged
    assert {"app.py", "notes.txt"} <= names
    assert b"SECRET" not in gzip_decompress(payload)


def test_tracked_credentials_and_protected_paths_are_left_out(tmp_path, monkeypatch) -> None:
    from kiro_crew.dashboard.remote_subagents import RemoteSubagentService

    _git_project(tmp_path)
    (tmp_path / ".env").write_text("TOKEN=x\n", encoding="utf-8")
    (tmp_path / "fenced.txt").write_text("fenced\n", encoding="utf-8")
    _commit_all(tmp_path, ".env", "fenced.txt")
    fenced = str(tmp_path / "fenced.txt")

    import kiro_crew.security.paths as paths

    real = paths.is_sensitive_resolved_path
    monkeypatch.setattr(paths, "is_sensitive_resolved_path", lambda p: p == fenced or real(p))

    names = _names(RemoteSubagentService._build_project_archive(tmp_path)[0])
    assert ".env" not in names and "fenced.txt" not in names
    assert "app.py" in names

    monkeypatch.setattr(paths, "is_sensitive_resolved_path", lambda p: True)
    with pytest.raises(RemoteSubagentError) as raised:
        RemoteSubagentService._build_project_archive(tmp_path)
    assert raised.value.code == "remote_project_protected"
    assert remote_snapshot._GIT_NEUTRALISED_CONFIG[0] == "core.fsmonitor="


@pytest.mark.asyncio
async def test_registered_upload_route_reaches_the_real_handler(monkeypatch) -> None:
    """The route the gateway REGISTERS must import the module where it lives.

    The handler tests above import ``api_remote_workspace_upload`` directly, so
    they stayed green while the registered route resolved it under
    ``dashboard.handlers`` and raised ``ModuleNotFoundError`` -- a bare 500 on
    every project-synced remote spawn, caught only by an end-to-end canary.
    """
    from aiohttp import web

    from kiro_crew.dashboard import remote_workspaces, server

    sentinel = web.json_response({"reached": True})

    async def _stub(_request: Any) -> web.Response:
        return sentinel

    monkeypatch.setattr(remote_workspaces, "api_remote_workspace_upload", _stub)
    app = web.Application()
    server._register_mcp_routes(app)
    route = next(
        r
        for r in app.router.routes()
        if r.method == "POST"
        and r.resource is not None
        and r.resource.canonical == "/api/remote-workspaces"
    )

    assert await route.handler(cast(Any, object())) is sentinel


@pytest.mark.asyncio
async def test_a_project_that_cannot_be_packaged_is_a_typed_refusal(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.cloud.aws import AWSError

    state = SimpleNamespace(
        instances_manager=_Instances([], connected=("crew-a",)),
        get_slot=lambda _name: SimpleNamespace(project=str(tmp_path)),
    )
    service = RemoteSubagentService(state, _auto_manager())  # type: ignore[arg-type]

    def not_git(_path: Path, *_identity: str) -> tuple[bytes, str, str]:
        raise AWSError("not a git checkout")

    monkeypatch.setattr(service, "_build_project_archive", not_git)

    with pytest.raises(RemoteSubagentError) as caught:
        await _spawn_placed(service, **_spawn_kwargs(cwd=""))  # type: ignore[arg-type]

    assert caught.value.code == "remote_project_snapshot_failed"
    assert caught.value.status == 400
    await service.close()


def test_a_claimed_snapshot_survives_the_prune_an_unclaimed_one_does_not(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT: pruning read run ownership, then deleted, and a run admitted in between
    lost its tree. Admission claims the snapshot under the same lock, which renews
    its age, so the prune that follows keeps it; a pruned one is reported gone."""
    from kiro_crew.dashboard import remote_workspaces

    base = tmp_path / "ws"
    monkeypatch.setattr(remote_workspaces, "_workspace_root", lambda: base)
    claimed = _expired_snapshot(base, "00000001")
    unclaimed = _expired_snapshot(base, "00000002")
    assert remote_workspaces.claim_workspace(str(claimed / "src")) is True
    assert remote_workspaces._prune_workspaces(base.resolve(), keep="", in_use=()) == 1
    assert claimed.is_dir() and not unclaimed.exists()
    assert remote_workspaces.claim_workspace(str(unclaimed)) is False
    assert remote_workspaces.claim_workspace(str(tmp_path / "elsewhere")) is True


@pytest.mark.skipif(os.name == "nt", reason="hard links need a privilege on Windows")
def test_a_tracked_name_hardlinked_to_an_excluded_file_is_not_packaged(tmp_path) -> None:
    """GPT: exclusions judge names, so a tracked file hardlinked to the project's
    .env (one inode, two names) carried the credential into the snapshot."""
    _git_project(tmp_path)
    (tmp_path / "config.py").write_text("TOKEN = 'x'\n", encoding="utf-8")
    _commit_all(tmp_path, "config.py")
    os.link(tmp_path / "config.py", tmp_path / ".env")
    payload, _digest, _commit = RemoteSubagentService._build_project_archive(tmp_path)
    names = _names(payload)
    assert "app.py" in names
    assert "config.py" not in names and ".env" not in names
