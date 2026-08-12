"""The allow-list barrier every worktree endpoint applies to a caller's ``repo``.

A dashboard caller names a repository by path. Without a barrier, any
authenticated caller could name an arbitrary host directory and have git run
against it (CodeQL: "uncontrolled data used in path expression"). The barrier is
the set of directories a chat slot is already bound to: each slot's project
(realpathed and screened when it was set), and the main checkout a worktree-bound
slot is labelled with. The frontend only ever sends one of those, so constraining
to that set costs the feature nothing.

The two halves do NOT have the same provenance, and
:func:`allowed_repo_roots` spells out why the weaker half is still sound: a
slot's project is server-screened, while its worktree ``repo`` label is a
shape-checked caller string. Read that docstring before leaning on this barrier
for a stronger property than it has.

:func:`match_allowed_root` returns the value FROM the allow-list, never the
caller's string, so every filesystem operation downstream runs on a path the
server chose.
"""

from __future__ import annotations

import os


def allowed_repo_roots(state: object) -> list[str]:
    """Realpath'd directories the caller may name: every slot's project, plus the
    main checkout each worktree-bound slot belongs to.

    Slot projects are set through ``/api/chat/slots/{slot}/project``, which
    realpaths and sensitive-path-screens them. ``slot.worktree.repo`` reaches the
    same endpoint in the request body and is only SHAPE-checked on the way in
    (string, length-capped, ``path`` equal to the project being set) — nothing
    verifies it is the main checkout that tree actually belongs to. So this half
    of the barrier does carry a caller-supplied string, and the docstring must not
    claim otherwise.

    That is acceptable rather than a hole, for two reasons that both have to hold:
    the writer is the same trusted dashboard caller who can already name any
    non-sensitive directory as a slot project, so admitting a repo string raises
    no ceiling it did not already have; and ``is_sensitive_path`` is re-applied at
    operation time to both the resolved repo root and the git toplevel, so the
    sensitive-path control sits where git actually runs rather than here. The
    barrier's job is to keep git off arbitrary UNRELATED host paths, not to defend
    against a hostile local operator.

    The repo half is what keeps a session usable after it enters a worktree: its
    ``project`` is then the worktree, so the repository it belongs to would fall
    outside the barrier and the session could no longer list, enter or leave its
    own trees.
    """
    roots: list[str] = []

    def add(raw: object) -> None:
        candidate = str(raw or "").strip()
        if not candidate:
            return
        resolved = os.path.realpath(candidate)
        if os.path.isdir(resolved) and resolved not in roots:
            roots.append(resolved)

    slots = getattr(state, "_slots", None) or {}
    for slot in list(getattr(slots, "values", list)()):
        add(getattr(slot, "project", ""))
        binding = getattr(slot, "worktree", None)
        if isinstance(binding, dict):
            add(binding.get("repo"))
    return roots


def match_allowed_root(candidate: str, roots: list[str]) -> str | None:
    """Return the allow-listed root that ``candidate`` names or sits inside.

    ``candidate`` must be normalized by the caller. Comparison goes through
    ``os.path.normcase`` because Windows paths are case-insensitive and
    ``realpath`` does not reliably canonicalize case there — without it a
    differently-cased but identical path would be refused. The prefix test is
    ``os.sep``-terminated, so ``/repo-evil`` does not pass as inside ``/repo``.
    """
    probe = os.path.normcase(candidate)
    for root in roots:
        normalized = os.path.normcase(root)
        if probe == normalized or probe.startswith(normalized.rstrip(os.sep) + os.sep):
            return root
    return None
