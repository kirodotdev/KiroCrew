"""A cron job pinned to a destination on another messaging channel.

The pin is ``channel_type`` + ``target_id``, the explicit-target pair ``send_message``
takes. Pinned here: the store keeps it and writes nothing for an unpinned job, a job
holds one pinned destination, the grant fingerprint binds it, the dashboard vets it
when it is saved, a run's result and failure alerts reach the pin instead of the origin
conversation and the audit trail names it, and the Schedule form's topic read lists the
private topics the bot already talks in.

Every collaborator is a double or lives under ``tmp_path``.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from body_stream_helpers import attach_body
from test_cron_channel_delivery import (
    DISCORD_CONVERSATION,
    _cron_callback,
    _cron_service_double,
    _discord_orch,
    _governance,
    _job,
    _run_result_leg,
    _slack_double,
)

from kiro_crew.cron import CronService
from kiro_crew.cron_script_grants import delivery_fingerprint
from kiro_crew.cron_service.store import job_record
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.platform import governance_profiles
from kiro_crew.slack import gateway as gw

TOPIC = "topic:42:7"


def _telegram(resolved: tuple[str, str | None] | None = ("42", "7")) -> MagicMock:
    tr = MagicMock()
    tr.capabilities = TransportCapabilities(supports_proactive_send=True, max_message_chars=4000)
    tr.send_message = AsyncMock(return_value="mid-1")
    tr.resolve_configured_target = AsyncMock(return_value=resolved)
    return tr


def _permit() -> Any:
    return patch.object(
        governance_profiles,
        "vet_and_audit",
        MagicMock(return_value=SimpleNamespace(permitted=True)),
    )


class TestStore:
    def test_the_pin_round_trips_and_an_unpinned_job_writes_no_key(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("plain", "m", every_secs=600)
        assert "channel_type" not in job_record(job)
        svc.update_job(job.id, channel_type="telegram", target_id=TOPIC)
        reloaded = CronService(base_dir=tmp_path).get_job(job.id)
        assert reloaded is not None
        assert (reloaded.channel_type, reloaded.target_id) == ("telegram", TOPIC)

    def test_a_job_holds_one_pinned_destination(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        # Made in a Slack thread: its thread_ts stays, and must not block the move.
        job = svc.add_job(
            "slack", "m", every_secs=600, channel="C0AP77JJSN6", thread_ts="1700000000.000100"
        )
        with pytest.raises(ValueError, match="one destination"):
            svc.update_job(job.id, channel_type="telegram", target_id=TOPIC)
        with pytest.raises(ValueError, match="set together"):
            svc.update_job(job.id, channel="", target_id=TOPIC)
        moved = svc.update_job(job.id, channel="", channel_type="telegram", target_id=TOPIC)
        assert moved is not None and moved.id == job.id and moved.channel is None


def test_the_fingerprint_binds_the_pin_and_keeps_unpinned_jobs_byte_identical():
    unpinned = delivery_fingerprint("dashboard:a", False, "C1", "1.2")
    assert (
        unpinned == '{"channel":"C1","session_key":"dashboard:a","silent":false,"thread_ts":"1.2"}'
    )
    assert delivery_fingerprint("dashboard:a", False, "", "", "telegram", TOPIC) != (
        delivery_fingerprint("dashboard:a", False, "", "", "telegram", "user:42")
    )


class TestDashboardWrite:
    @staticmethod
    def _request(state: Any, body: dict[str, Any]) -> MagicMock:
        request = MagicMock()
        request.app = {"state": state}
        attach_body(request, body)
        return request

    @pytest.mark.asyncio
    async def test_a_destination_the_channel_refuses_is_not_saved(self):
        from kiro_crew.dashboard.handlers import api_crons_create

        state = MagicMock()
        state.channel_transports = {"telegram": _telegram(resolved=None)}
        state.crons.add_job_async = AsyncMock()
        body = {"name": "n", "message": "m", "every": 300}
        with _permit():
            resp = await api_crons_create(
                self._request(state, {**body, "channel_type": "telegram", "target_id": TOPIC})
            )
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "target_not_allowed"
        state.crons.add_job_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_reachable_destination_is_handed_to_the_update(self):
        from kiro_crew.dashboard.handlers import api_cron_update

        state = MagicMock()
        state.channel_transports = {"telegram": _telegram()}
        state.crons.update_job_async = AsyncMock(return_value=MagicMock(id="j1"))
        request = self._request(state, {"channel_type": "telegram", "target_id": TOPIC})
        request.match_info = {"job_id": "j1"}
        with _permit():
            resp = await api_cron_update(request)
        assert resp.status == 200
        kwargs = state.crons.update_job_async.await_args.kwargs
        assert (kwargs["channel_type"], kwargs["target_id"]) == ("telegram", TOPIC)


class TestDelivery:
    @pytest.mark.asyncio
    async def test_the_result_reaches_the_pin_not_the_origin_conversation(self):
        orch, discord = _discord_orch()
        telegram = _telegram()
        orch.dashboard_state.channel_transports = {"telegram": telegram}
        job = _job(channel_type="telegram", target_id=TOPIC)
        await _run_result_leg(orch, job, result="3 new issues")
        discord.send_message.assert_not_awaited()
        conversation, text = telegram.send_message.await_args.args
        assert (conversation, telegram.send_message.await_args.kwargs["thread_id"]) == ("42", "7")
        assert "3 new issues" in text
        assert DISCORD_CONVERSATION not in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("job_kw", "failing", "tool"),
        [
            (
                {"script": "raise SystemExit(1)", "message": ""},
                ("run_script_sandboxed", RuntimeError("boom")),
                "cron_run_failure_alert",
            ),
            ({}, ("stream_and_collect", ValueError("crashed")), "cron_failure_alert"),
        ],
        ids=["run-failure", "crash"],
    )
    async def test_a_failure_alert_reaches_the_pin_and_its_audit_names_it(
        self, job_kw, failing, tool
    ):
        orch, discord = _discord_orch(slack=_slack_double())
        telegram = _telegram()
        orch.dashboard_state.channel_transports = {"telegram": telegram}
        job = _job(**job_kw, channel_type="telegram", target_id=TOPIC)
        audit = MagicMock()
        target, error = failing
        async with _cron_callback(orch, svc=_cron_service_double(job)) as on_job:
            with _governance(permitted=True), patch.object(gw, "sel", lambda: audit):
                with patch.object(gw, target, MagicMock(side_effect=error)):
                    # The crash leg re-raises its exception after alerting.
                    with contextlib.suppress(ValueError):
                        await on_job(job)
        discord.send_message.assert_not_awaited()
        assert telegram.send_message.await_args.kwargs["thread_id"] == "7"
        surfaces = [
            call.kwargs["downstream_service"]
            for call in audit.log_tool_invocation.call_args_list
            if call.kwargs.get("tool_name") == tool
        ]
        assert surfaces == ["telegram"]

    @pytest.mark.asyncio
    async def test_a_refused_pin_reports_no_delivery(self):
        orch, _discord = _discord_orch()
        telegram = _telegram(resolved=None)
        orch.dashboard_state.channel_transports = {"telegram": telegram}
        orch.cron_svc = MagicMock(
            get_job=MagicMock(return_value=_job(channel_type="telegram", target_id=TOPIC))
        )
        with _permit():
            assert await orch._deliver_cron_to_channel("", "done", actor_key="cron:j1") is False
        telegram.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_app_job_asks_its_apps_messaging_grant(self):
        orch, _discord = _discord_orch()
        telegram = _telegram()
        orch.dashboard_state.channel_transports = {"telegram": telegram}
        orch.cron_svc = MagicMock(
            get_job=MagicMock(
                return_value=_job(
                    channel_type="telegram", target_id=TOPIC, created_by="app:digest-app"
                )
            )
        )

        def _vet(scope: str, *_args: Any, app: str = "", **_kwargs: Any) -> Any:
            return SimpleNamespace(permitted=not (scope == "capabilities.messaging" and app))

        with patch.object(governance_profiles, "vet_and_audit", MagicMock(side_effect=_vet)):
            assert await orch._deliver_cron_to_channel("", "done", actor_key="cron:j1") is False
        telegram.send_message.assert_not_awaited()


class TestTopicRead:
    @pytest.mark.asyncio
    async def test_lists_the_private_topics_the_bot_talks_in(self, tmp_path, monkeypatch):
        from chat_mirror_helpers import _prep

        from kiro_crew.dashboard.chat_mirror import api_channel_topics

        state = _prep(tmp_path, monkeypatch)
        state.get_or_create_slot("s2")
        state.get_or_create_slot("s3")
        state._slots["s1"].title = "Cron"
        links = {
            "dashboard:s1": ChannelLink("telegram", channel_id="42", thread_id="7"),
            "dashboard:s2": ChannelLink("telegram", channel_id="-100", thread_id="9"),
            "dashboard:s3": ChannelLink("telegram", channel_id="42"),
        }
        state.sessions.get_mirror_link = MagicMock(side_effect=links.get)
        telegram = _telegram()
        telegram.resolve_configured_target = AsyncMock(
            side_effect=lambda target: ("42", "7") if target == TOPIC else None
        )
        state.channel_transports["telegram"] = telegram
        app = web.Application()
        app["state"] = state
        app.router.add_get("/api/chat/channel-topics", api_channel_topics)
        with patch(
            "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request",
            AsyncMock(return_value=None),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/chat/channel-topics")
                assert resp.status == 200
                assert await resp.json() == [
                    {"channel_type": "telegram", "target_id": TOPIC, "label": "Cron"}
                ]
