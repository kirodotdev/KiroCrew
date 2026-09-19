"""Obsidian attachment support in the Notes (md-notebook) backend.

A vault written with Obsidian embeds images as ``![[file.png]]`` and finds the
file through ``.obsidian/app.json``'s ``attachmentFolderPath``. The backend
exposes that setting on one per-vault route, so the page can resolve an embed
the way Obsidian does by default; it builds no index of the vault's files.
These tests pin what the route says about an Obsidian vault's setting, and
how the settings file is reached (never through a link). The vault listing
itself reads no vault tree, so one unreachable vault cannot hold the others
back.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from test_md_notebook import _clone, _seed_template, fixtures, signed_client

from conftest import requires_symlinks
from kiro_crew import pinned_fs, platform_compat

# The seed-repo fixtures live in the main suite module, and pytest registers a
# fixture under the name it holds in the REQUESTING module, so both are
# re-exported here rather than re-spelled. Listing them is also what tells
# flake8 the imports are used; each test's `fixtures` parameter would
# otherwise read as a redefinition.
__all__ = ["_seed_template", "fixtures"]

# A vault becomes an Obsidian one by carrying this directory. The seed repos the
# shared fixture builds have none, so a test that needs one writes it.
OBSIDIAN_DIR = ".obsidian"


def _make_obsidian(root: Path, settings: object) -> None:
    (root / OBSIDIAN_DIR).mkdir()
    text = settings if isinstance(settings, str) else json.dumps(settings)
    (root / OBSIDIAN_DIR / "app.json").write_text(text, encoding="utf-8")


@pytest.mark.asyncio
async def test_plain_vault_has_no_attachment_folder(fixtures) -> None:
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        await _clone(client, remote)
        status, body = await client.get("/api/attachments")
        assert status == 200
        assert body["attachmentFolderPath"] is None
        # The listing is the app's entry call and reads no vault tree.
        _status, listing = await client.get("/api/vaults")
        assert "attachmentFolderPath" not in listing["vaults"][0]


@pytest.mark.asyncio
async def test_obsidian_vault_exposes_its_attachment_folder(fixtures) -> None:
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        _make_obsidian(Path(vault["localPath"]), {"attachmentFolderPath": "z-assets"})
        status, body = await client.get("/api/attachments")
        assert status == 200
        assert body["attachmentFolderPath"] == "z-assets"
        # Computed on read, never written back to the registry.
        assert "attachmentFolderPath" not in server_mod._read_vaults_sync()[0]


@pytest.mark.asyncio
async def test_obsidian_relative_forms_are_kept_verbatim(fixtures) -> None:
    """``./`` and ``./sub`` mean "next to the note"; the page interprets them."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        _make_obsidian(Path(vault["localPath"]), {"attachmentFolderPath": "./attachments"})
        _status, body = await client.get("/api/attachments")
        assert body["attachmentFolderPath"] == "./attachments"


