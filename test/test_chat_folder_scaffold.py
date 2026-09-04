"""Tests for the folder-scaffold endpoints.

The scan endpoint is a preview, so what is pinned there is mostly what it does
NOT do: it creates no folder, it refuses exactly the roots manual folder
creation refuses, and it never offers to re-create a folder the user already
has. The scaffold endpoint is the half that writes, so what is pinned here is
that it writes only what the server itself just offered, nests what it creates
the way the preview showed it, and never removes anything — including the
folders a failed creation leaves behind. The scanner's own detection rules are
covered by the engine suites; the layouts below are the smallest ones that
exercise an endpoint concern.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.dashboard.chat_folder_scaffold import (
    MAX_REPORTED_UNKNOWN,
    STATUS_EMPTY,
    STATUS_OK,
    _scan_off_loop,
    api_chat_folders_scaffold,
    api_chat_folders_scan,
)
from kiro_crew.dashboard.chat_folders import (
    FolderCreateError,
    FolderOwnershipError,
    create_folder_record,
)


def _make_scaffold_app(state: Any) -> web.Application:
    """Minimal aiohttp app with the folder-scaffold endpoints."""

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/project-scaffold/scan", api_chat_folders_scan)
    app.router.add_post("/api/project-scaffold/create", api_chat_folders_scaffold)
    return app


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    return _make_state(tmp_path)


def _sibling_repos(root: Path) -> Path:
    """Two sibling repositories under a plain directory: both AUTO."""

    root.mkdir()
    for name in ("api", "web"):
        (root / name / ".git").mkdir(parents=True)
    return root


def _monorepo(root: Path) -> Path:
    """A repo whose own manifest declares two members: both OFFERED.

    The root carrying a manifest puts everything below it inside a package, which
    is the tier split worth exercising through the endpoint — a payload where
    nothing is ticked by default has to still be a usable preview.
    """

    root.mkdir()
    (root / ".git").mkdir()
    (root / "package.json").write_text(json.dumps({"workspaces": ["packages/*"]}), encoding="utf-8")
    for name in ("alpha", "beta"):
        member = root / "packages" / name
        member.mkdir(parents=True)
        (member / "package.json").write_text("{}", encoding="utf-8")
    return root


def _nested(root: Path) -> Path:
    """Two repositories, one of which holds a nested manifest.

    Spans both tiers and two group levels, which is what the selection-default
    and grouping assertions need from one layout.
    """

    root.mkdir()
    (root / "other" / ".git").mkdir(parents=True)
    (root / "repo" / ".git").mkdir(parents=True)
    # A manifest INSIDE a repository is the ambiguous case — offered, unticked.
    (root / "repo" / "sub").mkdir()
    (root / "repo" / "sub" / "pyproject.toml").write_text("", encoding="utf-8")
    return root


def _deep(root: Path) -> Path:
    """Three candidate levels: a repository, a manifest inside it, a ``.kiro``.

    The middle candidate is OFFERED, so a default selection leaves it out — which
    is the layout the nearest-created-ancestor parenting rule needs.
    """

    root.mkdir()
    (root / "repo" / ".git").mkdir(parents=True)
    (root / "repo" / "mid").mkdir()
    (root / "repo" / "mid" / "pyproject.toml").write_text("", encoding="utf-8")
    (root / "repo" / "mid" / "leaf" / ".kiro").mkdir(parents=True)
    return root


async def _scan(client: TestClient, root: Any) -> tuple[int, dict[str, Any]]:
    resp = await client.post("/api/project-scaffold/scan", json={"root": str(root)})
    return resp.status, await resp.json()


async def _scaffold(
    client: TestClient, root: Any, selected: Any = ()
) -> tuple[int, dict[str, Any]]:
    resp = await client.post(
        "/api/project-scaffold/create",
        json={"root": str(root), "selected": [str(path) for path in selected]},
    )
    return resp.status, await resp.json()


def _by_project_dir(state: Any) -> dict[str, dict[str, Any]]:
    """Folders keyed by the project directory they were created on."""

    return {f["project_dir"]: f for f in state._folders}


class TestScanPreview:
    @pytest.mark.asyncio
    async def test_returns_candidates_with_tier_name_and_path(
        self, state: Any, tmp_path: Path
    ) -> None:
        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scan(client, root)

        assert status == 200
        assert body["status"] == STATUS_OK
        assert [(c["name"], c["tier"]) for c in body["candidates"]] == [
            ("api", "auto"),
            ("web", "auto"),
        ]
        assert [c["path"] for c in body["candidates"]] == [
            str(root / "api"),
            str(root / "web"),
        ]
        assert body["root"] == str(root)

    @pytest.mark.asyncio
    async def test_creates_nothing(self, state: Any, tmp_path: Path) -> None:
        """A preview is a preview: no folder may exist afterwards."""

        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scan(client, root)

        assert status == 200
        assert len(body["candidates"]) == 2
        assert state._folders == []

    @pytest.mark.asyncio
    async def test_monorepo_members_are_offered_unticked(self, state: Any, tmp_path: Path) -> None:
        """Inside a package every nested manifest is ambiguous, so nothing is ticked."""

        root = _monorepo(tmp_path / "mono")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, body = await _scan(client, root)

        selection = {c["name"]: (c["tier"], c["selected"]) for c in body["candidates"]}
        assert selection == {"alpha": ("offered", False), "beta": ("offered", False)}
        # Declared membership is reported alongside the manifest that was found.
        assert body["candidates"][0]["signals"] == ["manifest:package.json", "member"]

    @pytest.mark.asyncio
    async def test_selection_default_follows_tier(self, state: Any, tmp_path: Path) -> None:
        """A tree holding both tiers: AUTO ticked, OFFERED unticked, one payload."""

        root = _nested(tmp_path / "mixed")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, body = await _scan(client, root)

        selection = {c["name"]: (c["tier"], c["selected"]) for c in body["candidates"]}
        assert selection == {
            "other": ("auto", True),
            "repo": ("auto", True),
            "sub": ("offered", False),
        }

    @pytest.mark.asyncio
    async def test_signals_explain_each_candidate(self, state: Any, tmp_path: Path) -> None:
        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, body = await _scan(client, root)

        assert all(c["signals"] == ["git"] for c in body["candidates"])

    @pytest.mark.asyncio
    async def test_empty_root_is_a_status_not_an_error(self, state: Any, tmp_path: Path) -> None:
        """Zero candidates must be answerable — a 200 a surface can branch on."""

        root = tmp_path / "bare"
        (root / "notes").mkdir(parents=True)
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scan(client, root)

        assert status == 200
        assert body["status"] == STATUS_EMPTY
        assert body["candidates"] == []

    @pytest.mark.asyncio
    async def test_warnings_are_reported_not_raised(self, state: Any, tmp_path: Path) -> None:
        """A declaration that cannot be parsed costs that declaration, not the scan."""

        root = tmp_path / "mono"
        root.mkdir()
        (root / ".git").mkdir()
        (root / "package.json").write_text("{not json", encoding="utf-8")
        (root / "svc" / ".git").mkdir(parents=True)
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scan(client, root)

        assert status == 200
        assert [c["name"] for c in body["candidates"]] == ["svc"]
        assert len(body["warnings"]) == 1
        assert "package.json" in body["warnings"][0]


class TestScanRootValidation:
    @pytest.mark.asyncio
    async def test_relative_root_rejected(self, state: Any) -> None:
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scan(client, "relative/path")

        assert status == 400
        assert body["error"] == "project_dir must be an absolute path"
        assert body["code"] == "folder_scan_root_invalid"

    @pytest.mark.asyncio
    async def test_missing_root_rejected(self, state: Any, tmp_path: Path) -> None:
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scan(client, tmp_path / "does-not-exist")

        assert status == 400
        assert body["error"] == "project_dir must be an existing directory"
        assert body["code"] == "folder_scan_root_invalid"

    @pytest.mark.asyncio
    async def test_sensitive_root_rejected(self, state: Any) -> None:
        """The scan refuses what manual folder creation refuses, same wording."""

        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scan(client, "~/.ssh")

        assert status == 400
        assert body["error"] == "project_dir refers to a sensitive path"
        assert body["code"] == "folder_scan_root_invalid"

    @pytest.mark.asyncio
    async def test_absent_root_field_rejected(self, state: Any) -> None:
        """An empty root is caller error, not a scan of nothing."""

        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            resp = await client.post("/api/project-scaffold/scan", json={})
            assert resp.status == 400
            body = await resp.json()

        assert body["code"] == "folder_scan_root_required"

    @pytest.mark.asyncio
    async def test_non_object_body_rejected(self, state: Any) -> None:
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            resp = await client.post("/api/project-scaffold/scan", json=["/tmp"])
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_malformed_json_rejected(self, state: Any) -> None:
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            resp = await client.post(
                "/api/project-scaffold/scan",
                data="{not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"


class TestReconcileOverlay:
    @pytest.mark.asyncio
    async def test_existing_candidate_marked_and_unticked(self, state: Any, tmp_path: Path) -> None:
        """A re-scan must not offer to duplicate a folder the user already has."""

        root = _sibling_repos(tmp_path / "work")
        state._folders = [
            {
                "id": "f-api",
                "name": "api",
                "order": 0,
                "collapsed": False,
                "project_dir": str(root / "api"),
            }
        ]
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, body = await _scan(client, root)

        overlay = {c["name"]: (c["existing"], c["selected"]) for c in body["candidates"]}
        assert overlay == {"api": (True, False), "web": (False, True)}
        # Still reported: "already set up" is information, not a reason to hide it.
        assert len(body["candidates"]) == 2

    @pytest.mark.asyncio
    async def test_new_package_since_a_prior_scan_is_still_offered(
        self, state: Any, tmp_path: Path
    ) -> None:
        root = _sibling_repos(tmp_path / "work")
        state._folders = [
            {"id": "f-api", "name": "api", "order": 0, "project_dir": str(root / "api")},
            {"id": "f-web", "name": "web", "order": 1, "project_dir": str(root / "web")},
        ]
        (root / "batch" / ".git").mkdir(parents=True)
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, body = await _scan(client, root)

        new = [c for c in body["candidates"] if not c["existing"]]
        assert [(c["name"], c["tier"], c["selected"]) for c in new] == [("batch", "auto", True)]

    @pytest.mark.asyncio
    async def test_match_is_exact_not_by_prefix(self, state: Any, tmp_path: Path) -> None:
        """A folder on a SIBLING directory must not mark a candidate as taken."""

        root = _sibling_repos(tmp_path / "work")
        (root / "api2" / ".git").mkdir(parents=True)
        state._folders = [
            {"id": "f-api", "name": "api", "order": 0, "project_dir": str(root / "api")}
        ]
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, body = await _scan(client, root)

        overlay = {c["name"]: c["existing"] for c in body["candidates"]}
        assert overlay == {"api": True, "api2": False, "web": False}

    @pytest.mark.asyncio
    async def test_root_reconcile_state_reported_separately(
        self, state: Any, tmp_path: Path
    ) -> None:
        """The root's folder is created by the scaffold step, so it gets its own flag."""

        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, before = await _scan(client, root)
            assert before["root_existing"] is False

            state._folders = [{"id": "f-root", "name": "work", "project_dir": str(root)}]
            _, after = await _scan(client, root)

        assert after["root_existing"] is True

    @pytest.mark.asyncio
    async def test_unusable_folder_entries_do_not_break_the_overlay(
        self, state: Any, tmp_path: Path
    ) -> None:
        """The folder store is loaded unvalidated, so a junk entry must be skipped."""

        root = _sibling_repos(tmp_path / "work")
        state._folders = [
            "not a folder",
            {"id": "f-none", "name": "No project"},
            {"id": "f-blank", "name": "Blank", "project_dir": ""},
            {"id": "f-null", "name": "Null", "project_dir": None},
            {"id": "f-api", "name": "api", "project_dir": str(root / "api")},
        ]
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scan(client, root)

        assert status == 200
        assert {c["name"]: c["existing"] for c in body["candidates"]} == {
            "api": True,
            "web": False,
        }


