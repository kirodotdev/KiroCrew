"""Every triage write Issue Radar makes on an Azure DevOps work item.

The work-item PATCH, tags as labels (add, remove, create), state and assignees, and
the issue and pull-request comments. Callers reach every name here through
:mod:`kiro_crew.apps.builtins.issue_radar.backend.azure_client`.
"""

from __future__ import annotations

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

from .azure_client_cli import (
    _API_GIT,
    _API_WIT,
    _API_WIT_COMMENTS,
    AZ_TIMEOUT_SEC,
    _az_invoke,
    _check_label,
    _check_repo,
    _field,
    _identity_login,
    _obj,
    _shape_labels,
    _split_owner,
    _tag_names,
    _tags_field,
    _values,
)
from .azure_client_detail import _work_item
from .azure_client_repo_reads import _identity_matches, _iter_project_identities, list_repo_labels
from .azure_client_work_items import _closed_state_names, _closing_state_name, _open_state_name
from .errors import ProviderCliError, ProviderInvalidInputError

# ── write operations ────────────────────────────────────────────────────────


def _patch_work_item(
    org: str,
    project: str,
    number: int,
    ops: list[dict],
    *,
    host: str,
    timeout: float,
) -> dict:
    """Apply a JSON-Patch document to one work item.

    Work item writes are JSON-Patch, not JSON, and Azure rejects the request
    outright when the content type says otherwise -- hence the explicit
    ``media_type``. The body rides in a temp file like every other body here, so
    no prose ever reaches an argv.
    """
    return _obj(
        _az_invoke(
            org=org,
            area="wit",
            resource="workitems",
            host=host,
            timeout=timeout,
            route={"project": project, "id": int(number)},
            method="PATCH",
            body=ops,
            api_version=_API_WIT,
            media_type="application/json-patch+json",
        )
    )


def _write_tags(
    org: str,
    project: str,
    number: int,
    names: list[str],
    *,
    rev: object,
    host: str,
    timeout: float,
) -> list[str]:
    """Replace a work item's tag field with ``names`` and return what Azure stored.

    ``System.Tags`` is a single delimited STRING field, and Azure exposes no
    add-one-tag or remove-one-tag operation, so every tag edit is unavoidably a
    read-modify-write of the whole set. That makes it lost-update-prone: another
    client adding a tag between our read and our write would be silently erased by
    our full-field value.

    ``rev`` is the revision the caller's ``names`` was computed from, and it is
    sent as a JSON Patch ``test`` on ``/rev`` ahead of the field write. Azure
    evaluates the operations in order and rejects the WHOLE patch when the test
    fails, so a concurrent edit makes this call fail loudly instead of deleting
    someone else's tag. The refusal surfaces as :class:`ProviderCliError`; the
    caller re-reads and retries rather than this function looping, because a retry
    here would hide a genuinely contended item behind an unbounded read-write
    cycle.

    A missing ``rev`` is refused rather than sent unguarded: silently degrading to
    the lost-update behaviour is the failure this guard exists to prevent.
    """
    if not isinstance(rev, int):
        raise ProviderCliError(
            f"work item {int(number)} carried no integer revision, so a tag write "
            "cannot be guarded against a concurrent edit"
        )
    data = _patch_work_item(
        org,
        project,
        number,
        [
            {"op": "test", "path": "/rev", "value": rev},
            {"op": "add", "path": "/fields/System.Tags", "value": _tags_field(names)},
        ],
        host=host,
        timeout=timeout,
    )
    return _tag_names(_obj(data.get("fields")).get("System.Tags"))


