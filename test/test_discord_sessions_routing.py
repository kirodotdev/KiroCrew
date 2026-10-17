"""Discord ``!sessions``: where a turn or a button press in a resumed DM goes.

The picker refuses outside a DM, a resumed session runs under its own agent, an
option press is routed by its provenance tag and refused fail-closed when the tag
is stale, and a choice, ``!leave``, ``!link``, ``!unlink`` and ``!new`` move the
binding without stranding it.
"""

from __future__ import annotations

import discord_sessions_helpers
import pytest
from discord_sessions_helpers import (
    _bind_to_chat1,
    _dispatcher,
    _interaction,
    _log,
    _log_with_titles,
    _message,
    _picker_button,
)

from kiro_crew.discord.renderer import session_provenance_tag
from kiro_crew.messaging import session_resume as session_resume_core
from kiro_crew.messaging.link import ChannelLink

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_isolated_home = discord_sessions_helpers._isolated_home


@pytest.mark.asyncio
async def test_picker_refuses_outside_a_dm() -> None:
    """`!sessions` must not list or replay private history into a shared thread.

    The owner gate answers WHO may resume, not WHERE the result is shown: in a
    guild thread the picker's session titles and the 5-message replay would be
    readable by every member of that thread.
    """
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())

    await dispatcher.handle_message(_message("!sessions", thread_id="t9"))

    assert len(dispatcher._session_pickers) == 0
    assert sessions.mirror_links == {}
    assert any("direct message" in text for text, _ in client.sent)


@pytest.mark.asyncio
async def test_persisted_default_agent_sentinel_is_not_forwarded() -> None:
    """`agent: "default"` is a sentinel, not an agent name.

    Most dashboard sessions record `"default"`, meaning "let the backend pick".
    Forwarding it reaches ACP `session/set_mode`, which rejects it with
    `Mode 'default' not found` and fails EVERY message sent to the resumed
    session. The channel's own agent must be used instead.
    """
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    log.metadata["dashboard:chat-1"] = {"agent": "default"}
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    sessions.is_new_result = True
    await dispatcher.handle_message(_message("continue here"))

    assert sessions.last_key == "dashboard:chat-1"
    assert sessions.last_agent == "kirocrew"


@pytest.mark.asyncio
async def test_persisted_auto_agent_sentinel_is_not_forwarded() -> None:
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    log.metadata["dashboard:chat-1"] = {"agent": "Auto"}
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    sessions.is_new_result = True
    await dispatcher.handle_message(_message("continue here"))

    assert sessions.last_agent == "kirocrew"


@pytest.mark.asyncio
async def test_resumed_session_runs_under_its_own_agent() -> None:
    """A resumed session keeps its own agent, not Discord's default.

    On a cold start get_or_create applies the agent it is handed, so using the
    Discord default would run the dashboard conversation under a different system
    prompt and a different allowedTools set.
    """
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    log.metadata["dashboard:chat-1"] = {"agent": "research-agent"}
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    sessions.is_new_result = True
    await dispatcher.handle_message(_message("continue here"))

    assert sessions.last_key == "dashboard:chat-1"
    assert sessions.last_agent == "research-agent"


@pytest.mark.asyncio
async def test_resumed_session_without_recorded_agent_falls_back() -> None:
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    sessions.is_new_result = True
    await dispatcher.handle_message(_message("continue here"))

    assert sessions.last_agent == "kirocrew"


@pytest.mark.asyncio
async def test_option_press_routes_into_resumed_session() -> None:
    """An [OPTIONS:] press on a bound DM is the RESUMED session's turn content.

    The press re-dispatches with ``interpret_commands=False`` because the label is
    model-authored text that must never execute as a command — but that flag must
    not also skip resume routing. The buttons were rendered on the bound session's
    own reply (and carry its provenance tag), so the choice belongs to that
    session. Before the fix the press ran in the native DM session: the click
    answered a question nobody asked there, while the bound session kept waiting
    for its answer (e.g. a merge-approach choice for the bound session would
    green-light the native session's parked debug plan instead).
    """
    log = _log()
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await _bind_to_chat1(dispatcher, client, log)

    tag = session_provenance_tag("dashboard:chat-1")
    await dispatcher.on_interaction(_interaction(f"opt:0:{tag}", "m-opts", label="Ship option (a)"))

    assert sessions.last_key == "dashboard:chat-1"
    # The label reached the session as literal turn content, commands off.
    assert any(t.startswith("> Ship option (a)") for t, _ in client.sent)


