"""One-click commit: apply a queued change and push it straight to a branch.

This is the button the operator presses to say "commit this now" — distinct from
the ``directCommit`` config *mode*, which tells the autonomous loop to do the
same thing without asking. Both land on the same safety gate: the target branch
is run through the spine's non-overridable protected-branch denylist, so a
protected branch is refused here exactly as it is in the driver's direct-push
path, and the message is credential-redacted before it becomes permanent git
metadata.

Applies the durable queue copy (``pr_queue/<fp>.diff``), commits it on the base
branch, and pushes over the authenticated remote. On any failure the change
stays in the queue — nothing is half-committed.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path

from kiro_crew.security import redact

from ..profiles.github_repo.pr_recipe import _prefer_authenticated_remote
from ..spine.git_safety import GIT_SAFE_CONFIG, require_pinned
from ..spine.push_policy import (
    authorize_direct_push,
    describe_scan,
    normalize_branch,
    scan_content_for_secrets,
)
from . import store
from .clone_setup import resolve_origin_url

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_S = 60.0
_PUSH_TIMEOUT_S = 120.0

#: Where the freshly-fetched base lands. An explicit ref under our own namespace, not
#: ``origin/<branch>``: the clone's ``origin`` is neutralized (see
#: ``clone_setup._disable_push``), so its remote-tracking refs are whatever the original
#: clone left behind and are never refreshed again. Fetching into a ref this module owns
#: makes "the base we are committing on" an explicit, inspectable value.
_BASE_REF = "refs/auto-improvement/commit-base"

#: Serializes every OPERATOR-triggered mutation of the shared clone.
#:
#: The run-status gate in `routes` stops these paths racing the LOOP, but not each other: the
#: dashboard's commit icon has no `disabled` while pending, so clicking two `filed` rows starts
#: two mutations, and each handler runs in its own `asyncio.to_thread` thread against the same
#: `config["clone"]`. Measured on a real bare repo: A stages its diff, B's
#: `checkout -B <branch> <base>` does NOT discard it (the branch is already at base, so no files
#: change), B's `git apply --index` stacks on top, and B's commit contains BOTH findings — the
#: commit recorded as B publishes A's change too. Worse, A's now-empty commit fails and its
#: `reset --hard` rewinds the local branch past B's already-pushed commit.
#:
#: A module-level lock rather than one per clone: there is exactly one configured clone per
#: install, and a per-path map would add eviction bookkeeping for no gain. `RLock` so a future
#: caller that already holds it (e.g. commit calling into another guarded helper) cannot
#: self-deadlock. Raised by the Opus 5 review of this branch.
_CLONE_LOCK = threading.RLock()


#: Trusted git config on every host-side git over the agent-writable clone — see the identical
#: `_GIT_SAFE_CONFIG` in the driver. These commands run on the HOST as the gateway user against
#: the tree the sandboxed agent edits, so a repo-written hook pointed at by `core.hooksPath`, or
#: a `core.fsmonitor` program, would execute host-side (outside the sandbox) on the next
#: add/commit/checkout. `-c` overrides on OUR argv beat the repo's own config. Raised by the GPT
#: review.
_GIT_SAFE_CONFIG = GIT_SAFE_CONFIG


def _git(
    clone: Path, *args: str, timeout: float = _GIT_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    require_pinned(clone)
    # ``errors="replace"``: the secret scan below reads a full `git diff`, which prints file
    # CONTENT — a strict decode raises inside ``subprocess.communicate`` on any repo holding
    # a binary, and this helper's callers read ``returncode`` as data. See
    # ``pr_watchers._git``. Scanning REPLACED text is still correct here: the replacement
    # only affects bytes that were never valid UTF-8, so no ASCII secret can hide behind it.
    return subprocess.run(
        ["git", "-C", str(clone), *_GIT_SAFE_CONFIG, *args],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )


def clone_lock() -> threading.RLock:
    """The lock every operator-triggered clone mutation must hold. See :data:`_CLONE_LOCK`.

    Exposed as a function rather than the bare object so callers read as
    ``with commit_mod.clone_lock():`` and cannot rebind it. The DRAFT route must hold it
    across its whole sequence — materialize → commit → draft → rollback — not just around
    each call, because the race is between the steps: another thread's ``checkout -B``
    landing between our apply and our commit is exactly what merges two findings into one
    commit.
    """
    return _CLONE_LOCK


def materialize_queued_diff(
    *, clone: Path, branch: str, config: dict, diff_text: str
) -> dict[str, object]:
    """Check out ``branch`` at its real base and apply ``diff_text`` to the index.

    Extracted so the DRAFT path can reuse it. Drafting pushed the clone's current
    ``HEAD`` (``pr_recipe._push_fix_branch`` pushes ``HEAD:refs/heads/<branch>``) while
    ``draft(diff=...)`` only WRITES the diff to the queue copy — it never applies it. In
    the loop that is fine, because ``_stage_winner`` puts the winner in the clone first;
    the backend's manual draft button has no such step, so drafting an OLDER queued
    finding published whatever a LATER cycle had left at HEAD. Measured against a real
    bare repo: finding A's queued diff adds ``FINDING_A``, and the branch pushed for A
    contained ``FINDING_B`` instead — PR metadata and content disagreeing. Raised by the
    GPT review of this branch.

    Returns ``{"ok": True, "base": <ref>}`` or an ``{"ok": False, "error": ...}`` in the
    same shape the callers already return, so neither has to branch on which step failed.
    """
    if not diff_text.strip():
        return {"ok": False, "error": "the queued diff is empty"}

    configured = resolve_origin_url(config)
    remote_url = _prefer_authenticated_remote(configured) if configured else ""

    base_ref_local = _BASE_REF
    if remote_url:
        fetch = _git(clone, "fetch", "--quiet", remote_url, f"+refs/heads/{branch}:{_BASE_REF}")
        if fetch.returncode != 0:
            return {
                "ok": False,
                "error": f"could not fetch {branch}: {(fetch.stderr or '')[:160]}",
            }
    else:
        # No configured url: the push cannot succeed either, so this degrades to
        # "committed locally only" rather than failing outright. Commit on whatever the
        # original clone recorded — stale, but it is the only base available.
        base_ref_local = f"origin/{branch}"
        if _git(clone, "rev-parse", "--verify", "--quiet", base_ref_local).returncode != 0:
            return {
                "ok": False,
                "error": (
                    f"no configured remote and no local ref for {branch} — "
                    "cannot establish a base to commit on"
                ),
            }

    checkout = _git(clone, "checkout", "-B", branch, base_ref_local)
    if checkout.returncode != 0:
        return {
            "ok": False,
            "error": f"could not check out {branch}: {(checkout.stderr or '')[:160]}",
        }

    apply_proc = subprocess.run(
        ["git", "-C", str(clone), "apply", "--index", "-"],
        input=diff_text,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
    )
    if apply_proc.returncode != 0:
        # Leave the tree clean so a retry or the draft-PR path still works.
        _git(clone, "reset", "--hard", base_ref_local)
        return {
            "ok": False,
            "error": f"the queued diff did not apply: {(apply_proc.stderr or '')[:160]}",
        }
    return {"ok": True, "base": base_ref_local}


def commit_staged_for_draft(*, clone: Path, body_path: Path, fp: str) -> dict[str, object]:
    """Commit what :func:`materialize_queued_diff` staged, so ``HEAD`` carries the fix.

    ``git apply --index`` stages; it does not move ``HEAD``. ``pr_recipe._push_fix_branch``
    pushes ``HEAD:refs/heads/<branch>``, so staging alone published the BASE and the queued
    fix was silently absent from the pull request. Measured against a real bare repo: the
    worktree read ``return 2`` while the pushed branch still read ``return 1``. The first
    version of the draft fix staged without committing and my test only asserted the
    worktree, which is exactly why it missed this. Raised by the GPT review of this branch.

    Reuses :func:`_commit_message` — the message is built from the agent-authored PR body and
    is redaction-hardened (fail-closed to a fixed subject), because a pushed commit message
    cannot be edited without rewriting history.
    """
    message = _commit_message(body_path, fp)
    commit = _git(clone, "-c", "commit.gpgsign=false", "commit", "-m", message)
    if commit.returncode != 0:
        return {
            "ok": False,
            "error": f"could not commit the staged diff: {(commit.stderr or '')[:160]}",
        }
    return {"ok": True, "sha": (_git(clone, "rev-parse", "HEAD").stdout or "").strip()}


def commit_finding(fp: str) -> dict[str, object]:
    """Commit the queued change for ``fp`` and push it to the configured branch.

    Returns ``{ok, ...}``. Refuses — without touching the repo — when the finding
    has no queued diff, no repository is configured, or the branch is protected.

    SERIALIZED on :func:`clone_lock`: two dashboard clicks would otherwise interleave
    checkout/apply/commit in the same clone and publish one commit containing both findings.
    A thin wrapper rather than an indented body so the diff stays reviewable and the lock's
    scope is unmistakable — it covers everything, including the push and the rollback.
    """
    with clone_lock():
        return _commit_finding_locked(fp)


def _commit_finding_locked(fp: str) -> dict[str, object]:
    """Body of :func:`commit_finding`. Callers MUST hold :func:`clone_lock`."""
    diff_path = store.pr_queue_dir() / f"{fp}.diff"
    body_path = store.pr_queue_dir() / f"{fp}.pr.md"
    if not diff_path.is_file():
        return {"ok": False, "error": f"no queued change for fingerprint {fp}"}

    config = store.read_json(store.config_path(), {}) or {}
    clone = Path(str(config.get("clone") or ""))
    if not str(config.get("clone") or "").strip():
        return {"ok": False, "error": "no repository configured"}

    base_ref = str(config.get("branch") or "origin/main")
    branch = normalize_branch(base_ref)

    # The gate: commit-to-branch is allowed ONLY for a non-protected branch, the
    # same rule the driver's direct-push obeys. A protected branch always falls
    # back to the draft-PR path, so the button cannot land on main.
    ok, reason = authorize_direct_push(direct_commit=True, branch=branch)
    if not ok:
        return {"ok": False, "error": f"branch refused by push policy: {reason}"}

    diff_text = diff_path.read_text(encoding="utf-8")
    if not diff_text.strip():
        return {"ok": False, "error": "the queued diff is empty"}

    # Base resolution + apply live in `materialize_queued_diff` (shared with the draft
    # route). Two hard-won properties are inside it: the fetch goes through the CONFIGURED
    # url rather than `origin` (both origin urls are neutralized, so `git fetch origin` here
    # exits 128), and the base is the freshly-fetched remote ref rather than the clone's
    # frozen `origin/<branch>` tracking ref.
    staged = materialize_queued_diff(clone=clone, branch=branch, config=config, diff_text=diff_text)
    if not staged.get("ok"):
        return staged
    base_ref_local = str(staged.get("base") or "")

    message = _commit_message(body_path, fp)
    commit = _git(clone, "-c", "commit.gpgsign=false", "commit", "-m", message)
    if commit.returncode != 0:
        _git(clone, "reset", "--hard", base_ref_local)
        return {"ok": False, "error": f"commit failed: {(commit.stderr or '')[:160]}"}
    sha = (_git(clone, "rev-parse", "HEAD").stdout or "").strip()

    # Scan the CONTENT before it leaves the host. `_commit_message` is already redacted;
    # this is the commit itself, which is equally unwipeable once pushed and is
    # agent-authored. Detect-and-refuse (never rewrite — that would corrupt the fix), and
    # roll the commit back so the tree is clean for a retry or the draft-PR path, exactly
    # as the failed-apply and failed-commit branches above do.

    scanned = _git(clone, "diff", f"{base_ref_local}..HEAD", timeout=_GIT_TIMEOUT_S)
    if scanned.returncode == 0:
        clean, code = scan_content_for_secrets(scanned.stdout or "")
        # Fixed code -> fixed literal: the message returned to the operator carries
        # nothing derived from the scanned diff.
        scan_note = "" if clean else describe_scan(code)
    else:
        clean, scan_note = False, "could not read the pushable diff"
    if not clean:
        _git(clone, "reset", "--hard", base_ref_local)
        return {
            "ok": False,
            "error": f"refusing to push: {scan_note} — the change stays in the local queue",
        }

    # From config first (the clone's urls are neutralized — see clone_setup._disable_push);
    # the clone lookup remains as a fallback and yields the DISABLED sentinel, which
    # `_resolve_push_url` rejects, so an older config degrades to "committed locally only"
    # rather than to an unguarded push.

    # Resolved from the SAME source `materialize_queued_diff` fetched through, so the base
    # this commit sits on and the url it is pushed to cannot disagree. (It used to be one
    # local variable shared by both steps; the fetch moved into the helper, so recompute it
    # here from `config` rather than threading it back out.)
    configured_url = resolve_origin_url(config)
    remote_url = _prefer_authenticated_remote(configured_url) if configured_url else ""
    url = remote_url or _resolve_push_url(clone, _prefer_authenticated_remote)
    # A push that never happened must not leave the commit on the branch.
    # `clone_setup.checkout_branch` PREFERS an existing local branch, so the next run would
    # start from this unpushed commit and treat the queued change as already-landed baseline —
    # measured on a real bare repo: local `work` sat 1 commit ahead of a remote it had never
    # reached. The earlier failure points in this function already reset; these last two
    # returned with the commit intact. The durable queue copy is untouched, so a retry still
    # has everything it needs, and the message says "queued" rather than "committed locally"
    # because after the reset that is what is true. Raised by the GPT review of this branch.
    if not url:
        _git(clone, "reset", "--hard", base_ref_local)
        return {
            "ok": False,
            "error": "no pushable remote — the change stays queued, nothing was committed",
        }
    push = _git(clone, "push", url, f"HEAD:refs/heads/{branch}", timeout=_PUSH_TIMEOUT_S)
    if push.returncode != 0:
        _git(clone, "reset", "--hard", base_ref_local)
        return {"ok": False, "error": f"push failed: {(push.stderr or '')[:200]}"}

    return {"ok": True, "fp": fp, "branch": branch, "sha": sha}


def _resolve_push_url(clone: Path, prefer):  # type: ignore[no-untyped-def]
    """The origin FETCH url, rewritten to the authenticated transport.

    The push REMOTE stays ``DISABLED_NO_PUSH`` by design; we push to the fetch url
    for this one ref, mirroring the driver's direct-push. Returns None when the
    clone is fully push-disabled (a per-PR watcher clone), so the commit stays
    local rather than silently failing to leave the machine.
    """
    proc = _git(clone, "remote", "get-url", "origin")
    raw = (proc.stdout or "").strip()
    if proc.returncode != 0 or not raw or "DISABLED" in raw.upper():
        return None
    return prefer(raw)


def _commit_message(body_path: Path, fp: str) -> str:
    """A commit message from the queued PR body's title, credential-redacted.

    Redaction matters because the message becomes permanent git history: an
    agent-authored body could echo a token it saw. Falls back to a plain subject
    when no body was queued.
    """
    subject = f"auto-improvement: apply verified change {fp[:12]}"
    if body_path.is_file():
        try:
            first = body_path.read_text(encoding="utf-8").lstrip().splitlines()[0]
            subject = first.lstrip("# ").strip() or subject
        except OSError:
            pass
    try:
        subject = redact(subject)
    except Exception:  # noqa: BLE001 - a commit message is permanent, pushed git history
        # FAIL CLOSED: the message becomes unwipeable git history the moment it is pushed,
        # so an unscannable agent-authored subject must NOT be committed verbatim. Fall
        # back to the fixed, prose-free subject. Raised by the GPT review of this branch.
        logger.warning("commit-message redaction failed; using the fixed subject", exc_info=True)
        return f"auto-improvement: apply verified change {fp[:12]}"
    return subject
