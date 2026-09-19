"""``GitProjectStore`` without a sandbox: the git seam is faked at ``_run_git``.

The real-git suites need an enforcing namespace sandbox, so on a GH runner (and on
any host that denies unprivileged user namespaces) every line they reach is
unmeasured -- which is what put this module below the per-file coverage floor. The
behaviour under test here is the store's own decision-making, not git's: which
coordinates it validates, when it reuses a checkout instead of re-cloning, what it
does when a publish half-fails, and which failures are reported versus raised.

So the fake sits exactly where ``test_project_bundle_git.py``'s ``bundle_store``
fixture already puts it -- ``store._run_git`` -- and nothing below it is stubbed.
In particular the provenance record is written and read through the real
``_write_source_record`` / ``_checkout_matches`` pair, so the hardened reader the
read-guard invariants pin (``test_project_read_guards.py``) is the one exercised.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from project_git_helpers import (  # noqa: F401
    local_git_remote,
    requires_local_git_remote,
)

from kiro_crew import platform_compat, project_git
from kiro_crew.project_git import (
    GitProjectStore,
    ProjectGitError,
    ProjectSandboxUnavailableError,
)
from kiro_crew.project_manifest import (
    ProjectManifest,
    ProjectManifestError,
    ProjectSource,
    _synthesize_source_id,
)
from kiro_crew.project_registry import ProjectRegistry
from kiro_crew.project_review import compute_review_digest
from kiro_crew.sandbox import SandboxUnavailableError


def _manifest_text(name: str = "Payments", sources: list[dict] | None = None) -> str:
    return yaml.safe_dump(
        {
            "apiVersion": "crew.kiro/v1",
            "kind": "Project",
            "name": name,
            "sources": sources or [],
        },
        sort_keys=False,
    )


def _registry(tmp_path: Path) -> ProjectRegistry:
    return ProjectRegistry(
        projects_dir=tmp_path / "projects",
        registry_dir=tmp_path / "projects-registry",
    )


def _accept_manifest(registry: ProjectRegistry, project_id: str, bundle: Path):
    """Record the owner's acceptance of the manifest exactly as it stands.

    Accepting ``project.yaml`` is what authorizes cloning the sources it
    declares, so every test about materialization starts here.
    """
    digest, hashes = compute_review_digest(bundle, None)
    return registry.record_review(project_id, digest, hashes)


def _ok(*args: str, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), 0, stdout=stdout, stderr="")


class FakeGit:
    """Stand in for one ``_run_git`` call, recording argv and faking a clone.

    A clone materializes the destination the store just handed it (a manifest plus
    a ``.git`` marker), because every reuse decision downstream is a question about
    that tree. Nothing else about git is modelled.
    """

    def __init__(
        self,
        *,
        manifest: str | None = None,
        branch: str = "main",
        clone_error: Exception | None = None,
        size: str | None = None,
        merge_error: Exception | None = None,
        merge_error_paths: tuple[Path, ...] = (),
        status: str = "",
        status_error: Exception | None = None,
        is_ancestor: bool = True,
        shares_history: bool = True,
    ) -> None:
        self.manifest = _manifest_text() if manifest is None else manifest
        self.branch = branch
        self.clone_error = clone_error
        self.size = size
        self.merge_error = merge_error
        self.merge_error_paths = merge_error_paths
        self.status = status
        self.status_error = status_error
        self.is_ancestor = is_ancestor
        self.shares_history = shares_history
        self.calls: list[tuple[Path, tuple[str, ...]]] = []

    def __call__(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        self.calls.append((Path(cwd), args))
        verb = args[0] if args else ""
        if verb == "clone":
            if self.clone_error is not None:
                raise self.clone_error
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / ".git").mkdir(exist_ok=True)
            (target / "project.yaml").write_text(self.manifest, encoding="utf-8")
            return _ok(*args)
        if verb == "symbolic-ref":
            return _ok(*args, stdout=f"{self.branch}\n")
        if verb == "cat-file":
            raw = self.size if self.size is not None else str(len(self.manifest.encode("utf-8")))
            return _ok(*args, stdout=f"{raw}\n")
        if verb == "show":
            return _ok(*args, stdout=self.manifest)
        if verb == "merge":
            if self.merge_error is not None and (
                not self.merge_error_paths or Path(cwd) in self.merge_error_paths
            ):
                raise self.merge_error
            return _ok(*args)
        if verb == "status":
            if self.status_error is not None:
                raise self.status_error
            return _ok(*args, stdout=self.status)
        if verb == "merge-base":
            reachable = self.is_ancestor if "--is-ancestor" in args else self.shares_history
            if not reachable:
                raise ProjectGitError("Git project operation failed")
            return _ok(*args, stdout="" if "--is-ancestor" in args else "d00dfeed\n")
        return _ok(*args)

    def verbs(self) -> list[str]:
        return [args[0] for _cwd, args in self.calls if args]


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GitProjectStore:
    """A store whose git seam is faked and whose checkout audit is a no-op.

    ``_assert_safe_checkout`` shells out to a real git to read the checkout's
    config; the fake trees here have no config to read, and its own refusal is
    covered explicitly below.
    """
    built = GitProjectStore(_registry(tmp_path))
    monkeypatch.setattr(built, "_assert_safe_checkout", staticmethod(lambda _path: None))
    monkeypatch.setattr(built, "_run_git", FakeGit())
    return built


def _fake(store: GitProjectStore) -> FakeGit:
    git = store._run_git
    assert isinstance(git, FakeGit)
    return git


class TestRemoteValidation:
    """Every refusal in ``_validate_remote``, and the two shapes it normalizes."""

    @pytest.mark.parametrize(
        ("remote", "message"),
        [
            ("", "must not be empty"),
            ("   ", "must not be empty"),
            ("https://example.com/a\nb", "invalid characters"),
            ("https://example.com/a\rb", "invalid characters"),
            ("https://example.com/a\x00b", "invalid characters"),
            # A scheme survives only when the remote is not also a valid scp-style
            # spelling; `ftp://host/x` IS one, so the refusal needs a shape that is
            # not (the drive-letter case is pinned in test_project_bundle_gate.py).
            ("ftp::bundle", "unsupported Git remote protocol"),
            ("https://user@example.com/b", "must use a credential helper"),
            ("https://example.com/b?token=x", "must use a credential helper"),
            ("https://example.com/b#frag", "must use a credential helper"),
            ("ssh://user:secret@example.com/b", "must not include a password"),
            ("file:///srv/bundle?x=1", "must not include a query or fragment"),
            ("file:///srv/bundle#frag", "must not include a query or fragment"),
            ("file://remote-host/srv/bundle", "file Git remotes must be local"),
            ("file:relative/bundle", "must use an absolute path"),
            ("file:///srv/bun%0adle", "invalid characters"),
            ("http://[::1/bundle", "invalid Git remote URL"),
        ],
    )
    def test_a_refused_remote_names_its_reason(self, remote: str, message: str) -> None:
        with pytest.raises(ProjectGitError, match=message):
            GitProjectStore._validate_remote(remote)

    @requires_local_git_remote
    def test_localhost_is_the_one_accepted_file_authority(self) -> None:
        remote = "file://localhost/srv/bundle"

        assert GitProjectStore._validate_remote(remote) == remote

    @requires_local_git_remote
    def test_a_sensitive_local_path_is_refused(self) -> None:
        with pytest.raises(ProjectGitError, match="sensitive path"):
            GitProjectStore._validate_remote(f"file://{Path.home() / '.ssh'}")

    @requires_local_git_remote
    def test_a_relative_local_path_resolves_against_the_bundle(self, tmp_path: Path) -> None:
        resolved = GitProjectStore._validate_remote("sibling", base_dir=tmp_path)

        assert resolved == str((tmp_path / "sibling").resolve())

    @requires_local_git_remote
    def test_an_absolute_local_path_comes_back_normalized(self, tmp_path: Path) -> None:
        resolved = GitProjectStore._validate_remote(f"{tmp_path}/./repo")

        assert resolved == str((tmp_path / "repo").resolve())

    @pytest.mark.parametrize("branch", ["-bad", "a..b", "a//b", "we\\ird", "tip~1", "x?", "end."])
    def test_an_invalid_branch_is_refused(self, branch: str) -> None:
        with pytest.raises(ProjectGitError, match="default branch is invalid"):
            GitProjectStore._validate_branch(branch)

    def test_a_branch_is_stripped_and_an_empty_one_is_allowed(self) -> None:
        assert GitProjectStore._validate_branch("  main  ") == "main"
        assert GitProjectStore._validate_branch("   ") == ""


class TestTheGitExecutableMustBeTrusted:
    def test_an_unresolvable_git_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform_compat, "trusted_git_bin", lambda: None)

        with pytest.raises(ProjectGitError, match="trusted Git executable is unavailable"):
            GitProjectStore._git_executable()


class TestCredentialHelperSanitizing:
    """Only two helper shapes survive, and each must resolve off ``PATH``."""

    def test_a_keychain_helper_resolves_to_a_trusted_absolute_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            platform_compat, "trusted_system_bin", lambda name: f"/usr/libexec/{name}"
        )

        assert (
            GitProjectStore._sanitize_credential_helper(" osxkeychain ")
            == "!/usr/libexec/git-credential-osxkeychain"
        )

    def test_an_unresolvable_keychain_helper_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda _name: None)

        assert GitProjectStore._sanitize_credential_helper("wincred") is None

    def test_the_gh_helper_is_rewritten_to_its_trusted_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda _name: "/usr/bin/gh")

        assert (
            GitProjectStore._sanitize_credential_helper("!gh auth git-credential")
            == "!/usr/bin/gh auth git-credential"
        )

    def test_an_unresolvable_gh_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda _name: None)

        assert GitProjectStore._sanitize_credential_helper("!gh auth git-credential") is None

    @pytest.mark.parametrize(
        "value",
        [
            "store",
            "!gh auth",
            "!gh auth git-credential --extra",
            "!curl auth git-credential",
            "!gh login git-credential",
            "!gh 'auth git-credential",
        ],
    )
    def test_every_other_shape_is_dropped(self, value: str) -> None:
        assert GitProjectStore._sanitize_credential_helper(value) is None


class TestCredentialHelperEnv:
    """The env handed to git: pinned transport helpers plus at most eight helpers."""

    @pytest.fixture(autouse=True)
    def _sandbox_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(project_git.sandbox, "enforcing_backend_available", lambda: True)
        monkeypatch.setattr(GitProjectStore, "_git_executable", staticmethod(lambda: "/bin/git"))

    def _wire(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        stdout: str,
        returncode: int = 0,
        base_env: dict[str, str] | None = None,
    ) -> list[Path]:
        cleanups: list[Path] = []

        def spawn(argv, *, mode, env):
            marker = tmp_path / f"cleanup-{len(cleanups)}"
            marker.write_text("x", encoding="utf-8")
            cleanups.append(marker)
            return argv, dict(env), str(marker)

        monkeypatch.setattr(project_git, "sandboxed_spawn_argv", spawn)
        monkeypatch.setattr(
            project_git,
            "run_limited",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, returncode, stdout, ""),
        )
        monkeypatch.setattr(
            project_git, "git_command_env", lambda: dict(base_env or {"GIT_CONFIG_COUNT": "0"})
        )
        return cleanups

    def test_an_unenforced_sandbox_refuses_before_any_spawn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(project_git.sandbox, "enforcing_backend_available", lambda: False)

        with pytest.raises(ProjectSandboxUnavailableError):
            GitProjectStore._credential_helper_env()

    def test_a_pinned_transport_helper_is_replaced_with_its_trusted_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: f"/usr/bin/{name}")
        self._wire(
            monkeypatch,
            tmp_path,
            stdout="",
            base_env={
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "remote.origin.uploadpack",
                "GIT_CONFIG_VALUE_0": "/dev/null",
                "GIT_CONFIG_KEY_1": "core.fsmonitor",
                "GIT_CONFIG_VALUE_1": "/dev/null",
            },
        )

        env = GitProjectStore._credential_helper_env()

        assert env["GIT_CONFIG_VALUE_0"] == "/usr/bin/git-upload-pack"
        assert env["GIT_CONFIG_VALUE_1"] == "/dev/null"

    def test_an_unresolvable_transport_helper_stays_pinned_to_the_null_device(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda _name: None)
        self._wire(
            monkeypatch,
            tmp_path,
            stdout="",
            base_env={
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "remote.origin.receivepack",
                "GIT_CONFIG_VALUE_0": "/dev/null",
            },
        )

        assert GitProjectStore._credential_helper_env()["GIT_CONFIG_VALUE_0"] == "/dev/null"

    def test_discovered_helpers_are_appended_and_the_spawn_temp_is_removed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda _name: "/usr/bin/gh")
        cleanups = self._wire(
            monkeypatch,
            tmp_path,
            # A valueless line has no separator and is dropped; an unknown helper
            # shape is dropped by the sanitizer.
            stdout=(
                "credential.helper !gh auth git-credential\n"
                "credential.https://x.helper store\n"
                "credential.helper-with-no-value\n"
            ),
        )

        env = GitProjectStore._credential_helper_env()

        assert env["GIT_CONFIG_COUNT"] == "2"
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_0"] == "!/usr/bin/gh auth git-credential"
        assert cleanups and not any(path.exists() for path in cleanups)

    def test_a_failed_config_probe_contributes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._wire(monkeypatch, tmp_path, stdout="credential.helper store\n", returncode=1)

        assert GitProjectStore._credential_helper_env()["GIT_CONFIG_COUNT"] == "0"

    def test_the_helper_list_is_capped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda _name: "/usr/bin/gh")
        self._wire(
            monkeypatch,
            tmp_path,
            stdout="".join(
                f"credential.h{index}.helper !gh auth git-credential\n" for index in range(12)
            ),
        )

        env = GitProjectStore._credential_helper_env()

        assert env["GIT_CONFIG_COUNT"] == str(project_git._MAX_CREDENTIAL_HELPERS)


class TestRunGit:
    """The one place a git subprocess is launched, and its failure translation."""

    @pytest.fixture(autouse=True)
    def _wire(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(project_git.sandbox, "enforcing_backend_available", lambda: True)
        monkeypatch.setattr(GitProjectStore, "_git_executable", staticmethod(lambda: "/bin/git"))
        monkeypatch.setattr(
            GitProjectStore, "_credential_helper_env", classmethod(lambda cls: {"E": "1"})
        )
        self.cleanup = tmp_path / "spawn-temp"
        self.cleanup.write_text("x", encoding="utf-8")
        monkeypatch.setattr(
            project_git,
            "sandboxed_spawn_argv",
            lambda argv, *, mode, env: (argv, dict(env), str(self.cleanup)),
        )

    def test_a_successful_run_returns_the_completed_process(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen: dict[str, object] = {}

        def run(argv, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, "out", "")

        monkeypatch.setattr(project_git, "run_limited", run)

        result = GitProjectStore._run_git(tmp_path, "status")

        assert result.stdout == "out"
        assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert seen["env"]["GIT_PROXY_COMMAND"] == "true"
        assert not self.cleanup.exists()

    @pytest.mark.parametrize(
        ("raised", "expected", "message"),
        [
            (
                SandboxUnavailableError(
                    "no backend", "no_backend", "unshare(CLONE_NEWUSER) failed with errno 1"
                ),
                ProjectSandboxUnavailableError,
                "no sandbox backend",
            ),
            (
                subprocess.TimeoutExpired("git", 1),
                ProjectGitError,
                "timed out",
            ),
            (
                subprocess.CalledProcessError(128, "git", stderr="https://user:pw@host"),
                ProjectGitError,
                "Git project operation failed",
            ),
        ],
    )
    def test_a_failure_is_translated_without_replaying_git_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        raised: Exception,
        expected: type[Exception],
        message: str,
    ) -> None:
        def run(argv, **kwargs):
            raise raised

        monkeypatch.setattr(project_git, "run_limited", run)

        with pytest.raises(expected, match=message) as exc_info:
            GitProjectStore._run_git(tmp_path, "fetch")

        assert "user:pw" not in str(exc_info.value)
        assert not self.cleanup.exists()


class TestDerivedPathGuards:
    def test_a_path_outside_managed_storage_is_refused(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        with pytest.raises(ProjectGitError, match="escapes managed storage"):
            store._assert_derived_path_unlinked(tmp_path / "elsewhere")

    def test_a_linked_component_is_refused(self, store: GitProjectStore, tmp_path: Path) -> None:
        root = store.registry.projects_dir
        root.mkdir(parents=True, exist_ok=True)
        (tmp_path / "outside").mkdir()
        platform_compat.symlink_or_junction(tmp_path / "outside", root / "state")

        with pytest.raises(ProjectGitError, match="link or junction"):
            store._assert_derived_path_unlinked(root / "state" / "x")

    def test_a_linked_root_is_refused(self, store: GitProjectStore, tmp_path: Path) -> None:
        (tmp_path / "real-root").mkdir()
        platform_compat.symlink_or_junction(tmp_path / "real-root", store.registry.projects_dir)

        with pytest.raises(ProjectGitError, match="link or junction"):
            store._assert_derived_path_unlinked(store.registry.projects_dir / "managed")

    def test_an_unopenable_lock_path_is_refused(self, store: GitProjectStore) -> None:
        locks = store.registry.projects_dir / "state" / "git-locks"
        locks.mkdir(parents=True)
        # A directory where the lock file belongs: os.open cannot take it for
        # writing, and a lock that cannot be held must refuse rather than proceed.
        (locks / "taken.lock").mkdir()

        with pytest.raises(ProjectGitError, match="lock path is not safe"):
            with store._lock("taken"):
                pass

    def test_an_unsafe_checkout_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(project_git, "repo_exec_config_reason", lambda _path: "filter.evil")

        with pytest.raises(ProjectGitError, match="unsafe to synchronize: filter.evil"):
            GitProjectStore._assert_safe_checkout(Path("/tmp/x"))


@requires_local_git_remote
class TestAdd:
    def test_a_clone_is_published_under_the_registration_id(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        project = store.add(f"{tmp_path}/remote")

        registration = project.registrations[-1]
        assert registration.origin == "managed_git"
        assert registration.default_branch == "main"
        assert (
            registration.path
            == (store.registry.projects_dir / "managed" / project.id / "bundle").resolve()
        )
        assert (registration.path / "project.yaml").exists()
        assert not list((store.registry.projects_dir / "managed").glob("project-clone-*"))

    def test_a_declared_source_is_not_cloned_before_it_is_reviewed(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        # The bundle remote is the URL the owner typed; a source url is a
        # DEFINITION the manifest carries, so adding the Project must not fetch
        # it. That is what keeps a manifest from aiming an outbound request at a
        # host of its choosing before anyone has seen the manifest.
        source = tmp_path / "payments-api"
        _fake(store).manifest = _manifest_text(
            sources=[{"type": "repo", "url": str(source), "role": "primary"}]
        )

        project = store.add(f"{tmp_path}/remote")

        assert _fake(store).verbs().count("clone") == 1
        assert not any(str(source) in " ".join(args) for _cwd, args in _fake(store).calls)
        assert not (store.registry.projects_dir / "state" / project.id / "sources").exists()

    def test_an_accepted_manifest_is_what_materializes_its_sources(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        source = tmp_path / "payments-api"
        _fake(store).manifest = _manifest_text(
            sources=[{"type": "repo", "url": str(source), "role": "primary"}]
        )
        project = store.add(f"{tmp_path}/remote")
        bundle = project.registrations[-1].path

        accepted = _accept_manifest(store.registry, project.id, bundle)
        failures = store.materialize_registered_sources(accepted)

        assert failures == {}
        sources_root = store.registry.projects_dir / "state" / project.id / "sources"
        assert [path.name for path in sources_root.iterdir() if path.is_dir()]
        assert _fake(store).verbs().count("clone") == 2

    def test_a_detached_head_clone_is_refused(self, store: GitProjectStore, tmp_path: Path) -> None:
        _fake(store).branch = ""

        with pytest.raises(ProjectGitError, match="detached HEAD"):
            store.add(f"{tmp_path}/remote")

        assert not list((store.registry.projects_dir / "managed").iterdir())

    def test_a_clone_without_a_valid_manifest_is_refused(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        _fake(store).manifest = "kind: NotAProject\n"

        with pytest.raises(ProjectGitError, match="not a valid Project bundle"):
            store.add(f"{tmp_path}/remote")

        assert not list((store.registry.projects_dir / "managed").iterdir())

    def test_a_failed_clone_leaves_no_staging_directory(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        _fake(store).clone_error = ProjectGitError("Git project operation failed")

        with pytest.raises(ProjectGitError, match="operation failed"):
            store.add(f"{tmp_path}/remote")

        assert not list((store.registry.projects_dir / "managed").iterdir())

    def test_a_publish_collision_is_refused(
        self, store: GitProjectStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        managed = store.registry.projects_dir / "managed"
        real_add_managed = store.registry.add_managed

        def add_managed(bundle_dir, *, remote, default_branch="", publish=None):
            # Something already occupies the id's publish target: the clone must
            # not be moved over it.
            def colliding(project_id: str):
                (managed / project_id / "bundle").mkdir(parents=True)
                return publish(project_id)

            return real_add_managed(
                bundle_dir, remote=remote, default_branch=default_branch, publish=colliding
            )

        monkeypatch.setattr(store.registry, "add_managed", add_managed)

        with pytest.raises(ProjectGitError, match="managed Project path collision"):
            store.add(f"{tmp_path}/remote")


class TestMaterializeSource:
    def test_an_empty_remote_is_refused(self, store: GitProjectStore) -> None:
        with pytest.raises(ProjectGitError, match="needs a URL"):
            store.materialize_source("p1", "api", "   ")

    @requires_local_git_remote
    def test_a_fresh_clone_records_its_provenance(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        target = store.materialize_source("p1", "api", str(tmp_path / "api"), "main")

        record = json.loads((target.parent / "api.source.json").read_text(encoding="utf-8"))
        assert record == {"remote": str(tmp_path / "api"), "default_branch": "main"}
        assert ("--branch", "main", "--single-branch") == _fake(store).calls[0][1][1:4]

    @requires_local_git_remote
    def test_a_branchless_declaration_clones_without_branch_arguments(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        store.materialize_source("p1", "api", str(tmp_path / "api"))

        assert _fake(store).calls[0][1][:2] == ("clone", "--")

    @requires_local_git_remote
    def test_a_matching_checkout_is_reused_untouched(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        remote = str(tmp_path / "api")
        first = store.materialize_source("p1", "api", remote, "main")
        (first / "local-work.txt").write_text("keep", encoding="utf-8")

        again = store.materialize_source("p1", "api", remote, "main")

        assert again == first
        assert (again / "local-work.txt").read_text(encoding="utf-8") == "keep"
        assert _fake(store).verbs() == ["clone"]

    @requires_local_git_remote
    def test_a_declaration_that_moved_replaces_the_checkout(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        first = store.materialize_source("p1", "api", str(tmp_path / "api"), "main")
        (first / "stale.txt").write_text("old", encoding="utf-8")

        again = store.materialize_source("p1", "api", str(tmp_path / "moved"), "main")

        assert again == first
        assert not (again / "stale.txt").exists()
        record = json.loads((again.parent / "api.source.json").read_text(encoding="utf-8"))
        assert record["remote"] == str(tmp_path / "moved")
        assert not list(again.parent.glob(".api-replaced-*"))

    @requires_local_git_remote
    def test_an_existing_path_that_is_not_a_checkout_is_refused(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        sources_root = store.registry.projects_dir / "state" / "p1" / "sources"
        (sources_root / "api").mkdir(parents=True)

        with pytest.raises(ProjectGitError, match="not a Git checkout"):
            store.materialize_source("p1", "api", str(tmp_path / "api"))

    @requires_local_git_remote
    def test_an_unrecorded_fresh_checkout_is_not_left_behind(
        self, store: GitProjectStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_args, **_kwargs):
            raise OSError("record write failed")

        monkeypatch.setattr(store, "_write_source_record", boom)

        with pytest.raises(OSError, match="record write failed"):
            store.materialize_source("p1", "api", str(tmp_path / "api"))

        sources_root = store.registry.projects_dir / "state" / "p1" / "sources"
        assert not (sources_root / "api").exists()

    @requires_local_git_remote
    def test_a_failed_replacement_puts_the_previous_checkout_back(
        self, store: GitProjectStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = store.materialize_source("p1", "api", str(tmp_path / "api"), "main")
        (first / "working.txt").write_text("still here", encoding="utf-8")

        def boom(*_args, **_kwargs):
            raise OSError("record write failed")

        monkeypatch.setattr(store, "_write_source_record", boom)

        with pytest.raises(OSError, match="record write failed"):
            store.materialize_source("p1", "api", str(tmp_path / "moved"), "main")

        assert (first / "working.txt").read_text(encoding="utf-8") == "still here"
        record = json.loads((first.parent / "api.source.json").read_text(encoding="utf-8"))
        assert record["remote"] == str(tmp_path / "api")

    @requires_local_git_remote
    def test_an_unremovable_replaced_checkout_is_reported(
        self, store: GitProjectStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.materialize_source("p1", "api", str(tmp_path / "api"), "main")
        monkeypatch.setattr(platform_compat, "rmtree_force", lambda _path: False)

        with pytest.raises(ProjectGitError, match="could not be removed"):
            store.materialize_source("p1", "api", str(tmp_path / "moved"), "main")


class TestSourceProvenanceRecord:
    """``_checkout_matches`` answers False for anything it cannot trust."""

    def _root(self, store: GitProjectStore) -> Path:
        root = store.registry.projects_dir / "state" / "p1" / "sources"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def test_a_matching_record_is_the_only_true(self, store: GitProjectStore) -> None:
        root = self._root(store)
        store._write_source_record(root, "api", "https://example.com/api", "main")

        assert store._checkout_matches(root, "api", "https://example.com/api", "main")
        assert not store._checkout_matches(root, "api", "https://example.com/other", "main")
        assert not store._checkout_matches(root, "api", "https://example.com/api", "next")

    def test_an_absent_record_is_a_mismatch(self, store: GitProjectStore) -> None:
        assert not store._checkout_matches(self._root(store), "api", "x", "")

    @pytest.mark.parametrize(
        "payload",
        [b"not json", b'"a string"', b"[1, 2]", b"null"],
    )
    def test_an_undecodable_record_is_a_mismatch(
        self, store: GitProjectStore, payload: bytes
    ) -> None:
        root = self._root(store)
        (root / "api.source.json").write_bytes(payload)

        assert not store._checkout_matches(root, "api", "x", "")

    def test_an_oversized_record_is_a_mismatch(self, store: GitProjectStore) -> None:
        root = self._root(store)
        (root / "api.source.json").write_bytes(
            b"x" * (GitProjectStore._SOURCE_RECORD_MAX_BYTES + 1)
        )

        assert not store._checkout_matches(root, "api", "x", "")

    def test_a_hardlinked_record_is_a_mismatch(self, store: GitProjectStore) -> None:
        root = self._root(store)
        store._write_source_record(root, "api", "x", "")
        os.link(root / "api.source.json", root / "second-name")

        assert not store._checkout_matches(root, "api", "x", "")

    def test_a_directory_at_the_record_path_is_a_mismatch(self, store: GitProjectStore) -> None:
        root = self._root(store)
        (root / "api.source.json").mkdir()

        assert not store._checkout_matches(root, "api", "x", "")

    def test_a_write_failure_leaves_no_temporary_behind(
        self, store: GitProjectStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = self._root(store)

        def boom(_src, _dst):
            raise OSError("replace failed")

        monkeypatch.setattr(project_git.os, "replace", boom)

        with pytest.raises(OSError, match="replace failed"):
            store._write_source_record(root, "api", "x", "")

        assert not list(root.glob(".api-record-*"))


@requires_local_git_remote
class TestSyncSource:
    def test_a_matching_checkout_fast_forwards_in_place(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        remote = str(tmp_path / "api")
        target = store.materialize_source("p1", "api", remote, "main")

        synced = store.sync_source("p1", "api", remote, "main")

        assert synced == target
        assert _fake(store).verbs() == ["clone", "fetch", "merge"]
        assert _fake(store).calls[-2][1] == ("fetch", "--", remote, "main")

    def test_a_branchless_declaration_fetches_only_the_remote(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        remote = str(tmp_path / "api")
        store.materialize_source("p1", "api", remote)

        store.sync_source("p1", "api", remote)

        assert _fake(store).calls[-2][1] == ("fetch", "--", remote)

    def test_a_declaration_that_moved_re_clones_instead_of_fetching(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        store.materialize_source("p1", "api", str(tmp_path / "api"), "main")

        store.sync_source("p1", "api", str(tmp_path / "moved"), "main")

        assert _fake(store).verbs() == ["clone", "clone"]

    def test_an_absent_checkout_is_cloned(self, store: GitProjectStore, tmp_path: Path) -> None:
        target = store.sync_source("p1", "api", str(tmp_path / "api"), "main")

        assert (target / ".git").exists()
        assert _fake(store).verbs() == ["clone"]


class TestPerSourceReporting:
    """``materialize_sources`` reports per-source failures but raises on the sandbox."""

    def _manifest(self, sources: tuple[ProjectSource, ...]) -> ProjectManifest:
        return ProjectManifest(
            name="Payments", description="", workspace_source="self", sources=sources
        )

    def test_a_source_without_a_url_is_reported(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        manifest = self._manifest(
            (
                ProjectSource(id="api", type="repo", config={"url": "   "}),
                ProjectSource(id="none", type="repo", config={}),
            )
        )

        failures = store.materialize_sources("p1", tmp_path, manifest)

        assert failures == {"api": "source declares no URL", "none": "source declares no URL"}

    def test_a_non_repo_source_is_skipped(self, store: GitProjectStore, tmp_path: Path) -> None:
        manifest = self._manifest(
            (ProjectSource(id="tickets", type="jira", config={"url": "https://x"}),)
        )

        assert store.materialize_sources("p1", tmp_path, manifest) == {}
        assert _fake(store).calls == []

    @requires_local_git_remote
    def test_a_non_string_branch_is_read_as_absent(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        manifest = self._manifest(
            (
                ProjectSource(
                    id="api",
                    type="repo",
                    config={"url": str(tmp_path / "api"), "default_branch": 7},
                ),
            )
        )

        assert store.materialize_sources("p1", tmp_path, manifest) == {}
        assert _fake(store).calls[0][1][:2] == ("clone", "--")

    @requires_local_git_remote
    def test_a_clone_failure_is_reported_not_raised(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        _fake(store).clone_error = ProjectGitError("Git project operation failed")
        manifest = self._manifest(
            (ProjectSource(id="api", type="repo", config={"url": str(tmp_path / "api")}),)
        )

        failures = store.materialize_sources("p1", tmp_path, manifest)

        assert failures == {"api": "Git project operation failed"}

    @requires_local_git_remote
    def test_sandbox_unavailability_is_raised(self, store: GitProjectStore, tmp_path: Path) -> None:
        _fake(store).clone_error = ProjectSandboxUnavailableError()
        manifest = self._manifest(
            (ProjectSource(id="api", type="repo", config={"url": str(tmp_path / "api")}),)
        )

        with pytest.raises(ProjectSandboxUnavailableError):
            store.materialize_sources("p1", tmp_path, manifest)

    @requires_local_git_remote
    def test_sync_sources_reports_each_source_that_advanced(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        manifest = self._manifest(
            (ProjectSource(id="api", type="repo", config={"url": str(tmp_path / "api")}),)
        )

        result = store.sync_sources("p1", tmp_path, manifest)

        assert (result.advanced, result.diverged, result.failures) == (["api"], [], {})
        assert _fake(store).verbs() == ["clone"]

    def test_registered_sources_are_not_materialized_before_acceptance(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        bundle = _local_bundle(
            tmp_path / "bundle", sources=[{"type": "repo", "url": str(tmp_path / "api")}]
        )
        project = store.registry.add_local(bundle)

        # No review record, so the declaration is unaccepted and nothing is
        # fetched -- reported as a no-op rather than as a failure, because the
        # Project's own health already says it is review-stale.
        assert store.materialize_registered_sources(project) == {}
        assert _fake(store).verbs() == []

    def test_registered_sources_need_a_readable_manifest(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "project.yaml").write_text("kind: NotAProject\n", encoding="utf-8")
        project = store.registry.add_local(_local_bundle(tmp_path / "ok"))
        registration = project.registrations[-1]
        object.__setattr__(registration, "path", bundle)

        with pytest.raises(ProjectManifestError, match="apiVersion"):
            store.materialize_registered_sources(project)

    @requires_local_git_remote
    def test_registered_sources_are_materialized_from_the_accepted_bundle(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        bundle = _local_bundle(
            tmp_path / "bundle", sources=[{"type": "repo", "url": str(tmp_path / "api")}]
        )
        project = store.registry.add_local(bundle)
        accepted = _accept_manifest(store.registry, project.id, bundle)

        assert store.materialize_registered_sources(accepted) == {}
        assert _fake(store).verbs() == ["clone"]

    @requires_local_git_remote
    def test_a_manifest_edited_after_acceptance_goes_back_to_pending(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        # Consent is keyed on the accepted manifest, so repointing a source --
        # the edit that would aim the fetch somewhere new -- un-accepts it.
        bundle = _local_bundle(
            tmp_path / "bundle", sources=[{"type": "repo", "url": str(tmp_path / "api")}]
        )
        project = store.registry.add_local(bundle)
        accepted = _accept_manifest(store.registry, project.id, bundle)
        _local_bundle(bundle, sources=[{"type": "repo", "url": str(tmp_path / "elsewhere")}])

        assert store.materialize_registered_sources(accepted) == {}
        assert _fake(store).verbs() == []


def _local_bundle(path: Path, sources: list[dict] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "project.yaml").write_text(_manifest_text(sources=sources), encoding="utf-8")
    return path


class TestResolveSource:
    def test_an_empty_remote_answers_none(self, store: GitProjectStore) -> None:
        assert store.resolve_source("p1", "api", remote="  ") is None

    def test_an_absent_checkout_answers_none(self, store: GitProjectStore) -> None:
        assert store.resolve_source("p1", "api") is None

    @requires_local_git_remote
    def test_a_matching_declaration_answers_the_checkout(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        remote = str(tmp_path / "api")
        target = store.materialize_source("p1", "api", remote, "main")

        assert store.resolve_source("p1", "api", remote=remote, default_branch="main") == target
        assert store.resolve_source("p1", "api") == target

    @requires_local_git_remote
    def test_a_declaration_that_moved_answers_none(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        store.materialize_source("p1", "api", str(tmp_path / "api"), "main")

        assert store.resolve_source("p1", "api", remote=str(tmp_path / "moved")) is None

    def test_a_path_that_is_not_a_checkout_is_refused(self, store: GitProjectStore) -> None:
        sources_root = store.registry.projects_dir / "state" / "p1" / "sources"
        (sources_root / "api").mkdir(parents=True)

        with pytest.raises(ProjectGitError, match="not a Git checkout"):
            store.resolve_source("p1", "api")


class TestRemoveDerivedState:
    """Both roots Crew owns, and the report a partial failure produces."""

    @requires_local_git_remote
    def test_both_owned_roots_are_removed(self, store: GitProjectStore, tmp_path: Path) -> None:
        store.materialize_source("p1", "api", str(tmp_path / "api"))
        managed = store.registry.projects_dir / "managed" / "p1" / "bundle"
        managed.mkdir(parents=True)

        assert store.remove_derived_state("p1") == []

        assert not (store.registry.projects_dir / "state" / "p1").exists()
        assert not (store.registry.projects_dir / "managed" / "p1").exists()

    def test_an_absent_state_root_is_not_an_error(self, store: GitProjectStore) -> None:
        assert store.remove_derived_state("never-registered") == []

    @requires_local_git_remote
    def test_an_unremovable_root_is_reported_rather_than_raised(
        self, store: GitProjectStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Cleanup is the second of removal's two operations, and the caller has
        # already forgotten the registration, so what remains is REPORTED: the
        # owner learns which files to delete instead of being told the removal
        # failed.
        store.materialize_source("p1", "api", str(tmp_path / "api"))
        (store.registry.projects_dir / "managed" / "p1").mkdir(parents=True)
        monkeypatch.setattr(platform_compat, "rmtree_force", lambda _path: False)

        assert store.remove_derived_state("p1") == ["state/p1/", "managed/p1/"]

    @requires_local_git_remote
    def test_a_local_bundle_registered_by_path_is_never_touched(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        bundle = _local_bundle(tmp_path / "owners-own-directory")
        project = store.registry.add_local(bundle)

        assert store.remove_derived_state(project.id) == []
        assert (bundle / "project.yaml").exists()


@requires_local_git_remote
class TestSync:
    def _managed(self, store: GitProjectStore, *, remote: str, branch: str = "main"):
        bundle = _local_bundle(store.registry.projects_dir / "managed" / "clone-1" / "bundle")
        return store.registry.add_managed(bundle, remote=remote, default_branch=branch)

    def test_a_local_only_project_cannot_be_synced(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        project = store.registry.add_local(_local_bundle(tmp_path / "bundle"))

        with pytest.raises(ProjectGitError, match="has no managed Git clone"):
            store.sync(project.id)

    @pytest.mark.parametrize("field", ["remote", "default_branch"])
    def test_an_unpinned_coordinate_is_refused(
        self,
        store: GitProjectStore,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        field: str,
    ) -> None:
        project = self._managed(store, remote=str(tmp_path / "remote"))
        # The store re-reads the registry, so the blank coordinate has to come
        # back from the resolve -- an in-memory edit of the returned record is
        # discarded. `add_managed` validates both, so only a registry written by
        # an older build can be missing one.
        blanked = dataclasses.replace(project.registrations[-1], **{field: "  "})
        doctored = dataclasses.replace(project, registrations=(blanked,))
        monkeypatch.setattr(store.registry, "resolve", lambda _identifier: doctored)

        with pytest.raises(ProjectGitError, match="remove and add it again"):
            store.sync(project.id)

    def test_a_sync_fetches_the_pinned_coordinates_and_fast_forwards(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        remote = str(tmp_path / "remote")
        project = self._managed(store, remote=remote)
        _fake(store).manifest = _manifest_text(name="Renamed")

        synced, result = store.sync(project.id)

        assert synced.id == project.id
        assert _fake(store).verbs() == ["fetch", "cat-file", "show", "merge"]
        assert _fake(store).calls[0][1] == ("fetch", "--", remote, "main")
        assert _fake(store).calls[-1][1] == ("merge", "--ff-only", "FETCH_HEAD")

    def test_an_accepted_manifest_source_is_synced_too(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        project = self._managed(store, remote=str(tmp_path / "remote"))
        bundle = project.registrations[-1].path
        _local_bundle(
            bundle, sources=[{"type": "repo", "url": str(tmp_path / "api"), "role": "primary"}]
        )
        accepted = _accept_manifest(store.registry, project.id, bundle)

        store.sync(accepted.id)

        assert "clone" in _fake(store).verbs()

    def test_an_unaccepted_manifest_source_is_not_fetched_by_a_sync(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        # The pull can land a manifest declaring a new host. Sync advances the
        # bundle and stops there: the new definition waits for the same
        # acceptance an added one waits for.
        project = self._managed(store, remote=str(tmp_path / "remote"))
        _local_bundle(
            project.registrations[-1].path,
            sources=[{"type": "repo", "url": str(tmp_path / "api"), "role": "primary"}],
        )

        store.sync(project.id)

        assert _fake(store).verbs() == ["fetch", "cat-file", "show", "merge"]

    def test_an_unreadable_remote_manifest_size_is_refused(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        project = self._managed(store, remote=str(tmp_path / "remote"))
        _fake(store).size = "not-a-number"

        with pytest.raises(ProjectGitError, match="manifest size is invalid"):
            store.sync(project.id)

    def test_an_oversized_remote_manifest_is_refused(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        project = self._managed(store, remote=str(tmp_path / "remote"))
        _fake(store).size = str(project_git.PROJECT_MANIFEST_MAX_BYTES + 1)

        with pytest.raises(ProjectGitError, match="manifest is too large"):
            store.sync(project.id)

    def test_an_invalid_remote_manifest_is_refused_before_the_merge(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        project = self._managed(store, remote=str(tmp_path / "remote"))
        _fake(store).manifest = "kind: NotAProject\n"

        with pytest.raises(ProjectManifestError, match="apiVersion"):
            store.sync(project.id)

        assert "merge" not in _fake(store).verbs()


@requires_local_git_remote
class TestAFastForwardThatCannotProceed:
    """Sync refuses a diverged checkout; it never rewrites one.

    The primary checkout is a session's working tree and is agent-writable, so
    local commits and uncommitted edits are ordinary states for it to be in. When
    ``merge --ff-only`` cannot proceed, the owner's own work is what stands in the
    way -- so the only safe answer is to name which state it is and stop.
    """

    #: Everything git offers for making a tree match a remote by force. None of it
    #: may appear on any call the store makes.
    DESTRUCTIVE = {"reset", "checkout", "clean", "stash", "restore", "switch"}

    def _managed(self, store: GitProjectStore, remote: str):
        bundle = _local_bundle(store.registry.projects_dir / "managed" / "clone-1" / "bundle")
        return store.registry.add_managed(bundle, remote=remote, default_branch="main")

    @pytest.mark.parametrize(
        ("kwargs", "detail"),
        [
            ({"status": " M src/app.py\n"}, "dirty-tree"),
            ({"is_ancestor": False}, "local-commits"),
            ({"is_ancestor": False, "shares_history": False}, "unrelated-history"),
        ],
    )
    def test_the_bundle_sync_names_which_state_blocks_it(
        self, store: GitProjectStore, tmp_path: Path, kwargs: dict, detail: str
    ) -> None:
        project = self._managed(store, str(tmp_path / "remote"))
        git = _fake(store)
        git.merge_error = ProjectGitError("Git project operation failed")
        for field, value in kwargs.items():
            setattr(git, field, value)

        with pytest.raises(project_git.ProjectCheckoutDivergedError) as exc_info:
            store.sync(project.id)

        error = exc_info.value
        assert error.code == "project_checkout_diverged"
        assert (error.project_id, error.checkout, error.detail) == (project.id, "bundle", detail)
        assert self.DESTRUCTIVE.isdisjoint(git.verbs())

    def test_a_dirty_tree_outranks_local_commits(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        # Both hold; the tree is what the owner must settle before either remedy
        # applies, so that is what they are told.
        project = self._managed(store, str(tmp_path / "remote"))
        git = _fake(store)
        git.merge_error = ProjectGitError("Git project operation failed")
        git.status = "?? untracked.txt\n"
        git.is_ancestor = False

        with pytest.raises(project_git.ProjectCheckoutDivergedError) as exc_info:
            store.sync(project.id)

        assert exc_info.value.detail == "dirty-tree"

    @pytest.mark.parametrize(
        "kwargs",
        [
            # A fast-forward WAS possible, so the merge failed for some other
            # reason; and a status the classifier cannot read establishes nothing.
            {"is_ancestor": True},
            {"status_error": ProjectGitError("Git project operation failed")},
        ],
    )
    def test_an_unclassifiable_failure_keeps_the_generic_message(
        self, store: GitProjectStore, tmp_path: Path, kwargs: dict
    ) -> None:
        project = self._managed(store, str(tmp_path / "remote"))
        git = _fake(store)
        git.merge_error = ProjectGitError("Git project operation failed")
        for field, value in kwargs.items():
            setattr(git, field, value)

        with pytest.raises(ProjectGitError) as exc_info:
            store.sync(project.id)

        assert not isinstance(exc_info.value, project_git.ProjectCheckoutDivergedError)
        assert "Git project operation failed" in str(exc_info.value)

    def test_a_sandbox_that_went_away_mid_merge_is_not_reported_as_divergence(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        project = self._managed(store, str(tmp_path / "remote"))
        _fake(store).merge_error = ProjectSandboxUnavailableError()

        with pytest.raises(ProjectSandboxUnavailableError):
            store.sync(project.id)

    def test_a_clean_checkout_still_fast_forwards(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        project = self._managed(store, str(tmp_path / "remote"))

        store.sync(project.id)

        # No classification probe runs on the happy path.
        assert _fake(store).verbs() == ["fetch", "cat-file", "show", "merge"]

    def test_a_diverged_source_checkout_names_the_source(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        remote = str(tmp_path / "api")
        target = store.materialize_source("p1", "api", remote, "main")
        git = _fake(store)
        git.merge_error = ProjectGitError("Git project operation failed")
        git.is_ancestor = False

        with pytest.raises(project_git.ProjectCheckoutDivergedError) as exc_info:
            store.sync_source("p1", "api", remote, "main")

        assert (exc_info.value.checkout, exc_info.value.detail) == ("api", "local-commits")
        assert exc_info.value.project_id == "p1"
        assert target.is_dir()
        assert self.DESTRUCTIVE.isdisjoint(git.verbs())

    def test_a_diverged_source_is_reported_beside_the_checkouts_that_advanced(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        """Each checkout is independent, so one refusal does not stop the others.

        A Project syncs N separate repositories and nothing can un-advance one
        that already fast-forwarded, so the honest answer is a report: every
        checkout that moved, and every one that could not with its own reason.
        """
        api_remote = tmp_path / "api"
        docs_remote = tmp_path / "docs"
        project = self._managed(store, str(tmp_path / "remote"))
        bundle = project.registrations[-1].path
        api = store.materialize_source(project.id, "api-x", str(api_remote), "")
        store.materialize_source(project.id, "docs-x", str(docs_remote), "")
        git = _fake(store)
        # Only the api checkout refuses; the bundle and the other source advance.
        git.merge_error = ProjectGitError("Git project operation failed")
        git.merge_error_paths = (api,)
        git.is_ancestor = False
        manifest = ProjectManifest(
            name="Payments",
            description="",
            workspace_source="docs-x",
            sources=(
                ProjectSource(id="api-x", type="repo", config={"url": str(api_remote)}),
                ProjectSource(id="docs-x", type="repo", config={"url": str(docs_remote)}),
            ),
        )

        result = store.sync_sources(project.id, bundle, manifest)

        assert result.advanced == ["docs-x"]
        assert result.diverged == [{"checkout": "api-x", "detail": "local-commits"}]
        assert result.failures == {}
        assert api.is_dir()
        assert self.DESTRUCTIVE.isdisjoint(git.verbs())

    def test_the_bundle_fast_forward_is_never_unwound_by_a_diverged_source(
        self, store: GitProjectStore, tmp_path: Path
    ) -> None:
        source_remote = tmp_path / "api"
        project = self._managed(store, str(tmp_path / "remote"))
        bundle = project.registrations[-1].path
        _local_bundle(
            bundle, sources=[{"type": "repo", "url": str(source_remote), "role": "primary"}]
        )
        accepted = _accept_manifest(store.registry, project.id, bundle)
        source_id = _synthesize_source_id(str(source_remote))
        source_target = store.materialize_source(project.id, source_id, str(source_remote), "")
        git = _fake(store)
        git.merge_error = ProjectGitError("Git project operation failed")
        git.merge_error_paths = (source_target,)
        git.is_ancestor = False

        with pytest.raises(project_git.ProjectCheckoutDivergedError) as exc_info:
            store.sync(accepted.id)

        error = exc_info.value
        assert error.advanced == ["bundle"]
        assert error.diverged == [{"checkout": source_id, "detail": "local-commits"}]
        # The top-level pair still names the first refusal, so a single-checkout
        # reading of the error is unchanged.
        assert (error.checkout, error.detail) == (source_id, "local-commits")
        assert self.DESTRUCTIVE.isdisjoint(git.verbs())


class TestSandboxErrorShape:
    def test_the_error_carries_its_code_and_the_host_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(project_git.sandbox, "unavailable_reason", lambda: "unshare: EPERM")

        error = ProjectSandboxUnavailableError()

        assert error.code == "project_sandbox_unavailable"
        assert str(error).endswith("unshare: EPERM")

    def test_a_missing_reason_leaves_the_message_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(project_git.sandbox, "unavailable_reason", lambda: "")

        assert str(ProjectSandboxUnavailableError()).endswith("it can enforce.")

    def test_manifest_errors_from_the_store_are_project_git_errors(self) -> None:
        assert issubclass(ProjectSandboxUnavailableError, ProjectGitError)
        assert not issubclass(ProjectManifestError, ProjectGitError)
