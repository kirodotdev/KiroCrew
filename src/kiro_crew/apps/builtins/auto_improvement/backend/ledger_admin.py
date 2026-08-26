"""Ledger maintenance: forget a finding, purge one, sweep the dead ones.

The ledger is the dedup layer. Every locus the loop discovers is fingerprinted
(``kind`` + ``target`` + signature → ``sha256[:16]``) and its outcome appended as one
JSONL event; on the next run ``Ledger.known()`` consults the latest event for a
fingerprint and skips any locus that already reached a decision. That is what stops
the loop re-proposing the same fix forever — and it is also what makes a *wrong*
decision permanent, because "failed_gate" is treated as a verdict, not a hint.

These three operations are the escape hatch, and all three work by APPENDING a
``purged`` event rather than editing history:

    forget(fp)      the locus becomes re-discoverable; artifacts are left alone
    purge(fp)       same, and the fingerprint-addressed artifacts are deleted
    purge_dead()    sweep every record that can no longer make progress

WHY ``purged`` and not a delete: ``Ledger.known()`` returns False for
:data:`STATUS_PURGED` (see ``spine/ledger.py``), so appending one event is exactly
"the dedup layer no longer knows this locus" — the retry falls out of the existing
status machine instead of needing a second mechanism. Rewriting or truncating the
file would also destroy the audit trail, which is the ledger's other job: the
timeline view shows ``seen → failed_gate → purged`` and a reader can see that a
human intervened.

WHY the file is only ever appended to, under a lock: a run may be appending
concurrently with this request. A read-modify-write would race that append and lose
an event; an append cannot. The lock serializes read → decide → append here so two
requests cannot both conclude "no prior event" for one fingerprint. Readers tolerate
a torn final line (a crash mid-append), so one bad tail never hides earlier entries.

The on-disk field carrying the pull-request reference is historically named ``cr``.
Both spellings are READ; the ledger event written here keeps ``cr`` because
``spine.ledger.LedgerEntry`` is a fixed-field dataclass that rejects an unknown key
outright — an event written with ``pr`` is dropped by ``Ledger._load()``, which would
silently defeat the purge (see :func:`_purged_event`). Everything this module
*returns* speaks ``pr``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any

from . import store

logger = logging.getLogger(__name__)

#: Mirrors ``spine.ledger.STATUS_PURGED``. Spelled literally rather than imported
#: because importing the spine package pulls in the whole engine (driver, agent
#: runner, PR pipeline — ~20 modules) for one string, on a request path that only
#: touches a file. The value is pinned by a test that imports the real constant.
STATUS_PURGED = "purged"

#: Mirrors ``spine.ledger.STATUS_COMMITTED``, spelled literally for the same reason as
#: :data:`STATUS_PURGED` above. Pinned against the real constant by the same test.
STATUS_COMMITTED = "committed"

#: The status a record must be in to be judged dead — nothing else claims to have
#: filed a pull request, so nothing else can be *wrong* about having filed one.
STATUS_FILED = "filed"

#: ``pr_recipe`` returns ``QUEUED:<fp>`` when it could not open a pull request but
#: the change is queued on disk. Not a real reference, but still materializable, so
#: a record carrying one is NOT dead — draft-PR can be retried against it.
_QUEUED_PREFIX = "QUEUED:"

#: A live pull/merge request URL. Same shape ``pr_watchers`` accepts, so "watchable"
#: and "real" cannot drift apart and leave a record that is dead to one and alive to
#: the other. Replaces the upstream predicate, which matched a hosted review service
#: URL and a ``CR-<digits>`` id — neither exists here.
_PR_URL_RE = re.compile(r"^https://[^\s]+/(?:pull|merge_requests)/\d+", re.IGNORECASE)

#: Fingerprint shape. Fingerprints are ``sha256[:16]``, but this is validated as an
#: ALLOWLIST rather than assumed, because an ``fp`` arrives from a URL path segment
#: and is then interpolated into a filename that :func:`purge` DELETES. No dot and no
#: separator is accepted at all, so neither ``..`` nor an absolute path nor a
#: suffix-swap can be expressed — a rejected fingerprint fails closed instead of
#: being sanitized into a different, valid one.
#:
#: Anchored with ``\Z``, not ``$``: ``$`` also matches immediately BEFORE a trailing
#: newline, so ``"abc\n"`` would pass and reach a path interpolation.
_FP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")

#: Serializes read → decide → append against the ledger file. Module-level because
#: the ledger is a process-wide singleton; the gateway runs these in worker threads.
_LEDGER_LOCK = threading.Lock()

#: Artifact families addressed by fingerprint. ``results/candidates/*`` is NOT here:
#: those files are named after the ``cand_id``, not the fingerprint, so removing them
#: would mean guessing from a target slug — and a wrong guess deletes another
#: finding's evidence. See :func:`_remove_artifacts`.
_ARTIFACT_DIRS = (store.pr_queue_dir, store.profiles_dir)


def validate_fingerprint(fp: str) -> str:
    """Return ``fp`` unchanged if it is a safe filename component, else raise.

    The shared fingerprint-shape authority for every path that interpolates an ``fp``
    into a filename. Validation happens here, at the boundary where untrusted input
    first becomes a path, rather than at each use site — and it rejects instead of
    sanitizing, because quietly rewriting a fingerprint would address a *different*
    finding's files.

    :raises ValueError: when ``fp`` is empty or not allowlist-shaped.
    """
    if not _FP_RE.match(fp or ""):
        # Deliberately terse: the message reaches an HTTP client, so it says what is
        # wrong without echoing the input or naming a path.
        raise ValueError("fingerprint is not a valid identifier")
    return fp


def is_real_pr_reference(ref: str) -> bool:
    """True when ``ref`` points at a pull/merge request that actually exists.

    A ``QUEUED:<fp>`` placeholder is not one, and neither is empty.
    """
    value = (ref or "").strip()
    if not value or value.upper().startswith(_QUEUED_PREFIX):
        return False
    return bool(_PR_URL_RE.match(value))


def pr_reference(record: dict[str, Any]) -> str:
    """The pull-request reference of a ledger record, reading either spelling."""
    return str(record.get("pr") or record.get("cr") or "").strip()


def is_dead_record(finding: dict[str, Any]) -> bool:
    """True when a record claims a filed pull request that does not exist.

    The predicate the sweep is built on, so it is deliberately narrow — only a
    ``filed`` record can be dead. Everything else is either still in flight or holds
    a real verdict, and a verdict is not garbage just because it was unwelcome.

    A ``filed`` record is dead when its reference is missing or is not a real
    pull/merge request URL. The one carve-out is ``QUEUED:<fp>``: the change is on
    disk and drafting can still be retried, so that record can make progress and is
    NOT dead. Without the carve-out the sweep would purge every locally queued change
    the moment ``gh`` was unavailable.
    """
    if str(finding.get("status") or "") != STATUS_FILED:
        return False
    ref = pr_reference(finding)
    if not ref:
        return True
    if ref.upper().startswith(_QUEUED_PREFIX):
        return False
    return not is_real_pr_reference(ref)


# ── ledger I/O ────────────────────────────────────────────────────────────────


def _load_events() -> list[dict[str, Any]]:
    """Every ledger event in file order, skipping lines that will not parse.

    Line-by-line rather than one parse of the whole file: a run can be killed
    mid-append, and a single torn line must cost that one event, never the entries
    before it. A skipped line is logged with its position — visible data loss beats
    silent data loss when the ledger is the audit trail.
    """
    path = store.ledger_path()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    lines = text.splitlines()
    events: list[dict[str, Any]] = []
    for idx, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            logger.warning("ledger: skipping unparseable line %d of %d", idx + 1, len(lines))
            continue
        if isinstance(row, dict) and row.get("fp"):
            events.append(row)
    return events


def _latest_event(events: list[dict[str, Any]], fp: str) -> dict[str, Any] | None:
    """The most recent event for ``fp``, or None when the ledger has never seen it.

    File order, not ``ts`` order: append order is the ledger's own notion of "latest"
    and every reader uses it, so a row with a missing or skewed timestamp still
    resolves the same way here as it does in the findings list.
    """
    for row in reversed(events):
        if str(row.get("fp") or "") == fp:
            return row
    return None


def latest_records() -> list[dict[str, Any]]:
    """The current state of every fingerprint — one record each, latest event wins.

    Includes already-purged records; callers that care filter on ``status``. Each
    record carries a normalized ``pr`` so consumers never have to know about ``cr``.
    """
    latest: dict[str, dict[str, Any]] = {}
    for row in _load_events():
        record = dict(row)
        record["pr"] = pr_reference(row)
        latest[str(row.get("fp"))] = record
    return list(latest.values())


def _purged_event(prior: dict[str, Any], note: str) -> dict[str, Any]:
    """Build the ``purged`` event that supersedes ``prior``.

    ``kind`` and ``target`` are carried over so the purged record is still
    identifiable in the timeline instead of becoming an anonymous fingerprint.

    ``cr`` — not ``pr`` — is written, and empty: ``spine.ledger.LedgerEntry`` is a
    fixed-field dataclass constructed as ``LedgerEntry(**row)``, so an event carrying
    an unexpected ``pr`` key raises ``TypeError`` and is swallowed by ``_load()``'s
    torn-line handler. The fingerprint would then never enter the ledger's index as
    purged, ``known()`` would keep returning True, and the locus would stay deduped —
    the purge would look like it worked and change nothing. It is cleared rather than
    preserved because the reference is precisely what is being disowned.
    """
    return {
        "fp": prior.get("fp"),
        "kind": prior.get("kind") or "",
        "target": prior.get("target") or "",
        "status": STATUS_PURGED,
        "cr": "",
        "note": note,
        "ts": time.time(),
    }


def _append_event(row: dict[str, Any]) -> None:
    """Append one event to the ledger, flushed to disk before returning.

    A plain append under ``"a"`` is atomic enough for a single line on a local
    filesystem, and it cannot lose a concurrent writer's event the way a rewrite
    would. The explicit fsync is because the caller may go on to delete files on the
    strength of this event having been recorded.
    """
    path = store.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


# ── artifact removal ──────────────────────────────────────────────────────────


def _remove_artifacts(safe_fp: str) -> list[str]:
    """Delete the fingerprint-addressed artifacts and return what was removed.

    Covers ``pr_queue/<fp>.*`` (the queued diff and PR body) and ``profiles/<fp>.*``
    (the normalized frame tree and any raw profiler dump). Each candidate is resolved
    and checked to still sit directly inside the directory it was globbed from, so a
    symlink planted in the data directory cannot redirect a delete outside it.

    ``results/candidates/*`` is deliberately untouched: those files are named after
    the ``cand_id``, which embeds the target rather than the fingerprint, so matching
    them means guessing from a slug — and two findings in the same function would
    share that slug. Deleting another finding's evidence is worse than leaving a
    stale candidate file behind.

    A per-file failure is logged and skipped rather than raised: this runs after the
    ledger event is already recorded, and one undeletable file must not present the
    whole purge as having failed.
    """
    removed: list[str] = []
    for make_dir in _ARTIFACT_DIRS:
        directory = make_dir()
        try:
            root = directory.resolve()
        except OSError:
            continue
        for path in sorted(directory.glob(f"{safe_fp}.*")):
            try:
                resolved = path.resolve()
                if resolved.parent != root or not resolved.is_file():
                    logger.warning("purge: refusing to remove %s (outside %s)", path.name, root)
                    continue
                resolved.unlink()
            except OSError:
                logger.warning("purge: could not remove %s", path.name, exc_info=True)
                continue
            removed.append(f"{directory.name}/{path.name}")
    return removed


# ── the three operations ──────────────────────────────────────────────────────


def forget(fp: str) -> dict[str, Any]:
    """Make a finding re-discoverable, leaving its artifacts in place.

    For the case where the loop reached the wrong verdict: a gate that failed for an
    environmental reason, a duplicate that was not one. Appending ``purged`` makes
    ``Ledger.known()`` report the locus as unknown, so the next run may propose it
    again. Nothing else is touched — no branches, no clones, no queued diff.

    REFUSES when the finding already has a real pull request. Forgetting it would let
    the loop rediscover the locus and open a second pull request for a change that is
    already up for review; closing the existing PR is the human's call, not a side
    effect of clearing a dedup entry.

    Returns ``{"ok", "fp", "forgotten", "reason", "detail"}``. ``reason`` is a stable
    code for the caller to map to a status: ``invalid_fingerprint``,
    ``unknown_finding``, ``has_pull_request``, or ``""`` on success.
    """
    try:
        safe_fp = validate_fingerprint(fp)
    except ValueError as exc:
        return _failure(fp, "invalid_fingerprint", str(exc), forgotten=False)

    with _LEDGER_LOCK:
        prior = _latest_event(_load_events(), safe_fp)
        if prior is None:
            return _failure(
                safe_fp,
                "unknown_finding",
                f"no finding with fingerprint {safe_fp}",
                forgotten=False,
            )
        ref = pr_reference(prior)
        if is_real_pr_reference(ref):
            return _failure(
                safe_fp,
                "has_pull_request",
                "this finding already has an open pull request — close or merge it "
                "instead of forgetting the finding",
                forgotten=False,
                pr=ref,
            )
        _append_event(_purged_event(prior, "purged (removed from findings; locus re-discoverable)"))
    logger.info("ledger: forgot %s (was %s)", safe_fp, prior.get("status") or "?")
    return {"ok": True, "fp": safe_fp, "forgotten": True, "reason": "", "detail": ""}


def purge(fp: str, *, remove_artifacts: bool = True) -> dict[str, Any]:
    """Purge a DEAD finding: mark it re-discoverable and delete its artifacts.

    Narrower than :func:`forget` on purpose. This deletes files, so it only accepts a
    record :func:`is_dead_record` judges hopeless — one claiming a filed pull request
    that does not exist. A record that is merely unwelcome keeps its evidence.

    The ledger event is appended BEFORE any file is removed. Ordered that way because
    the two failure modes are not symmetric: an event with some artifacts left over
    is a re-discoverable locus plus a stale file, while deleted artifacts with no
    event is a locus still blocked by dedup whose evidence is gone.

    Returns ``{"ok", "fp", "purged", "removed", "reason", "detail"}`` where
    ``removed`` lists the artifact paths deleted, relative to the data directory.
    ``reason`` is ``invalid_fingerprint``, ``unknown_finding``, ``not_dead``, or
    ``""``.
    """
    try:
        safe_fp = validate_fingerprint(fp)
    except ValueError as exc:
        return _failure(fp, "invalid_fingerprint", str(exc), purged=False, removed=[])

    with _LEDGER_LOCK:
        prior = _latest_event(_load_events(), safe_fp)
        if prior is None:
            return _failure(
                safe_fp,
                "unknown_finding",
                f"no finding with fingerprint {safe_fp}",
                purged=False,
                removed=[],
            )
        if not is_dead_record(prior):
            return _failure(
                safe_fp,
                "not_dead",
                "not a dead record — it holds a real or still-materializable pull "
                "request reference, so purging it would discard live work",
                purged=False,
                removed=[],
                pr=pr_reference(prior),
            )
        _append_event(_purged_event(prior, "purged (dead record; artifacts removed)"))

    # Outside the lock: this touches artifact files, not the ledger, and an unlink
    # storm must not hold off a concurrent forget.
    removed = _remove_artifacts(safe_fp) if remove_artifacts else []
    logger.info("ledger: purged %s (%d artifact(s) removed)", safe_fp, len(removed))
    return {
        "ok": True,
        "fp": safe_fp,
        "purged": True,
        "removed": removed,
        "reason": "",
        "detail": "",
    }


def purge_dead(*, remove_artifacts: bool = False) -> dict[str, Any]:
    """Sweep every record that can no longer make progress.

    Housekeeping for the findings list: a run interrupted between "filed" and the
    reference being recorded leaves records that claim a pull request nobody can
    open, and each one keeps its locus deduped forever. The sweep marks them
    re-discoverable in one pass.

    Artifacts are KEPT by default, unlike a single :func:`purge`. A dead record is one
    whose pull request was never created, which makes the queued diff the only
    surviving copy of that change — a bulk sweep is the wrong place to discard it.
    Removing one finding's artifacts is an explicit per-finding decision; pass
    ``remove_artifacts=True`` to opt the sweep in.

    Returns ``{"ok": True, "purged": [<fp>, ...], "count": n}``, in ledger order.
    """
    purged: list[str] = []
    for record in latest_records():
        if not is_dead_record(record):
            continue
        fp = str(record.get("fp") or "")
        result = purge(fp, remove_artifacts=remove_artifacts)
        if result.get("ok"):
            purged.append(fp)
        else:
            # Reachable when a fingerprint predates the shape rule, or when a
            # concurrent writer moved the record on between the read and the purge.
            logger.info("ledger: skipped %s during sweep (%s)", fp, result.get("reason"))
    return {"ok": True, "purged": purged, "count": len(purged)}


def _failure(fp: str, reason: str, detail: str, **extra: Any) -> dict[str, Any]:
    """One shape for every refusal, so callers never branch on which one it was."""
    return {"ok": False, "fp": fp, "reason": reason, "detail": detail, "error": detail, **extra}


def record_committed(fp: str, *, branch: str, sha: str) -> bool:
    """Append a ``committed`` event for a fingerprint the operator just landed.

    One-click commit pushes the queued diff and returns the sha, but wrote nothing to
    the ledger — so the record stayed ``filed``. The ledger is last-write-wins per
    fingerprint, and ``filed`` is what ``filed_crs()`` feeds the pull-request watchers,
    so a landed change kept being reported as an open PR and the UI kept offering the
    commit button for work already on the branch. The loop's own direct-commit path
    records this row; the manual path has to as well or the two disagree about the same
    outcome. Raised by the GPT review of this branch.

    Returns True iff the event was written. Failure is reported, never raised: the
    commit itself already succeeded and is on the branch, so a bookkeeping problem
    must not turn a landed change into an error the operator might retry.

    Writes ``cr`` (never ``pr``) for the reason spelled out in :func:`_purged_event`:
    ``LedgerEntry(**row)`` is a fixed-field dataclass, so an unexpected key raises
    ``TypeError`` inside ``_load()``'s torn-line handler and the event vanishes.
    """
    try:
        safe_fp = validate_fingerprint(fp)
    except ValueError:
        # `validate_fingerprint` RAISES on a bad shape rather than returning falsy.
        logger.warning("ledger: refusing to record a commit for a malformed fingerprint")
        return False
    prior = {}
    for record in latest_records():
        if str(record.get("fp")) == safe_fp:
            prior = record
            break
    try:
        _append_event(
            {
                "fp": safe_fp,
                # Carried over so the row stays identifiable rather than becoming an
                # anonymous fingerprint in the timeline (as in `_purged_event`).
                "kind": prior.get("kind") or "",
                "target": prior.get("target") or "",
                "status": STATUS_COMMITTED,
                "cr": sha,
                "note": f"committed to {branch} ({sha})"[:200],
                "ts": time.time(),
            }
        )
    except OSError as exc:
        logger.error("ledger: could not record the commit of %s: %s", safe_fp, exc)
        return False
    return True
