"""Tests for kiro_crew.portability — export/import zip feature."""

from __future__ import annotations

import contextlib
import errno
import io
import json
import ntpath
import os
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePath
from unittest.mock import patch

import portability_helpers
import pytest

from conftest import make_dir_link, requires_o_nofollow
from kiro_crew import platform_compat, portability
from kiro_crew.jsonl_util import UnreadableRecord
from kiro_crew.portability import (
    EXPORT_EXCLUDE,
    apply_import_zip,
    create_export_zip,
    validate_import_zip,
)
from kiro_crew.security import is_sensitive_path


def _detach_dir_link(link: Path) -> None:
    """Remove the link ITSELF at *link*, leaving whatever it pointed at alone.

    The call differs by platform and the wrong one raises rather than misbehaving
    quietly: a POSIX directory symlink is a link entry, so ``rmdir`` answers
    ``NotADirectoryError`` and only ``unlink`` removes it; a Windows junction is a
    real directory entry, which ``unlink`` refuses. ``is_symlink()`` separates them
    -- it is False for a junction, which is the same property the production code
    under test is about.
    """
    if link.is_symlink():
        link.unlink()
    else:
        os.rmdir(link)


#: pathlib's ``**`` deliberately does NOT descend a directory SYMLINK, so on POSIX
#: the export walk cannot reach past one at all. A Windows JUNCTION is a different
#: reparse tag -- ``DirEntry.is_symlink()`` answers False for it -- so the same walk
#: descends it and reaches an ordinary file living outside the crew directory.
#: MEASURED both ways rather than assumed; the guard-the-guard assertion in the test
#: below re-checks it on the platform that runs.
walk_descends_a_directory_link = pytest.mark.skipif(
    not platform_compat.IS_WINDOWS,
    reason=(
        "pathlib's ** does not descend a POSIX directory symlink, so the export walk "
        "cannot reach past one; this escape is the Windows junction shape"
    ),
)


# The shared fixtures, bound here by name so pytest registers them for this module.
fake_kirocrew_home = portability_helpers.fake_kirocrew_home
patched_config_dir = portability_helpers.patched_config_dir


# ── Export Tests ──


class TestExport:
    def test_export_creates_valid_zip(self, patched_config_dir):
        zip_bytes, manifest = create_export_zip()
        assert len(zip_bytes) > 0
        assert manifest["version"] == portability.EXPORT_MANIFEST_VERSION
        assert manifest["format"] == "zip"
        assert "created_at" in manifest
        assert "hostname" in manifest
        assert "contents" in manifest

        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = zf.namelist()
        assert any("MANIFEST.json" in n for n in names)
        assert any("config.json" in n for n in names)
        zf.close()

    def test_export_includes_config(self, patched_config_dir):
        zip_bytes, _ = create_export_zip()
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        config_entries = [n for n in zf.namelist() if n.endswith("config.json")]
        assert len(config_entries) == 1
        data = json.loads(zf.read(config_entries[0]))
        assert data["agent"]["provider"] == "acp"
        zf.close()

    def test_export_includes_crons(self, patched_config_dir):
        zip_bytes, manifest = create_export_zip()
        assert manifest["contents"].get("crons.json", 0) > 0
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        cron_entries = [n for n in zf.namelist() if n.endswith("crons.json")]
        assert len(cron_entries) == 1
        data = json.loads(zf.read(cron_entries[0]))
        assert data["jobs"][0]["name"] == "daily-check"
        zf.close()

    def test_export_includes_memory_db(self, patched_config_dir):
        zip_bytes, manifest = create_export_zip()
        assert manifest["contents"].get("memory.db", 0) > 0
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        db_entries = [n for n in zf.namelist() if n.endswith("memory.db")]
        assert len(db_entries) == 1
        # Verify it's a valid SQLite DB
        db_bytes = zf.read(db_entries[0])
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.write(db_bytes)
        tmp.close()
        try:
            conn = sqlite3.connect(tmp.name)
            rows = conn.execute("SELECT key, value_json FROM semantic_memory").fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "user.name"
            conn.close()
        finally:
            os.unlink(tmp.name)
        zf.close()

    def test_export_includes_workspace_files(self, patched_config_dir):
        zip_bytes, manifest = create_export_zip()
        assert manifest["contents"]["workspace_files"] >= 4  # prefs, projects, 2 history
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = zf.namelist()
        assert any("preferences.md" in n for n in names)
        assert any("projects.md" in n for n in names)
        assert any("2026-05-17.md" in n for n in names)
        zf.close()

    def test_export_includes_skills(self, patched_config_dir):
        zip_bytes, manifest = create_export_zip()
        assert manifest["contents"]["skill_count"] >= 1
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = zf.namelist()
        assert any("SKILL.md" in n for n in names)
        zf.close()

    def test_export_includes_plan_memory(self, patched_config_dir):
        zip_bytes, manifest = create_export_zip()
        assert manifest["contents"]["plan_memory_files"] >= 1
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = zf.namelist()
        assert any("current_plan.md" in n for n in names)
        zf.close()

    def test_export_excludes_credentials(self, patched_config_dir):
        zip_bytes, _ = create_export_zip()
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = zf.namelist()
        for excluded in EXPORT_EXCLUDE:
            assert not any(n.endswith(excluded) for n in names), f"{excluded} should be excluded"
        zf.close()

    def test_export_excludes_snapshots_dir(self, patched_config_dir):
        zip_bytes, _ = create_export_zip()
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = zf.namelist()
        assert not any("snapshots" in n for n in names)
        assert not any("outbox" in n for n in names)
        zf.close()

    def test_export_excludes_pid_files(self, patched_config_dir):
        (patched_config_dir / "gateway.pid").write_text("99999")
        zip_bytes, _ = create_export_zip()
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = zf.namelist()
        assert not any(".pid" in n for n in names)
        zf.close()

    def test_export_skips_symlinks(self, patched_config_dir):
        link = patched_config_dir / "workspace" / "memory" / "evil_link.md"
        try:
            link.symlink_to("/etc/passwd")
        except OSError:
            pytest.skip("Cannot create symlinks")
        zip_bytes, _ = create_export_zip()
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = zf.namelist()
        assert not any("evil_link" in n for n in names)
        zf.close()

    @walk_descends_a_directory_link
    def test_export_does_not_package_files_reached_through_a_directory_link(
        self, patched_config_dir, tmp_path
    ):
        """`rglob` DESCENDS a directory link, and the file on the far side is real.

        `test_export_skips_symlinks` above covers a link that IS the entry. It does
        not cover a link crossed on the way DOWN: the document found beyond it
        answers False to `is_symlink()`, so the skip never fires, and the file is
        packaged into an archive the user hands to someone else.

        Neither filter below the skip is a containment test. `_is_excluded` is a
        rule about the archive NAME. `is_sensitive_path` does resolve links — so a
        linked `~/.aws` really would be caught — but it asks whether a path is a
        PROTECTED location, never whether it is inside the crew directory. An
        ordinary file of the user's is neither, and that is what leaked.

        Windows-only by construction, and measured rather than assumed: pathlib's
        `**` does not descend a POSIX directory symlink, so the production walk
        cannot reach past one there at all. A junction carries a different reparse
        tag, `DirEntry.is_symlink()` answers False for it, and the same walk goes
        straight through.
        """
        outside = tmp_path / "outside-the-crew-dir"
        outside.mkdir()
        (outside / "not-ours.md").write_text("private notes", encoding="utf-8")
        link = patched_config_dir / "workspace" / "memory" / "linked"
        make_dir_link(link, outside)

        # Guard the guard, through an oracle OUTSIDE the module under test — and
        # from the root the PRODUCTION walk starts at, not from the link itself.
        # Walking from the link would descend on every platform and prove nothing
        # about whether `workspace/`'s own walk ever gets there.
        reached = [
            p
            for p in (patched_config_dir / "workspace").rglob("*")
            if p.name == "not-ours.md"
        ]
        assert reached, "the walk never descended the link, so nothing was under test"
        assert reached[0].is_file() and not reached[0].is_symlink()

        zip_bytes, _ = create_export_zip()
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = zf.namelist()
        zf.close()
        assert not any("not-ours" in n for n in names), (
            f"content from outside the crew dir was packaged: {names}"
        )

    def test_export_still_packages_a_real_nested_workspace_file(
        self, patched_config_dir
    ):
        """Negative control: ordinary nested content must still be exported."""
        nested = patched_config_dir / "workspace" / "memory" / "deep" / "keep.md"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text("ours", encoding="utf-8")

        zip_bytes, _ = create_export_zip()
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = zf.namelist()
        zf.close()
        assert any(n.endswith("workspace/memory/deep/keep.md") for n in names), names

    def test_export_preserves_the_mtime_of_what_it_packages(self, patched_config_dir):
        """The archive entry is built by hand now, so its metadata must not regress.

        `ZipFile.write` took the timestamp from the file it opened; the entry is
        assembled from a descriptor instead, and the timestamp has to keep coming
        from the same bytes rather than from "now".
        """
        nested = patched_config_dir / "workspace" / "dated.md"
        nested.write_text("ours", encoding="utf-8")
        os.utime(nested, (1_000_000_000, 1_000_000_000))
        expected = time.localtime(1_000_000_000)[:6]

        zip_bytes, _ = create_export_zip()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            entry = next(i for i in zf.infolist() if i.filename.endswith("dated.md"))
        assert entry.date_time == expected

    def test_export_empty_kirocrew_dir(self, tmp_path):
        mc = tmp_path / "empty_mc"
        mc.mkdir()
        with patch("kiro_crew.portability.config_dir", return_value=mc):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(mc)}):
                zip_bytes, manifest = create_export_zip()
        assert len(zip_bytes) > 0
        assert manifest["contents"].get("workspace_files", 0) == 0


