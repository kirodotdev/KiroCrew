"""Notes (md-notebook) backend: creating, trashing, duplicating, moving and searching notes.

Covers new-note naming, the symlinked-folder and symlinked-scope refusals on create,
save, move, duplicate and delete, delete-to-trash and how sync treats the trash,
duplicate and move, search, and inline ``#tag`` extraction. Shared fixtures live in
``md_notebook_helpers.py``.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest
from md_notebook_helpers import _seed_template  # noqa: F401 - fixture
from md_notebook_helpers import fixtures_fixture  # noqa: F401 - fixture
from md_notebook_helpers import _clone, _git, signed_client

from conftest import requires_symlinks
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.md_notebook import git_ops


@pytest.mark.asyncio
async def test_new_note_names_are_unique(fixtures) -> None:
    """Two quick creations must not collide on a name."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        first = (await client.post("/api/note/new"))[1]["path"]
        second = (await client.post("/api/note/new"))[1]["path"]
        assert first == "Untitled.md"
        assert second == "Untitled 2.md"
        _, read = await client.get(f"/api/note?path={first}")
        # Created empty: the filename is already the title (`note_title` falls back
        # to the basename), so a seeded `# Untitled` heading was a duplicate the
        # user had to delete — and it did not follow a later rename.
        assert read["content"] == ""
        # ...and the listing still names it, from the filename alone.
        titles = {n["path"]: n["title"] for n in (await client.get("/api/notes"))[1]["notes"]}
        assert titles[first] == "Untitled"


