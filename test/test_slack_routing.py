"""Tests for the brokered Slack channel-routing write.

``PUT /api/slack/channels/{channel_id}/routing`` is the one config write core
performs on behalf of an edition-supplied recipes app, so the things worth
pinning are the ones an app depends on: that every malformed request is refused
with a stable machine-readable ``code`` rather than prose a client would have to
parse, that a successful write lands in ``config.json``, that teardown removes
the entry, that an unreadable config is refused rather than overwritten, and
that a gateway not running Slack still reports the durable half as done.

The endpoint does not implement the write. It delegates to
``slack.handler._persist_channel_config`` via ``run_config_write`` (both config
locks, fail-closed on a corrupt document), so these tests drive the real writer
with ``config_path`` redirected at ``tmp_path`` and never touch a real crew home.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import slack_routing
from kiro_crew.dashboard.handlers.slack_routing import (
    _ACTIVATION_VALUES,
    _caller,
    _refresh_routing,
    api_slack_channel_routing_put,
)

_CHANNEL = "C0123ABC"


def _req(body, channel_id: str = _CHANNEL, app: str | None = None, bad_json: bool = False):
    """A mocked PUT carrying *body*, optionally as a verified app identity."""
    req = make_mocked_request(
        "PUT",
        f"/api/slack/channels/{channel_id}/routing",
        match_info={"channel_id": channel_id},
    )
    if app is not None:
        req["app"] = app
    if bad_json:
        req.json = AsyncMock(side_effect=ValueError("not json"))
    else:
        req.json = AsyncMock(return_value=body)
    return req


async def _call(req, tmp_path, *, orch: object | None = None):
    """Drive the handler against the real writer, rooted at *tmp_path*.

    ``config_path`` is patched where ``_persist_channel_config`` reads it, so the
    genuine locked read-modify-write runs, just against a throwaway file.

    ``orch`` is what ``get_orch_cfg`` returns: ``None`` models a gateway with no
    Slack orchestrator bound, which must still succeed on the durable write. The
    Slack functions are patched on the real module rather than by swapping
    ``sys.modules``, because ``_refresh_routing`` does ``from kiro_crew.slack
    import handler`` — an ATTRIBUTE lookup on the parent package that a
    ``sys.modules`` entry never satisfies. It would also pass vacuously: the real
    ``get_orch_cfg`` already returns ``None`` in a test process.
    """
    cfg = tmp_path / "config.json"
    with (
        patch.object(slack_routing, "_sel", return_value=MagicMock()),
        patch("kiro_crew.slack.handler.config_path", return_value=cfg),
        patch("kiro_crew.slack.handler.get_orch_cfg", return_value=orch),
        patch("kiro_crew.slack.handler._reload_orch_cfg") as reload_mock,
    ):
        resp = await api_slack_channel_routing_put(req)
    return resp, reload_mock


def _payload(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


def _channels(tmp_path) -> dict:
    doc = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    return doc.get("slack", {}).get("channels", {})


class TestRejections:
    """Every refusal carries a stable ``code``; prose is advisory only."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "channel_id",
        ["", "nope", "c0123abc", "X0123ABC", "C1", "C" + "A" * 40, "../etc/passwd"],
    )
    async def test_invalid_channel_id(self, channel_id, tmp_path):
        resp, _ = await _call(_req({"agent": "a"}, channel_id=channel_id), tmp_path)
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_channel_id"

    @pytest.mark.asyncio
    async def test_unparseable_body(self, tmp_path):
        resp, _ = await _call(_req(None, bad_json=True), tmp_path)
        assert _payload(resp)["code"] == "invalid_json"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [[], "str", 3, None])
    async def test_body_must_be_an_object(self, body, tmp_path):
        resp, _ = await _call(_req(body), tmp_path)
        assert _payload(resp)["code"] == "body_not_an_object"

    @pytest.mark.asyncio
    async def test_remove_must_be_a_boolean(self, tmp_path):
        resp, _ = await _call(_req({"remove": "yes"}), tmp_path)
        assert _payload(resp)["code"] == "invalid_remove"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("extra", [{"agent": "a"}, {"activation": "always"}])
    async def test_remove_cannot_carry_updates(self, extra, tmp_path):
        resp, _ = await _call(_req({"remove": True, **extra}), tmp_path)
        assert _payload(resp)["code"] == "remove_with_updates"

    @pytest.mark.asyncio
    async def test_agent_must_be_a_string(self, tmp_path):
        resp, _ = await _call(_req({"agent": 7}), tmp_path)
        assert _payload(resp)["code"] == "invalid_agent"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("activation", ["sometimes", "", 5, []])
    async def test_activation_must_be_a_known_mode(self, activation, tmp_path):
        resp, _ = await _call(_req({"activation": activation}), tmp_path)
        body = _payload(resp)
        assert body["code"] == "invalid_activation"
        # The prose lists the accepted modes, so a human sees the options.
        assert "must be one of" in body["error"]

    @pytest.mark.asyncio
    async def test_empty_update_is_refused(self, tmp_path):
        # Neither a removal nor an update: writing nothing would still rewrite
        # config.json and refresh routing for no reason.
        resp, _ = await _call(_req({}), tmp_path)
        assert _payload(resp)["code"] == "nothing_to_update"

    @pytest.mark.asyncio
    async def test_a_refusal_writes_no_config(self, tmp_path):
        await _call(_req({"agent": 7}), tmp_path)
        assert not (tmp_path / "config.json").exists()


