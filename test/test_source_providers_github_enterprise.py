"""GitHub Enterprise hosts: the ``dashboard.github_hosts`` allowlist end to end.

The allowlist mirrors ``dashboard.gitlab_hosts``: exact ``host[:port]`` match,
empty by default, config-only. These tests pin the guard rails -- an empty list
leaves every github.com path as it is, a lookalike host is never recognized, and
a host cannot be claimed by two provider allowlists.
"""

from __future__ import annotations

import ast
import logging
import pathlib
from typing import Any

import pytest

from kiro_crew.config import loader as config_loader
from kiro_crew.config import sections as config_sections
from kiro_crew.dashboard.source_providers import (
    cache,
    chip_status,
    github,
    hosts,
    links,
    mutations,
    runner,
)
from kiro_crew.dashboard.source_providers.contract import RepoRef, SourceProviderError, SourceRef

GHE = "ghe.example.com"


@pytest.fixture
def ghe_listed(monkeypatch):
    """Seed the cached snapshot the sync parsers read, as a config load would."""
    monkeypatch.setattr(hosts, "_github_hosts_snapshot", frozenset({GHE}))


@pytest.fixture(autouse=True)
def _cold_allowlists(_floor_monkeypatch):
    _floor_monkeypatch.setattr(hosts, "_github_hosts_snapshot", frozenset())
    _floor_monkeypatch.setattr(hosts, "_gitlab_hosts_snapshot", frozenset())
    _floor_monkeypatch.setattr(hosts, "_jira_hosts_snapshot", frozenset())


# --- config ------------------------------------------------------------------


def test_github_hosts_defaults_to_empty() -> None:
    dashboard = config_loader._build_dashboard_config(set(), {})
    assert dashboard.github_hosts == []


def test_github_hosts_entries_are_normalized_like_gitlab_hosts() -> None:
    raw = [
        " GHE.Example.com ",
        "ghe.example.com",  # duplicate after normalization
        "ghe2.example.com:443",  # default port dropped
        "ghe3.example.com.:08443",  # a non-default port: gh cannot address it
        "https://ghe4.example.com",  # scheme
        "*.example.com",  # wildcard
        "ghe5.example.com/path",  # path
        "user@ghe6.example.com",  # userinfo
        "ghe7.example.com:",  # empty port
        "github.com",  # needs no entry
        "www.github.com",
        "gitlab.com",  # another provider's public host
        42,
    ]
    assert config_sections._coerce_github_hosts(raw) == [
        "ghe.example.com",
        "ghe2.example.com",
    ]
    assert config_sections._coerce_github_hosts(["ghe8.example.com."]) == ["ghe8.example.com"]
    assert config_sections._coerce_github_hosts("ghe.example.com") == []


def test_a_host_also_in_gitlab_hosts_is_refused_at_config_load(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        dashboard = config_loader._build_dashboard_config(
            set(),
            {
                "github_hosts": ["shared.example.com", GHE],
                "gitlab_hosts": ["shared.example.com"],
            },
        )
    assert dashboard.github_hosts == [GHE]
    # The GitLab side is untouched by the refusal.
    assert dashboard.gitlab_hosts == ["shared.example.com"]
    refused = [record.args for record in caplog.records if record.levelno == logging.WARNING]
    assert ("shared.example.com",) in refused


def test_a_host_also_in_jira_hosts_is_refused_at_config_load() -> None:
    dashboard = config_loader._build_dashboard_config(
        set(),
        {"github_hosts": ["jira.example.com:8443"], "jira_hosts": ["JIRA.example.com:8443"]},
    )
    assert dashboard.github_hosts == []
    assert dashboard.jira_hosts == ["jira.example.com:8443"]


@pytest.mark.asyncio
async def test_the_refresh_publishes_github_hosts_and_bumps_the_generation(monkeypatch) -> None:
    def fake_load():
        return frozenset(), frozenset(), True, frozenset({GHE}), frozenset(), frozenset()

    monkeypatch.setattr(hosts, "_load_source_link_settings", fake_load)
    monkeypatch.setattr(hosts, "_gitlab_hosts_loaded_at", 0.0)
    monkeypatch.setattr(hosts, "_gitlab_hosts_generation", 3)
    # The same refresh publishes the chip switch, which bumps the shared
    # generation too when it changes; pin it to the loaded value so only the
    # host list can move it here.
    monkeypatch.setattr(hosts, "_session_card_chips_snapshot", True)

    assert hosts._allowed_github_hosts() == frozenset()
    await hosts.ensure_gitlab_hosts_loaded()
    assert hosts._allowed_github_hosts() == frozenset({GHE})
    # Cached parse decisions fold the generation in, so a newly listed host
    # must move it or a cold-snapshot rejection would stay memoized.
    assert hosts.gitlab_hosts_generation() == 4


def test_an_unreadable_config_leaves_github_hosts_closed(monkeypatch) -> None:
    def broken_load(*_a, **_k):
        raise OSError("torn read")

    monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(broken_load))
    assert hosts._load_source_link_settings()[3] == frozenset()


