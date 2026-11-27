"""A new consolidation lesson is a standing rule only when the person's own words back it.

``kiro_crew.memory_tier_policy`` stores an untagged lesson, and an ``always`` lesson
whose ``user_quote`` is not found in a person's turn, as ``on_topic``. Under
``memory.lesson_tier_policy`` ``off`` nothing changes, ``shadow`` only keeps a
bounded log (with a re-teach signal from ``learn_add``), and ``enforce`` applies it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from consolidation_lease_helpers import lease_aware_log

from kiro_crew import memory_tier_policy as mtp
from kiro_crew.config.section_builders import _build_memory_config
from kiro_crew.history import HistoryConsolidator
from kiro_crew.vector_memory import VectorMemoryStore, _lesson_key

_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

PERSON = {
    "role": "user",
    "content": "No.  Please ALWAYS run the linter before every commit.",
    "meta": {"human": True},
}
RELAY = {
    "role": "user",
    "content": "[sent by session chat-1 via session_send]\n\nalways run the linter before every commit",
    "meta": {"relay": {"senderKey": "chat-1", "via": "session_send"}},
}
ASSISTANT = {"role": "assistant", "content": "always run the linter before every commit"}

QUOTE = "always run the linter before every commit"
QUOTED = {"rule": "Run the linter before a commit", "applies": "always", "user_quote": QUOTE}
GUESSED = {"rule": "Check the exit code of every build", "applies": "always"}
UNTAGGED = {"rule": "Prefer small pull requests", "negative": "one big PR"}
FINDING = {"rule": "The flaky test was a clock skew", "applies": "on_topic"}


#: The person's own words, but not what the rule says.
UNBACKED = {
    "rule": "Indent with tabs, not spaces",
    "applies": "always",
    "user_quote": "Please ALWAYS run the linter",
}


def _answer() -> dict:
    return {
        "history_entry": "x",
        "lessons": [dict(QUOTED), dict(GUESSED), dict(UNTAGGED), dict(FINDING)],
    }


def _embed(text: str) -> list[float]:
    """A stand-in embedder: one axis per topic word, so only same-topic texts agree."""
    low = text.lower()
    return [float(word in low) for word in ("linter", "stash", "tabs")] + [0.01]


def _quotes(answer: dict, rows: list, embed=_embed) -> mtp.QuoteRun:
    """The answer's own quotes, checked and scored, as enforce gathers them."""
    return asyncio.run(mtp.gather_quotes(answer, rows, ask=None, embed=embed))


def _v1(tmp_path, name: str = "m") -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / f"{name}.db", embedding_dim=4)
    store.init()
    return store


def _log(store: VectorMemoryStore) -> dict[str, Any]:
    with store._db_lock:
        rows = store.db.execute(
            "SELECT key, value FROM memory_meta WHERE key GLOB 'memory_tier_log:*'"
        ).fetchall()
    return {str(k): json.loads(v) if str(v)[:1] in "[{" else v for k, v in rows}


# the setting and the prompt


@pytest.mark.parametrize(
    "raw, mode",
    [("off", "off"), ("shadow", "shadow"), ("enforce", "enforce"), ("SHADOW", "off"), (1, "off")],
)
def test_the_setting_defaults_to_off(raw, mode) -> None:
    assert _build_memory_config({}).lesson_tier_policy == "off"
    assert _build_memory_config({"lesson_tier_policy": raw}).lesson_tier_policy == mode
    assert mtp.apply_tier_policy(_answer(), [PERSON], raw).mode == mode


def test_only_enforce_asks_for_a_quote() -> None:
    assert mtp.prompt_rule_for("off") == ""
    assert mtp.prompt_rule_for("shadow") == "", "shadow must not change what the model writes"
    assert mtp.prompt_rule_for("bogus") == ""
    assert mtp.prompt_rule_for("enforce") == mtp.QUOTE_INSTRUCTION
    assert "user_quote" in mtp.QUOTE_INSTRUCTION


# the quote check


@pytest.mark.parametrize(
    "quote, rows, want",
    [
        (QUOTE, [PERSON], "pass"),
        # NFKC, case folding and whitespace collapse.
        ("ＡＬＷＡＹＳ run   the\nlinter", [PERSON], "pass"),
        ("请总是先跑测试再提交", [{**PERSON, "content": "以后请总是先跑测试再提交。"}], "pass"),
        (None, [PERSON], "absent"),
        ("   ", [PERSON], "absent"),
        (42, [PERSON], "absent"),
        # Fewer than three significant tokens.
        ("No. Please", [PERSON], "fail"),
        ("run linter", [PERSON], "fail"),
        ("always skip the linter", [PERSON], "fail"),
        # Only a person's own, unrelayed turn counts, whatever it says.
        (QUOTE, [RELAY], "fail"),
        (QUOTE, [ASSISTANT], "fail"),
        (QUOTE, [{"role": "user", "content": PERSON["content"]}], "fail"),
        (QUOTE, [{**PERSON, "meta": {"human": True, "relay": {"senderKey": "a"}}}], "fail"),
        (QUOTE, [{**PERSON, "content": [{"type": "text", "text": QUOTE}]}], "fail"),
        (QUOTE, None, "fail"),
    ],
)
def test_the_quote_check(quote, rows, want) -> None:
    assert mtp.quote_check(quote, rows) == want


