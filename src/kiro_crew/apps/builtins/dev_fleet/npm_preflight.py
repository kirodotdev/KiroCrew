"""Pre-merge installability probe for Dev Fleet's Pull+Build frontend half.

``npm ci`` DELETES ``node_modules`` before it installs. So a registry that
refuses one package turns a sync into damage rather than a no-op: the tree is
emptied, the run aborts mid-reify, and the checkout is left with new source, a
new lockfile, and no frontend dependencies at all. Re-pressing Pull+Build
repeats the deletion.

This module answers the one question that prevents that, BEFORE the merge
lands: *can the incoming lockfile be installed at all?* It reads the lockfile
out of the fetched ref (``git show``) rather than the working tree, so it can
run between ``fetch`` and ``merge`` — the only point where the new lockfile is
already knowable and nothing has been applied yet. A refusal there costs
nothing, because ``fetch`` only moves remote refs.

Two things it deliberately is NOT:

* It is NOT a registry auth check. A dead registry token is invisible to a
  build whose packages are all in npm's local cache, because cacache retrieval
  is integrity-addressed: a tarball already on disk satisfies its lockfile
  entry with no network and no credentials. A ``npm ping``-style probe would
  therefore fail while the very install it guards would have succeeded — it
  would block good syncs and teach operators to ignore it. Asking
  "is this installable" instead is both narrower and correct, and it is
  registry-agnostic: it holds for a public registry, a private mirror, or an
  air-gapped cache alike.
* It does NOT run the package tree's lifecycle scripts (``--ignore-scripts``).
  The probe exists to answer a question, not to execute the worktree's install
  hooks a second time; skipping them also keeps the probe strictly less
  privileged than the step it guards.

It performs a REAL install into a disposable directory rather than
``npm ci --dry-run``, and that is not a preference. A dry run does not attempt
retrieval at all: against a lockfile pinning a tarball that 404s, measured,
``npm ci --dry-run --ignore-scripts`` exits 0 and reports "added 1 package"
while the same command without ``--dry-run`` exits 1 on the missing tarball. A
dry run would therefore pass exactly the case this module exists to catch, so
the probe has to fetch. That install is cheap next to the emptied
``node_modules`` it prevents -- and it is not paid on every sync:
:func:`_install_already_proven` skips it when the incoming ref touches nothing
under ``website/`` and a populated tree is already there to answer for it.

That disposable directory is created inside the REPO rather than in ``TMPDIR``
whenever the checkout can host it -- it exists, it is writable, and git hides the
scratch name; :func:`_scratch_parent` and :func:`_scratch_name_is_ignored` carry
those conditions and :func:`_make_scratch` falls back to ``TMPDIR`` when any of
them fails. The reason to prefer the repo: a rehearsal is only meaningful on the
filesystem the real install writes to, and the usual ``TMPDIR`` is a
memory-backed filesystem with a fixed file limit that a dependency tree reaches
long before it runs short of bytes.

The flags otherwise MIRROR the real step exactly. A probe that resolves
differently from the install is worse than no probe: it either passes what will
fail, or fails what would have worked. ``--no-audit``/``--no-fund`` are the only
additions, and neither participates in resolution.

The distinct exit codes do NOT drive a cross-process protocol: the runner only
tests non-zero, and nothing outside this module reads which code came back. What
the classification is for is :func:`explain` -- turning a failure into one
registry-neutral sentence the dashboard can show instead of npm's log-file
pointer. Keeping the codes separate is what makes that sentence specific, and
what lets a caller tell a host condition (a full scratch filesystem) from a
lockfile that genuinely cannot be installed.
"""

from __future__ import annotations

import argparse
import errno
import os
import re
import shutil
import subprocess  # nosec B404 - probing npm/git is this module's purpose
import sys
import tempfile
import time
from pathlib import Path

#: Line prefix the probe uses for a human-readable detail line in the run log.
#: This is LOG TEXT ONLY -- it is never promoted into the authoritative failure
#: diagnosis. That distinction is a security boundary: the run's stdout also
#: carries worktree-controlled build output, so any in-band marker there can be
#: forged by an install script printing the same prefix and then failing. The
#: diagnosis therefore travels as an EXIT CODE, which a child of a step cannot
#: forge, and the gateway maps it to text through :func:`explain`.
DETAIL_PREFIX = "preflight: "

