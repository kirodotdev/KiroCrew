"""A session above a KAS sub-agent run holds it from the directory it is bound to.

A run started with no ``cwd`` still runs somewhere: the pool's directory, or a
folder of its own. Its path limits are anchored there. A child it starts in another
folder is held to that anchor, not to the child's own folder. The run
path is the real ``SubagentManager`` one, with only the ACP session provider faked:
the directory is the one the run's own provider reports.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from idle_tab_drain_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _idle_tab_drains_head_as_turn,
)
from subagent_stop_reason_helpers import (
    _await_run,
    _manager,
    _mock_sessions,
    _single_turn,
    _wait_writers_landed,
)
from test_kas_parent_limits import (  # noqa: F401 -- the fixtures register by import
    _isolated,
    _judge,
    _pool_decision,
    _shell,
    _stamped,
    _write,
    agents,
    sessions,
)
from test_kas_parent_limits_same_agent import (  # noqa: F401 -- fixtures
    _NOWRITE,
    _anchors,
    _batch_in,
    _write_to,
    dirs,
    runs_in,
    shifty,
)

from kiro_crew.acp import kas_parent_limits
from kiro_crew.acp.types import STOP_REASON_END_TURN
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.hooks import TOOL_DENY
from kiro_crew.subagent_persistence import create_agent_folder

pytestmark = pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")

_PARENT_TIER = "spec-parent-limits"


class _Runs:
    """A real manager. Each run's session provider reports the directory given for it."""

    def __init__(self) -> None:
        stream_factory, _calls = _single_turn(STOP_REASON_END_TURN)
        self.sessions = _mock_sessions(stream_factory)
        self.manager = _manager(self.sessions)
        self.manager._spawn_stagger_secs = 0.0  # a second start is not held for the tick

    async def run(self, parent: str, bound: Path) -> str:
        """Run a sub-agent under *parent*, started with no ``cwd``, whose session is
        bound to *bound*. Its session key."""
        self.sessions._provider.cwd = str(bound)
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = self.manager.spawn("do work", parent_session_key=parent)
            assert info is not None
            assert info.id in self.manager._tasks, (info.error, info.queued_reason)
            await _await_run(self.manager, info)
        await _wait_writers_landed(self.manager, info)
        return f"subagent:{info.id}"


@pytest.fixture
def chain(agents, sessions, dirs, runs_in):  # noqa: F811
    """``chain(bound, child_dir)``: a chat on ``plain`` in ``a`` starts ``nowrite``
    with no ``cwd``, its session bound to *bound*; that run starts a child. With
    *child_dir* the child's record names that directory, as a child started with a
    ``cwd`` has it; with ``None`` the child has none either. The child's session key."""
    _write(agents, _NOWRITE)
    runs = _Runs()

    async def _chain(bound: Path, child_dir: Path | None) -> str:
        sessions["dashboard:chat"] = "plain"
        runs_in["dashboard:chat"] = dirs[0]
        parent = await runs.run("dashboard:chat", bound)
        sessions[parent] = "nowrite"
        child = await runs.run(parent, child_dir or bound)
        if child_dir is not None:
            runs_in[child] = child_dir
        return child

    _chain.runs = runs  # type: ignore[attr-defined]
    _chain.a, _chain.b = dirs  # type: ignore[attr-defined]
    return _chain


# ── the run above has no cwd and starts a child in another folder ────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("child_agent", ["plain", "nowrite"])
async def test_a_run_started_with_no_cwd_holds_its_child_from_where_it_runs(chain, child_agent):
    a, b = chain.a, chain.b
    child = await chain(a, b)
    batch = await _batch_in(child_agent, child, b)

    there = _judge(_stamped(_write_to(b / "out" / "x.txt"), batch, child_agent, cwd=str(b)))
    assert (there.action, there.tier) == (TOOL_DENY, _PARENT_TIER)
    assert "'nowrite'" in there.reason
    # What the run above may write itself is not refused in its name.
    here = _judge(_stamped(_write_to(a / "out" / "x.txt"), batch, child_agent, cwd=str(b)))
    assert here.tier != _PARENT_TIER
    assert _anchors(batch, child_agent) == [("nowrite", str(a))]


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["pooled", "per-run"])
async def test_the_anchor_is_the_directory_the_runs_session_is_bound_to(chain, tmp_path, how):
    """A run with no ``cwd`` takes a pooled session, bound to the pool's directory,
    or starts in a folder of its own. Its provider says which; the pool's directory
    is not assumed."""
    pool, b = chain.a, chain.b
    own = tmp_path / "workspace" / "its-own-folder"
    (own / "out").mkdir(parents=True)
    chain.runs.sessions._pool_cwd = str(pool)
    bound = pool if how == "pooled" else own
    child = await chain(bound, b)
    batch = await _batch_in("plain", child, b)

    assert _anchors(batch, "plain") == [("nowrite", str(bound))]
    there = _judge(_stamped(_write_to(b / "out" / "x.txt"), batch, "plain", cwd=str(b)))
    assert (there.action, there.tier) == (TOOL_DENY, _PARENT_TIER)
    here = _judge(_stamped(_write_to(bound / "out" / "x.txt"), batch, "plain", cwd=str(b)))
    assert here.tier != _PARENT_TIER


