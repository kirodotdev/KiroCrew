"""Bitbucket Server / Data Center pull requests, read over the instance's REST API.

Bitbucket Server has no CLI to lean on, so the gateway calls the instance itself,
as it does for Jira: HTTPS to the exact allowlisted host only, no redirects
followed, every response size-bounded, and the token sent only in the request
header. The provider implements the registered-provider protocol and is served
through that seam (see ``plugins``), so the shared cache, redaction and payload
cap apply to it, and every write it does not implement is refused there. It is
read-only: overview, commits, files, comments and build status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypeVar, cast
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout

from kiro_crew.config import loader as config_loader
from kiro_crew.dashboard.handlers import _shared
from kiro_crew.dashboard.source_providers import (
    LOGGER_NAME,
    contract,
    hosts,
    plugins,
    projection,
    runner,
)
from kiro_crew.dashboard.source_providers.contract import (
    SourceChangePayload,
    SourceProviderError,
    SourceProviderNotConfigured,
    SourceRef,
)
from kiro_crew.secrets import SecretVault

logger = logging.getLogger(LOGGER_NAME)

_ResultT = TypeVar("_ResultT")

_FETCH_TIMEOUT_SECS = 15
#: The token used when exactly one Bitbucket Server host is listed.
GLOBAL_TOKEN_NAME = contract.BITBUCKET_SERVER_TOKEN_NAME
_PAGE_LIMIT = projection._SECONDARY_PAGE_SIZE
_CHANGES_PAGE_LIMIT = 500
_MAX_COMMENTS = 300
_MAX_REPLY_DEPTH = 8
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}")
_STATE_MAP = {"OPEN": "OPEN", "MERGED": "MERGED", "DECLINED": "CLOSED"}
_CHECK_BUCKETS = {
    "SUCCESSFUL": "passed",
    "FAILED": "failed",
    "CANCELLED": "failed",
    "INPROGRESS": "pending",
}
_CHANGE_STATUS = {
    "ADD": "added",
    "DELETE": "removed",
    "MOVE": "renamed",
    "COPY": "copied",
}
_REVIEW_STATES = {"APPROVED": "APPROVED", "REVIEWED": "CHANGES_REQUESTED"}


def host_token_name(host: str) -> str:
    """The per-host vault name for *host*: ``BITBUCKET_SERVER_TOKEN_<HEX>``."""
    normalized = config_loader.normalize_jira_host(host)
    return f"{GLOBAL_TOKEN_NAME}_{normalized.encode().hex().upper()}"


def _vault_secret(name: str) -> str:
    """Return the vault secret *name*, or ``""`` when absent or unreadable."""
    try:
        secret = SecretVault(config_loader.config_dir()).get(name)
    except Exception:
        logger.debug("Bitbucket Server token unavailable", exc_info=True)
        return ""
    return secret.reveal() if secret is not None else ""


def _require_listed(host: str) -> None:
    """Refuse *host* unless it is in the current Bitbucket Server allowlist."""
    if host not in hosts._allowed_bitbucket_server_hosts():
        raise SourceProviderError(f"{host} is not listed in dashboard.bitbucket_server_hosts.")


def _resolve_token(host: str) -> str:
    """The token for *host*: its per-host vault entry, else the single-host one.

    BLOCKING (a vault read), so callers run it in a worker thread. The global
    name serves only when *host* is the one listed host, so adding a second
    instance never hands the first instance's token to it.
    """
    token = _vault_secret(host_token_name(host))
    if token:
        return token
    if hosts._allowed_bitbucket_server_hosts() == frozenset({host}):
        return _vault_secret(GLOBAL_TOKEN_NAME)
    return ""


def _token_secret_name(host: str) -> str:
    """The vault name to tell the user about: the lone-host name, else per-host."""
    if hosts._allowed_bitbucket_server_hosts() == frozenset({host}):
        return GLOBAL_TOKEN_NAME
    return host_token_name(host)


def token_secret_names() -> frozenset[str]:
    """Every vault name a token error can tell the reader about, from config alone.

    One per listed host, by the rule :func:`_token_secret_name` names them. A
    value on a provider error ships to the client as ``secretName`` only when it
    is one of these: its shape cannot prove it is a name and not an opaque token
    the provider repeated in its own text, but a name derived from the operator's
    allowlist carries nothing a provider supplied.
    """
    return frozenset(_token_secret_name(host) for host in hosts._allowed_bitbucket_server_hosts())


def _setup_guidance(host: str) -> str:
    name = _token_secret_name(host)
    if name == GLOBAL_TOKEN_NAME:
        # A lone listed host: one secret to store, so name only that one.
        note = ""
    else:
        # The shared name serves only a lone listed host, so name it as the
        # likely cause when a second host was added after it was stored.
        note = f"; {GLOBAL_TOKEN_NAME} stops applying once a second host is listed"
    return (
        f"Bitbucket Server at {host} needs an HTTP access token with read access. "
        f"Store it in Settings > Secrets as {name}{note}."
    )


def _replace_token_guidance(host: str) -> str:
    return (
        f"Replace {_token_secret_name(host)} in Settings > Secrets with a token "
        "that has repository read access."
    )


def _iso(value: Any) -> str:
    """Render a Bitbucket millisecond timestamp as ISO-8601 UTC, ``""`` if absent."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return ""
    try:
        moment = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.isoformat().replace("+00:00", "Z")


