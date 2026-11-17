"""``memory.lesson_fold``: near-duplicate rules folded out of the rule block at render.

Pinned: ``off`` renders today's block byte for byte and records no stage field;
``shadow`` renders the same block and records ``(leader, folded)`` id pairs;
``enforce`` leaves folded rows out, prints the fold line and does not count them as
budget omissions, on the default and the private store alike. A user-taught row is
never folded; a missing, zero, mismatched or unattributed vector never folds; a
row with a NOT-clause its leader lacks stays; and leaders never chain.
"""

from __future__ import annotations

import json
import math
import struct
from datetime import datetime, timedelta, timezone

import pytest
from member_memory_helpers import declare_v2_store

from kiro_crew import injection_receipts, memory_lesson_fold, memory_stores, vector_memory
from kiro_crew.config.memory_sections import MemoryConfig, lesson_fold_mode_from
from kiro_crew.config.section_builders import _build_memory_config
from kiro_crew.recall_pool import lessons_args
from kiro_crew.vector_memory import VectorMemoryStore, open_member_database

STAGE = injection_receipts.LESSON_FOLD
_DIM = 4
_SIG = "test-space"
_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _vec(angle: float) -> list[float]:
    """A unit vector at *angle* radians from the x axis (cosine to x is cos(angle))."""
    return [math.cos(angle), math.sin(angle), 0.0, 0.0]


@pytest.fixture(params=["default", "private"])
def store(request, tmp_path, monkeypatch):
    # Every write gets its own, strictly later stamp, so "newest first" never rests
    # on the clock's resolution.
    ticks = iter(range(1, 1_000_000))
    monkeypatch.setattr(
        vector_memory,
        "_now_iso",
        lambda: (_T0 + timedelta(microseconds=next(ticks))).isoformat(),
    )
    if request.param == "default":
        opened = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=_DIM)
        opened.init()
    else:
        monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "memory_stores")
        directory = declare_v2_store(tmp_path, "member-alice")
        opened = open_member_database(
            directory / "memory.db",
            member_id="alice",
            store_id="member-alice",
            embedding_dim=_DIM,
        )
    opened.reconcile_embedding_space(_SIG)
    try:
        yield opened
    finally:
        opened.close()


_seq = iter(range(1, 1_000_000))


def _put(
    store,
    rule: str,
    vec: list[float] | bytes | None = None,
    *,
    source: str = "consolidation",
    scope: str | None = None,
    negative: str | None = None,
    legacy: bool = False,
) -> str:
    """Store *rule* as a new, newest row and give it vector *vec*; returns its key.

    The row is written under a unique placeholder, so the write path's own dedup
    never merges it, then its value and vector are set to what the test needs.
    """
    placeholder = f"placeholder{next(_seq)} " + " ".join(f"w{next(_seq)}" for _ in range(8))
    result = store.write_lesson(
        placeholder, category="knowledge", source=source, applies="always", repo_scope=scope
    )
    assert result.outcome.value == "inserted"
    row = next(r for r in store.get_lessons() if json.loads(r["value_json"])["rule"] == placeholder)
    value = json.loads(row["value_json"])
    value["rule"] = rule
    if negative:
        value["negative"] = negative
    if legacy:
        # The old in-band string form: the rule and its clause in one string.
        value = f"{rule} — NOT: {negative}" if negative else rule
    if isinstance(vec, bytes) or vec is None:
        blob = vec
    else:
        blob = struct.pack(f"{len(vec)}f", *vec)
    with store._db_lock:
        store.db.execute(
            f"UPDATE {store._sem_rel} SET value_json = ?, embedding = ? "
            f"WHERE key = ?{store._sem_guard}",
            (json.dumps(value), blob, row["key"]),
        )
        store.db.commit()
    return row["key"]


def _block(store, mode: str | None, *, budget: int = 20_000, project=None) -> tuple[str, dict]:
    kwargs = {} if mode is None else {"lesson_fold": mode}
    with injection_receipts.collect() as receipts:
        out = store.get_lessons_context(
            "",
            background=True,
            hard_cap=99_000,
            directive_budget=budget,
            experience_budget=500,
            project_dir=project,
            **kwargs,
        )
    return out, receipts


def _pairs(store) -> list[list[str]]:
    return _block(store, "shadow")[1][STAGE]["pairs"]


