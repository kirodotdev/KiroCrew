"""The Project-bundle HTTP surface, with the Git store faked at its own boundary.

Every handler here is reachable without a sandbox: the only thing the endpoints
need a sandbox for is the clone, and that is one collaborator
(``GitProjectStore``) whose failures the handlers are supposed to TRANSLATE --
into 503 ``project_sandbox_unavailable``, 409 ``project_not_syncable``, 400
``project_add_failed`` and so on. Faking that collaborator is what lets the
translation table be tested on a host with no namespace sandbox, which is every
CI shard.

The registry, the manifest reader, the review digest and the payload builder are
all real, so nothing here bypasses the hardened registry read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import handlers_project
from kiro_crew.project_git import (
    ProjectCheckoutDivergedError,
    ProjectGitError,
    ProjectSandboxUnavailableError,
    ProjectSyncResult,
)
from kiro_crew.project_registry import ProjectRegistry, ProjectRegistryError
from kiro_crew.project_review import MCP_SETTINGS_RELPATH, compute_review_digest

_MISSING_ID = "018f4f4a-760f-7a8b-a5d4-5a7e0f130d4e"


def _registry(tmp_path: Path) -> ProjectRegistry:
    return ProjectRegistry(
        projects_dir=tmp_path / "projects",
        registry_dir=tmp_path / "projects-registry",
    )


def _bundle(root: Path, name: str = "Payments", sources: list[dict] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "crew.kiro/v1",
                "kind": "Project",
                "name": name,
                "description": "The payments context.",
                "sources": sources or [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return root


def _reviewed(registry: ProjectRegistry, bundle: Path):
    project = registry.add_local(bundle)
    digest, hashes = compute_review_digest(bundle, bundle)
    return registry.record_review(project.id, digest, hashes)


class FakeStore:
    """Stand in for ``GitProjectStore`` at the handler boundary.

    Class-level hooks so a test can choose one behaviour without threading a
    factory through the app; reset by the autouse fixture below.
    """

    add_result: Any = None
    add_error: Exception | None = None
    sync_result: Any = None
    sync_error: Exception | None = None
    materialize_error: Exception | None = None
    remove_error: Exception | None = None
    cleanup_pending: list[str] | None = None
    on_remove: Any = None
    calls: list[str] = []

    def __init__(self, registry: ProjectRegistry) -> None:
        self.registry = registry

    def materialize_registered_sources(self, project) -> dict[str, str]:
        type(self).calls.append("materialize")
        if self.materialize_error is not None:
            raise self.materialize_error
        return {}

    def add(self, source: str):
        type(self).calls.append(f"add:{source}")
        if self.add_error is not None:
            raise self.add_error
        return self.add_result

    def sync(self, identifier: str):
        type(self).calls.append(f"sync:{identifier}")
        if self.sync_error is not None:
            raise self.sync_error
        return self.sync_result, ProjectSyncResult()

    def remove_derived_state(self, project_id: str) -> list[str]:
        type(self).calls.append(f"remove:{project_id}")
        # Read off the CLASS, not the instance: a plain function reached through an
        # instance would bind as a method and be handed a `self` it never declared.
        hook = type(self).on_remove
        if hook is not None:
            hook()
        if self.remove_error is not None:
            raise self.remove_error
        return list(self.cleanup_pending or [])


@pytest.fixture(autouse=True)
def _fake_store(monkeypatch: pytest.MonkeyPatch) -> type[FakeStore]:
    for field in (
        "add_result",
        "add_error",
        "sync_result",
        "sync_error",
        "materialize_error",
        "remove_error",
        "on_remove",
        "cleanup_pending",
    ):
        setattr(FakeStore, field, None)
    FakeStore.calls = []
    monkeypatch.setattr(handlers_project, "GitProjectStore", FakeStore)
    return FakeStore


@pytest.fixture
def audit() -> MagicMock:
    """A recording SEL double, so the audit branches are observable."""
    return MagicMock()


@pytest.fixture(autouse=True)
def _sel(monkeypatch: pytest.MonkeyPatch, audit: MagicMock) -> None:
    monkeypatch.setattr(handlers_project, "_sel", lambda: audit)


def _app(state, registry: ProjectRegistry, *, owner: bool = True) -> web.Application:
    @web.middleware
    async def identity(request: web.Request, handler):
        # An empty app claim plus a user is the owner dashboard; an app claim is
        # an installed app, which every Project route refuses.
        request["app"] = "" if owner else "some-app"
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = state
    services = handlers_project._ProjectServices()
    services._registry = registry
    app[handlers_project.PROJECT_SERVICES_KEY] = services
    app.router.add_get("/api/project-bundles", handlers_project.api_projects_list)
    app.router.add_get("/api/project-bundles/{id}", handlers_project.api_project_get)
    app.router.add_post("/api/project-bundles", handlers_project.api_project_create)
    app.router.add_post("/api/project-bundles/add", handlers_project.api_project_add)
    app.router.add_post("/api/project-bundles/{id}/sync", handlers_project.api_project_sync)
    app.router.add_get(
        "/api/project-bundles/{id}/review", handlers_project.api_project_review_preview
    )
    app.router.add_post("/api/project-bundles/{id}/review", handlers_project.api_project_review)
    app.router.add_delete("/api/project-bundles/{id}", handlers_project.api_project_remove)
    return app


def _client(tmp_path: Path, registry: ProjectRegistry, **kwargs) -> TestClient:
    return TestClient(TestServer(_app(_make_state(tmp_path / "sessions"), registry, **kwargs)))


class TestOwnerAuthorization:
    @pytest.mark.asyncio
    async def test_an_installed_app_is_refused(self, tmp_path: Path) -> None:
        async with _client(tmp_path, _registry(tmp_path), owner=False) as client:
            response = await client.get("/api/project-bundles")

            assert response.status == 403
            assert (await response.json())["code"] == "owner_only"

    @pytest.mark.asyncio
    async def test_an_unwritable_audit_refuses_an_authorized_call(
        self, tmp_path: Path, audit: MagicMock
    ) -> None:
        # Fail closed: an owner action whose audit cannot be written must not
        # proceed unrecorded.
        audit.log_api_access.side_effect = OSError("audit chain unwritable")

        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.get("/api/project-bundles")

            assert response.status == 503
            assert (await response.json())["code"] == "project_audit_unavailable"

    @pytest.mark.asyncio
    async def test_a_non_owner_audit_failure_still_refuses(
        self, tmp_path: Path, audit: MagicMock
    ) -> None:
        audit.log_api_access.side_effect = OSError("audit chain unwritable")

        async with _client(tmp_path, _registry(tmp_path), owner=False) as client:
            response = await client.get("/api/project-bundles")

            assert response.status == 403


class TestList:
    @pytest.mark.asyncio
    async def test_an_empty_install_lists_nothing(self, tmp_path: Path) -> None:
        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.get("/api/project-bundles")

            assert response.status == 200
            assert await response.json() == {"projects": []}

    @pytest.mark.asyncio
    async def test_a_registered_project_carries_its_manifest_and_health(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        _reviewed(registry, _bundle(tmp_path / "bundle"))

        async with _client(tmp_path, registry) as client:
            payload = await (await client.get("/api/project-bundles")).json()

        [project] = payload["projects"]
        assert project["name"] == "Payments"
        assert project["description"] == "The payments context."
        assert project["health"] == {"status": "healthy", "code": "project_healthy"}
        assert project["registrations"] == [
            {"origin": "local", "path": str(tmp_path / "bundle"), "syncable": False}
        ]
        assert "mcp" not in project and "memory" not in project

    @pytest.mark.asyncio
    async def test_an_unreadable_registry_is_a_500(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = _registry(tmp_path)
        monkeypatch.setattr(
            registry,
            "list_projects",
            lambda: (_ for _ in ()).throw(ProjectRegistryError("registry file is corrupt")),
        )

        async with _client(tmp_path, registry) as client:
            response = await client.get("/api/project-bundles")

            assert response.status == 500
            body = await response.json()
            assert body["code"] == "project_registry_invalid"
            assert "corrupt" in body["error"]


class TestPayloadHealth:
    @pytest.mark.asyncio
    async def test_a_project_whose_manifest_vanished_is_unavailable(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        bundle = _bundle(tmp_path / "bundle")
        project = registry.add_local(bundle)
        (bundle / "project.yaml").unlink()

        async with _client(tmp_path, registry) as client:
            payload = await (await client.get(f"/api/project-bundles/{project.id}")).json()

        assert payload["health"] == {
            "status": "unavailable",
            "code": "project_manifest_unavailable",
        }
        assert payload["sources"] == [] and payload["workspace_source"] == ""

    @pytest.mark.asyncio
    async def test_a_changed_executable_surface_is_review_stale(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        bundle = _bundle(tmp_path / "bundle")
        project = _reviewed(registry, bundle)
        (bundle / MCP_SETTINGS_RELPATH).parent.mkdir(parents=True, exist_ok=True)
        (bundle / MCP_SETTINGS_RELPATH).write_text(
            json.dumps({"mcpServers": {"x": {"command": "sh"}}}), encoding="utf-8"
        )

        async with _client(tmp_path, registry) as client:
            payload = await (await client.get(f"/api/project-bundles/{project.id}")).json()

        assert payload["health"]["code"] == "project_review_stale"
        assert payload["health"]["stale_files"] == [MCP_SETTINGS_RELPATH]

    @pytest.mark.asyncio
    async def test_an_unavailable_source_is_named_alongside_its_state(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        bundle = _bundle(
            tmp_path / "bundle",
            sources=[{"type": "repo", "url": "https://example.com/api", "role": "primary"}],
        )
        project = _reviewed(registry, bundle)

        async with _client(tmp_path, registry) as client:
            payload = await (await client.get(f"/api/project-bundles/{project.id}")).json()

        # No checkout was ever materialized, so nothing can start -- and that
        # outranks the digest, which could only be computed against a missing tree.
        assert payload["health"]["code"] == "project_sources_unavailable"
        assert payload["health"]["unavailable_sources"] == list(
            {source["id"] for source in payload["sources"]}
        )

    @pytest.mark.asyncio
    async def test_an_unknown_project_is_a_404(self, tmp_path: Path) -> None:
        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.get(f"/api/project-bundles/{_MISSING_ID}")

            assert response.status == 404
            assert (await response.json())["code"] == "project_not_found"


class TestSessionIndex:
    @pytest.mark.asyncio
    async def test_live_and_historical_sessions_are_both_listed(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        project = _reviewed(registry, _bundle(tmp_path / "bundle"))
        state = _make_state(tmp_path / "sessions")
        state.conversation_log.list_sessions = lambda: [
            {
                "key": "dashboard_chat-old",
                "title": "Historical",
                "messages": 4,
                "project_id": project.id,
            },
            # Filtered: incognito transcripts are never surfaced, and a session
            # with no Project or no key belongs to no Project list.
            {"key": "dashboard_hidden", "project_id": project.id, "memory_mode": "incognito"},
            {"key": "dashboard_other", "project_id": ""},
            {"key": "", "project_id": project.id},
        ]
        live = MagicMock(
            key="chat-live",
            display_title="Live",
            messages=["a"],
            running=True,
            project_id=project.id,
            memory_mode="",
        )
        hidden = MagicMock(key="chat-secret", project_id=project.id, memory_mode="incognito")
        unbound = MagicMock(key="chat-none", project_id="", memory_mode="")
        state._slots = {"chat-live": live, "chat-secret": hidden, "chat-none": unbound}

        async with TestClient(TestServer(_app(state, registry))) as client:
            payload = await (await client.get(f"/api/project-bundles/{project.id}")).json()

        assert [session["key"] for session in payload["sessions"]] == ["chat-live", "chat-old"]
        assert payload["sessions"][0] == {
            "key": "chat-live",
            "title": "Live",
            "messages": 1,
            "running": True,
            "live": True,
        }
        assert payload["sessions"][1]["messages"] == 4


class TestCreate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [{}, {"name": "x"}, {"path": "/tmp/x"}, {"name": "  ", "path": "/tmp/x"}, {"name": 1}],
    )
    async def test_an_incomplete_request_is_a_400(self, tmp_path: Path, body: dict) -> None:
        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.post("/api/project-bundles", json=body)

            assert response.status == 400
            assert (await response.json())["code"] == "project_invalid_request"

    @pytest.mark.asyncio
    async def test_a_non_object_body_is_a_400(self, tmp_path: Path) -> None:
        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.post(
                "/api/project-bundles", data="not json", headers={"Content-Type": "text/plain"}
            )

            assert response.status == 400

    @pytest.mark.asyncio
    async def test_a_created_bundle_is_registered_and_reviewed(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)

        async with _client(tmp_path, registry) as client:
            response = await client.post(
                "/api/project-bundles",
                json={"name": "Fresh", "path": str(tmp_path / "fresh")},
            )

            assert response.status == 201
            payload = await response.json()

        assert payload["name"] == "Fresh"
        assert (tmp_path / "fresh" / "project.yaml").exists()
        assert registry.get(payload["id"]).reviewed_digest
        # A created bundle declares no sources, so there is nothing to clone and
        # the Git store is never reached.
        assert FakeStore.calls == []

    @pytest.mark.asyncio
    async def test_a_created_bundle_needs_no_sandbox(self, tmp_path: Path) -> None:
        # Creation writes a manifest and registers it. Nothing here runs git, so a
        # host with no enforcing sandbox can still create a Project.
        FakeStore.materialize_error = ProjectSandboxUnavailableError()

        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.post(
                "/api/project-bundles", json={"name": "Fresh", "path": str(tmp_path / "fresh")}
            )

            assert response.status == 201
        assert FakeStore.calls == []

    @pytest.mark.asyncio
    async def test_a_source_that_cannot_be_cloned_still_registers(self, tmp_path: Path) -> None:
        # Reported through health, not fatal: the owner needs the registration to
        # exist before they can correct the URL and review it again.
        FakeStore.materialize_error = ProjectGitError("Git project operation failed")

        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.post(
                "/api/project-bundles", json={"name": "Fresh", "path": str(tmp_path / "fresh")}
            )

            assert response.status == 201

    @pytest.mark.asyncio
    async def test_an_unusable_path_is_a_400(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")

        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.post(
                "/api/project-bundles", json={"name": "Fresh", "path": str(blocker / "under")}
            )

            assert response.status == 400
            assert (await response.json())["code"] == "project_create_failed"


class TestAdd:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [{}, {"source": "   "}, {"source": 7}])
    async def test_an_incomplete_request_is_a_400(self, tmp_path: Path, body: dict) -> None:
        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.post("/api/project-bundles/add", json=body)

            assert response.status == 400
            assert (await response.json())["code"] == "project_invalid_request"

    @pytest.mark.asyncio
    async def test_an_existing_local_path_is_registered_without_the_git_store(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        bundle = _bundle(tmp_path / "bundle")

        async with _client(tmp_path, registry) as client:
            response = await client.post("/api/project-bundles/add", json={"source": str(bundle)})

            assert response.status == 201
            assert (await response.json())["name"] == "Payments"
        assert FakeStore.calls == []

    @pytest.mark.asyncio
    async def test_a_local_add_declaring_sources_clones_nothing(self, tmp_path: Path) -> None:
        # The sources are definitions the owner has not accepted, so add registers
        # and stops: no clone, and the rows come back pending.
        registry = _registry(tmp_path)
        bundle = _bundle(
            tmp_path / "bundle",
            sources=[{"type": "repo", "url": "https://example.com/api", "role": "primary"}],
        )

        async with _client(tmp_path, registry) as client:
            payload = await (
                await client.post("/api/project-bundles/add", json={"source": str(bundle)})
            ).json()

        assert FakeStore.calls == []
        assert not registry.get(payload["id"]).reviewed_digest
        assert payload["health"]["code"] == "project_review_stale"
        assert payload["health"]["stale_files"] == ["project.yaml"]
        assert [source["status"] for source in payload["sources"]] == ["pending"]
        assert "unavailable_sources" not in payload["health"]

    @pytest.mark.asyncio
    async def test_a_url_is_handed_to_the_git_store(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        FakeStore.add_result = registry.add_local(_bundle(tmp_path / "bundle"))

        async with _client(tmp_path, registry) as client:
            response = await client.post(
                "/api/project-bundles/add", json={"source": " https://example.com/b.git "}
            )

            assert response.status == 201
        assert FakeStore.calls == ["add:https://example.com/b.git"]

    @pytest.mark.asyncio
    async def test_a_clone_failure_is_a_400(self, tmp_path: Path) -> None:
        FakeStore.add_error = ProjectGitError("Git project operation failed")

        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.post(
                "/api/project-bundles/add", json={"source": "https://example.com/b.git"}
            )

            assert response.status == 400
            assert (await response.json())["code"] == "project_add_failed"

    @pytest.mark.asyncio
    async def test_an_unenforced_sandbox_is_a_503(self, tmp_path: Path) -> None:
        FakeStore.add_error = ProjectSandboxUnavailableError()

        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.post(
                "/api/project-bundles/add", json={"source": "https://example.com/b.git"}
            )

            assert response.status == 503
            assert (await response.json())["code"] == "project_sandbox_unavailable"


class TestSync:
    @pytest.mark.asyncio
    async def test_a_synced_project_comes_back_with_its_payload(
        self, tmp_path: Path, audit: MagicMock
    ) -> None:
        registry = _registry(tmp_path)
        project = _reviewed(registry, _bundle(tmp_path / "bundle"))
        FakeStore.sync_result = project

        async with _client(tmp_path, registry) as client:
            response = await client.post(f"/api/project-bundles/{project.id}/sync")

            assert response.status == 200
            body = await response.json()
            assert body["id"] == project.id
            assert "unavailable_sources" not in body
        assert FakeStore.calls == [f"sync:{project.id}"]
        assert any(
            call.kwargs.get("operation") == "project_sync"
            for call in audit.log_api_access.call_args_list
        )

    @pytest.mark.asyncio
    async def test_a_failed_sync_audit_does_not_fail_the_sync(
        self, tmp_path: Path, audit: MagicMock
    ) -> None:
        registry = _registry(tmp_path)
        project = _reviewed(registry, _bundle(tmp_path / "bundle"))
        FakeStore.sync_result = project
        calls: list[str] = []

        def log_api_access(**kwargs):
            calls.append(str(kwargs.get("resources")))
            # Only the post-sync record, not the owner-authorization one that
            # shares this operation name and whose failure DOES refuse the call.
            if str(kwargs.get("resources", "")).startswith("project="):
                raise OSError("audit chain unwritable")

        audit.log_api_access.side_effect = log_api_access

        async with _client(tmp_path, registry) as client:
            response = await client.post(f"/api/project-bundles/{project.id}/sync")

            assert response.status == 200
        assert f"project={project.id}" in calls

    @pytest.mark.asyncio
    async def test_an_unknown_project_is_a_404(self, tmp_path: Path) -> None:
        FakeStore.sync_error = ProjectRegistryError("project not found")

        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.post(f"/api/project-bundles/{_MISSING_ID}/sync")

            assert response.status == 404
            assert (await response.json())["code"] == "project_not_found"

    @pytest.mark.asyncio
    async def test_a_local_only_project_is_a_409(self, tmp_path: Path) -> None:
        FakeStore.sync_error = ProjectGitError("Project p1 has no managed Git clone")

        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.post(f"/api/project-bundles/{_MISSING_ID}/sync")

            assert response.status == 409
            assert (await response.json())["code"] == "project_not_syncable"

    @pytest.mark.asyncio
    async def test_any_other_git_failure_is_a_400(self, tmp_path: Path) -> None:
        FakeStore.sync_error = ProjectGitError("Git project operation failed")

        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.post(f"/api/project-bundles/{_MISSING_ID}/sync")

            assert response.status == 400
            assert (await response.json())["code"] == "project_sync_failed"

    @pytest.mark.asyncio
    async def test_an_unenforced_sandbox_is_a_503(self, tmp_path: Path) -> None:
        FakeStore.sync_error = ProjectSandboxUnavailableError()

        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.post(f"/api/project-bundles/{_MISSING_ID}/sync")

            assert response.status == 503
            assert (await response.json())["code"] == "project_sandbox_unavailable"

    @pytest.mark.asyncio
    async def test_a_diverged_checkout_names_the_tree_and_the_state(self, tmp_path: Path) -> None:
        FakeStore.sync_error = ProjectCheckoutDivergedError("p-1", "api", "local-commits")

        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.post(f"/api/project-bundles/{_MISSING_ID}/sync")

            assert response.status == 409
            assert await response.json() == {
                "error": "project_checkout_diverged",
                "code": "project_checkout_diverged",
                "project_id": "p-1",
                "advanced": [],
                "diverged": [{"checkout": "api", "detail": "local-commits"}],
            }

    @pytest.mark.asyncio
    async def test_the_body_reports_what_advanced_beside_what_diverged(
        self, tmp_path: Path
    ) -> None:
        # The owner needs both halves: the checkouts that DID move (and stay
        # moved), and each one that refused with its own remedy.
        error = ProjectCheckoutDivergedError("p-1", "api", "dirty-tree")
        error.advanced = ["bundle", "docs"]
        error.diverged = [
            {"checkout": "api", "detail": "dirty-tree"},
            {"checkout": "infra", "detail": "unrelated-history"},
        ]
        FakeStore.sync_error = error

        async with _client(tmp_path, _registry(tmp_path)) as client:
            body = await (await client.post(f"/api/project-bundles/{_MISSING_ID}/sync")).json()

        assert body["advanced"] == ["bundle", "docs"]
        assert body["diverged"] == [
            {"checkout": "api", "detail": "dirty-tree"},
            {"checkout": "infra", "detail": "unrelated-history"},
        ]
        assert "checkout" not in body and "detail" not in body

    @pytest.mark.asyncio
    @pytest.mark.parametrize("detail", ["local-commits", "dirty-tree", "unrelated-history"])
    async def test_every_divergence_detail_reaches_the_dashboard(
        self, tmp_path: Path, detail: str
    ) -> None:
        FakeStore.sync_error = ProjectCheckoutDivergedError("p-1", "bundle", detail)

        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.post(f"/api/project-bundles/{_MISSING_ID}/sync")

            assert response.status == 409
            body = await response.json()
        assert body["diverged"] == [{"checkout": "bundle", "detail": detail}]
        assert "checkout" not in body and "detail" not in body

    @pytest.mark.asyncio
    async def test_divergence_is_not_collapsed_into_the_generic_sync_failure(
        self, tmp_path: Path
    ) -> None:
        # It is a ProjectGitError subclass, so the specific arm has to come first
        # or the owner gets `project_sync_failed` with no detail to act on.
        FakeStore.sync_error = ProjectCheckoutDivergedError("p-1", "bundle", "dirty-tree")

        async with _client(tmp_path, _registry(tmp_path)) as client:
            body = await (await client.post(f"/api/project-bundles/{_MISSING_ID}/sync")).json()

        assert body["code"] != "project_sync_failed"


class TestReview:
    @pytest.mark.asyncio
    async def test_a_preview_names_the_digest_acceptance_requires(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        bundle = _bundle(tmp_path / "bundle")
        project = registry.add_local(bundle)

        async with _client(tmp_path, registry) as client:
            preview = await (await client.get(f"/api/project-bundles/{project.id}/review")).json()

            response = await client.post(
                f"/api/project-bundles/{project.id}/review", json={"digest": preview["digest"]}
            )

            assert response.status == 200
        assert registry.get(project.id).reviewed_digest == preview["digest"]

    @pytest.mark.asyncio
    async def test_acceptance_materializes_the_sources_the_manifest_declares(
        self, tmp_path: Path
    ) -> None:
        # Stage one: accepting the manifest is what authorizes the clone, so the
        # store is reached only after the digest matched.
        registry = _registry(tmp_path)
        bundle = _bundle(
            tmp_path / "bundle",
            sources=[{"type": "repo", "url": "https://example.com/api", "role": "primary"}],
        )
        project = registry.add_local(bundle)

        async with _client(tmp_path, registry) as client:
            preview = await (await client.get(f"/api/project-bundles/{project.id}/review")).json()
            response = await client.post(
                f"/api/project-bundles/{project.id}/review", json={"digest": preview["digest"]}
            )

            assert response.status == 200
        # The manifest is shown for acceptance, because it names the hosts.
        assert [row["path"] for row in preview["files"]] == ["project.yaml"]
        assert registry.get(project.id).reviewed_digest == preview["digest"]
        # Two store calls: the unavailable-source retry (a no-op while nothing is
        # accepted yet) and the clone the acceptance authorized.
        assert FakeStore.calls == ["materialize", "materialize"]

    @pytest.mark.asyncio
    async def test_a_refused_digest_never_reaches_the_git_store(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        bundle = _bundle(
            tmp_path / "bundle",
            sources=[{"type": "repo", "url": "https://example.com/api", "role": "primary"}],
        )
        project = registry.add_local(bundle)

        async with _client(tmp_path, registry) as client:
            response = await client.post(
                f"/api/project-bundles/{project.id}/review", json={"digest": "sha256:stale"}
            )

            assert response.status == 409
        # No acceptance, so no clone: only the unavailable-source retry ran, and
        # that one has nothing accepted to materialize.
        assert FakeStore.calls == ["materialize"]
        assert not registry.get(project.id).reviewed_digest

    @pytest.mark.asyncio
    async def test_an_unknown_project_has_no_preview(self, tmp_path: Path) -> None:
        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.get(f"/api/project-bundles/{_MISSING_ID}/review")

            assert response.status == 404
            assert (await response.json())["code"] == "project_not_found"

    @pytest.mark.asyncio
    async def test_an_unreadable_checkout_is_a_409(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        bundle = _bundle(tmp_path / "bundle")
        project = registry.add_local(bundle)
        (bundle / "project.yaml").unlink()

        async with _client(tmp_path, registry) as client:
            response = await client.get(f"/api/project-bundles/{project.id}/review")

            assert response.status == 409
            assert (await response.json())["code"] == "project_manifest_invalid"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [{}, {"digest": ""}, {"digest": 7}])
    async def test_acceptance_needs_a_digest(self, tmp_path: Path, body: dict) -> None:
        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.post(f"/api/project-bundles/{_MISSING_ID}/review", json=body)

            assert response.status == 400
            assert (await response.json())["code"] == "project_invalid_request"

    @pytest.mark.asyncio
    async def test_a_stale_digest_is_refused_with_a_fresh_preview(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        project = registry.add_local(_bundle(tmp_path / "bundle"))

        async with _client(tmp_path, registry) as client:
            response = await client.post(
                f"/api/project-bundles/{project.id}/review", json={"digest": "sha256:stale"}
            )

            assert response.status == 409
            body = await response.json()
        assert body["code"] == "project_review_moved"
        assert body["preview"]["digest"] != "sha256:stale"

    @pytest.mark.asyncio
    async def test_acceptance_on_an_unknown_project_is_a_404(self, tmp_path: Path) -> None:
        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.post(
                f"/api/project-bundles/{_MISSING_ID}/review", json={"digest": "sha256:x"}
            )

            assert response.status == 404
            assert (await response.json())["code"] == "project_not_found"

    @pytest.mark.asyncio
    async def test_an_unavailable_source_is_retried_before_the_preview(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        bundle = _bundle(
            tmp_path / "bundle",
            sources=[{"type": "repo", "url": "https://example.com/api", "role": "primary"}],
        )
        project = registry.add_local(bundle)

        async with _client(tmp_path, registry) as client:
            response = await client.post(
                f"/api/project-bundles/{project.id}/review", json={"digest": "sha256:x"}
            )

            # Still unavailable after the retry, so the snapshot cannot be built.
            assert response.status == 409
        assert FakeStore.calls == ["materialize"]

    @pytest.mark.asyncio
    async def test_an_unenforced_sandbox_during_retry_is_a_503(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        bundle = _bundle(
            tmp_path / "bundle",
            sources=[{"type": "repo", "url": "https://example.com/api", "role": "primary"}],
        )
        project = registry.add_local(bundle)
        FakeStore.materialize_error = ProjectSandboxUnavailableError()

        async with _client(tmp_path, registry) as client:
            response = await client.post(
                f"/api/project-bundles/{project.id}/review", json={"digest": "sha256:x"}
            )

            assert response.status == 503
            assert (await response.json())["code"] == "project_sandbox_unavailable"


class TestRemove:
    """Removal is two operations, and each row of the matrix is pinned here.

    Forgetting the record and deleting the on-disk roots have different
    durability, so the response says which of them happened rather than
    collapsing both into one verdict.
    """

    @pytest.mark.asyncio
    async def test_a_removed_project_is_unregistered_with_its_derived_state(
        self, tmp_path: Path, audit: MagicMock
    ) -> None:
        registry = _registry(tmp_path)
        project = _reviewed(registry, _bundle(tmp_path / "bundle"))
        order: list[str] = []
        audit.log_governance_decision.side_effect = lambda **_kwargs: order.append("audit")
        FakeStore.on_remove = lambda: order.append("remove")

        async with _client(tmp_path, registry) as client:
            response = await client.delete(f"/api/project-bundles/{project.id}")

            assert response.status == 200
            assert await response.json() == {"ok": True, "id": project.id}
        assert FakeStore.calls == [f"remove:{project.id}"]
        assert registry.list_projects() == ()
        assert order == ["audit", "remove"]
        assert audit.log_governance_decision.call_args.kwargs["tool_name"] == "project_remove"

    @pytest.mark.asyncio
    async def test_an_unwritable_audit_removes_nothing(
        self, tmp_path: Path, audit: MagicMock
    ) -> None:
        registry = _registry(tmp_path)
        project = _reviewed(registry, _bundle(tmp_path / "bundle"))
        audit.log_governance_decision.side_effect = OSError("audit chain unwritable")

        async with _client(tmp_path, registry) as client:
            response = await client.delete(f"/api/project-bundles/{project.id}")

            assert response.status == 409
            assert await response.json() == {
                "error": "Project removal audit is unavailable",
                "code": "project_remove_failed",
                "detail": "audit-unwritable",
            }
        # Nothing was removed, so the owner can retry once SEL is writable again.
        assert [entry.id for entry in registry.list_projects()] == [project.id]
        assert FakeStore.calls == []

    @pytest.mark.asyncio
    async def test_cleanup_that_fails_reports_the_leftover_paths_on_a_200(
        self, tmp_path: Path, audit: MagicMock
    ) -> None:
        # Row three: the registration IS gone, so this is success with a warning.
        # A 409 here would tell the owner the Project survived when it did not.
        registry = _registry(tmp_path)
        project = _reviewed(registry, _bundle(tmp_path / "bundle"))
        FakeStore.cleanup_pending = [
            f"state/{project.id}/",
            f"managed/{project.id}/",
        ]

        async with _client(tmp_path, registry) as client:
            response = await client.delete(f"/api/project-bundles/{project.id}")

            assert response.status == 200
            assert await response.json() == {
                "ok": True,
                "id": project.id,
                "cleanup_pending": [f"state/{project.id}/", f"managed/{project.id}/"],
            }
        assert registry.list_projects() == ()
        assert audit.log_governance_decision.called

    @pytest.mark.asyncio
    async def test_a_failure_before_the_registration_is_forgotten_is_a_409(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Row two: unregister itself could not write, so the registration is
        # intact and the owner is told the removal did not happen.
        registry = _registry(tmp_path)
        project = _reviewed(registry, _bundle(tmp_path / "bundle"))
        monkeypatch.setattr(
            registry,
            "unregister",
            lambda _id: (_ for _ in ()).throw(OSError("registry is read-only")),
        )

        async with _client(tmp_path, registry) as client:
            response = await client.delete(f"/api/project-bundles/{project.id}")

            assert response.status == 409
            body = await response.json()
        assert body["code"] == "project_remove_failed"
        assert "read-only" in body["error"]
        assert [entry.id for entry in registry.list_projects()] == [project.id]
        assert FakeStore.calls == []

    @pytest.mark.asyncio
    async def test_an_unknown_project_is_a_404_and_is_never_audited(
        self, tmp_path: Path, audit: MagicMock
    ) -> None:
        async with _client(tmp_path, _registry(tmp_path)) as client:
            response = await client.delete(f"/api/project-bundles/{_MISSING_ID}")

            assert response.status == 404
            assert (await response.json())["code"] == "project_not_found"
        # No governance record of removing something that was never registered.
        assert not audit.log_governance_decision.called
        assert FakeStore.calls == []


class TestServiceHolder:
    def test_the_holder_is_installed_without_touching_storage(self) -> None:
        app = web.Application()

        handlers_project.install_project_services(app)

        assert isinstance(
            app[handlers_project.PROJECT_SERVICES_KEY], handlers_project._ProjectServices
        )

    @pytest.mark.asyncio
    async def test_the_registry_is_built_once_off_the_event_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built: list[int] = []
        registry = _registry(tmp_path)
        monkeypatch.setattr(
            handlers_project, "ProjectRegistry", lambda: (built.append(1), registry)[1]
        )
        app = web.Application()
        app["state"] = _make_state(tmp_path / "sessions")
        handlers_project.install_project_services(app)

        request = MagicMock()
        request.app = app

        assert await handlers_project.project_registry_for_request(request) is registry
        assert await handlers_project.project_registry_for_request(request) is registry
        assert built == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["primary", "reference"])
@pytest.mark.parametrize("stale", [False, True])
async def test_sync_reports_fetch_failure_even_when_the_cached_source_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str, stale: bool
) -> None:
    from test_project_git_unit import FakeGit

    from kiro_crew.project_git import GitProjectStore
    from kiro_crew.project_manifest import load_project_manifest

    registry = _registry(tmp_path)
    sources = [{"type": "repo", "url": "https://example.com/api", "role": role}]
    if role == "reference":
        sources.append({"type": "repo", "url": "https://example.com/main", "role": "primary"})
    bundle = _bundle(registry.projects_dir / "managed" / "bundle", sources=sources)
    project = registry.add_managed(
        bundle, remote="https://example.com/bundle", default_branch="main"
    )
    git = FakeGit(manifest=(bundle / "project.yaml").read_text(encoding="utf-8"))

    def run_git(cwd, *args):
        if args[0] == "fetch" and args[2] == "https://example.com/api":
            raise ProjectGitError("source fetch failed")
        return git(cwd, *args)

    monkeypatch.setattr(handlers_project, "GitProjectStore", GitProjectStore)
    monkeypatch.setattr(GitProjectStore, "_run_git", staticmethod(run_git))
    monkeypatch.setattr(GitProjectStore, "_assert_safe_checkout", staticmethod(lambda _p: None))
    store = GitProjectStore(registry)
    source = load_project_manifest(bundle).sources[0]
    checkout = store.materialize_source(project.id, source.id, source.config["url"])
    workspace = checkout
    if role == "reference":
        primary = load_project_manifest(bundle).sources[1]
        workspace = store.materialize_source(project.id, primary.id, primary.config["url"])
    digest, hashes = compute_review_digest(bundle, workspace)
    registry.record_review(project.id, digest, hashes)
    if stale:
        target = workspace / MCP_SETTINGS_RELPATH
        target.parent.mkdir(parents=True)
        target.write_text('{"mcpServers": {}}', encoding="utf-8")

    async with _client(tmp_path, registry) as client:
        response = await client.post(f"/api/project-bundles/{project.id}/sync")
        assert response.status == 200
        body = await response.json()

    assert body["unavailable_sources"] == [source.id]
    assert body["health"]["status"] == "sources_unavailable"
    assert body["health"]["unavailable_sources"] == [source.id]
    assert body["sources"][0]["status"] == "unavailable"
    assert bool(body["health"].get("stale_files")) == stale
    assert registry.get(project.id).reviewed_digest == digest
    assert checkout.is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["sync", "preview", "accept"])
async def test_invalid_manifest_returns_parser_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from test_project_git_unit import FakeGit

    from kiro_crew.project_git import GitProjectStore
    from kiro_crew.project_manifest import ProjectManifestError, load_project_manifest

    registry = _registry(tmp_path)
    bundle = _bundle(registry.projects_dir / "managed" / "bundle")
    project = registry.add_managed(
        bundle, remote="https://example.com/bundle", default_branch="main"
    )
    (bundle / "project.yaml").write_text("kind: NotAProject\n", encoding="utf-8")
    with pytest.raises(ProjectManifestError) as error:
        load_project_manifest(bundle)
    monkeypatch.setattr(handlers_project, "GitProjectStore", GitProjectStore)
    monkeypatch.setattr(GitProjectStore, "_run_git", staticmethod(FakeGit()))
    monkeypatch.setattr(GitProjectStore, "_assert_safe_checkout", staticmethod(lambda _p: None))

    async with _client(tmp_path, registry) as client:
        url = f"/api/project-bundles/{project.id}"
        if operation == "sync":
            response = await client.post(url + "/sync")
        elif operation == "preview":
            response = await client.get(url + "/review")
        else:
            response = await client.post(url + "/review", json={"digest": "shown"})
        assert response.status == 409
        assert await response.json() == {
            "error": "project_manifest_invalid",
            "code": "project_manifest_invalid",
            "project_id": project.id,
            "detail": str(error.value),
        }


@pytest.mark.asyncio
async def test_failed_accept_materialization_is_unavailable_then_retry_requires_fresh_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_project_git_unit import FakeGit

    from kiro_crew.project_git import GitProjectStore

    registry = _registry(tmp_path)
    bundle = _bundle(
        tmp_path / "bundle",
        sources=[{"type": "repo", "url": "https://example.com/api", "role": "primary"}],
    )
    git = FakeGit(clone_error=ProjectGitError("source unavailable"))

    def run_git(cwd, *args):
        result = git(cwd, *args)
        if args[0] == "clone":
            target = Path(args[-1]) / MCP_SETTINGS_RELPATH
            target.parent.mkdir(parents=True)
            target.write_text('{"mcpServers": {}}', encoding="utf-8")
        return result

    monkeypatch.setattr(handlers_project, "GitProjectStore", GitProjectStore)
    monkeypatch.setattr(GitProjectStore, "_run_git", staticmethod(run_git))
    monkeypatch.setattr(GitProjectStore, "_assert_safe_checkout", staticmethod(lambda _p: None))
    async with _client(tmp_path, registry) as client:
        added = await client.post("/api/project-bundles/add", json={"source": str(bundle)})
        payload = await added.json()
        assert payload["health"]["status"] == "review_stale"
        assert git.calls == []
        url = f"/api/project-bundles/{payload['id']}/review"
        preview = await (await client.get(url)).json()
        accepted = await client.post(url, json={"digest": preview["digest"]})
        assert accepted.status == 200
        failed = await accepted.json()
        assert failed["health"] == {
            "status": "sources_unavailable",
            "code": "project_sources_unavailable",
            "unavailable_sources": [payload["sources"][0]["id"]],
        }
        git.clone_error = None
        moved = await client.post(url, json={"digest": preview["digest"]})
        assert moved.status == 409
        fresh = (await moved.json())["preview"]
        assert fresh["files"][0]["path"] == MCP_SETTINGS_RELPATH
        final = await client.post(url, json={"digest": fresh["digest"]})
        assert final.status == 200
        assert (await final.json())["health"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_divergence_keeps_other_sources_fetch_failures(tmp_path: Path) -> None:
    result = ProjectSyncResult(
        advanced=["bundle"],
        diverged=[{"checkout": "api", "detail": "local-commits"}],
        failures={"docs": "fetch failed"},
    )
    with pytest.raises(ProjectCheckoutDivergedError) as error:
        result.raise_if_diverged(_MISSING_ID)
    FakeStore.sync_error = error.value
    async with _client(tmp_path, _registry(tmp_path)) as client:
        response = await client.post(f"/api/project-bundles/{_MISSING_ID}/sync")
        assert response.status == 409
        body = await response.json()
    assert body["unavailable_sources"] == ["docs"]
    assert body["advanced"] == ["bundle"]
    assert body["diverged"] == [{"checkout": "api", "detail": "local-commits"}]
    assert "checkout" not in body and "detail" not in body
