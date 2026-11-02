"""The per-channel policy (``allowed_channel_ids``) on every owner control of a code channel.

Each control refuses a channel the policy refuses before any side effect, and
again past its owner check: the Approval mode select and ``/kirocrew trust``,
"Trust this channel", a worker card's Approve / Reject / Trust (including the
hand answer after a mode change), and the worker card relay itself. The mode
change's own grant-time fence is pinned in
``test_slack_code_channel_approval_prepush`` (``..._refused_mid_...``), and the
agent picker's select and ``/kirocrew agent`` in
``test_slack_code_channel_agent_picker``.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
import test_slack_code_channel_trust_from_slack as trust_from_slack
from slack_code_channel_surface_helpers import CH, SLOT, env_fixture  # noqa: F401
from slack_code_channels_helpers import OWNER
from test_slack_code_channel_approval_select import _pick, _run_slash
from test_slack_code_channel_trust_from_slack import REQUEST, chan  # noqa: F401
from test_slack_code_channel_worker_approvals import _click, _run_relay, relay  # noqa: F401

from kiro_crew.slack.code_channels import agent_picker, approval_picker, commands
from kiro_crew.slack.code_channels import worker_approvals as wa
from kiro_crew.slack.handler import post_linked_approval


def _refuse(monkeypatch: pytest.MonkeyPatch, *, after: list[bool] | None = None) -> None:
    """The policy refuses CH (from the start, or once *after* holds [True])."""
    from kiro_crew.slack import channel_policy

    def verdict(channel: str) -> str:
        if channel == CH and (after is None or after[0]):
            return "slack channel refused by the security policy"
        return ""

    monkeypatch.setattr(channel_policy, "_verdict", verdict)


def _refuse_during_authorize(monkeypatch: pytest.MonkeyPatch) -> None:
    """The policy narrows as authorize returns (it admitted the owner), so only
    the control's own commit-time check can refuse."""
    flag = [False]
    _refuse(monkeypatch, after=flag)
    real = agent_picker.authorize

    async def admitted_then_narrowed(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        flag[0] = True
        return out

    monkeypatch.setattr(agent_picker, "authorize", admitted_then_narrowed)


# ── The Approval mode select and /kirocrew trust ──────────────────────────


def _menu(mode: str) -> dict[str, Any]:
    """A pick in the channel's own kickoff (its stored anchor)."""
    from kiro_crew.slack.code_channels.store import store

    binding = store().get_binding(CH)
    assert binding is not None and binding.anchor_ts
    return _pick(mode, ts=binding.anchor_ts)


def test_the_approval_menu_on_a_refused_channel_changes_and_posts_nothing(
    chan: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    _refuse(monkeypatch)
    assert asyncio.run(approval_picker.handle_select(chan.orch, _menu("trust")))
    assert chan.slot._trust is False
    assert chan.slack.ephemerals == [] and chan.slack.edits == []


@pytest.mark.parametrize("mode", ["trust", "trust_reads"])
def test_the_approval_menu_refused_while_authorized_changes_nothing(
    chan: Any, monkeypatch: pytest.MonkeyPatch, mode: str  # noqa: F811
) -> None:
    from test_slack_code_channel_approval_select import _confirm_click

    # The pick asks privately; the policy narrows as the confirmation's own
    # authorize returns, so only the change's own gates can refuse.
    asyncio.run(approval_picker.handle_select(chan.orch, _menu(mode)))
    click = _confirm_click(chan)
    _refuse_during_authorize(monkeypatch)
    asyncio.run(approval_picker.handle_confirm(chan.orch, click))
    assert approval_picker.current_mode(chan.slot) == "normal"
    # Refused at the change's own gates, not as "not the kickoff".
    assert not [
        r
        for r in chan.sel.ops(approval_picker.OPERATION)
        if "not a code channel kickoff" in r.get("error", "")
    ]


def test_kirocrew_trust_on_a_refused_channel_changes_nothing(
    chan: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    _refuse(monkeypatch)
    _run_slash(chan, "approval trust")
    assert chan.slot._trust is False


def test_kirocrew_trust_refused_after_its_owner_gate_changes_nothing(
    chan: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Past the command's owner gate, the change's own gates refuse."""
    from kiro_crew.slack.code_channels.store import store

    _refuse(monkeypatch)
    replies: list[str] = []

    async def respond(text: str) -> None:
        replies.append(text)

    async def go() -> None:
        binding = await asyncio.to_thread(store().get_binding, CH)
        await approval_picker.handle_command(chan.orch, binding, OWNER, "all", respond, team="T1")

    asyncio.run(go())
    assert chan.slot._trust is False and replies


# ── "Trust this channel" on a channel card ────────────────────────────────


def _channel_card(e: Any) -> str:
    return asyncio.run(
        post_linked_approval(
            e.slack, CH, None, REQUEST, f"dashboard:{SLOT}", "Run shell", "ls", channel_trust=True
        )
    )


def test_trust_this_channel_refused_while_authorized_changes_nothing(
    chan: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    ts = _channel_card(chan)
    _refuse_during_authorize(monkeypatch)

    async def go() -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        chan.slot._approval_futures[REQUEST] = fut
        await approval_picker.handle_trust_call(chan.orch, trust_from_slack._click(OWNER, ts))
        return fut

    fut = asyncio.run(go())
    assert not fut.done() and chan.slot._trust is False


# ── A worker card's buttons, and the relay that posts it ──────────────────


@pytest.mark.parametrize(
    "action_id", [wa.APPROVE_ACTION_ID, wa.REJECT_ACTION_ID, wa.TRUST_ACTION_ID]
)
def test_a_worker_card_refused_while_authorized_answers_nothing(
    relay: Any, monkeypatch: pytest.MonkeyPatch, action_id: str  # noqa: F811
) -> None:
    async def go() -> asyncio.Future:
        card, fut = await _run_relay(relay)
        _refuse_during_authorize(monkeypatch)
        await wa.handle_click(relay.orch, _click(OWNER, card, action_id))
        return fut

    fut = asyncio.run(go())
    assert not fut.done()
    assert relay.slot._trust is False and relay.worker._trust is False


def test_the_relay_posts_no_card_into_a_refused_channel(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    _refuse(monkeypatch)
    before = len(relay.slack.blocks_posts)

    async def go() -> Any:
        card, _fut = await _run_relay(relay)
        return card

    assert asyncio.run(go()) is None
    assert len(relay.slack.blocks_posts) == before


# ── Every owner control reaches the guard at entry and at commit ──────────


def test_every_owner_control_meets_the_policy_at_entry_and_commit() -> None:
    """A control enters through the dispatcher or the command's owner gate and
    ``authorize`` (each applying ``channel_policy_refusal``), and commits
    through a fence that re-reads it (``_sync_gates``)."""
    from kiro_crew.slack import interactions

    def src(func: Any) -> str:
        return inspect.getsource(func)

    # Entry: the routers apply the policy before handing over.
    assert src(interactions.dispatch).count("channel_refusal(channel") >= 2  # pick, Archive
    assert "channel_refusal(channel" in src(wa.dispatch_card_click)
    assert "channel_refusal(channel" in src(agent_picker._still_admitted)
    assert "channel_policy_refusal" in src(commands.handle_slash)
    assert "channel_policy_refusal" in src(agent_picker.authorize)
    assert "channel_policy_refusal" in src(agent_picker._sync_gates)
    # Each control: authorize first, then a commit fence through _sync_gates.
    controls = {
        agent_picker.handle_select: "switch_channel_agent(",
        approval_picker.handle_select: "_apply_pick(",
        approval_picker.handle_confirm: "_apply_pick(",
        approval_picker.handle_trust_call: "set_channel_mode(",
        wa.handle_click: "_sync_gates(",
    }
    for control, commit in controls.items():
        assert "authorize(" in src(control), control.__qualname__
        assert commit in src(control), control.__qualname__
    assert "_sync_gates(" in src(agent_picker.switch_channel_agent)
    assert "_sync_gates(" in src(approval_picker.set_channel_mode)
    assert "switch_channel_agent(" in src(agent_picker.handle_command)
    assert "set_channel_mode(" in src(approval_picker.handle_command)
    assert "set_channel_mode(" in src(approval_picker._apply_pick)
    # A worker card's Reject and the relay apply it themselves.
    assert "channel_policy_refusal" in src(wa.handle_click)
    assert "channel_policy_refusal" in src(wa.relay)


def _narrow_after_the_mode_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """The policy narrows once set_channel_mode returns, before the click's own
    hand answer: only that answer's own re-check can refuse."""
    flag = [False]
    _refuse(monkeypatch, after=flag)
    real = approval_picker.set_channel_mode

    async def then_narrowed(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        flag[0] = True
        return out

    monkeypatch.setattr(approval_picker, "set_channel_mode", then_narrowed)


def test_trust_this_channel_answers_nothing_once_refused_after_the_mode_change(
    chan: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The channel already holds Trust, so the click answers its call by hand."""
    chan.slot._trust = True
    ts = _channel_card(chan)
    _narrow_after_the_mode_change(monkeypatch)

    async def go() -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        chan.slot._approval_futures[REQUEST] = fut
        await approval_picker.handle_trust_call(chan.orch, trust_from_slack._click(OWNER, ts))
        return fut

    assert not asyncio.run(go()).done()


def test_a_worker_cards_trust_answers_nothing_once_refused_after_the_mode_change(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The channel already holds Trust and the worker keeps its own mode, so no
    sweep answers the card: the click's hand answer re-checks the policy."""
    relay.slot._trust = True
    relay.worker._posture_inherited = False

    async def go() -> asyncio.Future:
        card, fut = await _run_relay(relay)
        _narrow_after_the_mode_change(monkeypatch)
        await wa.handle_click(relay.orch, _click(OWNER, card, wa.TRUST_ACTION_ID))
        return fut

    assert not asyncio.run(go()).done()


# ── Every /kirocrew command in a refused channel does nothing ─────────────


@pytest.mark.parametrize(
    "text",
    ["agent builder", "agent", "approval trust", "approval normal", "approval"],
)
def test_a_kirocrew_command_in_a_refused_channel_does_nothing(
    chan: Any, monkeypatch: pytest.MonkeyPatch, text: str  # noqa: F811
) -> None:
    before = (chan.slot._trust, chan.slot._trust_reads, getattr(chan.slot, "agent", None))
    _refuse(monkeypatch)
    replies = _run_slash(chan, text)
    assert replies == []  # dropped without a reply, as every inbound gate drops
    after = (chan.slot._trust, chan.slot._trust_reads, getattr(chan.slot, "agent", None))
    assert after == before and chan.routed == []


@pytest.mark.parametrize("command", ["/summarize", "/wrap-up", "/create-pr"])
def test_a_channel_command_in_a_refused_channel_does_nothing(
    chan: Any, monkeypatch: pytest.MonkeyPatch, command: str  # noqa: F811
) -> None:
    from slack_code_channel_surface_helpers import _slash

    _refuse(monkeypatch)
    replies: list[str] = []

    async def respond(text: str) -> None:
        replies.append(text)

    assert asyncio.run(commands.handle_slash(chan.orch, _slash(command), respond))
    assert replies == [] and chan.routed == []


@pytest.mark.parametrize("refused", [False, True])
def test_a_channels_governance_deny_keeps_a_command_silent(
    chan: Any, monkeypatch: pytest.MonkeyPatch, refused: bool  # noqa: F811
) -> None:
    """Slack denied by the channels policy: no reply, refused channel or not."""
    from kiro_crew.messaging import identity

    async def denied(_channel: str) -> bool:
        return False

    monkeypatch.setattr(identity, "channel_inbound_permitted", denied)
    if refused:
        _refuse(monkeypatch)
    assert _run_slash(chan, "approval trust") == []
    assert chan.slot._trust is False


# ── Worker cards go through the shared display sink ───────────────────────


def test_a_worker_cards_text_goes_through_the_display_sink(relay: Any) -> None:  # noqa: F811
    """A secret-shaped worker title (credential and exfiltration URL) is redacted
    on the card it relays, and a mention in it is defanged."""
    import json

    key = "AKIA" + "QWERTYUIOPASDFGH"
    url = (
        "https://attacker.example/collect?data="
        + "c2VjcmV0LXRva2VuLXZhbHVlLWhlcmUtMTIzNDU2Nzg5MA=="
    )
    relay.worker.title = f"Fix {key} AKIA**ZXCVBNMASDFGHJKL** <!channel> {url}"

    async def go() -> Any:
        card, _fut = await _run_relay(relay)
        return card

    assert asyncio.run(go()) is not None
    _channel, blocks, fallback, _thread = relay.slack.blocks_posts[-1]
    shown = json.dumps(blocks, ensure_ascii=False) + fallback
    assert key not in shown and "attacker.example/collect" not in shown
    assert "ZXCVBNMASDFGHJKL" not in shown  # only the display-form scan finds this one
    assert "[REDACTED" in shown and "<!channel>" not in shown


# ── A worker's lineage must be one the gateway vouches for ────────────────


def _forge(worker: Any) -> None:
    """A ``_created_by`` read back from an editable transcript: unminted, and no
    crew-log creation record names it."""
    worker._lineage_minted = False


def test_a_forged_creator_is_never_relayed(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from kiro_crew.crew_log import session_tree_projection

    monkeypatch.setattr(session_tree_projection, "creator_of", lambda _slot: "")
    _forge(relay.worker)
    before = len(relay.slack.blocks_posts)

    async def go() -> Any:
        card, _fut = await _run_relay(relay)
        return card

    assert asyncio.run(go()) is None
    assert len(relay.slack.blocks_posts) == before


def test_a_crew_log_creation_record_still_counts(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Unminted after a restart, but the crew log records the edge: relayed."""
    from kiro_crew.crew_log import session_tree_projection

    _forge(relay.worker)
    monkeypatch.setattr(
        session_tree_projection, "creator_of", lambda slot: SLOT if slot == relay.worker.key else ""
    )

    async def go() -> Any:
        card, _fut = await _run_relay(relay)
        return card

    assert asyncio.run(go()) is not None


def test_a_linked_session_is_never_this_channels_worker(relay: Any) -> None:  # noqa: F811
    relay.worker.linked_session_key = "chat-elsewhere"

    async def go() -> Any:
        card, _fut = await _run_relay(relay)
        return card

    assert asyncio.run(go()) is None


@pytest.mark.parametrize(
    "action_id", [wa.APPROVE_ACTION_ID, wa.REJECT_ACTION_ID, wa.TRUST_ACTION_ID]
)
def test_a_card_whose_worker_lineage_no_longer_verifies_answers_nothing(
    relay: Any, monkeypatch: pytest.MonkeyPatch, action_id: str  # noqa: F811
) -> None:
    """A card posted for a verified worker, whose lineage stops verifying before
    the click (a restart restored the slot from its transcript): no answer."""
    from kiro_crew.crew_log import session_tree_projection

    monkeypatch.setattr(session_tree_projection, "creator_of", lambda _slot: "")

    async def go() -> asyncio.Future:
        card, fut = await _run_relay(relay)
        _forge(relay.worker)
        await wa.handle_click(relay.orch, _click(OWNER, card, action_id))
        return fut

    fut = asyncio.run(go())
    assert not fut.done()
    assert relay.slot._trust is False and relay.worker._trust is False


def test_a_trust_click_rechecks_the_lineage_before_its_hand_answer(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The lineage stops verifying while the mode change awaits: the click's own
    hand answer after it refuses."""
    from kiro_crew.crew_log import session_tree_projection

    monkeypatch.setattr(session_tree_projection, "creator_of", lambda _slot: "")
    relay.slot._trust = True  # the channel already holds Trust: no sweep answers it
    relay.worker._posture_inherited = False
    real = approval_picker.set_channel_mode

    async def then_forged(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        _forge(relay.worker)
        return out

    monkeypatch.setattr(approval_picker, "set_channel_mode", then_forged)

    async def go() -> asyncio.Future:
        card, fut = await _run_relay(relay)
        await wa.handle_click(relay.orch, _click(OWNER, card, wa.TRUST_ACTION_ID))
        return fut

    assert not asyncio.run(go()).done()


@pytest.mark.parametrize("action_id", [wa.APPROVE_ACTION_ID, wa.REJECT_ACTION_ID])
def test_a_lineage_that_stops_verifying_during_authorize_answers_nothing(
    relay: Any, monkeypatch: pytest.MonkeyPatch, action_id: str  # noqa: F811
) -> None:
    """Re-checked after authorize's awaits, with nothing awaited before the answer."""
    from kiro_crew.crew_log import session_tree_projection

    monkeypatch.setattr(session_tree_projection, "creator_of", lambda _slot: "")
    real = agent_picker.authorize

    async def admitted_then_forged(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        _forge(relay.worker)
        return out

    monkeypatch.setattr(agent_picker, "authorize", admitted_then_forged)

    async def go() -> asyncio.Future:
        card, fut = await _run_relay(relay)
        await wa.handle_click(relay.orch, _click(OWNER, card, action_id))
        return fut

    assert not asyncio.run(go()).done()


def test_a_trust_click_whose_lineage_stops_verifying_mid_grant_changes_nothing(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The card's own fence inside the mode change refuses the grant."""
    from test_slack_code_channel_mode_reaches_workers import _during_grant

    from kiro_crew.crew_log import session_tree_projection

    monkeypatch.setattr(session_tree_projection, "creator_of", lambda _slot: "")
    _during_grant(relay, monkeypatch, 1, lambda: _forge(relay.worker))

    async def go() -> asyncio.Future:
        card, fut = await _run_relay(relay)
        await wa.handle_click(relay.orch, _click(OWNER, card, wa.TRUST_ACTION_ID))
        return fut

    fut = asyncio.run(go())
    assert not fut.done() and relay.slot._trust is False


def test_a_worker_adopted_while_the_relay_awaits_is_not_posted(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The lineage is checked again right before the post."""
    from kiro_crew.slack.code_channels import surface

    real = surface.outbound_permitted

    async def adopted_meanwhile() -> bool:
        relay.worker._created_by = "chat-elsewhere-1"  # another session adopts it
        return await real()

    monkeypatch.setattr(surface, "outbound_permitted", adopted_meanwhile)
    before = len(relay.slack.blocks_posts)

    async def go() -> Any:
        card, _fut = await _run_relay(relay)
        return card

    assert asyncio.run(go()) is None
    assert len(relay.slack.blocks_posts) == before


def test_a_lowering_that_misses_an_unverified_session_does_not_name_it(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """A session whose lineage the gateway cannot vouch for is lowered like any
    other, but the reply never shows its title."""
    from kiro_crew.crew_log import session_tree_projection

    monkeypatch.setattr(session_tree_projection, "creator_of", lambda _slot: "")
    relay.slot._trust = True
    relay.worker._trust = True
    relay.worker._posture_inherited = False
    relay.worker.title = "Someone else's secret project"
    _forge(relay.worker)
    real = approval_picker._apply

    async def refuse_worker(
        state: Any, slot: Any, mode: str, user: str, still_live: Any = None, **kw: Any
    ) -> Any:
        if slot is relay.worker:
            return False, "the change was refused"
        return await real(state, slot, mode, user, still_live, **kw)

    monkeypatch.setattr(approval_picker, "_apply", refuse_worker)

    async def go() -> Any:
        from kiro_crew.slack.code_channels.store import store

        binding = await asyncio.to_thread(store().get_binding, CH)
        return await approval_picker.set_channel_mode(
            binding, "normal", OWNER, via="command", team="T1"
        )

    out = asyncio.run(go())
    assert "Couldn't change 1 running worker(s): a session" in out.text
    assert "secret project" not in out.text


def test_a_mirror_paused_while_the_relay_awaits_is_not_posted(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The channel's egress gates are asked again right before the post."""
    from kiro_crew.dashboard import chat_utils
    from kiro_crew.slack.code_channels import surface

    paused = [False]
    real_paused = chat_utils.slack_mirror_is_paused

    def mirror_paused(state: Any, key: str) -> bool:
        return paused[0] or real_paused(state, key)

    monkeypatch.setattr(chat_utils, "slack_mirror_is_paused", mirror_paused)
    real = surface.outbound_permitted

    async def paused_meanwhile() -> bool:
        paused[0] = True  # the operator disconnects the channel's mirror
        return await real()

    monkeypatch.setattr(surface, "outbound_permitted", paused_meanwhile)
    before = len(relay.slack.blocks_posts)

    async def go() -> Any:
        card, _fut = await _run_relay(relay)
        return card

    assert asyncio.run(go()) is None
    assert len(relay.slack.blocks_posts) == before


def test_wrapups_text_goes_through_the_one_display_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wrapup's agent-surface text is sanitized by ``surface.display_text``, the
    same function every other code-channel surface uses, never a second spelling."""
    from kiro_crew.slack.code_channels import surface, wrapup

    seen: list[tuple[str, bool]] = []

    def marker(text: str, *, mentions: bool = True) -> str:
        seen.append((text, mentions))
        return f"<<{text}>>"

    monkeypatch.setattr(surface, "display_text", marker)
    assert wrapup._redact("summary") == "<<summary>>"
    assert wrapup._redact_payload("<!DOCTYPE html>") == "<<<!DOCTYPE html>>>"
    assert seen == [("summary", True), ("<!DOCTYPE html>", False)]
