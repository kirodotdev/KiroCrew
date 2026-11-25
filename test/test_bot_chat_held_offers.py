"""A reconcile reads every held offer the same way (``bot_chat_held_offers``).

A Bot chat that starts while its app's offer is held is handed no server for it.
If the reconcile then counted that gap as a change, it would reload the chat into
the same hold again and again. Each case below breaks with one change:

* a stop fence span open (a disable or uninstall under way): drop the
  ``fence_holds`` term from ``reconcile_view``'s ``held``;
* a running chat keeps its server while the fence holds, even when the offer's
  entry moved: return ``f`` instead of ``was[a]`` for a held alias;
* a trust withdrawal under way (the fence ``revoke_app_generation`` raises, no
  span): drop the ``not any(...)`` term from ``fence_holds``;
* a pending close for this Bot: drop the ``pending_close_withheld`` term;
* another Bot's pending close does not hold this one: drop the
  ``(slug, app) in pending_close`` restriction (return True for any fence);
* the last span's exit reconciles once: drop ``_schedule_reconcile()`` from
  ``_revocation_fence_clear``.
"""

from __future__ import annotations

import pytest

from kiro_crew import bot_chat_app_tools as tools
from kiro_crew.bot_chat_held_offers import fence_holds, reconcile_view

APP = "team-app"
ALIAS = f"{APP}:team"
ENTRY = {"command": "python3", "args": ["/a/server.py"]}


@pytest.fixture
def reconcile(monkeypatch):
    """Run ``after_app_change`` for Bot ``atlas`` offered ``ALIAS``; return what it queued."""
    seen: dict[str, list] = {"reload": [], "fresh": []}
    offer = {"entry": ENTRY}

    async def _fresh(state, slugs):
        seen["fresh"].extend(slugs)

    monkeypatch.setattr(tools, "bot_servers", lambda slug: {ALIAS: offer["entry"]})
    monkeypatch.setattr(tools, "fresh_bot_chats", _fresh)
    monkeypatch.setattr(tools, "schedule_reload", lambda state, slugs: seen["reload"].extend(slugs))

    async def _run(was=None):
        tools._MOUNTED["atlas"] = dict(was or {})
        await tools.after_app_change(object())
        return seen

    _run.offer = offer
    return _run


def _hold_span():
    tools._REVOCATION_FENCE.add(APP)
    tools._FENCE_HOLD_COUNTS[APP] = 1


@pytest.mark.asyncio
async def test_a_chat_started_inside_a_stop_span_is_not_reloaded_into_it(reconcile):
    _hold_span()
    assert await reconcile() == {"reload": [], "fresh": []}


@pytest.mark.asyncio
async def test_a_running_chat_keeps_its_server_while_the_fence_holds(reconcile):
    _hold_span()
    reconcile.offer["entry"] = {**ENTRY, "args": ["/b/server.py"]}  # updated meanwhile
    running = tools._fingerprints({ALIAS: ENTRY})
    assert await reconcile(was=running) == {"reload": [], "fresh": []}


@pytest.mark.asyncio
async def test_a_trust_withdrawal_under_way_holds_the_offer(reconcile):
    tools.revoke_app_generation(APP)
    assert tools._FENCE_HOLD_COUNTS.get(APP, 0) == 0
    assert await reconcile() == {"reload": [], "fresh": []}


@pytest.mark.asyncio
async def test_this_bots_pending_close_holds_the_offer(reconcile):
    tools._PENDING_CLOSE_WITHHELD["atlas"] = tools._fingerprints({ALIAS: ENTRY})
    assert await reconcile() == {"reload": [], "fresh": []}


@pytest.mark.asyncio
async def test_another_bots_pending_close_does_not_hold_this_one(reconcile):
    tools._REVOCATION_FENCE.add(APP)
    tools._PENDING_CLOSE_APPS.add(("orion", APP))
    assert (await reconcile())["reload"] == ["atlas"]


@pytest.mark.asyncio
async def test_only_the_last_span_exit_reconciles(monkeypatch):
    scheduled: list[None] = []
    monkeypatch.setattr(tools, "_schedule_reconcile", lambda: scheduled.append(None))
    async with tools.app_stop_fence(APP):
        async with tools.app_stop_fence(APP):
            pass
        assert scheduled == []
    assert scheduled == [None]
    assert APP not in tools._REVOCATION_FENCE


def test_a_withheld_offer_counts_once_its_entry_changes_and_platform_names_pass():
    view = reconcile_view(
        "atlas",
        {ALIAS: "new", "kirocrew-core": "k", "other-app:x": "same"},
        {},
        unmounted={ALIAS: "old", "other-app:x": "same"},
        pending_close_withheld={},
        fence={"kirocrew-core"},
        hold_counts={},
        pending_close=set(),
    )
    assert view == {ALIAS: "new", "kirocrew-core": "k"}


def test_fence_holds_nothing_for_an_app_it_does_not_name():
    assert not fence_holds(APP, "atlas", fence=set(), hold_counts={APP: 1}, pending_close=set())
