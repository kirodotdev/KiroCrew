"""The channels governance ceiling gates channel-side spawn-approval delivery.

A spawn parented on a channel conversation is offered its Approve/Deny prompt
there by that channel's registered delivery hook. The operator's `channels`
ceiling has to be consulted before any hook is invoked, because the press that
would answer such a prompt arrives INBOUND on the same channel, and a denied
channel drops that press: a channel's callback path refuses everything except an
explicit reject. A prompt posted under a deny is therefore unanswerable, its
deny-by-default wait elapses, and the seam hands the host gate a ``False`` nobody
pressed -- which the gate reads as the user's decision and refuses the spawn on,
instead of re-offering it on the still-permitted Slack DM or dashboard surface.

Four properties are pinned:

* a denied channel invokes no hook, posts nothing, and answers ``None``
  (fall through) rather than ``False`` (a denial nobody made);
* the check sits at the seam, so a channel whose hook never spells it is gated
  too, and the Telegram hook arms no approval nonce under a deny;
* hook resolution runs first, so a key in a non-channel namespace never asks the
  profile store about a channel type that does not exist;
* a permitted channel is unchanged -- it still prompts and still resolves its
  press.

All Telegram client I/O is faked; nothing touches the network, and the ceiling
predicate is substituted rather than driven through a real profile store.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

# Reuse the doubles the Telegram suite already uses (fake client, fake sessions,
# dispatcher factory) so this suite exercises a REAL registered hook.
from test_telegram import _dispatcher  # noqa: E402

from kiro_crew.messaging import spawn_approval_delivery as seam
from kiro_crew.telegram.renderer import TelegramApprovalDecider

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_RID = "spawn:abc"
_TASK = "spawn_run(build the widget)"


@pytest.fixture(autouse=True)
def _clean_registries():
    """Start and end with an empty seam registry and no armed prompts."""
    seam.clear_channel_delivery_hooks()
    TelegramApprovalDecider._REGISTRY.clear()
    TelegramApprovalDecider._NONCES.clear()
    yield
    seam.clear_channel_delivery_hooks()
    TelegramApprovalDecider._REGISTRY.clear()
    TelegramApprovalDecider._NONCES.clear()


@pytest.fixture(autouse=True)
def _short_prompt_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the deny-by-default wait so a REGRESSION fails fast.

    A refusal returns before any prompt is awaited, so this does not touch the
    path under test. It bounds the OTHER outcome: code that posts the prompt
    under a deny would park every assertion below on the real approval timeout
    (minutes) and surface as a suite-wide hang instead of a failed assertion.
    """
    import kiro_crew.telegram.renderer as renderer_mod

    monkeypatch.setattr(renderer_mod, "_APPROVAL_TIMEOUT_S", 0.2)


@pytest.fixture
def _ceiling(monkeypatch: pytest.MonkeyPatch):
    """Substitute the ceiling predicate, defaulting to PERMIT.

    The real predicate hops to a thread pool and walks the profile store, so
    leaving it in place would couple these tests to an executor round-trip
    instead of to the delivery behaviour they are about. The premise is stated
    rather than implied: each test either takes the permit default or flips it.
    """

    calls: list[str] = []
    state = {"permitted": True}

    async def _permitted(channel_type: str) -> bool:
        calls.append(channel_type)
        return bool(state["permitted"])

    monkeypatch.setattr(seam, "channel_inbound_permitted", _permitted)
    return SimpleNamespace(calls=calls, state=state)


async def _press(dispatcher, session_key: str, flag: str) -> None:
    """Press an inline button in the DM, exactly as the real callback path does."""
    key = TelegramApprovalDecider.key(session_key, _RID)
    for _ in range(50):
        if key in TelegramApprovalDecider._REGISTRY:
            break
        await asyncio.sleep(0.01)
    nonce = TelegramApprovalDecider._NONCES[key]
    await dispatcher.on_callback(
        SimpleNamespace(
            callback_query_id="q1",
            user_id=7,
            chat_id=7,
            message_id=100,
            data=f"a:{_RID}:{nonce}:{flag}",
            label="",
            chat_type="private",
        )
    )


class TestTheGovernanceCeilingGatesChannelDelivery:
    """A denied channel reaches no hook, and the spawn stays answerable elsewhere."""

    def test_a_denied_channel_invokes_no_hook_and_falls_through(self, _ceiling) -> None:
        invoked: list[str] = []

        async def _hook(rid: str, _desc: str, _parent: str) -> bool:
            invoked.append(rid)
            return True

        seam.register_channel_delivery("telegram", _hook)
        _ceiling.state["permitted"] = False

        result = asyncio.run(seam.deliver_spawn_approval(_RID, _TASK, "telegram:k:direct:7"))

        # None, not False: nothing was surfaced, so there is no decision to report
        # and the gate re-offers the prompt on Slack/dashboard.
        assert result is None
        assert result is not False
        assert invoked == []
        assert _ceiling.calls == ["telegram"]

    def test_the_ceiling_is_asked_about_the_channel_that_owns_the_session(self, _ceiling) -> None:
        # The channel the prompt would be posted to is the one whose permission
        # decides it, so the question carries that channel's own name.
        async def _hook(_rid: str, _desc: str, _parent: str) -> bool:
            return True

        seam.register_channel_delivery("discord", _hook)
        _ceiling.state["permitted"] = False

        assert asyncio.run(seam.deliver_spawn_approval(_RID, _TASK, "discord:k:direct:9")) is None
        assert _ceiling.calls == ["discord"]

    def test_a_key_with_no_hook_never_asks_about_its_channel_type(self, _ceiling) -> None:
        # Hook resolution runs first, so a non-channel namespace and a channel
        # nobody registered both fall through without a governance question about
        # a channel type the policy has no opinion on.
        assert asyncio.run(seam.deliver_spawn_approval(_RID, _TASK, "dashboard:chat-1-2")) is None
        assert asyncio.run(seam.deliver_spawn_approval(_RID, _TASK, "unified:kirocrew")) is None
        assert asyncio.run(seam.deliver_spawn_approval(_RID, _TASK, "telegram:k:direct:7")) is None
        assert _ceiling.calls == []

    def test_a_denied_channel_leaves_the_telegram_hook_arming_nothing(self, _ceiling) -> None:
        # Through the REAL dispatcher hook: a refused delivery arms no approval
        # nonce and opens no wait, because the hook is never entered.
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        seam.register_channel_delivery("telegram", d.deliver_spawn_approval)
        _ceiling.state["permitted"] = False

        result = asyncio.run(seam.deliver_spawn_approval(_RID, _TASK, session_key))

        assert result is None
        assert cli.sent == []
        key = TelegramApprovalDecider.key(session_key, _RID)
        assert key not in TelegramApprovalDecider._NONCES
        assert key not in TelegramApprovalDecider._REGISTRY

    def test_a_permitted_channel_still_prompts_and_resolves_the_press(self, _ceiling) -> None:
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        seam.register_channel_delivery("telegram", d.deliver_spawn_approval)

        async def _go() -> bool | None:
            task = asyncio.ensure_future(seam.deliver_spawn_approval(_RID, _TASK, session_key))
            await asyncio.sleep(0)
            await _press(d, session_key, "1")
            return await task

        assert asyncio.run(_go()) is True
        assert len(cli.sent) == 1
        assert _ceiling.calls == ["telegram"]
