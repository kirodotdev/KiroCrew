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
from kiro_crew.platform_compat import file_lock, open_lock_file

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

    The write rewrites the WHOLE document, so only a missing file may read as
    empty. An unreadable one (transient EACCES/EIO, a Windows scanner holding the
    handle) raises: treating it as empty drops every account's push records, so
    the console offers duplicate billable re-pushes and loses the handles to
    remove the cloud copies.

    Corruption raises too, so a truncated ledger stays recoverable by hand. Non-UTF-8
    bytes (a ``ValueError`` the callers' ``JSONDecodeError`` clauses would miss) and a
    non-object JSON root are both raised as ``json.JSONDecodeError`` (see
    :func:`shares._load_for_update` for why not a named type).

    :func:`_update_ledger` still resets a corrupted scalar entry for the one account
    it writes, carrying every other row forward.
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

    Push, removal and reconcile all write here: two writers would each rewrite the
    WHOLE ledger from a stale snapshot and drop the other's records.

    ``mutate`` gets THIS account's slug map (a corrupted scalar entry is reset to a
    dict first, as in :func:`read_ledger`) and returns whether it changed anything;
    the file is rewritten only then, since reconcile runs on every page load.

    Raises ``OSError`` / ``json.JSONDecodeError`` for an unreadable / unparseable
    ledger (see :func:`_read_ledger_for_update`).

    The lock covers only the read and atomic rename. Callers do S3 work OUTSIDE it:
    :func:`platform_compat.file_lock` fails closed on Windows past a bounded wait, so
    holding it across a network delete would 500 a concurrent push.
    """
    lock_path = _ledger_path().with_suffix(".lock")
    _ledger_path().parent.mkdir(parents=True, exist_ok=True)
    with open_lock_file(lock_path) as fd:
        with file_lock(fd, exclusive=True, required=True):
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

    Shared by :func:`reconcile` (its listing) and :func:`library_remove` (its delete
    sweep): a record written after the observation could not have been seen by it,
    so neither may act on it.

    Sound only because ``pushedAt`` is stamped when the record is WRITTEN (inside the
    ledger lock, after the upload), not when the push began.

    ``moment`` is floored to whole seconds because ``pushedAt`` uses
    ``timespec="seconds"``; with ``>=`` this errs toward protecting a record from
    the observation's own second.

    An unstamped or unparseable record returns False (stays actionable): every
    record written here has ``pushedAt``, so it is corrupt, not concurrent, and
    must stay repairable.
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
    from kiro_crew.security import redact

    def _clean(text: str) -> str:
        text = redact(text)
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
    from kiro_crew.security import redact, scan_exfiltration_urls

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
        text = redact(text)
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
        # Stamped HERE, inside the lock after both uploads, not reused from the
        # sidecar's pre-upload stamp: _recorded_at_or_after needs "when this record
        # was written", or a reconcile on a listing taken during a slow upload
        # would delete a record whose objects are in the bucket.
        entry["pushedAt"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        slugs[slug] = entry
        return True

    _update_ledger(account, _record)
    return entry | {"slug": slug, "account": account}


def reconcile(
    account: str, remote_slugs: Collection[str], *, observed_at: dt.datetime
) -> list[str]:
    """Correct the ledger from the bucket. Returns the slugs it forgot.

    The bucket is truth: a record it does not back is dropped (a stale record
    makes the console refuse the push that would restore the copy). The reverse,
    a bucket slug missing from the ledger, needs one ``meta.json`` GET per slug,
    so it is reported (``routes._handle_library_list``), never invented.

    ``remote_slugs`` MUST be a COMPLETE listing (:func:`storage.list_library_folders`,
    not the paged display listing): absence from a partial one would delete live
    records.

    ``observed_at`` is REQUIRED: a record written at or after the snapshot is never
    pruned (:func:`_recorded_at_or_after`), since the push may have landed after
    the listing. Under-pruning self-corrects on the next render.

    ``routes._library_lock`` covers this within one process; the cutoff covers a
    second gateway sharing the same ledger and bucket.
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

    An ORDER, not a transaction: objects first, record second. A crash between
    them leaves a record without objects, which :func:`reconcile` repairs; the
    reverse order would leave paid objects nothing lists.

    The record is forgotten only if written before the sweep
    (:func:`_recorded_at_or_after`): a second gateway may push this slug after the
    sweep looked, and ``forgotten`` then reports False. ``routes._library_lock``
    covers the in-process race.

    The whole ``<slug>/`` prefix goes, since v1 and v2 pushes leave two content
    keys plus the sidecar. ``storage.delete_prefix`` anchors on the trailing slash
    (sibling slugs are safe), pages the batch API, and fails on a partial delete.

    The bucket is versioned, so deletes write MARKERS: the listing empties and
    presigned shares stop resolving, but prior versions stay billed until a
    version-aware purge. "Removed" means removed from the drive, as in the Drive's
    own deletes.
    """
    if not valid_slug(slug):
        # The slug becomes an object PREFIX below. An empty or '/'-shaped value
        # would widen that prefix to the whole section, so the shape is checked
        # here as well as at the route -- a blast radius is not a display
        # concern to be validated only on the way in.
        raise ValueError(f"{slug!r} is not an artifact slug")
    # Taken BEFORE the delete: an upload landing after delete_prefix's final
    # listing survives the sweep, and a later cutoff would forget its record.
    # Reading early can only leave a stale record, which reconcile repairs.
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
