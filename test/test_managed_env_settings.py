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


def test_the_config_reader_keeps_only_text():
    def read(raw):
        return _build_instances_config(
            None, {"managed_env_endpoint": raw}, None
        ).managed_env_endpoint

    assert read(ENDPOINT) == ENDPOINT
    assert read(7) == ""
    assert read(None) == ""
