"""``config.agents`` is keyed by ``member_id``; ``display_name`` is a field.

A legacy document keys the map by the crew's display name with the stable id
inside the record. These tests pin the keyed shape:

* MIGRATION -- an old-shape document (key = display name, ``member_id`` inside)
  loads re-keyed by the id with the old key kept as ``display_name``; the
  load writes nothing and the one-shot ``migrate_member_identity`` is idempotent; an id another entry already uses as its key is
  refused, not guessed; ``default_agent`` follows the re-key.
* RENAME -- ``PUT /api/agents/{name}`` with ``display_name`` edits the field
  only: the key, ``member_id``, the DM slot key and the memory store stay.
* ROSTER -- ``GET /api/agents`` and ``GET /api/members`` carry ``member_id``,
  ``display_name`` and ``name`` (an alias of ``display_name`` for one release).
* LOOKUP -- every handle route resolves the key AND the display name through
  ``members.resolve_member``; an unknown handle is 404.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew import members as members_mod
from kiro_crew.config import loader as loader_module
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.paths import config_dir


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _config_path() -> Path:
    return config_dir() / "config.json"


def _write_config(data: dict) -> Path:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    loader_module._invalidate_config_cache()
    return path


def _old_shape() -> dict:
    """A legacy document: agents keyed by display name, ids inside."""
    return {
        "agents": {
            "default": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
            },
            "Crew Program Manager": {
                "member_id": "crew-program-manager",
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "member-cpm",
            },
            "Dr. Eggbot": {
                "member_id": "dr-eggbot",
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "member-egg",
                "display_name": "Doctor Eggbot",
            },
        },
        "default_agent": "Crew Program Manager",
        "workspaces": {"default": {"dir": "workspace"}},
        "memory_stores": {
            "default": {},
            "member-cpm": {
                "owner_member": "Crew Program Manager",
                "owner_member_id": "crew-program-manager",
                "memory_version": 2,
            },
            "member-egg": {
                "owner_member": "Dr. Eggbot",
                "owner_member_id": "dr-eggbot",
                "memory_version": 2,
            },
        },
    }


class TestPlan:
    """The pure planner both halves of the migration share."""

    def test_rekeys_entries_whose_id_differs_from_their_key(self):
        plan = loader_module.plan_member_rekeys(
            [("Crew Program Manager", "crew-program-manager", ""), ("default", "", "")]
        )
        assert plan == {"Crew Program Manager": "crew-program-manager"}

    def test_entry_without_id_keeps_its_key(self):
        assert loader_module.plan_member_rekeys([("legacy", "", ""), ("other", None, "")]) == {}

    def test_id_already_used_as_a_key_is_refused(self, caplog):
        with caplog.at_level(logging.WARNING):
            plan = loader_module.plan_member_rekeys([("Writer", "writer", ""), ("writer", "", "")])
        assert plan == {}
        assert "cannot become their key" in caplog.text

    def test_id_claimed_by_two_entries_is_refused(self):
        plan = loader_module.plan_member_rekeys([("A", "shared", ""), ("B", "shared", "")])
        assert plan == {}

    def test_refused_move_does_not_vacate_its_key(self, caplog):
        # ``A`` and ``B`` both claim ``shared`` and are refused, so ``A`` STAYS
        # a key; ``X`` claiming ``A`` must be refused too, or ``X`` and ``A``
        # would be written under one key and one of them dropped.
        with caplog.at_level(logging.WARNING):
            plan = loader_module.plan_member_rekeys(
                [("A", "shared", ""), ("B", "shared", ""), ("X", "A", "")]
            )
        assert plan == {}
        assert "already a key that stays" in caplog.text

    def test_refusal_cascades_along_a_chain_onto_a_staying_key(self):
        # ``b -> c`` is refused because ``c`` stays; ``a -> b`` then finds ``b``
        # staying as well and is refused in turn.
        plan = loader_module.plan_member_rekeys([("a", "b", ""), ("b", "c", ""), ("c", "", "")])
        assert plan == {}
        # The same chain with ``c`` moving away lands every entry on its id.
        plan = loader_module.plan_member_rekeys([("a", "b", ""), ("b", "c", ""), ("c", "d", "")])
        assert plan == {"a": "b", "b": "c", "c": "d"}

    def test_document_rekey_never_drops_an_entry(self):
        agents = {
            "A": {"member_id": "shared"},
            "B": {"member_id": "shared"},
            "X": {"member_id": "A"},
        }
        rekeyed, plan = loader_module.rekey_agents_document(agents)
        assert plan == {}
        assert rekeyed == agents and len(rekeyed) == 3


class TestLoadMigration:
    def test_old_shape_loads_keyed_by_id_with_old_key_as_display_name(self):
        _write_config(_old_shape())
        cfg = KiroCrewConfig.load()
        assert set(cfg.agents) == {"default", "crew-program-manager", "dr-eggbot"}
        cpm = cfg.agents["crew-program-manager"]
        assert cpm.member_id == "crew-program-manager"
        assert cpm.display_name == "Crew Program Manager"
        # A display_name already set is kept; the old key is not forced over it.
        assert cfg.agents["dr-eggbot"].display_name == "Doctor Eggbot"
        # default_agent followed the re-key rather than being reassigned.
        assert cfg.default_agent == "crew-program-manager"

    def test_load_is_read_only_and_the_migration_lands_once(self):
        path = _write_config(_old_shape())
        before = path.read_text(encoding="utf-8")
        # A load serves the keyed shape in memory and writes nothing for it.
        cfg = KiroCrewConfig.load()
        assert set(cfg.agents) == {"default", "crew-program-manager", "dr-eggbot"}
        assert path.read_text(encoding="utf-8") == before
        # The one-shot moves the document, once.
        moved = loader_module.migrate_member_identity()
        assert moved["rekeyed"] == 2
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert set(on_disk["agents"]) == {"default", "crew-program-manager", "dr-eggbot"}
        assert on_disk["agents"]["crew-program-manager"]["display_name"] == "Crew Program Manager"
        assert on_disk["default_agent"] == "crew-program-manager"
        # Identity bindings are untouched by the re-key.
        assert on_disk["memory_stores"]["member-cpm"]["owner_member_id"] == "crew-program-manager"
        first = path.read_text(encoding="utf-8")
        loader_module._invalidate_config_cache()
        assert loader_module.migrate_member_identity()["rekeyed"] == 0
        assert path.read_text(encoding="utf-8") == first

    def test_a_record_whose_key_a_task_profile_binds_keeps_its_key_until_re_pointed(
        self, tmp_path, monkeypatch, caplog
    ):
        # Fail-secure: the profile keeps binding under the old key (in memory AND
        # on disk the record stays there), the refusal names the profile and the
        # exact re-point, and ``profiles/`` is never written. Once the operator
        # re-points the bind to the member_id, the next boot moves the record.
        from kiro_crew.platform import governance_profiles as gp

        profiles = tmp_path / "profiles"
        profiles.mkdir()
        monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
        gp.reset_store()
        profile = profiles / "cpm-ceiling.json"

        def _bind(task_id: str) -> None:
            profile.write_text(
                json.dumps(
                    {
                        "name": "cpm-ceiling",
                        "bind": {"type": "task", "id": task_id},
                        "capabilities": {"spawn": {"enabled": False}},
                    }
                ),
                encoding="utf-8",
            )
            gp.reset_store()

        _bind("Crew Program Manager")
        before = profile.read_bytes()
        path = _write_config(_old_shape())
        try:
            # The in-memory view refuses the move too: the bind keeps applying.
            cfg = KiroCrewConfig.load()
            assert "Crew Program Manager" in cfg.agents and "crew-program-manager" not in cfg.agents
            assert "dr-eggbot" in cfg.agents
            bound = gp.resolve_active_scope("subagent:abc", agent="Crew Program Manager")
            assert bound is not None and bound.name == "cpm-ceiling"
            with caplog.at_level("WARNING"):
                first = loader_module.migrate_member_identity()
            assert first["rekeyed"] == 1  # Dr. Eggbot moved; the bound record stayed
            on_disk = json.loads(path.read_text(encoding="utf-8"))["agents"]
            assert "Crew Program Manager" in on_disk and "crew-program-manager" not in on_disk
            assert "dr-eggbot" in on_disk
            assert profile.read_bytes() == before
            assert any(
                "governance profile 'cpm-ceiling' binds task:'Crew Program Manager'; re-point it to "
                "task:'crew-program-manager'" in rec.getMessage()
                for rec in caplog.records
            )
            # The operator re-points the bind; the next boot moves the record and
            # the ceiling follows it onto the id.
            _bind("crew-program-manager")
            loader_module._invalidate_config_cache()
            second = loader_module.migrate_member_identity()
            assert second["rekeyed"] == 1 and second["stale_task_binds"] == 0
            on_disk = json.loads(path.read_text(encoding="utf-8"))["agents"]
            assert "crew-program-manager" in on_disk and "Crew Program Manager" not in on_disk
            after = gp.resolve_active_scope("subagent:abc", agent="crew-program-manager")
            assert after is not None and after.name == "cpm-ceiling"
        finally:
            gp.reset_store()

    def test_an_unreadable_profile_refuses_every_re_key_until_it_is_fixed(
        self, tmp_path, monkeypatch, caplog
    ):
        # Fail closed: a profile present but unreadable might bind any key, so no
        # record moves (in memory or on disk) until the file is fixed or removed.
        from kiro_crew.platform import governance_profiles as gp

        profiles = tmp_path / "profiles"
        profiles.mkdir()
        monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
        gp.reset_store()
        broken = profiles / "broken.json"
        broken.write_text("{ not json", encoding="utf-8")
        path = _write_config(_old_shape())
        try:
            cfg = KiroCrewConfig.load()
            assert "Crew Program Manager" in cfg.agents and "Dr. Eggbot" in cfg.agents
            with caplog.at_level("WARNING"):
                report = loader_module.migrate_member_identity()
            assert report["rekeyed"] == 0
            assert set(json.loads(path.read_text(encoding="utf-8"))["agents"]) == {
                "default",
                "Crew Program Manager",
                "Dr. Eggbot",
            }
            assert any("not fully readable" in rec.getMessage() for rec in caplog.records)
            broken.unlink()
            gp.reset_store()
            loader_module._invalidate_config_cache()
            assert loader_module.migrate_member_identity()["rekeyed"] == 2
        finally:
            gp.reset_store()

    def test_a_profile_bound_after_the_move_is_reported_every_boot(
        self, tmp_path, monkeypatch, caplog
    ):
        # A bind authored against a key the record already left cannot be
        # protected by refusing a move that is done; it is named in a warning
        # with the exact re-point, every boot, and never rewritten.
        from kiro_crew.platform import governance_profiles as gp

        profiles = tmp_path / "profiles"
        profiles.mkdir()
        monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
        gp.reset_store()
        _write_config(_old_shape())
        try:
            assert loader_module.migrate_member_identity()["rekeyed"] == 2
            profile = profiles / "cpm-ceiling.json"
            profile.write_text(
                json.dumps(
                    {
                        "name": "cpm-ceiling",
                        "bind": {"type": "task", "id": "Crew Program Manager"},
                        "capabilities": {"spawn": {"enabled": False}},
                    }
                ),
                encoding="utf-8",
            )
            before = profile.read_bytes()
            loader_module._invalidate_config_cache()
            with caplog.at_level("WARNING"):
                later = loader_module.migrate_member_identity()
            assert later["rekeyed"] == 0 and later["stale_task_binds"] == 1
            assert profile.read_bytes() == before
            assert any(
                "cpm-ceiling: task:'Crew Program Manager' -> task:'crew-program-manager'"
                in rec.getMessage()
                for rec in caplog.records
            )
        finally:
            gp.reset_store()

    def test_member_update_patches_the_legacy_record_before_the_migration(self):
        # Between a load (read-only) and the one-shot migration the document
        # still stores the member under its legacy key; an update addressed by
        # the id must land on THAT record, not add a second one beside it.
        from kiro_crew.memory_stores import persist_member_config

        path = _write_config(_old_shape())
        cfg = KiroCrewConfig.load()
        cfg.agents["crew-program-manager"].model = "pinned"
        persist_member_config(
            cfg,
            "crew-program-manager",
            create=False,
            expected_store="member-cpm",
            changed_fields={"model"},
        )
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert "crew-program-manager" not in on_disk["agents"]
        assert on_disk["agents"]["Crew Program Manager"]["model"] == "pinned"
        loader_module._invalidate_config_cache()
        assert KiroCrewConfig.load().agents["crew-program-manager"].model == "pinned"

    def test_new_shape_is_not_rewritten(self):
        data = _old_shape()
        data["agents"] = {
            "default": data["agents"]["default"],
            "crew-program-manager": {
                **data["agents"]["Crew Program Manager"],
                "display_name": "Crew Program Manager",
            },
        }
        data["default_agent"] = "crew-program-manager"
        del data["memory_stores"]["member-egg"]
        path = _write_config(data)
        before = path.read_text(encoding="utf-8")
        cfg = KiroCrewConfig.load()
        assert path.read_text(encoding="utf-8") == before
        assert cfg.agents["crew-program-manager"].display_name == "Crew Program Manager"

    def test_colliding_id_stays_where_stored(self):
        data = _old_shape()
        # ``writer`` is both a legacy key and another entry's id: refused.
        data["agents"]["writer"] = {"kiro_agent": "kirocrew"}
        data["agents"]["Writer"] = {"member_id": "writer", "kiro_agent": "kirocrew"}
        _write_config(data)
        cfg = KiroCrewConfig.load()
        assert "Writer" in cfg.agents and "writer" in cfg.agents
        assert cfg.agents["Writer"].member_id == "writer"


class TestResolver:
    def test_resolves_by_key_and_by_display_name(self):
        _write_config(_old_shape())
        cfg = KiroCrewConfig.load()
        by_id = members_mod.resolve_member("crew-program-manager", cfg)
        by_name = members_mod.resolve_member("Crew Program Manager", cfg)
        assert by_id is not None and by_id == by_name
        assert by_id[0] == "crew-program-manager"
        assert members_mod.resolve_member_id("Doctor Eggbot", cfg) == "dr-eggbot"
        assert members_mod.resolve_member("nobody", cfg) is None
        assert members_mod.resolve_member("", cfg) is None

    def test_key_wins_over_a_display_name_spelling(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["alpha"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="beta")
        cfg.agents["beta"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="alpha")
        assert members_mod.resolve_member_id("alpha", cfg) == "alpha"
        assert members_mod.resolve_member_id("beta", cfg) == "beta"

    def test_shared_display_name_is_refused(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["one"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="Same")
        cfg.agents["two"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="Same")
        assert members_mod.resolve_member("Same", cfg) is None
        assert members_mod.same_member("one", "two", cfg) is False

    def test_member_slug_uses_the_id_for_either_handle(self):
        _write_config(_old_shape())
        cfg = KiroCrewConfig.load()
        assert members_mod.member_slug("Crew Program Manager", cfg) == "crew-program-manager"
        assert members_mod.member_slug("crew-program-manager", cfg) == "crew-program-manager"

    def test_allocator_reserves_every_key(self):
        from kiro_crew.memory_stores import _allocate_member_id

        cfg = KiroCrewConfig.load()
        cfg.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        allocated = _allocate_member_id(cfg, "Writer")
        assert allocated != "writer" and allocated.startswith("writer-")
        # The entry's own key is exempt: a record already keyed by its id keeps it.
        assert _allocate_member_id(cfg, "writer") == "writer"

    def test_allocator_reserves_every_display_name(self):
        # ``resolve_member`` prefers a key over a label: were the new member
        # keyed ``writer``, every request addressed to the label ``writer``
        # would route to it instead of ``author``.
        from kiro_crew.memory_stores import _allocate_member_id

        cfg = KiroCrewConfig.load()
        cfg.agents["author"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="writer")
        allocated = _allocate_member_id(cfg, "Writer!")
        assert allocated != "writer" and allocated.startswith("writer-")
        assert members_mod.resolve_member_id("writer", cfg) == "author"
        # The entry's own label is exempt, like its own key.
        cfg.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="writer")
        del cfg.agents["author"]
        assert _allocate_member_id(cfg, "writer") == "writer"


def _agents_app(tmp_path) -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_kirocrew_agent_delete,
        api_kirocrew_agent_update,
        api_kirocrew_agents,
    )
    from kiro_crew.dashboard.handlers.members import api_members

    app = web.Application()
    app["state"] = _make_state(tmp_path)
    app.router.add_get("/api/agents", api_kirocrew_agents)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    app.router.add_delete("/api/agents/{name}", api_kirocrew_agent_delete)
    app.router.add_get("/api/members", api_members)
    return app


@pytest.fixture()
def keyed_member():
    """A new-shape member with a private V2 store on disk (identity to protect)."""
    from kiro_crew.memory_stores import persist_member_config, provision_member_memory

    cfg = KiroCrewConfig.load()
    cfg.agents["release-writer"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", display_name="Release Writer"
    )
    provision_member_memory(cfg, "release-writer")
    persist_member_config(cfg, "release-writer", create=True)
    cfg = KiroCrewConfig.load()
    assert cfg.agents["release-writer"].member_id == "release-writer"
    return cfg.agents["release-writer"]


class TestRosters:
    @pytest.mark.asyncio
    async def test_agents_rows_carry_id_label_and_alias(self, tmp_path, keyed_member):
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            body = await (await client.get("/api/agents")).json()
        row = next(a for a in body["agents"] if a["member_id"] == "release-writer")
        assert row["display_name"] == "Release Writer"
        assert row["name"] == "Release Writer"
        # A record with no label shows its key in every slot.
        default = next(a for a in body["agents"] if a["member_id"] == "default")
        assert default["name"] == default["display_name"] == "default"

    @pytest.mark.asyncio
    async def test_members_rows_carry_id_label_and_alias(self, tmp_path, keyed_member):
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            body = await (await client.get("/api/members")).json()
        row = next(m for m in body["members"] if m["member_id"] == "release-writer")
        assert row["display_name"] == "Release Writer"
        assert row["name"] == "Release Writer"
        assert row["slug"] == "release-writer"

    @pytest.mark.asyncio
    async def test_agent_authored_key_is_masked_like_every_other_row_value(self, tmp_path):
        # An agent can write ``config.json`` directly with an entry that carries
        # no ``member_id``; the planner leaves it under its authored key, so the
        # key is agent-writable text and must never ship raw. A key the mask
        # WOULD alter is omitted (empty) rather than replaced by the sentinel:
        # the picker prefers ``member_id`` over ``name``, and a sentinel there
        # would pin a slot to a handle no member has, while the row's ``name``
        # stays the reachable handle it was left unmasked to be.
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

        probe = "AKIAIOSFODNN7EXAMPLE"
        doc = _old_shape()
        doc["agents"][probe] = {"kiro_agent": "kirocrew", "workspace": "default"}
        _write_config(doc)
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            body = await (await client.get("/api/agents")).json()
        ids = {a["member_id"] for a in body["agents"]}
        assert probe not in ids
        assert _SENSITIVE_MASK not in ids
        masked = [a for a in body["agents"] if a["display_name"] == _SENSITIVE_MASK]
        assert masked and all(a["member_id"] == "" for a in masked)
        # The row is still addressable by its name (the global row's one handle).
        assert all(a["name"] == probe for a in masked)
        # Benign keys are byte-identical.
        assert "crew-program-manager" in ids

    @pytest.mark.asyncio
    async def test_members_row_never_ships_a_key_the_mask_would_alter(self, tmp_path):
        # ``display_name`` was an unvalidated label before this change, so a
        # pre-existing record can pair a credential-shaped KEY with a benign
        # label. The label passes the row's handle gate; the key must still not
        # be the one ``/api/members`` field that bypasses ``_roster_mask``.
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

        # Lower-case so the key is a legal slug and the row is listed at all.
        probe = "glpat-abcdefghijklmnopqrst"
        doc = _old_shape()
        doc["agents"][probe] = {
            "member_id": probe,
            "display_name": "Benign Label",
            "kiro_agent": "kirocrew",
            "workspace": "default",
        }
        _write_config(doc)
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            body = await (await client.get("/api/members")).json()
        rows = [m for m in body["members"] if m["display_name"] == "Benign Label"]
        assert rows, [m["name"] for m in body["members"]]
        assert all(m["member_id"] == "" for m in rows)
        assert probe not in {m["member_id"] for m in body["members"]}
        assert _SENSITIVE_MASK not in {m["member_id"] for m in body["members"]}
        # The benign label stays the addressable handle.
        assert all(m["name"] == "Benign Label" for m in rows)

    @pytest.mark.asyncio
    async def test_a_masked_key_row_still_reports_its_live_binding(self, tmp_path):
        # The masked ``member_id`` is payload only: the handler's own lookups
        # (binding match, projection, header attribution) run on the real key,
        # so a record whose key the redactors would alter still shows its
        # bound thread instead of an empty slot key.
        probe = "glpat-abcdefghijklmnopqrst"
        doc = _old_shape()
        doc["agents"][probe] = {
            "member_id": probe,
            "display_name": "Benign Label",
            "kiro_agent": "kirocrew",
            "workspace": "default",
        }
        _write_config(doc)
        cfg = KiroCrewConfig.load()
        slug = members_mod.member_slug(probe, cfg)
        # An old-shape record on global memory: the V1 slot key.
        slot_key = members_mod.member_slot_key(slug)
        members_mod.write_dm_binding(slug, member=probe, slot_key=slot_key)
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            body = await (await client.get("/api/members")).json()
        row = next(m for m in body["members"] if m["display_name"] == "Benign Label")
        assert row["member_id"] == ""
        assert row["slot_key"] == slot_key


class TestRename:
    @pytest.mark.asyncio
    async def test_rename_edits_the_field_and_keeps_identity(self, tmp_path, keyed_member):
        before = keyed_member
        slug = members_mod.member_slug("release-writer")
        members_mod.write_dm_binding(
            slug,
            member="release-writer",
            slot_key=members_mod.member_slot_key(slug, before.memory_store),
            memory_store=before.memory_store,
        )
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            resp = await client.put(
                "/api/agents/Release Writer", json={"display_name": "Release Author"}
            )
            assert resp.status == 200
            data = await resp.json()
        assert data["member_id"] == "release-writer"
        assert data["display_name"] == data["name"] == "Release Author"
        cfg = KiroCrewConfig.load()
        assert "release-writer" in cfg.agents and "Release Author" not in cfg.agents
        after = cfg.agents["release-writer"]
        assert after.member_id == "release-writer"
        assert after.memory_store == before.memory_store
        assert after.display_name == "Release Author"
        assert members_mod.member_slug("Release Author", cfg) == slug
        binding = members_mod.read_dm_binding(slug)
        assert binding is not None and binding["slot_key"] == members_mod.member_slot_key(
            slug, before.memory_store
        )

    @pytest.mark.asyncio
    async def test_rename_to_another_members_handle_is_refused(self, tmp_path, keyed_member):
        cfg = KiroCrewConfig.load()
        cfg.agents["other"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="Other")
        cfg.save()
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            for taken in ("other", "Other"):
                resp = await client.put("/api/agents/release-writer", json={"display_name": taken})
                assert resp.status == 409
                assert (await resp.json())["code"] == "agent_exists"
        assert KiroCrewConfig.load().agents["release-writer"].display_name == "Release Writer"

    @pytest.mark.asyncio
    async def test_rename_validates_like_create(self, tmp_path, keyed_member):
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            resp = await client.put("/api/agents/release-writer", json={"display_name": "a\tb"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_member_name"

    @pytest.mark.asyncio
    async def test_rename_keeps_the_prior_label_as_an_alias(self, tmp_path, keyed_member):
        # ``config.local.json`` is user-owned and never rewritten, so an overlay
        # entry and a ``default_agent`` filed under the label being left behind
        # must keep folding onto THIS member after the rename -- not surface as
        # a second partial member, nor make the default fall back to "default".
        from kiro_crew.config.loader import config_local_path

        overlay = {
            "agents": {"Release Writer": {"model": "overlay-model"}},
            "default_agent": "Release Writer",
        }
        config_local_path().write_text(json.dumps(overlay), encoding="utf-8")
        loader_module._invalidate_config_cache()
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            resp = await client.put(
                "/api/agents/release-writer", json={"display_name": "Release Author"}
            )
            assert resp.status == 200
        loader_module._invalidate_config_cache()
        cfg = KiroCrewConfig.load()
        assert set(cfg.agents) == {"default", "release-writer"}
        record = cfg.agents["release-writer"]
        assert record.display_name == "Release Author"
        assert record.legacy_keys == ["Release Writer"]
        assert record.model == "overlay-model"
        assert cfg.default_agent == "release-writer"
        assert members_mod.resolve_member_id("Release Writer", cfg) == "release-writer"
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))
        assert on_disk["agents"]["release-writer"]["legacy_keys"] == ["Release Writer"]
        # Renaming back makes the label live again: it is no alias any more,
        # and the key itself is never recorded as one.
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            resp = await client.put(
                "/api/agents/release-writer", json={"display_name": "Release Writer"}
            )
            assert resp.status == 200
            resp = await client.put("/api/agents/release-writer", json={"display_name": ""})
            assert resp.status == 200
        loader_module._invalidate_config_cache()
        record = KiroCrewConfig.load().agents["release-writer"]
        assert record.display_name == ""
        assert record.legacy_keys == ["Release Author", "Release Writer"]
        assert "release-writer" not in record.legacy_keys

    @pytest.mark.asyncio
    async def test_rename_to_the_same_label_records_no_alias(self, tmp_path, keyed_member):
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            resp = await client.put(
                "/api/agents/release-writer", json={"display_name": "Release Writer"}
            )
            assert resp.status == 200
        assert KiroCrewConfig.load().agents["release-writer"].legacy_keys == []


class TestHandleRoutes:
    @pytest.mark.asyncio
    async def test_update_by_id_and_by_display_name_hit_one_record(self, tmp_path, keyed_member):
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            assert (
                await client.put("/api/agents/release-writer", json={"description": "by id"})
            ).status == 200
            assert KiroCrewConfig.load().agents["release-writer"].description == "by id"
            assert (
                await client.put("/api/agents/Release Writer", json={"description": "by name"})
            ).status == 200
            assert KiroCrewConfig.load().agents["release-writer"].description == "by name"

    @pytest.mark.asyncio
    async def test_unknown_handle_is_404(self, tmp_path, keyed_member):
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            assert (await client.put("/api/agents/nobody", json={"description": "x"})).status == 404
            assert (await client.delete("/api/agents/nobody")).status == 404

    @pytest.mark.asyncio
    async def test_delete_by_display_name(self, tmp_path, keyed_member):
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            assert (await client.delete("/api/agents/Release Writer")).status == 200
        assert "release-writer" not in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_delete_before_the_disk_migration_removes_the_legacy_entry(self, tmp_path):
        # ``load`` re-keys in memory only; the document keeps the legacy key
        # until ``migrate_member_identity`` has run. DELETE must find and
        # remove the entry where the document files it, not answer 500.
        _write_config(_old_shape())
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            resp = await client.delete("/api/agents/dr-eggbot")
            assert resp.status == 200, await resp.text()
        raw = json.loads(_config_path().read_text(encoding="utf-8"))
        assert "Dr. Eggbot" not in raw["agents"]
        assert "dr-eggbot" not in raw["agents"]
        assert "dr-eggbot" not in KiroCrewConfig.load().agents

    def test_rebind_before_the_disk_migration_finds_the_legacy_entry(self):
        # Same window as DELETE: ``_rebind_crew_locked`` receives the canonical
        # id but ``update_config_locked`` hands it the raw document, still
        # filed under the legacy key. An id lookup there answers a spurious
        # 409 (``_StaleBinding``); the binding must be patched where it is.
        from kiro_crew.dashboard.handlers.agents import _rebind_crew_locked

        _write_config(_old_shape())
        _rebind_crew_locked("dr-eggbot", ("kirocrew",), "kirocrew-conductor")
        raw = json.loads(_config_path().read_text(encoding="utf-8"))
        assert raw["agents"]["Dr. Eggbot"]["kiro_agent"] == "kirocrew-conductor"
        assert "dr-eggbot" not in raw["agents"]
        loader_module._invalidate_config_cache()
        assert KiroCrewConfig.load().agents["dr-eggbot"].kiro_agent == "kirocrew-conductor"

    def test_dispatch_resolves_either_handle(self, keyed_member):
        cfg = KiroCrewConfig.load()
        by_id = loader_module.resolve_agent_identity(cfg, "release-writer")
        by_name = loader_module.resolve_agent_identity(cfg, "Release Writer")
        assert by_id == by_name
        assert by_id[0] == "release-writer"
        assert loader_module.resolve_crew_identity(cfg, "Release Writer", None) == "release-writer"
        assert loader_module.resolve_crew_identity(cfg, None, "Release Writer") == "release-writer"


class TestOverlayStaysOneMember:
    """``config.local.json`` is never written back, so a member it patches can
    stay filed under the display-name key for good; merging that onto the
    id-keyed base must not surface a second, partial member."""

    def _overlay_path(self) -> Path:
        from kiro_crew.config.loader import config_local_path

        return config_local_path()

    def test_overlay_patch_under_the_legacy_key_folds_onto_the_member(self):
        _write_config(_old_shape())
        overlay = {"agents": {"Crew Program Manager": {"model": "overlay-model"}}}
        self._overlay_path().write_text(json.dumps(overlay), encoding="utf-8")
        loader_module._invalidate_config_cache()
        loader_module.migrate_member_identity()
        cfg = KiroCrewConfig.load()
        assert "Crew Program Manager" not in cfg.agents
        assert cfg.agents["crew-program-manager"].model == "overlay-model"
        assert members_mod.resolve_member_id("Crew Program Manager", cfg) == "crew-program-manager"
        # Only the base document is migrated; the user-owned overlay is untouched
        # and still folds onto the member on the next load.
        assert json.loads(self._overlay_path().read_text(encoding="utf-8")) == overlay
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))
        assert "Crew Program Manager" not in on_disk["agents"]
        loader_module._invalidate_config_cache()
        again = KiroCrewConfig.load()
        assert "Crew Program Manager" not in again.agents
        assert again.agents["crew-program-manager"].model == "overlay-model"

    def test_overlay_under_the_legacy_key_of_an_explicitly_labelled_member_stays_attached(self):
        # ``Dr. Eggbot`` carried its own display_name (``Doctor Eggbot``), so the
        # old key survives only as the record's ``legacy_keys``; the overlay,
        # never rewritten, still patches ``Dr. Eggbot`` and must keep folding
        # onto ``dr-eggbot`` on every later load instead of surfacing as a
        # second, partial member.
        _write_config(_old_shape())
        overlay = {"agents": {"Dr. Eggbot": {"model": "overlay-model"}}}
        self._overlay_path().write_text(json.dumps(overlay), encoding="utf-8")
        loader_module._invalidate_config_cache()
        loader_module.migrate_member_identity()
        first = KiroCrewConfig.load()
        assert first.agents["dr-eggbot"].model == "overlay-model"
        assert first.agents["dr-eggbot"].legacy_keys == ["Dr. Eggbot"]
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))
        assert set(on_disk["agents"]) == {"default", "crew-program-manager", "dr-eggbot"}
        assert on_disk["agents"]["dr-eggbot"]["display_name"] == "Doctor Eggbot"
        assert on_disk["agents"]["dr-eggbot"]["legacy_keys"] == ["Dr. Eggbot"]
        loader_module._invalidate_config_cache()
        again = KiroCrewConfig.load()
        assert set(again.agents) == {"default", "crew-program-manager", "dr-eggbot"}
        assert again.agents["dr-eggbot"].model == "overlay-model"
        assert again.agents["dr-eggbot"].display_name == "Doctor Eggbot"
        # The resolver, the default-agent canonicalizer and the capability
        # service's overlay view all honour the remembered key.
        assert members_mod.resolve_member_id("Dr. Eggbot", again) == "dr-eggbot"
        assert loader_module.canonical_agent_key("Dr. Eggbot", on_disk["agents"], {}) == "dr-eggbot"
        from kiro_crew.agent_capabilities import _canonical_overlay_agents

        assert _canonical_overlay_agents(on_disk, overlay) == {
            "dr-eggbot": {"model": "overlay-model"}
        }

    def test_a_live_handle_beats_a_remembered_legacy_key(self):
        # Once another member takes the retired spelling as key or label, the
        # overlay entry under it is THAT member's patch -- the memory yields;
        # a key two records both remember answers nobody.
        _write_config(_old_shape())
        loader_module._invalidate_config_cache()
        loader_module.migrate_member_identity()
        KiroCrewConfig.load()
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))
        agents = on_disk["agents"]
        assert loader_module.legacy_key_aliases(agents) == {"Dr. Eggbot": "dr-eggbot"}
        # ``Crew Program Manager`` became the member's label, so the label owns it.
        assert agents["crew-program-manager"]["legacy_keys"] == ["Crew Program Manager"]
        agents["newcomer"] = {
            "member_id": "newcomer",
            "kiro_agent": "kirocrew",
            "display_name": "Dr. Eggbot",
        }
        assert loader_module.legacy_key_aliases(agents) == {}
        cfg = KiroCrewConfig.load()
        cfg.agents["newcomer"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", member_id="newcomer", display_name="Dr. Eggbot"
        )
        assert members_mod.resolve_member_id("Dr. Eggbot", cfg) == "newcomer"
        del agents["newcomer"]
        agents["twin"] = {
            "member_id": "twin",
            "kiro_agent": "kirocrew",
            "legacy_keys": ["Dr. Eggbot"],
        }
        assert loader_module.legacy_key_aliases(agents) == {}
        del cfg.agents["newcomer"]
        cfg.agents["twin"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", member_id="twin", legacy_keys=["Dr. Eggbot"]
        )
        assert members_mod.resolve_member("Dr. Eggbot", cfg) is None
        # A malformed stored list is read as clean strings, never raised on.
        assert loader_module._legacy_keys_field(["a", 1, None, "a", ""]) == ["a"]
        assert loader_module._legacy_keys_field("a") == []

    def test_allocator_and_create_reserve_a_remembered_legacy_key(self):
        from kiro_crew.memory_stores import _allocate_member_id

        cfg = KiroCrewConfig.load()
        cfg.agents["author"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", member_id="author", display_name="Author", legacy_keys=["writer"]
        )
        assert _allocate_member_id(cfg, "Writer").startswith("writer-")
        assert members_mod.resolve_member_id("writer", cfg) == "author"

    def test_raw_agent_key_files_a_patch_where_the_record_is(self):
        base = {
            "agents": {
                "Writer": {"member_id": "writer", "kiro_agent": "k"},
                "Dr. Eggbot": {"member_id": "dr-eggbot", "display_name": "Doctor Eggbot"},
                "plain": {"kiro_agent": "k"},
            }
        }
        assert loader_module.raw_agent_key(base, "writer") == "Writer"
        assert loader_module.raw_agent_key(base, "dr-eggbot") == "Dr. Eggbot"
        assert loader_module.raw_agent_key(base, "plain") == "plain"
        assert loader_module.raw_agent_key(base, "newcomer") == "newcomer"
        overlay = {"agents": {"Writer": {"model": "x"}, "Doctor Eggbot": {"model": "y"}}}
        assert loader_module.raw_agent_key(overlay, "writer", base=base) == "Writer"
        assert loader_module.raw_agent_key(overlay, "dr-eggbot", base=base) == "Doctor Eggbot"
        assert loader_module.raw_agent_key(overlay, "plain", base=base) == "plain"
        assert loader_module.raw_agent_key({}, "writer", base=base) == "writer"

    def test_raw_agent_key_patches_the_effective_overlay_alias(self):
        # A post-migration overlay can hold the id AND its legacy-label spelling.
        # ``canonicalize_overlay_agents`` merges later-wins, so the effective
        # entry is the LAST one; a writer filing under the first would be
        # shadowed and its write silently dropped.
        base = {
            "agents": {
                "writer": {
                    "member_id": "writer",
                    "display_name": "Writer",
                    "legacy_keys": ["Writer"],
                    "kiro_agent": "k",
                }
            }
        }
        overlay = {"agents": {"writer": {"model": "x"}, "Writer": {"kiro_agent": "k-local"}}}
        assert loader_module.raw_agent_keys(overlay, "writer", base=base) == ["writer", "Writer"]
        assert loader_module.raw_agent_key(overlay, "writer", base=base) == "Writer"
        merged = loader_module.merge_config_documents(copy.deepcopy(base), copy.deepcopy(overlay))
        assert merged["agents"]["writer"]["kiro_agent"] == "k-local"
        reversed_overlay = {"agents": {"Writer": {"kiro_agent": "k-local"}, "writer": {}}}
        assert loader_module.raw_agent_key(reversed_overlay, "writer", base=base) == "writer"
        assert loader_module.raw_agent_keys({"agents": {}}, "writer", base=base) == ["writer"]

    def test_merge_without_a_plan_honours_remembered_legacy_keys(self):
        # The capability service merges without the loader's plan; an overlay
        # entry under a REMEMBERED legacy key of an explicitly labelled,
        # already-migrated member still folds onto that member.
        _write_config(_old_shape())
        loader_module.migrate_member_identity()
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))
        merged = loader_module.merge_config_documents(
            on_disk, {"agents": {"Dr. Eggbot": {"model": "overlay-model"}}}
        )
        assert "Dr. Eggbot" not in merged["agents"]
        assert merged["agents"]["dr-eggbot"]["model"] == "overlay-model"

    def test_overlay_patch_by_display_name_after_the_base_migrated(self):
        data = _old_shape()
        data["agents"] = {
            "default": data["agents"]["default"],
            "crew-program-manager": {
                **data["agents"]["Crew Program Manager"],
                "display_name": "Crew Program Manager",
            },
        }
        data["default_agent"] = "crew-program-manager"
        del data["memory_stores"]["member-egg"]
        _write_config(data)
        overlay = {"agents": {"Crew Program Manager": {"triggers": "from overlay"}}}
        self._overlay_path().write_text(json.dumps(overlay), encoding="utf-8")
        loader_module._invalidate_config_cache()
        cfg = KiroCrewConfig.load()
        assert set(cfg.agents) == {"default", "crew-program-manager"}
        assert cfg.agents["crew-program-manager"].triggers == "from overlay"

    def test_merge_config_documents_canonicalizes_both_sides(self):
        base = {
            "agents": {"Writer": {"member_id": "writer", "kiro_agent": "kirocrew"}},
            "default_agent": "Writer",
        }
        overlay = {
            "agents": {
                "Writer": {"model": "a"},
                "writer": {"triggers": "b"},
                "Only Here": {"kiro_agent": "kirocrew"},
            }
        }
        merged = loader_module.merge_config_documents(base, overlay)
        assert set(merged["agents"]) == {"writer", "Only Here"}
        assert merged["agents"]["writer"] == {
            "member_id": "writer",
            "kiro_agent": "kirocrew",
            "display_name": "Writer",
            "legacy_keys": ["Writer"],
            "model": "a",
            "triggers": "b",
        }
        # The merge returns copies: the base document handed in is not migrated.
        assert "Writer" in base["agents"]


class TestTeamsHoldOneIdentityPerMember:
    def _app(self) -> web.Application:
        from kiro_crew.dashboard.handlers.teams import (
            api_teams_create,
            api_teams_list,
            api_teams_update,
        )

        @web.middleware
        async def _auth(request: web.Request, handler):
            request["app"] = ""
            return await handler(request)

        app = web.Application(middlewares=[_auth])
        app.router.add_get("/api/teams", api_teams_list)
        app.router.add_post("/api/teams", api_teams_create)
        app.router.add_put("/api/teams/{id}", api_teams_update)
        return app

    @pytest.mark.asyncio
    async def test_id_and_display_name_alias_store_one_member(self, monkeypatch, keyed_member):
        from unittest.mock import AsyncMock

        from kiro_crew import crew_teams

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.teams.require_owner_dashboard_request",
            AsyncMock(return_value=None),
        )
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.post(
                "/api/teams",
                json={"name": "Docs", "members": ["release-writer", "Release Writer"]},
            )
            assert resp.status == 201, await resp.text()
            team = (await resp.json())["team"]
            # Stored once, by the key; answered by the roster's handle.
            assert crew_teams.read_teams()[0].members == ["release-writer"]
            assert team["members"] == ["Release Writer"]
            # The one-team invariant holds across spellings: naming the member by
            # its display name on a second team moves it off the first.
            resp = await client.post(
                "/api/teams", json={"name": "Ops", "members": ["Release Writer"]}
            )
            assert resp.status == 201
            stored = {t.name: t.members for t in crew_teams.read_teams()}
            assert stored == {"Docs": [], "Ops": ["release-writer"]}
            # ``add`` / ``remove`` deltas canonicalize the same way.
            ops_id = next(t.id for t in crew_teams.read_teams() if t.name == "Ops")
            resp = await client.put(f"/api/teams/{ops_id}", json={"remove": ["Release Writer"]})
            assert resp.status == 200
            assert (await resp.json())["team"]["members"] == []
            listing = await (await client.get("/api/teams")).json()
            assert {t["name"]: t["members"] for t in listing["teams"]} == {"Docs": [], "Ops": []}

    def test_a_legacy_document_listing_display_names_reads_as_the_key(self, keyed_member):
        from kiro_crew import crew_teams

        cfg = KiroCrewConfig.load()
        teams = [crew_teams.Team(id="aaaaaaaaaaaa", name="Docs", members=["Release Writer"])]
        canonical = crew_teams.canonicalize_teams(
            teams, lambda m: members_mod.resolve_member_id(m, cfg) or m
        )
        assert canonical[0].members == ["release-writer"]


# A publish receipt / capability intent pins its parent by name, source and
# path (all non-empty), scope and project -- see agent_state._capability_parent_valid.
_PARENT = {
    "name": "kirocrew",
    "scope": "global",
    "source": "builtin",
    "path": "builtin://kirocrew",
    "project": "",
}


class TestPrivateCopyOwnershipIsById:
    """A private template copy's ``private_to`` is the owner's ``config.agents``
    key. Labels are reusable, so an owner recorded by label would hand the copy
    to whichever member wears the label next."""

    def test_load_rewrites_legacy_label_owners_with_the_agents_rekey(self):
        from kiro_crew import agent_state

        data = _old_shape()
        data["agents"]["Crew Program Manager"]["kiro_agent"] = "cpm-copy"
        _write_config(data)
        agent_state.set_fork_info(
            "cpm-copy", forked_from="kirocrew", private_to="Crew Program Manager"
        )
        agent_state.set_fork_info("other-copy", forked_from="kirocrew", private_to="somebody-else")
        loader_module.migrate_member_identity()
        KiroCrewConfig.load()
        assert agent_state.get_fork_info("cpm-copy")["private_to"] == "crew-program-manager"
        assert agent_state.get_fork_info("other-copy")["private_to"] == "somebody-else"
        # Idempotent: a second load rewrites nothing.
        loader_module._invalidate_config_cache()
        KiroCrewConfig.load()
        assert agent_state.get_fork_info("cpm-copy")["private_to"] == "crew-program-manager"

    def test_a_sidecar_write_that_fails_abandons_the_config_write_and_the_next_boot_retries(
        self, monkeypatch
    ):
        # The sidecar's owners move INSIDE the config transaction, from the run's
        # own plan. A sidecar that cannot be written abandons the config write:
        # the document stays in its old shape, so the next boot derives the very
        # same plan and moves both together. Nothing is re-derived from the
        # agent-writable ``legacy_keys`` field.
        from kiro_crew import agent_state

        data = _old_shape()
        data["agents"]["Crew Program Manager"]["kiro_agent"] = "cpm-copy"
        path = _write_config(data)
        before = path.read_bytes()
        agent_state.set_fork_info(
            "cpm-copy", forked_from="kirocrew", private_to="Crew Program Manager"
        )
        real = agent_state.rekey_private_owners

        def _unwritable(plan, bound=None):
            raise OSError("sidecar locked by another process")

        with monkeypatch.context() as patched:
            patched.setattr(agent_state, "rekey_private_owners", _unwritable)
            first = loader_module.migrate_member_identity()
        assert first["rekeyed"] == 0 and first["private_owners"] == 0
        assert path.read_bytes() == before
        assert agent_state.get_fork_info("cpm-copy")["private_to"] == "Crew Program Manager"

        loader_module._invalidate_config_cache()
        second = loader_module.migrate_member_identity()
        assert second["rekeyed"] == 2
        assert second["private_owners"] == 1
        assert real is agent_state.rekey_private_owners
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert "Crew Program Manager" in stored["agents"]["crew-program-manager"]["legacy_keys"]
        assert agent_state.get_fork_info("cpm-copy")["private_to"] == "crew-program-manager"

    def test_legacy_keys_never_drive_a_sidecar_owner_move(self):
        # A record that spells an orphaned owner into its own ``legacy_keys`` and
        # binds that template is handed nothing: the field is agent-writable and
        # is not a source for ownership, so a boot with no key move changes no owner.
        from kiro_crew import agent_state

        _write_config(_old_shape())
        assert loader_module.migrate_member_identity()["rekeyed"] == 2
        agent_state.set_fork_info("orphan-copy", forked_from="kirocrew", private_to="Departed")
        loader_module._invalidate_config_cache()
        cfg = KiroCrewConfig.load()
        cfg.agents["crew-program-manager"].legacy_keys = ["Crew Program Manager", "Departed"]
        cfg.agents["crew-program-manager"].kiro_agent = "orphan-copy"
        cfg.save()
        loader_module._invalidate_config_cache()
        report = loader_module.migrate_member_identity()
        assert report["rekeyed"] == 0 and report["private_owners"] == 0
        assert agent_state.get_fork_info("orphan-copy")["private_to"] == "Departed"

    def test_a_label_owner_is_never_this_member(self):
        from kiro_crew import agent_state
        from kiro_crew.dashboard.handlers.agents import _foreign_private_copy_owner

        cfg = KiroCrewConfig.load()
        cfg.agents["release-writer"] = KiroCrewAgentConfig(
            kiro_agent="rw-copy", display_name="Release Writer"
        )
        cfg.save()
        agent_state.set_fork_info("rw-copy", forked_from="kirocrew", private_to="Release Writer")
        # Recorded by label: foreign to everyone, the labelled member included.
        assert _foreign_private_copy_owner("release-writer", "rw-copy") == "Release Writer"
        assert _foreign_private_copy_owner("someone", "rw-copy") == "Release Writer"
        agent_state.set_fork_info("rw-copy", forked_from="kirocrew", private_to="release-writer")
        assert _foreign_private_copy_owner("release-writer", "rw-copy") is None
        assert _foreign_private_copy_owner("someone", "rw-copy") == "release-writer"

    def test_an_unambiguous_legacy_key_owner_is_still_this_member(self):
        """A boot whose sidecar rewrite did not land leaves ``private_to`` at
        the key the record was moved from; the owner keeps their copy meanwhile,
        and only while no other record wears that spelling."""
        from kiro_crew import agent_state
        from kiro_crew.dashboard.handlers.agents import _foreign_private_copy_owner
        from kiro_crew.members import member_owns_private_copy

        cfg = KiroCrewConfig.load()
        cfg.agents["crew-program-manager"] = KiroCrewAgentConfig(
            kiro_agent="cpm-copy",
            member_id="crew-program-manager",
            display_name="Program Manager",
            legacy_keys=["Crew Program Manager"],
        )
        cfg.save()
        agent_state.set_fork_info(
            "cpm-copy", forked_from="kirocrew", private_to="Crew Program Manager"
        )
        cfg = KiroCrewConfig.load()
        assert member_owns_private_copy("Crew Program Manager", "crew-program-manager", cfg)
        assert _foreign_private_copy_owner("crew-program-manager", "cpm-copy") is None
        assert _foreign_private_copy_owner("someone", "cpm-copy") == "Crew Program Manager"
        # The current label is NOT a legacy key: still never an owner spelling.
        assert not member_owns_private_copy("Program Manager", "crew-program-manager", cfg)

        # Another member takes the old spelling as its label: contested, fail closed.
        cfg.agents["newcomer"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", member_id="newcomer", display_name="Crew Program Manager"
        )
        cfg.save()
        cfg = KiroCrewConfig.load()
        assert not member_owns_private_copy("Crew Program Manager", "crew-program-manager", cfg)
        assert not member_owns_private_copy("Crew Program Manager", "newcomer", cfg)
        assert _foreign_private_copy_owner("crew-program-manager", "cpm-copy") == (
            "Crew Program Manager"
        )

    def test_a_legacy_key_that_is_a_live_key_owns_nothing(self):
        from kiro_crew.members import member_owns_private_copy

        cfg = KiroCrewConfig.load()
        cfg.agents["author"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", member_id="author", display_name="Author", legacy_keys=["writer"]
        )
        cfg.agents["writer"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", member_id="writer", display_name="Writer"
        )
        cfg.save()
        cfg = KiroCrewConfig.load()
        assert member_owns_private_copy("writer", "writer", cfg)
        assert not member_owns_private_copy("writer", "author", cfg)

    def test_rekey_moves_publish_receipts_with_the_forks(self):
        """A publish receipt names its publisher by the same key ``private_to``
        uses; the retained receipt (kept so a lost acknowledgement stays
        retryable) must follow the record too, or the publisher is refused their
        own template name forever after the upgrade."""
        from kiro_crew import agent_state

        data = _old_shape()
        # Mid-publish: the record is still bound to the copy being published,
        # which corroborates both the fork's owner and the receipt's source.
        data["agents"]["Crew Program Manager"]["kiro_agent"] = "cpm-copy"
        _write_config(data)
        agent_state.set_fork_info(
            "cpm-copy", forked_from="kirocrew", private_to="Crew Program Manager"
        )
        with agent_state._locked():
            state = agent_state._read(strict=True)
            state.setdefault("cpm-public", {})["publish"] = {
                "member": "Crew Program Manager",
                "source": "cpm-copy",
                "source_digest": "a" * 8,
                "digest": "b" * 8,
                "parent": _PARENT,
            }
            state.setdefault("theirs", {})["publish"] = {
                "member": "somebody-else",
                "source": "their-copy",
                "source_digest": "c" * 8,
                "digest": "d" * 8,
                "parent": _PARENT,
            }
            agent_state._write(state)
        report = loader_module.migrate_member_identity()
        assert report["private_owners"] == 2
        assert agent_state.get_fork_info("cpm-copy")["private_to"] == "crew-program-manager"
        assert agent_state.get_publish_info("cpm-public")["member"] == "crew-program-manager"
        assert agent_state.get_publish_info("theirs")["member"] == "somebody-else"
        # Idempotent: nothing left to rewrite.
        assert loader_module.migrate_member_identity()["private_owners"] == 0

    def test_migration_leaves_an_orphaned_copy_under_a_reused_label(self):
        """A retired member's private copy can outlive its record (delete-time
        cleanup keeps lineage it cannot prove safe to drop). A namesake created
        afterwards is keyed by the same label pre-upgrade, so the ``old key ->
        member_id`` plan spells the orphan's owner too. The rewrite must not
        hand the deleted member's template to the newcomer: only the template
        the moved record is BOUND to moves with it."""
        from kiro_crew import agent_state

        data = _old_shape()
        data["agents"]["Crew Program Manager"]["kiro_agent"] = "cpm-copy"
        _write_config(data)
        agent_state.set_fork_info(
            "cpm-copy", forked_from="kirocrew", private_to="Crew Program Manager"
        )
        # The orphan: a copy the deleted namesake owned, never cleaned up.
        agent_state.set_fork_info(
            "cpm-copy-2", forked_from="kirocrew", private_to="Crew Program Manager"
        )
        report = loader_module.migrate_member_identity()
        assert report["rekeyed"] == 2
        assert report["private_owners"] == 1
        assert agent_state.get_fork_info("cpm-copy")["private_to"] == "crew-program-manager"
        assert agent_state.get_fork_info("cpm-copy-2")["private_to"] == "Crew Program Manager"
        # The retry path (owner map rebuilt from ``legacy_keys``) applies the
        # same corroboration: a later boot does not pick the orphan up either.
        assert loader_module.migrate_member_identity()["private_owners"] == 0
        assert agent_state.get_fork_info("cpm-copy-2")["private_to"] == "Crew Program Manager"
        # And the newcomer never reads the orphan as their own.
        from kiro_crew.dashboard.handlers.agents import _foreign_private_copy_owner

        KiroCrewConfig.load()
        assert _foreign_private_copy_owner("crew-program-manager", "cpm-copy") is None
        assert _foreign_private_copy_owner("crew-program-manager", "cpm-copy-2") == (
            "Crew Program Manager"
        )

    def test_a_published_templates_receipt_moves_by_the_public_name(self):
        """After a publish the record is bound to the public template, which
        holds the receipt; that binding corroborates the receipt even though
        the copy it was published from is gone."""
        from kiro_crew import agent_state

        data = _old_shape()
        data["agents"]["Crew Program Manager"]["kiro_agent"] = "cpm-public"
        _write_config(data)
        with agent_state._locked():
            state = agent_state._read(strict=True)
            state.setdefault("cpm-public", {})["publish"] = {
                "member": "Crew Program Manager",
                "source": "cpm-copy",
                "source_digest": "a" * 8,
                "digest": "b" * 8,
                "parent": _PARENT,
            }
            # A receipt on a template this record is NOT bound to: not proven
            # theirs, left as written.
            state.setdefault("cpm-other", {})["publish"] = {
                "member": "Crew Program Manager",
                "source": "cpm-other-copy",
                "source_digest": "c" * 8,
                "digest": "d" * 8,
                "parent": _PARENT,
            }
            agent_state._write(state)
        report = loader_module.migrate_member_identity()
        assert report["private_owners"] == 1
        assert agent_state.get_publish_info("cpm-public")["member"] == "crew-program-manager"
        assert agent_state.get_publish_info("cpm-other")["member"] == "Crew Program Manager"

    def test_capabilities_accept_the_unambiguous_legacy_key_owner(self):
        """Between gateway readiness and the identity migration (or after a
        sidecar rewrite that could not land) the fork still records the key the
        record was moved from. The capability seams must read that spelling the
        way the fork/publish/reset routes do, or the member cannot cold-start."""
        from kiro_crew import agent_capabilities, agent_state
        from kiro_crew.platform.governance_profiles import governance_answer_generation

        cfg = KiroCrewConfig.load()
        cfg.agents["crew-program-manager"] = KiroCrewAgentConfig(
            kiro_agent="cpm-copy",
            member_id="crew-program-manager",
            display_name="Program Manager",
            legacy_keys=["Crew Program Manager"],
        )
        cfg.save()
        agent_state.set_fork_info(
            "cpm-copy", forked_from="kirocrew", private_to="Crew Program Manager"
        )
        with agent_state._locked():
            state = agent_state._read(strict=True)
            state.setdefault("cpm-copy", {})["capabilities"] = {
                "schema_version": 1,
                "parent": _PARENT,
                "accepted": {section: {} for section in agent_state.CAPABILITY_SECTIONS},
                "overrides": {section: {} for section in agent_state.CAPABILITY_SECTIONS},
                "status": "saved",
                "governance_generation": governance_answer_generation(),
            }
            agent_state._write(state)
        with pytest.raises(agent_capabilities.CapabilityError) as refused:
            agent_capabilities.prepare_member_capabilities("crew-program-manager")
        # Past the ownership gate: the next check in line is what fails, not
        # ``foreign_private_copy``.
        assert refused.value.args[0] != "foreign_private_copy"

        # A contested spelling still fails closed.
        cfg.agents["newcomer"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", member_id="newcomer", display_name="Crew Program Manager"
        )
        cfg.save()
        with pytest.raises(agent_capabilities.CapabilityError) as foreign:
            agent_capabilities.prepare_member_capabilities("crew-program-manager")
        assert foreign.value.args[0] == "foreign_private_copy"

    @pytest.mark.asyncio
    async def test_reused_label_does_not_inherit_the_renamed_members_copy(
        self, tmp_path, keyed_member
    ):
        from kiro_crew import agent_state
        from kiro_crew.dashboard.handlers.agents import _foreign_private_copy_owner

        cfg = KiroCrewConfig.load()
        cfg.agents["release-writer"].kiro_agent = "rw-copy"
        cfg.save()
        # A sidecar the load could not repair still names the member by label.
        agent_state.set_fork_info("rw-copy", forked_from="kirocrew", private_to="Release Writer")
        app = _agents_app(tmp_path)
        from kiro_crew.dashboard.handlers import api_kirocrew_agents_create

        app.router.add_post("/api/agents", api_kirocrew_agents_create)
        async with TestClient(TestServer(app)) as client:
            # Renaming repairs the member's own copy to its key first ...
            resp = await client.put(
                "/api/agents/release-writer", json={"display_name": "Release Author"}
            )
            assert resp.status == 200
            assert agent_state.get_fork_info("rw-copy")["private_to"] == "release-writer"
            # ... so a new member taking the old label owns nothing of it.
            resp = await client.post(
                "/api/agents", json={"name": "Release Writer", "kiro_agent": "kirocrew"}
            )
            assert resp.status == 200, await resp.text()
            newcomer = (await resp.json())["member_id"]
            assert newcomer != "release-writer"
            assert _foreign_private_copy_owner(newcomer, "rw-copy") == "release-writer"
            resp = await client.put(f"/api/agents/{newcomer}", json={"kiro_agent": "rw-copy"})
            assert resp.status == 409
            assert (await resp.json())["code"] == "foreign_private_copy"

    @pytest.mark.asyncio
    async def test_namesake_cannot_bind_a_deleted_members_orphaned_copy(self, tmp_path):
        """The migration refused to move the orphan (see
        ``test_migration_leaves_an_orphaned_copy_under_a_reused_label``); the
        bind gate must refuse it too. The namesake's ``legacy_keys`` spell the
        orphan's owner, and that spelling is unambiguous -- but the orphan is
        not the template the namesake is bound to, so it is not theirs."""
        from kiro_crew import agent_state

        data = _old_shape()
        data["agents"]["Crew Program Manager"]["kiro_agent"] = "cpm-copy"
        _write_config(data)
        agent_state.set_fork_info(
            "cpm-copy", forked_from="kirocrew", private_to="Crew Program Manager"
        )
        agent_state.set_fork_info(
            "cpm-copy-2", forked_from="kirocrew", private_to="Crew Program Manager"
        )
        loader_module.migrate_member_identity()
        app = _agents_app(tmp_path)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/agents/crew-program-manager", json={"kiro_agent": "cpm-copy-2"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "foreign_private_copy"
            # Their own copy -- the one they are bound to -- is still theirs to
            # act on while it spells the legacy key (a rewrite that did not land).
            agent_state.set_fork_info(
                "cpm-copy", forked_from="kirocrew", private_to="Crew Program Manager"
            )
            resp = await client.put(
                "/api/agents/crew-program-manager", json={"kiro_agent": "cpm-copy"}
            )
            assert resp.status == 200, await resp.text()


class TestDefaultAgentFollowsTheMember:
    def _overlay_path(self) -> Path:
        from kiro_crew.config.loader import config_local_path

        return config_local_path()

    def test_overlay_default_by_legacy_label_selects_the_migrated_member(self):
        _write_config(_old_shape())
        self._overlay_path().write_text(
            json.dumps({"default_agent": "Crew Program Manager"}), encoding="utf-8"
        )
        loader_module._invalidate_config_cache()
        assert KiroCrewConfig.load().default_agent == "crew-program-manager"
        # The base has migrated (the legacy key is now the display name); the
        # untouched overlay still names the same member.
        loader_module._invalidate_config_cache()
        assert KiroCrewConfig.load().default_agent == "crew-program-manager"
        # By an explicit display name too.
        self._overlay_path().write_text(
            json.dumps({"default_agent": "Doctor Eggbot"}), encoding="utf-8"
        )
        loader_module._invalidate_config_cache()
        assert KiroCrewConfig.load().default_agent == "dr-eggbot"

    def test_unresolvable_overlay_default_is_reported_not_silent(self, caplog):
        _write_config(_old_shape())
        self._overlay_path().write_text(
            json.dumps({"default_agent": "nobody-here"}), encoding="utf-8"
        )
        loader_module._invalidate_config_cache()
        with caplog.at_level(logging.WARNING):
            cfg = KiroCrewConfig.load()
        assert cfg.default_agent == "default"
        assert "default_agent 'nobody-here' names no Crew Member" in caplog.text

    def test_base_default_by_display_name_is_written_back_as_the_key(self):
        data = _old_shape()
        data["agents"] = {
            "default": data["agents"]["default"],
            "dr-eggbot": {**data["agents"]["Dr. Eggbot"]},
        }
        data["default_agent"] = "Doctor Eggbot"
        del data["memory_stores"]["member-cpm"]
        path = _write_config(data)
        assert KiroCrewConfig.load().default_agent == "dr-eggbot"
        assert json.loads(path.read_text(encoding="utf-8"))["default_agent"] == "dr-eggbot"

    def test_base_default_by_a_retired_legacy_key_still_names_that_member(self, caplog):
        """No overlay: a default set by a label the crew was later renamed away
        from (now only in its ``legacy_keys``) must select that crew, as every
        other resolver does, not fall through to a substituted default."""
        data = _old_shape()
        data["agents"] = {
            "default": data["agents"]["default"],
            "dr-eggbot": {
                **data["agents"]["Dr. Eggbot"],
                "display_name": "Egg Doctor",
                "legacy_keys": ["Dr. Eggbot"],
            },
        }
        data["default_agent"] = "Dr. Eggbot"
        del data["memory_stores"]["member-cpm"]
        path = _write_config(data)
        with caplog.at_level(logging.WARNING):
            assert KiroCrewConfig.load().default_agent == "dr-eggbot"
        assert "names no Crew Member" not in caplog.text
        assert json.loads(path.read_text(encoding="utf-8"))["default_agent"] == "dr-eggbot"

    def test_merge_config_documents_maps_default_agent(self):
        base = {
            "agents": {"Writer": {"member_id": "writer", "kiro_agent": "kirocrew"}},
            "default_agent": "Writer",
        }
        assert loader_module.merge_config_documents(base, {})["default_agent"] == "writer"
        merged = loader_module.merge_config_documents(base, {"default_agent": "Writer"})
        assert merged["default_agent"] == "writer"
        merged = loader_module.merge_config_documents(base, {"default_agent": "nobody"})
        assert merged["default_agent"] == "nobody"

    @pytest.mark.asyncio
    async def test_default_agent_route_accepts_either_handle(self, tmp_path, keyed_member):
        from kiro_crew.dashboard.handlers import api_default_agent, api_kirocrew_agents

        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_route("*", "/api/config/default-agent", api_default_agent)
        app.router.add_get("/api/agents", api_kirocrew_agents)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/config/default-agent", json={"agent": "Release Writer"})
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert body["default_agent"] == "release-writer"
            assert body["name"] == "Release Writer"
            assert KiroCrewConfig.load().default_agent == "release-writer"
            roster = await (await client.get("/api/agents")).json()
            # Spelled like the rows' ``name``, so the picker's marker matches.
            assert roster["default_agent"] == "Release Writer"
            assert "default_member_id" not in roster  # the row carries member_id
            assert any(a["name"] == roster["default_agent"] for a in roster["agents"])
            resp = await client.put("/api/config/default-agent", json={"agent": "nobody"})
            assert resp.status == 400


class TestAvatarFilesAreKeyedByMemberId:
    """The picture is filed under the immutable ``member_id``, never the label.

    A rename therefore moves no file, cannot fail on the filesystem, and the
    committed pin keeps serving under the new display name.
    """

    _PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64 + b"\x00\x00\x00\x00IEND\xaeB`\x82"

    async def _wear_picture(self, client, handle: str) -> tuple[str, Path]:
        from aiohttp import FormData

        from kiro_crew.dashboard.handlers.agents import _avatar_stem, _avatars_dir

        form = FormData()
        form.add_field("file", self._PNG, filename="face.png", content_type="image/png")
        up = await client.post(f"/api/agents/{handle}/avatar", data=form)
        assert up.status == 200, await up.text()
        token = (await up.json())["token"]
        resp = await client.put(
            f"/api/agents/{handle}",
            json={"avatar": {"kind": "image", "promote": True, "token": token}},
        )
        assert resp.status == 200, await resp.text()
        pin = KiroCrewConfig.load().agents["release-writer"].avatar["file"]
        # Filed under the id whichever handle the upload URL carried.
        return pin, _avatars_dir() / f"{_avatar_stem('release-writer')}.{pin}"

    def _app(self, tmp_path) -> web.Application:
        from kiro_crew.dashboard.handlers import (
            api_kirocrew_agent_avatar_get,
            api_kirocrew_agent_avatar_upload,
        )

        app = _agents_app(tmp_path)
        app.router.add_post("/api/agents/{name}/avatar", api_kirocrew_agent_avatar_upload)
        app.router.add_get("/api/agents/{name}/avatar", api_kirocrew_agent_avatar_get)
        return app

    @pytest.mark.asyncio
    async def test_upload_by_label_files_under_the_id(self, tmp_path, keyed_member):
        from kiro_crew.dashboard.handlers.agents import _avatar_stem, _avatars_dir

        async with TestClient(TestServer(self._app(tmp_path))) as client:
            pin, path = await self._wear_picture(client, "Release Writer")
            assert path.is_file()
            assert not (_avatars_dir() / f"{_avatar_stem('Release Writer')}.{pin}").exists()

    @pytest.mark.asyncio
    async def test_rename_moves_no_file_and_keeps_serving(self, tmp_path, keyed_member):
        from kiro_crew.dashboard.handlers.agents import _avatar_stem, _avatars_dir

        async with TestClient(TestServer(self._app(tmp_path))) as client:
            pin, path = await self._wear_picture(client, "Release Writer")
            resp = await client.put(
                "/api/agents/release-writer", json={"display_name": "Release Author"}
            )
            assert resp.status == 200
            assert path.is_file()
            assert not (_avatars_dir() / f"{_avatar_stem('Release Author')}.{pin}").exists()
            assert KiroCrewConfig.load().agents["release-writer"].avatar["file"] == pin
            got = await client.get("/api/agents/Release Author/avatar")
            assert got.status == 200
            assert await got.read() == self._PNG

    @pytest.mark.asyncio
    async def test_rename_does_not_touch_the_filesystem(self, tmp_path, keyed_member, monkeypatch):
        import kiro_crew.dashboard.handlers.agents as handlers

        async with TestClient(TestServer(self._app(tmp_path))) as client:
            pin, path = await self._wear_picture(client, "Release Writer")

            def _refuse(src, dst):
                raise AssertionError("a rename must not move avatar files")

            with monkeypatch.context() as patched:
                patched.setattr(handlers, "replace_with_retry", _refuse)
                resp = await client.put(
                    "/api/agents/release-writer", json={"display_name": "Release Author"}
                )
                assert resp.status == 200
            assert path.is_file()
            assert KiroCrewConfig.load().agents["release-writer"].avatar["file"] == pin

    @pytest.mark.asyncio
    async def test_failed_sidecar_claim_refuses_the_rename(
        self, tmp_path, keyed_member, monkeypatch
    ):
        from kiro_crew import agent_state

        async with TestClient(TestServer(self._app(tmp_path))) as client:
            pin, old_path = await self._wear_picture(client, "Release Writer")

            def _unreadable(*args, **kwargs):
                raise OSError("sidecar unreadable")

            with monkeypatch.context() as patched:
                patched.setattr(agent_state, "claim_private_owner", _unreadable)
                resp = await client.put(
                    "/api/agents/release-writer", json={"display_name": "Release Author"}
                )
                assert resp.status >= 500
            after = KiroCrewConfig.load().agents["release-writer"]
            assert after.display_name == "Release Writer"
            assert after.avatar["file"] == pin
            assert old_path.is_file()
            got = await client.get("/api/agents/Release Writer/avatar")
            assert got.status == 200

    @pytest.mark.asyncio
    async def test_failed_config_write_keeps_the_picture(self, tmp_path, keyed_member, monkeypatch):
        import kiro_crew.dashboard.handlers.agents as handlers
        from kiro_crew.memory_stores import UnknownMemoryStore

        async with TestClient(TestServer(self._app(tmp_path))) as client:
            pin, old_path = await self._wear_picture(client, "Release Writer")

            def _refuse_write(*args, **kwargs):
                raise UnknownMemoryStore("config unwritable")

            with monkeypatch.context() as patched:
                patched.setattr(handlers, "persist_member_config", _refuse_write)
                resp = await client.put(
                    "/api/agents/release-writer", json={"display_name": "Release Author"}
                )
                assert resp.status >= 400
            assert old_path.is_file()
            assert KiroCrewConfig.load().agents["release-writer"].display_name == "Release Writer"
            got = await client.get("/api/agents/Release Writer/avatar")
            assert got.status == 200


class TestCreateAndDeleteEdges:
    @pytest.mark.asyncio
    async def test_create_refuses_a_credential_shaped_display_name(self, tmp_path):
        from kiro_crew.dashboard.handlers import api_kirocrew_agents_create

        app = _agents_app(tmp_path)
        app.router.add_post("/api/agents", api_kirocrew_agents_create)
        cred = "ghp_" + "0123456789abcdefghijABCDEFGHIJ0123456789"
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/agents",
                json={"name": "alpha", "kiro_agent": "kirocrew", "display_name": cred},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "credential_shaped_name"
        assert "alpha" not in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_create_refuses_a_handle_that_is_another_members_label(
        self, tmp_path, keyed_member
    ):
        from kiro_crew.dashboard.handlers import api_kirocrew_agents_create

        app = _agents_app(tmp_path)
        app.router.add_post("/api/agents", api_kirocrew_agents_create)
        async with TestClient(TestServer(app)) as client:
            for body in (
                {"name": "Release Writer", "kiro_agent": "kirocrew"},
                {
                    "name": "someone-else",
                    "kiro_agent": "kirocrew",
                    "display_name": "Release Writer",
                },
                {"name": "release-writer", "kiro_agent": "kirocrew", "display_name": "Other"},
            ):
                resp = await client.post("/api/agents", json=body)
                assert resp.status == 409, body
                assert (await resp.json())["code"] == "agent_exists"
        assert set(KiroCrewConfig.load().agents) >= {"release-writer"}
        assert "someone-else" not in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_create_never_mints_a_key_that_is_a_live_label(self, tmp_path, keyed_member):
        # End to end: ``release-writer`` is renamed to the label ``writer``;
        # creating ``Writer!`` slugs to ``writer`` and must NOT take that key,
        # or every request addressed to ``writer`` would route to it.
        from kiro_crew.dashboard.handlers import api_kirocrew_agents_create

        app = _agents_app(tmp_path)
        app.router.add_post("/api/agents", api_kirocrew_agents_create)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/agents/release-writer", json={"display_name": "writer"})
            assert resp.status == 200
            resp = await client.post(
                "/api/agents", json={"name": "Writer!", "kiro_agent": "kirocrew"}
            )
            assert resp.status in (200, 201), await resp.text()
            created = await resp.json()
        cfg = KiroCrewConfig.load()
        new_key = created.get("member_id") or next(
            key for key, member in cfg.agents.items() if member.display_name == "Writer!"
        )
        assert new_key != "writer" and new_key.startswith("writer-")
        assert members_mod.resolve_member_id("writer", cfg) == "release-writer"
        assert members_mod.resolve_member_id("Writer!", cfg) == new_key

    @pytest.mark.asyncio
    async def test_delete_reaps_a_private_copy_still_owned_by_label(
        self, tmp_path, keyed_member, monkeypatch
    ):
        from kiro_crew import agent_state
        from kiro_crew.dashboard.handlers import agents as handlers

        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "rw-copy.json").write_text(
            json.dumps({"name": "rw-copy", "prompt": "mine"}), encoding="utf-8"
        )
        monkeypatch.setattr(handlers, "kiro_agents_dir_path", lambda: specs)
        cfg = KiroCrewConfig.load()
        cfg.agents["release-writer"].kiro_agent = "rw-copy"
        cfg.save()
        # A sidecar the load could not repair names the owner by label.
        agent_state.set_fork_info("rw-copy", forked_from="kirocrew", private_to="Release Writer")
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            assert (await client.delete("/api/agents/Release Writer")).status == 200
        assert not (specs / "rw-copy.json").exists()
        assert agent_state.get_fork_info("rw-copy") is None


class TestLabelledMemberSurfaces:
    """Surfaces that index by the display name must index by the key instead."""

    @pytest.mark.asyncio
    async def test_members_row_reports_its_binding_for_a_labelled_crew(
        self, tmp_path, keyed_member
    ):
        slug = members_mod.member_slug("release-writer")
        members_mod.write_dm_binding(
            slug,
            member="release-writer",
            slot_key=members_mod.member_slot_key(slug, keyed_member.memory_store),
            memory_store=keyed_member.memory_store,
        )
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            body = await (await client.get("/api/members")).json()
        row = next(m for m in body["members"] if m["member_id"] == "release-writer")
        assert row["name"] == "Release Writer"
        assert row["slot_key"] == members_mod.member_slot_key(slug, keyed_member.memory_store)

    def test_rules_recorded_under_the_pre_publication_label_still_bind(self, keyed_member):
        # A rules payload saved while the record had no ``member_id`` stores
        # ``member_id: ""`` and the display name in ``member``. After the
        # re-key both handles the context builder asks for are the id, so an
        # exact-name compare would read the user's safety boundary as "never
        # set" -- silently, on the one layer whose absence nothing reports.
        path = members_mod.member_rules_path("release-writer")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"member_id": "", "member": "Release Writer", "rules": "Never merge."}),
            encoding="utf-8",
        )
        cfg = KiroCrewConfig.load()
        assert (
            members_mod.read_member_rules("release-writer", "release-writer", cfg) == "Never merge."
        )
        # Without a config handed in, the reader loads one only for this branch.
        assert members_mod.read_member_rules("release-writer", "release-writer") == "Never merge."

    def test_rules_recorded_under_a_label_the_member_was_renamed_from_still_bind(
        self, keyed_member
    ):
        cfg = KiroCrewConfig.load()
        cfg.agents["release-writer"].display_name = "Release Author"
        cfg.agents["release-writer"].legacy_keys = ["Release Writer"]
        cfg.save()
        path = members_mod.member_rules_path("release-writer")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"member_id": "", "member": "Release Writer", "rules": "Ask first."}),
            encoding="utf-8",
        )
        cfg = KiroCrewConfig.load()
        assert (
            members_mod.read_member_rules("release-writer", "release-writer", cfg) == "Ask first."
        )

    def test_rules_recorded_under_a_label_another_member_now_holds_stay_unset(self, keyed_member):
        # The live namespace wins: once "Release Writer" is another member's
        # live label, a payload recorded under it is THAT member's boundary,
        # never this one's -- the same rule as every resolver.
        from kiro_crew.memory_stores import persist_member_config, provision_member_memory

        cfg = KiroCrewConfig.load()
        cfg.agents["release-writer"].display_name = "Release Author"
        cfg.save()
        cfg = KiroCrewConfig.load()
        cfg.agents["docs-writer"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", display_name="Release Writer"
        )
        provision_member_memory(cfg, "docs-writer")
        persist_member_config(cfg, "docs-writer", create=True)
        path = members_mod.member_rules_path("release-writer")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"member_id": "", "member": "Release Writer", "rules": "Theirs."}),
            encoding="utf-8",
        )
        cfg = KiroCrewConfig.load()
        assert members_mod.read_member_rules("release-writer", "release-writer", cfg) == ""

    def test_context_builder_reads_rules_recorded_under_the_legacy_label(self, keyed_member):
        from kiro_crew.context import ContextBuilder

        path = members_mod.member_rules_path("release-writer")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"member_id": "", "member": "Release Writer", "rules": "Never merge."}),
            encoding="utf-8",
        )
        builder = ContextBuilder.__new__(ContextBuilder)
        text = ContextBuilder._build_member_section(
            builder, "release-writer", strict=True, include_briefing=False
        )
        assert "Never merge." in text

    @pytest.mark.asyncio
    async def test_a_dm_binding_recorded_under_the_pre_rekey_label_still_binds(
        self, tmp_path, keyed_member
    ):
        # A dm.json written before the re-key carries the display name in
        # ``member`` (``member_id: ""``). Every reader compares ``member`` with
        # the config key, so the label must canonicalize on the way in --
        # otherwise the row loses its slot key and the thread open answers
        # ``member_pin_mismatch`` for a transcript that is right there.
        slug = "release-writer"
        slot_key = members_mod.member_slot_key(slug, keyed_member.memory_store)
        path = members_mod.dm_binding_path(slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "member_id": "",
                    "member": "Release Writer",
                    "slug": slug,
                    "slot_key": slot_key,
                    "memory_store": keyed_member.memory_store,
                    "created_ts": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        binding = members_mod.read_dm_binding(slug)
        assert binding is not None and binding["member"] == "release-writer"
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            body = await (await client.get("/api/members")).json()
        row = next(m for m in body["members"] if m["member_id"] == "release-writer")
        assert row["slot_key"] == slot_key

    def test_a_dm_binding_naming_another_members_live_label_reads_as_absent(self, keyed_member):
        # Slug A's dm.json naming a label member B now holds live must not pin
        # A's thread to B -- and a label nobody answers to keeps the legacy
        # slug-fold rule (any name folding to this slug passes).
        from kiro_crew.memory_stores import persist_member_config, provision_member_memory

        cfg = KiroCrewConfig.load()
        cfg.agents["release-writer"].display_name = "Release Author"
        cfg.save()
        cfg = KiroCrewConfig.load()
        cfg.agents["docs-writer"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", display_name="Release Writer"
        )
        provision_member_memory(cfg, "docs-writer")
        persist_member_config(cfg, "docs-writer", create=True)
        slug = "release-writer"
        store = KiroCrewConfig.load().agents[slug].memory_store
        path = members_mod.dm_binding_path(slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "member_id": "",
            "member": "Release Writer",
            "slug": slug,
            "slot_key": members_mod.member_slot_key(slug, store),
            "memory_store": store,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        binding = members_mod.read_dm_binding(slug)
        # "Release Writer" folds to this slug, so the legacy rule still admits
        # it -- but it is NOT rewritten onto this member's key.
        assert binding is not None and binding["member"] == "Release Writer"
        payload["member"] = "Someone Else"
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert members_mod.read_dm_binding(slug) is None

    def test_member_identity_text_uses_the_display_name(self, keyed_member):
        from kiro_crew.context import ContextBuilder

        builder = ContextBuilder.__new__(ContextBuilder)
        text = ContextBuilder._build_member_section(
            builder, "release-writer", strict=True, include_briefing=False
        )
        assert "You are Release Writer." in text
        assert "You are release-writer" not in text

    def test_create_purges_a_team_entry_left_under_the_label(self, keyed_member):
        from kiro_crew import crew_teams
        from kiro_crew.memory_stores import persist_member_config, provision_member_memory

        crew_teams.write_teams(
            [crew_teams.Team(id="aaaaaaaaaaaa", name="Docs", members=["Release Writer"])]
        )
        cfg = KiroCrewConfig.load()
        cfg.agents["release-writer"].display_name = "Release Author"
        cfg.save()
        cfg = KiroCrewConfig.load()
        cfg.agents["release-writer-2"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", display_name="Release Writer"
        )
        provision_member_memory(cfg, "release-writer-2")
        persist_member_config(cfg, "release-writer-2", create=True)
        # The stale label entry was purged with the create, so canonicalizing the
        # document onto the new member yields no membership.
        stored = crew_teams.read_teams()[0].members
        assert stored == []

    def test_capabilities_see_an_overlay_entry_filed_under_the_legacy_key(self):
        from kiro_crew.agent_capabilities import _canonical_overlay_agents

        effective = {
            "agents": {
                "writer": {"member_id": "writer", "display_name": "Writer", "kiro_agent": "k"}
            }
        }
        overlay = {"agents": {"Writer": {"kiro_agent": "k-local"}}}
        assert _canonical_overlay_agents(effective, overlay) == {
            "writer": {"kiro_agent": "k-local"}
        }

    def test_capabilities_resolve_the_overlay_against_the_unmerged_base(self):
        # An overlay entry keyed by the display name it simultaneously replaces
        # is nobody's in the MERGED view (the label is gone there); against the
        # base it still folds onto the member, exactly as the merge folds it.
        from kiro_crew.agent_capabilities import _canonical_overlay_agents

        base = {
            "agents": {
                "writer": {"member_id": "writer", "display_name": "Writer", "kiro_agent": "k"}
            }
        }
        overlay = {"agents": {"Writer": {"display_name": "Author", "kiro_agent": "k-local"}}}
        assert _canonical_overlay_agents(base, overlay) == {
            "writer": {"display_name": "Author", "kiro_agent": "k-local"}
        }
        merged = loader_module.merge_config_documents(base, overlay)
        assert merged["agents"]["writer"]["display_name"] == "Author"
        assert _canonical_overlay_agents(merged, overlay) == {
            "Writer": {"display_name": "Author", "kiro_agent": "k-local"}
        }

    def test_labelled_members_store_is_recognised_as_owned(self, keyed_member):
        # The record's ``owner_member`` is the label ("Release Writer"); the
        # readers compare against the KEY. ``owner_member_id`` decides.
        from kiro_crew.dashboard.chat_persistence import member_store_ownership_holds
        from kiro_crew.dashboard.session_control import _store_is_member_owned
        from kiro_crew.memory_stores import store_owned_by_member

        cfg = KiroCrewConfig.load()
        store = cfg.agents["release-writer"].memory_store
        record = cfg.memory_stores[store]
        assert record.owner_member == "Release Writer"
        assert store_owned_by_member(record, "release-writer", cfg)
        assert member_store_ownership_holds(cfg, "release-writer", store)
        assert _store_is_member_owned(store)
        # A member that later takes the label owns nothing of it.
        cfg.agents["release-writer"].display_name = "Author"
        cfg.agents["other"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", member_id="other", display_name="Release Writer"
        )
        assert not store_owned_by_member(record, "other", cfg)
        assert store_owned_by_member(record, "release-writer", cfg)

    def test_legacy_owner_label_is_an_owner_spelling_not_a_display_name(self):
        # An older layout stamped ``owner_member`` with the member's KEY of the
        # day (labels were the keys). After the migration that spelling is the
        # record's legacy key, and it still names the member -- through the key
        # it was, never through whoever now wears it as a display name.
        from kiro_crew.memory_stores import store_owned_by_member

        cfg = KiroCrewConfig.load()
        cfg.agents["dr-eggbot"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew",
            member_id="dr-eggbot",
            display_name="Dr. Eggbot",
            legacy_keys=["Dr. Eggbot"],
        )
        legacy = loader_module.MemoryStoreConfig(owner_member="Dr. Eggbot", memory_version=2)
        assert store_owned_by_member(legacy, "dr-eggbot", cfg)
        assert not store_owned_by_member(legacy, "someone", cfg)
        # The deleted crew's store is not handed to a member who merely took
        # its old label: a display-name match is never ownership.
        cfg.agents["successor"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", member_id="successor", display_name="Old Eggbot"
        )
        orphan = loader_module.MemoryStoreConfig(owner_member="Old Eggbot", memory_version=2)
        assert not store_owned_by_member(orphan, "successor", cfg)
        # A legacy key two records remember answers nobody: refused, never guessed.
        cfg.agents["twin"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", member_id="twin", legacy_keys=["Dr. Eggbot"]
        )
        assert not store_owned_by_member(legacy, "dr-eggbot", cfg)

    def test_picture_filed_under_a_label_is_never_adopted(self):
        # ``researcher`` is already keyed by its id, so the plan never moves it.
        # No earlier build filed a picture under a DISPLAY NAME (labels were
        # presentation-only; the stem was always the config key), so a file
        # under ``Research Lead`` was written by whichever identity once had
        # that spelling as its KEY -- a deleted crew, here -- and must stay
        # where it is rather than become ``researcher``'s picture.
        from kiro_crew.members import avatar_stem, avatars_root

        data = _old_shape()
        data["agents"] = {
            "default": data["agents"]["default"],
            "researcher": {
                "member_id": "researcher",
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "display_name": "Research Lead",
            },
        }
        data["default_agent"] = "default"
        root = avatars_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{avatar_stem('Research Lead')}.0123456789abcdef.png").write_bytes(b"png")
        path = _write_config(data)
        before = path.read_text(encoding="utf-8")
        loader_module.migrate_member_identity()
        KiroCrewConfig.load()
        assert not (root / f"{avatar_stem('researcher')}.0123456789abcdef.png").exists()
        assert (root / f"{avatar_stem('Research Lead')}.0123456789abcdef.png").is_file()
        # Nothing moved in the document, so it is not rewritten.
        assert path.read_text(encoding="utf-8") == before

    def test_rekey_moves_pictures_filed_under_the_old_key_onto_the_id(self):
        from kiro_crew.members import avatar_stem, avatars_root

        data = _old_shape()
        root = avatars_root()
        root.mkdir(parents=True, exist_ok=True)
        # ``Dr. Eggbot`` already carried the explicit label ``Doctor Eggbot``;
        # its picture was filed under the KEY (the only stem earlier builds
        # used). A file under the label belongs to whoever had that spelling
        # as a key and is not touched.
        (root / f"{avatar_stem('Dr. Eggbot')}.abcdef0123456789.png").write_bytes(b"key")
        (root / f"{avatar_stem('Doctor Eggbot')}.fedcba9876543210.png").write_bytes(b"label")
        # ``Crew Program Manager`` takes its old key as label.
        (root / f"{avatar_stem('Crew Program Manager')}.0123456789abcdef.png").write_bytes(b"png")
        _write_config(data)
        loader_module.migrate_member_identity()
        KiroCrewConfig.load()
        assert (root / f"{avatar_stem('dr-eggbot')}.abcdef0123456789.png").read_bytes() == b"key"
        assert not (root / f"{avatar_stem('dr-eggbot')}.fedcba9876543210.png").exists()
        assert not (root / f"{avatar_stem('Dr. Eggbot')}.abcdef0123456789.png").exists()
        assert (
            root / f"{avatar_stem('Doctor Eggbot')}.fedcba9876543210.png"
        ).read_bytes() == b"label"
        assert (root / f"{avatar_stem('crew-program-manager')}.0123456789abcdef.png").is_file()
        assert not (root / f"{avatar_stem('Crew Program Manager')}.0123456789abcdef.png").exists()

    def test_avatar_rekey_never_overwrites_and_retries_the_source_next_boot(self):
        # Two legacy records whose handles fold onto one id stem must not
        # destroy each other's picture: the variant already under the id is
        # kept, the legacy source stays where it is, and a later boot (once the
        # conflict is resolved by hand) picks it up because nothing was lost.
        from kiro_crew.members import avatar_stem, avatars_root, rekey_avatar_files

        root = avatars_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{avatar_stem('writer')}.0123456789abcdef.png").write_bytes(b"mine")
        (root / f"{avatar_stem('Writer!')}.0123456789abcdef.png").write_bytes(b"theirs")
        assert rekey_avatar_files("Writer!", "writer") == 0
        assert (root / f"{avatar_stem('writer')}.0123456789abcdef.png").read_bytes() == b"mine"
        assert (root / f"{avatar_stem('Writer!')}.0123456789abcdef.png").read_bytes() == b"theirs"

    def test_avatar_rekey_leaves_an_unmovable_source_in_place(self, monkeypatch):
        from kiro_crew import members as members_module
        from kiro_crew.members import avatar_stem, avatars_root, rekey_avatar_files

        root = avatars_root()
        root.mkdir(parents=True, exist_ok=True)
        src = root / f"{avatar_stem('Research Lead')}.0123456789abcdef.png"
        src.write_bytes(b"png")

        def _refuse(a, b):
            raise PermissionError("busy")

        monkeypatch.setattr(members_module, "replace_with_retry", _refuse)
        assert rekey_avatar_files("Research Lead", "researcher") == 0
        assert src.read_bytes() == b"png"

    def test_avatar_sweep_never_moves_a_live_members_picture(self):
        # A release-then-recreate can leave one record REMEMBERING (as a legacy
        # key) the spelling another member now holds as its id. That spelling
        # is the live member's own stem; the sweep must not read it as the
        # first record's legacy source and carry the live picture away.
        from kiro_crew.members import avatar_stem, avatars_root

        data = _old_shape()
        data["agents"] = {
            "default": data["agents"]["default"],
            "on-call-a1b2c3d4e5f6": {
                "member_id": "on-call-a1b2c3d4e5f6",
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "display_name": "On Call (retired spelling)",
                "legacy_keys": ["on-call"],
            },
            "on-call": {
                "member_id": "on-call",
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "display_name": "On Call",
            },
        }
        data["default_agent"] = "default"
        root = avatars_root()
        root.mkdir(parents=True, exist_ok=True)
        live = root / f"{avatar_stem('on-call')}.0123456789abcdef.png"
        live.write_bytes(b"live")
        _write_config(data)
        loader_module.migrate_member_identity()
        KiroCrewConfig.load()
        assert live.read_bytes() == b"live"
        assert not (root / f"{avatar_stem('on-call-a1b2c3d4e5f6')}.0123456789abcdef.png").exists()

    def test_avatar_rekey_lands_when_one_crews_old_key_is_anothers_new_key(self):
        # Pre-migration: ``Alice`` (id ``alice``) and ``alice`` (id
        # ``alice-7f3``) -- the first crew's OLD key is the second crew's NEW
        # key. Moved in one pass, ``Alice -> alice`` is refused (the stem still
        # holds the other file) and ``alice -> alice-7f3`` is never a candidate
        # (its source is now a live key): ``alice`` would serve the other crew's
        # picture for good. Staging every source first makes both land.
        from kiro_crew.members import avatar_stem, avatars_root

        data = _old_shape()
        data["agents"] = {
            "default": data["agents"]["default"],
            "Alice": {"member_id": "alice", "kiro_agent": "kirocrew", "workspace": "default"},
            "alice": {"member_id": "alice-7f3", "kiro_agent": "kirocrew", "workspace": "default"},
        }
        data["default_agent"] = "default"
        root = avatars_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{avatar_stem('Alice')}.0123456789abcdef.png").write_bytes(b"first")
        (root / f"{avatar_stem('alice')}.fedcba9876543210.png").write_bytes(b"second")
        _write_config(data)
        report = loader_module.migrate_member_identity()
        assert report["avatars"] == 2
        assert (root / f"{avatar_stem('alice')}.0123456789abcdef.png").read_bytes() == b"first"
        assert (root / f"{avatar_stem('alice-7f3')}.fedcba9876543210.png").read_bytes() == b"second"
        assert not (root / f"{avatar_stem('alice')}.fedcba9876543210.png").exists()
        assert not (root / f"{avatar_stem('Alice')}.0123456789abcdef.png").exists()
        assert sorted(p.name for p in root.iterdir()) == sorted(
            [
                f"{avatar_stem('alice')}.0123456789abcdef.png",
                f"{avatar_stem('alice-7f3')}.fedcba9876543210.png",
            ]
        )
        # Nothing left to move; a later boot is a no-op.
        assert loader_module.migrate_member_identity()["avatars"] == 0

    def test_avatar_sweep_leaves_a_contested_handle_in_place(self):
        # Two records remember ``Twin`` as a legacy key: the spelling names two
        # members, so nothing moves and nobody guesses.
        from kiro_crew.config.loader import _rekey_member_avatars
        from kiro_crew.members import avatar_stem, avatars_root

        root = avatars_root()
        root.mkdir(parents=True, exist_ok=True)
        shared = root / f"{avatar_stem('Twin')}.0123456789abcdef.png"
        shared.write_bytes(b"png")
        agents = {
            "twin-a": {"member_id": "twin-a", "legacy_keys": ["Twin"]},
            "twin-b": {"member_id": "twin-b", "legacy_keys": ["Twin"]},
        }
        assert _rekey_member_avatars(agents, {}) == 0
        assert shared.read_bytes() == b"png"


class TestIdentityNamespaceProperty:
    """One namespace, four invariants, under any sequence of member operations.

    The identity namespace is {``config.agents`` keys, ``display_name`` labels,
    legacy pre-migration keys, allocated ids, retired store owners}. The
    handlers' guards (``resolve_member`` on create and rename, the allocator's
    reservation set, the migration planner's survivor rule) exist to keep it
    consistent; this test drives them with random labels drawn from a small
    alphabet built to collide (``writer`` / ``Writer`` / ``Writer!`` all slug to
    ``writer``) and checks, after every step:

    (a) no two live members share a key -- and a document round-trip through
        the migration keeps every entry;
    (b) no live member's ``display_name`` equals another live member's key;
    (c) every live handle (key or label) resolves to exactly its member;
    (d) writing a legacy rendering of the state (some entries filed under their
        label) and migrating it recovers the state, and migrating again moves
        nothing.
    """

    LABELS = ["writer", "Writer", "Writer!", "author", "Author", "x y", "x-y"]

    @staticmethod
    def _model_create(cfg, label: str) -> str | None:
        # Mirrors ``create_agent``: refused when either handle names a member.
        from kiro_crew.memory_stores import _allocate_member_id

        if members_mod.resolve_member(label, cfg) is not None:
            return None
        key = _allocate_member_id(cfg, label)
        cfg.agents[key] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", display_name=label, member_id=key, memory_store=f"store-{key}"
        )
        cfg.memory_stores[f"store-{key}"] = loader_module.MemoryStoreConfig(
            owner_member_id=key, owner_member=label, memory_version=2
        )
        return key

    @staticmethod
    def _model_rename(cfg, handle: str, new_label: str) -> None:
        # Mirrors ``update_agent``: refused when the label names another member.
        hit = members_mod.resolve_member(handle, cfg)
        if hit is None:
            return
        taken = members_mod.resolve_member(new_label, cfg)
        if taken is not None and taken[0] != hit[0]:
            return
        hit[1].display_name = new_label

    @staticmethod
    def _model_delete(cfg, handle: str) -> None:
        # Mirrors ``delete_agent``: the record goes, the retired store stays.
        hit = members_mod.resolve_member(handle, cfg)
        if hit is not None:
            del cfg.agents[hit[0]]

    @staticmethod
    def _live_handles(cfg) -> list[str]:
        handles = list(cfg.agents)
        handles.extend(member.display_name for member in cfg.agents.values() if member.display_name)
        # Insertion order, not sorted: a collision-suffixed id is random, so a
        # sort would order the handles differently on hypothesis' replay.
        return list(dict.fromkeys(handles))

    @classmethod
    def _check(cls, cfg) -> None:
        keys = set(cfg.agents)
        for key, member in cfg.agents.items():
            label = member.display_name
            # (b)
            assert not label or label == key or label not in keys, (key, label, keys)
            # (c)
            assert members_mod.resolve_member_id(key, cfg) == key
            if label:
                assert members_mod.resolve_member_id(label, cfg) == key, (label, key)
            # every id equals its key from the first write
            assert member.member_id == key

    @classmethod
    def _document(cls, cfg, legacy: list[bool]) -> dict:
        doc = {}
        for (key, member), as_legacy in zip(cfg.agents.items(), legacy):
            entry = {
                "member_id": key,
                "kiro_agent": "kirocrew",
                "display_name": member.display_name,
            }
            doc[member.display_name if as_legacy and member.display_name else key] = entry
        return doc

    @given(data=st.data())
    @settings(max_examples=300, deadline=None)
    def test_namespace_stays_consistent(self, data):
        cfg = KiroCrewConfig()
        cfg.agents = {}
        cfg.memory_stores = {}
        labels = st.sampled_from(self.LABELS)
        for _ in range(data.draw(st.integers(min_value=0, max_value=10), label="steps")):
            live = self._live_handles(cfg)
            op = data.draw(st.sampled_from(["create", "rename", "delete"] if live else ["create"]))
            if op == "create":
                self._model_create(cfg, data.draw(labels, label="create"))
            elif op == "rename":
                self._model_rename(
                    cfg,
                    data.draw(st.sampled_from(live), label="rename"),
                    data.draw(labels, label="to"),
                )
            else:
                self._model_delete(cfg, data.draw(st.sampled_from(live), label="delete"))
            self._check(cfg)  # (a) is the dict itself; (b) and (c) inside
        # (d) a legacy rendering migrates back to exactly this state, once.
        legacy = data.draw(
            st.lists(st.booleans(), min_size=len(cfg.agents), max_size=len(cfg.agents))
        )
        doc = self._document(cfg, legacy)
        rekeyed, plan = loader_module.rekey_agents_document(doc)
        assert len(rekeyed) == len(cfg.agents) == len(doc)
        assert set(rekeyed) == set(cfg.agents)
        for key, entry in rekeyed.items():
            assert entry["display_name"] == cfg.agents[key].display_name
        again, plan_again = loader_module.rekey_agents_document(dict(rekeyed))
        assert plan_again == {} and again == rekeyed

    @given(
        entries=st.lists(
            st.tuples(
                st.sampled_from(["a", "b", "c", "d", "A", "B"]),
                st.sampled_from(["", "a", "b", "c", "d", "shared"]),
            ),
            max_size=6,
            unique_by=lambda e: e[0],
        )
    )
    @settings(max_examples=300, deadline=None)
    def test_arbitrary_documents_migrate_without_loss_and_idempotently(self, entries):
        # Any document, however tangled (duplicate ids, chains, cycles): every
        # entry survives the rewrite and a second rewrite is a no-op.
        doc = {key: {"member_id": member_id} for key, member_id in entries}
        rekeyed, plan = loader_module.rekey_agents_document(dict(doc))
        assert len(rekeyed) == len(doc)
        assert sorted(e["member_id"] for e in rekeyed.values()) == sorted(
            e["member_id"] for e in doc.values()
        )
        for old_key, new_key in plan.items():
            assert doc[old_key]["member_id"] == new_key
        again, plan_again = loader_module.rekey_agents_document(dict(rekeyed))
        assert plan_again == {} and again == rekeyed


class TestTeamsBoundTheRetainedIdentity:
    """The teams document is bounded on what a write RETAINS: the canonical key."""

    def _long_key(self) -> str:
        from kiro_crew import crew_teams

        return "k" * (crew_teams.MEMBER_NAME_MAX_CHARS + 1)

    def test_a_write_refuses_a_canonical_key_the_reader_would_not_read_back(self):
        from kiro_crew import crew_teams

        long_key = self._long_key()
        # A short, in-bounds display name addressing a record whose KEY is out
        # of bounds: bounding the submitted handle alone would let the write
        # retain the key and make the next read refuse the document whole.
        with pytest.raises(crew_teams.TeamError) as excinfo:
            crew_teams._validate_names(["Kai"], lambda m: long_key if m == "Kai" else m)
        assert excinfo.value.code == "invalid_members"
        # The same handle mapping to an in-bounds key is retained as that key.
        assert crew_teams._validate_names(["Kai"], lambda m: "kai" if m == "Kai" else m) == ["kai"]

    def test_the_read_view_keeps_the_stored_spelling_over_an_unbounded_key(self):
        from kiro_crew import crew_teams

        long_key = self._long_key()
        teams = [crew_teams.Team(id="aaaaaaaaaaaa", name="Docs", members=["Kai", "Ro"])]
        view = crew_teams.canonicalize_teams(
            teams, lambda m: long_key if m == "Kai" else ("ro" if m == "Ro" else m)
        )
        # ``Ro`` maps; ``Kai`` keeps the value that already passed the read
        # bounds, so the view holds nothing a write could not retain.
        assert view[0].members == ["Kai", "ro"]
        assert crew_teams.prune_unknown(view, {"ro"})[0].members == ["ro"]


class TestSharedLabelsFallBackToTheKey:
    """Two records sharing a display name each answer by their key, never the label."""

    def _twins(self) -> None:
        data = _old_shape()
        data["agents"] = {
            "default": data["agents"]["default"],
            "kai-1": {
                "member_id": "kai-1",
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
                "display_name": "Kai",
            },
            "kai-2": {
                "member_id": "kai-2",
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
                "display_name": "Kai",
            },
        }
        data["default_agent"] = "default"
        data["memory_stores"] = {"default": {}}
        _write_config(data)

    @pytest.mark.asyncio
    async def test_roster_rows_ship_the_key_as_name(self, tmp_path):
        self._twins()
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            agents = (await (await client.get("/api/agents")).json())["agents"]
            members = (await (await client.get("/api/members")).json())["members"]
            by_id = {a["member_id"]: a for a in agents}
            assert by_id["kai-1"]["display_name"] == by_id["kai-2"]["display_name"] == "Kai"
            # The label resolves to neither record, so it cannot be the handle.
            assert by_id["kai-1"]["name"] == "kai-1"
            assert by_id["kai-2"]["name"] == "kai-2"
            assert {m["name"] for m in members} >= {"kai-1", "kai-2"}
            # The shared label answers neither; the handle the rows ship does.
            resp = await client.put("/api/agents/Kai", json={"display_name": "Nobody"})
            assert resp.status == 404
            resp = await client.put("/api/agents/kai-2", json={"display_name": "Kai Two"})
            assert resp.status == 200, await resp.text()

    def test_team_answers_a_shared_label_by_the_key(self):
        from kiro_crew import crew_teams
        from kiro_crew.dashboard.handlers.teams import _public_team

        self._twins()
        cfg = KiroCrewConfig.load()
        team = crew_teams.Team(id="aaaaaaaaaaaa", name="Docs", members=["kai-1"])
        assert _public_team(team, cfg)["members"] == ["kai-1"]

    def test_a_row_serialized_alone_still_answers_its_label(self):
        from kiro_crew.dashboard.handlers.agents import _agent_roster_row

        row = _agent_roster_row(
            "kai-1", "global", KiroCrewAgentConfig(display_name="Kai"), redact=False
        )
        assert row["name"] == "Kai"

    def test_an_undispatchable_label_ships_the_key_on_every_roster(self):
        """A stored label the mask leaves alone but that cannot be dispatched
        (trailing whitespace predates the rename validation) is one crew, so
        ``/api/agents``, ``/api/members`` and the default marker all spell its
        handle the same way: the key."""
        from kiro_crew.dashboard.handlers.agents import (
            _agent_roster_row,
            _default_agent_handle,
        )
        from kiro_crew.dashboard.handlers.members import member_roster_handle

        record = KiroCrewAgentConfig(display_name="Kai ")
        agents = {"kai-1": record}
        row = _agent_roster_row("kai-1", "global", record, redact=False, agents=agents)
        assert row["name"] == "kai-1"
        assert member_roster_handle("kai-1", record, agents) == "kai-1"
        cfg = KiroCrewConfig.load()
        cfg.agents["kai-1"] = record
        assert _default_agent_handle(cfg, "kai-1") == "kai-1"


class TestUsageSortIsKeyedByMemberId:
    @pytest.mark.asyncio
    async def test_a_labelled_crew_sorts_by_the_key_sessions_pin(self, tmp_path, keyed_member):
        from types import SimpleNamespace

        app = _agents_app(tmp_path)
        # Sessions pin the key the picker dispatches (``member_id``); usage is
        # counted under it, never under the display label.
        app["state"].conversation_log = SimpleNamespace(
            agent_usage=lambda: {"release-writer": (7, 100.0)}
        )
        async with TestClient(TestServer(app)) as client:
            agents = (await (await client.get("/api/agents")).json())["agents"]
        assert agents[0]["member_id"] == "release-writer"
        assert agents[0]["name"] == "Release Writer"

    @pytest.mark.asyncio
    async def test_history_pinned_under_a_retired_handle_still_counts(self, tmp_path, keyed_member):
        # Sessions created before the migration pin the crew's OLD key (now its
        # display name or a legacy key). The sort takes the best tuple across
        # every spelling, so the most-used crew stays first after the upgrade.
        from types import SimpleNamespace

        doc = json.loads(_config_path().read_text(encoding="utf-8"))
        doc["agents"]["release-writer"]["legacy_keys"] = ["Release-Writer"]
        _write_config(doc)
        app = _agents_app(tmp_path)
        app["state"].conversation_log = SimpleNamespace(
            agent_usage=lambda: {"Release Writer": (3, 10.0), "Release-Writer": (9, 500.0)}
        )
        async with TestClient(TestServer(app)) as client:
            agents = (await (await client.get("/api/agents")).json())["agents"]
        assert agents[0]["member_id"] == "release-writer"


class TestCrossTeamDedupeOnCanonicalize:
    def test_one_identity_spelled_two_ways_lands_in_the_first_team_only(self):
        from kiro_crew import crew_teams

        teams = [
            crew_teams.Team(id="aaaaaaaaaaaa", name="Docs", members=["kai"]),
            crew_teams.Team(id="bbbbbbbbbbbb", name="Ops", members=["Kai", "ro"]),
        ]
        view = crew_teams.canonicalize_teams(teams, lambda m: "kai" if m == "Kai" else m)
        assert view[0].members == ["kai"]
        assert view[1].members == ["ro"]


class TestPruneOwnershipIgnoresDisplayNames:
    def test_prune_entry_unchanged_compares_every_shared_field(self):
        from kiro_crew.dashboard.handlers.agents import _prune_entry_unchanged

        snapshot = {"kiro_agent": "coder", "source": "pkg", "description": "old"}
        # An edit to ANY shared field is newer evidence than the stale scan.
        assert not _prune_entry_unchanged({**snapshot, "description": "edited"}, snapshot)
        # A field only the SNAPSHOT carries (an older build's on-disk shape
        # lacking a newer dataclass field) does not count as a change ...
        assert _prune_entry_unchanged(dict(snapshot), {**snapshot, "avatar": {}})
        # ... but a field only the LIVE entry carries is a write the snapshot
        # never saw -- even one to a key the dataclass does not model -- and
        # the row survives rather than being pruned on a stale scan.
        assert not _prune_entry_unchanged({**snapshot, "display_name": ""}, snapshot)
        assert not _prune_entry_unchanged({**snapshot, "pinned": True}, snapshot)


class TestCreateChecksLineageForTheStoredKey:
    @pytest.mark.asyncio
    async def test_create_asks_about_the_key_the_record_is_filed_under(self, tmp_path, monkeypatch):
        # A stale fork sidecar left by a deleted crew records ``private_to`` as
        # the very label being requested. Asked for the LABEL, the check would
        # answer "own copy"; asked for the KEY the record is filed under (what
        # every later ownership question asks with), it answers foreign.
        from kiro_crew.dashboard.handlers import agents as handlers

        asked: list[str] = []

        def fake_owner(crew: str, target: str) -> str | None:
            asked.append(crew)
            return None if crew == "Coder" else "Coder"

        monkeypatch.setattr(handlers, "_foreign_private_copy_owner", fake_owner)
        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_post("/api/agents", handlers.api_kirocrew_agents_create)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/agents", json={"name": "Coder", "kiro_agent": "coder"})
            body = await resp.json()
        assert resp.status == 409
        assert body["code"] == "foreign_private_copy"
        assert asked == ["coder"]


class TestCliDeleteFindsTheStoredRecord:
    def test_delete_by_id_on_an_unmigrated_document(self, capsys):
        import argparse

        from kiro_crew import cli_commands

        path = _write_config(_old_shape())
        # The load answers ``dr-eggbot``; the document still files the record
        # under its legacy key. The delete must find it where it IS.
        cli_commands._handle_agent(argparse.Namespace(agent_action="delete", name="dr-eggbot"))
        assert "Deleted agent: dr-eggbot" in capsys.readouterr().out
        stored = json.loads(path.read_text(encoding="utf-8"))["agents"]
        assert "Dr. Eggbot" not in stored and "dr-eggbot" not in stored
        assert "Crew Program Manager" in stored

    def test_delete_refuses_the_default_under_its_legacy_spelling(self, capsys):
        import argparse

        from kiro_crew import cli_commands

        _write_config(_old_shape())  # default_agent: "Crew Program Manager" (legacy key)
        with pytest.raises(SystemExit):
            cli_commands._handle_agent(
                argparse.Namespace(agent_action="delete", name="crew-program-manager")
            )
        assert "cannot delete default agent" in capsys.readouterr().err


def _set_default_on_disk_after_load(monkeypatch, handle: str) -> None:
    """Simulate ``kirocrew config set default_agent <handle>`` landing between a
    delete's own load and its in-lock mutate: the FIRST load answers the old
    default, and the raw document the mutate then reads carries *handle*
    verbatim, in whatever spelling the operator typed."""
    real_load = KiroCrewConfig.load
    fired = {"done": False}

    def _load(*args, **kwargs):
        cfg = real_load(*args, **kwargs)
        if not fired["done"]:
            fired["done"] = True
            path = _config_path()
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["default_agent"] = handle
            path.write_text(json.dumps(raw), encoding="utf-8")
            loader_module._invalidate_config_cache()
        return cfg

    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_load))


class TestDeleteGuardReadsEveryDefaultSpelling:
    """The in-lock default re-check must recognise the default under the display
    name or a legacy key, not only the id and the stored key: the generic
    ``config set default_agent`` writes handles verbatim, and the member deletion
    it is meant to refuse has no recovery path (record, avatars, private copy)."""

    def test_helper_expands_each_spelling_against_the_raw_agents_map(self):
        agents = {
            "crew-program-manager": {
                "display_name": "Crew Program Manager",
                "legacy_keys": ["Crew Program Manager", "cpm"],
            },
            "dr-eggbot": {"display_name": "Doctor Eggbot"},
        }
        me = ("crew-program-manager", "crew-program-manager")
        for spelling in ("crew-program-manager", "Crew Program Manager", "cpm"):
            assert members_mod.document_default_names_member(
                {"agents": agents, "default_agent": spelling}, *me
            ), spelling
            assert members_mod.document_default_names_member(
                {"agents": agents, "agent": {"default_agent": spelling}}, *me
            ), spelling
        # A record still filed under its legacy key while the id is the default.
        legacy_doc = {
            "agents": {"Crew Program Manager": {"member_id": "crew-program-manager"}},
            "default_agent": "crew-program-manager",
        }
        assert members_mod.document_default_names_member(
            legacy_doc, "crew-program-manager", "Crew Program Manager"
        )
        for other in ("dr-eggbot", "Doctor Eggbot", "", None, 7):
            assert not members_mod.document_default_names_member(
                {"agents": agents, "default_agent": other}, *me
            ), other
        assert not members_mod.document_default_names_member(None, *me)
        assert not members_mod.document_default_names_member({"agents": agents})

    @pytest.mark.asyncio
    async def test_dashboard_delete_refuses_a_default_set_by_display_name_in_the_window(
        self, tmp_path, monkeypatch, keyed_member
    ):
        _set_default_on_disk_after_load(monkeypatch, "Release Writer")
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            resp = await client.delete("/api/agents/release-writer")
            assert resp.status == 409, await resp.text()
        raw = json.loads(_config_path().read_text(encoding="utf-8"))
        assert "release-writer" in raw["agents"]
        assert raw["default_agent"] == "Release Writer"

    def test_cli_delete_refuses_a_default_set_by_display_name_in_the_window(
        self, monkeypatch, capsys
    ):
        import argparse

        from kiro_crew import cli_commands

        _write_config(_old_shape())  # default is "Crew Program Manager"; Eggbot is free
        _set_default_on_disk_after_load(monkeypatch, "Doctor Eggbot")
        with pytest.raises(SystemExit):
            cli_commands._handle_agent(argparse.Namespace(agent_action="delete", name="dr-eggbot"))
        assert "cannot delete default agent" in capsys.readouterr().err
        raw = json.loads(_config_path().read_text(encoding="utf-8"))
        assert "Dr. Eggbot" in raw["agents"]


class TestLegacyKeyDefaultIsNotWrittenBackByTheLoad:
    def test_a_default_spelled_as_a_still_stored_key_pends_no_migration(self, monkeypatch):
        calls: list[frozenset] = []
        real = loader_module._persist_config_migration

        def _counting(*args, **kwargs):
            calls.append(args[1] if len(args) > 1 else kwargs.get("pending"))
            return real(*args, **kwargs)

        monkeypatch.setattr(loader_module, "_persist_config_migration", _counting)
        path = _write_config(_old_shape())  # default_agent: "Crew Program Manager"
        for _ in range(3):
            loader_module._invalidate_config_cache()
            assert KiroCrewConfig.load().default_agent == "crew-program-manager"
        # The document's spelling is right for the document as stored; nothing
        # is pended for it, so no load re-takes the lock to make no progress.
        assert not any(loader_module.MIGRATE_DEFAULT_AGENT in (p or ()) for p in calls)
        assert json.loads(path.read_text(encoding="utf-8"))["default_agent"] == (
            "Crew Program Manager"
        )
        # The one-shot migration moves it with the record.
        loader_module.migrate_member_identity()
        assert json.loads(path.read_text(encoding="utf-8"))["default_agent"] == (
            "crew-program-manager"
        )


class TestHandleUniquenessIsDecidedUnderTheLock:
    """A create or rename rechecks its handles on the locked document.

    The pre-lock snapshot check (``resolve_member`` on the config the caller
    loaded) is advisory: the dashboard, the CLI and an app's ``ensure_team``
    serialize only on the config flock, so a writer that lands between the
    snapshot and the write is invisible to it. ``persist_member_config`` is the
    one place every path passes through, so the refusal lives inside its
    ``mutate``.
    """

    @staticmethod
    def _land_concurrently(key: str, **fields) -> None:
        """Another writer's record reaches the document behind the caller's snapshot."""
        path = _config_path()
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        on_disk["agents"][key] = {"kiro_agent": "kirocrew", "member_id": key, **fields}
        path.write_text(json.dumps(on_disk), encoding="utf-8")
        loader_module._invalidate_config_cache()

    def test_rename_to_a_label_taken_after_the_snapshot_is_refused(self, keyed_member):
        from kiro_crew.memory_stores import MemberAlreadyExists, persist_member_config

        snapshot = KiroCrewConfig.load()
        assert members_mod.resolve_member("Shared", snapshot) is None
        self._land_concurrently("shared", display_name="Shared")
        snapshot.agents["release-writer"].display_name = "Shared"
        with pytest.raises(MemberAlreadyExists):
            persist_member_config(
                snapshot,
                "release-writer",
                create=False,
                expected_store=keyed_member.memory_store,
                changed_fields={"display_name"},
            )
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))["agents"]
        assert on_disk["release-writer"]["display_name"] == "Release Writer"
        assert on_disk["shared"]["display_name"] == "Shared"

    def test_rename_to_a_slug_shaped_label_that_became_a_key_is_refused(self, keyed_member):
        # The silent variant: the contested string is another record's KEY, so
        # a committed duplicate would not be refused as ambiguous -- the key
        # wins ``resolve_member`` and the renamed member is shadowed.
        from kiro_crew.memory_stores import MemberAlreadyExists, persist_member_config

        snapshot = KiroCrewConfig.load()
        self._land_concurrently("shared")
        snapshot.agents["release-writer"].display_name = "shared"
        with pytest.raises(MemberAlreadyExists):
            persist_member_config(
                snapshot,
                "release-writer",
                create=False,
                expected_store=keyed_member.memory_store,
                changed_fields={"display_name"},
            )
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))["agents"]
        assert on_disk["release-writer"]["display_name"] == "Release Writer"

    def test_create_whose_label_was_taken_after_the_snapshot_is_refused(self, keyed_member):
        from kiro_crew.memory_stores import (
            MemberAlreadyExists,
            persist_member_config,
            provision_member_memory,
        )

        snapshot = KiroCrewConfig.load()
        snapshot.agents["shared-2"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", display_name="Shared"
        )
        provision_member_memory(snapshot, "shared-2")
        self._land_concurrently("shared", display_name="Shared")
        with pytest.raises(MemberAlreadyExists):
            persist_member_config(snapshot, "shared-2", create=True)
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))["agents"]
        assert "shared-2" not in on_disk
        assert on_disk["shared"]["display_name"] == "Shared"

    def test_create_whose_key_is_another_records_legacy_key_is_refused(self, keyed_member):
        from kiro_crew.memory_stores import (
            MemberAlreadyExists,
            persist_member_config,
            provision_member_memory,
        )

        snapshot = KiroCrewConfig.load()
        snapshot.agents["writer"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", display_name="Writer"
        )
        provision_member_memory(snapshot, "writer")
        self._land_concurrently(
            "writer-4b1e0c9d7a22", display_name="Author", legacy_keys=["writer"]
        )
        with pytest.raises(MemberAlreadyExists):
            persist_member_config(snapshot, "writer", create=True)
        assert "writer" not in json.loads(_config_path().read_text(encoding="utf-8"))["agents"]

    def test_an_unchanged_label_and_a_free_label_still_publish(self, keyed_member):
        # Control: the guard litigates only handles the write publishes.
        from kiro_crew.memory_stores import persist_member_config

        snapshot = KiroCrewConfig.load()
        self._land_concurrently("shared", display_name="Shared")
        snapshot.agents["release-writer"].model = "pinned"
        persist_member_config(
            snapshot,
            "release-writer",
            create=False,
            expected_store=keyed_member.memory_store,
            changed_fields={"model"},
        )
        snapshot.agents["release-writer"].display_name = "Release Author"
        persist_member_config(
            snapshot,
            "release-writer",
            create=False,
            expected_store=keyed_member.memory_store,
            changed_fields={"display_name"},
        )
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))["agents"]
        assert on_disk["release-writer"]["model"] == "pinned"
        assert on_disk["release-writer"]["display_name"] == "Release Author"

    def test_a_label_another_member_only_remembers_publishes_and_retires_the_alias(
        self, keyed_member
    ):
        # A legacy key is a forwarding address for references written before a
        # rename; the live namespace wins over it in every resolver, so the
        # label is free -- and taking it retires the alias in the same write,
        # so the document never holds a label that is also an alias.
        from kiro_crew.memory_stores import persist_member_config, provision_member_memory

        snapshot = KiroCrewConfig.load()
        snapshot.agents["writer-2"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", display_name="Release Writer"
        )
        provision_member_memory(snapshot, "writer-2")
        self._land_concurrently(
            "release-writer", display_name="Release Author", legacy_keys=["Release Writer"]
        )
        persist_member_config(snapshot, "writer-2", create=True)
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))["agents"]
        assert on_disk["writer-2"]["display_name"] == "Release Writer"
        assert on_disk["release-writer"].get("legacy_keys", []) == []
        assert members_mod.resolve_member("Release Writer", KiroCrewConfig.load())[0] == "writer-2"

    def test_a_label_an_overlay_entry_is_still_filed_under_is_refused(self, keyed_member):
        # config.local.json is user-owned and never written back: an entry filed
        # under the renamed member's former label would follow the label onto
        # the NEW member, so the label stays reserved until the user re-files it.
        from kiro_crew.config.loader import config_local_path
        from kiro_crew.memory_stores import (
            MemberAlreadyExists,
            persist_member_config,
            provision_member_memory,
        )

        config_local_path().write_text(
            json.dumps({"agents": {"Release Writer": {"model": "pinned"}}}), encoding="utf-8"
        )
        snapshot = KiroCrewConfig.load()
        snapshot.agents["writer-2"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", display_name="Release Writer"
        )
        provision_member_memory(snapshot, "writer-2")
        self._land_concurrently(
            "release-writer", display_name="Release Author", legacy_keys=["Release Writer"]
        )
        with pytest.raises(MemberAlreadyExists, match="config.local.json"):
            persist_member_config(snapshot, "writer-2", create=True)
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))["agents"]
        assert "writer-2" not in on_disk
        assert on_disk["release-writer"]["legacy_keys"] == ["Release Writer"]

    def test_a_malformed_overlay_aborts_taking_a_remembered_label(self, keyed_member):
        # config.local.json is present but not valid JSON. The guard cannot tell
        # whether an entry is still filed under the label, so it refuses: a
        # "no entry" guess would retire the legacy handle, and once the user
        # repairs the file its entry would re-home onto the new member.
        from kiro_crew.config.loader import config_local_path
        from kiro_crew.memory_stores import (
            UnknownMemoryStore,
            persist_member_config,
            provision_member_memory,
        )

        snapshot = KiroCrewConfig.load()
        snapshot.agents["writer-2"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", display_name="Release Writer"
        )
        provision_member_memory(snapshot, "writer-2")
        self._land_concurrently(
            "release-writer", display_name="Release Author", legacy_keys=["Release Writer"]
        )
        # The overlay breaks AFTER the snapshot was taken: the loader is not in
        # the way, only the in-lock guard is.
        config_local_path().write_text('{"agents": {"Release Writer": ', encoding="utf-8")
        with pytest.raises(UnknownMemoryStore, match="config.local.json"):
            persist_member_config(snapshot, "writer-2", create=True)
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))["agents"]
        assert "writer-2" not in on_disk
        assert on_disk["release-writer"]["legacy_keys"] == ["Release Writer"]

    def test_the_overlay_is_locked_from_the_check_to_the_commit(self, keyed_member, monkeypatch):
        """Taking a remembered label holds config.local.json's OWN lock from the
        overlay read until the base document has committed, so a ``config set
        --local`` cannot land an entry under the label in that window and find
        it re-homed onto the new member. Probed from inside the check (the
        overlay writer's non-waiting acquire must be refused) and again from
        ``after_write`` (still refused: the commit is inside the window)."""
        from kiro_crew import memory_stores
        from kiro_crew.config import loader
        from kiro_crew.config.loader import config_local_path
        from kiro_crew.memory_stores import persist_member_config, provision_member_memory

        snapshot = KiroCrewConfig.load()
        snapshot.agents["writer-2"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", display_name="Release Writer"
        )
        provision_member_memory(snapshot, "writer-2")
        self._land_concurrently(
            "release-writer", display_name="Release Author", legacy_keys=["Release Writer"]
        )
        config_local_path().write_text(json.dumps({"agents": {}}), encoding="utf-8")

        def _overlay_writer_is_refused() -> bool:
            try:
                with loader._config_write_lock(config_local_path(), wait=False):
                    return False
            except OSError:
                return True

        probes: list[tuple[str, bool]] = []
        real_check = memory_stores._overlay_files_agent_under

        def _probing_check(handle: str) -> bool:
            probes.append(("check", _overlay_writer_is_refused()))
            return real_check(handle)

        real_update = loader.update_config_locked

        def _probing_update(*args, **kwargs):
            inner = kwargs.get("after_write")

            def _after() -> None:
                probes.append(("commit", _overlay_writer_is_refused()))
                if inner is not None:
                    inner()

            kwargs["after_write"] = _after
            return real_update(*args, **kwargs)

        monkeypatch.setattr(memory_stores, "_overlay_files_agent_under", _probing_check)
        # ``persist_member_config`` imports the symbol at call time, from the loader.
        monkeypatch.setattr(loader, "update_config_locked", _probing_update)
        persist_member_config(snapshot, "writer-2", create=True)
        assert probes == [("check", True), ("commit", True)]
        # And released afterwards: the next overlay writer gets in.
        assert _overlay_writer_is_refused() is False
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))["agents"]
        assert on_disk["writer-2"]["display_name"] == "Release Writer"
        assert on_disk["release-writer"]["legacy_keys"] == []

    def test_an_orphaned_overlay_entry_is_never_reassigned_to_a_new_member(self, keyed_member):
        """Delete removes the base record, never the user-owned overlay, so an
        entry filed under a departed member's label is ordinary residue. A new
        member taking that label would silently inherit the entry's fields at
        the next load; the create refuses instead and names the entry."""
        from kiro_crew.config.loader import config_local_path
        from kiro_crew.memory_stores import (
            MemberAlreadyExists,
            persist_member_config,
            provision_member_memory,
        )

        config_local_path().write_text(
            json.dumps({"agents": {"Departed Writer": {"model": "pinned"}}}), encoding="utf-8"
        )
        # No BASE record holds the label (the merged view shows the overlay entry
        # as a stray agent; the locked write sees only the base document).
        assert (
            "Departed Writer"
            not in json.loads(_config_path().read_text(encoding="utf-8"))["agents"]
        )
        snapshot = KiroCrewConfig.load()
        snapshot.agents["writer-3"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", display_name="Departed Writer"
        )
        provision_member_memory(snapshot, "writer-3")
        with pytest.raises(MemberAlreadyExists, match="belongs to no current member"):
            persist_member_config(snapshot, "writer-3", create=True)
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))["agents"]
        assert "writer-3" not in on_disk

    def test_an_unreadable_overlay_aborts_taking_a_remembered_label(self, keyed_member):
        # Present but unreadable (a directory where the file should be) is the
        # same answer as malformed: refuse, write nothing, keep the alias.
        from kiro_crew.config.loader import config_local_path
        from kiro_crew.memory_stores import (
            UnknownMemoryStore,
            persist_member_config,
            provision_member_memory,
        )

        config_local_path().mkdir()
        snapshot = KiroCrewConfig.load()
        snapshot.agents["writer-2"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", display_name="Release Writer"
        )
        provision_member_memory(snapshot, "writer-2")
        self._land_concurrently(
            "release-writer", display_name="Release Author", legacy_keys=["Release Writer"]
        )
        with pytest.raises(UnknownMemoryStore, match="config.local.json"):
            persist_member_config(snapshot, "writer-2", create=True)
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))["agents"]
        assert "writer-2" not in on_disk
        assert on_disk["release-writer"]["legacy_keys"] == ["Release Writer"]

    @pytest.mark.asyncio
    async def test_rename_route_refuses_with_409_when_the_overlay_is_malformed(
        self, tmp_path, keyed_member
    ):
        from kiro_crew.config.loader import config_local_path
        from kiro_crew.memory_stores import persist_member_config, provision_member_memory

        cfg = KiroCrewConfig.load()
        cfg.agents["writer-2"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="Author")
        provision_member_memory(cfg, "writer-2")
        persist_member_config(cfg, "writer-2", create=True)
        self._land_concurrently(
            "release-writer", display_name="Release Author", legacy_keys=["Release Writer"]
        )
        config_local_path().write_text("{not json", encoding="utf-8")
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            resp = await client.put("/api/agents/writer-2", json={"display_name": "Release Writer"})
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "member_memory_unavailable"
            assert "config.local.json" in body["error"]
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))["agents"]
        assert on_disk["writer-2"]["display_name"] == "Author"
        assert on_disk["release-writer"]["legacy_keys"] == ["Release Writer"]

    @pytest.mark.asyncio
    async def test_rename_route_lets_a_member_take_a_label_another_only_remembers(
        self, tmp_path, keyed_member
    ):
        from kiro_crew.memory_stores import persist_member_config, provision_member_memory

        cfg = KiroCrewConfig.load()
        cfg.agents["writer-2"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="Author")
        provision_member_memory(cfg, "writer-2")
        persist_member_config(cfg, "writer-2", create=True)
        self._land_concurrently(
            "release-writer", display_name="Release Author", legacy_keys=["Release Writer"]
        )
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            resp = await client.put("/api/agents/writer-2", json={"display_name": "Release Writer"})
            assert resp.status == 200, await resp.text()
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))["agents"]
        assert on_disk["writer-2"]["display_name"] == "Release Writer"
        assert on_disk["release-writer"].get("legacy_keys", []) == []

    @pytest.mark.asyncio
    async def test_rename_route_answers_the_lock_time_conflict_with_409(
        self, tmp_path, keyed_member, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import agents as agents_handlers

        # The handler's snapshot sees the label free (the concurrent writer
        # has not landed yet from its point of view) ...
        real = agents_handlers.resolve_member

        def stale(handle, cfg, *a, **k):
            return None if handle == "Shared" else real(handle, cfg, *a, **k)

        monkeypatch.setattr(agents_handlers, "resolve_member", stale)
        # ... but the document under the lock already holds it.
        self._land_concurrently("shared", display_name="Shared")
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            resp = await client.put("/api/agents/release-writer", json={"display_name": "Shared"})
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "agent_exists"
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))["agents"]
        assert on_disk["release-writer"]["display_name"] == "Release Writer"
