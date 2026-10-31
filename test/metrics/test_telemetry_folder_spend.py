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
from kiro_crew.dashboard.handlers import telemetry_folder_spend as telemetry_mod
from kiro_crew.dashboard.handlers import usage as usage_mod
from kiro_crew.dashboard.handlers.telemetry_folder_spend import _folder_paths, _with_folder_spend
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
async def test_an_over_long_stored_folder_id_is_not_retained(tmp_path):
    # The closed-folder memo outlives the request, so every value it keeps must
    # be bounded. A metadata folder_id longer than any real id is dropped before
    # the memo stores it, and the spend still counts, under Unfiled.
    limit = telemetry_mod._FOLDER_ID_MAX_CHARS
    huge = "f" * (limit + 1)
    log = ConversationLog(tmp_path)
    log.update_metadata(slot_transcript_key("chat-9-9"), {"folder_id": huge})
    log.update_metadata(slot_transcript_key("chat-8-8"), {"folder_id": "f-dev"})
    out = await _with_folder_spend(
        _request({}, conversation_log=log),
        _cost(
            {
                "chat-9-9": {"credits": 7.0, "turns": 1},
                "chat-8-8": {"credits": 2.0, "turns": 1},
            }
        ),
    )
    rows = _by_id(out)
    assert rows[""]["credits"] == 7.0
    assert rows["f-dev"]["credits"] == 2.0
    memo = telemetry_mod._CLOSED_FOLDER_MEMO
    assert memo is not None
    assert memo[3] == {"chat-8-8": "f-dev"}


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
    monkeypatch.setattr(usage_mod, "_shards_in_window", lambda days, until=None: [shard])
    monkeypatch.setattr(usage_mod, "_COST_CACHE", None)
    monkeypatch.setattr(usage_mod, "_COST_CACHE_KEY", None)

    spend = usage_mod.cost_breakdown(7)["slot_spend"]
    assert spend["current"] == {"chat-1-1": {"credits": 5.0, "turns": 2}}
    assert spend["prior"] == {"chat-1-1": 2.0, "chat-2-2": 3.0}


def _spend(current: dict, prior: dict | None = None) -> dict:
    """Cut whole-session totals down the way ``cost_breakdown`` does."""
    return usage_mod._slot_spend({k: dict(v) for k, v in current.items()}, dict(prior or {}))


def test_slot_spend_is_capped_and_the_tail_is_summed(monkeypatch):
    # The map rides the memoised payload, so it needs a count bound; the cut
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
    assert out["unsorted"] == {"credits": 3.0, "turns": 7, "prior_credits": 2.0}


def test_the_no_slot_row_is_kept_outside_the_cap(monkeypatch):
    # Spend with no slot is Unfiled whatever happens, so it never takes one of
    # the capped places or lands in the "could not sort" remainder.
    monkeypatch.setattr(usage_mod, "_COST_SLOT_SPEND_CAP", 1)
    out = _spend(
        {"": {"credits": 1.0, "turns": 1}, "a": {"credits": 9.0, "turns": 1}},
        {"": 2.0},
    )
    assert set(out["current"]) == {"", "a"}
    assert out["prior"] == {"": 2.0}
    assert out["unsorted"] == {"credits": 0.0, "turns": 0, "prior_credits": 0.0}


def test_cost_breakdown_caps_the_slot_map_and_sums_the_rest(tmp_path, monkeypatch):
    # The production path: 200 sessions, a cap of 4, and kept plus the
    # remainder is every credit in both periods.
    monkeypatch.setattr(usage_mod, "_COST_SLOT_SPEND_CAP", 4)
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
    monkeypatch.setattr(usage_mod, "_shards_in_window", lambda days, until=None: [shard])
    monkeypatch.setattr(usage_mod, "_COST_CACHE", None)
    monkeypatch.setattr(usage_mod, "_COST_CACHE_KEY", None)
    cost = usage_mod.cost_breakdown(7)
    spend = cost["slot_spend"]
    assert len({*spend["current"], *spend["prior"]}) <= 4
    assert "sessions" not in spend["unsorted"]
    kept_cur = sum(v["credits"] for v in spend["current"].values())
    kept_prior = sum(spend["prior"].values())
    assert kept_cur + spend["unsorted"]["credits"] == pytest.approx(cost["credits"])
    assert kept_prior + spend["unsorted"]["prior_credits"] == pytest.approx(cost["prior_credits"])