# ── Validate Tests ──


class TestTheArchivedBytesAreTheValidatedBytes:
    """Containment is decided on the DESCRIPTOR, and the archive reads that descriptor.

    A resolve-and-compare answers "where does this path point?" for an instant.
    `ZipFile.write` then opens the path AGAIN, so the file that was checked and the
    file that is read are two separate lookups, and a parent component retargeted in
    between is followed by the second one. A running gateway hands agent tools write
    access to the workspace while an export can be triggered, so that window is not
    theoretical — which is why a reviewer held the by-name-only version of this fix.

    `_open_verified` inverts the order — open first, then ask the kernel where the open
    thing actually is — and `_add_from_fd` streams from that descriptor. The property
    under test is not "the check is stricter" but "the check and the use address one
    object".
    """

    def test_a_file_reached_through_a_directory_link_gets_no_descriptor(
        self, patched_config_dir, tmp_path
    ):
        outside = tmp_path / "outside-the-crew-dir"
        outside.mkdir()
        (outside / "not-ours.md").write_text("private notes", encoding="utf-8")
        link = patched_config_dir / "workspace" / "linked"
        make_dir_link(link, outside)

        candidate = link / "not-ours.md"
        # Guard the guard: the lexical path IS inside the crew dir, so a check
        # that only read the name would accept it. That is the whole point.
        assert candidate.is_file()
        assert patched_config_dir in candidate.parents

        root = os.path.realpath(patched_config_dir)
        assert portability._open_verified(str(candidate), root) is None

    def test_a_real_file_inside_the_crew_dir_gets_a_descriptor_on_its_own_bytes(
        self, patched_config_dir
    ):
        """Positive control, and the one that catches an over-tight check.

        `fd_real_path` and `realpath` reach the same name by different kernel
        routes, and on Windows a path can also come back in 8.3 short form — a
        comparison that agrees only by luck must fail here, not in production.
        """
        nested = patched_config_dir / "workspace" / "deep" / "keep.md"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text("ours", encoding="utf-8")

        fd = portability._open_verified(
            str(nested), os.path.realpath(patched_config_dir)
        )
        assert fd is not None
        try:
            assert os.read(fd, 64) == b"ours"
        finally:
            os.close(fd)

    def test_retargeting_the_link_after_validation_cannot_change_what_is_archived(
        self, tmp_path
    ):
        """The race itself: a link accepted at validation is then retargeted out.

        The link resolves INSIDE the crew directory when the candidate is
        validated, so containment accepts it — correctly. It is then retargeted
        before the archive step. A build that re-opens the path packages the
        attacker's file; a build that streams the validated descriptor packages the
        bytes it checked.

        The link IS the crew root here rather than a directory under it, and the
        placement is the point rather than a convenience. The walk refuses a linked
        component BELOW the root — it is classified from the directory listing and
        never descended, opened or resolved — so a link there is not a
        reachable swap site at all. The root
        itself and everything above it are deliberately outside the screen — that
        is configuration, not a workspace an agent tool can write into — which
        makes it exactly where a swap can still land, and running the race here
        keeps the descriptor property covered on both platforms rather than on
        POSIX alone.

        Deterministic on purpose: the swap is placed exactly where a racing writer
        would land, rather than run concurrently and hoped for.
        """
        genuine = tmp_path / "genuine-home"
        (genuine / "workspace").mkdir(parents=True)
        (genuine / "workspace" / "note.md").write_text("ours", encoding="utf-8")
        outside = tmp_path / "outside-the-crew-dir"
        (outside / "workspace").mkdir(parents=True)
        (outside / "workspace" / "note.md").write_text("private notes", encoding="utf-8")

        link = tmp_path / "kirocrew-home"
        make_dir_link(link, genuine)
        candidate = link / "workspace" / "note.md"

        # Resolved ONCE before the window, exactly as the export hoists `mc_real`
        # out of its walk -- a root re-resolved after the swap would agree with
        # the attacker's tree and prove nothing.
        fd = portability._open_verified(str(candidate), os.path.realpath(link))
        assert fd is not None, "the contained candidate was refused; nothing under test"
        buf = io.BytesIO()
        try:
            # The window. Detaching the link is not the same call on both
            # platforms and getting it wrong fails the whole test: a POSIX
            # directory SYMLINK is a link entry, so `rmdir` answers
            # `NotADirectoryError` and only `unlink` removes it, while a Windows
            # JUNCTION is a real directory entry that `unlink` refuses. Neither
            # call touches what the link points at.
            _detach_dir_link(link)
            make_dir_link(link, outside)
            assert candidate.read_text(encoding="utf-8") == "private notes", (
                "the swap did not take effect, so the race was never simulated"
            )

            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                portability._add_from_fd(zf, fd, "export/note.md")
        finally:
            os.close(fd)

        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
            assert zf.read("export/note.md") == b"ours"

    def test_containment_fails_closed_when_the_real_path_is_unknowable(
        self, patched_config_dir, monkeypatch
    ):
        """A host whose kernel route for "where is this fd" is unavailable gets a
        refusal, never a fallback to the pathname the descriptor was opened by."""
        nested = patched_config_dir / "workspace" / "keep.md"
        nested.write_text("ours", encoding="utf-8")
        root = os.path.realpath(patched_config_dir)
        fd = portability._open_verified(str(nested), root)
        assert fd is not None
        os.close(fd)

        monkeypatch.setattr(portability.pinned_fs, "fd_real_path", lambda _fd: None)
        assert portability._open_verified(str(nested), root) is None

    def test_a_sensitive_target_is_refused_on_the_descriptor(
        self, patched_config_dir, monkeypatch
    ):
        """`is_sensitive_path` runs again on the fd's real path, not only the name.

        Re-running it there is what keeps the protected-location rule from being
        the one filter the swap defeats: it is asked about the inode that will
        actually be read.
        """
        nested = patched_config_dir / "workspace" / "keep.md"
        nested.write_text("ours", encoding="utf-8")
        root = os.path.realpath(patched_config_dir)
        real = os.path.realpath(nested)

        seen: list[str] = []

        def _sensitive(path: str, base_dir: str | None = None) -> bool:
            seen.append(path)
            return os.path.normcase(path) == os.path.normcase(real)

        monkeypatch.setattr(portability, "is_sensitive_path", _sensitive)
        assert portability._open_verified(str(nested), root) is None
        assert seen == [real], f"the gate was not asked about the fd's real path: {seen}"

    def test_a_hardlinked_alias_gets_no_descriptor(self, patched_config_dir, tmp_path):
        """A hardlink is the one alias no path-based guard can see.

        It shares the target's inode, so there is no symlink for `O_NOFOLLOW` to
        refuse, `is_symlink()` is False, and `fd_real_path` reports the path the
        descriptor was OPENED BY — the innocent workspace name — not a canonical
        one. `is_sensitive_path` is therefore asked about the alias and answers
        "not sensitive" while the bytes behind it are the target's.

        Only the link COUNT, read off the descriptor, sees it. Same rule as
        `pinned_fs.refuse_hardlink_alias` and `hooks.safe_read_file_bytes_nolink`.
        """
        target = tmp_path / "outside-the-crew-dir" / "credentials"
        target.parent.mkdir()
        target.write_text("aws_secret_access_key = hunter2", encoding="utf-8")
        alias = patched_config_dir / "workspace" / "innocent.txt"
        try:
            os.link(target, alias)
        except (OSError, NotImplementedError) as exc:  # pragma: no cover
            pytest.skip(f"this filesystem cannot create a hardlink: {exc}")

        root = os.path.realpath(patched_config_dir)
        # Guard the guard, through oracles OUTSIDE the module under test: every
        # path-based check the export had ACCEPTS this alias, and it really does
        # carry the target's bytes. Without this the test could pass because the
        # alias was refused for some unrelated reason.
        assert not alias.is_symlink()
        assert os.path.realpath(alias) == str(alias)
        assert not is_sensitive_path(str(alias))
        assert alias.read_text(encoding="utf-8").startswith("aws_secret")
        assert os.stat(alias).st_nlink == 2

        assert portability._open_verified(str(alias), root) is None

    def test_an_ordinary_single_link_file_is_still_accepted(self, patched_config_dir):
        """Negative control for the link-count rule: one name is the normal case,
        and refusing it would empty every export."""
        ordinary = patched_config_dir / "workspace" / "ordinary.txt"
        ordinary.write_text("ours", encoding="utf-8")
        assert os.stat(ordinary).st_nlink == 1

        fd = portability._open_verified(
            str(ordinary), os.path.realpath(patched_config_dir)
        )
        assert fd is not None
        os.close(fd)

    def test_a_streamed_entry_larger_than_the_zip64_limit_still_exports(
        self, patched_config_dir, monkeypatch
    ):
        """A streamed entry does not know its size when the header is written.

        `ZipFile.write` stat'd the source and enabled ZIP64 on its own; hand-building
        the entry gave that up, and a source over `ZIP64_LIMIT` then raises
        `RuntimeError` part-way through — the export endpoint answers 500 for a file
        that would otherwise archive fine.

        The limit is lowered rather than the fixture inflated: the branch under test
        is selected by `size > ZIP64_LIMIT`, and a multi-gigabyte artifact in the
        suite would buy nothing but minutes.
        """
        monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1024)
        big = patched_config_dir / "workspace" / "big.bin"
        payload = b"x" * 4096
        big.write_bytes(payload)

        zip_bytes, _ = create_export_zip()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert zf.read(
                next(n for n in zf.namelist() if n.endswith("workspace/big.bin"))
            ) == payload

    def test_the_zip64_guard_can_actually_fail(self, patched_config_dir, monkeypatch):
        """Guard the guard: prove the lowered limit really selects the ZIP64 branch.

        Without this, `..._still_exports` would pass just as happily on a build that
        never crosses the limit at all, and the fix it pins would be untested.
        """
        monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1024)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("e/big.bin", date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zf.compression
            # Matched case-insensitively because CPython's wording for this branch
            # is not stable across the versions this project supports: 3.10 raises
            # "File size unexpectedly exceeded ZIP64 limit" and 3.12 raises "File
            # size too large, try using force_zip64". `zip64` is the one token both
            # spellings share, so this stays specific to the ZIP64 branch without
            # pinning a message the stdlib is free to reword again.
            with pytest.raises(RuntimeError, match="(?i)zip64"):
                with zf.open(info, "w") as dest:  # the call WITHOUT force_zip64
                    dest.write(b"x" * 4096)

    def test_a_directory_never_yields_a_descriptor(self, patched_config_dir):
        """`_add_from_fd` streams bytes; a non-regular source must be refused before
        it reaches that, not turned into an unreadable archive entry."""
        adir = patched_config_dir / "workspace" / "adir"
        adir.mkdir(parents=True, exist_ok=True)
        assert (
            portability._open_verified(str(adir), os.path.realpath(patched_config_dir))
            is None
        )


pins_are_a_windows_property = pytest.mark.skipif(
    not platform_compat.IS_WINDOWS,
    reason=(
        "the walk pins on both platforms, but the ANTI-RENAME half is a Windows "
        "share-mode property -- a handle opened without FILE_SHARE_DELETE blocks "
        "rename/delete, and POSIX has no equivalent to hold a name still"
    ),
)


class TestTheAncestorSwapIsRefusedNotDetected:
    """The names are HELD, not inspected — which is a different kind of guarantee.

    Any check of a pathname is a check-to-open window: an adversary that can plant
    a link chooses when to plant it, so "we looked and it was fine" says nothing
    about the open that follows. On Windows that window is not merely a wrong
    answer, it is an outbound SMB authentication — resolving a reparse point aimed
    at a UNC share IS the probe — so a refusal computed afterwards has already paid
    the cost it exists to prevent.

    `platform_compat.pin_directory` removes the window instead of narrowing it. The
    handle omits `FILE_SHARE_DELETE`, so while it lives the directory can be neither
    renamed nor deleted; and the open refuses to follow a reparse point, so a
    junction already at the name fails there rather than being traversed. Pinning
    each component before naming the next means the only path the kernel walks to
    reach component *n* runs through components already opened and verified — the
    pattern `aws_control/backend/storage_staging.py` already uses for the path a sandboxed
    CLI writes through.

    These tests do not assert that the mechanism is present. They run the attacker
    at the exact instant the old code was vulnerable and assert the operating system
    refused them.
    """

    @pins_are_a_windows_property
    def test_a_racing_writer_cannot_swap_a_pinned_ancestor(
        self, patched_config_dir, tmp_path, monkeypatch
    ):
        """The blocking finding's own scenario, executed rather than described.

        "Writable directory swapped to a UNC reparse point after inspection." The
        swap is performed from inside `pin_directory`, immediately after the
        component it targets has been verified and pinned and before the next
        filesystem call — the precise instant a real racing writer would aim for,
        rather than a thread that has to be lucky.

        Two things are asserted, and both matter. The rename must FAIL, because that
        is the property: the export does not depend on nobody having swapped the
        directory, it depends on nobody being able to. And the descriptor must still
        come back on the genuine bytes, because a guard that closed the race by
        refusing everything would pass the first assertion and be useless.
        """
        nested = patched_config_dir / "workspace" / "deep"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "keep.md").write_text("ours", encoding="utf-8")
        outside = tmp_path / "outside-the-crew-dir"
        outside.mkdir()

        workspace = os.path.realpath(patched_config_dir / "workspace")
        attempts: list[str] = []
        real_pin = platform_compat.pin_directory

        def _pin_then_race(path):
            fd = real_pin(path)
            if os.path.normcase(os.fspath(path)) == os.path.normcase(workspace):
                # The racing writer, at the worst possible moment: the component is
                # verified, the walk is about to name what is under it.
                try:
                    os.rename(workspace, str(tmp_path / "carried-off"))
                    attempts.append("SWAPPED")
                except OSError as exc:
                    attempts.append(f"refused:{type(exc).__name__}")
            return fd

        monkeypatch.setattr(platform_compat, "pin_directory", _pin_then_race)
        found = list(
            portability._walk_contained(
                os.path.realpath(patched_config_dir),
                PurePath("workspace"),
                lambda _rel: True,
            )
        )
        try:
            assert attempts and attempts[0].startswith("refused:"), (
                f"the racing writer swapped a verified ancestor: {attempts}"
            )
            assert found, "the guard refused every legitimate file; nothing proven"
            fd = next(fd for rel, fd in found if rel.name == "keep.md")
            assert os.read(fd, 64) == b"ours"
        finally:
            for _rel, fd in found:
                with contextlib.suppress(OSError):
                    os.close(fd)

    def test_the_same_rename_succeeds_once_nothing_is_pinned(
        self, patched_config_dir, tmp_path
    ):
        """Guard the guard: prove the refusal above is the pin and not the filesystem.

        Without this, the test above would pass just as happily on a host where that
        rename could never have worked, and would be pinning nothing at all.
        """
        workspace = patched_config_dir / "workspace"
        carried_off = tmp_path / "carried-off"
        os.rename(workspace, carried_off)
        try:
            assert not workspace.exists()
        finally:
            os.rename(carried_off, workspace)

    def test_a_reparse_point_at_the_leaf_is_refused_without_being_followed(
        self, patched_config_dir, tmp_path
    ):
        """The final component gets the same treatment, in one operation.

        Windows has no `O_NOFOLLOW`, so `os.open` FOLLOWS a reparse point at the
        name — the guard-the-guard below proves it on the very path under test, so
        the refusal cannot be mistaken for the link being unopenable. The refusal
        has to come from the open itself rather than from a check before it, or it
        is the same window again.
        """
        outside = tmp_path / "outside-the-crew-dir"
        outside.mkdir()
        (outside / "marker.txt").write_text("private notes", encoding="utf-8")
        leaf = patched_config_dir / "workspace" / "leaf-link"
        make_dir_link(leaf, outside)

        with pytest.raises(OSError):
            fd = platform_compat.open_file_no_reparse(leaf)
            os.close(fd)  # pragma: no cover - only runs if the refusal regressed

        # Guard the guard: this exact name really is traversable by an ordinary
        # open, so the refusal above is the no-follow flag doing its job.
        fd = os.open(str(leaf / "marker.txt"), os.O_RDONLY)
        try:
            assert os.read(fd, 32) == b"private notes"
        finally:
            os.close(fd)

    def test_an_ordinary_file_still_opens_through_the_no_follow_open(
        self, patched_config_dir
    ):
        """Negative control for the leaf open: it must still return usable bytes.

        The descriptor is also the one `_add_from_fd` streams from, so it has to
        support the same operations the previous `os.open` descriptor did — on
        Windows it is now a `CreateFileW` handle wrapped in a CRT descriptor, which
        is exactly the kind of difference that passes a shallow test and breaks the
        archive.
        """
        ordinary = patched_config_dir / "workspace" / "ordinary.md"
        ordinary.write_text("ours", encoding="utf-8")

        fd = platform_compat.open_file_no_reparse(ordinary)
        try:
            st = os.fstat(fd)
            assert st.st_nlink == 1  # the hardlink rule reads this off the handle
            os.lseek(fd, 0, os.SEEK_SET)  # `_add_from_fd` rewinds before streaming
            with os.fdopen(os.dup(fd), "rb", closefd=True) as src:
                assert src.read() == b"ours"
        finally:
            os.close(fd)

    @pins_are_a_windows_property
    def test_every_pin_is_released_even_when_a_component_refuses(
        self, patched_config_dir, tmp_path, monkeypatch
    ):
        """A handle leak here would exhaust an export, not just look untidy.

        `_pin_ancestors` holds one descriptor per directory for every file the walk
        offers, so an unbalanced open on the refusal path would run a real export
        out of handles part-way through — a failure that would only show up on a
        large workspace.
        """
        nested = patched_config_dir / "workspace" / "deep"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "keep.md").write_text("ours", encoding="utf-8")

        opened: list[int] = []
        closed: list[int] = []
        real_pin = platform_compat.pin_directory
        real_close = os.close

        def _counting_pin(path):
            if len(opened) == 2:  # refuse the third component, mid-chain
                raise NotADirectoryError(errno.ENOTDIR, "planted refusal", str(path))
            fd = real_pin(path)
            opened.append(fd)
            return fd

        def _counting_close(fd):
            closed.append(fd)
            return real_close(fd)

        monkeypatch.setattr(platform_compat, "pin_directory", _counting_pin)
        monkeypatch.setattr(portability.os, "close", _counting_close)
        assert (
            list(
                portability._walk_contained(
                    os.path.realpath(patched_config_dir),
                    PurePath("workspace"),
                    lambda _rel: True,
                )
            )
            == []
        )
        assert opened, "no component was pinned, so nothing was under test"
        assert set(opened) <= set(closed), (
            f"pinned descriptors leaked: opened={opened} closed={closed}"
        )


