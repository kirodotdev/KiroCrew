"""Tests for the resource sampler's attribution, dedupe, and gateway-self pass.

Measurement (task 2.1) answers "how big is this tree"; this task answers "whose
tree is it, and is any process counted twice". The behaviours that a naive
attributor gets wrong:

* a runtime whose session resolves to a dashboard slot is a *chat* (its slot and
  session key carried through so the page can link to it), a runtime the subagent
  manager owns is a *subagent* (its task/id/parent enriched from the record), and
  everything else is a *worker* — and the order matters, because a subagent-owner
  runtime that also happens to carry a resolvable session must read as the chat;
* a *shared-runtime* subagent has no owner marker of its own, so it must NOT be
  split into its own entry — its usage rides the host runtime (Req 2.2);
* when two runtimes overlap on the same pids (a shared runtime), a pid must land
  in exactly ONE entry — the first in deterministic order — never both (Req 8.4);
* the gateway self-entry is the gateway's own tree MINUS everything already
  attributed, and it disappears entirely when nothing is left to it (Req 2.4).

The ``/proc`` layer and the gateway self-pid are faked by monkeypatching the
probes the module imports, so the tests are hermetic: they touch neither the
host's real process table nor the concurrently-authored runtime registry.

The property test (Property 1: attribution partition) drives ``snapshot`` over
random overlapping pid forests and asserts every attributed pid appears in
exactly one entry.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.acp import resource_monitor as rm

# Sentinel gateway self-pid, distinct from the runtime pids the tests use.
_GW = 999_001


class _FakeRuntime:
    """A live runtime as the sampler's attribution pass reads it.

    Carries the identity accessors ``_classify`` consults: ``session_keys`` (chat
    detection), ``is_subagent_owner`` (subagent detection), plus the measurement
    identity (``pid``, ``agent``, ``spawn_monotonic``).
    """

    def __init__(
        self,
        pid: int,
        *,
        agent: str = "kirocrew",
        spawn_monotonic: float | None = 100.0,
        session_keys: list[str] | None = None,
        is_subagent_owner: bool = False,
    ) -> None:
        self.pid = pid
        self._agent = agent
        self._spawn_monotonic = spawn_monotonic
        self.session_keys = list(session_keys or [])
        self.is_subagent_owner = is_subagent_owner


class _FakeSubagent:
    """A subagent record as ``_subagent_fields`` reads it (duck-typed)."""

    def __init__(self, id: str, task: str, parent_session_key: str, agent: str = "") -> None:
        self.id = id
        self.task = task
        self.parent_session_key = parent_session_key
        self.agent = agent


def _fake_proc(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tree: dict[int, list[int]],
    gateway_tree: list[int] | None = None,
) -> None:
    """Install a faked Linux ``/proc`` layer plus a pinned gateway self-pid.

    ``tree`` maps a root pid to its descendant pids. ``gateway_tree`` is the
    gateway self-pid's own tree (defaults to empty → no gateway entry). RSS is a
    constant and ticks are zero so the attribution/dedupe logic is isolated.
    """
    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(rm.os, "getpid", lambda: _GW)
    full = {**tree, _GW: list(gateway_tree or [])}
    monkeypatch.setattr(
        rm, "_iter_descendant_pids", lambda pid, max_pids=None: list(full.get(pid, []))
    )
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: 1.0)
    monkeypatch.setattr(rm, "read_pid_stat", lambda proc_root, pid: ("R", 0.0, 0))


# ── per-kind classification ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_runtime_with_a_resolvable_slot_is_a_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_proc(monkeypatch, tree={7: [7]})
    sampler = rm.ResourceSampler(
        live_runtimes=lambda: [_FakeRuntime(7, session_keys=["sess-A"])],
        slot_resolver=lambda key: "slot-1" if key == "sess-A" else None,
    )
    snap = await sampler.snapshot()
    entry = snap.entries[0]
    assert entry.kind == "chat"
    assert entry.slot == "slot-1"
    assert entry.session_key == "sess-A"
    # Label falls back to the session key until the resolver's caller enriches it.
    assert entry.label == "sess-A"


@pytest.mark.asyncio
async def test_first_resolvable_session_wins_on_a_multiplexed_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_proc(monkeypatch, tree={7: [7]})
    sampler = rm.ResourceSampler(
        live_runtimes=lambda: [_FakeRuntime(7, session_keys=["nope", "sess-B", "sess-C"])],
        slot_resolver=lambda key: {"sess-B": "slot-B", "sess-C": "slot-C"}.get(key),
    )
    snap = await sampler.snapshot()
    assert snap.entries[0].slot == "slot-B"
    assert snap.entries[0].session_key == "sess-B"


@pytest.mark.asyncio
async def test_runtime_with_no_resolvable_session_is_a_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_proc(monkeypatch, tree={7: [7]})
    sampler = rm.ResourceSampler(
        live_runtimes=lambda: [_FakeRuntime(7, session_keys=["channel-sess"])],
        slot_resolver=lambda key: None,  # nothing resolves → worker
    )
    snap = await sampler.snapshot()
    entry = snap.entries[0]
    assert entry.kind == "worker"
    assert entry.slot == "" and entry.session_key == ""


@pytest.mark.asyncio
async def test_default_resolver_leaves_runtime_a_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no injected resolver/lookup the sampler classifies everything as a
    worker — the pre-wiring behaviour task 3.1 replaces."""
    _fake_proc(monkeypatch, tree={7: [7]})
    sampler = rm.ResourceSampler(
        live_runtimes=lambda: [_FakeRuntime(7, session_keys=["sess"], is_subagent_owner=False)]
    )
    snap = await sampler.snapshot()
    assert snap.entries[0].kind == "worker"