@pytest.mark.parametrize(
    "settings",
    [
        "not json at all",
        json.dumps([1, 2, 3]),
        json.dumps({"attachmentFolderPath": 42}),
        json.dumps({"attachmentFolderPath": ""}),
        json.dumps({"attachmentFolderPath": "/"}),
        # A NUL byte is refused by safe_join's component stage on every OS.
        json.dumps({"attachmentFolderPath": "z-assets\u0000"}),
        json.dumps({"readableLineLength": False}),
        # Nested past the decoder's recursion limit: corrupt, not a crash.
        "[" * 5000,
        # An integer literal past the int-parser digit limit raises a bare
        # ValueError, not JSONDecodeError: still corrupt settings, not a 500.
        '{"attachmentFolderPath": ' + "9" * 5000 + "}",
    ],
)
@pytest.mark.asyncio
async def test_missing_or_corrupt_setting_keeps_the_vault_usable(fixtures, settings: str) -> None:
    """A vault opens with no attachment folder rather than not at all."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        _make_obsidian(Path(vault["localPath"]), settings)
        status, body = await client.get("/api/attachments")
        assert status == 200
        assert body["attachmentFolderPath"] is None
        assert (await client.get("/api/notes"))[0] == 200


def _real_vault_behind_a_link(tmp_path: Path) -> Path:
    """`real/vault`, an Obsidian vault, plus `link -> real`."""
    real = tmp_path / "real" / "vault"
    real.mkdir(parents=True)
    _make_obsidian(real, {"attachmentFolderPath": "z-assets"})
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    return real


@requires_symlinks
def test_linked_vault_root_is_never_read(fixtures, tmp_path: Path) -> None:
    """A vault whose ROOT is a link reads as no settings.

    A leaf check cannot help here: the first `is_dir()` on `.obsidian` would
    already traverse the link. The read refuses in the open that would have
    entered the root, so the setting reads as absent -- the target tree is
    never probed.
    """
    server_mod, _remote, _seed = fixtures
    real = _real_vault_behind_a_link(tmp_path)
    # Quick check: reached directly, the same vault reads normally.
    assert server_mod._obsidian_attachment_folder_sync(real) == "z-assets"

    linked = tmp_path / "vault-link"
    linked.symlink_to(real, target_is_directory=True)
    assert server_mod._obsidian_attachment_folder_sync(linked) is None


@requires_symlinks
@pytest.mark.parametrize("arm", ["relative", "by-path"])
def test_vault_under_a_linked_ancestor(
    fixtures, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    """An ancestor of the root that is a link: settled once (POSIX) or refused (Windows).

    On POSIX the root is opened through `pinned_fs.open_dir_pinned`: the parent
    is resolved ONCE and then opened component by component, each relative to
    the one before and each refusing a link in its own open. A link that was
    already there is followed by that single resolution -- the vault is where
    it really is, and `/tmp` on macOS is itself a link -- so the setting reads
    normally; a link planted on the chain AFTER the resolution fails the
    component's `O_NOFOLLOW` open, which is the check-to-use window the earlier
    probe left open (`pinned_fs` tests pin that property). Windows has no
    relative open, so there the root-first probe still refuses a linked
    ancestor before anything is opened by path.
    """
    server_mod, _remote, _seed = fixtures
    _real_vault_behind_a_link(tmp_path)
    through_link = tmp_path / "link" / "vault"
    if arm == "by-path":
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        assert server_mod._obsidian_attachment_folder_sync(through_link) is None
        return
    if not pinned_fs.supports_pinned_walk():
        pytest.skip("descriptor-relative opens need POSIX dir_fd support")
    assert server_mod._obsidian_attachment_folder_sync(through_link) == "z-assets"


def _spy_opens(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object, int]]:
    """Record every `os.open` as `(path-as-given, dir_fd, flags)`, then perform it.

    Replacing `os.open` removes it from `os.supports_dir_fd`, which is exactly the
    set `pinned_fs.supports_pinned_walk()` consults, so the platform's real answer
    is frozen first -- a test that pins that answer itself patches it afterwards.
    """
    supported = pinned_fs.supports_pinned_walk()
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: supported)
    real_open = os.open
    calls: list[tuple[str, object, int]] = []

    def spy(path, flags, mode=0o777, *, dir_fd=None):
        calls.append((os.fspath(path), dir_fd, flags))
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", spy)
    return calls


def _obsidian_vault(tmp_path: Path) -> Path:
    """An Obsidian vault whose setting names `z-assets`."""
    root = tmp_path / "vault"
    root.mkdir()
    _make_obsidian(root, {"attachmentFolderPath": "z-assets"})
    return root


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(),
    reason="descriptor-relative opens need POSIX dir_fd support",
)
def test_descent_below_the_root_is_relative_to_the_held_descriptor(
    fixtures, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the parent of the root is resolved, nothing is opened by path again.

    Opening the root BY PATH would resolve every ancestor by name, and a rename
    above the vault between two such opens (POSIX holds no lock against it)
    would make the same name a different directory -- one open would have
    checked one tree and the next read another. So the root is reached the way
    `pinned_fs.open_dir_pinned` reaches it: the filesystem root is the ONLY
    open by path, and every component of the vault's resolved path, the root's
    own name included, is an `openat` on the descriptor before it. The same
    holds below the root: `.obsidian` and `app.json` are a bare name, a
    `dir_fd`, and the object opened is a child of the directory that was
    actually opened, whatever the path now names.
    """
    server_mod, _remote, _seed = fixtures
    root = _obsidian_vault(tmp_path)
    calls = _spy_opens(monkeypatch)
    assert server_mod._obsidian_attachment_folder_sync(root) == "z-assets"
    by_path = [(p, fd) for p, fd, _ in calls if fd is None]
    relative = [(p, fd) for p, fd, _ in calls if fd is not None]
    # One chain, started at the filesystem root, by path: the one name that
    # cannot be swapped for a link.
    assert [p for p, _ in by_path] == [os.sep]
    # Everything else is a bare name resolved against a held descriptor: the
    # vault's own resolved path (its parent as resolved once, then its name),
    # then the settings read below it.
    chain = list(Path(os.path.realpath(root.parent)).parts[1:]) + [root.name]
    assert sorted(p for p, _ in relative) == sorted(chain + [".obsidian", "app.json"])
    assert all(isinstance(fd, int) and fd >= 0 for _, fd in relative)
    # The one FILE open never blocks: a FIFO at the name returns at once and is
    # refused by the fstat that follows, instead of parking the worker thread.
    (app_json_flags,) = [flags for p, _, flags in calls if p == "app.json"]
    assert app_json_flags & os.O_NONBLOCK


