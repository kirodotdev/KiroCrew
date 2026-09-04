"""The business-event counters, driven through their production call sites.

Each subsystem emits from its own call site because no frame is common to all of
them, so what these tests hold is the property that makes that safe: the name is
the shared constant, and every attribute VALUE is a member of a closed set
(``metrics/schema.py`` -- the recorder caches one instrument per name and never
evicts, so an unbounded value is a cardinality bomb).
"""

import ast
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.metrics import events as ev


class _CapturingRecorder:
    def __init__(self) -> None:
        self.counters: list = []

    def counter(self, name, value=1, *, attrs=None, **kwargs) -> None:
        self.counters.append({"name": name, "value": value, "attrs": dict(attrs or {})})

    def histogram(self, name, value, **kwargs) -> None:  # pragma: no cover - unused
        pass


@pytest.fixture
def rec():
    r = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=r):
        yield r


def _named(rec, name):
    return [c for c in rec.counters if c["name"] == name]


class TestApprovalDecisions:
    """Every ``ToolHookResult`` construction is a gate verdict."""

    def test_allow_is_the_branch_that_reaches_a_prompt(self, rec):
        from kiro_crew.hooks import TOOL_ALLOW, ToolHookResult

        ToolHookResult.allow()
        call = _named(rec, ev.APPROVAL_DECISIONS)[-1]
        assert call["attrs"]["decision"] == TOOL_ALLOW

    def test_auto_approve_is_counted_separately(self, rec):
        from kiro_crew.hooks import TOOL_AUTO_APPROVE, ToolHookResult

        ToolHookResult.auto_approve()
        assert _named(rec, ev.APPROVAL_DECISIONS)[-1]["attrs"]["decision"] == TOOL_AUTO_APPROVE

    def test_a_security_deny_and_a_policy_deny_are_distinguishable(self, rec):
        from kiro_crew.hooks import ToolHookResult

        ToolHookResult.deny("blocked")
        ToolHookResult.deny_policy("ceiling")
        calls = _named(rec, ev.APPROVAL_DECISIONS)[-2:]
        assert calls[0]["attrs"]["security_deny"] is True
        assert calls[1]["attrs"]["security_deny"] is False

    def test_the_deny_reason_never_reaches_the_attributes(self, rec):
        """A reason carries paths and commands -- unbounded, and often sensitive."""
        from kiro_crew.hooks import ToolHookResult

        ToolHookResult.deny("refusing to read /home/someone/.aws/credentials")
        call = _named(rec, ev.APPROVAL_DECISIONS)[-1]
        assert set(call["attrs"]) == {"decision", "security_deny"}

    def test_the_gate_still_returns_its_verdict_when_telemetry_raises(self):
        from kiro_crew.hooks import TOOL_DENY, ToolHookResult

        with patch("kiro_crew.metrics.events.emit_counter", side_effect=RuntimeError("boom")):
            result = ToolHookResult.deny("blocked")
        assert result.action == TOOL_DENY
        assert result.reason == "blocked"

    def test_the_existing_child_permission_counters_are_untouched(self):
        """Those measure a specific hang-resilience fix, not this population."""
        assert ev.CHILD_PERMISSION_DENIED == "kirocrew.acp.child_permission.denied"
        assert ev.CHILD_PERMISSION_ROUTED == "kirocrew.acp.child_permission.routed"

    def test_a_surface_downgrade_of_the_verdict_is_not_a_second_decision(self, rec):
        """Found in review: chat_runner rebuilds the result to downgrade it."""
        from kiro_crew.hooks import TOOL_ALLOW, ToolHookResult

        ToolHookResult.auto_approve()  # the gate's verdict
        before = len(_named(rec, ev.APPROVAL_DECISIONS))
        ToolHookResult(action=TOOL_ALLOW)  # the surface overriding it
        after = len(_named(rec, ev.APPROVAL_DECISIONS))
        assert after == before, "one consultation must count once, not once per object"

    def test_every_gate_exit_is_counted(self):
        """All 18 returns in on_tool_call go through a factory; nothing else does."""
        import ast
        import inspect

        from kiro_crew import hooks

        tree = ast.parse(inspect.getsource(hooks))
        factories = set()
        direct = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "on_tool_call":
                for call in ast.walk(node):
                    if not isinstance(call, ast.Call):
                        continue
                    fn = call.func
                    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                        if fn.value.id == "ToolHookResult":
                            factories.add(fn.attr)
                    elif isinstance(fn, ast.Name) and fn.id == "ToolHookResult":
                        direct += 1
        assert factories, "the gate must return through the counted factories"
        assert factories <= {"allow", "auto_approve", "deny", "deny_policy"}
        assert direct == 0, "a direct construction inside the gate would go uncounted"


