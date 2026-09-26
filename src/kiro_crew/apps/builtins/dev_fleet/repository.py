"""Authoritative repository, worktree, and dirty-state access for Dev Fleet."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import locale
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from kiro_crew.apps.builtins.dev_fleet import runtime
from kiro_crew.executors import subprocess_executor
from kiro_crew.sandbox import run_limited, sandboxed_spawn_argv
from kiro_crew.security import (
    is_unverifiable_path_refusal,
    path_contains_sensitive,
    sensitive_path_refusal,
)


def _resolve_primary_checkout(path: str) -> str:
    """Given any checkout, return the primary one, WITHOUT running git.

    A linked worktree's common dir is the primary's ``.git``, so the primary is that
    dir's parent. git would answer this with ``rev-parse --git-common-dir`` -- and
    parse the repository's config to do it, following any ``include.path`` it names
    before answering. That is the hazard, not an incidental cost, so the answer is
    read from git's own layout files instead: this asks nothing of the repository that
    the include scan does not already ask.

    Falls back to the path as named, which is the same answer a failed ask gave. Safe
    here and only here: the caller re-asks the fence about whatever this returns, so an
    unrewritten path is gated rather than trusted.
    """
    _gitdir, common_dir, _reason = _repo_metadata_dirs(path)
    if common_dir is not None and common_dir.name == ".git":
        return str(common_dir.parent)
    return path


def _fenced_reason(configured: str, resolved: str) -> str | None:
    """The central path gate's verdict on a checkout this app is about to adopt.

    BOTH spellings are asked, because they differ and the difference is the hole:
    ``_resolve_primary_checkout`` rewrites a linked worktree to its primary
    checkout, and a fenced worktree can have a primary that sits outside the
    fence. Gating only the rewritten form would clear a protected path by a path
    that is not it, and the fleet's own reads would go on to enumerate it.

    BOTH DIRECTIONS are asked too. ``sensitive_path_refusal`` answers "is this path
    INSIDE a protected location", which leaves the ancestor case open: a home
    directory that happens to carry a ``.git`` is not itself protected, yet it
    CONTAINS ``~/.aws`` and ``~/.ssh`` — and ``/api/disk`` walks every worktree root
    recursively, so adopting such a path reads the credential stores under it.
    ``path_contains_sensitive`` is the reverse gate the security module already
    publishes for exactly this shape.

    Blocking: it waits on the bounded path-resolution pool, so every caller runs
    it off the event loop.
    """
    for candidate in (configured, resolved):
        if not candidate:
            continue
        reason = sensitive_path_refusal(candidate)
        if reason:
            return reason
        if path_contains_sensitive(candidate):
            return f"{candidate} contains a protected location"
    return None


#: Filter drivers run a COMMAND on content. ``smudge`` and ``clean`` are the
#: checkout/check-in pair and ``process`` is the long-running form; any of the
#: three turns a content-touching read into repo-controlled code execution.
_FILTER_COMMAND_SUFFIXES = (".clean", ".smudge", ".process")
# What one config section's KEY NAMES may weigh before the reply is refused
# unparsed. These probes pass ``--name-only``, so no config VALUE is ever
# captured and the only thing left to grow is the number and length of keys --
# tens of bytes in a real repository, against a quarter megabyte here.
_PROBE_OUTPUT_MAX_BYTES = 256 * 1024
# What a repository-controlled METADATA file may weigh before it is refused unread:
# its `.git` pointer, `commondir`, and its own config. Read in this process, so the
# cap is what keeps an oversized config out of the gateway's memory. A real one is
# hundreds of bytes.
_METADATA_MAX_BYTES = 64 * 1024
# Both spellings git honours, matched as a config SECTION header because that is
# what a hand read of the file can see without parsing it: ``[include]`` and
# ``[includeIf "cond"]``. The question is only "does this repository reach outside
# itself", never "where to".
_INCLUDE_SECTION_RE = re.compile(r"^\[\s*include(if)?\b", re.IGNORECASE)


def _repo_config_env() -> dict[str, str]:
    """Environment for a repo-scoped ``git config`` read: safe keys and trusted PATH."""
    env = {k: v for k, v in os.environ.items() if runtime._is_safe_env_key(k)}
    env["PATH"] = runtime._TRUSTED_PATH
    return env


def _configured_filter_commands(path: str) -> tuple[list[str], str | None]:
    """Filter-driver command keys this repository's OWN config sets, and what went unread.

    ``git status`` compares the working tree against the index, so it runs any
    filter a ``.gitattributes`` entry binds to a path — and the driver's command
    comes from config the repository can write. The env neutralizers pin the
    named execution vectors (``core.fsmonitor``, ``core.hooksPath``,
    ``credential.helper``, ``core.sshCommand``) but cannot enumerate driver names,
    so a filter is the one that stays reachable. Reading config executes nothing,
    which is what makes asking first the whole remedy.

    ``--includes`` is mandatory. For a SPECIFIC scope git defaults include-following
    OFF, so ``[include] path = other.cfg`` holding ``filter.f.process`` answers an
    empty list while git still resolves — and runs — that driver on the next
    content-touching read. The repository proved this empirically in
    ``dashboard.handlers.worktree._checkout_filter``; the same flag is what makes
    this probe's answer mean what it says.

    Two answers, never one: the drivers found, and separately the reason a live
    scope could not be read. They are acted on differently — a driver is a
    measurement that refuses the repository for good, an unreadable scope is a
    measurement nobody took — so folding the second into the first would refuse a
    repository for configuring a driver that was never seen.

    Only the scopes the repository controls are read: a driver in the operator's
    own global config is the operator's decision, not a repo-supplied one.
    """
    git = runtime._trusted_bin("git")
    if git is None:
        return [], "git is unavailable, so its filter configuration is unverified"
    env = _repo_config_env()
    reads, reason = _repo_owned_config_reads(path)
    if reason:
        return [], reason
    by_name = {entry.name: entry for entry in reads}
    for entry in reads:
        if entry.problem:
            return [], f"its own git config at {entry.name} {entry.problem}"
        if entry.text is None:
            # Absent, or not a regular file. git creates ``config.worktree`` lazily, so
            # an absent one is the empty scope rather than a scope nobody read.
            continue
        if entry.name == "config.worktree":
            # git IGNORES this file unless the repository enables the extension, and the
            # flag lives in ``config`` -- whose bytes are already in hand. Asked so this
            # does not refuse a driver git itself would never resolve.
            shared = by_name.get("config")
            live = (
                _snapshot_bool(shared.text, git, env, "extensions.worktreeConfig")
                if shared is not None and shared.text is not None
                else False
            )
            if live is None:
                # Unread rather than resolved to "not live": dropping the scope on a
                # repository that HAS it admits a driver nobody looked for.
                return [], "whether its --worktree filter scope is live could not be read"
            if not live:
                continue
        # The FIRST measurement wins, in git's own precedence order: a driver found is a
        # measurement, and it outranks any unread verdict a later scope could produce.
        found, unread = _snapshot_filter_keys(entry, git, env)
        if found or unread:
            return found, unread
    return [], None


def _metadata_redirect_reason(target: Path) -> str | None:
    """Why a path git's own metadata NAMED must not be followed, if it must not.

    The ``.git`` pointer file and ``commondir`` are written by the repository, so
    they are attacker-controlled input, not a trusted layout: ``gitdir: /home/x/.aws``
    aims this module's own config read at a credential directory, and a symlinked
    ``.git`` does the same without naming anything. Neither is covered by the
    adoption fence, which judges the checkout path and the primary it resolves to --
    not a third path that checkout's metadata points at.

    So every named target is put through the SAME central gate the checkout itself
    passes, and a symlink is refused outright rather than resolved: following one
    means trusting its target, which is the decision being withheld.
    """
    try:
        if target.is_symlink():
            return f"its git metadata at {target.name} is a symlink"
    except OSError:
        return f"its git metadata at {target.name} could not be examined"
    fenced = _fenced_reason("", str(target))
    if fenced:
        return f"its git metadata names a protected location ({runtime._redact(fenced)})"
    return None


# Whether this platform can resolve a name against an open directory descriptor. Both
# calls are needed together: a stat that must fall back to a path while the read is
# pinned would judge one file and open another. POSIX has them; Windows has neither, and
# there the reads degrade to the ordered check -- the same bound as before, not a new
# hole, and the degradation is asserted rather than assumed.
_PINNED_READS = os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd


class _ConfigRead(NamedTuple):
    """One config file this repository owns, as READ -- never as a path to re-open.

    A path handed back to a caller is a path the caller opens by name a second time, and
    the repository owns those names. So the content travels instead, read while the
    directory holding it was pinned open.

    ``problem`` is set when the file exists but its content is not trustworthy evidence
    of anything -- unreadable, over the bound, or a symlink. Doubt is not safety, so a
    problem refuses adoption exactly as a found include does.
    """

    name: str
    text: str | None
    problem: str | None


def _open_pinned_dir(name: str | Path, *, dir_fd: int | None = None) -> int | None:
    """Open a DIRECTORY without following a link to it, as a pinned descriptor.

    This is what makes an ancestor swap unreachable. ``O_NOFOLLOW`` on a file open
    guards the FINAL component only, so checking that ``.git`` is a real directory and
    later opening ``.git/config`` by path leaves the window this module's whole design
    exists to close: the repository renames ``.git`` to a symlink in between, the open
    traverses it, and the final component is a genuine file at the attacker's target.
    A descriptor cannot be redirected that way -- it refers to the directory that was
    validated, whatever the name later points at.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        if dir_fd is not None and _PINNED_READS:
            return os.open(name, flags, dir_fd=dir_fd)
        return os.open(name, flags)
    except OSError:
        # ELOOP for a symlinked directory, ENOTDIR when it is not a directory at all,
        # ENOENT when absent. None for all three: the caller distinguishes by asking.
        return None