@pytest.mark.asyncio
async def test_new_note_in_folder(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, body = await client.post("/api/note/new", {"folder": "sub"})
        assert status == 200
        assert body["path"] == "sub/Untitled.md"


@pytest.mark.asyncio
async def test_new_note_through_a_symlinked_folder_is_refused(fixtures) -> None:
    """A cloned `Projects -> .git/refs/heads` passes `safe_join` (its target is
    inside the vault), so creating a note "in Projects" would write into `.git`.
    The lexical folder path must be refused when any component is a link."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        heads = root / ".git" / "refs" / "heads"
        assert heads.is_dir()
        before = sorted(p.name for p in heads.iterdir())
        try:
            (root / "Projects").symlink_to(
                Path(".git") / "refs" / "heads", target_is_directory=True
            )
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform/filesystem")

        # The link itself, a folder to be created UNDER the link, and the same
        # path with a backslash separator — `safe_join` normalises it to two
        # components, so the guard must see two components as well.
        for folder in ("Projects", "Projects/Sub", "Projects\\Sub"):
            status, body = await client.post("/api/note/new", {"folder": folder})
            assert status == 400, (folder, body)
            assert body["code"] == "folder_is_symlink"
        assert sorted(p.name for p in heads.iterdir()) == before, ".git must be untouched"
        assert not (heads / "Untitled.md").exists()
        assert not (heads / "Sub").exists()


@pytest.mark.asyncio
async def test_save_and_move_through_a_symlinked_folder_are_refused(fixtures) -> None:
    """Same hazard as the create path, on the other writes that mkdir, rename or
    create through folder components: a save into `Projects/x.md`, a move to
    `Projects/x.md` (or out of it) and a duplicate of `Projects/x.md` must not
    follow `Projects -> .git/refs/heads`."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        heads = root / ".git" / "refs" / "heads"
        before = sorted(p.name for p in heads.iterdir())
        try:
            (root / "Projects").symlink_to(
                Path(".git") / "refs" / "heads", target_is_directory=True
            )
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform/filesystem")

        status, body = await client.put("/api/note", {"path": "Projects/Plan.md", "content": "x"})
        assert status == 400, body
        assert body["code"] == "folder_is_symlink"
        status, body = await client.post(
            "/api/note/move", {"from": "One.md", "to": "Projects/One.md"}
        )
        assert status == 400, body
        assert body["code"] == "folder_is_symlink"
        assert (root / "One.md").exists(), "the source must not have moved"
        status, body = await client.post(
            "/api/note/move", {"from": "Projects/Any.md", "to": "Leak.md"}
        )
        assert status == 400, body
        assert body["code"] == "folder_is_symlink"
        # The duplicate creates its copy THROUGH the source's folder too. A
        # source under the link is read through it, so give the link a note to
        # read: the copy would land beside it, inside `.git`.
        (heads / "Any.md").write_text("x", encoding="utf-8")
        try:
            status, body = await client.post("/api/note/duplicate", {"path": "Projects/Any.md"})
            assert status == 400, body
            assert body["code"] == "folder_is_symlink"
        finally:
            (heads / "Any.md").unlink()
        assert sorted(p.name for p in heads.iterdir()) == before, ".git must be untouched"


@pytest.mark.asyncio
async def test_delete_through_a_symlinked_folder_is_refused(fixtures) -> None:
    """Delete renames through the SOURCE's folder components too, not just `.trash`.

    `abs_path` is the lexical `root/rel`, so with `Projects -> public` the
    `os.replace` inside `_to_trash` follows the parent link and moves
    `public/Kept.md` — the link target's note, which nobody asked to delete. The
    other writers already walk these components; this is that same walk on the
    one path that guarded only its `.trash` destination.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        public = root / "public"
        public.mkdir()
        kept = public / "Kept.md"
        kept.write_text("keep me", encoding="utf-8")
        # Junction as well as symlink: a POSIX-only guard lets a Windows junction
        # through, and the component walk must refuse both.
        platform_compat.symlink_or_junction(str(public), str(root / "Projects"))

        status, body = await client.delete("/api/note?path=Projects/Kept.md")
        assert status == 400, body
        assert body["code"] == "folder_is_symlink"
        assert (
            kept.read_text(encoding="utf-8") == "keep me"
        ), "the link target's note was moved into the trash"
        assert not (root / git_ops.TRASH_DIR).exists(), "a refused delete must not create the trash"


@pytest.mark.asyncio
async def test_new_note_in_a_symlinked_scope_is_refused(fixtures) -> None:
    """A subfolder-scoped vault whose scope is itself a tracked
    `Scope -> .git/refs/heads`: `content_root` returns the REALPATH, so a walk
    started there would begin past the link. The scope's own components must be
    walked from the vault root, and the root-level create (empty folder) too."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        # Scope the clone on a folder that does not exist yet, then plant the
        # link in the checkout — planted locally like the other symlink tests,
        # because a committed link checks out as a plain FILE on a Windows
        # runner without `core.symlinks`, which is a different failure.
        vault = await _clone(client, remote, subfolder="Scope")
        root = Path(vault["localPath"])
        try:
            (root / "Scope").symlink_to(Path(".git") / "refs" / "heads", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform/filesystem")
        heads = root / ".git" / "refs" / "heads"
        before = sorted(p.name for p in heads.iterdir())
        for folder in ("", "Sub"):
            status, body = await client.post("/api/note/new", {"folder": folder})
            assert status == 400, (folder, body)
            assert body["code"] == "folder_is_symlink"
        assert sorted(p.name for p in heads.iterdir()) == before, ".git must be untouched"


@pytest.mark.asyncio
async def test_new_note_in_a_vault_reached_through_a_symlink_is_allowed(
    fixtures, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link ABOVE the vault is the user's filesystem, not the vault's content:
    a crew home relocated to another disk (`~/.kiro/crew -> /Volumes/SSD/crew`)
    puts every cloned vault behind a symlink. The guard walks only the typed
    folder components under `content_root`, so a subfolder note is still created.
    """
    _mod, remote, _seed = fixtures
    (tmp_path / "home").mkdir(exist_ok=True)
    home_alias = tmp_path / "home-alias"
    try:
        home_alias.symlink_to(tmp_path / "home", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform/filesystem")
    # Clone THROUGH the alias: the vault's `localPath` now carries a symlinked
    # ancestor while every component under `content_root` is real.
    monkeypatch.setenv("MD_NOTEBOOK_HOME", str(home_alias))
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        assert str(home_alias) in vault["localPath"]
        status, body = await client.post("/api/note/new", {"folder": "sub/Deeper"})
        assert status == 200, body
        assert body["path"] == "sub/Deeper/Untitled.md"
        assert (Path(vault["localPath"]) / "sub" / "Deeper" / "Untitled.md").exists()
        # And a SAVE of a subfolder note must succeed too. The save path compares
        # the resolved directory against the staged temp's parent to catch an
        # in-vault link; both must be canonicalised so the legitimate ancestor
        # link above the vault (this alias) does not make them differ and 400
        # every subfolder save with a bogus `folder_is_symlink` (Opus finding).
        status, body = await client.put(
            "/api/note", {"path": "sub/Deeper/Untitled.md", "content": "# edited\n"}
        )
        assert status == 200, body
        assert (Path(vault["localPath"]) / "sub" / "Deeper" / "Untitled.md").read_text(
            encoding="utf-8"
        ) == "# edited\n"
        # A ROOT-LEVEL save (folder == "") must succeed too. It exercises the
        # empty-folder branch of `resolve_write_dir_checked`, which must return a
        # realpath just like the folder branch — otherwise the non-canonical
        # stored `localPath` (this alias) differs from the realpath'd temp parent
        # and every root save 400s `folder_is_symlink` (Opus finding).
        status, body = await client.put(
            "/api/note", {"path": "One.md", "content": "# root edited\n"}
        )
        assert status == 200, body
        assert (Path(vault["localPath"]) / "One.md").read_text(
            encoding="utf-8"
        ) == "# root edited\n"


@pytest.mark.asyncio
async def test_delete_moves_the_note_into_the_local_trash(fixtures) -> None:
    """Delete is recoverable: the file lands in .trash, not the void."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        before = (root / "One.md").read_text(encoding="utf-8")
        status, body = await client.delete("/api/note?path=One.md")
        assert status == 200, body
        assert body["trashed"] == ".trash/One.md"
        assert not (root / "One.md").exists()
        assert (root / ".trash" / "One.md").read_text(encoding="utf-8") == before
        # And it is gone from the listing — the walk prunes dotted directories.
        paths = [n["path"] for n in (await client.get("/api/notes"))[1]["notes"]]
        assert "One.md" not in paths
        assert not any(p.startswith(".trash") for p in paths)


@pytest.mark.asyncio
async def test_trash_does_not_overwrite_a_same_named_note(fixtures) -> None:
    """Two notes with one filename in different folders both survive deletion."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        (root / "sub" / "One.md").write_text("# nested one\n", encoding="utf-8")
        assert (await client.delete("/api/note?path=One.md"))[1]["trashed"] == ".trash/One.md"
        second = (await client.delete("/api/note?path=sub/One.md"))[1]["trashed"]
        assert second == ".trash/One 2.md"
        assert (root / ".trash" / "One 2.md").read_text(encoding="utf-8") == "# nested one\n"


@pytest.mark.asyncio
async def test_sync_refuses_rather_than_pushing_a_subset(fixtures, monkeypatch) -> None:
    """An explicit Sync must not commit only part of what the user asked for.

    `status()` reports a rename as TWO entries — old path deleted, new path added —
    and the argv cap slices a path-sorted list, so the cutoff can fall between them.
    Pushing only the deletion half makes the note look deleted to every other clone
    while the UI reports success. The autosave keeps the cap (it pushes nothing, and
    the next tick repairs a split), so it must still succeed here.
    """
    # The production limit is an argv-safety bound, not part of this test's
    # semantics.  Exercising all 500 paths makes a functional assertion depend
    # on filesystem throughput under Windows CI load; a small injected limit
    # still proves refusal and the complete two-batch autosave drain.
    monkeypatch.setattr(git_ops, "MAX_STAGED_PATHS", 3)
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        remote_before = _git("rev-parse", "HEAD", cwd=Path(remote))
        for n in range(git_ops.MAX_STAGED_PATHS + 1):
            (root / f"bulk-{n:04d}.md").write_text(f"# Bulk {n}\n", encoding="utf-8")

        status, body = await client.post("/api/sync")
        assert status >= 400, body
        assert str(git_ops.MAX_STAGED_PATHS) in body["error"]
        # Nothing was committed locally and nothing reached the remote.
        assert _git("rev-parse", "HEAD", cwd=Path(remote)) == remote_before

        # The autosave still drains them in batches — capped, never refused.
        commit_status, commit_body = await client.post("/api/commit")
        assert commit_status == 200, commit_body
        assert len(commit_body["result"]["committed"]) == git_ops.MAX_STAGED_PATHS
        second_status, second_body = await client.post("/api/commit")
        assert second_status == 200, second_body
        assert len(second_body["result"]["committed"]) == 1
        # Drained, so a Sync is possible again.
        assert (await client.post("/api/sync"))[0] == 200


@pytest.mark.asyncio
async def test_sync_never_commits_the_trash(fixtures) -> None:
    """A deleted note must not be pushed to the remote.

    `git add -A` stages from the working tree rather than from the status list,
    so the pathspec exclusion is what actually enforces this.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        assert (await client.delete("/api/note?path=One.md"))[0] == 200
        status, body = await client.post("/api/sync")
        assert status == 200, body
        committed = [c["path"] for c in body["result"]["committed"]]
        assert not any(".trash" in p for p in committed)
        assert "One.md" in committed  # the deletion itself IS committed
        # Nothing under .trash reached the index at any point.
        tracked = _git("ls-files", cwd=Path(_seed))
        assert ".trash" not in tracked


@pytest.mark.asyncio
async def test_delete_refreshes_backlinks(fixtures) -> None:
    """Deleting a linking note drops the backlink it contributed."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        # One.md links to [[Two]]; deleting it must leave Two with no backlinks.
        assert (await client.delete("/api/note?path=One.md"))[0] == 200
        _, target = await client.get("/api/note?path=sub/Two.md")
        assert target["backlinks"] == []


@pytest.mark.asyncio
async def test_sync_works_when_the_vault_already_ignores_the_trash(fixtures) -> None:
    """The Obsidian case: `.trash/` is in the vault's own .gitignore.

    An `:(exclude,literal)` pathspec would make `git add` treat `.trash` as an
    EXPLICITLY named ignored path and fail the whole add ("use -f if you really
    want to add them", exit 1) in precisely the vaults most likely to have a
    trash folder already. So staging names no pathspec for the trash: it lists
    only the paths `status()` reported, and `status()` filters it out.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        (root / ".gitignore").write_text(".trash/\n", encoding="utf-8")
        assert (await client.delete("/api/note?path=One.md"))[0] == 200
        # An edit alongside the delete, which is what the user was syncing.
        status, body = await client.put(
            "/api/note", {"path": "sub/Two.md", "content": "---\ntitle: Two\n---\n\nedited\n"}
        )
        assert status == 200, body
        status, body = await client.post("/api/sync")
        assert status == 200, body
        committed = [c["path"] for c in body["result"]["committed"]]
        assert "sub/Two.md" in committed
        assert not any(".trash" in p for p in committed)


@requires_symlinks
@pytest.mark.asyncio
async def test_delete_refuses_a_trash_symlink_escaping_the_vault(fixtures, tmp_path) -> None:
    """A vault-supplied `.trash` symlink must not redirect the move out of the vault."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        outside = tmp_path / "outside"
        outside.mkdir()
        # Directory redirect out of the vault: symlink on POSIX, junction on
        # non-admin Windows. The guard refuses either as a `.trash` reparse point.
        platform_compat.symlink_or_junction(str(outside), str(root / ".trash"))
        status, body = await client.delete("/api/note?path=One.md")
        assert status == 400, body
        assert "symlink" in body["error"] or "escapes vault" in body["error"]
        # The note stayed put and nothing was written outside the vault.
        assert (root / "One.md").exists()
        assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_duplicate_note_copies_content_beside_the_source(fixtures) -> None:
    """A copy lands in the source's folder, keeping its body."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, body = await client.post("/api/note/duplicate", {"path": "sub/Two.md"})
        assert status == 200, body
        assert body["path"] == "sub/Two copy.md"
        _, read = await client.get("/api/note?path=sub/Two copy.md")
        assert read["content"] == "---\ntitle: Two\n---\n\nbody #tag\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("refuse_read", [False, True], ids=["readable", "gate-refused"])
async def test_duplicate_alias_keeps_cross_folder_read_and_sensitive_gate(
    fixtures, monkeypatch: pytest.MonkeyPatch, refuse_read: bool
) -> None:
    """A note alias may point across folders, but cannot bypass the read gate."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        target = root / "One.md"
        target.write_bytes(b"first\r\nsecond\rthird\n")
        alias = root / "sub" / "Alias.md"
        try:
            alias.symlink_to(Path("..") / "One.md")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform/filesystem")
        real_read = _mod.hooks.safe_read_file_bytes

        def checked_read(path, *args, **kwargs):
            if refuse_read and Path(path).resolve() == target.resolve():
                return None
            return real_read(path, *args, **kwargs)

        monkeypatch.setattr(_mod.hooks, "safe_read_file_bytes", checked_read)
        status, body = await client.post("/api/note/duplicate", {"path": "sub/Alias.md"})
        copy = root / "sub" / "Alias copy.md"
        if refuse_read:
            assert status == 404, body
            assert body["code"] == "no_such_note"
            assert not copy.exists()
        else:
            assert status == 200, body
            assert body["path"] == "sub/Alias copy.md"
            assert copy.read_bytes() == b"first\nsecond\nthird\n"
        assert alias.is_symlink()
        assert target.read_bytes() == b"first\r\nsecond\rthird\n"


@pytest.mark.asyncio
async def test_duplicate_note_names_are_unique(fixtures) -> None:
    """Two quick duplications must not collide, or overwrite the first copy."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        first = (await client.post("/api/note/duplicate", {"path": "One.md"}))[1]["path"]
        second = (await client.post("/api/note/duplicate", {"path": "One.md"}))[1]["path"]
        assert first == "One copy.md"
        assert second == "One copy 2.md"


@pytest.mark.asyncio
async def test_duplicate_note_requires_an_existing_note(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, body = await client.post("/api/note/duplicate", {"path": "Nope.md"})
        assert status == 404, body
        assert body["code"] == "no_such_note"
        assert (await client.post("/api/note/duplicate", {}))[0] == 400


@pytest.mark.asyncio
async def test_duplicate_note_refuses_a_path_outside_the_vault(fixtures) -> None:
    """Containment is checked on the source, so the copy cannot import a file."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, _ = await client.post("/api/note/duplicate", {"path": "../outside.md"})
        assert status == 400


@pytest.mark.asyncio
async def test_duplicate_note_normalizes_a_windows_separator(fixtures) -> None:
    """A backslash path names the same note on every OS, so the copy lands beside it.

    ``safe_join`` normalizes the separator to validate containment, so the SOURCE
    resolves either way. Deriving the copy's name from the raw string did not:
    the backslash survived into the filename, so the copy landed at the vault
    root on POSIX and in a subdirectory on Windows.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        status, body = await client.post("/api/note/duplicate", {"path": "sub\\Two.md"})
        assert status == 200, body
        assert body["path"] == "sub/Two copy.md"
        assert (Path(vault["localPath"]) / "sub" / "Two copy.md").exists()


@pytest.mark.asyncio
async def test_duplicate_note_refreshes_backlinks(fixtures) -> None:
    """The copy inherits the source's wikilinks, so its targets gain a backlink.

    An incremental index add would keep search working while leaving every linked
    target's backlink list missing the copy until an unrelated cache rebuild.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        # One.md links to [[Two]]; the copy carries that link too.
        status, body = await client.post("/api/note/duplicate", {"path": "One.md"})
        assert status == 200, body
        _, target = await client.get("/api/note?path=sub/Two.md")
        sources = sorted(b["sourcePath"] for b in target["backlinks"])
        assert sources == ["One copy.md", "One.md"]


@pytest.mark.asyncio
async def test_move_note(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        status, body = await client.post("/api/note/move", {"from": "One.md", "to": "moved/One.md"})
        assert status == 200
        assert body["path"] == "moved/One.md"
        # The target folder is created as needed.
        assert (Path(vault["localPath"]) / "moved" / "One.md").exists()
        assert not (Path(vault["localPath"]) / "One.md").exists()


@pytest.mark.asyncio
async def test_move_refuses_to_overwrite(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, body = await client.post("/api/note/move", {"from": "One.md", "to": "sub/Two.md"})
        assert status == 409
        assert "already exists" in body["error"]


@pytest.mark.asyncio
async def test_search(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, body = await client.get("/api/search?q=body")
        assert status == 200
        assert [r["path"] for r in body["results"]] == ["sub/Two.md"]
        # An empty query returns nothing rather than everything.
        assert (await client.get("/api/search?q="))[1]["results"] == []


# --- inline #tag extraction ----------------------------------------------------


def test_extract_tags_accepts_a_non_ascii_leading_character() -> None:
    """A tag may start with any Unicode alphanumeric, as the tail already does.

    Latin-script, Han, Hangul, Cyrillic and Greek leading characters all
    register the same way. Tags feed an in-memory index, never a path, so
    the wide lead cannot create a filesystem collision.
    """
    from kiro_crew.apps.builtins.md_notebook import notes as notes_mod

    assert notes_mod.extract_tags("#论文") == ["论文"]
    assert notes_mod.extract_tags("#분기") == ["분기"]
    assert notes_mod.extract_tags("#Отчет") == ["Отчет"]
    assert notes_mod.extract_tags("#München") == ["München"]
    assert notes_mod.extract_tags("#a논문") == ["a논문"]
    assert notes_mod.extract_tags("see (#논문) note") == ["논문"]
    assert notes_mod.extract_tags("#todo #论文 #todo") == ["todo", "论文"]


def test_extract_tags_still_refuses_a_non_alphanumeric_lead() -> None:
    """Only the script restriction is lifted; the shape rules stand."""
    from kiro_crew.apps.builtins.md_notebook import notes as notes_mod

    assert notes_mod.extract_tags("#_foo") == []
    assert notes_mod.extract_tags("#-x") == []
    assert notes_mod.extract_tags("# tag") == []
    assert notes_mod.extract_tags("#\u200bfoo") == []
    assert notes_mod.extract_tags("#2024") == ["2024"]
    assert notes_mod.extract_tags("#todo") == ["todo"]


def test_extract_tags_ignores_masked_code_in_any_script() -> None:
    """Code masking still wins over the wider lead: a tag inside fenced or
    inline code is a sample, not an index entry."""
    from kiro_crew.apps.builtins.md_notebook import notes as notes_mod

    assert notes_mod.extract_tags("```\n#논문\n```") == []
    assert notes_mod.extract_tags("`#논문`") == []
    assert notes_mod.extract_tags("real #논문 and `#가짜`") == ["논문"]


@pytest.mark.parametrize(
    "word",
    [
        "\u0939\u093f\u0928\u094d\u0926\u0940",  # Hindi: vowel signs and a virama
        "\u09b0\u09bf\u09aa\u09cb\u09b0\u09cd\u099f",  # Bengali
        "\u0ba4\u0bae\u0bbf\u0bb4\u0bcd",  # Tamil
        "\u0e20\u0e32\u0e29\u0e32\u0e44\u0e17\u0e22\u0e14\u0e35",  # Thai, with a vowel mark
        "\u05e9\u05c1\u05b8\u05dc\u05d5\u05b9\u05dd",  # pointed Hebrew
        "\u0645\u064f\u062f\u064e\u0631\u0651\u0650\u0633",  # Arabic with harakat
    ],
)
def test_extract_tags_keeps_a_word_whose_script_uses_combining_marks(word: str) -> None:
    """Python's ``\\w`` has no combining marks, so a tag scanner built on it alone
    would register these as their first letter -- ``#हिन्दी`` as ``ह`` -- a
    different tag from the one typed. The whole word is the tag."""
    from kiro_crew.apps.builtins.md_notebook import notes as notes_mod

    # NFC can reorder a run of marks (canonical ordering), so the tag is the word's NFC form.
    tag = unicodedata.normalize("NFC", word)
    assert notes_mod.extract_tags(f"see #{word} here") == [tag]
    assert notes_mod.extract_tags(f"#{word}/sub-topic") == [f"{tag}/sub-topic"]


def test_extract_tags_is_nfc_so_one_tag_has_one_spelling() -> None:
    """A decomposed accent (``e`` + U+0301) is the same tag as the composed one,
    not a visually identical second tag -- and not the truncated ``cafe``."""
    from kiro_crew.apps.builtins.md_notebook import notes as notes_mod

    assert notes_mod.extract_tags("#cafe\u0301 and #caf\u00e9") == ["caf\u00e9"]
    meta = notes_mod.parse_note("n.md", "---\ntags: [cafe\u0301]\n---\nbody #caf\u00e9\n")
    assert meta["tags"] == ["caf\u00e9"]


def test_extract_tags_still_ends_at_a_format_character() -> None:
    """Only combining marks were added: a zero-width space or a bidi override is
    not part of a word, so it still ends the tag rather than hiding inside it."""
    from kiro_crew.apps.builtins.md_notebook import notes as notes_mod

    assert notes_mod.extract_tags("#ab\u200bcd") == ["ab"]
    assert notes_mod.extract_tags("#ab\u202ecd") == ["ab"]
    assert notes_mod.extract_tags("#todo.") == ["todo"]
    assert notes_mod.extract_tags("#a,b") == ["a"]
