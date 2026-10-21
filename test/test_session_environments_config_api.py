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

    async def test_the_default_profile_leaf_may_name_a_kiro_cloud_profile(self, config_file):
        """Every saved profile list counts, not only the Coder one."""
        async with _api() as client:
            section = {
                "coder_url": "",
                "profiles": [],
                "kiro_cloud_profiles": [{"id": "cloud", "name": "Cloud", "repositories": []}],
            }
            assert (await _patch(client, "session_environments", section)).status == 200
            resp = await _patch(client, "session_environments.default_profile", "cloud")
            assert resp.status == 200, await resp.text()
            assert _stored(config_file)["session_environments"]["default_profile"] == "cloud"
            resp = await _patch(client, "session_environments.default_profile", "big-box")
            assert resp.status == 400 and (await resp.json())["code"] == "unknown_profile"

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

    @pytest.mark.asyncio
    async def test_a_kiro_cloud_profile_round_trips_and_survives_an_older_client(self, config_file):
        cloud = {"id": "cloud", "name": "Kiro Cloud", "repositories": ["acme/web"]}
        async with _api() as client:
            resp = await _patch(client, "session_environments.kiro_cloud_profiles", [cloud])
            assert resp.status == 200, await resp.text()
            # A dashboard that predates Kiro Cloud saves the section without it.
            resp = await _patch(
                client,
                "session_environments",
                {"coder_url": "https://c.example.com", "profiles": []},
            )
            assert resp.status == 200, await resp.text()
            got = await _section(client)
            # The id decides the provider, so a Coder profile may not reuse it.
            clash = await _patch(
                client,
                "session_environments",
                {
                    "coder_url": "https://c.example.com",
                    "profiles": [_profile("cloud")],
                    "kiro_cloud_profiles": [cloud],
                },
            )
            assert clash.status == 400
        assert got["kiro_cloud_profiles"] == [cloud]

    @pytest.mark.parametrize("default", ["kc1", "dev"])
    async def test_a_cloud_list_leaf_drops_a_default_whose_profile_it_removes(
        self, config_file, default
    ):
        """A leaf write that removes the default's profile drops the default, as a
        whole-section save does, so it never names a profile that is gone."""
        seeded = {
            "coder_url": "https://c.example.com",
            "profiles": [_profile("dev")],
            "kiro_cloud_profiles": [{"id": "kc1", "name": "Cloud", "repositories": []}],
            "default_profile": default,
        }
        async with _api() as client:
            assert (await _patch(client, "session_environments", seeded)).status == 200
            resp = await _patch(client, "session_environments.kiro_cloud_profiles", [])
            assert resp.status == 200, await resp.text()
            saved = _stored(config_file)["session_environments"]
            assert saved["kiro_cloud_profiles"] == []
            if default == "kc1":
                assert "default_profile" not in saved
            else:
                # A default naming a profile the write kept is left alone.
                assert saved["default_profile"] == "dev"
            # The next whole-section save of what the tab shows still succeeds.
            assert (await _patch(client, "session_environments", saved)).status == 200

    @pytest.mark.parametrize(
        "bad_entry,default,kept",
        [
            # A hand-edited profile id that is not a string sits beside a valid one.
            ({"id": ["dev"], "name": "Bad"}, "kc1", True),
            ({"id": {"x": 1}, "name": "Bad"}, "kc1", True),
            # A hand-edited default that is not a string is dropped, not crashed on.
            ({"id": "dev2", "name": "Ok"}, ["kc1"], False),
            ({"id": "dev2", "name": "Ok"}, {"id": "kc1"}, False),
        ],
    )
    async def test_a_malformed_hand_edit_does_not_fail_an_unrelated_leaf_write(
        self, config_file, bad_entry, default, kept
    ):
        """The dangling-default check reads the section raw: a non-string profile id
        or default from a hand edit must not turn a valid write into a 500."""
        _write_section(
            config_file,
            {
                "coder_url": "https://c.example.com",
                "profiles": [_profile("dev"), bad_entry],
                "kiro_cloud_profiles": [{"id": "kc1", "name": "Cloud", "repositories": []}],
                "default_profile": default,
            },
        )
        async with _api() as client:
            resp = await _patch(client, "session_environments.stop_on_shutdown", False)
            assert resp.status == 200, await resp.text()
            saved = _stored(config_file)["session_environments"]
            assert saved["stop_on_shutdown"] is False
            if kept:
                assert saved["default_profile"] == "kc1"
            else:
                assert "default_profile" not in saved
            # The explicit default leaf reads the same raw lists, and is not crashed either.
            dflt = await _patch(client, "session_environments.default_profile", "kc1")
            assert dflt.status == 200, await dflt.text()
        assert _stored(config_file)["session_environments"]["default_profile"] == "kc1"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path,value",
        [
            # A leaf write of the cloud list, checked against the Coder list on disk.
            # (Coder profiles have no leaf path; the whole-section case covers them.)
            (
                "session_environments.kiro_cloud_profiles",
                [{"id": "dev", "name": "Dev", "repositories": []}],
            ),
            # A whole-section save that omits the cloud list, which the writer preserves.
            (
                "session_environments",
                {"coder_url": "https://c.example.com", "profiles": [_profile("cloud")]},
            ),
        ],
    )
    async def test_a_write_that_would_share_an_id_across_providers_is_refused(
        self, config_file, path, value
    ):
        async with _api() as client:
            seed = await _patch(
                client,
                "session_environments",
                {
                    "coder_url": "https://c.example.com",
                    "profiles": [_profile("dev")],
                    "kiro_cloud_profiles": [
                        {"id": "cloud", "name": "Kiro Cloud", "repositories": []}
                    ],
                },
            )
            assert seed.status == 200, await seed.text()
            before = config_file.read_bytes()
            resp = await _patch(client, path, value)
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == "profile_id_collision"
        assert config_file.read_bytes() == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stored",
        [
            {"profiles": [{"name": "no id"}, "not an object", None, _profile("dev")]},
            {"profiles": None, "kiro_cloud_profiles": None},
            {"profiles": [], "kiro_cloud_profiles": [{"name": "no id"}, 7, None]},
            {"profiles": [_profile("dev", id=3)], "kiro_cloud_profiles": [{"id": 3}]},
        ],
    )
    async def test_a_hand_edited_entry_the_loader_drops_does_not_break_a_write(
        self, config_file, stored
    ):
        """The id check reads each list the way the loader does, so a dropped entry cannot 500."""
        _write_section(config_file, {"coder_url": "https://c.example.com", **stored})
        cloud = [{"id": "cloud", "name": "Kiro Cloud", "repositories": []}]
        async with _api() as client:
            # A leaf that rewrites neither list: the check reads both as stored.
            resp = await _patch(client, "session_environments.stop_on_shutdown", False)
            assert resp.status == 200, await resp.text()
            resp = await _patch(client, "session_environments.kiro_cloud_profiles", cloud)
            assert resp.status == 200, await resp.text()
            resp = await _patch(client, "session_environments.default_profile", "cloud")
            assert resp.status == 200, await resp.text()
        assert _stored(config_file)["session_environments"]["kiro_cloud_profiles"] == cloud