@pytest.mark.asyncio
async def test_untagged_press_is_refused_fail_closed() -> None:
    """A pre-provenance button (bare ``opt:<i>``) is refused, never routed.

    Discord replays old components indefinitely, so an untagged press can never
    prove which session it belongs to — and routing it by current binding is
    exactly the cross-session injection this fix exists to stop (the legacy
    pass-through never ages out, so it is the same hole with a different door).
    Fail closed with a type-it-instead
    notice; no echo, no turn anywhere.
    """
    log = _log()
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await _bind_to_chat1(dispatcher, client, log)

    await dispatcher.on_interaction(_interaction("opt:0", "m-opts", label="Ship option (a)"))

    assert any("predate" in t for t, _ in client.sent)
    assert not any(t.startswith("> Ship option (a)") for t, _ in client.sent)
    assert sessions.last_key == ""  # no turn ran anywhere


@pytest.mark.asyncio
async def test_tagged_press_is_revalidated_after_idle_rotation() -> None:
    """Idle/daily rotation between the gate and the turn invalidates the tag.

    ``maybe_rotate`` can bump the native generation AFTER the pre-busy gate
    passed, so the turn would run under a key the tag never named. The gate is
    re-run against the FINAL key after rotation — the same invariant ``!new``
    enforces, applied to the reset nobody typed.
    """
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    tag = session_provenance_tag(dispatcher.current_session_key("u1"))
    scope = dispatcher._scope_id("u1")

    def _rotate_now(*args: object, **kwargs: object) -> None:
        dispatcher._conv.bump_gen(scope)

    dispatcher._conv.maybe_rotate = _rotate_now  # type: ignore[method-assign]

    await dispatcher.on_interaction(_interaction(f"opt:0:{tag}", "m-opts", label="Choice A"))

    assert any("moved away" in t for t, _ in client.sent)
    assert sessions.last_key == ""  # the rotated generation never ran the press