#: The probe found the incoming lockfile installable.
EXIT_OK = 0
#: The registry refused to authenticate us (npm ``E401``/``E403``) -- the one
#: failure an operator can act on directly, by refreshing their credential.
EXIT_AUTH = 41
#: Something else went wrong that we could not classify.
EXIT_FAILED = 42
#: A network-shaped failure -- the one class where a later retry can differ.
EXIT_TRANSIENT = 43
#: A package version the lockfile pins is not obtainable (npm ``E404``). On a
#: curated mirror this is what a blocked version looks like, so it is NOT an
#: auth problem, and refreshing a credential would not make the version appear.
EXIT_UNAVAILABLE = 44
#: Something ran out of room. Because the probe performs a REAL install it needs
#: about as much space -- and as many FILES -- as a ``node_modules`` tree. Its own
#: class so running out of room reads as a host condition rather than a lockfile
#: that cannot be installed.
#:
#: Two axes, and pinning either one in the explanation is what makes this code
#: hard to act on. A filesystem can run out of BYTES or of FILE SLOTS (inodes),
#: and both return ``ENOSPC``: a memory-backed filesystem is mounted with a fixed
#: inode count that a dependency tree's tens of thousands of files reach long
#: before its bytes run short, while a disk-backed one usually runs out of bytes.
#: And the install writes to TWO filesystems, which need not be the same one --
#: the scratch directory, and the package cache the retrieval populates. So the
#: explanation names no single filesystem and no single budget; it sends the
#: operator to check all of them.
EXIT_NO_SPACE = 45
#: The sync runner found a dependency tree AND a leftover backup of one, and
#: cannot tell which is complete. Owned by the runner rather than the probe, but
#: numbered here so ONE table maps every code the sync can exit with to text.
EXIT_TREE_AMBIGUOUS = 46
#: The runner could not put a stashed dependency tree back after a failed step.
EXIT_RESTORE_FAILED = 47
#: The incoming ref proved the frontend install and build are already on disk
#: (whole ``website/`` subtree unchanged AND ``node_modules`` populated), so the
#: reinstall and rebuild are not owed. This is a SUCCESS verdict, not a failure
#: -- it carries no ``_EXPLAIN`` sentence -- but it is RESERVED so the runner
#: trusts it only from the preflight step's own label. A later worktree-run step
#: (a pip lifecycle script) exiting 48 is DEMOTED to a plain failure by
#: :func:`sync_runner.demote_reserved`, which is what stops an untrusted step
#: from forging a "skip the build" verdict and shipping stale assets. The
#: verdict travels as this exit code and lives in the runner's own state, never
#: as a file any same-UID step could create.
EXIT_FRONTEND_SKIP = 48

#: The checkout subdirectory holding the frontend half. A ``probe()`` parameter
#: once carried this, but only ``main()`` ever called it and it never passed one
#: -- the same reason the CLI's own ``--subdir`` flag was removed.
_FRONTEND_SUBDIR = "website"

#: Files the probe needs from the incoming ref to resolve the same way the real
#: step will. ``.npmrc`` matters as much as the lockfile: it carries settings
#: that change resolution (a minimum-release-age gate, for one), so omitting it
#: would make the probe answer a different question than the install.
_PROBE_FILES = ("package-lock.json", "package.json", ".npmrc")

#: Ordered classification. First match wins, so the specific auth and
#: not-found signals are tested before the generic network ones.
_SIGNALS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (
        EXIT_AUTH,
        re.compile(
            r"\bE401\b|\bE403\b|\bEAUTHUNKNOWN\b|\bENEEDAUTH\b"
            r"|unable to authenticate|401 unauthorized|403 forbidden"
            r"|authentication token seems to be invalid",
            re.I,
        ),
    ),
    (EXIT_UNAVAILABLE, re.compile(r"\bE404\b|404 not found", re.I)),
    # Both spellings of out of room, so npm's OWN output is classified the same
    # way `_OUT_OF_ROOM_ERRNOS` classifies a direct filesystem error. Without the
    # quota spellings the two paths disagree: a probe whose `mkdtemp` hit EDQUOT
    # got the actionable out-of-room sentence, while one whose `npm ci` reported
    # the same condition fell through to the generic failure message.
    (
        EXIT_NO_SPACE,
        re.compile(
            r"\bENOSPC\b|no space left on device|\bEDQUOT\b|disk quota exceeded",
            re.I,
        ),
    ),
    (
        EXIT_TRANSIENT,
        re.compile(
            r"\bETIMEDOUT\b|\bENOTFOUND\b|\bECONNRESET\b|\bECONNREFUSED\b"
            r"|\bEAI_AGAIN\b|\bERR_SOCKET_TIMEOUT\b|network timeout|socket hang up",
            re.I,
        ),
    ),
)

#: Human-facing, registry-neutral explanations. These are what the dashboard
#: shows instead of npm's last output line, which is its "a complete log of this
#: run can be found in ..." pointer — the least informative line it prints.
_EXPLAIN = {
    EXIT_AUTH: (
        "the package registry rejected our credentials, so the incoming "
        "lockfile cannot be installed — refresh the registry credential and "
        "press Pull + Build again"
    ),
    EXIT_UNAVAILABLE: (
        "a package version the incoming lockfile pins is not available from "
        "the configured registry"
    ),
    EXIT_TRANSIENT: (
        "the package registry could not be reached, so the incoming lockfile "
        "could not be verified — try again in a moment"
    ),
    EXIT_NO_SPACE: (
        "verifying the incoming lockfile ran out of room — check the free bytes "
        "(df -h) AND the free file count (df -i), on the checkout's filesystem "
        "and on the package cache's, because any one of those four can be "
        "exhausted while the other three look healthy — then free room and press "
        "Pull + Build again"
    ),
    EXIT_TREE_AMBIGUOUS: (
        "a previous sync left a dependency-tree backup beside the tree, and "
        "which one is complete cannot be told from disk — remove whichever you "
        "do not want to keep (the log lists both paths), then press Pull + "
        "Build again"
    ),
    EXIT_RESTORE_FAILED: (
        "the dependency tree could not be restored automatically; both the "
        "partial tree and its backup were left in place — see the log for both "
        "paths"
    ),
    EXIT_FAILED: "the incoming lockfile could not be installed",
}


