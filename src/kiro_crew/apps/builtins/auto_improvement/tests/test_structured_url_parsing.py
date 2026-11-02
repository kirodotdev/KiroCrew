"""URL, path and name handling without regex: the structured helpers keep each shape.

The MR/PR URL checks, the GitLab clone path, and the generated directory and branch
names are parsed with ``urlsplit`` and plain character tests. These cases pin the
accepted and refused inputs, including near misses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup, pr_watchers
from kiro_crew.apps.builtins.auto_improvement.profiles.gitlab_repo import mr_recipe as mr
from kiro_crew.apps.builtins.auto_improvement.profiles.pr_recipe_base import (
    ProviderPRRecipe,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r/pull/12",
        "https://github.com/o/r/pull/12/files",
        "https://gitlab.com/g/p/-/merge_requests/3",
        "https://gitlab.com/g/sub/p/-/merge_requests/3/diffs",
        "https://git.example.com:8443/g/p/-/merge_requests/9",
        "HTTPS://GITHUB.COM/o/r/PULL/1",
    ],
)
def test_watchable_pr_urls(url: str) -> None:
    assert pr_watchers.is_watchable_pr(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "QUEUED:abc",
        "http://github.com/o/r/pull/12",  # not https
        "https://github.com/o/r/pull/",  # no number
        "https://github.com/o/r/pull/12a",  # not a number
        "https://github.com/o/r/pulls/12",  # near-miss segment
        "https://gitlab.com/g/p/merge_requests/3",  # no `-` segment
        "https://gitlab.com/g/p/-/merge_request/3",  # near-miss segment
        "https:///o/r/pull/1",  # no host
        "https://github.com/o/r?x=/pull/1",  # shape only in the query
        "https://github.com/o/r#/pull/1",  # shape only in the fragment
        "https://github.com/o/r/pull/1 trailing",  # whitespace
    ],
)
def test_unwatchable_pr_urls(url: str) -> None:
    assert not pr_watchers.is_watchable_pr(url)


@pytest.mark.parametrize(
    ("url", "is_mr"),
    [
        ("https://gitlab.com/g/p/-/merge_requests/3", True),
        ("https://gitlab.com/g/p/-/merge_requests/3/", True),
        ("https://gitlab.com/g/s/p/-/merge_requests/3", True),
        ("https://gitlab.com/g/p/-/MERGE_REQUESTS/3", True),
        ("https://gitlab.com/g/p/-/merge_requests/3/diffs", False),  # extra segment
        ("https://gitlab.com/-/merge_requests/3", False),  # no project
        ("https://gitlab.com/g//-/merge_requests/3", False),  # empty segment
        ("https://github.com/o/merge_requests/pull/1", False),  # a repo by that name
        ("https://gitlab.com/g/p/-/merge_requests/x", False),
        ("https://gitlab.com/g/p?/-/merge_requests/3", False),  # in the query
        ("http://gitlab.com/g/p/-/merge_requests/3", False),
    ],
)
def test_merge_request_url_is_the_exact_gitlab_shape(url: str, is_mr: bool) -> None:
    assert pr_watchers._is_merge_request_url(url) is is_mr


@pytest.mark.parametrize(
    ("stdout", "found"),
    [
        (
            "https://gitlab.com/g/p/-/merge_requests/7\n",
            "https://gitlab.com/g/p/-/merge_requests/7",
        ),
        (
            "Creating draft...\nhttps://gitlab.com/g/s/p/-/merge_requests/42 done",
            "https://gitlab.com/g/s/p/-/merge_requests/42",
        ),
        (
            "see https://h.example/g/p/-/merge_requests/5.",
            "https://h.example/g/p/-/merge_requests/5",
        ),
        (
            "x https://h.example/g/p/-/merge_requests/5 y https://h.example/g/p/-/merge_requests/6",
            "https://h.example/g/p/-/merge_requests/5",
        ),
        ("(https://h.example/g/p/-/merge_requests/8)", "https://h.example/g/p/-/merge_requests/8"),
        ("https://github.com/o/r/pull/1", None),
        ("https://gitlab.com/-/merge_requests/1", None),
        ("https://gitlab.com/g/p/-/merge_requests/", None),
        ("http://gitlab.com/g/p/-/merge_requests/1", None),
        ("no url here", None),
        ("", None),
    ],
)
def test_extract_mr_url(stdout: str, found: str | None) -> None:
    assert mr.extract_mr_url(stdout) == found


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://gitlab.com/g/p", ("gitlab.com", "g/p")),
        ("https://gitlab.com/g/p.git", ("gitlab.com", "g/p")),
        ("https://gitlab.com/g/s/p.git/", ("gitlab.com", "g/s/p")),
        ("  https://gitlab.com/g/p  ", ("gitlab.com", "g/p")),
        ("https://git.example.com:8443/g/p", ("git.example.com:8443", "g/p")),
        ("https://gitlab.com/", None),
        ("https://gitlab.com", None),
        ("http://gitlab.com/g/p", None),
        ("git@gitlab.com:g/p.git", None),
        ("https://gitlab.com/g/p?x=1", None),
        ("https://gitlab.com/g p", None),
    ],
)
def test_https_host_and_path(url: str, expected: tuple[str, str] | None) -> None:
    assert mr._https_host_and_path(url) == expected


@pytest.mark.parametrize(
    ("raw_path", "expected"),
    [
        ("/g/p", "g/p"),
        ("/g/s/p.git", "g/s/p"),
        ("/g/p/-/tree/main", "g/p"),
        ("/g.x/p_y-z", "g.x/p_y-z"),
        ("/p", ""),  # one segment is not a project
        ("/g/../p", ""),
        ("/g/./p", ""),
        ("/g/p q", ""),
        ("/g/p@x", ""),
        ("/g/" + "a" * 101, ""),  # over the length bound
        ("/g/" + "a" * 100, "g/" + "a" * 100),
    ],
)
def test_parse_gitlab_path_checks_each_segment(raw_path: str, expected: str) -> None:
    assert clone_setup._parse_gitlab_path(raw_path) == expected


@pytest.mark.parametrize(
    ("host", "path", "readable"),
    [
        ("gitlab.com", "g/p", "gitlab.com--g--p"),
        ("git.example.com:8443", "g/s/p", "git.example.com--8443--g--s--p"),
        ("gitlab.com", "g/p/", "gitlab.com--g--p"),
        ("gitlab.com", "-g/p", "gitlab.com---g--p"),
    ],
)
def test_gitlab_dir_name_keeps_allowed_characters(host: str, path: str, readable: str) -> None:
    name = clone_setup._gitlab_dir_name(host, path)
    assert name.startswith(f"gitlab+{readable}--")
    assert len(name.rsplit("--", 1)[-1]) == 12


def test_keep_allowed_folds_each_run_into_one_separator() -> None:
    allowed = frozenset("ab")
    assert clone_setup._keep_allowed("a//b::a", allowed, "-") == "a-b-a"
    assert clone_setup._keep_allowed("", allowed, "-") == ""
    assert clone_setup._keep_allowed("xx", allowed, "--") == "--"


@pytest.mark.parametrize(
    ("kind", "safe"),
    [
        ("perf", "perf"),
        ("Bug Fix!!", "bug-fix"),
        ("  a__b  ", "a-b"),
        ("!!!", "fix"),
        ("", "fix"),
        ("ünïcode", "n-code"),
    ],
)
def test_branch_name_keeps_lowercase_letters_and_digits(
    tmp_path: Path, kind: str, safe: str
) -> None:
    recipe = ProviderPRRecipe.__new__(ProviderPRRecipe)
    assert recipe.branch_name(kind=kind, fingerprint="ab") == f"auto-improvement/{safe}-ab"
