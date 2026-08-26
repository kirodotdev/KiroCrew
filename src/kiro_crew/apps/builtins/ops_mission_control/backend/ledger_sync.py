"""Git-native ledger sync — shared memory for a team, with no server.

Team knowledge belongs in a git-tracked, append-only JSONL file — one JSON object per
line with an author and a timestamp. That shape is not an accident: an append-only JSONL
file in a git repo *is* a distributed shared-memory primitive, provided two writers who
learn the same thing produce the same bytes.

This app's ledger already has that property — ids are content-addressed over
`(pattern, fix)`, so two people who independently learn one lesson write one id. What
was missing was the transport. This module is the transport, and deliberately nothing
more:

- **No new data model.** The synced artifact is `ledger.jsonl` exactly as written
  locally. A teammate's copy is readable by an instance that has never heard of sync.
- **No server.** Git is the coordination substrate; GitHub (or any remote) is the
  shared place. Identity and access control are the remote's problem, which is the
  point — a team that can already share a repo can already share memory.
- **Only the ledger.** NOT the dispatch index. The index is last-writer-wins on a
  shared key, so syncing it would silently let two instances believe they each own an
  incident. Cross-instance claim arbitration is a separate contract that has to be
  designed, not a file copy. See the module spec.

**Conflicts are expected and already handled.** Verified against a real `git merge` of
two divergent ledgers: git DOES conflict (both branches append to the same region), so
this is not the "content addressing means no conflicts" story. What content addressing
buys is that the conflicted file is *reconcilable* — `ledger.read_entries` skips the
`<<<<<<<` markers as malformed lines and merges duplicate ids. So the app stays correct
mid-merge, and `resolve_conflict` finishes the job by rewriting the file from the
already-reconciled entries.

**Pull before you match, push after you learn** — the INTENDED ordering, and stated here as
intent because the wiring does not yet deliver it. Pulling before a ledger match is what
makes a teammate's lesson available to this investigation, and pushing after recording one is
what makes yours available to theirs.

What actually runs is coarser: the only caller of this transport is the daily
``POST /ledger/hygiene`` pass on the ``primary`` tier, so exchange happens once a day on one
box rather than around each match. Recorded here rather than left as an aspiration a reader
would mistake for a description — see ``sync_safely`` for what the gap costs, which is more
than latency because ``rotation.yaml`` rides the same repo.

See ``docs/system-specs/modules/ops-mission-control.md`` § Ledger sync.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, policy_store
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config
from kiro_crew.sandbox import create_subprocess_limited, sandboxed_spawn_argv
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_GIT_BINARY = "git"

#: The identity this app commits under, supplied on the argv of every git invocation.
#:
#: These commits are the APP's, not the operator's: a ledger sync is machine-to-machine,
#: it runs unattended, and the repository lives under the data home where nobody edits it
#: by hand. Naming the app is therefore the honest attribution -- and it is also what makes
#: the behaviour the same on every host. Relying on the ambient `user.email` meant that on
#: a host with no git identity configured (a fresh container, a CI runner, a locked-down
#: image) `git commit` refused with "Author identity unknown", the push that followed had
#: nothing to send, and the operator saw only "push errored" with no reason.
#:
#: `.invalid` is the reserved TLD for exactly this (RFC 2606), so the address cannot route.
_COMMIT_IDENTITY = (
    "-c",
    "user.name=Kiro Crew ops-mission-control",
    "-c",
    "user.email=ops-mission-control@kirocrew.invalid",
)

#: Config keys. A remote URL is not a credential (auth is the remote's job — an SSH
#: key or a `gh` login the operator already has), so these live in plain app config.
_ENABLED_KEY = "ledger_sync_enabled"
_REMOTE_KEY = "ledger_sync_remote"
_BRANCH_KEY = "ledger_sync_branch"

DEFAULT_BRANCH = "main"

#: Branch names we will hand to ``git``, mirroring ``routes._SAFE_BRANCH_RE``.
#:
#: Duplicated rather than imported, deliberately: ``routes`` is the HTTP door, and it is
#: not the only door. ``set_settings`` is a plain function the tests (and any in-process
#: caller) invoke directly, and ``data/config.json`` is a hand-editable file. So the
#: API-layer check cannot be the only validation before a value reaches a ``git`` argv.
#: Importing ``routes`` from here would also invert the dependency — the transport must
#: not know about the web layer.
#:
#: The hazard is concrete, not theoretical: ``git init -b '-x'`` cheerfully creates
#: ``refs/heads/-x``, and ``git symbolic-ref HEAD refs/heads/--upload-pack=evil`` succeeds
#: with no validation at all. ``git branch -m --`` does refuse those, which is why the
#: alignment path below uses it — but ``branch()`` also feeds fetch/merge/push refspecs,
#: where an option-like value is a worse surprise than a clear fallback.
_SAFE_BRANCH_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]{0,98}")

#: Why the local HEAD is not on the configured branch, when that could not be fixed.
#: Module-level rather than threaded through ``_ensure_repo``'s ``(bool, str)`` return,
#: because a refusal to align must NOT read as "the repo is unusable" — pull and push
#: still work through their explicit refspecs, exactly as they did before this alignment
#: existed. ``status()`` reads it so the operator hears about it without pull/push
#: changing behaviour.
_align_refusal: str = ""

#: Wall-clock cap per git invocation. A hung fetch against an unreachable remote must
#: not stall the dispatch heartbeat, which is the caller.
GIT_TIMEOUT_SECS = 30.0

#: Marker prefixes git writes into a conflicted file. Listed here rather than inferred
#: so ``resolve_conflict`` and ``ledger.read_entries`` agree on what to ignore.
CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


def configured() -> bool:
    """True when the operator enabled sync AND named a remote."""
    return bool(policy_store.get(_ENABLED_KEY)) and bool(remote())


def remote() -> str:
    """The git remote the shared ledger is pushed to — from the FENCED store.

    An agent that could rewrite this would redirect the team's accumulated incident knowledge
    to a repo it controls, and `POST /ledger/hygiene` (which the agent's own hygiene cron
    calls) is what performs the push. Found by auditing for the class of the autonomy-ceiling
    finding. See `policy_store.OPERATOR_ONLY_KEYS`.
    """
    return str(policy_store.get(_REMOTE_KEY, "") or "").strip()


def branch() -> str:
    """The configured branch, or ``main``. Never returns something git could misread.

    Falls back rather than raising: this is called on the ``/state`` hot path and from
    inside git argv construction, and a hard failure there would take out the whole card
    over a typo in a hand-edited config file. The fallback is logged at WARNING because
    silently syncing a branch the operator did not name is its own kind of lie.
    """
    configured_name = str(read_config().get(_BRANCH_KEY, "")).strip()
    if not configured_name:
        return DEFAULT_BRANCH
    if not _SAFE_BRANCH_RE.fullmatch(configured_name):
        logger.warning(
            "ops-mission-control: %s=%r is not a usable git ref (letters, digits and "
            "._/- only, not starting with '-'); syncing %s instead",
            _BRANCH_KEY,
            configured_name[:120],
            DEFAULT_BRANCH,
        )
        return DEFAULT_BRANCH
    return configured_name


def _head_branch() -> str:
    """The branch ``.git/HEAD`` actually points at, or "" when there is not one.

    Returns "" for a detached HEAD (the file holds a bare sha), for a repo that has not
    been initialized, and for any read failure — including a ``.git`` FILE rather than a
    directory, which is a worktree/submodule gitdir pointer.

    A FILE READ, not a ``git`` spawn, and that is the load-bearing choice: ``status()`` is
    synchronous and sits inline on ``/state``, the dashboard's hot poll. ``.git/HEAD`` is a
    single line git rewrites atomically, so reading it costs nothing and cannot block.

    Never raises. ``routes._ledger_sync_status`` catches any throw from ``status()``,
    logs ``.exception`` and falls back to a blank card — so a raising probe would both spam
    the gateway log on every poll and HIDE the very state it exists to report.
    """
    try:
        line = (_repo_root() / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not line.startswith("ref:"):
        return ""  # a bare sha — detached HEAD
    ref = line[4:].strip()
    prefix = "refs/heads/"
    return ref[len(prefix) :] if ref.startswith(prefix) else ""


def status() -> dict[str, Any]:
    """Why sync is or is not usable — surfaced in Settings.

    Distinguishes the failure modes because they need different fixes: off (flip the
    toggle), no remote (enter one), and no git repo yet (it gets created on first sync).

    CONFLICT STATE WAS MISSING, AND IT WAS THE ONE STATE THAT LIES. ``push`` REFUSES
    outright while ``rotation.yaml`` holds conflict markers (see the guard below), but it
    said so only to the log and to a SEL audit line: ``sync_safely`` swallows the refusal
    into a ``logger.warning``, and the daily hygiene handler drops it into a response
    nobody reads. Meanwhile this function reported "Syncing …" — so an operator whose
    schedule conflicted watched a card claim sync was working while every single push was
    refused. The cost is not cosmetic: nothing new reaches the team's ledger, and the
    schedule that gates who picks up work stays unparseable for everyone who pulls it.
    Both detectors already exist, are cheap file reads, and never raise, so there was no
    reason for this to be invisible.

    "SYNCING ON BRANCH <b>" WAS A SECOND OVERSTATED CLAIM, and it was true nowhere.
    ``git init`` was run with no ``-b``, so git picked its own default (``master``) and  # wokeignore:rule=master
    nothing ever moved HEAD onto the configured branch — ``branch()`` was used ONLY inside
    fetch/merge/push refspecs. Found by inspecting the author's live install: config said
    ``main``, ``.git/HEAD`` said ``master``, and there was no ``[branch]`` section at all.  # wokeignore:rule=master
    Sync worked, by accident of those explicit refspecs, while this card named a ref the
    local repo was not on. The cost landed on the operator: with no upstream, the two
    obvious recovery commands both fail outright (``git pull`` → "no tracking information
    for the current branch"; ``git push`` → "the current branch master has no upstream  # wokeignore:rule=master
    branch") — and a conflicted ``rotation.yaml``, which the push guard above REFUSES, is
    exactly the case that has to be fixed by hand in that directory.

    ``local_branch`` / ``branch_matches`` / ``detached`` exist so the card can tell the
    operator which of those it is. ``initialized`` deliberately stays gated on
    ``.is_dir()``, so a ``.git`` FILE (a worktree or submodule gitdir pointer) reads as
    uninitialized exactly as it did before.
    """
    enabled = bool(policy_store.get(_ENABLED_KEY))
    url = remote()
    initialized = (_repo_root() / ".git").is_dir()
    ledger_conflict = has_conflict()
    schedule_conflict = schedule_has_conflict()
    wanted_branch = branch()
    local_branch = _head_branch() if initialized else ""
    detached = initialized and not local_branch
    # An uninitialized repo has nothing to disagree with, so it MATCHES. Reporting a
    # mismatch there would put a warning on the ordinary pre-first-sync state and train the
    # operator to ignore the one field that means something.
    branch_matches = (not initialized) or local_branch == wanted_branch
    if not enabled:
        detail = "Off. Turn on to share the knowledge ledger with your team over git."
    elif not url:
        detail = "No remote set — enter a git URL (SSH or HTTPS) your team can push to."
    elif schedule_conflict:
        # Named as a refusal rather than as a warning because that is literally what the
        # push path does. Anything softer would leave the operator waiting for a sync that
        # is never going to happen.
        detail = (
            f"{_SCHEDULE_FILENAME} holds git conflict markers, so every push is refused "
            "until it is resolved by hand — nothing new is reaching your team."
        )
    elif ledger_conflict:
        # Reconcilable, not fatal: entries are content-addressed, ``read_entries`` skips
        # markers, and the next push rewrites the file from the reconciled union. Said out
        # loud anyway so a conflict an operator can see in git is not a mystery here.
        #
        # ``_where`` rather than a hardcoded "on branch <b>": this branch OUTRANKS the
        # branch-mismatch branch below, so if the sentence claimed the local repo was on the
        # configured branch, a conflicted ledger would hide the mismatch entirely.
        detail = (
            "The ledger holds git conflict markers. Entries are still readable and the "
            f"next sync reconciles them; {_where(url, wanted_branch, branch_matches)}."
        )
    elif not initialized:
        detail = f"Ready. The repo is created on the first sync ({url})."
    elif detached:
        # Reported, never repaired. A detached HEAD here means a merge or rebase went
        # sideways, and moving refs out from under one can lose the operator's in-progress
        # work — so alignment refuses too (``git branch -m`` refuses outright anyway).
        detail = (
            f"Publishing to {url} on branch {wanted_branch}, but this repo's HEAD is "
            "detached — no local branch is checked out, so git commands you run here have "
            f"no upstream. Finish or abort the merge or rebase, then switch to "
            f"{wanted_branch}. Left alone on purpose: moving refs under a detached HEAD "
            "can lose work in progress."
        )
    elif not branch_matches:
        detail = (
            f"Publishing to {url} on branch {wanted_branch} through an explicit refspec, "
            f"but this repo is on {local_branch} — so a plain git pull or git push in the "
            "ledger directory fails with no upstream configured. "
        ) + (_align_refusal or f"The next sync moves it onto {wanted_branch}.")
    else:
        detail = f"Syncing {url} on branch {wanted_branch}."
    return {
        "enabled": enabled,
        "remote": url,
        # The CONFIGURED branch — what the operator asked for. Kept under this key because
        # that is what every existing consumer already means by it.
        "branch": wanted_branch,
        # What ``.git/HEAD`` actually points at. The two are separate keys because they
        # genuinely diverged on a live install, and collapsing them is the bug.
        "local_branch": local_branch,
        "branch_matches": branch_matches,
        "detached": detached,
        "initialized": initialized,
        "ready": enabled and bool(url),
        "conflict": ledger_conflict,
        "schedule_conflict": schedule_conflict,
        "detail": detail,
    }


def _where(url: str, wanted_branch: str, branch_matches: bool) -> str:
    """How to name where sync publishes, in a way that is true in both branch states.

    Splitting this out keeps one wording in one place: "syncing X on branch b" is a claim
    about the LOCAL repo as well as the remote, and it is false whenever HEAD is elsewhere.
    """
    if branch_matches:
        return f"syncing {url} on branch {wanted_branch}"
    return f"publishing to {url} onto branch {wanted_branch} through an explicit refspec"


def set_settings(
    *,
    enabled: bool | None = None,
    remote_url: str | None = None,
    branch_name: str | None = None,
) -> None:
    """Persist the operator's choice.

    The DESTINATION keys (``enabled``, ``remote``) go to the keystone policy store: an agent
    that could rewrite the remote would redirect the team's ledger push to a repo it controls.
    ``branch`` stays in plain config — it selects a ref inside a remote the operator already
    chose, is shape-validated at the route, and cannot move data off-box.
    """
    from kiro_crew.apps.builtins.ops_mission_control.backend.providers import set_top_level

    if enabled is not None:
        policy_store.put(_ENABLED_KEY, bool(enabled))
    if remote_url is not None:
        policy_store.put(_REMOTE_KEY, remote_url.strip())
    if branch_name is not None:
        # `set_top_level`, not an open-coded read/write: it holds `_ConfigLock` across the
        # read-modify-write, so a concurrent settings PUT cannot drop this key (or have its own
        # dropped). The bare sequence here bypassed that lock. Found in review.
        set_top_level(_BRANCH_KEY, branch_name.strip())


def _repo_root() -> Path:
    """The ledger's own directory. The ledger file IS the repo's content.

    Syncing the directory that already holds ``ledger.jsonl`` avoids a copy step and
    the drift a copy invites. Note this directory also holds other app data, so
    ``_ensure_repo`` writes a ``.gitignore`` that tracks the ledger and nothing else —
    the dispatch index must never be committed (see the module docstring).
    """
    return ledger.ledger_path().parent


async def _git(*args: str) -> tuple[int, str, str]:
    """Run one git command in the ledger directory, sandboxed.

    Routed through ``sandboxed_spawn_argv`` for OS filesystem isolation, because the
    remote URL and branch come from config an agent can influence and git reads its own
    config files on the way. This is the chokepoint ``test/test_spawn_audit.py`` requires.
    Never raises on a non-zero exit — the caller decides what that means.

    Resource limits come from ``create_subprocess_limited``, NOT from
    ``preexec_fn=resource_limit_preexec()``. This is an ASYNC spawn, and a ``preexec_fn``
    there forces a plain ``fork()`` of the multi-GB, ~118-thread gateway and runs Python in
    the child before ``exec``: a lock another thread held at fork time can never be released
    in the child, ``Popen._execute_child`` then blocks the event loop in an unbounded
    ``os.read`` with no await point for a timeout to reach, and the orphan keeps a duplicate
    of every inherited fd — the dashboard's listening socket included. The shim applies the
    same limits after ``exec`` instead. Caught by ``test/test_spawn_preexec_guard.py``
    (issue #935), a core gate this app had not been run against.
    """
    # Identity on the ARGV, not in the repo's config: `-c` beats config, so it holds
    # even against a repo-local `user.email` an agent could have written, and it needs
    # no `git config` write of our own. Passed on every verb -- the read-only ones
    # ignore it, and scoping it to `commit` would miss the next verb that makes one.
    argv, env, cleanup = sandboxed_spawn_argv([_GIT_BINARY, *_COMMIT_IDENTITY, *args])
    try:
        proc = await create_subprocess_limited(
            *argv,
            cwd=str(_repo_root()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "", f"git {args[0]} timed out after {GIT_TIMEOUT_SECS}s"
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    except OSError as exc:
        return 127, "", f"could not run git: {exc}"
    finally:
        # The third value is a temp-profile PATH, not a callable — same handling as
        # the github_issues adapter. Assuming it was a cleanup function is what mypy
        # caught ("str" not callable).
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)


async def _align_branch() -> str:
    """Put HEAD on the configured branch and make it track the remote's.

    Returns "" on success, or a short operator-facing reason when it deliberately refused.
    Never treated as a sync failure by the caller: pull and push have always worked through
    explicit refspecs, and this fix must not be able to make the app worse than it was.

    ``git branch -m --`` is the primitive, and every one of these properties was verified
    against real git rather than assumed:

    - rc=0 on an UNBORN branch (fresh ``git init``, no commit yet) — it just rewrites
      ``.git/HEAD``. That matters because a fresh install has no commit.
    - rc=0 on a born branch, keeping the SAME sha. No content moves.
    - Leaves a DIRTY tree (modified + untracked files) completely untouched.
    - Preserves an IN-PROGRESS conflicted merge: ``MERGE_HEAD`` survives and the index
      stays ``UU``. A pull that conflicted mid-way therefore is not disturbed.

    ``git checkout``/``git switch`` has none of those: against a dirty tree and a divergent
    ref it auto-merges and can conflict, which would corrupt the working ledger.

    ``--`` plus git's own ref-name validation is the argv guard. Verified:
    ``git branch -m -- '--upload-pack=evil'`` fails rc=128, while
    ``git symbolic-ref HEAD refs/heads/--upload-pack=evil`` succeeds with no validation —
    which is why the symbolic-ref shortcut is not used here.
    """
    wanted = branch()

    # DETACHED HEAD: report, do not touch. A detached HEAD in this directory means a merge
    # or rebase went sideways, and moving refs under it can lose the operator's state.
    # ``git branch -m`` refuses anyway ("cannot rename the current branch while not on
    # any"), so this is naming the reason rather than adding a restriction.
    rc, out, _ = await _git("symbolic-ref", "--short", "HEAD")
    current = out.strip()
    if rc != 0 or not current:
        return (
            "This repo's HEAD is detached, so it was left alone — finish or abort the "
            f"merge or rebase in progress, then run: git switch {wanted}"
        )

    if current != wanted:
        # A DIFFERENT branch of that name ALREADY EXISTS. Refuse. ``git branch -M`` would
        # succeed here by DELETING that ref and every commit only it holds — two divergent
        # lines of ledger work silently collapsed into one, which is precisely the
        # lesson-stranding this whole change exists to stop. Same register as the push
        # guard above: refuse, and name the cost and the manual command.
        rc, _, _ = await _git("show-ref", "--verify", "--quiet", f"refs/heads/{wanted}")
        if rc == 0:
            reason = (
                f"A different local branch named {wanted} already exists, so this repo was "
                f"left on {current} rather than guess which history to keep. Merge them by "
                f"hand: git switch {wanted} && git merge {current}"
            )
            logger.warning("ops-mission-control: ledger sync branch not aligned — %s", reason)
            return reason
        rc, _, err = await _git("branch", "-m", "--", wanted)
        if rc != 0:
            reason = (
                f"Could not move this repo onto {wanted}: {err.strip()[:160]}. It stays on "
                f"{current}; sync still publishes through an explicit refspec."
            )
            logger.warning("ops-mission-control: ledger sync branch not aligned — %s", reason)
            return reason

    # TRACKING, written explicitly and AFTER the rename. The order is load-bearing and the
    # trap is easy to miss: ``git branch -m`` migrates ``branch.<old>.remote`` but leaves
    # ``branch.<old>.merge`` pointing at the OLD ref, so renaming master → main leaves git  # wokeignore:rule=master
    # holding ``branch.main.merge = refs/heads/master``. Verified.  # wokeignore:rule=master
    #
    # ``git config``, NOT ``git branch --set-upstream-to``: the latter fails in both of the
    # ordinary first-sync states — no ``origin/<branch>`` fetched yet ("the requested
    # upstream branch 'origin/main' does not exist") and an unborn branch ("no commit on
    # branch 'main' yet"). An empty remote is the NORMAL way a team starts, so a tool that
    # only works once the remote has commits is the wrong tool. ``git config`` works in
    # both, and it is the same two keys ``--set-upstream-to`` would have written.
    await _git("config", "--", f"branch.{wanted}.remote", "origin")
    await _git("config", "--", f"branch.{wanted}.merge", f"refs/heads/{wanted}")
    return ""


async def _ensure_repo() -> tuple[bool, str]:
    """Initialize the repo and its remote if needed. Idempotent."""
    root = _repo_root()
    root.mkdir(parents=True, exist_ok=True)

    # Track ONLY the shared-knowledge files: the ledger and the on-call schedule. The
    # dispatch index, provider config, and incident logs live in this same directory and
    # must never be pushed — the index because it is not merge-safe, the rest because it
    # is local state (and config could name a log group an operator considers private).
    #
    # ``rotation.yaml`` is here because a schedule only works if every teammate reads the
    # same one; a locally-written file that never syncs is worse than no schedule, since
    # it looks configured while disagreeing with everyone else. It is small, human-edited,
    # and text — the same merge profile as the ledger.
    # Off the loop. `_ensure_repo` is awaited from `sync_safely` DIRECTLY on the event loop
    # (not via `to_thread`), and these are synchronous file ops. The `.gitignore` is tiny and
    # fixed-size, so the stall is microseconds rather than the hundreds of ms a ledger parse
    # costs — but it is the same class the off-loop guard exists to keep out, and "small today"
    # is how the ledger reads earned their inline calls too. Bundled into one worker hop so the
    # read-compare-write is a single offload rather than two.
    def _sync_gitignore() -> None:
        gitignore = root / ".gitignore"
        wanted = "*\n!.gitignore\n!ledger.jsonl\n!rotation.yaml\n"
        if not gitignore.exists() or gitignore.read_text(encoding="utf-8") != wanted:
            gitignore.write_text(wanted, encoding="utf-8")

    await asyncio.to_thread(_sync_gitignore)

    if not (root / ".git").is_dir():
        rc, _, err = await _git("init", "-q")
        if rc != 0:
            return False, f"git init failed: {err.strip()[:200]}"

    rc, out, _ = await _git("remote", "get-url", "origin")
    url = remote()
    if rc != 0:
        rc, _, err = await _git("remote", "add", "origin", url)
        if rc != 0:
            return False, f"git remote add failed: {err.strip()[:200]}"
    elif out.strip() != url:
        # The operator changed the remote in Settings; follow it rather than silently
        # continuing to sync the old one.
        rc, _, err = await _git("remote", "set-url", "origin", url)
        if rc != 0:
            return False, f"git remote set-url failed: {err.strip()[:200]}"

    # Put HEAD on the configured branch and give it an upstream. One call site covers both
    # directions because ``pull`` and ``push`` both come through here, and it also handles
    # the operator CHANGING the branch later: the next sync re-runs this and follows them,
    # exactly like the remote handling above.
    #
    # NOT allowed to fail this function. Alignment is a usability fix — it is what makes a
    # plain ``git pull`` in the ledger directory work, which is how a conflicted
    # ``rotation.yaml`` gets fixed by hand. Sync itself has never depended on it. Turning a
    # refusal into ``(False, ...)`` would stop publishing over a condition that never
    # stopped publishing before, so the reason is stashed for ``status()`` instead.
    global _align_refusal
    _align_refusal = await _align_branch()
    return True, ""


def has_conflict() -> bool:
    """True when the ledger file currently holds git conflict markers."""
    path = ledger.ledger_path()
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(line.startswith(CONFLICT_MARKERS) for line in text.splitlines())


#: The on-call schedule. Named here rather than imported from ``schedule_file`` to keep
#: this module free of a dependency on a provider (the transport must not know which
#: rotation source exists); ``TRACKED_FILES`` already carries the same literal.
_SCHEDULE_FILENAME = "rotation.yaml"

#: The shared knowledge file — the one artifact this module publishes.
_LEDGER_FILENAME = "ledger.jsonl"


def _credential_bearing_lines() -> list[int]:
    """1-based line numbers in ``ledger.jsonl`` that look like they carry a credential.

    The pre-push half of the ledger-redaction defence. ``POST /ledger`` redacts on the
    WRITE path, which is the right place — the entry is on local disk and in the vector
    index long before any sync runs. This catches what that cannot: an entry written by an
    older build, or one that reached the file by some path other than that route.

    Uses the UNION of two detectors, because neither is a superset of the other and this is
    the last gate before bytes leave the machine:

    - ``security.get_credential_patterns()`` — the core accessor, which exists specifically so
      a downstream scan cannot be silently turned into a no-op by a rename, and which means a
      pattern added for any other egress site starts protecting this one too. It carries the
      AKIA/ASIA shapes.
    - ``secrets.redact_tokens`` — this app's own detector, which knows the PROVIDER credential
      shapes the core has no reason to: a `Bearer` header, a prefixed Datadog application key
      (`ddapp_…`), a PagerDuty token.

    Review found the omission and was right; measured before fixing, the gap runs in BOTH
    directions — the core patterns miss `ddapp_…` while `redact_tokens` misses
    `AKIAIOSFODNN7EXAMPLE`. Either detector alone therefore lets a real credential through, so
    a line is credential-bearing if EITHER flags it. A legacy row (written by an older build,
    or by some path other than `POST /ledger`) holding a provider token would otherwise have
    been committed to the team's shared remote.

    Returns line NUMBERS, never the matched text: the caller logs this to SEL and to the
    operator's console, and echoing the secret into either would be the leak this function
    exists to prevent. Never raises — an unreadable ledger is reported as clean because
    the commit/push that follows will fail on its own and say why, whereas raising here
    would turn a missing file into an unexplained sync failure.
    """
    from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import redact_tokens
    from kiro_crew.security import get_credential_patterns

    try:
        text = ledger.ledger_path().read_text(encoding="utf-8")
    except OSError:
        return []
    patterns = get_credential_patterns()

    def _bearing(line: str) -> bool:
        if any(p.search(line) for p in patterns):
            return True
        # `redact_tokens` returns the line unchanged when it finds nothing, so inequality IS
        # the detection — and it keeps this in step with the write path automatically: a
        # provider shape added there starts blocking pushes here with no second edit.
        return redact_tokens(line) != line

    return [n for n, line in enumerate(text.splitlines(), start=1) if line.strip() and _bearing(line)]


def schedule_has_conflict() -> bool:
    """True when ``rotation.yaml`` currently holds git conflict markers.

    Separate from ``has_conflict`` (which is ledger-only) because a conflicted schedule
    is far more dangerous than a conflicted ledger: markers make the YAML unparseable,
    and an unparseable schedule means no instance can tell whether it is on call.
    """
    path = _repo_root() / _SCHEDULE_FILENAME
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(line.startswith(CONFLICT_MARKERS) for line in text.splitlines())


async def _resolve_schedule_conflict() -> bool:
    """Take the REMOTE's schedule when it conflicts. Returns whether it did.

    "Theirs" rather than a merge attempt: a shift is a single-owner fact, so there is no
    union to compute — one of the two edits has to lose, and the remote is the version the
    rest of the team is already acting on. Converging on it keeps every instance's view of
    who is on call identical, which is the property that makes the file usable as a lock.
    The local edit is not destroyed; it stays in the reflog and the operator can re-apply
    and push it.
    """
    # Off the loop with the rest of the conflict probes above -- a file read is a file read,
    # and this one runs on every pull.
    if not await asyncio.to_thread(schedule_has_conflict):
        return False
    rc, _, err = await _git("checkout", "--theirs", "--", _SCHEDULE_FILENAME)
    if rc != 0:
        logger.warning(
            "ops-mission-control: could not take the remote schedule: %s", err.strip()[:200]
        )
        return False
    await _git("add", "--", _SCHEDULE_FILENAME)
    sel().log_api_access(
        caller="core:ops-mission-control",
        operation="ledger_sync_schedule_conflict",
        outcome="success",
        resources="resolution=theirs",
    )
    logger.warning(
        "ops-mission-control: %s conflicted on pull; took the remote version. A local "
        "edit to the on-call schedule was NOT merged — re-apply and push it if it is "
        "still wanted.",
        _SCHEDULE_FILENAME,
    )
    return True


def resolve_conflict() -> int:
    """Rewrite the ledger from its own reconciled entries, dropping markers.

    ``read_entries`` already tolerates conflict markers and merges duplicate ids, so
    the reconciled view is available *before* this runs — which is why the app stays
    correct mid-merge. This makes that view durable so git sees a clean file.

    Returns the number of entries kept. Safe to call when there is no conflict: it
    rewrites the same content.

    Holds ``ledger._LedgerLock`` across the READ and the WRITE. That lock's own docstring
    already states the rule — "every mutation that RE-READS the ledger and REWRITES the whole
    file must hold this" — and this function was a caller that did not. The gap is real: a
    ``POST /ledger`` landing between the ``read_entries`` and the ``_write_all`` is silently
    erased, because ``_write_all`` overwrites rather than appends. It is the same defect the
    lock was introduced to fix in ``hygiene``, in a function added afterwards, which is the
    argument for the lock living at the read-modify-write span rather than inside the writer.
    Found in review.
    """
    with ledger._LedgerLock():
        entries = ledger.read_entries()
        ledger._write_all(entries)  # same writer upsert/hygiene use, so format cannot drift
    sel().log_api_access(
        caller="core:ops-mission-control",
        operation="ledger_sync_resolve",
        outcome="success",
        resources=f"entries={len(entries)}",
    )
    return len(entries)


async def pull() -> tuple[bool, str]:
    """Fetch and merge the team's ledger. Call BEFORE matching.

    A teammate's lesson is only useful to this investigation if it arrives before the
    fingerprint lookup. Returns ``(ok, detail)`` and never raises: sync is a
    convenience, and a dispatch cycle must not fail because a remote was unreachable.
    """
    if not configured():
        return False, "ledger sync is not configured"
    ok, err = await _ensure_repo()
    if not ok:
        return False, err

    rc, _, err = await _git("fetch", "--quiet", "origin", branch())
    if rc != 0:
        # An empty remote has no branch yet — normal on first use, not an error.
        detail = err.strip()[:200]
        if "couldn't find remote ref" in detail or "not found" in detail.lower():
            return True, "remote has no ledger branch yet (first sync will create it)"
        return False, f"fetch failed: {detail}"

    # Stage-and-commit any local tracked file BEFORE merging. Without this, git refuses
    # the merge outright — "Untracked working tree file 'ledger.jsonl' would be
    # overwritten by merge" — so an instance that recorded even one lesson before its
    # first pull could NEVER pull, permanently. Found by a real two-instance roundtrip
    # against a bare remote; every unit test passed because they mock git.
    #
    # Committing first is also the correct semantic: this instance's entries are real
    # work, and the merge is meant to UNION them with the team's, which is exactly what
    # the conflict reconciler below does.
    #
    # KNOWN CONSEQUENCE for the SCHEDULE, which does not union. Staging is
    # ``TRACKED_FILES``-wide, so an instance holding a local ``rotation.yaml`` commits it
    # here and the merge then conflicts against the remote's — resolved to "theirs", which
    # DISCARDS the local edit. Observed on a first pull in the three-instance run, where a
    # freshly-written local schedule met a remote one.
    #
    # Left as-is deliberately: taking the remote is the correct convergence rule for a
    # single-owner fact (see ``_resolve_schedule_conflict``), and the alternative — skipping
    # the pre-merge commit for the schedule alone — would reintroduce git's
    # "untracked file would be overwritten" refusal that made pulls impossible at all.
    # The discard is logged at WARNING with the re-apply instruction, and the operator's
    # edit survives in the reflog.
    #
    # The right FIX is a schedule edited through one path (the repo, reviewed) rather than
    # locally on each instance — which is what the shared-file model already asks for.
    await _stage_and_commit("local ops ledger before merge")

    # ``--allow-unrelated-histories`` is REQUIRED here, not a convenience. Every instance
    # runs its own ``git init`` against a shared remote, so two installs that each
    # recorded a lesson before their first pull have genuinely unrelated root commits and
    # git refuses outright ("fatal: refusing to merge unrelated histories") — the second
    # teammate to join could never pull, which is the ordinary multi-instance case, not an
    # edge case. Also found by the real roundtrip.
    #
    # The flag is safe precisely because of how this repo is shaped: the tracked content
    # is a content-addressed union (ledger ids are sha256 over pattern+fix) and the
    # conflict path below reconciles duplicates rather than picking a side, so joining two
    # histories cannot lose an entry. On a normal source repo this flag would be reckless.
    rc, _, err = await _git(
        "merge", "--no-edit", "--allow-unrelated-histories", f"origin/{branch()}"
    )
    if rc != 0:
        # The SCHEDULE is checked first and handled differently from the ledger, because
        # the two have opposite merge semantics.
        #
        # A ledger conflict is reconcilable: entries are content-addressed, so the union
        # is unambiguously correct. A rotation.yaml conflict is a genuine disagreement —
        # two people edited the same shift — and there is no safe automatic answer.
        #
        # Left alone, the markers made the YAML unparseable, which under fail-open
        # RE-ARMED EVERY INSTANCE: observed in a three-teammate run through a real repo,
        # where a conflicted schedule reported `team=[]` and all three instances armed —
        # the exact double-claim the shared schedule exists to prevent. Taking THEIRS is
        # the safe resolution: the remote is what the rest of the team is already acting
        # on, so converging on it keeps every instance's view identical, and the local
        # edit is recoverable from the reflog rather than silently merged into nonsense.
        schedule_conflicted = await _resolve_schedule_conflict()

        # Off the loop: `has_conflict` reads the whole ledger and `resolve_conflict` re-parses
        # and REWRITES it, and a team ledger is the one file in this app that grows without
        # bound. `pull` is awaited from the heartbeat and from hygiene, so on a conflicted
        # ledger this stalled the gateway — every chat token, every other app — for the length
        # of a full parse-and-rewrite. Found in review; the async functions in this module are
        # otherwise careful to keep file work in a thread, which is what made it stand out.
        if await asyncio.to_thread(has_conflict):
            kept = await asyncio.to_thread(resolve_conflict)
            await _stage_and_commit("merge team ledger", allow_empty_message_only=True)
            detail = f"merged with conflict, reconciled to {kept} entries"
            if schedule_conflicted:
                detail += "; schedule conflict resolved to the remote's version"
            return True, detail
        if schedule_conflicted:
            await _stage_and_commit("take remote schedule", allow_empty_message_only=True)
            return True, "schedule conflict resolved to the remote's version"
        return False, f"merge failed: {err.strip()[:200]}"
    return True, "pulled"


#: Files the sync tracks. The ledger is the shared knowledge; ``rotation.yaml`` is the
#: on-call schedule (see ``providers/schedule_file.py``). Both are small, human-edited
#: text that merges. Everything else in this directory is local state — the dispatch
#: index is not merge-safe, and provider config could name a private log group.
TRACKED_FILES: tuple[str, ...] = ("ledger.jsonl", "rotation.yaml", ".gitignore")


async def _stage_and_commit(message: str, *, allow_empty_message_only: bool = False) -> bool:
    """Stage every tracked file and commit if anything changed.

    Returns True when a commit was made. Staging the whole tracked SET rather than just
    the ledger is load-bearing: ``rotation.yaml`` is un-ignored so it can sync, but a
    push that only ever ran ``git add ledger.jsonl`` would leave the schedule committed
    nowhere and silently never reach teammates.
    """
    for name in TRACKED_FILES:
        if (_repo_root() / name).exists():
            await _git("add", "--", name)
    rc, out, _ = await _git("status", "--porcelain")
    if rc == 0 and not out.strip() and not allow_empty_message_only:
        return False
    rc, _, err = await _git("commit", "--no-edit", "-q", "-m", message)
    if rc != 0 and "nothing to commit" not in err.lower():
        # WARNING, not debug. A refused commit is not a quiet no-op: the push that follows
        # then has nothing to send and fails with git's `src refspec HEAD does not match
        # any`, which names the push and says nothing about the commit that never
        # happened. That is how the real cause stayed invisible on a host where the commit
        # could not be made at all -- the operator sees "push errored" and no reason.
        logger.warning("ops-mission-control: ledger commit refused: %s", err.strip()[:200])
        return False
    return rc == 0


async def push(*, message: str = "update ops ledger") -> tuple[bool, str]:
    """Commit and push the local ledger. Call AFTER recording a lesson."""
    if not configured():
        return False, "ledger sync is not configured"
    ok, err = await _ensure_repo()
    if not ok:
        return False, err

    if await asyncio.to_thread(has_conflict):
        # Never push a conflicted file to teammates. Both calls go off the loop for the reason
        # documented in `pull` above: whole-ledger read, parse and rewrite.
        await asyncio.to_thread(resolve_conflict)

    # A conflicted SCHEDULE must never reach the remote, and unlike the ledger it cannot
    # be auto-reconciled — so REFUSE rather than guess. This is not defensive
    # hypothesising: an earlier three-teammate run pushed a schedule containing conflict
    # markers, and from then on every teammate's pull faithfully received a file that
    # cannot be parsed. An unparseable schedule means no instance can tell whether it is
    # on call, so one bad push disarms (or, under fail-open, wrongly arms) the entire
    # team, and no amount of downstream conflict handling can recover it — "theirs" is
    # already corrupt.
    #
    # Refusing costs one operator a push they must fix by hand; publishing costs the whole
    # team its on-call gating.
    if await asyncio.to_thread(schedule_has_conflict):
        logger.error(
            "ops-mission-control: refusing to push — %s holds conflict markers. Resolve "
            "the on-call schedule by hand; pushing it would leave every teammate unable "
            "to parse who is on call.",
            _SCHEDULE_FILENAME,
        )
        sel().log_api_access(
            caller="core:ops-mission-control",
            operation="ledger_sync_push",
            outcome="refused",
            resources=f"reason=conflicted_{_SCHEDULE_FILENAME}",
        )
        return False, f"refused: {_SCHEDULE_FILENAME} holds conflict markers — resolve it first"

    # Last line of defence: never publish a credential, even one written before the
    # write-path redactor existed.
    #
    # `POST /ledger` now redacts `pattern`/`fix`, which is where this belongs — the entry
    # is on local disk and in the vector index long before sync runs. But an entry written
    # by an older build, or reaching the file by any path that is not that route, would
    # still be committed verbatim. Recovery from a published secret is a history rewrite
    # across every teammate's clone, so the asymmetry justifies a second check: refusing
    # costs one operator a push they must fix by hand.
    #
    # Reuses the canonical pattern set rather than a private regex, so a pattern added for
    # any other egress site protects this one too.
    # Off-loop: reads and regex-scans the WHOLE ledger against every credential pattern, and
    # `push` is a coroutine reached from the hygiene route. Same class as the store/ledger
    # parses already moved, but through a module-LOCAL helper, which is why the AST guard did
    # not see it — that guard matched `store.*`/`ledger.*` attribute calls only. It now also
    # covers the local file-scanning helpers in this package. Found in review.
    leaky = await asyncio.to_thread(_credential_bearing_lines)
    if leaky:
        logger.error(
            "ops-mission-control: refusing to push — %s appears to contain credential "
            "material on %d line(s). Remove the entries by hand; a pushed secret has to "
            "be scrubbed from every teammate's clone.",
            _LEDGER_FILENAME,
            len(leaky),
        )
        sel().log_api_access(
            caller="core:ops-mission-control",
            operation="ledger_sync_push",
            outcome="refused",
            # Line numbers only. Naming the matched text here would copy the secret into
            # the audit log, which is the thing being prevented.
            resources=f"reason=credential_in_{_LEDGER_FILENAME} lines={leaky}",
        )
        return False, (
            f"refused: {_LEDGER_FILENAME} holds apparent credential material on "
            f"line(s) {', '.join(str(n) for n in leaky)} — remove it before syncing"
        )

    committed = await _stage_and_commit(message)
    # A clean tree is not automatically "nothing to push": a previous run may have
    # committed locally and then failed to reach the remote, and returning early there
    # would strand that commit forever. Only skip when there is also nothing unpushed.
    if not committed and not await _has_unpushed():
        return True, "nothing to push"

    rc, _, err = await _git("push", "--quiet", "origin", f"HEAD:{branch()}")
    if rc != 0:
        return False, f"push failed: {err.strip()[:200]}"
    sel().log_api_access(
        caller="core:ops-mission-control",
        operation="ledger_sync_push",
        outcome="success",
        resources=f"remote={remote()} branch={branch()}",
    )
    return True, "pushed"


async def _has_unpushed() -> bool:
    """True when HEAD holds commits the remote branch does not.

    Distinguishes "clean tree, all shared" from "clean tree, but a previous push never
    landed". Treats an unknown answer as True: attempting a redundant push is cheap,
    while skipping a needed one strands a lesson locally forever.
    """
    rc, out, _ = await _git("rev-list", "--count", f"origin/{branch()}..HEAD")
    if rc != 0:
        return True
    try:
        return int(out.strip() or "0") > 0
    except ValueError:
        return True


async def sync_safely(*, direction: str = "pull") -> str:
    """Run a sync step, swallowing every fault. Returns a short outcome string.

    **Two callers, asymmetric on purpose.** ``run_cycle`` PULLS on every heartbeat, on
    every instance; ``POST /ledger/hygiene`` pulls and PUSHES once a day, on the primary
    only.

    The pull half was added because ``rotation.yaml`` travels in this repo and hygiene is
    gated to the primary — so a non-primary instance had no code path that ever fetched
    the schedule. It kept arming (or not) off whatever it last saw, which is the
    double-claim the single-owner model exists to prevent, reintroduced by the transport
    rather than by the model. ``run_cycle`` pulls BEFORE reading the shift, since pulling
    after would gate the current cycle on the stale file and only help the next one.

    Push stays daily and primary-only: publishing is the leader's job, and a per-cycle
    push from every instance is the concurrent-write problem this asymmetry avoids. So
    lessons still converge on a daily cadence even though the schedule now converges per
    heartbeat — the module docstring's "pull before you match" is now true, "push after
    you learn" is still aspirational.

    Shared memory improving an investigation is worth having; it is never worth losing a
    claim over, so an unreachable remote degrades to "this instance works from what it
    already knows".

    One retry on the FIRST attempt, because the sandbox backend probe is deliberately
    deferred off the event loop on a cold cache and raises a self-described TRANSIENT
    error telling the caller to retry ("cache warms in ms"). A real roundtrip hit this
    on every first push in a fresh process — the whole first sync failed for a condition
    that resolves in milliseconds. The retry is bounded at one and only re-runs an
    idempotent git step, so it cannot mask a genuine fault.
    """
    if not configured():
        return ""
    last = ""
    for attempt in (1, 2):
        try:
            ok, detail = await (pull() if direction == "pull" else push())
        except Exception as exc:  # noqa: BLE001 — sync must never break a cycle
            last = f"{direction} errored"
            transient = "retry" in str(exc).lower() or "transient" in str(exc).lower()
            if attempt == 1 and transient:
                logger.debug(
                    "ops-mission-control: ledger %s hit a transient spawn fault; retrying",
                    direction,
                )
                await asyncio.sleep(0.25)
                continue
            logger.exception("ops-mission-control: ledger %s failed", direction)
            return last
        if not ok:
            logger.warning("ops-mission-control: ledger %s: %s", direction, detail)
        return detail
    return last
