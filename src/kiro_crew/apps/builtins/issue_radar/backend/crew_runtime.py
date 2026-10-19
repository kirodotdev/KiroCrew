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
   follows the TASK RUNNER instead: a scoped, expiring, SEL-audited
   ``SafetyOverride`` grant (:func:`autoapprove_scope`), never the unbounded
   ``slot._trust`` flag a human clicks.

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
state lives in ``crew-signals.json`` and ``crew-answer-checks.json``, beside
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

from . import crew_store, github_client, provider, store
from .crew_runtime_session import is_live
from .crew_runtime_supervision import wake_crew, watchdog_cycle

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


def _read_or_refresh_deps(key: provider.RepoKey, scope) -> dict[str, Any] | None:
    """The sweep's deps source: cache read, refreshed in place when absent/expired.

    Runs in a worker thread (the caller wraps it in ``asyncio.to_thread``).
    GitHub-only, like the /deps route: other providers have no dependency fetch
    yet, and for them this stays a plain cache read (normally None). A refresh
    failure of any kind keeps whatever the cache held — stale beats wrong, and
    an unknown reading can never fire the unlock (see ``detect_unblocks``).
    """
    cached = store.read_deps_cache(key.owner, key.repo, scope)
    fresh_enough = (
        isinstance(cached, dict)
        and (time.time() - float(cached.get("fetched_at") or 0)) < store.DEPS_CACHE_TTL_SEC
    )
    if fresh_enough or key.provider != "github":
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
        hints: dict[int, dict] = {}
        edges, nodes = github_client.fetch_dependency_edges(key.owner, key.repo, open_issues, hints)
        store.write_deps_cache(
            key.owner, key.repo, edges, nodes, root=scope, fetched_at=fetch_started
        )
        return store.read_deps_cache(key.owner, key.repo, scope)
    except Exception:
        logger.debug(
            "issue-radar: sweep deps refresh failed for %s/%s; keeping cached graph",
            key.owner,
            key.repo,
            exc_info=True,
        )
        return cached


def _open_blocker_count(deps: dict[str, Any] | None, number: Any) -> int | None:
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
    keep an item blocked than to fire a spurious unlock on a missing node).
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
    fp["open_blockers"] = _open_blocker_count(deps, number) if item_state == "open" else None
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
# (``crew_store.ANSWERABLE_SKIP_SCOPES``: ``needs-decision``, ``needs-investigation``
# and the four scope gates ``architecture``, ``new-feature``, ``needs-design``,
# ``breaking-change``) and labels the issue with the repo's needs-human label.
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
# activity on the issue re-arms the read.

SIG_HUMAN_ANSWERED = "human-answered"

ANSWER_CHECKS_SCHEMA = 1

#: Label-event reads one sweep may spend. A repo that just had a dozen questions
#: answered gets them over a few cycles instead of one burst.
MAX_ANSWER_READS_PER_SWEEP = 10


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
    """Beside ``crew-signals.json`` and for the same reason: not inside ``crews/``."""
    return store.repo_data_dir(owner, repo, root) / "crew-answer-checks.json"


def read_answer_checks(owner: str, repo: str, root: Path | None = None) -> dict[str, Any]:
    path = answer_checks_path(owner, repo, root)
    if not path.is_file():
        return {}
    doc = read_json_or(path, None, logger=logger, what="crew answer checks")
    if not isinstance(doc, dict) or doc.get("schema") != ANSWER_CHECKS_SCHEMA:
        return {}
    items = doc.get("items")
    return items if isinstance(items, dict) else {}


def write_answer_checks(
    owner: str, repo: str, items: dict[str, Any], root: Path | None = None
) -> None:
    atomic_write(
        answer_checks_path(owner, repo, root),
        json.dumps({"schema": ANSWER_CHECKS_SCHEMA, "items": items}, indent=2),
    )


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
        reader = getattr(client, "get_current_login_uncached", None) or client.get_current_login
        login = reader(**provider.call_kwargs(key))
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
    """The label removals on one issue, read from its timeline."""
    client = provider.client_for(key)
    rows = client.list_issue_timeline(key.owner, key.repo, number, **provider.call_kwargs(key))
    return [r for r in rows or [] if isinstance(r, dict) and r.get("kind") == "unlabeled"]


