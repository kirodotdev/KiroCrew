"""``POST /api/source/issues``: GitHub and GitLab issue list and search.

Covers the three invariants the endpoint rests on: the same owner gate and audit as
``/api/source/issue`` (and a segment-bounded app-token grant), boundary validation
that no field can widen the scope or inject a flag, and the shared redaction path.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
from aiohttp.test_utils import TestClient, TestServer
from source_providers_helpers import _clear_shared_fetch_state  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401
from source_providers_helpers import _app

from kiro_crew.dashboard import token_auth_app_scope
from kiro_crew.dashboard.handlers import source_providers as source
from kiro_crew.dashboard.source_providers import cache, hosts, issue_search
from kiro_crew.dashboard.source_providers.contract import SourceProviderError

_SECRET = "ghp_" + "b" * 36
# The PEM markers are assembled from their dashes, so no line of this file holds a
# whole marker (the internal-content scan reads source lines).
_DASHES = "-----"
_PEM_BEGIN = f"{_DASHES}BEGIN PRIVATE KEY{_DASHES}"
_PEM_END = f"{_DASHES}END PRIVATE KEY{_DASHES}"


@pytest.fixture(autouse=True)
def _isolated_search(_floor_monkeypatch):
    """No config read, a known allowlist, and an empty cache for every test."""
    patch = _floor_monkeypatch
    patch.setattr(hosts, "ensure_gitlab_hosts_loaded", AsyncMock(return_value=frozenset()))
    patch.setattr(hosts, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.dev"}))
    patch.setattr(hosts, "_allowed_github_hosts", lambda: frozenset({"ghe.acme.dev"}))
    cache._SEARCH_CACHE.clear()
    yield
    cache._SEARCH_CACHE.clear()


def _gh_item(number: int, **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "state_reason": None,
        "repository_url": "https://api.github.com/repos/acme/repo",
        "comments": 3,
        "created_at": "2026-11-01T10:00:00Z",
        "updated_at": "2026-11-09T10:00:00Z",
        "is_pr": False,
        "labels": [{"name": "good first issue", "color": "7057ff", "description": "Newcomers"}],
        "assignees": [{"login": "alice", "avatar_url": "https://avatars.example/alice"}],
        "user": {"login": "bob", "avatar_url": "javascript:alert(1)"},
        "body": "## Steps\n\n<!-- template hint -->\n- Run **crew** with `--x`\n"
        "See [the docs](https://docs.example/x).",
    }
    item.update(extra)
    return item


class _GitHub:
    """A fake ``runner._run_json`` that answers ``gh api search/issues``."""

    def __init__(self, items: list[dict[str, Any]], total: int | None = None) -> None:
        self.items = items
        self.total = len(items) if total is None else total
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def __call__(self, *argv: str, **kwargs: Any) -> Any:
        self.calls.append((argv, kwargs))
        return {"total_count": self.total, "items": self.items}

    def params(self, index: int = 0) -> dict[str, str]:
        argv = self.calls[index][0]
        return dict(parse_qsl(urlsplit(argv[2]).query))


def _gl_item(iid: int, **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "iid": iid,
        "title": f"GL {iid}",
        "state": "opened",
        "user_notes_count": 2,
        "created_at": "2026-11-01T10:00:00Z",
        "updated_at": "2026-11-09T10:00:00Z",
        "labels": [{"name": "needs triage", "color": "#ff0000", "description": ""}],
        "assignees": [{"username": "carol", "name": "Carol C", "avatar_url": "https://a/c"}],
        "author": {"username": "dave", "name": "Dave", "avatar_url": "https://a/d"},
        "description": "Plain body",
        "web_url": "https://evil.example/elsewhere",
    }
    item.update(extra)
    return item


class _GitLab:
    """Fakes for ``runner._run_provider`` (the list) and ``runner._run_json`` (``user``)."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        headers: dict[str, str] | None = None,
        user: Any = None,
    ) -> None:
        self.rows = rows
        self.headers = headers if headers is not None else {"X-Total": str(len(rows))}
        self.user = user if user is not None else {"username": "me-user"}
        self.list_calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.user_calls: list[tuple[str, ...]] = []

    async def run_provider(self, *argv: str, **kwargs: Any) -> Any:
        self.list_calls.append((argv, kwargs))
        head = "HTTP/2.0 200 OK\r\n" + "".join(f"{k}: {v}\r\n" for k, v in self.headers.items())
        stdout = (head + "\r\n" + json.dumps(self.rows)).encode()
        return kwargs["parse"](0, stdout, b"")

    async def run_json(self, *argv: str, **kwargs: Any) -> Any:
        self.user_calls.append(argv)
        if isinstance(self.user, BaseException):
            raise self.user
        return self.user

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(source, "_run_provider", self.run_provider)
        monkeypatch.setattr(source, "_run_json", self.run_json)

    def path(self, index: int = 0) -> str:
        return self.list_calls[index][0][2]

    def params(self, index: int = 0) -> list[tuple[str, str]]:
        return parse_qsl(urlsplit(self.path(index)).query)


async def _post(body: Any, **app_kwargs: Any) -> tuple[int, Any]:
    async with TestClient(TestServer(_app(**app_kwargs))) as client:
        response = await client.post("/api/source/issues", json=body)
        return response.status, await response.json()


# ── Invariant 1: authorization identical to /api/source/issue ────────────────


