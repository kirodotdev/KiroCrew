"""Tests for ``api_directory_upload`` (POST /api/directory-upload).

Backs the Files panel's drag-and-drop and "Upload files..." row-menu actions:
unlike ``api_upload_file`` (which always writes into the fixed chat-attachment
scratch directory), this endpoint writes into a caller-supplied directory
anywhere the dashboard's own path gate permits.

Covers: the happy path, the create-only name-collision contract (an existing
name is always a 409, even for a request that still sends ``overwrite=1``),
the reused extension allowlist and size cap, the magic-byte content-signature
gate, invalid/sensitive/missing directories, that a path-traversal attempt via the uploaded filename lands
inside the target directory rather than escaping it, that a sensitive
DESTINATION is refused even when the directory alone would pass (the crew-home
keystone leaves), and that a symlink/junction planted at the destination name
is refused rather than followed on every platform.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
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

    ``/api/directory-upload`` creates files anywhere off the
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
) -> aiohttp.ClientResponse:
    form = aiohttp.FormData()
    form.add_field("file", content, filename=filename, content_type="text/plain")
    return await client.post(f"/api/directory-upload?dir={directory}", data=form)


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
    # The mode is the one atomic_write's default gives a new file in the same place.
    sibling = tmp_path / "sibling.txt"
    atomic_write(sibling, b"x")
    assert stat.S_IMODE(written.stat().st_mode) == stat.S_IMODE(sibling.stat().st_mode)


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
async def test_name_collision_is_refused(tmp_path: Path, mock_sel) -> None:
    existing = tmp_path / "report.txt"
    existing.write_bytes(b"original")
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client, directory=str(tmp_path), content=b"new content", filename="report.txt"
        )
        assert resp.status == 409, await resp.text()
        body = await resp.json()
    assert body["code"] == "name_collision"
    assert existing.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_name_collision_is_refused_even_when_overwrite_is_requested(
    tmp_path: Path, mock_sel
) -> None:
    """The endpoint never replaces a file: a request that sends the old
    ``overwrite=1`` query parameter still gets ``409 name_collision`` and the
    original bytes stay as they were."""
    existing = tmp_path / "report.txt"
    existing.write_bytes(b"original")
    form = aiohttp.FormData()
    form.add_field("file", b"replacement", filename="report.txt", content_type="text/plain")
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(f"/api/directory-upload?dir={tmp_path}&overwrite=1", data=form)
        assert resp.status == 409, await resp.text()
        body = await resp.json()
    assert body["code"] == "name_collision"
    assert existing.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_concurrent_create_lands_only_the_winner(
    tmp_path: Path, mock_sel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-name race between two concurrent CREATE uploads: both classify
    the destination as missing, but a rival claims the name before THIS
    request's create runs. The create is ``O_EXCL``, so it fails with
    ``FileExistsError`` (reported as ``name_collision``) and the rival's bytes
    stay as they were.

    The rival's file is planted from ``is_owner_only_target``, the last call
    before the create on both the pinned and the by-name branch, so the REAL
    ``O_EXCL`` open meets a genuinely present file.
    """
    real_is_owner_only_target = files_mod.is_owner_only_target

    def _rival_wins_then_create(path):
        Path(path).write_bytes(b"rival's bytes")
        return real_is_owner_only_target(path)

    monkeypatch.setattr(files_mod, "is_owner_only_target", _rival_wins_then_create)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(client, directory=str(tmp_path), content=b"mine", filename="race.txt")
        assert resp.status == 409, await resp.text()
        body = await resp.json()
    assert body["code"] == "name_collision"
    assert (tmp_path / "race.txt").read_bytes() == b"rival's bytes"


