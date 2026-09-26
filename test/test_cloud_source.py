"""Unit tests for source shipping (cloud/source.py)."""

from __future__ import annotations

import os
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

from conftest import make_dir_link
from kiro_crew.cloud import aws, source


class TestRepoRoot:
    def test_repo_root_has_install_sh(self):
        root = source.repo_root()
        assert (root / "install.sh").exists()
        assert (root / "setup.cfg").exists()

    def test_repo_root_fails_closed_when_no_marker(self, monkeypatch):
        # Installed as a wheel (no install.sh + setup.cfg above the module):
        # must raise, NOT fall back to an ancestor dir that could tar up
        # unrelated packages and ship them to S3.
        #
        # The module path is FABRICATED, never created: the walk climbs every
        # ancestor, and a real path a test can create lives under the temp root,
        # which itself may sit inside this checkout (a developer's
        # `TMPDIR=./tmp`) -- where install.sh + setup.cfg ARE above it. The
        # walk is lexical on the resolved path, so existence is not required.
        fake_module = Path("/kc-wheel-install-no-markers/site-packages/kiro_crew/cloud/source.py")
        monkeypatch.setattr(source, "__file__", str(fake_module))
        with pytest.raises(aws.AWSError, match="source root"):
            source.repo_root()


class TestBuildTarball:
    @pytest.fixture(autouse=True)
    def _fake_tracked(self, monkeypatch):
        # The tarfile fallback now packages `git ls-files` output (tracked
        # files, honoring .gitignore). For the exclusion-logic tests, simulate
        # "every file present is tracked" so the denylist/home-dir assertions
        # still exercise; the dedicated tests below override this.
        def _all_files(root):
            return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]

        monkeypatch.setattr(source, "_git_tracked_files", _all_files)

    def test_tar_fallback_excludes_heavy_dirs(self, tmp_path):
        # Build a fake repo tree with an excluded dir.
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("print('x')\n")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref: x\n")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "junk.js").write_text("//x\n")
        (tmp_path / "install.sh").write_text("echo hi\n")

        tarball = source._tar_fallback(tmp_path)
        try:
            with tarfile.open(tarball) as tf:
                names = tf.getnames()
            # Included
            assert any(n.endswith("src/app.py") for n in names)
            assert any(n.endswith("install.sh") for n in names)
            # Excluded
            assert not any(".git" in n for n in names)
            assert not any("node_modules" in n for n in names)
        finally:
            tarball.unlink()

    def test_tar_fallback_excludes_secrets(self, tmp_path):
        # Secret-bearing files/dirs must never be packaged by the fallback.
        (tmp_path / "install.sh").write_text("echo hi\n")
        (tmp_path / ".kirocrew-dev").mkdir()
        (tmp_path / ".kirocrew-dev" / "config.json").write_text('{"token":"secret"}\n')
        (tmp_path / ".env").write_text("SLACK_BOT_TOKEN=xoxb-secret\n")
        (tmp_path / "server.pem").write_text("-----BEGIN KEY-----\n")
        (tmp_path / "id_rsa.key").write_text("privatekey\n")
        (tmp_path / "ok.py").write_text("x=1\n")

        tarball = source._tar_fallback(tmp_path)
        try:
            with tarfile.open(tarball) as tf:
                names = tf.getnames()
            assert any(n.endswith("ok.py") for n in names)  # normal file shipped
            assert not any(".kirocrew-dev" in n for n in names)
            assert not any(n.endswith(".env") for n in names)
            assert not any(n.endswith(".pem") for n in names)
            assert not any(n.endswith(".key") for n in names)
        finally:
            tarball.unlink()

    def test_tar_fallback_excludes_custom_kirocrew_home(self, monkeypatch, tmp_path):
        # Dev mode can set KIROCREW_HOME to a custom-named dir at the repo root;
        # the tarfile fallback must exclude it by its actual name (not just the
        # hardcoded .kirocrew* entries) so its data/secrets don't ship to S3.
        (tmp_path / "install.sh").write_text("echo hi\n")
        home = tmp_path / "my-custom-home"
        home.mkdir()
        (home / "contacts.json").write_text('{"secret":"x"}\n')
        (tmp_path / "ok.py").write_text("x=1\n")
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        tarball = source._tar_fallback(tmp_path)
        try:
            with tarfile.open(tarball) as tf:
                names = tf.getnames()
            assert any(n.endswith("ok.py") for n in names)
            assert not any("my-custom-home" in n for n in names)
        finally:
            tarball.unlink()

    def test_tar_fallback_excludes_nested_custom_home(self, monkeypatch, tmp_path):
        # A custom KIROCREW_HOME nested below the repo root (root/data/kc-home)
        # must also be excluded from the tarfile fallback.
        (tmp_path / "install.sh").write_text("echo hi\n")
        home = tmp_path / "data" / "kc-home"
        home.mkdir(parents=True)
        (home / "secrets.json").write_text('{"k":"v"}\n')
        (tmp_path / "data" / "keep.txt").write_text("keep\n")  # sibling stays
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        tarball = source._tar_fallback(tmp_path)
        try:
            with tarfile.open(tarball) as tf:
                names = tf.getnames()
            assert not any("kc-home" in n for n in names)
            assert any(n.endswith("data/keep.txt") for n in names)  # sibling not dropped
        finally:
            tarball.unlink()

    def test_custom_home_not_excluded_when_outside_repo(self, monkeypatch, tmp_path):
        # An absolute ~/.kirocrew OUTSIDE the repo root isn't in the tarball
        # anyway; _custom_home_rel_parts must return None so we don't accidentally
        # drop a same-named dir that legitimately lives in the repo. Use a
        # sibling dir that is genuinely not under the packaged root.
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "home" / ".kirocrew"
        monkeypatch.setenv("KIROCREW_HOME", str(outside))
        assert source._custom_home_rel_parts(repo) is None

    def test_env_exclusion_is_exact_not_prefix_greedy(self, tmp_path):
        # .env and .env.local are excluded; .environment (innocent name) ships.
        (tmp_path / "install.sh").write_text("echo hi\n")
        (tmp_path / ".env").write_text("TOKEN=x\n")
        (tmp_path / ".env.local").write_text("TOKEN=y\n")
        (tmp_path / ".environment").write_text("just docs\n")

        tarball = source._tar_fallback(tmp_path)
        try:
            with tarfile.open(tarball) as tf:
                names = {n.split("/")[-1] for n in tf.getnames()}
            assert ".env" not in names
            assert ".env.local" not in names
            assert ".environment" in names
        finally:
            tarball.unlink()

    def test_tar_fallback_excludes_suffixless_credentials(self, tmp_path):
        # Gitignored root files with no telltale suffix (SSH keys,
        # credentials.json) must still be dropped by the fallback, which
        # doesn't consult .gitignore.
        (tmp_path / "install.sh").write_text("echo hi\n")
        (tmp_path / "id_rsa").write_text("privatekey\n")
        (tmp_path / "credentials.json").write_text('{"key":"secret"}\n')
        (tmp_path / ".netrc").write_text("machine x login y\n")
        (tmp_path / "ok.py").write_text("x=1\n")

        tarball = source._tar_fallback(tmp_path)
        try:
            with tarfile.open(tarball) as tf:
                names = {n.split("/")[-1] for n in tf.getnames()}
            assert "ok.py" in names
            assert "id_rsa" not in names
            assert "credentials.json" not in names
            assert ".netrc" not in names
        finally:
            tarball.unlink()

    def test_tar_fallback_never_ships_untracked_secret(self, monkeypatch, tmp_path):
        # An untracked/gitignored secret with an UNRECOGNIZED name (not in the
        # denylist) must never be packaged — because the fallback only packages
        # tracked files (git ls-files), not a whole-tree walk.
        (tmp_path / "install.sh").write_text("echo hi\n")
        (tmp_path / "app.py").write_text("x=1\n")
        (tmp_path / "secrets.yaml").write_text("token: hunter2\n")  # gitignored, odd name
        (tmp_path / "local_settings.py").write_text("SECRET='x'\n")
        # git ls-files reports only the tracked files (secrets.yaml is NOT tracked)
        monkeypatch.setattr(source, "_git_tracked_files", lambda root: ["install.sh", "app.py"])

        tarball = source._tar_fallback(tmp_path)
        try:
            with tarfile.open(tarball) as tf:
                names = {n.split("/")[-1] for n in tf.getnames()}
            assert "app.py" in names
            assert "secrets.yaml" not in names
            assert "local_settings.py" not in names
        finally:
            tarball.unlink()

    def test_tar_fallback_fails_closed_without_git(self, monkeypatch, tmp_path):
        # No tracked-file list (not a git repo / git absent) -> fail closed,
        # never walk the whole tree (which could ship a gitignored secret).
        monkeypatch.setattr(source, "_git_tracked_files", lambda root: None)
        with pytest.raises(aws.AWSError, match="without git"):
            source._tar_fallback(tmp_path)

    def test_tar_fallback_does_not_recurse_submodule_gitlink(self, monkeypatch, tmp_path):
        # `git ls-files` lists a submodule as a single gitlink entry (its dir).
        # tar.add() on a directory recurses by default and would package the
        # submodule's UNTRACKED/gitignored files (secrets). The fallback must add
        # non-recursively and skip the gitlink dir entirely.
        (tmp_path / "install.sh").write_text("echo hi\n")
        sub = tmp_path / "vendor" / "libfoo"
        sub.mkdir(parents=True)
        (sub / "tracked.py").write_text("x=1\n")  # not in ls-files (it's the submodule's)
        (sub / "submodule_secret.env").write_text("TOKEN=leak\n")  # untracked in submodule
        # ls-files reports the gitlink as the submodule DIRECTORY path (no trailing
        # slash), plus the top-level tracked file.
        monkeypatch.setattr(
            source, "_git_tracked_files", lambda root: ["install.sh", "vendor/libfoo"]
        )
        tarball = source._tar_fallback(tmp_path)
        try:
            with tarfile.open(tarball) as tf:
                names = tf.getnames()
            leaf = {n.split("/")[-1] for n in names}
            assert "install.sh" in leaf
            # nothing from inside the submodule may be packaged
            assert "submodule_secret.env" not in leaf
            assert "tracked.py" not in leaf
            assert not any("libfoo" in n and n != "vendor/libfoo" for n in names)
        finally:
            tarball.unlink()

    def test_git_archive_output_is_refiltered(self, tmp_path):
        # git archive ships tracked files; a force-added secret must still be
        # stripped so both packaging paths give symmetric guarantees.
        import io

        raw = tmp_path / "raw.tar.gz"
        with tarfile.open(raw, "w:gz") as tf:
            for name, data in (
                ("src/app.py", b"x=1\n"),
                ("server.pem", b"-----BEGIN KEY-----\n"),
                (".env", b"TOKEN=secret\n"),
                (".kirocrew/config.json", b"{}\n"),
            ):
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))

        filtered = source._refilter_archive(raw)
        try:
            with tarfile.open(filtered) as tf:
                names = tf.getnames()
            assert names == ["src/app.py"]
            assert not raw.exists()  # unfiltered original removed
        finally:
            filtered.unlink()

    def test_refilter_corrupt_archive_cleans_up_temp(self, tmp_path, monkeypatch):
        # A corrupt source archive makes tarfile.open raise; _refilter_archive
        # must NOT leak the half-written 'filtered' temp (the caller only cleans
        # up the original). Track the exact file created rather than globbing
        # /tmp (the glob is racy under parallel xdist workers).
        import tarfile as _tf
        import tempfile as _tmp

        import kiro_crew.cloud.source as _src

        created: list[str] = []
        real_ntf = _tmp.NamedTemporaryFile

        def _capturing_ntf(*a, **kw):
            f = real_ntf(*a, **kw)
            created.append(f.name)
            return f

        monkeypatch.setattr(_tmp, "NamedTemporaryFile", _capturing_ntf)

        bad = tmp_path / "corrupt.tar.gz"
        bad.write_bytes(b"not a gzip tarball")
        with pytest.raises(_tf.TarError):
            _src._refilter_archive(bad)
        assert created, "NamedTemporaryFile was never called"
        leaked = [p for p in created if Path(p).exists()]
        assert not leaked, f"refilter leaked temp file(s): {leaked}"

    def test_dirty_tree_uses_working_tree_not_git_archive(self, monkeypatch, tmp_path):
        # A dirty tracked working tree must NOT be packaged via `git archive HEAD`
        # (which ships stale committed code). build_source_tarball must switch to
        # the ls-files tar path so uncommitted edits are shipped.
        monkeypatch.setattr(source, "_tracked_tree_is_dirty", lambda root: True)

        def _archive_must_not_run(root):  # pragma: no cover - must not be called
            raise AssertionError("git archive must be skipped for a dirty tree")

        monkeypatch.setattr(source, "_use_git_archive", _archive_must_not_run)
        (tmp_path / "app.py").write_text("x=2  # edited, uncommitted\n")
        monkeypatch.setattr(source, "_git_tracked_files", lambda root: ["app.py"])
        tarball = source.build_source_tarball(tmp_path)
        try:
            with tarfile.open(tarball) as tf:
                data = tf.extractfile("app.py").read().decode()
            assert "edited, uncommitted" in data  # working-tree content, not HEAD
        finally:
            tarball.unlink()

    def test_clean_tree_prefers_git_archive(self, monkeypatch, tmp_path):
        # The fast path (git archive) is still used when the tree is clean.
        monkeypatch.setattr(source, "_tracked_tree_is_dirty", lambda root: False)
        sentinel = tmp_path / "archive.tar.gz"
        sentinel.write_bytes(b"x")
        monkeypatch.setattr(source, "_use_git_archive", lambda root: sentinel)
        assert source.build_source_tarball(tmp_path) == sentinel


