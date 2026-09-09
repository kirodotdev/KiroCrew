"""Advisor dispatch: reviewer envelope through guard and severity routing.

Contract under test (see docs/system-specs/modules/advisor.md):

- ``dispatch_reviewer_result`` validates the envelope, admits notes through
  the emission guard, and routes by severity: a ``blocker`` goes to advisory
  delivery (steer or preserve); ``nit``/``concern`` become preserved Advisor
  cards + pending context, never steers.
- Malformed reviewer output degrades the session status and delivers
  nothing; the primary is never affected.
- Guard-suppressed notes deliver nothing.
- The final (``in_progress=False``) update resets the guard's per-update
  budget via ``begin_update`` on the next dispatch.
"""

from __future__ import annotations

import json
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.advisor.composition import AdvisorDispatcher
from kiro_crew.advisor.guard import EmissionGuard


@pytest.fixture(autouse=True)
def _hook_self_test_passes(monkeypatch):
    """The installer executes the real hook command as a self-test; these tests
    exercise the installer's other refusals, so the self-test is stubbed green
    (its own contract is covered by test_advisor_read_gate.py)."""
    from kiro_crew.advisor import read_gate

    monkeypatch.setattr(read_gate, "self_test", lambda cwd: "")


def _running_slot(state, key="test"):
    slot = state.get_or_create_slot(key)
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    client = MagicMock()
    client.supports_steer = True

    async def accept(message):
        return True

    client.steer = accept
    slot._acp_client = client
    return slot


def envelope(notes):
    return {"version": 1, "notes": notes}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    return st


@pytest.fixture
def dispatcher():
    return AdvisorDispatcher(guard=EmissionGuard())


class TestSeverityRouting:
    @pytest.mark.asyncio
    async def test_blocker_is_steered_into_running_turn(self, state, dispatcher):
        slot = _running_slot(state)
        outcome = await dispatcher.dispatch(
            state,
            slot,
            envelope([{"severity": "blocker", "text": "wrong table dropped"}]),
            advisor_update_id="u1",
        )
        assert outcome == {"steered": 1}
        row = slot.messages[-1]
        assert row["role"] == "advisor"
        assert row["meta"]["advisorSeverity"] == "blocker"

    @pytest.mark.asyncio
    async def test_blockers_past_the_interruption_cap_are_preserved_not_steered(
        self, state, dispatcher
    ):
        from kiro_crew.advisor.guard import DEFAULT_INTERRUPTION_CAP

        slot = _running_slot(state)
        steer_calls = []

        async def record_steer(message):
            steer_calls.append(message)
            return True

        slot._acp_client.steer = record_steer
        notes = [
            {"severity": "blocker", "text": f"blocker number {i}"}
            for i in range(DEFAULT_INTERRUPTION_CAP + 2)
        ]
        outcome = await dispatcher.dispatch(state, slot, envelope(notes), advisor_update_id="u1")
        assert outcome == {"steered": DEFAULT_INTERRUPTION_CAP, "preserved": 2}
        assert len(steer_calls) == DEFAULT_INTERRUPTION_CAP
        advisor_rows = [m for m in slot.messages if m.get("role") == "advisor"]
        assert (
            len(advisor_rows) == DEFAULT_INTERRUPTION_CAP + 2
        ), "every blocker still reaches the user"

    @pytest.mark.asyncio
    async def test_nit_becomes_preserved_card_never_steer(self, state, dispatcher):
        slot = _running_slot(state)
        steer_calls = []

        async def record_steer(message):
            steer_calls.append(message)
            return True

        slot._acp_client.steer = record_steer
        outcome = await dispatcher.dispatch(
            state,
            slot,
            envelope([{"severity": "nit", "text": "typo in comment"}]),
            advisor_update_id="u2",
        )
        assert outcome == {"preserved": 1}
        assert steer_calls == []
        row = slot.messages[-1]
        assert row["role"] == "advisor"
        assert row["meta"]["advisorState"] == "preserved"

    @pytest.mark.asyncio
    async def test_concern_is_preserved(self, state, dispatcher):
        slot = _running_slot(state)
        outcome = await dispatcher.dispatch(
            state,
            slot,
            envelope([{"severity": "concern", "text": "unbounded retry"}]),
            advisor_update_id="u3",
        )
        assert outcome == {"preserved": 1}

    @pytest.mark.asyncio
    async def test_mixed_severities_route_independently(self, state, dispatcher):
        slot = _running_slot(state)
        outcome = await dispatcher.dispatch(
            state,
            slot,
            envelope(
                [
                    {"severity": "nit", "text": "naming"},
                    {"severity": "blocker", "text": "data loss"},
                ]
            ),
            advisor_update_id="u4",
        )
        assert outcome == {"preserved": 1, "steered": 1}


