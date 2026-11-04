"""``/api/session-tool-policy`` answers an unchanged agents directory without re-reading it.

The policy read resolves an agent by parsing every spec in the agents directory
and comparing declared names, then -- when nothing matched -- parses every spec
a second time, strictly, to prove that "no match" is honest. Both passes run the
hardened reader per file: symlink resolution, the sensitive-path fence, a
descriptor-pinned open, a JSON parse. With a couple of thousand installed specs
that is most of a second of GIL-holding work on a worker thread for every
request, and a managed MCP server makes the request on ordinary traffic.

These tests pin the policy semantics of the memo that answers the second request
from one ``scandir``: an unchanged directory performs no spec read at all, an
in-place edit or a new file makes the next call read again, and refusals are
deliberately not memoized. The revision the memo is pinned to, and the memo's
store rules, are :mod:`kiro_crew.agent_discovery`'s and are tested in
``test_agents_dir_memo.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew import agent_discovery
from kiro_crew.dashboard.handlers import sessions as sessions_mod

AGENT = "reviewer"


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions_mod, "_TOOL_POLICY_MEMO", agent_discovery.AgentsDirMemo())
    monkeypatch.setattr(agent_discovery, "AGENTS_DIR_MEMO_ENABLED", True)
    monkeypatch.setattr(agent_discovery, "_AGENTS_DIR_RACY_WINDOW_NS", 0)


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every spec file the hardened readers open, both passes included."""
    reads: list[Path] = []
    real_read = agent_discovery._read_spec_bytes

    def recording_read(real: Path) -> bytes:
        reads.append(real)
        return real_read(real)

    monkeypatch.setattr(agent_discovery, "_read_spec_bytes", recording_read)
    return reads


def _write_spec(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _bump_mtime(path: Path, seconds: int) -> None:
    """Move *path*'s mtime by a whole number of seconds, so an edit is never
    lost inside one filesystem timestamp tick."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


def _populate(agents_dir: Path, others: int = 5) -> None:
    for i in range(others):
        _write_spec(agents_dir / f"other-{i}.json", {"name": f"other-{i}"})
    _write_spec(
        agents_dir / f"SomePackage-{AGENT}.json",
        {"name": AGENT, "managedToolPolicy": {"exclude": ["shell"]}},
    )


def test_an_unchanged_directory_is_answered_without_reading_any_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _populate(tmp_path)
    reads = _count_reads(monkeypatch)

    first = sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT)
    assert first == {"exclude": ["shell"]}
    assert reads, "the first call must read the directory"

    reads.clear()
    second = sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT)
    assert second == {"exclude": ["shell"]}
    assert reads == [], (
        "the agents directory did not change between the two calls, yet the "
        "second call re-read specs: every request pays the full scan"
    )


def test_an_in_place_edit_is_seen_and_the_new_policy_is_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _populate(tmp_path)
    reads = _count_reads(monkeypatch)
    spec = tmp_path / f"SomePackage-{AGENT}.json"

    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == {"exclude": ["shell"]}

    _write_spec(spec, {"name": AGENT, "managedToolPolicy": {"exclude": ["browser"]}})
    _bump_mtime(spec, 5)
    reads.clear()

    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == {"exclude": ["browser"]}
    assert reads, "an edited spec must be read again"


def test_an_identical_twin_edited_to_differ_turns_the_memoized_policy_into_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two identical specs declaring the agent resolve to one policy, which the
    memo keeps; an edit that makes them differ changes the directory revision,
    so the next read scans again and refuses, and the stale policy is gone."""
    spec = {"name": AGENT, "managedToolPolicy": {"exclude": ["shell"]}}
    _write_spec(tmp_path / f"Alpha-{AGENT}.json", spec)
    beta = tmp_path / f"Beta-{AGENT}.json"
    _write_spec(beta, spec)
    reads = _count_reads(monkeypatch)

    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == {"exclude": ["shell"]}
    reads.clear()
    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == {"exclude": ["shell"]}
    assert reads == [], "an unchanged pair is served from the memo"

    _write_spec(beta, {"name": AGENT, "managedToolPolicy": {"exclude": ["browser"]}})
    _bump_mtime(beta, 5)

    with pytest.raises(agent_discovery.AmbiguousAgentSpecError):
        sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT)
    assert reads, "the edited directory must be scanned again"


