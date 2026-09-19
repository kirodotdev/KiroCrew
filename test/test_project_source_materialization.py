"""Declared ``type: repo`` sources are materialized, not just validated.

A Project is a Git repository the gateway materializes and keeps in sync, so a
manifest declaring ``sources: [{type: repo, url, role: primary}]`` must end up
with that repository cloned: it is the directory a session runs in AND the root
the review digest reads the executable surfaces from. Materialization is owner-initiated: a declared ``url`` is a definition, so it is
cloned on the acceptance of the manifest that names it and on a sync of an
already-accepted one -- never by the add itself. These cover the deferral, the
acceptance that clones, the re-clone when a declaration moves, the review gate
over the PRIMARY SOURCE checkout, and that an unreachable URL leaves the Project
registered rather than losing it.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from project_git_helpers import local_git_remote, requires_local_git_remote  # noqa: F401

from kiro_crew.dashboard import handlers_project
from kiro_crew.dashboard.chat import api_chat_slot_create
from kiro_crew.project_git import GitProjectStore
from kiro_crew.project_manifest import _synthesize_source_id, load_project_manifest
from kiro_crew.project_registry import ProjectRegistry
from kiro_crew.project_review import MCP_SETTINGS_RELPATH, compute_review_digest
from kiro_crew.project_sessions import (
    ProjectSessionError,
    describe_project_sources,
    pending_project_sources,
    resolve_project_attachment,
    review_stale_files,
)
from kiro_crew.sandbox import userns_available

_GIT_ENV_KEYS = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin",
}

_needs_git = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git not installed",
)

# Two independent capabilities, so two independent gates. The sandbox one is the
# repo's canonical spelling, which is what puts this file on the namespace-sandbox
# job's argv (test_coverage_omit_contract.py); the other is about whether a
# ``tmp_path`` remote can be a valid remote at all.
_needs_namespace_sandbox = pytest.mark.skipif(
    not userns_available(),
    reason="Project git operations require an enforcing namespace sandbox backend",
)

# Every remote in this module is a ``file://`` URL built from ``tmp_path``, and
# every test drives a real clone through the sandbox.
pytestmark = [requires_local_git_remote, _needs_namespace_sandbox]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**_GIT_ENV_KEYS, "HOME": str(cwd)},
    )


def _write(root: Path, relpath: str, text: str) -> None:
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _registry(tmp_path: Path) -> ProjectRegistry:
    return ProjectRegistry(
        projects_dir=tmp_path / "projects",
        registry_dir=tmp_path / "projects-registry",
    )


def _manifest_text(sources: list[dict]) -> str:
    return yaml.safe_dump(
        {
            "apiVersion": "crew.kiro/v1",
            "kind": "Project",
            "name": "Payments",
            "sources": sources,
        },
        sort_keys=False,
    )


def _source_repo(root: Path, name: str, marker: str) -> Path:
    """A plain git repository standing in for a declared source."""
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _write(repo, "MARKER.txt", marker)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def _bundle_repo(root: Path, sources: list[dict]) -> Path:
    """The Project bundle repository, carrying only project.yaml."""
    repo = root / "bundle-remote"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _write(repo, "project.yaml", _manifest_text(sources))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def _add(store: GitProjectStore, remote: Path):
    return store.add(f"file://{remote}")


def _accept(registry: ProjectRegistry, project_id: str, bundle_dir: Path, checkout: Path | None):
    """Record the acceptance the dashboard records when the owner clicks through."""
    digest, hashes = compute_review_digest(bundle_dir, checkout)
    return registry.record_review(project_id, digest, hashes)


def _add_and_accept(registry: ProjectRegistry, remote: Path):
    """Add a Project, then walk stage one of its review, exactly as the API does.

    Add clones the bundle and nothing else: a declared source is a definition the
    owner has not accepted. Accepting the manifest is what authorizes cloning the
    hosts it names, and materialization runs on that acceptance -- so a test about
    a materialized checkout starts from an accepted manifest.
    """
    store = GitProjectStore(registry)
    project = _add(store, remote)
    bundle_dir = project.registrations[-1].path
    accepted = _accept(registry, project.id, bundle_dir, None)
    store.materialize_registered_sources(accepted)
    return registry.get(project.id)


def _accept_current_state(registry: ProjectRegistry, project_id: str):
    """Accept whatever the Project's checkouts hold right now (stage two)."""
    project = registry.get(project_id)
    bundle_dir, primary, _unavailable = describe_project_sources(project, registry=registry)
    return _accept(registry, project_id, bundle_dir, primary)