class TestGuardAndDegradation:
    @pytest.mark.asyncio
    async def test_malformed_envelope_degrades_and_delivers_nothing(self, state, dispatcher):
        slot = _running_slot(state)
        before = len(slot.messages)
        outcome = await dispatcher.dispatch(state, slot, "free-form prose", advisor_update_id="u5")
        assert outcome == {"degraded": 1}
        assert len(slot.messages) == before

    @pytest.mark.asyncio
    async def test_suppressed_duplicate_delivers_nothing(self, state, dispatcher):
        slot = _running_slot(state)
        note = [{"severity": "concern", "text": "same finding"}]
        await dispatcher.dispatch(state, slot, envelope(note), advisor_update_id="u6")
        before = len(slot.messages)
        outcome = await dispatcher.dispatch(state, slot, envelope(note), advisor_update_id="u7")
        assert outcome == {}
        assert len(slot.messages) == before

    @pytest.mark.asyncio
    async def test_empty_notes_is_a_clean_noop(self, state, dispatcher):
        slot = _running_slot(state)
        outcome = await dispatcher.dispatch(state, slot, envelope([]), advisor_update_id="u8")
        assert outcome == {}


@pytest.fixture(autouse=True)
def _masking_host(monkeypatch):
    from kiro_crew.advisor import composition

    monkeypatch.setattr(
        composition, "credential_mask_applies", lambda mode, **_kw: True, raising=False
    )


class TestAdvisorAgentMaterialization:
    """The packaged reviewer agent spec reaches the kiro agents dir.

    Found by the live-gateway e2e: the runtime spawns with
    agent="kirocrew-advisor", which only resolves if the packaged JSON has
    been materialized into the agents dir.
    """

    def test_installs_the_packaged_spec_when_absent(self, tmp_path):
        import json

        from kiro_crew.advisor.composition import ensure_advisor_agent_installed

        installed = ensure_advisor_agent_installed(agents_dir=tmp_path)
        assert installed == tmp_path / "kirocrew-advisor.json"
        spec = json.loads(installed.read_text())
        assert spec["name"] == "kirocrew-advisor"
        assert spec["tools"] == ["fs_read", "grep"]

    def test_overwrites_an_existing_spec_from_the_package(self, tmp_path):
        """A managed agent: a hand-edited managed file is replaced from the package."""
        import json

        from kiro_crew.advisor.composition import (
            MANAGED_DESCRIPTION_PREFIX,
            ensure_advisor_agent_installed,
        )

        target = tmp_path / "kirocrew-advisor.json"
        target.write_text(
            json.dumps(
                {
                    "name": "kirocrew-advisor",
                    "description": MANAGED_DESCRIPTION_PREFIX,
                    "user": "edited",
                }
            )
        )
        ensure_advisor_agent_installed(agents_dir=tmp_path)
        assert "edited" not in target.read_text()

    def test_is_idempotent(self, tmp_path):
        from kiro_crew.advisor.composition import ensure_advisor_agent_installed

        first = ensure_advisor_agent_installed(agents_dir=tmp_path)
        second = ensure_advisor_agent_installed(agents_dir=tmp_path)
        assert first == second

    def test_existing_spec_grants_do_not_survive_a_launch(self, tmp_path, monkeypatch):
        """An on-disk spec is a persistent artifact: a hand-added
        `allowedTools` grant must not outlive the next launch."""
        import json

        from kiro_crew.advisor.composition import (
            MANAGED_DESCRIPTION_PREFIX,
            ensure_advisor_agent_installed,
        )

        target = tmp_path / "kirocrew-advisor.json"
        target.write_text(
            json.dumps(
                {
                    "name": "kirocrew-advisor",
                    "description": MANAGED_DESCRIPTION_PREFIX,
                    "user": "edited",
                    "allowedTools": ["fs_read", "execute_bash"],
                }
            )
        )
        monkeypatch.setattr("kiro_crew.agent._may_auto_approve", lambda ref: ref == "fs_read")
        ensure_advisor_agent_installed(agents_dir=tmp_path)
        spec = json.loads(target.read_text())
        # the packaged spec carries NO auto-approvals and is the only source
        assert "user" not in spec
        assert spec.get("allowedTools", []) == []

    def test_fresh_install_is_ceiling_filtered_too(self, tmp_path, monkeypatch):
        import json

        from kiro_crew.advisor.composition import ensure_advisor_agent_installed

        monkeypatch.setattr("kiro_crew.agent._may_auto_approve", lambda ref: False)
        installed = ensure_advisor_agent_installed(agents_dir=tmp_path)
        spec = json.loads(installed.read_text())
        assert spec.get("allowedTools", []) == []