def classify(output: str) -> int:
    """Map npm's own diagnostics onto one of the ``EXIT_*`` codes.

    Reads npm's error CODES rather than guessing from the registry URL, so the
    result is the same whichever registry is configured. Unrecognized failures
    are ``EXIT_FAILED``, never ``EXIT_TRANSIENT``: calling an unknown failure
    "transient" invites a retry that cannot help and hides the real cause.
    """
    for code, pattern in _SIGNALS:
        if pattern.search(output):
            return code
    return EXIT_FAILED


#: Every code this module can explain. The sync runner needs this set because an
#: exit code is only trustworthy from a step whose binary is OURS: a step running
#: worktree-controlled code (an npm lifecycle script, a vite config) can exit any
#: number it likes, and a forged 41 would make the dashboard assert a registry
#: credential failure -- with a remedy -- for what was actually a build error. So
#: the runner remaps a reserved code coming from any step other than the probe.
#: EXIT_FRONTEND_SKIP is added explicitly: it is a SUCCESS verdict with no
#: _EXPLAIN sentence, but it must be reserved so a worktree-run step cannot forge
#: it to skip the frontend build (its whole point is that only the trusted
#: preflight step may assert it).
RESERVED_EXIT_CODES = frozenset(_EXPLAIN) | {EXIT_FRONTEND_SKIP}


def explain_exit(rc: int) -> str:
    """The sentence for a run's exit code, or ``""`` when we own no diagnosis.

    Deliberately NOT a fallback: :func:`explain` answers ``EXIT_FAILED`` for
    anything it does not recognise, which is right when a probe has already
    decided the failure is its own. Here the input is the exit code of an
    arbitrary step -- ``npm ci`` exiting 1, a compile error, a killed process --
    and inventing "the incoming lockfile could not be installed" for those would
    state a cause that was never established. Only codes this module assigns get
    a sentence; everything else leaves the dashboard on its existing fallback.
    """
    return _EXPLAIN[rc] if rc in _EXPLAIN else ""


#: Errnos that mean a filesystem has no room for another byte or another file.
#: ``ENOSPC`` is the general one; ``EDQUOT`` is the SAME condition enforced per
#: user by a quota, which is an ordinary managed-host configuration rather than
#: an exotic one. Both have to classify identically in both places that read an
#: errno here, or a quota-limited checkout falls through the "is this the target
#: filesystem's own answer?" test in :func:`_make_scratch`, retries in
#: ``TMPDIR``, and the probe certifies a filesystem the install will never use.
#: Built by lookup because ``EDQUOT`` is absent on some platforms.
_OUT_OF_ROOM_ERRNOS = tuple(
    code
    for code in (getattr(errno, name, None) for name in ("ENOSPC", "EDQUOT"))
    if code is not None
)


def _os_error_code(exc: OSError) -> int:
    """Classify an OSError from the probe's own filesystem work.

    Every write the probe makes lands in its scratch directory, and because the
    probe performs a REAL install that directory's filesystem can run out of
    room -- in bytes or in files. An uncaught OSError would kill the step with a
    traceback and no classified cause -- which puts the dashboard back to showing
    whatever the last output line happened to be, the exact defect this module
    exists to remove. So the probe's own IO is mapped to a code here, in ONE
    place, rather than guarded a site at a time.
    """
    if getattr(exc, "errno", None) in _OUT_OF_ROOM_ERRNOS:
        return EXIT_NO_SPACE
    return EXIT_FAILED


#: Prefix for the probe's disposable install directory.
_SCRATCH_PREFIX = ".kirocrew-npm-preflight-"

#: How old a scratch directory must be before the sweep may remove it.
#:
#: :func:`probe` deletes its own scratch in a ``finally``, so the only ones left
#: behind are from a run that never reached it -- SIGKILL, an OOM kill, a reboot.
#: Those need a sweeper, and needing one is what moving the scratch into the repo
#: introduced: ``/tmp`` is age-cleaned by the host, the repo root is cleaned by
#: nobody, and the directory is git-ignored, so an abandoned ``node_modules``
#: tree accumulates there invisibly and permanently.
#:
#: The threshold is what makes the sweep safe without a lock. A LIVE probe's
#: scratch is bounded by ``probe(timeout=...)`` plus its fixed-timeout helpers --
#: under twenty minutes at the default -- so a directory hours old cannot belong
#: to a probe still running, and a concurrent Pull + Build is never touched. The
#: margin is deliberately far wider than that bound rather than close to it: the
#: cost of sweeping too late is delay, and the cost of sweeping too early is
#: breaking another operator's in-flight verification.
_SCRATCH_STALE_SECS = 6 * 3600

