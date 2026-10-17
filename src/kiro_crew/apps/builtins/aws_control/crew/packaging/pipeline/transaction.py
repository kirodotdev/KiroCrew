"""``build_bundle``: the one transaction that stages, promotes and rolls back a bundle.

It composes every owner below it. It stays one function because every exit path has to
release what this run acquired -- the staging tree, its descriptor and marker, the report temp
-- and restore the previous bundle, and those paths share that state.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import destination as _destination
from . import hashing as _hashing
from . import layout as _layout
from . import pinned as _pinned
from . import plan as _plan
from . import report as _report
from . import spec as _spec
from . import staging as _staging
from .candidates import Candidate
from .contract import _STAGING_OWNED_TOP_LEVEL, BUNDLE_VERSION, PLAN_FILENAME, ExportRefused
from .crew import ResolvedCrew
from .pinned import _NOFOLLOW_READ_FLAGS
from .plan import Plan
from .report import BuildReport
from .staging import _RUN_ID


def build_bundle(
    crew: ResolvedCrew,
    agent_spec: dict,
    candidates: dict[str, list[Candidate]],
    plan: Plan | None,
    out_dir: Path,
) -> BuildReport:
    """Write the four-entry bundle for *crew*, or refuse and leave nothing behind.

    "Leave nothing behind" holds for every refusal BEFORE promotion: the staging tree, its
    marker, and any temp report are cleaned and the prior bundle is left in place. There is one
    deliberate exception AFTER promotion. Publication is ordered promotion first
    (``staging.rename(out_dir)``), then the report, because a report written before a promotion
    that then fails would be a false success claim in the one artifact an operator reads as
    proof -- and a MISSING report is recoverable by regenerating where a FALSE one is not. So if
    the report publish fails after a good promotion, the NEW bundle stays installed and the
    report is absent: a partial success, not a clean refusal. This is the strictly-better of the
    two, and it is the only state in which this function returns having neither fully succeeded
    nor left nothing behind.
    """
    _pinned._refuse_without_nofollow_primitive()
    _destination._refuse_unc_out(out_dir)
    included_mcp = plan.included("mcp") if plan else set()
    included_skills = plan.included("skills") if plan else set()

    result = _spec.build_spec(crew, agent_spec, included_mcp, crew.agent_spec_path.parent)

    # The PARENT is judged first, before any of the three derived paths below exist as
    # names. Every one of them -- the staging tree, its marker, the report -- is
    # ``out_dir.parent / <something>``, so a junction at that parent silently relocates all
    # of them together, and the per-path checks further down each validate a path that is
    # already pointing somewhere else. Guarding one derived path at a time cannot catch a
    # redirect in the component they share.
    _destination._refuse_unusable_parent(out_dir, what="the bundle")
    # Resolve the shared parent ONCE, here, where it has just been validated as having no
    # redirecting ancestor. The promotion below pins THIS value by descriptor and renames
    # staging onto out_dir relative to it, so a component swapped between now and the rename
    # fails its own no-follow open rather than being re-resolved and followed. Resolving again
    # at promotion time would be a second reading of the tree that a swap could win.
    resolved_out_parent = out_dir.parent.resolve()
    staging = out_dir.parent / (out_dir.name + ".staging")
    # Beside staging, not inside: see the marker note below. Cleaned on every exit path,
    # because a marker left behind is a licence for the NEXT run to delete whatever sits at
    # that path.
    staging_marker = out_dir.parent / (out_dir.name + ".staging.owned")
    # A PLAIN FILE at either path is refused before any directory call. `exists()` is true
    # for a file, so `_walk_no_reparse(staging)` and `out_dir.iterdir()` below both raised an
    # uncaught NotADirectoryError -- reproduced for each -- and the staging directory was
    # left on disk by the crash. A refusal is the same answer the residue checks give, and
    # it arrives before anything is created.
    # ``is_symlink`` FIRST at both paths, because ``is_dir()`` follows links and so answers
    # about the target rather than the entry.
    #
    # Measured, rather than assumed: with a link at ``--out`` pointing at a directory, the
    # build SUCCEEDS and the promotion replaces the link with a real directory. The target
    # does not receive the bundle and is left orphaned, so an operator who arranged that link
    # deliberately -- pointing ``--out`` at a volume, a share, a versioned directory -- loses
    # the arrangement silently, and anything else reading through the target keeps stale
    # content while the path they published now serves the new bundle.
    #
    # The existing stranger check catches SOME of these by accident, because a target holding
    # the operator's own files trips "holds files this build does not own". It says nothing
    # when the target is empty or holds a valid previous bundle, which are the ordinary cases
    # for a deliberately placed link.
    for label, candidate in (("the staging path", staging), ("--out", out_dir)):
        if _pinned._is_redirecting_entry(candidate):
            raise ExportRefused(
                f"{label} {candidate} is a symlink. Promotion replaces that path with a real "
                f"directory, so building here would destroy the link and orphan whatever it "
                f"points at. Point --out at a real directory."
            )
    if staging.exists() and not staging.is_dir():
        raise ExportRefused(
            f"the staging path {staging} exists and is not a directory. It is derived from "
            f"--out by appending '.staging', so --out is pointing somewhere this build "
            f"cannot work. Move that file, or point --out elsewhere."
        )
    if out_dir.exists() and not out_dir.is_dir():
        raise ExportRefused(
            f"--out {out_dir} exists and is not a directory. A bundle is four entries in a "
            f"directory, so this cannot be replaced in place. Point --out at a fresh "
            f"directory or at a complete previous bundle."
        )
    # Whether an existing marker is one WE wrote. Computed here, before anything is
    # created, and passed to the write below: it is the only ownership proof in this
    # function, and the write must not decide for itself whether to remove what is there.
    marker_is_ours = _staging._marker_is_ours(staging_marker)
    if staging.exists():
        # PROOF that this build made it, not a description of what is inside. The name and
        # shape rules were here first and both are satisfied by an operator's own
        # directory: `skills` is a name this build writes, so `<out>.staging/skills/notes.txt`
        # passed the top-level check and the recursive delete then removed notes.txt.
        #
        # The digest rule the other two sites use cannot apply here. Staging is filled in
        # incrementally and its manifest is written near the end, so a crashed staging
        # directory legitimately has no digest to verify -- checking one would refuse
        # exactly the case this branch exists to clean up.
        #
        # So the marker. This build CREATES staging, so it can leave a token saying so, and
        # a directory without one was made by someone else whatever it contains. It sits
        # BESIDE staging rather than inside: `bundle_digest(staging)` is a frozen contract
        # value computed over everything in there, so a file inside would either change
        # that digest or ship inside the bundle.
        if not marker_is_ours:
            raise ExportRefused(
                f"the staging path {staging} already exists and this build did not create "
                f"it (no {staging_marker.name} beside it carrying this builder's marker). "
                f"It is derived from --out by appending '.staging', and building would "
                f"delete it recursively. Move it, or point --out elsewhere."
            )
        # It IS ours, so the older content rules still apply: they catch a staging directory
        # this build made and something else then wrote into.
        residue = sorted(
            p.relative_to(staging).as_posix()
            for p in _pinned._walk_no_reparse(staging)
            if p.relative_to(staging).parts[0] not in _STAGING_OWNED_TOP_LEVEL
            or _staging._is_shape_this_build_never_writes(p)
        )
        if residue:
            raise ExportRefused(
                f"the staging path {staging} already holds files this build did not "
                f"write ({', '.join(residue[:5])}"
                + (f", and {len(residue) - 5} more" if len(residue) > 5 else "")
                + "). It is derived from --out by appending '.staging', and building "
                "would delete it recursively. Move it, or point --out elsewhere."
            )

        # The two checks above cleared this tree BY NAME (its marker is ours, its contents
        # are ours). A bare ``shutil.rmtree(staging)`` then re-resolves ``staging`` from its
        # string, so a swap of the path -- or of a parent component -- between the checks and
        # the delete lands the recursive delete on whatever the name points at then, outside
        # --out and irreversible. This is the same name-then-delete window the previous-bundle
        # and private-aside disposals close, so it closes the same way: move the cleared tree
        # into a run-private aside under a pinned parent descriptor, re-confirm ON THE MOVED
        # ENTRY that it is still one this build owns, and only then sweep it. A tree swapped in
        # since the checks is moved (not deleted), fails the re-confirmation, is renamed back
        # untouched, and refuses. The re-confirmation reads the moved entry through the pinned
        # parent (``_verify_captured_is_staging_fd``), never by re-resolving the staging name.
        _staging._purge_via_private_aside(
            staging,
            lambda parent_fd, moved_rel: _staging._verify_captured_is_staging_fd(
                parent_fd, moved_rel, label=staging
            ),
            resolved_parent=out_dir.parent.resolve(),
        )
    # The marker path's SHAPE is judged before staging is created, for the reason stated
    # above about a plain file at either path: a refusal that arrives after ``mkdir`` leaves
    # a staging tree nothing cleans up, so the operator gets a traceback and a directory to
    # remove by hand. ``_write_nofollow`` refuses a directory here, and this is where that
    # refusal has to happen for it to cost nothing.
    if staging_marker.is_dir() and not staging_marker.is_symlink():
        raise ExportRefused(
            f"{staging_marker} is a directory. This build needs that exact path for its "
            f"staging marker, and it will not delete a directory to get it. It is derived "
            f"from --out by appending '.staging.owned'. Move it, or point --out elsewhere."
        )
    # ``exist_ok`` stays FALSE: creating the directory is how this build CLAIMS the staging
    # path, and succeeding when it already exists would put two builds in one tree.
    #
    # A ``FileExistsError`` here is the concurrent-claim loser: two builds cleared preflight
    # for the same ``--out`` and both reached this line; the winner created staging, this one
    # lost the race. It has created nothing yet, so there is no partial state to unwind --
    # translate the crash into a clean refusal so the loser gets an "already claimed" outcome
    # instead of a traceback. The pre-mkdir checks above refuse a PRE-EXISTING staging tree
    # (link, non-directory, unowned, or holding files) with a better message; this covers
    # only the narrow window between those checks and this create.
    try:
        staging.mkdir(parents=True)
    except FileExistsError:
        raise ExportRefused(
            f"the staging path {staging} was claimed by another build in progress. "
            f"One build owns a given --out at a time; re-run once the other finishes."
        )
    # Retain a no-follow descriptor on the staging tree THIS build just created. Every write
    # into staging below resolves its leaf relative to this descriptor rather than by
    # re-walking ``staging`` from its path string, so a swap of ``staging`` for another
    # directory between this ``mkdir`` and a later write cannot redirect the write outside
    # ``--out``. ``staging_fd`` is -1 on a platform without directory-descriptor support
    # (Windows), where the writes fall back to the by-name no-follow open and the whole
    # builder is POSIX-gated anyway. Closed in the transaction's ``finally`` below.
    try:
        staging_fd = (
            _pinned._open_dir_nofollow_pinned(staging) if _pinned._dir_fd_supported() else -1
        )
    except OSError as exc:
        _staging._purge_staging_best_effort(staging, resolved_out_parent)
        _staging._unlink_out_leaf_best_effort(staging_marker, resolved_out_parent)
        raise ExportRefused(
            f"cannot open the staging tree {staging} as a pinned descriptor after creating "
            f"it ({exc}); a component changed since --out was validated. Nothing was written. "
            f"Re-run the build."
        ) from exc

    def _release_pretransaction_staging(*, remove_marker: bool) -> bool:
        """Attempt an identity-checked purge, then close the descriptor exactly once."""
        nonlocal staging_fd
        active_fd = staging_fd if staging_fd != -1 else None
        purged = False
        try:
            purged = _staging._purge_staging_best_effort(
                staging,
                resolved_out_parent,
                staging_fd=active_fd,
            )
        finally:
            if staging_fd != -1:
                try:
                    os.close(staging_fd)
                except OSError:
                    pass
                staging_fd = -1
        if remove_marker:
            _staging._unlink_out_leaf_best_effort(staging_marker, resolved_out_parent)
        return purged

    try:
        _staging._write_marker_exclusive(staging_marker, ours=marker_is_ours)
    except BaseException:
        # The marker write can refuse a pre-existing foreign or redirecting marker, and it
        # runs AFTER the mkdir above. Without this, that refusal leaves the staging tree
        # behind, and the pre-mkdir checks then read it as another build's claim -- so the
        # first refusal makes every later run refuse too, for a different reason, until
        # someone deletes the directory by hand. Only the tree THIS call created is removed.
        # Do not unlink the marker here: the exclusive write may have refused a foreign one.
        _release_pretransaction_staging(remove_marker=False)
        raise

    # The swap below replaces out_dir wholesale, which is what makes a failed build
    # leave nothing half-written. But the plan command writes its review template
    # INTO this same directory, so the documented flow (plan, sign, build with the
    # same --out) had the build delete the signed plan it had just read, with no
    # message. The owner then had to regenerate and re-sign without being told why.
    #
    # Two rules, so the atomic swap survives without eating anything:
    #   1. Refuse when out_dir holds something this build does not own. Pointing
    #      --out at a directory of unrelated files is exactly when a silent
    #      recursive delete does the most damage, so it is refused by name rather
    #      than absorbed.
    #   2. Carry the plan through the staging directory, so it lands back in the
    #      new out_dir instead of being replaced along with the bundle.
    # Every probe below runs AFTER the staging tree and its ownership marker exist and
    # BEFORE the main transaction's own ``except BaseException`` cleanup: the report
    # baseline read, ``out_dir.exists()``, and the ``plan_file.is_file()`` read. A raw
    # ``OSError`` from any of them (a permission change, EIO, ESTALE) would escape every
    # handler and strand the staging tree AND the marker -- and the marker is what the next
    # run reads as another build's claim, so one transient fault refuses every later build
    # until the directory is removed by hand. The deliberate refusals inside are
    # ``ExportRefused`` (a ``RuntimeError``, not an ``OSError``), so they pass this handler
    # untouched with their own messages and their own cleanup; only a raw ``OSError`` is
    # converted here, after the same best-effort release of this call's staging and marker.
    try:
        carried_plan: bytes | None = None
        # Declared BEFORE the try, because the handler reads it. Bound inside, it would be
        # unbound for every failure that happens earlier in the block -- and the handler runs
        # on exactly those, so the restore would raise NameError and mask the real error.
        previous: Path | None = None

        # Established BEFORE the try, because the except block reads all three and a refusal
        # raised early in the body would otherwise hit UnboundLocalError -- which does not just
        # lose the rollback, it REPLACES the real refusal with a confusing one. Found exactly
        # that way: 13 tests turned red naming UnboundLocalError instead of the ExportRefused
        # they assert.
        #
        # The report is written before the swap on purpose -- a report failure must not land
        # after the previous bundle is gone -- and that ordering is what leaves the other hole:
        # a rename failure restores the previous bundle while the report still describes the new
        # one that never landed. The transaction has to cover both files or it covers neither.
        report_path = out_dir.parent / f"{out_dir.name}.smc-bundle.json"
        report_before: bytes | None = None
        if report_path.is_file() and not _pinned._is_redirecting_entry(report_path):
            # Fail closed rather than treat an unreadable existing report as absence. On the
            # rollback path below, ``report_before is None`` means "no report was here, so unlink
            # the one this run wrote" -- if a read failure quietly set it to None, a rollback
            # would DELETE the operator's existing report instead of restoring it. The read is
            # the only thing that tells "no report" from "a report we could not read".
            # Read the baseline through the whole-window no-follow reader, not ``read_bytes``,
            # which follows every component: a parent/intermediate swapped after the leaf check
            # above would be traversed and the drift/rollback baseline taken from outside --out.
            # Inside this ``is_file()`` branch a ``None`` return means unreadable or redirected,
            # never absent, so it fails closed the same way the old ``OSError`` branch did.
            report_before = _pinned._read_bytes_openat(report_path.parent, Path(report_path.name))
            if report_before is None:
                # Release the staging tree and marker this build already created before refusing.
                # This refusal sits BEFORE the main transaction's own cleanup, so without this the
                # correct refusal would leak the tree and -- worse -- the ownership marker, which
                # the next run reads as another build's claim and refuses on, turning one refusal
                # into a standing one until someone deletes the directory by hand. A refusal must
                # release what this build acquired, not only report the reason.
                _release_pretransaction_staging(remove_marker=True)
                raise ExportRefused(
                    f"the existing report at {report_path} cannot be read or a component of its "
                    f"path changed to a link, so this build cannot restore it if the swap fails "
                    f"and will not risk deleting it. Fix or remove that file."
                )
        report_written = False
        promoted = False
        report_tmp = report_path.parent / (report_path.name + f".{_RUN_ID}.tmp")
        if out_dir.exists():
            # The SAME vocabulary the staging check above uses. It was briefly written
            # out twice, which is the duplicate-spelling mistake this branch has paid for
            # more than once: two copies of one rule drift, and here the drift would be
            # one of the two recursive deletes quietly accepting a name the other
            # refuses.
            # One function owns all three rules (names, shapes, the manifest's own digest),
            # because this site had all three and the `<out>.previous` site below had only the
            # first two -- reported as a defect for precisely the case the third one catches.
            # Both are about to run a recursive delete, so they cannot be allowed to drift.
            try:
                _staging._refuse_unless_this_build_wrote_it(out_dir, "--out", crew.name)
            except ExportRefused:
                _release_pretransaction_staging(remove_marker=True)
                raise
            plan_file = out_dir / PLAN_FILENAME
            if plan_file.is_file():
                # Inside the cleanup transaction, and translated. This read sat OUTSIDE the
                # ``except ExportRefused`` above, so an unreadable plan -- a permission change, a
                # file that became a directory, a device node -- raised a bare OSError past every
                # handler and left the staging tree and its marker on disk. The marker is worse
                # than the tree: it is what authorises the NEXT run's recursive delete.
                carried_plan = _pinned._read_bytes_openat(out_dir, Path(PLAN_FILENAME))
                if carried_plan is None:
                    _release_pretransaction_staging(remove_marker=True)
                    raise ExportRefused(
                        f"the existing plan at {plan_file} cannot be read or a component of its "
                        f"path changed to a link, so this build cannot carry it across the swap "
                        f"and will not replace the bundle without it. Fix or remove that file."
                    )
    except OSError as exc:
        staging_released = _release_pretransaction_staging(remove_marker=True)
        if staging_released:
            retry_guidance = (
                "the staging tree was released and ownership-marker cleanup was attempted. "
                "Re-run the build."
            )
        else:
            # ``False`` covers more than an incomplete sweep: the tree may have been moved away
            # by another process before capture, or refused by verification and restored. So
            # say deletion is unconfirmed and name every place the residue can be, rather than
            # assert it is in one of two.
            retry_guidance = (
                f"the staging tree at {staging} could not be released safely and its deletion "
                f"was not confirmed; it may still be there, under a .smc-purge-* directory "
                f"beside it, or wherever another process moved it. Check for it and remove it "
                f"before retrying. Ownership-marker cleanup was attempted."
            )
        raise ExportRefused(
            f"a filesystem fault while preparing to build into {out_dir} ({exc}); "
            f"{retry_guidance}"
        ) from exc

    try:
        _sfd = staging_fd if staging_fd != -1 else None
        _layout._write_guarded(
            staging / "agent.json",
            json.dumps(result.spec, indent=2, ensure_ascii=False) + "\n",
            "agent.json",
            staging_fd=_sfd,
            rel="agent.json",
        )
        _layout._write_guarded(
            staging / "mcp.json",
            json.dumps({"mcpServers": result.mcp}, indent=2, ensure_ascii=False) + "\n",
            "mcp.json",
            staging_fd=_sfd,
            rel="mcp.json",
        )
        skills_dst = staging / "skills"
        if _sfd is not None:
            # Create skills/ relative to the retained staging descriptor, not by re-resolving
            # ``staging / "skills"``, so a swap of staging cannot place it elsewhere.
            try:
                os.mkdir("skills", 0o700, dir_fd=_sfd)
            except FileExistsError:
                pass
        else:
            skills_dst.mkdir(exist_ok=True)  # MUST exist even when empty
        for cid in sorted(included_skills):
            skill_dir = crew.skills_root / cid
            # ``is_dir()`` follows, so a selected skill replaced by a junction between the
            # review and this copy would answer True and be copied THROUGH to its target.
            # Checked by ``lstat`` first: the pin recheck below compares the staged bytes to
            # the reviewed hash, but a redirect that names a share has already been probed by
            # then, and on Windows that probe is the credential exchange.
            if _pinned._is_redirecting_entry(skill_dir):
                raise ExportRefused(
                    f"selected skill {cid} is a link or a reparse point, so copying it would "
                    f"take bytes from wherever it points rather than from the crew."
                )
            if not skill_dir.is_dir():
                raise ExportRefused(f"selected skill has gone: {cid}")
            written = _layout._copy_skill(
                skill_dir, cid, skills_dst, included_skills, staging_fd=_sfd
            )
            # Re-hash the STAGED copy against the reviewed pin. ``verify()`` compared
            # the pin to a hash taken at ENUMERATION time, and this copy reads the
            # source directory again -- two moments, with the source writable in
            # between. Losing that race would put bytes nobody reviewed into a signed
            # bundle, which is the one thing the signature is supposed to prevent.
            #
            # Hashing the copy rather than re-reading the source is what makes this
            # closed rather than merely narrower: what the source says afterwards does
            # not matter, because what is checked is the artifact that ships.
            #
            # A MISSING pin is deliberately not re-refused here. ``verify()`` owns
            # that refusal, and the deny-by-default mutation test probes exactly this
            # path with pins absent, so a second refusal would change its outcome.
            reviewed = plan.pins.get("skills", {}).get(cid, "") if plan else ""
            if reviewed:
                staged = _hashing._staged_tree_hash(skills_dst / cid, skill_dir, written)
                if staged != reviewed:
                    raise ExportRefused(
                        f"skills/{cid} changed while the bundle was being written, so "
                        f"the copy that would ship is not the copy that was approved."
                        f"\n  reviewed: {reviewed}\n  staged:   {staged}\n"
                        f"Re-run the plan command and look again."
                    )

        digest = _hashing.bundle_digest(staging)
        _layout._write_guarded(
            staging / "manifest.json",
            json.dumps(
                {
                    "bundle_version": BUNDLE_VERSION,
                    "crew_name": crew.name,
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "digest": digest,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            "manifest.json",
            staging_fd=_sfd,
            rel="manifest.json",
        )
        # The previous bundle is MOVED ASIDE, not deleted. `rmtree(out_dir)` followed by
        # `staging.rename(out_dir)` would be two operations, and a failure between them
        # would leave NOTHING: the old bundle already gone, and the `except BaseException`
        # below deleting staging too, taking the new bundle and the carried plan with it.
        # The swap is the last thing that happens in ORDER; the aside is what makes it
        # safe to fail.
        #
        if carried_plan is not None:
            # AFTER the digest, deliberately.
            #
            # The plan is the OPERATOR's file. ``_cmd_plan`` writes it into --out, the
            # operator edits and signs it, and the next build carries it forward -- so it is
            # expected to differ between builds, which is what
            # ``test_the_plan_flow_still_works`` pins by editing it and rebuilding. Putting it
            # inside the digest would make every such edit break the rebuild preflight.
            #
            # And it protects nothing, because nothing reads it. The container consumes four
            # entries -- manifest.json, agent.json, mcp.json, skills/ (``BUNDLE_ENTRIES``) --
            # and ``crew/runtime/**`` contains no reference to the plan filename at all. What
            # ships was decided at build time and is covered by the digest; the plan beside it
            # is a record for the humans, living in that directory for convenience.
            #
            # Into staging rather than back into out_dir after the rename: the swap stays the
            # last thing that happens, so a failure above leaves the existing directory and
            # its plan untouched.
            # Re-read before writing back, and refuse if it changed. The bytes above were
            # taken before the build ran, so an operator who edited and re-signed the plan
            # while it ran would have that edit silently replaced by the stale copy -- and
            # the plan is THEIR file, the one they sign. Refusing costs them a rebuild;
            # overwriting costs them a signature they have to reproduce without being told
            # it was lost.
            current_plan = _pinned._read_bytes_openat(out_dir, Path(PLAN_FILENAME))
            if current_plan is None:
                # Fail closed rather than skip the concurrent-edit guard. If this read failed
                # and we treated it as carried, the guard below would be bypassed and
                # ``carried_plan`` -- the stale copy read at the start -- would be written over
                # the operator's signed plan. An unreadable-or-redirected plan at write-back
                # time is exactly when we must NOT write, so refuse and leave their file alone.
                _staging._purge_staging_best_effort(
                    staging,
                    resolved_out_parent,
                    staging_fd=_sfd,
                )
                _staging._unlink_out_leaf_best_effort(staging_marker, resolved_out_parent)
                raise ExportRefused(
                    f"{plan_file} could not be re-read before carrying it across the swap "
                    f"(unreadable, or a component of its path changed to a link), so this "
                    f"build cannot confirm it is unchanged and will not risk overwriting it "
                    f"with the copy read at the start. Nothing was installed and the existing "
                    f"bundle is untouched. Re-run the build."
                )
            if current_plan != carried_plan:
                _staging._purge_staging_best_effort(
                    staging,
                    resolved_out_parent,
                    staging_fd=_sfd,
                )
                _staging._unlink_out_leaf_best_effort(staging_marker, resolved_out_parent)
                raise ExportRefused(
                    f"{plan_file} changed while this build was running, so carrying the "
                    f"copy read at the start would discard that edit. Nothing was "
                    f"installed and the existing bundle is untouched. Re-run the build to "
                    f"pick up the current plan."
                )
            # No-follow, like every other staged leaf: the staging tree lives beside --out in a
            # directory this build does not own, so a same-UID process can plant a symlink at
            # this leaf in the window after ``staging.mkdir`` and a following ``write_bytes``
            # would truncate whatever the link named and ship a redirect as the plan. Written
            # through the bytes no-follow primitive so a link at the leaf is refused at open,
            # and the signed plan lands byte-for-byte.
            _destination._write_bytes_nofollow(
                staging / PLAN_FILENAME, carried_plan, staging_fd=_sfd, rel=PLAN_FILENAME
            )
        # A rename within one directory is atomic, so at every instant either the old
        # bundle or the new one is at out_dir, and the aside copy is deleted only after
        # the new one is in place.
        if out_dir.exists():
            previous = out_dir.parent / (out_dir.name + ".previous")
            if _pinned._is_redirecting_entry(previous):
                # Before ``exists()``, which follows the link. This path is derived from
                # --out, so a redirect here aims the ownership check and the rmtree below it
                # at somewhere else entirely -- and the check would pass, because it would be
                # examining whatever the link points at. ``staging`` and ``out_dir`` carry
                # the same check; this is the third path derived from --out.
                raise ExportRefused(
                    f"the aside path {previous} is a link or junction. The previous bundle is "
                    f"moved there and then deleted, so following a redirect would delete "
                    f"somewhere this build was never pointed at. Remove it, or point --out "
                    f"elsewhere."
                )
            if previous.exists():
                # The SAME three rules --out gets, from the same function. This path is
                # derived from --out, so `<out>.previous` can be a directory the operator
                # put there themselves -- and one holding their own regular files under
                # bundle names passed the earlier two-rule version of this check and was
                # deleted. The manifest digest is the rule that tells their directory from
                # one this build wrote.
                # Delete through a RUN-PRIVATE aside, and verify ownership on the MOVED tree
                # rather than at this path. A plain ``rmtree(previous)`` re-resolves the path
                # string, so even an identity check taken immediately before it leaves a
                # window; verifying at the path before the rename has the same window in the
                # other order, because what the rename then captures need not be what was
                # verified. Instead ``_purge_via_private_aside`` atomically ``rename``s
                # ``previous`` into a directory THIS build just created and owns exclusively,
                # then runs the ownership check on the entry the rename captured -- now at a
                # path no other writer holds and so unswappable -- and deletes only if it
                # passes, restoring a swapped-in operator tree untouched otherwise. The
                # verified inode and the deleted inode are one and the same.
                _staging._purge_via_private_aside(
                    previous,
                    lambda parent_fd, moved_rel: _staging._verify_build_wrote_captured_fd(
                        parent_fd, moved_rel, "the aside path", crew.name, label=previous
                    ),
                    resolved_parent=resolved_out_parent,
                )
            # The same binding the aside path gets, for the same reason. ``out_dir`` was
            # verified as a tree this build wrote far above, and a rename here acts on
            # whatever the name IS by now: a tree swapped in between is moved to
            # ``previous`` unverified, the new bundle is then promoted over the original
            # path, and the ownership check that would have objected runs afterwards, when
            # the operator's data is already somewhere they did not put it. Capturing into
            # a run-private directory first makes the verified entry and the kept entry one
            # and the same, and a tree this build did not write is returned to where it came
            # from before anything is promoted.
            _staging._dispose_via_private_aside(
                out_dir,
                lambda parent_fd, moved_rel: _staging._verify_build_wrote_captured_fd(
                    parent_fd, moved_rel, "--out", crew.name, label=out_dir
                ),
                lambda moved_rel, pfd: os.rename(
                    moved_rel, previous.name, src_dir_fd=pfd, dst_dir_fd=pfd
                ),
                resolved_parent=resolved_out_parent,
            )
        # The report is written BEFORE the swap, which is the point of no return.
        #
        # Written here rather than by the caller after ``build_bundle`` returns -- and by then
        # this function had already renamed the previous bundle aside AND deleted it, so a
        # report write that failed left the operator with a non-zero exit code, no report, and
        # their previous bundle gone. A failure that has already replaced what it was going to
        # replace is the worst shape a failure can have.
        #
        # Everything the report says is known here: the digest was computed above, the
        # destination is out_dir, and the plan and candidates are arguments. So there is no
        # reason for it to happen later, and moving it up means a failure lands inside the
        # ``except BaseException`` below, which restores the previous bundle.
        # Written to a sibling temp and PUBLISHED by an exclusive hard link, not written in
        # place. ``_write_nofollow`` opens with ``O_TRUNC``, so a write that fails partway
        # has already emptied the old report while ``report_written`` is still False and the
        # rollback below does not fire -- the one shape the rollback cannot see. The link
        # publish is atomic within the directory, so the destination holds either the previous
        # bytes or the complete new ones and never a truncated mix.
        _report._write_report_temp(
            report_tmp,
            crew_name=crew.name,
            bundle_dir=out_dir,
            digest=digest,
            skill_count=len(included_skills),
            mcp_servers=sorted(result.mcp),
            denied=_plan._denied_list(candidates, plan),
        )
        # The DESTINATION's shape is judged here so a planted link at the report path is
        # refused with a clear message before the publish. The exclusive-link publish would
        # itself refuse a link at the name (it is not a regular file this build wrote), but an
        # in-place ``O_NOFOLLOW`` open is the primitive that states WHY, and a shape check
        # gives the operator the reason at the earliest point. So the two properties are kept
        # separately -- shape checked before, atomicity by the exclusive link after.
        if _pinned._is_redirecting_entry(report_path):
            raise ExportRefused(
                f"{report_path} is a link or junction. The report is written at a path "
                f"derived from --out, and publishing over the link would orphan whatever it "
                f"named. Move it, or point --out elsewhere."
            )
        if report_path.exists() and not report_path.is_file():
            raise ExportRefused(
                f"{report_path} exists and is not a plain file, so the report cannot "
                f"replace it. It is derived from --out; point --out elsewhere."
            )
        # Content drift is judged BEFORE promotion, not only inside ``_publish_report``. The
        # report is one of the values this build wrote and reads back, and "same object, still
        # readable" is not "same content": a concurrent process that edits it in place leaves a
        # readable regular file with different bytes, which the shape checks above pass. The
        # build owns the report exclusively for one build (it writes it only through the atomic
        # publish, never in place), so its bytes must still equal what was read at the start
        # (``report_before``) or be absent. A mismatch is a foreign edit, and it is refused HERE
        # -- before ``staging.rename`` -- because refusing after promotion is too late: the
        # rollback's "promoted and not report_written" branch would then UNLINK the report,
        # destroying the very edit this guard exists to protect. Refusing before promotion
        # leaves the prior bundle restored and the foreign report untouched. ``_publish_report``
        # repeats the check descriptor-relative to close the window between here and the publish.
        if report_before is not None and report_path.is_file():
            if _pinned._read_text_nofollow(report_path) != report_before.decode(
                "utf-8", errors="replace"
            ):
                raise ExportRefused(
                    f"{report_path} was edited by another process while this build ran "
                    f"(its bytes changed since the build started). The report is written "
                    f"only through an atomic publish, so an in-place change is a foreign "
                    f"edit; refusing to overwrite it rather than destroy that write. "
                    f"Re-run the build once nothing else is writing there."
                )
        # The report is published by an exclusive hard link, which is a filesystem CAPABILITY:
        # answer whether this directory can do it BEFORE the irreversible promote, because
        # ``_publish_report`` runs after ``promoted = True`` and an unsupported-link failure
        # there would unwind a good promotion. A refusal here leaves the prior bundle untouched.
        _report._refuse_report_dir_without_hard_link_support(report_path)
        # Promote FIRST, publish the report only once the outcome is known. The report is the
        # proof an operator reads INSTEAD of checking the bundle exists, so it must describe
        # what happened, never an assumed outcome: writing it before ``staging.rename`` meant a
        # promotion that then failed left a report claiming success -- a lie in the one artifact
        # offered as evidence. Ordering it after the rename costs at most a MISSING report when
        # the report write itself fails after a good promotion (recoverable: regenerate), which
        # is strictly better than a false one. The staging-shape checks above stay before,
        # because they are destination validation, not the outcome.
        # Promote by renaming staging onto out_dir RELATIVE to the parent pinned by
        # descriptor, not ``staging.rename(out_dir)``. A bare rename re-resolves both path
        # strings, so a parent or intermediate component swapped for a link after --out was
        # validated -- and before this rename -- would land the promotion wherever the link
        # points. ``resolved_out_parent`` was resolved once at validation; opening it
        # ``O_NOFOLLOW`` at every component refuses a component swapped since, and both names
        # are single leaves under it. Same descriptor-relative shape the report publish and
        # the aside purge use. A pinned-open failure refuses BEFORE ``promoted`` is set, so the
        # rollback below restores the previous bundle and nothing is left half-promoted.
        try:
            promote_parent_fd = _pinned._open_dir_nofollow_pinned(
                resolved_out_parent, already_resolved=True
            )
        except OSError as exc:
            raise ExportRefused(
                f"cannot promote the bundle into {out_dir}: a component of its directory "
                f"changed to a link or is no longer an openable directory since --out was "
                f"validated ({exc}). Nothing was installed and the existing bundle is "
                f"untouched. Point --out elsewhere."
            ) from exc
        try:
            # The parent is pinned, but ``staging.name`` under it is still a NAME resolved at
            # rename time. If the staging leaf itself was swapped for another directory since
            # ``staging_fd`` was opened -- the same-UID plant this whole path guards against --
            # the pinned-parent rename would promote whatever now sits at that name, not the
            # inode this build staged and verified. So confirm the name still resolves to the
            # captured inode: open it no-follow under the pinned parent and compare (st_dev,
            # st_ino) to the retained descriptor. This is the publish-side twin of the delete
            # path's "the inode verified is the inode deleted" -- here, the inode created is the
            # inode published. A mismatch or an open failure refuses BEFORE ``promoted`` is set,
            # so the rollback restores the previous bundle and nothing is half-promoted.
            if staging_fd != -1:
                try:
                    check_fd = os.open(
                        staging.name,
                        os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW_READ_FLAGS,
                        dir_fd=promote_parent_fd,
                    )
                except OSError as exc:
                    raise ExportRefused(
                        f"cannot promote the bundle into {out_dir}: the staging entry "
                        f"{staging.name} could not be reopened as the directory this build "
                        f"created ({exc}). It may have been replaced since it was staged. "
                        f"Nothing was installed and the existing bundle is untouched. Re-run "
                        f"the build once nothing else is writing there."
                    ) from exc
                try:
                    captured = os.fstat(staging_fd)
                    present = os.fstat(check_fd)
                finally:
                    os.close(check_fd)
                if (captured.st_dev, captured.st_ino) != (present.st_dev, present.st_ino):
                    raise ExportRefused(
                        f"cannot promote the bundle into {out_dir}: the staging entry "
                        f"{staging.name} is no longer the directory this build staged (its "
                        f"inode changed, so it was swapped for another entry since it was "
                        f"created). Refusing to publish it. Nothing was installed and the "
                        f"existing bundle is untouched. Re-run once nothing else is writing "
                        f"there."
                    )
            os.rename(
                staging.name,
                out_dir.name,
                src_dir_fd=promote_parent_fd,
                dst_dir_fd=promote_parent_fd,
            )
        finally:
            os.close(promote_parent_fd)
        promoted = True
        _report._publish_report(report_tmp, report_path, report_before)
        report_written = True
    except BaseException:
        active_fd = staging_fd if staging_fd != -1 else None
        try:
            _staging._purge_staging_best_effort(
                staging,
                resolved_out_parent,
                staging_fd=active_fd,
            )
        finally:
            if staging_fd != -1:
                try:
                    os.close(staging_fd)
                except OSError:
                    pass
                staging_fd = -1
        # Every cleanup unlink below targets a file DERIVED from --out (the staging marker, the
        # report temp, the report) in a directory this build does not own, so each goes through
        # ``_unlink_out_leaf_best_effort``: descriptor-relative to the validated parent, and
        # LEAVING RESIDUE if that parent cannot be pinned rather than deleting on a guess of
        # where a swapped path now points. A bare ``Path.unlink`` here re-resolves the name and
        # a swapped parent component steers it outside the validated parent.
        _staging._unlink_out_leaf_best_effort(staging_marker, resolved_out_parent)
        # Roll the report back to exactly what was there, which for the ordinary first build
        # is nothing. Only when this run wrote it: an earlier failure leaves the operator's
        # own file untouched, and restoring bytes we never replaced would be a second bug.
        # The temp is removed whether or not the write reached the rename: a failure before
        # the rename leaves it behind, and it carries this run's id so it cannot be mistaken
        # for another build's.
        _staging._unlink_out_leaf_best_effort(report_tmp, resolved_out_parent)
        if report_written and not promoted:
            # The report was published but promotion did not complete -- restore exactly
            # what was there so no report claims a bundle that is not present.
            # ``report_written`` without ``promoted`` cannot happen in the normal order
            # (promote precedes the report), so this covers only an out-of-order failure;
            # it stays for safety.
            if report_before is None:
                _staging._unlink_out_leaf_best_effort(report_path, resolved_out_parent)
            else:
                _destination._write_nofollow(
                    report_path, report_before.decode("utf-8", errors="strict")
                )
        if promoted and not report_written:
            # Promotion landed and the report did not. The comment above the ordering
            # accepts a MISSING report as the cost of promoting first, because a missing one
            # is recoverable by regenerating. On a REBUILD the actual outcome is worse and
            # not what the ordering assumed: the PREVIOUS build's report is still sitting
            # there, describing a bundle this promotion has already replaced. Measured:
            # after a failed publication the file on disk was byte-identical to the first
            # build's, digest included, while the new bundle was promoted.
            #
            # Removed rather than rolled back -- but ONLY the stale previous-build report
            # this ordering is responsible for. The publish step refuses to overwrite a
            # foreign in-place edit (same-object-different-content) precisely so it is not
            # destroyed; unlinking unconditionally here would destroy that same foreign
            # write on the way out, undoing the refusal. So the delete is CONDITIONAL:
            # remove the report only while its bytes still equal ``report_before`` (the
            # stale description this branch owns). If they drifted -- a concurrent foreign
            # edit -- or a foreign report was created where there was none
            # (``report_before is None`` but a file is now there), the write belongs to
            # someone else and is LEFT in place. A missing report is the cost the ordering
            # already accepts; destroying a foreign write is not.
            current = _pinned._read_text_nofollow(report_path)
            before_text = (
                None if report_before is None else report_before.decode("utf-8", errors="replace")
            )
            if current is not None and current == before_text:
                _staging._unlink_out_leaf_best_effort(report_path, resolved_out_parent)

        # If promotion did not complete, put the previous bundle back: a failed replacement
        # must leave the prior bundle reachable, never delete or orphan what was already there.
        # Keyed on ``promoted`` (not a re-stat of out_dir) so the contract reads directly.
        # The restore is descriptor-relative, NOT ``previous.rename(out_dir)``: a bare rename
        # re-resolves both path strings, so a parent component swapped since --out was validated
        # would land the restore -- and any directory already at ``out_dir`` -- wherever the
        # link points. ``previous`` and ``out_dir`` are single leaves under the same parent
        # (``previous = out_dir.parent / (out_dir.name + ".previous")``), so both are reached
        # through ``resolved_out_parent`` pinned ``O_NOFOLLOW``, the same shape the promotion
        # used. Best-effort like the cleanup around it: a restore that cannot complete must not
        # raise a second exception over the one unwinding, so a failed pin-open or rename is
        # swallowed here, leaving the previous bundle at its ``.previous`` name to recover by
        # hand rather than crashing the operator's build on the way out.
        if previous is not None and not promoted:
            try:
                restore_parent_fd = _pinned._open_dir_nofollow_pinned(
                    resolved_out_parent, already_resolved=True
                )
            except OSError:
                restore_parent_fd = -1
            if restore_parent_fd != -1:
                try:
                    # Refuse to clobber: only restore when nothing sits at out_dir's leaf.
                    try:
                        os.stat(out_dir.name, dir_fd=restore_parent_fd, follow_symlinks=False)
                        out_dir_present = True
                    except FileNotFoundError:
                        out_dir_present = False
                    except OSError:
                        out_dir_present = True
                    if not out_dir_present:
                        try:
                            os.rename(
                                previous.name,
                                out_dir.name,
                                src_dir_fd=restore_parent_fd,
                                dst_dir_fd=restore_parent_fd,
                            )
                        except OSError:
                            # previous already gone, or a component changed: leave the aside in
                            # place to recover by hand rather than raise over the unwind.
                            pass
                finally:
                    os.close(restore_parent_fd)
        raise
    if staging_fd != -1:
        os.close(staging_fd)
        staging_fd = -1
    _staging._unlink_out_leaf_best_effort(staging_marker, resolved_out_parent)
    if previous is not None:
        # Delete the aside bundle through the same move-verify-delete as the leftover purge,
        # not a bare ``rmtree(previous)``. This runs after the earlier ``_is_redirecting_entry``
        # check on ``previous``, and ``rmtree`` re-resolves the path string, so a swap between
        # that check and this delete would land the recursive delete on whatever the path names
        # now -- "build-owned by construction" does not hold once the path is re-resolved. The
        # aside was made by this build's own ``out_dir`` rename, so the verifier confirms
        # exactly that and a swapped-in tree is restored, never deleted; the delete itself runs
        # through a parent pinned by descriptor.
        _staging._purge_via_private_aside(
            previous,
            lambda parent_fd, moved_rel: _staging._verify_build_wrote_captured_fd(
                parent_fd, moved_rel, "the aside path", crew.name, label=previous
            ),
            resolved_parent=resolved_out_parent,
        )

    # The number of skills SHIPPED, which is the number of selected ids -- not the number
    # of top-level entries under skills/. A skill id comes from
    # ``relative_to(skills_root).as_posix()`` and may nest, so "aws/ec2" and "aws/s3" are
    # two skills sharing one top-level "aws" directory; counting directories reported 1
    # for that pair, in the human output and in SMC_BUNDLE_JSON alike. ``included_skills``
    # is the set the plan selected and ``_copy_skill`` was driven from, so it is the same
    # population the bundle now contains.
    skill_count = len(included_skills)
    return BuildReport(
        bundle_dir=out_dir,
        digest=digest,
        skill_count=skill_count,
        mcp_servers=sorted(result.mcp),
        denied=_plan._denied_list(candidates, plan),
        notes=result.notes,
    )
