"""The agents-directory revision and the memo pinned to it, shared by the two
``spec_by_declared_name`` callers.

:func:`kiro_crew.agent_discovery.agents_dir_revision` is a stat-only fingerprint
strong enough to pin a read answer to: it answers ``None`` whenever entry
metadata could miss a rewrite (a symlinked spec, a fresh edit inside the racy
window, a directory past the entry cap, a platform whose ``ctime`` is creation
time). :class:`kiro_crew.agent_discovery.AgentsDirMemo` holds the store and hit
rules once, so the tool-policy read and the KAS projection cannot drift apart.

The KAS tests pin the second caller: a KAS session start resolves its agent by
parsing every spec in the directory, and with a couple of thousand installed
specs that is the same GIL-holding second the tool-policy read paid. An
unchanged directory must answer from one ``scandir``, and the answer handed to a
session must be a copy the caller owns.
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
from kiro_crew.acp import kas_agents

AGENT = "reviewer"


@pytest.fixture(autouse=True)
def _memo_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_discovery, "AGENTS_DIR_MEMO_ENABLED", True)
    monkeypatch.setattr(agent_discovery, "_AGENTS_DIR_RACY_WINDOW_NS", 0)
    monkeypatch.setattr(kas_agents, "_SPEC_SCAN_MEMO", agent_discovery.AgentsDirMemo())


def _write_spec(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _bump_mtime(path: Path, seconds: int) -> None:
    """Move *path*'s mtime by a whole number of seconds, so an edit is never
    lost inside one filesystem timestamp tick."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


