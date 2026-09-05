"""Enforcement of the ``approval_modes`` policy at every ``yolo`` arming surface.

``test_approval_modes_governance.py`` pins the *predicate*
(``approval_mode_permitted`` reading the boot-frozen ceiling). This module pins the
places that must CONSULT it, because a mode the policy denies is only actually off
if every path that can arm it -- and every path that HONOURS an existing grant --
refuses:

* ``POST /api/chat/mode`` -- the explicit session-mode switch (403, no mutation).
* ``safety_override`` arming -- session-wide and scoped.
* ``is_active`` / ``is_scope_active`` / ``renew_scoped`` -- the consult points that
  honour a LIVE grant, so a mid-session deny revokes rather than waiting for a TTL.

The scope governs ``yolo`` only. ``trust`` / ``trust_reads`` are non-deniable (a
policy naming them is refused at parse time), because their consumption predicates
are not gated -- so there is deliberately nothing here asserting Trust enforcement.

Every refusal must also be SEL-audited: a governance denial that leaves no trace is
indistinguishable from the request never having been made, which is exactly the
record an operator needs after an attempted escalation.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import safety_override as so_mod
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.governance import parse_policy


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    d.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", d)
    # Reset on BOTH sides, and reset the context too. Every piece of state these
    # cases touch is module-level: the profile store, the boot-frozen context, and
    # the YOLO verdict cache -- which has a WALL-CLOCK TTL. Resetting only on
    # teardown leaves each case at the mercy of whatever ran before it in the same
    # worker, which is an order-dependent result that says nothing about the code
    # under test. Cleaning up on entry is what makes a case mean the same thing
    # alone and inside the file.
    # Wait out a resolve an earlier case left in flight BEFORE installing
    # anything. It runs on a worker thread and its governance read installs the
    # lazy default context as a side effect, so one that lands after this case has
    # installed its ceiling REPLACES that ceiling -- and the deny then silently
    # stops applying in a case that never touched a thread itself. Taking and
    # releasing the resolve lock is precisely "no resolve is in flight"; it is held
    # across the whole resolve, so this cannot return while one is running.
    with so_mod._yolo_policy_lock:
        pass
    gp.reset_store()
    ctx_mod.reset_context()
    so_mod.reset_yolo_policy_cache()
    so_mod.reset_singleton()
    yield
    so_mod.reset_yolo_policy_cache()
    so_mod.reset_singleton()
    gp.reset_store()
    ctx_mod.reset_context()


@contextlib.asynccontextmanager
async def _refresh_in_flight(monkeypatch):
    """Hold a REAL in-flight refresh record for the currently running loop.

    The scheduler suppresses a duplicate only when the record names THIS loop and
    its task is not done, so a stand-in has to satisfy both. A pending task on the
    live loop is the honest way to say "a refresh is already running"; the boolean
    these cases used before could be set from anywhere and stopped meaning anything
    once the loop lookup moved ahead of the suppression check.

    A context manager rather than a plain helper so the task is always released --
    a test that abandoned it pending would emit the very "Task was destroyed but it
    is pending!" noise this record exists to remove.
    """
    from kiro_crew import safety_override as so

    loop = asyncio.get_running_loop()
    gate = asyncio.Event()

    async def _blocked() -> None:
        await gate.wait()

    task = loop.create_task(_blocked())
    monkeypatch.setattr(so, "_yolo_policy_refresh", (loop, task))
    try:
        yield task
    finally:
        gate.set()
        await task


def _install_no_policy() -> None:
    """Install a context with NO governance ceiling: every mode permitted."""
    from kiro_crew.config.loader import KiroCrewConfig

    ctx_mod.set_context(
        dataclasses.replace(build_default_context(KiroCrewConfig.load()), governance=None)
    )


def _deny(*modes: str) -> None:
    """Install a boot-frozen ceiling denying ``modes``."""
    from kiro_crew.config.loader import KiroCrewConfig

    base = build_default_context(KiroCrewConfig.load())
    ceiling = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "approval_modes": {"mode": "deny", "deny": list(modes)},
        }
    )
    ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


def _make_app(state: DashboardState) -> web.Application:
    from kiro_crew.dashboard.chat import api_chat_mode, api_chat_slot_approve

    @web.middleware
    async def _test_auth(request: web.Request, handler):
        if "app" not in request:
            request["app"] = ""
        if "user" not in request:
            request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_test_auth])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    app.router.add_post("/api/chat/mode", api_chat_mode)
    return app


class TestChatModeEndpointRefusalIsAudited:
    @pytest.mark.asyncio
    async def test_mode_switch_refusal_is_sel_audited(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _deny("yolo")
        recorded: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: recorded.append(kw)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: fake_sel)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "yolo", "slot": "s1"})
            data = await resp.json()

        assert resp.status == 403
        assert data["code"] == "mode_disabled_by_policy"
        assert recorded[-1]["outcome"] == "approval_mode_denied_by_policy"


class TestTheEndpointNamesTheRealCause:
    """``mode_disabled_by_policy`` is reserved for a DEFINITE deny.

    ``approval_mode_permitted`` fails closed, so reading its boolean alone conflated
    two different answers: a governance READ FAILURE also came back False and the
    caller was told their organization's policy forbids the mode -- sent looking for
    a policy that may not exist, while the real fault went unnamed. The two Slack
    paths were given a three-way split in an earlier round; this endpoint was the
    twin that did not get it.
    """

    @pytest.mark.asyncio
    async def test_an_unreadable_policy_is_503_not_a_policy_403(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        # Current policy could not be read. The two stand-ins are the SAME
        # governance failure seen through the two readers, which is the whole
        # point: the verdict says ``unknown``, and the fail-closed boolean the
        # endpoint used to read on its own says False for exactly that reason --
        # so reading the boolean alone is indistinguishable from a real deny.
        #
        # Stubbed at the ENDPOINT's two readers rather than by forging the module
        # cache: the verdict is backed by an off-loop refresh, so a thread left
        # over from an earlier case in this file can restamp it mid-request. The
        # cache's own three states are pinned by ``TestAPolicyChangeIsNotBoundedByTheTTL``
        # and ``TestUNKNOWNNeverDestroysAScopedGrant``; what is under test HERE is
        # which answer the endpoint gives for each.
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.yolo_policy_verdict", lambda: "unknown"
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.approval_mode_permitted", lambda m: False
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "yolo", "slot": "s1"})
            data = await resp.json()

        assert resp.status == 503, "an unreadable policy is transient, not a refusal"
        assert data["code"] == "approval_mode_policy_unreadable", data
        assert data["code"] != "mode_disabled_by_policy", (
            "blaming the organization's policy for a policy nobody could read sends "
            "the operator down the wrong troubleshooting path"
        )

    @pytest.mark.asyncio
    async def test_a_definite_deny_is_still_the_audited_403(self, tmp_path, monkeypatch):
        """The refusal must still work, or the split has broken the control.

        Same stubbing rationale as the case above; the end-to-end refusal against a
        real boot-frozen ceiling is ``TestChatModeEndpointRefusalIsAudited``.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _deny("yolo")
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.yolo_policy_verdict", lambda: "denied"
        )
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "yolo", "slot": "s1"})
            data = await resp.json()

        assert resp.status == 403
        assert data["code"] == "mode_disabled_by_policy", data


