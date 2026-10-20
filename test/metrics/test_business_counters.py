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
from test_gate_tiers import ALL_SCENARIOS, gate_world, judge_scenario  # noqa: F401

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


def _admission_modules():
    """Every module of the admission package, relative to the package root."""
    pkg = Path(ev.__file__).resolve().parent.parent / "subagent_manager" / "admission"
    return sorted(f"subagent_manager/admission/{p.name}" for p in pkg.glob("*.py"))


def _artifact_store_modules():
    """The artifact store's modules: the facade and every ``artifacts_*`` owner."""
    pkg = Path(ev.__file__).resolve().parent.parent
    return sorted(p.name for p in pkg.glob("artifacts*.py"))


def _cron_service_modules():
    """Every module ``CronService`` binds its members from, relative to the package root."""
    pkg = Path(ev.__file__).resolve().parent.parent / "cron_service" / "service"
    return sorted(f"cron_service/service/{p.name}" for p in pkg.glob("*.py"))


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

    @pytest.mark.usefixtures("gate_world")
    @pytest.mark.parametrize("scenario", ALL_SCENARIOS)
    def test_every_judgement_is_counted_exactly_once(self, rec, scenario):
        """One consultation, one counted decision, whichever tier decided and by which
        of its branches (the scenarios reach every reachable verdict of every row):
        a verdict built directly would count zero and one built twice would count
        two."""
        verdict = judge_scenario(scenario)
        calls = _named(rec, ev.APPROVAL_DECISIONS)
        assert len(calls) == 1, f"tier {scenario.tier!r} counted {len(calls)} decisions"
        assert calls[0]["attrs"] == {
            "decision": verdict.action,
            "security_deny": verdict.security_deny if verdict.action == "deny" else False,
        }

    def test_a_refusal_the_gate_makes_of_itself_is_counted_once(self, rec, monkeypatch):
        """The two verdicts no tier returns -- a crash, and an unsound table -- are
        counted decisions too."""
        from kiro_crew import hooks
        from kiro_crew.hooks import HookManager, ToolCall

        call = ToolCall(title="Running: ls -la", is_shell=True, command="ls -la")

        def boom(*_args, **_kwargs):
            raise SystemError("boom")

        with monkeypatch.context() as patched:
            patched.setattr(hooks.current_context().security, "is_denied", boom)
            HookManager().judge(call)
        with monkeypatch.context() as patched:
            patched.setattr(hooks, "_GATE_TABLE_PROBLEMS", ("unsound",))
            HookManager().judge(call)
        assert [c["attrs"] for c in _named(rec, ev.APPROVAL_DECISIONS)] == [
            {"decision": "deny", "security_deny": True}
        ] * 2

    def test_a_consultation_inside_uncounted_gate_counts_nothing(self, rec):
        from kiro_crew.hooks import HookManager, ToolCall, uncounted_gate

        with uncounted_gate():
            HookManager().judge(ToolCall(title="Running: ls -la", is_shell=True, command="ls -la"))
        assert _named(rec, ev.APPROVAL_DECISIONS) == []