NOTICE_1 = memory_lesson_fold.FOLD_NOTICE.format(count=1)


class TestTheModes:
    def test_off_is_todays_block_and_records_no_stage(self, store):
        _put(store, "Run the linter before every commit", _vec(0.0))
        _put(store, "Lint before you commit", _vec(0.1))
        default, default_receipts = _block(store, None)
        off, off_receipts = _block(store, "off")
        assert off == default
        assert off_receipts == default_receipts
        assert STAGE not in off_receipts

    def test_shadow_keeps_the_block_and_records_the_pairs(self, store):
        older = _put(store, "Run the linter before every commit", _vec(0.0))
        newer = _put(store, "Lint before you commit", _vec(0.1))
        off, off_receipts = _block(store, "off")
        shadow, receipts = _block(store, "shadow")
        assert shadow == off
        assert receipts[injection_receipts.LESSONS] == off_receipts[injection_receipts.LESSONS]
        # Newest first: the newer row leads, the older one folds into it.
        assert receipts[STAGE] == {
            "mode": "shadow",
            "policy": memory_lesson_fold.POLICY_VERSION,
            "would_add": [],
            "would_drop": [older],
            "pairs": [[newer, older]],
        }

    def test_enforce_leaves_the_copy_out_and_says_so(self, store):
        older = _put(store, "Run the linter before every commit", _vec(0.0))
        newer = _put(store, "Lint before you commit", _vec(0.1))
        other = _put(store, "Write tests first", _vec(1.5))
        out, receipts = _block(store, "enforce")
        assert "Run the linter before every commit" not in out
        assert "Lint before you commit" in out and "Write tests first" in out
        assert NOTICE_1 in out.split("[End of learned corrections]")[0]
        # A fold is not a budget omission.
        assert "-character rule budget" not in out
        assert receipts[injection_receipts.LESSONS] == {"ids": [other, newer], "omitted": 0}
        assert receipts[STAGE]["pairs"] == [[newer, older]]

    def test_enforce_without_a_copy_is_todays_block(self, store):
        _put(store, "Run the linter before every commit", _vec(0.0))
        _put(store, "Write tests first", _vec(1.5))
        assert _block(store, "enforce")[0] == _block(store, "off")[0]

    def test_folded_rows_do_not_count_toward_the_omission_total(self, store):
        _put(store, "Run the linter before every commit", _vec(0.0))
        _put(store, "Lint before you commit", _vec(0.1))
        for i in range(6):
            _put(store, f"Distinct rule number {i} " + "pad " * 40, _vec(0.6 + 0.5 * i))
        out, receipts = _block(store, "enforce", budget=700)
        assert "omitted" in out and "of 7 retained rules" in out
        kept = len(receipts[injection_receipts.LESSONS]["ids"])
        assert receipts[injection_receipts.LESSONS]["omitted"] == 7 - kept

    def test_a_textual_copy_folds_without_vectors(self, store):
        older = _put(store, "Run the linter before every commit")
        newer = _put(store, "run the  LINTER before every commit")
        assert _pairs(store) == [[newer, older]]


