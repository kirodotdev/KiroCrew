"""An agent's request that a packaged desktop install be updated.

What these pin is the ABSENCE of authority. On a packaged install the arm
endpoint records a request and fires a notification; there is no nonce, no
token, and no gateway endpoint that turns the request into an install. The only
control that installs is the human's click in Settings › About inside the
desktop app, so an agent that arms a request and reads it back gains nothing.

The refusals matter too: a policy-pinned host, a version below the floor, and
the managed-venv step-up staying exactly as it was.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.handlers import updates
from kiro_crew.platform import app_update_request, update_stepup
from kiro_crew.platform.update_capability import (
    MANAGED_BY_ELECTRON,
    MANAGED_BY_KIROCREW,
    MODE_CONSENT,
    UNAVAILABLE_MANAGED_BY_APP,
    UpdateCapability,
)


def _request(
    body: object = None,
    *,
    remote: str = "127.0.0.1",
    marks: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    req = MagicMock()

    async def _json() -> object:
        if isinstance(body, Exception):
            raise body
        return body

    req.json = _json
    req.remote = remote
    store = dict(marks or {})
    req.get = store.get
    req.__contains__ = lambda self, key: key in store
    req.headers = dict(headers or {})
    req.query = {}
    req.transport.get_extra_info = lambda key, default=None: default
    state = MagicMock()
    state._background_tasks = set()
    req.app = {"state": state}
    return req


def _electron_capability() -> UpdateCapability:
    return UpdateCapability(
        supported=True,
        managed_by=MANAGED_BY_ELECTRON,
        mode=MODE_CONSENT,
        can_download=False,
        can_apply=False,
        requires_restart=True,
        unavailable_reason=UNAVAILABLE_MANAGED_BY_APP,
    )


def _wheel_capability() -> UpdateCapability:
    return UpdateCapability(
        supported=True,
        managed_by=MANAGED_BY_KIROCREW,
        mode=MODE_CONSENT,
        can_download=True,
        can_apply=False,
        requires_restart=True,
    )


@pytest.fixture()
def store(tmp_path):
    """A request store on an isolated path with an injectable clock."""
    clock = {"t": 1_000_000.0}
    s = app_update_request.AppUpdateRequests(
        now=lambda: clock["t"], path=lambda: tmp_path / "req.json"
    )
    s.clock = clock  # type: ignore[attr-defined]
    return s


@pytest.fixture()
def packaged(monkeypatch: pytest.MonkeyPatch, store):
    from kiro_crew.platform import update_capability

    monkeypatch.setattr(update_capability, "derive_capability", _electron_capability)
    monkeypatch.setattr(updates, "resolve_provider", lambda: None)
    monkeypatch.setattr(app_update_request, "get_app_update_requests", lambda: store)
    update_stepup.clear_pending()
    try:
        yield store
    finally:
        store.clear()
        update_stepup.clear_pending()


class TestRequestModule:
    def test_a_request_lapses_after_its_ttl(self, store) -> None:
        store.arm(target_version="0.6.0", requested_by="agent")
        assert store.current() is not None
        store.clock["t"] += app_update_request.REQUEST_TTL_SECS + 1
        assert store.current() is None

    def test_the_ttl_is_sized_for_a_user_who_is_not_there(self) -> None:
        """The persona is on Slack or behind a cron; they arrive hours later."""
        assert app_update_request.REQUEST_TTL_SECS >= 8 * 60 * 60

    def test_a_request_survives_a_new_store_instance(self, tmp_path) -> None:
        """On disk, so a gateway restart does not lose it."""
        path = tmp_path / "req.json"
        first = app_update_request.AppUpdateRequests(path=lambda: path)
        req, _ = first.arm(target_version="0.6.0", requested_by="chat-1")
        second = app_update_request.AppUpdateRequests(path=lambda: path)
        cur = second.current()
        assert cur is not None and cur.request_id == req.request_id

    def test_a_different_ask_replaces_and_is_new(self, store) -> None:
        store.arm(target_version="0.6.0", requested_by="a")
        req, is_new = store.arm(target_version="0.7.0", requested_by="b")
        assert is_new is True
        cur = store.current()
        assert cur is not None and cur.request_id == req.request_id
        assert cur.target_version == "0.7.0" and cur.requested_by == "b"

    def test_the_same_ask_is_not_new_and_keeps_its_id(self, store) -> None:
        """A looping agent turn must not ring the bell per iteration."""
        first, new1 = store.arm(target_version="0.6.0", requested_by="a")
        again, new2 = store.arm(target_version="0.6.0", requested_by="a")
        assert new1 is True and new2 is False
        assert again.request_id == first.request_id

    def test_decline_removes_only_the_request_it_names(self, store) -> None:
        shown, _ = store.arm(target_version="0.6.0", requested_by="a")
        newer, _ = store.arm(target_version="0.7.0", requested_by="b")
        # A stale decline for `shown` must leave `newer` standing.
        assert store.decline(shown.request_id) is False
        cur = store.current()
        assert cur is not None and cur.request_id == newer.request_id
        assert store.decline(newer.request_id) is True
        assert store.current() is None

    def test_decline_of_an_unknown_id_is_a_no_op(self, store) -> None:
        assert store.decline("nope") is False

    def test_the_record_carries_nothing_an_approval_could_present(self, store) -> None:
        """No nonce, no token: reading your own request gains you nothing."""
        req, _ = store.arm(target_version="0.6.0", requested_by="agent")
        public = req.to_public(req.armed_at)
        assert set(public) == {
            "armed",
            "managed_by",
            "request_id",
            "version",
            "requested_by",
            "armed_at",
            "expires_in",
        }
        assert public["managed_by"] == "electron"

    def test_a_malformed_file_reads_as_no_request(self, tmp_path) -> None:
        path = tmp_path / "req.json"
        path.write_text("{not json", encoding="utf-8")
        s = app_update_request.AppUpdateRequests(path=lambda: path)
        assert s.current() is None

    @pytest.mark.parametrize(
        "field, value",
        [
            # json.loads accepts these spellings; a record must not.
            ("expires_at", "Infinity"),
            ("expires_at", "NaN"),
            ("armed_at", "-Infinity"),
            # Not the arm endpoint's stamps: armed in the future, or lasting
            # longer than the TTL allows.
            ("armed_at", "2000000000.0"),
            ("expires_at", "9999999999.0"),
            # Wrong types.
            ("expires_at", '"soon"'),
            ("expires_at", "true"),
            ("request_id", "7"),
            ("request_id", '""'),
            ("target_version", '"' + "v" * 65 + '"'),
            ("requested_by", "[]"),
        ],
    )
    def test_an_agent_written_record_out_of_shape_reads_as_no_request(
        self, tmp_path, field, value
    ) -> None:
        """The file is agent-writable on purpose. It is never TRUSTED.

        A non-finite `expires_at` would never lapse and would overflow
        `expires_in`; a far-future stamp did not come from the arm endpoint.
        """
        clock = {"t": 1_000_000.0}
        path = tmp_path / "req.json"
        fields = {
            "request_id": '"abcdef0123456789"',
            "target_version": '"0.6.0"',
            "requested_by": '"chat-1"',
            "armed_at": "1000000.0",
            "expires_at": "1086400.0",
        }
        fields[field] = value
        path.write_text(
            "{" + ", ".join(f'"{k}": {v}' for k, v in fields.items()) + "}", encoding="utf-8"
        )
        s = app_update_request.AppUpdateRequests(now=lambda: clock["t"], path=lambda: path)
        assert s.current() is None
        # And projecting it never runs, so nothing can overflow.

    def test_a_well_formed_agent_written_record_is_accepted(self, tmp_path) -> None:
        """The validation is a shape check, not a provenance check."""
        clock = {"t": 1_000_000.0}
        path = tmp_path / "req.json"
        path.write_text(
            '{"request_id": "abcdef0123456789", "target_version": "0.6.0", '
            '"requested_by": "chat-1", "armed_at": 1000000.0, "expires_at": 1086400.0}',
            encoding="utf-8",
        )
        s = app_update_request.AppUpdateRequests(now=lambda: clock["t"], path=lambda: path)
        cur = s.current()
        assert cur is not None and cur.expires_in(clock["t"]) == 86400

    def test_fields_are_length_capped(self, store) -> None:
        req, _ = store.arm(target_version="v" * 500, requested_by="w" * 500)
        assert len(req.target_version) == 64 and len(req.requested_by) == 64


@pytest.mark.asyncio
class TestArmPackagedApp:
    async def test_arm_records_a_request_and_notifies(self, packaged) -> None:
        req = _request({"version": "0.6.0"}, headers={"X-Session-Key": "chat-42"})
        resp = await updates.api_update_arm(req)
        assert resp.status == 200, resp.body
        body = json.loads(resp.body.decode())
        assert body["armed"] is True
        assert body["managed_by"] == "electron"
        assert body["version"] == "0.6.0"
        assert body["requested_by"] == "chat-42"
        assert "nonce" not in json.dumps(body)
        cur = packaged.current()
        assert cur is not None and cur.target_version == "0.6.0"
        # The existing notification path is what reaches a chat-only user.
        state = req.app["state"]
        state.notify.assert_called_once()
        kind, title, text = state.notify.call_args.args
        assert kind == "update"
        assert "0.6.0" in text and "Settings" in text
        assert state.notify.call_args.kwargs["url"] == "/settings/about"

    async def test_re_arming_the_same_ask_does_not_notify_again(self, packaged) -> None:
        first = _request({"version": "0.6.0"}, headers={"X-Session-Key": "chat-42"})
        await updates.api_update_arm(first)
        again = _request({"version": "0.6.0"}, headers={"X-Session-Key": "chat-42"})
        resp = await updates.api_update_arm(again)
        assert resp.status == 200
        again.app["state"].notify.assert_not_called()
        # And the id the panel may already be holding is unchanged.
        assert json.loads(resp.body.decode())["request_id"] == packaged.current().request_id

    async def test_a_different_ask_notifies(self, packaged) -> None:
        await updates.api_update_arm(_request({"version": "0.6.0"}))
        req = _request({"version": "0.7.0"})
        await updates.api_update_arm(req)
        req.app["state"].notify.assert_called_once()

    async def test_arm_without_a_version_asks_for_latest(self, packaged) -> None:
        resp = await updates.api_update_arm(_request({}))
        assert resp.status == 200
        assert json.loads(resp.body.decode())["version"] == ""
        assert packaged.current() is not None

    async def test_arm_with_no_body_at_all_still_works(self, packaged) -> None:
        resp = await updates.api_update_arm(_request(ValueError("no json")))
        assert resp.status == 200

    async def test_a_non_string_version_is_rejected(self, packaged) -> None:
        resp = await updates.api_update_arm(_request({"version": 7}))
        assert resp.status == 400
        assert packaged.current() is None

    async def test_policy_managed_host_refuses(self, packaged, monkeypatch) -> None:
        monkeypatch.setattr(updates, "resolve_provider", lambda: MagicMock())
        resp = await updates.api_update_arm(_request({"version": "0.6.0"}))
        assert resp.status == 409
        assert json.loads(resp.body.decode())["code"] == "arm_policy_managed"
        assert packaged.current() is None

    async def test_below_the_minimum_version_floor_refuses(self, packaged, monkeypatch) -> None:
        monkeypatch.setattr(updates, "min_version", lambda: "0.9.0")
        monkeypatch.setattr(updates, "_local_version", "1.0.0")
        resp = await updates.api_update_arm(_request({"version": "0.6.0"}))
        assert resp.status == 409
        assert json.loads(resp.body.decode())["code"] == "arm_below_min_version"
        assert packaged.current() is None

    async def test_the_managed_venv_step_up_is_untouched(self, packaged) -> None:
        """The packaged lane never writes the nonce file."""
        await updates.api_update_arm(_request({"version": "0.6.0"}))
        assert update_stepup.read_pending() is None

    async def test_no_gateway_endpoint_installs_from_the_request(self, packaged) -> None:
        """The whole point: approve does not know the packaged request exists.

        An agent that armed and then POSTs approve with any nonce is refused —
        there is no nonce to match, because none was written.
        """
        await updates.api_update_arm(_request({"version": "0.6.0"}))
        resp = await updates.api_update_approve(
            _request({"nonce": "0" * 64}, marks={"internal_auth": True})
        )
        assert resp.status == 403
        assert json.loads(resp.body.decode())["code"] == "approve_refused"
        # And nothing was queued, dispatched, or applied.
        assert not _request().app["state"]._background_tasks


@pytest.mark.asyncio
class TestStatusAndDecline:
    async def test_status_projects_the_packaged_request(self, packaged) -> None:
        req, _ = packaged.arm(target_version="0.6.0", requested_by="chat-1")
        resp = await updates.api_update_arm_status(_request())
        body = json.loads(resp.body.decode())
        assert body["armed"] is True and body["managed_by"] == "electron"
        assert body["version"] == "0.6.0" and body["requested_by"] == "chat-1"
        assert body["request_id"] == req.request_id

    async def test_status_refuses_a_record_policy_would_have_refused(
        self, packaged, monkeypatch
    ) -> None:
        """A forged file must not surface a card the arm endpoint would refuse."""
        packaged.arm(target_version="0.6.0", requested_by="chat-1")
        monkeypatch.setattr(updates, "resolve_provider", lambda: MagicMock())
        resp = await updates.api_update_arm_status(_request())
        assert json.loads(resp.body.decode()) == {"armed": False}
        # Dropped, so it does not flicker back on the next poll.
        assert packaged.current() is None

    async def test_status_refuses_a_version_below_the_floor(self, packaged, monkeypatch) -> None:
        packaged.arm(target_version="0.6.0", requested_by="chat-1")
        monkeypatch.setattr(updates, "min_version", lambda: "0.9.0")
        monkeypatch.setattr(updates, "_local_version", "1.0.0")
        resp = await updates.api_update_arm_status(_request())
        assert json.loads(resp.body.decode()) == {"armed": False}
        assert packaged.current() is None

    async def test_status_reads_unarmed_when_nothing_is_live(self, packaged) -> None:
        resp = await updates.api_update_arm_status(_request())
        assert json.loads(resp.body.decode()) == {"armed": False}

    async def test_status_labels_the_managed_venv_lane(self, packaged) -> None:
        update_stepup.arm("9.9.9", "stable")
        resp = await updates.api_update_arm_status(_request())
        body = json.loads(resp.body.decode())
        assert body["armed"] is True and body["managed_by"] == "kirocrew"
        assert "approve_command" in body and "nonce" not in json.dumps(body)

    async def test_decline_drops_the_request_it_names(self, packaged) -> None:
        req, _ = packaged.arm(target_version="0.6.0", requested_by="chat-1")
        r = _request(remote="10.1.2.3")
        r.query = {"request_id": req.request_id}
        resp = await updates.api_update_disarm(r)
        assert json.loads(resp.body.decode()) == {"ok": True, "armed": False, "dismissed": True}
        assert packaged.current() is None

    async def test_a_stale_decline_leaves_a_newer_request(self, packaged) -> None:
        """Panel shows A; agent arms B; Decline on A must not erase B."""
        shown, _ = packaged.arm(target_version="0.6.0", requested_by="chat-1")
        newer, _ = packaged.arm(target_version="0.7.0", requested_by="chat-2")
        r = _request()
        r.query = {"request_id": shown.request_id}
        resp = await updates.api_update_disarm(r)
        assert json.loads(resp.body.decode()) == {"ok": True, "armed": True, "dismissed": False}
        cur = packaged.current()
        assert cur is not None and cur.request_id == newer.request_id

    async def test_decline_requires_the_shown_id(self, packaged) -> None:
        packaged.arm(target_version="0.6.0", requested_by="chat-1")
        resp = await updates.api_update_disarm(_request())
        assert resp.status == 400
        assert packaged.current() is not None

    async def test_decline_of_a_lapsed_request_is_not_an_error(self, packaged) -> None:
        r = _request()
        r.query = {"request_id": "gone"}
        resp = await updates.api_update_disarm(r)
        assert json.loads(resp.body.decode()) == {"ok": True, "armed": False, "dismissed": False}

    async def test_decline_leaves_the_managed_venv_step_up_alone(self, packaged) -> None:
        update_stepup.arm("9.9.9", "stable")
        r = _request()
        r.query = {"request_id": "anything"}
        await updates.api_update_disarm(r)
        assert update_stepup.read_pending() is not None


class TestAgentReachability:
    def test_arm_is_mixed_so_an_agent_can_present_the_secret(self) -> None:
        """The agent entry point must be reachable by an agent.

        A route in neither bucket falls through to cookie auth and refuses an
        agent's X-Internal-Secret. MIXED, not strict: the browser polls it too.
        """
        from kiro_crew.dashboard.server import (
            _MIXED_INTERNAL_API_PATHS,
            _STRICT_INTERNAL_API_PATHS,
        )

        assert "/api/update/arm" in _MIXED_INTERNAL_API_PATHS
        assert "/api/update/arm" not in _STRICT_INTERNAL_API_PATHS
        # The host-only approval stays strict and is not swallowed by a prefix.
        assert "/api/update/approve" in _STRICT_INTERNAL_API_PATHS
        assert "/api/update/approve" not in _MIXED_INTERNAL_API_PATHS


class TestWheelLaneUnchanged:
    """The managed-venv path is exactly what main ships."""

    @pytest.mark.asyncio
    async def test_wheel_arm_still_refuses_non_managed_shapes(self, monkeypatch) -> None:
        from kiro_crew.platform import update_capability, wheel_engine

        monkeypatch.setattr(update_capability, "derive_capability", _wheel_capability)
        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: False)
        resp = await updates.api_update_arm(_request())
        assert resp.status == 409
        assert json.loads(resp.body.decode())["code"] == "arm_wrong_shape"


class TestCliApprove:
    def test_packaged_install_is_told_to_approve_in_the_app(self, monkeypatch, capsys) -> None:
        from kiro_crew import cli_server
        from kiro_crew.platform import update_capability

        monkeypatch.setattr(update_capability, "derive_capability", _electron_capability)
        cli_server._update_approve()
        out = capsys.readouterr().out
        assert "Settings" in out and "About" in out
