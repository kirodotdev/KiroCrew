"""Tests for the one-click in-app update (``POST /api/update/arm`` installing at once).

A newer release of the running build's own channel, from the official update CDN,
armed by the dashboard owner, installs with that click: the arm runs the host
approval's checks, lock, audit and apply, minus the nonce. Every other arm keeps
writing the nonce for ``kirocrew update approve``. Both branches are pinned here,
each case that keeps the host approval, each refusal of the one-click apply, and
its audit.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from real_dashboard_state import _settle_real_dashboard_states  # noqa: F401

from kiro_crew.dashboard.handlers import updates, updates_step_up
from kiro_crew.platform import update_stepup


def _request(*, marks: dict[str, object] | None = None) -> MagicMock:
    req = MagicMock()

    async def _json() -> object:
        return {"offered": "9.9.9"}

    req.json = _json
    req.remote = "127.0.0.1"
    store = dict(marks or {})
    req.get = store.get
    req.__contains__ = lambda self, key: key in store
    req.transport.get_extra_info = lambda key, default=None: default
    state = MagicMock()
    state._background_tasks = set()
    state._restart_commit_hold = None
    state._gateway_restart_in_progress = False
    req.app = {"state": state}
    return req


def _drain(req: MagicMock) -> list[object]:
    return list(req.app["state"]._background_tasks)


@pytest.fixture
def audited(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []

    async def fake_audit(_request: object, **kwargs: object) -> None:
        events.append(kwargs)

    monkeypatch.setattr(updates, "_audit_update_event", fake_audit)
    return events


@pytest.fixture
def applied(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """An eligible host: the owner's session, stable build 0.7.0, 9.9.9 offered on stable.

    The apply's engine is stubbed at its seam, so nothing is downloaded or built.
    """
    from kiro_crew.dashboard.handlers import source_providers
    from kiro_crew.platform import update_governance, wheel_apply, wheel_engine

    installs: list[dict[str, object]] = []
    monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
    monkeypatch.setattr(wheel_engine, "apply_wheel_update", lambda **kw: installs.append(kw))
    monkeypatch.setattr(wheel_apply, "restart_reaches", lambda _version: True)
    monkeypatch.setattr(wheel_apply, "userns_reattach_needed", lambda _version: False)
    monkeypatch.setattr(wheel_apply, "relaunch_before_apply", lambda: False)
    monkeypatch.setattr(wheel_apply, "_IN_FLIGHT", set())
    monkeypatch.setattr(source_providers, "is_host_owner_dashboard_request", lambda _request: True)
    monkeypatch.setattr(update_governance, "unattended_apply_allowed", lambda: True)
    monkeypatch.setattr(updates, "resolve_provider", lambda: None)
    monkeypatch.setattr(updates, "min_version", lambda: "")
    monkeypatch.setattr(updates, "_running_version", "0.7.0")
    monkeypatch.delenv("KIROCREW_CDN_BASE", raising=False)

    async def _check() -> None:
        updates._set_update_info(update_available=True, latest_version="9.9.9", channel="stable")

    monkeypatch.setattr(updates, "_do_update_check", _check)
    yield installs
    update_stepup.clear_pending()
    updates._set_update_info()


@pytest.mark.asyncio
class TestOneClick:
    async def test_the_owners_click_installs_a_same_channel_release_without_a_nonce(
        self, applied: list[dict[str, object]], audited: list[dict[str, object]]
    ) -> None:
        from kiro_crew import update_restart_ready

        req = _request()
        resp = await updates.api_update_arm(req)
        assert resp.status == 200
        body = json.loads(resp.body.decode())
        assert (body["status"], body["version"], body["version_display"]) == (
            "applying",
            "9.9.9",
            "9.9.9",
        )
        assert "approve_command" not in body
        assert update_stepup.read_pending() is None, "a one-click install writes no nonce"
        for task in _drain(req):
            await task
        assert [kw["expected_version"] for kw in applied] == ["9.9.9"]
        note = update_restart_ready.current()
        assert note is not None
        assert (note.reason, note.version) == (update_restart_ready.REASON_STAGED, "9.9.9")
        frames = [c.args for c in req.app["state"].push_update_progress.call_args_list]
        assert frames[-1] == ("done", "v9.9.9 installed. Restart to use it.")
        assert [(a["operation"], a["outcome"]) for a in audited] == [
            ("update.one_click", "granted"),
            ("update.one_click", "success"),
        ]
        assert audited[0]["required"] is True, "a code install is audit-or-deny"

    @pytest.mark.parametrize(
        ("case", "hold"),
        [
            ("agent", updates_step_up.HOLD_CALLER),
            ("non_owner", updates_step_up.HOLD_CALLER),
            ("teammate", updates_step_up.HOLD_CALLER),
            ("move_pending", updates_step_up.HOLD_CHANNEL_MOVE),
            ("switched", updates_step_up.HOLD_CHANNEL_MOVE),
            ("nightly_target", updates_step_up.HOLD_CHANNEL_MOVE),
            ("stable_build_to_insider", updates_step_up.HOLD_CHANNEL_MOVE),
            ("not_newer", updates_step_up.HOLD_DOWNGRADE),
            ("cdn_override", updates_step_up.HOLD_UPDATE_SOURCE),
            ("unattended_off", updates_step_up.HOLD_POLICY),
        ],
    )
    async def test_every_other_case_keeps_the_host_approval(
        self,
        monkeypatch: pytest.MonkeyPatch,
        applied: list[dict[str, object]],
        audited: list[dict[str, object]],
        case: str,
        hold: str,
    ) -> None:
        from kiro_crew.config.paths import data_home
        from kiro_crew.dashboard.handlers import source_providers
        from kiro_crew.platform import update_governance

        marks: dict[str, object] = {}
        version, channel, move = "9.9.9", "stable", False
        if case == "agent":
            marks = {"internal_auth": True}
        elif case == "non_owner":
            monkeypatch.setattr(
                source_providers, "is_host_owner_dashboard_request", lambda _r: False
            )
        elif case == "teammate":
            # A Shared Crew teammate passes the dashboard's owner gate, not the
            # machine owner's: it can only ask.
            monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda _r: True)
            monkeypatch.setattr(
                source_providers, "is_host_owner_dashboard_request", lambda _r: False
            )
        elif case == "move_pending":
            move = True
        elif case == "switched":
            # A switch from insider to stable, made in an earlier gateway process.
            from kiro_crew.platform import update_channel_move
            from kiro_crew.platform.update_layout import set_release_channel

            data_home().mkdir(parents=True, exist_ok=True)
            set_release_channel("insider")
            update_channel_move.switch_channel("stable", set_release_channel)
            monkeypatch.setattr(updates, "_running_version", "0.7.0rc3")
        elif case == "nightly_target":
            channel = "nightly"
        elif case == "stable_build_to_insider":
            # A bare release stamp came from stable: an insider release is a move,
            # even with no record (a switch made before the record existed).
            version, channel = "9.9.9rc1", "insider"
        elif case == "not_newer":
            version = "0.7.0"
        elif case == "cdn_override":
            monkeypatch.setenv("KIROCREW_CDN_BASE", "https://mirror.example.test")
        elif case == "unattended_off":
            monkeypatch.setattr(update_governance, "unattended_apply_allowed", lambda: False)
        req = _request(marks=marks)
        assert updates_step_up._one_click_hold(req, version, channel, move_pending=move) == hold

        if case in ("move_pending", "not_newer"):
            return  # the arm's verdict guards own these shapes; the hold alone is pinned
        if channel != "stable":

            async def _check() -> None:
                updates._set_update_info(
                    update_available=True, latest_version=version, channel=channel
                )

            monkeypatch.setattr(updates, "_do_update_check", _check)
        resp = await updates.api_update_arm(req)
        assert resp.status == 200
        body = json.loads(resp.body.decode())
        assert body["armed"] is True
        assert body["approve_command"] == "kirocrew update approve"
        assert update_stepup.read_pending() is not None
        assert _drain(req) == [] and applied == []
        assert audited == [], "an arm that installs nothing writes no install audit"

    async def test_a_source_pin_refuses_the_click_and_arms_nothing(
        self, monkeypatch: pytest.MonkeyPatch, applied: list[dict[str, object]]
    ) -> None:
        from kiro_crew.platform import update_governance

        monkeypatch.setattr(
            update_governance, "update_blocked_reason", lambda _base: "source not allowed"
        )
        req = _request()
        resp = await updates.api_update_arm(req)
        body = json.loads(resp.body.decode())
        assert resp.status == 403
        assert (body["code"], body["governance"]) == ("arm_blocked_by_policy", True)
        assert update_stepup.read_pending() is None
        assert _drain(req) == [] and applied == []

    @pytest.mark.parametrize(
        ("reason", "status", "code"),
        [
            ("memory_preparing", 409, "arm_memory_preparing"),
            ("busy", 409, "arm_busy"),
            ("restarting", 409, "arm_restarting"),
            ("channel_pin", 403, "arm_blocked_by_policy"),
            ("audit", 503, "arm_audit_failed"),
        ],
    )
    async def test_the_click_runs_every_check_the_approval_runs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        applied: list[dict[str, object]],
        reason: str,
        status: int,
        code: str,
    ) -> None:
        from kiro_crew.platform import wheel_apply, wheel_engine

        req = _request()
        held: list[int] = []
        if reason == "memory_preparing":

            def _preparing() -> None:
                raise wheel_engine.WheelUpdateNotReady("memory is still being prepared")

            monkeypatch.setattr(wheel_apply, "check_memory_ready", _preparing)
        elif reason == "busy":
            held.append(wheel_engine.hold_update_lock())
        elif reason == "restarting":
            req.app["state"]._gateway_restart_in_progress = True
        elif reason == "channel_pin":
            monkeypatch.setattr(
                "kiro_crew.platform.update_layout.policy_release_channel", lambda: "insider"
            )
        elif reason == "audit":

            async def failing_audit(_request: object, **kwargs: object) -> None:
                if kwargs["outcome"] == "granted":
                    raise OSError("audit unavailable")

            monkeypatch.setattr(updates, "_audit_update_event", failing_audit)
        try:
            resp = await updates.api_update_arm(req)
        finally:
            for fd in held:
                wheel_engine.release_update_lock(fd)
        assert resp.status == status
        assert json.loads(resp.body.decode())["code"] == code
        assert update_stepup.read_pending() is None, "a refused click does not fall back to a nonce"
        assert _drain(req) == [] and applied == []
        # The lock is free again whichever check refused.
        wheel_engine.release_update_lock(wheel_engine.hold_update_lock())


class TestBuildOnChannel:
    @pytest.mark.parametrize(
        ("running", "channel", "on"),
        [
            ("0.7.0", "stable", True),
            # A promoted stable release keeps its insider stamp.
            ("0.7.0rc3", "stable", True),
            ("0.7.0rc3", "insider", True),
            ("0.7.0.dev20261126", "nightly", True),
            ("0.7.0.dev20261126", "insider", False),
            ("0.7.0rc3", "nightly", False),
            # A bare release stamp is a stable build.
            ("0.7.0", "insider", False),
            ("0.7.0", "nightly", False),
        ],
    )
    def test_the_version_stamp_tells_nightly_bytes_from_the_rest(
        self, monkeypatch: pytest.MonkeyPatch, running: str, channel: str, on: bool
    ) -> None:
        monkeypatch.setattr("kiro_crew.edition.is_lts", lambda: False)
        monkeypatch.setattr(updates_step_up, "_running_version", running)
        assert updates_step_up._build_on_channel(channel) is on

    @pytest.mark.parametrize(("channel", "on"), [("lts", True), ("stable", False)])
    def test_an_lts_build_is_only_on_lts(
        self, monkeypatch: pytest.MonkeyPatch, channel: str, on: bool
    ) -> None:
        # An LTS release carries a bare stamp, which reads as stable off the stamp.
        monkeypatch.setattr("kiro_crew.edition.is_lts", lambda: True)
        monkeypatch.setattr(updates_step_up, "_running_version", "0.7.0")
        assert updates_step_up._build_on_channel(channel) is on


class TestChannelMoveRecord:
    def test_the_first_switch_is_kept_and_switching_back_forgets_it(self) -> None:
        from kiro_crew.config.paths import data_home
        from kiro_crew.platform import update_channel_move
        from kiro_crew.platform.update_layout import set_release_channel

        data_home().mkdir(parents=True, exist_ok=True)
        set_release_channel("insider")
        assert update_channel_move.moved_from() == ""
        update_channel_move.switch_channel("stable", set_release_channel)
        assert update_channel_move.moved_from() == "insider"
        update_channel_move.switch_channel("nightly", set_release_channel)
        assert update_channel_move.moved_from() == "insider", "the origin is not overwritten"
        update_channel_move.switch_channel("insider", set_release_channel)
        assert update_channel_move.moved_from() == ""

    def test_a_failed_switch_back_keeps_the_record(self) -> None:
        from kiro_crew.config.paths import data_home
        from kiro_crew.platform import update_channel_move
        from kiro_crew.platform.update_layout import release_channel, set_release_channel

        data_home().mkdir(parents=True, exist_ok=True)
        set_release_channel("stable")
        update_channel_move.switch_channel("insider", set_release_channel)

        def _full_disk(_channel: str) -> str:
            raise OSError(28, "no space left on device")

        with pytest.raises(OSError):
            update_channel_move.switch_channel("stable", _full_disk)
        assert release_channel() == "insider"
        assert update_channel_move.moved_from() == "stable", "the hold survives the failure"

    def test_a_switch_back_during_a_build_names_the_installed_channel(self) -> None:
        # stable -> insider, an insider install is approved, then the owner switches
        # back to stable while it builds: the install came from insider, so the
        # record must name insider once it is promoted.
        from kiro_crew.config.paths import data_home
        from kiro_crew.platform import update_channel_move
        from kiro_crew.platform.update_layout import set_release_channel

        data_home().mkdir(parents=True, exist_ok=True)
        set_release_channel("stable")
        update_channel_move.switch_channel("insider", set_release_channel)
        update_channel_move.switch_channel("stable", set_release_channel)
        assert update_channel_move.moved_from() == ""
        update_channel_move.settle("insider")
        assert update_channel_move.moved_from() == "insider"

    def test_a_settlement_only_clears_the_record_for_the_followed_channel(self) -> None:
        from kiro_crew.config.paths import data_home
        from kiro_crew.platform import update_channel_move
        from kiro_crew.platform.update_layout import set_release_channel

        data_home().mkdir(parents=True, exist_ok=True)
        set_release_channel("stable")
        update_channel_move.switch_channel("insider", set_release_channel)
        update_channel_move.settle("stable")
        assert update_channel_move.moved_from() == "stable"
        update_channel_move.settle("insider")
        assert update_channel_move.moved_from() == ""

    def test_an_unreadable_record_reads_as_a_move(self) -> None:
        from kiro_crew.config.paths import data_home
        from kiro_crew.platform import update_channel_move

        (data_home() / "channel-moved-from").mkdir(parents=True)
        assert update_channel_move.moved_from() == "unknown"


@pytest.mark.asyncio
class TestChannelSwitchRecordsTheMove:
    @pytest.fixture
    def wheel_layout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.dashboard.handlers import updates_channel

        layout = MagicMock(is_git=False, is_externally_managed=False)
        monkeypatch.setattr(updates_channel, "detect_install_layout", lambda: layout)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(updates, "_invalidate_update_check", lambda _channel: None)
        monkeypatch.setattr(updates, "require_owner_dashboard_request", AsyncNone())

        async def _check() -> None:
            return None

        monkeypatch.setattr(updates, "_do_update_check", _check)

    async def _switch(self, channel: str) -> int:
        req = _request()

        async def _json() -> object:
            return {"channel": channel}

        req.json = _json
        resp = await updates.api_update_channel(req)
        return resp.status

    async def test_a_switch_records_where_the_install_came_from(self, wheel_layout: None) -> None:
        from kiro_crew.platform import update_channel_move
        from kiro_crew.platform.update_layout import release_channel

        assert await self._switch("insider") == 200
        assert release_channel() == "insider"
        assert update_channel_move.moved_from() == "stable"
        assert await self._switch("stable") == 200
        assert update_channel_move.moved_from() == ""

    async def test_a_record_that_cannot_be_written_refuses_the_switch(
        self, monkeypatch: pytest.MonkeyPatch, wheel_layout: None
    ) -> None:
        from kiro_crew.platform import update_channel_move
        from kiro_crew.platform.update_layout import release_channel

        assert update_channel_move.moved_from() == ""

        # The record's own write fails; the channel file writer is untouched.
        def _refuse(*_args: object, **_kwargs: object) -> None:
            raise OSError(28, "no space left on device")

        monkeypatch.setattr("kiro_crew.atomic_write.atomic_write", _refuse)
        assert await self._switch("insider") == 500
        assert release_channel() == "stable", "the channel file is not written"


class AsyncNone:
    """An awaitable stand-in for the owner gate: the caller is the owner."""

    async def __call__(self, *_args: object, **_kwargs: object) -> None:
        return None


@pytest.mark.asyncio
class TestApprovedInstallSettlesTheMove:
    async def test_an_approved_install_from_the_followed_channel_clears_the_record(
        self, applied: list[dict[str, object]]
    ) -> None:
        from kiro_crew.config.paths import data_home
        from kiro_crew.platform import update_channel_move
        from kiro_crew.platform.update_layout import set_release_channel

        data_home().mkdir(parents=True, exist_ok=True)
        set_release_channel("stable")
        update_channel_move.switch_channel("insider", set_release_channel)
        pending = update_stepup.arm("9.9.9rc1", "insider")
        req = _request()

        async def _json() -> object:
            return {"nonce": pending.nonce}

        req.json = _json
        resp = await updates.api_update_approve(req)
        assert resp.status == 200
        for task in _drain(req):
            await task
        assert applied, "the approved install ran"
        assert update_channel_move.moved_from() == ""

    async def test_a_one_click_audit_names_the_signed_in_person(
        self, applied: list[dict[str, object]], audited: list[dict[str, object]]
    ) -> None:
        req = _request(marks={"user": "local-app"})
        resp = await updates.api_update_arm(req)
        assert resp.status == 200
        for task in _drain(req):
            await task
        assert {a["caller"] for a in audited} == {"local-app"}