@pytest.mark.asyncio
async def test_a_run_two_levels_above_holds_from_where_it_runs(
    agents, sessions, dirs, runs_in  # noqa: F811
):
    """``nowrite`` with no ``cwd`` starts ``plain`` with no ``cwd``, which starts a
    child in ``b``: the directory of the run two levels up was noted when the run
    between them started."""
    a, b = dirs
    _write(agents, _NOWRITE)
    runs = _Runs()
    sessions["dashboard:chat"] = "plain"
    top = await runs.run("dashboard:chat", a)
    sessions[top] = "nowrite"
    middle = await runs.run(top, a)
    sessions[middle] = "plain"
    child = await runs.run(middle, b)
    runs_in[child] = b
    batch = await _batch_in("plain", child, b)

    there = _judge(_stamped(_write_to(b / "out" / "x.txt"), batch, "plain", cwd=str(b)))
    assert (there.action, there.tier) == (TOOL_DENY, _PARENT_TIER)
    assert _anchors(batch, "plain") == [("nowrite", str(a))]


@pytest.mark.asyncio
async def test_a_limit_only_the_read_with_no_directory_finds_is_still_carried(
    agents, sessions, dirs, runs_in, shifty  # noqa: F811
):
    """The run above is bound to ``b``, where the folder declares a plain ``shifty``;
    read with no directory, ``shifty`` is the limited file in the Bot folder. Both
    are read, so the child keeps the limit it is held to with no directory noted."""
    a, b = dirs
    runs = _Runs()
    sessions["dashboard:chat"] = "plain"
    above = await runs.run("dashboard:chat", b)
    sessions[above] = "shifty"
    child = await runs.run(above, a)
    runs_in[child] = a
    batch = await _batch_in("plain", child, a)

    assert _anchors(batch, "plain") == [("shifty", "")]
    verdict = _judge(_stamped(_shell("touch marker"), batch, "plain", cwd=str(a)))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, _PARENT_TIER)


# ── one folder: the batch a run gets today ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_child_on_the_same_agent_in_the_same_folder_is_unchanged(chain, monkeypatch):
    a = chain.a
    child = await chain(a, a)
    with monkeypatch.context() as no_parent:
        no_parent.setattr(kas_parent_limits, "_parent_keys", lambda _key: ())
        alone = await _batch_in("nowrite", child, a)
    assert await _batch_in("nowrite", child, a) == alone


@pytest.mark.asyncio
async def test_a_child_on_another_agent_in_the_same_folder_is_unchanged(chain):
    """The limit of the run above is carried with no directory, so it is anchored at
    the child's, which is the same folder."""
    a = chain.a
    child = await chain(a, a)
    batch = await _batch_in("plain", child, a)

    assert _anchors(batch, "plain") == [("nowrite", "")]
    inside = _judge(_stamped(_write_to(a / "out" / "x.txt"), batch, "plain", cwd=str(a)))
    assert inside.tier != _PARENT_TIER
    outside = _judge(_stamped(_write_to(a / "elsewhere.txt"), batch, "plain", cwd=str(a)))
    assert (outside.action, outside.tier) == (TOOL_DENY, _PARENT_TIER)


@pytest.mark.asyncio
async def test_a_directory_the_resolver_knows_is_the_one_used(chain, dirs, runs_in):  # noqa: F811
    """One session has one directory to the walk: where the resolver answers, what
    the run's provider reported is not read, so the two never disagree."""
    a, b = dirs
    child = await chain(a, b)
    above = chain.runs.manager._agents[child.removeprefix("subagent:")].parent_session_key
    runs_in[above] = b
    batch = await _batch_in("plain", child, b)

    assert _anchors(batch, "plain") == [("nowrite", str(b))]
    there = _judge(_stamped(_write_to(b / "out" / "x.txt"), batch, "plain", cwd=str(b)))
    assert there.tier != _PARENT_TIER


# ── a session above that left no directory ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_session_above_with_no_known_directory_is_anchored_at_the_runs(
    agents, sessions, dirs, runs_in  # noqa: F811
):
    """After a restart only the run records are left, and they are not trusted for a
    directory. The run above is then anchored at the child's folder, and the child
    is started."""
    a, b = dirs
    _write(agents, _NOWRITE)
    sessions.update({"dashboard:chat": "plain", "subagent:p1": "nowrite"})
    runs_in.update({"dashboard:chat": a, "subagent:c2": b})
    create_agent_folder("p1", task="t", agent="nowrite", parent_session="dashboard:chat")
    create_agent_folder("c2", task="t", agent="plain", parent_session="subagent:p1")
    batch = await _batch_in("plain", "subagent:c2", b)

    assert _anchors(batch, "plain") == [("nowrite", "")]
    there = _judge(_stamped(_write_to(b / "out" / "x.txt"), batch, "plain", cwd=str(b)))
    assert there.tier != _PARENT_TIER


# ── the warm pool ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("above_in_pool_dir", "decision"),
    [(False, "bypass_parent_denies"), (True, "miss_empty")],
    ids=["another-folder", "the-pool-folder"],
)
async def test_the_pool_decision_reads_where_the_run_above_is_bound(
    chain, above_in_pool_dir, decision
):
    """A pooled session is bound to the pool's directory. A child on the agent of the
    run above takes one only when that run is bound there too."""
    pool, elsewhere = chain.a, chain.b
    child = await chain(pool if above_in_pool_dir else elsewhere, None)
    assert await _pool_decision(child, "nowrite", ACP_BACKEND_KAS, str(pool)) == [decision]