class TestSafetyOverrideRefusalIsAudited:
    def test_denied_yolo_arming_is_audited_and_refused(self, monkeypatch):
        from kiro_crew import safety_override as so

        _deny("yolo")
        so.reset_singleton()
        recorded: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: recorded.append(kw)
        monkeypatch.setattr(so, "sel", lambda: fake_sel)

        result = so.safety_override().activate("dashboard")

        assert result.active is False
        assert recorded, "a refused arming must leave a security-event trace"
        assert recorded[-1]["outcome"] == "denied"
        assert "approval_modes" in recorded[-1]["resources"]

    def test_scoped_arming_is_refused_and_audited_too(self, monkeypatch):
        """A narrow scoped grant is still an auto-approve grant."""
        from kiro_crew import safety_override as so

        _deny("yolo")
        so.reset_singleton()
        recorded: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: recorded.append(kw)
        monkeypatch.setattr(so, "sel", lambda: fake_sel)

        result = so.safety_override().activate_scoped("session:abc", "dashboard")

        assert result.active is False
        assert recorded[-1]["outcome"] == "denied"
        assert "session:abc" in recorded[-1]["resources"]


class TestStatusSnapshotNeverResolvesOnTheEventLoop:
    """``status_snapshot`` runs on the event loop for every 5s WS frame.

    Resolving governance there walks the profiles dir, so the reader must be pure
    memory: a stale value schedules an off-loop refresh and returns the previous
    one rather than blocking the frame.

    The status field is derived from the SAME verdict the per-tool-call enforcement
    predicate reads. It used to have its own TTL cache in ``dashboard/state.py``,
    which had already drifted from this one: that copy was TTL-only while this one is
    generation-aware, so the picker could show YOLO selectable for up to a TTL after
    enforcement had stopped honouring it.
    """

    @pytest.mark.asyncio
    async def test_cached_reader_does_not_resolve_inline(self, monkeypatch):
        from kiro_crew import safety_override as so

        calls: list[str] = []

        def _tripwire(mode: str) -> bool:
            calls.append(mode)
            return True

        monkeypatch.setattr(so, "approval_mode_permitted", _tripwire)
        monkeypatch.setattr(so, "_yolo_policy_cache", (0.0, False, so._governance_generation()))

        # Stale cache + a refresh already in flight: the reader must return the
        # last good value WITHOUT resolving, which is what keeps the frame off
        # the filesystem.
        async with _refresh_in_flight(monkeypatch):
            assert so.cached_disabled_approval_modes() == ["yolo"]
            assert calls == []

    def test_blocking_resolver_updates_the_shared_cache(self, monkeypatch):
        from kiro_crew import safety_override as so

        monkeypatch.setattr(so, "approval_mode_permitted", lambda mode: mode != "yolo")

        assert so.resolve_disabled_approval_modes_blocking() == ["yolo"]
        # The event-loop reader now serves the resolved value from memory.
        assert so.cached_disabled_approval_modes() == ["yolo"]

    def test_a_resolve_error_keeps_the_last_good_value(self, monkeypatch):
        from kiro_crew import safety_override as so

        monkeypatch.setattr(
            so, "_yolo_policy_cache", (time.monotonic(), False, so._governance_generation())
        )

        def _boom(mode: str) -> bool:
            raise RuntimeError("governance unavailable")

        monkeypatch.setattr(so, "approval_mode_permitted", _boom)

        # NOT [] — reporting "nothing is denied" would unhide a locked mode in
        # the picker on a transient governance error.
        assert so.resolve_disabled_approval_modes_blocking() == ["yolo"]

    def test_the_status_field_cannot_contradict_enforcement(self, monkeypatch):
        """The anti-divergence property, stated directly.

        Two caches of one question is what allowed the picker to disagree with the
        gate. Whatever ``yolo_policy_permits`` says, the reported list must say.
        """
        from kiro_crew import safety_override as so

        _install_no_policy()
        assert so.yolo_policy_permits() is True
        assert so.cached_disabled_approval_modes() == []

        _deny("yolo")
        assert so.yolo_policy_permits() is False
        assert so.cached_disabled_approval_modes() == ["yolo"]

    @pytest.mark.asyncio
    async def test_status_snapshot_is_served_from_the_cache(self, tmp_path, monkeypatch):
        from kiro_crew import safety_override as so

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        # A denial cached under the CURRENT generation, with a refresh in flight so
        # the reader cannot go resolve. ``["trust", "yolo"]`` was the shape an earlier
        # revision asserted here; the backend can never emit it, because those modes
        # are non-deniable.
        monkeypatch.setattr(so, "_yolo_policy_cache", (0.0, False, so._governance_generation()))
        state = _make_state(tmp_path)

        async with _refresh_in_flight(monkeypatch):
            snap = state.status_snapshot()

        assert snap["disabled_approval_modes"] == ["yolo"]