def _person(value: Any) -> str:
    user = projection._as_dict(value)
    return str(user.get("displayName") or user.get("name") or user.get("slug") or "")


class _Client:
    """One fetch's view of one instance: the base URL and an open session."""

    def __init__(self, ref: SourceRef, session: ClientSession, *, has_token: bool) -> None:
        self.ref = ref
        self.session = session
        self.has_token = has_token
        base = f"https://{ref.host}{ref.project}"
        self.base = base
        self.repo_api = (
            f"{base}/rest/api/1.0/projects/{quote(ref.owner, safe='')}"
            f"/repos/{quote(ref.repo, safe='')}"
        )
        self.pr_api = f"{self.repo_api}/pull-requests/{ref.number}"

    async def get(self, url: str, *, cap: int) -> Any:
        """GET one JSON document, mapping every failure to a provider error.

        The allowlist is re-read before each request, so once the operator
        removes the host no further request in this fetch reaches it.
        """
        host = self.ref.host
        _require_listed(host)
        try:
            async with self.session.get(url, allow_redirects=False) as resp:
                if resp.status in {401, 403}:
                    # Every remedy here names the token's vault entry, so it also
                    # rides as its own field: with two or more hosts listed the
                    # per-host name runs past 60 characters, and the panel shows
                    # it as copyable text rather than inside the wrapped sentence.
                    secret_name = _token_secret_name(host)
                    if not self.has_token:
                        raise SourceProviderNotConfigured(
                            _setup_guidance(host), secret_name=secret_name
                        )
                    if resp.status == 401:
                        raise SourceProviderError(
                            f"Bitbucket Server at {host} rejected the configured token. "
                            f"{_replace_token_guidance(host)}",
                            secret_name=secret_name,
                        )
                    raise SourceProviderError(
                        f"The Bitbucket Server token for {host} cannot read this pull request. "
                        f"{_replace_token_guidance(host)}",
                        secret_name=secret_name,
                    )
                if resp.status == 404:
                    raise SourceProviderError(
                        f"Bitbucket Server at {host} has no such pull request or build status."
                    )
                if resp.status != 200:
                    raise SourceProviderError(
                        f"Bitbucket Server at {host} returned HTTP {resp.status}. Try again later."
                    )
                body = await _shared.read_capped_response(resp, cap)
        except SourceProviderError:
            raise
        except (ClientError, asyncio.TimeoutError) as exc:
            raise SourceProviderError(
                f"Could not reach Bitbucket Server at {host}: {type(exc).__name__}"
            ) from exc
        if len(body) > cap:
            raise SourceProviderError(f"Bitbucket Server response from {host} is too large.")
        try:
            return json.loads(body)
        except (ValueError, RecursionError) as exc:
            raise SourceProviderError(
                f"Bitbucket Server at {host} returned an unparseable response."
            ) from exc

    async def pull_request(self) -> dict[str, Any]:
        data = await self.get(self.pr_api, cap=runner._METADATA_OUTPUT_BYTES)
        if not isinstance(data, dict):
            raise SourceProviderError("Bitbucket Server returned an invalid pull-request payload")
        return data

    async def build_statuses(self, sha: str) -> tuple[list[dict[str, Any]], bool]:
        """The head commit's builds as check rows, and whether the page was complete."""
        if not _SHA_RE.fullmatch(sha):
            return [], True
        data = await self.get(
            f"{self.base}/rest/build-status/1.0/commits/{sha}?limit={_PAGE_LIMIT}",
            cap=runner._CHECKS_OUTPUT_BYTES,
        )
        page = projection._as_dict(data)
        rows = [_build_check(item) for item in projection._as_list(page.get("values"))]
        return rows, page.get("isLastPage", True) is not False


