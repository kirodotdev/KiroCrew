"""GitLab issue-link reads for Issue Radar's dependency graph.

Split out of ``gitlab_client`` to keep that module under the repository's
file-line cap; ``gitlab_client.list_issue_links`` stays the import path callers
and tests use (it re-exports this function). The client's ``_glab_api`` /
``_rows`` are resolved through the module AT CALL TIME, not bound at import:
``test_gitlab_deps`` patches ``gitlab_client._glab_api`` as the transport seam,
and a reference captured here at import would bypass that patch. The late
import also avoids an import cycle with ``gitlab_client``.
"""

from __future__ import annotations


def list_issue_links(
    owner: str, repo: str, iid: int, *, host: str, timeout: float | None = None
) -> list[dict]:
    """The issue-link rows GitLab holds for one issue (``blocks`` / ``is_blocked_by`` /
    ``relates_to``), raw. ``iid`` is an int so it can never carry a path segment.

    ``paginate=False`` on purpose: GitLab's ``/issues/:iid/links`` returns the whole
    list in one response and ignores ``page``/``per_page`` (its docs list no
    pagination parameters). Walking pages here would never see a short page for an
    issue with >= 100 links and would spawn ``_MAX_PAGES`` identical ``glab`` calls
    per issue, each holding a full copy of the payload.
    """
    from . import gitlab_client as client  # late: seam patching + no import cycle

    path = f"projects/{client.project_path(owner, repo)}/issues/{int(iid)}/links"
    effective_timeout = client.GL_TIMEOUT_SEC if timeout is None else timeout
    return client._rows(
        client._glab_api(path, host=host, timeout=effective_timeout, paginate=False)
    )
