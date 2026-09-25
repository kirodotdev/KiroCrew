# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The held no-follow chain walk.

Both routes are exercised here on one host. The walk itself is ordinary Python over
two platform primitives, so the by-name route is testable wherever those primitives
answer -- and on POSIX they do: ``open_entry_no_follow`` opens with ``O_NOFOLLOW`` and
``is_reparse_point_fd`` is always False. What is NOT testable off Windows is the
guarantee the held descriptors buy, because a handle that denies ``FILE_SHARE_DELETE``
is the mechanism blocking the rename a swap needs. The Windows CI shard covers that;
what these tests pin is the walk's verdict for every state a component can be in, and
that each verdict is reached without following anything.
"""

from __future__ import annotations

import errno
import os
import types

import pytest

from kiro_crew import pinned_fs, platform_compat

_DEEP = 255


def _walk(path, **kwargs):
    kwargs.setdefault("max_depth", _DEEP)
    return pinned_fs.hold_no_follow_chain(str(path), **kwargs)


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

    @pytest.mark.skipif(
        not pinned_fs.supports_chain_walk(), reason="the descriptor route needs openat"
    )
    def test_a_symlinked_component_is_reported_not_followed(self, tmp_path):
        """The descriptor route's link report. ``O_NOFOLLOW`` refuses the component, so
        the target is never opened and the walk names the link it stopped on."""
        target = tmp_path / "target"
        target.mkdir()
        (target / "doc.txt").write_text("payload", encoding="utf-8")
        alias = tmp_path / "alias"
        alias.symlink_to(target, target_is_directory=True)

        chain = _walk(alias / "doc.txt")
        try:
            assert chain.outcome == pinned_fs.CHAIN_REPARSE
            # The boundary stops ABOVE the link: the link itself is never proven, so a
            # caller cannot resolve through it.
            assert chain.held == str(tmp_path)
        finally:
            pinned_fs.close_all(chain.fds)

    @pytest.mark.skipif(
        not pinned_fs.supports_chain_walk(), reason="the descriptor route needs openat"
    )
    def test_a_symlinked_leaf_is_reported_not_followed(self, tmp_path):
        """The leaf is opened without ``O_DIRECTORY`` so an ordinary file works, but it
        still carries ``O_NOFOLLOW``: a link at the final name is reported too."""
        real = tmp_path / "real.txt"
        real.write_text("payload", encoding="utf-8")
        alias = tmp_path / "alias.txt"
        alias.symlink_to(real)

        chain = _walk(alias)
        try:
            assert chain.outcome == pinned_fs.CHAIN_REPARSE
            assert chain.held == str(tmp_path)
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
                patch.setattr(platform_compat, "is_reparse_point_fd", lambda _fd: True)
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

    def test_a_leaf_that_cannot_be_pinned_is_classified_and_becomes_the_boundary(self, tmp_path):
        """The LAST component, existing and refusing the pinning mask. Windows answers a
        sharing violation as ``EACCES``, so this is a file another process holds
        exclusively -- ordinary, and it must not fail validation. A resolution ends at
        this name rather than passing through it, so the walk re-opens it through a mask
        that takes no part in sharing, classifies it, and reports the boundary above it.
        The name is then re-attached as text by the caller and never resolved."""
        leaf = tmp_path / "a" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")
        anchor, components = pinned_fs._chain_components(str(leaf))
        real_open = platform_compat.open_entry_no_follow
        asked: list[tuple[str, bool]] = []

        def _exclusively_held(path, *, hold=True):
            asked.append((os.path.basename(str(path)), hold))
            if os.path.basename(str(path)) == "doc.txt" and hold:
                raise OSError(errno.EACCES, "sharing violation")
            return real_open(path)

        fds: list[int] = []
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(platform_compat, "open_entry_no_follow", _exclusively_held)
                chain = pinned_fs._hold_chain_by_path(anchor, components, fds)
            assert chain.outcome == pinned_fs.CHAIN_MISSING
            # The boundary is the parent: the leaf was classified, never pinned.
            assert chain.held == str(leaf.parent)
            # hold=False is asked for the leaf ALONE, and only after the pinning open
            # was refused. Every other component is opened to be pinned.
            assert asked[-3:] == [("a", True), ("doc.txt", True), ("doc.txt", False)]
            assert [name for name, hold in asked if not hold] == ["doc.txt"]
        finally:
            pinned_fs.close_all(fds)

    def test_an_interior_component_that_cannot_be_opened_refuses(self, tmp_path):
        """The finding this asymmetry exists for. A DACL-restricted junction planted at
        an INTERIOR component is denied to a direct open while a later traversal through
        it is not, so reporting a boundary above it would hand the caller a path whose
        remaining text names an object nothing has classified -- and the caller's own
        resolution follows it. The walk raises instead, and no classify-only retry is
        attempted, because a resolution passes through this name."""
        leaf = tmp_path / "a" / "b" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")
        anchor, components = pinned_fs._chain_components(str(leaf))
        real_open = platform_compat.open_entry_no_follow
        asked: list[tuple[str, bool]] = []

        def _interior_denied(path, *, hold=True):
            asked.append((os.path.basename(str(path)), hold))
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
            # No attribute-only retry for an interior component, and the walk stopped
            # there rather than going on to the leaf.
            assert asked[-2:] == [("a", True), ("b", True)]
            assert [name for name, hold in asked if not hold] == []
            assert "doc.txt" not in [name for name, _ in asked]
        finally:
            pinned_fs.close_all(fds)

    def test_an_unpinnable_leaf_that_is_a_link_still_refuses(self, tmp_path):
        """Classifying the leaf is the point of the retry, not a formality: a redirecting
        reparse point there is refused, because its name is what the caller opens."""
        leaf = tmp_path / "a" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")
        anchor, components = pinned_fs._chain_components(str(leaf))
        real_open = platform_compat.open_entry_no_follow

        def _exclusively_held(path, *, hold=True):
            if os.path.basename(str(path)) == "doc.txt" and hold:
                raise OSError(errno.EACCES, "sharing violation")
            return real_open(path)

        fds: list[int] = []
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(platform_compat, "open_entry_no_follow", _exclusively_held)
                patch.setattr(
                    platform_compat,
                    "is_reparse_point_fd",
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

        def _denied_both_ways(path, *, hold=True):
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


class TestRouteChoice:
    """Which route runs, and which answer decides it."""

    def test_the_walk_reads_its_own_probe_not_the_tree_walk_s(self, tmp_path, monkeypatch):
        """A host simulating a POSIX tree walk substitutes ``supports_pinned_walk``. This
        walk must not be carried along by that: on a host with no ``openat`` the
        descriptor route's anchor open is refused, and a validator resolving under it
        refuses every path it is handed."""
        leaf = tmp_path / "a" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")

        opened: list[str] = []
        real_open = platform_compat.open_entry_no_follow

        def _spy(path):
            opened.append(path)
            return real_open(path)

        def _forbidden():  # pragma: no cover
            raise AssertionError("the chain walk read the tree walk's probe")

        monkeypatch.setattr(pinned_fs, "supports_chain_walk", lambda: False)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", _forbidden)
        monkeypatch.setattr(platform_compat, "open_entry_no_follow", _spy)

        chain = _walk(leaf)
        try:
            assert chain.outcome == pinned_fs.CHAIN_HELD
            # Opening every component by its FULL path is what the by-name route does
            # and the descriptor route never does, so this is the route itself rather
            # than a side effect of it.
            _, components = pinned_fs._chain_components(str(leaf))
            assert len(opened) == len(components)
            assert opened[-1] == str(leaf)
        finally:
            pinned_fs.close_all(chain.fds)

    def test_a_host_without_openat_answers_no(self, monkeypatch):
        """The condition a Windows host meets. The probe asks for what this walk needs --
        ``O_NOFOLLOW`` and a descriptor-relative open -- and not for ``O_DIRECTORY``,
        which this walk must never pass."""
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            assert pinned_fs.supports_chain_walk() is True
        monkeypatch.setattr(os, "supports_dir_fd", frozenset())
        assert pinned_fs.supports_chain_walk() is False


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
    def test_it_answers_false_for_a_posix_descriptor(self, tmp_path):
        """On POSIX a descriptor cannot BE a link -- ``O_NOFOLLOW`` refuses one at the
        name -- so False is the correct answer rather than an unimplemented one."""
        f = tmp_path / "doc.txt"
        f.write_text("payload", encoding="utf-8")
        fd = platform_compat.open_entry_no_follow(str(f))
        try:
            assert platform_compat.is_reparse_point_fd(fd) is False
        finally:
            os.close(fd)

    @pytest.mark.skipif(not pinned_fs.supports_chain_walk(), reason="needs O_NOFOLLOW")
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


class TestReparseTagSource:
    r"""WHERE the classifier reads the reparse tag, which decides what it refuses.

    ``os.fstat`` does not carry one. CPython fills ``st_reparse_tag`` only on the
    path-based ``stat``/``lstat`` route, which queries ``FileAttributeTagInfo`` by name;
    the descriptor route builds its result from ``GetFileInformationByHandle``, which has
    no tag field. A classifier reading the tag there sees zero for every object and can
    only fall back to the bare ``FILE_ATTRIBUTE_REPARSE_POINT`` bit -- which Windows also
    sets on kinds that redirect NOTHING, so the fallback denies an ordinary OneDrive
    placeholder or WOF-backed file. These tests substitute both sources to pin that the
    answer comes from the handle.
    """

    #: Attribute bit set, tag reading zero: what a descriptor really reports on Windows.
    def _attributes_say_reparse(self, patch):
        patch.setattr(
            platform_compat,
            "os",
            types.SimpleNamespace(
                fstat=lambda _fd: types.SimpleNamespace(
                    st_file_attributes=platform_compat._WIN_FILE_ATTRIBUTE_REPARSE_POINT,
                    st_reparse_tag=0,
                )
            ),
        )

    @pytest.mark.parametrize(
        ("tag", "redirects", "kind"),
        [
            (0x9000_001A, False, "cloud placeholder"),
            (0x8000_0017, False, "WOF-backed file"),
            (0xA000_0003, True, "junction"),
            (0xA000_000C, True, "symlink"),
        ],
        ids=["cloud", "wof", "junction", "symlink"],
    )
    def test_the_tag_from_the_handle_decides(self, tmp_path, tag, redirects, kind):
        """Only a tag carrying the name-surrogate bit names another path, and only
        those may be refused. The two that decorate an object in place must classify
        as ordinary, because a walk that refuses them refuses normal local files."""
        fd = platform_compat.open_entry_no_follow(str(tmp_path))
        try:
            with pytest.MonkeyPatch.context() as patch:
                self._attributes_say_reparse(patch)
                patch.setattr(platform_compat, "_win_reparse_tag_from_fd", lambda _fd: tag)
                assert platform_compat.is_reparse_point_fd(fd) is redirects, kind
        finally:
            os.close(fd)

    def test_a_tag_that_cannot_be_read_raises(self, tmp_path):
        """An object that IS a reparse point while giving nothing to classify it by.
        The classifier raises rather than guessing either way, and the callers turn
        that into a refusal -- their existing answer for unreadable state."""
        fd = platform_compat.open_entry_no_follow(str(tmp_path))

        def _unreadable(_fd):
            raise OSError(errno.EIO, "tag query failed")

        try:
            with pytest.MonkeyPatch.context() as patch:
                self._attributes_say_reparse(patch)
                patch.setattr(platform_compat, "_win_reparse_tag_from_fd", _unreadable)
                with pytest.raises(OSError) as caught:
                    platform_compat.is_reparse_point_fd(fd)
            assert caught.value.errno == errno.EIO
        finally:
            os.close(fd)

    def test_the_tag_is_not_consulted_without_the_attribute_bit(self, tmp_path):
        """The cheap answer stays cheap, and stays first: an object with no reparse
        attribute is not a reparse point, and the handle query never runs for it."""
        fd = platform_compat.open_entry_no_follow(str(tmp_path))

        def _forbidden(_fd):  # pragma: no cover
            raise AssertionError("the tag was queried for a non-reparse object")

        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(platform_compat, "_win_reparse_tag_from_fd", _forbidden)
                assert platform_compat.is_reparse_point_fd(fd) is False
        finally:
            os.close(fd)
