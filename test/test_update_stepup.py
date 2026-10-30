"""Tests for the in-app wheel update step-up (arm + host-local approve).

The mechanism's whole point is what each half CANNOT do: the arm response
never carries the nonce (a dashboard bearer must not be able to approve), and
an approve without the exact armed nonce is refused. Both halves are pinned
here, plus TTL expiry, single-use consumption, and the endpoint refusals.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.handlers import updates
from kiro_crew.platform import update_stepup
from kiro_crew.testing.wait import await_until


def _request(
    body: object = None,
    *,
    remote: str = "127.0.0.1",
    marks: dict[str, object] | None = None,
    unix_socket: bool = False,
) -> MagicMock:
    req = MagicMock()

    async def _json() -> object:
        if isinstance(body, Exception):
            raise body
        return body

    req.json = _json
    req.remote = remote
    # aiohttp requests are Mapping-like; a bare MagicMock.get returns a truthy
    # MagicMock, which would make the handler's auth-mark check pass vacuously
    # and hide a broken locality gate. Model the mapping explicitly.
    store = dict(marks or {})
    req.get = store.get
    req.__contains__ = lambda self, key: key in store
    if unix_socket:
        # The shared discriminator reads the transport's socket family.
        import socket as _socket

        sock = MagicMock()
        sock.family = _socket.AF_UNIX
        req.transport.get_extra_info = lambda key, default=None: (
            sock if key == "socket" else default
        )
    else:
        req.transport.get_extra_info = lambda key, default=None: default
    state = MagicMock()
    state._background_tasks = set()
    req.app = {"state": state}
    return req


class TestStepUpModule:
    def test_arm_writes_owner_only_file_and_read_round_trips(self) -> None:
        pending = update_stepup.arm("9.9.9", "stable")
        try:
            path = update_stepup.pending_path()
            assert path.exists()
            # POSIX mode bits only: on Windows restrict_to_owner grants via
            # DACL and stat().st_mode reports 0o666 regardless, so the POSIX
            # assertion would test the platform, not the code.
            from kiro_crew.platform_compat import IS_POSIX

            if IS_POSIX:
                mode = path.stat().st_mode & 0o777
                assert mode == 0o600, f"nonce file must be owner-only, got {oct(mode)}"
            read = update_stepup.read_pending()
            assert read is not None
            assert read.nonce == pending.nonce
            assert read.version == "9.9.9"
        finally:
            update_stepup.clear_pending()

    def test_public_view_never_carries_the_nonce(self) -> None:
        pending = update_stepup.arm("9.9.9", "stable")
        try:
            view = update_stepup.public_view(pending)
            flat = json.dumps(view)
            assert pending.nonce not in flat
            assert view["armed"] is True
            assert view["request_id"] == pending.request_id
        finally:
            update_stepup.clear_pending()

    def test_consume_is_single_use(self) -> None:
        pending = update_stepup.arm("9.9.9", "stable")
        got = update_stepup.consume(pending.nonce)
        assert got.version == "9.9.9"
        with pytest.raises(update_stepup.StepUpError, match="no armed update request"):
            update_stepup.consume(pending.nonce)

    def test_consume_refuses_when_the_nonce_cannot_be_removed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending = update_stepup.arm("9.9.9", "stable")
        path = update_stepup.pending_path()
        original_unlink = Path.unlink

        def refuse_nonce_unlink(target: Path, *args, **kwargs) -> None:
            if target == path:
                raise PermissionError("nonce file is locked")
            original_unlink(target, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", refuse_nonce_unlink)
            with pytest.raises(update_stepup.StepUpError, match="could not consume"):
                update_stepup.consume(pending.nonce)

        still_pending = update_stepup.read_pending()
        assert still_pending is not None
        assert still_pending.nonce == pending.nonce
        update_stepup.clear_pending()

    def test_in_flight_consume_cannot_unlink_a_concurrent_rearm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A consume that validated request A must never remove a request B
        that a concurrent arm swapped in between the read and the unlink —
        arm and approve run as executor threads in one process, so the
        window is real. The mutex serializes them: the re-arm lands only
        after the consume's critical section, and its nonce survives."""
        first = update_stepup.arm("9.9.9", "stable")
        in_window = threading.Event()
        original_consume = update_stepup._consume_pending_file

        def paused_consume() -> None:
            in_window.set()
            # Without the mutex this sleep is exactly the race window: the
            # re-arm thread swaps its fresh request in, and the unlink
            # below destroys it while the stale approval is accepted.
            time.sleep(0.3)
            original_consume()

        rearmed: list[update_stepup.PendingUpdate] = []

        def rearm() -> None:
            # A False return means consume() raised before opening the
            # window: do NOT arm — past teardown that write would land in
            # the operator's real data home, not the test's.
            if in_window.wait(timeout=5):
                rearmed.append(update_stepup.arm("9.9.10", "stable"))

        rearm_thread = threading.Thread(target=rearm)
        with monkeypatch.context() as patch:
            patch.setattr(update_stepup, "_consume_pending_file", paused_consume)
            rearm_thread.start()
            try:
                consumed = update_stepup.consume(first.nonce)
            finally:
                # Always release and reap the worker INSIDE the fixture's
                # scope, even when consume() raises — a leaked thread would
                # outlive the isolated KIROCREW_HOME.
                in_window.set()
                rearm_thread.join(timeout=5)
        assert not rearm_thread.is_alive()
        assert consumed.request_id == first.request_id
        still_pending = update_stepup.read_pending()
        assert still_pending is not None
        assert still_pending.request_id == rearmed[0].request_id
        update_stepup.clear_pending()

    def test_wrong_nonce_refused_and_not_consumed(self) -> None:
        pending = update_stepup.arm("9.9.9", "stable")
        try:
            with pytest.raises(update_stepup.StepUpError, match="does not match"):
                update_stepup.consume("0" * 64)
            # The armed request survives a failed guess.
            assert update_stepup.read_pending() is not None
            update_stepup.consume(pending.nonce)
        finally:
            update_stepup.clear_pending()

    @pytest.mark.parametrize("bad", ["é" * 64, "0" * 63 + "\udcff"])
    def test_a_non_ascii_nonce_is_refused_and_not_consumed(self, bad: str) -> None:
        # The nonce arrives in a JSON body, where non-ASCII text and lone
        # surrogates are ordinary str values. hmac.compare_digest raises
        # TypeError on a non-ASCII str, which is not the StepUpError the
        # approve handler refuses (and audits) on.
        pending = update_stepup.arm("9.9.9", "stable")
        try:
            with pytest.raises(update_stepup.StepUpError, match="does not match"):
                update_stepup.consume(bad)
            assert update_stepup.read_pending() is not None
            update_stepup.consume(pending.nonce)
        finally:
            update_stepup.clear_pending()

    def test_expired_request_reads_as_absent_and_is_removed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending = update_stepup.arm("9.9.9", "stable")
        monkeypatch.setattr(
            time, "time", lambda: pending.created_at + update_stepup.PENDING_TTL_SECS + 1
        )
        assert update_stepup.read_pending(clear_expired=True) is None
        assert not update_stepup.pending_path().exists()

    def test_cross_process_reader_never_deletes_the_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DEFAULT read never writes. A reader outside the gateway (the
        `kirocrew update approve` CLI) uses the default: its unlink cannot be
        serialized against a gateway re-arm, so an expired read must report
        absent WITHOUT touching the file — otherwise it could delete a fresh
        request it never read. Cleanup
        is the explicit clear_expired=True opt-in for gateway callers."""
        pending = update_stepup.arm("9.9.9", "stable")
        monkeypatch.setattr(
            time, "time", lambda: pending.created_at + update_stepup.PENDING_TTL_SECS + 1
        )
        assert update_stepup.read_pending() is None
        assert update_stepup.pending_path().exists()
        update_stepup.clear_pending()

    def test_malformed_file_reads_as_absent(self) -> None:
        path = update_stepup.pending_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        try:
            assert update_stepup.read_pending() is None
        finally:
            update_stepup.clear_pending()

    def test_second_arm_replaces_the_first(self) -> None:
        first = update_stepup.arm("9.9.9", "stable")
        second = update_stepup.arm("9.9.10", "stable")
        try:
            with pytest.raises(update_stepup.StepUpError):
                update_stepup.consume(first.nonce)
            got = update_stepup.consume(second.nonce)
            assert got.version == "9.9.10"
        finally:
            update_stepup.clear_pending()


def _freeze_check(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Neutralise the arm handler's pre-arm re-check, counting its calls.

    The handler re-consults the feed so an approval installs the NEWEST build
    rather than a verdict up to 12 hours old. Tests that stage `_update_info`
    by hand need that refresh stubbed out or the real check would overwrite the
    state under test -- and reach the network from a unit test.
    """
    calls: list[int] = []

    # Mirrors the real signature (no arguments): the arm path awaits the shared
    # check task, so a check already in flight is joined rather than no-opped onto
    # the stale cache. The stub only has to be awaitable to neutralise it.
    async def _noop() -> None:
        calls.append(1)

    monkeypatch.setattr(updates, "_do_update_check", _noop)
    return calls


