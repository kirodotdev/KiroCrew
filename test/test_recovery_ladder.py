"""The ladder: L1 retry honouring retry_after, escalation L2->L3, L5 never automatic."""

from __future__ import annotations

import random
from unittest.mock import patch

import pytest
from overload_fakes import Clock

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.metrics import events as ev
from kiro_crew.recovery import ladder as lad


@pytest.fixture(autouse=True)
def _fresh_process_ladder():
    """The process ladder is a module global: reset it around every test here."""
    lad._reset_default_ladder_for_tests()
    yield
    lad._reset_default_ladder_for_tests()


def _cfg(base: float, cap: float) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agent.recovery_backoff_base_secs = base
    cfg.agent.recovery_backoff_max_secs = cap
    return cfg


class _Rec:
    def __init__(self) -> None:
        self.counters: list = []
        self.hists: list = []

    def counter(self, name, value=1, *, attrs=None, **kw):
        self.counters.append({"name": name, "attrs": dict(attrs or {})})

    def histogram(self, name, value, *, attrs=None, **kw):
        self.hists.append({"name": name, "value": value, "attrs": dict(attrs or {})})


@pytest.fixture
def rec():
    r = _Rec()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=r):
        yield r


@pytest.fixture
def clock():
    return Clock()


def _ladder(clock, **kw) -> lad.RecoveryLadder:
    return lad.RecoveryLadder(clock=clock, rng=random.Random(42), **kw)


# ── L1 detector ──────────────────────────────────────────────────────────────


class TestClassifyInfraError:
    def test_stub_capacity_error_object(self):
        err = {
            "code": -32001,
            "message": "gateway at capacity",
            "data": {"class": "capacity", "retry_after_secs": 7},
        }
        got = lad.classify_infra_error(err)
        assert got is not None
        assert got.error_class == lad.CLASS_CAPACITY
        assert got.retry_after_secs == 7.0
        assert got.code == -32001

    def test_jsonrpc_envelope_with_error_member(self):
        got = lad.classify_infra_error(
            {"jsonrpc": "2.0", "id": 4, "error": {"code": -32001, "data": {"class": "capacity"}}}
        )
        assert got is not None and got.error_class == lad.CLASS_CAPACITY
        assert got.retry_after_secs is None

    def test_serialised_error_text(self):
        text = '{"code": -32001, "message": "capacity", "data": {"class": "capacity", "retry_after_secs": 12}}'
        got = lad.classify_infra_error(text)
        assert got is not None and got.retry_after_secs == 12.0

    def test_kiro_cli_prose_carrying_the_code(self):
        got = lad.classify_infra_error(
            "MCP error -32001: gateway at capacity (class=capacity, retry_after_secs=5)"
        )
        assert got is not None
        assert got.error_class == lad.CLASS_CAPACITY
        assert got.retry_after_secs == 5.0

    def test_gateway_recoverable_infra_markers(self):
        for text in (
            "BackendGone: pooled backend exited before the reply",
            "spawn queue timed out after 600s",
            "reconnect budget exhausted; gateway daemon unavailable",
        ):
            got = lad.classify_infra_error(text)
            assert got is not None, text
            assert got.error_class == lad.CLASS_RECOVERABLE_INFRA

    def test_ordinary_failures_are_not_infra(self):
        for text in (
            "Permission denied: /home/x/.aws/credentials",
            "invalid arguments: expected string",
            "I cannot help with that request.",
            "Traceback (most recent call last): ValueError",
            "",
        ):
            assert lad.classify_infra_error(text) is None, text
        assert lad.classify_infra_error(None) is None

    def test_a_document_quoting_a_marker_is_not_an_error(self):
        doc = "log line about a BackendGone incident\n" * 200
        assert len(doc) > lad._INFRA_TEXT_MAX_CHARS
        assert lad.classify_infra_error(doc) is None

    def test_a_number_that_merely_contains_the_code_does_not_match(self):
        assert lad.classify_infra_error("balance: -320011.5 units") is None
        assert lad.classify_infra_error("id 132001 processed") is None

    def test_exceptions_are_classified_by_their_text(self):
        assert lad.classify_infra_error(RuntimeError("SpawnGateTimeout after 600s")) is not None


# ── L1 retry ────────────────────────────────────────────────────────────────


