"""A relay teammate's read of a Bot's package page leaves the owner's live page alone.

The armed page is shared by every owner tab: it carries one version counter and pushes
in the language of the reader who armed it. A teammate's read is a one-shot page of its
own, so it can neither restart the owner's counter nor hand the owner frames in the
teammate's language. Who counts as a teammate is decided by the relay gate, covered in
``test_members_roster_relay_teammate.py``; here it is pinned at the route's own seam.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from member_dashboard_package_route_helpers import wired_fixture  # noqa: F401
from member_dashboard_package_route_helpers import (
    PACKAGE_SLUG,
    PAGE,
    PATH,
    SLUG,
    _call,
    _publish,
    _request,
)

from kiro_crew.dashboard.handlers import member_dashboard as md

KEY = (SLUG, PACKAGE_SLUG)


@pytest.mark.asyncio
async def test_a_teammates_read_leaves_the_owners_live_page_armed_in_the_owners_language(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, owner_body = await _call(
        md.api_member_dashboard, _request(wired.app, "GET", PATH + "&locale=en")
    )
    assert status == 200
    owners_page = wired.app["member_dashboard_live_pages"][KEY]

    monkeypatch.setattr(md, "_teammate_reader", AsyncMock(return_value=True))
    status, body = await _call(
        md.api_member_dashboard, _request(wired.app, "GET", PATH + "&locale=fr")
    )
    assert status == 200
    # The teammate gets the page, in their own language.
    assert PAGE in body["rendered_html"]
    assert body["locale"] == "fr"
    # And the owner's armed page is the same one, still subscribed.
    assert wired.app["member_dashboard_live_pages"][KEY] is owners_page
    assert owners_page.folds() == frozenset({"work"})

    wired.frames.clear()
    _publish("work", {"conductor": {"goal": "ship the host"}}, 1)
    # One frame, from the owner's page only: the teammate's page was disarmed after its read.
    assert len(wired.frames) == 1, wired.frames
    read = wired.frames[0][1]["read"]
    assert read["locale"] == owner_body["locale"] == "en"
    assert read["fields"]["goal"] == "ship the host"


@pytest.mark.asyncio
async def test_a_teammates_read_arms_no_page_when_the_owner_has_none(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(md, "_teammate_reader", AsyncMock(return_value=True))
    status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert status == 200
    assert PAGE in body["rendered_html"]
    assert not wired.app.get("member_dashboard_live_pages"), "a teammate's read armed a page"
    wired.frames.clear()
    _publish("work", {"conductor": {"goal": "nobody is armed"}}, 1)
    assert wired.frames == []


@pytest.mark.asyncio
async def test_a_teammates_read_that_cannot_be_framed_keeps_the_owners_page(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

    status, _ = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert status == 200
    owners_page = wired.app["member_dashboard_live_pages"][KEY]

    def _refuse(*_args: Any, **_kwargs: Any) -> str:
        raise ValueError("the page cannot be framed")

    monkeypatch.setattr(md, "_teammate_reader", AsyncMock(return_value=True))
    monkeypatch.setattr(mdf, "render_package_page", _refuse)
    status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert (status, body["code"]) == (409, "dashboard_refused")
    assert wired.app["member_dashboard_live_pages"][KEY] is owners_page
    assert owners_page.folds() == frozenset({"work"})


@pytest.mark.asyncio
async def test_a_teammates_read_whose_arm_raises_leaves_nothing_subscribed(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

    armed: list[Any] = []
    real_arm = mdf.LivePage.arm

    def _arm_then_fail(self: Any) -> dict[str, Any]:
        armed.append(self)
        real_arm(self)
        raise RuntimeError("the read failed after subscribing")

    monkeypatch.setattr(md, "_teammate_reader", AsyncMock(return_value=True))
    monkeypatch.setattr(mdf.LivePage, "arm", _arm_then_fail)
    with pytest.raises(RuntimeError):
        await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert len(armed) == 1
    assert armed[0].folds() == frozenset(), "the teammate's page is still subscribed"
    assert not wired.app.get("member_dashboard_live_pages")
