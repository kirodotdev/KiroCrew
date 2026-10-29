"""Host-side git for the spine driver: the ``_git`` chokepoint and the reads built on it.

The driver's git reads and writes against the agent-writable clone go through :func:`_git`,
which turns hooks, fsmonitor and replace refs off on its own command line; the push itself
(``_push_with_rebase``) and the ``git apply`` of a winner's diff (``_stage_winner``,
``_stage_bug_winner``) spawn git directly. The readers below each answer one question from an
immutable object id -- the tip a fetch reported, the tree a replay must produce, a commit's
attribution, committer and tree, and the full id at ``HEAD`` -- so the publish path never
resolves a name something else could repoint.

``kiro_crew.apps.builtins.auto_improvement.spine.driver`` serves every name here and is the
import path callers use; a patch through it reaches this module (``kiro_crew._facade_mirror``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .git_safety import GIT_SAFE_CONFIG, hook_off_args, require_pinned

#: Trusted git config injected on EVERY host-side git invocation over an agent-writable tree.
#: The agent runs inside a sandbox, but these git commands run on the HOST as the gateway user
#: against the same worktree/clone the agent edits — so a repository instruction that has the
#: auto-approved shell write a hook and point `core.hooksPath` at it would get that hook
#: EXECUTED host-side, outside the sandbox, on the next add/commit/checkout. `core.hooksPath` to
#: os.devnull disables every hook; `core.fsmonitor=false` disables the fsmonitor daemon, a second
#: repo-controlled exec vector (a repo can set it to an arbitrary program git then spawns). These
#: are `-c` overrides on OUR argv, which take precedence over anything in the repo's own config,
#: and they are placed BEFORE the subcommand so git applies them.
_GIT_SAFE_CONFIG = GIT_SAFE_CONFIG


def _git(
    args: list[str], cwd: Path, *, config: tuple[str, ...] = ()
) -> subprocess.CompletedProcess:
    require_pinned(cwd)
    # ``config`` carries EXTRA ``-c`` pairs for the one caller that needs them, spliced into the
    # flag section so *args* still begins with the subcommand. Keeping that position stable is
    # deliberate: the driver family's callers and their test doubles read argv by its leading
    # verb, and a flag pushed in front of it would make the same call unrecognisable to them.
    # ``core.useReplaceRefs=false`` on EVERY driver-family call, not just the credential scan.
    # Git substitutes objects named by ``refs/replace/<oid>`` transparently in reads while the
    # push transport sends the ORIGINAL, and `git replace` is not on the pre-push reviewer's
    # denylist -- so any read here that a replacement could redirect is a place where what this
    # code inspects and what it publishes come apart. Patching reads one at a time (the scan,
    # the rebase) leaves the next one open; disabling it at the single chokepoint every read
    # goes through closes the class FOR THE DRIVER FAMILY'S READS instead of the next
    # instance. It is not repository-wide: `pr_recipe` and `backend/commit.py` do their own
    # git reads and still honour `refs/replace/*`, so their scans remain substitutable the
    # same way. Out of scope here, named so the guarantee is not read as broader than it is.
    # ``errors="replace"``: callers run `diff`/`show`, which print file CONTENT, and a repo
    # legitimately holds non-UTF-8 bytes. A strict decode raises inside
    # ``subprocess.communicate``, so the failure cannot be read off ``returncode`` — the
    # direct-push path would abort on any tree containing a PNG. See ``pr_watchers._git``.
    return subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            *_GIT_SAFE_CONFIG,
            *hook_off_args(cwd),
            "-c",
            "core.useReplaceRefs=false",
            *config,
            *args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _fetched_tip_oid(porcelain_stdout: str) -> str:
    """The object id ``git fetch --porcelain`` reports for the ref it just fetched.

    ``FETCH_HEAD`` is a mutable FILE in the clone, not a value, so keying the rebase or the
    replay count on that NAME lets anything else operating in the clone substitute what gets
    replayed -- and because only the replayed commit is scanned, a substituted parent's content
    rides along to the push through a scanner that believed it had looked. ``--porcelain`` makes
    the fetch report the id on its OWN stdout, one line per ref as
    ``<flag> <old-oid> <new-oid> <local-ref>``, so the id never comes from a ref at all. A
    forty-hex object id cannot be substituted; a ref name can. Raised by the GPT review of this
    branch, which prescribed exactly this: capture the id from the fetch operation itself.

    Returns ``""`` when no line carries a usable id -- including the all-zero null id, which
    means the ref was DELETED rather than fetched, and the case where ``--porcelain`` is not
    understood at all (an older git fails the fetch outright, so the caller never gets here).
    The caller must treat ``""`` as a refusal: an id it could not obtain is the absence of the
    check, not a pass.
    """
    for line in (porcelain_stdout or "").splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        new_oid = fields[2]
        if len(new_oid) == 40 and all(c in "0123456789abcdef" for c in new_oid):
            if new_oid == "0" * 40:
                continue  # a deletion, not a tip anything can be rebased onto
            return new_oid
    return ""


def _expected_replay_tree(base: str, src: str, cwd: Path) -> str:
    """The tree a replay of *src* onto *base* must produce. ``""`` if it cannot be computed.

    A rebase replays a commit onto a new parent, so the replay is a DIFFERENT object id
    carrying the same change -- an id captured from ambient ``HEAD`` afterwards cannot be
    checked against the authorized input by equality. What CAN be stated exactly is the
    RESULT: ``git merge-tree --write-tree`` performs the same three-way merge the rebase does
    and prints the resulting tree's object id on its own stdout, computed from two immutable
    inputs and touching no ref. Comparing that against the replay's own tree authorizes the
    published content itself rather than a property of it.

    A TREE BINDS LOCATION; A PATCH IDENTITY DOES NOT. ``git patch-id`` deliberately ignores
    hunk line numbers -- that is what lets it recognise a change replayed onto a moved base --
    so a commit whose added and removed LINES match the authorized change while sitting
    elsewhere in the file hashes identically to it. A tree names the exact content of every
    path, so the same lines in a different place is a different tree. Raised by the GPT review
    of this branch, which asked for exactly this: authorize the replay against an expected
    tree rather than a patch identity.

    Returns ``""`` on any non-zero exit, which covers a merge that CONFLICTS (the tree printed
    then records a conflict, it is not an authorization) and a git too old to know
    ``--write-tree``. The caller treats ``""`` as a refusal: a result it could not compute is
    the absence of the check, not a pass.
    """
    out = _git(["merge-tree", "--write-tree", base, src], cwd)
    if out.returncode != 0:
        return ""
    tree = ((out.stdout or "").splitlines() or [""])[0].strip()
    if len(tree) != 40 or any(c not in "0123456789abcdef" for c in tree):
        return ""
    return tree


_ATTRIBUTION_FORMAT = "%an%x00%ae%x00%cn%x00%ce%x00%B"


def _commit_attribution(rev: str, cwd: Path) -> str:
    """*rev*'s author, committer and message as one comparable string, or ``""``.

    DATES ARE DELIBERATELY ABSENT. A replay commits at a new wall-clock time, so including a
    date would make every honest retry compare unequal; the fields here are the ones a replay
    is supposed to carry over unchanged. NUL separates them so a name holding the delimiter
    cannot forge a match by spanning two fields.

    ``%B`` is the raw body, last because it is the only field that may contain newlines.
    """
    out = _git(["log", "-1", f"--format={_ATTRIBUTION_FORMAT}", rev], cwd)
    if out.returncode != 0:
        return ""
    return out.stdout or ""


def _committer_identity(rev: str, cwd: Path) -> tuple[str, str]:
    """*rev*'s committer name and email, or ``("", "")``.

    READ OFF AN IMMUTABLE OBJECT, which is the whole point: the identity a replay should carry
    is the one the authorized commit already holds, recorded when the pipeline created it and
    therefore before any reviewing agent ran. Configuration cannot be trusted for this at
    replay time -- the agent's shell can write the repository's own config, and nothing masks a
    global one either.
    """
    out = _git(["log", "-1", "--format=%cn%x00%ce", rev], cwd)
    if out.returncode != 0:
        return "", ""
    parts = (out.stdout or "").rstrip("\n").split("\0")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return "", ""
    return parts[0], parts[1]


def _commit_tree(rev: str, cwd: Path) -> str:
    """The tree object id *rev* points at, or ``""``.

    ``log -1 --format=%T`` rather than ``rev-parse <rev>^{tree}`` so this question carries its
    own verb: ``rev-parse`` in the driver family already answers an unrelated one (does a
    parent exist), and a caller keyed on argv cannot tell two questions apart under one prefix.
    """
    out = _git(["log", "-1", "--format=%T", rev], cwd)
    tree = (out.stdout or "").strip()
    if out.returncode != 0 or len(tree) != 40 or any(c not in "0123456789abcdef" for c in tree):
        return ""
    return tree


def _full_head_id(cwd: Path) -> str:
    """The FULL object id at ``HEAD``, validated, or ``""``.

    WHAT A FINALIZER HANDS BACK IS A TRUST ANCHOR, so it is a full object id and never an
    abbreviation. A short sha is an ambiguous NAME: git resolves a revision through ref names
    before abbreviated object ids, and a prefix stops naming one object as soon as a second
    object shares it. Anything able to write the repository between a read and the anchor's
    capture can arrange either reading, which makes the id that gets verified and the id that
    gets published two different things -- the substitution this whole path exists to refuse.
    A forty-hex id has no second reading, so the class is absent rather than defended against.
    Raised by the GPT review of this branch.

    ``HEAD`` is safe to resolve here, and that is the distinction: no ref can shadow ``HEAD``,
    and this runs immediately after the commit it names, before any gate hands control to an
    agent. Callers that want a short form derive it from the returned id for DISPLAY and never
    feed it back into a lookup.

    Fails CLOSED: an unreadable or malformed answer returns ``""``, which every caller treats
    as "no committed object" and refuses to publish.
    """
    out = _git(["rev-parse", "HEAD"], cwd)
    sha = (out.stdout or "").strip()
    if out.returncode != 0 or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        return ""
    return sha