class TestSet:
    @pytest.mark.asyncio
    async def test_agent_and_activation_land_in_config(self, tmp_path):
        resp, _ = await _call(_req({"agent": "task-intake", "activation": "always"}), tmp_path)
        assert resp.status == 200
        body = _payload(resp)
        assert body["ok"] is True
        assert sorted(body["changed"]) == ["activation", "agent"]
        assert _channels(tmp_path)[_CHANNEL] == {"activation": "always", "agent": "task-intake"}

    @pytest.mark.asyncio
    async def test_rewriting_the_same_values_reports_no_change(self, tmp_path):
        await _call(_req({"agent": "a"}), tmp_path)
        resp, _ = await _call(_req({"agent": "a"}), tmp_path)
        assert _payload(resp)["changed"] == []
        assert _channels(tmp_path)[_CHANNEL]["agent"] == "a"

    @pytest.mark.asyncio
    async def test_existing_unrelated_config_is_preserved(self, tmp_path):
        (tmp_path / "config.json").write_text(
            json.dumps({"dashboard": {"url": "http://x:1"}, "slack": {"botToken": "keep"}}),
            encoding="utf-8",
        )
        await _call(_req({"agent": "a"}), tmp_path)
        doc = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert doc["dashboard"] == {"url": "http://x:1"}
        assert doc["slack"]["botToken"] == "keep"
        assert doc["slack"]["channels"][_CHANNEL]["agent"] == "a"

    @pytest.mark.asyncio
    async def test_other_channels_are_untouched(self, tmp_path):
        (tmp_path / "config.json").write_text(
            json.dumps({"slack": {"channels": {"C9999OTHER": {"agent": "other"}}}}),
            encoding="utf-8",
        )
        await _call(_req({"agent": "a"}), tmp_path)
        channels = _channels(tmp_path)
        assert channels["C9999OTHER"] == {"agent": "other"}
        assert channels[_CHANNEL]["agent"] == "a"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("activation", sorted(_ACTIVATION_VALUES))
    async def test_every_activation_mode_is_accepted(self, activation, tmp_path):
        resp, _ = await _call(_req({"activation": activation}), tmp_path)
        assert resp.status == 200
        assert _channels(tmp_path)[_CHANNEL]["activation"] == activation


class TestRemove:
    @pytest.mark.asyncio
    async def test_remove_deletes_the_entry(self, tmp_path):
        await _call(_req({"agent": "a"}), tmp_path)
        resp, _ = await _call(_req({"remove": True}), tmp_path)
        assert resp.status == 200
        assert _payload(resp)["changed"] == ["removed"]
        assert _CHANNEL not in _channels(tmp_path)

    @pytest.mark.asyncio
    async def test_remove_leaves_other_channels_alone(self, tmp_path):
        (tmp_path / "config.json").write_text(
            json.dumps({"slack": {"channels": {"C9999OTHER": {"agent": "other"}}}}),
            encoding="utf-8",
        )
        await _call(_req({"agent": "a"}), tmp_path)
        await _call(_req({"remove": True}), tmp_path)
        channels = _channels(tmp_path)
        assert _CHANNEL not in channels
        assert channels["C9999OTHER"] == {"agent": "other"}

    @pytest.mark.asyncio
    async def test_removing_an_absent_entry_is_a_no_op(self, tmp_path):
        # Teardown must be idempotent: an app that retries a removal, or removes
        # a recipe whose channel was already archived, must not get an error.
        resp, _ = await _call(_req({"remove": True}), tmp_path)
        assert resp.status == 200
        assert _payload(resp)["changed"] == []


