"""Lessons from turns no person sent stay out of the standing tier.

``kiro_crew.memory_origin_gate`` tells a person's turn from an agent's relay or an
automatic turn using markers the gateway writes, and ``memory.lesson_origin_gate``
decides what that changes: nothing under ``off``, only a bounded log under
``shadow``, a demotion to ``on_topic`` under ``enforce``. The default store's
consolidation lessons also record which session wrote them.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from body_stream_helpers import attach_body
from consolidation_lease_helpers import lease_aware_log

from kiro_crew import memory_origin_gate as mog
from kiro_crew import memory_record_metadata as record_meta
from kiro_crew.config.section_builders import _build_memory_config
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.vector_memory import VectorMemoryStore, _lesson_key

_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

PERSON = {"role": "user", "content": "please always run the linter", "meta": {"human": True}}
RELAY = {
    "role": "user",
    "content": "[sent by session chat-1 via session_send]\n\nrun the linter",
    "meta": {"relay": {"senderKey": "chat-1", "via": "session_send"}},
}
ENVELOPE_ONLY = {"role": "user", "content": "[sent by session chat-1 via session_send]\n\nhi"}
NUDGE = {"role": "nudge", "content": "[auto-nudge cycle 3] keep going"}
ASSISTANT = {"role": "assistant", "content": "done"}

ALWAYS = {"rule": "Always run the linter before a commit", "applies": "always"}
UNTAGGED = {"rule": "Check the exit code of every build"}
FINDING = {"rule": "The flaky test was a clock skew", "applies": "on_topic"}


def _answer() -> dict:
    return {"history_entry": "x", "lessons": [dict(ALWAYS), dict(UNTAGGED), dict(FINDING)]}


def _v1(tmp_path, name: str = "m") -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / f"{name}.db", embedding_dim=4)
    store.init()
    return store


def _log(store: VectorMemoryStore) -> dict[str, Any]:
    with store._db_lock:
        rows = store.db.execute(
            "SELECT key, value FROM memory_meta WHERE key GLOB 'memory_origin_log:*'"
        ).fetchall()
    return {str(k): json.loads(v) if str(v)[:1] in "[{" else v for k, v in rows}


# classification


@pytest.mark.parametrize(
    "row, want",
    [
        (PERSON, "human"),
        (RELAY, "agent"),
        (ENVELOPE_ONLY, "agent"),
        # Human is an allowlist: an unmarked user row is never a person, whatever it says.
        ({"role": "user", "content": "please always run the linter"}, "automatic"),
        ({"role": "user", "content": "x", "meta": {"author": {"kind": "human"}}}, "automatic"),
        (NUDGE, "automatic"),
        ({"role": "inject", "content": "cron result"}, "automatic"),
        ({"role": "subagent", "content": "done"}, "automatic"),
        ({"role": "system", "content": "x"}, "automatic"),
        ({"role": "user", "content": "[work-ledger wake] item done"}, "automatic"),
        ({"role": "user", "content": "[Heartbeat] check the goal"}, "automatic"),
        ({"role": "user", "content": "# Cron Run: digest\n\nbody"}, "automatic"),
        # A steer is the person's only with the person's marker; an app's steer has none.
        ({"role": "user", "content": "stop", "meta": {"steer": True, "human": True}}, "human"),
        ({"role": "user", "content": "stop", "meta": {"steer": True}}, "automatic"),
        # A relayed send steered into a turn is still the other session's.
        (
            {"role": "user", "content": "go", "meta": {"steer": True, "relay": {"senderKey": "a"}}},
            "agent",
        ),
        (
            {"role": "user", "content": [{"type": "text", "text": "hi"}], "meta": {"human": True}},
            "human",
        ),
        ({"role": "user", "content": [{"type": "text", "text": "hi"}]}, "automatic"),
        (ASSISTANT, None),
        ({"role": "notice", "content": "x"}, None),
        ("not a row", None),
    ],
)
def test_each_turn_is_classified_by_its_markers(row, want) -> None:
    assert mog.classify_turn(row) == want


def test_the_triggering_turn_is_the_latest_input_row_fail_closed() -> None:
    assert mog.triggering_turn([PERSON, ASSISTANT]) == "human"
    assert mog.triggering_turn([PERSON, NUDGE, ASSISTANT]) == "automatic"
    assert mog.triggering_turn([NUDGE, PERSON, ASSISTANT]) == "human"
    # Any later input row wins, so no turn opener can be walked past: a drained
    # subagent completion, an app or cron inject, or a mid-turn note.
    for later in (
        {"role": "subagent", "content": "[Subagent completion event] done"},
        {"role": "inject", "content": "x", "meta": {"injectKind": "mcp_app"}},
        {"role": "inject", "content": "x", "meta": {"injectKind": "cron"}},
        {"role": "system", "content": "note"},
    ):
        assert mog.triggering_turn([PERSON, ASSISTANT, later]) == "automatic", later
    assert mog.triggering_turn([ASSISTANT]) is None
    assert mog.triggering_turn(None) is None


# the setting


@pytest.mark.parametrize(
    "raw, mode",
    [("off", "off"), ("shadow", "shadow"), ("enforce", "enforce"), ("ENFORCE", "off"), (1, "off")],
)
def test_the_setting_defaults_to_off(raw, mode) -> None:
    assert _build_memory_config({}).lesson_origin_gate == "off"
    assert _build_memory_config({"lesson_origin_gate": raw}).lesson_origin_gate == mode
    assert mog.gate_consolidation_lessons(_answer(), [RELAY], raw).mode == mode


# the consolidation gate


def test_off_hands_back_the_answer_itself_and_reads_nothing() -> None:
    answer = _answer()
    gate = mog.gate_consolidation_lessons(answer, [RELAY], "off")
    assert gate.result is answer
    assert gate == mog.OriginGate(answer, "off")
    assert not gate.loggable


def test_shadow_writes_the_answer_unchanged_and_counts_the_demotions() -> None:
    answer = _answer()
    gate = mog.gate_consolidation_lessons(answer, [RELAY, ASSISTANT, NUDGE], "shadow")
    assert gate.result is answer
    assert answer == _answer()
    assert (gate.lessons, gate.standing, gate.demoted, gate.no_human) == (3, 2, 2, True)
    assert gate.demoted_keys == (_lesson_key(ALWAYS["rule"]), _lesson_key(UNTAGGED["rule"]))


def test_enforce_stores_an_agent_spans_standing_lessons_as_findings() -> None:
    answer = _answer()
    gate = mog.gate_consolidation_lessons(answer, [RELAY, ASSISTANT], "enforce")
    assert answer == _answer(), "the model's answer is not mutated"
    assert [item.get("applies") for item in gate.result["lessons"]] == ["on_topic"] * 3
    assert not any("negative" in item for item in gate.result["lessons"][:2])
    # A lesson the model already tagged a finding loses its clause too.
    tagged = {"lessons": [{**FINDING, "negative": "agent text"}]}
    (fenced,) = mog.gate_consolidation_lessons(tagged, [RELAY], "enforce").result["lessons"]
    assert fenced == {"rule": FINDING["rule"], "applies": "on_topic"}
    assert [item["rule"] for item in gate.result["lessons"]] == [
        ALWAYS["rule"],
        UNTAGGED["rule"],
        FINDING["rule"],
    ], "nothing is dropped"
    assert gate.result["history_entry"] == "x"


@pytest.mark.parametrize(
    "rows",
    [
        [PERSON, ASSISTANT],
        [RELAY, PERSON],
        [NUDGE, {"role": "user", "content": "ok", "meta": {"steer": True, "human": True}}],
    ],
    ids=["human", "mixed", "steer"],
)
def test_enforce_keeps_tiers_when_a_person_spoke(rows) -> None:
    answer = _answer()
    gate = mog.gate_consolidation_lessons(answer, rows, "enforce")
    assert gate.result is answer
    assert (gate.standing, gate.demoted, gate.no_human) == (2, 0, False)


def test_a_channel_session_is_left_as_it_is() -> None:
    answer = _answer()
    for key in ("discord:bot:direct:u1", "slack:123.456", "telegram_99"):
        gate = mog.gate_consolidation_lessons(answer, [RELAY], "enforce", session_key=key)
        assert gate.result is answer and not gate.loggable, key
    gate = mog.gate_consolidation_lessons(answer, [RELAY], "enforce", session_key="chat-1")
    assert gate.demoted == 2


# the learn_add gate


def test_learn_add_from_a_person_keeps_its_authority() -> None:
    for mode in ("shadow", "enforce"):
        gate = mog.gate_learn_add("always", [PERSON], mode, rule="r")
        assert (gate.applies, gate.source, gate.gate.demoted) == ("always", "user_explicit", 0)


def test_learn_add_after_a_relay_is_a_finding_with_its_own_source_under_enforce() -> None:
    gate = mog.gate_learn_add(None, [PERSON, RELAY], "enforce", rule="Use uv")
    assert (gate.applies, gate.source) == ("on_topic", mog.LESSON_SOURCE_AGENT_LEARN_ADD)
    assert gate.drop_negative
    assert gate.gate.demoted_keys == (_lesson_key("Use uv"),)
    shadow = mog.gate_learn_add(None, [PERSON, RELAY], "shadow", rule="Use uv")
    assert (shadow.applies, shadow.source, shadow.gate.demoted) == (None, "user_explicit", 1)
    # Already a finding: the source still says an agent turn asked for it.
    finding = mog.gate_learn_add("on_topic", [NUDGE], "enforce", rule="Use uv")
    assert (finding.applies, finding.source, finding.gate.demoted) == (
        "on_topic",
        mog.LESSON_SOURCE_AGENT_LEARN_ADD,
        0,
    )


def test_learn_add_with_no_chat_to_read_is_left_as_submitted() -> None:
    for rows in (None, [], [ASSISTANT]):
        gate = mog.gate_learn_add("always", rows, "enforce", rule="r")
        assert (gate.applies, gate.source, gate.gate.loggable) == ("always", "user_explicit", False)


# the bounded log


def test_the_log_counts_by_mode_and_path_and_keeps_ids_only_in_shadow(tmp_path) -> None:
    store = _v1(tmp_path)
    try:
        shadow = mog.gate_consolidation_lessons(_answer(), [RELAY], "shadow")
        assert mog.record_origin_gate(store, shadow, now=_T0)
        enforce = mog.gate_consolidation_lessons(_answer(), [RELAY], "enforce")
        assert mog.record_origin_gate(store, enforce, now=_T0)
        assert not mog.record_origin_gate(
            store, mog.gate_consolidation_lessons(_answer(), [RELAY], "off"), now=_T0
        )
        log = _log(store)
    finally:
        store.close()
    row = {"passes": 1, "no_human": 1, "lessons": 3, "standing": 2, "demoted": 2}
    assert log[mog.COUNTS_PREFIX + "2026-10-01"] == {
        "shadow": {"consolidation": row},
        "enforce": {"consolidation": row},
    }
    ids = log[mog.IDS_PREFIX + "2026-10-01"]
    assert [i["key"] for i in ids] == list(shadow.demoted_keys), "enforce adds no ids"
    assert all(set(i) == {"key", "path", "rules"} for i in ids), "ids and counts only, no text"
    assert ALWAYS["rule"] not in json.dumps(log)


def _many(n: int) -> mog.OriginGate:
    return mog.OriginGate(
        {}, "shadow", "consolidation", n, n, True, n, tuple(f"lesson.k{i}" for i in range(n))
    )


def test_ids_stop_at_the_daily_cap_and_after_the_window(tmp_path) -> None:
    store = _v1(tmp_path)
    try:
        mog.record_origin_gate(store, _many(100), now=_T0)
        mog.record_origin_gate(store, _many(100), now=_T0 + timedelta(hours=1))
        assert len(_log(store)[mog.IDS_PREFIX + "2026-10-01"]) == mog.IDS_PER_DAY
        late = _T0 + timedelta(days=15)
        mog.record_origin_gate(store, _many(5), now=late)
        log = _log(store)
        assert mog.IDS_PREFIX + late.date().isoformat() not in log, "the window has closed"
        assert (
            log[mog.COUNTS_PREFIX + late.date().isoformat()]["shadow"]["consolidation"]["demoted"]
            == 5
        ), "counters keep going"
    finally:
        store.close()


def test_a_rules_version_change_restarts_the_window(tmp_path, monkeypatch) -> None:
    store = _v1(tmp_path)
    try:
        mog.record_origin_gate(store, _many(1), now=_T0)
        late = _T0 + timedelta(days=20)
        monkeypatch.setattr(mog, "RULES_VERSION", mog.RULES_VERSION + 1)
        mog.record_origin_gate(store, _many(1), now=late)
        assert _log(store)[mog.IDS_PREFIX + late.date().isoformat()][0]["rules"] == (
            mog.RULES_VERSION
        )
    finally:
        store.close()


def test_the_total_id_cap_holds(tmp_path, monkeypatch) -> None:
    from kiro_crew import memory_kind_routing as mkr

    # Five days sit inside the 14-day window, so the total cap is what binds.
    monkeypatch.setattr(mkr, "SAMPLES_TOTAL", 400)
    store = _v1(tmp_path)
    try:
        for day in range(5):
            mog.record_origin_gate(store, _many(150), now=_T0 + timedelta(days=day))
        ids = [v for k, v in _log(store).items() if k.startswith(mog.IDS_PREFIX)]
        assert sum(len(v) for v in ids) == 400
    finally:
        store.close()


# the write paths


def _consolidator(store) -> HistoryConsolidator:
    return HistoryConsolidator(
        log=MagicMock(), memory=MagicMock(), vector_store=store, migrated=True
    )


def _stored(store: VectorMemoryStore) -> dict[str, tuple[str | None, str]]:
    return {
        json.loads(r["value_json"])["rule"]: (
            json.loads(r["value_json"]).get("applies"),
            r["source"],
        )
        for r in store.get_lessons()
    }


def test_v1_enforce_writes_the_demoted_tier_and_off_writes_todays(tmp_path) -> None:
    written = {}
    for mode in ("off", "shadow", "enforce"):
        store = _v1(tmp_path, mode)
        try:
            gate = mog.gate_consolidation_lessons(_answer(), [RELAY], mode)
            _consolidator(store)._save_lessons(gate.result["lessons"], key="s1")
            written[mode] = _stored(store)
        finally:
            store.close()
    assert written["off"] == {
        ALWAYS["rule"]: ("always", "consolidation"),
        UNTAGGED["rule"]: (None, "consolidation"),
        FINDING["rule"]: ("on_topic", "consolidation"),
    }
    assert written["shadow"] == written["off"]
    assert {applies for applies, _ in written["enforce"].values()} == {"on_topic"}


def test_member_enforce_writes_the_demoted_tier(tmp_path, monkeypatch) -> None:
    from member_memory_helpers import declare_v2_store

    from kiro_crew import memory_stores
    from kiro_crew.vector_memory import open_member_database

    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "stores")
    directory = declare_v2_store(tmp_path, "member-alice")
    store = open_member_database(
        directory / "memory.db", member_id="alice", store_id="member-alice", embedding_dim=4
    )
    try:
        gate = mog.gate_consolidation_lessons(_answer(), [RELAY], "enforce")
        store.apply_consolidation(
            source_id="src-1",
            session_key="s1",
            source_total=1,
            result=gate.result,
            snapshot={},
            messages=[RELAY],
        )
        assert {applies for applies, _ in _stored(store).values()} == {"on_topic"}
    finally:
        store.close()


def test_a_demoted_lesson_cannot_enrich_a_standing_row(tmp_path) -> None:
    store = _v1(tmp_path)
    try:
        _consolidator(store)._save_lessons([dict(ALWAYS)], key="s0")
        answer = {"lessons": [{**ALWAYS, "negative": "skip it on docs-only changes"}]}
        gate = mog.gate_consolidation_lessons(answer, [RELAY], "enforce")
        _consolidator(store)._save_lessons(gate.result["lessons"], key="s1")
        (row,) = [json.loads(r["value_json"]) for r in store.get_lessons()]
        assert row.get("applies") == "always"
        assert not row.get("negative"), "agent text must not reach the standing row"
    finally:
        store.close()


def test_default_store_lessons_record_their_session(tmp_path) -> None:
    store = _v1(tmp_path)
    try:
        _consolidator(store)._save_lessons([dict(UNTAGGED)], key="chat-7")
        _consolidator(store)._save_lessons([dict(FINDING)])
        with store._db_lock:
            with_key = record_meta.get_record_metadata(
                store.db, record_meta.record_id_for("directive", _lesson_key(UNTAGGED["rule"]))
            )
            without = record_meta.get_record_metadata(
                store.db, record_meta.record_id_for("directive", _lesson_key(FINDING["rule"]))
            )
        assert with_key["source_ref"] == "consolidation:chat-7"
        # No session known: the old bare form, as every earlier row has.
        assert without["source_ref"] == "consolidation"
        # ``source`` itself is unchanged, so every reader comparing it is too.
        assert {source for _, source in _stored(store).values()} == {"consolidation"}
    finally:
        store.close()


# the pass and the route


async def _run_pass(monkeypatch, mode: str) -> tuple[str, list, list]:
    """One history pass over a relay-only span; returns the prompt, saved lessons, log entries."""
    from kiro_crew import consolidation_write_offload, executors

    log = lease_aware_log()
    log.snapshot_for_consolidation.return_value = ([RELAY, ASSISTANT], 2, 0, 0)
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
    monkeypatch.setattr(mog, "configured_mode", lambda: mode)
    monkeypatch.setattr(mog, "record_origin_gate", lambda s, g: recorded.append(g) or True)
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
async def test_the_pass_gates_on_the_rows_the_model_read(monkeypatch) -> None:
    off_prompt, off_lessons, off_log = await _run_pass(monkeypatch, "off")
    assert off_lessons == _answer()["lessons"]
    assert off_log == []
    for mode in ("shadow", "enforce"):
        prompt, lessons, recorded = await _run_pass(monkeypatch, mode)
        assert prompt == off_prompt, "no mode changes the prompt"
        assert [g.demoted for g in recorded] == [2]
        if mode == "shadow":
            assert lessons == off_lessons
        else:
            assert {item.get("applies") for item in lessons} == {"on_topic"}


SESSION = "dashboard:chat-1"


class _Slot:
    def __init__(self, messages: list[dict], key: str = SESSION) -> None:
        self.key = key
        self.messages = messages

    def append(self, *a: Any, **k: Any) -> dict:
        return {}


async def _learn_add(
    tmp_path,
    monkeypatch,
    mode: str,
    messages: list[dict],
    *,
    internal=True,
    session: str = SESSION,
    negative: str | None = None,
    seed: dict | None = None,
):
    from kiro_crew.dashboard.handlers import cron
    from kiro_crew.shared_crew import memory_changes

    monkeypatch.setattr(mog, "configured_mode", lambda: mode)
    monkeypatch.setattr(mog._kind_log, "_utcnow", lambda: _T0)
    store = _v1(tmp_path, f"route-{mode}-{internal}")
    if seed is not None:
        _consolidator(store)._save_lessons([seed], key="s0")
    state = MagicMock()
    state.conversation_log = ConversationLog()
    state._background_tasks = set()
    state._slots = {session: _Slot(messages, session)}
    state._ws_clients = []
    state.owner_id = ""
    published = {"internal_auth": True} if internal else {}
    request = MagicMock()
    request.get.side_effect = lambda key, default=None: published.get(key, default)
    request.app = {"state": state}
    request.headers = {"X-Session-Key": session}
    body = {"rule": "Use uv for installs", "applies": "always"}
    if negative is not None:
        body["negative"] = negative
    attach_body(request, body)
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
    for p in patches:
        p.start()
    try:
        resp = await cron.api_lessons_create(request)
        assert resp.status == 200, resp.text
        (row,) = [
            json.loads(r["value_json"])
            for r in store.get_lessons()
            if json.loads(r["value_json"])["rule"] == "Use uv for installs"
        ]
        return _stored(store)["Use uv for installs"], _log(store), row
    finally:
        for p in reversed(patches):
            p.stop()
        store.close()


@pytest.mark.asyncio
async def test_the_route_demotes_an_agent_triggered_learn_add_under_enforce(
    tmp_path, monkeypatch
) -> None:
    stored, log, _ = await _learn_add(tmp_path, monkeypatch, "enforce", [PERSON, ASSISTANT, RELAY])
    assert stored == ("on_topic", mog.LESSON_SOURCE_AGENT_LEARN_ADD)
    counts = log[mog.COUNTS_PREFIX + _T0.date().isoformat()]
    assert counts["enforce"]["learn_add"]["demoted"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, messages, internal",
    [
        ("off", [RELAY], True),
        ("shadow", [RELAY], True),
        ("enforce", [RELAY, PERSON], True),
        # A person's own dashboard Add is the person, whatever the chat holds.
        ("enforce", [RELAY], False),
    ],
)
async def test_the_route_keeps_user_explicit_otherwise(
    tmp_path, monkeypatch, mode, messages, internal
) -> None:
    stored, log, _ = await _learn_add(tmp_path, monkeypatch, mode, messages, internal=internal)
    assert stored == ("always", "user_explicit")
    if mode == "off" or not internal:
        assert log == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "enforce"])
async def test_the_route_leaves_a_channel_sessions_learn_add_as_it_is(
    tmp_path, monkeypatch, mode
) -> None:
    # A channel transport adds the person's row only after the reply, so the
    # latest row here is an unmarked one; reading it would demote the person.
    unmarked = {"role": "user", "content": "remember: use uv"}
    stored, log, _ = await _learn_add(
        tmp_path, monkeypatch, mode, [PERSON, ASSISTANT, unmarked], session="slack:1700000000.1"
    )
    assert stored == ("always", "user_explicit")
    assert log == {}


@pytest.mark.asyncio
async def test_the_route_drops_a_demoted_learn_adds_not_clause(tmp_path, monkeypatch) -> None:
    seed = {"rule": "Use uv for installs", "applies": "always"}
    stored, _, row = await _learn_add(
        tmp_path, monkeypatch, "enforce", [RELAY], negative="skip it in CI", seed=seed
    )
    assert stored[0] == "always"
    assert not row.get("negative"), "agent text must not reach the standing row"


async def _learn_update(tmp_path, monkeypatch, mode: str, messages: list[dict], *, session=SESSION):
    from kiro_crew.dashboard.handlers import cron

    monkeypatch.setattr(mog, "configured_mode", lambda: mode)
    store = _v1(tmp_path, f"update-{mode}-{session.split(':')[0]}")
    assert store.write_lesson("Use uv for installs", "knowledge", applies="on_topic")
    state = MagicMock()
    state._slots = {session: _Slot(messages, session)}
    published = {"internal_auth": True}
    request = MagicMock()
    request.get.side_effect = lambda key, default=None: published.get(key, default)
    request.app = {"state": state}
    request.headers = {"X-Session-Key": session}
    attach_body(
        request,
        {
            "rule": "Use uv for installs",
            "repo_scope": "",
            "current": "on_topic",
            "applies": "always",
        },
    )
    patches = [
        patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=store)),
        patch.object(cron, "_is_restricted_session", return_value=False),
        patch.object(cron, "_recognize_session", new=AsyncMock(return_value=None)),
        patch.object(cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=("", None))),
        patch.object(cron, "_prepare_member_lesson_store", new=AsyncMock(return_value=None)),
        patch.object(cron, "_sel"),
        patch.object(cron, "emit_lessons_changed"),
    ]
    for p in patches:
        p.start()
    try:
        resp = await cron.api_lessons_update(request)
        return resp.status, _stored(store)["Use uv for installs"][0]
    finally:
        for p in reversed(patches):
            p.stop()
        store.close()


@pytest.mark.asyncio
async def test_enforce_refuses_an_agent_retier_into_the_standing_tier(
    tmp_path, monkeypatch
) -> None:
    # Otherwise a demoted learn_add is one learn_update away from ``always``.
    assert await _learn_update(tmp_path, monkeypatch, "enforce", [RELAY]) == (403, "on_topic")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, messages, session",
    [
        ("enforce", [RELAY, PERSON], SESSION),
        ("shadow", [RELAY], SESSION),
        ("enforce", [PERSON, ASSISTANT, {"role": "user", "content": "x"}], "slack:1700000000.1"),
    ],
)
async def test_a_retier_is_left_as_it_is_otherwise(
    tmp_path, monkeypatch, mode, messages, session
) -> None:
    got = await _learn_update(tmp_path, monkeypatch, mode, messages, session=session)
    assert got == (200, "always")


# kirocrew learn add


def _cli_learn_add(tmp_path, monkeypatch, mode: str, *, in_agent: bool, name: str):
    """Run ``kirocrew learn add`` under *mode*; return the stored row, the log and stdout."""
    import argparse

    from kiro_crew import cli_commands
    from kiro_crew.cloud import aws

    probes: list[bool] = []

    def _probe() -> bool:
        probes.append(True)
        return in_agent

    monkeypatch.setattr(aws, "_in_agent_session", _probe)
    monkeypatch.setattr(mog._kind_log, "_utcnow", lambda: _T0)
    cfg = MagicMock()
    cfg.memory.lesson_origin_gate = mode
    db = tmp_path / f"cli-{name}.db"
    args = argparse.Namespace(
        learn_action="add", rule="Use uv for installs", category="tool", negative="pip"
    )
    with (
        patch.object(
            cli_commands,
            "VectorMemoryStore",
            side_effect=lambda **_: VectorMemoryStore(db_path=db, embedding_dim=4),
        ),
        patch.object(cli_commands, "LessonStore", return_value=MagicMock()),
        patch.object(cli_commands.KiroCrewConfig, "load", return_value=cfg),
    ):
        cli_commands._learn(args)
    reader = VectorMemoryStore(db_path=db, embedding_dim=4)
    reader.init()
    try:
        (row,) = [json.loads(r["value_json"]) for r in reader.get_lessons()]
        return _stored(reader)["Use uv for installs"], row, _log(reader), probes
    finally:
        reader.close()


def test_cli_learn_add_from_an_agent_session_is_a_finding_under_enforce(
    tmp_path, monkeypatch, capsys
) -> None:
    stored, row, log, _ = _cli_learn_add(tmp_path, monkeypatch, "enforce", in_agent=True, name="e")
    assert stored == ("on_topic", mog.LESSON_SOURCE_AGENT_LEARN_ADD)
    assert not row.get("negative"), "a demoted write carries no NOT-clause"
    counts = log[mog.COUNTS_PREFIX + _T0.date().isoformat()]
    assert counts["enforce"][mog.PATH_CLI_LEARN_ADD]["demoted"] == 1
    assert "Stored as an on_topic finding" in capsys.readouterr().out


def test_cli_learn_add_from_an_agent_session_is_counted_under_shadow(
    tmp_path, monkeypatch, capsys
) -> None:
    stored, row, log, _ = _cli_learn_add(tmp_path, monkeypatch, "shadow", in_agent=True, name="s")
    assert stored == (None, "user_explicit")
    assert row.get("negative") == "pip"
    day = _T0.date().isoformat()
    assert log[mog.COUNTS_PREFIX + day]["shadow"][mog.PATH_CLI_LEARN_ADD]["demoted"] == 1
    assert [i["path"] for i in log[mog.IDS_PREFIX + day]] == [mog.PATH_CLI_LEARN_ADD]
    assert "on_topic finding" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "mode, in_agent",
    [
        ("off", True),
        # A person's own terminal carries no agent marker: never gated, never logged.
        ("shadow", False),
        ("enforce", False),
    ],
)
def test_cli_learn_add_keeps_user_explicit_otherwise(tmp_path, monkeypatch, mode, in_agent) -> None:
    stored, row, log, probes = _cli_learn_add(
        tmp_path, monkeypatch, mode, in_agent=in_agent, name=f"{mode}-{in_agent}"
    )
    assert stored == (None, "user_explicit")
    assert row.get("negative") == "pip"
    assert log == {}
    if mode == "off":
        assert probes == [], "off reads nothing"