# --- parse_source_url ----------------------------------------------------------


def test_github_dot_com_parses_exactly_as_without_the_allowlist() -> None:
    expected = SourceRef(
        "github", "https://github.com/acme/repo/pull/12", "github.com", "acme", "repo", 12
    )
    assert links.parse_source_url("https://github.com/acme/repo/pull/12") == expected
    assert links.parse_source_url("https://www.github.com/acme/repo/pull/12/") == expected
    issue = links.parse_source_url("https://github.com/acme/repo/issues/3")
    assert (issue.host, issue.kind, issue.url) == (
        "github.com",
        "issue",
        "https://github.com/acme/repo/issues/3",
    )


def test_an_unlisted_enterprise_host_is_refused() -> None:
    with pytest.raises(ValueError):
        links.parse_source_url(f"https://{GHE}/acme/repo/pull/12")


@pytest.mark.usefixtures("ghe_listed")
def test_a_listed_enterprise_host_parses_as_github_and_keeps_its_host() -> None:
    ref = links.parse_source_url(f"https://{GHE.upper()}/acme/repo/pull/12")
    assert ref == SourceRef("github", f"https://{GHE}/acme/repo/pull/12", GHE, "acme", "repo", 12)
    issue = links.parse_source_url(f"https://{GHE}./acme/repo/issues/9")
    assert (issue.provider, issue.host, issue.kind, issue.number) == ("github", GHE, "issue", 9)
    assert issue.url == f"https://{GHE}/acme/repo/issues/9"
    # Explicit :443 is the same origin the browser URL API reports.
    assert links.parse_source_url(f"https://{GHE}:443/acme/repo/pull/12").url == (
        f"https://{GHE}/acme/repo/pull/12"
    )


@pytest.mark.usefixtures("ghe_listed")
def test_the_enterprise_identity_never_collides_with_github_dot_com() -> None:
    public = links.parse_source_url("https://github.com/acme/repo/pull/12")
    enterprise = links.parse_source_url(f"https://{GHE}/acme/repo/pull/12")
    assert public.identity != enterprise.identity


@pytest.mark.usefixtures("ghe_listed")
@pytest.mark.parametrize(
    "url",
    [
        "https://github.com.evil.example/acme/repo/pull/1",
        f"https://{GHE}.attacker/acme/repo/pull/1",
        f"https://evil-{GHE}/acme/repo/pull/1",
        f"https://sub.{GHE}/acme/repo/pull/1",
        "https://example.com/acme/repo/pull/1",
        f"https://evil.example/{GHE}/acme/repo/pull/1",
        f"https://evil.example/acme/repo/pull/1?u=https://{GHE}/acme/repo/pull/1",
        f"https://user@{GHE}/acme/repo/pull/1",
        f"http://{GHE}/acme/repo/pull/1",
        f"https://{GHE}:8443/acme/repo/pull/1",
    ],
)
def test_lookalike_and_unlisted_spellings_are_not_recognized(url: str) -> None:
    with pytest.raises(ValueError):
        links.parse_source_url(url)


def test_a_ported_entry_is_refused_at_config_load(caplog) -> None:
    """gh rejects a port in ``--hostname``, so a ported entry can never be
    reached; keeping it, or pinning its bare name, would aim calls elsewhere."""
    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        dashboard = config_loader._build_dashboard_config(
            set(), {"github_hosts": [f"{GHE}:8443", GHE]}
        )
    assert dashboard.github_hosts == [GHE]
    refused = [record.args for record in caplog.records if record.levelno == logging.WARNING]
    assert (f"{GHE}:8443",) in refused