def _build_check(item: dict[str, Any]) -> dict[str, Any]:
    state = str(item.get("state") or "").upper()
    return {
        "name": str(item.get("name") or item.get("key") or "Build"),
        "workflow": "",
        "status": state,
        "conclusion": state,
        # An unrecognized state is never read as green.
        "bucket": _CHECK_BUCKETS.get(state, "pending"),
        "url": projection._safe_https_url(item.get("url")),
        "startedAt": _iso(item.get("dateAdded")),
        "completedAt": "",
    }


def _head_sha(pr: dict[str, Any]) -> str:
    return str(projection._as_dict(pr.get("fromRef")).get("latestCommit") or "")


def _chip_lifecycle(pr: dict[str, Any]) -> str | None:
    raw = _STATE_MAP.get(str(pr.get("state") or "").upper(), "")
    return projection._project_state(raw, draft=bool(pr.get("draft")))


def _commit_rows(client: _Client, page: Any) -> list[dict[str, Any]]:
    rows = []
    for item in projection._as_list(projection._as_dict(page).get("values")):
        sha = str(item.get("id") or "")
        message = str(item.get("message") or "")
        title, _, body = message.partition("\n")
        rows.append(
            {
                "sha": sha,
                "title": title,
                "body": body.strip(),
                "author": _person(item.get("author")),
                "date": _iso(item.get("authorTimestamp")),
                "url": (
                    f"{client.base}/projects/{quote(client.ref.owner, safe='')}"
                    f"/repos/{quote(client.ref.repo, safe='')}/commits/{sha}"
                    if _SHA_RE.fullmatch(sha)
                    else ""
                ),
            }
        )
    return rows


def _diff_path(side: Any) -> str:
    return str(projection._as_dict(side).get("toString") or "")


def _patches(diff: Any) -> tuple[dict[str, tuple[str, int, int]], bool]:
    """Unified-diff text and line counts per destination path, and a truncation flag."""
    document = projection._as_dict(diff)
    truncated = bool(document.get("truncated"))
    out: dict[str, tuple[str, int, int]] = {}
    marks = {"ADDED": "+", "REMOVED": "-", "CONTEXT": " "}
    for entry in projection._as_list(document.get("diffs")):
        path = _diff_path(entry.get("destination")) or _diff_path(entry.get("source"))
        if not path:
            continue
        lines: list[str] = []
        additions = deletions = 0
        for hunk in projection._as_list(entry.get("hunks")):
            lines.append(
                f"@@ -{projection._int_or_zero(hunk.get('sourceLine'))},"
                f"{projection._int_or_zero(hunk.get('sourceSpan'))} "
                f"+{projection._int_or_zero(hunk.get('destinationLine'))},"
                f"{projection._int_or_zero(hunk.get('destinationSpan'))} @@"
            )
            for segment in projection._as_list(hunk.get("segments")):
                mark = marks.get(str(segment.get("type") or ""), " ")
                for line in projection._as_list(segment.get("lines")):
                    lines.append(f"{mark}{line.get('line') or ''}")
                    if mark == "+":
                        additions += 1
                    elif mark == "-":
                        deletions += 1
        truncated = truncated or bool(entry.get("truncated"))
        out[path] = ("\n".join(lines), additions, deletions)
    return out, truncated


def _file_rows(changes: Any, diff: Any, partial: list[str]) -> list[dict[str, Any]]:
    page = projection._as_dict(changes)
    if page.get("isLastPage", True) is False:
        projection._mark_partial(partial, "files")
    patches, truncated = _patches(diff)
    if truncated:
        projection._mark_partial(partial, "files")
    rows = []
    for item in projection._as_list(page.get("values")):
        path = _diff_path(item.get("path"))
        if not path:
            continue
        patch, additions, deletions = patches.get(path, ("", 0, 0))
        rows.append(
            {
                "path": path,
                "status": _CHANGE_STATUS.get(str(item.get("type") or "").upper(), "modified"),
                "additions": additions,
                "deletions": deletions,
                "patch": patch,
            }
        )
    return rows


