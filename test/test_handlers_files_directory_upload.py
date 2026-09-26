"""Tests for ``api_directory_upload`` (POST /api/directory-upload).

Backs the Files panel's drag-and-drop and "Upload files..." row-menu actions:
unlike ``api_upload_file`` (which always writes into the fixed chat-attachment
scratch directory), this endpoint writes into a caller-supplied directory
anywhere the dashboard's own path gate permits.

Covers: the happy path, the name-collision prompt-not-overwrite contract (and
its ``overwrite=1`` escape hatch), the reused extension allowlist and size
cap, the magic-byte content-signature gate, invalid/sensitive/missing
directories, that a path-traversal attempt via the uploaded filename lands
inside the target directory rather than escaping it, that a sensitive
DESTINATION is refused even when the directory alone would pass (the crew-home
keystone leaves), and that a symlink/junction planted at the destination name
is refused rather than followed on every platform.
"""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.files as files_mod
from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.dashboard.handlers.files import api_directory_upload


def _make_app() -> web.Application:
    """The directory-upload route as it is wired live: behind the dashboard owner gate.

    ``as_owner`` installs the standalone-local owner identity plus a stand-in for
    the token middleware, so a request with no ``X-Test-User`` reads as the owner
    (the default these tests want) and one carrying ``X-Test-User`` reads as that
    non-owner subject -- the shape ``slack/allowlist.py::send_dashboard_link``
    mints for a channel-allowlisted user.
    """
    from dashboard_owner_helpers import as_owner

    app = web.Application()
    app.router.add_post("/api/directory-upload", api_directory_upload)
    return as_owner(app)


# Kept as a named alias for the owner-gate tests that read as the non-owner.
_make_owner_app = _make_app


@pytest.mark.asyncio
async def test_directory_upload_refuses_a_non_owner(tmp_path: Path, mock_sel) -> None:
    """A non-owner dashboard session cannot plant a file into the workspace.

    ``/api/directory-upload`` creates and overwrites files anywhere off the
    sensitive floor -- steering docs, ``SKILL.md`` and the MCP config included --
    so it holds the same owner boundary its sibling ``/api/file-write`` holds.
    The refusal must land before the path probe, so a denied caller cannot even
    learn whether the directory exists.
    """
    steering = tmp_path / ".kiro" / "steering"
    steering.mkdir(parents=True)

    with patch.object(files_mod, "_run_path_probe", wraps=files_mod._run_path_probe) as probe:
        async with TestClient(TestServer(_make_owner_app())) as client:
            form = aiohttp.FormData()
            form.add_field("file", b"planted", filename="rules.md", content_type="text/plain")
            resp = await client.post(
                f"/api/directory-upload?dir={steering}",
                data=form,
                headers={"X-Test-User": "U0NONOWNER"},
            )
            body = await resp.json()

    assert resp.status == 403
    assert body.get("code") == "owner_only"
    # Refused ahead of the path probe, so a non-owner learns nothing about the tree.
    probe.assert_not_called()
    assert not (steering / "rules.md").exists()


@pytest.mark.asyncio
async def test_directory_upload_still_writes_for_the_owner(tmp_path: Path, mock_sel) -> None:
    """Positive control: the owner passes the gate and the upload lands."""
    target = tmp_path / "docs"
    target.mkdir()

    async with TestClient(TestServer(_make_owner_app())) as client:
        resp = await _upload(
            client,
            directory=str(target),
            content=b"hello",
            filename="note.md",
        )

    assert resp.status == 200
    assert (target / "note.md").read_bytes() == b"hello"


