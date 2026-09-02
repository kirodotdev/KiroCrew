"""The per-turn usage family: tokens, credits, cost_usd, and their attributes.

Drives the REAL production emitters -- ``metrics.turns.emit_turn_usage`` and the
two owners that call it (``usage._emit_turn_histogram`` for every background
surface, ``chat_runner._emit_turn_metric`` for the dashboard) -- against a
capturing recorder, so the instrument names, the non-zero gate, the
model/provider attributes and the exactly-once split all live in production
rather than in this file.

Also covers the aggregator end: a credit amount must NOT be reported under a
``*_ms`` key, because the Telemetry page formats those as durations.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.metrics.turns import (
    TURN_COST_METRIC,
    TURN_CREDITS_METRIC,
    TURN_METRIC,
    TURN_TOKENS_METRIC,
    emit_turn_usage,
    turn_outcome,
)


class _CapturingRecorder:
    """Stand-in recorder capturing both instrument kinds this family uses."""

    def __init__(self) -> None:
        self.hist: list = []
        self.ctr: list = []

    def histogram(self, name, value, *, unit="ms", attrs=None, **kw) -> None:
        self.hist.append({"name": name, "value": value, "unit": unit, "attrs": dict(attrs or {})})

    def counter(self, name, value=1, *, unit="1", attrs=None, **kw) -> None:
        self.ctr.append({"name": name, "value": value, "unit": unit, "attrs": dict(attrs or {})})

    def named(self, name):
        return [c for c in self.hist + self.ctr if c["name"] == name]


def _emit(**kwargs) -> _CapturingRecorder:
    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.turns.get_recorder", return_value=rec):
        emit_turn_usage(**kwargs)
    return rec


class TestTokenCounter:
    def test_input_and_output_share_one_instrument_split_by_direction(self):
        rec = _emit(input_tokens=1200, output_tokens=340)
        calls = rec.named(TURN_TOKENS_METRIC)
        assert len(calls) == 2
        by_dir = {c["attrs"]["direction"]: c for c in calls}
        assert by_dir["input"]["value"] == 1200
        assert by_dir["output"]["value"] == 340
        assert {c["unit"] for c in calls} == {"token"}

    def test_it_is_a_counter_not_a_histogram(self):
        """Token volume is a sum over the window, not a distribution.

        A histogram would also drag the instrument into the bucket-table
        contract for no gain -- nobody asks for the p90 of a turn's input size.
        """
        rec = _emit(input_tokens=10, output_tokens=10)
        assert len(rec.ctr) == 2
        assert rec.hist == []

    def test_a_zero_direction_emits_no_series(self):
        """A backend that reports no output tokens must not publish a 0 series.

        A recorded 0 reads as a measured zero on the Telemetry page; an absent
        series reads as absent, which is the truth for a backend that does not
        bill in this dimension.
        """
        rec = _emit(input_tokens=500, output_tokens=0)
        assert [c["attrs"]["direction"] for c in rec.named(TURN_TOKENS_METRIC)] == ["input"]

    def test_nothing_at_all_when_the_backend_reports_no_tokens(self):
        """The shipped kiro/acp shape: credits only, every token field zero."""
        rec = _emit(input_tokens=0, output_tokens=0, credits=6.75)
        assert rec.named(TURN_TOKENS_METRIC) == []
        assert len(rec.named(TURN_CREDITS_METRIC)) == 1


class TestBillingHistograms:
    def test_credits_only_when_non_zero(self):
        assert len(_emit(credits=6.75).named(TURN_CREDITS_METRIC)) == 1
        assert _emit(credits=0.0).named(TURN_CREDITS_METRIC) == []

    def test_cost_only_when_non_zero(self):
        assert len(_emit(cost_usd=0.0032).named(TURN_COST_METRIC)) == 1
        assert _emit(cost_usd=0.0).named(TURN_COST_METRIC) == []

    def test_a_backend_emits_one_of_the_two_never_both(self):
        """``acp/types.py``: "Consumers read whichever is non-zero"."""
        acp = _emit(credits=6.75, cost_usd=0.0)
        assert [c["name"] for c in acp.hist] == [TURN_CREDITS_METRIC]
        cc = _emit(credits=0.0, cost_usd=1.25)
        assert [c["name"] for c in cc.hist] == [TURN_COST_METRIC]

    def test_units_are_the_billed_unit_not_milliseconds(self):
        rec = _emit(credits=1.0, cost_usd=1.0)
        units = {c["name"]: c["unit"] for c in rec.hist}
        assert units == {TURN_CREDITS_METRIC: "credit", TURN_COST_METRIC: "usd"}

    def test_a_sub_cent_cost_survives(self):
        """Values below the ms family's rounding must reach the recorder intact."""
        assert _emit(cost_usd=0.0007).named(TURN_COST_METRIC)[0]["value"] == 0.0007


