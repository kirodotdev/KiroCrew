"""``/api/session-tool-policy`` answers an unchanged agents directory without re-reading it.

The policy read resolves an agent by parsing every spec in the agents directory
and comparing declared names, then -- when nothing matched -- parses every spec
a second time, strictly, to prove that "no match" is honest. Both passes run the
hardened reader per file: symlink resolution, the sensitive-path fence, a
descriptor-pinned open, a JSON parse. With a couple of thousand installed specs
that is most of a second of GIL-holding work on a worker thread for every
request, and a managed MCP server makes the request on ordinary traffic.

These tests pin the cache that answers the second request from one ``scandir``:
an unchanged directory performs no spec read at all, and every kind of change
the fingerprint claims to see -- an in-place edit, a new file, a permission
change -- makes the next call read again. Refusals are deliberately not cached.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew import agent_discovery
from kiro_crew.dashboard.handlers import sessions as sessions_mod

AGENT = "reviewer"


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions_mod, "_TOOL_POLICY_CACHE", {}, raising=False)
    monkeypatch.setattr(sessions_mod, "_TOOL_POLICY_MEMO_ENABLED", True)
    monkeypatch.setattr(sessions_mod, "_TOOL_POLICY_RACY_WINDOW_NS", 0)


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


def test_the_revision_sees_content_and_permission_changes(tmp_path: Path) -> None:
    spec = tmp_path / f"{AGENT}.json"
    _write_spec(spec, {"managedToolPolicy": {}})
    before = sessions_mod._agents_dir_revision(tmp_path)

    _write_spec(spec, {"managedToolPolicy": {}, "description": "changed"})
    after_content = sessions_mod._agents_dir_revision(tmp_path)
    assert after_content != before

    if os.name != "nt":
        # POSIX exposes permission bits through st_mode. Keep this assertion in
        # the cross-platform test rather than skipping the whole ratchet.
        time.sleep(0.05)
        spec.chmod(stat.S_IRUSR)
        after_mode = sessions_mod._agents_dir_revision(tmp_path)
        assert after_mode != after_content
        assert after_mode[1][0][6] != after_content[1][0][6]


def test_a_fresh_spec_is_not_memoized_until_the_racy_window_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions_mod, "_TOOL_POLICY_RACY_WINDOW_NS", 2_000_000_000)
    _write_spec(tmp_path / f"{AGENT}.json", {"managedToolPolicy": {}})
    observed_at = time.time_ns()

    assert sessions_mod._agents_dir_revision(tmp_path) is None

    monkeypatch.setattr(sessions_mod.time, "time_ns", lambda: observed_at + 3_000_000_000)
    assert sessions_mod._agents_dir_revision(tmp_path) is not None


def test_an_edit_during_the_read_is_not_memoized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = tmp_path / f"{AGENT}.json"
    old_policy = {"exclude": ["shell"]}
    new_policy = {"exclude": ["browser"]}
    _write_spec(spec, {"name": AGENT, "managedToolPolicy": old_policy})
    real_read = sessions_mod._read_managed_tool_policy_uncached
    first_call = True

    def read_while_editing(agents_dir: Path, agent_name: str) -> dict[str, Any] | None:
        nonlocal first_call
        policy = real_read(agents_dir, agent_name)
        if first_call:
            first_call = False
            _write_spec(spec, {"name": AGENT, "managedToolPolicy": new_policy})
            _bump_mtime(spec, 5)
        return policy

    monkeypatch.setattr(sessions_mod, "_read_managed_tool_policy_uncached", read_while_editing)

    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == old_policy
    assert str(tmp_path) not in sessions_mod._TOOL_POLICY_CACHE
    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == new_policy


def test_a_directory_past_the_entry_cap_is_not_memoized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sessions_mod, "_TOOL_POLICY_REVISION_MAX_ENTRIES", 2)
    monkeypatch.setattr(sessions_mod, "_TOOL_POLICY_REVISION_OVERFLOW_WARNED", set())
    _write_spec(
        tmp_path / f"{AGENT}.json",
        {"name": AGENT, "managedToolPolicy": {"exclude": ["shell"]}},
    )
    _write_spec(tmp_path / "other-a.json", {"name": "other-a"})
    _write_spec(tmp_path / "other-b.json", {"name": "other-b"})

    with caplog.at_level("WARNING", logger=sessions_mod.__name__):
        assert sessions_mod._agents_dir_revision(tmp_path) is None
        assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == {
            "exclude": ["shell"]
        }
        assert sessions_mod._agents_dir_revision(tmp_path) is None

    warnings = [
        record for record in caplog.records if "tool-policy memo disabled" in record.message
    ]
    assert len(warnings) == 1
    assert "3 spec entries exceed 2" in warnings[0].message


def test_the_memo_is_disabled_when_the_platform_cannot_prove_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions_mod, "_TOOL_POLICY_MEMO_ENABLED", False)
    _write_spec(
        tmp_path / f"{AGENT}.json",
        {"name": AGENT, "managedToolPolicy": {"exclude": ["shell"]}},
    )
    reads = _count_reads(monkeypatch)

    assert sessions_mod._agents_dir_revision(tmp_path) is None
    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == {"exclude": ["shell"]}
    first_read_count = len(reads)
    assert first_read_count > 0
    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == {"exclude": ["shell"]}
    assert len(reads) > first_read_count


def test_a_symlink_entry_is_seen_without_creating_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = MagicMock()
    entry.name = f"{AGENT}.json"
    entry.is_symlink.return_value = True
    real_scandir = os.scandir

    class _FakeScan:
        def __init__(self, entries):
            self._it = iter(entries)

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._it)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def close(self):
            return None

    intercept = {"active": False}

    def scan(path=".", *args, **kwargs):
        try:
            is_target = Path(path).resolve() == tmp_path.resolve()
        except TypeError:
            is_target = False
        if intercept["active"] and is_target:
            return _FakeScan([entry])
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(sessions_mod.os, "scandir", scan)

    intercept["active"] = True
    try:
        assert sessions_mod._agents_dir_revision(tmp_path) is None
    finally:
        intercept["active"] = False
    entry.stat.assert_not_called()


def test_the_catalog_invalidation_point_drops_cached_policy_answers_too(tmp_path: Path) -> None:
    """The shared generation makes the catalog invalidation point sufficient."""
    _populate(tmp_path)
    assert sessions_mod._read_managed_tool_policy_sync(tmp_path, AGENT) == {"exclude": ["shell"]}
    assert sessions_mod._TOOL_POLICY_CACHE
    before = sessions_mod._agents_dir_revision(tmp_path)
    assert sessions_mod._agents_dir_revision(tmp_path) == before

    agent_discovery.clear_list_agents_cache()

    assert sessions_mod._agents_dir_revision(tmp_path) != before


def test_the_revision_ignores_files_the_spec_scans_ignore(tmp_path: Path) -> None:
    _write_spec(tmp_path / f"{AGENT}.json", {"managedToolPolicy": {}})
    before = sessions_mod._agents_dir_revision(tmp_path)
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    # The directory's own mtime moved, so the revision does; but the stray
    # file itself is not an entry of it.
    after = sessions_mod._agents_dir_revision(tmp_path)
    assert [e[0] for e in after[1]] == [f"{AGENT}.json"]
    assert [e[0] for e in before[1]] == [f"{AGENT}.json"]


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
