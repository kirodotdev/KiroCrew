"""Issues as a source: GitHub, GitLab and Jira issue URL parsing, the chip label,
the normalized issue payload and its cache, and the ``/api/source/issue`` endpoint."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from named_cases_helpers import named_cases
from source_providers_helpers import _clear_issue_cache  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401
from source_providers_helpers import _ISSUE_URL, _app

from kiro_crew.dashboard.handlers import source_providers as source

# --- Issues -----------------------------------------------------------------


def test_parse_github_issue_url() -> None:
    ref = source.parse_source_url("https://github.com/kirodotdev/KiroCrew/issues/58#issue-1")
    assert ref.provider == "github"
    assert ref.owner == "kirodotdev"
    assert ref.repo == "KiroCrew"  # brand-ok: the kirodotdev/KiroCrew repository name
    assert ref.number == 58
    assert ref.kind == "issue"
    assert ref.url == "https://github.com/kirodotdev/KiroCrew/issues/58"


def test_parse_github_pull_request_still_reports_change_kind() -> None:
    ref = source.parse_source_url("https://github.com/acme/repo/pull/12")
    assert ref.kind == "change"


def test_parse_gitlab_issue_url_with_nested_group() -> None:
    ref = source.parse_source_url("https://gitlab.com/acme/platform/service/-/issues/42")
    assert ref.provider == "gitlab"
    assert ref.project == "acme/platform/service"
    assert ref.repo == "service"
    assert ref.number == 42
    assert ref.kind == "issue"
    assert ref.url == "https://gitlab.com/acme/platform/service/-/issues/42"


def test_parse_gitlab_work_item_url_is_an_issue() -> None:
    ref = source.parse_source_url("https://gitlab.com/acme/platform/service/-/work_items/42")
    assert ref.provider == "gitlab"
    assert ref.project == "acme/platform/service"
    assert ref.number == 42
    assert ref.kind == "issue"
    assert ref.url == "https://gitlab.com/acme/platform/service/-/work_items/42"


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/groups/acme/-/work_items/5",
        "https://gitlab.com/Groups/acme/sub/-/work_items/5",
    ],
)
def test_parse_gitlab_group_work_item_is_not_an_issue(url: str) -> None:
    """A group work item is an epic: the project issues API cannot fetch it."""
    with pytest.raises(ValueError):
        source.parse_source_url(url)


def test_parse_gitlab_project_named_like_groups_still_parses_work_item() -> None:
    ref = source.parse_source_url("https://gitlab.com/acme/groups/-/work_items/7")
    assert ref.project == "acme/groups"
    assert ref.kind == "issue"


def test_parse_gitlab_merge_request_still_reports_change_kind() -> None:
    ref = source.parse_source_url("https://gitlab.com/acme/platform/-/merge_requests/9")
    assert ref.kind == "change"


def test_parse_gitlab_issue_rejects_a_traversal_project_path() -> None:
    """The issue marker inherits the MR marker's segment rejection."""
    with pytest.raises(ValueError, match="Invalid GitLab project path"):
        source.parse_source_url("https://gitlab.com/a/../b/-/issues/1")


@pytest.mark.parametrize(
    "url",
    [
        *named_cases(
            "test_parse_source_url_rejects_untrusted_issue_shapes",
            "url",
            [
                # GitLab issues live under the /-/ scope; the bare form is not a
                # GitLab issue URL and must stay rejected.
                "https://gitlab.com/group/project/issues/1",
                "http://github.com/org/repo/issues/1",
                "https://evil.example/github.com/org/repo/issues/1",
                "https://github.com.evil.example/org/repo/issues/1",
                "https://user@github.com/org/repo/issues/1",
                "https://github.com/org/repo/issues/abc",
                "https://gitlab.com/group/project/-/issues/",
            ],
        ),
        *named_cases(
            "test_parse_source_url_rejects_untrusted_jira_shapes",
            "url",
            [
                # The bare suffix is not a tenant; only real subdomains are Cloud Jira.
                "https://atlassian.net/browse/PROJ-1",
                "https://acme.atlassian.net.evil.example/browse/PROJ-1",
                "http://acme.atlassian.net/browse/PROJ-1",
                "https://user@acme.atlassian.net/browse/PROJ-1",
                # Keys must be PROJECT-NUMBER: a bare number, a digit-led project part,
                # an overlong project part, and a missing number all fail.
                "https://acme.atlassian.net/browse/123",
                "https://acme.atlassian.net/browse/1PROJ-1",
                "https://acme.atlassian.net/browse/ABCDEFGHIJK-1",
                "https://acme.atlassian.net/browse/PROJ-",
                "https://acme.atlassian.net/browse/",
                "https://acme.atlassian.net/PROJ-1",
            ],
        ),
        *named_cases(
            "test_parse_source_url_rejects_untrusted_shapes",
            "url",
            [
                "http://github.com/org/repo/pull/1",
                "https://evil.example/github.com/org/repo/pull/1",
                "https://github.com.evil.example/org/repo/pull/1",
                "https://user@github.com/org/repo/pull/1",
                "https://gitlab.com/group/project/issues/1",
            ],
        ),
    ],
)
def test_parse_source_url_rejects_an_untrusted_shape(url: str) -> None:
    with pytest.raises(ValueError):
        source.parse_source_url(url)