def test_cost_breakdown_reads_each_shard_once(tmp_path, monkeypatch):
    # The per-session spend comes from the same pass as every other figure.
    shard = _write_shard(tmp_path, [_token_row("chat-1-1", 1.0, 0)], monkeypatch)
    opened: list[Any] = []
    real = usage_mod.bounded_records

    def counting(fh, path, **kw):
        opened.append(path)
        return real(fh, path, **kw)

    monkeypatch.setattr(usage_mod, "bounded_records", counting)
    usage_mod.cost_breakdown(7)
    assert opened == [shard]


def test_kept_set_is_the_exact_top_cap(monkeypatch):
    monkeypatch.setattr(usage_mod, "_COST_SLOT_SPEND_CAP", 5)
    current = {f"s{i:03d}": {"credits": float((i * 37) % 101), "turns": 1} for i in range(120)}
    prior = {f"p{i:03d}": float(i) for i in range(30)}
    out = _spend(current, prior)

    def rank(key: str) -> tuple[float, float, str]:
        return (-current[key]["credits"] if key in current else 0.0, -prior.get(key, 0.0), key)

    want = set(sorted({*current, *prior}, key=rank)[:5])
    assert {*out["current"], *out["prior"]} == want


@pytest.mark.asyncio
async def test_unsorted_spend_lands_in_unfiled_with_its_credits():
    slots = {"chat-1-1": SimpleNamespace(folder_id="f-dev")}
    payload = _cost({"chat-1-1": {"credits": 10.0, "turns": 1}}, {"chat-1-1": 5.0})
    payload["slot_spend"]["unsorted"] = {"credits": 4.0, "turns": 2, "prior_credits": 1.0}
    out = await _with_folder_spend(_request(slots), payload)
    rows = _by_id(out)
    assert rows[""]["credits"] == 4.0
    assert rows[""]["turns"] == 2
    assert rows[""]["delta_pct"] == 300.0
    # The Unfiled row says how much of it is the unsorted tail, which the panel
    # renders; a filed row never carries it, nor a session count or the cap.
    assert (rows[""]["unsorted_credits"], rows[""]["unsorted_turns"]) == (4.0, 2)
    assert "unsorted_credits" not in rows["f-dev"]
    assert "capped_limit" not in rows[""]
    assert sum(r["credits"] for r in out["by_folder"]) == 14.0


@pytest.mark.asyncio
async def test_zero_credit_unsorted_turns_are_disclosed():
    # A token- or cost-only provider bills no credits, so the tail can be all
    # turns. Its turns must still reach Unfiled, and the disclosure names them.
    slots = {"chat-1-1": SimpleNamespace(folder_id="f-dev")}
    payload = _cost({"chat-1-1": {"credits": 10.0, "turns": 1}})
    payload["slot_spend"]["unsorted"] = {"credits": 0.0, "turns": 5, "prior_credits": 0.0}
    out = await _with_folder_spend(_request(slots), payload)
    rows = _by_id(out)
    assert rows[""]["turns"] == 5
    assert rows[""]["credits"] == 0.0
    assert (rows[""]["unsorted_credits"], rows[""]["unsorted_turns"]) == (0.0, 5)


