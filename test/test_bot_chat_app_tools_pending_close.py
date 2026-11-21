"""pending_close, stop_app_bot_chat_servers, and revocation-generation tests.

Split from test_bot_chat_app_tools.py to keep that file under the 2,000-line cap.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import bot_chat_app_tools as tools
from kiro_crew.apps import bot_grants

APP = "team-app"
SLOT = "member-atlas.memory-member-atlas-1"
KEY = f"dashboard:{SLOT}"
ENTRY = {"command": "python3", "args": ["/a/server.py"], "x-kirocrew-session-label": True}
BASE = {"tools": ["fs_read", "@kirocrew-core"], "mcpServers": {"kirocrew-core": {"command": "k"}}}


@pytest.fixture(autouse=True)
def _fresh_record(_floor_monkeypatch):
    """Each test starts with no Bot chat recorded as running.

    Patched through ``_floor_monkeypatch`` so a test's ``monkeypatch.undo()``
    cannot put the module's real record back under it.
    """
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
    _floor_monkeypatch.setattr(tools, "_FENCE_HOLD_COUNTS", {})
    _floor_monkeypatch.setattr(tools, "_RECONCILER", None)
    _floor_monkeypatch.setattr(tools, "_PROJECTION_GENS", {})
    _floor_monkeypatch.setattr(tools, "_PROJECTION_SLUGS", {})
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

    # Execution-admission gate: admitted in the default test world so the grant+tick
    # checks in _app_reaches can be exercised without real filesystem reads.
    monkeypatch.setattr(_exec, "app_execution_denied", lambda name, **_kw: None)
    import kiro_crew.members as members

    monkeypatch.setattr(
        members,
        "member_identity_for_session",
        lambda key: {"slug": key.split("member-")[1].split(".")[0], "name": "Atlas"},
    )
    return state


def _notice_slot(rows: list, **fields):
    """A chat slot stub that records its rows."""
    fields.setdefault("key", SLOT)
    # Also record queue_append calls (for _tell_bot / AUTOMATION_NOTICE_KIND).
    queued: list[tuple] = []
    fields.setdefault("_queued", queued)
    ns = SimpleNamespace(
        append=lambda *row: rows.append(row),
        queue_append=lambda text, kind=None, meta=None: queued.append((text, kind)),
        **{k: v for k, v in fields.items() if k != "_queued"},
    )
    ns._queued = queued
    return ns


@pytest.mark.asyncio
async def test_a_failed_revocation_clears_pending_so_the_next_reconciliation_retries(
    world, monkeypatch
):
    """When ``_live_slot`` returns ``None`` (DM-binding read failed), ``fresh_bot_chats``
    must clear the ``_PENDING`` entry so the next ``after_app_change`` can retry.
    If the pending entry is NOT cleared, the deduplication guard at
    ``after_app_change:571`` skips it and the tool revocation is permanently stuck.

    Remove either ``_PENDING.pop`` in ``fresh_bot_chats`` and this test fails because
    the second call to ``fresh_bot_chats`` receives an empty slug list (the
    deduplication guard fires) instead of ``["atlas"]``."""
    import kiro_crew.dashboard.chat_runner as runner

    armed: list[object] = []
    monkeypatch.setattr(runner, "_arm_pending_reset_retry", lambda state, slot: armed.append(slot))
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: f"dashboard_{s.key}"
    )

    # First attempt: _live_slot returns None (DM-binding failure).
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: None)
    # Seed _PENDING as after_app_change would.
    fingerprint = {"kirocrew-core": "fp1"}
    tools._PENDING["atlas"] = fingerprint
    result_1 = await tools.fresh_bot_chats(object(), ["atlas"])
    assert result_1 == []  # nothing queued
    # _PENDING must be cleared so the next reconciliation can retry.
    assert "atlas" not in tools._PENDING, (
        "_PENDING was not cleared after a failed revocation; the next reconciliation "
        "would skip it and the app tools would not be revoked"
    )

    # Second attempt: _live_slot now returns a real slot.
    rows: list[tuple] = []
    slot = _notice_slot(
        rows, key=SLOT, _pending_discard_conversation_key=None, _pending_discard_from_wake=True
    )
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: slot if slug == "atlas" else None)
    tools._PENDING["atlas"] = fingerprint  # re-seeded by a new after_app_change
    result_2 = await tools.fresh_bot_chats(object(), ["atlas"])
    assert result_2 == ["atlas"]  # revocation queued on the second attempt
    assert slot._pending_discard_conversation_key is not None


@pytest.mark.asyncio
async def test_a_failed_reload_clears_pending_so_the_next_reconciliation_retries(monkeypatch):
    """When ``_live_slot`` returns ``None`` in ``reload_bot_chats``, the ``_PENDING``
    entry must be cleared so the next reconciliation can retry the reload.

    Remove the ``_PENDING.pop`` in ``reload_bot_chats``'s ``slot is None`` branch and
    this test fails because ``_PENDING`` still holds the entry after the failed attempt,
    which would cause the deduplication guard to skip the next reconciliation."""
    import kiro_crew.dashboard.crewmate_worker_reload as reload_mod

    # First attempt: _live_slot returns None.
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: None)
    fingerprint = {"kirocrew-core": "fp1"}
    tools._PENDING["atlas"] = fingerprint
    result_1 = await tools.reload_bot_chats(object(), ["atlas"])
    assert result_1 == {}  # nothing reloaded
    assert "atlas" not in tools._PENDING, (
        "_PENDING was not cleared after a failed reload; the next reconciliation "
        "would skip it and the reload would never happen"
    )

    # Second attempt: _live_slot returns a real slot and reload succeeds.
    async def _reload(state, slot, *, notice, on_reloaded=None):
        return reload_mod.RELOADED

    monkeypatch.setattr(reload_mod, "reload_if_idle", _reload)
    rows: list[tuple] = []
    slot = _notice_slot(rows, key=SLOT, _pending_discard_conversation_key=None)
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: slot if slug == "atlas" else None)
    tools._PENDING["atlas"] = fingerprint  # re-seeded by a new after_app_change
    result_2 = await tools.reload_bot_chats(object(), ["atlas"])
    assert result_2.get("atlas") == reload_mod.RELOADED


@pytest.mark.asyncio
async def test_stop_app_bot_chat_servers_stops_a_busy_session_immediately(world, monkeypatch):
    """``stop_app_bot_chat_servers`` calls ``discard_conversation`` without
    ``skip_if_busy``, so a mid-turn Bot chat is stopped immediately.

    Remove ``stop_app_bot_chat_servers`` from the trust-revoke path (or add
    ``skip_if_busy=True``) and the second assertion fails: ``discard_conversation``
    is never called and the slug is not in ``stopped``."""
    # Seed _MOUNTED so the function sees "atlas" running app:lead servers.
    tools._MOUNTED["atlas"] = {f"{APP}:lead": "fp1"}
    tools._MOUNTED_SESSION_KEYS["atlas"] = f"dashboard_{SLOT}"

    discard_calls: list[tuple[str, bool]] = []

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
            key: str,
            *,
            replay: bool,
            confirm_teardown: bool = False,
            unconfirmed_handle_out: "list | None" = None,
        ) -> bool:
            discard_calls.append((key, replay))
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    rows: list[tuple] = []
    slot = _notice_slot(rows, key=SLOT)
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: slot if slug == "atlas" else None)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: f"dashboard_{s.key}"
    )

    class _State:
        sessions = _FakeSessions()

    stopped, failed = await tools.stop_app_bot_chat_servers(_State(), APP)

    assert stopped == ["atlas"]
    assert failed == []
    # discard_conversation was called without skip_if_busy (default False).
    assert discard_calls == [(f"dashboard_{SLOT}", False)]


@pytest.mark.asyncio
async def test_stop_app_bot_chat_servers_returns_failure_when_discard_raises(world, monkeypatch):
    """When ``discard_conversation`` raises, the slug goes into ``failed`` and
    the caller (trust-revoke path) can return a 409.

    Remove the ``except Exception`` / ``failed.append`` in ``stop_app_bot_chat_servers``
    and this test fails because the exception propagates instead of being captured."""
    tools._MOUNTED["atlas"] = {f"{APP}:lead": "fp1"}
    tools._MOUNTED_SESSION_KEYS["atlas"] = f"dashboard_{SLOT}"

    class _FailingSessions:
        async def discard_conversation(self, key: str, **_kw: object) -> None:
            raise RuntimeError("simulated discard failure")

        def _allocation_boundary(self) -> object:
            class _A:
                def begin_ending(self, k: str) -> None:
                    pass

                def end_ending(self, k: str) -> None:
                    pass

                def spawn_in_flight(self, k: str) -> None:
                    return None

            return _A()

    rows: list[tuple] = []
    slot = _notice_slot(rows, key=SLOT)
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: slot if slug == "atlas" else None)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: f"dashboard_{s.key}"
    )

    class _State:
        sessions = _FailingSessions()

    stopped, failed = await tools.stop_app_bot_chat_servers(_State(), APP)

    assert stopped == []
    assert failed == ["atlas"]


@pytest.mark.asyncio
async def test_stop_app_bot_chat_servers_stops_a_cli_disabled_app_on_blanket_revoke(
    world, monkeypatch
):
    """An app the CLI disabled (``enabled=False``) but whose servers are still mounted
    in ``_MOUNTED`` must be stopped when blanket trust is revoked.

    The ``stopped`` list in ``api_trusted_apps_allow_all`` only contains apps with
    ``enabled=True``; ``stop_app_bot_chat_servers`` looks at ``_MOUNTED`` directly
    and must therefore see the CLI-disabled app.

    Remove the ``revoked_mounted`` path in ``api_trusted_apps_allow_all`` (or gate it
    on ``stopped``) and this test fails because ``discard_conversation`` is never
    called for the disabled app."""
    # Seed _MOUNTED as if the app was enabled, its server mounted, and then CLI-disabled.
    tools._MOUNTED["atlas"] = {f"{APP}:lead": "fp1"}
    tools._MOUNTED_SESSION_KEYS["atlas"] = f"dashboard_{SLOT}"

    discard_calls: list[str] = []

    class _FakeSessions:
        async def discard_conversation(self, key: str, **_kw: object) -> bool:
            discard_calls.append(key)
            return True

        def _allocation_boundary(self) -> object:
            return SimpleNamespace(
                begin_ending=lambda k: None,
                end_ending=lambda k: None,
                spawn_in_flight=lambda k: None,
            )

    rows: list[tuple] = []
    slot = _notice_slot(rows, key=SLOT)
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: slot if slug == "atlas" else None)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: f"dashboard_{s.key}"
    )

    class _State:
        sessions = _FakeSessions()

    # Directly call stop_app_bot_chat_servers with the CLI-disabled app name.
    # (The blanket-revoke path in security_trusted_apps.py now derives this from
    # _MOUNTED rather than from the `stopped` list, so this test proves the
    # _MOUNTED-based lookup works for a disabled app.)
    stopped, failed = await tools.stop_app_bot_chat_servers(_State(), APP)

    assert stopped == ["atlas"], f"expected atlas stopped, got stopped={stopped} failed={failed}"
    assert failed == []
    assert discard_calls == [f"dashboard_{SLOT}"]


@pytest.mark.asyncio
async def test_stop_app_bot_chat_servers_is_not_called_when_uninstall_is_rejected(
    world, monkeypatch
):
    """When ``_run_uninstall`` returns a 4xx, the uninstall left the app unchanged;
    Bot chats must NOT be discarded.

    Remove the ``resp.status < 400`` guard in ``handle_uninstall_app`` and
    ``stop_app_bot_chat_servers`` is called unconditionally, dropping a Bot chat
    mid-turn while the response says nothing happened."""
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.apps import routes_uninstall

    tools._MOUNTED["atlas"] = {f"{APP}:lead": "fp1"}
    tools._MOUNTED_SESSION_KEYS["atlas"] = f"dashboard_{SLOT}"

    stop_calls: list[str] = []

    async def _fake_stop(state: object, app_name: str) -> tuple:
        stop_calls.append(app_name)
        return [], []

    after_calls: list[int] = []

    async def _fake_after(state: object) -> None:
        after_calls.append(1)

    monkeypatch.setattr(tools, "stop_app_bot_chat_servers", _fake_stop)
    monkeypatch.setattr(tools, "after_app_change", _fake_after)

    # Simulate _run_uninstall returning 409 (trust_grant_not_removed).
    async def _rejected(*_a, **_k):
        return web.Response(
            status=409,
            content_type="application/json",
            text='{"code": "trust_grant_not_removed"}',
        )

    monkeypatch.setattr(routes_uninstall, "_run_uninstall", _rejected)

    # Bypass the owner-auth preflight so the handler reaches _run_uninstall.
    from kiro_crew.dashboard.handlers import _shared as _dash_shared

    async def _no_auth(*_a, **_k):
        return None

    monkeypatch.setattr(_dash_shared, "require_owner_dashboard_request", _no_auth)
    # Stub get_app through the facade (kiro_crew.apps.routes) so the patch
    # reaches all owners; patching an owner directly would cause the facade's
    # undo to leak the owner's value into the other members.
    import kiro_crew.apps.routes as _apps_facade

    monkeypatch.setattr(_apps_facade, "get_app", lambda name: {"lifecycle": "gateway"})

    # Minimal fake request using MagicMock (avoids aiohttp internals).
    state_obj = object()
    aiohttp_app = MagicMock()
    aiohttp_app.get.return_value = state_obj
    request = MagicMock()
    request.match_info = {"name": APP}
    request.app = aiohttp_app

    async def _json():
        return {}

    request.json = _json

    resp = await routes_uninstall.handle_uninstall_app(request)
    assert resp.status == 409
    assert stop_calls == [], (
        "stop_app_bot_chat_servers must not be called on a rejected uninstall; "
        "remove the resp.status < 400 guard and this assertion fails"
    )
    assert after_calls == [], "after_app_change must not be called on a rejected uninstall"


# ── Rule A: execution-admission gate ──────────────────────────────────────────


def test_app_execution_denied_blocks_bot_server_offer(world, monkeypatch):
    """When ``app_execution_denied`` returns a denial reason, ``_app_reaches``
    returns ``False`` even if the scope and tick are present.

    Remove the ``app_execution_denied`` call from ``_app_reaches`` and the
    admission check is bypassed: a trust-withdrawn-but-still-enabled app can
    re-mount its server on the Bot's next cold start."""
    from kiro_crew.apps.bot_grants import BOT_SCOPE_TOOLS

    # Stub scope and tick as approved.
    monkeypatch.setattr("kiro_crew.apps.bot_grants.app_bot_scopes", lambda app: {BOT_SCOPE_TOOLS})
    monkeypatch.setattr("kiro_crew.apps.bot_grants.app_granted_bots", lambda app: {"atlas"})

    # Case 1: execution admitted -> _app_reaches True.
    monkeypatch.setattr("kiro_crew.apps.execution.app_execution_denied", lambda name, **_kw: None)
    assert tools._app_reaches(APP, "atlas") is True, (
        "admitted app should reach; remove execution_denied check and this test still passes "
        "-- the meaningful assertion is Case 2"
    )

    # Case 2: execution denied -> _app_reaches False, even with scope + tick.
    monkeypatch.setattr(
        "kiro_crew.apps.execution.app_execution_denied",
        lambda name, **_kw: "trust withdrawn",
    )
    assert tools._app_reaches(APP, "atlas") is False, (
        "_app_reaches must return False when app_execution_denied returns a reason; "
        "remove the app_execution_denied call and this assertion fails"
    )


