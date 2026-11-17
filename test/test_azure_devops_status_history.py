"""The Azure DevOps pull request watch reads each check by its newest status.

``pullRequestStatuses`` lists every status ever posted to the pull request,
across iterations, so a superseded post must not stay in the verdict. The rest of
the Azure provider is pinned in ``test_source_provider_pull_request_monitors.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew.monitoring.azure_devops_pull_request import AzureDevOpsPullRequestProvider

_TARGET = "https://dev.azure.com/acme/project/_git/widgets/pullrequest/9"


@pytest.fixture(autouse=True)
def _isolate_azure_cli_config_dirs(_floor_monkeypatch):
    """Provider tests must not inherit Azure CLI paths from the CI host."""
    _floor_monkeypatch.delenv("AZURE_CONFIG_DIR", raising=False)
    _floor_monkeypatch.delenv("AZURE_EXTENSION_DIR", raising=False)
    _floor_monkeypatch.delenv("KIROCREW_POD", raising=False)


@pytest.mark.parametrize(
    "posts",
    [
        # A build that failed on iteration 1 and passed on iteration 2.
        [("failed", 1, 1), ("succeeded", 2, 2)],
        # An external CI that posted pending, then succeeded, on one iteration.
        [("pending", 1, 3), ("succeeded", 1, 4)],
        # Listed newest first: order in the response does not decide.
        [("succeeded", 2, 6), ("failed", 1, 5)],
    ],
)
def test_azure_only_the_newest_status_per_context_counts(posts):
    context = {"name": "build", "genre": "ci"}
    payloads = {
        "pull_request": {
            "status": "active",
            "isDraft": False,
            "repository": {"name": "widgets", "project": {"name": "project"}},
            "lastMergeSourceCommit": {"commitId": "def"},
            "mergeStatus": "succeeded",
            "reviewers": [],
        },
        "statuses": {
            "value": [
                {"id": status_id, "iterationId": iteration, "context": context, "state": state}
                for state, iteration, status_id in posts
            ]
        },
        "threads": {"value": []},
        "policies": {"value": []},
    }
    provider = AzureDevOpsPullRequestProvider(fetch=lambda _target, resource: payloads[resource])

    result = provider.probe((_TARGET,))[_TARGET]

    assert result.observation.reason_code == "review_ready"
    assert len(result.canonical["checks"]["passed"]) == 1


def test_azure_a_long_history_is_folded_before_it_is_bounded():
    """A newer post past the first 100 still replaces the old one it supersedes."""
    context = {"name": "build", "genre": "ci"}
    history = [
        {"id": status_id, "iterationId": status_id, "context": context, "state": "failed"}
        for status_id in range(1, 120)
    ]
    history.append({"id": 120, "iterationId": 120, "context": context, "state": "succeeded"})
    payloads = {
        "pull_request": {
            "status": "active",
            "isDraft": False,
            "repository": {"name": "widgets", "project": {"name": "project"}},
            "lastMergeSourceCommit": {"commitId": "def"},
            "mergeStatus": "succeeded",
            "reviewers": [],
        },
        "statuses": {"value": history},
        "threads": {"value": []},
        "policies": {"value": []},
    }
    provider = AzureDevOpsPullRequestProvider(fetch=lambda _target, resource: payloads[resource])

    result = provider.probe((_TARGET,))[_TARGET]

    assert result.observation.reason_code == "review_ready"
    assert result.canonical["checks"]["failed"] == []
