# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The held no-follow chain walk.

Both routes are exercised here on one host. The walk itself is ordinary Python over
two platform primitives, so the by-name route is testable wherever those primitives
answer -- and on POSIX they do: ``open_entry_no_follow`` opens with ``O_NOFOLLOW`` and
a symlink is refused at the open with ``ELOOP`` rather than classified. What is NOT testable off Windows is the
guarantee the held descriptors buy, because a handle that denies ``FILE_SHARE_DELETE``
is the mechanism blocking the rename a swap needs. The Windows CI shard covers that;
what these tests pin is the walk's verdict for every state a component can be in, and
that each verdict is reached without following anything.
"""

from __future__ import annotations

import errno
import os

import pytest

from kiro_crew import pinned_fs, platform_compat

_DEEP = 255

#: The real classifier, captured at import before any fixture can replace it, so
#: ``TestRealWindowsClassifier`` can prove the autouse stand-in left it in place.
_REAL_WIN_FD_IS_LINK = platform_compat.win_fd_is_link

#: The one class the autouse stand-in does not apply to: it runs the REAL classifier.
_REAL_CLASSIFIER_CLASS = "TestRealWindowsClassifier"


def _walk(path, **kwargs):
    kwargs.setdefault("max_depth", _DEEP)
    return pinned_fs.hold_no_follow_chain(str(path), **kwargs)


@pytest.fixture(autouse=True)
def _posix_safe_classifier(request, _floor_monkeypatch):
    """The walk now has a single route -- the Windows by-name one -- and it classifies
    each held descriptor with ``platform_compat.win_fd_is_link``, which calls
    ``ctypes.WinDLL`` and runs only on Windows. Exercising the walk off Windows needs a
    stand-in: default it to "not a link" so an ordinary chain holds, and let a test that
    is specifically about link detection override it. The POSIX descriptor route that
    once let these tests run their own primitives was deleted as dead production code
    (its only caller, ``hooks.validate_file_path``, reaches the walk only under
    ``os.name == "nt"``).

    Uses ``_floor_monkeypatch`` (not plain ``monkeypatch``), per D11: an autouse fixture
    that mutates a process global must own an undo stack independent of the test's, so
    the classifier stand-in's isolation is never coupled to a test's rollback.

    Not applied to ``TestRealWindowsClassifier``, which runs the real classifier on the
    Windows shard: that run is the evidence the stand-in cannot give.
    """
    if request.cls is not None and request.cls.__name__ == _REAL_CLASSIFIER_CLASS:
        return
    _floor_monkeypatch.setattr(platform_compat, "win_fd_is_link", lambda _fd: False)


class TestRealWindowsClassifier:
    """The real ``win_fd_is_link`` on a handle opened the way the walk opens it.

    Every other test here replaces the classifier, because it calls ``kernel32``. These
    run it unreplaced: on the Windows CI shard a normal temp file, opened attribute-only
    by ``open_entry_no_follow``, must read as not-a-link, and the walk must hold it.
    """

    def test_the_autouse_stand_in_leaves_the_real_classifier_here(self) -> None:
        assert platform_compat.win_fd_is_link is _REAL_WIN_FD_IS_LINK

    @pytest.mark.skipif(os.name != "nt", reason="the real classifier calls kernel32")
    def test_a_normal_file_opened_attribute_only_is_not_a_link(self, tmp_path) -> None:
        leaf = tmp_path / "doc.txt"
        leaf.write_text("payload", encoding="utf-8")
        fd = platform_compat.open_entry_no_follow(str(leaf))
        try:
            assert platform_compat.win_fd_is_link(fd) is False
            assert pinned_fs.fd_real_path(fd) is not None
        finally:
            os.close(fd)
        with pinned_fs.held_no_follow_chain(str(leaf), max_depth=_DEEP) as chain:
            assert chain.outcome == pinned_fs.CHAIN_HELD


def test_the_autouse_classifiers_patch_through_the_floor() -> None:
    """D11 regression (GPT 6.1): both autouse ``_posix_safe_classifier`` fixtures must
    mutate the ``win_fd_is_link`` process global through ``_floor_monkeypatch``, not the
    test's own ``monkeypatch``. An autouse fixture sharing the test's undo stack couples
    classifier isolation to test rollback (prohibited by D11, ``tests-are-deterministic``).
    Pinned structurally -- each fixture must declare ``_floor_monkeypatch`` as a
    dependency -- so a revert to plain ``monkeypatch`` fails here rather than silently
    re-coupling the stacks.
    """
    import inspect

    module_fixture = _posix_safe_classifier
    class_fixture = TestByNameRoute._posix_safe_classifier
    for fixture in (module_fixture, class_fixture):
        params = set(inspect.signature(fixture.__wrapped__).parameters)
        assert "_floor_monkeypatch" in params, (
            f"{fixture.__wrapped__.__qualname__} must patch through _floor_monkeypatch "
            "(D11), not the test's own monkeypatch"
        )
        assert "monkeypatch" not in params, (
            f"{fixture.__wrapped__.__qualname__} still takes the plain monkeypatch -- "
            "its undo would race the floor's teardown"
        )


class TestOutcomes:
    def test_a_whole_real_chain_is_held(self, tmp_path):
        """Every component exists and none is a link, so the walk reaches the leaf and
        holds one descriptor per component it proved."""
        leaf = tmp_path / "a" / "b" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")

        chain = _walk(leaf)
        try:
            assert chain.outcome == pinned_fs.CHAIN_HELD
            assert chain.held == str(leaf)
            assert chain.fds
        finally:
            pinned_fs.close_all(chain.fds)

    def test_a_missing_leaf_stops_the_walk_without_refusing(self, tmp_path):
        """The shape every write caller hands in. The name holds nothing, so nothing
        below it can redirect a resolution, and the walk reports the deepest name it
        did prove."""
        chain = _walk(tmp_path / "not-created-yet.txt")
        try:
            assert chain.outcome == pinned_fs.CHAIN_MISSING
            assert chain.held == str(tmp_path)
        finally:
            pinned_fs.close_all(chain.fds)

    def test_a_file_part_way_along_the_path_reads_as_missing(self, tmp_path):
        """A regular file cannot carry the rest of the path, so the components under it
        name nothing -- the same fact as a missing component, not a failure."""
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")

        chain = _walk(blocker / "below" / "doc.txt")
        try:
            assert chain.outcome == pinned_fs.CHAIN_MISSING
        finally:
            pinned_fs.close_all(chain.fds)

    def test_a_relative_path_is_refused(self, tmp_path, monkeypatch):
        """A relative path's components resolve against a current directory the walk
        never inspected, so there is no chain for it to hold."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError):
            _walk("doc.txt")

    def test_a_path_deeper_than_the_bound_is_refused_before_the_walk(self, tmp_path):
        """One open per component makes an adversarially deep path a stall inside the
        guard, so the depth is judged before any of them run."""
        deep = os.sep + os.sep.join("a" for _ in range(_DEEP + 1))
        with pytest.raises(ValueError):
            _walk(deep, max_depth=_DEEP)