#: Calls that RESOLVE the pathname handed to them, i.e. follow a reparse point at
#: the final component. On Windows each one IS the outbound SMB authentication
#: when that reparse point aims at a UNC share, which is why the property under
#: test is about the calls and not about the archive.
#:
#: ``os.lstat`` and ``os.path.islink`` are deliberately absent: they do not follow
#: the final component, so naming the link with one of them traverses nothing. The
#: recorder still flags ANY call naming a path BELOW the link, whatever the call
#: is, because reaching one at all means the link was traversed.
_RESOLVING_CALLS = (
    (os, "stat", "os.stat"),
    (os, "open", "os.open"),
    (os, "listdir", "os.listdir"),
    (os, "scandir", "os.scandir"),
    (os.path, "realpath", "os.path.realpath"),
    (os.path, "isfile", "os.path.isfile"),
    (os.path, "isdir", "os.path.isdir"),
    (os.path, "exists", "os.path.exists"),
    (os.path, "getsize", "os.path.getsize"),
)

#: ``pathlib`` binds ``scandir`` at import time, so patching ``os.scandir`` does
#: not see ``rglob``'s own descent -- the single most important call to catch
#: here. An audit hook sees it wherever it was bound from. Hooks cannot be
#: removed once installed, so one is installed lazily for the whole process and
#: routes into whichever sink is active.
_audit_sink: list[tuple[str, str]] | None = None
_audit_installed = False