@pytest.mark.usefixtures("ghe_listed")
def test_a_ported_url_on_a_listed_host_is_refused() -> None:
    with pytest.raises(ValueError):
        links.parse_source_url(f"https://{GHE}:8443/acme/repo/pull/1")
    assert links.parse_source_url(f"https://{GHE}:443/acme/repo/pull/1").host == GHE


@pytest.mark.usefixtures("ghe_listed")
def test_a_listed_enterprise_host_still_requires_the_github_path_grammar() -> None:
    for path in ("/acme/repo/-/merge_requests/1", "/acme/repo/pull/x", "/../repo/pull/1"):
        with pytest.raises(ValueError):
            links.parse_source_url(f"https://{GHE}{path}")


# --- parse_repo_url ------------------------------------------------------------


def test_repo_url_on_an_unlisted_enterprise_host_is_refused() -> None:
    with pytest.raises(ValueError):
        links.parse_repo_url(f"https://{GHE}/acme/repo")


@pytest.mark.usefixtures("ghe_listed")
def test_repo_url_on_a_listed_enterprise_host_keeps_its_host() -> None:
    assert links.parse_repo_url(f"https://{GHE}/acme/repo.git") == RepoRef(
        "github", GHE, "acme", "repo"
    )
    assert links.parse_repo_url("https://github.com/acme/repo") == RepoRef(
        "github", "github.com", "acme", "repo"
    )
    with pytest.raises(ValueError):
        links.parse_repo_url(f"https://{GHE}/acme/repo/pull/1")
    with pytest.raises(ValueError):
        links.parse_repo_url(f"https://{GHE}.attacker/acme/repo")


# --- runner: host pinning, credentials, --hostname -----------------------------


