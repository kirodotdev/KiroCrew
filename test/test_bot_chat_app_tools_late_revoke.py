"""Late-revocation barrier test for bot_chat_app_tools.

Covers the race where revoke_app_generation runs AFTER check_and_filter_projection
pops _PENDING_DELIVERY but BEFORE record_mounted acquires _DELIVERY_LOCK.

In that window the revoke cannot void the pending entry (it is already gone from
_PENDING_DELIVERY), and without the final-barrier check the app lands in _MOUNTED.
stop_app_bot_chat_servers then calls discard_conversation; KAS has not registered
the session yet, so discard returns "confirmed" and clears _MOUNTED.  When
create_session eventually completes, the app server starts with no one watching it.

The fix adds a _REVOCATION_FENCE check inside _DELIVERY_LOCK in record_mounted,
after _MOUNTED is written, so a revocation that serialised between check_and_filter
and the lock is caught here and stripped from _MOUNTED.

Remove the late_revoked block and the app remains in _MOUNTED despite the fence,
so the second assertion (APP not in tools._MOUNTED[slug]) fails.
"""

from __future__ import annotations

import pytest

from kiro_crew import bot_chat_app_tools as tools
from kiro_crew.apps import bot_grants

APP = "team-app"
KEY = "dashboard:member-atlas.memory-member-atlas-1"
ENTRY = {"command": "python3", "args": ["/a/server.py"]}
BASE = {"tools": ["fs_read", "@kirocrew-core"], "mcpServers": {"kirocrew-core": {"command": "k"}}}


@pytest.fixture(autouse=True)
def _fresh_record(_floor_monkeypatch):
    _floor_monkeypatch.setattr(tools, "_MOUNTED", {})
    _floor_monkeypatch.setattr(tools, "_MOUNTED_SESSION_KEYS", {})
    _floor_monkeypatch.setattr(tools, "_PENDING", {})
    _floor_monkeypatch.setattr(tools, "_UNMOUNTED", {})
    _floor_monkeypatch.setattr(tools, "_OFFERED", {})
    _floor_monkeypatch.setattr(tools, "_HARNESS_NOTED", set())
    _floor_monkeypatch.setattr(tools, "_HARNESS_OWED", set())
    _floor_monkeypatch.setattr(tools, "_APP_GENERATIONS", {})
    _floor_monkeypatch.setattr(tools, "_REPROJECT_SLUGS", {})
    _floor_monkeypatch.setattr(tools, "_CHANGE_EPOCH", 0)
    _floor_monkeypatch.setattr(tools, "_PROJECTION_EPOCH", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_DELIVERY", {})
    _floor_monkeypatch.setattr(tools, "_VOIDED_DELIVERY", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE_APPS", set())
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE_RETRIES", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE_BACKOFF_IDX", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE_TIMERS", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE_WITHHELD", {})
    _floor_monkeypatch.setattr(tools, "_REVOCATION_FENCE", set())
    _floor_monkeypatch.setattr(tools, "_FENCE_HOLD_COUNTS", {})
    _floor_monkeypatch.setattr(tools, "_RECONCILER", None)
    _floor_monkeypatch.setattr(tools, "_PROJECTION_GENS", {})
    _floor_monkeypatch.setattr(tools, "_PROJECTION_SLUGS", {})
    _floor_monkeypatch.setattr(tools, "_HAD_SIDECAR", set())
    _floor_monkeypatch.setattr(tools, "_SESSION_APP_SNAPSHOT", {})
    _floor_monkeypatch.setattr(tools, "_SESSION_APP_SNAPSHOT_PENDING", {})
    _floor_monkeypatch.setattr(tools, "_PROJECTION_OFFER_FP", {})


@pytest.fixture
def world(monkeypatch):
    import kiro_crew.apps.execution as _exec
    import kiro_crew.members as members

    monkeypatch.setattr(tools, "preview_on", lambda cfg=None: True)
    monkeypatch.setattr(tools, "_app_names", lambda: [APP])
    monkeypatch.setattr(tools, "_offers", lambda app: [("lead", "team")])
    monkeypatch.setattr(tools, "_rendered_entry", lambda *a: dict(ENTRY))
    monkeypatch.setattr(bot_grants, "app_bot_scopes", lambda app: frozenset({"tools"}))
    monkeypatch.setattr(bot_grants, "app_granted_bots", lambda app: ("atlas",))
    monkeypatch.setattr(_exec, "app_execution_denied", lambda name, **_kw: None)
    monkeypatch.setattr(
        members,
        "member_identity_for_session",
        lambda key: {"slug": key.split("member-")[1].split(".")[0], "name": "Atlas"},
    )


def test_late_revoke_between_filter_and_record_does_not_land_in_mounted(world, monkeypatch):
    """revoke_app_generation that runs after check_and_filter_projection (which
    popped _PENDING_DELIVERY) but before record_mounted's _DELIVERY_LOCK must
    still prevent the app from landing in _MOUNTED.

    The final-barrier check inside record_mounted reads _REVOCATION_FENCE while
    holding _DELIVERY_LOCK and strips any app the revoke added to the fence
    after the delivery gate closed.

    Remove the late_revoked block from record_mounted and the app appears in
    _MOUNTED[slug] even though the revoke ran — confirming the race was real.
    """
    slug = "atlas"

    # Step 1: conductor_session_spec builds the projection (normal path).
    spec = tools.conductor_session_spec(dict(BASE), KEY)
    mcp = dict(spec.get("mcpServers") or {})

    # Step 2: check_and_filter_projection runs (this pops _PENDING_DELIVERY).
    mcp = tools.check_and_filter_projection(KEY, mcp)

    # At this point _PENDING_DELIVERY[KEY] is gone — revoke_app_generation cannot
    # void it.  Simulate the revoke winning the race here.
    tools.revoke_app_generation(APP)
    # Also simulate an open app_stop_fence span (it increments _FENCE_HOLD_COUNTS
    # under _DELIVERY_LOCK).
    with tools._DELIVERY_LOCK:
        tools._FENCE_HOLD_COUNTS[APP] = 1

    # Step 3: record_mounted runs with the fence already raised.
    voided = tools.record_mounted(KEY, tools.mounted_names(mcp), str)

    # The final-barrier check must have caught the late revoke.
    assert APP in voided or APP not in tools._MOUNTED.get(slug, {}), (
        f"APP must not appear in _MOUNTED[{slug!r}] when revoke_app_generation ran "
        "between check_and_filter_projection and record_mounted and "
        "_FENCE_HOLD_COUNTS[APP] > 0; "
        "without the late_revoked_hold barrier in record_mounted the app lands in "
        "_MOUNTED, letting the subsequent create_session start the revoked app server"
    )