class TestATightenedPolicyRevokesALiveGrant:
    """Gating only at ARMING left a live grant honoured until its own TTL.

    An admin who denies ``yolo`` mid-session then kept auto-approving every tool
    for up to 24h, so the control announced a state it was not enforcing.
    ``is_active()`` is the consult point every transport hands to ``TurnDriver``,
    which is why the check lives there rather than at each call site.
    """

    def test_a_live_grant_stops_being_honoured_when_policy_denies(self, monkeypatch):
        from kiro_crew import safety_override as so

        # Arm under a policy that permits.
        _install_no_policy()
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())
        assert so.safety_override().activate("dashboard").active is True
        assert so.safety_override().is_active() is True

        # The admin tightens the policy while the grant is live.
        _deny("yolo")
        so.reset_yolo_policy_cache()

        assert so.safety_override().is_active() is False

    def test_a_denied_grant_is_revoked_so_relaxing_does_not_resurrect_it(self, monkeypatch):
        """Denial REVOKES. Masking-without-revoking was a defect, not a feature.

        An earlier revision left the grant in place and only reported inactive, so
        relaxing the policy silently restored auto-approve. That is wrong because the
        same predicate answers "is there a grant to clear?" -- see the test below. A
        fresh arm after the policy relaxes is the honest outcome.
        """
        from kiro_crew import safety_override as so

        _install_no_policy()
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())
        so.safety_override().activate("dashboard")

        _deny("yolo")
        so.reset_yolo_policy_cache()
        assert so.safety_override().is_active() is False

        _install_no_policy()
        so.reset_yolo_policy_cache()
        assert (
            so.safety_override().is_active() is False
        ), "a policy-revoked grant must stay revoked once the policy relaxes"

    def test_an_explicit_revoke_during_a_denial_window_is_not_swallowed(self, monkeypatch):
        """The bug the revoke-vs-mask choice exists to prevent.

        Slack's off-path is `if is_yolo_mode(): disable_yolo()`. While a mask-only
        denial reported inactive, that branch cleared NOTHING, and a later policy
        relaxation resurrected the grant the operator had explicitly revoked.
        """
        from kiro_crew import safety_override as so

        _install_no_policy()
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())
        so.safety_override().activate("dashboard")

        _deny("yolo")
        so.reset_yolo_policy_cache()

        # Exactly what the Slack handler does: consult, then skip when inactive.
        if so.safety_override().is_active():
            so.safety_override().deactivate("slack")

        # Policy lifts. Nothing may come back.
        _install_no_policy()
        so.reset_yolo_policy_cache()
        assert so.safety_override().is_active() is False

    def test_a_declared_grant_is_revoked_too(self, monkeypatch):
        """A permanent grant has no deadline, so policy is its ONLY off-switch."""
        from kiro_crew import safety_override as so

        _install_no_policy()
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())
        so.safety_override().activate_declared()
        assert so.safety_override().is_active() is True

        _deny("yolo")
        so.reset_yolo_policy_cache()
        assert so.safety_override().is_active() is False


class TestTheHotPredicateNeverTouchesTheFilesystem:
    """``is_active()`` runs per TOOL CALL via ``auto_approve_session``.

    Resolving governance there would walk the profiles dir on every tool, so the
    reader must be pure memory with an off-loop refresh.
    """

    def test_the_cached_reader_does_not_resolve_inline(self, monkeypatch):
        from kiro_crew import safety_override as so

        calls: list[str] = []

        def _tripwire(mode: str) -> bool:
            calls.append(mode)
            return True

        monkeypatch.setattr(so, "approval_mode_permitted", _tripwire)
        # Stamped with the CURRENT generation: this case is about the TTL branch, and
        # a mismatched generation would resolve inline for a different reason.
        monkeypatch.setattr(
            so, "_yolo_policy_cache", (time.monotonic(), True, so._governance_generation())
        )

        assert so.yolo_policy_permits() is True
        assert calls == [], "a fresh cache must not trigger a governance read"

    @pytest.mark.asyncio
    async def test_a_stale_cache_with_a_refresh_in_flight_serves_the_last_value(self, monkeypatch):
        from kiro_crew import safety_override as so

        calls: list[str] = []
        monkeypatch.setattr(so, "approval_mode_permitted", lambda m: calls.append(m) or True)
        monkeypatch.setattr(so, "_yolo_policy_cache", (0.0, False, so._governance_generation()))

        async with _refresh_in_flight(monkeypatch):
            assert so.yolo_policy_permits() is False
            assert calls == []

    def test_a_sync_caller_resolves_inline_because_it_has_no_loop_to_protect(self, monkeypatch):
        """The no-running-loop branch, stated as a property rather than assumed.

        The suppression check now comes AFTER the loop lookup, and that order is the
        correct one: with no loop there is nothing to stall, and handing a CLI caller
        a cold cache's permissive default would be the worse outcome. An in-flight
        record belonging to some other loop must not talk a sync caller out of
        resolving.
        """
        from kiro_crew import safety_override as so

        calls: list[str] = []
        monkeypatch.setattr(so, "approval_mode_permitted", lambda m: calls.append(m) or False)
        monkeypatch.setattr(so, "_yolo_policy_cache", (0.0, True, so._governance_generation()))

        assert so.yolo_policy_permits() is False
        assert calls == ["yolo"], "a sync caller with a stale entry must resolve"