@pytest.mark.asyncio
async def test_stale_tagged_press_after_rebind_is_refused() -> None:
    """A button minted by session A, pressed after the DM was rebound to B, refuses.

    Discord replays old components indefinitely: A's reply keeps its live buttons
    after ``!unlink`` + ``!sessions`` rebinds the DM to B. Routing alone would send
    the press into B — injecting A's model-authored choice into an unrelated
    transcript. The provenance tag makes the press valid
    only while the conversation still targets the session that posted it.
    """
    log = _log_with_titles("Alpha plan", "Beta plan")
    log.messages["dashboard:chat-0"] = [{"role": "assistant", "content": "prior a"}]
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior b"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)

    # Bind A (chat-0) through the real picker, as the incident did.
    await dispatcher.handle_message(_message("!sessions Alpha"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    assert dispatcher._session_resume.resumed_session("c1") == "dashboard:chat-0"
    stale_tag = session_provenance_tag("dashboard:chat-0")

    # Rebind to B (chat-1): unlink, then pick again.
    await dispatcher.handle_message(_message("!unlink"))
    await dispatcher.handle_message(_message("!sessions Beta"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    assert dispatcher._session_resume.resumed_session("c1") == "dashboard:chat-1"
    sessions.last_key = ""

    await dispatcher.on_interaction(
        _interaction(f"opt:0:{stale_tag}", "m-old-opts", label="Ship option (a)")
    )

    assert any("moved away" in t for t, _ in client.sent)
    assert sessions.last_key == ""  # no turn ran in B, in A, or natively


@pytest.mark.asyncio
async def test_press_tagged_for_pre_new_conversation_is_refused() -> None:
    """``!new`` invalidates the previous conversation's buttons.

    The native key embeds the generation, so a press carrying the pre-``!new``
    tag does not match — honest, since the conversation that asked the
    question is over. Before the tag the press ran silently in the fresh
    generation, answering a question it never asked.
    """
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    old_tag = session_provenance_tag(dispatcher.current_session_key("u1"))

    await dispatcher.handle_message(_message("!new"))
    assert dispatcher.current_session_key("u1") != ""
    sessions.last_key = ""

    await dispatcher.on_interaction(_interaction(f"opt:0:{old_tag}", "m-opts", label="Choice A"))

    assert any("moved away" in t for t, _ in client.sent)
    assert sessions.last_key == ""


@pytest.mark.asyncio
async def test_stale_press_against_busy_target_is_refused_not_queued() -> None:
    """The provenance gate precedes the busy check — pinned as ordering.

    A stale press must be refused BEFORE ``is_busy`` runs: the busy path either
    queues (native) or posts the busy refusal (resumed), and a queued stale
    press would be drained and replayed WITHOUT its tag, executing unchecked
    later. It also must not leak whether the current target is mid-turn.
    """
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    old_tag = session_provenance_tag(dispatcher.current_session_key("u1"))
    await dispatcher.handle_message(_message("!new"))
    sessions.last_key = ""
    sessions.is_busy = lambda key: True  # type: ignore[method-assign]
    enqueued: list = []
    sessions.enqueue = lambda *a, **k: enqueued.append(a)  # type: ignore[attr-defined]

    await dispatcher.on_interaction(_interaction(f"opt:0:{old_tag}", "m-opts", label="Choice A"))

    assert any("moved away" in t for t, _ in client.sent)
    assert not any("busy" in t for t, _ in client.sent)
    assert enqueued == []
    assert sessions.last_key == ""


@pytest.mark.asyncio
async def test_valid_tagged_press_against_busy_target_is_refused_not_queued_or_steered() -> None:
    """A VALID press whose target is mid-turn refuses — it never enters the queue.

    ``_handle_busy`` enqueues bare text (or steers it mid-turn), and the drain
    replays queued text WITHOUT the provenance tag — so a ``!new`` or idle
    rotation between enqueue and drain would execute the model-authored choice
    in a conversation the tag never named. The busy path is therefore
    unreachable for any tagged press.
    """
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    tag = session_provenance_tag(dispatcher.current_session_key("u1"))
    sessions.is_busy = lambda key: True  # type: ignore[method-assign]
    enqueued: list = []
    sessions.enqueue = lambda *a, **k: enqueued.append(a)  # type: ignore[attr-defined]
    steered: list = []
    sessions.steer = lambda *a, **k: steered.append(a)  # type: ignore[attr-defined]

    await dispatcher.on_interaction(_interaction(f"opt:0:{tag}", "m-opts", label="Choice A"))

    assert any("busy" in t and "NOT applied" in t for t, _ in client.sent)
    assert enqueued == [] and steered == []
    assert sessions.last_key == ""  # no turn ran anywhere


@pytest.mark.asyncio
async def test_option_press_after_binding_destroyed_is_refused_not_native() -> None:
    """A press whose binding died mid-flight gets the Detached refusal.

    The refusal is the honest outcome routing already produces for a typed
    message; the press must not fall back to a silent native turn, which is the
    exact misroute the routing gate exists to stop.
    """
    log = _log()
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await _bind_to_chat1(dispatcher, client, log)
    stale_tag = session_provenance_tag("dashboard:chat-1")
    # The binding dies out from under the DM (a housekeeping clear, not !unlink).
    sessions.clear_mirror_link("dashboard:chat-1")

    await dispatcher.on_interaction(
        _interaction(f"opt:0:{stale_tag}", "m-opts", label="Ship option (a)")
    )

    assert any("Detached" in t for t, _ in client.sent)
    assert sessions.last_key == ""  # no turn ran anywhere


@pytest.mark.asyncio
async def test_synthetic_dispatch_without_origin_tag_keeps_native_affinity() -> None:
    """Untagged synthetic turns keep the legacy skip: native session, no routing.

    Queue drains replay messages the native session accepted while busy, and
    AutoNudge fires target the native key their loop rotation-checked — routing
    either into a binding created later would run them in a session that never
    queued or armed them. This pins that ``interpret_commands=False`` with no
    ``origin_tag`` still means "skip routing" for those callers.
    """
    log = _log()
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await _bind_to_chat1(dispatcher, client, log)

    await dispatcher.handle_message(_message("nudge cycle text"), interpret_commands=False)

    assert sessions.last_key != "dashboard:chat-1"
    assert sessions.last_key.startswith("discord")


@pytest.mark.asyncio
async def test_stale_picker_fails_closed(monkeypatch) -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    # Expire by moving the TTL, not by reaching into a registry entry: the constant is
    # the contract, and a test that ages private state passes even if the TTL stops
    # being consulted.
    monkeypatch.setattr(session_resume_core, "PICKER_TTL_SECS", -1)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    assert sessions.mirror_links == {}
    assert any("expired" in text for _, text, _ in client.edits)


@pytest.mark.asyncio
async def test_picker_nonce_is_bound_to_its_registered_choices() -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    await dispatcher.handle_message(_message("!sessions"))
    _, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction("s:not-the-picker:0", message_id))

    assert sessions.mirror_links == {}
    assert any("expired" in text for _, text, _ in client.edits)


@pytest.mark.asyncio
async def test_choice_refuses_session_linked_elsewhere() -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    sessions.set_mirror_link(
        "dashboard:chat-1",
        ChannelLink(channel_type="telegram", channel_id="other"),
    )
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    assert sessions.mirror_links["dashboard:chat-1"].channel_type == "telegram"
    assert any("already active on Telegram" in text for _, text, _ in client.edits)


@pytest.mark.asyncio
async def test_choice_refuses_occupied_discord_conversation() -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    sessions.set_mirror_link(
        "dashboard:other",
        ChannelLink(channel_type="discord", channel_id="c1"),
        accepts_inbound=True,
    )
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    assert "dashboard:chat-1" not in sessions.mirror_links
    assert any("!unlink" in text for _, text, _ in client.edits)


@pytest.mark.asyncio
async def test_choice_refusal_for_outbound_mirror_names_unlink() -> None:
    # An outbound-only occupant would get "Unlink the existing dashboard
    # mirror first" — an instruction with no in-channel action. `!unlink` now
    # clears outbound mirrors by location, so the guidance is unified and must
    # name the command for BOTH occupant kinds.
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    sessions.set_mirror_link(
        "dashboard:other",
        ChannelLink(channel_type="discord", channel_id="c1"),
    )
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    assert "dashboard:chat-1" not in sessions.mirror_links
    assert any("Run `!unlink` first" in text for _, text, _ in client.edits)
    # And the instruction is followable: the sweep frees the location, after
    # which the conflict check does not refuse.
    sessions.clear_mirror_links_at(ChannelLink(channel_type="discord", channel_id="c1"))
    conflict = dispatcher._session_resume._binder.binding_conflict(
        "dashboard:chat-1",
        "chat one",
        ChannelLink(channel_type="discord", channel_id="c1"),
    )
    assert conflict is None


@pytest.mark.asyncio
async def test_leave_resumed_session_frees_whole_location() -> None:
    # The resumed-session release must clear co-located occupants too. A session
    # map can hold that state — written before conversations became exclusive, or
    # hand-edited — and the release has to free all of it. `set_mirror_link`
    # refuses to create it, so the rows go in directly.
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    loc = ChannelLink(channel_type="discord", channel_id="c1")
    sessions.mirror_links["dashboard:resumed"] = loc
    sessions.inbound_keys.add("dashboard:resumed")
    sessions.mirror_links["dashboard:bystander"] = loc

    released = await dispatcher._session_resume.leave_resumed_session("c1")

    assert released == "dashboard:resumed"
    assert sessions.mirror_links == {}


@pytest.mark.asyncio
async def test_outbound_only_mirror_does_not_hijack_inbound_turn() -> None:
    dispatcher, _, sessions = _dispatcher({"u1"}, _log())
    sessions.set_mirror_link(
        "dashboard:chat-1",
        ChannelLink(channel_type="discord", channel_id="c1"),
    )

    await dispatcher.handle_message(_message("hello"))

    assert sessions.last_key == dispatcher._session_key("u1")


@pytest.mark.asyncio
async def test_link_refuses_while_resumed_instead_of_stranding_binding() -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    link = ChannelLink(channel_type="discord", channel_id="c1")
    sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)

    await dispatcher.handle_message(_message("!link"))

    # The resumed binding is untouched and still the inbound route.
    assert sessions.find_mirror_sessions(link, inbound_only=True) == ["dashboard:chat-1"]
    assert "!unlink" in client.sent[-1][0]


@pytest.mark.asyncio
async def test_unlink_returns_to_native_discord_context() -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    sessions.set_mirror_link(
        "dashboard:chat-1",
        ChannelLink(channel_type="discord", channel_id="c1"),
        accepts_inbound=True,
    )

    await dispatcher.handle_message(_message("!unlink"))
    await dispatcher.handle_message(_message("hello"))

    assert "dashboard:chat-1" not in sessions.mirror_links
    assert "Back to your Discord conversation" in client.sent[0][0]
    assert sessions.last_key == dispatcher._session_key("u1")


@pytest.mark.asyncio
async def test_new_leaves_resumed_session_and_advances_native_generation() -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    old_key = dispatcher._session_key("u1")
    sessions.set_mirror_link(
        "dashboard:chat-1",
        ChannelLink(channel_type="discord", channel_id="c1"),
        accepts_inbound=True,
    )

    await dispatcher.handle_message(_message("!new"))

    assert "dashboard:chat-1" not in sessions.mirror_links
    assert dispatcher._session_key("u1") != old_key
    assert "left the resumed session" in client.sent[-1][0]
