"""Tests for the Slack pin board.

The load-bearing properties, in order of what would hurt most if broken:

1. **No credential of our own.** The app must never grow a Slack token field; it
   reuses Kiro Crew's client. A test asserts the secret store stays untouched,
   because "just add a token field" is the obvious future regression.
2. **A Slack outage cannot break ops.** Every send swallows its exception. If this
   regresses, a Slack blip stops incident claiming.
3. **Board, not feed.** One message per incident, edited in place.
4. **Outbound text is redacted.** Provider payloads reach a wider audience here.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from kiro_crew.apps.builtins.ops_mission_control.backend import slack_out, store
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    STATUS_NEEDS_HUMAN,
    STATUS_RESOLVED,
    Incident,
    Signal,
)


class _FakeSlack:
    """Records calls the way SlackClientOps would receive them."""

    def __init__(self, *, fail_update: bool = False, fail_post: bool = False) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.threads: list[dict[str, Any]] = []
        self._fail_update = fail_update
        self._fail_post = fail_post
        self._ts = 0

    async def post_blocks(
        self, channel: str, blocks: list[dict], text: str, thread_ts: str | None = None
    ) -> str:
        if self._fail_post:
            raise RuntimeError("slack is down")
        self.posts.append({"channel": channel, "blocks": blocks, "text": text})
        self._ts += 1
        return f"171000.{self._ts}"

    async def update_message(
        self, channel: str, ts: str, text: str = "", blocks: list[dict] | None = None
    ) -> None:
        if self._fail_update:
            raise RuntimeError("message_not_found")
        self.updates.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})

    async def post_message(self, channel: str, text: str, thread_ts: str | None = None) -> str:
        self.threads.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return "171000.99"


def _signal(**kw: Any) -> Signal:
    base = {
        "id": "sig-1",
        "source": "cloudwatch",
        "title": "DLQ depth above threshold",
        "resource": "arn:aws:sqs:us-west-2:1234:my-dlq",
        "severity": "warning",
        "state": "firing",
    }
    base.update(kw)
    return Signal.from_dict(base)


class _HomeIsolated(unittest.IsolatedAsyncioTestCase):
    """Every test gets its own data home — never the operator's real config.

    IsolatedAsyncioTestCase rather than bare asyncio.run calls: the
    subprocess-spawn audit (test/test_spawn_audit.py) scans for asyncio.<spawn
    attr> across the package, and asyncio.run trips it — four hits from this file
    alone. Same convention the other async tests here already document.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self._tmp.name

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        self._tmp.cleanup()


class TestConfiguration(_HomeIsolated):
    def test_off_by_default(self) -> None:
        """A fresh install must not post anywhere."""
        self.assertFalse(slack_out.configured())

    def test_needs_both_toggle_and_channel(self) -> None:
        slack_out.set_settings(enabled=True)
        self.assertFalse(slack_out.configured(), "enabled with no channel is not configured")
        slack_out.set_settings(channel_id="C0123456789")
        self.assertTrue(slack_out.configured())

    def test_enabled_without_channel_says_which_half_is_missing(self) -> None:
        slack_out.set_settings(enabled=True)
        self.assertIn("channel", slack_out.status()["detail"].lower())

    def test_status_distinguishes_missing_host_slack(self) -> None:
        """The unfixable-by-this-app case must name the real fix."""
        slack_out.set_settings(enabled=True, channel_id="C1")
        status = slack_out.status(None)
        self.assertFalse(status["ready"])
        self.assertFalse(status["slack_available"])
        self.assertIn("Slack", status["detail"])

    def test_channel_id_is_not_written_to_the_secret_store(self) -> None:
        """A channel ID is not a credential; it belongs in plain config."""
        slack_out.set_settings(enabled=True, channel_id="C0123456789")
        secrets = Path(self._tmp.name) / "ops_mission_control_secrets.json"
        self.assertFalse(secrets.exists(), "Slack output must add no secret material")


class TestNoTokenOfItsOwn(_HomeIsolated):
    """The design invariant most likely to be 'helpfully' regressed later."""

    def test_module_defines_no_token_config_key(self) -> None:
        source = Path(slack_out.__file__).read_text(encoding="utf-8")
        # Look for a config KEY named for a token, not the prose explaining why
        # we have none (the docstring legitimately says "bot token").
        for banned in ('"slack_token"', "'slack_token'", '"bot_token"', "'bot_token'"):
            self.assertNotIn(
                banned,
                source,
                "Slack output must reuse Kiro Crew's client, never store its own token",
            )

    def test_reads_the_client_off_gateway_state(self) -> None:
        state = mock.Mock()
        state.slack_client = _FakeSlack()
        self.assertIs(slack_out.client_from_state(state), state.slack_client)

    def test_no_state_means_no_client(self) -> None:
        """A CLI/test process has no gateway; that must be a quiet no-op."""
        self.assertIsNone(slack_out.client_from_state(None))

    def test_state_without_slack_means_no_client(self) -> None:
        """Gateway up but Slack unconfigured — the common case."""

        class _Bare:
            pass

        self.assertIsNone(slack_out.client_from_state(_Bare()))


