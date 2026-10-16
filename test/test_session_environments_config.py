"""Persisted Session Environments settings: validator, loader and dashboard API.

The section is CONFIGURATION ONLY -- a Coder URL and named workspace profiles the
Settings UI offers behind the per-device preview flag. These pin:

* one validator (``config/session_environments.py``) decides what is valid, and
  both the loader and ``PATCH /api/config/kirocrew`` use it;
* an absent section loads as ``{"coder_url": "", "profiles": []}`` and a valid one
  round-trips through ``load()`` -> ``save()`` unchanged;
* a hand-edited file with one bad profile keeps the valid ones (lenient load),
  while the API REFUSES the same value outright (nothing is written);
* a write preserves every unrelated config key and calls nothing but the config
  writer -- no provider, connection or provisioning path is reachable from it;
* ``GET`` returns exactly the non-secret fields.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config import session_environments as se

_BASE_CONFIG = {
    "agents": {"kirocrew": {"kiro_agent": "kirocrew"}},
    "default_agent": "kirocrew",
    "session": {"timeout_secs": 1234},
    "an_edition_section": {"keep": "me"},
}


def _profile(pid: str = "dev", **overrides: object) -> dict:
    base = {
        "id": pid,
        "name": "Dev box",
        "template": "python-dev",
        "workdir": "/workspace/repo",
        "compute_survives_stop": True,
    }
    base.update(overrides)
    return base


def _app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config, api_kirocrew_config_patch

    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    app.router.add_get("/api/config/kirocrew", api_kirocrew_config)
    return as_owner(app)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_BASE_CONFIG), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    return path


def _stored(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


async def _patch(client, path, value):
    return await client.patch("/api/config/kirocrew", json={"path": path, "value": value})


# ── Validator ──────────────────────────────────────────────────────────────


class TestCoderUrl:
    @pytest.mark.parametrize(
        "url",
        ["", "https://coder.example.com", "http://10.0.0.5:3000", "https://c.example.com/base/"],
    )
    def test_accepts(self, url):
        assert se.validate_coder_url(url) == url

    def test_strips_whitespace(self):
        assert se.validate_coder_url("  https://c.example.com  ") == "https://c.example.com"

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://c.example.com",
            "c.example.com",
            "https://",
            "https://user@c.example.com",
            "https://user:pw@c.example.com",
            "https://c.example.com?x=1",
            "https://c.example.com?",
            "https://c.example.com#frag",
            "https://c.example.com:99999",
            "https://c.exa mple.com",
            "https://c.exa\tmple.com",
            "javascript:alert(1)",
            123,
            None,
        ],
    )
    def test_rejects(self, url):
        with pytest.raises(se.SessionEnvironmentsError):
            se.validate_coder_url(url)


class TestProfiles:
    def test_valid_list(self):
        profiles = [_profile("a"), _profile("B-2_x")]
        assert se.validate_profiles(profiles) == profiles

    def test_id_max_length(self):
        assert se.validate_profile(_profile("a" * 64))["id"] == "a" * 64
        with pytest.raises(se.SessionEnvironmentsError):
            se.validate_profile(_profile("a" * 65))

    @pytest.mark.parametrize("pid", ["", "-dev", "_dev", "dev box", "dév", "dev.1", "dev\n", 5])
    def test_bad_id(self, pid):
        with pytest.raises(se.SessionEnvironmentsError, match=r"\.id"):
            se.validate_profile(_profile(pid))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("name", ""),
            ("name", "   "),
            ("name", "x" * 129),
            ("name", 7),
            ("template", ""),
            ("template", "t" * 256),
            ("template", "a\x00b"),
            ("workdir", ""),
            ("workdir", "relative/path"),
            ("workdir", "C:\\repo"),
            ("workdir", "/" + "p" * 1024),
        ],
    )
    def test_bad_field(self, field, value):
        with pytest.raises(se.SessionEnvironmentsError, match=field):
            se.validate_profile(_profile(**{field: value}))

    def test_bounds_inclusive(self):
        p = _profile(name="n" * 128, template="t" * 255, workdir="/" + "p" * 1023)
        assert se.validate_profile(p) == p

    def test_unknown_key_refused_so_no_credential_can_ride_along(self):
        with pytest.raises(se.SessionEnvironmentsError, match="unknown"):
            se.validate_profile({**_profile(), "token": "secret"})

    def test_missing_key(self):
        p = _profile()
        del p["workdir"]
        with pytest.raises(se.SessionEnvironmentsError, match="missing"):
            se.validate_profile(p)

    def test_duplicate_id(self):
        with pytest.raises(se.SessionEnvironmentsError, match="duplicate"):
            se.validate_profiles([_profile("a"), _profile("a")])

    def test_cap(self):
        ok = [_profile(f"p{i}") for i in range(se.MAX_PROFILES)]
        assert len(se.validate_profiles(ok)) == se.MAX_PROFILES
        with pytest.raises(se.SessionEnvironmentsError, match="at most"):
            se.validate_profiles(ok + [_profile("extra")])

    @pytest.mark.parametrize("value", [None, {}, "a", ({"id": "a"},)])
    def test_not_a_list(self, value):
        with pytest.raises(se.SessionEnvironmentsError):
            se.validate_profiles(value)


class TestSection:
    def test_both_keys_required_and_no_others(self):
        good = {"coder_url": "", "profiles": []}
        assert se.validate_session_environments(good) == good
        with pytest.raises(se.SessionEnvironmentsError, match="missing"):
            se.validate_session_environments({"coder_url": ""})
        with pytest.raises(se.SessionEnvironmentsError, match="unknown"):
            se.validate_session_environments({**good, "enabled": True})
        with pytest.raises(se.SessionEnvironmentsError):
            se.validate_session_environments([])


# ── Loader ─────────────────────────────────────────────────────────────────


class TestLoader:
    def test_absent_section_defaults(self, config_file):
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        assert cfg.session_environments.coder_url == ""
        assert cfg.session_environments.profiles == []
        assert cfg.to_dict()["session_environments"] == {
            "coder_url": "",
            "profiles": [],
            "fargate_profiles": [],
            "default_profile": "",
            "stop_on_shutdown": True,
            "archived_retention_days": 7,
        }

    def test_valid_section_round_trips(self, config_file):
        from kiro_crew.config.loader import KiroCrewConfig

        section = {
            "coder_url": "https://c.example.com",
            "profiles": [_profile("a")],
            "fargate_profiles": [],
            "default_profile": "",
            "stop_on_shutdown": True,
            "archived_retention_days": 2,
        }
        config_file.write_text(
            json.dumps({**_BASE_CONFIG, "session_environments": section}), encoding="utf-8"
        )
        cfg = KiroCrewConfig.load()
        assert cfg.to_dict()["session_environments"] == section
        cfg.save()
        stored = _stored(config_file)
        assert stored["session_environments"] == section
        assert stored["an_edition_section"] == {"keep": "me"}
        assert KiroCrewConfig.load().to_dict()["session_environments"] == section

    def test_lenient_load_keeps_valid_parts(self, config_file):
        from kiro_crew.config.loader import KiroCrewConfig

        section = {
            "coder_url": "https://user:pw@c.example.com",
            "profiles": [
                _profile("good"),
                _profile("bad", workdir="relative"),
                _profile("good"),
                "not-an-object",
                _profile("also-good"),
            ],
        }
        config_file.write_text(
            json.dumps({**_BASE_CONFIG, "session_environments": section}), encoding="utf-8"
        )
        env = KiroCrewConfig.load().session_environments
        assert env.coder_url == ""
        assert [p.id for p in env.profiles] == ["good", "also-good"]

    def test_over_cap_truncated_on_load(self):
        coder_url, profiles = se.load_session_environments(
            {"profiles": [_profile(f"p{i}") for i in range(se.MAX_PROFILES + 5)]}
        )
        assert coder_url == ""
        assert len(profiles) == se.MAX_PROFILES

    @pytest.mark.parametrize("raw", [None, "x", [], {"profiles": "x"}])
    def test_malformed_section_defaults(self, raw):
        assert se.load_session_environments(raw) == ("", [])

    def test_is_a_known_section(self):
        from kiro_crew.config.loader import _KNOWN_CONFIG_SECTIONS

        assert "session_environments" in _KNOWN_CONFIG_SECTIONS


# ── Dashboard API ──────────────────────────────────────────────────────────


class TestApi:
    @pytest.mark.asyncio
    async def test_get_exposes_safe_defaults(self, config_file):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/config/kirocrew")
            assert resp.status == 200
            assert (await resp.json())["session_environments"] == {
                "coder_url": "",
                "profiles": [],
                "fargate_profiles": [],
                "default_profile": "",
                "stop_on_shutdown": True,
                "archived_retention_days": 7,
            }

    @pytest.mark.asyncio
    async def test_whole_section_saves_and_preserves_unrelated_config(self, config_file):
        section = {
            "coder_url": " https://c.example.com ",
            "profiles": [_profile("a"), _profile("b", name="  B  ")],
        }
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, "session_environments", section)
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        expected = {
            "coder_url": "https://c.example.com",
            "profiles": [_profile("a"), _profile("b", name="B")],
        }
        # The read shows the retention default; the file holds only what was sent.
        assert body["session_environments"] == {
            **expected,
            "fargate_profiles": [],
            "default_profile": "",
            "stop_on_shutdown": True,
            "archived_retention_days": 7,
        }
        stored = _stored(config_file)
        assert stored["session_environments"] == expected
        assert stored["session"]["timeout_secs"] == 1234
        assert stored["an_edition_section"] == {"keep": "me"}
        assert stored["default_agent"] == "kirocrew"

    @pytest.mark.asyncio
    async def test_leaf_paths_update_one_half(self, config_file):
        async with TestClient(TestServer(_app())) as client:
            assert (
                await _patch(client, "session_environments.profiles", [_profile()])
            ).status == 200
            resp = await _patch(client, "session_environments.coder_url", "https://c.example.com")
            assert resp.status == 200
            got = (await (await client.get("/api/config/kirocrew")).json())["session_environments"]
        assert got == {
            "coder_url": "https://c.example.com",
            "profiles": [_profile()],
            "fargate_profiles": [],
            "default_profile": "",
            "stop_on_shutdown": True,
            "archived_retention_days": 7,
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path", "value"),
        [
            ("session_environments", {"coder_url": "https://c.example.com"}),
            ("session_environments", {"coder_url": "", "profiles": [], "token": "x"}),
            ("session_environments", "https://c.example.com"),
            ("session_environments.coder_url", "https://c.example.com?token=x"),
            ("session_environments.coder_url", "file:///etc/passwd"),
            ("session_environments.profiles", [_profile("a"), _profile("a")]),
            ("session_environments.profiles", [_profile(workdir="repo")]),
            ("session_environments.profiles", [{**_profile(), "password": "x"}]),
            ("session_environments.profiles", [_profile(f"p{i}") for i in range(21)]),
            ("session_environments.enabled", True),
        ],
    )
    async def test_invalid_values_are_refused_and_nothing_is_written(
        self, config_file, path, value
    ):
        before = config_file.read_bytes()
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, path, value)
            assert resp.status == 400
            assert (await resp.json())["error"]
        assert config_file.read_bytes() == before

    @pytest.mark.asyncio
    async def test_get_drops_unmodelled_keys_but_save_keeps_them(self, config_file):
        """A key a newer build wrote inside the section survives the write but is
        not shown to the browser (the schema cannot vouch that it is not secret)."""
        config_file.write_text(
            json.dumps(
                {
                    **_BASE_CONFIG,
                    "session_environments": {"coder_url": "", "profiles": [], "future": "x"},
                }
            ),
            encoding="utf-8",
        )
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, "session_environments.coder_url", "https://c.example.com")
            assert resp.status == 200
            got = (await (await client.get("/api/config/kirocrew")).json())["session_environments"]
        assert got == {
            "coder_url": "https://c.example.com",
            "profiles": [],
            "fargate_profiles": [],
            "default_profile": "",
            "stop_on_shutdown": True,
            "archived_retention_days": 7,
        }
        assert _stored(config_file)["session_environments"]["future"] == "x"

    @pytest.mark.asyncio
    async def test_an_older_client_that_omits_retention_keeps_the_saved_value(self, config_file):
        async with TestClient(TestServer(_app())) as client:
            assert (
                await _patch(client, "session_environments.archived_retention_days", 2)
            ).status == 200
            # A dashboard from before the setting saves the section without it.
            resp = await _patch(
                client,
                "session_environments",
                {"coder_url": "https://c.example.com", "profiles": []},
            )
            assert resp.status == 200, await resp.text()
            got = (await (await client.get("/api/config/kirocrew")).json())["session_environments"]
        assert got["archived_retention_days"] == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [8, -1, True, "3"])
    async def test_retention_outside_zero_to_seven_is_refused(self, config_file, value):
        before = config_file.read_bytes()
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, "session_environments.archived_retention_days", value)
            assert resp.status == 400
        assert config_file.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["coder_leaf", "fargate_leaf", "whole", "whole_omitted"])
async def test_provider_id_collision_is_refused_after_locked_merge(config_file, form):
    from test_fargate_session import PROFILE

    coder = _profile(PROFILE["id"])
    section = {"coder_url": "https://coder.example.com", "profiles": [], "fargate_profiles": []}
    if form == "fargate_leaf":
        section["profiles"] = [coder]
        key, value = "session_environments.fargate_profiles", [PROFILE]
    else:
        section["fargate_profiles"] = [PROFILE]
        if form == "coder_leaf":
            key, value = "session_environments.profiles", [coder]
        else:
            key = "session_environments"
            value = {"coder_url": "https://coder.example.com", "profiles": [coder]}
            if form == "whole":
                value["fargate_profiles"] = [PROFILE]
    config_file.write_text(
        json.dumps({**_BASE_CONFIG, "session_environments": section}), encoding="utf-8"
    )
    before = config_file.read_bytes()
    async with TestClient(TestServer(_app())) as client:
        response = await _patch(client, key, value)
        assert response.status == 400, await response.text()
        assert "both a" in (await response.json())["error"]
    assert config_file.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["profiles", "fargate_profiles"])
@pytest.mark.parametrize("malformed", [None, 7, "invalid", [None, "invalid", {}, {"id": []}]])
@pytest.mark.parametrize("whole", [False, True])
async def test_unrelated_save_tolerates_malformed_stored_profiles(
    config_file, key, malformed, whole
):
    section = {"coder_url": "", "profiles": [], key: malformed}
    config_file.write_text(
        json.dumps({**_BASE_CONFIG, "session_environments": section}), encoding="utf-8"
    )
    path, value = "session_environments.stop_on_shutdown", False
    if whole:
        path, value = "session_environments", {"coder_url": "", "profiles": []}
    async with TestClient(TestServer(_app())) as client:
        response = await _patch(client, path, value)
        assert response.status == 200, await response.text()
    saved = _stored(config_file)
    assert saved["an_edition_section"] == _BASE_CONFIG["an_edition_section"]
    if not whole or key == "fargate_profiles":
        assert saved["session_environments"][key] == malformed


def test_cross_provider_check_ignores_duplicates_within_one_stored_list():
    section = {"profiles": [_profile(), _profile()], "fargate_profiles": []}
    assert se.validate_unique_profile_ids(section) == {"dev": "Coder"}
    section["fargate_profiles"] = [{"id": "dev"}]
    with pytest.raises(se.SessionEnvironmentsError, match="both a Coder and a Fargate"):
        se.validate_unique_profile_ids(section)