@pytest.mark.asyncio
async def test_dedicated_subagent_runtime_is_a_subagent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime the manager owns as a dedicated run is a subagent, enriched from
    the record. The runtime itself carries no owner marker — a dedicated child
    process hosts nothing."""
    _fake_proc(monkeypatch, tree={7: [7]})
    record = _FakeSubagent(
        id="agent-42", task="research the options", parent_session_key="parent-sess", agent="claude"
    )
    sampler = rm.ResourceSampler(
        live_runtimes=lambda: [_FakeRuntime(7, is_subagent_owner=False)],
        subagent_lookup=lambda rt: record,
    )
    snap = await sampler.snapshot()
    entry = snap.entries[0]
    assert entry.kind == "subagent"
    assert entry.subagent_id == "agent-42"
    assert entry.label == "research the options"
    assert entry.session_key == "parent-sess"
    assert entry.agent == "claude"


@pytest.mark.asyncio
async def test_owner_marker_without_a_record_is_a_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``is_subagent_owner`` flags a runtime that HOSTS shared subagents; it is
    not itself a subagent. With no manager record and no slot it is a worker."""
    _fake_proc(monkeypatch, tree={7: [7]})
    sampler = rm.ResourceSampler(
        live_runtimes=lambda: [_FakeRuntime(7, is_subagent_owner=True)],
        subagent_lookup=lambda rt: None,  # manager has no record
    )
    snap = await sampler.snapshot()
    entry = snap.entries[0]
    assert entry.kind == "worker"
    assert entry.subagent_id == "" and entry.session_key == ""


@pytest.mark.asyncio
async def test_chat_wins_over_subagent_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime that both resolves to a slot AND has a manager record reads
    as the chat — the operator-visible slot takes precedence."""
    _fake_proc(monkeypatch, tree={7: [7]})
    sampler = rm.ResourceSampler(
        live_runtimes=lambda: [_FakeRuntime(7, session_keys=["sess-A"], is_subagent_owner=True)],
        slot_resolver=lambda key: "slot-1",
        subagent_lookup=lambda rt: _FakeSubagent("a", "t", "p"),
    )
    snap = await sampler.snapshot()
    assert snap.entries[0].kind == "chat"


@pytest.mark.asyncio
async def test_shared_runtime_subagent_is_not_split_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subagent sharing a parent's runtime has NO owner marker, so it produces
    no entry of its own — its usage rides the host runtime (Req 2.2). Only the
    host runtime (a chat here) appears."""
    _fake_proc(monkeypatch, tree={7: [7, 8]})
    sampler = rm.ResourceSampler(
        live_runtimes=lambda: [_FakeRuntime(7, session_keys=["sess-A"], is_subagent_owner=False)],
        slot_resolver=lambda key: "slot-1",
    )
    snap = await sampler.snapshot()
    assert len(snap.entries) == 1
    assert snap.entries[0].kind == "chat"
    # The shared subagent's pids ride the host runtime's tree.
    assert snap.entries[0].pids == frozenset({7, 8})


