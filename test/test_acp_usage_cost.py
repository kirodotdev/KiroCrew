"""Per-turn cost/token wiring for the claude ACP seam (issue #6750).

The claude-agent-acp adapter reports a session-cumulative ``cost`` on
``usage_update`` and turn-scoped token counts on the PromptResponse. These
tests pin the shared parse chokepoints in ``acp/_dispatch.py``, the per-turn
delta/accumulation semantics on ``AcpPromptStats``, the single
``to_turn_usage()`` conversion, and the downstream consequence: the per-turn
persist gate fires and a ``usage/tokens/*.jsonl`` row materializes. Harness
parity: a backend that sends neither signal (kiro-cli) must produce
byte-identical behavior to before the wiring existed.
"""

import json
import math
from datetime import datetime

import pytest

from kiro_crew.acp._dispatch import parse_prompt_token_usage, parse_usage_cost
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import AcpPromptStats, TurnUsage

# ── parse_usage_cost ─────────────────────────────────────────────────────


class TestParseUsageCost:
    def test_flat_cost_amount(self):
        assert parse_usage_cost({"cost": {"amount": 0.42, "currency": "USD"}}) == 0.42

    def test_nested_usage_cost_fallback(self):
        update = {"usage": {"used": 1, "size": 2, "cost": {"amount": 1.5}}}
        assert parse_usage_cost(update) == 1.5

    def test_flat_wins_over_nested(self):
        update = {"cost": {"amount": 2.0}, "usage": {"cost": {"amount": 9.0}}}
        assert parse_usage_cost(update) == 2.0

    def test_int_amount_coerced_to_float(self):
        value = parse_usage_cost({"cost": {"amount": 3}})
        assert value == 3.0
        assert isinstance(value, float)

    def test_kiro_shape_has_no_cost(self):
        # kiro-cli's usage_update carries only used/size — must read as absent.
        assert parse_usage_cost({"used": 5000, "size": 10000}) is None

    def test_non_dict_update(self):
        assert parse_usage_cost("nope") is None  # type: ignore[arg-type]
        assert parse_usage_cost(None) is None  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "cost",
        [
            0.5,  # cost not dict-shaped
            "0.5",
            [0.5],
            {"amount": "0.5"},  # string amount
            {"amount": True},  # bool excluded
            {"amount": float("nan")},
            {"amount": float("inf")},
            {"amount": -0.01},  # negative refused
            {"amount": 10**400},  # bignum beyond float range
            {"currency": "USD"},  # amount missing
        ],
    )
    def test_malformed_degrades_to_absent(self, cost):
        assert parse_usage_cost({"cost": cost}) is None

    def test_zero_amount_is_valid(self):
        assert parse_usage_cost({"cost": {"amount": 0}}) == 0.0

    def test_non_usd_currency_drops_cost(self):
        # Consumers store the result in cost_usd fields — a non-USD amount
        # must degrade to absent, never be mislabeled as USD.
        assert parse_usage_cost({"cost": {"amount": 0.42, "currency": "EUR"}}) is None

    def test_absent_currency_is_lenient(self):
        # Adapters that omit currency keep working; USD is assumed.
        assert parse_usage_cost({"cost": {"amount": 0.42}}) == 0.42

    def test_explicit_usd_currency_accepted(self):
        assert parse_usage_cost({"cost": {"amount": 0.42, "currency": "USD"}}) == 0.42

    def test_currency_match_is_case_sensitive_exact_usd(self):
        # ISO 4217 codes are uppercase; a lowercase "usd" is treated as
        # not-USD and drops the cost (exact-match posture, pinned).
        assert parse_usage_cost({"cost": {"amount": 0.42, "currency": "usd"}}) is None

    def test_non_string_currency_drops_cost(self):
        # A non-string currency value is present-and-not-"USD" → absent.
        assert parse_usage_cost({"cost": {"amount": 0.42, "currency": 42}}) is None

    def test_explicit_null_currency_treated_as_absent(self):
        # Issue #6761's prescribed policy: "present (not None)" — an explicit
        # JSON null reads as the adapter omitting currency, so lenience applies.
        # Pinned so a refactor to `"currency" in cost` can't silently flip it.
        assert parse_usage_cost({"cost": {"amount": 0.42, "currency": None}}) == 0.42

    def test_nested_shape_currency_guard_applies(self):
        # The guard runs after the flat/nested resolution, so the nested
        # update.usage.cost shape gets the same currency policy.
        update = {"usage": {"cost": {"amount": 0.42, "currency": "EUR"}}}
        assert parse_usage_cost(update) is None


