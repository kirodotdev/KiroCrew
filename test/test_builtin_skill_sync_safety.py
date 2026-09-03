"""Builtin-skill sync safety invariants (issue #3433).

``_ensure_builtin_skills`` may only destroy a destination directory it can
PROVE it installed and that has not changed since: a full-tree fingerprint
recorded in a ``.builtin-skill-provenance`` dotfile, verified after the
directory is atomically claimed. Everything else is user data: on update it
moves aside to a non-clobbering dot-prefixed ``.<name>.user-backup`` quarantine
(hidden from discovery, contents untouched), and the
stale-cleanup pass leaves it alone entirely.

The invariants under test: a name collision with a builtin never deletes a
user-authored tree, an in-place edit or user-added file marks a destination
diverged, a user skill named after a stale-cleanup entry survives startup,
legitimate updates of untouched builtins still happen, quarantines never
clobber each other, and verification can never hang or read unbounded bytes
on the startup path.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import skills as skills_mod
from kiro_crew.skills import (
    _PROVENANCE_MARKER,
    _ensure_builtin_skills,
    _record_builtin_provenance,
    _skill_tree_fingerprint,
)


def _make_skill(root: Path, name: str, body: str, extra: dict[str, str] | None = None) -> Path:
    """Create a skill directory ``root/name`` with a SKILL.md and extra files."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {body}\n---\n{body}\n", encoding="utf-8"
    )
    for rel, content in (extra or {}).items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return skill_dir


def _bump_mtime(path: Path, seconds: float = 60.0) -> None:
    """Make *path* strictly newer than any file written so far."""
    future = time.time() + seconds
    os.utime(path, (future, future))


def _backup_skill_md(backup: Path) -> Path:
    """The (untouched) SKILL.md inside a dot-prefixed quarantine directory."""
    return backup / "SKILL.md"


@pytest.fixture()
def builtin_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated fake packaged-builtin source root wired into the module."""
    root = tmp_path / "packaged-builtins"
    root.mkdir()
    monkeypatch.setattr(skills_mod, "_BUILTIN_SKILLS_DIR", root)
    return root


@pytest.fixture()
def base(tmp_path: Path) -> Path:
    """The user's installed-skills base the sync writes into."""
    dest = tmp_path / "skills"
    dest.mkdir()
    return dest


