"""Import surface, patch seams and exact HTTP/MCP outputs of the artifact projections.

The artifact store is reached through three projections -- the
``kiro_crew.artifacts`` facade, the dashboard HTTP handlers and the MCP tool
module -- and each of them is a contract of its own: names other modules import,
module attributes tests and callers rebind, response status codes and bodies,
and the tool registry the MCP server advertises. This file pins those surfaces
so moving an implementation between modules cannot change any of them.
"""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactFolderStore, ArtifactStore
from kiro_crew.dashboard.handlers import artifacts as art_handlers

#: Every name the facade exposes to callers: the public API plus the private
#: names other modules import from it (``slugify``, ``_validate_slug``,
#: ``_SLUG_RE``, ``_infer_kind``) and the module attributes tests rebind.
FACADE_NAMES = (
    "ALLOWED_EVENT_TYPES",
    "ALLOWED_KINDS",
    "ALLOWED_SOURCES",
    "ARTIFACTS_CREATED",
    "Artifact",
    "ArtifactAlreadyExistsError",
    "ArtifactComment",
    "ArtifactError",
    "ArtifactFolderStore",
    "ArtifactNotFoundError",
    "ArtifactPublication",
    "ArtifactReplacedError",
    "ArtifactStillPublishedError",
    "ArtifactStore",
    "ArtifactValidationError",
    "DOC_EXTENSIONS",
    "EXPECT_ABSENT",
    "FOLDER_PATH_SEP",
    "ForkMetadata",
    "ImageMetadata",
    "MAX_AUTO_WIDGET_ARTIFACTS",
    "MAX_COMMENTS_PER_ARTIFACT",
    "MAX_CONTENT_BYTES",
    "MAX_DESCRIPTION_LEN",
    "MAX_EVENTS_PER_ARTIFACT",
    "MAX_FOLDER_DEPTH",
    "MAX_NAME_LEN",
    "MAX_SOURCE_PATH_LEN",
    "MAX_TAGS",
    "MAX_TAG_LEN",
    "MAX_VERSIONS",
    "USER_SELECTABLE_KINDS",
    "WebAppArchitecture",
    "WebAppCost",
    "WebAppDeployTarget",
    "WebAppLifecycle",
    "WebAppMetadata",
    "WebAppTeardown",
    "_ExpectAbsent",
    "_IMAGE_MIME_EXT",
    "_NO_GENERATIONS",
    "_SLUG_RE",
    "_default_folder_store",
    "_default_store",
    "_fence_refusal",
    "_fence_refuses",
    "_infer_kind",
    "_lock_for_root",
    "_markdown_misclassification_reason",
    "_now_iso",
    "_open_pinned_for_read",
    "_session_touched",
    "_sniff_image_dimensions",
    "_strip_session_scope",
    "_validate_content",
    "_validate_description",
    "_validate_kind",
    "_validate_name",
    "_validate_slug",
    "_validate_source",
    "_validate_source_path",
    "_validate_tags",
    "canonical_path_refusal",
    "config_dir",
    "detect_editor_kind",
    "emit_counter",
    "filter_comments_for_forward",
    "get_default_folder_store",
    "get_default_store",
    "has_unthemed_hardcoded_colors",
    "hooks",
    "is_document_path",
    "is_sensitive_path",
    "is_verifiable_root",
    "normalize_tag",
    "sensitive_path_refusal",
    "slug_is_well_formed",
    "slugify",
    "webapp_metadata_from_dict",
)

#: Handler-module attributes other modules import or tests rebind.
HANDLER_NAMES = (
    "_MAX_BODY_BYTES",
    "_REMOTE_PROVIDER_TIMEOUT_S",
    "_audit",
    "_err",
    "_is_restricted_session",
    "_json_response",
    "_notify_artifact_update",
    "_publish_governance_denied",
    "_redact_remote_response",
    "_redact_text",
    "_resolve_folder_ref",
    "_resolve_folder_ref_off_loop",
    "_run_off_loop",
    "_serialize",
    "_serialize_folder",
    "_spawn_artifact_folder_icon_task",
    "_validate_inbound_webapp_metadata",
    "generate_emoji_for_name",
    "get_default_folder_store",
    "get_default_store",
    "get_provider",
    "publish_sync",
    "sel",
)