def test_self_hosted_gitlab_issue_rejected_when_allowlist_empty(monkeypatch) -> None:
    monkeypatch.setattr(source, "_allowed_gitlab_hosts", lambda: frozenset())
    with pytest.raises(ValueError, match="dashboard.gitlab_hosts"):
        source.parse_source_url("https://gitlab.acme.internal/team/api/-/issues/7")


def test_parse_jira_cloud_issue_url() -> None:
    """Atlassian Cloud (*.atlassian.net) is auto-recognized without allowlisting."""
    ref = source.parse_source_url("https://acme.atlassian.net/browse/PROJ-123")
    assert ref.provider == "jira"
    assert ref.repo == "PROJ"
    assert ref.number == 123
    assert ref.kind == "issue"
    assert ref.url == "https://acme.atlassian.net/browse/PROJ-123"


def test_parse_jira_issue_key_is_canonicalized_uppercase() -> None:
    """Jira treats keys case-insensitively; one case means one dedup-map entry."""
    ref = source.parse_source_url("https://acme.atlassian.net/browse/proj-9")
    assert ref.repo == "PROJ"
    assert ref.url == "https://acme.atlassian.net/browse/PROJ-9"


def test_parse_jira_issue_drops_query_and_deeper_segments() -> None:
    ref = source.parse_source_url(
        "https://acme.atlassian.net/browse/OPS-77/comments?focusedCommentId=1"
    )
    assert ref.url == "https://acme.atlassian.net/browse/OPS-77"
    assert ref.repo == "OPS"
    assert ref.number == 77


def test_parse_jira_issue_preserves_context_path_prefix(monkeypatch) -> None:
    """Data Center installs serve Jira behind a context path; the chip must
    link to the real endpoint, not the host root."""
    monkeypatch.setattr(source, "_jira_hosts_snapshot", frozenset({"jira.acme.internal"}))
    ref = source.parse_source_url("https://jira.acme.internal/jira/browse/CORE-5")
    assert ref.url == "https://jira.acme.internal/jira/browse/CORE-5"
    assert ref.provider == "jira"


def test_self_hosted_jira_rejected_when_allowlist_empty(monkeypatch) -> None:
    """Same fail-closed discipline as self-managed GitLab."""
    monkeypatch.setattr(source, "_jira_hosts_snapshot", frozenset())
    with pytest.raises(ValueError, match="dashboard.jira_hosts"):
        source.parse_source_url("https://jira.acme.internal/browse/PROJ-1")