def _read_bounded_bytes(target: str | Path, *, dir_fd: int | None = None) -> bytes | None:
    """At most ``_METADATA_MAX_BYTES`` of a metadata file, or None when that is passed.

    Bounded because these files are repository-controlled and this read happens in the
    gateway's own process: an oversized ``.git/config`` would otherwise be held whole in
    memory before any check could look at it. One byte over the cap is enough to know,
    so the rest is never allocated.

    ``dir_fd`` is how the ancestor is pinned -- see :func:`_open_pinned_dir`. With it,
    ``target`` is a single NAME resolved against that descriptor, so no component of the
    path is re-walked by name. ``O_NOFOLLOW`` still guards the leaf.

    Returns BYTES: the two decoders below differ, and the choice belongs to the caller
    that knows whether it holds config text or a path.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        if dir_fd is not None and _PINNED_READS:
            handle = os.open(target, flags, dir_fd=dir_fd)
        else:
            handle = os.open(target, flags)
    except OSError:
        # ELOOP when the final component is a symlink, ENOENT when it vanished.
        return None
    try:
        with os.fdopen(handle, "rb") as stream:
            raw = stream.read(_METADATA_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _METADATA_MAX_BYTES:
        return None
    return raw


def _read_bounded(target: str | Path, *, dir_fd: int | None = None) -> str | None:
    """Bounded metadata decoded as config TEXT.

    ONE leading byte-order mark is removed, because git removes it too: a config opening
    ``\ufeff[include]`` is an include as far as git is concerned, and a scanner that sees
    the mark as part of the section name misses it.
    """
    raw = _read_bounded_bytes(target, dir_fd=dir_fd)
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace").lstrip("\ufeff")


def _read_bounded_path(target: str | Path, *, dir_fd: int | None = None) -> str | None:
    """A bounded metadata line decoded as a PATH, or None when it cannot be read.

    Separate from :func:`_read_bounded` because of the decoder, not the bound: a path
    byte that is not valid UTF-8 must survive as a surrogate that ``os.fsencode``
    restores exactly, and the ``errors="replace"`` decode a config SCAN wants would
    destroy it. The answer here reaches an ``lstat``.

    The mark is stripped here too, and leniently on purpose: a byte-order mark in front
    of ``gitdir:`` that this failed to skip would hide the gitdir, and a gitdir this
    cannot see is a config this cannot scan -- the unsafe direction.
    """
    raw = _read_bounded_bytes(target, dir_fd=dir_fd)
    if raw is None:
        return None
    return os.fsdecode(raw).lstrip("\ufeff")


def _common_dir_owns(common_fd: int | None, common_dir: Path, gitdir: Path) -> bool:
    """True when the pinned *common_fd* really is the repository *gitdir* belongs to.

    A linked worktree's gitdir sits at ``<common>/worktrees/<name>``, so the claim is
    checkable rather than taken on trust -- which matters because the common dir is
    reached by walking ``..`` through components the repository controls, and lexical
    normalisation answers differently from the kernel when one of them is a symlink.

    Compared by device and inode, and the candidate is reached through the pinned
    descriptor rather than by path, so the directory being vouched for is the one that
    was opened. ``lstat`` follows nothing.
    """
    claimed = _stat_metadata(
        os.path.join("worktrees", gitdir.name), dir_fd=common_fd, dir_path=common_dir
    )
    if claimed is None:
        return False
    try:
        return os.path.samestat(claimed, os.lstat(gitdir))
    except OSError:
        return False


# The metadata files a repository owns, read under the pinned gitdir.
_GITDIR_CONFIGS = ("config", "config.worktree")


def _repo_metadata(
    path: str,
) -> tuple[Path | None, Path | None, list[_ConfigRead], str | None]:
    """``(gitdir, common_dir, config_reads, refusal)`` for a checkout, WITHOUT git.

    git parses a repository's config on every command and follows ``include.path`` while
    doing so, so asking git where its own directories are is itself the hazard this
    module exists to bound. Its layout answers that question in files instead: ``.git``
    is either the directory or a one-line pointer to it, and ``commondir`` names the
    shared directory a linked worktree's own gitdir points back to.

    Every read happens under a descriptor opened no-follow on the directory holding it,
    and the descriptors are closed before returning, so no caller is handed a path to
    re-open. ``gitdir`` is per WORKTREE and ``common_dir`` is shared: a linked worktree's
    ``config.worktree`` lives under its own gitdir, while its primary checkout is the
    common dir's parent.

    All of these may be empty: a path that is not a checkout has no metadata of its own,
    which is not a refusal -- the callers report "not a git checkout" for it.
    """
    dot = Path(path) / ".git"
    reason = _metadata_redirect_reason(dot)
    if reason:
        return None, None, [], reason
    reads: list[_ConfigRead] = []
    gitdir_fd: int | None = None
    try:
        # The BRANCH is chosen by asking what `.git` is; the no-follow open below is what
        # enforces that the answer cannot be redirected. `is_dir` follows a link, and a
        # symlinked `.git` present at this point was refused above -- one appearing after
        # it is caught by the pinned open, which is the race this ordering serves.
        if dot.is_dir():
            gitdir = dot
            if _PINNED_READS:
                gitdir_fd = _open_pinned_dir(dot)
                if gitdir_fd is None:
                    # ELOOP: the name became a symlink between the check and here.
                    return None, None, [], "its git directory could not be opened"
        else:
            # A linked worktree names its gitdir in `.git` as a file, and `.git` is the
            # FINAL component of that read, so a no-follow open of it is already pinned:
            # there is no ancestor inside the repository to swap.
            if not dot.is_file():
                return None, None, [], None
            pointer = _read_bounded_path(dot)
            if pointer is None:
                return None, None, [], "its .git pointer file could not be read"
            named = next(
                (
                    line.split(":", 1)[1].strip()
                    for line in pointer.splitlines()
                    if line.lower().startswith("gitdir:")
                ),
                "",
            )
            if not named:
                return None, None, [], None
            gitdir = Path(named) if os.path.isabs(named) else Path(path) / named
            reason = _metadata_redirect_reason(gitdir)
            if reason:
                return None, None, [], reason
            if _PINNED_READS:
                gitdir_fd = _open_pinned_dir(gitdir)
                if gitdir_fd is None:
                    return None, None, [], "its git directory could not be opened"

        for name in _GITDIR_CONFIGS:
            reads.append(_config_read_at(gitdir_fd, gitdir, name))

        common_dir = gitdir
        # `commondir` is judged ITSELF, not merely the path it names. A failed read
        # would be indistinguishable from an absent file, and "absent" admits the
        # repository -- so the entry is classified under the pinned descriptor first.
        named = ""
        info = _stat_metadata("commondir", dir_fd=gitdir_fd, dir_path=gitdir)
        if info is not None:
            if stat.S_ISLNK(info.st_mode):
                return None, None, [], "its git metadata at commondir is a symlink"
            if stat.S_ISREG(info.st_mode):
                line = _read_bounded_path(
                    (
                        "commondir"
                        if gitdir_fd is not None and _PINNED_READS
                        else gitdir / "commondir"
                    ),
                    dir_fd=gitdir_fd,
                )
                if line is None:
                    return None, None, [], "its git metadata at commondir could not be read"
                named = line.strip()
        if named:
            shared = Path(named) if os.path.isabs(named) else gitdir / named
            # Normalised LEXICALLY, never resolved: `commondir` is relative (`../..`),
            # and `Path.resolve` follows symlinks -- the one thing this refuses to do.
            shared = Path(os.path.normpath(shared))
            reason = _metadata_redirect_reason(shared)
            if reason:
                return None, None, [], reason
            common_fd = _open_pinned_dir(shared) if _PINNED_READS else None
            if _PINNED_READS and common_fd is None:
                return None, None, [], "its shared git directory could not be opened"
            try:
                # Lexical normalisation and the kernel disagree when a component of the
                # walked-up path is a symlink, and those components live inside the
                # repository's own `.git`. So the pinned common dir must OWN this
                # gitdir: its `worktrees/<name>` entry has to be this very directory.
                if not _common_dir_owns(common_fd, shared, gitdir):
                    return (
                        None,
                        None,
                        [],
                        "its git metadata names a common directory that does not own "
                        "this worktree",
                    )
                common_dir = shared
                reads.append(_config_read_at(common_fd, shared, "config"))
            finally:
                if common_fd is not None:
                    os.close(common_fd)
    except OSError:
        return None, None, [], "its git metadata could not be read"
    finally:
        if gitdir_fd is not None:
            os.close(gitdir_fd)
    return gitdir, common_dir, reads, None


def _stat_metadata(name: str, *, dir_fd: int | None, dir_path: Path) -> os.stat_result | None:
    """``lstat`` a metadata entry, under the pinned descriptor where the platform has one.

    Never follows a link: the caller decides what a link means, and for every caller here
    it means refusal. Returns None when the entry is absent.
    """
    try:
        if dir_fd is not None and _PINNED_READS:
            return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return os.lstat(dir_path / name)
    except OSError:
        return None


def _config_read_at(dir_fd: int | None, dir_path: Path, name: str) -> _ConfigRead:
    """Read one config file under a pinned directory, classifying what came back."""
    info = _stat_metadata(name, dir_fd=dir_fd, dir_path=dir_path)
    if info is None:
        return _ConfigRead(name, None, None)  # absent: nothing to judge
    if stat.S_ISLNK(info.st_mode):
        return _ConfigRead(name, None, "is a symlink")
    if not stat.S_ISREG(info.st_mode):
        return _ConfigRead(name, None, None)
    text = _read_bounded(
        name if dir_fd is not None and _PINNED_READS else dir_path / name, dir_fd=dir_fd
    )
    if text is None:
        return _ConfigRead(name, None, "could not be read within the bound")
    return _ConfigRead(name, text, None)


def _repo_metadata_dirs(path: str) -> tuple[Path | None, Path | None, str | None]:
    """The layout answer alone, for the two callers that need a directory and no read."""
    gitdir, common_dir, _reads, reason = _repo_metadata(path)
    return gitdir, common_dir, reason


def _repo_owned_config_reads(path: str) -> tuple[list[_ConfigRead], str | None]:
    """The content of the config files THIS repository owns, read under pinned dirs."""
    _gitdir, _common, reads, reason = _repo_metadata(path)
    return reads, reason


def _names_an_include(text: str) -> bool:
    """True when this config text names an include, by SECTION.

    One predicate, shared by the adoption scan and the snapshot guard, because the two
    asking differently is the failure mode: the section regex is start-anchored, so a
    whole-text ``search`` answers False for an ``[include]`` on any line but the first,
    and a guard that disagrees with the scan lets exactly what the scan refused reach a
    spawn. Comments are stripped first -- git honours neither ``#`` nor ``;`` lines.
    """
    for line in text.splitlines():
        bare = line.split("#", 1)[0].split(";", 1)[0].strip()
        if _INCLUDE_SECTION_RE.match(bare):
            return True
    return False


def _include_refusal(path: str) -> str | None:
    """Why this repository's own config makes it unsafe for git to touch, if it does.

    git follows ``include.path`` while parsing config on every command, so a foreign
    repository naming one makes the FIRST git command open that file -- a credential
    store included. The sandbox is not the bound: this app's backend is itself spawned
    at the standard tier and a nested wrap is impossible by design, so a stricter tier
    contributes its env scrub while the file-level hides stay outside, where the
    credential homes are visible. The bound is therefore structural, and it has to be
    taken BEFORE any spawn, which is why it reads files instead of asking git.

    Detected by SECTION, not by resolved target: ``[include]`` and ``[includeIf ...]``
    are the only two spellings git honours, an include may name a file that includes a
    third, and following the chain to judge each hop would reimplement the very
    resolution this avoids triggering. A file that exists and cannot be read within the
    bound is refused, because doubt about its content is not evidence of safety.
    """
    reads, reason = _repo_owned_config_reads(path)
    if reason:
        return reason
    for entry in reads:
        if entry.problem:
            return f"its own git config at {entry.name} {entry.problem}"
        if entry.text is None:
            continue  # absent, or not a regular file: nothing to judge
        if _names_an_include(entry.text):
            return (
                "its own git config includes another file, so git would open that "
                "file before answering any question about this repository"
            )
    return None


@contextlib.contextmanager
def _config_snapshot(text: str):
    """Write vetted config bytes to a private file and yield its path.

    The point is not caching: it is that git is asked about bytes this module ALREADY
    read and approved, so the include scan and the question git answers cannot disagree.
    Probing the live repository re-opens a file the repository may have replaced in
    between, and that window is what this closes -- the check and the use become one
    read of one set of bytes.

    Written 0600 in a private directory and removed on the way out, so the snapshot is
    not itself something another process can swap.

    No snapshot may NAME an include, and that is enforced here rather than at the
    callers: :func:`_probe_git_snapshot` points git's repository discovery at this
    directory, where the file is read as a repository's own config -- and at repository
    scope git follows ``include.path`` by DEFAULT, with no flag asked for. So an
    include-naming snapshot would reach through the very isolation that stops git
    touching the checkout. One guard at the point the file is created covers every probe
    shape, including one added later, and does not depend on which caller checked first.

    Raises:
        ProbeIncludes: the bytes name an include, so no snapshot was written.
    """
    if _names_an_include(text):
        raise ProbeIncludes("the config scope names an include")
    directory = tempfile.mkdtemp(prefix="dev-fleet-cfg-")
    target = os.path.join(directory, "config")
    try:
        handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        yield target
    finally:
        with contextlib.suppress(OSError):
            os.unlink(target)
        with contextlib.suppress(OSError):
            os.rmdir(directory)


def _snapshot_filter_keys(
    entry: _ConfigRead, git: str, env: dict[str, str]
) -> tuple[list[str], str | None]:
    """Driver keys one config scope sets, read from a snapshot of that scope.

    ``--includes`` is kept even though the include scan already refuses a config that
    names one: for a SPECIFIC scope git defaults include-following OFF, so the flag is
    what makes this answer mean what git will DO, and a probe whose answer means less
    than git's behaviour is the bug an earlier round found empirically. The snapshot
    holds no include -- the scan refused it otherwise, and the guard below says so
    locally -- so the flag has nothing to follow and keeping it costs nothing.
    """
    text = entry.text or ""
    if _names_an_include(text):
        # Unreachable through the scan above. Kept because this function's safety rests
        # on the snapshot having nothing to follow, which is cheap to assert here and
        # invisible if inferred from a caller two levels up.
        return [], f"its own git config at {entry.name} includes another file"
    try:
        with _config_snapshot(text) as snapshot:
            out = _probe_git_snapshot(
                [
                    git,
                    "config",
                    "--file",
                    snapshot,
                    "--includes",
                    "--name-only",
                    "--get-regexp",
                    r"^filter\.",
                ],
                env,
                snapshot=snapshot,
                capture_output=True,
                text=True,
                encoding=locale.getpreferredencoding(False),
                timeout=5,
            )
    except ProbeUnread:
        # Not a clean reading: either the question was never asked, or the reply was too
        # large to retain. Both route to the same unread answer, so the checkout is
        # declined rather than served as driver-free.
        return [], f"its {entry.name} filter configuration could not be read safely"
    except (OSError, subprocess.SubprocessError):
        return [], f"its {entry.name} filter configuration could not be read"
    # 1 is "no match" and is the ordinary answer. A snapshot cannot be an absent file,
    # so any other code is an answer nobody read.
    if out.returncode not in (0, 1):
        return [], f"its {entry.name} filter configuration could not be read"
    # Only the keys that name a PROGRAM. The probe asks for the whole ``filter.``
    # section because git has no regexp for "the command keys of any driver name", and
    # the section also holds ``filter.<name>.required`` -- a boolean saying git should
    # fail when the filter fails, which names nothing executable. Returning it refused a
    # repository that configures a filter perfectly safely, and the refusal it produced
    # said the repository "configures executable git filter drivers", which of that key
    # was untrue.
    #
    # git lowercases the section and the final key while preserving the driver NAME's
    # case, so the comparison is on a lowercased copy and the reported key keeps the
    # spelling the operator would find in their config.
    return [
        key
        for key in (line.strip() for line in out.stdout.splitlines())
        if key and key.lower().endswith(_FILTER_COMMAND_SUFFIXES)
    ], None


def _snapshot_bool(text: str, git: str, env: dict[str, str], key: str) -> bool | None:
    """One boolean a config sets, read from a snapshot. None when it went unread."""
    try:
        with _config_snapshot(text) as snapshot:
            out = _probe_git_snapshot(
                [git, "config", "--file", snapshot, "--bool", "--get", key],
                env,
                snapshot=snapshot,
                capture_output=True,
                text=True,
                encoding=locale.getpreferredencoding(False),
                timeout=5,
            )
    except (ProbeUnread, OSError, subprocess.SubprocessError):
        return None
    if out.returncode == 1:
        return False  # unset is the ordinary answer
    if out.returncode != 0:
        return None
    return out.stdout.strip() == "true"


class ProbeUnread(RuntimeError):
    """A probe produced no reading this app may act on. Base of the ways that happens.

    Every probe site catches THIS rather than the subclasses, for the reason
    :class:`RepoUnavailable` exists: a new way for a probe to go unread must not
    slip past a handler that enumerated only the reasons alive when it was
    written, and the fail-safe answer is identical for all of them -- decline to
    serve the checkout, never serve it as driver-free.
    """


class ProbeUnsandboxed(ProbeUnread):
    """No sandbox could be built for a probe, so the probe was not run.

    Raised INSTEAD of running git, and every caller maps it to its own "went
    unread" answer. It must never be mapped to a clean reading: a question that
    was not asked has not been answered ``no``.
    """


class ProbeIncludes(ProbeUnread):
    """The checkout's own config names an include, so no probe was run in it.

    Raised INSTEAD of spawning, like its unsandboxed sibling and for a sharper reason:
    the spawn itself is the hazard here. git follows ``include.path`` while parsing
    config on EVERY command, so the first probe would open whatever the include names
    before answering anything -- which is why the question is settled by reading the
    repository's own config files rather than by asking git.
    """


class ProbeOversized(ProbeUnread):
    """A probe's output passed the bound, so it was discarded unparsed.

    Unlike its sibling, git DID run -- the reading is refused on size alone. The
    output of these probes is key NAMES of one config section, which is tens of
    bytes in every real repository, so a reply that passes the bound is a foreign
    repository answering a small question with an unbounded one. The bytes are
    dropped without being decoded or split, because the point is to not retain
    them.
    """


def _probe_git(
    argv: list[str], env: dict[str, str], *, path: str, **kwargs
) -> subprocess.CompletedProcess:
    """Run one read-only git probe against a checkout this app does not own.

    ``path`` is the checkout being read. Required rather than recovered from ``argv``,
    because the include refusal below has to be taken BEFORE the spawn and a
    chokepoint that guessed its subject from a flag would be one refactor away from
    guessing wrong.

    Routed through the ``sandboxed_spawn_argv`` chokepoint in ``strict`` mode, the same
    one :func:`kiro_crew.dashboard.handlers.worktree._run_git` uses. The tier is NOT
    the bound and must not be read as one: git parses the target repository's config on
    EVERY command and follows ``include.path`` while doing so -- ``git config
    --includes`` only decides whether an include is followed for the value being
    PRINTED, not whether the file is opened -- so a foreign repository can name any
    path there, a credential store included, and the fence cannot help because it gates
    the checkout path rather than the files that path's config points at. That is what
    :func:`_include_refusal` closes, and it reads the repository's own config files
    instead of asking git, because asking would already have opened them.

    Raises:
        ProbeIncludes: the checkout's own config names an include, so nothing ran.
        ProbeUnsandboxed: the sandbox could not be built, so nothing ran.
        ProbeOversized: the reply passed ``_PROBE_OUTPUT_MAX_BYTES`` and was dropped.
    """
    reason = _include_refusal(path)
    if reason is not None:
        raise ProbeIncludes(reason)
    return _spawn_bounded_probe(argv, env, **kwargs)


def _probe_git_snapshot(
    argv: list[str], env: dict[str, str], *, snapshot: str, **kwargs
) -> subprocess.CompletedProcess:
    """Run one git probe against a SNAPSHOT this app wrote, never a live checkout.

    A separate entry point rather than a flag on :func:`_probe_git`, because what it
    skips is that function's whole safety argument: there is no repository here to
    refuse, so the include gate has no subject. A boolean would put losing that gate one
    typo away; this cannot be reached without naming a snapshot, and it checks that the
    argv it was handed reads THAT file and does not enter a repository.
    """
    if "-C" in argv:
        raise AssertionError("a snapshot probe must not enter a repository")
    if "--file" not in argv or snapshot not in argv:
        raise AssertionError("a snapshot probe must read the snapshot it was given")
    return _spawn_bounded_probe(argv, env, **kwargs)


def _spawn_bounded_probe(
    argv: list[str], env: dict[str, str], **kwargs
) -> subprocess.CompletedProcess:
    """The sandboxed, output-bounded spawn both probe entry points share."""
    text = kwargs.pop("text", False)
    encoding = kwargs.pop("encoding", None)
    kwargs.pop("capture_output", None)
    try:
        wrapped, scrubbed, cleanup = sandboxed_spawn_argv(argv, mode="strict", env=env)
    except RuntimeError as exc:  # no backend and no explicit opt-in
        raise ProbeUnsandboxed(str(exc)) from exc
    try:
        # Captured through a file rather than a pipe, so the bound is checked
        # BEFORE the bytes enter this process. ``capture_output`` reads a pipe to
        # EOF into the parent's memory, and the ``tool`` rlimit profile bounds the
        # child, not what the parent accumulates from it -- so a foreign config
        # answering with megabytes of key names would be held whole in the backend
        # before any length check could run. stderr is discarded: no probe site
        # reads it, and keeping it would leave a second unbounded capture.
        with tempfile.TemporaryFile() as sink:
            done = run_limited(
                wrapped, env=scrubbed, stdout=sink, stderr=subprocess.DEVNULL, **kwargs
            )
            size = sink.seek(0, os.SEEK_END)
            if size > _PROBE_OUTPUT_MAX_BYTES:
                raise ProbeOversized(f"{size} bytes of config output")
            sink.seek(0)
            captured = sink.read()
    finally:
        if cleanup:
            with contextlib.suppress(OSError):
                os.unlink(cleanup)
    return subprocess.CompletedProcess(
        argv,
        done.returncode,
        captured.decode(encoding or "utf-8", "replace") if text else captured,
        "" if text else b"",
    )


def _audit_security_refusal(kind: str, path: str, detail: str) -> None:
    """Record a PERMANENT security refusal of a checkout as a denied SEL event.

    The two refusals that reach here are permission decisions, not errors: a
    protected location, and a repository that would execute its own code on a read.
    Each is taken during discovery and surfaced only as banner text, so without an
    event the decision leaves no record — while the sibling denials this app takes
    at its HTTP boundaries are audited. The unmeasured verdicts are deliberately NOT
    audited: an unresolved path gate and an unread config scope decided nothing, and
    recording them as denials would put refusals nobody took into the log.

    Wrapped, because auditing may not mask the refusal it describes.
    """
    try:
        runtime._sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name=f"dev-fleet:repo-{kind}",
            tool_kind="dev_fleet",
            outcome="denied",
            resources=f"repo discovery {path}",
            error=runtime._redact(detail),
        )
    except Exception:  # noqa: BLE001 — auditing must never mask the refusal
        runtime.logger.warning("dev-fleet: SEL emit failed for a discovery refusal")


class RepoUnavailable(RuntimeError):
    """No usable main checkout. Base of the two ways that happens.

    Sites that deliberately degrade rather than fail catch THIS, so a new reason
    for "there is no fleet to act on" cannot slip past a handler that enumerated
    only the reasons that existed when it was written.
    """


class RepoNotConfigured(RepoUnavailable):
    """No Kiro Crew checkout could be found, so there is no fleet to manage.

    Distinct from a discovery FAILURE, where a checkout was named and git could
    not read it: nothing is broken here, the app simply has no checkout to point
    at. Callers render a setup state asking where the checkout is, rather than an
    error blaming a path.
    """


class RepoUnreadable(RepoUnavailable):
    """A checkout was named but is not one this app can manage.

    Either git cannot enumerate its worktrees, or the path is a readable
    directory that does not carry the Kiro Crew markers. Carries the same
    consequence as RepoNotConfigured for every route except ``/fleet``: the fleet
    is unknown, so no action that needs a worktree can run. Typed separately so
    the two states can be told apart — this one names the path and asks the user
    to fix it, that one asks where the checkout is.
    """


class RepoReadOnly(RepoUnavailable):
    """The checkout is readable, but this app may only READ it.

    An operator-named git repository that does not carry the Kiro Crew markers is
    adopted for the generic read surface — worktree list, branch, ahead and
    own-commit counts, the dirty split, PR state, disk usage — all of which is
    plain git and gh work that holds for any repository. The mutating half does
    not generalize: ``worktree remove``, ``update-ref -d``, ``pull --ff-only``,
    ``git fetch``, the ``pip install -e .`` provision chain and the live-target
    service unit all act on this product's own source, so they are refused here.

    Shares a base with the two unavailable states so every site that already
    degrades on ``RepoUnavailable`` degrades on this too: a helper that answers
    "not derivable" for an unresolved checkout gives the same answer for one it
    may not write, which is correct without that site being touched.
    """


#: Set at startup when the resolved checkout does not carry the Kiro Crew markers,
#: to the message ``_repo()`` raises. Tiers 1-2 (env var, config) are taken
#: verbatim, so a configured path can be a readable directory that is not this
#: project; the message is composed on the executor at startup because it embeds
#: the config-derived source hint.
_REPO_INVALID_MSG: str | None = None

#: Set at startup when the resolved checkout is a git repository that does not
#: carry the Kiro Crew markers, to the message ``_repo()`` raises. Truthy means
#: the fleet is READABLE and every mutating verb is refused; ``_REPO_INVALID_MSG``
#: is then None, because there is nothing wrong with the path.
_REPO_READ_ONLY_MSG: str | None = None


def _repo_read() -> str:
    """The resolved main checkout path, safe to READ.

    The single gate between ``MAIN_REPO`` and every git argv or path built from
    it. ``git -C ""`` does not fail — it silently runs against this process's
    working directory — and ``Path("")`` is ``Path(".")``, so an unresolved
    checkout reaching a consumer would operate on an arbitrary directory and
    return plausible results. Raising here makes that state fail loud at every
    call site — including the ones that never touch a route, like the background
    refresher and sync — instead of each site carrying (or forgetting) its own
    guard. Sites that deliberately degrade catch ``RepoUnavailable`` and say what
    the degraded answer is.

    Read-only means read-only in the git sense: listing worktrees, resolving
    remotes and reading refs. It is what the generic surface needs and it is the
    weaker of the two accessors, so a call site that mutates must NOT use it —
    ``_repo()`` is the one that also refuses a repository this app may only read.
    """
    if not MAIN_REPO:
        raise RepoNotConfigured("no Kiro Crew checkout found to manage")
    if _REPO_INVALID_MSG:
        raise RepoUnreadable(_REPO_INVALID_MSG)
    return MAIN_REPO


def _repo() -> str:
    """The resolved main checkout path, safe to MUTATE. The default accessor.

    Everything ``_repo_read`` refuses, plus a repository this app may only read.
    A configured path that is a readable but unrelated git repository is a hazard
    wearing a valid-looking path: git answers happily, so nothing downstream can
    tell that ``worktree remove``, ``update-ref -d``, ``pull --ff-only`` and
    ``pip install -e .`` are landing in a stranger's tree. Refusing in the
    accessor rather than per verb is what makes that fail CLOSED: a call site
    added later inherits the refusal by default, and only a site that has
    reasoned about a foreign repository opts down to ``_repo_read``.

    Verb-rooted safety is not enough on its own, because some mutations are
    rooted at a WORKTREE path rather than at the main checkout — a rebase fetches
    into the worktree it rebases. Those are refused at the route boundary, which
    gates every non-GET on this accessor.
    """
    path = _repo_read()
    if _REPO_READ_ONLY_MSG:
        raise RepoReadOnly(_REPO_READ_ONLY_MSG)
    settling = _mutations_settling_reason()
    if settling:
        raise RepoReadOnly(settling)
    return path


def _read_only_reason() -> str | None:
    """Why mutating verbs are refused on the resolved checkout, or None.

    The one reader of the read-only state outside the accessors, so the route
    boundary, the ``/fleet`` payload and the row fields that describe this
    product's own build artifacts all consult one answer rather than three.
    """
    return _REPO_READ_ONLY_MSG


#: True between publishing a resolved checkout and finishing the reads that derive
#: its identity. Deliberately NOT part of ``_read_only_reason``: that answer feeds
#: the ``/fleet`` payload and the page's whole read-only mode, and folding a
#: sub-second transition into it would flash the banner and withdraw every control
#: on each re-resolution.
_REPO_DERIVING = False

#: Mutations refused during that window get this, not the adoption verdict, because
#: nothing about the path is wrong -- the answer is "not yet".
_DERIVING_REFUSAL = (
    "the configured checkout just changed and its base branch and upstream remote "
    "are still being read, so actions that rewrite history are refused until they "
    "settle. The next poll picks it up."
)


def _mutations_settling_reason() -> str | None:
    """Why a mutation must wait, or None once the checkout's identity is derived.

    Publishing the path and deriving what the path MEANS cannot be one step: the
    resolvers read the checkout, so the path has to be visible to them first. That
    leaves a window in which the mutation gate sees a managed checkout while
    ``BASE_BRANCH`` is still the shared default the switch reset it to -- and
    ``git rebase {remote}/{base}`` against a base that happens to exist in the new
    repository rewrites the worktree onto the wrong one, which nothing here can
    undo. So mutations are refused for the width of that window.

    Read by the mutating accessor AND by the route boundary, because not every
    mutation passes the accessor: a rebase is rooted at the worktree it rebases.
    """
    return _DERIVING_REFUSAL if _REPO_DERIVING else None


def _own_source_checkout() -> str | None:
    """The source checkout whose code this process is EXECUTING, or None.

    Derived from the location of the loaded module, so it needs no configuration
    and cannot go stale. None for a packaged or site-packages install, which is
    not a checkout at all.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name == "src" and (parent / "kiro_crew").is_dir():
            return str(parent.parent)
    return None


