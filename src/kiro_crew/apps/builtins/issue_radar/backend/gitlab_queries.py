"""GitLab dependency-edge fetcher for Issue Radar.

Reads ``/projects/:id/issues/:iid/links`` for every open issue and extracts
dependency edges from ``blocks`` / ``is_blocked_by`` link types.  ``relates_to``
is intentionally ignored — it is not a blocking relationship.

Per-issue failure policy
------------------------
A single issue's ``/links`` call failing (network error, 403, timeout) is
treated as **degrade that issue**: the issue is skipped with a debug log entry
and the rest of the graph continues to be built, as the GitHub path does for a
per-issue ``list_issue_blocked_by`` failure.

When MOST fetched issues fail (strictly more than ``_LINKS_FAILURE_RATIO`` of the
calls, which includes every one of them), that policy stops applying: the cause is
the host (unreachable, de-allowlisted, a 403 on the whole project, rate limiting
that set in partway), not the issues, and a graph built from the few survivors is
not an empty graph. ``fetch_dependency_edges`` raises
:class:`~.errors.ProviderCliError` instead, logged once at WARNING, so the route
answers ``deps_fetch_failed`` and the previous cache is kept.

A :class:`~.errors.ProviderSetupError` is the exception to that policy: it means
the ``glab`` CLI is absent or has no session for this host, so EVERY call would
fail the same way and "degrade each issue" would hand the route a complete-looking
empty graph, which it would persist and serve as fresh for the cache TTL. It
propagates instead, the pool stops submitting, and the route maps it to the
``deps_fetch_failed`` 502 while the previous cache stays intact.

Cross-project links
-------------------
``/links`` can name an issue in ANOTHER project (``blocks`` across projects is
legal). A row is kept only when its ``references.relative`` is ``#<iid>`` -- the
form GitLab uses for an issue in the project the request was made in. Any other
form, and a row with no references at all, is dropped rather than assumed local,
because a foreign ``#42`` would otherwise become this project's #42.

Bounded concurrency / call-count cap
-------------------------------------
``_LINKS_WORKERS`` controls the thread-pool width. ``_MAX_LINKS_CALLS`` is a hard
cap on the number of issues whose links are fetched; the overflow is counted and
logged once per fetch so a truncated graph is distinguishable from a complete one
(``github_queries.DEPS_GRAPHQL_MAX_PAGES`` is the GitHub counterpart).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import gitlab_client
from .errors import ProviderCliError, ProviderSetupError

logger = logging.getLogger("kirocrew.app.issue-radar")

#: Link types that represent a blocking relationship.
_BLOCKING_LINK_TYPES = frozenset({"blocks", "is_blocked_by"})

#: Maximum concurrent ``/links`` requests.  Small enough not to flood the
#: GitLab instance, large enough to be faster than purely sequential.
_LINKS_WORKERS = 8

#: Hard ceiling on the number of issues whose links are fetched.  Beyond this
#: the graph is partial but the call never loops without bound.
_MAX_LINKS_CALLS = 200

#: Retained-edge cap; edges past it are dropped and the graph served partial.
_MAX_DEPS_EDGES = 20_000

#: Retained-node cap; open issues and linked nodes past it are dropped, graph partial.
_MAX_DEPS_NODES = 5_000

#: Per-issue link-row cap; rows past it are dropped inside the worker.
_MAX_LINKS_PER_ISSUE = 500

#: Share of ``/links`` calls that may fail before the walk stops counting as a
#: graph. Strictly MORE than this fraction failing raises instead of returning the
#: survivors; at or below it the partial graph is returned.
_LINKS_FAILURE_RATIO = 0.5

#: Hard ceiling for titles retained in dependency graph nodes. GitLab limits issue
#: titles to 255 characters, so 512 is generous while keeping snapshots bounded.
_MAX_TITLE_CHARS = 512

#: Per-call timeout forwarded to ``gitlab_client.list_issue_links``.
_LINKS_TIMEOUT_SEC = 15.0


def _bounded_title(value: object) -> str:
    """Return a dependency-node title with a fixed retention bound."""
    return str(value or "")[:_MAX_TITLE_CHARS]


def _dep_node_state(state: object) -> str:
    """Normalize a GitLab issue state into the vocabulary the frontend expects.

    GitLab issues use ``"opened"`` / ``"closed"``.  There is no ``"merged"``
    concept for issues (only for MRs), so this maps to ``"open"`` / ``"closed"``.
    The store's ``_normalize_deps`` and the frontend's ``resolveLifecycle`` both
    accept these values.
    """
    normalized = str(state or "").lower()
    return "closed" if normalized == "closed" else "open"


def _warn_if_deps_truncated(
    owner: str,
    repo: str,
    *,
    open_issue_nodes_over_max: int = 0,
    link_rows_over_per_issue_max: int = 0,
    edges_over_max: int = 0,
    nodes_over_max: int = 0,
) -> None:
    """Log once when a retention bound dropped rows, so a partial graph is
    distinguishable from a complete one. The bounds truncate rather than reject:
    a project past a cap gets the same partial graph on every load, the way the
    GitHub fetcher behaves at ``DEPS_GRAPHQL_MAX_PAGES`` and this fetcher at
    ``_MAX_LINKS_CALLS``, instead of a 502 that no later load can clear."""
    overflows = (
        open_issue_nodes_over_max,
        link_rows_over_per_issue_max,
        edges_over_max,
        nodes_over_max,
    )
    if not any(overflows):
        return
    logger.warning(
        "issue-radar: gitlab dependency graph for %s/%s truncated at its retention bounds "
        "(open_issue_nodes_over_max=%d, link_rows_over_per_issue_max=%d, "
        "edges_over_max=%d, nodes_over_max=%d); serving the partial graph",
        owner,
        repo,
        *overflows,
    )


def _fetch_issue_links(
    owner: str, repo: str, iid: int, *, host: str, timeout: float
) -> tuple[list[dict], int] | None:
    """Fetch bounded, projected links; ``None`` when this issue's call fails.

    Successful results contain at most ``_MAX_LINKS_PER_ISSUE`` rows plus the
    number of dictionary rows dropped beyond that cap. Each row retains only the
    fields consumed by ``fetch_dependency_edges``, with strings canonicalized or
    bounded before the worker returns.

    ``None``, not an empty result: the caller counts failures, and an issue with no
    links is a result while a failed call is not. Conflating them is what let a
    host-wide outage read as "every issue is link-free".

    A :class:`ProviderSetupError` propagates: it is a host-wide condition (no CLI,
    no session), not this issue's, and swallowing it would make every issue look
    link-free at once. Anything else is logged at DEBUG and degrades this issue.

    Goes through ``gitlab_client.list_issue_links``, the client's public read for this
    endpoint; tests stand that one function in.
    """
    try:
        data: object = gitlab_client.list_issue_links(owner, repo, iid, host=host, timeout=timeout)
    except ProviderSetupError:
        raise
    except ProviderCliError as exc:
        logger.debug(
            "issue-radar: gitlab /links failed for %s/%s#%s; skipping issue: %s",
            owner,
            repo,
            iid,
            exc,
        )
        return None
    if not isinstance(data, list):
        return [], 0

    projected: list[dict] = []
    rows_over_max = 0
    for raw in data:
        if not isinstance(raw, dict):
            continue
        if len(projected) >= _MAX_LINKS_PER_ISSUE:
            rows_over_max += 1
            continue

        linked_iid_raw = raw.get("iid")
        linked_iid = linked_iid_raw if isinstance(linked_iid_raw, int) else 0
        link_type_raw = str(raw.get("link_type") or "")
        references = raw.get("references")
        relative_raw = references.get("relative") if isinstance(references, dict) else ""
        relative = f"#{linked_iid}" if relative_raw == f"#{linked_iid}" else ""
        row = {
            "link_type": link_type_raw if link_type_raw in _BLOCKING_LINK_TYPES else "",
            "iid": linked_iid,
            "relative": relative,
        }
        if "state" in raw:
            row["state"] = _dep_node_state(raw.get("state"))
        if "title" in raw:
            row["title"] = _bounded_title(raw.get("title"))
        projected.append(row)
    return projected, rows_over_max


def fetch_dependency_edges(
    owner: str,
    repo: str,
    open_issues: list[dict],
    *,
    host: str,
    timeout: float = _LINKS_TIMEOUT_SEC,
) -> tuple[list[dict], dict[str, dict]]:
    """Build dependency edges for a GitLab project from its issue links.

    Returns ``(edges, nodes)`` in the exact shape ``store.write_deps_cache`` expects:

    * ``edges``: ``[{"blocked": int, "blocker": int, "source": "native"}]``
    * ``nodes``:  ``{str(number): {"kind": str, "state": str, "title": str}}``

    GitLab has no timeline-inferred edges; every edge here has ``source="native"``, and
    there are no node hints to merge: every node is seeded from ``open_issues`` or from
    the link payload itself (GitLab issues have no pull-request counterpart here).

    Deduplication (a ``blocks`` A→B pair and a matching ``is_blocked_by`` B→A
    from the other side of the same link) is handled by ``store._normalize_deps``
    on write, so the fetcher may emit both directions — the store's native-wins
    dedup collapses them.
    """
    numbers: list[int] = []
    open_rows: dict[int, dict] = {}
    open_issue_nodes_over_max = 0
    for row in open_issues:
        if isinstance(row, dict) and isinstance(row.get("number"), int) and row["number"] > 0:
            if len(open_rows) >= _MAX_DEPS_NODES:
                # Past the node bound the row is neither seeded nor walked: the
                # graph is served partial rather than refused.
                open_issue_nodes_over_max += 1
                continue
            n = int(row["number"])
            numbers.append(n)
            open_rows[n] = row

    # Seed nodes from the open-issue list (kind is always "issue" for GitLab
    # issues; there are no PR/MR entries in the open-issue scope).
    nodes: dict[int, dict] = {}
    for n, row in open_rows.items():
        nodes[n] = {
            "kind": "issue",
            "state": _dep_node_state(row.get("state")),
            "title": _bounded_title(row.get("title")),
        }
    raw_edges: list[dict] = []
    capped = numbers[:_MAX_LINKS_CALLS]
    dropped = len(numbers) - len(capped)
    if dropped:
        logger.warning(
            "issue-radar: gitlab deps for %s/%s fetched links for %d of %d open issues; "
            "%d past _MAX_LINKS_CALLS carry no edges in this graph",
            owner,
            repo,
            len(capped),
            len(numbers),
            dropped,
        )

    failures = 0
    link_rows_over_per_issue_max = 0
    edges_over_max = 0
    nodes_over_max = 0
    with ThreadPoolExecutor(max_workers=_LINKS_WORKERS) as pool:
        future_to_iid = {
            pool.submit(_fetch_issue_links, owner, repo, n, host=host, timeout=timeout): n
            for n in capped
        }
        for future in as_completed(future_to_iid):
            iid = future_to_iid.pop(future)
            try:
                result = future.result()
            except ProviderSetupError:
                # Host-wide: every remaining call would fail the same way, so stop
                # submitting and let the route turn this into its 502.
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            if result is None:
                failures += 1
                continue
            links, rows_over_max = result
            link_rows_over_per_issue_max += rows_over_max
            for link in links:
                link_type = link["link_type"]
                if link_type not in _BLOCKING_LINK_TYPES:
                    continue
                linked_iid = link["iid"]
                if linked_iid <= 0 or link["relative"] != f"#{linked_iid}":
                    continue
                if link_type == "is_blocked_by":
                    # This issue (iid) is blocked by linked_iid.
                    edge = {"blocked": iid, "blocker": linked_iid, "source": "native"}
                else:
                    # link_type == "blocks": this issue (iid) blocks linked_iid.
                    edge = {"blocked": linked_iid, "blocker": iid, "source": "native"}
                if len(raw_edges) >= _MAX_DEPS_EDGES:
                    edges_over_max += 1
                else:
                    raw_edges.append(edge)
                if linked_iid in nodes:
                    # Fresh /links fields win over the possibly cached issue-list row.
                    if "state" in link:
                        nodes[linked_iid]["state"] = link["state"]
                    if "title" in link:
                        nodes[linked_iid]["title"] = link["title"]
                else:
                    if len(nodes) >= _MAX_DEPS_NODES:
                        nodes_over_max += 1
                    else:
                        nodes[linked_iid] = {
                            "kind": "issue",
                            "state": link.get("state", "open"),
                            "title": link.get("title", ""),
                        }

    _warn_if_deps_truncated(
        owner,
        repo,
        open_issue_nodes_over_max=open_issue_nodes_over_max,
        link_rows_over_per_issue_max=link_rows_over_per_issue_max,
        edges_over_max=edges_over_max,
        nodes_over_max=nodes_over_max,
    )

    if capped and failures > len(capped) * _LINKS_FAILURE_RATIO:
        # A mostly-failed walk is not a graph, it is a missing result. Every issue
        # failing means the host is unreachable, dropped from the allowlist, or
        # refusing the whole project; most of them failing (rate limiting that set in
        # partway, a host that fell over mid-walk) leaves a few survivors that look
        # like a complete, nearly empty graph. Either way returning what came back
        # would let the route persist it over the previous good graph and serve it
        # as fresh for the cache TTL, which is the harm this guard exists to prevent.
        # Raise so the route answers ``deps_fetch_failed`` and the cache stays intact.
        if failures == len(capped):
            logger.warning(
                "issue-radar: gitlab /links failed for every one of %d open issues in %s/%s; "
                "keeping the previous dependency graph",
                len(capped),
                owner,
                repo,
            )
            raise ProviderCliError(
                f"gitlab issue links unavailable for {owner}/{repo}: "
                f"all {len(capped)} /links calls failed"
            )
        logger.warning(
            "issue-radar: gitlab /links failed for %d of %d open issues in %s/%s; "
            "keeping the previous dependency graph",
            failures,
            len(capped),
            owner,
            repo,
        )
        raise ProviderCliError(
            f"gitlab issue links unavailable for {owner}/{repo}: "
            f"{failures} of {len(capped)} /links calls failed"
        )

    return raw_edges, {str(n): v for n, v in nodes.items()}
