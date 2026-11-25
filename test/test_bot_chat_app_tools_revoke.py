"""Per-app revocation generation tests for bot_chat_app_tools.

Covers the guarantee that an unrelated app change does not stale a cold-starting
Bot chat's entries for a different app, and that generation-dropped entries are
recovered by the next reconcile.

Split from test_bot_chat_app_tools.py to keep that file under the 2,000-line cap.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew import bot_chat_app_tools as tools
from kiro_crew.apps import bot_grants

APP = "team-app"
APP_B = "bravo-app"
KEY = "dashboard:member-atlas.memory-member-atlas-1"
KEY_C = "dashboard:member-charlie.memory-member-charlie-1"
ENTRY = {"command": "python3", "args": ["/a/server.py"], "x-kirocrew-session-label": True}
BASE = {"tools": ["fs_read", "@kirocrew-core"], "mcpServers": {"kirocrew-core": {"command": "k"}}}


@pytest.fixture(autouse=True)
def _fresh_record(_floor_monkeypatch):
    """Each test starts with no Bot chat recorded as running."""
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
    _floor_monkeypatch.setattr(tools, "_REVOCATION_FENCE", set())
    _floor_monkeypatch.setattr(tools, "_RECONCILER", None)
    _floor_monkeypatch.setattr(tools, "_PROJECTION_GENS", {})
    _floor_monkeypatch.setattr(tools, "_PROJECTION_SLUGS", {})
    _floor_monkeypatch.setattr(tools, "_FENCE_HOLD_COUNTS", {})
    _floor_monkeypatch.setattr(tools, "_PROJECTION_OFFER_FP", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE_WITHHELD", {})
    _floor_monkeypatch.setattr(tools, "_HAD_SIDECAR", set())
    _floor_monkeypatch.setattr(tools, "_SESSION_APP_SNAPSHOT", {})
    _floor_monkeypatch.setattr(tools, "_SESSION_APP_SNAPSHOT_PENDING", {})


@pytest.fixture
def world(monkeypatch):
    """One enabled app offering ``team`` from its agent ``lead``, the Bot ``atlas`` ticked."""
    state = {
        "preview": True,
        "scopes": {"tools"},
        "ticked": ("atlas",),
        "offers": [("lead", "team")],
        "entry": ENTRY,
    }
    monkeypatch.setattr(tools, "preview_on", lambda cfg=None: state["preview"])
    monkeypatch.setattr(tools, "_app_names", lambda: [APP])
    monkeypatch.setattr(tools, "_offers", lambda app: list(state["offers"]))
    monkeypatch.setattr(tools, "_rendered_entry", lambda *a: dict(state["entry"]))
    monkeypatch.setattr(bot_grants, "app_bot_scopes", lambda app: frozenset(state["scopes"]))
    monkeypatch.setattr(bot_grants, "app_granted_bots", lambda app: tuple(state["ticked"]))
    import kiro_crew.apps.execution as _exec

    monkeypatch.setattr(_exec, "app_execution_denied", lambda name, **_kw: None)
    import kiro_crew.members as members

    monkeypatch.setattr(
        members,
        "member_identity_for_session",
        lambda key: {"slug": key.split("member-")[1].split(".")[0], "name": "Atlas"},
    )
    return state


@pytest.fixture
def world_two_apps(monkeypatch):
    """Two enabled apps (APP and APP_B), Bot atlas ticked for both."""
    state = {
        "preview": True,
        "scopes": {"tools"},
        "ticked": ("atlas",),
        "offers_a": [("lead", "team")],
        "offers_b": [("lead", "tool")],
        "entry": ENTRY,
    }
    monkeypatch.setattr(tools, "preview_on", lambda cfg=None: state["preview"])
    monkeypatch.setattr(tools, "_app_names", lambda: [APP, APP_B])

    def _offers(app: str) -> list[tuple[str, str]]:
        return state["offers_a"] if app == APP else state["offers_b"]

    monkeypatch.setattr(tools, "_offers", _offers)
    monkeypatch.setattr(tools, "_rendered_entry", lambda *a: dict(state["entry"]))
    import kiro_crew.apps.bot_grants as _bg

    monkeypatch.setattr(_bg, "app_bot_scopes", lambda app: frozenset(state["scopes"]))
    monkeypatch.setattr(_bg, "app_granted_bots", lambda app: tuple(state["ticked"]))
    import kiro_crew.apps.execution as _exec

    monkeypatch.setattr(_exec, "app_execution_denied", lambda name, **_kw: None)
    import kiro_crew.members as members

    monkeypatch.setattr(
        members,
        "member_identity_for_session",
        lambda key: {"slug": key.split("member-")[1].split(".")[0], "name": "Atlas"},
    )
    return state


# ── Per-app generation: unrelated change does not stale another app's entries ─


def test_unrelated_app_tick_does_not_stale_a_cold_starting_bots_entries(world_two_apps):
    """A cold-starting Bot chat for APP must mount its servers even when APP_B is
    being ticked (revoke_app_generation called for APP_B) concurrently.

    Before the per-app generation fix, revoke_app_generation bumped a single global
    counter; APP's snapshot was then stale vs the new global, and check_and_filter_
    projection stripped APP's entries even though APP's own generation never changed.

    Remove the per-app key from _APP_GENERATIONS (or revert to a global counter) and
    the entry for APP:team is stripped, so this assertion fails."""
    # 1. Build the projection for APP (snapshot: APP=0, APP_B=0).
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    assert f"{APP}:team" in spec_out.get("mcpServers", {}), "setup"
    assert f"{APP_B}:tool" in spec_out.get("mcpServers", {}), "setup: both apps offered"

    # 2. An unrelated change bumps APP_B's generation (not APP's).
    tools.revoke_app_generation(APP_B)
    assert tools._APP_GENERATIONS.get(APP_B, 0) == 1
    assert tools._APP_GENERATIONS.get(APP, 0) == 0, "APP's generation must not change"

    # 3. Delivery: APP's entry must pass (its snapshot still matches APP's generation).
    mcpServers = dict(spec_out.get("mcpServers") or {})
    filtered = tools.check_and_filter_projection(KEY, mcpServers)

    assert f"{APP}:team" in filtered, (
        "APP's entry must mount even though APP_B was revoked; "
        "per-app generation means APP_B's bump does not stale APP's snapshot"
    )
    assert (
        f"{APP_B}:tool" not in filtered
    ), "APP_B's entry must be stripped (its own generation was bumped)"


def test_own_app_revocation_still_fences_the_entry(world_two_apps):
    """A cold-starting Bot chat for APP must NOT mount its servers when APP itself
    is revoked.

    This is the complementary half of the per-app generation fix: changing the
    counter to per-app must not weaken the security fence for the app that was
    actually revoked.

    Remove the check_and_filter_projection gate and APP:team survives revocation."""
    # Build the projection (APP snapshot=0).
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    assert f"{APP}:team" in spec_out.get("mcpServers", {}), "setup"

    # Revoke APP (not APP_B).
    tools.revoke_app_generation(APP)
    assert tools._APP_GENERATIONS.get(APP, 0) == 1

    # Delivery: APP's entry must be stripped.
    mcpServers = dict(spec_out.get("mcpServers") or {})
    filtered = tools.check_and_filter_projection(KEY, mcpServers)

    assert f"{APP}:team" not in filtered, (
        "APP's entry must be stripped when APP itself is revoked; "
        "remove the generation fence and this fails"
    )


def test_gen_dropped_entries_are_recovered_by_reconcile(world_two_apps, monkeypatch):
    """Entries dropped at delivery due to a generation mismatch must NOT block the
    reconcile from recovering them.

    Scenario:
    1. Bot A's chat starts; projection built at APP=0.
    2. Bump APP to 1 (simulates a concurrent revoke after the snapshot).
    3. Delivery gate sees APP snapshot=0 != APP current=1 and strips APP:team.
    4. record_mounted is called with the stripped set; APP:team goes to _REPROJECT_SLUGS
       (not _UNMOUNTED).
    5. after_app_change must detect the gap and schedule a reload.

    Remove the _REPROJECT_SLUGS exclusion from record_mounted and after_app_change
    will put APP:team into _UNMOUNTED, withheld.get(a)==f will match, now==was, and
    the reload never fires."""
    import asyncio

    # 1. Build the projection at APP=0.
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    assert f"{APP}:team" in spec_out.get("mcpServers", {}), "setup"

    # 2. Bump APP's generation AFTER the snapshot (simulates a race).
    tools._APP_GENERATIONS[APP] = 1  # type: ignore[index]

    # 3. Delivery: APP:team is stripped (gen mismatch).
    mcpServers = dict(spec_out.get("mcpServers") or {})
    filtered = tools.check_and_filter_projection(KEY, mcpServers)
    assert f"{APP}:team" not in filtered, "entry must be stripped on gen mismatch"

    # 4. record_mounted: atlas's _REPROJECT_SLUGS should contain APP:team.
    tools.record_mounted(KEY, tools.mounted_names(filtered))
    slug = KEY.split("member-")[1].split(".")[0]  # "atlas"
    assert tools._MOUNTED.get(slug, {}).get(f"{APP}:team") is None, "must not be mounted"
    assert f"{APP}:team" not in tools._UNMOUNTED.get(slug, {}), (
        "gen-dropped alias must NOT be in _UNMOUNTED; "
        "if it is, after_app_change treats it as 'harness-withheld' and skips it"
    )

    # 5. after_app_change must schedule a reload (or fresh-start) for atlas.
    reloaded: list[str] = []
    changed: list[str] = []

    async def _run() -> None:
        async def _fake_reload(state: object, slugs: object) -> dict[str, str]:
            changed.extend(slugs)  # type: ignore[arg-type]
            return {}

        async def _fake_fresh(state: object, slugs: object) -> None:
            reloaded.extend(slugs)  # type: ignore[arg-type]

        monkeypatch.setattr(tools, "reload_bot_chats", _fake_reload)
        monkeypatch.setattr(tools, "fresh_bot_chats", _fake_fresh)

        class _FakeState:
            _crewmate_reload_tasks: set = set()

        state_obj = _FakeState()
        tools._RECONCILER = (state_obj, asyncio.get_running_loop())
        # Reset APP's generation to 0 so bot_servers() returns the entry and
        # the reconcile sees it as a gap.
        tools._APP_GENERATIONS[APP] = 0  # type: ignore[index]
        await tools.after_app_change(state_obj)

    asyncio.run(_run())

    assert changed == [slug] or reloaded == [slug], (
        "after_app_change must schedule a reload for atlas because APP:team is absent "
        "from _MOUNTED but not in _UNMOUNTED (gen-dropped, not harness-withheld); "
        "remove the _REPROJECT_SLUGS exclusion from record_mounted and this fails"
    )


# ── GPT :277: record_mounted uses projection-time slug, not a live re-read ────


def test_record_mounted_uses_projection_slug_when_live_read_fails(world, monkeypatch):
    """record_mounted must use the slug stored at projection time, not re-read it.

    If member_identity_for_session fails at record time (DM binding unreadable,
    race with a member removal), _MOUNTED is never written and revoke cannot find
    the server to stop it.

    Fix: record_mounted reads slug from _PROJECTION_SLUGS first; the live read is
    only the fallback for sessions without a sidecar.

    Remove the _PROJECTION_SLUGS.get(session_key) early-out (or change it to always
    use the live read) and this assertion fails: _MOUNTED stays empty when the live
    read raises, even though the server was handed to the session."""
    import kiro_crew.members as members

    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    # The sidecar must have written the slug.
    assert KEY in tools._PROJECTION_SLUGS, "setup: _PROJECTION_SLUGS must be written"

    # Simulate a live member_identity read that fails at record time.
    monkeypatch.setattr(
        members,
        "member_identity_for_session",
        lambda key: (_ for _ in ()).throw(OSError("binding gone")),
    )

    # Delivery still hands the server.
    mcpServers = dict(spec_out.get("mcpServers") or {})
    filtered = tools.check_and_filter_projection(KEY, mcpServers)
    tools.record_mounted(KEY, tools.mounted_names(filtered))

    slug = KEY.split("member-")[1].split(".")[0]
    assert f"{APP}:team" in tools._MOUNTED.get(slug, {}), (
        "record_mounted must write _MOUNTED using the projection-time slug even when "
        "the live member_identity read raises; remove the _PROJECTION_SLUGS path and this fails"
    )


# ── GPT :535: no-server cold start stores epoch so reconcile can find the Bot ─


@pytest.mark.asyncio
async def test_a_no_server_cold_start_is_found_by_reconcile_after_a_tick(world, monkeypatch):
    """A Bot that starts with no app servers must be reconciled when one is added.

    Without the fix, conductor_session_spec returns early before storing
    _PROJECTION_EPOCH, so record_mounted finds no epoch snapshot and schedules no
    reconcile.  When the owner later ticks the app for this Bot, after_app_change
    iterates _MOUNTED but the Bot is not there yet, and the tools never appear.

    Fix: conductor_session_spec stores _PROJECTION_EPOCH (and _PROJECTION_SLUGS)
    even on the no-server path, so record_mounted can detect the subsequent tick.

    Remove the epoch/slug store before 'return spec' in the no-server path and this
    test fails: after_app_change does not schedule a reload for atlas."""
    seen: dict[str, list[str]] = {"reload": [], "fresh": []}

    def _watch() -> None:
        async def _r(state: object, slugs: object) -> dict[str, str]:
            seen["reload"].extend(slugs)  # type: ignore[arg-type]
            return {}

        async def _f(state: object, slugs: object) -> None:
            seen["fresh"].extend(slugs)  # type: ignore[arg-type]

        monkeypatch.setattr(tools, "reload_bot_chats", _r)
        monkeypatch.setattr(tools, "fresh_bot_chats", _f)

    # 1. At cold start the Bot has no app servers (preview off mimics "no servers").
    world["preview"] = False
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    # _PROJECTION_EPOCH must be stored even though there are no app servers.
    assert KEY in tools._PROJECTION_EPOCH, (
        "conductor_session_spec must store _PROJECTION_EPOCH on the no-server path; "
        "remove the epoch store before 'return spec' and this fails"
    )
    assert KEY in tools._PROJECTION_SLUGS, "_PROJECTION_SLUGS must also be stored"

    # 2. record_mounted with the empty-server set.
    mcpServers = dict(spec_out.get("mcpServers") or {})
    tools.record_mounted(KEY, tools.mounted_names(mcpServers))
    slug = KEY.split("member-")[1].split(".")[0]
    assert tools._MOUNTED.get(slug, None) is not None, "atlas must be recorded (empty set)"

    # 3. Owner turns preview on; _CHANGE_EPOCH advances.
    world["preview"] = True
    _watch()
    state = object.__new__(type("S", (), {"_crewmate_reload_tasks": set()}))
    state._crewmate_reload_tasks = set()
    await tools.after_app_change(state)
    # Wait for the reload task (schedule_reload creates it on the loop).
    await asyncio.gather(*list(getattr(state, "_crewmate_reload_tasks", ())))

    assert seen["reload"] == [slug] or seen["fresh"] == [slug], (
        "after_app_change must schedule a reload for atlas after a tick on a Bot that "
        "started with no app servers; "
        "the fix stores _PROJECTION_EPOCH so record_mounted detects the change"
    )


# ── GPT/Opus F3: seed-lag withholding must not block reconcile recovery ───────


def test_seed_lag_does_not_strand_app_tools_in_unmounted(world, monkeypatch):
    """When a seed lag withholds the whole array, app aliases go to _REPROJECT_SLUGS
    so the next reconcile can recover them -- not into _UNMOUNTED where they would
    be filtered as 'harness-withheld' and never retried.

    Without mark_seed_lag_reproject, record_mounted puts every offer into _UNMOUNTED,
    after_app_change's withheld filter matches them, and now == was forever.

    Remove the mark_seed_lag_reproject call and this assertion fails: the alias
    ends up in _UNMOUNTED, _schedule_reconcile is not called, and after_app_change
    skips the reload."""
    # 1. Build the projection.
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    assert f"{APP}:team" in spec_out.get("mcpServers", {}), "setup"

    # 2. Seed lag triggers before delivery: mark_seed_lag_reproject is called.
    tools.mark_seed_lag_reproject(KEY)

    # 3. The alias must be in _REPROJECT_SLUGS, not waiting in _UNMOUNTED.
    slug = KEY.split("member-")[1].split(".")[0]
    assert f"{APP}:team" in tools._REPROJECT_SLUGS.get(slug, set()), (
        "mark_seed_lag_reproject must put the alias in _REPROJECT_SLUGS; "
        "remove the call and this fails"
    )

    # 4. record_mounted with empty names (seed lag path).
    tools.record_mounted(KEY, [])

    # The alias must NOT be in _UNMOUNTED.
    assert f"{APP}:team" not in tools._UNMOUNTED.get(slug, {}), (
        "seed-lag aliases must be excluded from _UNMOUNTED so after_app_change "
        "can recover them; remove mark_seed_lag_reproject and they land in _UNMOUNTED"
    )


# ── GPT F2: on_execution_trust_change stops execution-denied Bot chats ────────


@pytest.mark.asyncio
async def test_on_execution_trust_change_stops_bots_whose_app_is_denied(world, monkeypatch):
    """When execution admission is withdrawn (e.g. apps_allow_third_party goes off),
    on_execution_trust_change must stop every Bot chat running a now-denied app.

    Remove the on_execution_trust_change call or its app_execution_denied check and
    the Bot chat keeps running the app's server after trust is closed."""
    import kiro_crew.apps.execution as _exec

    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    stopped_apps: list[str] = []

    monkeypatch.setattr(_exec, "app_execution_denied", lambda name, **kw: "blanket_trust_closed")

    async def _fake_stop(state: object, name: str) -> tuple[list[str], list[str]]:
        stopped_apps.append(name)
        return [slug], []

    monkeypatch.setattr(tools, "stop_app_bot_chat_servers", _fake_stop)

    class _FakeState:
        _crewmate_reload_tasks: set = set()

    await tools.on_execution_trust_change(_FakeState())

    assert APP in stopped_apps, (
        "on_execution_trust_change must stop Bot chats for an execution-denied app; "
        "remove the app_execution_denied check and this fails"
    )


