"""Chat runtime sharing inside one session tree (Ultra Mode trees and Bot trees).

Each rule below has a test that reddens without it:

* On KAS a lead, a conductor and workers with different agents, specs, models and
  efforts land on ONE process: KAS takes all of those per session.
* Two trees never share, whatever else matches: the tree root is a key field.
* A chat with no creator is its own tree; so is one whose lineage cannot be read.
* A tree past the cap spills to a second process of the SAME tree.
* A Bot's sessions share only under that Bot: a member whose root is another Bot,
  or no Bot, is refused; two Bots' keys never match.
* With the preview off nothing is read and nothing is placed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from chat_runtime_sharing_helpers import _PlacementProvider, confirming, make_placement

from kiro_crew import crew_mode
from kiro_crew import runtime_ownership as ro
from kiro_crew.acp import chat_runtime_sharing as sharing
from kiro_crew.acp.chat_runtime_sharing import ChatRuntimeKey, share_tree_root_of
from kiro_crew.acp_backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.providers import chat_share
from kiro_crew.providers.chat_share import resolve_chat_share
from kiro_crew.runtime_ownership import RuntimeOwnership

LEAD = "chat-1-1790000000"
CONDUCTOR = "chat-2-1790000000"
WORKER_A = "chat-3-1790000000"
WORKER_B = "chat-4-1790000000"
OTHER_LEAD = "chat-9-1790000000"


class _Runtime:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def is_alive(self) -> bool:
        return True


def _spawner():
    made: list[_Runtime] = []

    async def spawn() -> _Runtime:
        rt = _Runtime(9000 + len(made))
        made.append(rt)
        return rt

    return spawn, made


def _kas_placement(session_key: str, tree_root: str, **overrides):
    """A KAS placement of *session_key* in *tree_root*'s tree, confirmed as read."""
    args = dict(session_key=session_key, tree_root=tree_root)
    args.update(overrides)
    p = make_placement(provider=_PlacementProvider(backend=ACP_BACKEND_KAS), **args)
    p._read_confirmation = confirming(
        (p._spec_generation, "acct-A", "auto", False, p._kas_telemetry)
    )
    return p


def _place_all(placements, cap: int = 10) -> list[_Runtime]:
    spawn, made = _spawner()

    async def scenario():
        chat_share.RUNTIME_OWNERSHIP = RuntimeOwnership()
        try:
            return [await p.place(spawn) for p in placements]
        finally:
            chat_share.RUNTIME_OWNERSHIP = ro.RUNTIME_OWNERSHIP

    for p in placements:
        p.cap = cap
    placed = asyncio.run(scenario())
    assert len({id(rt) for rt in placed}) == len(made)
    return placed


# -- one tree, one KAS process ---------------------------------------------------


def test_a_lead_conductor_and_workers_with_different_agents_land_on_one_kas_process():
    tree = [
        _kas_placement(
            LEAD,
            LEAD,
            agent="kirocrew-team-lead",
            configured_model="claude-opus-5.5",
            spec_generation="lead-spec",
        ),
        _kas_placement(
            CONDUCTOR, LEAD, agent="kirocrew-conductor", configured_model="", spec_generation="c"
        ),
        _kas_placement(
            WORKER_A,
            LEAD,
            agent="kirocrew-worker",
            configured_model="claude-opus-5",
            spec_generation="w",
        ),
        _kas_placement(
            WORKER_B,
            LEAD,
            agent="kirocrew-worker",
            configured_model="claude-sonnet",
            spec_generation="w2",
        ),
    ]
    placed = _place_all(tree)
    assert len({rt.pid for rt in placed}) == 1
    assert [p.joined for p in tree] == [False, True, True, True]
    assert tree[-1].leases_on_runtime == 4


@pytest.mark.parametrize("field", sorted(sharing.KAS_PER_SESSION_FIELDS))
def test_kas_leaves_each_per_session_field_out_of_the_key(field):
    base = dict(
        work_dir="/w",
        agent="a",
        sandbox_mode="auto",
        extra_env={},
        acp_backend=ACP_BACKEND_KAS,
        tool_search=None,
        memory_mode="persistent",
        shared_scratch=None,
        mcp_gateway_overlay=None,
        mcp_gateway_socket=None,
        tree_root=LEAD,
    )
    other = {field: "something-else"}
    assert ChatRuntimeKey.build(**base) == ChatRuntimeKey.build(**{**base, **other})
    # kiro-cli reads them once at startup, so a kiro key still splits on each.
    kiro = {**base, "acp_backend": ACP_BACKEND_KIRO}
    assert ChatRuntimeKey.build(**kiro) != ChatRuntimeKey.build(**{**kiro, **other})


