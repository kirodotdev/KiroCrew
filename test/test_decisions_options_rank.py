"""``options.rank``: its consent scope, question registry, combiner and hooks.

The point is SHADOW-only. It asks the registry's questions about a finalized
owner-dashboard reply's ``[OPTIONS:]`` chips, logs the answers and a combined
recommendation, and records the owner's next pick as a label. These tests pin what
it may send, what it writes, and that it leaves the reply exactly as it was.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.decisions import consent, gate
from kiro_crew.decisions.points import options_rank as rank
from kiro_crew.decisions.types import Answer, Choice

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


@pytest.fixture
def keystone(tmp_path, monkeypatch):
    path = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)
    return path


def _config():
    """A snapshot the gate can read: consent is on, the bucket admits everyone."""
    provider = SimpleNamespace(endpoint=DEFAULT_ENDPOINT, model="", timeout_ms=1000, api_key="")
    return SimpleNamespace(
        decisions=SimpleNamespace(bucket=100, provider=provider, history_budget_chars=0)
    )


def _consent(keystone, **scopes):
    keystone.write_text(
        json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT, **scopes}), encoding="utf-8"
    )


# ── the consent scope ──────────────────────────────────────────────────────────


class TestTheScope:
    def test_an_absent_scope_reads_as_not_consented(self, keystone):
        _consent(keystone)
        assert consent.permits(DEFAULT_ENDPOINT) is True
        assert consent.consented_options_text() is False

    @pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
    def test_only_a_literal_true_consents(self, keystone, value):
        _consent(keystone, options_text=value)
        assert consent.consented_options_text() is False

    def test_a_literal_true_consents(self, keystone):
        _consent(keystone, options_text=True)
        assert consent.consented_options_text() is True

    def test_no_other_scope_grants_it(self, keystone):
        _consent(keystone, tool_args=True, compaction=True, memory_text=True, nudge_evidence=True)
        assert consent.consented_options_text() is False

    def test_the_writer_records_keeps_and_clears_it(self, keystone):
        state = consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT)
        assert state["options_text"] is False
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, options_text=True)
        assert json.loads(keystone.read_text())["options_text"] is True
        consent.save_enabled(
            True, endpoint=DEFAULT_ENDPOINT, options_text=consent.KEEP_OPTIONS_TEXT
        )
        assert consent.consented_options_text() is True
        consent.save_enabled(False, endpoint=DEFAULT_ENDPOINT)
        assert consent.consented_options_text() is False

    @pytest.mark.parametrize("bad", ["true", 1, None])
    def test_a_non_boolean_scope_is_refused(self, keystone, bad):
        with pytest.raises(ValueError):
            consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, options_text=bad)

    def test_the_gate_refuses_the_point_without_it(self, keystone):
        gate._unscoped_warned.clear()
        _consent(keystone)
        assert gate.is_enabled("skills.select", config=_config()) is True
        assert gate.is_enabled("options.rank", config=_config()) is False
        _consent(keystone, options_text=True)
        assert gate.is_enabled("options.rank", config=_config()) is True
        gate._unscoped_warned.clear()

    def test_the_point_reports_its_scope_key(self):
        assert gate.POINT_SCOPE_KEYS["options.rank"] == consent.STATE_KEY_OPTIONS_TEXT


def _request(body=None):
    """A request shaped like a real dashboard OWNER call to the consent route."""
    request = MagicMock()
    request.path = "/api/decisions/consent"
    store = {"app": "", "user": "owner-1"}
    request.get = lambda key, default=None: store.get(key, default)
    request.__contains__ = lambda _self, key: key in store
    request.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = "owner-1"
    request.app = {"state": state}
    request.query = {}
    request.json = AsyncMock(return_value=body if body is not None else {})
    return request


@pytest.fixture
def quiet_route(monkeypatch):
    import kiro_crew.dashboard.handlers as handlers_pkg

    monkeypatch.setattr(handlers_pkg, "sel", lambda: MagicMock())
    monkeypatch.setattr(gate, "configured_endpoint", lambda *_a, **_kw: DEFAULT_ENDPOINT)
    monkeypatch.setattr(
        "kiro_crew.decisions.capability.is_decisions_denied", lambda *_a, **_kw: False
    )


class TestTheRoute:
    @pytest.mark.asyncio
    async def test_the_get_reports_the_scope_and_the_point_row(self, keystone, quiet_route):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_get

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, options_text=True)
        body = json.loads((await api_decisions_consent_get(_request())).text)
        assert body["options_text"] is True
        row = next(r for r in body["points"] if r["id"] == "options.rank")
        assert row["needs_scope"] == "options_text"

    @pytest.mark.asyncio
    async def test_a_scope_only_put_records_it_and_an_omission_keeps_it(
        self, keystone, quiet_route
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT)
        resp = await api_decisions_consent_put(_request({"options_text": True}))
        assert resp.status == 200
        assert consent.consented_options_text() is True
        resp = await api_decisions_consent_put(_request({"tool_args": True}))
        assert resp.status == 200
        assert consent.consented_options_text() is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["true", 1, [], {}])
    async def test_a_truthy_stand_in_is_a_400(self, keystone, quiet_route, bad):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        resp = await api_decisions_consent_put(
            _request({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "options_text": bad})
        )
        assert resp.status == 400
        assert consent.consented_options_text() is False


# ── question definitions ───────────────────────────────────────────────────────


def _def(**over):
    raw = {
        "id": "custom",
        "version": 1,
        "prompt": "Which is cheapest?",
        "shape": "menu_choice",
        "inputs": ["reply_text"],
        "role": "score",
    }
    raw.update(over)
    return raw


class TestParseQuestion:
    def test_a_minimal_score_question_parses_with_defaults(self):
        q = rank.parse_question(_def())
        assert (q.key, q.weight, q.direction, q.none_option) == (
            "custom@1",
            1.0,
            "higher_is_better",
            False,
        )

    @pytest.mark.parametrize(
        "over",
        [
            {"id": "Bad-Id"},
            {"id": "x" * 41},
            {"version": 0},
            {"version": True},
            {"version": "1"},
            {"prompt": ""},
            {"prompt": "x" * 301},
            {"shape": "ranking"},
            {"none_option": "yes"},
            {"shape": "per_chip_yes_no", "none_option": True},
            {"inputs": ["reply_text", "reply_text"]},
            {"inputs": ["preferences"]},
            {"inputs": ["transcript"]},
            {"inputs": "reply_text"},
            {"role": "rank"},
            {"weight": 0},
            {"weight": 11},
            {"weight": True},
            {"direction": "up"},
            {"threshold": 0.5},
            {"role": "flag", "weight": 1.0},
            {"role": "veto", "threshold": 0},
            {"role": "veto", "threshold": 1.5},
            {"strip_label": "x" * 41},
            {"surprise": 1},
        ],
    )
    def test_an_invalid_definition_is_refused(self, over):
        with pytest.raises(rank.QuestionError):
            rank.parse_question(_def(**over))

    def test_a_non_object_is_refused(self):
        with pytest.raises(rank.QuestionError):
            rank.parse_question(["id", "custom"])


class TestBuiltins:
    def test_the_starter_set(self):
        by_id = {q.id: q for q in rank.BUILTINS}
        assert list(by_id) == ["owner_pick", "goal_progress", "risk"]
        assert by_id["owner_pick"].role == "score"
        assert set(by_id["owner_pick"].inputs) == {"reply_text", "past_picks"}
        assert by_id["goal_progress"].role == "score"
        assert by_id["goal_progress"].none_option is True
        assert "goal" in by_id["goal_progress"].inputs
        assert by_id["risk"].role == "flag"

    def test_goal_progress_is_skipped_without_a_goal(self):
        asked, skipped = rank.active_questions(rank.BUILTINS, goal="")
        assert [q.id for q in asked] == ["owner_pick", "risk"]
        assert [q.id for q in skipped] == ["goal_progress"]
        asked, skipped = rank.active_questions(rank.BUILTINS, goal="ship it")
        assert skipped == []


class TestRegistry:
    def test_an_absent_directory_is_the_built_ins(self, tmp_path):
        assert rank.load_registry(tmp_path / "missing") == rank.BUILTINS

    def test_valid_files_add_or_replace_and_invalid_ones_are_skipped(self, tmp_path, caplog):
        (tmp_path / "a_cost.json").write_text(json.dumps(_def(id="cost")), encoding="utf-8")
        (tmp_path / "b_risk.json").write_text(
            json.dumps(
                {
                    "id": "risk",
                    "version": 2,
                    "prompt": "Which one deletes data?",
                    "shape": "per_chip_yes_no",
                    "inputs": ["reply_text"],
                    "role": "veto",
                    "threshold": 0.9,
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "c_bad.json").write_text(json.dumps(_def(id="BAD")), encoding="utf-8")
        (tmp_path / "d_junk.json").write_text("{not json", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
        with caplog.at_level("WARNING"):
            loaded = rank.load_registry(tmp_path)
        assert [q.key for q in loaded] == ["owner_pick@1", "goal_progress@1", "risk@2", "cost@1"]
        assert "c_bad.json" in caplog.text and "d_junk.json" in caplog.text

    def test_a_link_is_not_followed(self, tmp_path):
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps(_def(id="linked")), encoding="utf-8")
        registry = tmp_path / "questions"
        registry.mkdir()
        (registry / "linked.json").symlink_to(outside)
        assert "linked" not in {q.id for q in rank.load_registry(registry)}

    def test_the_total_is_capped(self, tmp_path):
        for n in range(8):
            (tmp_path / f"q{n}.json").write_text(json.dumps(_def(id=f"q{n}")), encoding="utf-8")
        assert len(rank.load_registry(tmp_path)) == rank.MAX_QUESTIONS


# ── state and questions ────────────────────────────────────────────────────────

REPLY = "Done with the refactor.\n\n[OPTIONS: Run the tests | Open a PR | Stop here]"
LABELS = ["Run the tests", "Open a PR", "Stop here"]


class TestState:
    def test_labels_come_from_the_last_marker(self):
        text = "[OPTIONS: old | older]\nmore\n[OPTIONS: a | b | a]"
        assert rank.labels_of(text) == ["a", "b"]

    def test_the_reply_is_sent_without_its_marker(self):
        assert rank.reply_text(REPLY) == "Done with the refactor."

    def test_past_picks_rebuild_from_the_transcript(self):
        messages = [
            {"role": "assistant", "content": "x\n[OPTIONS: A | B]"},
            {"role": "user", "content": " B "},
            {"role": "assistant", "content": "y\n[OPTIONS: C | D]"},
            {"role": "user", "content": "something else"},
            {"role": "assistant", "content": "z\n[OPTIONS: E | F]"},
            {"role": "tool", "content": "E"},
            {"role": "assistant", "content": "no options here"},
            {"role": "user", "content": "E"},
            {"role": "assistant", "content": "w\n[OPTIONS: G | H]"},
            {"role": "user", "content": "H"},
        ]
        assert rank.past_picks(messages) == [
            {"offered": ["A", "B"], "picked": "B"},
            {"offered": ["G", "H"], "picked": "H"},
        ]

    def test_past_picks_keep_the_newest_few(self):
        messages = []
        for n in range(8):
            messages += [
                {"role": "assistant", "content": f"[OPTIONS: a{n} | b{n}]"},
                {"role": "user", "content": f"a{n}"},
            ]
        picks = rank.past_picks(messages)
        assert [p["picked"] for p in picks] == [f"a{n}" for n in range(3, 8)]

    def test_the_state_carries_only_the_asked_questions_inputs(self):
        asked, _ = rank.active_questions(rank.BUILTINS, goal="")
        state = rank.build_state(
            asked, labels=LABELS, reply="r", picks=[{"offered": ["a"], "picked": "a"}], goal=""
        )
        assert set(state) == {"options", "reply", "past_picks"}
        only_risk = [q for q in rank.BUILTINS if q.id == "risk"]
        assert set(rank.build_state(only_risk, labels=LABELS, reply="r", picks=[], goal="g")) == {
            "options",
            "reply",
        }


class TestQuestionBuild:
    def test_menu_and_per_chip_shapes(self):
        chip = rank.parse_question(_def(id="lint", shape="per_chip_yes_no", role="flag"))
        asked = [q for q in rank.BUILTINS if q.id in ("owner_pick", "goal_progress")] + [chip]
        questions = rank.build_questions(asked, LABELS)
        by_id = {q.id: q for q in questions}
        assert by_id["owner_pick"].options == LABELS
        assert by_id["goal_progress"].options == LABELS + [rank.NONE_ID]
        assert [by_id[f"lint.{i}"].options for i in range(3)] == [["yes", "no"]] * 3
        assert "Open a PR" in by_id["lint.1"].prompt
        assert all(isinstance(q, Choice) for q in questions)

    def test_the_none_id_cannot_be_a_label(self):
        """A label never contains the marker's separator, and the reserved id does."""
        assert "|" in rank.NONE_ID
        assert rank.labels_of(f"[OPTIONS: {rank.NONE_ID} | x]") == ["none of these", "x"]


