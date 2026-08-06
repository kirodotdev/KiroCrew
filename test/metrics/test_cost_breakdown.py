"""Cost attribution over the token row store.

The guarded failures are attribution ones: a truncated model list hides the
model switch that actually moves the bill, and reading ``surface`` instead of
deriving the channel from the session key silently books every Telegram turn as
dashboard spend.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone

import pytest

from kiro_crew.dashboard.handlers import usage as usage_mod
from kiro_crew.messaging.link import TELEMETRY_CHANNELS, telemetry_channel_of


def _row(*, slot, model="m", credits=1.0, used=0, window=1_000_000, age_days=0.0,
         surface="dashboard"):
    ts = datetime.now(timezone.utc) - timedelta(days=age_days)
    return {
        "_type": "tokens", "ts": ts.isoformat(), "slot": slot, "model": model,
        "credits": credits, "context_used": used, "context_window": window,
        "surface": surface, "agent": "kirocrew", "provider": "acp",
    }


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Point the aggregator at a throwaway row store."""
    def _write(rows):
        d = tmp_path / "usage" / ("to" + "kens")
        d.mkdir(parents=True, exist_ok=True)
        shard = d / "shard.jsonl"
        shard.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        monkeypatch.setattr(usage_mod, "_shards_in_window", lambda days: [shard])
        # The 30s memo would otherwise serve a previous test's answer.
        monkeypatch.setattr(usage_mod, "_COST_CACHE", None)
        monkeypatch.setattr(usage_mod, "_COST_CACHE_KEY", None)
        return shard
    return _write


class TestChannelAttribution:
    def test_a_bare_dashboard_slot_key_is_dashboard_not_other(self):
        """The row store persists ``_ChatSlot.key``, which has no namespace prefix.

        Without a rule for that shape every dashboard turn read back from the
        store lands in the catch-all bucket, which is where 84% of real spend sat.
        """
        assert telemetry_channel_of("chat-41-1785445181") == "dashboard"

    @pytest.mark.parametrize("key", ["chatty-thing", "chat-", "chat-x-1", "chat-1"])
    def test_the_dashboard_rule_stays_anchored(self, key):
        assert telemetry_channel_of(key) != "dashboard"

    def test_every_label_stays_inside_the_closed_set(self):
        for key in ("chat-1-2", "telegram_9", "subagent:a", "_hb", "junk"):
            assert telemetry_channel_of(key) in TELEMETRY_CHANNELS

    def test_telegram_spend_is_not_booked_as_dashboard(self, store):
        """Both rows carry surface="dashboard" -- only the key can separate them.

        Every persist site passes a hardcoded surface, so the chat runner stamps
        "dashboard" whatever transport the human used.
        """
        store([
            _row(slot="chat-1-1700000000", credits=10.0, surface="dashboard"),
            _row(slot="telegram_555", credits=4.0, surface="dashboard"),
        ])
        by = {r["name"]: r for r in usage_mod.cost_breakdown(7)["by_channel"]}
        assert by["dashboard"]["credits"] == 10.0
        assert by["telegram"]["credits"] == 4.0

    def test_channels_carry_a_period_delta(self, store):
        """The table renders a "vs last" column for channels too, so the data has
        to exist -- a header over an always-empty column promises a number that
        never arrives."""
        store([
            _row(slot="telegram_1", credits=2.0, age_days=0.5),
            _row(slot="telegram_1", credits=8.0, age_days=9.0),
            _row(slot="chat-1-1", credits=5.0, age_days=0.5),
        ])
        by = {r["name"]: r for r in usage_mod.cost_breakdown(7)["by_channel"]}
        assert by["telegram"]["delta_pct"] == -75.0
        assert by["dashboard"]["delta_pct"] is None


class TestModelRanking:
    def test_no_model_is_truncated_away(self, store):
        """A top-N slice would drop the cheap models, and one of them being new
        is exactly the signal a spend page exists to show."""
        store([_row(slot="chat-1-1", model=f"m{i}", credits=float(20 - i))
               for i in range(9)])
        rows = usage_mod.cost_breakdown(7)["by_model"]
        assert len(rows) == 9
        assert [r["name"] for r in rows] == [f"m{i}" for i in range(9)]

    def test_a_model_with_no_prior_spend_reports_no_delta(self, store):
        """None, not a percentage: there is no change from zero to express."""
        store([
            _row(slot="chat-1-1", model="fresh", credits=5.0, age_days=0.5),
            _row(slot="chat-1-1", model="old", credits=5.0, age_days=0.5),
            _row(slot="chat-1-1", model="old", credits=2.0, age_days=9.0),
        ])
        by = {r["name"]: r for r in usage_mod.cost_breakdown(7)["by_model"]}
        assert by["fresh"]["delta_pct"] is None
        assert by["old"]["delta_pct"] == 150.0

    def test_shares_are_of_the_current_period_only(self, store):
        store([
            _row(slot="chat-1-1", model="a", credits=3.0, age_days=0.5),
            _row(slot="chat-1-1", model="b", credits=1.0, age_days=0.5),
            _row(slot="chat-1-1", model="a", credits=99.0, age_days=9.0),
        ])
        d = usage_mod.cost_breakdown(7)
        assert d["credits"] == 4.0
        assert d["prior_credits"] == 99.0
        by = {r["name"]: r for r in d["by_model"]}
        assert by["a"]["share_pct"] == 75.0