class TestPublish(_HomeIsolated):
    # Annotated non-optional: the assert below proves it, but a per-method
    # narrowing does not survive into the other test methods.
    incident: Incident

    def setUp(self) -> None:
        super().setUp()
        slack_out.set_settings(enabled=True, channel_id="C0123456789")
        claimed = store.claim(_signal(), operating_mode="observe")
        assert claimed is not None
        self.incident = claimed

    async def _publish(self, fake: _FakeSlack, incident: Incident | None = None) -> bool:
        return await slack_out.publish(incident or self.incident, fake)

    async def test_first_publish_posts_and_records_ts(self) -> None:
        fake = _FakeSlack()
        self.assertTrue(await self._publish(fake))
        self.assertEqual(len(fake.posts), 1)
        reread = store.get_incident(self.incident.incident_id)
        assert reread is not None
        self.assertTrue(reread.slack_thread_ts, "ts must be recorded so the next send edits")

    async def test_second_publish_edits_in_place(self) -> None:
        """Board, not feed: the same incident must occupy ONE message."""
        fake = _FakeSlack()
        await self._publish(fake)
        updated = store.transition(self.incident.incident_id, STATUS_NEEDS_HUMAN)
        await self._publish(fake, updated)
        self.assertEqual(len(fake.posts), 1, "must not post a second message")
        self.assertEqual(len(fake.updates), 1)

    async def test_lost_ts_reposts_rather_than_going_silent(self) -> None:
        """A deleted message must not silence the incident forever."""
        fake = _FakeSlack(fail_update=True)
        store.update_fields(self.incident.incident_id, slack_thread_ts="171000.dead")
        stale = store.get_incident(self.incident.incident_id)
        assert stale is not None
        self.assertTrue(await self._publish(fake, stale))
        self.assertEqual(len(fake.posts), 1, "falls back to a fresh post")

    async def test_post_failure_is_swallowed(self) -> None:
        """A Slack outage must never fail the caller's dispatch cycle."""
        fake = _FakeSlack(fail_post=True)
        self.assertFalse(await self._publish(fake))

    async def test_no_client_publishes_nothing(self) -> None:
        """Slack not configured on Kiro Crew itself: quiet no-op, never a crash."""
        self.assertFalse(await slack_out.publish(self.incident, None))

    async def test_disabled_publishes_nothing(self) -> None:
        slack_out.set_settings(enabled=False)
        fake = _FakeSlack()
        self.assertFalse(await self._publish(fake))
        self.assertEqual(fake.posts, [])

    def test_status_emoji_tracks_state(self) -> None:
        resolved = store.transition(self.incident.incident_id, STATUS_NEEDS_HUMAN)
        self.assertIn("🧑", slack_out.summary_line(resolved))
        resolved = store.transition(self.incident.incident_id, STATUS_RESOLVED)
        self.assertIn("✅", slack_out.summary_line(resolved))

    def test_blocked_reason_beats_bare_status(self) -> None:
        """'Needs human' alone does not tell the reader what to do."""
        store.update_fields(self.incident.incident_id, blocked_reason="awaiting_approval")
        blocked = store.transition(self.incident.incident_id, STATUS_NEEDS_HUMAN)
        self.assertIn("awaiting approval", slack_out.summary_line(blocked))

    def test_long_title_is_clipped(self) -> None:
        incident = store.claim(_signal(id="sig-2", title="X" * 500), operating_mode="observe")
        assert incident is not None
        self.assertLess(len(slack_out.summary_line(incident)), 400)