# ── combiner ───────────────────────────────────────────────────────────────────


def _reading(qid, per_label, *, role="score", none_p=None, picked_none=False, **over):
    raw = _def(id=qid, role=role, **over)
    if role != "score":
        raw.setdefault("threshold", 0.5)
    return rank.Reading(
        question=rank.parse_question(raw),
        per_label=per_label,
        none_p=none_p,
        picked_none=picked_none,
    )


class TestCombine:
    def test_the_product_rewards_agreement(self):
        combined = rank.combine([_reading("a", [0.6, 0.3, 0.1]), _reading("b", [0.5, 0.4, 0.1])], 3)
        assert combined.recommendation == 0
        assert combined.disagree is False
        assert sum(combined.scores) == pytest.approx(1.0)
        assert combined.scores[0] == pytest.approx(0.30 / (0.30 + 0.12 + 0.01))

    def test_disagreement_is_reported(self):
        combined = rank.combine(
            [_reading("owner_pick", [0.7, 0.2, 0.1]), _reading("goal", [0.1, 0.8, 0.1])], 3
        )
        assert combined.disagree is True
        assert combined.top1 == {"owner_pick@1": 0, "goal@1": 1}

    def test_a_near_zero_sinks_an_option(self):
        combined = rank.combine([_reading("a", [0.9, 0.1]), _reading("b", [0.001, 0.999])], 2)
        assert combined.recommendation == 1

    def test_weight_and_direction(self):
        cost = _reading("cost", [0.9, 0.1], direction="lower_is_better", weight=2.0)
        combined = rank.combine([_reading("a", [0.6, 0.4]), cost], 2)
        assert combined.recommendation == 1
        assert combined.scores[1] == pytest.approx((0.4 * 0.9**2) / (0.4 * 0.9**2 + 0.6 * 0.1**2))

    def test_a_veto_removes_a_chip_and_a_flag_only_badges(self):
        combined = rank.combine(
            [
                _reading("a", [0.8, 0.1, 0.1]),
                _reading("gone", [0.95, 0.0, 0.0], role="veto"),
                _reading("risky", [0.0, 0.7, 0.3], role="flag"),
            ],
            3,
        )
        assert combined.vetoed == [0]
        assert combined.scores[0] is None
        assert combined.recommendation in (1, 2)
        assert combined.flags == [("risky@1", 1)]

    def test_none_of_these_is_a_signal(self):
        combined = rank.combine(
            [
                _reading("a", [0.6, 0.4]),
                _reading("goal", [0.1, 0.1], none_p=0.8, picked_none=True, none_option=True),
            ],
            2,
        )
        assert combined.none_signal == ["goal@1"]
        assert combined.recommendation == 0

    def test_a_flag_picking_none_is_not_a_none_signal(self):
        combined = rank.combine(
            [
                _reading("a", [0.6, 0.4]),
                _reading("r", [0.1, 0.1], role="flag", none_option=True, picked_none=True),
            ],
            2,
        )
        assert combined.none_signal == []

    def test_no_score_question_means_no_recommendation(self):
        combined = rank.combine([_reading("r", [0.9, 0.1], role="flag")], 2)
        assert combined.recommendation is None


