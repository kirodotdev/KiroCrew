"""Tests for the in-app wheel update step-up (arm + host-local approve).

The mechanism's whole point is what each half CANNOT do: the arm response
never carries the nonce (a dashboard bearer must not be able to approve), and
an approve without the exact armed nonce is refused. Both halves are pinned
here, plus TTL expiry, single-use consumption, and the endpoint refusals.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.handlers import updates
from kiro_crew.platform import update_stepup


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

    def test_expired_request_reads_as_absent_and_is_removed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending = update_stepup.arm("9.9.9", "stable")
        monkeypatch.setattr(
            time, "time", lambda: pending.created_at + update_stepup.PENDING_TTL_SECS + 1
        )
        assert update_stepup.read_pending() is None
        assert not update_stepup.pending_path().exists()

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


@pytest.mark.asyncio
class TestArmEndpoint:
    async def test_arm_refuses_non_managed_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: False)
        resp = await updates.api_update_arm(_request())
        assert resp.status == 409
        assert json.loads(resp.body.decode())["code"] == "arm_wrong_shape"

    async def test_arm_refuses_without_a_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        updates._set_update_info(update_available=None, latest_version="")
        resp = await updates.api_update_arm(_request())
        assert resp.status == 409
        assert json.loads(resp.body.decode())["code"] == "arm_no_verdict"

    async def test_arm_response_carries_no_nonce(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.platform import wheel_engine

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

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
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

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(updates, "resolve_provider", lambda: object())
        resp = await updates.api_update_arm(_request())
        assert resp.status == 409
        assert json.loads(resp.body.decode())["code"] == "arm_policy_managed"


@pytest.mark.asyncio
class TestApproveEndpoint:
    async def test_non_loopback_peer_refused(self) -> None:
        resp = await updates.api_update_approve(_request({"nonce": "x"}, remote="10.0.0.9"))
        assert resp.status == 403
        assert json.loads(resp.body.decode())["code"] == "approve_not_local"

    async def test_wrong_nonce_refused(self) -> None:
        update_stepup.arm("9.9.9", "stable")
        try:
            resp = await updates.api_update_approve(_request({"nonce": "0" * 64}))
            assert resp.status == 403
            assert json.loads(resp.body.decode())["code"] == "approve_refused"
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

    async def test_no_armed_request_refused(self) -> None:
        update_stepup.clear_pending()
        resp = await updates.api_update_approve(_request({"nonce": "abc"}))
        assert resp.status == 403

    async def test_valid_nonce_launches_the_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        applied: list[dict[str, object]] = []

        def fake_apply(**kwargs: object) -> None:
            applied.append(kwargs)

        restarted: list[object] = []

        async def fake_restart(state: object) -> bool:
            restarted.append(state)
            return True

        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "apply_wheel_update", fake_apply)
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

    async def test_failed_apply_reports_and_does_not_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform.wheel_engine import WheelUpdateError

        def failing_apply(**kwargs: object) -> None:
            raise WheelUpdateError("wheel SHA-256 mismatch")

        restarted: list[object] = []

        async def fake_restart(state: object) -> bool:
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
