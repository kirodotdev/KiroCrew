"""Opt-in, count-based retention of this install's own archives.

This is the backup engine's only path that erases object versions permanently. It
is off unless an operator stores a usable count. Ownership is proven down to the
recorded version id, and the count and consent are both re-read at the moment of
deletion. Every terminal outcome is audited. The per-account count, and the last
sweep's measurements the status route serves, are read and written here as well.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Any

from kiro_crew.apps.builtins.aws_control.backend import storage
from kiro_crew.apps.builtins.aws_control.backend.backup_parts import _FACADE_MODULE
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.catalog import _archive_sort_key
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.identity import (
    KEY_SEP,
    KIND_SUBPATHS,
    LABEL_OBJECT_NAME,
    _key_basename,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.ledger import (
    retention_owned_keys,
    uploaded_versions,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.state import (
    _account_state,
    _account_view,
    _account_view_checked,
    _locked_state_update,
    _state_path,
    a_retained_archive_carries_conversations,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.uploads import (
    SEL_OP_RETENTION,
    _authorize_upload,
)
from kiro_crew.platform.context import redact_log_via_context
from kiro_crew.platform_compat import file_lock, open_lock_file
from kiro_crew.sel import sel

logger = logging.getLogger(_FACADE_MODULE)


#: The floor, and not a style choice: at ``keep=0`` the sweep would delete the
#: archive the run has just uploaded, so a backup would end by destroying itself.
#: Every configured value is clamped through :func:`_clamp_retention_keep`. The
#: newest complete archive is protected SEPARATELY from this floor, because a floor
#: is a property of the number and the protection must not depend on the number.
RETENTION_KEEP_MIN = 1


#: Where the count lives in ``backup.json``, and the whole switch: this key present
#: and holding a usable count is the only thing that enables retention. Absent, or
#: holding anything else, means keep everything -- there is no default that deletes.
#:
#: Deleting an object version cannot be undone, and these are the operator's bytes
#: in the operator's bucket, so the two ways of being wrong do not compare. Shipping
#: off costs storage an operator can see in a listing and fix with one command;
#: shipping on silently destroys archives somebody was deliberately keeping and
#: leaves them nothing to restore from. It is also the house shape for this kind of
#: switch rather than a new policy: ``nightly`` ships off, reads fail-closed, and is
#: never inferred from an adjacent grant. Retention defaulting to delete would be
#: the first capability here to act destructively on operator data unasked.
#:
#: Read only through :func:`_retention_keep_for_sweep`, never inferred from
#: ``nightly`` or any other grant in either direction: authorizing unattended
#: UPLOADS is not authorizing permanent DELETES.
#:
#: Per ACCOUNT, because the drive is per account: two connected accounts are two
#: buckets and two bills, and one number for both would apply a decision made about
#: one to the other.
RETENTION_KEEP_STATE_KEY = "retention_keep"


#: Where the last sweep's unclaimed measurement lives in ``backup.json``: per account,
#: then per kind, ``{"archives": int, "bytes": int, "at": iso8601}``.
#:
#: An archive holds a ``keep`` slot only while its version id is recorded, so a key this
#: install remembers with no recorded version -- pushed before the record existed, an
#: unversioned bucket, a put response naming none -- can never be retired, and its bytes
#: are billed permanently. The sweep already measures that floor, but the two numbers
#: reached only :data:`SEL_OP_RETENTION` and a log line -- neither of which an operator
#: reads while deciding whether retention is bounding their bill. So the floor belongs
#: where the count itself is read.
#:
#: It is a floor ON THE REMEMBERED SET, not over the whole prefix. The sweep counts only
#: keys in :func:`retention_owned_keys`, so a key with neither an ``uploads`` entry nor a
#: version record is filtered out before this measurement and reads 0 here however many
#: bytes it holds. That is the contract rather than an omission: counting such a key
#: here would mean attributing an object this install has no record of, and this pair is
#: read against the ``keep`` count to see what retention will collect out of the set it
#: can see. Those keys are counted separately and claim nothing -- see
#: :data:`RETENTION_UNRECORDED_STATE_KEY`, which reaches the same status read and the
#: same audit event.
#:
#: Stamped because it is the LAST SWEEP's measurement and not a live read: a manual
#: :func:`storage.delete_key` between sweeps leaves the number high until the next one,
#: and a reader cannot tell a stale number from a current one without knowing when it
#: was taken.
RETENTION_UNCLAIMED_STATE_KEY = "retention_unclaimed"


#: Where the last sweep's count of LISTED-BUT-UNRECORDED objects lives in
#: ``backup.json``: per account, then per kind,
#: ``{"objects": int, "bytes": int, "at": iso8601}``.
#:
#: This pair makes NO ownership claim and NO reclaim claim, and the wording is the
#: contract rather than caution. It counts objects the listing showed under this
#: kind's ``<subpath>/<install id>/`` folder that this install holds no record of --
#: neither an ``uploads`` entry nor a version record. Two different things land in
#: it and nothing here can tell them apart: this install's own archives whose
#: records aged out before :data:`MAX_RECORDED_VERSIONS` gave them the archive's
#: lifetime, and objects some other writer put under a prefix that is co-writable by
#: design. So it is ``objects``, never ``archives``: calling them archives would
#: assert they are ours, and the install id in the key is a string any co-writer can
#: type.
#:
#: Nothing acts on this number. The sweep counts these keys and then skips them
#: exactly as before -- they never enter ``by_key``, never hold a ``keep`` slot, and
#: are never deleted. It is reported because an operator who enables a keep count to
#: bound their bill needs to see the bytes that count will not touch, and because
#: :data:`RETENTION_UNCLAIMED_STATE_KEY` deliberately reads 0 for them: that pair is
#: a floor on the REMEMBERED set and these keys are filtered out before it is taken.
#: Two numbers with two meanings, rather than one number that means neither.
#:
#: Whether any of these could be adopted and reclaimed is a separate design that
#: owes its own argument about proof, and this field is deliberately not a step
#: toward it: a count needs no proof of ownership because it erases nothing.
#:
#: Stamped, and written under the same trusted-listing gate as the pair above, for
#: the same reason: a listing the sweep refused to trust about age cannot be trusted
#: about what it omitted either.
RETENTION_UNRECORDED_STATE_KEY = "retention_unrecorded"


def _clamp_retention_keep(raw: Any) -> int | None:
    """A usable stored count, or ``None`` when there is none.

    ``None`` means keep everything. Absent, a string, a float, a bool -- none of
    those is a count somebody chose, and the only safe reading of a value nobody
    chose is not to delete. There is deliberately no fallback number: a fallback
    here would be a permanent delete performed on a value the operator never wrote.

    Zero and negatives ARE counts somebody wrote, just unusable ones, so they clamp
    UP to :data:`RETENTION_KEEP_MIN` rather than turning retention off -- switching
    it off on a typo would silently stop doing the thing the operator asked for, and
    that direction deletes FEWER archives than the stored number named. There is no
    ceiling to clamp down to: a count larger than the number of archives that exist
    simply keeps all of them, which is not a harm worth refusing an operator over, and
    honouring what they wrote beats reading it as something smaller or as nothing.

    ``bool`` is screened before ``int`` on purpose: ``True`` IS an ``int`` in
    Python, so a state file carrying ``"retention_keep": true`` would otherwise
    resolve to ``keep=1`` -- a plausible-looking number nobody configured, and the
    most destructive one available.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return max(RETENTION_KEEP_MIN, raw)


