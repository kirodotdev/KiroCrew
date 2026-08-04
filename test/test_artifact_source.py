"""Tests for :mod:`kiro_crew.artifact_source` — the copy-vs-link decision.

The decision is ORDERED, and the order is the whole point: a disposable root
wins over a git repository (a throwaway clone in the temp dir is still
throwaway), and a caller-supplied project root wins over repo detection.
Each branch gets its own test so a reordering can't pass silently.

Fixtures live under the real temp dir, which is itself a disposable root — so
tests that need a NON-disposable directory move the ``_tempdir`` seam to a
narrow subdirectory instead of trying to work around it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import artifact_source
from kiro_crew.artifact_source import (
    COPY,
    LINK,
    classify_source,
    disposable_roots,
    is_verifiable_root,
    project_root_marker,
)


@pytest.fixture
def narrow_tempdir(tmp_path: Path, monkeypatch) -> Path:
    """Point the disposable temp root at ``tmp_path/tmp`` only.

    Lets the rest of ``tmp_path`` act as ordinary (non-disposable) filesystem
    so the link branches are reachable in a test.
    """
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(artifact_source, "_tempdir", lambda: str(tmp))
    return tmp


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    """A home dir under tmp_path, so ~/Downloads and ~/Desktop are testable."""
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr(artifact_source, "_home", lambda: str(home))
    return home


def _file(path: Path, body: str = "x") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return str(path)


class TestClassifySourceGuards:
    """Branch 0 — anything unusable is a copy, never a link."""

    @pytest.mark.parametrize("bad", ["", None, 123, [], {}])
    def test_non_string_or_empty_is_copy(self, bad) -> None:
        assert classify_source(bad) == (COPY, "")

    def test_relative_path_is_copy(self, tmp_path: Path, monkeypatch) -> None:
        # A relative path has no meaning once the artifact outlives the cwd.
        monkeypatch.chdir(tmp_path)
        _file(tmp_path / "notes.md")
        assert classify_source("notes.md") == (COPY, "")

    def test_nonexistent_path_is_copy(self, narrow_tempdir, tmp_path: Path) -> None:
        # Even inside a git repo: we will not record a pointer we cannot read.
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        assert classify_source(str(tmp_path / "repo" / "gone.md")) == (COPY, "")

    def test_directory_is_copy(self, narrow_tempdir, tmp_path: Path) -> None:
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        assert classify_source(str(tmp_path / "repo")) == (COPY, "")

    def test_sensitive_path_is_copy(self, narrow_tempdir, tmp_path: Path, monkeypatch) -> None:
        target = _file(tmp_path / "repo" / "creds")
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        monkeypatch.setattr(
            artifact_source, "is_sensitive_path", lambda p: p == os.path.realpath(target)
        )
        assert classify_source(target) == (COPY, "")

    def test_symlink_into_sensitive_is_copy(
        self, narrow_tempdir, tmp_path: Path, monkeypatch
    ) -> None:
        # The sensitive check runs on the RESOLVED path, so a benign-looking
        # link inside a repo cannot smuggle a pointer to a secret.
        secret = _file(tmp_path / "vault" / "id_rsa")
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        link = tmp_path / "repo" / "innocent.md"
        link.symlink_to(secret)
        monkeypatch.setattr(
            artifact_source, "is_sensitive_path", lambda p: p == os.path.realpath(secret)
        )
        assert classify_source(str(link)) == (COPY, "")


class TestClassifySourceDisposableFirst:
    """Branch 1 — disposable roots beat every link rule."""

    def test_tempdir_file_is_copy(self, narrow_tempdir: Path) -> None:
        assert classify_source(_file(narrow_tempdir / "scratch.md")) == (COPY, "")

    def test_git_repo_inside_tempdir_is_still_copy(self, narrow_tempdir: Path) -> None:
        # THE ordering test: /tmp/scratch can contain a .git, and a throwaway
        # clone is still throwaway. Disposable MUST be evaluated first.
        repo = narrow_tempdir / "scratch"
        (repo / ".git").mkdir(parents=True)
        assert classify_source(_file(repo / "doc.md")) == (COPY, "")

    def test_downloads_is_copy(self, narrow_tempdir, fake_home: Path) -> None:
        assert classify_source(_file(fake_home / "Downloads" / "spec.md")) == (COPY, "")

    def test_desktop_is_copy(self, narrow_tempdir, fake_home: Path) -> None:
        assert classify_source(_file(fake_home / "Desktop" / "notes.md")) == (COPY, "")

    def test_disposable_roots_are_canonical(self, narrow_tempdir, fake_home: Path) -> None:
        roots = disposable_roots()
        assert len(roots) == 3
        assert all(os.path.isabs(r) for r in roots)


class TestNoExternalRootNomination:
    """Nothing outside the filesystem itself can nominate an authorizing root.

    A caller-supplied project-root branch used to live here. It was removed: the
    recents list it drew on lives in the agent-writable data home, so an agent
    could write ["/"] into it and then forge an artifact naming source_root="/",
    turning re-verification into a rubber stamp for its own input. A LINK is now
    granted only by an observable git repository root.
    """

    def test_plain_directory_outside_home_copies(self, narrow_tempdir, tmp_path: Path) -> None:
        # A real project tree that is NOT a repo: copying is a working
        # capability, unlike a link that degrades to a stale snapshot.
        proj = tmp_path / "workplace" / "nrb" / "notes"
        assert classify_source(_file(proj / "spec.md")) == (COPY, "")

    def test_repo_outside_home_links_with_its_root(self, narrow_tempdir, tmp_path: Path) -> None:
        # The headline case: /workplace/... is outside $HOME, which is exactly
        # where linking was silently broken.
        repo = tmp_path / "workplace" / "nrb" / "repo"
        (repo / ".git").mkdir(parents=True)
        target = _file(repo / "docs" / "spec.md")
        assert classify_source(target) == (LINK, str(repo))

    def test_nearest_repo_root_wins(self, narrow_tempdir, tmp_path: Path) -> None:
        outer = tmp_path / "outer"
        (outer / ".git").mkdir(parents=True)
        inner = outer / "vendored"
        (inner / ".git").mkdir(parents=True)
        target = _file(inner / "doc.md")
        assert classify_source(target) == (LINK, str(inner))


class TestNonGitProjectsLink:
    """Not every project is a git repository, so ``.git`` alone is too narrow."""

    @pytest.mark.parametrize(
        "marker",
        ["pyproject.toml", "package.json", "Cargo.toml", "go.mod", "Makefile", ".kiro"],
    )
    def test_marker_makes_a_project_root(
        self, narrow_tempdir, tmp_path: Path, marker: str
    ) -> None:
        proj = tmp_path / "workplace" / "nrb" / "notes"
        proj.mkdir(parents=True)
        (proj / marker).mkdir() if marker.startswith(".") else (proj / marker).write_text(
            "x", encoding="utf-8"
        )
        target = _file(proj / "docs" / "spec.md")
        assert classify_source(target) == (LINK, str(proj))

    def test_nearest_marker_wins_in_a_monorepo(
        self, narrow_tempdir, tmp_path: Path
    ) -> None:
        # A package inside a repo links to the PACKAGE, not the whole tree, so the
        # recorded root stays as narrow as the thing the user actually opened.
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        pkg = repo / "packages" / "app"
        (pkg / "package.json").parent.mkdir(parents=True)
        (pkg / "package.json").write_text("{}", encoding="utf-8")
        assert classify_source(_file(pkg / "src" / "doc.md")) == (LINK, str(pkg))

    def test_unmarked_directory_still_copies(
        self, narrow_tempdir, tmp_path: Path
    ) -> None:
        # No marker anywhere above it -> a snapshot, not a pointer.
        assert classify_source(_file(tmp_path / "loose" / "doc.md")) == (COPY, "")

    def test_verifier_agrees_with_the_classifier(
        self, narrow_tempdir, tmp_path: Path
    ) -> None:
        # Create-time authority and read-time verification MUST use the same
        # predicate, or a recorded link degrades to a stale snapshot on read.
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text("", encoding="utf-8")
        verdict, root = classify_source(_file(proj / "doc.md"))
        assert (verdict, root) == (LINK, str(proj))
        assert is_verifiable_root(root) is True
        assert is_verifiable_root(str(tmp_path / "loose")) is False

    def test_a_marked_filesystem_root_is_never_a_project_root(
        self, narrow_tempdir, tmp_path: Path, monkeypatch
    ) -> None:
        """`/` is never verifiable, however marked.

        A stray ``/Makefile`` would otherwise make the filesystem root a project
        root, and a forged artifact naming ``source_root="/"`` would then
        authorize reading any file the process can open.
        """
        from kiro_crew import artifact_source as mod

        # Treat tmp_path as a filesystem root: dirname(root) == root is the
        # portable root test, so fake exactly that for this one path.
        real_dirname = os.path.dirname

        def fake_dirname(path: str) -> str:
            return str(tmp_path) if str(path) == str(tmp_path) else real_dirname(path)

        (tmp_path / "Makefile").write_text("", encoding="utf-8")
        monkeypatch.setattr(mod.os.path, "dirname", fake_dirname)
        assert mod.project_root_marker(str(tmp_path)) is None
        assert mod.is_verifiable_root(str(tmp_path)) is False

    def test_home_is_never_a_project_root(
        self, narrow_tempdir, fake_home: Path
    ) -> None:
        """`$HOME` is never a project root, and `.kiro` is why this matters.

        KiroCrew's own data home is ``~/.kiro/crew``, so ``~/.kiro`` exists for
        every user. Treating that as a marker would make the whole home
        directory a project and turn a loose ``~/notes.md`` into a LIVE link
        whose artifact edits overwrite the original file.
        """
        (fake_home / ".kiro" / "crew").mkdir(parents=True, exist_ok=True)
        assert project_root_marker(str(fake_home)) is None
        assert is_verifiable_root(str(fake_home)) is False
        # And a loose file directly in home is a COPY, not a link.
        assert classify_source(_file(fake_home / "notes.md")) == (COPY, "")

    def test_every_home_spelling_is_rejected(
        self, narrow_tempdir, fake_home: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """A second, divergent home path must be rejected too.

        The OS reports home through several channels that need not agree -- on
        Windows ``expanduser`` reads ``USERPROFILE`` while ``HOME`` may say
        something else, and a Windows profile genuinely ships markers such as
        ``.vscode``. Keying the rule to one accessor let the other real home be
        walked into and authorize a link across the whole profile.
        """
        other_home = tmp_path / "otherhome"
        (other_home / ".vscode").mkdir(parents=True)
        monkeypatch.setenv("USERPROFILE", str(other_home))
        assert project_root_marker(str(other_home)) is None
        assert classify_source(_file(other_home / "notes.md")) == (COPY, "")

    def test_a_real_project_under_home_still_links(
        self, narrow_tempdir, fake_home: Path
    ) -> None:
        # Rejecting home must not reject projects that live inside it.
        proj = fake_home / "code" / "myproj"
        proj.mkdir(parents=True)
        (proj / "pyproject.toml").write_text("", encoding="utf-8")
        assert classify_source(_file(proj / "doc.md")) == (LINK, str(proj))

    def test_disposable_still_beats_a_marker(self, narrow_tempdir: Path) -> None:
        proj = narrow_tempdir / "scratch"
        proj.mkdir()
        (proj / "pyproject.toml").write_text("", encoding="utf-8")
        assert classify_source(_file(proj / "doc.md")) == (COPY, "")


class TestClassifySourceProjectWalk:
    """Branch 3 — bounded walk up to a git repository root."""

    def test_git_dir_repo_links_with_repo_root(self, narrow_tempdir, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        target = _file(repo / "src" / "deep" / "doc.md")
        assert classify_source(target) == (LINK, str(repo))

    def test_git_FILE_worktree_counts_as_repo(self, narrow_tempdir, tmp_path: Path) -> None:
        # A git worktree's .git is a FILE (gitdir pointer). os.path.exists is
        # used precisely so worktrees don't report as "not a repo" — this is
        # the KiroCrew development layout itself.
        wt = tmp_path / "kirocrew-wt-feature"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /workplace/user/KiroCrew/.git/worktrees/feature\n")
        target = _file(wt / "src" / "mod.md")
        assert classify_source(target) == (LINK, str(wt))

    def test_no_repo_no_project_is_copy(self, narrow_tempdir, tmp_path: Path) -> None:
        assert classify_source(_file(tmp_path / "loose" / "doc.md")) == (COPY, "")

    def test_walk_is_bounded(self, narrow_tempdir, tmp_path: Path, monkeypatch) -> None:
        # With a 1-level cap, a repo root two levels up must NOT be found —
        # proving the cap is actually enforced rather than incidental.
        monkeypatch.setattr(artifact_source, "PROJECT_ROOT_WALK_LIMIT", 1)
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        target = _file(repo / "a" / "b" / "doc.md")
        assert classify_source(target) == (COPY, "")
