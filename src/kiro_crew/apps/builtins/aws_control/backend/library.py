"""Library — cloud copies of artifacts on the drive's ``artifacts/`` prefix.

An artifact's current version is uploaded as ``artifacts/<slug>/v<N>`` plus a
``meta.json`` sidecar, and a local sync ledger records what was pushed when.
Artifact versions map onto both the version-named keys AND the bucket's object
versioning, so history survives even a same-key overwrite. Pull stays a
download (share/presign or Drive download) — merging a cloud copy back into the
local store is future work.

The ledger (``<app data dir>/library.json``) is display state, not truth:
truth is the bucket listing; the ledger only makes "synced · 2h ago" cheap.
That is a claim the code has to keep, so two rules hold it up:

* :func:`_update_ledger` is the ONLY writer. Push, removal and reconcile all
  edit the file through it, because two independent writers of one JSON
  document is how a later whole-file write drops an earlier record.
* :func:`reconcile` lets the bucket CORRECT the ledger. Without it a stale
  record is believed indefinitely, and the surface ends up accommodating the
  divergence instead of resolving it. It prunes only what the bucket has had a
  chance to disprove: a record stamped at or after the listing it is judged
  against is left alone, because that listing predates the record.

CALLER CONTRACT: same as storage.py — handlers hold the consent gate; sync
functions, call via ``asyncio.to_thread``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import tempfile
from pathlib import Path
from typing import Any, Callable, Collection

from kiro_crew.apps.builtins.aws_control.backend import storage
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.artifacts import ArtifactValidationError, _validate_slug, get_default_store
from kiro_crew.atomic_write import atomic_write
from kiro_crew.deploy.engine import AWSError
from kiro_crew.platform_compat import file_lock

logger = logging.getLogger(__name__)

APP_NAME = "aws-control"


def valid_slug(slug: str) -> bool:
    """Whether ``slug`` has a shape the artifact store could have produced.

    Checked against the store's OWN validator rather than a second copy of the
    pattern: removal turns the slug into an object prefix, and a local copy that
    drifted from ``artifacts._SLUG_RE`` would let a caller address a prefix the
    store can never have written. Shape only, never existence -- a cloud copy
    whose local artifact was deleted is precisely the thing that must stay
    removable.
    """
    try:
        _validate_slug(slug)
    except ArtifactValidationError:
        return False
    return True


#: Artifact kind → pushed file extension (content is text for all of these).
_KIND_EXT = {
    "widget": ".html",
    "html": ".html",
    "markdown": ".md",
    "svg": ".svg",
    "json": ".json",
    "text": ".txt",
    "webapp": ".html",
}


def _ledger_path() -> Path:
    return app_data_dir(APP_NAME) / "library.json"


def read_ledger() -> dict[str, Any]:
    """The push ledger, or ``{}`` when there is nothing readable.

    A DISPLAY read: the Library list must keep rendering local artifacts on a
    ledger it could not load. See :func:`_read_ledger_for_update` for why the
    single writer may not stand on the same answer.

    An absent file is silent -- no push has happened yet, not a fault. Anything
    else is logged, because the state this degrades into looks exactly like
    health: every artifact renders as never pushed, which is also what a
    healthy fresh install shows.
    """
    try:
        data = json.loads(_ledger_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning(
            "aws-control library: push ledger unreadable; every artifact will "
            "render as never pushed",
            exc_info=True,
        )
        return {}
    if not isinstance(data, dict):
        # The same degradation reached without a parse failure -- and the same
        # silence problem, so it gets the same log line.
        logger.warning(
            "aws-control library: push ledger root is not an object; every "
            "artifact will render as never pushed"
        )
        return {}
    return data


def _read_ledger_for_update() -> dict[str, Any]:
    """The ledger :func:`_update_ledger` is allowed to publish over.

    The write rewrites the WHOLE document, so an empty base there does not mean
    "no records to carry forward" -- it means "drop every other account's push
    state, and every other slug's record for this one". Only a missing file
    makes that true; an unreadable one (a transient EACCES/EIO, a scanner
    holding the handle on Windows) is state we still have. Losing it is not
    cosmetic: the console then reports pushed artifacts as never pushed, which
    offers a re-push (a duplicate billable upload) and leaves the real cloud
    copies with no record to remove them by.

    Corruption propagates too (#7805, mirroring #7794): "cannot merge into" is
    not "safe to destroy". A truncated ledger still holds most of its records
    verbatim, and replacing it discards the operator's only chance to recover
    them by hand. Two shapes that never reach ``json.loads``'s own raise are
    folded into the same refusal: a byte stream that is not UTF-8 (a
    ``ValueError`` but NOT a ``JSONDecodeError``, so unwrapped it would slip
    past every corruption clause at the callers) and valid JSON whose root is
    not an object (which parses without raising, so coercing it to ``{}``
    would destroy a document nobody could read). Plain ``json.JSONDecodeError``
    rather than another app's named type -- see :func:`shares._load_for_update`
    in this app for the reasoning.

    The per-ACCOUNT tolerance in :func:`_update_ledger` (a corrupted scalar
    entry for the account being written is reset) is deliberately untouched:
    it replaces one account's unusable entry while carrying every other row
    forward verbatim, not the whole document. The sidecar-preserve alternative
    for even that case is tracked in #7789.
    """
    try:
        data = json.loads(_ledger_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError as exc:
        raise json.JSONDecodeError(
            f"push ledger is not valid UTF-8: {exc.reason}",
            exc.object.decode("utf-8", "replace")[:120],
            0,
        ) from exc
    if not isinstance(data, dict):
        raise json.JSONDecodeError("push ledger root is not a JSON object", str(data)[:120], 0)
    return data


def _write_ledger(ledger: dict[str, Any]) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(ledger, indent=1))


def _update_ledger(account: str, mutate: Callable[[dict[str, Any]], bool]) -> bool:
    """The ONE writer of ``library.json``. Returns whether the file was written.

    Every mutation — a push recording a copy, a removal forgetting one, a
    reconcile pruning what the bucket does not back — goes through here, so
    there is exactly one place that reads, edits and rewrites this file. Two
    independent writers of one JSON document is how a later atomic write drops
    an earlier record: each would rewrite the WHOLE ledger from its own stale
    snapshot.

    ``mutate`` receives THIS account's slug map (always a dict — a corrupted
    scalar entry is reset first, the same tolerance :func:`read_ledger` shows)
    and returns whether it changed anything. The file is rewritten only when it
    did, so a reconcile that finds nothing stale costs no disk write on a
    surface that renders on every page load.

    Raises ``OSError`` when the existing ledger could not be read and
    ``json.JSONDecodeError`` when it could not be parsed; see
    :func:`_read_ledger_for_update` for why neither is collapsed to an empty
    document here.

    The lock is held for a read plus an atomic rename and nothing else. Callers
    that also touch S3 do that OUTSIDE this block on purpose:
    :func:`platform_compat.file_lock` documents every in-tree critical section
    as sub-second, and its Windows path FAILS CLOSED once a bounded ceiling
    passes — so holding it across a multi-round network delete would turn a
    concurrent push into a 500.
    """
    lock_path = _ledger_path().with_suffix(".lock")
    _ledger_path().parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fd:
        with file_lock(fd.fileno(), exclusive=True, required=True):
            ledger = _read_ledger_for_update()
            slugs = ledger.get(account)
            if not isinstance(slugs, dict):
                slugs = {}
            ledger[account] = slugs
            if not mutate(slugs):
                return False
            _write_ledger(ledger)
            return True


def _recorded_at_or_after(entry: Any, moment: dt.datetime) -> bool:
    """Whether ``entry``'s ledger record was written at or after ``moment``.

    ONE rule, two callers, because both ask the same question of a remote
    observation: "could what I saw have covered this record?" :func:`reconcile`
    asks it of the listing it prunes against, and :func:`library_remove` of the
    delete sweep it is about to forget a record for. A record written after the
    observation is one the observation could not have seen, so neither may act on
    it. Two copies of a rule this subtle would drift.

    ``pushedAt`` is stamped when the record is WRITTEN (inside the ledger lock,
    after the upload has already succeeded), not when the push began -- that is
    what makes this comparison sound. A pre-upload stamp would read older than an
    observation that ran during a slow upload, and the record it names would be
    acted on despite being newer than the observation.

    ``moment`` is floored to the second the ledger records at: ``pushedAt`` is
    written with ``timespec="seconds"``, so a record written at 12:00:00.9 stores
    "12:00:00" and an unfloored 12:00:00.1 comparison would read it as older.
    Flooring plus ``>=`` absorbs exactly that truncation, at the cost of
    protecting a record written in the observation's own second -- one round's
    delay, in the safe direction for both callers.

    An unstamped or unparseable record answers False, i.e. it stays actionable.
    Every record this module writes carries ``pushedAt``, so one without it is
    not a concurrent write -- and protecting it would make a corrupted record
    permanently unrepairable, which is the opposite of what reconcile is for.
    """
    if not isinstance(entry, dict):
        return False
    raw = entry.get("pushedAt")
    if not isinstance(raw, str):
        return False
    try:
        stamp = dt.datetime.fromisoformat(raw)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        # A naive stamp is this machine's own UTC clock (the writer below is
        # tz-aware UTC); read it as UTC rather than crashing on the comparison.
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp >= moment.astimezone(dt.timezone.utc).replace(microsecond=0)


def list_pushable(account: str) -> list[dict[str, Any]]:
    """Local artifacts with their push state for ONE account.

    Push state is per account (ledger keyed account -> slug): the same
    artifact can be synced to two drives, and neither console may report
    the other's state.

    Names are redacted on the way out: an LLM-authored artifact NAME can
    quote a secret as easily as its body can, and this listing is a display
    surface (the same both-pass chain the push path applies to metadata).
    """
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    def _clean(text: str) -> str:
        text, _ = redact_credentials(text)
        text, _ = redact_exfiltration_urls(text)
        return text

    ledger = read_ledger().get(account, {})
    if not isinstance(ledger, dict):
        # A corrupted per-account entry reads as empty rather than crashing
        # the Library list/push routes.
        ledger = {}
    rows: list[dict[str, Any]] = []
    for artifact in get_default_store().list():
        pushed = ledger.get(artifact.slug) or {}
        if not isinstance(pushed, dict):
            pushed = {}
        rows.append(
            {
                "slug": artifact.slug,
                "name": _clean(artifact.name),
                "kind": artifact.kind,
                "version": artifact.version,
                "updatedAt": artifact.updated_at,
                "pushedVersion": pushed.get("version"),
                "pushedAt": pushed.get("pushedAt"),
            }
        )
    rows.sort(key=lambda r: r.get("updatedAt") or "", reverse=True)
    return rows


def push_artifact(
    profile: str, region: str, bucket: str, account: str, slug: str
) -> dict[str, Any]:
    """Upload one artifact's current content + metadata sidecar.

    Image artifacts are excluded in this PR (binary asset plumbing); the
    caller surfaces the refusal as a plain message, not an error wall.
    """
    store = get_default_store()
    artifact = store.get(slug)  # raises ArtifactNotFoundError for an unknown slug
    ext = _KIND_EXT.get(artifact.kind)
    if ext is None:
        raise ValueError(f"artifact kind {artifact.kind!r} is not pushable yet")
    content = artifact.content or ""

    # Same discipline deploy-web applies before ITS artifact uploads: a
    # credential-bearing artifact is hard-blocked (the drive is private, but
    # a pushed copy is one presigned share away from anyone), and the
    # metadata sidecar runs both redaction passes — an LLM-authored name or
    # description can quote a secret as easily as the body can.
    from kiro_crew.deploy.scan import is_credential_finding, scan_content
    from kiro_crew.security import (
        redact_credentials,
        redact_exfiltration_urls,
        scan_exfiltration_urls,
    )

    if any(is_credential_finding(f) for f in scan_content(content)):
        raise ValueError(
            "this artifact contains credential-like content and will not be "
            "uploaded — remove the secret and push again"
        )
    # LLM-authored content can carry a beacon: a suspicious URL that exfiltrates
    # whatever page context it is embedded in once the pushed copy is shared.
    # The scanner is targeted (heuristic hosts, exemption list) — ordinary links
    # pass; a flagged one blocks the push rather than being silently rewritten.
    if scan_exfiltration_urls(content):
        raise ValueError(
            "this artifact links to a suspicious external endpoint and will "
            "not be uploaded — remove the URL and push again"
        )

    def _clean(text: str) -> str:
        text, _ = redact_credentials(text)
        text, _ = redact_exfiltration_urls(text)
        return text

    meta = {
        "slug": artifact.slug,
        "name": _clean(artifact.name),
        "kind": artifact.kind,
        "version": artifact.version,
        "description": _clean(artifact.description),
        "tags": [_clean(t) for t in (artifact.tags or [])],
        "pushedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    with tempfile.TemporaryDirectory(prefix="kc-library-") as tmp:
        content_path = Path(tmp) / f"v{artifact.version}{ext}"
        content_path.write_text(content, encoding="utf-8")
        meta_path = Path(tmp) / "meta.json"
        meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
        storage.put_file(
            profile,
            region,
            bucket,
            "library",
            f"{slug}/v{artifact.version}{ext}",
            str(content_path),
            account=account,
        )
        storage.put_file(
            profile,
            region,
            bucket,
            "library",
            f"{slug}/meta.json",
            str(meta_path),
            account=account,
        )

    # Through the single ledger writer: two concurrent pushes of different
    # slugs would otherwise each rewrite the whole ledger from a stale
    # snapshot, and the later atomic write would silently drop the earlier
    # record. Removal and reconcile write through the same helper.
    entry = {
        "version": artifact.version,
        "kind": artifact.kind,
    }

    def _record(slugs: dict[str, Any]) -> bool:
        # Stamped HERE -- inside the ledger lock, after both uploads have
        # succeeded -- and deliberately not reused from the metadata sidecar,
        # which is stamped before the upload starts. `pushedAt` is what
        # _recorded_at_or_after compares a remote observation against, so it has
        # to mean "when this record came into existence". With the sidecar's
        # pre-upload stamp, a listing that ran DURING a slow upload would
        # postdate the stamp while predating the record, and a reconcile on that
        # listing would delete a record whose objects are in the bucket. The
        # sidecar keeps its own stamp: it describes when the push began, which is
        # the right thing for remote metadata to say.
        entry["pushedAt"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        slugs[slug] = entry
        return True

    _update_ledger(account, _record)
    return entry | {"slug": slug, "account": account}


def reconcile(
    account: str, remote_slugs: Collection[str], *, observed_at: dt.datetime
) -> list[str]:
    """Correct the ledger from the bucket. Returns the slugs it forgot.

    This is the direction that was missing, and every symptom the removal
    action kept producing came from its absence. The ledger is LOCAL and the
    bucket is REMOTE, the two can disagree, and only one of them is truth — so
    a record the bucket does not back is dropped rather than believed. A stale
    record is not cosmetic: it made the console refuse the very push that would
    have restored a cloud copy deleted somewhere else.

    Only that direction is safe to automate. A slug present in the bucket but
    absent from the ledger cannot be reconstructed here — version and push time
    live in its ``meta.json``, one GET per slug — so it is reported to the
    caller instead (see ``routes._handle_library_list``), never invented.

    ``remote_slugs`` MUST be a COMPLETE listing of this account's library
    prefix, which is what :func:`storage.list_library_folders` guarantees and
    what the paged display listing does not. This function concludes ABSENCE
    from it, and absence from a partial listing would delete live records.

    ``observed_at`` is when that listing was taken, and it is REQUIRED because
    the listing is a snapshot: a push completing after it — objects uploaded,
    record written — is absent from ``remote_slugs`` while the bucket does back
    it, and pruning on the stale snapshot would delete a live record. So a
    record written at or after the snapshot is never pruned; the bucket has not
    had a chance to disprove it yet (see :func:`_recorded_at_or_after`).
    Under-pruning is the safe direction, and it self-corrects: the next render's
    snapshot postdates the record.

    Callers hold ``routes._library_lock`` across listing and prune, which closes
    the same window structurally IN THIS PROCESS. The cutoff is what still holds
    when that is not enough — a second gateway on the same machine shares this
    ledger and this bucket, and an in-process lock does not reach it.
    """
    present = set(remote_slugs)
    forgotten: list[str] = []

    def _prune(slugs: dict[str, Any]) -> bool:
        stale = [
            slug
            for slug, entry in slugs.items()
            if slug not in present and not _recorded_at_or_after(entry, observed_at)
        ]
        for slug in stale:
            del slugs[slug]
        forgotten.extend(stale)
        return bool(stale)

    if _update_ledger(account, _prune):
        logger.info(
            "aws-control library: forgot %d ledger record(s) the bucket does not back",
            len(forgotten),
        )
    return forgotten


def library_remove(
    profile: str, region: str, bucket: str, account: str, slug: str
) -> dict[str, Any]:
    """Remove one artifact's cloud copy AND its ledger record, together.

    "Together" is an ORDER, not a transaction: a local file and a remote bucket
    cannot be committed as one, and pretending otherwise is what produced three
    rounds of the same defect. So the order is chosen for which divergence a
    crash between the two halves can leave:

    * objects first, record second — the worst case is "the ledger claims a
      copy the bucket does not have", which is exactly the state
      :func:`reconcile` repairs on the next listing;
    * the reverse would leave an untracked cloud copy — objects nothing points
      at, in a bucket the user pays for, invisible to the surface that would
      remove them.

    That is why the two halves of this fix are one change: the removal is only
    safe to order this way because the reconcile direction exists.

    The record is forgotten CONDITIONALLY. A second gateway sharing this ledger
    can push this slug after the delete sweep has finished looking, and its
    record names objects that really are in the bucket -- so a record written at
    or after the sweep survives and ``forgotten`` reports False. The same rule
    reconcile applies to its listing (:func:`_recorded_at_or_after`), asked of
    the sweep instead. ``routes._library_lock`` already prevents the in-process
    version of this race; this is the half that holds across processes.

    The whole ``<slug>/`` prefix goes, not just the version the ledger recorded.
    An artifact pushed at v1 and again at v2 has TWO content keys plus the
    sidecar, and deleting only the recorded version would leave a paid object
    behind that no surface lists. ``storage.delete_prefix`` anchors on the
    trailing slash (so a sibling slug sharing a name-prefix is not swept in),
    pages the batch API, and fails rather than reporting a partial delete as
    done.

    The bucket is versioned, so each delete writes a delete MARKER: the listing
    empties and a presigned share stops resolving, while prior versions remain
    until the version-aware purge the spec still calls out. "Removed" here means
    removed from the drive, not billing-zero — the same meaning the Drive's own
    file and folder deletes carry.
    """
    if not valid_slug(slug):
        # The slug becomes an object PREFIX below. An empty or '/'-shaped value
        # would widen that prefix to the whole section, so the shape is checked
        # here as well as at the route -- a blast radius is not a display
        # concern to be validated only on the way in.
        raise ValueError(f"{slug!r} is not an artifact slug")
    # Taken BEFORE the delete, never after -- the same rule the reconcile path
    # states for its listing cutoff, and for the same reason: a cutoff that
    # POSTDATES the observation it describes protects nothing. `delete_prefix`
    # stops looking at its final listing, so an upload landing after that survives
    # the sweep; if the cutoff were read once delete_prefix returned, a record
    # written in that gap would compare as OLDER than the cutoff and be forgotten
    # while its objects sat in the bucket. Reading it early only widens the set of
    # records left alone, and a record left behind whose objects really were
    # deleted is merely stale -- which reconcile repairs on the next render.
    swept_at = dt.datetime.now(dt.timezone.utc)
    objects = storage.delete_prefix(profile, region, bucket, "library", slug, account=account)
    # delete_prefix does NOT guarantee the prefix is empty when it returns: a
    # garbled listing page makes it stop the walk and report the count so far,
    # deliberately, so a bad page can only under-delete. That is the right choice
    # there and the wrong thing to forget a record on -- the ledger would drop a
    # copy that is still in the bucket and the response would call it removed. So
    # the absence is CONFIRMED, before the ledger is touched, and a slug still
    # present raises rather than reporting a removal that did not happen.
    if slug in set(storage.list_library_folders(profile, region, bucket, account=account)):
        raise AWSError(
            f"objects under {slug} are still present after the delete; refusing to "
            "report the copy as removed or to forget its record"
        )

    def _forget(slugs: dict[str, Any]) -> bool:
        # CONDITIONAL, not an unconditional pop. A second gateway pushing this
        # same slug after the sweep finished writes a record whose objects are
        # really in the bucket; popping it would leave objects with no record and
        # report a removal that did not fully happen. Such a record survives, and
        # `forgotten` says so, which is the honest answer -- the objects then
        # surface as `remoteOnly` on the next render and stay removable.
        if _recorded_at_or_after(slugs.get(slug), swept_at):
            return False
        return slugs.pop(slug, None) is not None

    forgotten = _update_ledger(account, _forget)
    return {
        "slug": slug,
        "account": account,
        "objects": objects,
        # Whether a RECORD was dropped, told apart from whether objects were.
        # A copy pushed from another machine has objects and no local record;
        # reporting one number for both would make the console unable to say
        # which of the two it just emptied.
        "forgotten": forgotten,
    }