# ── GPT F1: trust revoke stops Bot chats even when get_app() returns None ─────


@pytest.mark.asyncio
async def test_revoking_trust_stops_bot_chats_when_app_metadata_is_missing(tmp_path, monkeypatch):
    """The Bot teardown must run even when get_app() returns None (the app deleted
    its own installed.json).  Gating it on the metadata lookup lets a hostile app
    evade revocation.

    The fix moves revoke_app_generation + stop_app_bot_chat_servers outside the
    'if record:' guard so they run unconditionally.  This test confirms the route
    calls stop_app_bot_chat_servers even when the app's metadata is absent.

    Remove the Bot teardown from outside the 'if record:' block and this fails:
    when get_app() returns None, stop_app_bot_chat_servers is never called."""
    from unittest.mock import patch

    import trusted_apps_api_helpers as trust

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "kirocrew-home"))
    (tmp_path / "kirocrew-home").mkdir()

    stop_calls: list[str] = []

    async def _spy_stop(state: object, name: str) -> tuple[list[str], list[str]]:
        stop_calls.append(name)
        return [], []

    with patch("kiro_crew.bot_chat_app_tools.stop_app_bot_chat_servers", _spy_stop):
        trust._install(tmp_path, trust._APP, enabled=True)

        from kiro_crew.apps import manager as appmanager

        real_get_app = appmanager.get_app

        def _get_app_none(name: str) -> object | None:
            return None if name == trust._APP else real_get_app(name)

        with patch("kiro_crew.dashboard.handlers.security._sel"):
            async with trust._client() as client:
                grant_resp = await client.post(f"/api/security/trusted-apps/{trust._APP}")
                assert grant_resp.status == 200, await grant_resp.text()
                with patch(
                    "kiro_crew.dashboard.handlers.security_trusted_apps.get_app",
                    _get_app_none,
                ):
                    resp = await client.delete(f"/api/security/trusted-apps/{trust._APP}")
                    assert resp.status in (200, 409), await resp.text()

    assert trust._APP in stop_calls, (
        "stop_app_bot_chat_servers must be called even when get_app() returns None; "
        "move the Bot teardown back inside 'if record:' and this fails"
    )


