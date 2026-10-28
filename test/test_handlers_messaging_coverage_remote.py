"""Coverage for the ``/api/spawn`` handlers on a run placed on a registered crew.

Split out of ``test_handlers_messaging_coverage.py`` (file line cap): the remote
executor's lifecycle refusals, the floored remote shadow and the governance of
remote placement. Shared doubles live in ``handlers_messaging_coverage_helpers``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from handlers_messaging_coverage_helpers import _info, _mgr, _payload, _Req, _run, _state

import kiro_crew.dashboard.handlers.messaging as mod
import kiro_crew.execution_context as mod_execution


@pytest.mark.parametrize(
    "handler,body,local_method",
    [
        (mod.api_spawn_continue, {"task": "next"}, "continue_conversation"),
        (mod.api_spawn_steer, {"message": "adjust"}, "steer_run"),
        (mod.api_spawn_release, None, "release_conversation"),
        (mod.api_spawn_retry, None, "spawn"),
    ],
)
def test_remote_run_refuses_local_only_lifecycle_handlers(
    handler: Any, body: Any, local_method: str
) -> None:
    mgr = _mgr()
    mgr.get.return_value = _info(
        id="a1",
        done=True,
        outcome="failed",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
    )
    request = _Req(_state(subagents=mgr), body, match_info={"agent_id": "a1"})

    response = _run(handler, request)

    assert response.status == 409
    assert _payload(response)["code"] == "remote_operation_unsupported"
    getattr(mgr, local_method).assert_not_called()


def test_a_floored_remote_shadow_is_refused_as_remote() -> None:
    # This hub's shadow records the floor it placed the run under; a retry on it
    # is refused because the run is the peer's, not with the peer's floor answer.
    mgr = _mgr()
    mgr.get.return_value = _info(
        id="a1",
        done=True,
        outcome="failed",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
        approval_floor="interactive",
    )
    request = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})

    response = _run(mod.api_spawn_retry, request)

    assert response.status == 409
    assert _payload(response)["code"] == "remote_operation_unsupported"
    mgr.spawn.assert_not_called()


class TestRemoteSpawnGovernance:
    """``executor="remote"`` must not let a run leave this gateway's ceiling.

    The peer applies its own tool-approval policy, so placement needs the
    operator's opt-in; and the spawn governance and parent-spec allowlist that
    the local admission path enforces apply to a remote child too.
    """

    @staticmethod
    def _arm(monkeypatch: pytest.MonkeyPatch, *, opted_in: bool) -> MagicMock:
        cfg = SimpleNamespace(instances=SimpleNamespace(remote_subagents=opted_in))
        monkeypatch.setattr(mod, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
        service = MagicMock()
        service.spawn = AsyncMock(
            return_value=SimpleNamespace(
                id="loc1", instance_id="crew-a", remote_id="peer01", task="[scrubbed] x"
            )
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.remote_subagents.get_remote_subagent_service",
            lambda _state: service,
        )
        return service

    @staticmethod
    def _spawn(**extra: str) -> web.Response:
        body = {"task": "x", "executor": "remote", "agent": "scout", **extra}
        return _run(mod.api_spawn, _Req(_state(subagents=_mgr()), body))

    @staticmethod
    def _parent_runs_as(monkeypatch: pytest.MonkeyPatch, template: str | None) -> None:
        """The parent session's execution record names *template* (None: no record)."""
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        record = (
            ExecutionContext(None, MemoryStoreRef("default"), "template", template)
            if template is not None
            else None
        )
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution", lambda *_a, **_k: record
        )

    @pytest.mark.parametrize(
        "binding",
        [("worker", "member-worker"), (None, "research")],
        ids=["member-bound-parent", "named-store-parent"],
    )
    def test_a_bound_parent_cannot_spawn_remotely_without_naming_a_crew(
        self, monkeypatch: pytest.MonkeyPatch, binding
    ) -> None:
        # The child inherits the parent's binding through derive_execution, but
        # the peer gets no store identity and would read its own Global memory.
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        member, store = binding
        service = self._arm(monkeypatch, opted_in=True)
        monkeypatch.setattr(
            "kiro_crew.subagent._vet_remote_placement_governance", lambda *_a, **_k: None
        )
        monkeypatch.setattr("kiro_crew.subagent._vet_spawn_governance", lambda *_a, **_k: None)
        parent = ExecutionContext(member, MemoryStoreRef(store, member), "template", "kirocrew")
        monkeypatch.setattr(
            "kiro_crew.execution_context.derive_execution", lambda *_a, **_k: parent
        )

        response = self._spawn()

        assert response.status == 400
        assert _payload(response)["code"] == "remote_crew_binding_unsupported"
        service.spawn.assert_not_awaited()

    def test_refused_unless_the_operator_opted_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A Normal-mode parent cannot reach a peer whose looser approval policy
        # would run its tool calls unapproved: placement is off by default.
        service = self._arm(monkeypatch, opted_in=False)

        response = self._spawn()

        assert response.status == 403
        assert _payload(response)["code"] == "remote_subagents_disabled"
        service.spawn.assert_not_awaited()

    def test_the_parent_agent_reaches_the_remote_placement_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A Slack parent running as "lead" has no dashboard tab; the child asks
        # for "scout". A profile bound to the parent's agent must decide
        # placement, so "lead", read from the parent's record, is the agent vetted.
        service = self._arm(monkeypatch, opted_in=True)
        self._parent_runs_as(monkeypatch, "lead")
        seen: list[dict] = []

        def vet(*args: object, **kwargs: object) -> str:
            seen.append(dict(kwargs))
            return "remote sub-agent placement disabled by policy"

        monkeypatch.setattr("kiro_crew.subagent._vet_remote_placement_governance", vet)

        response = self._spawn(parent_session="slack:C1:123")

        assert response.status == 403
        assert seen == [{"agent": "lead"}]
        service.spawn.assert_not_awaited()

    def test_the_parent_agent_reaches_the_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The same agent narrows what the project snapshot may read.
        service = self._arm(monkeypatch, opted_in=True)
        self._parent_runs_as(monkeypatch, "lead")
        monkeypatch.setattr(
            "kiro_crew.subagent._vet_remote_placement_governance", lambda *_a, **_k: None
        )
        monkeypatch.setattr("kiro_crew.subagent._vet_spawn_governance", lambda *_a, **_k: None)
        monkeypatch.setattr(
            "kiro_crew.subagent._vet_parent_available_agents", lambda *_a, **_k: None
        )

        response = self._spawn(parent_session="slack:C1:123")

        assert response.status == 200
        assert service.spawn.await_args.kwargs["parent_agent"] == "lead"

    def test_a_named_parent_with_no_agent_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Without the parent's agent, a profile bound to it would be skipped,
        # so a named parent whose record cannot name one fails closed.
        service = self._arm(monkeypatch, opted_in=True)
        self._parent_runs_as(monkeypatch, None)
        vet = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.subagent._vet_remote_placement_governance", vet)

        response = self._spawn(parent_session="slack:C1:123")

        assert response.status == 403
        assert _payload(response)["code"] == "remote_parent_agent_unresolved"
        vet.assert_not_called()
        service.spawn.assert_not_awaited()

    def test_a_remote_placement_policy_denial_refuses_the_spawn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = self._arm(monkeypatch, opted_in=True)
        monkeypatch.setattr(
            "kiro_crew.subagent._vet_remote_placement_governance",
            lambda *_a, **_k: "remote sub-agent placement disabled by policy",
        )
        monkeypatch.setattr("kiro_crew.subagent._vet_spawn_governance", lambda *_a, **_k: None)

        response = self._spawn()

        assert response.status == 403
        assert _payload(response)["code"] == "remote_spawn_denied"
        service.spawn.assert_not_awaited()

    def test_spawn_governance_applies_to_remote_children(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = self._arm(monkeypatch, opted_in=True)
        seen: list[tuple[str, str]] = []
        parent_agents: list[str] = []

        def deny(parent: str, agent: str, app: str = "", *, parent_agent: str = "") -> str:
            seen.append((parent, agent))
            parent_agents.append(parent_agent)
            return "spawn capability disabled"

        monkeypatch.setattr("kiro_crew.subagent._vet_spawn_governance", deny)

        response = self._spawn()

        assert response.status == 403
        assert _payload(response)["code"] == "remote_spawn_denied"
        assert seen == [("", "scout")]
        # The parent's agent travels beside the child's, as a separate argument.
        assert len(parent_agents) == 1
        service.spawn.assert_not_awaited()

    def test_parent_available_agents_applies_to_remote_children(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = self._arm(monkeypatch, opted_in=True)
        monkeypatch.setattr("kiro_crew.subagent._vet_spawn_governance", lambda *_a, **_k: None)
        templates: list[str] = []

        def deny(_policy: Any, agent: str, *, app: str = "") -> str:
            templates.append(agent)
            return "not in availableAgents"

        monkeypatch.setattr("kiro_crew.subagent._vet_parent_available_agents", deny)

        response = self._spawn()

        assert response.status == 403
        assert _payload(response)["code"] == "remote_spawn_denied"
        assert templates == ["scout"]
        service.spawn.assert_not_awaited()

    def test_admitted_remote_spawn_reaches_the_peer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service = self._arm(monkeypatch, opted_in=True)
        monkeypatch.setattr("kiro_crew.subagent._vet_spawn_governance", lambda *_a, **_k: None)
        monkeypatch.setattr(
            "kiro_crew.subagent._vet_parent_available_agents", lambda *_a, **_k: None
        )

        response = self._spawn()

        assert response.status == 200
        assert _payload(response)["executor"] == "remote"
        service.spawn.assert_awaited_once()
        # The receipt echoes the placement's redacted copy, never the raw body.
        assert _payload(response)["task"] == "[scrubbed] x"

    def test_a_backend_cannot_be_chosen_for_a_remote_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = self._arm(monkeypatch, opted_in=True)

        async def known(raw: object) -> tuple[str | None, web.Response | None]:
            return str(raw), None

        monkeypatch.setattr(mod, "_requested_backend", known)
        body = {"task": "x", "executor": "remote", "agent": "scout", "backend": "claude"}
        response = _run(mod.api_spawn, _Req(_state(subagents=_mgr()), body))
        assert response.status == 400
        assert _payload(response)["code"] == "remote_backend_unsupported"
        service.spawn.assert_not_awaited()

    def test_silent_reaches_the_remote_placement(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service = self._arm(monkeypatch, opted_in=True)
        monkeypatch.setattr("kiro_crew.subagent._vet_spawn_governance", lambda *_a, **_k: None)
        monkeypatch.setattr(
            "kiro_crew.subagent._vet_parent_available_agents", lambda *_a, **_k: None
        )
        body = {"task": "x", "executor": "remote", "agent": "scout", "silent": True}
        response = _run(mod.api_spawn, _Req(_state(subagents=_mgr()), body))
        assert response.status == 200
        assert service.spawn.await_args.kwargs["silent"] is True

    def test_the_sdk_approval_mode_reaches_the_remote_floor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The hub derives the crew's approval floor from the same inputs the
        # local run step reads, so an SDK approval_mode=auto must reach it.
        service = self._arm(monkeypatch, opted_in=True)
        monkeypatch.setattr("kiro_crew.subagent._vet_spawn_governance", lambda *_a, **_k: None)
        monkeypatch.setattr(
            "kiro_crew.subagent._vet_parent_available_agents", lambda *_a, **_k: None
        )
        body = {"task": "x", "executor": "remote", "agent": "scout", "approval_mode": "auto"}

        response = _run(mod.api_spawn, _Req(_state(subagents=_mgr()), body))

        assert response.status == 200
        assert service.spawn.await_args.kwargs["approval_mode"] == "auto"

    @staticmethod
    def _parent_template(monkeypatch: pytest.MonkeyPatch, template: str, app: str = "") -> None:
        """Make admission resolve the PARENT's own template, as it does when the
        parent session has an execution record and no target member is named."""
        real = mod_execution.derive_execution

        def derive(parent: Any, **kw: Any) -> Any:
            out = real(parent, **kw)
            return SimpleNamespace(
                template_id=template, store=out.store, app=app, member_id=out.member_id
            )

        monkeypatch.setattr(mod_execution, "derive_execution", derive)

    def test_an_app_owned_parent_is_vetted_under_its_app_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An app-owned member session calls in over the loopback MCP secret, not
        # an app token; its app identity comes from the execution record, and
        # both gates must see it or the PROFILE half of POLICY ∩ PROFILE is skipped.
        service = self._arm(monkeypatch, opted_in=True)
        self._parent_template(monkeypatch, "worker-a", app="some-app")
        gov_apps: list[str] = []
        allow_apps: list[str] = []

        def governance(_parent: str, _agent: str, app: str = "", *, parent_agent: str = "") -> str:
            gov_apps.append(app)
            return "spawn disabled by the app profile"

        def allowlist(_policy: Any, _agent: str, *, app: str = "") -> None:
            allow_apps.append(app)

        monkeypatch.setattr("kiro_crew.subagent._vet_spawn_governance", governance)
        monkeypatch.setattr("kiro_crew.subagent._vet_parent_available_agents", allowlist)

        response = self._spawn()
        assert response.status == 403
        assert gov_apps == ["some-app"]
        service.spawn.assert_not_awaited()

        monkeypatch.setattr("kiro_crew.subagent._vet_spawn_governance", lambda *_a, **_k: None)
        response = self._spawn()
        assert allow_apps == ["some-app"]

    def test_an_app_owned_parent_may_place_only_its_own_agents(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Opus: the local admission gate refuses an app naming an agent it does
        not own; over remote placement the same request must be refused too."""
        service = self._arm(monkeypatch, opted_in=True)
        self._parent_template(monkeypatch, "worker-a", app="some-app")
        for gate in ("_vet_remote_placement_governance", "_vet_spawn_governance"):
            monkeypatch.setattr(f"kiro_crew.subagent.{gate}", lambda *_a, **_k: None)
        monkeypatch.setattr(
            "kiro_crew.subagent._vet_parent_available_agents", lambda *_a, **_k: None
        )
        checked: list[tuple[str, str]] = []

        def ownership(agent: str, app: str) -> str:
            checked.append((agent, app))
            return "" if agent == "own" else f"app {app!r} may only spawn its OWN agents"

        monkeypatch.setattr("kiro_crew.subagent._validate_app_agent_ownership", ownership)

        response = self._spawn(agent="kirocrew")
        assert response.status == 403
        assert _payload(response)["code"] == "remote_spawn_denied"
        service.spawn.assert_not_awaited()

        response = self._spawn(agent="own")
        assert response.status == 200
        assert checked == [("kirocrew", "some-app"), ("own", "some-app")]
        assert service.spawn.await_args.kwargs["agent"] == "own"

    def test_gates_check_the_named_child_not_the_parent_template(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A parent "orchestrator" allowed only itself must not pass the allowlist
        # on its own name and then run "admin-agent" on the peer.
        service = self._arm(monkeypatch, opted_in=True)
        self._parent_template(monkeypatch, "orchestrator")
        gov: list[str] = []
        allow: list[str] = []

        def governance(_parent: str, agent: str, app: str = "", *, parent_agent: str = "") -> None:
            gov.append(agent)

        def allowlist(_policy: Any, agent: str, *, app: str = "") -> str | None:
            allow.append(agent)
            return None if agent == "orchestrator" else "not in availableAgents"

        monkeypatch.setattr("kiro_crew.subagent._vet_spawn_governance", governance)
        monkeypatch.setattr("kiro_crew.subagent._vet_parent_available_agents", allowlist)

        body = {"task": "x", "executor": "remote", "agent": "admin-agent"}
        response = _run(mod.api_spawn, _Req(_state(subagents=_mgr()), body))

        assert response.status == 403
        assert gov == ["admin-agent"] and allow == ["admin-agent"]
        service.spawn.assert_not_awaited()

    def test_an_inherited_template_is_vetted_and_sent_to_the_peer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No explicit agent: the child inherits the parent's template, so the
        # governance agents scope sees that template (not ""), and the peer is
        # told which agent to run rather than falling back to its own default.
        service = self._arm(monkeypatch, opted_in=True)
        self._parent_template(monkeypatch, "worker-a")
        gov: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.subagent._vet_spawn_governance",
            lambda _p, agent, app="", **_k: gov.append(agent),
        )
        monkeypatch.setattr(
            "kiro_crew.subagent._vet_parent_available_agents", lambda *_a, **_k: None
        )

        body = {"task": "x", "executor": "remote"}
        response = _run(mod.api_spawn, _Req(_state(subagents=_mgr()), body))

        assert response.status == 200
        assert gov == ["worker-a"]
        assert service.spawn.await_args.kwargs["agent"] == "worker-a"

    def test_refusals_before_the_gates_are_audited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._arm(monkeypatch, opted_in=False)
        events: list[dict[str, Any]] = []
        fake_sel = SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw))
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: fake_sel)

        response = self._spawn()

        assert response.status == 403
        assert [e["outcome"] for e in events] == ["denied"]
        assert events[0]["metadata"] == {"executor": "remote"}