@pytest.fixture()
def mock_sel():
    """Stub the SEL audit sink so upload audit calls don't blow up."""
    with patch("kiro_crew.dashboard.handlers.files._sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


async def _upload(
    client: TestClient,
    *,
    directory: str,
    content: bytes,
    filename: str,
    overwrite: bool = False,
) -> aiohttp.ClientResponse:
    form = aiohttp.FormData()
    form.add_field("file", content, filename=filename, content_type="text/plain")
    q = f"?dir={directory}" + ("&overwrite=1" if overwrite else "")
    return await client.post(f"/api/directory-upload{q}", data=form)


async def _upload_raw_filename(
    client: TestClient,
    *,
    directory: str,
    content: bytes,
    filename: str,
) -> aiohttp.ClientResponse:
    """Send the filename verbatim instead of applying FormData's quoting."""
    boundary = "kirocrew-directory-upload-test"
    body = (
        f"--{boundary}\r\n".encode()
        + (
            'Content-Disposition: form-data; name="file"; '
            f"filename*=UTF-8''{quote(filename, safe='')}\r\n"
        ).encode()
        + b"Content-Type: text/plain\r\n\r\n"
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return await client.post(
        f"/api/directory-upload?dir={directory}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    platform_compat.IS_WINDOWS,
    reason="Win32 strips trailing spaces from a path component, so the two "
    "directories this test needs to tell apart cannot both exist there",
)
async def test_trailing_space_in_dir_is_not_trimmed_to_a_sibling(tmp_path: Path, mock_sel) -> None:
    """``dir`` is used verbatim: a directory whose name ends in a space is a
    different directory from its space-less sibling, and trimming the query
    value would land the upload in the sibling while the client is told the
    name it asked for succeeded."""
    spaced = tmp_path / "reports "
    sibling = tmp_path / "reports"
    spaced.mkdir()
    sibling.mkdir()
    async with TestClient(TestServer(_make_app())) as client:
        form = aiohttp.FormData()
        form.add_field("file", b"payload", filename="a.txt", content_type="text/plain")
        resp = await client.post(f"/api/directory-upload?dir={quote(str(spaced))}", data=form)
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    assert body["path"] == str(spaced / "a.txt")
    assert (spaced / "a.txt").read_bytes() == b"payload"
    assert not (sibling / "a.txt").exists()


@pytest.mark.asyncio
async def test_uploads_a_file_into_the_target_directory(tmp_path: Path, mock_sel) -> None:
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client, directory=str(tmp_path), content=b"hello world", filename="notes.txt"
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    assert body["ok"] is True
    assert body["name"] == "notes.txt"
    written = tmp_path / "notes.txt"
    assert written.read_bytes() == b"hello world"
    assert body["path"] == str(written)
    assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "success"


@pytest.mark.asyncio
async def test_cancelled_committed_upload_is_audited_once(
    tmp_path: Path, mock_sel, monkeypatch
) -> None:
    """A committed write is audited before request cancellation propagates."""
    real_run_path_probe = files_mod._run_path_probe
    real_atomic_write = files_mod.atomic_write
    loop = asyncio.get_running_loop()
    handler_task: asyncio.Task | None = None
    committed = threading.Event()

    async def _capture_handler_task(fn, *args, transfer=False):
        nonlocal handler_task
        if handler_task is None:
            handler_task = asyncio.current_task()
        return await real_run_path_probe(fn, *args, transfer=transfer)

    def _commit_then_cancel(*args, **kwargs):
        result = real_atomic_write(*args, **kwargs)
        committed.set()
        assert handler_task is not None
        loop.call_soon_threadsafe(handler_task.cancel)
        return result

    monkeypatch.setattr(files_mod, "_run_path_probe", _capture_handler_task)
    monkeypatch.setattr(files_mod, "atomic_write", _commit_then_cancel)

    async with TestClient(TestServer(_make_app())) as client:
        with pytest.raises(aiohttp.ServerDisconnectedError):
            await _upload(
                client,
                directory=str(tmp_path),
                content=b"committed before cancellation",
                filename="cancelled.txt",
            )

    assert committed.is_set()
    destination = tmp_path / "cancelled.txt"
    assert destination.read_bytes() == b"committed before cancellation"
    mock_sel.log_api_access.assert_called_once_with(
        caller="local-app",
        operation="upload.directory",
        outcome="success",
        source="dashboard",
        resources=str(destination),
    )


@pytest.mark.asyncio
async def test_cancelled_refused_upload_is_audited_once(
    tmp_path: Path, mock_sel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal is audited before request cancellation propagates."""
    real_run_path_probe = files_mod._run_path_probe
    loop = asyncio.get_running_loop()
    handler_task: asyncio.Task | None = None
    cancellation_requested = threading.Event()

    async def _capture_handler_task(fn, *args, transfer=False):
        nonlocal handler_task
        if handler_task is None:
            handler_task = asyncio.current_task()
        return await real_run_path_probe(fn, *args, transfer=transfer)

    def _cancel_then_refuse(*args, **kwargs):
        assert handler_task is not None

        def _request_cancellation() -> None:
            handler_task.cancel()
            cancellation_requested.set()

        loop.call_soon_threadsafe(_request_cancellation)
        assert cancellation_requested.wait(timeout=5)
        raise FileExistsError

    monkeypatch.setattr(files_mod, "_run_path_probe", _capture_handler_task)
    monkeypatch.setattr(files_mod, "atomic_write", _cancel_then_refuse)

    async with TestClient(TestServer(_make_app())) as client:
        with pytest.raises(aiohttp.ServerDisconnectedError):
            await _upload(
                client,
                directory=str(tmp_path),
                content=b"must not be published",
                filename="cancelled.txt",
            )

    assert not (tmp_path / "cancelled.txt").exists()
    mock_sel.log_api_access.assert_called_once_with(
        caller="local-app",
        operation="upload.directory",
        outcome="rejected",
        source="dashboard",
        resources="reason:name_collision",
    )


@pytest.mark.asyncio
async def test_windows_pin_final_path_must_match_validated_directory(
    tmp_path: Path, mock_sel, monkeypatch
) -> None:
    """A Windows pin that resolves elsewhere is refused before publication."""
    monkeypatch.setattr(files_mod.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(files_mod.platform_compat, "IS_WINDOWS", True)
    opened: list[int] = []
    closed: list[int] = []
    guard_fd = 1_000_000_000

    def _pin_redirected_directory(path):
        opened.append(guard_fd)
        return guard_fd

    real_close = os.close

    def _track_guard_close(fd, *args, **kwargs):
        if fd in opened:
            closed.append(fd)
            return None
        return real_close(fd, *args, **kwargs)

    monkeypatch.setattr(files_mod.platform_compat, "pin_directory", _pin_redirected_directory)
    monkeypatch.setattr(
        files_mod.pinned_fs,
        "fd_real_path",
        lambda fd: str(tmp_path / "redirected"),
    )
    monkeypatch.setattr(os, "close", _track_guard_close)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(tmp_path),
            content=b"must not be published",
            filename="redirected.txt",
        )
        assert resp.status == 403, await resp.text()
        body = await resp.json()

    assert body["code"] == "symlink_refused"
    assert opened and opened == closed
    assert not (tmp_path / "redirected.txt").exists()


@pytest.mark.asyncio
async def test_directory_validation_runs_off_the_event_loop_thread(
    tmp_path: Path, mock_sel, monkeypatch
) -> None:
    """``_validate_dashboard_path`` (``realpath`` plus ``is_sensitive_path``'s
    candidate-form walk) and the directory-existence check are blocking
    filesystem calls. AUTOSDE's ``no-blocking-call-on-event-loop`` rule
    (``blocking: true`` -- see ``AUTOSDE.yaml``) is authoritative here: a
    stalled network-mounted ``dir`` resolving or stat-ing directly on the
    gateway's event loop would freeze every other request and the heartbeat
    for as long as that mount takes to time out. Both validation calls
    (``target_dir``'s own, and the destination re-check) must run on a
    worker thread, never on the thread driving this test's own event loop.
    """
    main_thread_ident = threading.get_ident()
    seen_idents: list[int] = []

    real_validate = files_mod._validate_dashboard_path

    def _recording_validate(raw: str) -> str | None:
        seen_idents.append(threading.get_ident())
        return real_validate(raw)

    monkeypatch.setattr(files_mod, "_validate_dashboard_path", _recording_validate)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(client, directory=str(tmp_path), content=b"hello", filename="c.txt")
        assert resp.status == 200, await resp.text()

    # target_dir's own validation, plus the destination re-check -- both
    # calls this handler makes to _validate_dashboard_path.
    assert len(seen_idents) == 2
    assert all(ident != main_thread_ident for ident in seen_idents)


@pytest.mark.asyncio
async def test_name_collision_is_refused_without_overwrite(tmp_path: Path, mock_sel) -> None:
    existing = tmp_path / "report.txt"
    existing.write_bytes(b"original")
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client, directory=str(tmp_path), content=b"new content", filename="report.txt"
        )
        assert resp.status == 409, await resp.text()
        body = await resp.json()
    assert body["code"] == "name_collision"
    # The original file must survive a declined overwrite untouched.
    assert existing.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_overwrite_flag_replaces_the_existing_file(tmp_path: Path, mock_sel) -> None:
    existing = tmp_path / "report.txt"
    existing.write_bytes(b"original")
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(tmp_path),
            content=b"replacement",
            filename="report.txt",
            overwrite=True,
        )
        assert resp.status == 200, await resp.text()
    assert existing.read_bytes() == b"replacement"


@pytest.mark.asyncio
@pytest.mark.skipif(
    platform_compat.IS_WINDOWS,
    reason="Windows has no POSIX permission bits -- os.chmod there only "
    "toggles the read-only attribute, so a file's mode always reports as "
    "0o666 (or 0o444 read-only) regardless of what was requested, and this "
    "assertion could never hold there",
)
async def test_overwrite_preserves_the_existing_files_mode(tmp_path: Path, mock_sel) -> None:
    """A replace must carry the ORIGINAL file's permission bits onto the
    replacement, mirroring ``_file_write_blocking``'s own carry for
    ``api_file_write`` -- without it, ``atomic_write``'s fresh temp file lands
    at the umask default, silently loosening (or tightening) whatever mode a
    restricted file actually had."""
    existing = tmp_path / "secret.txt"
    existing.write_bytes(b"original")
    os.chmod(existing, 0o640)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(tmp_path),
            content=b"replacement",
            filename="secret.txt",
            overwrite=True,
        )
        assert resp.status == 200, await resp.text()
    assert existing.read_bytes() == b"replacement"
    assert oct(existing.stat().st_mode & 0o777) == oct(0o640)


@pytest.mark.asyncio
async def test_overwrite_carries_the_existing_files_acl(
    tmp_path: Path, mock_sel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same carry, for a named POSIX ACL: ``mode=`` alone only reproduces the
    permission BITS, so a ``system.posix_acl_access`` entry the owner set
    would otherwise be dropped the moment the replacement's fresh inode lands,
    same as the missing carry ``_file_write_blocking``'s own test pins."""
    if not all(hasattr(os, a) for a in ("listxattr", "getxattr", "setxattr")):
        pytest.skip("platform without xattr syscalls")
    existing = tmp_path / "secret.txt"
    existing.write_bytes(b"original")

    monkeypatch.setattr(os, "listxattr", lambda *a, **k: ["system.posix_acl_access"], raising=False)
    monkeypatch.setattr(os, "getxattr", lambda *a, **k: b"acl", raising=False)
    recorded: list[tuple[str, bytes]] = []
    monkeypatch.setattr(
        os,
        "setxattr",
        lambda fd, attr, value, *a, **k: recorded.append((attr, value)),
        raising=False,
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(tmp_path),
            content=b"replacement",
            filename="secret.txt",
            overwrite=True,
        )
        assert resp.status == 200, await resp.text()
    assert ("system.posix_acl_access", b"acl") in recorded


@pytest.mark.asyncio
async def test_overwrite_carries_mode_when_access_control_xattrs_are_unsupported(
    tmp_path: Path, mock_sel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without xattr carry, overwrite degrades to the preserved mode."""
    existing = tmp_path / "secret.txt"
    existing.write_bytes(b"original")
    if not platform_compat.IS_WINDOWS:
        os.chmod(existing, 0o600)
    mode = existing.stat().st_mode & 0o777
    observed_modes: list[int | None] = []
    real_atomic_write = files_mod.atomic_write

    def _record_mode(*args, **kwargs):
        observed_modes.append(kwargs.get("mode"))
        return real_atomic_write(*args, **kwargs)

    # Pin the platform to the non-macOS POSIX branch, mirroring the sibling
    # tests above. On a real macOS host IS_MACOS is true, so the handler's
    # macOS ACL-carry branch runs; once open_access_control_source is stubbed
    # to yield no descriptor that branch refuses the overwrite with
    # access_control_preservation_failed instead of exercising the intended
    # xattrs-unsupported degrade-to-mode path this test asserts. IS_WINDOWS is
    # left at the host value so a Windows runner keeps its own DACL branch.
    monkeypatch.setattr(files_mod.platform_compat, "IS_MACOS", False)
    monkeypatch.setattr(files_mod, "ACCESS_CONTROL_XATTRS_SUPPORTED", False, raising=False)
    monkeypatch.setattr(files_mod, "open_access_control_source", lambda *args, **kwargs: None)
    monkeypatch.setattr(files_mod, "atomic_write", _record_mode)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(tmp_path),
            content=b"replacement",
            filename="secret.txt",
            overwrite=True,
        )
        assert resp.status == 200, await resp.text()

    assert existing.read_bytes() == b"replacement"
    assert observed_modes == [mode]


@pytest.mark.asyncio
async def test_windows_overwrite_preserves_dacl_without_holding_source_open(
    tmp_path: Path, mock_sel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Windows overwrite carries the old DACL without blocking ``os.replace``.

    Windows has no POSIX ACL xattrs, and an open source handle prevents replacement.
    The handler must therefore snapshot the DACL from the validated source descriptor,
    close that descriptor, and pass the snapshot to ``atomic_write`` for the staged
    inode before publication.
    """
    monkeypatch.setattr(files_mod.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(files_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        files_mod.pinned_fs,
        "fd_real_path",
        lambda _fd: str(tmp_path),
    )

    existing = tmp_path / "secret.txt"
    existing.write_bytes(b"original")
    source_fd = os.open(existing, os.O_RDONLY)
    captured_dacl = object()
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        files_mod.platform_compat,
        "open_file_no_reparse",
        lambda _path: source_fd,
    )

    def _snapshot_dacl(fd: int) -> object:
        assert fd == source_fd
        return captured_dacl

    monkeypatch.setattr(
        files_mod.platform_compat,
        "snapshot_windows_dacl",
        _snapshot_dacl,
        raising=False,
    )

    def _publish(path, content, **kwargs) -> None:
        observed["dacl"] = kwargs.get("preserve_windows_dacl")
        try:
            os.fstat(source_fd)
        except OSError as exc:
            observed["source_closed"] = exc.errno == errno.EBADF
        else:
            observed["source_closed"] = False
        Path(path).write_bytes(content)

    monkeypatch.setattr(files_mod, "atomic_write", _publish)

    try:
        async with TestClient(TestServer(_make_app())) as client:
            resp = await _upload(
                client,
                directory=str(tmp_path),
                content=b"replacement",
                filename="secret.txt",
                overwrite=True,
            )
            assert resp.status == 200, await resp.text()
    finally:
        try:
            os.close(source_fd)
        except OSError:
            pass

    assert existing.read_bytes() == b"replacement"
    assert observed == {"dacl": captured_dacl, "source_closed": True}


@pytest.mark.asyncio
async def test_windows_overwrite_refuses_when_dacl_cannot_be_snapshotted(
    tmp_path: Path, mock_sel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable destination DACL leaves the original file untouched."""
    monkeypatch.setattr(files_mod.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(files_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(files_mod.pinned_fs, "fd_real_path", lambda _fd: str(tmp_path))

    existing = tmp_path / "secret.txt"
    existing.write_bytes(b"original")

    def _refuse(_fd: int) -> platform_compat.WindowsDaclSnapshot:
        raise platform_compat.WindowsDaclError(errno.EACCES, "DACL read refused")

    monkeypatch.setattr(files_mod.platform_compat, "snapshot_windows_dacl", _refuse)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(tmp_path),
            content=b"replacement",
            filename="secret.txt",
            overwrite=True,
        )
        assert resp.status == 409, await resp.text()
        body = await resp.json()

    assert body["code"] == "access_control_preservation_failed"
    assert existing.read_bytes() == b"original"


@pytest.mark.asyncio
@pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="requires native Windows DACLs")
async def test_windows_overwrite_preserves_the_real_restrictive_dacl(
    tmp_path: Path, mock_sel
) -> None:
    """A real Windows overwrite retains the destination's restrictive DACL."""
    existing = tmp_path / "secret.txt"
    existing.write_bytes(b"original")
    platform_compat.restrict_to_owner(existing)

    def _snapshot(path: Path) -> platform_compat.WindowsDaclSnapshot:
        fd = platform_compat.open_file_no_reparse(path)
        try:
            return platform_compat.snapshot_windows_dacl(fd)
        finally:
            os.close(fd)

    before = _snapshot(existing)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(tmp_path),
            content=b"replacement",
            filename="secret.txt",
            overwrite=True,
        )
        assert resp.status == 200, await resp.text()

    assert existing.read_bytes() == b"replacement"
    assert _snapshot(existing) == before


@pytest.mark.asyncio
async def test_concurrent_create_lands_only_the_winner(
    tmp_path: Path, mock_sel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-name race between two concurrent CREATE uploads: both classify
    the destination as missing, but a rival claims the name before THIS
    request's publish runs. ``create_only=True`` makes the publish itself
    (``os.link``, not a rename) the only place the race is decided -- the
    loser's ``os.link`` fails with ``FileExistsError`` (reported as
    ``name_collision``) instead of silently replacing the rival's bytes with
    its own, which is what an unconditional rename after a stale classification
    would do.

    The rival's write is injected right before the real ``atomic_write`` call
    (mirroring the pinned-parent tests' own inject-after-the-real-call
    technique in test_dashboard_pinned_write_migration.py), so this exercises
    the REAL create_only/os.link mechanics against a genuinely present rival
    file, not a mocked outcome.
    """
    real_atomic_write = files_mod.atomic_write

    def _rival_wins_then_publish(path, content, **kwargs):
        Path(path).write_bytes(b"rival's bytes")
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(files_mod, "atomic_write", _rival_wins_then_publish)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(client, directory=str(tmp_path), content=b"mine", filename="race.txt")
        assert resp.status == 409, await resp.text()
        body = await resp.json()
    assert body["code"] == "name_collision"
    assert (tmp_path / "race.txt").read_bytes() == b"rival's bytes"


@pytest.mark.asyncio
async def test_temp_cleanup_failure_after_publish_is_not_an_error(
    tmp_path: Path, mock_sel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleanup failure AFTER the create-only publish must not report failure.

    ``create_only``'s publish is ``os.link``; the moment the link succeeds the
    destination file exists. Removing the staging temp afterwards is pure
    cleanup -- on Windows a scanner or indexer briefly holding the temp open
    makes that ``os.unlink`` raise ``EACCES``. Routing that error into the
    link-unsupported fallback (or out of the handler as a 5xx) would tell the
    client the upload failed although the file is already there, inviting a
    retry that then collides with the file the "failed" request created.

    ``os.unlink`` is patched to raise only for atomic_write's staging temps
    (both temp shapes end in ``.tmp``; the destination name here does not), so
    the REAL link/publish mechanics run untouched.
    """
    real_unlink = os.unlink

    def _unlink_denied_for_temps(name, *args, **kwargs):
        if str(name).endswith(".tmp"):
            raise PermissionError(errno.EACCES, "scanner holds the temp open", str(name))
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", _unlink_denied_for_temps)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client, directory=str(tmp_path), content=b"published bytes", filename="kept.txt"
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    assert body["ok"] is True
    assert (tmp_path / "kept.txt").read_bytes() == b"published bytes"
    # The publish committed; the only residue is the orphaned staging temp.
    orphans = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert len(orphans) == 1


def test_atomic_write_create_only_commits_when_temp_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit-level twin of the handler test above, pinned directly on
    ``atomic_write(create_only=True)``: a post-link ``os.unlink`` failure is
    swallowed (logged), never raised, and never re-routed into the
    link-unsupported fallback."""
    real_unlink = os.unlink

    def _unlink_denied_for_temps(name, *args, **kwargs):
        if str(name).endswith(".tmp"):
            raise PermissionError(errno.EACCES, "scanner holds the temp open", str(name))
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", _unlink_denied_for_temps)

    dest = tmp_path / "fresh.txt"
    atomic_write(dest, "committed", create_only=True)

    assert dest.read_text() == "committed"
    orphans = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert len(orphans) == 1


@pytest.mark.asyncio
async def test_overwrite_refuses_to_replace_a_directory(tmp_path: Path, mock_sel) -> None:
    (tmp_path / "report.txt").mkdir()
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(tmp_path),
            content=b"x",
            filename="report.txt",
            overwrite=True,
        )
        assert resp.status == 409, await resp.text()
        body = await resp.json()
    assert body["code"] == "is_a_directory"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename",
    [
        "report?.txt",
        "a<b.txt",
        "trailing.",
        "trailing ",
        "CON",
        "nul.txt",
        "x\x01y.txt",
    ],
    ids=[
        "question-mark",
        "less-than",
        "trailing-dot",
        "trailing-space",
        "reserved-name",
        "reserved-name-with-extension",
        "control-character",
    ],
)
async def test_windows_invalid_filename_is_rejected_before_any_write(
    tmp_path: Path, mock_sel, filename: str
) -> None:
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload_raw_filename(
            client,
            directory=str(tmp_path),
            content=b"must not be published",
            filename=filename,
        )
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "invalid_filename"
    assert not any(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_unsupported_extension_is_rejected_before_any_write(tmp_path: Path, mock_sel) -> None:
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client, directory=str(tmp_path), content=b"MZ\x90\x00", filename="payload.exe"
        )
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "unsupported_file_type"
    assert not any(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_size_limit_is_enforced(tmp_path: Path, mock_sel, monkeypatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.files._MAX_UPLOAD_BYTES",
        8,
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client, directory=str(tmp_path), content=b"way too many bytes", filename="big.txt"
        )
        assert resp.status == 413, await resp.text()
        body = await resp.json()
    assert body["code"] == "file_too_large"
    assert not any(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_content_signature_mismatch_is_rejected(tmp_path: Path, mock_sel) -> None:
    """A ``.png`` extension whose bytes are not really PNG is refused
    (CWE-434) — the same magic-byte gate ``api_upload_file`` applies."""
    async with TestClient(TestServer(_make_app())) as client:
        form = aiohttp.FormData()
        form.add_field(
            "file",
            b"not actually a png",
            filename="fake.png",
            content_type="image/png",
        )
        resp = await client.post(f"/api/directory-upload?dir={tmp_path}", data=form)
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "content_signature_mismatch"
    assert not any(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_non_multipart_request_is_rejected_with_coded_400(tmp_path: Path, mock_sel) -> None:
    """Malformed upload requests are rejected before aiohttp parses multipart."""
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(
            f"/api/directory-upload?dir={tmp_path}",
            json={"file": "not multipart"},
        )
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body == {"error": "expected multipart/form-data", "code": "not_multipart"}


@pytest.mark.asyncio
async def test_missing_directory_is_rejected(tmp_path: Path, mock_sel) -> None:
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(tmp_path / "does-not-exist"),
            content=b"x",
            filename="a.txt",
        )
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "invalid_directory"


@pytest.mark.asyncio
async def test_directory_that_is_actually_a_file_is_rejected(tmp_path: Path, mock_sel) -> None:
    a_file = tmp_path / "not-a-dir.txt"
    a_file.write_text("x")
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(client, directory=str(a_file), content=b"x", filename="a.txt")
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "invalid_directory"


@pytest.mark.asyncio
async def test_sensitive_directory_is_rejected(tmp_path: Path, mock_sel, monkeypatch) -> None:
    """The same ``is_sensitive_path`` gate ``/api/file-write`` trusts must
    also cover the new write direction: a directory ``hooks.validate_file_path``
    flags as sensitive is refused before any multipart body is even read.
    Faked via the shared gate (rather than a real ``~/.ssh``) so the test does
    not depend on this host's actual home-directory layout."""
    sensitive_dir = tmp_path / "sensitive"
    sensitive_dir.mkdir()
    monkeypatch.setattr(
        "kiro_crew.hooks.is_sensitive_path",
        lambda p: os.path.realpath(p) == os.path.realpath(str(sensitive_dir)),
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client, directory=str(sensitive_dir), content=b"x", filename="id_rsa.pub"
        )
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "invalid_directory"


@pytest.mark.asyncio
async def test_missing_dir_query_param_is_rejected(tmp_path: Path, mock_sel) -> None:
    async with TestClient(TestServer(_make_app())) as client:
        form = aiohttp.FormData()
        form.add_field("file", b"x", filename="a.txt", content_type="text/plain")
        resp = await client.post("/api/directory-upload", data=form)
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "missing_required_fields"


@pytest.mark.asyncio
async def test_path_traversal_in_filename_lands_inside_target_dir(tmp_path: Path, mock_sel) -> None:
    """A filename carrying ``../`` components must never escape the
    validated target directory -- the upload lands, but as a plain file
    named after its OWN basename, inside ``tmp_path`` only.

    Built as a raw multipart body (rather than ``aiohttp.FormData``) because
    the high-level FormData writer percent-encodes a ``/`` inside a
    ``filename=`` parameter before it ever reaches the wire, which would
    neutralize the traversal attempt in the TEST CLIENT rather than in the
    handler under test — this sends the literal, unencoded ``../`` an
    attacker-controlled client can send.

    The upload target is a CHILD of ``tmp_path`` (not ``tmp_path`` itself), so
    a single ``../`` resolves to ``tmp_path`` -- still inside pytest's
    auto-pruned basetemp -- rather than to ``tmp_path.parent``, which is
    outside this test's own temp tree and not pytest's to clean up. The
    assertion stays exactly as meaningful: it still fails loudly if the
    traversal guards regress, since a real escape would land the file at
    ``target_dir``'s PARENT (``tmp_path``), which is exactly what is checked
    below.
    """
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    outside_marker = tmp_path / "escaped.txt"
    outside_marker_existed = outside_marker.exists()
    boundary = "kctestboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="../escaped.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "pwned?\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(
            f"/api/directory-upload?dir={target_dir}",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        assert resp.status == 200, await resp.text()
        response_body = await resp.json()
    assert response_body["name"] == "escaped.txt"
    assert (target_dir / "escaped.txt").read_bytes() == b"pwned?"
    # Nothing was ever written outside target_dir (i.e. one level up, at
    # tmp_path.parent, which this test never touches).
    assert outside_marker.exists() == outside_marker_existed


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``KIROCREW_HOME`` override, exercising the REAL ``is_sensitive_path``
    gate rather than a monkeypatched stand-in: the keystone leaves
    (``security_policy.json``, ``profiles``, ``admission_policy.json``,
    ``computer_use.json``) live directly under this directory once the
    env var is set, the same way they would under a real crew data home."""
    home = tmp_path / "crew_home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leaf_name",
    ["security_policy.json", "computer_use.json", "admission_policy.json", "profiles"],
)
async def test_keystone_destination_is_refused_even_with_overwrite(
    crew_home: Path, mock_sel, leaf_name: str
) -> None:
    """The crew-home ROOT passes ``_validate_dashboard_path`` on its own (it
    is deliberately not itself sensitive -- only its leaves are), so naming a
    keystone leaf as the UPLOADED FILENAME while ``dir`` is the crew-home root
    must still be refused by the re-gated destination check, whether or not
    the leaf already exists and whether or not ``overwrite=1`` is set. This is
    the exact escape the destination re-validation in ``api_directory_upload``
    closes: ``dir=<crew-home>&overwrite=1`` with a ``file`` part named after a
    keystone leaf must never reach it."""
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(crew_home),
            content=b"forged content",
            filename=leaf_name,
            overwrite=True,
        )
        assert resp.status == 403, await resp.text()
        body = await resp.json()
    assert body["code"] == "sensitive_destination"
    # Nothing was written: the keystone leaf must not even come to exist.
    assert not (crew_home / leaf_name).exists()


@pytest.mark.asyncio
async def test_keystone_destination_is_refused_when_leaf_already_exists(
    crew_home: Path, mock_sel
) -> None:
    """Same refusal, but against an EXISTING keystone file with real content --
    the destination re-check runs before any collision/overwrite logic, so the
    original ceiling is provably untouched, not merely "not yet created"."""
    policy = crew_home / "security_policy.json"
    policy.write_text('{"real": "ceiling"}')
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(crew_home),
            content=b'{"forged": "ceiling"}',
            filename="security_policy.json",
            overwrite=True,
        )
        assert resp.status == 403, await resp.text()
        body = await resp.json()
    assert body["code"] == "sensitive_destination"
    assert policy.read_text() == '{"real": "ceiling"}'


@pytest.mark.asyncio
async def test_crew_home_root_as_dir_cannot_reach_a_nested_keystone_leaf(
    crew_home: Path, mock_sel
) -> None:
    """``profiles`` is gated as a whole DIRECTORY, so a file inside it is just
    as much a keystone write as the bare leaf name above -- prove the same
    re-gate closes that path too, by asking for the upload directly on the
    (pre-existing) ``profiles`` directory rather than the crew-home root."""
    profiles_dir = crew_home / "profiles"
    profiles_dir.mkdir()
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(profiles_dir),
            content=b"rogue profile",
            filename="rogue.json",
        )
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    # Caught by the DIRECTORY gate this time (dir itself is sensitive), not the
    # destination re-check -- either gate refusing is correct; this pins that
    # at least one of them does, so nothing can reach a leaf through the
    # crew-home root, whether by naming the leaf directly or by walking into
    # its directory.
    assert body["code"] == "invalid_directory"
    assert not any(profiles_dir.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing_kind", "overwrite", "expected_status", "expected_code"),
    [
        ("dir", True, 409, "is_a_directory"),
        ("dir", False, 409, "is_a_directory"),
        ("file", False, 409, "name_collision"),
        ("file", True, 200, None),
        ("missing", False, 200, None),
    ],
)
async def test_by_name_floor_still_classifies_the_destination_correctly(
    tmp_path: Path,
    mock_sel,
    monkeypatch,
    existing_kind: str,
    overwrite: bool,
    expected_status: int,
    expected_code: str | None,
) -> None:
    """Forces the platform to the "cannot pin a directory descriptor" floor
    (``pinned_fs.supports_pinned_walk`` returning False) -- the branch Windows
    takes for real, since it has no ``O_NOFOLLOW`` + ``dir_fd`` support to pin
    with -- and pins that the by-name existing-target classification
    (``platform_compat.is_link_or_junction`` / ``Path.is_dir()`` /
    ``Path.is_file()``) still gets missing/dir/file right, matching the
    dir-fd-pinned classification's outcomes exactly.

    This is a real regression: the PRE-REWRITE ``_write()`` used a single
    ``os.open(dest, O_EXCL/O_TRUNC | O_NOFOLLOW, ...)`` and branched on the
    POSIX-specific exceptions it raised (``IsADirectoryError`` etc.) -- exactly
    the design CI's Windows shard caught opening a directory with write flags,
    where the OS does not raise that same exception type, so the branch never
    matched and the exception surfaced as an unhandled 500 instead of the
    intended 409. Detecting the existing target's kind BEFORE ever attempting
    to open it (as this rewrite does) does not depend on which exception type
    a given OS raises for which failure, so it does not reproduce that gap --
    which is what this test, run for real on every platform CI exercises,
    pins.
    """
    monkeypatch.setattr(files_mod.pinned_fs, "supports_pinned_walk", lambda: False)
    # Also forces IS_WINDOWS so the handler takes its Windows ancestor-guard
    # branch (platform_compat.pin_directory) rather than failing closed with
    # ancestor_pin_unsupported -- that branch's own real POSIX pin_directory
    # implementation still works fine here (IS_POSIX is untouched), so this
    # reaches the same by-name classify/publish floor the test's docstring
    # describes, guarded exactly as Windows would guard it for real.
    monkeypatch.setattr(files_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        files_mod.pinned_fs,
        "fd_real_path",
        lambda fd: str(tmp_path),
    )
    copied_dacl = platform_compat.WindowsDaclSnapshot(acl=None, protected=False)
    monkeypatch.setattr(
        files_mod.platform_compat,
        "snapshot_windows_dacl",
        lambda _fd: copied_dacl,
    )
    monkeypatch.setattr(
        files_mod.platform_compat,
        "apply_windows_dacl",
        lambda _fd, _snapshot: None,
    )
    target = tmp_path / "report.txt"
    if existing_kind == "dir":
        target.mkdir()
    elif existing_kind == "file":
        target.write_bytes(b"original")

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(tmp_path),
            content=b"new content",
            filename="report.txt",
            overwrite=overwrite,
        )
        assert resp.status == expected_status, await resp.text()
        if expected_code is not None:
            body = await resp.json()
            assert body["code"] == expected_code

    if expected_status == 200:
        assert target.read_bytes() == b"new content"
    elif existing_kind == "file":
        assert target.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_ancestor_swapped_after_directory_validation_is_refused(
    tmp_path: Path, mock_sel, monkeypatch
) -> None:
    """Mirrors ``test_a_grandparent_swapped_after_canonicalization_is_refused``
    in ``test_dashboard_pinned_write_migration.py`` (the ``api_file_write``
    sibling's own coverage of the identical race): ``pinned_fs.pin_parent``
    walks ``target_dir``'s OWN already-resolved component chain with
    ``O_NOFOLLOW`` rather than re-resolving it, so an ancestor swapped for a
    symlink AFTER validation is refused rather than followed.

    ``_validate_dashboard_path`` is frozen to the pre-swap canonical directory
    here (bypassing a fresh, live re-resolution) to model a request that is
    already past validation at the moment the swap lands -- a live
    re-resolution at request time would simply resolve through the new
    symlink and validate a different, equally legitimate directory, which is
    a different scenario than the one this protects against.
    """
    if not files_mod.pinned_fs.supports_pinned_walk():
        pytest.skip("platform without pinned directory descriptors")

    named = tmp_path / "named"
    (named / "mid" / "leaf").mkdir(parents=True)
    canonical_target = str(Path(os.path.realpath(named / "mid" / "leaf")))

    # The tree an attacker wants the write redirected into, holding a file
    # that must survive with its own bytes.
    victim = tmp_path / "victim"
    (victim / "leaf").mkdir(parents=True)
    (victim / "leaf" / "sentinel.txt").write_text("PROTECTED")

    shutil.rmtree(named / "mid")
    (named / "mid").symlink_to(victim, target_is_directory=True)

    monkeypatch.setattr(files_mod, "_validate_dashboard_path", lambda raw: canonical_target)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client, directory=canonical_target, content=b"attacker body", filename="a.txt"
        )
        assert resp.status == 403, await resp.text()
        body = await resp.json()
    assert body["code"] == "symlink_refused"
    assert not (victim / "leaf" / "a.txt").exists()
    assert (victim / "leaf" / "sentinel.txt").read_text() == "PROTECTED"


@pytest.mark.asyncio
async def test_a_failed_publish_leaves_the_original_untouched(
    tmp_path: Path, mock_sel, monkeypatch
) -> None:
    """``atomic_write`` stages the full payload in a same-directory temp file
    and publishes it with ONE rename; a failure THERE (standing in for a short
    write or ``ENOSPC``) must never have already truncated the real
    destination -- the original survives byte-for-byte. This is exactly the
    hazard the single ``os.open(..., O_TRUNC | O_NOFOLLOW)`` design this
    replaced did not guard against."""
    existing = tmp_path / "report.txt"
    existing.write_bytes(b"original content, byte for byte")

    def _boom(*args, **kwargs):
        raise OSError("simulated ENOSPC")

    monkeypatch.setattr(files_mod, "atomic_write", _boom)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(tmp_path),
            content=b"replacement",
            filename="report.txt",
            overwrite=True,
        )
        assert resp.status >= 400, await resp.text()
    assert existing.read_bytes() == b"original content, byte for byte"


@pytest.mark.asyncio
async def test_windows_without_pinned_walk_guards_ancestors_via_pin_directory(
    tmp_path: Path, mock_sel, monkeypatch
) -> None:
    """Windows has no O_NOFOLLOW + dir_fd combination to pin a parent CHAIN
    with (``pinned_fs.supports_pinned_walk()`` is False there), so a bare
    by-name write there would leave open the exact ancestor-swap window this
    fix closes: ``platform_compat.pin_directory(target_dir)`` is called and
    its handle held across the whole classify-then-publish -- which, for
    real on Windows, blocks renaming ANY ancestor while the handle lives
    (``TestPinDirectory::test_a_pinned_directory_cannot_be_renamed_or_removed``
    in ``test_platform_compat.py``).

    Simulated here (this test runs on POSIX dev machines) by forcing the
    "not pinned" branch plus ``IS_WINDOWS``, and replacing ``pin_directory``
    with a fake that raises exactly as the real one does when a reparse
    point already sits at the name. Before this fix there was no such call
    and no such branch at all: the handler fell straight through to an
    unguarded by-name write, so this fake would never even be reached and
    the upload would have SUCCEEDED instead of being refused.
    """
    monkeypatch.setattr(files_mod.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(files_mod.platform_compat, "IS_WINDOWS", True)

    calls: list[str] = []

    def _fake_pin_directory(path):
        calls.append(str(path))
        raise OSError("simulated: a reparse point already sits at this name")

    monkeypatch.setattr(files_mod.platform_compat, "pin_directory", _fake_pin_directory)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(client, directory=str(tmp_path), content=b"attacker", filename="a.txt")
        assert resp.status == 403, await resp.text()
        body = await resp.json()
    assert body["code"] == "symlink_refused"
    assert calls == [str(tmp_path)]
    assert not (tmp_path / "a.txt").exists()


@pytest.mark.asyncio
async def test_windows_without_pinned_walk_still_uploads_when_pin_succeeds(
    tmp_path: Path, mock_sel, monkeypatch
) -> None:
    """Companion to the refusal test above: when ``pin_directory`` succeeds,
    the upload still proceeds normally (by name, exactly as the by-name
    floor already did) with the guard held for the duration and released
    afterward -- the fix adds a held guard, not a new failure mode for the
    ordinary case."""
    monkeypatch.setattr(files_mod.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(files_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        files_mod.pinned_fs,
        "fd_real_path",
        lambda fd: str(tmp_path),
    )

    real_pin_directory = files_mod.platform_compat.pin_directory
    opened: list[int] = []
    closed: list[int] = []

    def _tracking_pin_directory(path):
        fd = real_pin_directory(path)
        opened.append(fd)
        return fd

    real_close = os.close

    def _tracking_close(fd, *a, **k):
        if fd in opened:
            closed.append(fd)
        return real_close(fd, *a, **k)

    monkeypatch.setattr(files_mod.platform_compat, "pin_directory", _tracking_pin_directory)
    monkeypatch.setattr(os, "close", _tracking_close)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(client, directory=str(tmp_path), content=b"hello", filename="b.txt")
        assert resp.status == 200, await resp.text()
    assert (tmp_path / "b.txt").read_bytes() == b"hello"
    assert opened and opened == closed


@pytest.mark.asyncio
async def test_no_file_part_is_rejected(tmp_path: Path, mock_sel) -> None:
    async with TestClient(TestServer(_make_app())) as client:
        form = aiohttp.FormData()
        form.add_field("not_file", b"x", filename="not_file.txt", content_type="text/plain")
        resp = await client.post(f"/api/directory-upload?dir={tmp_path}", data=form)
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "missing_required_fields"


@pytest.mark.asyncio
async def test_fresh_upload_succeeds_with_windows_link_shape(
    tmp_path: Path, mock_sel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directory upload accepts plain-link publication when /proc is unavailable."""
    real_isdir = os.path.isdir

    def _without_proc_self_fd(path) -> bool:
        if os.fspath(path) == "/proc/self/fd":
            return False
        return real_isdir(path)

    monkeypatch.setattr(os.path, "isdir", _without_proc_self_fd)
    monkeypatch.setattr(
        os,
        "supports_follow_symlinks",
        set(os.supports_follow_symlinks) - {os.link},
    )

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(tmp_path),
            content=b"plain-link payload",
            filename="new-file.txt",
        )
        assert resp.status == 200, await resp.text()
    assert (tmp_path / "new-file.txt").read_bytes() == b"plain-link payload"


@pytest.mark.asyncio
async def test_fresh_upload_succeeds_on_linkless_filesystem(
    tmp_path: Path, mock_sel, monkeypatch
) -> None:
    """On a filesystem without hard-link support (FAT/exFAT, SMB/CIFS), the
    ``create_only`` publish falls back to ``platform_compat.rename_noreplace``
    -- still one atomic only-if-absent syscall, never a claim-then-replace
    two-step. Where that primitive is available (Linux/macOS via
    ``renameat2``/``renameatx_np``) the upload must land the file exactly
    once, not crash with a 500. Where it is NOT available (Windows, which has
    neither hard links working here NOR ``RENAME_NOREPLACE_AVAILABLE``), the
    fix's own contract is to fail closed rather than risk an unconditional
    replace -- asserted here as ``atomic_create_unsupported`` (501), honestly
    gated rather than asserting the POSIX success path on a platform that
    cannot safely provide it.
    """
    from windows_sim import link_unsupported

    with link_unsupported():
        async with TestClient(TestServer(_make_app())) as client:
            resp = await _upload(
                client,
                directory=str(tmp_path),
                content=b"payload on a linkless fs",
                filename="new-file.txt",
            )
            if platform_compat.RENAME_NOREPLACE_AVAILABLE:
                assert resp.status == 200, await resp.text()
            else:
                assert resp.status == 501, await resp.text()
                body = await resp.json()
                assert body["code"] == "atomic_create_unsupported"
    if platform_compat.RENAME_NOREPLACE_AVAILABLE:
        assert (tmp_path / "new-file.txt").read_bytes() == b"payload on a linkless fs"
    else:
        assert not (tmp_path / "new-file.txt").exists()


@pytest.mark.asyncio
async def test_linkless_filesystem_collision_still_reports_name_collision(
    tmp_path: Path, mock_sel, monkeypatch
) -> None:
    """Even without hard links, a name collision is still detected and
    reported as ``name_collision`` — the ``O_CREAT|O_EXCL`` fallback
    fails with ``FileExistsError`` just like ``os.link`` would."""
    from windows_sim import link_unsupported

    (tmp_path / "existing.txt").write_bytes(b"rival")
    with link_unsupported():
        async with TestClient(TestServer(_make_app())) as client:
            resp = await _upload(
                client,
                directory=str(tmp_path),
                content=b"mine",
                filename="existing.txt",
            )
            assert resp.status == 409, await resp.text()
            body = await resp.json()
    assert body["code"] == "name_collision"
    assert (tmp_path / "existing.txt").read_bytes() == b"rival"


@pytest.mark.asyncio
async def test_linkless_fallback_refuses_a_rival_planted_at_the_last_instant(
    tmp_path: Path, mock_sel, monkeypatch
) -> None:
    """The linkless (``os.link``-unsupported) fallback publishes through
    ``platform_compat.rename_noreplace`` -- ONE atomic only-if-absent
    syscall -- rather than an ``O_CREAT|O_EXCL`` claim followed by a separate
    replacing rename. The claim-then-replace shape only narrows the race
    between two concurrent creates: the claim proves the name was free AT
    CLAIM TIME, and a rival's own publish landing in the window before an
    unconditional replace following it would be silently clobbered by that
    replace. Injecting the rival's write immediately
    before the REAL ``rename_noreplace`` call (skipping ``os.link`` via
    ``link_unsupported``) proves the new publish still refuses even a plant
    that lands at the very last possible instant -- there is no window left
    to land it in, atomically or otherwise.

    Skipped where ``platform_compat.RENAME_NOREPLACE_AVAILABLE`` is False
    (pre-5.3 Linux kernels, Windows, most non-Linux/macOS platforms): there
    the fix's own behavior is to FAIL rather than fall back further, which
    is exercised by a separate test rather than asserted here as if the
    primitive existed.
    """
    if not platform_compat.RENAME_NOREPLACE_AVAILABLE:
        pytest.skip("platform_compat.rename_noreplace is unavailable on this platform")
    from windows_sim import link_unsupported

    real_rename_noreplace = platform_compat.rename_noreplace

    def _rival_wins_then_publish(src, dst, **kwargs):
        # dst is a bare basename here (dir_fd-relative); resolve it against
        # tmp_path (the only directory this test ever targets) to plant the
        # rival at the real destination the syscall is about to name.
        (tmp_path / dst).write_bytes(b"rival's bytes")
        return real_rename_noreplace(src, dst, **kwargs)

    monkeypatch.setattr(platform_compat, "rename_noreplace", _rival_wins_then_publish)

    with link_unsupported():
        async with TestClient(TestServer(_make_app())) as client:
            resp = await _upload(
                client, directory=str(tmp_path), content=b"mine", filename="race.txt"
            )
            assert resp.status == 409, await resp.text()
            body = await resp.json()
    assert body["code"] == "name_collision"
    assert (tmp_path / "race.txt").read_bytes() == b"rival's bytes"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="simulates the POSIX pinned-descriptor path; a Windows host has no dir_fd pin and refuses ancestor_pin_unsupported",
)
@pytest.mark.asyncio
async def test_macos_acl_apply_failure_is_reported_as_preservation_refusal(
    tmp_path: Path, mock_sel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A macOS acl_set_fd failure is a 409 and leaves the old inode intact."""
    import kiro_crew.atomic_write as aw

    existing = tmp_path / "secret.txt"
    existing.write_bytes(b"original")
    snapshot = object()
    acl_calls: list[tuple[str, int, object | None]] = []
    real_isdir = aw.os.path.isdir
    monkeypatch.setattr(files_mod.platform_compat, "IS_MACOS", True)
    monkeypatch.setattr(files_mod.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(aw, "ACCESS_CONTROL_XATTRS_SUPPORTED", False)
    monkeypatch.setattr(
        aw.os.path,
        "isdir",
        lambda path: False if path == "/proc/self/fd" else real_isdir(path),
    )

    def _snapshot_acl(fd: int) -> object:
        acl_calls.append(("snapshot", fd, None))
        return snapshot

    monkeypatch.setattr(
        aw.platform_compat,
        "snapshot_macos_acl",
        _snapshot_acl,
        raising=False,
    )

    def _refuse_apply(fd: int, got_snapshot: object) -> None:
        acl_calls.append(("apply", fd, got_snapshot))
        raise platform_compat.MacOSAclError(errno.EACCES, "macOS ACL write refused")

    monkeypatch.setattr(
        aw.platform_compat,
        "apply_macos_acl",
        _refuse_apply,
        raising=False,
    )

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(tmp_path),
            content=b"replacement",
            filename="secret.txt",
            overwrite=True,
        )
        assert resp.status == 409, await resp.text()
        body = await resp.json()

    assert body["code"] == "access_control_preservation_failed"
    assert existing.read_bytes() == b"original"
    assert [call[0] for call in acl_calls] == ["snapshot", "apply"]
    assert acl_calls[1][2] is snapshot


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="simulates the POSIX pinned-descriptor path; a Windows host has no dir_fd pin and refuses ancestor_pin_unsupported",
)
@pytest.mark.asyncio
async def test_macos_overwrite_open_failure_is_not_misreported_as_name_collision(
    tmp_path: Path, mock_sel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A confirmed Replace that cannot open the source reports the real refusal."""
    import kiro_crew.atomic_write as aw

    existing = tmp_path / "secret.txt"
    existing.write_bytes(b"original")
    real_isdir = aw.os.path.isdir
    monkeypatch.setattr(files_mod.platform_compat, "IS_MACOS", True)
    monkeypatch.setattr(files_mod.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(aw, "ACCESS_CONTROL_XATTRS_SUPPORTED", False)
    monkeypatch.setattr(
        aw.os.path,
        "isdir",
        lambda path: False if path == "/proc/self/fd" else real_isdir(path),
    )

    def _open_denied(*_args, **_kwargs) -> int:
        raise OSError(errno.EACCES, "source cannot be opened for ACL carry")

    monkeypatch.setattr(files_mod, "open_access_control_source", _open_denied)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(tmp_path),
            content=b"replacement",
            filename="secret.txt",
            overwrite=True,
        )
        assert resp.status == 409, await resp.text()
        body = await resp.json()

    assert body["code"] == "access_control_preservation_failed"
    assert existing.read_bytes() == b"original"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="simulates the POSIX pinned-descriptor path; a Windows host has no dir_fd pin and refuses ancestor_pin_unsupported",
)
@pytest.mark.asyncio
async def test_overwrite_source_open_symlink_race_keeps_symlink_refusal(
    tmp_path: Path, mock_sel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaf changed to a symlink after classification remains a link refusal."""
    existing = tmp_path / "secret.txt"
    existing.write_bytes(b"original")
    monkeypatch.setattr(files_mod.platform_compat, "IS_WINDOWS", False)

    def _link_refused(*_args, **_kwargs) -> int:
        raise OSError(errno.ELOOP, "leaf became a symlink")

    monkeypatch.setattr(files_mod, "open_access_control_source", _link_refused)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(tmp_path),
            content=b"replacement",
            filename="secret.txt",
            overwrite=True,
        )
        assert resp.status == 403, await resp.text()
        body = await resp.json()

    assert body["code"] == "symlink_refused"
    assert existing.read_bytes() == b"original"