class TestScanDepthBound:
    @pytest.mark.asyncio
    async def test_the_scan_is_bounded_by_the_scanner_default(
        self, state: Any, tmp_path: Path
    ) -> None:
        """The bound is the scanner's own default, not a configuration key.

        Pinned from the endpoint rather than from ``project_scan`` directly so the
        endpoint is shown to inherit the bound: nothing here passes a depth, so a
        caller that later started passing one would have to update this test.
        """

        root = tmp_path / "deep"
        # One level past DEFAULT_DEPTH_CAP (5), so it is reached only if the bound
        # is absent; its sibling at the cap is reached either way.
        (root / "a" / "b" / "c" / "d" / "e" / "too_deep").mkdir(parents=True)
        (root / "a" / "b" / "c" / "d" / "e" / "too_deep" / ".git").mkdir()
        (root / "a" / "b" / "c" / "d" / "at_cap").mkdir(parents=True)
        (root / "a" / "b" / "c" / "d" / "at_cap" / ".git").mkdir()

        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, body = await _scan(client, root)

        assert [c["name"] for c in body["candidates"]] == ["at_cap"]


class _StubConfig:
    """Stands in for ``KiroCrewConfig`` so ``.load()`` returns a chosen config."""

    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg

    def load(self) -> Any:
        return self._cfg