def find_human_answer(
    events: Sequence[Mapping[str, Any]], label: str, since: str, identity: str
) -> dict[str, str] | None:
    """The newest removal of *label* after *since* by someone other than *identity*.

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
    found: dict[str, str] | None = None
    for actor, at in _removals_after(events, label, since):
        if actor.casefold() == identity.casefold():
            continue
        if found is None or at > found["at"]:
            found = {"actor": actor, "at": at}
    return found


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


def _crew_live_unit(state: Any, crew: Mapping[str, Any]) -> str:
    """The crew log unit *crew*'s slot serves on now, or ``""`` -- the write route's
    own resolution (``crew_routes._crew_live_unit``), from the gateway state."""
    from kiro_crew.crew_log.resolve import unit_for_session_key

    # Read from the owner at call time: this facade binds the session helpers only
    # for type checkers, and a bare global read here never reaches its __getattr__.
    from .crew_runtime_session import _slot_key as slot_key_of

    slot = slot_key_of(dict(crew))
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return ""
    try:
        return unit_for_session_key(sessions, f"dashboard:{slot}")
    except Exception:
        logger.debug("issue-radar: resolving crew %s's unit failed", crew.get("id"), exc_info=True)
        return ""


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

    The answer is written into the log of the crew that recorded the pass when that
    crew is live with a session (so its own re-skip replaces its own row), else into
    the woken crew's. The crew woken is the one that recorded the pass when it is
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
    # An answer whose write was still queued in the crew log writer when it answered
    # (``durable: False``) has woken nobody yet. Its number leaves ``candidates`` once
    # the entry lands, so it is carried here until a read confirms it, and only then
    # does the wake go out.
    pending = {
        k: m for k, m in checks.items() if isinstance(m, dict) and isinstance(m.get("answer"), dict)
    }
    for k in [k for k in checks if k not in candidates and k not in pending]:
        checks.pop(k, None)
    out: dict[str, list[tuple[int, str]]] = {}
    if pending:
        landed = await asyncio.to_thread(partial(crew_store.read_answers, owner, repo, scope))
        live_ids = {str(c.get("id")) for c in live}
        for k, held in pending.items():
            answer = held["answer"]
            got = landed.get(k)
            if not isinstance(got, dict) or str(got.get("at") or "") < str(answer.get("at") or ""):
                continue  # still queued: keep the mark, wake nobody
            crew_id = str(held.get("crew_id") or "")
            if crew_id not in live_ids:
                crew_id = min(live_ids)
            out.setdefault(crew_id, []).append((int(k), str(answer.get("actor") or "")))
            checks.pop(k, None)
            candidates.pop(k, None)
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
        for k, row in sorted(candidates.items(), key=lambda kv: int(kv[0])):
            issue = by_number.get(k)
            if issue is None:
                # One page: a candidate missing from it may be older, not closed.
                continue
            # Label still present: no call, and the mark is dropped so a label that
            # comes back and goes again is read afresh.
            if label.casefold() in _label_names(issue):
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
            ):
                continue
            if reads >= MAX_ANSWER_READS_PER_SWEEP:
                break
            if identity is None:
                identity = await asyncio.to_thread(partial(_forge_identity, key))
            if not identity:
                break
            reads += 1
            try:
                events = await asyncio.to_thread(partial(_list_label_events, key, int(k)))
            except Exception:
                logger.debug("issue-radar: label events for #%s failed", k, exc_info=True)
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
                # A mark stops later reads until the issue changes again.
                checks[k] = {
                    "decided_at": decided_at,
                    "updated_at": updated_at,
                    "checked_at": time.time(),
                }
                continue
            owner_crew = live_by_id.get(str(row.get("crew_id") or ""))
            woken = owner_crew or fallback
            writer = owner_crew if owner_crew and _crew_live_unit(state, owner_crew) else woken
            unit = _crew_live_unit(state, writer)
            try:
                written = await asyncio.to_thread(
                    partial(
                        crew_store.record_human_answer,
                        owner,
                        repo,
                        str(writer.get("id")),
                        int(k),
                        answer["actor"],
                        answer["at"],
                        scope,
                        session_id=unit,
                    )
                )
            except Exception:
                logger.warning(
                    "issue-radar: recording the human answer on #%s failed; retrying next sweep",
                    k,
                    exc_info=True,
                )
                continue
            if isinstance(written, Mapping) and written.get("durable") is False:
                # Queued, not yet in the log: publishing the wake now would hand a
                # crew an issue the snapshot it reads still lists as skipped.
                checks[k] = {"answer": dict(answer), "crew_id": str(woken.get("id"))}
                continue
            checks.pop(k, None)
            out.setdefault(str(woken.get("id")), []).append((int(k), str(answer["actor"])))
    if checks != before:
        await asyncio.to_thread(partial(write_answer_checks, owner, repo, checks, scope))
    return out


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
        await _wake_for(state, key, crews_by_id, reasons, scope)
        return woken

    prs = sorted({pr for _, it in due if isinstance(pr := it.get("pr_number"), int) and pr > 0})
    enriched = await _enrich(key, prs) if prs else {}

    # The dependency graph for the whole repo, read ONCE per sweep. Without a
    # refresh here the signal only works while someone keeps visiting /deps: a
    # headless gateway would fingerprint the same stale blocker states forever
    # and a merged blocker would never wake its crew. So an absent or expired
    # cache is refreshed IN the sweep — one bounded batched fetch per repo per
    # cycle (the same batched GraphQL walk the route uses, ~1 call per 100 open
    # issues), gated on there being due items at all. A failed refresh keeps the
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
    await _wake_for(state, key, crews_by_id, reasons, scope)
    return woken


async def _wake_for(
    state: Any,
    key: provider.RepoKey,
    crews_by_id: dict[str, dict[str, Any]],
    reasons: dict[str, list[str]],
    scope: Path | None,
) -> None:
    """ONE wake per crew, carrying every reason.

    Two items signalling in the same sweep is one turn's worth of work, and the second
    call would be dropped as mid-turn anyway — so the crew would have been told about
    only the first.
    """
    if state is None:
        return
    for crew_id, why in reasons.items():
        try:
            await wake_crew(
                state,
                key.owner,
                key.repo,
                crews_by_id[crew_id],
                "; ".join(why),
                scope,
                key,
            )
        except Exception:  # pragma: no cover - defensive
            logger.warning("issue-radar: waking crew %s failed", crew_id, exc_info=True)


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
        _hold_grant,
        _record_model_hold,
        _rehydrate,
        _slot_key,
        _slot_title,
        _turn_model_items,
        autoapprove_scope,
        dispatch_crew_turn,
        ensure_crew_session,
        launch_crew,
        stop_sentinel_path,
        sync_trust,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.crew_runtime_supervision import (  # noqa: F401
        _CREW_SLOT_PREFIX,
        DISMISSED_PAUSE_REASON,
        _crew_for_slot_key,
        _disabling,
        _is_crew_slot_key,
        _lookup_scopes,
        _note_revocation,
        _on_slot_close_undone,
        _on_slot_closed,
        _reconcile_trust,
        _revoke_generation,
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
    from kiro_crew.dashboard.chat_persistence import (  # noqa: F401
        rehydrate_slot_from_history_async,
    )
    from kiro_crew.dashboard.chat_runner import (  # noqa: F401
        _run_chat,
    )
    from kiro_crew.dashboard.chat_utils import (  # noqa: F401
        slot_history_key,
    )
    from kiro_crew.safety_override import (  # noqa: F401
        safety_override,
    )
    from kiro_crew.security import (  # noqa: F401
        redact_credentials,
        redact_exfiltration_urls,
    )