class TestL1:
    def test_retry_honours_retry_after(self, clock, rec):
        ladder = _ladder(clock)
        d = ladder.observe_failure(lad.L1_TOOL_CALL, "chat-1", retry_after_secs=30)
        assert d.action == lad.ACTION_RETRY
        assert d.retry is True
        assert d.attempt == 1
        assert d.delay_secs == 30.0  # floor: the hint is larger than the jittered 2s

    def test_retry_delay_is_jittered_and_capped_without_a_hint(self, clock, rec):
        ladder = _ladder(clock)
        d1 = ladder.observe_failure(lad.L1_TOOL_CALL, "chat-1")
        d2 = ladder.observe_failure(lad.L1_TOOL_CALL, "chat-1")
        assert 1.0 <= d1.delay_secs <= 2.0
        assert 2.0 <= d2.delay_secs <= 4.0
        assert d2.attempt == 2

    def test_third_failure_escalates_to_l2(self, clock, rec):
        ladder = _ladder(clock)
        ladder.observe_failure(lad.L1_TOOL_CALL, "chat-1")
        ladder.observe_failure(lad.L1_TOOL_CALL, "chat-1")
        d = ladder.observe_failure(lad.L1_TOOL_CALL, "chat-1")
        assert d.action == lad.ACTION_ESCALATE
        assert d.next_layer == lad.L2_BACKEND
        assert d.retry is False
        esc = [c for c in rec.counters if c["name"] == ev.RECOVERY_ESCALATIONS]
        assert esc[-1]["attrs"] == {"from_layer": lad.L1_TOOL_CALL, "to_layer": lad.L2_BACKEND}

    def test_units_do_not_share_attempts(self, clock, rec):
        ladder = _ladder(clock)
        ladder.observe_failure(lad.L1_TOOL_CALL, "a")
        ladder.observe_failure(lad.L1_TOOL_CALL, "a")
        assert ladder.observe_failure(lad.L1_TOOL_CALL, "b").retry is True

    def test_success_resets_and_measures_the_outage(self, clock, rec):
        ladder = _ladder(clock)
        ladder.observe_failure(lad.L1_TOOL_CALL, "a")
        clock.t += 45.0
        duration = ladder.observe_success(lad.L1_TOOL_CALL, "a")
        assert duration == 45.0
        assert ladder.attempts(lad.L1_TOOL_CALL, "a") == 0
        h = [x for x in rec.hists if x["name"] == ev.RECOVERY_DURATION_SECS]
        assert h[-1]["value"] == 45.0 and h[-1]["attrs"] == {"layer": lad.L1_TOOL_CALL}

    def test_success_without_an_open_run_measures_nothing(self, clock, rec):
        ladder = _ladder(clock)
        assert ladder.observe_success(lad.L1_TOOL_CALL, "never-failed") is None
        assert not rec.hists

    def test_forget_resets_without_measuring_a_recovery(self, clock, rec):
        """How a SPENT run is closed: the next cycle gets a fresh budget, and the
        outage that never closed is neither measured nor reported recovered."""
        rows: list[tuple[str, dict]] = []
        ladder = _ladder(clock, event_sink=lambda tid, data: rows.append((tid, data)))
        ladder.observe_failure(lad.L1_TOOL_CALL, "a", task_id="t-1")
        clock.t += 45.0
        ladder.forget(lad.L1_TOOL_CALL, "a")
        assert ladder.attempts(lad.L1_TOOL_CALL, "a") == 0
        assert not [x for x in rec.hists if x["name"] == ev.RECOVERY_DURATION_SECS]
        assert not [d for _, d in rows if d.get("action") == "recovered"]

    def test_cooldown_starts_the_count_over(self, clock, rec):
        ladder = _ladder(clock)
        ladder.observe_failure(lad.L1_TOOL_CALL, "a")
        ladder.observe_failure(lad.L1_TOOL_CALL, "a")
        clock.t += ladder.layer_policy(lad.L1_TOOL_CALL).cooldown_secs + 1
        assert ladder.observe_failure(lad.L1_TOOL_CALL, "a").attempt == 1

    def test_every_decision_is_counted_with_closed_attrs(self, clock, rec):
        ladder = _ladder(clock)
        ladder.observe_failure(lad.L1_TOOL_CALL, "a")
        c = [x for x in rec.counters if x["name"] == ev.RECOVERY_ATTEMPTS][-1]
        assert c["attrs"] == {"layer": lad.L1_TOOL_CALL, "action": lad.ACTION_RETRY}


# ── L2 -> L3 -> L4 -> L5 ──────────────────────────────────────────────────────