def test_a_no_policy_answer_is_cached_until_a_spec_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``None`` is a resolved answer and is cached; a file added afterwards
    changes the directory and the agent it declares is found."""
    _write_spec(tmp_path / "other.json", {"name": "other"})
    reads = _count_reads(monkeypatch)

    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) is None
    reads.clear()
    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) is None
    assert reads == []

    _write_spec(
        tmp_path / f"Pkg-{AGENT}.json",
        {"name": AGENT, "managedToolPolicy": {"exclude": ["shell"]}},
    )
    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == {"exclude": ["shell"]}


def test_a_refusal_is_re_derived_on_every_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable spec refuses on each call and each call reads: the
    refusal is the operator's signal and must track the directory exactly."""
    _write_spec(tmp_path / "other.json", {"name": "other"})
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    reads = _count_reads(monkeypatch)

    with pytest.raises(sessions_mod.ManagedToolPolicyUnreadable):
        sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT)
    assert reads
    reads.clear()
    with pytest.raises(sessions_mod.ManagedToolPolicyUnreadable):
        sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT)
    assert reads, "a refusal must not be served from the cache"


def test_the_cache_is_per_agent_and_per_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    _write_spec(dir_a / f"{AGENT}.json", {"managedToolPolicy": {"exclude": ["shell"]}})
    _write_spec(dir_a / "second.json", {"managedToolPolicy": {"exclude": ["browser"]}})
    _write_spec(dir_b / f"{AGENT}.json", {"managedToolPolicy": {"exclude": ["cron"]}})

    assert sessions_mod._read_managed_tool_policy_sync(dir_a, AGENT) == {"exclude": ["shell"]}
    assert sessions_mod._read_managed_tool_policy_sync(dir_a, "second") == {"exclude": ["browser"]}
    assert sessions_mod._read_managed_tool_policy_sync(dir_b, AGENT) == {"exclude": ["cron"]}
    assert sessions_mod._read_managed_tool_policy_sync(dir_a, AGENT) == {"exclude": ["shell"]}


def test_an_edit_during_the_read_is_not_memoized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = tmp_path / f"{AGENT}.json"
    old_policy = {"exclude": ["shell"]}
    new_policy = {"exclude": ["browser"]}
    _write_spec(spec, {"name": AGENT, "managedToolPolicy": old_policy})
    real_read = sessions_mod._read_managed_tool_policy_uncached
    uncached_calls = 0

    def read_while_editing(agents_dir: Path, agent_name: str) -> dict[str, Any] | None:
        nonlocal uncached_calls
        uncached_calls += 1
        policy = real_read(agents_dir, agent_name)
        if uncached_calls == 1:
            _write_spec(spec, {"name": AGENT, "managedToolPolicy": new_policy})
            _bump_mtime(spec, 5)
        return policy

    monkeypatch.setattr(sessions_mod, "_read_managed_tool_policy_uncached", read_while_editing)
    # The one-name read is what this pins; the every-name walk has its own test.
    monkeypatch.setattr(sessions_mod, "_prefill_managed_tool_policies", lambda agents_dir, **_: {})

    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == old_policy
    # The read count alone cannot distinguish "not stored" from "stored but
    # missed": the bumped mtime makes the second call miss either way.
    stored = sessions_mod._TOOL_POLICY_MEMO._answers.get(str(tmp_path), (b"", {}, False))[1]
    assert AGENT not in stored
    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == new_policy
    assert uncached_calls == 2, "the answer read across the edit must not be served again"


