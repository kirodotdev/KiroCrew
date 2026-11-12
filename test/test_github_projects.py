"""Tests for GitHub Projects (Projects v2) task management."""

from __future__ import annotations

import argparse
import json
import subprocess
from unittest.mock import patch

import pytest

from kiro_crew import github_projects
from kiro_crew.cli_commands_agent import _project_tasks
from kiro_crew.mcp_tools import github_projects as mcp_gh_projects


def test_parse_repo_string() -> None:
    assert github_projects.parse_repo_string("owner/repo") == ("owner", "repo")
    assert github_projects.parse_repo_string("owner/repo.git") == ("owner", "repo")
    assert github_projects.parse_repo_string("https://github.com/my-org/my-repo.git") == (
        "my-org",
        "my-repo",
    )
    assert github_projects.parse_repo_string("https://github.com/my-org/my-repo") == (
        "my-org",
        "my-repo",
    )
    assert github_projects.parse_repo_string("git@github.com:my-org/my-repo.git") == (
        "my-org",
        "my-repo",
    )
    assert github_projects.parse_repo_string("ssh://git@github.com/my-org/my-repo.git") == (
        "my-org",
        "my-repo",
    )
    assert github_projects.parse_repo_string("") == ("", "")
    assert github_projects.parse_repo_string("invalid-url") == ("", "")


def test_detect_repo_context_success() -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout="https://github.com/test-owner/test-repo.git\n",
        )
        owner, repo = github_projects.detect_repo_context()
        assert owner == "test-owner"
        assert repo == "test-repo"


def test_run_graphql_query_success() -> None:
    mock_proc = subprocess.CompletedProcess(
        args=["gh"],
        returncode=0,
        stdout=json.dumps({"data": {"test": "ok"}}),
    )
    with patch("kiro_crew.github_runner.resolve_gh", return_value="/usr/bin/gh"):
        with patch("kiro_crew.github_runner.run_gh", return_value=mock_proc):
            res = github_projects.run_graphql_query("query { test }")
            assert res == {"test": "ok"}


def test_run_graphql_query_auth_error() -> None:
    mock_proc = subprocess.CompletedProcess(
        args=["gh"],
        returncode=1,
        stdout="",
        stderr="HTTP 401: Bad credentials",
    )
    with patch("kiro_crew.github_runner.resolve_gh", return_value="/usr/bin/gh"):
        with patch("kiro_crew.github_runner.run_gh", return_value=mock_proc):
            with pytest.raises(github_projects.GitHubAuthError):
                github_projects.run_graphql_query("query { test }")


def test_run_graphql_query_not_found_error() -> None:
    mock_proc = subprocess.CompletedProcess(
        args=["gh"],
        returncode=0,
        stdout=json.dumps(
            {
                "data": None,
                "errors": [{"message": "Could not resolve to a ProjectV2 with number 999"}],
            }
        ),
    )
    with patch("kiro_crew.github_runner.resolve_gh", return_value="/usr/bin/gh"):
        with patch("kiro_crew.github_runner.run_gh", return_value=mock_proc):
            with pytest.raises(github_projects.GitHubNotFoundError):
                github_projects.run_graphql_query("query { test }")


def test_resolve_project_by_number() -> None:
    mock_data = {
        "repositoryOwner": {
            "projectV2": {
                "id": "PVT_123",
                "number": 5,
                "title": "Roadmap",
                "closed": False,
            }
        }
    }
    with patch("kiro_crew.github_projects.run_graphql_query", return_value=mock_data):
        proj = github_projects.resolve_project(owner="my-org", project_number=5)
        assert proj.id == "PVT_123"
        assert proj.number == 5
        assert proj.title == "Roadmap"
        assert not proj.closed


