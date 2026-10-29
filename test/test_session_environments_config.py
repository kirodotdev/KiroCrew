"""Persisted Session Environments settings: validator and loader.

One validator serves both: a hand-edited file keeps its valid profiles while
``PATCH`` refuses the same value and writes nothing (the API half is tested
with the dashboard handlers).
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.config import session_environments as se
from kiro_crew.config.loader import _KNOWN_CONFIG_SECTIONS, KiroCrewConfig

_BASE_CONFIG = {
    "agents": {"kirocrew": {"kiro_agent": "kirocrew"}},
    "default_agent": "kirocrew",
    "session": {"timeout_secs": 1234},
    "an_edition_section": {"keep": "me"},
}
#: What a read adds to a section that omits them.
_DEFAULTS = {"default_profile": "", "stop_on_shutdown": True}


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


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_BASE_CONFIG), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    return path


def _stored(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_section(path, section) -> None:
    path.write_text(json.dumps({**_BASE_CONFIG, "session_environments": section}), encoding="utf-8")


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


class TestLoader:
    def test_absent_section_defaults(self, config_file):
        cfg = KiroCrewConfig.load()
        assert cfg.session_environments.coder_url == ""
        assert cfg.session_environments.profiles == []
        assert cfg.to_dict()["session_environments"] == {
            "coder_url": "",
            "profiles": [],
            **_DEFAULTS,
        }

    def test_valid_section_round_trips(self, config_file):
        section = {
            "coder_url": "https://c.example.com",
            "profiles": [_profile("a")],
            "default_profile": "",
            "stop_on_shutdown": True,
        }
        _write_section(config_file, section)
        cfg = KiroCrewConfig.load()
        assert cfg.to_dict()["session_environments"] == section
        cfg.save()
        stored = _stored(config_file)
        assert stored["session_environments"] == section
        assert stored["an_edition_section"] == {"keep": "me"}
        assert KiroCrewConfig.load().to_dict()["session_environments"] == section

    def test_lenient_load_keeps_valid_parts(self, config_file):
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
        _write_section(config_file, section)
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
        assert "session_environments" in _KNOWN_CONFIG_SECTIONS