# ── dedupe ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_overlapping_runtimes_partition_the_pid_union(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two runtimes that overlap on pids must not both claim the shared pids: the
    first in deterministic order keeps them, the later entry loses them."""
    _fake_proc(monkeypatch, tree={7: [7, 8, 9], 10: [9, 11]})
    sampler = rm.ResourceSampler(
        live_runtimes=lambda: [_FakeRuntime(7), _FakeRuntime(10)],
    )
    snap = await sampler.snapshot()
    by_pid = {e.pid: e for e in snap.entries if e.kind != "gateway"}
    # 9 is shared; the earlier root (7) keeps it, root 10 loses it.
    assert by_pid[7].pids == frozenset({7, 8, 9})
    assert by_pid[10].pids == frozenset({11})
    assert by_pid[10].proc_count == 1
    # No pid appears in two entries.
    all_pids = [p for e in snap.entries for p in e.pids]
    assert len(all_pids) == len(set(all_pids))


@pytest.mark.asyncio
async def test_fully_subsumed_runtime_survives_as_a_zero_pid_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime whose pids are wholly claimed by an earlier entry still appears —
    as a zero-pid row — rather than vanishing."""
    _fake_proc(monkeypatch, tree={7: [7, 8], 10: [7, 8]})
    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7), _FakeRuntime(10)])
    snap = await sampler.snapshot()
    by_pid = {e.pid: e for e in snap.entries if e.kind != "gateway"}
    assert by_pid[10].pids == frozenset()
    assert by_pid[10].proc_count == 0


# ── gateway self-entry ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gateway_entry_is_its_tree_minus_attributed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway entry claims only pids no runtime already did (Req 2.4)."""
    # Runtime 7 owns {7, 8}; the gateway tree is {gw, 7, 8, 99} → gateway keeps
    # only {gw, 99}.
    _fake_proc(monkeypatch, tree={7: [7, 8]}, gateway_tree=[_GW, 7, 8, 99])
    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)])
    snap = await sampler.snapshot()
    gateway = [e for e in snap.entries if e.kind == "gateway"]
    assert len(gateway) == 1
    assert gateway[0].pids == frozenset({_GW, 99})
    assert gateway[0].proc_count == 2


@pytest.mark.asyncio
async def test_gateway_entry_is_omitted_when_fully_attributed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every gateway pid is already attributed to a runtime, no gateway entry
    is emitted."""
    _fake_proc(monkeypatch, tree={7: [7, 8, _GW]}, gateway_tree=[_GW, 7, 8])
    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)])
    snap = await sampler.snapshot()
    assert [e for e in snap.entries if e.kind == "gateway"] == []


@pytest.mark.asyncio
async def test_gateway_entry_alone_when_no_runtimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_proc(monkeypatch, tree={}, gateway_tree=[_GW, 42])
    sampler = rm.ResourceSampler(live_runtimes=lambda: [])
    snap = await sampler.snapshot()
    assert len(snap.entries) == 1
    assert snap.entries[0].kind == "gateway"
    assert snap.entries[0].pids == frozenset({_GW, 42})


@pytest.mark.asyncio
async def test_gateway_rss_counts_only_its_own_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway row's memory is the sum over the pids no runtime claimed, not
    the whole-tree total (which, the gateway being every runtime's parent, would
    be the fleet's memory and would double-count each chat on the gateway row)."""
    _fake_proc(monkeypatch, tree={7: [7, 8]}, gateway_tree=[_GW, 7, 8, 99])
    # Distinct RSS per pid so the aggregate proves WHICH pids were summed.
    rss = {_GW: 10.0, 99: 5.0, 7: 100.0, 8: 200.0}
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: rss[pid])
    # Ticks likewise: the gateway's own pids advance 100 ticks between passes,
    # the runtime's pids advance 100_000 — a whole-tree delta would be ~1000x.
    ticks = {_GW: 0, 99: 0, 7: 0, 8: 0}
    monkeypatch.setattr(rm, "read_pid_stat", lambda proc_root, pid: ("R", 0.0, ticks[pid]))
    clock = {"now": 100.0}
    monkeypatch.setattr(rm.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(rm, "_CLK_TCK", 100)

    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)], interval_s=0.0)
    snap = await sampler.snapshot()
    gateway = next(e for e in snap.entries if e.kind == "gateway")
    assert gateway.pids == frozenset({_GW, 99})
    assert gateway.rss_mb == pytest.approx(15.0)
    runtime = next(e for e in snap.entries if e.kind != "gateway")
    assert runtime.rss_mb == pytest.approx(300.0)

    # Second pass one second later: gateway's own pids burn 100 ticks (= 1 core),
    # the runtime's pids burn 100_000.
    ticks.update({_GW: 50, 99: 50, 7: 50_000, 8: 50_000})
    clock["now"] = 101.0
    snap = await sampler.snapshot()
    gateway = next(e for e in snap.entries if e.kind == "gateway")
    assert gateway.cpu_pct == pytest.approx(100.0)


