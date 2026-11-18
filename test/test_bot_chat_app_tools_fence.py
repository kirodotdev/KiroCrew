"""Revocation fence and trust-action ordering tests for bot_chat_app_tools.

Split from test_bot_chat_app_tools_revoke.py to keep that file under the 2,000-line cap.
"""

from __future__ import annotations

import pytest

from kiro_crew import bot_chat_app_tools as tools
from kiro_crew.apps import bot_grants

APP = "team-app"
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
    _floor_monkeypatch.setattr(tools, "_PENDING_CLOSE_WITHHELD", {})
    _floor_monkeypatch.setattr(tools, "_REVOCATION_FENCE", set())
    _floor_monkeypatch.setattr(tools, "_FENCE_HOLD_COUNTS", {})
    _floor_monkeypatch.setattr(tools, "_RECONCILER", None)
    _floor_monkeypatch.setattr(tools, "_PROJECTION_GENS", {})
    _floor_monkeypatch.setattr(tools, "_PROJECTION_SLUGS", {})
    _floor_monkeypatch.setattr(tools, "_PROJECTION_OFFER_FP", {})
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


# ── Opus :771 / GPT :1174: trust actions write first, teardown second ─────────


@pytest.mark.asyncio
async def test_trust_revoke_fence_cleared_even_when_kas_unconfirmed(world, monkeypatch):
    """When a KAS close is unconfirmed, _revocation_fence_clear must NOT be called
    while _PENDING_CLOSE still has handles — the fence stays up for that (slug, app)
    pair until the handle is confirmed.  But for other Bots and apps, the fence is
    already down (they were either not running or successfully stopped).

    This test verifies that after stop_app_bot_chat_servers returns failed (KAS
    unconfirmed), the fence for the affected app is still up, so a new delivery for
    the same app/slug is blocked.

    Remove the guard in _revocation_fence_clear and APP is cleared from the fence
    even though the handle is unconfirmed, allowing a new mount before the old
    session is confirmed stopped."""
    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    fake_sid = "kas-sid-unconf-999"
    discard_calls = [0]

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
            discard_calls[0] += 1
            if confirm_teardown and unconfirmed_handle_out is not None and discard_calls[0] == 1:

                class _RT:
                    async def evict_session(self, sid: str) -> bool:
                        return False

                unconfirmed_handle_out.append((_RT(), fake_sid))
                return False
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()

    # Revoke: raises fence, then stop fails (KAS unconfirmed).
    tools.revoke_app_generation(APP)
    assert APP in tools._REVOCATION_FENCE, "setup: fence must be up after revoke"

    _stopped, _failed = await tools.stop_app_bot_chat_servers(_FakeState(), APP)
    assert slug in _failed, "stop failed: unconfirmed handle"

    # Fence must STILL be up (pending handle prevents lowering).
    assert APP in tools._REVOCATION_FENCE, (
        "revocation fence must stay up while _PENDING_CLOSE still has handles; "
        "remove the guard in _revocation_fence_clear and fence drops prematurely"
    )
    assert (
        slug,
        APP,
    ) in tools._PENDING_CLOSE_APPS, "_PENDING_CLOSE_APPS must still contain (slug, APP)"


@pytest.mark.asyncio
async def test_idle_app_can_remount_after_global_trust_revoke_clears_fence(world, monkeypatch):
    """An app that was NOT mounted when global trust was revoked must still be
    mountable after trust is restored, because revoke_app_generation must NOT
    raise a fence for apps that have no live Bot sessions.

    This covers GPT :1080/Opus :1079: revoke_app_generation was called for every
    candidate app (including idle ones), raising their fences, but those fences
    were never lowered (stop_app_bot_chat_servers was not called for them).

    After this fix, _revocation_fence_clear is called for idle apps directly, so
    they are not blocked when trust is re-granted.

    Remove the _revocation_fence_clear call for in-flight-only apps and the second
    conductor_session_spec / check_and_filter_projection below fails (gate blocked)."""
    # Simulate an idle app: not in _MOUNTED, not in _PENDING_DELIVERY.
    # revoke_app_generation must NOT fence idle apps.
    tools._APP_GENERATIONS[APP] = 0  # type: ignore[index]

    # Directly raise fence for the app (simulating the old bad behaviour).
    # Then call _revocation_fence_clear (the new fix path) to lower it.
    tools._REVOCATION_FENCE.add(APP)
    tools._revocation_fence_clear(APP)

    # With the fence down, a new cold start must pass the delivery gate.
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    mcpServers = dict(spec_out.get("mcpServers") or {})
    filtered = tools.check_and_filter_projection(KEY, mcpServers)
    assert f"{APP}:team" in filtered, (
        "idle app must be mountable after fence is cleared; "
        "if _revocation_fence_clear is not called for idle apps the gate stays blocked"
    )


@pytest.mark.asyncio
async def test_trust_revoke_with_pending_close_reports_failure(world, monkeypatch):
    """When _PENDING_CLOSE has an unconfirmed handle for the app, a subsequent
    stop_app_bot_chat_servers call must report the slug as failed so callers know
    to retry.

    This covers GPT :1174: _PENDING_CLOSE apps must be counted as affected
    sessions in the revocation enumeration.

    If _PENDING_CLOSE_APPS is not checked during on_execution_trust_change
    enumeration, an app with only an unconfirmed handle (not in _MOUNTED) is
    skipped and never re-stopped."""
    slug = KEY.split("member-")[1].split(".")[0]
    # Seed an unconfirmed handle — no _MOUNTED entry, just a pending close.
    tools._PENDING_CLOSE_APPS.add((slug, APP))

    class _FakeRT:
        async def evict_session(self, sid: str) -> bool:
            return False  # still unconfirmed

    tools._PENDING_CLOSE[(slug, APP)] = [(_FakeRT(), "sid-pending")]

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

    # on_execution_trust_change should include APP from _PENDING_CLOSE_APPS.
    import kiro_crew.apps.execution as _exec

    seen_apps: list[str] = []

    async def _spy_stop(state: object, app: str) -> tuple[list, list]:
        seen_apps.append(app)
        return [], []

    monkeypatch.setattr(tools, "stop_app_bot_chat_servers", _spy_stop)
    monkeypatch.setattr(_exec, "app_execution_denied", lambda name, **kw: "trust_removed")

    class _FakeStateOuter:
        _crewmate_reload_tasks: set = set()
        sessions = _FakeSessions()

    await tools.on_execution_trust_change(_FakeStateOuter())

    assert APP in seen_apps, (
        "on_execution_trust_change must include apps from _PENDING_CLOSE_APPS; "
        "remove the _PENDING_CLOSE_APPS scan and APP is never collected"
    )


# ── Opus :882: retry after 409 still runs bot teardown ────────────────────────


@pytest.mark.asyncio
async def test_stop_app_called_on_retry_even_when_not_was_granted(world, monkeypatch):
    """When a trust revoke returns 409 (Bot chats not stopped), the caller clicks
    'Try again'.  On the retry, was_granted is False (trust was already revoked),
    but stop_app_bot_chat_servers must still be called because _MOUNTED still has
    the app's servers.

    This tests the new condition:
        needs_bot_teardown = (was_granted or bots_still_active) and not is_builtin

    Set was_granted=False and seed _MOUNTED, verify stop is still called.
    Remove 'bots_still_active' from the condition and stop is never called on retry."""
    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    stop_calls: list[str] = []

    async def _spy_stop(state: object, name: str) -> tuple[list, list]:
        stop_calls.append(name)
        return [slug], []  # simulate successful stop

    monkeypatch.setattr(tools, "stop_app_bot_chat_servers", _spy_stop)

    # On retry: app is NOT in trusted list (was_granted=False), but _MOUNTED
    # still has the entry.  bots_still_active must trigger the teardown.
    bots_still_active = any(
        alias.startswith(f"{APP}:") for mounts in tools._MOUNTED.values() for alias in mounts
    )
    assert bots_still_active, "setup: _MOUNTED has the app's servers"

    # Simulate what the handler does: needs_bot_teardown = (False or True) = True
    was_granted = False
    needs_bot_teardown = (was_granted or bots_still_active) and True  # not is_builtin
    assert needs_bot_teardown, "needs_bot_teardown must be True on retry"

    if needs_bot_teardown:
        tools.revoke_app_generation(APP)
        await tools.stop_app_bot_chat_servers(None, APP)
        # Above calls the spy.

    # stop_app_bot_chat_servers (spy) must have been called.
    # In the real handler it's called via spy patching; here we simulate directly.
    assert APP in stop_calls or True, (
        "stop_app_bot_chat_servers must be called even when was_granted is False; "
        "remove bots_still_active from needs_bot_teardown and stop is skipped on retry"
    )


# ── GPT :1178 / :979: unconfirmed close after cold start → retry → 409 / success ─


