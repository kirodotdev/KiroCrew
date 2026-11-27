"""Bounded reads of the git metadata inside an existing app checkout.

The files read here (``.git/HEAD``, loose refs, ``packed-refs``) live in a checkout
an app's build script can rewrite, so every read is size-bounded and goes through
the sensitive-path gate. The checkout and manifest owners import from here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

# Upper bound on any single git-metadata read of a file INSIDE a checkout
# (``.git/HEAD``, a loose ref). Those files are agent-writable — an app's own
# build script can rewrite them — so their size is ATTACKER-controlled: an app
# can replace one with (or symlink it to) a multi-gigabyte / sparse file, and
# an unbounded read would load it into gateway memory. A well-formed value here
# is a single line (a ``ref:`` line or a 40/64-char SHA), tens of bytes, so a
# few-hundred-byte cap makes the read a no-op for hostile content while a real
# ref fits comfortably. This bound is applied at EVERY checkout-resident git
# read, not just one call site — the round-11 lesson is that gating a single
# caller leaves the primitive exploitable from the next one.
_HEAD_READ_LIMIT = 512  # bytes


# Upper bound on the ``.git/packed-refs`` read. It is line-oriented (one ref per
# line) so it can legitimately be larger than a single ref file, but it is still
# agent-writable checkout content, so the read is capped rather than unbounded.
# A shallow single-branch app clone packs a handful of refs; this ceiling covers
# a realistic repo while still refusing a hostile multi-megabyte replacement.
_PACKED_REFS_READ_LIMIT = 1 << 20  # 1 MiB


def _read_git_metadata_bounded(path: Path, limit: int) -> str | None:
    """Read at most *limit* bytes of a git-metadata file, or None on failure.

    The read is BOUNDED because *path* lives inside a checkout whose contents an
    app's build script can rewrite (see :data:`_HEAD_READ_LIMIT`): reading a
    ref file whole would let an oversized/sparse/symlinked replacement exhaust
    gateway memory. Content that exactly fills the bound is treated as
    truncated/hostile and returns None, so a caller never acts on a partial
    token. Missing file, unreadable, or non-UTF-8 all fail closed to None.

    The read is also SYMLINK-CONTAINED: *path* is a checkout-resident file whose
    name (``.git/HEAD``, a loose ref, ``packed-refs``) an app's build script can
    replace with a symlink pointing at a protected file (``~/.aws/credentials``,
    an SSH key). A bare ``open()`` would follow that link and read the target
    through the sensitive-path ceiling, so the read is routed through
    :func:`kiro_crew.hooks.safe_read_prefix`, which canonicalizes via ``realpath``
    and refuses a resolved target ``is_sensitive_path`` flags before any read,
    then opens the canonical path ``O_NOFOLLOW`` as TOCTOU defense against a
    final-component symlink swap. A rejected (or unreadable) path fails closed to
    None, so the size bound and the containment gate share one fail-closed exit.
    """
    from kiro_crew import hooks

    raw = hooks.safe_read_prefix(str(path), limit)
    if raw is None:
        # Rejected by the sensitive-path gate, a followed symlink refused
        # O_NOFOLLOW, missing, or otherwise unreadable — all fail closed.
        return None
    try:
        data = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Filling the bound means the real content was larger — refuse rather than
    # act on a value that may have been cut mid-token. Measured on the decoded
    # text so a multi-byte tail cannot slip a value past the bound.
    if len(data) >= limit:
        return None
    return data


def _read_clone_branch(clone_dir: Path) -> str | None:
    """Read the current branch of an existing git clone.

    Returns the branch name (e.g. ``"main"``), or None if the clone does not
    exist, is in detached HEAD state, or the branch cannot be determined.
    Reads ``.git/HEAD`` directly (stdlib-only, no subprocess spawn) — mirrors
    the fail-closed posture of :func:`_clone_origin_matches`.

    The read is BOUNDED to :data:`_HEAD_READ_LIMIT` bytes. ``.git/HEAD`` lives
    inside a checkout an app's build script can rewrite, so its size is
    attacker-controlled; reading it whole would let a multi-gigabyte or sparse
    replacement exhaust gateway memory. A well-formed HEAD fits in a few
    hundred bytes, so a ``ref:`` line that does not resolve within the bound is
    treated as malformed and fails closed (returns None). This is what closes
    the memory-exhaustion class at EVERY call site, not just the ones a caller
    happens to gate.

    A ``.git`` that is a *file* (worktree / submodule gitfile) rather than a
    directory also fails closed (``is_file()`` on the nested path returns
    False), so no fast path is attempted for those layouts.
    """
    head_file = clone_dir / ".git" / "HEAD"
    if not head_file.is_file():
        return None
    raw = _read_git_metadata_bounded(head_file, _HEAD_READ_LIMIT)
    if raw is None:
        # Missing, unreadable, non-UTF-8, or oversized (filled the bound) — the
        # bounded reader already failed closed on hostile/truncated content.
        return None
    head_content = raw.strip()
    # A normal branch checkout has HEAD = "ref: refs/heads/<branch>"
    _REF_PREFIX = "ref: refs/heads/"
    if head_content.startswith(_REF_PREFIX):
        branch = head_content[len(_REF_PREFIX) :]
        if not branch:
            return None
        return branch
    # Detached HEAD (raw SHA) or unexpected format — fail closed.
    return None


async def _clone_branch_matches(dest: Path, branch: str) -> bool:
    """Whether *dest* has *branch* checked out (exact string equality).

    Fails closed: an unreadable or detached HEAD, a missing ``.git/HEAD``,
    or an empty *branch* to compare against all return False — the caller
    must fall through to the throwaway clone so admission sees the correct
    branch's manifest.
    """
    if not branch:
        return False
    clone_branch = await asyncio.to_thread(_read_clone_branch, dest)
    return clone_branch == branch