class TestByNameRoute:
    """The route Windows takes, exercised here through the POSIX primitives it uses."""

    @pytest.fixture(autouse=True)
    def _posix_safe_classifier(self, _floor_monkeypatch):
        """The by-name route classifies each held descriptor with
        ``platform_compat.win_fd_is_link``, which calls ``ctypes.WinDLL`` and only runs
        on Windows. Exercising this route on POSIX needs a stand-in: default it to
        "not a link" so an ordinary chain holds, and let a test that is specifically
        about link detection override it.

        Uses ``_floor_monkeypatch`` (not plain ``monkeypatch``), per D11: an autouse
        fixture mutating a process global owns an undo stack independent of the test's.
        """
        _floor_monkeypatch.setattr(platform_compat, "win_fd_is_link", lambda _fd: False)

    def test_it_holds_a_real_chain(self, tmp_path):
        leaf = tmp_path / "a" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")
        anchor, components = pinned_fs._chain_components(str(leaf))

        fds: list[int] = []
        try:
            chain = pinned_fs._hold_chain_by_path(anchor, components, fds)
            assert chain.outcome == pinned_fs.CHAIN_HELD
            # The anchor is not opened: a drive or share root cannot be a link, and on
            # a share the open would be one more round-trip to an admitted host.
            assert len(chain.fds) == len(components)
        finally:
            pinned_fs.close_all(fds)

    def test_a_descriptor_reported_as_a_reparse_point_stops_the_walk(self, tmp_path):
        """The Windows link report, which is a question asked of the DESCRIPTOR. The
        classifier answers False on every POSIX descriptor by design, so the walk's
        handling of a True is pinned by substituting it."""
        leaf = tmp_path / "a" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")
        anchor, components = pinned_fs._chain_components(str(leaf))

        fds: list[int] = []
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(platform_compat, "win_fd_is_link", lambda _fd: True)
                chain = pinned_fs._hold_chain_by_path(anchor, components, fds)
            assert chain.outcome == pinned_fs.CHAIN_REPARSE
            # The boundary is the ANCHOR, so the walk stopped at the first component
            # rather than reading past it -- which is the property, not a detail.
            assert chain.held == anchor
        finally:
            pinned_fs.close_all(fds)

    def test_a_missing_component_stops_the_walk(self, tmp_path):
        anchor, components = pinned_fs._chain_components(str(tmp_path / "absent" / "doc.txt"))
        fds: list[int] = []
        try:
            chain = pinned_fs._hold_chain_by_path(anchor, components, fds)
            assert chain.outcome == pinned_fs.CHAIN_MISSING
        finally:
            pinned_fs.close_all(fds)

    def test_a_leaf_held_exclusively_by_another_process_still_opens(self, tmp_path):
        """The LAST component, held exclusively by another process. The walk asks for a
        single attribute-only mask, which takes no part in Windows sharing, so a leaf
        another process holds exclusively is NOT refused -- it opens, is classified off
        its own descriptor, and the walk holds the whole chain. This is the base
        comparison the Windows path now falls back to: no traverse probe that a share
        mode or ACL could refuse."""
        leaf = tmp_path / "a" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")
        anchor, components = pinned_fs._chain_components(str(leaf))
        real_open = platform_compat.open_entry_no_follow
        asked: list[str] = []

        def _attribute_only(path):
            asked.append(os.path.basename(str(path)))
            return real_open(path)

        fds: list[int] = []
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(platform_compat, "open_entry_no_follow", _attribute_only)
                chain = pinned_fs._hold_chain_by_path(anchor, components, fds)
            # The whole chain is held: the leaf opened for attribute-only access.
            assert chain.outcome == pinned_fs.CHAIN_HELD
            # Every component is opened exactly once, with no second (retry) open.
            assert asked[-2:] == ["a", "doc.txt"]
            assert asked.count("doc.txt") == 1
        finally:
            pinned_fs.close_all(fds)

    def test_an_interior_component_that_cannot_be_opened_refuses(self, tmp_path):
        """The finding this exists for. A DACL-restricted junction planted at an INTERIOR
        component is denied to a direct open while a later traversal through it is not,
        so reporting a boundary above it would hand the caller a path whose remaining
        text names an object nothing has classified -- and the caller's own resolution
        follows it. The walk raises instead: a component it cannot open is one it cannot
        classify, and there is no weaker mask to retry."""
        leaf = tmp_path / "a" / "b" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")
        anchor, components = pinned_fs._chain_components(str(leaf))
        real_open = platform_compat.open_entry_no_follow
        asked: list[str] = []

        def _interior_denied(path):
            asked.append(os.path.basename(str(path)))
            if os.path.basename(str(path)) == "b":
                raise OSError(errno.EACCES, "access denied")
            return real_open(path)

        fds: list[int] = []
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(platform_compat, "open_entry_no_follow", _interior_denied)
                with pytest.raises(OSError) as caught:
                    pinned_fs._hold_chain_by_path(anchor, components, fds)
            assert caught.value.errno == errno.EACCES
            # The walk stopped at the denied component rather than going on to the leaf.
            assert asked[-2:] == ["a", "b"]
            assert "doc.txt" not in asked
        finally:
            pinned_fs.close_all(fds)

    def test_a_leaf_that_is_a_link_still_refuses(self, tmp_path):
        """Classifying the leaf is the point: a redirecting reparse point there is
        refused, because its name is what the caller opens. The attribute-only mask
        opens the reparse point ITSELF (never following it), so the leaf is classified
        off its own descriptor in the main walk path and reported as a reparse."""
        leaf = tmp_path / "a" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")
        anchor, components = pinned_fs._chain_components(str(leaf))

        fds: list[int] = []
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(
                    platform_compat,
                    "win_fd_is_link",
                    lambda fd: os.fstat(fd).st_ino == os.stat(leaf).st_ino,
                )
                chain = pinned_fs._hold_chain_by_path(anchor, components, fds)
            assert chain.outcome == pinned_fs.CHAIN_REPARSE
        finally:
            pinned_fs.close_all(fds)

    def test_a_leaf_denied_even_attribute_access_refuses(self, tmp_path):
        """Nothing could be learned about the object, so there is nothing to report a
        boundary about. The error propagates and the caller fails closed."""
        leaf = tmp_path / "a" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")
        anchor, components = pinned_fs._chain_components(str(leaf))
        real_open = platform_compat.open_entry_no_follow

        def _denied_both_ways(path):
            if os.path.basename(str(path)) == "doc.txt":
                raise OSError(errno.EACCES, "access denied")
            return real_open(path)

        fds: list[int] = []
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(platform_compat, "open_entry_no_follow", _denied_both_ways)
                with pytest.raises(OSError) as caught:
                    pinned_fs._hold_chain_by_path(anchor, components, fds)
            assert caught.value.errno == errno.EACCES
        finally:
            pinned_fs.close_all(fds)

    def test_any_other_open_failure_propagates(self, tmp_path):
        """A component that fails for a reason the walk has no reading of -- an I/O
        error, an unreachable host -- is not a boundary. Nothing is known about what
        sits there, so the walk raises and its caller fails closed."""
        leaf = tmp_path / "a" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")
        anchor, components = pinned_fs._chain_components(str(leaf))

        def _broken(_path):
            raise OSError(errno.EIO, "input/output error")

        fds: list[int] = []
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(platform_compat, "open_entry_no_follow", _broken)
                with pytest.raises(OSError) as caught:
                    pinned_fs._hold_chain_by_path(anchor, components, fds)
            assert caught.value.errno == errno.EIO
        finally:
            pinned_fs.close_all(fds)


