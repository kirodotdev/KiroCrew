"""Share ledger — the local record of every live presigned share.

A presigned URL is self-contained: once minted, S3 honours it until it
expires and there is nothing to revoke server-side. What the Access section
can honestly show is therefore a LEDGER: what was shared, when it stops
working, and (approximately) with whom — written at mint time, pruned as
entries expire. "Forget" removes the record; it does not (cannot) kill the
link early, and the UI copy says so.

The ledger stores metadata only — never the URL itself. The URL embeds a
signature that IS the access grant; persisting it would turn the app data
dir into a credential store. It is returned once, to the human who asked.

Storage: ``<app data dir>/shares.json``, atomic-write + sidecar lock, same
pattern as the deploy pending store.

What the file holds is CURRENT STATE, not an audit log, and the difference
decides what may edit it. It is state because it self-empties: :func:`_prune`
drops every expired entry on both the read and the write path, and
:func:`record_share` keeps only the newest ``_MAX_SHARES``. An audit log does
neither. The audit trail of minted URLs is a separate, real one — ``routes``
writes a SEL event for every grant — so nothing is lost by this file forgetting.

The state it holds is a GRANT: a URL was minted for this key, and has not
expired. It is NOT a claim that the object is still there, which is why
:func:`mark_missing_objects` annotates a row whose object is gone instead of
removing it. Only the two functions below WRITE this file.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Collection, Optional

from kiro_crew.apps.builtins.aws_control.backend import storage
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.platform_compat import file_lock

logger = logging.getLogger(__name__)

APP_NAME = "aws-control"
_MAX_SHARES = 200
_NOTE_MAX = 120


def _store_path() -> Path:
    return app_data_dir(APP_NAME) / "shares.json"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _load() -> list[dict[str, Any]]:
    """Every recorded share, or ``[]`` when there is nothing readable.

    A DISPLAY read: :func:`list_shares` must render on a store it could not
    load rather than failing the Access section. See :func:`_load_for_update`
    for why a mutation may not stand on the same answer.

    An absent file is silent -- that is a store with no shares yet, not a fault.
    Anything else is logged, because the state this degrades into looks exactly
    like health: an empty Access section renders as "nothing is shared", which
    for a ledger of live presigned URLs is the one wrong answer nobody would
    question.
    """
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning(
            "aws-control shares: ledger unreadable; the Access section will render empty",
            exc_info=True,
        )
        return []
    if not isinstance(data, list):
        # The same degradation reached without a parse failure -- same silence
        # problem, same log line.
        logger.warning(
            "aws-control shares: ledger root is not an array; the Access section "
            "will render empty"
        )
        return []
    return data


def _load_for_update() -> list[dict[str, Any]]:
    """The ledger a read-modify-write is allowed to publish over.

    Both mutations below rewrite the WHOLE file from what they read, so an
    empty base is not "nothing to carry forward" -- it is "forget every share
    already recorded". Only a MISSING file makes that true. An unreadable one
    (a transient EACCES/EIO, a scanner holding the handle on Windows) is a
    ledger we still have, and this one is the only local record of live
    PRESIGNED URLs: they are bearer grants that cannot be revoked, so a
    truncated ledger under-reports access that is still working. The error
    propagates and the mutation is abandoned instead.

    Corruption propagates too (#7805, mirroring #7794): a document that failed
    to parse carries nothing to merge into, but "cannot merge into" is not
    "safe to destroy". A truncated file still holds most of its records
    verbatim, and replacing it discards the operator's only chance to recover
    them by hand -- silently, while a refusal costs one skipped mutation and a
    visible error. Two shapes that never reach ``json.loads``'s own raise are
    folded into the same refusal: a byte stream that is not UTF-8 (which
    arrives as ``UnicodeDecodeError`` -- a ``ValueError`` but NOT a
    ``JSONDecodeError``, so left unwrapped it would slip past every corruption
    clause at the callers), and valid JSON whose root is not an array (which
    parses without raising, so normalizing it to ``[]`` would destroy a
    document nobody could read -- the same loss, reached without a parse
    failure).

    Plain ``json.JSONDecodeError`` rather than ops-mission-control's named
    ``CorruptDocumentError``: that type lives in another app and apps do not
    import each other.

    The per-ROW check exists because this reader's return value is not written
    back verbatim: both mutations pipe it through :func:`_prune`, whose damage
    path silently DROPS any row it cannot read an ``expiresAt`` from -- a
    non-object row, a missing stamp, a mangled one -- and the whole-file
    rewrite then takes those rows with it. That is the same coercion loss the
    secret store's strict reader refuses, arriving one call later. So every
    row must hold the one field the retention pass needs to make a
    keep/expire decision; a row it would drop for DAMAGE refuses the mutation,
    while the deliberate expiry drop (a parseable stamp in the past) stays
    what it is: retention, not loss. The refusal names the row's index and
    nothing else -- entry content must not ride on an exception that crosses
    into responses and logs.
    """
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except UnicodeDecodeError as exc:
        raise json.JSONDecodeError(
            f"share ledger is not valid UTF-8: {exc.reason}",
            exc.object.decode("utf-8", "replace")[:120],
            0,
        ) from exc
    if not isinstance(data, list):
        raise json.JSONDecodeError("share ledger root is not a JSON array", str(data)[:120], 0)
    for index, entry in enumerate(data):
        damaged = not isinstance(entry, dict)
        if not damaged:
            try:
                dt.datetime.fromisoformat(entry["expiresAt"])
            except (KeyError, ValueError, TypeError):
                damaged = True
        if damaged:
            raise json.JSONDecodeError(
                f"share ledger entry {index} has no readable expiresAt, so the "
                "retention pass would silently drop it",
                "",
                0,
            )
    return data


def _save(entries: list[dict[str, Any]]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(entries, indent=1))


def _prune(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop entries whose link is already dead."""
    now = _now()
    alive: list[dict[str, Any]] = []
    for entry in entries:
        try:
            expires = dt.datetime.fromisoformat(entry["expiresAt"])
        except (KeyError, ValueError, TypeError):
            continue
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt.timezone.utc)
        if expires > now:
            alive.append(entry)
    return alive