class TestFailClosed:
    """An unreadable config must be refused, never overwritten."""

    @pytest.mark.asyncio
    async def test_corrupt_config_is_refused_and_left_intact(self, tmp_path):
        # The regression this replaces: the endpoint used to log a warning and
        # rewrite from {}, destroying every unrelated setting including the bot
        # token. The shared writer fails closed instead.
        corrupt = "{ this is not json"
        (tmp_path / "config.json").write_text(corrupt, encoding="utf-8")
        resp, reload_mock = await _call(_req({"agent": "a"}), tmp_path)
        assert resp.status == 500
        assert _payload(resp)["code"] == "config_write_failed"
        # Untouched on disk, and no live refresh was attempted.
        assert (tmp_path / "config.json").read_text(encoding="utf-8") == corrupt
        reload_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_writer_failure_surfaces_as_a_coded_error(self, tmp_path):
        with (
            patch.object(slack_routing, "_sel", return_value=MagicMock()),
            patch(
                "kiro_crew.slack.handler._persist_channel_config",
                side_effect=ValueError("Refusing to write to sensitive path"),
            ),
        ):
            resp = await api_slack_channel_routing_put(_req({"agent": "a"}))
        assert resp.status == 500
        assert _payload(resp)["code"] == "config_write_failed"


class TestDelegation:
    """The write must go through the dual-locked shared writer, not a local one."""

    @pytest.mark.asyncio
    async def test_the_shared_channel_writer_is_what_runs(self, tmp_path):
        cfg = tmp_path / "config.json"
        with (
            patch.object(slack_routing, "_sel", return_value=MagicMock()),
            patch("kiro_crew.slack.handler.config_path", return_value=cfg),
            patch("kiro_crew.slack.handler.get_orch_cfg", return_value=None),
            patch(
                "kiro_crew.slack.handler._persist_channel_config", return_value=["agent"]
            ) as writer,
        ):
            resp = await api_slack_channel_routing_put(_req({"agent": "a", "activation": "always"}))
        assert resp.status == 200
        writer.assert_called_once_with(_CHANNEL, activation="always", agent="a", remove=False)

    @pytest.mark.asyncio
    async def test_this_module_no_longer_owns_a_config_writer(self):
        # Guard against the duplicate returning: a second read-modify-write of
        # config.json is what lets the two writer families revert each other.
        import inspect

        src = inspect.getsource(slack_routing)
        assert "atomic_write" not in src
        assert "_write_routing_locked" not in src


class TestRefresh:
    @pytest.mark.asyncio
    async def test_routing_refreshes_when_the_orchestrator_is_bound(self, tmp_path):
        resp, reload_mock = await _call(_req({"agent": "a"}), tmp_path, orch=object())
        assert _payload(resp)["routing_refreshed"] is True
        reload_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_durable_write_still_succeeds_with_no_orchestrator(self, tmp_path):
        # Slack disabled or gateway not up: the on-disk write is what matters and
        # is read at next boot, so this is reported as success-without-refresh.
        resp, reload_mock = await _call(_req({"agent": "a"}), tmp_path, orch=None)
        assert resp.status == 200
        assert _payload(resp)["routing_refreshed"] is False
        reload_mock.assert_not_called()
        assert _channels(tmp_path)[_CHANNEL]["agent"] == "a"

    def test_refresh_reports_false_when_the_reload_raises(self):
        # Defensive branch: a broken in-memory refresh must not surface as a 500,
        # because the durable write has already landed by this point.
        with (
            patch("kiro_crew.slack.handler.get_orch_cfg", return_value=object()),
            patch(
                "kiro_crew.slack.handler._reload_orch_cfg",
                side_effect=RuntimeError("boom"),
            ),
        ):
            assert _refresh_routing() is False


class TestCaller:
    def test_app_identity_is_attributed(self):
        req = make_mocked_request("PUT", "/x")
        req["app"] = "kiro-crew-recipe-system"
        assert _caller(req) == "app:kiro-crew-recipe-system"

    def test_dashboard_is_the_fallback(self):
        assert _caller(make_mocked_request("PUT", "/x")) == "dashboard"

    def test_empty_app_falls_back_to_dashboard(self):
        req = make_mocked_request("PUT", "/x")
        req["app"] = ""
        assert _caller(req) == "dashboard"


def test_sel_returns_the_audit_logger():
    """``_sel`` is the audit seam every branch logs through."""
    sentinel = object()
    with patch.object(slack_routing, "_sel_impl", return_value=sentinel):
        assert slack_routing._sel() is sentinel


def test_activation_values_are_the_loader_constants():
    from kiro_crew.config import loader

    assert _ACTIVATION_VALUES == frozenset(
        {
            loader.ACTIVATION_ALWAYS,
            loader.ACTIVATION_MENTION,
            loader.ACTIVATION_OBSERVE,
            loader.ACTIVATION_REVIEW,
            loader.ACTIVATION_OFF,
        }
    )
