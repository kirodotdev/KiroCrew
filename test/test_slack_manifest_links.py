"""The Slack setup wizard's links: built only from ids Slack issues."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import slack_manifest

_BAD_APP_IDS = ["", "a0123abcd", "A1/../x", "A0123ABCD?x=1", "T0123ABCD", "A12"]


@pytest.mark.parametrize("app_id", _BAD_APP_IDS)
def test_a_bad_app_id_gets_no_link(app_id: str) -> None:
    assert slack_manifest.app_basic_info_url(app_id) is None
    assert slack_manifest.app_dm_url(app_id, "T0123ABCD") is None


def test_basic_info_url_has_the_app_manifest_shape() -> None:
    assert (
        slack_manifest.app_basic_info_url("A0123ABCD")
        == "https://api.slack.com/apps/A0123ABCD/general"
    )


def test_dm_url_carries_the_workspace() -> None:
    assert (
        slack_manifest.app_dm_url("A0123ABCD", "T0456EFGH")
        == "https://slack.com/app_redirect?app=A0123ABCD&team=T0456EFGH"
    )


@pytest.mark.parametrize("team_id", ["", "E0123ABCD", "t0123abcd", "T01&x=y"])
def test_dm_url_without_a_workspace_id_carries_only_the_app(team_id: str) -> None:
    """An org-level install's team is an ``E…`` org id: the link names the app only."""
    assert slack_manifest.app_dm_url("A0123ABCD", team_id) == (
        "https://slack.com/app_redirect?app=A0123ABCD"
    )


@pytest.mark.parametrize(
    "raw,served",
    [
        ("https://wiki.example.com/slack", "https://wiki.example.com/slack"),
        ("http://wiki.example.com/slack", None),
        ("javascript:alert(1)", None),
        ("", None),
    ],
)
def test_the_org_guide_link_is_served_only_as_https(
    raw: str, served: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.config import loader
    from kiro_crew.dashboard.messaging_api.slack_workspace import api_slack_manifest

    cfg = loader.KiroCrewConfig()
    cfg.slack.setup_guide_url = raw
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))
    resp = asyncio.run(api_slack_manifest(make_mocked_request("GET", "/api/slack/manifest")))
    assert json.loads(resp.text)["org_guide_url"] == served