#: Every route coroutine the dashboard server registers from this module.
ROUTES = (
    "api_artifact_asset",
    "api_artifact_comments",
    "api_artifact_delete",
    "api_artifact_delete_comment",
    "api_artifact_detail",
    "api_artifact_edit_comment",
    "api_artifact_events",
    "api_artifact_folder_create",
    "api_artifact_folder_delete",
    "api_artifact_folder_update",
    "api_artifact_folders",
    "api_artifact_mark_review",
    "api_artifact_materialize",
    "api_artifact_overwrite_remote",
    "api_artifact_post_comment",
    "api_artifact_publish",
    "api_artifact_publish_providers",
    "api_artifact_pull_latest",
    "api_artifact_record_event",
    "api_artifact_refresh_sharing",
    "api_artifact_relocate",
    "api_artifact_reopen_comment",
    "api_artifact_reply_comment",
    "api_artifact_reprobe_notice",
    "api_artifact_resolve_comment",
    "api_artifact_session_docs",
    "api_artifact_set_folder",
    "api_artifact_set_pinned",
    "api_artifact_settle_blank",
    "api_artifact_unpublish",
    "api_artifact_update",
    "api_artifact_update_sharing",
    "api_artifact_upstream_status",
    "api_artifact_version_detail",
    "api_artifact_versions",
    "api_artifacts_create",
    "api_artifacts_list",
    "api_remote_artifact_comments",
    "api_remote_artifact_delete_comment",
    "api_remote_artifact_get",
    "api_remote_artifact_mark_review",
    "api_remote_artifact_post_comment",
    "api_remote_artifact_reply_comment",
    "api_remote_artifacts_browse",
    "api_remote_artifacts_clone",
    "api_remote_artifacts_fork",
)

MCP_TOOLS = (
    ("artifact_save", ["name", "content"]),
    ("artifact_get", ["slug"]),
    ("artifact_update", ["slug"]),
    ("artifact_revert", ["slug", "target_version"]),
    ("artifact_list", []),
    ("artifact_versions", ["slug"]),
    ("artifact_delete", ["slug"]),
    ("artifact_get_comments", ["slug"]),
    ("artifact_post_comment", ["slug", "text"]),
    ("artifact_reply_comment", ["slug", "parent_id", "text"]),
    ("artifact_mark_review", ["slug", "comment_id"]),
    ("artifact_delete_comment", ["slug", "comment_id", "reason"]),
    ("artifact_folder_list", []),
    ("artifact_folder_create", ["name"]),
    ("artifact_folder_rename", ["folder", "name"]),
    ("artifact_folder_move", ["folder"]),
    ("artifact_folder_delete", ["folder"]),
    ("artifact_move", ["slug"]),
    ("deploy_artifact", ["site_id"]),
)


class TestImportSurface:
    def test_facade_names_resolve(self) -> None:
        missing = [name for name in FACADE_NAMES if not hasattr(art_mod, name)]
        assert missing == []

    def test_handler_names_resolve(self) -> None:
        missing = [name for name in HANDLER_NAMES if not hasattr(art_handlers, name)]
        assert missing == []

    def test_every_route_handler_is_a_coroutine(self) -> None:
        assert tuple(n for n in dir(art_handlers) if n.startswith("api_")) == ROUTES
        assert [
            n for n in ROUTES if not inspect.iscoroutinefunction(getattr(art_handlers, n))
        ] == []

    def test_projections_resolve_the_store_through_the_facade(self) -> None:
        # The handlers must read the singletons through the facade functions, so a
        # test (or caller) that rebinds ``kiro_crew.artifacts._default_store`` steers
        # every route.
        assert art_handlers.get_default_store is art_mod.get_default_store
        assert art_handlers.get_default_folder_store is art_mod.get_default_folder_store

    def test_leaf_modules_import_and_keep_their_identity_pins(self) -> None:
        image_artifacts = importlib.import_module("kiro_crew.image_artifacts")
        outbound = importlib.import_module("kiro_crew.messaging.outbound_files")
        widget_artifacts = importlib.import_module("kiro_crew.widget_artifacts")
        artifact_source = importlib.import_module("kiro_crew.artifact_source")
        for name in ("strip_url_syntax", "local_destination", "is_remote_destination"):
            assert getattr(image_artifacts, name) is getattr(outbound, name)
        assert callable(widget_artifacts.register_widgets_off_loop)
        assert callable(image_artifacts.register_images_off_loop)
        assert callable(artifact_source.classify_source)

    def test_webapp_types_are_re_exported_not_copied(self) -> None:
        webapp_types = importlib.import_module("kiro_crew.deploy.webapp_types")
        for name in ("WebAppMetadata", "WebAppLifecycle", "webapp_metadata_from_dict"):
            assert getattr(art_mod, name) is getattr(webapp_types, name)


