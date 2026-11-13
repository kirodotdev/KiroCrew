"""``POST /api/source/issues`` for Jira Cloud and Server/Data Center.

Every request goes through the real route and the real Jira REST client; only the
HTTP session and the credential lookup are faked. The invariants: no submitted
value can end a JQL string or the gateway's ``project =`` clause, both editions
answer in the shared issue shape, and summaries come back redacted.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlsplit

import pytest
from aiohttp.test_utils import TestClient, TestServer
from source_providers_helpers import _clear_shared_fetch_state  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401
from source_providers_helpers import _app

from kiro_crew.dashboard.source_providers import cache, hosts, issue_search, jira

_SECRET = "ghp_" + "b" * 36
_DASHES = "-----"
_CLOUD = "acme.atlassian.net"
_SERVER = "jira.acme.dev"
_TOKEN = "Ch0jU3RyaW5nJlNVTlE9JUlOVCZNVEl6TkRVPRAZGOyG6qr7MyIQ+/_-="


class _Content:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, _n: int):
        if self._body:
            yield self._body


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.content = _Content(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Jira:
    """A fake ``aiohttp.ClientSession`` serving one canned answer per request."""

    def __init__(self, *answers: Any, status: int = 200) -> None:
        self.answers = list(answers)
        self.status = status
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def session(self, *_args: Any, headers: dict[str, str] | None = None, **_kwargs: Any):
        self.headers.append(dict(headers or {}))
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def get(self, url: str, **kwargs: Any) -> _Response:
        assert kwargs.get("allow_redirects") is False
        self.urls.append(url)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        body = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        return _Response(self.status, body)

    def install(self, monkeypatch) -> _Jira:
        monkeypatch.setattr(jira.aiohttp, "ClientSession", self.session)
        return self

    def path(self, index: int = 0) -> str:
        return urlsplit(self.urls[index]).path

    def params(self, index: int = 0) -> dict[str, str]:
        return dict(parse_qsl(urlsplit(self.urls[index]).query))


_AUTH = {_CLOUD: ("me@acme.dev", "cloud-api-key"), _SERVER: ("", "server-pat")}


@pytest.fixture(autouse=True)
def _isolated_search(_floor_monkeypatch):
    """No config read, a known allowlist and credentials, and an empty cache."""
    patch = _floor_monkeypatch
    patch.setattr(hosts, "ensure_gitlab_hosts_loaded", AsyncMock(return_value=frozenset()))
    patch.setattr(hosts, "_allowed_jira_hosts", lambda: frozenset({_SERVER, "jira.acme.dev:8443"}))
    patch.setattr(jira, "_get_jira_auth", lambda host: _AUTH.get(host))
    cache._SEARCH_CACHE.clear()
    issue_search._JIRA_PAGE_TOKENS.clear()
    yield
    cache._SEARCH_CACHE.clear()
    issue_search._JIRA_PAGE_TOKENS.clear()


def _adf(*paragraphs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": nodes} for nodes in paragraphs],
    }


def _text(value: str, *marks: str) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": value}
    if marks:
        node["marks"] = [{"type": mark} for mark in marks]
    return node


def _row(key: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "summary": f"Issue {key}",
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "assignee": {
            "accountId": "5b10ac8d82e05b22cc7d4ef5",
            "displayName": "Alice A",
            "avatarUrls": {"48x48": "https://avatar.example/alice"},
        },
        "reporter": {"accountId": "557058:bob", "displayName": "Bob"},
        "labels": ["backend", "ui"],
        "priority": {"name": "High"},
        "created": "2026-11-01T10:00:00.000+0000",
        "updated": "2026-11-09T10:00:00.000-0530",
        "description": _adf([_text("Steps: "), _text("run crew", "strong")]),
    }
    base.update(fields)
    return {"id": "10001", "key": key, "self": "https://evil.example/x", "fields": base}


async def _post(body: dict[str, Any]) -> tuple[int, Any]:
    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/source/issues", json=body)
        return response.status, await response.json()


def _cloud(**extra: Any) -> dict[str, Any]:
    return {"provider": "jira", "scope": "CREW", "host": _CLOUD, **extra}


def _server(**extra: Any) -> dict[str, Any]:
    return {"provider": "jira", "scope": "CREW", "host": _SERVER, **extra}


def _jql_parts(jql: str) -> tuple[str, list[str]]:
    """Split JQL into its text outside string literals and the decoded literals,
    reading quotes and backslash escapes the way Jira does."""
    outside: list[str] = []
    literals: list[str] = []
    current: list[str] | None = None
    quote = ""
    chars = iter(jql)
    for char in chars:
        if current is not None:
            if char == "\\":
                current.append(next(chars))
            elif char == quote:
                literals.append("".join(current))
                current = None
                outside.append('""')
            else:
                current.append(char)
        elif char in "\"'":
            current, quote = [], char
        else:
            outside.append(char)
    assert current is None, "unterminated string literal"
    return "".join(outside), literals


# ── Query building: Cloud and Server ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_cloud_structured_filters_build_one_scoped_query(monkeypatch) -> None:
    fake = _Jira({"issues": [], "isLast": True}).install(monkeypatch)
    status, payload = await _post(
        _cloud(
            scope="crew",
            state="open",
            assignee="me",
            author="557058:bob",
            labels=["bug", "needs triage"],
            text="crash on save",
            sort="created",
            order="asc",
            limit=10,
        )
    )
    assert status == 200, payload
    assert fake.urls[0].startswith(f"https://{_CLOUD}/rest/api/3/search/jql?")
    params = fake.params()
    assert params["jql"] == (
        'project = "CREW" AND statusCategory != Done AND assignee = currentUser() '
        'AND reporter = "557058:bob" AND labels = "bug" AND labels = "needs triage" '
        'AND text ~ "crash on save" ORDER BY created ASC'
    )
    assert params["maxResults"] == "10"
    assert "startAt" not in params and "nextPageToken" not in params
    assert set(params["fields"].split(",")) >= {"summary", "status", "description", "updated"}
    assert fake.headers[0]["Authorization"].startswith("Basic ")
    assert payload["scope"] == "CREW"


@pytest.mark.asyncio
async def test_server_uses_v2_search_with_start_at_and_a_bearer_token(monkeypatch) -> None:
    fake = _Jira({"issues": [], "total": 0, "startAt": 40}).install(monkeypatch)
    status, _ = await _post(_server(state="closed", cursor="3", limit=20))
    assert status == 200
    assert fake.urls[0].startswith(f"https://{_SERVER}/rest/api/2/search?")
    params = fake.params()
    assert params["jql"] == 'project = "CREW" AND statusCategory = Done ORDER BY updated DESC'
    assert (params["startAt"], params["maxResults"]) == ("40", "20")
    assert fake.headers[0]["Authorization"] == "Bearer server-pat"


@pytest.mark.asyncio
async def test_state_all_adds_no_status_clause(monkeypatch) -> None:
    fake = _Jira({"issues": [], "isLast": True}).install(monkeypatch)
    await _post(_cloud(state="all"))
    assert fake.params()["jql"] == 'project = "CREW" ORDER BY updated DESC'


_INJECTIONS = [
    'x" OR project = OTHER OR assignee = "y',
    "a) OR (project = OTHER",
    'x\\" OR project = OTHER OR assignee = \\"',
    "x\\",
    "it's ' OR project = OTHER",
    'me" ORDER BY created',
    "x OR 1=1 AND project != CREW",
]


@pytest.mark.parametrize("value", _INJECTIONS)
@pytest.mark.parametrize(("field", "jql_field"), [("assignee", "assignee"), ("author", "reporter")])
@pytest.mark.asyncio
async def test_a_person_value_stays_one_string_literal(
    monkeypatch, value, field, jql_field
) -> None:
    fake = _Jira({"issues": [], "isLast": True}).install(monkeypatch)
    status, _ = await _post(_cloud(state="all", **{field: value}))
    assert status == 200
    skeleton, literals = _jql_parts(fake.params()["jql"])
    assert skeleton == f'project = "" AND {jql_field} = "" ORDER BY updated DESC'
    assert literals == ["CREW", value]


@pytest.mark.parametrize("value", _INJECTIONS)
@pytest.mark.asyncio
async def test_a_label_value_stays_one_string_literal(monkeypatch, value) -> None:
    if '"' in value:
        # Refused for every provider at the boundary (a GitHub label qualifier).
        status, payload = await _post(_cloud(labels=[value]))
        assert (status, payload["code"]) == (400, "invalid_request")
        return
    fake = _Jira({"issues": [], "isLast": True}).install(monkeypatch)
    status, _ = await _post(_cloud(state="all", labels=[value]))
    assert status == 200
    skeleton, literals = _jql_parts(fake.params()["jql"])
    assert skeleton == 'project = "" AND labels = "" ORDER BY updated DESC'
    assert literals == ["CREW", value]


@pytest.mark.parametrize("value", [*_INJECTIONS, '"quoted" (x) {y} [z] a:b c/d e*f? g~ !h ^i'])
@pytest.mark.asyncio
async def test_free_text_becomes_plain_words_in_one_literal(monkeypatch, value) -> None:
    fake = _Jira({"issues": [], "isLast": True}).install(monkeypatch)
    status, _ = await _post(_cloud(state="all", text=value))
    assert status == 200
    skeleton, literals = _jql_parts(fake.params()["jql"])
    assert skeleton == 'project = "" AND text ~ "" ORDER BY updated DESC'
    words = literals[1]
    assert words and not set(words) & set(issue_search._JIRA_TEXT_OPERATORS)


@pytest.mark.asyncio
async def test_text_of_only_operators_adds_no_text_clause(monkeypatch) -> None:
    fake = _Jira({"issues": [], "isLast": True}).install(monkeypatch)
    await _post(_cloud(state="all", text='"()" - *'))
    assert fake.params()["jql"] == 'project = "CREW" ORDER BY updated DESC'


@pytest.mark.parametrize(
    "raw",
    [
        "labels = bug AND (priority = High OR priority = Highest)",
        'summary ~ "a) OR (project = OTHER"',
        "summary ~ 'it\\'s ) here'",
        'summary ~ "say \\"hi\\" (twice)"',
        'issuekey in ("CREW-1", "CREW-2")',
        "fixVersion = '1.0' and status was \"Open\"",
    ],
)
@pytest.mark.asyncio
async def test_raw_jql_is_anded_inside_the_project_group(monkeypatch, raw) -> None:
    fake = _Jira({"issues": [], "isLast": True}).install(monkeypatch)
    # Structured filters are replaced, not combined, when raw is set.
    status, payload = await _post(_cloud(raw=raw, assignee="me", text="ignored"))
    assert status == 200, payload
    assert fake.params()["jql"] == f'project = "CREW" AND ({raw}) ORDER BY updated DESC'


@pytest.mark.parametrize(
    "raw",
    [
        "a = 1) OR (project = OTHER",
        ") OR project = OTHER OR (",
        "labels = bug) OR project in (OTHER",
        "a))((b",
        "((a = 1)",
        "labels = bug ORDER BY created DESC",
        "labels = bug order   by key",
        "x = 1) ORDER BY created",
        'summary ~ "unterminated',
        "summary ~ 'unterminated",
        'summary ~ "ends in an escape\\"',
        "summary ~ a\\) OR project = OTHER",
    ],
)
@pytest.mark.asyncio
async def test_raw_jql_that_could_leave_the_project_is_refused(monkeypatch, raw) -> None:
    fake = _Jira({"issues": [], "isLast": True}).install(monkeypatch)
    status, payload = await _post(_cloud(raw=raw))
    assert (status, payload["code"]) == (400, "invalid_request"), payload
    assert fake.urls == []
    assert raw not in payload["error"]


@pytest.mark.parametrize(
    "body",
    [
        _cloud(scope="CREW-1"),
        _cloud(scope="1CREW"),
        _cloud(scope="C" * 11),
        _cloud(scope="CR EW"),
        _cloud(scope='CREW" OR project = "X'),
        {"provider": "jira", "scope": "CREW"},
        _cloud(host="evil.example"),
        _cloud(host="acme.atlassian.net.evil.example"),
        _cloud(host="a.b.atlassian.net"),
        _cloud(host="-acme.atlassian.net"),
        _cloud(host=".atlassian.net"),
        _cloud(host="acme.atlassian.net:8443"),
        _cloud(host="acme.atlassian.net/x"),
        _cloud(host="user@acme.atlassian.net"),
        _server(host="jira.acme.dev:9443"),
        _server(cursor=_TOKEN),
        _server(cursor="0"),
        _cloud(cursor=_TOKEN),
        _cloud(cursor="a%2Fb"),
        _cloud(cursor="1234567"),
        _cloud(cursor=7),
    ],
)
@pytest.mark.asyncio
async def test_invalid_jira_fields_are_refused_before_any_request(monkeypatch, body) -> None:
    fake = _Jira({"issues": []}).install(monkeypatch)
    status, payload = await _post(body)
    assert (status, payload["code"]) == (400, "invalid_request"), payload
    assert fake.urls == []


@pytest.mark.asyncio
async def test_an_allowlisted_server_port_and_default_https_port_are_kept(monkeypatch) -> None:
    fake = _Jira({"issues": [], "total": 0}).install(monkeypatch)
    monkeypatch.setattr(jira, "_get_jira_auth", lambda host: ("me@acme.dev", "key"))
    assert (await _post(_server(host="jira.acme.dev:8443")))[0] == 200
    assert (await _post(_cloud(host="ACME.atlassian.net:443")))[0] == 200
    assert fake.urls[0].startswith("https://jira.acme.dev:8443/rest/api/2/search?")
    assert fake.urls[1].startswith(f"https://{_CLOUD}/rest/api/3/search/jql?")


# ── Credentials and provider errors ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_host_without_credentials_is_refused_without_a_request(monkeypatch) -> None:
    fake = _Jira({"issues": []}).install(monkeypatch)
    status, payload = await _post(_cloud(host="other.atlassian.net"))
    assert (status, payload["code"]) == (400, "invalid_request")
    assert "No Jira credentials" in payload["error"] and "other." not in payload["error"]
    assert fake.urls == []


@pytest.mark.asyncio
async def test_cloud_credentials_without_an_email_are_refused(monkeypatch) -> None:
    fake = _Jira({"issues": []}).install(monkeypatch)
    monkeypatch.setattr(jira, "_get_jira_auth", lambda host: ("", "cloud-api-key"))
    status, payload = await _post(_cloud())
    assert (status, payload["code"]) == (400, "invalid_request")
    assert "email" in payload["error"]
    assert fake.urls == []


@pytest.mark.parametrize(
    ("http_status", "message"),
    [
        (401, "Jira authentication failed"),
        (403, "permission to read this project"),
        (404, "Jira issue search is not available"),
        (302, "Jira returned HTTP 302"),
        (500, "Jira returned HTTP 500"),
    ],
)
@pytest.mark.asyncio
async def test_http_failures_map_to_provider_errors(
    monkeypatch, _mock_source_sel, http_status, message
) -> None:
    _Jira({"errorMessages": [_SECRET]}, status=http_status).install(monkeypatch)
    status, payload = await _post(_server())
    assert (status, payload["code"]) == (503, "provider_error")
    assert message in payload["error"] and _SECRET not in str(payload)
    assert _mock_source_sel.log_api_access.call_args.kwargs["error"] == "provider_error"


@pytest.mark.parametrize(
    "answer",
    [b"not json", b"[1, 2]", {"issues": "none"}, {"total": 3}],
)
@pytest.mark.asyncio
async def test_an_invalid_payload_is_a_provider_error(monkeypatch, answer) -> None:
    _Jira(answer).install(monkeypatch)
    status, payload = await _post(_cloud())
    assert (status, payload["code"]) == (503, "provider_error")


# ── Response shape, paging and empty results ─────────────────────────────────


@pytest.mark.asyncio
async def test_cloud_summary_shape(monkeypatch) -> None:
    _Jira({"issues": [_row("CREW-7")], "isLast": True}).install(monkeypatch)
    status, payload = await _post(_cloud())
    assert status == 200
    assert payload == {
        "provider": "jira",
        "scope": "CREW",
        "issues": [
            {
                "provider": "jira",
                "url": f"https://{_CLOUD}/browse/CREW-7",
                "key": "CREW-7",
                "number": 7,
                "title": "Issue CREW-7",
                "state": "open",
                "statusLabel": "In Progress",
                "labels": [
                    {"name": "backend", "color": "", "description": ""},
                    {"name": "ui", "color": "", "description": ""},
                ],
                "assignees": [
                    {
                        "login": "5b10ac8d82e05b22cc7d4ef5",
                        "name": "Alice A",
                        "avatarUrl": "https://avatar.example/alice",
                    }
                ],
                "author": {"login": "557058:bob", "name": "Bob", "avatarUrl": ""},
                "priority": "High",
                "commentCount": 0,
                "createdAt": "2026-11-01T10:00:00.000+00:00",
                "updatedAt": "2026-11-09T10:00:00.000-05:30",
                "excerpt": "Steps: run crew",
                "project": "CREW",
                "linkedChanges": [],
            }
        ],
        "nextCursor": None,
        "total": None,
    }


@pytest.mark.asyncio
async def test_a_cloud_excerpt_keeps_literal_text_markdown_would_strip(monkeypatch) -> None:
    """ADF text is plain already: markdown-looking characters in it are content."""
    code = {"type": "codeBlock", "content": [_text("<!-- Press Save twice to reproduce -->")]}
    description = {
        "type": "doc",
        "version": 1,
        "content": [code, *_adf([_text("snake__case")])["content"]],
    }
    _Jira({"issues": [_row("CREW-3", description=description)], "isLast": True}).install(
        monkeypatch
    )
    payload = (await _post(_cloud()))[1]
    assert payload["issues"][0]["excerpt"] == "<!-- Press Save twice to reproduce --> snake__case"


@pytest.mark.asyncio
async def test_server_summary_shape(monkeypatch) -> None:
    row = _row(
        "CREW-12",
        status={"name": "Closed", "statusCategory": {"key": "done"}},
        assignee={"name": "carol", "displayName": "Carol C", "avatarUrls": {}},
        reporter={"name": "dave"},
        priority=None,
        labels=[],
        description="Plain *wiki* body",
        created="2026-11-01T10:00:00Z",
    )
    _Jira({"issues": [row], "total": 1, "startAt": 0}).install(monkeypatch)
    status, payload = await _post(_server(state="all"))
    assert status == 200
    issue = payload["issues"][0]
    assert issue["url"] == f"https://{_SERVER}/browse/CREW-12"
    assert (issue["state"], issue["statusLabel"], issue["priority"]) == ("closed", "Closed", None)
    assert issue["assignees"] == [{"login": "carol", "name": "Carol C", "avatarUrl": ""}]
    assert issue["author"] == {"login": "dave", "name": "dave", "avatarUrl": ""}
    assert issue["createdAt"] == "2026-11-01T10:00:00Z"
    assert issue["excerpt"] == "Plain *wiki* body"
    assert payload["total"] == 1 and payload["nextCursor"] is None


@pytest.mark.asyncio
async def test_rows_outside_the_project_are_dropped(monkeypatch) -> None:
    rows = [_row(key) for key in ("OTHER-1", "CREWX-2", "CREW-0", "CREW-x", "CREW-", "CREW-3")]
    rows.append({"key": "CREW-4", "fields": "not a dict"})
    _Jira({"issues": rows, "isLast": True}).install(monkeypatch)
    status, payload = await _post(_cloud())
    assert status == 200
    assert [issue["key"] for issue in payload["issues"]] == ["CREW-3", "CREW-4"]
    assert payload["issues"][1]["title"] == "" and payload["issues"][1]["state"] == "open"


@pytest.mark.asyncio
async def test_empty_results_on_both_editions(monkeypatch) -> None:
    _Jira({"issues": [], "isLast": True}).install(monkeypatch)
    assert (await _post(_cloud()))[1] == {
        "provider": "jira",
        "scope": "CREW",
        "issues": [],
        "nextCursor": None,
        "total": None,
    }
    _Jira({"issues": [], "total": 0, "startAt": 0}).install(monkeypatch)
    payload = (await _post(_server()))[1]
    assert (payload["issues"], payload["nextCursor"], payload["total"]) == ([], None, 0)


@pytest.mark.asyncio
async def test_cloud_pages_by_number_with_the_token_held_by_the_gateway(monkeypatch) -> None:
    fake = _Jira(
        {"issues": [_row("CREW-2")], "nextPageToken": _TOKEN, "isLast": False},
        {"issues": [_row("CREW-1")], "isLast": True},
    ).install(monkeypatch)
    first = (await _post(_cloud(limit=1)))[1]
    assert first["nextCursor"] == "2" and first["total"] is None
    second = (await _post(_cloud(limit=1, cursor=first["nextCursor"])))[1]
    assert "nextPageToken" not in fake.params(0)
    assert fake.params(1)["nextPageToken"] == _TOKEN and "startAt" not in fake.params(1)
    assert [issue["key"] for issue in second["issues"]] == ["CREW-1"]
    assert second["nextCursor"] is None
    # The token stays in the gateway: the payload redaction would mangle it, and
    # the browser pages by number.
    assert _TOKEN not in json.dumps([first, second])


@pytest.mark.asyncio
async def test_a_cloud_page_without_a_held_token_is_reached_by_walking(monkeypatch) -> None:
    fake = _Jira(
        {"issues": [_row("CREW-9")], "nextPageToken": "tokA", "isLast": False},
        {"issues": [_row("CREW-8")], "nextPageToken": "tokB", "isLast": False},
        {"issues": [_row("CREW-7")], "isLast": True},
    ).install(monkeypatch)
    payload = (await _post(_cloud(limit=1, cursor="3")))[1]
    assert [issue["key"] for issue in payload["issues"]] == ["CREW-7"]
    assert [fake.params(n).get("nextPageToken") for n in range(3)] == [None, "tokA", "tokB"]
    # Each token it walked through is held, so page 2 is now one request.
    await _post(_cloud(limit=1, cursor="2"))
    assert fake.params(3)["nextPageToken"] == "tokA"


@pytest.mark.asyncio
async def test_a_cloud_page_past_the_last_is_empty(monkeypatch) -> None:
    fake = _Jira({"issues": [_row("CREW-1")], "isLast": True}).install(monkeypatch)
    payload = (await _post(_cloud(cursor="3")))[1]
    assert (payload["issues"], payload["nextCursor"]) == ([], None)
    assert len(fake.urls) == 1


@pytest.mark.asyncio
async def test_a_cloud_page_too_far_to_walk_is_refused(monkeypatch) -> None:
    fake = _Jira({"issues": [], "nextPageToken": "t", "isLast": False}).install(monkeypatch)
    status, payload = await _post(_cloud(cursor="25"))
    assert (status, payload["code"]) == (400, "invalid_request")
    assert fake.urls == []


@pytest.mark.parametrize(
    "answer",
    [
        {"issues": [], "nextPageToken": _TOKEN, "isLast": True},
        {"issues": [], "nextPageToken": "has\ttab", "isLast": False},
        {"issues": [], "nextPageToken": "x" * 4097, "isLast": False},
        {"issues": [], "nextPageToken": 7, "isLast": False},
        {"issues": [], "isLast": False},
    ],
)
@pytest.mark.asyncio
async def test_cloud_offers_no_next_page_it_cannot_open(monkeypatch, answer) -> None:
    _Jira(answer).install(monkeypatch)
    assert (await _post(_cloud()))[1]["nextCursor"] is None
    assert issue_search._JIRA_PAGE_TOKENS == {}


@pytest.mark.parametrize(
    ("cursor", "rows", "total", "expected"),
    [
        (None, 20, 45, "2"),
        ("2", 20, 45, "3"),
        ("3", 5, 45, None),
        ("3", 0, None, None),
        (None, 20, 20, None),
    ],
)
@pytest.mark.asyncio
async def test_server_pages_by_number_against_the_total(
    monkeypatch, cursor, rows, total, expected
) -> None:
    issues = [_row(f"CREW-{n + 1}") for n in range(rows)]
    _Jira({"issues": issues, "total": total}).install(monkeypatch)
    status, payload = await _post(_server(limit=20, cursor=cursor))
    assert status == 200
    assert payload["nextCursor"] == expected


@pytest.mark.asyncio
async def test_a_server_that_caps_page_size_still_fills_each_page(monkeypatch) -> None:
    """Jira caps maxResults below the limit: no issue between two pages is skipped."""
    keys = [f"CREW-{n}" for n in range(1, 121)]

    def page(start: int) -> dict[str, Any]:
        rows = [_row(key) for key in keys[start : start + 50]]
        return {"issues": rows, "total": len(keys), "startAt": start, "maxResults": 50}

    fake = _Jira(page(0), page(50), page(100)).install(monkeypatch)
    first = (await _post(_server(limit=100)))[1]
    second = (await _post(_server(limit=100, cursor=first["nextCursor"])))[1]
    assert [fake.params(n)["startAt"] for n in range(3)] == ["0", "50", "100"]
    assert [issue["key"] for issue in first["issues"] + second["issues"]] == keys
    assert (first["nextCursor"], second["nextCursor"]) == ("2", None)


@pytest.mark.asyncio
async def test_an_atlassian_subdomain_on_the_allowlist_pages_as_cloud(monkeypatch) -> None:
    """The edition that picks the endpoint also picks the paging."""
    host = "eu.acme.atlassian.net"
    monkeypatch.setattr(hosts, "_allowed_jira_hosts", lambda: frozenset({host}))
    monkeypatch.setattr(jira, "_get_jira_auth", lambda _host: ("me@acme.dev", "key"))
    fake = _Jira(
        {"issues": [_row("CREW-2")], "nextPageToken": _TOKEN, "isLast": False},
    ).install(monkeypatch)
    payload = (await _post(_cloud(host=host, limit=1)))[1]
    assert fake.path() == "/rest/api/3/search/jql" and "startAt" not in fake.params()
    assert payload["nextCursor"] == "2"


@pytest.mark.asyncio
async def test_a_cloud_page_twenty_past_a_held_token_is_walked(monkeypatch) -> None:
    query = issue_search.parse_issue_query(_cloud(limit=1, cursor="22"))[1]
    issue_search._remember_jira_page(query, 2, "held")
    fake = _Jira({"issues": [_row("CREW-1")], "nextPageToken": "t", "isLast": False}).install(
        monkeypatch
    )
    status, _payload = await _post(_cloud(limit=1, cursor="22"))
    assert status == 200 and fake.params(0)["nextPageToken"] == "held"
    assert len(fake.urls) == 21


@pytest.mark.asyncio
async def test_results_are_cached_per_page_until_refresh(monkeypatch) -> None:
    fake = _Jira({"issues": [], "nextPageToken": _TOKEN, "isLast": False}).install(monkeypatch)
    await _post(_cloud())
    await _post(_cloud())
    await _post(_cloud(cursor="2"))
    await _post(_cloud(refresh=True))
    assert len(fake.urls) == 3


# ── Redaction through the shared path ────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_secret_in_a_summary_status_or_description_is_redacted(monkeypatch) -> None:
    cloud_row = _row(
        "CREW-1",
        summary=f"leaked {_SECRET} here",
        status={"name": f"Waiting {_SECRET}", "statusCategory": {"key": "new"}},
        description=_adf([_text(f"use {_SECRET} to log in")]),
    )
    _Jira({"issues": [cloud_row], "isLast": True}).install(monkeypatch)
    status, payload = await _post(_cloud())
    assert status == 200
    assert _SECRET not in str(payload)
    issue = payload["issues"][0]
    assert issue["title"].startswith("leaked ") and issue["excerpt"].startswith("use ")

    server_row = _row("CREW-2", description=f"token: {_SECRET}")
    _Jira({"issues": [server_row], "total": 1}).install(monkeypatch)
    status, payload = await _post(_server())
    assert status == 200 and _SECRET not in str(payload)


@pytest.mark.asyncio
async def test_a_token_split_across_adf_text_nodes_is_redacted(monkeypatch) -> None:
    """Neither node holds the token; rendering and stripping join it."""
    head, tail = _SECRET[:20], _SECRET[20:]
    row = _row("CREW-1", description=_adf([_text("see "), _text(head), _text(tail, "strong")]))
    _Jira({"issues": [row], "isLast": True}).install(monkeypatch)
    status, payload = await _post(_cloud())
    assert status == 200
    assert "ghp_" not in str(payload)
    assert payload["issues"][0]["excerpt"].startswith("see ")


@pytest.mark.asyncio
async def test_a_pem_key_across_adf_paragraphs_is_redacted(monkeypatch) -> None:
    lines = [f"{_DASHES}BEGIN RSA PRIVATE KEY{_DASHES}"]
    lines += [f"MIIEow{n}AAKCAQEAxQQ" + "Q" * 50 for n in range(6)]
    row = _row("CREW-1", description=_adf([_text("Our deploy key:")], *[[_text(x)] for x in lines]))
    _Jira({"issues": [row], "isLast": True}).install(monkeypatch)
    status, payload = await _post(_cloud())
    assert status == 200
    assert "MIIEow" not in str(payload)
    assert payload["issues"][0]["excerpt"].startswith("Our deploy key:")


def test_adf_plain_lines_breaks_blocks_and_items_but_joins_inline_runs() -> None:
    from kiro_crew.dashboard.source_providers import adf

    doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [_text("see "), _text("bold", "strong")]},
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [{"type": "paragraph", "content": [_text("a")]}],
                    },
                    {
                        "type": "listItem",
                        "content": [{"type": "paragraph", "content": [_text("b")]}],
                    },
                ],
            },
            {"type": "paragraph", "content": [_text("x"), {"type": "hardBreak"}, _text("y")]},
        ],
    }
    assert adf._adf_plain_lines(doc) == "see bold\na\nb\nx\ny"
    assert adf._adf_plain_lines("not a node") == ""