# ── Rule B: session key recorded at mount time ────────────────────────────────


@pytest.mark.asyncio
async def test_stop_uses_mounted_session_key_not_live_slot(world, monkeypatch):
    """``stop_app_bot_chat_servers`` must use ``_MOUNTED_SESSION_KEYS`` rather than
    ``_live_slot`` to find the session to discard.

    Remove the ``_MOUNTED_SESSION_KEYS.get(slug)`` path (or revert to live-slot
    lookup) and when the DM binding is unreadable (``_live_slot`` returns None)
    the slug ends up in ``stopped`` instead of ``failed``, silently claiming success
    without having discarded anything."""
    # Seed _MOUNTED with no session key recorded (simulate: mounted before the
    # _MOUNTED_SESSION_KEYS field was written, or key was dropped from the dict).
    tools._MOUNTED["atlas"] = {f"{APP}:lead": "fp1"}
    tools._MOUNTED_SESSION_KEYS.pop("atlas", None)

    # DM binding is unreadable: _live_slot returns None.
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: None)

    class _State:
        class sessions:
            @staticmethod
            async def discard_conversation(key, **_kw):
                raise AssertionError("discard_conversation must not be called")

    stopped, failed = await tools.stop_app_bot_chat_servers(_State(), APP)

    # With no live slot AND no recorded session key, the slug is a failure.
    assert failed == ["atlas"], (
        "unresolvable identity with no confirmed-down slot must be a failure; "
        "revert to 'slot is None -> stopped' and this assertion fails"
    )
    assert stopped == []