class TestAnInFlightRecordCannotWedgeTheRefresh:
    """The record is ``(loop, task)`` so that staleness is DECIDABLE.

    A bare boolean was set before the task existed and cleared in that task's
    ``finally``. A loop torn down while the refresh was pending therefore left it
    ``True`` with nothing alive to clear it, and every later call took the early
    return -- the verdict cache stopped refreshing for the life of the process. On a
    safety predicate that means a policy tightening silently stops landing.
    """

    @pytest.mark.asyncio
    async def test_a_record_naming_another_loop_does_not_suppress_a_refresh(self, monkeypatch):
        """The wedge, reproduced: a pending task whose loop is not this one.

        The loop is a sentinel rather than a second real event loop on purpose. What
        decides the branch is ``record[0] is running_loop``, and a sentinel states
        that mismatch exactly while leaving nothing to tear down -- a real second
        loop would have to be abandoned holding a pending task, which is the noise
        this record removes.
        """
        from kiro_crew import safety_override as so

        loop = asyncio.get_running_loop()
        gate = asyncio.Event()

        async def _blocked() -> None:
            await gate.wait()

        orphan = loop.create_task(_blocked())
        monkeypatch.setattr(so, "_yolo_policy_refresh", (object(), orphan))
        monkeypatch.setattr(so, "approval_mode_permitted", lambda m: False)
        assert not orphan.done(), "the wedging state is a PENDING task, not a finished one"

        try:
            so._schedule_yolo_policy_refresh()

            record = so._yolo_policy_refresh
            assert record is not None
            assert record[0] is loop, (
                "the scheduler must adopt the LIVE loop rather than defer to a record "
                "that names a loop which is no longer running"
            )
            assert record[1] is not orphan
            record[1].cancel()
        finally:
            gate.set()
            await orphan

    @pytest.mark.asyncio
    async def test_a_finished_task_on_this_loop_does_not_suppress_a_refresh(self, monkeypatch):
        """The other half of decidable: done means done, so a new one may start."""
        from kiro_crew import safety_override as so

        loop = asyncio.get_running_loop()

        async def _immediate() -> None:
            return None

        finished = loop.create_task(_immediate())
        await finished
        monkeypatch.setattr(so, "_yolo_policy_refresh", (loop, finished))
        monkeypatch.setattr(so, "approval_mode_permitted", lambda m: False)

        so._schedule_yolo_policy_refresh()

        record = so._yolo_policy_refresh
        assert record is not None and record[1] is not finished
        record[1].cancel()

    @pytest.mark.asyncio
    async def test_a_live_refresh_on_this_loop_IS_respected(self, monkeypatch):
        """The suppression still has to hold, or every call spawns another task."""
        from kiro_crew import safety_override as so

        monkeypatch.setattr(so, "approval_mode_permitted", lambda m: False)

        async with _refresh_in_flight(monkeypatch) as held:
            so._schedule_yolo_policy_refresh()

            assert so._yolo_policy_refresh is not None
            assert (
                so._yolo_policy_refresh[1] is held
            ), "a live refresh for this loop must not be duplicated"

    def test_the_reset_helper_clears_the_record(self):
        """A reset must not leave a record that suppresses the next refresh."""
        from kiro_crew import safety_override as so

        so.reset_yolo_policy_cache()
        assert so._yolo_policy_refresh is None

    def test_arming_reads_authoritatively_rather_than_from_the_cache(self, monkeypatch):
        """A deliberate, rare act must not be decided by a TTL-old value."""
        from kiro_crew import safety_override as so

        _deny("yolo")
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())
        # A stale-but-permissive cache must NOT let an arm through.
        monkeypatch.setattr(
            so, "_yolo_policy_cache", (time.monotonic(), True, so._governance_generation())
        )

        assert so.safety_override().activate("dashboard").active is False


