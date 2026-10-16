"""An app's repository: parsing its repo URL, the contributors list read from it,
and the ``/api/source/contributors`` endpoint."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from source_providers_helpers import _clear_issue_cache  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401
from source_providers_helpers import _app

from kiro_crew.dashboard.handlers import source_providers as source


def test_parse_repo_url_accepts_github_repo() -> None:
    ref = source.parse_repo_url("https://github.com/acme/console")
    assert ref.provider == "github"
    assert ref.host == "github.com"
    assert ref.owner == "acme"
    assert ref.repo == "console"


def test_parse_repo_url_strips_git_suffix_and_trailing_slash() -> None:
    ref = source.parse_repo_url("https://github.com/acme/repo.git/")
    assert (ref.owner, ref.repo) == ("acme", "repo")


def test_parse_repo_url_rejects_pull_request_url() -> None:
    # A pull URL has extra path segments and is not a repository root.
    with pytest.raises(ValueError):
        source.parse_repo_url("https://github.com/acme/repo/pull/12")


def test_parse_repo_url_rejects_userinfo() -> None:
    with pytest.raises(ValueError):
        source.parse_repo_url("https://user:pass@github.com/acme/repo")


def test_parse_repo_url_rejects_non_https() -> None:
    with pytest.raises(ValueError):
        source.parse_repo_url("http://github.com/acme/repo")


def test_parse_repo_url_rejects_non_allowlisted_host(monkeypatch) -> None:
    # Cold allowlist snapshot: a self-managed host is not recognized (fails closed).
    monkeypatch.setattr(source, "_gitlab_hosts_snapshot", frozenset())
    with pytest.raises(ValueError):
        source.parse_repo_url("https://git.internal.example.com/acme/repo")


def test_parse_repo_url_accepts_public_gitlab() -> None:
    ref = source.parse_repo_url("https://gitlab.com/group/subgroup/project")
    assert ref.provider == "gitlab"
    assert ref.owner == "group/subgroup"
    assert ref.repo == "project"


@pytest.mark.asyncio
async def test_fetch_app_contributors_uses_profile_name(monkeypatch) -> None:
    source._contributors_cache.clear()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock(return_value=frozenset()))

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if "/contributors?" in command:
            return [
                {"login": "octocat", "avatar_url": "https://avatars.example/1"},
                {"login": "hubot", "avatar_url": "https://avatars.example/2"},
            ]
        if command.endswith("users/octocat"):
            return {"name": "The Octocat"}
        if command.endswith("users/hubot"):
            return {"name": None}
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    result = await source.fetch_app_contributors("https://github.com/acme/repo")

    assert result == [
        {
            "login": "octocat",
            "name": "The Octocat",
            "avatarUrl": "https://avatars.example/1",
            "profileUrl": "https://github.com/octocat",
        },
        {
            "login": "hubot",
            "name": "hubot",  # null profile name falls back to the login
            "avatarUrl": "https://avatars.example/2",
            "profileUrl": "https://github.com/hubot",
        },
    ]


@pytest.mark.asyncio
async def test_fetch_app_contributors_non_github_returns_empty(monkeypatch) -> None:
    source._contributors_cache.clear()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock(return_value=frozenset()))
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)

    assert await source.fetch_app_contributors("https://gitlab.com/group/project") == []
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_app_contributors_coalesces_concurrent_calls(monkeypatch) -> None:
    source._contributors_cache.clear()
    source._contributors_inflight.clear()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock(return_value=frozenset()))
    calls = {"n": 0}

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if "/contributors?" in command:
            calls["n"] += 1
            # flake-ok: pre-existing 20 ms sleep inside the stub that keeps the first read in flight while gather starts the second, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 20 ms stub sleep, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.02)
            return [{"login": "octocat", "avatar_url": "a"}]
        if command.endswith("users/octocat"):
            return {"name": "Octo"}
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    first, second = await asyncio.gather(
        source.fetch_app_contributors("https://github.com/acme/repo"),
        source.fetch_app_contributors("https://github.com/acme/repo"),
    )

    assert first == second
    assert calls["n"] == 1  # one shared provider fanout for both callers
    # A second identical read is served from cache without another provider call.
    await source.fetch_app_contributors("https://github.com/acme/repo")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_fetch_app_contributors_skips_profile_for_unsafe_login(monkeypatch) -> None:
    source._contributors_cache.clear()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock(return_value=frozenset()))
    seen: list[str] = []

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        seen.append(command)
        if "/contributors?" in command:
            return [{"login": "../etc", "avatar_url": "a"}]
        raise AssertionError(f"unexpected profile lookup: {command}")

    monkeypatch.setattr(source, "_run_json", fake_run)
    result = await source.fetch_app_contributors("https://github.com/acme/repo")

    # A malformed login never reaches the users/<login> path; name falls back.
    assert result == [
        {
            "login": "../etc",
            "name": "../etc",
            "avatarUrl": "a",
            "profileUrl": "https://github.com/../etc",
        }
    ]
    assert not any("users/" in c for c in seen)


@pytest.mark.asyncio
async def test_app_contributors_endpoint_requires_owner_claim(monkeypatch) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(source, "fetch_app_contributors", fetch)

    async with TestClient(TestServer(_app(user="U_OTHER"))) as client:
        response = await client.post(
            "/api/source/contributors", json={"url": "https://github.com/acme/repo"}
        )
        assert response.status == 403
        assert await response.json() == {"error": "forbidden"}
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_app_contributors_endpoint_returns_list_for_owner(monkeypatch) -> None:
    payload = [
        {
            "login": "octocat",
            "name": "Octo",
            "avatarUrl": "https://a/1",
            "profileUrl": "https://github.com/octocat",
        }
    ]
    monkeypatch.setattr(source, "fetch_app_contributors", AsyncMock(return_value=payload))

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/contributors", json={"url": "https://github.com/acme/repo"}
        )
        assert response.status == 200
        assert await response.json() == {"contributors": payload}


@pytest.mark.asyncio
async def test_app_contributors_endpoint_maps_bad_url_to_400(monkeypatch) -> None:
    monkeypatch.setattr(
        source, "fetch_app_contributors", AsyncMock(side_effect=ValueError("bad url"))
    )

    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/source/contributors", json={"url": "nope"})
        assert response.status == 400
        assert (await response.json())["error"] == "bad url"