@pytest.mark.parametrize(
    ("app_kwargs", "reason"),
    [
        ({"user": "U_OTHER"}, "non_owner"),
        ({"app_name": "someapp"}, "app_token_not_allowed"),
        ({"include_app_claim": False}, "app_token_not_allowed"),
        ({"include_user_claim": False}, "non_owner"),
        ({"owner_id": "", "user": "U_OWNER"}, "owner_not_configured"),
    ],
)
@pytest.mark.asyncio
async def test_denials_match_the_single_issue_read(
    monkeypatch, _mock_source_sel, app_kwargs, reason
) -> None:
    fake = _GitHub([_gh_item(1)])
    monkeypatch.setattr(source, "_run_json", fake)
    monkeypatch.setattr(source, "fetch_issue", AsyncMock())
    results = {}
    async with TestClient(TestServer(_app(**app_kwargs))) as client:
        for path, body in (
            ("/api/source/issues", {"provider": "github", "scope": "acme/repo"}),
            ("/api/source/issue", {"url": "https://github.com/acme/repo/issues/1"}),
        ):
            response = await client.post(path, json=body)
            results[path] = (response.status, await response.json())
    assert (
        results["/api/source/issues"]
        == results["/api/source/issue"]
        == (
            403,
            {"error": "forbidden"},
        )
    )
    assert fake.calls == []
    denials = [c.kwargs for c in _mock_source_sel.log_api_access.call_args_list]
    assert {"operation": "source.issues.list", "outcome": "denied", "error": reason}.items() <= (
        denials[0].items()
    )
    assert {"operation": "source.issue.read", "outcome": "denied", "error": reason}.items() <= (
        denials[1].items()
    )


@pytest.mark.asyncio
async def test_owner_read_is_audited_as_source_issues_list(monkeypatch, _mock_source_sel) -> None:
    monkeypatch.setattr(source, "_run_json", _GitHub([_gh_item(1)]))
    status, payload = await _post({"provider": "github", "scope": "acme/repo"})
    assert status == 200
    assert payload["provider"] == "github"
    calls = [c.kwargs for c in _mock_source_sel.log_api_access.call_args_list]
    assert [(c["operation"], c["outcome"]) for c in calls] == [("source.issues.list", "completed")]


@pytest.mark.asyncio
async def test_local_owner_without_configured_owner_is_admitted(monkeypatch) -> None:
    monkeypatch.setattr(source, "_run_json", _GitHub([]))
    status, _ = await _post(
        {"provider": "github", "scope": "acme/repo"}, owner_id="", user="local-app"
    )
    assert status == 200


@pytest.mark.parametrize(
    ("declared", "path", "allowed"),
    [
        ("/api/source/issue", "/api/source/issues", False),
        ("/api/source/issues", "/api/source/issue", False),
        ("/api/source/issues", "/api/source/issues", True),
        ("/api/source/issue", "/api/source/issue", True),
        ("/api/source/pull-request", "/api/source/issues", False),
    ],
)
def test_app_token_grant_is_path_segment_bounded(monkeypatch, declared, path, allowed) -> None:
    """A manifest's bare ``permissions.api`` entry covers itself and its children only."""
    monkeypatch.setattr(token_auth_app_scope, "_app_api_allowlist", lambda _app: (declared,))
    assert token_auth_app_scope.app_token_path_allowed("someapp", path) is allowed


# ── Providers the contract names but the gateway does not serve yet ──────────


@pytest.mark.parametrize("provider", ["linear"])
@pytest.mark.asyncio
async def test_planned_providers_answer_provider_not_supported(
    monkeypatch, _mock_source_sel, provider
) -> None:
    fake = _GitHub([])
    monkeypatch.setattr(source, "_run_json", fake)
    status, payload = await _post({"provider": provider, "scope": "CREW"})
    assert status == 400
    assert payload["code"] == "provider_not_supported"
    assert fake.calls == []
    assert _mock_source_sel.log_api_access.call_args.kwargs["error"] == "provider_not_supported"


