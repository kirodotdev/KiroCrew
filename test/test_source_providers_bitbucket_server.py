"""Bitbucket Server pull requests: ``dashboard.bitbucket_server_hosts`` end to end.

A host listed in config makes its ``/projects/KEY/repos/SLUG/pull-requests/N``
links parse, and the source API then reads the pull request over the instance's
REST API -- here a stubbed one -- with the vault token sent only to that host and
no redirect followed. An empty list leaves every existing provider as it is, and
every write is refused as unsupported.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config import loader as config_loader
from kiro_crew.config import sections as config_sections
from kiro_crew.dashboard.handlers import source_providers as source
from kiro_crew.dashboard.source_providers import (
    bitbucket_server,
    chip_refresh,
    chip_status,
    hosts,
    links,
    plugins,
)
from kiro_crew.dashboard.source_providers.contract import SourceProviderError

HOST = "bitbucket.acme.example:7990"
PR_URL = f"https://{HOST}/bitbucket/projects/PROJ/repos/widgets/pull-requests/42"
REST = f"https://{HOST}/bitbucket/rest"
PR_API = f"{REST}/api/1.0/projects/PROJ/repos/widgets/pull-requests/42"
HEAD = "a" * 40
TOKEN_VALUE = "fixture-access-token"


@pytest.fixture(autouse=True)
def _cold_allowlists(_floor_monkeypatch):
    for name in (
        "_github_hosts_snapshot",
        "_gitlab_hosts_snapshot",
        "_jira_hosts_snapshot",
        "_bitbucket_server_hosts_snapshot",
    ):
        _floor_monkeypatch.setattr(hosts, name, frozenset())


@pytest.fixture(autouse=True)
def _isolated_caches(_floor_monkeypatch):
    _floor_monkeypatch.setattr(source, "_sel", lambda: MagicMock())
    for name in (
        "_CACHE",
        "_FULL_FETCH_INFLIGHT",
        "_FULL_FETCH_TASKS",
        "_FULL_FETCH_GENERATIONS",
        "_CHECKS_FETCH_INFLIGHT",
    ):
        _floor_monkeypatch.setattr(source, name, {})


@pytest.fixture
def listed(monkeypatch):
    monkeypatch.setattr(hosts, "_bitbucket_server_hosts_snapshot", frozenset({HOST}))


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """A real config.json naming the instance, loaded by the real refresh."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"dashboard": {"bitbucket_server_hosts": [HOST]}}), encoding="utf-8")
    monkeypatch.setattr(hosts, "_gitlab_hosts_loaded_at", 0.0)
    with patch("kiro_crew.config.loader.config_path", return_value=path):
        yield path


# --- a stubbed Bitbucket Server ----------------------------------------------


def _ms(day: int) -> int:
    return (1_767_225_600 + day * 86_400) * 1000