#: File written inside a scratch directory to prove the probe created it.
#:
#: The sweep performs a RECURSIVE DELETE in the operator's checkout root, where an
#: unrecoverable mistake is the worst outcome this module could have. A name prefix
#: is a convention, not proof of authorship: anything able to create a directory
#: there can wear the prefix, and the ignore rule added with this change keeps such
#: a directory out of ``git status`` as well. So deletion requires a marker this
#: code wrote, and the sweep's two conditions then divide the question cleanly --
#: the marker answers "is this MINE", the age answers "is it still IN USE".
_SCRATCH_MARKER = ".kirocrew-probe-owned"


def _mark_scratch_owned(path: Path) -> None:
    """Stamp *path* as this module's, so the sweep may delete it later.

    Best-effort: a failure here means the directory is never swept, which leaks one
    tree. That is the deliberate direction to fail in -- an unswept directory costs
    room, and room is reclaimable, while deleting an unmarked directory costs data
    nobody can get back. The window is narrow too: the only run that leaks is one
    killed between ``mkdtemp`` and this write, since a probe that gets any further
    removes its own scratch in a ``finally``.
    """
    try:
        (path / _SCRATCH_MARKER).touch()
    except OSError:
        pass


def _sweep_stale_scratch(parent: str) -> None:
    """Remove scratch directories in *parent* left by runs that were killed.

    Best-effort by construction: every failure is swallowed, because a sweep is
    housekeeping and must never be the reason a verification does not happen.
    An unreadable parent, a racing sweep in another process and a tree the
    current user cannot delete all end the same way -- the probe proceeds.

    Called BEFORE the scratch is created, which is what makes it a remedy rather
    than only hygiene: the litter it removes is charged to the same byte and file
    budgets the incoming install needs, so on a filesystem that abandoned trees
    have filled, the sweep is what lets the probe run at all. If room is still
    short afterwards, that scarcity is the target filesystem's real answer.

    Both conditions are load-bearing and neither implies the other. The marker is
    the only evidence that this module created the directory, so a look-alike in
    the checkout root is never touched. The age is what makes a lock unnecessary:
    a CONCURRENT probe's scratch carries a marker too, and only its youth keeps it.
    """
    cutoff = time.time() - _SCRATCH_STALE_SECS
    try:
        entries = list(os.scandir(parent))
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith(_SCRATCH_PREFIX):
            continue
        try:
            # follow_symlinks=False: read the ENTRY's own age. Through a link the
            # answer is the target's, so a fresh link to an old tree and an old
            # link to a fresh one both decide wrong.
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                continue
            marker = Path(entry.path) / _SCRATCH_MARKER
            # A REGULAR file, not merely something at that name: a symlinked
            # marker would let whatever planted it authorize the delete.
            if marker.is_symlink() or not marker.is_file():
                continue
        except OSError:
            continue
        shutil.rmtree(entry.path, ignore_errors=True)


