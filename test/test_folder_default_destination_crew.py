"""Default destination crew: the crew *Send a copy to* lists first.

Two stores feed it: the global ``instances.default_destination`` config key,
served with the crew list, and a chat folder's ``default_destination_crew``,
which overrides it for sessions in that folder. Both only order and mark the
menu; nothing here sends anything.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_folder_app, _make_state
from instances_helpers import _body, _FakeReq, _State

from kiro_crew.instances.crew_ref import MAX_CREW_REF_LEN, clean_crew_ref


def _enable_with(tmp_path, monkeypatch, **section) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"instances": {"enabled": True, **section}}))
    from kiro_crew.config import loader

    loader._invalidate_config_cache()


class TestCleanCrewRef:
    def test_strips_and_keeps_a_string(self) -> None:
        assert clean_crew_ref("  crew-a1 ") == "crew-a1"

    @pytest.mark.parametrize("junk", [None, 7, ["crew-a1"], {"id": "crew-a1"}, True])
    def test_a_non_string_names_no_crew(self, junk) -> None:
        assert clean_crew_ref(junk) == ""

    def test_caps_the_length(self) -> None:
        assert len(clean_crew_ref("c" * (MAX_CREW_REF_LEN + 50))) == MAX_CREW_REF_LEN


class TestServedWithTheCrewList:
    def _state(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry

        return _State(InstancesRegistry(path=tmp_path / "instances.json"))

    def test_unset_serves_empty(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable_with(tmp_path, monkeypatch)
        body = _body(asyncio.run(handlers.api_instances_list(_FakeReq(self._state(tmp_path)))))
        assert body["default_destination"] == ""

    def test_set_serves_the_stored_id_cleaned(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable_with(tmp_path, monkeypatch, default_destination="  crew-a1 ")
        body = _body(asyncio.run(handlers.api_instances_list(_FakeReq(self._state(tmp_path)))))
        assert body["default_destination"] == "crew-a1"

    def test_a_non_string_config_value_is_no_default(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable_with(tmp_path, monkeypatch, default_destination=42)
        body = _body(asyncio.run(handlers.api_instances_list(_FakeReq(self._state(tmp_path)))))
        assert body["default_destination"] == ""


class TestSavedFromSettings:
    """The Settings picker saves through the real config PATCH route, and the
    crew list serves what it wrote. Mocking ``patchConfig`` in the frontend
    tests cannot catch a path the route's allow-list does not name."""

    @staticmethod
    def _patch_app():
        from aiohttp import web

        from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

        @web.middleware
        async def _owner(request, handler):
            request["user"] = "local-app"
            request["app"] = ""
            return await handler(request)

        app = web.Application(middlewares=[_owner])
        app["state"] = type("S", (), {"owner_id": ""})()
        app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
        return app

    @pytest.mark.asyncio
    async def test_a_patch_is_served_back_with_the_crew_list(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.config import loader
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.registry import InstancesRegistry

        _enable_with(tmp_path, monkeypatch)
        async with TestClient(TestServer(self._patch_app())) as client:
            resp = await client.patch(
                "/api/config/kirocrew",
                json={"path": "instances.default_destination", "value": "crew-a1"},
            )
            assert resp.status == 200, await resp.text()
        loader._invalidate_config_cache()
        state = _State(InstancesRegistry(path=tmp_path / "instances.json"))
        body = _body(await handlers.api_instances_list(_FakeReq(state)))
        assert body["default_destination"] == "crew-a1"

    @pytest.mark.asyncio
    async def test_clearing_and_bad_values(self, tmp_path, monkeypatch) -> None:
        _enable_with(tmp_path, monkeypatch, default_destination="crew-a1")
        async with TestClient(TestServer(self._patch_app())) as client:
            path = "instances.default_destination"
            for bad in (7, None, "c" * (MAX_CREW_REF_LEN + 1)):
                resp = await client.patch("/api/config/kirocrew", json={"path": path, "value": bad})
                assert resp.status == 400, (bad, await resp.text())
            # "" is None in the picker: it clears the default.
            resp = await client.patch("/api/config/kirocrew", json={"path": path, "value": ""})
            assert resp.status == 200, await resp.text()
        written = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert written["instances"]["default_destination"] == ""
        assert written["instances"]["enabled"] is True


class TestFolderOverride:
    @pytest.mark.asyncio
    async def test_create_stores_it_cleaned(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Payments", "default_destination_crew": " crew-a1 "},
            )
            assert resp.status == 201
            assert (await resp.json())["default_destination_crew"] == "crew-a1"
            assert state._folders[0]["default_destination_crew"] == "crew-a1"

    @pytest.mark.asyncio
    async def test_create_without_it_is_no_override(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Oncall"})
            assert (await resp.json())["default_destination_crew"] == ""

    @pytest.mark.asyncio
    async def test_update_sets_and_clears_it(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": False}]
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.patch(
                "/api/chat/folders/f1", json={"default_destination_crew": "crew-a1"}
            )
            assert (await resp.json())["default_destination_crew"] == "crew-a1"
            resp = await client.patch(
                "/api/chat/folders/f1", json={"default_destination_crew": None}
            )
            assert (await resp.json())["default_destination_crew"] == ""

    @pytest.mark.asyncio
    async def test_a_rename_keeps_it(self, tmp_path, monkeypatch) -> None:
        """A rename must not wipe the crew another client set."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [
            {
                "id": "f1",
                "name": "Test",
                "order": 0,
                "collapsed": False,
                "default_destination_crew": "crew-a1",
            }
        ]
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.patch("/api/chat/folders/f1", json={"name": "Renamed"})
            assert (await resp.json())["default_destination_crew"] == "crew-a1"

    def test_a_folder_carrying_it_is_kept_by_cleanup(self) -> None:
        from kiro_crew.dashboard.chat_folder_cleanup import removable_folder_ids

        folders = [
            {"id": "top", "name": "top", "order": 0, "parent_id": ""},
            {
                "id": "leaf",
                "name": "leaf",
                "order": 0,
                "parent_id": "top",
                "default_destination_crew": "crew-a1",
            },
        ]
        assert removable_folder_ids(folders, set(), include_top_level=True) == []
