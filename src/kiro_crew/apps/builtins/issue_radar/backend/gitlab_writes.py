"""GitLab write calls the Issue Radar crew runtime needs: edit an issue note, open a
merge request.

Split out of ``gitlab_client`` to keep that module under the repository's
file-line cap; ``gitlab_client.update_issue_comment`` / ``create_pull_request``
stay the import path callers and tests use (the client re-exports both). The
client's ``_glab_api`` / ``_obj`` / ``project_path`` are resolved through the
module AT CALL TIME, not bound at import: the GitLab tests patch
``gitlab_client._glab_api`` as the transport seam, and a reference captured here
at import would bypass that patch. The late import also avoids an import cycle
with ``gitlab_client``.
"""

from __future__ import annotations

from .errors import ProviderCliError


def update_issue_comment(
    owner: str,
    repo: str,
    comment_id: int,
    body: str,
    *,
    number: int,
    host: str = "",
    timeout: float | None = None,
) -> dict:
    """EDIT an existing ISSUE note (``PUT .../issues/{iid}/notes/{note_id}``).

    Parity with ``github_client.update_issue_comment``, which the crew claim ledger
    uses to rewrite its one public record on every heartbeat instead of appending a
    note per cycle. The positional shape matches GitHub's exactly; the one extra,
    ``number``, is KEYWORD-ONLY and required: GitLab's note endpoints are nested
    under the issue (``issues/{iid}/notes/{id}``), not repo-flat like GitHub's, so
    the issue iid is part of the address. Keyword-only so a caller written against
    the GitHub shape fails with a ``TypeError`` here instead of silently addressing
    the wrong note. Both ids are ``int()``-coerced before they reach the path.

    Issue notes only, on purpose. An MR note lives in a different collection with
    its own ids, and a crew's claim is always on the issue it took; a function that
    guessed the collection from the id would be a silent way to edit the wrong
    note (see ``gitlab_client._add_note``).

    ``body`` is model-authored prose and rides as JSON on stdin, never argv. An
    empty edit is refused: it would BLANK the claim, not clear it.

    Returns ``{id, url, updated_at}``; ``url`` is ``None`` because GitLab's note
    response carries no web link, matching ``gitlab_client._add_note``.
    """
    from . import gitlab_client as client  # late: seam patching + no import cycle

    text = (body or "").strip()
    if not text:
        raise ProviderCliError("a comment edit needs a body")
    data = client._obj(
        client._glab_api(
            f"projects/{client.project_path(owner, repo)}/issues/{int(number)}/notes/{int(comment_id)}",
            host=host,
            timeout=client.GL_TIMEOUT_SEC if timeout is None else timeout,
            method="PUT",
            body={"body": text},
        )
    )
    return {
        "id": data.get("id"),
        "url": None,
        "updated_at": data.get("updated_at"),
    }


def create_pull_request(
    owner: str,
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str = "",
    *,
    draft: bool = False,
    host: str = "",
    timeout: float | None = None,
) -> dict:
    """Open a merge request (``POST .../merge_requests``).

    Parity with ``github_client.create_pull_request``: REST through ``glab api``
    with the title and description as JSON on stdin, so model-authored prose never
    lands on an argv or on disk. ``head`` and ``base`` are the source and target
    BRANCH names on this project; a cross-fork MR (GitLab's ``target_project_id``)
    is not offered, because no caller needs it and an extra id in the payload is
    one more thing a brief could be talked into setting.

    GitLab has no ``draft`` field: a draft MR is one whose title starts with
    ``Draft:``, so ``draft=True`` prepends that marker (once). ``head`` and
    ``base`` ride inside the JSON body, never a path, so they are not
    charset-validated beyond being non-empty, exactly as on GitHub.

    Returns ``{number, url, html_url, draft, state}`` in the GitHub-shaped
    vocabulary; ``number`` is the MR's ``iid``.
    """
    from . import gitlab_client as client  # late: seam patching + no import cycle

    subject = (title or "").strip()
    if not subject:
        raise ProviderCliError("a merge request needs a title")
    if not (head or "").strip() or not (base or "").strip():
        raise ProviderCliError(
            f"a merge request needs both source and target branches (got {head!r} → {base!r})"
        )
    if draft and not subject.lower().startswith("draft:"):
        subject = f"Draft: {subject}"
    if subject.lower().startswith("draft:") and not subject[len("draft:") :].strip():
        # A bare marker is a title with no content; GitLab would 400 it, and the
        # client's job is to say so before a request is spent.
        raise ProviderCliError("a merge request needs a title beyond the Draft: marker")
    payload: dict[str, object] = {
        "source_branch": head.strip(),
        "target_branch": base.strip(),
        "title": subject,
        "description": body or "",
    }
    data = client._obj(
        client._glab_api(
            f"projects/{client.project_path(owner, repo)}/merge_requests",
            host=host,
            timeout=client.GL_TIMEOUT_SEC if timeout is None else timeout,
            method="POST",
            body=payload,
        )
    )
    link = data.get("web_url")
    return {
        "number": data.get("iid"),
        "url": link,
        "html_url": link,
        "draft": bool(data.get("draft")),
        "state": data.get("state"),
    }