class TestReadAnswers:
    def test_full_maps_and_the_fallback_without_one(self):
        owner = next(q for q in rank.BUILTINS if q.id == "owner_pick")
        goal = next(q for q in rank.BUILTINS if q.id == "goal_progress")
        chip = rank.parse_question(_def(id="lint", shape="per_chip_yes_no", role="flag"))
        answers = {
            "owner_pick": Answer(
                "owner_pick",
                "Open a PR",
                0.5,
                probabilities={"Run the tests": 0.3, "Open a PR": 0.5, "Stop here": 0.2},
            ),
            "goal_progress": Answer("goal_progress", rank.NONE_ID, 0.7),
            "lint.0": Answer("lint.0", "yes", 0.9),
            "lint.1": Answer("lint.1", "no", 0.8),
            "lint.2": Answer("lint.2", "no", 0.6, probabilities={"yes": 0.4, "no": 0.6}),
        }
        readings = rank.read_answers([owner, goal, chip], LABELS, answers)
        assert readings[0].per_label == [0.3, 0.5, 0.2]
        assert readings[1].picked_none is True
        assert readings[1].none_p == pytest.approx(0.7)
        assert readings[1].per_label == pytest.approx([0.1, 0.1, 0.1])
        assert readings[2].per_label == pytest.approx([0.9, 0.2, 0.4])

    def test_a_missing_answer_reads_as_none(self):
        owner = next(q for q in rank.BUILTINS if q.id == "owner_pick")
        assert rank.read_answers([owner], LABELS, {}) is None

    def test_a_partial_map_is_not_read_as_zeros(self):
        # The Jev lane drops an entry it cannot read. The missing chip's probability is
        # unknown, not 0, so the chosen-probability fallback applies instead.
        owner = next(q for q in rank.BUILTINS if q.id == "owner_pick")
        answers = {
            "owner_pick": Answer(
                "owner_pick",
                "Open a PR",
                0.5,
                probabilities={"Open a PR": 0.5, "Stop here": 0.2},
            ),
        }
        readings = rank.read_answers([owner], LABELS, answers)
        assert readings[0].per_label == pytest.approx([0.25, 0.5, 0.25])

    def test_a_covering_map_is_normalized(self):
        owner = next(q for q in rank.BUILTINS if q.id == "owner_pick")
        answers = {
            "owner_pick": Answer(
                "owner_pick",
                "Open a PR",
                0.4,
                probabilities={"Run the tests": 0.2, "Open a PR": 0.4, "Stop here": 0.2},
            ),
        }
        readings = rank.read_answers([owner], LABELS, answers)
        assert readings[0].per_label == pytest.approx([0.25, 0.5, 0.25])


