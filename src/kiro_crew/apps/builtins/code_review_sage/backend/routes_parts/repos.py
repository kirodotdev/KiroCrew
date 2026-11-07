"""Repo and pull request discovery, and the pinned-repo list.

The recent, reachable and review-requested listings through ``gh``, the pasted
pull request parser and the pinned-repo CRUD the sidebar renders. New discovery
endpoints go here.

Composed onto :mod:`kiro_crew.apps.builtins.code_review_sage.backend.routes`;
see this package's ``__init__``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.apps.builtins.code_review_sage.backend.routes import (
        adapters,
        discovery,
        logger,
        require_owner_dashboard_request,
        review_driver,
        web,
    )


# --- Repo + PR discovery -----------------------------------------------------
# So the user picks a PR instead of pasting a URL.


def _across_hosts(one: Callable[[str], dict]) -> dict:
    """Run a per-host discovery call on every configured GitHub host and merge.

    github.com keeps its historical behaviour: when it is the only host, a
    ``GhSetupError`` or ``GhError`` propagates exactly as before. With
    Enterprise hosts configured, one host being unauthenticated or unreachable
    must not blank the picker for the others, so a failing host is skipped and
    named in ``host_errors``; only when EVERY host is unusable does the result
    read ``setup_required`` (all setup errors) or re-raise (any other error).

    Returns ``{"repos": [...], "truncated": bool, "login"?: str,
    "logins": {host: login}, "host_errors": {host: msg}}``."""
    hosts = discovery.configured_hosts()
    if len(hosts) <= 1:
        try:
            res = one(hosts[0] if hosts else discovery.DEFAULT_HOST)
        except discovery.GhSetupError as exc:
            return {"repos": [], "setup_required": True, "error": str(exc)}
        res.setdefault("repos", [])
        return res
    rows: list[dict] = []
    truncated = False
    logins: dict[str, str] = {}
    errors: dict[str, str] = {}
    last_exc: Exception | None = None
    all_setup = True
    for host in hosts:
        try:
            res = one(host)
        except discovery.GhSetupError as exc:
            errors[host] = str(exc)
            last_exc = exc
            continue
        except discovery.GhError as exc:
            logger.warning("code-review-sage: discovery on %s failed: %s", host, exc)
            errors[host] = "upstream service error"
            last_exc = exc
            all_setup = False
            continue
        rows.extend(res.get("repos") or [])
        truncated = truncated or bool(res.get("truncated"))
        if res.get("login"):
            logins[host] = res["login"]
    if errors and len(errors) == len(hosts):
        if all_setup:
            return {"repos": [], "setup_required": True,
                    "error": "; ".join(f"{h}: {m}" for h, m in errors.items())}
        assert last_exc is not None
        raise last_exc
    out: dict[str, Any] = {"repos": rows, "truncated": truncated, "logins": logins}
    if discovery.DEFAULT_HOST in logins:
        out["login"] = logins[discovery.DEFAULT_HOST]
    elif logins:
        out["login"] = next(iter(logins.values()))
    if errors:
        out["host_errors"] = errors
    return out


def _mark_pinned(rows: list[dict], pinned: list[dict]) -> None:
    """Annotate discovery rows with ``pinned``, keyed by the host-qualified slug."""
    keys = {
        str(r.get("slug") or discovery.repo_slug(
            r.get("host"), str(r.get("owner") or ""), str(r.get("repo") or ""))).lower()
        for r in pinned
    }
    for row in rows:
        slug = row.get("slug") or discovery.repo_slug(
            row.get("host"), str(row.get("owner") or ""), str(row.get("repo") or ""))
        row["pinned"] = str(slug).lower() in keys


async def _handle_recent_repos(request: web.Request) -> web.Response:
    """GET .../recent-repos[?days=N] — repos the ``gh`` user recently worked on.

    Each row is annotated with ``pinned`` so the picker can show what is already
    in the sidebar. A host without a usable/authenticated ``gh`` returns 200 with
    ``setup_required`` rather than an error status: "you need to set up gh" is a
    normal first-run state for this panel, not a failure."""
    raw_days = (request.query.get("days") or "").strip()
    days = discovery.CONTRIB_WINDOW_DAYS
    if raw_days:
        try:
            days = int(raw_days)
        except ValueError:
            return web.json_response(
                {"code": "invalid_days", "error": "days must be an integer"}, status=400
            )
        if days < 0 or days > discovery.MAX_WINDOW_DAYS:
            return web.json_response(
                {
                    "code": "invalid_days",
                    "error": f"days must be between 0 and {discovery.MAX_WINDOW_DAYS}",
                },
                status=400,
            )

    def _one(host: str) -> dict:
        login = discovery.current_login(host=host)
        if not login:
            return {"repos": [], "login": None}
        rows, truncated = discovery.list_contributed_repos(
            login, within_days=days, host=host)
        return {"repos": rows, "login": login, "truncated": truncated}

    def _load() -> dict:
        pinned = discovery.read_repos()
        out = _across_hosts(_one)
        out["pinned"] = pinned
        _mark_pinned(out["repos"], pinned)
        return out

    try:
        return web.json_response(await asyncio.to_thread(_load))
    except discovery.GhSetupError as exc:
        return web.json_response(
            {"repos": [], "pinned": [], "setup_required": True, "error": str(exc)}
        )
    except discovery.GhError as exc:
        return web.json_response({"code": "provider_unavailable", "error": str(exc)}, status=502)


async def _handle_my_repos(request: web.Request) -> web.Response:
    """GET .../my-repos — every repo the ``gh`` user can reach, newest push first.

    The companion to ``/recent-repos``: that one answers "what have I touched
    lately", this one answers "what can I reach at all", which is what you need
    for a repo you own but have not pushed to inside the activity window. Rows are
    annotated with ``pinned``. A host without a usable/authenticated ``gh`` returns
    200 with ``setup_required`` — an unconfigured CLI is a normal first-run state
    for this panel, and the UI still offers manual entry."""

    def _one(host: str) -> dict:
        rows, truncated = discovery.list_user_repos(host=host)
        return {"repos": rows, "truncated": truncated}

    def _load() -> dict:
        pinned = discovery.read_repos()
        out = _across_hosts(_one)
        out["pinned"] = pinned
        _mark_pinned(out["repos"], pinned)
        return out

    try:
        return web.json_response(await asyncio.to_thread(_load))
    except discovery.GhSetupError as exc:
        return web.json_response(
            {"repos": [], "pinned": [], "setup_required": True, "error": str(exc)}
        )
    except discovery.GhError as exc:
        return web.json_response({"code": "provider_unavailable", "error": str(exc)}, status=502)


async def _handle_review_queue(request: web.Request) -> web.Response:
    """GET .../review-queue — open PRs where the ``gh`` user is a requested
    reviewer. Like ``/my-repos``, an unusable ``gh`` is a 200 ``setup_required``."""
    def _one(host: str) -> dict:
        prs, truncated = discovery.list_review_requested(host=host)
        return {"repos": prs, "truncated": truncated}

    try:
        merged = await asyncio.to_thread(_across_hosts, _one)
    except discovery.GhSetupError as exc:
        return web.json_response({"prs": [], "setup_required": True, "error": str(exc)})
    except discovery.GhError as exc:
        logger.warning("review queue failed: %s", exc)
        return web.json_response(
            {"code": "provider_unavailable", "error": "upstream service error"}, status=502
        )
    if merged.get("setup_required"):
        return web.json_response(
            {"prs": [], "setup_required": True, "error": merged.get("error", "")})
    rows = sorted(merged["repos"], key=lambda r: r.get("updated_at") or "", reverse=True)
    truncated = bool(merged.get("truncated"))
    # The same change id the repo list carries: the detail pane scopes posting by
    # it, and an empty one would publish every change of a multi-PR run.
    for row in rows:
        row["change_id"] = review_driver.change_id_for(row["url"])
    return web.json_response({"prs": rows, "truncated": truncated})


def _pull_request_ref(link: str) -> dict | None:
    """Parse a pasted GitHub PR URL into the repo plus the PR's identity.

    Returns None for anything that is not a PR link, so the caller can fall back
    to repo-URL parsing. Deliberately tolerant about what it accepts and strict
    about what it returns: every field here is produced by the same validated
    parser the review path uses, never by string slicing.
    """
    if "/pull/" not in (link or ""):
        return None
    try:
        host, owner, repo, number = adapters.github_pr_ref(link)
    except (adapters.AdapterParseError, adapters.UnsupportedPlatform, ValueError):
        return None
    return {
        "host": host,
        "owner": owner,
        "repo": repo,
        "number": int(number),
        "url": f"https://{host}/{owner}/{repo}/pull/{number}",
        "change_id": adapters.github_change_id(owner, repo, number, host=host),
    }


async def _handle_repos(request: web.Request) -> web.Response:
    """GET/POST/DELETE .../repos — the pinned-repo list the sidebar renders.

    POST/DELETE body: ``{"owner": "...", "repo": "..."}`` or ``{"repo": "<url>"}``
    (a repo URL, parsed by the same validator the review path uses)."""
    if request.method == "GET":
        repos = await asyncio.to_thread(discovery.read_repos)
        return web.json_response({"repos": repos})
    owner_denied = await require_owner_dashboard_request(request, "code_review_sage.repos_write")
    if owner_denied is not None:
        return owner_denied
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    owner = str(body.get("owner") or "").strip()
    name = str(body.get("repo") or "").strip()
    raw_host = body.get("host")
    host = str(raw_host or "").strip().lower() if isinstance(raw_host, str) else ""
    host = host or discovery.DEFAULT_HOST
    pr: dict | None = None
    if owner and name:
        # An owner/repo pair supplied directly still has to satisfy the same
        # character allowlist as a parsed URL — this value becomes a path segment
        # in a `gh api` call and (via full_name) a stored identifier. The host
        # goes through the same parsed-hostname allowlist, so only a configured
        # `github_hosts` entry can be pinned.
        try:
            host, owner, name = adapters.parse_repo_ref(f"https://{host}/{owner}/{name}")
        except (adapters.AdapterParseError, adapters.UnsupportedPlatform, ValueError) as e:
            return web.json_response(
                {"code": "invalid_repo", "error": f"invalid repo: {e}"}, status=400
            )
    elif name:
        # A PASTED PR LINK is the common case here: the field is the only place in
        # the app you can type, so that is where a URL from the clipboard lands.
        # Rejecting it and pointing at the paste box was unhelpful — that box only
        # exists once a repo is already picked, which is the thing being asked for.
        # Pin the PR's repo and report the PR back so the caller can open it.
        pr = _pull_request_ref(name)
        if pr is not None:
            host, owner, name = pr["host"], pr["owner"], pr["repo"]
        else:
            try:
                host, owner, name = adapters.parse_repo_ref(name)
            except (adapters.AdapterParseError, adapters.UnsupportedPlatform, ValueError) as e:
                return web.json_response(
                    {"code": "invalid_repo_url", "error": f"invalid repo url: {e}"}, status=400
                )
    else:
        return web.json_response(
            {"code": "repo_required", "error": "missing 'owner'+'repo' or a repo url in 'repo'"},
            status=400,
        )

    if request.method == "POST":
        repos = await asyncio.to_thread(discovery.add_repo, owner, name, host=host)
    else:
        repos = await asyncio.to_thread(discovery.remove_repo, owner, name, host=host)
    out: dict[str, Any] = {"ok": True, "repos": repos}
    if request.method == "POST":
        # Name the added repo explicitly: repos[0] is only it if the store
        # happens to prepend.
        out["added"] = {
            "owner": owner, "repo": name, "host": host,
            "slug": discovery.repo_slug(host, owner, name),
        }
    if request.method == "POST" and pr is not None:
        # The caller uses this to open the pasted pull request instead of leaving
        # the user to find it in the list.
        out["pull_request"] = pr
    return web.json_response(out)
