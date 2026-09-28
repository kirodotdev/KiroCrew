"""Jira read failures and request shape, pinned at the handler's names.

Jira is the one provider the gateway calls over HTTP itself, so every failure the
panel can show -- an HTTP status, an oversized or malformed body, an unreachable
host, missing credentials -- is raised by this code rather than by a CLI. These
cases pin each message and the request the read sends.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp
import pytest

from kiro_crew.dashboard.handlers import source_providers as sp

_CLOUD = "https://acme.atlassian.net/browse/PROJ-7"
_SERVER = "https://jira.corp.example/jira/browse/OPS-3"


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body
        self.content = self

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def read(self, n: int = -1) -> bytes:
        body, self._body = self._body, b""
        return body

    async def iter_chunked(self, _size: int):
        body, self._body = self._body, b""
        if body:
            yield body


class _Session:
    def __init__(self, response: _Response | BaseException, seen: list[dict[str, Any]]) -> None:
        self._response = response
        self._seen = seen

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def get(self, url: str, **kwargs: Any) -> _Response:
        self._seen.append({"url": url, **kwargs})
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


def _serve(monkeypatch, response: _Response | BaseException) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    headers: list[dict[str, str]] = []

    def session(*_args: Any, **kwargs: Any) -> _Session:
        headers.append(kwargs.get("headers") or {})
        return _Session(response, seen)

    monkeypatch.setattr(sp.aiohttp, "ClientSession", session)
    monkeypatch.setattr(sp, "_get_jira_auth", lambda host: ("me@example.com", "tok"))
    seen.append({"headers": headers})
    return seen


async def _fetch(url: str = _CLOUD) -> dict[str, Any]:
    return await sp._fetch_jira_issue(sp.parse_source_url(url))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "Jira authentication failed."),
        (403, "Jira access denied."),
        (404, "Jira issue PROJ-7 not found on acme.atlassian.net."),
        (500, "Jira returned HTTP 500 for PROJ-7."),
    ],
)
async def test_each_http_status_has_its_own_message(monkeypatch, status, message) -> None:
    _serve(monkeypatch, _Response(status, b""))

    with pytest.raises(sp.SourceProviderError) as raised:
        await _fetch()

    assert str(raised.value).startswith(message)


@pytest.mark.asyncio
async def test_an_oversized_body_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(sp, "_MAX_PAYLOAD_BYTES", 8)
    _serve(monkeypatch, _Response(200, b'{"fields": {"summary": "long enough"}}'))

    with pytest.raises(sp.SourceProviderError, match="exceeds the size limit"):
        await _fetch()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"{not json", "Jira returned an unparseable response for PROJ-7."),
        (b"[1, 2]", "Jira returned an invalid issue payload"),
        (b"[" * 100_000 + b"]" * 100_000, "Jira response for PROJ-7 is too deeply nested."),
    ],
    ids=["unparseable", "not-an-object", "too-deep"],
)
async def test_a_malformed_body_is_refused(monkeypatch, body, message) -> None:
    _serve(monkeypatch, _Response(200, body))

    with pytest.raises(sp.SourceProviderError) as raised:
        await _fetch()

    assert str(raised.value) == message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [aiohttp.ClientConnectionError("refused"), asyncio.TimeoutError()]
)
async def test_an_unreachable_host_names_only_the_error_type(monkeypatch, error) -> None:
    _serve(monkeypatch, error)

    with pytest.raises(sp.SourceProviderError) as raised:
        await _fetch()

    assert (
        str(raised.value) == f"Could not reach Jira at acme.atlassian.net: {type(error).__name__}"
    )


@pytest.mark.asyncio
async def test_missing_credentials_name_the_per_host_secret(monkeypatch) -> None:
    monkeypatch.setattr(sp, "_get_jira_auth", lambda host: None)

    with pytest.raises(ValueError) as raised:
        await _fetch()

    assert str(raised.value).startswith("jira_no_credentials: ")
    assert sp.jira_host_token_name("acme.atlassian.net") in str(raised.value)


@pytest.mark.asyncio
async def test_cloud_reads_v3_with_basic_auth_and_no_redirects(monkeypatch) -> None:
    seen = _serve(monkeypatch, _Response(200, json.dumps({"fields": {}}).encode()))

    await _fetch()

    headers, request = seen[0]["headers"][0], seen[1]
    assert request["url"].startswith("https://acme.atlassian.net/rest/api/3/issue/PROJ-7?fields=")
    assert request["allow_redirects"] is False
    assert headers["Authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_server_reads_v2_under_its_context_path_with_a_bearer_token(monkeypatch) -> None:
    monkeypatch.setattr(sp, "_allowed_jira_hosts", lambda: frozenset({"jira.corp.example"}))
    seen = _serve(
        monkeypatch,
        _Response(200, json.dumps({"fields": {"description": "plain *wiki* text"}}).encode()),
    )

    issue = await _fetch(_SERVER)

    headers, request = seen[0]["headers"][0], seen[1]
    assert request["url"].startswith("https://jira.corp.example/jira/rest/api/2/issue/OPS-3?")
    assert headers["Authorization"] == "Bearer tok"
    assert issue["description"] == "plain *wiki* text"


@pytest.mark.asyncio
async def test_the_payload_projects_every_contract_field(monkeypatch) -> None:
    fields = {
        "summary": "Title",
        "status": {"statusCategory": {"key": "done"}},
        "resolution": {"name": "Fixed"},
        "reporter": {"displayName": "Rae"},
        "assignee": {"displayName": "Ash"},
        "labels": ["infra", ""],
        "priority": {"name": "High"},
        "issuetype": {"name": "Bug"},
        "comment": {
            "total": 3,
            "comments": [
                {"id": "1", "author": {"name": "bob"}, "body": "plain", "created": "c1"},
                {"id": "2", "author": {}, "body": {"type": "doc", "content": []}},
                {"id": "3", "author": {}, "body": 5},
            ],
        },
        "created": "c",
        "updated": "u",
        "resolutiondate": "r",
    }
    _serve(monkeypatch, _Response(200, json.dumps({"fields": fields}).encode()))

    issue = await _fetch()

    assert issue["state"] == "closed"
    assert issue["stateReason"] == "Fixed"
    assert (issue["author"], issue["assignees"]) == ("Rae", ["Ash"])
    assert [label["name"] for label in issue["labels"]] == ["Bug", "Priority: High", "infra"]
    assert [comment["body"] for comment in issue["comments"]] == ["plain", "", ""]
    assert issue["commentCount"] == 3
    assert issue["partialSections"] == []
    assert (issue["createdAt"], issue["updatedAt"], issue["closedAt"]) == ("c", "u", "r")