# the policy


def test_off_hands_back_the_answer_itself() -> None:
    answer = _answer()
    policy = mtp.apply_tier_policy(answer, [PERSON], "off")
    assert policy == mtp.TierPolicy(answer, "off")
    assert policy.result is answer and not policy.loggable


def test_shadow_writes_the_answer_unchanged_and_counts_the_measured_quotes() -> None:
    answer = _answer()
    rows = [PERSON, ASSISTANT]
    policy = mtp.apply_tier_policy(answer, rows, "shadow", quotes=_quotes(answer, rows))
    assert policy.result is answer and answer == _answer()
    assert (policy.lessons, policy.unstated, policy.always) == (4, 1, 2)
    assert (policy.quote_present, policy.quote_pass, policy.quote_fail) == (1, 1, 0)
    assert (policy.backed, policy.unmeasured, policy.support) == (1, 0, (("0.9", 1),))
    # The backed quote stays standing, as under enforce: shadow is no upper bound.
    assert policy.demoted == 2
    assert policy.demoted_keys == tuple(_lesson_key(i["rule"]) for i in (GUESSED, UNTAGGED))
    assert [label for _, label, _ in policy.labels] == ["absent", "unstated"]


def test_shadow_with_no_measurement_counts_unmeasured_not_demoted() -> None:
    answer = _answer()
    failed = mtp.QuoteRun(None, asked=True, failed=True)
    policy = mtp.apply_tier_policy(answer, [PERSON], "shadow", quotes=failed)
    assert (policy.always, policy.unmeasured, policy.quote_present) == (2, 2, 0)
    assert policy.demoted_keys == (_lesson_key(UNTAGGED["rule"]),)
    assert _lesson_key(QUOTED["rule"]) not in policy.kept_keys, "unmeasured is not a control"
    assert dict(policy.calls) == {"quote_calls": 1, "quote_call_failed": 1, "quote_cut": 0}


@pytest.mark.parametrize(
    "score, bucket",
    [(None, "none"), (0.1, "<0.4"), (0.4, "0.4"), (0.59, "0.5"), (0.6, "0.6"), (1.0, "0.9")],
)
def test_support_buckets(score, bucket) -> None:
    assert mtp.support_bucket(score) == bucket


def test_a_verbatim_quote_must_also_back_its_rule() -> None:
    answer = {"lessons": [dict(QUOTED), dict(UNBACKED)]}
    quotes = _quotes(answer, [PERSON])
    assert quotes.evidence[QUOTED["rule"]].backed
    unbacked = quotes.evidence[UNBACKED["rule"]]
    assert unbacked.outcome == "pass" and not unbacked.backed
    assert unbacked.score is not None and unbacked.score < mtp.SUPPORT_COSINE
    policy = mtp.apply_tier_policy(answer, [PERSON], "enforce", quotes=quotes)
    assert [i["applies"] for i in policy.result["lessons"]] == ["always", "on_topic"]
    assert (policy.quote_pass, policy.backed, policy.demoted) == (2, 1, 1)
    assert policy.labels == ((_lesson_key(UNBACKED["rule"]), "pass", "<0.4"),)


def test_with_no_embedder_no_quote_backs_its_rule() -> None:
    answer = {"lessons": [dict(QUOTED)]}
    for embed in (None, lambda _t: None, lambda _t: 1 / 0):
        quotes = _quotes(answer, [PERSON], embed=embed)
        assert quotes.evidence[QUOTED["rule"]] == mtp.QuoteEvidence("pass", None)
        policy = mtp.apply_tier_policy(answer, [PERSON], "enforce", quotes=quotes)
        assert policy.result["lessons"][0]["applies"] == "on_topic", "fails toward a finding"
        assert policy.support == (("none", 1),)


def test_enforce_with_no_evidence_demotes_every_standing_rule() -> None:
    policy = mtp.apply_tier_policy(_answer(), [PERSON], "enforce")
    assert {i["applies"] for i in policy.result["lessons"]} == {"on_topic"}
    assert (policy.demoted, policy.unmeasured) == (3, 0)