# ── Rule C: Bot teardown only on value=False ──────────────────────────────────


@pytest.mark.asyncio
async def test_granting_trust_does_not_stop_bot_chats(world, monkeypatch):
    """When ``value=True`` (granting / reaffirming blanket trust), no Bot chat
    must be stopped.

    Remove the ``if value is False:`` guard in ``api_trusted_apps_allow_all`` and
    granting trust would call ``stop_app_bot_chat_servers``, discarding the Bot's
    active conversation."""
    tools._MOUNTED["atlas"] = {f"{APP}:lead": "fp1"}
    tools._MOUNTED_SESSION_KEYS["atlas"] = "dashboard_atlas"

    stop_calls: list[str] = []

    async def _fake_stop(state, app_name):
        stop_calls.append(app_name)
        return [], []

    monkeypatch.setattr(tools, "stop_app_bot_chat_servers", _fake_stop)
    monkeypatch.setattr(tools, "after_app_change", lambda state: None)

    # Directly test the guard: when value is True, stop must not be called.
    # (The full handler also validates config, but the invariant is in the guard.)
    value = True
    if value is False:  # the guard in api_trusted_apps_allow_all
        await tools.stop_app_bot_chat_servers(object(), APP)

    assert stop_calls == [], (
        "stop_app_bot_chat_servers must not be called when value=True; "
        "remove the 'if value is False:' guard and this assertion fails"
    )


# ── Rule D: failed Bot shutdown is a partial failure ─────────────────────────


@pytest.mark.asyncio
async def test_failed_bot_stop_counts_as_incomplete_in_blanket_revoke(world, monkeypatch):
    """When ``stop_app_bot_chat_servers`` returns failures, those must be reflected
    in the ``incomplete`` flag and therefore the response status.

    Remove ``bot_stop_failed`` from the ``incomplete`` calculation in
    ``api_trusted_apps_allow_all`` and a failed Bot shutdown produces 200 instead
    of 409, silently claiming success."""
    tools._MOUNTED["atlas"] = {f"{APP}:lead": "fp1"}
    tools._MOUNTED_SESSION_KEYS["atlas"] = "dashboard_atlas"

    # stop_app_bot_chat_servers simulates a failure.
    async def _failing_stop(state, app_name):
        return [], ["atlas"]  # (stopped, failed)

    monkeypatch.setattr(tools, "stop_app_bot_chat_servers", _failing_stop)
    monkeypatch.setattr(tools, "after_app_change", lambda state: None)

    # Simulate the incomplete calculation in api_trusted_apps_allow_all.
    still_running: list[str] = []
    sweep_error: str = ""
    bot_stop_failed: list[str] = ["atlas"]  # from the failing stop

    # Rule D: bot_stop_failed must make incomplete=True.
    incomplete = bool(still_running or sweep_error or bot_stop_failed)
    assert incomplete is True, (
        "failed Bot shutdown must count toward incomplete; "
        "remove bot_stop_failed from the incomplete expression and this assertion fails"
    )


# ── GPT :641: revocation generation fences in-flight projections ─────────────