@pytest.fixture
def captured_spawn(monkeypatch):
    """Capture the argv and env ``_run_provider`` would spawn, then stop.

    The sandbox wrapper is the last step before the child starts; refusing there
    lets a test read exactly what would have run without running anything.
    """
    seen: dict[str, Any] = {}
    audits: list[tuple[str, str]] = []

    def capture(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["env"] = dict(kwargs["env"])
        raise RuntimeError("captured")

    monkeypatch.setattr(runner, "_resolve_provider_executable", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(runner, "sandboxed_spawn_argv", capture)
    monkeypatch.setattr(
        runner,
        "_audit_provider_cli",
        lambda executable, outcome, reason, **_k: audits.append((outcome, reason)),
    )
    monkeypatch.setenv("GH_TOKEN", "public-credential-value")
    monkeypatch.setenv("GITHUB_TOKEN", "public-credential-value")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "enterprise-credential-value")
    seen["audits"] = audits
    return seen


@pytest.mark.asyncio
async def test_a_github_dot_com_call_is_spawned_exactly_as_without_the_allowlist(
    captured_spawn,
) -> None:
    for host in ("", "github.com"):
        with pytest.raises(SourceProviderError, match="could not start securely"):
            await runner._run_json("gh", "api", "repos/acme/repo/pulls/1", host=host)
        assert captured_spawn["argv"] == ["/usr/bin/gh", "api", "repos/acme/repo/pulls/1"]
        env = captured_spawn["env"]
        assert env["GH_HOST"] == "github.com"
        assert env["GH_TOKEN"] == "public-credential-value"
        assert env["GITHUB_TOKEN"] == "public-credential-value"
        assert "GH_ENTERPRISE_TOKEN" not in env


@pytest.mark.asyncio
@pytest.mark.usefixtures("ghe_listed")
async def test_a_listed_host_call_carries_hostname_and_no_github_dot_com_credential(
    captured_spawn,
) -> None:
    with pytest.raises(SourceProviderError, match="could not start securely"):
        await runner._run_json("gh", "api", "repos/acme/repo/pulls/1", host=GHE)
    assert captured_spawn["argv"] == [
        "/usr/bin/gh",
        "api",
        "--hostname",
        GHE,
        "repos/acme/repo/pulls/1",
    ]
    env = captured_spawn["env"]
    assert env["GH_HOST"] == GHE
    # Only gh's own per-host login authenticates the Enterprise host.
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        assert key not in env
    assert "public-credential-value" not in env.values()

    # A non-api subcommand takes the host from its URL and from GH_HOST.
    url = f"https://{GHE}/acme/repo/pull/1"
    with pytest.raises(SourceProviderError, match="could not start securely"):
        await runner._run_json("gh", "pr", "view", url, "--json", "state", host=GHE)
    assert captured_spawn["argv"] == ["/usr/bin/gh", "pr", "view", url, "--json", "state"]
    assert captured_spawn["env"]["GH_HOST"] == GHE


@pytest.mark.asyncio
async def test_an_unlisted_host_is_denied_before_spawn(captured_spawn) -> None:
    for host in (GHE, "github.com.evil.example"):
        with pytest.raises(SourceProviderError, match="not allowlisted"):
            await runner._run_json("gh", "api", "repos/acme/repo", host=host)
    assert "argv" not in captured_spawn
    assert captured_spawn["audits"] == [("denied", "host_not_allowlisted")] * 2


@pytest.mark.asyncio
async def test_a_ported_host_is_denied_even_if_it_reached_the_snapshot(
    monkeypatch, captured_spawn
) -> None:
    monkeypatch.setattr(hosts, "_github_hosts_snapshot", frozenset({f"{GHE}:8443"}))
    with pytest.raises(SourceProviderError, match="not allowlisted"):
        await runner._run_json("gh", "api", "repos/acme/repo", host=f"{GHE}:8443")
    assert "argv" not in captured_spawn
    assert captured_spawn["audits"] == [("denied", "host_not_allowlisted")]


@pytest.mark.asyncio
@pytest.mark.usefixtures("ghe_listed")
async def test_a_caller_cannot_choose_the_hostname_itself(captured_spawn) -> None:
    for spelling in (["--hostname", "evil.example"], ["--hostname=evil.example"]):
        with pytest.raises(SourceProviderError, match="set by the runner"):
            await runner._run_json("gh", "api", *spelling, "repos/acme/repo", host=GHE)
    assert "argv" not in captured_spawn
    assert captured_spawn["audits"] == [("denied", "hostname_in_argv")] * 2


def test_the_sign_in_hint_names_the_enterprise_host() -> None:
    stderr = b"To get started with GitHub CLI, please run: gh auth login (not logged in)"
    enterprise = runner._provider_failure("gh", stderr, GHE)
    assert enterprise.login_command == f"gh auth login --hostname {GHE}"
    assert f"`gh auth login --hostname {GHE}`" in str(enterprise)
    public = runner._provider_failure("gh", stderr, "github.com")
    assert public.login_command == ""
    assert str(public).endswith("Run `gh auth login`, then retry.")


def _source_provider_modules() -> list[pathlib.Path]:
    package = pathlib.Path(runner.__file__).parent
    return sorted(path for path in package.glob("*.py") if path.name != "runner.py")


def _is_gh_call(node: ast.Call) -> bool:
    """A provider call whose executable is ``gh`` (or an argv list built for it)."""
    if not node.args:
        return False
    first = node.args[0]
    if isinstance(first, ast.Constant):
        return first.value == "gh"
    return isinstance(first, ast.Starred)


def test_every_gh_call_names_its_host() -> None:
    """A gh call that omits ``host`` is a github.com call, so on an Enterprise ref
    it would read -- or write -- the same owner/repo/number on github.com. Every
    gh call site outside the runner must therefore say which host it targets."""
    missing: list[str] = []
    seen = 0
    for path in _source_provider_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"_run_json", "_run_provider"} or not _is_gh_call(node):
                continue
            seen += 1
            if not any(keyword.arg == "host" for keyword in node.keywords):
                missing.append(f"{path.name}:{node.lineno}")
    assert seen > 20
    assert missing == []


# --- provider reads and writes target the ref's host ---------------------------


def _ghe_ref(kind: str = "change") -> SourceRef:
    segment = "pull" if kind == "change" else "issues"
    return SourceRef(
        "github", f"https://{GHE}/acme/repo/{segment}/12", GHE, "acme", "repo", 12, kind=kind
    )