class TestHeldContextManager:
    def test_it_releases_every_descriptor(self, tmp_path):
        """The guarantee lasts exactly as long as the descriptors, so the block is where
        a caller resolves -- and leaving it must not leak a held component."""
        leaf = tmp_path / "a" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")

        with pinned_fs.held_no_follow_chain(str(leaf), max_depth=_DEEP) as chain:
            assert chain.outcome == pinned_fs.CHAIN_HELD
            held = chain.fds
            for fd in held:
                assert os.fstat(fd) is not None

        for fd in held:
            with pytest.raises(OSError):
                os.fstat(fd)

    def test_it_releases_them_when_the_block_raises(self, tmp_path):
        leaf = tmp_path / "doc.txt"
        leaf.write_text("payload", encoding="utf-8")

        with pytest.raises(RuntimeError):
            with pinned_fs.held_no_follow_chain(str(leaf), max_depth=_DEEP) as chain:
                held = chain.fds
                raise RuntimeError("caller failed mid-resolution")

        for fd in held:
            with pytest.raises(OSError):
                os.fstat(fd)


class TestReparseClassifier:
    @pytest.mark.skipif(
        os.name == "nt" or not hasattr(os, "O_NOFOLLOW"),
        reason="O_NOFOLLOW link refusal is the POSIX path",
    )
    def test_the_opener_refuses_a_symlink_on_posix(self, tmp_path):
        """How a link is reported where ``OPEN_REPARSE_POINT`` does not exist: the open
        itself fails, so no descriptor for the link is ever handed back."""
        real = tmp_path / "real.txt"
        real.write_text("payload", encoding="utf-8")
        alias = tmp_path / "alias.txt"
        alias.symlink_to(real)

        with pytest.raises(OSError) as caught:
            platform_compat.open_entry_no_follow(str(alias))
        assert caught.value.errno == errno.ELOOP

    def test_the_opener_returns_a_descriptor_for_a_directory(self, tmp_path):
        """A walk needs the interior components too, so the opener must not refuse a
        directory the way the typed leaf opener does."""
        fd = platform_compat.open_entry_no_follow(str(tmp_path))
        try:
            assert os.fstat(fd).st_ino == os.stat(tmp_path).st_ino
        finally:
            os.close(fd)