# ── Invariant 2: boundary validation ─────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"provider": "bitbucket", "scope": "a/b"},
        {"provider": "github"},
        {"provider": "github", "scope": "acme"},
        {"provider": "github", "scope": "acme/repo/extra"},
        {"provider": "github", "scope": "acme/re po"},
        {"provider": "github", "scope": "--hostname/repo"},
        {"provider": "github", "scope": "acme/.."},
        {"provider": "github", "scope": "a/" + "r" * 600},
        {"provider": "github", "scope": "acme/repo", "host": "--hostname"},
        {"provider": "github", "scope": "acme/repo", "host": "evil.example"},
        {"provider": "github", "scope": "acme/repo", "host": "ghe.acme.dev:8443"},
        {"provider": "github", "scope": "acme/repo", "state": "merged"},
        {"provider": "github", "scope": "acme/repo", "sort": "comments"},
        {"provider": "github", "scope": "acme/repo", "order": "up"},
        {"provider": "github", "scope": "acme/repo", "assignee": "a b"},
        {"provider": "github", "scope": "acme/repo", "assignee": "--hostname"},
        {"provider": "github", "scope": "acme/repo", "author": "x:y"},
        {"provider": "github", "scope": "acme/repo", "author": "_lead"},
        {"provider": "github", "scope": "acme/repo", "labels": "bug"},
        {"provider": "github", "scope": "acme/repo", "labels": ['say "hi"']},
        {"provider": "github", "scope": "acme/repo", "labels": ["a,b"]},
        {"provider": "github", "scope": "acme/repo", "labels": [""]},
        {"provider": "github", "scope": "acme/repo", "labels": ["l"] * 21},
        {"provider": "github", "scope": "acme/repo", "labels": ["x" * 101]},
        {"provider": "github", "scope": "acme/repo", "text": "line\nbreak"},
        {"provider": "github", "scope": "acme/repo", "text": "x" * 257},
        {"provider": "github", "scope": "acme/repo", "limit": 0},
        {"provider": "github", "scope": "acme/repo", "limit": 101},
        {"provider": "github", "scope": "acme/repo", "limit": "25"},
        {"provider": "github", "scope": "acme/repo", "limit": True},
        {"provider": "github", "scope": "acme/repo", "cursor": "0"},
        {"provider": "github", "scope": "acme/repo", "cursor": "abc"},
        {"provider": "github", "scope": "acme/repo", "cursor": "1234567"},
        {"provider": "github", "scope": "acme/repo", "cursor": 2},
        {"provider": "github", "scope": "acme/repo", "raw": "repo:other/repo"},
        {"provider": "github", "scope": "acme/repo", "raw": "bug REPO:other/repo"},
        {"provider": "github", "scope": "acme/repo", "raw": "-repo:acme/repo"},
        {"provider": "github", "scope": "acme/repo", "raw": "org:other"},
        {"provider": "github", "scope": "acme/repo", "raw": 'user:"other"'},
        {"provider": "github", "scope": "acme/repo", "raw": "owner:other"},
        {"provider": "github", "scope": "acme/repo", "raw": "bug OR is:pr"},
        {"provider": "github", "scope": "acme/repo", "raw": "(label:bug)"},
        {"provider": "github", "scope": "acme/repo", "raw": '"unbalanced'},
        {"provider": "github", "scope": "acme/repo", "raw": "a\tb"},
        {"provider": "gitlab", "scope": "project-only"},
        {"provider": "gitlab", "scope": "grp/../proj"},
        {"provider": "gitlab", "scope": "grp/sub proj"},
        {"provider": "gitlab", "scope": "/".join(["g"] * 23)},
        {"provider": "gitlab", "scope": "grp/proj", "host": "gitlab.evil.example"},
        {"provider": "gitlab", "scope": "grp/proj", "host": "gitlab.acme.dev.evil"},
        {"provider": "gitlab", "scope": "grp/proj", "assignee": "a b"},
        {"provider": "gitlab", "scope": "grp/proj", "raw": "scope=all"},
        {"provider": "gitlab", "scope": "grp/proj", "raw": "per_page=1000"},
        {"provider": "gitlab", "scope": "grp/proj", "raw": "labels=a&labels=b"},
        {"provider": "gitlab", "scope": "grp/proj", "raw": "not a filter"},
        {"provider": "gitlab", "scope": "grp/proj", "raw": "labels=a%0Ab"},
    ],
)
@pytest.mark.asyncio
async def test_invalid_fields_are_refused_before_any_provider_call(monkeypatch, body) -> None:
    gh = _GitHub([])
    gl = _GitLab([])
    monkeypatch.setattr(source, "_run_json", gh)
    monkeypatch.setattr(source, "_run_provider", gl.run_provider)
    status, payload = await _post(body)
    assert status == 400, payload
    assert payload["code"] == "invalid_request"
    assert gh.calls == [] and gl.list_calls == []


@pytest.mark.asyncio
async def test_a_refusal_never_echoes_the_submitted_value(monkeypatch, _mock_source_sel) -> None:
    monkeypatch.setattr(source, "_run_json", _GitHub([]))
    status, payload = await _post(
        {"provider": "github", "scope": "acme/repo", "author": f"{_SECRET} x"}
    )
    assert status == 400
    assert _SECRET not in str(payload)
    assert _SECRET not in str(_mock_source_sel.log_api_access.call_args_list)


@pytest.mark.asyncio
async def test_github_structured_filters_build_one_scoped_query(monkeypatch) -> None:
    fake = _GitHub([_gh_item(4821)])
    monkeypatch.setattr(source, "_run_json", fake)
    status, _ = await _post(
        {
            "provider": "github",
            "scope": "acme/repo",
            "state": "closed",
            "assignee": "me",
            "author": "bob",
            "labels": ["good first issue", "bug"],
            "text": 'crash repo:other/x "OR" --hostname',
            "sort": "created",
            "order": "asc",
            "limit": 10,
        }
    )
    assert status == 200
    argv, kwargs = fake.calls[0]
    assert argv[:2] == ("gh", "api")
    assert argv[2].startswith("search/issues?")
    assert "--hostname" not in argv
    assert kwargs["host"] == "github.com"
    params = fake.params()
    assert params["q"] == (
        'repo:acme/repo is:issue is:closed assignee:@me author:bob label:"good first issue" '
        'label:"bug" "crash" "repo:other/x" "OR" "--hostname"'
    )
    assert (params["sort"], params["order"], params["per_page"], params["page"]) == (
        "created",
        "asc",
        "10",
        "1",
    )


