"""Tests for kiro_crew.apps.scaffold — app scaffolding."""
from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest

from conftest import make_dir_link, requires_symlinks
from kiro_crew import platform_compat
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.scaffold import (
    _placeholder_icon_png,
    _write_sites,
    scaffold_app,
)


class TestPlaceholderIcon:
    """The scaffolded store icon, pinned against the publishing guide's spec.

    A scaffolded app that reaches the App Store catalog with no icon publishes a
    generated placeholder card, indistinguishable from an icon the publish
    pipeline dropped -- so it reads as a store bug rather than an incomplete
    manifest. These pin the shape the guide actually requires, so a change here
    cannot silently produce an icon the store rejects.
    """

    def test_is_a_png(self):
        assert _placeholder_icon_png()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_is_the_square_512_the_guide_asks_for(self):
        width, height = struct.unpack(">II", _placeholder_icon_png()[16:24])
        assert width == height == 512

    def test_carries_no_alpha_channel(self):
        """Colour type 2 is truecolor RGB. The guide requires an opaque icon, so
        an alpha channel would model a freedom the icon cannot use."""
        assert _placeholder_icon_png()[25] == 2

    def test_stays_small(self):
        """Two flat colours should compress to nothing; a regression that
        inflates this would otherwise be silent."""
        assert len(_placeholder_icon_png()) < 8 * 1024

    def test_is_byte_identical_across_calls(self):
        """One known digest stays recognisable as 'still the placeholder'."""
        assert _placeholder_icon_png() == _placeholder_icon_png()

    def test_pixels_decode_to_the_intended_plate(self):
        """Cheap proof the scanline filter byte and row stride are right: a wrong
        stride still produces a file every header check above accepts."""
        data = _placeholder_icon_png()
        raw = zlib.decompress(data[data.index(b"IDAT") + 4 : -12])
        stride = 1 + 512 * 3
        assert len(raw) == 512 * stride
        middle = raw[256 * stride : 257 * stride]
        assert middle[0] == 0, "scanline filter type must be None"
        assert tuple(middle[1:4]) == (46, 52, 64), "row starts in the field"
        centre = 1 + 256 * 3
        assert tuple(middle[centre : centre + 3]) == (67, 76, 94), "plate inside"


# Derived from scaffold.py, not restated here: the module owns the list its own
# up-front validation walks, so a newly added write site is picked up by the
# containment cases below without anyone remembering to add it twice.
_SCAFFOLD_DIRS, _SCAFFOLD_FILES = _write_sites(
    include_backend=True, include_ui=True
)


def _scaffold_all(output_dir, name):
    return scaffold_app(
        output_dir, name,
        include_backend=True, include_ui=True, include_cron=True,
    )


