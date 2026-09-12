"""Obsidian attachment support in the Notes (md-notebook) backend.

A vault written with Obsidian embeds images as ``![[file.png]]`` and finds the
file through ``.obsidian/app.json``'s ``attachmentFolderPath``. The backend
exposes that setting together with an index of the image files that exist, on
one per-vault route, so the page can resolve an embed the way Obsidian does.
These tests pin the two halves: what the route says about an Obsidian vault's
setting, and which files the index reports. The vault listing itself reads no
vault tree, so one unreachable vault cannot hold the others back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_md_notebook import _clone, _seed_template, fixtures, signed_client

from conftest import requires_symlinks

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


def _touch(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")


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


@pytest.mark.parametrize("where", ["root", "ancestor"])
def test_linked_vault_root_or_ancestor_is_never_walked(
    fixtures, tmp_path: Path, where: str
) -> None:
    """A vault reached through a linked root or a linked ancestor yields nothing.

    Leaf checks cannot help here: the first `is_dir()` on `.obsidian`, or the
    first `os.walk` step, would already traverse the link. Both entry points
    refuse before touching anything below the root, so the settings read as
    absent and the index as empty -- the target tree is never probed. A linked
    ROOT is refused by the pinned open itself; a linked ANCESTOR by the
    root-first probe that runs before that open.
    """
    server_mod, _remote, _seed = fixtures
    real = tmp_path / "real" / "vault"
    real.mkdir(parents=True)
    _make_obsidian(real, {"attachmentFolderPath": "z-assets"})
    _touch(real, "z-assets/pic.png")
    # Quick check: reached directly, the same vault indexes normally.
    assert server_mod._obsidian_attachment_folder_sync(real) == "z-assets"
    assert server_mod._list_attachment_files_sync(real, "z-assets") == ["z-assets/pic.png"]

    if where == "root":
        linked = tmp_path / "link"
        linked.symlink_to(real, target_is_directory=True)
    else:
        (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
        linked = tmp_path / "link" / "vault"
    assert server_mod._obsidian_attachment_folder_sync(linked) is None
    assert server_mod._list_attachment_files_sync(linked, "z-assets") == []


@requires_symlinks
def test_pinned_walk_enters_only_real_directories(fixtures, tmp_path: Path) -> None:
    """The walk lists what `os.walk` would, minus anything reached through a link.

    Every directory is entered through an open that refuses a link at the name,
    so a symlinked directory is neither named nor descended into, and a
    symlinked FILE is not named either (the file endpoint would refuse the link
    anyway, so listing it could only produce a failing image). Pruning
    `dirnames` in place before the generator resumes skips the subtree, as with
    `os.walk`.
    """
    server_mod, _remote, _seed = fixtures
    root = tmp_path / "vault"
    _touch(root, "a.png")
    _touch(root, "sub/b.png")
    _touch(root, "sub/deep/c.png")
    _touch(root, "skip/d.png")
    outside = tmp_path / "outside"
    _touch(outside, "leak.png")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "alias.png").symlink_to(outside / "leak.png")

    seen: list[tuple[str, list[str], list[str]]] = []
    for rel_dir, dirnames, filenames in server_mod._walk_pinned(root):
        if rel_dir == "":
            dirnames.remove("skip")
        seen.append((rel_dir, list(dirnames), list(filenames)))
    assert seen == [
        ("", ["sub"], ["a.png"]),
        ("sub", ["deep"], ["b.png"]),
        ("sub/deep", [], ["c.png"]),
    ]


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
    assert server_mod._list_attachment_files_sync(path, None) == []


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
async def test_attachment_index_lists_images_and_skips_the_rest(fixtures) -> None:
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        _touch(root, "z-assets/Pasted image 20260706094611.png")
        _touch(root, "z-assets/diagram.SVG")
        _touch(root, "sub/inline.jpeg")
        _touch(root, "cover.webp")
        _touch(root, "z-assets/script.py")  # not an image
        _touch(root, ".hidden/secret.png")  # dotted directory, not the attachment folder
        _touch(root, "z-assets/.thumb.png")  # dotted file
        status, body = await client.get("/api/attachments")
        assert status == 200, body
        assert body["files"] == [
            "cover.webp",
            "sub/inline.jpeg",
            "z-assets/Pasted image 20260706094611.png",
            "z-assets/diagram.SVG",
        ]


@pytest.mark.asyncio
async def test_attachment_index_walks_a_dotted_attachment_folder(fixtures) -> None:
    """A vault that keeps images in ``.attachments/`` has every embed pointing there."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        _make_obsidian(root, {"attachmentFolderPath": ".attachments/img"})
        _touch(root, ".attachments/img/a.png")
        _touch(root, ".attachments/img/deep/b.png")
        _touch(root, ".attachments/.cache/c.png")  # dotted below the kept folder: pruned
        _touch(root, ".cache/d.png")  # sibling dotted tree: pruned
        _touch(root, ".git/objects/e.png")
        _status, body = await client.get("/api/attachments")
        assert body["files"] == [".attachments/img/a.png", ".attachments/img/deep/b.png"]


@pytest.mark.asyncio
async def test_attachment_index_reads_the_vault_root_not_the_subfolder(fixtures) -> None:
    """Obsidian's setting is vault-relative, so a scoped vault's images can sit outside its scope."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote, subfolder="sub")
        root = Path(vault["localPath"])
        _make_obsidian(root, {"attachmentFolderPath": "z-assets"})
        _touch(root, "z-assets/a.png")
        _status, body = await client.get("/api/attachments")
        assert body["files"] == ["z-assets/a.png"]


@requires_symlinks
@pytest.mark.asyncio
async def test_attachment_index_does_not_follow_a_symlinked_directory(
    fixtures, tmp_path: Path
) -> None:
    server_mod, remote, _seed = fixtures
    outside = tmp_path / "outside"
    outside.mkdir()
    _touch(outside, "leak.png")
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        (root / "linked").symlink_to(outside, target_is_directory=True)
        _status, body = await client.get("/api/attachments")
        assert body["files"] == []


@pytest.mark.asyncio
async def test_attachment_index_requires_a_vault(fixtures) -> None:
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, _body = await client.get("/api/attachments?vault=nope")
        assert status == 404