@pytest.mark.asyncio
async def test_github_enterprise_managed_user_logins_are_accepted(monkeypatch) -> None:
    """EMU logins are ``<handle>_<shortcode>``; the single-issue read accepts them."""
    fake = _GitHub([_gh_item(1, repository_url="https://api.github.com/repos/mona-cat_octo/notes")])
    monkeypatch.setattr(source, "_run_json", fake)
    status, _ = await _post(
        {"provider": "github", "scope": "mona-cat_octo/notes", "assignee": "hubot_octo"}
    )
    assert status == 200
    assert fake.params()["q"] == "repo:mona-cat_octo/notes is:issue is:open assignee:hubot_octo"


@pytest.mark.asyncio
async def test_github_raw_replaces_filters_but_keeps_the_scope(monkeypatch) -> None:
    fake = _GitHub([])
    monkeypatch.setattr(source, "_run_json", fake)
    status, _ = await _post(
        {
            "provider": "github",
            "scope": "acme/repo",
            "state": "closed",
            "labels": ["ignored"],
            "raw": 'label:bug -label:"won\'t fix" no:assignee "repo:in a phrase" --hostname',
        }
    )
    assert status == 200
    # A flag-shaped raw term is only a search word inside the encoded path argument.
    assert "--hostname" not in fake.calls[0][0]
    assert fake.params()["q"] == (
        'repo:acme/repo is:issue label:bug -label:"won\'t fix" no:assignee "repo:in a phrase" '
        "--hostname"
    )


@pytest.mark.asyncio
async def test_github_enterprise_host_on_the_allowlist_is_pinned(monkeypatch) -> None:
    fake = _GitHub([_gh_item(7, repository_url="https://ghe.acme.dev/api/v3/repos/acme/repo")])
    monkeypatch.setattr(source, "_run_json", fake)
    status, payload = await _post(
        {"provider": "github", "scope": "acme/repo", "host": "GHE.acme.dev."}
    )
    assert status == 200
    assert fake.calls[0][1]["host"] == "ghe.acme.dev"
    assert payload["issues"][0]["url"] == "https://ghe.acme.dev/acme/repo/issues/7"


# ── Response projection and paging (GitHub) ─────────────────────────────────


@pytest.mark.asyncio
async def test_github_summary_shape_and_pull_requests_dropped(monkeypatch) -> None:
    items = [
        _gh_item(4821),
        _gh_item(4822, is_pr=True),
        _gh_item(4823, repository_url="https://api.github.com/repos/other/repo"),
        _gh_item(4824, state="closed", state_reason="not_planned"),
    ]
    monkeypatch.setattr(source, "_run_json", _GitHub(items, total=4))
    status, payload = await _post({"provider": "github", "scope": "acme/repo", "state": "all"})
    assert status == 200
    assert [issue["number"] for issue in payload["issues"]] == [4821, 4824]
    first = payload["issues"][0]
    assert first == {
        "provider": "github",
        "url": "https://github.com/acme/repo/issues/4821",
        "key": "#4821",
        "number": 4821,
        "title": "Issue 4821",
        "state": "open",
        "statusLabel": "Open",
        "labels": [{"name": "good first issue", "color": "7057ff", "description": "Newcomers"}],
        "assignees": [
            {"login": "alice", "name": "alice", "avatarUrl": "https://avatars.example/alice"}
        ],
        "author": {"login": "bob", "name": "bob", "avatarUrl": ""},
        "priority": None,
        "commentCount": 3,
        "createdAt": "2026-11-01T10:00:00Z",
        "updatedAt": "2026-11-09T10:00:00Z",
        "excerpt": "Steps Run crew with --x See the docs.",
        "project": "acme/repo",
        "linkedChanges": [],
    }
    assert payload["issues"][1]["state"] == "closed"
    assert payload["issues"][1]["statusLabel"] == "Not planned"
    assert payload["total"] == 4


@pytest.mark.asyncio
async def test_github_empty_results(monkeypatch) -> None:
    monkeypatch.setattr(source, "_run_json", _GitHub([], total=0))
    status, payload = await _post({"provider": "github", "scope": "acme/repo"})
    assert (status, payload) == (
        200,
        {"provider": "github", "scope": "acme/repo", "issues": [], "nextCursor": None, "total": 0},
    )


@pytest.mark.asyncio
async def test_github_paging_cursor_and_last_page(monkeypatch) -> None:
    full = _GitHub([_gh_item(n) for n in range(1, 26)], total=60)
    monkeypatch.setattr(source, "_run_json", full)
    _, first = await _post({"provider": "github", "scope": "acme/repo"})
    assert first["nextCursor"] == "2"

    last = _GitHub([_gh_item(n) for n in range(51, 61)], total=60)
    monkeypatch.setattr(source, "_run_json", last)
    _, third = await _post({"provider": "github", "scope": "acme/repo", "cursor": "3"})
    assert last.params()["page"] == "3"
    assert third["nextCursor"] is None

    beyond = _GitHub([], total=60)
    monkeypatch.setattr(source, "_run_json", beyond)
    _, past = await _post({"provider": "github", "scope": "acme/repo", "cursor": "9"})
    assert past["issues"] == [] and past["nextCursor"] is None


@pytest.mark.asyncio
async def test_github_paging_stops_at_the_search_window(monkeypatch) -> None:
    window = _GitHub([_gh_item(n) for n in range(100)], total=5000)
    monkeypatch.setattr(source, "_run_json", window)
    _, page10 = await _post(
        {"provider": "github", "scope": "acme/repo", "limit": 100, "cursor": "10"}
    )
    assert page10["nextCursor"] is None

    untouched = _GitHub([], total=5000)
    monkeypatch.setattr(source, "_run_json", untouched)
    status, page11 = await _post(
        {"provider": "github", "scope": "acme/repo", "limit": 100, "cursor": "11"}
    )
    assert status == 200 and page11["issues"] == [] and page11["nextCursor"] is None
    assert untouched.calls == []