def test_enforce_keeps_only_a_quoted_rule_standing() -> None:
    answer = _answer()
    rows = [PERSON, ASSISTANT]
    policy = mtp.apply_tier_policy(answer, rows, "enforce", quotes=_quotes(answer, rows))
    assert answer == _answer(), "the model's answer is not mutated"
    assert [i.get("applies") for i in policy.result["lessons"]] == [
        "always",
        "on_topic",
        "on_topic",
        "on_topic",
    ]
    assert policy.result["lessons"][0] is answer["lessons"][0]
    assert "negative" not in policy.result["lessons"][2], "a demoted lesson loses its NOT-clause"
    assert [i["rule"] for i in policy.result["lessons"]] == [
        i["rule"] for i in answer["lessons"]
    ], "nothing is dropped"
    assert (policy.demoted, policy.quote_pass) == (2, 1)
    assert policy.result["history_entry"] == "x"


def test_enforce_demotes_a_quote_only_an_agent_said() -> None:
    answer = {"lessons": [dict(QUOTED)]}
    rows = [RELAY, ASSISTANT]
    policy = mtp.apply_tier_policy(answer, rows, "enforce", quotes=_quotes(answer, rows))
    assert policy.result["lessons"][0]["applies"] == "on_topic"
    assert (policy.quote_present, policy.quote_fail, policy.demoted) == (1, 1, 1)


def test_a_channel_session_is_left_as_it_is() -> None:
    answer = _answer()
    for key in ("discord:bot:direct:u1", "slack:123.456", "telegram_99"):
        policy = mtp.apply_tier_policy(answer, [PERSON], "enforce", session_key=key)
        assert policy.result is answer and not policy.loggable, key


# the bounded log


def test_the_log_counts_by_mode_and_keeps_ids_only_in_shadow(tmp_path) -> None:
    store = _v1(tmp_path)
    answer = {"lessons": [*_answer()["lessons"], dict(UNBACKED)]}
    quotes = _quotes(answer, [PERSON])
    try:
        shadow = mtp.apply_tier_policy(answer, [PERSON], "shadow", quotes=quotes)
        assert mtp.record_tier_policy(store, shadow, now=_T0)
        enforce = mtp.apply_tier_policy(answer, [PERSON], "enforce", quotes=quotes)
        assert mtp.record_tier_policy(store, enforce, now=_T0)
        assert not mtp.record_tier_policy(
            store, mtp.apply_tier_policy(answer, [PERSON], "off"), now=_T0
        )
        log = _log(store)
    finally:
        store.close()
    row = {
        "passes": 1,
        "lessons": 5,
        "unstated": 1,
        "always": 3,
        "quote_present": 2,
        "quote_pass": 2,
        "quote_fail": 0,
        "backed": 1,
        "unmeasured": 0,
        "demoted": 3,
        "support_0.9": 1,
        "support_<0.4": 1,
        "quote_calls": 0,
        "quote_call_failed": 0,
        "quote_cut": 0,
    }
    assert log[mtp.COUNTS_PREFIX + "2026-10-01"] == {
        "shadow": {"consolidation": row},
        "enforce": {"consolidation": row},
    }, "shadow counts what enforce does"
    ids = log[mtp.IDS_PREFIX + "2026-10-01"]
    assert [i["key"] for i in ids] == list(shadow.demoted_keys), "enforce adds no ids"
    assert [(i["quote"], i.get("support")) for i in ids] == [
        ("absent", None),
        ("unstated", None),
        ("pass", "<0.4"),
    ]
    assert all(set(i) - {"support"} == {"key", "path", "rules", "at", "quote"} for i in ids)
    assert all(i["at"] == _T0.isoformat() and i["rules"] == 2 for i in ids)
    assert log[mtp.RULES_VERSION_KEY] == "2"
    assert log[mtp.RULES_CHANGED_KEY] == _T0.isoformat()
    text = json.dumps(log, ensure_ascii=False)
    assert QUOTE not in text and GUESSED["rule"] not in text and UNBACKED["user_quote"] not in text


def test_a_new_rules_version_stamps_its_change_on_the_first_counter_write(tmp_path) -> None:
    store = _v1(tmp_path)
    try:
        with store._db_lock:
            for key, value in ((mtp.RULES_VERSION_KEY, "1"), (mtp.SHADOW_STARTED_KEY, "x")):
                store.db.execute(
                    "INSERT INTO memory_meta (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, "2026-09-01T00:00:00+00:00"),
                )
            store.db.commit()
        later = _T0 + timedelta(hours=3)
        enforce = mtp.apply_tier_policy({"lessons": [dict(GUESSED)]}, [PERSON], "enforce")
        assert mtp.record_tier_policy(store, enforce, now=_T0)
        assert mtp.record_tier_policy(store, enforce, now=later)
        log = _log(store)
        # The id window is cleared, not started: enforce keeps no ids, so a later
        # shadow run still gets its whole window from its first id write.
        assert mtp.SHADOW_STARTED_KEY not in log
        shadow_at = _T0 + timedelta(days=20)
        shadow = mtp.apply_tier_policy({"lessons": [dict(UNTAGGED)]}, [PERSON], "shadow")
        assert mtp.record_tier_policy(store, shadow, now=shadow_at)
        after = _log(store)
    finally:
        store.close()
    assert log[mtp.RULES_VERSION_KEY] == str(mtp.RULES_VERSION)
    assert log[mtp.RULES_CHANGED_KEY] == _T0.isoformat()
    assert after[mtp.SHADOW_STARTED_KEY] == shadow_at.isoformat()
    assert [i["key"] for i in after[mtp.IDS_PREFIX + shadow_at.date().isoformat()]] == [
        _lesson_key(UNTAGGED["rule"])
    ]