class TestStaleCardSave:
    """A whole-section save from a card or tab that predates a provider keeps that
    provider's saved profiles (and a default naming one), never wiping them."""

    pytestmark = pytest.mark.asyncio

    async def test_a_save_that_omits_the_cloud_list_keeps_it_and_its_default(self, config_file):
        cloud = {"id": "cloud", "name": "Kiro Cloud", "repositories": ["acme/web"]}
        seeded = {
            "coder_url": "https://c.example.com",
            "profiles": [_profile("dev")],
            "kiro_cloud_profiles": [cloud],
            "default_profile": "cloud",
        }
        # What an older Settings tab or an agent's change card sends: only the keys it knows.
        stale = {"coder_url": "https://c.example.com", "profiles": [_profile("dev")]}
        async with _api() as client:
            assert (await _patch(client, "session_environments", seeded)).status == 200
            resp = await _patch(client, "session_environments", stale)
            assert resp.status == 200, await resp.text()
        saved = _stored(config_file)["session_environments"]
        assert saved["kiro_cloud_profiles"] == [cloud]
        assert saved["default_profile"] == "cloud"

    async def test_a_hand_edited_cloud_list_the_loader_refuses_is_not_carried(self, config_file):
        _write_section(
            config_file,
            {
                "coder_url": "https://c.example.com",
                "profiles": [],
                "kiro_cloud_profiles": [{"id": "Not Valid!", "repositories": "x"}],
                "default_profile": "Not Valid!",
            },
        )
        stale = {"coder_url": "https://c.example.com", "profiles": []}
        async with _api() as client:
            assert (await _patch(client, "session_environments", stale)).status == 200
        saved = _stored(config_file)["session_environments"]
        assert "kiro_cloud_profiles" not in saved and "default_profile" not in saved

    @pytest.mark.parametrize(
        "malformed",
        [
            {"id": "Not Valid!", "repositories": "x"},
            "not an object",
            {"id": "cloud", "name": "Duplicate", "repositories": []},
        ],
        ids=["bad-entry", "not-an-object", "duplicate-id"],
    )
    async def test_one_hand_edited_cloud_entry_does_not_drop_the_valid_ones(
        self, config_file, malformed
    ):
        """The omitted list is carried as the loader reads it: entry by entry."""
        cloud = {"id": "cloud", "name": "Kiro Cloud", "repositories": ["acme/web"]}
        _write_section(
            config_file,
            {
                "coder_url": "https://c.example.com",
                "profiles": [],
                "kiro_cloud_profiles": [cloud, malformed],
                "default_profile": "cloud",
            },
        )
        stale = {"coder_url": "https://c.example.com", "profiles": []}
        async with _api() as client:
            assert (await _patch(client, "session_environments", stale)).status == 200
        saved = _stored(config_file)["session_environments"]
        assert saved["kiro_cloud_profiles"] == [cloud]
        assert saved["default_profile"] == "cloud"


def test_every_optional_key_has_the_loaders_validator():
    """A key without a validator is never carried, so a new provider list must add one."""
    from kiro_crew.config.session_environments import SECTION_OPTIONAL_KEYS
    from kiro_crew.dashboard.handlers.core_config_patch import (
        _session_environment_key_validators,
    )

    assert set(_session_environment_key_validators()) == set(SECTION_OPTIONAL_KEYS)
