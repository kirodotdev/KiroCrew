"""A listener sidecar must not outlive the listener it names.

The invariant: a ``run/gateway-<port>-<address>.secret`` file asserts that THIS
generation holds THAT address *now*. Clients read the set of them to answer "does
this gateway hold every family the name I am dialling resolves to?", and send the
credential only when the answer is yes -- so a sidecar that survives its listener
converts a refusal into a disclosure. A co-resident process binds the address the
dead listener freed, the client still reads coverage, and the credential goes to
the party that took the socket.

Two paths could leave that claim standing, and both are covered here:

* The second loopback family's listener was started and its handle DISCARDED, so
  nothing could observe its death or withdraw its sidecar. On Windows one failed
  ``accept()`` closes a LISTEN socket for good while the process lives on (see
  ``listener_guard``), which is exactly that death.
* Even a guarded listener has a REBIND WINDOW: from the moment it is confirmed
  dead until a rebind lands, nobody holds the address. A withdrawal that happens
  only after the rebind ladder gives up leaves the claim standing for the whole
  window, which is why the ordering here is pinned rather than the mere fact of a
  withdrawal.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from kiro_crew import _LazyShutdownEvent
from kiro_crew.dashboard import server as dashboard_server
from kiro_crew.dashboard.listener_guard import LISTENER_LOST_EXIT_CODE, ListenerGuard
from kiro_crew.instances import run_marker


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "_config_dir_memo", None, raising=False)
    monkeypatch.setattr(run_marker, "_PUBLISHED_LISTENERS", {}, raising=True)
    return tmp_path


# ---------------------------------------------------------------------------
# run_marker: the single-address withdrawal
# ---------------------------------------------------------------------------


class TestWithdrawPublishedListener:
    def test_removes_the_file_and_the_in_memory_claim(self, home: Path) -> None:
        """Both halves. The in-memory set is what a later clear_marker deletes from."""
        path = run_marker.listener_secret_path(5476, "::1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("s3cret", encoding="utf-8")
        run_marker.note_published_listener(5476, "::1")
        assert run_marker.published_listeners(5476) == frozenset({"::1"})

        assert run_marker.withdraw_published_listener(5476, "::1") is True

        assert not path.exists()
        assert run_marker.published_listeners(5476) == frozenset()

    def test_leaves_the_other_addresses_of_the_same_port_alone(self, home: Path) -> None:
        """A port names a SET of listeners; withdrawing one must not touch its siblings."""
        for address in ("127.0.0.1", "::1"):
            p = run_marker.listener_secret_path(5476, address)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("s3cret", encoding="utf-8")
            run_marker.note_published_listener(5476, address)

        run_marker.withdraw_published_listener(5476, "::1")

        assert run_marker.listener_secret_path(5476, "127.0.0.1").exists()
        assert run_marker.published_listeners(5476) == frozenset({"127.0.0.1"})

    def test_refuses_an_address_this_process_never_published(self, home: Path) -> None:
        """Ownership, same rule as clear_marker: what cannot be proven is not deleted.

        Two gateways in one data home can hold the same port on different
        addresses. Unlinking an entry this process did not write would cost the
        live sibling every client that had already read it.
        """
        foreign = run_marker.listener_secret_path(5476, "::1")
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text("someone-elses", encoding="utf-8")

        assert run_marker.withdraw_published_listener(5476, "::1") is False
        assert foreign.exists()
        assert foreign.read_text(encoding="utf-8") == "someone-elses"

    def test_a_missing_file_still_drops_the_claim(self, home: Path) -> None:
        """A sidecar this process cannot vouch for stops being advertised."""
        run_marker.note_published_listener(5476, "::1")
        assert run_marker.withdraw_published_listener(5476, "::1") is True
        assert run_marker.published_listeners(5476) == frozenset()

    def test_an_empty_address_is_a_no_op(self, home: Path) -> None:
        assert run_marker.withdraw_published_listener(5476, "") is False

    def test_an_unlinkable_credential_is_emptied_instead(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sharing violation must not leave a readable credential behind.

        A client's coverage test is an intersection over the secret VALUE, so a
        surviving file re-admits the live family's own secret and the mint sends
        it to whoever took the address. An empty sidecar carries no secret, so it
        covers no family and the mint refuses instead.
        """
        run_marker.note_published_listener(5476, "::1")
        path = run_marker.listener_secret_path(5476, "::1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("live-generation-secret", encoding="utf-8")

        def _refuse(self: Path, missing_ok: bool = False) -> None:
            raise OSError(32, "sharing violation")

        monkeypatch.setattr(Path, "unlink", _refuse, raising=True)

        assert run_marker.withdraw_published_listener(5476, "::1") is True
        assert path.exists(), "the unlink was refused, so the file is still there"
        assert path.read_text(encoding="utf-8") == ""
        assert run_marker.published_listeners(5476) == frozenset()

    def test_a_credential_that_cannot_be_emptied_keeps_its_claim(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """clear_marker deletes only what published_listeners still names.

        Dropping the claim here would make the surviving credential permanently
        unreachable by this process. Keeping it means shutdown finishes the
        retraction this call could not, and the False answer says so.
        """
        run_marker.note_published_listener(5476, "::1")
        path = run_marker.listener_secret_path(5476, "::1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("live-generation-secret", encoding="utf-8")

        def _refuse(self: Path, missing_ok: bool = False) -> None:
            raise OSError(32, "sharing violation")

        monkeypatch.setattr(Path, "unlink", _refuse, raising=True)
        monkeypatch.setattr(run_marker, "_blank_listener_secret", lambda _path: False, raising=True)

        assert run_marker.withdraw_published_listener(5476, "::1") is False
        assert run_marker.published_listeners(5476) == frozenset({"::1"})
        assert path.read_text(encoding="utf-8") == "live-generation-secret"


# ---------------------------------------------------------------------------
# ListenerGuard: the lifecycle hooks and the replaceable terminal action
# ---------------------------------------------------------------------------


async def _live(_request: web.Request) -> web.Response:
    return web.json_response({"alive": True})


class _Guarded:
    """A real aiohttp server on an ephemeral loopback port plus its guard."""

    def __init__(self, **guard_kwargs: Any) -> None:
        self.app = web.Application()
        self.app.router.add_get("/api/live", _live)
        self.runner = web.AppRunner(self.app)
        self.shutdown = asyncio.Event()
        self._guard_kwargs = guard_kwargs
        self.guard: ListenerGuard | None = None

    async def __aenter__(self) -> "_Guarded":
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.guard = ListenerGuard(self.runner, site, self.shutdown, **self._guard_kwargs)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self.guard is not None:
            self.guard.stop()
        await self.runner.cleanup()


class TestRecoveryWindowOrdering:
    @pytest.mark.asyncio
    async def test_withdrawal_precedes_the_first_rebind_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordering IS the fix, so the ordering is what is pinned.

        A guard that withdraws only after the rebind ladder finishes satisfies
        "a withdrawal happens" while leaving the false claim standing for the
        entire window in which the address is free. The event sequence has to be
        lost -> bind -> restored, with the withdrawal strictly BEFORE the first
        bind.
        """
        events: list[str] = []
        async with _Guarded(
            interval=3600,
            on_listener_lost=lambda: events.append("lost"),
            on_listener_restored=lambda: events.append("restored"),
        ) as served:
            guard = served.guard
            assert guard is not None
            old_site = guard.site
            monkeypatch.setattr(guard, "listener_open", lambda: guard.site is not old_site)

            original_new_site = guard._new_site

            async def _recording_new_site() -> Any:
                events.append("bind")
                return await original_new_site()

            monkeypatch.setattr(guard, "_new_site", _recording_new_site)

            assert await guard.check_now("test") is True

        assert events == ["lost", "bind", "restored"]
        assert events.index("lost") < events.index("bind")

    @pytest.mark.asyncio
    async def test_no_restore_when_every_rebind_attempt_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The address is re-advertised only once a bind actually lands."""
        events: list[str] = []
        async with _Guarded(
            interval=3600,
            max_attempts=2,
            backoff_base=0.0,
            max_backoff=0.0,
            on_listener_lost=lambda: events.append("lost"),
            on_listener_restored=lambda: events.append("restored"),
            on_give_up=lambda reason: events.append("gave-up"),
        ) as served:
            guard = served.guard
            assert guard is not None
            monkeypatch.setattr(guard, "listener_open", lambda: False)

            async def _always_fails() -> Any:
                raise OSError("bind refused")

            monkeypatch.setattr(guard, "_new_site", _always_fails)

            assert await guard.check_now("test") is False

        assert events == ["lost", "gave-up"]
        assert "restored" not in events


class TestHooksRunOffTheLoop:
    """Every lifecycle hook is filesystem work, so none of it runs on the loop.

    The shipped hooks unlink, ``mkdir`` and ``os.open`` under ``run/``, and each
    of those applies an owner-only DACL on Windows -- the only platform these
    guards arm on -- which is an unbounded SMB round trip for a UNC data home.
    Inline, that freezes every request on the listener that is still alive at the
    moment the gateway is already degraded, and a long enough freeze is what the
    shell's liveness probe force-kills the gateway for.
    """

    @pytest.mark.asyncio
    async def test_lost_and_restored_hooks_run_on_a_worker_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop_thread = threading.get_ident()
        threads: dict[str, int] = {}
        async with _Guarded(
            interval=3600,
            on_listener_lost=lambda: threads.setdefault("lost", threading.get_ident()),
            on_listener_restored=lambda: threads.setdefault("restored", threading.get_ident()),
        ) as served:
            guard = served.guard
            assert guard is not None
            old_site = guard.site
            monkeypatch.setattr(guard, "listener_open", lambda: guard.site is not old_site)

            assert await guard.check_now("test") is True

        assert set(threads) == {"lost", "restored"}
        assert loop_thread not in threads.values()

    @pytest.mark.asyncio
    async def test_the_give_up_hook_runs_on_a_worker_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop_thread = threading.get_ident()
        threads: list[int] = []
        async with _Guarded(
            interval=3600,
            max_attempts=1,
            backoff_base=0.0,
            on_give_up=lambda _reason: threads.append(threading.get_ident()),
        ) as served:
            guard = served.guard
            assert guard is not None
            monkeypatch.setattr(guard, "listener_open", lambda: False)

            async def _always_fails() -> Any:
                raise OSError("bind refused")

            monkeypatch.setattr(guard, "_new_site", _always_fails)

            assert await guard.check_now("test") is False

        assert len(threads) == 1
        assert threads[0] != loop_thread


class TestReplaceableTerminalAction:
    @pytest.mark.asyncio
    async def test_injected_action_neither_exits_nor_signals_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A best-effort listener degrades; it must not kill a serving gateway."""
        reasons: list[str] = []
        async with _Guarded(
            interval=3600,
            max_attempts=1,
            backoff_base=0.0,
            on_give_up=reasons.append,
        ) as served:
            guard = served.guard
            assert guard is not None
            monkeypatch.setattr(guard, "listener_open", lambda: False)

            async def _always_fails() -> Any:
                raise OSError("bind refused")

            monkeypatch.setattr(guard, "_new_site", _always_fails)

            assert await guard.check_now("test") is False

            assert len(reasons) == 1
            assert guard.exit_code == 0
            assert served.shutdown.is_set() is False

    @pytest.mark.asyncio
    async def test_default_action_still_exits_non_zero_and_signals_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control: the primary listener's behaviour is unchanged.

        Without this the test above passes for a guard that never escalates at
        all, which would be an outage dressed up as a degradation.
        """
        async with _Guarded(interval=3600, max_attempts=1, backoff_base=0.0) as served:
            guard = served.guard
            assert guard is not None
            monkeypatch.setattr(guard, "listener_open", lambda: False)

            async def _always_fails() -> Any:
                raise OSError("bind refused")

            monkeypatch.setattr(guard, "_new_site", _always_fails)

            assert await guard.check_now("test") is False

            assert guard.exit_code == LISTENER_LOST_EXIT_CODE
            assert served.shutdown.is_set() is True

    @pytest.mark.asyncio
    async def test_injected_action_stops_the_guard_so_it_cannot_rebind_forever(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default path halted the loop via the shutdown event; this one cannot.

        Without stopping here the probe loop would rediscover the same dead
        listener every interval and rebind it for the life of the process.

        Asserting only that a later ``check_now`` reports "serving" would NOT pin
        this: a second, broader rule -- the ``_shutdown_event.is_set()`` check at
        the top of ``check_now`` and ``_recover`` -- returns exactly the same
        answer. So the assertions below are the pair that rule cannot satisfy: the
        shutdown event must still be CLEAR, and no further bind may be attempted
        anyway. A guard that escalated through the default path sets that event
        and fails the first half.
        """
        binds = 0
        async with _Guarded(
            interval=3600, max_attempts=1, backoff_base=0.0, on_give_up=lambda _r: None
        ) as served:
            guard = served.guard
            assert guard is not None
            monkeypatch.setattr(guard, "listener_open", lambda: False)

            async def _always_fails() -> Any:
                nonlocal binds
                binds += 1
                raise OSError("bind refused")

            monkeypatch.setattr(guard, "_new_site", _always_fails)

            await guard.check_now("test")
            assert binds == 1

            # Quiescent for the right reason: stopped, not shutting down.
            assert served.shutdown.is_set() is False
            assert await guard.check_now("again") is True
            assert binds == 1, "a stopped guard must not attempt another rebind"


# ---------------------------------------------------------------------------
# Two guards on one loop: chaining and the reverse-order detach
# ---------------------------------------------------------------------------


class TestTwoGuardsChain:
    @pytest.mark.asyncio
    async def test_reverse_order_detach_restores_the_original_handler(self) -> None:
        """Guards chain, so they must be detached in the reverse of the arming order.

        ``arm()`` captures whatever handler is installed and delegates to it, and
        each guard restores its neighbour only while it is still the installed
        handler. Detaching the outer one first therefore restores nothing and
        leaves the inner guard's handler on the loop for good.
        """
        loop = asyncio.get_running_loop()

        def _original(_loop: Any, _context: Any) -> None:
            return None

        loop.set_exception_handler(_original)
        try:
            async with _Guarded(interval=3600) as primary, _Guarded(interval=3600) as secondary:
                first, second = primary.guard, secondary.guard
                assert first is not None and second is not None
                first.arm()
                second.arm()
                assert loop.get_exception_handler() is not _original

                second.stop()
                first.stop()

                assert loop.get_exception_handler() is _original
        finally:
            loop.set_exception_handler(None)

    @pytest.mark.asyncio
    async def test_shipped_shutdown_hook_stops_the_secondary_first(self) -> None:
        """Pin the ORDER the shipped hook uses, not just that it stops both."""
        stopped: list[str] = []

        class _FakeGuard:
            def __init__(self, name: str) -> None:
                self._name = name

            def stop(self) -> None:
                stopped.append(self._name)

        class _FakeState:
            _listener_guard = _FakeGuard("primary")
            _secondary_listener_guard = _FakeGuard("secondary")

        app = web.Application()
        dashboard_server._register_listener_guard_shutdown(app, _FakeState())  # type: ignore[arg-type]
        for hook in app.on_cleanup:
            await hook(app)

        assert stopped == ["secondary", "primary"]


# ---------------------------------------------------------------------------
# The secondary helper must hand back something guardable
# ---------------------------------------------------------------------------


def _loopback_pair_available() -> bool:
    """Both loopback families bindable here, or the counterpart test proves nothing."""
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.bind((address, 0))
        except OSError:
            return False
    return True


class TestSecondaryLoopbackReturnsItsSite:
    @pytest.mark.asyncio
    async def test_the_returned_site_is_live_on_every_platform(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same contract, with the BIND injected so no host capability gates it.

        The case below needs a real IPv6 loopback and is skipped where the host
        has none, which would leave the contract unasserted exactly on the
        platform the guards arm for. The bind is the only part that needs the
        second family, so it is the part replaced: ``_bind_once`` hands back a
        v4 loopback socket while the helper still resolves, starts and returns
        the counterpart address, which is what the caller guards.
        """
        app = web.Application()
        app.router.add_get("/api/live", _live)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            primary = web.TCPSite(runner, "127.0.0.1", 0)
            await primary.start()
            port = dashboard_server._resolved_bound_port(runner, 0)

            asked: list[tuple[str, int]] = []

            def _fake_bind(host: str, bind_port: int) -> socket.socket:
                asked.append((host, bind_port))
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(("127.0.0.1", 0))
                sock.listen(16)
                return sock

            monkeypatch.setattr(dashboard_server, "_bind_once", _fake_bind)

            result = await dashboard_server._start_secondary_loopback_site(
                runner, port, "127.0.0.1"
            )

            assert asked == [("::1", port)], "the counterpart family is what gets bound"
            assert result is not None
            assert result.address == "::1"
            sockets = getattr(getattr(result.site, "_server", None), "sockets", None)
            assert sockets, "the returned site must carry a live LISTEN socket"
            assert all(sock.fileno() != -1 for sock in sockets)
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _loopback_pair_available(), reason="both loopback families must be bindable"
    )
    async def test_the_second_listener_comes_back_with_a_live_site(self) -> None:
        """The address alone is unguardable: without the site nothing can observe its death.

        This is the precondition every candidate fix needed -- the helper started
        a ``web.SockSite`` and discarded it at return, so the sidecar it caused to
        be published could never be withdrawn.
        """
        app = web.Application()
        app.router.add_get("/api/live", _live)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            primary = web.TCPSite(runner, "127.0.0.1", 0)
            await primary.start()
            port = dashboard_server._resolved_bound_port(runner, 0)

            result = await dashboard_server._start_secondary_loopback_site(
                runner, port, "127.0.0.1"
            )

            assert result is not None
            assert result.address == "::1"
            assert result.site is not None
            sockets = getattr(getattr(result.site, "_server", None), "sockets", None)
            assert sockets, "the returned site must carry a live LISTEN socket"
            assert all(sock.fileno() != -1 for sock in sockets)
        finally:
            await runner.cleanup()


# ---------------------------------------------------------------------------
# A withdrawal that did NOT land is not cleanup
# ---------------------------------------------------------------------------


class TestWithdrawalIsAPreconditionForRecovery:
    """Withdrawing first is only a fix while the withdrawal actually happens.

    The rebind ladder runs up to ``max_attempts`` times with exponential backoff
    between attempts, and for all of it the address is unheld. That is safe ONLY
    because the claim was withdrawn first: a client reading coverage refuses and
    signs in explicitly instead of sending the credential to whoever took the
    address. When the sidecar can be neither unlinked nor blanked -- a sharing
    violation its own docstring calls ordinary on Windows -- the claim stands for
    that whole window, so spending the window buys nothing and risks everything.
    """

    @pytest.mark.asyncio
    async def test_a_reported_failure_takes_the_terminal_action_instead_of_rebinding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binds = 0
        reasons: list[str] = []
        async with _Guarded(
            interval=3600,
            max_attempts=5,
            backoff_base=0.0,
            max_backoff=0.0,
            on_listener_lost=lambda: False,
            on_give_up=reasons.append,
        ) as served:
            guard = served.guard
            assert guard is not None
            monkeypatch.setattr(guard, "listener_open", lambda: False)

            async def _counting_new_site() -> Any:
                nonlocal binds
                binds += 1
                raise OSError("never reached")

            monkeypatch.setattr(guard, "_new_site", _counting_new_site)

            assert await guard.check_now("test") is False

        assert binds == 0, "no rebind may be attempted while the claim still stands"
        assert len(reasons) == 1
        assert "withdraw" in reasons[0]

    @pytest.mark.asyncio
    async def test_a_hook_that_raised_counts_as_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The filesystem refusing IS the failure, whether it is returned or thrown."""

        def _refuses() -> bool:
            raise OSError("sharing violation")

        reasons: list[str] = []
        async with _Guarded(
            interval=3600,
            max_attempts=5,
            backoff_base=0.0,
            max_backoff=0.0,
            on_listener_lost=_refuses,
            on_give_up=reasons.append,
        ) as served:
            guard = served.guard
            assert guard is not None
            monkeypatch.setattr(guard, "listener_open", lambda: False)
            assert await guard.check_now("test") is False

        assert len(reasons) == 1

    @pytest.mark.asyncio
    async def test_a_hook_that_answers_nothing_still_recovers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control this pair needs: ``None`` is not a refusal.

        Most hooks report no outcome at all, so reading their silence as failure
        would stop every recovery in the product rather than the one this guards
        against. Without this case the two above pass for a guard that simply
        never rebinds.
        """
        events: list[str] = []
        async with _Guarded(
            interval=3600,
            on_listener_lost=lambda: events.append("lost"),
            on_listener_restored=lambda: events.append("restored"),
        ) as served:
            guard = served.guard
            assert guard is not None
            old_site = guard.site
            monkeypatch.setattr(guard, "listener_open", lambda: guard.site is not old_site)
            assert await guard.check_now("test") is True

        assert events == ["lost", "restored"]


class TestWithdrawSidecarReportsTheFile:
    """``_withdraw_listener_sidecar`` answers about the FILE, not about the call.

    Its caller escalates on False, so the two reasons the retraction returns
    False must not be collapsed: nothing of ours to retract (never published, or
    a repeat of a withdrawal that already succeeded -- the give-up path repeats it
    by design) versus a filesystem that refused and left the credential readable.
    """

    def test_an_unrecorded_claim_is_already_not_advertised(self) -> None:
        state = type("S", (), {})()
        assert dashboard_server._withdraw_listener_sidecar(state, "secondary") is True

    def test_a_repeat_withdrawal_reports_success(self, home: Path) -> None:
        port, address = 41411, "::1"
        path = run_marker.listener_secret_path(port, address)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("s3cret", encoding="utf-8")
        run_marker.note_published_listener(port, address)
        state = type("S", (), {})()
        state._listener_sidecars = {"secondary": (port, address, "s3cret")}

        assert dashboard_server._withdraw_listener_sidecar(state, "secondary") is True
        # The claim is gone from memory now, so the retraction itself answers
        # False on the repeat -- the file is what makes the answer True.
        assert run_marker.withdraw_published_listener(port, address) is False
        assert dashboard_server._withdraw_listener_sidecar(state, "secondary") is True

    def test_a_surviving_credential_reports_failure(self, home: Path) -> None:
        port, address = 41412, "::1"
        path = run_marker.listener_secret_path(port, address)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("s3cret", encoding="utf-8")
        run_marker.note_published_listener(port, address)
        state = type("S", (), {})()
        state._listener_sidecars = {"secondary": (port, address, "s3cret")}

        def _refuse(_self: Path, missing_ok: bool = False) -> None:
            raise OSError("sharing violation")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "unlink", _refuse)
            mp.setattr(run_marker, "_blank_listener_secret", lambda _p: False)
            assert dashboard_server._withdraw_listener_sidecar(state, "secondary") is False
        assert path.read_text(encoding="utf-8") == "s3cret"


class TestTerminalEscalationReachesTheLoop:
    """The escalation runs in a worker thread, so its shutdown must be marshalled.

    ``request_exit`` is only ever called from a terminal hook, and every hook runs
    in the executor (:meth:`ListenerGuard._notify`). The process-wide shutdown
    event binds an ``asyncio.Event`` to the loop that first touched it, so a
    ``set()`` from a thread with no running loop flips only the proxy's pending
    flag while the bound Event -- what ``is_set()`` reads on the loop -- stays
    clear. An escalation that cannot be observed leaves the gateway serving with a
    readable credential for an address it does not hold, and nothing revisits it.

    The real proxy is used deliberately: a bare ``asyncio.Event`` would record the
    flag even when set off-loop, so this case would pass for the broken version.
    """

    @pytest.mark.asyncio
    async def test_an_exit_requested_off_loop_is_observed_on_the_loop(self) -> None:
        shutdown = _LazyShutdownEvent()
        app = web.Application()
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            guard = ListenerGuard(runner, site, shutdown, interval=3600)
            guard.arm()
            # Bind the proxy's Event to THIS loop first, exactly as the gateway
            # does long before any listener dies -- an unbound proxy would take
            # the pending-flag path and hide the defect.
            assert shutdown.is_set() is False

            await asyncio.to_thread(guard.request_exit, "sidecar could not be withdrawn")

            assert guard.exit_code == LISTENER_LOST_EXIT_CODE
            await asyncio.sleep(0)
            assert shutdown.is_set() is True, "the loop never observed the exit request"
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_an_unarmed_guard_still_requests_the_exit(self) -> None:
        """No loop captured means nothing to marshal onto, so the direct call stands."""
        shutdown = _LazyShutdownEvent()
        app = web.Application()
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            guard = ListenerGuard(runner, site, shutdown, interval=3600)
            guard.request_exit("never armed")
            assert guard.exit_code == LISTENER_LOST_EXIT_CODE
            assert shutdown.is_set() is True
        finally:
            await runner.cleanup()


class TestEveryPublishedClaimIsReconciled:
    """Publication is an await, so a claim is checked the moment it is recorded.

    Two different holes, one remedy. For the SECOND family the guard is not armed
    until after the write, so a listener that dies inside it fires nothing. For the
    PRIMARY the guard IS armed before the write, but its claim is recorded after, and
    a withdrawal with no recorded claim answers True without touching the file --
    which recovery reads as "the address is safe to leave free" and rebinds behind a
    sidecar that still names it. Neither is revisited: the probe is 60 seconds away.
    """

    @staticmethod
    def _state_with(guard: Any, attr: str) -> Any:
        state = type("S", (), {})()
        setattr(state, attr, guard)
        state._background_tasks = set()
        return state

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("which", "attr"),
        [("primary", "_listener_guard"), ("secondary", "_secondary_listener_guard")],
    )
    async def test_a_closed_listener_goes_to_the_guards_own_recovery(
        self, which: str, attr: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _Guarded(interval=3600) as served:
            guard = served.guard
            assert guard is not None
            monkeypatch.setattr(guard, "listener_open", lambda: False)
            checked: list[str] = []

            async def _record(reason: str) -> bool:
                checked.append(reason)
                return True

            monkeypatch.setattr(guard, "check_now", _record)
            state = self._state_with(guard, attr)

            dashboard_server._reconcile_listener_publication(state, which, 45001, "127.0.0.1")
            # The reconcile schedules rather than awaits, so boot never waits on a
            # rebind; one loop turn is what makes the scheduled call observable.
            await asyncio.sleep(0)

            assert len(checked) == 1, f"{which}: the guard's own entry point must be called"
            assert "publication" in checked[0]

    @pytest.mark.asyncio
    async def test_a_live_listener_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The control: a reconcile that fires on a HEALTHY boot would churn every start."""
        async with _Guarded(interval=3600) as served:
            guard = served.guard
            assert guard is not None
            monkeypatch.setattr(guard, "listener_open", lambda: True)
            checked: list[str] = []

            async def _record(reason: str) -> bool:
                checked.append(reason)
                return True

            monkeypatch.setattr(guard, "check_now", _record)
            state = self._state_with(guard, "_listener_guard")

            dashboard_server._reconcile_listener_publication(state, "primary", 45002, "127.0.0.1")
            await asyncio.sleep(0)

            assert checked == []

    @pytest.mark.asyncio
    async def test_an_unarmed_guard_is_not_required(self) -> None:
        """POSIX arms no guard at all, and boot must not care."""
        state = type("S", (), {})()
        state._background_tasks = set()
        dashboard_server._reconcile_listener_publication(state, "secondary", 45003, "::1")
        await asyncio.sleep(0)
        assert state._background_tasks == set()