@pytest.mark.asyncio
async def test_an_uncapped_window_carries_no_unsorted_figures():
    slots = {"chat-1-1": SimpleNamespace(folder_id="")}
    payload = _cost({"chat-1-1": {"credits": 10.0, "turns": 1}})
    payload["slot_spend"]["unsorted"] = {"credits": 0.0, "turns": 0, "prior_credits": 0.0}
    out = await _with_folder_spend(_request(slots), payload)
    assert "unsorted_credits" not in _by_id(out)[""]
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
    assert out["unsorted"] == {"credits": 0.0, "turns": 0, "prior_credits": 150.0}


def test_over_long_session_keys_are_counted_not_retained():
    # The count cap bounds the cache only if every retained key is bounded too:
    # an API-supplied slot name of any length would otherwise ride the memoised
    # payload. An over-long key is never kept: its spend is summed into
    # ``unsorted``, in both periods.
    limit = usage_mod._COST_SLOT_KEY_MAX_CHARS
    huge = "chat-" + "x" * (limit * 4)
    edge = "c" * limit
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
    assert out["unsorted"] == {"credits": 7.0, "turns": 3, "prior_credits": 4.0}


def test_prior_only_sessions_rank_after_current_spenders(monkeypatch):
    monkeypatch.setattr(usage_mod, "_COST_SLOT_SPEND_CAP", 3)
    out = _spend(
        {"a": {"credits": 1.0, "turns": 1}},
        {"c": 2.0, "d": 50.0, "e": 0.5},
    )
    assert set(out["current"]) == {"a"}
    assert out["prior"] == {"d": 50.0, "c": 2.0}
    assert out["unsorted"]["prior_credits"] == 0.5


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


# ── Per-session spend from the single pass ─────────────────────────────────


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
    monkeypatch.setattr(usage_mod, "_shards_in_window", lambda days, until=None: [shard])
    monkeypatch.setattr(usage_mod, "_COST_CACHE", None)
    monkeypatch.setattr(usage_mod, "_COST_CACHE_KEY", None)
    return shard


def test_a_kept_sessions_prior_spend_comes_with_it(tmp_path, monkeypatch):
    # The session's prior row is read first, then many other sessions, then
    # its current rows, and it ends among the kept. Its prior credits must come
    # with it, or its folder shows this week's spend as "new" while last week's
    # sits in Unfiled.
    monkeypatch.setattr(usage_mod, "_COST_SLOT_SPEND_CAP", 3)
    others = 20
    rows = [_token_row("chat-x-1", 3.0, 9)]
    rows += [_token_row(f"chat-{i}-1", 1.0, 0) for i in range(others)]
    rows += [_token_row("chat-x-1", 50.0, 0), _token_row("chat-x-1", 0.0, 0)]
    _write_shard(tmp_path, rows, monkeypatch)

    cost = usage_mod.cost_breakdown(7)
    spend = cost["slot_spend"]
    assert spend["current"]["chat-x-1"] == {"credits": 50.0, "turns": 2}
    assert spend["prior"] == {"chat-x-1": 3.0}
    assert len({*spend["current"], *spend["prior"]}) == 3
    # The remainder holds only the other sessions past the cap, none of X's.
    assert spend["unsorted"]["prior_credits"] == 0.0
    assert spend["unsorted"]["credits"] == pytest.approx(float(others - 2))
    assert spend["unsorted"]["turns"] == others - 2
    assert sum(v["credits"] for v in spend["current"].values()) + spend["unsorted"][
        "credits"
    ] == pytest.approx(cost["credits"])


def test_cost_breakdown_counts_over_long_spend_as_unsorted(tmp_path, monkeypatch):
    huge = "chat-" + "y" * (usage_mod._COST_SLOT_KEY_MAX_CHARS * 2)
    _write_shard(
        tmp_path,
        [_token_row(huge, 5.0, 0), _token_row(huge, 2.0, 9), _token_row("chat-1-1", 1.0, 0)],
        monkeypatch,
    )
    cost = usage_mod.cost_breakdown(7)
    spend = cost["slot_spend"]
    assert spend["unsorted"] == {"credits": 5.0, "turns": 1, "prior_credits": 2.0}
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