def test_an_alias_named_read_sees_the_alias_appear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The revision leaves skill-view aliases out, so an alias write moves
    nothing it pins to; a policy read BY an alias name reads ``<alias>.json``
    by filename and is therefore never served from the memo."""
    from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX

    alias = f"{NATIVE_SKILL_ALIAS_PREFIX}0123456789abcdef01234567"
    _populate(tmp_path)

    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, alias) is None
    _write_spec(tmp_path / f"{alias}.json", {"managedToolPolicy": {"exclude": ["shell"]}})

    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, alias) == {"exclude": ["shell"]}
    assert alias not in sessions_mod._TOOL_POLICY_MEMO._answers.get(str(tmp_path), (b"", {}))[1]


def test_the_memo_is_disabled_when_the_platform_cannot_prove_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_discovery, "AGENTS_DIR_MEMO_ENABLED", False)
    _write_spec(
        tmp_path / f"{AGENT}.json",
        {"name": AGENT, "managedToolPolicy": {"exclude": ["shell"]}},
    )
    reads = _count_reads(monkeypatch)

    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == {"exclude": ["shell"]}
    first_read_count = len(reads)
    assert first_read_count > 0
    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == {"exclude": ["shell"]}
    assert len(reads) > first_read_count


def test_the_policy_memo_is_separate_from_the_projection_memo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two ``spec_by_declared_name`` callers carry different SEL
    ``operation`` labels, so an answer read under one label is never handed
    to the other surface."""
    from kiro_crew.acp import kas_agents

    _populate(tmp_path)
    monkeypatch.setattr(kas_agents, "_SPEC_SCAN_MEMO", agent_discovery.AgentsDirMemo())
    reads = _count_reads(monkeypatch)

    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == {"exclude": ["shell"]}
    reads.clear()
    assert kas_agents.load_agent_spec(tmp_path, AGENT)["name"] == AGENT
    assert reads, "the projection must read under its own label, not reuse the policy read"


@pytest.mark.asyncio
async def test_the_route_serves_a_repeat_request_without_spec_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the handler: the second request for the same agent
    against an unchanged directory opens no spec file."""
    _populate(tmp_path)
    monkeypatch.setattr(sessions_mod, "kiro_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(sessions_mod, "_sel", lambda: MagicMock())
    state = MagicMock()
    slot = MagicMock()
    slot.agent = AGENT
    state.get_slot = MagicMock(return_value=slot)
    state.sessions = None
    request = MagicMock()
    request.headers = {"X-Session-Key": f"dashboard:{AGENT}-slot"}
    request.app = {"state": state}
    reads = _count_reads(monkeypatch)

    first = await sessions_mod.api_session_tool_policy(request)
    assert json.loads(first.body.decode("utf-8")) == {"exclude": ["shell"]}
    reads.clear()
    second = await sessions_mod.api_session_tool_policy(request)
    assert json.loads(second.body.decode("utf-8")) == {"exclude": ["shell"]}
    assert reads == []


def test_one_walk_serves_the_first_read_of_every_other_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each managed server asks for its own agent's policy. The first read under
    a revision answers every name exactly one spec declares, so the next
    agent's first read opens no spec file, and its answer is the one the
    one-name read gives."""
    _populate(tmp_path)
    _write_spec(
        tmp_path / "Pkg-writer.json",
        {"name": "writer", "managedToolPolicy": {"exclude": ["cron"]}},
    )
    expected = sessions_mod._read_managed_tool_policy_uncached(tmp_path, "writer")
    reads = _count_reads(monkeypatch)

    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == {"exclude": ["shell"]}
    assert reads, "the first read must walk the directory"
    reads.clear()

    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, "writer") == expected
    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, "other-0") == {}
    assert reads == [], "another agent's first read walked the directory again"


