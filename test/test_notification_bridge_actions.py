"""The bridge action binding (RFC notification-bridge-actions, phase B4a).

Pins that a bridged answer resolves the live decision future once, only for the
verified owner, only with an offered choice and only before its deadline; that a
forged, stale or post-restart token resolves nothing; that only the approval note
and the Slack sink and press handler reach it; and that it follows the
``bridge_approvals`` preview, failing closed.
"""

from __future__ import annotations

import ast
import asyncio
import inspect

import pytest

from kiro_crew.notifications import bridge_actions as actions_mod
from kiro_crew.notifications.bridge_actions import ActionOutcome, BridgeActionRegistry
from kiro_crew.slack.approval_tokens import build_approval_token, split_approval_token

#: Each answer maps to the value the decision future takes, here a bool.
_CHOICES = {"approve": True, "deny": False}

#: A short deadline, advanced on the fake clock; nothing sleeps.
_TTL_S = 30.0
_DEADLINE = {"ttl_s": _TTL_S}  # flake-ok: a deadline on the fake clock, nothing waits


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def clock(monkeypatch) -> _Clock:
    """Drive the one bridged-expiry seam, so no test sleeps."""
    fake = _Clock()
    monkeypatch.setattr(actions_mod, "bridge_clock", fake)
    return fake


@pytest.fixture()
def registry(clock) -> BridgeActionRegistry:
    return BridgeActionRegistry()


@pytest.mark.asyncio
async def test_the_owner_answer_resolves_the_decision_future(registry):
    decision = asyncio.get_running_loop().create_future()
    token = registry.bind("dashboard:s1", 7, decision, choices=_CHOICES, **_DEADLINE)
    assert registry.resolve(token, "approve", from_owner=True) is ActionOutcome.ADMITTED
    assert decision.result() is True


@pytest.mark.asyncio
async def test_the_token_is_the_shipped_slack_token_with_a_fresh_nonce(registry):
    first = registry.bind(
        "dashboard:s1",
        7,
        asyncio.get_running_loop().create_future(),
        choices=_CHOICES,
        **_DEADLINE,
    )
    key, nonce = split_approval_token(first)
    assert nonce and first == build_approval_token("dashboard:s1", 7, nonce)
    second = registry.bind(
        "dashboard:s1",
        8,
        asyncio.get_running_loop().create_future(),
        choices=_CHOICES,
        **_DEADLINE,
    )
    assert split_approval_token(second)[1] != nonce


@pytest.mark.asyncio
async def test_it_is_single_use(registry):
    decision = asyncio.get_running_loop().create_future()
    token = registry.bind("dashboard:s1", 7, decision, choices=_CHOICES, **_DEADLINE)
    registry.resolve(token, "approve", from_owner=True)
    assert registry.resolve(token, "deny", from_owner=True) is ActionOutcome.ALREADY_ANSWERED
    assert decision.result() is True


@pytest.mark.asyncio
async def test_an_answer_from_another_surface_wins_and_the_bridge_sees_it(registry):
    decision = asyncio.get_running_loop().create_future()
    token = registry.bind("dashboard:s1", 7, decision, choices=_CHOICES, **_DEADLINE)
    decision.set_result(False)  # the dashboard answered first
    assert registry.resolve(token, "approve", from_owner=True) is ActionOutcome.ALREADY_ANSWERED
    assert decision.result() is False


@pytest.mark.asyncio
async def test_a_non_owner_is_refused_and_does_not_burn_the_owner_answer(registry):
    decision = asyncio.get_running_loop().create_future()
    token = registry.bind("dashboard:s1", 7, decision, choices=_CHOICES, **_DEADLINE)
    assert registry.resolve(token, "approve", from_owner=False) is ActionOutcome.NOT_OWNER
    assert not decision.done()
    assert registry.resolve(token, "deny", from_owner=True) is ActionOutcome.ADMITTED


@pytest.mark.asyncio
async def test_a_deny_sets_the_future_value_the_prompt_maps_it_to(registry):
    """The future takes its own value, never the raw choice string (bool("deny") is True)."""
    decision = asyncio.get_running_loop().create_future()
    token = registry.bind("dashboard:s1", 7, decision, choices=_CHOICES, **_DEADLINE)
    assert registry.resolve(token, "deny", from_owner=True) is ActionOutcome.ADMITTED
    assert decision.result() is False


@pytest.mark.asyncio
async def test_a_decision_whose_waiter_timed_out_reads_as_expired(registry):
    decision = asyncio.get_running_loop().create_future()
    token = registry.bind("dashboard:s1", 7, decision, choices=_CHOICES, **_DEADLINE)
    decision.cancel()  # what asyncio.wait_for does to the future on timeout
    assert registry.resolve(token, "approve", from_owner=True) is ActionOutcome.EXPIRED


