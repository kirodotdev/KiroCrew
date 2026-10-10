"""Spend grouped by the sidebar folder a session is filed in.

``cost_breakdown`` cannot see folders, so it hands every session's spend to the
handler under ``slot_spend`` and ``_with_folder_spend`` rolls it up. The guarded
failures:

* a closed session dropping out of its folder into Unfiled, because only live
  slots were consulted;
* the folder rows not adding up to the window total (a session in no folder, or
  in a deleted one, must still be counted somewhere);
* the per-session map leaking into the response, or the memoised payload being
  written through;
* two folders with the same name merging into one row.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.dashboard.chat_utils import slot_transcript_key
from kiro_crew.dashboard.handlers import usage as usage_mod
from kiro_crew.dashboard.handlers.telemetry import _folder_paths, _with_folder_spend
from kiro_crew.history import ConversationLog

FOLDERS = [
    {"id": "f-dev", "name": "Platform dev", "parent_id": ""},
    {"id": "f-sub", "name": "Telemetry", "parent_id": "f-dev"},
    {"id": "f-ops", "name": "Ops"},
    {"id": "f-ops2", "name": "Ops"},
]


def _request(
    slots: dict, folders=FOLDERS, conversation_log=None, aliases: dict | None = None
) -> Any:
    # ``Any``: a stand-in for ``web.Request``, which the handlers are typed against.
    async def read_folders(read):
        # The committed-state reader the handler must use; the raw list below is
        # deliberately different so a read that bypasses it shows up.
        return read(folders)

    state = SimpleNamespace(
        get_slot=lambda key: slots.get(key),
        conversation_log=conversation_log,
        read_folders=read_folders,
        _folders=[],
    )
    if aliases is not None:
        # ``DashboardState.spend_slot_by_session``: live session identity -> slot key.
        state.spend_slot_by_session = lambda: dict(aliases)
    return SimpleNamespace(app={"state": state})


def _cost(current: dict, prior: dict | None = None) -> dict:
    return {
        "credits": sum(v["credits"] for v in current.values()),
        "slot_spend": {"current": current, "prior": prior or {}},
    }


def _by_id(out: dict) -> dict:
    return {r["folder_id"]: r for r in out["by_folder"]}


def test_nested_folders_render_as_the_sidebar_breadcrumb():
    paths = _folder_paths(FOLDERS)
    assert paths["f-sub"] == "Platform dev › Telemetry"
    assert paths["f-dev"] == "Platform dev"


def test_a_parent_cycle_does_not_hang():
    paths = _folder_paths(
        [{"id": "a", "name": "A", "parent_id": "b"}, {"id": "b", "name": "B", "parent_id": "a"}]
    )
    assert paths["a"] == "B › A"


@pytest.mark.asyncio
async def test_live_slots_group_by_their_folder_and_unfiled_catches_the_rest():
    slots = {
        "chat-1-1": SimpleNamespace(folder_id="f-sub"),
        "chat-2-2": SimpleNamespace(folder_id="f-sub"),
        "chat-3-3": SimpleNamespace(folder_id=""),
        "chat-4-4": SimpleNamespace(folder_id="deleted-folder"),
    }
    out = await _with_folder_spend(
        _request(slots),
        _cost(
            {
                "chat-1-1": {"credits": 10.0, "turns": 2},
                "chat-2-2": {"credits": 5.0, "turns": 1},
                "chat-3-3": {"credits": 3.0, "turns": 1},
                "chat-4-4": {"credits": 2.0, "turns": 1},
                "": {"credits": 1.0, "turns": 1},
            }
        ),
    )
    rows = _by_id(out)
    assert rows["f-sub"]["name"] == "Platform dev › Telemetry"
    assert rows["f-sub"]["credits"] == 15.0
    assert rows["f-sub"]["turns"] == 3
    # No folder, a deleted folder and a row with no slot all land in Unfiled.
    assert rows[""]["name"] == ""
    assert rows[""]["credits"] == 6.0
    assert sum(r["credits"] for r in out["by_folder"]) == 21.0
    assert out["by_folder"][0]["folder_id"] == "f-sub"  # largest first
    assert rows["f-sub"]["share_pct"] == pytest.approx(71.4)


@pytest.mark.asyncio
async def test_same_named_folders_stay_separate_rows():
    slots = {
        "chat-1-1": SimpleNamespace(folder_id="f-ops"),
        "chat-2-2": SimpleNamespace(folder_id="f-ops2"),
    }
    out = await _with_folder_spend(
        _request(slots),
        _cost(
            {
                "chat-1-1": {"credits": 4.0, "turns": 1},
                "chat-2-2": {"credits": 6.0, "turns": 1},
            }
        ),
    )
    rows = _by_id(out)
    assert rows["f-ops"]["credits"] == 4.0
    assert rows["f-ops2"]["credits"] == 6.0


@pytest.mark.asyncio
async def test_prior_period_delta_is_per_folder():
    slots = {
        "chat-1-1": SimpleNamespace(folder_id="f-dev"),
        "chat-2-2": SimpleNamespace(folder_id="f-ops"),
    }
    out = await _with_folder_spend(
        _request(slots),
        _cost(
            {"chat-1-1": {"credits": 30.0, "turns": 3}, "chat-2-2": {"credits": 5.0, "turns": 1}},
            {"chat-1-1": 20.0},
        ),
    )
    rows = _by_id(out)
    assert rows["f-dev"]["delta_pct"] == 50.0
    # No prior spend: no percentage, rendered as "new".
    assert rows["f-ops"]["delta_pct"] is None


@pytest.mark.asyncio
async def test_a_closed_session_keeps_its_stored_folder(tmp_path):
    log = ConversationLog(tmp_path)
    log.update_metadata(slot_transcript_key("chat-9-9"), {"folder_id": "f-dev"})
    out = await _with_folder_spend(
        _request({}, conversation_log=log),
        _cost({"chat-9-9": {"credits": 7.0, "turns": 1}}),
    )
    assert _by_id(out)["f-dev"]["credits"] == 7.0


@pytest.mark.asyncio
async def test_the_live_folder_wins_over_the_stored_one(tmp_path):
    log = ConversationLog(tmp_path)
    log.update_metadata(slot_transcript_key("chat-1-1"), {"folder_id": "f-dev"})
    slots = {"chat-1-1": SimpleNamespace(folder_id="f-ops")}
    out = await _with_folder_spend(
        _request(slots, conversation_log=log),
        _cost({"chat-1-1": {"credits": 7.0, "turns": 1}}),
    )
    assert set(_by_id(out)) == {"f-ops"}


@pytest.mark.asyncio
async def test_the_session_map_never_leaves_and_the_input_is_not_mutated():
    payload = _cost({"chat-1-1": {"credits": 1.0, "turns": 1}})
    out = await _with_folder_spend(_request({}), payload)
    assert "slot_spend" not in out
    assert "slot_spend" in payload and "by_folder" not in payload


@pytest.mark.asyncio
async def test_without_dashboard_state_the_map_is_still_dropped():
    no_state: Any = SimpleNamespace(app={})
    out = await _with_folder_spend(no_state, _cost({"chat-1-1": {"credits": 1.0, "turns": 1}}))
    assert out["by_folder"] == []
    assert "slot_spend" not in out


def test_cost_breakdown_reports_every_sessions_spend_for_both_periods(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)

    def row(slot, credits, age_days):
        return {
            "_type": "tokens",
            "ts": (now - timedelta(days=age_days)).isoformat(),
            "slot": slot,
            "model": "m",
            "credits": credits,
        }

    d = tmp_path / "usage"
    d.mkdir()
    shard = d / "shard.jsonl"
    shard.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                row("chat-1-1", 4.0, 0),
                row("chat-1-1", 1.0, 1),
                row("chat-1-1", 2.0, 9),
                row("chat-2-2", 3.0, 10),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(usage_mod, "_shards_in_window", lambda days: [shard])
    monkeypatch.setattr(usage_mod, "_COST_CACHE", None)
    monkeypatch.setattr(usage_mod, "_COST_CACHE_KEY", None)

    spend = usage_mod.cost_breakdown(7)["slot_spend"]
    assert spend["current"] == {"chat-1-1": {"credits": 5.0, "turns": 2}}
    assert spend["prior"] == {"chat-1-1": 2.0, "chat-2-2": 3.0}


def _two_pass(rows: list[tuple[str, float, bool]]) -> dict:
    """Run *rows* ``(key, credits, prior)`` through both passes, as cost_breakdown does."""
    chooser = usage_mod._SlotSpend()
    for key, credits, prior in rows:
        chooser.add(key, credits, prior=prior)
    exact = usage_mod._ExactSlotSpend(chooser.kept())
    for key, credits, prior in rows:
        exact.add(key, credits, prior=prior)
    return exact.result()


def _spend(current: dict, prior: dict | None = None) -> dict:
    """Feed whole-session totals through both passes, current period first.

    A session's turns arrive as that many rows: the first carries its credits,
    the rest carry none, so the sums stay exact in binary floating point.
    """
    rows: list[tuple[str, float, bool]] = []
    for key, v in current.items():
        for i in range(int(v["turns"])):
            rows.append((key, float(v["credits"]) if i == 0 else 0.0, False))
    for key, credits in (prior or {}).items():
        rows.append((key, float(credits), True))
    return _two_pass(rows)


def test_slot_spend_is_capped_and_the_tail_is_summed(monkeypatch):
    # The map rides the memoised payload, so it needs a count bound; the folded
    # tail must still be counted so the folder rows add up to the total.
    monkeypatch.setattr(usage_mod, "_COST_SLOT_SPEND_CAP", 2)
    out = _spend(
        {
            "a": {"credits": 9.0, "turns": 1},
            "b": {"credits": 5.0, "turns": 2},
            "c": {"credits": 2.0, "turns": 3},
            "d": {"credits": 1.0, "turns": 4},
        },
        {"a": 4.0, "b": 3.0, "c": 2.0},
    )
    assert set(out["current"]) == {"a", "b"}
    assert set(out["prior"]) == {"a", "b"}
    # No session count: with eviction a key can leave and come back, so a count
    # could name one session twice. The credits are exact either way.
    assert out["overflow"] == {"credits": 3.0, "turns": 7, "prior_credits": 2.0}


def test_the_accumulator_never_holds_more_than_twice_the_cap(monkeypatch):
    # The bound applies while the shards are read, not after: a store with a
    # million distinct session keys must not build a million-entry map first.
    monkeypatch.setattr(usage_mod, "_COST_SLOT_SPEND_CAP", 3)
    acc = usage_mod._SlotSpend()
    peak = 0
    for i in range(500):
        acc.add(f"chat-{i}-1", float(i % 17), prior=bool(i % 3 == 0))
        peak = max(peak, len(acc))
    assert peak <= 6
    assert len(acc.kept()) <= 3


def test_cost_breakdown_bounds_the_slot_map_while_reading(tmp_path, monkeypatch):
    # The production path, not only the class: every row of a 200-session shard
    # goes through one accumulator whose size never passes twice the cap.
    monkeypatch.setattr(usage_mod, "_COST_SLOT_SPEND_CAP", 4)
    peaks: list[int] = []

    class Watched(usage_mod._SlotSpend):
        def add(self, key: str, credits: float, *, prior: bool = False) -> None:
            super().add(key, credits, prior=prior)
            peaks.append(len(self))

    monkeypatch.setattr(usage_mod, "_SlotSpend", Watched)
    now = datetime.now(timezone.utc)
    shard = tmp_path / "shard.jsonl"
    shard.write_text(
        "\n".join(
            json.dumps(
                {
                    "_type": "tokens",
                    "ts": (now - timedelta(days=0 if i % 2 else 9)).isoformat(),
                    "slot": f"chat-{i}-1",
                    "model": "m",
                    "credits": 1.0 + i,
                }
            )
            for i in range(200)
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(usage_mod, "_shards_in_window", lambda days: [shard])
    monkeypatch.setattr(usage_mod, "_COST_CACHE", None)
    monkeypatch.setattr(usage_mod, "_COST_CACHE_KEY", None)
    cost = usage_mod.cost_breakdown(7)
    assert peaks and max(peaks) <= 8
    spend = cost["slot_spend"]
    assert "sessions" not in spend["overflow"]
    kept_cur = sum(v["credits"] for v in spend["current"].values())
    kept_prior = sum(spend["prior"].values())
    assert kept_cur + spend["overflow"]["credits"] == pytest.approx(cost["credits"])
    assert kept_prior + spend["overflow"]["prior_credits"] == pytest.approx(cost["prior_credits"])


def test_totals_stay_exact_across_eviction_and_returning_keys(monkeypatch):
    # Evicted keys come back later in the stream (the same session in a later
    # shard). The first pass keeps them by their partial rank, the second sums
    # every row exactly, so kept + overflow is every credit and every turn.
    monkeypatch.setattr(usage_mod, "_COST_SLOT_SPEND_CAP", 2)
    chooser = usage_mod._SlotSpend()
    rows: list[tuple[str, float, bool]] = []
    cur_total = prior_total = 0.0
    turns = 0
    for i in range(300):
        key = f"k{(i * 7) % 23}"
        credits = float((i * 13) % 11)
        prior = i % 4 == 0
        rows.append((key, credits, prior))
        chooser.add(key, credits, prior=prior)
        if prior:
            prior_total += credits
        else:
            cur_total += credits
            turns += 1
        assert len(chooser) <= 4
    out = _two_pass(rows)
    assert sum(v["credits"] for v in out["current"].values()) + out["overflow"][
        "credits"
    ] == pytest.approx(cur_total)
    assert sum(int(v["turns"]) for v in out["current"].values()) + out["overflow"]["turns"] == turns
    assert sum(out["prior"].values()) + out["overflow"]["prior_credits"] == pytest.approx(
        prior_total
    )


def test_kept_set_is_the_top_cap_when_no_evicted_key_returns(monkeypatch):
    # Each key's spend arrives in one row, so an evicted key never returns, and
    # the kept set must be exactly what ranking the whole population would keep.
    monkeypatch.setattr(usage_mod, "_COST_SLOT_SPEND_CAP", 5)
    rows = [(f"s{i:03d}", float((i * 37) % 101), i % 5 == 0) for i in range(120)]
    out = _two_pass(rows)

    def rank(row: tuple[str, float, bool]) -> tuple[float, float, str]:
        key, credits, prior = row
        return (0.0 if prior else -credits, -credits if prior else 0.0, key)

    want = {key for key, _, _ in sorted(rows, key=rank)[:5]}
    assert {*out["current"], *out["prior"]} == want


@pytest.mark.asyncio
async def test_capped_spend_lands_in_unfiled_with_its_credits():
    slots = {"chat-1-1": SimpleNamespace(folder_id="f-dev")}
    payload = _cost({"chat-1-1": {"credits": 10.0, "turns": 1}}, {"chat-1-1": 5.0})
    payload["slot_spend"]["overflow"] = {"credits": 4.0, "turns": 2, "prior_credits": 1.0}
    out = await _with_folder_spend(_request(slots), payload)
    rows = _by_id(out)
    assert rows[""]["credits"] == 4.0
    assert rows[""]["turns"] == 2
    assert rows[""]["delta_pct"] == 300.0
    # The Unfiled row says how many of its credits are the capped tail, which
    # the panel renders; a filed row never carries it, and no session count
    # rides along any more.
    assert rows[""]["capped_credits"] == 4.0
    assert "capped_credits" not in rows["f-dev"]
    assert "capped_sessions" not in rows[""]
    assert sum(r["credits"] for r in out["by_folder"]) == 14.0


@pytest.mark.asyncio
async def test_zero_credit_capped_sessions_keep_their_turns():
    # A token- or cost-only provider bills no credits, so a capped tail can be
    # all turns and no credits. Its turns must still reach Unfiled, and the
    # disclosure names them (see test_zero_credit_capped_turns_are_disclosed).
    slots = {"chat-1-1": SimpleNamespace(folder_id="f-dev")}
    payload = _cost({"chat-1-1": {"credits": 10.0, "turns": 1}})
    payload["slot_spend"]["overflow"] = {"credits": 0.0, "turns": 5, "prior_credits": 0.0}
    out = await _with_folder_spend(_request(slots), payload)
    rows = _by_id(out)
    assert rows[""]["turns"] == 5
    assert rows[""]["credits"] == 0.0
    assert rows[""]["capped_turns"] == 5


@pytest.mark.asyncio
async def test_an_uncapped_window_carries_no_capped_credits():
    slots = {"chat-1-1": SimpleNamespace(folder_id="")}
    payload = _cost({"chat-1-1": {"credits": 10.0, "turns": 1}})
    payload["slot_spend"]["overflow"] = {"credits": 0.0, "turns": 0, "prior_credits": 0.0}
    out = await _with_folder_spend(_request(slots), payload)
    assert "capped_credits" not in _by_id(out)[""]
    assert [r["folder_id"] for r in out["by_folder"]] == [""]


# ── Every session-key family the usage-row writers emit ─────────────────────
#
# ``persist_token_record_async`` is called with these keys (grep of its call
# sites): the dashboard slot name (``chat-<n>-<ts>``, ``cron-<job>``,
# ``slack_<ts>``, ``task-review-<token>``; chat_runner), a ``dashboard:`` session
# key (llm_helpers), a channel key (``slack:<ts>``; slack gateway), a cron
# execution key (``cron:<job>``, ``cron:<job>:<run>``, ``cron:<job>:<agent>``;
# slack gateway), a task-runner key (``taskrunner:<id>:task<n>`` / ``:review``),
# and machine namespaces with no tab (``hook:``, ``wf:``,
# ``memory-consolidation:``). ``subagent:`` rows never reach the roll-up.


@pytest.fixture
def surfaced():
    from kiro_crew import session_surface

    before = session_surface.dashboard_surfaced_keys()

    def publish(*keys: str) -> None:
        session_surface.set_dashboard_surfaced(keys)

    yield publish
    session_surface.set_dashboard_surfaced(before)


async def _folder_of(key: str, slots: dict, log=None) -> str:
    out = await _with_folder_spend(
        _request(slots, conversation_log=log), _cost({key: {"credits": 1.0, "turns": 1}})
    )
    (row,) = out["by_folder"]
    return row["folder_id"]


@pytest.mark.asyncio
async def test_family_dashboard_session_key_finds_its_live_tab():
    slots = {"chat-1-1": SimpleNamespace(folder_id="f-dev")}
    assert await _folder_of("dashboard:chat-1-1", slots) == "f-dev"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["cron:job1", "cron:job1:run-7", "cron:job1:agent-x"])
async def test_family_cron_execution_key_finds_the_jobs_live_tab(key, surfaced):
    # The surface registry holds the tab's linked key, ``cron:<job>``.
    surfaced("cron:job1")
    slots = {"cron-job1": SimpleNamespace(folder_id="f-ops")}
    assert await _folder_of(key, slots) == "f-ops"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["cron:job1", "cron:job1:run-7", "cron:job1:agent-x"])
async def test_family_cron_execution_key_reads_the_jobs_transcript_when_closed(key, tmp_path):
    log = ConversationLog(tmp_path)
    log.update_metadata("cron:job1", {"folder_id": "f-ops"})
    assert await _folder_of(key, {}, log) == "f-ops"


@pytest.mark.asyncio
async def test_family_cron_tab_slot_reads_the_jobs_transcript_when_closed(tmp_path):
    # A follow-up turn typed in the cron tab is keyed by the slot name
    # ``cron-<job>``, but the tab's transcript is the linked ``cron:<job>``.
    log = ConversationLog(tmp_path)
    log.update_metadata("cron:job1", {"folder_id": "f-ops"})
    assert await _folder_of("cron-job1", {}, log) == "f-ops"


@pytest.mark.asyncio
async def test_family_channel_key_finds_its_live_tab(surfaced):
    surfaced("slack:1785370133.085469")
    slots = {"slack_1785370133.085469": SimpleNamespace(folder_id="f-dev")}
    assert await _folder_of("slack:1785370133.085469", slots) == "f-dev"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["slack:1785370133.085469", "slack_1785370133.085469"])
async def test_family_channel_keys_read_the_channel_transcript_when_closed(key, tmp_path):
    log = ConversationLog(tmp_path)
    log.update_metadata("slack:1785370133.085469", {"folder_id": "f-dev"})
    assert await _folder_of(key, {}, log) == "f-dev"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        "taskrunner:t1:task0",
        "taskrunner:t1:review",
        "hook:default:1785370133",
        "wf:run1:0",
        "memory-consolidation:default:abc",
    ],
)
async def test_family_namespaced_keys_read_their_own_transcript(key, tmp_path):
    log = ConversationLog(tmp_path)
    log.update_metadata(key, {"folder_id": "f-dev"})
    assert await _folder_of(key, {}, log) == "f-dev"


@pytest.mark.asyncio
async def test_family_closed_task_review_tab_stays_unfiled(tmp_path):
    # Known limit: the tab's transcript lives under its linked task-runner key,
    # and nothing persists the slot -> linked-key mapping once the tab closes.
    log = ConversationLog(tmp_path)
    log.update_metadata("taskrunner:t1:review", {"folder_id": "f-dev"})
    assert await _folder_of("task-review-tok", {}, log) == ""


@pytest.mark.asyncio
async def test_a_dashboard_tab_named_like_a_cron_tab_keeps_its_own_folder(tmp_path):
    # A slot name is client-supplied (``POST /api/chat/slots``, an OpenAI-compat
    # id), so a dashboard tab can be called ``cron-foo`` without being one. Its
    # own transcript answers first; an unrelated job's ``cron:foo`` must not.
    log = ConversationLog(tmp_path)
    log.update_metadata(slot_transcript_key("cron-foo"), {"folder_id": "f-dev"})
    log.update_metadata("cron:foo", {"folder_id": "f-ops"})
    assert await _folder_of("cron-foo", {}, log) == "f-dev"


@pytest.mark.asyncio
async def test_an_unfiled_tab_named_like_a_cron_tab_stays_unfiled(tmp_path):
    # The tab's own transcript exists but names no folder: that is the answer
    # (Unfiled). Falling through to ``cron:foo`` would hand its spend to an
    # unrelated job's folder.
    log = ConversationLog(tmp_path)
    log.update_metadata(slot_transcript_key("cron-foo"), {"title": "mine"})
    log.update_metadata("cron:foo", {"folder_id": "f-ops"})
    assert await _folder_of("cron-foo", {}, log) == ""


@pytest.mark.asyncio
async def test_a_real_cron_tab_still_reads_the_jobs_transcript(tmp_path):
    # A real cron tab has no ``dashboard:cron-<job>`` transcript: its
    # conversation is the linked ``cron:<job>``, which is the fallback.
    log = ConversationLog(tmp_path)
    log.update_metadata("cron:foo", {"folder_id": "f-ops"})
    assert await _folder_of("cron-foo", {}, log) == "f-ops"


def test_usage_transcript_keys_live_beside_the_slot_key_rules():
    from kiro_crew.dashboard.chat_utils import usage_transcript_keys

    assert usage_transcript_keys("cron:job1:run-7") == ("cron:job1",)
    assert usage_transcript_keys("cron-job1") == (slot_transcript_key("cron-job1"), "cron:job1")
    assert usage_transcript_keys("slack:1.2") == ("slack:1.2",)
    assert usage_transcript_keys("chat-1-1") == (slot_transcript_key("chat-1-1"),)


# ── One key set for both periods ───────────────────────────────────────────


def test_both_periods_are_capped_on_one_key_set(monkeypatch):
    # A session kept for the current period must keep its prior spend too, or
    # its folder's delta compares this week's spend with nothing.
    monkeypatch.setattr(usage_mod, "_COST_SLOT_SPEND_CAP", 2)
    out = _spend(
        {"a": {"credits": 9.0, "turns": 1}, "b": {"credits": 5.0, "turns": 2}},
        {"c": 100.0, "d": 50.0, "a": 1.0},
    )
    assert set(out["current"]) == {"a", "b"}
    assert out["prior"] == {"a": 1.0}
    assert out["overflow"]["prior_credits"] == 150.0
    assert out["overflow"]["credits"] == 0.0
    assert "sessions" not in out["overflow"]


def test_over_long_session_keys_are_counted_not_retained():
    # The count cap bounds memory only if every retained key is bounded too: an
    # API-supplied slot name of any length would otherwise ride the memoised
    # payload. An over-long key is never admitted, not even transiently: its
    # spend is summed on its own (``over_long``), in both periods.
    limit = usage_mod._COST_SLOT_KEY_MAX_CHARS
    huge = "chat-" + "x" * (limit * 4)
    edge = "c" * limit
    acc = usage_mod._SlotSpend()
    acc.add(huge, 7.0)
    assert len(acc) == 0
    out = _spend(
        {
            huge: {"credits": 7.0, "turns": 3},
            edge: {"credits": 1.0, "turns": 1},
            "chat-1-1": {"credits": 2.0, "turns": 1},
        },
        {huge: 4.0, "chat-1-1": 1.5},
    )
    assert set(out["current"]) == {edge, "chat-1-1"}
    assert out["prior"] == {"chat-1-1": 1.5}
    assert all(len(k) <= limit for k in (*out["current"], *out["prior"]))
    assert out["over_long"] == {"credits": 7.0, "turns": 3, "prior_credits": 4.0}


def test_prior_only_sessions_rank_after_current_spenders(monkeypatch):
    monkeypatch.setattr(usage_mod, "_COST_SLOT_SPEND_CAP", 3)
    out = _spend(
        {"a": {"credits": 1.0, "turns": 1}},
        {"c": 2.0, "d": 50.0, "e": 0.5},
    )
    assert set(out["current"]) == {"a"}
    assert out["prior"] == {"d": 50.0, "c": 2.0}
    assert out["overflow"]["prior_credits"] == 0.5


# ── Closed-transcript reads are memoised per cost payload ──────────────────


@pytest.mark.asyncio
async def test_closed_transcript_reads_once_per_cost_payload(tmp_path):
    log = ConversationLog(tmp_path)
    log.update_metadata(slot_transcript_key("chat-9-9"), {"folder_id": "f-dev"})
    calls: list[str] = []
    real = log.get_metadata

    def counting(key):
        calls.append(key)
        return real(key)

    log.get_metadata = counting  # type: ignore[method-assign]
    payload = _cost({"chat-9-9": {"credits": 7.0, "turns": 1}})
    for _ in range(3):
        out = await _with_folder_spend(_request({}, conversation_log=log), payload)
        assert _by_id(out)["f-dev"]["credits"] == 7.0
    assert len(calls) == 1
    # A new payload (the cost cache refreshed) reads again.
    await _with_folder_spend(
        _request({}, conversation_log=log), _cost({"chat-9-9": {"credits": 7.0, "turns": 1}})
    )
    assert len(calls) == 2


# ── Kept keys are chosen first, then aggregated exactly ────────────────────


def _token_row(slot: str, credits: float, age_days: float) -> dict:
    return {
        "_type": "tokens",
        "ts": (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat(),
        "slot": slot,
        "model": "m",
        "credits": credits,
    }


def _write_shard(tmp_path, rows: list[dict], monkeypatch):
    shard = tmp_path / "shard.jsonl"
    shard.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(usage_mod, "_shards_in_window", lambda days: [shard])
    monkeypatch.setattr(usage_mod, "_COST_CACHE", None)
    monkeypatch.setattr(usage_mod, "_COST_CACHE_KEY", None)
    return shard


def test_a_returning_sessions_prior_spend_survives_eviction(tmp_path, monkeypatch):
    # The session's prior row is read first, then 10,001 other keys force a
    # fold that evicts it (it has no current spend yet), then its current rows
    # arrive and it ends among the kept. Its prior credits must come with it,
    # or its folder shows this week's spend as "new" while last week's sits in
    # Unfiled. Real cap, so the fold happens exactly where production's does.
    others = 2 * usage_mod._COST_SLOT_SPEND_CAP + 1
    rows = [_token_row("chat-x-1", 3.0, 9)]
    rows += [_token_row(f"chat-{i}-1", 1.0, 0) for i in range(others)]
    rows += [_token_row("chat-x-1", 50.0, 0), _token_row("chat-x-1", 0.0, 0)]
    _write_shard(tmp_path, rows, monkeypatch)

    cost = usage_mod.cost_breakdown(7)
    spend = cost["slot_spend"]
    assert spend["current"]["chat-x-1"] == {"credits": 50.0, "turns": 2}
    assert spend["prior"] == {"chat-x-1": 3.0}
    assert len({*spend["current"], *spend["prior"]}) == usage_mod._COST_SLOT_SPEND_CAP
    # The overflow holds only the other sessions past the cap, none of X's.
    assert spend["overflow"]["prior_credits"] == 0.0
    kept_others = len(spend["current"]) - 1
    assert spend["overflow"]["credits"] == pytest.approx(float(others - kept_others))
    assert spend["overflow"]["turns"] == others - kept_others
    assert sum(v["credits"] for v in spend["current"].values()) + spend["overflow"][
        "credits"
    ] == pytest.approx(cost["credits"])


def test_the_exact_pass_holds_only_the_kept_keys(tmp_path, monkeypatch):
    # Pass 2 accumulates exact spend for the kept keys only; every other row
    # goes straight to the overflow, so its map is bounded by the kept set.
    monkeypatch.setattr(usage_mod, "_COST_SLOT_SPEND_CAP", 4)
    seen: dict[str, int] = {"kept": 0, "peak": 0}

    class Watched(usage_mod._ExactSlotSpend):
        def __init__(self, kept) -> None:
            super().__init__(kept)
            seen["kept"] = len(kept)

        def add(self, key: str, credits: float, *, prior: bool = False) -> None:
            super().add(key, credits, prior=prior)
            seen["peak"] = max(seen["peak"], len(self))

    monkeypatch.setattr(usage_mod, "_ExactSlotSpend", Watched)
    _write_shard(
        tmp_path,
        [_token_row(f"chat-{i}-1", 1.0 + i, 0 if i % 2 else 9) for i in range(200)],
        monkeypatch,
    )
    usage_mod.cost_breakdown(7)
    assert 0 < seen["kept"] <= 4
    assert seen["peak"] <= seen["kept"]


def test_rows_appended_between_the_passes_are_not_counted(tmp_path, monkeypatch):
    # The exact pass re-reads only the bytes the first pass read, so a turn
    # persisted in between cannot reach the kept spend or the overflow without
    # also being in the totals.
    monkeypatch.setattr(usage_mod, "_COST_SLOT_SPEND_CAP", 2)
    shard = _write_shard(
        tmp_path, [_token_row(f"chat-{i}-1", 1.0 + i, 0) for i in range(6)], monkeypatch
    )
    real = usage_mod._ExactSlotSpend

    class Appending(real):  # type: ignore[misc, valid-type]
        def __init__(self, kept) -> None:
            super().__init__(kept)
            with shard.open("a", encoding="utf-8") as fh:
                for key in (*kept, "chat-99-1"):
                    fh.write(json.dumps(_token_row(key, 100.0, 0)) + "\n")

    monkeypatch.setattr(usage_mod, "_ExactSlotSpend", Appending)
    cost = usage_mod.cost_breakdown(7)
    spend = cost["slot_spend"]
    assert sum(v["credits"] for v in spend["current"].values()) + spend["overflow"][
        "credits"
    ] == pytest.approx(cost["credits"])
    assert cost["credits"] == pytest.approx(21.0)


@pytest.mark.asyncio
async def test_the_unfiled_row_carries_the_cap_it_was_capped_at():
    # The panel names the limit; the backend owns it, so it rides the payload
    # next to the capped credits rather than being baked into the copy.
    payload = _cost({"chat-1-1": {"credits": 10.0, "turns": 1}})
    payload["slot_spend"]["overflow"] = {"credits": 4.0, "turns": 2, "prior_credits": 0.0}
    out = await _with_folder_spend(_request({}), payload)
    assert _by_id(out)[""]["capped_limit"] == usage_mod._COST_SLOT_SPEND_CAP
    payload["slot_spend"]["overflow"] = {"credits": 0.0, "turns": 0, "prior_credits": 0.0}
    out = await _with_folder_spend(_request({}), payload)
    assert "capped_limit" not in _by_id(out)[""]


# ── What Unfiled absorbs from overflow is disclosed whole, per cause ────────
#
# One disclosure model: every row Unfiled takes in from outside the kept set is
# reported on the Unfiled row by cause, credits AND turns, whenever either is
# non-zero. The two causes are the count cap (``capped_*``, with the cap as
# ``capped_limit``) and a session key too long to retain (``long_key_*``).


def _with_overflow(capped=None, long_key=None) -> dict:
    payload = _cost({"chat-1-1": {"credits": 10.0, "turns": 1}})
    zero = {"credits": 0.0, "turns": 0, "prior_credits": 0.0}
    payload["slot_spend"]["overflow"] = capped or dict(zero)
    payload["slot_spend"]["over_long"] = long_key or dict(zero)
    return payload


@pytest.mark.asyncio
async def test_zero_credit_capped_turns_are_disclosed():
    # A token- or cost-only provider bills turns with no credits. Those turns
    # are in Unfiled, so the disclosure must name them even with 0 credits.
    payload = _with_overflow(capped={"credits": 0.0, "turns": 5, "prior_credits": 0.0})
    row = _by_id(await _with_folder_spend(_request({}), payload))[""]
    assert row["capped_turns"] == 5
    assert row["capped_credits"] == 0.0
    assert row["capped_limit"] == usage_mod._COST_SLOT_SPEND_CAP


@pytest.mark.asyncio
async def test_the_capped_disclosure_carries_credits_and_turns():
    # Only the overflow's own turns: the Unfiled sessions that are simply in no
    # folder are not part of the capped figure.
    payload = _with_overflow(capped={"credits": 4.0, "turns": 2, "prior_credits": 0.0})
    row = _by_id(await _with_folder_spend(_request({}), payload))[""]
    assert row["turns"] == 3
    assert (row["capped_credits"], row["capped_turns"]) == (4.0, 2)
    assert "long_key_credits" not in row and "long_key_turns" not in row


def test_over_long_keys_are_kept_apart_from_the_count_cap():
    # An over-long key is not "beyond the cap": the window can hold one session
    # and still meet one. Its spend is summed on its own, never in the overflow.
    huge = "chat-" + "x" * (usage_mod._COST_SLOT_KEY_MAX_CHARS * 2)
    out = _spend(
        {huge: {"credits": 7.0, "turns": 3}, "chat-1-1": {"credits": 2.0, "turns": 1}},
        {huge: 4.0},
    )
    assert out["overflow"] == {"credits": 0.0, "turns": 0, "prior_credits": 0.0}
    assert out["over_long"] == {"credits": 7.0, "turns": 3, "prior_credits": 4.0}


@pytest.mark.asyncio
async def test_over_long_key_spend_is_disclosed_without_the_cap():
    payload = _with_overflow(long_key={"credits": 2.0, "turns": 3, "prior_credits": 1.0})
    rows = _by_id(await _with_folder_spend(_request({}), payload))
    row = rows[""]
    assert (row["long_key_credits"], row["long_key_turns"]) == (2.0, 3)
    for key in ("capped_credits", "capped_turns", "capped_limit"):
        assert key not in row
    # Counted in Unfiled in both periods, so the rows still add up.
    assert row["credits"] == 12.0 and row["turns"] == 4
    assert row["delta_pct"] == 1100.0


@pytest.mark.asyncio
async def test_zero_credit_over_long_turns_are_disclosed():
    payload = _with_overflow(long_key={"credits": 0.0, "turns": 2, "prior_credits": 0.0})
    row = _by_id(await _with_folder_spend(_request({}), payload))[""]
    assert (row["long_key_credits"], row["long_key_turns"]) == (0.0, 2)


@pytest.mark.asyncio
async def test_both_overflow_causes_are_disclosed_separately():
    payload = _with_overflow(
        capped={"credits": 4.0, "turns": 2, "prior_credits": 0.0},
        long_key={"credits": 1.0, "turns": 1, "prior_credits": 0.0},
    )
    row = _by_id(await _with_folder_spend(_request({}), payload))[""]
    assert (row["capped_credits"], row["capped_turns"]) == (4.0, 2)
    assert (row["long_key_credits"], row["long_key_turns"]) == (1.0, 1)
    assert row["credits"] == 15.0


def test_cost_breakdown_reports_over_long_spend_apart(tmp_path, monkeypatch):
    huge = "chat-" + "y" * (usage_mod._COST_SLOT_KEY_MAX_CHARS * 2)
    _write_shard(
        tmp_path,
        [_token_row(huge, 5.0, 0), _token_row(huge, 2.0, 9), _token_row("chat-1-1", 1.0, 0)],
        monkeypatch,
    )
    cost = usage_mod.cost_breakdown(7)
    spend = cost["slot_spend"]
    assert spend["over_long"] == {"credits": 5.0, "turns": 1, "prior_credits": 2.0}
    assert spend["overflow"] == {"credits": 0.0, "turns": 0, "prior_credits": 0.0}
    assert spend["current"] == {"chat-1-1": {"credits": 1.0, "turns": 1}}


# ── A live slot whose name is not the key's tab name still answers ──────────


@pytest.mark.asyncio
async def test_a_live_slot_linked_under_another_name_keeps_its_folder(tmp_path):
    # A ``workflow-<run>`` fallback tab linked to ``slack:<ts>`` (workflow_inject)
    # displays that thread, but its name is not the folded channel key, so
    # ``dashboard_slot_key`` cannot reach it. Rows the Slack side writes under
    # ``slack:<ts>`` must still take the open tab's folder, not the transcript's
    # older one.
    log = ConversationLog(tmp_path)
    log.update_metadata("slack:1785370133.085469", {"folder_id": "f-dev"})
    slots = {"workflow-r1": SimpleNamespace(folder_id="f-ops")}
    out = await _with_folder_spend(
        _request(slots, conversation_log=log, aliases={"slack:1785370133.085469": "workflow-r1"}),
        _cost({"slack:1785370133.085469": {"credits": 1.0, "turns": 1}}),
    )
    assert [r["folder_id"] for r in out["by_folder"]] == ["f-ops"]


@pytest.mark.asyncio
async def test_a_cron_run_key_reaches_a_tab_linked_to_its_job():
    slots = {"workflow-r2": SimpleNamespace(folder_id="f-ops")}
    out = await _with_folder_spend(
        _request(slots, aliases={"cron:job9": "workflow-r2"}),
        _cost({"cron:job9:run-1": {"credits": 1.0, "turns": 1}}),
    )
    assert [r["folder_id"] for r in out["by_folder"]] == ["f-ops"]


# ── Titles keep main's single-transcript lookup ─────────────────────────────


@pytest.mark.parametrize("key", ["cron:job1", "cron:job1:run-7", "cron-job1"])
def test_titles_do_not_borrow_the_cron_jobs_transcript(key, tmp_path):
    # Folders read the job's transcript for a cron run or tab; titles did not
    # before this change (main resolves every key with ``slot_transcript_key``,
    # which files a ``cron:`` key under ``dashboard:cron:...``), and the folder
    # lookup must not change what a row is called.
    from kiro_crew.dashboard.handlers.telemetry import _persisted_titles

    log = ConversationLog(tmp_path)
    log.update_metadata("cron:job1", {"title": "Nightly job", "folder_id": "f-ops"})
    assert _persisted_titles(log, [key]) == {}


def test_titles_still_read_the_keys_own_transcript(tmp_path):
    from kiro_crew.dashboard.handlers.telemetry import _persisted_titles

    log = ConversationLog(tmp_path)
    log.update_metadata(slot_transcript_key("chat-1-1"), {"title": "Mine"})
    log.update_metadata("slack:1.2", {"title": "Thread"})
    assert _persisted_titles(log, ["chat-1-1", "slack:1.2"]) == {
        "chat-1-1": "Mine",
        "slack:1.2": "Thread",
    }
