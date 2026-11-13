"""A new consolidation lesson is a standing rule only when the person's own words back it.

``kiro_crew.memory_tier_policy`` stores an untagged lesson, and an ``always`` lesson
whose ``user_quote`` is not found in a person's turn, as ``on_topic``. Under
``memory.lesson_tier_policy`` ``off`` nothing changes, ``shadow`` only keeps a
bounded log (with a re-teach signal from ``learn_add``), and ``enforce`` applies it.
"""

from __future__ import annotations

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


def _answer() -> dict:
    return {
        "history_entry": "x",
        "lessons": [dict(QUOTED), dict(GUESSED), dict(UNTAGGED), dict(FINDING)],
    }


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


def test_shadow_writes_the_answer_unchanged_and_counts_every_standing_lesson() -> None:
    answer = _answer()
    policy = mtp.apply_tier_policy(answer, [PERSON, ASSISTANT], "shadow")
    assert policy.result is answer and answer == _answer()
    assert (policy.lessons, policy.unstated, policy.always) == (4, 1, 2)
    assert (policy.quote_present, policy.quote_pass, policy.quote_fail) == (1, 1, 0)
    # Shadow asked for no quote, so a passing one is still counted as a would-be demotion.
    assert policy.demoted == 3
    assert policy.demoted_keys == tuple(_lesson_key(i["rule"]) for i in (QUOTED, GUESSED, UNTAGGED))


def test_enforce_keeps_only_a_quoted_rule_standing() -> None:
    answer = _answer()
    policy = mtp.apply_tier_policy(answer, [PERSON, ASSISTANT], "enforce")
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
    policy = mtp.apply_tier_policy({"lessons": [dict(QUOTED)]}, [RELAY, ASSISTANT], "enforce")
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
    try:
        shadow = mtp.apply_tier_policy(_answer(), [PERSON], "shadow")
        assert mtp.record_tier_policy(store, shadow, now=_T0)
        enforce = mtp.apply_tier_policy(_answer(), [PERSON], "enforce")
        assert mtp.record_tier_policy(store, enforce, now=_T0)
        assert not mtp.record_tier_policy(
            store, mtp.apply_tier_policy(_answer(), [PERSON], "off"), now=_T0
        )
        log = _log(store)
    finally:
        store.close()
    base = {"passes": 1, "lessons": 4, "unstated": 1, "always": 2, "quote_present": 1}
    assert log[mtp.COUNTS_PREFIX + "2026-10-01"] == {
        "shadow": {"consolidation": {**base, "quote_pass": 1, "quote_fail": 0, "demoted": 3}},
        "enforce": {"consolidation": {**base, "quote_pass": 1, "quote_fail": 0, "demoted": 2}},
    }
    ids = log[mtp.IDS_PREFIX + "2026-10-01"]
    assert [i["key"] for i in ids] == list(shadow.demoted_keys), "enforce adds no ids"
    assert all(set(i) == {"key", "path", "rules"} for i in ids), "ids and counts only"
    assert QUOTE not in json.dumps(log) and GUESSED["rule"] not in json.dumps(log)


def _many(n: int, start: int = 0) -> mtp.TierPolicy:
    keys = tuple(f"lesson.k{i}" for i in range(start, start + n))
    return mtp.TierPolicy({}, "shadow", n, n, 0, 0, 0, 0, n, keys)


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
    policy = mtp.TierPolicy({}, "shadow", 1, 0, 1, 0, 0, 0, 1, (key,))
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
            policy = mtp.apply_tier_policy(_answer(), [PERSON], mode)
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
        policy = mtp.apply_tier_policy(_answer(), [PERSON], "enforce")
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


async def _run_pass(monkeypatch, mode: str) -> tuple[str, list, list]:
    """One history pass over a person's span; returns the prompt, saved lessons, log entries."""
    from kiro_crew import consolidation_write_offload, executors

    log = lease_aware_log()
    log.snapshot_for_consolidation.return_value = ([PERSON, ASSISTANT], 2, 0, 0)
    log.consolidation_retry_state.return_value = (0, 0.0)
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    store = MagicMock()
    store.algorithm_version = "v1"
    store.get_all_semantic.return_value = []
    c = HistoryConsolidator(
        log=log, memory=memory, sessions=None, vector_store=store, migrated=True
    )
    prompts: list[str] = []
    saved: list = []
    recorded: list = []

    async def inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def fake_llm(prompt: str, **_: Any) -> dict:
        prompts.append(prompt)
        return _answer()

    monkeypatch.setattr(executors, "run_in_memory_background_pool", inline)
    monkeypatch.setattr(mtp, "configured_mode", lambda: mode)
    monkeypatch.setattr(mtp, "record_tier_policy", lambda s, p: recorded.append(p) or True)
    monkeypatch.setattr(
        consolidation_write_offload, "save_lessons", lambda _c, raw, *a, **k: saved.append(raw)
    )
    with (
        patch.object(c, "_call_llm", side_effect=fake_llm),
        patch.object(c, "_write_structured_memory"),
    ):
        await c._consolidate("k", include_history=True)
    (prompt,) = prompts
    (lessons,) = saved
    return prompt, lessons, recorded


@pytest.mark.asyncio
async def test_the_pass_applies_the_policy_and_only_enforce_changes_the_prompt(
    monkeypatch,
) -> None:
    off_prompt, off_lessons, off_log = await _run_pass(monkeypatch, "off")
    assert off_lessons == _answer()["lessons"]
    assert off_log == []
    assert mtp.QUOTE_INSTRUCTION not in off_prompt

    shadow_prompt, shadow_lessons, shadow_log = await _run_pass(monkeypatch, "shadow")
    assert shadow_prompt == off_prompt, "shadow asks today's prompt, byte for byte"
    assert shadow_lessons == off_lessons
    assert [p.demoted for p in shadow_log] == [3]

    enforce_prompt, enforce_lessons, enforce_log = await _run_pass(monkeypatch, "enforce")
    assert enforce_prompt.replace(mtp.QUOTE_INSTRUCTION, "", 1) == off_prompt
    assert [i.get("applies") for i in enforce_lessons] == ["always"] + ["on_topic"] * 3
    assert [p.demoted for p in enforce_log] == [2]


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