class TestArmingIsOffloadedByItsAsyncCallers:
    """Arming does filesystem work, so no async caller may run it inline."""

    @pytest.mark.asyncio
    async def test_taskrunner_grant_run_trust_is_a_coroutine(self):
        import inspect

        from kiro_crew.taskrunner import TaskRunner

        assert inspect.iscoroutinefunction(TaskRunner._grant_run_trust), (
            "_grant_run_trust is reached from two async methods; a sync body puts "
            "the SEL write and the governance read on the event loop"
        )

    def test_every_grant_run_trust_call_site_is_awaited(self):
        """A coroutine left unawaited silently does nothing at all.

        That failure mode is invisible: the trust grant simply never happens, and
        the run proceeds with `auto_approve` set on the object but no authoritative
        scoped grant behind it.
        """
        import pathlib as _pl

        # Anchored to THIS FILE, not the process CWD. A relative path here reads
        # whatever happens to sit under the worker's working directory -- so the
        # case either fails for the wrong reason or, worse, silently inspects a
        # different file and passes without checking anything.
        repo_root = _pl.Path(__file__).resolve().parents[1]
        src = (repo_root / "src" / "kiro_crew" / "taskrunner.py").read_text(encoding="utf-8")
        bare = [
            line.strip()
            for line in src.splitlines()
            if "self._grant_run_trust(" in line and "await " not in line and "def " not in line
        ]
        assert not bare, f"unawaited _grant_run_trust call(s): {bare}"


class TestEveryGrantBranchHonoursAMidSessionDeny:
    """A mid-session deny must reach EVERY branch that honours a grant.

    Masking only the session-wide grant revoked one kind of auto-approve and left
    the other, and the scoped branch is the reachable one: ``task_executor``
    consults ``is_scope_active`` before every approval and slides the grant with
    ``renew_scoped``, so a taskrunner run armed while permitted kept auto-approving
    for up to 24h after the deny. The branch table this pins:

    | consult point        | grant kind           | revoked |
    |----------------------|----------------------|---------|
    | ``is_active``        | session-wide, TTL    | yes     |
    | ``is_active``        | declared, no TTL     | yes     |
    | ``is_scope_active``  | scoped, per run      | yes     |
    | ``renew_scoped``     | scoped, expiry slide | yes     |
    """

    def _armed_scope(self, monkeypatch):
        from kiro_crew import safety_override as so

        _install_no_policy()
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())
        assert so.safety_override().activate_scoped("run:abc", "dashboard").active is True
        assert so.safety_override().is_scope_active("run:abc") is True
        return so

    def test_a_live_scoped_grant_stops_being_honoured(self, monkeypatch):
        so = self._armed_scope(monkeypatch)

        _deny("yolo")
        so.reset_yolo_policy_cache()

        assert so.safety_override().is_scope_active("run:abc") is False

    def test_a_denied_scoped_grant_is_not_slid_forward(self, monkeypatch):
        """Renewal is what keeps an active run's grant alive to the ceiling."""
        so = self._armed_scope(monkeypatch)

        _deny("yolo")
        so.reset_yolo_policy_cache()

        result = so.safety_override().renew_scoped("run:abc", "dashboard")
        assert result.renewed is False

    def test_a_denied_scoped_grant_is_revoked_not_merely_masked(self, monkeypatch):
        """Same correction as the session-wide grant: denial tears it down."""
        so = self._armed_scope(monkeypatch)

        _deny("yolo")
        so.reset_yolo_policy_cache()
        assert so.safety_override().is_scope_active("run:abc") is False

        _install_no_policy()
        so.reset_yolo_policy_cache()
        assert (
            so.safety_override().is_scope_active("run:abc") is False
        ), "a policy-revoked scoped grant must stay revoked once the policy relaxes"


class TestAPolicyRevocationRunsTheSameTeardownAsATTLLapse:
    """Clearing ``_active`` is not the whole revocation.

    A dashboard grant also writes ``approval_policy="auto"`` onto the slots, and a
    spawned subagent reads THAT rather than this flag -- so dropping only the flag
    left spawn admission and every child tool auto-approved against a policy that
    denies it. The TTL-lapse path already fixes this by firing ``on_expired``, whose
    handler resets those policies and clears the shared trust mapping; a policy
    revocation owes the same cleanup, so it fires the same callback rather than
    growing a second, divergent teardown.
    """

    def test_a_policy_revocation_fires_the_expiry_callback(self, monkeypatch):
        from kiro_crew import safety_override as so

        _install_no_policy()
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())
        override = so.safety_override()
        override.activate("dashboard")

        seen: list[str] = []
        override.on_expired = seen.append

        _deny("yolo")
        so.reset_yolo_policy_cache()
        assert override.is_active() is False

        assert seen == ["policy"], (
            "a policy revocation must run the same teardown as a TTL lapse -- "
            "otherwise the slots keep approval_policy='auto' and subagents stay "
            "auto-approved against a policy that denies yolo"
        )

    def test_the_callback_fires_exactly_once_across_repeated_consults(self, monkeypatch):
        """``is_active`` runs per TOOL CALL.

        The handler broadcasts and rewrites slot policies, so re-firing it on every
        call would be its own defect -- which is why the teardown is gated on there
        actually being a grant to tear down.
        """
        from kiro_crew import safety_override as so

        _install_no_policy()
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())
        override = so.safety_override()
        override.activate("dashboard")

        seen: list[str] = []
        override.on_expired = seen.append

        _deny("yolo")
        so.reset_yolo_policy_cache()
        for _ in range(5):
            assert override.is_active() is False

        assert seen == ["policy"], f"expected exactly one teardown, got {seen}"

    def test_no_grant_means_no_callback(self, monkeypatch):
        """A denying policy with nothing armed must not synthesise an expiry.

        Every consult on an unarmed override takes this path, so an ungated fire
        would broadcast a revocation for a grant that never existed.
        """
        from kiro_crew import safety_override as so

        _deny("yolo")
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())
        override = so.safety_override()

        seen: list[str] = []
        override.on_expired = seen.append

        assert override.is_active() is False
        assert seen == [], "nothing was armed, so there is nothing to tear down"

    def test_a_raising_callback_does_not_break_the_refusal(self, monkeypatch):
        """The verdict is the safety-relevant half; the cleanup is best-effort."""
        from kiro_crew import safety_override as so

        _install_no_policy()
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())
        override = so.safety_override()
        override.activate("dashboard")

        def _boom(_reason: str) -> None:
            raise RuntimeError("handler exploded")

        override.on_expired = _boom

        _deny("yolo")
        so.reset_yolo_policy_cache()
        assert override.is_active() is False