def _is_kirocrew_checkout(path: str) -> bool:
    """Whether *path* is a Kiro Crew source checkout. Blocking — stats only.

    Fail-closed: every marker must be present. ``.git`` alone is not enough
    because adopting an unrelated repository would list ITS worktrees and run
    Pull+Build, rebase and worktree-removal git commands inside it. ``.git`` is
    tested as a path rather than a directory since a linked worktree's is a file.
    """
    if not path:
        return False
    try:
        p = Path(path)
        return (
            (p / ".git").exists()
            and (p / "src" / "kiro_crew").is_dir()
            and (p / "pyproject.toml").is_file()
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _is_git_checkout(path: str) -> bool:
    """Whether *path* is a git checkout at all. Blocking — stats only.

    The weaker half of the marker test, and the difference between a path this
    app can READ and one it can do nothing with. ``.git`` is tested as a path
    rather than a directory because a linked worktree's is a file.
    """
    if not path:
        return False
    try:
        return (Path(path) / ".git").exists()
    except (OSError, RuntimeError, ValueError):
        return False


# Conventional clone locations, probed in this order and ONLY as a last resort.
# A candidate is adopted solely when it passes _is_kirocrew_checkout, so an
# absent or unrelated directory is skipped rather than assumed; no candidate is
# ever named in a user-facing message, because a path the user did not choose is
# noise to them. Names are matched case-insensitively against what is on disk,
# so only the canonical spelling is listed here.
_CHECKOUT_DIR_NAMES = frozenset({"kirocrew", "kiro-crew"})
_CHECKOUT_PARENT_DIRS = (
    "",
    "repos",
    "src",
    "projects",
    "dev",
    "git",
    "code",
    "workplace",
)


def _matching_child_dirs(base: Path, wanted: frozenset[str] | set[str]) -> list[Path]:
    """Child directories of *base* whose name is in *wanted*, compared
    case-insensitively and returned as the filesystem spells them. Sorted so a
    directory holding two case-variants resolves deterministically.
    """
    try:
        return sorted(
            child for child in base.iterdir() if child.name.lower() in wanted and child.is_dir()
        )
    except (OSError, ValueError):
        return []


def _candidate_checkouts() -> list[str]:
    """Conventional clone locations under the user's home, in probe order.

    EVERY path segment comes from a directory listing rather than from joining
    the guessed spellings. On a case-insensitive filesystem a blind join succeeds
    against a differently-cased directory and yields a path that does not match
    the ones git reports for the same tree; matching on disk also finds a clone
    whose case is not in the name list, on any OS.
    """
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        return []
    # One listing of home resolves every named parent to its real spelling.
    parents: dict[str, list[Path]] = {}
    for child in _matching_child_dirs(home, {p for p in _CHECKOUT_PARENT_DIRS if p}):
        parents.setdefault(child.name.lower(), []).append(child)
    found: list[str] = []
    for parent in _CHECKOUT_PARENT_DIRS:
        for base in ([home] if not parent else parents.get(parent, [])):
            found.extend(str(p) for p in _matching_child_dirs(base, _CHECKOUT_DIR_NAMES))
    return found


def _configured_main_repo() -> str:
    """The operator's explicit choice of main checkout, or ``""``.

    Env wins over config so a one-off override needs no file edit. Both are
    returned VERBATIM, with no marker test: the user named this path, so a typo
    must surface as an error against THAT path instead of being silently replaced
    by a discovered one.
    """
    explicit = os.environ.get("KIROCREW_DEVFLEET_REPO", "").strip()
    if explicit:
        return explicit
    configured = _load_dev_fleet_cfg().get("repo_path")
    return configured.strip() if isinstance(configured, str) else ""


def _configured_main_repo_checked() -> tuple[str, bool]:
    """``_configured_main_repo``'s answer, and whether the config read was whole.

    An env-set path is read off this process's own environment, which no other
    writer can be observed half-way through, so that route always reports a whole
    read. For the config route the flag is ``_load_dev_fleet_cfg_checked``'s own,
    because an unreadable ``config.json`` and one naming no path both resolve to
    ``""`` here -- the verbatim contract above cannot express "the file did not
    parse", and a caller comparing this value against a previous one must not read
    that as the operator having cleared the path.
    """
    explicit = os.environ.get("KIROCREW_DEVFLEET_REPO", "").strip()
    if explicit:
        return explicit, True
    section, whole = _load_dev_fleet_cfg_checked()
    configured = section.get("repo_path")
    return (configured.strip() if isinstance(configured, str) else ""), whole


def _repo_source_hint() -> str:
    """Where the current MAIN_REPO came from, phrased as the remedy to apply.

    Blocking (reads the config files) — executor ONLY. Being on an error path is
    not a licence to read files on the event loop: a network-backed home stalls
    every other request while this one composes its banner.
    """
    if os.environ.get("KIROCREW_DEVFLEET_REPO", "").strip():
        return "It is set by the KIROCREW_DEVFLEET_REPO environment variable."
    configured = _load_dev_fleet_cfg().get("repo_path")
    if isinstance(configured, str) and configured.strip():
        return "It is set by dev_fleet.repo_path in config.json."
    return (
        "Point Dev Fleet at your Kiro Crew checkout with the "
        "KIROCREW_DEVFLEET_REPO environment variable, or with "
        "dev_fleet.repo_path in config.json."
    )


def _discover_main_repo(configured: str | None = None) -> str:
    """Resolve the main checkout, or ``""`` when there is none to find.

    Blocking (config read + stats) — executor only; ``dev_fleet_startup`` calls
    it there. Order: the operator's explicit choice, the active project
    directory, the checkout this gateway runs from, then conventional clone
    locations. Every INFERRED candidate must pass the marker test, so the fleet
    can only ever be pointed at a real Kiro Crew checkout.

    ``""`` means "no checkout found" and is deliberately not a path: inventing
    one made the out-of-the-box dashboard report a checkout as missing that the
    user had never asked for, hiding the real question of where theirs lives.

    ``configured`` lets a caller that has already read tier 2 hand its snapshot in
    rather than paying a second read. That is not only cheaper, it closes a window:
    two reads of one file can disagree, and a caller that acted on the first while
    this function acted on the second could latch an INFERRED checkout on the
    strength of a configured path the first read had seen. Passing ``None`` reads it
    here, which is what a caller holding no snapshot wants.
    """
    if configured is None:
        configured = _configured_main_repo()
    if configured:
        return configured
    for candidate in (
        os.environ.get("KIROCREW_PROJECT_DIR", ""),
        _own_source_checkout() or "",
        *_candidate_checkouts(),
    ):
        if _is_kirocrew_checkout(candidate):
            return candidate
    return ""


def _default_main_repo() -> str:
    """The import-time main checkout hint.

    Env and stat-only tiers of ``_discover_main_repo`` (NO subprocess and no file
    reads — this module is imported from the async route-registration path, where
    both would block the event loop). ``dev_fleet_startup`` then re-resolves via
    the full discovery chain and normalizes the result to the PRIMARY checkout,
    both on the subprocess executor.
    """
    explicit = os.environ.get("KIROCREW_DEVFLEET_REPO", "").strip()
    if explicit:
        return explicit
    for candidate in (os.environ.get("KIROCREW_PROJECT_DIR", ""), _own_source_checkout() or ""):
        if _is_kirocrew_checkout(candidate):
            return candidate
    return ""


# --- configuration ---
def _default_main_repo_state() -> tuple[str, bool]:
    """Import-time checkout hint and whether an inferred tier supplied it."""
    repo = _default_main_repo()
    explicit = os.environ.get("KIROCREW_DEVFLEET_REPO", "").strip()
    return repo, bool(repo and not explicit)


# Startup replaces this stat-only hint after the complete discovery chain runs.
MAIN_REPO, MAIN_REPO_INFERRED = _default_main_repo_state()

#: The resolved checkout's OWN default branch, re-resolved by
#: ``_resolve_base_branch`` on every attempt that publishes a resolution. ``main``
#: is the import-time value and the fallback: a repository that publishes no
#: default branch and carries none of ``_LOCAL_BASE_CANDIDATES`` keeps it, which
#: is the same answer every consumer read before any repository was known.
BASE_BRANCH = "main"

#: Whether the current :data:`BASE_BRANCH` was STATED by the repository rather than
#: guessed from it. True only for the two tiers that answer the question asked -- a
#: remote's published ``HEAD``, or one of ``_LOCAL_BASE_CANDIDATES`` existing as a
#: branch. False for the import-time default and for the last-resort tier, which
#: publishes whatever branch happens to be checked out.
#:
#: Read by MUTATIONS, which is the whole reason it exists. A wrong base is nearly
#: free on a read -- the primary row carries a label, a behind-count goes unmeasured
#: -- and unrecoverable on a rebase, which rewrites a worktree's commits onto
#: ``{remote}/{BASE_BRANCH}`` and returns ``ok`` with no rollback path when the
#: replay is clean. The last-resort tier's own comment concedes its trigger is
#: ordinary: a checkout sitting on a feature branch is the normal state of a dev
#: box, so on a repository publishing no remote HEAD and carrying none of
#: ``_LOCAL_BASE_CANDIDATES`` a ``/rebase`` would rebase onto ``origin/<that feature
#: branch>`` -- and the branch it rewrites need not be the one checked out there.
_BASE_BRANCH_POSITIVE = False

#: Local branch names tried, in order, when no remote states a default. Both
#: conventional names are needed: this surface serves a repository the operator
#: named, and an older one still carries the legacy name as its only default.
_LOCAL_BASE_CANDIDATES = ("main", "master")  # wokeignore:rule=master

# --- full discovery: once per process, or once per attempt while unresolved ---
_DISCOVERY_DONE = False
_DISCOVERY_LOCK: asyncio.Lock | None = None
# The configured string the latching attempt read. `_invalid_resolution_is_stale`
# compares against THIS rather than against `MAIN_REPO`, because `MAIN_REPO` is
# `_resolve_primary_checkout` OF it and that rewrites a linked worktree to its
# primary -- so an operator whose path needs rewriting would differ on every poll
# and pay a re-resolution for a config nobody touched.
_LATCHED_CONFIGURED = ""

#: The checkout the last attempt RESOLVED, kept beside the configured string it was
#: resolved from. Compared rather than ``MAIN_REPO`` so this module's discovery still
#: reads no bare global, and it is what tells a re-resolution that LANDED SOMEWHERE
#: ELSE from one that merely re-confirmed the same path.
_LATCHED_RESOLVED = ""

#: Resets for caches derived from the RESOLVED checkout, registered by the modules
#: that own them. A memo about checkout A is an answer about a repository this app no
#: longer serves once it resolves B, and the two worst consumers are
#: ``git rebase {remote}/{base}`` and the prune ancestry gate -- both would run
#: against B carrying A's remote. Registered rather than reached directly because
#: ``repository`` sits BELOW those modules in the component DAG and cannot import
#: them; the registration is the dependency pointing the way it already points.
_CHECKOUT_RESETS: list[Callable[[], None]] = []


def register_checkout_reset(reset: Callable[[], None]) -> None:
    """Register *reset* to run whenever the resolved checkout changes.

    Called at import time by each module that memoizes something about the
    checkout. A cache that is not registered survives a switch, which is the
    defect this exists to prevent, so a new checkout-derived cache registers here
    in the same change that introduces it.
    """
    _CHECKOUT_RESETS.append(reset)


#: Incremented by every resolution that lands on a DIFFERENT checkout, so work that
#: began against one can tell that it finished against another. Clearing a cache is
#: only half the guard: a value computed before the clear lands after it, and a read
#: already in flight is still aimed at the path it captured. Owned here because this
#: module owns the resolution; the modules above it read it rather than keeping their
#: own, so there is exactly one answer to "is this still the same checkout".
_CHECKOUT_GEN = 0


def _checkout_generation() -> int:
    """Read the generation to compare later work against.

    Call it BEFORE the await that computes a value or spawns a read, so the
    comparison covers the whole window in which a resolution could land.
    """
    return _CHECKOUT_GEN


def _still_same_checkout(generation: int) -> bool:
    """True when nothing has re-resolved the checkout since *generation* was read."""
    return _CHECKOUT_GEN == generation


def _drop_checkout_derived_state() -> None:
    """Forget everything computed from the checkout being left behind.

    Run on every resolution that lands on a DIFFERENT path, including a resolution
    to nothing: a stale answer is as wrong when the fleet goes away as when it
    moves. Each registered reset is guarded on its own, because one module's
    failure must not leave the rest of the caches holding the old repository.
    """
    global _UPSTREAM_REMOTE, _FALLBACK_REPOS, BASE_BRANCH, _CHECKOUT_GEN
    global _BASE_BRANCH_POSITIVE
    # FIRST, before any clear: a write racing the clear must already see the new
    # generation, or it lands afterwards and puts the old repository's answer back.
    _CHECKOUT_GEN += 1
    _UPSTREAM_REMOTE = None
    _FALLBACK_REPOS = None
    # Back to the conventional default rather than the old checkout's branch name:
    # the resolvers below re-read it from the new checkout, and until they do, the
    # honest answer is the default every repository shares.
    BASE_BRANCH = "main"
    # And the default is a GUESS, so it clears with the name. Keeping the old verdict
    # would let the new checkout inherit the previous one's "the repository stated
    # this", which is what the flag exists to deny -- and it clears toward refusal,
    # so the gap before the resolvers re-run costs a mutation a retry, not a wrong
    # base.
    _BASE_BRANCH_POSITIVE = False
    for reset in _CHECKOUT_RESETS:
        try:
            reset()
        except Exception:  # noqa: BLE001 — one cache must not block the others
            runtime.logger.warning("dev-fleet: a checkout-derived cache reset failed")


def _invalid_resolution_is_stale() -> bool:
    """True when a latched INVALID path differs from the operator's current config.

    A found-but-invalid path is truthy, so it latches like any other resolution and
    ``_repo()`` raises ``RepoUnreadable`` against it. The config tier re-reads
    ``config.json`` on every call, so an operator who corrects a typo changes the
    answer this process resolves to, and holding the old verdict freezes a state the
    operator can still change -- the one shape this chain exists to remove. Reopening
    on a changed string alone keeps the resolved-and-valid case at its single guard
    and costs no git and no stats.

    Blocking (reads the config files) -- executor ONLY, matching every other reader
    of ``config.json`` here. An env-set path cannot change inside one process, so
    this answers False for it and no retry fires.

    A read that did not parse answers False as well. An unreadable ``config.json``
    yields the same ``""`` as one naming no path, so treating that as a change would
    reopen the latch on evidence nobody read: the reopened discovery would find no
    configured path, fall through to the INFERRED tiers, and latch a checkout the
    operator never named while their own setting sat in a file this process merely
    failed to read. Only a whole read can say the operator's answer changed.
    """
    if not (_DISCOVERY_DONE and (_REPO_INVALID_MSG or _REPO_READ_ONLY_MSG) and MAIN_REPO):
        return False
    configured, whole = _configured_main_repo_checked()
    if not whole:
        return False
    return configured != _LATCHED_CONFIGURED


async def ensure_main_repo_discovered() -> None:
    """Resolve the main checkout, and keep trying while there is none to find.

    The backend runs it from ``server.dev_fleet_startup``; the GATEWAY runs it lazily
    from its in-gateway cutover route (``gateway_routes._ensure_repo``), because
    ``_make_live`` validates its target against the discovered worktree set and the
    gateway never ran the backend's startup hook; and ``/api/fleet`` runs it per poll
    while nothing is resolved, so a user who answers the setup card stops seeing that
    card without restarting the gateway. Single-flight, so several concurrent first
    requests run discovery once between them rather than racing the globals below.

    Latched only once a checkout RESOLVED. An unresolved process has no answer worth
    keeping — there is no fleet to serve, and the answer changes the moment the
    operator writes ``dev_fleet.repo_path`` — so trying again is the point. Only that
    half self-heals: ``_load_dev_fleet_cfg`` re-reads ``config.json`` on every call,
    whereas ``KIROCREW_DEVFLEET_REPO`` is read off THIS process's environment, which
    no outside shell can change, so setting the variable still requires a restart and
    always will. A resolved path that FAILS the marker test latches
    too, and renders its own banner naming the path and the remedy rather than asking
    for a restart. That latch is reopened by ``_invalid_resolution_is_stale`` once the
    configured string changes: the config tier is re-read per call, so an operator who
    corrects a typo would otherwise meet exactly the frozen banner this chain removes
    for the not-found case. An env-set path cannot change inside one process, so the
    reopening never fires for it.

    Every global written here is a function of THIS attempt alone, including
    ``_REPO_INVALID_MSG``, which an unresolved attempt clears instead of inheriting.
    That is what makes a second attempt safe to run at all: the shape to avoid is a
    later attempt assigning ``MAIN_REPO`` while an earlier attempt's validation
    verdict survives beside it, because then ``_repo()`` hands out a path whose
    markers were never checked — and ``worktree remove``, ``update-ref -d``,
    ``pull --ff-only`` and ``pip install -e`` run inside whatever that is.

    Discovery runs on a local so the global is written exactly once per attempt —
    this keeps the function out of the ``MAIN_REPO`` AST ratchet's allowlist: nothing
    here reads the bare global, so a git call added to discovery (where it is most
    often still unresolved) cannot consume it unnoticed.
    """
    global _DISCOVERY_DONE, _DISCOVERY_LOCK, MAIN_REPO, MAIN_REPO_INFERRED, _REPO_INVALID_MSG
    global _LATCHED_CONFIGURED, _REPO_READ_ONLY_MSG, _LATCHED_RESOLVED, _REPO_DERIVING
    # A latched VALID resolution is final and returns here with no await at all, so an
    # install that has a fleet to serve pays nothing for the per-poll retry. Only the
    # latched-INVALID state falls through, and it settles under the lock so concurrent
    # polls share one config read rather than each taking their own. A READ-ONLY
    # verdict falls through with it: it is a resolution against a path the operator
    # named and may correct, so the reopen-on-changed-config self-heal has to reach it
    # or a typo keeps serving a stranger's repository until the gateway restarts.
    if _DISCOVERY_DONE and not ((_REPO_INVALID_MSG or _REPO_READ_ONLY_MSG) and MAIN_REPO):
        return
    if _DISCOVERY_LOCK is None:
        _DISCOVERY_LOCK = asyncio.Lock()
    async with _DISCOVERY_LOCK:
        loop = asyncio.get_running_loop()
        if _DISCOVERY_DONE:
            if not ((_REPO_INVALID_MSG or _REPO_READ_ONLY_MSG) and MAIN_REPO):
                return
            if not await loop.run_in_executor(subprocess_executor(), _invalid_resolution_is_stale):
                return
        # ONE checked read, handed to discovery below rather than read again there.
        # Two reads of one file can disagree, and the pair is what a torn write is
        # visible through: the staleness test above could see a whole, corrected path
        # and reopen, while a second read returned "" and sent discovery to the
        # INFERRED tiers. That latch is VALID, so it is final -- nothing re-resolves
        # it and only a restart clears it, with `Pull + Build` meanwhile mutating a
        # checkout the operator never named. A partial read therefore publishes
        # nothing: the attempt returns, and the next poll retries against a settled
        # file.
        configured, configured_whole = await loop.run_in_executor(
            subprocess_executor(), _configured_main_repo_checked
        )
        if not configured_whole:
            # Publish the UNRESOLVED state rather than leaving the import-time hint
            # standing. `_repo()` gates on `MAIN_REPO` alone and never consults
            # `_DISCOVERY_DONE`, so returning with that hint in place lets every
            # consumer operate on a checkout this attempt could not confirm: the
            # provisional value `_default_main_repo_state` picks before any config is
            # read, which `dev_fleet_startup` exists to replace and normalize. An
            # attempt that cannot read tier 2 has no basis for endorsing it, and the
            # alternative is `Pull + Build` running inside a checkout the operator may
            # not have chosen. Cleared, `_repo()` raises `RepoNotConfigured`, the page
            # shows the setup card, and the next poll retries against a settled file.
            # `_DISCOVERY_DONE` is part of that clearing. The reopen path arrives here
            # holding it True, and the gate above returns early once the pair is empty,
            # so leaving it set strands the very poll this branch promises and freezes
            # the page until a restart -- the failure this whole attempt exists to end.
            MAIN_REPO = ""
            MAIN_REPO_INFERRED = False
            _REPO_INVALID_MSG = None
            _REPO_READ_ONLY_MSG = None
            _DISCOVERY_DONE = False
            # The checkout is gone, not merely unconfirmed, so the memos taken from it
            # are as wrong here as on a switch to a different one.
            if _LATCHED_RESOLVED:
                _drop_checkout_derived_state()
            _LATCHED_RESOLVED = ""
            return
        invalid_msg: str | None = None
        read_only_msg: str | None = None
        # Set when the path gate refused fail-closed without judging the path, which
        # is a measurement this attempt does not hold rather than a verdict.
        unverified = False
        valid = False
        is_git = False
        hint = ""
        filters: list[str] = []
        filters_unread: str | None = None
        # The fence is asked about the path the operator NAMED before anything touches
        # it, and asked again about the primary checkout resolution rewrites that path
        # to. Both are needed and the ORDER is the point: everything else in this
        # attempt reads INSIDE the candidate -- the marker tests stat it,
        # `_resolve_primary_checkout` runs `git rev-parse` in it, and
        # `_configured_filter_commands` runs `git config --includes`, which follows
        # `include.path` and can therefore make git READ a file the fence exists to
        # keep unread. A verdict consulted after those probes is consulted too late.
        # Two spellings because they differ: a fenced linked worktree can have a
        # primary outside the fence, and a named path outside one can resolve to a
        # primary inside it, so clearing either by the other clears it by a path that
        # is not it.
        fenced = await loop.run_in_executor(subprocess_executor(), _fenced_reason, configured, "")
        if fenced:
            # Not discovered, ADOPTED-AS-NAMED for the banner alone: `_repo()` raises
            # on the invalid verdict below, so no consumer receives this path, and the
            # page names what the operator typed instead of showing the setup card as
            # though nothing were configured.
            discovered = configured
        else:
            discovered = await loop.run_in_executor(
                subprocess_executor(), _discover_main_repo, configured
            )
            if discovered:
                discovered = await loop.run_in_executor(
                    subprocess_executor(), _resolve_primary_checkout, discovered
                )
                fenced = await loop.run_in_executor(
                    subprocess_executor(), _fenced_reason, "", discovered
                )
        if discovered and fenced:
            hint = await loop.run_in_executor(subprocess_executor(), _repo_source_hint)
            if is_unverifiable_path_refusal(fenced):
                # The gate could not finish resolving in time and refused fail-closed
                # WITHOUT judging the path, so this attempt measured nothing. Say that,
                # and leave the resolution unlatched below so the next poll retries --
                # latched, one transient timeout would stand as a permanent refusal
                # asserting a measurement nobody took, and the retry the gate itself
                # advises could never fire.
                unverified = True
                invalid_msg = (
                    f"could not verify {discovered} against the protected-path "
                    f"list in time, so Dev Fleet is not serving it yet. The next "
                    f"refresh retries. {hint}"
                )
            else:
                invalid_msg = (
                    f"refused: {discovered} is a protected location. "
                    f"{runtime._redact(fenced)} {hint}"
                )
                _audit_security_refusal("path-fenced", discovered, fenced)
        elif discovered:
            # Tiers 1-2 are taken verbatim so a typo surfaces against the path the
            # user named — but "not replaced by a discovered checkout" and "not
            # validated" are separable, and only the first is wanted. Validated once
            # here rather than per call, so no request or refresher cycle pays the
            # stats; the messages are composed here too because they embed the
            # config-derived source hint, which reads files.
            valid, is_git, hint, (filters, filters_unread) = await loop.run_in_executor(
                subprocess_executor(),
                lambda: (
                    _is_kirocrew_checkout(discovered),
                    _is_git_checkout(discovered),
                    _repo_source_hint(),
                    _configured_filter_commands(discovered),
                ),
            )
            if not valid:
                # The generic READ surface — worktree list, branch, ahead and
                # own-commit counts, the dirty split, PR state, disk usage — is
                # plain git and gh work that holds for ANY repository, so an
                # operator who names one explicitly gets a fleet rather than a
                # banner. Only tiers 1-2 can reach here, because
                # `_discover_main_repo` adopts an INFERRED candidate solely when it
                # passes the marker test; the `configured` test states that
                # dependence instead of relying on it, so no repository is ever
                # auto-adopted. Mutating verbs stay refused: `worktree remove`,
                # `update-ref -d`, `pull --ff-only` and `pip install -e` act on
                # this product's own source, and a stranger's repository is not it.
                if configured and is_git:
                    # The one adoption this app performs on a path it did not verify as
                    # its own, so it is also the one that must ask git whether the
                    # repository would execute code on the reads that follow. The
                    # fence was already asked, twice, before this attempt read
                    # anything inside the path, so a fenced candidate never arrives
                    # here.
                    if filters:
                        # A filter driver is a COMMAND, and `git status` — which every
                        # fleet render runs against this checkout — is what would run
                        # it. Refused rather than served read-only, because "read-only"
                        # is a claim about what this app does to the repository, and a
                        # read that executes the repository's own code is not one.
                        invalid_msg = (
                            f"refused: {discovered} configures executable git filter "
                            f"drivers ({', '.join(sorted(filters)[:4])}), which a "
                            f"content-touching read would run. {hint}"
                        )
                        _audit_security_refusal(
                            "filter-drivers", discovered, ", ".join(sorted(filters)[:4])
                        )
                    elif filters_unread:
                        # A live scope git would read and this attempt could not. Same
                        # shape as the fail-closed path verdict above and latched the
                        # same way — not at all: reporting an unread scope as a
                        # configured driver would refuse the repository for something
                        # nobody saw, and latching it would make one transient git
                        # failure permanent.
                        unverified = True
                        invalid_msg = (
                            f"could not verify {discovered} is free of executable git "
                            f"filter drivers ({filters_unread}), so Dev Fleet is not "
                            f"serving it yet. The next refresh retries. {hint}"
                        )
                    else:
                        read_only_msg = (
                            f"read-only: {discovered} is a git repository but does not carry "
                            f"the Kiro Crew markers (src/kiro_crew/, pyproject.toml), so "
                            f"Dev Fleet reads its worktrees and refuses every action that "
                            f"would change it. {hint}"
                        )
                else:
                    invalid_msg = (
                        f"not a Kiro Crew checkout: {discovered} exists but does not carry the "
                        f"markers (.git, src/kiro_crew/, pyproject.toml). {hint}"
                    )
        MAIN_REPO = discovered
        MAIN_REPO_INFERRED = bool(discovered and not configured)
        # Assigned on EVERY branch. An attempt that found nothing must not inherit
        # an earlier attempt's verdict, or `_repo()` would raise against a path this
        # process does not hold.
        _REPO_INVALID_MSG = invalid_msg
        _REPO_READ_ONLY_MSG = read_only_msg
        # Written with the rest of this attempt's state, so the staleness test compares
        # against the string THIS attempt read. `MAIN_REPO` is the resolved form of it
        # and is the wrong side of that comparison.
        _LATCHED_CONFIGURED = configured
        # A resolution that landed on a DIFFERENT checkout invalidates every memo taken
        # from the old one, and it must happen BEFORE the resolvers below re-read: they
        # each return early on a latched value, so leaving the memo in place means the
        # new checkout is served with the old repository's remote, owner/repo and base
        # branch -- which `git rebase {remote}/{base}` and the prune ancestry gate then
        # act on. The read-only mode is what makes this reachable: a foreign checkout
        # now RESOLVES, so those memos latch against it, where before they declined
        # because `_repo_read()` raised.
        if discovered != _LATCHED_RESOLVED:
            _drop_checkout_derived_state()
        _LATCHED_RESOLVED = discovered
        # The path is published above and what it MEANS is derived below, and the two
        # cannot be one step: the resolvers read the checkout, so they need the path
        # visible first. Mutations are refused for exactly that width -- see
        # `_mutations_settling_reason`. `finally`, so a resolver that raises cannot
        # leave every mutation refused for the life of the process.
        #
        # Set BEFORE the credential-helper warm below, not after it: that warm is an
        # await, and an await after the path is published is a point where a rebase
        # request runs. It only happens when the first attempt left the helpers
        # unloaded -- a startup config read that failed and then recovered -- but in
        # that window the checkout is visible while BASE_BRANCH still holds whatever
        # the switch reset it to, which is the whole hazard this flag exists for.
        _REPO_DERIVING = True
        try:
            if runtime._GIT_TRUSTED_HELPERS is None:
                # Two `git config` subprocesses, and repo-INDEPENDENT (--system and
                # --global scope only, never repo-local), so this is a once-per-process
                # warm rather than something a re-resolution attempt repeats. `None` is
                # the not-yet-loaded sentinel; the loader always assigns a dict, so an
                # operator with no helpers configured still latches at `{}`. INSIDE the
                # try, so a warm that raises cannot leave every mutation refused for the
                # life of the process.
                await _load_trusted_credential_helpers()
            # Resolved BEFORE the remote: remote resolution reads
            # `branch.<base>.remote` and so needs the base branch name, while the base
            # branch resolver needs no remote — so the dependency runs one way only.
            await _resolve_base_branch()
            # Both decline to cache when `_repo_read()` raises and cost no subprocess in
            # that case, so an unresolved attempt leaves them to the attempt that
            # resolves.
            await _load_fallback_repos()
            await _upstream_remote()
        finally:
            _REPO_DERIVING = False
        # The local, not the global: see the ratchet note in the docstring. An attempt
        # whose path verdict was never measured does not latch: the next poll retries.
        _DISCOVERY_DONE = bool(discovered) and not unverified


# --- base branch resolution ---
# A branch NAME is interpolated into git argv both as a bare argument
# (``git fetch <remote> <base>``) and inside a rev range (``<remote>/<base>..HEAD``),
# so a repo-controlled value is constrained before it is trusted: a leading ``-``
# would be parsed as a flag, and an embedded ``..`` would split a range at the
# wrong place. The leading character class excludes ``-``; ``..`` is checked
# separately, because the body class admits it.
_BASE_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def _plausible_branch_name(name: str) -> bool:
    """Whether *name* is safe to interpolate into a git argv as a branch."""
    return bool(name) and ".." not in name and bool(_BASE_BRANCH_RE.fullmatch(name))


async def _resolve_base_branch() -> None:
    """Resolve ``BASE_BRANCH`` to the resolved checkout's own default branch.

    Read in the order the answer is trustworthy, and from ONE remote only. A
    remote's published ``HEAD`` is the repository's own statement of which branch
    is its default, but ``git remote`` lists names alphabetically, so consulting
    them in listing order lets an archive or fork remote outvote ``origin``.
    ``_upstream_remote`` resolves independently and falls back to ``origin``, so
    the pair can then disagree and ``/rebase`` rewrites a branch onto a base the
    upstream never published. ``origin`` is therefore the only remote consulted,
    or the sole remote of a checkout that has exactly one under another name.

    The local candidates are the fallback, and they need no remote at all — which
    matters, because remote resolution reads ``branch.<base>.remote`` and
    therefore cannot run before the base branch is known. A base taken from a
    local branch composes with that read: ``_upstream_remote`` resolves the remote
    THAT branch tracks.

    Re-resolved on every publishing attempt rather than cached behind a sentinel:
    an attempt that publishes a DIFFERENT checkout must not inherit the previous
    repository's default branch, and the cost is a few short git reads once per
    resolved attempt.

    A resolution that finds nothing at all leaves the value alone, so a process with
    no readable checkout keeps ``main`` and every consumer reads the name it always
    did. A readable checkout always answers something, because its own HEAD is the
    final tier: a name that matches no ref is worse than a name that is merely not
    the base, since `{remote}/{base}` is queried against it.

    Each tier also records whether its answer is the repository's STATEMENT of its
    default branch or this function's guess, in :data:`_BASE_BRANCH_POSITIVE`. The
    first two tiers state it; the final tier guesses, and mutations refuse on a guess
    through :func:`base_branch_mutation_refusal`. Reads are served either way -- being
    wrong about the label costs a row's caption, being wrong about the rebase base
    costs another worktree's commits.
    """
    try:
        repo = _repo_read()
    except RepoUnavailable:
        # No checkout to ask. Reaching git here would answer for whatever tree the
        # backend happens to sit in, which is the hazard the accessor exists for.
        return
    # Captured before the first await, and every tier publishes THROUGH the closure
    # below rather than assigning the global, so the fence cannot be skipped by a
    # tier added later: an attempt whose checkout was replaced mid-read has resolved
    # the wrong repository's default branch, and the switch already reset the value
    # to the shared default for the resolvers to re-read.
    generation = _checkout_generation()

    def _publish(name: str, *, positive: bool) -> None:
        global BASE_BRANCH, _BASE_BRANCH_POSITIVE
        if _still_same_checkout(generation):
            BASE_BRANCH = name
            # Set through the SAME fence and in the same assignment, so no reader can
            # observe the name of one tier beside the verdict of another.
            _BASE_BRANCH_POSITIVE = positive

    rc, remotes, _err = await _run_gated_git_soft(repo, "remote", timeout=5)
    names = remotes.split() if rc == 0 else []
    remote = "origin" if "origin" in names else (names[0] if len(names) == 1 else "")
    if remote:
        ref = await _git(repo, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD")
        # Spelled ``<remote>/<branch>``, and the branch half may itself carry
        # slashes, so only the FIRST separator belongs to the remote.
        head = ref.split("/", 1)[1] if ref and "/" in ref else ""
        if _plausible_branch_name(head):
            # The repository's own statement of its default branch.
            _publish(head, positive=True)
            return
    for candidate in _LOCAL_BASE_CANDIDATES:
        if await _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}"):
            # A conventional default that EXISTS here. Not the repository saying so,
            # but the name is a default by convention rather than by accident of
            # whatever is checked out, so a mutation may act on it.
            _publish(candidate, positive=True)
            return
    # Last resort: the branch the checkout is actually on. A repository whose base is
    # named something else entirely -- `trunk`, `develop` -- publishes no remote HEAD
    # and carries neither candidate, and the alternative is keeping a name that names
    # no ref: the primary row is labelled with it and `{remote}/{base}` is queried
    # against it. It ranks BELOW the candidates because a checkout sitting on a
    # feature branch is the ordinary state of a dev box, and a present `main` is the
    # better answer there than whatever is checked out at this moment.
    #
    # Published as NOT positive for that same reason. It is the best label available
    # and a fine answer for a read, but it is a guess about which branch is the base,
    # and `_rebase_locked` refuses rather than rewrite a worktree onto a guess.
    checked_out = await _git(repo, "symbolic-ref", "--short", "HEAD") or ""
    if _plausible_branch_name(checked_out):
        _publish(checked_out, positive=False)


def base_branch_mutation_refusal() -> str | None:
    """Why a MUTATION must not act on :data:`BASE_BRANCH`, or ``None`` when it may.

    The reason lives here, beside the flag, rather than at each mutation: a caller
    reads one value and reports it, so a mutation added later cannot get the gate
    subtly different, and there is one sentence to change.

    A refusal is a REFUSAL and not a fallback to ``main``. Guessing here is what the
    finding is about: on a repository publishing no remote ``HEAD`` and carrying none
    of ``_LOCAL_BASE_CANDIDATES``, ``main`` names no ref at all, so
    ``{remote}/main`` fails the fetch -- and if a same-named ref does happen to
    exist, it is a stranger's branch this app has no reason to rewrite onto.

    Clears by itself. Every publishing attempt re-resolves, so an operator who
    pushes a default branch or sets the remote's ``HEAD`` is served on the next poll
    with no restart.
    """
    if _BASE_BRANCH_POSITIVE:
        return None
    return (
        f"refusing to rebase: the configured checkout does not state which branch is "
        f"its default -- no remote publishes HEAD and neither "
        f"{' nor '.join(_LOCAL_BASE_CANDIDATES)} exists, so {BASE_BRANCH!r} is the "
        f"branch that happens to be checked out rather than the base. Rebasing onto "
        f"it would rewrite this worktree's commits onto a guess, with no undo once "
        f"the replay is clean. Set the remote's default branch (git remote set-head "
        f"<remote> -a) or create the base branch locally; the next refresh retries."
    )


# --- upstream remote resolution (replaces hardcoded 'origin') ---
_UPSTREAM_REMOTE: str | None = None


async def _upstream_remote() -> str:
    """Resolve the configured remote for BASE_BRANCH, falling back to 'origin'.

    Uses `git config branch.<BASE_BRANCH>.remote` so renamed remotes (e.g.
    'kirocrew' instead of 'origin') are honoured automatically. Cached at
    startup via dev_fleet_startup().
    """
    global _UPSTREAM_REMOTE
    if _UPSTREAM_REMOTE is not None:
        return _UPSTREAM_REMOTE
    try:
        repo = _repo_read()
    except RepoUnavailable:
        # A repo that never resolved must not reach git at all — it would
        # answer for whatever tree the backend happens to sit in. Remote
        # resolution degrades to git's conventional default instead of failing.
        return "origin"
    # Captured before the two awaits below. This memo has no expiry, and
    # `git rebase {remote}/{base}` plus the prune ancestry gate both act on it, so a
    # value resolved against the checkout being left behind must not be memoized
    # against the one now configured.
    generation = _checkout_generation()
    rc, out, _ = await _run_gated_git_soft(
        repo, "config", f"branch.{BASE_BRANCH}.remote", timeout=5
    )
    cand = out.strip() if rc == 0 else ""
    # Repo-writable config could smuggle an option-like value ("--exec=...")
    # that later argv interpolation (`git rebase {remote}/main`) would parse
    # as a flag. Accept only a plausible remote NAME that git itself lists.
    if cand and not cand.startswith("-") and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", cand):
        rc2, remotes, _ = await _run_gated_git_soft(repo, "remote", timeout=5)
        if rc2 == 0 and cand in remotes.split():
            if not _still_same_checkout(generation):
                # The name is this checkout's answer, not the configured one's.
                # Returned WITHOUT memoizing, and git's conventional default is
                # the honest degraded answer the unresolved path already uses: a
                # wrong name fails the rebase loudly, where a stranger's name can
                # match a same-named remote here and point somewhere else. The
                # next call re-resolves against the checkout now configured.
                return "origin"
            _UPSTREAM_REMOTE = cand
            return _UPSTREAM_REMOTE
    if not _still_same_checkout(generation):
        return "origin"
    _UPSTREAM_REMOTE = "origin"
    return _UPSTREAM_REMOTE


# Legacy-remote fallback: a renamed project keeps old remotes (e.g. origin ->
# the pre-rename repo) whose PRs cover older worktrees. A fallback repo's
# merged verdict is trusted ONLY when that remote's BASE_BRANCH is an ANCESTOR
# of the upstream BASE_BRANCH — i.e. everything merged there is contained in
# the current main, so "merged" still means "content is shipped".
_FALLBACK_REPOS: list[str] | None = None


def _same_path(a: str, b: str) -> bool:
    # "Cannot resolve" means "not the same path", never a crash: ValueError
    # covers unresolvable operands (an embedded NUL byte in caller-supplied
    # input), OSError covers ELOOP and friends, and RuntimeError covers the
    # symlink-loop signal Path.resolve() raises on some platform/version
    # combinations instead of ELOOP.
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, ValueError, RuntimeError):
        return False