class TestDefensiveValueHandling:
    @pytest.mark.parametrize("bad", [None, "", "abc", object()])
    def test_a_non_numeric_field_emits_nothing_and_does_not_raise(self, bad):
        rec = _emit(input_tokens=bad, credits=bad, cost_usd=bad)
        assert rec.hist == [] and rec.ctr == []

    def test_a_negative_amount_is_dropped(self):
        """A monotonic counter cannot take a negative back, and a negative
        sample would drag a histogram's mean below anything that happened."""
        rec = _emit(input_tokens=-5, output_tokens=-1, credits=-2.0, cost_usd=-0.5)
        assert rec.hist == [] and rec.ctr == []

    def test_a_non_finite_amount_is_dropped(self):
        rec = _emit(credits=float("inf"), cost_usd=float("nan"))
        assert rec.hist == []

    def test_a_raising_recorder_never_reaches_the_caller(self):
        class _Boom:
            def counter(self, *a, **k):
                raise RuntimeError("boom")

            def histogram(self, *a, **k):
                raise RuntimeError("boom")

        with patch("kiro_crew.metrics.turns.get_recorder", return_value=_Boom()):
            emit_turn_usage(input_tokens=1, credits=1.0, cost_usd=1.0)


class TestModelProviderAttrs:
    def test_both_ride_every_instrument_in_the_family(self):
        rec = _emit(
            input_tokens=10,
            output_tokens=5,
            credits=1.0,
            cost_usd=2.0,
            model="some-model",
            provider="kiro",
        )
        assert rec.hist and rec.ctr
        for call in rec.hist + rec.ctr:
            assert call["attrs"]["model"] == "some-model", call["name"]
            assert call["attrs"]["provider"] == "kiro", call["name"]

    def test_an_empty_value_is_omitted_rather_than_sent_blank(self):
        """``model=""`` would publish a series that reads as a real nameless model."""
        rec = _emit(credits=1.0, model="", provider="kiro")
        attrs = rec.named(TURN_CREDITS_METRIC)[0]["attrs"]
        assert "model" not in attrs
        assert attrs["provider"] == "kiro"

    def test_no_outcome_or_session_source_on_the_usage_family(self):
        """Both live on kirocrew.turn.duration, which samples the same turns.

        Repeating them here would multiply these series by two dimensions to
        answer a question the duration instrument already answers.
        """
        rec = _emit(input_tokens=10, credits=1.0, model="m", provider="p")
        for call in rec.hist + rec.ctr:
            assert "outcome" not in call["attrs"]
            assert "session_source" not in call["attrs"]


class TestCancelledOutcome:
    def test_a_user_cancel_is_its_own_label(self):
        assert turn_outcome("cancelled") == "cancelled"

    def test_it_is_not_a_terminal_fault(self):
        from kiro_crew.dashboard.handlers.telemetry import _TERMINAL_FAULT_OUTCOMES

        assert "cancelled" not in _TERMINAL_FAULT_OUTCOMES

    def test_the_watchdog_cancel_wedge_stays_an_error(self):
        assert turn_outcome("error: cancel unacked") == "error"