class TestMcpRegistry:
    def test_schemas_and_handlers_stay_paired_and_ordered(self) -> None:
        tools = importlib.import_module("kiro_crew.mcp_tools.artifacts")
        advertised = [(s["name"], s["inputSchema"].get("required", [])) for s in tools.schemas()]
        assert advertised == list(MCP_TOOLS)
        assert list(tools.HANDLERS) == [name for name, _ in MCP_TOOLS]

    def test_save_posts_the_store_route(self) -> None:
        from kiro_crew.mcp_core import _call_tool_inner

        sent: dict = {}

        def _capture(path, body, **kwargs):
            sent["path"] = path
            sent["body"] = dict(body)
            return {"slug": "s", "version": 1, "name": "N", "kind": "widget"}

        # A chat widget save first asks the gateway for same-named widgets; stub that
        # probe too so the test never reaches a real gateway on the loopback port.
        with patch("kiro_crew.mcp_core._resolve_session_key", return_value=""):
            with patch("kiro_crew.mcp_core._get", return_value={"artifacts": []}) as probe:
                with patch("kiro_crew.mcp_core._post", side_effect=_capture):
                    _call_tool_inner("artifact_save", {"name": "N", "content": "<div/>"})
        assert [c.args[0] for c in probe.call_args_list] == [
            "/api/artifacts?kind=widget&source=chat&q=N"
        ]
        assert sent["path"] == "/api/artifacts"
        assert sent["body"]["name"] == "N"
        assert sent["body"]["content"] == "<div/>"
        # Kind is left for the store to infer, not decided in the tool layer.
        assert sent["body"] == {"name": "N", "content": "<div/>"}


# ── HTTP ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def stores(tmp_path: Path, monkeypatch) -> tuple[ArtifactStore, ArtifactFolderStore]:
    store = ArtifactStore(root=tmp_path / "artifacts")
    folders = ArtifactFolderStore(path=tmp_path / "artifact_folders.json")
    monkeypatch.setattr(art_mod, "_default_store", store)
    monkeypatch.setattr(art_mod, "_default_folder_store", folders)
    monkeypatch.setattr(art_handlers, "_is_restricted_session", lambda _s, req: req.app["_r"])
    monkeypatch.setattr(art_handlers, "_spawn_artifact_folder_icon_task", lambda *a, **k: None)
    return store, folders


def _request(
    *,
    body: dict | bytes | None = None,
    match: dict | None = None,
    query: dict | None = None,
    restricted: bool = False,
    mcp: bool = False,
) -> MagicMock:
    req = MagicMock()
    headers = {"X-Session-Key": "dashboard:test"}
    if mcp:
        headers["X-Internal-Secret"] = "s"
    req.headers = headers
    req.match_info = match or {}
    req.query = query or {}
    raw = json.dumps(body).encode() if isinstance(body, dict) else (body or b"")
    req.read = AsyncMock(return_value=raw)
    state = MagicMock()
    state.get_slot.return_value = None
    req.app = {"state": state, "_r": restricted}
    return req


def _body(resp) -> dict:
    return json.loads(resp.body)