# owner/repo capture, shared by identity normalization and the fallback scan.
_REPO_PATH_RE = re.compile(r"[:/]([^/]+/[^/]+?)(?:\.git)?$")


def _normalize_repo_identity(url: str) -> tuple[str, str] | None:
    """Return a ``(host, owner/repo)`` identity for a git remote URL, or None.

    Normalizes across the spellings git accepts for the same repository so two
    aliases of one repo compare equal:

    - ``https://github.com/owner/Repo.git`` and ``git@github.com:owner/repo``
      collapse to the same identity;
    - a trailing ``.git`` is stripped and the whole identity is lowercased;
    - the host is part of the identity, so ``owner/repo`` on two different
      forges stays distinct.

    Returns None when no ``owner/repo`` can be extracted.
    """
    url = url.strip()
    m = _REPO_PATH_RE.search(url)
    if not m:
        return None
    owner_repo = m.group(1).lower()
    # Host: scp-style ``user@host:owner/repo`` or a URL with a scheme.
    host = ""
    scp = re.match(r"(?:[^@/]+@)?([^/:]+):", url)
    if scp and "://" not in url:
        host = scp.group(1).lower()
    else:
        scheme = re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^@/]+@)?([^/:]+)", url)
        if scheme:
            host = scheme.group(1).lower()
    return (host, owner_repo)