class TestNeverFolded:
    def test_a_user_taught_row_leads_and_is_never_folded(self, store):
        taught = _put(
            store, "Run the linter before every commit", _vec(0.0), source="user_explicit"
        )
        inferred = _put(store, "Lint before you commit", _vec(0.1))
        # The inferred row is newer, but the user-taught level is visited first.
        assert _pairs(store) == [[taught, inferred]]

    def test_two_user_taught_copies_both_stay(self, store):
        _put(store, "Run the linter before every commit", _vec(0.0), source="user_explicit")
        _put(store, "Lint before you commit", _vec(0.1), source="user_explicit")
        assert _pairs(store) == []
        out, _ = _block(store, "enforce")
        assert "Run the linter before every commit" in out and "Lint before you commit" in out

    @pytest.mark.parametrize(
        "blob",
        [
            None,
            struct.pack(f"{_DIM}f", 0.0, 0.0, 0.0, 0.0),
            struct.pack("3f", 1.0, 0.0, 0.0),
            struct.pack(f"{_DIM}f", float("nan"), 0.0, 0.0, 0.0),
        ],
        ids=["missing", "zero", "other-width", "non-finite"],
    )
    def test_an_unusable_vector_never_folds(self, store, blob):
        _put(store, "Run the linter before every commit", _vec(0.0))
        _put(store, "Lint before you commit", blob)
        assert _pairs(store) == []

    def test_two_zero_vectors_never_fold(self, store):
        zero = struct.pack(f"{_DIM}f", 0.0, 0.0, 0.0, 0.0)
        _put(store, "Run the linter before every commit", zero)
        _put(store, "Lint before you commit", zero)
        assert _pairs(store) == []

    def test_an_unrecorded_space_never_folds_by_vector(self, store):
        _put(store, "Run the linter before every commit", _vec(0.0))
        _put(store, "Lint before you commit", _vec(0.0))
        with store._db_lock:
            store.db.execute("DELETE FROM memory_meta WHERE key = 'embedding_space_sig'")
            store.db.commit()
        assert store.recorded_embedding_space() is None
        assert _pairs(store) == []

    def test_another_scope_never_folds(self, store, tmp_path):
        project = tmp_path / "repo"
        (project / "src" / "pkg").mkdir(parents=True)
        _put(store, "Run the linter before every commit", _vec(0.0))
        _put(store, "Run the linter before every commit", _vec(0.0), scope="src/pkg")
        out, receipts = _block(store, "shadow", project=project)
        assert receipts[STAGE]["pairs"] == []
        assert len(receipts[injection_receipts.LESSONS]["ids"]) == 2

    def test_below_the_cosine_never_folds(self, store):
        # cos(0.33) is about 0.946, just under 0.95.
        _put(store, "Run the linter before every commit", _vec(0.0))
        _put(store, "Lint before you commit", _vec(0.33))
        assert _pairs(store) == []

    def test_a_copy_that_adds_a_not_clause_stays(self, store):
        _put(store, "Run the linter before every commit", _vec(0.0))
        _put(store, "Lint before you commit", _vec(0.1), negative="skipping it on hotfixes")
        newest = _put(store, "Always lint first", _vec(0.05))
        pairs = _pairs(store)
        # The newest row (no clause) leads; the clause-carrying row cannot fold into
        # it and leads itself; the oldest row folds into the newest.
        assert len(pairs) == 1 and pairs[0][0] == newest

    def test_a_legacy_row_with_an_in_band_clause_stays(self, store):
        _put(store, "Lint before you commit", _vec(0.0), negative="on hotfixes", legacy=True)
        _put(store, "Lint before you commit", _vec(0.0))
        assert _pairs(store) == []
        out, _ = _block(store, "enforce")
        assert "on hotfixes" in out


