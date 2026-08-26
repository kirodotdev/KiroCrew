"""One source of truth for hardening HOST-SIDE git over an agent-writable tree.

## The threat

The improvement agent runs sandboxed, but this app's OWN git commands run on the HOST as
the gateway user, in the very worktree/clone the agent edits. Git is configurable BY THE
REPOSITORY, and several of those settings name a PROGRAM git then executes:

  * ``core.hooksPath`` + a hook file — runs on ``commit``/``push``/``checkout``.
  * ``core.fsmonitor`` — a program git spawns to enumerate changes (``status``/``diff``).
  * ``filter.<name>.clean`` / ``.smudge`` — bound by ``.gitattributes``, run by ``add``
    and ``checkout``.
  * ``diff.<name>.textconv`` / ``.command`` — bound by ``.gitattributes``, run by ``diff``.

An agent with the auto-approved shell can write both halves (``git config …`` writes
repo-local ``.git/config``; a ``.gitattributes`` is just a file), so any host-side git call
afterwards executes agent-chosen code OUTSIDE the sandbox with the operator's credentials.

## Why two mechanisms, not one

Measured against a real repo (git 2.47):

  * ``-c core.hooksPath=<devnull> -c core.fsmonitor=false`` blocks hooks and fsmonitor but
    NOT a clean filter — a planted ``filter.pwn.clean`` still executed on ``git add``.
  * ``-c core.attributesFile=<devnull>`` does NOT help: an IN-TREE ``.gitattributes`` still
    binds the driver.
  * ``GIT_CONFIG_GLOBAL/SYSTEM=<devnull>`` does NOT help: the driver lives in repo-local
    ``.git/config``, which those do not suppress.
  * ``.git/info/attributes`` DOES block it — that file outranks the in-tree
    ``.gitattributes``, and clearing the binding unbinds a driver without needing its name.
  * For a LINKED WORKTREE git reads ``info/attributes`` from the COMMON gitdir, NOT the
    per-worktree ``.git/worktrees/<id>/info`` — measured on git 2.47: a pin in the
    per-worktree copy leaves the in-tree ``.gitattributes`` filter BOUND, while the same pin
    in the common ``info`` unsets it. Candidates run in ``git worktree``s, so pinning the
    common dir (see :func:`_common_gitdir`) is what actually defends the hot path.

So the flags close the config-named vectors and the attributes pin closes the
attribute-bound ones. Both are required; neither is sufficient alone.

## Why the pin is ``* -filter diff`` and NOT ``* -filter -diff -text``

The first version of this pin used ``-diff``, which was WRONG in a way the suite caught:
``-diff`` marks every path BINARY, so ``git diff`` prints "Binary files a/f.txt and b/f.txt
differ" instead of content — and this app has a credential scanner
(``pr_recipe._scan_pushable_content``) that reads a diff to find secrets before a push. The
pin would have silently blinded a security control while appearing to harden one.

``diff`` SET (not unset) is the fix: it forces git's BUILT-IN textual differ, which unbinds
any ``diff=<driver>`` (so no ``textconv``/external command runs) while leaving the diff
readable. Verified both ways — a planted ``diff.pwn.textconv`` does not execute, and a
credential added to a tracked file is still visible in ``git diff``. ``-text`` is dropped
for the same reason: EOL conversion is not an execution vector on its own.

## Why the pin is rewritten every time

``.git/info/attributes`` is inside the tree the agent can write, and an agent that wipes it
re-opens the hole (measured: wiping it let the filter execute again). :func:`git_argv`
therefore re-pins immediately before handing back an argv, so the file is correct at the
moment of the call rather than merely at setup time.

Deliberately ONE module rather than a constant copied per call site: this same class of
finding has now been reported against six different helpers, each time because a new
host-side git surface did not carry the hardening. A single ``git_argv`` is the only shape
where adding a call site cannot silently omit it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

#: Config-named executable vectors, disabled on OUR argv (``-c`` beats any repo config).
#: ``core.hooksPath`` to os.devnull disables every hook; ``core.fsmonitor=false`` disables the
#: fsmonitor daemon. Both are global options and must precede the subcommand.
GIT_SAFE_CONFIG: tuple[str, ...] = (
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "core.fsmonitor=false",
)

#: What gets written to ``.git/info/attributes``.
#:
#: ``-filter`` UNSETS the filter attribute, unbinding any ``filter=<driver>`` so no
#: ``clean``/``smudge`` command runs. ``diff`` (SET, deliberately not ``-diff``) pins git's
#: BUILT-IN textual differ, which unbinds any ``diff=<driver>`` so no ``textconv``/external
#: diff command runs — while keeping the diff READABLE. ``-diff`` would instead mark every
#: path binary and blind this app's own credential scanner; see the module docstring.
#:
#: Both forms are name-agnostic: they neutralize a driver whose name we never have to learn.
_ATTRIBUTES_PIN = "* -filter diff\n"


class GitSafetyError(RuntimeError):
    """The attributes pin could not be established, so a host-side git call must NOT run.

    Fail-closed: if the pin cannot be written the filter/diff drivers are NOT neutralized, so
    running git anyway would execute agent-planted code. Callers turn this into a refused
    operation (a failed pass / a degraded-to-queue path), never a silent proceed.
    """


def _reject_link(path: Path) -> None:
    """Raise if ``path`` exists and is a symlink / non-regular reparse point.

    The pin lands inside a tree the AGENT can write, so it could replace any component of
    ``.git/info/attributes`` with a symlink (or, on Windows, a reparse point/junction).
    Following it would let ``pin_attributes`` write our content THROUGH the link and corrupt
    an arbitrary host file — or, worse, be pointed at a location that re-enables a driver.
    ``lstat`` (never ``stat``) so the link itself is inspected, not its target. A missing
    component is fine — we create it. This is the check GPT flagged the original code for
    omitting.
    """
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise GitSafetyError(f"cannot stat {path}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise GitSafetyError(f"refusing to follow a symlink at {path} while pinning git attributes")
    # `.git` may legitimately be a FILE (linked worktree) or a DIR; `info` must be a real dir;
    # `attributes` must be a real file. A reparse-point-backed dir (Windows junction) is not a
    # plain directory and is rejected by the S_ISDIR checks in the caller.


def _resolve_gitdir(git_dir_owner: Path | str) -> Path | None:
    """The real gitdir whose ``info/attributes`` we must pin, or None if not a git tree.

    For a linked worktree ``.git`` is a FILE (``gitdir: <path>``); for a normal clone it is a
    DIR; a caller may also pass the gitdir itself. Every component is link-checked.
    """
    root = Path(git_dir_owner)
    dot_git = root / ".git"
    _reject_link(dot_git)
    if dot_git.is_file():
        # Linked worktree: ".git" is a FILE holding "gitdir: <path>". Its CONTENTS are
        # agent-writable, so the path cannot be trusted on its own — an injected prompt could
        # repoint it at ANOTHER repository's gitdir, and we would then `O_TRUNC` that repo's
        # `info/attributes` (an arbitrary-write primitive). Git makes this backpointer
        # BIDIRECTIONAL: the real gitdir carries a `gitdir` file that points back at THIS
        # worktree's `.git` file. Validate that round-trip and refuse a mismatch. Raised by the
        # GPT review.
        text = dot_git.read_text(encoding="utf-8").strip()
        if "gitdir:" not in text:
            return None
        gd = Path(text.split("gitdir:", 1)[1].strip())
        _reject_link(gd)
        if not gd.is_dir():
            return None
        backptr = gd / "gitdir"
        _reject_link(backptr)
        try:
            back = Path(backptr.read_text(encoding="utf-8").strip())
        except OSError:
            # No backpointer — not a genuine linked-worktree gitdir. Refuse rather than pin an
            # unvalidated location.
            raise GitSafetyError(
                f"linked-worktree gitdir {gd} has no `gitdir` backpointer — refusing to pin an "
                f"unvalidated location"
            )
        # The backpointer must resolve to THIS worktree's own `.git` file.
        try:
            same = back.resolve() == dot_git.resolve()
        except OSError:
            same = False
        if not same:
            raise GitSafetyError(
                f"linked-worktree gitdir {gd} backpoints to {back}, not to this worktree's "
                f"{dot_git} — refusing (the `.git` file may have been repointed at another repo)"
            )
        # `gd` is a VALIDATED per-worktree gitdir — but that is NOT where git reads
        # `info/attributes`. For a linked worktree git redirects `info/` to the COMMON
        # gitdir, so a pin in the per-worktree copy leaves the in-tree `.gitattributes`
        # filter/diff drivers BOUND — the exact sandbox escape this module closes.
        # Return the common gitdir instead. Raised (correctly) by the GPT review.
        return _common_gitdir(gd)
    if dot_git.is_dir():
        return dot_git
    if root.name == ".git":
        _reject_link(root)
        return root if root.is_dir() else None
    return None


def _common_gitdir(per_worktree: Path) -> Path:
    """The COMMON gitdir whose ``info/attributes`` git actually consults for a linked worktree.

    Git redirects a linked worktree's ``info/`` (and ``objects/``, ``refs/`` …) to the COMMON
    gitdir; only ``HEAD``, ``index`` and a handful of others are per-worktree. So the pin that
    neutralizes the in-tree ``.gitattributes`` filter/diff drivers has to land in the common
    ``info/attributes`` — a per-worktree copy is simply not read (measured on git 2.47).

    ``per_worktree`` is ALREADY validated (its bidirectional ``gitdir`` backpointer resolved to
    this worktree's ``.git``), so it is a genuine ``$GIT_COMMON_DIR/worktrees/<id>``. The common
    dir is therefore ``per_worktree.parent.parent`` by git's fixed layout. We derive it from that
    LAYOUT rather than from the ``commondir`` FILE, because that file lives inside the
    agent-writable gitdir and repointing it would make us ``O_TRUNC`` an arbitrary repo's
    ``info/attributes`` — the same arbitrary-write primitive the ``.git``-file check already
    closes. If a ``commondir`` file is present we cross-check it and REFUSE a mismatch, so a
    poisoned copy is caught rather than followed.
    """
    if per_worktree.parent.name != "worktrees":
        raise GitSafetyError(
            f"linked-worktree gitdir {per_worktree} is not under a `worktrees/` parent — "
            f"refusing to guess its common gitdir"
        )
    common = per_worktree.parent.parent
    _reject_link(common)
    # A real gitdir has a HEAD; requiring it rejects a common dir that was swapped for a
    # non-repo directory. (objects/refs are also present but HEAD is the cheapest witness.)
    if not common.is_dir() or not (common / "HEAD").is_file():
        raise GitSafetyError(
            f"derived common gitdir {common} for {per_worktree} is not a git directory — refusing"
        )
    commondir_file = per_worktree / "commondir"
    _reject_link(commondir_file)
    if commondir_file.is_file():
        # git resolves `commondir` RELATIVE TO the per-worktree gitdir. It is `../..` for a
        # standard `git worktree add`, which resolves to exactly the layout-derived `common`.
        try:
            stated = (per_worktree / commondir_file.read_text(encoding="utf-8").strip()).resolve()
        except OSError as exc:
            raise GitSafetyError(f"cannot read commondir for {per_worktree}: {exc}") from exc
        try:
            agree = stated == common.resolve()
        except OSError:
            agree = False
        if not agree:
            raise GitSafetyError(
                f"commondir for {per_worktree} names {stated}, not the layout-derived {common} — "
                f"refusing (the `commondir` file may have been repointed at another repo)"
            )
    return common


def _pin(git_dir_owner: Path | str) -> str:
    """Establish the pin. Returns one of:

      * ``"pinned"``  — a gitdir was found and its ``info/attributes`` now holds the pin.
      * ``"no-repo"`` — there is NO gitdir at this path, so there is nothing to protect: no
                        repo-local config and no in-tree ``.gitattributes`` can bind a driver,
                        so a bare git call here has no attribute-execution surface. This is a
                        legitimate, safe outcome (a pre-clone probe, a non-repo tmp dir), NOT a
                        failure — distinguishing it is what keeps :func:`require_pinned` from
                        refusing harmless calls while still failing closed on a real repo.

    Raises :class:`GitSafetyError` if a gitdir EXISTS but the pin cannot be safely written (a
    symlink swap on any component, an unwritable ``info``). Link-checks every component and
    writes ``O_NOFOLLOW`` so a symlink swapped in at the last instant is refused by the kernel.
    """
    gitdir = _resolve_gitdir(git_dir_owner)  # may raise GitSafetyError on a linked `.git`
    if gitdir is None:
        return "no-repo"
    try:
        info = gitdir / "info"
        _reject_link(info)
        info.mkdir(parents=True, exist_ok=True)
        target = info / "attributes"
        _reject_link(target)
        if target.is_file() and target.read_text(encoding="utf-8") == _ATTRIBUTES_PIN:
            return "pinned"
        # O_NOFOLLOW: a symlink at `target` fails the open (ELOOP) rather than being written
        # through. O_TRUNC replaces stale/partial content. 0o600 — this is our file.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(target), flags, 0o600)
        try:
            os.write(fd, _ATTRIBUTES_PIN.encode("utf-8"))
        finally:
            os.close(fd)
        return "pinned"
    except GitSafetyError:
        raise
    except OSError as exc:
        # A gitdir EXISTS but we could not pin it — that is the dangerous case (a driver bound
        # in this real repo would run undefended), so escalate rather than degrade.
        raise GitSafetyError(f"could not write the git attributes pin under {gitdir}: {exc}") from exc


def pin_attributes(git_dir_owner: Path | str) -> bool:
    """Best-effort boolean wrapper around :func:`_pin`: True iff a pin was established.

    Retained for callers/tests that want a simple did-it-pin answer. A symlink swap still
    RAISES (that is a security event, not a soft miss); a non-repo path returns False.
    """
    return _pin(git_dir_owner) == "pinned"


def require_pinned(cwd: Path | str) -> None:
    """Fail CLOSED: establish the attributes pin before a host-side git call, or raise.

    The primitive every host-side git helper calls before spawning git over an agent-writable
    tree. Raises :class:`GitSafetyError` when a gitdir EXISTS but its pin cannot be safely
    written (symlink swap, unwritable ``info``) — running git there undefended is exactly the
    sandbox escape the pin prevents. A path with NO gitdir is allowed through: there is no
    repo-local config or in-tree ``.gitattributes`` to bind a driver, so no attribute-execution
    surface exists to defend (a pre-clone probe must not be refused).
    """
    _pin(cwd)  # raises GitSafetyError on a real-repo pin failure; "no-repo"/"pinned" both OK


def git_argv(cwd: Path | str, *args: str) -> list[str]:
    """A hardened ``git -C <cwd> <safe-config> <args…>`` argv, with the attributes pin
    established FIRST (fail-closed — raises :class:`GitSafetyError` if it cannot be).

    THE call-site helper: every host-side git invocation over an agent-writable tree should
    build its argv here, so the hook/fsmonitor flags and the attribute pin cannot be
    forgotten by a new caller.
    """
    require_pinned(cwd)
    return ["git", "-C", str(cwd), *GIT_SAFE_CONFIG, *args]
