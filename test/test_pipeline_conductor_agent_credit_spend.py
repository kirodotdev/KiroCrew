"""Pipeline-conductor ``credit_spend.py``, the bundled budget script, run over fixtures.

The script half of ``test_pipeline_conductor_agent.py`` for the budget: credits summed
from the usage shards, the budget verdicts, and every way an incomplete or malformed
read refuses to report ``within``.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline_conductor_agent_helpers import SKILL_DIR
from skill_script_helpers import load_skill_script


class TestCreditSpend:
    def _mod(self):
        return load_skill_script("credit_spend", SKILL_DIR / "scripts" / "credit_spend.py")

    def _shard(self, usage_dir: Path, name: str, rows: list[dict]) -> None:
        usage_dir.mkdir(parents=True, exist_ok=True)
        (usage_dir / name).write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )

    def _run(self, mod, capsys, *args: str) -> dict:
        assert mod.main(list(args)) == 0
        return json.loads(capsys.readouterr().out)

    def test_sums_credits_and_counts_rows_as_turns(self, tmp_path, capsys):
        """Production rows are per-turn with a literal ``turns: 0`` field
        (measured on real shards), so turns are counted per accepted row --
        summing the field would report zero for every active session."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(
            usage,
            "2026-08-30.jsonl",
            [
                {"_type": "tokens", "slot": "a", "credits": 2.5, "turns": 0},
                {"_type": "tokens", "slot": "b", "credits": 99, "turns": 0},  # not ours
                {"_type": "other", "slot": "a", "credits": 50},  # not a tokens row
                {"_type": "tokens", "slot": "a", "credits": 1.5, "turns": 0},
            ],
        )
        out = self._run(mod, capsys, "--slots", "a", "--usage-dir", str(usage))
        assert out["slots"]["a"]["credits"] == 4.0
        assert out["slots"]["a"]["turns"] == 2  # two accepted rows = two turns
        assert out["total_credits"] == 4.0

    def test_budget_verdicts(self, tmp_path, capsys):
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(usage, "2026-08-30.jsonl", [{"_type": "tokens", "slot": "a", "credits": 40}])
        within = self._run(
            mod, capsys, "--slots", "a", "--budget", "100", "--usage-dir", str(usage)
        )
        assert within["verdict"] == "within" and within["remaining"] == 60.0
        exhausted = self._run(
            mod, capsys, "--slots", "a", "--budget", "40", "--usage-dir", str(usage)
        )
        assert exhausted["verdict"] == "exhausted"

    def test_unmetered_when_no_shard_mentions_the_slots(self, tmp_path, capsys):
        """Absent metering must read as UNKNOWN, never as zero spend: today's
        shards only carry chat-runner turns, so a session outside them burns
        invisibly and 'within budget' would be a false comfort."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(usage, "2026-08-30.jsonl", [{"_type": "tokens", "slot": "other", "credits": 1}])
        out = self._run(
            mod, capsys, "--slots", "ghost", "--budget", "100", "--usage-dir", str(usage)
        )
        assert out["verdict"] == "unmetered"
        assert out["slots"]["ghost"]["unmetered"] == 1.0

    def test_missing_usage_dir_is_unmetered_not_a_crash(self, tmp_path, capsys):
        mod = self._mod()
        out = self._run(
            mod,
            capsys,
            "--slots",
            "a",
            "--budget",
            "5",
            "--usage-dir",
            str(tmp_path / "absent"),
        )
        assert out["verdict"] == "unmetered" and out["shards_scanned"] == 0

    def test_max_shards_keeps_newest(self, tmp_path, capsys):
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(usage, "2026-08-01.jsonl", [{"_type": "tokens", "slot": "a", "credits": 7}])
        self._shard(usage, "2026-08-30.jsonl", [{"_type": "tokens", "slot": "a", "credits": 3}])
        out = self._run(mod, capsys, "--slots", "a", "--usage-dir", str(usage), "--max-shards", "1")
        assert out["total_credits"] == 3.0  # newest shard only
        assert out["truncated"] is True  # and the omission is reported

    def test_default_scan_is_all_shards(self, tmp_path, capsys):
        """The cost bound is opt-in: with no --max-shards every retained shard
        counts, so old spend can never silently vanish from the total."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(usage, "2026-08-01.jsonl", [{"_type": "tokens", "slot": "a", "credits": 7}])
        self._shard(usage, "2026-08-30.jsonl", [{"_type": "tokens", "slot": "a", "credits": 3}])
        out = self._run(mod, capsys, "--slots", "a", "--usage-dir", str(usage))
        assert out["total_credits"] == 10.0 and out["truncated"] is False

    def test_truncated_scan_never_reports_within(self, tmp_path, capsys):
        """Under budget on a partial view is not a verdict: spend older than
        the window could flip it, so the answer is `truncated` — while
        exhaustion is monotone and stands even when truncated."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(usage, "2026-08-01.jsonl", [{"_type": "tokens", "slot": "a", "credits": 90}])
        self._shard(usage, "2026-08-30.jsonl", [{"_type": "tokens", "slot": "a", "credits": 5}])
        partial = self._run(
            mod,
            capsys,
            "--slots",
            "a",
            "--budget",
            "50",
            "--usage-dir",
            str(usage),
            "--max-shards",
            "1",
        )
        assert partial["verdict"] == "truncated"  # 5 < 50 but 90 was skipped
        full = self._run(mod, capsys, "--slots", "a", "--budget", "50", "--usage-dir", str(usage))
        assert full["verdict"] == "exhausted"  # the real answer
        over = self._run(
            mod,
            capsys,
            "--slots",
            "a",
            "--budget",
            "4",
            "--usage-dir",
            str(usage),
            "--max-shards",
            "1",
        )
        assert over["verdict"] == "exhausted"  # monotone: valid even truncated

    def test_unreadable_shard_never_yields_a_complete_within(self, tmp_path, capsys):
        """A shard that cannot be read makes the total incomplete: readable
        spend still counts (exhaustion is monotone), but an under-budget
        answer degrades to `truncated`, never a false `within`."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(usage, "2026-08-29.jsonl", [{"_type": "tokens", "slot": "a", "credits": 5}])
        unreadable = usage / "2026-08-30.jsonl"
        unreadable.mkdir()  # a directory named like a shard -> OSError on read
        out = self._run(mod, capsys, "--slots", "a", "--budget", "50", "--usage-dir", str(usage))
        assert out["verdict"] == "truncated"
        assert out["total_credits"] == 5.0

    def test_torn_row_never_yields_a_complete_within(self, tmp_path, capsys):
        """An interrupted append leaves a torn row: readable spend still counts,
        but the verdict degrades to `truncated` -- a blank line, by contrast,
        is not corruption and costs nothing."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        usage.mkdir(parents=True)
        (usage / "2026-08-29.jsonl").write_text(
            json.dumps({"_type": "tokens", "slot": "a", "credits": 5})
            + "\n\n"  # blank line: benign
            + '{"_type": "tokens", "slot": "a", "cred',  # torn mid-write
            encoding="utf-8",
        )
        out = self._run(mod, capsys, "--slots", "a", "--budget", "50", "--usage-dir", str(usage))
        assert out["verdict"] == "truncated"
        assert out["total_credits"] == 5.0

    def test_mixed_metering_is_unmetered_not_within(self, tmp_path, capsys):
        """One metered slot under budget + one slot with no rows at all: the
        under-budget answer is unknowable, so the verdict is `unmetered` --
        never `within`. Exhaustion still wins when the metered spend alone
        crosses the budget (monotone)."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(usage, "2026-08-29.jsonl", [{"_type": "tokens", "slot": "a", "credits": 5}])
        out = self._run(
            mod, capsys, "--slots", "a,ghost", "--budget", "50", "--usage-dir", str(usage)
        )
        assert out["verdict"] == "unmetered"
        out = self._run(
            mod, capsys, "--slots", "a,ghost", "--budget", "4", "--usage-dir", str(usage)
        )
        assert out["verdict"] == "exhausted"  # metered spend alone crosses it

    def test_invalid_credits_on_matched_row_degrades_verdict(self, tmp_path, capsys):
        """A matched tokens row with NaN/negative/boolean credits is corrupt:
        its spend is unknown, so a complete `within` may not be claimed."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(
            usage,
            "2026-08-29.jsonl",
            [
                {"_type": "tokens", "slot": "a", "credits": 5},
                {"_type": "tokens", "slot": "a", "credits": float("nan")},
            ],
        )
        out = self._run(mod, capsys, "--slots", "a", "--budget", "50", "--usage-dir", str(usage))
        assert out["verdict"] == "truncated"
        assert out["total_credits"] == 5.0

    def test_nonfinite_or_negative_budget_is_malformed(self, tmp_path, capsys):
        """`--budget nan` would compare false against everything and print
        `within`; non-finite and non-positive budgets are malformed input."""
        mod = self._mod()
        usage = tmp_path / "tokens"
        self._shard(usage, "2026-08-29.jsonl", [{"_type": "tokens", "slot": "a", "credits": 5}])
        for bad in ("nan", "inf", "-10", "0"):
            rc = mod.main(["--slots", "a", "--budget", bad, "--usage-dir", str(usage)])
            assert rc == 2, bad
            capsys.readouterr()

    def test_no_slots_exits_2(self, tmp_path):
        mod = self._mod()
        assert mod.main(["--slots", " , "]) == 2