# ── the run, the refusal and the label ─────────────────────────────────────────

SESSION = "dashboard:chat-1"


class _Oracle:
    """Answers every question with its first option and a full map."""

    def __init__(self):
        self.states: list = []
        self.questions: list = []

    async def ask(self, state, questions):
        self.states.append(state)
        self.questions.append([q.id for q in questions])
        answers = {}
        for q in questions:
            n = len(q.options)
            first = 0.6 if n > 1 else 1.0
            probabilities = {
                o: (first if i == 0 else (1 - first) / (n - 1)) for i, o in enumerate(q.options)
            }
            answers[q.id] = Answer(q.id, q.options[0], first, probabilities=probabilities)
        return answers


@pytest.fixture
def run_env(keystone, tmp_path, monkeypatch):
    """A consented seam, a fake oracle, an empty registry and captured rows."""
    rank.reset_state()
    gate._unscoped_warned.clear()
    _consent(keystone, options_text=True)
    monkeypatch.setattr(gate, "_snapshot", _config)
    oracle = _Oracle()
    monkeypatch.setattr("kiro_crew.decisions.impl_jev.JevOracle", lambda _provider: oracle)
    log_dir = tmp_path / "decisions"
    monkeypatch.setattr("kiro_crew.decisions.log.log_dir", lambda: log_dir)
    rows: list = []

    def _append(row, **_kw):
        rows.append(row)
        return True

    monkeypatch.setattr("kiro_crew.decisions.log.append", _append)
    registry = tmp_path / "questions"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_questions_dir", lambda: registry)
    goal = {"value": ""}
    monkeypatch.setattr(rank, "_read_goal", lambda _key: goal["value"])
    env = SimpleNamespace(oracle=oracle, rows=rows, goal=goal, log_dir=log_dir, keystone=keystone)
    yield env
    rank.reset_state()
    gate._unscoped_warned.clear()