class TestSameItem:
    """The shared identity rule, as the fold reads it."""

    def _item(self, key="k", text="t", scope=None, vec=None, space="s", taught=False):
        from kiro_crew import memory_item_identity as mii

        blob = struct.pack(f"{_DIM}f", *vec) if vec is not None else None
        return mii.Item(
            key=key,
            text=text,
            scope=scope,
            tier=mii.TIER_RULE,
            vector=mii.unit_vector(blob, _DIM),
            space=space,
            user_taught=taught,
        )

    def test_unit_vector_refuses_what_cannot_be_compared(self):
        from kiro_crew import memory_item_identity as mii

        assert mii.unit_vector(None, _DIM) is None
        assert mii.unit_vector(b"x", 0) is None
        assert mii.unit_vector(struct.pack("3f", 1.0, 0.0, 0.0), _DIM) is None
        assert mii.unit_vector(struct.pack(f"{_DIM}f", float("inf"), 0, 0, 0), _DIM) is None
        assert mii.unit_vector(struct.pack(f"{_DIM}f", 0, 0, 0, 0), _DIM) is None
        unit = mii.unit_vector(struct.pack(f"{_DIM}f", 3.0, 4.0, 0, 0), _DIM)
        assert [round(float(v), 6) for v in unit] == [0.6, 0.8, 0.0, 0.0]

    def test_unit_vector_answers_as_the_pure_python_formula_does(self):
        from kiro_crew import memory_item_identity as mii

        def formula(blob, dim):
            if not isinstance(blob, (bytes, bytearray, memoryview)) or dim <= 0:
                return None
            if len(bytes(blob)) != dim * 4:
                return None
            values = struct.unpack(f"{dim}f", bytes(blob))
            if not all(math.isfinite(v) for v in values):
                return None
            norm = math.sqrt(math.fsum(v * v for v in values))
            return None if norm == 0.0 else tuple(v / norm for v in values)

        for blob in (
            None,
            b"",
            struct.pack("3f", 1.0, 0.0, 0.0),
            struct.pack(f"{_DIM}f", float("nan"), 0, 0, 0),
            struct.pack(f"{_DIM}f", float("-inf"), 0, 0, 0),
            struct.pack(f"{_DIM}f", 0, 0, 0, 0),
            struct.pack(f"{_DIM}f", 3.0, 4.0, 0, 0),
            struct.pack(f"{_DIM}f", -1.5, 2.25, 1e-3, 7.0),
        ):
            want, got = formula(blob, _DIM), mii.unit_vector(blob, _DIM)
            assert (want is None) == (got is None)
            if want is not None:
                assert max(abs(float(a) - float(b)) for a, b in zip(want, got)) < 1e-12
        three_four = mii.unit_vector(struct.pack(f"{_DIM}f", 3.0, 4.0, 0, 0), _DIM)
        assert all(abs(float(a) - b) < 1e-12 for a, b in zip(three_four, (0.6, 0.8, 0.0, 0.0)))
        assert mii.unit_vector(struct.pack(f"{_DIM}f", 1, 0, 0, 0), 0) is None

    def test_same_item_rules(self):
        from kiro_crew import memory_item_identity as mii

        a = self._item(text="Lint first", vec=_vec(0.0))
        assert mii.same_item(a, self._item(text="lint  FIRST"))
        assert not mii.same_item(a, self._item(text="Lint first", scope="src/pkg"))
        assert mii.same_item(a, self._item(text="other", vec=_vec(0.1)))
        assert not mii.same_item(a, self._item(text="other", vec=_vec(0.4)))
        assert not mii.same_item(a, self._item(text="other", vec=_vec(0.0), space="t"))
        assert not mii.same_item(a, self._item(text="other", vec=_vec(0.0), space=None))
        assert mii.cosine(a, self._item(text="other")) is None
        assert mii.normalize_text(None) == ""

    def test_cosine_without_numpy_vectors(self):
        from kiro_crew import memory_item_identity as mii

        a = mii.Item(key="a", text="a", scope=None, tier="rule", vector=(1.0, 0.0), space="s")
        b = mii.Item(key="b", text="b", scope=None, tier="rule", vector=(0.6, 0.8), space="s")
        assert mii.cosine(a, b) == pytest.approx(0.6)

    def test_leader_fold_never_chains_or_folds_a_taught_row(self):
        from kiro_crew import memory_item_identity as mii

        a = self._item(key="a", text="A", vec=_vec(0.0))
        b = self._item(key="b", text="B", vec=_vec(0.3))
        c = self._item(key="c", text="C", vec=_vec(0.6))
        taught = self._item(key="t", text="T", vec=_vec(0.0), taught=True)
        groups = mii.leader_fold([a, b, c, taught])
        assert [(g[0].key, [i.key for i in g[1]]) for g in groups] == [
            ("a", ["b"]),
            ("c", []),
            ("t", []),
        ]


class TestLeaders:
    def test_the_stacked_leaders_answer_what_leader_of_answers(self):
        """The matrix shortcut picks the same leader as one same_item call per pair."""
        import random

        from kiro_crew import memory_item_identity as mii

        rng = random.Random(7)
        dim = 8
        rows = []
        for i in range(300):
            base = [rng.choice([1.0, 0.0, -1.0]) for _ in range(dim)]
            noisy = [x + rng.uniform(-0.15, 0.15) for x in base]
            rule = rng.choice(["Lint first", "lint  FIRST", "Test first", f"rule {i}"])
            rows.append(
                (
                    {
                        "key": f"lesson.{i:04d}",
                        "value_json": json.dumps({"rule": rule}),
                        "embedding": struct.pack(f"{dim}f", *noisy),
                        "source": "user_explicit" if i % 17 == 0 else "consolidation",
                    },
                    rule,
                )
            )
        got = memory_lesson_fold.fold_pairs(rows, rows, space="s", dim=dim)
        # Reference: the same visit order, one leader_of call per row.
        visit = sorted(rows, key=lambda e: e[0]["source"] != "user_explicit")
        leaders, want = [], []
        for row, rule in visit:
            item = mii.Item(
                key=row["key"],
                text=rule,
                scope=None,
                tier=mii.TIER_RULE,
                vector=mii.unit_vector(row["embedding"], dim),
                space="s",
                user_taught=row["source"] == "user_explicit",
            )
            leader = mii.leader_of(item, leaders)
            if leader is None:
                leaders.append(item)
            else:
                want.append((leader.key, item.key))
        assert want, "the sample should hold near copies"
        assert got == want

    def test_no_chaining(self, store):
        # Written oldest first: C, B, A. Visited newest first: A leads, B is near A
        # and folds, C is near B but not A, and B is no leader, so C stays.
        c = _put(store, "Rule C", _vec(0.6))
        b = _put(store, "Rule B", _vec(0.3))
        a = _put(store, "Rule A", _vec(0.0))
        assert _pairs(store) == [[a, b]]
        out, _ = _block(store, "enforce")
        assert "Rule A" in out and "Rule C" in out and "Rule B" not in out
        assert c