class TestWriteContainment:
    """Every write site refuses a path that resolves outside the app dir.

    Path.exists() is False for a dangling symlink, so an existence test on the
    joined path falls through to a write that follows the link and lands
    outside the app directory; a symlinked parent directory escapes the same
    way. Both shapes are exercised per write site. A refusal aborts the
    scaffold (never a silent skip: a skipped write would leave a partial app
    while the CLI reports success), and nothing lands outside the app dir.
    """

    def test_site_list_matches_what_scaffold_creates(self, tmp_path):
        app_dir = _scaffold_all(tmp_path, "probe")
        # Compared as Path objects built from the same components the module
        # ships, so there is no separator to agree on and no posix/Windows
        # string form to normalize.
        files = {p.relative_to(app_dir) for p in app_dir.rglob("*") if p.is_file()}
        dirs = {p.relative_to(app_dir) for p in app_dir.rglob("*") if p.is_dir()}
        assert files == {Path(*parts) for parts in _SCAFFOLD_FILES}
        assert dirs == {Path(*parts) for parts in _SCAFFOLD_DIRS}

    @requires_symlinks
    @pytest.mark.parametrize("relpath", _SCAFFOLD_FILES)
    def test_write_refuses_to_follow_an_escaping_symlink(self, tmp_path, relpath):
        # requires_symlinks: a DANGLING file symlink has no junction equivalent
        # (junctions target existing directories), so unelevated Windows skips.
        outside = tmp_path / "outside-target"
        out = tmp_path / "out"
        link = out.joinpath("victim", *relpath)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)

        with pytest.raises(ValueError):
            _scaffold_all(out, "victim")

        assert not outside.exists(), f"{relpath} wrote through a dangling symlink"
        self._assert_nothing_was_written(out / "victim", relpath)

    @staticmethod
    def _assert_nothing_was_written(app_dir, site):
        """A refusal must abort BEFORE the first write, not part-way through.

        app.json is written first, so a run that refuses at a later site would
        already have overwritten the manifest of an existing app -- the refusal
        would cost the developer that file. Asserting the manifest is absent is
        what pins the ordering: it can only hold if validation precedes writing.
        """
        for name in ("app.json", "README.md"):
            assert not (app_dir / name).exists(), (
                f"refusing {site} left {name} behind -- validation ran after a write"
            )

    @pytest.mark.parametrize("reldir", _SCAFFOLD_DIRS)
    def test_mkdir_refuses_an_escaping_dir_symlink(self, tmp_path, reldir):
        outside = tmp_path / "outside-dir"
        outside.mkdir()
        out = tmp_path / "out"
        link = out.joinpath("victim", *reldir)
        link.parent.mkdir(parents=True, exist_ok=True)
        # make_dir_link: a junction on Windows needs no privilege and resolves
        # through the same reparse machinery, so this stays exercised there.
        make_dir_link(link, outside)

        with pytest.raises(ValueError):
            _scaffold_all(out, "victim")

        assert list(outside.iterdir()) == [], (
            f"{reldir} let writes land outside the app dir"
        )
        self._assert_nothing_was_written(out / "victim", reldir)

    @pytest.mark.parametrize("reldir", _SCAFFOLD_DIRS)
    def test_a_dir_site_occupied_by_a_regular_file_is_refused(self, tmp_path, reldir):
        """Contained but the wrong KIND -- the half containment cannot see.

        A plain file at a directory site resolves squarely inside the app dir, so
        every containment check passes, and then `mkdir(exist_ok=True)` raises
        `FileExistsError` (exist_ok forgives an existing DIRECTORY, not a file).
        That is not a ValueError, so it escapes the CLI's error contract as a raw
        traceback -- on top of the manifest the run had already overwritten.
        """
        out = tmp_path / "out"
        app_dir = out / "victim"
        squatter = app_dir.joinpath(*reldir)
        squatter.parent.mkdir(parents=True, exist_ok=True)
        squatter.write_text("not a directory", encoding="utf-8")

        original = '{"name": "victim", "version": "9.9.9"}\n'
        (app_dir / "app.json").write_text(original, encoding="utf-8")

        with pytest.raises(ValueError, match="not a directory"):
            _scaffold_all(out, "victim")

        assert (app_dir / "app.json").read_text(encoding="utf-8") == original
        assert squatter.read_text(encoding="utf-8") == "not a directory"

    @pytest.mark.parametrize("relpath", _SCAFFOLD_FILES)
    def test_a_file_site_occupied_by_a_directory_is_refused(self, tmp_path, relpath):
        """The mirror case: `write_text` raises IsADirectoryError, also not a
        ValueError, so it escapes the same way with the same lost manifest."""
        out = tmp_path / "out"
        app_dir = out / "victim"
        squatter = app_dir.joinpath(*relpath)
        squatter.mkdir(parents=True, exist_ok=True)

        # app.json is itself a site here, so only seed the manifest when the
        # squatter is not standing on it.
        manifest = app_dir / "app.json"
        original = '{"name": "victim", "version": "9.9.9"}\n'
        seeded = squatter != manifest
        if seeded:
            manifest.write_text(original, encoding="utf-8")

        with pytest.raises(ValueError, match="is a directory"):
            _scaffold_all(out, "victim")

        if seeded:
            assert manifest.read_text(encoding="utf-8") == original
        assert squatter.is_dir(), "the refused run replaced the occupied site"

    @pytest.mark.parametrize("relpath", _SCAFFOLD_FILES)
    def test_a_read_only_file_site_is_refused(self, tmp_path, relpath):
        """A read-only existing file passes the type check (it is not a
        directory) but its own write_text would raise PermissionError -- not a
        ValueError -- after app.json was already overwritten. The up-front pass
        must refuse it while the app is still exactly as it was found."""
        out = tmp_path / "out"
        app_dir = out / "victim"
        squatter = app_dir.joinpath(*relpath)
        squatter.parent.mkdir(parents=True, exist_ok=True)
        squatter.write_text("locked", encoding="utf-8")
        squatter.chmod(0o444)

        # app.json is itself a site here, so only seed a separate manifest when
        # the read-only squatter is not standing on app.json.
        manifest = app_dir / "app.json"
        original = '{"name": "victim", "version": "9.9.9"}\n'
        seeded = squatter != manifest
        if seeded:
            manifest.write_text(original, encoding="utf-8")

        try:
            with pytest.raises(ValueError, match="not writable"):
                _scaffold_all(out, "victim")

            if seeded:
                assert manifest.read_text(encoding="utf-8") == original, (
                    "the refused run overwrote the existing manifest"
                )
            assert squatter.read_text(encoding="utf-8") == "locked", (
                "the refused run overwrote the read-only site"
            )
        finally:
            squatter.chmod(0o644)

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="chmod(0o555) on a directory does not remove write access on Windows; the read-only-ancestor refusal is POSIX-observable only",
    )
    def test_a_read_only_parent_of_an_absent_site_is_refused(self, tmp_path):
        """A read-only existing directory with the file site still ABSENT slips
        past the file-writability check (there is no file to test yet), then the
        eventual write_bytes/mkdir into that directory raises PermissionError --
        not a ValueError -- after app.json was already overwritten. GPT's case:
        a read-only `assets/` and no `icon.png`. The up-front pass must prove the
        nearest existing ancestor of every site is writable and refuse first."""
        out = tmp_path / "out"
        app_dir = out / "victim"
        # Read-only assets/ directory, icon.png absent -- the write site is the
        # icon, whose nearest existing ancestor is the unwritable assets dir.
        assets = app_dir / "assets"
        assets.mkdir(parents=True, exist_ok=True)

        manifest = app_dir / "app.json"
        original = '{"name": "victim", "version": "9.9.9"}\n'
        manifest.write_text(original, encoding="utf-8")

        assets.chmod(0o555)
        try:
            with pytest.raises(ValueError, match="not writable"):
                _scaffold_all(out, "victim")

            assert manifest.read_text(encoding="utf-8") == original, (
                "the refused run overwrote the existing manifest"
            )
            assert list(assets.iterdir()) == [], (
                "the refused run wrote into the read-only directory"
            )
        finally:
            assets.chmod(0o755)

    def test_a_refusal_leaves_an_existing_apps_manifest_untouched(self, tmp_path):
        """Re-running `app init` over an existing app must not cost it app.json.

        `app_dir.mkdir(exist_ok=True)` does not refuse an app that already exists,
        so this path is reachable in normal use: a developer re-runs `app init` in
        a tree where `assets` is a symlink pointing out of the app. Validating at
        the write sites alone overwrote the manifest first and refused second,
        destroying data the run had no business touching.
        """
        outside = tmp_path / "outside-dir"
        outside.mkdir()
        out = tmp_path / "out"
        app_dir = out / "victim"
        app_dir.mkdir(parents=True)

        original = '{"name": "victim", "version": "9.9.9"}\n'
        (app_dir / "app.json").write_text(original, encoding="utf-8")
        make_dir_link(app_dir / "assets", outside)

        with pytest.raises(ValueError):
            _scaffold_all(out, "victim")

        assert (app_dir / "app.json").read_text(encoding="utf-8") == original, (
            "the refused run overwrote the existing manifest"
        )

    def test_a_runtime_write_failure_leaves_an_existing_manifest_intact(
        self, tmp_path, monkeypatch
    ):
        """The up-front pass proves every path is writable, but it cannot prove
        the write itself will not fail at runtime -- a full disk, an exhausted
        inode table, EIO. app.json is written LAST so that such a failure aborts
        before the destructive overwrite, leaving the existing manifest intact.

        Simulated by making the icon write (an early, pre-manifest site) raise
        ENOSPC the way a full disk would; the pre-seeded app.json must survive.
        """
        import errno

        out = tmp_path / "out"
        app_dir = out / "victim"
        app_dir.mkdir(parents=True)
        original = '{"name": "victim", "version": "9.9.9"}\n'
        (app_dir / "app.json").write_text(original, encoding="utf-8")

        def _boom():
            raise OSError(errno.ENOSPC, "No space left on device")

        # The icon body is produced right before it is written, early in the
        # write sequence and well before app.json. Failing here stands in for any
        # runtime write failure past what validation can foresee.
        monkeypatch.setattr(
            "kiro_crew.apps.scaffold._placeholder_icon_png", _boom
        )

        with pytest.raises(OSError):
            _scaffold_all(out, "victim")

        assert (app_dir / "app.json").read_text(encoding="utf-8") == original, (
            "a runtime write failure overwrote the existing manifest -- app.json "
            "must be written last so the destructive write never precedes a "
            "failure"
        )

    def test_a_failed_icon_write_leaves_no_partial_placeholder(
        self, tmp_path, monkeypatch
    ):
        """The icon is written only when absent, so a truncated icon.png left by
        a write that failed partway would be mistaken for the developer's own
        artwork on every later run and never repaired -- a manifest pointing at a
        corrupt PNG. The write goes through atomic_write (temp file + rename,
        temp cleaned up on failure), so a failure leaves NO icon.png rather than
        a half-written one. Simulated by making atomic_write raise ENOSPC the way
        a full disk would mid-write; no icon.png must remain.
        """
        import errno

        out = tmp_path / "out"

        def _boom(*args, **kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr("kiro_crew.apps.scaffold.atomic_write", _boom)

        with pytest.raises(OSError):
            _scaffold_all(out, "myapp")

        icon = out / "myapp" / "assets" / "icon.png"
        assert not icon.exists(), (
            "a failed icon write left a partial icon.png behind; the only-when-"
            "absent guard would treat it as real artwork and never repair it"
        )

    def test_a_failed_final_manifest_write_leaves_the_old_manifest_intact(
        self, tmp_path, monkeypatch
    ):
        """app.json is written last AND atomically. Last so an earlier failure
        never reaches it; atomically because the write itself is destructive --
        write_text would truncate the existing app.json in place, so a full disk
        DURING the final write would corrupt the very manifest the ordering
        protects. atomic_write renames a complete temp into place, so app.json is
        always either the old manifest or the new one, never a truncated hybrid.

        Simulated by failing atomic_write only for app.json (the way a disk that
        fills exactly at the final write would); the pre-seeded manifest must be
        byte-for-byte intact.
        """
        import errno

        import kiro_crew.apps.scaffold as scaffold_mod

        out = tmp_path / "out"
        app_dir = out / "victim"
        app_dir.mkdir(parents=True)
        original = '{"name": "victim", "version": "9.9.9"}\n'
        (app_dir / "app.json").write_text(original, encoding="utf-8")

        real = scaffold_mod.atomic_write

        def _fail_only_manifest(path, content, *args, **kwargs):
            if Path(path).name == "app.json":
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, content, *args, **kwargs)

        monkeypatch.setattr(scaffold_mod, "atomic_write", _fail_only_manifest)

        with pytest.raises(OSError):
            _scaffold_all(out, "victim")

        assert (app_dir / "app.json").read_text(encoding="utf-8") == original, (
            "a disk-full during the final manifest write truncated the existing "
            "app.json; the write must be atomic so the old manifest survives"
        )

    def test_app_dir_symlink_escaping_output_dir_is_refused(self, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        out = tmp_path / "out"
        out.mkdir()
        make_dir_link(out / "victim", outside)

        with pytest.raises(ValueError):
            _scaffold_all(out, "victim")

        assert list(outside.iterdir()) == []

    def test_name_with_traversal_is_refused(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()

        with pytest.raises(ValueError):
            scaffold_app(out, "../evil")

        assert not (tmp_path / "evil").exists()

    def test_absolute_name_is_refused(self, tmp_path):
        """joinpath discards the root for an absolute component, so an absolute
        name would compare equal trivially while writing outside --dir."""
        out = tmp_path / "out"
        out.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        with pytest.raises(ValueError):
            scaffold_app(out, str(elsewhere / "evil"))

        assert not (elsewhere / "evil").exists()

    def test_app_dir_alias_of_a_sibling_project_is_refused(self, tmp_path):
        """An IN-ROOT alias passes plain containment: out/victim -> out/existing
        resolves inside the output dir, and scaffolding "victim" would truncate
        the sibling project's files. Exact-path equality refuses it."""
        out = tmp_path / "out"
        out.mkdir()
        existing = out / "existing"
        existing.mkdir()
        (existing / "app.json").write_text('{"name": "existing"}', encoding="utf-8")
        make_dir_link(out / "victim", existing)

        with pytest.raises(ValueError):
            _scaffold_all(out, "victim")

        assert (existing / "app.json").read_text(encoding="utf-8") == (
            '{"name": "existing"}'
        )

    def test_in_app_alias_of_a_sibling_dir_is_refused(self, tmp_path):
        """Same alias shape one level down: agents -> ./real inside the app dir
        resolves inside the root but is not the lexical path; refused."""
        out = tmp_path / "out"
        app_dir = out / "victim"
        (app_dir / "real").mkdir(parents=True)
        make_dir_link(app_dir / "agents", app_dir / "real")

        with pytest.raises(ValueError):
            _scaffold_all(out, "victim")

        assert list((app_dir / "real").iterdir()) == []

    @requires_symlinks
    def test_symlink_loop_raises_a_clear_error(self, tmp_path):
        """A self-referential symlink makes resolve() raise; the scaffold must
        abort with the containment ValueError, not an unexplained OSError.
        requires_symlinks: a junction cannot point at itself pre-creation, so
        the loop shape has no junction equivalent on unelevated Windows."""
        out = tmp_path / "out"
        app_dir = out / "victim"
        app_dir.mkdir(parents=True)
        (app_dir / "agents").symlink_to(app_dir / "agents")

        with pytest.raises(ValueError):
            _scaffold_all(out, "victim")

        assert not (app_dir / "agents" / "sample-agent.json").exists()

    def test_scaffold_through_a_symlinked_output_dir_succeeds(self, tmp_path):
        """A symlink in the user's own --dir is not an escape: both sides are
        resolved, so a symlinked home or /tmp on macOS compares equal."""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        make_dir_link(link, real)

        app_dir = _scaffold_all(link, "my-app")

        assert (real / "my-app" / "app.json").is_file()
        assert (app_dir / "README.md").is_file()


class TestScaffold:
    def test_basic_scaffold(self, tmp_path):
        app_dir = scaffold_app(tmp_path, "my-test-app")
        assert app_dir.is_dir()
        assert (app_dir / "app.json").is_file()
        assert (app_dir / "agents" / "sample-agent.json").is_file()
        assert (app_dir / "skills" / "sample-skill" / "SKILL.md").is_file()
        assert (app_dir / "README.md").is_file()

        # Manifest should be valid
        m = AppManifest.from_json_file(app_dir / "app.json")
        assert m.name == "my-test-app"
        assert m.validate() == []

    def test_store_icon_exists_and_is_declared(self, tmp_path):
        """Both halves together. The bytes without the manifest key are an unused
        file; the key without the bytes is a broken reference, which publishes
        worse than declaring nothing at all."""
        app_dir = scaffold_app(tmp_path, "icon-app")
        icon = app_dir / "assets" / "icon.png"
        assert icon.is_file()
        assert icon.read_bytes() == _placeholder_icon_png()
        manifest = json.loads((app_dir / "app.json").read_text(encoding="utf-8"))
        assert manifest["iconPath"] == "assets/icon.png"

    def test_icon_path_resolves_from_the_app_root(self, tmp_path):
        """`iconPath` is repo-relative, so it must resolve against the app
        directory exactly as written -- no leading slash, no `ui/` prefix."""
        app_dir = scaffold_app(tmp_path, "resolve-app")
        declared = json.loads((app_dir / "app.json").read_text(encoding="utf-8"))
        assert (app_dir / declared["iconPath"]).is_file()

    def test_rerun_does_not_destroy_a_replaced_icon(self, tmp_path):
        """Every other scaffolded file is GENERATED and reproduced from the same
        arguments, so overwriting costs nothing. This one becomes the developer's
        artwork the moment they replace it -- which is the point of scaffolding it
        -- so a second `app init` must not overwrite their icon."""
        app_dir = scaffold_app(tmp_path, "rerun-app")
        icon = app_dir / "assets" / "icon.png"
        icon.write_bytes(b"\x89PNG\r\n\x1a\nnot-the-placeholder")

        scaffold_app(tmp_path, "rerun-app")
        assert icon.read_bytes() == b"\x89PNG\r\n\x1a\nnot-the-placeholder"

    def test_icon_ships_without_the_optional_ui(self, tmp_path):
        """The store icon is about being LISTED, not about having a UI, so it
        must not ride along on `include_ui`."""
        app_dir = scaffold_app(tmp_path, "headless-app")
        assert not (app_dir / "ui").exists()
        assert (app_dir / "assets" / "icon.png").is_file()

    def test_readme_points_at_the_placeholder(self, tmp_path):
        """The generated tree is where a developer learns the file is theirs to
        replace; a placeholder nobody knows to replace ships as the real icon."""
        readme = (scaffold_app(tmp_path, "tree-app") / "README.md").read_text(
            encoding="utf-8"
        )
        assert "assets/" in readme
        assert "replace this placeholder" in readme

    def test_scaffold_with_backend(self, tmp_path):
        app_dir = scaffold_app(tmp_path, "backend-app", include_backend=True)
        assert (app_dir / "backend" / "server.py").is_file()
        m = AppManifest.from_json_file(app_dir / "app.json")
        assert m.backend.entryPoint == "backend/server.py"

    def test_scaffold_without_backend(self, tmp_path):
        app_dir = scaffold_app(tmp_path, "no-backend")
        assert not (app_dir / "backend").exists()

    def test_custom_metadata(self, tmp_path):
        app_dir = scaffold_app(
            tmp_path, "custom-app",
            display_name="Custom App",
            description="A custom description",
            author="testuser",
        )
        m = AppManifest.from_json_file(app_dir / "app.json")
        assert m.displayName == "Custom App"
        assert m.description == "A custom description"
        assert m.author == "testuser"

    def test_agent_is_valid_json(self, tmp_path):
        app_dir = scaffold_app(tmp_path, "json-check")
        agent = json.loads((app_dir / "agents" / "sample-agent.json").read_text(encoding="utf-8"))
        assert agent["name"] == "sample-agent"
        assert "model" in agent

    def test_skill_has_frontmatter(self, tmp_path):
        app_dir = scaffold_app(tmp_path, "skill-check")
        content = (app_dir / "skills" / "sample-skill" / "SKILL.md").read_text(encoding="utf-8")
        assert "---" in content
        assert "description:" in content

    def test_readme_has_name(self, tmp_path):
        app_dir = scaffold_app(tmp_path, "readme-check")
        readme = (app_dir / "README.md").read_text(encoding="utf-8")
        assert "readme-check" in readme
        assert "kirocrew app install" in readme

    def test_scaffold_installable(self, tmp_path, monkeypatch):
        """Scaffolded app can be installed by the app manager."""
        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        app_dir = scaffold_app(tmp_path / "output", "installable-app")
        from kiro_crew.apps.manager import install_app
        result = install_app(app_dir)
        assert result.ok, result.error

    def test_scaffold_cli_integration(self, tmp_path, monkeypatch, capsys):
        """Test the CLI init command via _handle_app."""
        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        import argparse

        from kiro_crew.cli_commands import _handle_app
        ns = argparse.Namespace(app_action="init", name="cli-scaffolded", dir=str(tmp_path), backend=False)
        _handle_app(ns)
        captured = capsys.readouterr()
        assert "Scaffolded" in captured.out
        assert (tmp_path / "cli-scaffolded" / "app.json").is_file()

    def test_scaffold_cli_refusal_prints_clean_error(self, tmp_path, monkeypatch, capsys):
        """A containment refusal exits 1 with the app actions' clean error
        contract on stderr, not a raw ValueError traceback."""
        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        import argparse

        from kiro_crew.cli_commands import _handle_app
        out = tmp_path / "out"
        out.mkdir()
        ns = argparse.Namespace(app_action="init", name="../evil", dir=str(out), backend=False)
        with pytest.raises(SystemExit) as excinfo:
            _handle_app(ns)
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        # Pin the contract (clean error prefix on stderr), not the specific
        # refusal wording, which differs by escape shape.
        assert captured.err.startswith("\u274c ")
        assert not (tmp_path / "evil").exists()

    def test_scaffold_with_ui(self, tmp_path):
        """--ui generates ui/ directory with package.json, vite config, and App.tsx."""
        app_dir = scaffold_app(tmp_path, "ui-app", include_ui=True)
        assert (app_dir / "ui" / "package.json").is_file()
        assert (app_dir / "ui" / "vite.config.ts").is_file()
        assert (app_dir / "ui" / "src" / "App.tsx").is_file()
        assert (app_dir / "ui" / ".gitignore").is_file()

        # package.json should reference the app name
        pkg = json.loads((app_dir / "ui" / "package.json").read_text(encoding="utf-8"))
        assert pkg["name"] == "ui-app-ui"
        assert "react" in pkg["dependencies"]
        assert "vite" in pkg["devDependencies"]

        # vite config should externalize shared modules
        vite_cfg = (app_dir / "ui" / "vite.config.ts").read_text(encoding="utf-8")
        assert "@kirocrew/app-sdk" in vite_cfg
        assert "@kirocrew/app-sdk/ui" in vite_cfg

        # App.tsx should have a valid component
        app_tsx = (app_dir / "ui" / "src" / "App.tsx").read_text(encoding="utf-8")
        assert "useAppApi" in app_tsx
        assert "PageHeader" in app_tsx

    def test_scaffold_with_ui_manifest_valid(self, tmp_path):
        """--ui scaffold produces a valid manifest with ui fields."""
        app_dir = scaffold_app(tmp_path, "ui-valid", include_ui=True)
        m = AppManifest.from_json_file(app_dir / "app.json")
        assert m.validate() == []
        assert m.ui.entry == "dist/index.mjs"
        assert len(m.ui.pages) == 1
        assert m.ui.pages[0].route == "/apps/ui-valid"

    def test_scaffold_without_ui(self, tmp_path):
        """Without --ui, no ui/ directory is created."""
        app_dir = scaffold_app(tmp_path, "no-ui")
        assert not (app_dir / "ui").exists()

    def test_scaffold_with_cron(self, tmp_path):
        """--cron generates a sample cron entry in app.json."""
        app_dir = scaffold_app(tmp_path, "cron-app", include_cron=True)
        m = AppManifest.from_json_file(app_dir / "app.json")
        assert m.validate() == []
        assert len(m.crons) == 1
        assert m.crons[0].name == "cron-app-check"
        assert m.crons[0].every == 300

    def test_scaffold_without_cron(self, tmp_path):
        """Without --cron, no crons in manifest."""
        app_dir = scaffold_app(tmp_path, "no-cron")
        m = AppManifest.from_json_file(app_dir / "app.json")
        assert len(m.crons) == 0

    def test_scaffold_all_options(self, tmp_path):
        """All flags together produce a valid manifest."""
        app_dir = scaffold_app(
            tmp_path, "full-app",
            include_backend=True, include_ui=True, include_cron=True,
        )
        m = AppManifest.from_json_file(app_dir / "app.json")
        assert m.validate() == []
        assert m.backend.entryPoint == "backend/server.py"
        assert m.ui.entry == "dist/index.mjs"
        assert len(m.crons) == 1
        assert (app_dir / "backend" / "server.py").is_file()
        assert (app_dir / "ui" / "src" / "App.tsx").is_file()