class TestEscalationChain:
    def test_l2_exhaustion_escalates_to_l3(self, clock, rec):
        ladder = _ladder(clock)
        assert ladder.observe_failure(lad.L2_BACKEND, "core").retry is True
        d = ladder.observe_failure(lad.L2_BACKEND, "core")
        assert (d.action, d.next_layer) == (lad.ACTION_ESCALATE, lad.L3_ACP_RUNTIME)

    def test_l3_exhaustion_escalates_to_l4(self, clock, rec):
        ladder = _ladder(clock)
        ladder.observe_failure(lad.L3_ACP_RUNTIME, "rt-1")
        d = ladder.observe_failure(lad.L3_ACP_RUNTIME, "rt-1")
        assert (d.action, d.next_layer) == (lad.ACTION_ESCALATE, lad.L4_GATEWAYD)

    def test_l4_second_respawn_in_window_notifies_l5_once(self, clock, rec):
        notes: list[tuple[str, str]] = []
        ladder = _ladder(clock, notifier=lambda layer, msg: notes.append((layer, msg)))
        first = ladder.observe_failure(lad.L4_GATEWAYD, "gatewayd", reason="daemon exited rc=1")
        assert first.action == lad.ACTION_RETRY  # one respawn per window is allowed
        clock.t += 60.0
        second = ladder.observe_failure(lad.L4_GATEWAYD, "gatewayd", reason="daemon exited rc=1")
        assert second.action == lad.ACTION_NOTIFY
        assert second.next_layer == lad.L5_GATEWAY
        assert notes and notes[0][0] == lad.L5_GATEWAY
        # A third failure in the same run does not spam the notifier.
        ladder.observe_failure(lad.L4_GATEWAYD, "gatewayd")
        assert len(notes) == 1
        # Recovery re-arms the notice for the next incident.
        ladder.observe_success(lad.L4_GATEWAYD, "gatewayd")
        clock.t += 1.0
        ladder.observe_failure(lad.L4_GATEWAYD, "gatewayd")
        ladder.observe_failure(lad.L4_GATEWAYD, "gatewayd")
        assert len(notes) == 2

    def test_l4_respawn_after_the_window_is_allowed_again(self, clock, rec):
        ladder = _ladder(clock)
        ladder.observe_failure(lad.L4_GATEWAYD, "gatewayd")
        clock.t += lad.GATEWAYD_RESPAWN_COOLDOWN_SECS + 1
        assert ladder.observe_failure(lad.L4_GATEWAYD, "gatewayd").retry is True

    def test_l5_is_never_automatic(self, clock, rec):
        notes: list = []
        ladder = _ladder(clock, notifier=lambda layer, msg: notes.append(layer))
        d = ladder.observe_failure(lad.L5_GATEWAY, "gateway")
        assert d.action == lad.ACTION_GIVE_UP
        assert d.retry is False
        assert d.delay_secs == 0.0
        assert notes == [lad.L5_GATEWAY]
        assert ladder.layer_policy(lad.L5_GATEWAY).automatic is False

    def test_a_raising_notifier_never_breaks_recovery(self, clock, rec):
        def boom(layer, msg):
            raise RuntimeError("no channel")

        ladder = _ladder(clock, notifier=boom)
        assert ladder.observe_failure(lad.L5_GATEWAY, "gateway").action == lad.ACTION_GIVE_UP


# ── event sink / restarts / table ────────────────────────────────────────────