@pytest.mark.asyncio
async def test_existing_directory_name_is_refused(tmp_path: Path, mock_sel) -> None:
    (tmp_path / "report.txt").mkdir()
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(tmp_path),
            content=b"x",
            filename="report.txt",
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
    ],
    ids=[
        "question-mark",
        "less-than",
        "trailing-dot",
        "trailing-space",
        "reserved-name",
        "reserved-name-with-extension",
    ],
)
async def test_windows_invalid_filename_is_rejected_before_any_write(
    tmp_path: Path, mock_sel, monkeypatch, filename: str
) -> None:
    """On a Windows host a name Windows cannot store answers a clean 400
    instead of the create's OSError."""
    monkeypatch.setattr(files_mod.platform_compat, "IS_WINDOWS", True)
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
@pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
@pytest.mark.parametrize("control", ["\x01", "\x7f", "\x85"], ids=["c0", "del", "c1"])
async def test_control_character_filename_is_rejected_on_every_platform(
    tmp_path: Path, mock_sel, monkeypatch, windows: bool, control: str
) -> None:
    monkeypatch.setattr(files_mod.platform_compat, "IS_WINDOWS", windows)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload_raw_filename(
            client,
            directory=str(tmp_path),
            content=b"must not be published",
            filename=f"x{control}y.txt",
        )
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "invalid_filename"
    assert not any(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disposition",
    ['form-data; name="file"; filename=""', 'form-data; name="file"'],
    ids=["empty-filename", "no-filename"],
)
async def test_empty_filename_is_rejected_as_invalid_filename(
    tmp_path: Path, mock_sel, disposition: str
) -> None:
    boundary = "kirocrew-directory-upload-test"
    body = (
        f"--{boundary}\r\nContent-Disposition: {disposition}\r\n"
        "Content-Type: text/plain\r\n\r\nmust not be published\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(
            f"/api/directory-upload?dir={tmp_path}",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        assert resp.status == 400, await resp.text()
        payload = await resp.json()
    assert payload["code"] == "invalid_filename"
    assert not any(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.skipif(
    platform_compat.IS_WINDOWS, reason="the host filesystem cannot store these names"
)
@pytest.mark.parametrize("filename", ["a:b.txt", "a?b.txt", "con.md"])
async def test_windows_only_names_upload_on_posix(tmp_path: Path, mock_sel, filename: str) -> None:
    """A Linux or macOS host stores a name only Windows refuses, as typed."""
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload_raw_filename(
            client,
            directory=str(tmp_path),
            content=b"payload",
            filename=filename,
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    assert body["path"] == str(tmp_path / filename)
    assert (tmp_path / filename).read_bytes() == b"payload"


@pytest.mark.asyncio
@pytest.mark.skipif(
    platform_compat.IS_WINDOWS, reason="the trailing-dot name rule applies on Windows"
)
async def test_trailing_dot_is_not_a_name_refusal_on_posix(tmp_path: Path, mock_sel) -> None:
    """On POSIX ``notes.`` passes the name rule; its empty suffix is then
    refused by the extension allowlist, like any extension-less name."""
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload_raw_filename(
            client,
            directory=str(tmp_path),
            content=b"payload",
            filename="notes.",
        )
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "unsupported_file_type"
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
    monkeypatch.setattr("kiro_crew.dashboard.handlers.files.upload_max_bytes", lambda: 8)
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
async def test_keystone_destination_is_refused(crew_home: Path, mock_sel, leaf_name: str) -> None:
    """The crew-home ROOT passes ``_validate_dashboard_path`` on its own (it
    is deliberately not itself sensitive -- only its leaves are), so naming a
    keystone leaf as the UPLOADED FILENAME while ``dir`` is the crew-home root
    must still be refused by the re-gated destination check. This is the
    exact escape the destination re-validation in ``api_directory_upload``
    closes: ``dir=<crew-home>`` with a ``file`` part named after an absent
    keystone leaf must never create it."""
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(crew_home),
            content=b"forged content",
            filename=leaf_name,
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
    the destination re-check runs before any collision logic, so the
    original ceiling is provably untouched, not merely "not yet created"."""
    policy = crew_home / "security_policy.json"
    policy.write_text('{"real": "ceiling"}')
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client,
            directory=str(crew_home),
            content=b'{"forged": "ceiling"}',
            filename="security_policy.json",
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
    ("existing_kind", "expected_status", "expected_code"),
    [
        ("dir", 409, "is_a_directory"),
        ("file", 409, "name_collision"),
        ("missing", 200, None),
    ],
)
async def test_by_name_floor_still_classifies_the_destination_correctly(
    tmp_path: Path,
    mock_sel,
    monkeypatch,
    existing_kind: str,
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
@pytest.mark.parametrize("branch", ["pinned", "by-name"])
async def test_a_write_that_fails_mid_body_leaves_nothing_behind(
    tmp_path: Path, mock_sel, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    """The file is created, the first ``os.write`` lands half the body and the
    second fails with ``ENOSPC``: the rollback removes the partly written
    file, so the destination is empty and a retry is not refused 409. Run on
    the pinned branch and on the by-name branch Windows takes."""
    if branch == "pinned" and not files_mod.pinned_fs.supports_pinned_walk():
        pytest.skip("platform without pinned directory descriptors")
    if branch == "by-name":
        monkeypatch.setattr(files_mod.pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(files_mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(files_mod.pinned_fs, "fd_real_path", lambda fd: str(tmp_path))
    payload = b"0123456789" * 8
    half = len(payload) // 2
    real_write = os.write
    calls: list[int] = []

    def _half_then_disk_full(fd, data):
        if bytes(data) in (payload, payload[half:]):
            calls.append(fd)
            if len(calls) == 1:
                return real_write(fd, payload[:half])
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", _half_then_disk_full)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await _upload(
            client, directory=str(tmp_path), content=payload, filename="report.txt"
        )
        assert resp.status == 500, await resp.text()
        body = await resp.json()
    assert body["code"] == "upload_failed"
    assert len(calls) == 2
    assert not (tmp_path / "report.txt").exists()


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


class TestWriteFailureIsACodedError:
    """A write the filesystem refuses answers a coded JSON 500, never aiohttp's bare one.

    The create raises ``PermissionError`` for a destination the gateway cannot
    write (read-only or root-owned) and ``OSError`` for ``ENOSPC``; the rail
    renders ``body.error`` verbatim, so an unhandled raise shows "Internal
    Server Error" with no ``code`` to switch on. The refusal is injected at the
    file flush, which both the pinned and the by-name branch run.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            PermissionError(errno.EACCES, "Permission denied"),
            OSError(errno.ENOSPC, "No space left on device"),
        ],
        ids=["permission-denied", "disk-full"],
    )
    async def test_write_oserror_answers_json_500_with_code(
        self, tmp_path: Path, mock_sel, monkeypatch: pytest.MonkeyPatch, exc: OSError
    ) -> None:
        def _refuse_flush(_fd: int) -> None:
            raise exc

        monkeypatch.setattr(os, "fsync", _refuse_flush)

        async with TestClient(TestServer(_make_app())) as client:
            resp = await _upload(
                client, directory=str(tmp_path), content=b"payload", filename="notes.txt"
            )
            assert resp.status == 500, await resp.text()
            assert resp.content_type == "application/json"
            body = await resp.json()

        assert body["code"] == "upload_failed"
        assert body["error"]
        assert not (tmp_path / "notes.txt").exists()
        assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "failure"
        assert mock_sel.log_api_access.call_args.kwargs["resources"] == str(tmp_path / "notes.txt")
