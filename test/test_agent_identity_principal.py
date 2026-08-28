"""Trusted AgentCore session principal — core-derived, never from tool input."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.constants import SUBAGENT_COMPLETION_PREFIX
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import reset_context, set_context
from kiro_crew.platform.defaults import DefaultAgentIdentityProvider
from kiro_crew.platform.interfaces import SessionPrincipal


def test_dashboard_owner_is_partitioned() -> None:
    from kiro_crew.platform.agent_identity import derive_session_principal

    p = derive_session_principal(surface="dashboard", raw_id="alice", session_key="dashboard:1")
    assert p.subject == "dashboard+alice"
    assert p.surface == "dashboard"
    assert p.session_key == "dashboard:1"
    assert p.user_jwt is None


def test_slack_user_is_partitioned() -> None:
    from kiro_crew.platform.agent_identity import derive_session_principal

    p = derive_session_principal(
        surface="slack", raw_id="U0123", session_key="slack:1783733803.877979"
    )
    assert p.subject == "slack+U0123"
    assert p.user_jwt is None


def test_discord_user_is_partitioned() -> None:
    from kiro_crew.platform.agent_identity import derive_session_principal

    p = derive_session_principal(surface="discord", raw_id="99", session_key="discord:kirocrew:g:t")
    assert p.subject == "discord+99"


def test_cli_os_user_is_partitioned() -> None:
    from kiro_crew.platform.agent_identity import derive_session_principal

    p = derive_session_principal(surface="cli", raw_id="kyle", session_key="cli")
    assert p.subject == "cli+kyle"


def test_cron_job_owner_is_partitioned() -> None:
    from kiro_crew.platform.agent_identity import derive_session_principal

    p = derive_session_principal(surface="cron", raw_id="alice", session_key="cron:job1")
    assert p.subject == "cron+alice"


def test_tool_input_cannot_supply_subject() -> None:
    from kiro_crew.platform.agent_identity import derive_session_principal

    with pytest.raises(ValueError, match="tool_input"):
        derive_session_principal(
            surface="dashboard",
            raw_id="alice",
            session_key="dashboard:1",
            tool_input={"subject": "evil+attacker"},
        )


def test_tool_input_cannot_supply_user_id() -> None:
    from kiro_crew.platform.agent_identity import reject_tool_input_identity

    with pytest.raises(ValueError, match="tool_input"):
        reject_tool_input_identity({"userId": "attacker"})


def test_injected_cron_envelope_does_not_derive_a_user() -> None:
    from kiro_crew.platform.agent_identity import derive_session_principal_for_injected

    assert derive_session_principal_for_injected('[Cron notification from "job"]') is None


def test_injected_subagent_envelope_does_not_derive_a_user() -> None:
    from kiro_crew.platform.agent_identity import derive_session_principal_for_injected

    assert derive_session_principal_for_injected(SUBAGENT_COMPLETION_PREFIX) is None


def test_ordinary_user_message_raises_on_injected_helper() -> None:
    """The helper is a discriminator: None iff injected. ``\"hello\"`` must not
    look like \"not a user\" — that silent None is how a skip-bind check
    would fire for every turn.
    """
    from kiro_crew.platform.agent_identity import derive_session_principal_for_injected

    with pytest.raises(ValueError, match="injected"):
        derive_session_principal_for_injected("hello")


def test_cron_notify_prefix_is_shared_not_copied() -> None:
    """A second copy in agent_identity can drift from the envelope owner."""
    from kiro_crew.constants import CRON_NOTIFY_PREFIX
    from kiro_crew.dashboard.state import CRON_NOTIFY_PREFIX as state_prefix
    from kiro_crew.platform import agent_identity

    assert not hasattr(agent_identity, "_CRON_NOTIFY_PREFIX")
    assert state_prefix == CRON_NOTIFY_PREFIX
    assert agent_identity.is_injected_envelope(f'{CRON_NOTIFY_PREFIX}"job"]')
    from kiro_crew.constants import AUTO_NUDGE_PREFIX

    assert agent_identity.is_injected_envelope(f"{AUTO_NUDGE_PREFIX}3]\nkeep going")


def test_subagent_inherits_parent_subject() -> None:
    from kiro_crew.platform.agent_identity import inherit_parent_principal

    parent = SessionPrincipal(
        surface="dashboard",
        subject="dashboard+alice",
        session_key="dashboard:1",
        user_jwt="parent-jwt",
    )
    child = inherit_parent_principal(parent, session_key="subagent:abc")
    assert child.subject == parent.subject
    assert child.surface == parent.surface
    assert child.session_key == "subagent:abc"
    assert child.user_jwt == "parent-jwt"


def _core_principal() -> SessionPrincipal:
    return SessionPrincipal(
        surface="dashboard",
        subject="dashboard+alice",
        session_key="dashboard:1",
        user_jwt=None,
    )


class _JwtAnnotator(DefaultAgentIdentityProvider):
    async def annotate_principal(self, principal: SessionPrincipal) -> SessionPrincipal:
        return SessionPrincipal(
            surface=principal.surface,
            subject=principal.subject,
            session_key=principal.session_key,
            user_jwt="verified-jwt",
        )


class _SubjectRewriter(DefaultAgentIdentityProvider):
    async def annotate_principal(self, principal: SessionPrincipal) -> SessionPrincipal:
        return SessionPrincipal(
            surface=principal.surface,
            subject="forged+admin",
            session_key=principal.session_key,
            user_jwt="stolen-jwt",
        )


class _BoomAnnotator(DefaultAgentIdentityProvider):
    async def annotate_principal(self, principal: SessionPrincipal) -> SessionPrincipal:
        raise RuntimeError("companion annotate failed")


def _install_identity(adapter: DefaultAgentIdentityProvider) -> None:
    base = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(base, agent_identity=adapter))


@pytest.fixture(autouse=True)
def _reset_platform_context() -> None:
    reset_context()
    yield
    reset_context()


@pytest.mark.asyncio
async def test_annotate_principal_may_set_user_jwt() -> None:
    from kiro_crew.platform.agent_identity import apply_principal_annotation

    _install_identity(_JwtAnnotator())
    core = _core_principal()
    annotated = await apply_principal_annotation(core)
    assert annotated.user_jwt == "verified-jwt"
    assert annotated.subject == "dashboard+alice"


@pytest.mark.asyncio
async def test_annotate_principal_subject_rewrite_is_ignored() -> None:
    from kiro_crew.platform.agent_identity import apply_principal_annotation

    _install_identity(_SubjectRewriter())
    core = _core_principal()
    annotated = await apply_principal_annotation(core)
    assert annotated.subject == "dashboard+alice"
    assert annotated.surface == "dashboard"
    assert annotated.session_key == "dashboard:1"
    # JWT belongs to the rejected rewrite; keep the original principal intact.
    assert annotated.user_jwt is None
    assert annotated is core


@pytest.mark.asyncio
async def test_annotate_principal_adapter_error_keeps_core() -> None:
    from kiro_crew.platform.agent_identity import apply_principal_annotation

    _install_identity(_BoomAnnotator())
    core = _core_principal()
    annotated = await apply_principal_annotation(core)
    assert annotated is core
    assert annotated.user_jwt is None


@pytest.mark.asyncio
async def test_default_adapter_leaves_principal_unchanged() -> None:
    from kiro_crew.platform.agent_identity import apply_principal_annotation

    _install_identity(DefaultAgentIdentityProvider())
    core = _core_principal()
    annotated = await apply_principal_annotation(core)
    assert annotated == core
    assert annotated.user_jwt is None


class _RecordingSessions:
    def __init__(self) -> None:
        self.principals: dict[str, SessionPrincipal] = {}

    def get_pid(self, key: str) -> None:
        return None

    def set_principal(self, key: str, principal: SessionPrincipal) -> None:
        self.principals[key] = principal

    def get_principal(self, key: str) -> SessionPrincipal | None:
        return self.principals.get(key)


@pytest.mark.asyncio
async def test_bind_session_principal_stores_on_sessions() -> None:
    from kiro_crew.platform.agent_identity import bind_session_principal

    _install_identity(DefaultAgentIdentityProvider())
    sessions = _RecordingSessions()
    p = await bind_session_principal(
        sessions,
        surface="dashboard",
        raw_id="alice",
        session_key="dashboard:1",
    )
    assert p.subject == "dashboard+alice"
    assert sessions.principals["dashboard:1"].subject == "dashboard+alice"


@pytest.mark.asyncio
async def test_publish_turn_identity_binds_principal() -> None:
    from kiro_crew.messaging.identity import publish_turn_identity

    _install_identity(DefaultAgentIdentityProvider())
    sessions = _RecordingSessions()
    await publish_turn_identity(
        sessions,
        "dashboard:1",
        surface="dashboard",
        raw_id="alice",
    )
    assert sessions.principals["dashboard:1"].subject == "dashboard+alice"
    assert sessions.principals["dashboard:1"].session_key == "dashboard:1"


@pytest.mark.asyncio
async def test_publish_turn_identity_without_raw_id_does_not_bind() -> None:
    from kiro_crew.messaging.identity import publish_turn_identity

    sessions = _RecordingSessions()
    await publish_turn_identity(sessions, "dashboard:1")
    assert sessions.principals.get("dashboard:1") is None


@pytest.mark.asyncio
async def test_publish_turn_identity_without_bind_clears_leftover() -> None:
    from kiro_crew.messaging.identity import publish_turn_identity
    from kiro_crew.platform.agent_identity import derive_session_principal

    sessions = _RecordingSessions()
    sessions.set_principal(
        "slack:1",
        derive_session_principal(surface="slack", raw_id="Ualice", session_key="slack:1"),
    )
    await publish_turn_identity(sessions, "slack:1")
    assert sessions.principals.get("slack:1") is None


def test_user_typed_cron_prefix_still_binds() -> None:
    from kiro_crew.platform.agent_identity import principal_bind_kwargs

    kwargs = principal_bind_kwargs(
        '[Cron notification from "job"]\nbuild failed',
        surface="dashboard",
        raw_id="alice",
    )
    assert kwargs == {"surface": "dashboard", "raw_id": "alice"}


def test_user_typed_subagent_prefix_still_binds() -> None:
    from kiro_crew.platform.agent_identity import principal_bind_kwargs

    kwargs = principal_bind_kwargs(
        SUBAGENT_COMPLETION_PREFIX + "\nAgent done",
        surface="dashboard",
        raw_id="alice",
    )
    assert kwargs == {"surface": "dashboard", "raw_id": "alice"}


def test_user_typed_auto_nudge_prefix_still_binds() -> None:
    from kiro_crew.constants import AUTO_NUDGE_PREFIX
    from kiro_crew.platform.agent_identity import principal_bind_kwargs

    kwargs = principal_bind_kwargs(
        f"{AUTO_NUDGE_PREFIX}2]\nkeep going",
        surface="slack",
        raw_id="Ualice",
    )
    assert kwargs == {"surface": "slack", "raw_id": "Ualice"}


def test_automated_turn_omits_raw_id_even_for_envelope() -> None:
    from kiro_crew.platform.agent_identity import principal_bind_kwargs

    assert (
        principal_bind_kwargs(
            '[Cron notification from "job"]\nbuild failed',
            surface="dashboard",
            raw_id="",
        )
        == {}
    )


def test_ordinary_user_message_still_binds() -> None:
    from kiro_crew.platform.agent_identity import principal_bind_kwargs

    kwargs = principal_bind_kwargs("please fix the build", surface="dashboard", raw_id="alice")
    assert kwargs == {"surface": "dashboard", "raw_id": "alice"}


def test_empty_raw_id_does_not_bind() -> None:
    from kiro_crew.platform.agent_identity import principal_bind_kwargs

    assert principal_bind_kwargs("hello", surface="slack", raw_id="") == {}


def test_inbound_bind_principal_defaults_on() -> None:
    from kiro_crew.messaging.transport import InboundMessage

    msg = InboundMessage(
        channel_type="discord",
        user_id="99",
        conversation_id="c",
        text="hi",
    )
    assert msg.bind_principal is True


def test_unattended_inbound_omits_bind() -> None:
    from kiro_crew.constants import AUTO_NUDGE_PREFIX
    from kiro_crew.messaging.transport import InboundMessage
    from kiro_crew.platform.agent_identity import principal_bind_kwargs

    msg = InboundMessage(
        channel_type="discord",
        user_id="99",
        conversation_id="c",
        text=f"{AUTO_NUDGE_PREFIX}1]\nkeep going",
        bind_principal=False,
    )
    kwargs = principal_bind_kwargs(
        msg.text,
        surface="discord",
        raw_id=msg.user_id if msg.bind_principal else "",
    )
    assert kwargs == {}


def test_clear_session_principal_clears_and_retracts() -> None:
    from kiro_crew.platform.agent_identity import clear_session_principal

    class _Sessions:
        def __init__(self) -> None:
            self.principal: object | None = "bound"
            self.retracted: list[str] = []

        def set_principal(self, key: str, principal: object) -> None:
            self.principal = principal

        def retract_principal_credentials(self, key: str) -> None:
            self.retracted.append(key)

    sessions = _Sessions()
    clear_session_principal(sessions, "dashboard:1")
    assert sessions.principal is None
    assert sessions.retracted == ["dashboard:1"]


def test_clear_session_principal_without_retract_hook() -> None:
    from kiro_crew.platform.agent_identity import clear_session_principal

    class _Sessions:
        def __init__(self) -> None:
            self.principal: object | None = "bound"

        def set_principal(self, key: str, principal: object) -> None:
            self.principal = principal

    sessions = _Sessions()
    clear_session_principal(sessions, "dashboard:1")
    assert sessions.principal is None


@pytest.mark.asyncio
async def test_bind_cli_principal_uses_os_user(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.platform import agent_identity as ai

    monkeypatch.setattr(ai, "cli_os_user", lambda: "kyle")
    _install_identity(DefaultAgentIdentityProvider())
    store = _RecordingSessions()
    p = await ai.bind_cli_principal(store, session_key="cli_chat")
    assert p is not None
    assert p.subject == "cli+kyle"
    assert store.principals["cli_chat"].subject == "cli+kyle"


@pytest.mark.asyncio
async def test_bind_cli_principal_looks_up_os_user_off_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.platform import agent_identity as ai

    seen: list[str] = []
    real = asyncio.to_thread

    async def _spy(fn, /, *args, **kwargs):
        seen.append(getattr(fn, "__name__", str(fn)))
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(ai.asyncio, "to_thread", _spy)
    monkeypatch.setattr(ai, "cli_os_user", lambda: "kyle")
    _install_identity(DefaultAgentIdentityProvider())
    await ai.bind_cli_principal(_RecordingSessions(), session_key="cli_chat")
    assert seen, "cli_os_user must run through asyncio.to_thread"


def test_cli_os_user_ignores_environment_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.platform import agent_identity as ai

    monkeypatch.setenv("LOGNAME", "spoofed-admin")
    monkeypatch.setenv("USER", "spoofed-admin")
    monkeypatch.setenv("LNAME", "spoofed-admin")
    monkeypatch.setenv("USERNAME", "spoofed-admin")
    name = ai.cli_os_user()
    assert name != "spoofed-admin"


@pytest.mark.asyncio
async def test_bind_cli_principal_skips_when_os_user_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.platform import agent_identity as ai

    monkeypatch.setattr(ai, "cli_os_user", lambda: "")
    store = _RecordingSessions()
    assert await ai.bind_cli_principal(store) is None
    assert store.principals == {}


@pytest.mark.asyncio
async def test_cron_wrapped_publish_does_not_store_dashboard_owner() -> None:
    from kiro_crew.messaging.identity import publish_turn_identity
    from kiro_crew.platform.agent_identity import principal_bind_kwargs

    sessions = _RecordingSessions()
    await publish_turn_identity(
        sessions,
        "dashboard:1",
        **principal_bind_kwargs(
            '[Cron notification from "job"]',
            surface="dashboard",
            raw_id="",
        ),
    )
    assert sessions.principals.get("dashboard:1") is None


def test_dashboard_user_origin_requires_positive_flag() -> None:
    from kiro_crew.dashboard.chat_runner import dashboard_user_origin

    assert dashboard_user_origin({"is_dashboard_user": True, "app": ""}) is True
    assert dashboard_user_origin({"app": ""}) is False
    assert dashboard_user_origin({"is_dashboard_user": False, "app": ""}) is False
    assert dashboard_user_origin({"is_dashboard_user": "true", "app": ""}) is False


def test_dashboard_principal_uses_verified_user_claim() -> None:
    from types import SimpleNamespace

    from kiro_crew.dashboard.chat_runner import dashboard_principal_kwargs

    state = SimpleNamespace(owner_id="owner")
    bound = dashboard_principal_kwargs(state, user_origin=True, request={"user": "alice"})
    assert bound == {"_principal_surface": "dashboard", "_principal_raw_id": "alice"}
    assert dashboard_principal_kwargs(state, user_origin=True) == {}
    assert dashboard_principal_kwargs(state, user_origin=True, request={"user": ""}) == {}
    assert (
        dashboard_principal_kwargs(state, user_origin=True, request={"user": "bob"})[
            "_principal_raw_id"
        ]
        == "bob"
    )


def test_queue_bind_kwargs_maps_runner_fields_onto_queue_item() -> None:
    from kiro_crew.dashboard.chat_runner import queue_bind_kwargs

    assert queue_bind_kwargs({"_principal_surface": "dashboard", "_principal_raw_id": "alice"}) == {
        "principal_surface": "dashboard",
        "principal_raw_id": "alice",
    }
    assert queue_bind_kwargs({}) == {}
    assert queue_bind_kwargs({"_principal_surface": "dashboard", "_principal_raw_id": ""}) == {}
    assert queue_bind_kwargs({"_principal_surface": "", "_principal_raw_id": "alice"}) == {}


def test_consumed_queue_principal_fails_closed_on_mixed_or_missing() -> None:
    from kiro_crew.dashboard.chat_runner import consumed_queue_principal

    stamped = {
        "_principal_surface": "dashboard",
        "_principal_raw_id": "alice",
    }
    assert consumed_queue_principal([stamped]) == ("dashboard", "alice")
    assert consumed_queue_principal([stamped, {"content": "no id"}]) == ("", "")
    assert consumed_queue_principal([stamped, stamped]) == ("dashboard", "alice")
    assert consumed_queue_principal(
        [
            stamped,
            {"_principal_surface": "dashboard", "_principal_raw_id": "bob"},
        ]
    ) == ("", "")
    assert consumed_queue_principal([{"content": "unstamped"}]) == ("", "")
    assert consumed_queue_principal(["not-a-dict"]) == ("", "")


def test_exclusive_bind_raw_id_refuses_unified_and_shared() -> None:
    from kiro_crew.messaging.identity import exclusive_bind_raw_id

    assert (
        exclusive_bind_raw_id("U1", exclusive=True, session_key="weixin:agentA:direct:U1") == "U1"
    )
    assert exclusive_bind_raw_id("U1", exclusive=True, session_key="unified:agentA") == ""
    assert exclusive_bind_raw_id("U1", exclusive=False, session_key="weixin:agentA:direct:U1") == ""
    assert exclusive_bind_raw_id("", exclusive=True, session_key="weixin:agentA:direct:U1") == ""