def _audit_probe(event: str, args: tuple) -> None:
    if _audit_sink is None or event not in ("os.scandir", "os.listdir"):
        return
    for arg in args:
        try:
            name = os.fspath(arg)
        except TypeError:
            continue
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        if isinstance(name, str):
            _audit_sink.append((event, name))
        return


@contextlib.contextmanager
def recording_probes_through(link: Path, monkeypatch):
    """Record every call that resolves *link*, or that names anything beneath it.

    Yields the list it fills. Emptiness is the assertion: a refusal computed after
    the probe went out is exactly the defect, so "nothing was archived" cannot be
    the oracle -- that was already true of the code this replaces.
    """
    global _audit_sink, _audit_installed
    seen: list[tuple[str, str]] = []
    prefix = os.path.normcase(str(link))

    def _record(label: str, target) -> None:
        try:
            name = os.fspath(target)
        except TypeError:
            return
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        if not isinstance(name, str):
            return
        normed = os.path.normcase(name)
        if normed == prefix or normed.startswith(prefix + os.sep):
            seen.append((label, name))

    for module, attr, label in _RESOLVING_CALLS:
        original = getattr(module, attr)

        def _spy(*args, __original=original, __label=label, **kwargs):
            if args:
                _record(__label, args[0])
            return __original(*args, **kwargs)

        monkeypatch.setattr(module, attr, _spy)

    # `os.path.realpath` reaches the filesystem through this, and a caller that
    # imported it directly would bypass the patch above.
    if hasattr(ntpath, "_getfinalpathname"):
        original_final = ntpath._getfinalpathname

        def _spy_final(*args, **kwargs):
            if args:
                _record("ntpath._getfinalpathname", args[0])
            return original_final(*args, **kwargs)

        monkeypatch.setattr(ntpath, "_getfinalpathname", _spy_final)

    if not _audit_installed:
        sys.addaudithook(_audit_probe)
        _audit_installed = True
    _audit_sink = []
    try:
        yield seen
    finally:
        for event, name in _audit_sink:
            _record(event, name)
        _audit_sink = None