def _scratch_name_is_ignored(git: str, repo: str) -> bool:
    """Would *repo*'s working tree hide a scratch directory of ours?

    Asked because the two halves ship SEPARATELY. ``npm_preflight`` arrives with
    the installed gateway, while the ignore rule that covers its scratch name is
    a commit in the checkout's own history -- so right after an upgrade, a fleet
    checkout still parked on an older ref runs this code with no rule for it. A
    probe killed in that window leaves an UNTRACKED directory in the checkout
    root, which reads as dirty and fail-closes "Prune merged": the same
    operator-unactionable refusal this module exists to remove, reintroduced from
    the other side.

    ``git check-ignore`` is the only correct oracle -- ignore resolution spans
    several files with precedence and negation, so reading ``.gitignore`` here
    would be a second, wrong implementation of it. Anything that leaves the
    question unanswered (a failing or missing git, a timeout) is read as NOT
    ignored: the conservative direction, because the cost of being wrong that way
    is a probe on ``TMPDIR`` -- this module's previous behaviour -- while the
    other way is a checkout that silently reads dirty.
    """
    try:
        proc = subprocess.run(  # nosec B603 - argv list, no shell
            [git, "-C", repo, "check-ignore", "-q", "--no-index", f"{_SCRATCH_PREFIX}probe"],
            # ``-C`` scopes git to the checkout; ``cwd`` makes that the child's
            # working directory as well, so the question is asked from inside the
            # checkout rather than from wherever the gateway happens to run. A
            # *repo* that does not exist fails the spawn, which the except below
            # already reads as "not ignored".
            cwd=repo,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    # 0 = ignored, 1 = not ignored, anything else = git could not answer.
    return proc.returncode == 0


def _scratch_parent(repo: str) -> str | None:
    """Where to create the probe's install, or ``None`` to use ``TMPDIR``.

    ``tempfile.mkdtemp()`` with no ``dir`` takes ``TMPDIR``, which on a default
    Linux host is ``/tmp`` -- and ``/tmp`` is commonly a memory-backed
    filesystem whose INODE count is capped at mount time and shared with every
    other process on the box. Two consequences follow that a bytes-only reading
    of "scratch space" misses. A ``node_modules`` tree is tens of thousands of
    files, so unrelated litter left in ``/tmp`` by anything else on the host can
    starve Pull+Build while tens of gigabytes are still free -- the failure then
    names space, and the operator's free-space check says there is plenty. And
    the probe's entire install is charged to RAM.

    So the scratch is taken from the REPO, which sits on the filesystem the real
    ``npm ci`` writes into. That is a correctness property and not only a
    capacity one: this module exists to REHEARSE the real install, and a
    rehearsal held on a filesystem with a different free-room budget than the
    real target answers a different question -- it can pass where the real step
    will fail for room, or fail where the real step would have succeeded.

    The repo ROOT rather than ``website/``, though ``website/node_modules`` is
    what the real step fills. Both are the same filesystem in any ordinary
    checkout, so the capacity answer is identical, and the root keeps the
    directory outside two things scoped to ``website/``: the frontend project
    ``npm`` would resolve config against, and the subtree
    :func:`_frontend_worktree_clean` reads -- so a directory left behind by a
    killed process cannot make the next sync's skip decision wrong.

    ``None`` when the repo cannot host it (missing, or not writable -- a
    read-only checkout), so the caller falls back to ``TMPDIR``: a checkout that
    cannot hold a scratch directory still gets a probe.
    """
    try:
        parent = Path(repo)
        if not parent.is_dir() or not os.access(parent, os.W_OK):
            return None
    except OSError:
        return None
    return str(parent)


def _make_scratch(git: str, repo: str) -> tuple[Path | None, tuple[int, str] | None]:
    """Create the probe's scratch directory. Returns ``(path, failure)``.

    Falls back from the repo to ``TMPDIR`` only for a host condition that makes
    the repo unusable as a scratch host at all -- it cannot hold the directory,
    or it would not hide it (see :func:`_scratch_name_is_ignored`). Being OUT OF
    ROOM is not one: that says the filesystem the real install targets has none,
    which is the probe's answer, and rehearsing somewhere roomier instead would
    certify a filesystem the install will never touch. ``_OUT_OF_ROOM_ERRNOS`` is
    what makes that hold under a per-user quota as well as a genuinely full disk.

    Sweeps abandoned scratch directories first, so the room a killed run is still
    holding is returned to the budget the incoming install is measured against.
    The sweep runs whenever the repo COULD host one, including when the ignore
    gate then sends this probe to ``TMPDIR`` -- litter a previous gateway build
    left behind is exactly what an un-ignored checkout needs cleared. Only the
    repo parent is swept: ``TMPDIR`` is age-cleaned by the host, and a fallback
    path is not one this module chose or can reason about.
    """
    parent = _scratch_parent(repo)
    if parent is not None:
        _sweep_stale_scratch(parent)
        if not _scratch_name_is_ignored(git, repo):
            parent = None
    try:
        made = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=parent))
        _mark_scratch_owned(made)
        return made, None
    except OSError as exc:
        if parent is None or getattr(exc, "errno", None) in _OUT_OF_ROOM_ERRNOS:
            return None, (
                _os_error_code(exc),
                f"could not create a scratch directory: {exc}",
            )
    try:
        made = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX))
        _mark_scratch_owned(made)
        return made, None
    except OSError as exc:
        return None, (_os_error_code(exc), f"could not create a scratch directory: {exc}")


