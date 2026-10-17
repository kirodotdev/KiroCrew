"""Compatibility seams for the split GitHub client facade."""

import subprocess
from unittest import mock

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh


def test_api_transport_uses_the_facades_current_runner():
    proc = subprocess.CompletedProcess([], 0, stdout='{"number": 7}\n', stderr="")
    with mock.patch.object(gh, "_gh_run", return_value=proc) as run:
        assert gh._run_gh_api("repos/o/r/issues", ".[]", paginate=False) == [{"number": 7}]

    run.assert_called_once_with(
        ["gh", "api", "repos/o/r/issues", "--jq", ".[]"],
        timeout=gh.GH_TIMEOUT_SEC,
    )


def test_transport_errors_use_the_facades_current_sanitizer():
    proc = subprocess.CompletedProcess([], 1, stdout="", stderr="private path")
    with (
        mock.patch.object(gh, "_gh_run", return_value=proc),
        mock.patch.object(gh, "sanitize_cli_stderr", return_value="clean") as sanitize,
    ):
        try:
            gh._run_gh_api("repos/o/r/issues", ".[]", paginate=False)
        except gh.GhCliError as exc:
            assert str(exc).endswith(": clean")
        else:  # pragma: no cover - the transport must reject a non-zero exit
            raise AssertionError("expected GhCliError")

    sanitize.assert_called_once_with("private path")


def test_timeline_normalizer_uses_the_facades_current_reaction_helper():
    shaped = {"total": 41}
    event = {
        "event": "commented",
        "user": {"login": "octo"},
        "reactions": {"total_count": 1},
    }
    with mock.patch.object(gh, "_norm_reactions", return_value=shaped) as normalize:
        normalized = gh._normalize_timeline_event(event)
        assert normalized is not None
        assert normalized["reactions"] is shaped

    normalize.assert_called_once_with({"total_count": 1})


def test_summary_query_uses_the_facades_current_runner_and_parser():
    proc = subprocess.CompletedProcess([], 0, stdout="raw rows", stderr="")
    expected = {7: {"additions": 2}}
    with (
        mock.patch.object(gh, "_gh_run", return_value=proc) as run,
        mock.patch.object(gh, "_parse_summary_rows", return_value=expected) as parse,
    ):
        assert gh.fetch_pr_summaries("o", "r") == expected

    run.assert_called_once()
    parse.assert_called_once_with("raw rows")


def test_search_uses_the_facades_current_builder_and_api_reader():
    query = "repo:o/r is:pr is:open author:octo"
    rows = [{"number": 7}]
    with (
        mock.patch.object(gh, "build_pr_search_query", return_value=query) as build,
        mock.patch.object(gh, "_run_gh_api", return_value=rows) as run_api,
    ):
        assert gh.search_pulls("o", "r", author="octo", limit=1) == rows

    build.assert_called_once_with(
        "o",
        "r",
        state="open",
        author="octo",
        assignee=None,
        review_requested=None,
    )
    assert run_api.call_args.args[1] is gh._PR_SEARCH_JQ
    assert run_api.call_args.kwargs == {
        "timeout": gh.GH_PAGINATE_TIMEOUT_SEC,
        "paginate": False,
    }