def _comment_rows(client: _Client, activities: Any, partial: list[str]) -> list[dict[str, Any]]:
    """Flatten the activity stream into comment, inline and review rows, oldest first."""
    page = projection._as_dict(activities)
    if page.get("isLastPage", True) is False:
        projection._mark_partial(partial, "review discussions")
    rows: list[dict[str, Any]] = []

    def add_comment(
        comment: dict[str, Any], thread: str, anchor: dict[str, Any], depth: int
    ) -> None:
        if len(rows) >= _MAX_COMMENTS:
            projection._mark_partial(partial, "review discussions")
            return
        comment_id = str(comment.get("id") or "")
        path = str(anchor.get("path") or "")
        line = anchor.get("line")
        rows.append(
            {
                "id": comment_id,
                "kind": "inline" if path else "comment",
                "author": _person(comment.get("author")),
                "body": str(comment.get("text") or ""),
                "state": "",
                "createdAt": _iso(comment.get("createdDate")),
                "url": f"{client.ref.url}/overview?commentId={comment_id}" if comment_id else "",
                "path": path,
                "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
                "threadId": thread,
                "resolvable": False,
                "resolved": False,
            }
        )
        if depth >= _MAX_REPLY_DEPTH:
            return
        for reply in projection._as_list(comment.get("comments")):
            add_comment(reply, thread, anchor, depth + 1)

    for activity in reversed(projection._as_list(page.get("values"))):
        action = str(activity.get("action") or "")
        if action == "COMMENTED":
            comment = projection._as_dict(activity.get("comment"))
            if comment:
                add_comment(
                    comment,
                    str(comment.get("id") or ""),
                    projection._as_dict(activity.get("commentAnchor")),
                    0,
                )
        elif action in _REVIEW_STATES:
            if len(rows) >= _MAX_COMMENTS:
                projection._mark_partial(partial, "review discussions")
                continue
            rows.append(
                {
                    "id": str(activity.get("id") or ""),
                    "kind": "review",
                    "author": _person(activity.get("user")),
                    "body": "",
                    "state": _REVIEW_STATES[action],
                    "createdAt": _iso(activity.get("createdDate")),
                    "url": "",
                    "path": "",
                    "line": None,
                    "threadId": "",
                    "resolvable": False,
                    "resolved": False,
                }
            )
    return rows


async def _with_client(ref: SourceRef, work: Callable[[_Client], Awaitable[_ResultT]]) -> _ResultT:
    """Run *work(client)* against *ref*'s instance, re-checking the allowlist first.

    The host was authorized when the URL was parsed; checking again here means a
    host the operator removed since then is not contacted, and its token is not
    read, by a request already in flight.
    """
    _require_listed(ref.host)
    token = await asyncio.to_thread(_resolve_token, ref.host)
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    timeout = ClientTimeout(total=_FETCH_TIMEOUT_SECS)
    async with ClientSession(timeout=timeout, headers=headers) as session:
        return await work(_Client(ref, session, has_token=bool(token)))