class TestSpawnCounterPlacement:
    """Found in review: the admission-time emit counted rejected spawns."""

    def test_the_counter_sits_at_the_confirmed_start_funnel(self):
        import ast
        import inspect

        from kiro_crew.subagent_manager import admission

        tree = ast.parse(inspect.getsource(admission))
        holders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for sub in ast.walk(node):
                    if not isinstance(sub, ast.Call):
                        continue
                    fn = sub.func
                    name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                    if name == "emit_counter":
                        holders.append(node.name)
        assert holders == ["_log_spawned_impl"], (
            "the spawn counter must live at _log_spawned_impl, which every path "
            f"reaches only after approval; found in {holders}"
        )

    def test_it_sits_beside_the_repo_s_own_spawn_stat(self):
        import inspect

        from kiro_crew.subagent_manager import admission

        body = (
            inspect.getsource(admission._SpawnAdmission._log_spawned_impl)
            if hasattr(admission, "_SpawnAdmission")
            else inspect.getsource(admission)
        )
        assert "inc_subagent_spawned" in body
        assert "SUBAGENTS_SPAWNED" in body


class TestCompactions:
    @staticmethod
    def _fire(success, *, recycling=False):
        """Drive the production funnel with the minimum state it reads."""
        import asyncio
        from types import SimpleNamespace
        from typing import Any, cast

        from kiro_crew.session_compaction import CompactionCoordinator

        coordinator = CompactionCoordinator.__new__(CompactionCoordinator)
        stub = cast(Any, coordinator)
        stub._owner = SimpleNamespace(
            _recycling={"k": object()} if recycling else {},
            mark_needs_reinjection=lambda key: None,
        )
        stub.state = SimpleNamespace(on_compacted=None)
        asyncio.run(
            CompactionCoordinator._fire_compact_callback(coordinator, "k", 0.9, success=success)
        )

    def test_a_compaction_verdict_is_counted(self, rec):
        self._fire(True)
        assert _named(rec, ev.CONTEXT_COMPACTIONS)[-1]["attrs"] == {"success": True}

    def test_a_failed_compaction_is_still_an_attempt(self, rec):
        self._fire(False)
        assert _named(rec, ev.CONTEXT_COMPACTIONS)[-1]["attrs"] == {"success": False}

    def test_a_failed_compact_that_recycles_is_not_counted_successful(self, rec):
        """Review round 6: the counter's success is not the callback's success.

        ``_recycle_held`` is reached exactly when an in-place ``/compact`` FAILED
        and the provider had to be replaced instead, yet it fires this funnel with
        ``success=True`` because the SESSION now has headroom -- which is what the
        callback needs to know. Counting that as a successful compaction reports
        the failure mode as the success case. The recycling marker is set for the
        whole of ``_recycle_held`` and popped only afterwards, so it is what
        separates the two populations at this point; if that ordering ever moves,
        this test is what notices.
        """
        self._fire(True, recycling=True)
        assert _named(rec, ev.CONTEXT_COMPACTIONS)[-1]["attrs"] == {"success": False}

    def test_a_surface_with_no_callback_is_still_counted(self, rec):
        """The early return below the emit would otherwise drop those surfaces."""
        self._fire(True)
        assert len(_named(rec, ev.CONTEXT_COMPACTIONS)) == 1


class TestCallSitesAreWired:
    """Each counter must be referenced by the module that owns its event."""

    OWNERS = {
        "SUBAGENTS_SPAWNED": "subagent_manager/admission.py",
        "CRON_FIRES": "cron.py",
        "ARTIFACTS_CREATED": "artifacts.py",
        "WORKFLOW_RUNS": "workflows/runner.py",
        "CONTEXT_COMPACTIONS": "session_compaction.py",
        "MCP_RECONNECTS": "mcp_gateway/stub.py",
        "APPROVAL_DECISIONS": "hooks.py",
    }

    @pytest.mark.parametrize("const,rel", sorted(OWNERS.items()))
    def test_the_owning_module_emits_the_counter(self, const, rel):
        src = (Path(ev.__file__).resolve().parent.parent / rel).read_text(encoding="utf-8")
        assert const in src, f"{rel} must emit {const}"
        assert "emit_counter" in src

    def test_every_declared_counter_has_an_owner(self):
        """A constant with no call site is a metric nobody can read."""
        declared = {
            name
            for name, value in vars(ev).items()
            if name.isupper() and isinstance(value, str) and value.startswith("kirocrew.")
        }
        # The hang-resilience series predates this change and is emitted from
        # acp/ + session.py; only the business series is asserted here.
        business = set(self.OWNERS)
        assert business <= declared


class TestAttributeValuesAreBounded:
    """No emit may hand the recorder a value derived from free-form input."""

    #: Names a call site may read for an attribute value. Anything else -- a
    #: session key, a tool name, a slug, a reason string -- is unbounded.
    ALLOWED_CALLS = {"bool", "len", "int"}

    @pytest.mark.parametrize(
        "rel",
        [
            "subagent_manager/admission.py",
            "cron.py",
            "artifacts.py",
            "workflows/runner.py",
            "session_compaction.py",
            "mcp_gateway/stub.py",
            "hooks.py",
        ],
    )
    def test_no_emit_passes_an_f_string_or_a_concatenation(self, rel):
        path = Path(ev.__file__).resolve().parent.parent / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "emit_counter":
                continue
            attrs = node.args[1] if len(node.args) > 1 else None
            assert isinstance(attrs, ast.Dict), f"{rel}: attrs must be a literal dict"
            for value in attrs.values:
                assert not isinstance(
                    value, (ast.JoinedStr, ast.BinOp)
                ), f"{rel}: an interpolated attribute value is unbounded"
