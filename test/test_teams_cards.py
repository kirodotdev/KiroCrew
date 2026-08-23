"""Adaptive Card approvals: the interactive surface, and its fail-closed edges.

A card submit is CLIENT-SUPPLIED input on the one publicly reachable route in the
product, so most of what matters here is what the code refuses. The two properties
these tests exist to hold are: a stale or forged card can never approve a tool,
and a click that does nothing tells the user rather than looking like it worked.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.messaging.renderer import new_approval_nonce
from kiro_crew.safety_override import safety_override
from kiro_crew.teams.approvals import TeamsApprovalDecider
from kiro_crew.teams.cards import (
    DECISION_APPROVE,
    DECISION_DENY,
    DECISION_TRUST,
    KIND_APPROVAL,
    KIND_OPTION,
    approval_card,
    options_card,
    parse_submit,
    resolved_card,
)
from kiro_crew.teams.client import TeamsInbound
from kiro_crew.teams.transport_dispatch import TeamsDispatcher

_SVC = "https://smba.trafficmanager.net/teams"
_EMAIL = "me@example.com"


@pytest.fixture(autouse=True)
def _clean_registry():
    """The decider registry and the shared grant are process-global; isolate each."""
    TeamsApprovalDecider.reset_for_tests()
    safety_override().deactivate("test-setup")
    yield
    TeamsApprovalDecider.reset_for_tests()
    safety_override().deactivate("test-teardown")


class TestCardShape:
    def test_the_approval_card_offers_approve_auto_approve_and_deny(self) -> None:
        card = approval_card(title="fs_read", purpose="read a file", request_id="7", nonce="n1")
        actions = card["content"]["actions"]

        # The middle button says what it actually does: it arms the ONE process-wide
        # grant, so its blast radius is every surface until that expires. "Trust
        # session" would promise a narrower scope than Teams has.
        assert [a["title"] for a in actions] == ["Approve", "Approve + auto-approve", "Deny"]
        assert all(a["type"] == "Action.Submit" for a in actions), (
            "Action.Execute arrives as an invoke needing a synchronous response, "
            "which the fast-ack ingress cannot give"
        )
        # Every action carries the request id AND the nonce, so a click is
        # attributable to one prompt of one run.
        for action in actions:
            assert action["data"]["rid"] == "7"
            assert action["data"]["nonce"] == "n1"
            assert action["data"]["kc"] == KIND_APPROVAL

    def test_a_settled_card_has_no_actions_left(self) -> None:
        """An answered prompt must stop looking clickable."""
        card = resolved_card(title="fs_read", outcome="approved")
        assert card["content"]["actions"] == []

    def test_option_chips_carry_their_index_and_label(self) -> None:
        card = options_card(prompt="pick", options=["yes", "no"], nonce="n2")
        actions = card["content"]["actions"]
        assert [a["title"] for a in actions] == ["yes", "no"]
        assert [a["data"]["index"] for a in actions] == [0, 1]
        assert all(a["data"]["kc"] == KIND_OPTION for a in actions)

    def test_nonces_are_unique_per_prompt(self) -> None:
        assert new_approval_nonce() != new_approval_nonce()


class TestParseSubmitRefusals:
    """Everything about a submit is validated before it is used as a key."""

    def test_a_valid_approval_payload_round_trips(self) -> None:
        parsed = parse_submit(
            {"kc": KIND_APPROVAL, "rid": "7", "nonce": "n1", "decision": DECISION_APPROVE}
        )
        assert parsed == {
            "kc": KIND_APPROVAL,
            "rid": "7",
            "nonce": "n1",
            "decision": DECISION_APPROVE,
        }

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            "not-a-dict",
            {},
            {"kc": KIND_APPROVAL, "rid": "7", "decision": DECISION_APPROVE},  # no nonce
            {"kc": KIND_APPROVAL, "nonce": "n", "decision": DECISION_APPROVE},  # no rid
            {"kc": KIND_APPROVAL, "rid": "7", "nonce": "n"},  # no decision
            # An unknown decision must not be coerced into an approval.
            {"kc": KIND_APPROVAL, "rid": "7", "nonce": "n", "decision": "yes-please"},
            {"kc": "something_else", "rid": "7", "nonce": "n", "decision": DECISION_APPROVE},
            {"kc": KIND_OPTION, "nonce": "n", "label": "yes", "index": "abc"},  # non-numeric
            {"kc": KIND_OPTION, "nonce": "n", "index": 0},  # no label
            {
                "kc": KIND_OPTION,
                "nonce": "n",
                "label": "yes",
                "index": True,
            },  # bool is not an index
        ],
    )
    def test_a_malformed_payload_is_refused(self, payload) -> None:
        assert parse_submit(payload) is None

    def test_an_option_index_is_accepted_as_int_or_string(self) -> None:
        """Teams clients differ on whether a number survives as a number."""
        for index in (0, "0"):
            parsed = parse_submit({"kc": KIND_OPTION, "nonce": "n", "label": "yes", "index": index})
            assert parsed is not None and parsed["index"] == "0"


class TestDeciderFailsClosed:
    @pytest.mark.asyncio
    async def test_a_click_approves_the_waiting_prompt(self) -> None:
        decider = TeamsApprovalDecider(session_key="teams:s1")
        decider.arm("7", "n1")
        task = asyncio.ensure_future(decider(SimpleNamespace(request_id="7")))
        await asyncio.sleep(0)

        assert decider.resolve("7", "n1", approved=True) is True
        assert await task is True

    @pytest.mark.asyncio
    async def test_a_stale_nonce_cannot_approve(self) -> None:
        """ACP request ids restart at 1 per process, so an old card can name a
        live id for a DIFFERENT tool. The nonce is what refuses it."""
        decider = TeamsApprovalDecider(session_key="teams:s1")
        decider.arm("7", "fresh")
        task = asyncio.ensure_future(decider(SimpleNamespace(request_id="7")))
        await asyncio.sleep(0)

        assert decider.resolve("7", "stale-from-a-previous-run", approved=True) is False
        # The prompt is still waiting -- the stale click neither approved nor
        # consumed it.
        assert decider.resolve("7", "fresh", approved=False) is True
        assert await task is False

    @pytest.mark.asyncio
    async def test_a_timeout_denies(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.teams.approvals.APPROVAL_TIMEOUT_SECS", 0.01)
        decider = TeamsApprovalDecider(session_key="teams:s1")
        decider.arm("7", "n1")

        assert await decider(SimpleNamespace(request_id="7")) is False

    @pytest.mark.asyncio
    async def test_a_prompt_whose_card_never_landed_denies_at_once(self) -> None:
        """No 300-second park behind a card nobody received.

        The nonce is armed BEFORE the post so a fast click is not refused as stale,
        which means a failed post leaves an armed prompt with no control.
        """
        decider = TeamsApprovalDecider(session_key="teams:s1")
        decider.arm("7", "n1")
        decider.abandon("7")

        assert await decider(SimpleNamespace(request_id="7")) is False
        # And the retired nonce means a card that somehow DID land cannot answer it.
        assert decider.resolve("7", "n1", approved=True) is False

    @pytest.mark.asyncio
    async def test_abandon_resolves_a_prompt_already_being_awaited(self) -> None:
        decider = TeamsApprovalDecider(session_key="teams:s1")
        decider.arm("7", "n1")
        task = asyncio.ensure_future(decider(SimpleNamespace(request_id="7")))
        await asyncio.sleep(0)

        decider.abandon("7")
        assert await task is False

    @pytest.mark.asyncio
    async def test_an_expired_prompt_notifies_so_its_card_can_be_settled(self, monkeypatch) -> None:
        """Otherwise the buttons keep looking live in the chat forever."""
        monkeypatch.setattr("kiro_crew.teams.approvals.APPROVAL_TIMEOUT_SECS", 0.01)
        settled: list[str] = []

        async def _on_expired(rid: str) -> None:
            settled.append(rid)

        decider = TeamsApprovalDecider(session_key="teams:s1")
        decider.on_expired = _on_expired
        decider.arm("7", "n1")

        assert await decider(SimpleNamespace(request_id="7")) is False
        assert settled == ["7"]

    @pytest.mark.asyncio
    async def test_a_failing_settle_hook_does_not_break_the_decision(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.teams.approvals.APPROVAL_TIMEOUT_SECS", 0.01)

        async def _boom(rid: str) -> None:
            raise RuntimeError("edit failed")

        decider = TeamsApprovalDecider(session_key="teams:s1")
        decider.on_expired = _boom
        decider.arm("7", "n1")

        assert await decider(SimpleNamespace(request_id="7")) is False

    @pytest.mark.asyncio
    async def test_an_unknown_request_resolves_nothing(self) -> None:
        decider = TeamsApprovalDecider(session_key="teams:s1")
        assert decider.resolve("999", "n1", approved=True) is False

    @pytest.mark.asyncio
    async def test_a_second_click_is_refused(self) -> None:
        decider = TeamsApprovalDecider(session_key="teams:s1")
        decider.arm("7", "n1")
        task = asyncio.ensure_future(decider(SimpleNamespace(request_id="7")))
        await asyncio.sleep(0)

        assert decider.resolve("7", "n1", approved=True) is True
        assert (
            decider.resolve("7", "n1", approved=True) is False
        ), "an answered prompt must not be answerable twice"
        await task

    @pytest.mark.asyncio
    async def test_the_nonce_is_retired_with_the_prompt(self, monkeypatch) -> None:
        """A closed window must not reopen if the request id is reused."""
        monkeypatch.setattr("kiro_crew.teams.approvals.APPROVAL_TIMEOUT_SECS", 0.01)
        decider = TeamsApprovalDecider(session_key="teams:s1")
        decider.arm("7", "n1")
        await decider(SimpleNamespace(request_id="7"))

        assert decider.resolve("7", "n1", approved=True) is False

    @pytest.mark.asyncio
    async def test_two_sessions_awaiting_the_same_request_id_do_not_collide(self) -> None:
        """kiro-cli request ids restart at 1, so id 1 can be live in two sessions."""
        first = TeamsApprovalDecider(session_key="teams:a")
        second = TeamsApprovalDecider(session_key="teams:b")
        first.arm("1", "na")
        second.arm("1", "nb")
        task_a = asyncio.ensure_future(first(SimpleNamespace(request_id="1")))
        task_b = asyncio.ensure_future(second(SimpleNamespace(request_id="1")))
        await asyncio.sleep(0)

        assert TeamsApprovalDecider.resolve_global("teams:a", "1", "na", approved=True) is True
        assert TeamsApprovalDecider.resolve_global("teams:b", "1", "nb", approved=False) is True
        assert await task_a is True
        assert await task_b is False

    @pytest.mark.asyncio
    async def test_a_trust_click_records_the_grant(self) -> None:
        decider = TeamsApprovalDecider(session_key="teams:s1")
        decider.arm("7", "n1")
        task = asyncio.ensure_future(decider(SimpleNamespace(request_id="7")))
        await asyncio.sleep(0)

        decider.resolve("7", "n1", approved=True, trust=True)
        await task

        assert decider.trusted is True


class TestNoChannelLocalGrantStore:
    """Teams deliberately keeps NO grant of its own.

    A channel-local trusted-session store is a SECOND grant: its own lifetime, its
    own audit trail, and its own way to disagree with the dashboard about whether
    auto-approve is on. "Is YOLO on?" has to have one answer, so the card button and
    ``/yolo`` both arm the shared process-wide grant instead.
    """

    def test_the_module_exposes_no_trust_store(self) -> None:
        from kiro_crew.teams import approvals

        gone = [
            name
            for name in (
                "trust_session",
                "untrust_session",
                "session_is_trusted",
                "trust_remaining_secs",
                "clear_trusted_sessions",
                "TRUST_TTL_SECS",
                "_TRUSTED_SESSIONS",
            )
            if hasattr(approvals, name)
        ]
        assert not gone, f"a channel-local grant store came back: {gone}"

    def test_the_decider_only_records_that_trust_was_pressed(self) -> None:
        """Arming is async and audited, and resolve() runs on a sync click path.

        So the decider records the press and the dispatcher arms the shared grant --
        which is also what keeps the duration and the SEL row identical to `/yolo on`.
        """

        async def _go() -> bool:
            decider = TeamsApprovalDecider(session_key="teams:s1")
            decider.arm("7", "n1")
            task = asyncio.ensure_future(decider(SimpleNamespace(request_id="7")))
            await asyncio.sleep(0)
            decider.resolve("7", "n1", approved=True, trust=True)
            assert decider.trusted is True
            assert safety_override().is_active() is False, "the DECIDER must not arm it"
            return await task

        assert asyncio.run(_go()) is True


# ── Dispatcher-level: a click routes, and a dead click says so ────────────────


class _Client:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.cards: list[dict] = []

    async def send_message(self, conversation_id, content, service_url):
        self.sent.append(content)
        return f"mid-{len(self.sent)}"

    async def send_card(self, conversation_id, card, service_url):
        self.cards.append(card)
        return f"card-{len(self.cards)}"

    async def update_card(self, conversation_id, activity_id, card, service_url) -> bool:
        self.cards.append(card)
        return True

    async def update_message(self, conversation_id, activity_id, content, service_url) -> bool:
        return True

    async def send_typing(self, conversation_id, service_url) -> None:
        return None


class _Sessions:
    """The slice a card click touches, plus the slice ONE real turn touches.

    The turn is only used to capture the ``auto_approve_session`` predicate the
    driver would read, so the provider never streams anything.
    """

    async def aflush(self) -> None:
        # The resume release flushes the session map before it reports success; a
        # double without this correctly surfaces as a release FAILURE.
        return None

    def clear_mirror_links_at(self, link, *, reason: str = "") -> list:
        return []

    def find_mirror_sessions(self, link, *, inbound_only: bool = False) -> list:
        # No resumed dashboard session in these tests, so routing is a no-op. Present
        # because Teams routes EVERY message through the resume resolver.
        return []

    def is_busy(self, key) -> bool:
        return False

    async def get_or_create(self, key, *, agent=None, channel_id=None):
        return SimpleNamespace(supports_steer=False), False, False

    async def set_channel(self, key, channel_id) -> None:
        return None

    def record_success(self, key) -> None:
        return None

    async def record_failure(self, key) -> None:
        return None

    def release(self, key) -> None:
        return None

    def get_provider(self, key):
        return None

    def has_session(self, key) -> bool:
        return True

    def check_context_usage(self, key, provider) -> float:
        return 0.0

    def get_pid(self, key) -> None:
        return None

    def is_mirror_paused(self, key, *, origin: bool = False) -> bool:
        return False

    def set_mirror_link(self, key, link, *, reason: str = "") -> None:
        return None

    def dequeue(self, key) -> None:
        return None

    def max_generation(self, bucket: str) -> int:
        return -1

    def mirror_opt_out(self, key) -> bool:
        return False

    def get_mirror_link(self, key):
        return None

    def batched_save(self):
        return contextlib.nullcontext()


def _dispatcher(client) -> TeamsDispatcher:
    d = TeamsDispatcher(
        sessions=_Sessions(),
        ctx_builder=SimpleNamespace(
            hooks=SimpleNamespace(auto_approve_subagent_spawn=False),
            build_message=lambda text, is_new, key, **kw: (text, None),
        ),
        cfg=SimpleNamespace(
            messaging=SimpleNamespace(
                queue_mode="steer", dm_scope="per_user", idle_reset_minutes=0, daily_reset_hour=-1
            ),
            agent=SimpleNamespace(default_agent="kirocrew", approval_mode="interactive"),
            teams=SimpleNamespace(soft_threshold_pct=80, hard_threshold_pct=95),
        ),
    )
    d.client = client
    return d


def _text_inbound(text: str) -> TeamsInbound:
    return TeamsInbound(
        conversation_id="CONV",
        conversation_type="personal",
        service_url=_SVC,
        text=text,
        user_email=_EMAIL,
        activity_id="act-1",
    )


def _click(value: dict) -> TeamsInbound:
    return TeamsInbound(
        conversation_id="CONV",
        conversation_type="personal",
        service_url=_SVC,
        text="",
        user_email=_EMAIL,
        activity_id="act-1",
        card_value=value,
    )


class TestCardActionRouting:
    @pytest.mark.asyncio
    async def test_a_dead_click_tells_the_user(self) -> None:
        """A button that silently does nothing is indistinguishable from a bug."""
        client = _Client()
        d = _dispatcher(client)

        await d._handle_card_action(
            _click({"kc": KIND_APPROVAL, "rid": "7", "nonce": "n", "decision": DECISION_APPROVE})
        )

        assert client.sent and "no longer waiting" in client.sent[-1]

    @pytest.mark.asyncio
    async def test_an_unparseable_click_is_ignored_silently(self) -> None:
        """Not ours: say nothing rather than answering an unrelated card."""
        client = _Client()
        d = _dispatcher(client)

        await d._handle_card_action(_click({"unrelated": "payload"}))

        assert client.sent == []

    @pytest.mark.asyncio
    async def test_a_deny_click_resolves_the_prompt_and_settles_the_card(self) -> None:
        client = _Client()
        d = _dispatcher(client)
        session_key = d._session_key(_EMAIL)
        decider = TeamsApprovalDecider(session_key=session_key)
        decider.arm("7", "n1")
        task = asyncio.ensure_future(decider(SimpleNamespace(request_id="7")))
        await asyncio.sleep(0)
        # Register a renderer so the answered card can be replaced.
        from kiro_crew.teams.renderer import TeamsRenderer
        from kiro_crew.teams.transport import TEAMS_CAPABILITIES

        renderer = TeamsRenderer(
            client, "CONV", _SVC, TEAMS_CAPABILITIES, session_key=session_key, decider=decider
        )
        renderer._pending_prompts["7"] = ("card-1", "fs_read")
        d._active_renderers[session_key] = renderer

        await d._handle_card_action(
            _click({"kc": KIND_APPROVAL, "rid": "7", "nonce": "n1", "decision": DECISION_DENY})
        )

        assert await task is False
        assert client.cards, "the answered prompt's card is replaced"
        assert client.cards[-1]["content"]["actions"] == []

    @pytest.mark.asyncio
    async def test_a_trust_click_grants_the_session(self) -> None:
        client = _Client()
        d = _dispatcher(client)
        session_key = d._session_key(_EMAIL)
        decider = TeamsApprovalDecider(session_key=session_key)
        decider.arm("7", "n1")
        task = asyncio.ensure_future(decider(SimpleNamespace(request_id="7")))
        await asyncio.sleep(0)

        await d._handle_card_action(
            _click({"kc": KIND_APPROVAL, "rid": "7", "nonce": "n1", "decision": DECISION_TRUST})
        )

        assert await task is True
        assert decider.trusted is True
        # Armed NOW, not in the post-turn finally: the button says "stop asking",
        # and the next tool of the SAME turn is usually what prompted it.
        assert safety_override().is_active() is True

    @pytest.mark.asyncio
    async def test_trust_stops_the_prompting_for_the_REST_of_the_same_turn(self) -> None:
        """The whole point of the button, and the half `decider.trusted` misses.

        ``TurnDriver`` re-reads ``auto_approve_session`` on every permission
        request, so what decides whether the second tool of a multi-tool turn is
        carded again is whether that predicate answers True the moment the click
        lands. Arming it in the dispatcher's post-turn ``finally`` instead would keep
        prompting for the rest of the turn while the settled card already said
        auto-approve was armed.
        """
        from kiro_crew.messaging import dispatch as D

        captured: list[Any] = []

        class _Recorder:
            def __init__(self, provider: Any, renderer: Any, **kw: Any) -> None:
                captured.append(kw.get("auto_approve_session"))

            async def run(self, message: str) -> str:
                return "ok"

        client = _Client()
        d = _dispatcher(client)
        session_key = d._session_key(_EMAIL)
        real_driver = D.TurnDriver
        try:
            D.TurnDriver = _Recorder  # type: ignore[misc]
            await d.handle_message(_text_inbound("do two things"))
        finally:
            D.TurnDriver = real_driver  # type: ignore[misc]

        assert captured, "the turn never reached the driver"
        predicate = captured[0]
        assert predicate is not None and predicate() is False

        decider = TeamsApprovalDecider(session_key=session_key)
        decider.arm("7", "n1")
        task = asyncio.ensure_future(decider(SimpleNamespace(request_id="7")))
        await asyncio.sleep(0)
        try:
            await d._handle_card_action(
                _click({"kc": KIND_APPROVAL, "rid": "7", "nonce": "n1", "decision": DECISION_TRUST})
            )
            assert await task is True
            # The SAME predicate the running turn holds now answers True.
            assert predicate() is True, "a trust click must be visible to the live turn"
        finally:
            safety_override().deactivate("test")

    @pytest.mark.asyncio
    async def test_a_stale_option_chip_asks_the_user_to_type(self) -> None:
        client = _Client()
        d = _dispatcher(client)

        await d._handle_card_action(
            _click({"kc": KIND_OPTION, "nonce": "old", "index": 0, "label": "yes"})
        )

        assert client.sent and "earlier reply" in client.sent[-1]

    @pytest.mark.asyncio
    async def test_a_chip_the_MODEL_labelled_never_executes_as_a_command(self) -> None:
        """A chip label is model output, so one tap must not run a slash command.

        ``[OPTIONS:]`` labels come from the model's own trailer and are only
        display-redacted, which does not strip a leading "/". With command
        interpretation on, ``[OPTIONS: /dashboard | cancel]`` renders a chip whose
        single tap mints a presigned dashboard login token into the chat; ``/yolo on``
        is the same tap away from process-wide auto-approve. A chip is turn content,
        exactly like a drained queue payload.
        """
        from kiro_crew.teams.renderer import TeamsRenderer
        from kiro_crew.teams.transport import TEAMS_CAPABILITIES

        client = _Client()
        d = _dispatcher(client)
        session_key = d._session_key(_EMAIL)
        renderer = TeamsRenderer(client, "CONV", _SVC, TEAMS_CAPABILITIES, session_key=session_key)
        renderer._option_nonce = "n9"
        renderer._option_labels = ["/dashboard", "cancel"]
        d._active_renderers[session_key] = renderer
        seen: list[TeamsInbound] = []
        minted: list[str] = []

        async def _record(inbound, *, interpret_commands: bool = True, drain: bool = True) -> None:
            seen.append(inbound)
            assert interpret_commands is False, "a chip must reach the model as text"

        async def _explode(inbound, arg) -> None:  # pragma: no cover - must not run
            minted.append(arg)

        real_handle, real_dashboard = d.handle_message, d._handle_dashboard
        try:
            d.handle_message = _record  # type: ignore[method-assign]
            d._handle_dashboard = _explode  # type: ignore[method-assign]
            await d._handle_card_action(
                _click({"kc": KIND_OPTION, "nonce": "n9", "index": 0, "label": "x"})
            )
        finally:
            d.handle_message, d._handle_dashboard = real_handle, real_dashboard  # type: ignore

        assert minted == [], "no dashboard credential may be minted by a chip tap"
        assert [msg.text for msg in seen] == ["/dashboard"]

    @pytest.mark.asyncio
    async def test_an_option_label_comes_from_this_process_not_the_payload(self) -> None:
        """A forged label must not be injected as if the user had typed it."""
        from kiro_crew.teams.renderer import TeamsRenderer
        from kiro_crew.teams.transport import TEAMS_CAPABILITIES

        client = _Client()
        renderer = TeamsRenderer(client, "CONV", _SVC, TEAMS_CAPABILITIES)
        renderer._option_nonce = "n9"
        renderer._option_labels = ["yes", "no"]

        assert renderer.option_label("n9", "1") == "no"
        assert renderer.option_label("wrong-nonce", "1") == ""
        assert renderer.option_label("n9", "99") == "", "an out-of-range index resolves to nothing"


class TestTheProductionEntrypoint:
    """A click arrives through ``handle_message``, not through the private helper.

    Every other test in this file calls ``_handle_card_action`` directly, which
    leaves the dispatch branch in ``handle_message`` — the only path a real Teams
    activity takes — unexercised. Deleting that branch would strand every button
    in production while the suite stayed green, so these two tests drive the real
    entrypoint.
    """

    @pytest.mark.asyncio
    async def test_a_click_arriving_as_an_activity_resolves_the_prompt(self, monkeypatch) -> None:
        async def _permitted(_channel_type):
            return True

        async def _no_turn(turn, **kw):
            raise AssertionError("a card click must never start a turn")

        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.inbound_permitted", _permitted)
        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", _no_turn)

        client = _Client()
        d = _dispatcher(client)
        session_key = d._session_key(_EMAIL)
        decider = TeamsApprovalDecider(session_key=session_key)
        decider.arm("7", "n1")
        task = asyncio.ensure_future(decider(SimpleNamespace(request_id="7")))
        await asyncio.sleep(0)

        # The real entrypoint, with a real card-action activity.
        await d.handle_message(
            _click({"kc": KIND_APPROVAL, "rid": "7", "nonce": "n1", "decision": DECISION_APPROVE})
        )

        assert await task is True, "the awaiting prompt was not resolved by the activity"

    @pytest.mark.asyncio
    async def test_the_governance_gate_still_applies_to_a_click(self, monkeypatch) -> None:
        """A host profile denying the channel must stop a click too."""

        async def _denied(_channel_type):
            return False

        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.inbound_permitted", _denied)

        client = _Client()
        d = _dispatcher(client)
        session_key = d._session_key(_EMAIL)
        decider = TeamsApprovalDecider(session_key=session_key)
        decider.arm("7", "n1")
        task = asyncio.ensure_future(decider(SimpleNamespace(request_id="7")))
        await asyncio.sleep(0)

        await d.handle_message(
            _click({"kc": KIND_APPROVAL, "rid": "7", "nonce": "n1", "decision": DECISION_APPROVE})
        )

        assert not task.done(), "a governance-denied click must not resolve the prompt"
        # Leave nothing pending for the event loop at teardown.
        decider.resolve("7", "n1", approved=False)
        await task


class TestTheTrustButtonArmsTheSharedGrant:
    """The button and `/yolo on` must be the same grant, or "is YOLO on?" has two
    answers. So the DISPATCHER routes the press through the shared helper."""

    @pytest.mark.asyncio
    async def test_a_valid_trust_click_arms_the_shared_grant(self) -> None:
        client = _Client()
        d = _dispatcher(client)
        session_key = d._session_key(_EMAIL)
        decider = TeamsApprovalDecider(session_key=session_key)
        decider.arm("7", "n1")
        task = asyncio.ensure_future(decider(SimpleNamespace(request_id="7")))
        await asyncio.sleep(0)
        assert safety_override().is_active() is False

        await d._handle_card_action(
            _click({"kc": KIND_APPROVAL, "rid": "7", "nonce": "n1", "decision": DECISION_TRUST})
        )

        assert await task is True
        assert safety_override().is_active() is True, "the same grant /yolo on arms"
        # And the user is told, in the shared helper's own words.
        assert any("YOLO" in body for body in client.sent), client.sent

    @pytest.mark.asyncio
    async def test_a_plain_approve_arms_nothing(self) -> None:
        client = _Client()
        d = _dispatcher(client)
        session_key = d._session_key(_EMAIL)
        decider = TeamsApprovalDecider(session_key=session_key)
        decider.arm("7", "n1")
        task = asyncio.ensure_future(decider(SimpleNamespace(request_id="7")))
        await asyncio.sleep(0)

        await d._handle_card_action(
            _click({"kc": KIND_APPROVAL, "rid": "7", "nonce": "n1", "decision": DECISION_APPROVE})
        )

        assert await task is True
        assert safety_override().is_active() is False

    @pytest.mark.asyncio
    async def test_a_stale_trust_click_arms_nothing(self) -> None:
        """A forged or replayed Trust payload must not create a grant."""
        client = _Client()
        d = _dispatcher(client)
        session_key = d._session_key(_EMAIL)
        decider = TeamsApprovalDecider(session_key=session_key)
        decider.arm("7", "fresh")
        task = asyncio.ensure_future(decider(SimpleNamespace(request_id="7")))
        await asyncio.sleep(0)

        await d._handle_card_action(
            _click({"kc": KIND_APPROVAL, "rid": "7", "nonce": "stale", "decision": DECISION_TRUST})
        )

        assert safety_override().is_active() is False, "a stale press must arm nothing"
        decider.resolve("7", "fresh", approved=False)
        assert await task is False