class TestSourceRefLabel:
    """``source_ref_label`` -- what a sidebar chip is CALLED.

    These assertions gather what was spread across the sidebar's own render
    fixtures, where each provider's punctuation was rebuilt by a template
    string. They live here now because this is the side that knows the
    convention, and the renderer prints whatever it is handed.
    """

    def test_github_uses_hash_for_both_namespaces(self) -> None:
        """GitHub writes ``#123`` for a pull request and an issue alike -- the two
        namespaces share one number counter, and the provider does not
        distinguish them in writing either."""
        pull = source.parse_source_url("https://github.com/acme/widgets/pull/123")
        issue = source.parse_source_url("https://github.com/acme/widgets/issues/124")
        assert source.source_ref_label(pull) == "#123"
        assert source.source_ref_label(issue) == "#124"

    def test_gitlab_bangs_only_the_merge_request(self) -> None:
        """``!7`` is GitLab's mark for a MERGE REQUEST specifically; its issues
        are ``#7``. Labelling a GitLab issue ``!7`` names an unrelated object
        that usually also exists, which is why the split is pinned rather than
        left to whichever renderer formats the chip."""
        mr = source.parse_source_url("https://gitlab.com/acme/service/-/merge_requests/7")
        issue = source.parse_source_url("https://gitlab.com/acme/service/-/issues/7")
        assert source.source_ref_label(mr) == "!7"
        assert source.source_ref_label(issue) == "#7"

    def test_jira_label_is_the_whole_key(self) -> None:
        """Jira has no bare number: ``PROJ-123`` is the identifier. This is the
        case that had the serializer shipping a project key purely so the
        renderer could paste it back on."""
        ref = source.parse_source_url("https://acme.atlassian.net/browse/PROJ-123")
        assert source.source_ref_label(ref) == "PROJ-123"

    def test_unknown_provider_borrows_no_vendor_punctuation(self) -> None:
        """A provider this build does not know gets ``#``, the most widely shared
        convention -- never ``!``, which would assert it is GitLab. Constructed
        directly because ``parse_source_url`` cannot yet produce such a ref; the
        point is that the label function is total over its input rather than
        exhaustive over today's three providers."""
        ref = source.SourceRef(
            "acme-review",
            "https://review.acme.internal/c/4821",
            "review.acme.internal",
            "acme",
            "widgets",
            4821,
            kind="change",
        )
        assert source.source_ref_label(ref) == "#4821"


def test_jira_ref_never_passes_the_change_gate() -> None:
    """Every provider-CLI entry point gates on _require_change_ref; a Jira ref
    (always kind='issue') must be refused there so it can never reach gh/glab."""
    ref = source.parse_source_url("https://acme.atlassian.net/browse/PROJ-123")
    with pytest.raises(ValueError, match="issue"):
        source._require_change_ref(ref)


@pytest.mark.asyncio
async def test_fetch_issue_jira_no_credentials(monkeypatch) -> None:
    """Jira issues raise a descriptive error when no credentials are configured."""

    async def no_hosts() -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", no_hosts)
    monkeypatch.setattr(source, "_get_jira_auth", lambda host: None)
    with pytest.raises(ValueError, match="jira_no_credentials"):
        await source.fetch_issue("https://acme.atlassian.net/browse/PROJ-123")


def test_self_hosted_gitlab_issue_accepted_when_allowlisted(monkeypatch) -> None:
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    ref = source.parse_source_url("https://gitlab.acme.internal/team/platform/api/-/issues/7")
    assert ref.kind == "issue"
    assert ref.host == "gitlab.acme.internal"
    assert ref.project == "team/platform/api"
    assert ref.url == "https://gitlab.acme.internal/team/platform/api/-/issues/7"


def test_self_hosted_gitlab_issue_matches_host_exactly(monkeypatch) -> None:
    """An allowlist entry must not widen to a lookalike host for issues either."""
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    for url in (
        "https://evil-gitlab.acme.internal/a/b/-/issues/1",
        "https://gitlab.acme.internal.evil.test/a/b/-/issues/1",
        "https://gitlab.acme.internal:8443/a/b/-/issues/1",
    ):
        with pytest.raises(ValueError):
            source.parse_source_url(url)


@pytest.mark.asyncio
async def test_status_endpoint_drops_issue_urls_before_scheduling(monkeypatch) -> None:
    """An issue URL submitted to the chip-status endpoint is skipped, not scheduled."""
    pr_url = "https://github.com/acme/repo/pull/12"
    source._check_cache.clear()
    refresh = MagicMock(return_value=[])
    monkeypatch.setattr(source, "schedule_check_refresh", refresh)

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/status", json={"urls": [_ISSUE_URL, pr_url]}
        )
        assert response.status == 200
        assert await response.json() == {
            "statuses": {},
            "refreshing": [],
            "ttlSecs": source.CHECK_STATUS_TTL_SECS,
        }

    assert refresh.call_args.args[0] == [pr_url]


