"""Linear issues: the single-issue read and ``POST /api/source/issues`` search.

Every test drives the real routes against a fake Linear GraphQL endpoint (the one
``linear._post`` seam) with the API key in a real vault, so the vault wiring, the
owner gate, the redaction in ``cache`` and the error mapping are the product's own.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from source_providers_helpers import _clear_shared_fetch_state  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401
from source_providers_helpers import _app

from kiro_crew.dashboard.source_providers import cache, linear, links
from kiro_crew.secrets import SecretVault

_KEY = "lin_api_" + "k" * 40
_SECRET = "ghp_" + "b" * 36
_URL = "https://linear.app/acme/issue/ENG-77"


@pytest.fixture(autouse=True)
def _isolated(_floor_monkeypatch, tmp_path):
    """An empty vault of our own and empty caches for every test."""
    _floor_monkeypatch.setattr(linear.config_loader, "config_dir", lambda: tmp_path)
    cache._SEARCH_CACHE.clear()
    yield
    cache._SEARCH_CACHE.clear()


@pytest.fixture
def stored_key(tmp_path) -> str:
    SecretVault(tmp_path).set_sync(linear.API_KEY_NAME, _KEY)
    return _KEY


def _node(number: int, **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "number": float(number),
        "title": f"Issue {number}",
        "description": "## Steps\n- Run **crew**\nSee [docs](https://docs.example/x).",
        "priority": 2,
        "priorityLabel": "High",
        "createdAt": "2026-11-01T10:00:00.000Z",
        "updatedAt": "2026-11-09T10:00:00.000Z",
        "state": {"name": "In Progress", "type": "started"},
        "team": {"key": "ENG"},
        "creator": {"name": "Bob B", "displayName": "bob", "avatarUrl": "javascript:alert(1)"},
        "assignee": {"name": "Alice A", "displayName": "alice", "avatarUrl": "https://a/al"},
        "labels": {"nodes": [{"name": "Bug", "color": "#eb5757", "description": "Broken"}]},
        "url": "https://evil.example/elsewhere",
    }
    node.update(extra)
    return node


def _issues(nodes: list[dict[str, Any]], more: bool = False) -> dict[str, Any]:
    return {
        "data": {
            "organization": {"urlKey": "acme"},
            "issues": {"pageInfo": {"hasNextPage": more, "endCursor": "c-end"}, "nodes": nodes},
        }
    }


def _issue(**extra: Any) -> dict[str, Any]:
    issue = _node(
        77,
        url="https://linear.app/acme/issue/ENG-77/crash-on-start",
        completedAt=None,
        canceledAt=None,
        comments={
            "nodes": [
                {
                    "id": "c2",
                    "body": "second",
                    "createdAt": "2026-11-03T00:00:00.000Z",
                    "url": "https://linear.app/acme/issue/ENG-77#comment-c2",
                    "user": {"name": "Carol", "displayName": "carol"},
                },
                {
                    "id": "c1",
                    "body": "first",
                    "createdAt": "2026-11-02T00:00:00.000Z",
                    "url": "javascript:alert(1)",
                    "user": None,
                },
            ]
        },
    )
    issue.update(extra)
    return {"data": {"issue": issue}}


class _Linear:
    """A fake Linear GraphQL endpoint: answers by operation name, records each call."""

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.calls: list[dict[str, Any]] = []
        self.keys: list[str] = []

    async def __call__(self, body: bytes, key: str) -> tuple[int, bytes]:
        request = json.loads(body)
        self.calls.append(request)
        self.keys.append(key)
        operation = request["query"].split("query ", 1)[1].split()[0].split("(")[0]
        answer = self.answers[operation]
        if isinstance(answer, BaseException):
            raise answer
        status, payload = answer if isinstance(answer, tuple) else (200, answer)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, raw

    def variables(self, index: int = -1) -> dict[str, Any]:
        return self.calls[index]["variables"]


async def _post(path: str, body: Any) -> tuple[int, Any]:
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(path, json=body)
        return response.status, await response.json()


async def _search(**body: Any) -> tuple[int, Any]:
    return await _post("/api/source/issues", {"provider": "linear", "scope": "ENG", **body})


# ── The API key ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/source/issues", {"provider": "linear", "scope": "ENG"}),
        ("/api/source/issue", {"url": _URL}),
        ("/api/source/issue", {"provider": "linear", "key": "ENG-77"}),
    ],
)
async def test_a_missing_key_asks_for_one_before_any_call(monkeypatch, path, body) -> None:
    fake = _Linear()
    monkeypatch.setattr(linear, "_post", fake)
    status, payload = await _post(path, body)
    assert status == 503
    assert payload["code"] == "provider_error"
    assert "Add a Linear API key" in payload["error"]
    assert "LINEAR_API_KEY" in payload["error"]
    assert fake.calls == []


@pytest.mark.asyncio
async def test_the_key_is_sent_only_as_the_header_and_never_returned(
    monkeypatch, stored_key, caplog, _mock_source_sel
) -> None:
    # Linear's own error text repeats the key; none of it may come back.
    leak = {"errors": [{"message": f"bad key {_KEY}", "extensions": {"code": "OTHER"}}]}
    fake = _Linear(KiroCrewLinearIssues=(400, leak), KiroCrewLinearIssue=(401, leak))
    monkeypatch.setattr(linear, "_post", fake)
    caplog.set_level(logging.DEBUG)
    search_status, search = await _search()
    issue_status, issue = await _post("/api/source/issue", {"url": _URL})
    assert (search_status, issue_status) == (503, 503)
    assert fake.keys == [_KEY, _KEY]
    assert all(_KEY not in json.dumps(call) for call in fake.calls)
    for text in (json.dumps(search), json.dumps(issue), caplog.text):
        assert _KEY not in text
    assert _KEY not in str(_mock_source_sel.log_api_access.call_args_list)
    assert "Replace LINEAR_API_KEY" in issue["error"]


@pytest.mark.asyncio
async def test_a_key_with_whitespace_is_refused_unsent(monkeypatch, tmp_path) -> None:
    SecretVault(tmp_path).set_sync(linear.API_KEY_NAME, "lin api key")
    fake = _Linear()
    monkeypatch.setattr(linear, "_post", fake)
    status, payload = await _search()
    assert status == 503
    assert "lin api key" not in payload["error"]
    assert fake.calls == []


# ── Errors ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ((200, {"errors": [{"extensions": {"code": "AUTHENTICATION_ERROR"}}]}), "rejected"),
        ((429, b"slow down"), "rate limit"),
        ((200, {"errors": [{"extensions": {"code": "RATELIMITED"}}]}), "rate limit"),
        ((200, {"errors": [{"extensions": {"code": "FORBIDDEN"}}]}), "not allowed"),
        ((400, {"errors": [{"message": "Field 'x' is not defined"}]}), "raw IssueFilter"),
        ((502, b"<html>bad gateway</html>"), "HTTP 502"),
        ((200, {"data": None}), "HTTP 200"),
        (aiohttp.ClientConnectionError("down"), "Could not reach Linear"),
    ],
)
async def test_provider_errors_map_to_fixed_messages(
    monkeypatch, stored_key, answer, expected
) -> None:
    monkeypatch.setattr(linear, "_post", _Linear(KiroCrewLinearIssues=answer))
    status, payload = await _search()
    assert status == 503
    assert payload["code"] == "provider_error"
    assert expected in payload["error"]
    assert "Field 'x'" not in payload["error"]


@pytest.mark.asyncio
async def test_an_unknown_issue_says_so(monkeypatch, stored_key) -> None:
    missing = {"data": None, "errors": [{"message": "Entity not found: Issue"}]}
    monkeypatch.setattr(linear, "_post", _Linear(KiroCrewLinearIssue=(200, missing)))
    status, payload = await _post("/api/source/issue", {"url": _URL})
    assert status == 503
    assert "ENG-77 was not found" in payload["error"]
    assert "Entity" not in payload["error"]


# ── One issue ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_issue_url_reads_the_issue_in_the_shared_shape(monkeypatch, stored_key) -> None:
    fake = _Linear(KiroCrewLinearIssue=_issue())
    monkeypatch.setattr(linear, "_post", fake)
    status, payload = await _post(
        "/api/source/issue", {"url": "https://linear.app/Acme/issue/eng-77/crash-on-start"}
    )
    assert status == 200
    assert fake.variables() == {"id": "ENG-77", "labels": 50, "comments": 51}
    assert payload["provider"] == "linear"
    assert payload["url"] == _URL
    assert payload["number"] == 77
    assert payload["state"] == "open" and payload["stateReason"] == ""
    assert payload["author"] == "bob" and payload["assignees"] == ["alice"]
    assert [label["name"] for label in payload["labels"]] == ["Priority: High", "Bug"]
    assert payload["labels"][1]["color"] == "eb5757"
    assert [c["body"] for c in payload["comments"]] == ["first", "second"]
    assert payload["comments"][0]["url"] == "" and payload["comments"][0]["author"] == ""
    assert payload["commentCount"] == 2 and payload["partialSections"] == []
    assert set(payload) >= {
        "description",
        "closedAt",
        "milestone",
        "locked",
        "reactions",
        "linkedChanges",
        "failedSections",
    }


@pytest.mark.asyncio
async def test_a_canceled_issue_is_closed_as_not_planned(monkeypatch, stored_key) -> None:
    answer = _issue(state={"name": "Duplicate", "type": "canceled"}, canceledAt="2026-11-05")
    monkeypatch.setattr(linear, "_post", _Linear(KiroCrewLinearIssue=answer))
    _status, payload = await _post("/api/source/issue", {"url": _URL})
    assert (payload["state"], payload["stateReason"], payload["closedAt"]) == (
        "closed",
        "not_planned",
        "2026-11-05",
    )


@pytest.mark.asyncio
async def test_more_comments_than_the_cap_mark_the_section_partial(monkeypatch, stored_key) -> None:
    rows = [
        {"id": str(i), "body": "x", "createdAt": f"2026-11-01T00:00:{i:02d}", "user": None}
        for i in range(51)
    ]
    monkeypatch.setattr(
        linear, "_post", _Linear(KiroCrewLinearIssue=_issue(comments={"nodes": rows}))
    )
    _status, payload = await _post("/api/source/issue", {"url": _URL})
    assert payload["commentCount"] == 50
    assert payload["partialSections"] == ["comments"]


@pytest.mark.asyncio
async def test_a_key_resolves_in_the_api_keys_workspace(monkeypatch, stored_key) -> None:
    fake = _Linear(
        KiroCrewLinearOrganization={"data": {"organization": {"urlKey": "acme"}}},
        KiroCrewLinearIssue=_issue(),
    )
    monkeypatch.setattr(linear, "_post", fake)
    status, payload = await _post("/api/source/issue", {"provider": "linear", "key": " eng-77 "})
    assert status == 200
    assert payload["url"] == _URL
    assert [c["variables"] for c in fake.calls][1]["id"] == "ENG-77"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "ENG", "77", "ENG-0", "ENG-07", "E NG-1", 77, None])
async def test_a_malformed_key_is_refused_unsent(monkeypatch, stored_key, key) -> None:
    fake = _Linear()
    monkeypatch.setattr(linear, "_post", fake)
    status, payload = await _post("/api/source/issue", {"provider": "linear", "key": key})
    assert status == 400 and payload["code"] == "invalid_request"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_a_link_into_another_workspace_is_refused(monkeypatch, stored_key) -> None:
    other = _issue(url="https://linear.app/othercorp/issue/ENG-77/same-key")
    monkeypatch.setattr(linear, "_post", _Linear(KiroCrewLinearIssue=other))
    status, payload = await _post("/api/source/issue", {"url": _URL})
    assert status == 503
    assert "not in the acme workspace" in payload["error"]


@pytest.mark.asyncio
async def test_secrets_in_an_issue_come_back_redacted(monkeypatch, stored_key) -> None:
    leaky = _issue(title=f"leaked {_SECRET}", description=f"use {_SECRET} to log in")
    monkeypatch.setattr(linear, "_post", _Linear(KiroCrewLinearIssue=leaky))
    status, payload = await _post("/api/source/issue", {"url": _URL})
    assert status == 200
    assert _SECRET not in json.dumps(payload)


@pytest.mark.parametrize(
    "url",
    [
        "https://linear.app/acme/issue/ENG-77",
        "https://www.linear.app/acme/issue/ENG-77/",
        "https://linear.app:443/ACME/issue/eng-77/a-slug",
    ],
)
def test_linear_urls_normalize_to_one_ref(url) -> None:
    ref = links.parse_source_url(url)
    assert (ref.provider, ref.url, ref.owner, ref.repo, ref.number, ref.kind) == (
        "linear",
        _URL,
        "acme",
        "ENG",
        77,
        "issue",
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://linear.app/acme/issue/ENG-77/slug/extra",
        "https://linear.app/acme/issues/ENG-77",
        "https://linear.app/acme/issue/77",
        "https://linear.app/ac_me/issue/ENG-77",
        "https://linear.app/acme/issue/ENG-0",
        "https://linear.app:8443/acme/issue/ENG-77",
        "https://linear.app/acme/project/ENG-77",
    ],
)
def test_other_linear_urls_are_refused(url) -> None:
    with pytest.raises(ValueError):
        links.parse_source_url(url)


# ── Search ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_structured_filters_become_variables_under_the_team(monkeypatch, stored_key) -> None:
    fake = _Linear(KiroCrewLinearIssues=_issues([_node(1)]))
    monkeypatch.setattr(linear, "_post", fake)
    status, _payload = await _search(
        scope="eng",
        state="closed",
        assignee="me",
        author="dana@acme.dev",
        labels=["Bug", "UI"],
        text='crash") { id } #',
        sort="created",
        order="asc",
        limit=10,
    )
    assert status == 200
    assert fake.calls[0]["query"] == linear._SEARCH_QUERY
    assert fake.variables() == {
        "filter": {
            "and": [
                {"team": {"key": {"eq": "ENG"}}},
                {"state": {"type": {"in": ["completed", "canceled"]}}},
                {"assignee": {"isMe": {"eq": True}}},
                {"creator": {"email": {"eqIgnoreCase": "dana@acme.dev"}}},
                {"labels": {"some": {"name": {"eqIgnoreCase": "Bug"}}}},
                {"labels": {"some": {"name": {"eqIgnoreCase": "UI"}}}},
                {"searchableContent": {"contains": 'crash") { id } #'}},
            ]
        },
        "sort": [{"createdAt": {"order": "Ascending"}}],
        "first": 10,
        "after": None,
        "labels": 20,
    }


@pytest.mark.asyncio
async def test_a_display_name_with_spaces_is_searched(monkeypatch, stored_key) -> None:
    fake = _Linear(KiroCrewLinearIssues=_issues([_node(1)]))
    monkeypatch.setattr(linear, "_post", fake)
    status, _payload = await _search(assignee="Alice Smith", author="Dana Lee")
    assert status == 200
    clauses = fake.variables()["filter"]["and"]
    assert {"assignee": {"displayName": {"eqIgnoreCase": "Alice Smith"}}} in clauses
    assert {"creator": {"displayName": {"eqIgnoreCase": "Dana Lee"}}} in clauses


@pytest.mark.asyncio
async def test_default_search_is_open_issues_newest_update_first(monkeypatch, stored_key) -> None:
    fake = _Linear(KiroCrewLinearIssues=_issues([]))
    monkeypatch.setattr(linear, "_post", fake)
    await _search(assignee="alice")
    variables = fake.variables()
    assert variables["filter"]["and"][1:] == [
        {"state": {"type": {"nin": ["completed", "canceled"]}}},
        {"assignee": {"displayName": {"eqIgnoreCase": "alice"}}},
    ]
    assert variables["sort"] == [{"updatedAt": {"order": "Descending"}}]
    assert variables["first"] == 25


@pytest.mark.asyncio
async def test_summary_shape(monkeypatch, stored_key) -> None:
    monkeypatch.setattr(linear, "_post", _Linear(KiroCrewLinearIssues=_issues([_node(42)])))
    status, payload = await _search()
    assert status == 200
    assert payload == {
        "provider": "linear",
        "scope": "ENG",
        "nextCursor": None,
        "total": None,
        "issues": [
            {
                "provider": "linear",
                "url": "https://linear.app/acme/issue/ENG-42",
                "key": "ENG-42",
                "number": 42,
                "title": "Issue 42",
                "state": "open",
                "statusLabel": "In Progress",
                "labels": [{"name": "Bug", "color": "eb5757", "description": "Broken"}],
                "assignees": [{"login": "alice", "name": "Alice A", "avatarUrl": "https://a/al"}],
                "author": {"login": "bob", "name": "Bob B", "avatarUrl": ""},
                "priority": "High",
                "commentCount": 0,
                "createdAt": "2026-11-01T10:00:00.000Z",
                "updatedAt": "2026-11-09T10:00:00.000Z",
                "excerpt": "Steps Run crew See docs.",
                "project": "ENG",
                "linkedChanges": [],
            }
        ],
    }


@pytest.mark.asyncio
async def test_empty_results(monkeypatch, stored_key) -> None:
    monkeypatch.setattr(linear, "_post", _Linear(KiroCrewLinearIssues=_issues([])))
    status, payload = await _search()
    assert status == 200
    assert (payload["issues"], payload["nextCursor"], payload["total"]) == ([], None, None)


# ── Team scope ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_raw_filter_cannot_leave_the_team(monkeypatch, stored_key) -> None:
    raw = {"or": [{"team": {"key": {"eq": "OPS"}}}, {"team": {"key": {"neq": "ENG"}}}]}
    rows = [_node(1), _node(2, team={"key": "OPS"}), _node(3, team=None)]
    fake = _Linear(KiroCrewLinearIssues=_issues(rows))
    monkeypatch.setattr(linear, "_post", fake)
    status, payload = await _search(raw=json.dumps(raw), state="all", assignee="me")
    assert status == 200
    # The raw filter replaces the structured ones but stays ANDed under the team.
    assert fake.variables()["filter"] == {"and": [{"team": {"key": {"eq": "ENG"}}}, raw]}
    assert [issue["key"] for issue in payload["issues"]] == ["ENG-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"scope": 'ENG"}'},
        {"scope": "ENG OPS"},
        {"scope": "1ENG"},
        {"scope": "ENG", "host": "evil.example"},
        {"scope": "ENG", "raw": "[1]"},
        {"scope": "ENG", "raw": "{}"},
        {"scope": "ENG", "raw": "team = OPS"},
        {"scope": "ENG", "raw": '{"te-am": {"key": {"eq": "OPS"}}}'},
        {"scope": "ENG", "raw": '{"number": {"eq": NaN}}'},
        {"scope": "ENG", "raw": '{"number": {"eq": 1e999}}'},
        {"scope": "ENG", "raw": '{"title": {"eq": "a\\nb"}}'},
        {"scope": "ENG", "raw": '{"a":' * 9 + "1" + "}" * 9},
    ],
)
async def test_invalid_fields_are_refused_before_any_call(monkeypatch, stored_key, body) -> None:
    fake = _Linear()
    monkeypatch.setattr(linear, "_post", fake)
    status, payload = await _post("/api/source/issues", {"provider": "linear", **body})
    assert status == 400 and payload["code"] == "invalid_request"
    assert fake.calls == []


# ── Paging ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_later_page_walks_the_earlier_boundaries(monkeypatch, stored_key) -> None:
    boundary = {"data": {"issues": {"pageInfo": {"hasNextPage": True, "endCursor": "c-250"}}}}
    fake = _Linear(KiroCrewLinearIssuePage=boundary, KiroCrewLinearIssues=_issues([_node(5)], True))
    monkeypatch.setattr(linear, "_post", fake)
    status, payload = await _search(limit=100, cursor="4")
    assert status == 200
    walks = [c["variables"] for c in fake.calls[:-1]]
    assert [(v["first"], v["after"]) for v in walks] == [(250, None), (50, "c-250")]
    assert fake.calls[0]["query"] == linear._PAGE_QUERY
    assert (fake.variables()["first"], fake.variables()["after"]) == (100, "c-250")
    assert payload["nextCursor"] == "5"


@pytest.mark.asyncio
async def test_a_page_past_the_end_is_empty(monkeypatch, stored_key) -> None:
    done = {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": "c-3"}}}}
    fake = _Linear(KiroCrewLinearIssuePage=done)
    monkeypatch.setattr(linear, "_post", fake)
    status, payload = await _search(cursor="2")
    assert status == 200
    assert (payload["issues"], payload["nextCursor"]) == ([], None)
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_paging_stops_at_the_window(monkeypatch, stored_key) -> None:
    boundary = {"data": {"issues": {"pageInfo": {"hasNextPage": True, "endCursor": "c"}}}}
    fake = _Linear(KiroCrewLinearIssuePage=boundary, KiroCrewLinearIssues=_issues([_node(1)], True))
    monkeypatch.setattr(linear, "_post", fake)
    _status, last = await _search(limit=100, cursor="10")
    assert last["nextCursor"] is None
    assert len(fake.calls) == 4 + 1
    fake.calls.clear()
    _status, past = await _search(limit=100, cursor="11")
    assert (past["issues"], past["nextCursor"]) == ([], None)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_the_last_page_stops_at_the_window(monkeypatch, stored_key) -> None:
    # 30 per page: page 34 starts at 990, so only 10 rows remain inside the window.
    boundary = {"data": {"issues": {"pageInfo": {"hasNextPage": True, "endCursor": "c"}}}}
    fake = _Linear(KiroCrewLinearIssuePage=boundary, KiroCrewLinearIssues=_issues([_node(1)], True))
    monkeypatch.setattr(linear, "_post", fake)
    _status, last = await _search(limit=30, cursor="34")
    assert fake.variables()["first"] == 10
    assert last["nextCursor"] is None


# ── Redaction ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_secrets_in_a_search_come_back_redacted(monkeypatch, stored_key) -> None:
    leaky = _node(
        9,
        title=f"leaked {_SECRET} here",
        description=f"use **{_SECRET[:20]}**{_SECRET[20:]} to log in",
        state={"name": f"Blocked {_SECRET}", "type": "started"},
    )
    monkeypatch.setattr(linear, "_post", _Linear(KiroCrewLinearIssues=_issues([leaky])))
    status, payload = await _search()
    assert status == 200
    assert _SECRET not in json.dumps(payload)
    assert payload["issues"][0]["key"] == "ENG-9"