PR_DOC = {
    "id": 42,
    "title": "Add the widget cache",
    "description": "Caches widgets per tenant.",
    "state": "OPEN",
    "draft": False,
    "updatedDate": _ms(2),
    "fromRef": {"displayId": "feature/cache", "latestCommit": HEAD},
    "toRef": {"displayId": "main"},
    "author": {"user": {"name": "dev1", "displayName": "Dev One"}},
}
COMMITS_DOC = {
    "isLastPage": True,
    "values": [
        {
            "id": HEAD,
            "message": "Add the widget cache\n\nKeyed by tenant.",
            "author": {"name": "dev1", "displayName": "Dev One"},
            "authorTimestamp": _ms(1),
        }
    ],
}
CHANGES_DOC = {
    "isLastPage": True,
    "values": [
        {"path": {"toString": "src/cache.py"}, "type": "ADD"},
        {"path": {"toString": "src/app.py"}, "type": "MODIFY"},
    ],
}
DIFF_DOC = {
    "truncated": False,
    "diffs": [
        {
            "source": None,
            "destination": {"toString": "src/cache.py"},
            "hunks": [
                {
                    "sourceLine": 0,
                    "sourceSpan": 0,
                    "destinationLine": 1,
                    "destinationSpan": 2,
                    "segments": [
                        {"type": "ADDED", "lines": [{"line": "CACHE = {}"}, {"line": ""}]}
                    ],
                }
            ],
        },
        {
            "source": {"toString": "src/app.py"},
            "destination": {"toString": "src/app.py"},
            "hunks": [
                {
                    "sourceLine": 1,
                    "sourceSpan": 2,
                    "destinationLine": 1,
                    "destinationSpan": 2,
                    "segments": [
                        {"type": "CONTEXT", "lines": [{"line": "import os"}]},
                        {"type": "REMOVED", "lines": [{"line": "cache = None"}]},
                        {"type": "ADDED", "lines": [{"line": "from cache import CACHE"}]},
                    ],
                }
            ],
        },
    ],
}
ACTIVITIES_DOC = {
    "isLastPage": True,
    # Bitbucket lists activity newest first.
    "values": [
        {
            "id": 903,
            "action": "APPROVED",
            "createdDate": _ms(3),
            "user": {"name": "rev1", "displayName": "Reviewer One"},
        },
        {
            "id": 902,
            "action": "COMMENTED",
            "createdDate": _ms(2),
            "comment": {
                "id": 11,
                "text": "Why a module-level dict?",
                "author": {"displayName": "Reviewer One"},
                "createdDate": _ms(2),
                "comments": [
                    {
                        "id": 12,
                        "text": "It is per process by design.",
                        "author": {"displayName": "Dev One"},
                        "createdDate": _ms(3),
                    }
                ],
            },
            "commentAnchor": {"path": "src/cache.py", "line": 1},
        },
        {"id": 901, "action": "OPENED", "createdDate": _ms(1)},
    ],
}
BUILDS_DOC = {
    "isLastPage": True,
    "values": [
        {
            "state": "SUCCESSFUL",
            "key": "unit",
            "name": "Unit tests",
            "url": "https://ci.acme.example/build/1",
            "dateAdded": _ms(2),
        },
        {
            "state": "FAILED",
            "key": "lint",
            "name": "Lint",
            "url": "javascript:alert(1)",
            "dateAdded": _ms(2),
        },
    ],
}


def _routes(pr: dict[str, Any] | None = None) -> dict[str, tuple[int, Any]]:
    return {
        PR_API: (200, pr or PR_DOC),
        f"{PR_API}/commits?limit=100": (200, COMMITS_DOC),
        f"{PR_API}/changes?limit=500": (200, CHANGES_DOC),
        f"{PR_API}/diff?contextLines=3&withComments=false": (200, DIFF_DOC),
        f"{PR_API}/activities?limit=100": (200, ACTIVITIES_DOC),
        f"{REST}/build-status/1.0/commits/{HEAD}?limit=100": (200, BUILDS_DOC),
    }


class _Content:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size: int):
        for start in range(0, len(self._body), size):
            yield self._body[start : start + size]


class _Response:
    def __init__(self, status: int, document: Any) -> None:
        self.status = status
        self.content = _Content(json.dumps(document).encode("utf-8"))

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Instance:
    """Records every request a fetch makes and answers from canned documents."""

    def __init__(self, routes: dict[str, tuple[int, Any]]) -> None:
        self.routes = routes
        self.requests: list[dict[str, Any]] = []
        self.on_get: Any = None

    def session(self, *args: Any, headers: dict[str, str] | None = None, **kwargs: Any):
        instance = self

        class _Session:
            async def __aenter__(self) -> _Session:
                return self

            async def __aexit__(self, *exc: object) -> bool:
                return False

            def get(self, url: str, *, allow_redirects: bool = True) -> _Response:
                instance.requests.append(
                    {"url": url, "headers": dict(headers or {}), "redirects": allow_redirects}
                )
                if instance.on_get is not None:
                    instance.on_get(url)
                status, document = instance.routes.get(url, (404, {"errors": []}))
                return _Response(status, document)

        return _Session()


@pytest.fixture
def instance(monkeypatch):
    stub = _Instance(_routes())
    monkeypatch.setattr(bitbucket_server, "ClientSession", stub.session)
    return stub


