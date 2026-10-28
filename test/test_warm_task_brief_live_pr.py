"""Live pull-request state in the warm task brief. GitHub is never called."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import slot_retention, warm_task_brief


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _item(
    item_id: str = "it_00000001",
    *,
    state: str = "open",
    acceptance: object = None,
    pr: object = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        item_id=item_id,
        title="Ship the fix",
        acceptance=(
            {"kind": "pr_checks", "pr": 7, "repo": "acme/widgets"}
            if acceptance is None
            else acceptance
        ),
        state=state,
        verdict=None,
        decision="",
        worker_session_key="worker-1",
        status="blocked",
        reason="approval",
        summary="waiting for approval",
        artifacts={},
        pr=pr,
        last_report_at="",
        created_at="",
        closed_at=None,
        decided_at=None,
    )


def _observation(**fields) -> SimpleNamespace:
    buckets = fields.pop("buckets", {"passing": 3, "failing": 1, "pending": 2})
    base = {
        "reached": True,
        "is_terminal": False,
        "state": "OPEN",
        "mergeability": "CONFLICTING",
        "merge_state": "DIRTY",
        "review_decision": "REVIEW_REQUIRED",
        "observed_at": 1_800_000_000.0,
        "checks_complete": True,
        "bucket": lambda name: ("x",) * buckets.get(name, 0),
    }
    base.update(fields)
    return SimpleNamespace(**base)


def _snapshot(refs: tuple[tuple[str, str, int], ...]) -> warm_task_brief.TaskSnapshot:
    return warm_task_brief.TaskSnapshot(
        slot_key="thread-x",
        history_key="thread-x",
        original_request="Fix the failing test",
        fingerprint="fp",
        transcript="user: Fix the failing test",
        session_state={},
        conductor=None,
        work_items=(),
        previous_claim="",
        pr_refs=refs,
    )


def test_refs_skip_repo_less_and_terminal_items_and_keep_the_newest():
    items = [
        _item("it_repoless", acceptance={"kind": "pr_checks", "pr": 3}),
        _item("it_closed", state="accepted"),
        _item("it_claim", acceptance={"kind": "pr_checks", "pr": "TBD", "repo": "a/b"}, pr=9),
        _item("it_bar", acceptance={"kind": "pr_checks", "pr": 5, "repo": "a/b"}, pr=99),
        _item("it_none", acceptance={"kind": "pr_checks", "pr": True, "repo": "a/b"}),
    ]
    assert warm_task_brief._pr_refs(items) == (("it_claim", "a/b", 9), ("it_bar", "a/b", 5))
    many = [_item(f"it_{n:08d}", pr=n) for n in range(1, 10)]
    refs = warm_task_brief._pr_refs(many)
    assert len(refs) == warm_task_brief._MAX_LIVE_PRS
    assert refs[-1][0] == "it_00000009"


@pytest.mark.asyncio
async def test_live_state_is_enums_counts_and_a_time(monkeypatch):
    seen: list[tuple[str, int]] = []

    def fetch(repo, pr):
        seen.append((repo, pr))
        if pr == 2:
            return _observation(is_terminal=True, state="MERGED", mergeability="UNKNOWN")
        return _observation(merge_state="DIRTY <b>now</b>")

    monkeypatch.setattr(warm_task_brief, "_fetch_pr", fetch)
    live = await warm_task_brief._live_pull_requests(
        _snapshot((("it_a", "acme/widgets", 1), ("it_b", "acme/widgets", 2)))
    )
    assert sorted(seen) == [("acme/widgets", 1), ("acme/widgets", 2)]
    assert live[0] == {
        "item_id": "it_a",
        "repo": "acme/widgets",
        "pr": 1,
        "state": "OPEN",
        "mergeable": "CONFLICTING",
        "merge_state": "DIRTYBNOWB",
        "review_decision": "REVIEW_REQUIRED",
        "observed_at": "2027-01-15T08:00:00+00:00",
        "checks": {"passing": 3, "failing": 1, "pending": 2, "complete": True},
    }
    # An ended pull request has no board to count.
    assert live[1]["state"] == "MERGED" and "checks" not in live[1]


@pytest.mark.asyncio
async def test_a_failed_or_unreached_read_drops_only_its_entry(monkeypatch):
    def fetch(repo, pr):
        if pr == 1:
            raise RuntimeError("gh not found")
        return _observation(reached=pr != 2)

    monkeypatch.setattr(warm_task_brief, "_fetch_pr", fetch)
    refs = (("it_a", "a/b", 1), ("it_b", "a/b", 2), ("it_c", "a/b", 3))
    live = await warm_task_brief._live_pull_requests(_snapshot(refs))
    assert [entry["pr"] for entry in live] == [3]


@pytest.mark.asyncio
async def test_a_slow_forge_is_abandoned_at_the_timeout(monkeypatch):
    release = threading.Event()

    def fetch(repo, pr):
        release.wait(5)
        return _observation()

    monkeypatch.setattr(warm_task_brief, "_fetch_pr", fetch)
    monkeypatch.setattr(warm_task_brief, "_LIVE_PR_TIMEOUT_SECS", 0.05)
    try:
        assert await warm_task_brief._live_pull_requests(_snapshot((("it_a", "a/b", 1),))) == []
    finally:
        release.set()


@pytest.mark.asyncio
async def test_no_refs_reads_nothing(monkeypatch):
    def fetch(repo, pr):
        raise AssertionError("no read without a ref")

    monkeypatch.setattr(warm_task_brief, "_fetch_pr", fetch)
    assert await warm_task_brief._live_pull_requests(_snapshot(())) == []


def test_prompt_carries_the_live_rule_only_with_live_state():
    snapshot = _snapshot(())
    plain = warm_task_brief._build_prompt(snapshot)
    assert plain == warm_task_brief._build_prompt(snapshot, "default", [])
    assert "live_pull_requests" not in plain
    live = warm_task_brief._build_prompt(snapshot, "default", [{"pr": 1, "state": "MERGED"}])
    assert '"live_pull_requests": [{"pr": 1, "state": "MERGED"}]' in live
    assert "the live state wins" in live


def test_a_large_ledger_never_cuts_the_live_block():
    big = ({"item_id": "it_big", "artifacts": {f"k{n}": "x" * 512 for n in range(16)}},) * 6
    snapshot = replace(_snapshot(()), work_items=big)
    prompt = warm_task_brief._build_prompt(snapshot, "default", [{"pr": 1, "state": "MERGED"}])
    assert '"live_pull_requests": [{"pr": 1, "state": "MERGED"}]' in prompt
    assert prompt.index("live_pull_requests") < prompt.index('"work_items"')
    # The ledger itself is what the length cap cut.
    assert '"transcript"' not in prompt


def test_fetch_reuses_the_shared_fetcher_with_its_own_audit_tag(monkeypatch):
    from kiro_crew.probes import gh_pr

    calls: list[tuple[object, dict]] = []
    monkeypatch.setattr(gh_pr, "fetch", lambda message, **kw: calls.append((message, kw)) or "obs")
    assert warm_task_brief._fetch_pr("acme/widgets", 7) == "obs"
    assert calls == [
        (
            '{"repo": "acme/widgets", "pr": 7}',
            {"budget_secs": 4.0, "audit_caller": "core:warm-task-brief"},
        )
    ]


# --- one whole brief, end to end, with the model and GitHub both faked ---


class _State:
    def __init__(self, log) -> None:
        self._foreground_slots: dict[int, str] = {}
        self._warm_task_brief_last_seen: dict[str, float] = {}
        self._warm_task_brief_not_before: dict[str, float] = {}
        self._warm_task_brief_tasks: dict = {}
        self._warm_task_brief_model_active: set[str] = set()
        self._warm_task_brief_abandoned: set[str] = set()
        self._warm_task_brief_opened: set[str] = set()
        self.slots = self._slots = {}
        self.sessions = object()
        self.conversation_log = log

    def broadcast_ws_owners(self, kind: str, data: dict) -> None:
        pass

    def broadcast_ws(self, kind: str, data: dict) -> None:
        pass

    def get_slot(self, key: str):
        return self.slots.get(key)

    def push_slots_update(self) -> None:
        pass

    def flush_slot_now(self, slot) -> bool:
        return True


class _Slot:
    def __init__(self, rows: list[dict]) -> None:
        self.key = "thread-x"
        self.running = False
        self._created_by = ""
        self._revived_by = ""
        self.linked_session_key = ""
        self.channel_origin = False
        self.mode = "member"
        self._has_reader = False
        self.rows = self.messages = rows
        self._preparing_count = 0

    def append(self, role: str, content: str, cls: str = "", **kwargs):
        row = {"role": role, "content": content, "cls": cls, "ts": _iso(time.time())}
        if kwargs.get("meta") is not None:
            row["meta"] = kwargs["meta"]
        self.rows.append(row)
        return row


class _Log:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def derive_messages_chained(self, key: str) -> list[dict]:
        return list(self.rows)


def _thread(monkeypatch, fetch):
    rows = [{"role": "user", "content": "Ship the fix", "ts": _iso(time.time() - 7200)}]
    state = _State(_Log(rows))
    slot = _Slot(rows)
    state.slots[slot.key] = slot
    item = _item()
    monkeypatch.setattr(warm_task_brief, "read_state", lambda key: {})
    monkeypatch.setattr(warm_task_brief, "task_records", lambda key: (None, [item]))
    monkeypatch.setattr(warm_task_brief, "_fetch_pr", fetch)
    monkeypatch.setattr(
        warm_task_brief.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: "background")),
    )
    prompts: list[str] = []

    async def run_model(sessions, prompt, **kwargs):
        prompts.append(prompt)
        return '{"headline":"The pull request needs a rebase."}'

    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", run_model)
    return state, slot, prompts


async def _return(state, slot) -> list[str]:
    warm_task_brief.handle_focus_frame(state, 1, slot.key)
    slot_retention.note_foreground(state, 1, slot.key)
    task = state._warm_task_brief_tasks.get(slot.key)
    if task is not None:
        await asyncio.wait_for(task, timeout=30)
    return [row["meta"]["kind"] for row in slot.rows if "meta" in row]


_FULL_BRIEF = ["warm_task_brief_claim", "warm_task_brief_preparing", "warm_task_brief_result"]


@pytest.mark.asyncio
async def test_a_return_reads_the_items_pull_request_into_the_prompt(monkeypatch):
    state, slot, prompts = _thread(monkeypatch, lambda repo, pr: _observation())
    assert await _return(state, slot) == _FULL_BRIEF
    assert len(prompts) == 1
    assert '"mergeable": "CONFLICTING"' in prompts[0]
    assert '"repo": "acme/widgets", "pr": 7' in prompts[0]


@pytest.mark.asyncio
async def test_a_failed_read_still_gives_todays_brief(monkeypatch):
    def fetch(repo, pr):
        raise OSError("no gh")

    state, slot, prompts = _thread(monkeypatch, fetch)
    assert await _return(state, slot) == _FULL_BRIEF
    assert len(prompts) == 1 and "live_pull_requests" not in prompts[0]


def test_pull_request_refs_never_feed_the_fingerprint(monkeypatch):
    state, slot, _ = _thread(monkeypatch, lambda repo, pr: _observation())
    with_pr = warm_task_brief._snapshot_sync(state, slot)
    bare = _item(acceptance={"kind": "human_approval"})
    monkeypatch.setattr(warm_task_brief, "task_records", lambda key: (None, [bare]))
    without_pr = warm_task_brief._snapshot_sync(state, slot)
    assert with_pr is not None and without_pr is not None
    assert with_pr.pr_refs == (("it_00000001", "acme/widgets", 7),)
    assert without_pr.pr_refs == ()
    assert with_pr.fingerprint == without_pr.fingerprint