class TestReviewerModelAndWorkdirPlumbing:
    """Round-4: advisor.model must select the reviewer model, and each
    reviewer session must run in the observed slot's own workspace."""

    def test_reviewer_model_reaches_the_runtime(self, monkeypatch):
        import kiro_crew.advisor.composition as comp

        seen = {}

        def fake_create(*, agent, work_dir, model=None, **kw):
            seen["agent"] = agent
            seen["model"] = model
            return object()

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.create_agent_runtime", fake_create)
        pool = comp.build_reviewer_runtime("reviewer-x", work_dir=None)
        pool._runtime_factory()
        assert seen["model"] == "reviewer-x"
        assert seen["agent"] == comp.ADVISOR_AGENT_NAME

    def test_empty_reviewer_model_selects_runtime_default(self, monkeypatch):
        import kiro_crew.advisor.composition as comp

        seen = {}

        def fake_create(*, agent, work_dir, model=None, **kw):
            seen["model"] = model
            return object()

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.create_agent_runtime", fake_create)
        comp.build_reviewer_runtime("", work_dir=None)._runtime_factory()
        assert seen["model"] is None

    @pytest.mark.asyncio
    async def test_prompt_fn_uses_the_payloads_work_dir(self, monkeypatch):
        import kiro_crew.advisor.composition as comp

        seen = {}

        async def fake_prompt_for_reply(runtime, *, cwd, prompt, **kw):
            seen["cwd"] = cwd
            from kiro_crew.agent_sdk.oneshot import OneShotReply

            return OneShotReply("ok", None)

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.prompt_for_reply", fake_prompt_for_reply)
        pool = comp.build_reviewer_runtime("m", work_dir="/gateway/cwd")
        out = await pool._prompt_fn(
            object(),
            {"_runtime": object(), "prompt": "p", "work_dir": "/parent/project"},
        )
        assert out == "ok"
        assert (
            seen["cwd"] == "/parent/project"
        ), "the reviewer session must run in the observed slot's workspace"

    @pytest.mark.asyncio
    async def test_prompt_fn_falls_back_to_the_pool_work_dir(self, monkeypatch):
        import kiro_crew.advisor.composition as comp

        seen = {}

        async def fake_prompt_for_reply(runtime, *, cwd, prompt, **kw):
            seen["cwd"] = cwd
            from kiro_crew.agent_sdk.oneshot import OneShotReply

            return OneShotReply("ok", None)

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.prompt_for_reply", fake_prompt_for_reply)
        pool = comp.build_reviewer_runtime("m", work_dir="/gateway/cwd")
        await pool._prompt_fn(object(), {"_runtime": object(), "prompt": "p"})
        assert seen["cwd"] == "/gateway/cwd"


class TestReviewerUsagePersistence:
    """Round-4: reviewer spend must actually be persisted (FP blocker 3)."""

    @pytest.mark.asyncio
    async def test_reviewer_turn_persists_an_attributed_usage_row(self, monkeypatch):
        import kiro_crew.advisor.composition as comp
        from kiro_crew.advisor.runtime import ReviewerSession

        terminal = object()

        class Reply:
            text = '{"version": 1, "notes": []}'
            model = "served-model-y"  # the backend-resolved id wins attribution

        Reply.terminal = terminal

        async def fake_prompt_for_reply(runtime, *, cwd, prompt, **kw):
            return Reply

        persisted = {}

        async def fake_persist(slot_key, model, event, provider="", **kw):
            persisted["slot_key"] = slot_key
            persisted["model"] = model
            persisted["event"] = event
            persisted.update(kw)

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.prompt_for_reply", fake_prompt_for_reply)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.usage.persist_token_record_async",
            fake_persist,
        )
        pool = comp.build_reviewer_runtime("reviewer-x", work_dir=None)
        session = ReviewerSession(parent_session_key="dashboard:p")
        out = await pool._prompt_fn(
            session,
            {
                "_runtime": object(),
                "prompt": "p",
                "advisor_update_id": "dashboard:p:2:7",
            },
        )
        assert out == Reply.text
        assert persisted["slot_key"] == session.session_id
        assert persisted["slot_key"].startswith("advisor:")
        assert persisted["model"] == "served-model-y"  # served id outranks configured
        assert persisted["event"] is terminal
        assert persisted["surface"] == "advisor"
        assert "parent_session_key" not in persisted  # the advisory row carries the parent link

    @pytest.mark.asyncio
    async def test_usage_persistence_failure_never_breaks_the_review(self, monkeypatch):
        import kiro_crew.advisor.composition as comp
        from kiro_crew.advisor.runtime import ReviewerSession

        class Reply:
            text = "raw"
            terminal = object()

        async def fake_prompt_for_reply(runtime, *, cwd, prompt, **kw):
            return Reply

        async def broken_persist(*a, **kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.prompt_for_reply", fake_prompt_for_reply)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.usage.persist_token_record_async",
            broken_persist,
        )
        pool = comp.build_reviewer_runtime("m", work_dir=None)
        session = ReviewerSession(parent_session_key="dashboard:p")
        out = await pool._prompt_fn(
            session, {"_runtime": object(), "prompt": "p", "advisor_update_id": "x"}
        )
        assert out == "raw"


class TestEveryReadOperationIsJudged:
    """kiro-cli's ``fs_read`` batches ``operations[]``; an Image operation names
    its targets under ``image_paths``. A batch whose first operation carries a
    clean ``path`` must not smuggle a denied image past the gate, and an
    operation that exposes no recognizable target is unverifiable and denied."""

    def _ev(self, **inp):
        import json

        return SimpleNamespace(
            kind="permission_request",
            request_id="r",
            tool_kind="read",
            tool_name="fs_read",
            title="fs_read",
            tool_input=json.dumps(inp),
            raw_tool_params=inp,
        )

    def test_image_paths_are_target_paths(self):
        from kiro_crew.platform.tool_paths import target_paths

        found = target_paths(
            {"operations": [{"path": "a.txt"}, {"mode": "Image", "image_paths": ["/p/x.png"]}]}
        )
        assert list(found) == ["a.txt", "/p/x.png"]

    def test_a_denied_image_beside_a_clean_path_is_blocked(self, tmp_path, monkeypatch):
        from kiro_crew.advisor import composition
        from kiro_crew.advisor.composition import advisor_permission_gate

        monkeypatch.setattr(composition, "_hook_manager", lambda: pytest.fail("floor denies first"))
        ev = self._ev(
            operations=[
                {"path": str(tmp_path / "allowed.txt")},
                {"mode": "Image", "image_paths": [str(Path.home() / ".ssh" / "key.png")]},
            ]
        )
        reason = advisor_permission_gate(ev, cwd=str(tmp_path))
        assert "sensitive" in reason

    def test_an_operation_with_no_recognizable_target_is_denied(self, tmp_path, monkeypatch):
        from kiro_crew.advisor import composition
        from kiro_crew.advisor.composition import advisor_permission_gate

        monkeypatch.setattr(
            composition, "_hook_manager", lambda: pytest.fail("denied before the hook")
        )
        ev = self._ev(
            operations=[{"path": str(tmp_path / "ok.py")}, {"mode": "Mystery", "target": "x"}]
        )
        reason = advisor_permission_gate(ev, cwd=str(tmp_path))
        assert reason
        assert "verif" in reason


