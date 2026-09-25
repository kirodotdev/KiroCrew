"""Channel-side delivery of the spawn-approval prompt on Discord.

The host spawn gate's own surfaces are a Slack owner DM and the dashboard, so a
spawn parented on a Discord conversation reaches neither and is refused fast
(``no_approval_surface``) unless this channel offers the prompt itself. This
suite covers Discord's implementation of the channel-neutral delivery seam the
host gate consults FIRST, over the same Approve/Deny buttons and nonce guard the
main-agent tool ladder already uses here.

What is pinned:

* the SAFETY BOUNDARY — a channel with no registered hook still fails fast, and
  the host gate's behaviour there is byte-for-byte what it was without any of
  this: the seam answers ``None`` and ``SpawnApprovalUnreachable`` is raised;
* FALL THROUGH, NEVER DENY, whenever the prompt did not reach anyone: a send
  that raises, a send this client refuses by returning no message id, a
  destination whose authorization was withdrawn, and a channel the operator's
  channels ceiling denies all answer ``None`` (the gate can still offer the spawn
  on Slack/dashboard) rather than ``False`` (a decision nobody made). The armed
  nonce is retired in each case;
* ROUTING — a thread route prompts in the thread, a direct route opens the peer's
  DM channel, and a key this channel cannot address falls through;
* RESOLUTION — Approve resolves ``True`` and Deny ``False`` through the existing
  ``on_interaction`` ``a:`` path, under the key a press recomputes.

All Discord client I/O is faked; nothing touches the network.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

# Reuse the fixtures the existing Discord suite uses (fake client, fake sessions,
# dispatcher factory) so this suite exercises the SAME doubles.
from test_discord import _dispatcher  # noqa: E402

from kiro_crew.discord import transport_dispatch as dispatch_mod
from kiro_crew.discord.client import DiscordInteraction
from kiro_crew.discord.renderer import DiscordApprovalDecider
from kiro_crew.messaging import spawn_approval_delivery as seam
from kiro_crew.messaging.display_safety import canonicalize_display
from kiro_crew.subagent import SpawnApprovalUnreachable

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.fixture(autouse=True)
def _clean_seam():
    """Every test starts and ends with an empty registry and no armed nonces."""
    seam.clear_channel_delivery_hooks()
    DiscordApprovalDecider._REGISTRY.clear()
    DiscordApprovalDecider._NONCES.clear()
    DiscordApprovalDecider._DETACHED.clear()
    yield
    seam.clear_channel_delivery_hooks()
    DiscordApprovalDecider._REGISTRY.clear()
    DiscordApprovalDecider._NONCES.clear()
    DiscordApprovalDecider._DETACHED.clear()


@pytest.fixture(autouse=True)
def _governance_permits(monkeypatch):
    """Default premise for every test here: the channels ceiling permits Discord.

    The real check hops to a thread pool, so leaving it in place would couple
    these tests to an executor round-trip instead of to the delivery behaviour
    they are about. Patched in BOTH namespaces that read it -- the seam, which
    gates entry into any hook, and this dispatcher, which re-reads across the DM
    open -- because a test that reaches the send has to pass both.
    """

    async def _permitted(_channel: str) -> bool:
        return True

    monkeypatch.setattr(dispatch_mod, "channel_inbound_permitted", _permitted)
    monkeypatch.setattr(seam, "channel_inbound_permitted", _permitted)


def _itx(custom_id: str, *, channel: str = "c1", guild: str = "") -> DiscordInteraction:
    return DiscordInteraction(
        interaction_id="i1",
        interaction_token="tok",
        channel_id=channel,
        user_id="u1",
        message_id="m1",
        custom_id=custom_id,
        label="",
        guild_id=guild,
    )


def _armed_nonces() -> dict[str, str]:
    return dict(DiscordApprovalDecider._NONCES)


def _pressed_ids(components: Any) -> list[str]:
    return [b["custom_id"] for row in components for b in row["components"]]


# ── The safety boundary: no hook registered ────────────────────────────────


@pytest.mark.asyncio
async def test_a_channel_with_no_hook_still_falls_through_to_the_host_gate() -> None:
    """The unchanged path. With nothing registered the seam answers ``None``.

    This is the whole safety argument for the change: a channel that has not
    opted in behaves exactly as it does with no delivery seam at all, so the
    host gate reaches its own Slack-DM/dashboard surfaces and, with neither
    attached, refuses fast.
    """
    decision = await seam.deliver_spawn_approval(
        "spawn:a1", "spawn_run(task)", "discord:kc:direct:u1"
    )
    assert decision is None


@pytest.mark.asyncio
async def test_the_host_gate_still_raises_unreachable_behind_an_unregistered_channel() -> None:
    """Non-vacuity for the test above: the refusal itself is still reached.

    Stands in for the host gate's ordering — consult the seam, then fall through
    to the surface check that raises when nothing is attached.
    """

    async def _host_gate(_request_id: str, _description: str, _parent: str) -> bool:
        raise SpawnApprovalUnreachable("no dashboard client is connected")

    async def _on_spawn_approval(request_id: str, description: str, parent: str) -> bool:
        channel_decision = await seam.deliver_spawn_approval(request_id, description, parent)
        if channel_decision is not None:
            return channel_decision
        return await _host_gate(request_id, description, parent)

    with pytest.raises(SpawnApprovalUnreachable):
        await _on_spawn_approval("spawn:a1", "spawn_run(task)", "discord:kc:direct:u1")


# ── Fall through, never deny ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_refused_send_falls_through_instead_of_denying() -> None:
    """This client reports a failed send by returning NO message id, not by raising.

    Waiting on that prompt would spend the whole deny-by-default window and then
    report a denial for a prompt nobody ever saw, so the absent id has to be read
    as "not surfaced here".
    """
    d, cli, _ = _dispatcher({"u1"})
    cli.fail_sends = True
    key = d._session_key("u1")

    decision = await d.deliver_spawn_approval("spawn:a1", "spawn_run(task)", key)

    assert decision is None, "a prompt that never landed must not answer for the user"
    assert decision is not False
    assert _armed_nonces() == {}, "the nonce armed for a prompt that never went out must be retired"


@pytest.mark.asyncio
async def test_a_raising_send_falls_through_instead_of_denying() -> None:
    """The other half of the same rule, for a client that raises instead."""
    d, cli, _ = _dispatcher({"u1"})

    async def _boom(*_a: Any, **_kw: Any) -> str:
        raise RuntimeError("discord is down")

    cli.send_message = _boom  # type: ignore[assignment]
    decision = await d.deliver_spawn_approval("spawn:a1", "spawn_run(task)", d._session_key("u1"))

    assert decision is None
    assert _armed_nonces() == {}


@pytest.mark.asyncio
async def test_a_destination_no_longer_authorized_falls_through_unposted() -> None:
    """The roster is re-read at delivery, not trusted from the originating turn.

    A spawn can be held for as long as its approval takes, so the peer may have
    been dropped from the allow-list in between. The prompt carries a task
    preview, so it must not be posted to a peer the rosters now refuse.
    """
    d, cli, _ = _dispatcher({"u1"})
    key = d._session_key("u1")
    d._allowed.clear()  # the operator withdrew the peer mid-approval

    decision = await d.deliver_spawn_approval("spawn:a1", "spawn_run(task)", key)

    assert decision is None
    assert cli.sent == [], "nothing may be posted to a destination the rosters refuse"
    assert _armed_nonces() == {}


@pytest.mark.asyncio
async def test_a_thread_dropped_from_the_roster_falls_through_unposted() -> None:
    """Same rule on the thread route, which is judged by a different roster."""
    d, cli, _ = _dispatcher({"u1"}, allowed_threads={"t1"})
    key = d._session_key("u1", "t1")
    d._allowed_threads.clear()

    decision = await d.deliver_spawn_approval("spawn:a1", "spawn_run(task)", key)

    assert decision is None
    assert cli.sent == []


@pytest.mark.asyncio
async def test_egress_revocation_falls_through_unposted() -> None:
    """``may_send_to`` is consulted as well, and a raise is read as revoked."""
    d, cli, _ = _dispatcher({"u1"})
    key = d._session_key("u1")
    d.transport = SimpleNamespace(may_send_to=lambda *_a, **_kw: False)  # type: ignore[assignment]

    assert await d.deliver_spawn_approval("spawn:a1", "spawn_run(t)", key) is None
    assert cli.sent == []

    def _raises(*_a: Any, **_kw: Any) -> bool:
        raise RuntimeError("roster unreadable")

    d.transport = SimpleNamespace(may_send_to=_raises)  # type: ignore[assignment]
    assert await d.deliver_spawn_approval("spawn:a2", "spawn_run(t)", key) is None
    assert cli.sent == []


@pytest.mark.asyncio
async def test_a_governance_denied_channel_never_reaches_this_hook(monkeypatch) -> None:
    """Entry is the seam's gate, and a deny there must not reach this dispatcher.

    The seam reads the operator's ceiling once before it invokes any hook, so a
    denied channel is answered ``None`` without this hook running at all: no nonce
    armed, no DM channel opened, nothing posted. Pinned THROUGH the seam rather
    than by calling the hook directly, because calling it directly would test a
    state the seam does not allow.
    """
    d, cli, _ = _dispatcher({"u1"})
    seam.register_channel_delivery("discord", d.deliver_spawn_approval)
    key = d._session_key("u1")
    reached: list[str] = []

    async def _record_dm(user_id: str) -> str:
        reached.append(user_id)
        return f"dm-{user_id}"

    monkeypatch.setattr(cli, "create_dm_channel", _record_dm)

    async def _denied(_channel: str) -> bool:
        return False

    monkeypatch.setattr(seam, "channel_inbound_permitted", _denied)

    decision = await seam.deliver_spawn_approval("spawn:a1", "spawn_run(task)", key)

    assert decision is None, "a denied channel must fall through, never answer False"
    assert cli.sent == []
    assert reached == [], "the hook must not run at all under a deny"
    assert _armed_nonces() == {}


@pytest.mark.asyncio
async def test_a_ceiling_closed_during_the_dm_open_falls_through_unposted(monkeypatch) -> None:
    """This dispatcher re-reads the ceiling across the one gap the seam cannot see.

    The seam's read happens before the hook is entered. Opening the peer's DM
    channel is a full round trip INSIDE the hook, so the ceiling can close across
    it while the seam's answer is already spent. A preview posted then reaches a
    channel the ceiling has switched off, and the Approve press that would answer
    it is dropped there, so the wait would deny-by-default. From the re-read to the
    send nothing suspends, which is what makes it the latest point a read can speak
    for.
    """
    d, cli, _ = _dispatcher({"u1"})
    key = d._session_key("u1")
    reads: list[str] = []

    async def _denied(channel: str) -> bool:
        reads.append(channel)
        return False

    async def _open_dm(user_id: str) -> str:
        return f"dm-{user_id}"

    monkeypatch.setattr(cli, "create_dm_channel", _open_dm)
    monkeypatch.setattr(dispatch_mod, "channel_inbound_permitted", _denied)

    decision = await d.deliver_spawn_approval("spawn:a1", "spawn_run(task)", key)

    assert decision is None, "a ceiling that closed mid-delivery must fall through"
    assert cli.sent == [], "no preview may reach a channel denied during the open"
    assert _armed_nonces() == {}
    assert reads == ["discord"], "the re-read must happen, and name this channel"


# ── An early press ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_press_landing_before_the_waiter_exists_is_still_honoured() -> None:
    """A press during the post resolves the wait instead of reporting "expired".

    The nonce is armed when the prompt is BUILT, but the future only registers
    when the caller starts awaiting, and the post in between suspends. A press
    that lands in that gap finds no future, so dropping it would deny-by-default
    at the timeout and tell a user who pressed Approve that the approval had
    expired. The decision is held under the same key and consumed by the waiter.
    """
    d, cli, _ = _dispatcher({"u1"})
    key = d._session_key("u1")
    k = DiscordApprovalDecider.key(key, "spawn:a1")

    async def _press_during_the_post(*_a: Any, **_kw: Any) -> str:
        # Stands in for Discord delivering the interaction while the send is
        # still in flight, which is exactly when no future exists yet.
        nonce = _armed_nonces()[k]
        assert DiscordApprovalDecider.resolve_global(k, True, nonce=nonce) is True
        return "m1"

    cli.send_message = _press_during_the_post  # type: ignore[assignment]

    assert await d.deliver_spawn_approval("spawn:a1", "spawn_run(task)", key) is True
    assert _armed_nonces() == {}


@pytest.mark.asyncio
async def test_an_early_deny_is_honoured_as_a_deny() -> None:
    """The held decision carries the user's actual answer, not a default."""
    d, cli, _ = _dispatcher({"u1"})
    key = d._session_key("u1")
    k = DiscordApprovalDecider.key(key, "spawn:a1")

    async def _deny_during_the_post(*_a: Any, **_kw: Any) -> str:
        DiscordApprovalDecider.resolve_global(k, False, nonce=_armed_nonces()[k])
        return "m1"

    cli.send_message = _deny_during_the_post  # type: ignore[assignment]

    assert await d.deliver_spawn_approval("spawn:a1", "spawn_run(task)", key) is False