def _count_spec_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every spec the declared-name scan parses."""
    reads: list[Path] = []
    real_read = agent_discovery._read_agent_spec

    def recording_read(path: Path, **kwargs: Any) -> Any:
        reads.append(path)
        return real_read(path, **kwargs)

    monkeypatch.setattr(agent_discovery, "_read_agent_spec", recording_read)
    return reads


# --- agents_dir_revision -------------------------------------------------------


def test_the_revision_sees_content_and_permission_changes(tmp_path: Path) -> None:
    spec = tmp_path / f"{AGENT}.json"
    _write_spec(spec, {"managedToolPolicy": {}})
    before = agent_discovery.agents_dir_revision(tmp_path)

    _write_spec(spec, {"managedToolPolicy": {}, "description": "changed"})
    after_content = agent_discovery.agents_dir_revision(tmp_path)
    assert after_content is not None
    assert after_content != before

    if os.name != "nt":
        # POSIX exposes permission bits through st_mode. Keep this assertion in
        # the cross-platform test rather than skipping the whole ratchet.
        time.sleep(0.05)
        spec.chmod(stat.S_IRUSR)
        after_mode = agent_discovery.agents_dir_revision(tmp_path)
        assert after_mode is not None
        assert after_mode != after_content
        assert after_mode[1][0][6] != after_content[1][0][6]


def test_a_fresh_spec_is_not_memoized_until_the_racy_window_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_discovery, "_AGENTS_DIR_RACY_WINDOW_NS", 2_000_000_000)
    _write_spec(tmp_path / f"{AGENT}.json", {"managedToolPolicy": {}})
    observed_at = time.time_ns()

    assert agent_discovery.agents_dir_revision(tmp_path) is None

    monkeypatch.setattr(agent_discovery.time, "time_ns", lambda: observed_at + 3_000_000_000)
    assert agent_discovery.agents_dir_revision(tmp_path) is not None


def test_a_directory_past_the_entry_cap_is_not_memoized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(agent_discovery, "_AGENTS_DIR_REVISION_MAX_ENTRIES", 2)
    monkeypatch.setattr(agent_discovery, "_AGENTS_DIR_REVISION_OVERFLOW_WARNED", set())
    _write_spec(tmp_path / f"{AGENT}.json", {"name": AGENT})
    _write_spec(tmp_path / "other-a.json", {"name": "other-a"})
    _write_spec(tmp_path / "other-b.json", {"name": "other-b"})
    memo: agent_discovery.AgentsDirMemo[int] = agent_discovery.AgentsDirMemo()
    calls = 0

    def compute() -> int:
        nonlocal calls
        calls += 1
        return calls

    with caplog.at_level("WARNING", logger=agent_discovery.__name__):
        assert agent_discovery.agents_dir_revision(tmp_path) is None
        assert memo.get(tmp_path, AGENT, compute) == 1
        assert memo.get(tmp_path, AGENT, compute) == 2
        assert agent_discovery.agents_dir_revision(tmp_path) is None

    warnings = [record for record in caplog.records if "agents-dir memo disabled" in record.message]
    assert len(warnings) == 1
    assert "3 spec entries exceed 2" in warnings[0].message
    # Nothing was stored: a further call computes again.
    assert memo.get(tmp_path, AGENT, compute) == 3


def test_the_revision_is_unavailable_when_the_platform_cannot_prove_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_discovery, "AGENTS_DIR_MEMO_ENABLED", False)
    _write_spec(tmp_path / f"{AGENT}.json", {"name": AGENT})

    assert agent_discovery.agents_dir_revision(tmp_path) is None


class _FakeScan:
    """The context-manager iterator ``os.scandir`` returns, over fake entries."""

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


def _serve_fake_entries(
    monkeypatch: pytest.MonkeyPatch, target: Path, entries: list[Any] | OSError
) -> dict[str, bool]:
    """Make ``scandir`` of *target* yield *entries* while ``active`` is set; every
    other directory still reaches the real ``scandir``. An ``OSError`` instance
    in place of the list is raised by the ``scandir`` call itself."""
    real_scandir = os.scandir
    intercept = {"active": False}

    def scan(path=".", *args, **kwargs):
        try:
            is_target = Path(path).resolve() == target.resolve()
        except TypeError:
            is_target = False
        if intercept["active"] and is_target:
            if isinstance(entries, OSError):
                raise entries
            return _FakeScan(entries)
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(agent_discovery.os, "scandir", scan)
    return intercept


def test_a_symlink_entry_is_seen_without_creating_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = MagicMock()
    entry.name = f"{AGENT}.json"
    entry.is_symlink.return_value = True
    intercept = _serve_fake_entries(monkeypatch, tmp_path, [entry])

    intercept["active"] = True
    try:
        assert agent_discovery.agents_dir_revision(tmp_path) is None
    finally:
        intercept["active"] = False
    entry.stat.assert_not_called()


def test_an_unlistable_directory_gives_no_revision_but_keeps_the_catalog_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that cannot be listed is not an empty one: a spec inside it
    may still be read directly by name, and an in-place edit of that spec moves
    nothing the revision could see. The catalog signature stays tolerant."""
    _write_spec(tmp_path / f"{AGENT}.json", {"managedToolPolicy": {}})
    intercept = _serve_fake_entries(monkeypatch, tmp_path, PermissionError(13, "Permission denied"))

    intercept["active"] = True
    try:
        assert agent_discovery.agents_dir_revision(tmp_path) is None
        assert agent_discovery._dir_signature(tmp_path) == ()
    finally:
        intercept["active"] = False


