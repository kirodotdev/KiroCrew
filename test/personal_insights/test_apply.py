from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.personal_insights import insights_apply as apply_mod
from kiro_crew.personal_insights.insights_apply import (
    ApplyResult,
    check_overfit,
    do_it,
    undo,
)
from kiro_crew.personal_insights.insights_report import build_actions
from kiro_crew.personal_insights.insights_runs import RunRepository


class FakeLog:
    def __init__(self, sessions: dict[str, tuple[str, str]]) -> None:
        self._sessions = sessions

    def _read_metadata(self, key: str) -> dict:
        return {"title": self._sessions[key][0]}

    def read_messages(self, key: str) -> list[dict]:
        return [{"role": "user", "content": "x", "ts": self._sessions[key][1] + "T10:00:00+00:00"}]


class FakeResult:
    def __init__(self, outcome: str, reason: str | None = None) -> None:
        self.outcome = outcome
        self.reason = reason
        self.superseded: tuple[str, ...] = ()


class FakeStore:
    def __init__(self, rules: list[tuple[str, str | None]] | None = None) -> None:
        self.rows: list[dict] = []
        self.embed_fn = None
        for rule, negative in rules or []:
            self._add(rule, negative)

    def _add(self, rule: str, negative: str | None) -> None:
        value = {"rule": rule, "category": "preference", "negative": negative}
        self.rows.append({"key": f"lesson.{len(self.rows)}", "value_json": json.dumps(value)})

    def get_lessons(self, limit=None, offset=0):
        return list(self.rows)

    def count_lessons(self) -> int:
        return len(self.rows)

    def embed_lesson(self, rule: str):
        return None

    def write_lesson(self, rule, category, negative, source, **kwargs):
        for row in self.rows:
            if json.loads(row["value_json"])["rule"].lower() == rule.lower():
                return FakeResult("deduped", "exact_rule")
        self._add(rule, negative)
        return FakeResult("inserted")

    def delete_lesson(self, text, repo_scope=None, *, exact=False):
        before = len(self.rows)
        wanted = text.lower().strip()

        def rendered(row):
            v = json.loads(row["value_json"])
            return (
                (v["rule"] + (f" — NOT: {v['negative']}" if v.get("negative") else ""))
                .lower()
                .strip()
            )

        self.rows = [
            r for r in self.rows if (rendered(r) != wanted if exact else wanted not in rendered(r))
        ]
        return len(self.rows) < before


SYNTH = {
    "recognition": "r",
    "working": [{"claim_id": "w1", "text": "w", "session_keys": ["s1", "s2"]}],
    "friction": [
        {"claim_id": "f1", "text": "f", "consequence": "c", "session_keys": ["s1", "s2", "s3"]},
        {"claim_id": "f2", "text": "f2", "consequence": "c", "session_keys": ["s4"]},
    ],
    "recommendations": [
        {
            "action_id": "a1",
            "rank": 1,
            "title": "Verify before claiming",
            "action_class": "lesson_proposal",
            "behavior_predicate": "verification_omission_explicit",
            "why": "w",
            "claim_ids": ["f1"],
            "artifact": {
                "rule": "Re-read the artifact before claiming done.",
                "negative": "Do not trust the last tool call.",
                "text": None,
            },
            "expected_observation": "e",
            "verification": "v",
            "undo": "u",
        },
        {
            "action_id": "a2",
            "rank": 2,
            "title": "Single-session rule",
            "action_class": "lesson_proposal",
            "behavior_predicate": "retries_operation",
            "why": "w",
            "claim_ids": ["f2"],
            "artifact": {"rule": "Always retry twice.", "negative": None, "text": None},
            "expected_observation": "e",
            "verification": "v",
            "undo": "u",
        },
    ],
}

SESSIONS = {
    "s1": ("Build thing", "2026-10-01"),
    "s2": ("Fix other thing", "2026-10-03"),
    "s3": ("↳ Fork of Build thing", "2026-10-05"),
    "s4": ("Lonely", "2026-10-06"),
}


@pytest.fixture
def repo(tmp_path: Path) -> RunRepository:
    r = RunRepository(tmp_path / "runs.sqlite")
    run_id = r.start_run(window_days=30, cataloged=10, selected=4)
    r.record_claims(
        run_id,
        [
            {
                "claim_id": c["claim_id"],
                "dimension": s,
                "text": c["text"],
                "session_keys": c["session_keys"],
            }
            for s in ("working", "friction")
            for c in SYNTH[s]
        ],
    )
    r.record_actions(run_id, build_actions(SYNTH), baseline_sessions=4)
    yield r
    r.close()


def _ids(repo: RunRepository) -> dict[str, str]:
    return {
        row[0].split("-")[0]: row[0]
        for row in repo.db.execute("SELECT action_id FROM actions").fetchall()
    }