# ── parse_prompt_token_usage ─────────────────────────────────────────────


class TestParsePromptTokenUsage:
    def test_flat_fields(self):
        result = {
            "stopReason": "end_turn",
            "inputTokens": 100,
            "outputTokens": 50,
            "cachedReadTokens": 30,
            "cachedWriteTokens": 20,
        }
        assert parse_prompt_token_usage(result) == (100, 50, 30, 20)

    def test_nested_usage_fallback(self):
        result = {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 7, "outputTokens": 3},
        }
        assert parse_prompt_token_usage(result) == (7, 3, 0, 0)

    def test_kiro_response_shape_is_none(self):
        # kiro-cli's PromptResponse carries only stopReason: the stats must
        # never be touched on the kiro path (harness parity).
        assert parse_prompt_token_usage({"stopReason": "end_turn"}) is None

    def test_non_dict_result(self):
        assert parse_prompt_token_usage(None) is None
        assert parse_prompt_token_usage("end_turn") is None

    def test_partial_fields_read_zero(self):
        assert parse_prompt_token_usage({"inputTokens": 5}) == (5, 0, 0, 0)

    @pytest.mark.parametrize(
        "bad", ["5", True, float("nan"), float("inf"), -3, [5], {"n": 5}, 10**400]
    )
    def test_malformed_field_reads_zero(self, bad):
        # The key is present, so the tuple is returned — but the malformed
        # value degrades to 0 instead of raising or going negative.
        assert parse_prompt_token_usage({"inputTokens": bad, "outputTokens": 2}) == (0, 2, 0, 0)

    def test_float_counts_coerced_to_int(self):
        tokens = parse_prompt_token_usage({"inputTokens": 10.0})
        assert tokens == (10, 0, 0, 0)
        assert all(isinstance(t, int) and not isinstance(t, bool) for t in tokens)


# ── AcpPromptStats: cost delta + token accumulation ──────────────────────