def _release_once_the_arm_joins(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """Signal the moment the arm handler enters the shared check.

    The arm hops through two thread offloads before it re-checks, so a test that
    released the blocked worker after one loop tick would race it: the worker would
    finish first, the arm would then start a FRESH check, and both outcomes look the
    same from the cache. Gating the release on this event is what makes "joined the
    running worker" the only way the arm can have been answered.
    """
    reached = asyncio.Event()
    real = updates._do_update_check

    async def _spy() -> None:
        reached.set()
        await real()

    monkeypatch.setattr(updates, "_do_update_check", _spy)
    return reached


@pytest.mark.asyncio
class TestArmEndpoint:
    async def test_arm_refuses_non_managed_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.platform import wheel_engine

        checks = _freeze_check(monkeypatch)
        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: False)
        resp = await updates.api_update_arm(_request())
        assert resp.status == 409
        assert json.loads(resp.body.decode())["code"] == "arm_wrong_shape"
        assert checks == [], "a host that cannot apply an update must not pay a feed round trip"

    async def test_arm_rechecks_and_pins_what_the_feed_serves_NOW(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cached verdict can be 12 hours old (the background poll's period).

        Arming it would either pin the user to a build the feed has already
        superseded or dead-end the flow, because the apply refuses any version
        the feed has moved past -- arm, host approval, refusal, start over. So
        the arm re-checks first and pins that answer.
        """
        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        updates._set_update_info(update_available=True, latest_version="9.9.9", channel="stable")

        async def _fresh_check() -> None:
            # A newer release published since the cached verdict was computed.
            updates._set_update_info(
                update_available=True, latest_version="9.9.10", channel="stable"
            )

        monkeypatch.setattr(updates, "_do_update_check", _fresh_check)
        try:
            resp = await updates.api_update_arm(_request())
            assert resp.status == 200
            on_disk = update_stepup.read_pending()
            assert on_disk is not None
            assert on_disk.version == "9.9.10", (
                "armed the stale cached version -- the approve would install a build the "
                "feed has already superseded, or be refused outright"
            )
        finally:
            update_stepup.clear_pending()
            updates._set_update_info()

    async def test_arm_response_carries_the_folded_display_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The panel offered a version up to 12h old; the arm may pin a newer one.

        So the response has to say WHICH version was armed, and say it the way
        the rest of the UI does -- folded, because the raw stamp of a promoted
        stable build reads `0.4.1rc1` and naming a prerelease the user never
        chose is its own dishonesty. The armed `version` stays raw: the apply
        compares it byte-for-byte.
        """
        from kiro_crew.platform import wheel_engine

        _freeze_check(monkeypatch)
        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        updates._set_update_info(update_available=True, latest_version="0.4.1rc1", channel="stable")
        try:
            resp = await updates.api_update_arm(_request())
            assert resp.status == 200
            payload = json.loads(resp.body.decode())
            assert payload["version"] == "0.4.1rc1"
            assert payload["version_display"] == "0.4.1"
        finally:
            update_stepup.clear_pending()
            updates._set_update_info()

    async def test_arm_refuses_when_the_fresh_check_finds_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A retraction between the cached verdict and the click refuses the arm.

        And says so in its own words. The panel renders ``error`` verbatim, so the
        no-verdict message ("run a check first") would be a wrong statement of fact
        here -- a check just ran, and running another returns this same refusal.
        """
        from kiro_crew.platform import wheel_engine
        from kiro_crew.platform.update_capability import CHECK_SUCCEEDED

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        updates._set_update_info(update_available=True, latest_version="9.9.9", channel="stable")

        async def _retracted() -> None:
            updates._set_update_info(
                update_available=False,
                latest_version="",
                channel="stable",
                check_status=CHECK_SUCCEEDED,
            )

        monkeypatch.setattr(updates, "_do_update_check", _retracted)
        try:
            resp = await updates.api_update_arm(_request())
            assert resp.status == 409
            payload = json.loads(resp.body.decode())
            assert payload["code"] == "arm_no_longer_offered"
            assert "run a check first" not in payload["error"]
            assert update_stepup.read_pending() is None
        finally:
            update_stepup.clear_pending()
            updates._set_update_info()

    async def test_arm_refuses_when_the_fresh_check_cannot_reach_the_feed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A feed that cannot answer refuses the arm; the failed check stays visible.

        The apply fetches the signed manifest before it installs anything, so an
        arm the feed could not confirm would only move the refusal to the approval
        step, after the user typed the host command. Refusing here, with the failed
        check left in the cache for the panel to show, says the true thing at the
        moment the user can still act on it.
        """
        from kiro_crew.platform import wheel_engine
        from kiro_crew.platform.update_capability import CHECK_FAILED

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        updates._set_update_info(update_available=True, latest_version="9.9.9", channel="stable")

        async def _unreachable() -> None:
            updates._set_update_info(
                update_available=None, latest_version="", check_status=CHECK_FAILED
            )

        monkeypatch.setattr(updates, "_do_update_check", _unreachable)
        try:
            resp = await updates.api_update_arm(_request())
            assert resp.status == 409
            assert json.loads(resp.body.decode())["code"] == "arm_no_verdict"
            assert update_stepup.read_pending() is None, "nothing may be armed on a failed check"
            assert (
                updates._update_info["check_status"] == CHECK_FAILED
            ), "the failed check is what the panel shows; it must not be papered over"
        finally:
            update_stepup.clear_pending()
            updates._set_update_info()

    async def test_arm_refuses_without_a_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No verdict at all keeps the "run a check first" refusal.

        `update_available: None` is the absence of an answer, not the answer "no" --
        the distinction the retraction test above pins from the other side. Here the
        advice is correct and actionable, so it must survive.
        """
        from kiro_crew.platform import wheel_engine

        _freeze_check(monkeypatch)
        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        updates._set_update_info(update_available=None, latest_version="")
        resp = await updates.api_update_arm(_request())
        assert resp.status == 409
        payload = json.loads(resp.body.decode())
        assert payload["code"] == "arm_no_verdict"
        assert "run a check first" in payload["error"]

    async def test_arm_refuses_when_check_reports_nothing_to_apply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        _freeze_check(monkeypatch)
        updates._set_update_info(
            update_available=False,
            channel_move_pending=False,
            latest_version="0.6.0",
            channel="stable",
        )
        resp = await updates.api_update_arm(_request())
        assert resp.status == 409
        # A check that reached the feed and found nothing is a POSITIVE answer,
        # not the absence of one.
        assert json.loads(resp.body.decode())["code"] == "arm_no_longer_offered"

    async def test_arm_refuses_an_upgrade_click_that_the_recheck_turns_into_a_downgrade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lane rolled back below the running build between the offer and the click.

        The fresh verdict is then only a pending channel move -- armable on its own,
        but a DOWNGRADE. The user clicked an upgrade, so arming the move would install
        an older build nobody chose; the offer they clicked is withdrawn instead.
        """
        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        updates._set_update_info(update_available=True, latest_version="0.4.8", channel="stable")

        async def _rolled_back() -> None:
            updates._set_update_info(
                update_available=False,
                channel_move_pending=True,
                latest_version="0.4.6",
                channel="stable",
            )

        monkeypatch.setattr(updates, "_do_update_check", _rolled_back)
        try:
            resp = await updates.api_update_arm(_request())
            assert resp.status == 409
            assert json.loads(resp.body.decode())["code"] == "arm_offer_withdrawn"
            assert update_stepup.read_pending() is None
        finally:
            update_stepup.clear_pending()
            updates._set_update_info()

    async def test_arm_accepts_a_pending_channel_downgrade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        _freeze_check(monkeypatch)
        monkeypatch.setattr(updates, "min_version", lambda: "0.5.0")
        updates._set_update_info(
            update_available=False,
            channel_move_pending=True,
            latest_version="0.6.0rc4",
            channel="insider",
        )
        try:
            resp = await updates.api_update_arm(_request())
            assert resp.status == 200
            pending = update_stepup.read_pending()
            assert pending is not None
            assert pending.version == "0.6.0rc4"
            assert pending.channel == "insider"
        finally:
            update_stepup.clear_pending()
            updates._set_update_info()

    async def test_a_straddling_check_is_awaited_rather_than_arming_the_stale_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The re-check must not silently no-op onto the cache it exists to replace.

        `_do_update_check` coalesces every caller onto ONE shared worker task, so a
        status poll that already owns the worker must not make the arm's re-check
        return instantly with the cached verdict -- arming the very stale version
        the re-check is there to catch, which approval then rejects. The arm joins
        the running worker and reads ITS answer.
        """
        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        updates._set_update_info(update_available=True, latest_version="0.4.7", channel="stable")

        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_worker() -> None:
            # The running check finds a build published since the offer, but only
            # after the arm has had to decide whether to wait for it.
            started.set()
            await release.wait()
            updates._set_update_info(
                update_available=True, latest_version="0.4.8", channel="stable"
            )

        monkeypatch.setattr(updates, "_run_update_check", _slow_worker)
        # A poll owns the worker, and its check has not answered yet. The worker is
        # released only once the arm has REACHED the shared check, so the arm can
        # only be answered by joining the running worker -- a fresh one would need
        # the poll's to have finished first.
        poll = asyncio.create_task(updates._do_update_check())
        await await_until(started, what="the running check to start")
        reached = _release_once_the_arm_joins(monkeypatch)
        try:
            arm = asyncio.create_task(updates.api_update_arm(_request()))
            await await_until(reached, what="the arm to join the running check")
            assert not arm.done(), "the arm returned before the running check answered"
            release.set()
            resp = await arm
            await poll
            assert resp.status == 200
            pending = update_stepup.read_pending()
            assert pending is not None
            # 0.4.7 here would be the bug: the stale offer armed and later rejected.
            assert pending.version == "0.4.8"
        finally:
            await updates._cancel_update_check()
            update_stepup.clear_pending()
            updates._set_update_info()

    async def test_a_straddling_arm_joins_the_running_check_instead_of_refetching(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Joining is a JOIN, not a second fetch.

        The shared worker exists so concurrent callers -- a status poll and the
        arm's re-check landing together -- cost the feed one round trip, not one
        per caller. An arm that started its own check would serialize behind the
        poll's and hit the CDN twice for one verdict.
        """
        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        updates._set_update_info(update_available=True, latest_version="0.4.7", channel="stable")

        started = asyncio.Event()
        release = asyncio.Event()
        runs = 0

        async def _counted_worker() -> None:
            nonlocal runs
            runs += 1
            started.set()
            await release.wait()

        monkeypatch.setattr(updates, "_run_update_check", _counted_worker)
        poll = asyncio.create_task(updates._do_update_check())
        await await_until(started, what="the running check to start")
        reached = _release_once_the_arm_joins(monkeypatch)
        try:
            arm = asyncio.create_task(updates.api_update_arm(_request()))
            await await_until(reached, what="the arm to join the running check")
            release.set()
            resp = await arm
            await poll
            assert resp.status == 200
            assert runs == 1, "the arm re-ran the check instead of joining the poll's"
        finally:
            await updates._cancel_update_check()
            update_stepup.clear_pending()
            updates._set_update_info()

    async def test_arm_refuses_a_channel_move_below_the_minimum_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        _freeze_check(monkeypatch)
        monkeypatch.setattr(updates, "min_version", lambda: "0.6.0")
        monkeypatch.setattr(updates, "_local_version", "0.7.0")
        audit = MagicMock()
        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: audit)
        updates._set_update_info(
            update_available=False,
            channel_move_pending=True,
            latest_version="0.5.0",
            channel="stable",
        )
        try:
            resp = await updates.api_update_arm(_request())
            payload = json.loads(resp.body.decode())
            assert resp.status == 409
            assert payload["code"] == "arm_below_min_version"
            assert payload["governance"] is True
            assert update_stepup.read_pending() is None
            audit.log_api_access.assert_called_once_with(
                caller="127.0.0.1",
                operation="update.arm",
                outcome="denied",
                source="dashboard",
                resources="v0.5.0 (stable)",
                error="selected release is below the required minimum version",
                critical=True,
            )
        finally:
            update_stepup.clear_pending()
            updates._set_update_info()

    async def test_arm_floor_denial_survives_an_unwritable_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        _freeze_check(monkeypatch)
        monkeypatch.setattr(updates, "min_version", lambda: "0.6.0")
        monkeypatch.setattr(updates, "_local_version", "0.7.0")

        class _BrokenSel:
            def log_api_access(self, **kwargs: object) -> None:
                raise OSError(28, "no space left on device")

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _BrokenSel())
        updates._set_update_info(
            update_available=False,
            channel_move_pending=True,
            latest_version="0.5.0",
            channel="stable",
        )
        try:
            resp = await updates.api_update_arm(_request())
            payload = json.loads(resp.body.decode())
            assert resp.status == 409
            assert payload["code"] == "arm_below_min_version"
            assert update_stepup.read_pending() is None
        finally:
            update_stepup.clear_pending()
            updates._set_update_info()

    async def test_arm_allows_an_upgrade_toward_the_minimum_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        _freeze_check(monkeypatch)
        monkeypatch.setattr(updates, "min_version", lambda: "0.6.0")
        monkeypatch.setattr(updates, "_local_version", "0.4.0")
        updates._set_update_info(
            update_available=True,
            channel_move_pending=False,
            latest_version="0.5.0",
            channel="stable",
        )
        try:
            resp = await updates.api_update_arm(_request())
            assert resp.status == 200
            pending = update_stepup.read_pending()
            assert pending is not None
            assert pending.version == "0.5.0"
        finally:
            update_stepup.clear_pending()
            updates._set_update_info()

    async def test_arm_response_carries_no_nonce(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.platform import wheel_engine

        _freeze_check(monkeypatch)
        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        updates._set_update_info(update_available=True, latest_version="9.9.9", channel="stable")
        try:
            resp = await updates.api_update_arm(_request())
            assert resp.status == 200
            payload = json.loads(resp.body.decode())
            assert payload["armed"] is True
            assert payload["approve_command"] == "kirocrew update approve"
            on_disk = update_stepup.read_pending()
            assert on_disk is not None
            assert on_disk.nonce not in resp.body.decode()
        finally:
            update_stepup.clear_pending()
            updates._set_update_info()

    async def test_arms_the_raw_promoted_stamp_not_a_folded_display_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A promoted stable candidate's `_update_info["latest_version"]` still
        carries its insider/rc stamp (promotion never re-stamps the bytes). The
        pending request's `version` MUST be that exact stamp -- the shadow-venv
        apply step later compares it byte-for-byte against the installed
        build's own never-folded `__version__`. Arming a display-folded value
        (e.g. "0.4.0" instead of "0.4.0rc14") would make apply fail every time
        on the stable channel."""
        from kiro_crew.platform import wheel_engine

        _freeze_check(monkeypatch)
        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(updates, "min_version", lambda: "0.4.0")
        updates._set_update_info(
            update_available=True, latest_version="0.4.0rc14", channel="stable"
        )
        try:
            resp = await updates.api_update_arm(_request())
            assert resp.status == 200
            on_disk = update_stepup.read_pending()
            assert on_disk is not None
            assert on_disk.version == "0.4.0rc14"
        finally:
            update_stepup.clear_pending()
            updates._set_update_info()

    async def test_policy_managed_host_refuses_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.platform import wheel_engine

        checks = _freeze_check(monkeypatch)
        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: object())
        resp = await updates.api_update_arm(_request())
        assert resp.status == 409
        assert json.loads(resp.body.decode())["code"] == "arm_policy_managed"
        assert checks == [], "policy owns this host's updates; the arm must not check its feed"


def _assert_update_lock_free() -> None:
    """The layout's update lock can be taken again (raises ``WheelUpdateBusy`` if not).

    The layout sits beside the scratch data home (the conftest floor clears
    ``KIROCREW_VENV``), so this takes a real lock no operator install uses.
    """
    from kiro_crew.platform import wheel_engine

    wheel_engine.release_update_lock(wheel_engine.hold_update_lock())


@pytest.mark.asyncio
class TestApproveEndpoint:
    @pytest.fixture(autouse=True)
    def _host_isolated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The host's AppArmor state is never read, and no apply is in flight."""
        from kiro_crew.platform import wheel_apply

        monkeypatch.setattr(wheel_apply, "userns_reattach_needed", lambda _version: False)
        monkeypatch.setattr(wheel_apply, "_IN_FLIGHT", set())

    async def test_non_loopback_peer_refused(self) -> None:
        resp = await updates.api_update_approve(_request({"nonce": "x"}, remote="10.0.0.9"))
        assert resp.status == 403
        assert json.loads(resp.body.decode())["code"] == "approve_not_local"

    @pytest.mark.parametrize("reason", ["nonce", "version", "floor", "audit"])
    async def test_refusal_releases_the_lock_once_off_loop(
        self, monkeypatch: pytest.MonkeyPatch, reason: str
    ) -> None:
        from kiro_crew.platform import wheel_engine

        loop_thread = threading.current_thread()
        released: list[tuple[int, threading.Thread]] = []
        real_release = wheel_engine.release_update_lock

        def release(fd: int) -> None:
            released.append((fd, threading.current_thread()))
            real_release(fd)

        async def audit(_request: object, **kwargs: object) -> None:
            if reason == "audit" and kwargs["outcome"] == "granted":
                raise OSError("audit unavailable")

        monkeypatch.setattr(wheel_engine, "release_update_lock", release)
        monkeypatch.setattr(updates, "_audit_update_event", audit)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(updates, "min_version", lambda: "0.6.0" if reason == "floor" else "")
        monkeypatch.setattr(updates, "_local_version", "0.7.0")
        version = {"version": "9.9/9", "floor": "0.5.0"}.get(reason, "9.9.9")
        pending = update_stepup.arm(version, "stable")
        try:
            nonce = "0" * 64 if reason == "nonce" else pending.nonce
            req = _request({"nonce": nonce})
            resp = await updates.api_update_approve(req)
            assert resp.status == {"nonce": 403, "version": 409, "floor": 409, "audit": 503}[reason]
            assert req.app["state"]._background_tasks == set()
            assert len(released) == 1, "each refusal releases exactly once"
            assert released[0][1] is not loop_thread
            _assert_update_lock_free()
        finally:
            update_stepup.clear_pending()

    async def test_cancelling_a_refusal_cannot_cancel_its_lock_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from kiro_crew.platform import wheel_engine

        entered, finish, released = threading.Event(), threading.Event(), threading.Event()
        loop_thread = threading.current_thread()
        releases: list[threading.Thread] = []
        real_release = wheel_engine.release_update_lock

        def release(fd: int) -> None:
            releases.append(threading.current_thread())
            entered.set()
            assert finish.wait(30), "the test never allowed refusal cleanup to finish"
            real_release(fd)
            released.set()

        monkeypatch.setattr(wheel_engine, "release_update_lock", release)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(updates, "_audit_update_event", AsyncMock())
        update_stepup.arm("9.9.9", "stable")
        req = _request({"nonce": "0" * 64})
        task = asyncio.create_task(updates.api_update_approve(req))
        try:
            assert await asyncio.to_thread(entered.wait, 30), "refusal never reached cleanup"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=30)
            assert not released.is_set()
        finally:
            finish.set()
            assert await asyncio.to_thread(released.wait, 30), "refusal did not release the lock"
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=30)
            update_stepup.clear_pending()
        assert len(releases) == 1 and releases[0] is not loop_thread
        assert req.app["state"]._background_tasks == set()
        await asyncio.to_thread(_assert_update_lock_free)

    async def test_wrong_nonce_refused(self) -> None:
        update_stepup.arm("9.9.9", "stable")
        try:
            resp = await updates.api_update_approve(_request({"nonce": "0" * 64}))
            assert resp.status == 403
            assert json.loads(resp.body.decode())["code"] == "approve_refused"
            _assert_update_lock_free()
        finally:
            update_stepup.clear_pending()

    async def test_a_non_ascii_nonce_is_refused_and_audited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audited: list[dict[str, object]] = []

        async def fake_audit(request: object, **kwargs: object) -> None:
            audited.append(kwargs)

        monkeypatch.setattr(updates, "_audit_update_event", fake_audit)
        update_stepup.arm("9.9.9", "stable")
        try:
            resp = await updates.api_update_approve(_request({"nonce": "é" * 64}))
            assert resp.status == 403
            assert json.loads(resp.body.decode())["code"] == "approve_refused"
            assert [a["outcome"] for a in audited] == ["denied"]
            assert audited[0]["operation"] == "update.approve"
            assert update_stepup.read_pending() is not None
        finally:
            update_stepup.clear_pending()

    @pytest.mark.skipif(
        not hasattr(__import__("socket"), "AF_UNIX"),
        reason="AF_UNIX does not exist on this platform",
    )
    async def test_unix_socket_caller_with_empty_remote_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AF_UNIX is the CLI's preferred transport and has NO loopback IP."""
        update_stepup.clear_pending()
        req = _request({"nonce": "irrelevant"}, remote="", unix_socket=True)
        resp = await updates.api_update_approve(req)
        # Past the locality gate: the refusal is the missing armed request
        # (403 approve_refused), not approve_not_local.
        assert json.loads(resp.body.decode())["code"] == "approve_refused"

    async def test_internal_auth_mark_passes_locality(self) -> None:
        update_stepup.clear_pending()
        req = _request({"nonce": "x"}, remote="", marks={"internal_auth": True})
        resp = await updates.api_update_approve(req)
        assert json.loads(resp.body.decode())["code"] == "approve_refused"

    async def test_empty_remote_without_socket_or_marks_refused(self) -> None:
        req = _request({"nonce": "x"}, remote="")
        resp = await updates.api_update_approve(req)
        assert json.loads(resp.body.decode())["code"] == "approve_not_local"

    async def test_provider_installed_after_arming_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A command provider that lands between arm and approve owns the update."""
        pending = update_stepup.arm("9.9.9", "stable")
        try:
            monkeypatch.setattr(updates, "resolve_provider", lambda: object())
            resp = await updates.api_update_approve(_request({"nonce": pending.nonce}))
            assert resp.status == 409
            assert json.loads(resp.body.decode())["code"] == "approve_policy_managed"
            # The armed request is NOT consumed by a policy refusal.
            assert update_stepup.read_pending() is not None
        finally:
            update_stepup.clear_pending()

    async def test_minimum_version_landing_after_arm_refuses_approve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stricter floor in the arm-to-approve window must win."""
        pending = update_stepup.arm("0.5.0", "stable")
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(updates, "min_version", lambda: "0.6.0")
        req = _request({"nonce": pending.nonce})
        resp = await updates.api_update_approve(req)
        payload = json.loads(resp.body.decode())
        assert resp.status == 409
        assert payload["code"] == "approve_below_min_version"
        assert payload["governance"] is True
        assert update_stepup.read_pending() is None
        assert req.app["state"]._background_tasks == set()

    async def test_channel_pin_landing_after_arm_refuses_approve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A channel pin in the arm-to-approve window must win, like the source pin."""
        pending = update_stepup.arm("0.5.0", "nightly")
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(
            "kiro_crew.platform.update_layout.policy_release_channel", lambda: "stable"
        )
        req = _request({"nonce": pending.nonce})
        resp = await updates.api_update_approve(req)
        payload = json.loads(resp.body.decode())
        assert resp.status == 403
        assert payload["code"] == "approve_blocked_by_policy"
        assert payload["governance"] is True
        assert update_stepup.read_pending() is None
        assert req.app["state"]._background_tasks == set()
        _assert_update_lock_free()

    async def test_a_version_outside_the_release_grammar_refuses_approve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refused before anything names its tree, with the update lock released."""
        audited: list[dict[str, object]] = []

        async def fake_audit(request: object, **kwargs: object) -> None:
            audited.append(kwargs)

        monkeypatch.setattr(updates, "_audit_update_event", fake_audit)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        pending = update_stepup.arm("9.9/9", "stable")
        req = _request({"nonce": pending.nonce})
        resp = await updates.api_update_approve(req)
        payload = json.loads(resp.body.decode())
        assert resp.status == 409
        assert payload["code"] == "approve_refused"
        assert "fails validation" in payload["error"]
        assert [a["outcome"] for a in audited] == ["denied"]
        assert req.app["state"]._background_tasks == set()
        _assert_update_lock_free()

    async def test_no_armed_request_refused(self) -> None:
        update_stepup.clear_pending()
        resp = await updates.api_update_approve(_request({"nonce": "abc"}))
        assert resp.status == 403

    async def test_valid_nonce_launches_the_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        applied: list[dict[str, object]] = []

        def fake_apply(**kwargs: object) -> None:
            applied.append(kwargs)

        restarted: list[object] = []

        async def fake_restart(state: object, *, resolver: object) -> bool:
            assert resolver is wheel_engine.respawn_executable
            restarted.append(state)
            return True

        from kiro_crew.platform import wheel_apply, wheel_engine

        monkeypatch.setattr(wheel_engine, "apply_wheel_update", fake_apply)
        monkeypatch.setattr(wheel_apply, "restart_reaches", lambda _version: True)
        monkeypatch.setattr(updates, "_restart_gateway", fake_restart)
        pending = update_stepup.arm("9.9.9", "stable")
        req = _request({"nonce": pending.nonce})
        resp = await updates.api_update_approve(req)
        assert resp.status == 200
        payload = json.loads(resp.body.decode())
        assert payload["status"] == "applying"
        # Drain the background task the handler scheduled.
        for task in list(req.app["state"]._background_tasks):
            await task
        assert len(applied) == 1
        assert applied[0]["expected_version"] == "9.9.9"
        assert restarted, "a promoted update must restart the gateway"
        # Single-use: the nonce file is gone.
        assert update_stepup.read_pending() is None
        _assert_update_lock_free()

    async def test_unwritable_audit_refuses_the_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A GRANTED approval whose SEL audit cannot be written must not install.

        The audit record is what makes an approval reconstructable; letting the
        install proceed when the write fails would be fail-open on the exact
        event the audit chain exists for. Denials remain best-effort.
        """
        applied: list[dict[str, object]] = []

        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "apply_wheel_update", lambda **kw: applied.append(kw))

        class _BrokenSel:
            def log_api_access(self, **kwargs: object) -> None:
                raise OSError(28, "no space left on device")

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _BrokenSel())
        pending = update_stepup.arm("9.9.9", "stable")
        req = _request({"nonce": pending.nonce})
        resp = await updates.api_update_approve(req)
        assert resp.status == 503
        payload = json.loads(resp.body.decode())
        assert payload["code"] == "approve_audit_failed"
        # No install task was scheduled.
        for task in list(req.app["state"]._background_tasks):
            await task
        assert applied == []
        _assert_update_lock_free()

    async def test_failed_apply_reports_and_does_not_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform.wheel_engine import WheelUpdateError

        def failing_apply(**kwargs: object) -> None:
            raise WheelUpdateError("wheel SHA-256 mismatch")

        restarted: list[object] = []

        async def fake_restart(state: object, *, resolver: object) -> bool:
            restarted.append(state)
            return True

        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "apply_wheel_update", failing_apply)
        monkeypatch.setattr(updates, "_restart_gateway", fake_restart)
        pending = update_stepup.arm("9.9.9", "stable")
        req = _request({"nonce": pending.nonce})
        resp = await updates.api_update_approve(req)
        assert resp.status == 200
        for task in list(req.app["state"]._background_tasks):
            await task
        assert not restarted, "a failed apply must never restart"
        state = req.app["state"]
        failed = [
            c.args
            for c in state.push_update_progress.call_args_list
            if c.args and c.args[0] == "failed"
        ]
        assert failed, "the failure must reach the progress feed"
        _assert_update_lock_free()

    async def test_an_incompatible_release_names_the_installer_rerun(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform import wheel_engine

        def incompatible(**kwargs: object) -> None:
            raise wheel_engine.WheelUpdateIncompatible(
                "kirocrew 9.9.9 requires Python >= 3.99", version="9.9.9", sha256="a" * 64
            )

        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(wheel_engine, "apply_wheel_update", incompatible)
        pending = update_stepup.arm("9.9.9", "stable")
        req = _request({"nonce": pending.nonce})
        await updates.api_update_approve(req)
        for task in list(req.app["state"]._background_tasks):
            await task
        steps = [c.args for c in req.app["state"].push_update_progress.call_args_list]
        assert any(
            step == "failed" and "3.99" in detail and "Re-run the installer" in detail
            for step, detail in steps
        ), steps
        _assert_update_lock_free()

    async def test_a_restart_in_progress_refuses_before_the_nonce_is_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The restart's exec would stop the apply before its outcome is audited."""
        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        locked: list[int] = []
        monkeypatch.setattr(wheel_engine, "hold_update_lock", lambda: locked.append(1) or 0)
        pending = update_stepup.arm("9.9.9", "stable")
        try:
            req = _request({"nonce": pending.nonce})
            req.app["state"]._gateway_restart_in_progress = True
            resp = await updates.api_update_approve(req)
            assert (resp.status, json.loads(resp.body.decode())["code"]) == (
                409,
                "approve_restarting",
            )
            assert update_stepup.read_pending() is not None, "the armed request is intact"
            assert locked == [] and req.app["state"]._background_tasks == set()
        finally:
            update_stepup.clear_pending()

    async def test_an_apply_already_holding_the_lock_refuses_before_the_nonce_is_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform import wheel_engine

        def busy() -> int:
            raise wheel_engine.WheelUpdateBusy("another kirocrew update is already in progress")

        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(wheel_engine, "hold_update_lock", busy)
        audited: list[str] = []
        monkeypatch.setattr(
            updates,
            "_audit_update_event",
            AsyncMock(side_effect=lambda *a, **k: audited.append(k["outcome"])),
        )
        pending = update_stepup.arm("9.9.9", "stable")
        try:
            resp = await updates.api_update_approve(_request({"nonce": pending.nonce}))
            assert resp.status == 409
            assert json.loads(resp.body.decode())["code"] == "approve_busy"
            assert update_stepup.read_pending() is not None, "the armed request is intact"
            assert audited == [], "a busy lock is not a failed install"
        finally:
            update_stepup.clear_pending()

    async def test_memory_still_preparing_defers_before_the_nonce_is_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform import wheel_apply, wheel_engine

        def preparing() -> None:
            raise wheel_engine.WheelUpdateNotReady("memory is still being prepared")

        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(wheel_apply, "check_memory_ready", preparing)
        pending = update_stepup.arm("9.9.9", "stable")
        try:
            resp = await updates.api_update_approve(_request({"nonce": pending.nonce}))
            assert resp.status == 409
            assert json.loads(resp.body.decode())["code"] == "approve_memory_preparing"
            assert update_stepup.read_pending() is not None, "the armed request is intact"
        finally:
            update_stepup.clear_pending()

    async def test_a_memory_failure_refuses_with_its_own_words_before_the_nonce(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform import wheel_apply, wheel_engine

        def failed() -> None:
            raise wheel_engine.WheelUpdateSnapshotFailed("Memory recovery failed: journal broken")

        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(wheel_apply, "check_memory_ready", failed)
        pending = update_stepup.arm("9.9.9", "stable")
        try:
            resp = await updates.api_update_approve(_request({"nonce": pending.nonce}))
            payload = json.loads(resp.body.decode())
            assert (resp.status, payload["code"]) == (409, "approve_memory")
            assert "journal broken" in payload["error"]
            assert update_stepup.read_pending() is not None
        finally:
            update_stepup.clear_pending()

    async def test_a_cancelled_apply_is_audited_as_cancelled_not_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform import wheel_engine

        def stopped(**_kw: object) -> None:
            raise wheel_engine.WheelUpdateCancelled("stopped", reason="exec")

        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(wheel_engine, "apply_wheel_update", stopped)
        audited: list[str] = []
        monkeypatch.setattr(
            updates,
            "_audit_update_event",
            AsyncMock(side_effect=lambda *a, **k: audited.append(k["outcome"])),
        )
        pending = update_stepup.arm("9.9.9", "stable")
        req = _request({"nonce": pending.nonce})
        await updates.api_update_approve(req)
        for task in list(req.app["state"]._background_tasks):
            await task
        assert audited == ["granted", "cancelled"]
        _assert_update_lock_free()
        failed = [
            c.args
            for c in req.app["state"].push_update_progress.call_args_list
            if c.args and c.args[0] == "failed"
        ]
        assert failed == [], "a deliberate stop pushes no failure"

    async def test_an_unlocked_apply_in_flight_here_does_not_burn_the_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The approval holds the lock, so an in-process apply that has not taken
        it yet loses on the lock and this one runs: the nonce is never spent on a
        ``busy`` that would make the operator approve again."""
        import asyncio

        from kiro_crew.platform import wheel_apply, wheel_engine
        from kiro_crew.platform.wheel_engine import ApplyCancel

        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(wheel_engine, "apply_wheel_update", lambda **_kw: None)
        monkeypatch.setattr(wheel_apply, "restart_reaches", lambda _version: True)
        restart = AsyncMock(return_value=True)
        monkeypatch.setattr(updates, "_restart_gateway", restart)
        other = asyncio.get_running_loop().create_future()
        wheel_apply._IN_FLIGHT.add(wheel_apply._Running(ApplyCancel(), other))
        audited: list[str] = []
        monkeypatch.setattr(
            updates,
            "_audit_update_event",
            AsyncMock(side_effect=lambda *a, **k: audited.append(k["outcome"])),
        )
        pending = update_stepup.arm("9.9.9", "stable")
        req = _request({"nonce": pending.nonce})
        resp = await updates.api_update_approve(req)
        assert resp.status == 200
        for task in list(req.app["state"]._background_tasks):
            await task
        assert audited == ["granted", "success"]
        restart.assert_awaited_once()
        other.cancel()
        _assert_update_lock_free()

    async def test_a_promotion_that_detaches_the_sandbox_profile_says_how_to_reattach(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform import wheel_apply, wheel_engine

        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(wheel_engine, "apply_wheel_update", lambda **_kw: None)
        monkeypatch.setattr(wheel_apply, "userns_reattach_needed", lambda _version: True)
        monkeypatch.setattr(wheel_apply, "restart_reaches", lambda _version: True)
        monkeypatch.setattr(updates, "_restart_gateway", AsyncMock(return_value=True))
        pending = update_stepup.arm("9.9.9", "stable")
        req = _request({"nonce": pending.nonce})
        await updates.api_update_approve(req)
        for task in list(req.app["state"]._background_tasks):
            await task
        state = req.app["state"]
        state.notify.assert_called_once()
        assert "kirocrew service install" in state.notify.call_args.args[2]
        # Sent after the promotion: the update is done, only the re-attach is left.
        assert "is installed" in state.notify.call_args.args[2]
        assert "Run `kirocrew update`" not in state.notify.call_args.args[2]

    async def test_a_restart_that_would_rerun_the_old_version_is_not_started(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A promotion the restart cannot reach (the stable link leads elsewhere)
        must not exec the running version again: that successor would find the
        same update, promote and restart, for ever."""
        from kiro_crew.platform import wheel_apply, wheel_engine

        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(wheel_engine, "apply_wheel_update", lambda **_kw: None)
        monkeypatch.setattr(wheel_apply, "restart_reaches", lambda _version: False)
        restart = AsyncMock(return_value=True)
        monkeypatch.setattr(updates, "_restart_gateway", restart)
        pending = update_stepup.arm("9.9.9", "stable")
        req = _request({"nonce": pending.nonce})
        await updates.api_update_approve(req)
        for task in list(req.app["state"]._background_tasks):
            await task
        restart.assert_not_awaited()
        steps = [c.args for c in req.app["state"].push_update_progress.call_args_list]
        assert any(
            step == "failed" and "Re-run the installer" in detail for step, detail in steps
        ), steps
        _assert_update_lock_free()


class TestCanArmOnTheWire:
    def test_status_update_fields_carries_can_arm(self) -> None:
        """The SPA's button gate rides /api/status; a cached probe must surface.

        This is the field whose absence made the whole in-app half unreachable
        twice in review — pinned at the WIRE seam, not the cache, so a refactor
        of either cannot silently drop it again.
        """
        updates._set_update_info(can_arm=True)
        try:
            assert updates.status_update_fields()["update_can_arm"] is True
        finally:
            updates._set_update_info()
        assert updates.status_update_fields()["update_can_arm"] is False


@pytest.mark.asyncio
class TestArmStatusEndpoint:
    async def test_absent_reads_unarmed(self) -> None:
        update_stepup.clear_pending()
        resp = await updates.api_update_arm_status(_request())
        assert json.loads(resp.body.decode()) == {"armed": False}

    async def test_armed_projection_has_no_nonce(self) -> None:
        pending = update_stepup.arm("9.9.9", "stable")
        try:
            resp = await updates.api_update_arm_status(_request())
            body = resp.body.decode()
            assert json.loads(body)["armed"] is True
            assert pending.nonce not in body
        finally:
            update_stepup.clear_pending()
