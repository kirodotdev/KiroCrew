"""A failed GitLab comment read drops only the comment digest.

The comment digest has no bearing on review-readiness, so no failure of the notes
read may fail the whole probe. The rest of the GitLab provider is pinned in
``test_source_provider_pull_request_monitors.py``.
"""

from __future__ import annotations

from kiro_crew.github_runner import SetupError
from kiro_crew.monitoring.gitlab_merge_request import GitLabMergeRequestProvider
from kiro_crew.monitoring.models import MonitorObservationStatus

_TARGET = "https://gitlab.com/acme/widgets/-/merge_requests/8"


def test_gitlab_comment_read_setup_error_only_drops_the_digest():
    payloads = {
        "merge_request": {
            "state": "opened",
            "draft": False,
            "sha": "c0ffee",
            "detailed_merge_status": "mergeable",
            "head_pipeline": {"id": 1, "sha": "c0ffee", "status": "success"},
        },
        "discussions": [],
    }

    def fetch(_target, resource):
        if resource == "comments":
            raise SetupError("provider CLI pipes did not close")
        return payloads.get(resource, [])

    provider = GitLabMergeRequestProvider(gitlab_hosts=[], fetch=fetch)

    result = provider.probe((_TARGET,))[_TARGET]

    assert result.observation.status is MonitorObservationStatus.SUCCESS
    assert result.observation.reason_code == "review_ready"