class TestAdvisorPermissionGate:
    """Round-41: the reviewer's tools are NOT auto-approved. Every request is
    judged by the advisor gate: read-only tool ceiling, the platform's
    PreToolUse hook (fail-closed), and is_sensitive_path on every path arg."""

    def _ev(self, tool, **inp):
        import json

        return SimpleNamespace(
            kind="permission_request",
            request_id="r",
            tool_kind=tool,
            tool_name=tool,
            title=tool,
            tool_input=json.dumps(inp),
        )

    def test_packaged_spec_has_no_auto_approvals(self):
        import json
        from pathlib import Path

        import kiro_crew.advisor.composition as comp

        spec = json.loads(
            (Path(comp.__file__).parent / "agents" / "kirocrew-advisor.json").read_text()
        )
        assert spec.get("allowedTools", []) == []
        assert spec["tools"] == ["fs_read", "grep"]

    def test_non_readonly_tool_denied_before_hook(self, monkeypatch):
        from kiro_crew.advisor.composition import advisor_permission_gate

        called = []
        monkeypatch.setattr(
            "kiro_crew.advisor.composition._hook_manager",
            lambda: SimpleNamespace(on_tool_call=lambda *a, **k: called.append(1)),
        )
        reason = advisor_permission_gate(self._ev("execute_bash", command="ls"), cwd="/tmp")
        assert reason and called == []

    def test_missing_tool_name_is_denied_even_with_a_readonly_kind(self, monkeypatch, tmp_path):
        """Round-82 (GPT): the ceiling keys on the trusted tool NAME; a request
        that carries no name must not be authorized by its self-declared kind,
        or a forged ``kind="fs_read"`` would run an arbitrary tool unattended."""
        from kiro_crew.advisor.composition import advisor_permission_gate

        called = []
        monkeypatch.setattr(
            "kiro_crew.advisor.composition._hook_manager",
            lambda: SimpleNamespace(on_tool_call=lambda *a, **k: called.append(1)),
        )
        ok = tmp_path / "ok.py"
        ok.write_text("x = 1\n")
        ev = SimpleNamespace(
            kind="permission_request",
            request_id="r",
            tool_kind="fs_read",
            tool_name="",
            title="Read",
            tool_input=json.dumps({"path": str(ok)}),
        )
        reason = advisor_permission_gate(ev, cwd=str(tmp_path))
        assert reason and called == []

    def test_sensitive_path_denied(self, monkeypatch, tmp_path):
        from kiro_crew.advisor.composition import advisor_permission_gate

        monkeypatch.setattr(
            "kiro_crew.advisor.composition._hook_manager",
            lambda: SimpleNamespace(
                on_tool_call=lambda *a, **k: SimpleNamespace(action="allow", reason="")
            ),
        )
        reason = advisor_permission_gate(
            self._ev("fs_read", path="~/.aws/credentials"), cwd=str(tmp_path)
        )
        assert reason and "sensitive" in reason.lower()

    def test_ordinary_read_approved(self, monkeypatch, tmp_path):
        from kiro_crew.advisor.composition import advisor_permission_gate

        monkeypatch.setattr(
            "kiro_crew.advisor.composition._hook_manager",
            lambda: SimpleNamespace(
                on_tool_call=lambda *a, **k: SimpleNamespace(action="allow", reason="")
            ),
        )
        f = tmp_path / "main.py"
        f.write_text("x = 1\n")
        assert advisor_permission_gate(self._ev("fs_read", path=str(f)), cwd=str(tmp_path)) == ""

    def test_hook_deny_and_hook_failure_both_deny(self, monkeypatch, tmp_path):
        from kiro_crew.advisor.composition import advisor_permission_gate

        monkeypatch.setattr(
            "kiro_crew.advisor.composition._hook_manager",
            lambda: SimpleNamespace(
                on_tool_call=lambda *a, **k: SimpleNamespace(action="deny", reason="policy")
            ),
        )
        assert advisor_permission_gate(self._ev("grep", path=str(tmp_path)), cwd=str(tmp_path))

        def broken():
            raise RuntimeError("hooks unavailable")

        monkeypatch.setattr("kiro_crew.advisor.composition._hook_manager", broken)
        assert advisor_permission_gate(self._ev("grep", path=str(tmp_path)), cwd=str(tmp_path))

    def test_ceiling_keys_on_tool_name_not_generic_kind(self, monkeypatch, tmp_path):
        """Round-43: ACP reports fs_read with the generic kind 'read'; the
        ceiling must authorize on the trusted tool NAME."""
        import json

        from kiro_crew.advisor.composition import advisor_permission_gate

        monkeypatch.setattr(
            "kiro_crew.advisor.composition._hook_manager",
            lambda: SimpleNamespace(
                on_tool_call=lambda *a, **k: SimpleNamespace(action="allow", reason="")
            ),
        )
        f = tmp_path / "a.py"
        f.write_text("x\n")
        ev = SimpleNamespace(
            kind="permission_request",
            request_id="r",
            tool_kind="read",  # generic ACP kind
            tool_name="fs_read",
            title="Read a.py",
            tool_input=json.dumps({"path": str(f)}),
        )
        assert advisor_permission_gate(ev, cwd=str(tmp_path)) == ""
        # and a non-read-only NAME is denied even under a benign generic kind
        ev2 = SimpleNamespace(
            kind="permission_request",
            request_id="r",
            tool_kind="read",
            tool_name="execute_bash",
            title="ls",
            tool_input="{}",
        )
        assert advisor_permission_gate(ev2, cwd=str(tmp_path))

    def test_hook_receives_trusted_tool_name(self, monkeypatch, tmp_path):
        """Round-45: per-tool governance matches on mcp_tool_name; the gate
        must hand the hook the trusted tool NAME, not just title and kind."""
        import json

        from kiro_crew.advisor.composition import advisor_permission_gate

        seen = {}

        def on_tool_call(*a, **k):
            seen.update(k)
            return SimpleNamespace(action="allow", reason="")

        monkeypatch.setattr(
            "kiro_crew.advisor.composition._hook_manager",
            lambda: SimpleNamespace(on_tool_call=on_tool_call),
        )
        f = tmp_path / "a.py"
        f.write_text("x\n")
        ev = SimpleNamespace(
            kind="permission_request",
            request_id="r",
            tool_kind="read",
            tool_name="fs_read",
            title="benign title",
            tool_input=json.dumps({"path": str(f)}),
            mcp_server_name="srv",
            mcp_identity_trusted=True,
        )
        assert advisor_permission_gate(ev, cwd=str(tmp_path)) == ""
        assert seen.get("mcp_tool_name") == "fs_read"
        assert seen.get("mcp_server_name") == "srv"
        assert seen.get("mcp_identity_trusted") is True
        assert seen.get("tool_kind") == "read"

    @pytest.mark.parametrize(
        "tool_input, raw",
        [
            ("", None),  # nothing at all
            ("not json", None),  # unparseable
            ('["a.py"]', None),  # parseable but not a dict
            ("", "path=a.py"),  # raw params not a dict
        ],
    )
    def test_unverifiable_arguments_deny(self, monkeypatch, tmp_path, tool_input, raw):
        """Round-47: no valid argument dictionary -> nothing reaches the path
        checks -> the request must be DENIED, never approved unverified."""
        from kiro_crew.advisor.composition import advisor_permission_gate

        monkeypatch.setattr(
            "kiro_crew.advisor.composition._hook_manager",
            lambda: SimpleNamespace(
                on_tool_call=lambda *a, **k: SimpleNamespace(action="allow", reason="")
            ),
        )
        ev = SimpleNamespace(
            kind="permission_request",
            request_id="r",
            tool_kind="read",
            tool_name="fs_read",
            title="Read",
            tool_input=tool_input,
            raw_tool_params=raw,
        )
        reason = advisor_permission_gate(ev, cwd=str(tmp_path))
        assert reason and "argument" in reason.lower()

    def test_path_argument_of_unexpected_type_denies(self, monkeypatch, tmp_path):
        import json

        from kiro_crew.advisor.composition import advisor_permission_gate

        monkeypatch.setattr(
            "kiro_crew.advisor.composition._hook_manager",
            lambda: SimpleNamespace(
                on_tool_call=lambda *a, **k: SimpleNamespace(action="allow", reason="")
            ),
        )
        ev = SimpleNamespace(
            kind="permission_request",
            request_id="r",
            tool_kind="read",
            tool_name="fs_read",
            title="Read",
            tool_input=json.dumps({"path": {"nested": "~/.ssh/id_rsa"}}),
        )
        assert advisor_permission_gate(ev, cwd=str(tmp_path))

    def test_batched_read_shape_is_path_checked(self, monkeypatch, tmp_path):
        """Round-76 (Opus): fs_read carries its targets NESTED
        (``{"operations": [{"mode": "Line", "path": ...}]}``). A top-level-only
        scan saw no path and let the call through to the hook; the gate must
        walk the arguments the way the platform keystone does."""
        from kiro_crew.advisor.composition import advisor_permission_gate

        monkeypatch.setattr(
            "kiro_crew.advisor.composition._hook_manager",
            lambda: SimpleNamespace(
                on_tool_call=lambda *a, **k: SimpleNamespace(action="allow", reason="")
            ),
        )
        reason = advisor_permission_gate(
            self._ev("fs_read", operations=[{"mode": "Line", "path": "~/.aws/credentials"}]),
            cwd=str(tmp_path),
        )
        assert reason and "sensitive" in reason.lower()
        ok = tmp_path / "ok.py"
        ok.write_text("x = 1\n")
        assert (
            advisor_permission_gate(
                self._ev("fs_read", operations=[{"mode": "Line", "path": str(ok)}]),
                cwd=str(tmp_path),
            )
            == ""
        )

    def test_read_without_a_visible_target_denies(self, monkeypatch, tmp_path):
        """A read whose arguments expose no path at all (an unknown shape, or a
        truncated walk) cannot be path-checked and is denied, not approved."""
        from kiro_crew.advisor import composition
        from kiro_crew.advisor.composition import advisor_permission_gate
        from kiro_crew.platform.tool_paths import TargetPaths

        monkeypatch.setattr(
            "kiro_crew.advisor.composition._hook_manager",
            lambda: SimpleNamespace(
                on_tool_call=lambda *a, **k: SimpleNamespace(action="allow", reason="")
            ),
        )
        assert advisor_permission_gate(
            self._ev("fs_read", target="a.txt"), cwd=str(tmp_path)  # unknown key
        )
        truncated = TargetPaths([str(tmp_path / "ok.py")])
        truncated.truncated = True
        monkeypatch.setattr(composition, "target_paths", lambda raw: truncated)
        reason = advisor_permission_gate(
            self._ev("fs_read", path=str(tmp_path / "ok.py")), cwd=str(tmp_path)
        )
        assert reason and "verified" in reason.lower()

    def test_gate_dependencies_are_module_scope_imports(self):
        import ast
        import inspect

        from kiro_crew.advisor import composition

        tree = ast.parse(inspect.getsource(composition))
        top = {n.module for n in tree.body if isinstance(n, ast.ImportFrom) and n.module}
        assert {"kiro_crew.config", "kiro_crew.hooks", "kiro_crew.security.paths"} <= top

    @pytest.mark.parametrize("tool", ["grep"])
    def test_ancestor_of_sensitive_leaf_is_denied(self, monkeypatch, tmp_path, tool):
        """Round-48 (Opus): a recursive search rooted at an ANCESTOR of a
        fenced leaf (~, /home/<user>) reaches what a direct read is denied --
        the gate must deny ancestors, not only exact sensitive paths."""
        import json

        from kiro_crew.advisor.composition import advisor_permission_gate

        monkeypatch.setattr(
            "kiro_crew.advisor.composition._hook_manager",
            lambda: SimpleNamespace(
                on_tool_call=lambda *a, **k: SimpleNamespace(action="allow", reason="")
            ),
        )
        # Stub the fence (as every consumer test in the repo does) rather
        # than relocating HOME: the gate under test is the CALL, the fence
        # itself is pinned in test_security.py.
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(
            "kiro_crew.advisor.composition.path_contains_sensitive",
            lambda p, base_dir=None: str(p).rstrip("/") == str(home),
        )
        ev = SimpleNamespace(
            kind="permission_request",
            request_id="r",
            tool_kind="search",
            tool_name=tool,
            title="search",
            tool_input=json.dumps({"path": str(home), "pattern": "AKIA"}),
        )
        reason = advisor_permission_gate(ev, cwd=str(tmp_path / "proj"))
        assert reason and "sensitive" in reason.lower()

    @pytest.mark.parametrize("tool", ["grep"])
    def test_pathless_search_validates_cwd_as_root(self, monkeypatch, tmp_path, tool):
        """Round-49: a recursive search with NO path argument defaults to the
        reviewer cwd -- that implicit root must be checked like an explicit
        one, and denied when no cwd is known."""
        import json

        from kiro_crew.advisor.composition import advisor_permission_gate

        monkeypatch.setattr(
            "kiro_crew.advisor.composition._hook_manager",
            lambda: SimpleNamespace(
                on_tool_call=lambda *a, **k: SimpleNamespace(action="allow", reason="")
            ),
        )
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(
            "kiro_crew.advisor.composition.path_contains_sensitive",
            lambda p, base_dir=None: str(p).rstrip("/") == str(home),
        )
        ev = SimpleNamespace(
            kind="permission_request",
            request_id="r",
            tool_kind="search",
            tool_name=tool,
            title="search",
            tool_input=json.dumps({"pattern": "AKIA"}),
        )
        # cwd is an ancestor of a protected dir -> denied
        assert "sensitive" in advisor_permission_gate(ev, cwd=str(home)).lower()
        # no cwd at all -> the root is unknowable -> denied
        assert advisor_permission_gate(ev, cwd=None)
        # a clean project cwd -> approved
        proj = tmp_path / "proj"
        proj.mkdir()
        assert advisor_permission_gate(ev, cwd=str(proj)) == ""


