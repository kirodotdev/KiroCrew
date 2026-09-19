"""Project Git sandbox failures remain explicit at the store and HTTP boundaries."""

import sys
from unittest.mock import Mock

import project_git_helpers
import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from test_project_review import _app, _bundle, _git, _registry

from kiro_crew import project_git, sandbox
from kiro_crew.dashboard import handlers_project
from kiro_crew.project_git import GitProjectStore, ProjectSandboxUnavailableError


@pytest.fixture
def denied_sandbox(monkeypatch):
    monkeypatch.setattr(sandbox, "enforcing_backend_available", lambda: False)
    monkeypatch.setattr(sandbox, "unavailable_reason", lambda: "unshare: EPERM")
    spawn = Mock(side_effect=AssertionError("Git must not spawn without its sandbox"))
    monkeypatch.setattr(project_git, "sandboxed_spawn_argv", spawn)
    monkeypatch.setattr(project_git, "run_limited", spawn)
    return spawn


@pytest.mark.parametrize("helper_only", [False, True])
def test_git_refuses_before_spawning(tmp_path, denied_sandbox, helper_only):
    with pytest.raises(ProjectSandboxUnavailableError) as caught:
        if helper_only:
            GitProjectStore._credential_helper_env()
        else:
            GitProjectStore._run_git(tmp_path, "status")
    assert caught.value.code == "project_sandbox_unavailable"
    assert "has no sandbox backend it can enforce." in str(caught.value)
    assert str(caught.value).endswith("unshare: EPERM")
    denied_sandbox.assert_not_called()


def test_sandbox_failure_without_probe_detail(tmp_path, monkeypatch, denied_sandbox):
    monkeypatch.setattr(sandbox, "unavailable_reason", lambda: "")
    with pytest.raises(ProjectSandboxUnavailableError) as caught:
        GitProjectStore._run_git(tmp_path, "status")
    assert str(caught.value) == (
        "Project git operations run inside the Kiro Crew sandbox, and this host "
        "has no sandbox backend it can enforce."
    )
    denied_sandbox.assert_not_called()


def test_spawn_sandbox_failure_keeps_its_error_code(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "enforcing_backend_available", lambda: True)
    monkeypatch.setattr(sandbox, "unavailable_reason", lambda: "unshare: EPERM")
    monkeypatch.setattr(GitProjectStore, "_credential_helper_env", lambda: {})
    spawn = Mock(
        side_effect=sandbox.SandboxUnavailableError(
            "sandbox denied", "no_backend", "unshare: EPERM"
        )
    )
    monkeypatch.setattr(project_git, "sandboxed_spawn_argv", spawn)
    with pytest.raises(ProjectSandboxUnavailableError) as caught:
        GitProjectStore._run_git(tmp_path, "status")
    assert caught.value.code == "project_sandbox_unavailable"
    assert str(caught.value).endswith("unshare: EPERM")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["url-add", "sync", "review"])
async def test_sandbox_denied_operations_return_503(tmp_path, denied_sandbox, operation):
    bundle = _bundle(tmp_path)
    registry = _registry(tmp_path)
    if operation == "review":
        manifest = yaml.safe_load((bundle / "project.yaml").read_text())
        manifest["sources"] = [
            {"type": "repo", "url": "https://example.com/source.git", "role": "primary"}
        ]
        (bundle / "project.yaml").write_text(yaml.safe_dump(manifest))
    if operation == "sync":
        bundle = _bundle(registry.projects_dir / "managed" / "test-clone")
        _git(bundle, "init", "-b", "main")
        project = registry.add_managed(
            bundle, remote="https://example.com/bundle.git", default_branch="main"
        )
    elif operation == "review":
        project = registry.add_local(bundle)
    state = _make_state(tmp_path / "sessions")
    app = _app(state, registry)
    app.router.add_post("/api/project-bundles/{id}/sync", handlers_project.api_project_sync)
    async with TestClient(TestServer(app)) as client:
        if operation == "url-add":
            response = await client.post(
                "/api/project-bundles/add", json={"source": "https://example.com/bundle.git"}
            )
        elif operation == "sync":
            response = await client.post(f"/api/project-bundles/{project.id}/sync")
        else:
            # Accepting the manifest is what authorizes the clone, so the refusal
            # lands on the digest the owner actually accepted -- a mismatched one
            # would be refused before any Git operation was attempted.
            shown = await (await client.get(f"/api/project-bundles/{project.id}/review")).json()
            response = await client.post(
                f"/api/project-bundles/{project.id}/review", json={"digest": shown["digest"]}
            )
        assert response.status == 503
        assert await response.json() == {
            "code": "project_sandbox_unavailable",
            "error": (
                "Project git operations run inside the Kiro Crew sandbox, and this host "
                "has no sandbox backend it can enforce. unshare: EPERM"
            ),
        }
    denied_sandbox.assert_not_called()