class TestPromptStatsCostDelta:
    def test_normal_turn_accumulates_delta(self):
        stats = AcpPromptStats()
        stats.apply_cost_cumulative(0.10)
        stats.apply_cost_cumulative(0.25)
        assert math.isclose(stats.cost_usd, 0.25)
        assert math.isclose(stats.cost_session_usd, 0.25)

    def test_carry_over_resets_per_turn_keeps_baseline(self):
        stats = AcpPromptStats()
        stats.apply_cost_cumulative(0.25)
        stats.apply_prompt_token_usage(100, 50, 30, 20)
        nxt = stats.carry_over()
        # Per-turn counters start fresh…
        assert nxt.cost_usd == 0.0
        assert nxt.input_tokens == 0
        assert nxt.output_tokens == 0
        assert nxt.cache_read_tokens == 0
        assert nxt.cache_write_tokens == 0
        # …but the cumulative baseline survives the turn boundary, so the next
        # turn is billed only its own movement.
        assert math.isclose(nxt.cost_session_usd, 0.25)
        nxt.apply_cost_cumulative(0.40)
        assert math.isclose(nxt.cost_usd, 0.15)

    def test_cumulative_reset_never_emits_negative_delta(self):
        stats = AcpPromptStats()
        stats.apply_cost_cumulative(0.25)
        # Adapter restarted: cumulative dropped below the baseline. The new
        # total is spend since the reset — taken whole, never negative.
        stats.apply_cost_cumulative(0.05)
        assert math.isclose(stats.cost_usd, 0.30)
        assert stats.cost_usd >= 0.25  # never went down
        assert math.isclose(stats.cost_session_usd, 0.05)

    def test_reset_context_state_drops_baseline(self):
        stats = AcpPromptStats()
        stats.apply_cost_cumulative(0.25)
        stats.reset_context_state()
        assert stats.cost_session_usd == 0.0
        # A fresh session's first reading is billed whole against the zeroed
        # baseline, not against the old session's total.
        stats.apply_cost_cumulative(0.10)
        assert math.isclose(stats.cost_usd, 0.25 + 0.10)

    def test_compaction_and_window_rebase_leave_cost_alone(self):
        # The adapter's cumulative counter is unrelated to context-window
        # events: neither boundary may clobber the delta or the baseline.
        stats = AcpPromptStats()
        stats.apply_cost_cumulative(0.20)
        stats.reset_after_compaction()
        assert math.isclose(stats.cost_usd, 0.20)
        assert math.isclose(stats.cost_session_usd, 0.20)
        stats.rebase_to_window(200_000)
        assert math.isclose(stats.cost_usd, 0.20)
        assert math.isclose(stats.cost_session_usd, 0.20)
        stats.apply_cost_cumulative(0.35)
        assert math.isclose(stats.cost_usd, 0.35)

    def test_token_accumulation_sums(self):
        stats = AcpPromptStats()
        stats.apply_prompt_token_usage(10, 5, 3, 2)
        stats.apply_prompt_token_usage(1, 1, 1, 1)
        assert stats.input_tokens == 11
        assert stats.output_tokens == 6
        assert stats.cache_read_tokens == 4
        assert stats.cache_write_tokens == 3


# ── to_turn_usage: single source of truth + harness parity ───────────────


class TestToTurnUsage:
    def test_maps_every_billing_dimension(self):
        stats = AcpPromptStats(credits=2.5)
        stats.apply_cost_cumulative(0.42)
        stats.apply_prompt_token_usage(100, 50, 30, 20)
        u = stats.to_turn_usage()
        assert u.input_tokens == 100
        assert u.output_tokens == 50
        assert u.cache_read_tokens == 30
        # Anthropic's "cache write" is a cache-creation charge.
        assert u.cache_creation_tokens == 20
        assert math.isclose(u.cost_usd, 0.42)
        assert u.credits == 2.5

    def test_harness_parity_credits_only(self):
        # A backend that never sends cost or token counts (kiro-cli) must
        # produce a TurnUsage byte-identical to the pre-wiring
        # TurnUsage(credits=...) construction.
        stats = AcpPromptStats(credits=1.23)
        assert stats.to_turn_usage() == TurnUsage(credits=1.23)

    def test_harness_parity_across_turn_boundary(self):
        stats = AcpPromptStats(credits=1.0).carry_over()
        assert stats.to_turn_usage() == TurnUsage()


# ── AcpClient tracking (no process spawn) ────────────────────────────────


def _bare_client() -> AcpClient:
    client = AcpClient.__new__(AcpClient)  # avoid spawning a real process
    client.last_prompt_stats = AcpPromptStats()
    return client


class _Msg:
    def __init__(self, update):
        self.params = {"update": update}