def test_only_listed_harnesses_take_fields_per_session():
    """The one table a harness joins tree sharing through; KAS is the only entry."""
    assert set(sharing.PER_SESSION_FIELDS_BY_BACKEND) == {ACP_BACKEND_KAS}
    assert sharing.per_session_fields(ACP_BACKEND_KAS) == sharing.KAS_PER_SESSION_FIELDS
    assert sharing.per_session_fields(ACP_BACKEND_KIRO) == frozenset()


def test_kas_still_splits_on_the_work_directory():
    """The spawn mounts the founder's work directory, so it stays a process input."""
    tree = [
        _kas_placement(LEAD, LEAD, work_dir="/w/one"),
        _kas_placement(WORKER_A, LEAD, work_dir="/w/two"),
    ]
    assert len({rt.pid for rt in _place_all(tree)}) == 2


# -- trees never share -----------------------------------------------------------


def test_two_trees_never_share_a_process():
    placed = _place_all(
        [
            _kas_placement(LEAD, LEAD),
            _kas_placement(WORKER_A, LEAD),
            _kas_placement(OTHER_LEAD, OTHER_LEAD),
            _kas_placement(WORKER_B, OTHER_LEAD),
        ]
    )
    assert placed[0] is placed[1]
    assert placed[2] is placed[3]
    assert placed[0] is not placed[2]


def test_a_tree_past_the_cap_spills_to_a_second_process_of_the_same_tree():
    placed = _place_all(
        [
            _kas_placement(LEAD, LEAD),
            _kas_placement(CONDUCTOR, LEAD),
            _kas_placement(WORKER_A, LEAD),
            _kas_placement(OTHER_LEAD, OTHER_LEAD),
            _kas_placement(WORKER_B, LEAD),
        ],
        cap=2,
    )
    lead_tree = {placed[0].pid, placed[1].pid, placed[2].pid, placed[4].pid}
    assert placed[0] is placed[1]
    assert placed[2] is placed[4]  # the spill process takes the tree's next session
    assert len(lead_tree) == 2
    # The other tree never lands on the lead tree's spill process, though it had room.
    assert placed[3].pid not in lead_tree


def test_a_placement_that_gave_up_takes_a_process_nothing_else_matches():
    """A KAS key leaves the spec out, so the give-up must also make the root unique."""
    spawn, made = _spawner()
    first = _kas_placement(LEAD, LEAD)
    moving = _kas_placement(WORKER_A, LEAD)
    moving._read_confirmation = confirming(
        *[("", f"acct-{n}", "auto", False, True) for n in range(2, 9)]
    )

    async def scenario():
        chat_share.RUNTIME_OWNERSHIP = RuntimeOwnership()
        try:
            a = await first.place(spawn)
            b = await moving.place(spawn)
            return a, b
        finally:
            chat_share.RUNTIME_OWNERSHIP = ro.RUNTIME_OWNERSHIP

    a, b = asyncio.run(scenario())
    assert a is not b
    assert moving.joinable is False
    assert moving.key.tree_root.startswith("unsettled-")


# -- the tree root ---------------------------------------------------------------


def _slot(key: str, created_by: str = "") -> SimpleNamespace:
    return SimpleNamespace(key=key, _created_by=created_by, _saved_created_by="", agent="x")


def test_a_grandchild_roots_at_the_top_of_its_creator_chain():
    slots = {
        LEAD: _slot(LEAD),
        CONDUCTOR: _slot(CONDUCTOR, LEAD),
        WORKER_A: _slot(WORKER_A, CONDUCTOR),
    }
    assert crew_mode.share_tree_root(slots, WORKER_A) == LEAD
    assert crew_mode.share_tree_root(slots, CONDUCTOR) == LEAD


def test_a_parentless_chat_is_its_own_tree():
    slots = {LEAD: _slot(LEAD), OTHER_LEAD: _slot(OTHER_LEAD)}
    assert crew_mode.share_tree_root(slots, OTHER_LEAD) == OTHER_LEAD


def test_a_closed_creator_is_followed_through_the_persisted_lineage():
    slots = {WORKER_A: _slot(WORKER_A, CONDUCTOR)}
    with patch.object(crew_mode, "persisted_parents", return_value={CONDUCTOR: LEAD}):
        assert crew_mode.share_tree_root(slots, WORKER_A) == LEAD


def test_a_cycle_in_the_records_stops_the_walk():
    slots = {WORKER_A: _slot(WORKER_A, WORKER_B), WORKER_B: _slot(WORKER_B, WORKER_A)}
    assert crew_mode.share_tree_root(slots, WORKER_A) in {WORKER_A, WORKER_B}