def _extract(git: str, repo: str, ref: str, subdir: str, dest: Path) -> tuple[int, str] | None:
    """Copy the probe files out of *ref* into *dest*.

    Reads from the fetched ref, NOT the working tree — that is what lets this
    run before the merge. ``package-lock.json`` is required; the others are
    optional because a checkout may legitimately not carry them.

    Returns ``(code, detail)`` on failure, or ``None`` on success. It returns a
    CODE rather than only a message because one of its failure modes is a full
    scratch filesystem, which is a host condition and not a lockfile that cannot
    be installed -- the caller must be able to tell those apart.
    """
    for name in _PROBE_FILES:
        try:
            proc = subprocess.run(  # nosec B603 - argv list, no shell
                [git, "-C", repo, "show", f"{ref}:{subdir}/{name}"],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return EXIT_TRANSIENT, f"reading {subdir}/{name} from {ref} timed out"
        except OSError as exc:
            return _os_error_code(exc), f"could not run git: {exc}"
        if proc.returncode != 0:
            if name == "package-lock.json":
                return EXIT_FAILED, (
                    f"cannot read {subdir}/{name} from {ref} "
                    f"({(proc.stderr or b'').decode(errors='replace').strip()})"
                )
            continue
        try:
            (dest / name).write_bytes(proc.stdout)
        except OSError as exc:
            return _os_error_code(exc), f"could not write {name} to the scratch dir: {exc}"
    return None


def _install_already_proven(git: str, repo: str, ref: str) -> str | None:
    """Reason to SKIP the probe install, or ``None`` to run it.

    The probe answers "can the INCOMING lockfile be installed?", and it pays a
    real script-free install to answer honestly. But when the incoming ref
    changes NOTHING under ``website/``, that question has already been put to
    disk: no new resolution is arriving, and a populated ``node_modules`` sits
    beside the one that is already there. Re-deriving it costs a full scratch
    install on every backend-only sync -- the common case, since most syncs move
    Python and never touch the frontend half at all.

    Both halves are load-bearing, and the skip is refused unless both hold:

    * **The whole frontend subtree is unchanged**, not merely its resolution
      inputs. Comparing only ``package-lock.json`` / ``package.json`` /
      ``.npmrc`` was not enough, and the gap is worth stating because it is
      subtle: with those three identical but frontend SOURCE changed, a skipped
      probe lets the merge land, and a failing ``npm ci`` afterwards leaves the
      checkout with new source and a bundle built from the old source. Requiring the
      entire subtree to be identical makes that unreachable -- with no frontend
      change there is no new bundle to be missing, so a failed sync leaves the
      frontend byte-for-byte as it was.
    * **A populated tree to point at.** With no ``node_modules`` there is no
      evidence at all -- so a fresh checkout's first sync still probes, which is
      exactly when the answer is least known. Populated rather than merely
      present, because an interrupted ``npm ci`` can leave an empty directory
      behind and an empty tree proves nothing.

      Be precise about what populated does NOT prove: it is evidence, not a
      verified install. A prior FRONTEND sync whose post-merge ``npm ci`` died
      partway can leave a partial tree beside the merged lockfile, and a later
      backend-only sync will skip on it -- the subtree is unchanged from there
      on, so nothing re-examines it. That stays benign for the same reason the
      dead-registry residual does: the skip decides only whether this sync PAYS
      for a rehearsal, so a refusal lands one step later instead of never, and
      the transaction keeps the checkout consistent either way. The evidence test
      should cover this scenario and not only the interrupted one.

    What makes skipping SAFE rather than merely cheap is where a failure lands.
    The probe exists because ``npm ci`` deletes ``node_modules`` first, so a
    refusal after the merge would leave new source beside an emptied tree.
    Under this condition that outcome is not reachable: the runner's transaction
    moves the tree aside and puts it back on any non-zero step, the lockfile did
    not change, and neither did the source the bundle was built from.
    A skipped probe can only leave a state a later ``npm ci`` fixes.

    ``git diff`` rather than a byte comparison of the resolution files, because
    the question is about the whole subtree and git already answers exactly that
    against the working tree. Anything it cannot answer -- a failing or missing
    git, a timeout -- returns ``None`` and the probe runs, so the unknown case
    costs an install rather than a guarantee.
    """
    try:
        proc = subprocess.run(  # nosec B603 - argv list, no shell
            [git, "-C", repo, "diff", "--name-only", ref, "--", _FRONTEND_SUBDIR],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or (proc.stdout or b"").strip():
        # Non-zero: the comparison could not be made, so nothing is established.
        # Non-empty: at least one path under the frontend half differs.
        return None
    node_modules = Path(repo) / _FRONTEND_SUBDIR / "node_modules"
    try:
        if not any(node_modules.iterdir()):
            return None
    except OSError:
        # Absent, a file, or unreadable -- in every case there is no tree to
        # treat as evidence, so probe.
        return None
    return (
        f"skipped the install: the incoming ref changes nothing under "
        f"{_FRONTEND_SUBDIR}/ and its node_modules is populated, so no new "
        "resolution is arriving and no new bundle is owed"
    )


#: File (under ``static/dist``) holding the git tree id of ``website/`` the
#: staged bundle was built from. Written by :func:`frontend._write_build_source_fingerprint`.
_BUILD_SOURCE_FINGERPRINT = "kirocrew-build-source.txt"
#: Where the staged bundle lives relative to the repo root.
_STATIC_DIST = ("src", "kiro_crew", "static", "dist")


def _frontend_worktree_clean(git: str, repo: str) -> bool:
    """True only when ``website/`` has NO uncommitted change, untracked included.

    ``git status --porcelain --untracked-files=normal -- website`` lists tracked
    modifications AND new untracked files (as ``?? path``); an empty result means
    the working subtree equals the committed one. Non-empty, a non-zero exit, a
    missing git, or a timeout all return False, so the skip is refused on any
    doubt -- the build then runs, the safe direction. This mirrors the stamp-time
    guard: the fingerprint is only WRITTEN when this holds, and here it is
    re-checked before the fingerprint is TRUSTED, so an untracked file added
    between build and skip cannot ride through.
    """
    try:
        proc = subprocess.run(  # nosec B603 - argv list, no shell
            [
                git,
                "-C",
                repo,
                "status",
                "--porcelain",
                "--untracked-files=normal",
                "--",
                _FRONTEND_SUBDIR,
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0 and not (proc.stdout or b"").strip()


def _frontend_tree_complete(npm: str, repo: str) -> bool:
    """True only when ``website/node_modules`` fully satisfies the lockfile.

    ``_install_already_proven``'s "populated" test is a NON-EMPTY directory,
    which a partial tree passes. Skipping the real ``npm ci`` on a partial tree
    would leave the sync succeeding on incomplete dependencies, so the build-skip
    needs a completeness check that a bare-populated one cannot give.

    ``npm ls --all`` walks the installed tree against the lockfile and exits
    non-zero (``ELSPROBLEMS``, "missing: ...") when any package is absent or
    invalid; it exits 0 only when the tree is complete. It runs no lifecycle
    scripts, writes nothing, and needs no network -- measured ~1s. Anything other
    than a clean exit 0 (a non-zero code, a missing npm, a timeout) returns
    False, so the unknown case rebuilds rather than trusting the tree.
    """
    try:
        proc = subprocess.run(  # nosec B603 - argv list, no shell
            [npm, "ls", "--all"],
            cwd=str(Path(repo) / _FRONTEND_SUBDIR),
            capture_output=True,
            timeout=120,
            check=False,
            env={**os.environ, "npm_config_update_notifier": "false"},
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def _frontend_build_already_current(git: str, npm: str, repo: str, ref: str) -> str | None:
    """Reason to SKIP the frontend reinstall AND rebuild, or ``None`` to run them.

    STRICTLY STRONGER than :func:`_install_already_proven`, and it must be: that
    predicate governs whether the pre-merge PROBE pays for a rehearsal, where a
    stale-but-populated tree is benign because a refusal merely lands one step
    later. Skipping the real ``npm ci`` AND ``npm run build`` is not benign in
    the same way -- a wrongly skipped build leaves ``static/dist`` holding a
    bundle that was never built from the current source, which surfaces days
    later as a stale-frontend bug. So this requires everything
    :func:`_install_already_proven` does, PLUS two proofs it does not: that the
    installed tree is COMPLETE (:func:`_frontend_tree_complete`, closing the
    partial-``node_modules`` gap), and that the staged bundle was built from the
    source the sync will end up with (the fingerprint below).

    The extra proof closes a concrete hole: a prior FRONTEND sync can
    merge new ``website/`` source and then have its ``npm ci`` fail, at which
    point the runner's transaction restores the OLD ``node_modules``. From then
    on the subtree stops changing, so ``_install_already_proven`` would skip --
    but ``static/dist`` was built from the OLD source and the merge landed the
    NEW one. The fingerprint distinguishes them: it records the git tree id of
    ``website/`` the staged bundle was built from, and this requires it to equal
    the incoming ref's ``website/`` tree. In that failed-sync case the two differ
    (old built tree vs new merged tree), so the skip is refused and the build
    runs. Any uncertainty -- no fingerprint, an unreadable one, a git that cannot
    resolve the ref's tree -- returns ``None`` and the build runs, the safe
    direction.
    """
    base = _install_already_proven(git, repo, ref)
    if base is None:
        return None
    # The install-proven check compares only TRACKED files (`git diff`), so an
    # untracked website/ file added since the last build -- one the staged bundle
    # cannot contain -- would not move the comparison. Require the working tree
    # to be clean INCLUDING untracked files before skipping, mirroring the
    # stamp-time guard in frontend._write_build_source_fingerprint: the two
    # together mean a skip implies the tree that produced the bundle and the tree
    # now on disk are the same, tracked and untracked alike.
    if not _frontend_worktree_clean(git, repo):
        return None
    # The install-proven check only requires node_modules to be NON-EMPTY. A
    # partial tree (an interrupted install) passes that, so verify completeness
    # against the lockfile before skipping the real npm ci -- otherwise the sync
    # could succeed on incomplete dependencies.
    if not _frontend_tree_complete(npm, repo):
        return None
    fingerprint_path = Path(repo).joinpath(*_STATIC_DIST) / _BUILD_SOURCE_FINGERPRINT
    try:
        built_tree = fingerprint_path.read_text(encoding="utf-8").strip()
    except OSError:
        # No fingerprint (a bundle built before this feature, or a stamp that
        # failed to write) proves nothing about what the dist was built from, so
        # rebuild rather than trust a populated tree alone.
        return None
    if not built_tree:
        return None
    try:
        proc = subprocess.run(  # nosec B603 - argv list, no shell
            [git, "-C", repo, "rev-parse", f"{ref}:{_FRONTEND_SUBDIR}"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    incoming_tree = (proc.stdout or b"").decode(errors="replace").strip()
    if not incoming_tree or incoming_tree != built_tree:
        # The staged bundle was built from a DIFFERENT website/ tree than the one
        # the sync will end up with -- the stale-after-failed-frontend-sync case.
        # Rebuild.
        return None
    return (
        f"skipped the frontend reinstall and rebuild: the incoming ref changes "
        f"nothing under {_FRONTEND_SUBDIR}/, its node_modules is populated and "
        "complete against the lockfile, and the staged bundle was built from this "
        "exact source tree, so no new resolution is arriving and no new bundle is "
        "owed"
    )


def probe(
    *,
    git: str,
    npm: str,
    repo: str,
    ref: str,
    timeout: int = 900,
) -> tuple[int, str]:
    """Report whether *ref*'s lockfile is installable. Returns (code, detail).

    Runs a REAL script-free install in a scratch directory, so it neither reads
    nor writes the checkout's own ``node_modules``. Both halves of that matter:
    a dry run never fetches, so it cannot answer the question at all; and an
    already-populated tree would make even a real install report only the delta
    against it, passing a lockfile that a delete-first ``npm ci`` cannot
    install.
    """
    # Creating the scratch directory is the FIRST thing that can fail on a full
    # or unwritable filesystem, and an uncaught OSError here would kill the step
    # with a traceback and no classified cause -- so the dashboard would be back
    # to showing whatever the last output line happened to be, which is the
    # defect this module exists to remove.
    tmp, failure = _make_scratch(git, repo)
    if tmp is None:
        # _make_scratch always pairs a missing path with a classified failure;
        # the fallback keeps the type honest without asserting.
        return failure or (EXIT_FAILED, "could not create a scratch directory")
    try:
        failure = _extract(git, repo, ref, _FRONTEND_SUBDIR, tmp)
        if failure:
            return failure
        # Asked AFTER the extraction, which is deliberate rather than leftover:
        # `_extract` is what establishes that the incoming ref carries a readable
        # lockfile at all, and that precondition should hold before any verdict
        # is returned. Three small `git show` calls are nothing against the
        # install being skipped.
        proven = _install_already_proven(git, repo, ref)
        if proven:
            return EXIT_OK, proven
        try:
            proc = subprocess.run(  # nosec B603 - argv list, no shell
                [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                cwd=str(tmp),
                capture_output=True,
                timeout=timeout,
                check=False,
                env={**os.environ, "npm_config_update_notifier": "false"},
            )
        except subprocess.TimeoutExpired:
            return EXIT_TRANSIENT, f"probe timed out after {timeout}s"
        except OSError as exc:
            return EXIT_FAILED, f"could not run npm: {exc}"
        if proc.returncode == 0:
            return EXIT_OK, ""
        blob = "\n".join(
            (
                (proc.stdout or b"").decode(errors="replace"),
                (proc.stderr or b"").decode(errors="replace"),
            )
        )
        code = classify(blob)
        return code, _first_error_line(blob) or f"npm exited {proc.returncode}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _first_error_line(blob: str) -> str:
    """The first line that names the failure, for the operator-facing detail.

    npm prints its diagnosis FIRST and its log-file pointer LAST, which is
    exactly why the dashboard's "last output line" was uninformative. Taking
    the first error-ish line inverts that. The log-pointer line is skipped
    explicitly so it can never win when it is the only match.
    """
    for raw in blob.splitlines():
        line = raw.strip()
        if not line or "complete log of this run" in line:
            continue
        low = line.lower()
        if low.startswith(("npm error", "npm err!", "error:")) or " error " in low:
            return line[:400]
    return ""


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: the sync runs this as one step of the Pull+Build run."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--git", required=True)
    ap.add_argument("--npm", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--ref", required=True)
    # When set, and only when the incoming ref proves the frontend install is
    # already on disk, exit EXIT_FRONTEND_SKIP instead of EXIT_OK. The runner
    # reads that verdict off THIS step's exit code -- a channel it already trusts
    # only from this step's label -- and skips the later npm ci and build+stage
    # steps. It is a flag, not a value: the verdict cannot be smuggled in from
    # outside, and a worktree-run step exiting 48 is demoted to a plain failure.
    # This is the same window the probe uses (after fetch pinned --ref, before
    # merge), which is the only point where "does the incoming ref touch the
    # frontend?" has a correct answer.
    ap.add_argument("--emit-frontend-skip", action="store_true")
    # --subdir and --timeout were CLI flags no caller passed. The subdir is now
    # _FRONTEND_SUBDIR and the timeout is probe()'s own default, so the surface
    # matches the one real invocation.
    args = ap.parse_args(argv)
    if args.emit_frontend_skip:
        # Asked with the SAME (git, repo, ref) the probe uses. This is the
        # STRONGER predicate: it requires the unchanged subtree and populated
        # node_modules the install-skip needs, PLUS proof (a build fingerprint)
        # that the staged bundle was built from the source the sync ends up with
        # -- so it cannot skip the rebuild on a tree left stale by a prior
        # failed frontend sync. Any uncertainty returns None, so the frontend
        # steps run: the unknown case pays the rebuild.
        proven = _frontend_build_already_current(args.git, args.npm, args.repo, args.ref)
        if proven is not None:
            print(f"{DETAIL_PREFIX}{proven}", flush=True)
            return EXIT_FRONTEND_SKIP
    code, detail = probe(
        git=args.git,
        npm=args.npm,
        repo=args.repo,
        ref=args.ref,
    )
    if code == EXIT_OK:
        # The detail carries the SKIP reason when the install was not needed, and
        # is empty when it ran and passed. Printing it rather than the generic
        # line is what keeps a skipped probe visible: an operator reading the run
        # log should never have to infer from a missing pause that the safety
        # step did not run.
        print(f"{DETAIL_PREFIX}{detail or 'incoming lockfile is installable'}", flush=True)
        return EXIT_OK
    # The DIAGNOSIS travels as the exit code, which the gateway maps through
    # explain_exit(). Only a human-readable detail goes to stdout, and it is log
    # text -- deliberately NOT an in-band marker the gateway promotes, because
    # this same stream carries worktree-controlled build output that could print
    # any marker it liked and then fail.
    print(f"{DETAIL_PREFIX}{explain_exit(code)}", flush=True)
    if detail:
        print(f"{DETAIL_PREFIX}{detail}", flush=True)
    return code


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