def _many(n: int, start: int = 0) -> mtp.TierPolicy:
    keys = tuple(f"lesson.k{i}" for i in range(start, start + n))
    return mtp.TierPolicy(
        {}, "shadow", n, n, 0, 0, 0, 0, n, keys, labels=tuple((k, "unstated", None) for k in keys)
    )


def test_ids_stop_at_the_daily_cap_and_after_the_window(tmp_path) -> None:
    store = _v1(tmp_path)
    try:
        mtp.record_tier_policy(store, _many(100), now=_T0)
        mtp.record_tier_policy(store, _many(100), now=_T0 + timedelta(hours=1))
        assert len(_log(store)[mtp.IDS_PREFIX + "2026-10-01"]) == mtp.IDS_PER_DAY
        late = _T0 + timedelta(days=15)
        mtp.record_tier_policy(store, _many(5), now=late)
        log = _log(store)
        assert mtp.IDS_PREFIX + late.date().isoformat() not in log, "the window has closed"
        day = log[mtp.COUNTS_PREFIX + late.date().isoformat()]
        assert day["shadow"]["consolidation"]["demoted"] == 5, "counters keep going"
    finally:
        store.close()


def test_the_total_id_cap_holds(tmp_path, monkeypatch) -> None:
    from kiro_crew import memory_kind_routing as mkr

    monkeypatch.setattr(mkr, "SAMPLES_TOTAL", 400)
    store = _v1(tmp_path)
    try:
        for day in range(5):
            mtp.record_tier_policy(store, _many(150, day * 150), now=_T0 + timedelta(days=day))
        ids = [v for k, v in _log(store).items() if k.startswith(mtp.IDS_PREFIX)]
        assert sum(len(v) for v in ids) == 400
    finally:
        store.close()


# the re-teach signal


def _seeded(tmp_path) -> VectorMemoryStore:
    """A store holding one would-be demoted lesson with a vector, and one ordinary one."""
    store = _v1(tmp_path)
    store.write_lesson(GUESSED["rule"], "knowledge", None, "consolidation", [1.0, 0, 0, 0])
    store.write_lesson("Unrelated finding", "knowledge", None, "consolidation", [0, 0, 1.0, 0])
    key = _lesson_key(GUESSED["rule"])
    policy = mtp.TierPolicy(
        {}, "shadow", 1, 0, 1, 0, 0, 0, 1, (key,), labels=((key, "absent", None),)
    )
    assert mtp.record_tier_policy(store, policy, now=_T0)
    return store


def test_a_learn_add_close_to_a_would_be_demotion_is_a_reteach(tmp_path) -> None:
    store = _seeded(tmp_path)
    try:
        assert mtp.record_reteach(store, [0.99, 0.05, 0, 0], "shadow", now=_T0)
        # Close to a lesson the log does not name: checked, not matched.
        assert mtp.record_reteach(store, [0, 0, 1.0, 0], "shadow", now=_T0)
        # Wrong width never matches.
        assert mtp.record_reteach(store, [1.0, 0, 0], "shadow", now=_T0)
        log = _log(store)
    finally:
        store.close()
    day = log[mtp.COUNTS_PREFIX + "2026-10-01"]["shadow"]
    assert day["reteach"] == {"checked": 3, "matched": 1}
    reteach = [i for i in log[mtp.IDS_PREFIX + "2026-10-01"] if i["path"] == "reteach"]
    assert [i["key"] for i in reteach] == [_lesson_key(GUESSED["rule"])]


def test_reteach_ignores_demotions_from_previous_rules(tmp_path, monkeypatch) -> None:
    with monkeypatch.context() as old_rules:
        old_rules.setattr(mtp, "RULES_VERSION", 1)
        store = _seeded(tmp_path)
    try:
        assert mtp.record_reteach(store, [0.99, 0.05, 0, 0], "shadow", now=_T0)
        log = _log(store)
    finally:
        store.close()
    assert log[mtp.COUNTS_PREFIX + "2026-10-01"]["shadow"]["reteach"] == {
        "checked": 1,
        "matched": 0,
    }
    assert all(i["rules"] == 1 for i in log[mtp.IDS_PREFIX + "2026-10-01"])