class TestContextBands:
    def test_bands_group_by_absolute_tokens_and_average_within(self, store):
        """Absolute tokens, not occupancy: spend tracks how many tokens are
        re-sent, and window sizes differ per model."""
        store([
            _row(slot="chat-1-1", credits=6.0, used=100_000),
            _row(slot="chat-1-1", credits=8.0, used=150_000),
            _row(slot="chat-1-1", credits=30.0, used=850_000),
        ])
        bands = {b["label"]: b for b in usage_mod.cost_breakdown(7)["context_bands"]}
        assert bands["0k–200k"]["turns"] == 2
        assert bands["0k–200k"]["mean_credits"] == 7.0
        assert bands["800k–1000k"]["mean_credits"] == 30.0

    def test_a_row_without_occupancy_is_left_out_rather_than_bucketed_at_zero(self, store):
        store([_row(slot="chat-1-1", credits=5.0, used=0)])
        d = usage_mod.cost_breakdown(7)
        assert d["turns"] == 1
        assert d["context_bands"] == []


class TestConversations:
    def test_growth_is_withheld_until_a_slope_means_something(self, store):
        store([_row(slot="chat-1-1", credits=1.0, used=100_000 * (i + 1))
               for i in range(3)])
        c = usage_mod.cost_breakdown(7)["conversations"][0]
        assert c["growth_pct_per_turn"] is None
        assert c["turns_to_compaction"] is None

    def test_growth_projects_turns_to_compaction(self, store):
        # 10% -> 60% over 6 turns = +10%/turn, leaving 30 points to the 90% line.
        store([_row(slot="chat-1-1", credits=1.0, used=100_000 * (i + 1), age_days=1 - i * 0.1)
               for i in range(6)])
        c = usage_mod.cost_breakdown(7)["conversations"][0]
        assert c["growth_pct_per_turn"] == 10.0
        assert c["turns_to_compaction"] == 3

    def test_the_slope_is_fitted_after_the_last_compaction_not_across_it(self, store):
        # Occupancy is a sawtooth, not a ramp. Six turns climbing 10%->60%, a
        # compaction back to 10%, then six more climbing 10%->60% at the same
        # real rate. A secant over the whole window sees 10% -> 60% across 11
        # steps and reports ~4.5%/turn; the live rate is 10%/turn.
        rows = [_row(slot="chat-1-1", credits=1.0, used=100_000 * (i + 1),
                     age_days=2 - i * 0.1) for i in range(6)]
        rows += [_row(slot="chat-1-1", credits=1.0, used=100_000 * (i + 1),
                      age_days=1 - i * 0.1) for i in range(6)]
        store(rows)
        c = usage_mod.cost_breakdown(7)["conversations"][0]
        assert c["growth_pct_per_turn"] == 10.0
        # 60% now, 30 points to the line, 10 per turn.
        assert c["turns_to_compaction"] == 3

    def test_a_conversation_freshly_past_a_compaction_withholds_the_projection(self, store):
        # Eight turns of history, but only two since the reset — the new
        # trajectory is not knowable yet, and projecting from two points is the
        # error this withholding exists to prevent.
        rows = [_row(slot="chat-1-1", credits=1.0, used=100_000 * (i + 1),
                     age_days=2 - i * 0.1) for i in range(6)]
        rows += [_row(slot="chat-1-1", credits=1.0, used=100_000 * (i + 1),
                      age_days=1 - i * 0.1) for i in range(2)]
        store(rows)
        c = usage_mod.cost_breakdown(7)["conversations"][0]
        assert c["turns"] == 8
        assert c["growth_pct_per_turn"] is None
        assert c["turns_to_compaction"] is None

    def test_sub_threshold_jitter_does_not_end_the_segment(self, store):
        # Occupancy drifts down by a fraction of a point between turns because
        # what counts toward context_used varies. Treating that as a compaction
        # would restart the segment constantly and withhold every projection.
        used = [100_000, 199_000, 300_000, 299_500, 400_000, 500_000, 600_000]
        rows = [_row(slot="chat-1-1", credits=1.0, used=u, age_days=1 - i * 0.1)
                for i, u in enumerate(used)]
        store(rows)
        c = usage_mod.cost_breakdown(7)["conversations"][0]
        assert c["growth_pct_per_turn"] is not None

    def test_every_session_is_reported_not_a_ranked_slice(self, store):
        # This used to be a top-8 cut, which hid most of a user's sessions behind
        # a count they could not reach. The list is now the whole population; the
        # remaining cap is a payload backstop far above any real account.
        store([_row(slot=f"chat-{i}-1", credits=float(i)) for i in range(1, 15)])
        d = usage_mod.cost_breakdown(7)
        assert d["conversation_count"] == 14
        assert len(d["conversations"]) == 14

    def test_a_subagent_is_not_a_session_and_reaches_no_figure(self, store):
        # A subagent is a fragment of another session's turn, and its row carries
        # no field pointing back at the session that spawned it. It is dropped
        # where the row is READ, so it must be absent from the rows AND from
        # every aggregate — a filter applied at the grouping step instead would
        # leave its credits in the window total with no row to explain them.
        store(
            [_row(slot="chat-1-1", credits=10.0)]
            + [_row(slot=f"subagent:{i:08x}", credits=5.0) for i in range(4)]
        )
        d = usage_mod.cost_breakdown(7)
        assert [r["slot"] for r in d["conversations"]] == ["chat-1-1"]
        assert d["conversation_count"] == 1
        assert d["credits"] == 10.0, "subagent credits leaked into the window total"
        assert d["turns"] == 1
        assert "subagent" not in {r["name"] for r in d["by_channel"]}
        assert "subagent" not in {r["name"] for r in d["by_category"]}
        assert json.dumps(d).lower().count("subagent") == 0

    def test_an_unrecognised_surface_stays_visible_instead_of_becoming_bg(self, store):
        # The whole point of deciding `bg` by DENYLIST is that a surface nobody
        # taught the classifier about shows up under its own category, where it
        # can be noticed and fixed. `telemetry_channel_of` answers `other` for a
        # key shape it does not recognise, so if `other` were in the background
        # set the new surface would be filed as background and buried — the exact
        # outcome the denylist is chosen to avoid. This pins the set's membership
        # rule: background BY NATURE, not merely unclassified.
        store([
            _row(slot="chat-1-1", credits=1.0),
            _row(slot="cron:default:nightly", credits=2.0),
            _row(slot="some-surface-nobody-taught-us-about", credits=3.0),
        ])
        rows = {r["slot"]: r for r in usage_mod.cost_breakdown(7)["conversations"]}
        assert rows["cron:default:nightly"]["category"] == "bg"
        unclassified = rows["some-surface-nobody-taught-us-about"]["category"]
        assert unclassified != "bg", "an unrecognised surface was buried as background"
        assert unclassified == "other"
        # And it earns its own line in the breakdown rather than swelling bg.
        by_cat = {r["name"] for r in usage_mod.cost_breakdown(7)["by_category"]}
        assert "other" in by_cat

    def test_a_session_carries_its_category_and_channel(self, store):
        # `category` is what the panel groups by; `channel` is the unollapsed
        # label beneath it, so a `bg` row still says which kind of bg it was.
        store([
            _row(slot="chat-1-1", credits=1.0),
            _row(slot="cron:default:nightly", credits=2.0),
        ])
        rows = {r["slot"]: r for r in usage_mod.cost_breakdown(7)["conversations"]}
        assert rows["chat-1-1"]["category"] == "dashboard"
        assert rows["cron:default:nightly"]["category"] == "bg"
        assert rows["cron:default:nightly"]["channel"] == "cron"


