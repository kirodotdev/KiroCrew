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

    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == old_policy
    # The read count alone cannot distinguish "not stored" from "stored but
    # missed": the bumped mtime makes the second call miss either way.
    assert str(tmp_path) not in sessions_mod._TOOL_POLICY_MEMO._answers
    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == new_policy
    assert uncached_calls == 2, "the answer read across the edit must not be served again"


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