class TestDefaultWorkspaceIsResolvedOnce:
    """Round-54 (GPT): with no work_dir on the payload or the pool, the
    runtime opens its session in the crew default workspace -- the permission
    gate must resolve RELATIVE paths against that same directory, not the
    gateway process cwd, or `../<file>` is judged against one tree and read
    from another."""

    @pytest.mark.asyncio
    async def test_session_and_gate_share_the_resolved_default_cwd(self, monkeypatch, tmp_path):
        import kiro_crew.advisor.composition as comp

        monkeypatch.setattr(comp, "config_dir", lambda: tmp_path)
        seen = {}

        async def fake_prompt_for_reply(runtime, *, cwd, prompt, permission_gate, **kw):
            seen["session_cwd"] = cwd
            permission_gate(SimpleNamespace(kind="permission_request"))
            from kiro_crew.agent_sdk.oneshot import OneShotReply

            return OneShotReply("ok", None)

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.prompt_for_reply", fake_prompt_for_reply)

        def fake_gate(ev, *, cwd):
            seen["gate_cwd"] = cwd
            return ""

        monkeypatch.setattr(comp, "advisor_permission_gate", fake_gate)
        pool = comp.build_reviewer_runtime("m", work_dir=None)
        await pool._prompt_fn(object(), {"_runtime": object(), "prompt": "p"})
        expected = str(tmp_path / "workspace")
        assert seen["session_cwd"] == expected
        assert seen["gate_cwd"] == expected