def test_gate_counts_forks_as_one_lineage_and_passes_independent_evidence():
    chk = check_overfit(
        log=FakeLog(SESSIONS),
        store=FakeStore(),
        session_keys=["s1", "s2", "s3"],
        rule="Re-read",
        negative="",
    )
    assert chk.independent_lineages == 2
    assert chk.distinct_days == 3
    assert chk.passed


def test_gate_holds_single_session_evidence():
    chk = check_overfit(
        log=FakeLog(SESSIONS),
        store=FakeStore(),
        session_keys=["s4"],
        rule="Always retry twice.",
        negative="",
    )
    assert not chk.passed
    assert any("supporting session" in r for r in chk.reasons)
    assert any("lineage" in r for r in chk.reasons)


def test_gate_holds_when_existing_guidance_overlaps_keyword_fallback():
    store = FakeStore(
        [
            (
                "Re-read the changed artifact before claiming the task done.",
                "Do not trust the last tool call.",
            )
        ]
    )
    chk = check_overfit(
        log=FakeLog(SESSIONS),
        store=store,
        session_keys=["s1", "s2"],
        rule="Re-read the artifact before claiming done.",
        negative="Do not trust the last tool call.",
    )
    assert chk.duplicate_method == "keyword"
    assert chk.guidance_overlap >= apply_mod.DUPLICATE_OVERLAP
    assert not chk.passed


def test_do_it_applies_verifies_records_and_undo_removes(repo: RunRepository):
    store = FakeStore()
    ids = _ids(repo)
    result = do_it(ids["a1"], repo=repo, store=store, log=FakeLog(SESSIONS))
    assert result.state == "applied_verified"
    assert result.verified and result.undo_available
    assert store.count_lessons() == 1
    rec = repo.get_action(ids["a1"])
    assert rec["state"] == "applied_verified"
    assert rec["applied_at"] and rec["verified_at"]
    assert rec["target_identity"].startswith("lesson:")
    assert rec["baseline_sessions"] == 4
    assert repo.prior_applied_actions()[0]["action_id"] == ids["a1"]

    undone = undo(ids["a1"], repo=repo, store=store)
    assert undone.state == "undone"
    assert store.count_lessons() == 0
    assert repo.get_action(ids["a1"])["state"] == "undone"
    assert repo.get_action(ids["a1"])["undone_at"]
    assert repo.prior_applied_actions() == []


def test_do_it_holds_overfit_and_writes_nothing(repo: RunRepository):
    store = FakeStore()
    ids = _ids(repo)
    result = do_it(ids["a2"], repo=repo, store=store, log=FakeLog(SESSIONS))
    assert result.state == "held_overfit"
    assert store.count_lessons() == 0
    assert repo.get_action(ids["a2"])["state"] == "held_overfit"


def test_force_overrides_gate_but_records_it(repo: RunRepository):
    store = FakeStore()
    ids = _ids(repo)
    result = do_it(ids["a2"], repo=repo, store=store, log=FakeLog(SESSIONS), force=True)
    assert result.state == "applied_verified"
    assert result.overfit is not None and not result.overfit.passed


def test_second_apply_is_idempotent(repo: RunRepository):
    store = FakeStore()
    ids = _ids(repo)
    do_it(ids["a1"], repo=repo, store=store, log=FakeLog(SESSIONS))
    again = do_it(ids["a1"], repo=repo, store=store, log=FakeLog(SESSIONS))
    assert again.message == "Already applied."
    assert store.count_lessons() == 1


def test_store_dedup_is_reported_not_hidden(repo: RunRepository):
    store = FakeStore([("Re-read the artifact before claiming done.", None)])
    ids = _ids(repo)
    result = do_it(ids["a1"], repo=repo, store=store, log=FakeLog(SESSIONS))
    assert result.state in ("held_overfit", "held_duplicate")
    assert store.count_lessons() == 1


def test_undo_without_apply_is_refused(repo: RunRepository):
    ids = _ids(repo)
    result = undo(ids["a1"], repo=repo, store=FakeStore())
    assert isinstance(result, ApplyResult)
    assert result.state == "proposed"


def test_copy_first_classes_are_not_applied(repo: RunRepository):
    synth = dict(SYNTH)
    synth["recommendations"] = [
        {
            **SYNTH["recommendations"][0],
            "action_id": "a9",
            "action_class": "prompt",
            "artifact": {"rule": None, "negative": None, "text": "Operating rules..."},
        }
    ]
    run_id = repo.start_run(window_days=30, cataloged=1, selected=1)
    repo.record_actions(run_id, build_actions(synth), baseline_sessions=1)
    action_id = [a["action_id"] for a in build_actions(synth)][0]
    result = do_it(action_id, repo=repo, store=FakeStore(), log=FakeLog(SESSIONS))
    assert "copy-first" in result.message