def _rank(history=()):
    return asyncio.run(rank.rank_options(SESSION, REPLY, LABELS, list(history)))


class TestRun:
    def test_one_request_carries_every_asked_question(self, run_env):
        run_env.goal["value"] = "ship the refactor"
        history = [
            {"role": "assistant", "content": "[OPTIONS: Run the tests | Skip]"},
            {"role": "user", "content": "Run the tests"},
        ]
        outcome = _rank(history)
        assert len(run_env.oracle.states) == 1, "all questions share ONE decide call"
        assert run_env.oracle.questions[0] == ["owner_pick", "goal_progress", "risk"]
        state = run_env.oracle.states[0]
        assert state["options"] == LABELS
        assert state["reply"] == "Done with the refactor."
        assert state["goal"] == "ship the refactor"
        assert state["past_picks"] == [
            {"offered": ["Run the tests", "Skip"], "picked": "Run the tests"}
        ]
        assert outcome["point"] == "options.rank"
        assert outcome["asked"] == ["owner_pick@1", "goal_progress@1", "risk@1"]
        assert outcome["dist.owner_pick@1"] == [0.6, 0.2, 0.2]
        assert outcome["dist.goal_progress@1"] == [0.6, 0.1333, 0.1333, 0.1333]
        assert outcome["recommendation"] == 0
        assert outcome["flags"] == ["risk@1:0"]
        assert outcome["disagree"] is False
        assert '"None"' not in json.dumps(outcome), "a list item never renders as text"
        # The gate's own decision row and the outcome row share the turn id.
        assert [r.get("turn_id") for r in run_env.rows] == [outcome["turn_id"]] * 2

    def test_goal_progress_is_not_asked_without_a_goal(self, run_env):
        outcome = _rank()
        assert run_env.oracle.questions[0] == ["owner_pick", "risk"]
        assert "goal" not in run_env.oracle.states[0]
        assert outcome["skipped"] == ["goal_progress@1"]

    def test_fewer_than_two_chips_asks_nothing(self, run_env):
        assert asyncio.run(rank.rank_options(SESSION, REPLY, ["only"], [])) is None
        assert run_env.oracle.states == [] and run_env.rows == []

    @pytest.mark.parametrize(
        "labels",
        [
            [f"Option {i}" for i in range(rank.MAX_LABELS + 1)],
            ["Run the tests", "x" * (rank.MAX_LABEL_CHARS + 1)],
        ],
        ids=["too-many-chips", "chip-too-long"],
    )
    def test_a_menu_it_would_have_to_cut_asks_nothing(self, run_env, labels):
        # A dropped or shortened chip could never match the owner's click, so the
        # pick would be recorded as typed text. The menu is left unscored instead.
        assert asyncio.run(rank.rank_options(SESSION, REPLY, labels, [])) is None
        assert run_env.oracle.states == [] and run_env.rows == []
        assert rank.record_pick(SESSION, labels[-1]) is None

    def test_an_unconsented_scope_sends_nothing_and_writes_nothing(self, run_env, monkeypatch):
        _consent(run_env.keystone)
        read: list = []
        monkeypatch.setattr(rank, "load_registry", lambda *a: read.append(a) or rank.BUILTINS)
        assert _rank() is None
        assert run_env.oracle.states == []
        assert run_env.rows == []
        assert read == [], "the registry is not read before the scope check"
        assert not run_env.log_dir.exists()
        assert rank.record_pick(SESSION, "Open a PR") is None

    def test_the_owner_s_pick_is_recorded_once(self, run_env):
        outcome = _rank()
        run_env.rows.clear()
        row = rank.note_send(SESSION, "Run the tests", owner=True)
        assert row["kind"] == "option_pick"
        assert row["turn_id"] == outcome["turn_id"]
        assert (row["picked"], row["picked_index"], row["agree_top1"]) == ("Run the tests", 0, True)
        assert row["scored"] == ["owner_pick@1"] and row["agree"] == [True]
        assert run_env.rows == [row]
        assert rank.record_pick(SESSION, "Run the tests") is None, "the record is cleared"

    def test_typed_text_is_a_label_with_no_pick(self, run_env):
        _rank()
        row = rank.record_pick(SESSION, "actually, do something else")
        assert (row["picked"], row["picked_index"], row["agree_top1"]) == (None, None, None)
        assert row["agree"] == []

    def test_someone_else_s_send_discards_the_record(self, run_env):
        _rank()
        assert rank.note_send(SESSION, "Run the tests", owner=False) is None
        assert rank.is_owner_turn(SESSION) is False
        assert rank.record_pick(SESSION, "Run the tests") is None

    def test_an_answer_that_lands_before_the_score_does_not_label_it(self, run_env, monkeypatch):
        """The owner already moved on, so the next message is not this reply's label."""
        real = rank._read_goal

        def _send_mid_run(key):
            rank.note_send(SESSION, "Open a PR", owner=True)
            return real(key)

        monkeypatch.setattr(rank, "_read_goal", _send_mid_run)
        assert _rank() is not None
        assert rank.record_pick(SESSION, "Stop here") is None

    def test_turn_ownership(self):
        rank.reset_state()
        assert rank.is_owner_turn(SESSION) is False
        rank.note_send(SESSION, "hi", owner=True)
        rank.note_turn(SESSION, user_turn=True)
        assert rank.is_owner_turn(SESSION) is True
        rank.note_turn(SESSION, user_turn=False)
        assert rank.is_owner_turn(SESSION) is False
        rank.reset_state()