class TestReviewerSpawnsFromSealedDirectory:
    """Round-58/60 (GPT): kiro-cli resolves ``--agent`` against the PROCESS
    cwd at spawn, so a preflight check of a writable directory is racy by
    construction -- and a temp dir under the crew home is agent-writable too.
    The reviewer process is spawned from the kiro agents tree, which the
    sandbox seals read-only for every agent process; the observed workspace
    is only ever the SESSION cwd."""

    def test_factory_spawns_from_a_crew_owned_cwd_outside_the_agents_tree(
        self, monkeypatch, tmp_path
    ):
        """Round-84 (GPT): kiro-cli resolves ``--agent`` against ``<cwd>/.kiro/agents``
        first, so the reviewer's process cwd must be a directory no agent can
        write a spec into -- and NOT the agents tree itself, which the delegated
        sandboxes (Windows, macOS internal) refuse as a workspace. It is a
        crew-owned directory under the config dir (write-protected for every
        agent), created alongside the spec."""
        import kiro_crew.advisor.composition as comp

        monkeypatch.setattr(comp, "kiro_agents_dir_path", lambda: tmp_path / "agents")
        monkeypatch.setattr(comp, "config_dir", lambda: tmp_path / "crew")
        seen = []

        def fake_create(*, agent, work_dir, model=None, **kw):
            seen.append(work_dir)
            return object()

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.create_agent_runtime", fake_create)
        pool = comp.build_reviewer_runtime("m", work_dir=str(tmp_path / "project"))
        pool._runtime_factory()
        assert seen == [str(tmp_path / "crew" / "advisor")]
        assert not str(seen[0]).startswith(str(tmp_path / "agents"))
        comp.ensure_advisor_agent_installed(tmp_path / "agents")  # the off-loop pre-spawn step
        assert (tmp_path / "crew" / "advisor").is_dir(), "created so the spawn cwd exists"

    def test_reviewer_cwd_is_write_protected_for_agents(self):
        """kiro-cli resolves ``--agent`` against ``<cwd>/.kiro/agents`` first,
        so the reviewer's process cwd must be a place no agent's file-edit
        tool can write a same-named spec into: the whole directory is on the
        platform's write-protected list (readable, never agent-writable)."""
        from kiro_crew.security.paths import is_sensitive_write_path

        for home in (".kiro/crew", ".kirocrew"):
            local_spec = f"~/{home}/advisor/.kiro/agents/kirocrew-advisor.json"
            assert is_sensitive_write_path(local_spec), local_spec
            assert is_sensitive_write_path(f"~/{home}/advisor/anything.txt")

    def test_reviewer_cwd_is_a_read_only_sandbox_mount(self):
        """The tool-gate list only fences the agent's file-edit tool; a shell
        inside the sandbox could still write ``<cwd>/.kiro/agents`` and race the
        pre-spawn check. The directory is therefore also an OS-level read-only
        ceiling: mounted read-only in every sandbox, pre-created so the mount
        exists, and its name never resolved through a symlink."""
        from kiro_crew import sandbox

        assert "advisor" in sandbox._CREW_READONLY_LEAVES
        assert "advisor" in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
        assert "advisor" in sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES

    def test_install_fails_closed_when_a_local_spec_could_shadow(self, tmp_path, monkeypatch):
        """Defense in depth behind the write gate: if anything sits under the
        reviewer cwd's ``.kiro`` tree (a planted agents/ or steering/), the
        install refuses rather than spawning a reviewer whose ``--agent``
        could resolve to the planted spec."""
        import kiro_crew.advisor.composition as comp

        monkeypatch.setattr(comp, "config_dir", lambda: tmp_path / "crew")
        planted = tmp_path / "crew" / "advisor" / ".kiro" / "agents"
        planted.mkdir(parents=True)
        (planted / "kirocrew-advisor.json").write_text(
            '{"name": "kirocrew-advisor", "allowedTools": ["*"]}'
        )
        with pytest.raises(comp.AdvisorSpecError, match="shadow"):
            comp.ensure_advisor_agent_installed(tmp_path / "agents")


