"""The artifact store's file helpers ask the sensitive-path fence off the pool.

``ArtifactStore._read_text`` / ``_write_text`` / ``_read_bytes`` / ``_write_bytes``
each canonicalise their path with ``os.path.realpath`` and then ask the fence.
Asking through the bounded ``is_sensitive_path`` costs two ``mc-pathres`` pool
hops per call (the candidate resolution the helper had already performed, plus
the anchor resolution), and ``list()`` reaches ``_read_text`` once per
``meta.json``. On a host with a few hundred artifacts one listing filled the
two-worker FIFO pool from the event loop; the gate then failed closed, the
listing logged every stall as ``refusing to read sensitive path`` and dropped
the artifact, and most of the library vanished from the UI under load.

The fix hands the fence the ``realpath`` the helper already holds, through
``is_sensitive_resolved_path``, whenever the helper runs OFF the event loop:
same decision, same targets, no submission. On the loop the bounded gate stays,
because the pre-resolved gate resolves its anchors inline and an inline
``realpath`` of a wedged root there is the stall the pool's budget prevents. The
dashboard listing handler therefore runs ``store.list()`` on a worker, which is
what earns the listing the off-pool gate. That gate's safety is a PRECONDITION
on its argument that no code enforces, so these tests pin it:

- every helper hands the gate a canonical spelling, and the listing entry point
  does so even when the store is rooted through a directory link;
- the listing submits nothing to the pool and never reaches the bounded gate;
- the two gates agree on canonical fenced and unfenced controls, with the
  literal verdicts spelled out so agreement cannot be vacuous;
- a fenced verdict still refuses every helper, and only the fenced path;
- on the event loop the bounded gate answers, off it the pre-resolved gate does,
  and ``GET /api/artifacts`` runs the listing off the loop.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from conftest import make_dir_link, requires_symlinks
from kiro_crew import artifacts as art_mod
from kiro_crew import security
from kiro_crew.artifacts import ArtifactError, ArtifactStore
from kiro_crew.atomic_write import on_event_loop
from kiro_crew.dashboard.handlers import artifacts as art_handlers


@pytest.fixture
def linked_root(tmp_path: Path) -> tuple[Path, Path]:
    """A real directory and a directory link that resolves to it.

    Paths spelled through the link are NOT canonical: ``os.path.realpath``
    rewrites the link component, so a helper that forgot to resolve would hand
    the gate a spelling the recorder can tell apart from the realpath.
    """
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    make_dir_link(alias, real)
    return real, alias


def _recorder(monkeypatch) -> list[str]:
    seen: list[str] = []
    real_gate = security.paths.is_sensitive_resolved_path

    def recording(resolved: str) -> bool:
        seen.append(resolved)
        return real_gate(resolved)

    monkeypatch.setattr(security.paths, "is_sensitive_resolved_path", recording)
    return seen


class TestHelpersHandTheGateCanonicalPaths:
    """The precondition of ``is_sensitive_resolved_path``, pinned per helper."""

    def test_each_helper_asks_about_the_realpath_not_the_spelling(
        self, tmp_path: Path, linked_root, monkeypatch
    ) -> None:
        real, alias = linked_root
        (real / "sub").mkdir()
        # Through the link AND through a dot-dot segment: pathlib keeps both, so
        # a helper that skipped realpath would hand the gate exactly this string.
        text_path = alias / "sub" / ".." / "sub" / "note.txt"
        bytes_path = alias / "sub" / ".." / "sub" / "blob.bin"
        store = ArtifactStore(root=tmp_path / "artifacts")
        seen = _recorder(monkeypatch)

        store._write_text(text_path, "hello")
        assert store._read_text(text_path) == "hello"
        store._write_bytes(bytes_path, b"\x89PNG")
        assert store._read_bytes(bytes_path) == b"\x89PNG"

        assert len(seen) == 4, seen
        assert seen == [os.path.realpath(p) for p in seen]
        assert seen[0] == seen[1] == os.path.realpath(str(text_path))
        assert seen[2] == seen[3] == os.path.realpath(str(bytes_path))
        # The raw spelling never reached the gate: it names the link, not the file.
        assert str(text_path) not in seen
        assert str(bytes_path) not in seen
        assert seen[0] != str(text_path)

    def test_the_listing_hands_the_gate_canonical_spellings(self, linked_root, monkeypatch) -> None:
        # The real entry point: list() -> _read_meta_file -> _read_text. Rooting
        # the store THROUGH the link makes _iter_meta_paths yield link-spelled
        # meta.json paths, so the realpath in _read_text is load-bearing here.
        real, alias = linked_root
        store = ArtifactStore(root=alias / "artifacts")
        for i in range(3):
            store.create(name=f"art-{i}", content=f"<p>{i}</p>")
        seen = _recorder(monkeypatch)

        listed = store.list()

        assert sorted(a.name for a in listed) == ["art-0", "art-1", "art-2"]
        metas = [p for p in seen if p.endswith("meta.json")]
        assert len(metas) == 3, seen
        assert seen == [os.path.realpath(p) for p in seen]
        assert not [p for p in seen if p.startswith(str(alias))], seen
        assert all(p.startswith(os.path.realpath(str(real))) for p in metas), metas


class TestTheListingStaysOffThePool:
    """``list()`` submits nothing to ``mc-pathres`` and never calls the bounded gate."""

    def test_listing_submits_nothing_and_skips_the_bounded_gate(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Construct and populate BEFORE arming the refusals: the root check in
        # __init__ deliberately stays on the bounded gate.
        store = ArtifactStore(root=tmp_path / "artifacts")
        for i in range(5):
            store.create(name=f"art-{i}", content=f"<p>{i}</p>")

        def refuse(expanded, worker, **kwargs):
            raise AssertionError(
                f"the artifact listing reached mc-pathres for {expanded!r} via {worker}"
            )

        monkeypatch.setattr(security.paths, "_run_resolution_bounded", refuse)
        # Cold target cache: the anchors must be rebuilt inline, not via the pool.
        monkeypatch.setattr(security.paths, "_home_targets_cache", {})
        monkeypatch.setattr(
            security.paths,
            "is_sensitive_path",
            lambda *a, **k: pytest.fail("the artifact listing called is_sensitive_path"),
        )

        listed = store.list()

        # Neither refusal is in list()'s warn-and-continue set, so a hit above
        # fails the test instead of shrinking this result.
        assert sorted(a.name for a in listed) == [f"art-{i}" for i in range(5)]

    def test_every_helper_skips_the_bounded_gate(self, tmp_path: Path, monkeypatch) -> None:
        store = ArtifactStore(root=tmp_path / "artifacts")
        monkeypatch.setattr(
            security.paths,
            "is_sensitive_path",
            lambda *a, **k: pytest.fail("a store file helper called is_sensitive_path"),
        )
        text_path = tmp_path / "files" / "note.txt"
        bytes_path = tmp_path / "files" / "blob.bin"
        store._write_text(text_path, "hello")
        store._write_bytes(bytes_path, b"\x00\x01")
        assert store._read_text(text_path) == "hello"
        assert store._read_bytes(bytes_path) == b"\x00\x01"


class TestTheTwoGatesAgree:
    """For a canonical path the pre-resolved gate is the bounded gate's verdict."""

    @staticmethod
    def _fenced_controls() -> list[str]:
        credential = os.path.realpath(os.path.expanduser("~/.aws/credentials"))
        parents = security.paths._home_dir_targets(security.paths._KEYSTONE_ARTIFACT_PARENTS)
        assert parents, "no keystone artifact parent resolved; the fixture home is wrong"
        # realpath'd so the spelling is canonical on every platform (the targets
        # are casefolded, and Windows realpath restores on-disk casing).
        keystone_temp = os.path.realpath(os.path.join(sorted(parents)[0], "meta.json.tmp"))
        return [credential, keystone_temp]

    def test_fenced_canonical_controls_are_refused_by_both(self) -> None:
        for path in self._fenced_controls():
            assert path == os.path.realpath(path), path
            assert security.is_sensitive_resolved_path(path) is True, path
            assert security.is_sensitive_path(path) is True, path

    def test_unfenced_canonical_controls_pass_both(self, tmp_path: Path) -> None:
        store = ArtifactStore(root=tmp_path / "artifacts")
        art = store.create(name="plain", content="<p>x</p>")
        meta = os.path.realpath(str(tmp_path / "artifacts" / art.slug / "meta.json"))
        assert os.path.exists(meta)
        for path in (meta, os.path.realpath(os.sep)):
            assert path == os.path.realpath(path), path
            assert security.is_sensitive_resolved_path(path) is False, path
            assert security.is_sensitive_path(path) is False, path