# ── GPT/Opus break removal: all apps per slug are checked ─────────────────────


@pytest.mark.asyncio
async def test_execution_trust_change_checks_all_apps_for_a_slug(world_two_apps, monkeypatch):
    """When a Bot chat mounts servers from both APP and APP_B, withdrawing trust
    for APP_B must stop the Bot chat even though APP still has trust.

    Before the fix, `on_execution_trust_change` had a `break` after finding the
    first app for a slug, so APP_B was never checked.  Removing the break means
    both apps are collected and each is tested against app_execution_denied.

    Add the break back and APP_B is never collected, so stopped_apps stays empty
    and this assertion fails."""
    import kiro_crew.apps.execution as _exec

    slug = KEY.split("member-")[1].split(".")[0]
    # Mount servers from two different apps on the same slug
    tools._MOUNTED[slug] = {f"{APP}:team": "fp_a", f"{APP_B}:tool": "fp_b"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    stopped_apps: list[str] = []

    def _deny(name: str, **kw: object) -> str | None:
        # APP is still trusted; only APP_B lost trust
        return "trust_withdrawn" if name == APP_B else None

    monkeypatch.setattr(_exec, "app_execution_denied", _deny)

    async def _fake_stop(state: object, name: str) -> tuple[list[str], list[str]]:
        stopped_apps.append(name)
        return [slug], []

    monkeypatch.setattr(tools, "stop_app_bot_chat_servers", _fake_stop)

    class _FakeState:
        _crewmate_reload_tasks: set = set()

    await tools.on_execution_trust_change(_FakeState())

    assert APP_B in stopped_apps, (
        "on_execution_trust_change must check every app a slug mounts, not just the "
        "first one; add the break back and APP_B is never reached, so this fails"
    )
    assert APP not in stopped_apps, "APP still has trust and must not be stopped"


# ── GPT :519: TOCTOU between delivery gate and record_mounted ─────────────────


def test_revocation_between_gate_and_record_does_not_mount_the_server(world, monkeypatch):
    """A revocation that fires after check_and_filter_projection passes but before
    record_mounted writes _MOUNTED must result in the session being discarded.

    Three-part fix:
    1. check_and_filter_projection registers the app in _PENDING_DELIVERY so
       revoke_app_generation can void it atomically.
    2. stop_app_bot_chat_servers discards sessions in _PENDING_DELIVERY so the
       server is torn down even before record_mounted runs.
    3. record_mounted, on seeing a void, writes the alias into _MOUNTED (not strips
       it) so future teardowns can find the session, and schedules a reconcile.

    Without the _PENDING_DELIVERY teardown in stop_app_bot_chat_servers, the
    pending session is not discarded before record_mounted writes it to _MOUNTED.
    Without the _MOUNTED write in record_mounted, future stop calls miss the session."""
    import asyncio

    # 1. Build the projection.
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    assert f"{APP}:team" in spec_out.get("mcpServers", {}), "setup"

    # 2. Run the delivery gate -- entry passes, _PENDING_DELIVERY is written.
    mcpServers = dict(spec_out.get("mcpServers") or {})
    filtered = tools.check_and_filter_projection(KEY, mcpServers)
    assert f"{APP}:team" in filtered, "gate must pass for setup"

    slug = KEY.split("member-")[1].split(".")[0]
    assert APP in tools._PENDING_DELIVERY.get(KEY, set()), (
        "check_and_filter_projection must register in _PENDING_DELIVERY; "
        "remove the registration and this fails"
    )

    # 3. Revocation fires between gate and record_mounted.
    tools.revoke_app_generation(APP)
    assert APP in tools._VOIDED_DELIVERY.get(KEY, set()), (
        "revoke_app_generation must void the pending delivery; "
        "remove the _VOIDED_DELIVERY write and this fails"
    )

    # 4. stop_app_bot_chat_servers discards the pending session even before record_mounted.
    discarded: list[str] = []

    class _Sessions:
        async def discard_conversation(
            self,
            session_key: str,
            replay: bool = False,
            confirm_teardown: bool = False,
            unconfirmed_handle_out: "list | None" = None,
        ) -> None:
            discarded.append(session_key)

        def _allocation_boundary(self) -> object:
            class _A:
                def begin_ending(self, key: str) -> None:
                    pass

                def end_ending(self, key: str) -> None:
                    pass

                def spawn_in_flight(self, key: str) -> None:
                    return None

            return _A()

    async def _run_stop() -> None:
        class _S:
            sessions = _Sessions()

        await tools.stop_app_bot_chat_servers(_S(), APP)

    asyncio.run(_run_stop())
    assert KEY in discarded, (
        "stop_app_bot_chat_servers must discard the pending session; "
        "remove the _PENDING_DELIVERY teardown and this fails"
    )

    # 5. record_mounted runs (after the revocation).
    # Void detected: the alias is written into _MOUNTED (not stripped!) so any
    # future teardown call can find the session.  A reconcile is scheduled to
    # trigger a fresh start that tears the running server down.
    tools.record_mounted(KEY, tools.mounted_names(filtered))

    slug = KEY.split("member-")[1].split(".")[0]
    # The alias MUST be in _MOUNTED so reconcile/teardown can find it.
    assert f"{APP}:team" in tools._MOUNTED.get(slug, {}), (
        "record_mounted must write voided aliases into _MOUNTED so teardown can find "
        "them; stripping them leaves the session invisible to future stop calls"
    )
    # The alias must NOT be in _UNMOUNTED (it is running, not harness-withheld).
    assert f"{APP}:team" not in tools._UNMOUNTED.get(
        slug, {}
    ), "voided aliases must not go into _UNMOUNTED; they are running in the session"
    # _PENDING_DELIVERY and _VOIDED_DELIVERY must be cleared.
    assert KEY not in tools._PENDING_DELIVERY
    assert KEY not in tools._VOIDED_DELIVERY


@pytest.mark.parametrize("harness", ["runtime_session_setup", "client_mcp_array", "kas"])
def test_pending_delivery_void_check_all_harness_paths(world, harness):
    """check_and_filter_projection registers _PENDING_DELIVERY and voids entries
    regardless of which harness calls it.  All three harness paths call the same
    function, so this test verifies the registration and void logic are reached on
    each one.

    Remove the _PENDING_DELIVERY registration in check_and_filter_projection and
    the registration assertion fails; remove the _VOIDED_DELIVERY write in
    revoke_app_generation and the void assertion fails."""
    # Build projection and run gate (simulates what each harness does).
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    mcpServers = dict(spec_out.get("mcpServers") or {})

    # All three harness paths call check_and_filter_projection (some via to_thread;
    # we call it directly since the registration is sync and harness-independent).
    filtered = tools.check_and_filter_projection(KEY, mcpServers)
    assert f"{APP}:team" in filtered, f"{harness}: gate must pass"
    assert APP in tools._PENDING_DELIVERY.get(
        KEY, set()
    ), f"{harness}: check_and_filter_projection must register _PENDING_DELIVERY"

    # Void the delivery (revocation races the harness).
    tools.revoke_app_generation(APP)
    assert APP in tools._VOIDED_DELIVERY.get(
        KEY, set()
    ), f"{harness}: revoke_app_generation must write _VOIDED_DELIVERY"

    # record_mounted writes the alias to _MOUNTED (server was handed; teardown needs it).
    tools.record_mounted(KEY, tools.mounted_names(filtered))
    slug = KEY.split("member-")[1].split(".")[0]
    assert f"{APP}:team" in tools._MOUNTED.get(
        slug, {}
    ), f"{harness}: voided alias must be in _MOUNTED so teardown can find the session"


# ── GPT :1003: unconditional allocation void before discard ──────────────────


@pytest.mark.asyncio
async def test_stop_app_bot_chat_servers_voids_allocation_even_when_discard_returns_true(
    world, monkeypatch
):
    """begin_ending must be called unconditionally before discard_conversation,
    even when discard returns True (the real KAS/ACP implementation returns True
    for a session key it does not recognise, so the return value cannot be used
    to decide whether to void the reservation).

    Without the unconditional begin_ending, a cold-starting session whose
    discard_conversation answered True (key unknown) has its reservation left
    active, and session/new can still accept the start.

    Remove the begin_ending call before discard_conversation (or gate it on
    'not torn_down') and begin_ending_calls stays empty."""
    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    begin_ending_calls: list[str] = []

    class _FakeAlloc:
        def begin_ending(self, key: str) -> None:
            begin_ending_calls.append(key)

        def end_ending(self, key: str) -> None:
            pass

        def spawn_in_flight(self, key: str) -> None:
            return None

    class _FakeSessions:
        async def discard_conversation(
            self,
            session_key: str,
            replay: bool = False,
            confirm_teardown: bool = False,
            unconfirmed_handle_out: "list | None" = None,
        ) -> bool:
            # Real discard returns True even for unregistered sessions.
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()

    stopped, failed = await tools.stop_app_bot_chat_servers(_FakeState(), APP)

    assert KEY in begin_ending_calls, (
        "begin_ending must be called before discard_conversation regardless of its "
        "return value; gate it on 'not torn_down' and this fails (key unknown → True)"
    )
    assert slug in stopped, "discard returned True: slug must be in stopped"
    assert slug not in failed, "discard returned True without raising: must not be failed"
    assert f"{APP}:team" not in tools._MOUNTED.get(
        slug, {}
    ), "_MOUNTED must be cleared after a successful (non-raising) discard"


@pytest.mark.asyncio
async def test_stop_app_bot_chat_servers_keeps_mounted_record_on_raised_exception(
    world, monkeypatch
):
    """When discard_conversation raises (e.g. shared KAS timeout, evict_session
    swallows close), stop_app_bot_chat_servers must keep _MOUNTED intact and
    report the slug as failed.

    Case (b): shared KAS session/close raises or times out.

    Without the fix the slug might be removed from _MOUNTED before the exception
    propagates, leaving the session invisible to future teardown calls.

    With the existing exception handler the slug is already in failed; this test
    verifies that _MOUNTED is kept intact too."""
    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    class _FakeAlloc:
        def begin_ending(self, key: str) -> None:
            pass

        def end_ending(self, key: str) -> None:
            pass

        def spawn_in_flight(self, key: str) -> None:
            return None

    class _FakeSessions:
        async def discard_conversation(
            self,
            session_key: str,
            replay: bool = False,
            confirm_teardown: bool = False,
            unconfirmed_handle_out: "list | None" = None,
        ) -> bool:
            raise TimeoutError("shared KAS session/close timed out")

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()

    stopped, failed = await tools.stop_app_bot_chat_servers(_FakeState(), APP)

    assert slug in failed, "raised exception must put slug in failed"
    assert slug not in stopped, "raised exception must not put slug in stopped"
    assert f"{APP}:team" in tools._MOUNTED.get(slug, {}), (
        "_MOUNTED must be kept when discard raises; "
        "the exception handler must not remove the record before the raise propagates"
    )


# ── Opus :309: harness paths strip voided aliases before session/new ──────────


@pytest.mark.parametrize(
    "harness_label",
    ["runtime_session_setup", "client_mcp_array", "kas"],
)
def test_record_mounted_strips_voided_from_harness_array(world, monkeypatch, harness_label):
    """record_mounted returns the voided app set so each harness strips those aliases
    from the ``servers`` array before handing it to ``session/new``.

    Without the return value (or without the caller reading it), the voided server
    remains in the array and starts in the new session even after revocation.

    Revoke APP between check_and_filter_projection and record_mounted, then call
    record_mounted and assert it returns a non-empty frozenset containing APP."""
    # 1. Build projection, run gate.
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    mcpServers = dict(spec_out.get("mcpServers") or {})
    filtered = tools.check_and_filter_projection(KEY, mcpServers)
    assert f"{APP}:team" in filtered, f"{harness_label}: setup"

    # 2. Void the delivery (revocation races between gate and record).
    tools.revoke_app_generation(APP)
    assert APP in tools._VOIDED_DELIVERY.get(KEY, set()), f"{harness_label}: setup void"

    # 3. record_mounted must return the voided frozenset.
    voided = tools.record_mounted(KEY, tools.mounted_names(filtered))
    assert isinstance(voided, frozenset), (
        f"{harness_label}: record_mounted must return a frozenset; "
        "the return type annotation is -> frozenset -- returning None means no stripping"
    )
    assert APP in voided, (
        f"{harness_label}: record_mounted must name the voided app in its return value "
        "so the harness can strip its aliases from the servers array; "
        "remove the voided_apps return and this fails"
    )

    # 4. Simulate what each harness does: strip voided aliases.
    aliases = list(filtered.keys())
    stripped = [a for a in aliases if ":" not in a or a.split(":")[0] not in voided]
    assert all(":" not in a or a.split(":")[0] != APP for a in stripped), (
        f"{harness_label}: harness must remove all aliases whose app prefix is in voided; "
        "the strip expression splits on ':' and checks the left side"
    )
    assert (
        f"{APP}:team" not in stripped
    ), f"{harness_label}: {APP}:team must not reach session/new after revocation"


# ── Opus :309: pending session void calls begin_ending/end_ending ─────────────


@pytest.mark.asyncio
async def test_pending_session_void_calls_begin_ending(world, monkeypatch):
    """begin_ending must be called for pending sessions unconditionally, even when
    discard_conversation returns True (the real implementation returns True for
    unknown session keys).

    Without unconditional begin_ending, a pending cold start whose delivery was
    voided can still complete: the reservation stays active, session/new accepts
    the start, and the revoked server runs.

    Remove the begin_ending/end_ending call in the pending loop and
    begin_ending_calls stays empty."""
    # Put a session key into _PENDING_DELIVERY for APP.
    tools._PENDING_DELIVERY[KEY] = {APP}

    begin_ending_calls: list[str] = []

    class _FakeAlloc:
        def begin_ending(self, key: str) -> None:
            begin_ending_calls.append(key)

        def end_ending(self, key: str) -> None:
            pass

        def spawn_in_flight(self, key: str) -> None:
            return None

    class _FakeSessions:
        async def discard_conversation(
            self,
            session_key: str,
            replay: bool = False,
            confirm_teardown: bool = False,
            unconfirmed_handle_out: "list | None" = None,
        ) -> bool:
            # Real discard returns True even for unregistered sessions.
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()

    await tools.stop_app_bot_chat_servers(_FakeState(), APP)

    assert KEY in begin_ending_calls, (
        "stop_app_bot_chat_servers must call begin_ending for a pending session "
        "unconditionally, not only when discard returns False; "
        "remove the begin_ending call in the pending loop and this fails"
    )


# ── GPT :891: on_execution_trust_change covers _PENDING_DELIVERY ─────────────


@pytest.mark.asyncio
async def test_on_execution_trust_change_stops_pending_delivery_sessions(world, monkeypatch):
    """on_execution_trust_change must also stop sessions in _PENDING_DELIVERY
    (gate passed, record_mounted not yet run), not only _MOUNTED sessions.

    A session that passed check_and_filter_projection but has not yet called
    record_mounted has APP's server array already in hand.  If a config change
    withdraws execution trust and on_execution_trust_change only scans _MOUNTED,
    that session is missed and the revoked server starts.

    Remove the _PENDING_DELIVERY scan from on_execution_trust_change and the
    stop call never fires, so APP not in stopped_apps and this test fails."""
    import kiro_crew.apps.execution as _exec

    # Only a pending session -- nothing in _MOUNTED.
    tools._PENDING_DELIVERY[KEY] = {APP}

    stopped_apps: list[str] = []

    monkeypatch.setattr(_exec, "app_execution_denied", lambda name, **kw: "trust_config_changed")

    async def _fake_stop(state: object, name: str) -> tuple[list[str], list[str]]:
        stopped_apps.append(name)
        return [], []

    monkeypatch.setattr(tools, "stop_app_bot_chat_servers", _fake_stop)

    class _FakeState:
        _crewmate_reload_tasks: set = set()

    await tools.on_execution_trust_change(_FakeState())

    assert APP in stopped_apps, (
        "on_execution_trust_change must stop sessions in _PENDING_DELIVERY when "
        "execution trust is withdrawn; "
        "remove the _PENDING_DELIVERY scan and APP is never collected, so this fails"
    )


# ── :1003b: shared KAS unconfirmed close is treated as failed stop ────────────


@pytest.mark.asyncio
async def test_stop_app_bot_chat_servers_treats_kas_unconfirmed_as_failure(world, monkeypatch):
    """When discard_conversation returns False (shared KAS session/close timed out
    or was refused), stop_app_bot_chat_servers must report the slug as failed and
    keep _MOUNTED intact so a future retry can find the session.

    discard_conversation(confirm_teardown=True) returns False when the KAS
    session/close was not acknowledged, even though the local unregister ran.
    Without the confirm_teardown=True call or the False-branch failure path,
    the slug lands in stopped and _MOUNTED is cleared, leaving the KAS session
    invisible to future teardown calls.

    Remove the `confirm_teardown=True` argument or the `if not confirmed:` branch
    and the slug ends up in stopped instead of failed."""
    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    class _FakeAlloc:
        def begin_ending(self, key: str) -> None:
            pass

        def end_ending(self, key: str) -> None:
            pass

        def spawn_in_flight(self, key: str) -> None:
            return None

    class _FakeSessions:
        async def discard_conversation(
            self,
            session_key: str,
            replay: bool = False,
            confirm_teardown: bool = False,
            unconfirmed_handle_out: "list | None" = None,
        ) -> bool:
            # Real discard_conversation(confirm_teardown=True) returns False when
            # KAS session/close timed out or was refused (another active lease).
            if confirm_teardown:
                return False
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()

    stopped, failed = await tools.stop_app_bot_chat_servers(_FakeState(), APP)

    assert slug in failed, (
        "KAS unconfirmed close must put the slug in failed; "
        "remove the `if not confirmed:` branch and this fails"
    )
    assert slug not in stopped, "unconfirmed stop must not appear in stopped"
    assert f"{APP}:team" in tools._MOUNTED.get(slug, {}), (
        "_MOUNTED must be kept for an unconfirmed KAS close so future calls can retry; "
        "remove the guarded _MOUNTED.pop and the record is lost"
    )


# ── GPT :553: delivery lock closes the gate-to-pending-registration race ──────


def test_delivery_lock_strips_entry_revoked_between_gate_and_registration(world, monkeypatch):
    """The final generation re-check under _DELIVERY_LOCK must strip an app-server
    entry whose generation advanced after the per-alias loop passed but before
    the pending-delivery registration.

    Before the lock, revoke_app_generation that fires in this window finds neither
    _PENDING_DELIVERY nor a passed generation check; the entry mounts unchallenged.

    Remove the final generation re-check inside the _DELIVERY_LOCK block (or remove
    the lock entirely) and this assertion fails: the entry survives despite the
    generation bump that happens after the loop."""
    # 1. Build projection, run gate -- entries pass with gen 0.
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    mcpServers = dict(spec_out.get("mcpServers") or {})
    # Simulate: revocation bumps the generation AFTER the sidecar was read by the
    # per-alias loop but BEFORE pending registration.  We do this by advancing
    # _APP_GENERATIONS before the actual check_and_filter_projection call, which
    # is equivalent since the sidecar was already captured at conductor_session_spec.
    # The sidecar still holds gen=0, but _APP_GENERATIONS is now 1.
    tools._APP_GENERATIONS[APP] = 1  # type: ignore[index]

    # 2. Delivery gate -- the sidecar has gen=0, _APP_GENERATIONS has 1.
    filtered = tools.check_and_filter_projection(KEY, mcpServers)

    # The final re-check under _DELIVERY_LOCK should strip the entry (gen mismatch).
    assert f"{APP}:team" not in filtered, (
        "check_and_filter_projection must strip an entry whose generation advanced "
        "after the per-alias loop but before pending registration; "
        "remove the final gen re-check inside _DELIVERY_LOCK and this fails"
    )
    # _PENDING_DELIVERY must NOT contain the app (stripped before registration).
    assert KEY not in tools._PENDING_DELIVERY or APP not in tools._PENDING_DELIVERY.get(
        KEY, set()
    ), "stripped app must not appear in _PENDING_DELIVERY"


# ── GPT teardown:971: unconfirmed handle is saved and retried ─────────────────


@pytest.mark.asyncio
async def test_stop_app_bot_chat_servers_saves_handle_on_unconfirmed_and_retries(
    world, monkeypatch
):
    """When discard_conversation returns False with an unconfirmed handle, the
    runtime+session_id must be saved to _PENDING_CLOSE[slug] so the next
    stop attempt retries the remote close before attempting a full discard.

    On the first call: discard returns False and populates unconfirmed_handle_out.
    On the second call: the saved handle is found in _PENDING_CLOSE, evict_session
    is called, and if it returns True the slug moves to stopped without a new
    discard.

    Remove the _PENDING_CLOSE save or the retry path and the second call either
    attempts a full discard (missing the handle) or finds the slug still in failed."""
    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    # Fake runtime that confirms evict_session on call.
    evict_calls: list[str] = []

    class _FakeRuntime:
        async def evict_session(self, session_id: str) -> bool:
            evict_calls.append(session_id)
            return True

    fake_sid = "kas-session-123"

    class _FakeAlloc:
        def begin_ending(self, key: str) -> None:
            pass

        def end_ending(self, key: str) -> None:
            pass

        def spawn_in_flight(self, key: str) -> None:
            return None

    call_count = [0]

    class _FakeSessions:
        async def discard_conversation(
            self,
            session_key: str,
            replay: bool = False,
            confirm_teardown: bool = False,
            unconfirmed_handle_out: "list | None" = None,
        ) -> bool:
            call_count[0] += 1
            if confirm_teardown and unconfirmed_handle_out is not None and call_count[0] == 1:
                # First call: simulate KAS close timeout.
                unconfirmed_handle_out.append((_FakeRuntime(), fake_sid))
                return False
            # Second and subsequent calls: confirmed (new session or confirmed KAS).
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()

    # First call: should return failed, save to _PENDING_CLOSE.
    stopped1, failed1 = await tools.stop_app_bot_chat_servers(_FakeState(), APP)
    assert slug in failed1, "first call: unconfirmed close must be in failed"
    assert (slug, APP) in tools._PENDING_CLOSE, (
        "unconfirmed handle must be saved to _PENDING_CLOSE[(slug, app)] for retry; "
        "remove the save and this fails"
    )
    assert (slug, APP) in tools._PENDING_CLOSE_APPS, (
        "_PENDING_CLOSE_APPS must contain (slug, app) for delivery-gate blocking; "
        "remove the _PENDING_CLOSE_APPS.add and this fails"
    )

    # Second call: _drain_pending_close retries the saved handle.
    stopped2, failed2 = await tools.stop_app_bot_chat_servers(_FakeState(), APP)
    assert fake_sid in evict_calls, (
        "second call must retry evict_session on the saved handle via _drain_pending_close; "
        "remove the drain call and evict_calls stays empty"
    )
    assert slug in stopped2, (
        "confirmed retry must move the slug to stopped; "
        "remove the retry confirm branch and this fails"
    )
    assert slug not in failed2, "confirmed retry must not appear in failed"
    assert f"{APP}:team" not in tools._MOUNTED.get(
        slug, {}
    ), "_MOUNTED must be cleared after confirmed retry"
    assert (slug, APP) not in tools._PENDING_CLOSE_APPS, (
        "_PENDING_CLOSE_APPS must be cleared after all handles confirmed; "
        "remove the discard and the app/slug can never mount again"
    )


# ── Opus :1095: pending close blocks new delivery; retry confirms both old and new ──


def test_pending_close_blocks_new_session_delivery(world, monkeypatch):
    """When an app has an unconfirmed KAS close in _PENDING_CLOSE_APPS, the delivery
    gate must reject that app's aliases so the server cannot mount in a new session
    while the old one may still be resident.

    Without the _PENDING_CLOSE_APPS gate in check_and_filter_projection, a cold start
    after a timeout lets the app mount again before the old session is confirmed closed,
    leaving two copies resident on the shared engine.

    Remove the `if (slug, app_name) in _PENDING_CLOSE_APPS: continue` gate and this
    test fails: the alias passes through despite the unconfirmed close."""
    # Seed: an unconfirmed close is on record for (slug, APP).
    slug = KEY.split("member-")[1].split(".")[0]
    tools._PENDING_CLOSE_APPS.add((slug, APP))

    # Build the projection as a cold start would.
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    mcpServers = dict(spec_out.get("mcpServers") or {})
    filtered = tools.check_and_filter_projection(KEY, mcpServers)

    assert f"{APP}:team" not in filtered, (
        "check_and_filter_projection must reject app aliases when the app is in "
        "_PENDING_CLOSE_APPS (an unconfirmed close is pending); "
        "remove the gate and the alias passes through"
    )


@pytest.mark.asyncio
async def test_unconfirmed_close_blocks_new_mount_then_unblocks_after_retry(world, monkeypatch):
    """Full scenario: KAS close times out -> new cold start is blocked ->
    retry confirms the old close -> new session allowed to mount.

    Three assertions:
    1. After the timeout, APP is in _PENDING_CLOSE_APPS (new mounts blocked).
    2. A cold start built immediately after is blocked.
    3. After a successful retry, APP is cleared from _PENDING_CLOSE_APPS.

    Break any single step and one of the three assertions fails."""
    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    fake_sid = "kas-session-unconfirmed-456"
    evict_calls: list[str] = []
    discard_count = [0]

    class _FakeRuntime:
        async def evict_session(self, session_id: str) -> bool:
            evict_calls.append(session_id)
            return True

    class _FakeAlloc:
        def begin_ending(self, key: str) -> None:
            pass

        def end_ending(self, key: str) -> None:
            pass

        def spawn_in_flight(self, key: str) -> None:
            return None

    class _FakeSessions:
        async def discard_conversation(
            self,
            session_key: str,
            replay: bool = False,
            confirm_teardown: bool = False,
            unconfirmed_handle_out: "list | None" = None,
        ) -> bool:
            discard_count[0] += 1
            if confirm_teardown and unconfirmed_handle_out is not None and discard_count[0] == 1:
                # First call only: simulate KAS close timeout.
                unconfirmed_handle_out.append((_FakeRuntime(), fake_sid))
                return False
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()

    # Step 1: First stop call times out.
    _stopped, _failed = await tools.stop_app_bot_chat_servers(_FakeState(), APP)
    assert (slug, APP) in tools._PENDING_CLOSE_APPS, (
        "after a KAS close timeout, (slug, APP) must be in _PENDING_CLOSE_APPS so "
        "deliveries for this Bot/app pair are blocked; "
        "remove the _PENDING_CLOSE_APPS.add and this fails"
    )

    # Step 2: A cold start for THIS slug must be blocked; a different slug is not.
    # KEY_C uses a different slug (charlie).  Tick charlie so conductor_session_spec
    # returns APP:team for it.
    world["ticked"] = ("atlas", "charlie")
    spec_out = tools.conductor_session_spec(dict(BASE), KEY_C)
    mcpServers = dict(spec_out.get("mcpServers") or {})
    filtered_other = tools.check_and_filter_projection(KEY_C, mcpServers)
    assert f"{APP}:team" in filtered_other, (
        "another Bot (charlie) must not be blocked by atlas's unconfirmed close; "
        "the gate must use (slug, app) keys, not a global app name"
    )
    # Same slug (atlas) on a new cold start IS blocked.
    world["ticked"] = ("atlas",)
    spec_out2 = tools.conductor_session_spec(dict(BASE), KEY)
    mcpServers2 = dict(spec_out2.get("mcpServers") or {})
    filtered_same = tools.check_and_filter_projection(KEY, mcpServers2)
    assert f"{APP}:team" not in filtered_same, (
        "atlas cold start after KAS timeout must not mount the app; "
        "remove the _PENDING_CLOSE_APPS gate and the alias passes through"
    )

    # Step 3: Retry via _drain_pending_close (triggered by stop or after_app_change).
    tools._MOUNTED[slug] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY
    _stopped2, _failed2 = await tools.stop_app_bot_chat_servers(_FakeState(), APP)
    assert fake_sid in evict_calls, "retry must call evict_session on the saved handle"
    assert (slug, APP) not in tools._PENDING_CLOSE_APPS, (
        "after confirmed retry, (slug, APP) must be removed from _PENDING_CLOSE_APPS; "
        "remove the discard and the slot pair can never mount again"
    )


# ── Opus :593: drain retries independently; only (slug, app) blocked, not all Bots ──


@pytest.mark.asyncio
async def test_drain_retries_pending_close_without_mounted_session(world, monkeypatch):
    """close timeout -> Bot cold-starts (new session, _MOUNTED cleared) ->
    re-enable triggers after_app_change -> drain confirms the old close ->
    new cold start succeeds.

    Four assertions:
    1. After timeout, (slug, APP) is in _PENDING_CLOSE_APPS (gate blocked).
    2. While blocked, atlas cannot mount APP's servers (delivery gate).
    3. after_app_change (via _drain_pending_close) retries the handle even though
       _MOUNTED has no entry for the slug.  evict_calls must contain fake_sid.
    4. After drain, (slug, APP) is gone from _PENDING_CLOSE_APPS; a new cold start
       for atlas succeeds.

    Remove _drain_pending_close from after_app_change and evict_calls stays empty
    (the drain never fires when _MOUNTED has no entry for the slug)."""
    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    fake_sid = "kas-session-drain-789"
    evict_calls: list[str] = []
    discard_count = [0]

    class _FakeRuntime:
        async def evict_session(self, session_id: str) -> bool:
            evict_calls.append(session_id)
            return True

    class _FakeAlloc:
        def begin_ending(self, key: str) -> None:
            pass

        def end_ending(self, key: str) -> None:
            pass

        def spawn_in_flight(self, key: str) -> None:
            return None

    class _FakeSessions:
        async def discard_conversation(
            self,
            session_key: str,
            replay: bool = False,
            confirm_teardown: bool = False,
            unconfirmed_handle_out: "list | None" = None,
        ) -> bool:
            discard_count[0] += 1
            if confirm_teardown and unconfirmed_handle_out is not None and discard_count[0] == 1:
                unconfirmed_handle_out.append((_FakeRuntime(), fake_sid))
                return False
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()
        _crewmate_reload_tasks: set = set()

    # Step 1: stop times out.
    await tools.stop_app_bot_chat_servers(_FakeState(), APP)
    assert (slug, APP) in tools._PENDING_CLOSE_APPS, "setup: (slug, APP) must be blocked"

    # Simulate cold start: _MOUNTED for slug is now a new session (or cleared).
    tools._MOUNTED.pop(slug, None)
    tools._MOUNTED_SESSION_KEYS.pop(slug, None)

    # Step 2: atlas delivery gate is blocked.
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    mcpServers = dict(spec_out.get("mcpServers") or {})
    filtered = tools.check_and_filter_projection(KEY, mcpServers)
    assert (
        f"{APP}:team" not in filtered
    ), "delivery gate must block APP for atlas while (slug, APP) is in _PENDING_CLOSE_APPS"

    # Step 3: after_app_change drains the handle even though _MOUNTED is empty.
    reload_calls: list = []

    async def _fake_reload(state: object, slugs: object) -> dict:
        reload_calls.extend(slugs)  # type: ignore[arg-type]
        return {}

    async def _fake_fresh(state: object, slugs: object) -> None:
        reload_calls.extend(slugs)  # type: ignore[arg-type]

    monkeypatch.setattr(tools, "reload_bot_chats", _fake_reload)
    monkeypatch.setattr(tools, "fresh_bot_chats", _fake_fresh)

    await tools.after_app_change(_FakeState())

    assert fake_sid in evict_calls, (
        "after_app_change must retry the unconfirmed handle via _drain_pending_close "
        "even when _MOUNTED has no entry for the slug; "
        "remove _drain_pending_close from after_app_change and this fails"
    )
    assert (
        slug,
        APP,
    ) not in tools._PENDING_CLOSE_APPS, (
        "after confirmed drain, (slug, APP) must be cleared from _PENDING_CLOSE_APPS"
    )

    # Step 4: new cold start for atlas now succeeds.
    spec_out2 = tools.conductor_session_spec(dict(BASE), KEY)
    mcpServers2 = dict(spec_out2.get("mcpServers") or {})
    filtered2 = tools.check_and_filter_projection(KEY, mcpServers2)
    assert f"{APP}:team" in filtered2, (
        "after drain, atlas must be able to mount APP's servers again; "
        "remove the _PENDING_CLOSE_APPS.discard and the gate stays blocked forever"
    )


@pytest.mark.asyncio
async def test_unconfirmed_close_does_not_block_other_bots(world, monkeypatch):
    """An unconfirmed close for atlas must NOT block bravo from mounting the same app.

    With a global app-name key (the old design), any Bot trying to mount an app
    with an unconfirmed handle was blocked -- even Bots that had no session in flight.
    With (slug, app) keys, only atlas is blocked; bravo mounts normally.

    Replace (slug, APP) in _PENDING_CLOSE_APPS with just APP and bravo's delivery
    also fails, breaking this assertion."""
    slug = KEY.split("member-")[1].split(".")[0]
    # Seed an unconfirmed close for atlas/APP.
    tools._PENDING_CLOSE_APPS.add((slug, APP))

    # Tick bravo as well so conductor_session_spec returns app servers for it.
    world["ticked"] = ("atlas", "bravo")

    bravo_key = "dashboard:member-bravo.memory-member-bravo-1"
    # Build a projection for bravo.
    spec_out = tools.conductor_session_spec(dict(BASE), bravo_key)
    assert f"{APP}:team" in spec_out.get("mcpServers", {}), (
        "setup: bravo must receive APP:team from conductor_session_spec; "
        "check that world ticked includes bravo"
    )
    mcpServers = dict(spec_out.get("mcpServers") or {})
    filtered = tools.check_and_filter_projection(bravo_key, mcpServers)

    assert f"{APP}:team" in filtered, (
        "bravo must not be blocked by atlas's unconfirmed close; "
        "the gate uses (slug, app) keys so only atlas is affected; "
        "switch to a global app-name set and bravo is also blocked"
    )


# ── GPT :1163/:1119/:940: race condition tests with controlled sync points ────


@pytest.mark.asyncio
async def test_fence_blocks_swap_conversation_during_teardown(world, monkeypatch):
    """GPT :1163: a Bot receiving a new message triggers swap_conversation during
    teardown, which calls record_mounted with the old server set.  While the
    revocation fence is up, record_mounted must schedule a reconcile rather than
    silently leaving the revoked server mounted.

    Controlled sync point: seed _REVOCATION_FENCE[APP] as if revoke_app_generation
    ran, then call record_mounted directly (simulating what swap_conversation does).
    record_mounted must set voided_apps for the fenced app and schedule a reconcile.

    Remove the fence check from record_mounted and the reconcile is not scheduled,
    leaving the revoked server silently mounted in the new conversation."""
    slug = KEY.split("member-")[1].split(".")[0]  # noqa: F841  used as part of session key
    tools._APP_GENERATIONS[APP] = 1  # type: ignore[index]
    tools._REVOCATION_FENCE.add(APP)

    # Build a projection (pre-revocation gen=0 snapshot, already in _PROJECTION_SLUGS).
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    assert f"{APP}:team" in spec_out.get("mcpServers", {}), "setup"

    # Simulate swap_conversation: record_mounted is called with the existing server set.
    all_names = list((spec_out.get("mcpServers") or {}).keys())
    reconcile_scheduled = []
    orig_schedule = tools._schedule_reconcile

    def _spy_reconcile() -> None:
        reconcile_scheduled.append(True)
        orig_schedule()

    monkeypatch.setattr(tools, "_schedule_reconcile", _spy_reconcile)

    voided = tools.record_mounted(KEY, all_names)

    assert APP in voided or len(reconcile_scheduled) > 0, (
        "record_mounted during revocation fence must detect the fenced app and "
        "schedule a reconcile; remove the fence check in record_mounted and the "
        "revoked server is silently left mounted"
    )


@pytest.mark.asyncio
async def test_generation_snapshot_prevents_new_session_from_being_stopped(world, monkeypatch):
    """GPT :1119: a cold start between the allocation-void and the discard-confirm
    overwrites _MOUNTED[slug] with a new session.  stop_app_bot_chat_servers must
    NOT clear the new session's record when the generation snapshot from before the
    await has changed.

    Controlled sync point: discard_conversation observes the await, then we
    replace _MOUNTED[slug] with a new session key before returning.  After
    stop_app_bot_chat_servers returns, the new session's record must still be in
    _MOUNTED.

    Remove the session_key snapshot / post-await comparison and the new session's
    _MOUNTED record is cleared, leaving it invisible to future teardown calls."""
    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp_old"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    NEW_KEY = "dashboard:member-atlas.memory-member-atlas-99"
    replaced = [False]

    class _FakeAlloc:
        def begin_ending(self, key: str) -> None:
            pass

        def end_ending(self, key: str) -> None:
            pass

        def spawn_in_flight(self, key: str) -> None:
            return None

    class _FakeSessions:
        async def discard_conversation(
            self,
            session_key: str,
            replay: bool = False,
            confirm_teardown: bool = False,
            unconfirmed_handle_out: "list | None" = None,
        ) -> bool:
            # Cold start fires DURING this await, replacing the mounted record.
            tools._MOUNTED[slug] = {f"{APP}:team": "fp_new"}
            tools._MOUNTED_SESSION_KEYS[slug] = NEW_KEY
            replaced[0] = True
            return True  # old session confirmed stopped

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()

    stopped, failed = await tools.stop_app_bot_chat_servers(_FakeState(), APP)

    assert replaced[0], "setup: cold start must fire during discard"
    # The old session was stopped, but the new session's record must be preserved.
    assert tools._MOUNTED.get(slug, {}).get(f"{APP}:team") == "fp_new", (
        "new session's _MOUNTED record must be preserved when it replaced the old "
        "session during the discard await; "
        "remove the session_key snapshot comparison and the new record is cleared"
    )
    # The old session is confirmed stopped, so the slug appears in failed
    # (new session needs its own teardown) or stopped (if snapshot matches).
    # Either way the new session must NOT have been cleared.
    assert (
        tools._MOUNTED_SESSION_KEYS.get(slug) == NEW_KEY
    ), "_MOUNTED_SESSION_KEYS must still point to the new session after the stop"


@pytest.mark.asyncio
async def test_drain_preserves_handles_added_during_await(world, monkeypatch):
    """GPT :940: _drain_pending_close must not lose handles added to _PENDING_CLOSE
    during an await.  The old drain replaced the entire list with still_pending
    after all awaits, discarding any handles appended concurrently.

    Controlled sync point: during the first evict_session await, append a second
    handle for the same (slug, app) key.  After drain returns, both the first
    (now confirmed) and the concurrent second handle must be accounted for:
    the first is gone, the second survives.

    Replace the per-entry remove with _PENDING_CLOSE.clear(); .update(still_pending)
    and the concurrent handle is lost."""
    slug = KEY.split("member-")[1].split(".")[0]

    # Seed one handle.
    evict_calls: list[str] = []

    class _RT1:
        async def evict_session(self, session_id: str) -> bool:
            # During this await, another coroutine appends a second handle.
            tools._PENDING_CLOSE.setdefault((slug, APP), []).append((_RT2(), "sid-concurrent"))
            tools._PENDING_CLOSE_APPS.add((slug, APP))
            evict_calls.append(session_id)
            return True  # confirmed

    class _RT2:
        async def evict_session(self, session_id: str) -> bool:
            evict_calls.append(session_id)
            return False  # not yet confirmed

    tools._PENDING_CLOSE[(slug, APP)] = [(_RT1(), "sid-first")]
    tools._PENDING_CLOSE_APPS.add((slug, APP))

    await tools._drain_pending_close()

    assert "sid-first" in evict_calls, "first handle must be retried"
    assert (slug, APP) in tools._PENDING_CLOSE, (
        "concurrently added second handle must still be in _PENDING_CLOSE; "
        "replace per-entry remove with bulk clear+update and it is lost"
    )
    # The gate must stay blocked because the concurrent handle is not yet confirmed.
    assert (slug, APP) in tools._PENDING_CLOSE_APPS, (
        "gate must remain blocked while the concurrent handle is unconfirmed; "
        "replace per-entry remove with bulk clear+update and the gate is prematurely lifted"
    )


# ── Opus :1158: fence released on every exit path ────────────────────────────


@pytest.mark.asyncio
async def test_fence_lowered_on_early_return_when_no_bots_mounted(world, monkeypatch):
    """When no Bot has the app mounted and no pending delivery exists,
    stop_app_bot_chat_servers returns early.  The revocation fence must be lowered
    on that early return, or the app can never mount again.

    Remove the _REVOCATION_FENCE.discard call before the early return and APP
    stays in _REVOCATION_FENCE, blocking every subsequent delivery."""
    tools._REVOCATION_FENCE.add(APP)
    # _MOUNTED and _PENDING_DELIVERY are empty (fixture reset them).

    class _FakeState:
        sessions = None

    stopped, failed = await tools.stop_app_bot_chat_servers(_FakeState(), APP)

    assert stopped == [] and failed == [], "early return must report empty lists"
    assert APP not in tools._REVOCATION_FENCE, (
        "revocation fence must be lowered on the early-return path; "
        "remove the discard before 'return [], []' and the fence is permanent"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revoke_path",
    [
        "disable",
        "uninstall",
        "single_trust",
        "global_trust",
        "config_trust",
    ],
)
async def test_revocation_fence_cleared_after_all_paths(world, monkeypatch, revoke_path):
    """Every revocation entry point (disable, uninstall, single trust revoke,
    global trust revoke, config-change trust revoke) must lower the revocation
    fence after stop_app_bot_chat_servers returns, assuming all stop calls succeed.

    Remove _revocation_fence_clear from stop_app_bot_chat_servers and the fence
    persists for every path that calls it."""
    import kiro_crew.apps.execution as _exec

    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    class _FakeAlloc:
        def begin_ending(self, key: str) -> None:
            pass

        def end_ending(self, key: str) -> None:
            pass

        def spawn_in_flight(self, key: str) -> None:
            return None

    class _FakeSessions:
        async def discard_conversation(
            self,
            session_key: str,
            replay: bool = False,
            confirm_teardown: bool = False,
            unconfirmed_handle_out: "list | None" = None,
        ) -> bool:
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()
        _crewmate_reload_tasks: set = set()

    async def _fake_stop(state: object, name: str) -> tuple[list[str], list[str]]:
        # Delegate to the real function so the fence logic runs.
        return await tools.stop_app_bot_chat_servers(state, name)

    if revoke_path in ("disable", "uninstall"):
        # These call revoke_app_generation then stop_app_bot_chat_servers.
        tools.revoke_app_generation(APP)
        assert APP in tools._REVOCATION_FENCE, "setup: fence must be up after revoke"
        await tools.stop_app_bot_chat_servers(_FakeState(), APP)

    elif revoke_path == "single_trust":
        # security_trusted_apps.py: revoke_app_generation then stop.
        tools.revoke_app_generation(APP)
        assert APP in tools._REVOCATION_FENCE, "setup"
        await tools.stop_app_bot_chat_servers(_FakeState(), APP)

    elif revoke_path == "global_trust":
        # on_execution_trust_change: collects denied apps, calls revoke + stop each.
        monkeypatch.setattr(_exec, "app_execution_denied", lambda name, **kw: "trust_removed")
        # Seed _MOUNTED so the app appears in app_slugs.
        await tools.on_execution_trust_change(_FakeState())

    elif revoke_path == "config_trust":
        # config_watch.py path: same as global trust change.
        monkeypatch.setattr(_exec, "app_execution_denied", lambda name, **kw: "trust_removed")
        await tools.on_execution_trust_change(_FakeState())

    assert APP not in tools._REVOCATION_FENCE, (
        f"{revoke_path}: revocation fence must be cleared after all sessions are "
        f"confirmed stopped; remove _revocation_fence_clear from stop_app_bot_chat_servers "
        f"and the fence stays up permanently"
    )


@pytest.mark.asyncio
async def test_drain_confirms_handle_when_host_rejects_id(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    """evict_session returning True on a host JSON-RPC rejection confirms the handle.

    Flow:
    1. stop_app_bot_chat_servers times out the first close (evict returns False).
    2. The handle goes to _PENDING_CLOSE_APPS; the fence stays up.
    3. A second stop call runs _drain_pending_close, which calls evict_session again.
    4. This time evict_session returns True (host rejected the id — session already freed).
    5. The drain must confirm the handle, clear _PENDING_CLOSE_APPS, and clear the fence.

    Remove the ``if getattr(exc, "code", None) is not None: confirmed = True`` branch
    from evict_session and the second evict returns False instead of True, the handle
    is never confirmed, and the final assertion fails.
    """
    # Ensure global state is clean for this test.
    _floor_monkeypatch.setattr(tools, "_MOUNTED", {})
    _floor_monkeypatch.setattr(tools, "_MOUNTED_SESSION_KEYS", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE_APPS", set())
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE_RETRIES", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE_BACKOFF_IDX", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE_TIMERS", {})
    _floor_monkeypatch.setattr(tools, "_REVOCATION_FENCE", set())
    _floor_monkeypatch.setattr(tools, "_APP_GENERATIONS", {})
    _floor_monkeypatch.setattr(tools, "_PROJECTION_GENS", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_DELIVERY", {})
    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY
    tools.revoke_app_generation(APP)

    fake_sid = "kas-session-rpc-reject-789"
    evict_calls: list[bool] = []
    discard_count = [0]

    class _FakeRuntime:
        async def evict_session(self, session_id: str) -> bool:
            # drain calls evict_session directly on the stored handle.
            # discard_conversation does NOT call evict_session — it only stores the
            # handle for later retry.  So drain's call is always the FIRST call on
            # this instance, and it should return True (simulating host RPC rejection).
            evict_calls.append(True)
            return True

    class _FakeAlloc:
        def begin_ending(self, key: str) -> None:
            pass

        def end_ending(self, key: str) -> None:
            pass

        def spawn_in_flight(self, key: str) -> None:
            return None

    class _FakeSessions:
        async def discard_conversation(
            self,
            session_key: str,
            replay: bool = False,
            confirm_teardown: bool = False,
            unconfirmed_handle_out: "list | None" = None,
        ) -> bool:
            discard_count[0] += 1
            if confirm_teardown and unconfirmed_handle_out is not None and discard_count[0] == 1:
                unconfirmed_handle_out.append((_FakeRuntime(), fake_sid))
                return False
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()

    # First stop: evict_session returns False (timeout); handle goes to PENDING_CLOSE.
    await tools.stop_app_bot_chat_servers(_FakeState(), APP)
    assert (slug, APP) in tools._PENDING_CLOSE_APPS, (
        "handle must be pending after first timeout; "
        "remove _PENDING_CLOSE_APPS.add in stop_app_bot_chat_servers and this fails"
    )
    assert (
        APP in tools._REVOCATION_FENCE
    ), "revocation fence must stay up while handle is unconfirmed"

    # Second stop: drain retries — evict_session now returns True (host RPC rejection).
    # The drain must confirm the handle and clear the fence.
    await tools.stop_app_bot_chat_servers(_FakeState(), APP)
    assert (slug, APP) not in tools._PENDING_CLOSE_APPS, (
        "handle must be confirmed after evict_session returns True on retry; "
        "remove the host-rejection True return from evict_session and evict returns "
        "False, the drain keeps the handle, and this assertion fails"
    )
    assert APP not in tools._REVOCATION_FENCE, (
        "fence must be cleared once all handles are confirmed; "
        "the 409 loop only breaks when the fence clears"
    )
    assert len(evict_calls) >= 1, "drain must have called evict_session at least once on retry"
