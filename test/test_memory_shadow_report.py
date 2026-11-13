"""``kirocrew memory shadow report``: reads every shadow source, writes nothing.

Fixtures stand in for what the stages already record: receipt shards and
``stage-log.json`` under ``usage/injections``, and ``memory_meta`` counter rows in
the default store's file. The command is run through ``_memory_cmd`` so config
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


def test_receipt_stage_reads_the_stage_log_and_sampled_lines(home: Path, capsys) -> None:
    _config(home, rule_authority_order="shadow")
    lines = [
        _receipt(
            DAY,
            "dashboard:a",
            rule_authority_order={
                "mode": "shadow",
                "policy": 1,
                "would_add": ["lesson.b"],
                "would_drop": [],
            },
        ),
        _receipt(
            "2026-11-02",
            "dashboard:b",
            rule_authority_order={"mode": "shadow", "policy": 1, "would_add": [], "would_drop": []},
        ),
        _receipt("2026-11-02", "dashboard:b", phase="per_turn"),
    ]
    stage_log = {
        "counts": {
            DAY: {
                "rule_authority_order": {"shadow": {"lines": 3, "would_add": 2, "would_drop": 1}}
            },
            "2026-11-02": {"rule_authority_order": {"shadow": {"lines": 1}}},
        },
        "stages": {"rule_authority_order": {"policy": 1, "started_at": f"{DAY}T00:00:00+00:00"}},
    }
    _write_receipts(home, lines, stage_log)
    card = _card(_run(capsys, json=True), "rule_authority_order")
    assert card["mode"] == "shadow"
    assert card["policy"] == 1
    assert card["window_start"].startswith(DAY)
    assert (card["days"], card["sessions"], card["base"], card["changed"]) == (2, 2, 2, 1)
    assert card["rate"] == 0.5
    assert card["detail"] == {"lines": 4, "would_add": 2, "would_drop": 1}
    assert card["status"] == msr.STATUS_WAITING


def test_lesson_fold_pairs_count_as_a_change(home: Path, capsys) -> None:
    _config(home, lesson_fold="shadow")
    fold = {
        "mode": "shadow",
        "policy": 1,
        "would_add": [],
        "would_drop": [],
        "pairs": [["lesson.a", "lesson.b"]],
    }
    _write_receipts(home, [_receipt(DAY, "dashboard:a", lesson_fold=fold)])
    card = _card(_run(capsys, json=True), "lesson_fold")
    assert (card["base"], card["changed"]) == (1, 1)


@pytest.mark.parametrize(
    "stage, counts, base, changed",
    [
        (
            "kind_routing",
            {"shadow": {"fact": 3, "event": 1, "state": 4}, "sibling_suffix": {"shadow": 9}},
            8,
            4,
        ),
        (
            "lesson_origin_gate",
            {"shadow": {"learn_add": {"passes": 2, "lessons": 5, "demoted": 2}}},
            5,
            2,
        ),
        ("lesson_tier_policy", {"shadow": {"consolidation": {"lessons": 10, "demoted": 7}}}, 10, 7),
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
    if stage == "kind_routing":
        rows[prefix + "rules_changed_at"] = f"{DAY}T00:00:00+00:00"
    _write_meta(rows)
    _write_receipts(home, [_receipt(DAY, f"dashboard:{i}") for i in range(3)])
    card = _card(_run(capsys, json=True), stage)
    assert (card["base"], card["changed"], card["days"], card["sessions"]) == (base, changed, 1, 3)
    assert card["policy"] == (None if stage == "dream" else 2)
    assert card["status"] == msr.STATUS_WAITING
    if stage == "dream":
        assert card["rate"] is None
        assert card["detail"] == {**counts, "proposals": 5}
        out = _run(capsys, stage=stage)
        row = next(line for line in out.splitlines() if line.startswith("dream "))
        assert row.split()[6:10] == ["200", "-", "-", "waiting"]
    else:
        assert card["rate"] == round(changed / base, 4)


@pytest.mark.parametrize("other_mode", ["enforce", "propose", "off"])
def test_only_shadow_counters_and_receipt_fields_count(home: Path, capsys, other_mode) -> None:
    _config(home, kind_routing="shadow", rule_authority_order="shadow")
    _write_meta(
        {
            "memory_kind_log:counts:" + DAY: {other_mode: {"state": 9}},
            "memory_kind_log:counts:2026-11-02": {
                "shadow": {"fact": 2, "state": 1},
                other_mode: {"state": 9},
            },
        }
    )
    _write_receipts(
        home,
        [
            _receipt(
                "2026-11-02",
                "dashboard:a",
                rule_authority_order={"mode": other_mode, "would_add": ["lesson.b"]},
            ),
            _receipt(
                "2026-11-02",
                "dashboard:b",
                rule_authority_order={"mode": "shadow", "would_add": []},
            ),
        ],
        {
            "counts": {
                DAY: {"rule_authority_order": {other_mode: {"lines": 9}}},
                "2026-11-02": {
                    "rule_authority_order": {
                        "shadow": {"lines": 1},
                        other_mode: {"lines": 9, "would_add": 9},
                    }
                },
            }
        },
    )
    out = _run(capsys, json=True)
    kind = _card(out, "kind_routing")
    assert (kind["days"], kind["base"], kind["changed"]) == (1, 3, 1)
    receipt = _card(out, "rule_authority_order")
    assert (receipt["days"], receipt["base"], receipt["changed"]) == (1, 1, 0)
    assert receipt["detail"] == {"lines": 1, "would_add": 0, "would_drop": 0}


@pytest.mark.parametrize("stage", ["kind_routing", "rule_authority_order"])
@pytest.mark.parametrize(
    "started, first_day",
    [
        ("2026-11-01T09:00:00+00:00", "2026-11-02"),
        ("2026-11-01T00:00:00+00:00", DAY),
        ("2026-11-01T00:00:00.000001+00:00", "2026-11-02"),
        ("2026-11-01T19:00:00-05:00", "2026-11-02"),
    ],
)
def test_window_uses_full_utc_days(home: Path, capsys, stage, started, first_day) -> None:
    _config(home, **{stage: "shadow"})
    days = [DAY, "2026-11-02"]
    _write_meta(
        {
            "memory_kind_log:rules_changed_at": started,
            **{f"memory_kind_log:counts:{day}": {"shadow": {"state": 2}} for day in days},
        }
    )
    _write_receipts(
        home,
        [
            _receipt(
                day,
                f"dashboard:{day}",
                rule_authority_order={"mode": "shadow", "would_add": ["lesson.b"]},
            )
            for day in days
        ],
        {
            "stages": {"rule_authority_order": {"started_at": started, "policy": 2}},
            "counts": {
                day: {"rule_authority_order": {"shadow": {"lines": 2, "would_add": 2}}}
                for day in days
            },
        },
    )
    card = _card(_run(capsys, json=True), stage)
    included = [day for day in days if day >= first_day]
    assert card["window_start"] == first_day + "T00:00:00+00:00"
    assert list(card["by_day"]) == included
    assert card["days"] == card["sessions"] == len(included)
    base = len(included) * (2 if stage == "kind_routing" else 1)
    assert (card["base"], card["changed"], card["rate"]) == (base, base, 1.0)
    if stage == "rule_authority_order":
        assert card["detail"] == {
            "lines": len(included) * 2,
            "would_add": len(included) * 2,
            "would_drop": 0,
        }


_DAYS = (DAY, "2026-11-02", "2026-11-03")


@pytest.mark.parametrize(
    "stage, counts",
    [
        ("lesson_origin_gate", {"shadow": {"learn_add": {"lessons": 2, "demoted": 1}}}),
        ("lesson_tier_policy", {"shadow": {"consolidation": {"lessons": 2, "demoted": 1}}}),
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


def test_kind_routing_window_starts_after_its_rules_change(home: Path, capsys) -> None:
    _config(home, kind_routing="shadow")
    _write_meta(
        {
            "memory_kind_log:rules_changed_at": "2026-11-02T09:00:00+00:00",
            "memory_kind_log:shadow_started_at": "2026-11-02T09:00:00+00:00",
            **{f"memory_kind_log:counts:{day}": {"shadow": {"state": 2}} for day in _DAYS},
        }
    )
    card = _card(_run(capsys, json=True), "kind_routing")
    assert card["window_start"] == "2026-11-03T00:00:00+00:00"
    assert list(card["by_day"]) == ["2026-11-03"]
    assert (card["days"], card["base"], card["changed"]) == (1, 2, 2)


def test_off_mode_counts_are_not_shadow_data(home: Path, capsys) -> None:
    _config(home, kind_routing="shadow")
    _write_meta({"memory_kind_log:counts:" + DAY: {"off": {"fact": 5}}})
    card = _card(_run(capsys, json=True), "kind_routing")
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
    _config(home, kind_routing="shadow")
    _write_meta({"memory_kind_log:counts:" + DAY: {"shadow": {"fact": 1}}})
    restarted = _receipt(DAY, "dashboard:a")
    restarted["ts"] = f"{DAY}T12:00:00.000Z"
    _write_receipts(
        home,
        [_receipt(DAY, "dashboard:a"), restarted, _receipt(DAY, "dashboard:a", phase="per_turn")],
    )
    assert _card(_run(capsys, json=True), "kind_routing")["sessions"] == 2


def test_meta_prefixes_match_the_owning_modules() -> None:
    owners = {
        "kind_routing": "memory_kind_routing",
        "lesson_origin_gate": "memory_origin_gate",
        "lesson_tier_policy": "memory_tier_policy",
        "member_lesson_dedup": "memory_lesson_dedup",
        "dream": "memory_dream",
    }
    sources = {s.name: s.source for s in msr.STAGES if s.source != msr.SOURCE_RECEIPTS}
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
    assert all(c["data"] is False and c["status"] == "off" for c in data["stages"])


def test_unreadable_sources_are_no_data(home: Path, capsys) -> None:
    _config(home, relevance_rule="shadow")
    folder = _write_receipts(home, [])
    (folder / "stage-log.json").write_text("{not json", encoding="utf-8")
    (folder / f"{DAY}.jsonl").write_text("garbage\n[1]\n", encoding="utf-8")
    path = owned_store_path("default")
    assert path is not None
    path.write_text("not a database", encoding="utf-8")
    assert _card(_run(capsys, json=True), "relevance_rule")["data"] is False


def test_json_shape(home: Path, capsys) -> None:
    data = json.loads(_run(capsys, json=True))
    assert set(data) == {"store", "since", "bar", "stages"}
    assert data["bar"] == {"days": 14, "sessions": 200}
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
        "status",
    }


def test_stage_filter_prints_counts_by_day(home: Path, capsys) -> None:
    _config(home, kind_routing="shadow")
    _write_meta({"memory_kind_log:counts:" + DAY: {"shadow": {"fact": 1, "state": 2}}})
    out = _run(capsys, stage="kind_routing")
    assert "lesson_fold" not in out
    assert f"  {DAY}  fact=1, state=2" in out


def test_since_moves_the_window_start(home: Path, capsys) -> None:
    _config(home, kind_routing="shadow")
    _write_meta(
        {
            "memory_kind_log:counts:" + DAY: {"shadow": {"state": 2}},
            "memory_kind_log:counts:2026-11-05": {"shadow": {"fact": 1}},
        }
    )
    card = _card(_run(capsys, json=True, since="2026-11-03"), "kind_routing")
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
    """Sample rows and the Dreaming list may hold text; the report never selects them."""
    _config(home, kind_routing="shadow", dream="propose")
    _write_meta(
        {
            "memory_kind_log:counts:" + DAY: {"shadow": {"state": 1}},
            "memory_kind_log:samples:" + DAY: [{"key": "project.x", "value": SECRET}],
            "memory_dream:proposals": [{"keys": ["lesson.a"], "text": SECRET}],
            "memory_dream:counts:" + DAY: {"runs": 1, "rows": 2},
        }
    )
    assert SECRET not in _run(capsys) + _run(capsys, json=True)
    meta = msr.read_meta(owned_store_path("default"))
    assert all(":samples:" not in k and not k.endswith(":proposals") for k in meta)


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    return {
        str(p.relative_to(root)): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_the_report_changes_no_file_under_the_data_home(home: Path, capsys) -> None:
    _config(home, rule_authority_order="shadow", kind_routing="shadow")
    _write_receipts(
        home,
        [
            _receipt(
                DAY,
                "dashboard:a",
                rule_authority_order={
                    "mode": "shadow",
                    "policy": 1,
                    "would_add": [],
                    "would_drop": [],
                },
            )
        ],
        {"counts": {}, "stages": {}},
    )
    _write_meta({"memory_kind_log:counts:" + DAY: {"shadow": {"fact": 1}}})
    # A first load may settle the config file; the report is measured after it.
    _run(capsys)
    before = _snapshot(home)
    _run(capsys)
    _run(capsys, json=True)
    _run(capsys, stage="kind_routing", since="30d")
    assert _snapshot(home) == before


def test_parser_accepts_every_stage_and_no_other() -> None:
    from kiro_crew.cli_parser_agent import register_memory_parser

    parser = argparse.ArgumentParser()
    register_memory_parser(parser.add_subparsers(dest="command"))
    ns = parser.parse_args(["memory", "shadow", "report", "--stage", "lesson_fold", "--json"])
    assert (ns.mem_action, ns.shadow_action, ns.stage, ns.json) == (
        "shadow",
        "report",
        "lesson_fold",
        True,
    )
    for stage in msr.STAGE_NAMES:
        assert parser.parse_args(["memory", "shadow", "report", "--stage", stage]).stage == stage
    with pytest.raises(SystemExit):
        parser.parse_args(["memory", "shadow", "report", "--stage", "lessons"])


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