@pytest.mark.parametrize(
    "mode, emb", [("off", [1.0, 0, 0, 0]), ("enforce", [1.0, 0, 0, 0]), ("shadow", None)]
)
def test_reteach_is_counted_only_under_shadow_with_a_vector(tmp_path, mode, emb) -> None:
    store = _seeded(tmp_path)
    try:
        before = _log(store)
        assert not mtp.record_reteach(store, emb, mode, now=_T0)
        assert _log(store) == before
    finally:
        store.close()


# the write paths


def _consolidator(store) -> HistoryConsolidator:
    return HistoryConsolidator(
        log=MagicMock(), memory=MagicMock(), vector_store=store, migrated=True
    )


def _tiers(store: VectorMemoryStore) -> dict[str, str | None]:
    return {
        json.loads(r["value_json"])["rule"]: json.loads(r["value_json"]).get("applies")
        for r in store.get_lessons()
    }


def test_v1_writes_by_mode(tmp_path) -> None:
    written = {}
    for mode in ("off", "shadow", "enforce"):
        store = _v1(tmp_path, mode)
        try:
            answer = _answer()
            policy = mtp.apply_tier_policy(answer, [PERSON], mode, quotes=_quotes(answer, [PERSON]))
            _consolidator(store)._save_lessons(policy.result["lessons"], key="s1")
            written[mode] = _tiers(store)
        finally:
            store.close()
    assert written["off"] == {
        QUOTED["rule"]: "always",
        GUESSED["rule"]: "always",
        UNTAGGED["rule"]: None,
        FINDING["rule"]: "on_topic",
    }
    assert written["shadow"] == written["off"]
    assert written["enforce"] == {
        **written["off"],
        GUESSED["rule"]: "on_topic",
        UNTAGGED["rule"]: "on_topic",
    }


def test_member_store_enforce_writes_by_the_policy(tmp_path, monkeypatch) -> None:
    from member_memory_helpers import declare_v2_store

    from kiro_crew import memory_stores
    from kiro_crew.vector_memory import open_member_database

    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "stores")
    directory = declare_v2_store(tmp_path, "member-alice")
    store = open_member_database(
        directory / "memory.db", member_id="alice", store_id="member-alice", embedding_dim=4
    )
    try:
        answer = _answer()
        policy = mtp.apply_tier_policy(
            answer, [PERSON], "enforce", quotes=_quotes(answer, [PERSON])
        )
        store.apply_consolidation(
            source_id="src-1",
            session_key="s1",
            source_total=1,
            result=policy.result,
            snapshot={},
            messages=[PERSON],
        )
        assert _tiers(store) == {
            QUOTED["rule"]: "always",
            GUESSED["rule"]: "on_topic",
            UNTAGGED["rule"]: "on_topic",
            FINDING["rule"]: "on_topic",
        }
        assert mtp.record_tier_policy(store, policy, now=_T0)
        assert (
            _log(store)[mtp.COUNTS_PREFIX + "2026-10-01"]["enforce"]["consolidation"]["demoted"]
            == 2
        )
    finally:
        store.close()


# the pass


#: The span the pass reads: a person, an agent's relay, the assistant.
SPAN = [PERSON, RELAY, ASSISTANT]
_QUOTE_CALL = "Find the user's own words"


def _quote_answer(prompt: str) -> dict:
    """What the quote call answers: the person's sentence for the linter rule only."""
    rules = json.loads(prompt.split("\nRules: ", 1)[1].split("\n", 1)[0])
    return {"quotes": {str(r["id"]): QUOTE if "linter" in r["rule"] else "" for r in rules}}


async def _run_pass(
    monkeypatch,
    mode: str,
    *,
    origin: str = "off",
    quote_answer=_quote_answer,
    span: list = SPAN,
) -> tuple[list[str], list, list]:
    """One history pass over *span*; returns the prompts, saved lessons, log entries."""
    from kiro_crew import consolidation_write_offload, executors
    from kiro_crew import memory_origin_gate as mog

    log = lease_aware_log()
    log.snapshot_for_consolidation.return_value = (list(span), len(span), 0, 0)
    log.consolidation_retry_state.return_value = (0, 0.0)
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    store = MagicMock()
    store.algorithm_version = "v1"
    store.get_all_semantic.return_value = []
    store.embed_lesson.side_effect = _embed
    c = HistoryConsolidator(
        log=log, memory=memory, sessions=None, vector_store=store, migrated=True
    )
    prompts: list[str] = []
    saved: list = []
    recorded: list = []

    async def inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def fake_llm(prompt: str, **_: Any) -> object:
        prompts.append(prompt)
        if prompt.startswith(_QUOTE_CALL):
            return quote_answer(prompt)
        return _answer()

    monkeypatch.setattr(executors, "run_in_memory_background_pool", inline)
    monkeypatch.setattr(mtp, "configured_mode", lambda: mode)
    monkeypatch.setattr(mog, "configured_mode", lambda: origin)
    monkeypatch.setattr(mtp, "record_tier_policy", lambda s, p, **_: recorded.append(p) or True)
    monkeypatch.setattr(mog, "record_origin_gate", lambda s, g, **_: True)
    monkeypatch.setattr(
        consolidation_write_offload, "save_lessons", lambda _c, raw, *a, **k: saved.append(raw)
    )
    with (
        patch.object(c, "_call_llm", side_effect=fake_llm),
        patch.object(c, "_write_structured_memory"),
    ):
        await c._consolidate("k", include_history=True)
    (lessons,) = saved
    return prompts, lessons, recorded