def test_resolve_project_from_repo_link() -> None:
    mock_data = {
        "repository": {
            "projectsV2": {
                "nodes": [{"id": "PVT_REPO_1", "number": 1, "title": "Repo Board", "closed": False}]
            },
            "owner": {"projectsV2": {"nodes": []}},
        }
    }
    with patch("kiro_crew.github_projects.run_graphql_query", return_value=mock_data):
        proj = github_projects.resolve_project(owner="my-org", repo="my-repo")
        assert proj.id == "PVT_REPO_1"
        assert proj.number == 1
        assert proj.title == "Repo Board"


def test_resolve_project_from_owner_fallback() -> None:
    mock_data = {
        "repository": {
            "projectsV2": {"nodes": []},
            "owner": {
                "projectsV2": {
                    "nodes": [
                        {
                            "id": "PVT_OWNER_1",
                            "number": 2,
                            "title": "Owner Board",
                            "closed": False,
                        }
                    ]
                }
            },
        }
    }
    with patch("kiro_crew.github_projects.run_graphql_query", return_value=mock_data):
        proj = github_projects.resolve_project(owner="my-org", repo="my-repo")
        assert proj.id == "PVT_OWNER_1"
        assert proj.number == 2
        assert proj.title == "Owner Board"


def test_list_project_tasks() -> None:
    mock_items_data = {
        "node": {
            "id": "PVT_123",
            "items": {
                "nodes": [
                    {
                        "id": "PVTI_ITEM_1",
                        "type": "ISSUE",
                        "fieldValues": {
                            "nodes": [
                                {
                                    "field": {"name": "Status"},
                                    "name": "In Progress",
                                    "optionId": "opt_in_prog",
                                }
                            ]
                        },
                        "content": {
                            "title": "Task One",
                            "number": 101,
                            "url": "https://github.com/org/repo/issues/101",
                            "body": "Body one",
                            "repository": {"nameWithOwner": "org/repo"},
                        },
                    },
                    {
                        "id": "PVTI_ITEM_2",
                        "type": "DRAFT_ISSUE",
                        "fieldValues": {
                            "nodes": [
                                {
                                    "field": {"name": "Status"},
                                    "name": "Done",
                                    "optionId": "opt_done",
                                }
                            ]
                        },
                        "content": {
                            "title": "Task Two",
                            "body": "Body two",
                        },
                    },
                ]
            },
        }
    }

    with patch(
        "kiro_crew.github_projects.resolve_project",
        return_value=github_projects.GitHubProject(id="PVT_123", number=1, title="Test Board"),
    ):
        with patch(
            "kiro_crew.github_projects.run_graphql_query",
            return_value=mock_items_data,
        ):
            all_tasks = github_projects.list_project_tasks(owner="org", repo="repo")
            assert len(all_tasks) == 2
            assert all_tasks[0]["id"] == "PVTI_ITEM_1"
            assert all_tasks[0]["status"] == "In Progress"
            assert all_tasks[0]["number"] == 101

            done_tasks = github_projects.list_project_tasks(
                owner="org", repo="repo", status_filter="Done"
            )
            assert len(done_tasks) == 1
            assert done_tasks[0]["id"] == "PVTI_ITEM_2"
            assert done_tasks[0]["status"] == "Done"


def test_create_project_task_validation() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        github_projects.create_project_task(title="")


def test_create_project_task_success() -> None:
    mock_add_data = {
        "addProjectV2DraftIssue": {
            "projectItem": {
                "id": "PVTI_NEW_1",
            }
        }
    }
    mock_fields = [
        {
            "id": "F_STATUS",
            "name": "Status",
            "options": [
                {"id": "OPT_TODO", "name": "Todo"},
                {"id": "OPT_DONE", "name": "Done"},
            ],
        }
    ]

    with patch(
        "kiro_crew.github_projects.resolve_project",
        return_value=github_projects.GitHubProject(id="PVT_123", number=1, title="Test Board"),
    ):
        with patch(
            "kiro_crew.github_projects.get_project_fields",
            return_value=mock_fields,
        ):
            with patch(
                "kiro_crew.github_projects.run_graphql_query",
                return_value=mock_add_data,
            ) as mock_query:
                created = github_projects.create_project_task(
                    title="New Feature",
                    body="Details here",
                    owner="org",
                    repo="repo",
                    status="Todo",
                )
                assert created["id"] == "PVTI_NEW_1"
                assert created["title"] == "New Feature"
                assert created["status"] == "Todo"
                assert mock_query.call_count == 2