class TestAPolicyChangeIsNotBoundedByTheTTL:
    """The TTL bounds staleness WITHIN one ceiling, not across a change.

    A verdict primed while ``yolo`` was permitted kept auto-approving for the rest
    of the TTL after a tightening -- the window the cache exists to make cheap was
    also a window in which the control was not enforced. The entry now carries the
    governance generation it was resolved under, so a newly installed ceiling makes
    the reader resolve immediately instead of serving the primed permit.
    """

    def test_a_permit_primed_under_the_previous_ceiling_is_not_served(self, monkeypatch):
        from kiro_crew import safety_override as so

        _install_no_policy()
        assert so.yolo_policy_permits() is True

        # Installing a ceiling bumps the generation, which is what invalidates the
        # entry. No cache reset here on purpose: that is the whole point.
        _deny("yolo")
        assert so.yolo_policy_permits() is False, (
            "a fresh-by-TTL permit resolved under the PREVIOUS ceiling must not be "
            "served after a tightening"
        )

    @pytest.mark.asyncio
    async def test_a_generation_mismatch_reads_UNKNOWN_and_never_resolves_on_the_loop(
        self, monkeypatch
    ):
        """Two properties at once, and an earlier revision had to break one to hold
        the other.

        Resolving inline held "never serve a stale permit" by walking the profiles
        dir on the event loop, in a predicate that runs per TOOL CALL. Serving the
        last-known verdict held "never block the loop" by handing back exactly the
        stale permit. UNKNOWN is what lets both stand: it is decided from memory, and
        it is not a permit.
        """
        from kiro_crew import safety_override as so

        calls: list[str] = []
        monkeypatch.setattr(so, "approval_mode_permitted", lambda m: calls.append(m) or False)
        # Fresh by the clock, but stamped with a generation nothing will ever equal.
        monkeypatch.setattr(so, "_yolo_policy_cache", (time.monotonic(), True, -999))

        assert so.yolo_policy_verdict() == so._YOLO_UNKNOWN
        assert so.yolo_policy_permits() is False, "an undated verdict is not a permit"
        assert calls == [], "a mismatched generation must NOT resolve on the loop"

        # The entry is NOT restamped: only a successful resolve may claim the current
        # generation, or an unresolvable policy would masquerade as a current one.
        assert so._yolo_policy_cache[2] == -999

    @pytest.mark.asyncio
    async def test_UNKNOWN_stops_auto_approval_without_revoking_the_grant(self, monkeypatch):
        """The reason UNKNOWN is a state rather than a boolean.

        Folding it onto DENIED would revoke -- permanently, by design -- and the
        window opens on EVERY ceiling install, including ones that still permit YOLO.
        So an unknown verdict must refuse to auto-approve and leave the grant intact
        for the refresh to settle.
        """
        from kiro_crew import safety_override as so

        _install_no_policy()
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())
        override = so.safety_override()
        override.activate("dashboard")

        seen: list[str] = []
        override.on_expired = seen.append

        # Undated verdict, on a running loop so the refresh cannot settle inline.
        monkeypatch.setattr(so, "approval_mode_permitted", lambda m: True)
        monkeypatch.setattr(so, "_yolo_policy_cache", (time.monotonic(), True, -999))

        assert override.is_active() is False, "no auto-approve on an undated verdict"
        assert seen == [], "and NO revocation -- the grant must survive to be settled"
        with override._lock:
            assert override._active is True, "the grant itself was not torn down"

    def test_a_resolve_that_keeps_failing_never_serves_the_stale_permit(self, monkeypatch):
        """The finding this invariant exists to close, in its plainest form.

        ``_resolve_yolo_policy_blocking`` returns without restamping when the resolve
        raises. While a stale entry could still read as a permit, a governance read
        that kept failing served the old ``True`` indefinitely -- auto-approving every
        tool against a ceiling nobody could read.
        """
        from kiro_crew import safety_override as so

        # A permit resolved under the ceiling installed now...
        _install_no_policy()
        assert so.yolo_policy_permits() is True

        # ...then the ceiling moves AND governance becomes unreadable.
        def _boom(mode: str) -> bool:
            raise RuntimeError("profiles dir unreadable")

        monkeypatch.setattr(so, "approval_mode_permitted", _boom)
        _deny("yolo")

        for _ in range(4):
            assert so.yolo_policy_verdict() == so._YOLO_UNKNOWN
            assert so.yolo_policy_permits() is False, (
                "a permit that no current resolve backs must never be served, however "
                "many times it is asked for"
            )

    def test_arming_refuses_when_policy_cannot_be_read(self, monkeypatch):
        """The authoritative path owed the same fail-closed reading.

        Arming consults ``_resolve_yolo_policy_blocking`` directly. While that
        returned the previous verdict on an exception, a grant could be armed against
        a policy the host had just failed to read.
        """
        from kiro_crew import safety_override as so

        _install_no_policy()
        assert so.yolo_policy_permits() is True  # prime a permitting entry
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())

        def _boom(mode: str) -> bool:
            raise RuntimeError("profiles dir unreadable")

        monkeypatch.setattr(so, "approval_mode_permitted", _boom)

        assert so.safety_override().activate("dashboard").active is False