# ── the hooks ──────────────────────────────────────────────────────────────────


def _dashboard_state(tmp_path):
    from unittest.mock import AsyncMock as _AsyncMock

    from chat_test_helpers import _make_ready_kiro_prerequisite

    from kiro_crew.dashboard.state import DashboardState
    from kiro_crew.history import ConversationLog

    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_slack_link = MagicMock(return_value=(None, None))
    sessions.get_mirror_link = MagicMock(return_value=None)
    sessions.get_provider = MagicMock(return_value=None)
    sessions.resumable_sid = MagicMock(return_value=None)
    sessions.reset = _AsyncMock()
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path / "history"),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    return state


def _flush(tmp_path, *, owner=True, memory_mode="persistent", text=REPLY):
    """Flush *text* as a finalized segment on a fresh slot; return (message, snapshot)."""
    import copy

    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.dashboard.state import _ChatSlot

    state = _dashboard_state(tmp_path)
    slot = _ChatSlot("chat-rank-1", memory_mode=memory_mode)
    slot._titled = True
    slot.messages.append({"role": "assistant", "content": "[OPTIONS: Run the tests | Skip]"})
    slot.messages.append({"role": "user", "content": "Skip"})
    rank.note_send(effective_session_key(slot), "Skip", owner=owner)

    async def _go():
        with patch.object(chat_runner, "sel", return_value=MagicMock()):
            chat_runner._flush_segment(state, slot, text)
        message = [m for m in slot.messages if m.get("role") == "assistant"][-1]
        snapshot = copy.deepcopy(message)
        scheduled = len(state._background_tasks)
        await asyncio.gather(*list(state._background_tasks), return_exceptions=True)
        return message, snapshot, scheduled

    return asyncio.run(_go())