class TestSinkAndMetrics:
    def test_event_sink_receives_one_recover_row_per_decision(self, clock, rec):
        rows: list = []
        ladder = _ladder(clock, event_sink=lambda tid, data: rows.append((tid, data)))
        ladder.observe_failure(lad.L1_TOOL_CALL, "a", task_id="task-1", reason="capacity")
        assert rows == [
            (
                "task-1",
                {
                    "layer": lad.L1_TOOL_CALL,
                    "attempt": 1,
                    "action": lad.ACTION_RETRY,
                    "delay_secs": rows[0][1]["delay_secs"],
                    "next_layer": None,
                    "reason": "capacity",
                },
            )
        ]

    def test_sink_is_skipped_without_a_task_id(self, clock, rec):
        rows: list = []
        ladder = _ladder(clock, event_sink=lambda tid, data: rows.append(tid))
        ladder.observe_failure(lad.L1_TOOL_CALL, "a")
        assert rows == []

    def test_record_restart_counts_per_layer(self, rec):
        lad.record_restart(lad.L4_GATEWAYD)
        c = [x for x in rec.counters if x["name"] == ev.RESTARTS_TOTAL][-1]
        assert c["attrs"] == {"layer": lad.L4_GATEWAYD}
        with pytest.raises(KeyError):
            lad.default_ladder().record_restart("L9")

    def test_table_rows_carry_every_column_the_rfc_names(self, clock):
        rows = _ladder(clock).table()
        assert [r["layer"] for r in rows] == list(lad.LAYERS)
        for r in rows:
            assert set(r) >= {
                "trigger",
                "cleanup_deadline_secs",
                "backoff_base_secs",
                "backoff_max_secs",
                "attempts_before_escalation",
                "escalates_to",
                "automatic",
            }
        by = {r["layer"]: r for r in rows}
        assert by[lad.L1_TOOL_CALL]["cleanup_deadline_secs"] is None
        assert by[lad.L2_BACKEND]["cleanup_deadline_secs"] == lad.POOL_SHUTDOWN_SECS
        assert by[lad.L4_GATEWAYD]["cleanup_deadline_secs"] == lad.TOTAL_SHUTDOWN_BUDGET_SECS
        assert by[lad.L5_GATEWAY]["automatic"] is False


# ── the layers actually read the shared schedule ─────────────────────────────


class TestLayersShareTheSchedule:
    def test_gatewayd_supervisor_reads_l4(self):
        from kiro_crew.mcp_gateway import manager

        l4 = lad.LADDER.layer(lad.L4_GATEWAYD)
        assert manager._RESPAWN_BACKOFF_START_SECS == l4.base_secs
        assert manager._RESPAWN_BACKOFF_MAX_SECS == l4.max_secs

    def test_supervisor_backoff_is_jittered_and_bounded(self, monkeypatch):
        from kiro_crew.mcp_gateway import manager

        floor, cap = manager._RESPAWN_BACKOFF_START_SECS, manager._RESPAWN_BACKOFF_MAX_SECS
        seen = set()
        cur = floor
        for _ in range(40):
            nxt = manager.GatewayManager._next_respawn_backoff(cur)
            assert floor <= nxt <= cap
            seen.add(round(nxt, 6))
            cur = nxt
        assert cur == cap or cap - cur < cap / 2  # converges toward the cap
        assert len(seen) > 5
        # A test that pins the floor to 0 gets a 0 delay, not a jittered one.
        monkeypatch.setattr(manager, "_RESPAWN_BACKOFF_START_SECS", 0.0)
        assert manager.GatewayManager._next_respawn_backoff(0.0) >= 0.0

    def test_acp_client_reads_l3(self):
        from kiro_crew.acp import client

        assert client._ACP_RESPAWN_BACKOFF_S == lad.LADDER.layer(lad.L3_ACP_RUNTIME).base_secs

    def test_task_store_reads_the_shared_schedule(self):
        from kiro_crew.taskq import model

        assert model.RECOVERY_BACKOFF_BASE_SECS == lad.LADDER.base_secs
        assert model.RECOVERY_BACKOFF_MAX_SECS == lad.LADDER.max_secs
        assert model.recovery_backoff_secs(0) == lad.LADDER.base_secs

    def test_the_import_time_readers_stay_on_the_static_defaults(self):
        """Two mirrors bind at import, so the boot snapshot cannot reach them.

        Both are single-site (one respawn delay, one re-dispatch delay) and both
        are documented as the ladder's DEFAULTS rather than as followers of the
        ``agent.recovery_backoff_*`` knobs; this pins that they read `LADDER` and
        not the configured process ladder.
        """
        from kiro_crew.acp import client
        from kiro_crew.taskq import model

        lad.configure_default_ladder(_cfg(7.0, 300.0))
        assert client._ACP_RESPAWN_BACKOFF_S == lad.LADDER.layer(lad.L3_ACP_RUNTIME).base_secs
        assert model.recovery_backoff_secs(0) == lad.LADDER.base_secs

    def test_no_layer_keeps_a_private_backoff_literal(self):
        """The literals the ladder replaced must not come back."""
        import inspect

        from kiro_crew.acp import client
        from kiro_crew.mcp_gateway import manager
        from kiro_crew.taskq import model

        assert "_RESPAWN_BACKOFF_START_SECS = 1.0" not in inspect.getsource(manager)
        assert "_RESPAWN_BACKOFF_MAX_SECS = 60.0" not in inspect.getsource(manager)
        assert "_ACP_RESPAWN_BACKOFF_S = 2.0" not in inspect.getsource(client)
        assert "RECOVERY_BACKOFF_BASE_SECS = 2.0" not in inspect.getsource(model)


