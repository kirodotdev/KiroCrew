"""Safe Git primitives for the user-approved review-fix workflow.

Generic Task Runner Git behavior remains in :mod:`git_coord`. This module only
operates on an explicitly captured target and a retained candidate worktree.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from kiro_crew import git_coord, platform_compat
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.task_models import ReviewFixGitRecord, ReviewFixTargetMode, ReviewFixTargetSnapshot

logger = logging.getLogger(__name__)

# Line-anchored: a real conflict marker owns its whole line (`<<<<<<< branch`,
# a bare `=======` divider, `>>>>>>> branch`). A plain `in` substring check
# false-positives on a `=======` Markdown/RST heading underline or comment
# banner that a file may legitimately contain.
_CONFLICT_START_RE = re.compile(r"^<{7}( |$)", re.MULTILINE)
_CONFLICT_MID_RE = re.compile(r"^={7}$", re.MULTILINE)
_CONFLICT_END_RE = re.compile(r"^>{7}( |$)", re.MULTILINE)

# Hard parent-side capture caps. communicate() buffered the ENTIRE child
# output first, so a runaway candidate diff (an agent that edited a vendored
# tree or generated megabytes) OOM'd the gateway before any downstream limit.
# A patch or path list beyond the cap is REFUSED, never truncated: a truncated
# patch would fail `git apply` mid-apply and an incomplete path list would
# silently violate group ownership.
_PATCH_CAPTURE_CAP_BYTES = 8 * 1024 * 1024
_PATH_LIST_CAP_BYTES = 256 * 1024

# One `git hash-object` invocation per untracked file made fingerprinting an
# N-process operation on a working tree with many untracked files; batching
# keeps it O(N / chunk) while staying well under a shell/argv length limit.
_HASH_OBJECT_CHUNK = 200
# Also chunk by a char budget so a batch of short paths doesn't still blow
# past Windows CreateProcess's 32,767-char command-line limit.
_HASH_OBJECT_CHUNK_CHARS = 24_000


def _chunk_by_chars(items: list[str], max_items: int, max_chars: int) -> Iterable[list[str]]:
    """Yield batches capped by both item count and total path length."""
    batch: list[str] = []
    batch_chars = 0
    for item in items:
        item_chars = len(item) + 1
        if batch and (len(batch) >= max_items or batch_chars + item_chars > max_chars):
            yield batch
            batch = []
            batch_chars = 0
        batch.append(item)
        batch_chars += item_chars
    if batch:
        yield batch


def write_artifact_text(path: Path, text: str, *, newline: str | None = None) -> Path:
    """Replace *path* under a pinned parent, never via a link."""
    path = Path(path)
    parent_fd = platform_compat.pin_directory(path.parent)
    temporary = f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            if platform_compat.IS_POSIX:
                existing = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            else:
                existing = path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise FileExistsError("artifact symlink", str(path))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
        if platform_compat.IS_POSIX:
            fd = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        else:
            fd = os.open(path.parent / temporary, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as stream:
            stream.write(text)
        if platform_compat.IS_POSIX:
            os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        else:
            os.replace(path.parent / temporary, path)
        return path
    finally:
        try:
            if platform_compat.IS_POSIX:
                os.unlink(temporary, dir_fd=parent_fd)
            else:
                (path.parent / temporary).unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent_fd)


class ReviewFixGitError(RuntimeError):
    """A bounded, user-safe error from a review-fix Git operation."""


@dataclass(frozen=True)
class ReviewFixPatch:
    """A candidate patch and the task-owned paths it may change."""

    patch_id: str
    patch_text: str
    paths: tuple[str, ...]
    patch_path: str = ""


async def _git_text(work_dir: str | Path, *args: str) -> str:
    """Run Git through the existing sandboxed Task Runner chokepoint."""
    try:
        return await git_coord._git(str(work_dir), *args)
    except Exception as exc:
        message = str(exc)
        message = redact_credentials(message)[0]
        message = redact_exfiltration_urls(message)[0]
        raise ReviewFixGitError(message) from exc


async def _try_git_text(work_dir: str | Path, *args: str) -> str:
    try:
        return await _git_text(work_dir, *args)
    except ReviewFixGitError:
        return ""


async def _git_text_bounded(work_dir: str | Path, cap_bytes: int, *args: str) -> str:
    """Run Git with a bounded output cap and refuse truncation."""
    stdout, stderr, returncode, truncated = await git_coord._git_bounded(
        str(work_dir), cap_bytes, *args
    )
    if returncode != 0:
        message = stderr.strip() or f"git {' '.join(args)} failed"
        message = redact_credentials(message)[0]
        message = redact_exfiltration_urls(message)[0]
        raise ReviewFixGitError(message)
    if truncated:
        raise ReviewFixGitError(
            f"git {' '.join(args)} output exceeded the {cap_bytes}-byte capture "
            "cap; refusing a truncated result"
        )
    return stdout


def _clean_path_list(raw: Iterable[str]) -> tuple[str, ...]:
    paths: set[str] = set()
    for value in raw:
        path = str(value).replace("\\", "/")
        if not path or path.startswith(("/", "../")) or path == ".":
            raise ReviewFixGitError("invalid task-owned path")
        if "/../" in f"/{path}/" or path.startswith("-"):
            raise ReviewFixGitError("invalid task-owned path")
        paths.add(path)
    return tuple(sorted(paths))


def _pathspec(path: str) -> str:
    """Return a literal pathspec for a repo-relative path."""
    return f":(literal){path}"


def _status_paths(status: str) -> tuple[list[str], list[str]]:
    """Parse porcelain status into tracked and untracked paths."""
    tracked: set[str] = set()
    untracked: set[str] = set()
    for entry in status.split("\0"):
        if len(entry) < 4:
            continue
        code = entry[:2]
        path = entry[3:]
        if code == "??":
            untracked.add(path)
        else:
            tracked.add(path)
    return sorted(tracked), sorted(untracked)


async def _dirty_content_fingerprint(path: Path, status: str, untracked: list[str]) -> str:
    """Hash status and dirty content so content edits change the fingerprint."""
    digest = hashlib.sha256(status.encode("utf-8"))
    diff_text = await _git_text_bounded(
        path,
        _PATCH_CAPTURE_CAP_BYTES,
        "diff",
        "--binary",
        "-z",
        "--no-ext-diff",
        "--no-color",
        "HEAD",
    )
    digest.update(diff_text.encode("utf-8"))
    untracked_files = [item for item in untracked if not item.endswith("/")]
    for chunk in _chunk_by_chars(untracked_files, _HASH_OBJECT_CHUNK, _HASH_OBJECT_CHUNK_CHARS):
        output = await _git_text(path, "hash-object", "--", *chunk)
        blobs = output.splitlines()
        if len(blobs) != len(chunk):
            raise ReviewFixGitError("git hash-object returned an unexpected number of blob hashes")
        for relative, blob in zip(chunk, blobs):
            digest.update(relative.encode("utf-8", errors="replace"))
            digest.update(blob.strip().encode("utf-8"))
    return digest.hexdigest()


async def inspect_target(
    target_path: str | Path,
    *,
    mode: ReviewFixTargetMode = ReviewFixTargetMode.CURRENT_BRANCH,
) -> ReviewFixTargetSnapshot:
    """Capture repository identity, HEAD, dirty paths, and a stable fingerprint."""
    path = Path(target_path).expanduser().resolve()
    if is_sensitive_path(str(path)):
        raise ReviewFixGitError("review-fix target is a sensitive path")
    if not path.exists() or not path.is_dir():
        raise ReviewFixGitError("review-fix target is not a directory")
    repo_root = Path((await _git_text(path, "rev-parse", "--show-toplevel")).strip()).resolve()
    if is_sensitive_path(str(repo_root)):
        raise ReviewFixGitError("review-fix repository is a sensitive path")
    try:
        path.relative_to(repo_root)
    except ValueError as exc:
        raise ReviewFixGitError("review-fix target is outside its repository") from exc

    branch = (await _git_text(path, "rev-parse", "--abbrev-ref", "HEAD")).strip()
    head_sha = (await _git_text(path, "rev-parse", "HEAD")).strip()
    status = await _git_text(
        path, "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all"
    )
    tracked, untracked = _status_paths(status)
    fingerprint = await _dirty_content_fingerprint(path, status, untracked)
    upstream = (
        await _try_git_text(
            path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
        )
    ).strip()
    remote = upstream.split("/", 1)[0] if "/" in upstream else ""
    if not remote:
        remotes = (await _try_git_text(path, "remote")).splitlines()
        remote = "origin" if "origin" in remotes else (remotes[0].strip() if remotes else "")
    target_ref = branch if branch and branch != "HEAD" else head_sha
    return ReviewFixTargetSnapshot(
        mode=mode,
        repo_root=str(repo_root),
        target_path=str(path),
        target_ref=target_ref,
        branch_name=branch,
        head_sha=head_sha,
        dirty_fingerprint=fingerprint,
        tracked_paths=tracked,
        untracked_paths=untracked,
        upstream=upstream,
        remote=remote,
    )


def dirty_paths(snapshot: ReviewFixTargetSnapshot) -> set[str]:
    """Return the conservative path set that must not overlap silently."""
    return set(snapshot.tracked_paths) | set(snapshot.untracked_paths)


def dirty_overlap(snapshot: ReviewFixTargetSnapshot, paths: Iterable[str]) -> list[str]:
    """Return shared paths; separate hunks never bypass this safety check."""
    return sorted(dirty_paths(snapshot) & set(_clean_path_list(paths)))


def assert_target_unchanged(
    expected: ReviewFixTargetSnapshot,
    current: ReviewFixTargetSnapshot,
) -> None:
    """Raise when Apply is operating on something other than the captured target."""
    if (
        expected.repo_root != current.repo_root
        or expected.target_path != current.target_path
        or expected.branch_name != current.branch_name
        or expected.head_sha != current.head_sha
        or expected.dirty_fingerprint != current.dirty_fingerprint
    ):
        raise ReviewFixGitError("review-fix target changed since confirmation")


async def create_candidate(
    target: ReviewFixTargetSnapshot,
    candidate_path: str | Path,
    candidate_branch: str,
) -> ReviewFixGitRecord:
    """Create a candidate worktree from the captured target HEAD."""
    if not target.repo_root or not target.head_sha:
        raise ReviewFixGitError("review-fix target snapshot is incomplete")
    path = Path(candidate_path).expanduser().resolve()
    if path.exists():
        raise ReviewFixGitError("review-fix candidate path already exists")
    if is_sensitive_path(str(path)):
        raise ReviewFixGitError("review-fix candidate is a sensitive path")
    path.parent.mkdir(parents=True, exist_ok=True)
    await _git_text(target.repo_root, "check-ref-format", "--branch", candidate_branch)
    await _git_text(
        target.repo_root,
        "worktree",
        "add",
        "-b",
        candidate_branch,
        str(path),
        target.head_sha,
    )
    return ReviewFixGitRecord(
        candidate_worktree_path=str(path),
        candidate_branch=candidate_branch,
        candidate_ref=candidate_branch,
        remote=target.remote,
        upstream=target.upstream,
    )


async def discard_candidate(record: ReviewFixGitRecord, repo_root: str) -> None:
    """Remove only the retained candidate worktree."""
    if not record.candidate_worktree_path:
        return
    await _git_text(repo_root, "worktree", "remove", record.candidate_worktree_path, "--force")


async def candidate_patch(
    candidate_path: str | Path,
    base_sha: str,
    paths: Iterable[str] = (),
) -> ReviewFixPatch:
    """Export an exact binary-capable patch and its owned path set."""
    clean_paths = _clean_path_list(paths)
    diff_args = ["diff", "--binary", "--no-ext-diff", base_sha]
    if clean_paths:
        diff_args.extend(["--", *(_pathspec(item) for item in clean_paths)])
    # Bounded capture: a runaway candidate diff must fail the capture, never
    # buffer unbounded (OOM) nor yield a truncated patch that would break
    # `git apply` after the group pinned its patch_id.
    patch_text = await _git_text_bounded(candidate_path, _PATCH_CAPTURE_CAP_BYTES, *diff_args)
    # -z: NUL-delimited, unquoted paths -- same reasoning as `_status_paths`,
    # so a non-ASCII filename (Thai, accented) survives byte-exact instead of
    # arriving C-quoted and silently failing to match an owned path.
    name_args = ["diff", "--name-only", "--no-ext-diff", "-z", base_sha]
    if clean_paths:
        name_args.extend(["--", *(_pathspec(item) for item in clean_paths)])
    discovered = _clean_path_list(
        path
        for path in (
            await _git_text_bounded(candidate_path, _PATH_LIST_CAP_BYTES, *name_args)
        ).split("\0")
        if path
    )
    patch_id = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
    return ReviewFixPatch(patch_id=patch_id, patch_text=patch_text, paths=discovered)


async def write_patch(patch: ReviewFixPatch, patch_path: str | Path) -> ReviewFixPatch:
    """Persist a candidate patch outside the target worktree."""
    raw_path = Path(patch_path).expanduser()
    path = raw_path.parent.resolve() / raw_path.name
    if is_sensitive_path(str(path)):
        raise ReviewFixGitError("review-fix patch path is sensitive")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Off-loop: a candidate patch can be arbitrarily large, and this runs on the
    # gateway event loop.
    #
    # newline="\n" keeps git's diff bytes byte-exact: a text-mode write would
    # translate them to os.linesep (CRLF on Windows) and `git apply` would
    # reject the EOL-mangled patch against the repository's own contexts.
    await asyncio.to_thread(write_artifact_text, path, patch.patch_text, newline="\n")
    return ReviewFixPatch(patch.patch_id, patch.patch_text, patch.paths, str(path))


def _safe_patch_path(repo_root: Path, patch_path: str | Path) -> Path:
    path = Path(patch_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise ReviewFixGitError("review-fix patch is missing")
    if is_sensitive_path(str(path)):
        raise ReviewFixGitError("review-fix patch is a sensitive path")
    return path


async def apply_patch(
    target: ReviewFixTargetSnapshot,
    patch: ReviewFixPatch,
) -> list[str]:
    """Apply only the supplied patch; never reset or stash the target."""
    if not patch.patch_path:
        raise ReviewFixGitError("review-fix patch has not been written")
    repo_root = Path(target.repo_root).resolve()
    patch_path = _safe_patch_path(repo_root, patch.patch_path)
    if await asyncio.to_thread(patch_path.read_bytes) != patch.patch_text.encode():
        raise ReviewFixGitError("patch changed before apply")
    await _git_text(repo_root, "apply", "--check", "--3way", str(patch_path))
    # Failures after real apply begins must restore owned paths.
    try:
        await _git_text(repo_root, "apply", "--3way", "--whitespace=nowarn", str(patch_path))
        conflicts = (
            await _try_git_text(repo_root, "diff", "--name-only", "--diff-filter=U")
        ).splitlines()
        if conflicts:
            raise ReviewFixGitError("review-fix apply produced unresolved conflicts")
        touched = {
            line.replace("\\", "/")
            for line in (await _try_git_text(repo_root, "diff", "--name-only")).splitlines()
            if line
        }
        if touched and not touched.issubset(patch.paths):
            raise ReviewFixGitError("apply escaped owned paths")
        await asyncio.to_thread(_assert_no_conflict_markers, repo_root, patch.paths)
    except ReviewFixGitError:
        await _restore_owned_paths(repo_root, patch.paths)
        raise
    return list(patch.paths)


def _assert_no_conflict_markers(repo_root: Path, relatives: Iterable[str]) -> None:
    """Fail if owned paths contain conflict markers."""
    for relative in relatives:
        candidate = (repo_root / relative).resolve()
        try:
            candidate.relative_to(repo_root)
        except ValueError as exc:
            raise ReviewFixGitError("review-fix patch escaped repository root") from exc
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="replace")
            if (
                _CONFLICT_START_RE.search(text)
                and _CONFLICT_MID_RE.search(text)
                and _CONFLICT_END_RE.search(text)
            ):
                raise ReviewFixGitError("review-fix apply left conflict markers")


async def _restore_owned_paths(repo_root: Path, relatives: Iterable[str]) -> None:
    """Restore owned paths after a failed apply."""
    clean_paths = _clean_path_list(relatives)
    if not clean_paths:
        return
    pathspecs = [_pathspec(item) for item in clean_paths]
    try:
        at_head = {
            item
            for item in (
                await _git_text(
                    repo_root, "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", *pathspecs
                )
            ).split("\0")
            if item
        }
    except ReviewFixGitError as exc:
        raise ReviewFixGitError(f"review-fix restore failed: {exc}") from exc

    tracked_at_head = [item for item in clean_paths if item in at_head]
    new_paths = [item for item in clean_paths if item not in at_head]
    try:
        if tracked_at_head:
            await _git_text(
                repo_root,
                "checkout",
                "HEAD",
                "--",
                *(_pathspec(item) for item in tracked_at_head),
            )
        if new_paths:
            # `git apply --3way` stages a newly-created file, so unlinking
            # alone leaves its blob in the index -- `git status --porcelain`
            # would still report it as a staged addition. Unstage before
            # removing it from disk.
            await _git_text(
                repo_root,
                "rm",
                "--cached",
                "-q",
                "-f",
                "--ignore-unmatch",
                "--",
                *(_pathspec(item) for item in new_paths),
            )
    except ReviewFixGitError as exc:
        raise ReviewFixGitError(f"review-fix restore failed: {exc}") from exc
    resolved_repo_root = repo_root.resolve()
    for item in new_paths:
        candidate = repo_root / item
        # Check containment on the resolved PARENT, not the resolved
        # candidate itself: resolving the candidate follows a symlink, so a
        # new path that is itself a symlink pointing outside the repo would
        # pass containment and then unlink() would delete its target instead
        # of the link. Unlinking the unresolved candidate path removes only
        # the link/file entry itself.
        try:
            candidate.parent.resolve().relative_to(resolved_repo_root)
        except ValueError:
            continue
        candidate.unlink(missing_ok=True)


async def stage_paths(repo_root: str | Path, paths: Iterable[str]) -> list[str]:
    """Stage exactly the task-owned paths and return the staged scope."""
    clean_paths = _clean_path_list(paths)
    if not clean_paths:
        raise ReviewFixGitError("review-fix commit has no owned paths")
    await _git_text(repo_root, "add", "--", *(_pathspec(item) for item in clean_paths))
    # -z: same non-ASCII-safe, NUL-delimited parsing as `_status_paths`.
    staged = _clean_path_list(
        path
        for path in (await _git_text(repo_root, "diff", "--cached", "--name-only", "-z")).split(
            "\0"
        )
        if path
    )
    if not set(staged).issubset(set(clean_paths)):
        raise ReviewFixGitError("review-fix staging escaped task-owned paths")
    return list(staged)


async def commit_group(repo_root: str | Path, paths: Iterable[str], message: str) -> str:
    """Commit only task-owned paths after explicit user confirmation."""
    if not message.strip():
        raise ReviewFixGitError("review-fix commit message is empty")
    staged = await stage_paths(repo_root, paths)
    if not staged:
        raise ReviewFixGitError("review-fix commit has no changes")
    await _git_text(
        repo_root,
        "commit",
        "-m",
        message.strip(),
        "--only",
        "--",
        *(_pathspec(item) for item in staged),
    )
    return (await _git_text(repo_root, "rev-parse", "HEAD")).strip()


async def push_preview(repo_root: str | Path, remote: str, branch: str) -> dict[str, object]:
    """Describe a push without contacting the remote."""
    if not remote or not branch or branch == "HEAD":
        raise ReviewFixGitError("review-fix push target is incomplete")
    upstream = (
        await _try_git_text(
            repo_root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
        )
    ).strip()
    if upstream:
        base = upstream
    else:
        # No tracking ref (e.g. a candidate branch for a new-branch target).
        # An EMPTY preview here would invite approving a push that publishes
        # unseen commits and files, so derive a local basis instead — both
        # candidates are cached refs, preserving the no-network contract:
        # the target branch's last-fetched state first, then the remote's
        # cached HEAD. With neither there is no basis to preview against,
        # and the push is rejected rather than previewed as empty.
        base = (
            await _try_git_text(
                repo_root,
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/remotes/{remote}/{branch}",
            )
        ).strip()
        if not base:
            base = (
                await _try_git_text(
                    repo_root,
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"refs/remotes/{remote}/HEAD",
                )
            ).strip()
        if not base:
            raise ReviewFixGitError(
                "push preview has no local basis: the branch has no upstream "
                "and no cached remote ref to compare against"
            )
    commits = (await _try_git_text(repo_root, "log", "--oneline", f"{base}..HEAD")).splitlines()
    files = (await _try_git_text(repo_root, "diff", "--name-only", f"{base}..HEAD")).splitlines()
    return {
        "remote": remote,
        "branch": branch,
        "upstream": upstream,
        "preview_base": base,
        "commits": commits,
        "files": files,
        "diverged": bool(upstream and not commits and files),
    }


async def push(repo_root: str | Path, remote: str, branch: str) -> dict[str, object]:
    """Push once to the named remote/branch; force-push is unavailable."""
    if not remote or not branch or branch == "HEAD":
        raise ReviewFixGitError("review-fix push target is incomplete")
    await _git_text(repo_root, "push", remote, branch)
    return {"remote": remote, "branch": branch, "pushed": True}