class TestTarballStagingDirectory:
    """Where the source tarball is built, not what goes into it.

    ``upload_source`` passes the tarball to ``s3api put-object`` as
    ``--body <path>``, so the AWS CLI re-opens it by name after this process has
    closed it. Every builder must therefore leave the file somewhere the
    launcher owns for the length of that window -- never in the process temp
    root, whose location ``tempfile`` takes from the environment and which
    carries no promise of being owner-only.
    """

    @staticmethod
    def _pin_home_and_temp_root(monkeypatch, tmp_path) -> tuple[Path, Path]:
        """Point the data home and the process temp root at two separate dirs.

        Separating them is what makes the assertions discriminating: a builder
        that honours the temp root and one that resolves the data home would
        otherwise write to the same place under pytest's ``tmp_path``.
        """
        home = tmp_path / "data-home"
        home.mkdir(mode=0o700)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        return home, temp_root

    @staticmethod
    def _tiny_targz(path: Path) -> Path:
        """A real (tiny) gzip tarball, so size and open checks see valid bytes."""
        member = path.parent / "member.txt"
        member.write_text("x\n")
        with tarfile.open(path, "w:gz") as tf:
            tf.add(member, arcname="member.txt")
        return path

    def _capture_staged_names(self, monkeypatch) -> list[str]:
        """Every path ``NamedTemporaryFile`` hands out, in call order."""
        created: list[str] = []
        real_ntf = tempfile.NamedTemporaryFile

        def _capturing(*a, **kw):
            fh = real_ntf(*a, **kw)
            created.append(fh.name)
            return fh

        monkeypatch.setattr(tempfile, "NamedTemporaryFile", _capturing)
        return created

    def test_the_tarfile_fallback_stages_under_the_data_home(self, monkeypatch, tmp_path):
        home, temp_root = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("x = 1\n")
        monkeypatch.setattr(source, "_git_tracked_files", lambda root: ["app.py"])

        tarball = source._tar_fallback(repo)
        try:
            assert tarball.parent == home / source._STAGING_DIR_LEAF
            # The process temp root must not have been used at all.
            assert list(temp_root.iterdir()) == []
        finally:
            tarball.unlink(missing_ok=True)

    def test_the_git_archive_path_stages_both_tarballs_under_the_data_home(
        self, monkeypatch, tmp_path
    ):
        # `git archive` writes one file and `_refilter_archive` rewrites it into a
        # second: BOTH are staged, so capture every name rather than only the
        # path handed back.
        home, temp_root = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        payload = self._tiny_targz(tmp_path / "payload.tar.gz")
        real_run = subprocess.run

        def _fake_run(argv, **kw):
            if argv[:1] == ["git"] and "archive" in argv:
                Path(argv[argv.index("-o") + 1]).write_bytes(payload.read_bytes())
                return subprocess.CompletedProcess(argv, 0, "", "")
            return real_run(argv, **kw)

        monkeypatch.setattr(subprocess, "run", _fake_run)
        staged = self._capture_staged_names(monkeypatch)

        tarball = source._use_git_archive(tmp_path / "repo")
        try:
            assert tarball is not None, "the stubbed git archive should have succeeded"
            expected_parent = home / source._STAGING_DIR_LEAF
            assert len(staged) == 2, f"expected an archive and a re-filtered copy, got {staged}"
            assert [Path(p).parent for p in staged] == [expected_parent, expected_parent]
            assert tarball.parent == expected_parent
            assert list(temp_root.iterdir()) == []
        finally:
            if tarball is not None:
                tarball.unlink(missing_ok=True)

    def test_the_refilter_stages_under_the_data_home(self, monkeypatch, tmp_path):
        home, temp_root = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        archive = self._tiny_targz(tmp_path / "archive.tar.gz")

        filtered = source._refilter_archive(archive)
        try:
            assert filtered.parent == home / source._STAGING_DIR_LEAF
            assert list(temp_root.iterdir()) == []
        finally:
            filtered.unlink(missing_ok=True)

    def test_the_staging_directory_is_owner_only(self, monkeypatch, tmp_path):
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        staging = source._staging_dir()
        assert staging == home / source._STAGING_DIR_LEAF
        assert staging.is_dir()
        if os.name == "posix":
            assert stat.S_IMODE(staging.stat().st_mode) == 0o700

    def test_a_pre_existing_staging_directory_is_re_restricted(self, monkeypatch, tmp_path):
        # A leaf left group- or world-readable by an earlier umask must not be
        # accepted as-is: the tarball is the box's whole source tree.
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        loose = home / source._STAGING_DIR_LEAF
        loose.mkdir()
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        # The loose mode IS the fixture: the assertion below is that _staging_dir
        # tightens a leaf an earlier umask left group- and world-readable. Nothing
        # is published from this temp directory.
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(loose, 0o755)  # noqa: S103 - the loose mode is the fixture. lockdown-ok.
        assert stat.S_IMODE(source._staging_dir().stat().st_mode) == 0o700

    def test_a_lockdown_that_cannot_be_applied_refuses_the_build(self, monkeypatch, tmp_path):
        # restrict_dir_to_owner is fail-loud by contract. A mount that rejects the
        # change must stop the build, not warn and stage the tarball anyway.
        self._pin_home_and_temp_root(monkeypatch, tmp_path)

        def _refuse(path):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(source.platform_compat, "restrict_dir_to_owner", _refuse)
        with pytest.raises(aws.AWSError, match="owner-only"):
            source._staging_dir()

    def test_a_lockdown_that_silently_does_not_take_refuses_the_build(self, monkeypatch, tmp_path):
        # A filesystem with a fixed permission mask accepts the chmod and keeps
        # its own mode, so a successful call is not evidence of the mode. The
        # read-back is what closes that, and it is the whole point of this pin.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        leaf = home / source._STAGING_DIR_LEAF
        leaf.mkdir()
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(
            leaf, 0o757
        )  # noqa: S103 - simulating the mask a FAT/CIFS mount keeps. lockdown-ok.
        monkeypatch.setattr(source.platform_compat, "restrict_dir_to_owner", lambda path: None)
        with pytest.raises(aws.AWSError, match="reachable by other accounts"):
            source._staging_dir()

    def test_a_link_planted_at_the_staging_leaf_is_refused(self, monkeypatch, tmp_path):
        # A link at the leaf would put every tarball back outside the fence, and
        # no per-file check can see that -- fail closed instead of building.
        #
        # conftest.make_dir_link, not symlink_to: a directory symlink needs
        # SeCreateSymbolicLinkPrivilege, which an unelevated Windows shell lacks,
        # so a symlink here would skip on exactly the host whose junction branch of
        # is_link_or_junction this check depends on.
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        elsewhere = tmp_path / "attacker-controlled"
        elsewhere.mkdir()
        make_dir_link(home / source._STAGING_DIR_LEAF, elsewhere)
        with pytest.raises(aws.AWSError, match="staging directory"):
            source._staging_dir()

    def test_a_linked_ancestor_above_the_data_home_is_followed(self, monkeypatch, tmp_path):
        # A link ABOVE the data home is the operator's own layout -- a symlinked
        # $HOME, a dotfile-managed ~/.kiro -- and the product documents both as
        # supported (atomic_write._link_trust_anchor, workflows.md). Refusing it
        # would refuse a launch on the DEFAULT data-home path, which is lexical
        # (Path.home() / ".kiro" / "crew"), so it is where such a link arrives.
        #
        # Driven through config_dir rather than KIROCREW_HOME: the override arrives
        # already resolved, so a link is unobservable on that path.
        #
        # What still protects the build is that _first_replaceable walks the
        # RESOLVED chain, so the ownership and mode tests land on the directories
        # that really hold the tarball. Asserting the resolved location is what
        # makes this discriminating: a staging dir merely created at the lexical
        # name would pass a bare "no exception" check.
        real = tmp_path / "real-parent"
        (real / "data-home").mkdir(mode=0o700, parents=True)
        link = tmp_path / "linked-parent"
        make_dir_link(link, real)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        monkeypatch.setattr(source, "config_dir", lambda: link / "data-home")

        staging = source._staging_dir()

        assert staging == link / "data-home" / source._STAGING_DIR_LEAF
        assert staging.is_dir()
        assert not staging.is_symlink()
        assert staging.resolve() == (real / "data-home" / source._STAGING_DIR_LEAF).resolve()

    def test_a_data_home_relocated_through_a_link_is_followed(self, monkeypatch, tmp_path):
        # The same policy one level down: the data home ITSELF being a link is the
        # documented "data home moved onto another disk" layout, and it sits AT the
        # trust anchor rather than below it. Planting such a link needs write on its
        # parent, which is exactly what _first_replaceable refuses -- so on a chain
        # that passes those tests the link can only be the operator's own.
        real = tmp_path / "another-disk"
        real.mkdir(mode=0o700)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        holder = tmp_path / "holder"
        holder.mkdir(mode=0o700)
        make_dir_link(holder / "data-home", real)
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        monkeypatch.setattr(source, "config_dir", lambda: holder / "data-home")

        staging = source._staging_dir()

        assert staging.is_dir()
        assert not staging.is_symlink()
        assert staging.resolve() == (real / source._STAGING_DIR_LEAF).resolve()

    def test_the_default_data_home_under_a_symlinked_home_is_accepted(self, monkeypatch, tmp_path):
        """The reviewer's scenario, through the REAL ``config_dir()``.

        ``HOME=/home/u`` with ``/home/u -> /local/home/u`` and ``KIROCREW_HOME``
        unset is an ordinary host, not an unusual one, and it is the layout this
        repo's own checkout instructions run on. The default data home is lexical,
        so ``config_dir()`` hands ``_staging_dir()`` a path whose ancestor is a
        link, and a blanket refusal there aborts ``cloud launch`` for a user who
        did nothing unusual and cannot satisfy it without repointing
        ``KIROCREW_HOME`` at a pre-resolved path.

        Driven through the real ``config_dir()`` rather than a stub, because the
        lexical default home is the whole mechanism: a stub returning an
        already-resolved path cannot show it. ``config.paths`` memoises the
        resolved home per process, so both caches are cleared alongside ``$HOME``
        or this would read whichever home the process resolved first.
        """
        from kiro_crew.config import paths as config_paths

        real_home = tmp_path / "local" / "home" / "u"
        real_home.mkdir(mode=0o700, parents=True)
        link_home = tmp_path / "home"
        link_home.mkdir()
        make_dir_link(link_home / "u", real_home)
        home = link_home / "u"
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setattr(config_paths, "_resolved_home", None)
        monkeypatch.setattr(config_paths, "_config_dir_memo", None)

        # The premise: the default home really is reached through the link.
        base = source.config_dir()
        assert base == home / ".kiro" / "crew"
        assert home.is_symlink() or source.platform_compat.is_link_or_junction(home)

        staging = source._staging_dir()

        assert staging == base / source._STAGING_DIR_LEAF
        assert staging.is_dir()
        assert not staging.is_symlink()
        assert (
            staging.resolve() == (real_home / ".kiro" / "crew" / source._STAGING_DIR_LEAF).resolve()
        )
        # And it is still locked down, which is the guarantee the leaf carries.
        if os.name == "posix":
            assert stat.S_IMODE(staging.stat().st_mode) & 0o077 == 0

    def test_a_writable_parent_of_a_relocated_home_is_refused(self, monkeypatch, tmp_path):
        # The other half of following a link at or above the data home: what the
        # guard gives up must be ONLY the link, never the hazard the link hid.
        #
        # The data home is a link onto another disk, and the loose directory is the
        # link TARGET's parent -- the directory whose write bit lets `real-home` be
        # renamed away and replaced after this process closes the tarball. That
        # directory appears on the RESOLVED chain and on no lexical one: walking the
        # path as written stats `holder/data-home`, which follows the link and reads
        # the target's own sound 0o700, then stops at `holder`. So `shared` is not
        # merely misnamed by a lexical walk, it is invisible to one, and resolving is
        # what brings the real hazard into scope at all.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        shared = tmp_path / "shared-real-parent"
        real_home = shared / "real-home"
        real_home.mkdir(mode=0o700, parents=True)
        holder = tmp_path / "holder"
        holder.mkdir(mode=0o700)
        make_dir_link(holder / "data-home", real_home)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        monkeypatch.setattr(source, "config_dir", lambda: holder / "data-home")
        # Every LEXICAL component is sound, so a walk on the path as written passes.
        assert stat.S_IMODE(holder.stat().st_mode) & stat.S_IWOTH == 0
        assert stat.S_IMODE((holder / "data-home").stat().st_mode) & stat.S_IWOTH == 0
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(shared, 0o777)  # noqa: S103 - the shared parent is the fixture. lockdown-ok.

        with pytest.raises(aws.AWSError, match="writable by any account") as caught:
            source._staging_dir()

        # Named in its RESOLVED form, which is the directory the operator has to fix.
        assert str(shared.resolve()) in str(caught.value)

    def test_an_ancestor_others_can_write_refuses_the_build(self, monkeypatch, tmp_path):
        # Replacing a directory entry needs write on its PARENT, so a writable
        # ancestor lets the data home itself be swapped wholesale after every
        # check on the home's own mode has passed.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        shared = tmp_path / "shared-parent"
        home = shared / "data-home"
        home.mkdir(mode=0o700, parents=True)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(shared, 0o777)  # noqa: S103 - the shared parent is the fixture. lockdown-ok.
        with pytest.raises(aws.AWSError, match="writable by any account"):
            source._staging_dir()
        assert str(shared) in str(
            pytest.raises(aws.AWSError, source._staging_dir).value
        ), "the refusal should name the outermost problem, not an inner one"

    def test_a_group_writable_ancestor_is_accepted(self, monkeypatch, tmp_path):
        # A host with user-private groups leaves an ordinary directory 0o775 to a
        # group holding only the operator, so refusing on the group bit would refuse
        # every launch on that host. Others-writable is the refusal; the group bit is
        # not, because no mode bit tells a shared group from a private one.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        shared = tmp_path / "group-writable-parent"
        home = shared / "data-home"
        home.mkdir(mode=0o700, parents=True)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(shared, 0o775)  # noqa: S103 - the user-private-group shape. lockdown-ok.
        staging = source._staging_dir()
        assert staging == home / source._STAGING_DIR_LEAF
        assert stat.S_IMODE(shared.stat().st_mode) == 0o775, "the launch rewrote the parent"

    def test_a_sticky_ancestor_is_accepted(self, monkeypatch, tmp_path):
        # The sticky bit is exactly the rule that only an entry's owner may rename
        # it, so a sticky world-writable ancestor (the shape of /tmp) is not the
        # swap this refuses -- otherwise the check would reject every ordinary host.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        shared = tmp_path / "sticky-parent"
        home = shared / "data-home"
        home.mkdir(mode=0o700, parents=True)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(shared, 0o1777)  # noqa: S103 - the /tmp shape is the fixture. lockdown-ok.
        assert source._staging_dir() == home / source._STAGING_DIR_LEAF

    def test_a_foreign_owned_ancestor_is_refused_whatever_its_mode(self, monkeypatch, tmp_path):
        # A directory's OWNER can replace what is inside it whatever the mode says,
        # so an unwritable 0o755 ancestor owned by someone else is still a swap. The
        # chain here is ordinary; what makes every node foreign is the launcher's
        # own euid, which is the only half of the comparison a test can move.
        if os.name != "posix":
            pytest.skip("POSIX ownership")
        self._pin_home_and_temp_root(monkeypatch, tmp_path)
        real_uid = os.geteuid()
        monkeypatch.setattr(source.os, "geteuid", lambda: real_uid + 4242)
        with pytest.raises(aws.AWSError, match="owned by another account"):
            source._staging_dir()

    def test_sticky_does_not_exempt_a_foreign_owned_ancestor(self, monkeypatch, tmp_path):
        # The hole the ordering closes. Under the sticky bit an entry may be renamed
        # by the entry's owner, by the DIRECTORY's owner, or by root -- so a
        # foreign-owned sticky directory hands its owner the same swap, and sticky
        # must not short-circuit before ownership is judged.
        #
        # Exactly ONE node is made foreign, the sticky one. A blanket euid change
        # would make every node foreign, and then a later node's refusal would let a
        # sticky-first short-circuit pass this test.
        if os.name != "posix":
            pytest.skip("POSIX ownership")
        shared = tmp_path / "sticky-foreign-parent"
        home = shared / "data-home"
        home.mkdir(mode=0o700, parents=True)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(
            shared, 0o1777
        )  # noqa: S103 - a sticky foreign parent is the fixture. lockdown-ok.

        real_stat = Path.stat
        foreign = os.geteuid() + 4242
        target = str(shared.resolve())

        def _stat_with_one_foreign_owner(self, *a, **kw):
            info = real_stat(self, *a, **kw)
            if str(self) in (str(shared), target):
                fields = list(info)
                fields[4] = foreign  # st_uid
                return os.stat_result(tuple(fields))
            return info

        monkeypatch.setattr(Path, "stat", _stat_with_one_foreign_owner)
        with pytest.raises(aws.AWSError, match="owned by another account"):
            source._staging_dir()

    def test_ancestors_above_the_operators_home_are_out_of_scope(self, monkeypatch, tmp_path):
        # Every real chain runs through directories this account does not own: on a
        # sandboxed host '/' itself reads as an unmapped uid. Refusing there would
        # reject an ordinary container and buy nothing, so the walk stops at the
        # operator's own home.
        #
        # The foreign ancestor is CONSTRUCTED above a pinned account home, not
        # probed from the real one: a host whose '/' and '/home' are both root-owned
        # offers nothing to find, and a test that reads the operator's real home is
        # a test that passes or fails on whose machine it runs.
        if os.name != "posix":
            pytest.skip("POSIX ownership")
        above = tmp_path / "above-the-account"
        account = above / "account-home"
        home = account / "kirocrew"
        home.mkdir(mode=0o700, parents=True)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: account))

        real_stat = Path.stat
        foreign = os.geteuid() + 4242
        targets = {str(above), str(above.resolve())}

        def _stat_with_one_foreign_ancestor(self, *a, **kw):
            info = real_stat(self, *a, **kw)
            if str(self) in targets:
                fields = list(info)
                fields[4] = foreign  # st_uid
                return os.stat_result(tuple(fields))
            return info

        monkeypatch.setattr(Path, "stat", _stat_with_one_foreign_ancestor)
        assert above.resolve() not in source._chain_the_launcher_owns(home)
        assert source._staging_dir() == home / source._STAGING_DIR_LEAF

    def test_a_home_inside_the_account_stops_the_walk_at_that_home(self, monkeypatch, tmp_path):
        # The scope branch, pinned where it lives rather than through whichever node
        # happens to refuse first. A home inside the operator's account is checked
        # from that home down, so the directories above it are never stat-ed.
        if os.name != "posix":
            pytest.skip("POSIX paths")
        account = tmp_path / "account-home"
        home = account / "kirocrew"
        home.mkdir(mode=0o700, parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: account))
        chain = source._chain_the_launcher_owns(home)
        assert chain[0] == account.resolve(), "the walk should start at the operator's own home"
        assert chain[-1] == home.resolve()
        assert (
            Path(account.resolve().root) not in chain
        ), "a directory above the account is out of scope"

    def test_a_home_relocated_outside_the_account_walks_its_whole_chain(
        self, monkeypatch, tmp_path
    ):
        # The premise above does not cover a data home the operator moved out of
        # their own account, which is exactly the shared-host case: there an ancestor
        # can belong to a local peer, so nothing is taken as given.
        if os.name != "posix":
            pytest.skip("POSIX paths")
        elsewhere = tmp_path / "pretend-account-home"
        elsewhere.mkdir(mode=0o700)
        home = tmp_path / "srv-shared" / "kirocrew"
        home.mkdir(mode=0o700, parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: elsewhere))
        chain = source._chain_the_launcher_owns(home)
        assert chain[0] == Path(home.resolve().root), "the walk should start at the root"
        assert chain[-1] == home.resolve()

    def test_a_peer_writable_directory_under_the_account_refuses_the_build(
        self, monkeypatch, tmp_path
    ):
        # The reachable shape of the same threat: a shared directory inside the
        # operator's own account holding the data home.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        shared = tmp_path / "shared-parent"
        home = shared / "data-home"
        home.mkdir(mode=0o700, parents=True)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(shared, 0o777)  # noqa: S103 - the peer-writable share is the fixture. lockdown-ok.
        with pytest.raises(aws.AWSError, match="writable by any account"):
            source._staging_dir()

    def test_the_data_home_is_verified_not_rewritten(self, monkeypatch, tmp_path):
        # The home belongs to the operator. A launch must not silently chmod it --
        # it refuses, and the mode it found is still there afterwards.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(home, 0o777)  # noqa: S103 - the writable home is the fixture. lockdown-ok.
        with pytest.raises(aws.AWSError, match="writable by any account"):
            source._staging_dir()
        assert stat.S_IMODE(home.stat().st_mode) == 0o777, "the launch rewrote the operator's home"

    def test_a_world_readable_but_unwritable_home_is_accepted(self, monkeypatch, tmp_path):
        # Others-WRITABLE is the swap precondition; others-readable is not, and the
        # leaf's own owner-only mode is what keeps the tarball unreadable. A launch
        # must not refuse the ordinary 0o755 a plain mkdir leaves under umask 022.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(home, 0o755)  # noqa: S103 - the default-umask home is the fixture. lockdown-ok.
        staging = source._staging_dir()
        assert staging == home / source._STAGING_DIR_LEAF
        assert stat.S_IMODE(staging.stat().st_mode) == 0o700

    def test_a_failed_fallback_build_leaves_no_tarball_behind(self, monkeypatch, tmp_path):
        # The staging dir lives under the data home, which no reboot clears, so a
        # build that dies mid-write must remove its own half-written file.
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("x = 1\n")
        monkeypatch.setattr(source, "_git_tracked_files", lambda root: ["app.py"])
        staged = self._capture_staged_names(monkeypatch)

        def _boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(tarfile, "open", _boom)
        with pytest.raises(OSError, match="disk full"):
            source._tar_fallback(repo)

        assert staged, "NamedTemporaryFile was never called"
        leaked = [p for p in staged if Path(p).exists()]
        assert not leaked, f"failed build leaked staged tarball(s): {leaked}"
        assert list((home / source._STAGING_DIR_LEAF).iterdir()) == []

    def test_an_interrupted_git_archive_leaves_no_tarball_behind(self, monkeypatch, tmp_path):
        # An interrupt is not a fallback case: it propagates, so the cleanup the
        # fallback path does on its way out never runs. Without its own arm the
        # archive keeps a copy of the whole checkout in the data home, which no
        # reboot clears, and every aborted launch adds another.
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        staged = self._capture_staged_names(monkeypatch)

        def _interrupted(*a, **kw):
            raise KeyboardInterrupt

        monkeypatch.setattr(subprocess, "run", _interrupted)
        with pytest.raises(KeyboardInterrupt):
            source._use_git_archive(tmp_path / "repo")

        assert staged, "NamedTemporaryFile was never called"
        leaked = [p for p in staged if Path(p).exists()]
        assert not leaked, f"an interrupted archive leaked staged tarball(s): {leaked}"
        assert list((home / source._STAGING_DIR_LEAF).iterdir()) == []


