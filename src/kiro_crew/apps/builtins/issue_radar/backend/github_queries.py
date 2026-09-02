"""GitHub-specific dependency, enrichment, and search queries."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable
from urllib.parse import quote

from .errors import ProviderCliError, PrSearchError

GhCliError = ProviderCliError
logger = logging.getLogger(__name__)

DEP_ISSUE_JQ = (
    ".[] | {number: .number, title: .title, state: .state, " "is_pr: (.pull_request != null)}"
)
DEPS_GRAPHQL_MAX_PAGES = 40

GRAPHQL_PR_STATES = {"open": "OPEN", "closed": "CLOSED, MERGED"}
ROLLUP_CONTEXT_PAGE = 100
ROLLUP_CONTEXTS_JQ = (
    "[(.commits.nodes[0].commit.statusCheckRollup.contexts.nodes[]? | "
    '{name: ((.name // .context) // ""), '
    'source: ((.checkSuite.app.slug // .checkSuite.app.name) // "status"), '
    "status: (.status // null), "
    "conclusion: ((.conclusion // .state) // null), "
    "started_at: ((.startedAt // .createdAt) // null), "
    "completed_at: (.completedAt // null)})]"
)
PR_SUMMARY_SELECTION = (
    " number additions deletions changedFiles"
    " mergeable state mergedAt"
    " commits(last:1){nodes{commit{oid statusCheckRollup{state"
    f"  contexts(first:{ROLLUP_CONTEXT_PAGE}){{pageInfo{{hasNextPage}} nodes{{ __typename"
    "   ... on CheckRun{name conclusion status startedAt completedAt"
    "    checkSuite{app{slug name}}}"
    "   ... on StatusContext{context state createdAt} }}}}}}"
)
PR_READINESS_SELECTION = " number mergeStateStatus"
PR_READINESS_JQ_BODY = "{number: .number, merge_state_status: (.mergeStateStatus // null)}"
READINESS_BATCH = 50
PR_SUMMARY_JQ_BODY = (
    "{number: .number, additions: .additions, deletions: .deletions, "
    "changed_files: (.changedFiles // 0), "
    "mergeable_raw: (.mergeable // null), "
    "pr_state: (.state // null), "
    "pr_merged_at: (.mergedAt // null), "
    "head_sha: (.commits.nodes[0].commit.oid // null), "
    "rollup: (.commits.nodes[0].commit.statusCheckRollup.state // null), "
    "contexts_truncated: "
    "(.commits.nodes[0].commit.statusCheckRollup.contexts.pageInfo.hasNextPage // false), "
    f"contexts: {ROLLUP_CONTEXTS_JQ}}}"
)
SUMMARY_BATCH = 100

PR_SEARCH_JQ = (
    ".items[] | {number: .number, title: .title, url: .html_url, "
    "state: .state, draft: (.draft // false), labels: [.labels[].name], "
    "author: (.user.login // null), "
    "author_association: (.author_association // null), "
    "updated_at: .updated_at, created_at: .created_at, "
    "closed_at: .closed_at, "
    "merged_at: (.pull_request.merged_at // null), "
    "assignees: [.assignees[].login], "
    "requested_reviewers: [], base: null, head: null, "
    "head_sha: null, "
    'body: (.body // "")}'
)
PR_SEARCH_MAX = 300
SEARCH_MAX_PAGES = 10
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
PR_STATE_QUALIFIERS = {
    "open": ["is:open"],
    "merged": ["is:merged"],
    "closed": ["is:closed", "is:unmerged"],
}


def is_deps_feature_absent(exc: GhCliError) -> bool:
    message = str(exc)
    return "HTTP 404" in message or "HTTP 410" in message


def list_issue_blocked_by(
    owner: str,
    repo: str,
    number: int,
    *,
    timeout: float,
    run_api: Callable[..., list[dict]],
    absent_classifier: Callable[[GhCliError], bool],
) -> list[dict]:
    path = f"repos/{owner}/{repo}/issues/{int(number)}/dependencies/blocked_by?per_page=100"
    try:
        rows = run_api(path, DEP_ISSUE_JQ, timeout=timeout, paginate=True)
    except GhCliError as exc:
        if absent_classifier(exc):
            return []
        raise
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("number"), int)]


def inferred_blockers_from_events(
    events: list[dict], owner: str, repo: str, number: int
) -> list[dict]:
    blockers: list[dict] = []
    prefix = f"/{owner}/{repo}/"
    for event in events:
        if not isinstance(event, dict) or event.get("kind") != "cross-referenced":
            continue
        source = event.get("source") or {}
        source_number = source.get("number")
        if not isinstance(source_number, int) or source_number <= 0 or source_number == number:
            continue
        if prefix not in str(source.get("url") or ""):
            continue
        blockers.append(
            {
                "number": source_number,
                "title": source.get("title"),
                "state": source.get("state"),
                "is_pr": bool(source.get("is_pr")),
            }
        )
    return blockers


def inferred_blockers_from_timeline(
    owner: str,
    repo: str,
    number: int,
    *,
    timeout: float,
    timeline_loader: Callable[..., list[dict]],
    absent_classifier: Callable[[GhCliError], bool],
    event_filter: Callable[[list[dict], str, str, int], list[dict]],
) -> list[dict]:
    try:
        events = timeline_loader(owner, repo, number, timeout=timeout)
    except GhCliError as exc:
        if absent_classifier(exc):
            return []
        raise
    return event_filter(events, owner, repo, number)


def batch_dependency_graph(
    owner: str,
    repo: str,
    *,
    timeout: float,
    gh_run: Callable[..., subprocess.CompletedProcess],
) -> dict[int, dict] | None:
    query = (
        "query($owner:String!,$name:String!,$after:String){"
        "repository(owner:$owner,name:$name){"
        "issues(states:OPEN,first:100,after:$after){"
        "pageInfo{hasNextPage endCursor}"
        "nodes{number title state "
        "blockedBy(first:50){pageInfo{hasNextPage} nodes{number title state}}"
        "timelineItems(itemTypes:[CROSS_REFERENCED_EVENT],first:100){pageInfo{hasNextPage} nodes{"
        "... on CrossReferencedEvent{source{"
        "... on Issue{number title state url repository{nameWithOwner}}"
        "... on PullRequest{number title state url merged repository{nameWithOwner}}"
        "}}}}}}}}"
    )

    def compact_row(node: dict) -> dict:
        state = str(node.get("state") or "open").lower()
        return {
            "number": node.get("number"),
            "title": node.get("title") or "",
            "state": "closed" if state in ("closed", "merged") else "open",
            "is_pr": "merged" in node,
            "merged_at": "merged" if node.get("merged") else None,
        }

    rows: dict[int, dict] = {}
    after: str | None = None
    full_name = f"{owner}/{repo}".lower()
    for _page in range(DEPS_GRAPHQL_MAX_PAGES):
        argv = [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={repo}",
        ]
        if after:
            argv += ["-F", f"after={after}"]
        proc = gh_run(["gh", *argv], timeout=timeout)
        if proc.returncode != 0:
            logger.info(
                "issue-radar: deps graphql batch unavailable for %s/%s; "
                "falling back to per-issue reads",
                owner,
                repo,
            )
            return None
        try:
            connection = json.loads(proc.stdout)["data"]["repository"]["issues"]
        except (ValueError, KeyError, TypeError):
            logger.info("issue-radar: deps graphql batch returned an unexpected shape")
            return None
        for node in connection.get("nodes") or []:
            if not isinstance(node, dict) or not isinstance(node.get("number"), int):
                continue
            number = node["number"]
            native = [
                compact_row(blocker)
                for blocker in (node.get("blockedBy") or {}).get("nodes") or []
                if isinstance(blocker, dict) and isinstance(blocker.get("number"), int)
            ]
            references: list[dict] = []
            for item in (node.get("timelineItems") or {}).get("nodes") or []:
                source = (item or {}).get("source") or {}
                source_repo = str(((source.get("repository") or {}).get("nameWithOwner")) or "")
                if not isinstance(source.get("number"), int) or source_repo.lower() != full_name:
                    continue
                references.append(
                    {
                        "kind": "cross-referenced",
                        "source": {
                            **compact_row(source),
                            "url": str(source.get("url") or ""),
                        },
                    }
                )
            rows[number] = {
                "row": compact_row(node),
                "native": native,
                "refs": references,
                "truncated": bool(
                    ((node.get("blockedBy") or {}).get("pageInfo") or {}).get("hasNextPage")
                    or ((node.get("timelineItems") or {}).get("pageInfo") or {}).get("hasNextPage")
                ),
            }
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return rows
        after = str(page_info.get("endCursor") or "") or None
        if after is None:
            return rows
    logger.info(
        "issue-radar: deps graphql batch exceeded %d pages for %s/%s; using partial graph",
        DEPS_GRAPHQL_MAX_PAGES,
        owner,
        repo,
    )
    return rows


def fetch_dependency_edges(
    owner: str,
    repo: str,
    open_issues: list[dict],
    node_hints: dict[int, dict] | None,
    *,
    timeout: float,
    batch_loader: Callable[..., dict[int, dict] | None],
    blockers_loader: Callable[..., list[dict]],
    inferred_loader: Callable[..., list[dict]],
    event_filter: Callable[[list[dict], str, str, int], list[dict]],
    ref_loader: Callable[..., dict],
    node_kind: Callable[[bool], str],
    node_state: Callable[..., str],
) -> tuple[list[dict], dict[str, dict]]:
    hints = dict(node_hints or {})
    open_rows: dict[int, dict] = {}
    numbers: list[int] = []
    for row in open_issues:
        if isinstance(row, dict) and isinstance(row.get("number"), int) and row["number"] > 0:
            number = int(row["number"])
            numbers.append(number)
            open_rows[number] = row
    edges: list[dict] = []
    nodes: dict[int, dict] = {}
    fresh: set[int] = set()

    def seed(number: int, row: dict | None, *, is_fresh: bool = False) -> None:
        if number in nodes and (number in fresh or not is_fresh):
            return
        if isinstance(row, dict):
            nodes[number] = {
                "kind": node_kind(bool(row.get("is_pr"))),
                "state": node_state(row.get("state"), row.get("merged_at")),
                "title": str(row.get("title") or ""),
            }
            if is_fresh:
                fresh.add(number)
        elif number in hints and number not in nodes:
            nodes[number] = dict(hints[number])

    for number in numbers:
        seed(number, open_rows.get(number))

    batch = batch_loader(owner, repo, timeout=timeout)
    for number in numbers:
        entry = batch.get(number) if batch is not None else None
        if entry is not None and not entry.get("truncated"):
            seed(number, entry.get("row"), is_fresh=True)
            native_rows = entry.get("native") or []
            inferred_rows = event_filter(entry.get("refs") or [], owner, repo, number)
        else:
            native_rows = blockers_loader(owner, repo, number, timeout=timeout)
            inferred_rows = inferred_loader(owner, repo, number, timeout=timeout)

        for blocker in native_rows:
            blocker_number = int(blocker["number"])
            edges.append({"blocked": number, "blocker": blocker_number, "source": "native"})
            seed(blocker_number, blocker, is_fresh=True)
        for blocker in inferred_rows:
            blocker_number = int(blocker["number"])
            edges.append(
                {
                    "blocked": number,
                    "blocker": blocker_number,
                    "source": "inferred",
                }
            )
            seed(blocker_number, blocker, is_fresh=True)

    referenced = {edge["blocked"] for edge in edges} | {edge["blocker"] for edge in edges}
    for number in sorted(referenced):
        if number in nodes:
            continue
        if number in hints:
            nodes[number] = dict(hints[number])
            continue
        try:
            summary = ref_loader(owner, repo, number, timeout=timeout)
        except GhCliError:
            logger.debug("issue-radar: deps node summary failed for #%s", number, exc_info=True)
            nodes[number] = {"kind": "issue", "state": "open", "title": ""}
            continue
        nodes[number] = {
            "kind": node_kind(bool(summary.get("is_pr"))),
            "state": node_state(summary.get("state"), summary.get("merged_at")),
            "title": str(summary.get("title") or ""),
        }
    return edges, {str(number): row for number, row in nodes.items()}


def parse_readiness_rows(
    stdout: str, *, lower: Callable[[object], str | None]
) -> dict[int, str | None]:
    rows: dict[int, str | None] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        number = row.get("number")
        if isinstance(number, int):
            rows[number] = lower(row.get("merge_state_status"))
    return rows


def fetch_pr_summaries(
    owner: str,
    repo: str,
    state: str,
    *,
    timeout: float,
    gh_run: Callable[..., subprocess.CompletedProcess],
    stderr_tail: Callable[[subprocess.CompletedProcess], str],
    parse_rows: Callable[[str], dict[int, dict]],
) -> dict[int, dict]:
    graphql_state = GRAPHQL_PR_STATES.get(state)
    if graphql_state is None:
        raise GhCliError(f"unsupported state for PR summaries: {state!r}")
    query = (
        "query($owner:String!,$name:String!){"
        " repository(owner:$owner,name:$name){"
        f"  pullRequests(states:[{graphql_state}], first:100,"
        "   orderBy:{field:UPDATED_AT,direction:DESC}){"
        "   nodes{" + PR_SUMMARY_SELECTION + " } } } }"
    )
    argv = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={query}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={repo}",
        "--jq",
        f".data.repository.pullRequests.nodes[] | {PR_SUMMARY_JQ_BODY}",
    ]
    proc = gh_run(argv, timeout=timeout)
    if proc.returncode != 0:
        tail = stderr_tail(proc)
        raise GhCliError(f"gh api graphql (pr summaries) failed (exit {proc.returncode}): {tail}")
    return parse_rows(proc.stdout or "")


def fetch_pr_readiness(
    owner: str,
    repo: str,
    state: str,
    *,
    timeout: float,
    gh_run: Callable[..., subprocess.CompletedProcess],
    stderr_tail: Callable[[subprocess.CompletedProcess], str],
    parse_rows: Callable[[str], dict[int, str | None]],
) -> dict[int, str | None]:
    graphql_state = GRAPHQL_PR_STATES.get(state)
    if graphql_state is None:
        raise GhCliError(f"unsupported state for PR readiness: {state!r}")
    query = (
        "query($owner:String!,$name:String!){"
        " repository(owner:$owner,name:$name){"
        f"  pullRequests(states:[{graphql_state}], first:100,"
        "   orderBy:{field:UPDATED_AT,direction:DESC}){"
        "   nodes{" + PR_READINESS_SELECTION + " } } } }"
    )
    argv = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={query}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={repo}",
        "--jq",
        f".data.repository.pullRequests.nodes[] | {PR_READINESS_JQ_BODY}",
    ]
    proc = gh_run(argv, timeout=timeout)
    if proc.returncode != 0:
        tail = stderr_tail(proc)
        raise GhCliError(f"gh api graphql (pr readiness) failed (exit {proc.returncode}): {tail}")
    return parse_rows(proc.stdout or "")


def fetch_pr_readiness_by_number(
    owner: str,
    repo: str,
    numbers: list[int],
    *,
    timeout: float,
    gh_run: Callable[..., subprocess.CompletedProcess],
    stderr_tail: Callable[[subprocess.CompletedProcess], str],
    parse_rows: Callable[[str], dict[int, str | None]],
) -> dict[int, str | None]:
    rows: dict[int, str | None] = {}
    wanted = [number for number in numbers if isinstance(number, int) and number > 0]
    for start in range(0, len(wanted), READINESS_BATCH):
        batch = wanted[start : start + READINESS_BATCH]
        fields = " ".join(
            f"p{number}: pullRequest(number:{number}){{{PR_READINESS_SELECTION} }}"
            for number in batch
        )
        query = (
            "query($owner:String!,$name:String!){"
            f" repository(owner:$owner,name:$name){{ {fields} }} }}"
        )
        argv = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={repo}",
            "--jq",
            ".data.repository | to_entries[] | .value | select(. != null) | "
            + PR_READINESS_JQ_BODY,
        ]
        proc = gh_run(argv, timeout=timeout)
        if proc.returncode != 0:
            tail = stderr_tail(proc)
            raise GhCliError(
                "gh api graphql (pr readiness by number) failed "
                f"(exit {proc.returncode}): {tail}"
            )
        rows.update(parse_rows(proc.stdout or ""))
    return rows


def fetch_pr_summaries_by_number(
    owner: str,
    repo: str,
    numbers: list[int],
    *,
    timeout: float,
    gh_run: Callable[..., subprocess.CompletedProcess],
    stderr_tail: Callable[[subprocess.CompletedProcess], str],
    parse_rows: Callable[[str], dict[int, dict]],
) -> dict[int, dict]:
    rows: dict[int, dict] = {}
    wanted = [number for number in numbers if isinstance(number, int) and number > 0]
    for start in range(0, len(wanted), SUMMARY_BATCH):
        batch = wanted[start : start + SUMMARY_BATCH]
        fields = " ".join(
            f"p{number}: pullRequest(number:{number}){{{PR_SUMMARY_SELECTION} }}"
            for number in batch
        )
        query = (
            "query($owner:String!,$name:String!){"
            f" repository(owner:$owner,name:$name){{ {fields} }} }}"
        )
        argv = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={repo}",
            "--jq",
            ".data.repository | to_entries[] | .value | select(. != null) | " + PR_SUMMARY_JQ_BODY,
        ]
        proc = gh_run(argv, timeout=timeout)
        if proc.returncode != 0:
            tail = stderr_tail(proc)
            raise GhCliError(
                "gh api graphql (pr summaries by number) failed "
                f"(exit {proc.returncode}): {tail}"
            )
        rows.update(parse_rows(proc.stdout or ""))
    return rows


def build_pr_search_query(
    owner: str,
    repo: str,
    *,
    state: str,
    author: str | None,
    assignee: str | None,
    review_requested: str | None,
) -> str:
    if state not in PR_STATE_QUALIFIERS:
        raise PrSearchError(f"unsupported state for PR search: {state!r}")
    parts = [f"repo:{owner}/{repo}", "is:pr", *PR_STATE_QUALIFIERS[state]]
    added = 0
    for qualifier, login in (
        ("author", author),
        ("assignee", assignee),
        ("review-requested", review_requested),
    ):
        if not login:
            continue
        if not LOGIN_RE.match(login):
            raise PrSearchError(f"invalid GitHub login: {login!r}")
        parts.append(f"{qualifier}:{login}")
        added += 1
    if added == 0:
        raise PrSearchError("PR search needs at least one person qualifier")
    return " ".join(parts)


def search_pulls(
    owner: str,
    repo: str,
    *,
    state: str,
    author: str | None,
    assignee: str | None,
    review_requested: str | None,
    timeout: float,
    limit: int,
    query_builder: Callable[..., str],
    run_api: Callable[..., list[dict]],
) -> list[dict]:
    query = query_builder(
        owner,
        repo,
        state=state,
        author=author,
        assignee=assignee,
        review_requested=review_requested,
    )
    cap = max(1, int(limit))
    per_page = min(100, cap)
    rows: list[dict] = []
    page = 1
    while len(rows) < cap and page <= SEARCH_MAX_PAGES:
        path = (
            f"search/issues?q={quote(query, safe='')}&sort=updated&order=desc"
            f"&per_page={per_page}&page={page}"
        )
        batch = run_api(path, PR_SEARCH_JQ, timeout=timeout, paginate=False)
        rows.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return rows[:cap]
