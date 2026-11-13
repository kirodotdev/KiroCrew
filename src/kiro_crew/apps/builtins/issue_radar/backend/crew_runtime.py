"""Crew runtime — the session a crew lives in, the brief it carries, the nudge that
drives every turn, and the zero-LLM change detector that wakes it.

Four things live here, in this module and the ``crew_runtime_*`` owners it serves
from:

1. **Session launch/attach** (:func:`ensure_crew_session`, :func:`launch_crew`).
   A crew is an app-owned dashboard slot keyed ``crew-<id>``, which is the only
   worker shape without a wall-clock ceiling shorter than one issue's lifecycle.
   ``auto_research``'s campaign worker is the working precedent for the session
   shape, including the parts that look redundant: the explicit title with
   ``_titled = True`` (the loop's messages never trigger the auto-titler), and
   re-deriving the crew's auto-approve grant from the watchdog every cycle because
   the grant is in-memory only and a gateway restart drops it. The grant itself
   follows the TASK RUNNER instead: a scoped, expiring, SEL-audited grant held
   through the App Kit's worker auto-approve seam (:func:`autoapprove_scope`), never
   the unbounded ``slot._trust`` flag a human clicks.

2. **Brief injection by presence check** (:func:`brief_is_present`). Not a
   schedule, not a turn counter — one rule that covers session start,
   post-compaction, gateway restart and any truncation mechanism nobody has
   written yet.

3. **Nudge composition** (:func:`compose_nudge`). A volatile snapshot the crew
   must not guess at, plus the brief's ``Never`` list compressed to ~80 words.

4. **The sweep** (:func:`sweep_repo`), driven from ``watch.py``'s poll loop — the
   only always-on loop in this app. Zero LLM, zero credits: it compares the six
   unblock signals against a stored fingerprint and wakes the owning crew when one
   moves.

Nothing here writes to the forge, and nothing here writes a work item: a change
detector that touched ``last_progress_at`` would renew the very claim TTL the
protocol measures from it. The one ledger write it makes is a human ANSWER to a
needs-human pass (:func:`detect_human_answers`), which patches no work item. Its own
state lives in ``crew-signals.json``, ``crew-answer-checks.json`` and
``crew-wake-checks.json`` (new work and mentions, :func:`detect_new_work`), beside
the crews dir rather than inside it — ``crew_store.list_crews`` globs ``*.json``
there, so a state file dropped in that directory would read back as a phantom crew.
"""

from __future__ import annotations

import asyncio
import importlib as _importlib
import json
import logging
import sys as _sys
import time
from collections.abc import Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew import _facade_mirror

# Bound here as well as in its owner: a test patches it through this module with
# create=True, which only a name the module binds itself survives.
from kiro_crew.apps.manager import is_app_enabled  # noqa: F401
from kiro_crew.atomic_write import atomic_write, read_json_or

from . import crew_answer_store as _answer_store
from . import crew_store
from . import crew_wake_inflight as _inflight
from . import github_client, gitlab_queries, provider, store
from .crew_mention_scan import (  # noqa: F401 -- re-exported for callers and tests
    MAX_MENTION_THREADS_PER_SWEEP,
    MAX_TIED_COMMENT_PAGES,
    REPO_COMMENTS_PAGE,
    _recent_comments,
    _second_before,
    _thread_key,
)
from .crew_runtime_session import is_live
from .crew_runtime_supervision import wake_crew, watchdog_cycle
from .mention_match import mentions

# This module is the facade of Issue Radar's crew runtime and keeps the sweep itself (the unblock
# signals and :func:`sweep_repo`), and every name is reached through it as
# ``kiro_crew.apps.builtins.issue_radar.backend.crew_runtime.<name>``: a read is served by the
# owner that binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches
# every owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. New
# work goes to the owner of its responsibility:
#   ``crew_runtime_prompt`` -- the brief, the nudge and the turn prompt
#   ``crew_runtime_session`` -- the crew's slot, minting its grant, launch and turn dispatch
#   ``crew_runtime_supervision`` -- trust reconciliation, wake, watchdog, hooks, revocation


logger = logging.getLogger("kirocrew.app.issue-radar")


# ── unblock signals ─────────────────────────────────────────────────────────
#
# The seven signals from the brief's table, and the field each one is read from:
#
#   requester replied      issue comment count            (issue detail)
#   CI state changed       check rollup + bucket counts   (batched enrichment)
#   approved / changes req review verdict                 (PR timeline, gated)
#   merge conflict         mergeable / merge state        (batched enrichment)
#   PR merged              merged flag                    (PR detail)
#   post-merge comment     PR comment count while merged  (PR detail)
#   dependency unblocked   still-open blocker count       (deps cache)
#
# Missing one means an item silently stalls forever, which is why they are named
# individually here rather than collapsed into "something changed".

SIG_REPLY = "requester-replied"
SIG_CI = "ci-changed"
SIG_REVIEW = "review-verdict"
SIG_CONFLICT = "merge-conflict"
SIG_MERGED = "pr-merged"
SIG_POST_MERGE = "post-merge-comment"
# Fires ONCE when a work item's blocker set — the deps-cache edges pointing at
# this item — transitions from "some blocker still open" to "all blockers
# closed/merged". Read from the deps cache the sweep already loads for the repo,
# so it adds NO forge call and NO second polling loop: the item is only re-read
# on its normal phase cadence, and the blocker count rides along in its
# fingerprint.
SIG_DEP_UNBLOCKED = "dependency-unblocked"

UNBLOCK_SIGNALS = (
    SIG_REPLY,
    SIG_CI,
    SIG_REVIEW,
    SIG_CONFLICT,
    SIG_MERGED,
    SIG_POST_MERGE,
    SIG_DEP_UNBLOCKED,
)

SIGNALS_SCHEMA = 1

#: How often an item in each phase is re-read. The forge is the mover for some
#: phases and the crew is the mover for others, and paying a CI-grade cadence for
#: a phase waiting on a human is pure rate limit. ``selected`` is pre-claim and
#: purely local — there is nothing public to watch yet.
RECHECK_SEC = {
    "awaiting-ci": 60,
    "addressing-review": 60,
    "awaiting-merge": 120,
    "claimed": 300,
    "investigating": 300,
    "implementing": 300,
    "awaiting-reply": 300,
}
_DEFAULT_RECHECK_SEC = 300

#: Phases where a fresh review verdict is worth one extra (paginated) timeline
#: read. Every other signal is already summarized on the issue/PR object; a review
#: is not, and a bodyless approval moves no counter at all.
_REVIEW_PHASES = frozenset({"awaiting-ci", "addressing-review", "awaiting-merge"})


def signals_path(owner: str, repo: str, root: Path | None = None) -> Path:
    """Where the fingerprints live — NOT inside ``crews/`` (see the module docstring)."""
    return store.repo_data_dir(owner, repo, root) / "crew-signals.json"


def read_signals(owner: str, repo: str, root: Path | None = None) -> dict[str, Any]:
    path = signals_path(owner, repo, root)
    if not path.is_file():
        return {}
    doc = read_json_or(path, None, logger=logger, what="issue signals")
    if not isinstance(doc, dict) or doc.get("schema") != SIGNALS_SCHEMA:
        # A schema mismatch is a cache miss here, exactly as it is for the issue
        # caches: the next sweep re-seeds and reports nothing, which loses at most
        # one cycle of latency and cannot report a stale signal.
        return {}
    items = doc.get("items")
    return items if isinstance(items, dict) else {}


def write_signals(owner: str, repo: str, items: dict[str, Any], root: Path | None = None) -> None:
    atomic_write(
        signals_path(owner, repo, root),
        json.dumps({"schema": SIGNALS_SCHEMA, "items": items}, indent=2),
    )


def _item_key(crew_id: str, number: Any) -> str:
    return f"{crew_id}:{number}"


def detect_unblocks(prev: dict[str, Any] | None, cur: dict[str, Any]) -> list[str]:
    """Which of the seven signals moved between two fingerprints.

    A FIRST observation reports nothing: it seeds the mark, the same discipline the
    new-issue watcher uses so connecting a repo does not announce its whole
    backlog. Here the equivalent mistake would wake every crew on every item the
    moment the gateway restarts.
    """
    if not prev:
        return []
    out: list[str] = []
    if _as_int(cur.get("issue_comments")) > _as_int(prev.get("issue_comments")):
        out.append(SIG_REPLY)
    if (cur.get("checks"), cur.get("check_counts")) != (
        prev.get("checks"),
        prev.get("check_counts"),
    ):
        # Only when the current reading is a real one — a failed enrichment call
        # reports None, and None-vs-known must not masquerade as "CI moved".
        if cur.get("checks") is not None:
            out.append(SIG_CI)
    verdict = cur.get("review_decision") or ""
    if verdict and verdict != (prev.get("review_decision") or ""):
        out.append(SIG_REVIEW)
    if cur.get("conflicted") and not prev.get("conflicted"):
        out.append(SIG_CONFLICT)
    if cur.get("merged") and not prev.get("merged"):
        out.append(SIG_MERGED)
    if cur.get("merged") and _as_int(cur.get("pr_comments")) > _as_int(prev.get("pr_comments")):
        out.append(SIG_POST_MERGE)
    # Dependency unblocked: the blocker set went from some-still-open to
    # all-closed. Both readings must be KNOWN (an integer): a None means the deps
    # cache could not be read this cycle, and None-vs-known must not read as
    # "unblocked" — the same guard the CI signal uses. An item with no blockers
    # sits at 0 in both readings, so it never fires; only a real >0 → 0 does.
    prev_open = prev.get("open_blockers")
    cur_open = cur.get("open_blockers")
    if isinstance(prev_open, int) and isinstance(cur_open, int) and prev_open > 0 and cur_open == 0:
        out.append(SIG_DEP_UNBLOCKED)
    return out