def _screen_canonical(path):
    """``screen_held_file_kind``'s canonical path, or ``None`` when it refuses."""
    got = pinned_fs.screen_held_file_kind(path)
    return None if got is None else got.canonical


class TestScreenHeldFileKindCanonical:
    """The canonical path ``screen_held_file_kind`` reports for each walk outcome, and
    its refusals, on the branches ``TestScreenHeldFileKind`` does not drive. The
    ``win_fd_is_link`` stand-in (autouse fixture above) lets the Windows-only walk run on
    this host; ``TestRealWindowsClassifier`` runs the real classifier on Windows.
    """

    def test_a_real_chain_returns_its_held_canonical_path(self, tmp_path):
        """No component is a link, so the screen returns the leaf's descriptor-canonical
        path rather than refusing -- the value the caller then gates by name."""
        leaf = tmp_path / "a" / "b" / "c.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("x", encoding="utf-8")
        got = _screen_canonical(str(leaf))
        assert got is not None
        assert os.path.realpath(got) == os.path.realpath(str(leaf))

    def test_a_missing_leaf_is_not_a_refusal(self, tmp_path):
        """A validated path routinely does not exist yet: the proven prefix is
        canonicalised through its held descriptor and the unproven tail re-attached as
        text, rather than refused."""
        base = tmp_path / "a"
        base.mkdir()
        target = base / "does-not-exist.txt"
        got = _screen_canonical(str(target))
        assert got is not None
        assert os.path.normpath(got) == os.path.normpath(str(target))

    def test_a_relative_path_is_refused(self, tmp_path, monkeypatch):
        """The walk refuses a relative path (``ValueError``); the screen turns that into
        a ``None`` refusal rather than letting it raise into the caller."""
        monkeypatch.chdir(tmp_path)
        assert _screen_canonical("a/b/c.txt") is None

    def test_a_dotdot_in_a_windows_spelling_is_refused(self):
        assert _screen_canonical("C:\\ws\\missing\\..\\junc\\img.png") is None

    @staticmethod
    def _held(monkeypatch, outcome, fds, held):
        """Make ``held_no_follow_chain`` yield a crafted ``HeldChain`` so the branch
        taken by ``screen_held_file_kind`` is driven directly, without needing a
        real Windows filesystem state the POSIX host cannot stage."""
        import contextlib

        chain = pinned_fs.HeldChain(outcome, tuple(fds), held)

        @contextlib.contextmanager
        def _fake(_path, *, max_depth):
            yield chain

        monkeypatch.setattr(pinned_fs, "held_no_follow_chain", _fake)

    def test_a_bare_anchor_held_with_no_descriptor_canonicalises_by_name(self, monkeypatch):
        """``CHAIN_HELD`` with no descriptor is a bare drive/share root -- it cannot be a
        reparse point, so it is canonicalised by name rather than refused."""
        self._held(monkeypatch, pinned_fs.CHAIN_HELD, [], "C:\\")
        assert _screen_canonical("C:\\") == os.path.realpath("C:\\")

    def test_a_missing_tail_that_escapes_the_boundary_is_refused(self, tmp_path, monkeypatch):
        """``CHAIN_MISSING`` whose remainder is not below the proven boundary is a
        contradiction a walk of this string cannot produce, so it fails closed."""
        # held is a child of path, so relpath(path, held) climbs out with '..'.
        self._held(
            monkeypatch,
            pinned_fs.CHAIN_MISSING,
            [],
            str(tmp_path / "a" / "b"),
        )
        assert _screen_canonical(str(tmp_path / "a")) is None

    def test_a_missing_anchor_only_prefix_canonicalises_by_name(self, tmp_path, monkeypatch):
        """``CHAIN_MISSING`` with no descriptor (the walk proved no component) uses the
        anchor's by-name realpath for the proven prefix and re-attaches the tail."""
        anchor = str(tmp_path)
        target = str(tmp_path / "nope" / "leaf.txt")
        self._held(monkeypatch, pinned_fs.CHAIN_MISSING, [], anchor)
        got = _screen_canonical(target)
        assert got == os.path.normpath(
            os.path.join(os.path.realpath(anchor), os.path.relpath(target, anchor))
        )

    def test_an_unreadable_held_descriptor_falls_back_to_legacy(self, tmp_path, monkeypatch):
        """Design ruling: when the handle's final path cannot be read (``fd_real_path``
        -> ``None``) the screen does NOT fail closed -- that is the same unverifiable hop
        as an interior-open failure (``GetFinalPathNameByHandleW`` returns nothing on
        some volumes/redirectors where CPython ``realpath`` resolves by name). It falls
        back to the legacy by-name screen for that path, admitting it when no component
        is a link."""
        leaf = str(tmp_path / "leaf")
        self._held(monkeypatch, pinned_fs.CHAIN_HELD, [7], leaf)
        monkeypatch.setattr(pinned_fs, "fd_real_path", lambda _fd: None)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _p: False)
        got = _screen_canonical(leaf)
        assert got is not None
        assert os.path.realpath(got) == os.path.realpath(leaf)

    def test_an_unreadable_held_descriptor_fallback_still_refuses_a_link(
        self, tmp_path, monkeypatch
    ):
        """The fail-to-legacy fallback for an unreadable final path is the STRICT
        refuse-any-link screen, so a linked ancestor still refuses -- no weaker than
        base."""
        leaf = str(tmp_path / "leaf")
        self._held(monkeypatch, pinned_fs.CHAIN_HELD, [7], leaf)
        monkeypatch.setattr(pinned_fs, "fd_real_path", lambda _fd: None)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: str(tmp_path))
        assert _screen_canonical(leaf) is None

    def test_a_missing_prefix_with_unreadable_descriptor_falls_back_to_legacy(
        self, tmp_path, monkeypatch
    ):
        """``CHAIN_MISSING`` with a held descriptor whose final path is unreadable is the
        same fail-to-legacy hop: it falls back to the legacy by-name screen rather than
        refusing."""
        anchor = str(tmp_path / "a")
        (tmp_path / "a").mkdir()
        target = str(tmp_path / "a" / "leaf.txt")
        self._held(monkeypatch, pinned_fs.CHAIN_MISSING, [7], anchor)
        monkeypatch.setattr(pinned_fs, "fd_real_path", lambda _fd: None)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _p: False)
        got = _screen_canonical(target)
        assert got is not None
        assert os.path.realpath(got) == os.path.realpath(target)