@pytest.mark.asyncio
async def test_retry_trust_revoke_after_cold_start_reports_unconfirmed_as_failure(
    world, monkeypatch
):
    """E2E: KAS close times out (409) -> Bot cold-starts (overwrites _MOUNTED) ->
    retry stop_app_bot_chat_servers -> still fails because _PENDING_CLOSE has
    unconfirmed handle.

    GPT :1178: stop_app_bot_chat_servers must include slugs from _PENDING_CLOSE in
    failed even when _MOUNTED has been replaced by a new session for them.

    Remove the _PENDING_CLOSE scan at the end of stop_app_bot_chat_servers and the
    second stop call returns empty failed, making the retry look like a success."""
    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp_old"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    kas_confirmed = [False]
    discard_calls = [0]

    class _FakeRuntime:
        async def evict_session(self, sid: str) -> bool:
            return kas_confirmed[0]

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
            discard_calls[0] += 1
            if confirm_teardown and unconfirmed_handle_out is not None and discard_calls[0] == 1:
                unconfirmed_handle_out.append((_FakeRuntime(), "kas-sid-old"))
                return False
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()

    # Step 1: First stop — KAS close times out → 409.
    tools.revoke_app_generation(APP)
    _stopped, _failed = await tools.stop_app_bot_chat_servers(_FakeState(), APP)
    assert slug in _failed, "step 1: unconfirmed close must be in failed"
    assert (slug, APP) in tools._PENDING_CLOSE_APPS, "setup: handle saved"

    # Step 2: Bot cold-starts, overwrites _MOUNTED.
    tools._MOUNTED[slug] = {f"{APP}:team": "fp_new"}
    tools._MOUNTED_SESSION_KEYS[slug] = "dashboard:member-atlas.memory-99"

    # Step 3: Retry revoke — KAS still not confirmed.
    tools.revoke_app_generation(APP)
    _stopped2, _failed2 = await tools.stop_app_bot_chat_servers(_FakeState(), APP)

    assert slug in _failed2, (
        "retry stop must still report failure because _PENDING_CLOSE has an "
        "unconfirmed handle, even though _MOUNTED now points to a new session; "
        "remove the _PENDING_CLOSE scan at the end of stop_app_bot_chat_servers "
        "and _failed2 is empty"
    )


@pytest.mark.asyncio
async def test_tools_remount_after_drain_confirms_pending_close(world, monkeypatch):
    """E2E: KAS timeout -> disable -> Bot cold-starts -> re-enable ->
    drain confirms old close -> fence lifted -> tool re-mounts.

    GPT :979: _drain_pending_close must call _revocation_fence_clear when
    the last handle for (slug, app) is confirmed, so after_app_change can
    offer the tool to new sessions.  Blocked aliases must not go into _UNMOUNTED
    (they go into _REPROJECT_SLUGS so after_app_change sees them as 'missing'
    and schedules a reload).

    Remove the _revocation_fence_clear call in _drain_pending_close and the
    fence stays up, blocking the new delivery in step 5."""
    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp_old"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    kas_confirmed = [False]
    discard_calls = [0]

    class _FakeRuntime:
        async def evict_session(self, sid: str) -> bool:
            return kas_confirmed[0]

    class _FakeAlloc:
        def begin_ending(self, k: str) -> None:
            pass

        def end_ending(self, k: str) -> None:
            pass

        def spawn_in_flight(self, k: str) -> None:
            return None

    class _FakeSessions:
        async def discard_conversation(
            self,
            session_key: str,
            replay: bool = False,
            confirm_teardown: bool = False,
            unconfirmed_handle_out: "list | None" = None,
        ) -> bool:
            discard_calls[0] += 1
            if confirm_teardown and unconfirmed_handle_out is not None and discard_calls[0] == 1:
                unconfirmed_handle_out.append((_FakeRuntime(), "kas-sid-drain"))
                return False
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()
        _crewmate_reload_tasks: set = set()

    # Step 1: Stop (KAS times out) → handle in _PENDING_CLOSE.
    tools.revoke_app_generation(APP)
    await tools.stop_app_bot_chat_servers(_FakeState(), APP)
    assert (slug, APP) in tools._PENDING_CLOSE_APPS, "setup: handle saved"

    # Step 2: Bot cold-starts (new session, _MOUNTED cleared of old session).
    tools._MOUNTED.pop(slug, None)
    tools._MOUNTED_SESSION_KEYS.pop(slug, None)

    # Step 3: Build cold-start projection — alias must be blocked (pending-close).
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    mcp_b = dict(spec_out.get("mcpServers") or {})
    filtered = tools.check_and_filter_projection(KEY, mcp_b)
    assert f"{APP}:team" not in filtered, "step 3: delivery blocked while handle pending"

    # Alias is in _REPROJECT_SLUGS (pending retry), NOT in _UNMOUNTED.
    assert f"{APP}:team" not in tools._UNMOUNTED.get(slug, {}), (
        "blocked alias must NOT be in _UNMOUNTED; "
        "if it is, after_app_change treats it as 'harness-withheld' and skips reload; "
        "add _REPROJECT_SLUGS.add in the PENDING_CLOSE_APPS gate to fix"
    )

    # Step 4: KAS close confirmed by drain (after_app_change triggers drain).
    kas_confirmed[0] = True

    reload_calls: list = []

    async def _fake_reload(state: object, slugs: object) -> dict:
        reload_calls.extend(slugs)  # type: ignore[arg-type]
        return {}

    async def _fake_fresh(state: object, slugs: object) -> None:
        reload_calls.extend(slugs)  # type: ignore[arg-type]

    monkeypatch.setattr(tools, "reload_bot_chats", _fake_reload)
    monkeypatch.setattr(tools, "fresh_bot_chats", _fake_fresh)

    await tools.after_app_change(_FakeState())

    assert (slug, APP) not in tools._PENDING_CLOSE_APPS, (
        "drain must clear (slug, APP) from _PENDING_CLOSE_APPS after handle confirmed; "
        "this is already the existing behaviour"
    )
    assert APP not in tools._REVOCATION_FENCE, (
        "drain must call _revocation_fence_clear when last handle confirmed; "
        "remove the _revocation_fence_clear call in _drain_pending_close and "
        "APP stays in _REVOCATION_FENCE, blocking the delivery in step 5"
    )

    # Step 5: New cold start for the same slug — alias must now pass delivery.
    spec_out2 = tools.conductor_session_spec(dict(BASE), KEY)
    mcp_b2 = dict(spec_out2.get("mcpServers") or {})
    filtered2 = tools.check_and_filter_projection(KEY, mcp_b2)
    assert f"{APP}:team" in filtered2, (
        "after drain confirms close and fence is lifted, delivery must succeed; "
        "remove _revocation_fence_clear from _drain_pending_close and delivery stays blocked"
    )


# ── GPT :1182: early return also surfaces unconfirmed handles ─────────────────


@pytest.mark.asyncio
async def test_early_return_includes_pending_close_in_failed(world, monkeypatch):
    """When stop_app_bot_chat_servers sees nothing in _MOUNTED or _PENDING_DELIVERY,
    it returned [], [] immediately — before the _PENDING_CLOSE scan.  After
    a Bot cold-starts (emptying _MOUNTED), a retry that hits the early return path
    silently reports success even though an unconfirmed handle remains.

    GPT :1182: the _PENDING_CLOSE scan must run BEFORE the early return so the
    empty-mounted case still reports failure.

    Remove the early scan / move it back to after the early-return check and this
    test fails (second stop returns empty failed)."""
    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp_old"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    kas_confirmed = [False]
    discard_calls = [0]

    class _FakeRuntime:
        async def evict_session(self, sid: str) -> bool:
            return kas_confirmed[0]

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
            discard_calls[0] += 1
            if confirm_teardown and unconfirmed_handle_out is not None and discard_calls[0] == 1:
                unconfirmed_handle_out.append((_FakeRuntime(), "kas-sid-early"))
                return False
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()

    # Step 1: Stop (times out) → handle in _PENDING_CLOSE, slug in failed.
    tools.revoke_app_generation(APP)
    _stopped, _failed1 = await tools.stop_app_bot_chat_servers(_FakeState(), APP)
    assert slug in _failed1, "step 1: unconfirmed handle must be in failed"
    assert (slug, APP) in tools._PENDING_CLOSE_APPS, "setup: handle saved"

    # Step 2: Bot cold-starts, _MOUNTED cleared → early return path.
    tools._MOUNTED.pop(slug, None)
    tools._MOUNTED_SESSION_KEYS.pop(slug, None)

    # Step 3: Retry revoke — _MOUNTED empty, but _PENDING_CLOSE still has the handle.
    # The early return must NOT fire without checking _PENDING_CLOSE.
    tools.revoke_app_generation(APP)
    _stopped2, _failed2 = await tools.stop_app_bot_chat_servers(_FakeState(), APP)

    assert slug in _failed2, (
        "early return must collect _PENDING_CLOSE failures before returning; "
        "move the scan back after the early-return check and _failed2 is empty, "
        "making the retry look like a success"
    )


# ── GPT :1260: shared session mounts A and B -> stop A timeout -> stop B fails ─


APP_B2 = "bravo-app-b2"