class TestRevocationRecheckedBetweenNotes:
    """Round-66 (GPT, fenced): authorization was checked once before dispatch.
    A blocker steer awaits the running turn, so an opt-out (or reset,
    compaction, slot rebind) that lands DURING the first note's delivery left
    the remaining notes persisting and steering into a session that had
    already revoked the advisor. The live predicate is rechecked per note."""

    @pytest.mark.asyncio
    async def test_notes_after_revocation_are_not_delivered(self, state, dispatcher):
        slot = _running_slot(state)
        authorized = {"ok": True}

        async def steer_then_revoke(message):
            authorized["ok"] = False  # the user opts out while the steer lands
            return True

        slot._acp_client.steer = steer_then_revoke
        outcome = await dispatcher.dispatch(
            state,
            slot,
            envelope(
                [
                    {"severity": "blocker", "text": "first: wrong table dropped"},
                    {"severity": "blocker", "text": "second: still writes to prod"},
                    {"severity": "nit", "text": "third: naming"},
                ]
            ),
            advisor_update_id="u1",
            authorized=lambda: authorized["ok"],
        )
        assert outcome == {"steered": 1, "revoked": 2}
        advisor_rows = [m for m in slot.messages if m.get("role") == "advisor"]
        assert len(advisor_rows) == 1
        assert "first" in advisor_rows[0]["content"]
        assert not getattr(slot, "_advisor_pending_context", [])

    @pytest.mark.asyncio
    async def test_revoked_before_first_note_delivers_nothing(self, state, dispatcher):
        slot = _running_slot(state)
        outcome = await dispatcher.dispatch(
            state,
            slot,
            envelope([{"severity": "concern", "text": "late advice"}]),
            advisor_update_id="u1",
            authorized=lambda: False,
        )
        assert outcome == {"revoked": 1}
        assert not [m for m in slot.messages if m.get("role") == "advisor"]

    @pytest.mark.asyncio
    async def test_revocation_during_a_refused_steer_preserves_nothing(self, state, dispatcher):
        """The steer await is where a rebind lands: when the running turn does
        NOT take the blocker and the predicate has flipped meanwhile, the
        preserve fallback must not write A's card and staged context into the
        conversation the slot now fronts."""
        slot = _running_slot(state)
        authorized = {"ok": True}

        async def refuse_and_revoke(message):
            authorized["ok"] = False  # cron/workflow rebind while the steer awaits
            return False

        slot._acp_client.steer = refuse_and_revoke
        outcome = await dispatcher.dispatch(
            state,
            slot,
            envelope([{"severity": "blocker", "text": "drops the prod table"}]),
            advisor_update_id="u1",
            authorized=lambda: authorized["ok"],
        )
        assert outcome == {"revoked": 1}
        assert not [m for m in slot.messages if m.get("role") == "advisor"]
        assert not getattr(slot, "_advisor_pending_context", [])