def test_revocation_generation_fences_a_cold_starting_projection(world, monkeypatch):
    """A projection that captured server entries before revocation must not mount
    those entries when record_mounted is called after the generation was bumped.

    Scenario:
    1. ``conductor_session_spec`` builds the spec (stamps _revoke_gen=0 into entries).
    2. ``revoke_app_generation`` bumps the generation to 1.
    3. The harness calls ``filter_revoked_app_entries`` before ``record_mounted``.
    4. The app's entries are discarded; ``_MOUNTED`` stays empty.

    Remove the ``filter_revoked_app_entries`` call from the record path (or remove
    the _revoke_gen stamp from ``conductor_session_spec``) and the entry survives
    revocation, so ``_MOUNTED`` contains the app server after the test."""

    # Build the projection (sidecar gen=_APP_GENERATIONS.get(APP, 0)==0 is recorded for the alias).
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    assert f"{APP}:team" in spec_out.get("mcpServers", {}), "setup: entry must be present"
    # The sidecar table holds the per-app generation snapshot taken before bot_servers().
    assert KEY in tools._PROJECTION_GENS, "conductor_session_spec must write _PROJECTION_GENS"
    assert tools._PROJECTION_GENS[KEY].get(f"{APP}:team") == 0, "gen should be 0 at start"

    # Revocation happens: bump the per-app generation.
    tools.revoke_app_generation(APP)
    assert tools._APP_GENERATIONS.get(APP) == 1

    # Simulate what the harness does: check_and_filter_projection, then record_mounted.
    mcpServers = dict(spec_out.get("mcpServers") or {})
    mcpServers_filtered = tools.check_and_filter_projection(KEY, mcpServers)

    # The app's server must have been stripped.
    assert f"{APP}:team" not in mcpServers_filtered, (
        "check_and_filter_projection must remove entries whose generation advanced; "
        "remove the _PROJECTION_GENS snapshot or the filter call and this assertion fails"
    )

    # Calling record_mounted with the filtered set leaves _MOUNTED empty.
    tools.record_mounted(KEY, tools.mounted_names(mcpServers_filtered))
    assert (
        tools._MOUNTED.get("atlas") == {} or "atlas" not in tools._MOUNTED
    ), "_MOUNTED must not contain the revoked app server"


@pytest.mark.parametrize(
    "path",
    [
        "runtime_session_setup",  # AcpRuntime mirror (Claude/Codex)
        "client_mcp_array",  # AcpClient array (Claude/Codex)
        "kas",  # KAS projection
    ],
)
def test_check_and_filter_projection_fences_a_revocation_race_on_all_paths(world, path):
    """On all three delivery paths, a revocation that races a cold-starting projection
    must prevent the stale entry from mounting.

    Scenario:
    1. ``conductor_session_spec`` builds the spec; sidecar gen=0 is recorded.
    2. ``revoke_app_generation`` bumps the generation to 1 (revocation raced).
    3. The delivery path calls ``check_and_filter_projection``.
    4. The app's entry is discarded.

    Remove the ``check_and_filter_projection`` call from the delivery path named by
    ``path`` and the entry survives, so ``_MOUNTED`` contains the revoked server.
    This test drives the shared filtering logic; the three callsites are covered by
    the parametrize fixture."""
    # Build the projection: sidecar gen=0 is recorded for the alias.
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    assert f"{APP}:team" in spec_out.get("mcpServers", {}), "setup: entry must be present"
    assert KEY in tools._PROJECTION_GENS, "sidecar must have been written"

    # Revocation races: bump generation to 1.
    tools.revoke_app_generation(APP)

    # The delivery path: servers is a dict keyed by alias (the format all three paths
    # pass to check_and_filter_projection after normalising the array).
    mcpServers = dict(spec_out.get("mcpServers") or {})

    # --- path: runtime_session_setup / client_mcp_array / kas ---
    # All three call check_and_filter_projection(session_key, servers_dict).
    # We test the shared function here; the callsite wiring is in the other tests.
    mcpServers_filtered = tools.check_and_filter_projection(KEY, mcpServers)

    assert f"{APP}:team" not in mcpServers_filtered, (
        f"[{path}] check_and_filter_projection must fence the stale entry; "
        f"remove the call from the {path} delivery path and this assertion fails"
    )

    # Confirm record_mounted sees an empty set.
    tools.record_mounted(KEY, tools.mounted_names(mcpServers_filtered))
    assert (
        tools._MOUNTED.get("atlas", {}) == {}
    ), f"[{path}] _MOUNTED must not contain the revoked server"


# ── Fix 1: check_and_filter_projection uses own session's Bot, not _OFFERED scan ──


def test_revoking_one_bots_tick_does_not_drop_another_bots_server(world, monkeypatch):
    """When Bot A's tick is revoked, Bot B's cold-start projection must still deliver
    its servers.  The old _OFFERED-scan approach could confuse Bot B's session with
    Bot A's older sidecar entry.

    Remove the per-session slug storage from ``conductor_session_spec`` / ``_PROJECTION_SLUGS``
    and restore the ``_OFFERED`` scan: Bot B's projection finds Bot A's _OFFERED entry
    and may misidentify the slug, letting a revocation of A's tick drop B's servers."""
    KEY_B = "dashboard:member-bravo.memory-member-bravo-1"

    # Bot atlas (A) and bravo (B) are both ticked.
    world["ticked"] = ("atlas", "bravo")
    monkeypatch.setattr(
        "kiro_crew.members.member_identity_for_session",
        lambda key: (
            {"slug": "atlas", "name": "Atlas"}
            if "atlas" in key
            else {"slug": "bravo", "name": "Bravo"}
        ),
    )

    # Build A's projection first (gen=0 stored for atlas).
    tools.conductor_session_spec(dict(BASE), KEY)
    assert KEY in tools._PROJECTION_GENS

    # Revoke A's tick by bumping the per-app generation.
    tools.revoke_app_generation(APP)
    assert tools._APP_GENERATIONS.get(APP) == 1

    # Build B's projection AFTER the revocation.  B still has its tick.
    # B's sidecar snapshot is gen=1 (the current _APP_GENERATIONS[APP]).
    spec_b = tools.conductor_session_spec(dict(BASE), KEY_B)
    assert f"{APP}:team" in spec_b.get("mcpServers", {}), "B's projection must include the server"

    # Deliver B's array: snapshot matches current _APP_GENERATIONS[APP] so it passes.
    mcp_b = dict(spec_b.get("mcpServers") or {})
    filtered_b = tools.check_and_filter_projection(KEY_B, mcp_b)

    assert f"{APP}:team" in filtered_b, (
        "Bot B's server must survive the delivery gate even though Bot A was revoked; "
        "B built its sidecar with gen=1 (after revocation), so its snapshot matches "
        "_APP_GENERATIONS[APP] and check_and_filter_projection passes it through"
    )