class TestRedaction(_HomeIsolated):
    def test_credential_in_title_is_not_republished(self) -> None:
        """Provider text reaches a wider audience in Slack than in the dashboard."""
        slack_out.set_settings(enabled=True, channel_id="C1")
        leaky = _signal(id="sig-leak", title="auth failed for AKIAIOSFODNN7EXAMPLE")
        incident = store.claim(leaky, operating_mode="observe")
        assert incident is not None
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", slack_out.summary_line(incident))

    async def test_thread_detail_is_redacted(self) -> None:
        slack_out.set_settings(enabled=True, channel_id="C1")
        incident = store.claim(_signal(id="sig-d"), operating_mode="observe")
        assert incident is not None
        store.update_fields(incident.incident_id, slack_thread_ts="171000.1")
        with_ts = store.get_incident(incident.incident_id)
        assert with_ts is not None
        fake = _FakeSlack()
        await slack_out.post_detail(with_ts, "root cause: key AKIAIOSFODNN7EXAMPLE expired", fake)
        self.assertEqual(len(fake.threads), 1)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", fake.threads[0]["text"])

    def test_a_provider_token_in_the_title_is_masked_too(self) -> None:
        """The gap the AKIA cases above could not catch.

        Slack-bound text passed through `security.redact` only, which knows AWS keys and
        exfiltration URLs but NOT the provider token shapes this app handles. Measured: a
        Datadog app-key shape and a PagerDuty `u+` token both survive `redact` completely
        unchanged and are masked only by `secrets.redact_tokens` — whose own docstring
        already listed Slack as one of its sinks, and Slack was the one sink not wired to
        it. A provider-authored alarm title carrying a key was therefore republished into
        the channel verbatim. Found in review.
        """
        from kiro_crew.security import redact

        slack_out.set_settings(enabled=True, channel_id="C1")
        for i, token in enumerate(
            (
                "abcdef0123456789abcdef0123456789abcdef01",  # Datadog app-key shape
                "u+AbCdEfGhIjKlMnOpQrSt",  # PagerDuty token shape
            )
        ):
            # Guard the premise: if `redact` ever learns these, this test is measuring
            # nothing and should be told so rather than passing quietly.
            self.assertIn(token, redact(f"x {token}"), "premise: core redact misses this")

            leaky = _signal(id=f"sig-tok-{i}", title=f"alert from key {token}")
            incident = store.claim(leaky, operating_mode="observe")
            assert incident is not None
            self.assertNotIn(token, slack_out.summary_line(incident))

    async def test_a_provider_token_in_thread_detail_is_masked(self) -> None:
        from kiro_crew.security import redact

        token = "abcdef0123456789abcdef0123456789abcdef01"
        self.assertIn(token, redact(f"x {token}"), "premise: core redact misses this")

        slack_out.set_settings(enabled=True, channel_id="C1")
        incident = store.claim(_signal(id="sig-td"), operating_mode="observe")
        assert incident is not None
        store.update_fields(incident.incident_id, slack_thread_ts="171000.2")
        with_ts = store.get_incident(incident.incident_id)
        assert with_ts is not None
        fake = _FakeSlack()
        await slack_out.post_detail(with_ts, f"root cause: dd key {token}", fake)
        self.assertNotIn(token, fake.threads[0]["text"])

    def test_every_outbound_field_goes_through_the_one_chokepoint(self) -> None:
        """One function, so the next field added inherits the floor.

        Three call sites each remembering two redaction passes is three chances to forget
        the second one — which is how this bug happened.
        """
        import inspect
        import re

        source = inspect.getsource(slack_out)
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        bare = [
            line.strip()
            for line in code.splitlines()
            if "redact(" in line and "_safe" not in line and "def _safe" not in line
        ]
        # TWO permitted `redact(` calls, one per position: `_safe` for message TEXT (which
        # also mrkdwn-escapes) and `_safe_link_target` for the target half of `<target|label>`
        # (which must NOT escape, and validates the scheme instead). Both are chokepoints; a
        # third bare call is the thing this guard exists to catch.
        self.assertEqual(
            len(bare),
            2,
            f"Slack-bound text must go through _safe(), not redact() directly: {bare}",
        )
        self.assertIn("redact_tokens(redact(", bare[0])

        # ...and every provider-derived FIELD must actually use it. The check above only
        # proved no bare `redact(` remained, which is why `signal.url` — interpolated
        # directly into a Slack block, and able to carry a token in a signed query string —
        # slipped through when `_safe` was introduced for the other three. Assert on the
        # fields, not on the absence of the old call.
        # Either chokepoint counts: `_safe(` for text, `_safe_link_target(` for a URL in the
        # target half of `<target|label>`, where `_safe`'s escaping would corrupt the link.
        unsafe = [
            line.strip()
            for line in code.splitlines()
            if re.search(r"incident\.signal\.(title|resource|url|diagnosis)", line)
            and "_safe(" not in line
            and "_safe_link_target(" not in line
            and "if incident.signal." not in line  # a bare truthiness guard reveals nothing
        ]
        self.assertEqual(
            unsafe,
            [],
            f"every provider-derived field reaching Slack must go through _safe(): {unsafe}",
        )

    async def test_detail_needs_an_existing_message(self) -> None:
        """A thread reply with no parent would be an orphan post."""
        slack_out.set_settings(enabled=True, channel_id="C1")
        incident = store.claim(_signal(id="sig-o"), operating_mode="observe")
        assert incident is not None
        fake = _FakeSlack()
        self.assertFalse(await slack_out.post_detail(incident, "detail", fake))
        self.assertEqual(fake.threads, [])


