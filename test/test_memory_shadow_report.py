"""``kirocrew memory shadow report``: reads every shadow source, writes nothing.

Fixtures stand in for what the stages already record: receipt shards under
``usage/injections``, and ``memory_meta`` counter rows in the default store's file. The command is run through ``_memory_cmd`` so config
loading and store admission are covered too.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from kiro_crew import cli_commands_memory as cm
from kiro_crew import memory_shadow_report as msr
from kiro_crew.memory_stores import owned_store_path

SECRET = "the person's private rule text"
DAY = "2026-11-01"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


def _config(home: Path, **flags: str) -> None:
    (home / "config.json").write_text(json.dumps({"memory": flags}), encoding="utf-8")


def _receipt(day: str, session: str, *, phase: str = "session_start", **stages) -> dict:
    return {
        "ts": f"{day}T10:00:00.000Z",
        "session_key": session,
        "turn_index": 1,
        "phase": phase,
        "memory_store": "default",
        "lessons": ["lesson.aaa"],
        **stages,
    }


def _write_receipts(home: Path, lines: list[dict], stage_log: dict | None = None) -> Path:
    folder = home / "usage" / "injections"
    folder.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[str]] = {}
    for line in lines:
        by_day.setdefault(line["ts"][:10], []).append(json.dumps(line))
    for day, rows in by_day.items():
        (folder / f"{day}.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    if stage_log is not None:
        (folder / "stage-log.json").write_text(json.dumps(stage_log), encoding="utf-8")
    return folder


def _write_meta(rows: dict[str, object]) -> Path:
    path = owned_store_path("default")
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memory_meta (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    for key, value in rows.items():
        text = value if isinstance(value, str) else json.dumps(value)
        conn.execute("INSERT INTO memory_meta VALUES (?, ?, ?)", (key, text, "x"))
    conn.commit()
    conn.close()
    return path


def _run(capsys, **kw) -> str:
    ns = argparse.Namespace(
        mem_action="shadow", shadow_action="report", stage=None, since=None, store=None, json=False
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    cm._memory_cmd(ns)
    return capsys.readouterr().out


def _card(out: str, stage: str) -> dict:
    return next(c for c in json.loads(out)["stages"] if c["stage"] == stage)


def origin_counts(lessons: int, demoted: int) -> dict:
    """One day of ``memory.lesson_origin_gate`` shadow counters."""
    return {"shadow": {"learn_add": {"lessons": lessons, "demoted": demoted}}}


@pytest.mark.parametrize(
    "stage, counts, base, changed",
    [
        (
            "lesson_origin_gate",
            {"shadow": {"learn_add": {"passes": 2, "lessons": 5, "demoted": 2}}},
            5,
            2,
        ),
        ("lesson_tier_policy", {"shadow": {"consolidation": {"lessons": 10, "demoted": 7}}}, 10, 7),
        (
            "fact_origin_gate",
            {"shadow": {"pref": {"facts": 3, "gated": 2}, "user": {"facts": 1, "gated": 1}}},
            4,
            3,
        ),
        (
            "member_lesson_dedup",
            {"shadow": {"lessons": 6, "text_folds": 1, "checked": 4, "vector_folds": 2}},
            10,
            3,
        ),
        ("member_lesson_dedup", {"shadow": {"checked": 5, "vector_folds": 3}}, 5, 3),
        (
            "dream",
            {
                "runs": 1,
                "rows": 200,
                "proposed_exact_copy": 3,
                "proposed_numbered_family": 2,
                "refused_not_live": 1,
            },
            200,
            None,
        ),
    ],
)
def test_each_meta_family_is_parsed(home, capsys, stage, counts, base, changed) -> None:
    mode = "propose" if stage == "dream" else "shadow"
    if stage != "dream":
        counts = {**counts, "enforce": counts["shadow"], "propose": counts["shadow"]}
    _config(home, **{stage: mode})
    prefix = next(s.source for s in msr.STAGES if s.name == stage)
    rows: dict[str, object] = {prefix + "counts:" + DAY: counts}
    if stage != "dream":
        rows[prefix + "rules_version"] = "2"
    _write_meta(rows)
    _write_receipts(home, [_receipt(DAY, f"dashboard:{i}") for i in range(3)])
    card = _card(_run(capsys, json=True), stage)
    assert (card["base"], card["changed"], card["days"], card["sessions"]) == (base, changed, 1, 3)
    assert card["policy"] == (None if stage == "dream" else 2)
    if stage != "lesson_tier_policy":
        assert card["quotes"] is None
    assert card["status"] == msr.STATUS_WAITING
    if stage == "dream":
        assert card["rate"] is None
        assert card["detail"] == {**counts, "proposals": 5}
        out = _run(capsys, stage=stage)
        row = next(line for line in out.splitlines() if line.startswith("dream "))
        assert row.split()[6:12] == ["200", "-", "-", "-", "-", "wait"]
    else:
        assert card["rate"] == round(changed / base, 4)


@pytest.mark.parametrize("other_mode", ["enforce", "propose", "off"])
def test_only_shadow_counters_count(home: Path, capsys, other_mode) -> None:
    _config(home, lesson_origin_gate="shadow")
    _write_meta(
        {
            "memory_origin_log:counts:" + DAY: {other_mode: {"learn_add": {"lessons": 9}}},
            "memory_origin_log:counts:2026-11-02": {
                "shadow": {"learn_add": {"lessons": 3, "demoted": 1}},
                other_mode: {"learn_add": {"lessons": 9, "demoted": 9}},
            },
        }
    )
    gate = _card(_run(capsys, json=True), "lesson_origin_gate")
    assert (gate["days"], gate["base"], gate["changed"]) == (1, 3, 1)


@pytest.mark.parametrize(
    "started, first_day",
    [
        ("2026-11-01T09:00:00+00:00", "2026-11-02"),
        ("2026-11-01T00:00:00+00:00", DAY),
        ("2026-11-01T00:00:00.000001+00:00", "2026-11-02"),
        ("2026-11-01T19:00:00-05:00", "2026-11-02"),
    ],
)
def test_window_uses_full_utc_days(home: Path, capsys, started, first_day) -> None:
    _config(home, lesson_origin_gate="shadow")
    days = [DAY, "2026-11-02"]
    _write_meta(
        {
            "memory_origin_log:rules_changed_at": started,
            **{
                f"memory_origin_log:counts:{day}": {
                    "shadow": {"learn_add": {"lessons": 2, "demoted": 2}}
                }
                for day in days
            },
        }
    )
    _write_receipts(home, [_receipt(day, f"dashboard:{day}") for day in days])
    card = _card(_run(capsys, json=True), "lesson_origin_gate")
    included = [day for day in days if day >= first_day]
    assert card["window_start"] == first_day + "T00:00:00+00:00"
    assert list(card["by_day"]) == included
    assert card["days"] == card["sessions"] == len(included)
    base = len(included) * 2
    assert (card["base"], card["changed"], card["rate"]) == (base, base, 1.0)


@pytest.mark.parametrize("policy", [2, 3])
def test_a_lesson_stage_reports_its_quotes_from_the_new_rules_only(
    home: Path, capsys, policy
) -> None:
    stage = "lesson_tier_policy"
    _config(home, **{stage: "shadow"})
    prefix = next(s.source for s in msr.STAGES if s.name == stage)
    new = {
        "lessons": 6,
        "always": 4,
        "quote_present": 3,
        "quote_pass": 2,
        "quote_fail": 1,
        "backed": 1,
        "unmeasured": 0,
        "demoted": 4,
        "support_0.8": 1,
        "support_<0.4": 1,
        "quote_calls": 2,
        "quote_call_failed": 0,
        "quote_cut": 1,
    }
    _write_meta(
        {
            prefix + "rules_version": str(policy),
            prefix + "rules_changed_at": "2026-11-01T09:00:00+00:00",
            # The day the rules changed mixes both rule sets: left out.
            prefix + f"counts:{DAY}": {"shadow": {"consolidation": {"lessons": 9, "demoted": 9}}},
            prefix + "counts:2026-11-02": {"shadow": {"consolidation": new}},
        }
    )
    card = _card(_run(capsys, json=True), stage)
    assert (card["policy"], card["base"], card["changed"]) == (policy, 6, 4)
    assert card["bias"] == ""
    assert card["quotes"] == {
        "absent": 1,
        "fail": 1,
        "pass": 2,
        "backed": 1,
        "unmeasured": 0,
        "support": {"0.8": 1, "<0.4": 1},
        "calls": 2,
        "call_failed": 0,
        "cut": 1,
    }
    text = _run(capsys)
    assert "upper bound" not in text
    assert (
        f"quotes {stage}: absent=1 fail=1 pass=2 (backed=1) unmeasured=0  "
        "support 0.8=1 <0.4=1  quote calls=2 failed=0 cut=1"
    ) in text


@pytest.mark.parametrize("policy", [None, "1"])
def test_legacy_tier_counters_keep_the_upper_bound_note(home: Path, capsys, policy) -> None:
    _config(home, lesson_tier_policy="shadow")
    rows: dict[str, object] = {
        "memory_tier_log:counts:"
        + DAY: {"shadow": {"consolidation": {"lessons": 2, "always": 2, "demoted": 2}}},
    }
    if policy is not None:
        rows["memory_tier_log:rules_version"] = policy
    _write_meta(rows)
    card = _card(_run(capsys, json=True), "lesson_tier_policy")
    assert card["policy"] == (None if policy is None else 1)
    assert (card["base"], card["changed"], card["rate"]) == (2, 2, 1.0)
    assert card["quotes"] is None
    assert "upper bound" in card["bias"]
    text = _run(capsys)
    assert "note lesson_tier_policy: upper bound" in text
    assert "quotes lesson_tier_policy" not in text


def test_origin_gate_never_reports_quotes_even_under_version_two(home: Path, capsys) -> None:
    _config(home, lesson_origin_gate="shadow")
    _write_meta(
        {
            "memory_origin_log:rules_version": "2",
            "memory_origin_log:counts:"
            + DAY: {
                "shadow": {
                    "consolidation": {
                        "passes": 1,
                        "no_human": 1,
                        "lessons": 2,
                        "standing": 2,
                        "demoted": 2,
                    }
                }
            },
        }
    )
    card = _card(_run(capsys, json=True), "lesson_origin_gate")
    assert card["policy"] == 2
    assert card["quotes"] is None
    assert card["bias"] == ""
    assert "quotes lesson_origin_gate" not in _run(capsys)


_DAYS = (DAY, "2026-11-02", "2026-11-03")


@pytest.mark.parametrize(
    "stage, counts",
    [
        ("lesson_origin_gate", {"shadow": {"learn_add": {"lessons": 2, "demoted": 1}}}),
        ("lesson_tier_policy", {"shadow": {"consolidation": {"lessons": 2, "demoted": 1}}}),
        ("fact_origin_gate", {"shadow": {"pref": {"facts": 2, "gated": 1}}}),
        ("member_lesson_dedup", {"shadow": {"lessons": 2, "text_folds": 1}}),
    ],
)
def test_write_window_starts_at_the_first_counter_day(home: Path, capsys, stage, counts) -> None:
    _config(home, **{stage: "shadow"})
    prefix = next(s.source for s in msr.STAGES if s.name == stage)
    _write_meta(
        {
            prefix + "shadow_started_at": "2026-11-03T10:00:00+00:00",
            **{prefix + "counts:" + day: counts for day in _DAYS},
        }
    )
    card = _card(_run(capsys, json=True), stage)
    assert card["window_start"] == DAY + "T00:00:00+00:00"
    assert list(card["by_day"]) == list(_DAYS)
    assert (card["days"], card["base"], card["changed"]) == (3, 6, 3)


def test_a_write_window_starts_after_its_rules_change(home: Path, capsys) -> None:
    _config(home, lesson_origin_gate="shadow")
    counts = {"shadow": {"learn_add": {"lessons": 2, "demoted": 2}}}
    _write_meta(
        {
            "memory_origin_log:rules_changed_at": "2026-11-02T09:00:00+00:00",
            **{f"memory_origin_log:counts:{day}": counts for day in _DAYS},
        }
    )
    card = _card(_run(capsys, json=True), "lesson_origin_gate")
    assert card["window_start"] == "2026-11-03T00:00:00+00:00"
    assert list(card["by_day"]) == ["2026-11-03"]
    assert (card["days"], card["base"], card["changed"]) == (1, 2, 2)


def test_off_mode_counts_are_not_shadow_data(home: Path, capsys) -> None:
    _config(home, lesson_origin_gate="shadow")
    _write_meta({"memory_origin_log:counts:" + DAY: {"off": {"learn_add": {"lessons": 5}}}})
    card = _card(_run(capsys, json=True), "lesson_origin_gate")
    assert card["data"] is False


@pytest.mark.parametrize(
    "stage, mode, counts",
    [
        ("lesson_tier_policy", "shadow", {"shadow": {"c": {"lessons": 1}}}),
        ("dream", "propose", {"runs": 1, "rows": 1}),
    ],
)
def test_enough_data_needs_both_bars(home: Path, capsys, stage, mode, counts) -> None:
    _config(home, **{stage: mode})
    days = [date(2026, 10, d).isoformat() for d in range(1, 15)]
    prefix = next(s.source for s in msr.STAGES if s.name == stage)
    _write_meta({f"{prefix}counts:{d}": counts for d in days})
    _write_receipts(home, [_receipt(days[i % 14], f"dashboard:{i}") for i in range(200)])
    card = _card(_run(capsys, json=True), stage)
    assert card["status"] == msr.STATUS_ENOUGH
    if stage == "dream":
        assert card["changed"] is None
        assert card["rate"] is None
        assert card["detail"]["proposals"] == 0


def test_every_session_start_line_counts_even_for_one_session_key(home: Path, capsys) -> None:
    _config(home, lesson_origin_gate="shadow")
    _write_meta({"memory_origin_log:counts:" + DAY: {"shadow": {"learn_add": {"lessons": 1}}}})
    restarted = _receipt(DAY, "dashboard:a")
    restarted["ts"] = f"{DAY}T12:00:00.000Z"
    _write_receipts(
        home,
        [_receipt(DAY, "dashboard:a"), restarted, _receipt(DAY, "dashboard:a", phase="per_turn")],
    )
    assert _card(_run(capsys, json=True), "lesson_origin_gate")["sessions"] == 2


def test_meta_prefixes_match_the_owning_modules() -> None:
    owners = {
        "lesson_origin_gate": "memory_origin_gate",
        "lesson_tier_policy": "memory_tier_policy",
        "fact_origin_gate": "memory_fact_origin_gate",
        "member_lesson_dedup": "memory_lesson_dedup",
        "dream": "memory_dream",
    }
    sources = {s.name: s.source for s in msr.STAGES}
    assert set(sources) == set(owners)
    for stage, module in owners.items():
        owner = importlib.import_module(f"kiro_crew.{module}")
        assert sources[stage] == owner._META_PREFIX, stage


def test_missing_sources_print_no_data(home: Path, capsys) -> None:
    out = _run(capsys)
    for stage in msr.STAGE_NAMES:
        line = next(row for row in out.splitlines() if row.startswith(stage + " "))
        assert line.endswith("off (no data)")
    data = json.loads(_run(capsys, json=True))
    assert all(c["data"] is False for c in data["stages"])
    assert {c["stage"]: c["status"] for c in data["stages"]} == {
        stage: "off" for stage in msr.STAGE_NAMES
    }


@pytest.mark.parametrize(
    "gone", ["kind_routing", "lesson_fold", "rule_authority_order", "relevance_rule"]
)
def test_a_stage_without_a_shadow_mode_is_not_listed(home: Path, capsys, gone) -> None:
    _config(home, **{gone: "shadow"})
    assert gone not in msr.STAGE_NAMES
    assert gone not in _run(capsys)


def test_unreadable_sources_are_no_data(home: Path, capsys) -> None:
    _config(home, lesson_origin_gate="shadow")
    folder = _write_receipts(home, [])
    (folder / f"{DAY}.jsonl").write_text("garbage\n[1]\n", encoding="utf-8")
    path = owned_store_path("default")
    assert path is not None
    path.write_text("not a database", encoding="utf-8")
    card = _card(_run(capsys, json=True), "lesson_origin_gate")
    assert card["data"] is False and card["sessions"] == 0


def test_json_shape(home: Path, capsys) -> None:
    data = json.loads(_run(capsys, json=True))
    assert set(data) == {"store", "since", "bar", "outcomes", "stages"}
    assert data["bar"] == {"days": 14, "sessions": 200, "miss": 0.01}
    assert data["outcomes"] == {"recall": 0, "learn_add": 0}
    assert [c["stage"] for c in data["stages"]] == list(msr.STAGE_NAMES)
    assert set(data["stages"][0]) == {
        "stage",
        "source",
        "mode",
        "policy",
        "window_start",
        "days",
        "sessions",
        "base",
        "changed",
        "rate",
        "detail",
        "by_day",
        "data",
        "bias",
        "quotes",
        "status",
        "miss",
        "control",
        "verdict",
        "verdict_reason",
    }


def test_stage_filter_prints_counts_by_day(home: Path, capsys) -> None:
    _config(home, lesson_origin_gate="shadow")
    _write_meta({"memory_origin_log:counts:" + DAY: origin_counts(1, 2)})
    out = _run(capsys, stage="lesson_origin_gate")
    assert "fact_origin_gate" not in out
    assert f"  {DAY}  demoted=2, lessons=1" in out


def test_since_moves_the_window_start(home: Path, capsys) -> None:
    _config(home, lesson_origin_gate="shadow")
    _write_meta(
        {
            "memory_origin_log:counts:" + DAY: origin_counts(2, 2),
            "memory_origin_log:counts:2026-11-05": origin_counts(1, 0),
        }
    )
    card = _card(_run(capsys, json=True, since="2026-11-03"), "lesson_origin_gate")
    assert (card["days"], card["base"], card["changed"]) == (1, 1, 0)


def test_a_bad_since_is_one_line_and_exit_1(home: Path, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(capsys, since="last week")
    assert exc.value.code == 1
    assert "--since" in capsys.readouterr().err


def test_parse_since() -> None:
    today = date(2026, 11, 13)
    assert msr.parse_since("7d", today=today) == datetime(2026, 11, 6, tzinfo=timezone.utc)
    assert msr.parse_since("2026-11-01") == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert msr.parse_since(None) is None
    with pytest.raises(ValueError):
        msr.parse_since("d")


def test_no_memory_text_reaches_the_report(home: Path, capsys) -> None:
    """Id rows and the Dreaming list may hold text; the report never selects it."""
    _config(home, lesson_origin_gate="shadow", dream="propose")
    _write_meta(
        {
            "memory_origin_log:counts:" + DAY: origin_counts(1, 1),
            "memory_origin_log:ids:" + DAY: [{"key": "lesson.x", "value": SECRET}],
            "memory_dream:proposals": [{"keys": ["lesson.a"], "text": SECRET}],
            "memory_dream:counts:" + DAY: {"runs": 1, "rows": 2},
        }
    )
    assert SECRET not in _run(capsys) + _run(capsys, json=True)
    meta = msr.read_meta(owned_store_path("default"))
    assert all(":ids:" not in k and not k.endswith(":proposals") for k in meta)


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    return {
        str(p.relative_to(root)): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_the_report_changes_no_file_under_the_data_home(home: Path, capsys) -> None:
    _config(home, lesson_origin_gate="shadow")
    _write_receipts(home, [_receipt(DAY, "dashboard:a")], {"counts": {}, "stages": {}})
    _write_meta({"memory_origin_log:counts:" + DAY: origin_counts(1, 0)})
    # A first load may settle the config file; the report is measured after it.
    _run(capsys)
    before = _snapshot(home)
    _run(capsys)
    _run(capsys, json=True)
    _run(capsys, stage="lesson_origin_gate", since="30d")
    assert _snapshot(home) == before


def test_parser_accepts_every_stage_and_no_other() -> None:
    from kiro_crew.cli_parser_agent import register_memory_parser

    parser = argparse.ArgumentParser()
    register_memory_parser(parser.add_subparsers(dest="command"))
    ns = parser.parse_args(
        ["memory", "shadow", "report", "--stage", "lesson_origin_gate", "--json"]
    )
    assert (ns.mem_action, ns.shadow_action, ns.stage, ns.json) == (
        "shadow",
        "report",
        "lesson_origin_gate",
        True,
    )
    for stage in msr.STAGE_NAMES:
        assert parser.parse_args(["memory", "shadow", "report", "--stage", stage]).stage == stage
    for other in (
        "lessons",
        "kind_routing",
        "lesson_fold",
        "rule_authority_order",
        "relevance_rule",
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(["memory", "shadow", "report", "--stage", other])


# The log families are written by their own stage modules and read back only by
# those modules and this report. A renderer that read them would let shadow data
# change what a prompt carries, and the shadow stages would stop being isolated.
_LOG_PREFIXES = (
    "memory_kind_log:",
    "memory_origin_log:",
    "memory_tier_log:",
    "memory_dedup_log:",
    "memory_dream:",
)
_LOG_OWNERS = {
    "memory_kind_routing.py",
    "memory_origin_gate.py",
    "memory_tier_policy.py",
    "memory_lesson_dedup.py",
    "memory_dream.py",
    "memory_shadow_report.py",
}
_LOG_MODULES = tuple(f"kiro_crew.{name[:-3]}" for name in sorted(_LOG_OWNERS))
_RENDERERS = (
    "vector_memory_runtime/lessons.py",
    "vector_memory_runtime/semantic.py",
    "memory.py",
    "context_assembly/store_admission.py",
    "context_blocks.py",
)
_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


def test_only_the_owning_modules_name_a_log_prefix() -> None:
    naming = {
        str(p.relative_to(_SRC))
        for p in _SRC.rglob("*.py")
        if any(
            prefix in p.read_text(encoding="utf-8", errors="replace") for prefix in _LOG_PREFIXES
        )
    }
    assert naming <= _LOG_OWNERS


@pytest.mark.parametrize("renderer", _RENDERERS)
def test_no_renderer_reads_memory_meta_or_a_log_module(renderer: str) -> None:
    text = (_SRC / renderer).read_text(encoding="utf-8")
    assert "memory_meta" not in text
    for module in _LOG_MODULES:
        short = module.rsplit(".", 1)[1]
        assert f"import {short}" not in text and f"{module} import" not in text, module