def _public_ref() -> SourceRef:
    return SourceRef(
        "github", "https://github.com/acme/repo/pull/12", "github.com", "acme", "repo", 12
    )


@pytest.fixture
def recorded_calls(monkeypatch):
    """Answer every provider call with a minimal payload and record its host."""
    calls: list[tuple[tuple[str, ...], str]] = []

    async def fake_run_provider(*argv: str, host: str = "", **_kwargs: Any) -> Any:
        calls.append((argv, host))
        if argv[:3] == ("gh", "pr", "view"):
            return {
                "headRefOid": "a" * 40,
                "commits": [{"oid": "b" * 40, "messageHeadline": "x", "authors": []}],
                "state": "OPEN",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
            }
        if argv[:3] == ("gh", "api", "graphql"):
            return {"data": {"repository": {"pullRequest": {"id": "PR_node", "isDraft": True}}}}
        return {}

    monkeypatch.setattr(runner, "_run_provider", fake_run_provider)
    return calls


@pytest.mark.asyncio
@pytest.mark.usefixtures("ghe_listed")
async def test_the_full_read_targets_the_enterprise_host_and_links_to_it(recorded_calls) -> None:
    payload = await github._fetch_github(_ghe_ref())
    assert recorded_calls
    assert {host for _argv, host in recorded_calls} == {GHE}
    assert payload["url"] == f"https://{GHE}/acme/repo/pull/12"
    assert payload["commits"][0]["url"] == f"https://{GHE}/acme/repo/commit/{'b' * 40}"


@pytest.mark.asyncio
@pytest.mark.usefixtures("ghe_listed")
async def test_the_issue_read_targets_the_enterprise_host(recorded_calls) -> None:
    await github._fetch_github_issue(_ghe_ref("issue"))
    assert len(recorded_calls) == 3
    assert {host for _argv, host in recorded_calls} == {GHE}


@pytest.mark.asyncio
@pytest.mark.usefixtures("ghe_listed")
async def test_a_write_targets_the_enterprise_host(monkeypatch, recorded_calls) -> None:
    async def no_invalidate(_url: str) -> None:
        return None

    async def loaded() -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr(cache, "_invalidate_pull_request_cache", no_invalidate)
    monkeypatch.setattr(hosts, "ensure_gitlab_hosts_loaded", loaded)
    await mutations.comment_on_pull_request(f"https://{GHE}/acme/repo/pull/12", "hello")
    assert recorded_calls
    assert {host for _argv, host in recorded_calls} == {GHE}


@pytest.mark.asyncio
@pytest.mark.usefixtures("ghe_listed")
async def test_contributors_target_the_enterprise_host_and_link_to_it(monkeypatch) -> None:
    calls: list[tuple[tuple[str, ...], str]] = []

    async def contributors_run(*argv: str, host: str = "", **_kwargs: Any) -> Any:
        calls.append((argv, host))
        if "contributors" in argv[2]:
            return [{"login": "octo", "avatar_url": ""}]
        return {"name": "Octo Cat"}

    monkeypatch.setattr(runner, "_run_provider", contributors_run)
    monkeypatch.setattr(cache, "_contributors_cache", {})
    rows = await cache._fetch_github_contributors(
        RepoRef("github", GHE, "acme", "repo"), f"{GHE}/acme/repo"
    )
    assert {host for _argv, host in calls} == {GHE}
    assert rows[0]["profileUrl"] == f"https://{GHE}/octo"


def test_conditional_get_revalidation_stays_on_github_dot_com() -> None:
    assert cache._revalidation_applies(_public_ref()) is True
    assert cache._revalidation_applies(_ghe_ref()) is False


