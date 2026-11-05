"""A self-managed GitLab served under a relative URL root (``/gitlab``).

The root is declared on the ``dashboard.gitlab_hosts`` entry
(``gitlab.example.com/gitlab``). It authorizes the host, is stripped before the
project path is split (glab already serves its API under it), and stays in every
canonical URL.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from source_providers_helpers import _clear_shared_fetch_state  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401

from kiro_crew.config.loader_builders import _build_dashboard_config
from kiro_crew.config.sections_dashboard_hosts import _coerce_gitlab_hosts
from kiro_crew.dashboard.handlers import source_providers as source
from kiro_crew.dashboard.source_providers import hosts
from kiro_crew.monitoring.targets import (
    infer_pull_request_kind,
    normalize_pull_request_target,
    parse_gitlab_merge_request_target,
)

_MR_URL = "https://gitlab.example.com/gitlab/group/subgroup/project/-/merge_requests/12"


@pytest.fixture()
def rooted_allowlist(monkeypatch):
    monkeypatch.setattr(hosts, "_gitlab_hosts_snapshot", frozenset({"gitlab.example.com/gitlab"}))


# --- config ---------------------------------------------------------------


def test_gitlab_hosts_entry_may_carry_a_url_root() -> None:
    entries = _coerce_gitlab_hosts(
        ["Gitlab.Example.com/gitlab/", "git.example.com:8443/Tools/GitLab", "plain.example.com"],
        allow_url_root=True,
    )
    assert entries == [
        "gitlab.example.com/gitlab",
        "git.example.com:8443/Tools/GitLab",
        "plain.example.com",
    ]


@pytest.mark.parametrize(
    "entry",
    [
        "gitlab.example.com/../x",
        "gitlab.example.com/./x",
        "gitlab.example.com/a b",
        "gitlab.example.com/a?b",
        "gitlab.example.com/a#b",
        "gitlab.example.com/a%2fb",
        "https://gitlab.example.com/gitlab",
        "/gitlab",
        "gitlab.example.com/" + "/".join(["a"] * 9),
    ],
)
def test_gitlab_hosts_refuses_a_malformed_url_root(entry) -> None:
    assert _coerce_gitlab_hosts([entry], allow_url_root=True) == []


def test_one_host_names_one_gitlab_instance() -> None:
    assert _coerce_gitlab_hosts(
        ["gitlab.example.com/gitlab", "gitlab.example.com", "gitlab.example.com/other"],
        allow_url_root=True,
    ) == ["gitlab.example.com/gitlab"]


def test_other_host_lists_still_refuse_a_path() -> None:
    assert _coerce_gitlab_hosts(["gitlab.example.com/gitlab"]) == []


def test_dashboard_config_keeps_the_root_and_claims_only_the_host() -> None:
    dashboard = _build_dashboard_config(
        set(),
        {
            "gitlab_hosts": ["gitlab.example.com/gitlab"],
            "github_hosts": ["gitlab.example.com"],
        },
    )
    assert dashboard.gitlab_hosts == ["gitlab.example.com/gitlab"]
    # The host belongs to GitLab, so the GitHub entry naming it is refused.
    assert dashboard.github_hosts == []


# --- Changes panel ----------------------------------------------------------


def test_source_url_strips_the_url_root_from_the_project(rooted_allowlist) -> None:
    ref = source.parse_source_url(_MR_URL)
    assert ref.provider == "gitlab"
    assert ref.host == "gitlab.example.com"
    assert ref.project == "group/subgroup/project"
    assert ref.owner == "group/subgroup"
    assert ref.repo == "project"
    assert ref.number == 12
    assert ref.url == _MR_URL


def test_source_url_outside_the_url_root_is_refused(rooted_allowlist) -> None:
    with pytest.raises(ValueError, match="/gitlab"):
        source.parse_source_url(
            "https://gitlab.example.com/group/subgroup/project/-/merge_requests/12"
        )


def test_repo_url_strips_the_url_root(rooted_allowlist) -> None:
    ref = source.parse_repo_url("https://gitlab.example.com/gitlab/group/subgroup/project")
    assert (ref.host, ref.owner, ref.repo) == ("gitlab.example.com", "group/subgroup", "project")


@pytest.mark.asyncio
async def test_changes_panel_reads_the_project_without_the_url_root(
    monkeypatch, rooted_allowlist
) -> None:
    monkeypatch.setattr(hosts, "_gitlab_hosts_loaded_at", 10**12)
    monkeypatch.setattr(hosts, "_gitlab_hosts_fresh", lambda: True)
    run = AsyncMock(side_effect=[[], []])
    monkeypatch.setattr(source, "_run_json", run)

    await source.fetch_pull_request_checks(_MR_URL)

    first = run.await_args_list[0]
    assert any(
        str(arg).startswith("projects/group%2Fsubgroup%2Fproject/merge_requests/12")
        for arg in first.args
    )
    assert first.kwargs.get("host") == "gitlab.example.com"


# --- monitor_watch -----------------------------------------------------------


def test_monitor_target_strips_the_url_root() -> None:
    target = parse_gitlab_merge_request_target(_MR_URL, gitlab_hosts=["gitlab.example.com/gitlab"])
    assert target.host == "gitlab.example.com"
    assert target.project_path == "group/subgroup/project"
    assert target.iid == 12
    assert target.url == _MR_URL


def test_monitor_target_infers_and_normalizes_under_a_url_root() -> None:
    hosts_cfg = ("gitlab.example.com/gitlab",)
    assert infer_pull_request_kind(_MR_URL, gitlab_hosts=hosts_cfg) == "gitlab_merge_request"
    assert (
        normalize_pull_request_target("gitlab_merge_request", _MR_URL, gitlab_hosts=hosts_cfg)
        == _MR_URL
    )


def test_monitor_target_outside_the_url_root_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_gitlab_merge_request_target(
            "https://gitlab.example.com/group/subgroup/project/-/merge_requests/12",
            gitlab_hosts=["gitlab.example.com/gitlab"],
        )
