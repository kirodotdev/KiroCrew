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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

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

#: Per-call timeout forwarded to ``gitlab_client.list_issue_links``.
_LINKS_TIMEOUT_SEC = 15.0


def _dep_node_state(state: object) -> str:
    """Normalize a GitLab issue state into the vocabulary the frontend expects.

    GitLab issues use ``"opened"`` / ``"closed"``.  There is no ``"merged"``
    concept for issues (only for MRs), so this maps to ``"open"`` / ``"closed"``.
    The store's ``_normalize_deps`` and the frontend's ``resolveLifecycle`` both
    accept these values.
    """
    normalized = str(state or "").lower()
    return "closed" if normalized == "closed" else "open"


def _fetch_issue_links(
    owner: str,
    repo: str,
    iid: int,
    *,
    host: str,
    timeout: float,
    run_api: Callable[..., object] | None,
) -> list[dict]:
    """Fetch the links for one issue; ``[]`` when that one issue's call fails.

    A :class:`ProviderSetupError` propagates: it is a host-wide condition (no CLI,
    no session), not this issue's, and swallowing it would make every issue look
    link-free at once. Anything else is logged at DEBUG and degrades this issue.

    Production goes through ``gitlab_client.list_issue_links``, the client's public
    read for this endpoint. ``run_api`` is the test seam: a path-taking callable
    standing in for the raw ``glab api`` call.
    """
    from . import gitlab_client  # deferred to allow test injection via run_api

    try:
        if run_api is None:
            data: object = gitlab_client.list_issue_links(
                owner, repo, iid, host=host, timeout=timeout
            )
        else:
            path = f"projects/{gitlab_client.project_path(owner, repo)}/issues/{int(iid)}/links"
            data = run_api(path)
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
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def fetch_dependency_edges(
    owner: str,
    repo: str,
    open_issues: list[dict],
    node_hints: dict[int, dict] | None,
    *,
    host: str,
    timeout: float = _LINKS_TIMEOUT_SEC,
    run_api: Callable[..., object] | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """Build dependency edges for a GitLab project from its issue links.

    Returns ``(edges, nodes)`` in the exact shape ``store.write_deps_cache`` expects:

    * ``edges``: ``[{"blocked": int, "blocker": int, "source": "native"}]``
    * ``nodes``:  ``{str(number): {"kind": str, "state": str, "title": str}}``

    GitLab has no timeline-inferred edges; every edge here has ``source="native"``.

    Deduplication (a ``blocks`` A→B pair and a matching ``is_blocked_by`` B→A
    from the other side of the same link) is handled by ``store._normalize_deps``
    on write, so the fetcher may emit both directions — the store's native-wins
    dedup collapses them.

    ``run_api`` is injected for testing; production callers omit it and the
    links come from ``gitlab_client.list_issue_links``.
    """
    hints = dict(node_hints or {})
    numbers: list[int] = []
    open_rows: dict[int, dict] = {}
    for row in open_issues:
        if isinstance(row, dict) and isinstance(row.get("number"), int) and row["number"] > 0:
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
            "title": str(row.get("title") or ""),
        }
    # Fall back to hints for anything not in open_rows (e.g. the pulls cache).
    for n, hint in hints.items():
        if n not in nodes:
            nodes[n] = dict(hint)

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

    with ThreadPoolExecutor(max_workers=_LINKS_WORKERS) as pool:
        future_to_iid = {
            pool.submit(
                _fetch_issue_links, owner, repo, n, host=host, timeout=timeout, run_api=run_api
            ): n
            for n in capped
        }
        for future in as_completed(future_to_iid):
            iid = future_to_iid[future]
            try:
                links = future.result()
            except ProviderSetupError:
                # Host-wide: every remaining call would fail the same way, so stop
                # submitting and let the route turn this into its 502.
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            for link in links:
                link_type = str(link.get("link_type") or "")
                if link_type not in _BLOCKING_LINK_TYPES:
                    continue
                linked_iid_raw = link.get("iid")
                if not isinstance(linked_iid_raw, int) or linked_iid_raw <= 0:
                    continue
                linked_iid = int(linked_iid_raw)
                references = link.get("references")
                if (
                    not isinstance(references, dict)
                    or references.get("relative") != f"#{linked_iid}"
                ):
                    continue
                if link_type == "is_blocked_by":
                    # This issue (iid) is blocked by linked_iid.
                    edge = {"blocked": iid, "blocker": linked_iid, "source": "native"}
                else:
                    # link_type == "blocks": this issue (iid) blocks linked_iid.
                    edge = {"blocked": linked_iid, "blocker": iid, "source": "native"}
                raw_edges.append(edge)
                # Seed the linked issue's node from the link payload if not yet known.
                if linked_iid not in nodes:
                    nodes[linked_iid] = {
                        "kind": "issue",
                        "state": _dep_node_state(link.get("state")),
                        "title": str(link.get("title") or ""),
                    }

    return raw_edges, {str(n): v for n, v in nodes.items()}