async def _load_fallback_repos() -> None:
    global _FALLBACK_REPOS
    try:
        repo = _repo_read()
    except RepoUnavailable:
        # No checkout, no remotes to enumerate; the fallback list stays empty.
        return
    # Captured before the awaits below. Every entry is derived from THIS checkout's
    # remotes, and the list decides which worktree-name prefixes count as legacy and
    # whose merged verdict is trusted, so a list built from the checkout being left
    # behind must not be published against the one now configured.
    generation = _checkout_generation()
    repos: list[str] = []
    seen: set[tuple[str, str]] = set()
    upstream = await _upstream_remote()
    # Resolve upstream's own repo identity so a remote carrying upstream's own
    # repo NAME is not mistaken for a pre-rename repo — whether it is an alias
    # of upstream (e.g. an ``origin`` left in place after the tracking remote
    # was renamed) or a fork of it under another owner. ``merge-base
    # --is-ancestor`` is trivially true for identical refs and stays true for a
    # fork until it diverges, so either would enter the fallback list under
    # upstream's own name, and the derived ``<reponame>-wt-`` prefix then flags
    # every worktree as legacy.
    upstream_identity: tuple[str, str] | None = None
    rc_up, up_url, _ = await _run_gated_git_soft(repo, "remote", "get-url", upstream, timeout=5)
    if rc_up == 0:
        upstream_identity = _normalize_repo_identity(up_url)
    rc, out, _err = await _run_gated_git_soft(repo, "remote", timeout=5)
    if rc == 0:
        for remote in out.split():
            if remote == upstream:
                continue
            rc2, _, _ = await _run_gated_git_soft(
                repo,
                "merge-base",
                "--is-ancestor",
                f"{remote}/{BASE_BRANCH}",
                f"{upstream}/{BASE_BRANCH}",
                timeout=10,
            )
            if rc2 != 0:
                continue
            rc3, url, _ = await _run_gated_git_soft(repo, "remote", "get-url", remote, timeout=5)
            if rc3 != 0:
                continue
            identity = _normalize_repo_identity(url)
            if identity is None:
                continue
            # Skip a remote whose repo NAME is upstream's — an alias of upstream
            # itself, or a fork of it under another owner. Name equality is the
            # right predicate for both consumers of the fallback list: the
            # legacy-worktree prefixes are derived from the repo name alone, so
            # a same-named entry yields the ``<name>-wt-`` prefix that every
            # current-convention worktree matches, and the PR-status fallback
            # should not consult a fork either — a fork is not a pre-rename
            # repo. Name equality also subsumes identity equality, so the alias
            # case stays covered. The genuine pre-rename case — a DIFFERENTLY
            # named repo whose main is an ancestor of upstream's — still
            # qualifies.
            if upstream_identity is not None and (
                identity[1].rsplit("/", 1)[-1] == upstream_identity[1].rsplit("/", 1)[-1]
            ):
                continue
            if identity in seen:
                continue
            seen.add(identity)
            repos.append(identity[1])
    if not _still_same_checkout(generation):
        # Left at the not-yet-loaded sentinel rather than published: the next call
        # re-enumerates the remotes of the checkout now configured.
        return
    _FALLBACK_REPOS = repos