def record_share(
    *, account: str, section: str, key: str, expires_secs: int, note: str = ""
) -> dict[str, Any]:
    """Append one share record (called at mint time). Returns the record."""
    entry = {
        "id": str(uuid.uuid4()),
        "account": account,
        "section": section,
        "key": key,
        "createdAt": _now().isoformat(timespec="seconds"),
        "expiresAt": (_now() + dt.timedelta(seconds=expires_secs)).isoformat(timespec="seconds"),
        "note": note[:_NOTE_MAX],
    }
    lock_path = _store_path().with_suffix(".lock")
    _store_path().parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fd:
        with file_lock(fd.fileno(), exclusive=True, required=True):
            entries = _prune(_load_for_update())
            entries.append(entry)
            _save(entries[-_MAX_SHARES:])
    return entry


def list_shares(account: str = "") -> list[dict[str, Any]]:
    """Live shares, newest first; optionally scoped to one account."""
    entries = _prune(_load())
    if account:
        entries = [e for e in entries if e.get("account") == account]
    return sorted(entries, key=lambda e: e.get("createdAt", ""), reverse=True)


def forget_share(share_id: str) -> Optional[dict[str, Any]]:
    """Remove one record from the ledger (the link itself lives to expiry)."""
    lock_path = _store_path().with_suffix(".lock")
    _store_path().parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fd:
        with file_lock(fd.fileno(), exclusive=True, required=True):
            entries = _prune(_load_for_update())
            kept = [e for e in entries if e.get("id") != share_id]
            removed = next((e for e in entries if e.get("id") == share_id), None)
            _save(kept)
    return removed


def mark_missing_objects(
    entries: list[dict[str, Any]], present_keys: Collection[str]
) -> list[dict[str, Any]]:
    """Annotate rows whose object is not in ``present_keys`` (``objectMissing``).

    ANNOTATES, never prunes, and writes nothing: the returned rows are for one
    response. That is the whole shape of the correction, and the reason is what
    this ledger claims. It claims "a presigned URL was minted for this key and
    has not expired yet" — and deleting the object does not un-mint the URL or
    shorten its expiry, so the bucket cannot disprove the claim. It can only add
    to it. Contrast ``library.reconcile``, which DOES prune: that ledger claims
    "a cloud copy exists", which is exactly a claim the bucket settles.

    Two consequences make dropping the row the wrong reading rather than merely
    the harsher one:

    * A presigned URL signs bucket, key and expiry — not a version. Re-creating
      the key while the URL is unexpired makes it resolve again, so a deleted
      object leaves the grant DORMANT, not dead. A dropped row would forget a
      bearer URL that can come back to life, which is the under-reporting
      direction the Access section exists to avoid.
    * Dropping the row is precisely :func:`forget_share`, which this app
      documents to the user as removing the record while the link lives on. A
      delete route taking that decision silently would be the system doing the
      one thing the copy promises it will not do on the user's behalf.

    The mark is not persisted for the same reason it is not a prune: it is a
    fact about the bucket at render time, and a stored ``objectMissing`` would
    itself go stale the moment the key is re-created — stale in the direction
    that under-reports access. A value recomputed per render cannot go stale.
    It also keeps ``record_share`` and ``forget_share`` the only writers of this
    file, the rule ``library._update_ledger`` had to be written to restore.

    ``present_keys`` MUST be a COMPLETE listing of the drive
    (:func:`storage.list_object_keys`), because this function concludes ABSENCE
    from it. There is deliberately no observation-cutoff parameter, unlike
    ``library.reconcile``: the caller reads the ledger BEFORE the listing is
    taken, so no row reaching this function can postdate the listing, and the
    race a cutoff exists to cover cannot arise. Callers must keep that order.

    A row is matched by its own ``section``/``key`` through
    :func:`storage.section_key` — the one mapper — rather than a second copy of
    the prefix scheme, which would resolve rows to keys the drive can never have
    written and mark every one of them missing.
    """
    present = set(present_keys)
    marked: list[dict[str, Any]] = []
    for entry in entries:
        section = str(entry.get("section", ""))
        key = str(entry.get("key", ""))
        if section in storage.SECTION_PREFIXES and storage.section_key(section, key) in present:
            marked.append(entry)
            continue
        # An unknown section reaches here too, and is marked. It cannot address
        # an object -- `section_key` has no prefix for it -- so no listing can
        # ever back it, and saying so is more use than rendering it as fine.
        marked.append({**entry, "objectMissing": True})
    return marked