# ── Property 1: attribution partition ────────────────────────────────────────


@settings(
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(data=st.data())
def test_property_entries_partition_the_attributed_pids(
    monkeypatch: pytest.MonkeyPatch,
    data: st.DataObject,
) -> None:
    """Property 1 (attribution partition): for any forest of overlapping runtime
    trees plus a gateway tree, every pid attributed across all entries appears in
    exactly one entry's pid set — the entries partition the attributed pids.

    Validates Requirements 8.4 (no double counting), 2.3 (a helper descendant is
    part of exactly one runtime's tree), and 2.4 (the gateway claims only the
    unattributed remainder).
    """
    import asyncio

    # Distinct root pids for the runtimes, and a pool of shared descendant pids
    # the trees can overlap on.
    n_roots = data.draw(st.integers(min_value=0, max_value=5), label="n_roots")
    roots = data.draw(
        st.lists(
            st.integers(min_value=1, max_value=50),
            min_size=n_roots,
            max_size=n_roots,
            unique=True,
        ),
        label="roots",
    )
    shared_pool = data.draw(
        st.lists(st.integers(min_value=51, max_value=100), unique=True, max_size=10),
        label="shared_pool",
    )

    tree: dict[int, list[int]] = {}
    for root in roots:
        if shared_pool:
            descendants = data.draw(
                st.lists(st.sampled_from(shared_pool), max_size=6),
                label=f"desc_{root}",
            )
        else:
            descendants = []
        # A tree is its root plus any shared descendants it drew (deduped).
        tree[root] = list(dict.fromkeys([root, *descendants]))

    gateway_tree = data.draw(
        st.lists(st.integers(min_value=1, max_value=100), unique=True, max_size=8),
        label="gateway_tree",
    )
    gateway_tree = list(dict.fromkeys([_GW, *gateway_tree]))

    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(rm.os, "getpid", lambda: _GW)
    full = {**tree, _GW: gateway_tree}
    monkeypatch.setattr(
        rm, "_iter_descendant_pids", lambda pid, max_pids=None: list(full.get(pid, []))
    )
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: 1.0)
    monkeypatch.setattr(rm, "read_pid_stat", lambda proc_root, pid: ("R", 0.0, 0))

    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(r) for r in roots])
    snap = asyncio.run(sampler.snapshot())

    # Every attributed pid appears in exactly one entry.
    all_pids = [pid for entry in snap.entries for pid in entry.pids]
    assert len(all_pids) == len(set(all_pids)), "a pid was attributed to more than one entry"

    # proc_count agrees with the (deduped) pid set on every entry.
    for entry in snap.entries:
        assert entry.proc_count == len(entry.pids)


# ── owner map, not ACP ids, drives chat attribution ──────────────────────────


class _OwnedRuntime(_FakeRuntime):
    """A production-shaped runtime: ``session_keys`` holds opaque ACP session ids
    and ``session_owners`` maps each of them to the Kiro Crew session key that
    owns it (``""`` while a pooled session is unclaimed)."""

    def __init__(self, pid: int, owners: dict[str, str]) -> None:
        super().__init__(pid, session_keys=list(owners))
        self.session_owners = dict(owners)


