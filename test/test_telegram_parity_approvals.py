"""Session trust and the approval prompt, on the grant every channel shares.

A Trust press grants before it resolves and reaches spawned subagents, a revoke or
a yolo switch drops both halves, the prompt shows what is being approved, escaped
and bounded, a press resolves only the prompt it was rendered for (its nonce), and
Telegram grows neither a channel-local grant nor a command redirect map. The doubles
come from ``test_telegram`` and the shared renderer from
``telegram_parity_helpers.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from telegram_parity_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _drop_live_config_snapshot,
    _renderer,
)
from test_telegram import FakeClient, _dispatcher

from kiro_crew.telegram.renderer import TelegramApprovalDecider, TelegramRenderer
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES


def _trust_press(nonce: str = "") -> SimpleNamespace:
    """A Trust press from the allow-listed owner's DM.

    *nonce* is the per-prompt value the renderer minted. Omitted means "a button
    from an earlier prompt or an earlier process", which must resolve nothing.
    """
    return SimpleNamespace(
        callback_query_id="q",
        user_id=1,
        chat_id=1,
        chat_type="private",
        message_id=1,
        data=f"a:r1:{nonce}:t",
        label="",
        message_thread_id=None,
    )


class TestSessionTrust:
    @pytest.mark.asyncio
    async def test_the_prompt_offers_trust_beside_approve_and_deny(self) -> None:
        renderer, client = _renderer()
        renderer._last_tool = "bash"
        await renderer.on_prompt_choice([], "r1")
        markup = client.sent[-1][1]
        data = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        # Every button of ONE prompt carries the SAME per-prompt nonce, and the
        # flag is last so the reject-press check can read it without parsing.
        nonces = {d.split(":")[2] for d in data}
        assert len(nonces) == 1, f"one prompt, one nonce: {data}"
        nonce = nonces.pop()
        # The shared `new_approval_nonce` mints token_urlsafe(8): 64 bits, ~11
        # urlsafe chars. Asserted as a floor rather than an exact width, since the
        # encoding is the shared minter's business and the property that matters
        # here is that a guessable-length value never reaches callback_data.
        assert len(nonce) >= 11, f"expected 64 bits of entropy: {nonce!r}"
        assert data == [f"a:r1:{nonce}:1", f"a:r1:{nonce}:0", f"a:r1:{nonce}:t"]
        assert all(len(d.encode()) <= 64 for d in data), "Telegram caps callback_data at 64 bytes"

    @pytest.mark.asyncio
    async def test_a_trust_press_grants_before_it_resolves(self) -> None:
        # The grant has to cover the tool THIS prompt is asking about: resolving
        # first would approve this one by the button and let the next tool race
        # the write.
        from kiro_crew.messaging import session_trust as trust

        trust.clear_trusted_sessions()
        dispatcher, client, sessions = _dispatcher({1})
        order: list[str] = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "kiro_crew.telegram.transport_dispatch.add_trusted_session",
                lambda k, s=None: order.append("granted"),
            )
            mp.setattr(
                TelegramApprovalDecider,
                "is_pending",
                classmethod(lambda cls, k, n="": True),
            )
            mp.setattr(
                TelegramApprovalDecider,
                "resolve_global",
                staticmethod(lambda k, approved, nonce="": order.append("resolved") or True),
            )
            await dispatcher.on_callback(_trust_press("f" * 16))
        assert order == ["granted", "resolved"]
        assert "Trusted" in client.edits[-1][1]

    @pytest.mark.asyncio
    async def test_trust_on_a_dead_prompt_grants_nothing(self) -> None:
        # The registry is empty after a gateway restart, so every approval button
        # left in the chat's scrollback is a dead key. Trust is the one press whose
        # effect OUTLIVES its prompt — it auto-approves every later tool in the
        # conversation and writes the session policy to `auto` so subagents inherit
        # it — so a dead key must grant nothing at all. Granting while replying
        # "expired" is strictly worse than either outcome alone: the operator is
        # told the press did nothing, and standing authority is handed out anyway.
        from kiro_crew.messaging import session_trust as trust

        trust.clear_trusted_sessions()
        dispatcher, client, _ = _dispatcher({1})
        audits: list[dict] = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "kiro_crew.telegram.transport_dispatch.sel",
                lambda: SimpleNamespace(log_api_access=lambda **kw: audits.append(kw)),
            )
            await dispatcher.on_callback(_trust_press())
        assert not trust.is_session_trusted(dispatcher._session_key(("direct", "1")))
        assert "expired" in client.edits[-1][1]
        grants = [a for a in audits if a.get("operation") == "telegram.trust_session"]
        assert grants, "a refused Trust press must still be auditable"
        assert [a["outcome"] for a in grants] == ["denied"]
        assert grants[0].get("error") == "no_pending_approval"

    @pytest.mark.asyncio
    async def test_a_live_trust_press_really_reaches_the_shared_set(self) -> None:
        # The unstubbed path, so the two tests above cannot both pass against a
        # grant that never lands anywhere.
        from kiro_crew.messaging import session_trust as trust

        trust.clear_trusted_sessions()
        dispatcher, _, _ = _dispatcher({1})
        key = dispatcher._session_key(("direct", "1"))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                TelegramApprovalDecider,
                "is_pending",
                classmethod(lambda cls, k, n="": True),
            )
            mp.setattr(
                TelegramApprovalDecider,
                "resolve_global",
                staticmethod(lambda k, a, nonce="": True),
            )
            await dispatcher.on_callback(_trust_press("f" * 16))
        assert trust.is_session_trusted(key)

    @pytest.mark.asyncio
    async def test_a_trusted_conversation_auto_approves_the_rest(self) -> None:
        from kiro_crew.messaging import session_trust as trust

        trust.clear_trusted_sessions()
        dispatcher, _, _ = _dispatcher({1})
        key = dispatcher._session_key(("direct", "1"))
        assert trust.is_session_trusted(key) is False
        trust.add_trusted_session(key, None)
        assert trust.is_session_trusted(key) is True
        trust.clear_trusted_sessions()
        assert trust.is_session_trusted(key) is False

    def test_trust_propagates_to_spawned_subagents(self) -> None:
        # A subagent reads its PARENT's approval policy, never the in-memory set,
        # so without the policy write a trusted conversation's children still stop
        # to ask.
        from kiro_crew.messaging import session_trust as trust

        trust.clear_trusted_sessions()
        sessions = MagicMock()
        trust.add_trusted_session("telegram:k", sessions)
        assert sessions.set_approval_policy.call_args.args == ("telegram:k", "auto")
        trust.clear_trusted_sessions()

    def test_revoking_trust_also_revokes_the_subagent_half(self) -> None:
        """Both halves of the grant, or a revoke reports success and leaves it on.

        The subagent path reads the parent's ``approval_policy``, not the in-memory
        mapping, so clearing only the mapping would let a later ``/spawn`` inherit
        ``auto`` from a grant that had been revoked. The empty string is what the
        dashboard's own untrust toggle writes, so the two paths agree.
        """
        from kiro_crew.messaging import session_trust as trust

        trust.clear_trusted_sessions()
        sessions = MagicMock()
        trust.add_trusted_session("telegram:k", sessions)
        trust.clear_trusted_sessions()

        assert trust.is_session_trusted("telegram:k") is False
        # Granted "auto", then revoked back to the default.
        policies = [c.args for c in sessions.set_approval_policy.call_args_list]
        assert policies == [("telegram:k", "auto"), ("telegram:k", "")]

    def test_a_revoke_that_raises_still_drops_every_other_grant(self) -> None:
        """One unhappy manager must not leave the rest of the grants standing."""
        from kiro_crew.messaging import session_trust as trust

        trust.clear_trusted_sessions()
        angry, calm = MagicMock(), MagicMock()
        angry.set_approval_policy.side_effect = RuntimeError("map locked")
        trust.add_trusted_session("telegram:a", angry)
        trust.add_trusted_session("telegram:b", calm)

        trust.clear_trusted_sessions()

        assert trust.is_session_trusted("telegram:a") is False
        assert trust.is_session_trusted("telegram:b") is False
        # The healthy one was still revoked despite the other raising.
        assert ("telegram:b", "") in [c.args for c in calm.set_approval_policy.call_args_list]

    def test_disabling_yolo_revokes_both_halves(self) -> None:
        """The reachable revoke path, pinned by BEHAVIOUR rather than by grep.

        This is the altitude the defect lived at: ``disable_yolo`` cleared the trust
        container directly, which drops the in-memory half and leaves each granted
        session's ``approval_policy`` at ``auto`` -- and a subagent reads that policy
        rather than the container, so a later spawn inherited a trust that had just
        been revoked. Asserted through the real function so an alias cannot dodge it.
        """
        from kiro_crew.messaging import session_trust as trust
        from kiro_crew.safety_override import safety_override
        from kiro_crew.slack import handler as h

        trust.clear_trusted_sessions()
        sessions = MagicMock()
        trust.add_trusted_session("telegram:k", sessions)
        safety_override().activate("test", ttl=60)
        try:
            h.disable_yolo()
        finally:
            safety_override().deactivate("test")
            trust.clear_trusted_sessions()

        assert trust.is_session_trusted("telegram:k") is False
        assert ("telegram:k", "") in [
            c.args for c in sessions.set_approval_policy.call_args_list
        ], "disable_yolo must reset the approval policy, not just the mapping"

    def test_the_dashboard_cannot_reach_the_trust_container(self) -> None:
        """No import means no alias, so the expiry path cannot revoke partially.

        The dashboard's override-expiry resets its own slots' policies and then
        revokes channel trust. Reaching the container directly is what let it drop
        one half; importing only the API makes the complete revoke the only option
        available to it.
        """
        import inspect
        import re
        from pathlib import Path

        from kiro_crew.dashboard import server as dash_server

        src = inspect.getsource(dash_server)
        # With the server_runtime owners server.py composes, which hold the rest of
        # its startup code.
        owners = sorted((Path(dash_server.__file__).parent / "server_runtime").glob("[!_]*.py"))
        assert owners, "expected the server_runtime owners beside server.py"
        src += "".join(path.read_text(encoding="utf-8") for path in owners)
        # A TOKEN match, so `clear_trusted_sessions` (which contains the substring)
        # is not mistaken for a reference to the container itself.
        bare = re.findall(r"(?<![A-Za-z0-9_])_trusted_sessions\b", src)
        assert not bare, (
            "dashboard/server.py must reach trust only through "
            "messaging.session_trust's API, so a partial revoke is not expressible"
        )
        assert "clear_trusted_sessions(" in src
        # And it must pass the standing-trust exclusion, built from the SAME
        # condition the policy-reset loop skips on. Without both halves the revoke
        # resets a policy the loop deliberately preserved, which revokes standing
        # dashboard trust that no override expiry was supposed to touch. Asserted on
        # the source because the callback is a closure inside the app factory, so
        # reaching it behaviourally would mean standing up the whole dashboard.
        assert (
            "keep_policy=standing_trust" in src
        ), "the override-expiry revoke must exclude slots carrying standing trust"
        assert (
            "if slot._trust or slot._trust_reads:" in src
        ), "the exclusion set must be built from the same condition the reset loop skips"

    def test_a_kept_key_loses_its_grant_but_keeps_its_policy(self) -> None:
        """Standing dashboard trust is a different, longer-lived decision.

        The dashboard's override-expiry deliberately preserves the approval policy of
        slots carrying standing trust. A Trust press can file a ``dashboard:`` key in
        this shared grant, so a blanket revoke here would reset a policy nobody
        expired. The GRANT still goes: only the policy write is skipped.
        """
        from kiro_crew.messaging import session_trust as trust

        trust.clear_trusted_sessions()
        kept, dropped = MagicMock(), MagicMock()
        trust.add_trusted_session("dashboard:slot-1", kept)
        trust.add_trusted_session("telegram:k", dropped)

        trust.clear_trusted_sessions(keep_policy={"dashboard:slot-1"})

        # Both grants are gone from the store.
        assert trust.is_session_trusted("dashboard:slot-1") is False
        assert trust.is_session_trusted("telegram:k") is False
        # Only the un-kept one had its policy reset.
        assert ("dashboard:slot-1", "") not in [
            c.args for c in kept.set_approval_policy.call_args_list
        ]
        assert ("telegram:k", "") in [c.args for c in dropped.set_approval_policy.call_args_list]

    def test_omitting_the_exclusion_revokes_everything(self) -> None:
        """A forgotten argument must err toward MORE revocation, not less."""
        from kiro_crew.messaging import session_trust as trust

        trust.clear_trusted_sessions()
        sessions = MagicMock()
        trust.add_trusted_session("dashboard:slot-1", sessions)
        trust.clear_trusted_sessions()
        assert ("dashboard:slot-1", "") in [
            c.args for c in sessions.set_approval_policy.call_args_list
        ]

    def test_a_grant_with_no_manager_needs_no_manager_to_revoke(self) -> None:
        """The channel paths pass one; the predicate-only callers do not."""
        from kiro_crew.messaging import session_trust as trust

        trust.clear_trusted_sessions()
        trust.add_trusted_session("telegram:k", None)
        trust.clear_trusted_sessions()  # must not raise
        assert trust.is_session_trusted("telegram:k") is False

    def test_a_failed_policy_write_still_grants_locally(self) -> None:
        from kiro_crew.messaging import session_trust as trust

        trust.clear_trusted_sessions()
        sessions = MagicMock()
        sessions.set_approval_policy.side_effect = RuntimeError("map locked")
        trust.add_trusted_session("telegram:k", sessions)
        assert trust.is_session_trusted("telegram:k") is True
        trust.clear_trusted_sessions()

    def test_slack_and_the_channels_read_ONE_grant(self) -> None:
        # Slack keeps its own names for its callers; both must resolve to the same
        # set, or a Trust press in one surface leaves the other still asking.
        from kiro_crew.messaging import session_trust as trust
        from kiro_crew.slack import handler as h

        trust.clear_trusted_sessions()
        trust.add_trusted_session("shared:key", None)
        assert h.is_slack_session_trusted("shared:key") is True
        assert h._trusted_sessions is trust._trusted_sessions
        trust.clear_trusted_sessions()


class TestApprovalDetail:
    @pytest.mark.asyncio
    async def test_the_prompt_shows_what_is_being_approved(self) -> None:
        # "Approve bash?" is not a decision a user can make; which command it
        # wants to run is.
        renderer, client = _renderer()
        renderer._last_tool = "bash"
        await renderer.on_prompt_choice([], "r1", tool_input="rm -rf /tmp/build && make")
        text = client.sent[-1][0]
        assert "rm -rf /tmp/build" in text and "<pre>" in text

    @pytest.mark.asyncio
    async def test_no_detail_leaves_the_prompt_as_it_was(self) -> None:
        renderer, client = _renderer()
        renderer._last_tool = "bash"
        await renderer.on_prompt_choice([], "r1", tool_input="")
        assert "<pre>" not in client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_the_detail_is_escaped_and_bounded(self) -> None:
        renderer, client = _renderer()
        renderer._last_tool = "bash"
        await renderer.on_prompt_choice([], "r1", tool_input="<script>x</script> " + "A" * 4000)
        text = client.sent[-1][0]
        assert "<script>" not in text
        assert len(text) < 1200 and text.rstrip().endswith("</pre>")

    @pytest.mark.asyncio
    async def test_the_driver_carries_the_redacted_input_to_the_renderer(self) -> None:
        # The field is additive on OutputEvent with a safe default, and dispatch
        # passes it unconditionally — no capability probe on the shared path.
        from kiro_crew.messaging.renderer import PROMPT_CHOICE, OutputEvent

        seen: list[str] = []

        class _R(TelegramRenderer):
            async def on_prompt_choice(
                self, options, request_id, tool_title="", tool_purpose="", tool_input=""
            ):
                seen.append(tool_input)

        renderer = _R(FakeClient(), 1, TELEGRAM_CAPABILITIES)  # type: ignore[arg-type]
        await renderer.dispatch(
            OutputEvent(kind=PROMPT_CHOICE, request_id="r1", tool_input="cat /etc/hosts")
        )
        assert seen == ["cat /etc/hosts"]


class TestApprovalPromptMarkup:
    @pytest.mark.asyncio
    async def test_the_tool_name_is_monospaced_not_shown_with_literal_backticks(self) -> None:
        renderer, client = _renderer()
        renderer._last_tool = "bash"
        await renderer.on_prompt_choice([], "r1")
        text, markup = client.sent[-1]
        assert "<code>bash</code>" in text and "`" not in text
        row = [b["callback_data"] for b in markup["inline_keyboard"][0]]
        assert [d.split(":")[1] for d in row] == ["r1", "r1"]
        assert [d.rsplit(":", 1)[1] for d in row] == ["1", "0"]

    @pytest.mark.asyncio
    async def test_a_markup_bearing_tool_name_is_escaped(self) -> None:
        renderer, client = _renderer()
        renderer._last_tool = "<script>x</script>"
        await renderer.on_prompt_choice([], "r1")
        assert "<script>" not in client.sent[-1][0]


class TestGatewayWiring:
    def test_the_dispatcher_property_comes_from_the_shared_ABC(self) -> None:
        # register_channel_transport reads transport.dispatcher to inject the
        # dashboard state; the base MessagingTransport already provides it, so a
        # per-channel override would be a second copy of one fact.
        from kiro_crew.telegram.transport import TelegramTransport

        assert "dispatcher" not in vars(TelegramTransport)
        dispatcher, _, _ = _dispatcher({1})
        transport = TelegramTransport(FakeClient(), dispatch=dispatcher.handle_message)  # type: ignore[arg-type]
        assert transport.dispatcher is dispatcher

    def test_the_dispatcher_starts_with_no_dashboard_state(self) -> None:
        dispatcher, _, _ = _dispatcher({1})
        assert dispatcher.dashboard_state is None


class TestNoChannelLocalGrantOrRedirectSeam:
    """Telegram must not grow Slack's two channel-local seams.

    Slack carries both, and neither is worth copying:

    * a **named yolo wrapper** (`is_yolo_mode` / `set_yolo_mode` in
      `slack/handler.py`). The state underneath is the shared `safety_override`
      grant, so the wrapper adds a second name for one fact — and a second name is
      how a channel ends up with a second SOURCE. A grant is global by nature: the
      operator who turns auto-approve off expects it off everywhere, not off in the
      surface they happened to type it in.
    * a **`!cmd` -> `/kirocrew cmd` redirect map** (`_BANG_TO_SLASH`). Three of its
      entries point at sub-commands that are not registered, so it tells the user to
      run something that falls through to help. A redirect table is a promise to keep
      two grammars working, and it rots the moment one of them moves.

    Telegram has neither today. This pins that, because both are the kind of thing
    that gets added one convenience at a time.
    """

    def test_yolo_reads_and_writes_the_shared_grant(self) -> None:
        """The handler owns no grant of its own; it delegates the decision.

        Asserted on the DELEGATION rather than on a literal ``safety_override()``
        call in this handler: the ladder now lives in
        ``messaging.commands.run_yolo_command``, which is one shared copy for every
        channel and is a stronger version of the same property. What must stay true
        either way is that Telegram holds no local grant state, so a grant taken
        here expires everywhere.
        """
        import inspect

        from kiro_crew.messaging import commands as shared_commands
        from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

        src = inspect.getsource(TelegramDispatcher._handle_yolo)
        assert "run_yolo_command(" in src, "the grant ladder must stay shared"
        for local in ("self._yolo", "_yolo_mode", "self.yolo"):
            assert local not in src, f"{local} would be a channel-local grant"
        # And the shared ladder is the thing that reaches the process-wide grant,
        # so the delegation above is not just indirection to another local copy.
        assert "safety_override()" in inspect.getsource(shared_commands.run_yolo_command)

    def test_there_is_no_telegram_yolo_config_field(self) -> None:
        # A config key is the durable form of the same mistake: it would survive a
        # restart and disagree with the shared grant.
        from dataclasses import fields

        from kiro_crew.config.loader import KiroCrewConfig

        names = {f.name for f in fields(KiroCrewConfig().telegram)}
        offenders = {n for n in names if "yolo" in n or "auto_approve" in n}
        assert not offenders, f"telegram must not own an approval grant: {offenders}"

    def test_the_dispatcher_holds_no_grant_state_of_its_own(self) -> None:
        d, _, _ = _dispatcher({7})
        offenders = [a for a in vars(d) if "yolo" in a.lower() or "auto_approve" in a.lower()]
        assert not offenders, f"channel-local grant state: {offenders}"

    def test_there_is_no_command_redirect_map(self) -> None:
        # Aliases are fine and different: `/new` and `/start` resolve to ONE command
        # name. A redirect map instead tells the user to type something else, which
        # is a second grammar to keep alive.
        import inspect

        from kiro_crew.telegram import commands as tc

        src = inspect.getsource(tc)
        for marker in ("BANG_TO_SLASH", "_REDIRECTS", "DEPRECATED_COMMANDS"):
            assert marker not in src, f"{marker} is a redirect seam"
        # Every alias resolves through the parser that OWNS it, so no alias can point
        # at a command that does not exist. Three sets are deliberately owned by a
        # different parser and would read as broken against `parse_command`:
        #   /queue, /steer  -> parse_mid_turn_override; they need a message body, and
        #                      a bare token answers with usage rather than acting.
        #   /kirocrew       -> requires the explicit `dashboard` subcommand, so a bare
        #                      "/kirocrew" falls through as ordinary chat text on
        #                      purpose (a typo or a menu tap must not mint a token).
        mid_turn = {"/queue", "/steer"}
        unresolved: list[str] = []
        for name, value in vars(tc).items():
            if not name.endswith("_ALIASES"):
                continue
            for spelling in value:
                if spelling in mid_turn:
                    cmd, _rest = tc.parse_mid_turn_override(f"{spelling} a message")
                    ok = cmd is not None
                elif spelling == "/kirocrew":
                    ok = tc.parse_command(f"{spelling} dashboard") == "dashboard"
                else:
                    ok = tc.parse_command(spelling) is not None
                if not ok:
                    unresolved.append(f"{name}:{spelling}")
        assert not unresolved, f"aliases that resolve to no command: {unresolved}"


class TestApprovalNonceBindsThePress:
    """A press resolves the prompt it was rendered for, not merely one with that id.

    The key is `session_key:request_id`, and neither half is unique over time. ACP
    request ids are REUSABLE — a provider or gateway restart resets the sequence —
    while the conversation generation only changes on `/new` or an idle/daily
    rotation. So a provider that restarts mid-conversation issues request id 1 again,
    and a button still in scrollback from before the restart carries that same id.

    Pressing it then resolves a prompt for a tool the user never read. On Approve
    that is an unrelated tool approved; on Trust it is also standing auto-approve for
    every later tool in the conversation, inherited by spawned subagents. Discord
    closed this with a per-prompt nonce; this is the same mechanism.
    """

    def setup_method(self) -> None:
        TelegramApprovalDecider._REGISTRY.clear()
        TelegramApprovalDecider._NONCES.clear()

    def test_a_reused_request_id_does_not_let_a_stale_button_resolve(self) -> None:
        # The reviewer's exact scenario, as a unit: prompt A is rendered and its
        # nonce retired; the provider restarts and prompt B reuses request id 1 in
        # the SAME conversation generation; the stale A button is pressed.
        key = "telegram:kirocrew:direct:7:1"
        stale_nonce = "stale-nonce-aaa"
        TelegramApprovalDecider.arm(key, stale_nonce)
        TelegramApprovalDecider._NONCES.pop(key, None)  # prompt A finished

        fresh_nonce = "fresh-nonce-bbb"
        TelegramApprovalDecider.arm(key, fresh_nonce)  # prompt B
        assert stale_nonce != fresh_nonce
        assert TelegramApprovalDecider.nonce_matches(key, stale_nonce) is False
        assert TelegramApprovalDecider.nonce_matches(key, fresh_nonce) is True

    @pytest.mark.asyncio
    async def test_a_stale_press_resolves_nothing_and_grants_nothing(self) -> None:
        from kiro_crew.messaging import session_trust as trust

        trust.clear_trusted_sessions()
        d, client, _ = _dispatcher({1})
        key = TelegramApprovalDecider.key(d._session_key(("direct", "1")), "r1")
        # A LIVE prompt exists under this key, with its own nonce.
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        TelegramApprovalDecider._REGISTRY[key] = fut
        TelegramApprovalDecider.arm(key, "n1")
        try:
            # A button from an EARLIER prompt: same key, wrong nonce.
            await d.on_callback(_trust_press("0" * 16))
            assert not fut.done(), "a stale press must not resolve the live prompt"
            assert not trust.is_session_trusted(
                d._session_key(("direct", "1"))
            ), "a stale press must not hand out standing auto-approve"
            assert "expired" in client.edits[-1][1]
        finally:
            TelegramApprovalDecider._REGISTRY.pop(key, None)

    @pytest.mark.asyncio
    async def test_the_matching_press_still_works(self) -> None:
        # The control: without it the test above passes for an approval path that is
        # broken for everyone.
        d, _, _ = _dispatcher({1})
        key = TelegramApprovalDecider.key(d._session_key(("direct", "1")), "r1")
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        TelegramApprovalDecider._REGISTRY[key] = fut
        nonce = "n1"
        TelegramApprovalDecider.arm(key, nonce)
        try:
            await d.on_callback(_trust_press(nonce))
            assert fut.done() and fut.result() is True
        finally:
            TelegramApprovalDecider._REGISTRY.pop(key, None)

    def test_a_pre_nonce_button_is_refused(self) -> None:
        # A button rendered by an older process has no nonce segment at all, so the
        # parse leaves part of the request id where the nonce should be. That must
        # fail closed rather than parse into something that matches.
        key = "telegram:kirocrew:direct:7:r1"
        TelegramApprovalDecider.arm(key, "n1")
        assert TelegramApprovalDecider.nonce_matches(key, "") is False
        assert TelegramApprovalDecider.nonce_matches(key, "r1") is False

    def test_the_nonce_is_retired_with_its_prompt(self) -> None:
        # Held for the prompt's lifetime only: a nonce that outlived its prompt would
        # re-open the same window on the next request id the provider reuses.
        async def _go() -> bool:
            decider = TelegramApprovalDecider(session_key="telegram:1:0")
            task = asyncio.ensure_future(decider(SimpleNamespace(request_id="rq5")))
            # flake-ok: pre-existing 20 ms yield so the decider registers its prompt, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 20 ms yield so the decider registers its prompt, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.02)
            key = "telegram:1:0:rq5"
            nonce = "n1"
            TelegramApprovalDecider.arm(key, nonce)
            TelegramApprovalDecider.resolve_global(key, True, nonce=nonce)
            # flake-ok: pre-existing unbounded await of the decider task the test resolves itself, moved verbatim; tracked for a de-flake pass
            await task
            return key in TelegramApprovalDecider._NONCES

        assert asyncio.run(_go()) is False

    @pytest.mark.asyncio
    async def test_every_button_of_one_prompt_shares_its_nonce(self) -> None:
        # Approve, Deny and Trust are one decision point. Separate nonces would mean
        # a Deny press could not retire the prompt an Approve press created.
        renderer, client = _renderer()
        renderer._last_tool = "bash"
        await renderer.on_prompt_choice([], "r1")
        data = [b["callback_data"] for row in client.sent[-1][1]["inline_keyboard"] for b in row]
        assert len({d.split(":")[2] for d in data}) == 1