class TestSpawnCounterPlacement:
    """Found in review: the admission-time emit counted rejected spawns."""

    def test_the_counter_sits_at_the_confirmed_start_funnel(self):
        """Every module of the admission package, not just the one that emits.

        Admission is a package, so the funnel is only guaranteed if no sibling
        module emits the counter from its own -- necessarily earlier -- frame.
        """
        from kiro_crew.subagent_manager import admission

        holders = []
        for module in sorted(Path(admission.__file__).resolve().parent.glob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for sub in ast.walk(node):
                        if not isinstance(sub, ast.Call):
                            continue
                        fn = sub.func
                        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                        if name == "emit_counter":
                            holders.append(node.name)
        assert holders == ["_log_spawned_impl"], (
            "the spawn counter must live at _log_spawned_impl, which every path "
            "reaches only after approval, and no other admission module may emit "
            f"it; found in {holders}"
        )

    def test_it_sits_beside_the_repo_s_own_spawn_stat(self):
        import inspect

        from kiro_crew.subagent_manager.admission.pump import _PumpMixin

        body = inspect.getsource(_PumpMixin._log_spawned_impl)
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
        # ``uncompactable_recycles`` is the set that tells a "nothing could compact
        # this" recycle apart from a "the compaction failed" one, which the callback
        # reports as different outcomes. This case is the FAILED one, so the set is
        # empty -- present rather than absent, because the real ``CompactionState``
        # always has it and a double without it would pass by accident.
        stub.state = SimpleNamespace(on_compacted=None, uncompactable_recycles=set())
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
        """The counter's success is not the callback's success.

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
        "SUBAGENTS_SPAWNED": "subagent_manager/admission/pump.py",
        "CRON_FIRES": "cron_service/service/run_execution.py",
        "ARTIFACTS_CREATED": "artifacts_writes.py",
        "WORKFLOW_RUNS": "workflows/runner.py",
        "CONTEXT_COMPACTIONS": "session_compaction.py",
        "MCP_RECONNECTS": "mcp_gateway/stub_link.py",
        "APPROVAL_DECISIONS": "hook_runtime/hook_types.py",
        "ADAPTIVE_DECISIONS": "adaptive/controller.py",
        # Overload-resilience series (RFC §10). Sampled ones are emitted by the
        # structured health sampler; the recovery ones by the ladder; the
        # completion counter at the one funnel every terminal task write crosses.
        "TASKQ_DEPTH": "dashboard/session_health.py",
        "TASKQ_OLDEST_WAIT_SECS": "dashboard/session_health.py",
        "TASKQ_EFFECTIVE_CAP": "dashboard/session_health.py",
        "TASKQ_PRESSURE_REASON": "dashboard/session_health.py",
        "TASKQ_COMPLETIONS": "taskq/store.py",
        "RECOVERY_ATTEMPTS": "recovery/ladder.py",
        "RECOVERY_ESCALATIONS": "recovery/ladder.py",
        "RECOVERY_DURATION_SECS": "recovery/ladder.py",
        "RESTARTS_TOTAL": "recovery/ladder.py",
    }

    @pytest.mark.parametrize("const,rel", sorted(OWNERS.items()))
    def test_the_owning_module_emits_the_counter(self, const, rel):
        src = (Path(ev.__file__).resolve().parent.parent / rel).read_text(encoding="utf-8")
        assert const in src, f"{rel} must emit {const}"
        assert "emit_counter" in src or "emit_histogram" in src

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
            # Every module of the admission package: before the split one file
            # carried the whole area, so naming only the emitting module would
            # leave a sibling's future emit unscanned.
            *_admission_modules(),
            "cron.py",
            *_cron_service_modules(),
            *_artifact_store_modules(),
            "workflows/runner.py",
            "session_compaction.py",
            "mcp_gateway/stub.py",
            "mcp_gateway/stub_bridge.py",
            "mcp_gateway/stub_fallback.py",
            "mcp_gateway/stub_link.py",
            "mcp_gateway/stub_register.py",
            "mcp_gateway/manager.py",
            "hook_runtime/hook_types.py",
            "adaptive/controller.py",
            "dashboard/session_health.py",
            "taskq/store.py",
            "taskq/store_vocab.py",
            "taskq/store_events.py",
            "taskq/store_waits.py",
            "taskq/store_reads.py",
            "recovery/ladder.py",
            "webhooks.py",
            "subagent_manager/terminal.py",
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
            if name not in ("emit_counter", "emit_histogram"):
                continue
            # emit_counter(name, attrs) / emit_histogram(name, value, attrs)
            attrs_index = 1 if name == "emit_counter" else 2
            attrs = node.args[attrs_index] if len(node.args) > attrs_index else None
            assert isinstance(attrs, ast.Dict), f"{rel}: attrs must be a literal dict"
            for value in attrs.values:
                assert not isinstance(
                    value, (ast.JoinedStr, ast.BinOp)
                ), f"{rel}: an interpolated attribute value is unbounded"


class TestOverloadSeriesNames:
    """The RFC §10 names exist, validate, and carry only closed-set attributes."""

    NAMES = [
        "TASKQ_DEPTH",
        "TASKQ_OLDEST_WAIT_SECS",
        "TASKQ_COMPLETIONS",
        "TASKQ_EFFECTIVE_CAP",
        "TASKQ_PRESSURE_REASON",
        "HOST_PROCS_PEAK",
        "HOST_FDS_PEAK",
        "HOST_RSS_PEAK_MB",
        "LOOP_LAG_MS",
        "RECOVERY_DURATION_SECS",
        "RECOVERY_ATTEMPTS",
        "RECOVERY_ESCALATIONS",
        "RESTARTS_TOTAL",
    ]

    @pytest.mark.parametrize("const", NAMES)
    def test_name_is_declared_and_valid(self, const):
        from kiro_crew.metrics.schema import validate_name

        value = getattr(ev, const)
        assert value.startswith("kirocrew.")
        assert validate_name(value) == value

    def test_emit_histogram_never_raises(self, rec):
        ev.emit_histogram(ev.RECOVERY_DURATION_SECS, 1.5, {"layer": "L1_tool_call"}, unit="s")
        with patch("kiro_crew.metrics.provider.get_recorder", side_effect=RuntimeError("x")):
            ev.emit_histogram(ev.TASKQ_DEPTH, 1.0, {"state": "queued"})


class TestWebhookRequests:
    """``kirocrew.webhook.requests``, driven through ``WebhookRunStore.record``.

    The run store is the one commit point every inbound request reaches, so these
    drive the real method against a temp base dir rather than calling
    ``emit_counter`` directly -- a counter wired anywhere else would pass a direct
    call and still miss the rejection outcomes.
    """

    @pytest.fixture
    def store(self, tmp_path):
        from kiro_crew.webhooks import WebhookRunStore

        return WebhookRunStore(base_dir=tmp_path)

    @pytest.mark.parametrize(
        "outcome",
        [
            "completed",
            "timeout",
            "error",
            "rejected_capacity",
            "unauthorized",
            "disabled",
        ],
    )
    def test_each_recorded_outcome_is_exactly_one_increment(self, rec, store, outcome):
        store.record(outcome=outcome)
        calls = _named(rec, ev.WEBHOOK_REQUESTS)
        assert len(calls) == 1
        assert calls[0]["attrs"] == {"outcome": outcome}
        assert calls[0]["value"] == 1

    def test_the_refusal_outcomes_are_not_folded_together(self, rec, store):
        """``unauthorized`` / ``disabled`` / ``rejected_capacity`` / ``timeout``
        are four different operator problems; a shared bucket answers none."""
        for outcome in ("unauthorized", "disabled", "rejected_capacity", "timeout"):
            store.record(outcome=outcome)
        seen = [c["attrs"]["outcome"] for c in _named(rec, ev.WEBHOOK_REQUESTS)]
        assert seen == ["unauthorized", "disabled", "rejected_capacity", "timeout"]

    def test_an_unknown_outcome_maps_to_other(self):
        from kiro_crew.webhooks import METRIC_OUTCOME_OTHER, metric_outcome

        assert metric_outcome("teleported") == METRIC_OUTCOME_OTHER
        assert metric_outcome("") == METRIC_OUTCOME_OTHER
        assert metric_outcome(None) == METRIC_OUTCOME_OTHER
        assert metric_outcome(7) == METRIC_OUTCOME_OTHER

    def test_an_unknown_outcome_is_refused_before_it_is_counted(self, rec, store):
        """``record`` rejects a value outside its own enum, so the fold is the
        fallback for a stored row -- not a way to admit a new outcome."""
        from kiro_crew.webhooks import WebhookError

        with pytest.raises(WebhookError):
            store.record(outcome="teleported")
        assert _named(rec, ev.WEBHOOK_REQUESTS) == []

    def test_every_recorded_outcome_is_a_member_of_the_metric_enum(self):
        """The recorded half of the series key is exactly the store's own enum.

        The other half -- the endpoint refusals the run store never sees -- and
        the whole-enum shape are pinned in
        ``test/test_webhooks_request_counter.py``, which owns that population.
        """
        from kiro_crew.webhooks import METRIC_OUTCOME_OTHER, METRIC_OUTCOMES, VALID_OUTCOMES

        assert VALID_OUTCOMES <= METRIC_OUTCOMES
        assert METRIC_OUTCOME_OTHER not in VALID_OUTCOMES

    def test_no_identifier_or_free_text_reaches_an_attribute(self, rec, store):
        """``detail``, ``name``, the hook id and the session key are all unbounded."""
        store.record(
            outcome="error",
            hook_id="hook_secret",
            session_key="hook:someone/private",
            name="Nightly deploy webhook",
            token_id="tok_abc",
            detail="Traceback: /home/someone/.aws/credentials",
        )
        assert _named(rec, ev.WEBHOOK_REQUESTS)[-1]["attrs"] == {"outcome": "error"}

    def test_a_history_write_failure_still_counts_the_request(self, rec, store):
        """The series counts REQUESTS, not history rows: the request happened
        whether or not the bounded ring could record it."""
        with patch("kiro_crew.webhooks.write_json_atomic", side_effect=OSError("full")):
            store.record(outcome="completed")
        assert len(_named(rec, ev.WEBHOOK_REQUESTS)) == 1

    def test_nothing_is_recorded_with_telemetry_off(self, store):
        """A disabled recorder is the gate: no instrument, no point."""
        from kiro_crew.metrics.recorder import MetricsRecorder

        off = MetricsRecorder(None)
        assert off.enabled is False
        with patch("kiro_crew.metrics.provider.get_recorder", return_value=off):
            # Would raise if a disabled recorder tried to reach an instrument.
            store.record(outcome="completed")


class TestSubagentCompleted:
    """``kirocrew.subagent.completed`` -- one increment per run, never two."""

    def test_each_terminal_outcome_keeps_its_own_label(self):
        assert ev.subagent_outcome("completed") == "completed"
        assert ev.subagent_outcome("failed") == "failed"
        assert ev.subagent_outcome("stopped") == "stopped"

    def test_an_unreadable_outcome_maps_to_unknown(self):
        """A stop is not a failure, so a record whose outcome cannot be read is
        named unknown rather than guessed as either."""
        assert ev.subagent_outcome(None) == ev.SUBAGENT_OUTCOME_UNKNOWN
        assert ev.subagent_outcome("") == ev.SUBAGENT_OUTCOME_UNKNOWN
        assert ev.subagent_outcome("reaped") == ev.SUBAGENT_OUTCOME_UNKNOWN
        assert ev.subagent_outcome(object()) == ev.SUBAGENT_OUTCOME_UNKNOWN
        assert ev.subagent_outcome("unknown") == ev.SUBAGENT_OUTCOME_UNKNOWN

    def test_the_enum_covers_every_outcome_the_runtime_can_report(self):
        """``SubagentInfo.outcome`` is THE classifier, and this enum restates it
        because ``metrics`` sits below the sub-agent manager in the dependency
        order. A fourth value appearing in that property without a row in the
        metrics doc would silently land on ``unknown``, so harvest the property's
        own return literals and fail here instead.
        """
        from kiro_crew.subagent_parts import run_record

        tree = ast.parse(Path(run_record.__file__).read_text(encoding="utf-8"))
        prop = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "outcome"
            and any(isinstance(d, ast.Name) and d.id == "property" for d in node.decorator_list)
        )
        returned = {
            node.value.value
            for node in ast.walk(prop)
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        assert returned == {"completed", "failed", "stopped"}
        assert returned <= ev.SUBAGENT_OUTCOMES
        assert ev.SUBAGENT_OUTCOMES == returned | {ev.SUBAGENT_OUTCOME_UNKNOWN}

    def test_one_terminal_is_exactly_one_increment(self, rec):
        """The emit sits past the finalize claim, so the reaper and the run
        loop's ``finally`` cannot both count the same run."""
        from kiro_crew.subagent_manager.terminal import TerminalCoordinator

        class _Info:
            outcome = "completed"

        coordinator = TerminalCoordinator.__new__(TerminalCoordinator)
        coordinator._record_terminal_metric(_Info())
        calls = _named(rec, ev.SUBAGENT_COMPLETED)
        assert len(calls) == 1
        assert calls[0]["attrs"] == {"outcome": "completed"}

    @pytest.mark.parametrize("outcome", ["completed", "failed", "stopped"])
    def test_a_stopped_run_is_not_reported_as_a_completion(self, rec, outcome):
        from kiro_crew.subagent_manager.terminal import TerminalCoordinator

        class _Info:
            pass

        info = _Info()
        info.outcome = outcome
        coordinator = TerminalCoordinator.__new__(TerminalCoordinator)
        coordinator._record_terminal_metric(info)
        assert _named(rec, ev.SUBAGENT_COMPLETED)[-1]["attrs"]["outcome"] == outcome

    def test_a_record_whose_outcome_raises_is_counted_as_unknown(self, rec):
        """``outcome`` is a property, so reading it is the one line that can
        raise -- and a run that reached a terminal must still be counted."""
        from kiro_crew.subagent_manager.terminal import TerminalCoordinator

        class _Info:
            @property
            def outcome(self):
                raise RuntimeError("record is half-written")

        coordinator = TerminalCoordinator.__new__(TerminalCoordinator)
        coordinator._record_terminal_metric(_Info())
        calls = _named(rec, ev.SUBAGENT_COMPLETED)
        assert len(calls) == 1
        assert calls[0]["attrs"] == {"outcome": ev.SUBAGENT_OUTCOME_UNKNOWN}

    def test_the_emit_is_not_gated_on_the_crew_log_emitter(self):
        """It is called BESIDE ``_record_crew_log_terminal``, not inside it: that
        method returns early with the crew log off, which would narrow this
        counter's population to installs that happen to keep one."""
        from kiro_crew.subagent_manager import terminal as term

        source = Path(term.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        bodies = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef)
        }
        emits = [
            node
            for node in ast.walk(bodies["_record_crew_log_terminal"])
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "emit_counter"
        ]
        assert emits == [], "the terminal counter must not sit behind the crew log flag"
        calls = [
            node
            for node in ast.walk(bodies["_report_terminal_impl"])
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "_record_terminal_metric"
        ]
        assert len(calls) == 1, "exactly one call, from the exclusive terminal report"

    def test_nothing_is_recorded_with_telemetry_off(self):
        from kiro_crew.metrics.recorder import MetricsRecorder
        from kiro_crew.subagent_manager.terminal import TerminalCoordinator

        class _Info:
            outcome = "failed"

        off = MetricsRecorder(None)
        assert off.enabled is False
        coordinator = TerminalCoordinator.__new__(TerminalCoordinator)
        with patch("kiro_crew.metrics.provider.get_recorder", return_value=off):
            coordinator._record_terminal_metric(_Info())
