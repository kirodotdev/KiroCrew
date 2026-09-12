from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from conftest import make_dir_link
from kiro_crew import platform_compat
from kiro_crew.project_git import (
    GitProjectStore,
    ProjectGitError,
)
from kiro_crew.project_manifest import create_project_manifest
from kiro_crew.project_registry import ProjectRegistry
from kiro_crew.sandbox import SandboxUnavailableError

_GIT = platform_compat.trusted_git_bin()
_PROJECT_ID = "018f4f4a-760f-7a8b-a5d4-5a7e0f130d4e"
pytestmark = pytest.mark.skipif(_GIT is None, reason="trusted git executable is unavailable")


@pytest.fixture(autouse=True)
def _available_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep transport tests independent of the runner's user-namespace policy."""

    def passthrough(
        argv: list[str], *, mode: str, env: dict[str, str] | None = None
    ) -> tuple[list[str], dict[str, str], None]:
        return list(argv), dict(env or {}), None

    monkeypatch.setattr("kiro_crew.project_git.sandboxed_spawn_argv", passthrough)


def _git(cwd: Path, *args: str) -> None:
    assert _GIT is not None
    subprocess.run(
        [_GIT, *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def _committed_bundle(path: Path) -> str:
    manifest = create_project_manifest(path, name="Payments")
    _git(path, "init")
    _git(path, "add", "project.yaml")
    _git(
        path,
        "-c",
        "user.name=Project Tests",
        "-c",
        "user.email=projects@example.invalid",
        "commit",
        "-m",
        "add project bundle",
    )
    return manifest.id


class TestGitProjectStore:
    @pytest.mark.parametrize("branch", ["-uploader=malicious", "--upload-pack=malicious"])
    def test_branch_names_cannot_be_parsed_as_git_options(self, branch: str) -> None:
        with pytest.raises(ProjectGitError, match="branch is invalid"):
            GitProjectStore._validate_branch(branch)

    @pytest.mark.parametrize("as_uri", [False, True])
    def test_local_remote_cannot_read_a_sensitive_path(self, as_uri: bool) -> None:
        sensitive = Path.home() / ".ssh" / "project-bundle"
        remote = sensitive.as_uri() if as_uri else str(sensitive)

        with pytest.raises(ProjectGitError, match="sensitive path"):
            GitProjectStore._validate_remote(remote)

    def test_file_remote_cannot_name_a_network_authority(self) -> None:
        with pytest.raises(ProjectGitError, match="must be local"):
            GitProjectStore._validate_remote("file://fileserver/projects/payments.git")

    def test_file_remote_rejects_a_percent_encoded_nul(self) -> None:
        with pytest.raises(ProjectGitError, match="invalid characters"):
            GitProjectStore._validate_remote("file:///tmp/%00payments.git")

    def test_malformed_bracketed_remote_is_a_project_error(self) -> None:
        with pytest.raises(ProjectGitError, match="invalid Git remote"):
            GitProjectStore._validate_remote("http://[::1")

    def test_add_rejects_a_planted_git_locks_directory(self, tmp_path: Path) -> None:
        remote = tmp_path / "remote"
        _committed_bundle(remote)
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        state = registry.projects_dir / "state"
        state.mkdir(parents=True)
        external = tmp_path / "external-locks"
        external.mkdir()
        make_dir_link(state / "git-locks", external)

        with pytest.raises(ProjectGitError, match="link"):
            GitProjectStore(registry).add(remote.as_uri())

        assert not any(external.iterdir())

    def test_add_rejects_a_planted_managed_root(self, tmp_path: Path) -> None:
        remote = tmp_path / "remote"
        _committed_bundle(remote)
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        registry.projects_dir.mkdir(parents=True)
        external = tmp_path / "external-managed"
        external.mkdir()
        make_dir_link(registry.projects_dir / "managed", external)

        with pytest.raises(ProjectGitError, match="link"):
            GitProjectStore(registry).add(remote.as_uri())

        assert not any(external.iterdir())

    def test_add_rejects_a_planted_managed_project_directory(self, tmp_path: Path) -> None:
        remote = tmp_path / "remote"
        project_id = _committed_bundle(remote)
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        managed_root = registry.projects_dir / "managed"
        managed_root.mkdir(parents=True)
        external = tmp_path / "external-project"
        external.mkdir()
        make_dir_link(managed_root / project_id, external)

        with pytest.raises(ProjectGitError, match="link"):
            GitProjectStore(registry).add(remote.as_uri())

        assert not (external / "bundle").exists()

    def test_relative_local_remote_is_resolved_against_its_declaring_bundle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        expected = (bundle / "repos" / "api").resolve()
        checked: list[str] = []

        def sensitive(path: str) -> bool:
            checked.append(path)
            return False

        monkeypatch.setattr("kiro_crew.project_git.is_sensitive_path", sensitive)

        remote = GitProjectStore._validate_remote("repos/api", base_dir=bundle)

        assert remote == str(expected)
        assert checked == [str(expected)]

    def test_add_rejects_http_remote_with_embedded_credentials(self, tmp_path: Path) -> None:
        registry = ProjectRegistry(tmp_path / "data" / "projects")

        with pytest.raises(ProjectGitError, match="credential helper"):
            GitProjectStore(registry).add("https://secret-token@example.invalid/payments.git")

    def test_add_rejects_uppercase_http_remote_with_embedded_credentials(
        self, tmp_path: Path
    ) -> None:
        registry = ProjectRegistry(tmp_path / "data" / "projects")

        with pytest.raises(ProjectGitError, match="credential helper"):
            GitProjectStore(registry).add("HTTPS://secret-token@example.invalid/payments.git")

    def test_add_rejects_remote_helper_protocol(self, tmp_path: Path) -> None:
        registry = ProjectRegistry(tmp_path / "data" / "projects")

        with pytest.raises(ProjectGitError, match="unsupported Git remote protocol"):
            GitProjectStore(registry).add("ext::sh -c touch% /tmp/project-git-marker")

    def test_git_runs_through_the_sandbox_and_resource_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cleanup = tmp_path / "sandbox-profile"
        cleanup.write_text("profile", encoding="utf-8")
        captured: dict[str, Any] = {}

        def fake_sandboxed_spawn_argv(
            argv: list[str], *, mode: str, env: dict[str, str] | None = None
        ) -> tuple[list[str], dict[str, str], str]:
            captured["sandbox_argv"] = argv
            captured["sandbox_mode"] = mode
            return ["sandbox-wrapper", *argv], {**(env or {}), "SAFE_ENV": "1"}, str(cleanup)

        def fake_run_limited(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["run_argv"] = argv
            captured["run_kwargs"] = kwargs
            return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

        monkeypatch.setattr("kiro_crew.project_git.sandboxed_spawn_argv", fake_sandboxed_spawn_argv)
        monkeypatch.setattr("kiro_crew.project_git.run_limited", fake_run_limited)

        result = GitProjectStore._run_git(tmp_path, "status", "--short")

        assert result.stdout == "ok\n"
        assert captured["sandbox_mode"] == "standard"
        assert captured["run_argv"][0] == "sandbox-wrapper"
        assert captured["run_kwargs"]["cwd"] == tmp_path
        assert captured["run_kwargs"]["env"]["SAFE_ENV"] == "1"
        assert captured["run_kwargs"]["env"]["GIT_TERMINAL_PROMPT"] == "0"
        config_values = {
            value
            for key, value in captured["run_kwargs"]["env"].items()
            if key.startswith("GIT_CONFIG_VALUE_")
        }
        assert "never" in config_values
        assert os.devnull in config_values
        assert not cleanup.exists()

    def test_git_transport_helpers_use_the_dedicated_trusted_resolver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.project_git.platform_compat.trusted_git_helper_bin",
            lambda name: f"/trusted/git/{name}",
        )
        monkeypatch.setattr(
            "kiro_crew.project_git.run_limited",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout="", stderr=""),
        )

        env = GitProjectStore._credential_helper_env()
        config = {
            env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
            for index in range(int(env["GIT_CONFIG_COUNT"]))
        }

        assert config["remote.origin.uploadpack"] == "/trusted/git/git-upload-pack"
        assert config["remote.origin.receivepack"] == "/trusted/git/git-receive-pack"

    def test_git_transport_helpers_are_shell_quoted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        helper_root = r"C:\Program Files\Git\mingw64\bin"
        monkeypatch.setattr(
            "kiro_crew.project_git.platform_compat.trusted_git_helper_bin",
            lambda name: rf"{helper_root}\{name}.exe",
        )
        monkeypatch.setattr(
            "kiro_crew.project_git.run_limited",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout="", stderr=""),
        )

        env = GitProjectStore._credential_helper_env()
        config = {
            env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
            for index in range(int(env["GIT_CONFIG_COUNT"]))
        }

        assert config["remote.origin.uploadpack"] == (
            r"'C:\Program Files\Git\mingw64\bin\git-upload-pack.exe'"
        )
        assert config["remote.origin.receivepack"] == (
            r"'C:\Program Files\Git\mingw64\bin\git-receive-pack.exe'"
        )

    def test_keychain_credential_helper_is_rewritten_to_a_trusted_absolute_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planted = tmp_path / "git-credential-osxkeychain"
        planted.write_text("malicious", encoding="utf-8")
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.project_git.platform_compat.trusted_git_helper_bin",
            lambda name: f"/trusted/git/{name}",
        )

        helper = GitProjectStore._sanitize_credential_helper("osxkeychain")

        assert helper == "!/trusted/git/git-credential-osxkeychain"

    def test_keychain_credential_helper_is_rejected_without_a_trusted_executable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.project_git.platform_compat.trusted_git_helper_bin",
            lambda _name: None,
        )

        assert GitProjectStore._sanitize_credential_helper("osxkeychain") is None

    def test_github_cli_credential_helper_quotes_its_trusted_windows_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.project_git.platform_compat.trusted_system_bin",
            lambda _name: r"C:\Program Files\GitHub CLI\gh.exe",
        )

        helper = GitProjectStore._sanitize_credential_helper("!gh auth git-credential")

        assert helper == r"!'C:\Program Files\GitHub CLI\gh.exe' auth git-credential"

    def test_windows_project_git_enables_long_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kiro_crew.project_git.platform_compat.IS_WINDOWS", True)
        monkeypatch.setattr(
            "kiro_crew.project_git.platform_compat.trusted_git_bin", lambda: str(_GIT)
        )
        monkeypatch.setattr(
            "kiro_crew.project_git.run_limited",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout="", stderr=""),
        )

        env = GitProjectStore._credential_helper_env()
        config = {
            env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
            for index in range(int(env["GIT_CONFIG_COUNT"]))
        }

        assert config["core.longpaths"] == "true"

    def test_git_failure_does_not_echo_remote_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        failure = subprocess.CalledProcessError(
            returncode=128,
            cmd=["git", "clone"],
            stderr="fatal: https://secret-token@example.invalid/repo.git failed",
        )
        monkeypatch.setattr(
            "kiro_crew.project_git.run_limited",
            lambda *args, **kwargs: (_ for _ in ()).throw(failure),
        )

        with pytest.raises(ProjectGitError) as exc_info:
            GitProjectStore._run_git(tmp_path, "clone", "remote", "target")

        assert "secret-token" not in str(exc_info.value)

    def test_fetch_failure_uses_the_single_project_git_error_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        failure = subprocess.CalledProcessError(returncode=128, cmd=["git", "fetch"])
        monkeypatch.setattr(
            "kiro_crew.project_git.run_limited",
            lambda *args, **kwargs: (_ for _ in ()).throw(failure),
        )

        with pytest.raises(ProjectGitError) as exc_info:
            GitProjectStore._run_git(tmp_path, "fetch", "--", "origin", "main")

        assert type(exc_info.value) is ProjectGitError

    def test_git_refuses_to_run_without_a_sandbox(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unavailable(*args: Any, **kwargs: Any) -> tuple[list[str], dict[str, str], None]:
            raise SandboxUnavailableError(
                "sandbox unavailable", kind="no_backend", detail="test backend unavailable"
            )

        monkeypatch.setattr("kiro_crew.project_git.sandboxed_spawn_argv", unavailable)

        with pytest.raises(ProjectGitError, match="requires an available sandbox"):
            GitProjectStore._run_git(tmp_path, "status", "--short")

    def test_add_clones_to_managed_id_path(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        project_id = _committed_bundle(source)
        registry = ProjectRegistry(tmp_path / "data" / "projects")

        project = GitProjectStore(registry).add(source.as_uri())

        expected = registry.projects_dir / "managed" / project_id / "bundle"
        assert project.id == project_id
        assert project.registrations[-1].path == expected
        assert project.registrations[-1].origin == "managed_git"
        assert project.registrations[-1].remote == source.as_uri()
        assert (expected / ".git").exists()

    def test_readd_refreshes_a_preserved_unregistered_managed_clone(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        project_id = _committed_bundle(source)
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        store = GitProjectStore(registry)
        original = store.add(source.as_uri())
        managed = original.registrations[-1].path
        registry.unregister(project_id)
        (source / "README.md").write_text("fresh upstream content\n", encoding="utf-8")
        _git(source, "add", "README.md")
        _git(
            source,
            "-c",
            "user.name=Project Tests",
            "-c",
            "user.email=projects@example.invalid",
            "commit",
            "-m",
            "update upstream bundle",
        )

        refreshed = store.add(source.as_uri())

        assert refreshed.registrations[-1].path == managed
        assert (managed / "README.md").read_text(encoding="utf-8") == ("fresh upstream content\n")

    def test_add_rejects_a_different_remote_with_an_existing_project_id(
        self, tmp_path: Path
    ) -> None:
        first = tmp_path / "first"
        project_id = _committed_bundle(first)
        second = tmp_path / "second"
        second_id = _committed_bundle(second)
        manifest_path = second / "project.yaml"
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8").replace(second_id, project_id),
            encoding="utf-8",
        )
        _git(second, "add", "project.yaml")
        _git(
            second,
            "-c",
            "user.name=Project Tests",
            "-c",
            "user.email=projects@example.invalid",
            "commit",
            "-m",
            "use shared project identity",
        )
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        store = GitProjectStore(registry)
        original = store.add(first.as_uri())

        with pytest.raises(ProjectGitError, match="different remote"):
            store.add(second.as_uri())

        registered = registry.get(project_id)
        assert registered.registrations == original.registrations
        assert registered.registrations[-1].remote == first.as_uri()

    def test_sync_fetches_and_fast_forwards_managed_clone(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        project_id = _committed_bundle(source)
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        store = GitProjectStore(registry)
        store.add(source.as_uri())
        (source / "README.md").write_text("shared context\n", encoding="utf-8")
        _git(source, "add", "README.md")
        _git(
            source,
            "-c",
            "user.name=Project Tests",
            "-c",
            "user.email=projects@example.invalid",
            "commit",
            "-m",
            "add shared context",
        )

        project = store.sync(project_id)

        managed = project.registrations[-1].path
        assert (managed / "README.md").read_text(encoding="utf-8") == "shared context\n"

    def test_add_pins_the_remote_and_branch_in_the_registry(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        project_id = _committed_bundle(source)
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        store = GitProjectStore(registry)

        project = store.add(source.as_uri())

        registration = project.registrations[-1]
        assert registration.origin == "managed_git"
        assert registration.remote == source.as_uri()
        branch = GitProjectStore._run_git(source, "symbolic-ref", "--short", "HEAD").stdout.strip()
        assert registration.default_branch == branch
        # Round-trips through the persisted registry, not just the in-memory object.
        reloaded = ProjectRegistry(tmp_path / "data" / "projects").get(project_id)
        assert reloaded.registrations[-1].default_branch == branch

    def test_sync_ignores_an_origin_rewritten_inside_the_checkout(self, tmp_path: Path) -> None:
        # The managed clone is agent-writable derived state. An agent that
        # rewrites `.git/config` (or moves HEAD) must not choose what the owner's
        # sync fetches: the fetch names the registry's pinned remote and branch.
        source = tmp_path / "source"
        project_id = _committed_bundle(source)
        attacker = tmp_path / "attacker"
        _committed_bundle(attacker)
        (attacker / "PAYLOAD.md").write_text("attacker content\n", encoding="utf-8")
        _git(attacker, "add", "PAYLOAD.md")
        _git(
            attacker,
            "-c",
            "user.name=Project Tests",
            "-c",
            "user.email=projects@example.invalid",
            "commit",
            "-m",
            "payload",
        )
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        store = GitProjectStore(registry)
        managed = store.add(source.as_uri()).registrations[-1].path
        _git(managed, "remote", "set-url", "origin", attacker.as_uri())
        (source / "README.md").write_text("legitimate update\n", encoding="utf-8")
        _git(source, "add", "README.md")
        _git(
            source,
            "-c",
            "user.name=Project Tests",
            "-c",
            "user.email=projects@example.invalid",
            "commit",
            "-m",
            "legitimate",
        )

        store.sync(project_id)

        assert (managed / "README.md").read_text(encoding="utf-8") == "legitimate update\n"
        assert not (managed / "PAYLOAD.md").exists()

    def test_sync_refuses_a_registration_without_pinned_coordinates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import dataclasses

        source = tmp_path / "source"
        project_id = _committed_bundle(source)
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        store = GitProjectStore(registry)
        project = store.add(source.as_uri())
        # A registration without a pinned branch (an older registry entry)
        # cannot be synced from the checkout's own HEAD instead.
        unpinned = dataclasses.replace(
            project,
            registrations=tuple(
                dataclasses.replace(registration, default_branch="")
                for registration in project.registrations
            ),
        )
        monkeypatch.setattr(registry, "resolve", lambda _identifier: unpinned)

        with pytest.raises(ProjectGitError, match="pinned branch"):
            store.sync(project_id)

    def test_sync_rejects_a_remote_manifest_identity_change(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        project_id = _committed_bundle(source)
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        store = GitProjectStore(registry)
        project = store.add(source.as_uri())
        managed = project.registrations[-1].path
        original_head = GitProjectStore._run_git(managed, "rev-parse", "HEAD").stdout.strip()
        manifest_path = source / "project.yaml"
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8").replace(project_id, _PROJECT_ID),
            encoding="utf-8",
        )
        _git(source, "add", "project.yaml")
        _git(
            source,
            "-c",
            "user.name=Project Tests",
            "-c",
            "user.email=projects@example.invalid",
            "commit",
            "-m",
            "change project identity",
        )

        with pytest.raises(ProjectGitError, match="identity changed"):
            store.sync(project_id)

        assert (
            GitProjectStore._run_git(managed, "rev-parse", "HEAD").stdout.strip() == original_head
        )
        assert registry.get(project_id).registrations[-1].path == managed

    def test_sync_rejects_an_oversized_remote_manifest_before_reading_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "source"
        project_id = _committed_bundle(source)
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        store = GitProjectStore(registry)
        store.add(source.as_uri())
        (source / "project.yaml").write_text("#" + ("x" * (1024 * 1024)), encoding="utf-8")
        _git(source, "add", "project.yaml")
        _git(
            source,
            "-c",
            "user.name=Project Tests",
            "-c",
            "user.email=projects@example.invalid",
            "commit",
            "-m",
            "oversize project manifest",
        )
        real_run_git = store._run_git
        calls: list[tuple[str, ...]] = []

        def tracked_run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return real_run_git(cwd, *args)

        monkeypatch.setattr(store, "_run_git", tracked_run_git)

        with pytest.raises(ProjectGitError, match="manifest is too large"):
            store.sync(project_id)

        assert not any(args[:1] == ("show",) for args in calls)

    def test_sync_refuses_to_mutate_external_checkout(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        project_id = _committed_bundle(source)
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        registry.add_local(source)

        with pytest.raises(ValueError, match="no managed Git clone"):
            GitProjectStore(registry).sync(project_id)

    def test_materialize_source_checks_out_and_syncs_the_declared_branch(
        self, tmp_path: Path
    ) -> None:
        remote = tmp_path / "remote"
        remote.mkdir()
        _git(remote, "init")
        (remote / "branch.txt").write_text("default\n", encoding="utf-8")
        _git(remote, "add", "branch.txt")
        _git(
            remote,
            "-c",
            "user.name=Project Tests",
            "-c",
            "user.email=projects@example.invalid",
            "commit",
            "-m",
            "default branch",
        )
        _git(remote, "checkout", "-b", "trunk")
        (remote / "branch.txt").write_text("trunk\n", encoding="utf-8")
        _git(remote, "add", "branch.txt")
        _git(
            remote,
            "-c",
            "user.name=Project Tests",
            "-c",
            "user.email=projects@example.invalid",
            "commit",
            "-m",
            "trunk branch",
        )
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        store = GitProjectStore(registry)

        checkout = store.materialize_source(_PROJECT_ID, "api", remote.as_uri(), "trunk")

        assert (checkout / "branch.txt").read_text(encoding="utf-8") == "trunk\n"
        assert (
            GitProjectStore._run_git(checkout, "symbolic-ref", "--short", "HEAD").stdout.strip()
            == "trunk"
        )

    def test_materialize_source_never_reopens_an_existing_checkout_for_git(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        remote = tmp_path / "remote"
        _committed_bundle(remote)
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        store = GitProjectStore(registry)
        checkout = store.materialize_source(_PROJECT_ID, "api", remote.as_uri())
        (checkout / "kept.txt").write_text("reused\n", encoding="utf-8")

        monkeypatch.setattr(
            store,
            "_run_git",
            lambda *_args, **_kwargs: pytest.fail(
                "existing checkout was reopened by name after validation"
            ),
        )

        resolved = store.materialize_source(_PROJECT_ID, "api", remote.as_uri())

        assert resolved == checkout
        # Reused, not re-cloned: a file planted in the tree survives.
        assert (resolved / "kept.txt").read_text(encoding="utf-8") == "reused\n"

    def test_materialize_source_replaces_a_checkout_whose_declaration_moved(
        self, tmp_path: Path
    ) -> None:
        # The source id is manifest-declared and independent of the URL, so a
        # declaration that now names another remote must not keep serving the
        # old tree under the same id.
        first = tmp_path / "first"
        _committed_bundle(first)
        second = tmp_path / "second"
        second.mkdir()
        _git(second, "init")
        (second / "marker.txt").write_text("second\n", encoding="utf-8")
        _git(second, "add", "marker.txt")
        _git(
            second,
            "-c",
            "user.name=Project Tests",
            "-c",
            "user.email=projects@example.invalid",
            "commit",
            "-m",
            "second remote",
        )
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        store = GitProjectStore(registry)
        checkout = store.materialize_source(_PROJECT_ID, "api", first.as_uri())
        assert not (checkout / "marker.txt").exists()

        replaced = store.materialize_source(_PROJECT_ID, "api", second.as_uri())

        assert replaced == checkout
        assert (replaced / "marker.txt").read_text(encoding="utf-8") == "second\n"
        # And the record now vouches for the new declaration: a third call reuses.
        (replaced / "kept.txt").write_text("reused\n", encoding="utf-8")
        again = store.materialize_source(_PROJECT_ID, "api", second.as_uri())
        assert again == checkout
        assert (again / "kept.txt").read_text(encoding="utf-8") == "reused\n"

    def test_materialize_source_replaces_a_checkout_with_no_provenance_record(
        self, tmp_path: Path
    ) -> None:
        remote = tmp_path / "remote"
        _committed_bundle(remote)
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        store = GitProjectStore(registry)
        checkout = store.materialize_source(_PROJECT_ID, "api", remote.as_uri())
        record = checkout.parent / "api.source.json"
        assert record.is_file()
        record.unlink()
        (checkout / "planted.txt").write_text("stale\n", encoding="utf-8")

        replaced = store.materialize_source(_PROJECT_ID, "api", remote.as_uri())

        assert replaced == checkout
        assert not (replaced / "planted.txt").exists()
        assert record.is_file()

    def test_materialize_source_keeps_the_previous_checkout_when_the_record_write_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The old tree is discarded only once its replacement is in place AND
        # recorded. A record write that fails puts the old tree back under its
        # own still-valid record instead of leaving the replacement unrecorded
        # and the previous checkout gone.
        first = tmp_path / "first"
        _committed_bundle(first)
        second = tmp_path / "second"
        second.mkdir()
        _git(second, "init")
        (second / "marker.txt").write_text("second\n", encoding="utf-8")
        _git(second, "add", "marker.txt")
        _git(
            second,
            "-c",
            "user.name=Project Tests",
            "-c",
            "user.email=projects@example.invalid",
            "commit",
            "-m",
            "second remote",
        )
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        store = GitProjectStore(registry)
        checkout = store.materialize_source(_PROJECT_ID, "api", first.as_uri())
        (checkout / "local-work.txt").write_text("keep me\n", encoding="utf-8")
        record = checkout.parent / "api.source.json"
        record_before = record.read_bytes()

        def failing_record(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(store, "_write_source_record", failing_record)
        with pytest.raises(OSError, match="disk full"):
            store.materialize_source(_PROJECT_ID, "api", second.as_uri())

        assert (checkout / "local-work.txt").read_text(encoding="utf-8") == "keep me\n"
        assert not (checkout / "marker.txt").exists()
        assert record.read_bytes() == record_before
        # No staging or backup directory is left beside the restored checkout.
        assert sorted(p.name for p in checkout.parent.iterdir()) == ["api", "api.source.json"]

    def test_materialize_source_leaves_nothing_when_the_first_record_write_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        remote = tmp_path / "remote"
        _committed_bundle(remote)
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        store = GitProjectStore(registry)

        def failing_record(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(store, "_write_source_record", failing_record)
        with pytest.raises(OSError, match="disk full"):
            store.materialize_source(_PROJECT_ID, "api", remote.as_uri())

        sources_root = registry.projects_dir / "state" / _PROJECT_ID / "sources"
        assert not sources_root.exists() or list(sources_root.iterdir()) == []

    def test_materialize_source_replaces_a_checkout_whose_branch_declaration_moved(
        self, tmp_path: Path
    ) -> None:
        remote = tmp_path / "remote"
        _committed_bundle(remote)
        _git(remote, "checkout", "-b", "trunk")
        (remote / "trunk.txt").write_text("trunk\n", encoding="utf-8")
        _git(remote, "add", "trunk.txt")
        _git(
            remote,
            "-c",
            "user.name=Project Tests",
            "-c",
            "user.email=projects@example.invalid",
            "commit",
            "-m",
            "trunk",
        )
        _git(remote, "checkout", "-")
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        store = GitProjectStore(registry)
        checkout = store.materialize_source(_PROJECT_ID, "api", remote.as_uri())
        assert not (checkout / "trunk.txt").exists()

        replaced = store.materialize_source(_PROJECT_ID, "api", remote.as_uri(), "trunk")

        assert replaced == checkout
        assert (replaced / "trunk.txt").read_text(encoding="utf-8") == "trunk\n"

    def test_resolve_source_refuses_a_checkout_the_declaration_no_longer_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The read-only attachment path: a sync that repointed the source's URL
        # or branch after activation must make the old checkout unavailable
        # here -- never silently served, and never re-cloned from this path.
        remote = tmp_path / "remote"
        _committed_bundle(remote)
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        store = GitProjectStore(registry)
        checkout = store.materialize_source(_PROJECT_ID, "api", remote.as_uri())
        monkeypatch.setattr(
            store,
            "_run_git",
            lambda *_args, **_kwargs: pytest.fail("resolve_source must never run git"),
        )

        assert store.resolve_source(_PROJECT_ID, "api", remote=remote.as_uri()) == checkout
        assert (
            store.resolve_source(_PROJECT_ID, "api", remote=remote.as_uri(), default_branch="trunk")
            is None
        )
        assert (
            store.resolve_source(_PROJECT_ID, "api", remote=(tmp_path / "other").as_uri()) is None
        )
        assert store.resolve_source(_PROJECT_ID, "api", remote="") is None
        # The tree itself is untouched by the refusals.
        assert (checkout / ".git").exists()

    def test_materialize_source_rejects_a_planted_checkout_link(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        remote = tmp_path / "remote"
        _committed_bundle(remote)
        external = tmp_path / "external-checkout"
        _git(tmp_path, "clone", str(remote), str(external))
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        target = registry.projects_dir / "state" / _PROJECT_ID / "sources" / "api"
        target.parent.mkdir(parents=True)
        make_dir_link(target, external)
        store = GitProjectStore(registry)
        monkeypatch.setattr(
            store,
            "_run_git",
            lambda *_args, **_kwargs: pytest.fail("Git followed a planted checkout link"),
        )

        with pytest.raises(ProjectGitError, match="link"):
            store.materialize_source(_PROJECT_ID, "api", remote.as_uri())

    def test_materialize_source_rejects_a_planted_derived_state_ancestor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        remote = tmp_path / "remote"
        _committed_bundle(remote)
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        source_parent = registry.projects_dir / "state" / _PROJECT_ID
        source_parent.mkdir(parents=True)
        planted = tmp_path / "planted-sources"
        planted.mkdir()
        make_dir_link(source_parent / "sources", planted)
        store = GitProjectStore(registry)
        monkeypatch.setattr(
            store,
            "_run_git",
            lambda *_args, **_kwargs: pytest.fail("Git followed a planted state ancestor"),
        )

        with pytest.raises(ProjectGitError, match="link"):
            store.materialize_source(_PROJECT_ID, "api", remote.as_uri())