@pytest.mark.asyncio
async def test_fetch_issue_refuses_a_pull_request_url(monkeypatch) -> None:
    """The refusal runs both ways: the issue reader must not read a PR."""
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="not an issue"):
        await source.fetch_issue("https://github.com/acme/repo/pull/12")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_github_issue_normalizes_the_contract_payload(monkeypatch) -> None:
    commands: list[str] = []

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        commands.append(command)
        if command.endswith("repos/acme/repo/issues/12"):
            return {
                "number": 999,
                "html_url": "https://github.com/attacker/evil/issues/1",
                "title": "Panel drops a link",
                "body": "Steps to reproduce",
                "state": "CLOSED",
                "state_reason": "completed",
                "user": {"login": "reporter"},
                "created_at": "2026-07-01T10:00:00Z",
                "updated_at": "2026-07-02T10:00:00Z",
                "closed_at": "2026-07-03T10:00:00Z",
                "closed_by": {"login": "maintainer"},
                "labels": [
                    {"name": "bug", "color": "d73a4a", "description": "Broken"},
                    {"name": "ui", "color": "", "description": ""},
                ],
                "assignees": [{"login": "octocat"}, {}],
                "milestone": {"title": "v2", "state": "open", "due_on": "2026-08-01T00:00:00Z"},
                "comments": 1,
                "locked": True,
                "reactions": {"total_count": 3, "+1": 2, "-1": 1, "heart": 0},
            }
        if "/comments?" in command:
            return [
                {
                    "id": 77,
                    "user": {"login": "helper"},
                    "body": "Confirmed",
                    "created_at": "2026-07-01T11:00:00Z",
                    "html_url": "https://github.com/acme/repo/issues/12#issuecomment-77",
                }
            ]
        if "/timeline?" in command:
            return [
                {"event": "labeled"},
                {
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            "number": 34,
                            "title": "Fix the panel",
                            "state": "open",
                            "html_url": "https://github.com/acme/repo/pull/34",
                            "pull_request": {"url": "x"},
                        }
                    },
                },
                {
                    # A plain issue-to-issue mention is NOT a linked change.
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            "number": 35,
                            "title": "Related report",
                            "state": "open",
                            "html_url": "https://github.com/acme/repo/issues/35",
                        }
                    },
                },
            ]
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url("https://github.com/acme/repo/issues/12")
    data = await source._fetch_github_issue(ref)

    assert set(data) == {
        "provider",
        "url",
        "number",
        "title",
        "description",
        "state",
        "stateReason",
        "author",
        "createdAt",
        "updatedAt",
        "closedAt",
        "closedBy",
        "labels",
        "assignees",
        "milestone",
        "commentCount",
        "locked",
        "reactions",
        "comments",
        "linkedChanges",
        "partialSections",
    }
    # Identity is the validated ref, never the provider echo.
    assert data["url"] == "https://github.com/acme/repo/issues/12"
    assert data["number"] == 12
    assert data["state"] == "closed"
    assert data["stateReason"] == "completed"
    assert data["author"] == "reporter"
    assert data["closedBy"] == "maintainer"
    assert data["labels"] == [
        {"name": "bug", "color": "d73a4a", "description": "Broken"},
        {"name": "ui", "color": "", "description": ""},
    ]
    assert data["assignees"] == ["octocat"]
    assert data["milestone"] == {"title": "v2", "state": "open", "dueOn": "2026-08-01T00:00:00Z"}
    assert data["locked"] is True
    assert data["reactions"] == {
        "total": 3,
        "plus1": 2,
        "minus1": 1,
        "laugh": 0,
        "hooray": 0,
        "confused": 0,
        "heart": 0,
        "rocket": 0,
        "eyes": 0,
    }
    assert data["comments"] == [
        {
            "id": "77",
            "author": "helper",
            "body": "Confirmed",
            "createdAt": "2026-07-01T11:00:00Z",
            "url": "https://github.com/acme/repo/issues/12#issuecomment-77",
        }
    ]
    assert data["linkedChanges"] == [
        {
            "provider": "github",
            "url": "https://github.com/acme/repo/pull/34",
            "number": 34,
            "title": "Fix the panel",
            "state": "open",
        }
    ]
    assert data["partialSections"] == []
    assert not any("pr view" in command for command in commands)