def test_complete_project_task_success() -> None:
    mock_item_data = {
        "node": {
            "id": "PVTI_TARGET",
            "project": {"id": "PVT_123", "number": 1, "title": "Board"},
        }
    }
    mock_fields = [
        {
            "id": "F_STATUS",
            "name": "Status",
            "options": [
                {"id": "OPT_TODO", "name": "Todo"},
                {"id": "OPT_DONE", "name": "Done"},
            ],
        }
    ]
    mock_update_data = {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_TARGET"}}}

    def _mock_query(query: str, variables=None, **kwargs):
        if "updateProjectV2ItemFieldValue" in query:
            return mock_update_data
        return mock_item_data

    with patch("kiro_crew.github_projects.get_project_fields", return_value=mock_fields):
        with patch("kiro_crew.github_projects.run_graphql_query", side_effect=_mock_query):
            res = github_projects.complete_project_task(task_id="PVTI_TARGET")
            assert res["id"] == "PVTI_TARGET"
            assert res["status"] == "Done"
            assert res["completed"] is True


def test_mcp_tools_handlers() -> None:
    mock_tasks = [
        {
            "id": "PVTI_1",
            "title": "Task 1",
            "status": "Todo",
            "type": "ISSUE",
            "number": 1,
            "url": "http://gh/1",
            "body": "",
            "repository": "o/r",
        }
    ]
    with patch("kiro_crew.github_projects.list_project_tasks", return_value=mock_tasks):
        out_raw = mcp_gh_projects.github_project_tasks_list(
            "github_project_tasks_list", {"owner": "o", "repo": "r"}
        )
        out = json.loads(out_raw)
        assert out["ok"] is True
        assert out["count"] == 1
        assert out["tasks"][0]["id"] == "PVTI_1"

    mock_created = {"id": "PVTI_2", "title": "New", "status": "Todo"}
    with patch("kiro_crew.github_projects.create_project_task", return_value=mock_created):
        out_raw = mcp_gh_projects.github_project_task_create(
            "github_project_task_create", {"title": "New"}
        )
        out = json.loads(out_raw)
        assert out["ok"] is True
        assert out["task"]["id"] == "PVTI_2"

    mock_completed = {"id": "PVTI_2", "status": "Done", "completed": True}
    with patch("kiro_crew.github_projects.complete_project_task", return_value=mock_completed):
        out_raw = mcp_gh_projects.github_project_task_complete(
            "github_project_task_complete", {"task_id": "PVTI_2"}
        )
        out = json.loads(out_raw)
        assert out["ok"] is True
        assert out["result"]["status"] == "Done"


def test_cli_project_tasks_list(capsys: pytest.CaptureFixture[str]) -> None:
    mock_tasks = [
        {
            "id": "PVTI_1",
            "title": "Bug Fix",
            "status": "Todo",
            "number": 42,
            "url": "https://github.com/o/r/issues/42",
        }
    ]
    with patch("kiro_crew.github_projects.list_project_tasks", return_value=mock_tasks):
        args = argparse.Namespace(
            project_tasks_action="list",
            owner="o",
            repo="r",
            project=None,
            status=None,
            limit=50,
            json=False,
        )
        _project_tasks(args)
        captured = capsys.readouterr()
        assert "[Todo] Bug Fix (#42, id: PVTI_1)" in captured.out
        assert "https://github.com/o/r/issues/42" in captured.out