@pytest.mark.asyncio
async def test_the_pass_applies_the_policy_and_only_enforce_changes_the_prompt(
    monkeypatch,
) -> None:
    off_prompts, off_lessons, off_log = await _run_pass(monkeypatch, "off")
    (off_prompt,) = off_prompts
    assert off_lessons == _answer()["lessons"]
    assert off_log == []
    assert mtp.QUOTE_INSTRUCTION not in off_prompt

    shadow_prompts, shadow_lessons, shadow_log = await _run_pass(monkeypatch, "shadow")
    consolidation, quote_call = shadow_prompts
    assert consolidation == off_prompt, "shadow asks today's prompt, byte for byte"
    assert shadow_lessons == off_lessons, "shadow writes today's lessons"
    # The quote call sees the rules and the person's own words, nothing an agent said.
    assert quote_call.startswith(_QUOTE_CALL)
    assert PERSON["content"] in quote_call
    assert QUOTED["rule"] in quote_call and GUESSED["rule"] in quote_call
    assert "session_send" not in quote_call and UNTAGGED["rule"] not in quote_call
    (policy,) = shadow_log
    assert (policy.demoted, policy.quote_pass, policy.backed) == (2, 1, 1)
    assert dict(policy.calls)["quote_calls"] == 1

    enforce_prompts, enforce_lessons, enforce_log = await _run_pass(monkeypatch, "enforce")
    (enforce_prompt,) = enforce_prompts
    assert enforce_prompt.replace(mtp.QUOTE_INSTRUCTION, "", 1) == off_prompt
    assert [i.get("applies") for i in enforce_lessons] == ["always"] + ["on_topic"] * 3
    assert [p.demoted for p in enforce_log] == [2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "quote_answer",
    [lambda _p: 1 / 0, lambda _p: "junk", lambda _p: {"quotes": []}],
    ids=["raises", "junk", "wrong-shape"],
)
async def test_a_failed_quote_call_leaves_shadow_unmeasured_and_writes_as_off(
    monkeypatch, quote_answer
) -> None:
    _, off_lessons, _ = await _run_pass(monkeypatch, "off")
    prompts, lessons, (policy,) = await _run_pass(monkeypatch, "shadow", quote_answer=quote_answer)
    assert len(prompts) == 2 and lessons == off_lessons
    assert (policy.unmeasured, policy.demoted, policy.quote_present) == (2, 1, 0)
    assert dict(policy.calls) == {"quote_calls": 1, "quote_call_failed": 1, "quote_cut": 0}


@pytest.mark.asyncio
async def test_only_the_tier_policy_asks_for_quotes_never_the_origin_gate(monkeypatch) -> None:
    for origin in ("shadow", "enforce"):
        prompts, _, recorded = await _run_pass(monkeypatch, "off", origin=origin)
        assert len(prompts) == 1 and recorded == [], f"origin {origin} sends no quote call"
        assert mtp.QUOTE_INSTRUCTION not in prompts[0], f"origin {origin} asks today's prompt"
    # The origin gate's enforce does not ask the prompt for a quote, so tier shadow
    # still measures with one quote call after the writes.
    prompts, _, (policy,) = await _run_pass(monkeypatch, "shadow", origin="enforce")
    assert len(prompts) == 2 and mtp.QUOTE_INSTRUCTION not in prompts[0]
    assert prompts[1].startswith(_QUOTE_CALL)
    assert dict(policy.calls)["quote_calls"] == 1


@pytest.mark.asyncio
async def test_shadow_measures_the_answer_the_origin_gate_returned(monkeypatch) -> None:
    # No person in the span: the origin gate's enforce demotes every standing lesson,
    # so the shadow re-run reads no ``always`` lesson and sends no quote call.
    prompts, lessons, (policy,) = await _run_pass(
        monkeypatch, "shadow", origin="enforce", span=[RELAY, ASSISTANT]
    )
    assert len(prompts) == 1 and not prompts[0].startswith(_QUOTE_CALL)
    assert all(i.get("applies") != "always" for i in lessons)
    assert (policy.always, policy.demoted, policy.unmeasured) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_store", [True, False])