def _retention_keep_for_sweep(account: str) -> tuple[int | None, str]:
    """The sweep's keep count, or ``None`` and why the sweep is keeping everything.

    Retention is OFF unless this account's state holds a usable count, so ``None``
    is the ordinary answer on an install nobody has configured, not an error. The
    reason comes back with it because the ways of reaching ``None`` need different
    handling downstream: an ABSENT key is the configured behaviour and a successful
    sweep that had nothing to do, while a state file this process could not read --
    or a key that is present and does not resolve to a usable count -- is an anomaly
    worth a warning even though it keeps everything too. Those two are the same
    reason on purpose: in both, somebody's intent is not being honoured, and the
    difference between "unreadable file" and "unreadable value" is not one an
    operator can act on differently.

    Both are fail-closed, the direction :func:`nightly_enabled` picks for the same
    kind of reason. A sweep that does not run costs storage the operator can see and
    reclaim; a sweep run on a value nobody configured erases archives permanently.
    An operator who set ``keep=50`` and meets a transient read failure after the
    upload must not have 47 archives deleted by a number this process guessed.

    Never derived from ``nightly`` or any other grant, in either direction.
    Authorizing unattended uploads is not authorizing permanent deletes, and a
    configured count is not withdrawn by turning the nightly off.
    """
    view, readable = _account_view_checked(account)
    if not readable:
        return None, "the retention setting could not be read"
    if RETENTION_KEEP_STATE_KEY not in view:
        return None, "retention is not enabled"
    keep = _clamp_retention_keep(view.get(RETENTION_KEEP_STATE_KEY))
    if keep is None:
        # The key is THERE and does not resolve. Somebody configured something and it
        # is not being honoured, so this is the anomaly reason rather than the off one:
        # reporting it as "not enabled" would audit a successful sweep and leave an
        # operator believing a count they wrote is in force.
        return None, "the retention setting could not be read"
    return keep, ""


#: Serializes a sweep's FINAL count check with the delete it authorizes, and with
#: :func:`set_retention_keep`. Re-reading the count is not enough on its own: whatever
#: sits between the read and the delete is a window, and an owner's ``keep:null``
#: landing inside it still loses versions they chose to keep.
#:
#: One lock for retention rather than one per account, deliberately. A per-account map
#: would be unbounded state keyed by a caller-supplied string, and the cost of the
#: coarser lock is only that two accounts' sweeps queue at their last step. It is NOT
#: :data:`_run_lock`: that one also serializes :func:`last_runs`, so holding it across
#: a purge would stall a status read for the length of the deletion.
_RETENTION_GATE = threading.Lock()