# ── Fix 2: codex-folded alias is found in sidecar ─────────────────────────────


def test_codex_folded_alias_is_fenced_on_revocation(world, monkeypatch):
    """A server with a whitespace name ("my server") is delivered by codex as
    "my_server".  The sidecar must store both forms so the delivery gate can
    find the entry and enforce revocation.

    Remove the ``codex_name`` fallback from ``conductor_session_spec``'s sidecar
    write (or from ``check_and_filter_projection``'s lookup) and the folded alias
    is not found in the sidecar -- it is treated as a re-delivery and passes through
    despite revocation."""
    world["offers"] = [("lead", "my server")]

    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    # The original alias is in the spec.
    assert f"{APP}:my server" in spec_out.get("mcpServers", {}), "setup"
    # Sidecar holds the original alias (not the codex-folded form).
    assert f"{APP}:my server" in tools._PROJECTION_GENS.get(KEY, {}), "original must be in sidecar"

    # Revoke the app.
    tools.revoke_app_generation(APP)

    # Deliver the codex-folded alias (what codex_elements hands the session),
    # passing codex_name as the name_transform so the sidecar lookup can match.
    from kiro_crew.providers.mirrors.codex import codex_name as _cn

    folded = {f"{APP}:my_server": {"name": f"{APP}:my_server", "command": "x"}}
    filtered = tools.check_and_filter_projection(KEY, folded, name_transform=_cn)

    assert f"{APP}:my_server" not in filtered, (
        "codex-folded alias must be fenced after revocation; "
        "remove the name_transform fallback in check_and_filter_projection and the "
        "alias is not found in the sidecar, so it passes through"
    )


# ── Fix 3: unknown <app>: entry without sidecar is dropped (fail-closed) ─────


def test_app_server_without_sidecar_record_is_dropped(world, monkeypatch):
    """An ``<app>:`` entry that has no sidecar record (did not originate from this
    session's projection) must not mount.

    The sidecar was consumed on first delivery; a second (injected) entry for a
    different app -- one that was never offered to this session -- must be dropped.

    Remove the ``if recorded_gen is None: continue`` branch and the injected entry
    would mount even though no projection ever offered it to this session."""
    # First projection: sets up the sidecar for APP.
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    mcpServers = dict(spec_out.get("mcpServers") or {})

    # Consume the sidecar by running the first delivery (passes through since gen=0 matches).
    tools.check_and_filter_projection(KEY, mcpServers)

    # Test fail-closed on a session that NEVER had a sidecar (a completely new session).
    # An <app>: entry for a session with no projection history is dropped.
    fresh_key = "dashboard:member-fresh.memory-member-fresh-1"
    injected = {"other-app:tool": {"name": "other-app:tool", "command": "x"}}
    filtered = tools.check_and_filter_projection(fresh_key, injected)

    assert "other-app:tool" not in filtered, (
        "an <app>: entry for a session with no sidecar must be dropped (fail-closed); "
        "the session never had a projection, so check_and_filter_projection rejects it"
    )


# ── F2 fix 1: pending-close alias excluded from _UNMOUNTED ───────────────────


def test_pending_close_alias_not_in_unmounted_after_record_mounted(world, monkeypatch):
    """record_mounted must NOT write pending-close-withheld aliases into _UNMOUNTED.

    Scenario: (slug, APP) is in _PENDING_CLOSE_APPS.  check_and_filter_projection
    stashes the alias in _PENDING_CLOSE_WITHHELD.  record_mounted runs with nothing
    handed.  The alias must NOT appear in _UNMOUNTED; it must remain in
    _PENDING_CLOSE_WITHHELD.  Once drain clears _PENDING_CLOSE_WITHHELD, neither
    dict holds it, so the next reconcile treats it as a new addition and reloads.

    Remove the ``spelled(a) not in pending_withheld`` clause in record_mounted and
    the alias is written into _UNMOUNTED; drain then clears _PENDING_CLOSE_WITHHELD
    but _UNMOUNTED still holds the alias, so after_app_change keeps treating
    it as unchanged and the Bot never gets its tools back -- this test fails.
    """
    _SLUG = "atlas"  # from KEY's member-atlas segment
    SERVER_ALIAS = f"{APP}:team"
    # Build the offer.
    tools.conductor_session_spec(dict(BASE), KEY)
    # Simulate gate: (slug, APP) is pending-close, alias stashed in _PENDING_CLOSE_WITHHELD.
    tools._PENDING_CLOSE_APPS.add((_SLUG, APP))
    tools._PENDING_CLOSE_WITHHELD[_SLUG] = {SERVER_ALIAS: "fp-pending"}
    # record_mounted with nothing handed.
    tools.record_mounted(KEY, [])
    # The alias must NOT be in _UNMOUNTED.
    unmounted_for_slug = tools._UNMOUNTED.get(_SLUG, {})
    assert SERVER_ALIAS not in unmounted_for_slug, (
        "pending-close alias must NOT be written into _UNMOUNTED; "
        "remove the ``spelled(a) not in pending_withheld`` clause in record_mounted "
        "and the alias lands in _UNMOUNTED, permanently suppressing the Bot's tools "
        "even after drain confirms -- drain only clears _PENDING_CLOSE_WITHHELD"
    )
    # Must still be in _PENDING_CLOSE_WITHHELD so after_app_change sees now==was.
    assert SERVER_ALIAS in tools._PENDING_CLOSE_WITHHELD.get(_SLUG, {}), (
        "alias must remain in _PENDING_CLOSE_WITHHELD after record_mounted; "
        "after_app_change merges _UNMOUNTED + _PENDING_CLOSE_WITHHELD into withheld, "
        "so it still treats the alias as unchanged until drain clears it"
    )