@pytest.fixture
def vault(monkeypatch):
    secrets: dict[str, str] = {}
    monkeypatch.setattr(bitbucket_server, "_vault_secret", lambda name: secrets.get(name, ""))
    return secrets


def _app() -> web.Application:
    @web.middleware
    async def owner(request, handler):
        request["user"] = "U_OWNER"
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[owner])
    state = MagicMock()
    state.owner_id = "U_OWNER"
    app["state"] = state
    app.router.add_post("/api/source/pull-request", source.api_pull_request_source)
    app.router.add_post("/api/source/pull-request/checks", source.api_pull_request_checks)
    app.router.add_post("/api/source/pull-request/comment", source.api_pull_request_comment)
    app.router.add_post("/api/source/pull-request/ready", source.api_pull_request_ready)
    return app


# --- config ------------------------------------------------------------------


def test_bitbucket_server_hosts_defaults_to_empty() -> None:
    assert config_loader._build_dashboard_config(set(), {}).bitbucket_server_hosts == []


def test_entries_are_normalized_like_gitlab_hosts_and_keep_their_port() -> None:
    raw = [
        " Bitbucket.Acme.Example:7990 ",
        "bitbucket.acme.example:7990",  # duplicate after normalization
        "git.acme.example:443",  # default port dropped
        "https://scheme.acme.example",
        "*.acme.example",
        "path.acme.example/bitbucket",
        "bitbucket.org",  # Cloud is never a Server host
        "www.bitbucket.org",
        "api.bitbucket.org",
        42,
    ]
    assert config_sections._coerce_bitbucket_server_hosts(raw) == [
        "bitbucket.acme.example:7990",
        "git.acme.example",
    ]
    assert config_sections._coerce_bitbucket_server_hosts("bitbucket.acme.example") == []