@pytest.mark.asyncio
async def test_github_last_partial_page_reads_the_window_end(monkeypatch) -> None:
    """With limit 30, page 34 holds results 991-1,000: ``per_page=30&page=34`` would
    end at 1,020 (refused), so the read is the window's last 100 and skips 90."""
    body = {"provider": "github", "scope": "acme/repo", "limit": 30}
    page33 = _GitHub([_gh_item(n) for n in range(961, 991)], total=999)
    monkeypatch.setattr(source, "_run_json", page33)
    _, first = await _post({**body, "cursor": "33"})
    assert first["nextCursor"] == "34"

    window_end = _GitHub([_gh_item(n) for n in range(901, 1000)], total=999)
    monkeypatch.setattr(source, "_run_json", window_end)
    status, last = await _post({**body, "cursor": "34"})
    assert status == 200
    params = window_end.params()
    assert (params["per_page"], params["page"]) == ("100", "10")
    assert [issue["number"] for issue in last["issues"]] == list(range(991, 1000))
    assert last["nextCursor"] is None

    untouched = _GitHub([], total=5000)
    monkeypatch.setattr(source, "_run_json", untouched)
    status, past = await _post({**body, "cursor": "35"})
    assert status == 200 and past["issues"] == [] and untouched.calls == []


@pytest.mark.asyncio
async def test_signed_out_github_maps_like_the_single_issue_read(
    monkeypatch, _mock_source_sel
) -> None:
    async def signed_out(*_argv: str, **_kwargs: Any) -> Any:
        raise SourceProviderError("gh is not signed in", login_command="gh auth login")

    monkeypatch.setattr(source, "_run_json", signed_out)
    status, payload = await _post({"provider": "github", "scope": "acme/repo", "assignee": "me"})
    assert status == 503
    assert payload == {
        "error": "gh is not signed in",
        "code": "provider_error",
        "loginCommand": "gh auth login",
    }
    assert _mock_source_sel.log_api_access.call_args.kwargs["error"] == "provider_error"


# ── Invariant 3: the shared redaction path ───────────────────────────────────


@pytest.mark.asyncio
async def test_github_secret_in_title_or_body_comes_back_redacted(monkeypatch) -> None:
    leaky = _gh_item(9, title=f"leaked {_SECRET} here", body=f"use {_SECRET} to log in")
    monkeypatch.setattr(source, "_run_json", _GitHub([leaky]))
    status, payload = await _post({"provider": "github", "scope": "acme/repo"})
    assert status == 200
    issue = payload["issues"][0]
    assert _SECRET not in issue["title"] and _SECRET not in issue["excerpt"]
    # Redacted in place, not dropped: the text around the secret survives.
    assert issue["title"].startswith("leaked ") and issue["title"].endswith(" here")
    assert issue["excerpt"].startswith("use ")


@pytest.mark.asyncio
async def test_gitlab_secret_in_title_or_description_comes_back_redacted(monkeypatch) -> None:
    leaky = _gl_item(5, title=f"leaked {_SECRET} here", description=f"use {_SECRET} to log in")
    _GitLab([leaky]).install(monkeypatch)
    status, payload = await _post({"provider": "gitlab", "scope": "grp/proj"})
    assert status == 200
    issue = payload["issues"][0]
    assert _SECRET not in issue["title"] and _SECRET not in issue["excerpt"]
    assert issue["title"].startswith("leaked ") and issue["excerpt"].startswith("use ")


@pytest.mark.asyncio
async def test_gitlab_non_ascii_digit_headers_are_ignored_not_echoed(monkeypatch) -> None:
    """``"²".isdigit()`` is true but ``int("²")`` raises: the header is unknown, not a 400."""
    _GitLab([_gl_item(1)], headers={"X-Total": "1²", "X-Next-Page": "²"}).install(monkeypatch)
    status, payload = await _post({"provider": "gitlab", "scope": "grp/proj"})
    assert status == 200
    assert (payload["nextCursor"], payload["total"]) == (None, None)


@pytest.mark.asyncio
async def test_an_unexpected_value_error_from_a_read_is_not_echoed(
    monkeypatch, _mock_source_sel
) -> None:
    class CodedValueError(ValueError):
        code = "invalid_request"

    for error in (ValueError, CodedValueError):

        async def broken(*_argv: str, _error: type = error, **_kwargs: Any) -> Any:
            raise _error(f"bad provider bytes {_SECRET}")

        monkeypatch.setattr(source, "_run_json", broken)
        status, payload = await _post({"provider": "github", "scope": "acme/repo", "text": "x"})
        assert status == 503, error
        assert payload["code"] == "provider_error"
        assert _SECRET not in str(payload) and "bad provider bytes" not in str(payload)


@pytest.mark.parametrize(
    "body",
    [
        # The 280-character cut lands inside a token whose run began far earlier.
        "word " * 30 + "y" * 120 + _SECRET + " tail",
        # The local GPT review's case: 250 non-space characters, a colon, a token.
        "x" * 250 + ":" + _SECRET + " tail",
        "lead " + "x" * 245 + ":" + _SECRET + " tail",
    ],
)
def test_excerpt_never_keeps_a_cut_token_fragment(body) -> None:
    """The excerpt cut drops a cut trailing run rather than keep a fragment of it."""
    assert "ghp_" not in issue_search.plain_excerpt(body)


