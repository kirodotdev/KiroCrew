"""What the company (IAM Identity Center) sign-in form starts filled in with.

The order is: the saved ``agent.kas_sso_start_url`` / ``agent.kas_sso_region``
setting, then the edition's ``DashboardContributor.company_sign_in_defaults()``,
then nothing. The public Default contributor supplies nothing, so a stock build
answers an empty pair and the form looks the same as before.
"""

from __future__ import annotations

import dataclasses
from typing import cast

import pytest
from aiohttp import web

from kiro_crew.auth.company_sign_in import company_sign_in_default
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.kas_login import api_kas_login_status
from kiro_crew.platform import PlatformCompositionError, build_default_context, set_context
from kiro_crew.platform.defaults import DefaultDashboardContributor

_EDITION_URL = "https://example-org.awsapps.com/start"
_SAVED_URL = "https://saved-org.awsapps.com/start"
_EMPTY = {"start_url": "", "region": ""}


class _Edition(DefaultDashboardContributor):
    def __init__(self, answer) -> None:
        self._answer = answer

    def company_sign_in_defaults(self):
        return self._answer


class _Raising(DefaultDashboardContributor):
    def company_sign_in_defaults(self):
        raise RuntimeError("boom")


class _Legacy:
    """A contributor that predates the seam: it has no such method."""


def _with(contributor, cfg: KiroCrewConfig) -> None:
    base = build_default_context(cfg)
    set_context(dataclasses.replace(base, dashboard=contributor))


def _cfg(url: str = "", region: str = "") -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agent.kas_sso_start_url = url
    cfg.agent.kas_sso_region = region
    return cfg


def test_the_public_build_starts_empty() -> None:
    cfg = _cfg()
    _with(DefaultDashboardContributor(), cfg)
    assert DefaultDashboardContributor().company_sign_in_defaults() == {}
    assert company_sign_in_default(cfg.agent) == _EMPTY


def test_the_edition_default_fills_an_unset_setting() -> None:
    cfg = _cfg()
    _with(_Edition({"start_url": _EDITION_URL, "region": "us-east-1"}), cfg)
    assert company_sign_in_default(cfg.agent) == {
        "start_url": _EDITION_URL,
        "region": "us-east-1",
    }


def test_a_saved_setting_wins_over_the_edition_default() -> None:
    # The saved pair travels together: the edition's region is not mixed in.
    cfg = _cfg(_SAVED_URL)
    _with(_Edition({"start_url": _EDITION_URL, "region": "us-east-1"}), cfg)
    assert company_sign_in_default(cfg.agent) == {"start_url": _SAVED_URL, "region": ""}


def test_a_saved_url_is_put_in_canonical_form() -> None:
    cfg = _cfg("saved-org.awsapps.com", "eu-west-1")
    _with(DefaultDashboardContributor(), cfg)
    assert company_sign_in_default(cfg.agent) == {"start_url": _SAVED_URL, "region": "eu-west-1"}


@pytest.mark.parametrize(
    "answer",
    [
        None,
        "https://example-org.awsapps.com/start",
        {"start_url": 7},
        {"start_url": "http://example-org.awsapps.com/start"},
        {"start_url": "https://user@example-org.awsapps.com/start"},
        {"start_url": "https://example-org.awsapps.com/start; rm -x"},
        {"region": "us-east-1"},
    ],
)
def test_an_unusable_edition_answer_fills_nothing(answer) -> None:
    cfg = _cfg()
    _with(_Edition(answer), cfg)
    assert company_sign_in_default(cfg.agent) == _EMPTY


def test_a_bad_region_is_dropped_but_the_url_kept() -> None:
    # The region goes into a hostname, so only the AWS region grammar is passed on.
    cfg = _cfg()
    _with(_Edition({"start_url": _EDITION_URL, "region": "evil.com/"}), cfg)
    assert company_sign_in_default(cfg.agent) == {"start_url": _EDITION_URL, "region": ""}


def test_an_unusable_saved_url_falls_back_to_the_edition() -> None:
    cfg = _cfg("http://saved-org.awsapps.com/start")
    _with(_Edition({"start_url": _EDITION_URL}), cfg)
    assert company_sign_in_default(cfg.agent) == {"start_url": _EDITION_URL, "region": ""}


@pytest.mark.parametrize("contributor", [_Raising(), _Legacy()])
def test_a_broken_or_older_contributor_fills_nothing(contributor) -> None:
    cfg = _cfg()
    _with(contributor, cfg)
    assert company_sign_in_default(cfg.agent) == _EMPTY


def test_a_composition_failure_is_not_swallowed() -> None:
    class _Uncomposed(DefaultDashboardContributor):
        def company_sign_in_defaults(self):
            raise PlatformCompositionError("companion missing")

    cfg = _cfg()
    _with(_Uncomposed(), cfg)
    with pytest.raises(PlatformCompositionError):
        company_sign_in_default(cfg.agent)


class _FakeState:
    owner_id = ""


class _FakeRequest:
    path = "/api/kas-login"

    def __init__(self, service) -> None:
        self.app = {"kas_login_service": service, "state": _FakeState()}
        # The dashboard owner (a local subject, empty app scope): the route is owner-gated.
        self._items = {"app": "", "user": "local-app"}

    def __contains__(self, key):
        return key in self._items

    def __getitem__(self, key):
        return self._items[key]

    def get(self, key, default=""):
        return self._items.get(key, default)


@pytest.mark.asyncio
async def test_the_status_route_carries_the_default(monkeypatch) -> None:
    import json

    from kiro_crew.auth.service import KasLoginService
    from kiro_crew.dashboard.handlers import kas_login

    class _Stub(KasLoginService):
        def __init__(self) -> None:
            pass

        async def status(self):
            return {"authenticated": False, "transport": "device"}

    cfg = _cfg()
    _with(_Edition({"start_url": _EDITION_URL, "region": "us-east-1"}), cfg)
    monkeypatch.setattr(kas_login, "_load_agent_config", lambda: cfg.agent)
    resp = await api_kas_login_status(cast(web.Request, _FakeRequest(_Stub())))
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    body = json.loads(resp.body)
    assert body["company_sign_in"] == {"start_url": _EDITION_URL, "region": "us-east-1"}
    assert body["transport"] == "device"


@pytest.mark.asyncio
@pytest.mark.parametrize("through_status", [False, True], ids=["default", "status"])
async def test_uncomposed_enterprise_sign_in_starts_empty(monkeypatch, through_status) -> None:
    import json

    from kiro_crew.auth.service import KasLoginService
    from kiro_crew.dashboard.handlers import kas_login
    from kiro_crew.platform import current_context, reset_context
    from kiro_crew.platform.context import installed_context

    class _Stub(KasLoginService):
        def __init__(self) -> None:
            pass

        async def status(self):
            return {"authenticated": False, "transport": "device"}

    cfg = _cfg()
    monkeypatch.setenv("KIROCREW_PROFILE", "enterprise")
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr("kiro_crew.platform.discovery.plugin_entry_points", lambda: [])
    reset_context()
    with pytest.raises(PlatformCompositionError, match="no installed context"):
        current_context()

    if through_status:
        monkeypatch.setattr(kas_login, "_load_agent_config", lambda: cfg.agent)
        resp = await api_kas_login_status(cast(web.Request, _FakeRequest(_Stub())))
        assert resp.status == 200
        assert isinstance(resp.body, bytes)
        assert json.loads(resp.body)["company_sign_in"] == _EMPTY
    else:
        assert company_sign_in_default(cfg.agent) == _EMPTY
    assert installed_context() is None
