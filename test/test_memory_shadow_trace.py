"""The shadow trace: session tags, control samples, outcome receipts and the scorecard.

Every piece is additive. These tests pin that it writes ids, tags and counts only,
writes nothing while every stage flag is off or for an incognito session, keeps
within its caps, and that the report joins drops with later outcomes the way its
docstring says.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import cli_commands_memory as cm
from kiro_crew import injection_receipts as ir
from kiro_crew import memory_kind_routing as mkr
from kiro_crew import memory_origin_gate as mog
from kiro_crew import memory_shadow_report as msr
from kiro_crew import memory_shadow_trace as trace
from kiro_crew.memory_stores import owned_store_path
from kiro_crew.vector_memory import VectorMemoryStore

SECRET = "the person's private rule text"
QUERY = "where does the signer deploy"
SESSION = "dashboard:chat-trace"
_T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


def _config(home: Path, **flags: str) -> None:
    (home / "config.json").write_text(json.dumps({"memory": flags}), encoding="utf-8")


def _shards(home: Path) -> list[dict]:
    folder = home / "usage" / "injections"
    return [
        json.loads(raw)
        for path in sorted(folder.glob("*.jsonl"))
        for raw in path.read_text(encoding="utf-8").splitlines()
    ]


# ── The shared pieces ──────────────────────────────────────────────────────


def test_a_session_tag_is_a_short_hash_and_never_the_key() -> None:
    tag = trace.session_tag(SESSION)
    assert len(tag) == trace.TAG_CHARS and int(tag, 16) >= 0
    assert tag == trace.session_tag(SESSION) != trace.session_tag(SESSION + "x")
    assert trace.session_tag("") == "" and trace.session_tag(None) == ""
    assert trace.stamp("", _T0) == {"at": _T0.isoformat()}
    assert trace.stamp(SESSION, _T0) == {"at": _T0.isoformat(), "sk": tag}


def test_the_control_sample_is_a_stable_capped_subset_of_kept_keys() -> None:
    kept = [f"lesson.{i:04d}" for i in range(2_000)]
    picked = trace.control_keys(SESSION, kept)
    assert len(picked) == trace.CONTROL_PER_ENTRY
    assert set(picked) <= set(kept)
    assert picked == trace.control_keys(SESSION, kept), "a re-render picks the same rows"
    share = sum(trace.in_control(trace.session_tag(SESSION), k) for k in kept) / len(kept)
    assert 0.05 < share < 0.15


def test_observing_is_false_only_when_every_stage_is_off() -> None:
    off = SimpleNamespace()
    assert trace.observing(off) is False
    for stage in msr.STAGES:
        mode = "propose" if stage.name == "dream" else "shadow"
        assert trace.observing(SimpleNamespace(**{stage.name: mode})) is True, stage.name


def test_recall_ids_reads_keys_only_and_caps_them() -> None:
    facts = [{"key": f"project.k{i}", "snippet": SECRET} for i in range(30)]
    payload = {"retrieval": {"facts": facts + [{"id": "markdown:x"}], "episodes": []}}
    ids = trace.recall_ids(payload)
    assert ids == [f"project.k{i}" for i in range(trace.OUTCOME_IDS_CAP)]
    assert trace.recall_ids({}) == [] and trace.recall_ids(None) == []


def test_an_outcome_line_carries_a_tag_and_keys_and_no_text() -> None:
    line = ir.build_outcome_line(
        ir.RECALL, session_key=SESSION, store="mate", ids=["project.a", "project.a", 7], now=_T0
    )
    assert line == {
        "ts": "2026-11-01T12:00:00.000Z",
        "sk": trace.session_tag(SESSION),
        "phase": "recall",
        "memory_store": "mate",
        "recall": {"mode": "trace", "policy": 1, "ids": ["project.a"]},
    }
    assert ir.build_outcome_line(ir.LEARN_ADD, session_key=SESSION, store="", ids=[]) is None
    with pytest.raises(ValueError):
        ir.build_outcome_line("lessons", session_key=SESSION, store="", ids=["x"])


def test_outcome_lines_have_their_own_daily_budget(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(mkr, "SAMPLES_PER_DAY", 2)
    for _ in range(3):
        ir.write_line(
            ir.build_outcome_line(
                ir.RECALL, session_key=SESSION, store="", ids=["project.a"], now=_T0
            )
        )
    ir.write_line(
        ir.build_outcome_line(
            ir.LEARN_ADD, session_key=SESSION, store="", ids=["lesson.a"], now=_T0
        )
    )
    phases = [line["phase"] for line in _shards(home)]
    assert phases == ["recall", "recall", "learn_add"], "a capped outcome line is not written"
    log = json.loads((home / "usage" / "injections" / "stage-log.json").read_text())
    assert log["counts"]["2026-11-01"]["recall"]["trace"] == {"lines": 3, "ids": 3}


def test_a_stage_field_names_a_control_sample_of_the_rows_it_keeps() -> None:
    lessons = [f"lesson.{i:03d}" for i in range(200)]
    receipts = {
        ir.LESSONS: {"ids": lessons, "omitted": 0},
        ir.RULE_AUTHORITY_ORDER: ir._stage_entry("shadow", 1, [], lessons[:100]),
    }
    line = ir.build_line(
        receipts,
        session_key=SESSION,
        turn_index=1,
        user_msg_id="",
        model="",
        agent="",
        phase="session_start",
        store="",
        now=_T0,
    )
    assert line is not None
    control = line[ir.RULE_AUTHORITY_ORDER]["control"]
    assert control and set(control) <= set(lessons[100:]), "only rows the stage keeps"
    assert control == trace.control_keys(SESSION, lessons[100:])


def test_kept_task_facts_feed_the_relevance_control_and_never_reach_the_line() -> None:
    facts = [f"project.f{i:03d}" for i in range(200)]
    with ir.collect() as sink:
        ir.record_stage(
            ir.RELEVANCE_RULE,
            mode="shadow",
            policy=1,
            would_drop=facts[:10],
            part="facts",
            kept=facts[10:],
        )
        ir.record_stage(ir.RELEVANCE_RULE, mode="shadow", policy=1, part="findings")
    line = ir.build_line(
        sink,
        session_key=SESSION,
        turn_index=1,
        user_msg_id="",
        model="",
        agent="",
        phase="session_start",
        store="",
        now=_T0,
    )
    assert line is not None
    field = line[ir.RELEVANCE_RULE]
    assert "kept" not in field and "parts" not in field
    assert field["control"] == trace.control_keys(SESSION, facts[10:])
    assert field["control"]
    replayed: dict = {}
    with ir.collect() as again:
        ir.replay(json.loads(json.dumps(sink)))
        replayed = dict(again)
    assert replayed[ir.RELEVANCE_RULE]["kept"] == sorted(facts[10:])


def test_recalled_lessons_cross_the_recall_child_boundary_but_never_a_line() -> None:
    with ir.collect() as child:
        ir.note_recalled_lessons([({"key": "lesson.a"}, "text"), ({"key": "lesson.b"}, "t")])
    frame = json.loads(json.dumps(child))
    with ir.collect() as parent:
        ir.replay(frame)
    assert parent[ir.RECALLED_LESSONS] == {"ids": ["lesson.a", "lesson.b"]}
    assert ir.RECALLED_LESSONS not in ir.KINDS
    line = ir.build_line(
        parent,
        session_key=SESSION,
        turn_index=1,
        user_msg_id="",
        model="",
        agent="",
        phase="session_start",
        store="",
        now=_T0,
    )
    assert line is None, "a recall's lesson names alone write no receipt line"


def test_a_line_without_a_stage_names_no_control() -> None:
    receipts = {ir.LESSONS: {"ids": [f"lesson.{i}" for i in range(100)], "omitted": 0}}
    line = ir.build_line(
        receipts,
        session_key=SESSION,
        turn_index=1,
        user_msg_id="",
        model="",
        agent="",
        phase="session_start",
        store="",
        now=_T0,
    )
    assert line is not None and "control" not in json.dumps(line)


# ── Write stages: tag, time and control rows ───────────────────────────────


@pytest.fixture
def store(tmp_path: Path, opened):
    vs = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
    vs.init()
    return vs


def _meta(vs: VectorMemoryStore) -> dict:
    rows = vs.db.execute("SELECT key, value FROM memory_meta").fetchall()
    return {
        str(k): mkr._decode(v) if str(v)[:1] in "[{" else v
        for k, v in rows
        if str(k).startswith("memory_origin_log:")
    }


def _answer() -> dict:
    rules = [f"Always run check number {i} before pushing" for i in range(40)]
    return {"lessons": [{"rule": r, "applies": "always"} for r in rules]}


def _person() -> dict:
    return {"role": "user", "content": "hi", "meta": {"human": True}}


def _sampling_session(keys: tuple[str, ...]) -> str:
    """A session key whose control sample of *keys* is not empty."""
    return next(s for s in (f"dashboard:{i}" for i in range(500)) if trace.control_keys(s, keys))


@pytest.mark.parametrize("mode", ["shadow", "enforce", "off"])
def test_origin_ids_carry_tag_and_time_and_shadow_keeps_a_control_sample(store, mode) -> None:
    demoting = mog.gate_consolidation_lessons(_answer(), [], mode)
    keeping = mog.gate_consolidation_lessons(_answer(), [_person()], mode)
    session = _sampling_session(
        mog.gate_consolidation_lessons(_answer(), [_person()], "shadow").kept_keys
    )
    mog.record_origin_gate(store, demoting, now=_T0, session_key=session)
    assert not any(":control:" in k for k in _meta(store)), "a pass that keeps nothing"
    mog.record_origin_gate(store, keeping, now=_T0, session_key=session)
    meta = _meta(store)
    ids = meta.get(mog.IDS_PREFIX + "2026-11-01", [])
    control = meta.get("memory_origin_log:control:2026-11-01", [])
    if mode != "shadow":
        assert ids == [] and control == []
        return
    tag = trace.session_tag(session)
    assert ids and all(i["sk"] == tag and i["at"] == _T0.isoformat() for i in ids)
    assert control, "the span with a person keeps its lessons; some join the control"
    assert [c["key"] for c in control] == trace.control_keys(session, keeping.kept_keys)
    assert all(set(c) == {"key", "rules", "sk", "at"} for c in control)
    assert session not in json.dumps(meta) and "check number" not in json.dumps(meta)


def test_control_rows_stop_with_the_shadow_window_and_keep_the_window_key(store) -> None:
    keeping = mog.gate_consolidation_lessons(_answer(), [_person()], "shadow")
    session = _sampling_session(keeping.kept_keys)
    late = _T0 + timedelta(seconds=mkr.SHADOW_SAMPLE_WINDOW_SECS + 1)
    with store._db_lock:
        mkr._upsert(store.db, mog.SHADOW_STARTED_KEY, _T0.isoformat(), _T0.isoformat())
        store.db.commit()
    mog.record_origin_gate(store, keeping, now=late, session_key=session)
    meta = _meta(store)
    assert not any(":control:" in k for k in meta)
    assert meta[mog.SHADOW_STARTED_KEY] == _T0.isoformat()


def test_control_rows_hold_their_daily_cap(store) -> None:
    for n in range(400):
        gate = mog.gate_consolidation_lessons(_answer(), [_person()], "shadow")
        mog.record_origin_gate(store, gate, now=_T0, session_key=f"{SESSION}{n}")
    control = _meta(store)["memory_origin_log:control:2026-11-01"]
    assert len(control) == trace.CONTROL_PER_DAY


# ── Re-teach neighbours ─────────────────────────────────────────────────────


def test_reteach_neighbours_names_lessons_above_the_bar_closest_first(store) -> None:
    import struct

    for key, vector in (
        ("lesson.near", [1.0, 0.1]),
        ("lesson.far", [0.0, 1.0]),
        ("lesson.same", [1.0, 0.0]),
        ("project.fact", [1.0, 0.0]),
    ):
        store.set_semantic(key, {"rule": key}, 1.0, "user_explicit")
        store.db.execute(
            "UPDATE semantic_memory SET embedding = ? WHERE key = ?",
            (struct.pack("2f", *vector), key),
        )
        store.db.commit()
    assert trace.reteach_neighbours(store, [1.0, 0.0]) == ["lesson.same", "lesson.near"]
    assert trace.reteach_neighbours(store, None) == []


# ── The recall route ────────────────────────────────────────────────────────


def _recall_request():
    request = MagicMock()
    request.path = "/api/memory/recall"
    flags = {"internal_auth": True}
    request.get = lambda key, default=None: flags.get(key, default)
    request.query = {"q": QUERY}
    request.headers = {"X-Session-Key": SESSION}
    request.app["state"]._owner_ws_clients = set()
    return request


async def _recall(vs: VectorMemoryStore, memory_mode: str, monkeypatch) -> str:
    from kiro_crew.dashboard.handlers import memory_member as mm

    monkeypatch.setattr(mm, "_recognize_session", AsyncMock(return_value=None))
    monkeypatch.setattr(mm, "_blocks_reads_session", lambda *_a, **_kw: False)
    monkeypatch.setattr(mm, "resolve_lesson_memory_store", AsyncMock(return_value=("", None)))
    monkeypatch.setattr(mm, "vector_memory_for_store", AsyncMock(return_value=vs))
    monkeypatch.setattr(mm, "markdown_memory_for_store", AsyncMock(return_value=None))
    monkeypatch.setattr(mm, "requesting_slot_project", lambda *_a, **_kw: None)
    monkeypatch.setattr(mm, "resolve_session_memory_mode", AsyncMock(return_value=memory_mode))
    monkeypatch.setattr(ir, "schedule_write", lambda line: line and ir.write_line(line))
    response = await mm.api_memory_recall(_recall_request())
    assert response.status == 200, response.text
    return response.text


@pytest.mark.asyncio
async def test_a_recall_answers_the_same_bytes_and_shadow_adds_only_its_receipt(
    home: Path, store, monkeypatch
) -> None:
    store.embed_fn = None
    store.set_semantic(
        "project.signer_region", "the signer deploys to us-west-2", 1.0, "user_explicit"
    )
    _config(home)
    off = await _recall(store, "persistent", monkeypatch)
    assert not (home / "usage" / "injections").exists(), "off writes nothing"
    assert "project.signer_region" in off
    _config(home, rule_authority_order="shadow")
    shadow = await _recall(store, "persistent", monkeypatch)
    assert shadow == off
    (line,) = _shards(home)
    assert line["phase"] == "recall"
    assert line["recall"]["ids"] == ["project.signer_region"]
    assert line["sk"] == trace.session_tag(SESSION)
    text = json.dumps(line)
    assert SESSION not in text and "signer deploys" not in text and QUERY not in text


@pytest.mark.asyncio
async def test_a_recall_names_the_lessons_it_rendered(home: Path, store, monkeypatch) -> None:
    store.embed_fn = None
    store.write_lesson("Always deploy the signer to us-west-2", "knowledge", None, "user_explicit")
    (lesson,) = [r["key"] for r in store.get_lessons()] or [None]
    _config(home)
    off = await _recall(store, "persistent", monkeypatch)
    _config(home, lesson_fold="shadow")
    assert await _recall(store, "persistent", monkeypatch) == off
    (line,) = _shards(home)
    assert lesson and line["recall"]["ids"] == [lesson]
    assert "us-west-2" not in json.dumps(line)


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode", ["incognito", "temporary"])
async def test_a_private_session_recall_writes_nothing(
    home: Path, store, monkeypatch, memory_mode
) -> None:
    store.embed_fn = None
    store.set_semantic(
        "project.signer_region", "the signer deploys to us-west-2", 1.0, "user_explicit"
    )
    _config(home, rule_authority_order="shadow")
    await _recall(store, memory_mode, monkeypatch)
    assert not (home / "usage" / "injections").exists()


# ── The scorecard ───────────────────────────────────────────────────────────


def _at(day: int, hour: int = 10) -> str:
    return f"2026-10-{day:02d}T{hour:02d}:00:00.000Z"


def _start(day: int, session: str, **stages) -> dict:
    return {
        "ts": _at(day),
        "session_key": session,
        "phase": "session_start",
        "memory_store": "default",
        "lessons": [],
        **stages,
    }


def _outcome(kind: str, day: int, session: str, ids: list[str], hour: int = 11) -> dict:
    return {
        "ts": _at(day, hour),
        "sk": trace.session_tag(session),
        "phase": kind,
        "memory_store": "default",
        kind: {"mode": "trace", "policy": 1, "ids": ids},
    }


def _write_receipts(home: Path, lines: list[dict], stage_log: dict) -> None:
    folder = home / "usage" / "injections"
    folder.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[str]] = {}
    for line in lines:
        by_day.setdefault(line["ts"][:10], []).append(json.dumps(line))
    for day, rows in by_day.items():
        (folder / f"{day}.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (folder / "stage-log.json").write_text(json.dumps(stage_log), encoding="utf-8")


def _write_meta(rows: dict[str, object]) -> None:
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


def _report(capsys, **kw) -> dict:
    ns = argparse.Namespace(
        mem_action="shadow", shadow_action="report", stage=None, since=None, store=None, json=True
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    cm._memory_cmd(ns)
    return json.loads(capsys.readouterr().out)


def _card(data: dict, stage: str) -> dict:
    return next(c for c in data["stages"] if c["stage"] == stage)


def _rao(drop: list[str], control: list[str]) -> dict:
    return {
        "mode": "shadow",
        "policy": 1,
        "would_add": [],
        "would_drop": drop,
        "control": control,
    }


def _rao_log(days: range) -> dict:
    return {
        "counts": {
            f"2026-10-{d:02d}": {"rule_authority_order": {"shadow": {"lines": 20}}} for d in days
        },
        "stages": {"rule_authority_order": {"policy": 1, "started_at": "2026-10-01T00:00:00Z"}},
    }


def test_a_render_drop_is_missed_only_by_a_later_outcome_in_its_own_session(
    home: Path, capsys
) -> None:
    _config(home, rule_authority_order="shadow")
    lines = [
        _start(1, "dashboard:a", rule_authority_order=_rao(["lesson.x", "lesson.y"], ["lesson.k"])),
        _start(1, "dashboard:b", rule_authority_order=_rao(["lesson.x"], ["lesson.k"])),
        # a: recalled after the drop -> a miss; b: recalled BEFORE its drop -> not.
        _outcome("recall", 1, "dashboard:a", ["lesson.x"]),
        _outcome("recall", 1, "dashboard:b", ["lesson.x"], hour=9),
        # Another session's re-teach of y does not count for a's render.
        _outcome("learn_add", 2, "dashboard:c", ["lesson.y"]),
        # b re-teaches its kept control row.
        _outcome("learn_add", 2, "dashboard:b", ["lesson.k"]),
    ]
    _write_receipts(home, lines, _rao_log(range(1, 3)))
    card = _card(_report(capsys), "rule_authority_order")
    assert card["miss"] == {"items": 3, "missed": 1, "rate": 0.3333}
    assert card["control"] == {"items": 2, "missed": 1, "rate": 0.5}
    assert card["verdict"] == msr.VERDICT_WAIT
    assert card["verdict_reason"] == "2/14 days, 2/200 sessions"


def _bar_met(home: Path, drop_misses: int, control_misses: int, *, control: int = 100) -> dict:
    """14 days, 200 session starts, 200 drops and *control* control rows in sessions s0..s199."""
    lines = []
    for i in range(200):
        session = f"dashboard:s{i}"
        drop = [f"lesson.d{i}"]
        ctrl = [f"lesson.c{i}"] if i < control else []
        lines.append(_start(1 + i % 14, session, rule_authority_order=_rao(drop, ctrl)))
        if i < drop_misses:
            lines.append(_outcome("recall", 1 + i % 14, session, drop))
        if i < control_misses:
            lines.append(_outcome("recall", 1 + i % 14, session, ctrl))
    # An outcome that names nothing scored, so the window has an observed trace.
    lines.append(_outcome("recall", 3, "dashboard:other", ["project.unrelated"]))
    _write_receipts(home, lines, _rao_log(range(1, 15)))
    return lines[0]


@pytest.mark.parametrize(
    "drop_misses, control_misses, verdict",
    [
        (0, 0, msr.VERDICT_PASS),
        (2, 2, msr.VERDICT_PASS),  # 1/100 drops, 2/100 control
        (3, 5, msr.VERDICT_FAIL),  # 1.5% is over the 1/100 bar
        (2, 0, msr.VERDICT_FAIL),  # 1/100 drops but worse than the control
    ],
)
def test_the_verdict_needs_the_bar_and_a_miss_rate_within_both_limits(
    home: Path, capsys, drop_misses, control_misses, verdict
) -> None:
    _config(home, rule_authority_order="shadow")
    _bar_met(home, drop_misses, control_misses)
    card = _card(_report(capsys), "rule_authority_order")
    assert (card["days"], card["sessions"]) == (14, 200)
    assert card["miss"]["items"] == 200 and card["control"]["items"] == 100
    assert card["verdict"] == verdict, card


def test_the_bar_is_compared_without_rounding() -> None:
    """3 of 299 is 1.003%: over the bar, though it rounds to 1.00%."""
    card = {
        "mode": "shadow",
        "stage": "rule_authority_order",
        "days": 14,
        "sessions": 200,
        "miss": {"items": 299, "missed": 3, "rate": 0.01},
        "control": {"items": 100, "missed": 5, "rate": 0.05},
    }
    assert msr._verdict(card, 1)[0] == msr.VERDICT_FAIL
    card["miss"] = {"items": 300, "missed": 3, "rate": 0.01}
    assert msr._verdict(card, 1)[0] == msr.VERDICT_PASS
    assert msr._verdict(card, 0) == (msr.VERDICT_WAIT, "no outcome trace in window")


def test_activity_ids_are_not_scored_and_a_dropped_key_leaves_the_control(
    home: Path, capsys
) -> None:
    _config(home, relevance_rule="shadow", member_lesson_dedup="shadow")
    field = {
        "mode": "shadow",
        "policy": 1,
        "would_add": [],
        "would_drop": ["projects:abc", "history:def", "project.fact"],
        "control": ["projects:ghi", "project.kept"],
    }
    log = {
        "counts": {"2026-10-01": {"relevance_rule": {"shadow": {"lines": 1}}}},
        "stages": {"relevance_rule": {"policy": 1, "started_at": "2026-10-01T00:00:00Z"}},
    }
    _write_receipts(home, [_start(1, "dashboard:a", relevance_rule=field)], log)
    _write_meta(
        {
            "memory_dedup_log:counts:2026-10-01": {"shadow": {"lessons": 2}},
            "memory_dedup_log:ids:2026-10-01": [
                {
                    "key": "lesson.n",
                    "leader": "lesson.o",
                    "step": "vector",
                    "rules": 1,
                    "at": "2026-10-01T09:00:00+00:00",
                }
            ],
            "memory_dedup_log:control:2026-10-01": [
                {"key": "lesson.n", "rules": 1, "at": "2026-10-01T08:00:00+00:00"},
                {"key": "lesson.k", "rules": 1, "at": "2026-10-01T08:00:00+00:00"},
            ],
        }
    )
    data = _report(capsys)
    relevance = _card(data, "relevance_rule")
    assert (relevance["miss"]["items"], relevance["control"]["items"]) == (1, 1)
    dedup = _card(data, "member_lesson_dedup")
    assert (dedup["miss"]["items"], dedup["control"]["items"]) == (1, 1)


def test_a_row_dropped_in_one_session_stays_in_another_sessions_control(home: Path, capsys) -> None:
    _config(home, relevance_rule="shadow")
    log = {
        "counts": {"2026-10-01": {"relevance_rule": {"shadow": {"lines": 2}}}},
        "stages": {"relevance_rule": {"policy": 1, "started_at": "2026-10-01T00:00:00Z"}},
    }
    keep = {"mode": "shadow", "policy": 1, "would_add": [], "would_drop": [], "control": ["k"]}
    drop = {"mode": "shadow", "policy": 1, "would_add": [], "would_drop": ["k"]}
    lines = [
        _start(1, "dashboard:a", relevance_rule=keep),
        _start(1, "dashboard:b", relevance_rule=drop),
    ]
    _write_receipts(home, lines, log)
    card = _card(_report(capsys), "relevance_rule")
    assert (card["miss"]["items"], card["control"]["items"]) == (1, 1)


def test_outcome_lines_keep_flowing_after_their_first_window(home: Path) -> None:
    late = _T0 + timedelta(seconds=mkr.SHADOW_SAMPLE_WINDOW_SECS * 3)
    for now in (_T0, late):
        ir.write_line(
            ir.build_outcome_line(
                ir.RECALL, session_key=SESSION, store="", ids=["project.a"], now=now
            )
        )
    assert [line["phase"] for line in _shards(home)] == ["recall", "recall"]
    log = json.loads((home / "usage" / "injections" / "stage-log.json").read_text())
    assert list(log["stages"]["recall"]["samples"]) == [late.date().isoformat()]


def test_no_control_sample_waits(home: Path, capsys) -> None:
    _config(home, rule_authority_order="shadow")
    _bar_met(home, 0, 0, control=0)
    card = _card(_report(capsys), "rule_authority_order")
    assert (card["verdict"], card["verdict_reason"]) == (msr.VERDICT_WAIT, "no control sample")


def test_write_stage_trace_reads_new_and_old_entries_and_never_text(home: Path, capsys) -> None:
    _config(home, lesson_origin_gate="shadow", kind_routing="shadow", lesson_tier_policy="shadow")
    tag = trace.session_tag("dashboard:a")
    _write_meta(
        {
            "memory_origin_log:counts:2026-10-01": {"shadow": {"consolidation": {"lessons": 3}}},
            "memory_origin_log:ids:2026-10-01": [
                {
                    "key": "lesson.new",
                    "path": "consolidation",
                    "rules": 1,
                    "sk": tag,
                    "at": "2026-10-01T08:00:00+00:00",
                },
                # Written before entries carried a time: placed at the end of its day.
                {"key": "lesson.old", "path": "consolidation", "rules": 1},
            ],
            "memory_origin_log:control:2026-10-01": [
                {"key": "lesson.kept", "rules": 1, "sk": tag, "at": "2026-10-01T08:00:00+00:00"}
            ],
            "memory_tier_log:counts:2026-10-01": {"shadow": {"consolidation": {"lessons": 1}}},
            "memory_tier_log:ids:2026-10-01": [
                {"key": "lesson.t", "path": "consolidation", "rules": 1},
                {"key": "lesson.r", "path": "reteach", "rules": 1},
            ],
            "memory_kind_log:counts:2026-10-01": {"shadow": {"state": 1, "fact": 1}},
            "memory_kind_log:samples:2026-10-01": [
                {"key": "project.s", "kind": "state", "value": SECRET, "ts": "2026-10-01T08:00:00"},
                {"key": "project.f", "kind": "fact", "value": SECRET, "ts": "2026-10-01T08:00:00"},
            ],
        }
    )
    lines = [
        _start(1, "dashboard:z"),
        # Later the same day in any session: names new, old (still earlier than the
        # end of day) and the kept control row.
        _outcome("recall", 1, "dashboard:z", ["lesson.new", "lesson.old", "lesson.kept"]),
        _outcome("learn_add", 2, "dashboard:z", ["project.s"]),
    ]
    _write_receipts(home, lines, {"counts": {}, "stages": {}})
    data = _report(capsys)
    origin = _card(data, "lesson_origin_gate")
    assert origin["miss"] == {"items": 2, "missed": 1, "rate": 0.5}
    assert origin["control"] == {"items": 1, "missed": 1, "rate": 1.0}
    tier = _card(data, "lesson_tier_policy")
    assert tier["miss"]["items"] == 1, "a re-teach entry is an outcome, not a drop"
    kind = _card(data, "kind_routing")
    assert kind["miss"] == {"items": 1, "missed": 1, "rate": 1.0}
    assert kind["control"] == {"items": 1, "missed": 0, "rate": 0.0}
    assert data["outcomes"] == {"recall": 1, "learn_add": 1}
    assert SECRET not in json.dumps(data)


def test_outcome_totals_count_only_the_reported_store(home: Path, capsys) -> None:
    _config(home, rule_authority_order="shadow")
    other = {**_outcome("recall", 1, "dashboard:a", ["lesson.x"]), "memory_store": "mate"}
    _write_receipts(home, [_start(1, "dashboard:a"), other], {"counts": {}, "stages": {}})
    assert _report(capsys)["outcomes"] == {"recall": 0, "learn_add": 0}


def test_read_trace_never_selects_a_value_field(home: Path) -> None:
    _write_meta(
        {
            "memory_kind_log:samples:2026-10-01": [{"key": "project.s", "value": SECRET}],
            "memory_dedup_log:ids:2026-10-01": "not json [",
        }
    )
    rows = msr.read_trace(owned_store_path("default"))
    assert rows == [("memory_kind_log:samples:2026-10-01", "samples", {"key": "project.s"})]


# ── The learn_add route ─────────────────────────────────────────────────────


async def _learn_add(monkeypatch, *, observing: bool) -> list:
    from body_stream_helpers import attach_body

    from kiro_crew import memory_shadow_trace
    from kiro_crew.dashboard.handlers import cron
    from kiro_crew.history import ConversationLog
    from kiro_crew.vector_memory import LessonWriteOutcome, LessonWriteResult

    state = MagicMock()
    state._background_tasks = set()
    state.conversation_log = ConversationLog()
    vs = MagicMock()
    vs.algorithm_version = "v2"
    vs.embed_lesson.return_value = [0.1] * 4
    vs.write_lesson.return_value = LessonWriteResult(LessonWriteOutcome.INSERTED)
    request = MagicMock()
    request.app = {"state": state}
    request.headers = {"X-Session-Key": "dashboard:ui"}
    attach_body(request, {"rule": SECRET, "category": "knowledge"})
    written: list = []
    monkeypatch.setattr(memory_shadow_trace, "observing", lambda *_a: observing)
    monkeypatch.setattr(
        memory_shadow_trace, "reteach_neighbours", lambda _vs, emb: ["lesson.old"] if emb else []
    )
    monkeypatch.setattr(ir, "schedule_write", written.append)
    monkeypatch.setattr(cron, "_get_memory", lambda *_a, **_k: MagicMock(vector_store=vs))
    monkeypatch.setattr(cron, "_is_restricted_session", lambda *_a, **_k: False)
    monkeypatch.setattr(cron, "_sel", MagicMock())
    resp = await cron.api_lessons_create(request)
    assert resp.status == 200
    return written


@pytest.mark.asyncio
async def test_a_learn_add_records_its_neighbours_only_while_a_stage_is_on(monkeypatch) -> None:
    assert await _learn_add(monkeypatch, observing=False) == []
    (line,) = await _learn_add(monkeypatch, observing=True)
    assert line["phase"] == "learn_add"
    assert line["learn_add"]["ids"] == ["lesson.old"]
    assert line["sk"] == trace.session_tag("dashboard:ui")
    assert SECRET not in json.dumps(line)