class TestFinalizeHook:
    def test_an_owner_reply_is_scored_and_left_byte_identical(self, run_env, tmp_path):
        message, snapshot, scheduled = _flush(tmp_path)
        assert scheduled == 1
        assert message == snapshot, "scoring never touches the stored message"
        assert message["content"] == REPLY
        outcome = next(r for r in run_env.rows if "asked" in r)
        assert outcome["labels"] == LABELS
        # The earlier pick in the slot rode along; the reply itself did not.
        assert run_env.oracle.states[0]["past_picks"] == [
            {"offered": ["Run the tests", "Skip"], "picked": "Skip"}
        ]

    def test_the_stored_message_matches_an_unscored_flush(self, run_env, tmp_path):
        scored, _, _ = _flush(tmp_path / "a")
        unscored, _, scheduled = _flush(tmp_path / "b", owner=False)
        assert scheduled == 0
        drop = {"ts", "mid", "id"}

        def _stable(message):
            kept = {k: v for k, v in message.items() if k not in drop}
            kept["meta"] = {k: v for k, v in (message.get("meta") or {}).items() if k != "mid"}
            return kept

        assert _stable(scored) == _stable(unscored)

    @pytest.mark.parametrize("memory_mode", ["incognito", "temporary"])
    def test_a_restricted_slot_is_skipped(self, run_env, tmp_path, memory_mode):
        _, _, scheduled = _flush(tmp_path, memory_mode=memory_mode)
        assert scheduled == 0
        assert run_env.oracle.states == [] and run_env.rows == []

    def test_a_reply_without_options_is_skipped(self, run_env, tmp_path):
        _, _, scheduled = _flush(tmp_path, text="no chips here")
        assert scheduled == 0

    def test_a_turn_nobody_typed_is_skipped(self, run_env, tmp_path, monkeypatch):
        real = rank.note_send

        def _then_a_nudge(key, text, *, owner):
            row = real(key, text, owner=owner)
            rank.note_turn(key, user_turn=False)
            return row

        monkeypatch.setattr(rank, "note_send", _then_a_nudge)
        _, _, scheduled = _flush(tmp_path)
        assert scheduled == 0


