"""The project-scope gate shared by every kind of injected instruction.

Skills and lessons are both instructions that reach a session's prompt, so both
need the same answer to one question: does this entry belong in THIS session?
Keeping the rule here means the two surfaces cannot drift into two different
notions of "in scope" -- a lesson scoped to a repository is admitted under
exactly the conditions a skill scoped to that repository is.

The gate is mechanical rather than prose. A scope guard written into the
instruction text ("ignore this outside repo X") depends on the model choosing to
obey it; this check runs while context is assembled, so an out-of-scope entry is
never rendered into the prompt at all.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.security import is_sensitive_path

__all__ = ["SCOPE_FRAGMENT_RE", "project_scope_satisfied"]

# One segment: no separator, and no ":" so a drive-qualified fragment cannot pass
# (``pathlib`` treats "C:/x" as absolute and would discard the project root). The
# lookahead rejects "." and ".." as whole segments -- "." is the dangerous one,
# because every directory contains it, so a scoped entry carrying it would match
# everywhere and silently become global.
_SEGMENT = r"(?!\.\.?(?:/|$))[^/\\:]+"

# The syntactic half of the scope rule, shared with the write surface so a value
# the gate can never satisfy is refused when it is SAVED rather than stored as a
# lesson that reports success and then applies nowhere. Keeping the pattern here,
# next to the gate that enforces the rest, is what stops the two from drifting;
# ``test_lesson_project_scope`` pins them to the same verdicts.
#
# No leading slash: an absolute path is not a fragment, and reducing one to a
# relative fragment is what let "/etc/passwd" match from the "/" ancestor.
SCOPE_FRAGMENT_RE = re.compile(rf"^{_SEGMENT}(?:/{_SEGMENT})*/?$")


def scope_is_admissible(raw: object) -> bool:
    """Whether the gate could EVER be satisfied by this raw stored scope.

    The gate itself front-gates on this, so the two cannot drift apart. That
    matters because a stored scope the gate can never satisfy is not merely
    useless: it renders nothing while still looking like a real scope to any
    reader that judges it by shape. Such a row must be classified as unusable
    everywhere, or it counts as stored knowledge, suppresses the JSONL store, and
    the lessons a user actually saved stop reaching the prompt.

    Judged on the RAW stored string rather than a canonicalised one, because that
    is what a reader will hand the gate. An imported row bypasses the write
    surface, so it can hold a leading slash that canonicalisation would quietly
    make look admissible while the gate still refuses it.
    """
    if not isinstance(raw, str):
        return False
    text = raw.strip().replace("\\", "/")
    if text.startswith("/"):
        return False
    rel = text.strip("/")
    return bool(rel) and SCOPE_FRAGMENT_RE.match(rel) is not None


def canonical_scope(raw: object) -> str | None:
    """The single stored form of a scope, or None when there is effectively none.

    The gate normalises separators and strips them before matching, and the write
    pattern accepts a trailing slash, so "src/pkg" and "src/pkg/" name the same
    scope. Both stores canonicalise through here before the value becomes part of a
    lesson's identity: storing the raw string made those two spellings distinct rows
    that behaved identically, so both were injected and neither deduped the other.
    Anything that is not a string has no scope.
    """
    if not isinstance(raw, str):
        return None
    return raw.strip().replace("\\", "/").strip("/") or None


def project_scope_satisfied(relpath: str, project_dir: str | Path | None) -> bool:
    """Whether an entry scoped to *relpath* applies to *project_dir*.

    *relpath* is a path fragment that identifies a repository by something it
    contains -- ``src/kiro_crew`` names the Kiro Crew source tree. The entry
    applies when *project_dir* or any ancestor of it holds that fragment, so a
    session working anywhere inside the tree qualifies while a session outside it
    does not.

    The fragment must be DISTINCTIVE to the repository, and that is the author's
    responsibility rather than something this gate can check. A fragment many
    repositories contain identifies many repositories: ``src``, ``test``,
    ``docs``, ``README.md``, ``.gitignore`` and ``.git`` all satisfy this gate in
    an unrelated checkout, and ``.git`` merely does so in every one by
    definition. Refusing particular filenames would not change that -- the set is
    open, and the most likely accident (``src``) is not a special case anyone
    would think to list -- so the rule is stated here instead of pretended at.
    The consequence of a vague fragment is a lesson applying more widely than
    intended, which is what an unscoped lesson already does; it is not a way to
    reach anything a caller could not reach by omitting the scope.

    *project_dir* is the SESSION's active project, the same value the
    ``[PROJECT]`` context block names. The process working directory is
    deliberately not consulted: this runs in the gateway while it assembles
    context, so ``Path.cwd()`` is the gateway's own directory and says nothing
    about the repository the session works on. Answering from it would decide by
    install shape instead of by work -- a gateway started inside a checkout of
    the scoped repo would admit the entry into EVERY session, and a packaged
    install would suppress it for every session.

    Fails CLOSED. No project, an unusable one, a traversal fragment, or any
    filesystem error all suppress the entry, so a surface whose project cannot be
    established never inherits repository-specific instructions.

    The fragment must name a path BELOW some ancestor, so every form that would
    resolve somewhere else is refused before any filesystem access:

    * ``.`` and ``..`` segments. ``.`` is the dangerous one -- it is a natural way
      to write "this repo", and ``candidate / "."`` exists for every candidate, so
      it would silently turn a scoped entry into a global one. That is fail-OPEN,
      the one direction this gate must never take.
    * Drive-qualified fragments. ``pathlib`` discards the left side when the right
      is absolute, so ``candidate / "C:/x"`` is ``C:/x`` on Windows and the join
      would answer about a path outside the project. A POSIX leading slash needs
      no guard: it is stripped to a relative fragment, so the join stays under the
      project and the only effect is that ``/src`` reads as ``src``.

    A backslash is read as a separator rather than as a filename character, so a
    Windows-style fragment is segmented and screened like any other.

    A fragment naming a credential path (``.ssh/id_rsa``) is refused before any
    filesystem call. Otherwise the walk would ``stat`` that path, and whether the
    entry then appears in the prompt would answer "does this key exist" for a
    surface that may be allowed to save a lesson while being denied file reads.

    The ancestor walk STOPS at the repository root, and an absolute fragment is
    refused rather than reinterpreted as a relative one. Both bound the same
    fail-open: an unbounded walk reaches ``/``, where a fragment that merely names
    a shared system directory -- ``tmp``, ``etc``, or ``/etc/passwd`` reduced to
    ``etc/passwd`` -- exists for EVERY project on the host, so a repository-scoped
    entry would silently apply everywhere. "Inside the repository identified by
    this fragment" is the question, so nothing above that repository can answer it.
    """
    if not scope_is_admissible(relpath):
        return False
    rel = relpath.strip().replace("\\", "/").strip("/")
    if not project_dir:
        return False
    try:
        root = Path(project_dir).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    for candidate in _walk_to_repo_root(root):
        # Resolve FIRST, then judge the resolved path. Checking the unresolved join
        # was a static bypass, not merely a race: a symlink already in the tree
        # ("data" -> "~/.aws") gives a literal fragment that passes the sensitive
        # check, and the existence probe then follows the link into the credential
        # directory. Strict resolution doubles as the existence probe, so a path
        # that does not exist and a path that resolves somewhere sensitive produce
        # the SAME answer -- which is what leaves no existence oracle behind.
        try:
            resolved = (candidate / rel).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        # CONTAINMENT, the invariant the bounded walk only half-enforced. Bounding
        # the walk at the repository root stops the SEARCH from climbing out, but
        # strict resolution FOLLOWS symlinks, so a link inside the tree
        # ("data" -> "/var/lib/x") answered the question from a path outside the
        # repository entirely. The question is "is this fragment inside the
        # repository it names", so a target that leaves the repository cannot answer
        # it, whether or not that target is sensitive -- the sensitive check below
        # only ever covered the subset that is.
        if not resolved.is_relative_to(candidate):
            return False
        if is_sensitive_path(str(resolved)):
            return False
        return True
    return False


def _walk_to_repo_root(root: Path) -> list[Path]:
    """*root* and its ancestors, stopping at the repository root inclusive.

    A ``.git`` entry marks that boundary -- tested with ``exists()`` rather than
    ``is_dir()`` because a worktree's ``.git`` is a FILE, and a worktree is exactly
    where this repository's own contributors work.

    With no ``.git`` anywhere above it, only *root* itself is offered: a fragment
    that identifies a repository cannot be confirmed against a directory that is
    not in one, and answering from an unbounded walk is what made a scoped entry
    global. Fails closed, like every other branch of the gate.
    """
    chain: list[Path] = []
    for candidate in (root, *root.parents):
        chain.append(candidate)
        try:
            if (candidate / ".git").exists():
                return chain
        except OSError:
            break
    return [root]