@pytest.mark.asyncio
async def test_chat_is_resolved_through_session_owners_not_acp_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session_keys`` on a real runtime are ACP ``sessionId``s, which the
    dashboard cannot resolve; the owner map carries the logical key. A resolver
    that only knows logical keys must still classify the runtime as a chat."""
    _fake_proc(monkeypatch, tree={7: [7]})
    rt = _OwnedRuntime(7, {"acp-uuid-1": "dashboard:chat-7"})
    seen: list[str] = []

    def resolver(key: str) -> str | None:
        seen.append(key)
        return "chat-7" if key == "dashboard:chat-7" else None

    snap = await rm.ResourceSampler(live_runtimes=lambda: [rt], slot_resolver=resolver).snapshot()
    assert snap.entries[0].kind == "chat"
    assert snap.entries[0].slot == "chat-7"
    assert snap.entries[0].session_key == "dashboard:chat-7"
    # The ACP id was never offered to the resolver.
    assert "acp-uuid-1" not in seen


@pytest.mark.asyncio
async def test_unclaimed_pool_worker_with_owner_map_is_a_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An owner map whose values are all empty (warm-pool session not yet
    claimed) must NOT fall back to the ACP ids: the runtime is a worker."""
    _fake_proc(monkeypatch, tree={8: [8]})
    rt = _OwnedRuntime(8, {"acp-uuid-2": ""})
    resolver_calls: list[str] = []

    def resolver(key: str) -> str | None:
        resolver_calls.append(key)
        return "would-match"

    snap = await rm.ResourceSampler(live_runtimes=lambda: [rt], slot_resolver=resolver).snapshot()
    assert snap.entries[0].kind == "worker"
    assert resolver_calls == []


# ── provider_pid_for_session (the stop route's staleness check) ──────────────


def test_provider_identity_for_session_reads_the_live_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answer is the (pid, process_instance) of the runtime hosting the
    session NOW, read from the same registry the sampler enumerates -- never from
    a cached snapshot -- and None when no live runtime owns the session (or the
    key is empty). The instance rides along because a pid alone is reusable."""

    class _Owned(_FakeRuntime):
        def __init__(self, pid: int, owners: dict[str, str], instance: str = "") -> None:
            super().__init__(pid)
            self.session_owners = owners
            self.process_instance = instance

    live = [
        _Owned(11, {"acp-1": "sess-A"}, "i-11"),
        _Owned(22, {"acp-2": "sess-B", "acp-3": "sess-C"}, "i-22"),
        _FakeRuntime(33, session_keys=["sess-D"]),  # no owner map / no instance
    ]
    monkeypatch.setattr(rm.runtime_registry, "live_runtimes", lambda: live)

    assert rm.provider_identity_for_session("sess-A") == (11, "i-11")
    assert rm.provider_identity_for_session("sess-C") == (22, "i-22")
    assert rm.provider_identity_for_session("sess-D") == (33, "")
    assert rm.provider_identity_for_session("sess-gone") is None
    assert rm.provider_identity_for_session("") is None

    # The registry moves on (the slot's conversation now runs elsewhere): the
    # answer follows the registry, which is what makes a sampled identity go
    # stale -- including a successor that inherited the SAME pid.
    live[0] = _Owned(11, {"acp-9": "sess-A"}, "i-11-successor")
    assert rm.provider_identity_for_session("sess-A") == (11, "i-11-successor")


@pytest.mark.asyncio
async def test_entries_carry_the_runtime_process_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each row carries the sampled runtime's per-spawn instance id, so the page
    can submit it with a Stop; a runtime without one yields ""."""
    _fake_proc(monkeypatch, tree={7: [7], 8: [8]})
    with_inst = _FakeRuntime(7)
    with_inst.process_instance = "spawn-7"
    without = _FakeRuntime(8)
    snap = await rm.ResourceSampler(live_runtimes=lambda: [with_inst, without]).snapshot()
    by_pid = {e.pid: e for e in snap.entries}
    assert by_pid[7].instance == "spawn-7"
    assert by_pid[8].instance == ""