async def _load_trusted_credential_helpers() -> None:
    extra: dict[str, str] = {}
    base = int(runtime._GIT_ENV_NEUTRALIZERS["GIT_CONFIG_COUNT"])
    idx = base
    # SYSTEM scope first, then GLOBAL, mirroring git's own precedence: for a
    # multi-valued key like credential.helper the later entry wins, so the
    # operator's own global setting still overrides a machine-wide default.
    #
    # System scope is read at all because that is where macOS puts the operator's
    # helper: Xcode's Command Line Tools ship
    # `credential.helper = osxkeychain` in
    # /Library/Developer/CommandLineTools/usr/share/git-core/gitconfig, and a
    # stock install has NOTHING in global. Scanning only --global therefore left
    # the neutralizer's reset unrepaired on every stock macOS host, and `git
    # fetch` died with "could not read Username" — no tty to prompt on.
    #
    # Repo-LOCAL scope stays excluded. That is the attack surface the reset
    # exists for: a checkout Dev Fleet builds can write .git/config, and a helper
    # from there would run in the credential-bearing standard tier.
    for scope in ("--system", "--global"):
        rc, out, _err = await runtime._run_cmd(
            ["git", "config", scope, "--get-regexp", r"^credential(\..+)?\.helper$"],
            timeout=5,
        )
        # A missing system gitconfig is rc != 0 with no output — normal, not an
        # error worth surfacing.
        if rc != 0 or not out:
            continue
        for line in out.splitlines():
            key, _, val = line.partition(" ")
            if not key.endswith(".helper"):
                continue
            trusted_val = runtime._sanitize_helper_value(val.strip())
            if trusted_val is None:
                # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
                # No secret is logged: the helper VALUE is deliberately
                # withheld; only the config KEY name is recorded.
                runtime.logger.warning(
                    "dev-fleet: skipping helper with unverifiable provenance"
                    " for config key %s (%s scope)",
                    key,
                    scope.lstrip("-"),
                )
                continue
            extra[f"GIT_CONFIG_KEY_{idx}"] = key
            extra[f"GIT_CONFIG_VALUE_{idx}"] = trusted_val
            idx += 1
            if idx - base >= 9:
                break
        if idx - base >= 9:
            break
    if idx > base:
        extra["GIT_CONFIG_COUNT"] = str(idx)
    runtime._GIT_TRUSTED_HELPERS = extra


def _load_dev_fleet_cfg_checked() -> tuple[dict, bool]:
    """The ``dev_fleet`` config section, and whether every file present parsed.

    Read lazily and best-effort from ``config.json`` plus its local overlay, and
    never raising: a missing file or section gives ``{}``. Read directly rather
    than through KiroCrewConfig (a separate process owns the validated loader) so
    a purely cosmetic template needs no schema dependency and can never break the
    fleet payload.

    The second element is the one thing a caller cannot recover from the first. A
    file that is present but unreadable or unparseable contributes no keys, so it
    is indistinguishable from a file that simply carries none -- and a caller that
    decides something on a CHANGE in a value needs those two apart, because a read
    that failed is not evidence the operator cleared the setting. ``False`` means
    at least one file that is present could not be read, so the section is a
    partial view rather than the operator's answer.
    """
    section: dict = {}
    try:
        from kiro_crew.config.loader import config_dir

        base = config_dir()
    except Exception:  # noqa: BLE001
        return section, False
    whole = True
    for fname in ("config.json", "config.local.json"):
        p = base / fname
        try:
            if not p.is_file():
                continue
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            whole = False
            continue
        if isinstance(raw, dict) and isinstance(raw.get("dev_fleet"), dict):
            section.update(raw["dev_fleet"])
    return section, whole


def _load_dev_fleet_cfg() -> dict:
    """The ``dev_fleet`` config section alone, for callers that read one setting.

    A caller fetching a single value wants the best-effort section and has no use
    for whether the read was whole, so this keeps the plain signature.
    """
    return _load_dev_fleet_cfg_checked()[0]


# --- worktree discovery via git worktree list --porcelain ---
# Bounds on what one `worktree list` may turn into. The records are written by the
# repository's own admin files, so their number and their field widths are chosen by
# whoever controls the repository -- not by this app and not by the operator. Every
# record is parsed, retained in the fleet cache and rendered, so an oversized listing
# would cost the gateway memory for as long as the cache holds it. A repository past
# these bounds is reported as over-limit rather than silently served short.
_WORKTREE_LIST_MAX_BYTES = 512 * 1024
_WORKTREE_RECORD_MAX = 512
_WORKTREE_FIELD_MAX_CHARS = 4096


def _parse_worktree_porcelain(raw: str) -> list[dict]:
    """Parse `git worktree list --porcelain` into records, bounded in count and width.

    Truncation is REPORTED, never quiet: the caller raises on an over-limit listing, so
    a fleet served short would be a fleet missing worktrees nobody was told about.
    """
    entries: list[dict] = []
    current: dict = {}
    for line in raw.splitlines():
        if len(entries) > _WORKTREE_RECORD_MAX:
            break
        # Each field is author-controlled: a lock reason or a path can be arbitrarily
        # wide, and it is retained in the cache and rendered. REFUSED rather than
        # sliced -- a truncated lock reason reads as the whole reason, so the operator
        # decides against text that says something its author did not.
        if len(line) > _WORKTREE_FIELD_MAX_CHARS:
            raise RepoUnreadable(
                "this repository's worktree listing has a field longer than "
                f"{_WORKTREE_FIELD_MAX_CHARS} characters, so the fleet is not served "
                "rather than served with a truncated value presented as complete"
            )
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[9:]
        elif line.startswith("HEAD "):
            current["head"] = line[5:]
        elif line.startswith("branch "):
            ref = line[7:]
            current["branch"] = ref.split("refs/heads/", 1)[-1] if "refs/heads/" in ref else ref
        elif line == "detached":
            current["branch"] = None
        elif line == "prunable" or line.startswith("prunable "):
            # git flags an entry `prunable` when its checkout directory is gone
            # but the admin record survives (a `rm -rf` with no
            # `git worktree prune`). The reason text is optional.
            current["prunable"] = line[len("prunable") :].strip() or "unknown"
        elif line == "locked" or line.startswith("locked "):
            # An explicit human "do not touch this tree". `git worktree remove`
            # refuses a locked tree, and its refusal comes LAST -- after any
            # pre-removal cleanup has already run -- so every removal path has
            # to recognise the lock up front instead of discovering it too late.
            # The reason text is optional and author-controlled.
            current["locked"] = line[len("locked") :].strip() or "unknown"
    if current:
        entries.append(current)
    return entries


async def _worktree_porcelain_entries() -> list[dict]:
    """List all git worktree records of MAIN_REPO, including prunable entries."""
    # Nothing to discover when no checkout resolved; _repo_read() raises
    # RepoNotConfigured and the setup state is the caller's job. The READ
    # accessor: enumerating worktrees is the generic surface's foundation and
    # holds for any git repository, so a checkout this app may only read still
    # produces a fleet.
    repo = _repo_read()
    # Through the SAME gated spawn every other read uses, not a bare one. This call
    # site had the default `standard` tier and no clearance, which is where
    # `_GIT_TRUSTED_HELPERS` is handed over -- so a foreign checkout got this app's
    # credential helpers, and an include written into its config AFTER adoption was
    # followed by this very command. `worktree` converts no content, so it is on the
    # safelist and the read still happens; what changes is that it is now bounded by
    # the strict tier and re-cleared immediately before the spawn, exactly like the
    # per-row reads whose one-time discovery answer this enumeration precedes.
    rc, stdout, stderr = await _run_gated_git(
        repo,
        "worktree",
        "list",
        "--porcelain",
        timeout=10,
        max_output_bytes=_WORKTREE_LIST_MAX_BYTES,
    )
    if rc != 0:
        # Propagate sandbox/git failures as a RuntimeError so callers can
        # surface the real reason instead of returning silent empty lists.
        raw = (stderr or stdout or "").strip()
        if f"passed the {_WORKTREE_LIST_MAX_BYTES}-byte bound" in raw:
            # Named before the generic git-error path below, which would otherwise
            # report a repository this size as corrupt and send the operator to debug
            # a healthy checkout.
            raise RepoUnreadable(
                "this repository's worktree listing passed the "
                f"{_WORKTREE_LIST_MAX_BYTES}-byte bound, so the fleet is not served "
                "rather than served incomplete"
            )
        if "sandbox unavailable" in raw:
            # Do NOT clip to the generic git-error length here. The sandbox layer
            # puts the *remedy* (which opt-in to set, or that an EPERM is a
            # Seatbelt nesting artifact rather than a missing backend) AFTER a
            # ~180-char preamble, so a tight cap would surface the diagnosis and
            # swallow the fix. Keep a generous bound purely to stop an unbounded
            # stderr reaching the UI.
            raise RepoUnreadable(raw[: runtime._SANDBOX_ERR_MAX])  # already prefixed by _run_cmd
        if raw.startswith(runtime._UNRESOLVED_TOOL_PREFIX):
            # git never ran: the HOST has no git the resolver is willing to
            # execute. Checked before the .git probe because the probe's
            # outcome is irrelevant here — wrapping this in "worktree
            # discovery failed in <repo>" would send users to debug a healthy
            # checkout. The trusted-PATH detail is
            # operator-diagnostic, so it goes to the log, not the banner.
            runtime.logger.warning("dev-fleet: %s", raw)
            raise RepoUnreadable(runtime._unresolved_tool_message("git"))
        # Every other git failure must NOT be swallowed into a silent [] —
        # which the UI renders as the "No worktrees found / Nothing under the
        # worktrees root yet" empty state. When MAIN_REPO is wrong that empty
        # state is a lie: the fleet is not empty, it is unreadable. Reaching here
        # means a checkout WAS named — discovery only ever adopts a path that
        # carries the Kiro Crew markers, so an unverifiable one came from the
        # operator's own env var or config — so name it and raise, and
        # api_dev_fleet_fleet's error path renders the Discovery Error banner.
        # The .git probe is a filesystem stat — on a wedged network mount it
        # can block indefinitely, and this branch is reachable precisely when
        # the checkout is unhealthy (git already failed or timed out against
        # it). Same "Blocking — executor only" convention as _is_checkout().
        loop = asyncio.get_running_loop()
        repo_is_git = await loop.run_in_executor(
            subprocess_executor(), (Path(repo) / ".git").exists
        )
        if not repo_is_git:
            # Name the mechanism that supplied the path: the remedy is to edit
            # THAT one, and a message listing both leaves the user guessing which
            # of the two they set. Resolved on the executor with the probe above —
            # it reads config files, and a network-backed home would otherwise
            # stall the gateway loop on the way to rendering an error banner.
            hint = await loop.run_in_executor(subprocess_executor(), _repo_source_hint)
            raise RepoUnreadable(
                f"main checkout not found: {repo} is missing or not a git " f"checkout. {hint}"
            )
        # The repo exists but git failed for some other reason (corrupt repo,
        # permissions): surface git's own message, redacted and bounded.
        raise RepoUnreadable(
            f"git worktree discovery failed in {repo}: "
            f"{runtime._redact(raw)[:runtime._GIT_ERR_MAX] or 'unknown git error'}"
        )
    entries = _parse_worktree_porcelain(stdout)
    if len(entries) > _WORKTREE_RECORD_MAX:
        # Refused whole, not truncated: a short fleet is a fleet missing worktrees the
        # operator was never told about, and one of the missing ones could be the main
        # checkout every row is anchored to.
        raise RepoUnreadable(
            f"this repository lists more than {_WORKTREE_RECORD_MAX} worktrees, so the "
            "fleet is not served rather than served incomplete"
        )
    # `git worktree list --porcelain` always lists the primary checkout
    # first — that is the authoritative main, regardless of whether
    # MAIN_REPO itself points at a linked worktree (it is only the
    # repository discovery hint).
    for i, e in enumerate(entries):
        e["is_main"] = i == 0

    # The adoption fence gates the path the OPERATOR named and the primary it
    # resolves to. These roots are neither: a linked worktree's location is a path
    # written into the repository's own admin files, which this module already
    # treats as adversary-controlled. `/api/disk` walks each root recursively, so a
    # record naming a credential directory has that directory read. Asked here
    # because this is the one function every worktree consumer goes through.
    def _fence_each() -> list[str | None]:
        # One executor hop for the whole list: `_fenced_reason` waits on the
        # bounded path-resolution pool, so it never runs on the event loop.
        return [_fenced_reason(str(e.get("path") or ""), "") for e in entries]

    loop = asyncio.get_running_loop()
    reasons = await loop.run_in_executor(subprocess_executor(), _fence_each)
    kept: list[dict] = []
    for entry, reason in zip(entries, reasons):
        if not reason:
            kept.append(entry)
            continue
        if entry.get("is_main"):
            # Refused whole rather than dropped: `is_main` anchors the fleet, so
            # removing it promotes a linked worktree to primary and the operator is
            # served a fleet rooted somewhere nobody named.
            raise RepoUnreadable(
                f"refused: the primary checkout git reports for {repo} is a protected "
                f"location, so its worktrees were not read. {reason}"
            )
        # One bad record must not cost the rest of the fleet, so the row is
        # withheld rather than the read refused -- but withholding it is a
        # permission decision on a protected location, so it is audited like the
        # sibling refusals this app takes at its HTTP boundaries. The log line
        # stays for the operator diagnosing their own config.
        _audit_security_refusal("worktree-root-fenced", str(entry.get("path") or ""), reason)
        runtime.logger.warning("dev-fleet: worktree root withheld: %s", reason)
    return kept


async def _discover_worktrees() -> list[dict]:
    """List usable git worktrees of MAIN_REPO."""
    entries = await _worktree_porcelain_entries()
    # A `prunable` entry has no checkout on disk, so every git call against its
    # path fails and it renders as a ghost row with no branch, behind count or
    # timestamp — and no refresh ever clears it, because git keeps reporting the
    # record until `git worktree prune` runs. Drop those. The primary checkout
    # is never filtered: it anchors `is_main`, and losing it would promote a
    # linked worktree to main.
    return [e for e in entries if e.get("is_main") or not e.get("prunable")]