def _send_request(*, user="owner-1", app=""):
    request = MagicMock()
    store = {"app": app, "user": user}
    request.get = lambda key, default=None: store.get(key, default)
    request.__contains__ = lambda _self, key: key in store
    request.__getitem__ = lambda _self, key: store[key]
    request.app = {"state": SimpleNamespace(owner_id="owner-1")}
    return request


class TestSendHook:
    def _slot(self, memory_mode="persistent"):
        from kiro_crew.dashboard.state import _ChatSlot

        return _ChatSlot("chat-send-1", memory_mode=memory_mode)

    def _pending(self, key):
        rank._pending[key] = {
            "seq": 1,
            "turn_id": "t1",
            "labels": ["A", "B"],
            "recommendation": 1,
            "top1": {"owner_pick@1": 1},
            "scored": ["owner_pick@1"],
            "created": __import__("time").monotonic(),
        }

    def test_the_owner_s_send_labels_the_pending_record(self, monkeypatch):
        from kiro_crew.dashboard import chat_handlers

        rank.reset_state()
        rows: list = []
        monkeypatch.setattr("kiro_crew.decisions.log.append", lambda row, **_k: rows.append(row))
        slot = self._slot()
        self._pending("dashboard:chat-send-1")
        chat_handlers._note_options_rank_send(_send_request(), slot, "B", "")
        assert [(r["kind"], r["picked"], r["agree_top1"]) for r in rows] == [
            ("option_pick", "B", True)
        ]
        assert rank.is_owner_turn("dashboard:chat-send-1") is True
        rank.reset_state()

    @pytest.mark.parametrize("request_kw", [{"user": "member-2"}, {"app": "crew"}])
    def test_anyone_else_s_send_labels_nothing(self, monkeypatch, request_kw):
        from kiro_crew.dashboard import chat_handlers

        rank.reset_state()
        rows: list = []
        monkeypatch.setattr("kiro_crew.decisions.log.append", lambda row, **_k: rows.append(row))
        self._pending("dashboard:chat-send-1")
        app = request_kw.get("app", "")
        chat_handlers._note_options_rank_send(_send_request(**request_kw), self._slot(), "B", app)
        assert rows == []
        assert rank.is_owner_turn("dashboard:chat-send-1") is False
        assert "dashboard:chat-send-1" not in rank._pending
        rank.reset_state()

    def test_a_restricted_slot_is_not_recorded(self, monkeypatch):
        from kiro_crew.dashboard import chat_handlers

        rank.reset_state()
        chat_handlers._note_options_rank_send(_send_request(), self._slot("incognito"), "B", "")
        assert rank._owner_turn == {} and rank._send_epoch == {}

    def test_the_send_route_records_before_it_dispatches(self):
        """Above every branch, so a steered, queued and fresh send all label."""
        import inspect

        from kiro_crew.dashboard import chat_handlers

        source = inspect.getsource(chat_handlers.api_chat)
        hook = source.index("_note_options_rank_send(")
        assert source.index('"message_required"') < hook < source.index("spawn_guarded_turn(")