def _as_int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _is_conflicted(mergeable: Any, merge_state: Any) -> bool:
    """A REAL conflict, not an uncomputed one.

    GitHub answers ``mergeable: null`` / ``mergeable_state: "unknown"`` on a cold
    read and computes the merge commit in the background, so truthiness cannot be
    used here: ``not mergeable`` would report every cold read as a conflict and
    wake the crew to resolve one that does not exist.
    """
    if mergeable is False:
        return True
    return str(merge_state or "").lower() == "dirty"


#: When a GitLab sweep refresh last failed, per repo and scope (monotonic seconds).
#: A failed GitLab walk costs up to ``gitlab_queries._MAX_LINKS_CALLS`` calls and
#: writes no cache, so without this stamp every watch tick would start the whole
#: walk again against a host that is already failing.
_GITLAB_DEPS_FAILED_AT: dict[tuple[str, str, str, str], float] = {}


def _gitlab_deps_key(key: provider.RepoKey, scope) -> tuple[str, str, str, str]:
    return (str(key.host or ""), key.owner, key.repo, str(scope or ""))


def _read_or_refresh_deps(key: provider.RepoKey, scope) -> dict[str, Any] | None:
    """The sweep's deps source: cache read, refreshed in place when absent/expired.

    Runs in a worker thread (the caller wraps it in ``asyncio.to_thread``).
    GitHub and GitLab refresh, dispatching exactly as the /deps route does; any
    other provider has no dependency fetch yet, and for it this stays a plain
    cache read (normally None). A refresh failure of any kind keeps whatever the
    cache held — stale beats wrong, and an unknown reading can never fire the
    unlock (see ``detect_unblocks``). A failed GitLab refresh is not tried again
    for ``store.DEPS_CACHE_TTL_SEC`` (see ``_GITLAB_DEPS_FAILED_AT``), the same wait
    a successful one earns. A GitLab graph can be edgeless for an issue
    whose own ``/links`` were never read (fetch cap, or a failed call); the fetcher
    either marks its node ``blockers_unknown`` or leaves it out of ``nodes``, and
    ``_open_blocker_count`` reads both as unknown for GitLab, so that edgeless
    shape never fires a false 1→0 unlock either.
    """
    cached = store.read_deps_cache(key.owner, key.repo, scope)
    fresh_enough = (
        isinstance(cached, dict)
        and (time.time() - float(cached.get("fetched_at") or 0)) < store.DEPS_CACHE_TTL_SEC
    )
    if fresh_enough or key.provider not in (provider.GITHUB, provider.GITLAB):
        return cached
    failed_at = _GITLAB_DEPS_FAILED_AT.get(_gitlab_deps_key(key, scope))
    if (
        key.provider == provider.GITLAB
        and failed_at is not None
        and time.monotonic() - failed_at < store.DEPS_CACHE_TTL_SEC
    ):
        return cached

    # An ABSENT issues cache is unknown scope, not an empty repo: building the
    # graph from it would overwrite a possibly-good cached graph with a
    # wrong-empty one (the same unknown-vs-empty distinction the /deps route
    # makes). Keep whatever we have; the sweep after the issues cache warms
    # will refresh.
    # Captured BEFORE the issues snapshot is read — the snapshot is the graph's
    # SCOPE, so the rebuilt graph is as old as its oldest input, not as old as
    # the edge fetch alone (same reasoning as the /deps route). Under-claiming
    # age is the safe direction for the store's compare-and-set.
    fetch_started = time.time()
    open_issues = store.read_issues_cache(key.owner, key.repo, scope, state="open")
    if open_issues is None:
        return cached
    try:
        if key.provider == provider.GITLAB:
            edges, nodes = gitlab_queries.fetch_dependency_edges(
                key.owner, key.repo, open_issues, host=key.host
            )
        else:
            hints: dict[int, dict] = {}
            edges, nodes = github_client.fetch_dependency_edges(
                key.owner, key.repo, open_issues, hints
            )
        store.write_deps_cache(
            key.owner, key.repo, edges, nodes, root=scope, fetched_at=fetch_started
        )
        _GITLAB_DEPS_FAILED_AT.pop(_gitlab_deps_key(key, scope), None)
        return store.read_deps_cache(key.owner, key.repo, scope)
    except Exception:
        if key.provider == provider.GITLAB:
            _GITLAB_DEPS_FAILED_AT[_gitlab_deps_key(key, scope)] = time.monotonic()
        logger.debug(
            "issue-radar: sweep deps refresh failed for %s/%s; keeping cached graph",
            key.owner,
            key.repo,
            exc_info=True,
        )
        return cached


def _open_blocker_count(
    deps: dict[str, Any] | None, number: Any, *, absent_is_unknown: bool = False
) -> int | None:
    """How many of ``number``'s blockers are still open, from a deps-cache graph.

    ``deps`` is a ``store.read_deps_cache`` result (``{"edges", "nodes", ...}``) or
    None. Returns:
      * ``None`` when the graph is unknown (no deps cache) — an unknown count must
        never read as "unblocked" (see ``detect_unblocks``);
      * ``0`` when the item has no blockers at all, or all of them are
        closed/merged;
      * the count of blockers whose node state is neither ``closed`` nor
        ``merged``.
    A blocker with no node entry is treated as still-open (conservative: better to
    keep an item blocked than to fire a spurious unlock on a missing node). An
    item whose own node carries ``blockers_unknown`` (GitLab: its ``/links`` were
    never read, so its edges may be missing) is unknown too — its edge count says
    nothing about its real blockers.

    ``absent_is_unknown`` makes an item with NO node of its own read as unknown.
    The GitLab fetcher leaves an open issue out of ``nodes`` exactly when it never
    read that issue's ``/links`` (past ``_MAX_LINKS_CALLS`` or ``_MAX_DEPS_NODES``
    with no fetched issue linking to it, or a failed call no other edge touched),
    so there an absent node is an unread one, not an unblocked one. The GitHub
    fetcher seeds and walks every open issue in the snapshot, so it keeps the
    default.
    """
    if not isinstance(deps, dict):
        return None
    if not isinstance(number, int) or number <= 0:
        return None
    edges = deps.get("edges")
    raw_nodes = deps.get("nodes")
    nodes: dict[str, Any] = raw_nodes if isinstance(raw_nodes, dict) else {}
    if not isinstance(edges, list):
        return None
    own = nodes.get(str(number))
    if own is None and absent_is_unknown:
        return None
    if isinstance(own, dict) and own.get("blockers_unknown") is True:
        return None
    open_count = 0
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("blocked") != number:
            continue
        blocker = edge.get("blocker")
        node = nodes.get(str(blocker)) if blocker is not None else None
        state = str((node or {}).get("state") or "open").lower()
        if state not in ("closed", "merged"):
            open_count += 1
    return open_count


