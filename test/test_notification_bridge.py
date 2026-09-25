"""Notification bridge (RFC phase B1): routing, governance, isolation, loops.

The four properties the RFC's exit criteria name, each pinned here rather than
left to reading:

* a ``critical`` note on a routed channel lands on the transport with redacted
  content and a paired SEL record;
* local delivery is unchanged and never waits on a transport;
* the dispatcher has no path that can publish to the bus;
* governance denies fail-closed, and one transport's failure cannot suppress
  another's delivery.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from kiro_crew.notifications import bridge as bridge_mod
from kiro_crew.notifications.bridge import (
    BRIDGE_BURST,
    DEFAULT_DELIVER_MIN_PRIORITY,
    DELIVER_MIN_PRIORITIES,
    KNOWN_BRIDGE_TRANSPORTS,
    BridgeDispatcher,
    BridgeRateLimiter,
    DeliveryRule,
    normalize_deliver_to,
    normalize_min_priority,
    priority_clears_floor,
    render_bridge_text,
    rule_from_settings,
)
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY


class _RecordingSink:
    """A sink that records what it was handed and can be told to fail."""

    def __init__(self, transport_id: str = "slack", fail_times: int = 0) -> None:
        self.transport_id = transport_id
        self.sent: list[str] = []
        self._fail_times = fail_times
        self.attempts = 0

    async def send(self, text: str) -> str:
        self.attempts += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("transient")
        self.sent.append(text)
        return f"{self.transport_id}-msg-{len(self.sent)}"


def _permit(*_args: Any, **_kwargs: Any) -> Any:
    return mock.Mock(permitted=True, rule="", layer="", reason="")


def _deny(*_args: Any, **_kwargs: Any) -> Any:
    return mock.Mock(permitted=False, rule="policy", layer="profile", reason="no")


def _dispatcher(
    sinks: dict[str, Any] | None = None,
    settings: dict[str, dict[str, Any]] | None = None,
    **kwargs: Any,
) -> BridgeDispatcher:
    # `is None` rather than `or {}`: a test that passes an EMPTY dict and fills
    # it later (a transport reconnecting mid-test) must have the resolver close
    # over ITS dict, not a fresh replacement.
    if sinks is None:
        sinks = {}
    if settings is None:
        settings = {}
    return BridgeDispatcher(
        sink_resolver=lambda transport: sinks.get(transport),
        settings_reader=lambda channel: settings.get(channel, {}),
        **kwargs,
    )


class RuleNormalizationTests(unittest.TestCase):
    def test_known_transports_are_the_five_chat_channels(self) -> None:
        self.assertEqual(
            KNOWN_BRIDGE_TRANSPORTS, ("slack", "discord", "telegram", "webex", "wecom")
        )

    def test_deliver_to_dedupes_and_orders_canonically(self) -> None:
        self.assertEqual(normalize_deliver_to(["wecom", "slack", "slack"]), ("slack", "wecom"))

    def test_deliver_to_rejects_unknown_transport(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            normalize_deliver_to(["slack", "pigeon"])
        self.assertIn("pigeon", str(ctx.exception))

    def test_deliver_to_rejects_a_bare_string(self) -> None:
        # A bare string is iterable, so a permissive check would read "slack"
        # as five single-character transport ids. It would still raise -- on
        # the unknown-transport branch -- so the thing worth asserting is WHICH
        # error the caller gets: "send a list" is actionable, "unknown
        # transport 's'" sends them looking for a typo they did not make.
        with self.assertRaises(ValueError) as ctx:
            normalize_deliver_to("slack")
        self.assertIn("must be a list", str(ctx.exception))

    def test_an_empty_string_is_refused_rather_than_silently_disarming(self) -> None:
        # The case where the per-entry check cannot save a permissive guard:
        # "" iterates to nothing, so without the type check it would read as
        # "the user cleared the route" and quietly turn bridging off.
        with self.assertRaises(ValueError):
            normalize_deliver_to("")

    def test_a_mapping_cannot_arm_a_route_off_its_keys(self) -> None:
        # A mapping is iterable and yields its KEYS, so an "is it iterable"
        # guard would read {"slack": False} -- a hand-edited file saying do NOT
        # deliver to Slack -- as arming Slack. The value is never even read.
        for shape in ({"slack": False}, {"slack": True}, {"slack"}):
            with self.subTest(shape=shape), self.assertRaises(ValueError) as ctx:
                normalize_deliver_to(shape)
            self.assertIn("must be a list", str(ctx.exception))

    def test_a_tuple_is_accepted(self) -> None:
        # Requiring list-or-tuple rather than list alone: a stored rule read
        # back through code that tuples it must not be refused.
        self.assertEqual(normalize_deliver_to(("slack",)), ("slack",))

    def test_a_mapping_stored_on_disk_disarms_rather_than_arming(self) -> None:
        # Same shape through the tolerant reader: it must fall to no route, not
        # inherit the keys.
        self.assertEqual(rule_from_settings({"deliver_to": {"slack": False}}), DeliveryRule())

    def test_deliver_to_rejects_non_string_entry(self) -> None:
        with self.assertRaises(ValueError):
            normalize_deliver_to([1])

    def test_min_priority_defaults_to_critical(self) -> None:
        self.assertEqual(normalize_min_priority(None), DEFAULT_DELIVER_MIN_PRIORITY)
        self.assertEqual(DEFAULT_DELIVER_MIN_PRIORITY, "critical")

    def test_min_priority_rejects_unknown_floor(self) -> None:
        with self.assertRaises(ValueError):
            normalize_min_priority("sometimes")

    def test_unusable_stored_rule_disarms_rather_than_raising(self) -> None:
        # A hand-edited settings file is not a validation boundary; the PUT is.
        rule = rule_from_settings({"deliver_to": ["pigeon"], "deliver_min_priority": "x"})
        self.assertEqual(rule, DeliveryRule())
        self.assertFalse(rule.armed)

    def test_unusable_floor_alone_keeps_the_route_at_the_default(self) -> None:
        rule = rule_from_settings({"deliver_to": ["slack"], "deliver_min_priority": "x"})
        self.assertEqual(rule.transports, ("slack",))
        self.assertEqual(rule.min_priority, "critical")


class FloorTests(unittest.TestCase):
    def test_critical_floor_admits_only_critical(self) -> None:
        self.assertTrue(priority_clears_floor("critical", "critical"))
        self.assertFalse(priority_clears_floor("default", "critical"))
        self.assertFalse(priority_clears_floor("passive", "critical"))

    def test_default_floor_admits_critical_and_default(self) -> None:
        self.assertTrue(priority_clears_floor("critical", "default"))
        self.assertTrue(priority_clears_floor("default", "default"))
        self.assertFalse(priority_clears_floor("passive", "default"))

    def test_all_floor_admits_passive_too(self) -> None:
        self.assertTrue(priority_clears_floor("passive", "all"))

    def test_unrankable_priority_is_not_admitted(self) -> None:
        self.assertFalse(priority_clears_floor("urgent", "all"))
        self.assertFalse(priority_clears_floor(None, "all"))

    def test_floors_are_the_three_documented_values(self) -> None:
        self.assertEqual(DELIVER_MIN_PRIORITIES, ("critical", "default", "all"))


class RenderTests(unittest.TestCase):
    def test_critical_note_renders_marker_title_body_link_channel(self) -> None:
        text = render_bridge_text(
            {
                "priority": "critical",
                "title": "Approval needed",
                "body": "Agent wants to run rm",
                "url": "/approvals/7",
                "channel": "system.approval",
            }
        )
        self.assertIn("Approval needed", text)
        self.assertIn("Agent wants to run rm", text)
        self.assertIn("/approvals/7", text)
        self.assertIn("system.approval", text)
        self.assertTrue(text.startswith("\U0001f534"))

    def test_non_critical_note_carries_no_marker(self) -> None:
        text = render_bridge_text({"priority": "default", "title": "Job done"})
        self.assertTrue(text.startswith("Job done"))

    def test_long_body_is_bounded_before_the_sink_sees_it(self) -> None:
        text = render_bridge_text({"title": "t", "body": "x" * 5000})
        self.assertLess(len(text), 2200)
        self.assertIn("\u2026", text)

    def test_titleless_note_still_renders_something_sendable(self) -> None:
        self.assertTrue(render_bridge_text({}).strip())


class RoutingTests(unittest.TestCase):
    def test_unarmed_channel_routes_nowhere(self) -> None:
        d = _dispatcher(settings={"system.cron": {}})
        self.assertEqual(d.routes({"channel": "system.cron", "priority": "critical"}), ())

    def test_armed_channel_routes_when_priority_clears_floor(self) -> None:
        d = _dispatcher(settings={"system.cron": {"deliver_to": ["slack"]}})
        self.assertEqual(d.routes({"channel": "system.cron", "priority": "critical"}), ("slack",))

    def test_armed_channel_does_not_route_below_its_floor(self) -> None:
        d = _dispatcher(settings={"system.cron": {"deliver_to": ["slack"]}})
        self.assertEqual(d.routes({"channel": "system.cron", "priority": "default"}), ())

    def test_muted_channel_only_routes_under_an_all_floor(self) -> None:
        # apply() forces a muted channel to passive before the bridge reads it,
        # so mute silences the bridge too unless the user asked for everything.
        muted = {"channel": "system.cron", "priority": "passive", "silenced": True}
        strict = _dispatcher(settings={"system.cron": {"deliver_to": ["slack"]}})
        self.assertEqual(strict.routes(muted), ())
        loose = _dispatcher(
            settings={"system.cron": {"deliver_to": ["slack"], "deliver_min_priority": "all"}}
        )
        self.assertEqual(loose.routes(muted), ("slack",))

    def test_note_without_a_channel_routes_nowhere(self) -> None:
        d = _dispatcher(settings={"system.cron": {"deliver_to": ["slack"]}})
        self.assertEqual(d.routes({"priority": "critical"}), ())

    def test_settings_reader_failure_disarms_rather_than_raising(self) -> None:
        def boom(_channel: str) -> dict[str, Any]:
            raise OSError("settings gone")

        d = BridgeDispatcher(sink_resolver=lambda _t: None, settings_reader=boom)
        self.assertEqual(d.routes({"channel": "system.cron", "priority": "critical"}), ())


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_critical_note_lands_on_the_transport_with_an_sel_record(self) -> None:
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.approval": {"deliver_to": ["slack"]}})
        sel = mock.Mock()
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=sel),
        ):
            outcomes = await d.dispatch(
                {
                    "channel": "system.approval",
                    "source": "system",
                    "priority": "critical",
                    "title": "Approval needed",
                    "body": "please look",
                }
            )
        self.assertEqual([o.delivered for o in outcomes], [True])
        self.assertEqual(len(sink.sent), 1)
        self.assertIn("Approval needed", sink.sent[0])
        sel.log_api_access.assert_called_once()
        kwargs = sel.log_api_access.call_args.kwargs
        self.assertEqual(kwargs["operation"], "notification_bridge.slack")
        self.assertEqual(kwargs["outcome"], "delivered")
        self.assertEqual(kwargs["caller"], "system")
        self.assertEqual(kwargs["source"], bridge_mod.BRIDGE_ORIGIN)

    async def test_credentials_are_redacted_before_egress(self) -> None:
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        secret = "AKIAIOSFODNN7EXAMPLE"
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            await d.dispatch(
                {
                    "channel": "system.cron",
                    "priority": "critical",
                    "title": "creds",
                    "body": f"key={secret}",
                }
            )
        self.assertEqual(len(sink.sent), 1)
        self.assertNotIn(secret, sink.sent[0])

    async def test_a_failed_redactor_withholds_content_rather_than_leaking(self) -> None:
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
            mock.patch(
                "kiro_crew.security.redaction.redact_credentials",
                side_effect=RuntimeError("regex engine down"),
            ),
        ):
            await d.dispatch(
                {
                    "channel": "system.cron",
                    "priority": "critical",
                    "title": "secret title",
                    "body": "AKIAIOSFODNN7EXAMPLE",
                }
            )
        self.assertEqual(len(sink.sent), 1)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", sink.sent[0])
        self.assertNotIn("secret title", sink.sent[0])
        self.assertIn("withheld", sink.sent[0])

    async def test_governance_denial_withholds_the_send_fail_closed(self) -> None:
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_deny),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            outcomes = await d.dispatch(
                {"channel": "system.cron", "priority": "critical", "title": "t"}
            )
        self.assertEqual(sink.sent, [])
        self.assertEqual([o.reason for o in outcomes], ["denied_by_governance"])

    async def test_governance_error_denies_rather_than_degrading_open(self) -> None:
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        with (
            mock.patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit",
                side_effect=RuntimeError("policy unreadable"),
            ),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            outcomes = await d.dispatch(
                {"channel": "system.cron", "priority": "critical", "title": "t"}
            )
        self.assertEqual(sink.sent, [])
        self.assertEqual([o.reason for o in outcomes], ["governance_error"])

    async def test_a_governance_error_denial_is_audited_rather_than_silent(self) -> None:
        """The one denial `vet_and_audit` cannot record is the one this path must record.

        `vet_and_audit` writes the SEL row itself for a denial it RETURNS, and
        `_deliver_one` returns on ANY denial without auditing. So when the evaluation
        RAISES, nothing else on the path writes anything: the note is withheld and the
        trail shows nothing at all. Only the exception TYPE belongs in the row, because
        the message can carry policy or note content and an audit row is not a place to
        widen what this boundary discloses.
        """
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        sel = mock.Mock()
        with (
            mock.patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit",
                side_effect=RuntimeError("policy unreadable"),
            ),
            mock.patch("kiro_crew.sel.sel", return_value=sel),
        ):
            outcomes = await d.dispatch(
                {
                    "channel": "system.cron",
                    "source": "system",
                    "priority": "critical",
                    "title": "t",
                }
            )
        self.assertEqual(sink.sent, [])
        self.assertEqual([o.reason for o in outcomes], ["governance_error"])
        sel.log_api_access.assert_called_once()
        kwargs = sel.log_api_access.call_args.kwargs
        self.assertEqual(kwargs["operation"], "notification_bridge.slack")
        self.assertEqual(kwargs["outcome"], "denied")
        self.assertIn("RuntimeError", kwargs["error"])
        self.assertNotIn("policy unreadable", kwargs["error"])

    async def test_both_governance_scopes_are_checked(self) -> None:
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        with (
            mock.patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit
            ) as vet,
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            await d.dispatch({"channel": "system.cron", "priority": "critical", "title": "t"})
        checked = [(c.args[0], c.args[1]) for c in vet.call_args_list]
        self.assertIn(("capabilities.messaging", ""), checked)
        self.assertIn(("channels", "slack"), checked)
        for call in vet.call_args_list:
            self.assertTrue(call.kwargs["fail_closed"])

    async def test_an_app_note_is_vetted_against_that_apps_own_profile(self) -> None:
        # resolve_active_scope puts the app bind FIRST, because a per-app
        # profile is what bounds an app's blast radius. A leg that never binds
        # app= skips that bind entirely and a surface profile decides instead, so
        # an app whose profile denies messaging would still egress -- with no
        # forgery and nothing unusual about the push.
        #
        # The bind belongs on the app's OWN lookup and nowhere else. Because it
        # outranks the surface bind, carrying it on the host lookup too would
        # make the app profile answer for the host, which is the inverse hole.
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"my-app.alerts": {"deliver_to": ["slack"]}})
        with (
            mock.patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit
            ) as vet,
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            await d.dispatch(
                {
                    "channel": "my-app.alerts",
                    "source": "app:my-app",
                    "priority": "critical",
                    "title": "t",
                }
            )
        self.assertTrue(vet.call_args_list)
        pairs = {(c.kwargs["session_key"], c.kwargs["app"]) for c in vet.call_args_list}
        # The app's own profile is asked about.
        self.assertIn((HOST_SESSION_KEY, "my-app"), pairs)
        # And the host's own profile is asked about separately, unbound, so its
        # denial cannot be answered by the app's profile.
        self.assertIn((HOST_SESSION_KEY, ""), pairs)

    async def test_app_supplied_meta_cannot_choose_the_governance_subject(self) -> None:
        # The bus merges meta flat onto the note and _RESERVED_NOTE_KEYS covers
        # neither session_key, slot nor caller, so these three are request-body
        # values wearing internal names. A claim may ADD a subject; it may never
        # replace the host subject, which is what would let an app name a
        # permissive surface and pick its own profile.
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"my-app.alerts": {"deliver_to": ["slack"]}})
        with (
            mock.patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit
            ) as vet,
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            await d.dispatch(
                {
                    "channel": "my-app.alerts",
                    "source": "app:my-app",
                    "priority": "critical",
                    "title": "t",
                    "session_key": "dashboard-unattended-and-permissive",
                    "slot": "chat-1",
                    "caller": "someone-else",
                }
            )
        pairs = {(c.kwargs["session_key"], c.kwargs["app"]) for c in vet.call_args_list}
        # The host subject is still asked, unbound, every time.
        self.assertIn((HOST_SESSION_KEY, ""), pairs)
        # The app is asked about on its own lookup.
        self.assertIn((HOST_SESSION_KEY, "my-app"), pairs)
        # The claimed key is asked UNBOUND: letting a caller-supplied key carry
        # the app bind would hand the app profile the claim's lookup as well, so
        # a claim that should only be able to narrow could answer for the app.
        claimed = [
            c for c in vet.call_args_list if c.kwargs["session_key"] not in (HOST_SESSION_KEY,)
        ]
        self.assertTrue(claimed)
        for call in claimed:
            self.assertEqual(call.kwargs["app"], "")

    async def test_a_cron_note_is_vetted_under_its_own_job_subject(self) -> None:
        # A cron names its JOB, not a session, so `job_id` is what identifies the producer.
        # Without `cron:<job_id>` in the subject list the only profile consulted is the
        # permissive host one, and a cron whose own profile denies messaging egresses
        # anyway -- every listed subject must permit, but only the listed ones are asked.
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        with (
            mock.patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit
            ) as vet,
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            await d.dispatch(
                {
                    "channel": "system.cron",
                    "source": "cron",
                    "priority": "critical",
                    "title": "t",
                    "job_id": "nightly-digest",
                }
            )
        pairs = {(c.kwargs["session_key"], c.kwargs["app"]) for c in vet.call_args_list}
        # The host subject is still asked, unbound, as it is for every note.
        self.assertIn((HOST_SESSION_KEY, ""), pairs)
        self.assertIn(
            ("cron:nightly-digest", ""),
            pairs,
            "the cron's own job subject was never vetted, so its profile cannot deny",
        )

    async def test_a_legacy_cron_note_is_vetted_under_its_job_even_though_source_is_system(
        self,
    ) -> None:
        # The shape cron notes ACTUALLY travel in. The legacy notify adapter stamps every
        # kind with source="system" while still routing kind "cron" to channel
        # "system.cron", so a source-based test never fires on the real path -- and no
        # producer sets source="cron" at all. Identifying by channel is what makes the cron
        # profile reachable; without it only the permissive host profile is consulted.
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        with (
            mock.patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit
            ) as vet,
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            await d.dispatch(
                {
                    "channel": "system.cron",
                    "source": "system",
                    "priority": "critical",
                    "title": "t",
                    "job_id": "nightly-digest",
                }
            )
        pairs = {(c.kwargs["session_key"], c.kwargs["app"]) for c in vet.call_args_list}
        self.assertIn((HOST_SESSION_KEY, ""), pairs)
        self.assertIn(
            ("cron:nightly-digest", ""),
            pairs,
            "a legacy cron note (source=system) never reached its own job profile",
        )

    async def test_a_forged_claim_can_only_deny_never_permit(self) -> None:
        # The asymmetry that makes consulting an untrusted claim safe: it is
        # intersected, so a forged key cannot lift the host's denial.
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"my-app.alerts": {"deliver_to": ["slack"]}})

        def host_denies(_scope: str, _item: str, **kw: Any) -> Any:
            return _deny() if kw.get("session_key") == HOST_SESSION_KEY else _permit()

        with (
            mock.patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=host_denies
            ),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            outcomes = await d.dispatch(
                {
                    "channel": "my-app.alerts",
                    "source": "app:my-app",
                    "priority": "critical",
                    "title": "t",
                    "session_key": "a-surface-that-permits-everything",
                }
            )
        self.assertEqual(sink.sent, [])
        self.assertEqual([o.reason for o in outcomes], ["denied_by_governance"])

    async def test_a_producers_own_profile_is_consulted(self) -> None:
        # The other direction: a cron whose own surface denies messaging must
        # not egress on the host's permission.
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})

        def cron_denies(_scope: str, _item: str, **kw: Any) -> Any:
            return _deny() if kw.get("session_key", "").startswith("cron:") else _permit()

        with (
            mock.patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=cron_denies
            ),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            outcomes = await d.dispatch(
                {
                    "channel": "system.cron",
                    "source": "system",
                    "priority": "critical",
                    "title": "t",
                    "session_key": "cron:nightly-report",
                }
            )
        self.assertEqual(sink.sent, [])
        self.assertEqual([o.reason for o in outcomes], ["denied_by_governance"])

    async def test_a_note_with_no_claim_is_vetted_on_the_host_alone(self) -> None:
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        with (
            mock.patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit
            ) as vet,
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            await d.dispatch(
                {
                    "channel": "system.cron",
                    "source": "system",
                    "priority": "critical",
                    "title": "t",
                }
            )
        self.assertEqual({c.kwargs["session_key"] for c in vet.call_args_list}, {HOST_SESSION_KEY})

    async def test_a_forged_app_prefix_in_meta_cannot_impersonate_an_app(self) -> None:
        # source is the server-set field; a meta key spelled like it is dropped
        # by the merge, so there is no second place an app identity can come
        # from. A system note therefore carries no app bind at all.
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        with (
            mock.patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit
            ) as vet,
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            await d.dispatch(
                {
                    "channel": "system.cron",
                    "source": "system",
                    "priority": "critical",
                    "title": "t",
                    "session_key": "app:privileged-app",
                }
            )
        for call in vet.call_args_list:
            self.assertEqual(call.kwargs["app"], "")

    async def test_an_app_denied_messaging_does_not_egress(self) -> None:
        # The end the bypass reaches: a profile that denies this app must stop
        # the send, which it can only do if the leg told it which app to judge.
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"my-app.alerts": {"deliver_to": ["slack"]}})

        def deny_that_app(_scope: str, _item: str, **kw: Any) -> Any:
            return _deny() if kw.get("app") == "my-app" else _permit()

        with (
            mock.patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit",
                side_effect=deny_that_app,
            ),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            outcomes = await d.dispatch(
                {
                    "channel": "my-app.alerts",
                    "source": "app:my-app",
                    "priority": "critical",
                    "title": "t",
                    "session_key": "dashboard",
                }
            )
        self.assertEqual(sink.sent, [])
        self.assertEqual([o.reason for o in outcomes], ["denied_by_governance"])

    async def test_the_host_surface_is_what_governance_is_asked_about(self) -> None:
        # End to end for the identity, not just its default: the key that
        # reaches vet_and_audit must be the one that infers the host surface.
        from kiro_crew.sel import _infer_source

        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        with (
            mock.patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit
            ) as vet,
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            await d.dispatch(
                {
                    "channel": "system.cron",
                    "source": "system",
                    "priority": "critical",
                    "title": "t",
                }
            )
        self.assertTrue(vet.call_args_list)
        for call in vet.call_args_list:
            self.assertEqual(_infer_source(call.kwargs["session_key"]), "host")

    async def test_the_audit_row_is_attributed_by_the_server_set_source(self) -> None:
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"my-app.alerts": {"deliver_to": ["slack"]}})
        sel = mock.Mock()
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=sel),
        ):
            await d.dispatch(
                {
                    "channel": "my-app.alerts",
                    "source": "app:my-app",
                    "priority": "critical",
                    "title": "t",
                    "caller": "not-me",
                }
            )
        self.assertEqual(sel.log_api_access.call_args.kwargs["caller"], "app:my-app")

    async def test_channel_scope_is_checked_for_the_leg_being_delivered(self) -> None:
        # A per-transport gate that named the wrong transport would let a
        # denial on one channel authorize a send on another.
        sinks = {"slack": _RecordingSink("slack"), "telegram": _RecordingSink("telegram")}
        d = _dispatcher(sinks, {"system.cron": {"deliver_to": ["slack", "telegram"]}})

        def only_slack(scope: str, item: str, **_kw: Any) -> Any:
            if scope == "channels" and item != "slack":
                return _deny()
            return _permit()

        with (
            mock.patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=only_slack
            ),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            outcomes = await d.dispatch(
                {"channel": "system.cron", "priority": "critical", "title": "t"}
            )
        delivered = {o.transport: o.delivered for o in outcomes}
        self.assertEqual(delivered, {"slack": True, "telegram": False})

    async def test_disconnected_transport_is_an_audited_skip_not_an_error(self) -> None:
        d = _dispatcher({}, {"system.cron": {"deliver_to": ["slack"]}})
        sel = mock.Mock()
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=sel),
        ):
            outcomes = await d.dispatch(
                {"channel": "system.cron", "priority": "critical", "title": "t"}
            )
        self.assertEqual([o.reason for o in outcomes], ["not_connected"])
        self.assertEqual(sel.log_api_access.call_args.kwargs["outcome"], "skipped")

    async def test_one_transport_failing_does_not_suppress_another(self) -> None:
        good = _RecordingSink("telegram")
        bad = _RecordingSink("slack", fail_times=1)
        d = _dispatcher(
            {"slack": bad, "telegram": good},
            {"system.cron": {"deliver_to": ["slack", "telegram"]}},
        )
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            outcomes = await d.dispatch(
                {"channel": "system.cron", "priority": "critical", "title": "t"}
            )
        delivered = {o.transport: o.delivered for o in outcomes}
        self.assertEqual(delivered, {"slack": False, "telegram": True})
        self.assertEqual(len(good.sent), 1)

    async def test_an_ambiguous_send_failure_is_not_retried(self) -> None:
        # A send that fails after the platform accepted the message is
        # indistinguishable from one that never landed, so retrying can post
        # the same notification twice. The note is already on the dashboard, so
        # a dropped leg costs a surface while a double post costs a wrong one.
        sink = _RecordingSink("slack", fail_times=1)
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            outcomes = await d.dispatch(
                {"channel": "system.cron", "priority": "critical", "title": "t"}
            )
        self.assertFalse(outcomes[0].delivered)
        self.assertEqual(sink.attempts, 1)
        self.assertEqual(sink.sent, [])

    async def test_a_successful_send_happens_exactly_once(self) -> None:
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            await d.dispatch({"channel": "system.cron", "priority": "critical", "title": "t"})
        self.assertEqual(sink.attempts, 1)
        self.assertEqual(len(sink.sent), 1)

    async def test_a_disconnected_transport_spends_no_budget(self) -> None:
        # The budget caps what is DELIVERED. A skipped leg sends nothing, so
        # charging it would let a transport that is merely down drain the burst
        # and throttle the first real delivery after it reconnects.
        sinks: dict[str, Any] = {}
        d = _dispatcher(sinks, {"system.cron": {"deliver_to": ["slack"]}})
        note = {"channel": "system.cron", "priority": "critical", "title": "t"}
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            for _ in range(BRIDGE_BURST + 3):
                outcomes = await d.dispatch(note)
                assert outcomes[0].reason == "not_connected"
            # Slack comes back. The very next note must still be deliverable.
            sink = _RecordingSink()
            sinks["slack"] = sink
            outcomes = await d.dispatch(note)
        self.assertTrue(outcomes[0].delivered)
        self.assertEqual(len(sink.sent), 1)

    async def test_a_governance_denial_spends_no_budget(self) -> None:
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        note = {"channel": "system.cron", "priority": "critical", "title": "t"}
        with (
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_deny),
        ):
            for _ in range(BRIDGE_BURST + 3):
                await d.dispatch(note)
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            outcomes = await d.dispatch(note)
        self.assertTrue(outcomes[0].delivered)

    async def test_egress_budget_throttles_a_flood(self) -> None:
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        note = {"channel": "system.cron", "priority": "critical", "title": "t"}
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            outcomes = [(await d.dispatch(note))[0].delivered for _ in range(BRIDGE_BURST + 3)]
        self.assertEqual(outcomes.count(True), BRIDGE_BURST)
        self.assertEqual(len(sink.sent), BRIDGE_BURST)

    async def test_schedule_returns_before_the_sink_completes(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class _Slow:
            transport_id = "slack"

            async def send(self, _text: str) -> str:
                started.set()
                await release.wait()
                return "id"

        d = _dispatcher({"slack": _Slow()}, {"system.cron": {"deliver_to": ["slack"]}})
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            task = d.schedule({"channel": "system.cron", "priority": "critical", "title": "t"})
            self.assertIsNotNone(task)
            assert task is not None
            self.assertFalse(task.done())
            await asyncio.wait_for(started.wait(), timeout=2)
            release.set()
            await asyncio.wait_for(task, timeout=2)

    async def test_schedule_snapshots_the_note(self) -> None:
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        note = {"channel": "system.cron", "priority": "critical", "title": "original"}
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            task = d.schedule(note)
            note["title"] = "mutated after scheduling"
            assert task is not None
            await asyncio.wait_for(task, timeout=2)
        self.assertIn("original", sink.sent[0])

    async def test_schedule_returns_none_for_an_unrouted_note(self) -> None:
        d = _dispatcher({"slack": _RecordingSink()}, {})
        self.assertIsNone(d.schedule({"channel": "system.cron", "priority": "critical"}))

    async def test_a_raising_sink_never_escapes_the_scheduled_task(self) -> None:
        class _Exploding:
            transport_id = "slack"

            async def send(self, _text: str) -> str:
                raise BaseException("not even an Exception")  # noqa: TRY002

        d = _dispatcher({"slack": _Exploding()}, {"system.cron": {"deliver_to": ["slack"]}})
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            task = d.schedule({"channel": "system.cron", "priority": "critical", "title": "t"})
            assert task is not None
            outcomes = await asyncio.wait_for(task, timeout=2)
        self.assertEqual([o.delivered for o in outcomes], [False])

    async def test_drain_awaits_in_flight_fanout(self) -> None:
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.cron": {"deliver_to": ["slack"]}})
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            d.schedule({"channel": "system.cron", "priority": "critical", "title": "t"})
            await d.drain(timeout=3)
        self.assertEqual(len(sink.sent), 1)


class OffLoopProducerTests(unittest.IsolatedAsyncioTestCase):
    """A producer on a worker thread must still reach the transport.

    Not hypothetical: `code_review_sage/backend/routes.py` publishes its
    run-finished notice as `asyncio.to_thread(state.notify, ...)`, deliberately
    off-loop because the delivery sink writes to disk. In that thread
    `get_running_loop` raises although the gateway loop is alive, so a bridge
    that only handles the on-loop case silently loses exactly the notification a
    user routed to chat.
    """

    async def test_an_off_loop_producer_still_reaches_the_transport(self) -> None:
        loop = asyncio.get_running_loop()
        sink = _RecordingSink()
        d = BridgeDispatcher(
            sink_resolver=lambda _t: sink,
            settings_reader=lambda _c: {"deliver_to": ["slack"]},
            loop_provider=lambda: loop,
        )
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            # Genuinely another thread, so get_running_loop() raises there.
            handed = await asyncio.to_thread(
                d.schedule,
                {"channel": "system.agent", "priority": "critical", "title": "Code review ready"},
            )
            # No task for this caller to await -- it was handed to the loop.
            self.assertIsNone(handed)
            for _ in range(6):
                await asyncio.sleep(0)
                if sink.sent:
                    break
            await d.drain(timeout=5)
        self.assertEqual(len(sink.sent), 1)
        self.assertIn("Code review ready", sink.sent[0])

    async def test_an_off_loop_producer_with_no_reachable_loop_is_a_skip(self) -> None:
        sink = _RecordingSink()
        d = BridgeDispatcher(
            sink_resolver=lambda _t: sink,
            settings_reader=lambda _c: {"deliver_to": ["slack"]},
            loop_provider=lambda: None,
        )
        handed = await asyncio.to_thread(
            d.schedule, {"channel": "system.agent", "priority": "critical", "title": "t"}
        )
        self.assertIsNone(handed)
        await asyncio.sleep(0)
        self.assertEqual(sink.sent, [])

    async def test_a_closed_gateway_loop_is_a_skip_not_a_crash(self) -> None:
        dead = asyncio.new_event_loop()
        dead.close()
        sink = _RecordingSink()
        d = BridgeDispatcher(
            sink_resolver=lambda _t: sink,
            settings_reader=lambda _c: {"deliver_to": ["slack"]},
            loop_provider=lambda: dead,
        )
        handed = await asyncio.to_thread(
            d.schedule, {"channel": "system.agent", "priority": "critical", "title": "t"}
        )
        self.assertIsNone(handed)
        self.assertEqual(sink.sent, [])

    async def test_a_raising_loop_provider_is_a_skip_not_a_crash(self) -> None:
        def boom() -> Any:
            raise RuntimeError("no loop for you")

        d = BridgeDispatcher(
            sink_resolver=lambda _t: _RecordingSink(),
            settings_reader=lambda _c: {"deliver_to": ["slack"]},
            loop_provider=boom,
        )
        handed = await asyncio.to_thread(
            d.schedule, {"channel": "system.agent", "priority": "critical", "title": "t"}
        )
        self.assertIsNone(handed)

    async def test_an_unrouted_off_loop_note_never_reaches_the_loop(self) -> None:
        calls: list[int] = []
        d = BridgeDispatcher(
            sink_resolver=lambda _t: _RecordingSink(),
            settings_reader=lambda _c: {},
            loop_provider=lambda: (calls.append(1), asyncio.get_event_loop())[1],
        )
        handed = await asyncio.to_thread(
            d.schedule, {"channel": "system.agent", "priority": "critical", "title": "t"}
        )
        self.assertIsNone(handed)
        self.assertEqual(calls, [])


class SynchronousContextTests(unittest.TestCase):
    def test_schedule_with_no_loop_at_all_is_a_silent_skip(self) -> None:
        # No running loop AND no gateway loop to hand to: a CLI, a boot-time
        # note, a synchronous test. The dashboard already holds the note, so
        # dropping the bridge leg is the right degrade rather than an error to
        # raise into the producer. Distinct from a WORKER THREAD of a live
        # gateway, where the loop is reachable and dropping loses a delivery --
        # see OffLoopProducerTests.
        d = _dispatcher({"slack": _RecordingSink()}, {"system.cron": {"deliver_to": ["slack"]}})
        self.assertIsNone(
            d.schedule({"channel": "system.cron", "priority": "critical", "title": "t"})
        )


class RateLimiterTests(unittest.TestCase):
    def test_budget_is_per_transport(self) -> None:
        limiter = BridgeRateLimiter()
        for _ in range(BRIDGE_BURST):
            self.assertTrue(limiter.allow("slack"))
        self.assertFalse(limiter.allow("slack"))
        self.assertTrue(limiter.allow("telegram"))

    @staticmethod
    def _frozen_clock(start: float) -> tuple[dict[str, float], Any]:
        """A clock the test advances by hand, and the stand-in for ``bridge.time``.

        Replaces the NAME ``time`` inside the bridge module rather than patching
        the stdlib module's attribute, so no other code running in this process
        sees a rewritten clock. ``_Bucket.last_refill``'s ``default_factory``
        captured the real function when the class was defined, which does not
        matter here: ``allow`` always constructs a bucket with an explicit
        ``last_refill``, so every value the production path reads comes from here.
        """
        clock = {"now": start}
        return clock, SimpleNamespace(monotonic=lambda: clock["now"])

    def test_the_documented_refill_rate_is_what_the_bucket_does(self) -> None:
        """One token per ``window / tokens`` seconds, measured on ``allow``.

        The spec states this budget as "20 per 5 minutes, burst 5". The sibling
        test above measures the burst; nothing measured the REFILL, because
        ``allow`` reads ``time.monotonic`` directly and no test advanced a clock
        -- so both the window and the token count were prose, and either constant
        could have been changed with the whole suite still green.

        Drives the real ``allow`` against the advanced clock rather than
        recomputing the arithmetic, so it also fails if the production path stops
        consulting elapsed time at all. Each step leaves half a token of margin
        either side of the threshold: asserting exactly at it would make the test
        a float-precision coin toss rather than a measurement.
        """
        per_token = bridge_mod.BRIDGE_WINDOW_SECS / bridge_mod.BRIDGE_TOKENS_PER_WINDOW
        clock, fake = self._frozen_clock(1000.0)
        with mock.patch.object(bridge_mod, "time", fake):
            limiter = BridgeRateLimiter()
            for _ in range(BRIDGE_BURST):
                self.assertTrue(limiter.allow("slack"))
            self.assertFalse(limiter.allow("slack"), "burst was not exhausted")
            clock["now"] += per_token * 0.5
            self.assertFalse(limiter.allow("slack"), "refilled before one interval elapsed")
            clock["now"] += per_token
            self.assertTrue(limiter.allow("slack"), "did not refill after one interval")
            self.assertFalse(limiter.allow("slack"), "refilled more than the elapsed time earns")

    def test_a_long_idle_refills_only_to_the_burst_cap(self) -> None:
        """Quiet time does not accumulate past ``BRIDGE_BURST``.

        Ten windows idle earns 200 tokens at the documented rate, and the bucket
        must still release only ``BRIDGE_BURST``. Without the cap a producer
        silent for an hour could post its entire backlog in one go, which is the
        chat flood the budget exists to prevent -- so the cap, not the rate, is
        what bounds the worst case.
        """
        clock, fake = self._frozen_clock(500.0)
        with mock.patch.object(bridge_mod, "time", fake):
            limiter = BridgeRateLimiter()
            for _ in range(BRIDGE_BURST):
                self.assertTrue(limiter.allow("slack"))
            self.assertFalse(limiter.allow("slack"))
            clock["now"] += bridge_mod.BRIDGE_WINDOW_SECS * 10
            allowed = 0
            while limiter.allow("slack") and allowed <= BRIDGE_BURST + 5:
                allowed += 1
            self.assertEqual(allowed, BRIDGE_BURST)


class HostIdentityTests(unittest.TestCase):
    """The host subject must select the HOST surface, not an arbitrary one."""

    def test_the_default_host_key_is_the_shared_sentinel(self) -> None:
        from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY

        default = (
            inspect.signature(BridgeDispatcher.__init__).parameters["host_session_key"].default
        )
        self.assertEqual(default, HOST_SESSION_KEY)

    def test_the_default_host_key_infers_the_host_surface(self) -> None:
        # The value is not a label, it picks a governance profile. A bare
        # "dashboard" matches no prefix in _infer_source and falls through to
        # "slack", which would judge "may the host send to Slack" under a Slack
        # profile and skip an operator's surface:host denial.
        from kiro_crew.sel import _infer_source

        default = (
            inspect.signature(BridgeDispatcher.__init__).parameters["host_session_key"].default
        )
        self.assertEqual(_infer_source(default), "host")
        self.assertEqual(_infer_source("dashboard"), "slack")


class RealProfilePrecedenceTests(unittest.IsolatedAsyncioTestCase):
    """Governance driven through the REAL profile store, with no ``vet_and_audit``
    mock anywhere.

    Every other governance test in this file patches ``vet_and_audit``, so it can
    only assert which arguments a leg passes and is blind to what
    ``resolve_active_scope`` then DOES with them. That blindness is not
    hypothetical: the app bind outranks the surface bind, so carrying ``app=`` on
    the host lookup had a permissive app profile answer "may the host send" and
    an operator's ``surface:host`` denial was skipped entirely, while the
    argument-shape tests stayed green. Resolving real bound profiles is the only
    way to catch that class, so these three drive the store itself.
    """

    def setUp(self) -> None:
        # addCleanup, not tearDown: unittest SKIPS tearDown when setUp raises,
        # so a failure between two acquisitions here leaks whatever the earlier
        # ones took. Each cleanup is registered the moment its resource exists.
        # The PATCH is the one that matters -- it rebinds a module global, so
        # leaking it corrupts every later test in the process, while a leaked
        # tempdir is only a stale directory under TMPDIR. Registration order is
        # chosen so the LIFO run order matches the tearDown this replaces:
        # patch.stop, then reset_store, then cleanup.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._dir = Path(self._tmp.name)
        self._patch = mock.patch.object(gp, "_PROFILES_DIR", self._dir)
        self.addCleanup(gp.reset_store)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        gp.reset_store()

    def _profile(self, name: str, bind: dict[str, str], *, messaging: bool) -> None:
        (self._dir / f"{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "bind": bind,
                    "capabilities": {"messaging": {"enabled": messaging}},
                }
            )
        )

    async def _deliver(self, **extra: Any) -> _RecordingSink:
        """Dispatch one critical app note on an armed channel. Real governance."""
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"my-app.alerts": {"deliver_to": ["slack"]}})
        with mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()):
            await d.dispatch(
                {
                    "channel": "my-app.alerts",
                    "source": "app:my-app",
                    "priority": "critical",
                    "title": "t",
                    **extra,
                }
            )
        return sink

    async def _deliver_agent(self, **extra: Any) -> _RecordingSink:
        """Dispatch one critical AGENT note: ``source="system"``, no app.

        The shape the ``send_notification`` route publishes. It matters on its own
        because ``source`` names no app here, so the session subject is the ONLY
        per-producer profile the bridge can consult -- the app bind that carries
        the other tests is absent by construction.
        """
        sink = _RecordingSink()
        d = _dispatcher({"slack": sink}, {"system.agent": {"deliver_to": ["slack"]}})
        with mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()):
            await d.dispatch(
                {
                    "channel": "system.agent",
                    "source": "system",
                    "priority": "critical",
                    "title": "t",
                    **extra,
                }
            )
        return sink

    async def test_an_agents_own_surface_denial_withholds_its_note(self) -> None:
        # The gap the agent publish path left open: with no session on the note
        # only the host surface is vetted, so a permissive host let a denied
        # agent egress. The dashboard denial must bind once the note names it.
        self._profile("dash-no-dm", {"type": "surface", "id": "dashboard"}, messaging=False)
        self._profile("host-yes-dm", {"type": "surface", "id": "host"}, messaging=True)
        sink = await self._deliver_agent(session_key="dashboard:chat-7")
        self.assertEqual(sink.sent, [])

    async def test_an_agent_note_still_egresses_when_its_own_surface_permits(self) -> None:
        # Paired with the test above so the guard cannot pass by withholding
        # every agent note, which would silently disable the whole feature for
        # the producer class that uses it most.
        self._profile("dash-yes-dm", {"type": "surface", "id": "dashboard"}, messaging=True)
        self._profile("host-yes-dm", {"type": "surface", "id": "host"}, messaging=True)
        sink = await self._deliver_agent(session_key="dashboard:chat-7")
        self.assertEqual(len(sink.sent), 1)

    async def test_a_permissive_app_profile_cannot_lift_the_host_denial(self) -> None:
        # The operator's ceiling. `surface:host` denies messaging; the app's own
        # profile permits it. The host denial must still bind, which it only does
        # if the host lookup resolves the HOST profile rather than the app's.
        self._profile("host-no-dm", {"type": "surface", "id": "host"}, messaging=False)
        self._profile("app-yes-dm", {"type": "app", "id": "my-app"}, messaging=True)
        self.assertEqual((await self._deliver()).sent, [])

    async def test_a_denying_app_profile_still_denies_under_a_permissive_host(self) -> None:
        # The other direction, which is the guarantee the app bind was added for.
        # Separating the lookups must not cost it.
        self._profile("host-yes-dm", {"type": "surface", "id": "host"}, messaging=True)
        self._profile("app-no-dm", {"type": "app", "id": "my-app"}, messaging=False)
        self.assertEqual((await self._deliver()).sent, [])

    async def test_both_permitting_does_egress(self) -> None:
        # Paired with the two above on purpose: without it, a guard that refused
        # every note would satisfy them and look correct.
        self._profile("host-yes-dm", {"type": "surface", "id": "host"}, messaging=True)
        self._profile("app-yes-dm", {"type": "app", "id": "my-app"}, messaging=True)
        self.assertEqual(len((await self._deliver()).sent), 1)

    async def test_a_bare_slot_claim_is_judged_on_the_dashboard_surface(self) -> None:
        # A bare slot id carries no prefix _infer_source recognises, so it falls
        # through to `slack` and the claim would be judged under a Slack profile.
        # Qualifying it is what makes the claim subject mean the producer.
        self._profile("dash-no-dm", {"type": "surface", "id": "dashboard"}, messaging=False)
        self._profile("host-yes-dm", {"type": "surface", "id": "host"}, messaging=True)
        self.assertEqual((await self._deliver(slot="chat-1")).sent, [])

    async def test_a_slack_denial_does_not_withhold_a_dashboard_slots_note(self) -> None:
        # The direction the adjudication did not weigh, and the one a user would
        # notice. Because the claim can only narrow, judging a dashboard slot's
        # note under a Slack profile does not leak -- it WITHHOLDS a delivery
        # that should have gone out.
        self._profile("slack-no-dm", {"type": "surface", "id": "slack"}, messaging=False)
        self._profile("host-yes-dm", {"type": "surface", "id": "host"}, messaging=True)
        self._profile("dash-yes-dm", {"type": "surface", "id": "dashboard"}, messaging=True)
        self.assertEqual(len((await self._deliver(slot="chat-1")).sent), 1)

    async def test_an_already_qualified_slot_is_not_prefixed_twice(self) -> None:
        # A producer that wrote a full key into `slot` must not become
        # "dashboard:dashboard:chat-1". That would name a session that does not
        # exist, and would only infer `dashboard` by luck of the leading prefix.
        # Judged here by its real surface: a cron key must reach the cron denial.
        self._profile("cron-no-dm", {"type": "surface", "id": "cron"}, messaging=False)
        self._profile("host-yes-dm", {"type": "surface", "id": "host"}, messaging=True)
        self._profile("dash-yes-dm", {"type": "surface", "id": "dashboard"}, messaging=True)
        self.assertEqual((await self._deliver(slot="cron:job-7:run-1")).sent, [])


class LoopSafetyTests(unittest.TestCase):
    """The dispatcher must have no path that publishes to the bus."""

    # Names that would give this module a way to publish: the bus classes, the
    # bus instance the dashboard holds, and the two publishing calls.
    _FORBIDDEN_NAMES = frozenset(
        {"NotificationBus", "NotificationPayload", "notification_bus", "payload_from_legacy"}
    )
    _FORBIDDEN_CALLS = frozenset({"push", "notify"})

    def _tree(self) -> ast.Module:
        source = Path(inspect.getsourcefile(bridge_mod) or "").read_text(encoding="utf-8")
        return ast.parse(source)

    def test_bridge_code_never_names_the_bus(self) -> None:
        # AST rather than a text scan on purpose: a text scan also matches the
        # module's own prose, so it would either fail on a docstring that
        # merely MENTIONS the bus or be weakened to stop doing so. The AST sees
        # code only, which is what the invariant is about.
        offenders = sorted(
            {
                node.id if isinstance(node, ast.Name) else node.attr
                for node in ast.walk(self._tree())
                if isinstance(node, (ast.Name, ast.Attribute))
                and (node.id if isinstance(node, ast.Name) else node.attr) in self._FORBIDDEN_NAMES
            }
        )
        self.assertEqual(offenders, [], f"bridge.py must not reach the bus: {offenders}")

    def test_bridge_code_never_calls_a_publishing_method(self) -> None:
        offenders = sorted(
            {
                node.func.attr
                for node in ast.walk(self._tree())
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in self._FORBIDDEN_CALLS
            }
        )
        self.assertEqual(offenders, [], f"bridge.py must not publish: {offenders}")

    def test_bridge_imports_nothing_from_the_bus_module(self) -> None:
        imported: list[str] = []
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                "notifications.bus"
            ):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.extend(
                    alias.name for alias in node.names if alias.name.endswith("notifications.bus")
                )
        self.assertEqual(imported, [])

    def test_the_forbidden_scan_would_catch_a_real_publish(self) -> None:
        # Guards the guard: a scan anchored on names absent from the codebase
        # would pass on a module that does publish.
        planted = ast.parse("state.notification_bus.push(payload)\n")
        named = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(planted)
            if isinstance(node, (ast.Name, ast.Attribute))
        }
        self.assertTrue(named & self._FORBIDDEN_NAMES)
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in self._FORBIDDEN_CALLS
                for node in ast.walk(planted)
            )
        )

    def test_dispatcher_collaborators_cannot_publish(self) -> None:
        # Constructor-level guarantee to match the source-level one: the
        # injected collaborators are a sink resolver, a settings reader and a
        # loop provider. None of them can publish a note.
        params = set(inspect.signature(BridgeDispatcher.__init__).parameters)
        self.assertEqual(
            params,
            {
                "self",
                "sink_resolver",
                "settings_reader",
                "loop_provider",
                "rate_limiter",
                "host_session_key",
            },
        )


class SettingsPersistenceTests(unittest.TestCase):
    """The routing rule round-trips through ChannelSettings and its file."""

    def setUp(self) -> None:
        self._tmp = self.enterContext(  # type: ignore[attr-defined]
            mock.patch("kiro_crew.notifications.settings.config_dir")
        )
        import tempfile

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._tmp.return_value = Path(self._dir.name)

    def _settings(self) -> Any:
        from kiro_crew.notifications.settings import ChannelSettings

        return ChannelSettings()

    def _stored(self) -> dict[str, Any]:
        path = Path(self._dir.name) / "notification_settings.json"
        return json.loads(path.read_text(encoding="utf-8"))["channel_settings"]

    def test_arming_a_route_writes_the_default_floor(self) -> None:
        entry = self._settings().update("system.cron", deliver_to=["slack"])
        self.assertEqual(entry["deliver_to"], ["slack"])
        self.assertEqual(entry["deliver_min_priority"], "critical")
        self.assertEqual(self._stored()["system.cron"]["deliver_to"], ["slack"])

    def test_an_explicit_floor_is_kept(self) -> None:
        entry = self._settings().update(
            "system.cron", deliver_to=["slack"], deliver_min_priority="all"
        )
        self.assertEqual(entry["deliver_min_priority"], "all")

    def test_an_unknown_transport_is_refused_and_nothing_is_written(self) -> None:
        from kiro_crew.notifications.settings import ChannelSettingsError

        settings = self._settings()
        with self.assertRaises(ChannelSettingsError):
            settings.update("system.cron", deliver_to=["pigeon"])
        self.assertEqual(settings.get("system.cron"), {})

    def test_an_unknown_floor_is_refused(self) -> None:
        from kiro_crew.notifications.settings import ChannelSettingsError

        with self.assertRaises(ChannelSettingsError):
            self._settings().update(
                "system.cron", deliver_to=["slack"], deliver_min_priority="whenever"
            )

    def test_emptying_the_route_drops_its_floor_too(self) -> None:
        settings = self._settings()
        settings.update("system.cron", deliver_to=["slack"], deliver_min_priority="all")
        entry = settings.update("system.cron", deliver_to=[])
        self.assertNotIn("deliver_to", entry)
        self.assertNotIn("deliver_min_priority", entry)

    def test_clear_delivery_drops_both_keys(self) -> None:
        settings = self._settings()
        settings.update("system.cron", deliver_to=["slack"])
        entry = settings.update("system.cron", clear_delivery=True)
        self.assertNotIn("deliver_to", entry)
        self.assertNotIn("deliver_min_priority", entry)

    def test_a_route_does_not_disturb_mute_or_priority(self) -> None:
        settings = self._settings()
        settings.update("system.cron", muted=True, priority="passive")
        entry = settings.update("system.cron", deliver_to=["slack"])
        self.assertTrue(entry["muted"])
        self.assertEqual(entry["priority"], "passive")
        self.assertEqual(entry["deliver_to"], ["slack"])

    def test_a_protected_channel_may_still_be_routed(self) -> None:
        # Protection is about attention (mute, priority), not about egress.
        entry = self._settings().update("system.approval", deliver_to=["slack"])
        self.assertEqual(entry["deliver_to"], ["slack"])

    def test_apply_does_not_read_or_write_the_routing_rule(self) -> None:
        # apply() mutates the note every sink sees; routing is one sink's
        # decision about a note it must not change.
        settings = self._settings()
        settings.update("system.cron", deliver_to=["slack"])
        note = {"channel": "system.cron", "priority": "default"}
        settings.apply(note)
        self.assertNotIn("deliver_to", note)
        self.assertNotIn("deliver_min_priority", note)
        self.assertEqual(note["priority"], "default")

    def test_a_stored_rule_reads_back_as_a_dispatcher_route(self) -> None:
        settings = self._settings()
        settings.update("system.cron", deliver_to=["slack", "telegram"])
        d = BridgeDispatcher(sink_resolver=lambda _t: None, settings_reader=settings.get)
        self.assertEqual(
            d.routes({"channel": "system.cron", "priority": "critical"}),
            ("slack", "telegram"),
        )


class SlackSinkTests(unittest.IsolatedAsyncioTestCase):
    def _state(self, connected: bool = True, owner: str = "U1") -> Any:
        state = mock.Mock()
        state.slack_client = mock.Mock()
        state.owner_id = owner
        state.slack_socket_connected = connected
        return state

    def test_no_sink_without_a_client(self) -> None:
        from kiro_crew.slack.notification_sink import slack_sink_for

        state = self._state()
        state.slack_client = None
        self.assertIsNone(slack_sink_for(state))

    def test_no_sink_without_an_owner(self) -> None:
        from kiro_crew.slack.notification_sink import slack_sink_for

        self.assertIsNone(slack_sink_for(self._state(owner="")))

    def test_no_sink_while_the_socket_is_down(self) -> None:
        from kiro_crew.slack.notification_sink import slack_sink_for

        self.assertIsNone(slack_sink_for(self._state(connected=False)))

    async def test_send_opens_the_owner_dm_and_posts(self) -> None:
        from kiro_crew.slack.notification_sink import SlackBridgeSink

        client = mock.Mock()
        client.open_dm = mock.AsyncMock(return_value="D1")
        client.post_message = mock.AsyncMock(return_value="ts-1")
        sink = SlackBridgeSink(client, "U1")
        message_id = await sink.send("hello")
        client.open_dm.assert_awaited_once_with("U1")
        self.assertEqual(client.post_message.await_args.args[0], "D1")
        self.assertEqual(message_id, "ts-1")

    async def test_send_raises_when_the_dm_cannot_be_opened(self) -> None:
        from kiro_crew.slack.notification_sink import SlackBridgeSink

        client = mock.Mock()
        client.open_dm = mock.AsyncMock(return_value=None)
        client.post_message = mock.AsyncMock()
        with self.assertRaises(RuntimeError):
            await SlackBridgeSink(client, "U1").send("hello")
        client.post_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