def test_a_host_another_provider_list_claims_is_refused(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        dashboard = config_loader._build_dashboard_config(
            set(),
            {
                "bitbucket_server_hosts": ["git.acme.example", "gh.acme.example", HOST],
                "gitlab_hosts": ["git.acme.example"],
                "github_hosts": ["gh.acme.example"],
            },
        )
    assert dashboard.bitbucket_server_hosts == [HOST]
    # The claiming lists are untouched by the refusal.
    assert dashboard.gitlab_hosts == ["git.acme.example"]
    assert dashboard.github_hosts == ["gh.acme.example"]
    refused = [record.args for record in caplog.records if record.levelno == logging.WARNING]
    assert ("git.acme.example",) in refused and ("gh.acme.example",) in refused


# --- URL validation ----------------------------------------------------------


def test_an_unlisted_host_is_refused() -> None:
    with pytest.raises(ValueError):
        links.parse_source_url(PR_URL)


@pytest.mark.usefixtures("listed")
def test_a_listed_host_parses_and_the_tab_suffix_is_dropped() -> None:
    ref = links.parse_source_url(f"{PR_URL}/overview")
    assert (ref.provider, ref.host, ref.owner, ref.repo, ref.number, ref.kind) == (
        "bitbucket_server",
        HOST,
        "PROJ",
        "widgets",
        42,
        "change",
    )
    assert ref.url == PR_URL
    assert ref.project == "/bitbucket"
    assert links.parse_source_url(ref.url) == ref


@pytest.mark.usefixtures("listed")
@pytest.mark.parametrize(
    "url",
    [
        "https://bitbucket.acme.example/bitbucket/projects/P/repos/r/pull-requests/1",  # port
        f"https://{HOST}.evil.example/projects/P/repos/r/pull-requests/1",
        f"https://{HOST}/users/dev1/repos/r/pull-requests/1",
        f"https://{HOST}/projects/P/repos/r/pull-requests/",
        f"https://{HOST}/projects/P/repos/r/pull-requests/0",
        f"https://{HOST}/projects/P/repos/r/pull-requests/\u0661",
        f"https://{HOST}/../projects/P/repos/r/pull-requests/1",
        f"https://{HOST}/projects/P/repos/../pull-requests/1",
        f"https://{HOST}/projects/P%2F/repos/r/pull-requests/1",
        f"https://{HOST}/projects/P/repos/r/pull-requests/1/settings",
        "https://bitbucket.org/ws/repo/pull-requests/1",
        f"http://{HOST}/projects/P/repos/r/pull-requests/1",
        f"https://user@{HOST}/projects/P/repos/r/pull-requests/1",
    ],
)
def test_lookalike_and_malformed_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        links.parse_source_url(url)


def test_the_pull_requests_marker_is_offered_only_while_a_host_is_listed(monkeypatch) -> None:
    assert "/pull-requests/" not in source.source_link_path_markers()
    monkeypatch.setattr(hosts, "_bitbucket_server_hosts_snapshot", frozenset({HOST}))
    assert "/pull-requests/" in source.source_link_path_markers()


def test_an_edition_cannot_register_the_core_id() -> None:
    class Shadow:
        id = "bitbucket_server"

    with pytest.raises(ValueError, match="built-in"):
        source.register_source_provider(Shadow())  # type: ignore[arg-type]


@pytest.mark.usefixtures("listed")
def test_a_ref_never_falls_through_to_a_builtin_branch(monkeypatch) -> None:
    ref = links.parse_source_url(PR_URL)
    assert plugins._plugin_for_change(ref) is bitbucket_server.PROVIDER
    monkeypatch.setattr(plugins, "_CORE_SEAM_PROVIDERS", {})
    with pytest.raises(SourceProviderError, match="not loaded"):
        plugins._plugin_for_change(ref)


# --- the source API against the stubbed instance ----------------------------


@pytest.mark.asyncio
async def test_the_panel_reads_a_listed_instance_with_its_own_token(
    config_file, instance, vault
) -> None:
    vault[bitbucket_server.host_token_name(HOST)] = TOKEN_VALUE
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": f"{PR_URL}/overview"})
        assert resp.status == 200, await resp.text()
        payload = await resp.json()

    assert payload["provider"] == "bitbucket_server"
    assert payload["url"] == PR_URL
    assert (payload["number"], payload["title"], payload["state"]) == (
        42,
        "Add the widget cache",
        "OPEN",
    )
    assert (payload["headBranch"], payload["baseBranch"], payload["headSha"]) == (
        "feature/cache",
        "main",
        HEAD,
    )
    assert payload["author"] == "Dev One"
    assert payload["updatedAt"] == "2026-01-03T00:00:00Z"
    assert payload["commits"] == [
        {
            "sha": HEAD,
            "title": "Add the widget cache",
            "body": "Keyed by tenant.",
            "author": "Dev One",
            "date": "2026-01-02T00:00:00Z",
            "url": f"https://{HOST}/bitbucket/projects/PROJ/repos/widgets/commits/{HEAD}",
        }
    ]
    files = {row["path"]: row for row in payload["files"]}
    assert files["src/cache.py"]["status"] == "added"
    assert files["src/cache.py"]["patch"] == "@@ -0,0 +1,2 @@\n+CACHE = {}\n+"
    assert files["src/app.py"]["patch"] == (
        "@@ -1,2 +1,2 @@\n import os\n-cache = None\n+from cache import CACHE"
    )
    assert (payload["additions"], payload["deletions"], payload["changedFiles"]) == (3, 1, 2)
    comments = payload["comments"]
    assert [(row["kind"], row["author"], row["threadId"]) for row in comments] == [
        ("inline", "Reviewer One", "11"),
        ("inline", "Dev One", "11"),
        ("review", "Reviewer One", ""),
    ]
    assert comments[0]["path"] == "src/cache.py" and comments[0]["line"] == 1
    assert comments[2]["state"] == "APPROVED"
    assert [(row["name"], row["bucket"], row["url"]) for row in payload["checks"]] == [
        ("Unit tests", "passed", "https://ci.acme.example/build/1"),
        ("Lint", "failed", ""),
    ]
    assert payload["partialSections"] == []

    # Every request went to the listed instance only, carried its own token, and
    # followed no redirect.
    assert instance.requests
    for request in instance.requests:
        assert request["url"].startswith(f"{REST}/")
        assert request["headers"]["Authorization"] == f"Bearer {TOKEN_VALUE}"
        assert request["redirects"] is False


@pytest.mark.asyncio
async def test_without_a_token_a_denied_read_names_the_secret_to_store(
    config_file, instance, vault
) -> None:
    instance.routes[PR_API] = (401, {"errors": []})
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": PR_URL})
        assert resp.status == 503
        error = (await resp.json())["error"]
    assert f"Settings > Secrets as {bitbucket_server.GLOBAL_TOKEN_NAME}." in error
    assert bitbucket_server.host_token_name(HOST) not in error
    assert "Authorization" not in instance.requests[0]["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_token_names_the_secret_to_replace(
    config_file, instance, vault, status
) -> None:
    vault[bitbucket_server.host_token_name(HOST)] = TOKEN_VALUE
    instance.routes[PR_API] = (status, {"errors": []})
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": PR_URL})
        error = (await resp.json())["error"]
    assert f"Replace {bitbucket_server.GLOBAL_TOKEN_NAME} in Settings > Secrets" in error


@pytest.mark.asyncio
async def test_a_redirect_is_reported_not_followed(config_file, instance, vault) -> None:
    instance.routes[PR_API] = (302, {})
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": PR_URL})
        assert resp.status == 503
        assert "HTTP 302" in (await resp.json())["error"]
    assert [request["url"] for request in instance.requests] == [PR_API]


@pytest.mark.asyncio
async def test_a_host_removed_mid_fetch_receives_no_further_request(
    config_file, instance, vault, monkeypatch
) -> None:
    vault[bitbucket_server.host_token_name(HOST)] = TOKEN_VALUE

    def revoke_on_primary_read(url: str) -> None:
        if url == PR_API:
            monkeypatch.setattr(hosts, "_bitbucket_server_hosts_snapshot", frozenset())

    instance.on_get = revoke_on_primary_read
    async with TestClient(TestServer(_app())) as client:
        await client.post("/api/source/pull-request", json={"url": PR_URL})
    assert [request["url"] for request in instance.requests] == [PR_API]


@pytest.mark.asyncio
async def test_a_failed_secondary_read_marks_its_section_partial(
    config_file, instance, vault
) -> None:
    del instance.routes[f"{PR_API}/activities?limit=100"]
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": PR_URL})
        payload = await resp.json()
    assert resp.status == 200
    assert payload["comments"] == []
    assert payload["partialSections"] == ["review discussions"]


@pytest.mark.asyncio
async def test_a_review_the_comment_cap_drops_marks_discussions_partial(
    config_file, instance, vault
) -> None:
    # A complete page whose comments fill the cap before a later approval.
    filler = [
        {
            "id": 2000 + index,
            "action": "COMMENTED",
            "createdDate": _ms(2),
            "comment": {"id": 3000 + index, "text": "note", "author": {"displayName": "Dev"}},
        }
        for index in range(bitbucket_server._MAX_COMMENTS)
    ]
    approval = {
        "id": 1999,
        "action": "APPROVED",
        "createdDate": _ms(3),
        "user": {"name": "rev1", "displayName": "Reviewer One"},
    }
    instance.routes[f"{PR_API}/activities?limit=100"] = (
        200,
        {"isLastPage": True, "values": [approval, *reversed(filler)]},
    )
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": PR_URL})
        payload = await resp.json()
    assert resp.status == 200
    assert len(payload["comments"]) == bitbucket_server._MAX_COMMENTS
    assert all(row["kind"] != "review" for row in payload["comments"])
    assert payload["partialSections"] == ["review discussions"]


@pytest.mark.asyncio
async def test_the_checks_tab_reads_the_head_commit_builds(config_file, instance, vault) -> None:
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request/checks", json={"url": PR_URL})
        assert resp.status == 200
        checks = (await resp.json())["checks"]
    assert [row["bucket"] for row in checks] == ["passed", "failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("/api/source/pull-request/comment", {"url": PR_URL, "body": "looks good"}),
        ("/api/source/pull-request/ready", {"url": PR_URL}),
    ],
)
async def test_writes_are_refused_as_unsupported(config_file, instance, vault, route, body) -> None:
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post(route, json=body)
        assert resp.status == 400
        assert "bitbucket_server" in (await resp.json())["error"]
    assert instance.requests == []


