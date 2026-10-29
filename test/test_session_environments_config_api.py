"""The dashboard config API for the session_environments section: the masked
GET and the PATCH that validates with the loader's own normalizers."""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from test_session_environments_config import (
    _BASE_CONFIG,
    _DEFAULTS,
    _profile,
    _stored,
    _write_section,
)

from kiro_crew.dashboard.handlers import api_kirocrew_config, api_kirocrew_config_patch


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_BASE_CONFIG), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    return path


def _app() -> web.Application:
    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    app.router.add_get("/api/config/kirocrew", api_kirocrew_config)
    return as_owner(app)


async def _patch(client, path, value):
    return await client.patch("/api/config/kirocrew", json={"path": path, "value": value})


async def _section(client) -> dict:
    return (await (await client.get("/api/config/kirocrew")).json())["session_environments"]


def _api() -> TestClient:
    return TestClient(TestServer(_app()))


class TestApi:
    pytestmark = pytest.mark.asyncio

    async def test_get_exposes_safe_defaults(self, config_file):
        async with _api() as client:
            resp = await client.get("/api/config/kirocrew")
            assert resp.status == 200
            assert (await resp.json())["session_environments"] == {
                "coder_url": "",
                "profiles": [],
                **_DEFAULTS,
            }

    async def test_whole_section_saves_and_preserves_unrelated_config(self, config_file):
        section = {
            "coder_url": " https://c.example.com ",
            "profiles": [_profile("a"), _profile("b", name="  B  ")],
        }
        async with _api() as client:
            resp = await _patch(client, "session_environments", section)
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        expected = {
            "coder_url": "https://c.example.com",
            "profiles": [_profile("a"), _profile("b", name="B")],
        }
        # The read shows the retention default; the file holds only what was sent.
        assert body["session_environments"] == {**expected, **_DEFAULTS}
        stored = _stored(config_file)
        assert stored["session_environments"] == expected
        assert stored["session"]["timeout_secs"] == 1234
        assert stored["an_edition_section"] == {"keep": "me"}
        assert stored["default_agent"] == "kirocrew"

    async def test_a_whole_section_save_keeps_omitted_optional_keys(self, config_file):
        """SECTION_OPTIONAL_KEYS left out of a whole-section save (an agent's change
        card may carry only the required keys) keep their saved values; an explicit
        value still overwrites, and no other stored key is carried over."""
        full = {
            "coder_url": "https://c.example.com",
            "profiles": [_profile("a"), _profile("b")],
            "default_profile": "a",
            "stop_on_shutdown": False,
        }
        required_only = {
            "coder_url": "https://c.example.com",
            "profiles": [_profile("a"), _profile("b")],
        }
        async with _api() as client:
            assert (await _patch(client, "session_environments", full)).status == 200
            stored = _stored(config_file)
            stored["session_environments"]["unmodelled"] = "dropped on a whole save"
            config_file.write_text(json.dumps(stored), encoding="utf-8")
            assert (await _patch(client, "session_environments", required_only)).status == 200
            saved = _stored(config_file)["session_environments"]
            assert saved["default_profile"] == "a" and saved["stop_on_shutdown"] is False
            assert "unmodelled" not in saved
            # An explicit value still overwrites.
            explicit = {**required_only, "default_profile": "b", "stop_on_shutdown": True}
            assert (await _patch(client, "session_environments", explicit)).status == 200
            saved = _stored(config_file)["session_environments"]
            assert saved["default_profile"] == "b" and saved["stop_on_shutdown"] is True
            # A kept default whose profile this save removes is dropped, not left dangling.
            only_a = {"coder_url": "https://c.example.com", "profiles": [_profile("a")]}
            assert (await _patch(client, "session_environments", only_a)).status == 200
            saved = _stored(config_file)["session_environments"]
            assert "default_profile" not in saved and saved["stop_on_shutdown"] is True
            # A hand-edited saved value the loader would not accept is not carried
            # over, and never turns a valid save into an error.
            stored = _stored(config_file)
            stored["session_environments"]["default_profile"] = ["a"]
            stored["session_environments"]["stop_on_shutdown"] = "no"
            config_file.write_text(json.dumps(stored), encoding="utf-8")
            assert (await _patch(client, "session_environments", only_a)).status == 200
            saved = _stored(config_file)["session_environments"]
            assert "default_profile" not in saved and "stop_on_shutdown" not in saved

    async def test_the_default_profile_leaf_names_only_a_saved_profile(self, config_file):
        """The leaf path (a change card, or a direct PATCH) is held to what a whole
        section save checks: a default that names no saved profile is refused."""
        async with _api() as client:
            section = {"coder_url": "https://c.example.com", "profiles": [_profile("a")]}
            assert (await _patch(client, "session_environments", section)).status == 200
            resp = await _patch(client, "session_environments.default_profile", "big-box")
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "unknown_profile" and "big-box" in body["error"]
            assert "default_profile" not in _stored(config_file)["session_environments"]
            # A saved id is accepted, and "" (this machine) always is.
            assert (await _patch(client, "session_environments.default_profile", "a")).status == 200
            assert _stored(config_file)["session_environments"]["default_profile"] == "a"
            assert (await _patch(client, "session_environments.default_profile", "")).status == 200
            assert _stored(config_file)["session_environments"]["default_profile"] == ""
            assert (await _patch(client, "session_environments.default_profile", "a")).status == 200
            assert (
                await _patch(client, "session_environments.default_profile", None)
            ).status == 200
            assert _stored(config_file)["session_environments"]["default_profile"] == ""
            # The refusal names only the id: no config path or other host detail.
            assert str(config_file.parent) not in body["error"] and "/" not in body["error"]
            # A later Settings save of the section still succeeds.
            assert (await _patch(client, "session_environments", section)).status == 200

    async def test_one_save_adds_a_profile_and_makes_it_the_default(self, config_file):
        """Checked against the profiles the save itself keeps, not the ones before it."""
        async with _api() as client:
            first = {"coder_url": "https://c.example.com", "profiles": [_profile("a")]}
            assert (await _patch(client, "session_environments", first)).status == 200
            both = {**first, "profiles": [_profile("a"), _profile("b")], "default_profile": "b"}
            resp = await _patch(client, "session_environments", both)
            assert resp.status == 200, await resp.text()
            assert _stored(config_file)["session_environments"]["default_profile"] == "b"
            # Deleting the profile that is the default is never refused: the
            # default is dropped with it.
            resp = await _patch(client, "session_environments", first)
            assert resp.status == 200, await resp.text()
            assert "default_profile" not in _stored(config_file)["session_environments"]

    async def test_a_default_profile_leaf_with_no_saved_section_is_refused(self, config_file):
        async with _api() as client:
            resp = await _patch(client, "session_environments.default_profile", "a")
            assert resp.status == 400 and (await resp.json())["code"] == "unknown_profile"
            assert "session_environments" not in _stored(config_file)

    async def test_leaf_paths_update_one_scalar_and_keep_the_profiles(self, config_file):
        async with _api() as client:
            section = {"coder_url": "", "profiles": [_profile()]}
            assert (await _patch(client, "session_environments", section)).status == 200
            resp = await _patch(client, "session_environments.coder_url", "https://c.example.com")
            assert resp.status == 200
            got = await _section(client)
        assert got == {"coder_url": "https://c.example.com", "profiles": [_profile()], **_DEFAULTS}

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            ("session_environments", {"coder_url": "https://c.example.com"}),
            ("session_environments", {"coder_url": "", "profiles": [], "token": "x"}),
            ("session_environments", "https://c.example.com"),
            ("session_environments.coder_url", "https://c.example.com?token=x"),
            ("session_environments.coder_url", "file:///etc/passwd"),
            ("session_environments", {"coder_url": "", "profiles": [_profile("a"), _profile("a")]}),
            ("session_environments", {"coder_url": "", "profiles": [_profile(workdir="repo")]}),
            (
                "session_environments",
                {"coder_url": "", "profiles": [{**_profile(), "password": "x"}]},
            ),
            (
                "session_environments",
                {"coder_url": "", "profiles": [_profile(f"p{i}") for i in range(21)]},
            ),
            # Profiles are written only with the whole section: no leaf path.
            ("session_environments.profiles", [_profile()]),
            ("session_environments.enabled", True),
        ],
    )
    async def test_invalid_values_are_refused_and_nothing_is_written(
        self, config_file, path, value
    ):
        before = config_file.read_bytes()
        async with _api() as client:
            resp = await _patch(client, path, value)
            assert resp.status == 400
            assert (await resp.json())["error"]
        assert config_file.read_bytes() == before

    async def test_get_drops_unmodelled_keys_but_save_keeps_them(self, config_file):
        """A key a newer build wrote survives the write but is not shown (the schema
        cannot vouch that it is not secret)."""
        _write_section(config_file, {"coder_url": "", "profiles": [], "future": "x"})
        async with _api() as client:
            resp = await _patch(client, "session_environments.coder_url", "https://c.example.com")
            assert resp.status == 200
            got = await _section(client)
        assert got == {"coder_url": "https://c.example.com", "profiles": [], **_DEFAULTS}
        assert _stored(config_file)["session_environments"]["future"] == "x"

    @pytest.mark.parametrize("field", ["", ".archived_retention_days"])
    async def test_retention_is_not_a_setting(self, config_file, field):
        # Fixed at ARCHIVED_RETENTION_DAYS: refused as a field and as a section key.
        section = {"coder_url": "https://c.example.com", "profiles": []}
        value = 2 if field else {**section, "archived_retention_days": 2}
        before = config_file.read_bytes()
        async with _api() as client:
            resp = await _patch(client, "session_environments" + field, value)
            assert resp.status == 400
        assert config_file.read_bytes() == before