class _RetentionCountWithdrawn(Exception):
    """The count stopped authorizing the candidate set while the gate was held.

    ``audit_as_failure`` is carried on the exception rather than re-derived from
    ``reason`` at the handler, because a reason the handler does not recognise falls to
    its ``successful`` branch -- and a REFUSED permanent delete audited as a healthy
    sweep that retired nothing is byte-identical in the SEL record to one that had
    nothing to do. A raiser that knows the refusal matters says so here.
    """

    def __init__(self, reason: str, *, audit_as_failure: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.audit_as_failure = audit_as_failure


class _RetentionAuthorizationWithdrawn(Exception):
    """Consent was gone by the time the gate was held, so nothing may be deleted.

    Carries the original refusal, because the audit entry the caller files reports
    WHY authorization failed and a flattened message would lose that.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


def _delete_under_the_retention_gate(
    account: str,
    keep: int,
    profile: str,
    region: str,
    bucket: str,
    versions: list[tuple[str, str]],
    *,
    caller: str,
    recheck_conversations_retained: bool = False,
) -> int:
    """Re-read the count and erase ``versions`` without letting a write interleave.

    The re-read happens with :data:`_RETENTION_GATE` held and the delete runs before
    it is released, so there is no instant at which the count can change between being
    checked and being acted on. :func:`set_retention_keep` takes the same lock, which
    is what makes the two orderings the only ones possible: either the write lands
    first and this read sees it, or the delete completes and the write applies to the
    next sweep.

    A count that GREW protects keys this candidate set was built to delete, so the set
    is stale and this raises. A count that shrank authorizes every key in the set and
    more, so the set stays valid. Unreadable raises, as the first read does.
    """
    # BOTH locks, because either alone leaves a real hole. `_RETENTION_GATE` orders
    # other THREADS in this process; the state file's sidecar lock orders other
    # PROCESSES, and the backup engine's own docstrings describe a second install writing
    # into the first one's bucket and state as a designed-for case -- so a clear issued
    # over there would otherwise not be ordered against this delete at all. A thread
    # lock cannot see another process, and a file lock alone would not serialize two
    # threads here, since each would open its own descriptor.
    #
    # The cost is deliberate: a state write in ANY process waits for this purge. That
    # is the price of ordering an irreversible remote delete against a local
    # withdrawal, and what waits is one batched delete rather than the whole sweep.
    lock_path = _state_path().with_suffix(".lock")
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    with (
        _RETENTION_GATE,
        open_lock_file(lock_path) as fd,
        file_lock(fd, exclusive=True, required=True),
    ):
        keep_now, off_now = _retention_keep_for_sweep(account)
        if keep_now is None or keep_now > keep:
            raise _RetentionCountWithdrawn(off_now or "the retention count changed before deletion")
        # Re-read the conversations fact HERE, for exactly the reason the count is
        # re-read here: the candidate set was built outside this lock, and a fact that
        # changed in between makes the set stale. Two same-account sessions runs can
        # overlap -- the owner-triggered path does not pass the upload gate, so it is not
        # serialized against a nightly run in flight -- so a wide run in ANOTHER process
        # can land its archive, and its fact, after this sweep chose its candidates.
        #
        # This is the existing lock used correctly, not a new protocol: the file half
        # orders other PROCESSES and `_RETENTION_GATE` orders other threads, which is the
        # pair this block already holds, and the read below takes no lock of its own
        # (`_read_state_checked` reads the file directly, and `_unpersisted_lock` is a
        # documented leaf), so nothing nests and the engine's one acquisition order is
        # unchanged.
        #
        # Refusing costs a kept archive until the next sweep. Proceeding costs the only
        # copy of somebody's conversations, permanently.
        if recheck_conversations_retained and a_retained_archive_carries_conversations(account):
            raise _RetentionCountWithdrawn(
                "an archive holding conversations this run does not was recorded before deletion",
                # A REFUSED permanent delete. Without this the handler's unmatched-reason
                # branch would audit it `successful` with an empty error, identical to a
                # sweep that found nothing to retire -- while the caller-side decline for
                # the very same condition records `failed` plus the reason. The suppression
                # stops the DELETE, never the audit.
                audit_as_failure=True,
            )
        # Consent is re-checked HERE, inside both locks, for the same reason the count
        # is: acquiring these locks can wait on another purge, and a gate is good for
        # the call that follows it rather than for one on the far side of a wait. This
        # is the LAST thing before the irreversible call.
        #
        # It costs an STS round trip inside the critical section, which is the trade
        # already taken for the delete: a longer wait for other state writers buys a
        # delete that cannot run on authority withdrawn while this waited.
        try:
            _authorize_upload(
                account,
                profile,
                region,
                caller=caller,
                # A retention sweep DELETES archives; it uploads no kind's payload,
                # so no per-kind unattended grant governs it. The account, app and
                # consent checks above it still do.
                payload_kind=None,
                operation=SEL_OP_RETENTION,
            )
        except Exception as exc:
            raise _RetentionAuthorizationWithdrawn(exc) from exc
        return storage.delete_object_versions(
            profile, region, bucket, "backup", versions, account=account
        )


def _newest_first(keys: dict[str, list[dict[str, Any]]]) -> list[str]:
    """``keys`` ordered newest archive first, by the same rule the panel sorts by.

    One ordering for the listing and the sweep, because two would eventually
    disagree and the operator would then be shown a row that retention had
    already decided was old. :func:`_archive_sort_key` leads on S3's own
    ``LastModified`` and tie-breaks on the basename, so a key put here by some
    other tool -- carrying no ``_stamp`` and therefore no time in its name --
    still sorts by when the bucket says it arrived.
    """

    def _entry(key: str) -> dict[str, Any]:
        newest = max((str(v.get("modified", "")) for v in keys[key]), default="")
        return {"key": key, "modified": newest}

    return sorted(keys, key=lambda k: _archive_sort_key(_entry(k)), reverse=True)


def _current_version_is_ours(rows: list[dict[str, Any]], recorded: str) -> bool:
    """Whether the version a restore would fetch FIRST under this key is ``recorded``.

    `storage.get_file` names no version unless it is given one, so a restore starts
    at the key's CURRENT version. That makes "is this a restorable archive of ours"
    a question about one version, and the answer decides both whether the key may
    hold a ``keep`` slot and whether the sweep may run at all.

    False for a key whose current version is a delete marker, and false for one
    whose current version is a co-writer's.

    In that second case our bytes are still on the drive as a noncurrent version,
    and a restore CAN reach them: when the current object fails the body fingerprint
    and a provable version was recorded for the key,
    :func:`_recover_recorded_version` reads exactly that version. Such a key is
    therefore present and reachable, not present and stranded.

    This function is deliberately about the CURRENT version, and retention's
    behaviour follows from that alone. Declining such a key is a CONSERVATIVE
    reading rather than a forced one: the key may in fact be recoverable, and it
    still holds no ``keep`` slot. Declining is the safe direction -- it retains more,
    never less -- and teaching retention to count a recoverable-but-noncurrent copy
    is a separate decision about what may be DELETED, which is not taken here.

    Empty ``recorded`` is false as well: with no recorded id nothing can be shown to
    be ours, which is the fail-closed end. Note this is the same input that makes
    recovery unavailable, so the two agree rather than merely coinciding.

    It is a question about the CURRENT version rather than the newest-by-timestamp
    one, so a key ordered by `_newest_first` also carries our version as its newest
    -- one rule, not two that can drift.
    """
    if not recorded:
        return False
    current = [row for row in rows if row.get("latest")]
    if not current:
        return False
    row = current[0]
    if row.get("deleteMarker"):
        return False
    return str(row.get("versionId", "")) == recorded


def _audit_retention(
    account: str,
    outcome: dict[str, Any],
    *,
    caller: str,
    result: str,
    error: str = "",
) -> None:
    """Record in the SEL what the sweep DID, not only what it refused.

    :func:`_refuse_upload` covers the gate, so a REFUSED sweep was already on the
    record and a sweep that RAN was not. That asymmetry is the one an auditor
    cannot work around, because this is the only path in the app that erases
    object versions permanently: a purge leaving no event is indistinguishable
    from no purge at all, and so is a purge that failed halfway. Each terminal
    outcome therefore files one :data:`SEL_OP_RETENTION` event carrying the
    counts that say which of them happened.

    ``result`` is ``successful`` when the sweep completed, whether or not it had
    anything to delete, and ``failed`` when it did not -- an AWS error, or the
    refusal to act on a listing that does not show the archive just uploaded.
    Neither is ``denied``: that value belongs to the access decisions
    :func:`_refuse_upload` files, and putting a cloud error in the same bucket as
    a withdrawn consent would devalue every real denial in the log.

    Best-effort and last, for the same reason the sweep itself is: the archive is
    already off-host, so a SEL write that fails must not reach the caller.
    """
    try:
        sel().log_api_access(
            caller=caller,
            operation=SEL_OP_RETENTION,
            outcome=result,
            source="aws-control",
            resources=(
                f"account={account} kind={outcome['kind']} keep={outcome['keep']} "
                f"live={outcome['live']} retired={outcome['retired']} "
                f"versions={outcome['versions']} unclaimed={outcome['unclaimed']} "
                f"unclaimedBytes={outcome['unclaimedBytes']} "
                # LAST on purpose. The field is capped, and every value before this
                # one is load-bearing for an auditor reading what the sweep did; a new
                # pair appended here can only ever cost itself to the cap, never
                # displace the count that says whether archives were erased.
                f"unrecorded={outcome['unrecorded']} "
                f"unrecordedBytes={outcome['unrecordedBytes']}"
            )[:200],
            error=error[:200],
        )
    except Exception:
        logger.debug("aws-control SEL audit failed", exc_info=True)


def _audit_unfiled_authorization(
    account: str,
    outcome: dict[str, Any],
    exc: BaseException,
    *,
    caller: str,
) -> None:
    """File a retention event for an authorization failure nobody else filed.

    :func:`_refuse_upload` files its own ``denied`` event and THEN raises, so a
    refusal is already on the record and filing again here would record one
    decision twice. Every other way :func:`_authorize_upload` can fail -- an
    :class:`AWSError` out of the live ``sts:GetCallerIdentity``, an expired or
    missing credential, a transport error -- never reaches ``_refuse_upload``,
    and leave the gate on the permanent-delete path with no event at all. A
    credential failure is ordinary, so that is the common case this covers.

    The exception TYPE is the discriminator because it is the one thing
    ``_refuse_upload`` guarantees: it ends in ``raise RuntimeError(reason)``.
    ``AWSError`` derives from ``Exception``, not ``RuntimeError``, and a test
    pins that relationship -- if it ever changed, this would silently go back to
    treating a real credential failure as already audited.

    ``failed`` rather than ``denied``, per :func:`_audit_retention`: a cloud error
    is not an access decision, and filing it as a denial would devalue the real
    ones.
    """
    if isinstance(exc, RuntimeError):
        return
    _audit_retention(
        account,
        outcome,
        caller=caller,
        result="failed",
        error=redact_log_via_context(str(exc)),
    )


def _record_unclaimed(account: str, kind: str, outcome: dict[str, Any]) -> None:
    """Persist the sweep's unclaimed counts so the status read can serve them.

    Best-effort and never raising, for the reason the sweep itself is best-effort:
    the archive is already off-host and the run is already recorded, so nothing
    this write can fail at is worth converting a successful backup into a failed
    one. :data:`SEL_OP_RETENTION` carries the same two numbers either way, so a
    lost write costs the status copy and not the record -- which is why it logs at
    debug, matching :func:`_audit_retention`.

    The stamp is taken here rather than read back from the record, so the value
    names when the LISTING was measured rather than when some later reader looked.
    """
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")

    def mutate(state: dict[str, Any]) -> None:
        entry = _account_state(state, account)
        measured = entry.setdefault(RETENTION_UNCLAIMED_STATE_KEY, {})
        if not isinstance(measured, dict):
            # Repaired rather than crashed, exactly as `_record_run_locked` repairs a
            # corrupted `runs`: this runs after an upload that already succeeded.
            measured = entry[RETENTION_UNCLAIMED_STATE_KEY] = {}
        measured[kind] = {
            "archives": int(outcome["unclaimed"]),
            "bytes": int(outcome["unclaimedBytes"]),
            "at": stamp,
        }

    try:
        _locked_state_update(mutate)
    except Exception:
        logger.debug(
            "aws-control: recording the unclaimed archive count for %s failed",
            account,
            exc_info=True,
        )


def _record_unrecorded(account: str, kind: str, outcome: dict[str, Any]) -> None:
    """Persist the sweep's count of listed-but-unrecorded objects for the status read.

    A sibling of :func:`_record_unclaimed` in every respect except what it counts, and
    separate from it for exactly that reason: one number is a floor on the archives
    this install REMEMBERS and the other is what the listing held that it has no
    record of. Merging them would produce a single figure that is neither, and the
    first is load-bearing -- an operator reads it against the ``keep`` count to see
    what retention will collect.

    Best-effort and never raising, like its sibling: the archive is already off-host
    and the run already recorded, so nothing this write can fail at is worth turning a
    successful backup into a failed one. :func:`_audit_retention` carries the same pair
    regardless, which is why this logs at debug.

    See :data:`RETENTION_UNRECORDED_STATE_KEY` for why the field claims no ownership.
    """
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")

    def mutate(state: dict[str, Any]) -> None:
        entry = _account_state(state, account)
        measured = entry.setdefault(RETENTION_UNRECORDED_STATE_KEY, {})
        if not isinstance(measured, dict):
            # Repaired rather than crashed, for the reason `_record_unclaimed` repairs
            # its own level: this runs after an upload that already succeeded.
            measured = entry[RETENTION_UNRECORDED_STATE_KEY] = {}
        measured[kind] = {
            "objects": int(outcome["unrecorded"]),
            "bytes": int(outcome["unrecordedBytes"]),
            "at": stamp,
        }

    try:
        _locked_state_update(mutate)
    except Exception:
        logger.debug(
            "aws-control: recording the unrecorded object count for %s failed",
            account,
            exc_info=True,
        )


def _prune_recorded_versions(
    account: str,
    kind: str,
    install_id: str,
    listed_keys: set[str],
    *,
    eligible: set[str],
) -> None:
    """Drop version records the listing proves name objects that are gone.

    This is what makes :data:`MAX_RECORDED_VERSIONS` a backstop rather than a horizon.
    A record lives as long as its archive does and this is the only thing that ends it,
    so no count chosen to bound a panel decides what retention is able to retire.

    It deletes STATE, never an object, so the failure directions are not symmetric. A
    record wrongly kept costs a little document space and nothing else -- the archive
    still has to pass :func:`_current_version_is_ours` before anything touches it. A
    record wrongly dropped returns its archive to the unreclaimable floor, which costs
    bytes but destroys nothing. Neither direction can erase data, and the prune is
    written to prefer keeping.

    Three bounds make the absence a PROOF rather than a guess:

    * The caller runs this only past the gate that accepted the listing as showing the
      archive this run just uploaded. ``storage.list_object_versions`` walks the whole
      token chain and RAISES rather than returning a partial answer, so a listing that
      got here is complete for its prefix. A listing that raised, or that the gate
      refused, never reaches this function and prunes nothing.
    * Only records under ``<kind subpath>/<install id>/`` are eligible. The listing saw
      exactly that folder, so it is evidence about nothing else: a snapshot sweep must
      not prune a sessions record, and no sweep may prune another install's.
    * Only records in ``eligible`` -- the ownership set read BEFORE the listing began --
      are eligible. A push that lands while the listing is in flight legitimately names
      an object the listing does not show, and a manual run racing the nightly loop is
      a documented case rather than a hypothetical one.

    The in-process records of pushes whose state write failed are untouched: this
    writes through :func:`_locked_state_update`, which mutates only the persisted
    document, and :func:`_merge_pending` carries those records back in afterwards.
    Their archives are in the bucket, so the listing shows them anyway.

    That holds only because a held record is RELEASED once the document carries it.
    :func:`_release_persisted_versions` is what makes it true: without it a held version
    outliving its fingerprint would be carried back after this prune deleted it, on
    every later update, and this function's deletion would be temporary rather than a
    decision.

    Best-effort and never raising, like the two recorders beside it.
    """
    prefix = f"{KIND_SUBPATHS[kind]}{KEY_SEP}{install_id}{KEY_SEP}"

    def _gone(key: str) -> bool:
        return key.startswith(prefix) and key in eligible and key not in listed_keys

    def mutate(state: dict[str, Any]) -> None:
        entry = _account_state(state, account)
        recorded = entry.get("upload_versions")
        if not isinstance(recorded, dict):
            # Nothing to prune, and nothing to repair either: a corrupted level is
            # rebuilt by `_merge_uploads` on the next push, which is where that
            # decision already lives. Publishing an empty map from here would throw
            # away every version record on the strength of one bad read.
            return
        for key in [key for key in recorded if isinstance(key, str) and _gone(key)]:
            recorded.pop(key, None)

    try:
        _locked_state_update(mutate)
    except Exception:
        logger.debug(
            "aws-control: pruning stale version records for %s failed",
            account,
            exc_info=True,
        )


def _prune_remote_archives(
    account: str,
    profile: str,
    region: str,
    bucket: str,
    kind: str,
    install_id: str,
    newest_key: str,
    *,
    caller: str,
    recheck_conversations_retained: bool = False,
) -> dict[str, Any]:
    """Retire this install's oldest archives of ``kind``, keeping the newest ``keep``.

    Without this the drive only ever grows. Both push paths mint a key carrying
    :func:`_stamp`, so nothing is ever overwritten and a nightly backup adds one
    archive a night forever -- measured on a real drive: 15 snapshot archives,
    10.1 GB, the newest 2.49 GB, oldest three weeks old, on a bucket whose size
    had never once gone down.

    **Deleting the OBJECT would not have helped.** The drive has versioning
    enabled and no lifecycle rule, so ``delete-object`` without a version id
    writes a delete marker and leaves the bytes as a noncurrent version that goes
    on being billed -- a retention pass built that way would empty the listing
    and save nothing. So the sweep is version-aware end to end: it lists versions
    (:func:`storage.list_object_versions`) and deletes them pinned to their
    ``VersionId`` (:func:`storage.delete_object_versions`), which erases the bytes
    and leaves no marker behind.

    **BEST-EFFORT AND LAST.** The upload is the point of the run; retention is
    housekeeping after it. Every failure below is caught and logged as one line,
    because a backup whose archive is safely off-host must never be reported as
    failed over a cleanup that was not -- the only cost of a skipped sweep is a
    bill, and the next successful run collects it.

    **AUDITED EITHER WAY.** Being best-effort is why the SEL entry matters: a
    failure that only logs is a failure nobody reviewing the audit trail can see,
    and this is the one path in the app that erases object versions for good. So
    every terminal outcome files one event through :func:`_audit_retention` --
    including the ones that deleted nothing -- while a refusal by the gate is left
    to :func:`_refuse_upload`, which already recorded it where the decision was
    made.

    Three scoping rules, each made load-bearing by the shape of this bucket:

    * **Per install.** One drive is reachable by several installs BY DESIGN (see
      "install identity"), so the listing and every delete are anchored on THIS
      install's prefix. Another machine's archives are not ours to retire and its
      retention setting is not ours to apply.
    * **Per kind.** ``snapshots/`` and ``sessions/`` are separate histories with
      separate cadences. Counted together, a burst of one kind would evict the
      other kind's only copy.
    * **Never the newest, whatever the count.** The key this run just uploaded is
      dropped from the candidates unconditionally, by name, and that is a SEPARATE
      guarantee from the count rather than a consequence of it. The floor at
      :data:`RETENTION_KEEP_MIN` also happens to spare the first entry of the age
      order, but the two are not the same claim: the run's own key is only first in
      that order while nothing else carries a later timestamp, and a co-writer with
      a skewed clock or a future-dated object is enough to move it. Tying the
      guarantee to the number would make it hold by luck exactly when the ordering
      surprises us, which is the case it exists for.

    And one refusal, which is the cloud form of the guard ``--keep`` already
    carries locally: a bundle that omits data it was asked to carry does not
    prune, so an incomplete backup cannot replace a complete one. Here, if the
    listing does not show the archive this run just uploaded, NOTHING is pruned. A
    view missing the newest object is a view that cannot be trusted about which
    objects are old, and acting on one is how a retention pass deletes the history
    and keeps nothing.

    Returns a record of what it did, for the log line and for tests. Callers
    ignore it: there is no outcome here that should change the run's own result.
    """
    keep, off_reason = _retention_keep_for_sweep(account)
    outcome: dict[str, Any] = {
        "kind": kind,
        # "off" rather than a number when nothing is configured or the state could
        # not be read. Recording a number here would name a count this sweep did not
        # act on, and the absence of one is the whole point.
        "keep": "off" if keep is None else keep,
        "live": 0,
        "retired": 0,
        "versions": 0,
        # Archives this install wrote and can never retire. Zero until the listing
        # is read, and reported even when the sweep deletes nothing, because their
        # whole problem is that no sweep ever collects them.
        "unclaimed": 0,
        "unclaimedBytes": 0,
        # Objects the listing showed under this kind's install folder that this
        # install holds NO record of. A different question from `unclaimed`, which is
        # a floor on the remembered set: these keys are filtered out before that
        # measurement, so they would otherwise be counted nowhere at all. No
        # ownership is asserted and nothing is ever done with them -- see
        # :data:`RETENTION_UNRECORDED_STATE_KEY`.
        "unrecorded": 0,
        "unrecordedBytes": 0,
        "skipped": "",
    }
    # First, and before any cloud call, because neither branch needs one to decline.
    #
    # Retention is off unless this account holds a usable count, so an install nobody
    # configured -- fresh or upgraded -- reaches here and keeps every archive. That
    # is the whole opt-in: no first run after an upgrade deletes anything.
    #
    # The two reasons are audited differently on purpose. Not enabled is the
    # configured behaviour, so the sweep SUCCEEDED at having nothing to do, and
    # filing it as a failure would put the ordinary state of every unconfigured
    # install in the same bucket as a real fault. A state file this process could not
    # read keeps everything too, but it is an anomaly: an operator who configured a
    # count is silently not getting it, so it stays a failure with its reason.
    if keep is None:
        outcome["skipped"] = off_reason
        if off_reason == "retention is not enabled":
            logger.debug(
                "aws-control: %s retention for %s is not enabled, so every archive is "
                "kept; set %s on this account to bound the pile",
                kind,
                account,
                RETENTION_KEEP_STATE_KEY,
            )
            _audit_retention(account, outcome, caller=caller, result="successful")
            return outcome
        logger.warning(
            "aws-control: skipping %s retention for %s: the app's state file could not "
            "be read, so a configured keep count cannot be seen and guessing one could "
            "erase archives the owner asked to keep; the backup itself succeeded and "
            "nothing was deleted",
            kind,
            account,
        )
        _audit_retention(account, outcome, caller=caller, result="failed", error=outcome["skipped"])
        return outcome
    # Re-authorized, like the archive PUT itself. These are DELETES of the
    # owner's data running after a build that may have taken minutes, and
    # consent can be withdrawn, the app disabled or the profile repointed in
    # between -- so the live gate runs again rather than the sweep inheriting a
    # decision made before the push. Its own operation name keeps a refused sweep
    # out of the upload's audit bucket: this run's upload succeeded.
    #
    # It gets its OWN handler so one refusal files one event: `_refuse_upload`
    # records the decision as `denied` at the point it is made, and letting that
    # RuntimeError fall into the sweep's broad handler below would file a second
    # `failed` event for the same decision. A failure that never reaches
    # `_refuse_upload` -- an expired credential, a dead STS call -- files nothing
    # by itself, so `_audit_unfiled_authorization` covers exactly that half.
    try:
        _authorize_upload(
            account,
            profile,
            region,
            caller=caller,
            # A retention sweep DELETES archives; it uploads no kind's payload,
            # so no per-kind unattended grant governs it. The account, app and
            # consent checks above it still do.
            payload_kind=None,
            operation=SEL_OP_RETENTION,
        )
    except Exception as exc:
        outcome["skipped"] = "authorization refused"
        # `redact_log_via_context`, not the two bare egress redactors the engine
        # uses for labels and object names: this is a gate-side log line, so a host with
        # a companion policy loaded must not have it scanned with the weaker OSS
        # pass. It never raises, and with no context installed it runs that same
        # OSS pass, so the spelling costs nothing where nothing composes.
        logger.warning(
            "aws-control: %s retention for %s was not authorized; the backup itself "
            "succeeded and nothing was deleted: %s",
            kind,
            account,
            redact_log_via_context(str(exc)),
        )
        _audit_unfiled_authorization(account, outcome, exc, caller=caller)
        return outcome
    try:
        sub = f"{KIND_SUBPATHS[kind]}/{install_id}"
        # Read BEFORE the listing, and used for ONE thing: bounding the version-record
        # prune below. A push that lands while the listing is in flight -- a manual run
        # racing the nightly loop, which the backup engine already treats as a real case --
        # legitimately names an object the listing cannot show, so its record must not
        # be eligible for a prune that reads absence as proof. Anything recorded from
        # here on is invisible to this set and therefore safe by construction.
        owned_before = retention_owned_keys(account)
        rows = storage.list_object_versions(profile, region, bucket, "backup", sub, account=account)
        # What this install can PROVE it wrote, not whatever sits under a prefix.
        # The prefix is shared by design, so a co-writer -- another tool pointed at
        # the same drive, or a compromised one -- can put an object under it, and
        # erasing versions cannot be undone. The restore path already draws exactly
        # this line for a far cheaper operation: anything outside this record reads
        # as ORIGIN_UNVERIFIED and is refused without an explicit override. A
        # permanent delete must be at least as strict as a read.
        #
        # `_record_run` writes this run's own key before the sweep is called, and
        # `uploaded_objects` also merges runs whose state write failed, so
        # `newest_key` is in here on both paths.
        ours = retention_owned_keys(account)
        our_versions = uploaded_versions(account)
        listed_keys: set[str] = set()
        unrecorded: set[str] = set()
        unrecorded_bytes = 0
        by_key: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            key = str(row.get("key", ""))
            # The label sidecar shares the prefix with the archives it labels. It
            # is not an archive: it must not consume a `keep` slot, and it must
            # not be deleted -- another install reads it to render a name instead
            # of hex. It is also not an unaccounted object, so it is excluded from
            # the count below as well: it is there on purpose and this app put it
            # there, so reporting it as something nothing has a record of would be
            # a permanent phantom in every operator's floor.
            if not key or _key_basename(key) == LABEL_OBJECT_NAME:
                continue
            # `_prune_recorded_versions` compares against this set, so it is built
            # from the raw listing rather than from `by_key`: a record whose key was
            # filtered out below is a record whose object EXISTS, and pruning it
            # would throw away the only proof that makes that archive retireable.
            #
            # A delete-marker row adds its key here even though the count below
            # skips it, and the asymmetry is the point: this set decides whether a
            # RECORD survives, where the two directions are not equally costly. A
            # record wrongly kept costs a little document space; a record wrongly
            # dropped is unrecoverable proof. A marker means the key was written
            # under, so treating it as absent is the expensive direction.
            listed_keys.add(key)
            # Not ours to retire, and it must not consume a `keep` slot either:
            # `keep` counts what this install keeps of its OWN archives, so letting
            # a foreign object fill a slot would let a co-writer's upload push one
            # of ours over the edge and delete it.
            #
            # Counted on the way past, and only counted. Which of the two things it
            # is -- one of our own archives whose record aged out, or another
            # writer's object under a co-writable prefix -- is not knowable from
            # here, which is exactly why the number asserts neither and nothing acts
            # on it. Bytes are summed over every version under the key, like
            # `unclaimedBytes`, because every version is billed.
            #
            # A delete marker is not an object and carries no bytes, so it cannot be
            # what makes a key count -- the same reading `_current_version_is_ours`
            # already applies. A key whose rows under this folder are ALL markers
            # holds nothing and is billed nothing, and counting it would put a
            # phantom in the floor that no later listing can ever remove. A key that
            # also has a real version still counts, on that version's row, because
            # those bytes exist and are billed whatever sits on top of them.
            if key not in ours:
                if not row.get("deleteMarker"):
                    unrecorded.add(key)
                    unrecorded_bytes += int(row.get("size", 0) or 0)
                continue
            by_key.setdefault(key, []).append(row)
        outcome["unrecorded"] = len(unrecorded)
        outcome["unrecordedBytes"] = unrecorded_bytes
        # A key counts as a live archive of OURS only when the version a restore
        # fetches FIRST is the version this install wrote. `storage.get_file` reads
        # whatever is CURRENT under the key unless it is handed a version id, so if a
        # co-writer's version is on top the key is not treated as a restorable copy
        # and must not hold a `keep` slot.
        #
        # Our bytes under such a key are not unreachable any more --
        # `_recover_recorded_version` reads the recorded version when the current
        # object fails the fingerprint. This sweep still does not count the key, which
        # is now the conservative reading rather than the only one: not counting it
        # retains more, and counting it would let retention delete something else.
        #
        # Two ways a key fails that, both left entirely alone. Its current version is
        # a delete marker: the noncurrent bytes are the separate, pre-existing cost
        # of the manual delete path, and erasing them here would silently revoke the
        # recoverability `storage.delete_key` documents. Or its current version is
        # foreign: erasing our unreachable version under it would reclaim bytes, but
        # it would also be this sweep deciding that a key a co-writer is actively
        # writing is finished with, which is not a call retention gets to make.
        live = {
            key: versions
            for key, versions in by_key.items()
            if _current_version_is_ours(versions, our_versions.get(key, ""))
        }
        outcome["live"] = len(live)
        # The keys this install wrote that carry no recorded version: an archive
        # pushed before the record existed, an unversioned bucket, or a put response
        # that named none. `_current_version_is_ours` is false for all of them, so
        # they hold no `keep` slot and no sweep can ever delete them -- their bytes
        # are a permanent cost. Every version under such a key is billed, so the
        # total is over the whole key rather than its current version. Reported so
        # the cost appears in the audit trail rather than only on an invoice.
        unclaimed = [key for key in by_key if not our_versions.get(key, "")]
        outcome["unclaimed"] = len(unclaimed)
        outcome["unclaimedBytes"] = sum(
            int(row.get("size", 0) or 0) for key in unclaimed for row in by_key[key]
        )
        if unclaimed:
            logger.info(
                "aws-control: %s retention for %s under install %s cannot own %d archive(s) "
                "holding %d byte(s): no version id was recorded for them, so no sweep will "
                "ever reclaim them",
                kind,
                account,
                install_id,
                outcome["unclaimed"],
                outcome["unclaimedBytes"],
            )
        if unrecorded:
            logger.info(
                "aws-control: %s retention for %s found %d object(s) holding %d byte(s) under "
                "install %s that this install has no record of; they are counted and left "
                "alone -- nothing here says they are ours and no sweep will touch them",
                kind,
                account,
                outcome["unrecorded"],
                outcome["unrecordedBytes"],
                install_id,
            )
        if newest_key not in live:
            # Two different faults, and an auditor needs to tell them apart: a
            # listing that omits the upload cannot be trusted about age at all,
            # while one that shows the key under a foreign current version says the
            # archive this run just wrote is already not the restorable copy.
            if newest_key in by_key:
                outcome["skipped"] = (
                    "the archive this run uploaded is not the current version of its key"
                )
            else:
                outcome["skipped"] = "the listing does not show the archive this run uploaded"
            logger.warning(
                "aws-control: skipping %s retention for %s under install %s: %s, so the "
                "listing cannot be trusted about which archives are old; nothing was deleted",
                kind,
                account,
                install_id,
                outcome["skipped"],
            )
            _audit_retention(
                account, outcome, caller=caller, result="failed", error=outcome["skipped"]
            )
            return outcome
        # Past the gate above, so the listing showed the archive this run uploaded as
        # the current version of its key -- which is the only point in this function
        # where the unclaimed measurement is worth persisting. Before it, a listing
        # the sweep itself refused to trust about age cannot be trusted about how many
        # keys it omitted either, and an UNDERCOUNT published as the floor is the one
        # shape an operator must not be handed: it reads as "nothing unreclaimable
        # here". The audit event still carries the number on that path, where its
        # `failed` result says how much to trust it.
        #
        # One call covers every path from here down. Deletion only ever touches
        # versions drawn from `candidates`, which come from `live`, and an unclaimed
        # key is absent from `live` by construction -- `_current_version_is_ours` is
        # false without a recorded id. So the set measured above survives a kept-all
        # return, a completed purge, a withdrawn consent and a half-finished delete
        # alike, and re-recording it after any of them would write the same numbers.
        _record_unclaimed(account, kind, outcome)
        # Same gate, same reason: a listing the sweep declined to trust about age
        # cannot be trusted about what it omitted, and an UNDERCOUNT served as a floor
        # reads as "nothing unaccounted here".
        _record_unrecorded(account, kind, outcome)
        # And the same gate is what makes the prune safe at all. It needs PROOF that
        # an object is gone, and only a complete listing this sweep was willing to act
        # on is that: `storage.list_object_versions` walks the whole token chain and
        # raises rather than returning a first page, so past the gate an absent key is
        # an absent object rather than an unread one. A listing that raised never
        # reaches here, and neither does one the gate refused.
        _prune_recorded_versions(account, kind, install_id, listed_keys, eligible=owned_before)
        by_age = _newest_first(live)
        candidates = [key for key in by_age[keep:] if key != newest_key]
        # `ours` proved the KEY. This proves the VERSION, which is what the delete
        # below actually erases: a key can carry a version this install did not
        # write, and the recorded id is the only thing that says which one is ours.
        #
        # One way a candidate drops out here, and it is left entirely alone:
        # recorded but absent from the listing, meaning our version is already gone,
        # so whatever remains under that key is not ours to erase -- exactly the
        # case a version COUNT read as ownership and got wrong. The membership test
        # beside it re-asserts an invariant rather than filtering a second case:
        # every candidate came from `live`, and `_current_version_is_ours` is false
        # without a recorded id, so a key with none is counted as unclaimed above
        # and never reaches this point.
        listed = {key: {str(row.get("versionId", "")) for row in by_key[key]} for key in candidates}
        versions = [
            (key, our_versions[key])
            for key in candidates
            if key in our_versions and our_versions[key] in listed[key]
        ]
        retire = [key for key, _ in versions]
        if not retire:
            logger.info(
                "aws-control: %s retention for %s kept all %d archive(s) under install %s "
                "(keep=%d)",
                kind,
                account,
                len(live),
                install_id,
                keep,
            )
            _audit_retention(account, outcome, caller=caller, result="successful")
            return outcome
        # The SECOND gate, immediately before the only irreversible call in this
        # function. The one at the top is separated from here by a
        # `list_object_versions` round trip, and consent withdrawn inside that
        # window would otherwise permanently erase versions nobody is authorized
        # to touch any more. `_publish_label` holds the same rule for its own
        # write: a gate is good for the call that FOLLOWS it, not for a later one.
        #
        # Its own handler, for the reason the first gate has one, and it returns
        # instead of falling through: nothing has been deleted at this point, so
        # this is a refusal and not a half-finished purge.
        # The COUNT gets the same treatment as the consent gate below, and for the
        # same reason: it was read once before a `list_object_versions` round trip
        # that takes as long as the network takes, and an owner who switched
        # retention off inside that window has chosen to keep these versions. A
        # count read before the listing is good for the listing, not for a delete
        # that happens after it.
        #
        # Re-reading here rather than holding `_run_lock` across the deletion is
        # deliberate: that lock also serializes `last_runs`, so holding it through
        # network calls would stall a status read for the length of a purge.
        #
        try:
            removed = _delete_under_the_retention_gate(
                account,
                keep,
                profile,
                region,
                bucket,
                versions,
                caller=caller,
                recheck_conversations_retained=recheck_conversations_retained,
            )
        except _RetentionAuthorizationWithdrawn as withdrawn:
            cause = withdrawn.cause
            outcome["skipped"] = "authorization withdrawn before deletion"
            logger.warning(
                "aws-control: %s retention for %s was authorized before the listing but "
                "no longer at the moment of deletion; the backup itself succeeded and "
                "nothing was deleted: %s",
                kind,
                account,
                redact_log_via_context(str(cause)),
            )
            _audit_unfiled_authorization(account, outcome, cause, caller=caller)
            return outcome
        except _RetentionCountWithdrawn as exc:
            # Nothing has been deleted: the gate checked the count and refused before
            # calling S3, so this is a refusal and not a half-finished purge.
            outcome["skipped"] = exc.reason
            logger.warning(
                "aws-control: %s retention for %s was set to keep %s when the listing "
                "began and no longer authorized that set at the moment of deletion; the "
                "backup itself succeeded and nothing was deleted: %s",
                kind,
                account,
                keep,
                exc.reason,
            )
            # An owner who changed their mind is not a failure. An unreadable setting
            # is, which is the same split the first read files.
            if exc.audit_as_failure or exc.reason == "the retention setting could not be read":
                _audit_retention(account, outcome, caller=caller, result="failed", error=exc.reason)
            else:
                _audit_retention(account, outcome, caller=caller, result="successful")
            return outcome
        except storage.PartialVersionDelete as exc:
            # Those bytes are already gone, so the count has to survive into the
            # audit the broad handler below files -- otherwise a purge that failed
            # halfway is recorded as having erased nothing.
            #
            # `retired` stays at 0 on purpose: batches are filled to the API's
            # limit without regard to key boundaries, so the erased versions do
            # not map to a number of retired KEYS, and a figure nothing measured
            # is worse in an audit record than an absent one.
            outcome["versions"] = exc.removed
            raise
        outcome["retired"] = len(retire)
        outcome["versions"] = removed
        logger.info(
            "aws-control: %s retention for %s kept %d of %d archive(s) under install %s "
            "(keep=%d), erasing %d object version(s)",
            kind,
            account,
            len(live) - len(retire),
            len(live),
            install_id,
            keep,
            removed,
        )
        _audit_retention(account, outcome, caller=caller, result="successful")
        return outcome
    except Exception as exc:
        # Deliberately broad, and it is the whole point of this function's
        # contract: the archive is already off-host and the run has already been
        # recorded, so nothing this sweep can fail at is worth converting a
        # successful backup into a failed one. One line, with the reason, and the
        # next run tries again.
        outcome["skipped"] = "cleanup failed"
        # Gate-side, so the same context-aware log spelling as the refusal above.
        # One redaction feeds both the log line and the audit event's `error`: a
        # composed companion's regexes apply to both, and in the one state that
        # withholds the text the audit entry still fires carrying the placeholder,
        # which is the shape an auditor can act on.
        reason = redact_log_via_context(str(exc))
        # Two spellings, because one of them would be false. A failure AFTER the
        # listing may already have erased versions permanently, and the audit entry
        # below carries that count deliberately -- so a log line next to it claiming
        # nothing was deleted contradicts the record an auditor reads beside it, and
        # points them at a purge that did not happen. Only the version count is named:
        # batches are filled to the API's limit without regard to key boundaries, so
        # `retired` stays 0 on that path and a number of KEYS is not something this
        # failure measured.
        if outcome["versions"]:
            logger.warning(
                "aws-control: %s retention for %s failed after erasing %d object "
                "version(s); those bytes are gone and cannot be recovered, and the "
                "backup itself succeeded: %s",
                kind,
                account,
                outcome["versions"],
                reason,
            )
        else:
            logger.warning(
                "aws-control: %s retention for %s could not run; the backup itself "
                "succeeded and nothing was deleted: %s",
                kind,
                account,
                reason,
            )
        # A failure AFTER the listing may already have erased some versions, so
        # `outcome` carries whatever the sweep got through -- an entry saying
        # nothing happened would be the one shape an auditor must not be handed.
        _audit_retention(account, outcome, caller=caller, result="failed", error=reason)
        return outcome


def set_retention_keep(account: str, count: int | None) -> None:
    """Write this account's retention count, or clear it to turn retention off.

    ``None`` REMOVES the key rather than storing a sentinel, so "off" has exactly one
    representation: the state an install has before anyone configures anything. A
    second spelling of off would be a second thing
    :func:`_retention_keep_for_sweep` has to agree about.

    Rejects a ``bool`` rather than coercing it, which is the same screen
    :func:`_clamp_retention_keep` applies on the way out. ``True`` IS an ``int`` in
    Python, so coercing here would let a stringly-typed caller store ``keep=1`` --
    the most destructive value available -- while believing it had sent a flag.

    Out-of-range is REFUSED rather than clamped, and there is only one end to be out of:
    below the floor. A count above any particular number is not refused, because keeping
    more archives than exist is not a harm. Clamping a below-floor value up here would
    store a number the caller did not ask for.

    Raises ``ValueError`` on anything it will not store, and propagates ``OSError``
    from the state write rather than reporting a count the next read contradicts.
    """
    if count is not None and (isinstance(count, bool) or not isinstance(count, int)):
        raise ValueError("retention count must be an int or None")
    if count is not None and count < RETENTION_KEEP_MIN:
        raise ValueError(f"retention count must be at least {RETENTION_KEEP_MIN}")

    def mutate(state: dict[str, Any]) -> None:
        entry = _account_state(state, account)
        if count is None:
            entry.pop(RETENTION_KEEP_STATE_KEY, None)
        else:
            entry[RETENTION_KEEP_STATE_KEY] = count

    # The same gate the sweep's final check holds. Without it here the lock there
    # protects nothing: this write is the one it exists to be ordered against. A
    # caller may wait for a purge already authorized to finish, which is the point --
    # it makes the two orderings the only ones possible, rather than leaving a window
    # where this write lands after the count was checked and before S3 was called.
    with _RETENTION_GATE:
        _locked_state_update(mutate)


def retention_keep(account: str) -> int | None:
    """This account's configured count, or ``None`` when retention is off.

    Reported by the backup status read so a client can see what it would act on. NO
    console renderer ships with this: the setting is reachable over HTTP only, and the
    read exists so an operator who sets a count can confirm what was stored rather than
    having to trust the write. Deliberately the same
    resolution the sweep uses, so the number returned is the number that would be acted
    on rather than the raw stored value -- reporting a count the sweep would clamp or
    ignore is worse than reporting nothing.
    """
    return _retention_keep_for_sweep(account)[0]


def retention_unclaimed(account: str) -> dict[str, Any]:
    """Per kind, the last sweep's count of REMEMBERED archives it can never retire.

    ``{kind: {"archives": int, "bytes": int, "at": iso8601}}``, and absent for a kind
    no sweep has measured yet. The two numbers are the sweep's own ``unclaimed`` and
    ``unclaimedBytes`` under plainer names, the same pair :data:`SEL_OP_RETENTION`
    carries, so a status read and the audit trail can be read against each other.

    A floor on :func:`retention_owned_keys`, not over the whole prefix: a key with
    neither an ``uploads`` entry nor a version record is filtered out before the
    measurement, so it reads 0 here however many bytes it holds. It is not counted
    nowhere -- :func:`retention_unrecorded` counts it, beside this pair on the same
    status read, and says nothing about whose it is. See
    :data:`RETENTION_UNCLAIMED_STATE_KEY`.

    AS OF ``at``, never live. Reporting it needs no cloud call and this endpoint is
    polled, so re-listing the bucket to refresh it would bill the owner for every
    poll -- which is the same reason the remote listing beside it is opt-in. The stamp
    is what makes the staleness readable instead of silent.

    A measured zero is stored and served like any other count. Writing only a non-zero
    floor would make an absent kind mean either "no floor" or "never measured", and
    those two want opposite things from an operator.

    Reported by the backup status read for the reason :func:`retention_keep` is: the
    count says what retention WILL collect, and without this an operator cannot see
    the part it never will -- which is why a bill can fail to fall after they enable
    it. NO console renderer ships with this either; the surface is HTTP only, and the
    absence is stated here so someone deciding whether to build the panel finds it.
    """
    measured = _account_view(account).get(RETENTION_UNCLAIMED_STATE_KEY, {})
    if not isinstance(measured, dict):
        return {}
    # Shape-safe per kind for the reason `_account_view` is shape-safe per level: a
    # corrupted document must read as nothing measured rather than raise on a polled
    # endpoint. Leaf values are served as stored, as `last_runs` serves a run record,
    # so this stays one projection of the state file rather than a second validator of
    # it -- the writer is the only producer and it writes ints.
    return {str(kind): dict(row) for kind, row in measured.items() if isinstance(row, dict)}


def retention_unrecorded(account: str) -> dict[str, Any]:
    """Per kind, the last sweep's count of objects it holds no record of.

    ``{kind: {"objects": int, "bytes": int, "at": iso8601}}``, and absent for a kind no
    sweep has measured yet.

    NOT a claim of ownership, and NOT a reclaim estimate. These are objects the
    listing showed under this kind's ``<subpath>/<install id>/`` folder for which this
    install holds neither an ``uploads`` entry nor a version record. Two unlike things
    land here and this count cannot separate them: archives of this install's own for
    which its state holds no record, and objects another writer put under a prefix that
    is co-writable by design. ``objects`` rather than
    ``archives`` for exactly that reason -- the install id in a key is a string anyone
    with write access can type, so calling them archives would assert something no
    reader here has checked.

    A key the listing shows only as a delete marker holds nothing and is billed
    nothing, so it is not one of these objects and is not counted.

    Nothing acts on it. These keys are skipped by the sweep before ownership is tested,
    hold no ``keep`` slot, and are never deleted. Whether any could be proven ours and
    reclaimed is a separate design owing its own argument; this number erases nothing,
    so it needs no such proof.

    Read it BESIDE :func:`retention_unclaimed`, never instead of it. That one is a
    floor on the archives this install remembers -- what retention will never collect
    out of the set it can see -- and it deliberately reads 0 for the keys counted here,
    because they are filtered out before it is taken. Two numbers because there are two
    questions; one number would answer neither.

    AS OF ``at``, never live, for the reason :func:`retention_unclaimed` is: this
    endpoint is polled and re-listing the bucket would bill the owner per poll. A
    measured zero is stored and served like any other count, so an absent kind means
    "never swept" rather than "nothing found".

    NO console renderer ships with this; the surface is HTTP only, and the absence is
    stated here so someone deciding whether to build the panel finds it.
    """
    measured = _account_view(account).get(RETENTION_UNRECORDED_STATE_KEY, {})
    if not isinstance(measured, dict):
        return {}
    # Shape-safe per kind for the reason :func:`retention_unclaimed` is: a polled
    # endpoint must read a hand-edited document as nothing measured rather than raise.
    return {str(kind): dict(row) for kind, row in measured.items() if isinstance(row, dict)}