class TestBlockShape(_HomeIsolated):
    def test_blocks_are_slack_valid_json(self) -> None:
        slack_out.set_settings(enabled=True, channel_id="C1")
        incident = store.claim(_signal(), operating_mode="observe")
        assert incident is not None
        blocks = slack_out._blocks(incident)
        json.dumps(blocks)  # must serialize
        self.assertEqual(blocks[0]["type"], "section")
        self.assertTrue(all("type" in b for b in blocks))

    def test_provider_url_is_linked_when_present(self) -> None:
        slack_out.set_settings(enabled=True, channel_id="C1")
        incident = store.claim(
            _signal(id="sig-u", url="https://example.com/alarm"), operating_mode="observe"
        )
        assert incident is not None
        rendered = json.dumps(slack_out._blocks(incident))
        self.assertIn("https://example.com/alarm", rendered)


class _FakeState:
    """The slice of DashboardState the reply link touches."""

    def __init__(self, *, slots: set[str] | None = None, raises: bool = False) -> None:
        self.links: list[tuple[str, str, str]] = []
        self._slots = slots if slots is not None else set()
        self._raises = raises

    def get_slot(self, name: str) -> object | None:
        return object() if name in self._slots else None

    def link_slack(self, slot_name: str, thread_ts: str, channel_id: str) -> None:
        if self._raises:
            raise RuntimeError("host refused")
        self.links.append((slot_name, thread_ts, channel_id))


class TestBoardThreadIsReplyable(_HomeIsolated):
    """A reply into the board thread must reach the investigation.

    The app recorded ``slack_thread_ts`` on its own incident record only. Inbound Slack
    routing resolves a thread through the HOST's session map, which nothing here
    populated — so a reply resolved to no session and was dropped **silently**, with no
    error and no ephemeral, while the store listing advertised replyable threads. A
    silent drop on the operator's most natural action is worse than having no Slack.
    """

    def setUp(self) -> None:
        super().setUp()
        slack_out.set_settings(enabled=True, channel_id="C0123456789")
        claimed = store.claim(_signal(), operating_mode="observe")
        assert claimed is not None
        self.incident = claimed

    def _with_ts(self, slot_key: str = "") -> Incident:
        updates: dict[str, Any] = {"slack_thread_ts": "171000.5"}
        if slot_key:
            updates["slot_key"] = slot_key
        return store.update_fields(self.incident.incident_id, **updates)

    def test_the_thread_is_registered_against_the_incidents_slot(self) -> None:
        slot = f"ops-mission-control-{self.incident.incident_id}"
        state = _FakeState(slots={slot})
        self.assertTrue(slack_out.link_thread_to_investigation(self._with_ts(), state))
        self.assertEqual(state.links, [(slot, "171000.5", "C0123456789")])

    def test_an_explicit_slot_key_is_honoured_over_the_convention(self) -> None:
        state = _FakeState(slots={"custom-slot"})
        incident = self._with_ts(slot_key="custom-slot")
        self.assertTrue(slack_out.link_thread_to_investigation(incident, state))
        self.assertEqual(state.links[0][0], "custom-slot")

    def test_no_thread_yet_is_not_a_link(self) -> None:
        """Nothing to register before the board message exists."""
        state = _FakeState(slots={f"ops-mission-control-{self.incident.incident_id}"})
        self.assertFalse(slack_out.link_thread_to_investigation(self.incident, state))
        self.assertEqual(state.links, [])

    def test_a_missing_slot_reports_honestly_rather_than_claiming_success(self) -> None:
        """The slot is created after the claim, so an early call legitimately misses.

        It must return False rather than True: the caller reports this to the agent as
        ``slack_thread_replyable``, and a false positive would tell an operator their
        reply will land when it will not.
        """
        state = _FakeState(slots=set())
        self.assertFalse(slack_out.link_thread_to_investigation(self._with_ts(), state))
        self.assertEqual(state.links, [])

    def test_a_host_without_the_method_is_a_no_op(self) -> None:
        self.assertFalse(slack_out.link_thread_to_investigation(self._with_ts(), object()))

    def test_no_state_is_a_no_op(self) -> None:
        self.assertFalse(slack_out.link_thread_to_investigation(self._with_ts(), None))

    def test_a_host_failure_is_never_fatal(self) -> None:
        """Same posture as every other send here: Slack trouble cannot break dispatch."""
        slot = f"ops-mission-control-{self.incident.incident_id}"
        state = _FakeState(slots={slot}, raises=True)
        self.assertFalse(slack_out.link_thread_to_investigation(self._with_ts(), state))

    def test_linking_is_skipped_when_slack_output_is_off(self) -> None:
        slack_out.set_settings(enabled=False)
        slot = f"ops-mission-control-{self.incident.incident_id}"
        state = _FakeState(slots={slot})
        self.assertFalse(slack_out.link_thread_to_investigation(self._with_ts(), state))