@pytest.mark.asyncio
async def test_an_enterprise_repo_is_never_treated_as_public(monkeypatch) -> None:
    async def no_call(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("visibility is decided without a provider read")

    monkeypatch.setattr(runner, "_run_provider", no_call)
    assert await chip_status._fetch_repo_visibility(_ghe_ref()) is False


# --- check rollup: GraphQL first, REST when an Enterprise field is missing -------


HEAD = "c" * 40
_DEFAULT_RUNS = {
    "total_count": 2,
    "check_runs": [
        {
            "name": "build",
            "status": "completed",
            "conclusion": "failure",
            "head_sha": HEAD,
            "details_url": f"https://{GHE}/acme/repo/runs/1",
        },
        {"name": "lint", "status": "in_progress", "conclusion": None, "head_sha": HEAD},
    ],
}
_DEFAULT_STATUS = {
    "sha": HEAD,
    "total_count": 1,
    "statuses": [{"context": "ci/legacy", "state": "success"}],
}


def _rest_rollup_run(runs: dict | None = None, status: dict | None = None):
    calls: list[tuple[str, ...]] = []

    async def run(*argv: str, host: str = "", **_kwargs: Any) -> Any:
        calls.append(argv)
        if argv[:3] == ("gh", "api", "graphql"):
            raise SourceProviderError("Field 'workflowRun' doesn't exist on type 'CheckSuite'")
        endpoint = argv[2]
        if endpoint.endswith("/pulls/12"):
            return {"head": {"sha": HEAD}}
        if "/check-runs" in endpoint:
            return _DEFAULT_RUNS if runs is None else runs
        if "/status?" in endpoint:
            return _DEFAULT_STATUS if status is None else status
        raise AssertionError(f"unexpected call {argv}")

    return run, calls


@pytest.mark.asyncio
@pytest.mark.usefixtures("ghe_listed")
async def test_a_missing_enterprise_graphql_field_degrades_to_the_rest_rollup(monkeypatch) -> None:
    run, calls = _rest_rollup_run()
    monkeypatch.setattr(runner, "_run_provider", run)
    checks, head = await github._github_rollup_read(_ghe_ref())
    assert head == HEAD
    assert [(c["name"], c["bucket"]) for c in checks] == [
        ("build", "failed"),
        ("lint", "pending"),
        ("ci/legacy", "passed"),
    ]
    assert checks[0]["url"] == f"https://{GHE}/acme/repo/runs/1"
    assert any("/check-runs" in argv[2] for argv in calls)


@pytest.mark.asyncio
async def test_github_dot_com_keeps_the_graphql_rollup_alone(monkeypatch) -> None:
    run, calls = _rest_rollup_run()
    monkeypatch.setattr(runner, "_run_provider", run)
    with pytest.raises(SourceProviderError):
        await github._github_rollup_read(_public_ref())
    assert calls and all(argv[:3] == ("gh", "api", "graphql") for argv in calls)


@pytest.mark.asyncio
@pytest.mark.usefixtures("ghe_listed")
@pytest.mark.parametrize(
    "runs, status",
    [
        ({"total_count": 101, "check_runs": []}, None),
        ({"total_count": 1, "check_runs": [{"name": "x", "head_sha": "d" * 40}]}, None),
        (None, {"sha": "d" * 40, "total_count": 0, "statuses": []}),
        ({"check_runs": "nope"}, None),
    ],
)
async def test_a_partial_or_mismatched_rest_rollup_raises(monkeypatch, runs, status) -> None:
    run, _calls = _rest_rollup_run(runs=runs, status=status)
    monkeypatch.setattr(runner, "_run_provider", run)
    with pytest.raises(SourceProviderError):
        await github._github_rollup_read(_ghe_ref())


@pytest.mark.asyncio
@pytest.mark.usefixtures("ghe_listed")
async def test_a_rest_head_that_is_not_a_sha_never_reaches_an_api_path(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def run(*argv: str, host: str = "", **_kwargs: Any) -> Any:
        calls.append(argv)
        if argv[:3] == ("gh", "api", "graphql"):
            raise SourceProviderError("unknown field")
        return {"head": {"sha": "../../../user"}}

    monkeypatch.setattr(runner, "_run_provider", run)
    with pytest.raises(SourceProviderError):
        await github._github_rollup_read(_ghe_ref())
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("ghe_listed")
async def test_the_full_read_keeps_the_view_when_the_rollup_degrades(
    monkeypatch, recorded_calls
) -> None:
    async def rollup_fails(_ref: SourceRef) -> Any:
        raise SourceProviderError("rollup unavailable")

    monkeypatch.setattr(github, "_github_rollup_read", rollup_fails)
    payload = await github._fetch_github(_ghe_ref())
    assert "checks" in payload["partialSections"]
    assert payload["url"] == f"https://{GHE}/acme/repo/pull/12"
