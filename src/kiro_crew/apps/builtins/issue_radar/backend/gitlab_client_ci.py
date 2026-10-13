"""The signed-in user's open MR heads for the Issue Radar CI watch (GitLab).

GitLab analogue of ``github_client_ci``. Split out of ``gitlab_client`` to keep
that module under the repository's file-line cap; ``gitlab_client`` re-exports
``search_viewer_open_mr_heads`` so that call keeps its import path for callers and
the tests that patch around it, while the page-size constants are read from here
(``gitlab_client_ci.CI_SEARCH_PAGE_SIZE``) by the CI watch and its tests. The client's ``_glab_api`` / ``_resolve_host`` / ``get_current_login`` and
friends are resolved through the module AT CALL TIME, not bound at import: the
GitLab tests patch ``gitlab_client._glab_api`` and ``gitlab_client.allowed_hosts``
as seams, and a reference captured here at import would bypass those patches.
The late import also avoids an import cycle with ``gitlab_client``.
"""

from __future__ import annotations

from .errors import ProviderCliError

#: Page size and page cap of the own-MR listing, the same bound as
#: ``github_client.search_viewer_open_pull_heads`` so a project with more open own
#: MRs than one search reads is skipped the same way on both forges.
CI_SEARCH_PAGE_SIZE = 100
CI_SEARCH_MAX_PAGES = 5
_CI_NAME_MAX_CHARS = 300
_CI_URL_MAX_CHARS = 500


def search_viewer_open_mr_heads(
    owner: str, repo: str, *, host: str, login: str | None = None, timeout: float | None = None
) -> dict:
    """The signed-in user's open MRs in one project, with each head commit.

    GitLab's analogue of ``github_client.search_viewer_open_pull_heads`` and the
    same return shape: ``{"pulls": [{owner, repo, number, title, url, head_sha}],
    "truncated": [(owner, repo)]}``. One project per call, because GitLab's MR list
    is per project; the CI watch calls it once per opted-in project.

    The listing is ``GET /projects/:id/merge_requests?scope=created_by_me&
    state=opened``: ``created_by_me`` is GitLab's server-side "author is the token's
    user" filter (the REST API has no ``@me`` spelling for ``author_username``).
    It is read page by page up to :data:`CI_SEARCH_MAX_PAGES`; a project whose
    last page was still full is listed in ``truncated`` and none of its MRs are
    returned, so a caller never mistakes a partial list for the whole one. As on
    GitHub, a row whose author is not the signed-in user is dropped as well, so a
    teammate's MR never reaches the caller whatever the server returns. ``login``
    is that user's username; ``None`` reads it from ``GET /user`` on the same host
    first, and a caller listing several projects on one host resolves it once and
    passes it in, so the per-cycle cost is one ``/user`` read per host rather than
    per project.

    The host is checked against the operator's allowlist HERE, before any call,
    as well as at the spawn boundary every ``glab`` call passes through: this is
    a background pass with no request to answer, so an unlisted host must raise
    rather than be quietly resolved. Raises :class:`ProviderCliError` when
    ``glab`` fails or does not report the signed-in user.
    """
    from . import gitlab_client as client  # late: seam patching + no import cycle

    effective_timeout = client.GL_TIMEOUT_SEC if timeout is None else timeout
    host = client._resolve_host(host)
    if login is None:
        login = client.get_current_login(host=host, timeout=effective_timeout)
    if not login:
        raise ProviderCliError("glab did not report the signed-in user")
    base = (
        f"projects/{client.project_path(owner, repo)}/merge_requests"
        f"?scope=created_by_me&state=opened&order_by=updated_at&sort=desc"
        f"&per_page={CI_SEARCH_PAGE_SIZE}"
    )
    rows: list[dict] = []
    complete = False
    for page in range(1, CI_SEARCH_MAX_PAGES + 1):
        data = client._rows(
            client._glab_api(
                f"{base}&page={page}", host=host, timeout=effective_timeout, paginate=False
            )
        )
        rows.extend(data)
        if len(data) < CI_SEARCH_PAGE_SIZE:
            complete = True
            break
    if not complete:
        return {"pulls": [], "truncated": [(owner, repo)]}
    pulls: list[dict] = []
    seen: set[int] = set()
    for raw in rows:
        row = _own_mr_row(raw, owner, repo, login)
        if row is None or row["number"] in seen:
            continue
        seen.add(row["number"])
        pulls.append(row)
    return {"pulls": pulls, "truncated": []}


def _own_mr_row(raw: object, owner: str, repo: str, viewer: str) -> dict | None:
    """Normalize one MR row, or ``None`` when it is not the viewer's or is malformed."""
    from . import gitlab_client as client  # late: seam patching + no import cycle

    if not isinstance(raw, dict):
        return None
    author = (client._username(raw.get("author")) or "").strip()
    if author.lower() != viewer.lower():
        return None
    number = raw.get("iid")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return None
    sha = client._obj(raw.get("diff_refs")).get("head_sha") or raw.get("sha")
    if not isinstance(sha, str) or not client._SHA_RE.match(sha):
        return None
    title = raw.get("title")
    url = raw.get("web_url")
    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        # The same caps as the GitHub row: a note body is built from these.
        "title": title[:_CI_NAME_MAX_CHARS] if isinstance(title, str) else "",
        "url": url[:_CI_URL_MAX_CHARS] if isinstance(url, str) else "",
        "head_sha": sha,
    }