async def _assert_read_cleared(path: str, *, read_only: bool) -> None:
    """Refuse a git read against a checkout that would run its own code on one.

    Two vectors, both re-measured here because both are things the repository can add
    AFTER it was adopted. An ``include.path`` makes git open a file of the
    repository's choosing while parsing config on every command, whatever the verb; a
    filter driver bound by ``.gitattributes`` runs a program of its choosing on a read
    that converts content. Discovery asks once and its answer is about the repository
    AS IT WAS THEN: either one written into the config afterwards is read by the next
    ``git`` invocation and by nothing else, so the one-time answer stops being true
    exactly where it matters. The clearance is therefore re-taken immediately before
    the read it licenses, and the repository stops being served the moment it stops
    being clean.

    Asked for EVERY read rather than for a list of content-touching subcommands. A
    list would have to be kept in step with every read this app grows, and the one
    that gets forgotten is the bug; the price is two ``git config`` reads per git
    read, paid only on a foreign checkout an operator named explicitly, and neither
    of them opens the object database.

    Nothing is latched. The banner this raises names the reason, and the next poll
    re-measures — a driver refuses again, while a git failure that happened to be
    transient clears itself.

    Only a checkout this app may READ is asked. This product's own checkout is the
    pre-existing trust boundary: its config is the operator's own, exactly like the
    global config no guard here probes.

    *read_only* is the caller's own reading of the disposition, passed in rather than
    re-read here. Reading it again would sample a global the caller already sampled,
    and a concurrent re-resolution between the two samples answers False on the second
    one -- which skips the probe entirely for a read still aimed at the foreign path
    the caller captured.
    """
    if not read_only:
        return
    loop = asyncio.get_running_loop()
    # Includes FIRST, and for two reasons. The ordering is load-bearing: the filter
    # probe below asks git with ``--includes``, so against a config naming one it would
    # itself open the included file -- the exact read this refuses. And the coverage is
    # the gap this closes. ``_include_refusal`` was taken only on the PROBE path, whose
    # chokepoint is ``_probe_git``; the fleet's own reads spawn through here instead, so
    # a repository that named no include when it was adopted and wrote
    # ``[include] path = <a credential store>`` afterwards had every later read open
    # that file. git follows ``include.path`` while parsing config on EVERY command, so
    # no verb safelist bounds this one -- the safelist answers which drivers a verb can
    # reach, and an include is followed before the verb is even considered.
    #
    # Reads the files rather than asking git, for the reason ``_include_refusal``
    # documents: asking would already have opened them.
    include_reason = await loop.run_in_executor(subprocess_executor(), _include_refusal, path)
    if include_reason is not None:
        # Audited on every occurrence, like the two below: an include appearing after
        # adoption is a repository asking for something it was not admitted with.
        _audit_security_refusal("config-include-refused", path, include_reason)
        raise RepoUnreadable(
            f"refused: {path} {include_reason}, so the read was not taken. "
            f"The next refresh retries."
        )
    drivers, unread = await loop.run_in_executor(
        subprocess_executor(), _configured_filter_commands, path
    )
    if drivers:
        # Audited: a filter driver can appear in a repository's config AFTER adoption, so
        # this refusal is the record that an admitted repository later asked for
        # something it may not have. Every occurrence, not the first -- a trail that
        # logs one cannot answer how often or how recently.
        _audit_security_refusal(
            "filter-driver-refused", path, f"configures {len(drivers)} filter driver(s)"
        )
        raise RepoUnreadable(
            f"refused: {path} configures executable git filter drivers "
            f"({', '.join(sorted(drivers)[:4])}), which a read would run."
        )
    if unread:
        # Also audited: doubt is refused, and a refusal nobody can see afterwards is
        # indistinguishable from a read that quietly succeeded.
        _audit_security_refusal("filter-scan-unverified", path, f"filter config unread ({unread})")
        raise RepoUnreadable(
            f"refused: could not verify {path} is free of executable git filter "
            f"drivers ({unread}), so the read was not taken. The next refresh retries."
        )


#: Git subcommands this app may run inside a checkout it does NOT own. A SAFELIST,
#: not a denylist, because the failure mode has to be a refusal: a verb added here
#: later, or a git version that teaches an existing verb to convert content, must
#: cost a measurement rather than a credential.
#:
#: What disqualifies a verb is the ability to invoke a program the TARGET REPOSITORY
#: configures -- a ``filter.<name>.clean/smudge/process`` driver on a working-tree
#: conversion, or a ``diff.<name>.textconv`` driver on a diff. Those are the two
#: ways a read turns into repo-controlled execution. The verbs below resolve refs,
#: count commits, read config and list worktrees: none converts a blob, so none can
#: reach either driver class. ``status`` and ``cherry`` are deliberately ABSENT --
#: the first runs clean filters against the working tree, the second goes through
#: diff machinery.
_NON_CONVERTING_GIT_VERBS = frozenset(
    {
        "rev-parse",
        "symbolic-ref",
        "rev-list",
        "log",
        "worktree",
        "config",
        "remote",
        "ls-files",
        # Reads the commit graph and answers with an exit status. It touches no blob,
        # so neither a filter driver nor a textconv driver is reachable through it.
        "merge-base",
    }
)

#: Verbs already reported as refused, so a per-poll refusal is logged once rather
#: than on every row of every refresh.
_REFUSED_CONVERTING_VERBS_LOGGED: set[str] = set()


class ConvertingVerbRefused(RuntimeError):
    """A verb that can convert content was asked of a checkout this app does not own.

    Not an error and not a refusal of the repository: exactly one measurement is
    declined, and the caller reports that field unmeasured. Raised rather than
    returned so the gated spawn can keep one return shape, and caught by :func:`_git`,
    whose own contract has said `None` for an unmeasured read since before this split.
    """


async def _run_gated_git(
    git_dir: str,
    *args: str,
    timeout: int = 6,
    mode: str = "standard",
    generation: int | None = None,
    max_output_bytes: int | None = None,
) -> tuple[int, str, str]:
    """Spawn git under the clearance and the tier a foreign checkout requires.

    THE gated spawn: every read of a checkout this app may only read goes through
    here, so a call site cannot acquire the credential-bearing tier by forgetting to
    ask for anything. That is not hypothetical -- the worktree enumeration did exactly
    that, spawning at the default `standard` tier with no clearance while every
    per-row read was forcing `strict` and re-clearing.

    Returns the raw ``(rc, stdout, stderr)`` because some callers need git's own
    stderr to report a failure; :func:`_git` is the wrapper for the common case.

    Raises:
        ConvertingVerbRefused: the verb can convert content in a foreign checkout.
        RepoUnreadable: the clearance refused this read.
    """
    # Repo-controlled execution vectors are neutralized centrally in
    # _run_cmd via _GIT_ENV_NEUTRALIZERS — no per-call-site flags needed.
    # That chokepoint also carries GIT_OPTIONAL_LOCKS=0, so a read here never
    # rewrites the index of a checkout this app may only read. Filter drivers are
    # the vector that set cannot name, so they are asked about instead.
    # The disposition and the generation are sampled ONCE, here, and carried into the
    # clearance. Every later reader uses what was captured: re-reading either from the
    # global would let a concurrent re-resolution answer differently than the branch
    # taken below, for a read whose target path is already fixed.
    read_only = _REPO_READ_ONLY_MSG is not None
    # The generation the PATH was captured under, supplied by whoever captured it.
    # Sampling it here instead was a hole: a path captured BEFORE a config switch
    # then compared the NEW generation against itself, passed, and -- because the
    # switch also CLEARS the read-only verdict -- ran git inside the foreign
    # checkout at the `standard` tier, where the trusted credential helpers are
    # handed over. The gate only ever caught a switch landing between this sample
    # and the spawn. A caller that resolves the path fresh passes nothing and gets
    # the current generation, which for it IS the capture.
    captured = _checkout_generation() if generation is None else generation
    if not _still_same_checkout(captured):
        # Refused before the disposition is even read: at a stale generation the
        # disposition describes a different checkout than the path does.
        raise RepoUnreadable(
            f"refused: the configured checkout changed before reading {git_dir}, "
            f"so the read was not taken. The next refresh reads the checkout now "
            f"configured."
        )
    if read_only and (not args or args[0] not in _NON_CONVERTING_GIT_VERBS):
        # The sandbox tier CANNOT be the bound here. A foreign read asks for the
        # strict tier, but this app's backend is itself spawned inside a standard
        # sandbox, and a nested wrap is impossible by design -- so `sandbox.wrap_argv`
        # passes through and applies the stricter tier's ENV scrub only, leaving its
        # file-level hides at the outer tier, where `~/.aws` and `~/.ssh` stay
        # visible. A driver written between the clearance and the exec would
        # therefore run with the credential stores readable.
        #
        # So the bound is structural instead of confinement-based: this app never asks
        # git to convert content in a checkout it does not own, and a verb that
        # cannot convert cannot reach a filter or textconv driver at all. The cost is
        # one measurement -- the caller reports the field unmeasured, which this mode
        # already renders as unknown, and which is the honest answer for a tree
        # nobody looked inside.
        verb = args[0] if args else "(none)"
        # Audited on EVERY occurrence. This is a permission decision on a repository,
        # and an audit trail that records only the first refusal per verb cannot answer
        # how often or how recently one happened -- which is most of what such a trail
        # is read for. Only the operator-facing log line below is deduplicated, because
        # that one is advice and repeating it per row per refresh buries the rest.
        _audit_security_refusal(
            "content-conversion-refused", git_dir, f"git {verb} converts content"
        )
        if verb not in _REFUSED_CONVERTING_VERBS_LOGGED:
            _REFUSED_CONVERTING_VERBS_LOGGED.add(verb)
            runtime.logger.info(
                "dev-fleet: not running `git %s` in a checkout this app does not own "
                "— it can invoke a driver the repository configures, and the strict "
                "tier's file hides do not apply inside this backend's own sandbox",
                verb,
            )
        raise ConvertingVerbRefused(verb)
    if read_only:
        # A foreign checkout is repo-controlled, which is what the strict tier is
        # FOR -- _run_cmd hands `_GIT_TRUSTED_HELPERS` only to `standard`, calling
        # that the gateway-controlled tier. Forced rather than defaulted, so a call
        # site that asks for `standard` cannot hand this repository's own config a
        # credential helper. This, not the clearance below, is what bounds the harm:
        # a filter driver written into the config after the clearance was taken and
        # before the child execs still runs, and no check-then-spawn can prevent
        # that -- it just runs with no credential store visible and no helper.
        mode = "strict"

    # ONE spawn, and `pre_spawn` gates it on BOTH captured values. `pre_spawn` is
    # evaluated after sandbox preparation, with the spawn as the only await that
    # follows, so what it proves holds for the child that runs. Gating before the
    # call instead would leave the preparation hop in between, and that hop is
    # unbounded -- it may cold-probe the backend with a synchronous subprocess.
    #
    # The generation gate is OUTSIDE the disposition branch, because the pairing of
    # the two is the hazard: `git_dir` is fixed by the caller, and a resolution that
    # lands on a managed checkout CLEARS the read-only verdict. A read aimed at the
    # foreign path it captured would then sample `standard` here and receive the
    # trusted credential helpers. Every resolution that lands elsewhere moves the
    # generation, including that one, so comparing it is what binds the disposition
    # to the path it was taken for.
    refused: list[RepoUnreadable] = []

    async def _clear() -> str | None:
        try:
            if not _still_same_checkout(captured):
                # This read is aimed at the path captured above, and the app has
                # since resolved a different one. Spawning would run git inside
                # the checkout being left behind -- under a disposition that
                # describes neither it nor the one now configured.
                raise RepoUnreadable(
                    f"refused: the configured checkout changed while reading "
                    f"{git_dir}, so the read was not taken. The next refresh "
                    f"reads the checkout now configured."
                )
            if read_only:
                await _assert_read_cleared(git_dir, read_only=read_only)
        except RepoUnreadable as exc:
            # Kept, not just reported: _run_cmd turns a pre_spawn refusal into a
            # failed read, and a failed read is indistinguishable from a git
            # error -- so the reason would stop reaching the banner.
            refused.append(exc)
            return str(exc)
        return None

    rc, stdout, stderr = await runtime._run_cmd(
        ["git", "-C", git_dir, *args],
        timeout=timeout,
        mode=mode,
        pre_spawn=_clear,
        max_output_bytes=max_output_bytes,
        # Asked for on the foreign path only. This product's own checkout is the
        # pre-existing trust boundary and its reads legitimately use the operator's
        # credentials; a foreign checkout's do not, so the stores are masked where the
        # sandbox can enforce it. Requested, not guaranteed: on a host where the mask
        # does not apply these are dropped unread, which is why the clearance above
        # still runs and why the verb safelist is still the structural bound.
        extra_hidden_dirs=runtime._credential_store_dirs() if read_only else (),
    )
    if refused:
        raise refused[0]
    return rc, stdout, stderr


async def _run_gated_git_soft(
    git_dir: str, *args: str, timeout: int = 6, generation: int | None = None
) -> tuple[int, str, str]:
    """The gated spawn for a caller whose contract is a DEGRADED answer, not an error.

    The resolvers are that caller: each already treats a non-zero rc as "could not
    resolve" and falls back to a documented default -- the configured base branch,
    ``origin`` for the upstream remote -- so a clearance refusal is reported the same
    way rather than raised. Without this they spawned bare, at the default tier and with
    no clearance, which is how a foreign repository's config got parsed by a `git
    remote` while the sandbox was still being prepared.
    """
    try:
        return await _run_gated_git(git_dir, *args, timeout=timeout, generation=generation)
    except (ConvertingVerbRefused, RepoUnreadable) as exc:
        return -1, "", str(exc)


async def _git(
    git_dir: str,
    *args: str,
    timeout: int = 6,
    mode: str = "standard",
    generation: int | None = None,
    max_output_bytes: int | None = None,
) -> str | None:
    """One gated read, as stripped stdout or None when it was not measured."""
    try:
        rc, stdout, _ = await _run_gated_git(
            git_dir,
            *args,
            timeout=timeout,
            mode=mode,
            generation=generation,
            max_output_bytes=max_output_bytes,
        )
    except ConvertingVerbRefused:
        # The refusal is a declined measurement, and this signature has reported that
        # as None since before the gated spawn was split out of it.
        return None
    return stdout.strip() if rc == 0 else None


async def _git_info(path: str, *, generation: int | None = None) -> dict:
    info: dict = {
        "branch": None,
        "head": None,
        "head_oid": None,
        # None until a `status` actually answers. False would assert this tree is
        # clean on a measurement nobody took -- which is the state a checkout this
        # app does not own is permanently in, since `status` converts content and is
        # not run there at all.
        "dirty": None,
        "ahead": 0,
        "behind": 0,
        "last_updated_at": None,
    }
    info["branch"] = await _git(path, "rev-parse", "--abbrev-ref", "HEAD", generation=generation)
    full_head = await _git(path, "rev-parse", "HEAD", generation=generation)
    info["head_oid"] = full_head
    info["head"] = full_head[:7] if full_head else None
    st = await _git(path, "status", "--porcelain", generation=generation)
    if st is not None:
        info["dirty"] = len(st) > 0
    remote = await _upstream_remote()
    behind = await _git(
        path, "rev-list", "--count", f"HEAD..{remote}/{BASE_BRANCH}", generation=generation
    )
    if behind and behind.isdigit():
        info["behind"] = int(behind)
    ct = await _git(path, "log", "-1", "--format=%ct", generation=generation)
    if ct and ct.isdigit():
        info["last_updated_at"] = int(ct)
    return info


async def _git_ahead(path: str, *, generation: int | None = None) -> int | None:
    """Patch-unique local commits via git cherry."""
    remote = await _upstream_remote()
    ch = await _git(
        path, "cherry", f"{remote}/{BASE_BRANCH}", "HEAD", timeout=12, generation=generation
    )
    if ch is not None:
        return sum(1 for ln in ch.splitlines() if ln.startswith("+"))
    ar = await _git(
        path, "rev-list", "--count", f"{remote}/{BASE_BRANCH}..HEAD", generation=generation
    )
    return int(ar) if ar and ar.isdigit() else None


async def _own_commits_count(path: str, *, generation: int | None = None) -> int | None:
    remote = await _upstream_remote()
    out = await _git(
        path, "rev-list", "--count", f"{remote}/{BASE_BRANCH}..HEAD", generation=generation
    )
    return int(out) if out and out.isdigit() else None