class TestUNKNOWNNeverDestroysAScopedGrant:
    """The scoped guards owed the same three-state reading as ``is_active``.

    UNKNOWN was introduced with ``is_active`` taught to discriminate on it, and the
    two SCOPED consult points were left on the two-way reading -- so they collapsed
    it onto permanent revocation. That is the sharper end of the same defect:
    ``is_scope_active`` runs before EVERY approval in an unattended run,
    ``deactivate_scope`` pops the entry for good, and ``task_executor`` then clears
    ``run.auto_approve``. Nothing re-arms. A mid-session ceiling install that STILL
    PERMITS yolo would therefore stall a legitimately granted run for its whole
    remainder, on nothing but the off-loop refresh window.
    """

    def _armed(self, monkeypatch):
        from kiro_crew import safety_override as so

        _install_no_policy()
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())
        override = so.safety_override()
        assert override.activate_scoped("run:abc", "dashboard").active is True
        return so, override

    @pytest.mark.asyncio
    async def test_UNKNOWN_withholds_approval_without_popping_the_scope(self, monkeypatch):
        so, override = self._armed(monkeypatch)

        # Undated verdict, on a running loop so the refresh cannot settle inline.
        monkeypatch.setattr(so, "approval_mode_permitted", lambda m: True)
        monkeypatch.setattr(so, "_yolo_policy_cache", (time.monotonic(), True, -999))

        assert override.is_scope_active("run:abc") is False, "no approval on unknown"
        with override._lock:
            assert "run:abc" in override._scoped, (
                "the grant must SURVIVE an unknown verdict -- popping it is permanent "
                "and nothing re-arms it"
            )

    @pytest.mark.asyncio
    async def test_UNKNOWN_refuses_a_renewal_without_popping_the_scope(self, monkeypatch):
        so, override = self._armed(monkeypatch)
        monkeypatch.setattr(so, "approval_mode_permitted", lambda m: True)
        monkeypatch.setattr(so, "_yolo_policy_cache", (time.monotonic(), True, -999))

        # Refusing the slide is the safe direction -- a renewal extends authority.
        assert override.renew_scoped("run:abc", "dashboard").renewed is False
        with override._lock:
            assert "run:abc" in override._scoped

    def test_a_definite_deny_still_revokes_the_scope(self, monkeypatch):
        """The revocation must still work, or the deny is cosmetic."""
        so, override = self._armed(monkeypatch)

        _deny("yolo")
        assert override.is_scope_active("run:abc") is False
        with override._lock:
            assert "run:abc" not in override._scoped, "a definite deny tears it down"


class TestAnExplicitOffIsNotPolicyFiltered:
    """``is_active`` answers "may a tool be auto-approved", which policy can veto.

    An explicit off asks a DIFFERENT question -- "is there something to tear down" --
    and reading the policy-filtered answer for it inverted the control: during the
    UNKNOWN window ``is_active`` reports False, so Slack's
    ``if is_yolo_mode(): disable_yolo()`` skipped the teardown, reported "already
    off", and left the grant standing to RESUME once the refresh settled. The
    operator revoked auto-approve and it came back.
    """

    def test_has_grant_ignores_policy_entirely(self, monkeypatch):
        from kiro_crew import safety_override as so

        _install_no_policy()
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())
        override = so.safety_override()
        override.activate("dashboard")

        # A denying ceiling makes is_active False; the grant still EXISTS.
        _deny("yolo")
        assert override.is_active() is False
        assert (
            override.has_grant() is False
        ), "a policy deny REVOKES, so after it there is genuinely nothing to clear"

    @pytest.mark.asyncio
    async def test_an_off_during_the_unknown_window_actually_revokes(self, monkeypatch):
        from kiro_crew import safety_override as so
        from kiro_crew.slack import handler as h

        _install_no_policy()
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())
        monkeypatch.setattr(h, "clear_trusted_sessions", lambda: None)
        override = so.safety_override()
        override.activate("dashboard")

        # Undated verdict on a running loop: is_active reports False while the grant
        # is still standing. This is the state the off path used to skip on.
        monkeypatch.setattr(so, "approval_mode_permitted", lambda m: True)
        monkeypatch.setattr(so, "_yolo_policy_cache", (time.monotonic(), True, -999))
        assert override.is_active() is False
        assert override.has_grant() is True, "the grant is there to be cleared"

        h.disable_yolo()

        with override._lock:
            assert override._active is False, (
                "an explicit off must tear the grant down even while the verdict is "
                "unknown -- otherwise it resumes when the refresh settles"
            )