@pytest.mark.asyncio
async def test_fetch_github_issue_degrades_failed_sections(monkeypatch) -> None:
    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if command.endswith("repos/acme/repo/issues/12"):
            return {"title": "T", "state": "open", "comments": 4}
        raise source.SourceProviderError("boom")

    monkeypatch.setattr(source, "_run_json", fake_run)
    data = await source._fetch_github_issue(
        source.parse_source_url("https://github.com/acme/repo/issues/12")
    )

    assert data["comments"] == []
    assert data["linkedChanges"] == []
    assert data["partialSections"] == ["comments", "linked changes"]
    # The provider's own count survives a failed comment page.
    assert data["commentCount"] == 4


@pytest.mark.asyncio
async def test_fetch_github_issue_rejects_a_non_https_linked_change(monkeypatch) -> None:
    """A cross-reference URL reaches an href, so only https survives."""

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if command.endswith("repos/acme/repo/issues/12"):
            return {"title": "T", "state": "open"}
        if "/comments?" in command:
            return []
        return [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 1,
                        "html_url": "javascript:alert(1)",
                        "pull_request": {},
                    }
                },
            }
        ]

    monkeypatch.setattr(source, "_run_json", fake_run)
    data = await source._fetch_github_issue(
        source.parse_source_url("https://github.com/acme/repo/issues/12")
    )

    assert data["linkedChanges"] == []


@pytest.mark.asyncio
async def test_fetch_gitlab_issue_normalizes_the_contract_payload(monkeypatch) -> None:
    hosts: list[str] = []

    async def fake_run(*argv: str, **kwargs):
        command = " ".join(argv)
        hosts.append(kwargs.get("host", ""))
        if "with_labels_details" in command:
            assert command.startswith("glab api projects/acme%2Fplatform%2Fservice/issues/42")
            return {
                "iid": 999,
                "web_url": "https://gitlab.evil.test/x/-/issues/1",
                "title": "MR panel is blank",
                "description": "Long form",
                "state": "opened",
                "author": {"username": "reporter"},
                "created_at": "2026-07-01T10:00:00Z",
                "updated_at": "2026-07-02T10:00:00Z",
                "closed_at": "",
                "closed_by": None,
                "labels": [
                    {"name": "bug", "color": "#d73a4a", "description": "Broken"},
                    "plain-name-only",
                ],
                "assignees": [{"username": "dev"}],
                "milestone": {"title": "v2", "state": "active", "due_date": "2026-08-01"},
                "user_notes_count": 1,
                "discussion_locked": False,
                "upvotes": 4,
                "downvotes": 1,
            }
        if "/notes?" in command:
            return [
                {"id": 5, "system": True, "body": "changed the description", "author": {}},
                {
                    "id": 6,
                    "author": {"username": "helper"},
                    "body": "Reproduced",
                    "created_at": "2026-07-01T11:00:00Z",
                },
            ]
        if command.endswith("/related_merge_requests"):
            return [
                {
                    "iid": 34,
                    "title": "Fix the panel",
                    "state": "opened",
                    "web_url": "https://gitlab.com/acme/platform/service/-/merge_requests/34",
                }
            ]
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url("https://gitlab.com/acme/platform/service/-/issues/42")
    data = await source._fetch_gitlab_issue(ref)

    assert data["provider"] == "gitlab"
    # Identity is the validated ref, not the provider's web_url/iid.
    assert data["url"] == "https://gitlab.com/acme/platform/service/-/issues/42"
    assert data["number"] == 42
    assert data["state"] == "open"
    assert data["stateReason"] == ""
    assert data["author"] == "reporter"
    assert data["closedBy"] == ""
    # #rrggbb is normalized to the bare form GitHub already uses.
    assert data["labels"] == [
        {"name": "bug", "color": "d73a4a", "description": "Broken"},
        {"name": "plain-name-only", "color": "", "description": ""},
    ]
    assert data["assignees"] == ["dev"]
    assert data["milestone"] == {"title": "v2", "state": "active", "dueOn": "2026-08-01"}
    assert data["locked"] is False
    assert data["reactions"] == {
        "total": 5,
        "plus1": 4,
        "minus1": 1,
        "laugh": 0,
        "hooray": 0,
        "confused": 0,
        "heart": 0,
        "rocket": 0,
        "eyes": 0,
    }
    # System notes are lifecycle churn, not discussion.
    assert data["comments"] == [
        {
            "id": "6",
            "author": "helper",
            "body": "Reproduced",
            "createdAt": "2026-07-01T11:00:00Z",
            "url": "https://gitlab.com/acme/platform/service/-/issues/42#note_6",
        }
    ]
    assert data["linkedChanges"] == [
        {
            "provider": "gitlab",
            "url": "https://gitlab.com/acme/platform/service/-/merge_requests/34",
            "number": 34,
            "title": "Fix the panel",
            "state": "open",
        }
    ]
    assert data["partialSections"] == []
    # Every glab call is pinned to the ref's host.
    assert set(hosts) == {"gitlab.com"}