class TestScaffoldCreation:
    @pytest.mark.asyncio
    async def test_root_folder_is_created_for_the_scan_root(
        self, state: Any, tmp_path: Path
    ) -> None:
        """The scan root always gets a folder — it is what the rest nests under."""

        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, root)

        assert status == 200
        assert [entry["path"] for entry in body["created"]] == [str(root)]
        folder = _by_project_dir(state)[str(root)]
        assert folder["name"] == "work"
        assert folder["parent_id"] == ""

    @pytest.mark.asyncio
    async def test_selected_candidates_become_folders_under_the_root(
        self, state: Any, tmp_path: Path
    ) -> None:
        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, root, [root / "api", root / "web"])

        assert status == 200
        assert body["failed"] == []
        folders = _by_project_dir(state)
        assert sorted(folders) == sorted([str(root), str(root / "api"), str(root / "web")])
        root_id = folders[str(root)]["id"]
        assert folders[str(root / "api")]["parent_id"] == root_id
        assert folders[str(root / "web")]["parent_id"] == root_id
        # A folder's project_dir is what makes a chat opened in it scope-correct,
        # so it is the candidate's own path, not the root's.
        assert folders[str(root / "api")]["project_dir"] == str(root / "api")
        assert folders[str(root / "api")]["name"] == "api"

    @pytest.mark.asyncio
    async def test_unselected_candidate_gets_no_folder(self, state: Any, tmp_path: Path) -> None:
        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, root, [root / "api"])

        assert status == 200
        assert sorted(_by_project_dir(state)) == sorted([str(root), str(root / "api")])
        assert [entry["path"] for entry in body["created"]] == [str(root), str(root / "api")]

    @pytest.mark.asyncio
    async def test_empty_selection_creates_only_the_root_folder(
        self, state: Any, tmp_path: Path
    ) -> None:
        """ "Just the root, none of the packages" is a real answer, not a no-op."""

        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, root, [])

        assert status == 200
        assert list(_by_project_dir(state)) == [str(root)]
        assert body["skipped_existing"] == []

    @pytest.mark.asyncio
    async def test_children_are_created_after_their_parent(
        self, state: Any, tmp_path: Path
    ) -> None:
        """Every folder names a parent that already exists when it is created."""

        root = _deep(tmp_path / "work")
        selected = [root / "repo", root / "repo" / "mid", root / "repo" / "mid" / "leaf"]
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, root, selected)

        assert status == 200
        assert body["failed"] == []
        folders = _by_project_dir(state)
        assert folders[str(root / "repo")]["parent_id"] == folders[str(root)]["id"]
        assert folders[str(root / "repo" / "mid")]["parent_id"] == folders[str(root / "repo")]["id"]
        assert (
            folders[str(root / "repo" / "mid" / "leaf")]["parent_id"]
            == folders[str(root / "repo" / "mid")]["id"]
        )
        # Creation order, not just the final links: a parent appearing after its
        # child would mean the child named an id that did not exist yet.
        order = [entry["path"] for entry in body["created"]]
        assert order == [str(root)] + [str(path) for path in selected]

    @pytest.mark.asyncio
    async def test_skipped_middle_candidate_reparents_to_nearest_created_ancestor(
        self, state: Any, tmp_path: Path
    ) -> None:
        """A partial selection nests as deeply as it can rather than flattening."""

        root = _deep(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, _ = await _scaffold(
                client, root, [root / "repo", root / "repo" / "mid" / "leaf"]
            )

        assert status == 200
        folders = _by_project_dir(state)
        assert str(root / "repo" / "mid") not in folders
        assert (
            folders[str(root / "repo" / "mid" / "leaf")]["parent_id"]
            == folders[str(root / "repo")]["id"]
        )


class TestScaffoldSelectionRederivation:
    @pytest.mark.asyncio
    async def test_path_the_scan_does_not_offer_is_rejected(
        self, state: Any, tmp_path: Path
    ) -> None:
        """A selection is only ever a pick from what this server just found."""

        root = _sibling_repos(tmp_path / "work")
        # A real directory inside the root, but one the scanner offers no
        # candidate for: no signal, so no folder may be created on it.
        (root / "notes").mkdir()
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, root, [root / "api", root / "notes"])

        assert status == 400
        assert body["code"] == "folder_scaffold_selection_stale"
        assert body["unknown"] == [str(root / "notes")]
        # Refused wholesale: the selection the user confirmed no longer describes
        # the tree, so none of it is acted on.
        assert state._folders == []

    @pytest.mark.asyncio
    async def test_path_outside_the_scan_root_is_rejected(self, state: Any, tmp_path: Path) -> None:
        """The one that matters: a forged path cannot smuggle in a folder."""

        root = _sibling_repos(tmp_path / "work")
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / ".git").mkdir(parents=True)
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, root, [elsewhere])

        assert status == 400
        assert body["code"] == "folder_scaffold_selection_stale"
        assert state._folders == []

    @pytest.mark.asyncio
    async def test_a_stale_scan_no_longer_naming_a_candidate_is_rejected(
        self, state: Any, tmp_path: Path
    ) -> None:
        """The preview's own paths go stale when the tree changes under it."""

        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, preview = await _scan(client, root)
            assert [c["path"] for c in preview["candidates"]] == [
                str(root / "api"),
                str(root / "web"),
            ]
            # The package the user saw is gone by the time they confirm.
            (root / "api" / ".git").rmdir()
            status, body = await _scaffold(client, root, [c["path"] for c in preview["candidates"]])

        assert status == 400
        assert body["unknown"] == [str(root / "api")]

    @pytest.mark.asyncio
    async def test_reported_unknown_paths_are_capped(self, state: Any, tmp_path: Path) -> None:
        """The rejected list is caller-controlled, so the response cannot grow with it."""

        root = _sibling_repos(tmp_path / "work")
        forged = [str(root / f"ghost{index}") for index in range(MAX_REPORTED_UNKNOWN + 10)]
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, root, forged)

        assert status == 400
        assert len(body["unknown"]) == MAX_REPORTED_UNKNOWN

    @pytest.mark.asyncio
    async def test_selected_must_be_a_list_of_strings(self, state: Any, tmp_path: Path) -> None:
        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            for selected in ("everything", {"path": 1}, [1, 2], [None]):
                resp = await client.post(
                    "/api/project-scaffold/create",
                    json={"root": str(root), "selected": selected},
                )
                assert resp.status == 400
                assert (await resp.json())["code"] == "folder_scaffold_selection_invalid"
        assert state._folders == []

    @pytest.mark.asyncio
    async def test_null_selection_is_an_empty_selection(self, state: Any, tmp_path: Path) -> None:
        """A surface with nothing ticked may send null; it means the root only."""

        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            resp = await client.post(
                "/api/project-scaffold/create", json={"root": str(root), "selected": None}
            )
            assert resp.status == 200
        assert list(_by_project_dir(state)) == [str(root)]

    @pytest.mark.asyncio
    async def test_root_is_validated_exactly_as_the_scan_validates_it(self, state: Any) -> None:
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, "relative/path")

        assert status == 400
        assert body["error"] == "project_dir must be an absolute path"
        assert body["code"] == "folder_scan_root_invalid"

    @pytest.mark.asyncio
    async def test_sensitive_root_rejected(self, state: Any) -> None:
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, "~/.ssh")

        assert status == 400
        assert body["error"] == "project_dir refers to a sensitive path"
        assert state._folders == []

    @pytest.mark.asyncio
    async def test_malformed_json_rejected(self, state: Any) -> None:
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            resp = await client.post(
                "/api/project-scaffold/create",
                data="{not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"


class TestScaffoldReconcile:
    @pytest.mark.asyncio
    async def test_existing_candidate_is_skipped_not_duplicated(
        self, state: Any, tmp_path: Path
    ) -> None:
        root = _sibling_repos(tmp_path / "work")
        state._folders = [
            {
                "id": "f-api",
                "name": "api",
                "order": 0,
                "parent_id": "",
                "project_dir": str(root / "api"),
            }
        ]
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, root, [root / "api", root / "web"])

        assert status == 200
        assert body["skipped_existing"] == [str(root / "api")]
        assert [entry["path"] for entry in body["created"]] == [str(root), str(root / "web")]
        assert [f["id"] for f in state._folders if f["project_dir"] == str(root / "api")] == [
            "f-api"
        ]

    @pytest.mark.asyncio
    async def test_existing_root_is_skipped_and_still_the_parent(
        self, state: Any, tmp_path: Path
    ) -> None:
        """A re-scan of an already-scaffolded root files new packages under it."""

        root = _sibling_repos(tmp_path / "work")
        state._folders = [
            {"id": "f-root", "name": "work", "order": 0, "parent_id": "", "project_dir": str(root)}
        ]
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, root, [root / "web"])

        assert status == 200
        assert body["skipped_existing"] == [str(root)]
        assert _by_project_dir(state)[str(root / "web")]["parent_id"] == "f-root"

    @pytest.mark.asyncio
    async def test_children_of_an_existing_folder_are_parented_to_it(
        self, state: Any, tmp_path: Path
    ) -> None:
        root = _deep(tmp_path / "work")
        state._folders = [
            {
                "id": "f-repo",
                "name": "repo",
                "order": 0,
                "parent_id": "",
                "project_dir": str(root / "repo"),
            }
        ]
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, _ = await _scaffold(client, root, [root / "repo", root / "repo" / "mid"])

        assert status == 200
        assert _by_project_dir(state)[str(root / "repo" / "mid")]["parent_id"] == "f-repo"

    @pytest.mark.asyncio
    async def test_rerunning_the_same_scaffold_creates_nothing(
        self, state: Any, tmp_path: Path
    ) -> None:
        root = _sibling_repos(tmp_path / "work")
        selected = [root / "api", root / "web"]
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            await _scaffold(client, root, selected)
            after_first = copy.deepcopy(state._folders)
            status, body = await _scaffold(client, root, selected)

        assert status == 200
        assert body["created"] == []
        assert sorted(body["skipped_existing"]) == sorted(
            [str(root)] + [str(path) for path in selected]
        )
        assert state._folders == after_first


