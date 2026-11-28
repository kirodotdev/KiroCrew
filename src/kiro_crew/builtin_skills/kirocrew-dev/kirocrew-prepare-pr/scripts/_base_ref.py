"""How every kirocrew-prepare-pr script reads the base it is given.

A script is handed its base as ``main``, ``origin/main`` or ``refs/remotes/origin/main``,
and all three mean origin's ``main``. git reads the two short ones by gitrevisions(7)
order instead: a bare ``main`` is the local branch, which can sit weeks behind its
remote, and ``origin/main`` is a local branch, tag or other ref of that name before it
is ``refs/remotes/origin/main``. So a script reads its base here, in one of two shapes:

* :func:`base_branch` is the branch name, for a script that fetches the base or builds
  its remote ref itself.
* :func:`base_revision` is the revision to hand git: ``refs/remotes/origin/main`` for
  all three spellings.

Stdlib only, and loaded by path (the scripts directory is not a package).
"""

from collections.abc import Callable

ORIGIN_REFS = "refs/remotes/origin/"


def base_branch(base: str) -> str:
    """The branch ``base`` names: ``origin/main`` and ``refs/remotes/origin/main`` are ``main``."""
    for prefix in (ORIGIN_REFS, "origin/"):
        if base.startswith(prefix):
            return base[len(prefix) :]
    return base


def base_revision(base: str, git_status: Callable[[list[str]], int]) -> str:
    """The revision ``base`` names, with a branch of origin's spelled in full.

    ``git_status(args)`` runs ``git <args>`` in the repository and returns its exit status.

    A base spelled with origin's prefix is origin's copy whether or not this clone has
    fetched it, so a missing one fails in git instead of reading a namesake, and
    ``origin/main~1`` is ``refs/remotes/origin/main~1``. A bare name is origin's copy
    only when origin holds a branch of exactly that name: ``show-ref --verify`` matches
    one full ref and evaluates no ``~`` or ``^``. So a commit, a ``~`` or ``^`` revision,
    ``refs/heads/<x>``, another remote's branch and a local branch not pushed yet are
    used as given, and so is ``HEAD``: a clone's ``refs/remotes/origin/HEAD`` names the
    remote's default branch, not this one.
    """
    branch = base_branch(base)
    if branch != base:
        return ORIGIN_REFS + branch
    if not base or base == "HEAD":
        return base
    remote = ORIGIN_REFS + base
    return remote if git_status(["show-ref", "--verify", "--quiet", remote]) == 0 else base