def test_the_walk_leaves_twins_refusals_and_undeclared_names_to_the_one_name_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a name exactly one spec declares, with a policy that reads, is
    answered by the walk; every other name is read on its own each time it
    misses, so a refusal is still re-derived and a twin is still compared."""
    twin = {"name": "twin", "managedToolPolicy": {"exclude": ["shell"]}}
    _write_spec(tmp_path / "Alpha-twin.json", twin)
    _write_spec(tmp_path / "Beta-twin.json", twin)
    _write_spec(tmp_path / "bad.json", {"name": "bad", "managedToolPolicy": ["shell"]})
    _write_spec(tmp_path / "plain.json", {"name": "plain"})

    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, "plain") == {}
    stored = sessions_mod._TOOL_POLICY_MEMO._answers[str(tmp_path)][1]
    assert set(stored) == {"plain"}

    reads = _count_reads(monkeypatch)
    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, "twin") == {"exclude": ["shell"]}
    assert reads, "a twin is resolved by the one-name read"
    for _ in range(2):
        reads.clear()
        with pytest.raises(sessions_mod.ManagedToolPolicyUnreadable):
            sessions_mod._read_managed_tool_policy_sync(tmp_path, "bad")
        assert reads, "a refusal must not be served from the walk"


def test_a_walk_read_across_an_edit_is_not_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The every-name walk follows the store rule of every answer: a write that
    lands during it leaves nothing from it in the memo."""
    _populate(tmp_path)
    writer = tmp_path / "Pkg-writer.json"
    _write_spec(writer, {"name": "writer", "managedToolPolicy": {"exclude": ["cron"]}})
    real_walk = sessions_mod._prefill_managed_tool_policies
    walks = 0

    def walk_while_editing(agents_dir: Path, **kwargs: Any) -> dict[str, Any]:
        nonlocal walks
        walks += 1
        answers = real_walk(agents_dir, **kwargs)
        if walks == 1:
            _write_spec(writer, {"name": "writer", "managedToolPolicy": {"exclude": ["browser"]}})
            _bump_mtime(writer, 5)
        return answers

    monkeypatch.setattr(sessions_mod, "_prefill_managed_tool_policies", walk_while_editing)

    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == {"exclude": ["shell"]}
    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, "writer") == {
        "exclude": ["browser"]
    }
    assert walks == 2, "the walk read across the edit was served"


def test_the_walk_keeps_no_more_policies_than_the_memo_can_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The every-name walk is bounded while it collects, not after: past the
    memo's key bound a name's policy is never read by the walk, the requested
    name always keeps its place wherever the walk meets it, and a name the
    bound left out is answered by the one-name read."""
    from kiro_crew.dashboard.handlers.sessions_parts import tool_policy

    monkeypatch.setattr(tool_policy, "_AGENTS_DIR_MEMO_MAX_KEYS", 4)
    _populate(tmp_path, others=10)
    real_policy_of_spec = tool_policy._policy_of_spec
    policy_reads: list[str] = []

    def counting_policy_of_spec(spec: Any, name: str, *args: Any) -> Any:
        policy_reads.append(name)
        return real_policy_of_spec(spec, name, *args)

    monkeypatch.setattr(tool_policy, "_policy_of_spec", counting_policy_of_spec)
    for requested in (AGENT, "other-0", "other-9"):
        policy_reads.clear()
        answers = tool_policy._prefill_managed_tool_policies(tmp_path, requested=requested)
        # Bounded while collecting: no policy past the bound was even read.
        assert len(policy_reads) <= 3, policy_reads
        assert len(answers) <= 3
        assert requested in answers
    policy_reads.clear()
    assert len(tool_policy._prefill_managed_tool_policies(tmp_path)) <= 3
    assert len(policy_reads) <= 3, policy_reads
    kept = tool_policy._prefill_managed_tool_policies(tmp_path, requested=AGENT)
    assert kept[AGENT] == {"exclude": ["shell"]}
    monkeypatch.setattr(tool_policy, "_policy_of_spec", real_policy_of_spec)

    # The first read prefills the memo; a name the bound left out then misses
    # it, and the one-name read answers it.
    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == {"exclude": ["shell"]}
    left_out = sorted({f"other-{i}" for i in range(10)} - set(kept))
    assert left_out
    real_uncached = sessions_mod._read_managed_tool_policy_uncached
    uncached_names: list[str] = []

    def recording_uncached(agents_dir: Path, agent_name: str) -> Any:
        uncached_names.append(agent_name)
        return real_uncached(agents_dir, agent_name)

    monkeypatch.setattr(sessions_mod, "_read_managed_tool_policy_uncached", recording_uncached)
    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, left_out[0]) == {}
    assert uncached_names == [left_out[0]]