@pytest.mark.asyncio
async def test_fetch_gitlab_issue_passes_the_self_managed_host(monkeypatch) -> None:
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    hosts: list[str] = []

    async def fake_run(*argv: str, **kwargs):
        hosts.append(kwargs.get("host", ""))
        if "with_labels_details" in " ".join(argv):
            return {"title": "T", "state": "opened"}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url("https://gitlab.acme.internal/team/api/-/issues/7")
    await source._fetch_gitlab_issue(ref)

    assert hosts == ["gitlab.acme.internal"] * 3


@pytest.mark.asyncio
async def test_fetch_issue_caches_and_coalesces_by_normalized_url(monkeypatch) -> None:
    calls = {"count": 0}

    async def fake_fetch(ref):
        calls["count"] += 1
        await asyncio.sleep(0)
        return {"provider": "github", "url": ref.url, "number": ref.number}

    monkeypatch.setattr(source, "_fetch_github_issue", fake_fetch)
    url = "https://github.com/acme/repo/issues/12"

    first, second = await asyncio.gather(source.fetch_issue(url), source.fetch_issue(f"{url}/"))
    assert first is second
    assert calls["count"] == 1

    # Served from cache inside the TTL.
    assert await source.fetch_issue(url) is first
    assert calls["count"] == 1

    # refresh=True bypasses the cache.
    await source.fetch_issue(url, refresh=True)
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_fetch_issue_never_writes_the_chip_status_cache(monkeypatch) -> None:
    """An issue has no CI or merge state, so it must not project a chip status."""
    record = MagicMock()
    monkeypatch.setattr(source, "record_full_payload_status", record)

    async def fake_fetch(ref):
        return {"provider": "github", "url": ref.url, "state": "open"}

    monkeypatch.setattr(source, "_fetch_github_issue", fake_fetch)
    source._check_cache.clear()

    url = "https://github.com/acme/repo/issues/12"
    await source.fetch_issue(url)

    record.assert_not_called()
    assert source.get_cached_check_status(url) is None


@pytest.mark.asyncio
async def test_fetch_issue_rejects_an_oversized_payload(monkeypatch) -> None:
    async def fake_fetch(ref):
        return {"provider": "github", "url": ref.url, "description": "x" * 16}

    monkeypatch.setattr(source, "_fetch_github_issue", fake_fetch)
    monkeypatch.setattr(source, "_MAX_PAYLOAD_BYTES", 8)

    with pytest.raises(source.SourceProviderError, match="issue payload was too large"):
        await source.fetch_issue("https://github.com/acme/repo/issues/12")
    assert source._ISSUE_CACHE == {}