class TestReviewerMaskUnderKiroDelegation:
    """On macOS with kiro-cli's own internal sandbox enabled, ``wrap_argv``
    delegates a kiro-cli child's isolation to that sandbox and never applies
    Crew's strict tier (the two cannot nest); on Windows the positively
    classified Kiro backend delegates by default. The reviewer's only read
    boundary is the strict tier's credential hide, so the predicate the
    advisor fails closed on must answer False wherever that hide would be
    delegated away -- while an enforced adapter's ``extra_hidden_dirs`` mask,
    which macOS keeps the seatbelt for, is unaffected."""

    @pytest.fixture
    def macos_kiro_delegation(self, monkeypatch):
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox.sys, "platform", "darwin")
        monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
        monkeypatch.setattr(sandbox, "detect_backend", lambda **_: "sandbox-exec")
        monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: True)
        return sandbox

    def test_probe_and_wrap_argv_share_the_delegation_predicate(
        self, macos_kiro_delegation, monkeypatch
    ):
        """One predicate decides whether a kiro-cli child is handed to Kiro's own
        sandbox; the probe must read it rather than restate it, so a change to
        the rule moves both."""
        sandbox = macos_kiro_delegation
        assert sandbox.credential_mask_applies("strict", is_kiro_cli=True) is False
        monkeypatch.setattr(sandbox, "_delegates_to_kiro_sandbox", lambda *a, **k: False)
        assert sandbox.credential_mask_applies("strict", is_kiro_cli=True) is True

    def test_delegated_kiro_child_has_no_strict_mask(self, macos_kiro_delegation):
        assert macos_kiro_delegation.credential_mask_applies("strict", is_kiro_cli=True) is False

    def test_enforced_adapter_mask_still_applies(self, macos_kiro_delegation):
        assert macos_kiro_delegation.credential_mask_applies("strict") is True

    def test_kiro_child_masked_when_its_internal_sandbox_is_off(self, macos_kiro_delegation):
        macos_kiro_delegation.kiro_internal_sandbox_enabled = lambda: False
        assert macos_kiro_delegation.credential_mask_applies("strict", is_kiro_cli=True) is True

    def test_installer_asks_the_kiro_form(self, monkeypatch):
        """The advisor's fail-closed guard must ask about a kiro-cli child,
        not the enforced-adapter form that macOS answers True for."""
        from kiro_crew.advisor import composition

        seen: list = []

        def probe(mode, **kw):
            seen.append((mode, kw))
            return False

        monkeypatch.setattr(composition, "credential_mask_applies", probe)
        with pytest.raises(composition.AdvisorSpecError):
            composition.ensure_advisor_agent_installed()
        assert seen == [("strict", {"is_kiro_cli": True})]
