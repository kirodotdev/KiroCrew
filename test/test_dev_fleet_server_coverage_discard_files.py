"""Real-filesystem tests of ``repository._discard_untracked_files``.

This module pins the per-file unlink walk against ``tmp_path``, with no git
repository: it deletes approved files, nested ones included, and leaves an emptied
directory; it refuses a symlinked worktree root or ancestor, a path escape and a
type change; it fails closed where ``openat`` and ``O_NOFOLLOW`` are missing, keeps
a genuine permission refusal verbatim, treats a missing path as done, and deletes a
pathspec-looking name literally. The removal path that calls it is pinned in
``test_dev_fleet_server_coverage_untracked_discard.py``; the base module is
``test_dev_fleet_server_coverage.py``.
"""

from __future__ import annotations

import errno
import os

import pytest
from dev_fleet_server_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _pin_module_state,
)

from kiro_crew.apps.builtins.dev_fleet import repository

# --- Direct tests of _discard_untracked_files against a REAL filesystem. ---
# No mocking, no git repo: the helper is a pure per-file os.unlink walk, so it
# can be exercised against tmp_path. Each maps to a property verified by hand.


# `_discard_untracked_files` refuses outright where openat/O_NOFOLLOW do not
# exist (Windows), because a path-based delete there could follow a swapped
# junction out of the worktree. Its BEHAVIOURAL cases therefore have nothing to
# exercise on such a platform -- the helper never reaches the deletion at all --
# and one of them cannot even be set up, since `:(glob)*` is not a legal Windows
# filename. The refusal itself is what matters there, and
# test_discard_untracked_files_fails_closed_without_openat covers it on every
# platform, so it is deliberately NOT marked.
_fd_safe_discard = hasattr(os, "O_NOFOLLOW") and os.unlink in os.supports_dir_fd
requires_fd_safe_discard = pytest.mark.skipif(
    not _fd_safe_discard,
    reason="_discard_untracked_files refuses without openat/O_NOFOLLOW",
)


def _need_unsymlinked_tmp(tmp_path):
    """Skip when pytest's own tmp_path resolves through a symlink.

    `_discard_untracked_files` refuses ANY symlink in the worktree's path by
    design, so on a runner whose temp root is linked (a linked /tmp, macOS
    /private/tmp) every behavioural case below would get the refusal instead of
    the behaviour it is asserting. That is the helper working, not a defect --
    and the refusal itself is covered by
    test_discard_untracked_files_refuses_a_symlinked_ancestor, which builds its
    own symlink rather than relying on the environment having one.
    """
    if os.path.realpath(str(tmp_path)) != str(tmp_path):
        pytest.skip("pytest tmp_path resolves through a symlink on this runner")


@requires_fd_safe_discard
def test_discard_untracked_files_refuses_a_symlinked_worktree_root(tmp_path):
    """ROOT SWAP: if the worktree root itself is a symlink, the discard refuses.

    Opening the root without O_NOFOLLOW would make the careful per-component
    walk pointless -- the very first open would already have left the checkout,
    and matching relative paths inside the symlink's target would be unlinked.
    """
    _need_unsymlinked_tmp(tmp_path)
    real = tmp_path / "elsewhere"
    real.mkdir()
    victim = real / "probe.py"
    victim.write_text("outside the worktree")
    as_link = tmp_path / "worktree-link"
    as_link.symlink_to(real, target_is_directory=True)

    reason = repository._discard_untracked_files(str(as_link), ["probe.py"])
    assert reason is not None
    assert "is a symlink" in reason
    assert victim.exists(), "followed a symlinked worktree root and deleted an external file"