class TestScreenHeldFileKind:
    """``screen_held_file_kind`` answers the leaf's kind THROUGH the held descriptor,
    so a consumer never re-probes by name after the hold closes -- the window GPT 6.1
    flagged (a leaf swapped to a UNC junction between the screen and a by-name
    ``is_file()`` would send an SMB auth). The kind here comes from ``os.fstat`` on the
    held leaf descriptor, immune to a name swap.
    """

    @staticmethod
    def _held(monkeypatch, outcome, fds, held):
        import contextlib

        chain = pinned_fs.HeldChain(outcome, tuple(fds), held)

        @contextlib.contextmanager
        def _fake(_path, *, max_depth):
            yield chain

        monkeypatch.setattr(pinned_fs, "held_no_follow_chain", _fake)

    def test_a_regular_file_reports_is_regular_from_the_descriptor(self, tmp_path):
        leaf = tmp_path / "a.txt"
        leaf.write_text("hello", encoding="utf-8")
        got = pinned_fs.screen_held_file_kind(str(leaf))
        assert got is not None
        assert got.exists and got.is_regular and not got.is_dir
        assert got.size == 5
        assert os.path.realpath(got.canonical) == os.path.realpath(str(leaf))

    def test_a_directory_reports_is_dir(self, tmp_path):
        d = tmp_path / "sub"
        d.mkdir()
        got = pinned_fs.screen_held_file_kind(str(d))
        assert got is not None
        assert got.is_dir and not got.is_regular

    def test_a_reparse_component_refuses(self, tmp_path, monkeypatch):
        self._held(monkeypatch, pinned_fs.CHAIN_REPARSE, [], str(tmp_path))
        assert pinned_fs.screen_held_file_kind(str(tmp_path / "x")) is None

    def test_a_dotdot_component_is_refused(self, tmp_path):
        assert pinned_fs.screen_held_file_kind(str(tmp_path / ".." / "x")) is None

    def test_a_missing_leaf_reports_not_existing(self, tmp_path):
        got = pinned_fs.screen_held_file_kind(str(tmp_path / "nope.txt"))
        assert got is not None
        assert not got.exists and not got.is_regular and not got.is_dir

    def test_the_kind_comes_from_the_descriptor_not_a_by_name_stat(self, tmp_path, monkeypatch):
        """A regression that re-stats the canonical path by name would be caught here:
        the leaf's own fd reports regular, and no by-name stat is consulted."""
        leaf = tmp_path / "b.txt"
        leaf.write_text("xy", encoding="utf-8")

        def _boom(*_a, **_k):  # pragma: no cover
            raise AssertionError("a by-name stat ran instead of fstat on the held fd")

        # os.stat by name must not be the source of the verdict (fstat is).
        monkeypatch.setattr(os, "stat", _boom)
        got = pinned_fs.screen_held_file_kind(str(leaf))
        assert got is not None and got.is_regular and got.size == 2

    def test_interior_open_error_falls_back_to_legacy_with_kind(self, tmp_path, monkeypatch):
        import contextlib

        leaf = tmp_path / "c.txt"
        leaf.write_text("z", encoding="utf-8")

        @contextlib.contextmanager
        def _boom(_path, *, max_depth):
            raise pinned_fs.ChainInteriorOpenError(13, "Permission denied", str(leaf))
            yield  # pragma: no cover

        monkeypatch.setattr(pinned_fs, "held_no_follow_chain", _boom)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _p: False)
        got = pinned_fs.screen_held_file_kind(str(leaf))
        assert got is not None and got.is_regular
        assert os.path.realpath(got.canonical) == os.path.realpath(str(leaf))

    def test_fallback_refuses_a_linked_ancestor(self, tmp_path, monkeypatch):
        import contextlib

        leaf = tmp_path / "d.txt"
        leaf.write_text("z", encoding="utf-8")

        @contextlib.contextmanager
        def _boom(_path, *, max_depth):
            raise pinned_fs.ChainInteriorOpenError(13, "Permission denied", str(leaf))
            yield  # pragma: no cover

        monkeypatch.setattr(pinned_fs, "held_no_follow_chain", _boom)
        monkeypatch.setattr(platform_compat, "first_linked_ancestor", lambda _p: str(tmp_path))
        assert pinned_fs.screen_held_file_kind(str(leaf)) is None


