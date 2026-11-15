"""``memory.rule_authority_order``: the rule block ordered by who taught each rule.

Pinned: ``off`` renders today's block byte for byte and records no stage field;
``shadow`` renders the same block and records which rule ids the authority order
would add or drop; ``enforce`` puts user-taught rules first, request relevance
inside a level, on the default and the private store alike, under the same cap
and omission line; an unknown value reads as ``off``; and the receipt's stage
field stays inside every kind-routing cap.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from member_memory_helpers import declare_v2_store

from kiro_crew import injection_receipts, memory_kind_routing, memory_rule_authority, memory_stores
from kiro_crew.config.memory_sections import MemoryConfig, rule_authority_order_mode_from
from kiro_crew.config.section_builders import _build_memory_config
from kiro_crew.recall_pool import lessons_args
from kiro_crew.vector_memory import VectorMemoryStore, open_member_database

RULE_BUDGET = 2_000
STAGE = injection_receipts.RULE_AUTHORITY_ORDER


def _rule(marker: str, size: int = 200) -> str:
    """A rule lexically distinct from every other marker's (see the startup-budget tests)."""
    filler = f"{marker.lower()}pad "
    return (f"{marker} " + (filler * (size // len(filler) + 1))[:size]).strip()


@pytest.fixture(params=["default", "private"])
def store(request, tmp_path, monkeypatch):
    if request.param == "default":
        opened = VectorMemoryStore(db_path=tmp_path / "memory.db")
        opened.init()
    else:
        monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "memory_stores")
        directory = declare_v2_store(tmp_path, "member-alice")
        opened = open_member_database(
            directory / "memory.db", member_id="alice", store_id="member-alice"
        )
    try:
        yield opened
    finally:
        opened.close()


def _seed(store) -> tuple[list[str], list[str]]:
    """Inferred rules that match the request, user-taught rules that do not.

    Returns the user-taught and inferred rule texts. The block cannot hold all of
    them, so relevance alone keeps the inferred rows and drops the taught ones.
    """
    taught = [_rule(f"TAUGHT{i}") for i in range(6)]
    # Many distinct words each, so the write path's keyword-overlap dedup keeps them apart.
    inferred = ["quokka deploy " + " ".join(f"inf{i}w{j}" for j in range(30)) for i in range(12)]
    for text in taught:
        assert store.write_lesson(text, category="preference", applies="always")
    for text in inferred:
        assert store.write_lesson(
            text, category="knowledge", source="consolidation", applies="always"
        )
    return taught, inferred


def _block(store, mode: str | None, query: str = "quokka deploy") -> tuple[str, dict]:
    kwargs = {} if mode is None else {"authority_order": mode}
    with injection_receipts.collect() as receipts:
        out = store.get_lessons_context(
            query,
            background=True,
            hard_cap=99_000,
            directive_budget=RULE_BUDGET,
            experience_budget=500,
            **kwargs,
        )
    return out, receipts


def _keys(store, texts: list[str]) -> set[str]:
    wanted = set(texts)
    return {
        row["key"] for row in store.get_lessons() if json.loads(row["value_json"])["rule"] in wanted
    }


class TestTheModes:
    def test_off_is_todays_block_and_records_no_stage(self, store):
        _seed(store)
        default, default_receipts = _block(store, None)
        off, off_receipts = _block(store, "off")
        assert off == default
        assert STAGE not in off_receipts and STAGE not in default_receipts
        assert off_receipts == default_receipts

    def test_shadow_keeps_the_block_and_records_what_would_change(self, store):
        taught, inferred = _seed(store)
        off, off_receipts = _block(store, "off")
        shadow, receipts = _block(store, "shadow")
        assert shadow == off
        assert receipts[injection_receipts.LESSONS] == off_receipts[injection_receipts.LESSONS]
        stage = receipts[STAGE]
        assert stage["mode"] == "shadow"
        assert stage["policy"] == memory_rule_authority.POLICY_VERSION
        shown = set(off_receipts[injection_receipts.LESSONS]["ids"])
        assert stage["would_add"], "the cut should have dropped a user-taught rule"
        assert set(stage["would_add"]) <= _keys(store, taught) - shown
        assert set(stage["would_drop"]) <= _keys(store, inferred) & shown
        # Ids only, never text.
        assert all(key.startswith("lesson.") for key in stage["would_add"] + stage["would_drop"])

    def test_enforce_puts_user_taught_rules_first_under_the_same_cap(self, store):
        taught, inferred = _seed(store)
        off, _ = _block(store, "off")
        out, receipts = _block(store, "enforce")
        assert all(text in out for text in taught)
        last_taught = max(out.index(text) for text in taught)
        kept_inferred = [text for text in inferred if text in out]
        assert all(out.index(text) > last_taught for text in kept_inferred)
        # Same cap and the same omission line.
        rule_block = out.split("[End of learned corrections]")[0]
        assert len(rule_block) <= RULE_BUDGET
        assert "omitted" in out and "-character rule budget" in out
        assert off != out
        assert receipts[STAGE] == {
            "mode": "enforce",
            "policy": memory_rule_authority.POLICY_VERSION,
            "would_add": [],
            "would_drop": [],
        }

    def test_enforce_keeps_request_relevance_inside_a_level(self, store):
        taught = [_rule("TAUGHTA"), "quokka deploy guard " + _rule("TAUGHTB")]
        for text in taught:
            assert store.write_lesson(text, category="preference", applies="always")
        out, _ = _block(store, "enforce")
        assert out.index(taught[1]) < out.index(taught[0])

    def test_enforce_keeps_authored_rules_ahead_of_untagged_inside_a_level(self, store):
        untagged = "quokka deploy " + " ".join(f"unt{j}" for j in range(30))
        authored = _rule("AUTHORED")
        assert store.write_lesson(untagged, category="knowledge", source="consolidation")
        assert store.write_lesson(
            authored, category="knowledge", source="consolidation", applies="always"
        )
        out, _ = _block(store, "enforce")
        assert out.index(authored) < out.index(untagged)

    def test_an_agent_triggered_learn_add_is_not_user_taught(self, store):
        assert store.write_lesson(
            _rule("AGENTSENT"), category="preference", source="agent_learn_add", applies="always"
        )
        row = store.get_lessons()[0]
        assert row["source"] == "agent_learn_add"
        assert memory_rule_authority.authority(row) == memory_rule_authority.LEVEL_AGENT_INFERRED


class TestTheFlag:
    @pytest.mark.parametrize("raw", [None, "", "ENFORCE", "on", 1, True, ["enforce"]])
    def test_an_unknown_value_reads_as_off(self, raw):
        assert rule_authority_order_mode_from(raw) == "off"
        assert _build_memory_config({"rule_authority_order": raw}).rule_authority_order == "off"

    @pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
    def test_a_known_value_is_kept(self, mode):
        assert _build_memory_config({"rule_authority_order": mode}).rule_authority_order == mode

    def test_the_default_is_off(self):
        assert MemoryConfig().rule_authority_order == "off"

    def test_an_unknown_value_renders_todays_block(self, store):
        _seed(store)
        assert _block(store, "sideways") == _block(store, "off")

    def test_a_recall_child_gets_the_mode(self):
        args = lessons_args(None, "q", 0, None, None, True, 0, 0, 0, "shadow")
        assert args["authority_order"] == "shadow"


# -- the receipt's stage field -----------------------------------------------

_NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def _stage_line(now: datetime, *, policy: int = 1, ids: int = 2) -> dict:
    receipts = {
        injection_receipts.LESSONS: {"ids": ["lesson.a"], "omitted": 0},
        STAGE: {
            "mode": "shadow",
            "policy": policy,
            "would_add": [f"lesson.{i:012x}" for i in range(ids)],
            "would_drop": [],
        },
    }
    return injection_receipts.build_line(
        receipts,
        session_key="dashboard_chat-1-2",
        turn_index=1,
        user_msg_id="m-1",
        model="m",
        agent="kirocrew",
        phase="session_start",
        store="",
        now=now,
    )


@pytest.fixture
def receipt_dir(tmp_path, monkeypatch):
    target = tmp_path / "injections"
    monkeypatch.setattr(injection_receipts, "_receipt_dir", lambda: target)
    return target


def _written(receipt_dir) -> list[dict]:
    return [
        json.loads(text)
        for shard in sorted(receipt_dir.glob("*.jsonl"))
        for text in shard.read_text(encoding="utf-8").splitlines()
    ]


def _log(receipt_dir) -> dict:
    return json.loads((receipt_dir / "stage-log.json").read_text(encoding="utf-8"))


class TestTheStageFieldCaps:
    def test_a_line_without_a_stage_is_unchanged_and_writes_no_log(self, receipt_dir):
        line = _stage_line(_NOW)
        del line[STAGE]
        injection_receipts.write_line(dict(line))
        assert _written(receipt_dir) == [line]
        assert not (receipt_dir / "stage-log.json").exists()

    def test_the_stage_field_joins_the_line(self, receipt_dir):
        line = _stage_line(_NOW)
        injection_receipts.write_line(line)
        assert _written(receipt_dir)[0][STAGE]["would_add"] == line[STAGE]["would_add"]
        counts = _log(receipt_dir)["counts"][_NOW.date().isoformat()][STAGE]["shadow"]
        assert counts == {"lines": 1, "would_add": 2, "would_drop": 0}

    def test_rows_per_day_cap_then_counters_only(self, receipt_dir):
        extra = 5
        for i in range(memory_kind_routing.SAMPLES_PER_DAY + extra):
            injection_receipts.write_line(_stage_line(_NOW + timedelta(seconds=i)))
        rows = _written(receipt_dir)
        assert len(rows) == memory_kind_routing.SAMPLES_PER_DAY + extra
        assert sum(STAGE in row for row in rows) == memory_kind_routing.SAMPLES_PER_DAY
        # A capped line is the plain line, byte for byte.
        capped = rows[-1]
        plain = _stage_line(_NOW + timedelta(seconds=len(rows) - 1))
        del plain[STAGE]
        assert capped == plain
        day = _log(receipt_dir)["counts"][_NOW.date().isoformat()][STAGE]["shadow"]
        assert day["lines"] == memory_kind_routing.SAMPLES_PER_DAY + extra

    def test_total_rows_cap(self, receipt_dir, monkeypatch):
        monkeypatch.setattr(memory_kind_routing, "SAMPLES_TOTAL", 4)
        for day in range(3):
            for i in range(2):
                injection_receipts.write_line(_stage_line(_NOW + timedelta(days=day, seconds=i)))
        assert sum(STAGE in row for row in _written(receipt_dir)) == 4

    def test_bytes_cap(self, receipt_dir, monkeypatch):
        size = len(json.dumps(_stage_line(_NOW)[STAGE], separators=(",", ":")))
        monkeypatch.setattr(memory_kind_routing, "SAMPLE_BYTES_CAP", size * 3)
        for i in range(6):
            injection_receipts.write_line(_stage_line(_NOW + timedelta(seconds=i)))
        assert sum(STAGE in row for row in _written(receipt_dir)) == 3

    def test_sample_window_then_a_policy_change_restarts_it(self, receipt_dir):
        window = timedelta(seconds=memory_kind_routing.SHADOW_SAMPLE_WINDOW_SECS)
        injection_receipts.write_line(_stage_line(_NOW))
        injection_receipts.write_line(_stage_line(_NOW + window))
        injection_receipts.write_line(_stage_line(_NOW + window, policy=2))
        assert [STAGE in row for row in _written(receipt_dir)] == [True, False, True]
        assert _log(receipt_dir)["stages"][STAGE]["policy"] == 2

    def test_counters_past_retention_are_pruned(self, receipt_dir):
        injection_receipts.write_line(_stage_line(_NOW))
        later = _NOW + timedelta(days=memory_kind_routing.COUNTER_DAYS_KEPT + 1)
        injection_receipts.write_line(_stage_line(later))
        assert list(_log(receipt_dir)["counts"]) == [later.date().isoformat()]

    def test_an_unreadable_log_drops_the_field_and_keeps_the_line(self, receipt_dir):
        receipt_dir.mkdir(parents=True)
        (receipt_dir / "stage-log.json").mkdir()  # neither readable nor replaceable
        injection_receipts.write_line(_stage_line(_NOW))
        rows = _written(receipt_dir)
        assert len(rows) == 1 and STAGE not in rows[0]

    def test_a_corrupt_log_drops_the_field_rather_than_restart_the_caps(self, receipt_dir):
        receipt_dir.mkdir(parents=True)
        (receipt_dir / "stage-log.json").write_text("{not json", encoding="utf-8")
        injection_receipts.write_line(_stage_line(_NOW))
        rows = _written(receipt_dir)
        assert len(rows) == 1 and STAGE not in rows[0]


class TestTheRecord:
    def test_a_stage_record_needs_a_collection(self):
        injection_receipts.record_stage(STAGE, mode="shadow", policy=1, would_add=["lesson.a"])

    def test_an_unknown_stage_is_a_programming_error(self):
        with pytest.raises(ValueError):
            injection_receipts.record_stage("nope", mode="shadow", policy=1)
        with pytest.raises(ValueError):
            injection_receipts.record(STAGE, ())

    def test_a_recall_child_record_replays_ids_only(self):
        with injection_receipts.collect() as sink:
            injection_receipts.replay(
                {
                    STAGE: {
                        "mode": "shadow",
                        "policy": 1,
                        "would_add": ["lesson.a", 3, None],
                        "would_drop": "lesson.b",
                        "text": "never carried",
                    }
                }
            )
        assert sink[STAGE] == {
            "mode": "shadow",
            "policy": 1,
            "would_add": ["lesson.a"],
            "would_drop": [],
        }


class TestBothRenderersPassTheMode:
    """Both vector callers in ``store_admission`` hand the configured mode to the store."""

    def _render(self, monkeypatch, mode: str, *, member: bool) -> dict:
        from unittest.mock import MagicMock

        from kiro_crew.context_assembly import store_admission

        monkeypatch.setattr(memory_rule_authority, "configured_mode", lambda: mode)
        store = MagicMock()
        store.get_lessons_context.return_value = ""
        memory = None
        if not member:
            memory = MagicMock()
            memory.vector_store = store
            store.has_any_lesson.return_value = True
        renderer = store_admission._lessons_renderer(
            MagicMock(),
            memory=memory,
            member_vectors=store if member else None,
            activity_ranked=False,
            workspace=None,
            memory_store=None,
            project=None,
            caps=MagicMock(),
            query_text="q",
        )
        renderer(1_000)
        return store.get_lessons_context.call_args.kwargs

    @pytest.mark.parametrize("member", [True, False])
    @pytest.mark.parametrize("mode", ["shadow", "enforce"])
    def test_a_stage_mode_reaches_the_store(self, monkeypatch, mode, member):
        assert self._render(monkeypatch, mode, member=member)["authority_order"] == mode

    @pytest.mark.parametrize("member", [True, False])
    def test_off_passes_nothing_so_the_call_is_todays(self, monkeypatch, member):
        assert "authority_order" not in self._render(monkeypatch, "off", member=member)
