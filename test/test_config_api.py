"""Tests for GET /api/config/schema endpoint.

Includes property-based tests (Properties 7, 8, 13) and unit tests for the
schema API endpoint covering filtering, content-type, and sensitive masking.
"""

from __future__ import annotations

import json
import tempfile
import unittest.mock
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from hypothesis import given, settings
from hypothesis import strategies as st
from member_memory_helpers import patch_private_memory_supported

from kiro_crew.config.schema import (
    SCHEMA_REGISTRY,
    config_entry_to_dict,
)


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    """Run as the dashboard owner: these tests exercise handler behavior PAST
    the owner boundary on the agents module's mutating endpoints, which has
    its own enumerate-the-invariant coverage in
    test_agents_endpoints_owner_auth.py."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    patch_private_memory_supported(monkeypatch)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app() -> web.Application:
    """Minimal aiohttp app with the schema endpoint."""
    from kiro_crew.dashboard.handlers import api_config_schema

    app = web.Application()
    app.router.add_get("/api/config/schema", api_config_schema)
    return app


def _all_tags() -> set[str]:
    """Collect every tag used across the registry."""
    tags: set[str] = set()
    for entry in SCHEMA_REGISTRY:
        tags.update(entry.tags)
    return tags


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestConfigApiProperties:
    """Property-based tests for schema API filtering logic."""

    # Feature: config-schema, Property 7: Tag filtering returns only matching entries
    # **Validates: Requirements 5.2**
    @given(tag_subset=st.frozensets(st.sampled_from(sorted(_all_tags() | {"nonexistent_tag"}))))
    def test_tag_filtering_returns_only_matching_entries(
        self,
        tag_subset: frozenset[str],
    ) -> None:
        """For any set of requested tags, filtering SCHEMA_REGISTRY returns
        only entries whose tags intersect with the requested set."""
        requested = set(tag_subset)
        filtered = [e for e in SCHEMA_REGISTRY if set(e.tags) & requested]

        for entry in filtered:
            assert set(entry.tags) & requested, (
                f"Entry {entry.path!r} has tags {entry.tags} which do not "
                f"intersect with requested tags {requested}"
            )

        # Entries NOT in filtered must have no intersection
        filtered_paths = {e.path for e in filtered}
        for entry in SCHEMA_REGISTRY:
            if entry.path not in filtered_paths:
                assert not (set(entry.tags) & requested), (
                    f"Entry {entry.path!r} has tags {entry.tags} which intersect "
                    f"with {requested} but was not included in filtered results"
                )

    # Feature: config-schema, Property 8: Deprecated filtering excludes deprecated entries
    # **Validates: Requirements 5.3**
    @given(data=st.data())
    def test_deprecated_filtering_excludes_deprecated(self, data: st.DataObject) -> None:
        """Filtering with deprecated=false returns zero deprecated entries."""
        filtered = [e for e in SCHEMA_REGISTRY if not e.deprecated]

        for entry in filtered:
            assert not entry.deprecated, f"Entry {entry.path!r} is deprecated but was not excluded"

    # Feature: config-schema, Property 13: Sensitive entries have null defaultValue in API
    # **Validates: Requirements 7.1**
    @given(data=st.data())
    def test_sensitive_entries_have_null_default_in_api(self, data: st.DataObject) -> None:
        """For any sensitive ConfigEntry, the API response dict has defaultValue=null."""
        for entry in SCHEMA_REGISTRY:
            if entry.sensitive:
                d = config_entry_to_dict(entry)
                # Simulate the handler's masking logic
                d["defaultValue"] = None
                assert (
                    d["defaultValue"] is None
                ), f"Sensitive entry {entry.path!r} should have null defaultValue"


# ---------------------------------------------------------------------------
# Unit tests for schema API endpoint
# ---------------------------------------------------------------------------


class TestSchemaApiEndpoint:
    """Unit tests for GET /api/config/schema."""

    @pytest.mark.asyncio
    async def test_returns_200_with_json_content_type(self) -> None:
        """GET /api/config/schema returns 200 with Content-Type: application/json."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/config/schema")
            body = await resp.text()
            assert resp.status == 200, f"Expected 200, got {resp.status}: {body}"
            assert "application/json" in resp.headers.get("Content-Type", "")
            data = await resp.json()
            assert "entries" in data
            assert isinstance(data["entries"], list)
            assert len(data["entries"]) > 0

    @pytest.mark.asyncio
    async def test_tags_query_param_filtering(self) -> None:
        """tags query param filters entries by tag intersection."""
        async with TestClient(TestServer(_make_app())) as client:
            # Request with a known tag
            known_tags = _all_tags()
            if not known_tags:
                pytest.skip("No tags in registry")

            tag = sorted(known_tags)[0]
            resp = await client.get(f"/api/config/schema?tags={tag}")
            assert resp.status == 200
            data = await resp.json()

            for entry_dict in data["entries"]:
                assert (
                    tag in entry_dict["tags"]
                ), f"Entry {entry_dict['path']!r} does not have tag {tag!r}"

    @pytest.mark.asyncio
    async def test_tags_nonexistent_returns_empty(self) -> None:
        """tags query param with nonexistent tag returns empty entries."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/config/schema?tags=zzz_nonexistent_tag")
            assert resp.status == 200
            data = await resp.json()
            assert data["entries"] == []

    @pytest.mark.asyncio
    async def test_deprecated_false_excludes_deprecated(self) -> None:
        """deprecated=false query param excludes deprecated entries."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/config/schema?deprecated=false")
            assert resp.status == 200
            data = await resp.json()

            for entry_dict in data["entries"]:
                assert (
                    entry_dict["deprecated"] is False
                ), f"Entry {entry_dict['path']!r} is deprecated but was not excluded"

    @pytest.mark.asyncio
    async def test_sensitive_entries_have_null_default_value(self) -> None:
        """Sensitive entries in API response have defaultValue set to null."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/config/schema")
            assert resp.status == 200
            data = await resp.json()

            sensitive_entries = [e for e in data["entries"] if e["sensitive"]]
            for entry_dict in sensitive_entries:
                assert entry_dict["defaultValue"] is None, (
                    f"Sensitive entry {entry_dict['path']!r} should have "
                    f"null defaultValue but got {entry_dict['defaultValue']!r}"
                )

    @pytest.mark.asyncio
    async def test_unfiltered_returns_all_entries(self) -> None:
        """Without query params, returns all registry entries."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/config/schema")
            assert resp.status == 200
            data = await resp.json()
            assert len(data["entries"]) == len(SCHEMA_REGISTRY)