class TestTheDecisionMovedGatesNotVerdicts:
    """A fenced verdict from the pre-resolved gate still refuses each helper."""

    def test_a_fenced_verdict_refuses_each_helper_and_only_the_fenced_path(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        store = ArtifactStore(root=tmp_path / "artifacts")
        fenced = tmp_path / "files" / "fenced.txt"
        benign = tmp_path / "files" / "benign.txt"
        fenced.parent.mkdir()
        fenced.write_text("secret", encoding="utf-8")
        benign.write_text("public", encoding="utf-8")
        monkeypatch.setattr(
            security.paths,
            "is_sensitive_resolved_path",
            lambda p: p == os.path.realpath(str(fenced)),
        )

        with pytest.raises(ArtifactError, match="refusing to read sensitive path"):
            store._read_text(fenced)
        with pytest.raises(ArtifactError, match="refusing to read sensitive path"):
            store._read_bytes(fenced)
        with pytest.raises(ArtifactError, match="refusing to write sensitive path"):
            store._write_text(fenced, "overwritten")
        with pytest.raises(ArtifactError, match="refusing to write sensitive path"):
            store._write_bytes(fenced, b"overwritten")
        # Refused means untouched: the payload was not written, not even as a temp.
        assert fenced.read_text(encoding="utf-8") == "secret"
        assert not (fenced.parent / (fenced.name + ".tmp")).exists()

        # Conditional, not a blanket break: the sibling still reads and writes.
        assert store._read_text(benign) == "public"
        assert store._read_bytes(benign) == b"public"
        store._write_text(benign, "edited")
        assert benign.read_text(encoding="utf-8") == "edited"
        store._write_bytes(benign, b"bytes")
        assert benign.read_bytes() == b"bytes"


def _bounded_recorder(monkeypatch) -> list[str]:
    seen: list[str] = []
    real_gate = security.paths.is_sensitive_path

    def recording(path_str: str, base_dir=None) -> bool:
        seen.append(path_str)
        return real_gate(path_str, base_dir)

    monkeypatch.setattr(security.paths, "is_sensitive_path", recording)
    return seen


class TestTheFenceFollowsTheThread:
    """Off the loop the pre-resolved gate answers; on it the bounded gate does."""

    @staticmethod
    def _fixture(tmp_path: Path) -> tuple[ArtifactStore, Path, Path]:
        store = ArtifactStore(root=tmp_path / "artifacts")
        text_path = tmp_path / "files" / "note.txt"
        bytes_path = tmp_path / "files" / "blob.bin"
        text_path.parent.mkdir()
        text_path.write_text("hello", encoding="utf-8")
        bytes_path.write_bytes(b"\x00\x01")
        return store, text_path, bytes_path

    @pytest.mark.asyncio
    async def test_on_the_event_loop_the_bounded_gate_answers(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # A helper called synchronously from a coroutine runs ON the loop: the
        # pre-resolved gate would resolve its anchors inline there, so the
        # bounded gate must answer instead -- exactly as it did before.
        store, text_path, bytes_path = self._fixture(tmp_path)
        seen = _bounded_recorder(monkeypatch)
        monkeypatch.setattr(
            security.paths,
            "is_sensitive_resolved_path",
            lambda *a, **k: pytest.fail("the pre-resolved gate ran on the event loop"),
        )

        assert store._read_text(text_path) == "hello"
        assert store._read_bytes(bytes_path) == b"\x00\x01"
        store._write_text(text_path, "edited")
        store._write_bytes(bytes_path, b"\x02")

        assert len(seen) == 4, seen
        assert seen == [os.path.realpath(p) for p in seen]

    @pytest.mark.asyncio
    async def test_off_the_event_loop_the_pre_resolved_gate_answers(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # The same helpers on a worker thread (to_thread / run_in_executor) earn
        # the off-pool gate; the bounded gate is not reached.
        store, text_path, bytes_path = self._fixture(tmp_path)
        seen = _recorder(monkeypatch)
        monkeypatch.setattr(
            security.paths,
            "is_sensitive_path",
            lambda *a, **k: pytest.fail("the bounded gate ran on a worker thread"),
        )

        assert await asyncio.to_thread(store._read_text, text_path) == "hello"
        assert await asyncio.to_thread(store._read_bytes, bytes_path) == b"\x00\x01"
        await asyncio.to_thread(store._write_text, text_path, "edited")
        await asyncio.to_thread(store._write_bytes, bytes_path, b"\x02")

        assert len(seen) == 4, seen
        assert seen == [os.path.realpath(p) for p in seen]

    @pytest.mark.asyncio
    async def test_the_listing_handler_runs_the_store_off_the_loop(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # GET /api/artifacts is the caller that fills the pool: it must run
        # store.list() on a worker so the listing takes the off-pool gate.
        store = ArtifactStore(root=tmp_path / "artifacts")
        for i in range(3):
            store.create(name=f"art-{i}", content=f"<p>{i}</p>")
        monkeypatch.setattr(art_mod, "_default_store", store)
        loop_thread = threading.get_ident()
        list_threads: list[int] = []
        loop_running_in_list: list[bool] = []
        real_list = store.list

        def recording_list(**kwargs):
            list_threads.append(threading.get_ident())
            loop_running_in_list.append(on_event_loop())
            return real_list(**kwargs)

        monkeypatch.setattr(store, "list", recording_list)
        resolved_seen = _recorder(monkeypatch)
        bounded_seen = _bounded_recorder(monkeypatch)

        request = MagicMock()
        request.query = {}
        request.headers = {"X-Session-Key": "dashboard:test"}
        request.app = {"state": None}
        response = await art_handlers.api_artifacts_list(request)

        assert response.status == 200
        assert list_threads and all(t != loop_thread for t in list_threads), list_threads
        assert loop_running_in_list == [False] * len(list_threads)
        metas = [p for p in resolved_seen if p.endswith("meta.json")]
        assert len(metas) == 3, resolved_seen
        assert not [p for p in bounded_seen if p.endswith("meta.json")], bounded_seen


class TestReadsAreDescriptorPinned:
    @requires_symlinks
    def test_the_helper_refuses_a_file_link_at_the_final_component(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"
        target.write_bytes(b"secret")
        link = tmp_path / "link.txt"
        os.symlink(target, link)

        with pytest.raises(ArtifactError, match="refusing to read through a link"):
            art_mod._open_pinned_for_read(link)

    def test_the_helper_fences_the_real_path_of_the_opened_inode(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # The alias spelling stands for a stale realpath: the caller judged one
        # string, the kernel reports another for the inode actually opened.
        real = tmp_path / "real"
        real.mkdir()
        guarded = real / "guarded.txt"
        guarded.write_text("secret", encoding="utf-8")
        benign = real / "benign.txt"
        benign.write_text("public", encoding="utf-8")
        alias = tmp_path / "alias"
        make_dir_link(alias, real)
        guarded_real = os.path.realpath(guarded)
        seen: list[str] = []

        def fence(path: str) -> bool:
            seen.append(path)
            return path == guarded_real

        monkeypatch.setattr(security.paths, "is_sensitive_resolved_path", fence)

        with pytest.raises(ArtifactError, match="refusing to read sensitive path"):
            art_mod._open_pinned_for_read(alias / "guarded.txt")
        assert seen == [guarded_real]

        fd = art_mod._open_pinned_for_read(alias / "benign.txt")
        try:
            assert os.read(fd, 64) == b"public"
        finally:
            os.close(fd)

    def test_the_helper_does_not_ask_the_fence_for_a_plain_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        plain = tmp_path / "plain.txt"
        plain.write_text("public", encoding="utf-8")
        monkeypatch.setattr(
            security.paths,
            "is_sensitive_resolved_path",
            lambda *a, **k: pytest.fail("the helper re-asked the fence for a judged path"),
        )
        monkeypatch.setattr(
            security.paths,
            "is_sensitive_path",
            lambda *a, **k: pytest.fail("the helper re-asked the fence for a judged path"),
        )

        fd = art_mod._open_pinned_for_read(Path(os.path.realpath(plain)))
        try:
            assert os.read(fd, 64) == b"public"
        finally:
            os.close(fd)

    def test_the_helper_refuses_a_hardlinked_file(self, tmp_path: Path) -> None:
        target = tmp_path / "target.bin"
        target.write_bytes(b"secret")
        alias = tmp_path / "alias.bin"
        os.link(target, alias)
        assert target.stat().st_nlink > 1

        with pytest.raises(ArtifactError, match="refusing to read a hardlinked file"):
            art_mod._open_pinned_for_read(alias)

    def test_text_and_bytes_keep_normal_read_semantics(self, tmp_path: Path) -> None:
        store = ArtifactStore(root=tmp_path / "artifacts")
        text_path = tmp_path / "files" / "note.txt"
        newline_path = tmp_path / "files" / "newlines.txt"
        bytes_path = tmp_path / "files" / "blob.bin"
        text = "first\nsecond\n"
        data = b"\x00\x01\xff"

        store._write_text(text_path, text)
        store._write_bytes(newline_path, b"first\r\nsecond\rthird\n")
        store._write_bytes(bytes_path, data)

        assert store._read_text(text_path) == text
        # The stdlib is the oracle for newline translation on every platform.
        assert store._read_text(newline_path) == newline_path.read_text(encoding="utf-8")
        assert store._read_bytes(bytes_path) == data
        with pytest.raises(FileNotFoundError):
            store._read_text(tmp_path / "missing.txt")
        with pytest.raises(FileNotFoundError):
            store._read_bytes(tmp_path / "missing.bin")

    def test_the_helper_fails_closed_when_the_kernel_path_is_unreadable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        plain = tmp_path / "plain.txt"
        plain.write_text("public", encoding="utf-8")
        monkeypatch.setattr(art_mod.pinned_fs, "fd_real_path", lambda fd: None)

        with pytest.raises(ArtifactError, match="refusing to read an unverifiable file"):
            art_mod._open_pinned_for_read(Path(os.path.realpath(plain)))

    def test_both_store_readers_go_through_the_helper(self, tmp_path: Path, monkeypatch) -> None:
        store = ArtifactStore(root=tmp_path / "artifacts")
        text_path = tmp_path / "files" / "note.txt"
        bytes_path = tmp_path / "files" / "blob.bin"
        store._write_text(text_path, "public")
        store._write_bytes(bytes_path, b"\x00\x01")
        opened: list[str] = []
        real_open = art_mod._open_pinned_for_read

        def recording_open(resolved: Path) -> int:
            opened.append(str(resolved))
            return real_open(resolved)

        monkeypatch.setattr(art_mod, "_open_pinned_for_read", recording_open)

        assert store._read_text(text_path) == "public"
        assert store._read_bytes(bytes_path) == b"\x00\x01"
        assert opened == [os.path.realpath(text_path), os.path.realpath(bytes_path)]

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are a POSIX construct")
    def test_the_helper_refuses_a_non_regular_file(self, tmp_path: Path) -> None:
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)

        with pytest.raises(ArtifactError, match="refusing to read a non-regular file"):
            art_mod._open_pinned_for_read(fifo)