class TestBucketNaming:
    def test_bucket_name(self, monkeypatch):
        monkeypatch.setattr(source, "_account_id", lambda *a: "814959995281")
        assert source.bucket_name("dev", "us-east-1") == "kirocrew-src-814959995281-us-east-1"


class TestEnsureBucket:
    def test_reuses_existing(self, monkeypatch):
        monkeypatch.setattr(source, "_account_id", lambda *a: "123")
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, "", ""))  # head-bucket ok
        actions = []
        monkeypatch.setattr(
            aws, "checked", lambda args, *a, action="", **k: actions.append(action) or ""
        )
        b = source.ensure_bucket("dev", "us-east-1")
        assert b == "kirocrew-src-123-us-east-1"
        # existing bucket: no create-bucket...
        assert "s3:CreateBucket" not in actions
        # ...but the public-access block MUST still be (re-)enforced on reuse,
        # or a pre-existing bucket with BPA disabled would receive private source.
        assert "s3:PutBucketPublicAccessBlock" in actions

    def test_reuse_bpa_pins_expected_owner(self, monkeypatch):
        # The BPA enforcement on the reuse path must itself pin the owner.
        monkeypatch.setattr(source, "_account_id", lambda *a: "123")
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, "", ""))  # head-bucket ok
        seen = {}
        monkeypatch.setattr(
            aws, "checked", lambda args, *a, action="", **k: seen.update(args=args) or ""
        )
        source.ensure_bucket("dev", "us-east-1")
        assert "put-public-access-block" in seen["args"]
        assert "--expected-bucket-owner" in seen["args"] and "123" in seen["args"]

    def test_head_bucket_pins_expected_owner(self, monkeypatch):
        # Bucket names are global — the reuse path must pin our account id so
        # a squatter's same-named bucket 403s instead of receiving our source.
        monkeypatch.setattr(source, "_account_id", lambda *a: "123")
        seen = {}

        def fake_run(args, *a, **k):
            seen["args"] = args
            return (0, "", "")

        monkeypatch.setattr(aws, "run_aws", fake_run)
        source.ensure_bucket("dev", "us-east-1")
        assert "--expected-bucket-owner" in seen["args"]
        assert "123" in seen["args"]

    def test_creates_when_missing(self, monkeypatch):
        monkeypatch.setattr(source, "_account_id", lambda *a: "123")
        # head-bucket fails (404) then create succeeds; subsequent run_aws calls ok.
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (255, "", "Not Found"))
        created = {"n": 0}
        monkeypatch.setattr(
            aws, "checked", lambda *a, **k: created.update(n=created["n"] + 1) or ""
        )
        b = source.ensure_bucket("dev", "us-west-2")
        assert b == "kirocrew-src-123-us-west-2"
        assert created["n"] >= 1

    def test_raises_when_account_id_unresolved(self, monkeypatch):
        # Without the account id we cannot pin --expected-bucket-owner; fail
        # closed instead of minting an unprotected `unknown`-named bucket.
        monkeypatch.setattr(source, "_account_id", lambda *a: "")

        def _boom(*a, **k):  # pragma: no cover - must not reach AWS
            raise AssertionError("must not touch S3 without a resolved account id")

        monkeypatch.setattr(aws, "run_aws", _boom)
        with pytest.raises(aws.AWSError, match="account id"):
            source.ensure_bucket("dev", "us-east-1")