def test_an_entry_whose_kind_cannot_be_read_gives_no_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``DirEntry`` of unknown ``d_type`` answers ``is_symlink`` with an
    ``lstat``, which can fail; freshness is then unprovable and the failure must
    not escape to the caller."""
    entry = MagicMock()
    entry.name = f"{AGENT}.json"
    entry.is_symlink.side_effect = PermissionError(13, "Permission denied")
    intercept = _serve_fake_entries(monkeypatch, tmp_path, [entry])

    intercept["active"] = True
    try:
        assert agent_discovery.agents_dir_revision(tmp_path) is None
    finally:
        intercept["active"] = False
    entry.stat.assert_not_called()


def test_the_revision_ignores_files_the_spec_scans_ignore(tmp_path: Path) -> None:
    _write_spec(tmp_path / f"{AGENT}.json", {"managedToolPolicy": {}})
    before = agent_discovery.agents_dir_revision(tmp_path)
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    # The directory's own mtime moved, so the revision does; but the stray
    # file itself is not an entry of it.
    after = agent_discovery.agents_dir_revision(tmp_path)
    assert before is not None and after is not None
    assert [e[0] for e in after[1]] == [f"{AGENT}.json"]
    assert [e[0] for e in before[1]] == [f"{AGENT}.json"]


def test_the_catalog_signature_stays_on_where_the_revision_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalog caches tolerate a same-tick edit and must keep caching where
    the stricter fingerprint declines; the two walks share a scan, not a contract.

    The symlinked entry is a fake ``DirEntry`` over a real file's ``stat``, so
    no symlink is created (unelevated Windows cannot create one)."""
    target = tmp_path / "real.json"
    _write_spec(target, {"name": "real"})
    real_entry = MagicMock()
    real_entry.name = "real.json"
    real_entry.is_symlink.return_value = False
    real_entry.stat.side_effect = lambda **kwargs: target.stat()
    link_entry = MagicMock()
    link_entry.name = f"{AGENT}.json"
    link_entry.is_symlink.return_value = True
    link_entry.stat.side_effect = lambda **kwargs: target.stat()
    intercept = _serve_fake_entries(monkeypatch, tmp_path, [real_entry, link_entry])

    intercept["active"] = True
    try:
        assert agent_discovery.agents_dir_revision(tmp_path) is None
        signature = agent_discovery._dir_signature(tmp_path)
    finally:
        intercept["active"] = False
    assert sorted(name for name, _ in signature) == sorted([f"{AGENT}.json", "real.json"])
    assert all(mtime > 0 for _, mtime in signature)


# --- AgentsDirMemo -------------------------------------------------------------


def test_the_memo_serves_an_unchanged_directory_and_sees_an_edit(tmp_path: Path) -> None:
    spec = tmp_path / f"{AGENT}.json"
    _write_spec(spec, {"name": AGENT})
    memo: agent_discovery.AgentsDirMemo[int] = agent_discovery.AgentsDirMemo()
    calls = 0

    def compute() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert memo.get(tmp_path, AGENT, compute) == 1
    assert memo.get(tmp_path, AGENT, compute) == 1
    assert memo.get(tmp_path, "someone-else", compute) == 2

    _write_spec(spec, {"name": AGENT, "description": "edited"})
    _bump_mtime(spec, 5)
    assert memo.get(tmp_path, AGENT, compute) == 3


def test_the_memo_stores_nothing_when_compute_raises(tmp_path: Path) -> None:
    _write_spec(tmp_path / f"{AGENT}.json", {"name": AGENT})
    memo: agent_discovery.AgentsDirMemo[int] = agent_discovery.AgentsDirMemo()

    def boom() -> int:
        raise ValueError("refused")

    with pytest.raises(ValueError):
        memo.get(tmp_path, AGENT, boom)
    # Nothing was stored under the key: the next call computes.
    assert memo.get(tmp_path, AGENT, lambda: 7) == 7


def test_the_memo_answer_set_is_capped_per_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_discovery, "_AGENTS_DIR_MEMO_MAX_KEYS", 2)
    _write_spec(tmp_path / f"{AGENT}.json", {"name": AGENT})
    memo: agent_discovery.AgentsDirMemo[str] = agent_discovery.AgentsDirMemo()

    assert memo.get(tmp_path, "a", lambda: "a1") == "a1"
    assert memo.get(tmp_path, "b", lambda: "b1") == "b1"
    assert memo.get(tmp_path, "a", lambda: "a2") == "a1"
    # A third key overflows the set: it is cleared and the new answer stored.
    assert memo.get(tmp_path, "c", lambda: "c1") == "c1"
    assert memo.get(tmp_path, "a", lambda: "a3") == "a3"


