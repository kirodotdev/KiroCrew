"""Unit tests for source shipping (cloud/source.py)."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from kiro_crew.cloud import aws, source


class TestRepoRoot:
    def test_repo_root_has_install_sh(self):
        root = source.repo_root()
        assert (root / "install.sh").exists()
        assert (root / "setup.cfg").exists()

    def test_repo_root_fails_closed_when_no_marker(self, monkeypatch, tmp_path):
        # Installed as a wheel (no install.sh + setup.cfg above the module):
        # must raise, NOT fall back to an ancestor dir that could tar up
        # unrelated packages and ship them to S3.
        fake_module = tmp_path / "site-packages" / "kiro_crew" / "cloud" / "source.py"
        fake_module.parent.mkdir(parents=True)
        fake_module.write_text("# stub\n")
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
        # The fast path (git archive) is still used when the tree is clean —
        # the tarball is the git-archive output, passed through _inject_dist
        # (a no-op without a local static/dist, which tmp_path lacks).
        monkeypatch.setattr(source, "_tracked_tree_is_dirty", lambda root: False)

        def _fake_archive(root):
            out = tmp_path / "archive.tar.gz"
            with tarfile.open(out, "w:gz") as tf:
                payload = tmp_path / "app.py"
                payload.write_text("x=1\n")
                tf.add(payload, arcname="app.py")
            return out

        monkeypatch.setattr(source, "_use_git_archive", _fake_archive)
        tarball = source.build_source_tarball(tmp_path)
        try:
            with tarfile.open(tarball) as tf:
                assert "app.py" in tf.getnames()
        finally:
            tarball.unlink()


class TestInjectDist:
    """``_inject_dist`` ships the git-ignored prebuilt frontend in the tarball."""

    @staticmethod
    def _make_archive(tmp_path: Path, names: list[str]) -> Path:
        out = tmp_path / "in.tar.gz"
        with tarfile.open(out, "w:gz") as tf:
            for name in names:
                payload = tmp_path / "payload.txt"
                payload.write_text("x=1\n")
                tf.add(payload, arcname=name)
        return out

    @staticmethod
    def _make_dist(root: Path) -> Path:
        dist = root / "src" / "kiro_crew" / "static" / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<html>spa</html>\n")
        (dist / "assets" / "app.js").write_text("// bundle\n")
        return dist

    def test_dist_members_injected(self, tmp_path):
        self._make_dist(tmp_path)
        archive = self._make_archive(tmp_path, ["install.sh"])
        out = source._inject_dist(archive, tmp_path)
        try:
            with tarfile.open(out) as tf:
                names = tf.getnames()
            assert "install.sh" in names
            assert f"{source._DIST_PREFIX}/index.html" in names
            assert f"{source._DIST_PREFIX}/assets/app.js" in names
        finally:
            out.unlink(missing_ok=True)

    def test_no_dist_returns_archive_unchanged(self, tmp_path):
        archive = self._make_archive(tmp_path, ["install.sh"])
        assert source._inject_dist(archive, tmp_path) == archive
        assert archive.exists()  # not rewritten/removed

    def test_dist_symlinks_never_shipped(self, tmp_path):
        dist = self._make_dist(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("secret beyond the tree\n")
        link = dist / "linked.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation not permitted here")
        archive = self._make_archive(tmp_path, ["install.sh"])
        out = source._inject_dist(archive, tmp_path)
        try:
            with tarfile.open(out) as tf:
                names = tf.getnames()
            assert not any("linked.txt" in n for n in names)
            assert not any("outside.txt" in n for n in names)
        finally:
            out.unlink(missing_ok=True)

    def test_dist_secret_files_still_filtered(self, tmp_path):
        # allow_dist_under skips only the _EXCLUDE_DIRS ("dist") check — the
        # credential checks (.env, .npmrc, .pem) still apply inside the bundle.
        dist = self._make_dist(tmp_path)
        (dist / ".env").write_text("TOKEN=x\n")
        (dist / ".npmrc").write_text("//registry/:_authToken=x\n")
        (dist / "cert.pem").write_text("-----BEGIN-----\n")
        archive = self._make_archive(tmp_path, ["install.sh"])
        out = source._inject_dist(archive, tmp_path)
        try:
            with tarfile.open(out) as tf:
                names = tf.getnames()
            assert f"{source._DIST_PREFIX}/index.html" in names
            assert not any(n.endswith(".env") for n in names)
            assert not any(n.endswith(".npmrc") for n in names)
            assert not any(n.endswith(".pem") for n in names)
        finally:
            out.unlink(missing_ok=True)

    def test_exclude_filter_default_still_drops_dist(self):
        # The prefix allow is opt-in: without allow_dist_under, "dist" members
        # (e.g. node_modules/pkg/dist/junk.js) stay excluded as before.
        ti = tarfile.TarInfo("node_modules/pkg/dist/junk.js")
        assert source._exclude_filter(ti) is None
        assert source._exclude_filter(ti, allow_dist_under=source._DIST_PREFIX) is None
        ok = tarfile.TarInfo(f"{source._DIST_PREFIX}/index.html")
        assert source._exclude_filter(ok) is None  # default unchanged
        assert source._exclude_filter(ok, allow_dist_under=source._DIST_PREFIX) is ok

    def test_allow_dist_under_does_not_allow_sibling_prefix(self):
        # The allow is an exact prefix: a path under "dist-evil" is NOT under
        # "dist", so the allow must not rescue it. This one has no literal "dist"
        # part so it passes either way — the discriminating case is below.
        not_under = tarfile.TarInfo("src/kiro_crew/static/dist-evil/x.js")
        assert source._exclude_filter(not_under, allow_dist_under=source._DIST_PREFIX) is not_under
        # A member that IS under a "dist" dir but NOT the exact allowed prefix
        # must stay excluded even when the allow is active.
        other_dist = tarfile.TarInfo("website/node_modules/pkg/dist/junk.js")
        assert source._exclude_filter(other_dist, allow_dist_under=source._DIST_PREFIX) is None


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