def _is_directory_at(name: str, dir_fd: int) -> bool:
    """Whether *name*, resolved relative to the pinned *dir_fd*, is a directory.

    ``lstat`` through the descriptor and without following links, so the answer is
    about the entry the failed ``unlink`` addressed and not about whatever a link
    at that name points to. Any error reads as "not a directory": the caller then
    reports the original failure instead of a guess.
    """
    try:
        return stat.S_ISDIR(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _discard_untracked_files(worktree: str, rel_paths: list[str]) -> str | None:
    """Delete exactly the approved untracked files. None on success, else a reason.

    Deliberately NOT ``git clean``. A pathspec naming an entry whose type changed
    between consent and execution is followed recursively -- verified: an approved
    regular file ``scratch`` replaced by a directory ``scratch/`` containing an
    unapproved file loses that file, with ``-fd`` AND with a bare ``-f``, even
    when the pathspec is spelled ``:(literal)``. ``os.unlink`` cannot do that: it
    removes ONE non-directory entry and raises ``IsADirectoryError`` when the name
    now refers to a directory, so a type change is a refusal rather than a sweep.
    Having no pathspec at all also removes the pathspec-magic surface entirely.

    EVERY component of the given absolute worktree path is opened ``O_NOFOLLOW``
    from ``/`` down, and the unlink is issued relative to that directory fd.
    Opening the worktree by path in one call re-resolves its ancestors, so a
    writable ancestor swapped for a symlink would redirect the deletion before
    the walk began. ``realpath`` is deliberately NOT used first: resolution
    follows the topology as it stands NOW, so it resolves INTO a swapped ancestor
    and lands the deletion in the attacker's target -- tried and verified to
    destroy an external file, which is laundering the swap rather than refusing
    it. The price is that a worktree path containing a legitimately symlinked
    ancestor is refused; that is the same trade as the platform check below.
    Where these primitives do not exist (Windows has no ``openat``/``O_NOFOLLOW``)
    the discard is REFUSED rather than downgraded to a path-based unlink, since a
    junction swapped into an ancestor is not even reported as a link by
    ``os.path.islink``. Empty directories are left behind on purpose -- git does
    not track them, ``status``/``ls-files`` do not report them, and ``git worktree
    remove`` does not object to them (verified), so removing them would be scope
    this consent does not cover.
    """
    if not ({"O_NOFOLLOW"} <= set(dir(os)) and os.unlink in os.supports_dir_fd):
        # No openat/O_NOFOLLOW (Windows). A path-based unlink re-resolves every
        # ancestor at each step, so a directory component swapped for a symlink
        # -- or a Windows junction, which `os.path.islink` does not even report
        # as a link -- redirects the deletion outside the worktree. There is no
        # safe way to do this here, so the affordance is withdrawn rather than
        # approximated: the caller loses a button, not a file.
        return (
            "cannot discard untracked files safely on this platform (no "
            "openat/O_NOFOLLOW, so a swapped directory could redirect the "
            "deletion outside the worktree) -- clean the worktree manually, "
            "then remove it"
        )
    # Walk the GIVEN path from `/`, pinning every component with O_NOFOLLOW.
    # Opening the worktree by path in one call re-resolves its ancestors, so a
    # writable ancestor swapped for a symlink redirects the deletion before the
    # walk starts.
    #
    # Deliberately NOT `realpath` first. That was tried and it DEFEATS the guard:
    # resolution follows whatever the topology says NOW, so a swapped ancestor is
    # resolved into and the deletion lands in the attacker's target -- verified,
    # an external file was destroyed. Resolution launders the swap instead of
    # refusing it.
    #
    # The cost is that a worktree whose path genuinely contains a symlinked
    # ancestor (a linked home directory, macOS /tmp) is refused. That is the same
    # trade as the platform check above: withdraw the affordance and say so,
    # rather than approximate it. git records worktree paths as plain absolute
    # paths, so this is the uncommon case, and the caller can still clean by hand.
    root_parts = PurePosixPath(worktree).parts
    if not root_parts or root_parts[0] != "/":
        return f"refusing to discard inside a non-absolute worktree path: {worktree!r}"

    for rel in rel_paths:
        parts = PurePosixPath(rel).parts
        if (
            not parts
            or any(p in ("", ".", "..") for p in parts)
            or PurePosixPath(rel).is_absolute()
        ):
            return f"refusing to discard a path that is not worktree-relative: {rel!r}"
        dir_fds: list[int] = []
        walked = 0
        try:
            try:
                dir_fds.append(os.open("/", os.O_RDONLY | os.O_DIRECTORY))
                for comp in (*root_parts[1:], *parts[:-1]):
                    dir_fds.append(
                        os.open(
                            comp,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=dir_fds[-1],
                        )
                    )
                    walked += 1
                os.unlink(parts[-1], dir_fd=dir_fds[-1])
            except FileNotFoundError:
                if walked < len(root_parts) - 1:
                    # A component of the WORKTREE path is missing, which is not
                    # the idempotent "file already gone" case below.
                    return (
                        "cannot discard untracked files: the worktree path no "
                        "longer resolves -- nothing was discarded"
                    )
                continue
            except IsADirectoryError:
                return (
                    f"refusing to discard {rel!r}: it is now a directory, not the "
                    "file that was confirmed"
                )
            except PermissionError as exc:
                # Same type change, different errno. Linux answers EISDIR from
                # unlink(2) on a directory; darwin answers EPERM, so the branch
                # above never sees it and the user reads a bare "operation not
                # permitted" that names nothing. Confirmed with a stat before it is
                # reported, so a genuine permission refusal keeps its own message,
                # and only the LEAF can be this case -- an EPERM from the ancestor
                # walk never reached the unlink.
                reached_unlink = walked == len(root_parts) - 1 + len(parts) - 1
                if reached_unlink and _is_dir_at(parts[-1], dir_fds[-1]):
                    return (
                        f"refusing to discard {rel!r}: it is now a directory, not the "
                        "file that was confirmed"
                    )
                return f"could not discard {rel!r}: {exc.strerror or exc}"
            except OSError as exc:
                if exc.errno == errno.EPERM and _is_directory_at(parts[-1], dir_fds[-1]):
                    # macOS and the BSDs answer unlink() on a directory with EPERM,
                    # not Linux's EISDIR, so the type change arrives here instead of
                    # in the clause above. Same refusal: nothing recurses into it.
                    return (
                        f"refusing to discard {rel!r}: it is now a directory, not the "
                        "file that was confirmed"
                    )
                if walked < len(root_parts) - 1 and exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    # A component of the worktree path is a symlink. Could be a
                    # host whose home directory is linked, could be an ancestor
                    # swapped since git reported the path -- indistinguishable
                    # from here, so both are refused.
                    return (
                        "cannot discard untracked files: a directory in the "
                        "worktree's own path is a symlink, so the deletion "
                        "cannot be pinned to the checkout -- clean the worktree "
                        "manually, then remove it"
                    )
                return f"could not discard {rel!r}: {exc.strerror or exc}"
        finally:
            for fd in dir_fds:
                try:
                    os.close(fd)
                except OSError:  # pragma: no cover - defensive
                    pass
    return None


def _count_missing(worktree: str, rel_paths: list[str]) -> int:
    """How many of the approved paths are absent -- i.e. how many the discard
    deleted before it was refused. Read-only (``lstat``, never follows the
    leaf) and consulted only so an incomplete-discard refusal says what is gone;
    it takes no decision, so it needs none of the helper's fd pinning."""
    gone = 0
    for rel in rel_paths:
        try:
            os.lstat(os.path.join(worktree, rel))
        except OSError:
            gone += 1
    return gone


def _is_dir_at(name: str, dir_fd: int) -> bool:
    """Whether *name* under *dir_fd* is a real directory right now.

    Never raises: the caller is already handling a refusal and only needs to know
    which refusal to report.
    """
    try:
        return stat.S_ISDIR(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


async def _real_dirty(path: str, *, generation: int | None = None) -> bool | None:
    st = await _git(path, "status", "--porcelain", generation=generation)
    if st is None:
        return None
    return any(ln.strip() for ln in st.splitlines())


# Bound on the untracked paths reported to the client. The list exists so a
# human can see what a discard would destroy; past a couple of dozen entries it
# stops informing that decision and only grows the payload.
_DIRTY_PATH_SAMPLE = 20


async def _dirty_split(
    path: str, *, generation: int | None = None
) -> tuple[bool | None, list[str]]:
    """Classify a worktree's dirt: tracked modifications vs untracked files.

    Returns ``(tracked_dirty, untracked_paths)``.

    * ``tracked_dirty`` is True when at least one TRACKED file is modified,
      staged, deleted, renamed or unmerged, False when none is, and ``None``
      when git could not answer — which callers must treat as unverifiable,
      never as clean.
    * ``untracked_paths`` are files git considers untracked and NOT ignored, so
      build output (``.venv``, ``node_modules``, anything in ``.gitignore``)
      never counts as dirt. An empty list means "none found OR git failed" — it
      is deliberately not a promise, and the discard path treats an empty list
      as "nothing approved to discard".

    Why two commands instead of parsing one ``--porcelain`` blob: ``-uno``
    suppresses untracked entries, so anything it prints is a tracked change and
    a plain non-empty test suffices; ``ls-files --others`` prints bare paths
    with no status columns to misparse.

    The untracked half deliberately bypasses the shared ``_git`` helper, which
    strips its output and would corrupt a first or last filename carrying
    leading or trailing whitespace. These paths are not merely displayed — they
    become the ``git clean`` pathspec deciding which files a discard destroys —
    so they must survive byte-exact. A corrupted path would simply fail to
    match and abort the removal, which is safe but is a refusal nobody earned.
    """
    tracked_out = await _git(path, "status", "--porcelain", "-uno", generation=generation)
    tracked_dirty: bool | None = (
        None if tracked_out is None else any(ln.strip() for ln in tracked_out.splitlines())
    )
    rc, others_raw, _ = await _run_gated_git_soft(
        path,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        timeout=6,
        generation=generation,
    )
    untracked = [p for p in others_raw.split("\0") if p] if rc == 0 else []
    return tracked_dirty, untracked


def _dirt_fields(tracked_dirty: bool | None, untracked: list[str]) -> dict:
    """The structured dirt description carried on a refusal or a fleet row.

    Kept separate from the human message so the client can RENDER the blocking
    files instead of parsing a sentence — a refusal that only says "uncommitted
    changes" leaves the user no way to find out what is in the way.

    Emitted paths go through ``_redact``, like every other path-ish string this
    module puts on the wire (the worktree path, the design-doc list). A filename
    is author-controlled text, so it is scrubbed on the way OUT while callers
    that need to act on the file keep the raw list from ``_dirty_split``.
    """
    return {
        "dirty_tracked": tracked_dirty,
        "dirty_untracked": len(untracked),
        "dirty_untracked_paths": [runtime._redact(p) for p in untracked[:_DIRTY_PATH_SAMPLE]],
    }


def _dirt_detail(tracked_dirty: bool | None, untracked: list[str]) -> str:
    """A short phrase naming what is dirty, appended to a refusal message.

    For callers that surface only the error string (the prune checklist's
    inline failure reason), this is the whole explanation they get, so it says
    which KIND of dirt is blocking. It deliberately never suggests forcing:
    force is refused for tracked modifications too.
    """
    if tracked_dirty is None:
        return ""
    parts = []
    if tracked_dirty:
        parts.append("tracked files are modified")
    if untracked:
        shown = ", ".join(runtime._redact(p) for p in untracked[:3])
        more = f" +{len(untracked) - 3} more" if len(untracked) > 3 else ""
        parts.append(f"{len(untracked)} untracked ({shown}{more})")
    if not parts:
        return ""
    return " -- " + "; ".join(parts)


async def _dirt_report(path: str) -> tuple[dict, str]:
    """Classify a dirty worktree for a refusal payload: fields + message tail."""
    tracked_dirty, untracked = await _dirty_split(path)
    return (
        _dirt_fields(tracked_dirty, untracked),
        _dirt_detail(tracked_dirty, untracked),
    )


def _find_worktree_sync(worktrees: list[dict], name: str) -> tuple[dict | None, str | None]:
    """Resolve a worktree by display name, rejecting ambiguous basenames."""
    matches = []
    for w in worktrees:
        wname = Path(w["path"]).name if not w.get("is_main") else BASE_BRANCH
        if wname == name:
            matches.append(w)
    if not matches:
        return None, f"worktree not found: {name}"
    if len(matches) > 1:
        paths = ", ".join(w["path"] for w in matches)
        return None, f"ambiguous worktree name {name!r} matches multiple checkouts: {paths}"
    return matches[0], None


async def _find_worktree(name: str) -> tuple[dict | None, str | None]:
    wts = await _discover_worktrees()
    return _find_worktree_sync(wts, name)


async def _find_retained_worktree_path(name: str) -> tuple[str | None, str | None]:
    """Find a non-main worktree record, including a prunable checkout."""
    matches = [
        worktree
        for worktree in await _worktree_porcelain_entries()
        if not worktree.get("is_main") and Path(worktree["path"]).name == name
    ]
    if not matches:
        return None, f"worktree not found: {name}"
    if len(matches) > 1:
        paths = ", ".join(worktree["path"] for worktree in matches)
        return None, f"ambiguous worktree name {name!r} matches multiple checkouts: {paths}"
    return matches[0]["path"], None


async def _valid_worktree_names() -> set[str]:
    return {
        Path(w["path"]).name if not w.get("is_main") else BASE_BRANCH
        for w in await _discover_worktrees()
    }


async def _find_worktree_by_path(path: str) -> tuple[dict | None, str | None]:
    """Resolve a discovered worktree by filesystem path.

    Reuses the same ``git worktree list`` enumeration the fleet listing uses,
    so the caller-supplied path is only ever a SELECTOR validated against the
    server's authoritative set — an arbitrary path can never be made live."""
    if not path:
        return None, "'path' must be a non-empty string"
    # Explicit on every platform: POSIX ``realpath`` raises ValueError on an
    # embedded NUL, but Windows' swallows it and resolves the string anyway,
    # which would send garbage on to the enumeration instead of refusing it.
    if "\x00" in path:
        return None, f"invalid path: {path!r}"
    worktrees = await _discover_worktrees()

    def _select() -> tuple[dict | None, str | None]:
        # ``resolve()`` walks the filesystem for the selector and for every
        # discovered worktree; this runs on the gateway's loop, so it hops out.
        try:
            want = Path(path).resolve()
        except (OSError, ValueError, RuntimeError):
            return None, f"invalid path: {path!r}"
        for w in worktrees:
            try:
                if Path(w["path"]).resolve() == want:
                    return w, None
            except OSError:
                continue
        return None, None

    found, err = await asyncio.get_running_loop().run_in_executor(subprocess_executor(), _select)
    if found is not None or err is not None:
        return found, err
    return None, f"path is not a known worktree: {path!r}"


__all__ = (
    "BASE_BRANCH",
    "MAIN_REPO",
    "MAIN_REPO_INFERRED",
    "RepoNotConfigured",
    "RepoReadOnly",
    "RepoUnavailable",
    "RepoUnreadable",
    "_BASE_BRANCH_POSITIVE",
    "_BASE_BRANCH_RE",
    "_CHECKOUT_DIR_NAMES",
    "_CHECKOUT_PARENT_DIRS",
    "_DIRTY_PATH_SAMPLE",
    "_FALLBACK_REPOS",
    "_LATCHED_CONFIGURED",
    "_LOCAL_BASE_CANDIDATES",
    "_REPO_INVALID_MSG",
    "_REPO_PATH_RE",
    "_REPO_READ_ONLY_MSG",
    "_UPSTREAM_REMOTE",
    "_candidate_checkouts",
    "_configured_main_repo",
    "_configured_main_repo_checked",
    "_default_main_repo",
    "_default_main_repo_state",
    "_dirt_detail",
    "_dirt_fields",
    "_dirt_report",
    "_dirty_split",
    "_discard_untracked_files",
    "_discover_main_repo",
    "ensure_main_repo_discovered",
    "_discover_worktrees",
    "_find_retained_worktree_path",
    "_find_worktree",
    "_find_worktree_by_path",
    "_find_worktree_sync",
    "_git",
    "_git_ahead",
    "_git_info",
    "_invalid_resolution_is_stale",
    "_is_git_checkout",
    "_is_kirocrew_checkout",
    "_load_dev_fleet_cfg",
    "_load_dev_fleet_cfg_checked",
    "_load_fallback_repos",
    "_load_trusted_credential_helpers",
    "_matching_child_dirs",
    "_normalize_repo_identity",
    "_own_commits_count",
    "_own_source_checkout",
    "_parse_worktree_porcelain",
    "_plausible_branch_name",
    "_read_only_reason",
    "_real_dirty",
    "_repo",
    "_repo_read",
    "_repo_source_hint",
    "_resolve_base_branch",
    "base_branch_mutation_refusal",
    "_resolve_primary_checkout",
    "_same_path",
    "_upstream_remote",
    "_valid_worktree_names",
)