@pytest.mark.parametrize(
    "reader", [None, lambda key: "", lambda key: (_ for _ in ()).throw(RuntimeError("x"))]
)
def test_an_unreadable_lineage_is_a_tree_of_one(reader):
    sharing.set_share_tree_reader(reader)
    try:
        assert share_tree_root_of(WORKER_A) == WORKER_A
    finally:
        sharing.set_share_tree_reader(None)


# -- resolve: Bot trees and the preview ------------------------------------------


def _cfg(on: bool):
    return SimpleNamespace(
        dashboard=SimpleNamespace(feature_previews={"chat_runtime_sharing": on}),
        agent=SimpleNamespace(chat_runtime_sharing_max_sessions=10),
    )


class _Provider(_PlacementProvider):
    def __init__(self, key: str, member: bool) -> None:
        super().__init__(backend=ACP_BACKEND_KAS)
        self._key = key
        self.member_context = member

    def _owning_session_key(self) -> str:
        return self._key


def _resolve(provider, *, on=True, roots=None, scopes=None):
    roots = roots or {}
    scopes = scopes or {}

    async def scenario():
        sharing.set_share_tree_reader(lambda key: roots.get(key, key))
        try:
            with (
                patch("kiro_crew.config.live.snapshot", return_value=_cfg(on)),
                patch.object(chat_share, "kas_chat_sharing_ready", return_value=True),
                patch.object(chat_share, "agent_spawns_native_subagents", return_value=False),
                patch.object(
                    chat_share, "member_scope_of", side_effect=lambda k: scopes.get(k, ("", ""))
                ),
            ):
                return await resolve_chat_share(
                    provider,
                    work_dir="/w",
                    agent="kirocrew-worker",
                    sandbox_mode="auto",
                    extra_env={},
                    mcp_gateway_overlay=None,
                    mcp_gateway_socket=None,
                    configured_model="",
                    launch_agent="",
                    remote_workspace=False,
                )
        finally:
            sharing.set_share_tree_reader(None)

    return asyncio.run(scenario())


BOT = ("kirocrew-team-lead", "bot-store")
OTHER_BOT = ("fund-manager", "fund-store")


def test_a_bot_worker_shares_under_its_own_bot():
    bot = _resolve(_Provider(LEAD, member=True), scopes={LEAD: BOT})
    worker = _resolve(
        _Provider(WORKER_A, member=True),
        roots={WORKER_A: LEAD},
        scopes={LEAD: BOT, WORKER_A: BOT},
    )
    assert bot is not None and worker is not None
    assert bot.key == worker.key
    assert (worker.key.member_id, worker.key.memory_store) == BOT
    assert worker.tree_kind == "bot"


@pytest.mark.parametrize(
    "root_scope",
    [OTHER_BOT, ("kirocrew-team-lead", "another-store"), ("", "")],
    ids=["another_bot", "another_store", "no_bot"],
)
def test_a_bot_session_under_anything_but_its_own_bot_is_refused(root_scope):
    placement = _resolve(
        _Provider(WORKER_A, member=True),
        roots={WORKER_A: LEAD},
        scopes={LEAD: root_scope, WORKER_A: BOT},
    )
    assert placement is None


def test_two_bots_never_share_even_when_a_lineage_names_one_root():
    """Even a forged edge cannot put one Bot's session on another's process."""
    a = make_placement(
        provider=_PlacementProvider(backend=ACP_BACKEND_KAS),
        session_key=WORKER_A,
        tree_root=LEAD,
        member_scope=BOT,
    )
    b = make_placement(
        provider=_PlacementProvider(backend=ACP_BACKEND_KAS),
        session_key=WORKER_B,
        tree_root=LEAD,
        member_scope=OTHER_BOT,
    )
    assert a.key != b.key


def test_an_ultra_mode_worker_is_placed_in_its_lead_s_tree():
    worker = _resolve(_Provider(WORKER_A, member=False), roots={WORKER_A: LEAD})
    assert worker is not None
    assert worker.key.tree_root == LEAD
    assert worker.tree_kind == "chat"


def test_the_preview_off_reads_no_tree_and_places_nothing():
    asked: list[str] = []

    async def scenario():
        sharing.set_share_tree_reader(lambda key: asked.append(key) or LEAD)
        try:
            with patch("kiro_crew.config.live.snapshot", return_value=_cfg(False)):
                return await resolve_chat_share(
                    _Provider(WORKER_A, member=False),
                    work_dir="/w",
                    agent="kirocrew-worker",
                    sandbox_mode="auto",
                    extra_env={},
                    mcp_gateway_overlay=None,
                    mcp_gateway_socket=None,
                    configured_model="",
                    launch_agent="",
                    remote_workspace=False,
                )
        finally:
            sharing.set_share_tree_reader(None)

    assert asyncio.run(scenario()) is None
    assert asked == []