class TestARefusedScopedArmDoesNotPersistAutoApprove:
    """``_grant_run_trust`` exists so the flag and the grant cannot diverge.

    It set ``run.auto_approve`` BEFORE arming, so a policy-refused arm still
    persisted and reported ``auto_approve: True`` with no authoritative grant behind
    it -- the very divergence the function's own docstring promises to prevent.
    """

    @pytest.mark.asyncio
    async def test_a_denied_policy_leaves_auto_approve_false(self, monkeypatch):
        from kiro_crew import safety_override as so
        from kiro_crew.taskrunner import TaskRunner

        _deny("yolo")
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())

        run = MagicMock()
        run.task_id = "t-1"
        run.auto_approve = False

        await TaskRunner._grant_run_trust(MagicMock(), run, True)

        assert (
            run.auto_approve is False
        ), "a refused arm must not persist or report auto-approve it does not have"

    @pytest.mark.asyncio
    async def test_a_permitting_policy_still_enables_it(self, monkeypatch):
        """The fix must not make the flag unreachable."""
        from kiro_crew import safety_override as so
        from kiro_crew.taskrunner import TaskRunner

        _install_no_policy()
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())

        run = MagicMock()
        run.task_id = "t-2"
        run.auto_approve = False

        await TaskRunner._grant_run_trust(MagicMock(), run, True)

        assert run.auto_approve is True


class TestALapsedGrantIsNotTornDownTwice:
    """``_active`` alone is the grant test, and the ``or`` was a double-fire.

    The natural-expiry branch clears ``_active`` but deliberately LEAVES
    ``_expires_at`` set -- ``deactivate`` reads that nonzero deadline to tell
    "lapsed" from "never armed", so it can still SEL-record an explicit off after a
    lapse. A policy deny that also accepted ``_expires_at > 0`` therefore called
    ``deactivate`` a second time and fired a second expiry teardown for a grant that
    had already expired: a duplicate owner DM and a redundant broadcast, in the same
    block whose comment promises it fires EXACTLY ONCE.
    """

    def test_a_deny_after_a_natural_lapse_fires_no_second_teardown(self, monkeypatch):
        import time as _time

        from kiro_crew import safety_override as so

        _install_no_policy()
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())
        override = so.safety_override()
        override.activate("dashboard")

        seen: list[str] = []
        override.on_expired = seen.append

        # Force the deadline into the past, then let is_active reconcile it. That is
        # the natural-expiry path: it clears _active and leaves _expires_at set.
        with override._lock:
            override._expires_at = _time.monotonic() - 1.0

        assert override.is_active() is False
        assert seen == ["dashboard"], "the natural lapse fires its own teardown, once"
        with override._lock:
            assert override._expires_at > 0.0, (
                "the lapsed deadline is deliberately retained -- that is what used to "
                "make the policy branch double-fire"
            )

        # Policy now denies. There is no grant left to revoke.
        _deny("yolo")
        assert override.is_active() is False
        assert seen == [
            "dashboard"
        ], "a grant that already expired must not be torn down a second time"

    def test_a_mismatch_serves_the_last_known_verdict_not_a_fail_closed_deny(self, monkeypatch):
        """Why the invalidation does not deny while the refresh is pending.

        A deny from this predicate is not a cautious read, it is a REVOCATION --
        ``is_active`` tears the grant down and that teardown is permanent. The
        generation bumps on EVERY ceiling install, including one that still permits
        YOLO, so failing closed here would destroy live grants on unrelated policy
        refreshes.
        """
        from kiro_crew import safety_override as so

        _install_no_policy()
        so.reset_singleton()
        monkeypatch.setattr(so, "sel", lambda: MagicMock())
        override = so.safety_override()
        override.activate("dashboard")
        assert override.is_active() is True

        seen: list[str] = []
        override.on_expired = seen.append

        # A ceiling is REINSTALLED that still permits yolo: generation moves, verdict
        # does not. The grant must survive.
        _install_no_policy()
        assert override.is_active() is True
        assert seen == [], "an unrelated ceiling install must not revoke a live grant"

    def test_an_unreadable_generation_invalidates_rather_than_trusting_the_entry(self, monkeypatch):
        """``-1`` never equals a STORED generation, so it can only invalidate.

        Answering ``-1`` on an unreadable counter is what keeps the failure mode
        "refresh more often" rather than "serve a value you cannot date".
        """
        from kiro_crew import safety_override as so

        _install_no_policy()
        assert so.yolo_policy_permits() is True

        monkeypatch.setattr(so, "_governance_generation", lambda: -1)
        calls: list[str] = []
        monkeypatch.setattr(so, "approval_mode_permitted", lambda m: calls.append(m) or False)

        assert so.yolo_policy_permits() is False
        assert calls == ["yolo"]

    def test_a_backwards_clock_step_cannot_freeze_the_entry_as_fresh(self):
        """Why the stamp is monotonic rather than wall clock.

        ``time.time()`` can move backwards -- an NTP step, a VM restore, an operator
        correcting the clock. A backwards jump makes ``now - resolved_at`` negative,
        so the TTL never elapses again and the entry reads as fresh forever: a permit
        with no expiry, for a reason that has nothing to do with policy. A monotonic
        stamp cannot go backwards, so a wall-clock step is simply not visible here.
        """
        from kiro_crew import safety_override as so

        _install_no_policy()
        assert so.yolo_policy_permits() is True
        stamped = so._yolo_policy_cache[0]

        # The entry is dated on the monotonic clock, which is what a wall-clock step
        # cannot reach. Asserting the SOURCE rather than simulating a jump: patching
        # the global clock would prove nothing about which clock was read.
        assert stamped <= time.monotonic(), "the stamp must come from the monotonic clock"
        assert stamped > 0.0, "a real reading, not the never-resolved sentinel"

    def test_the_reset_helper_stamps_a_generation_nothing_matches(self):
        """A reset must force a resolve, not leave a permit that looks current."""
        from kiro_crew import safety_override as so

        so.reset_yolo_policy_cache()
        assert so._yolo_policy_cache == (0.0, True, -1)
