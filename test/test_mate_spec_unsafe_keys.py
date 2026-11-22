"""A crewmate file is written only for a key that is one path component, and migration skips others.

``write_mate_spec`` joins the key into ``mates/agents/<key>.json`` and creates missing
folders, so a key such as ``../x`` or ``a/b`` would write outside that folder. The boot
migration reads the same keys from a stored roster and must not hand them on. A
``_safe_dir_name`` result is always a normal child name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.config import paths as paths_mod
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig


@pytest.fixture(autouse=True)
def mates(tmp_path, _floor_monkeypatch):
    kiro = tmp_path / "kiro"
    (kiro / "agents").mkdir(parents=True)
    _floor_monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    _floor_monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: kiro / "agents")
    folder = kiro / "mates" / "agents"
    folder.mkdir(parents=True)
    return folder


@pytest.mark.parametrize("key", ["../x", "a/b", "..", ".", "a\\b", "", "a\nb", "a\x00b"])
def test_write_mate_spec_refuses_a_key_that_is_not_one_path_component(mates, key):
    from kiro_crew.mate_config import MateConfigError, write_mate_spec

    before = sorted(p.name for p in mates.parent.parent.rglob("*"))
    with pytest.raises(MateConfigError, match="single path component"):
        write_mate_spec(key, {"name": "x", "prompt": "p"})
    assert sorted(p.name for p in mates.parent.parent.rglob("*")) == before
    assert not (mates.parent / "x.json").exists()


@pytest.mark.parametrize("key", ["my.pkg", "my crew", "小明"])
def test_write_mate_spec_still_writes_an_odd_but_single_component_key(mates, key):
    from kiro_crew.mate_config import write_mate_spec

    write_mate_spec(key, {"name": key, "prompt": "p"})
    assert json.loads((mates / f"{key}.json").read_text("utf-8"))["prompt"] == "p"


def _cfg(**agents: KiroCrewAgentConfig) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agents = dict(agents)
    return cfg


@pytest.mark.parametrize("name", ["../x", "a/b"])
def test_migration_candidates_skip_a_key_that_is_not_one_path_component(name, caplog):
    from kiro_crew.mate_config_migration import _candidates

    cfg = _cfg(**{name: KiroCrewAgentConfig(member_id="m1", kiro_agent="a")})
    with caplog.at_level("WARNING"):
        assert _candidates(cfg) == {}
    assert [r for r in caplog.records if "single path component" in r.getMessage()]
    assert "m1" not in caplog.text


def test_migration_candidates_skip_a_member_id_that_is_not_one_path_component():
    from kiro_crew.mate_config_migration import _candidates

    cfg = _cfg(ok=KiroCrewAgentConfig(member_id="../x", kiro_agent="a"))
    assert _candidates(cfg) == {}


def test_migration_candidates_skip_an_unsafe_key_that_is_being_minted():
    from kiro_crew.mate_config_migration import _candidates

    cfg = _cfg(**{"../x": KiroCrewAgentConfig(member_id="", kiro_agent="a")})
    assert _candidates(cfg, minting={"../x"}) == {}


def test_migration_candidates_keep_an_odd_but_single_component_key(mates):
    from kiro_crew.mate_config_migration import _candidates

    cfg = _cfg(**{"my.pkg": KiroCrewAgentConfig(member_id="m.1", kiro_agent="a")})
    assert _candidates(cfg) == {"my.pkg": "a"}


@pytest.mark.parametrize("key", [".", ".."])
def test_a_dot_key_is_not_a_dot_directory_name(key):
    from kiro_crew.config.paths import _safe_dir_name

    name = _safe_dir_name(key)
    assert name not in (".", "..")
    assert Path(name).parent == Path(".") and "/" not in name


def test_safe_dir_name_still_replaces_separators_colons_and_spaces():
    from kiro_crew.config.paths import _safe_dir_name

    assert _safe_dir_name("a/b\\c:d e") == "a_b_c_d_e"