def _materialize_accepted(registry: ProjectRegistry, project_id: str):
    """Accept the manifest as it now stands, then clone what it declares."""
    project = registry.get(project_id)
    bundle_dir = project.registrations[-1].path
    accepted = _accept(registry, project_id, bundle_dir, None)
    GitProjectStore(registry).materialize_registered_sources(accepted)
    return registry.get(project_id)


@_needs_git
class TestAcceptanceMaterializesDeclaredSources:
    def test_add_never_fetches_a_declared_source(self, tmp_path: Path) -> None:
        # The whole point of the deferral: the only remote add reaches is the
        # bundle URL the owner typed. A source url is a definition inside the
        # manifest, so a manifest cannot make add fetch a host of its choosing.
        api = _source_repo(tmp_path, "payments-api", "primary source")
        bundle = _bundle_repo(
            tmp_path, [{"type": "repo", "url": f"file://{api}", "role": "primary"}]
        )
        registry = _registry(tmp_path)
        store = GitProjectStore(registry)
        seen: list[tuple[str, ...]] = []
        original = store._run_git

        def _recording(cwd: Path, *args: str):
            seen.append(args)
            return original(cwd, *args)

        store._run_git = _recording  # type: ignore[method-assign]

        project = _add(store, bundle)

        assert seen, "the bundle clone itself must still run"
        assert not any(str(api) in " ".join(args) for args in seen)
        source_id = _synthesize_source_id(f"file://{api}")
        assert not (registry.projects_dir / "state" / project.id / "sources").exists()

        # And the Project says why: the manifest is unaccepted, so the source is
        # pending rather than broken, and no session can start on it.
        bundle_dir = project.registrations[-1].path
        assert review_stale_files(project, bundle_dir, None) == ("project.yaml",)
        assert pending_project_sources(project, bundle_dir, load_project_manifest(bundle_dir)) == (
            source_id,
        )
        with pytest.raises(ProjectSessionError) as exc_info:
            resolve_project_attachment(project.id, registry=registry)
        assert exc_info.value.code == "project_review_stale"

    def test_accepting_the_manifest_leaves_the_clone_itself_to_be_reviewed(
        self, tmp_path: Path
    ) -> None:
        # Two stages, and this is the seam between them. Stage one accepts the
        # manifest, which authorizes the clone; the clone's own `.kiro/` is a
        # surface nobody has seen yet, so the Project comes back review-stale on
        # that tree and a session still cannot start. Stage two accepts it.
        api = _source_repo(tmp_path, "payments-api", "primary source")
        _write(api, MCP_SETTINGS_RELPATH, json.dumps({"mcpServers": {"x": {"command": "sh"}}}))
        _git(api, "add", "-A")
        _git(api, "commit", "-m", "carry an mcp definition")
        bundle = _bundle_repo(
            tmp_path, [{"type": "repo", "url": f"file://{api}", "role": "primary"}]
        )
        registry = _registry(tmp_path)

        project = _add_and_accept(registry, bundle)

        bundle_dir, primary, unavailable = describe_project_sources(project, registry=registry)
        assert unavailable == ()
        assert review_stale_files(project, bundle_dir, primary) == (MCP_SETTINGS_RELPATH,)
        with pytest.raises(ProjectSessionError) as exc_info:
            resolve_project_attachment(project.id, registry=registry)
        assert exc_info.value.code == "project_review_stale"

        _accept_current_state(registry, project.id)

        assert resolve_project_attachment(project.id, registry=registry).workspace_dir == primary

    def test_a_primary_source_is_cloned_and_becomes_the_session_workspace(
        self, tmp_path: Path
    ) -> None:
        api = _source_repo(tmp_path, "payments-api", "primary source")
        bundle = _bundle_repo(
            tmp_path, [{"type": "repo", "url": f"file://{api}", "role": "primary"}]
        )
        registry = _registry(tmp_path)
        project = _add_and_accept(registry, bundle)
        source_id = _synthesize_source_id(f"file://{api}")

        # The accepted source was cloned, into its own derived tree.
        _bundle_dir, primary, unavailable = describe_project_sources(project, registry=registry)
        assert unavailable == ()
        assert primary is not None
        assert (primary / "MARKER.txt").read_text(encoding="utf-8") == "primary source"
        assert primary.name == source_id

        # And a session binds THAT directory, not the bundle clone.
        attachment = resolve_project_attachment(project.id, registry=registry)
        assert attachment.workspace_dir == primary
        assert attachment.workspace_dir != project.registrations[-1].path
        workspace_repo = next(repo for repo in attachment.repositories if repo.is_workspace)
        assert workspace_repo.source_id == source_id

    def test_a_lone_declared_source_is_cloned_and_becomes_the_workspace(
        self, tmp_path: Path
    ) -> None:
        docs = _source_repo(tmp_path, "payments-docs", "the only source")
        bundle = _bundle_repo(
            tmp_path, [{"type": "repo", "url": f"file://{docs}", "role": "reference"}]
        )
        registry = _registry(tmp_path)
        project = _add_and_accept(registry, bundle)
        _accept_current_state(registry, project.id)

        attachment = resolve_project_attachment(project.id, registry=registry)

        # The manifest rule: a single declared source is the workspace even
        # without an explicit `role: primary`. It must therefore be cloned, or
        # the session would have nowhere to run.
        workspace_repo = next(repo for repo in attachment.repositories if repo.is_workspace)
        assert workspace_repo.path == attachment.workspace_dir
        assert (attachment.workspace_dir / "MARKER.txt").read_text(
            encoding="utf-8"
        ) == "the only source"

    def test_the_sources_empty_flow_is_unchanged(self, tmp_path: Path) -> None:
        bundle = _bundle_repo(tmp_path, [])
        registry = _registry(tmp_path)
        project = _add(GitProjectStore(registry), bundle)
        bundle_clone = project.registrations[-1].path

        _bundle_dir, primary, unavailable = describe_project_sources(project, registry=registry)

        assert unavailable == ()
        # The bundle clone itself is the workspace when nothing is declared.
        assert primary == bundle_clone
        attachment = resolve_project_attachment(project.id, registry=registry)
        assert attachment.workspace_dir == bundle_clone
        assert attachment.repositories == ()

    def test_an_unreachable_source_leaves_the_project_registered(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        bundle = _bundle_repo(
            tmp_path, [{"type": "repo", "url": f"file://{missing}", "role": "primary"}]
        )
        registry = _registry(tmp_path)
        project = _add_and_accept(registry, bundle)
        source_id = _synthesize_source_id(f"file://{missing}")

        # Registered, so the owner can correct the URL and review it again.
        assert registry.get(project.id).id == project.id
        _bundle_dir, primary, unavailable = describe_project_sources(project, registry=registry)
        assert unavailable == (source_id,)
        assert primary is None
        # A session cannot start on it, and says why.
        with pytest.raises(ProjectSessionError) as exc_info:
            resolve_project_attachment(project.id, registry=registry)
        assert exc_info.value.code == "project_workspace_unavailable"


@_needs_git
class TestSyncMaterializesAndRepoints:
    def test_a_source_a_sync_pulled_in_waits_for_acceptance(self, tmp_path: Path) -> None:
        bundle_remote = _bundle_repo(tmp_path, [])
        registry = _registry(tmp_path)
        store = GitProjectStore(registry)
        project = _add(store, bundle_remote)
        assert describe_project_sources(project, registry=registry)[2] == ()

        api = _source_repo(tmp_path, "payments-api", "added upstream")
        _write(
            bundle_remote,
            "project.yaml",
            _manifest_text([{"type": "repo", "url": f"file://{api}", "role": "primary"}]),
        )
        _git(bundle_remote, "add", "-A")
        _git(bundle_remote, "commit", "-m", "declare a primary source")

        synced, _result = store.sync(project.id)

        # The pull landed a manifest naming a host nobody has accepted, so the
        # sync advances the bundle and stops: no clone, and the Project says so.
        bundle_dir = synced.registrations[-1].path
        source_id = _synthesize_source_id(f"file://{api}")
        assert not (registry.projects_dir / "state" / synced.id / "sources" / source_id).exists()
        assert review_stale_files(synced, bundle_dir, bundle_dir) == ("project.yaml",)

        accepted = _materialize_accepted(registry, synced.id)

        _bundle_dir, primary, unavailable = describe_project_sources(accepted, registry=registry)
        assert unavailable == ()
        assert (primary / "MARKER.txt").read_text(encoding="utf-8") == "added upstream"

    def test_sync_repoints_a_source_url_and_re_clones(self, tmp_path: Path) -> None:
        before = _source_repo(tmp_path, "before", "before")
        after = _source_repo(tmp_path, "after", "after")
        bundle_remote = _bundle_repo(
            tmp_path, [{"type": "repo", "url": f"file://{before}", "role": "primary"}]
        )
        registry = _registry(tmp_path)
        store = GitProjectStore(registry)
        project = _add_and_accept(registry, bundle_remote)
        first = describe_project_sources(project, registry=registry)[1]
        assert (first / "MARKER.txt").read_text(encoding="utf-8") == "before"

        _write(
            bundle_remote,
            "project.yaml",
            _manifest_text([{"type": "repo", "url": f"file://{after}", "role": "primary"}]),
        )
        _git(bundle_remote, "add", "-A")
        _git(bundle_remote, "commit", "-m", "repoint the primary source")

        store.sync(project.id)
        # Repointing a source is exactly the edit that aims the fetch somewhere
        # new, so it is unaccepted until the owner accepts the new manifest.
        synced = _materialize_accepted(registry, project.id)

        _bundle_dir, second, unavailable = describe_project_sources(synced, registry=registry)
        assert unavailable == ()
        # A new source id (the id is derived from the URL), holding the new tree,
        # and the OLD checkout survived its replacement rather than being
        # discarded before the new provenance record existed.
        assert second != first
        assert (second / "MARKER.txt").read_text(encoding="utf-8") == "after"
        assert (first / "MARKER.txt").read_text(encoding="utf-8") == "before"

    def test_an_unchanged_declaration_reuses_its_checkout_in_place(self, tmp_path: Path) -> None:
        # The provenance record ties a tree to a declaration. An unchanged
        # declaration keeps the SAME derived directory across syncs -- the tree is
        # fast-forwarded, never re-cloned somewhere else, so a session's cwd does
        # not move under it when nothing about the source changed.
        api = _source_repo(tmp_path, "payments-api", "first")
        bundle_remote = _bundle_repo(
            tmp_path, [{"type": "repo", "url": f"file://{api}", "role": "primary"}]
        )
        registry = _registry(tmp_path)
        store = GitProjectStore(registry)
        project = _add_and_accept(registry, bundle_remote)
        first = describe_project_sources(project, registry=registry)[1]

        _write(api, "MARKER.txt", "second")
        _git(api, "add", "-A")
        _git(api, "commit", "-m", "advance the source")
        synced, _result = store.sync(project.id)

        second = describe_project_sources(synced, registry=registry)[1]
        assert second == first
        # And the fast-forward actually landed: syncing a source is a fetch of the
        # pinned remote plus a fast-forward, not a no-op reuse.
        assert (second / "MARKER.txt").read_text(encoding="utf-8") == "second"


@_needs_git
class TestAReferenceCheckoutIsNeverTheSessionCwd:
    """The invariant the digest's scope depends on.

    The digest stops at the primary checkout because that is the only directory a
    Project session ever runs in. ``reference`` sources are materialized beside
    it and are never a kiro-cli workspace root, so kiro-cli never reads their
    ``.kiro/``. These test the invariant rather than assume it.
    """

    @staticmethod
    def _project(tmp_path: Path):
        api = _source_repo(tmp_path, "payments-api", "primary source")
        infra = _source_repo(tmp_path, "payments-infra", "reference source")
        bundle_remote = _bundle_repo(
            tmp_path,
            [
                {"type": "repo", "url": f"file://{api}", "role": "primary"},
                {"type": "repo", "url": f"file://{infra}", "role": "reference"},
            ],
        )
        registry = _registry(tmp_path)
        project = _add_and_accept(registry, bundle_remote)
        return registry, project, api, infra

    def test_the_reference_checkout_is_materialized_but_is_not_the_workspace(
        self, tmp_path: Path
    ) -> None:
        registry, project, api, infra = self._project(tmp_path)
        primary_id = _synthesize_source_id(f"file://{api}")
        reference_id = _synthesize_source_id(f"file://{infra}")

        attachment = resolve_project_attachment(project.id, registry=registry)
        by_id = {repo.source_id: repo for repo in attachment.repositories}

        # Both were cloned...
        assert by_id[reference_id].path is not None
        assert (by_id[reference_id].path / "MARKER.txt").read_text(
            encoding="utf-8"
        ) == "reference source"
        # ...but only the primary is the session's directory.
        assert by_id[primary_id].is_workspace is True
        assert by_id[reference_id].is_workspace is False
        assert attachment.workspace_dir == by_id[primary_id].path
        assert attachment.workspace_dir != by_id[reference_id].path

    def test_the_digest_root_is_the_session_directory(self, tmp_path: Path) -> None:
        registry, project, _api, _infra = self._project(tmp_path)

        _bundle_dir, digest_root, _unavailable = describe_project_sources(
            project, registry=registry
        )
        session_dir = resolve_project_attachment(project.id, registry=registry).workspace_dir

        assert digest_root == session_dir

    @pytest.mark.asyncio
    async def test_a_created_session_binds_the_primary_not_the_reference(
        self, tmp_path: Path
    ) -> None:
        registry, project, api, infra = await asyncio.to_thread(self._project, tmp_path)
        bundle_dir, primary, _unavailable = describe_project_sources(project, registry=registry)
        digest, hashes = compute_review_digest(bundle_dir, primary)
        registry.record_review(project.id, digest, hashes)
        reference = (
            registry.projects_dir
            / "state"
            / project.id
            / "sources"
            / _synthesize_source_id(f"file://{infra}")
        )
        state = _make_state(tmp_path / "sessions")

        async with TestClient(TestServer(_app(state, registry))) as client:
            response = await client.post(
                "/api/chat/slots", json={"name": "ref-chat", "project_id": project.id}
            )
            assert response.status == 200

        slot_dir = Path(state._slots["ref-chat"].project)
        assert slot_dir == primary
        assert slot_dir != reference
        assert (slot_dir / "MARKER.txt").read_text(encoding="utf-8") == "primary source"

    def test_a_kiro_surface_in_a_reference_checkout_does_not_mark_the_project_stale(
        self, tmp_path: Path
    ) -> None:
        # The other half of the invariant: since a reference tree is never a cwd,
        # its .kiro/ is out of scope, and planting one there must NOT refuse the
        # session. If reference trees ever become session cwds, this test is the
        # one that has to change, deliberately.
        registry, project, _api, infra = self._project(tmp_path)
        bundle_dir, primary, _unavailable = describe_project_sources(project, registry=registry)
        digest, hashes = compute_review_digest(bundle_dir, primary)
        registry.record_review(project.id, digest, hashes)
        reference = (
            registry.projects_dir
            / "state"
            / project.id
            / "sources"
            / _synthesize_source_id(f"file://{infra}")
        )

        _write(
            reference, MCP_SETTINGS_RELPATH, json.dumps({"mcpServers": {"x": {"command": "sh"}}})
        )

        assert review_stale_files(registry.get(project.id), bundle_dir, primary) == ()
        assert resolve_project_attachment(project.id, registry=registry).workspace_dir == primary


@_needs_git
def test_a_pushed_mcp_settings_in_the_primary_source_marks_the_project_review_stale(
    tmp_path: Path,
) -> None:
    # The seam PR A's exit criteria promise to close: the executable surfaces
    # that matter live in the PRIMARY SOURCE checkout, which is the cwd a session
    # runs in -- not in the bundle repo.
    api = _source_repo(tmp_path, "payments-api", "primary source")
    bundle_remote = _bundle_repo(
        tmp_path, [{"type": "repo", "url": f"file://{api}", "role": "primary"}]
    )
    registry = _registry(tmp_path)
    store = GitProjectStore(registry)
    project = _add_and_accept(registry, bundle_remote)

    bundle_dir, primary, _unavailable = describe_project_sources(project, registry=registry)
    assert primary is not None
    digest, hashes = compute_review_digest(bundle_dir, primary)
    registry.record_review(project.id, digest, hashes)
    assert review_stale_files(registry.get(project.id), bundle_dir, primary) == ()
    # A session starts fine before the hostile push.
    assert resolve_project_attachment(project.id, registry=registry).workspace_dir == primary

    # A hostile commit to the SOURCE repository, then a plain sync.
    _write(api, MCP_SETTINGS_RELPATH, json.dumps({"mcpServers": {"x": {"command": "curl"}}}))
    _git(api, "add", "-A")
    _git(api, "commit", "-m", "add mcp settings to the source repo")

    synced, _result = store.sync(project.id)

    bundle_dir, primary_after, _unavailable = describe_project_sources(synced, registry=registry)
    assert review_stale_files(synced, bundle_dir, primary_after) == (MCP_SETTINGS_RELPATH,)
    with pytest.raises(ProjectSessionError) as exc_info:
        resolve_project_attachment(project.id, registry=registry)
    assert exc_info.value.code == "project_review_stale"


def _app(state, registry: ProjectRegistry) -> web.Application:
    @web.middleware
    async def identity(request: web.Request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = state
    services = handlers_project._ProjectServices()
    services._registry = registry
    app[handlers_project.PROJECT_SERVICES_KEY] = services
    app.router.add_post("/api/chat/slots", api_chat_slot_create)
    app.router.add_get("/api/project-bundles/{id}", handlers_project.api_project_get)
    return app


@_needs_git
class TestHealthAndSessionOverMaterializedSources:
    @pytest.mark.asyncio
    async def test_a_session_starts_in_the_primary_source_checkout(self, tmp_path: Path) -> None:
        api = _source_repo(tmp_path, "payments-api", "primary source")
        bundle_remote = _bundle_repo(
            tmp_path, [{"type": "repo", "url": f"file://{api}", "role": "primary"}]
        )
        registry = _registry(tmp_path)
        project = await asyncio.to_thread(_add_and_accept, registry, bundle_remote)
        _bundle_dir, primary, _unavailable = describe_project_sources(project, registry=registry)
        digest, hashes = compute_review_digest(_bundle_dir, primary)
        registry.record_review(project.id, digest, hashes)
        state = _make_state(tmp_path / "sessions")

        async with TestClient(TestServer(_app(state, registry))) as client:
            response = await client.post(
                "/api/chat/slots", json={"name": "api-chat", "project_id": project.id}
            )
            assert response.status == 200

        slot = state._slots["api-chat"]
        assert slot.project == str(primary)
        assert (Path(slot.project) / "MARKER.txt").exists()

    @pytest.mark.asyncio
    async def test_health_reports_an_unavailable_source_by_id(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        bundle_remote = _bundle_repo(
            tmp_path, [{"type": "repo", "url": f"file://{missing}", "role": "primary"}]
        )
        registry = _registry(tmp_path)
        project = await asyncio.to_thread(_add_and_accept, registry, bundle_remote)
        source_id = _synthesize_source_id(f"file://{missing}")
        state = _make_state(tmp_path / "sessions")

        async with TestClient(TestServer(_app(state, registry))) as client:
            payload = await (await client.get(f"/api/project-bundles/{project.id}")).json()

        assert payload["health"]["status"] == "sources_unavailable"
        assert payload["health"]["code"] == "project_sources_unavailable"
        assert payload["health"]["unavailable_sources"] == [source_id]
        assert "stale_files" not in payload["health"]

    @pytest.mark.asyncio
    async def test_a_healthy_materialized_project_reports_no_source_fields(
        self, tmp_path: Path
    ) -> None:
        api = _source_repo(tmp_path, "payments-api", "primary source")
        bundle_remote = _bundle_repo(
            tmp_path, [{"type": "repo", "url": f"file://{api}", "role": "primary"}]
        )
        registry = _registry(tmp_path)
        project = await asyncio.to_thread(_add_and_accept, registry, bundle_remote)
        bundle_dir, primary, _unavailable = describe_project_sources(project, registry=registry)
        digest, hashes = compute_review_digest(bundle_dir, primary)
        registry.record_review(project.id, digest, hashes)
        state = _make_state(tmp_path / "sessions")

        async with TestClient(TestServer(_app(state, registry))) as client:
            payload = await (await client.get(f"/api/project-bundles/{project.id}")).json()

        assert payload["health"] == {"status": "healthy", "code": "project_healthy"}

    @pytest.mark.asyncio
    async def test_an_unavailable_secondary_is_named_while_the_primary_still_starts(
        self, tmp_path: Path
    ) -> None:
        api = _source_repo(tmp_path, "payments-api", "primary source")
        missing = tmp_path / "no-such-repo"
        bundle_remote = _bundle_repo(
            tmp_path,
            [
                {"type": "repo", "url": f"file://{api}", "role": "primary"},
                {"type": "repo", "url": f"file://{missing}", "role": "reference"},
            ],
        )
        registry = _registry(tmp_path)
        project = await asyncio.to_thread(_add_and_accept, registry, bundle_remote)
        bundle_dir, primary, unavailable = describe_project_sources(project, registry=registry)
        digest, hashes = compute_review_digest(bundle_dir, primary)
        registry.record_review(project.id, digest, hashes)
        state = _make_state(tmp_path / "sessions")

        assert unavailable == (_synthesize_source_id(f"file://{missing}"),)

        async with TestClient(TestServer(_app(state, registry))) as client:
            payload = await (await client.get(f"/api/project-bundles/{project.id}")).json()
            started = await client.post(
                "/api/chat/slots", json={"name": "mixed-chat", "project_id": project.id}
            )
            assert started.status == 200

        assert payload["health"]["status"] == "sources_unavailable"
        assert payload["health"]["unavailable_sources"] == [
            _synthesize_source_id(f"file://{missing}")
        ]
        # A missing SECONDARY never blocks the session: the primary is what the
        # session runs in, and the brief already reports the rest as unavailable.
        assert state._slots["mixed-chat"].project == str(primary)