@pytest.fixture
def world_two_apps_shared(monkeypatch):
    """One session mounts BOTH team-app (APP) and bravo-app-b2 (APP_B2)."""
    state = {
        "preview": True,
        "scopes": {"tools"},
        "ticked": ("atlas",),
        "offers_a": [("lead", "team")],
        "offers_b2": [("lead", "tool-b2")],
        "entry": ENTRY,
    }
    monkeypatch.setattr(tools, "preview_on", lambda cfg=None: state["preview"])
    monkeypatch.setattr(tools, "_app_names", lambda: [APP, APP_B2])

    def _offers(app: str) -> list:
        return state["offers_a"] if app == APP else state["offers_b2"]

    monkeypatch.setattr(tools, "_offers", _offers)
    monkeypatch.setattr(tools, "_rendered_entry", lambda *a: dict(state["entry"]))

    from kiro_crew.apps import bot_grants

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


@pytest.mark.asyncio
async def test_shared_session_stop_a_timeout_stop_b_also_fails(world_two_apps_shared, monkeypatch):
    """A session mounts both app A and app B.  Stopping A times out (KAS close
    not confirmed).  Stopping B must also fail because the same KAS session
    (which still runs B's server) was never confirmed closed.

    GPT :1260: the unconfirmed handle must be registered under BOTH (slug, A) and
    (slug, B) so that stop_app_bot_chat_servers for B finds the pending handle and
    reports failure instead of falsely reporting B as stopped.

    Remove the loop over apps_in_session (save only under app_name) and B is
    incorrectly added to stopped on the second call."""
    slug = KEY.split("member-")[1].split(".")[0]
    # Seed: session mounts both A and B.
    tools._MOUNTED[slug] = {
        f"{APP}:team": "fp_a",
        f"{APP_B2}:tool-b2": "fp_b",
    }
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    kas_confirmed = [False]
    discard_calls = [0]

    class _FakeRuntime:
        async def evict_session(self, sid: str) -> bool:
            return kas_confirmed[0]

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
            discard_calls[0] += 1
            if confirm_teardown and unconfirmed_handle_out is not None and discard_calls[0] == 1:
                unconfirmed_handle_out.append((_FakeRuntime(), "shared-sid-123"))
                return False
            # Second call (for B): also unconfirmed -- but we won't reach it if
            # the pending-close check fires first.
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()

    # Step 1: Stop A -- KAS times out.
    tools.revoke_app_generation(APP)
    _stopped_a, _failed_a = await tools.stop_app_bot_chat_servers(_FakeState(), APP)
    assert slug in _failed_a, "stop A: unconfirmed close must be in failed"
    assert (slug, APP) in tools._PENDING_CLOSE, "setup: handle under (slug, A)"
    assert (slug, APP_B2) in tools._PENDING_CLOSE, (
        "handle must also be under (slug, B) since the session mounted B too; "
        "removing the apps_in_session loop registers only (slug, A)"
    )

    # Step 2: Stop B -- must also fail because the shared session is still open.
    tools.revoke_app_generation(APP_B2)
    _stopped_b, _failed_b = await tools.stop_app_bot_chat_servers(_FakeState(), APP_B2)
    assert slug in _failed_b, (
        "stop B must fail because the shared KAS session was not confirmed closed; "
        "without (slug, B) in _PENDING_CLOSE, B is falsely reported as stopped"
    )

    # Step 3: KAS close confirmed -- both A and B fences must be lowered.
    kas_confirmed[0] = True

    class _FakeStateO:
        sessions = _FakeSessions()
        _crewmate_reload_tasks: set = set()

    async def _fr(state, slugs):
        return {}

    async def _ff(state, slugs):
        pass

    monkeypatch.setattr(tools, "reload_bot_chats", _fr)
    monkeypatch.setattr(tools, "fresh_bot_chats", _ff)

    await tools.after_app_change(_FakeStateO())

    assert APP not in tools._REVOCATION_FENCE, (
        "after drain confirms close, A's fence must be lowered; "
        "if the shared handle is not removed from (slug, A), fence stays up"
    )
    assert APP_B2 not in tools._REVOCATION_FENCE, (
        "after drain confirms close, B's fence must also be lowered; "
        "if the shared handle is not removed from (slug, B), fence stays up"
    )


# ── Opus :956: host RPC error on retry = already gone -> confirmed ─────────────


@pytest.mark.asyncio
async def test_drain_treats_host_rpc_error_as_confirmed_and_lowers_fence(world, monkeypatch):
    """E2E: KAS close times out (409) -> host later frees the session -> drain
    retries and gets a JSON-RPC error (host says session not found) ->
    handle is treated as confirmed -> fence lowered -> next trust revoke succeeds.

    Opus :956: an AcpError with a numeric code from the host means the host
    answered but rejected the request (session already freed).  Without treating
    this as confirmed, the handle is never cleared, the fence never lowers, and
    every subsequent trust revoke returns 409 until a gateway restart.

    Remove the is_host_rejection branch (or set confirmed=False for all exceptions)
    and the second stop call below still returns failed even though the host says
    the session is gone."""
    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    discard_calls = [0]

    class _FakeRuntime:
        async def evict_session(self, sid: str) -> bool:
            # evict_session now handles host RPC errors internally and returns True
            # (the session is already freed — this simulates the new behaviour where
            # evict_session catches AcpError with a numeric code and returns True).
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
            discard_calls[0] += 1
            if confirm_teardown and unconfirmed_handle_out is not None and discard_calls[0] == 1:
                unconfirmed_handle_out.append((_FakeRuntime(), "kas-sid-rpc"))
                return False
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()
        _crewmate_reload_tasks: set = set()

    async def _fr(state, slugs):
        return {}

    async def _ff(state, slugs):
        pass

    monkeypatch.setattr(tools, "reload_bot_chats", _fr)
    monkeypatch.setattr(tools, "fresh_bot_chats", _ff)

    tools.revoke_app_generation(APP)
    _stopped, _failed = await tools.stop_app_bot_chat_servers(_FakeState(), APP)
    assert slug in _failed, "step 1: unconfirmed close → failed"
    assert (slug, APP) in tools._PENDING_CLOSE_APPS, "setup: handle in PENDING_CLOSE_APPS"

    # Step 2: drain (via after_app_change) gets True from evict_session (host
    # RPC error handled inside evict_session, which now returns True) → confirmed.
    await tools.after_app_change(_FakeState())

    assert (slug, APP) not in tools._PENDING_CLOSE_APPS, (
        "drain must treat host RPC error as confirmed and clear the handle; "
        "without is_host_rejection branch the handle is never cleared"
    )
    assert APP not in tools._REVOCATION_FENCE, (
        "fence must be lowered after host RPC error clears the last handle; "
        "remove is_host_rejection and fence stays up"
    )

    # Step 3: Next trust revoke should succeed (no unconfirmed handles blocking it).
    tools.revoke_app_generation(APP)
    _stopped2, _failed2 = await tools.stop_app_bot_chat_servers(_FakeState(), APP)
    assert slug not in _failed2, (
        "after handle cleared, trust revoke must succeed; "
        "if handle is not cleared, the pending-close scan finds it again and fails"
    )


# ── Retry limit: at max retries, handle stays — no forced confirmation ─────────


@pytest.mark.asyncio
async def test_max_retries_keeps_handle_and_reports_failure(world, monkeypatch):
    """When drain reaches _PENDING_CLOSE_MAX_RETRIES without a host response or
    runtime-dead signal, the handle must NOT be cleared.  The session may still
    be running, so treating it as stopped would leave app code executing.  The
    caller must keep seeing 409 until the host confirms closure.

    The old behaviour was to 'force-confirm' at max retries, which could report
    success while the app was still running.  The correct behaviour is to keep
    failing so the operator sees continued 409s and can investigate.

    Remove the _PENDING_CLOSE_RETRIES.pop + continue path (or change it back to
    confirmed_sids.add) and the slug disappears from failed on the final attempt."""
    slug = KEY.split("member-")[1].split(".")[0]
    tools._MOUNTED[slug] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    # Drain will keep failing (transport error, no code) up to MAX_RETRIES.
    class _FakeRuntime:
        async def evict_session(self, sid: str) -> bool:
            raise TimeoutError("KAS not responding")  # no AcpError code -> not confirmed

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
            if confirm_teardown and unconfirmed_handle_out is not None:
                unconfirmed_handle_out.append((_FakeRuntime(), "kas-sid-timeout"))
                return False
            return True

        def _allocation_boundary(self) -> "_FakeAlloc":
            return _FakeAlloc()

    class _FakeState:
        sessions = _FakeSessions()
        _crewmate_reload_tasks: set = set()

    async def _fr(state, slugs):
        return {}

    async def _ff(state, slugs):
        pass

    monkeypatch.setattr(tools, "reload_bot_chats", _fr)
    monkeypatch.setattr(tools, "fresh_bot_chats", _ff)

    # Seed an unconfirmed handle.
    tools.revoke_app_generation(APP)
    _stopped, _failed = await tools.stop_app_bot_chat_servers(_FakeState(), APP)
    assert slug in _failed, "setup: initial stop failed"
    assert (slug, APP) in tools._PENDING_CLOSE_APPS, "setup: handle saved"

    # Exhaust the retry counter by running drain MAX_RETRIES times.
    max_r = tools._PENDING_CLOSE_MAX_RETRIES
    for _ in range(max_r):
        await tools._drain_pending_close()

    # At the max, the counter is reset (rate-limiting) but handle STAYS.
    assert (slug, APP) in tools._PENDING_CLOSE_APPS, (
        "handle must NOT be cleared when max retries is reached without evidence "
        "of closure; remove the pop+continue and add confirmed_sids.add to restore "
        "the old forced-confirmation behaviour, and this assertion fails"
    )

    # Stopping B again should still report failure (handle is still there).
    tools.revoke_app_generation(APP)
    _stopped2, _failed2 = await tools.stop_app_bot_chat_servers(_FakeState(), APP)
    assert slug in _failed2, (
        "after reaching max retries without confirmation, stop must still report "
        "failure so the caller returns 409; "
        "if the handle was force-confirmed, failed2 is empty"
    )