# --- token routing -----------------------------------------------------------


def test_the_single_host_token_serves_only_a_lone_listed_host(monkeypatch, vault) -> None:
    vault[bitbucket_server.GLOBAL_TOKEN_NAME] = TOKEN_VALUE
    monkeypatch.setattr(hosts, "_bitbucket_server_hosts_snapshot", frozenset({HOST}))
    assert bitbucket_server._resolve_token(HOST) == TOKEN_VALUE
    monkeypatch.setattr(
        hosts, "_bitbucket_server_hosts_snapshot", frozenset({HOST, "other.acme.example"})
    )
    assert bitbucket_server._resolve_token(HOST) == ""
    vault[bitbucket_server.host_token_name(HOST)] = "per-host"
    assert bitbucket_server._resolve_token(HOST) == "per-host"


def test_with_one_host_the_setup_guidance_names_only_the_shared_token(monkeypatch) -> None:
    monkeypatch.setattr(hosts, "_bitbucket_server_hosts_snapshot", frozenset({HOST}))
    guidance = bitbucket_server._setup_guidance(HOST)
    assert f"Settings > Secrets as {bitbucket_server.GLOBAL_TOKEN_NAME}." in guidance
    assert bitbucket_server.host_token_name(HOST) not in guidance


def test_with_two_hosts_the_setup_guidance_says_the_shared_token_stopped_applying(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        hosts, "_bitbucket_server_hosts_snapshot", frozenset({HOST, "other.acme.example"})
    )
    guidance = bitbucket_server._setup_guidance(HOST)
    assert bitbucket_server.host_token_name(HOST) in guidance
    assert (
        f"{bitbucket_server.GLOBAL_TOKEN_NAME} stops applying once a second host is listed"
        in guidance
    )