class TestScaffoldPartialFailure:
    @pytest.mark.asyncio
    async def test_one_refused_folder_costs_only_that_folder(
        self, state: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What was created stays created: no rollback deletion on partial failure."""

        root = _sibling_repos(tmp_path / "work")

        async def _refuse_web(state_arg: Any, **kwargs: Any) -> dict[str, Any]:
            if kwargs.get("project_dir") == str(root / "web"):
                raise FolderCreateError("folder store is full", "folder_store_full")
            return await create_folder_record(state_arg, **kwargs)

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_folder_scaffold.create_folder_record", _refuse_web
        )
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, root, [root / "api", root / "web"])

        assert status == 200
        assert [entry["path"] for entry in body["created"]] == [str(root), str(root / "api")]
        assert body["failed"] == [
            {
                "path": str(root / "web"),
                "error": "folder store is full",
                "code": "folder_store_full",
            }
        ]
        # The successful half survives — deleting it could delete a folder that
        # already holds conversations.
        assert sorted(_by_project_dir(state)) == sorted([str(root), str(root / "api")])

    @pytest.mark.asyncio
    async def test_an_ownership_refusal_is_audited_as_denied(
        self, state: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A foreign-owned parent is a security decision, so the trail says denied.

        Worth pinning because nothing else notices: the refusal is fail-safe —
        the folder is not created and the path is reported — so a trail that
        recorded it as an allow would still look like a working endpoint.
        """
        root = _sibling_repos(tmp_path / "work")

        async def _refuse_web(state_arg: Any, **kwargs: Any) -> dict[str, Any]:
            if kwargs.get("project_dir") == str(root / "web"):
                raise FolderOwnershipError()
            return await create_folder_record(state_arg, **kwargs)

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_folder_scaffold.create_folder_record", _refuse_web
        )
        events: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_folder_scaffold.sel",
            lambda: mock.Mock(log_api_access=lambda **kw: events.append(kw)),
        )
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, root, [root / "api", root / "web"])

        assert status == 200
        assert [entry["path"] for entry in body["failed"]] == [str(root / "web")]
        denied = [event for event in events if event.get("outcome") == "denied"]
        assert [event["resources"] for event in denied] == [str(root / "web")]
        assert denied[0]["error"] == "folder_not_owned"

    @pytest.mark.asyncio
    async def test_a_store_write_failure_is_reported_not_raised(
        self, state: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _sibling_repos(tmp_path / "work")

        async def _fail_web(state_arg: Any, **kwargs: Any) -> dict[str, Any]:
            if kwargs.get("project_dir") == str(root / "web"):
                raise OSError("No space left on device")
            return await create_folder_record(state_arg, **kwargs)

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_folder_scaffold.create_folder_record", _fail_web
        )
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, root, [root / "api", root / "web"])

        assert status == 200
        assert body["failed"][0]["path"] == str(root / "web")
        assert body["failed"][0]["code"] == "folder_create_failed"

    @pytest.mark.asyncio
    async def test_root_failure_stops_before_creating_children(
        self, state: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a root folder the selection would land as unrelated top-level folders."""

        root = _sibling_repos(tmp_path / "work")

        async def _refuse(state_arg: Any, **kwargs: Any) -> dict[str, Any]:
            raise FolderCreateError("name required")

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_folder_scaffold.create_folder_record", _refuse
        )
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, root, [root / "api", root / "web"])

        assert status == 200
        assert body["created"] == []
        assert [entry["path"] for entry in body["failed"]] == [str(root)]
        assert state._folders == []

    @pytest.mark.asyncio
    async def test_a_candidate_swapped_for_a_symlink_after_the_scan_is_refused(
        self, state: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A directory substituted after the scan must not bind a folder elsewhere.

        The create path re-resolves each ``project_dir``; a component swapped for
        a symlink in the scan-to-create window resolves outside the confirmed
        tree, and persisting that resolution would scope the folder to a
        directory the user never approved. The swap is staged inside the scan
        wrapper so it lands in exactly that window.
        """

        root = _sibling_repos(tmp_path / "work")
        outside = tmp_path / "outside"
        outside.mkdir()

        async def _scan_then_swap(scan_root: str) -> Any:
            tree = await _scan_off_loop(scan_root)
            api = root / "api"
            api.rename(tmp_path / "moved-aside")
            api.symlink_to(outside, target_is_directory=True)
            return tree

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_folder_scaffold._scan_off_loop", _scan_then_swap
        )
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, root, [root / "api", root / "web"])

        assert status == 200
        failed = {entry["path"]: entry for entry in body["failed"]}
        assert set(failed) == {str(root / "api")}
        assert failed[str(root / "api")]["code"] == "folder_project_dir_moved"
        dirs = _by_project_dir(state)
        assert str(outside) not in dirs
        assert str(root / "api") not in dirs
        # The swap costs only its own path: the rest of the selection lands.
        assert str(root / "web") in dirs

    @pytest.mark.asyncio
    async def test_the_root_swapped_for_a_symlink_after_the_scan_is_refused(
        self, state: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A swapped root is refused before any folder is created."""

        root = _sibling_repos(tmp_path / "work")
        outside = tmp_path / "outside"
        outside.mkdir()

        async def _scan_then_swap(scan_root: str) -> Any:
            tree = await _scan_off_loop(scan_root)
            root.rename(tmp_path / "moved-aside")
            root.symlink_to(outside, target_is_directory=True)
            return tree

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_folder_scaffold._scan_off_loop", _scan_then_swap
        )
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scaffold(client, root, [root / "api"])

        assert status == 200
        assert body["created"] == []
        assert [entry["path"] for entry in body["failed"]] == [str(root)]
        assert body["failed"][0]["code"] == "folder_project_dir_moved"
        assert state._folders == []


# Filesystem work plus an aiohttp server per example is far past Hypothesis'
# default per-example deadline. The shared profile already lifts it; restating it
# keeps this property correct if that profile is ever narrowed, while leaving
# ``max_examples`` to the profile.
_P7_SETTINGS = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])


def _additive_layout(base: Path) -> tuple[Path, list[str]]:
    """A tree with candidates at three levels, plus the paths it offers.

    Fixed rather than generated: the property below quantifies over *selections*
    and over which folders already exist, and the scanner's own answer for random
    trees is what the engine property suite covers.
    """

    root = base / "work"
    (root / "api" / ".git").mkdir(parents=True)
    (root / "web" / ".git").mkdir(parents=True)
    (root / "web" / "pkg").mkdir()
    (root / "web" / "pkg" / "package.json").write_text("{}", encoding="utf-8")
    (root / "web" / "pkg" / "inner" / ".kiro").mkdir(parents=True)
    return root, [
        str(root / "api"),
        str(root / "web"),
        str(root / "web" / "pkg"),
        str(root / "web" / "pkg" / "inner"),
    ]


_CANDIDATE_COUNT = 4
_FLAGS = st.lists(st.booleans(), min_size=_CANDIDATE_COUNT, max_size=_CANDIDATE_COUNT)


def _preexisting_folder(index: int, project_dir: str) -> dict[str, Any]:
    """A folder already in the store, carrying a value nothing may rewrite."""

    return {
        "id": f"pre{index}",
        "name": f"kept-{index}",
        "order": index,
        "collapsed": True,
        "hidden": False,
        "parent_id": "",
        "project_dir": project_dir,
        "default_agent": "",
        # Not a field the scaffold path writes; present so a settings-preserving
        # assertion has something to be about beyond the fields it does write.
        "color": "#22c55e",
    }


class TestAdditiveScaffold:
    """Property 7: scaffolding only ever adds.

    Whatever the selection and whatever is already in the store, a scaffold call
    leaves every folder that existed before it byte-identical, and the folders it
    adds are exactly the selected candidates that had none. This is the property
    that makes re-scanning a growing project safe: the user's existing setup —
    names, colors, placements, the conversations filed under them — is not
    something a re-run may rewrite.
    """

    @_P7_SETTINGS
    @given(selection=_FLAGS, preexisting=_FLAGS, root_exists=st.booleans())
    def test_scaffolding_only_adds_folders(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        selection: list[bool],
        preexisting: list[bool],
        root_exists: bool,
    ) -> None:
        # Per-example directories: Hypothesis creates a fixture once per test, not
        # once per example, so the area is session-scoped and isolation comes from
        # a fresh sub-directory here.
        base = tmp_path_factory.mktemp("additive")
        home = base / "home"
        home.mkdir()
        root, offered = _additive_layout(base)
        selected = [path for path, ticked in zip(offered, selection) if ticked]
        already = [path for path, ticked in zip(offered, preexisting) if ticked]
        if root_exists:
            already = [str(root)] + already
        before = [
            _preexisting_folder(index, project_dir) for index, project_dir in enumerate(already)
        ]

        with mock.patch("kiro_crew.dashboard.state.config_dir", lambda: home):
            state = _make_state(base)
            state._folders = copy.deepcopy(before)
            body = asyncio.run(self._scaffold_once(state, root, selected))
            after = copy.deepcopy(state._folders)
            # Additive twice over: a second identical run is a no-op, which is
            # what a user re-scanning an unchanged project must get.
            repeat = asyncio.run(self._scaffold_once(state, root, selected))

        by_id = {folder["id"]: folder for folder in after}
        for folder in before:
            # Byte-identical, not merely present: no rename, no re-placement, no
            # settings change.
            assert by_id.get(folder["id"]) == folder

        expected_new = ({str(root)} | set(selected)) - set(already)
        assert body["failed"] == []
        assert {entry["path"] for entry in body["created"]} == expected_new
        offered_again = {str(root)} | set(selected)
        assert sorted(body["skipped_existing"]) == sorted(set(already) & offered_again)
        assert {folder["project_dir"] for folder in after} == set(already) | expected_new
        # Nothing dangling: every parent named by a created folder resolves.
        ids = set(by_id)
        assert all(folder["parent_id"] in ids or not folder["parent_id"] for folder in after)
        assert repeat["created"] == []
        assert state._folders == after

    @staticmethod
    async def _scaffold_once(state: Any, root: Path, selected: list[str]) -> dict[str, Any]:
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, body = await _scaffold(client, root, selected)
            return body


class TestRouteRegistration:
    """The handler must be reachable in the running dashboard, not just in
    the private apps these tests build. Registration lives in the route
    table (``dashboard/routes/``, one module per section; ``sessions``
    carries the folder routes), so this guard inspects that module's source
    the same way the repo's YAML guard inspects call sites -- it fails if the ``add_post`` line for the
    scan route is ever dropped."""

    def test_facade_reexports_the_scan_handler(self) -> None:
        from kiro_crew.dashboard import chat

        assert chat.api_chat_folders_scan is api_chat_folders_scan

    def test_facade_reexports_the_scaffold_handler(self) -> None:
        from kiro_crew.dashboard import chat

        assert chat.api_chat_folders_scaffold is api_chat_folders_scaffold

    def test_route_table_registers_the_scan_route(self) -> None:
        import inspect

        from kiro_crew.dashboard.routes import sessions

        source = inspect.getsource(sessions)
        assert (
            'add_post("/api/project-scaffold/scan", chat.api_chat_folders_scan)' in source
        ), "POST /api/project-scaffold/scan is not registered in the route table"

    def test_route_table_registers_the_scaffold_route(self) -> None:
        import inspect

        from kiro_crew.dashboard.routes import sessions

        source = inspect.getsource(sessions)
        assert (
            'add_post("/api/project-scaffold/create", chat.api_chat_folders_scaffold)' in source
        ), "POST /api/project-scaffold/create is not registered in the route table"


def _make_scaffold_app_with_claim(state: Any, app_claim: str) -> web.Application:
    """The scaffold endpoints behind a stand-in for the token middleware.

    Mirrors ``test_chat_folder_ownership.py``: the middleware publishes the
    validated app claim, which is the only place ``_effective_request_app``
    may read it from — never the body.
    """

    app = _make_scaffold_app(state)

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        request["app"] = app_claim
        return await handler(request)

    app.middlewares.append(_publish_app)
    return app


class TestScaffoldOwnership:
    """Scaffolded folders get the SAME app-ownership isolation a hand-created
    folder gets: the caller's identity is derived from the middleware claim,
    stamped as ``owner_app``, and an unattributable caller is refused."""

    @pytest.mark.asyncio
    async def test_an_apps_scaffold_stamps_every_folder_with_that_app(
        self, state: Any, tmp_path: Path
    ) -> None:
        root = _sibling_repos(tmp_path / "work")
        selected = [str(root / "api"), str(root / "web")]
        app = _make_scaffold_app_with_claim(state, "issue-radar")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/project-scaffold/create", json={"root": str(root), "selected": selected}
            )
            assert resp.status == 200
            body = await resp.json()
        assert len(body["created"]) == 3
        assert [f.get("owner_app") for f in state._folders] == ["issue-radar"] * 3

    @pytest.mark.asyncio
    async def test_a_persons_scaffold_leaves_the_owner_key_absent(
        self, state: Any, tmp_path: Path
    ) -> None:
        # Absent, not empty: "absent means the person" is the one on-disk
        # representation (see chat_folders._folder_owner_app).
        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            resp = await client.post(
                "/api/project-scaffold/create", json={"root": str(root), "selected": None}
            )
            assert resp.status == 200
        assert state._folders and all("owner_app" not in f for f in state._folders)

    @pytest.mark.asyncio
    async def test_an_unattributable_caller_is_refused_before_anything_is_created(
        self, state: Any, tmp_path: Path
    ) -> None:
        # A ``dashboard:`` session key naming a popped slot cannot be
        # attributed, so handing it the person's authority over the person's
        # folder tree is refused — the same rule the folder-create route
        # applies to this write.
        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            resp = await client.post(
                "/api/project-scaffold/create",
                json={"root": str(root), "selected": None},
                headers={"X-Session-Key": "dashboard:gone-9-9"},
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "caller_unattributable"
        assert state._folders == []

    @pytest.mark.asyncio
    async def test_an_internal_caller_is_rate_limited_at_the_endpoint(
        self, state: Any, tmp_path: Path
    ) -> None:
        """One scaffold call consumes one budget unit; without this check the
        scaffold route is the loophole around the create route's limiter.

        Mutation guard: drop the ``allow_create`` call and the burst all
        returns 200.
        """
        from kiro_crew.dashboard import create_rate_limit

        create_rate_limit.reset_for_tests()
        try:
            root = _sibling_repos(tmp_path / "work")
            headers = {
                "X-Internal-Secret": "s3cret",
                "X-Internal-Caller": "kirocrew-dashboard",
            }
            async with TestClient(TestServer(_make_scaffold_app(state))) as client:
                allowed = 0
                for _ in range(create_rate_limit.MAX_FOLDER_CREATES_PER_WINDOW + 3):
                    resp = await client.post(
                        "/api/project-scaffold/create",
                        json={"root": str(root), "selected": None},
                        headers=headers,
                    )
                    if resp.status == 200:
                        allowed += 1
                    else:
                        assert resp.status == 429
                        assert (await resp.json())["code"] == "create_rate_limited"
            assert allowed == create_rate_limit.MAX_FOLDER_CREATES_PER_WINDOW
        finally:
            create_rate_limit.reset_for_tests()

    @pytest.mark.asyncio
    async def test_the_browser_is_not_rate_limited(self, state: Any, tmp_path: Path) -> None:
        # Same carve-out as the create route: a request without the internal
        # secret is the person's own browser.
        from kiro_crew.dashboard import create_rate_limit

        create_rate_limit.reset_for_tests()
        try:
            root = _sibling_repos(tmp_path / "work")
            async with TestClient(TestServer(_make_scaffold_app(state))) as client:
                for _ in range(create_rate_limit.MAX_FOLDER_CREATES_PER_WINDOW + 3):
                    resp = await client.post(
                        "/api/project-scaffold/create", json={"root": str(root), "selected": None}
                    )
                    assert resp.status == 200, "a browser scaffold must never be throttled"
        finally:
            create_rate_limit.reset_for_tests()


class TestSensitiveRootContainment:
    """A root that is an ANCESTOR of a protected location is refused before any
    work starts: the scan sweeps everything below the root, so containment is
    the direction the folder validator's own is-this-path-protected check
    cannot answer."""

    @pytest.mark.asyncio
    async def test_a_root_containing_a_sensitive_path_is_refused(
        self, state: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _sibling_repos(tmp_path / "work")
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_folder_scaffold.path_contains_sensitive",
            lambda p: str(p) == str(root),
        )
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            for url in ("/api/project-scaffold/scan", "/api/project-scaffold/create"):
                resp = await client.post(url, json={"root": str(root)})
                assert resp.status == 400
                assert (await resp.json())["code"] == "folder_scan_root_invalid"
        assert state._folders == []


class TestConcurrentScaffoldDedup:
    """Two concurrent scaffolds of the same tree persist ONE folder per
    directory: the uniqueness check runs inside the locked append, so both
    racers cannot observe a path unclaimed and both create it."""

    @pytest.mark.asyncio
    async def test_concurrent_scaffolds_create_each_folder_exactly_once(
        self, state: Any, tmp_path: Path
    ) -> None:
        root = _sibling_repos(tmp_path / "work")
        selected = [str(root / "api"), str(root / "web")]
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            r1, r2 = await asyncio.gather(
                client.post(
                    "/api/project-scaffold/create",
                    json={"root": str(root), "selected": selected},
                ),
                client.post(
                    "/api/project-scaffold/create",
                    json={"root": str(root), "selected": selected},
                ),
            )
            assert r1.status == 200 and r2.status == 200
            b1, b2 = await r1.json(), await r2.json()
        # One folder per directory, whatever the interleaving.
        dirs = [str(f.get("project_dir") or "") for f in state._folders]
        assert sorted(dirs) == sorted([str(root), str(root / "api"), str(root / "web")])
        # Every directory is accounted for on both responses (created or
        # skipped), and nothing is reported failed.
        for body in (b1, b2):
            assert body["failed"] == []
            covered = {c["path"] for c in body["created"]} | set(body["skipped_existing"])
            assert covered == {str(root), str(root / "api"), str(root / "web")}
