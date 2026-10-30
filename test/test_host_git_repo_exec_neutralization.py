"""Host-side git over an agent-writable checkout runs no program the repo names.

Each test plants a MARKER program in a real repository's own config (and, for
hooks, in its ``.git/hooks``), drives one host-side caller over it, and asserts
the marker never ran. The planted keys are the ones measured to execute on the
commands these callers run (git 2.47):

* ``core.fsmonitor`` -- ``status``, a worktree ``diff``, ``ls-files``, ``fetch``,
  ``merge`` and ``reset`` spawn it.
* ``.git/hooks/*`` -- ``post-index-change`` (``status``/``diff`` refresh the
  index), ``reference-transaction`` (``fetch``/``reset``), ``post-merge``.
* ``filter.<name>.clean``/``.smudge`` bound by an in-tree ``.gitattributes`` --
  ``status``, a worktree ``diff``, ``archive`` and ``reset``. No environment pin
  reaches a repo-named key, so these callers REFUSE (or fall back) instead.
* ``diff.<name>.textconv`` -- a content ``diff``/``show``.

POSIX only: the markers are shell scripts.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from git_maintenance_helpers import git_maintenance_off_entries

pytestmark = [
    pytest.mark.skipif(os.name == "nt", reason="marker programs are POSIX shell scripts"),
    pytest.mark.skipif(shutil.which("git") is None, reason="git not available"),
]

_HOOKS = (
    "post-index-change",
    "reference-transaction",
    "post-merge",
    "post-checkout",
    "pre-commit",
)


def _g(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@t",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@t",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    env.update(git_maintenance_off_entries(env))
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, check=True, timeout=30
    )
    return done.stdout.decode().strip()


def _marker_programs(tmp: Path) -> tuple[Path, Path]:
    """``(marker_file, program)``: the program appends its argv to the marker."""
    marker = tmp / "MARKER"
    prog = tmp / "planted.sh"
    # Passes stdin through (a filter must answer), or cats a file argument
    # (textconv). Either way it RECORDS that it ran, which is the assertion.
    prog.write_text(
        "#!/bin/sh\n"
        f'echo "$0 $*" >> "{marker}"\n'
        'if [ -f "$1" ]; then cat "$1"; else cat; fi\n'
    )
    prog.chmod(0o755)
    return marker, prog


def _plant(repo: Path, prog: Path, *, drivers: bool) -> None:
    """Plant fixed-key exec vectors, plus repo-NAMED drivers when *drivers*."""
    _g(repo, "config", "core.fsmonitor", str(prog))
    _g(repo, "config", "core.pager", str(prog))
    _g(repo, "config", "diff.external", str(prog))
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in _HOOKS:
        hook = hooks / name
        hook.write_text(f'#!/bin/sh\nexec "{prog}" hook-{name} </dev/null\n')
        hook.chmod(0o755)
    if drivers:
        _g(repo, "config", "filter.pwn.clean", str(prog))
        _g(repo, "config", "filter.pwn.smudge", str(prog))
        _g(repo, "config", "diff.pwn.textconv", str(prog))


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _g(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("one\n")
    (path / "website").mkdir()
    (path / "website" / "index.js").write_text("w\n")
    _g(path, "add", "-A")
    _g(path, "commit", "-qm", "one")
    return path


def _with_attributes(repo: Path) -> None:
    """Bind every path to the planted drivers through an in-tree file."""
    (repo / ".gitattributes").write_text("* filter=pwn diff=pwn\n")
    _g(repo, "add", ".gitattributes")
    _g(repo, "commit", "-qm", "attrs")


def _ran(marker: Path) -> str:
    return marker.read_text() if marker.exists() else ""


def _prove_planted_vector_fires(repo: Path, marker: Path, *args: str) -> None:
    """The fixture is live: an UNPINNED git runs the marker. Then reset it."""
    subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=30)
    assert _ran(marker), f"fixture is inert: bare `git {' '.join(args)}` ran no marker"
    marker.unlink()


# ---------------------------------------------------------------- shared helper


class TestRepoExecDriverReason:
    def test_names_a_planted_filter_and_is_clean_otherwise(self, tmp_path):
        from kiro_crew.platform.update_governance import repo_exec_driver_reason

        _, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        assert repo_exec_driver_reason(str(repo)) == ""
        _plant(repo, prog, drivers=False)
        # Fixed-name keys are pinned, not refused.
        assert repo_exec_driver_reason(str(repo)) == ""
        _g(repo, "config", "filter.pwn.clean", str(prog))
        assert "filter.pwn.clean" in repo_exec_driver_reason(str(repo))
        # Answered from a subdirectory too: read-only callers run below the top.
        assert "filter.pwn.clean" in repo_exec_driver_reason(str(repo / "website"))

    def test_an_empty_named_filter_is_refused_too(self, tmp_path):
        # Git accepts `[filter ""]` (key `filter..clean`) bound by `filter=`.
        from kiro_crew.dashboard.handlers.worktree import _FILTER_KEY_RE
        from kiro_crew.platform.update_governance import (
            repo_exec_config_reason,
            repo_exec_driver_reason,
        )

        marker, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        _g(repo, "config", "filter..clean", str(prog))
        (repo / ".gitattributes").write_text("* filter=\n")
        (repo / "a.txt").write_text("edited\n")
        _prove_planted_vector_fires(repo, marker, "diff", "--no-ext-diff", "--no-textconv", "HEAD")

        assert "filter..clean" in repo_exec_driver_reason(str(repo))
        assert "filter..clean" in repo_exec_config_reason(str(repo))
        assert _FILTER_KEY_RE.match("filter..clean")
        assert _ran(marker) == ""

    @pytest.mark.asyncio
    async def test_dashboard_diff_refuses_an_empty_named_filter(self, tmp_path):
        marker, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        _g(repo, "config", "filter..clean", str(prog))
        (repo / ".gitattributes").write_text("* filter=\n")
        (repo / "a.txt").write_text("edited\n")

        body = await TestDashboardFileDiff._diff(repo / "a.txt")
        assert body["status"] == "error"
        assert _ran(marker) == ""

    def test_url_rewrite_is_not_a_read_only_refusal(self, tmp_path):
        from kiro_crew.platform.update_governance import (
            repo_exec_config_reason,
            repo_exec_driver_reason,
        )

        repo = _repo(tmp_path / "r")
        _g(repo, "config", "url.https://mirror.invalid/.insteadOf", "https://example.invalid/")
        assert repo_exec_driver_reason(str(repo)) == ""
        assert "url." in repo_exec_config_reason(str(repo))


def _with_nested_filter_repo(repo: Path, prog: Path) -> Path:
    """Commit an initialized nested repo as a gitlink; plant a filter in ITS config.

    `status`/worktree `diff` start a child git in it, which runs the nested repo's
    own `filter.<name>.clean` (measured on git 2.47) -- and the superproject's own
    config is clean, so a probe of only that config misses it.
    """
    nested = repo / "vendor" / "n"
    nested.mkdir(parents=True)
    _g(nested, "init", "-q")
    (nested / "x.txt").write_text("x\n")
    (nested / ".gitattributes").write_text("* filter=pwn\n")
    _g(nested, "add", "-A")
    _g(nested, "commit", "-qm", "n")
    _g(repo, "add", "vendor/n")
    _g(repo, "commit", "-qm", "gitlink")
    _g(nested, "config", "filter.pwn.clean", str(prog))
    # SAME size as the committed "x\n", with a changed mtime: git cannot call the
    # file modified from its stat, so it must hash it, and hashing runs the clean
    # filter. A size change lets git skip the hash, and the filter then runs only
    # on `.gitattributes`, and only while its index entry is racily clean -- a
    # timing accident a slow shard misses.
    (nested / "x.txt").write_text("y\n")
    os.utime(nested / "x.txt", (1, 1))
    return nested


class TestNestedRepoDrivers:
    def test_probes_see_a_driver_declared_in_a_nested_repo(self, tmp_path):
        from kiro_crew.platform.update_governance import (
            nested_repo_exec_driver_reason,
            repo_exec_config_reason,
            repo_exec_driver_reason,
        )

        marker, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        _with_nested_filter_repo(repo, prog)
        _prove_planted_vector_fires(repo, marker, "status", "--porcelain")

        assert "filter.pwn.clean" in repo_exec_driver_reason(str(repo))
        assert "filter.pwn.clean" in repo_exec_config_reason(str(repo))
        assert "filter.pwn.clean" in nested_repo_exec_driver_reason(str(repo))
        assert _ran(marker) == ""

    def test_the_nested_fixture_fires_without_a_racily_clean_index(self, tmp_path):
        # A nested index written well after every file it lists has no racily
        # clean entry, which is what a slow shard produces. The dirty file alone
        # must still reach the filter.
        marker, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        nested = _with_nested_filter_repo(repo, prog)
        later = (nested / ".gitattributes").stat().st_mtime + 60
        os.utime(nested / ".git" / "index", (later, later))
        _prove_planted_vector_fires(repo, marker, "status", "--porcelain")

    def test_nested_repos_are_found_in_a_redirected_work_tree(self, tmp_path):
        # `core.worktree` moves the tree git descends into; the nested repo lives
        # THERE, not under the directory the caller named.
        import shutil as _shutil

        from kiro_crew.platform.update_governance import repo_exec_driver_reason

        _, prog = _marker_programs(tmp_path)
        real = _repo(tmp_path / "real")
        _with_nested_filter_repo(real, prog)
        named = tmp_path / "named"
        named.mkdir()
        _shutil.copytree(real / ".git", named / ".git")
        _g(named, "config", "core.worktree", str(real))
        assert not (named / "vendor" / "n" / ".git").exists()

        assert "filter.pwn.clean" in repo_exec_driver_reason(str(named))

    def test_fingerprint_falls_back_instead_of_reaching_the_nested_filter(self, tmp_path):
        from kiro_crew import code_fingerprint

        marker, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        _with_nested_filter_repo(repo, prog)

        assert code_fingerprint._git_fingerprint(repo) is None
        assert _ran(marker) == ""


class TestTransportPins:
    @staticmethod
    def _pins(env: dict[str, str]) -> list[tuple[str, str]]:
        return [
            (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
            for i in range(int(env["GIT_CONFIG_COUNT"]))
        ]

    def test_upload_pack_pins_are_the_bare_names_an_ssh_server_accepts(self):
        # Over ssh the value is the REMOTE command; GitHub/GitLab accept only the
        # bare name, so an absolute local path broke every ssh fetch.
        from kiro_crew.platform.update_governance import git_command_env

        pins = dict(self._pins(git_command_env()))
        # Every signature verifier is pinned, not only OpenPGP: `archive`'s
        # export-subst `%G?` verifies an SSH-signed HEAD through gpg.ssh.program.
        for key in ("gpg.program", "gpg.ssh.program", "gpg.x509.program"):
            assert os.path.isabs(pins[key]) or pins[key] == os.devnull, key
        assert pins["remote.origin.uploadpack"] == "git-upload-pack"
        assert pins["remote.origin.receivepack"] == "git-receive-pack"

    def test_human_transport_leaves_the_users_own_config_to_git(self, monkeypatch, tmp_path):
        # For a human-run update the two transport keys are not pinned, so git
        # reads the operator's config natively: plain entries, an include, an
        # includeIf on this checkout, an empty reset and a per-URL helper.
        from kiro_crew.platform.update_governance import git_command_env

        repo = _repo(tmp_path / "r")
        extra = tmp_path / "extra.gitconfig"
        extra.write_text("[credential]\n\thelper =\n\thelper = from-include\n")
        scoped = tmp_path / "scoped.gitconfig"
        scoped.write_text("[core]\n\tsshCommand = ssh -i /home/me/.ssh/only-this-repo\n")
        user = tmp_path / "gitconfig"
        user.write_text(
            "[credential]\n\thelper = switched-off\n"
            f"[include]\n\tpath = {extra}\n"
            f'[includeIf "gitdir:{repo}/"]\n\tpath = {scoped}\n'
            '[credential "https://example.invalid"]\n\thelper = !gh-like\n'
        )
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(user))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        env = git_command_env(human_transport=True)
        keys = {k.lower() for k, _ in self._pins(env)}
        assert "credential.helper" not in keys and "core.sshcommand" not in keys
        assert "core.fsmonitor" in keys, "every other pin still applies"

        def _resolved(*args: str) -> list[str]:
            done = subprocess.run(
                ["git", "config", *args], cwd=repo, env=env, capture_output=True, timeout=30
            )
            return done.stdout.decode().splitlines()

        assert _resolved("--get-all", "credential.helper")[-2:] == ["", "from-include"]
        assert _resolved("--get", "core.sshCommand") == ["ssh -i /home/me/.ssh/only-this-repo"]
        assert _resolved("--get-urlmatch", "credential.helper", "https://example.invalid/x") == [
            "!gh-like"
        ]
        # The unattended default keeps both pins.
        plain = {k.lower() for k, _ in self._pins(git_command_env())}
        assert {"credential.helper", "core.sshcommand"} <= plain

    @pytest.mark.parametrize(
        "key,value", [("credential.helper", "repo-planted"), ("core.sshCommand", "evil-ssh")]
    )
    def test_human_transport_refuses_the_repos_own_transport_keys(self, tmp_path, key, value):
        from kiro_crew.platform.update_governance import repo_exec_config_reason

        repo = _repo(tmp_path / "r")
        _g(repo, "config", key, value)
        assert key.lower() in repo_exec_config_reason(str(repo), human_transport=True).lower()
        # Pinned on the unattended path, so not refused there.
        assert repo_exec_config_reason(str(repo)) == ""

    def test_probes_run_the_callers_git_when_no_trusted_git_exists(self, monkeypatch, tmp_path):
        # A host whose git is outside the trusted dirs: the probe runs the same bare
        # `git` the caller does, so a clean repo is clean and a driver is still found.
        from kiro_crew import platform_compat
        from kiro_crew.platform.update_governance import (
            repo_exec_config_reason,
            repo_exec_driver_reason,
        )

        _, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        monkeypatch.setattr(platform_compat, "trusted_git_bin", lambda: None)
        assert repo_exec_driver_reason(str(repo)) != "", "no git at all still refuses"
        assert repo_exec_driver_reason(str(repo), git="git") == ""
        assert repo_exec_config_reason(str(repo), git="git") == ""
        _g(repo, "config", "filter.pwn.clean", str(prog))
        assert "filter.pwn.clean" in repo_exec_driver_reason(str(repo), git="git")


# ---------------------------------------------------------------- code_fingerprint


class TestCodeFingerprint:
    def test_worktree_diff_runs_no_fixed_key_program(self, tmp_path):
        from kiro_crew import code_fingerprint

        marker, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        _plant(repo, prog, drivers=False)
        (repo / "a.txt").write_text("dirty\n")
        _prove_planted_vector_fires(repo, marker, "diff", "--no-ext-diff", "HEAD")

        got = code_fingerprint._git_fingerprint(repo)
        assert got is not None and "+" in got, "a dirty tree still fingerprints"
        assert _ran(marker) == ""

    def test_repo_named_filter_falls_back_without_running_it(self, tmp_path):
        from kiro_crew import code_fingerprint

        marker, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        _with_attributes(repo)
        _plant(repo, prog, drivers=True)
        (repo / "a.txt").write_text("dirty\n")
        _prove_planted_vector_fires(repo, marker, "diff", "--no-ext-diff", "--no-textconv", "HEAD")

        assert code_fingerprint._git_fingerprint(repo) is None
        assert _ran(marker) == ""


# ---------------------------------------------------------------- frontend fingerprint


class TestBuildSourceFingerprint:
    @staticmethod
    def _dist(repo: Path) -> Path:
        dist = repo / "src" / "kiro_crew" / "static" / "dist"
        dist.mkdir(parents=True)
        return dist

    def test_status_runs_no_fixed_key_program(self, tmp_path):
        from kiro_crew import frontend

        marker, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        dist = self._dist(repo)
        _plant(repo, prog, drivers=False)
        _prove_planted_vector_fires(repo, marker, "status", "--porcelain")

        frontend._write_build_source_fingerprint(repo, "git", lambda _m: None)
        assert (dist / frontend._BUILD_SOURCE_FINGERPRINT).read_text() == _g(
            repo, "rev-parse", "HEAD:website"
        )
        assert _ran(marker) == ""

    def test_repo_named_filter_skips_the_stamp(self, tmp_path):
        from kiro_crew import frontend

        marker, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        dist = self._dist(repo)
        _with_attributes(repo)
        _plant(repo, prog, drivers=True)
        os.utime(repo / "website" / "index.js", (1, 1))  # stat-dirty: clean filter consulted
        lines: list[str] = []

        frontend._write_build_source_fingerprint(repo, "git", lines.append)
        assert not (dist / frontend._BUILD_SOURCE_FINGERPRINT).exists()
        assert any("filter.pwn" in line for line in lines)
        assert _ran(marker) == ""


# ---------------------------------------------------------------- cloud source tarball


class TestCloudSourceTarball:
    def test_fixed_key_programs_do_not_run(self, tmp_path):
        from kiro_crew.cloud import source

        marker, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        (repo / "install.sh").write_text("#!/bin/sh\n")
        _g(repo, "add", "install.sh")
        _g(repo, "commit", "-qm", "marker")
        _plant(repo, prog, drivers=False)
        _prove_planted_vector_fires(repo, marker, "ls-files", "-z")

        staged = source.build_source_tarball(repo)
        try:
            assert staged.path.stat().st_size > 0
        finally:
            staged.path.unlink()
        assert _ran(marker) == ""

    def test_repo_named_archive_compressor_does_not_run(self, tmp_path):
        # `git archive --format=tar.gz` runs `tar.tar.gz.command`; the archive is
        # now taken as plain tar and compressed in Python.
        import tarfile

        from kiro_crew.cloud import source

        marker, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        _g(repo, "config", "tar.tar.gz.command", str(prog))
        _prove_planted_vector_fires(repo, marker, "archive", "--format=tar.gz", "HEAD")

        staged = source.build_source_tarball(repo)
        try:
            with tarfile.open(staged.path, "r:gz") as tf:
                assert "a.txt" in tf.getnames()
        finally:
            staged.path.unlink()
        assert _ran(marker) == ""

    def test_a_filtered_tree_ships_its_working_files_not_raw_blobs(self, tmp_path):
        # A global filter (e.g. LFS) is dropped by the isolated archive, which would
        # ship raw blobs; such a tree packages the checked-out working files.
        import tarfile

        from kiro_crew.cloud import source

        repo = _repo(tmp_path / "r")
        (repo / ".gitattributes").write_text("*.bin filter=lfsish\n")
        (repo / "big.bin").write_text("HYDRATED\n")
        _g(repo, "-c", "filter.lfsish.clean=sed s/HYDRATED/POINTER/", "add", "-A")
        _g(repo, "commit", "-qm", "filtered")
        staged = source.build_source_tarball(repo)
        try:
            with tarfile.open(staged.path) as tf:
                assert tf.extractfile("big.bin").read() == b"HYDRATED\n"
        finally:
            staged.path.unlink()

    def test_working_tree_packaging_keeps_export_ignore_out(self, tmp_path):
        # The filtered-tree path must leave out what `git archive` would.
        import tarfile

        from kiro_crew.cloud import source

        repo = _repo(tmp_path / "r")
        # `private export-ignore` names the DIRECTORY; check-attr does not pass it to
        # the files, git archive does.
        (repo / ".gitattributes").write_text("*.bin filter=lfsish\nprivate export-ignore\n")
        (repo / "big.bin").write_text("HYDRATED\n")
        (repo / "private").mkdir()
        (repo / "private" / "notes.py").write_text("x = 1\n")
        _g(repo, "-c", "filter.lfsish.clean=cat", "add", "-A")
        _g(repo, "commit", "-qm", "filtered")
        staged = source.build_source_tarball(repo)
        try:
            with tarfile.open(staged.path) as tf:
                names = tf.getnames()
            assert "big.bin" in names
            assert "private/notes.py" not in names
        finally:
            staged.path.unlink()

    def test_working_tree_packaging_keeps_a_directory_only_export_ignore_out(self, tmp_path):
        # `private/ export-ignore` matches only the slashed form in check-attr.
        import tarfile

        from kiro_crew.cloud import source

        repo = _repo(tmp_path / "r")
        (repo / ".gitattributes").write_text("*.bin filter=lfsish\nprivate/ export-ignore\n")
        (repo / "big.bin").write_text("HYDRATED\n")
        (repo / "private" / "sub").mkdir(parents=True)
        (repo / "private" / "sub" / "notes.py").write_text("x = 1\n")
        _g(repo, "-c", "filter.lfsish.clean=cat", "add", "-A")
        _g(repo, "commit", "-qm", "filtered")
        archived = subprocess.run(
            ["git", "archive", "HEAD"], cwd=repo, capture_output=True, check=True
        ).stdout
        assert b"private/sub/notes.py" not in archived
        staged = source.build_source_tarball(repo)
        try:
            with tarfile.open(staged.path) as tf:
                names = tf.getnames()
            assert "big.bin" in names
            assert "private/sub/notes.py" not in names
        finally:
            staged.path.unlink()

    def test_repo_named_filter_skips_archive_and_status(self, tmp_path):
        import tarfile

        from kiro_crew.cloud import source

        marker, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        _with_attributes(repo)
        _plant(repo, prog, drivers=True)
        _prove_planted_vector_fires(repo, marker, "archive", "--format=tar", "HEAD")

        staged = source.build_source_tarball(repo)
        try:
            with tarfile.open(staged.path) as tf:
                assert "a.txt" in tf.getnames()
        finally:
            staged.path.unlink()
        assert _ran(marker) == ""


# ---------------------------------------------------------------- dashboard file diff


class TestDashboardFileDiff:
    @staticmethod
    async def _diff(path: Path) -> dict:
        from unittest.mock import MagicMock, patch

        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request
        from dashboard_owner_helpers import as_owner

        from kiro_crew.dashboard.handlers.files import api_file_diff

        req = make_mocked_request(
            "GET", f"/api/file-diff?path={path}", app=as_owner(web.Application())
        )
        req["user"] = "local-app"
        req["app"] = ""
        with patch("kiro_crew.dashboard.handlers.files._sel", return_value=MagicMock()):
            resp = await api_file_diff(req)
        assert resp.status == 200
        return json.loads(resp.body)

    @pytest.mark.asyncio
    async def test_fixed_key_programs_and_hooks_do_not_run(self, tmp_path):
        marker, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        _plant(repo, prog, drivers=False)
        # Untracked, so the handler reaches its `git status` probe.
        (repo / "new.txt").write_text("two\n")
        # Make a tracked file stale relative to the index so git must re-read it
        # during status, refresh the cached stat entry, and fire post-index-change.
        # Without this, a fresh commit leaves every cached stat matching, git skips
        # the re-check, and post-index-change never fires on fast runners.
        os.utime(repo / "a.txt", (1, 1))
        # The editor's own `-c` flags pin fsmonitor; hooks were the open half.
        _prove_planted_vector_fires(repo, marker, "-c", "core.fsmonitor=", "status", "--porcelain")

        body = await self._diff(repo / "new.txt")
        assert body["status"] == "untracked"
        assert "+two" in body["diff"]
        assert _ran(marker) == ""

    @pytest.mark.asyncio
    async def test_repo_named_filter_gets_no_diff(self, tmp_path):
        marker, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        _with_attributes(repo)
        _plant(repo, prog, drivers=True)
        (repo / "a.txt").write_text("two\n")

        body = await self._diff(repo / "a.txt")
        assert body["status"] == "error"
        assert _ran(marker) == ""

    @pytest.mark.asyncio
    async def test_probe_checks_the_repo_the_diff_runs_in(self, tmp_path):
        # A trailing-space checkout beside a clean sibling: the stripped toplevel
        # names the sibling, the diff runs in the poisoned one.
        marker, prog = _marker_programs(tmp_path)
        _repo(tmp_path / "r")
        poisoned = _repo(tmp_path / "r ")
        _with_attributes(poisoned)
        _plant(poisoned, prog, drivers=True)
        (poisoned / "a.txt").write_text("two\n")

        body = await self._diff(poisoned / "a.txt")
        assert body["status"] == "error"
        assert _ran(marker) == ""

    @pytest.mark.asyncio
    async def test_partial_clone_lazy_fetch_runs_no_repo_upload_pack(self, tmp_path):
        # `git show HEAD:<file>` lazy-fetches a missing blob from the promisor, and
        # git runs the repository's `remote.<n>.uploadpack` (the first one read).
        marker, _ = _marker_programs(tmp_path)
        prog = tmp_path / "planted-upload-pack.sh"
        prog.write_text(f'#!/bin/sh\necho "$0 $*" >> "{marker}"\nexec git-upload-pack "$@"\n')
        prog.chmod(0o755)
        up = _repo(tmp_path / "up")
        _g(up, "config", "uploadpack.allowFilter", "true")
        repo = tmp_path / "r"
        _g(tmp_path, "clone", "-q", "--filter=blob:none", "--no-checkout", up.as_uri(), str(repo))
        (repo / "a.txt").write_text("edited\n")
        _g(repo, "config", "remote.origin.uploadpack", str(prog))
        _prove_planted_vector_fires(repo, marker, "show", "HEAD:a.txt")
        # The proof fetched the blob; drop it again so the handler would refetch.
        repo2 = tmp_path / "r2"
        _g(tmp_path, "clone", "-q", "--filter=blob:none", "--no-checkout", up.as_uri(), str(repo2))
        (repo2 / "a.txt").write_text("edited\n")
        _g(repo2, "config", "remote.origin.uploadpack", str(prog))

        body = await self._diff(repo2 / "a.txt")
        assert body["status"] == "error"
        assert _ran(marker) == ""


# ---------------------------------------------------------------- update paths


def _diverged_checkout(tmp_path: Path) -> Path:
    """A clone whose ``main`` is one commit ahead of and one behind ``origin``."""
    up = _repo(tmp_path / "up")
    proj = tmp_path / "proj"
    _g(tmp_path, "clone", "-q", str(up), str(proj))
    (up / "a.txt").write_text("upstream\n")
    _g(up, "commit", "-qam", "upstream")
    (proj / "b.txt").write_text("local\n")
    _g(proj, "add", "b.txt")
    _g(proj, "commit", "-qm", "local")
    return proj


def _behind_checkout(tmp_path: Path) -> Path:
    """A clone whose ``main`` is one commit behind ``origin`` and nothing ahead."""
    up = _repo(tmp_path / "up")
    proj = tmp_path / "proj"
    _g(tmp_path, "clone", "-q", str(up), str(proj))
    (up / "a.txt").write_text("upstream\n")
    _g(up, "commit", "-qam", "upstream")
    return proj


class TestDashboardUpdateApply:
    @staticmethod
    def _arm(monkeypatch, proj: Path) -> None:
        from kiro_crew.dashboard.handlers import updates

        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability.running_from_checkout",
            lambda root, **kw: True,
        )

        async def _no_provider():
            return None

        monkeypatch.setattr("kiro_crew.platform.update_provider.apply_policy_update", _no_provider)
        monkeypatch.setattr(updates, "update_blocked_reason", lambda url: None)

    @staticmethod
    def _post():
        from kiro_crew.dashboard.handlers import updates

        class _State:
            def push_refresh(self, kind: str) -> None:
                pass

        class _Req(dict):
            app = {"state": _State()}

        return asyncio.run(updates.api_update_apply(_Req(app="", user="local-app")))

    def test_status_fetch_and_count_run_no_planted_program(self, monkeypatch, tmp_path):
        # Real git end to end: `status` (fsmonitor, post-index-change), `fetch`
        # (reference-transaction) and the divergence count all run in the
        # poisoned checkout; the diverged refusal stops it before the merge.
        marker, prog = _marker_programs(tmp_path)
        proj = _diverged_checkout(tmp_path)
        _plant(proj, prog, drivers=False)
        _prove_planted_vector_fires(proj, marker, "status", "--porcelain")
        self._arm(monkeypatch, proj)

        resp = self._post()
        assert resp.status == 409
        assert json.loads(resp.body)["code"] == "checkout_diverged", resp.body
        assert _ran(marker) == ""

    def test_fetch_from_a_tracked_non_origin_remote_runs_no_upload_pack(
        self, monkeypatch, tmp_path
    ):
        # The bare `git fetch` follows the branch's tracked remote; the env pins only
        # `remote.origin.uploadpack`, so `--upload-pack` must cover the others.
        marker, _ = _marker_programs(tmp_path)
        # An upload-pack must not wait on stdin, or the proving fetch would hang.
        prog = tmp_path / "planted-upload-pack.sh"
        prog.write_text(f'#!/bin/sh\necho "$0 $*" >> "{marker}"\nexit 1\n')
        prog.chmod(0o755)
        proj = _diverged_checkout(tmp_path)
        _g(proj, "remote", "add", "upstream", str(tmp_path / "up"))
        _g(proj, "fetch", "-q", "upstream")
        _g(proj, "config", "branch.main.remote", "upstream")
        _g(proj, "config", "branch.main.merge", "refs/heads/main")
        _g(proj, "config", "remote.upstream.uploadpack", str(prog))
        _prove_planted_vector_fires(proj, marker, "fetch", "--quiet")
        self._arm(monkeypatch, proj)

        # The probe refuses the key outright...
        resp = self._post()
        assert resp.status == 409
        assert json.loads(resp.body)["code"] == "repo_exec_config", resp.body
        assert _ran(marker) == ""

        # ...and with the probe bypassed, `--upload-pack` on the fetch still wins.
        from kiro_crew.dashboard.handlers import updates

        monkeypatch.setattr(updates, "repo_exec_config_reason", lambda _p, **_k: "")
        resp = self._post()
        assert resp.status == 409
        assert json.loads(resp.body)["code"] == "checkout_diverged", resp.body
        assert _ran(marker) == ""

    @pytest.mark.parametrize("via_include", [False, True])
    def test_a_globally_ignored_file_is_not_a_dirty_tree(self, monkeypatch, tmp_path, via_include):
        # The isolated env drops a global core.excludesFile; the dirty-tree check
        # passes the operator's own one back (also when set through a global
        # include), so `.envrc` is still ignored.
        proj = _diverged_checkout(tmp_path)
        (proj / ".envrc").write_text("export X=1\n")
        ignore = tmp_path / "global-ignore"
        ignore.write_text(".envrc\n")
        user = tmp_path / "gitconfig"
        setting = f"[core]\n\texcludesFile = {ignore}\n"
        if via_include:
            included = tmp_path / "gitconfig-included"
            included.write_text(setting)
            user.write_text(f"[include]\n\tpath = {included}\n")
        else:
            user.write_text(setting)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(user))
        self._arm(monkeypatch, proj)

        resp = self._post()
        assert resp.status == 409
        assert json.loads(resp.body)["code"] == "checkout_diverged", resp.body

    def test_repo_named_driver_is_refused_before_any_git_runs(self, monkeypatch, tmp_path):
        from kiro_crew.dashboard.handlers import updates

        marker, prog = _marker_programs(tmp_path)
        proj = _diverged_checkout(tmp_path)
        _plant(proj, prog, drivers=True)
        self._arm(monkeypatch, proj)

        async def _boom(*a, **k):  # pragma: no cover - must not be called
            raise AssertionError(f"git must not run: {a}")

        monkeypatch.setattr(updates.asyncio, "create_subprocess_exec", _boom)
        resp = self._post()
        assert resp.status == 409
        body = json.loads(resp.body)
        assert body["code"] == "repo_exec_config"
        assert "filter.pwn" in body["error"]
        assert _ran(marker) == ""

    def test_driver_planted_during_the_fetch_is_refused_before_the_merge(
        self, monkeypatch, tmp_path
    ):
        # The first probe runs before `fetch`, which can take its whole timeout.
        # A driver that appears after it must still be refused before the merge.
        from kiro_crew.dashboard.handlers import updates

        proj = _behind_checkout(tmp_path)
        before = _g(proj, "rev-parse", "HEAD")
        self._arm(monkeypatch, proj)
        answers = ["", "repository declares filter.late.smudge"]
        monkeypatch.setattr(updates, "repo_exec_config_reason", lambda _p, **_k: answers.pop(0))

        async def _no_snapshot(_state):  # pragma: no cover - must not be reached
            raise AssertionError("the refusal must come before the memory snapshot")

        monkeypatch.setattr(updates, "_snapshot_memory_or_refuse", _no_snapshot)
        resp = self._post()
        assert resp.status == 409
        assert json.loads(resp.body)["code"] == "repo_exec_config"
        assert answers == [], "probed twice: before the fetch and before the merge"
        assert _g(proj, "rev-parse", "HEAD") == before


class TestCliUpdate:
    class _Stop(Exception):
        pass

    def _arm(self, monkeypatch, proj: Path) -> None:
        from kiro_crew import cli_server

        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
        monkeypatch.setattr(
            cli_server, "derive_capability", lambda **_kw: SimpleNamespace(managed_by="git")
        )
        monkeypatch.setattr(
            "kiro_crew.platform.update_provider.resolve_provider", lambda *a, **k: None
        )

        async def _no_provider():
            return None

        monkeypatch.setattr("kiro_crew.platform.update_provider.apply_policy_update", _no_provider)
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.update_blocked_reason", lambda url: None
        )
        monkeypatch.setattr(cli_server, "_snapshot_memory_or_exit", lambda: None)

        def _stop() -> bool:
            raise self._Stop

        # The first step after `git reset --hard`: stop there, the git work is done.
        monkeypatch.setattr("kiro_crew.agent_sdk.kas_bundle.kas_bundle_route", _stop)

    def test_fetch_status_and_reset_run_no_planted_program(self, monkeypatch, tmp_path):
        from kiro_crew import cli_server

        marker, prog = _marker_programs(tmp_path)
        proj = _behind_checkout(tmp_path)
        _plant(proj, prog, drivers=False)
        _prove_planted_vector_fires(proj, marker, "status", "--porcelain")
        self._arm(monkeypatch, proj)

        with pytest.raises(self._Stop):
            cli_server._update()
        assert _g(proj, "rev-parse", "HEAD") == _g(proj, "rev-parse", "origin/main")
        assert _ran(marker) == ""

    def test_repo_named_driver_is_refused_before_fetch(self, monkeypatch, tmp_path, capsys):
        from kiro_crew import cli_server

        marker, prog = _marker_programs(tmp_path)
        proj = _behind_checkout(tmp_path)
        before = _g(proj, "rev-parse", "HEAD")
        _plant(proj, prog, drivers=True)
        self._arm(monkeypatch, proj)

        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        assert "filter.pwn" in capsys.readouterr().out
        assert _g(proj, "rev-parse", "HEAD") == before
        assert _ran(marker) == ""

    def test_planted_origin_upload_pack_does_not_run(self, monkeypatch, tmp_path):
        # Git takes the FIRST uploadpack it reads -- the repository's -- so the env
        # pin alone does not win. The probe refuses it before the fetch.
        from kiro_crew import cli_server

        marker, _ = _marker_programs(tmp_path)
        prog = tmp_path / "planted-upload-pack.sh"
        prog.write_text(f'#!/bin/sh\necho "$0 $*" >> "{marker}"\nexit 1\n')
        prog.chmod(0o755)
        proj = _behind_checkout(tmp_path)
        _g(proj, "config", "remote.origin.uploadpack", str(prog))
        _prove_planted_vector_fires(proj, marker, "fetch", "origin")
        self._arm(monkeypatch, proj)

        with pytest.raises(SystemExit):
            cli_server._update()
        assert _ran(marker) == ""

    def test_fetch_overrides_upload_pack_even_without_the_probe(self, monkeypatch, tmp_path):
        # Belt and braces: with the probe bypassed, `--upload-pack` still wins.
        from kiro_crew import cli_server

        marker, _ = _marker_programs(tmp_path)
        prog = tmp_path / "planted-upload-pack.sh"
        prog.write_text(f'#!/bin/sh\necho "$0 $*" >> "{marker}"\nexit 1\n')
        prog.chmod(0o755)
        proj = _behind_checkout(tmp_path)
        _g(proj, "config", "remote.origin.uploadpack", str(prog))
        self._arm(monkeypatch, proj)
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.repo_exec_config_reason",
            lambda _p, **_k: "",
        )

        with pytest.raises(self._Stop):
            cli_server._update()
        assert _ran(marker) == ""

    def test_an_inherited_git_dir_cannot_point_the_guards_at_another_repo(
        self, monkeypatch, tmp_path
    ):
        # The reset strips GIT_DIR; the divergence guard must judge the same repo,
        # or a behind-only decoy approves resetting a diverged checkout.
        from kiro_crew import cli_server

        proj = _diverged_checkout(tmp_path)
        decoy = tmp_path / "decoy"
        _g(tmp_path, "clone", "-q", str(tmp_path / "up"), str(decoy))
        _g(decoy, "reset", "-q", "--hard", "HEAD~1")
        before = _g(proj, "rev-parse", "HEAD")
        self._arm(monkeypatch, proj)
        monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))

        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1, "diverged without --force is refused"
        monkeypatch.delenv("GIT_DIR")
        assert _g(proj, "rev-parse", "HEAD") == before

    def test_driver_planted_after_the_fetch_is_refused_before_the_reset(
        self, monkeypatch, tmp_path, capsys
    ):
        # The fetch and the local-changes prompt leave an unbounded gap before
        # the reset; the probe is repeated right before it.
        from kiro_crew import cli_server

        proj = _behind_checkout(tmp_path)
        before = _g(proj, "rev-parse", "HEAD")
        self._arm(monkeypatch, proj)
        answers = ["", "repository declares filter.late.smudge"]
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.repo_exec_config_reason",
            lambda _p, **_k: answers.pop(0),
        )

        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        assert "filter.late.smudge" in capsys.readouterr().out
        assert answers == []
        assert _g(proj, "rev-parse", "HEAD") == before

    def test_up_to_date_diff_runs_no_textconv_planted_during_the_fetch(self, monkeypatch, tmp_path):
        # The `HEAD..target` diff runs before the post-fetch re-check, so a
        # textconv planted while the fetch ran must not reach it.
        from kiro_crew import cli_server

        marker, prog = _marker_programs(tmp_path)
        proj = _behind_checkout(tmp_path)
        (proj / ".git" / "info").mkdir(exist_ok=True)
        (proj / ".git" / "info" / "attributes").write_text("* diff=pwn\n")
        _g(proj, "config", "diff.pwn.textconv", str(prog))
        _g(proj, "fetch", "-q", "origin")
        _prove_planted_vector_fires(proj, marker, "diff", "HEAD", "origin/main", "--quiet")
        self._arm(monkeypatch, proj)
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.repo_exec_config_reason",
            lambda _p, **_k: "",
        )

        with pytest.raises(self._Stop):
            cli_server._update()
        assert _ran(marker) == ""


class TestNestedProbeFailures:
    """A skipping nested walk passes over only an unreadable nested CONFIG."""

    def test_a_nested_repo_whose_later_probe_fails_is_refused(self, monkeypatch, tmp_path):
        from kiro_crew.platform import update_governance as ug

        _, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        nested = _with_nested_filter_repo(repo, prog)
        real = ug._git_probe

        def _probe(proj, *args, **kw):
            # The nested repo's config reads fine; its own index listing fails.
            if Path(proj) == nested and args[:1] == ("ls-files",):
                return None
            return real(proj, *args, **kw)

        monkeypatch.setattr(ug, "_git_probe", _probe)
        assert ug.nested_repo_exec_driver_reason(str(repo)) == ug._NESTED_REPO_INCOMPLETE

    def test_a_nested_repo_with_its_config_deleted_is_refused(self, monkeypatch, tmp_path):
        # The child git still runs there, and runs a GLOBAL driver the nested
        # attributes bind, so an unlistable nested config is not skipped.
        from kiro_crew.platform import update_governance as ug

        marker, prog = _marker_programs(tmp_path)
        repo = _repo(tmp_path / "r")
        nested = _with_nested_filter_repo(repo, prog)
        _g(nested, "config", "--unset", "filter.pwn.clean")
        user = tmp_path / "gitconfig"
        user.write_text(f'[filter "pwn"]\n\tclean = {prog}\n')
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(user))
        (nested / ".git" / "config").unlink()
        _prove_planted_vector_fires(repo, marker, "status", "--porcelain")

        assert ug.nested_repo_exec_driver_reason(str(repo)) == ug._NESTED_REPO_INCOMPLETE

    def test_a_timed_out_top_level_listing_is_refused(self, monkeypatch, tmp_path):
        from kiro_crew.platform import update_governance as ug

        repo = _repo(tmp_path / "r")
        real_run = ug.subprocess.run

        def _run(argv, *a, **kw):
            if "ls-files" in argv:
                raise subprocess.TimeoutExpired(argv, 10)
            return real_run(argv, *a, **kw)

        monkeypatch.setattr(ug.subprocess, "run", _run)
        assert ug.nested_repo_exec_driver_reason(str(repo)) == ug._NESTED_REPO_INCOMPLETE

    def test_an_unreadable_top_level_index_is_still_passed_over(self, monkeypatch, tmp_path):
        from kiro_crew.platform import update_governance as ug

        repo = _repo(tmp_path / "r")
        (repo / ".git" / "index").write_bytes(b"garbage")
        assert ug.nested_repo_exec_driver_reason(str(repo)) == ""


@pytest.mark.parametrize(
    "key",
    [
        "url.https://bot:s3cr3tTOKEN@example.invalid/.insteadOf",
        "http.https://example.invalid/x?access_token=s3cr3tTOKEN.sslVerify",
        "credential.https://bot:s3cr3tTOKEN@example.invalid.helper",
    ],
)
def test_refusal_reasons_do_not_carry_url_credentials(tmp_path, key):
    # The reason reaches the gateway log and the 409 body.
    from kiro_crew.platform.update_governance import repo_exec_config_reason

    repo = _repo(tmp_path / "r")
    _g(repo, "config", key, "false" if key.endswith("sslVerify") else "https://x.invalid/")
    reason = repo_exec_config_reason(str(repo))
    assert reason, "the key is refused"
    assert "s3cr3tTOKEN" not in reason
    assert "<redacted>" in reason


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("credential.https://example.invalid.helper", "!true"),
        ("remote.origin.vcs", "pwn"),
        ("core.gitProxy", "pwn"),
    ],
)
def test_transport_keys_refuse_read_only_callers_too(tmp_path, key, value):
    # A partial clone's `archive`/`diff`/`show` lazy-fetches a missing blob over
    # the transport, so read-only callers refuse these keys like the fetch does.
    from kiro_crew.platform.update_governance import (
        repo_exec_config_reason,
        repo_exec_driver_reason,
    )

    repo = _repo(tmp_path / "r")
    _g(repo, "config", key, value)
    assert repo_exec_driver_reason(str(repo)) != ""
    assert repo_exec_config_reason(str(repo)) != ""


def test_human_transport_probe_sees_includes_that_depend_on_global_remotes(monkeypatch, tmp_path):
    # A repo-local `includeIf "hasconfig:remote.*.url:..."` matches only on a URL
    # from GLOBAL config; the fetch reads global config, so the probe must too.
    from kiro_crew.platform.update_governance import repo_exec_config_reason

    repo = _repo(tmp_path / "r")
    inc = tmp_path / "inc.gitconfig"
    inc.write_text("[core]\n\tsshCommand = evil-ssh\n")
    user = tmp_path / "gitconfig"
    user.write_text('[remote "g"]\n\turl = https://example.invalid/x\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(user))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    _g(repo, "config", "includeIf.hasconfig:remote.*.url:https://example.invalid/**.path", str(inc))
    assert "sshcommand" in repo_exec_config_reason(str(repo), human_transport=True).lower()


def test_file_diff_builds_its_git_env_off_the_event_loop(monkeypatch, tmp_path):
    # `git_command_env` can shell out; the handler builds it in its worker thread.
    import threading

    from kiro_crew.dashboard.handlers import files

    loop_thread = threading.get_ident()
    seen: list[int] = []

    original = files.git_command_env

    def _spy():
        seen.append(threading.get_ident())
        return original()

    monkeypatch.setattr(files, "git_command_env", _spy)
    repo = _repo(tmp_path / "r")
    (repo / "a.txt").write_text("two\n")
    asyncio.run(TestDashboardFileDiff._diff(repo / "a.txt"))
    assert seen and all(t != loop_thread for t in seen), (seen, loop_thread)
