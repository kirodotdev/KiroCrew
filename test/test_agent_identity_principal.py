"""Trusted AgentCore session principal — core-derived, never from tool input."""

from __future__ import annotations

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
    # JWT from the companion is kept; only the core-derived fields are pinned.
    assert annotated.user_jwt == "stolen-jwt"


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
    assert sessions.principals == {}


def test_cron_wrapped_message_does_not_bind_dashboard_owner() -> None:
    from kiro_crew.platform.agent_identity import principal_bind_kwargs

    kwargs = principal_bind_kwargs(
        '[Cron notification from "job"]\nbuild failed',
        surface="dashboard",
        raw_id="alice",
    )
    assert kwargs == {}


def test_subagent_completion_does_not_bind_dashboard_owner() -> None:
    from kiro_crew.platform.agent_identity import principal_bind_kwargs

    kwargs = principal_bind_kwargs(
        SUBAGENT_COMPLETION_PREFIX + "\nAgent done",
        surface="dashboard",
        raw_id="alice",
    )
    assert kwargs == {}


def test_ordinary_user_message_still_binds() -> None:
    from kiro_crew.platform.agent_identity import principal_bind_kwargs

    kwargs = principal_bind_kwargs("please fix the build", surface="dashboard", raw_id="alice")
    assert kwargs == {"surface": "dashboard", "raw_id": "alice"}


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
            raw_id="alice",
        ),
    )
    assert sessions.principals == {}
    assert not any(p.subject == "dashboard+alice" for p in sessions.principals.values())