if __name__ == "__main__":
    unittest.main()


class TestSlackMrkdwnInjection(unittest.TestCase):
    """Provider text must not be able to forge Slack markup.

    Every string reaching `_safe` is content this app does not control — an alarm name, a
    GitHub issue title, an HMAC-signed webhook body — rendered into a Slack message as mrkdwn.
    A title of `<https://attacker.example|runbook>` painted an attacker-chosen hyperlink into
    the team's incident channel, labelled however the attacker liked. Redaction does not help:
    the payload holds no credential. Found in review.
    """

    def test_a_forged_link_in_provider_text_is_neutralised(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import slack_out

        out = slack_out._safe("<https://attacker.example|runbook>")
        self.assertNotIn("<https://attacker.example|", out)
        self.assertIn("&lt;", out)
        self.assertIn("&gt;", out)

    def test_ampersand_is_escaped_first(self):
        """`&` before `<`/`>`, or the ampersands those introduce get double-escaped."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import slack_out

        self.assertEqual(slack_out._safe("a & b"), "a &amp; b")
        self.assertEqual(slack_out._safe("<x>"), "&lt;x&gt;")
        self.assertNotIn("&amp;lt;", slack_out._safe("<x>"))

    def test_ordinary_copy_is_untouched(self):
        """Escaping must not degrade every normal title. `*`/`_`/backtick are deliberately
        NOT escaped: they only affect emphasis and cannot fabricate a link or mention."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import slack_out

        for plain in ("DLQ depth exceeded", "worker_pool saturated", "p99 > SLO"):
            out = slack_out._safe(plain)
            self.assertEqual(out, plain.replace(">", "&gt;"))

    def test_redaction_still_runs(self):
        """Escaping is additional to the credential floor, not a replacement."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import slack_out

        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", slack_out._safe("key AKIAIOSFODNN7EXAMPLE"))


class TestSlackLinkTargetIsValidated(unittest.TestCase):
    """The TARGET half of `<target|label>` needs the opposite treatment from message text.

    `_safe`'s `<`/`>` escaping would corrupt a URL while doing nothing about the characters
    that actually end a link target (`|`, `>`) or about a hostile scheme.
    """

    def test_a_legitimate_provider_url_survives(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import slack_out

        url = "https://example.com/console/alarm?id=1&x=2"
        self.assertEqual(slack_out._safe_link_target(url), url)

    def test_a_non_http_scheme_is_refused(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import slack_out

        for hostile in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd"):
            self.assertEqual(slack_out._safe_link_target(hostile), "")

    def test_a_link_breakout_is_refused(self):
        """`https://x|label> <https://attacker.example` would close our own link and open
        another one the attacker controls."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import slack_out

        self.assertEqual(
            slack_out._safe_link_target("https://x|label> <https://attacker.example"), ""
        )
        self.assertEqual(slack_out._safe_link_target("https://x y"), "")

    def test_a_token_in_the_query_string_is_redacted(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import slack_out

        out = slack_out._safe_link_target("https://example.com/x?key=AKIAIOSFODNN7EXAMPLE")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)

    def test_the_block_omits_the_link_rather_than_rendering_it_broken(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, slack_out

        signal = models.Signal.create(
            source="webhook", native_id="a", title="t", url="javascript:alert(1)"
        )
        inc = models.Incident(incident_id="INV-1", signal=signal, status=models.STATUS_DISPATCHED)
        rendered = json.dumps(slack_out._blocks(inc))
        self.assertNotIn("javascript:", rendered)
        self.assertNotIn("open in provider", rendered)