@pytest.mark.asyncio
class TestHttpContract:
    async def test_create_then_read_then_delete(self, stores) -> None:
        resp = await art_handlers.api_artifacts_create(
            _request(body={"name": "Doc", "content": "# hi", "tags": ["a"]})
        )
        assert resp.status == 201
        created = _body(resp)
        assert list(created)[:4] == ["slug", "name", "kind", "kind_auto"]
        assert list(created)[-2:] == ["slug_collided_with", "theme_contrast_warning"]
        assert (created["slug"], created["kind"], created["content"]) == ("doc", "markdown", "# hi")
        assert created["slug_collided_with"] == "" and created["theme_contrast_warning"] is False

        detail = await art_handlers.api_artifact_detail(_request(match={"slug": "doc"}))
        assert detail.status == 200
        # Create and current detail both serve the persisted stale-write token.
        assert list(_body(detail)) == [
            k for k in created if k not in ("slug_collided_with", "theme_contrast_warning")
        ]

        gone = await art_handlers.api_artifact_delete(_request(match={"slug": "doc"}))
        assert (gone.status, _body(gone)) == (200, {"ok": True})
        missing = await art_handlers.api_artifact_detail(_request(match={"slug": "doc"}))
        assert (missing.status, _body(missing)) == (404, {"error": "artifact not found: doc"})

    async def test_error_bodies(self, stores) -> None:
        dup = _request(body={"name": "a", "content": "c", "slug": "taken"})
        assert (await art_handlers.api_artifacts_create(dup)).status == 201
        again = await art_handlers.api_artifacts_create(
            _request(body={"name": "b", "content": "c", "slug": "taken"})
        )
        assert (again.status, _body(again)) == (409, {"error": "artifact already exists: taken"})

        bad_json = await art_handlers.api_artifacts_create(_request(body=b"[1]"))
        assert (bad_json.status, _body(bad_json)) == (
            400,
            {"error": "request body must be a JSON object"},
        )

        denied = await art_handlers.api_artifacts_create(
            _request(body={"name": "x"}, restricted=True)
        )
        assert (denied.status, _body(denied)) == (
            403,
            {"error": "restricted session cannot create artifacts"},
        )

        bad_slug = await art_handlers.api_artifact_versions(_request(match={"slug": "Bad"}))
        assert bad_slug.status == 400
        assert _body(bad_slug)["error"].startswith("invalid slug 'Bad': must match")

        kind = await art_handlers.api_artifact_update(
            _request(match={"slug": "taken"}, body={"kind": "widget"})
        )
        assert kind.status == 400
        assert _body(kind) == {
            "error": "kind must be one of ['json', 'markdown', 'svg', 'text']; got 'widget'"
        }

    async def test_update_snapshot_defaults_follow_the_caller(self, stores) -> None:
        store, _ = stores
        store.create(name="v", content="1")
        ui = await art_handlers.api_artifact_update(
            _request(match={"slug": "v"}, body={"content": "2"})
        )
        assert (ui.status, _body(ui)["version"]) == (200, 1)
        agent = await art_handlers.api_artifact_update(
            _request(match={"slug": "v"}, body={"content": "3"}, mcp=True)
        )
        assert (agent.status, _body(agent)["version"]) == (200, 2)
        assert store.get("v").events[-1]["type"] == "iterated"
        versions = await art_handlers.api_artifact_versions(_request(match={"slug": "v"}))
        assert _body(versions) == {"slug": "v", "versions": [1, 2]}

    async def test_folder_routes(self, stores) -> None:
        store, folders = stores
        made = await art_handlers.api_artifact_folder_create(_request(body={"name": "Reports"}))
        assert made.status == 201
        folder = _body(made)
        assert list(folder) == ["id", "name", "order", "parent_id", "path"]
        assert (folder["name"], folder["path"], folder["parent_id"]) == ("Reports", "Reports", "")

        store.create(name="filed", content="c")
        moved = await art_handlers.api_artifact_set_folder(
            _request(match={"slug": "filed"}, body={"folder": "Reports/Q3"})
        )
        assert moved.status == 200
        leaf = _body(moved)["folder_id"]
        assert folders.breadcrumb(leaf) == "Reports/Q3"

        listed = await art_handlers.api_artifact_folders(_request())
        rows = {f["path"]: f["item_count"] for f in _body(listed)["folders"]}
        assert rows == {"Reports": 0, "Reports/Q3": 1}

        unknown = await art_handlers.api_artifact_set_folder(
            _request(match={"slug": "filed"}, body={"folder_id": "nope"})
        )
        assert (unknown.status, _body(unknown)) == (400, {"error": "folder path not found: nope"})

        dropped = await art_handlers.api_artifact_folder_delete(
            _request(match={"id": folder["id"]}, query={"delete_contents": "0"})
        )
        assert dropped.status == 200
        summary = _body(dropped)
        assert summary["ok"] is True and summary["delete_contents"] is False
        assert summary["deleted_folder_ids"] == [folder["id"]]
        assert store.get("filed").folder_id == leaf