@pytest.mark.asyncio
async def test_a_choice_the_prompt_does_not_offer_is_refused(registry):
    """A prompt bound without an override choice can never be answered with one."""
    decision = asyncio.get_running_loop().create_future()
    token = registry.bind("dashboard:s1", 7, decision, choices=_CHOICES, **_DEADLINE)
    assert registry.resolve(token, "override", from_owner=True) is ActionOutcome.INVALID_CHOICE
    assert not decision.done()


@pytest.mark.asyncio
async def test_it_expires_at_its_deadline(registry, clock):
    decision = asyncio.get_running_loop().create_future()
    token = registry.bind(
        "dashboard:s1", 7, decision, choices=_CHOICES, **_DEADLINE
    )  # flake-ok: a deadline on the fake clock, nothing waits
    clock.now += _TTL_S
    assert registry.resolve(token, "approve", from_owner=True) is ActionOutcome.EXPIRED
    assert not decision.done()


@pytest.mark.asyncio
async def test_the_deadline_is_the_surface_ttl_on_the_one_bridge_clock(registry, clock):
    """Each surface passes its own deadline; only ``bridge_clock`` measures it."""
    surface = {"ttl_s": 5.0}  # flake-ok: a deadline on the fake clock, nothing waits
    token = registry.bind(
        "dashboard:s1", 7, asyncio.get_running_loop().create_future(), choices=_CHOICES, **surface
    )
    clock.now += 4.9
    assert registry.token_for_message("slack", "m1") == ""  # nothing linked
    clock.now += 0.1
    assert registry.resolve(token, "approve", from_owner=True) is ActionOutcome.EXPIRED


def test_a_bind_without_a_surface_deadline_is_refused(registry):
    """No module constant stands in for the per-surface deadline."""
    assert inspect.signature(BridgeActionRegistry.bind).parameters["ttl_s"].default is (
        inspect.Parameter.empty
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["trust", "always", "3"])
async def test_a_trust_reply_to_a_bridged_prompt_is_refused(registry, choice):
    """A bridged prompt offers approve and deny only; trust never auto-approves."""
    decision = asyncio.get_running_loop().create_future()
    token = registry.bind("dashboard:s1", 7, decision, choices=_CHOICES, **_DEADLINE)
    assert registry.resolve(token, choice, from_owner=True) is ActionOutcome.INVALID_CHOICE
    assert not decision.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("choices", [{"approve": "trust"}, {"deny": "trust"}])
