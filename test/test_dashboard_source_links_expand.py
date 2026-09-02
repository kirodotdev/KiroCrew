"""Tests for the unbudgeted source-links read behind the sidebar's "+N" chip.

The slots payload serializes at most ``_SERIALIZED_SOURCE_LINKS_PER_SLOT`` links
per kind so a broadcast carrying dozens of rows stays small. That budget is
exactly why the sidebar's overflow chip has nothing on the client to expand into,
and why ``GET /api/chat/slots/{slot}/source-links`` exists: it is the only place
the links behind "+N" can come from.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.chat_handlers import api_chat_slot_source_links
from kiro_crew.dashboard.state import _ChatSlot

PRS = [f"https://github.com/acme/widgets/pull/{n}" for n in (11, 12, 13, 14)]
ISSUES = [f"https://github.com/acme/widgets/issues/{n}" for n in (21, 22, 23, 24)]
# The extractor scans messages newest-first, so the links a session mentioned
# last are the ones it reports first. Both fixtures land in ONE message, so the
# order is that message's urls reversed.
RECENT_PRS = list(reversed(PRS))
RECENT_ISSUES = list(reversed(ISSUES))


def _slot() -> _ChatSlot:
    slot = _ChatSlot("s1")
    slot.append("assistant", "\n".join([*PRS, *ISSUES]), ts="t1")
    return slot


class TestSourceLinksPayload:
    def test_returns_every_link_the_chip_budget_hides(self):
        slot = _slot()
        # Precondition: the slots payload really is capped, three per kind.
        budgeted = slot.to_dict()["source_links"]
        assert len(budgeted) == 6
        assert slot.to_dict()["source_links_total"] == 8

        payload = slot.source_links_payload()
        assert payload["total"] == 8
        assert [link["url"] for link in payload["links"]] == [*RECENT_PRS, *RECENT_ISSUES]

    def test_groups_changes_before_issues_so_visible_chips_keep_their_place(self):
        """The budgeted slice must be a per-group prefix of the expanded list.

        Expanding is a reveal, not a reshuffle: every chip already on screen has
        to stay where it was, with the newly revealed ones appended inside their
        own group. A flat discovery-order response would interleave them and move
        chips the user was already looking at.
        """
        slot = _slot()
        expanded = [link["url"] for link in slot.source_links_payload()["links"]]
        changes = [url for url in expanded if "/pull/" in url]
        issues = [url for url in expanded if "/issues/" in url]
        assert expanded == changes + issues

        budgeted = [link["url"] for link in slot.to_dict()["source_links"]]
        assert budgeted == changes[:3] + issues[:3]

    def test_check_status_is_withheld_unless_the_caller_asked(self):
        slot = _slot()
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.get_cached_check_status",
            return_value={"ci": "passed", "state": "OPEN"},
        ):
            plain = slot.source_links_payload()
            owner = slot.source_links_payload(include_check_status=True)

        assert all("ci" not in link for link in plain["links"])
        # Every pull request carries the cached status, including the ones the
        # budget hid -- an expanded chip is as decorated as a visible one.
        changes = [link for link in owner["links"] if link["kind"] == "change"]
        assert len(changes) == 4
        assert all(link["ci"] == "passed" for link in changes)

    def test_issue_links_never_inherit_pull_request_status(self):
        """The chip-status cache is pull-request-only, so an issue has nothing
        truthful to colour -- a borrowed glyph would assert state never fetched."""
        slot = _slot()
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.get_cached_check_status",
            return_value={"ci": "failed", "state": "OPEN"},
        ):
            owner = slot.source_links_payload(include_check_status=True)

        issues = [link for link in owner["links"] if link["kind"] == "issue"]
        assert len(issues) == 4
        assert all("ci" not in link and "state" not in link for link in issues)


def _request(slot_key: str, slots: dict, *, app: str = ""):
    request = MagicMock(spec=web.Request)
    request.method = "GET"
    request.match_info = {"slot": slot_key}
    request.get = lambda key, default=None: app if key == "app" else default
    state = MagicMock()
    state._slots = slots
    request.app = {"state": state}
    return request


async def _get(slot_key: str, slots: dict, *, owner: bool = True, app: str = "") -> web.Response:
    with patch(
        "kiro_crew.dashboard.handlers.source_providers.ensure_gitlab_hosts_loaded",
        return_value=None,
    ), patch(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        return_value=owner,
    ), patch("kiro_crew.dashboard.chat_handlers.sel"):
        return await api_chat_slot_source_links(_request(slot_key, slots, app=app))


class TestSourceLinksEndpoint:
    @pytest.mark.asyncio
    async def test_serves_the_unbudgeted_list(self):
        resp = await _get("s1", {"s1": _slot()}, owner=False)
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["total"] == 8
        assert [link["url"] for link in body["links"]] == [*RECENT_PRS, *RECENT_ISSUES]

    @pytest.mark.asyncio
    async def test_unknown_slot_is_404_not_an_empty_strip(self):
        """An empty 200 would collapse the chip strip to nothing, which reads as
        "this session has no links" rather than "this session is gone"."""
        resp = await _get("nope", {"s1": _slot()})
        assert resp.status == 404
        assert json.loads(resp.text) == {"error": "not found", "code": "slot_not_found"}

    @pytest.mark.asyncio
    async def test_check_status_follows_the_owner_gate(self):
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.get_cached_check_status",
            return_value={"ci": "passed", "state": "OPEN"},
        ):
            non_owner = json.loads((await _get("s1", {"s1": _slot()}, owner=False)).text)
            owner = json.loads((await _get("s1", {"s1": _slot()}, owner=True)).text)

        assert all("ci" not in link for link in non_owner["links"])
        assert [link["ci"] for link in owner["links"] if link["kind"] == "change"] == ["passed"] * 4

    @pytest.mark.asyncio
    async def test_a_cold_gitlab_allowlist_failure_still_answers(self):
        """The warm-up is best-effort: a self-hosted MR may be missing for one
        round, but the expand must not fail outright over it."""
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.ensure_gitlab_hosts_loaded",
            side_effect=RuntimeError("cold"),
        ), patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=False,
        ):
            resp = await api_chat_slot_source_links(_request("s1", {"s1": _slot()}))

        assert resp.status == 200
        assert json.loads(resp.text)["total"] == 8


class TestAppTokenIsolation:
    """App Kit §5.2 deny-by-default. Without this an app token scoped to
    /api/chat/slots/* could name any slot the list endpoint reveals and read
    every pull request and issue URL that session ever mentioned."""

    @pytest.mark.asyncio
    async def test_app_token_cannot_read_a_dashboard_owned_slot(self):
        slot = _slot()
        slot._app = ""  # created by the dashboard, owned by no app
        resp = await _get("s1", {"s1": slot}, app="design_critique")
        assert resp.status == 404
        assert json.loads(resp.text) == {"error": "not found", "code": "slot_not_found"}

    @pytest.mark.asyncio
    async def test_app_token_cannot_read_another_apps_slot(self):
        slot = _slot()
        slot._app = "spec_builder"
        resp = await _get("s1", {"s1": slot}, app="design_critique")
        assert resp.status == 404
        # The SAME 404 body as a missing slot, so the response cannot be used to
        # probe which foreign slots exist.
        assert json.loads(resp.text) == {"error": "not found", "code": "slot_not_found"}

    @pytest.mark.asyncio
    async def test_an_app_still_reads_its_own_slot(self):
        slot = _slot()
        slot._app = "design_critique"
        resp = await _get("s1", {"s1": slot}, app="design_critique")
        assert resp.status == 200
        assert json.loads(resp.text)["total"] == 8

    @pytest.mark.asyncio
    async def test_the_allowed_app_read_is_audited_too(self):
        """An audit trail that records only refusals cannot answer which app
        actually read a slot's links -- the ALLOW is a permission decision."""
        slot = _slot()
        slot._app = "design_critique"
        with patch("kiro_crew.dashboard.chat_handlers.sel") as sel, patch(
            "kiro_crew.dashboard.handlers.source_providers.ensure_gitlab_hosts_loaded",
            return_value=None,
        ), patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=False,
        ):
            resp = await api_chat_slot_source_links(
                _request("s1", {"s1": slot}, app="design_critique")
            )

        assert resp.status == 200
        outcomes = [
            call.kwargs
            for call in sel().log_api_access.call_args_list
            if call.kwargs.get("operation") == "chat_source_links"
        ]
        assert [entry["outcome"] for entry in outcomes] == ["allowed"]
        assert outcomes[0]["caller"] == "design_critique"
        assert outcomes[0]["resources"] == "slot=s1"

    @pytest.mark.asyncio
    async def test_a_dashboard_read_is_not_audited(self):
        """The owner reads on every expand; logging those would bury the app
        events the trail exists for."""
        slot = _slot()
        slot._app = ""
        with patch("kiro_crew.dashboard.chat_handlers.sel") as sel, patch(
            "kiro_crew.dashboard.handlers.source_providers.ensure_gitlab_hosts_loaded",
            return_value=None,
        ), patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=True,
        ):
            resp = await api_chat_slot_source_links(_request("s1", {"s1": slot}, app=""))

        assert resp.status == 200
        assert sel().log_api_access.call_args_list == []

    @pytest.mark.asyncio
    async def test_the_dashboard_still_reads_an_app_owned_slot(self):
        """An empty request app is the dashboard user, who owns everything."""
        slot = _slot()
        slot._app = "spec_builder"
        resp = await _get("s1", {"s1": slot}, app="")
        assert resp.status == 200
        assert json.loads(resp.text)["total"] == 8
