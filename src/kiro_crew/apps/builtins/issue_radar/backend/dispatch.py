"""Issue Radar — dispatch readiness (RFC phase 0).

Issue Radar reads everything through the user's own provider CLI and needs no
local clone. Asking an agent to *implement* an issue does need one, so a
connected repo carries an optional local checkout path and dispatch is gated on
it.

This module owns two things and no I/O beyond stat:

* :func:`resolve_checkout` — validate a user-supplied path, or refuse it.
* :func:`readiness` — turn a stored path into a ready flag plus a reason a UI
  can render without re-deriving the rule.

Nothing here runs an agent or touches git. The gate exists first, on its own, so
that the phase which does run an agent has no judgement left to make.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from kiro_crew.security import is_sensitive_path

    _HAS_SECURITY = True
except Exception:  # pragma: no cover - security module always present in prod
    _HAS_SECURITY = False

    def is_sensitive_path(path: str) -> bool:  # type: ignore[misc]
        """Fail CLOSED when the security module is unavailable.

        This decides whether an agent may be pointed at a directory. Without the
        module we cannot make that judgement, so refuse every path rather than
        admit all of them.
        """
        return True


#: Dispatch can proceed as far as this gate is concerned.
REASON_OK = "ok"
#: The repo has no local checkout recorded yet.
REASON_NO_LOCAL_PATH = "no_local_path"
#: A path is recorded but no longer validates (moved, deleted, or no longer a
#: git checkout). Deliberately distinct from :data:`REASON_NO_LOCAL_PATH`: one
#: asks the user to set a value, the other tells them the value they set broke.
REASON_CHECKOUT_UNUSABLE = "checkout_unusable"


def resolve_checkout(raw: str) -> Path | None:
    """Return *raw* as a usable git work-tree root, or ``None`` if it is not one.

    The rules, in order, each of which has a reason:

    * ``~`` expanded and symlinks resolved BEFORE the sensitivity test, so a
      symlink planted in a benign directory cannot smuggle its target past it.
    * A value the OS cannot resolve at all (an embedded NUL, plus the
      platform-specific shapes that raise ``OSError``) is refused rather than
      raised. Every other unusable value returns ``None`` here, and a caller made
      to catch one exception separately will eventually forget.
    * Must be absolute, asserted on the EXPANDED INPUT and before ``realpath``.
      ``realpath`` resolves a relative value against the gateway's own cwd and
      always returns an absolute path, so testing afterwards can never fail and
      the guarantee would be vacuous.
    * Must not be a sensitive path (credential stores, ``.ssh``, ``.aws``, the
      governance policy files) per :func:`kiro_crew.security.is_sensitive_path`.
    * Must be an existing directory holding a USABLE ``.git`` (see
      :func:`_is_work_tree`). ``.git`` may be a directory (an ordinary clone) or a
      FILE (a linked worktree's ``gitdir:`` pointer), and both are validated
      positively rather than by existence alone: an empty ``.git`` directory and a
      pointer whose target is gone both ``exists()`` while being unusable. A bare
      repository has no work tree to edit and is refused.
    """
    if not raw or not raw.strip():
        return None
    expanded = os.path.expanduser(raw.strip())
    if not os.path.isabs(expanded):
        return None
    try:
        resolved = Path(os.path.realpath(expanded))
    except (ValueError, OSError):
        # An embedded NUL raises ValueError, and some platform-specific shapes
        # raise OSError. Both mean "not a usable path", which is what this
        # function already returns None for. Letting them propagate turns a bad
        # request into a 500.
        return None
    if is_sensitive_path(str(resolved)):
        return None
    if not resolved.is_dir():
        return None
    if not _is_work_tree(resolved):
        return None
    return resolved


def _is_work_tree(root: Path) -> bool:
    """Whether *root* holds a git work tree Phase 1 could actually add a worktree to.

    Existence of a ``.git`` ENTRY is not the same question. An empty ``.git``
    directory, or a linked worktree's pointer file whose target has since been
    deleted, both satisfy ``.exists()`` while being unusable — and reporting
    ``ready`` for one is the same defect as rendering a check that never ran as a
    check that passed. So the markers are checked positively instead.

    Deliberately filesystem-only, no ``git`` subprocess: this runs on every
    readiness read, a subprocess per read is a cost the answer does not need, and
    a missing/again-unusable git binary would turn "the path is fine" into "we
    could not tell". A tree that satisfies these markers and still fails
    ``worktree add`` fails loudly at dispatch time, which is the right place for
    a git-level error.
    """
    dot_git = root / ".git"
    if dot_git.is_file():
        # Linked worktree: ``gitdir: <path>`` pointing at the real repo's
        # per-worktree admin dir. A pointer whose target is gone is dangling.
        try:
            text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return False
        if not text.startswith("gitdir:"):
            return False
        target = text[len("gitdir:") :].strip()
        if not target:
            return False
        if not os.path.isabs(target):
            target = os.path.join(str(root), target)
        # A directory is not enough: the pointer can name any directory, and
        # accepting one would report a non-repository as ready. Require the admin
        # markers git actually puts there -- ``HEAD`` for a worktree admin dir,
        # or ``commondir`` pointing at the shared repo.
        return _has_git_admin_markers(Path(target))
    if not dot_git.is_dir():
        return False
    return _has_git_admin_markers(dot_git)


def _has_git_admin_markers(admin: Path) -> bool:
    """Whether *admin* is a git admin directory rather than an arbitrary one.

    ``HEAD`` plus an object and ref store is the ordinary-clone shape.
    ``objects``/``refs`` can legitimately live elsewhere, spelled by a
    ``commondir`` file, so that is accepted as the alternative -- but the
    relocation target is then checked too, because a ``commondir`` naming a
    directory with no object store describes a repository that is not there.
    """
    if not admin.is_dir():
        return False
    if not (admin / "HEAD").is_file():
        return False
    common = admin / "commondir"
    if common.is_file():
        try:
            rel = common.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return False
        if not rel:
            return False
        shared = Path(rel) if os.path.isabs(rel) else Path(os.path.join(str(admin), rel))
        return (shared / "objects").is_dir() and (shared / "refs").is_dir()
    return (admin / "objects").is_dir() and (admin / "refs").is_dir()


def readiness(local_path: str | None) -> tuple[bool, str]:
    """Whether dispatch may proceed for a repo whose stored path is *local_path*.

    Re-validates on every read rather than trusting the stored value: a checkout
    that was deleted or moved after being recorded must not keep reporting ready,
    for the same reason a check that never ran must not render as a check that
    passed.
    """
    if not local_path or not str(local_path).strip():
        return False, REASON_NO_LOCAL_PATH
    if resolve_checkout(str(local_path)) is None:
        return False, REASON_CHECKOUT_UNUSABLE
    return True, REASON_OK