_ACCT12 = "123456789012"


def _boundary_verify_json(args, doc):
    """Fake aws.checked_json for the content-verification path.

    get-policy → DefaultVersionId v1; get-policy-version → the given Document.
    """
    if args[:2] == ["iam", "get-policy"]:
        return {"Policy": {"DefaultVersionId": "v1"}}
    if args[:2] == ["iam", "get-policy-version"]:
        return {"PolicyVersion": {"Document": doc}}
    raise AssertionError(f"unexpected checked_json {args[:2]}")


class TestEnsureInstanceBoundary:
    def test_reuses_existing_boundary_when_content_matches(self, monkeypatch):
        # An existing boundary is reused (never re-versioned) ONLY after its
        # content is verified to match the fixed document.
        from kiro_crew.cloud import iam

        monkeypatch.setattr(source, "_account_id", lambda *a: _ACCT12)
        calls = []

        def fake_run(args, *a, **k):
            calls.append(list(args[:2]))
            if args[:2] == ["iam", "get-policy"]:
                return (0, "{}", "")  # already exists
            raise AssertionError(f"must not run {args[:2]} when boundary exists")

        # content-verification uses checked_json; return the MATCHING document
        expected = iam.boundary_policy_document(_ACCT12)
        monkeypatch.setattr(
            aws, "checked_json", lambda args, *a, **k: _boundary_verify_json(args, expected)
        )
        monkeypatch.setattr(aws, "run_aws", fake_run)
        arn = source.ensure_instance_boundary("dev", "us-east-1")
        assert arn == iam.boundary_arn(_ACCT12)
        assert ["iam", "get-policy"] in calls
        assert ["iam", "create-policy"] not in calls  # never re-created/versioned

    def test_existing_boundary_content_mismatch_fails_closed(self, monkeypatch):
        # A permissive/altered boundary seeded at the fixed name must be REFUSED,
        # not reused — closing the first-write-race escalation (now DoS-only).
        monkeypatch.setattr(source, "_account_id", lambda *a: _ACCT12)
        monkeypatch.setattr(aws, "run_aws", lambda args, *a, **k: (0, "{}", ""))  # exists
        # verification returns a PERMISSIVE document (admin *:*), not ours
        permissive = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
        }
        monkeypatch.setattr(
            aws, "checked_json", lambda args, *a, **k: _boundary_verify_json(args, permissive)
        )
        with pytest.raises(aws.AWSError, match="does NOT match"):
            source.ensure_instance_boundary("dev", "us-east-1")

    def test_existing_boundary_match_is_dict_key_order_insensitive(self, monkeypatch):
        # The compare is canonical JSON (sort_keys), so the same document with its
        # dict KEYS in a different order (as AWS may return them) still matches and
        # is reused — we don't spuriously reject our own boundary over key order.
        from kiro_crew.cloud import iam

        monkeypatch.setattr(source, "_account_id", lambda *a: _ACCT12)
        monkeypatch.setattr(aws, "run_aws", lambda args, *a, **k: (0, "{}", ""))
        expected = iam.boundary_policy_document(_ACCT12)
        # Rebuild each statement dict with keys in reversed insertion order
        # (semantically identical; only key order differs).
        rekeyed = {
            "Statement": [
                {k: s[k] for k in reversed(list(s.keys()))} for s in expected["Statement"]
            ],
            "Version": expected["Version"],
        }
        monkeypatch.setattr(
            aws, "checked_json", lambda args, *a, **k: _boundary_verify_json(args, rekeyed)
        )
        # matches under canonical (sorted-key) JSON → reused, no exception
        assert source.ensure_instance_boundary("dev", "us-east-1") == iam.boundary_arn(_ACCT12)

    def test_creates_when_absent_with_fixed_document(self, monkeypatch):
        from kiro_crew.cloud import iam

        monkeypatch.setattr(source, "_account_id", lambda *a: "123456789012")
        created = {}

        def fake_run(args, *a, **k):
            if args[:2] == ["iam", "get-policy"]:
                return (255, "", "NoSuchEntity")  # absent
            if args[:2] == ["iam", "create-policy"]:
                created["args"] = list(args)
                return (0, "{}", "")
            raise AssertionError(f"unexpected {args[:2]}")

        monkeypatch.setattr(aws, "run_aws", fake_run)
        arn = source.ensure_instance_boundary("dev", "us-east-1")
        assert arn == iam.boundary_arn("123456789012")
        # created with the fixed name + the content-fixed, account-scoped document
        assert "--policy-name" in created["args"]
        assert iam.BOUNDARY_NAME in created["args"]
        assert "--policy-document" in created["args"]
        doc_idx = created["args"].index("--policy-document") + 1
        assert created["args"][doc_idx] == iam.boundary_policy_json("123456789012")
        # NEVER a versioning/delete verb on the create path
        assert "create-policy-version" not in created["args"]

    def test_concurrent_create_race_is_success_when_content_matches(self, monkeypatch):
        # get-policy says absent, but a concurrent launch created it first →
        # create-policy returns EntityAlreadyExists → we VERIFY content matches
        # ours, then treat as success.
        from kiro_crew.cloud import iam

        monkeypatch.setattr(source, "_account_id", lambda *a: _ACCT12)

        def fake_run(args, *a, **k):
            if args[:2] == ["iam", "get-policy"]:
                return (255, "", "NoSuchEntity")
            return (255, "", "EntityAlreadyExists: policy already exists")

        expected = iam.boundary_policy_document(_ACCT12)
        monkeypatch.setattr(
            aws, "checked_json", lambda args, *a, **k: _boundary_verify_json(args, expected)
        )
        monkeypatch.setattr(aws, "run_aws", fake_run)
        assert source.ensure_instance_boundary("dev", "us-east-1") == iam.boundary_arn(_ACCT12)

    def test_create_denied_surfaces_missing_action(self, monkeypatch):
        monkeypatch.setattr(source, "_account_id", lambda *a: "123456789012")

        def fake_run(args, *a, **k):
            if args[:2] == ["iam", "get-policy"]:
                return (255, "", "NoSuchEntity")
            return (
                255,
                "",
                "User is not authorized to perform: iam:CreatePolicy on resource ...",
            )

        monkeypatch.setattr(aws, "run_aws", fake_run)
        with pytest.raises(aws.AWSError, match="iam:CreatePolicy"):
            source.ensure_instance_boundary("dev", "us-east-1")

    def test_raises_without_account_id(self, monkeypatch):
        monkeypatch.setattr(source, "_account_id", lambda *a: "")

        def _boom(*a, **k):  # pragma: no cover - must not reach AWS
            raise AssertionError("must not touch IAM without a resolved account id")

        monkeypatch.setattr(aws, "run_aws", _boom)
        with pytest.raises(aws.AWSError, match="account id"):
            source.ensure_instance_boundary("dev", "us-east-1")