def test_github_bodies_are_fetched_whole_for_redaction() -> None:
    """Redaction runs on the fetched rows, so the jq projection must not cut a body:
    a cut can drop a PEM key's END line or split a token, and the redactor then
    misses what is left. The excerpt is cut only after redaction."""
    assert 'body: (.body // "")' in issue_search._GITHUB_SEARCH_JQ
    assert ".[0:" not in issue_search._GITHUB_SEARCH_JQ


@pytest.mark.parametrize("indent", ["", "    ", "> ", "  "])
@pytest.mark.asyncio
async def test_a_pem_key_ending_past_the_excerpt_source_window_is_redacted(
    monkeypatch, indent
) -> None:
    """A long template comment, then an indented key whose END line lies past
    character 4,000: the excerpt strip moves the key lines into the excerpt."""
    lines = [f"MIIE{n:02d}" + "A" * 60 for n in range(40)]
    key = "\n".join(indent + line for line in [_PEM_BEGIN, *lines, _PEM_END])
    body = "<!-- " + "template " * 285 + "-->\n" + key + "\n"
    assert body.index("-----END") > 4000
    monkeypatch.setattr(source, "_run_json", _GitHub([_gh_item(3, body=body)]))
    status, payload = await _post({"provider": "github", "scope": "acme/repo"})
    assert status == 200
    assert "MIIE" not in str(payload)


_PEM_BODY = (
    f"Our deploy key:\n{_DASHES}BEGIN RSA PRIVATE KEY{_DASHES}\n"
    + "\n".join(f"MIIEow{n}AAKCAQEAxQQ" + "Q" * 50 for n in range(6))
    + "\n"
)


@pytest.mark.asyncio
async def test_github_pem_key_in_a_body_is_redacted_before_the_excerpt_joins_lines(
    monkeypatch,
) -> None:
    """A key is only recognised across its newlines, which the excerpt collapses."""
    monkeypatch.setattr(source, "_run_json", _GitHub([_gh_item(4, body=_PEM_BODY)]))
    status, payload = await _post({"provider": "github", "scope": "acme/repo"})
    assert status == 200
    excerpt = payload["issues"][0]["excerpt"]
    assert "MIIEow" not in str(payload) and excerpt.startswith("Our deploy key:")


@pytest.mark.asyncio
async def test_a_token_joined_by_the_markdown_strip_is_redacted(monkeypatch) -> None:
    """Neither half matches in the raw body; stripping ``**`` joins them into one."""
    joined = "ghp_" + "b" * 16 + "**" + "b" * 20
    monkeypatch.setattr(source, "_run_json", _GitHub([_gh_item(6, body=f"see {joined} now")]))
    status, payload = await _post({"provider": "github", "scope": "acme/repo"})
    assert status == 200
    assert _SECRET not in str(payload) and payload["issues"][0]["excerpt"].startswith("see ")


_SPLIT_HEAD = "ghp_" + "b" * 16


@pytest.mark.parametrize(
    "joiner",
    [
        "**",
        "``",
        # A link whose url holds a space (the diff-only audit's earlier case).
        "[x](a b)",
        # A comment spanning lines inside a link url (the latest audit's case):
        # comment, then link removal, joins text from different source lines.
        "[](<!--\n" + "filler line\n" * 400 + "-->)",
    ],
)
@pytest.mark.parametrize("lead", [0, 3970, 8000])
@pytest.mark.asyncio
async def test_a_token_the_strip_joins_is_redacted_wherever_it_sits(
    monkeypatch, joiner, lead
) -> None:
    """A token the raw pass cannot see (split by markup) forms only when the strip
    joins it. The strip runs on the whole body and the only cut is at a word, so the
    final pass sees the joined token whole, however far into the body it starts."""
    body = "<!--" + "c" * lead + "--> see " + _SPLIT_HEAD + joiner + "b" * 20 + " now\nmore"
    monkeypatch.setattr(source, "_run_json", _GitHub([_gh_item(8, body=body)]))
    status, payload = await _post({"provider": "github", "scope": "acme/repo"})
    assert status == 200
    assert "ghp_" not in str(payload)
    assert payload["issues"][0]["excerpt"].startswith("see ")


def test_an_excerpt_with_no_word_boundary_is_empty() -> None:
    assert issue_search.plain_excerpt("y" * 5000) == ""


@pytest.mark.asyncio
async def test_a_token_the_strip_would_shorten_is_redacted_on_the_raw_row(monkeypatch) -> None:
    """``__`` is valid inside a GitLab token, and the strip deletes it, leaving a
    run too short for the token rule: only the raw-row pass sees the whole token."""
    token = "glpat-" + "a" * 10 + "__" + "b" * 5
    monkeypatch.setattr(source, "_run_json", _GitHub([_gh_item(7, body=f"use {token} now")]))
    status, payload = await _post({"provider": "github", "scope": "acme/repo"})
    assert status == 200
    assert "aaaaaaaaaa" not in str(payload)