class TestNothingIsProbedThroughAPlantedReparsePoint:
    """The walk must not TOUCH a planted junction, not merely refuse its bytes.

    This is the difference the exact-head blocking finding turned on. The previous
    build already archived nothing from behind a junction — containment worked —
    and still issued twelve resolving calls through it while deciding that:
    `rglob` descended it, then `is_file()` and `is_sensitive_path()` resolved what
    it yielded. If the junction names `\\\\host\\share`, those resolutions are an
    outbound SMB authentication that leaks this process's credentials to whoever
    planted it, and `return None` afterwards cannot recall them.

    So the assertion is on the CALLS, not on the archive. A test that only checked
    the archive passes on the vulnerable build — measured, not assumed.
    """

    @pytest.mark.skipif(
        not platform_compat.IS_WINDOWS,
        reason=(
            "the probe is a Windows reparse-point property: `rglob` descends a "
            "junction there, while `pathlib` does not descend a POSIX directory "
            "symlink, so the traversal under test cannot be staged on POSIX"
        ),
    )
    def test_a_pre_planted_junction_is_never_resolved_during_an_export(
        self, patched_config_dir, tmp_path, monkeypatch
    ):
        """Plant the junction BEFORE the export, then assert nothing reached it.

        Pre-planted rather than raced on purpose: the swap race is the previous
        finding and has its own test. This is the plant-and-wait case, which needs
        no timing at all — the attacker leaves the link in the workspace a gateway
        tool can write to and waits for an export to walk into it.
        """
        target = tmp_path / "never-touch-me"
        target.mkdir()
        (target / "loot.txt").write_text("private notes", encoding="utf-8")
        junction = patched_config_dir / "workspace" / "trap"
        make_dir_link(junction, target)
        genuine = patched_config_dir / "workspace" / "keep.md"
        genuine.write_text("ours", encoding="utf-8")

        # Guard the guard, through oracles OUTSIDE the module under test: this is
        # really a junction and not a symlink, which is the whole reason `rglob`
        # walked into it -- `is_symlink()` answers False because a junction's tag
        # is IO_REPARSE_TAG_MOUNT_POINT, not IO_REPARSE_TAG_SYMLINK -- and the far
        # side really is reachable by an ordinary resolving call.
        assert not junction.is_symlink()
        assert platform_compat.is_link_or_junction(junction)
        assert (junction / "loot.txt").read_text(encoding="utf-8") == "private notes"

        with recording_probes_through(junction, monkeypatch) as probed:
            zip_bytes, _ = create_export_zip()

        assert probed == [], (
            "the export resolved a path through the planted junction "
            f"({len(probed)} calls): {probed[:6]}"
        )
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
        # Negative control: the guard must not have "passed" by refusing the whole
        # tree, and the loot must still be out.
        assert any(n.endswith("workspace/keep.md") for n in names), names
        assert not any("loot.txt" in n for n in names), names

    @pytest.mark.skipif(
        not platform_compat.IS_WINDOWS,
        reason="the recorder is exercised against a Windows junction",
    )
    def test_the_recorder_actually_catches_a_probe(
        self, patched_config_dir, tmp_path, monkeypatch
    ):
        """Guard the guard: an oracle that can never fire proves nothing.

        Every call here is one the previous build made on this exact path, so this
        also pins WHY that build was vulnerable rather than asserting it in prose.
        """
        target = tmp_path / "never-touch-me"
        target.mkdir()
        (target / "loot.txt").write_text("private notes", encoding="utf-8")
        junction = patched_config_dir / "workspace" / "trap"
        make_dir_link(junction, target)

        with recording_probes_through(junction, monkeypatch) as probed:
            junction.is_file()  # what the old `if not fpath.is_file()` did
            os.path.realpath(junction)  # what `is_sensitive_path` did
            list((patched_config_dir / "workspace").rglob("*"))  # the descent

        labels = {label for label, _ in probed}
        assert "os.path.realpath" in labels, probed
        assert "os.scandir" in labels, (
            "the audit hook did not observe `rglob` descending the junction, so "
            f"the strongest half of the oracle is dead: {probed}"
        )


class TestALinkedCrewRootStillExports:
    def test_a_crew_root_reached_through_a_link_still_packages_its_workspace(
        self, fake_kirocrew_home, tmp_path, monkeypatch
    ):
        """The regression the screen's scoping exists to avoid.

        Screening with `first_linked_ancestor` as-is would refuse every candidate
        on a host whose `$KIROCREW_HOME` is a link -- a perfectly ordinary setup,
        and the export would come back EMPTY rather than safe. A silently empty
        backup is a worse outcome than the leak being fixed, so the boundary gets
        its own test rather than a comment.
        """
        (fake_kirocrew_home / "workspace" / "keep.md").write_text("ours", encoding="utf-8")
        linked_home = tmp_path / "linked-home"
        make_dir_link(linked_home, fake_kirocrew_home)
        monkeypatch.setenv("KIROCREW_HOME", str(linked_home))

        zip_bytes, _ = create_export_zip()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
        assert any(n.endswith("workspace/keep.md") for n in names), names


