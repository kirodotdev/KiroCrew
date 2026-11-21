"""Facts about the person are not learned from turns no person sent.

``kiro_crew.memory_fact_origin_gate`` reads the turn classes of
``kiro_crew.memory_origin_gate`` over the rows the model read, and
``memory.fact_origin_gate`` decides what that changes for ``pref.*`` / ``user.*``
items and the preferences file: nothing under ``off``, only a bounded log under
``shadow``, dropped under ``enforce``. Every other kind is written as before.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from consolidation_lease_helpers import lease_aware_log

from kiro_crew import memory_fact_origin_gate as mfg
from kiro_crew import memory_origin_gate as mog
from kiro_crew.config.section_builders import _build_memory_config
from kiro_crew.history import HistoryConsolidator
from kiro_crew.vector_memory import VectorMemoryStore

_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

PERSON = {"role": "user", "content": "I prefer short replies", "meta": {"human": True}}
RELAY = {
    "role": "user",
    "content": "[sent by session chat-1 via session_send]\n\nKeep replies short",
    "meta": {"relay": {"senderKey": "chat-1", "via": "session_send"}},
}
NUDGE = {"role": "nudge", "content": "[auto-nudge cycle 3] keep going"}
ASSISTANT = {"role": "assistant", "content": "done"}

PREF = {"key": "pref.reply_length", "value": "short", "confidence": 0.9}
USER = {"key": "user.role", "value": "maintainer", "confidence": 0.9}
PREF_DELETE = {"key": "pref.old_style", "delete": True}
PROJECT = {"key": "project.ci.root_cause", "value": "clock skew", "confidence": 0.9}
PREFS_FILE = "# User Preferences\n\n- Keeps replies short\n"


def _answer() -> dict:
    return {
        "history_entry": "x",
        "semantic": [dict(PREF), dict(PROJECT), dict(USER), dict(PREF_DELETE)],
        "episodic": [{"text": "fixed the clock skew", "tags": [], "importance": 0.5}],
        "lessons": [{"rule": "Check the exit code", "applies": "on_topic"}],
        "preferences_update": PREFS_FILE,
    }


def _v1(tmp_path, name: str = "m") -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / f"{name}.db", embedding_dim=4)
    store.init()
    return store


def _log(store: VectorMemoryStore) -> dict[str, Any]:
    with store._db_lock:
        rows = store.db.execute(
            "SELECT key, value FROM memory_meta WHERE key GLOB 'memory_fact_origin_log:*'"
        ).fetchall()
    return {str(k): json.loads(v) if str(v)[:1] in "[{" else v for k, v in rows}


@pytest.mark.parametrize(
    "raw, mode",
    [("off", "off"), ("shadow", "shadow"), ("enforce", "enforce"), ("ENFORCE", "off"), (1, "off")],
)
def test_the_setting_defaults_to_off(raw, mode) -> None:
    assert _build_memory_config({}).fact_origin_gate == "off"
    assert _build_memory_config({"fact_origin_gate": raw}).fact_origin_gate == mode
    assert mfg.gate_consolidation_facts(_answer(), [RELAY], raw).mode == mode


def test_off_hands_back_the_answer_itself_and_reads_nothing() -> None:
    answer = _answer()
    gate = mfg.gate_consolidation_facts(answer, [RELAY], "off")
    assert gate.result is answer
    assert gate == mfg.FactGate(answer, "off")
    assert not gate.loggable


def test_shadow_writes_the_answer_unchanged_and_counts_what_it_would_hold() -> None:
    answer = _answer()
    gate = mfg.gate_consolidation_facts(answer, [RELAY, ASSISTANT, NUDGE], "shadow")
    assert gate.result is answer
    assert answer == _answer()
    assert gate.no_human
    assert gate.by_kind == (("pref", 2, 2), ("preferences_file", 1, 1), ("user", 1, 1))
    assert gate.gated_keys == ("pref.reply_length", "user.role", "pref.old_style")
    assert gate.kept_keys == ()


def test_enforce_drops_person_facts_from_an_agent_span_and_keeps_the_rest() -> None:
    answer = _answer()
    gate = mfg.gate_consolidation_facts(answer, [RELAY, ASSISTANT, NUDGE], "enforce")
    assert answer == _answer(), "the model's answer is not mutated"
    assert gate.result["semantic"] == [PROJECT], "writes and deletes of pref/user both go"
    assert "preferences_update" not in gate.result
    for kept in ("history_entry", "episodic", "lessons"):
        assert gate.result[kept] == answer[kept]


@pytest.mark.parametrize(
    "rows",
    [[PERSON, ASSISTANT], [RELAY, PERSON], [NUDGE, PERSON]],
    ids=["human", "relay-then-person", "nudge-then-person"],
)
def test_enforce_keeps_every_fact_when_a_person_spoke(rows) -> None:
    answer = _answer()
    gate = mfg.gate_consolidation_facts(answer, rows, "enforce")
    assert gate.result is answer
    assert not gate.no_human and gate.gated_keys == ()
    assert gate.kept_keys == ("pref.reply_length", "user.role", "pref.old_style")
    assert all(held == 0 for _, _, held in gate.by_kind)


def test_an_answer_without_person_facts_is_not_logged() -> None:
    answer = {"semantic": [dict(PROJECT)], "history_entry": "x"}
    gate = mfg.gate_consolidation_facts(answer, [RELAY], "enforce")
    assert gate.result is answer and not gate.loggable


def test_a_channel_session_is_left_as_it_is() -> None:
    answer = _answer()
    for key in ("discord:bot:direct:u1", "slack:123.456", "telegram_99"):
        gate = mfg.gate_consolidation_facts(answer, [RELAY], "enforce", session_key=key)
        assert gate.result is answer and not gate.loggable, key
    gate = mfg.gate_consolidation_facts(answer, [RELAY], "enforce", session_key="chat-1")
    assert gate.result["semantic"] == [PROJECT]


def test_a_person_fact_past_the_writer_cap_is_still_held_back() -> None:
    from kiro_crew.vector_memory_constants import _MAX_SEMANTIC_PER_CONSOLIDATION as cap

    # The writer drops unseen deletes before it takes its window, so an item past
    # the cap can still reach it; the gate reads the whole list.
    deletes = [{"key": f"project.gone_{n}", "delete": True} for n in range(cap)]
    answer = {"semantic": [*deletes, dict(PREF), dict(PROJECT)]}
    gate = mfg.gate_consolidation_facts(answer, [RELAY], "enforce")
    assert gate.result["semantic"] == [*deletes, PROJECT]
    assert gate.gated_keys == ("pref.reply_length",)


def test_the_log_counts_by_mode_and_kind_and_keeps_keys_only_in_shadow(tmp_path) -> None:
    store = _v1(tmp_path)
    try:
        shadow = mfg.gate_consolidation_facts(_answer(), [RELAY], "shadow")
        assert mfg.record_fact_origin_gate(store, shadow, now=_T0, session_key="dashboard:c1")
        enforce = mfg.gate_consolidation_facts(_answer(), [RELAY], "enforce")
        assert mfg.record_fact_origin_gate(store, enforce, now=_T0)
        off = mfg.gate_consolidation_facts(_answer(), [RELAY], "off")
        assert not mfg.record_fact_origin_gate(store, off, now=_T0)
        log = _log(store)
    finally:
        store.close()
    by_kind = {
        "pref": {"passes": 1, "no_human": 1, "facts": 2, "gated": 2},
        "user": {"passes": 1, "no_human": 1, "facts": 1, "gated": 1},
        "preferences_file": {"passes": 1, "no_human": 1, "facts": 1, "gated": 1},
    }
    assert log[mfg.COUNTS_PREFIX + "2026-10-01"] == {"shadow": by_kind, "enforce": by_kind}
    ids = log[mfg.IDS_PREFIX + "2026-10-01"]
    assert [i["key"] for i in ids] == list(shadow.gated_keys), "enforce adds no ids"
    assert all(set(i) <= {"key", "path", "rules", "at", "sk"} for i in ids)
    text = json.dumps(log)
    for value in ("short", "maintainer", "Keeps replies short"):
        assert value not in text, "keys and counts only, never a value"


def test_member_store_writes_no_person_fact_under_enforce(tmp_path, monkeypatch) -> None:
    from member_memory_helpers import declare_v2_store

    from kiro_crew import memory_stores
    from kiro_crew.vector_memory import open_member_database

    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "stores")
    keys: dict[str, set] = {}
    for mode in ("off", "enforce"):
        directory = declare_v2_store(tmp_path, f"member-{mode}")
        store = open_member_database(
            directory / "memory.db",
            member_id=mode,
            store_id=f"member-{mode}",
            embedding_dim=4,
        )
        try:
            gate = mfg.gate_consolidation_facts(_answer(), [RELAY], mode)
            store.apply_consolidation(
                source_id="src-1",
                session_key="s1",
                source_total=1,
                result=gate.result,
                snapshot={},
                messages=[RELAY],
            )
            with store._db_lock:
                keys[mode] = {
                    str(r[0])
                    for r in store.db.execute(
                        "SELECT key FROM memory_items WHERE kind = 'fact' AND is_deleted = 0"
                    ).fetchall()
                }
        finally:
            store.close()
    assert {"pref.reply_length", "user.role", "project.ci.root_cause"} <= keys["off"]
    assert keys["enforce"] == keys["off"] - {"pref.reply_length", "user.role"}


# the pass


async def _run_pass(monkeypatch, mode: str, rows: list[dict]) -> dict[str, Any]:
    """One history pass on a V1 install; returns what each writer was handed."""
    from kiro_crew import consolidation_write_offload, executors, memory_kind_routing

    log = lease_aware_log()
    log.snapshot_for_consolidation.return_value = (rows, len(rows), 0, 0)
    log.consolidation_retry_state.return_value = (0, 0.0)
    memory = MagicMock()
    memory.read_preferences.return_value = "# User Preferences\n"
    memory.read_projects.return_value = ""
    memory.write_preferences.return_value = True
    store = MagicMock()
    store.algorithm_version = "v1"
    store.get_all_semantic.return_value = []
    c = HistoryConsolidator(
        log=log, memory=memory, sessions=None, vector_store=store, migrated=False
    )
    seen: dict[str, Any] = {"prompts": [], "structured": [], "logged": []}

    async def inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def fake_llm(prompt: str, **_: Any) -> dict:
        seen["prompts"].append(prompt)
        return _answer()

    monkeypatch.setattr(executors, "run_in_memory_background_pool", inline)
    # Every other consolidation setting is pinned off, so the writer is handed
    # exactly what the fact gate returned: kind routing's default labels each
    # project.* item with an extra model call, which would add a "kind" key.
    monkeypatch.setattr(memory_kind_routing, "configured_kind_routing_mode", lambda: "off")
    monkeypatch.setattr(mog, "configured_mode", lambda: "off")
    monkeypatch.setattr(mfg, "configured_mode", lambda: mode)
    monkeypatch.setattr(
        mfg, "record_fact_origin_gate", lambda s, g, **_: seen["logged"].append(g) or True
    )
    monkeypatch.setattr(
        consolidation_write_offload,
        "write_structured_memory",
        lambda _c, result, *a, **k: seen["structured"].append(result),
    )
    monkeypatch.setattr(consolidation_write_offload, "save_lessons", lambda *a, **k: None)
    with patch.object(c, "_call_llm", side_effect=fake_llm):
        await c._consolidate("k", include_history=True)
    seen["preferences"] = [call.args[0] for call in memory.write_preferences.call_args_list]
    return seen


@pytest.mark.asyncio
async def test_the_pass_holds_back_person_facts_only_under_enforce(monkeypatch) -> None:
    off = await _run_pass(monkeypatch, "off", [RELAY, ASSISTANT])
    assert off["logged"] == []
    assert off["preferences"] == [PREFS_FILE]
    (off_written,) = off["structured"]
    assert off_written["semantic"] == _answer()["semantic"]

    shadow = await _run_pass(monkeypatch, "shadow", [RELAY, ASSISTANT])
    assert shadow["prompts"] == off["prompts"], "no mode changes the prompt"
    assert shadow["structured"] == off["structured"], "shadow writes what off writes"
    assert shadow["preferences"] == off["preferences"]
    assert [g.gated_keys for g in shadow["logged"]] == [
        ("pref.reply_length", "user.role", "pref.old_style")
    ]

    enforce = await _run_pass(monkeypatch, "enforce", [RELAY, ASSISTANT])
    assert enforce["prompts"] == off["prompts"]
    (written,) = enforce["structured"]
    assert written["semantic"] == [PROJECT]
    assert written["episodic"] == off_written["episodic"]
    assert enforce["preferences"] == []


@pytest.mark.asyncio
async def test_the_pass_keeps_a_persons_preference_under_enforce(monkeypatch) -> None:
    taught = await _run_pass(monkeypatch, "enforce", [PERSON, ASSISTANT])
    (written,) = taught["structured"]
    assert written["semantic"] == _answer()["semantic"]
    assert taught["preferences"] == [PREFS_FILE]