@pytest.mark.parametrize("width", [32, 64])
@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.asyncio
async def test_a_pem_header_formed_by_the_strip_is_redacted_before_the_cut(
    monkeypatch, width, provider
) -> None:
    """The CI GPT review's case: emphasis inside the header hides it from the raw
    pass, the strip rebuilds it, and the END line lies past the 280-character cut.
    The joined text is redacted with its newlines kept, before the excerpt cut."""
    header = f"{_DASHES}BEGIN **PRIVATE** KEY{_DASHES}"
    lines = [f"MIIE{n:02d}" + "Q" * (width - 6) for n in range(30)]
    body = "\n".join(["Key below:", header, *lines, _PEM_END, "done"])
    if provider == "github":
        monkeypatch.setattr(source, "_run_json", _GitHub([_gh_item(5, body=body)]))
        scope = "acme/repo"
    else:
        _GitLab([_gl_item(5, description=body)]).install(monkeypatch)
        scope = "grp/proj"
    status, payload = await _post({"provider": provider, "scope": scope})
    assert status == 200
    excerpt = payload["issues"][0]["excerpt"]
    assert "MIIE" not in str(payload) and excerpt.startswith("Key below:")


@pytest.mark.asyncio
async def test_gitlab_pem_key_in_a_description_is_redacted(monkeypatch) -> None:
    _GitLab([_gl_item(4, description=_PEM_BODY)]).install(monkeypatch)
    status, payload = await _post({"provider": "gitlab", "scope": "grp/proj"})
    assert status == 200
    assert "MIIEow" not in str(payload)
    assert payload["issues"][0]["excerpt"].startswith("Our deploy key:")


@pytest.mark.asyncio
async def test_a_cached_answer_is_the_redacted_one(monkeypatch) -> None:
    fake = _GitHub([_gh_item(9, title=f"leaked {_SECRET}")])
    monkeypatch.setattr(source, "_run_json", fake)
    body = {"provider": "github", "scope": "acme/repo"}
    await _post(body)
    status, payload = await _post(body)
    assert status == 200 and len(fake.calls) == 1
    assert _SECRET not in str(payload)
    assert _SECRET not in str(list(cache._SEARCH_CACHE.values()))


@pytest.mark.asyncio
async def test_results_are_cached_until_refresh(monkeypatch) -> None:
    fake = _GitHub([_gh_item(1)])
    monkeypatch.setattr(source, "_run_json", fake)
    body = {"provider": "github", "scope": "acme/repo"}
    await _post(body)
    await _post(body)
    assert len(fake.calls) == 1
    await _post({**body, "refresh": True})
    assert len(fake.calls) == 2


# ── GitLab ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gitlab_nested_group_and_structured_filters(monkeypatch) -> None:
    gl = _GitLab([_gl_item(142)], headers={"X-Total": "57", "X-Next-Page": "2"})
    gl.install(monkeypatch)
    status, payload = await _post(
        {
            "provider": "gitlab",
            "scope": "acme/platform/service",
            "author": "me",
            "assignee": "carol",
            "labels": ["needs triage", "bug"],
            "text": "crash & burn",
        }
    )
    assert status == 200
    argv, kwargs = gl.list_calls[0]
    assert argv[:2] == ("glab", "api") and argv[3] == "--include"
    assert kwargs["host"] == "gitlab.com"
    assert unquote(urlsplit(gl.path()).path) == "projects/acme/platform/service/issues"
    assert urlsplit(gl.path()).path == "projects/acme%2Fplatform%2Fservice/issues"
    assert gl.params() == [
        ("scope", "all"),
        ("with_labels_details", "true"),
        ("order_by", "updated_at"),
        ("sort", "desc"),
        ("per_page", "25"),
        ("page", "1"),
        ("state", "opened"),
        ("assignee_username", "carol"),
        ("author_username", "me-user"),
        ("labels", "needs triage,bug"),
        ("search", "crash & burn"),
    ]
    assert gl.user_calls == [("glab", "api", "user")]
    assert payload["nextCursor"] == "2"
    assert payload["total"] == 57
    issue = payload["issues"][0]
    assert issue["url"] == "https://gitlab.com/acme/platform/service/-/issues/142"
    assert issue["key"] == "#142"
    assert issue["labels"] == [{"name": "needs triage", "color": "ff0000", "description": ""}]
    assert issue["assignees"] == [{"login": "carol", "name": "Carol C", "avatarUrl": "https://a/c"}]
    assert issue["author"]["login"] == "dave"
    assert issue["commentCount"] == 2
    assert issue["project"] == "acme/platform/service"


@pytest.mark.asyncio
async def test_gitlab_assignee_me_becomes_the_signed_in_username(monkeypatch) -> None:
    gl = _GitLab([])
    gl.install(monkeypatch)
    status, _ = await _post({"provider": "gitlab", "scope": "grp/proj", "assignee": "me"})
    assert status == 200
    assert ("assignee_username", "me-user") in gl.params()
    assert ("assignee_username", "me") not in gl.params()


@pytest.mark.asyncio
async def test_gitlab_closed_only_last_page_and_unknown_total(monkeypatch) -> None:
    gl = _GitLab([_gl_item(3, state="closed")], headers={"X-Next-Page": ""})
    gl.install(monkeypatch)
    status, payload = await _post(
        {"provider": "gitlab", "scope": "grp/proj", "state": "closed", "cursor": "4"}
    )
    assert status == 200
    assert ("state", "closed") in gl.params() and ("page", "4") in gl.params()
    assert payload["issues"][0]["state"] == "closed"
    assert payload["issues"][0]["statusLabel"] == "Closed"
    assert payload["nextCursor"] is None
    assert payload["total"] is None


