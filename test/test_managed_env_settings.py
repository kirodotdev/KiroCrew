"""The lane's settings: read only with the preview on, nothing guessed."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.config.section_builders import _build_instances_config
from kiro_crew.managed_env import settings as s

ENDPOINT = "https://service.example.com"


def _cfg(on: bool, endpoint: object = ENDPOINT):
    return SimpleNamespace(
        dashboard=SimpleNamespace(feature_previews={"crew_client": on}),
        instances=SimpleNamespace(managed_env_endpoint=endpoint),
    )


def test_the_preview_off_reads_the_endpoint_empty():
    assert s.read_settings(_cfg(False)) == s.LaneSettings()


def test_the_preview_on_reads_the_endpoint():
    assert s.read_settings(_cfg(True)).service_endpoint() == ENDPOINT
    assert s.read_settings(_cfg(True, endpoint=3)) == s.LaneSettings()


@pytest.mark.parametrize(
    "endpoint, ok",
    [
        ("https://service.example.com/", True),
        ("http://127.0.0.1:8080", True),
        ("http://localhost:8080", True),
        ("http://service.example.com", False),
        ("ftp://service.example.com", False),
        ("https://", False),
        ("", False),
    ],
)
def test_the_endpoint_is_https_or_loopback_http(endpoint, ok):
    lane = s.LaneSettings(endpoint=endpoint)
    if ok:
        assert lane.service_endpoint() == endpoint.rstrip("/")
    else:
        with pytest.raises(s.SettingsMissing) as ei:
            lane.service_endpoint()
        assert ei.value.keys == ("instances.managed_env_endpoint",)
        assert "instances.managed_env_endpoint" in str(ei.value)


CELL = dict(
    kas_endpoint="https://runtime.example.com",
    kas_control_plane_endpoint="https://control.example.com",
    kas_region="ex-test-1",
    box_token_path="/var/example/token",
)


def test_the_cell_names_every_gap():
    assert s.LaneSettings(**CELL).cell().region == "ex-test-1"
    with pytest.raises(s.SettingsMissing) as ei:
        s.LaneSettings().cell()
    assert ei.value.keys == (
        "instances.managed_env_kas_endpoint",
        "instances.managed_env_kas_control_plane_endpoint",
        "instances.managed_env_kas_region",
        "instances.managed_env_box_token_path",
    )
    with pytest.raises(s.SettingsMissing) as ei:
        s.LaneSettings(**CELL, box_env_drop=("OK", "not ok")).cell()
    assert ei.value.keys == ("instances.managed_env_box_env_drop",)
    with pytest.raises(s.SettingsMissing):
        s.LaneSettings(ssh_region="nowhere").ssh_region_or_raise()


@pytest.mark.parametrize(
    "endpoint, control, region, bad",
    [
        ("https://a.example", "https://b.example", "ex-test-1", ()),
        ("http://a.example", "https://b.example", "ex-test-1", ("kas_endpoint",)),
        (
            "https://a.example?x=1",
            "https://u@b.example",
            "ex-test-1",
            ("kas_endpoint", "kas_control_plane_endpoint"),
        ),
        ("https://a.example", "https://b.example", "EX-TEST-1", ("kas_region",)),
        (None, 3, "", ("kas_endpoint", "kas_control_plane_endpoint", "kas_region")),
    ],
)
def test_the_cell_check(endpoint, control, region, bad):
    lane = s.LaneSettings(
        kas_endpoint=endpoint,
        kas_control_plane_endpoint=control,
        kas_region=region,
        box_token_path="/var/example/token",
    )
    if not bad:
        assert lane.cell().endpoint == endpoint
        return
    with pytest.raises(s.SettingsMissing) as ei:
        lane.cell()
    assert ei.value.keys == tuple(f"instances.managed_env_{k}" for k in bad)


def test_the_preview_on_reads_every_key():
    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(feature_previews={"crew_client": True}),
        instances=SimpleNamespace(
            managed_env_endpoint=ENDPOINT,
            managed_env_start_url="https://start.example.com/start",
            managed_env_ssh_region="ex-test-1",
            managed_env_box_env_drop=["A", 3],
            **{f"managed_env_{k}": v for k, v in CELL.items()},
        ),
    )
    got = s.read_settings(cfg)
    assert got.ssh_region_or_raise() == "ex-test-1" and got.box_env_drop == ("A",)
    assert got.is_lane_start_url("https://start.example.com/start/")
    assert not got.is_lane_start_url("https://other.example.com/start")


def test_the_config_reader_keeps_only_text():
    def read(raw):
        return _build_instances_config(
            None, {"managed_env_endpoint": raw}, None
        ).managed_env_endpoint

    assert read(ENDPOINT) == ENDPOINT
    assert read(7) == ""
    assert read(None) == ""