class TestClientTracking:
    def test_usage_update_cost_folds_into_stats(self):
        client = _bare_client()
        client._track_usage_update(
            _Msg(
                {
                    "sessionUpdate": "usage_update",
                    "used": 10,
                    "size": 100,
                    "cost": {"amount": 0.30, "currency": "USD"},
                }
            )
        )
        client._track_usage_update(
            _Msg(
                {
                    "sessionUpdate": "usage_update",
                    "used": 20,
                    "size": 100,
                    "cost": {"amount": 0.50, "currency": "USD"},
                }
            )
        )
        assert math.isclose(client.last_prompt_stats.cost_usd, 0.50)
        # The token-count context tracking is untouched by the cost wiring.
        assert client.last_prompt_stats.context_used_tokens == 20

    def test_usage_update_without_cost_is_byte_identical(self):
        client = _bare_client()
        client._track_usage_update(_Msg({"sessionUpdate": "usage_update", "used": 10, "size": 100}))
        assert client.last_prompt_stats.cost_usd == 0.0
        assert client.last_prompt_stats.cost_session_usd == 0.0

    def test_prompt_usage_folds_tokens(self):
        client = _bare_client()
        client._track_prompt_usage(
            {
                "stopReason": "end_turn",
                "inputTokens": 100,
                "outputTokens": 50,
                "cachedReadTokens": 30,
                "cachedWriteTokens": 20,
            }
        )
        u = client.last_prompt_stats.to_turn_usage()
        assert (u.input_tokens, u.output_tokens) == (100, 50)
        assert (u.cache_read_tokens, u.cache_creation_tokens) == (30, 20)

    def test_prompt_usage_kiro_response_is_noop(self):
        client = _bare_client()
        client._track_prompt_usage({"stopReason": "end_turn"})
        assert client.last_prompt_stats.to_turn_usage() == TurnUsage()

    def test_reset_state_drops_cost_baseline(self):
        """A replacement process restarts the adapter's cumulative cost counter
        at zero. If the delta baseline survived ``_reset_state``, a new counter
        that catches back up to the old total would bill only the difference
        (the monotonic guard cannot see a caught-up counter), silently dropping
        spend."""
        from collections import deque
        from unittest.mock import patch

        client = _bare_client()
        client._process = None
        client._pid = None
        client._session_id = "s-old"
        client._buffer = bytearray()
        client._cancelled = False
        client._resumed = False
        client._sandbox_cleanup = None
        client._child_pids = {}
        client._stderr_lines = deque(maxlen=20)
        client._stderr_task = None
        client._pending_oauth_requests = []
        client._oauth_emitted_servers = set()
        client.last_prompt_stats.apply_cost_cumulative(0.50)

        with patch("kiro_crew.session._untrack_pid"):
            client._reset_state()

        assert client.last_prompt_stats.cost_session_usd == 0.0
        # The already-billed per-turn delta is kept (carry_over() zeroes it at
        # the next turn boundary); only the process-scoped baseline restarts.
        assert client.last_prompt_stats.cost_usd == pytest.approx(0.50)
        # The new process's first reading is billed whole, not delta'd against
        # the dead process's counter.
        client.last_prompt_stats = client.last_prompt_stats.carry_over()
        client.last_prompt_stats.apply_cost_cumulative(0.55)
        assert client.last_prompt_stats.cost_usd == pytest.approx(0.55)


# ── consequence: the persist gate fires and a jsonl row materializes ─────


class TestPersistGateFires:
    def test_claude_seam_row_materializes(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import usage as usage_mod

        shard_dir = tmp_path / "tokens"
        shard_dir.mkdir()
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)

        stats = AcpPromptStats()
        stats.apply_cost_cumulative(0.42)
        stats.apply_prompt_token_usage(100, 50, 30, 20)
        u = stats.to_turn_usage()

        # The chat-runner persist gate (chat_runner.py) — previously all zeros
        # on the claude seam, so the row was never written.
        assert u.input_tokens or u.output_tokens or u.credits

        usage_mod.persist_token_record("slot-1", "test-model", u, "acp")
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        rows = (shard_dir / f"{today}.jsonl").read_text(encoding="utf-8").strip().splitlines()
        record = json.loads(rows[0])
        assert record["input"] == 100
        assert record["output"] == 50
        assert record["cache_read"] == 30
        assert record["cache_create"] == 20
        assert math.isclose(record["cost"], 0.42)

    def test_kiro_seam_gate_behavior_unchanged(self):
        # No cost, no tokens: the gate fires exactly when credits are non-zero,
        # as before the wiring.
        empty = AcpPromptStats().to_turn_usage()
        assert not (empty.input_tokens or empty.output_tokens or empty.credits)
        credits_only = AcpPromptStats(credits=1.0).to_turn_usage()
        assert credits_only.input_tokens or credits_only.output_tokens or credits_only.credits