@pytest.mark.asyncio
async def test_fetch_issue_reserves_direct_fetch_capacity(monkeypatch) -> None:
    """The issue task must be visible to the shared pending/byte accounting.

    A task absent from `_direct_fetch_tasks` would hold a reservation nothing
    reads, making the cap decorative for issues.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_fetch(ref):
        started.set()
        # flake-ok: pre-existing unbounded hold in the stub until the test releases it, moved verbatim; tracked for a de-flake pass
        await release.wait()
        return {"provider": "github", "url": ref.url}

    monkeypatch.setattr(source, "_fetch_github_issue", fake_fetch)
    monkeypatch.setattr(source, "_DIRECT_FETCH_PENDING_MAX", 1)
    monkeypatch.setattr(source, "_DIRECT_FETCH_WAIT_SECS", 0.05)

    inflight = asyncio.ensure_future(source.fetch_issue("https://github.com/acme/repo/issues/12"))
    # flake-ok: pre-existing unbounded wait for the stub to signal it started, moved verbatim; tracked for a de-flake pass
    await started.wait()
    try:
        with pytest.raises(source.SourceProviderError, match="pending"):
            await source.fetch_issue("https://github.com/acme/repo/issues/13")
    finally:
        release.set()
        # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
        await inflight


@pytest.mark.asyncio
async def test_issue_endpoint_returns_the_payload(monkeypatch) -> None:
    payload = {"provider": "github", "url": _ISSUE_URL, "number": 12}
    fetch = AsyncMock(return_value=payload)
    monkeypatch.setattr(source, "fetch_issue", fetch)

    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/source/issue", json={"url": _ISSUE_URL})
        assert response.status == 200
        assert await response.json() == payload

    fetch.assert_awaited_once_with(_ISSUE_URL, refresh=False)


@pytest.mark.asyncio
async def test_issue_endpoint_maps_value_error_to_400(monkeypatch) -> None:
    monkeypatch.setattr(
        source, "fetch_issue", AsyncMock(side_effect=ValueError("An issue URL is required."))
    )

    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/source/issue", json={"url": "nope"})
        assert response.status == 400
        assert await response.json() == {
            "error": "An issue URL is required.",
            "code": "invalid_request",
        }


@pytest.mark.asyncio
async def test_issue_endpoint_maps_provider_error_to_503(monkeypatch) -> None:
    monkeypatch.setattr(
        source,
        "fetch_issue",
        AsyncMock(side_effect=source.SourceProviderError("gh timed out")),
    )

    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/source/issue", json={"url": _ISSUE_URL})
        assert response.status == 503
        # `code` distinguishes a real provider failure from admission pressure, so
        # the client retries only the latter. Issues share the pull-request
        # admission pool, so this endpoint can report either.
        assert await response.json() == {"error": "gh timed out", "code": "provider_error"}


@pytest.mark.asyncio
async def test_issue_endpoint_rejects_a_non_owner(monkeypatch) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(source, "fetch_issue", fetch)

    async with TestClient(TestServer(_app(user="U_OTHER"))) as client:
        response = await client.post("/api/source/issue", json={"url": _ISSUE_URL})
        assert response.status == 403

    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_issue_endpoint_rejects_an_app_token(monkeypatch) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(source, "fetch_issue", fetch)

    async with TestClient(TestServer(_app(app_name="issue-radar"))) as client:
        response = await client.post("/api/source/issue", json={"url": _ISSUE_URL})
        assert response.status == 403

    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_issue_endpoint_audits_its_own_operation_name(monkeypatch, _mock_source_sel) -> None:
    monkeypatch.setattr(
        source, "fetch_issue", AsyncMock(return_value={"provider": "github", "url": _ISSUE_URL})
    )

    async with TestClient(TestServer(_app())) as client:
        assert (await client.post("/api/source/issue", json={"url": _ISSUE_URL})).status == 200

    operations = {
        call.kwargs.get("operation") for call in _mock_source_sel.log_api_access.call_args_list
    }
    assert operations == {"source.issue.read"}
    assert _mock_source_sel.log_api_access.call_args.kwargs["outcome"] == "completed"


@pytest.mark.asyncio
async def test_issue_endpoint_warms_allowlist_before_parsing_self_hosted_urls(
    monkeypatch,
) -> None:
    """A cold snapshot would drop an authorized self-managed issue URL as
    unsupported, so the handler's fetch path must warm the allowlist first."""
    url = "https://gitlab.acme.internal/team/api/-/issues/7"
    monkeypatch.setattr(source, "_gitlab_hosts_snapshot", frozenset())
    monkeypatch.setattr(source, "_gitlab_hosts_loaded_at", 0.0)

    async def fake_ensure() -> frozenset:
        source._publish_provider_hosts(frozenset({"gitlab.acme.internal"}), frozenset())
        return frozenset({"gitlab.acme.internal"})

    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", fake_ensure)

    async def fake_fetch(ref):
        return {"provider": "gitlab", "url": ref.url, "number": ref.number}

    monkeypatch.setattr(source, "_fetch_gitlab_issue", fake_fetch)

    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/source/issue", json={"url": url})
        assert response.status == 200
        assert (await response.json())["url"] == url