def test_the_catalog_invalidation_point_moves_the_revision_past_memoized_answers(
    tmp_path: Path,
) -> None:
    """In-process spec writers call ``clear_list_agents_cache``; the generation
    in the revision tuple moves, so a stored answer misses and the memo
    computes again without being told."""
    _write_spec(tmp_path / f"{AGENT}.json", {"name": AGENT})
    memo: agent_discovery.AgentsDirMemo[int] = agent_discovery.AgentsDirMemo()
    calls = 0

    def compute() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert memo.get(tmp_path, AGENT, compute) == 1
    assert memo.get(tmp_path, AGENT, compute) == 1, "the answer was stored"
    before = agent_discovery.agents_dir_revision(tmp_path)
    assert agent_discovery.agents_dir_revision(tmp_path) == before

    agent_discovery.clear_list_agents_cache()

    assert agent_discovery.agents_dir_revision(tmp_path) != before
    assert memo.get(tmp_path, AGENT, compute) == 2, "the stored answer misses after the clear"


# --- kas_agents.load_agent_spec ------------------------------------------------


def test_kas_load_agent_spec_reads_once_per_directory_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    others = 5
    for i in range(others):
        _write_spec(tmp_path / f"other-{i}.json", {"name": f"other-{i}"})
    spec = tmp_path / f"SomePackage-{AGENT}.json"
    _write_spec(spec, {"name": AGENT, "description": "first"})
    reads = _count_spec_reads(monkeypatch)

    assert kas_agents.load_agent_spec(tmp_path, AGENT)["description"] == "first"
    assert len(reads) == others + 1

    reads.clear()
    assert kas_agents.load_agent_spec(tmp_path, AGENT)["description"] == "first"
    assert reads == [], (
        "the agents directory did not change between two session starts, yet "
        "the second start re-read every spec"
    )

    _write_spec(spec, {"name": AGENT, "description": "edited"})
    _bump_mtime(spec, 5)
    reads.clear()
    assert kas_agents.load_agent_spec(tmp_path, AGENT)["description"] == "edited"
    assert len(reads) == others + 1


def test_kas_memoized_spec_is_a_copy(tmp_path: Path) -> None:
    _write_spec(tmp_path / f"SomePackage-{AGENT}.json", {"name": AGENT, "tools": ["shell"]})

    first = kas_agents.load_agent_spec(tmp_path, AGENT)
    first["tools"].append("browser")
    first["name"] = "someone-else"

    second = kas_agents.load_agent_spec(tmp_path, AGENT)
    assert second == {"name": AGENT, "tools": ["shell"]}
    assert second is not first


def test_kas_refusals_are_re_derived_on_every_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_spec(tmp_path / f"Alpha-{AGENT}.json", {"name": AGENT})
    _write_spec(tmp_path / f"Beta-{AGENT}.json", {"name": AGENT})
    reads = _count_spec_reads(monkeypatch)

    with pytest.raises(kas_agents.KasAgentTranslationError):
        kas_agents.load_agent_spec(tmp_path, AGENT)
    assert reads
    reads.clear()
    with pytest.raises(kas_agents.KasAgentTranslationError):
        kas_agents.load_agent_spec(tmp_path, AGENT)
    assert reads, "a refusal must not be served from the memo"


def test_kas_direct_filename_fallback_is_not_memoized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing declaring the id, the scan's ``None`` is memoized but the
    ``<id>.json`` read is one file and is performed every call."""
    direct = tmp_path / f"{AGENT}.json"
    _write_spec(direct, {"name": "other", "description": "stem fallback"})
    _write_spec(tmp_path / "sibling.json", {"name": "sibling"})
    reads = _count_spec_reads(monkeypatch)
    strict_reads: list[Path] = []
    real_strict = kas_agents.read_agent_spec_strict

    def recording_strict(path: Path, **kwargs: Any) -> Any:
        strict_reads.append(path)
        return real_strict(path, **kwargs)

    monkeypatch.setattr(kas_agents, "read_agent_spec_strict", recording_strict)

    assert kas_agents.load_agent_spec(tmp_path, AGENT)["description"] == "stem fallback"
    assert reads and strict_reads == [direct]
    reads.clear()
    assert kas_agents.load_agent_spec(tmp_path, AGENT)["description"] == "stem fallback"
    assert reads == []
    assert strict_reads == [direct, direct]
