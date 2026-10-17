"""Remote backup listings, attributed to the installs that wrote them.

Reads the drive's key namespace and nothing a writer authors about itself, apart
from the display label, which is fetched range-bounded and sanitised.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kiro_crew.apps.builtins.aws_control.backend import storage
from kiro_crew.apps.builtins.aws_control.backend.backup_parts import _FACADE_MODULE
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.egress_text import sanitize_label
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.identity import (
    _INSTALL_ID_RE,
    KIND_SNAPSHOT,
    KIND_SUBPATHS,
    LABEL_OBJECT_NAME,
    ORIGIN_OTHER,
    ORIGIN_SELF,
    _key_basename,
    classify_key,
    install_identity,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.ledger import uploaded_keys
from kiro_crew.deploy.engine import AWSError, _checked

logger = logging.getLogger(_FACADE_MODULE)


#: How many OTHER installs' prefixes one expanded listing will enumerate.
#:
#: A RUNAWAY BOUND, not a display choice, and the distinction is what makes the
#: number this large. An earlier draft capped this at 8 and picked the shown
#: subset with ``sorted(other_ids)[:8]`` -- hex order, which is arbitrary with
#: respect to recency, so a stale prefix (a reinstall, or the process-local
#: fallback id minting a fresh one per restart) could displace the install
#: holding the newest surviving archive. That is a coin flip on exactly the
#: replacement-machine path this expansion exists for. Set high enough that
#: truncation cannot bite a real drive, the subset stops being a decision at all:
#: every install present is listed, and which one sorts first only affects the
#: order rows appear in, which the timestamp sort then fixes anyway.
#:
#: Each install still costs a list call per kind plus at most one label read, all
#: on the owner's bill -- which is why the whole expansion is opt-in. What this
#: bound protects is the pathological case, not the ordinary one: two machines is
#: what the feature is for.
MAX_OTHER_INSTALLS = 32


def _install_folders(
    profile: str, region: str, bucket: str, kind: str, *, account: str
) -> set[str]:
    """Every install id with a prefix under ``kind`` — COMPLETE, unredacted.

    Deliberately not :func:`storage.list_section`, whose page is capped and whose
    names are run through the egress redactors. Both are right for a listing that
    is about to be displayed and wrong for the ONE caller that reasons about
    ABSENCE: the nightly loop asks "is another install writing here" and answers
    "no" by finding nothing, and nothing-on-the-first-page is not nothing. The
    ``--query`` projection with no ``--max-items`` lets the CLI auto-paginate and
    apply the projection to the merged result, the same property
    ``storage.list_library_folders`` relies on, so the answer is the complete set
    or a raised error.

    The prefix is built from :data:`storage.SECTION_PREFIXES`, not passed in, which
    keeps the rule that a raw S3 prefix never comes from a caller.
    """
    prefix = storage.SECTION_PREFIXES["backup"] + f"{KIND_SUBPATHS[kind]}/"
    out = _checked(
        [
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--prefix",
            prefix,
            "--delimiter",
            "/",
            "--expected-bucket-owner",
            account,
            "--output",
            "json",
            "--query",
            "CommonPrefixes[].Prefix",
        ],
        profile,
        action="s3:ListBucket",
        timeout=60,
    )
    try:
        rows = json.loads(out or "[]") or []
    except json.JSONDecodeError:
        raise AWSError(
            "the backup folder listing returned a response that could not be read as "
            "JSON; refusing to report the prefix as unshared"
        ) from None
    found: set[str] = set()
    for row in rows:
        if not isinstance(row, str) or not row.startswith(prefix):
            continue
        name = row[len(prefix) :].rstrip("/")
        if _INSTALL_ID_RE.match(name):
            found.add(name)
    return found


def other_install_ids(
    profile: str,
    region: str,
    bucket: str,
    *,
    account: str,
    kind: str = KIND_SNAPSHOT,
) -> list[str]:
    """Install ids OTHER than this one that have written to this drive.

    The nightly loop's evidence that the drive is shared. Reads the key namespace
    and nothing a writer authors, so it cannot be talked out of the answer.

    Scoped to ONE kind, defaulting to the snapshot prefix, and its only caller is
    the nightly loop -- which uploads snapshots and nothing else. Sweeping both
    prefixes cost two paid LIST calls per scheduled run to answer a question one
    call answers for the run actually happening.

    This is a cost tradeoff, not a complete census, and it is worth being exact
    about what it gives up: the two manual runners write one kind each, so an
    install whose only backup was an on-demand SESSIONS upload has no folder under
    ``snapshots/`` and this read does not see it. The notice is therefore a
    best-effort signal about a shared drive rather than proof of who else writes
    here. :func:`list_remote_backups` is the complete view, and it is the one a
    reader consults before restoring.
    """
    mine = install_identity()["id"]
    seen = _install_folders(profile, region, bucket, kind, account=account)
    return sorted(seen - {mine})


def read_remote_label(
    profile: str, region: str, bucket: str, kind: str, install_id: str, *, account: str
) -> str:
    """The label another install published for itself, sanitised for display.

    LAZY on purpose: called only for an install whose rows are about to be shown,
    so an ordinary listing pays nothing and an expanded one pays one bounded GET
    per foreign install.

    The transfer is RANGE-bounded through :func:`storage.get_object_head_bytes`
    rather than a plain download. The object is written by another install, so its
    size is that install's choice; a full ``get-object`` of a file named
    ``_label.json`` would let a multi-gigabyte object be pulled onto this disk, on
    the owner's transfer bill, to render one caption. Two kilobytes is far more
    than a label needs and is the whole exposure.

    Every failure answers the empty string, which the caller renders as the id.
    A caption that could not be read must not break the listing that would have
    told the operator whose archives these are.
    """
    key = f"{KIND_SUBPATHS[kind]}/{install_id}/{LABEL_OBJECT_NAME}"
    try:
        raw, _size = storage.get_object_head_bytes(
            profile, region, bucket, "backup", key, account=account, max_bytes=2048
        )
        doc = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except Exception:
        logger.debug("aws-control: no readable backup label at %s", key, exc_info=True)
        return ""
    if not isinstance(doc, dict):
        return ""
    return sanitize_label(doc.get("label"))


def _archive_row(entry: dict[str, Any], install_id: str, uploaded: set[str]) -> dict[str, Any]:
    """One listing row, with its origin decided from the key plus what we uploaded."""
    origin, owner = classify_key(str(entry.get("key", "")), install_id, uploaded)
    return {**entry, "install": owner, "origin": origin}


def _archive_sort_key(entry: dict[str, Any]) -> tuple[str, str]:
    """Newest first, ACROSS installs.

    Sorting by the whole key would sort by install id first, so two machines'
    archives would render as two blocks and the newest overall would not be at the
    top -- which is how an operator picks the wrong one.

    ``modified`` leads because it is S3's own answer about the object rather than
    an inference from its name. The basename is the tie-break and not the primary
    key: it happens to order archives by time today, since every name this app
    writes begins with the same ``%Y%m%dT%H%M%SZ`` stamp, but an object put under
    these prefixes by any other tool carries no such promise, and one differently
    named file would then sort the whole list wrong. A missing or non-string
    timestamp degrades to the name rather than raising.
    """
    modified = entry.get("modified")
    return (modified if isinstance(modified, str) else "", _key_basename(str(entry.get("key", ""))))


def list_remote_backups(
    profile: str,
    region: str,
    bucket: str,
    *,
    account: str,
    include_others: bool = False,
) -> dict[str, Any]:
    """Remote backup listings for both kinds, attributed to the installs that wrote them.

    Three listing calls per kind in the default view. The nested key shape is what
    buys the first one two answers at once: a '/'-delimited list of ``snapshots/``
    returns every install id present as a FOLDER and every pre-namespace archive as
    a FILE, so "who else writes here" and "what is unattributed" arrive together.
    The second is :func:`_install_folders`, which re-reads the ids unredacted -- the
    display page above is capped and passes names through the egress redactors, so a
    hex id could in principle come back rewritten and a co-tenant would then read as
    absent. The third reads this install's own prefix.

    ``include_others`` enumerates the other installs' prefixes too, capped at
    :data:`MAX_OTHER_INSTALLS`. It is opt-in because it costs a list call per
    install per kind plus a label read, and it EXISTS because a replacement machine
    has no archives of its own: every archive in the bucket is foreign there, so a
    view that only ever showed this install's own prefix would show a fresh install
    nothing at all -- on the one occasion the backup is the only copy left.
    """
    identity = install_identity()
    mine = identity["id"]
    mine_uploads = uploaded_keys(account)
    result: dict[str, Any] = {}
    other_ids: set[str] = set()

    for kind, sub in KIND_SUBPATHS.items():
        # Call 1: folders name the installs, files are the legacy flat archives.
        page = storage.list_section(profile, region, bucket, "backup", sub, account=account)
        rows = [_archive_row(f, mine, mine_uploads) for f in page["files"]]
        # The install ids come from `_install_folders`, not from this page's
        # `folders`. One implementation of "which install ids have prefixes here"
        # rather than two, and it is the COMPLETE, unredacted one: `list_section`
        # is a display read whose page is capped and whose names pass through the
        # egress redactors, so a hex id could in principle come back rewritten and
        # a co-tenant would then read as absent.
        other_ids |= _install_folders(profile, region, bucket, kind, account=account) - {mine}
        # Call 2: this install's own archives.
        prefixes = [mine]
        if include_others:
            prefixes += sorted(other_ids)[:MAX_OTHER_INSTALLS]
        for install_id in prefixes:
            owned = storage.list_section(
                profile, region, bucket, "backup", f"{sub}/{install_id}", account=account
            )
            rows += [
                _archive_row(f, mine, mine_uploads)
                for f in owned["files"]
                # The label sidecar shares the prefix with the archives it labels.
                # It is not an archive and must never be offered for restore.
                if _key_basename(str(f.get("key", ""))) != LABEL_OBJECT_NAME
            ]
        rows.sort(key=_archive_sort_key, reverse=True)
        result[kind] = rows[:20]

    # Labels, for the installs whose rows are actually on screen. This install's
    # own comes from local state; a foreign one is fetched, once, and only when its
    # archives are being listed.
    installs: list[dict[str, Any]] = [
        {"id": mine, "label": identity["label"], "origin": ORIGIN_SELF}
    ]
    shown = sorted(other_ids)[:MAX_OTHER_INSTALLS]
    for install_id in shown:
        label = ""
        if include_others:
            for kind in KIND_SUBPATHS:
                label = read_remote_label(
                    profile, region, bucket, kind, install_id, account=account
                )
                if label:
                    break
        installs.append({"id": install_id, "label": label, "origin": ORIGIN_OTHER})
    result["installs"] = installs
    result["others"] = len(other_ids)
    result["truncated"] = len(other_ids) > MAX_OTHER_INSTALLS
    # The cap travels with the answer rather than being inferred from the roster
    # length. `installs` carries THIS install as its first entry, so a reader
    # deriving the cap from its length is off by one on the very message that
    # exists to state the cap -- and it would be off silently.
    result["max"] = MAX_OTHER_INSTALLS
    return result