class TestStopReasonVocabularyIsPinnedToTheBackend:
    """`metrics/turns.py` spells its stop reasons instead of importing them.

    It has to: `scripts/check_agent_sdk_boundary.py` forbids application code
    from importing `kiro_crew.acp`, and the baseline it ratchets can only shrink,
    so a metrics leaf cannot take an exemption. Duplicating a wire constant is
    only safe with a guard, and this is it -- the test tree is outside the gate's
    scope (`DEFAULT_TARGETS = ("src",)`), so these assertions may import what the
    module may not.

    Without them, a rename on the backend side would leave `turn_outcome`
    comparing against a string nothing sends: every cancelled turn would
    silently classify as `error` again, restoring the exact defect this PR
    removes, with no failing test anywhere.
    """

    def test_each_local_constant_equals_its_backend_counterpart(self):
        from kiro_crew.acp.types import (
            STOP_REASON_CANCELLED,
            STOP_REASON_END_TURN,
            STOP_REASON_STALE_RECOVER,
            STOP_REASON_TOOL_STALL,
        )
        from kiro_crew.metrics import turns

        assert turns._STOP_CANCELLED == STOP_REASON_CANCELLED
        assert turns._STOP_TOOL_STALL == STOP_REASON_TOOL_STALL
        assert turns._STOP_STALE_RECOVER == STOP_REASON_STALE_RECOVER
        # The clean-completion set is spelled inline in the branch, so pin the one
        # backend constant among those literals too.
        assert turn_outcome(STOP_REASON_END_TURN) == "ok"

    def test_the_module_does_not_import_the_acp_layer(self):
        """Pins the boundary itself, so a future edit cannot quietly re-add it.

        The repo gate only inspects files a given diff touches, so a PR that does
        not touch this module would not notice the import coming back.
        """
        import ast
        from pathlib import Path

        from kiro_crew.metrics import turns

        tree = ast.parse(Path(turns.__file__).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
        offenders = [
            m
            for m in imported
            if m.startswith("kiro_crew.acp") or m.startswith("kiro_crew.providers")
        ]
        assert not offenders, (
            f"metrics/turns.py imports the ACP layer again: {offenders}. "
            "Application code must not; spell the constant and pin it above."
        )


class TestSharedBoundaryEmitsTheWholeFamily:
    """``usage._emit_turn_histogram`` is the one call every surface makes."""

    def _run(self, record, event=None):
        from kiro_crew.dashboard.handlers import usage as usage_mod

        rec = _CapturingRecorder()
        with patch("kiro_crew.metrics.turns.get_recorder", return_value=rec):
            usage_mod._emit_turn_histogram(record, "cron:nightly", event or object())
        return rec

    def _record(self, **over):
        row = {
            "duration_ms": 42000,
            "input": 1000,
            "output": 200,
            "credits": 6.75,
            "cost": 0.0,
            "model": "some-model",
            "provider": "kiro",
        }
        row.update(over)
        return row

    def test_duration_and_usage_are_emitted_together(self):
        rec = self._run(self._record())
        assert len(rec.named(TURN_METRIC)) == 1
        assert len(rec.named(TURN_TOKENS_METRIC)) == 2
        assert len(rec.named(TURN_CREDITS_METRIC)) == 1
        assert rec.named(TURN_COST_METRIC) == []

    def test_model_and_provider_come_off_the_record(self):
        """So the row store and the instruments cannot disagree about one turn."""
        rec = self._run(self._record(model="other-model", provider="claude_code"))
        for call in rec.hist + rec.ctr:
            assert call["attrs"]["model"] == "other-model"
            assert call["attrs"]["provider"] == "claude_code"

    def test_the_duration_sample_carries_them_too(self):
        rec = self._run(self._record())
        attrs = rec.named(TURN_METRIC)[0]["attrs"]
        assert attrs["model"] == "some-model"
        assert attrs["provider"] == "kiro"
        # ...alongside what it already carried.
        assert attrs["outcome"] == "unclassified"
        assert attrs["session_source"] == "cron"

    def test_a_row_with_no_billing_still_yields_the_duration(self):
        rec = self._run(self._record(input=0, output=0, credits=0.0, cost=0.0))
        assert len(rec.named(TURN_METRIC)) == 1
        assert rec.ctr == []
        assert len(rec.hist) == 1


class TestDashboardOwnerEmitsTheWholeFamily:
    """The dashboard emits for itself, because its persist call is gated.

    ``_run_chat`` passes ``emit_metric=False`` so the shared boundary does not
    double-sample. A usage emit that lived only at the boundary would therefore
    leave the busiest surface contributing no token or spend samples at all --
    that is what this class pins.
    """

    def _run(self, usage, **kw):
        from kiro_crew.dashboard import chat_runner

        rec = _CapturingRecorder()
        with patch("kiro_crew.metrics.turns.get_recorder", return_value=rec):
            chat_runner._emit_turn_metric(
                0, "end_turn", "dashboard:abc123", elapsed_ms=1500, usage=usage, **kw
            )
        return rec

    def test_usage_reaches_the_instruments_from_the_dashboard_path(self):
        from kiro_crew.acp.types import TurnUsage

        rec = self._run(
            TurnUsage(input_tokens=900, output_tokens=120, credits=3.5),
            model="some-model",
            provider="kiro",
        )
        assert len(rec.named(TURN_METRIC)) == 1
        assert len(rec.named(TURN_TOKENS_METRIC)) == 2
        assert rec.named(TURN_CREDITS_METRIC)[0]["value"] == 3.5

    def test_a_turn_with_no_usage_object_emits_only_the_duration(self):
        """An errored turn can complete without usage; that must not raise."""
        rec = self._run(None)
        assert len(rec.named(TURN_METRIC)) == 1
        assert rec.ctr == []


class TestServedBackendAttribution:
    """`provider` must name the backend that RAN the turn.

    `cfg.agent.provider` cannot: it is declared `enum=["acp"]` with default
    "acp", so reading it stamped every dashboard turn -- claude_code and KAS
    included -- with one constant, and a provider split over a constant answers
    nothing. `providers.acp.provider_label` resolves the live client to the same
    three-value vocabulary that `subagent_manager` already writes.
    """

    def test_the_config_field_is_a_single_valued_enum(self):
        """The premise. If this ever gains values, revisit the emit site."""
        from dataclasses import fields

        from kiro_crew.config.loader import AgentConfig

        provider = next(f for f in fields(AgentConfig) if f.name == "provider")
        assert provider.default == "acp"
        assert provider.metadata["enum"] == ["acp"]

    def test_label_distinguishes_the_backend_the_config_cannot(self):
        """`is_claude_backend` separates the two backends this surface can see.

        The richer `provider_label` (which also names KAS) is deliberately NOT
        used: the boundary gate rejects an ACP-layer import on any line a change
        touches, so chat_runner cannot reach it. Asserted here anyway, so the day
        the label is exposed through `kiro_crew.agent_sdk` the KAS gap is already
        described.
        """
        from kiro_crew.acp.session_provider import AcpSessionProvider
        from kiro_crew.acp.types import (
            ACP_BACKEND_CLAUDE,
            ACP_BACKEND_KAS,
            PROVIDER_LABEL_CLAUDE,
            PROVIDER_LABEL_DEFAULT,
            PROVIDER_LABEL_KAS,
        )
        from kiro_crew.providers.acp import provider_label

        # `backend` is a read-only property that delegates to the live runtime, so
        # a stand-in shadows it with a class attribute rather than assigning it.
        class _Claude(AcpSessionProvider):
            backend = ACP_BACKEND_CLAUDE  # type: ignore[assignment]

        class _Kas(AcpSessionProvider):
            backend = ACP_BACKEND_KAS  # type: ignore[assignment]

        assert provider_label(object.__new__(_Claude)) == PROVIDER_LABEL_CLAUDE
        # The residue this PR accepts: chat_runner labels a KAS turn "acp",
        # because the only thing that separates them lives behind the boundary.
        assert provider_label(object.__new__(_Kas)) == PROVIDER_LABEL_KAS
        assert PROVIDER_LABEL_KAS != PROVIDER_LABEL_DEFAULT
        assert provider_label(object()) == PROVIDER_LABEL_DEFAULT

    def test_the_emit_site_reads_the_client_not_the_config_field(self):
        """Pins the resolution, so a revert to `cfg.agent.provider` reddens here.

        Scoped to the assignment: `_run_chat` legitimately reads
        `cfg.agent.provider` elsewhere, for the separate `provider_name` local the
        model-resolution branches use.
        """
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._run_chat)
        assert '_provider_name = "claude_code" if is_claude_backend(client) else "acp"' in src
        assert "_provider_name = cfg.agent.provider" not in src

    def test_chat_runner_does_not_import_provider_label(self):
        """The constraint that shaped the resolution above.

        `scripts/check_agent_sdk_boundary.py` rejects an ACP-layer import sitting
        on any line a change touches -- baselined file or not -- so pulling
        `provider_label` in, even onto the module's existing
        `from kiro_crew.providers.acp import ...` line, is a hard gate failure.
        Named directly rather than diffed against main: a new ACP import can only
        appear by touching this file, which is exactly when the repo gate runs, so
        the one thing worth pinning here is the symbol that looked like the obvious
        fix.

        Checked as an IMPORTED NAME via AST, not as a substring: the emit site's
        own comment explains why the symbol is not used, and a text search matches
        that prose.
        """
        import ast
        from pathlib import Path

        from kiro_crew.dashboard import chat_runner

        tree = ast.parse(Path(chat_runner.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imported.update(alias.name for alias in node.names)
        assert "provider_label" not in imported, (
            "chat_runner imports provider_label again; the boundary gate fails on "
            "an ACP-layer import on any touched line. Resolve the backend with the "
            "already-imported is_claude_backend, or expose the label through "
            "kiro_crew.agent_sdk."
        )
        # Guard the guard: the harvest must actually see this module's imports.
        assert "is_claude_backend" in imported

    def test_the_metric_model_prefers_the_served_id(self):
        """A fallback-served turn is attributed, not dropped from the split.

        The ROW blanks the model for that case on purpose (billing a model that
        never ran is wrong); the metric answers a different question, so it takes
        `read_turn_model`'s served id and falls back to the row's value only when
        the backend reported none.
        """
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._run_chat)
        assert "model=_turn_model or _record_model" in src


class TestAggregatorReportsAmountsWithoutMsKeys:
    """The design trap: the generic histogram surface reports every stat as
    ``*_ms`` and the frontend formats those with a millisecond suffix. A credit
    amount arriving there would be rendered as a duration."""

    def _aggregate(self, tmp_path: Path, points):
        from kiro_crew.dashboard.handlers.telemetry import _aggregate

        metrics = []
        for name, bounds, landed, total in points:
            counts = [0] * (len(bounds) + 1)
            counts[landed] = 1
            metrics.append(
                {
                    "name": name,
                    "data": {
                        "data_points": [
                            {
                                "attributes": {},
                                "count": 1,
                                "sum": total,
                                "min": total,
                                "max": total,
                                "bucket_counts": counts,
                                "explicit_bounds": list(bounds),
                            }
                        ]
                    },
                }
            )
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        shard = tmp_path / f"metrics-{day}-1234.jsonl"
        shard.write_text(
            json.dumps({"resource_metrics": [{"scope_metrics": [{"metrics": metrics}]}]}) + "\n",
            encoding="utf-8",
        )
        return _aggregate([shard])

    def test_credits_are_reported_in_the_turn_block_under_unit_neutral_keys(self, tmp_path):
        from kiro_crew.metrics.provider import _CREDIT_BUCKETS

        out = self._aggregate(tmp_path, [(TURN_CREDITS_METRIC, _CREDIT_BUCKETS, 8, 6.75)])
        credits = out["turn"]["credits"]
        assert credits["count"] == 1
        assert credits["unit"] == "credit"
        assert credits["total"] == 6.75
        assert "p50" in credits
        assert not [k for k in credits if k.endswith("_ms")], credits

    def test_they_never_appear_on_the_generic_ms_surface(self, tmp_path):
        from kiro_crew.metrics.provider import _CREDIT_BUCKETS, _USD_BUCKETS

        out = self._aggregate(
            tmp_path,
            [
                (TURN_CREDITS_METRIC, _CREDIT_BUCKETS, 8, 6.75),
                (TURN_COST_METRIC, _USD_BUCKETS, 3, 0.0075),
            ],
        )
        assert [row["name"] for row in out["other"]] == []
        assert out["turn"]["cost_usd"]["unit"] == "usd"

    def test_a_host_that_does_not_bill_in_a_unit_reports_an_empty_block(self, tmp_path):
        """Absent must read as absent, not as a measured zero."""
        from kiro_crew.metrics.provider import _CREDIT_BUCKETS

        out = self._aggregate(tmp_path, [(TURN_CREDITS_METRIC, _CREDIT_BUCKETS, 8, 6.75)])
        assert out["turn"]["cost_usd"] == {"count": 0, "unit": "usd"}

    def test_a_sub_cent_p50_is_not_rounded_away(self, tmp_path):
        from kiro_crew.metrics.provider import _USD_BUCKETS

        out = self._aggregate(tmp_path, [(TURN_COST_METRIC, _USD_BUCKETS, 1, 0.002)])
        cost = out["turn"]["cost_usd"]
        assert cost["total"] == 0.002
        assert 0 < cost["p50"] <= _USD_BUCKETS[1]


class TestSpendAttributionByModel:
    """The `model` attribute has to survive the aggregator, not just the emitter.

    The emitters stamp `model` on both spend histograms, but the turn block used to
    fold every point into one pooled `_Hist`, so the attribute was recorded and
    then discarded at the one place that reports the amount -- "which model spent
    this" was unanswerable from the API despite being emitted.
    """

    def _aggregate(self, tmp_path: Path, points):
        """`points` are (name, bounds, landed_bucket, total, attrs) tuples."""
        from kiro_crew.dashboard.handlers.telemetry import _aggregate

        metrics = []
        for name, bounds, landed, total, attrs in points:
            counts = [0] * (len(bounds) + 1)
            counts[landed] = 1
            metrics.append(
                {
                    "name": name,
                    "data": {
                        "data_points": [
                            {
                                "attributes": dict(attrs),
                                "count": 1,
                                "sum": total,
                                "min": total,
                                "max": total,
                                "bucket_counts": counts,
                                "explicit_bounds": list(bounds),
                            }
                        ]
                    },
                }
            )
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        shard = tmp_path / f"metrics-{day}-4321.jsonl"
        shard.write_text(
            json.dumps({"resource_metrics": [{"scope_metrics": [{"metrics": metrics}]}]}) + "\n",
            encoding="utf-8",
        )
        return _aggregate([shard])

    def _credit_points(self, spend):
        from kiro_crew.metrics.provider import _CREDIT_BUCKETS

        return [(TURN_CREDITS_METRIC, _CREDIT_BUCKETS, 8, amount, attrs) for amount, attrs in spend]

    def test_spend_is_split_by_model_biggest_spender_first(self, tmp_path):
        out = self._aggregate(
            tmp_path,
            self._credit_points(
                [
                    (2.0, {"model": "cheap", "provider": "acp"}),
                    (10.0, {"model": "pricey", "provider": "acp"}),
                ]
            ),
        )
        credits = out["turn"]["credits"]
        assert [row["model"] for row in credits["by_model"]] == ["pricey", "cheap"]
        assert [row["total"] for row in credits["by_model"]] == [10.0, 2.0]
        assert credits["by_model"][0]["count"] == 1
        assert credits["by_model"][0]["mean"] == 10.0

    def test_the_split_totals_reconcile_with_the_pooled_total(self, tmp_path):
        """A reader must not have to wonder whether the parts sum to the whole."""
        out = self._aggregate(
            tmp_path,
            self._credit_points(
                [(2.0, {"model": "a"}), (10.0, {"model": "b"}), (1.0, {"model": "c"})]
            ),
        )
        credits = out["turn"]["credits"]
        assert credits["total"] == 13.0
        assert sum(row["total"] for row in credits["by_model"]) == credits["total"]
        assert sum(row["count"] for row in credits["by_model"]) == credits["count"]

    def test_a_turn_with_no_model_attribute_is_counted_under_unknown(self, tmp_path):
        """The emitter omits `model` entirely when it is empty; the spend still happened."""
        out = self._aggregate(
            tmp_path, self._credit_points([(5.0, {"provider": "acp"}), (1.0, {"model": "named"})])
        )
        credits = out["turn"]["credits"]
        assert {row["model"] for row in credits["by_model"]} == {"unknown", "named"}
        assert sum(row["total"] for row in credits["by_model"]) == credits["total"] == 6.0

    def test_the_split_is_never_truncated(self, tmp_path):
        """Pins the convention the sibling `cost_breakdown.by_model` states.

        A top-N cut would hide exactly the cheap-model-creep a spend split exists
        to show, so every model is named however many there are.
        """
        spend = [(float(30 - i), {"model": f"m{i:02d}"}) for i in range(30)]
        out = self._aggregate(tmp_path, self._credit_points(spend))
        rows = out["turn"]["credits"]["by_model"]
        assert len(rows) == 30
        assert sum(r["total"] for r in rows) == out["turn"]["credits"]["total"]

    def test_no_split_key_when_nothing_was_collected(self, tmp_path):
        """Absent rather than an empty list, so a non-billing host publishes no key."""
        from kiro_crew.metrics.provider import _USD_BUCKETS

        out = self._aggregate(
            tmp_path, [(TURN_COST_METRIC, _USD_BUCKETS, 3, 0.0075, {"model": "m"})]
        )
        assert "by_model" in out["turn"]["cost_usd"]
        assert out["turn"]["credits"] == {"count": 0, "unit": "credit"}