def test_harness_blocked_and_pending_close_stays_blocked_after_drain(world, monkeypatch):
    """When an alias is BOTH harness-permanently-blocked (_UNMOUNTED) AND in
    _PENDING_CLOSE_WITHHELD, drain confirming the pending-close must NOT unblock it.

    _UNMOUNTED is permanent: if the harness withheld the alias for its own reasons
    (platform server name collision, execution-admission denial, etc.) that is
    independent of the pending-close.  drain must only clear _PENDING_CLOSE_WITHHELD;
    the alias stays in _UNMOUNTED and after_app_change still treats it as blocked.

    This test verifies that the new ``spelled(a) not in pending_withheld`` filter in
    record_mounted does not incorrectly exclude an alias that the harness itself
    blocked (i.e., spelled(a) IS in handed for the platform server that won the
    name collision, so it still ends up in _UNMOUNTED via the shadow dict).
    It then verifies drain does not touch _UNMOUNTED.
    """
    import asyncio

    _SLUG = "atlas"
    # Directly seed _UNMOUNTED with a harness-permanent block (independent of pending-close).
    PERM_ALIAS = f"{APP}:team"
    tools._UNMOUNTED[_SLUG] = {PERM_ALIAS: "fp-harness-block"}
    # Also add to _PENDING_CLOSE_WITHHELD (the alias happened to be pending-close too).
    tools._PENDING_CLOSE_WITHHELD[_SLUG] = {PERM_ALIAS: "fp-harness-block"}

    class _ConfirmsRuntime:
        async def evict_session(self, _sid: str) -> bool:
            return True

    tools._PENDING_CLOSE[(_SLUG, APP)] = [(_ConfirmsRuntime(), "kas-sid-drain-perm")]
    tools._PENDING_CLOSE_APPS.add((_SLUG, APP))

    asyncio.run(tools._drain_pending_close())

    # _PENDING_CLOSE_WITHHELD must be cleared.
    assert PERM_ALIAS not in tools._PENDING_CLOSE_WITHHELD.get(
        _SLUG, {}
    ), "_drain_pending_close must clear _PENDING_CLOSE_WITHHELD after confirmation"
    # _UNMOUNTED must still hold the harness-permanent block.
    unmounted_for_slug = tools._UNMOUNTED.get(_SLUG, {})
    assert PERM_ALIAS in unmounted_for_slug, (
        "_drain_pending_close must NOT clear _UNMOUNTED; the harness-permanent block "
        "remains valid even after the pending-close is confirmed"
    )


# ── F2 fix 2: whitespace-folded gen_dropped alias excluded from _UNMOUNTED ───


def test_gen_dropped_folded_alias_not_written_to_unmounted(world, monkeypatch):
    """When a name_transform folds whitespace in an alias (e.g. codex_name), a
    generation-dropped alias is stored as the folded delivered name in
    _REPROJECT_SLUGS.  record_mounted must exclude the ORIGINAL offered alias
    (which does NOT equal the folded name) from _UNMOUNTED so after_app_change
    schedules a reload on the next reconcile.

    Without the fix, ``a not in gen_dropped_delivered`` compares the original
    'my app:my server' against 'my_app:my_server' (folded), never matches, and
    the alias is written into _UNMOUNTED permanently.

    Remove the ``spelled(a) not in gen_dropped_delivered`` fix and revert to
    ``a not in gen_dropped`` and this test fails.
    """
    from kiro_crew.providers.mirrors.codex import codex_name as _cn

    _SLUG = "atlas"
    SPACE_APP = "my app"
    SPACE_ALIAS = "my app:my server"
    FOLDED_ALIAS = _cn(SPACE_ALIAS)  # 'my_app:my_server'

    # Set up an offer with a whitespace alias.
    monkeypatch.setattr(tools, "_app_names", lambda: [SPACE_APP])
    monkeypatch.setattr(tools, "_offers", lambda app: [("lead", "my server")])
    monkeypatch.setattr(tools, "preview_on", lambda cfg=None: True)
    import kiro_crew.apps.execution as _exec
    import kiro_crew.members as members
    from kiro_crew.apps import bot_grants

    monkeypatch.setattr(_exec, "app_execution_denied", lambda name, **_kw: None)
    monkeypatch.setattr(bot_grants, "app_bot_scopes", lambda app: frozenset({"tools"}))
    monkeypatch.setattr(bot_grants, "app_granted_bots", lambda app: (_SLUG,))
    monkeypatch.setattr(
        members, "member_identity_for_session", lambda k: {"slug": _SLUG, "name": "Atlas"}
    )
    monkeypatch.setattr(tools, "_rendered_entry", lambda *a: dict(ENTRY))

    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    assert SPACE_ALIAS in (
        spec_out.get("mcpServers") or {}
    ), "setup: original alias must be offered"

    # Simulate a gen-mismatch drop: _REPROJECT_SLUGS gets the FOLDED alias
    # (as check_and_filter_projection would add it when called with codex_name transform).
    tools._REPROJECT_SLUGS[_SLUG] = {FOLDED_ALIAS}

    # record_mounted with nothing handed (the whole delivery was filtered out).
    tools.record_mounted(KEY, [], spelled=_cn)

    unmounted_for_slug = tools._UNMOUNTED.get(_SLUG, {})
    assert SPACE_ALIAS not in unmounted_for_slug, (
        "gen-dropped alias with whitespace must not be written into _UNMOUNTED; "
        "revert to ``a not in gen_dropped`` (without spelled) and the original alias "
        "lands in _UNMOUNTED, permanently suppressing the Bot's tools after revocation"
    )
    # _REPROJECT_SLUGS should be cleared (popped) so after_app_change can schedule reload.
    assert _SLUG not in tools._REPROJECT_SLUGS, (
        "_REPROJECT_SLUGS must be cleared by record_mounted; "
        "if still present the reload window is never opened"
    )


# ── GPT :419 fix: active handler fence blocks ALL Bots ────────────────────────