@pytest.mark.asyncio
async def test_gitlab_empty_results_past_the_last_page(monkeypatch) -> None:
    gl = _GitLab([], headers={"X-Total": "12", "X-Next-Page": ""})
    gl.install(monkeypatch)
    status, payload = await _post({"provider": "gitlab", "scope": "grp/proj", "cursor": "7"})
    assert (status, payload["issues"], payload["nextCursor"], payload["total"]) == (
        200,
        [],
        None,
        12,
    )


@pytest.mark.asyncio
async def test_gitlab_allowlisted_self_managed_host_is_pinned(monkeypatch) -> None:
    gl = _GitLab([_gl_item(5)])
    gl.install(monkeypatch)
    status, payload = await _post(
        {"provider": "gitlab", "scope": "grp/proj", "host": "gitlab.acme.dev"}
    )
    assert status == 200
    assert gl.list_calls[0][1]["host"] == "gitlab.acme.dev"
    assert payload["issues"][0]["url"] == "https://gitlab.acme.dev/grp/proj/-/issues/5"


@pytest.mark.asyncio
async def test_gitlab_assignee_me_with_no_signed_in_user_fails_closed(
    monkeypatch, _mock_source_sel
) -> None:
    gl = _GitLab([_gl_item(1)], user=SourceProviderError("glab: 401 Unauthorized"))
    gl.install(monkeypatch)
    status, payload = await _post({"provider": "gitlab", "scope": "grp/proj", "assignee": "me"})
    assert status == 503
    assert payload["code"] == "provider_error"
    assert gl.list_calls == []


@pytest.mark.asyncio
async def test_gitlab_user_without_a_username_fails_closed(monkeypatch) -> None:
    gl = _GitLab([_gl_item(1)], user={"username": ""})
    gl.install(monkeypatch)
    status, _ = await _post({"provider": "gitlab", "scope": "grp/proj", "author": "me"})
    assert status == 503
    assert gl.list_calls == []


@pytest.mark.asyncio
async def test_gitlab_raw_filter_is_re_encoded_after_the_gateway_params(monkeypatch) -> None:
    gl = _GitLab([])
    gl.install(monkeypatch)
    status, _ = await _post(
        {
            "provider": "gitlab",
            "scope": "grp/proj",
            "state": "closed",
            "raw": "labels=bug,ux&not[labels]=wontfix&milestone=v1&search=a%26per_page%3D1",
        }
    )
    assert status == 200
    params = gl.params()
    assert params[:6] == [
        ("scope", "all"),
        ("with_labels_details", "true"),
        ("order_by", "updated_at"),
        ("sort", "desc"),
        ("per_page", "25"),
        ("page", "1"),
    ]
    assert params[6:] == [
        ("labels", "bug,ux"),
        ("not[labels]", "wontfix"),
        ("milestone", "v1"),
        ("search", "a&per_page=1"),
    ]
    assert ("state", "closed") not in params


def test_gitlab_list_parse_refuses_output_without_a_status_line() -> None:
    parse = issue_search._parse_gitlab_list("gitlab.com")
    with pytest.raises(SourceProviderError):
        parse(0, b"[]", b"")
    headers, rows = parse(0, b"HTTP/1.1 200 OK\nX-Total: 1\n\n[{}]", b"")
    assert headers == {"x-total": "1"} and rows == [{}]


# ── Excerpt ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (None, ""),
        ("", ""),
        ("<!-- only a template -->", ""),
        ("```py\nprint(1)\n```\nafter", "print(1) after"),
        ("![shot](https://x/y.png) and <b>bold</b>", "shot and bold"),
        ("> quoted\n> > twice\n1. one\n- [x] done", "quoted twice one done"),
        ("snake_case stays | table", "snake_case stays table"),
        ("unclosed <!-- comment to the end", "unclosed"),
        ("see [the docs](https://d/x) now", "see the docs now"),
        ("[[nested](https://u)] ok", "[nested] ok"),
    ],
)
def test_plain_excerpt(body, expected) -> None:
    assert issue_search.plain_excerpt(body) == expected


@pytest.mark.parametrize(
    ("opener", "text"),
    [
        ("[", "[a[b](https://u)"),
        ("![", "![a![b](https://u)"),
        ("[", "[a](x(https://u)"),
        ("![", "![a](x(https://u)"),
    ],
)
def test_link_strip_tries_stop_at_the_next_bracket(opener, text) -> None:
    """Anyone can file a body of repeated ``![`` or ``[``: a strip try that could
    cross the next bracket rescans to the end of the line from every one of them
    (quadratic, ~16 s on the event loop for a page of 100 such issues). Stopping
    at the next ``[`` or ``(`` keeps each try local, so the strip is linear.

    The try at offset 0 fails, so the opener there is kept. (This asserted
    ``_LINK_RE``/``_IMAGE_RE.match(text) is None`` while the strip used them; the
    old patterns are oracles in test_source_issue_search_string_parity.py.)"""
    assert issue_search._strip_bracket_links(text, opener).startswith(opener)


def test_bracket_runs_strip_to_the_inner_link() -> None:
    assert issue_search.plain_excerpt("![" * 50 + "a](https://u)") == "![" * 49 + "a"


def test_plain_excerpt_is_cut_near_280_characters() -> None:
    excerpt = issue_search.plain_excerpt("word " * 200)
    assert excerpt.endswith("…")
    assert 240 <= len(excerpt) <= 281