async def _fetch_pull_request(ref: SourceRef) -> dict[str, Any]:
    async def work(client: _Client) -> dict[str, Any]:
        pr = await client.pull_request()
        partial: list[str] = []
        head = _head_sha(pr)
        commits, changes, diff, activities, builds = await asyncio.gather(
            client.get(
                f"{client.pr_api}/commits?limit={_PAGE_LIMIT}", cap=runner._METADATA_OUTPUT_BYTES
            ),
            client.get(
                f"{client.pr_api}/changes?limit={_CHANGES_PAGE_LIMIT}",
                cap=runner._METADATA_OUTPUT_BYTES,
            ),
            client.get(
                f"{client.pr_api}/diff?contextLines=3&withComments=false",
                cap=runner._DIFF_OUTPUT_BYTES,
            ),
            client.get(
                f"{client.pr_api}/activities?limit={_PAGE_LIMIT}",
                cap=runner._DISCUSSION_OUTPUT_BYTES,
            ),
            client.build_statuses(head),
            return_exceptions=True,
        )
        # Secondary reads degrade to an empty, partial-marked section; the
        # pull request read above already carries the core fields.
        for value, section in (
            (commits, "commits"),
            (changes, "files"),
            (diff, "files"),
            (activities, "review discussions"),
            (builds, "checks"),
        ):
            if isinstance(value, BaseException):
                if isinstance(value, SourceProviderNotConfigured):
                    raise value
                projection._mark_partial(partial, section)
        commit_page = projection._or_empty(commits)
        if projection._as_dict(commit_page).get("isLastPage", True) is False:
            projection._mark_partial(partial, "commits")
        checks: list[dict[str, Any]] = []
        if isinstance(builds, tuple):
            checks, complete = builds
            if not complete:
                projection._mark_partial(partial, "checks")
        files = _file_rows(projection._or_empty(changes), projection._or_empty(diff), partial)
        state = _STATE_MAP.get(str(pr.get("state") or "").upper(), "")
        from_ref = projection._as_dict(pr.get("fromRef"))
        to_ref = projection._as_dict(pr.get("toRef"))
        return {
            "provider": contract.BITBUCKET_SERVER_PROVIDER,
            # Identity comes from the validated ref, never from the instance's echo.
            "url": ref.url,
            "number": ref.number,
            "title": str(pr.get("title") or ""),
            "description": str(pr.get("description") or ""),
            "state": state,
            "draft": bool(pr.get("draft")),
            "mergedAt": _iso(pr.get("closedDate")) if state == "MERGED" else "",
            "mergeable": "",
            "mergeStateStatus": "",
            "autoMerge": False,
            "updatedAt": _iso(pr.get("updatedDate")),
            "headBranch": str(from_ref.get("displayId") or ""),
            "baseBranch": str(to_ref.get("displayId") or ""),
            "headSha": head,
            "author": _person(projection._as_dict(pr.get("author")).get("user")),
            "additions": sum(row["additions"] for row in files),
            "deletions": sum(row["deletions"] for row in files),
            "changedFiles": len(files),
            "commits": _commit_rows(client, commit_page),
            "checks": checks,
            "comments": _comment_rows(client, projection._or_empty(activities), partial),
            "files": files,
            "partialSections": partial,
        }

    return await _with_client(ref, work)


async def _fetch_checks(ref: SourceRef) -> list[dict[str, Any]]:
    async def work(client: _Client) -> list[dict[str, Any]]:
        rows, _complete = await client.build_statuses(_head_sha(await client.pull_request()))
        return rows

    return await _with_client(ref, work)


async def _fetch_chip_status(ref: SourceRef) -> dict[str, str]:
    """The chip's ``{state, ci, ciPending}``, from the same reads the full payload projects."""

    async def work(client: _Client) -> dict[str, str]:
        pr = await client.pull_request()
        status: dict[str, str] = {}
        state = _chip_lifecycle(pr)
        if state is not None:
            status["state"] = state
        try:
            rows, _complete = await client.build_statuses(_head_sha(pr))
        except SourceProviderNotConfigured:
            raise
        except SourceProviderError:
            return status
        projection._record_ci(status, rows)
        return status

    return await _with_client(ref, work)


class BitbucketServerProvider:
    """The core Bitbucket Server provider, served through the plugin seam.

    ``parse`` declines every URL: ``parse_source_url`` recognizes Bitbucket
    Server links itself, from the operator's allowlist, before any plugin is
    asked. ``setup_message`` is empty so the seam shows the per-host guidance
    each not-configured error carries.
    """

    id = contract.BITBUCKET_SERVER_PROVIDER

    def parse(self, raw_url: str) -> SourceRef | None:
        return None

    async def fetch_full(self, ref: SourceRef, *, refresh: bool = False) -> SourceChangePayload:
        return cast(SourceChangePayload, await _fetch_pull_request(ref))

    async def fetch_checks(self, ref: SourceRef) -> list[dict[str, Any]]:
        return await _fetch_checks(ref)

    async def fetch_check_status(self, ref: SourceRef) -> dict[str, str]:
        return await _fetch_chip_status(ref)

    def setup_message(self) -> str:
        return ""


PROVIDER = BitbucketServerProvider()
plugins._install_core_seam_provider(PROVIDER)