@pytest.mark.asyncio
async def test_a_local_add_declaring_sources_needs_no_sandbox(tmp_path, denied_sandbox):
    """Adding a Project never fetches, so a host with no sandbox can still add one.

    Cloning a declared source is the operation that needs the sandbox, and it
    runs only on the owner's acceptance, never at add.
    """
    bundle = _bundle(tmp_path)
    manifest = yaml.safe_load((bundle / "project.yaml").read_text())
    manifest["sources"] = [
        {"type": "repo", "url": "https://example.com/source.git", "role": "primary"}
    ]
    (bundle / "project.yaml").write_text(yaml.safe_dump(manifest))
    registry = _registry(tmp_path)
    state = _make_state(tmp_path / "sessions")

    async with TestClient(TestServer(_app(state, registry))) as client:
        response = await client.post("/api/project-bundles/add", json={"source": str(bundle)})

        assert response.status == 201
        payload = await response.json()

    assert [source["status"] for source in payload["sources"]] == ["pending"]
    assert payload["health"]["code"] == "project_review_stale"
    denied_sandbox.assert_not_called()


@pytest.mark.parametrize("helper_only", [False, True])
def test_enforcing_backend_allows_git(tmp_path, monkeypatch, helper_only):
    monkeypatch.setattr(sandbox, "enforcing_backend_available", lambda: True)
    monkeypatch.setattr(GitProjectStore, "_git_executable", lambda: "/trusted/git")
    monkeypatch.setattr(project_git, "git_command_env", lambda: {"GIT_CONFIG_COUNT": "0"})
    spawn = Mock(side_effect=lambda argv, **kwargs: (argv, kwargs["env"], None))
    monkeypatch.setattr(project_git, "sandboxed_spawn_argv", spawn)
    result = Mock(returncode=0, stdout="")
    run = Mock(return_value=result)
    monkeypatch.setattr(project_git, "run_limited", run)
    if helper_only:
        assert GitProjectStore._credential_helper_env() == {"GIT_CONFIG_COUNT": "0"}
    else:
        assert GitProjectStore._run_git(tmp_path, "status") is result
        assert run.call_args.args[0] == ["/trusted/git", "status"]
    assert spawn.call_count == run.call_count == (2 if helper_only else 3)


@pytest.mark.parametrize("remote", [r"C:\repos\bundle", "C:/repos/bundle", "z:/bundle"])
def test_drive_letter_remote_is_invalid(remote):
    with pytest.raises(project_git.ProjectGitError, match="unsupported Git remote protocol"):
        GitProjectStore._validate_remote(remote)


@pytest.mark.parametrize(
    "remote", ["git@example.com:bundle.git", "example.com:/repos/bundle", "git@x:/bundle", "x:repo"]
)
def test_scp_remote_is_valid(remote):
    assert GitProjectStore._validate_remote(remote) == remote


class TestTheLocalRemoteTestGate:
    """The suite's own gate for tests that can only build the remote above.

    ``test_drive_letter_remote_is_invalid`` is the design decision; the tests that
    build a remote out of ``tmp_path`` cannot express one anywhere that decision
    refuses, so they are skipped there through
    ``project_git_helpers.requires_local_git_remote``. These pin the gate itself,
    so the skip stays tied to the platform rather than to a hand-maintained list.
    """

    @pytest.mark.parametrize(
        ("platform", "supported"),
        [("win32", False), ("linux", True), ("darwin", True)],
    )
    def test_the_gate_reads_the_running_platform(self, monkeypatch, platform, supported):
        monkeypatch.setattr(sys, "platform", platform)

        assert project_git_helpers.local_git_remotes_supported() is supported

    def test_an_unsupported_platform_skips_and_names_the_reason(self, monkeypatch):
        monkeypatch.setattr(project_git_helpers, "local_git_remotes_supported", lambda: False)

        with pytest.raises(pytest.skip.Exception, match="enforcing sandbox backend"):
            project_git_helpers.skip_unless_local_git_remotes()

    def test_a_supported_platform_runs_the_test(self, monkeypatch):
        monkeypatch.setattr(project_git_helpers, "local_git_remotes_supported", lambda: True)

        assert project_git_helpers.skip_unless_local_git_remotes() is None

    def test_the_marker_requests_the_gating_fixture(self):
        mark = project_git_helpers.requires_local_git_remote.mark

        assert mark.name == "usefixtures"
        assert mark.args == ("local_git_remote",)