class TestRefuseAnyLinkSurfacesUseTheHeldScreen:
    """One test per consumer site that the ruling named: each must route its Windows
    link screen through the held screen (``screen_held_file_kind``) so a reparse
    ANYWHERE in the chain refuses, with no
    second by-name resolve in between. Each test forces the held screen to refuse
    (``None``) and asserts the surface refuses too. The held screen's own correctness
    is pinned above.
    """

    def test_themes_resolve_local_source_refuses_on_held_screen(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import themes

        monkeypatch.setattr(themes, "IS_WINDOWS", True)
        monkeypatch.setattr(themes, "is_unc_shape", lambda _c: False)
        monkeypatch.setattr(themes, "screen_held_file_kind", lambda _p: None)
        d = tmp_path / "theme"
        d.mkdir()
        resolved, err = themes._resolve_local_source(str(d))
        assert resolved is None
        assert err == "local path must not be a symlink"

    def test_image_artifacts_local_image_refuses_on_held_screen(self, tmp_path, monkeypatch):
        from kiro_crew import image_artifacts

        f = tmp_path / "pic.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        monkeypatch.setattr(image_artifacts.os, "name", "nt")
        monkeypatch.setattr(image_artifacts, "local_destination", lambda _r: f)
        monkeypatch.setattr(image_artifacts, "screen_held_file_kind", lambda _p: None)
        assert image_artifacts._local_file(str(f)) is None

    def test_outbound_files_inspect_refuses_on_held_screen(self, tmp_path, monkeypatch):
        from kiro_crew.messaging import outbound_files

        f = tmp_path / "pic.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        monkeypatch.setattr(outbound_files.os, "name", "nt")
        monkeypatch.setattr(outbound_files, "screen_held_file_kind", lambda _p: None)
        rej = outbound_files._inspect(
            str(f),
            f,
            str(f),
            budget=1 << 20,
            max_file_bytes=1 << 20,
            within_root=str(tmp_path),
        )
        assert isinstance(rej, outbound_files.Rejection)
        assert rej.reason == outbound_files.REASON_SYMLINK

    def test_prompt_blocks_skips_image_on_held_screen(self, monkeypatch):
        from kiro_crew.acp import prompt_blocks

        monkeypatch.setattr(prompt_blocks.os, "name", "nt")
        monkeypatch.setattr(prompt_blocks, "screen_held_file_kind", lambda _p: None)
        # A path that linkifies to an image under nt but screens as a link: the block
        # builder must leave it as text (no image block) rather than stat it by name.
        blocks = prompt_blocks.build_prompt_blocks("see C:\\pics\\a.png")
        assert not any(b.get("type") == "image" for b in blocks)