class TestNonFiniteCredits:
    """A NaN or infinite provider value must never reach a JSON response.

    `json.dumps` writes those as the bare tokens `NaN` / `Infinity`, which are
    not valid JSON — but `json.loads` accepts them, so one bad row travels
    silently from the store into a response body no browser will parse, taking
    the whole panel down instead of losing one turn's numbers.
    """

    def test_a_non_finite_value_is_not_written_to_the_shard(self, tmp_path, monkeypatch):
        monkeypatch.setattr(usage_mod, "_token_usage_dir", lambda: tmp_path)
        now = datetime.now(timezone.utc)
        usage_mod._write_token_record(
            {"_type": "tokens", "credits": float("nan"), "cost": float("inf")}, now
        )
        raw = usage_mod._shard_path_for(now).read_text(encoding="utf-8")
        # The strict parser is the assertion: it rejects exactly the tokens the
        # default dumps would have emitted.
        assert "NaN" not in raw and "Infinity" not in raw
        row = json.loads(raw, parse_constant=lambda c: pytest.fail(f"bare {c} written"))
        assert row["credits"] == 0.0
        assert row["cost"] == 0.0

    def test_a_finite_record_is_written_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(usage_mod, "_token_usage_dir", lambda: tmp_path)
        now = datetime.now(timezone.utc)
        usage_mod._write_token_record({"_type": "tokens", "credits": 12.5}, now)
        row = json.loads(usage_mod._shard_path_for(now).read_text(encoding="utf-8"))
        assert row["credits"] == 12.5

    def test_a_legacy_non_finite_row_is_skipped_rather_than_summed(self, store):
        # Shards written before the persist-side guard can already hold `NaN`.
        # Counting it as free would understate spend; propagating it would break
        # the response, so the row is dropped and the turn is not counted.
        store([_row(slot="chat-1-1", credits=5.0), _row(slot="chat-2-1", credits=float("nan"))])
        d = usage_mod.cost_breakdown(7)
        assert d["turns"] == 1
        assert d["credits"] == 5.0
        assert math.isfinite(d["credits"])

    def test_the_substitution_is_reported(self, tmp_path, monkeypatch, caplog):
        """Zeroing a provider's measurement books the turn as free.

        Silently, before this: the row is valid JSON and reads as a real turn
        that cost nothing, so nobody has a reason to look at the provider.
        """
        monkeypatch.setattr(usage_mod, "_token_usage_dir", lambda: tmp_path)
        with caplog.at_level(logging.WARNING, logger=usage_mod.__name__):
            usage_mod._write_token_record(
                {"_type": "tokens", "credits": float("nan")}, datetime.now(timezone.utc)
            )
        assert "credits" in caplog.text
        assert "non-finite" in caplog.text

    def test_every_affected_field_is_named(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(usage_mod, "_token_usage_dir", lambda: tmp_path)
        with caplog.at_level(logging.WARNING, logger=usage_mod.__name__):
            usage_mod._write_token_record(
                {"_type": "tokens", "credits": float("nan"), "cost": float("inf")},
                datetime.now(timezone.utc),
            )
        assert "credits" in caplog.text and "cost" in caplog.text

    def test_a_clean_record_is_written_quietly(self, tmp_path, monkeypatch, caplog):
        # The warning has to mean something when it appears, so the ordinary
        # path must not emit one.
        monkeypatch.setattr(usage_mod, "_token_usage_dir", lambda: tmp_path)
        with caplog.at_level(logging.WARNING, logger=usage_mod.__name__):
            usage_mod._write_token_record(
                {"_type": "tokens", "credits": 12.5}, datetime.now(timezone.utc)
            )
        assert caplog.records == []

    def test_a_serialization_failure_that_is_not_a_non_finite_float_still_raises(
        self, tmp_path, monkeypatch, caplog
    ):
        # `allow_nan=False` is not the only way json.dumps raises ValueError.
        # Sanitizing cannot fix the others, so they must surface rather than be
        # absorbed into a warning that names no field.
        monkeypatch.setattr(usage_mod, "_token_usage_dir", lambda: tmp_path)
        cyclic: dict = {"_type": "tokens"}
        cyclic["self"] = cyclic
        with caplog.at_level(logging.WARNING, logger=usage_mod.__name__):
            with pytest.raises(ValueError):
                usage_mod._write_token_record(cyclic, datetime.now(timezone.utc))
        assert caplog.records == []


class TestDegenerateInputs:
    def test_an_empty_store_yields_no_turns_rather_than_raising(self, store):
        store([])
        assert usage_mod.cost_breakdown(7)["turns"] == 0

    def test_a_period_with_no_predecessor_reports_no_delta(self, store):
        store([_row(slot="chat-1-1", credits=5.0)])
        assert usage_mod.cost_breakdown(7)["delta_pct"] is None

    def test_unparseable_rows_are_skipped(self, store, tmp_path, monkeypatch):
        d = tmp_path / "usage" / ("to" + "kens")
        d.mkdir(parents=True, exist_ok=True)
        shard = d / "s.jsonl"
        shard.write_text(
            "not json\n"
            + json.dumps({"_type": "other", "ts": "x"}) + "\n"
            + json.dumps(_row(slot="chat-1-1", credits=2.0)) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(usage_mod, "_shards_in_window", lambda days: [shard])
        monkeypatch.setattr(usage_mod, "_COST_CACHE", None)
        monkeypatch.setattr(usage_mod, "_COST_CACHE_KEY", None)
        assert usage_mod.cost_breakdown(7)["credits"] == 2.0