def test_without_dir_fd_the_descent_opens_by_path_under_the_pin(
    fixtures, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows arm, exercised everywhere: no relative open, same answers.

    Where `dir_fd` is not available the child is opened by its full path while
    the parent's descriptor is still held -- on Windows that handle denies the
    rename a swap would need. The result is identical to the relative read, and
    the link refusals (`_pinned_dir`, `open_file_no_reparse`) are the ones
    every other test here already pins.
    """
    server_mod, _remote, _seed = fixtures
    root = _obsidian_vault(tmp_path)
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    # The seam is `platform_compat`, not `os.open`: on Windows `pin_directory`
    # and `open_file_no_reparse` are `CreateFileW`, so spying `os.open` there
    # records nothing (the round-15 Windows shard learned this the hard way).
    pinned: list[str] = []
    opened: list[tuple[str, bool]] = []
    real_pin = platform_compat.pin_directory
    real_open = platform_compat.open_file_no_reparse

    def pin_spy(path):
        pinned.append(os.fspath(path))
        return real_pin(path)

    def open_spy(path, *, nonblocking=False):
        opened.append((os.fspath(path), nonblocking))
        return real_open(path, nonblocking=nonblocking)

    monkeypatch.setattr(platform_compat, "pin_directory", pin_spy)
    monkeypatch.setattr(platform_compat, "open_file_no_reparse", open_spy)
    assert server_mod._obsidian_attachment_folder_sync(root) == "z-assets"
    # Each level is pinned by its FULL path, and the file is opened by path,
    # non-blocking, under that pin.
    assert sorted(pinned) == sorted([str(root), str(root / ".obsidian")])
    assert opened == [(str(root / ".obsidian" / "app.json"), True)]
    assert all(os.path.isabs(p) for p in pinned)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs need a POSIX filesystem")
@pytest.mark.parametrize("arm", ["relative", "by-path"])
def test_a_fifo_at_app_json_is_refused_without_blocking(
    fixtures, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    """A FIFO planted at `.obsidian/app.json` reads as no settings, at once.

    `O_NOFOLLOW` refuses a link but not a FIFO, and a blocking open of a FIFO
    with no writer never returns -- it would park one of the thread pool's
    workers for good, and a vault is untrusted content. Both arms of
    `_open_file_in_pinned` open non-blocking and `fstat` the descriptor, so the
    FIFO is refused in the same operation that opens it. The read runs on a
    thread with a deadline so a regression fails the test instead of hanging it.
    """
    server_mod, _remote, _seed = fixtures
    root = tmp_path / "vault"
    (root / OBSIDIAN_DIR).mkdir(parents=True)
    os.mkfifo(root / OBSIDIAN_DIR / "app.json")
    if arm == "by-path":
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)

    result: list[object] = []
    worker = threading.Thread(
        target=lambda: result.append(server_mod._obsidian_attachment_folder_sync(root)),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "the settings read blocked on the FIFO"
    assert result == [None]


@pytest.mark.parametrize("what", ["missing", "file"])
def test_pinned_dir_yields_none_for_anything_but_a_directory(
    fixtures, tmp_path: Path, what: str
) -> None:
    """A missing name or a plain file is not a directory to hold; the block sees None."""
    server_mod, _remote, _seed = fixtures
    path = tmp_path / "thing"
    if what == "file":
        path.write_bytes(b"x")
    with server_mod._pinned_dir(path) as fd:
        assert fd is None
    with server_mod._pinned_dir(tmp_path) as fd:
        assert isinstance(fd, int) and fd >= 0


@pytest.mark.parametrize(
    "escape",
    ["/etc", "../outside", "a/../../b", "C:\\attachments", "\\\\server\\share"],
)
@pytest.mark.asyncio
async def test_setting_that_escapes_the_vault_is_dropped(fixtures, escape: str) -> None:
    """The setting is vault content: a crafted value must not name a folder outside."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        _make_obsidian(Path(vault["localPath"]), {"attachmentFolderPath": escape})
        _status, body = await client.get("/api/attachments")
        assert body["attachmentFolderPath"] is None


@requires_symlinks
@pytest.mark.asyncio
@pytest.mark.parametrize("linked", ["dir", "file"])
async def test_linked_obsidian_settings_are_never_followed(
    fixtures, tmp_path: Path, linked: str
) -> None:
    """A `.obsidian` link, or an `app.json` link, is treated as no settings.

    Following either would dereference vault content that may point anywhere,
    including a UNC share on Windows. The check happens before the probe, so the
    target directory is never touched and the vault reads as a plain one.
    """
    server_mod, remote, _seed = fixtures
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "app.json").write_text(
        json.dumps({"attachmentFolderPath": "z-assets"}), encoding="utf-8"
    )
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        if linked == "dir":
            (root / OBSIDIAN_DIR).symlink_to(outside, target_is_directory=True)
        else:
            (root / OBSIDIAN_DIR).mkdir()
            (root / OBSIDIAN_DIR / "app.json").symlink_to(outside / "app.json")
        status, body = await client.get("/api/attachments")
        assert status == 200
        assert body["attachmentFolderPath"] is None


@pytest.mark.asyncio
async def test_attachment_setting_is_read_from_the_vault_root_not_the_subfolder(fixtures) -> None:
    """`.obsidian/` sits at the vault root, so a scoped vault still reads its setting."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote, subfolder="sub")
        _make_obsidian(Path(vault["localPath"]), {"attachmentFolderPath": "z-assets"})
        _status, body = await client.get("/api/attachments")
        assert body == {"attachmentFolderPath": "z-assets"}


@pytest.mark.asyncio
async def test_attachment_setting_requires_a_vault(fixtures) -> None:
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, _body = await client.get("/api/attachments?vault=nope")
        assert status == 404