class TestEnsureCrewBoundary:
    """T3's creator, and the evidence it does not reimplement the instance one."""

    def test_creates_when_absent_from_the_content_fixed_document(self, monkeypatch):
        from kiro_crew.cloud import iam

        monkeypatch.setattr(source, "_account_id", lambda *a: _ACCT12)
        created = {}

        def fake_run(args, *a, **k):
            if args[:2] == ["iam", "get-policy"]:
                return (255, "", "NoSuchEntity")
            if args[:2] == ["iam", "create-policy"]:
                created["args"] = list(args)
                return (0, "{}", "")
            raise AssertionError(f"unexpected {args[:2]}")

        monkeypatch.setattr(aws, "run_aws", fake_run)
        assert source.ensure_crew_boundary("dev", "us-east-1") == iam.crew_boundary_arn(_ACCT12)
        argv = created["args"]
        assert iam.CREW_BOUNDARY_NAME in argv
        doc_idx = argv.index("--policy-document") + 1
        assert argv[doc_idx] == iam.crew_boundary_policy_json()
        # The EC2 lane's name must not appear on this path: two boundaries exist
        # precisely so neither is created under the other's identity.
        assert iam.BOUNDARY_NAME not in argv
        for verb in ("create-policy-version", "delete-policy", "set-default-policy-version"):
            assert verb not in argv

    def test_reuses_an_existing_boundary_only_after_verifying_content(self, monkeypatch):
        from kiro_crew.cloud import iam

        monkeypatch.setattr(source, "_account_id", lambda *a: _ACCT12)
        calls = []

        def fake_run(args, *a, **k):
            calls.append(list(args[:2]))
            if args[:2] == ["iam", "get-policy"]:
                return (0, "{}", "")
            raise AssertionError(f"must not run {args[:2]} when the boundary exists")

        monkeypatch.setattr(
            aws,
            "checked_json",
            lambda args, *a, **k: _boundary_verify_json(args, iam.crew_boundary_policy_document()),
        )
        monkeypatch.setattr(aws, "run_aws", fake_run)
        assert source.ensure_crew_boundary("dev", "us-east-1") == iam.crew_boundary_arn(_ACCT12)
        assert ["iam", "create-policy"] not in calls

    def test_a_permissive_boundary_seeded_at_the_name_fails_closed(self, monkeypatch):
        """The control that makes create-once trustworthy, pinned for THIS lane.

        Inheriting the sequence is not the same as being covered by it, so this
        asserts the crew lane's own refusal: drop its verify call and this reddens.
        """
        monkeypatch.setattr(source, "_account_id", lambda *a: _ACCT12)
        monkeypatch.setattr(aws, "run_aws", lambda args, *a, **k: (0, "{}", ""))
        permissive = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
        }
        monkeypatch.setattr(
            aws, "checked_json", lambda args, *a, **k: _boundary_verify_json(args, permissive)
        )
        with pytest.raises(aws.AWSError, match="does NOT match"):
            source.ensure_crew_boundary("dev", "us-east-1")

    def test_the_ec2_ceiling_is_not_accepted_as_this_ones_content(self, monkeypatch):
        """A ceiling above the floor caps nothing, so it is refused here too.

        The EC2 document is a real, kirocrew-authored, non-permissive policy, which
        is what makes it the interesting mismatch: a lane that checked "some
        kirocrew boundary is present" rather than "this exact content" would accept
        it and cap a four-action role with a twenty-action ceiling.
        """
        from kiro_crew.cloud import iam

        monkeypatch.setattr(source, "_account_id", lambda *a: _ACCT12)
        monkeypatch.setattr(aws, "run_aws", lambda args, *a, **k: (0, "{}", ""))
        monkeypatch.setattr(
            aws,
            "checked_json",
            lambda args, *a, **k: _boundary_verify_json(
                args, iam.boundary_policy_document(_ACCT12)
            ),
        )
        with pytest.raises(aws.AWSError, match="does NOT match"):
            source.ensure_crew_boundary("dev", "us-east-1")

    def test_a_lost_create_race_is_verified_before_it_counts_as_success(self, monkeypatch):
        from kiro_crew.cloud import iam

        monkeypatch.setattr(source, "_account_id", lambda *a: _ACCT12)

        def fake_run(args, *a, **k):
            if args[:2] == ["iam", "get-policy"]:
                return (255, "", "NoSuchEntity")
            return (255, "", "EntityAlreadyExists: policy already exists")

        monkeypatch.setattr(
            aws,
            "checked_json",
            lambda args, *a, **k: _boundary_verify_json(args, iam.crew_boundary_policy_document()),
        )
        monkeypatch.setattr(aws, "run_aws", fake_run)
        assert source.ensure_crew_boundary("dev", "us-east-1") == iam.crew_boundary_arn(_ACCT12)

    def test_a_lost_race_against_a_permissive_policy_still_fails_closed(self, monkeypatch):
        monkeypatch.setattr(source, "_account_id", lambda *a: _ACCT12)

        def fake_run(args, *a, **k):
            if args[:2] == ["iam", "get-policy"]:
                return (255, "", "NoSuchEntity")
            return (255, "", "EntityAlreadyExists: policy already exists")

        permissive = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
        }
        monkeypatch.setattr(
            aws, "checked_json", lambda args, *a, **k: _boundary_verify_json(args, permissive)
        )
        monkeypatch.setattr(aws, "run_aws", fake_run)
        with pytest.raises(aws.AWSError, match="does NOT match"):
            source.ensure_crew_boundary("dev", "us-east-1")

    def test_it_refuses_before_touching_iam_without_an_account_id(self, monkeypatch):
        monkeypatch.setattr(source, "_account_id", lambda *a: "")

        def _boom(*a, **k):  # pragma: no cover - must not reach AWS
            raise AssertionError("must not touch IAM without a resolved account id")

        monkeypatch.setattr(aws, "run_aws", _boom)
        with pytest.raises(aws.AWSError, match="account id"):
            source.ensure_crew_boundary("dev", "us-east-1")

    def test_both_lanes_verify_through_the_same_function(self, monkeypatch):
        """The extraction, asserted behaviourally rather than by reading the source.

        A copied core would satisfy every other test in this class while leaving two
        places for the fail-closed comparison to drift apart. Patching the ONE
        function and seeing both lanes route through it is what rules that out, and
        checking the documents differ is what stops a shared core from being shared
        by handing both lanes the same expected content.
        """
        from kiro_crew.cloud import iam

        monkeypatch.setattr(source, "_account_id", lambda *a: _ACCT12)
        monkeypatch.setattr(aws, "run_aws", lambda args, *a, **k: (0, "{}", ""))
        seen = []
        monkeypatch.setattr(
            source,
            "_verify_boundary_content",
            lambda arn, name, expected, profile, region: seen.append((arn, name, expected)),
        )

        source.ensure_instance_boundary("dev", "us-east-1")
        source.ensure_crew_boundary("dev", "us-east-1")
        source.ensure_crew_exec_boundary("dev", "us-east-1")

        assert [name for _, name, _ in seen] == [
            iam.BOUNDARY_NAME,
            iam.CREW_BOUNDARY_NAME,
            iam.CREW_EXEC_BOUNDARY_NAME,
        ]
        assert [arn for arn, _, _ in seen] == [
            iam.boundary_arn(_ACCT12),
            iam.crew_boundary_arn(_ACCT12),
            iam.crew_exec_boundary_arn(_ACCT12),
        ]
        assert seen[0][2] == iam.boundary_policy_document(_ACCT12)
        assert seen[1][2] == iam.crew_boundary_policy_document()
        assert seen[2][2] == iam.crew_exec_boundary_policy_document()
        documents = [str(sorted(d.items())) for _, _, d in seen]
        assert len(set(documents)) == 3, "two lanes were handed the same ceiling"


