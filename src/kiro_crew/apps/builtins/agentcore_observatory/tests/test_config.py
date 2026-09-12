"""Config store: validation on both read and write, and corruption tolerance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.agentcore_observatory.backend import config as cfg_mod
from kiro_crew.apps.builtins.agentcore_observatory.backend.config import ObservatoryConfig


def test_app_name_is_kebab_case() -> None:
    """The manifest name the data dir is keyed on."""
    assert cfg_mod.APP_NAME == "agentcore-observatory"


def test_roundtrip(tmp_path: Path) -> None:
    ObservatoryConfig(profile="my-prof", region="us-east-2").save(tmp_path)
    loaded = ObservatoryConfig.load(tmp_path)
    assert (loaded.profile, loaded.region) == ("my-prof", "us-east-2")
    assert loaded.configured is True


def test_stores_no_credential_material(tmp_path: Path) -> None:
    """The names-only invariant, asserted on the bytes actually written."""
    ObservatoryConfig(profile="my-prof", region="us-east-2").save(tmp_path)
    written = json.loads(cfg_mod.config_path(tmp_path).read_text(encoding="utf-8"))
    assert set(written) == {"profile", "region"}


def test_missing_file_is_unconfigured(tmp_path: Path) -> None:
    loaded = ObservatoryConfig.load(tmp_path)
    assert (loaded.profile, loaded.region) == ("", "")
    assert loaded.configured is False


def test_region_has_no_default(tmp_path: Path) -> None:
    """A defaulted region would render a misconfiguration as an empty account."""
    assert ObservatoryConfig().region == ""
    assert ObservatoryConfig.load(tmp_path).region == ""


def test_empty_profile_is_configured_when_region_set() -> None:
    """An empty profile is legitimate — the CLI resolves its own default."""
    assert ObservatoryConfig(profile="", region="eu-central-1").configured is True


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        '"a bare string"',
        "[1, 2, 3]",
        "42",
        "null",
        "",
    ],
)
def test_corrupt_file_degrades_to_default(tmp_path: Path, raw: str) -> None:
    """Every non-object shape reads back as unconfigured, never raises."""
    cfg_mod.config_path(tmp_path).write_text(raw, encoding="utf-8")
    loaded = ObservatoryConfig.load(tmp_path)
    assert (loaded.profile, loaded.region) == ("", "")


@pytest.mark.parametrize(
    "bad_profile",
    ["-leading-dash", "has space", "has/slash", "has;semicolon", "--profile"],
)
def test_malformed_profile_is_dropped_on_read(tmp_path: Path, bad_profile: str) -> None:
    """A hand-edited hostile value must not reach an argv."""
    cfg_mod.config_path(tmp_path).write_text(
        json.dumps({"profile": bad_profile, "region": "us-east-1"}), encoding="utf-8"
    )
    loaded = ObservatoryConfig.load(tmp_path)
    assert loaded.profile == ""
    assert loaded.region == "us-east-1"


@pytest.mark.parametrize("bad_region", ["US-EAST-1", "useast1", "us_east_1", "../x", "us-east"])
def test_malformed_region_is_dropped_on_read(tmp_path: Path, bad_region: str) -> None:
    cfg_mod.config_path(tmp_path).write_text(
        json.dumps({"profile": "ok", "region": bad_region}), encoding="utf-8"
    )
    loaded = ObservatoryConfig.load(tmp_path)
    assert loaded.region == ""
    assert loaded.configured is False


def test_save_refuses_malformed_profile(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ObservatoryConfig(profile="has space", region="us-east-1").save(tmp_path)


def test_save_refuses_malformed_region(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ObservatoryConfig(profile="ok", region="nope").save(tmp_path)


def test_to_dict_exposes_configured_flag() -> None:
    assert ObservatoryConfig(profile="p", region="us-east-1").to_dict() == {
        "profile": "p",
        "region": "us-east-1",
        "configured": True,
    }