async def test_the_provider_path_scores_quotes_with_crews_embedder(monkeypatch, with_store) -> None:
    from types import SimpleNamespace

    from kiro_crew import history_consolidation
    from kiro_crew.memory_provider import consolidation

    monkeypatch.setattr(mtp, "configured_mode", lambda: "enforce")
    monkeypatch.setattr(history_consolidation, "_prompt_rows", lambda chunk: chunk)
    seen: list[dict] = []
    monkeypatch.setattr(consolidation, "extracted_items", lambda result: seen.append(result) or [])
    prompts: list[str] = []

    async def call_llm(prompt, session_key):
        prompts.append(prompt)
        return {"lessons": [dict(QUOTED)]}

    async def moved():
        return "moved"

    caps = SimpleNamespace(ingest="items", read_only=False)
    provider = SimpleNamespace(capabilities=lambda: caps)
    store = SimpleNamespace(embed_lesson=_embed) if with_store else None
    consolidator = SimpleNamespace(_call_llm=call_llm, _vector_store=store)
    answer = await consolidation.publish(
        consolidator,  # type: ignore[arg-type]
        "dashboard:x",
        SimpleNamespace(provider=provider, unavailable=None),  # type: ignore[arg-type]
        store_name="default",
        execution=SimpleNamespace(memory_mode="persistent", member_id=None),
        chunk=[PERSON],
        attempted=SimpleNamespace(offset=0, prompted=1),
        lease="l",
        include_history=True,
        commit_state=None,
        transcript_moved=moved,
        refused=object(),  # type: ignore[arg-type]
    )
    assert answer == "moved"
    (prompt,) = prompts
    assert mtp.QUOTE_INSTRUCTION in prompt
    ((lesson,),) = [r["lessons"] for r in seen]
    assert lesson["applies"] == ("always" if with_store else "on_topic")


def _gather(answer: dict, rows: list, ask) -> mtp.QuoteRun:
    return asyncio.run(mtp.gather_quotes(answer, rows, ask=ask, embed=_embed))


def test_the_quote_call_is_not_sent_without_an_always_lesson_or_a_person() -> None:
    sent: list[str] = []

    async def ask(prompt: str) -> dict:
        sent.append(prompt)
        return {"quotes": {}}

    assert _gather({"lessons": [dict(UNTAGGED), dict(FINDING)]}, [PERSON], ask) == mtp.QuoteRun({})
    run = _gather(_answer(), [RELAY, ASSISTANT], ask)
    assert set(run.evidence.values()) == {mtp.QuoteEvidence("absent")} and not run.asked
    assert sent == []


def test_the_quote_call_keeps_the_newest_messages_within_its_bound(monkeypatch) -> None:
    sent: list[str] = []

    async def ask(prompt: str) -> dict:
        sent.append(prompt)
        return {"quotes": {"0": QUOTE}}

    old = {**PERSON, "content": "an old message " + "x" * 1_900}
    monkeypatch.setattr(mtp, "QUOTE_INPUT_TOKENS", 500)
    run = _gather({"lessons": [dict(QUOTED)]}, [old, old, PERSON], ask)
    (prompt,) = sent
    assert mtp._kind_log.label_prompt_tokens(prompt) <= 500
    assert PERSON["content"] in prompt and "an old message" not in prompt
    assert run.cut and run.asked and run.evidence[QUOTED["rule"]].backed
    # Even one message over the bound: nothing is sent, nothing is measured.
    monkeypatch.setattr(mtp, "QUOTE_INPUT_TOKENS", 50)
    assert _gather({"lessons": [dict(QUOTED)]}, [PERSON], ask).evidence is None
    assert len(sent) == 1


def test_a_quote_call_that_never_reached_the_provider_is_not_counted_as_sent() -> None:
    from kiro_crew.history_consolidation import _ConsolidationNotDispatched

    async def ask(prompt: str) -> dict:
        raise _ConsolidationNotDispatched("no model")

    run = _gather({"lessons": [dict(QUOTED)]}, [PERSON], ask)
    assert run.evidence is None
    assert run.counts() == {"quote_calls": 0, "quote_call_failed": 0, "quote_cut": 0}


def test_the_quote_call_answer_is_checked_like_any_quote() -> None:
    async def ask(prompt: str) -> dict:
        # The call names an agent's text and a paraphrase: neither is the person's.
        return {"quotes": {"0": "always run the linter before every commit!", "1": "skip it"}}

    answer = {"lessons": [dict(QUOTED), dict(GUESSED)]}
    run = _gather(answer, [RELAY, {**PERSON, "content": "skip it please"}], ask)
    assert run.evidence[QUOTED["rule"]].outcome == "fail"
    assert run.evidence[GUESSED["rule"]].outcome == "fail", "two words are too few"


# the learn_add route