@pytest.mark.asyncio
async def test_a_host_removed_after_parsing_is_not_contacted(monkeypatch, instance, vault) -> None:
    monkeypatch.setattr(hosts, "_bitbucket_server_hosts_snapshot", frozenset({HOST}))
    ref = links.parse_source_url(PR_URL)
    monkeypatch.setattr(hosts, "_bitbucket_server_hosts_snapshot", frozenset())
    with pytest.raises(SourceProviderError, match="not listed"):
        await bitbucket_server.PROVIDER.fetch_full(ref)
    assert instance.requests == []


# --- sidebar chip ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected"),
    [("OPEN", "open"), ("MERGED", "merged"), ("DECLINED", "closed")],
)
async def test_the_chip_reads_lifecycle_and_build_rollup(
    config_file, monkeypatch, vault, state, expected
) -> None:
    stub = _Instance(_routes({**PR_DOC, "state": state}))
    monkeypatch.setattr(bitbucket_server, "ClientSession", stub.session)
    status = await chip_refresh._fetch_check_status(PR_URL)
    assert status == {"state": expected, "ci": "failed"}


@pytest.mark.asyncio
async def test_a_failed_build_with_one_still_running_reads_the_same_on_chip_and_full(
    config_file, listed, monkeypatch, vault
) -> None:
    builds = {
        "isLastPage": True,
        "values": [
            {"state": "FAILED", "key": "lint", "name": "Lint", "dateAdded": _ms(2)},
            {"state": "INPROGRESS", "key": "unit", "name": "Unit tests", "dateAdded": _ms(2)},
        ],
    }
    routes = {**_routes(), f"{REST}/build-status/1.0/commits/{HEAD}?limit=100": (200, builds)}
    stub = _Instance(routes)
    monkeypatch.setattr(bitbucket_server, "ClientSession", stub.session)

    chip = await chip_refresh._fetch_check_status(PR_URL)
    assert chip == {"state": "open", "ci": "failed", "ciPending": "1"}

    payload = await bitbucket_server.PROVIDER.fetch_full(links.parse_source_url(PR_URL))
    full = chip_status.status_from_full_payload(dict(payload))
    assert full is not None
    assert {key: full[key] for key in ("ci", "ciPending")} == {"ci": "failed", "ciPending": "1"}