def add_issue_labels(
    owner: str,
    repo: str,
    number: int,
    labels: list[str],
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> list[dict]:
    """Add tags to a work item and return its FULL resulting tag set.

    Azure has no add-one-tag operation: ``System.Tags`` is a single delimited
    string, so this is a read-modify-write. That makes it non-atomic -- two
    concurrent label edits can lose one another's addition -- which is inherent to
    the field's shape rather than a choice made here, and is why the write reads
    the current value immediately before patching rather than trusting a cached
    row.

    Tags are CASE SENSITIVE on Azure, so ``bug`` and ``Bug`` are two tags; the
    caller's spelling is preserved exactly. A name containing ``,`` or ``;`` is
    refused (see :func:`_check_label`).
    """
    org, project = _split_owner(owner)
    del repo
    additions = [_check_label(name) for name in labels]
    raw = _work_item(org, project, number, host=host, timeout=timeout)
    current = _tag_names(_obj(raw.get("fields")).get("System.Tags"))
    merged = list(current)
    for name in additions:
        if name not in merged:
            merged.append(name)
    if merged == current:
        # Idempotent: nothing to write, and Azure would answer with an unchanged
        # revision anyway. Skipping the write keeps the revision log clean.
        return _shape_labels(current)
    return _shape_labels(
        _write_tags(org, project, number, merged, rev=raw.get("rev"), host=host, timeout=timeout)
    )


def remove_issue_label(
    owner: str,
    repo: str,
    number: int,
    label: str,
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> list[dict] | None:
    """Remove ONE tag from a work item; returns the remaining tags, shaped.

    Removing a tag the item does not carry is a no-op success, and the authoritative
    remaining set is still returned -- so, as on GitLab, this never needs to answer
    ``None``. The ``None`` stays in the return type for parity with github_client,
    whose callers handle it.
    """
    org, project = _split_owner(owner)
    del repo
    target = _check_label(label)
    raw = _work_item(org, project, number, host=host, timeout=timeout)
    current = _tag_names(_obj(raw.get("fields")).get("System.Tags"))
    remaining = [name for name in current if name != target]
    if remaining == current:
        return _shape_labels(current)
    return _shape_labels(
        _write_tags(org, project, number, remaining, rev=raw.get("rev"), host=host, timeout=timeout)
    )


def set_issue_state(
    owner: str,
    repo: str,
    number: int,
    state: str,
    state_reason: str | None = None,
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Close or reopen a work item. ``state`` is ``"open"`` or ``"closed"``.

    The concrete state name is resolved from the item's OWN type through the
    process template's state categories, so this works on a custom process whose
    closing state is not called "Closed".

    ``state_reason`` is accepted for signature parity and IGNORED, and the returned
    ``state_reason`` is always ``None`` -- exactly as gitlab_client does. Azure's
    ``System.Reason`` is a template-defined value with no mapping onto GitHub's
    two, so translating one would report a reason the platform did not record.
    """
    del state_reason
    org, project = _split_owner(owner)
    del repo
    if state not in ("open", "closed"):
        raise ProviderCliError(f"invalid work item state: {state!r}")
    raw = _work_item(org, project, number, host=host, timeout=timeout)
    work_item_type = str(_field(_obj(raw.get("fields")), "System.WorkItemType", "") or "")
    if not work_item_type:
        raise ProviderCliError(f"could not read the work item type of #{int(number)}")
    if state == "closed":
        target = _closing_state_name(org, project, work_item_type, host=host, timeout=timeout)
    else:
        target = _open_state_name(org, project, work_item_type, host=host, timeout=timeout)
    data = _patch_work_item(
        org,
        project,
        number,
        [{"op": "add", "path": "/fields/System.State", "value": target}],
        host=host,
        timeout=timeout,
    )
    written = str(_field(_obj(data.get("fields")), "System.State", "") or "")
    closed_states = _closed_state_names(org, project, work_item_type, host=host, timeout=timeout)
    if written in closed_states:
        resolved = "closed"
    elif written:
        resolved = "open"
    else:
        # No usable System.State came back -- absent, null and "" all normalize to
        # "" -- so report what was asked for rather than guessing a category.
        resolved = state
    return {"state": resolved, "state_reason": None}


def set_issue_assignees(
    owner: str,
    repo: str,
    number: int,
    assignees: list[str],
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> list[str]:
    """REPLACE a work item's assignee and return the set Azure actually stored.

    Parity with github_client.set_issue_assignees -- replace semantics, an empty
    list clears, and the result is read back from the RESPONSE rather than echoed
    from the request -- with one structural difference that cannot be papered
    over: ``System.AssignedTo`` is a SINGLE identity field, so a work item cannot
    carry a set at all.

    More than one name is therefore REFUSED as
    :class:`ProviderInvalidInputError`, the same class an unassignable GitHub
    login maps to, so the route answers 400 either way. Assigning the first and
    dropping the rest would report success for names the item does not carry --
    the exact failure the read-back exists to prevent -- and is the direction that
    fails silently, so it is not taken.

    The login is resolved against the project's team roster BEFORE the write, for
    the reason gitlab_client.set_issue_assignees resolves ids first: Azure's
    identity picker binds the field to a project member, and a name it cannot bind
    fails with a message naming the FIELD, not the value. Resolving here turns
    that into a refusal that names the login the user actually typed.

    Unlike :func:`_write_tags` this sends no ``/rev`` precondition, because the
    hazard is not the same: a tag edit is a read-modify-write of a delimited
    string shared with every other tag, while this field is replaced wholesale.
    The lost-update guard for a wholesale replace lives one level up, where
    ``routes._replace_assignees_checked`` refuses when the forge does not hold
    the set the editor was rendered from.
    """
    org, project = _split_owner(owner)
    del repo
    wanted = [name.strip() for name in assignees if isinstance(name, str) and name.strip()]
    if len(wanted) > 1:
        raise ProviderInvalidInputError(
            "an Azure DevOps work item holds exactly one assignee, so "
            + ", ".join(wanted)
            + " cannot all be assigned -- pick one.",
            values=list(wanted),
        )

    value = ""
    if wanted:
        login = wanted[0]
        target = login.lower()
        resolved = None
        try:
            for identity in _iter_project_identities(org, project, host=host, timeout=timeout):
                if _identity_matches(identity, target):
                    resolved = _identity_login(identity)
                    if resolved:
                        break
        except ProviderCliError as exc:
            raise ProviderInvalidInputError(
                f"cannot assign {login!r}: the project's team roster is unreadable, so "
                "the name cannot be checked against it.",
                values=[login],
            ) from exc
        if not resolved:
            raise ProviderInvalidInputError(
                f"Azure DevOps will not assign {login!r} -- an assignee must be a member "
                f"of a team in {org}/{project}.",
                values=[login],
            )
        # The roster's own spelling, not the caller's: the field is bound to an
        # identity, and echoing a display-name match back as the field value
        # would send something the picker cannot resolve.
        value = resolved

    # An empty string is how the field is CLEARED. A JSON Patch ``remove`` would
    # fail outright on a work item that never carried an assignee, which is the
    # common case for "clear it" arriving from a stale editor.
    data = _patch_work_item(
        org,
        project,
        number,
        [{"op": "add", "path": "/fields/System.AssignedTo", "value": value}],
        host=host,
        timeout=timeout,
    )
    written = _identity_login(_obj(data.get("fields")).get("System.AssignedTo"))
    return [written] if written else []


def create_label(
    owner: str,
    repo: str,
    name: str,
    color: str = "888888",
    description: str = "",
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Return an existing project tag or refuse a name Azure cannot pre-create.

    Azure has no create-tag-definition endpoint -- a tag comes into existence when
    it is first APPLIED to a work item -- so this cannot create anything. It
    therefore REFUSES a name that does not already exist rather than returning a
    shaped tag: answering successfully would put a phantom label in the caller's
    cache and in the label palette, offering the user a tag that no work item can
    be filtered by and that the next cache refresh silently removes. An existing
    tag is returned as Azure holds it, which makes the call idempotent for the
    only input it can actually satisfy.
    """
    del color, description
    org, project = _split_owner(owner)
    del repo
    tag = _check_label(name)
    try:
        existing = {
            row["name"]: row for row in list_repo_labels(owner, project, host=host, timeout=timeout)
        }
    except ProviderCliError:
        existing = {}
    if tag in existing:
        return existing[tag]
    raise ProviderCliError(
        f"Azure DevOps cannot create the tag {tag!r} on its own: a work item tag is "
        "created by applying it to a work item, so there is no tag to create in "
        "advance. Apply it to an item instead."
    )


def _comment_text(body: str) -> str:
    """The vetted text for a comment this app is about to publish.

    Refuses an empty body, then REDACTS before the text can reach a provider. A
    comment body is frequently model-authored (a crew's reply, an AI summary the
    user accepted), and posting it puts that text somewhere public and permanent,
    so a leaked credential or an exfiltration URL cannot be walked back.

    Both redactions run because they catch different things: one strips URLs that
    would smuggle data out to a third party, the other strips secrets that appear
    in the text itself. Redaction is idempotent, so a caller that already redacted
    (the crew path does) loses nothing by passing through here.
    """
    text = (body or "").strip()
    if not text:
        raise ProviderCliError("a comment needs a body")
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def add_issue_comment(
    owner: str,
    repo: str,
    number: int,
    body: str,
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Post a comment on a WORK ITEM.

    Work item comments live on a PREVIEW resource -- Azure has never promoted them
    to a GA api-version -- which is why this call's version differs from the rest
    of the ``wit`` area.
    """
    org, project = _split_owner(owner)
    del repo
    text = _comment_text(body)
    data = _obj(
        _az_invoke(
            org=org,
            area="wit",
            resource="comments",
            host=host,
            timeout=timeout,
            route={"project": project, "workItemId": int(number)},
            method="POST",
            body={"text": text},
            api_version=_API_WIT_COMMENTS,
        )
    )
    return {
        "id": data.get("id"),
        # A work item comment has no direct web URL of its own, so the caller links
        # to the work item rather than fabricating an anchor.
        "url": None,
        "created_at": data.get("createdDate"),
    }


def add_pr_comment(
    owner: str,
    repo: str,
    number: int,
    body: str,
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Post a comment on a PULL REQUEST, as a new thread.

    A general PR comment on Azure is a thread with NO ``threadContext`` -- that is
    what distinguishes it from an inline diff comment. A separate function from
    :func:`add_issue_comment` because work items and pull requests are different
    services with independent id sequences, so the number alone does not identify
    the item and posting to the wrong one would comment on something unrelated.
    """
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    text = _comment_text(body)
    data = _obj(
        _az_invoke(
            org=org,
            area="git",
            resource="pullRequestThreads",
            host=host,
            timeout=timeout,
            route={"project": project, "repositoryId": name, "pullRequestId": int(number)},
            method="POST",
            body={
                "comments": [{"parentCommentId": 0, "content": text, "commentType": "text"}],
                "status": "active",
            },
            api_version=_API_GIT,
        )
    )
    comments = _values(data.get("comments"))
    first = comments[0] if comments else {}
    return {
        "id": first.get("id") or data.get("id"),
        "url": None,
        "created_at": first.get("publishedDate") or data.get("publishedDate"),
    }