class TestTheFlag:
    @pytest.mark.parametrize("raw", [None, "", "ENFORCE", "on", 1, True, ["enforce"]])
    def test_an_unknown_value_reads_as_off(self, raw):
        assert lesson_fold_mode_from(raw) == "off"
        assert _build_memory_config({"lesson_fold": raw}).lesson_fold == "off"

    @pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
    def test_a_known_value_is_kept(self, mode):
        assert _build_memory_config({"lesson_fold": mode}).lesson_fold == mode

    def test_the_default_is_off(self):
        assert MemoryConfig().lesson_fold == "off"

    def test_an_unknown_value_renders_todays_block(self, store):
        _put(store, "Run the linter before every commit", _vec(0.0))
        _put(store, "Lint before you commit", _vec(0.1))
        assert _block(store, "sideways") == _block(store, "off")

    def test_explicit_recall_never_folds(self, store):
        _put(store, "Run the linter before every commit", _vec(0.0))
        _put(store, "Lint before you commit", _vec(0.1))
        with injection_receipts.collect() as receipts:
            out = store.get_lessons_context("", lesson_fold="enforce")
        assert "Run the linter before every commit" in out and STAGE not in receipts

    def test_a_recall_child_gets_the_mode(self):
        args = lessons_args(None, "q", 0, None, None, True, 0, 0, 0, "off", "enforce")
        assert args["lesson_fold"] == "enforce"


class TestTheRecord:
    def test_a_recall_child_record_replays_valid_pairs_only(self):
        with injection_receipts.collect() as sink:
            injection_receipts.replay(
                {
                    STAGE: {
                        "mode": "shadow",
                        "policy": 1,
                        "would_add": [],
                        "would_drop": ["lesson.b"],
                        "pairs": [["lesson.a", "lesson.b"], ["lesson.a"], [1, 2], "x"],
                    }
                }
            )
        assert sink[STAGE]["pairs"] == [["lesson.a", "lesson.b"]]

    def test_the_line_carries_the_pairs(self):
        receipts = {
            injection_receipts.LESSONS: {"ids": ["lesson.a"], "omitted": 0},
            STAGE: {
                "mode": "shadow",
                "policy": 1,
                "would_add": [],
                "would_drop": ["lesson.b"],
                "pairs": [["lesson.a", "lesson.b"]],
            },
        }
        line = injection_receipts.build_line(
            receipts,
            session_key="dashboard_chat-1-2",
            turn_index=1,
            user_msg_id="m-1",
            model="m",
            agent="kirocrew",
            phase="session_start",
            store="",
        )
        assert line[STAGE]["pairs"] == [["lesson.a", "lesson.b"]]


class TestBothRenderersPassTheMode:
    def _render(self, monkeypatch, mode: str, *, member: bool) -> dict:
        from unittest.mock import MagicMock

        from kiro_crew.context_assembly import store_admission

        monkeypatch.setattr(memory_lesson_fold, "configured_mode", lambda: mode)
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
        assert self._render(monkeypatch, mode, member=member)["lesson_fold"] == mode

    @pytest.mark.parametrize("member", [True, False])
    def test_off_passes_nothing_so_the_call_is_todays(self, monkeypatch, member):
        assert "lesson_fold" not in self._render(monkeypatch, "off", member=member)