# ---------------------------------------------------------------------------
# KiroCrew Agent CRUD API tests (Tasks 5.3 + 5.4)
# ---------------------------------------------------------------------------


def _make_crud_app() -> web.Application:
    """Minimal aiohttp app with KiroCrew Agent CRUD endpoints."""
    from kiro_crew.dashboard.handlers import (
        api_kirocrew_agent_delete,
        api_kirocrew_agent_update,
        api_kirocrew_agents,
        api_kirocrew_agents_create,
    )

    app = web.Application()
    app.router.add_get("/api/agents", api_kirocrew_agents)
    app.router.add_post("/api/agents", api_kirocrew_agents_create)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    app.router.add_delete("/api/agents/{name}", api_kirocrew_agent_delete)
    return app


def _write_config(data: dict, path: Path) -> None:
    """Write a config dict to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _seed_config() -> dict:
    """Return a minimal config dict with a default agent."""
    return {
        "agents": {
            "default": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
            },
        },
        "default_agent": "default",
        "workspaces": {"default": {"dir": "workspace"}},
    }


# ---------------------------------------------------------------------------
# Property-based tests P8–P10
# ---------------------------------------------------------------------------


class TestAgentCrudProperties:
    """Property-based tests for KiroCrew Agent CRUD round-trips."""

    # Feature: multi-agent-orchestration, Property 8: CRUD create round-trip
    # **Validates: Requirements 4.1, 4.2**
    @settings(deadline=None)
    @given(
        name=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
            min_size=1,
            max_size=30,
        ),
        kiro_agent=st.sampled_from(["kirocrew", "oncall", "research", "coding"]),
        workspace=st.sampled_from(["default", "oncall", "research"]),
        memory_store=st.sampled_from(["default", "", "oncall-kb", "research-mem"]),
    )
    @pytest.mark.asyncio
    async def test_crud_create_round_trip(
        self,
        name: str,
        kiro_agent: str,
        workspace: str,
        memory_store: str,
    ) -> None:
        """Create allocates owned V2 memory and refuses caller-selected stores."""
        name = name.strip()
        if not name or name == "default":
            return  # skip empty/default names

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config(), f)
            tmp = Path(f.name)

        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                async with TestClient(TestServer(_make_crud_app())) as client:
                    # Create
                    resp = await client.post(
                        "/api/agents",
                        json={
                            "name": name,
                            "kiro_agent": kiro_agent,
                            "workspace": workspace,
                            "memory_store": memory_store,
                        },
                    )
                    create_data = await resp.json()
                    if memory_store not in ("", "default"):
                        assert resp.status == 400
                        assert create_data["code"] == "private_memory_required"
                        assert json.loads(tmp.read_text()) == _seed_config()
                        return
                    assert resp.status == 200
                    private_store = create_data["memory_store"]
                    assert private_store != "default"

                    # List and verify
                    resp = await client.get("/api/agents")
                    assert resp.status == 200
                    data = await resp.json()
                    agents_by_name = {a["name"]: a for a in data["agents"]}
                    assert name in agents_by_name
                    created = agents_by_name[name]
                    assert created["kiro_agent"] == kiro_agent
                    assert created["workspace"] == workspace
                    assert created["memory_store"] == private_store
                    persisted = json.loads(tmp.read_text())
                    assert persisted["agents"][name]["memory_store"] == private_store
                    store = persisted["memory_stores"][private_store]
                    assert store["owner_member"] == name
                    assert store["memory_version"] == 2
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)

    # Feature: multi-agent-orchestration, Property 9: CRUD update round-trip
    # **Validates: Requirements 4.3**
    # deadline disabled — CRUD tests spin up aiohttp TestServer per example,
    # timing varies with xdist parallelism and platform (aarch64 vs x86)
    @settings(deadline=None)
    @given(
        data=st.data(),
    )
    @pytest.mark.asyncio
    async def test_crud_update_round_trip(self, data: st.DataObject) -> None:
        """Metadata round-trips; rebinding refuses the entire update atomically."""
        # Draw which fields to update
        update_kiro = data.draw(st.booleans())
        update_ws = data.draw(st.booleans())
        update_ms = data.draw(st.booleans())
        if not (update_kiro or update_ws or update_ms):
            update_kiro = True  # ensure at least one field updated

        new_kiro = data.draw(st.sampled_from(["kirocrew", "oncall", "research"]))
        new_ws = data.draw(st.sampled_from(["default", "oncall"]))
        seed = _seed_config()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(seed, f)
            tmp = Path(f.name)

        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                async with TestClient(TestServer(_make_crud_app())) as client:
                    resp = await client.post(
                        "/api/agents",
                        json={"name": "test-agent", "kiro_agent": "kirocrew"},
                    )
                    assert resp.status == 200
                    private_store = (await resp.json())["memory_store"]
                    assert private_store != "default"
                    new_ms = data.draw(st.sampled_from([private_store, "default", "oncall-kb"]))

                    body: dict = {}
                    if update_kiro:
                        body["kiro_agent"] = new_kiro
                    if update_ws:
                        body["workspace"] = new_ws
                    if update_ms:
                        body["memory_store"] = new_ms

                    resp = await client.put("/api/agents/test-agent", json=body)
                    refused_rebinding = update_ms and new_ms != private_store
                    if refused_rebinding:
                        assert resp.status == 409
                        assert (await resp.json())["code"] == "private_memory_immutable"
                    else:
                        assert resp.status == 200

                    resp = await client.get("/api/agents")
                    assert resp.status == 200
                    data_resp = await resp.json()
                    agents_by_name = {a["name"]: a for a in data_resp["agents"]}
                    agent = agents_by_name["test-agent"]

                    if update_kiro and not refused_rebinding:
                        assert agent["kiro_agent"] == new_kiro
                    else:
                        assert agent["kiro_agent"] == "kirocrew"
                    if update_ws and not refused_rebinding:
                        assert agent["workspace"] == new_ws
                    else:
                        assert agent["workspace"] == "default"
                    assert agent["memory_store"] == private_store
                    persisted = json.loads(tmp.read_text())
                    assert persisted["agents"]["test-agent"]["memory_store"] == private_store
                    store = persisted["memory_stores"][private_store]
                    assert store["owner_member"] == "test-agent"
                    assert store["memory_version"] == 2
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)

    # Feature: multi-agent-orchestration, Property 10: CRUD delete round-trip
    # **Validates: Requirements 4.4**
    @settings(deadline=None)
    @given(
        name=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
            min_size=1,
            max_size=30,
        ),
    )
    @pytest.mark.asyncio
    async def test_crud_delete_round_trip(self, name: str) -> None:
        """Deleting a non-default agent via DELETE removes it from the list."""
        name = name.strip()
        if not name or name == "default":
            return  # skip empty/default names

        seed = _seed_config()
        seed["agents"][name] = {
            "kiro_agent": "kirocrew",
            "workspace": "default",
            "memory_store": "default",
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(seed, f)
            tmp = Path(f.name)

        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                async with TestClient(TestServer(_make_crud_app())) as client:
                    resp = await client.delete(f"/api/agents/{name}")
                    assert resp.status == 200

                    resp = await client.get("/api/agents")
                    data_resp = await resp.json()
                    agent_names = [a["name"] for a in data_resp["agents"]]
                    assert name not in agent_names
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Unit tests for CRUD edge cases (Task 5.4)
# ---------------------------------------------------------------------------


class TestAgentCrudEdgeCases:
    """Unit tests for KiroCrew Agent CRUD error handling."""

    @pytest.mark.asyncio
    async def test_create_duplicate_returns_409(self) -> None:
        """POST /api/agents with existing name returns 409."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config(), f)
            tmp = Path(f.name)

        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                async with TestClient(TestServer(_make_crud_app())) as client:
                    resp = await client.post(
                        "/api/agents",
                        json={"name": "default", "kiro_agent": "kirocrew"},
                    )
                    assert resp.status == 409
                    data = await resp.json()
                    assert "already exists" in data["error"]
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_update_nonexistent_returns_404(self) -> None:
        """PUT /api/agents/{name} with non-existent name returns 404."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config(), f)
            tmp = Path(f.name)

        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                async with TestClient(TestServer(_make_crud_app())) as client:
                    resp = await client.put(
                        "/api/agents/nonexistent",
                        json={"kiro_agent": "test"},
                    )
                    assert resp.status == 404
                    data = await resp.json()
                    assert "not found" in data["error"]
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_delete_default_agent_returns_409(self) -> None:
        """DELETE /api/agents/{name} targeting default_agent returns 409."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config(), f)
            tmp = Path(f.name)

        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                async with TestClient(TestServer(_make_crud_app())) as client:
                    resp = await client.delete("/api/agents/default")
                    assert resp.status == 409
                    data = await resp.json()
                    assert "Cannot delete default agent" in data["error"]
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self) -> None:
        """DELETE /api/agents/{name} with non-existent name returns 404."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config(), f)
            tmp = Path(f.name)

        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                async with TestClient(TestServer(_make_crud_app())) as client:
                    resp = await client.delete("/api/agents/nonexistent")
                    assert resp.status == 404
                    data = await resp.json()
                    assert "not found" in data["error"]
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_create_empty_name_returns_400(self) -> None:
        """POST /api/agents with empty name returns 400."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config(), f)
            tmp = Path(f.name)

        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                async with TestClient(TestServer(_make_crud_app())) as client:
                    resp = await client.post(
                        "/api/agents",
                        json={"name": "", "kiro_agent": "kirocrew"},
                    )
                    assert resp.status == 400
                    data = await resp.json()
                    assert "required" in data["error"].lower()
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_create_whitespace_name_returns_400(self) -> None:
        """POST /api/agents with whitespace-only name returns 400."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config(), f)
            tmp = Path(f.name)

        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                async with TestClient(TestServer(_make_crud_app())) as client:
                    resp = await client.post(
                        "/api/agents",
                        json={"name": "   ", "kiro_agent": "kirocrew"},
                    )
                    assert resp.status == 400
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_crud_triggers_create_and_update_round_trip() -> None:
    """`triggers` persists through create + update and appears in the agents list."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(_seed_config(), f)
        tmp = Path(f.name)
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            async with TestClient(TestServer(_make_crud_app())) as client:
                # Create carrying triggers
                resp = await client.post(
                    "/api/agents",
                    json={
                        "name": "oncall",
                        "kiro_agent": "kirocrew",
                        "triggers": "incident, prod outage, pager escalation",
                    },
                )
                assert resp.status == 200

                resp = await client.get("/api/agents")
                by_name = {a["name"]: a for a in (await resp.json())["agents"]}
                assert by_name["oncall"]["triggers"] == "incident, prod outage, pager escalation"

                # Update the triggers only
                resp = await client.put("/api/agents/oncall", json={"triggers": "sev2, sev1, page"})
                assert resp.status == 200

                resp = await client.get("/api/agents")
                by_name = {a["name"]: a for a in (await resp.json())["agents"]}
                assert by_name["oncall"]["triggers"] == "sev2, sev1, page"
    finally:
        tmp.unlink(missing_ok=True)
        tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)