_ACCT = "123456789012"
_BUCKET = f"kirocrew-src-{_ACCT}-us-east-1"


class TestAccountFromBucket:
    def test_extracts_12_digit_account(self):
        assert source._account_from_bucket(_BUCKET) == _ACCT
        # region with hyphens doesn't confuse the first-field split
        assert source._account_from_bucket("kirocrew-src-123456789012-ap-south-1") == "123456789012"

    def test_rejects_unknown_fallback_and_bad_shapes(self):
        # The `kirocrew-src-unknown-*` fallback (account didn't resolve) yields ""
        # so callers fail closed instead of pinning a bogus owner.
        assert source._account_from_bucket("kirocrew-src-unknown-us-east-1") == ""
        assert source._account_from_bucket("kirocrew-src-123-us-east-1") == ""  # too short
        assert source._account_from_bucket("some-other-bucket") == ""


class TestUploadDelete:
    def test_upload_source(self, monkeypatch, tmp_path):
        monkeypatch.setattr(source, "ensure_bucket", lambda *a: _BUCKET)
        fake_tar = tmp_path / "src.tar.gz"
        fake_tar.write_bytes(b"x")
        monkeypatch.setattr(source, "build_source_tarball", lambda *a, **k: fake_tar)
        cp = {}
        monkeypatch.setattr(
            aws,
            "checked",
            lambda args, *a, action="", **k: cp.update(args=args, action=action) or "",
        )
        bucket, key = source.upload_source("kc-1", "dev", "us-east-1")
        assert bucket == _BUCKET
        assert key == "kc-1/kirocrew-src.tar.gz"
        # low-level s3api put-object (only it accepts --expected-bucket-owner)
        assert cp["args"][:2] == ["s3api", "put-object"]
        assert "--bucket" in cp["args"] and bucket in cp["args"]
        assert "--key" in cp["args"] and key in cp["args"]
        assert "--body" in cp["args"] and str(fake_tar) in cp["args"]
        assert cp["action"] == "s3:PutObject"
        # anti-squat: pin derived from the bucket name (NOT a 2nd sts call), so a
        # transient sts "" can't silently drop it.
        assert "--expected-bucket-owner" in cp["args"]
        assert _ACCT in cp["args"]
        assert not fake_tar.exists()  # cleaned up

    def test_upload_source_fails_closed_when_owner_underivable(self, monkeypatch, tmp_path):
        # If the bucket name isn't the expected shape (account unresolved →
        # `kirocrew-src-unknown-*`), we must NOT upload without an owner pin.
        monkeypatch.setattr(source, "ensure_bucket", lambda *a: "kirocrew-src-unknown-us-east-1")

        def _boom_build(*a, **k):  # pragma: no cover - must not build/upload
            raise AssertionError("must not build/upload without an owner pin")

        monkeypatch.setattr(source, "build_source_tarball", _boom_build)
        monkeypatch.setattr(
            aws, "checked", lambda *a, **k: pytest.fail("must not call s3api put-object")
        )
        with pytest.raises(aws.AWSError, match="expected-bucket-owner"):
            source.upload_source("kc-1", "dev", "us-east-1")

    def test_delete_source(self, monkeypatch):
        monkeypatch.setattr(source, "bucket_name", lambda *a: _BUCKET)
        rm = {}
        monkeypatch.setattr(
            aws, "run_aws", lambda args, *a, **k: rm.update(args=args) or (0, "", "")
        )
        res = source.delete_source("kc-1", "dev", "us-east-1")
        # low-level s3api delete-object (only it accepts --expected-bucket-owner)
        assert rm["args"][:2] == ["s3api", "delete-object"]
        assert "--bucket" in rm["args"] and _BUCKET in rm["args"]
        assert "--key" in rm["args"] and "kc-1/kirocrew-src.tar.gz" in rm["args"]
        assert "--expected-bucket-owner" in rm["args"] and _ACCT in rm["args"]
        assert res["removed"] is True
        assert res["error"] == ""
        assert res["uri"] == f"s3://{_BUCKET}/kc-1/kirocrew-src.tar.gz"

    def test_delete_source_fails_closed_when_owner_underivable(self, monkeypatch):
        # bucket_name fell back to unknown → skip the unpinned delete, report it.
        monkeypatch.setattr(source, "bucket_name", lambda *a: "kirocrew-src-unknown-us-east-1")
        monkeypatch.setattr(
            aws, "run_aws", lambda *a, **k: pytest.fail("must not issue unpinned delete")
        )
        res = source.delete_source("kc-1", "dev", "us-east-1")
        assert res["removed"] is False
        assert "expected-bucket-owner" in res["error"]

    def test_delete_source_surfaces_failure(self, monkeypatch):
        # A non-zero `s3 rm` (denied / wrong bucket) must be reported, not
        # swallowed — teardown otherwise leaves a private tarball billing.
        monkeypatch.setattr(source, "bucket_name", lambda *a: _BUCKET)
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (255, "", "AccessDenied"))
        res = source.delete_source("kc-1", "dev", "us-east-1")
        assert res["removed"] is False
        assert "AccessDenied" in res["error"]
