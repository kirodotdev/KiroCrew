"""A warmed recent-history read is reused once, and only while the directory is unchanged.

A new chat's pre-warm reads the history files (``warm_recent_history``), and the
first message's build reuses that read. Nothing that changed in the directory
since may be hidden by it: a rewritten day, a new day, a removed day, or a day
swapped for a symlink all read every file again, through the same hardened
reader. Reads nobody warmed keep the plain TTL and never reuse past it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from eager_spawn_helpers import (  # noqa: F401 -- fixtures are used by name
    _isolate_armed_registry,
    _pin_prewarm_allowance,
)

from kiro_crew import memory as memory_mod
from kiro_crew.memory import MemoryStore

SECRET = "aws_secret_access_key = SUPERSECRET"

posix_only = pytest.mark.skipif(os.name == "nt", reason="history reuse is POSIX-only")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    # Past the TTL from the first read on, so only the warmed reuse can skip a read.
    monkeypatch.setattr(memory_mod, "_HISTORY_CACHE_TTL_SECS", 0.0)
    ms = MemoryStore(workspace=tmp_path / "ws")
    ms.init()
    ms.append_history("FIRST_ENTRY")
    return ms


def _today_file(ms: MemoryStore) -> Path:
    """The day file ``append_history`` wrote: the fixture writes exactly one."""
    (day,) = sorted(ms._history_dir.glob("*.md"))
    return day


def _count_uncached_reads(ms: MemoryStore, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = ms._read_recent_history_uncached

    def counting(days, today, **kwargs):
        calls.append(days)
        return real(days, today, **kwargs)

    monkeypatch.setattr(ms, "_read_recent_history_uncached", counting)
    return calls


@posix_only
def test_an_unchanged_directory_after_the_warm_is_not_read_again(store, monkeypatch):
    store.warm_recent_history(days=14)
    calls = _count_uncached_reads(store, monkeypatch)
    assert "FIRST_ENTRY" in store.read_recent_history(days=14)
    assert calls == []


@posix_only
def test_the_warmed_read_is_used_once(store, monkeypatch):
    store.warm_recent_history(days=14)
    calls = _count_uncached_reads(store, monkeypatch)
    store.read_recent_history(days=14)
    store.read_recent_history(days=14)
    assert calls == [14]


def test_reads_nobody_warmed_never_reuse(store, monkeypatch):
    calls = _count_uncached_reads(store, monkeypatch)
    store.read_recent_history(days=14)
    store.read_recent_history(days=14)
    assert calls == [14, 14]


def test_a_rewritten_day_is_read_again(store, monkeypatch):
    store.warm_recent_history(days=14)
    calls = _count_uncached_reads(store, monkeypatch)
    path = _today_file(store)
    path.write_text(path.read_text(encoding="utf-8") + "\nWRITTEN_OUTSIDE\n", encoding="utf-8")
    assert "WRITTEN_OUTSIDE" in store.read_recent_history(days=14)
    assert calls == [14]


def test_a_new_day_file_is_read(store, monkeypatch):
    store.warm_recent_history(days=14)
    (store._history_dir / "2000-01-01.md").write_text("# old\n", encoding="utf-8")
    calls = _count_uncached_reads(store, monkeypatch)
    store.read_recent_history(days=14)
    assert calls == [14]


def test_a_day_swapped_for_a_symlink_is_refused_not_reused(store, tmp_path):
    store.warm_recent_history(days=14)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "credentials"
    secret.write_text(SECRET, encoding="utf-8")
    path = _today_file(store)
    path.unlink()
    try:
        path.symlink_to(secret)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without symlinks
        pytest.skip("symlinks not available on this platform")
    history = store.read_recent_history(days=14)
    assert SECRET not in history
    assert "FIRST_ENTRY" not in history
    assert SECRET not in json.dumps(store.markdown_snapshot())


def test_a_day_that_gains_a_hard_link_is_read_again(store, tmp_path, monkeypatch):
    store.warm_recent_history(days=14)
    calls = _count_uncached_reads(store, monkeypatch)
    try:
        os.link(_today_file(store), tmp_path / "extra-link.md")
    except (OSError, NotImplementedError):  # pragma: no cover - no hard links here
        pytest.skip("hard links not available on this platform")
    store.read_recent_history(days=14)
    assert calls == [14]


def test_reuse_ends_after_its_bound(store, monkeypatch):
    store.warm_recent_history(days=14)
    monkeypatch.setattr(memory_mod, "_HISTORY_REUSE_MAX_SECS", 0.0)
    calls = _count_uncached_reads(store, monkeypatch)
    store.read_recent_history(days=14)
    assert calls == [14]


def test_an_append_drops_the_reuse(store, monkeypatch):
    store.warm_recent_history(days=14)
    store.append_history("SECOND_ENTRY")
    assert "SECOND_ENTRY" in store.read_recent_history(days=14)


@posix_only
def test_a_day_rewritten_after_the_warm_is_read_again_inside_the_ttl(tmp_path, monkeypatch):
    # The first message often builds inside the plain TTL of the pre-warm's read;
    # the warm writes no TTL entry, so another process's edit is still seen.
    monkeypatch.setattr(memory_mod, "_HISTORY_CACHE_TTL_SECS", 3600.0)  # flake-ok: never waited
    ms = MemoryStore(workspace=tmp_path / "ws")
    ms.init()
    ms.append_history("FIRST_ENTRY")
    ms.warm_recent_history(days=14)
    path = _today_file(ms)
    path.write_text(path.read_text(encoding="utf-8") + "\nWRITTEN_OUTSIDE\n", encoding="utf-8")
    assert "WRITTEN_OUTSIDE" in ms.read_recent_history(days=14)


def test_a_read_inside_the_ttl_lists_nothing(tmp_path, monkeypatch):
    # An explicit reader's TTL hit stays as cheap as before: no directory listing.
    monkeypatch.setattr(memory_mod, "_HISTORY_CACHE_TTL_SECS", 3600.0)  # flake-ok: never waited
    ms = MemoryStore(workspace=tmp_path / "ws")
    ms.init()
    ms.append_history("FIRST_ENTRY")
    ms.read_recent_history(days=14)
    monkeypatch.setattr(ms, "_history_signature", lambda: pytest.fail("listed inside the TTL"))
    assert "FIRST_ENTRY" in ms.read_recent_history(days=14)


@posix_only
def test_a_crowded_directory_gets_no_signature(store, monkeypatch):
    # Whatever is planted in the history directory, the signature never holds
    # more than its cap: past it there is no reuse, and every read goes to files.
    monkeypatch.setattr(memory_mod, "_HISTORY_SIGNATURE_MAX_ENTRIES", 3)
    for name in ("a", "b", "c"):
        (store._history_dir / name).write_text("x", encoding="utf-8")
    assert store._history_signature() is None
    calls = _count_uncached_reads(store, monkeypatch)
    store.warm_recent_history(days=14)
    store.read_recent_history(days=14)
    assert calls == [14, 14]


def test_without_reuse_support_the_directory_is_not_probed(store, monkeypatch):
    # Windows: no lstat or scandir runs ahead of the hardened reader's root guard,
    # the warm reads nothing, and every read past the TTL reads the files again.
    monkeypatch.setattr(memory_mod, "HISTORY_REUSE_SUPPORTED", False)
    monkeypatch.setattr(
        store, "_history_signature", lambda: pytest.fail("probed without reuse support")
    )
    calls = _count_uncached_reads(store, monkeypatch)
    store.warm_recent_history(days=14)
    assert calls == []
    store.read_recent_history(days=14)
    store.read_recent_history(days=14)
    assert calls == [14, 14]


# ── The new chat's pre-warm reads the history ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "is_new, resumed, warmed", [(True, False, True), (True, True, False), (False, False, False)]
)
async def test_a_fresh_prewarm_reads_the_history_early(monkeypatch, is_new, resumed, warmed):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from eager_spawn_helpers import _bindings, _cfg, _mock_state

    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat_runner import _eager_spawn
    from kiro_crew.dashboard.state import _ChatSlot

    monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
    slot = _ChatSlot("t1")
    state = _mock_state(slot)
    state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), is_new, resumed))
    memory = MagicMock()
    state.context_builder = MagicMock()
    state.context_builder.get_memory_for = MagicMock(return_value=memory)
    state._background_tasks = set()
    with (
        patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
        patch.object(chat_runner, "resolve_agent_bindings", return_value=_bindings()),
    ):
        await _eager_spawn(state, slot)
        if state._background_tasks:
            await asyncio.gather(*list(state._background_tasks))
    if warmed:
        # The chat's own store: its workspace (none for a new chat) and its store.
        assert state.context_builder.get_memory_for.call_args.args[0] == (slot.workspace or None)
        memory.warm_recent_history.assert_called_once_with(days=14)
    else:
        memory.warm_recent_history.assert_not_called()
