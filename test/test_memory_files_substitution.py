"""The seam DECIDES which storage answers -- not whichever directory exists on disk.

A provider returns a NON-local implementation, and two halves are pinned:

* it answers even when a local tree holds different bytes (no stale local copy); and
* removing the local tree does not change the answer.

The stand-in is NOT a mock: it keeps real text in a dict and implements the protocol,
so the protocol is shown to be implementable without a filesystem.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from kiro_crew.memory import MemoryStore
from kiro_crew.platform.context import current_context, set_context
from kiro_crew.platform.interfaces import MemoryEntry, MemoryRoots


class DictMemoryFiles:
    """A ``MemoryFiles`` whose storage is a dict -- no filesystem anywhere.

    Implements the protocol's stated semantics rather than recording calls: a
    missing file reads as empty, ``replace_if`` compares byte-for-byte and
    refuses on drift, ``glob`` matches one level by suffix. That makes it a
    substitutability witness instead of a mock.
    """

    def __init__(self, docs: Optional[Dict[str, str]] = None) -> None:
        self.docs: Dict[str, str] = dict(docs or {})
        self.dirs: set[str] = set()

    # ── reads ──

    def read_entry(
        self, path: Path, *, require_readable: bool = False, missing_ok: bool = True
    ) -> MemoryEntry:
        text = self.docs.get(str(path))
        if text is None:
            if not missing_ok:
                raise OSError(f"missing: {path}")
            return MemoryEntry(path=str(path))
        if text == "":
            return MemoryEntry(path=str(path))
        return MemoryEntry(path=str(path), updated_at="2026-09-26T00:00:00+00:00", content=text)

    def read_text(self, path: Path) -> str:
        return self.docs.get(str(path), "")

    def read_text_for_rewrite(self, path: Path) -> str:
        return self.docs.get(str(path), "")

    # ── writes ──

    def write(self, path: Path, content: str, *, newline: Optional[str] = None) -> None:
        self.docs[str(path)] = content

    def replace_if(
        self,
        path: Path,
        content: str,
        *,
        base: Optional[str],
        newline: Optional[str] = None,
    ) -> bool:
        if base is not None and self.docs.get(str(path), "") != base:
            return False
        self.docs[str(path)] = content
        return True

    # ── structure ──

    def exists(self, path: Path) -> bool:
        return str(path) in self.docs or str(path) in self.dirs

    def is_dir(self, path: Path) -> bool:
        return str(path) in self.dirs

    def glob(self, directory: Path, pattern: str) -> List[Path]:
        suffix = pattern.lstrip("*")
        sep = os.sep
        prefix = str(directory).rstrip(sep) + sep
        return sorted(
            Path(k)
            for k in self.docs
            if k.startswith(prefix) and sep not in k[len(prefix) :] and k.endswith(suffix)
        )

    def mkdir(self, path: Path) -> None:
        self.dirs.add(str(path))

    def remove(self, path: Path) -> None:
        self.docs.pop(str(path), None)

    def lock(self, path: Path) -> Any:
        from contextlib import nullcontext

        return nullcontext()


class DictProvider:
    """Serves one :class:`DictMemoryFiles` for every store."""

    def __init__(self, files: DictMemoryFiles) -> None:
        self.files = files
        self.asked: List[MemoryRoots] = []

    def files_for(self, roots: MemoryRoots) -> Any:
        self.asked.append(roots)
        return self.files


@pytest.fixture
def injected(monkeypatch: pytest.MonkeyPatch):
    """Compose a context whose ``memory_files`` is the dict implementation."""
    base = current_context()
    files = DictMemoryFiles()
    provider = DictProvider(files)
    set_context(dataclasses.replace(base, memory_files=provider))
    try:
        yield provider
    finally:
        set_context(base)


def _seed_local_tree(tmp_path: Path, *, preferences: str) -> Path:
    """A real local memory tree holding DIFFERENT bytes from the injected store."""
    memory_dir = tmp_path / "ws" / "memory"
    (memory_dir / "history").mkdir(parents=True)
    (memory_dir / "preferences.md").write_text(preferences, encoding="utf-8")
    return tmp_path / "ws"


class TestTheInjectedImplementationAnswers:
    def test_a_read_is_served_by_the_seam_not_the_local_file(
        self, tmp_path: Path, injected: DictProvider
    ) -> None:
        """The failure this pins is the one that matters most for a mount.

        If a read fell through to local disk, a mounted session would serve
        whatever stale copy this machine happens to hold -- and the next
        whole-file write would then publish it over the live document.
        """
        workspace = _seed_local_tree(tmp_path, preferences="- STALE local copy\n")
        store = MemoryStore(workspace=workspace)
        injected.files.docs[str(workspace / "memory" / "preferences.md")] = "- the live copy\n"

        assert store.read_preferences() == "- the live copy\n"
        # and the local file was not consulted, nor rewritten
        assert (workspace / "memory" / "preferences.md").read_text(
            encoding="utf-8"
        ) == "- STALE local copy\n"

    def test_a_read_still_works_with_no_local_tree_at_all(
        self, tmp_path: Path, injected: DictProvider
    ) -> None:
        """The inverse direction: the answer does not depend on local disk."""
        workspace = _seed_local_tree(tmp_path, preferences="- STALE local copy\n")
        store = MemoryStore(workspace=workspace)
        injected.files.docs[str(workspace / "memory" / "preferences.md")] = "- the live copy\n"

        shutil.rmtree(workspace / "memory")

        assert store.read_preferences() == "- the live copy\n"

    def test_a_write_lands_in_the_seam_and_not_on_local_disk(
        self, tmp_path: Path, injected: DictProvider
    ) -> None:
        workspace = _seed_local_tree(tmp_path, preferences="- STALE local copy\n")
        store = MemoryStore(workspace=workspace)
        key = str(workspace / "memory" / "preferences.md")
        injected.files.docs[key] = "- before\n"

        assert store.write_preferences("- after\n") is True

        assert injected.files.docs[key] == "- after\n"
        assert (workspace / "memory" / "preferences.md").read_text(
            encoding="utf-8"
        ) == "- STALE local copy\n"

    def test_history_enumeration_comes_from_the_seam(
        self, tmp_path: Path, injected: DictProvider
    ) -> None:
        """``glob``/``exists``/``is_dir`` must be answered by the implementation.

        A local directory listing here is the same bug as a local read: it would
        make a mounted session's history the union of two unrelated trees.
        """
        workspace = _seed_local_tree(tmp_path, preferences="- x\n")
        history = workspace / "memory" / "history"
        (history / "2020-01-01.md").write_text("# local only\n", encoding="utf-8")
        store = MemoryStore(workspace=workspace)
        injected.files.docs[str(history / "2026-09-25.md")] = "# 2026-09-25\n\n#### 09:00\nremote\n"

        days = {Path(e["path"]).name for e in store.read_history_entries()}

        assert days == {"2026-09-25.md"}
        assert (history / "2020-01-01.md").exists()  # local file untouched

    def test_pruning_removes_from_the_seam_and_leaves_local_files_alone(
        self, tmp_path: Path, injected: DictProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``prune_history`` dates its cutoff from ``datetime.now()``. Pinning that
        # clock one day after the recent entry keeps the entry inside the 30-day
        # window on any calendar day and in any host zone.
        class _PinnedClock(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return cls(2026, 9, 26, 12, tzinfo=tz)

        monkeypatch.setattr("kiro_crew.memory.datetime", _PinnedClock)
        workspace = _seed_local_tree(tmp_path, preferences="- x\n")
        history = workspace / "memory" / "history"
        (history / "2020-01-01.md").write_text("# local only\n", encoding="utf-8")
        store = MemoryStore(workspace=workspace)
        injected.files.docs[str(history / "2020-01-01.md")] = "# old\n"
        injected.files.docs[str(history / "2026-09-25.md")] = "# recent\n"

        removed = store.prune_history(keep_days=30)

        assert removed == 1
        assert str(history / "2020-01-01.md") not in injected.files.docs
        assert str(history / "2026-09-25.md") in injected.files.docs
        # The local file of the same name survives: this phase never deletes one.
        assert (history / "2020-01-01.md").exists()

    def test_the_provider_is_asked_once_per_store_with_that_store_s_roots(
        self, tmp_path: Path, injected: DictProvider
    ) -> None:
        """Resolution is cached, and carries the roots an edition decides on.

        An edition that mounts some stores and not others can only do that if it
        is told which store it is answering for, so the roots must arrive intact.
        """
        workspace = _seed_local_tree(tmp_path, preferences="- x\n")
        store = MemoryStore(workspace=workspace)

        store.read_preferences()
        store.read_projects()
        store.read_preferences()

        assert len(injected.asked) == 1
        roots = injected.asked[0]
        assert roots.workspace == workspace
        assert roots.memory_dir == workspace / "memory"
        assert roots.history_dir == workspace / "memory" / "history"


class TestProviderFailureIsNotSwallowed:
    def test_a_provider_that_raises_does_not_fall_back_to_local_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-closed, per the design: never serve a stale local copy silently.

        A provider raising means the edition could not supply the storage it meant
        to. Falling back would read this machine's abandoned copy and then let the
        next write publish it over the live document -- so the failure has to
        propagate, and the read surface's own callers degrade visibly instead.
        """

        class Boom:
            def files_for(self, roots: MemoryRoots) -> Any:
                raise RuntimeError("drive unreachable at boot")

        base = current_context()
        workspace = _seed_local_tree(tmp_path, preferences="- STALE local copy\n")
        set_context(dataclasses.replace(base, memory_files=Boom()))
        try:
            store = MemoryStore(workspace=workspace)
            with pytest.raises(RuntimeError, match="drive unreachable"):
                store.read_preferences()
        finally:
            set_context(base)

    def test_a_composition_error_is_not_swallowed_into_local_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CPP fail-closed signal must propagate, not degrade to local disk.

        ``memory_files_for`` re-raises ``PlatformCompositionError`` above its bare
        ``except Exception``; falling back would serve a stale local copy that the next
        write publishes over the live document.
        """
        import kiro_crew.memory_files as mf
        import kiro_crew.platform.context as ctx
        from kiro_crew.platform.context import PlatformCompositionError
        from kiro_crew.platform.interfaces import MemoryRoots

        def _raise() -> Any:
            raise PlatformCompositionError("platform did not compose (governed host)")

        # memory_files_for imports current_context from this module at call time,
        # so patch it at its source.
        monkeypatch.setattr(ctx, "current_context", _raise)
        roots = MemoryRoots(
            workspace=tmp_path / "ws",
            memory_dir=tmp_path / "ws" / "memory",
            history_dir=tmp_path / "ws" / "memory" / "history",
        )
        with pytest.raises(PlatformCompositionError):
            mf.memory_files_for(roots)


class TestNoContextStillWorks:
    def test_a_store_built_with_no_composed_context_uses_local_disk(self, tmp_path: Path) -> None:
        """The one tolerated fallback: a bare unit test or an unbooted worker.

        This is a standalone-shaped situation, not a failed mount, so it yields
        the standalone implementation -- which is what keeps several hundred
        direct ``MemoryStore(...)`` constructions across this suite working.
        """
        from kiro_crew.memory_files import LocalMemoryFiles

        workspace = _seed_local_tree(tmp_path, preferences="- from local disk\n")
        store = MemoryStore(workspace=workspace)

        assert isinstance(store._files, LocalMemoryFiles)
        assert store.read_preferences() == "- from local disk\n"