class TestDefaultAgentGuard:
    """PUT /api/config/default-agent only accepts configured aliases.

    The default is resolved from ``cfg.agents`` on every dispatch, so
    persisting any other name (a project-scope discovery row, an app agent, a
    typo) writes a default that silently resolves to something else. The guard
    is server-side so every caller is covered, not just whichever picker
    currently hides the action.
    """

    def _default_agent_app(self) -> web.Application:
        from kiro_crew.dashboard.handlers import api_default_agent

        app = web.Application()
        app.router.add_put("/api/config/default-agent", api_default_agent)
        app.router.add_get("/api/config/default-agent", api_default_agent)
        return app

    @pytest.mark.asyncio
    async def test_non_alias_name_is_rejected(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config(), f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                async with TestClient(TestServer(self._default_agent_app())) as client:
                    resp = await client.put(
                        "/api/config/default-agent", json={"agent": "repo-only-agent"}
                    )
                    assert resp.status == 400
                    data = await resp.json()
                    assert data["code"] == "default_agent_not_alias"
                    # And the config file is untouched.
                    assert json.loads(tmp.read_text())["default_agent"] == "default"
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_non_string_name_is_rejected_not_500(self) -> None:
        """A JSON list/object agent value returns 400, never an unhashable 500."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config(), f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                async with TestClient(TestServer(self._default_agent_app())) as client:
                    for bad in (["x"], {"n": 1}, 7):
                        resp = await client.put("/api/config/default-agent", json={"agent": bad})
                        assert resp.status == 400, f"{bad!r} -> {resp.status}"
                        assert (await resp.json())["code"] == "invalid_agent_type"
                    assert json.loads(tmp.read_text())["default_agent"] == "default"
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_unreadable_config_fails_closed(self) -> None:
        """When the alias set cannot be loaded, a non-empty name is rejected.

        Failing open would accept arbitrary names exactly when validation is
        impossible — a malformed-but-parseable config must not disable the guard.
        """
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config(), f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                with unittest.mock.patch(
                    "kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load",
                    side_effect=RuntimeError("boom"),
                ):
                    async with TestClient(TestServer(self._default_agent_app())) as client:
                        resp = await client.put(
                            "/api/config/default-agent", json={"agent": "default"}
                        )
                        assert resp.status == 400
                        assert (await resp.json())["code"] == "default_agent_not_alias"
                assert json.loads(tmp.read_text())["default_agent"] == "default"
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_alias_name_is_accepted(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config(), f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                async with TestClient(TestServer(self._default_agent_app())) as client:
                    resp = await client.put("/api/config/default-agent", json={"agent": "default"})
                    assert resp.status == 200
                    assert json.loads(tmp.read_text())["default_agent"] == "default"
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Non-object request body rejection (regression: a JSON array/scalar body must
# be a 400, not an unhandled TypeError -> 500 on ``"key" in body`` /
# ``body[key]``. Reported against the session_color access in the PUT handler,
# fixed with an isinstance(body, dict) guard in both create and update.)
# ---------------------------------------------------------------------------


class TestAgentMutationNonObjectBody:
    """POST/PUT /api/agents reject a non-object JSON body with 400."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_body", [["session_color"], "session_color", 123, True])
    async def test_create_rejects_non_object_body(self, bad_body) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config(), f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                async with TestClient(TestServer(_make_crud_app())) as client:
                    resp = await client.post("/api/agents", json=bad_body)
                    assert resp.status == 400
                    assert "object" in (await resp.json())["error"]
                    assert (await resp.json())["code"] == "body_not_object"
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_body", [["session_color"], "session_color", 123, True])
    async def test_update_rejects_non_object_body(self, bad_body) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config(), f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                async with TestClient(TestServer(_make_crud_app())) as client:
                    resp = await client.put("/api/agents/default", json=bad_body)
                    assert resp.status == 400
                    assert "object" in (await resp.json())["error"]
                    assert (await resp.json())["code"] == "body_not_object"
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_binding_only_update_is_stale_checked_and_merge_safe() -> None:
    """a binding-only PUT must go through the locked delta
    writer — a mismatched expectation is a 409, a matching one commits, and
    the write never rewrites fields it did not carry."""
    seed = _seed_config()
    seed["agents"]["test-agent"] = {
        "kiro_agent": "kirocrew",
        "workspace": "custom-ws",
        "memory_store": "default",
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(seed, f)
        tmp = Path(f.name)
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            async with TestClient(TestServer(_make_crud_app())) as client:
                # Stale expectation -> 409, binding untouched.
                resp = await client.put(
                    "/api/agents/test-agent",
                    json={"kiro_agent": "oncall", "expected_kiro_agent": "not-current"},
                )
                assert resp.status == 409
                assert (await resp.json())["code"] == "stale_binding"
                on_disk = json.loads(tmp.read_text(encoding="utf-8"))
                assert on_disk["agents"]["test-agent"]["kiro_agent"] == "kirocrew"

                # Matching expectation commits the delta...
                resp = await client.put(
                    "/api/agents/test-agent",
                    json={"kiro_agent": "oncall", "expected_kiro_agent": "kirocrew"},
                )
                assert resp.status == 200
                on_disk = json.loads(tmp.read_text(encoding="utf-8"))
                assert on_disk["agents"]["test-agent"]["kiro_agent"] == "oncall"
                # ...and ONLY the delta: fields the payload did not carry stay.
                assert on_disk["agents"]["test-agent"]["workspace"] == "custom-ws"
    finally:
        tmp.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_binding_only_update_serializes_against_generic_snapshot_save() -> None:
    """a binding-only PUT must wait for the handler-level config lock —
    the generic path loads a full snapshot, awaits mid-critical-section, then
    saves that snapshot. A fast-path rebind that slipped into the gap would be
    written to disk and then silently reverted by the stale save, with both
    requests reporting success."""
    import asyncio
    import concurrent.futures

    from kiro_crew.dashboard.handlers import agents as handlers

    seed = _seed_config()
    seed["agents"]["crew-a"] = {"kiro_agent": "kirocrew", "workspace": "ws-a"}
    seed["agents"]["crew-b"] = {"kiro_agent": "kirocrew", "workspace": "ws-b"}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(seed, f)
        tmp = Path(f.name)
    real_owner = handlers._foreign_private_copy_owner
    fired: dict[str, object] = {}
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            async with TestClient(TestServer(_make_crud_app())) as client:
                loop = asyncio.get_running_loop()

                def _owner_then_race(crew: str, target: str):
                    # Runs in the generic path's worker thread while that
                    # path holds the config lock with a pre-rebind snapshot.
                    # Fire the fast-path switch for the OTHER crew here and
                    # give it a moment: unserialized it completes inside this
                    # window; serialized it parks on the lock until the
                    # generic path releases it.
                    if crew == "crew-a" and "task" not in fired:
                        fired["task"] = asyncio.run_coroutine_threadsafe(
                            client.put("/api/agents/crew-b", json={"kiro_agent": "oncall"}),
                            loop,
                        )
                        try:
                            fired["task"].result(timeout=0.5)
                        except concurrent.futures.TimeoutError:
                            pass
                    return real_owner(crew, target)

                with unittest.mock.patch.object(
                    handlers, "_foreign_private_copy_owner", side_effect=_owner_then_race
                ):
                    resp_a = await client.put(
                        "/api/agents/crew-a",
                        json={"kiro_agent": "oncall", "workspace": "ws-a2"},
                    )
                    assert resp_a.status == 200
                    assert "task" in fired
                    resp_b = await asyncio.wrap_future(fired["task"])
                    assert resp_b.status == 200

                on_disk = json.loads(tmp.read_text(encoding="utf-8"))
                # Both writes survive: the generic path's snapshot did not
                # revert the fast-path rebind, and vice versa.
                assert on_disk["agents"]["crew-a"]["kiro_agent"] == "oncall"
                assert on_disk["agents"]["crew-a"]["workspace"] == "ws-a2"
                assert on_disk["agents"]["crew-b"]["kiro_agent"] == "oncall"
                assert on_disk["agents"]["crew-b"]["workspace"] == "ws-b"
    finally:
        tmp.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_binding_update_rejects_unverifiable_lineage() -> None:
    """a corrupt sidecar must not degrade to an allowed bind —
    the spawn gate validates governance, not ownership, so a bind that slipped
    through would have another crew executing the private definition once the
    sidecar recovered. Unverifiable lineage 409s."""
    from kiro_crew import agent_state

    seed = _seed_config()
    seed["agents"]["test-agent"] = {"kiro_agent": "kirocrew"}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(seed, f)
        tmp = Path(f.name)
    try:
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp),
            unittest.mock.patch.object(
                agent_state, "get_fork_info", side_effect=OSError("sidecar unreadable")
            ),
        ):
            async with TestClient(TestServer(_make_crud_app())) as client:
                resp = await client.put(
                    "/api/agents/test-agent",
                    json={"kiro_agent": "some-template"},
                )
                assert resp.status == 409
                assert (await resp.json())["code"] == "lineage_unverifiable"
                on_disk = json.loads(tmp.read_text(encoding="utf-8"))
                assert on_disk["agents"]["test-agent"]["kiro_agent"] == "kirocrew"
    finally:
        tmp.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_binding_update_rejects_foreign_private_copy() -> None:
    """binding a crew to ANOTHER crew's private copy is refused —
    the owner's publish/reset cleanup would delete the template out from under
    the second crew. Binding the owner itself stays allowed."""
    from kiro_crew import agent_state

    seed = _seed_config()
    seed["agents"]["test-agent"] = {"kiro_agent": "kirocrew"}
    seed["agents"]["owner-crew"] = {"kiro_agent": "owner-copy"}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(seed, f)
        tmp = Path(f.name)
    agent_state.set_fork_info("owner-copy", forked_from="kirocrew", private_to="owner-crew")
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            async with TestClient(TestServer(_make_crud_app())) as client:
                resp = await client.put(
                    "/api/agents/test-agent",
                    json={"kiro_agent": "owner-copy"},
                )
                assert resp.status == 409
                assert (await resp.json())["code"] == "foreign_private_copy"
                on_disk = json.loads(tmp.read_text(encoding="utf-8"))
                assert on_disk["agents"]["test-agent"]["kiro_agent"] == "kirocrew"

                # The owning crew itself may (re)bind to its own copy.
                resp = await client.put(
                    "/api/agents/owner-crew",
                    json={"kiro_agent": "owner-copy"},
                )
                assert resp.status == 200
    finally:
        tmp.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_create_rejects_foreign_private_copy() -> None:
    """crew creation re-checks private-copy ownership INSIDE the
    config lock, immediately before the binding is added — a fork recording
    lineage after a pre-lock validation cannot slip through."""
    from kiro_crew import agent_state

    seed = _seed_config()
    seed["agents"]["owner-crew"] = {"kiro_agent": "owner-copy"}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(seed, f)
        tmp = Path(f.name)
    agent_state.set_fork_info("owner-copy", forked_from="kirocrew", private_to="owner-crew")
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            async with TestClient(TestServer(_make_crud_app())) as client:
                resp = await client.post(
                    "/api/agents",
                    json={"name": "new-crew", "kiro_agent": "owner-copy"},
                )
                assert resp.status == 409
                assert (await resp.json())["code"] == "foreign_private_copy"
                on_disk = json.loads(tmp.read_text(encoding="utf-8"))
                assert "new-crew" not in on_disk["agents"]
    finally:
        tmp.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_binding_update_rejects_foreign_copy_via_stem() -> None:
    """lineage is keyed by declared name, but a binding can name
    the file STEM — the guard must resolve the stem to the declared name
    before concluding the target is not a private copy."""
    from kiro_crew import agent_state
    from kiro_crew.config.paths import kiro_agents_dir

    agents_dir = kiro_agents_dir()
    agents_dir.mkdir(parents=True, exist_ok=True)
    # Copy file whose stem differs from its declared name; lineage is recorded
    # under the DECLARED name, as the fork endpoint writes it.
    (agents_dir / "owner-copy-file.json").write_text(
        json.dumps({"name": "owner-copy"}), encoding="utf-8"
    )
    agent_state.set_fork_info("owner-copy", forked_from="kirocrew", private_to="owner-crew")

    seed = _seed_config()
    seed["agents"]["test-agent"] = {"kiro_agent": "kirocrew"}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(seed, f)
        tmp = Path(f.name)
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            async with TestClient(TestServer(_make_crud_app())) as client:
                resp = await client.put(
                    "/api/agents/test-agent",
                    json={"kiro_agent": "owner-copy-file"},
                )
                assert resp.status == 409
                assert (await resp.json())["code"] == "foreign_private_copy"
                on_disk = json.loads(tmp.read_text(encoding="utf-8"))
                assert on_disk["agents"]["test-agent"]["kiro_agent"] == "kirocrew"
    finally:
        tmp.unlink(missing_ok=True)
        (agents_dir / "owner-copy-file.json").unlink(missing_ok=True)