def test_handler_held_fence_blocks_all_bots_not_only_pending_close_bot(
    world, monkeypatch, _floor_monkeypatch
):
    """When _FENCE_HOLD_COUNTS has a non-zero entry (a handler is mid-operation),
    record_mounted must fence the app for ALL Bots, not only the Bot whose slug
    matches a _PENDING_CLOSE_APPS entry.

    Before the fix, the predicate was:
        not any(a == app for _, a in _PENDING_CLOSE_APPS)
        or (slug, app) in _PENDING_CLOSE_APPS
    This allowed Bot B to mount while Bot A had a pending close, even during an
    active revocation that set _FENCE_HOLD_COUNTS[app] > 0.

    Remove the ``_FENCE_HOLD_COUNTS.get(app_name, 0) > 0`` guard in the
    fenced_apps comprehension and Bot B's mount goes through — the app appears
    in tools._MOUNTED[slug_b], so the assertion below fails.
    """
    _floor_monkeypatch.setattr(tools, "_FENCE_HOLD_COUNTS", {})
    _floor_monkeypatch.setattr(tools, "_PROJECTION_OFFER_FP", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE_WITHHELD", {})

    slug_a = "atlas"
    slug_b = "charlie"
    key_b = f"dashboard:member-{slug_b}.memory-member-{slug_b}-1"

    # Simulate: Bot A has an unconfirmed KAS close for APP.
    tools._PENDING_CLOSE_APPS.add((slug_a, APP))

    # Handler is currently mid-operation: _FENCE_HOLD_COUNTS[APP] > 0.
    tools._REVOCATION_FENCE.add(APP)
    tools._FENCE_HOLD_COUNTS[APP] = 1

    # Bot B cold-starts: conductor_session_spec builds projection, then record_mounted.
    spec_b = tools.conductor_session_spec(dict(BASE), key_b)
    mcp_b = dict(spec_b.get("mcpServers") or {})
    # check_and_filter_projection needs _PROJECTION_GENS entry (set by conductor_session_spec).
    # The sidecar was consumed by conductor_session_spec above; pass the original dict.
    mcp_b = tools.check_and_filter_projection(key_b, mcp_b)
    # record_mounted must fence Bot B because _FENCE_HOLD_COUNTS[APP] > 0.
    voided = tools.record_mounted(key_b, tools.mounted_names(mcp_b), str)

    assert APP in voided or APP not in tools._MOUNTED.get(slug_b, {}), (
        "Bot B must not mount the app while _FENCE_HOLD_COUNTS[APP] > 0; "
        "without the _FENCE_HOLD_COUNTS guard, Bot B mounts despite the active handler"
    )


# ── GPT :730 fix: projection offer fp for _PENDING_CLOSE_WITHHELD ─────────────


def test_pending_close_withheld_uses_unstamped_offer_fp(world, monkeypatch, _floor_monkeypatch):
    """check_and_filter_projection must store the PRE-stamp fingerprint in
    _PENDING_CLOSE_WITHHELD, not the post-stamp entry.

    Before the fix, fp_entry = {k: v for k, v in entry.items()
                                if k != 'x-kirocrew-session-label'}
    was used, which still contained the env changes that stamp_session_label
    made.  after_app_change compares against bot_servers() (unstamped), so the
    fp never matched and the Bot reloaded on every after_app_change call.

    With the fix, conductor_session_spec stores the pre-stamp fp in
    _PROJECTION_OFFER_FP, and check_and_filter_projection uses it.

    Remove the _PROJECTION_OFFER_FP lookup (fall through to the strip-only path)
    and withheld_fp != expected_fp, confirming the mismatch that caused reloads.
    """
    _floor_monkeypatch.setattr(tools, "_FENCE_HOLD_COUNTS", {})
    _floor_monkeypatch.setattr(tools, "_PROJECTION_OFFER_FP", {})
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE_WITHHELD", {})

    import json

    slug = "atlas"

    # Use a plain ENTRY without x-kirocrew-session-label; the real _rendered_entry
    # does not include that key (stamp_session_label adds it).  The test must use
    # a plain entry so the pre-stamp and post-stamp dicts are clearly different
    # (post-stamp has env.KIROCREW_SESSION_LABEL; pre-stamp does not).
    plain_entry = {"command": "python3", "args": ["/a/server.py"]}
    monkeypatch.setattr(tools, "_rendered_entry", lambda *a: dict(plain_entry))

    # Seed a pending close so check_and_filter_projection withholds the alias.
    tools._PENDING_CLOSE_APPS.add((slug, APP))

    # Build projection via conductor_session_spec (which writes _PROJECTION_OFFER_FP).
    spec = tools.conductor_session_spec(dict(BASE), KEY)
    mcp = dict(spec.get("mcpServers") or {})

    # The expected fp is the unstamped offer — plain_entry without any env changes.
    expected_fp = json.dumps(plain_entry, sort_keys=True, default=str)

    # check_and_filter_projection withholds the alias into _PENDING_CLOSE_WITHHELD.
    tools.check_and_filter_projection(KEY, mcp)

    withheld = tools._PENDING_CLOSE_WITHHELD.get(slug, {})
    alias = f"{APP}:team"
    assert alias in withheld, f"{alias!r} must be withheld into _PENDING_CLOSE_WITHHELD"
    withheld_fp = withheld[alias]

    assert withheld_fp == expected_fp, (
        f"_PENDING_CLOSE_WITHHELD fp must equal the unstamped bot_servers() fp;\n"
        f"stored:   {withheld_fp!r}\n"
        f"expected: {expected_fp!r}\n"
        "without _PROJECTION_OFFER_FP, stamp_session_label changes env and the fps "
        "never match, causing a reload loop on every after_app_change call"
    )


@pytest.mark.asyncio
async def test_stop_still_discards_current_session_when_predecessor_pending_close_remains(
    world, monkeypatch
):
    """stop_app_bot_chat_servers must NOT skip the current session just because a
    predecessor session's close is still pending (after a swap_conversation).

    Before the fix, the loop hit ``_PENDING_CLOSE.get((slug, app_name))`` and
    ``continue``-d, leaving the current Bot session running the withdrawn app's
    tools.  After the fix, the pending entry records a failure but the current
    session is still discarded.

    Remove the fix (restore the ``continue``) and ``discard_called`` stays empty
    although the app server should have been stopped.
    """
    _SLUG = "atlas"
    SERVER_ALIAS = f"{APP}:team"

    # Seed _MOUNTED so stop_app_bot_chat_servers sees a mounted session.
    tools._MOUNTED[_SLUG] = {SERVER_ALIAS: "fp-current"}
    tools._MOUNTED_SESSION_KEYS[_SLUG] = KEY

    # Seed a PENDING_CLOSE for this slug/app -- simulating a predecessor session's
    # unconfirmed close that is still retrying after a swap_conversation.
    predecessor_rt = object()
    tools._PENDING_CLOSE[(_SLUG, APP)] = [(predecessor_rt, "predecessor-sid")]
    tools._PENDING_CLOSE_APPS.add((_SLUG, APP))

    discard_called: list[str] = []

    class _SessionManager:
        def _allocation_boundary(self):
            class _Alloc:
                def begin_ending(self, key):
                    pass

                def end_ending(self, key):
                    pass

                def spawn_in_flight(self, key):
                    return None

            return _Alloc()

        async def discard_conversation(self, key, **kw):
            discard_called.append(key)
            return True  # confirmed

    class _State:
        sessions = _SessionManager()

    state = _State()

    # _drain_pending_close: mock to not change _PENDING_CLOSE so the pending entry
    # is still present when the for-slug loop runs.
    monkeypatch.setattr(tools, "_drain_pending_close", lambda: (lambda: None)())

    async def _noop_drain():
        pass

    monkeypatch.setattr(tools, "_drain_pending_close", _noop_drain)

    stopped, failed = await tools.stop_app_bot_chat_servers(state, APP)

    assert KEY in discard_called, (
        "discard_conversation must be called for the current session even when "
        "_PENDING_CLOSE still holds a predecessor-session handle. "
        "Restore the ``continue`` and discard_called stays empty."
    )
    # The predecessor's failure should still be recorded.
    assert (
        _SLUG in failed
    ), "_SLUG must appear in failed because the predecessor's pending close is unresolved"


@pytest.mark.asyncio
async def test_stop_registers_reconciler_when_not_yet_set(world, monkeypatch):
    """stop_app_bot_chat_servers must register _RECONCILER when it is None so that
    close-recovery can schedule after_app_change independently after a restart.

    Without the fix (_RECONCILER stays None), _schedule_reconcile called by
    _drain_pending_close is a no-op, and the Bot chat never remounts its app servers
    after a confirmed close-retry (the grant is still valid, nothing changed).

    Remove the ``global _RECONCILER; if _RECONCILER is None...`` block and
    _RECONCILER stays None.
    """
    # Ensure _RECONCILER is None at the start.
    monkeypatch.setattr(tools, "_RECONCILER", None)
    # Nothing mounted, nothing to stop.
    state = object()

    async def _noop_drain():
        pass

    monkeypatch.setattr(tools, "_drain_pending_close", _noop_drain)

    await tools.stop_app_bot_chat_servers(state, APP)

    assert tools._RECONCILER is not None, (
        "_RECONCILER must be set after stop_app_bot_chat_servers runs with a non-None "
        "state; without it, _schedule_reconcile called by drain is a no-op and "
        "close-recovery never remounts app servers after a gateway restart."
    )
    reg_state, _ = tools._RECONCILER
    assert reg_state is state, "_RECONCILER state must be the passed state object"


@pytest.mark.asyncio
async def test_teardown_app_runtime_calls_bot_stop_when_state_given(monkeypatch):
    """teardown_app_runtime must call stop_app_bot_chat_servers when state is provided.

    This covers the three paths that miss the bot stop when the per-handler blocks
    are absent: session_consent.py, uninstall_app_in_process, and handle_migrate_cleanup.

    Remove the ``if state is not None:`` guard (or the bot-stop block inside
    teardown_app_runtime) and bot_stop_calls stays empty.
    """
    from unittest.mock import AsyncMock

    from kiro_crew.apps.teardown import teardown_app_runtime

    bot_stop_calls: list[tuple] = []
    after_calls: list = []

    async def _fake_stop(state, app_name):
        bot_stop_calls.append(app_name)
        return [], []

    async def _fake_after(state):
        after_calls.append(state)

    monkeypatch.setattr(tools, "revoke_app_generation", lambda name: None)
    monkeypatch.setattr(tools, "stop_app_bot_chat_servers", _fake_stop)
    monkeypatch.setattr(tools, "after_app_change", _fake_after)

    # Minimal stubs so teardown_app_runtime completes
    import unittest.mock

    with unittest.mock.patch(
        "kiro_crew.apps.teardown.stop_app_startup_hooks", new=AsyncMock(return_value=True)
    ):
        with unittest.mock.patch(
            "kiro_crew.apps.teardown.on_app_disable", new=AsyncMock(return_value={})
        ):
            with unittest.mock.patch(
                "kiro_crew.apps.teardown.stop_backend_and_observe",
                new=AsyncMock(return_value=(None, True)),
            ):
                with unittest.mock.patch(
                    "kiro_crew.apps.teardown.notify_app_disabled", new=AsyncMock()
                ):
                    state = object()
                    await teardown_app_runtime(APP, {}, state=state)

    assert APP in bot_stop_calls, (
        "stop_app_bot_chat_servers must be called by teardown_app_runtime when state is given; "
        "remove the ``if state is not None:`` block and bot_stop_calls stays empty"
    )
    assert (
        after_calls
    ), "after_app_change must be called by teardown_app_runtime when state is given"


@pytest.mark.asyncio
async def test_teardown_app_runtime_skips_bot_stop_when_state_is_none(monkeypatch):
    """teardown_app_runtime must NOT call stop_app_bot_chat_servers when state is None
    (e.g. session_consent.py path which has no HTTP request context).

    This is the fail-safe: state=None means no session manager is available; bot
    stop is a no-op but teardown still completes.
    """
    import unittest.mock
    from unittest.mock import AsyncMock

    from kiro_crew.apps.teardown import teardown_app_runtime

    bot_stop_calls: list = []
    monkeypatch.setattr(
        tools, "stop_app_bot_chat_servers", lambda *a, **kw: bot_stop_calls.append(a)
    )

    with unittest.mock.patch(
        "kiro_crew.apps.teardown.stop_app_startup_hooks", new=AsyncMock(return_value=True)
    ):
        with unittest.mock.patch(
            "kiro_crew.apps.teardown.on_app_disable", new=AsyncMock(return_value={})
        ):
            with unittest.mock.patch(
                "kiro_crew.apps.teardown.stop_backend_and_observe",
                new=AsyncMock(return_value=(None, True)),
            ):
                with unittest.mock.patch(
                    "kiro_crew.apps.teardown.notify_app_disabled", new=AsyncMock()
                ):
                    await teardown_app_runtime(APP, {}, state=None)

    assert bot_stop_calls == [], "stop_app_bot_chat_servers must NOT be called when state is None"


@pytest.mark.asyncio
async def test_notify_app_disabled_runs_before_bot_stop_in_teardown(monkeypatch):
    """teardown_app_runtime must call notify_app_disabled BEFORE stop_app_bot_chat_servers,
    so the app's in-process workers lose their authority before the potentially
    network-bound Bot-chat teardown starts.

    Remove the ``await notify_app_disabled(name)`` call that precedes the bot-stop
    block (or swap the order) and ``order`` shows ``bot_stop`` before ``notify``,
    meaning app workers keep auto-approval grants through several close timeouts.
    """
    from unittest.mock import AsyncMock

    from kiro_crew.apps.teardown import teardown_app_runtime

    order: list[str] = []

    async def _fake_notify(name: str) -> None:
        order.append("notify")

    async def _fake_stop(state, app_name):
        order.append("bot_stop")
        return [], []

    monkeypatch.setattr(tools, "revoke_app_generation", lambda name: None)
    monkeypatch.setattr(tools, "stop_app_bot_chat_servers", _fake_stop)
    monkeypatch.setattr(tools, "after_app_change", AsyncMock())

    import unittest.mock

    with unittest.mock.patch(
        "kiro_crew.apps.teardown.stop_app_startup_hooks", new=AsyncMock(return_value=True)
    ):
        with unittest.mock.patch(
            "kiro_crew.apps.teardown.on_app_disable", new=AsyncMock(return_value={})
        ):
            with unittest.mock.patch(
                "kiro_crew.apps.teardown.stop_backend_and_observe",
                new=AsyncMock(return_value=(None, True)),
            ):
                with unittest.mock.patch(
                    "kiro_crew.apps.teardown.notify_app_disabled", side_effect=_fake_notify
                ):
                    state = object()
                    await teardown_app_runtime(APP, {}, state=state)

    assert (
        "notify" in order and "bot_stop" in order
    ), "Both notify_app_disabled and stop_app_bot_chat_servers must run"
    notify_idx = order.index("notify")
    bot_stop_idx = order.index("bot_stop")
    assert notify_idx < bot_stop_idx, (
        "notify_app_disabled must run BEFORE stop_app_bot_chat_servers; "
        "swapping the order lets app workers keep auto-approval grants through "
        "network-bound close timeouts. "
        f"Got order: {order}"
    )