@requires_fd_safe_discard
def test_discard_untracked_files_refuses_a_symlinked_ancestor(tmp_path):
    """ANCESTOR SWAP: a symlink ANYWHERE in the worktree's own path is refused.

    An earlier revision resolved the path first so that a legitimately linked
    ancestor would still work. That was wrong and a probe proved it: `realpath`
    follows the topology as it stands NOW, so it resolved INTO a swapped ancestor
    and deleted a file outside the checkout. Resolution launders the swap rather
    than refusing it, and from inside this helper a legitimate link and a
    malicious one are indistinguishable.

    So both are refused, and the documented cost is that a host whose worktree
    path contains a linked directory cannot use the discard and must clean by
    hand -- the same trade as the platform check for Windows.
    """
    _need_unsymlinked_tmp(tmp_path)
    real = tmp_path / "realparent"
    (real / "wt").mkdir(parents=True)
    kept = real / "wt" / "probe.py"
    kept.write_text("x")
    link_parent = tmp_path / "link-parent"
    link_parent.symlink_to(real, target_is_directory=True)

    reason = repository._discard_untracked_files(str(link_parent / "wt"), ["probe.py"])
    assert reason is not None
    assert "is a symlink" in reason
    assert kept.exists(), "deleted through a symlinked ancestor instead of refusing"


@requires_fd_safe_discard
def test_discard_untracked_files_plain_path_still_deletes(tmp_path):
    """The control: with no symlink anywhere in the path, the walk from `/` pins
    every component and the approved files are removed, including a nested one."""
    _need_unsymlinked_tmp(tmp_path)
    wt = tmp_path / "wt"
    (wt / "sub").mkdir(parents=True)
    (wt / "probe.py").write_text("x")
    (wt / "sub" / "harness.py").write_text("y")

    reason = repository._discard_untracked_files(str(wt), ["probe.py", "sub/harness.py"])
    assert reason is None, reason
    assert not (wt / "probe.py").exists()
    assert not (wt / "sub" / "harness.py").exists()


def test_discard_untracked_files_fails_closed_without_openat(tmp_path, monkeypatch):
    """PLATFORM: without openat/O_NOFOLLOW the discard is REFUSED, not downgraded
    to a path-based unlink.

    A path-based delete re-resolves every ancestor, so a directory component
    swapped for a symlink -- or a Windows junction, which `os.path.islink` does
    not report as a link at all -- would redirect the deletion outside the
    worktree. Withdrawing the affordance costs a button; approximating it costs
    a file that was never in the worktree.
    """
    victim = tmp_path / "probe.py"
    victim.write_text("x")
    # Simulate a platform whose unlink cannot take a dir_fd.
    monkeypatch.setattr(os, "supports_dir_fd", set())

    reason = repository._discard_untracked_files(str(tmp_path), ["probe.py"])
    assert reason is not None
    assert "cannot discard untracked files safely on this platform" in reason
    # and it refused BEFORE touching anything
    assert victim.exists(), "a file was deleted on a platform we cannot delete safely on"


@requires_fd_safe_discard
def test_discard_untracked_files_type_change_refuses_and_keeps_unapproved(tmp_path):
    """TYPE CHANGE regression guard (the finding that forced the rewrite): the
    approved name is now a DIRECTORY holding an unapproved file. os.unlink
    raises IsADirectoryError, so the helper REFUSES -- it names the type change
    and does NOT recurse into the directory, so the unapproved file survives."""
    _need_unsymlinked_tmp(tmp_path)
    # `scratch` was approved as a regular file, but between consent and
    # execution it became a directory holding a file nobody approved.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    victim = scratch / "not-approved.txt"
    victim.write_text("keep me")

    reason = repository._discard_untracked_files(str(tmp_path), ["scratch"])

    assert reason is not None
    assert "scratch" in reason
    assert "directory" in reason.lower()
    # The unapproved file inside the swapped-in directory is untouched.
    assert victim.exists()
    assert victim.read_text() == "keep me"


@requires_fd_safe_discard
def test_discard_untracked_files_type_change_is_named_on_an_eperm_platform(tmp_path, monkeypatch):
    """The SAME type change, arriving as the errno darwin uses.

    `unlink(2)` on a directory is EISDIR on Linux and EPERM on darwin, so the
    branch above is unreachable there and the refusal the user reads was a bare
    "operation not permitted" naming nothing. Injecting EPERM runs the darwin
    shape here, so the message stays pinned on every runner rather than only on
    the one that happens to be macOS.
    """
    _need_unsymlinked_tmp(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    victim = scratch / "not-approved.txt"
    victim.write_text("keep me")

    real_unlink = os.unlink

    def _eperm_on_a_directory(name, *a, **kw):
        try:
            return real_unlink(name, *a, **kw)
        except IsADirectoryError:
            raise PermissionError(errno.EPERM, "Operation not permitted") from None

    # The helper's own capability gate reads `os.unlink in os.supports_dir_fd`, so
    # the replacement has to be declared dir_fd-capable or the whole discard is
    # refused as unsupported before any unlink happens.
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {_eperm_on_a_directory})
    monkeypatch.setattr(os, "unlink", _eperm_on_a_directory)
    reason = repository._discard_untracked_files(str(tmp_path), ["scratch"])
    monkeypatch.setattr(os, "unlink", real_unlink)

    assert reason is not None
    assert "scratch" in reason
    assert "directory" in reason.lower()
    assert victim.read_text() == "keep me"