class TestNameCollisionPreservation:
    """A destination the sync cannot prove it owns is preserved, never deleted."""

    def test_user_skill_colliding_with_new_builtin_survives(
        self, builtin_root: Path, base: Path
    ) -> None:
        # A user-authored `deploy` collides with a bundled skill of the same
        # name whose packaged SKILL.md is newer: the packaged version must
        # install, and the user's whole tree must survive in quarantine.
        _make_skill(base, "deploy", "USER original", {"scripts/run.sh": "echo user"})
        src = _make_skill(builtin_root, "deploy", "packaged builtin")
        # Filesystem mtime granularity can tie the two writes; make the
        # packaged copy deterministically newer so the update gate fires.
        _bump_mtime(src / "SKILL.md")

        _ensure_builtin_skills(base)

        installed = base / "deploy" / "SKILL.md"
        assert "packaged builtin" in installed.read_text(encoding="utf-8")
        assert (base / "deploy" / _PROVENANCE_MARKER).is_file()
        backup = base / ".deploy.user-backup"
        assert "USER original" in _backup_skill_md(backup).read_text(encoding="utf-8")
        assert (backup / "scripts" / "run.sh").read_text(encoding="utf-8") == "echo user"

    def test_in_place_edited_builtin_not_destroyed_on_update(
        self, builtin_root: Path, base: Path
    ) -> None:
        src = _make_skill(builtin_root, "helper", "v1")
        _ensure_builtin_skills(base)  # clean install, provenance recorded

        # A user edit to the installed copy makes it user data; the packaged
        # v2 must still install, with the edit preserved in quarantine.
        dest_md = base / "helper" / "SKILL.md"
        dest_md.write_text(dest_md.read_text(encoding="utf-8") + "\nMY EDIT\n", encoding="utf-8")
        (src / "SKILL.md").write_text("---\nname: helper\n---\nv2\n", encoding="utf-8")
        _bump_mtime(src / "SKILL.md")

        _ensure_builtin_skills(base)

        assert "v2" in dest_md.read_text(encoding="utf-8")
        preserved = _backup_skill_md(base / ".helper.user-backup")
        assert "MY EDIT" in preserved.read_text(encoding="utf-8")

    def test_extra_auxiliary_file_marks_dest_diverged(
        self, builtin_root: Path, base: Path
    ) -> None:
        # SKILL.md is byte-identical; the ONLY divergence is a user-added
        # note. A SKILL.md-only comparison calls this unchanged and deletes
        # the note with the rest of the tree; the full-tree fingerprint must
        # treat it as diverged.
        src = _make_skill(builtin_root, "notes", "v1")
        _ensure_builtin_skills(base)
        (base / "notes" / "my-notes.txt").write_text("precious", encoding="utf-8")
        _bump_mtime(src / "SKILL.md")  # trigger the update gate, same content

        _ensure_builtin_skills(base)

        backup = base / ".notes.user-backup"
        assert (backup / "my-notes.txt").read_text(encoding="utf-8") == "precious"
        assert (base / "notes" / "SKILL.md").is_file()

    def test_quarantine_never_clobbers_prior_quarantine(
        self, builtin_root: Path, base: Path
    ) -> None:
        src = _make_skill(builtin_root, "deploy", "packaged v1")
        _make_skill(base, "deploy", "USER first")
        _bump_mtime(src / "SKILL.md")
        _ensure_builtin_skills(base)
        first = _backup_skill_md(base / ".deploy.user-backup")
        assert "USER first" in first.read_text(encoding="utf-8")

        # A second colliding copy appears (restore from a user's own backup,
        # rollback, ...) and the package ships an update: the next quarantine
        # must take a numbered name, not overwrite the first.
        dest_md = base / "deploy" / "SKILL.md"
        dest_md.write_text("USER second", encoding="utf-8")
        (src / "SKILL.md").write_text("packaged v2", encoding="utf-8")
        _bump_mtime(src / "SKILL.md", 120.0)

        _ensure_builtin_skills(base)

        assert "USER first" in first.read_text(encoding="utf-8")
        second = _backup_skill_md(base / ".deploy.user-backup.2")
        assert "USER second" in second.read_text(encoding="utf-8")

    @requires_symlinks
    def test_dangling_symlink_at_backup_name_is_not_overwritten(
        self, builtin_root: Path, base: Path
    ) -> None:
        # ``Path.exists()`` reports False for a dangling symlink; the
        # quarantine namer must probe with ``lexists`` so it steps over the
        # occupied name instead of replacing the link.
        _make_skill(base, "deploy", "USER original")
        src = _make_skill(builtin_root, "deploy", "packaged builtin")
        _bump_mtime(src / "SKILL.md")
        os.symlink(base / "no-such-target", base / ".deploy.user-backup")

        _ensure_builtin_skills(base)

        assert os.path.islink(base / ".deploy.user-backup")
        preserved = _backup_skill_md(base / ".deploy.user-backup.2")
        assert "USER original" in preserved.read_text(encoding="utf-8")

    def test_failed_claim_never_falls_through_to_destroy(
        self, builtin_root: Path, base: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When the atomic claim rename fails, the sync must leave the
        # destination untouched — a failed preserve must never degrade into
        # the deletion it guards against.
        _make_skill(base, "deploy", "USER original")
        src = _make_skill(builtin_root, "deploy", "packaged builtin")
        _bump_mtime(src / "SKILL.md")

        def _refuse(_src: object, _dst: object) -> None:
            raise OSError("simulated rename failure")

        monkeypatch.setattr(skills_mod.os, "replace", _refuse)
        _ensure_builtin_skills(base)

        assert "USER original" in (base / "deploy" / "SKILL.md").read_text(encoding="utf-8")
        assert not os.path.lexists(base / ".deploy.user-backup")

    def test_backup_is_not_discovered_as_a_live_skill(
        self, builtin_root: Path, base: Path
    ) -> None:
        # The quarantine directory is a sibling (not dot-prefixed), so its
        # SKILL.md must be deactivated or the backup shadows the builtin that
        # replaced it in trigger matching, once per numbered copy.
        _make_skill(base, "deploy", "USER original")
        src = _make_skill(builtin_root, "deploy", "packaged builtin")
        _bump_mtime(src / "SKILL.md")

        _ensure_builtin_skills(base)

        discovered = {name for name, _ in skills_mod._iter_skill_files(base)}
        assert "deploy" in discovered
        assert "deploy.user-backup" not in discovered

    def test_empty_placeholder_dir_is_not_quarantined(
        self, builtin_root: Path, base: Path
    ) -> None:
        # App registration leaves an empty ``skills/<name>/`` placeholder
        # behind; there is nothing in it to preserve, so quarantining it would
        # mint a junk backup on every update cycle.
        (base / "deploy").mkdir()
        src = _make_skill(builtin_root, "deploy", "packaged builtin")
        _bump_mtime(src / "SKILL.md")

        _ensure_builtin_skills(base)

        assert "packaged builtin" in (base / "deploy" / "SKILL.md").read_text(encoding="utf-8")
        assert not os.path.lexists(base / ".deploy.user-backup")
        # A zero-entry placeholder holds no bytes to park: it is rmdir'd
        # (kernel-atomic, fails if anything landed) without using the slot.
        assert not os.path.lexists(base / ".deploy.superseded")


class TestStaleCleanupGuard:
    """The by-name stale sweep may only delete verifiable sync-installed copies."""

    def test_user_skill_named_cron_survives_startup(
        self, builtin_root: Path, base: Path
    ) -> None:
        # `cron` is in the stale-cleanup set; deletion by name alone is the
        # exact data-loss path this guard closes.
        _make_skill(base, "cron", "my own cron skill")

        _ensure_builtin_skills(base)

        assert "my own cron skill" in (base / "cron" / "SKILL.md").read_text(encoding="utf-8")

    def test_unchanged_sync_installed_stale_copy_is_still_removed(
        self, builtin_root: Path, base: Path
    ) -> None:
        # The guard must not disable the cleanup itself: a copy the sync
        # verifiably installed and that nobody touched is still swept from
        # its live name. It is parked at the hidden retirement slot rather
        # than deleted outright, so bytes written through a file descriptor
        # that survived the claim rename are never lost.
        _make_skill(base, "subagent", "old builtin")
        _record_builtin_provenance(base / "subagent")

        _ensure_builtin_skills(base)

        assert not (base / "subagent").exists()
        parked = base / ".subagent.superseded"
        assert "old builtin" in (parked / "SKILL.md").read_text(encoding="utf-8")

    def test_user_edited_stale_copy_is_left_alone(
        self, builtin_root: Path, base: Path
    ) -> None:
        # A marker whose fingerprint no longer matches means the user edited
        # the copy after install: it is user data and stays at its own name.
        _make_skill(base, "learn", "old builtin")
        _record_builtin_provenance(base / "learn")
        (base / "learn" / "SKILL.md").write_text("USER EDIT", encoding="utf-8")

        _ensure_builtin_skills(base)

        assert (base / "learn" / "SKILL.md").read_text(encoding="utf-8") == "USER EDIT"


class TestLegitimateUpdatesStillHappen:
    """The guard must never freeze normal package updates."""

    def test_unmodified_builtin_is_updated_when_package_ships_newer(
        self, builtin_root: Path, base: Path
    ) -> None:
        src = _make_skill(builtin_root, "helper", "v1", {"scripts/tool.py": "# v1"})
        _ensure_builtin_skills(base)

        (src / "SKILL.md").write_text("---\nname: helper\n---\nv2\n", encoding="utf-8")
        (src / "scripts" / "tool.py").write_text("# v2", encoding="utf-8")
        _bump_mtime(src / "SKILL.md")

        _ensure_builtin_skills(base)

        assert "v2" in (base / "helper" / "SKILL.md").read_text(encoding="utf-8")
        assert (base / "helper" / "scripts" / "tool.py").read_text(encoding="utf-8") == "# v2"
        # Updated cleanly in place: no quarantine involved.
        assert not os.path.lexists(base / ".helper.user-backup")
        # The superseded v1 copy is parked at the hidden retirement slot for
        # one cycle (never deleted while writes could still be landing in it).
        parked = base / ".helper.superseded"
        assert "v1" in (parked / "SKILL.md").read_text(encoding="utf-8")

    def test_clean_install_records_provenance(self, builtin_root: Path, base: Path) -> None:
        _make_skill(builtin_root, "helper", "v1")

        _ensure_builtin_skills(base)

        marker = base / "helper" / _PROVENANCE_MARKER
        recorded = marker.read_text(encoding="utf-8").strip()
        expected = _skill_tree_fingerprint(base / "helper")
        assert recorded == f"{skills_mod._PROVENANCE_FORMAT}:{expected}"

    def test_pre_provenance_install_is_adopted_then_updated(
        self, builtin_root: Path, base: Path
    ) -> None:
        # First-install migration: an existing install has no marker. When it
        # matches the packaged tree exactly it is adopted as builtin-owned (so
        # builtins are not frozen forever), and a later package update then
        # replaces it without quarantine noise.
        src = _make_skill(builtin_root, "helper", "v1")
        dest = base / "helper"
        import shutil as _shutil

        _shutil.copytree(src, dest)  # a pre-provenance install: no marker

        _ensure_builtin_skills(base)  # adoption pass — no update due
        assert (dest / _PROVENANCE_MARKER).is_file()

        (src / "SKILL.md").write_text("---\nname: helper\n---\nv2\n", encoding="utf-8")
        _bump_mtime(src / "SKILL.md")
        _ensure_builtin_skills(base)

        assert "v2" in (dest / "SKILL.md").read_text(encoding="utf-8")
        assert not os.path.lexists(base / ".helper.user-backup")

    def test_diverged_unmarked_dest_is_not_adopted(
        self, builtin_root: Path, base: Path
    ) -> None:
        # A colliding user skill whose SKILL.md is NEWER than the packaged one
        # never becomes update-due; the adoption pass must refuse to bless it
        # (stat manifests differ, so no content is even read).
        src = _make_skill(builtin_root, "deploy", "packaged")
        user = _make_skill(base, "deploy", "USER own", {"notes.txt": "mine"})
        _bump_mtime(user / "SKILL.md")
        assert (user / "SKILL.md").stat().st_mtime > (src / "SKILL.md").stat().st_mtime

        _ensure_builtin_skills(base)

        assert not (user / _PROVENANCE_MARKER).exists()
        assert "USER own" in (user / "SKILL.md").read_text(encoding="utf-8")

    def test_script_only_package_update_reaches_the_install(
        self, builtin_root: Path, base: Path
    ) -> None:
        # The manifest is NOT a version stamp for the skill. A release that
        # only touches a bundled script leaves SKILL.md byte-identical with its
        # packaged mtime, and a manifest-only update gate then reports "up to
        # date" forever -- the install keeps executing superseded code. Broke
        # prepare-pr in practice: its extractor was fixed in the package while
        # every install kept the previous copy and failed against CI's current
        # workflow.
        src = _make_skill(builtin_root, "helper", "v1", {"scripts/tool.py": "# v1"})
        _ensure_builtin_skills(base)
        assert (base / "helper" / "scripts" / "tool.py").read_text(encoding="utf-8") == "# v1"

        script = src / "scripts" / "tool.py"
        script.write_text("# v2", encoding="utf-8")
        _bump_mtime(script)
        manifest_mtime = (src / "SKILL.md").stat().st_mtime

        _ensure_builtin_skills(base)

        assert (base / "helper" / "scripts" / "tool.py").read_text(encoding="utf-8") == "# v2"
        # The manifest genuinely never moved, so the update cannot have come
        # from the manifest arm of the gate.
        assert (src / "SKILL.md").stat().st_mtime == manifest_mtime
        assert not os.path.lexists(base / ".helper.user-backup")

    def test_provenance_marker_mtime_cannot_suppress_an_update(
        self, builtin_root: Path, base: Path
    ) -> None:
        # The marker is written AFTER the copy, so its mtime is install time.
        # Counting it would make every destination newer than the package it
        # came from and the tree arm would never fire again.
        src = _make_skill(builtin_root, "helper", "v1", {"scripts/tool.py": "# v1"})
        _ensure_builtin_skills(base)
        _bump_mtime(base / "helper" / _PROVENANCE_MARKER, seconds=600.0)

        script = src / "scripts" / "tool.py"
        script.write_text("# v2", encoding="utf-8")
        _bump_mtime(script)

        _ensure_builtin_skills(base)

        assert (base / "helper" / "scripts" / "tool.py").read_text(encoding="utf-8") == "# v2"

    def test_unchanged_install_is_not_recopied_each_sync(
        self, builtin_root: Path, base: Path
    ) -> None:
        # Widening the gate must not make a steady state churn: copytree copies
        # with copy2, so an untouched install is mtime-equal to its package and
        # nothing is due. A re-copy would rotate the retirement slot on every
        # startup, quietly discarding the previous cycle's parked tree.
        _make_skill(
            builtin_root,
            "helper",
            "v1",
            # A script NEWER than the manifest is the ordinary packaged shape,
            # and the one that would loop if the tree arm compared the source's
            # newest file against the destination's manifest.
            {"scripts/tool.py": "# v1"},
        )
        _bump_mtime(builtin_root / "helper" / "scripts" / "tool.py")
        _ensure_builtin_skills(base)

        _ensure_builtin_skills(base)

        assert not os.path.lexists(base / ".helper.superseded")

    def test_first_source_root_owns_a_colliding_name(
        self, builtin_root: Path, base: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two source roots shipping one name must not race each other's copy
        # inside a single run. Once the project pass installs, the destination is
        # this run's own output rather than user data, so letting the packaged
        # pass replace it decides the winner by comparing mtimes across two
        # unrelated trees -- and a project skill loses to a packaged one that
        # merely carries a newer auxiliary file.
        project = tmp_project = base.parent / "project-skills"
        project.mkdir()
        _make_skill(project, "helper", "PROJECT", {"scripts/tool.py": "# project"})
        packaged = _make_skill(
            builtin_root, "helper", "PACKAGED", {"scripts/tool.py": "# packaged"}
        )
        # Both the manifest arm and the tree arm would otherwise fire.
        _bump_mtime(packaged / "SKILL.md")
        _bump_mtime(packaged / "scripts" / "tool.py")
        monkeypatch.setattr(skills_mod, "_project_skills_dir", lambda: tmp_project)

        _ensure_builtin_skills(base)

        installed = (base / "helper" / "SKILL.md").read_text(encoding="utf-8")
        assert "PROJECT" in installed, installed
        assert (
            base / "helper" / "scripts" / "tool.py"
        ).read_text(encoding="utf-8") == "# project"

    def test_destination_vanishing_mid_sync_does_not_abort_the_run(
        self, builtin_root: Path, base: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The gateway and the CLI sync the same home concurrently, so the
        # destination can be claimed between the exists() check and the manifest
        # comparison -- a window the tree walks widened. An unguarded stat raises
        # FileNotFoundError out of the startup sync, losing every skill queued
        # behind it, so the vanished destination must read as update-due instead.
        _make_skill(builtin_root, "aaa-helper", "v1")
        _make_skill(builtin_root, "zzz-later", "v1")
        _ensure_builtin_skills(base)

        real_stat = Path.stat
        vanished = base / "aaa-helper" / "SKILL.md"
        seen = [0]

        def _vanish(self: Path, *args: object, **kwargs: object) -> os.stat_result:
            # The FIRST stat of the destination is the sync's own exists()
            # probe, which must succeed -- otherwise the manifest comparison is
            # never reached and this test proves nothing. Every stat after it
            # raises, which is the real race: the destination was claimed while
            # the tree walks ran in between.
            if self == vanished:
                seen[0] += 1
                if seen[0] > 1:
                    raise FileNotFoundError(2, "No such file or directory", str(self))
            return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "stat", _vanish)
        (builtin_root / "zzz-later" / "SKILL.md").write_text(
            "---\nname: zzz-later\n---\nv2\n", encoding="utf-8"
        )
        _bump_mtime(builtin_root / "zzz-later" / "SKILL.md")

        _ensure_builtin_skills(base)  # must not raise
        monkeypatch.undo()

        # The comparison past the exists() probe was actually reached.
        assert seen[0] > 1, f"manifest comparison never stat'd the destination ({seen[0]})"
        # The skill queued after the vanished one was still processed.
        assert "v2" in (base / "zzz-later" / "SKILL.md").read_text(encoding="utf-8")


class TestFingerprint:
    """The fingerprint covers the full tree, excludes only the marker, and can
    neither hang nor read unbounded bytes on the startup path."""

    def test_identical_trees_match(self, tmp_path: Path) -> None:
        a = _make_skill(tmp_path, "a", "same", {"scripts/x.py": "print(1)"})
        b = _make_skill(tmp_path, "b", "same", {"scripts/x.py": "print(1)"})
        # Same file NAMES and content; names of the roots don't participate.
        (a / "SKILL.md").write_text("body", encoding="utf-8")
        (b / "SKILL.md").write_text("body", encoding="utf-8")
        assert _skill_tree_fingerprint(a) == _skill_tree_fingerprint(b)

    def test_extra_file_changes_fingerprint(self, tmp_path: Path) -> None:
        a = _make_skill(tmp_path, "a", "same")
        before = _skill_tree_fingerprint(a)
        (a / "extra.txt").write_text("x", encoding="utf-8")
        assert _skill_tree_fingerprint(a) != before

    def test_extra_empty_directory_changes_fingerprint(self, tmp_path: Path) -> None:
        a = _make_skill(tmp_path, "a", "same")
        before = _skill_tree_fingerprint(a)
        (a / "assets").mkdir()
        assert _skill_tree_fingerprint(a) != before

    def test_marker_itself_is_excluded(self, tmp_path: Path) -> None:
        a = _make_skill(tmp_path, "a", "same")
        before = _skill_tree_fingerprint(a)
        _record_builtin_provenance(a)
        assert _skill_tree_fingerprint(a) == before

    @requires_symlinks
    def test_symlink_target_participates(self, tmp_path: Path) -> None:
        # A symlink is hashed by its target TEXT, never followed: retargeting
        # it diverges the tree, and outside file content can't leak into the
        # fingerprint through a link.
        a = _make_skill(tmp_path, "a", "same")
        os.symlink("target-one", a / "link")
        one = _skill_tree_fingerprint(a)
        os.remove(a / "link")
        os.symlink("target-two", a / "link")
        assert _skill_tree_fingerprint(a) != one

    @requires_symlinks
    def test_symlink_and_regular_file_with_same_bytes_differ(self, tmp_path: Path) -> None:
        a = _make_skill(tmp_path, "a", "same")
        b = _make_skill(tmp_path, "b", "same")
        (a / "entry").write_text("payload", encoding="utf-8")
        os.symlink("payload", b / "entry")
        assert _skill_tree_fingerprint(a) != _skill_tree_fingerprint(b)

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
    def test_fifo_is_never_opened(self, tmp_path: Path) -> None:
        # Opening a FIFO for reading blocks until a writer appears; on the
        # gateway startup path that is a permanent hang. Special files are
        # classified by lstat and never opened.
        a = _make_skill(tmp_path, "a", "same")
        os.mkfifo(a / "pipe")
        fingerprint = _skill_tree_fingerprint(a)  # must return, not hang
        assert fingerprint is not None

    def test_oversized_tree_is_unprovable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A tree over the read ceiling returns None ("cannot prove"), which
        # matches nothing — the caller then preserves rather than deletes, and
        # startup never reads more than the ceiling from any one tree.
        a = _make_skill(tmp_path, "a", "same")
        (a / "big.bin").write_text("x" * 4096, encoding="utf-8")
        monkeypatch.setattr(skills_mod, "_FINGERPRINT_MAX_BYTES", 1024)
        assert _skill_tree_fingerprint(a) is None

    def test_unreadable_file_is_unprovable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Unprovable, not sentinel-hashed: two unreadable files must never
        # fingerprint as equal, or a diverged tree could be blessed as an
        # exact packaged copy.
        a = _make_skill(tmp_path, "a", "same")
        (a / "hidden.txt").write_text("secret", encoding="utf-8")
        real_open = os.open

        # ``**kwargs`` because this replaces the os module attribute, so it is live for
        # the whole test INCLUDING teardown, where pytest's own tmp_path cleanup calls
        # os.open with dir_fd=. A stub narrower than the API it stands in for turns that
        # cleanup into a TypeError reported as an error at teardown.
        def _deny(path: object, flags: int, *args: object, **kwargs: object) -> int:
            if "hidden.txt" in str(path):
                raise PermissionError("denied")
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(skills_mod.os, "open", _deny)
        assert _skill_tree_fingerprint(a) is None


class TestMarkerHardening:
    """The provenance marker is data the sync wrote, never a path to follow."""

    @requires_symlinks
    def test_marker_symlink_is_not_followed_on_write(
        self, builtin_root: Path, base: Path, tmp_path: Path
    ) -> None:
        # A symlink planted at the marker path must not redirect the write
        # outside the skill directory: the atomic write renames a temp file
        # over the link, replacing it.
        victim = tmp_path / "victim.txt"
        victim.write_text("do not touch", encoding="utf-8")
        user = _make_skill(base, "deploy", "USER own")
        os.symlink(victim, user / _PROVENANCE_MARKER)
        src = _make_skill(builtin_root, "deploy", "packaged")
        _bump_mtime(src / "SKILL.md")

        _ensure_builtin_skills(base)

        assert victim.read_text(encoding="utf-8") == "do not touch"

    @requires_symlinks
    def test_marker_symlink_reads_as_no_provenance(self, tmp_path: Path) -> None:
        # A symlink at the marker path is not a marker: the directory counts
        # as user-authored ("no provenance") instead of trusting content read
        # through a link.
        a = _make_skill(tmp_path, "a", "own")
        real = tmp_path / "real-marker"
        real.write_text("deadbeef\n", encoding="utf-8")
        os.symlink(real, a / _PROVENANCE_MARKER)
        assert skills_mod._recorded_fingerprint(a) is None


class TestVerificationBounds:
    """Verification stays bounded and crash-free on the startup path."""

    def test_entry_count_over_ceiling_is_unprovable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _make_skill(tmp_path, "a", "same")
        for i in range(8):
            (a / f"f{i}.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(skills_mod, "_FINGERPRINT_MAX_ENTRIES", 4)
        assert _skill_tree_fingerprint(a) is None
        assert skills_mod._trees_stat_equal(a, a) is False

    @pytest.mark.skipif(os.name == "nt", reason="hardlink semantics differ")
    def test_hardlinked_file_is_unprovable(self, tmp_path: Path) -> None:
        # A hardlink planted at a walked name aliases an inode that may live
        # anywhere (e.g. a credential file); the descriptor-pinned reader
        # rejects st_nlink > 1, so the tree reads as unprovable instead of
        # hashing the linked bytes.
        a = _make_skill(tmp_path, "a", "same")
        outside = tmp_path / "outside-secret"
        outside.write_text("credential bytes", encoding="utf-8")
        os.link(outside, a / "innocuous.txt")
        assert _skill_tree_fingerprint(a) is None

    @requires_symlinks
    def test_link_root_is_unprovable(self, tmp_path: Path) -> None:
        real = _make_skill(tmp_path, "real", "content")
        link = tmp_path / "linked"
        os.symlink(real, link)
        assert _skill_tree_fingerprint(link) is None

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
    def test_unlistable_subdir_makes_collision_preserve_not_crash(
        self, builtin_root: Path, base: Path, request: pytest.FixtureRequest
    ) -> None:
        # A colliding destination with an unlistable subdirectory must read as
        # "has content, unprovable" — quarantined whole — never as empty (which
        # deletes it) and never as a startup crash.
        user = _make_skill(base, "deploy", "USER original")
        locked = user / "locked"
        locked.mkdir()
        (locked / "data.txt").write_text("hidden", encoding="utf-8")
        os.chmod(locked, 0)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        request.addfinalizer(lambda: _restore_locked(base, "deploy"))
        src = _make_skill(builtin_root, "deploy", "packaged")
        _bump_mtime(src / "SKILL.md")

        _ensure_builtin_skills(base)

        assert "packaged" in (base / "deploy" / "SKILL.md").read_text(encoding="utf-8")
        backup = base / ".deploy.user-backup"
        assert backup.is_dir()
        assert "USER original" in _backup_skill_md(backup).read_text(encoding="utf-8")

    def test_failed_slot_rotation_preserves_occupant_and_still_updates(
        self, builtin_root: Path, base: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # rmtree only ever runs against the epoch-old retirement slot. When
        # even that fails partway, the occupant is preserved out of the slot's
        # way, the update still lands, and nothing stays hidden at a claim
        # path or crashes startup.
        src = _make_skill(builtin_root, "helper", "v1")
        _ensure_builtin_skills(base)
        (src / "SKILL.md").write_text("---\nname: helper\n---\nv2\n", encoding="utf-8")
        _bump_mtime(src / "SKILL.md")
        _ensure_builtin_skills(base)  # parks v1 at the retirement slot
        (src / "SKILL.md").write_text("---\nname: helper\n---\nv3\n", encoding="utf-8")
        _bump_mtime(src / "SKILL.md", 180.0)

        def _refuse(_path: object, **_kw: object) -> None:
            raise OSError("simulated rmtree failure")

        monkeypatch.setattr(skills_mod.shutil, "rmtree", _refuse)
        _ensure_builtin_skills(base)  # must not raise

        assert "v3" in (base / "helper" / "SKILL.md").read_text(encoding="utf-8")
        assert not os.path.lexists(base / ".helper.sync-claim")
        # The v1 occupant that could not be deleted is preserved, not lost.
        preserved = list(base.glob("helper.user-backup*")) + list(
            base.glob(".helper.user-backup*")
        )
        assert preserved

    @requires_symlinks
    def test_linked_dest_is_quarantined_without_following(
        self, builtin_root: Path, base: Path, tmp_path: Path
    ) -> None:
        # A destination that is a symlink must be moved aside AS a link:
        # the quarantine rename moves the link object itself and the target
        # tree is never entered or modified.
        target = _make_skill(tmp_path, "elsewhere", "USER tree behind a link")
        os.symlink(target, base / "deploy")
        src = _make_skill(builtin_root, "deploy", "packaged")
        _bump_mtime(src / "SKILL.md")

        _ensure_builtin_skills(base)

        assert "packaged" in (base / "deploy" / "SKILL.md").read_text(encoding="utf-8")
        # The target tree is untouched, SKILL.md still at its own name.
        assert "USER tree behind a link" in (target / "SKILL.md").read_text(encoding="utf-8")
        moved = base / ".deploy.user-backup"
        assert os.path.islink(moved)

    def test_install_records_packaged_fingerprint_not_dest_state(
        self, builtin_root: Path, base: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A user write landing in the destination during installation must not
        # be blessed as sync-owned: the marker records the PACKAGED tree, so
        # the raced-in file diverges the destination and is preserved later.
        src = _make_skill(builtin_root, "helper", "v1")
        real_copytree = skills_mod.shutil.copytree

        def _race(src_arg: object, dst_arg: object, **kw: object) -> object:
            result = real_copytree(str(src_arg), str(dst_arg), **kw)
            (Path(str(dst_arg)) / "raced-in.txt").write_text("user", encoding="utf-8")
            return result

        monkeypatch.setattr(skills_mod.shutil, "copytree", _race)
        _ensure_builtin_skills(base)

        recorded = (base / "helper" / skills_mod._PROVENANCE_MARKER).read_text(
            encoding="utf-8"
        ).strip()
        fmt = skills_mod._PROVENANCE_FORMAT
        assert recorded == f"{fmt}:{_skill_tree_fingerprint(src)}"
        assert recorded != f"{fmt}:{_skill_tree_fingerprint(base / 'helper')}"

    def test_project_skill_named_cron_is_not_swept(
        self, builtin_root: Path, base: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A project source can legitimately ship a skill named after a
        # stale-cleanup entry; a name a source still ships is not stale, so
        # the sweep must not delete what the sync just installed.
        proj = tmp_path / "project-skills"
        proj.mkdir()
        _make_skill(proj, "cron", "project cron skill")
        monkeypatch.setattr(skills_mod, "_project_skills_dir", lambda: proj)

        _ensure_builtin_skills(base)

        installed = base / "cron" / "SKILL.md"
        assert "project cron skill" in installed.read_text(encoding="utf-8")


def _restore_locked(base: Path, name: str) -> None:
    """Re-open permission-locked test dirs so pytest can clean tmp_path."""
    for candidate in base.parent.rglob("locked"):
        try:
            os.chmod(candidate, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        except OSError:
            pass


class TestConcurrentSync:
    """Two processes syncing the same home must not crash each other."""

    def test_concurrent_install_race_keeps_the_winner(
        self, builtin_root: Path, base: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Another process recreates the destination between our claim and our
        # copytree: losing the race keeps the winner's copy and moves on.
        _make_skill(base, "deploy", "USER original")
        src = _make_skill(builtin_root, "deploy", "packaged")
        _bump_mtime(src / "SKILL.md")
        real_copytree = skills_mod.shutil.copytree

        def _race(src_arg: object, dst_arg: object, **kw: object) -> object:
            real_copytree(str(src_arg), str(dst_arg), **kw)  # winner lands first
            raise FileExistsError(str(dst_arg))

        monkeypatch.setattr(skills_mod.shutil, "copytree", _race)
        _ensure_builtin_skills(base)  # must not raise

        assert "packaged" in (base / "deploy" / "SKILL.md").read_text(encoding="utf-8")
        assert "USER original" in _backup_skill_md(base / ".deploy.user-backup").read_text(
            encoding="utf-8"
        )


class TestEventLoopGuard:
    """Loader construction on a running event loop must not run the sync."""

    def test_sync_skipped_on_running_loop(
        self, builtin_root: Path, base: Path
    ) -> None:
        import asyncio

        _make_skill(builtin_root, "deploy", "packaged")

        async def _build() -> None:
            skills_mod.SkillsLoader(skills_path=base)

        asyncio.run(_build())
        assert not (base / "deploy").exists()

    def test_sync_runs_off_loop(self, builtin_root: Path, base: Path) -> None:
        _make_skill(builtin_root, "deploy", "packaged")
        skills_mod.SkillsLoader(skills_path=base)
        assert (base / "deploy" / "SKILL.md").is_file()


class TestQuarantineDeactivationFallback:
    """A quarantine whose SKILL.md cannot be renamed is hidden whole."""

    def test_sync_builtins_seam_syncs_explicitly(
        self, builtin_root: Path, base: Path
    ) -> None:
        # The explicit seam works regardless of construction context: a loader
        # built without syncing (as the gateway does before its socket binds)
        # syncs when told to, from the worker-thread background task.
        _make_skill(builtin_root, "deploy", "packaged")
        loader = skills_mod.SkillsLoader(skills_path=base, install_builtins=False)
        assert not (base / "deploy").exists()
        loader.sync_builtins()
        assert (base / "deploy" / "SKILL.md").is_file()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
    def test_mode_change_diverges_fingerprint(self, tmp_path: Path) -> None:
        # A mode-only customization (chmod +x on a script) is a user edit:
        # it must diverge the tree so an update preserves it instead of
        # silently resetting the mode.
        a = _make_skill(tmp_path, "a", "same", {"scripts/run.sh": "echo hi"})
        before = _skill_tree_fingerprint(a)
        os.chmod(a / "scripts" / "run.sh", 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        assert _skill_tree_fingerprint(a) != before

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
    def test_fifo_skill_md_in_backup_is_deactivated(
        self, builtin_root: Path, base: Path
    ) -> None:
        # Whatever occupies the SKILL.md name in a quarantined tree must stop
        # being discoverable — a FIFO left live there would block the first
        # reader that opens it.
        user = base / "deploy"
        user.mkdir()
        os.mkfifo(user / "SKILL.md")
        src = _make_skill(builtin_root, "deploy", "packaged")
        _bump_mtime(src / "SKILL.md")

        _ensure_builtin_skills(base)

        backup = base / ".deploy.user-backup"
        assert backup.is_dir()
        # The tree is moved wholesale and never entered: the FIFO stays at
        # its own name, deactivated by the dot-prefix alone.
        assert os.path.lexists(backup / "SKILL.md")

    def test_nested_empty_directory_structure_is_preserved(
        self, builtin_root: Path, base: Path
    ) -> None:
        # A colliding tree holding only empty subdirectories is still
        # user-made structure: it must be quarantined, not deleted. Only a
        # zero-entry directory counts as content-free.
        (base / "deploy" / "layouts" / "drafts").mkdir(parents=True)
        src = _make_skill(builtin_root, "deploy", "packaged")
        _bump_mtime(src / "SKILL.md")

        _ensure_builtin_skills(base)

        assert (base / ".deploy.user-backup" / "layouts" / "drafts").is_dir()
        assert "packaged" in (base / "deploy" / "SKILL.md").read_text(encoding="utf-8")


class TestVerifiedCopyRetirement:
    """Verified copies are parked for one cycle, never deleted while hot.

    Deleting a just-verified claim can still lose bytes written through file
    descriptors that survived the claim rename (the fingerprint ran before
    those writes landed). The sync therefore parks verified copies at a hidden
    per-name ``.<name>.superseded`` slot and only deletes the slot's previous,
    epoch-old occupant — after re-verifying it — so a late write converts
    deletion into preservation. Retention stays bounded: one slot per name.
    """

    def test_slot_rotates_deleting_only_epoch_old_copy(
        self, builtin_root: Path, base: Path
    ) -> None:
        src = _make_skill(builtin_root, "helper", "v1")
        _ensure_builtin_skills(base)
        (src / "SKILL.md").write_text("---\nname: helper\n---\nv2\n", encoding="utf-8")
        _bump_mtime(src / "SKILL.md")
        _ensure_builtin_skills(base)  # parks v1
        (src / "SKILL.md").write_text("---\nname: helper\n---\nv3\n", encoding="utf-8")
        _bump_mtime(src / "SKILL.md", 180.0)

        _ensure_builtin_skills(base)  # epoch-old v1 verified and deleted; v2 parked

        assert "v3" in (base / "helper" / "SKILL.md").read_text(encoding="utf-8")
        parked = base / ".helper.superseded"
        assert "v2" in (parked / "SKILL.md").read_text(encoding="utf-8")
        # Rotation is bounded: exactly one parked copy, no quarantines minted.
        assert not list(base.glob("helper.user-backup*"))
        assert not list(base.glob(".helper.user-backup*"))

    def test_late_write_into_parked_copy_is_preserved(
        self, builtin_root: Path, base: Path
    ) -> None:
        # Simulates the racing write the retirement exists for: bytes landing
        # in the tree after it was fingerprinted and parked. The re-check at
        # rotation time sees the divergence and preserves instead of deleting.
        src = _make_skill(builtin_root, "helper", "v1")
        _ensure_builtin_skills(base)
        (src / "SKILL.md").write_text("---\nname: helper\n---\nv2\n", encoding="utf-8")
        _bump_mtime(src / "SKILL.md")
        _ensure_builtin_skills(base)  # parks v1
        (base / ".helper.superseded" / "late-write.txt").write_text(
            "USER BYTES", encoding="utf-8"
        )
        (src / "SKILL.md").write_text("---\nname: helper\n---\nv3\n", encoding="utf-8")
        _bump_mtime(src / "SKILL.md", 180.0)

        _ensure_builtin_skills(base)

        preserved = list(base.glob("helper.user-backup*")) + list(
            base.glob(".helper.user-backup*")
        )
        assert preserved
        assert (preserved[0] / "late-write.txt").read_text(encoding="utf-8") == "USER BYTES"
        # The slot rotated normally after the occupant was moved to safety.
        assert "v2" in (base / ".helper.superseded" / "SKILL.md").read_text(encoding="utf-8")

    def test_adopted_claim_is_reverifiable_next_cycle(
        self, builtin_root: Path, base: Path
    ) -> None:
        # A claim proven by the first-install migration rule (matches the
        # packaged tree, no marker of its own) must gain a marker when parked,
        # or the next rotation would mistake it for user data forever.
        _make_skill(base, "helper", "v1")
        src = _make_skill(builtin_root, "helper", "v1")
        _bump_mtime(src / "SKILL.md")

        _ensure_builtin_skills(base)

        parked = base / ".helper.superseded"
        assert (parked / _PROVENANCE_MARKER).is_file()

    def test_stale_retirement_slot_never_shadows_live_skills(
        self, builtin_root: Path, base: Path
    ) -> None:
        # The slot is dot-prefixed: discovery must never surface it.
        _make_skill(base, "cron", "old builtin")
        _record_builtin_provenance(base / "cron")
        _ensure_builtin_skills(base)

        discovered = {name for name, _ in skills_mod._iter_skill_files(base)}
        assert not any("superseded" in n for n in discovered)

    def test_marker_only_destination_is_removed_cleanly(
        self, builtin_root: Path, base: Path
    ) -> None:
        # A destination holding nothing but our own provenance marker (the
        # user deleted the content) carries no user bytes: it is removed
        # without minting a junk quarantine or consuming the slot.
        dest = base / "helper"
        dest.mkdir()
        _record_builtin_provenance(dest)
        src = _make_skill(builtin_root, "helper", "packaged")
        _bump_mtime(src / "SKILL.md")

        _ensure_builtin_skills(base)

        assert "packaged" in (base / "helper" / "SKILL.md").read_text(encoding="utf-8")
        assert not list(base.glob("helper.user-backup*"))
        assert not list(base.glob(".helper.user-backup*"))
        assert not os.path.lexists(base / ".helper.superseded")

    def test_stale_slot_is_disposed_on_the_following_sweep(
        self, builtin_root: Path, base: Path
    ) -> None:
        # Nothing ever ships for a stale name again, so its parked copy is
        # disposed of by the NEXT sweep — its full quiescent cycle — instead
        # of lingering forever.
        _make_skill(base, "subagent", "old builtin")
        _record_builtin_provenance(base / "subagent")
        _ensure_builtin_skills(base)  # parks
        assert (base / ".subagent.superseded").is_dir()

        _ensure_builtin_skills(base)  # following sweep disposes

        assert not os.path.lexists(base / ".subagent.superseded")
        assert not list(base.glob("subagent.user-backup*"))

    def test_stale_slot_with_late_writes_is_preserved_on_disposal(
        self, builtin_root: Path, base: Path
    ) -> None:
        # Bytes that landed in the parked copy after it was fingerprinted are
        # exactly what the retirement protects: disposal must preserve them.
        _make_skill(base, "cron", "old builtin")
        _record_builtin_provenance(base / "cron")
        _ensure_builtin_skills(base)  # parks
        (base / ".cron.superseded" / "late.txt").write_text("USER BYTES", encoding="utf-8")

        _ensure_builtin_skills(base)

        assert not os.path.lexists(base / ".cron.superseded")
        preserved = list(base.glob("cron.user-backup*")) + list(
            base.glob(".cron.user-backup*")
        )
        assert preserved
        assert (preserved[0] / "late.txt").read_text(encoding="utf-8") == "USER BYTES"


class TestStandaloneCliPathsSync:
    """Gateway-less async entry points must run the explicit sync seam.

    Construction-time sync skips itself on a running event loop; the gateway
    compensates at startup, but standalone ``kirocrew run`` and the eval
    runner have no gateway to own the sync and must call the seam themselves
    (off-loop, mirroring the gateway pattern).
    """

    def test_run_task_uses_explicit_off_loop_sync(self) -> None:
        import inspect

        from kiro_crew import cli_server

        src = inspect.getsource(cli_server._run_task)
        assert "SkillsLoader(install_builtins=False)" in src
        assert "asyncio.to_thread(skills.sync_builtins)" in src

    def test_eval_runner_uses_explicit_off_loop_sync(self) -> None:
        import inspect

        from kiro_crew.eval import runner as eval_runner

        src = inspect.getsource(eval_runner)
        assert "install_builtins=False" in src
        assert "asyncio.to_thread(skills.sync_builtins)" in src
        # The eval loader must target the scenario workspace, never the
        # user's real skills home.
        assert 'skills_path=ws / "skills"' in src

    def test_seam_installs_builtins_when_awaited_from_async(
        self, builtin_root: Path, base: Path
    ) -> None:
        import asyncio

        _make_skill(builtin_root, "deploy", "packaged")

        async def _boot() -> None:
            loader = skills_mod.SkillsLoader(skills_path=base, install_builtins=False)
            await asyncio.to_thread(loader.sync_builtins)

        asyncio.run(_boot())
        assert (base / "deploy" / "SKILL.md").is_file()


class TestPermissionBitsInFingerprint:
    """chmod customizations on dirs (and the root) diverge the tree."""

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
    def test_child_dir_mode_change_diverges_fingerprint(self, tmp_path: Path) -> None:
        a = _make_skill(tmp_path, "a", "same", {"scripts/run.sh": "x"})
        before = _skill_tree_fingerprint(a)
        os.chmod(a / "scripts", 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(a / "scripts", 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        assert _skill_tree_fingerprint(a) == before  # restored mode: unchanged
        os.chmod(a / "scripts", 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        assert _skill_tree_fingerprint(a) != before

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
    def test_root_dir_mode_change_diverges_fingerprint(self, tmp_path: Path) -> None:
        a = _make_skill(tmp_path, "a", "same")
        before = _skill_tree_fingerprint(a)
        os.chmod(a, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        assert _skill_tree_fingerprint(a) != before

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
    def test_chmod_customized_builtin_is_preserved_on_update(
        self, builtin_root: Path, base: Path
    ) -> None:
        # End to end: sync installs a builtin, the user chmods a subdirectory,
        # then an update ships. The customized tree no longer matches the
        # recorded fingerprint, so it must be quarantined, not deleted.
        _make_skill(builtin_root, "helper", "v1", {"scripts/tool.py": "# v1"})
        _ensure_builtin_skills(base)
        os.chmod(base / "helper" / "scripts", 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions

        src_md = builtin_root / "helper" / "SKILL.md"
        src_md.write_text(
            "---\nname: helper\ndescription: v2\n---\nv2\n", encoding="utf-8"
        )
        _bump_mtime(src_md)
        _ensure_builtin_skills(base)

        assert "v2" in (base / "helper" / "SKILL.md").read_text(encoding="utf-8")
        backup = base / ".helper.user-backup"
        assert backup.is_dir()
        assert "v1" in _backup_skill_md(backup).read_text(encoding="utf-8")


class TestMarkerFormatVersioning:
    """An unparseable (old/future format) marker means "no provenance"."""

    def test_old_format_marker_falls_back_to_adoption(
        self, builtin_root: Path, base: Path
    ) -> None:
        # A marker written by a pre-versioning build holds a bare fingerprint.
        # After a format bump it must be unparseable -> on the next no-update
        # startup the adoption pass re-verifies the copy against the packaged
        # tree and re-records ownership in the current format, so a later
        # content update proceeds in place instead of misreading the unchanged
        # copy as diverged and quarantining it.
        _make_skill(builtin_root, "helper", "v1")
        _ensure_builtin_skills(base)
        marker = base / "helper" / _PROVENANCE_MARKER
        bare = _skill_tree_fingerprint(base / "helper")
        marker.write_text(f"{bare}\n", encoding="utf-8")  # legacy format

        _ensure_builtin_skills(base)  # no update due: adoption pass runs
        recorded = marker.read_text(encoding="utf-8").strip()
        assert recorded.startswith(skills_mod._PROVENANCE_FORMAT + ":")

        src_md = builtin_root / "helper" / "SKILL.md"
        src_md.write_text(
            "---\nname: helper\ndescription: v2\n---\nv2\n", encoding="utf-8"
        )
        _bump_mtime(src_md)
        _ensure_builtin_skills(base)

        # Untouched copy: updated in place, no quarantine minted.
        assert "v2" in (base / "helper" / "SKILL.md").read_text(encoding="utf-8")
        assert not (base / ".helper.user-backup").exists()

    def test_old_format_marker_with_user_edits_is_preserved(
        self, builtin_root: Path, base: Path
    ) -> None:
        # Legacy marker AND a real user edit: the adoption comparison fails
        # (tree != packaged tree), so the tree is user data and must survive.
        _make_skill(builtin_root, "helper", "v1")
        _ensure_builtin_skills(base)
        (base / "helper" / _PROVENANCE_MARKER).write_text(
            "legacy-fingerprint\n", encoding="utf-8"
        )
        (base / "helper" / "my-notes.txt").write_text("precious", encoding="utf-8")

        src_md = builtin_root / "helper" / "SKILL.md"
        src_md.write_text(
            "---\nname: helper\ndescription: v2\n---\nv2\n", encoding="utf-8"
        )
        _bump_mtime(src_md)
        _ensure_builtin_skills(base)

        assert "v2" in (base / "helper" / "SKILL.md").read_text(encoding="utf-8")
        backup = base / ".helper.user-backup"
        assert backup.is_dir()
        assert (backup / "my-notes.txt").read_text(encoding="utf-8") == "precious"


class TestJunctionClassification:
    """A Windows junction must be treated as a link BEFORE descent."""

    def test_junction_like_dir_is_classified_as_link_not_descended(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # POSIX cannot create a junction, so simulate its observable shape: a
        # directory entry that lstats as a plain dir but that
        # ``is_link_or_junction`` reports True for. The walk must classify it
        # as a link (never "dir") and must NOT enumerate anything inside it —
        # a junction to a credential directory would otherwise have its
        # contents listed outside the file-read gate.
        a = _make_skill(tmp_path, "a", "body")
        junction = a / "jct"
        junction.mkdir()
        (junction / "secret.txt").write_text("credential", encoding="utf-8")

        real = skills_mod.is_link_or_junction

        def _fake(path: object) -> bool:
            if Path(str(path)) == junction:
                return True
            return real(path)

        monkeypatch.setattr(skills_mod, "is_link_or_junction", _fake)

        entries = {rel: (kind, detail) for rel, kind, detail in skills_mod._tree_entries(a)}
        assert "jct" in entries
        kind, _detail = entries["jct"]
        # readlink on a real directory fails -> "unreadable" (equals nothing,
        # fails toward diverged); on a real junction it returns the target
        # text ("link"). Either way it must not read as a plain "dir".
        assert kind in ("link", "unreadable")
        assert not any(rel.startswith("jct/") for rel in entries)


class TestMarkerNameCollision:
    """A user file that merely shares the marker NAME is user bytes."""

    def test_marker_only_user_dir_is_quarantined_not_deleted(
        self, builtin_root: Path, base: Path
    ) -> None:
        # The tree walk excludes the marker name, so a directory holding ONLY
        # a user-made file at that name reports content-free and takes the
        # placeholder-removal path. Before the fix that path unlinked the
        # file unverified; it must instead fail verification (the content is
        # no fingerprint of this tree) and quarantine the directory intact.
        user = base / "deploy"
        user.mkdir()
        (user / skills_mod._PROVENANCE_MARKER).write_text(
            "user bytes that happen to live at the marker name",
            encoding="utf-8",
        )
        src = _make_skill(builtin_root, "deploy", "packaged")
        _bump_mtime(src / "SKILL.md")

        _ensure_builtin_skills(base)

        assert "packaged" in (base / "deploy" / "SKILL.md").read_text(encoding="utf-8")
        preserved = [
            p for p in base.glob(".deploy.*")
            if (p / skills_mod._PROVENANCE_MARKER).is_file()
        ]
        assert preserved, "user marker-name file was deleted instead of preserved"
        content = (preserved[0] / skills_mod._PROVENANCE_MARKER).read_text(
            encoding="utf-8"
        )
        assert "user bytes" in content

    def test_genuine_empty_placeholder_still_removed(
        self, builtin_root: Path, base: Path
    ) -> None:
        # The fix must not break the placeholder rule: a zero-entry directory
        # with NO marker file is still removed without minting a quarantine.
        (base / "deploy").mkdir()
        src = _make_skill(builtin_root, "deploy", "packaged")
        _bump_mtime(src / "SKILL.md")

        _ensure_builtin_skills(base)

        assert "packaged" in (base / "deploy" / "SKILL.md").read_text(encoding="utf-8")
        assert not list(base.glob(".deploy.user-backup*"))