# ── the process ladder follows agent.recovery_backoff_* ──────────────────────


class TestConfigureDefaultLadder:
    """``configure_default_ladder`` is the seam the two config keys arrive on."""

    def test_the_snapshot_moves_every_unpinned_layer_and_the_delays(self):
        installed = lad.configure_default_ladder(_cfg(7.0, 300.0))
        assert installed is lad.default_ladder()
        for layer in (lad.L1_TOOL_CALL, lad.L2_BACKEND, lad.L3_ACP_RUNTIME, lad.L5_GATEWAY):
            lp = lad.default_ladder().layer_policy(layer)
            assert (lp.base_secs, lp.max_secs) == (7.0, 300.0)
        assert lad.default_ladder().policy.base_secs == 7.0
        assert lad.default_ladder().policy.max_secs == 300.0
        # The delay a consumer actually waits on, not just the table.
        decision = lad.default_ladder().observe_failure(lad.L1_TOOL_CALL, "slot-1")
        assert decision.retry and 3.5 <= decision.delay_secs <= 7.0

    def test_the_pinned_rung_ignores_the_shared_knobs(self):
        l4 = lad.configure_default_ladder(_cfg(7.0, 300.0)).layer_policy(lad.L4_GATEWAYD)
        assert l4.base_secs == lad.GATEWAYD_BACKOFF_BASE_SECS
        assert l4.max_secs == lad.GATEWAYD_BACKOFF_MAX_SECS
        assert l4.cooldown_secs == lad.GATEWAYD_RESPAWN_COOLDOWN_SECS

    def test_a_default_config_reproduces_the_module_table(self):
        installed = lad.configure_default_ladder(KiroCrewConfig())
        assert installed.table() == lad.RecoveryLadder().table()

    def test_the_snapshot_is_one_shot_and_keeps_its_counts(self):
        """Both keys are restart=True: a second call must not hot-reload."""
        cfg = _cfg(7.0, 300.0)
        first = lad.configure_default_ladder(cfg)
        first.observe_failure(lad.L1_TOOL_CALL, "unit-a")
        cfg.agent.recovery_backoff_base_secs = 30.0
        again = lad.configure_default_ladder(cfg)
        assert again is first
        assert first.layer_policy(lad.L1_TOOL_CALL).base_secs == 7.0
        assert first.attempts(lad.L1_TOOL_CALL, "unit-a") == 1

    def test_a_ladder_built_before_the_snapshot_adopts_it(self):
        """Boot order must not decide whether the config is honoured."""
        early = lad.default_ladder()
        early.observe_failure(lad.L2_BACKEND, "backend-a")
        assert early.layer_policy(lad.L2_BACKEND).base_secs == lad.LADDER.base_secs
        assert lad.configure_default_ladder(_cfg(7.0, 300.0)) is early
        assert early.layer_policy(lad.L2_BACKEND).base_secs == 7.0
        assert early.attempts(lad.L2_BACKEND, "backend-a") == 1

    def test_a_config_without_the_keys_is_not_an_error(self):
        installed = lad.configure_default_ladder(object())
        assert installed.policy is lad.LADDER


def test_the_gateway_installs_the_ladder_snapshot_before_its_consumers():
    """Source-level pin on the boot seam; no gateway is booted for it."""
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    assert "configure_default_ladder(self._cfg)" in inspect.getsource(
        GatewayOrchestrator._init_subagents
    )
    boot = inspect.getsource(GatewayOrchestrator.run)
    assert boot.index("self._init_subagents()") < boot.index("self._init_task_runner()")
    assert boot.index("self._init_subagents()") < boot.index("self._start_adaptive_controller()")


def test_manager_records_an_l4_restart_and_escalation(monkeypatch):
    """The supervisor's respawn path is what feeds L4 (source-level pin)."""
    import inspect

    from kiro_crew.mcp_gateway import manager

    src = inspect.getsource(manager.GatewayManager._run_watchdog)
    assert "record_restart(L4_GATEWAYD)" in src
    assert "observe_failure(" in src and "L4_GATEWAYD" in src
    assert "observe_success(L4_GATEWAYD" in src
