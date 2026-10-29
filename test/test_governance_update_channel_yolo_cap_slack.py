"""``updates.channel``, ``yolo_max_seconds`` and ``channels.posture.slack.allowed_channel_ids``.

RFC crew-enterprise-edition, appendix rows "Update channel", "Auto-approval duration"
and "Slack channels". Each key parses (and a bad value fails the load), folds across
tiers, and is enforced where its feature reads it. With no policy an ``oss`` build
behaves as before; an enterprise build follows ``stable``, caps a grant at the
shortest duration and admits no Slack channel.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import parse_policy, parse_profile

EDITIONS = ("oss", "enterprise")

#: The package sources, read as text by the route-coverage tests.
_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


def _doc(**body: object) -> dict:
    return {"version": 1, "boot": {"fail_closed": True}, **body}


def _set_edition(monkeypatch: pytest.MonkeyPatch, value: str) -> bool:
    monkeypatch.setattr("kiro_crew.edition.EDITION", value)
    return value == "enterprise"


def _install(monkeypatch: pytest.MonkeyPatch, doc: dict | None) -> None:
    from kiro_crew.platform import context as pc

    ctx = SimpleNamespace(governance=parse_policy(doc) if doc is not None else None)
    monkeypatch.setattr(pc, "current_context", lambda: ctx)


# ── updates.channel ─────────────────────────────────────────────────────


class TestUpdateChannel:
    def test_the_pin_parses_and_an_unknown_channel_fails_the_load(self) -> None:
        assert parse_policy(_doc(updates={"channel": "Insider"})).updates.channel == "insider"
        assert parse_policy(_doc()).updates.channel == ""
        for bad in ("beta", 3, False):
            with pytest.raises(PlatformCompositionError):
                parse_policy(_doc(updates={"channel": bad}))

    def test_the_policy_channels_are_the_release_channels(self) -> None:
        from kiro_crew.platform.governance_parts.carriers import UPDATE_CHANNELS
        from kiro_crew.platform.update_layout import RELEASE_CHANNELS

        assert set(UPDATE_CHANNELS) == set(RELEASE_CHANNELS)

    def test_tiers_keep_the_authority_channel(self) -> None:
        from kiro_crew.platform.governance import _intersect_ceilings as intersect_ceilings

        top = parse_policy(_doc(updates={"channel": "stable"}))
        low = parse_policy(_doc(updates={"channel": "nightly"}))
        none = parse_policy(_doc())
        assert intersect_ceilings(top, low).updates.channel == "stable"
        assert intersect_ceilings(none, low).updates.channel == "nightly"

    @pytest.mark.parametrize("edition", EDITIONS)
    @pytest.mark.parametrize("pin", ["", "insider"])
    def test_release_channel_follows_the_pin(self, monkeypatch, tmp_path, edition, pin) -> None:
        from kiro_crew.platform import update_layout

        enterprise = _set_edition(monkeypatch, edition)
        (tmp_path / "channel").write_text("nightly\n", encoding="utf-8")
        monkeypatch.setattr(update_layout, "data_home", lambda: tmp_path)
        _install(monkeypatch, _doc(updates={"channel": pin}) if pin else None)
        expected = pin or ("stable" if enterprise else "nightly")
        assert update_layout.release_channel() == expected

    @pytest.mark.parametrize("requested", ["nightly", "insider"])
    def test_the_switch_api_refuses_another_channel_than_the_pin(
        self, monkeypatch, tmp_path, requested
    ) -> None:
        from test_enterprise_unkeyed_locks import _owner_request

        from kiro_crew.dashboard.handlers import updates
        from kiro_crew.platform import update_layout
        from kiro_crew.platform.update_layout import InstallLayout

        _install(monkeypatch, _doc(updates={"channel": "insider"}))
        monkeypatch.setattr(update_layout, "data_home", lambda: tmp_path)
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        wheel = InstallLayout(
            kind="wheel", proj="", is_git=False, is_externally_managed=False, guidance=""
        )
        monkeypatch.setattr(updates, "detect_install_layout", lambda: wheel)
        monkeypatch.setattr(updates, "_audit_update_event", AsyncMock())
        monkeypatch.setattr(updates, "_do_update_check", AsyncMock())
        resp = asyncio.run(updates.api_update_channel(_owner_request({"channel": requested})))
        if requested == "insider":
            assert resp.status == 200
            assert (tmp_path / "channel").read_text(encoding="utf-8") == "insider\n"
        else:
            assert resp.status == 409
            assert not (tmp_path / "channel").exists()


class TestApplyChannelPin:
    """Every route that installs a wheel meets the one channel-pin check.

    ``wheel_engine.check_policy_channel`` runs inside the engine under the update
    lock, so each install route is covered by reaching ``apply_wheel_update``; the
    approve route also asks it up front (``test_update_stepup.py``,
    ``test_channel_pin_landing_after_arm_refuses_approve``).
    """

    def test_the_check_refuses_another_channel_than_the_pin(self, monkeypatch) -> None:
        from kiro_crew.platform import wheel_engine

        pin = "kiro_crew.platform.update_layout.policy_release_channel"
        monkeypatch.setattr(pin, lambda: "stable")
        with pytest.raises(wheel_engine.WheelUpdateError, match="stable channel"):
            wheel_engine.check_policy_channel("nightly")
        wheel_engine.check_policy_channel("stable")
        monkeypatch.setattr(pin, lambda: "")
        wheel_engine.check_policy_channel("nightly")

    def test_the_engine_refuses_before_its_preflight_and_any_download(
        self, monkeypatch, tmp_path
    ) -> None:
        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(
            "kiro_crew.platform.update_layout.policy_release_channel", lambda: "stable"
        )
        monkeypatch.setattr(wheel_engine, "_sweep_layout_debris", lambda *a, **k: None)
        monkeypatch.setattr(wheel_engine, "_repair_dangling_stable_link", lambda *a: None)
        monkeypatch.setattr(wheel_engine, "_usable_promoted_tree", lambda *a: False)
        fetched: list[str] = []
        monkeypatch.setattr(
            wheel_engine, "fetch_verified_manifest", lambda **k: fetched.append("x")
        )
        ran: list[bool] = []
        layout = MagicMock()
        layout.versioned_tree.return_value = tmp_path / "v"
        with pytest.raises(wheel_engine.WheelUpdateError, match="stable channel"):
            wheel_engine._apply_locked(
                layout,
                channel="nightly",
                feed_base="https://feed.example",
                artifact_base="https://cdn.example",
                expected_version="9.9.9",
                progress=lambda _m: None,
                ctx=MagicMock(),
                preflight=lambda: ran.append(True),
                before_promote=lambda: None,
            )
        assert ran == [] and fetched == []

    def test_a_pin_installed_during_the_build_stops_the_swap(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.platform import wheel_engine

        pin = {"value": ""}
        monkeypatch.setattr(
            "kiro_crew.platform.update_layout.policy_release_channel", lambda: pin["value"]
        )
        monkeypatch.setattr(wheel_engine, "_sweep_layout_debris", lambda *a, **k: None)
        monkeypatch.setattr(wheel_engine, "_repair_dangling_stable_link", lambda *a: None)
        monkeypatch.setattr(wheel_engine, "_usable_promoted_tree", lambda *a: False)
        monkeypatch.setattr(wheel_engine, "_staging_dir", lambda: tmp_path)
        monkeypatch.setattr(
            wheel_engine, "fetch_verified_manifest", lambda **k: {"version": "9.9.9"}
        )
        monkeypatch.setattr(wheel_engine, "_check_buildable", lambda _p: None)
        monkeypatch.setattr(wheel_engine, "_require_free_space", lambda _p: None)
        monkeypatch.setattr(
            wheel_engine, "download_verified_wheel", lambda *a, **k: tmp_path / "w.whl"
        )

        def _build(*_a, **_k) -> None:
            pin["value"] = "stable"  # a central refresh lands mid-build

        monkeypatch.setattr(wheel_engine, "build_shadow_venv", _build)
        monkeypatch.setattr(wheel_engine, "verify_shadow_venv", lambda *a, **k: None)
        monkeypatch.setattr(wheel_engine, "_dir_identity", lambda _p: None)
        monkeypatch.setattr(wheel_engine, "_is_unpromoted_debris", lambda *a: False)
        promoted: list[bool] = []
        monkeypatch.setattr(wheel_engine, "promote", lambda *a: promoted.append(True))
        layout = MagicMock()
        layout.versioned_tree.return_value = tmp_path / "v"
        ctx = MagicMock()
        ctx.cancel = None
        with pytest.raises(wheel_engine.WheelUpdateError, match="stable channel"):
            wheel_engine._apply_locked(
                layout,
                channel="nightly",
                feed_base="https://feed.example",
                artifact_base="https://cdn.example",
                expected_version="9.9.9",
                progress=lambda _m: None,
                ctx=ctx,
                preflight=lambda: None,
                before_promote=lambda: None,
            )
        assert promoted == []

    @pytest.mark.parametrize(
        ("route", "module", "call"),
        [
            ("dashboard approve", "dashboard/handlers/updates_step_up.py", "run_wheel_apply("),
            ("scheduled auto-apply", "slack/gateway_runtime/update_apply.py", "run_wheel_apply("),
            ("server update CLI", "cli_server_update.py", "apply_wheel_update("),
            ("run_wheel_apply", "platform/wheel_apply.py", "wheel_engine.apply_wheel_update,"),
        ],
    )
    def test_each_install_route_goes_through_the_engine(self, route, module, call) -> None:
        source = (_SRC / module).read_text(encoding="utf-8")
        assert call in source, route

    def test_no_module_installs_a_tree_outside_the_engine(self) -> None:
        # The engine's own install steps; a caller using them directly would
        # skip the check ``apply_wheel_update`` runs.
        import re

        uses_step = re.compile(
            r"wheel_engine\.(build_shadow_venv|promote)\b"
            r"|from kiro_crew\.platform\.wheel_engine import [^)]*\b(build_shadow_venv|promote)\b",
            re.S,
        )
        offenders = sorted(
            path.relative_to(_SRC).as_posix()
            for path in _SRC.rglob("*.py")
            if path.name != "wheel_engine.py" and uses_step.search(path.read_text(encoding="utf-8"))
        )
        assert offenders == []


# ── yolo_max_seconds ────────────────────────────────────────────────────


class TestYoloCap:
    def test_the_cap_parses_and_a_bad_value_fails_the_load(self) -> None:
        assert parse_policy(_doc(yolo_max_seconds=900)).yolo_max_seconds == 900
        assert parse_policy(_doc()).yolo_max_seconds == 0
        for bad in (0, -5, True, "900", 1.5):
            with pytest.raises(PlatformCompositionError):
                parse_policy(_doc(yolo_max_seconds=bad))

    def test_a_profile_may_not_set_the_cap(self) -> None:
        with pytest.raises(PlatformCompositionError):
            parse_profile({"name": "p", "yolo_max_seconds": 60})

    def test_tiers_keep_the_lower_cap(self) -> None:
        from kiro_crew.platform.governance import _intersect_ceilings as intersect_ceilings

        top = parse_policy(_doc(yolo_max_seconds=3600))
        low = parse_policy(_doc(yolo_max_seconds=600))
        none = parse_policy(_doc())
        assert intersect_ceilings(top, low).yolo_max_seconds == 600
        assert intersect_ceilings(none, low).yolo_max_seconds == 600
        assert intersect_ceilings(top, none).yolo_max_seconds == 3600

    @pytest.mark.parametrize("edition", EDITIONS)
    @pytest.mark.parametrize("cap", [0, 900])
    def test_yolo_ttl_cap(self, monkeypatch, edition, cap) -> None:
        from kiro_crew.platform.governance_parts.baseline import ENTERPRISE_YOLO_MAX_SECONDS
        from kiro_crew.safety_override_permits import yolo_ttl_cap

        enterprise = _set_edition(monkeypatch, edition)
        _install(monkeypatch, _doc(yolo_max_seconds=cap) if cap else None)
        expected = cap or (ENTERPRISE_YOLO_MAX_SECONDS if enterprise else 0)
        assert yolo_ttl_cap() == expected

    def test_no_untimed_grant_under_a_cap(self, monkeypatch) -> None:
        from kiro_crew import safety_override_permits as permits

        monkeypatch.setattr("kiro_crew.safety_override.yolo_ttl_cap", lambda: 900)
        assert permits.declared_grant_permitted() is False
        assert permits.until_shutdown_permitted() is False

    @pytest.fixture
    def capped(self, monkeypatch):
        from kiro_crew.safety_override import reset_singleton, safety_override

        reset_singleton()
        monkeypatch.setattr("kiro_crew.safety_override.yolo_ttl_cap", lambda: 900)
        with patch("kiro_crew.safety_override.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            yield safety_override()
        reset_singleton()

    def test_live_grants_are_clamped_before_the_permit_is_published(
        self, capped, monkeypatch
    ) -> None:
        from kiro_crew import safety_override_grant as grant

        monkeypatch.setattr("kiro_crew.safety_override.approval_mode_permitted", lambda _m: True)
        # The invalidation mask that precedes every push.
        grant._on_ceiling_invalidating()
        seen: list[bool] = []

        def _clamp(cap: int) -> bool:
            seen.append(grant._yolo_policy_permitted)
            return False

        monkeypatch.setattr(capped, "clamp_to_cap", _clamp)
        grant._push_yolo_policy()
        assert seen == [False]
        assert grant._yolo_policy_permitted is True

    def test_a_cap_past_a_day_still_leaves_the_24h_ceiling(self, capped, monkeypatch) -> None:
        from kiro_crew.safety_override import SafetyOverride

        monkeypatch.setattr("kiro_crew.safety_override.yolo_ttl_cap", lambda: 172800)
        assert capped.activate_declared().ttl == SafetyOverride._MAX_TTL
        assert capped.is_permanent is False

    def test_activations_are_held_to_the_cap(self, capped) -> None:
        assert capped.activate("dashboard").ttl == 900
        assert capped.activate("dashboard", ttl=60).ttl == 60
        declared = capped.activate_declared()
        assert declared.ttl == 900
        assert capped.is_permanent is False
        assert capped.activate_scoped("slot:1", "dashboard").ttl == 900

    def test_a_live_grant_is_shortened_by_a_new_cap(self, capped, monkeypatch) -> None:

        monkeypatch.setattr("kiro_crew.safety_override.yolo_ttl_cap", lambda: 0)
        capped.activate_declared()
        assert capped.is_permanent is True
        capped.clamp_to_cap(900)
        assert capped.is_permanent is False
        assert 0 < capped.remaining_secs() <= 900

    def test_a_new_cap_counts_from_when_the_grant_was_armed(self, capped, monkeypatch) -> None:

        monkeypatch.setattr("kiro_crew.safety_override.yolo_ttl_cap", lambda: 0)
        capped.activate("dashboard", ttl=3600)
        capped._activated_at -= 1700
        capped.clamp_to_cap(1800)
        assert capped.remaining_secs() <= 100
        # Past the new cap already: the deadline moves into the past and the next
        # read expires the grant through the TTL-lapse path, notice included.
        assert capped.activate_scoped("run:1", "dashboard", ttl=60).active
        expired = MagicMock()
        capped.on_expired = expired
        assert capped.clamp_to_cap(1500) is True
        assert capped.is_active() is False
        expired.assert_called_once()
        assert capped.is_scope_active("run:1")

    def test_a_scoped_grant_past_a_new_cap_is_ended(self, capped, monkeypatch) -> None:

        monkeypatch.setattr("kiro_crew.safety_override.yolo_ttl_cap", lambda: 0)
        capped.activate_scoped("run:1", "dashboard", ttl=3600)
        started, ends = capped._scoped["run:1"]
        capped._scoped["run:1"] = (started - 1000, ends)
        capped.clamp_to_cap(900)
        assert capped.is_scope_active("run:1") is False


# ── channels.posture.slack.allowed_channel_ids ──────────────────────────


def _slack_channels(*allowed: str) -> dict:
    return _doc(
        channels={
            "members": {"mode": "allow", "allow": ["slack"]},
            "posture": {
                "slack": {"allowed_channel_ids": {"mode": "allow", "allow": list(allowed)}}
            },
        }
    )


class TestSlackChannels:
    @pytest.mark.parametrize("edition", EDITIONS)
    def test_the_list_admits_what_it_names(self, monkeypatch, edition) -> None:
        from kiro_crew.slack.channel_policy import channel_refusal

        _set_edition(monkeypatch, edition)
        _install(monkeypatch, _slack_channels("C0123", "D*"))
        assert channel_refusal("C0123") == ""
        assert channel_refusal("D9999") == ""
        assert "refused by the security policy" in channel_refusal("C0999")

    @pytest.mark.parametrize("edition", EDITIONS)
    @pytest.mark.parametrize(
        "doc",
        [None, _doc(), _doc(channels={"members": {"mode": "allow", "allow": ["slack"]}})],
        ids=["no-ceiling", "unnamed", "no-leaf"],
    )
    def test_an_unnamed_list_admits_nothing_only_in_enterprise(
        self, monkeypatch, edition, doc
    ) -> None:
        from kiro_crew.slack.channel_policy import channel_refusal

        enterprise = _set_edition(monkeypatch, edition)
        _install(monkeypatch, doc)
        assert bool(channel_refusal("C0123")) is enterprise

    def test_every_refusal_is_audited_once(self, monkeypatch) -> None:
        from kiro_crew.slack.channel_policy import channel_refusal

        _install(monkeypatch, _slack_channels("C0123"))
        fake = MagicMock()
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: fake)
        assert channel_refusal("C0123") == ""
        assert channel_refusal("C0999", operation="slack.interactive", caller="U1")
        assert channel_refusal("C0999", audit=False)
        assert fake.log_api_access.call_count == 1
        kwargs = fake.log_api_access.call_args.kwargs
        assert (kwargs["operation"], kwargs["caller"], kwargs["outcome"]) == (
            "slack.interactive",
            "U1",
            "denied",
        )

    @pytest.mark.parametrize("edition", EDITIONS)
    def test_an_evaluation_error_refuses_only_in_enterprise(self, monkeypatch, edition) -> None:
        from kiro_crew.platform import context as pc
        from kiro_crew.slack.channel_policy import channel_refusal

        enterprise = _set_edition(monkeypatch, edition)

        def _boom():
            raise RuntimeError("transient")

        monkeypatch.setattr(pc, "current_context", _boom)
        assert bool(channel_refusal("C0123")) is enterprise

    @pytest.mark.asyncio
    async def test_a_client_write_to_a_refused_channel_never_reaches_slack(
        self, monkeypatch
    ) -> None:
        from kiro_crew.slack.channel_policy import SlackChannelRefused
        from kiro_crew.slack.client import RealSlackClient

        _install(monkeypatch, _slack_channels("C0123"))
        client = RealSlackClient.__new__(RealSlackClient)
        client._web = MagicMock()
        client._web.chat_postMessage = AsyncMock(return_value={"ts": "1"})
        client._web.chat_postEphemeral = AsyncMock()
        client._web.files_upload_v2 = AsyncMock()
        client._web.api_call = AsyncMock(return_value={"ts": "1"})
        client.ensure_channel_team = AsyncMock()
        with pytest.raises(SlackChannelRefused):
            await client.post_message("C0999", "hi")
        with pytest.raises(SlackChannelRefused):
            await client.post_blocks("C0999", [], "hi")
        with pytest.raises(SlackChannelRefused):
            await client.post_ephemeral("C0999", "U1", "hi")
        with pytest.raises(SlackChannelRefused):
            await client.upload_file("C0999", "1", "/dev/null", "f", "t")
        with pytest.raises(SlackChannelRefused):
            await client.start_stream("C0999", "1")
        client._web.chat_postMessage.assert_not_awaited()
        client._web.chat_postEphemeral.assert_not_awaited()
        client._web.files_upload_v2.assert_not_awaited()
        # The stream and thread writes swallow the refusal, as their API errors.
        assert await client.append_stream("C0999", "1", "x") is False
        await client.set_thread_title("C0999", "1", "title")
        await client.set_thread_status("C0999", "1", "working")
        await client.set_suggested_prompts("C0999", "1", [{"title": "a", "message": "b"}])
        client._web.api_call.assert_not_awaited()
        # Clearing a status adds no content and is not refused.
        await client.set_thread_status("C0999", "1", "")
        client._web.api_call.assert_awaited_once()
        assert await client.post_message("C0123", "hi") == "1"


class TestSlackIntake:
    @pytest.mark.asyncio
    async def test_a_message_in_a_refused_channel_is_dropped_and_audited(self, monkeypatch) -> None:
        from slack_events_coverage_helpers import _event, _make_orch

        from kiro_crew.slack import events as ev

        monkeypatch.setattr(
            "kiro_crew.slack.channel_policy.channel_refusal", lambda _c, **_k: "refused"
        )
        fake_sel = MagicMock()
        orch = _make_orch()
        with patch("kiro_crew.slack.events.sel", return_value=fake_sel):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
        hm.assert_not_called()
        assert fake_sel.log_api_access.call_args.kwargs["error"] == "channel_not_allowed_by_policy"

    @pytest.mark.asyncio
    async def test_a_pure_stop_is_not_held_to_the_channel_list(self, monkeypatch) -> None:
        from slack_events_coverage_helpers import _event, _make_orch

        from kiro_crew.slack import events as ev

        asked: list[str] = []

        def _refuse(channel: str, **_k: object) -> str:
            asked.append(channel)
            return "refused"

        monkeypatch.setattr("kiro_crew.slack.channel_policy.channel_refusal", _refuse)
        orch = _make_orch()
        with patch("kiro_crew.slack.events.sel", return_value=MagicMock()):
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                await ev._route_message(orch, _event(text="!stop"), ev.SeenCache())
        assert asked == []


class TestSlackInteractions:
    """Button and modal entry points meet the channel list, as message intake does."""

    @pytest.mark.asyncio
    async def test_an_options_choice_in_a_refused_channel_is_dropped(self, monkeypatch) -> None:
        from kiro_crew.slack import interactions as ix

        monkeypatch.setattr(ix, "is_allowed_user", lambda _u: True)
        monkeypatch.setattr(ix, "channel_refusal", lambda _c, **_k: "refused")
        click, menu = AsyncMock(), AsyncMock()
        monkeypatch.setattr(ix, "_handle_options", click)
        monkeypatch.setattr(ix, "_open_options_several", menu)
        for action in (
            {"action_id": f"{ix.OPTIONS_ACTION_PREFIX}0", "value": "A"},
            {"action_id": ix.OPTIONS_SEVERAL_ACTION},
        ):
            payload = {
                "type": "block_actions",
                "actions": [action],
                "channel": {"id": "C0999"},
                "message": {"ts": "1"},
                "user": {"id": "U1"},
            }
            await ix.dispatch(payload)
        click.assert_not_awaited()
        menu.assert_not_awaited()

    def test_every_slack_inbound_gate_also_asks_the_channel(self) -> None:
        # The intake gate (events.py), the nudge fire and the two review-modal
        # submits (which learn their channel only after parsing the modal) ask the
        # channel on their own lines; every other Slack transport gate pairs them.
        unpaired = []
        for path in sorted((_SRC / "slack").rglob("*.py")):
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines, 1):
                if 'await channel_inbound_permitted("slack")' not in line:
                    continue
                # The pair may wrap: the gate and the next two lines.
                gate = " ".join(lines[number - 1 : number + 2])
                if "and not channel_refusal(channel," not in gate:
                    unpaired.append(f"{path.relative_to(_SRC).as_posix()}:{number}")
        files = sorted(site.rsplit(":", 1)[0] for site in unpaired)
        assert files == [
            "slack/events.py",
            "slack/gateway_runtime/nudge_channel_fire.py",
            "slack/interactions_approvals.py",
            "slack/interactions_approvals.py",
        ]


class TestEnterpriseTiers:
    """A local tier under a central policy narrows the enterprise values, never sets them."""

    def _compose(self, monkeypatch, central: dict, home: dict):
        from dataclasses import replace

        from kiro_crew.platform.governance import TIER_CENTRAL, TIER_HOME, compose_tier_ladder

        _set_edition(monkeypatch, "enterprise")
        top = replace(parse_policy(central), tier=TIER_CENTRAL)
        low = replace(parse_policy(home), tier=TIER_HOME)
        return compose_tier_ladder(top, low)

    def test_a_home_tier_cannot_replace_the_enterprise_values(self, monkeypatch) -> None:
        central = _doc(channels={"members": {"mode": "allow", "allow": ["slack"]}})
        home = {
            **_slack_channels("C0123"),
            "yolo_max_seconds": 3600,
            "updates": {"channel": "nightly"},
        }
        ceiling = self._compose(monkeypatch, central, home)
        assert ceiling.yolo_max_seconds == 1800
        assert ceiling.updates.channel == "stable"
        _install(monkeypatch, None)
        from kiro_crew.platform import context as pc

        monkeypatch.setattr(pc, "current_context", lambda: SimpleNamespace(governance=ceiling))
        from kiro_crew.slack.channel_policy import channel_refusal

        assert channel_refusal("C0123")

    def test_a_home_tier_still_narrows_them(self, monkeypatch) -> None:
        central = _doc(yolo_max_seconds=3600, updates={"channel": "insider"})
        ceiling = self._compose(monkeypatch, central, _doc(yolo_max_seconds=600))
        assert ceiling.yolo_max_seconds == 600
        assert ceiling.updates.channel == "insider"


class TestSessionRenewalCap:
    def test_renewal_cannot_carry_a_grant_past_the_cap(self, monkeypatch) -> None:
        from kiro_crew.safety_override import reset_singleton, safety_override

        reset_singleton()
        monkeypatch.setattr("kiro_crew.safety_override.yolo_ttl_cap", lambda: 900)
        try:
            with patch("kiro_crew.safety_override.sel") as mock_sel:
                mock_sel.return_value = MagicMock()
                so = safety_override()
                assert so.activate("dashboard").active
                # Armed 850s ago: a renewal may add at most the 50s left.
                so._activated_at -= 850
                so._expires_at -= 850
                renewed = so.renew("dashboard")
                assert renewed.renewed and renewed.ttl <= 50
                so._activated_at -= 100
                refused = so.renew("dashboard")
                assert not refused.renewed
                assert refused.reason == "ceiling_reached"
        finally:
            reset_singleton()


class TestScopedRenewalCap:
    def test_activity_renewal_cannot_carry_a_grant_past_the_cap(self, monkeypatch) -> None:
        from kiro_crew.safety_override import reset_singleton, safety_override

        reset_singleton()
        monkeypatch.setattr("kiro_crew.safety_override.yolo_ttl_cap", lambda: 900)
        try:
            with patch("kiro_crew.safety_override.sel") as mock_sel:
                mock_sel.return_value = MagicMock()
                so = safety_override()
                assert so.activate_scoped("run:1", "dashboard").active
                started, _ = so._scoped["run:1"]
                so._scoped["run:1"] = (started - 899, started + 1)
                result = so.renew_scoped("run:1", "dashboard")
                _, ends = so._scoped["run:1"]
                assert ends <= started - 899 + 900 + 0.001
                assert result.ttl <= 1
        finally:
            reset_singleton()
