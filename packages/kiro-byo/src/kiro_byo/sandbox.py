"""Filesystem sandbox primitives for Kiro BYO native tools.

The sandbox is deliberately separate from user consent.  Crew may approve a
native tool call, but that approval never widens the filesystem roots exposed to
Kiro BYO.  The current policy supports a workspace anchor plus explicit allowed
roots; canonical path resolution and symlink/junction resolution are applied
before every filesystem operation.

Shell execution is a different security plane: on native Windows there is no
portable Python-only filesystem sandbox for an arbitrary PowerShell child.  The
shell tool therefore uses the same allowed-root policy for its *cwd* and always
returns to Crew's permission gate by default, but it must not be described as
OS-confined to those roots.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence


class SandboxError(RuntimeError):
    """A native tool attempted an operation outside its sandbox policy."""


DEFAULT_SENSITIVE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    ".git/config",
    ".git/credentials",
    ".git/hooks",
    ".git/hooks/**",
    "**/.ssh",
    "**/.ssh/**",
    "**/.aws",
    "**/.aws/**",
    "**/.azure",
    "**/.azure/**",
    "**/.gnupg",
    "**/.gnupg/**",
    "**/.netrc",
    "**/.npmrc",
    "**/.pypirc",
    "**/*credentials*.json",
    "**/*credential*.json",
    "**/*secrets*.json",
    "**/*secret*.json",
    "**/*tokens*.json",
    "**/*token*.json",
    "**/providers.json",
    "**/providers.json.bak",
    "**/providers.json.backup",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
)

SENSITIVE_ALLOW_EXCEPTIONS: frozenset[str] = frozenset(
    {
        ".env.example",
        ".env.sample",
        ".env.template",
        ".env.defaults",
        "providers.example.json",
    }
)

_WINDOWS_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\")
_WINDOWS_ADS_RE = re.compile(r"(^|[\\/])[^\\/:]+:[^\\/]+")


def _normcase_abs(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _within(root: Path, candidate: Path) -> bool:
    root_s = _normcase_abs(root)
    candidate_s = _normcase_abs(candidate)
    try:
        return os.path.commonpath((root_s, candidate_s)) == root_s
    except ValueError:
        return False


def _glob_match(path: str, pattern: str) -> bool:
    path = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    if path.startswith("./"):
        path = path[2:]
    if pattern.startswith("./"):
        pattern = pattern[2:]
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]):
        return True
    return False


def _dedupe_roots(roots: Sequence[Path]) -> tuple[Path, ...]:
    out: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = _normcase_abs(root)
        if key in seen:
            continue
        seen.add(key)
        out.append(root)
    # Longest first: relative displays and sensitive matching use the narrowest
    # applicable root when allowed roots overlap (workspace under Crew data).
    out.sort(key=lambda p: len(_normcase_abs(p)), reverse=True)
    return tuple(out)


@dataclass(frozen=True)
class SandboxPolicy:
    workspace_root: Path
    allowed_roots: tuple[Path, ...]
    read_only: bool = False
    deny_patterns: tuple[str, ...] = DEFAULT_SENSITIVE_PATTERNS
    policy_path: Path | None = None


class WorkspaceSandboxBackend:
    """Canonical path boundary shared by native filesystem tools."""

    def __init__(
        self,
        workspace_root: str | os.PathLike[str],
        *,
        allowed_roots: Iterable[str | os.PathLike[str]] = (),
        read_only: bool = False,
        deny_patterns: tuple[str, ...] = DEFAULT_SENSITIVE_PATTERNS,
        policy_path: Path | None = None,
    ) -> None:
        raw = os.fspath(workspace_root)
        if not str(raw).strip():
            raise SandboxError("workspace root is empty")
        root = Path(raw).expanduser()
        try:
            root = root.resolve(strict=True)
        except OSError as exc:
            raise SandboxError(f"workspace root is unavailable: {exc}") from exc
        if not root.is_dir():
            raise SandboxError(f"workspace root is not a directory: {root}")

        roots: list[Path] = [root]
        for value in allowed_roots:
            text = os.fspath(value)
            if not str(text).strip():
                continue
            candidate = Path(text).expanduser()
            try:
                candidate = candidate.resolve(strict=True)
            except OSError:
                # A typo in a policy root must not widen access or prevent the
                # whole backend from starting.  It simply is not an allowed root.
                continue
            if candidate.is_dir():
                roots.append(candidate)
        self.policy = SandboxPolicy(
            workspace_root=root,
            allowed_roots=_dedupe_roots(roots),
            read_only=bool(read_only),
            deny_patterns=deny_patterns,
            policy_path=policy_path,
        )

    @property
    def root(self) -> Path:
        return self.policy.workspace_root

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        return self.policy.allowed_roots

    @property
    def read_only(self) -> bool:
        return self.policy.read_only

    def scope_root_for_path(
        self,
        value: str | os.PathLike[str],
        *,
        for_write: bool = False,
    ) -> Path:
        """Return the narrow existing directory that can scope ``value``.

        This helper deliberately does *not* widen the sandbox.  It is used by
        the ACP layer to explain an out-of-scope request to KiroCrew and, only
        after the user approves it, to create a temporary/session grant.

        For a file target the containing directory is returned; for a directory
        target the directory itself is returned.  A not-yet-created write path
        is scoped to its nearest existing parent.  Sensitive paths remain a hard
        Kiro BYO boundary even when Crew grants ordinary directory access.
        """
        raw, candidate = self._candidate(value)
        try:
            resolved = candidate.resolve(strict=not for_write)
        except (OSError, RuntimeError) as exc:
            kind = "write" if for_write else "path"
            raise SandboxError(f"{kind} path cannot be resolved: {raw}: {exc}") from exc

        if self._is_sensitive(resolved):
            raise SandboxError(f"sensitive path is blocked by sandbox policy: {raw}")

        if resolved.exists() and resolved.is_dir():
            root = resolved
        else:
            root = resolved.parent
            while not root.exists() and root != root.parent:
                root = root.parent
            try:
                root = root.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise SandboxError(f"scope root cannot be resolved for {raw}: {exc}") from exc
        if not root.is_dir():
            raise SandboxError(f"scope root is not a directory for {raw}: {root}")
        if self._is_sensitive(root):
            raise SandboxError(f"sensitive path is blocked by sandbox policy: {raw}")
        return root

    def root_is_allowed(self, root: Path) -> bool:
        """Return whether ``root`` is already inside the active filesystem scope."""
        try:
            resolved = root.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        return self._containing_root(resolved) is not None

    def grant_root(self, root: str | os.PathLike[str]) -> tuple[Path, bool]:
        """Add one user-approved directory to this session's active roots.

        Returns ``(resolved_root, added)``.  ``added`` is false when the target
        was already covered by an existing root, which lets callers safely undo
        an ``allow_once`` grant without removing a pre-existing scope.
        """
        raw, candidate = self._candidate(root)
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SandboxError(f"approved root cannot be resolved: {raw}: {exc}") from exc
        if not resolved.is_dir():
            raise SandboxError(f"approved root is not a directory: {resolved}")
        if self._is_sensitive(resolved):
            raise SandboxError(f"sensitive path is blocked by sandbox policy: {raw}")
        if self._containing_root(resolved) is not None:
            return resolved, False
        self.policy = replace(
            self.policy,
            allowed_roots=_dedupe_roots((*self.policy.allowed_roots, resolved)),
        )
        return resolved, True

    def revoke_root(self, root: str | os.PathLike[str]) -> None:
        """Remove a root that was added by :meth:`grant_root` for allow-once."""
        try:
            resolved = Path(root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return
        if _normcase_abs(resolved) == _normcase_abs(self.root):
            return
        key = _normcase_abs(resolved)
        kept = tuple(r for r in self.policy.allowed_roots if _normcase_abs(r) != key)
        self.policy = replace(self.policy, allowed_roots=_dedupe_roots(kept))

    @property
    def platform_name(self) -> str:
        return "windows" if os.name == "nt" else "portable"

    @staticmethod
    def _reject_unsafe_windows_spelling(value: str) -> None:
        lowered = value.lower()
        if lowered.startswith(tuple(prefix.lower() for prefix in _WINDOWS_DEVICE_PREFIXES)):
            raise SandboxError("Windows device namespace paths are not allowed")
        if _WINDOWS_ADS_RE.search(value):
            raise SandboxError("NTFS alternate data streams are not allowed")
        if re.match(r"^[A-Za-z]:[^\\/]", value):
            raise SandboxError("drive-relative Windows paths are not allowed")

    def _containing_root(self, resolved: Path) -> Path | None:
        for root in self.allowed_roots:
            if _within(root, resolved):
                return root
        return None

    def _outside_message(self, raw: str, candidate: Path) -> str:
        roots = ", ".join(str(root) for root in self.allowed_roots)
        # Suggest a concrete existing directory rather than an unusable file path.
        suggestion = candidate if candidate.is_dir() else candidate.parent
        policy = str(self.policy.policy_path) if self.policy.policy_path else "Kiro BYO native policy"
        return (
            f"path escapes workspace root or other configured allowed roots: {raw}. "
            f"Allowed roots: {roots}. "
            f"If this access is intended, add an appropriate parent such as {suggestion!s} "
            f"to allowed_roots in {policy}."
        )

    def relative_display(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix() or "."
        except ValueError:
            containing = self._containing_root(path)
            if containing is None:
                return str(path)
            try:
                rel = path.relative_to(containing).as_posix() or "."
            except ValueError:
                return str(path)
            return f"{containing}{os.sep}{rel}" if rel != "." else str(containing)

    def _is_sensitive(self, resolved: Path) -> bool:
        name = resolved.name.lower()
        if name in SENSITIVE_ALLOW_EXCEPTIONS:
            return False
        root = self._containing_root(resolved)
        if root is None:
            # Out-of-scope paths can still be considered for an explicit Crew
            # directory grant.  Evaluate deny patterns against every suffix of
            # the canonical path instead of treating *all* external paths as
            # sensitive merely because they are not yet authorized.
            parts = [part.lower() for part in resolved.parts if part not in {resolved.anchor, "/", "\\"}]
            candidates = ["/".join(parts[i:]) for i in range(len(parts))]
            return any(
                _glob_match(candidate, pattern.lower())
                for candidate in candidates
                for pattern in self.policy.deny_patterns
            )
        try:
            rel = resolved.relative_to(root).as_posix().lower()
        except ValueError:
            return True
        return any(_glob_match(rel, pattern.lower()) for pattern in self.policy.deny_patterns)

    def _candidate(self, value: str | os.PathLike[str]) -> tuple[str, Path]:
        raw = os.fspath(value)
        if not isinstance(raw, str):
            raw = str(raw)
        if "\x00" in raw:
            raise SandboxError("NUL bytes are not allowed in paths")
        self._reject_unsafe_windows_spelling(raw)
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return raw, candidate

    def _check_resolved(self, raw: str, resolved: Path, *, allow_sensitive: bool) -> Path:
        if self._containing_root(resolved) is None:
            raise SandboxError(self._outside_message(raw, resolved))
        if not allow_sensitive and self._is_sensitive(resolved):
            raise SandboxError(f"sensitive path is blocked by sandbox policy: {raw}")
        return resolved

    def resolve_read_path(
        self,
        value: str | os.PathLike[str],
        *,
        must_exist: bool = True,
        allow_sensitive: bool = False,
    ) -> Path:
        raw, candidate = self._candidate(value)
        try:
            resolved = candidate.resolve(strict=must_exist)
        except (OSError, RuntimeError) as exc:
            raise SandboxError(f"path cannot be resolved: {raw}: {exc}") from exc
        return self._check_resolved(raw, resolved, allow_sensitive=allow_sensitive)

    def resolve_write_path(
        self,
        value: str | os.PathLike[str],
        *,
        allow_sensitive: bool = False,
    ) -> Path:
        if self.read_only:
            raise SandboxError("native policy is read-only; disable read_only to allow file writes")
        raw, candidate = self._candidate(value)
        try:
            # strict=False follows every existing symlink/junction in the parent
            # chain, then appends the non-existing tail.  The allowed-root check is
            # therefore performed on the physical destination rather than lexical '..'.
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise SandboxError(f"write path cannot be resolved: {raw}: {exc}") from exc
        self._check_resolved(raw, resolved, allow_sensitive=allow_sensitive)

        # Re-resolve the nearest existing parent strictly. This catches junction
        # redirection even when the final leaf does not exist yet.
        parent = resolved.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        try:
            physical_parent = parent.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SandboxError(f"write parent cannot be resolved: {raw}: {exc}") from exc
        if self._containing_root(physical_parent) is None:
            raise SandboxError(self._outside_message(raw, physical_parent))
        return resolved

    def resolve_cwd(self, value: str | os.PathLike[str] | None = None) -> Path:
        resolved = self.resolve_read_path(value or ".")
        if not resolved.is_dir():
            raise SandboxError(f"shell cwd is not a directory: {resolved}")
        return resolved

    def is_allowed_resolved(self, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        return self._containing_root(resolved) is not None and not self._is_sensitive(resolved)

    def iter_tree(self, base: Path) -> Iterable[Path]:
        base = self.resolve_read_path(base)
        if base.is_file():
            yield base
            return
        if not base.is_dir():
            return
        for current, dirnames, filenames in os.walk(base, followlinks=False):
            current_path = Path(current)
            kept_dirs: list[str] = []
            for dirname in dirnames:
                child = current_path / dirname
                try:
                    if child.is_symlink():
                        continue
                    resolved = child.resolve(strict=True)
                except OSError:
                    continue
                if self.is_allowed_resolved(resolved):
                    kept_dirs.append(dirname)
            dirnames[:] = kept_dirs
            for filename in filenames:
                child = current_path / filename
                if self.is_allowed_resolved(child):
                    yield child.resolve(strict=True)


class WindowsSandboxBackend(WorkspaceSandboxBackend):
    @property
    def platform_name(self) -> str:
        return "windows"


def create_sandbox(
    workspace_root: str | os.PathLike[str],
    *,
    allowed_roots: Iterable[str | os.PathLike[str]] = (),
    read_only: bool = False,
    deny_patterns: tuple[str, ...] = DEFAULT_SENSITIVE_PATTERNS,
    policy_path: Path | None = None,
) -> WorkspaceSandboxBackend:
    cls = WindowsSandboxBackend if os.name == "nt" else WorkspaceSandboxBackend
    return cls(
        workspace_root,
        allowed_roots=allowed_roots,
        read_only=read_only,
        deny_patterns=deny_patterns,
        policy_path=policy_path,
    )