@requires_fd_safe_discard
def test_discard_untracked_files_keeps_a_real_permission_refusal_verbatim(tmp_path, monkeypatch):
    """A genuine EPERM on a FILE is not relabelled as a type change.

    The stat that confirms the type change is what keeps these two apart, so an
    unwritable directory still reports the permission problem it has.
    """
    _need_unsymlinked_tmp(tmp_path)
    (tmp_path / "scratch").write_text("approved")

    def _always_eperm(name, *a, **kw):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {_always_eperm})
    monkeypatch.setattr(os, "unlink", _always_eperm)
    reason = repository._discard_untracked_files(str(tmp_path), ["scratch"])

    assert reason is not None
    assert "scratch" in reason
    assert "directory" not in reason.lower()
    assert "not permitted" in reason.lower()


@requires_fd_safe_discard
def test_discard_untracked_files_deletes_files_but_leaves_emptied_dir(tmp_path):
    """Approved files, including a nested `sub/harness.py`, are deleted; the
    now-empty `sub/` directory is deliberately left behind (git does not track
    empty dirs and `worktree remove` does not object to them)."""
    _need_unsymlinked_tmp(tmp_path)
    (tmp_path / "top.txt").write_text("x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "harness.py").write_text("y")

    reason = repository._discard_untracked_files(str(tmp_path), ["top.txt", "sub/harness.py"])

    assert reason is None
    assert not (tmp_path / "top.txt").exists()
    assert not (sub / "harness.py").exists()
    # The emptied directory is intentionally NOT removed.
    assert sub.is_dir()


@requires_fd_safe_discard
def test_discard_untracked_files_refuses_escapes_and_deletes_nothing(tmp_path):
    """Escape attempts are refused with NOTHING deleted: a `..` traversal, an
    absolute path, and a mid-path `..`. An outside file and an inside sibling
    both survive."""
    _need_unsymlinked_tmp(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    inner = tmp_path / "inside.txt"
    inner.write_text("inside")

    for bad in ("../outside.txt", str(outside), "sub/../../x"):
        reason = repository._discard_untracked_files(str(tmp_path), [bad])
        assert reason is not None, bad
        assert "refusing" in reason.lower(), (bad, reason)

    # Neither the outside target nor the inside sibling was touched.
    assert outside.exists() and outside.read_text() == "outside"
    assert inner.exists() and inner.read_text() == "inside"


@requires_fd_safe_discard
def test_discard_untracked_files_missing_path_is_idempotent(tmp_path):
    """A path that does not exist returns None (idempotent) so a retry after a
    partially-completed discard is not an error."""
    _need_unsymlinked_tmp(tmp_path)
    assert not (tmp_path / "gone.txt").exists()
    reason = repository._discard_untracked_files(str(tmp_path), ["gone.txt"])
    assert reason is None


@requires_fd_safe_discard
def test_discard_untracked_files_magic_filename_is_literal(tmp_path):
    """A filename that looks like pathspec magic (`:(glob)*`) is deleted as a
    LITERAL name -- there is no pathspec for git to interpret it as a pattern --
    while a sibling file that a glob would have swept survives."""
    _need_unsymlinked_tmp(tmp_path)
    magic = tmp_path / ":(glob)*"
    magic.write_text("approved")
    sibling = tmp_path / "sibling.txt"
    sibling.write_text("not approved")

    reason = repository._discard_untracked_files(str(tmp_path), [":(glob)*"])

    assert reason is None
    assert not magic.exists()
    # A real glob would have matched the sibling; a literal unlink does not.
    assert sibling.exists()
    assert sibling.read_text() == "not approved"