class TestValidate:
    def test_validate_valid_zip(self, patched_config_dir):
        zip_bytes, _ = create_export_zip()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        tmp.write(zip_bytes)
        tmp.close()
        try:
            ok, error, manifest = validate_import_zip(Path(tmp.name))
            assert ok is True
            assert error == ""
            assert manifest["version"] == portability.EXPORT_MANIFEST_VERSION
        finally:
            os.unlink(tmp.name)

    def test_validate_not_a_zip(self, tmp_path):
        bad = tmp_path / "notazip.zip"
        bad.write_text("this is not a zip file")
        ok, error, _ = validate_import_zip(bad)
        assert ok is False
        assert "Invalid zip" in error

    def test_validate_missing_manifest(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("some-dir/config.json", '{"agent":{}}')
        zip_path = tmp_path / "no_manifest.zip"
        zip_path.write_bytes(buf.getvalue())
        ok, error, _ = validate_import_zip(zip_path)
        assert ok is False
        assert "MANIFEST" in error

    def test_validate_bad_version(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("export/MANIFEST.json", json.dumps({"version": 99}))
        zip_path = tmp_path / "bad_version.zip"
        zip_path.write_bytes(buf.getvalue())
        ok, error, _ = validate_import_zip(zip_path)
        assert ok is False
        assert "version" in error.lower()

    def test_validate_path_traversal(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../../etc/passwd", "root:x:0:0")
            zf.writestr("export/MANIFEST.json", json.dumps({"version": 2}))
        zip_path = tmp_path / "traversal.zip"
        zip_path.write_bytes(buf.getvalue())
        ok, error, _ = validate_import_zip(zip_path)
        assert ok is False
        assert "traversal" in error.lower()

    def test_validate_absolute_path(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("/etc/shadow", "bad")
            zf.writestr("export/MANIFEST.json", json.dumps({"version": 2}))
        zip_path = tmp_path / "absolute.zip"
        zip_path.write_bytes(buf.getvalue())
        ok, error, _ = validate_import_zip(zip_path)
        assert ok is False
        assert "traversal" in error.lower()

    def test_validate_corrupt_manifest_json(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("export/MANIFEST.json", "not valid json {{{{")
        zip_path = tmp_path / "corrupt_manifest.zip"
        zip_path.write_bytes(buf.getvalue())
        ok, error, _ = validate_import_zip(zip_path)
        assert ok is False
        assert "manifest" in error.lower()


# ── Import Tests ──


class TestImportMerge:
    def _make_export(self, source_dir):
        """Export from source_dir and return zip path."""
        with patch("kiro_crew.portability.config_dir", return_value=source_dir):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(source_dir)}):
                zip_bytes, _ = create_export_zip()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        tmp.write(zip_bytes)
        tmp.close()
        return Path(tmp.name)

    def test_import_merge_into_empty(self, patched_config_dir, tmp_path):
        """Import into a fresh (empty) KiroCrew instance."""
        zip_path = self._make_export(patched_config_dir)
        try:
            # Target: empty directory
            target = tmp_path / "target_mc"
            target.mkdir()
            with patch("kiro_crew.portability.config_dir", return_value=target):
                with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                    summary = apply_import_zip(zip_path, mode="merge")
            assert len(summary["items"]) > 0
            # memory.db should be copied
            assert (target / "memory.db").is_file()
            # crons.json should be copied
            assert (target / "crons.json").is_file()
        finally:
            os.unlink(str(zip_path))

    def test_import_merge_deduplicates_crons(self, patched_config_dir, tmp_path):
        """Merging the same export twice doesn't duplicate cron jobs."""
        zip_path = self._make_export(patched_config_dir)
        try:
            target = tmp_path / "target_mc"
            target.mkdir()
            with patch("kiro_crew.portability.config_dir", return_value=target):
                with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                    apply_import_zip(zip_path, mode="merge")
                    # Import again — should not duplicate
                    apply_import_zip(zip_path, mode="merge")
            crons = json.loads((target / "crons.json").read_text(encoding="utf-8"))
            job_names = [j["name"] for j in crons["jobs"]]
            assert job_names.count("daily-check") == 1
        finally:
            os.unlink(str(zip_path))

    def test_import_merge_memory_db(self, patched_config_dir, tmp_path):
        """Merging memory.db inserts new rows without overwriting existing."""
        zip_path = self._make_export(patched_config_dir)
        try:
            # Create target with its own memory.db with different data
            target = tmp_path / "target_mc"
            target.mkdir()
            dst_db = target / "memory.db"
            conn = sqlite3.connect(str(dst_db))
            conn.execute("CREATE TABLE semantic_memory (key TEXT PRIMARY KEY, value_json TEXT, confidence REAL, source TEXT, created_at TEXT, updated_at TEXT, embedding BLOB, is_deleted INTEGER DEFAULT 0)")
            conn.execute("INSERT INTO semantic_memory (key, value_json, confidence, source, created_at, updated_at, is_deleted) VALUES ('user.team', '\"Platform\"', 0.95, 'agent', '2026-01-01', '2026-01-01', 0)")
            conn.execute("CREATE TABLE episodic_memories (id TEXT PRIMARY KEY, conversation_id TEXT, text TEXT, embedding BLOB, tags TEXT, importance REAL, created_at TEXT, last_accessed_at TEXT, is_deleted INTEGER DEFAULT 0)")
            conn.execute("CREATE TABLE knowledge_facts (subject TEXT, predicate TEXT, object TEXT, episode_id TEXT, created_at TEXT)")
            conn.execute("CREATE TABLE knowledge_edges (source_key TEXT, target_key TEXT, relation TEXT, weight REAL, metadata TEXT, created_at TEXT)")
            conn.commit()
            conn.close()

            with patch("kiro_crew.portability.config_dir", return_value=target):
                with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                    apply_import_zip(zip_path, mode="merge")

            # Both keys should exist
            conn = sqlite3.connect(str(dst_db))
            rows = conn.execute("SELECT key FROM semantic_memory ORDER BY key").fetchall()
            keys = [r[0] for r in rows]
            assert "user.name" in keys  # from import
            assert "user.team" in keys  # pre-existing
            conn.close()
        finally:
            os.unlink(str(zip_path))

    def test_import_merge_workspace_no_overwrite(self, patched_config_dir, tmp_path):
        """Merge doesn't overwrite existing workspace files."""
        zip_path = self._make_export(patched_config_dir)
        try:
            target = tmp_path / "target_mc"
            target.mkdir()
            # Create a pre-existing preferences file with different content
            mem_dir = target / "workspace" / "memory"
            mem_dir.mkdir(parents=True)
            (mem_dir / "preferences.md").write_text("# Existing prefs\n- Keep this\n")

            with patch("kiro_crew.portability.config_dir", return_value=target):
                with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                    apply_import_zip(zip_path, mode="merge")

            # Pre-existing file should NOT be overwritten
            content = (mem_dir / "preferences.md").read_text(encoding="utf-8")
            assert "Existing prefs" in content
            assert "Uses vim" not in content
        finally:
            os.unlink(str(zip_path))

    def test_import_merge_notifications(self, patched_config_dir, tmp_path):
        """Merge deduplicates notifications by timestamp."""
        zip_path = self._make_export(patched_config_dir)
        try:
            target = tmp_path / "target_mc"
            target.mkdir()
            # Pre-existing notification
            (target / "notifications.jsonl").write_text(
                json.dumps({"ts": "1700000000", "title": "existing"}) + "\n"
            )

            with patch("kiro_crew.portability.config_dir", return_value=target):
                with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                    apply_import_zip(zip_path, mode="merge")

            # Should still have only 1 entry (same ts)
            lines = [line for line in (target / "notifications.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            assert len(lines) == 1
        finally:
            os.unlink(str(zip_path))

    @requires_o_nofollow
    def test_import_merge_notifications_refuses_an_undecodable_record(
        self, patched_config_dir, tmp_path
    ):
        """The copy branch: no live file yet, so the merge branch never runs.

        ``apply_import_zip`` reports ``notifications (copied)`` in its summary and
        the dashboard handler turns that into ``ok: True``, so accepting the
        record here tells an API caller the import succeeded while the live
        reader -- which decodes the whole file inside one ``try`` and returns
        ``[]`` -- has lost every row it will ever load. The refusal therefore has
        to RAISE, and must leave no partially copied file behind.
        """
        (patched_config_dir / "notifications.jsonl").write_bytes(
            b'{"ts":"1700000001","title":"ok"}\n{"ts":"1700000002","title":"\xff"}\n'
        )
        zip_path = self._make_export(patched_config_dir)
        try:
            target = tmp_path / "target_mc"
            target.mkdir()
            assert not (target / "notifications.jsonl").exists()

            with patch("kiro_crew.portability.config_dir", return_value=target):
                with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                    with pytest.raises(UnreadableRecord):
                        apply_import_zip(zip_path, mode="merge")

            assert not (target / "notifications.jsonl").exists(), (
                "an unvalidated prefix was installed where the reader will find it"
            )
        finally:
            os.unlink(str(zip_path))

    def test_import_reports_the_platform_skip_instead_of_claiming_a_copy(
        self, patched_config_dir, tmp_path, monkeypatch
    ):
        """Where ``O_NOFOLLOW`` does not exist the import skips notifications and SAYS so.

        The summary is what the dashboard handler turns into a result for an API caller,
        so this is the one place the refusal could go silent: reporting
        ``notifications (copied)`` for a copy that did not happen, or omitting the item
        entirely, would be the same bug class this change removes -- telling a caller the
        import succeeded when the records are not there.

        The rest of the import must still complete. A missing platform primitive is not a
        reason to refuse the other components.
        """
        zip_path = self._make_export(patched_config_dir)
        try:
            target = tmp_path / "target_mc"
            target.mkdir()
            monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

            with patch("kiro_crew.portability.config_dir", return_value=target):
                with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                    summary = apply_import_zip(zip_path, mode="merge")

            items = summary["items"]
            notif = [i for i in items if i.startswith("notifications")]
            assert notif, f"the skip was not reported at all: {items}"
            assert "SKIPPED" in notif[0], notif[0]
            assert "O_NOFOLLOW" in notif[0], notif[0]
            assert not (target / "notifications.jsonl").exists()
            assert len(items) > 1, f"the whole import stopped on a platform refusal: {items}"
            # Flagged machine-readably so the handler logs the import as
            # partial, not a flat ok, over records that were not installed.
            assert "notifications" in (summary.get("refused_merges") or []), summary
        finally:
            os.unlink(str(zip_path))

    def test_import_merge_branch_platform_skip_is_recorded_as_refused(
        self, patched_config_dir, tmp_path, monkeypatch
    ):
        """The MERGE branch (a live file exists) also records the platform skip.

        With a live ``notifications.jsonl`` already at the target the import takes
        the merge branch, and on a platform without ``O_NOFOLLOW`` the merge
        raises ``NotificationCopyUnsupported``. That must degrade to a skip that
        is flagged in ``refused_merges`` -- not a silent ``ok`` over an import
        that merged zero records -- and must leave the live file untouched.
        """
        zip_path = self._make_export(patched_config_dir)
        try:
            target = tmp_path / "target_mc"
            target.mkdir()
            live = target / "notifications.jsonl"
            live.write_text(json.dumps({"ts": "1700000000", "title": "existing"}) + "\n")
            before = live.read_bytes()
            monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

            with patch("kiro_crew.portability.config_dir", return_value=target):
                with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                    summary = apply_import_zip(zip_path, mode="merge")

            notif = [i for i in summary["items"] if i.startswith("notifications")]
            assert notif and "SKIPPED" in notif[0], summary["items"]
            assert "notifications" in (summary.get("refused_merges") or []), summary
            assert live.read_bytes() == before, "a refused merge changed the live file"
        finally:
            os.unlink(str(zip_path))

    def test_import_merge_skills_no_overwrite(self, patched_config_dir, tmp_path):
        """Merge adds new skills but doesn't overwrite existing ones."""
        zip_path = self._make_export(patched_config_dir)
        try:
            target = tmp_path / "target_mc"
            target.mkdir()
            sk_dir = target / "skills" / "my-skill"
            sk_dir.mkdir(parents=True)
            (sk_dir / "SKILL.md").write_text("# Existing skill content\n")

            with patch("kiro_crew.portability.config_dir", return_value=target):
                with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                    apply_import_zip(zip_path, mode="merge")

            # Existing skill should NOT be overwritten
            content = (sk_dir / "SKILL.md").read_text(encoding="utf-8")
            assert "Existing skill content" in content
        finally:
            os.unlink(str(zip_path))


class TestImportReplace:
    def _make_export(self, source_dir):
        with patch("kiro_crew.portability.config_dir", return_value=source_dir):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(source_dir)}):
                zip_bytes, _ = create_export_zip()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        tmp.write(zip_bytes)
        tmp.close()
        return Path(tmp.name)

    def test_import_replace_overwrites(self, patched_config_dir, tmp_path):
        """Replace mode overwrites existing files."""
        zip_path = self._make_export(patched_config_dir)
        try:
            target = tmp_path / "target_mc"
            target.mkdir()
            # Pre-existing config with different content
            (target / "config.json").write_text(json.dumps({"agent": {"provider": "bedrock"}}))

            with patch("kiro_crew.portability.config_dir", return_value=target):
                with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                    apply_import_zip(zip_path, mode="replace")

            # Config should be replaced
            data = json.loads((target / "config.json").read_text(encoding="utf-8"))
            assert data["agent"]["provider"] == "acp"
        finally:
            os.unlink(str(zip_path))

    def test_a_junction_at_skills_auto_is_not_rmtreed_through(self, patched_config_dir, tmp_path):
        """Replace mode strips ``skills/auto`` before copying the tree in.

        The strip was a bare ``auto_dir.is_dir()`` guard on a ``shutil.rmtree``.
        A directory JUNCTION answers ``is_dir()`` True and ``is_symlink()`` False,
        and ``rmtree`` follows one into its target -- so a junction planted at
        ``skills/auto`` in the extraction tree aimed the delete OUTSIDE the archive.
        ``is_link_or_junction`` refuses it: the LINK is unlinked, its target
        untouched. ``make_dir_link`` plants a real junction on Windows and a
        directory symlink on POSIX, so the arm the defect lived in is exercised.

        The junction is planted in ``_strip_host_local_store_state``, which runs on
        the extracted snapshot immediately before the replace branch reaches the
        ``skills/auto`` strip -- the only in-flight seam, since a zip cannot carry a
        reparse point.
        """
        zip_path = self._make_export(patched_config_dir)
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "precious.txt").write_text("not the import's to delete", encoding="utf-8")
        try:
            target = tmp_path / "target_mc"
            target.mkdir()

            real_strip = portability._strip_host_local_store_state

            def _plant_then_strip(snap: Path) -> None:
                auto_dir = snap / "skills" / "auto"
                auto_dir.parent.mkdir(parents=True, exist_ok=True)
                make_dir_link(auto_dir, victim)
                real_strip(snap)

            with patch("kiro_crew.portability.config_dir", return_value=target):
                with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                    with patch.object(
                        portability, "_strip_host_local_store_state", _plant_then_strip
                    ):
                        apply_import_zip(zip_path, mode="replace")

            # The rmtree never followed the junction into ``victim``.
            assert (victim / "precious.txt").read_text(encoding="utf-8") == (
                "not the import's to delete"
            )
            # And the replace still landed (the import was not aborted by the link).
            assert (target / "config.json").is_file()
        finally:
            os.unlink(str(zip_path))


class TestCrewTemplateWarnings:
    """A bundle never carries ``<kiro home>/agents``: both ends name what that leaves out."""

    @staticmethod
    def _with_crews(mc: Path, crews: dict) -> None:
        cfg = json.loads((mc / "config.json").read_text(encoding="utf-8"))
        cfg["agents"] = crews
        (mc / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    @staticmethod
    def _agents_dir(tmp_path: Path) -> Path:
        agents = tmp_path / "kiro_agents"
        agents.mkdir()
        (agents / "by-stem.json").write_text(json.dumps({"name": "by-stem"}))
        (agents / "file.json").write_text(json.dumps({"name": "by-declared-name"}))
        return agents

    def test_refs_skip_rows_with_no_template_and_tolerate_a_bad_file(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"agents": {"b": {"kiro_agent": "t2"}, "a": {"kiro_agent": "t1"},
                                   "c": {"kiro_agent": ""}, "d": "not-a-row",
                                   "m": {"kiro_agent": "kirocrew"},
                                   "w": {"kiro_agent": "kirocrew-worker"},
                                   "l": {"kiro_agent": "kirocrew-lite"}}})
        )
        assert portability.crew_template_refs(cfg) == [("a", "t1"), ("b", "t2")]
        cfg.write_text(json.dumps({
            "agent": {"default_agent": "d1"},
            "session": {"pool_agent": "p1"},
            "agents": {"a": {"kiro_agent": "t1"}},
        }))
        assert portability.crew_template_refs(cfg) == [
            ("a", "t1"), ("agent.default_agent", "d1"), ("session.pool_agent", "p1")
        ]
        cfg.write_text("[not an object")
        assert portability.crew_template_refs(cfg) == []
        cfg.write_text("[" * 100_000 + "]" * 100_000)
        assert portability.crew_template_refs(cfg) == []
        assert portability.crew_template_refs(tmp_path / "absent.json") == []

    def test_missing_matches_by_stem_or_declared_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(portability, "kiro_agents_dir", lambda: self._agents_dir(tmp_path))
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"agents": {
            "stem": {"kiro_agent": "by-stem"},
            "named": {"kiro_agent": "by-declared-name"},
            "gone": {"kiro_agent": "only-on-the-source"},
        }}))
        assert portability.missing_crew_templates(cfg) == (
            [{"crew": "gone", "kiro_agent": "only-on-the-source"}],
            0,
        )

    def test_export_names_every_template_the_crews_use(self, patched_config_dir):
        self._with_crews(patched_config_dir, {
            "x": {"kiro_agent": "shared"}, "y": {"kiro_agent": "shared"}, "z": {"kiro_agent": "solo"},
        })
        assert portability.unbundled_agent_templates() == (["shared", "solo"], 0)

    def test_both_lists_are_bounded_in_count_and_length(
        self, patched_config_dir, tmp_path, monkeypatch
    ):
        limit = portability.MAX_TEMPLATE_WARNINGS
        crews = {f"c{i:03}": {"kiro_agent": f"t{i:03}"} for i in range(limit + 7)}
        crews["long"] = {"kiro_agent": "a" + "x" * 5000}
        self._with_crews(patched_config_dir, crews)
        names, more = portability.unbundled_agent_templates()
        assert len(names) == limit and more == 8
        assert all(len(n) <= portability.MAX_TEMPLATE_NAME_CHARS for n in names)
        assert names[0].endswith("\u2026")
        monkeypatch.setattr(portability, "kiro_agents_dir", lambda: self._agents_dir(tmp_path))
        rows, more = portability.missing_crew_templates(patched_config_dir / "config.json")
        assert len(rows) == limit and more == 8
        assert all(len(r["kiro_agent"]) <= portability.MAX_TEMPLATE_NAME_CHARS for r in rows)

    @pytest.mark.parametrize("mode", ["merge", "replace"])
    def test_import_warns_about_the_missing_template_and_still_imports(
        self, patched_config_dir, tmp_path, monkeypatch, mode
    ):
        self._with_crews(patched_config_dir, {
            "ok": {"kiro_agent": "by-stem"}, "broken": {"kiro_agent": "only-on-the-source"},
        })
        zip_path = TestImportMerge()._make_export(patched_config_dir)
        monkeypatch.setattr(portability, "kiro_agents_dir", lambda: self._agents_dir(tmp_path))
        target = tmp_path / "target_mc"
        target.mkdir()
        try:
            with patch("kiro_crew.portability.config_dir", return_value=target):
                with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                    summary = apply_import_zip(zip_path, mode=mode)
        finally:
            os.unlink(str(zip_path))
        assert summary["missing_agent_templates"] == [
            {"crew": "broken", "kiro_agent": "only-on-the-source"}
        ]
        crews = json.loads((target / "config.json").read_text(encoding="utf-8"))["agents"]
        assert set(crews) == {"ok", "broken"}

    def test_an_unreadable_spec_costs_the_warning_not_the_import(
        self, patched_config_dir, tmp_path, monkeypatch
    ):
        self._with_crews(patched_config_dir, {"broken": {"kiro_agent": "only-on-the-source"}})
        zip_path = TestImportMerge()._make_export(patched_config_dir)

        def deep(*_a, **_k):
            raise RecursionError("nested spec")

        monkeypatch.setattr(portability, "parsed_agent_specs", deep)
        target = tmp_path / "target_mc"
        target.mkdir()
        try:
            with patch("kiro_crew.portability.config_dir", return_value=target):
                with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                    summary = apply_import_zip(zip_path, mode="merge")
        finally:
            os.unlink(str(zip_path))
        assert "missing_agent_templates" not in summary
        assert "broken" in json.loads((target / "config.json").read_text(encoding="utf-8"))["agents"]

    def test_import_with_every_template_present_adds_no_warning(
        self, patched_config_dir, tmp_path, monkeypatch
    ):
        self._with_crews(patched_config_dir, {"ok": {"kiro_agent": "by-declared-name"}})
        zip_path = TestImportMerge()._make_export(patched_config_dir)
        monkeypatch.setattr(portability, "kiro_agents_dir", lambda: self._agents_dir(tmp_path))
        target = tmp_path / "target_mc"
        target.mkdir()
        try:
            with patch("kiro_crew.portability.config_dir", return_value=target):
                with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                    summary = apply_import_zip(zip_path, mode="merge")
        finally:
            os.unlink(str(zip_path))
        assert "missing_agent_templates" not in summary
