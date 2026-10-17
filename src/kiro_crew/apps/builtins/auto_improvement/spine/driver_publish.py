"""Publish a verified winner straight to its authorized branch: the F10 direct push.

``_direct_push`` re-checks the authorization, runs the pre-push review gate, proves ``HEAD``
is still the commit the pipeline made, credential-scans that object by id and pushes it by id.
A non-fast-forward rejection gets one rebase-and-retry (``_push_with_rebase``): the replay is
re-verified through the profile's build gate, bound to the authorized change by its tree and
its attribution, scanned again, and only then promoted onto the branch and pushed.

Each function here whose first parameter is ``self`` is a ``Driver`` method: the class binds
it by name, so it reads and writes the run's state on the instance, and a patch of the method
on the class or on an instance reaches every caller.

Instance fields written: ``_pushed_object``, ``pushed_sha``.

Instance fields read: ``_agent_runner``, ``_rollback_failed``, ``branch``, ``clone``,
``direct_commit``, ``ledger``, ``log``, ``prepush_review``, ``profile``.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from kiro_crew.platform.context import redact_log_via_context, redact_via_context
from kiro_crew.subprocess_utf8 import UTF8_TEXT

from . import ledger as L
from .driver_git import (
    _GIT_SAFE_CONFIG,
    _commit_attribution,
    _commit_tree,
    _committer_identity,
    _expected_replay_tree,
    _fetched_tip_oid,
    _git,
)
from .git_safety import hook_off_args, require_pinned

if TYPE_CHECKING:
    from .driver import Driver


def _reverify_head(self: Driver) -> bool:
    """Re-run the profile's build gate on the clone's CURRENT tree. Fail-closed.

    Called only after a rebase rewrote HEAD. The gate result we hold was measured
    against the PRE-rebase base, so it says nothing about the replayed tree: a rebase
    can apply cleanly and still produce a combination that was never built or tested
    (our patch plus whatever landed on the branch meanwhile). Publishing on the
    strength of the stale result would break the app's core promise — that nothing
    reaches a shared branch unless a measurement on THAT tree passed.

    Any error re-verifying is a refusal, not a pass: an unverifiable tree is exactly
    the case this gate exists for.
    """
    try:
        res = self.profile.build_gate.build_and_test(worktree=self.clone, src=self.clone / "src")
    except Exception as exc:  # noqa: BLE001 — an unverifiable tree must not publish
        self.log.error("direct-push: could not re-verify the rebased tree: %s", exc)
        return False
    if not getattr(res, "passed", False):
        self.log.warning(
            "direct-push: rebased tree FAILED re-verification (%s) — not pushing",
            getattr(res, "detail", "") or "no detail",
        )
        return False
    return True


def _revision_scans_clean(self: Driver, rev: str) -> tuple[bool, str]:
    """Credential-scan ONE revision's own object -- metadata and content. ``(clean, note)``.

    BOTH HALVES OF THE OBJECT ARE SCANNED. The tree diff covers what the commit changes;
    the commit's header lines are separate bytes the push transfers just the same, and the
    committer identity among them comes from configuration rather than from the replayed
    commit, so a process able to write the clone's git config chooses it.

    Shared by both publish paths so the scan cannot be attached to one of them and
    forgotten on the other -- which is exactly what happened: the scan lived inline in
    :meth:`_direct_push` and covered the first push's object, while the rebase retry in
    :meth:`_push_with_rebase` published a REPLAYED object that had been build-verified and
    never credential-scanned. A non-fast-forward retry is not an edge case here; the
    retry's own docstring records losing 3 of 6 gate survivors to that race. Raised by the
    GPT review of this branch.

    *rev* must be a FULL OBJECT ID, never ``HEAD``: the point is that the object scanned
    here and the object pushed are the same one by construction rather than by timing.

    The git call's EXIT STATUS is load-bearing, not just its stdout: ``_git`` does not
    raise, and a failed read exits non-zero with EMPTY stdout -- which the scanner would
    read as "nothing to scan". Every failure here therefore refuses. ``--root`` lets one
    listing cover a root commit, which has no parent to compare against.

    HISTORY WORTH KEEPING, because the shape of the range is what went wrong before: an
    earlier form diffed ``{dest}..HEAD``, which was silently EMPTY -- ``dest`` is the local
    branch the commit sits on the tip of, so that diffs a ref against itself, the scanner
    read "clean", and the fail-closed credential gate was SKIPPED entirely. Measured
    against a real repo at the time: a commit adding an AWS key produced a 0-byte
    ``{dest}..HEAD`` diff and a 144-byte single-commit diff carrying the key.
    """
    # Imported here, not at module scope, to match `_direct_push`'s existing pattern: the
    # name is then resolved on `push_policy` at call time, which is also the seam the
    # tests patch.
    from .push_policy import describe_scan, scan_content_for_secrets

    # `core.useReplaceRefs=false` is load-bearing, not hygiene. Git substitutes objects
    # named by `refs/replace/<oid>` transparently in READS -- `diff` and `show` included --
    # while the push transport sends the ORIGINAL object. So a reviewer that runs
    # `git replace` (not on that runner's denylist, which covers only `push` and
    # `remote set-url`) could point a replacement at clean content, have this scan read the
    # decoy, and still have the credential-bearing object transferred. Reading with replace
    # refs disabled makes what is scanned and what is sent the same bytes. Raised by the
    # GPT review of this branch.
    # Only `diff.external=` here: `_git` already injects `core.useReplaceRefs=false` on
    # every call it makes, so spelling it again is the same guarantee written twice, and
    # only the chokepoint spelling is test-pinned. Dropped on the first-principles review.
    _NO_REPLACE = ("-c", "diff.external=")
    # SCAN THE COMMIT OBJECT ITSELF, not only the content it carries. A tree diff shows
    # what the commit CHANGES; the commit's own header lines are separate bytes that the
    # push transfers just the same, and `--format=` on the root-commit branch suppresses
    # them outright. `git rebase` takes the COMMITTER identity from configuration rather
    # than from the commit it replays, so a process able to write the clone's git config
    # decides those bytes -- the same actor this method already refuses to trust for
    # `git replace` above, since the runner's denylist covers only `push` and
    # `remote set-url`. Measured on a real repository: a replayed commit whose config set
    # a key-shaped `user.email` carries it in the committer line while the diff branch and
    # the `--format=` branch both print nothing but the tree change. `cat-file commit`
    # yields the raw object -- tree, parent, author, committer, then the message -- so the
    # header bytes and the message reach the scanner as the transport sees them, and no
    # diff-shaping flag applies. Raised by the GPT review of this branch.
    meta = _git(["cat-file", "commit", rev], self.clone)
    if meta.returncode != 0:
        return False, "could not read the pushable commit object"
    clean, scan_code = scan_content_for_secrets(meta.stdout or "")
    if not clean:
        return False, describe_scan(scan_code)
    # SCAN THE RAW BLOBS THE PUSH TRANSFERS, not a rendered diff. `git diff` is a REPORT
    # for humans: for a blob it decides is binary it prints "Binary files a and b differ"
    # and no content at all, so a credential inside such a blob is invisible to a scan that
    # reads diff output -- and a NUL byte anywhere in a file is enough to trigger that,
    # which whatever writes the tree chooses freely. Measured on a real repository: a blob
    # holding NUL, then a key, then NUL renders as that one summary line with zero
    # occurrences of the key, while `cat-file blob` on the same object yields it. The push
    # sends objects rather than a diff, so enumerating the revision's own changed blobs and
    # reading each one is what makes the scanned bytes and the transferred bytes the same
    # bytes. This also scans a WHOLE blob rather than the changed hunks, which is again what
    # is published: a hunk is a view of an object, and it is the object that lands on the
    # remote. Raised by the GPT review of this branch.
    #
    # `--root` covers a root commit, which has no parent to diff against, so both cases go
    # through one listing rather than a branch whose second half was the weaker reading.
    listing = _git(
        ["diff-tree", "-r", "--root", "--no-commit-id", rev],
        self.clone,
        config=_NO_REPLACE,
    )
    if listing.returncode != 0:
        return False, "could not list the pushable blobs"
    for line in (listing.stdout or "").splitlines():
        # ``:<src mode> <dst mode> <src sha> <dst sha> <status>\t<path>``
        if not line.startswith(":"):
            continue
        meta_fields = line[1:].partition("\t")[0].split()
        if len(meta_fields) < 5:
            return False, "could not read a pushable blob entry"
        dst_mode, dst_sha, status = meta_fields[1], meta_fields[3], meta_fields[4]
        # A DELETION publishes no content, and a gitlink is a commit pointer with no blob
        # behind it -- `cat-file blob` on either is a failure that would fail this closed
        # for a revision carrying nothing to read.
        if status.startswith("D") or dst_mode == "160000" or set(dst_sha) == {"0"}:
            continue
        blob = _git(["cat-file", "blob", dst_sha], self.clone)
        if blob.returncode != 0:
            return False, "could not read a pushable blob"
        clean, scan_code = scan_content_for_secrets(blob.stdout or "")
        if not clean:
            # `describe_scan` maps a fixed code to a fixed literal, so nothing derived from
            # the scanned content reaches a log line or a ledger row.
            return False, describe_scan(scan_code)
    return True, ""


def _restore_branch(self: Driver, branch: str, *, promote: str = "") -> bool:
    """Put the clone back on *branch*, moving it to *promote* first when given. ``ok``.

    RETURNS A STATUS because the caller must be able to abort on it. Logging a failed
    restore and continuing would publish with the clone detached or its durable branch
    stale -- a ref lock or a checkout failure is exactly the case where the repository is
    not in the state the push assumes. Raised by the GPT review of this branch.

    The rebase retry DETACHES HEAD in order to replay the authorized object rather than
    whatever the branch points at, so every exit from that path has to leave the clone
    attached again. :meth:`_reset_provisional` rolls a provisional commit back with
    ``git reset --hard``, which on a detached HEAD moves the detachment and leaves the
    BRANCH still carrying that commit -- a rollback that silently does not roll back.

    PROMOTION IS CONDITIONAL BY CONSTRUCTION. ``git branch -f`` is only correct when the
    thing it moves onto is the verified, scanned replay, so the caller passes *promote*
    at exactly one place: after the replay has passed re-verification, the HEAD-identity
    check and the credential scan. A failed or aborted rebase therefore cannot force the
    branch onto whatever HEAD happens to be -- it takes the no-argument form, which
    re-attaches without moving anything. Raised in review of this branch.
    """
    was = ""
    if promote:
        # TRANSACTIONAL: remember where the branch was, because `git branch -f` lands
        # BEFORE the checkout can fail. A concurrent index lock that fails the checkout
        # would otherwise leave the branch promoted to a replay this method then reports as
        # unrestored -- the push aborts, the caller rolls back the detached HEAD, and the
        # unpushed commit stays on the durable branch. Raised by the GPT review.
        was = _git(["rev-parse", branch], self.clone).stdout.strip()
        if _git(["branch", "-f", branch, promote], self.clone).returncode != 0:
            self.log.error(
                "direct-push: could not move %s onto the replayed commit %s",
                branch,
                promote[:12],
            )
            return False
    if _git(["checkout", branch], self.clone).returncode != 0:
        self.log.error("direct-push: could not re-attach the clone to %s", branch)
        if promote and was:
            # Undo the promotion so the branch is not left carrying an unpushed commit.
            if _git(["branch", "-f", branch, was], self.clone).returncode != 0:
                self.log.error(
                    "direct-push: could not put %s back on %s after a failed checkout",
                    branch,
                    was[:12],
                )
        return False
    return True


def _push_with_rebase(
    self: Driver, fetch_url: str, dest: str, target: str, src: str
) -> subprocess.CompletedProcess | None:
    """Push *src* to ``dest``, rebasing ONCE onto the remote if it moved meanwhile.

    *src* is the revision the FIRST push sends, and it is REQUIRED: an object id cannot be
    repointed, so once this is called, nothing running in the clone can change which commit
    gets published. Pushing the symbolic ``HEAD`` left a time-of-check/time-of-use window --
    the identity check happens, then the credential scan and the push each re-read ``HEAD``
    -- and a background process left behind by the pre-push reviewer could move it in
    between. Raised by the GPT review of this branch.

    It began as an optional parameter defaulting to ``HEAD`` "for callers that have nothing
    more specific". There were none: the sole production caller resolves the committed
    object first and passes it. That default kept three arms alive that fired only for a
    symbolic source -- skipping the pre-fetch tamper check, replaying the BRANCH rather than
    the object, and reading ``HEAD`` after the push for the ledger -- each of them the very
    check-then-use shape this change exists to remove. Requiring the argument deletes all
    three. Raised by the first-principles review of this branch, which asked for the
    subtraction rather than more guards.

    THE RETRY REPUBLISHES BY ID TOO. A rebase REPLAYS the commit as a NEW object, so *src*
    names the pre-rebase commit -- not what the rebase produced, and non-fast-forward
    anyway -- but the answer is to resolve the REPLACEMENT to a full id, credential-scan
    that object, and push it. An earlier revision of this method pushed ``HEAD`` there and
    justified it with "``_reverify_head`` has just run", which was the same
    check-then-use mistake this branch fixed twice elsewhere: the build gate is a check,
    the push is a later use, and ``HEAD`` is re-resolved in between. It also published an
    object no credential scan had ever covered, because the caller scans the PRE-rebase
    object. Raised by the GPT review of this branch.

    A run takes tens of minutes, so the branch can legitimately advance between the
    clone's fetch and the winner's push — and a bare push then dies
    ``! [rejected] ... (fetch first)``, stranding a fully verified fix. Measured on
    this app's own dogfood: 3 of 6 gate survivors were lost this way, every one of
    them work that had already passed RED x2 -> GREEN -> STAYGREEN.

    The retry is deliberately narrow and safe:
      * only on a NON-FAST-FORWARD rejection — any other failure (auth, no such
        ref, protected branch) is returned untouched, because retrying those just
        hides the real error;
      * ``git rebase`` REPLAYS our single verified commit on top of the new remote
        tip. A conflict aborts the rebase and returns the original failure rather
        than pushing a half-merged tree;
      * the REPLAYED tree is RE-VERIFIED through the profile's build gate before it
        is pushed (:meth:`_reverify_head`). A clean rebase is a statement about
        TEXT, not about behaviour: our verified patch combined with whatever landed
        on the branch meanwhile is a tree nothing has ever built or tested. Without
        this the retry published an unverified commit — the one thing the whole
        measurement-first pipeline exists to prevent. Raised by the GPT review of
        this branch;
      * never ``--force``. If the second push is also rejected, we stop — a losing
        race is a signal, not something to overwrite.

    The caller must record ``self._pushed_object``, which this sets to whatever it actually
    sent, and NOT its own pre-push snapshot: a rebase rewrites HEAD, so the pre-rebase sha
    names a commit that does not exist on the remote. Reading the sha back from the clone is
    wrong for the same reason -- HEAD can move between the push and the read.
    """
    if self._retire_if_unsafe("direct-push preflight"):
        return None
    require_pinned(self.clone)
    # Remember WHICH OBJECT was sent, so the caller records that rather than re-reading
    # `HEAD` after the push: HEAD can move between the push and the read, which would put
    # an unrelated sha in the ledger for a commit that did land. Raised by the GPT review.
    self._pushed_object = src
    push = subprocess.run(
        [
            "git",
            "-C",
            str(self.clone),
            *_GIT_SAFE_CONFIG,
            *hook_off_args(self.clone),
            "push",
            fetch_url,
            f"{src}:refs/heads/{dest}",
        ],
        capture_output=True,
        **UTF8_TEXT,
    )
    for _ in range(self._PUSH_ATTEMPTS - 1):
        if push.returncode == 0:
            return push
        blob = f"{push.stdout or ''}\n{push.stderr or ''}"
        if "non-fast-forward" not in blob and "fetch first" not in blob:
            return push  # a different failure — do not mask it with a retry
        self.log.info("direct-push: %s moved under us; rebasing and retrying", dest)
        # `git rebase` replays whatever the CURRENT BRANCH points at, not *src* -- so if
        # HEAD has moved since the caller's checks, the retry would replay the moved commit
        # and then bind its own capture, verify and scan to THAT, consistently passing while
        # publishing content that never descended from the verified object. Refuse instead:
        # the rebase input has to be the object this run authorized. Skipped when *src* is
        # the default symbolic `HEAD`, where there is no retained id to compare against.
        # Raised by the GPT review of this branch.
        # A TAMPER DETECTOR, not the guarantee it once was. The rebase below names the
        # authorized object EXPLICITLY, so what gets replayed does not depend on where
        # HEAD points and this check is not what makes that safe. It stays because a HEAD
        # that has moved by now means something wrote the clone after the caller's checks,
        # and a clone being written by an unknown actor is not one to publish from. On its
        # own it is defeatable: it sits before the `fetch`, a network operation lasting
        # seconds, so relied on alone it would only see the branch as it was before that
        # window. Raised by the GPT review of this branch.
        src_ok, src_note = self._head_is_the_committed_sha(src)
        if not src_ok:
            self.log.error(
                "direct-push: HEAD is no longer the authorized source (%s) -- not rebasing",
                src_note,
            )
            return push
        # CAPTURE THE FETCHED TIP AS AN OBJECT ID, FROM THE FETCH ITSELF. `FETCH_HEAD` is a
        # mutable file in the clone, and the pre-push reviewer's shell can rewrite it between
        # the fetch and the rebase: the replay then lands on a SUBSTITUTED parent whose
        # content nothing scanned (the scan covers only the replayed commit), and the push
        # carries it. Keying both the rebase input and the replayed-commit count on that same
        # name made them corroborate the substitution rather than catch it. Measured on a
        # throwaway repo: with `.git/FETCH_HEAD` rewritten to a prepared child,
        # `rev-list --count FETCH_HEAD..HEAD` still reported 1 while the truth against the
        # real tip was 2, the scanned range held only our own file, and a bare remote ACCEPTED
        # the push -- the foreign file landed. With the id taken from `--porcelain` stdout the
        # same attack replays onto the real tip and the foreign file is gone from the
        # published range. Raised by the GPT review of this branch.
        fetched = _git(["fetch", "--porcelain", fetch_url, dest], self.clone)
        if fetched.returncode != 0:
            return push
        base = _fetched_tip_oid(fetched.stdout)
        if not base:
            self.log.error(
                "direct-push: the fetch did not report the tip's object id -- not rebasing"
            )
            return push
        # PIN THE COMMITTER FROM THE AUTHORIZED OBJECT, not from configuration. A replay
        # writes a NEW commit object, and git takes its committer identity from config
        # rather than from the commit being replayed -- while author and message carry
        # over. The reviewing agent's shell can write the repository's config (its git
        # denylist covers only `push` and `remote set-url`) and nothing masks a global
        # one, so a replay left to resolve identity itself publishes attribution that a
        # distrusted actor chose. `src` already holds the identity the pipeline committed
        # under, recorded before any agent ran, and it is an immutable object: reading the
        # answer off it needs no new state and no trust in any config file. Raised by the
        # GPT review of this branch.
        pin_name, pin_email = _committer_identity(src, self.clone)
        if not pin_name or not pin_email:
            self._restore_branch(dest)
            self.log.error(
                "direct-push: cannot read the authorized commit's own identity -- not" " rebasing"
            )
            return push
        # REPLAY THE RETAINED OBJECT ONTO THE CAPTURED BASE. `git rebase <base> <rev>` checks
        # out *rev* detached and replays it, so BOTH ends are immutable object ids and nothing
        # that rewrites a ref in between can substitute either one. There is no no-argument
        # form any more: *src* is required, so the branch is never the thing replayed.
        reb = _git(
            ["rebase", base, src],
            self.clone,
            config=("-c", f"user.name={pin_name}", "-c", f"user.email={pin_email}"),
        )
        if reb.returncode != 0:
            _git(["rebase", "--abort"], self.clone)
            self._restore_branch(dest)
            self.log.warning("direct-push: rebase onto %s conflicted — not pushing", dest)
            return push
        # The rebase REPLAYED the commit, so the object about to be published is a new one
        # that nothing has scanned: the caller's credential scan covered the PRE-rebase
        # object. Resolve the replacement to a full id, scan THAT, and push THAT -- so this
        # path holds the same invariant as the first push instead of trusting `HEAD`, which
        # is re-resolved by the push itself and can be moved in between by a process the
        # reviewer left behind. Raised by the GPT review of this branch.
        #
        # RESOLVED BEFORE `_reverify_head`, DELIBERATELY. That gate runs the
        # repository-under-improvement's OWN test suite, i.e. arbitrary code from the tree
        # being published, so resolving afterwards let a test teardown move HEAD and have
        # the moved commit captured, scanned and pushed as though the gate had measured it:
        # credentials still covered, but the build-verification invariant broken with no
        # recheck. Capturing first and asserting HEAD is still that object afterwards makes
        # the thing the gate measured and the thing published the same object. Raised by
        # the GPT review of this branch, which found the earlier ordering.
        rebased_id = (_git(["rev-list", "-1", "HEAD^{commit}"], self.clone).stdout or "").strip()
        verified = self._reverify_head()
        if self._retire_if_unsafe("post-rebase verification"):
            return None
        # Every refusal from here re-attaches the clone WITHOUT moving the branch: the
        # rebase left HEAD detached at the replay, and `_reset_provisional` rolls back with
        # `git reset --hard`, which on a detached HEAD would move the detachment and leave
        # the branch still carrying the provisional commit. Leaving the branch where it was
        # is also the conservative outcome -- it never points at a replay that failed a
        # check.
        if not verified:
            self._restore_branch(dest)
            return push  # rebased tree is unverified — return the original rejection
        # The emptiness refusal is deliberately HERE rather than at the capture above: the
        # capture has to precede the gate, but refusing there would preempt the retire and
        # re-verify checks and swallow the atomic clone retirement (`return None`), which is
        # a stronger outcome than a failed push. Same shape as `_direct_push`: resolve
        # early, carry the value forward, refuse in the original order.
        if not rebased_id:
            self.log.error("direct-push: cannot resolve the rebased commit -- not pushing")
            self._restore_branch(dest)
            return push
        # THE CAPTURED REPLAY MUST CARRY THE AUTHORIZED CHANGE. Everything below binds to
        # *rebased_id*, and *rebased_id* comes from ambient `HEAD` -- so a process that
        # moves HEAD in the window between the rebase returning and that read gets its own
        # commit measured, scanned, counted and published, with every check agreeing
        # because they all agree ABOUT THE SUBSTITUTE. The HEAD-equality check below cannot
        # catch it: it compares HEAD against the same captured id, so a substitution that
        # happened BEFORE the capture is corroborated rather than refused. Nothing in the
        # chain reached back to *src*, the object the caller authorized and already
        # scanned.
        #
        # Equality of ids is not available -- a replay is a new object by definition -- so
        # the binding is the RESULT: the tree a replay of *src* onto *base* must produce,
        # computed from those two immutable ids by git's own merge, against the tree the
        # captured commit actually carries. That closes the window by making the published
        # CONTENT provably the authorized content rather than by trying to make the window
        # empty; HEAD is the only handle the rebase leaves, so the window cannot be
        # removed, only made harmless.
        #
        # A TREE, NOT A PATCH IDENTITY, because a patch identity ignores hunk positions:
        # the same added and removed lines placed elsewhere in the file carry the same
        # identity and a different tree. Raised by the GPT review of this branch.
        #
        # FAIL-CLOSED, and the cost is a skipped publish, never a bad one: this cycle's
        # commit stays on the branch and the next cycle re-pushes it.
        want_tree = _expected_replay_tree(base, src, self.clone)
        have_tree = _commit_tree(rebased_id, self.clone)
        if not want_tree or not have_tree or want_tree != have_tree:
            self.log.error(
                "direct-push: the rebased commit %s does not carry the tree a replay of the"
                " authorized %s produces -- not pushing",
                rebased_id[:12],
                src[:12],
            )
            self._restore_branch(dest)
            return push
        want_attr = _commit_attribution(src, self.clone)
        have_attr = _commit_attribution(rebased_id, self.clone)
        if not want_attr or not have_attr or want_attr != have_attr:
            # THE PIN IS NOT THE GUARANTEE, this is. A `-c` override loses to the
            # ``GIT_COMMITTER_*`` environment, and author and message are carried over by
            # the replay rather than pinned at all, so the published object's attribution
            # is checked against the authorized one instead of assumed. Comparing objects
            # to each other keeps configuration out of the answer entirely.
            self.log.error(
                "direct-push: the rebased commit %s does not carry the attribution of the"
                " authorized %s -- not pushing",
                rebased_id[:12],
                src[:12],
            )
            self._restore_branch(dest)
            return push
        head_ok, head_note = self._head_is_the_committed_sha(rebased_id)
        if not head_ok:
            self.log.error(
                "direct-push: HEAD moved while the build gate ran (%s) -- not pushing",
                head_note,
            )
            self._restore_branch(dest)
            return push
        scan_ok, scan_note = self._revision_scans_clean(rebased_id)
        if not scan_ok:
            self.log.error(
                "direct-push: the rebased commit %s did not scan clean (%s) -- not pushing",
                rebased_id[:12],
                scan_note,
            )
            self._restore_branch(dest)
            return push
        # THE REBASE MUST HAVE REPLAYED EXACTLY ONE COMMIT. If the remote already carries an
        # equivalent patch, `git rebase` drops ours as already-applied and leaves HEAD at
        # the remote tip -- which every check below would then bind to, consistently, and
        # the ledger would record an unrelated commit as the one this pipeline landed. A
        # count of 0 is that case; anything above 1 means the replay is not the single
        # commit this method is documented to handle. Raised by the GPT review.
        #
        # BOTH ENDPOINTS ARE CAPTURED IDS. The base is the tip the fetch reported, not
        # `FETCH_HEAD` -- that name is a mutable file, so a count keyed on it measures the
        # same substituted state the rebase would have replayed onto, and agrees with it.
        # The far end is *rebased_id*, not ambient `HEAD`: `HEAD` resolves to whatever the
        # working tree points at when this line runs, and the build gate above executes the
        # target repository's OWN test suite, so a teardown can move it in between. The
        # equality check above proves HEAD was still the replay a moment ago, which is a
        # check-then-use pair -- naming the id removes the dependency instead of timing it.
        # It also makes the range counted identical to the range PUBLISHED: the push sends
        # *rebased_id*, so `base..rebased_id` is exactly what this adds to the remote.
        # Raised by the GPT review of this branch, one step along from the same defect in
        # the rebase input.
        replayed = (
            _git(["rev-list", "--count", f"{base}..{rebased_id}"], self.clone).stdout or ""
        ).strip()
        if replayed != "1":
            self.log.error(
                "direct-push: the rebase replayed %s commits, not 1 -- not pushing",
                replayed or "an unreadable number of",
            )
            self._restore_branch(dest)
            return push
        # THE ONE PLACE THE BRANCH IS PROMOTED, and it is reached only after the replay has
        # passed re-verification, the HEAD-identity check and the credential scan. Doing it
        # any earlier would force the branch onto whatever HEAD happened to be if a later
        # check refused -- silently, with the branch looking healthy. Raised in review.
        # A FAILED restore ABORTS the push: publishing while the clone is detached or its
        # durable branch is stale is exactly what the restore exists to prevent, so its
        # status is checked rather than logged. Raised by the GPT review.
        if not self._restore_branch(dest, promote=rebased_id):
            self.log.error("direct-push: branch restore failed -- not pushing %s", dest)
            return push
        require_pinned(self.clone)
        self._pushed_object = rebased_id
        push = subprocess.run(
            [
                "git",
                "-C",
                str(self.clone),
                *_GIT_SAFE_CONFIG,
                *hook_off_args(self.clone),
                "push",
                fetch_url,
                f"{rebased_id}:refs/heads/{dest}",
            ],
            capture_output=True,
            **UTF8_TEXT,
        )
    return push


def _prepush_review_clean(self: Driver, *, target: str, base_ref: str) -> tuple[bool, str]:
    """F10 + F6: run a REAL automated reviewer review on the just-committed fix
    diff BEFORE the direct push, and return ``(clean, note)``.

    A direct-pushed commit gets NO human review — so the automated reviewer must clear
    it first (the user's pre-push-gate decision; the F6 roadmap item). We run the SAME
    automated reviewer the post-CR watcher uses, but as a one-shot REVIEW-only verdict on
    ``base_ref...HEAD`` in the clone, via this driver's agent runner. The agent emits a
    final ``REVIEW: clean`` / ``REVIEW: <N> open`` / ``REVIEW: unavailable`` line.

    AUTHORIZATION (operator directive): the push is allowed when the fix has
    a clean POSITIVE signal — a clean review verdict OR (when the review is INCONCLUSIVE)
    a clean full build/test (``_build_test_pre_push_clean``). CONCRETE review open
    findings still BLOCK (a green build does not excuse a real review finding).
    FAIL-CLOSED: if the gate is required (``self.prepush_review``) and NEITHER signal
    is provably clean — open findings, OR (review inconclusive AND build/test red),
    no agent runner, or any error — we return ``(False, …)`` so the push is BLOCKED. An
    auto-pushed, un-reviewed, unproven commit is what the gate exists to prevent. When
    the gate is OFF (default), returns ``(True, "gate disabled")`` without running."""
    if not getattr(self, "prepush_review", False):
        return True, "pre-push review gate disabled"
    runner = self._agent_runner
    if runner is None:
        return False, "pre-push review REQUIRED but no agent runner — blocking push"
    try:
        # Self-contained review instruction. The upstream version shelled out to a
        # host-specific reviewer skill discovered on disk; that coupling is gone, so
        # the diff itself is the whole input and the reviewer is the session agent.
        prompt = (
            "You are the PRE-PUSH review gate for an autonomous bug-fix loop. A fix was\n"
            "just committed locally and is about to be PUSHED to a shared feature branch\n"
            "with NO human review, so you are the only review it will get.\n\n"
            f"Review the diff of `{base_ref}...HEAD` in this repository. Read the changed\n"
            "files for real context — do not review the diff hunks in isolation.\n\n"
            "Look for defects that would matter in review: incorrect logic, unhandled\n"
            "errors, resource leaks, race conditions, security issues, and behaviour\n"
            "changes the commit does not mention. Ignore style preferences.\n\n"
            "Do NOT push. Do NOT open a pull request. Do NOT commit or amend anything:\n"
            "this is REPORT-ONLY. A commit here replaces the object the pipeline measured\n"
            "and reproduced, which the publish gate then refuses -- so a well-meant fix\n"
            "discards the verified change. Report the finding instead. The last line MUST\n"
            "be the verdict.\n\n"
            "End your reply with EXACTLY one line: `REVIEW: clean` if there are no open\n"
            "findings on the added or changed lines, else `REVIEW: <N> open`. If you could\n"
            "not actually review the diff, say `REVIEW: unavailable` rather than guessing —\n"
            "an unfounded `clean` is the one answer that defeats this gate."
        )
        res = runner.run(
            prompt,
            cwd=str(self.clone),
            # NO `Edit`. The prompt above is report-only and gives a reviewer no
            # sanctioned reason to modify the clone. What actually determines whether a
            # reviewer CAN modify it is the tool list, not the prose, so withholding the
            # capability is what matters here. Raised by the first-principles review of
            # this branch.
            #
            # This is least privilege, NOT the enforcement boundary: `Bash` remains (the
            # review has to run git to read the diff), and a shell can write files and
            # commit. That is precisely why the identity checks below exist rather than
            # relying on what the reviewer was told or granted.
            allowed_tools=["Bash", "Read", "Grep", "Glob"],
            max_turns=30,
            timeout_s=420,
        )
        out = (getattr(res, "text", "") or "").strip()
        # Parse the LAST REVIEW verdict line (the review may print intermediate ones).
        verdict = ""
        for ln in out.splitlines():
            s = ln.strip()
            if s.upper().startswith("REVIEW:"):
                verdict = s
        low = verdict.lower()
        if "clean" in low:
            return True, "prepush_review clean"
        # the review found CONCRETE open findings → a real defect signal → BLOCK (a clean
        # build does NOT excuse open review findings; this path stays fail-closed).
        if "open" in low:
            return False, f"prepush_review found open findings: {verdict[:120]}"
        # the review was INCONCLUSIVE (unavailable / no parseable verdict). Per the operator
        # decision, an inconclusive review must NOT permanently block a fix that is
        # otherwise provably safe — fall back to a clean POSITIVE build/test signal
        # (a fresh full `bb release` / pytest suite green on the committed fix). The push
        # is authorized iff the review is clean OR the build/test is clean; it stays
        # fail-closed only when BOTH are inconclusive (06_*.md F6/F10; operator
        # directive: "clean prepush_review + bb release or other clean autotest
        # should allow push to the remote").
        reason = (
            "prepush_review unavailable"
            if "unavailable" in low
            else "prepush_review produced no clear verdict"
        )
        build_ok, build_note = self._build_test_pre_push_clean(target=target)
        if build_ok:
            return True, f"{reason}; authorized by clean build/test ({build_note})"
        return (
            False,
            f"{reason} AND build/test not clean ({build_note}) — blocking push (fail-closed)",
        )
    except Exception as e:  # noqa: BLE001 — a gate error must BLOCK, never silently pass
        return False, f"prepush_review gate error ({type(e).__name__}) — blocking push"


def _build_test_pre_push_clean(self: Driver, *, target: str) -> tuple[bool, str]:
    """Fallback pre-push signal: is the committed fix's tree a clean full build/test?

    Returns ``(clean, note)``. Runs the profile's full-suite gate (``bug_runner
    .run_suite`` — the profile-supplied build/test command, the SAME check STAYGREEN
    uses) against the clone's working tree (HEAD = the just-committed fix). A green
    suite is an independent, deterministic positive signal that the fix did not break
    the build — the operator-approved alternative to a clean review verdict when the
    autonomous review is inconclusive. Fail-closed: any missing primitive /
    error / red suite returns ``(False, …)`` so the push is still blocked unless the
    suite is PROVABLY green. (The concrete build command is the profile's concern, not
    the spine's — kept target-agnostic here.)"""
    runner = getattr(self.profile, "bug_runner", None)
    run_suite = getattr(runner, "run_suite", None)
    if not callable(run_suite):
        return False, "no build/test gate available"
    src = self.clone / "src"
    if not src.exists():
        src = self.clone
    try:
        green, failing = run_suite(src=src)
    except Exception as e:  # noqa: BLE001 — a gate error blocks, never silently passes
        return False, f"build/test gate error ({type(e).__name__})"
    if green:
        return True, "full suite green"
    return False, f"{len(failing)} failing test(s): {', '.join(failing[:3])}"


def _head_is_the_committed_sha(self: Driver, committed_id: str) -> tuple[bool, str]:
    """Is the clone's HEAD still the commit this pipeline committed? ``(ok, note)``.

    This gate holds one invariant: do not push what a worker did not commit.
    *committed_id* is the FULL object id of what :meth:`_finalize_winner_commit` /
    :meth:`_finalize_bug_winner_commit` produced -- the commit that was measured,
    reproduced and written to the ledger -- resolved by the caller BEFORE the review gate
    runs. Nothing holds the verified commit and the published one together, and the window
    between them is not empty: :meth:`_prepush_review_clean` runs an agent in THIS clone
    with ``Bash`` for up to 30 turns, and that runner's git denylist covers only ``push``
    and ``remote set-url``, so ``git commit --amend`` is permitted there. That prompt is
    report-only and grants no ``Edit``, but neither is an ENFORCEMENT BOUNDARY -- a prompt
    is an instruction to an agent that may be
    carrying an injected diff, and a shell can write files whatever the tool list says -- so
    a moved HEAD stays reachable and this check is what refuses it.

    THE ANCHOR IS A FULL OBJECT ID, which is what makes this comparison mean anything. A
    short sha is an ambiguous NAME: git reads a revision as a ref name before an
    abbreviated object id, and a prefix stops naming one object once a second object shares
    it. Either reading is arrangeable by whatever can write the repository, so a gate that
    re-resolved an abbreviation would compare two values the distrusted actor chose. The
    finalizers therefore return a validated forty-hex id and nothing downstream resolves by
    prefix; a short form is derived for DISPLAY only. Raised by the GPT review of this
    branch.

    Resolved with ``rev-list -1`` rather than ``rev-parse --verify`` because this method
    ALREADY asks a different question with ``rev-parse --verify --quiet HEAD~1`` -- "does
    a parent exist", a boolean that picks the credential scan's range. One verb carrying
    two unrelated questions is indistinguishable to a reader and to any caller keyed on
    the argv, and ``test_ai_spine_driver_coverage``'s git double is keyed on exactly that:
    its own docstring says prefixes are scripted "so a caller can pin
    ``rev-parse --verify`` separately from ``rev-parse HEAD``". One prefix, one call site.

    FAIL-CLOSED. A revision that does not resolve is the ABSENCE of the check, not a
    pass: an amended-away commit can be pruned, and "I could not look" must never read
    as "I looked and it was fine".
    """
    if not committed_id:
        return False, "cannot resolve the committed object -- refusing to publish"
    have = _git(["rev-list", "-1", "HEAD^{commit}"], self.clone)
    have_sha = (have.stdout or "").strip()
    if have.returncode != 0 or not have_sha:
        return False, "cannot resolve the clone's HEAD -- refusing to publish"
    if committed_id != have_sha:
        return False, (
            f"HEAD moved after the pipeline committed {committed_id[:12]}"
            f" (HEAD is now {have_sha[:12]}): the commit that would be published is"
            " not the one this run verified"
        )
    return True, f"HEAD is the committed revision {have_sha[:12]}"


def _direct_push(self: Driver, *, fp: str, kind: str, target: str, sha: str) -> bool | None:
    """F10: push the just-committed verified change to the operator-authorized branch.

    Returns True iff the push succeeded. Re-checks authorization at push time (never
    assumes the start-time check still holds): a protected/blank branch is refused by
    :func:`.push_policy.authorize_direct_push` — the spine-side, non-overridable gate.
    The push target is the bare branch name (``origin/x`` → ``x``), pushed to ``origin``
    explicitly via the clone's FETCH url (the push *remote* stays ``DISABLED_NO_PUSH``;
    we push to the real fetch url for this ONE ref so the global push-disable holds for
    everything else). A failed/ refused push is logged and recorded as ``error`` — the
    verified commit stays local (recoverable), nothing escapes the sandbox silently.

    Before pushing, the pre-push review gate (:meth:`_prepush_review_clean`) must pass
    when enabled — an auto-pushed commit gets no human review, so the automated reviewer
    is its gate (F6/F10; fail-closed)."""
    from .push_policy import (
        authorize_direct_push,
        normalize_branch,
    )

    ok, reason = authorize_direct_push(direct_commit=self.direct_commit, branch=self.branch)
    if not ok:
        self.log.warning("direct-push refused for %s: %s", target, reason)
        self.ledger.record(
            L.LedgerEntry(
                fp=fp,
                kind=kind,
                target=target,
                status=L.STATUS_ERROR,
                note=f"direct-push refused: {reason}"[:200],
            )
        )
        return False
    # A FAILED ROLLBACK IS TERMINAL FOR PUBLISHING. HEAD still carries a commit that was
    # refused and never published, so this winner's scan (`<rev>~1..<rev>`) reads one
    # revision while its push sends the whole ancestry -- publishing the refused content
    # through a scanner that never looked at it. Checked HERE and not only via the run's
    # stop flag because that flag is read at cycle boundaries, and a cycle can hold SEVERAL
    # bug winners (`for prop, bug_res in bug_winners`), each reaching this method. Raised by
    # the GPT review of this branch.
    if getattr(self, "_rollback_failed", False):
        reason = "a provisional rollback failed, so HEAD carries an unpublished commit"
        self.log.error("direct-push refused for %s: %s", target, reason)
        self.ledger.record(
            L.LedgerEntry(
                fp=fp,
                kind=kind,
                target=target,
                status=L.STATUS_ERROR,
                note=f"direct-push refused: {reason}"[:200],
            )
        )
        return False
    # THE FINALIZER HANDS BACK A FULL OBJECT ID, so nothing is resolved by prefix here. A
    # short sha is an ambiguous name -- git reads a revision as a ref name before an
    # abbreviated object id, and a prefix stops naming one object once a second object
    # shares it -- so a lookup keyed on one lets whatever can write the repository decide
    # which object this path anchors to. Validating the id the finalizer already produced
    # leaves no lookup to aim at. An empty or malformed value is carried forward and
    # refused below rather than raised, so a missing sha keeps its own existing message.
    # Raised by the GPT review of this branch.
    committed_id = ""
    if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
        committed_id = sha
    # PRE-PUSH REVIEW GATE (fail-closed): a direct-pushed commit gets no human review,
    # so the automated reviewer must clear it before it lands on the shared branch.
    clean, note = self._prepush_review_clean(target=target, base_ref=self.branch)
    if self._retire_if_unsafe("pre-push review"):
        self.ledger.record(
            L.LedgerEntry(
                fp=fp,
                kind=kind,
                target=target,
                status=L.STATUS_ERROR,
                note="direct-push refused: repository safety changed after review",
            )
        )
        return None
    if not clean:
        self.log.warning("direct-push BLOCKED by review gate for %s: %s", target, note)
        self.ledger.record(
            L.LedgerEntry(
                fp=fp,
                kind=kind,
                target=target,
                status=L.STATUS_ERROR,
                note=f"pre-push review gate blocked: {note}"[:200],
            )
        )
        return False
    if not sha or sha == "-":
        self.log.error("direct-push: no commit sha for %s — skipping push", target)
        self.ledger.record(
            L.LedgerEntry(
                fp=fp,
                kind=kind,
                target=target,
                status=L.STATUS_ERROR,
                note="direct-push: winner diff did not apply",
            )
        )
        return False
    # Publish only what this pipeline committed. Placed HERE
    # deliberately -- AFTER the review gate, so a reviewer that edited and amended the
    # clone is caught, and BEFORE the credential scan and the push, so an unverified
    # commit is neither scanned as though it were the verified one nor published. NOT
    # wrapped around the push itself: `_push_with_rebase` rewrites HEAD on purpose and
    # re-verifies the replayed tree, so a check there would refuse that legitimate path.
    # Refusing returns False, the same disposition every other gate in this method uses:
    # the commit stays local and recoverable and the caller rolls the provisional back.
    head_ok, head_note = self._head_is_the_committed_sha(committed_id)
    if not head_ok:
        self.log.error("direct-push REFUSED for %s: %s", target, head_note)
        self.ledger.record(
            L.LedgerEntry(
                fp=fp,
                kind=kind,
                target=target,
                status=L.STATUS_ERROR,
                note=f"direct-push refused: {head_note}"[:200],
            )
        )
        return False
    dest = normalize_branch(self.branch)
    # Resolve the real remote URL the clone FETCHES from (push remote is disabled). We
    # push HEAD (the verified commit we just made on self.branch) to the authorized ref.
    # Prefer the url the PROFILE was given (carried in config), because the clone's
    # own remote urls are both neutralized so agent-run Bash inside it cannot find a
    # push target. Falling back to the clone keeps older configs working: it yields
    # the DISABLED sentinel, which the check below refuses — fail closed, never a
    # silent unguarded push.
    fetch_url = str(getattr(getattr(self.profile, "pr_recipe", None), "fetch_url", "") or "")
    if not fetch_url:
        fetch_url = _git(["remote", "get-url", "origin"], self.clone).stdout.strip()
    if not fetch_url or "DISABLED" in fetch_url.upper():
        self.log.error("direct-push: no usable fetch url for %s — refusing", target)
        self.ledger.record(
            L.LedgerEntry(
                fp=fp,
                kind=kind,
                target=target,
                status=L.STATUS_ERROR,
                note="direct-push: no usable remote url",
            )
        )
        return False

    # Scan the CONTENT before it leaves the host. `_redact_commit_message` covers the
    # message; this covers the commit itself, which is equally unwipeable once pushed
    # and is agent-authored. Detect-and-refuse: the commit stays local and the ledger
    # records why, rather than publishing a silently-rewritten patch. The range mechanics
    # and the history behind them live in `_revision_scans_clean`.
    #
    # Scan the COMMITTED OBJECT BY ID, never through `HEAD`. Binding this to the symbolic
    # ref left a hole that no amount of re-checking closes: a background process can point
    # HEAD at a clean decoy for the duration of the `git diff` and restore it before any
    # later check, so the scanner reads the decoy, every check passes, and the real commit
    # is published having never been scanned. An id names one immutable object, so the
    # thing scanned here and the thing pushed below are the same object by construction
    # rather than by timing. `_push_with_rebase` calls the SAME helper on the replayed
    # object if it has to rebase, so neither publish path can go out unscanned.
    # Raised by the GPT review of this branch.
    scan_ok, scan_note = self._revision_scans_clean(committed_id)
    if not scan_ok:
        self.log.error("direct-push REFUSED for %s: %s", target, scan_note)
        self.ledger.record(
            L.LedgerEntry(
                fp=fp,
                kind=kind,
                target=target,
                status=L.STATUS_ERROR,
                note=f"direct-push refused: {scan_note}",
            )
        )
        return False

    # A TAMPER DETECTOR, not the thing that binds the scan -- that is the object id used
    # above and below, and this check is deliberately not load-bearing for it. Relying on
    # a post-scan re-check to prove the scan had covered the published object would be
    # wrong: the re-check is itself a check-then-use pair, so a process that swapped HEAD
    # during the diff and restored it before this line defeats both. What this still
    # catches is worth keeping: HEAD
    # differing here means something wrote the clone after the review returned, and a
    # clone that is being written by an unknown actor is not one to publish from, even
    # when the object about to be published is provably the verified one.
    head_ok, head_note = self._head_is_the_committed_sha(committed_id)
    if not head_ok:
        self.log.error("direct-push REFUSED for %s after the scan: %s", target, head_note)
        self.ledger.record(
            L.LedgerEntry(
                fp=fp,
                kind=kind,
                target=target,
                status=L.STATUS_ERROR,
                note=f"direct-push refused after the scan: {head_note}"[:200],
            )
        )
        return False

    # Publish the OBJECT ID, not ``HEAD``: an id cannot be repointed, so no process in
    # the clone can change what lands on the branch between here and the push itself.
    push = self._push_with_rebase(fetch_url, dest, target, src=committed_id)
    if push is None:
        self.ledger.record(
            L.LedgerEntry(
                fp=fp,
                kind=kind,
                target=target,
                status=L.STATUS_ERROR,
                note="direct-push refused: repository retired after safety change",
            )
        )
        return None
    # Record the OBJECT THAT WAS SENT, which `_push_with_rebase` reports -- on the fast path
    # the retained id, on the retry path the replayed one. Re-reading `HEAD` here was wrong
    # for the same reason it is wrong everywhere else on this path: HEAD can move between
    # the push and the read, and the ledger would then name an unrelated commit for a change
    # that really did land. The HEAD read is GONE rather than kept as a fallback: `src` is
    # required now, so there is no caller for whom the sent object is unknown, and a fallback
    # that re-reads HEAD is the defect wearing a smaller hat. There is no fallback to the
    # caller's own snapshot either, because it is unreachable: the ONLY return in
    # `_push_with_rebase` that precedes `self._pushed_object = src` is the retire path's
    # `return None`, and that routes to this method's own `return None` above -- before this
    # line. Raised by the GPT review, then narrowed twice by the first-principles review.
    sent = str(getattr(self, "_pushed_object", "") or "")
    self.pushed_sha = sent
    if push.returncode != 0:
        # Redact BEFORE the bound (here and at every stderr slice below): git
        # echoes the authenticated remote URL on an auth failure, and slicing
        # first can cut the credential into a fragment no later pass matches.
        # Log lines use the companion-aware log redactor; the persisted ledger
        # note keeps the baseline redact-then-bound helper.
        self.log.error(
            "direct-push FAILED for %s: %s",
            target,
            redact_log_via_context(push.stderr or "")[:300],
        )
        self.ledger.record(
            L.LedgerEntry(
                fp=fp,
                kind=kind,
                target=target,
                status=L.STATUS_ERROR,
                note=f"direct-push failed: {redact_via_context(push.stderr or '')[:150]}",
            )
        )
        return False
    self.log.info("direct-push OK: %s → origin/%s (%s)", target, dest, self.pushed_sha)
    return True