# ── Routing ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_key_this_channel_cannot_address_falls_through() -> None:
    """Unowned, foreign and unified keys name no Discord conversation.

    A ``unified`` DM bucket collapses several peers into one session, so it names
    no single conversation to post a task preview into.
    """
    d, cli, _ = _dispatcher({"u1"})
    for key in (
        "unified:kirocrew",
        "telegram:kirocrew:direct:12345",
        "dashboard:main",
        "discord:kirocrew:direct",  # no scope segment
        "",
    ):
        assert await d.deliver_spawn_approval("spawn:a1", "spawn_run(t)", key) is None, key
    assert cli.sent == []


@pytest.mark.asyncio
async def test_a_thread_route_prompts_in_the_thread() -> None:
    """A Discord thread's snowflake IS its channel, so the prompt goes there."""
    d, cli, _ = _dispatcher({"u1"}, allowed_threads={"t1"})
    key = d._session_key("u1", "t1")

    task = asyncio.ensure_future(d.deliver_spawn_approval("spawn:a1", "spawn_run(task)", key))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert cli.send_channels == ["t1"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_direct_route_opens_the_peers_dm_channel() -> None:
    """A direct key names the PEER, so its DM channel has to be opened first."""
    d, cli, _ = _dispatcher({"u1"})
    key = d._session_key("u1")

    task = asyncio.ensure_future(d.deliver_spawn_approval("spawn:a1", "spawn_run(task)", key))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert cli.send_channels == ["dm-u1"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_the_task_preview_cannot_break_out_of_its_code_block() -> None:
    """The preview is agent-authored and lands in a markdown message.

    Backticks would close the fence and let the rest of the text style the
    message, and raw newlines would spread one preview over several lines, so
    neither survives into the body.
    """
    d, cli, _ = _dispatcher({"u1"})
    key = d._session_key("u1")
    nasty = "spawn_run(```\n# LOOK AT ME\n)"

    task = asyncio.ensure_future(d.deliver_spawn_approval("spawn:a1", nasty, key))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    text, _components = cli.sent[-1]
    # Exactly the opening and the closing fence, so the preview opened none of
    # its own. Counted on the WHOLE message rather than on a slice of it: a
    # fence-delimited slice is fooled by the injected fence it is meant to catch.
    assert text.count("```") == 2, text
    # Header, opening fence, the one-line preview, closing fence.
    lines = text.splitlines()
    assert len(lines) == 4, lines
    assert "`" not in lines[2], lines[2]
    assert "LOOK AT ME" in lines[2], "the preview is still shown, just neutralized"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_credential_split_by_invisible_characters_is_not_displayed() -> None:
    """The preview is cleared in the form Discord renders, not in the form it arrives.

    A zero-width character between two halves of a key breaks every literal scan
    while rendering as nothing, so the upstream pass leaves it intact and the
    reader sees a whole secret. The assertion is made on the canonicalized text
    for the same reason: the literal message never contains the secret as one
    string, so checking the literal would pass with no redaction at all.
    """
    d, cli, _ = _dispatcher({"u1"})
    key = d._session_key("u1")
    secret = "AKIAIOSFODNN7EXAMPLE"
    split = secret[:6] + "\u200b" + secret[6:]

    task = asyncio.ensure_future(
        d.deliver_spawn_approval("spawn:a1", f"spawn_run(deploy using {split})", key)
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    text, _components = cli.sent[-1]
    assert secret not in canonicalize_display(text), "the rendered preview carries the secret"
    assert "REDACTED" in text, text
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_registering_the_hook_routes_a_discord_key_to_this_channel() -> None:
    """End to end through the seam: the gateway's registration is what opts in."""
    d, cli, _ = _dispatcher({"u1"})
    seam.register_channel_delivery("discord", d.deliver_spawn_approval)

    task = asyncio.ensure_future(
        seam.deliver_spawn_approval("spawn:a1", "spawn_run(task)", d._session_key("u1"))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert cli.send_channels == ["dm-u1"], "the seam did not reach this dispatcher"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_registering_discord_does_not_widen_another_channel() -> None:
    """Opting one channel in leaves every other channel on the unchanged path.

    The registry is keyed per channel, so a key belonging to a channel that has
    registered nothing still answers ``None`` and still reaches the host gate's
    own surfaces.
    """
    d, cli, _ = _dispatcher({"u1"})
    seam.register_channel_delivery("discord", d.deliver_spawn_approval)

    for foreign in ("webex:kirocrew:direct:u1", "telegram:kirocrew:direct:12345"):
        assert await seam.deliver_spawn_approval("spawn:a1", "spawn_run(t)", foreign) is None
    assert cli.sent == []


def test_a_foreign_surface_is_refused_by_the_reverse_lookup_itself() -> None:
    """The surface check stands on its own, not on the roster behind it.

    Another channel's key can parse into a plausible-looking scope, and the
    rosters would usually refuse the resulting destination anyway. Asserted on the
    lookup directly so that second line of defence cannot hide a regression here.
    """
    d, _cli, _ = _dispatcher({"u1"})
    assert d._spawn_chat_target("telegram:kirocrew:direct:u1") is None
    assert d._spawn_chat_target("webex:kirocrew:group:t1") is None
    assert d._spawn_chat_target("unified:kirocrew") is None
    # Non-vacuity: this channel's own keys DO resolve, and to the right places.
    assert d._spawn_chat_target(d._session_key("u1")) == ("", "", "u1", d._session_key("u1"))
    thread_key = d._session_key("u1", "t1")
    assert d._spawn_chat_target(thread_key) == ("t1", "t1", "", thread_key)


def test_the_reverse_lookup_refuses_every_shape_it_cannot_place() -> None:
    """The rest of the case list, each answered rather than guessed at.

    A chat_type this channel does not mint, and a scope deeper than the one
    segment it does mint, are both unaddressable: there is no single conversation
    to put a task preview in. Neither is reachable from this channel's own
    builder today, which is the reason to pin them — a future route that starts
    minting either shape should have to come back here rather than silently
    resolve to the first segment.
    """
    d, _cli, _ = _dispatcher({"u1"})
    assert d._spawn_chat_target("discord:kirocrew:channel:c1") is None, "unknown chat_type"
    assert d._spawn_chat_target("discord:kirocrew:group:g1:t1") is None, "scope deeper than one"
    assert d._spawn_chat_target("discord:kirocrew:direct:u1:extra") is None


# ── Resolution through the existing press path ─────────────────────────────


@pytest.mark.asyncio
async def test_approve_resolves_true_through_the_existing_press_path() -> None:
    """The press arrives on ``on_interaction``'s ``a:`` branch, as a tool press does."""
    d, cli, _ = _dispatcher({"u1"})
    key = d._session_key("u1")

    task = asyncio.ensure_future(d.deliver_spawn_approval("spawn:a1", "spawn_run(task)", key))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    ids = _pressed_ids(cli.sent[-1][1])
    assert ids[0].startswith("a:spawn:a1:") and ids[0].endswith(":1")
    assert ids[1].endswith(":0")

    await d.on_interaction(_itx(ids[0]))
    assert await task is True


@pytest.mark.asyncio
async def test_deny_resolves_false_through_the_existing_press_path() -> None:
    """Non-vacuity for the test above: the same path carries a real refusal."""
    d, cli, _ = _dispatcher({"u1"})
    key = d._session_key("u1")

    task = asyncio.ensure_future(d.deliver_spawn_approval("spawn:a1", "spawn_run(task)", key))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    ids = _pressed_ids(cli.sent[-1][1])
    await d.on_interaction(_itx(ids[1]))
    assert await task is False


@pytest.mark.asyncio
async def test_the_arming_turns_end_does_not_close_the_spawn_window() -> None:
    """The prompt outlives the turn that asked for the spawn, so the sweep spares it.

    The gate awaits the approval in its own task and the agent is told to end its
    turn, so the turn's end runs while the user is still looking at the prompt.
    Closing the window there drops the nonce, and the press is then answered with
    an expiry the window had not reached while the wait spends its whole timeout
    and hands the gate a refusal nobody pressed.
    """
    d, cli, _ = _dispatcher({"u1"})
    key = d._session_key("u1")
    k = DiscordApprovalDecider.key(key, "spawn:a1")

    task = asyncio.ensure_future(d.deliver_spawn_approval("spawn:a1", "spawn_run(t)", key))
    for _ in range(3):
        await asyncio.sleep(0)

    DiscordApprovalDecider.discard_session(key)

    assert k in _armed_nonces(), "the sweep must leave a detached window armed"
    ids = _pressed_ids(cli.sent[-1][1])
    await d.on_interaction(_itx(ids[0]))
    assert await task is True


@pytest.mark.asyncio
async def test_the_turns_end_still_closes_an_ordinary_tool_window() -> None:
    """The exemption is per key, so a turn-owned prompt is swept exactly as before.

    Only a caller that declares its wait detached is spared. A tool approval is
    awaited inside its own turn, so its unawaited reservation must still go.
    """
    key = "discord:dm:u1"
    tool_key = DiscordApprovalDecider.key(key, "a1")
    DiscordApprovalDecider.register_nonce(tool_key)
    assert tool_key in _armed_nonces()

    DiscordApprovalDecider.discard_session(key)

    assert tool_key not in _armed_nonces(), "a turn-owned window still closes"
    assert DiscordApprovalDecider.resolve_global(tool_key, True, nonce="anything") is False


@pytest.mark.asyncio
async def test_a_consumed_early_press_stops_exempting_its_key() -> None:
    """The exemption ends with the window it was granted for.

    A press landing during the post is consumed on the adopting path, which
    retires the key. Its detached mark goes with it, because a mark outliving its
    key exempts that key from every later sweep for the life of the process: the
    next prompt on it is an ordinary tool approval, awaited inside its own turn,
    and that one must still be swept when the turn ends.
    """
    d, cli, _ = _dispatcher({"u1"})
    key = d._session_key("u1")
    k = DiscordApprovalDecider.key(key, "spawn:a1")

    async def _press_during_the_post(*_a: Any, **_kw: Any) -> str:
        nonce = _armed_nonces()[k]
        assert DiscordApprovalDecider.resolve_global(k, True, nonce=nonce) is True
        return "m1"

    cli.send_message = _press_during_the_post  # type: ignore[assignment]
    assert await d.deliver_spawn_approval("spawn:a1", "spawn_run(task)", key) is True

    # The same key, reused by a turn-owned prompt once the spawn window is gone.
    DiscordApprovalDecider.register_nonce(k)
    assert k in _armed_nonces()

    DiscordApprovalDecider.discard_session(key)

    assert k not in _armed_nonces(), "the consumed window's exemption outlived it"


@pytest.mark.asyncio
async def test_a_spawn_id_does_not_collide_with_a_tool_id() -> None:
    """The registry is keyed by ``session_key:request_id``.

    A spawn's ``spawn:<id>`` request id and an opaque tool id therefore occupy
    different slots, so one prompt's press cannot resolve the other's wait.
    """
    d, cli, _ = _dispatcher({"u1"})
    key = d._session_key("u1")

    spawn_task = asyncio.ensure_future(d.deliver_spawn_approval("spawn:a1", "spawn_run(t)", key))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    tool_key = DiscordApprovalDecider.key(key, "a1")
    tool_fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
    DiscordApprovalDecider._REGISTRY[tool_key] = tool_fut
    tool_nonce = DiscordApprovalDecider.register_nonce(tool_key)

    # Resolving the TOOL slot leaves the spawn's wait pending.
    assert DiscordApprovalDecider.resolve_global(tool_key, True, nonce=tool_nonce)
    assert not spawn_task.done()

    ids = _pressed_ids(cli.sent[-1][1])
    await d.on_interaction(_itx(ids[0]))
    assert await spawn_task is True


@pytest.mark.asyncio
async def test_a_press_after_a_generation_rotation_resolves_nothing() -> None:
    """The prompt is armed under the key it was raised on.

    A press recomputes the key from the LIVE conversation, so a rotation between
    the spawn and the press means the press matches no armed prompt and the
    prompt deny-by-defaults at its own timeout instead of resolving under a key
    that has moved on.
    """
    d, cli, _ = _dispatcher({"u1"})
    stale_key = f"{d._session_key('u1')}:gen7"

    task = asyncio.ensure_future(d.deliver_spawn_approval("spawn:a1", "spawn_run(t)", stale_key))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    ids = _pressed_ids(cli.sent[-1][1])
    await d.on_interaction(_itx(ids[0]))

    assert not task.done(), "a press under a rotated key must not answer the armed prompt"
    assert any("expired" in text for _mid, text, _c in cli.edits)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