def fingerprint_item(
    key: provider.RepoKey,
    item: dict[str, Any],
    enriched: dict[int, dict[str, Any]],
    prev: dict[str, Any] | None,
    deps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read the seven signals for ONE work item. Blocking ``gh`` calls — run off-loop.

    Cost is one REST call for the issue, one more for the PR when the item has one,
    and (only when the PR's ``updated_at`` moved and the phase makes a review
    plausible) one paginated timeline read. The check rollup and merge state come
    from ``enriched``, which the caller fetched for the whole repo in two batched
    GraphQL calls. The blocker count comes from ``deps`` (the repo's deps cache,
    read ONCE by the caller), so the seventh signal costs no extra forge call.
    """
    client = provider.client_for(key)
    kwargs = provider.call_kwargs(key)
    number = item.get("number")
    fp: dict[str, Any] = {"phase": item.get("phase") or ""}
    if not isinstance(number, int) or number <= 0:
        fp["open_blockers"] = None  # no forge-readable number: nothing to count
        return fp  # a record with no issue number has nothing on the forge to read

    issue = client.get_issue_detail(key.owner, key.repo, int(number), **kwargs)
    # The blocker count is read from the deps cache regardless of whether the
    # item has a PR — an issue can be blocked too. But the graph is scoped to
    # OPEN issues: a tracked item that is itself closed has NO edges in the
    # graph, and "no edges" would read as zero open blockers — a prev>0 → 0
    # transition that falsely fires SIG_DEP_UNBLOCKED the moment someone closes
    # a nonterminal item. Out of scope must read UNKNOWN, not unblocked.
    item_state = str((issue or {}).get("state") or "").lower() if isinstance(issue, dict) else ""
    open_blockers = (
        _open_blocker_count(deps, number, absent_is_unknown=key.provider == provider.GITLAB)
        if item_state == "open"
        else None
    )
    # An unknown reading on a still-open item keeps the last known count. The
    # sweep stores this fingerprint as next cycle's ``prev``, so writing None
    # here would erase the count: a partial GitLab refresh between a blocker's
    # 1 and its 0 turns 1 → 0 into 1 → None → 0, and the unlock never fires.
    # The carried value equals ``prev``, so it cannot fire anything itself.
    if open_blockers is None and item_state == "open" and prev is not None:
        last = prev.get("open_blockers")
        if isinstance(last, int) and not isinstance(last, bool):
            open_blockers = last
    fp["open_blockers"] = open_blockers
    if isinstance(issue, dict):
        fp["issue_comments"] = _as_int(issue.get("comments"))
        fp["issue_state"] = issue.get("state")

    pr = item.get("pr_number")
    if not isinstance(pr, int) or pr <= 0:
        return fp

    fp["pr_number"] = pr
    row = enriched.get(pr) or {}
    fp["checks"] = row.get("checks_state")
    counts = row.get("checks_counts")
    fp["check_counts"] = counts if isinstance(counts, dict) else None

    # ``resolve_mergeable=False``: the batched readiness call above already supplies
    # a COMPUTED merge state, so paying this call's retry-plus-sleep for the lazy
    # one would buy a second answer to a question already answered.
    detail = client.get_pr_detail(key.owner, key.repo, pr, resolve_mergeable=False, **kwargs)
    detail = detail if isinstance(detail, dict) else {}
    fp["pr_comments"] = _as_int(detail.get("comments"))
    fp["pr_updated_at"] = detail.get("updated_at")
    fp["merged"] = bool(detail.get("merged") or detail.get("merged_at"))
    mergeable = (
        row.get("mergeable") if row.get("mergeable") is not None else detail.get("mergeable")
    )
    merge_state = row.get("mergeable_state") or detail.get("mergeable_state")
    fp["conflicted"] = _is_conflicted(mergeable, merge_state)
    fp["merge_state"] = merge_state

    # Carry the previous verdict forward when the read is skipped, so a skipped
    # cycle reads as "unchanged" instead of as a verdict being withdrawn.
    fp["review_decision"] = (prev or {}).get("review_decision") or ""
    moved = prev is not None and detail.get("updated_at") != prev.get("pr_updated_at")
    if not fp["merged"] and fp["phase"] in _REVIEW_PHASES and (prev is None or moved):
        fp["review_decision"] = _latest_review_decision(client, key, pr, kwargs)
    return fp


def _latest_review_decision(
    client: Any, key: provider.RepoKey, pr: int, kwargs: dict[str, str]
) -> str:
    """``approved`` / ``changes_requested`` / ``""`` from the PR's newest review.

    The one signal REST does not summarize anywhere on the PR object: an approval
    with no body increments no counter, and ``mergeable_state`` reports both "no
    review yet" and "changes requested" as ``blocked``. So it is read from the
    timeline, and the read is gated on a cheap field having moved first.
    """
    try:
        events = client.list_issue_timeline(key.owner, key.repo, pr, **kwargs)
    except Exception:
        logger.debug("issue-radar: review read failed for #%s", pr, exc_info=True)
        return ""
    latest = ""
    for ev in events or []:
        if not isinstance(ev, dict) or ev.get("kind") != "reviewed":
            continue
        state = str(ev.get("review_state") or "").lower()
        if state in ("approved", "changes_requested"):
            latest = state  # timeline is chronological; the last one wins
    return latest


# ── human answers to a needs-human pass ─────────────────────────────────────
#
# A crew that needs a human records a pass with an answerable scope
# (``crew_store.ANSWERABLE_SKIP_SCOPES``: ``needs-decision`` and
# ``needs-investigation``; scope-gate passes stay skipped) and labels the issue with
# the repo's needs-human label.
# The pass keeps every crew off the issue, so without this nothing ever hands it
# back. The hand-back is the LABEL: a human removing it on an open issue says
# "answered, take it again". A comment alone is not a hand-back -- people comment
# without deciding -- and a removal the crew made is not an answer. A removal under
# the gateway's own forge login never counts, even when a person made it: the forge
# cannot tell the two apart (:func:`find_human_answer`).
#
# Cost, per sweep: the forge's first page of open issues (``list_open_issues_first_page``,
# newest update first, labels included; when that read fails the sweep reads nothing
# and the next one retries), zero further calls for a number whose issue still
# carries the label, and ONE timeline read for a qualifying number the first time it
# is seen without the label. A read that finds no human removal is remembered in
# ``crew-answer-checks.json`` against that pass AND the issue's ``updated_at``, so a
# pass that simply lost its label is not re-read every minute, while any later
# activity on the issue re-arms the read. A timeline with no trace of the label is
# re-read after ``INCOMPLETE_READ_RETRY_SECS`` too (it may be a failed fetch). A
# candidate off the first page is read directly (``get_issue_detail``), from the
# same budget, at most once per that window, least recently read first.

SIG_HUMAN_ANSWERED = "human-answered"

ANSWER_CHECKS_SCHEMA = 1

#: Label-event reads one sweep may spend. A repo that just had a dozen questions
#: answered gets them over a few cycles instead of one burst.
MAX_ANSWER_READS_PER_SWEEP = 10

#: How long a mark from a timeline with no trace of the label holds while the issue
#: is unchanged. Such a read may be a failed fetch the provider returned as empty, so
#: it is tried again, but not every sweep: an old pass that never had the label
#: would otherwise spend the read budget every minute and starve real answers.
INCOMPLETE_READ_RETRY_SECS = 3600.0


def _read_updated_open_issues(
    key: provider.RepoKey,
) -> list[dict[str, Any]] | None:
    """The forge's first page of open issues, newest UPDATE first, kept to the fields
    the sweep reads: number, labels and ``updated_at``.

    This is the same ``list_open_issues_first_page`` the dashboard's first paint uses,
    on every provider. Removing a label moves an issue's update time, so an issue a
    person just handed back is at the top of it. A number missing from the page is
    UNKNOWN, not closed: the page may stop before an older issue. ``None`` when the
    read failed: unknown is not "no issues".
    """
    try:
        rows = provider.client_for(key).list_open_issues_first_page(
            key.owner, key.repo, **provider.call_kwargs(key)
        )
    except Exception:
        logger.debug("issue-radar: open-issues read for human answers failed", exc_info=True)
        return None
    return [
        {
            "number": r.get("number"),
            "labels": r.get("labels") or [],
            "updated_at": str(r.get("updated_at") or "")[:40],
        }
        for r in rows or []
        if isinstance(r, dict)
    ]


def answer_checks_path(owner: str, repo: str, root: Path | None = None) -> Path:
    """Beside ``crew-signals.json`` and for the same reason: not inside ``crews/``.
    The file also holds the human answers themselves (``crew_answer_store``)."""
    return _answer_store.path(owner, repo, root)


def read_answer_checks(owner: str, repo: str, root: Path | None = None) -> dict[str, Any]:
    return dict(_answer_store.read(owner, repo, root)["items"])


def write_answer_checks(
    owner: str, repo: str, items: dict[str, Any], root: Path | None = None
) -> None:
    """Replace the check marks, keeping the answers the same file holds."""
    _answer_store.update(owner, repo, root, lambda doc: doc.__setitem__("items", items))


def _forge_identity(key: provider.RepoKey) -> str:
    """The crew's forge login, or ``""`` when it cannot be read (then nothing fires).

    Read afresh on every sweep that needs it: the operator can switch the CLI login
    while the gateway runs, and a remembered login would then attribute the crew's own
    removals to someone else.
    """
    try:
        client = provider.client_for(key)
        # Azure caches the identity for the life of the process; its uncached read
        # sees a login switched mid-run. The other clients read afresh every call.
        uncached = getattr(client, "get_current_login_uncached", None)
        if uncached is not None:
            # The repo's own organization, not the CLI default, which a repo
            # connected by URL may not have.
            login = uncached(owner=key.owner, **provider.call_kwargs(key))
        else:
            login = client.get_current_login(**provider.call_kwargs(key))
    except Exception:
        logger.debug("issue-radar: forge identity read failed", exc_info=True)
        return ""
    return str(login or "").strip()


def _label_names(issue: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, Mapping) else label
        if isinstance(name, str) and name:
            out.add(name.casefold())
    return out


def _list_label_events(key: provider.RepoKey, number: int) -> list[dict[str, Any]]:
    """The label additions and removals on one issue, read from its timeline."""
    client = provider.client_for(key)
    rows = client.list_issue_timeline(key.owner, key.repo, number, **provider.call_kwargs(key))
    return [
        r for r in rows or [] if isinstance(r, dict) and r.get("kind") in ("labeled", "unlabeled")
    ]


def _answer_clock() -> float:
    """Wall time for answer-check marks; its own binding so a test moves only it."""
    return time.time()


def _answer_scan_order(
    candidates: Mapping[str, Any], on_page: Mapping[str, Any], checks: Mapping[str, Any]
) -> list[tuple[str, Any]]:
    """Candidates on the forge's first page first, by number; then the ones off it,
    least recently probed first, so a fixed budget rotates through all of them."""

    def probed(k: str) -> float:
        mark = checks.get(k)
        try:
            return float(mark.get("probed_at") or 0.0) if isinstance(mark, dict) else 0.0
        except (TypeError, ValueError):
            return 0.0

    first = sorted((kv for kv in candidates.items() if kv[0] in on_page), key=lambda kv: int(kv[0]))
    rest = sorted(
        (kv for kv in candidates.items() if kv[0] not in on_page),
        key=lambda kv: (probed(kv[0]), int(kv[0])),
    )
    return [*first, *rest]


def _probe_holds(mark: Mapping[str, Any]) -> bool:
    """Whether an off-page candidate was read within the retry window (a mark from
    the future, after the clock stepped back, has lapsed)."""
    try:
        probed_at = float(mark.get("probed_at") or 0.0)
    except (TypeError, ValueError):
        return False
    if not probed_at:
        return False
    return 0.0 <= _answer_clock() - probed_at < INCOMPLETE_READ_RETRY_SECS


def _read_one_issue(key: provider.RepoKey, number: int) -> dict[str, Any] | None:
    """One issue's state, labels and ``updated_at``, read directly, for a candidate
    off the first page; ``None`` when the read fails (retried after the window)."""
    try:
        row = provider.client_for(key).get_issue_detail(
            key.owner, key.repo, number, **provider.call_kwargs(key)
        )
    except Exception:
        logger.debug("issue-radar: issue read for #%s failed", number, exc_info=True)
        return None
    if not isinstance(row, dict):
        return None
    return {
        "number": row.get("number"),
        "state": str(row.get("state") or "").lower(),
        "labels": row.get("labels") or [],
        "updated_at": str(row.get("updated_at") or "")[:40],
    }


def _mark_holds(mark: Mapping[str, Any]) -> bool:
    """Whether a "no answer" mark still stops a read of the unchanged issue: always,
    unless it came from an incomplete timeline older than the retry window."""
    if not mark.get("incomplete"):
        return True
    try:
        checked_at = float(mark.get("checked_at") or 0.0)
    except (TypeError, ValueError):
        return False
    # A mark from the future (the clock stepped back) has lapsed, not held.
    return 0.0 <= _answer_clock() - checked_at < INCOMPLETE_READ_RETRY_SECS


def _ends_with_removal(events: Sequence[Mapping[str, Any]], label: str, since: str) -> bool:
    """Whether the newest addition or removal of *label* after *since* is a removal.

    Called only for an issue the forge list shows without the label, so a timeline
    with no removal after the pass's cutoff (empty, ending in an addition, or ending
    in a removal from an earlier cycle) is behind the list, and its "no answer" must
    not be kept as final.
    """
    wanted = label.casefold()
    newest: tuple[str, str] | None = None
    for ev in events:
        kind = ev.get("kind")
        if kind not in ("labeled", "unlabeled"):
            continue
        name = (ev.get("label") or {}).get("name") if isinstance(ev.get("label"), Mapping) else None
        if not isinstance(name, str) or name.casefold() != wanted:
            continue
        at = crew_store.normalize_forge_time(ev.get("created_at"))
        if not at or at <= since:
            continue
        if newest is None or at >= newest[1]:
            newest = (str(kind), at)
    return newest is not None and newest[0] == "unlabeled"


def find_human_answer(
    events: Sequence[Mapping[str, Any]], label: str, since: str, identity: str
) -> dict[str, str] | None:
    """The newest removal of *label* after *since*, when someone other than *identity*
    made it.

    *since* is the pass's ``decided_at`` in the fold's spelling; event times are
    normalized to the same spelling before comparing. ``None`` when there is none,
    including when *identity* is unknown (fail closed: a removal nobody can attribute
    must not read as a human's).

    *identity* is the login the crew acts through, so a removal under it is never an
    answer, even when a person made it: the forge cannot tell the two apart, and
    counting it would let a crew hand an issue back to itself. An operator who shares
    that login answers from another account.
    """
    if not identity:
        return None
    # Only the removal that left the label off counts: an earlier human removal the
    # label was put back after is superseded, and must not let the crew's own later
    # removal hand the issue back.
    # The actor is checked only after the newest removal is chosen, so one with no
    # actor is not skipped in favour of an older, attributed one. Timelines arrive
    # oldest first, so on a tie in time the later row wins (``>=``). An addition
    # counts too: the label put back after the removal means the hold stands.
    wanted = label.casefold()
    latest: tuple[str, str, str] | None = None
    for ev in events:
        kind = ev.get("kind")
        if kind not in ("labeled", "unlabeled"):
            continue
        name = (ev.get("label") or {}).get("name") if isinstance(ev.get("label"), Mapping) else None
        if not isinstance(name, str) or name.casefold() != wanted:
            continue
        at = crew_store.normalize_forge_time(ev.get("created_at"))
        if not at or at <= since:
            continue
        if latest is None or at >= latest[2]:
            latest = (str(kind), str(ev.get("actor") or "").strip(), at)
    if latest is None or latest[0] != "unlabeled":
        return None
    if not latest[1] or latest[1].casefold() == identity.casefold():
        return None
    return {"actor": latest[1], "at": latest[2]}


def _removals_after(
    events: Sequence[Mapping[str, Any]], label: str, since: str
) -> list[tuple[str, str]]:
    """Every ``(actor, at)`` removal of *label* after *since* -- the one scan both
    the answer and the own-login warning read."""
    wanted = label.casefold()
    out: list[tuple[str, str]] = []
    for ev in events:
        if ev.get("kind") != "unlabeled":
            continue
        name = (ev.get("label") or {}).get("name") if isinstance(ev.get("label"), Mapping) else None
        if not isinstance(name, str) or name.casefold() != wanted:
            continue
        actor = str(ev.get("actor") or "").strip()
        at = crew_store.normalize_forge_time(ev.get("created_at"))
        if actor and at and at > since:
            out.append((actor, at))
    return out


async def detect_human_answers(
    state: Any,
    key: provider.RepoKey,
    live: list[dict[str, Any]],
    scope: Path | None,
) -> dict[str, list[tuple[int, str]]]:
    """Find needs-human passes a human handed back; record each and say whom to wake.

    Returns ``{crew_id: [(number, actor), ...]}``, *actor* being the forge user who
    removed the label. A number qualifies when its standing pass is answerable, its
    issue is OPEN, the needs-human label is absent from it (read from the forge, newest
    update first; when that read fails nothing is read and the next sweep retries),
    and the label's newest removal after the pass was made under a login other than
    the gateway's own (:func:`find_human_answer`).

    The answer is written into the app's own ``crew-answer-checks.json``
    (``crew_answer_store``), not the crew log. The crew woken is the one that recorded the pass when it is
    live, else the first live crew by id. An answer that cannot be written wakes
    nobody and is retried next sweep.
    """
    if not live:
        return {}
    owner, repo = key.owner, key.repo
    settings = await asyncio.to_thread(partial(crew_store.read_settings, owner, repo, scope))
    label = str(settings.get("needs_human_label") or "").strip()
    skips = await asyncio.to_thread(partial(crew_store.read_skips, owner, repo, scope))
    candidates = {
        k: row for k, row in skips.items() if row.get("scope") in crew_store.ANSWERABLE_SKIP_SCOPES
    }
    checks = await asyncio.to_thread(partial(read_answer_checks, owner, repo, scope))
    before = dict(checks)
    for k in [k for k in checks if k not in candidates]:
        checks.pop(k, None)
    out: dict[str, list[tuple[int, str]]] = {}
    open_issues = None
    if candidates and label:
        # Straight from the forge: the dashboard cache is refreshed only when a person
        # opens Issue Radar, and the timeline read below hits the same forge anyway. A
        # failed read leaves every mark as it was; the next sweep (a minute later)
        # tries again.
        open_issues = await asyncio.to_thread(partial(_read_updated_open_issues, key))
    if open_issues is not None:
        by_number = {str(i.get("number")): i for i in open_issues if isinstance(i, dict)}
        live_by_id = {str(c.get("id")): c for c in live}
        fallback = live_by_id[min(live_by_id)]
        reads = 0
        identity: str | None = None
        for k, row in _answer_scan_order(candidates, by_number, checks):
            issue = by_number.get(k)
            probe: dict[str, float] = {}
            if issue is None:
                # Off the first page: older, not necessarily closed. Read the issue
                # itself, from the same budget, at most once per retry window each,
                # least recently probed first, so every standing pass is seen.
                mark = checks.get(k)
                if isinstance(mark, dict) and _probe_holds(mark):
                    continue
                # Room for the issue read AND its timeline read, or a candidate whose
                # read lands on the last unit would never get its timeline read and
                # would keep losing the same race every window. Left unprobed, it
                # sorts first on the next sweep.
                if reads + 2 > MAX_ANSWER_READS_PER_SWEEP:
                    break
                reads += 1
                probe = {"probed_at": _answer_clock()}
                issue = await asyncio.to_thread(partial(_read_one_issue, key, int(k)))
                if issue is None or issue.get("state") != "open":
                    checks[k] = {**probe}
                    continue
            # Label still present: no call, and the mark is dropped so a label that
            # comes back and goes again is read afresh.
            if label.casefold() in _label_names(issue):
                if probe:
                    checks[k] = {**probe}
                else:
                    checks.pop(k, None)
                continue
            # A removal at or before the pass, or before the answer the pass was
            # recorded after, is already consumed: whatever the clocks, it never
            # wakes a crew twice.
            decided_at = max(str(row.get("decided_at") or ""), str(row.get("answered_at") or ""))
            updated_at = str(issue.get("updated_at") or "")
            mark = checks.get(k)
            # A mark holds only while the issue is untouched: any later activity (a
            # comment, a second removal) moves updated_at and re-arms the read, so a
            # timeline that lagged the list is read again.
            if (
                isinstance(mark, dict)
                and mark.get("decided_at") == decided_at
                and mark.get("updated_at") == updated_at
                and _mark_holds(mark)
            ):
                if probe:
                    checks[k] = {**mark, **probe}
                continue
            if identity is None and reads < MAX_ANSWER_READS_PER_SWEEP:
                identity = await asyncio.to_thread(partial(_forge_identity, key))
            if reads >= MAX_ANSWER_READS_PER_SWEEP or not identity:
                if probe:
                    # The direct read was spent: record it, or this candidate sorts
                    # first and is read again on every sweep.
                    checks[k] = {**(mark if isinstance(mark, dict) else {}), **probe}
                break
            reads += 1
            try:
                events = await asyncio.to_thread(partial(_list_label_events, key, int(k)))
            except Exception:
                logger.debug("issue-radar: label events for #%s failed", k, exc_info=True)
                # Retried after the window, like an empty timeline, so a read that
                # keeps failing does not spend every sweep's budget.
                checks[k] = {
                    "decided_at": decided_at,
                    "updated_at": updated_at,
                    "checked_at": _answer_clock(),
                    "incomplete": True,
                    **probe,
                }
                continue
            answer = find_human_answer(events, label, decided_at, identity)
            if answer is None:
                if any(
                    actor.casefold() == identity.casefold()
                    for actor, _at in _removals_after(events, label, decided_at)
                ):
                    # The label was removed, but under the gateway's own forge login,
                    # which never counts (see the section note). Say so once, at the
                    # read that is about to be marked, so an operator who shares the
                    # login learns why the hand-back did nothing.
                    logger.warning(
                        "issue-radar: #%s in %s/%s: the needs-human label was removed "
                        "by @%s, the gateway's own forge login, so it does not count as "
                        "an answer. Remove it from a different account (see #22527).",
                        k,
                        owner,
                        repo,
                        identity,
                    )
                # A mark stops later reads until the issue changes again. A timeline
                # with no trace of the label the pass applied may be a failed fetch,
                # so its mark lapses after INCOMPLETE_READ_RETRY_SECS (_mark_holds).
                checks[k] = {
                    "decided_at": decided_at,
                    "updated_at": updated_at,
                    "checked_at": _answer_clock(),
                    # The list says the label is off, so a timeline whose newest
                    # event for it is not a removal lags the list (or is empty).
                    "incomplete": not _ends_with_removal(events, label, decided_at),
                    **probe,
                }
                continue
            owner_crew = live_by_id.get(str(row.get("crew_id") or ""))
            woken = owner_crew or fallback
            try:
                await asyncio.to_thread(
                    partial(
                        crew_store.record_human_answer,
                        owner,
                        repo,
                        str(woken.get("id")),
                        int(k),
                        answer["actor"],
                        answer["at"],
                        scope,
                    )
                )
            except Exception:
                logger.warning(
                    "issue-radar: recording the human answer on #%s failed; retrying next sweep",
                    k,
                    exc_info=True,
                )
                continue
            landed = await asyncio.to_thread(partial(crew_store.read_answers, owner, repo, scope))
            got = landed.get(k)
            # Compared by time: another crew's older answer on the number does not
            # stand for this one.
            if not isinstance(got, dict) or str(got.get("at") or "") < str(answer["at"]):
                # The store keeps the newest ANSWERS_LIMIT answers, and this one
                # (an old removal) was evicted the moment it was written, so the pass
                # still stands. Mark it like a "no" instead of writing and waking again
                # every sweep.
                checks[k] = {
                    "decided_at": decided_at,
                    "updated_at": updated_at,
                    "checked_at": _answer_clock(),
                    **probe,
                }
                continue
            checks.pop(k, None)
            out.setdefault(str(woken.get("id")), []).append((int(k), str(answer["actor"])))
    if checks != before:
        await asyncio.to_thread(partial(write_answer_checks, owner, repo, checks, scope))
    return out


# ── new work and mentions ───────────────────────────────────────────────────
#
# Without these a crew finds new work only on its own idle clock, a model turn per
# tick, and that clock is the only knob: fast reaction meant paying for a turn every
# 30 s to hear "nothing new". The sweep already reads the forge for free, so it now
# says three more things itself:
#
#   ``#N scope-label`` an open issue carries a label in the crew's scope, is not
#                      claimed, not waiting on a human, not in the shared skip index
#                      and not one of the crew's own items -- and the crew has not
#                      been woken for it before;
#   ``#N new-issue``   the same, for a crew with an EMPTY scope, on an issue numbered
#                      above the crew's high-water mark. Both only when the repo
#                      setting ``crew_wake_on_new_work`` is on, so a crew that never
#                      opted in keeps taking turns on its idle clock alone;
#   ``#N asked <id>``  a comment by someone other than the crew's forge identity
#                      mentions that identity (``!N`` for a GitLab merge request).
#                      Only when the repo setting ``crew_answer_mentions`` is on:
#                      the identity is the gateway's login, often the operator's own.
#
# Each one seeds on first observation, like every other signal here: a crew seen for
# the first time (or whose scope changed) records what is already there and wakes for
# nothing, so connecting a repo cannot wake a crew for its whole backlog. And each is
# committed only when the wake STARTED a turn: ``wake_crew`` drops a wake on a crew
# that is mid-turn, so a signal recorded as handled then would be lost until the idle
# clock. Uncommitted, the next sweep finds it again. A mention is kept as a pending
# reason instead, because its comment cursor has already moved past it.
#
# Cost, per sweep and repo with a live crew and either setting on: one open-issues
# read (by update time, labels included); for mentions on GitHub one repository-comments read, on GitLab one
# updated-issues and one updated-MRs read plus one notes read per thread updated since
# the cursor, at most :data:`MAX_MENTION_THREADS_PER_SWEEP`. State lives in
# ``crew-wake-checks.json``.

SIG_SCOPE_LABEL = "scope-label"
SIG_NEW_ISSUE = "new-issue"
SIG_ASKED = "asked"

WAKE_CHECKS_SCHEMA = 1

#: Bounds on what the state file remembers: issues a crew was woken for (per crew),
#: comment ids already read, and mention reasons still waiting on a free crew.
_MAX_WAKE_SEEN = 500
_MAX_COMMENT_IDS = 1000
#: While a mention scan is unfinished (its floor is held), every handled id is kept up
#: to this many: the resume finds its page by them, so evicting one would rewind the
#: scan onto comments it already handled, every sweep. A finished scan's floor covers
#: them, and the list is cut back to ``_MAX_COMMENT_IDS``.
#: shortcut: a single tie of more than 20,000 post-floor comments can still rewind;
#: upgrade to explicit tie-window progress if one is ever seen.
_MAX_WINDOW_COMMENT_IDS = 20_000
_MAX_PENDING_ASKS = 20


def wake_checks_path(owner: str, repo: str, root: Path | None = None) -> Path:
    """Beside ``crew-signals.json`` and for the same reason: not inside ``crews/``."""
    return store.repo_data_dir(owner, repo, root) / "crew-wake-checks.json"


class WakeChecksUnreadable(OSError):
    """``crew-wake-checks.json`` exists but could not be read; this sweep skips it."""


def read_wake_checks(owner: str, repo: str, root: Path | None = None) -> dict[str, Any]:
    """The state, with every section present. A missing or foreign file is empty,
    which seeds rather than wakes on the next sweep. A file that exists but cannot
    be read raises :class:`WakeChecksUnreadable`: an empty fallback would be saved
    over it and erase the pending asks it holds."""
    out: dict[str, Any] = {"scope": {}, "new_mark": {}, "mentions": {}}
    path = wake_checks_path(owner, repo, root)
    if not path.is_file():
        return out
    doc = read_json_or(path, None, logger=logger, what="crew wake checks")
    if not isinstance(doc, dict):
        raise WakeChecksUnreadable(f"{path} could not be read; not written")
    if doc.get("schema") != WAKE_CHECKS_SCHEMA:
        return out
    for section in out:
        value = doc.get(section)
        if isinstance(value, dict):
            out[section] = value
    return out


def write_wake_checks(
    owner: str, repo: str, checks: dict[str, Any], root: Path | None = None
) -> None:
    atomic_write(
        wake_checks_path(owner, repo, root),
        json.dumps({"schema": WAKE_CHECKS_SCHEMA, **checks}, indent=2, sort_keys=True),
    )


def _is_bot(actor: str) -> bool:
    """A GitHub App account (``name[bot]``). A bot that @mentions the crew -- a CI or
    review bot addressing a PR's author -- is not a person asking, and waking on it
    would spend a model turn on every bot round."""
    return actor.casefold().endswith("[bot]")


def _now_stamp() -> str:
    """Now, at WHOLE seconds: GitHub stamps comments at second precision, so a floor
    seeded mid-second would drop a mention written later in that same second."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).replace(microsecond=0)
    return crew_store.normalize_forge_time(now.isoformat())


def _issue_rows(issues: Sequence[Any]) -> list[tuple[int, set[str], str]]:
    """``(number, casefolded labels, normalized updated_at)`` per well-formed row."""
    out: list[tuple[int, set[str], str]] = []
    for issue in issues:
        if not isinstance(issue, Mapping):
            continue
        number = issue.get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            stamp = crew_store.normalize_forge_time(issue.get("updated_at"))
            out.append((number, _label_names(issue), stamp))
    return out


def _crew_scope(crew: Mapping[str, Any]) -> list[str]:
    labels = crew.get("labels") or []
    return sorted({str(label).strip().casefold() for label in labels if str(label).strip()})


def _read_open_issues(key: provider.RepoKey) -> list[dict[str, Any]] | None:
    """One page of open issues, newest UPDATE first: the same forge read the answer
    sweep makes (``list_open_issues_first_page``, kept to number, labels and
    ``updated_at``).

    ``None`` when the read failed: unknown is not "no issues", and a sweep that cannot
    see the list leaves every mark as it was.
    """
    return _read_updated_open_issues(key)


async def detect_new_work(
    state: Any,
    key: provider.RepoKey,
    live: list[dict[str, Any]],
    scope: Path | None,
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """Scope-label, new-issue and mention wakes for *live* crews (see the section note).

    Returns ``(reasons, plan)``: the wake reasons per crew id, and what
    :func:`settle_new_work` commits once the wakes have run. Nothing it detects is
    recorded as handled here; only seeding and the mention cursor are written.
    """
    # The prompt owner holds the claim label; read through the owner so the facade
    # does not rebind a name it already serves from it.
    from . import crew_runtime_prompt as _prompt

    owner, repo = key.owner, key.repo
    checks = await asyncio.to_thread(partial(read_wake_checks, owner, repo, scope))
    before = json.dumps(checks, sort_keys=True)
    reasons: dict[str, list[str]] = {}
    staged: dict[str, dict[str, Any]] = {}
    live_ids = [str(c.get("id")) for c in live]

    # Each crew's own threads: the issue numbers and the PR numbers of its items, and
    # the same for its OPEN items (a crew that was preempted or yielded keeps the old
    # item, but the question is for the crew that holds the work now).
    owned: dict[str, tuple[set[int], set[int]]] = {}
    held: dict[str, tuple[set[int], set[int]]] = {}
    for crew in live:
        cid = str(crew.get("id"))
        items = await asyncio.to_thread(
            partial(crew_store.list_work_items, owner, repo, cid, scope)
        )
        live_items = [it for it in items if it.get("phase") not in crew_store.TERMINAL_PHASES]
        for into, group in ((owned, items), (held, live_items)):
            into[cid] = (
                {n for it in group if isinstance(n := it.get("number"), int)},
                {n for it in group if isinstance(n := it.get("pr_number"), int)},
            )

    settings = await asyncio.to_thread(partial(crew_store.read_settings, owner, repo, scope))
    new_work_on = settings.get("crew_wake_on_new_work") is True
    if not new_work_on:
        # Off (the default): no marks kept, so turning it on later seeds from that
        # moment instead of waking for everything labelled meanwhile.
        checks["scope"], checks["new_mark"] = {}, {}
    issues = None
    if new_work_on or settings.get("crew_answer_mentions") is True:
        issues = await asyncio.to_thread(partial(_read_open_issues, key))
    rows = _issue_rows(issues) if issues is not None else []
    if issues is not None and new_work_on:
        needs_human = str(settings.get("needs_human_label") or "").strip().casefold()
        blocked = {_prompt.CLAIM_LABEL.casefold()} | ({needs_human} if needs_human else set())
        skips = await asyncio.to_thread(partial(crew_store.read_skips, owner, repo, scope))
        skipped = {int(k) for k in skips if str(k).isdigit()}
        for crew in live:
            cid = str(crew.get("id"))
            mine = owned[cid][0]
            free = [
                (number, labels)
                for number, labels, _stamp in rows
                if not labels & blocked and number not in skipped and number not in mine
            ]
            crew_scope = _crew_scope(crew)
            if crew_scope:
                _scope_wakes(checks, cid, crew_scope, free, reasons, staged)
            else:
                _new_issue_wakes(checks, cid, free, rows, reasons, staged)

    # Crews that are not live (retired, deleted, paused) keep no scope state. Their
    # pending asks are dropped only once the crew is gone for good: a paused crew's
    # questions are already read, and it answers them when it is resumed.
    for section in ("scope", "new_mark"):
        for cid in [c for c in checks[section] if c not in live_ids]:
            checks[section].pop(cid, None)
    pending = checks["mentions"].get("pending")
    if isinstance(pending, dict):
        kept = await asyncio.to_thread(partial(crew_store.list_crews, owner, repo, scope))
        for cid in set(pending) - {str(c.get("id")) for c in kept}:
            pending.pop(cid, None)

    # Carried asks whose turn ended before consuming them (cancel, permit timeout,
    # restart) go back on the queue: their comments are already read. They leave
    # the in-flight file only after the queue holding them is saved.
    returned: dict[str, list[str]] = {}
    for crew in live:
        cid = str(crew.get("id"))
        lost, carried = await asyncio.to_thread(partial(_inflight.review, owner, repo, scope, cid))
        returned[cid] = lost
        if settings.get("crew_answer_mentions") is True:
            _inflight.requeue(checks["mentions"], cid, lost, carried)

    if settings.get("crew_answer_mentions") is True:
        await _detect_mentions(key, live, (held, owned), checks, rows, issues is not None, reasons)
    else:
        # Off (the default): no mention read, and no cursor kept, so turning it on
        # later seeds from that moment instead of replaying what was said meanwhile.
        checks["mentions"] = {}

    if json.dumps(checks, sort_keys=True) != before:
        await asyncio.to_thread(partial(write_wake_checks, owner, repo, checks, scope))
    for cid, lost in returned.items():
        await asyncio.to_thread(partial(_inflight.settle, owner, repo, scope, cid, lost))
    # The issues on this page: a crew's seen marks for them are evicted last.
    visible = sorted({str(number) for number, _labels, _stamp in rows})
    return reasons, {"checks": checks, "staged": staged, "visible": visible}


def _scope_wakes(
    checks: dict[str, Any],
    cid: str,
    crew_scope: list[str],
    free: list[tuple[int, set[str]]],
    reasons: dict[str, list[str]],
    staged: dict[str, dict[str, Any]],
) -> None:
    wanted = set(crew_scope)
    matching = sorted((n, lab) for n, lab in free if lab & wanted)
    entry = checks["scope"].get(cid)
    if not isinstance(entry, dict) or entry.get("labels") != crew_scope:
        # First sight of this crew, or of this scope: seed, wake nobody.
        checks["scope"][cid] = {
            "labels": crew_scope,
            "seen": {str(n): 1 for n, _lab in matching},
        }
        return
    # One page: an issue missing from it may be older, not closed, so no mark is
    # dropped for being absent (settle_new_work bounds them, page issues kept last).
    seen = entry.setdefault("seen", {})
    fresh = [(n, lab) for n, lab in matching if str(n) not in seen]
    if fresh:
        reasons.setdefault(cid, []).extend(f"#{n} {SIG_SCOPE_LABEL}" for n, _ in fresh)
        staged.setdefault(cid, {})["seen"] = {str(n): 1 for n, _lab in fresh}


def _new_issue_wakes(
    checks: dict[str, Any],
    cid: str,
    free: list[tuple[int, set[str]]],
    rows: list[tuple[int, set[str], str]],
    reasons: dict[str, list[str]],
    staged: dict[str, dict[str, Any]],
) -> None:
    top = max((number for number, _labels, _stamp in rows), default=0)
    mark = checks["new_mark"].get(cid)
    if not isinstance(mark, int) or isinstance(mark, bool):
        checks["new_mark"][cid] = top  # first sight: seed, wake nobody
        return
    fresh = sorted(n for n, _ in free if n > mark)
    if fresh:
        reasons.setdefault(cid, []).extend(f"#{n} {SIG_NEW_ISSUE}" for n in fresh)
        staged.setdefault(cid, {})["mark"] = max(top, mark)
    elif top > mark:
        # Everything new is claimed, skipped or waiting on a human: nothing to say.
        checks["new_mark"][cid] = top


async def _detect_mentions(
    key: provider.RepoKey,
    live: list[dict[str, Any]],
    owners: Sequence[Mapping[str, tuple[set[int], set[int]]]],
    checks: dict[str, Any],
    rows: list[tuple[int, set[str], str]],
    issues_known: bool,
    reasons: dict[str, list[str]],
) -> None:
    """Fold new ``@identity`` comments into the pending asks and *reasons*, in place.

    The crew's own comments never count, and neither does anything when the forge
    identity cannot be read: a mention nobody can tell from the crew's own is skipped.
    Neither does a comment CREATED before the floor (the newest point a scan fully
    reached): that is an old comment someone edited, or one already handled.
    """
    identity = await asyncio.to_thread(partial(_forge_identity, key))
    if not identity:
        return
    state = checks["mentions"]
    cursor = state.get("cursor")
    pending = state.get("pending") if isinstance(state.get("pending"), dict) else {}
    state["pending"] = pending
    if not isinstance(cursor, str) or not cursor:
        # First observation of the repo: start from now, wake for nothing older.
        state["cursor"] = state["floor"] = _now_stamp()
        state["seen"], state["at_cursor"] = [], []
        return
    floor = state.get("floor") if isinstance(state.get("floor"), str) else cursor
    at_cursor = [str(k) for k in state.get("at_cursor") or []]
    seen_now = frozenset(str(i) for i in state.get("seen") or [])
    try:
        comments, next_cursor, at_cursor, complete = await asyncio.to_thread(
            partial(_recent_comments, key, cursor, at_cursor, identity, floor, seen_now)
        )
    except Exception:
        logger.debug("issue-radar: mention read failed", exc_info=True)
        comments, next_cursor, complete = [], cursor, False
    seen_ids = [str(i) for i in state.get("seen") or []]
    seen = set(seen_ids)
    labels_by_number = {number: labels for number, labels, _stamp in rows}
    gitlab = key.provider == provider.GITLAB
    held_back = 0
    for comment in sorted(comments, key=lambda c: str(c.get("created_at") or "")):
        note_id = str(comment.get("id") or "")
        stamp = crew_store.normalize_forge_time(comment.get("created_at"))
        if not note_id or not stamp or note_id in seen or stamp < floor:
            continue
        if held_back:
            held_back += 1
            continue
        actor = str(comment.get("actor") or "").strip()
        number = comment.get("number")
        if not actor or actor.casefold() == identity.casefold() or _is_bot(actor):
            number = None
        # The read matched the WHOLE body before cutting it; the cut body is the
        # fallback for a client that did not.
        asked = comment.get("asked") is True or mentions(str(comment.get("body") or ""), identity)
        if not isinstance(number, int) or not asked:
            seen.add(note_id)
            seen_ids.append(note_id)
            continue
        is_pr = bool(comment.get("is_pr"))
        # The page lists issues: a GitLab MR shares no number space with them.
        thread_labels = {str(lab).casefold() for lab in comment.get("labels") or []}
        labels = set() if gitlab and is_pr else thread_labels or labels_by_number.get(number, set())
        sigil = "!" if is_pr and gitlab else "#"
        reason = f"{sigil}{number} {SIG_ASKED} {note_id}"
        targets = _ask_targets(live, owners, number, is_pr, gitlab, labels)
        if any(len(pending.get(cid, [])) >= _MAX_PENDING_ASKS for cid in targets):
            # Backpressure, not eviction: a crew's queue is full, so this ask and
            # everything after it stay unread. The cursor and floor do not move, so
            # the next sweep reads them again once the crew has drained its queue.
            held_back = 1
            continue
        seen.add(note_id)
        seen_ids.append(note_id)
        for cid in targets:
            queue = pending.setdefault(cid, [])
            if reason not in queue:
                queue.append(reason)
    finished = complete and not held_back
    state["seen"] = seen_ids[-(_MAX_COMMENT_IDS if finished else _MAX_WINDOW_COMMENT_IDS) :]
    if held_back:
        logger.warning(
            "issue-radar: %s/%s: %d comment(s) held back, a crew's %d pending asks are full",
            key.owner,
            key.repo,
            held_back,
            _MAX_PENDING_ASKS,
        )
        state["held_back"] = held_back
    else:
        state.pop("held_back", None)
        state["cursor"], state["at_cursor"] = next_cursor, at_cursor
        state["floor"] = next_cursor if complete else floor
    live_ids = {str(c.get("id")) for c in live}
    for cid, queue in pending.items():
        for reason in queue if cid in live_ids else ():
            if reason not in reasons.get(cid, []):
                reasons.setdefault(cid, []).append(reason)


def _ask_targets(
    live: list[dict[str, Any]],
    owners: Sequence[Mapping[str, tuple[set[int], set[int]]]],
    number: int,
    is_pr: bool,
    gitlab: bool,
    labels: set[str],
) -> list[str]:
    """The ONE crew a mention on thread *number* goes to.

    Every crew on a repo shares the forge identity, so a mention names the account,
    not a crew. It goes to the first crew by id whose item that thread is (its issue,
    or its PR), trying each map in *owners* in turn (open items before finished
    ones); else to the first by id whose scope matches the issue's labels; else to
    the first by id, so a question nobody owns is still answered exactly once.
    """
    ids = sorted(str(c.get("id")) for c in live)
    none: tuple[set[int], set[int]] = (set(), set())
    for owned in owners:
        # GitLab numbers issues and MRs separately; on GitHub they share one sequence.
        hits = [
            cid
            for cid in ids
            if (is_pr and number in owned.get(cid, none)[1])
            or (not (gitlab and is_pr) and number in owned.get(cid, none)[0])
        ]
        if hits:
            return hits[:1]
    by_scope = {str(c.get("id")): set(_crew_scope(c)) for c in live}
    matched = sorted(cid for cid, wanted in by_scope.items() if wanted & labels)
    return (matched or ids)[:1]


async def settle_new_work(
    key: provider.RepoKey,
    plan: dict[str, Any],
    results: Mapping[str, bool],
    scope: Path | None,
    sent: Mapping[str, Sequence[str]] | None = None,
) -> None:
    """Commit what :func:`detect_new_work` found, for the crews whose wake STARTED a turn.

    A crew that was mid-turn keeps everything: its scope issues stay unseen, its mark
    stays where it was, its asks stay pending, and the next sweep wakes it again.
    """
    checks = plan.get("checks")
    if not isinstance(checks, dict):
        return
    changed = False
    for cid, staged in (plan.get("staged") or {}).items():
        if not results.get(cid):
            continue
        entry = checks["scope"].get(cid)
        if "seen" in staged and isinstance(entry, dict):
            seen = entry.setdefault("seen", {})
            seen.update(staged["seen"])
            # Evict marks for issues off the current page first: evicting one that is
            # still on it would make it fresh again, and wake the crew every sweep.
            visible = set(plan.get("visible") or [])
            order = sorted(seen, key=lambda k: (k in visible, int(k)))
            for k in order[: max(0, len(seen) - _MAX_WAKE_SEEN)]:
                seen.pop(k, None)
            changed = True
        if "mark" in staged:
            checks["new_mark"][cid] = staged["mark"]
            changed = True
    pending = checks["mentions"].get("pending")
    if isinstance(pending, dict):
        for cid in [c for c in pending if results.get(c)]:
            # Only the asks that wake actually carried: one a sweep did not fold into
            # its reasons (its mention read failed) stays queued for the next wake.
            carried = set((sent or {}).get(cid) or [])
            kept = [r for r in pending[cid] if r not in carried]
            if kept != pending[cid]:
                changed = True
            if kept:
                pending[cid] = kept
            else:
                pending.pop(cid, None)
    if changed:
        await asyncio.to_thread(partial(write_wake_checks, key.owner, key.repo, checks, scope))


# ── the sweep ───────────────────────────────────────────────────────────────


def _is_due(item: dict[str, Any], stored: dict[str, Any] | None, now: float) -> bool:
    """Whether this item's phase is due for a re-read on this tick."""
    phase = str(item.get("phase") or "")
    if phase == "selected":
        return False  # pre-claim, local only — nothing public to watch
    if not stored:
        return True
    interval = RECHECK_SEC.get(phase, _DEFAULT_RECHECK_SEC)
    last = stored.get("checked_at")
    if not isinstance(last, (int, float)):
        return True
    # A mark in the FUTURE is due immediately. The mark is wall-clock, so a clock
    # correction (or a machine that resumed with a bad clock) can leave one ahead
    # of now — and an unconditional ``elapsed >= interval`` would then park that
    # item forever, which is the exact silent stall this sweep exists to prevent.
    if now < float(last):
        return True
    return (now - float(last)) >= interval


async def sweep_repo(app: Any, key: provider.RepoKey, root: Path | None = None) -> dict[str, Any]:
    """One zero-LLM pass over every crew's open work items in ``key``'s repo.

    Returns ``{crew_id: [signal, ...]}`` for the crews that were woken (the return
    value is what the tests assert on; the loop ignores it).

    Deliberately NOT gated on the per-repo ``notify_on_new_issue`` setting. That
    flag is a notification preference — whether the bell rings for a new issue —
    and a crew that stopped reconciling its own PRs because the user muted
    notifications would stall every open item with no trace. The app's enabled gate
    is the switch that stops crews, and it stays in ``watch.py``.
    """
    scope = (
        root
        if root is not None
        else store.provider_root(root=None, provider=key.provider, host=key.host)
    )
    crews = await asyncio.to_thread(partial(crew_store.list_crews, key.owner, key.repo, scope))
    if not crews:
        return {}
    state = app.get("state") if hasattr(app, "get") else None
    if state is not None:
        # Runs BEFORE the signal pass and over ALL crews, not just the ones with a
        # due item: re-establishing trust and re-arming a lost loop is exactly what
        # a crew with nothing due needs after a restart.
        await watchdog_cycle(state, key.owner, key.repo, crews, scope, key)
    live = [c for c in crews if is_live(c)]
    if not live:
        return {}

    # Human answers first, and regardless of whether any work item is due: an answered
    # pass has no work item to come due, which is exactly why it needs its own read.
    answered: dict[str, list[tuple[int, str]]] = {}
    if state is not None:
        try:
            answered = await detect_human_answers(state, key, live, scope)
        except Exception:
            logger.warning("issue-radar: human-answer detection failed", exc_info=True)
    woken: dict[str, Any] = {}
    reasons: dict[str, list[str]] = {}
    for crew_id, numbers in answered.items():
        for number, actor in numbers:
            woken.setdefault(crew_id, []).append(SIG_HUMAN_ANSWERED)
            reasons.setdefault(crew_id, []).append(f"#{number} {SIG_HUMAN_ANSWERED} by @{actor}")
    crews_by_id = {str(c.get("id")): c for c in live}

    # New work and mentions: same footing as the answers, no work item needed.
    plan: dict[str, Any] = {}
    if state is not None:
        found: dict[str, list[str]] = {}
        try:
            found, plan = await detect_new_work(state, key, live, scope)
        except Exception:
            logger.warning("issue-radar: new-work detection failed", exc_info=True)
        for crew_id, why in found.items():
            woken.setdefault(crew_id, []).extend(r.split(" ")[1] for r in why)
            reasons.setdefault(crew_id, []).extend(why)

    stored = await asyncio.to_thread(partial(read_signals, key.owner, key.repo, scope))
    # WALL clock, not the loop's monotonic one: this value is persisted, and a
    # monotonic reading restarts near zero on the next gateway launch — every
    # stored mark would then sit in the future and no item would ever come due
    # again until the loop clock caught up.
    now = time.time()

    # Pass 1 — which items are due, and which PRs need the batched enrichment.
    due: list[tuple[dict[str, Any], dict[str, Any]]] = []
    open_keys: set[str] = set()
    for crew in live:
        items = await asyncio.to_thread(
            partial(
                crew_store.list_work_items,
                key.owner,
                key.repo,
                str(crew.get("id")),
                scope,
                open_only=True,
            )
        )
        for item in items:
            ikey = _item_key(str(crew.get("id")), item.get("number"))
            open_keys.add(ikey)
            entry = stored.get(ikey)
            if _is_due(item, entry if isinstance(entry, dict) else None, now):
                due.append((crew, item))

    # A mark for an item that is not open is dead weight — a crew that works
    # for a month would otherwise carry every issue it ever finished in a file it
    # rewrites every minute. Dropping it also makes a REOPENED item re-seed, which
    # is right: its old fingerprint describes a different state of the world.
    stale = [k for k in stored if k not in open_keys]
    for k in stale:
        stored.pop(k, None)

    if not due:
        if stale:
            await asyncio.to_thread(partial(write_signals, key.owner, key.repo, stored, scope))
        await _wake_and_settle(state, key, crews_by_id, reasons, scope, plan)
        return woken

    prs = sorted({pr for _, it in due if isinstance(pr := it.get("pr_number"), int) and pr > 0})
    enriched = await _enrich(key, prs) if prs else {}

    # The dependency graph for the whole repo, read ONCE per sweep. Without a
    # refresh here the signal only works while someone keeps visiting /deps: a
    # headless gateway would fingerprint the same stale blocker states forever
    # and a merged blocker would never wake its crew. So an absent or expired
    # cache is refreshed IN the sweep — one bounded batched fetch per repo per
    # cycle (the same fetch the /deps route uses: on GitHub a batched GraphQL
    # walk, ~1 call per 100 open issues; on GitLab one bounded ``/links`` call
    # per open issue, up to ``gitlab_queries._MAX_LINKS_CALLS``), gated on there
    # being due items at all. A failed refresh keeps the
    # stale graph (or None), which per ``detect_unblocks`` never fires the
    # unlock — fail-closed, exactly like an unknown reading.
    deps = await asyncio.to_thread(partial(_read_or_refresh_deps, key, scope))

    # Pass 2 — fingerprint each due item, compare, then wake each crew ONCE.
    for crew, item in due:
        crew_id = str(crew.get("id"))
        ikey = _item_key(crew_id, item.get("number"))
        entry = stored.get(ikey) if isinstance(stored.get(ikey), dict) else None
        prev = (entry or {}).get("fp") if isinstance((entry or {}).get("fp"), dict) else None
        try:
            fp = await asyncio.to_thread(partial(fingerprint_item, key, item, enriched, prev, deps))
        except Exception:
            # A per-item failure leaves its mark UNTOUCHED, so the change is still
            # pending next cycle rather than being silently consumed by the error.
            logger.warning(
                "issue-radar crew sweep failed for %s#%s",
                key.owner,
                item.get("number"),
                exc_info=True,
            )
            continue
        signals = detect_unblocks(prev, fp)
        stored[ikey] = {"fp": fp, "checked_at": now}
        if not signals:
            continue
        woken.setdefault(crew_id, []).extend(signals)
        reasons.setdefault(crew_id, []).append(f"#{item.get('number')} {', '.join(signals)}")

    await asyncio.to_thread(partial(write_signals, key.owner, key.repo, stored, scope))
    await _wake_and_settle(state, key, crews_by_id, reasons, scope, plan)
    return woken


async def _wake_and_settle(
    state: Any,
    key: provider.RepoKey,
    crews_by_id: dict[str, dict[str, Any]],
    reasons: dict[str, list[str]],
    scope: Path | None,
    plan: dict[str, Any],
) -> None:
    """Wake each crew once, then commit the new-work signals whose wake started a turn.

    ``crew_answer_mentions`` is read again here, after every forge read of the sweep:
    an operator who turned it off while a mention scan was in flight has the scan's
    asks dropped before any turn starts, not answered once more.
    """
    if any(r.split(" ")[1:2] == [SIG_ASKED] for why in reasons.values() for r in why):
        settings = await asyncio.to_thread(
            partial(crew_store.read_settings, key.owner, key.repo, scope)
        )
        if settings.get("crew_answer_mentions") is not True:
            reasons = {
                cid: kept
                for cid, why in reasons.items()
                if (kept := [r for r in why if r.split(" ")[1:2] != [SIG_ASKED]])
            }
            if isinstance(plan.get("checks"), dict):
                # Saved now, so turning it back on before the next sweep replays none.
                plan["checks"]["mentions"] = {}
                await asyncio.to_thread(
                    partial(write_wake_checks, key.owner, key.repo, plan["checks"], scope)
                )
    # Asks are carried BEFORE the wake: the turn can be admitted before this returns.
    from .crew_runtime_session import _slot_key as _crew_slot_key

    asks = {
        cid: [r for r in why if r.split(" ")[1:2] == [SIG_ASKED]] for cid, why in reasons.items()
    }
    for cid, mine in asks.items():
        if mine and cid in crews_by_id:
            slot_key = _crew_slot_key(crews_by_id[cid])
            try:
                took = await asyncio.to_thread(
                    partial(_inflight.carry, key.owner, key.repo, scope, cid, slot_key, mine)
                )
            except OSError:
                # Not recorded as carried, so not carried: the asks stay pending.
                logger.warning("issue-radar: could not record carried asks", exc_info=True)
                took = []
            # An ask the in-flight file had no room for stays pending, out of this wake.
            asks[cid] = took
            if not (kept := [r for r in reasons[cid] if r not in mine or r in took]):
                reasons.pop(cid)
            else:
                reasons[cid] = kept
    results = await _wake_for(state, key, crews_by_id, reasons, scope)
    for cid, mine in asks.items():
        if mine and not results.get(cid):
            try:
                await asyncio.to_thread(
                    partial(_inflight.uncarry, key.owner, key.repo, scope, cid, mine)
                )
            except OSError:
                # The rows stay; past LAPSE_SECS the sweep hands them back to the queue.
                logger.warning("issue-radar: could not forget carried asks", exc_info=True)
    if plan:
        try:
            await settle_new_work(key, plan, results, scope, reasons)
        except Exception:
            logger.warning("issue-radar: recording new-work wakes failed", exc_info=True)


async def _wake_for(
    state: Any,
    key: provider.RepoKey,
    crews_by_id: dict[str, dict[str, Any]],
    reasons: dict[str, list[str]],
    scope: Path | None,
) -> dict[str, bool]:
    """ONE wake per crew, carrying every reason. Returns whether each STARTED a turn.

    Two items signalling in the same sweep is one turn's worth of work, and the second
    call would be dropped as mid-turn anyway — so the crew would have been told about
    only the first. A crew that was mid-turn (or whose wake failed) maps to ``False``,
    which is what makes :func:`settle_new_work` keep its signals for the next sweep.
    """
    results: dict[str, bool] = {}
    if state is None:
        return results
    for crew_id, why in reasons.items():
        results[crew_id] = False
        try:
            results[crew_id] = bool(
                await wake_crew(
                    state,
                    key.owner,
                    key.repo,
                    crews_by_id[crew_id],
                    "; ".join(why),
                    scope,
                    key,
                )
            )
        except Exception:  # pragma: no cover - defensive
            logger.warning("issue-radar: waking crew %s failed", crew_id, exc_info=True)
    return results


async def _enrich(key: provider.RepoKey, prs: list[int]) -> dict[int, dict[str, Any]]:
    """Check rollup + merge state for every watched PR in the repo, batched.

    Two GraphQL calls for the whole repo, not two per PR — and it is the only way
    to see CI at all: a check-run completing does not touch the PR record, so no
    ``updated_at`` anywhere reflects it.
    """
    client = provider.client_for(key)
    kwargs = provider.call_kwargs(key)
    rows = [{"number": n} for n in prs]
    try:
        enriched = await asyncio.to_thread(
            partial(client.enrich_pulls_by_number, key.owner, key.repo, rows, **kwargs)
        )
    except Exception:
        # Best-effort, like every other enrichment caller: an unknown rollup reads
        # as None, which `detect_unblocks` refuses to interpret as a CI change.
        logger.debug("issue-radar: crew sweep enrichment failed", exc_info=True)
        return {}
    out: dict[int, dict[str, Any]] = {}
    for row in enriched or []:
        num = row.get("number") if isinstance(row, dict) else None
        if isinstance(num, int):
            out[num] = row
    return out


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.apps.builtins.issue_radar.backend.crew_runtime_prompt",
    "kiro_crew.apps.builtins.issue_radar.backend.crew_runtime_session",
    "kiro_crew.apps.builtins.issue_radar.backend.crew_runtime_supervision",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.apps.builtins.issue_radar.backend.crew_runtime_prompt import (  # noqa: F401
        _BRIEF_PATH,
        BRIEF_SENTINEL,
        CLAIM_LABEL,
        _assemble_turn_prompt,
        _brief_cache,
        brief_is_present,
        brief_text,
        build_snapshot,
        compose_nudge,
        compose_turn_prompt,
        compose_turn_prompt_async,
        never_block,
        vocabulary,
        writable_labels,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.crew_runtime_session import (  # noqa: F401
        _ADMISSION_HOOK,
        _GRANTS,
        _HAS_SECURITY,
        APP_NAME,
        DEFAULT_IDLE_SECS,
        GRANT_SOURCE,
        NO_PERMIT_CARD,
        TRUST_TTL_SECS,
        _apply_turn_model,
        _autonudge_instance,
        _call_if_present,
        _capped_run_chat,
        _current_crew,
        _grant_name,
        _hold_grant,
        _record_model_hold,
        _rehydrate,
        _slot_key,
        _slot_title,
        _tell_admission,
        _turn_model_items,
        autoapprove_scope,
        dispatch_crew_turn,
        ensure_crew_session,
        launch_crew,
        release_grant,
        release_slot_grant,
        stop_sentinel_path,
        sync_trust,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.crew_runtime_supervision import (  # noqa: F401
        _CREW_SLOT_PREFIX,
        _STAND_DOWN_REASONS,
        DISMISSED_PAUSE_REASON,
        STOOD_DOWN_PAUSE_REASON,
        _crew_for_slot_key,
        _disabling,
        _is_crew_slot_key,
        _lookup_scopes,
        _note_revocation,
        _on_slot_close_undone,
        _on_slot_closed,
        _pause_stood_down_crew,
        _reconcile_trust,
        _retire_stood_down_loop,
        _revoke_generation,
        _stood_down,
        _trust_inputs,
        _wake_crew_body,
        install_app_disable_hook,
        install_slot_close_hook,
        on_app_disabled,
        refresh_crew_loop_message,
        revoke_crew_execution,
        revoke_crew_grants,
        suspend_crews,
    )

if TYPE_CHECKING:  # names it serves from outside the owner family, from their homes
    from kiro_crew.apps import (  # noqa: F401
        teardown,
    )
    from kiro_crew.apps.builtins.issue_radar.backend import (  # noqa: F401
        crew_model,
    )
    from kiro_crew.apps.teardown import (  # noqa: F401
        register_slot_close_hook,
        register_slot_close_undo_hook,
    )
    from kiro_crew.apps.worker_auto_approve import (  # noqa: F401
        WorkerAutoApprove,
    )
    from kiro_crew.autonudge_service.model import (  # noqa: F401
        CONSECUTIVE_FAILURE_REASON,
        SESSION_START_FAILURE_REASON,
    )
    from kiro_crew.dashboard.chat_persistence import (  # noqa: F401
        rehydrate_slot_from_history_async,
    )
    from kiro_crew.dashboard.chat_runner import (  # noqa: F401
        _run_chat,
    )
    from kiro_crew.dashboard.chat_utils import (  # noqa: F401
        slot_history_key,
    )
    from kiro_crew.security import (  # noqa: F401
        redact_credentials,
        redact_exfiltration_urls,
    )