class _Slot:
    key = "dashboard:chat-1"

    def __init__(self, messages: list[dict]) -> None:
        self.messages = messages

    def append(self, *a: Any, **k: Any) -> dict:
        return {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, internal, chat, counted",
    [
        ("shadow", False, [], True),
        ("off", False, [], False),
        ("enforce", False, [], False),
        # An agent's own learn_add re-teaches only when the person spoke last.
        ("shadow", True, [PERSON, ASSISTANT], True),
        ("shadow", True, [PERSON, ASSISTANT, RELAY], False),
        ("shadow", True, [], False),
    ],
)
async def test_the_route_checks_a_learn_add_for_a_reteach(
    tmp_path, monkeypatch, mode, internal, chat, counted
) -> None:
    from body_stream_helpers import attach_body

    from kiro_crew.dashboard.handlers import cron
    from kiro_crew.shared_crew import memory_changes

    monkeypatch.setattr(mtp, "configured_mode", lambda: mode)
    monkeypatch.setattr(mtp._kind_log, "_utcnow", lambda: _T0)
    store = _seeded(tmp_path)
    state = MagicMock()
    state._background_tasks = set()
    state._slots = {"dashboard:chat-1": _Slot(chat)}
    state._ws_clients = []
    state.owner_id = ""
    published = {"internal_auth": True} if internal else {}
    request = MagicMock()
    request.get.side_effect = lambda key, default=None: published.get(key, default)
    request.app = {"state": state}
    request.headers = {"X-Session-Key": "dashboard:chat-1"}
    attach_body(request, {"rule": "Always check every build's exit code"})
    from unittest.mock import AsyncMock

    patches = [
        patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=store)),
        patch.object(cron, "_is_restricted_session", return_value=False),
        patch.object(cron, "_recognize_session", new=AsyncMock(return_value=None)),
        patch.object(cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=("", None))),
        patch.object(cron, "_prepare_member_lesson_store", new=AsyncMock(return_value=None)),
        patch.object(cron, "_sel"),
        patch.object(cron, "_resolve_and_supersede", new=AsyncMock()),
        patch.object(cron, "_surface_lesson_saved", new=AsyncMock()),
        patch.object(memory_changes, "crew_is_shared", return_value=False),
    ]
    from kiro_crew.dashboard.handlers import cron_lessons

    patches.append(
        patch.object(cron_lessons, "_embed_lesson_bounded", return_value=[0.99, 0.05, 0, 0])
    )
    for p in patches:
        p.start()
    try:
        resp = await cron.api_lessons_create(request)
        assert resp.status == 200, resp.text
        day = _log(store).get(mtp.COUNTS_PREFIX + "2026-10-01", {})
    finally:
        for p in reversed(patches):
            p.stop()
        store.close()
    if counted:
        assert day["shadow"]["reteach"] == {"checked": 1, "matched": 1}
    else:
        assert "reteach" not in day.get("shadow", {})


def test_a_demoted_lesson_cannot_enrich_a_standing_row(tmp_path) -> None:
    store = _v1(tmp_path)
    try:
        _consolidator(store)._save_lessons([dict(GUESSED)], key="s0")
        answer = {"lessons": [{**GUESSED, "negative": "skip it on docs-only changes"}]}
        policy = mtp.apply_tier_policy(answer, [PERSON], "enforce")
        _consolidator(store)._save_lessons(policy.result["lessons"], key="s1")
        (row,) = [json.loads(r["value_json"]) for r in store.get_lessons()]
        assert row.get("applies") == "always", "the stored row is untouched"
        assert not row.get("negative"), "unquoted text must not reach the standing row"
    finally:
        store.close()


def test_shadow_logs_the_key_each_store_writes_a_padded_rule_under() -> None:
    padded = {"rule": "  Prefer small pull requests  "}
    policy = mtp.apply_tier_policy({"lessons": [padded]}, [PERSON], "shadow")
    assert policy.demoted == 1
    assert policy.demoted_keys == (
        _lesson_key(padded["rule"].strip()),
        _lesson_key(padded["rule"]),
    )


def test_enforce_strips_the_clause_of_a_finding_the_model_already_tagged(tmp_path) -> None:
    tagged = {"lessons": [{**FINDING, "negative": "unquoted exception"}]}
    policy = mtp.apply_tier_policy(tagged, [PERSON], "enforce")
    assert policy.result["lessons"] == [FINDING]
    assert mtp.apply_tier_policy(tagged, [PERSON], "shadow").result is tagged
    store = _v1(tmp_path)
    try:
        _consolidator(store)._save_lessons([{**FINDING, "applies": "always"}], key="s0")
        _consolidator(store)._save_lessons(policy.result["lessons"], key="s1")
        (row,) = [json.loads(r["value_json"]) for r in store.get_lessons()]
        assert row.get("applies") == "always"
        assert not row.get("negative"), "unquoted text must not reach the standing row"
    finally:
        store.close()