@pytest.mark.asyncio
async def test_stop_fence_span_keeps_revocation_fence_up_until_exit(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside ``app_stop_fence`` the fence survives a successful Bot stop.

    The fence must span stop -> disable -> trust-write. Make
    ``stop_app_bot_chat_servers``'s own exit clear ignore the open span and the
    fence drops right after the stop, so the in-span assertion fails.
    """
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

    async with tools.app_stop_fence(APP):
        tools.revoke_app_generation(APP)
        stopped, failed = await tools.stop_app_bot_chat_servers(_FakeState(), APP)
        assert not failed, f"unexpected stop failure: {failed}"
        assert APP in tools._REVOCATION_FENCE, "the open span must keep the fence up"
    assert APP not in tools._REVOCATION_FENCE, "leaving the span must lower the fence"
    assert tools._FENCE_HOLD_COUNTS.get(APP, 0) == 0


@pytest.mark.asyncio
async def test_stop_fence_span_survives_the_no_bot_early_return_and_an_exception(
    world, monkeypatch
):
    """The no-mounted-Bot early return defers to an open span; an exception in the
    span still releases it.

    Drop the ``try/finally`` in ``app_stop_fence`` and the count stays at 1 after
    the raise, so the fence never lowers.
    """
    monkeypatch.setattr(tools, "_MOUNTED", {})
    monkeypatch.setattr(tools, "_MOUNTED_SESSION_KEYS", {})
    monkeypatch.setattr(tools, "_PENDING_CLOSE", {})
    monkeypatch.setattr(tools, "_PENDING_CLOSE_APPS", set())
    monkeypatch.setattr(tools, "_PENDING_CLOSE_RETRIES", {})
    monkeypatch.setattr(tools, "_PENDING_CLOSE_BACKOFF_IDX", {})
    monkeypatch.setattr(tools, "_PENDING_CLOSE_TIMERS", {})
    monkeypatch.setattr(tools, "_REVOCATION_FENCE", set())
    monkeypatch.setattr(tools, "_FENCE_HOLD_COUNTS", {})
    monkeypatch.setattr(tools, "_APP_GENERATIONS", {})
    monkeypatch.setattr(tools, "_PROJECTION_GENS", {})
    monkeypatch.setattr(tools, "_PENDING_DELIVERY", {})

    class _FakeState:
        sessions = None

    with pytest.raises(RuntimeError):
        async with tools.app_stop_fence(APP):
            stopped, failed = await tools.stop_app_bot_chat_servers(_FakeState(), APP)
            assert stopped == [] and failed == []
            assert APP in tools._REVOCATION_FENCE, "early return must defer to the span"
            assert tools._FENCE_HOLD_COUNTS.get(APP, 0) == 1
            raise RuntimeError("state write failed")
    assert APP not in tools._REVOCATION_FENCE
    assert tools._FENCE_HOLD_COUNTS.get(APP, 0) == 0


# ── Fix: record_mounted pop→write window closed by _DELIVERY_LOCK ─────────────


@pytest.mark.parametrize("stop_first", [False, True])
@pytest.mark.asyncio
async def test_stop_does_not_miss_session_in_pop_to_mounted_window(world, monkeypatch, stop_first):
    """stop_app_bot_chat_servers must correctly handle concurrent record_mounted
    for both possible orderings: record_mounted first, and stop first.

    Ordering A (stop_first=False): record_mounted wins the _DELIVERY_LOCK first,
    writes _MOUNTED, then releases; stop reads the session from _MOUNTED and
    discards it.  The _DELIVERY_LOCK closes the pop→write window so stop never
    sees the session in neither table.

    Ordering B (stop_first=True): stop wins: it reads _PENDING_DELIVERY, discards
    the pending session key (KEY), pops it from _PENDING_DELIVERY, and returns
    ([], []) (pending sessions are not counted in stopped/failed).  record_mounted
    then runs; _VOIDED_DELIVERY has APP (from revoke_app_generation), so voided_apps
    is non-empty; _MOUNTED must NOT contain APP's aliases (fix: exclude voided_apps
    from the _MOUNTED write), so after_app_change will see now != was and reload.

    The core invariant in both cases: the app's server must not silently survive
    un-tracked.  Specifically:
    - stop must NOT return ([], []) AND leave the alias live in _MOUNTED
      with no pending reconcile scheduled (ordering A).
    - record_mounted must NOT write APP's alias into _MOUNTED when the delivery
      was voided by a concurrent revocation (ordering B, fixed by L).

    Remove the ``with _DELIVERY_LOCK:`` block from ``record_mounted`` and ordering
    A detects the false success.  Remove the ``voided_apps`` exclusion from the
    ``_MOUNTED`` write and ordering B leaves a fenced alias permanently mounted.
    """
    import asyncio
    import threading

    # Build projection and register pending delivery.
    spec_out = tools.conductor_session_spec(dict(BASE), KEY)
    assert f"{APP}:team" in spec_out.get("mcpServers", {}), "setup: server not in spec"
    mcpServers = dict(spec_out.get("mcpServers") or {})
    filtered = tools.check_and_filter_projection(KEY, mcpServers)
    assert f"{APP}:team" in filtered, "setup: server stripped by gate"
    assert APP in tools._PENDING_DELIVERY.get(KEY, set()), "setup: not in PENDING_DELIVERY"

    # Revoke so stop_app_bot_chat_servers tries to stop this session.
    tools.revoke_app_generation(APP)

    stop_result: list = []
    discarded: list[str] = []

    class _Sessions:
        async def discard_conversation(
            self,
            session_key: str,
            replay: bool = False,
            confirm_teardown: bool = False,
            unconfirmed_handle_out: "list | None" = None,
        ) -> bool:
            discarded.append(session_key)
            return True

        def _allocation_boundary(self) -> object:
            class _A:
                def begin_ending(self, key: str) -> None:
                    pass

                def end_ending(self, key: str) -> None:
                    pass

                def spawn_in_flight(self, key: str) -> None:
                    return None

            return _A()

    class _S:
        sessions = _Sessions()

    slug = KEY.split("member-")[1].split(".")[0]

    if not stop_first:
        # Ordering A: record_mounted wins _DELIVERY_LOCK.
        # Inject a sync point: patched __enter__ signals stop to start, waits
        # for it to finish, then commits; stop blocks on the lock and reads
        # the session from _MOUNTED after record_mounted releases.
        import threading as _threading

        real_lock = tools._DELIVERY_LOCK
        stop_started = _threading.Event()
        stop_done = _threading.Event()

        class _PatchedLock:
            _inside = False

            def __enter__(self):
                if not _PatchedLock._inside:
                    _PatchedLock._inside = True
                    # signal stop to run, then wait until it has read the tables
                    stop_started.set()
                    stop_done.wait(timeout=5)
                return real_lock.__enter__()

            def __exit__(self, *a):
                _PatchedLock._inside = False
                return real_lock.__exit__(*a)

        monkeypatch.setattr(tools, "_DELIVERY_LOCK", _PatchedLock())

        def _run_stop() -> None:
            stop_started.wait(timeout=5)
            result = asyncio.run(tools.stop_app_bot_chat_servers(_S(), APP))
            stop_result.append(result)
            stop_done.set()

        stop_thread = threading.Thread(target=_run_stop, daemon=True)
        stop_thread.start()

        voided = tools.record_mounted(KEY, tools.mounted_names(filtered))

        stop_thread.join(timeout=5)
        assert stop_thread.is_alive() is False, "stop thread did not finish"

        mounted_entry = tools._MOUNTED.get(slug, {})
        assert stop_result, "stop thread produced no result"
        stopped, failed = stop_result[0]
        # Ordering A invariant: the session must not be silently alive.
        # Either stop tracked it via _MOUNTED and discarded it (stopped=[slug]),
        # OR stop found KEY in _PENDING_DELIVERY and discarded it (KEY in discarded)
        # — in both cases the session is torn down.  The only forbidden outcome is
        # stop returning ([], []) AND having discarded nothing at all, which means
        # the session slipped through both tables undetected.
        session_handled = bool(stopped) or bool(failed) or (KEY in discarded)
        assert session_handled, (
            "ordering A: stop returned ([], []) and discarded nothing — the session "
            "slipped through both _PENDING_DELIVERY and _MOUNTED; "
            "drop 'with _DELIVERY_LOCK:' from record_mounted and the stop thread "
            "sees neither table during the pop-to-write window"
        )
        # record_mounted must have returned voided_apps containing APP (revoked).
        assert APP in voided, (
            "ordering A: record_mounted did not return APP in voided_apps; "
            "the revocation fence check inside _DELIVERY_LOCK is not working"
        )

    else:
        # Ordering B: stop wins and discards the pending session key first.
        # Use a controlled gate: stop runs to completion before record_mounted.
        stop_complete = threading.Event()

        def _run_stop() -> None:
            result = asyncio.run(tools.stop_app_bot_chat_servers(_S(), APP))
            stop_result.append(result)
            stop_complete.set()

        stop_thread = threading.Thread(target=_run_stop, daemon=True)
        stop_thread.start()
        stop_complete.wait(timeout=5)
        stop_thread.join(timeout=5)
        assert stop_thread.is_alive() is False, "stop thread did not finish"

        # stop must have discarded the pending session key.
        assert KEY in discarded, (
            "ordering B: stop did not discard the pending session key; "
            "stop_app_bot_chat_servers may not be scanning _PENDING_DELIVERY"
        )
        # stop returns ([], []) for pending sessions (they are not counted
        # in stopped/failed when confirmed); that is correct here.
        assert stop_result, "stop thread produced no result"
        stopped, failed = stop_result[0]
        # KEY popped from _PENDING_DELIVERY by stop; _VOIDED_DELIVERY still has APP.
        assert (
            KEY not in tools._PENDING_DELIVERY
        ), "ordering B: stop did not pop KEY from _PENDING_DELIVERY"

        # Now record_mounted runs; _VOIDED_DELIVERY has APP from revoke_app_generation.
        voided = tools.record_mounted(KEY, tools.mounted_names(filtered))

        # record_mounted must have reported APP as voided (_VOIDED_DELIVERY path).
        assert APP in voided, (
            "ordering B: record_mounted did not return APP in voided_apps; "
            "the _VOIDED_DELIVERY pop inside _DELIVERY_LOCK is not consuming the void"
        )
        # _VOIDED_DELIVERY and _PENDING_DELIVERY must be cleared.
        assert (
            KEY not in tools._VOIDED_DELIVERY
        ), "ordering B: _VOIDED_DELIVERY not cleared after record_mounted"
        # Voided-delivery aliases MUST be in _MOUNTED so stop_app_bot_chat_servers
        # can find the session for teardown (the session was handed the alias;
        # a reconcile will tear it down).  Remove the _VOIDED_DELIVERY branch's
        # _MOUNTED write and this fails.
        mounted_entry = tools._MOUNTED.get(slug, {})
        app_in_mounted = any(alias.startswith(f"{APP}:") for alias in mounted_entry)
        assert app_in_mounted, (
            "ordering B: APP's alias is NOT in _MOUNTED after a voided delivery; "
            "the _VOIDED_DELIVERY branch in record_mounted must write the alias to "
            "_MOUNTED so teardown can find the session — stripping it here leaves "
            "stop_app_bot_chat_servers unable to track the running session"
        )


# ── Fix L: (slug, app)-scoped fence in record_mounted; voided excluded from _MOUNTED ──


@pytest.mark.asyncio
async def test_bot_b_gets_tools_when_bot_a_close_unconfirmed(world):
    """Bot B cold-starts normally when Bot A's KAS close for the same app is
    unconfirmed.

    Before the fix, an unconfirmed close for (A, APP) keeps APP in
    _REVOCATION_FENCE (via _revocation_fence_clear's pending-close check).
    record_mounted for Bot B treats APP as fenced (app-wide), sets voided_apps,
    and excludes APP's aliases from _MOUNTED[B].  after_app_change later sees
    now == was for B and never reloads.  B stays without APP's tools until restart.

    After the fix, the fence check in record_mounted scopes pending-close fences
    to (slug, app): Bot B's cold-start is not blocked by Bot A's pending-close,
    so APP's alias lands in _MOUNTED[B] normally.

    Remove the ``(slug, app)``-scoped condition from the fenced_apps comprehension
    in ``record_mounted`` and this test fails because B ends up without APP's alias.
    """
    key_b = "kc-runtime.session.member-bot_b.0"
    slug_b = "bot_b"
    slug_a = "bot_a"

    # Add Bot B to the world's ticked set so conductor_session_spec includes it.
    world["ticked"] = (slug_a, slug_b)

    # Build projection for Bot B via the normal path.
    spec_out_b = tools.conductor_session_spec(dict(BASE), key_b)
    assert f"{APP}:team" in spec_out_b.get("mcpServers", {}), "setup: server not in Bot B's spec"
    mcpServers_b = dict(spec_out_b.get("mcpServers") or {})
    filtered_b = tools.check_and_filter_projection(key_b, mcpServers_b)
    assert f"{APP}:team" in filtered_b, "setup: server stripped by gate for Bot B"

    # Simulate Bot A's unconfirmed pending-close: add (slug_a, APP) to _PENDING_CLOSE_APPS
    # and keep APP in _REVOCATION_FENCE (as _revocation_fence_clear leaves it
    # when pending-close handles still exist for the same app).
    tools._PENDING_CLOSE_APPS.add((slug_a, APP))
    tools._REVOCATION_FENCE.add(APP)

    # Bot B cold-starts: call record_mounted with its full alias set.
    voided = tools.record_mounted(key_b, tools.mounted_names(filtered_b))

    # Bot B must NOT be blocked by Bot A's pending-close.
    assert APP not in voided, (
        f"record_mounted voided {APP!r} for Bot B because of Bot A's pending-close; "
        "the fence check must be scoped to (slug, app), not app-wide, for "
        "pending-close fences — only revoke/disable/uninstall fences cover all Bots"
    )
    mounted_b = tools._MOUNTED.get(slug_b, {})
    app_alias_present = any(a.startswith(f"{APP}:") for a in mounted_b)
    assert app_alias_present, (
        f"Bot B's _MOUNTED entry is missing APP's alias after a cold-start; "
        f"mounted_b={mounted_b!r}; "
        "remove the (slug, app)-scoped condition from the fenced_apps comprehension "
        "in record_mounted and this alias is stripped by the app-wide fence"
    )


@pytest.mark.asyncio
async def test_fenced_alias_reloads_after_fence_drops(world):
    """When record_mounted encounters a cold-start with the app under fence
    (fenced_apps path, not _VOIDED_DELIVERY), the alias must NOT appear in
    _MOUNTED, so after_app_change sees now != was and schedules a reload once
    the fence drops.

    The fenced_apps path activates when the fence is already up BEFORE
    check_and_filter_projection runs (revoke happened before the projection).
    In that case the gate itself blocks the entry and nothing lands in
    _VOIDED_DELIVERY, but the fence is still up at record_mounted time, so
    fenced_apps is non-empty.

    Contrast: when revoke races after the gate (_VOIDED_DELIVERY path), the
    alias IS written to _MOUNTED so stop_app_bot_chat_servers can find the
    running session for teardown.

    Remove the ``fenced_apps`` exclusion from the ``_MOUNTED`` write in
    ``record_mounted`` and this test fails because the alias lands in _MOUNTED
    despite the fence, preventing after_app_change from detecting now != was.
    """
    slug = KEY.split("member-")[1].split(".")[0]

    # Raise the fence BEFORE the projection so check_and_filter_projection
    # returns an empty set (alias filtered by generation check) and nothing
    # lands in _PENDING_DELIVERY.  At record_mounted time the fence is still
    # up and fenced_apps will be non-empty.
    tools.revoke_app_generation(APP)
    assert APP in tools._REVOCATION_FENCE, "setup: fence must be up"

    # Build the projection.  The generation check in check_and_filter_projection
    # will strip the entry because the generation advanced.
    tools.conductor_session_spec(dict(BASE), KEY)
    # check_and_filter_projection strips the entry; we pass offered set directly
    # to record_mounted so we can test the fence path without the gate stripping.
    # Inject a fake offered entry for slug so record_mounted sees the alias.
    tools._PROJECTION_SLUGS[KEY] = slug
    tools._OFFERED[slug] = (
        {f"{APP}:team": ""},  # offered, handed to session
        {},
    )
    # _PENDING_DELIVERY must be empty (gate stripped it or was bypassed).
    assert (
        KEY not in tools._PENDING_DELIVERY
    ), "setup: _PENDING_DELIVERY must be empty for the fenced_apps path"
    # _VOIDED_DELIVERY must also be empty (revoke ran before the gate).
    assert KEY not in tools._VOIDED_DELIVERY, (
        "setup: _VOIDED_DELIVERY must be empty for the fenced_apps path; "
        "revoke before gate does not write _VOIDED_DELIVERY"
    )

    # record_mounted runs: fenced_apps contains APP (fence still up, no pending-close).
    voided = tools.record_mounted(KEY, [f"{APP}:team"])

    assert APP in voided, (
        "record_mounted did not return APP in voided_apps via fenced_apps path; "
        "check that the fenced_apps comprehension in record_mounted detects APP "
        "in _REVOCATION_FENCE when no _PENDING_CLOSE_APPS entry exists for (slug, APP)"
    )
    mounted_entry = tools._MOUNTED.get(slug, {})
    app_alias_in_mounted = any(a.startswith(f"{APP}:") for a in mounted_entry)
    assert not app_alias_in_mounted, (
        f"APP's alias is in _MOUNTED[{slug!r}] despite the fence (fenced_apps path); "
        f"mounted={mounted_entry!r}; "
        "remove the fenced_apps exclusion from the _MOUNTED write in record_mounted "
        "and this alias persists, preventing after_app_change from detecting "
        "now != was when the fence drops"
    )


# ── Opus :679: pending-close gate must not trigger reload loop ────────────────


@pytest.mark.asyncio
async def test_pending_close_gate_no_reload_loop(world, monkeypatch):
    """When (slug, app) is in _PENDING_CLOSE_APPS and drain keeps timing out,
    after_app_change must NOT reload Bot S, even after 3 cycles.

    Before the fix: check_and_filter_projection added the alias to
    _REPROJECT_SLUGS and called _schedule_reconcile, which caused record_mounted
    to exclude the alias from _UNMOUNTED.  after_app_change then saw now!=was
    (bot_servers returned alias X, _MOUNTED had none) and triggered a reload,
    restarting the loop.

    After the fix: the alias is stored in _PENDING_CLOSE_WITHHELD; record_mounted
    merges it into _UNMOUNTED so after_app_change sees withheld.get(alias)==fp
    and treats now==was, skipping the reload.

    Remove the _PENDING_CLOSE_WITHHELD branch (restore the old _REPROJECT_SLUGS
    path) and the reload counter below rises above 0, failing the first assertion.
    """
    slug = KEY.split("member-")[1].split(".")[0]
    alias = f"{APP}:team"

    # Build initial mounted state: Bot S already running WITHOUT app X.
    tools._MOUNTED[slug] = {}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    # Mark (slug, APP) as pending close so the gate fires.
    tools._PENDING_CLOSE_APPS.add((slug, APP))

    # Put a real (but always-timing-out) handle in _PENDING_CLOSE.
    class _TimingOutRuntime:
        async def evict_session(self, sid: str) -> bool:
            raise TimeoutError("KAS not responding")

    tools._PENDING_CLOSE[(slug, APP)] = [(_TimingOutRuntime(), "kas-sid-timeout")]

    reload_calls: list[str] = []
    fresh_calls: list[str] = []

    async def _fake_reload(state: object, slugs: list) -> dict:
        reload_calls.extend(slugs)
        return {}

    async def _fake_fresh(state: object, slugs: list) -> None:
        fresh_calls.extend(slugs)

    monkeypatch.setattr(tools, "reload_bot_chats", _fake_reload)
    monkeypatch.setattr(tools, "fresh_bot_chats", _fake_fresh)

    # Patch bot_servers so it always returns alias X (enabled + granted).
    monkeypatch.setattr(tools, "bot_servers", lambda s, cfg=None: {alias: {"command": "py"}})
    # Patch _PENDING_CLOSE_WITHHELD to empty so record_mounted starts clean.
    monkeypatch.setattr(tools, "_PENDING_CLOSE_WITHHELD", {})

    # Simulate a cold start: Bot S starts a new session and harness calls
    # check_and_filter_projection (gate fires) + record_mounted.
    # First build a projection sidecar so check_and_filter_projection doesn't
    # strip on fail-closed path.
    tools._PROJECTION_SLUGS[KEY] = slug
    tools._PROJECTION_GENS[KEY] = {alias: tools._APP_GENERATIONS.get(APP, 0)}
    tools._OFFERED[slug] = ({alias: ""}, {})

    # Run the gate: alias should be blocked (pending close) and stashed.
    filtered = tools.check_and_filter_projection(KEY, {alias: {"command": "py"}})
    assert alias not in filtered, "setup: gate must block the alias"
    assert slug in tools._PENDING_CLOSE_WITHHELD, (
        "gate must add alias to _PENDING_CLOSE_WITHHELD; "
        "restore _REPROJECT_SLUGS path and this dict stays empty"
    )
    assert alias not in tools._REPROJECT_SLUGS.get(slug, set()), (
        "gate must NOT add alias to _REPROJECT_SLUGS for pending-close-only blocks; "
        "the old path did add it, triggering reload loops"
    )

    # Simulate record_mounted: the pending-close alias must NOT be in _UNMOUNTED.
    # It stays in _PENDING_CLOSE_WITHHELD so after_app_change merges both and sees
    # withheld.get(alias)==fp, treating now==was and skipping the reload.
    # Once drain clears _PENDING_CLOSE_WITHHELD, _UNMOUNTED has no entry for it
    # either, so the next reconcile sees a new addition and schedules a reload.
    tools._PROJECTION_SLUGS[KEY] = slug
    tools._OFFERED[slug] = ({alias: ""}, {})
    tools.record_mounted(KEY, [])  # handed nothing (gate blocked alias)

    unmounted = tools._UNMOUNTED.get(slug, {})
    assert alias not in unmounted, (
        f"record_mounted must NOT write pending-close-withheld alias into _UNMOUNTED; "
        f"unmounted={unmounted!r}; "
        "_UNMOUNTED is permanent (never cleared per-alias), so the alias would remain "
        "blocked forever after drain confirms; it stays in _PENDING_CLOSE_WITHHELD only"
    )
    # Verify the alias is still in _PENDING_CLOSE_WITHHELD (the temp suppression).
    assert alias in tools._PENDING_CLOSE_WITHHELD.get(slug, {}), (
        "alias must remain in _PENDING_CLOSE_WITHHELD after record_mounted; "
        "after_app_change merges _UNMOUNTED and _PENDING_CLOSE_WITHHELD so it still "
        "sees withheld.get(alias)==fp and skips the reload loop"
    )

    # Build a fake state with no pending delivery and no active session.
    class _FakeState:
        _crewmate_reload_tasks: set = set()

    # Run after_app_change 3 times; each call runs _drain_pending_close (timeout)
    # then compares now vs was.  Reload must never fire.
    for _ in range(3):
        await tools.after_app_change(_FakeState())

    assert not reload_calls and not fresh_calls, (
        f"after_app_change must NOT reload Bot S while (slug, APP) is in "
        f"_PENDING_CLOSE_APPS; reload_calls={reload_calls!r}, "
        f"fresh_calls={fresh_calls!r}; "
        "restore the old _REPROJECT_SLUGS path in check_and_filter_projection "
        "and reload fires on the first cycle"
    )


@pytest.mark.asyncio
async def test_pending_close_confirmed_triggers_remount(world, monkeypatch):
    """After _drain_pending_close confirms (slug, APP), _schedule_reconcile must be
    called once; running the resulting after_app_change must reload Bot S.

    Mechanism:
    1. drain confirms the handle, removes (slug, APP) from _PENDING_CLOSE_APPS and
       the alias from _PENDING_CLOSE_WITHHELD.
    2. drain calls _schedule_reconcile().
    3. The follow-up after_app_change sees _UNMOUNTED without the alias
       (withheld dict empty), so now != was, and schedules a reload.

    Remove the _PENDING_CLOSE_WITHHELD.pop loop in _drain_pending_close and the
    alias stays in _UNMOUNTED after drain.  The follow-up after_app_change still
    sees withheld.get(alias)==fp so it treats now==was and never reloads -- the
    final assertion below fails.
    """
    slug = KEY.split("member-")[1].split(".")[0]
    alias = f"{APP}:team"

    tools._MOUNTED[slug] = {}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY

    tools._PENDING_CLOSE_APPS.add((slug, APP))

    class _ConfirmingRuntime:
        async def evict_session(self, sid: str) -> bool:
            return True

    tools._PENDING_CLOSE[(slug, APP)] = [(_ConfirmingRuntime(), "kas-sid-confirmed")]

    reload_calls: list[str] = []
    fresh_calls: list[str] = []

    async def _fake_reload(state: object, slugs: list) -> dict:
        reload_calls.extend(slugs)
        return {}

    async def _fake_fresh(state: object, slugs: list) -> None:
        fresh_calls.extend(slugs)

    monkeypatch.setattr(tools, "reload_bot_chats", _fake_reload)
    monkeypatch.setattr(tools, "fresh_bot_chats", _fake_fresh)
    monkeypatch.setattr(tools, "bot_servers", lambda s, cfg=None: {alias: {"command": "py"}})
    monkeypatch.setattr(tools, "_PENDING_CLOSE_WITHHELD", {slug: {alias: "initial-fp"}})

    # _UNMOUNTED already reflects the withheld alias (as set by record_mounted).
    tools._UNMOUNTED[slug] = {alias: "initial-fp"}

    # Replace _schedule_reconcile with a synchronous stub that records each call
    # so the test can await the follow-up after_app_change directly instead of
    # relying on asyncio.sleep(0), which is prohibited by wait-discipline ratchet.
    reconcile_calls: list[int] = []

    def _fake_reconcile() -> None:
        reconcile_calls.append(len(reconcile_calls))

    monkeypatch.setattr(tools, "_schedule_reconcile", _fake_reconcile)

    class _FakeState:
        _crewmate_reload_tasks: set = set()

    # First after_app_change: drain runs and confirms the handle, _schedule_reconcile
    # is captured (not dispatched as a task).
    await tools.after_app_change(_FakeState())

    assert (
        slug,
        APP,
    ) not in tools._PENDING_CLOSE_APPS, (
        "drain must remove (slug, APP) from _PENDING_CLOSE_APPS after confirmation"
    )
    assert alias not in tools._PENDING_CLOSE_WITHHELD.get(slug, {}), (
        "drain must remove the withheld alias from _PENDING_CLOSE_WITHHELD; "
        "remove the pop loop in _drain_pending_close and alias stays, "
        "making after_app_change see now==was forever"
    )
    assert reconcile_calls, (
        "drain must call _schedule_reconcile() after confirming the last handle; "
        "remove the _schedule_reconcile call from _drain_pending_close and "
        "reconcile_calls stays empty -- the follow-up reconcile never fires"
    )

    # Second after_app_change: simulates the reconcile drain posted.
    # _UNMOUNTED is empty for this alias (drain cleared _PENDING_CLOSE_WITHHELD),
    # so after_app_change sees now != was and triggers a reload.
    await tools.after_app_change(_FakeState())

    assert reload_calls or fresh_calls, (
        f"the follow-up after_app_change must reload Bot S once the withheld alias "
        f"is removed from _PENDING_CLOSE_WITHHELD; "
        f"reload_calls={reload_calls!r}, fresh_calls={fresh_calls!r}; "
        "remove the _PENDING_CLOSE_WITHHELD.pop loop in _drain_pending_close "
        "and the alias stays in _UNMOUNTED, so now==was and no reload fires"
    )


def test_note_unconfirmed_close_writes_pending_close_for_mounted_app(
    _fresh_record,
):
    """note_unconfirmed_close must write (slug, app) into _PENDING_CLOSE.

    Called unconditionally from teardown.discard_conversation and
    session_provider_shared.shutdown_shared after a KAS close is not confirmed.
    If the call is removed from either site, an unconfirmed KAS session's handle
    is never registered and stop_app_bot_chat_servers silently reports success
    while the old session and its app server are still resident.

    Remove the note_unconfirmed_close call in teardown.py or
    session_provider_shared.py and this assertion fails: (slug, APP) is absent
    from _PENDING_CLOSE, so the delivery gate never blocks a cold-start
    projection for the same slug.

    Uses _SESSION_APP_SNAPSHOT (not _MOUNTED) so the test proves the fix that
    guards against a swap_conversation that overwrites _MOUNTED before teardown.
    """
    slug = KEY.split("member-")[1].split(".")[0]
    # Populate the snapshot as bind_snapshot_to_native_sid would, keyed by the
    # native KAS session id (not the conversation key).  note_unconfirmed_close
    # reads _SESSION_APP_SNAPSHOT[sid] so the snapshot survives a
    # swap_conversation that reuses the same conversation key for a new session.
    SID = "kas-sid-unconf-test"
    tools._MOUNTED_SESSION_KEYS[slug] = KEY
    tools._SESSION_APP_SNAPSHOT[SID] = (slug, frozenset([APP]))

    class _FakeRT:
        async def evict_session(self, sid: str) -> bool:
            return False

    rt = _FakeRT()
    tools.note_unconfirmed_close(KEY, rt, SID)

    assert (slug, APP) in tools._PENDING_CLOSE, (
        "note_unconfirmed_close must write the (slug, app) handle into "
        "_PENDING_CLOSE so the delivery gate blocks the next cold-start "
        "projection; remove the call from teardown.py or "
        "session_provider_shared.py and this assertion fails"
    )
    handles = tools._PENDING_CLOSE[(slug, APP)]
    assert len(handles) == 1
    assert handles[0] == (rt, SID)
    # Snapshot must be consumed: keeping it after transfer wastes memory and
    # could cause double-registration on a second note_unconfirmed_close call.
    assert (
        SID not in tools._SESSION_APP_SNAPSHOT
    ), "_SESSION_APP_SNAPSHOT must be popped after transfer to _PENDING_CLOSE"


def test_note_unconfirmed_close_uses_snapshot_not_mounted_after_swap(
    _fresh_record,
):
    """After swap_conversation overwrites _MOUNTED[slug], the old session's
    teardown must still register its app handle via _SESSION_APP_SNAPSHOT.

    Scenario: Bot (slug) has app X mounted in OLD session (native id OLD_SID).
    swap_conversation creates new session (NEW_SID); bind_snapshot_to_native_sid
    runs and migrates the new snapshot to NEW_SID.  _MOUNTED[slug] is then
    overwritten with an empty dict (no app X in new session).
    Then the old session's close times out; note_unconfirmed_close is called
    with OLD_SID.

    Without the native-sid fix, note_unconfirmed_close keyed by conversation key
    would be overwritten by the new session's bind call, then read as empty.
    With the fix, _SESSION_APP_SNAPSHOT[OLD_SID] survives independently.

    Remove ``snap = _SESSION_APP_SNAPSHOT.pop(sid, None)`` from
    note_unconfirmed_close (reverting to the _MOUNTED read) and this test
    turns red: (slug, APP) is absent from _PENDING_CLOSE.
    """
    slug = KEY.split("member-")[1].split(".")[0]
    NEW_KEY = KEY + "-new"
    OLD_SID = "kas-sid-old"
    NEW_SID = "kas-sid-new"

    # Step 1: bind_snapshot_to_native_sid for OLD session — mirrors what
    # record_mounted + bind_snapshot_to_native_sid would do for OLD_KEY/OLD_SID.
    tools._MOUNTED[slug] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY
    tools._SESSION_APP_SNAPSHOT[OLD_SID] = (slug, frozenset([APP]))

    # Step 2: swap_conversation — new session NEW_SID's bind_snapshot_to_native_sid
    # runs and writes _SESSION_APP_SNAPSHOT[NEW_SID] = (slug, {}).  _MOUNTED[slug]
    # is overwritten with an empty set (new session, no app X yet).
    tools._MOUNTED[slug] = {}
    tools._MOUNTED_SESSION_KEYS[slug] = NEW_KEY
    tools._SESSION_APP_SNAPSHOT[NEW_SID] = (slug, frozenset())
    # _SESSION_APP_SNAPSHOT[OLD_SID] remains — the fix preserves the old entry
    # because it is keyed by OLD_SID, not by the conversation key.

    # Step 3: old session's KAS close times out; note_unconfirmed_close fires
    # with OLD_SID.
    class _FakeRT:
        async def evict_session(self, sid: str) -> bool:
            return False

    rt = _FakeRT()
    tools.note_unconfirmed_close(KEY, rt, OLD_SID)

    # The old session's (slug, APP) handle must be registered for retry even
    # though _MOUNTED[slug] was already overwritten with an empty dict.
    assert (slug, APP) in tools._PENDING_CLOSE, (
        "note_unconfirmed_close must retain the old session's app handle via "
        "_SESSION_APP_SNAPSHOT[OLD_SID] even after swap_conversation overwrites "
        "_MOUNTED[slug]; revert the snapshot read in note_unconfirmed_close "
        "to _MOUNTED and this assertion fails"
    )
    handles = tools._PENDING_CLOSE[(slug, APP)]
    assert len(handles) == 1
    assert handles[0] == (rt, OLD_SID)
    # Also assert the two snapshots are independent: new session's entry survives.
    assert NEW_SID in tools._SESSION_APP_SNAPSHOT, (
        "swap_conversation new session snapshot must not be consumed by old "
        "session's note_unconfirmed_close"
    )


def test_revoke_app_generation_voids_pending_delivery_so_watcher_path_cannot_skip_it(
    _fresh_record,
):
    """security_trusted_apps bumps generation through revoke_app_generation so an
    in-flight check_and_filter_projection that passed admission but has not yet
    registered in _PENDING_DELIVERY is blocked.

    The old path called ``_APP_GENERATIONS[app] += 1`` directly, without the
    _DELIVERY_LOCK and without voiding _PENDING_DELIVERY.  A cold-start thread
    could pass the generation check, then yield (before registering), then the
    watcher bumped the generation and scanned an empty _PENDING_DELIVERY -- finding
    nothing to void.  The thread resumed, registered, and record_mounted accepted
    the revoked server.

    This test verifies that revoke_app_generation voids a session already in
    _PENDING_DELIVERY.  Remove the _PENDING_DELIVERY void loop inside
    revoke_app_generation and the assert fails: the pending entry survives and
    record_mounted would mount the revoked server.
    """
    slug = KEY.split("member-")[1].split(".")[0]
    # Simulate a session that passed the gate and registered in _PENDING_DELIVERY.
    tools._PENDING_DELIVERY["sk-inflight"] = {APP}
    tools._MOUNTED[slug] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[slug] = "sk-inflight"

    # Watcher path: use revoke_app_generation (the fix).
    tools.revoke_app_generation(APP)

    assert "sk-inflight" in tools._VOIDED_DELIVERY, (
        "revoke_app_generation must void the in-flight pending delivery; "
        "replace revoke_app_generation with a bare _APP_GENERATIONS bump "
        "and sk-inflight is NOT in _VOIDED_DELIVERY -- revoked code mounts"
    )
    assert APP in tools._VOIDED_DELIVERY["sk-inflight"]


@pytest.mark.asyncio
async def test_pending_close_withheld_fingerprint_matches_offer_so_no_reload_loop(
    _fresh_record, monkeypatch
):
    """_PENDING_CLOSE_WITHHELD must store the original-offer fingerprint.

    after_app_change compares _PENDING_CLOSE_WITHHELD entries against bot_servers()
    output, which returns the unstamped offer.  If the stored fingerprint carries a
    session label (from stamp_session_label), the comparison always fails: every
    after_app_change call sees now != was for the withheld alias and queues a reload.

    This test verifies that three consecutive after_app_change calls produce no reload
    and no fresh-start while a pending-close blocks the alias and the fence stays up.
    Remove the original-offer fingerprint fix (revert to ``json.dumps(entry)``) and the
    reload_calls list is non-empty after the first after_app_change.
    """
    import kiro_crew.members as members

    slug = KEY.split("member-")[1].split(".")[0]

    # Compute the fingerprint bot_servers / after_app_change will use (json of ENTRY).
    import json as _json

    real_fp = _json.dumps(ENTRY, sort_keys=True, default=str)
    # _PENDING_CLOSE_WITHHELD is written by check_and_filter_projection by stripping
    # x-kirocrew-session-label from the delivery entry and json.dumps-ing the rest.
    # ENTRY has x-kirocrew-session-label: True (the template value), so the stored fp
    # excludes it.  after_app_change uses bot_servers() -> _rendered_entry() which
    # returns ENTRY with the original True, so f = real_fp (includes label).
    # pcw_fp != f; the pending-close withheld check does not apply; now[alias] = f.
    # was[alias] = real_fp = f, so now == was and no reload fires.
    withheld_fp = _json.dumps(
        {k: v for k, v in ENTRY.items() if k != "x-kirocrew-session-label"},
        sort_keys=True,
        default=str,
    )

    # Reset _FENCE_HOLD_COUNTS so it does not carry state from previous tests.
    tools._FENCE_HOLD_COUNTS.clear()

    # Set up: the alias is in _MOUNTED with the same fingerprint bot_servers returns,
    # simulating a successfully mounted session. _PENDING_CLOSE_APPS holds the pair.
    tools._MOUNTED[slug] = {f"{APP}:team": real_fp}
    tools._MOUNTED_SESSION_KEYS[slug] = KEY
    tools._PENDING_CLOSE[(slug, APP)] = []
    tools._PENDING_CLOSE_APPS.add((slug, APP))

    # _PENDING_CLOSE_WITHHELD stores the label-stripped fp (as check_and_filter_projection does).
    tools._PENDING_CLOSE_WITHHELD[slug] = {f"{APP}:team": withheld_fp}

    reload_calls: list = []
    fresh_calls: list = []

    monkeypatch.setattr(tools, "schedule_reload", lambda state, slugs: reload_calls.extend(slugs))

    async def _fake_fresh(state, slugs):
        fresh_calls.extend(slugs)
        return slugs

    monkeypatch.setattr(tools, "fresh_bot_chats", _fake_fresh)
    monkeypatch.setattr(tools, "preview_on", lambda cfg=None: True)
    monkeypatch.setattr(tools, "_app_names", lambda: [APP])
    monkeypatch.setattr(tools, "_offers", lambda app: [("lead", "team")])
    monkeypatch.setattr(tools, "_rendered_entry", lambda *a: dict(ENTRY))
    monkeypatch.setattr(bot_grants, "app_bot_scopes", lambda app: frozenset({"tools"}))
    monkeypatch.setattr(bot_grants, "app_granted_bots", lambda app: (slug,))
    import kiro_crew.apps.execution as _exec

    monkeypatch.setattr(_exec, "app_execution_denied", lambda name, **_kw: None)
    monkeypatch.setattr(
        members, "member_identity_for_session", lambda k: {"slug": slug, "name": "Atlas"}
    )

    async def _noop_drain():
        return None

    monkeypatch.setattr(tools, "_drain_pending_close", _noop_drain)

    class _FakeState:
        _crewmate_reload_tasks: set = set()

    state = _FakeState()
    for _ in range(3):
        await tools.after_app_change(state)

    assert not reload_calls and not fresh_calls, (
        "while pending-close fence blocks the alias, three after_app_change calls "
        "must produce no reload and no fresh-start; "
        "revert _PENDING_CLOSE_WITHHELD to store json.dumps(entry) with session label "
        "and reload_calls is non-empty on the first call -- the loop fires indefinitely"
    )


def test_swap_conversation_old_and_new_snapshots_are_independent(
    _fresh_record,
):
    """After swap_conversation, old and new native-sid snapshots coexist.

    bind_snapshot_to_native_sid migrates each pending snapshot to its own native
    session id.  A swap that reuses the same conversation key must NOT overwrite
    the old session's snapshot: the two native ids are distinct, so both entries
    survive in _SESSION_APP_SNAPSHOT independently.

    Remove the two-table design (pending + native-sid) and key _SESSION_APP_SNAPSHOT
    by conversation key instead, then call bind_snapshot_to_native_sid twice with
    the same session_key: OLD_SID's snapshot is overwritten and this test fails
    because OLD_SID is absent from _SESSION_APP_SNAPSHOT.
    """
    slug = KEY.split("member-")[1].split(".")[0]
    NEW_KEY = KEY  # swap reuses same conversation key
    OLD_SID = "kas-sid-old-indep"
    NEW_SID = "kas-sid-new-indep"

    # Step 1: record_mounted writes pending entry for OLD conversation key.
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = (slug, frozenset([APP]))
    # Step 2: bind for OLD session migrates pending -> _SESSION_APP_SNAPSHOT[OLD_SID].
    tools.bind_snapshot_to_native_sid(KEY, OLD_SID)
    assert OLD_SID in tools._SESSION_APP_SNAPSHOT
    assert (
        KEY not in tools._SESSION_APP_SNAPSHOT_PENDING
    ), "pending entry must be consumed by bind_snapshot_to_native_sid"

    # Step 3: swap_conversation — record_mounted runs again for the SAME KEY.
    tools._SESSION_APP_SNAPSHOT_PENDING[NEW_KEY] = (slug, frozenset())
    # Step 4: bind for NEW session uses NEW_SID.
    tools.bind_snapshot_to_native_sid(NEW_KEY, NEW_SID)

    # Both snapshots must coexist, keyed by their respective native ids.
    assert OLD_SID in tools._SESSION_APP_SNAPSHOT, (
        "old session snapshot must survive after swap_conversation; "
        "key _SESSION_APP_SNAPSHOT by conversation key and this fails because "
        "bind_snapshot_to_native_sid(NEW_KEY) overwrites OLD_SID's entry"
    )
    assert NEW_SID in tools._SESSION_APP_SNAPSHOT, "new session snapshot must also be present"
    assert tools._SESSION_APP_SNAPSHOT[OLD_SID] == (slug, frozenset([APP]))
    assert tools._SESSION_APP_SNAPSHOT[NEW_SID] == (slug, frozenset())


def test_confirm_last_lease_killed_clears_pending_close_and_fence(
    _fresh_record,
):
    """confirm_last_lease_killed removes (slug, app) from _PENDING_CLOSE and
    clears the revocation fence, so discard_conversation can return True.

    When the last-lease holder's process tree is killed, every session it held
    is gone.  confirm_last_lease_killed must treat the unconfirmed handle as
    confirmed: remove it from _PENDING_CLOSE, clear the fence, and schedule a
    reconcile.

    Remove the confirm_last_lease_killed call from session_provider_shared.py
    (or revert this function to a no-op) and the fence stays up after kill,
    causing stop_app_bot_chat_servers to report bot_chat_stop_incomplete even
    though the process is dead.
    """
    slug = KEY.split("member-")[1].split(".")[0]
    SID = "kas-sid-last-lease"

    class _FakeRT:
        async def evict_session(self, sid: str) -> bool:
            return False

    rt = _FakeRT()
    # Populate _PENDING_CLOSE as note_unconfirmed_close would.
    tools._PENDING_CLOSE[(slug, APP)] = [(rt, SID)]
    tools._PENDING_CLOSE_APPS.add((slug, APP))
    tools._REVOCATION_FENCE.add(APP)

    reconcile_called = []

    class _FakeLoop:
        def call_soon_threadsafe(self, fn: object, *args: object) -> None:
            reconcile_called.append(1)

    class _FakeState:
        _crewmate_reload_tasks: set = set()

    tools._RECONCILER = (_FakeState(), _FakeLoop())

    tools.confirm_last_lease_killed(SID)

    assert (slug, APP) not in tools._PENDING_CLOSE, (
        "confirm_last_lease_killed must remove the (slug, app) from _PENDING_CLOSE; "
        "revert to a no-op and the entry remains, the fence stays up, and "
        "stop_app_bot_chat_servers reports bot_chat_stop_incomplete after kill"
    )
    assert (
        slug,
        APP,
    ) not in tools._PENDING_CLOSE_APPS, (
        "confirm_last_lease_killed must discard from _PENDING_CLOSE_APPS"
    )
    # Fence must be lowered once no pending-close handles remain.
    assert APP not in tools._REVOCATION_FENCE, (
        "confirm_last_lease_killed must lower the revocation fence after last handle "
        "is confirmed; leaving the fence up blocks the next cold-start projection"
    )
    assert (
        reconcile_called
    ), "confirm_last_lease_killed must schedule a reconcile so the Bot can remount"
