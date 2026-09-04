"""Managed Git transport for portable Project bundles."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import unquote, urlsplit

from kiro_crew import platform_compat
from kiro_crew.platform.update_governance import git_command_env, repo_exec_config_reason
from kiro_crew.project_manifest import (
    PROJECT_MANIFEST_MAX_BYTES,
    ProjectManifestError,
    load_project_manifest,
    load_project_manifest_text,
)
from kiro_crew.project_registry import ProjectRegistration, ProjectRegistry, RegisteredProject
from kiro_crew.sandbox import SandboxUnavailableError, run_limited, sandboxed_spawn_argv
from kiro_crew.security import is_sensitive_path

_GIT_TIMEOUT_SECONDS = 120
_SUPPORTED_REMOTE_SCHEMES = frozenset({"file", "git", "http", "https", "ssh"})
_BRANCH_RE = re.compile(r"^(?![-./])(?!.*(?:\.\.|//|@\{|\\|[~^:?*\[]))[^\x00-\x20\x7f]+(?<![./])$")
_KEYCHAIN_HELPERS = frozenset({"libsecret", "manager", "manager-core", "osxkeychain", "wincred"})
_MAX_CREDENTIAL_HELPERS = 8
_ORIGIN_HELPER_CONFIG = {
    "remote.origin.uploadpack": "git-upload-pack",
    "remote.origin.receivepack": "git-receive-pack",
}


class ProjectGitError(ValueError):
    """A managed Project clone could not be created or synchronized safely."""


class GitProjectStore:
    """Own managed Project clones while leaving external checkouts untouched."""

    def __init__(self, registry: ProjectRegistry | None = None) -> None:
        self.registry = registry or ProjectRegistry()

    @staticmethod
    def _git_executable() -> str:
        executable = platform_compat.trusted_git_bin()
        if executable is None:
            raise ProjectGitError("a trusted Git executable is unavailable")
        return executable

    @staticmethod
    def _validate_remote(remote: str, *, base_dir: Path | None = None) -> str:
        remote = remote.strip()
        if not remote:
            raise ProjectGitError("Git Project remote must not be empty")
        if any(character in remote for character in ("\x00", "\r", "\n")):
            raise ProjectGitError("Git Project remote contains invalid characters")
        try:
            parsed = urlsplit(remote)
        except ValueError as exc:
            raise ProjectGitError("invalid Git remote URL") from exc
        scheme = parsed.scheme.lower()
        windows_drive = len(parsed.scheme) == 1 and remote[1:3] in {":\\", ":/"}
        scp_style = bool(re.fullmatch(r"(?:[^/@:\s]+@)?[^/:\s]+:[^:\s].*", remote))
        if (
            parsed.scheme
            and not windows_drive
            and not scp_style
            and scheme not in _SUPPORTED_REMOTE_SCHEMES
        ):
            raise ProjectGitError("unsupported Git remote protocol")
        if scheme in {"http", "https"} and (
            parsed.username is not None or parsed.query or parsed.fragment
        ):
            raise ProjectGitError(
                "HTTP Git remotes must use a credential helper, not credentials in the URL"
            )
        if parsed.password is not None:
            raise ProjectGitError("Git remotes must not include a password")
        local_path: str | None = None
        if scheme == "file":
            if parsed.query or parsed.fragment:
                raise ProjectGitError("file Git remotes must not include a query or fragment")
            if parsed.netloc and parsed.netloc.lower() != "localhost":
                raise ProjectGitError("file Git remotes must be local")
            local_path = unquote(parsed.path)
            if any(character in local_path for character in ("\x00", "\r", "\n")):
                raise ProjectGitError("Git Project remote contains invalid characters")
            if re.match(r"^/[A-Za-z]:[/\\]", local_path):
                local_path = local_path[1:]
            if not Path(local_path).is_absolute() and not re.match(r"^[A-Za-z]:[/\\]", local_path):
                raise ProjectGitError("file Git remotes must use an absolute path")
        elif (not parsed.scheme and not scp_style) or windows_drive:
            local_path = remote
        if local_path is not None:
            local = Path(local_path).expanduser()
            if not local.is_absolute() and not windows_drive:
                local = (base_dir or Path.cwd()) / local
            normalized = str(local.resolve(strict=False)) if not windows_drive else str(local)
            if is_sensitive_path(normalized):
                raise ProjectGitError("Git Project remote is a sensitive path")
            if scheme != "file":
                return normalized
        return remote

    @staticmethod
    def _validate_branch(default_branch: str) -> str:
        branch = default_branch.strip()
        if branch and not _BRANCH_RE.fullmatch(branch):
            raise ProjectGitError("Project repo default branch is invalid")
        return branch

    @contextmanager
    def _lock(self, name: str) -> Iterator[None]:
        locks_dir = self.registry.projects_dir / "state" / "git-locks"
        self._assert_derived_path_unlinked(locks_dir)
        platform_compat.make_owner_only_dir(locks_dir)
        lock_path = locks_dir / f"{name}.lock"
        self._assert_derived_path_unlinked(lock_path)
        try:
            fd = os.open(
                str(lock_path),
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            raise ProjectGitError("Project Git lock path is not safe") from exc
        try:
            with platform_compat.file_lock(fd, exclusive=True, required=True):
                yield
        finally:
            os.close(fd)

    @staticmethod
    def _sanitize_credential_helper(value: str) -> str | None:
        value = value.strip()
        if value in _KEYCHAIN_HELPERS:
            trusted_helper = platform_compat.trusted_git_helper_bin(f"git-credential-{value}")
            return f"!{shlex.quote(trusted_helper)}" if trusted_helper else None
        if not value.startswith("!"):
            return None
        try:
            argv = shlex.split(value[1:])
        except ValueError:
            return None
        if len(argv) != 3 or Path(argv[0]).name not in {"gh", "gh.exe"}:
            return None
        if argv[1:] != ["auth", "git-credential"]:
            return None
        trusted_gh = platform_compat.trusted_system_bin("gh")
        return f"!{shlex.quote(trusted_gh)} auth git-credential" if trusted_gh else None

    @classmethod
    def _credential_helper_env(cls) -> dict[str, str]:
        env = git_command_env()
        # ``git_command_env`` fails closed to the null device when an executable
        # pin cannot be resolved from OS system directories. Git for Windows
        # keeps these two helpers beside its trusted installation instead, so
        # replace only those existing pins through the dedicated fixed-root
        # resolver. Missing helpers keep the fail-closed null-device value.
        for index in range(int(env["GIT_CONFIG_COUNT"])):
            key = env.get(f"GIT_CONFIG_KEY_{index}", "").lower()
            helper_name = _ORIGIN_HELPER_CONFIG.get(key)
            if helper_name is None:
                continue
            helper = platform_compat.trusted_git_helper_bin(helper_name)
            if helper is not None:
                # Git treats the pack-program value as a shell command and
                # appends the repository path. Git for Windows installs these
                # helpers below ``Program Files``; an unquoted absolute path is
                # split at the space before the helper ever starts.
                env[f"GIT_CONFIG_VALUE_{index}"] = shlex.quote(helper)
        helpers: list[tuple[str, str]] = []
        for scope in ("--system", "--global"):
            cleanup: str | None = None
            try:
                argv, scrubbed, cleanup = sandboxed_spawn_argv(
                    [
                        cls._git_executable(),
                        "config",
                        scope,
                        "--get-regexp",
                        r"^credential(\..+)?\.helper$",
                    ],
                    mode="standard",
                    env=env,
                )
                result = run_limited(
                    argv,
                    env=scrubbed,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=_GIT_TIMEOUT_SECONDS,
                )
            finally:
                if cleanup:
                    Path(cleanup).unlink(missing_ok=True)
            if result.returncode != 0:
                continue
            for line in result.stdout.splitlines():
                key, separator, raw_value = line.partition(" ")
                helper = cls._sanitize_credential_helper(raw_value) if separator else None
                if helper is not None:
                    helpers.append((key, helper))
                if len(helpers) >= _MAX_CREDENTIAL_HELPERS:
                    break
            if len(helpers) >= _MAX_CREDENTIAL_HELPERS:
                break
        start = int(env["GIT_CONFIG_COUNT"])
        if platform_compat.IS_WINDOWS:
            # Managed state has several fixed directory layers before Git's
            # temporary pack names. Opt Git for Windows into its Unicode path
            # APIs so an ordinary per-user data root cannot hit MAX_PATH.
            env[f"GIT_CONFIG_KEY_{start}"] = "core.longpaths"
            env[f"GIT_CONFIG_VALUE_{start}"] = "true"
            start += 1
        for offset, (key, value) in enumerate(helpers):
            env[f"GIT_CONFIG_KEY_{start + offset}"] = key
            env[f"GIT_CONFIG_VALUE_{start + offset}"] = value
        env["GIT_CONFIG_COUNT"] = str(start + len(helpers))
        return env

    @staticmethod
    def _assert_safe_checkout(path: Path) -> None:
        reason = repo_exec_config_reason(str(path))
        if reason:
            raise ProjectGitError(f"Project repository is unsafe to synchronize: {reason}")

    def _assert_derived_path_unlinked(self, path: Path) -> None:
        """Reject links in an install-local Project path without resolving through them."""
        root = self.registry.projects_dir.absolute()
        candidate = path.absolute()
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise ProjectGitError("Project derived path escapes managed storage") from exc
        current = root
        if platform_compat.is_link_or_junction(current):
            raise ProjectGitError("Project derived path contains a link or junction")
        for part in relative.parts:
            current = current / part
            if platform_compat.is_link_or_junction(current):
                raise ProjectGitError("Project derived path contains a link or junction")

    @classmethod
    def _run_git(cls, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        cleanup: str | None = None
        try:
            argv, env, cleanup = sandboxed_spawn_argv(
                [cls._git_executable(), *args],
                mode="standard",
                env=cls._credential_helper_env(),
            )
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GIT_PROXY_COMMAND"] = "true"
            return run_limited(
                argv,
                cwd=cwd,
                env=env,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except SandboxUnavailableError as exc:
            raise ProjectGitError("Git project operation requires an available sandbox") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProjectGitError("Git project operation timed out") from exc
        except subprocess.CalledProcessError as exc:
            # Git commonly echoes a remote URL in stderr. That URL may carry an
            # embedded credential, so the CLI reports the failure without
            # replaying subprocess output.
            raise ProjectGitError("Git project operation failed") from exc
        finally:
            if cleanup:
                Path(cleanup).unlink(missing_ok=True)

    def add(
        self,
        remote: str,
        *,
        before_primary_change: (
            Callable[[RegisteredProject, ProjectRegistration], None] | None
        ) = None,
    ) -> RegisteredProject:
        """Clone a Git-backed bundle into managed storage and register it."""
        remote = self._validate_remote(remote)
        with self._lock("bundle-add"):
            managed_root = self.registry.projects_dir / "managed"
            self._assert_derived_path_unlinked(managed_root)
            managed_root.mkdir(parents=True, exist_ok=True)
            self._assert_derived_path_unlinked(managed_root)
            staging = Path(tempfile.mkdtemp(prefix="project-clone-", dir=managed_root))
            published = False
            try:
                self._assert_derived_path_unlinked(staging)
                self._run_git(managed_root, "clone", "--", remote, str(staging))
                self._assert_derived_path_unlinked(staging)
                self._assert_safe_checkout(staging)
                manifest = load_project_manifest(staging)
                target = managed_root / manifest.id / "bundle"
                self._assert_derived_path_unlinked(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                self._assert_derived_path_unlinked(target)
                if target.exists():
                    existing = load_project_manifest(target)
                    if existing.id != manifest.id:
                        raise ProjectGitError(f"managed Project path collision at {target}")
                    existing_remote = self._run_git(
                        target, "remote", "get-url", "origin"
                    ).stdout.strip()
                    if existing_remote != remote:
                        raise ProjectGitError(
                            "Project ID is already managed from a different remote"
                        )
                    registered = any(
                        project.id == manifest.id for project in self.registry.list_projects()
                    )
                    if not registered:
                        # Unregister preserves the managed clone so a registry write
                        # failure never leaves a registration pointing at missing
                        # source data. A later add already has a fresh, validated
                        # clone in staging, so replace the unregistered cache now
                        # rather than silently reusing stale upstream content.
                        if not platform_compat.rmtree_force(target):
                            raise ProjectGitError(
                                "stale managed Project clone could not be replaced"
                            )
                        self._assert_derived_path_unlinked(target)
                        os.replace(staging, target)
                        published = True
                else:
                    self._assert_derived_path_unlinked(target)
                    os.replace(staging, target)
                    published = True
                return self.registry.add_managed(
                    target,
                    remote=remote,
                    before_primary_change=before_primary_change,
                )
            except ProjectManifestError as exc:
                raise ProjectGitError(
                    f"Git repository is not a valid Project bundle: {exc}"
                ) from exc
            finally:
                if not published and staging.exists():
                    platform_compat.rmtree_force(staging)

    def materialize_source(
        self,
        project_id: str,
        source_id: str,
        remote: str,
        default_branch: str = "",
        *,
        base_dir: Path | None = None,
        on_stale: Callable[[str], None] | None = None,
    ) -> Path:
        """Clone one repo source or reuse its install-local derived checkout."""
        if not remote.strip():
            raise ProjectGitError(f"Project repo source {source_id} needs a URL")
        remote = self._validate_remote(remote, base_dir=base_dir)
        default_branch = self._validate_branch(default_branch)
        sources_root = self.registry.projects_dir / "state" / project_id / "sources"
        target = sources_root / source_id
        self._assert_derived_path_unlinked(target)
        sources_root.mkdir(parents=True, exist_ok=True)
        self._assert_derived_path_unlinked(target)
        with self._lock(f"{project_id}-{source_id}"):
            self._assert_derived_path_unlinked(target)
            if target.exists():
                if not (target / ".git").exists():
                    raise ProjectGitError(
                        f"Project repo source path is not a Git checkout: {source_id}"
                    )
                self._assert_safe_checkout(target)
                if on_stale is not None:
                    on_stale(source_id)
                return target

            staging = Path(tempfile.mkdtemp(prefix=f".{source_id}-", dir=sources_root))
            published = False
            try:
                branch_args = (
                    ("--branch", default_branch, "--single-branch") if default_branch else ()
                )
                self._run_git(
                    sources_root,
                    "clone",
                    *branch_args,
                    "--",
                    remote,
                    str(staging),
                )
                self._assert_safe_checkout(staging)
                if target.exists():
                    backup = Path(
                        tempfile.mkdtemp(prefix=f".{source_id}-replaced-", dir=sources_root)
                    )
                    backup.rmdir()
                    os.replace(target, backup)
                    try:
                        os.replace(staging, target)
                    except Exception:
                        os.replace(backup, target)
                        raise
                    if not platform_compat.rmtree_force(backup):
                        raise ProjectGitError(
                            f"replaced Project repo source could not be removed: {source_id}"
                        )
                else:
                    os.replace(staging, target)
                published = True
                return target
            finally:
                if not published and staging.exists():
                    platform_compat.rmtree_force(staging)

    def resolve_source(self, project_id: str, source_id: str) -> Path | None:
        """Return an existing derived checkout without cloning or fetching."""
        sources_root = self.registry.projects_dir / "state" / project_id / "sources"
        target = sources_root / source_id
        self._assert_derived_path_unlinked(target)
        with self._lock(f"{project_id}-{source_id}"):
            self._assert_derived_path_unlinked(target)
            if not target.exists():
                return None
            if not (target / ".git").exists():
                raise ProjectGitError(
                    f"Project repo source path is not a Git checkout: {source_id}"
                )
            self._assert_safe_checkout(target)
            return target

    def remove_derived_state(self, project_id: str) -> None:
        """Remove regenerable source checkouts before unregistering a Project."""
        with self._lock(f"{project_id}-storage"):
            state_root = self.registry.projects_dir / "state" / project_id
            self._assert_derived_path_unlinked(state_root)
            if state_root.exists() and not platform_compat.rmtree_force(state_root):
                raise ProjectGitError("Project derived state could not be removed")

    def sync(self, identifier: str) -> RegisteredProject:
        """Fetch and fast-forward a managed clone without committing or pushing."""
        project = self.registry.resolve(identifier)
        managed = [
            registration
            for registration in project.registrations
            if registration.origin == "managed_git"
        ]
        if not managed:
            raise ProjectGitError(f"Project {project.id} has no managed Git clone")
        registration = managed[-1]
        with self._lock(f"{project.id}-bundle"):
            self._assert_safe_checkout(registration.path)
            branch_result = self._run_git(
                registration.path, "symbolic-ref", "--quiet", "--short", "HEAD"
            )
            branch = branch_result.stdout.strip()
            if not branch:
                raise ProjectGitError("managed Project clone has a detached HEAD")
            branch = self._validate_branch(branch)
            self._run_git(registration.path, "fetch", "--", "origin", branch)
            remote_manifest_ref = f"origin/{branch}:project.yaml"
            remote_manifest_size_raw = self._run_git(
                registration.path, "cat-file", "-s", remote_manifest_ref
            ).stdout.strip()
            try:
                remote_manifest_size = int(remote_manifest_size_raw)
            except ValueError as exc:
                raise ProjectGitError("remote Project manifest size is invalid") from exc
            if remote_manifest_size > PROJECT_MANIFEST_MAX_BYTES:
                raise ProjectGitError("remote Project manifest is invalid: manifest is too large")
            try:
                remote_manifest = load_project_manifest_text(
                    self._run_git(
                        registration.path,
                        "show",
                        remote_manifest_ref,
                    ).stdout,
                    source=remote_manifest_ref,
                )
            except ProjectManifestError as exc:
                raise ProjectGitError(f"remote Project manifest is invalid: {exc}") from exc
            if remote_manifest.id != project.id:
                raise ProjectGitError("managed Project manifest identity changed on the remote")
            self._run_git(registration.path, "merge", "--ff-only", f"origin/{branch}")
            return self.registry.add_managed(registration.path, remote=registration.remote)