async def test_an_answer_can_never_map_to_the_trust_verdict(registry, choices):
    with pytest.raises(ValueError):
        registry.bind(
            "dashboard:s1",
            7,
            asyncio.get_running_loop().create_future(),
            choices=choices,
            **_DEADLINE,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl", [float("nan"), float("inf"), 0.0, -1.0])
async def test_a_deadline_that_never_passes_is_refused(registry, ttl):
    with pytest.raises(ValueError):
        registry.bind(
            "dashboard:s1",
            7,
            asyncio.get_running_loop().create_future(),
            choices=_CHOICES,
            **{"ttl_s": ttl},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_nonce", ["é", "\udcff", "nonce\x00"])
async def test_a_malformed_forged_nonce_answers_unknown(registry, bad_nonce):
    """A forged nonce the platform echoes back is refused, never raised."""
    decision = asyncio.get_running_loop().create_future()
    token = registry.bind("dashboard:s1", 7, decision, choices=_CHOICES, **_DEADLINE)
    key = split_approval_token(token)[0]
    for from_owner in (False, True):
        forged = f"{key}|{bad_nonce}"
        assert registry.resolve(forged, "approve", from_owner=from_owner) is ActionOutcome.UNKNOWN
    assert not decision.done()


@pytest.mark.asyncio
async def test_a_bridged_prompt_cannot_be_bound_with_a_trust_choice(registry):
    with pytest.raises(ValueError):
        registry.bind(
            "dashboard:s1",
            7,
            asyncio.get_running_loop().create_future(),
            choices={**_CHOICES, "trust": True},
            **_DEADLINE,
        )
    assert actions_mod.BRIDGED_CHOICES == {"approve", "deny"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forge",
    [
        lambda token: split_approval_token(token)[0],  # no nonce at all
        lambda token: split_approval_token(token)[0] + "|not-the-nonce",
        lambda token: build_approval_token("dashboard:other", 7, split_approval_token(token)[1]),
    ],
)
async def test_a_forged_token_resolves_nothing(registry, forge):
    decision = asyncio.get_running_loop().create_future()
    token = registry.bind("dashboard:s1", 7, decision, choices=_CHOICES, **_DEADLINE)
    assert registry.resolve(forge(token), "approve", from_owner=True) is ActionOutcome.UNKNOWN
    assert not decision.done()


@pytest.mark.asyncio
async def test_a_rebind_retires_the_earlier_token(registry):
    loop = asyncio.get_running_loop()
    old = registry.bind("dashboard:s1", 7, loop.create_future(), choices=_CHOICES, **_DEADLINE)
    decision = loop.create_future()
    new = registry.bind("dashboard:s1", 7, decision, choices=_CHOICES, **_DEADLINE)
    assert registry.resolve(old, "approve", from_owner=True) is ActionOutcome.UNKNOWN
    assert registry.resolve(new, "approve", from_owner=True) is ActionOutcome.ADMITTED


@pytest.mark.asyncio
async def test_a_restart_leaves_no_decision_to_resolve(clock):
    token = BridgeActionRegistry().bind(
        "dashboard:s1",
        7,
        asyncio.get_running_loop().create_future(),
        choices=_CHOICES,
        **_DEADLINE,
    )
    fresh = BridgeActionRegistry()
    assert fresh.resolve(token, "approve", from_owner=True) is ActionOutcome.UNKNOWN


@pytest.mark.asyncio
async def test_a_posted_message_names_its_decision_while_the_registry_remembers_it(
    registry, clock, monkeypatch
):
    monkeypatch.setattr(actions_mod, "EXPIRED_MEMORY", 1)
    loop = asyncio.get_running_loop()
    decision = loop.create_future()
    token = registry.bind(
        "dashboard:s1", 7, decision, choices=_CHOICES, **_DEADLINE
    )  # flake-ok: a deadline on the fake clock, nothing waits
    registry.link_message("slack", "D1:1700000000.0001", token)
    assert registry.token_for_message("slack", "D1:1700000000.0001") == token
    assert registry.token_for_message("discord", "D1:1700000000.0001") == ""
    clock.now += _TTL_S + 1
    # Pruned past its deadline, the link still names the decision, so a late reply
    # is resolved and audited as expired rather than read as an ordinary message.
    registry.bind("dashboard:s1", 8, loop.create_future(), choices=_CHOICES, **_DEADLINE)
    assert registry.token_for_message("slack", "D1:1700000000.0001") == token
    assert registry.resolve(token, "approve", from_owner=True) is ActionOutcome.EXPIRED
    # Once the registry forgets the expired binding, the link goes with it.
    clock.now += _TTL_S + 1
    registry.bind("dashboard:s1", 9, loop.create_future(), choices=_CHOICES, **_DEADLINE)
    assert registry.token_for_message("slack", "D1:1700000000.0001") == ""


@pytest.mark.asyncio
async def test_bind_refuses_no_choices_and_a_resolved_decision(registry):
    loop = asyncio.get_running_loop()
    with pytest.raises(ValueError):
        registry.bind("dashboard:s1", 7, loop.create_future(), choices={}, **_DEADLINE)
    done = loop.create_future()
    done.set_result(False)
    with pytest.raises(ValueError):
        registry.bind("dashboard:s1", 7, done, choices=_CHOICES, **_DEADLINE)


#: The modules that may reach the B4 registry: its own two, the gateway state that
#: holds it, the pending-approval note that binds it, the Slack and reply-transport
#: sinks that post and link its affordances, and the Slack press handler and the
#: Telegram reply reader that read it (behind ``actions_enabled``).
_B4_READERS = frozenset(
    {
        "notifications/bridge_actions.py",
        "notifications/bridge_action_audit.py",
        "notifications/bridge_approval_note.py",
        "notifications/bridge_slack.py",
        "notifications/bridge_transport.py",
        "dashboard/state_parts/dashboard_state.py",
        "slack/interactions.py",
        "slack/interactions_approvals.py",
        "telegram/dispatch/bridge_reply.py",
    }
)
_B4_NAMES = ("bridge_actions", "bridge_action_audit")


def test_only_the_approval_note_slack_and_telegram_reach_the_registry():
    """Nothing else imports B4, and nothing outside its readers binds a decision."""
    from pathlib import Path

    import kiro_crew

    root = Path(inspect.getfile(kiro_crew)).parent
    importers = []
    binders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel in _B4_READERS:
            continue
        source = path.read_text(encoding="utf-8")
        if "bridge_actions.bind(" in source or ".link_message(" in source:
            binders.append(rel)
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            module = getattr(node, "module", None) or ""
            names = [alias.name for alias in node.names]
            if any(m in module or any(m in n for n in names) for m in _B4_NAMES):
                importers.append(rel)
    assert importers == []
    assert binders == []
    assert actions_mod.BridgeActionRegistry is BridgeActionRegistry


def _cfg(previews):
    from types import SimpleNamespace

    return SimpleNamespace(dashboard=SimpleNamespace(feature_previews=previews))


@pytest.mark.parametrize(
    ("previews", "expected"),
    [
        ({}, False),
        ({"bridge_approvals": False}, False),
        ({"bridge_approvals": "yes"}, False),
        ({"bridge_approvals": True}, True),
    ],
)
def test_actions_follow_the_bridge_approvals_preview(previews, expected):
    assert actions_mod.actions_enabled(_cfg(previews)) is expected


def test_actions_fail_closed_when_the_preview_cannot_be_read(monkeypatch):
    from kiro_crew.notifications import bridge_approval_note

    def _boom(cfg):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(bridge_approval_note, "enabled", _boom)
    assert actions_mod.actions_enabled(_cfg({"bridge_approvals": True})) is False
