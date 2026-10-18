"""Unit tests for source shipping (cloud/source.py).

Where the tarball is staged (the launcher-owned staging directory and its ancestor walk,
the Windows arm included) is covered in ``test_cloud_source_staging.py``.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path

import pytest

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

        with raw.open("rb") as raw_fh:
            filtered = source._refilter_archive(raw_fh)
        try:
            with tarfile.open(filtered) as tf:
                names = tf.getnames()
            assert names == ["src/app.py"]
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
        with bad.open("rb") as bad_fh:
            with pytest.raises(_tf.TarError):
                _src._refilter_archive(bad_fh)
        assert created, "NamedTemporaryFile was never called"
        leaked = [p for p in created if Path(p).exists()]
        assert not leaked, f"refilter leaked temp file(s): {leaked}"

    def test_dirty_tree_uses_working_tree_not_git_archive(self, monkeypatch, tmp_path):
        # A dirty tracked working tree must NOT be packaged via `git archive HEAD`
        # (which ships stale committed code). build_source_tarball must switch to
        # the ls-files tar path so uncommitted edits are shipped.
        monkeypatch.setattr(source, "_tracked_tree_is_dirty", lambda root: True)

        def _archive_must_not_run(root, **k):  # pragma: no cover - must not be called
            raise AssertionError("git archive must be skipped for a dirty tree")

        monkeypatch.setattr(source, "_use_git_archive", _archive_must_not_run)
        (tmp_path / "app.py").write_text("x=2  # edited, uncommitted\n")
        monkeypatch.setattr(source, "_git_tracked_files", lambda root: ["app.py"])
        staged = source.build_source_tarball(tmp_path)
        try:
            with tarfile.open(staged.path) as tf:
                data = tf.extractfile("app.py").read().decode()
            assert "edited, uncommitted" in data  # working-tree content, not HEAD
        finally:
            staged.path.unlink()

    def test_clean_tree_prefers_git_archive(self, monkeypatch, tmp_path):
        # The fast path (git archive) is still used when the tree is clean.
        monkeypatch.setattr(source, "_tracked_tree_is_dirty", lambda root: False)
        sentinel = tmp_path / "archive.tar.gz"
        sentinel.write_bytes(b"x")
        monkeypatch.setattr(source, "_use_git_archive", lambda root, **k: sentinel)
        assert source.build_source_tarball(tmp_path).path == sentinel


class TestSourceChecksumPin:
    """The digest ``upload_source`` hands to S3 as ``--checksum-sha256``.

    The ancestor walk can only inspect the staged path BEFORE the AWS CLI re-opens
    it, which is the wrong side of the substitution window. This checksum is the
    check that sits on the other side: S3 compares it against the bytes it
    actually received, so a swapped tarball is a refused upload. For that to hold,
    the digest must describe the bytes this process WROTE -- a digest taken from a
    later read of the path would describe whatever the name resolves to then,
    which is the substitution itself.
    """

    @staticmethod
    def _b64_sha256_of(path: Path) -> str:
        import base64
        import hashlib

        return base64.b64encode(hashlib.sha256(path.read_bytes()).digest()).decode("ascii")

    def test_fallback_digest_matches_the_bytes_on_disk(self, monkeypatch, tmp_path):
        (tmp_path / "app.py").write_text("x = 1\n")
        monkeypatch.setattr(source, "_git_tracked_files", lambda root: ["app.py"])
        monkeypatch.setattr(source, "_tracked_tree_is_dirty", lambda root: True)
        staged = source.build_source_tarball(tmp_path)
        try:
            assert staged.sha256 == self._b64_sha256_of(staged.path)
        finally:
            staged.path.unlink(missing_ok=True)

    def test_refilter_digest_matches_the_bytes_on_disk(self, tmp_path):
        # The git-archive path's FINAL writer is _refilter_archive, so that is
        # where its digest has to come from.
        import hashlib

        src_tar = tmp_path / "in.tar.gz"
        member = tmp_path / "keep.py"
        member.write_text("y = 2\n")
        with tarfile.open(src_tar, "w:gz") as tf:
            tf.add(member, arcname="keep.py")
        digest = hashlib.sha256()
        with src_tar.open("rb") as src_fh:
            out = source._refilter_archive(src_fh, digest=digest)
        try:
            import base64

            got = base64.b64encode(digest.digest()).decode("ascii")
            assert got == self._b64_sha256_of(out)
        finally:
            out.unlink(missing_ok=True)

    def test_digest_is_base64_not_hex(self, monkeypatch, tmp_path):
        # S3 rejects a hex digest for x-amz-checksum-sha256; it wants Base64 of
        # the 32 raw bytes. A hex string would be a silently wrong pin.
        import base64

        (tmp_path / "app.py").write_text("z = 3\n")
        monkeypatch.setattr(source, "_git_tracked_files", lambda root: ["app.py"])
        monkeypatch.setattr(source, "_tracked_tree_is_dirty", lambda root: True)
        staged = source.build_source_tarball(tmp_path)
        try:
            assert len(base64.b64decode(staged.sha256, validate=True)) == 32
            assert len(staged.sha256) == 44 and staged.sha256.endswith("=")
        finally:
            staged.path.unlink(missing_ok=True)

    def test_a_rewritten_tarball_no_longer_matches_its_pin(self, monkeypatch, tmp_path):
        # The whole point, stated as behaviour: change the staged bytes after the
        # build and the pin stops describing them, which is what makes S3 refuse.
        (tmp_path / "app.py").write_text("real = 1\n")
        monkeypatch.setattr(source, "_git_tracked_files", lambda root: ["app.py"])
        monkeypatch.setattr(source, "_tracked_tree_is_dirty", lambda root: True)
        staged = source.build_source_tarball(tmp_path)
        try:
            staged.path.write_bytes(b"substituted tarball")
            assert staged.sha256 != self._b64_sha256_of(staged.path)
        finally:
            staged.path.unlink(missing_ok=True)

    def test_the_digest_never_re_reads_the_staged_file(self, monkeypatch, tmp_path):
        """The digest comes from the write, through a handle, never from the name.

        A read-back yields the SAME digest on a quiet host, so no value comparison
        can tell the two apart -- what separates them is that one resolves the path
        again and the other does not. Resolving it again is the whole bug: between
        the mint and the write the name can come to mean a different file, which is
        the substitution the pin exists to catch. So this asserts the writer resolves
        the staged name ZERO times, and that reading it is never attempted.
        """
        import builtins
        import hashlib

        dest = tmp_path / "out.tar.gz"
        real_open = builtins.open
        # Opened before the spy is installed, so the writer's own handle is not one
        # of the resolutions being counted.
        handle = real_open(dest, "w+b")
        modes: list[str] = []

        def _spy_open(file, mode="r", *a, **k):
            if str(file) == str(dest):
                modes.append(mode)
            return real_open(file, mode, *a, **k)

        def _no_read_bytes(self):  # pragma: no cover - must not be called
            raise AssertionError("the digest must not re-read the staged tarball")

        monkeypatch.setattr(builtins, "open", _spy_open)
        monkeypatch.setattr(Path, "read_bytes", _no_read_bytes)
        digest = hashlib.sha256()
        try:
            source._write_tar_gz(handle, lambda tar: None, digest)
        finally:
            monkeypatch.undo()
            handle.close()

        assert modes == [], f"the writer resolved the staged name: {modes}"
        # Control: the digest really was fed, so an empty `modes` is not passing
        # because nothing happened.
        assert digest.digest() != hashlib.sha256(b"").digest()
        assert digest.digest() == hashlib.sha256(dest.read_bytes()).digest()

    def test_a_link_planted_at_the_staged_name_takes_no_tarball_bytes(self, monkeypatch, tmp_path):
        """Tarball bytes land on the minted inode, never on what its name points at.

        The staging leaf sits under the data home, which a same-uid process can
        write, so the window between minting a temp and writing it is one in which
        that name can be replaced by a link. A writer that resolves the name again
        sends the whole source tarball through the link and onto its target -- a
        governance file such as ``security_policy.json``, whose absent or
        unparseable form resolves to the permissive default -- while the launch
        still reports success. Writing through the held descriptor puts the target
        out of reach: ``mkstemp`` creates the inode with ``O_CREAT|O_EXCL`` and the
        handle keeps pointing at it however the name is rebound.

        POSIX only, because planting the link means unlinking a name whose
        descriptor is still open, which Windows refuses outright -- there the
        rebind this pin simulates cannot be staged in the first place.
        """
        import tempfile as _tmp

        if os.name != "posix":
            pytest.skip("rebinding a name while its descriptor is open")
        ceiling = b'{"ceiling": "strict"}\n'
        victim = tmp_path / "security_policy.json"
        victim.write_bytes(ceiling)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("x = 1\n")
        monkeypatch.setattr(source, "_git_tracked_files", lambda root: ["app.py"])

        real_ntf = _tmp.NamedTemporaryFile
        planted: list[str] = []

        def _rebind_the_name_to_the_victim(*a, **kw):
            fh = real_ntf(*a, **kw)
            # Exactly the race: the name is rebound the instant after the mint,
            # while the caller still holds the descriptor.
            Path(fh.name).unlink()
            Path(fh.name).symlink_to(victim)
            planted.append(fh.name)
            return fh

        monkeypatch.setattr(_tmp, "NamedTemporaryFile", _rebind_the_name_to_the_victim)
        try:
            source._tar_fallback(repo)
        finally:
            monkeypatch.undo()
            for name in planted:
                Path(name).unlink(missing_ok=True)

        assert planted, "the mint was never intercepted -- the test is broken"
        assert (
            victim.read_bytes() == ceiling
        ), "tarball bytes were written through the planted link onto the victim"

    def test_every_tarball_writer_goes_through_the_hashing_writer(self):
        """No producer may write tarball bytes outside ``_write_tar_gz``.

        Read structurally (AST), not by grepping for text: a string needle would
        match this test's own source. A future third producer that opened a
        ``w:gz`` tarball directly would ship bytes the pin never saw, and S3 would
        then validate the CLI's re-read against a digest of a different file --
        so the shared writer is the invariant, not a convention.
        """
        import ast

        tree = ast.parse(Path(source.__file__).read_text(encoding="utf-8"))
        enclosing: dict[int, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    enclosing.setdefault(id(child), node.name)

        writes: list[str] = []
        reads = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "open"):
                continue
            if not (isinstance(fn.value, ast.Name) and fn.value.id == "tarfile"):
                continue
            modes = [a.value for a in node.args if isinstance(a, ast.Constant)]
            modes += [
                k.value.value
                for k in node.keywords
                if k.arg == "mode" and isinstance(k.value, ast.Constant)
            ]
            mode = next((m for m in modes if isinstance(m, str)), "")
            if mode.startswith("w"):
                writes.append(enclosing.get(id(node), "<module>"))
            elif mode.startswith("r"):
                reads += 1

        # Controls: the walker must actually be finding calls in both directions,
        # so an empty offender list means the invariant holds rather than that the
        # scan matched nothing.
        assert writes, "found no tarfile write-mode open at all -- the scan is broken"
        assert reads, "found no tarfile read-mode open at all -- the scan is broken"
        assert set(writes) == {"_write_tar_gz"}, f"tarball bytes written outside the pin: {writes}"

    def test_a_failed_archive_attempt_does_not_contaminate_the_fallback_digest(
        self, monkeypatch, tmp_path
    ):
        """A digest must never span two tarball attempts.

        ``_use_git_archive`` SWALLOWS a mid-write ``_refilter_archive`` failure so it
        can fall through to the tarfile fallback. By then the gzip header has already
        gone through ``_HashingWriter``, so ONE shared accumulator would fold those
        discarded bytes into the fallback's checksum -- the pin would describe bytes
        the named file does not contain, and S3 would refuse a perfectly good
        tarball.

        Real ``_use_git_archive`` and real ``_refilter_archive`` run here. Only
        ``git archive`` itself is stubbed, and it is stubbed to SUCCEED with a corrupt
        archive, which is what makes the re-filter raise after it has begun writing.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("x = 1\n")
        monkeypatch.setattr(source, "_git_tracked_files", lambda root: ["app.py"])
        # A clean tree, so the git-archive attempt is the one that runs first.
        monkeypatch.setattr(source, "_tracked_tree_is_dirty", lambda root: False)
        real_run = subprocess.run

        def _git_archive_writes_a_corrupt_tarball(argv, **kw):
            if argv[:1] == ["git"] and "archive" in argv:
                # The real command writes to stdout, which is the caller's held handle.
                kw["stdout"].write(b"not a gzip stream")
                kw["stdout"].flush()
                return subprocess.CompletedProcess(argv, 0, "", "")
            return real_run(argv, **kw)

        monkeypatch.setattr(subprocess, "run", _git_archive_writes_a_corrupt_tarball)

        staged = source.build_source_tarball(repo)
        try:
            assert staged.sha256 == self._b64_sha256_of(staged.path)
        finally:
            staged.path.unlink(missing_ok=True)

    def test_each_tarball_attempt_gets_its_own_digest(self, monkeypatch, tmp_path):
        """The same property stated as identity, so it holds however an attempt fails.

        ``_use_git_archive`` dirtying the accumulator it was handed and then returning
        ``None`` is the reachable case, but any future producer that fails part way is
        the same hazard. What keeps the checksum honest is that no accumulator is ever
        read for an attempt other than the one that filled it.
        """
        (tmp_path / "app.py").write_text("x = 1\n")
        monkeypatch.setattr(source, "_git_tracked_files", lambda root: ["app.py"])
        monkeypatch.setattr(source, "_tracked_tree_is_dirty", lambda root: False)
        handed: list[object] = []
        real_fallback = source._tar_fallback

        def _dirties_its_digest_then_gives_up(root, *, digest=None):
            handed.append(digest)
            if digest is not None:
                digest.update(b"bytes from an attempt that produced no file")
            return None

        def _recording_fallback(root, *, digest=None):
            handed.append(digest)
            return real_fallback(root, digest=digest)

        monkeypatch.setattr(source, "_use_git_archive", _dirties_its_digest_then_gives_up)
        monkeypatch.setattr(source, "_tar_fallback", _recording_fallback)

        staged = source.build_source_tarball(tmp_path)
        try:
            assert len(handed) == 2, f"expected two attempts, got {len(handed)}: {handed}"
            # Control: both attempts really were handed an accumulator, so the
            # identity check below is comparing two digests rather than two Nones.
            assert handed[0] is not None and handed[1] is not None
            assert handed[0] is not handed[1], "the fallback reused the failed attempt's digest"
            assert staged.sha256 == self._b64_sha256_of(staged.path)
        finally:
            staged.path.unlink(missing_ok=True)


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

    def test_read_denied_names_the_read_verb_and_skips_create(self, monkeypatch):
        # A DENIED get-policy is not an absent boundary. Reaching create-policy
        # here would surface the CREATE verb as the missing grant, sending an
        # operator who lacks only the read to grant iam:CreatePolicy — a write they
        # do not want and which does not fix the launch. The read verb is named and
        # create is never attempted.
        monkeypatch.setattr(source, "_account_id", lambda *a: _ACCT12)
        calls = []

        def fake_run(args, *a, **k):
            calls.append(list(args[:2]))
            if args[:2] == ["iam", "get-policy"]:
                return (
                    255,
                    "",
                    "An error occurred (AccessDenied): User: "
                    "arn:aws:sts::123456789012:assumed-role/launcher/s is not "
                    "authorized to perform: iam:GetPolicy on resource: "
                    "arn:aws:iam::123456789012:policy/kirocrew-ec2-boundary",
                )
            raise AssertionError(f"must not run {args[:2]} after a denied read")

        monkeypatch.setattr(aws, "run_aws", fake_run)
        with pytest.raises(aws.AWSError, match="iam:GetPolicy") as excinfo:
            source.ensure_instance_boundary("dev", "us-east-1")
        assert excinfo.value.missing_action == "iam:GetPolicy"
        assert "iam:CreatePolicy" not in str(excinfo.value)
        assert ["iam", "create-policy"] not in calls

    def test_absent_boundary_still_falls_through_to_create(self, monkeypatch):
        # The denied-read branch must not swallow the ordinary absent case: a
        # boundary that does not exist answers NoSuchEntity, not AccessDenied, and
        # is created as before.
        from kiro_crew.cloud import iam

        monkeypatch.setattr(source, "_account_id", lambda *a: _ACCT12)
        calls = []

        def fake_run(args, *a, **k):
            calls.append(list(args[:2]))
            if args[:2] == ["iam", "get-policy"]:
                return (255, "", "An error occurred (NoSuchEntity): cannot be found.")
            return (0, "{}", "")

        monkeypatch.setattr(aws, "run_aws", fake_run)
        assert source.ensure_instance_boundary("dev", "us-east-1") == iam.boundary_arn(_ACCT12)
        assert ["iam", "create-policy"] in calls

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
        monkeypatch.setattr(
            source, "build_source_tarball", lambda *a, **k: source.StagedSource(fake_tar, "D1G3ST=")
        )
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
        # The build-time digest is handed to S3, so a tarball substituted between
        # our close and the CLI's re-open is refused rather than stored.
        assert cp["args"][cp["args"].index("--checksum-sha256") + 1] == "D1G3ST="
        # NOT --checksum-algorithm: that asks the CLI to hash what it re-opened,
        # which would certify a substitution instead of catching it.
        assert "--checksum-algorithm" not in cp["args"]
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
