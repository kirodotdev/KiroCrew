"""The builder's leaf reads borrow their no-follow refusal from the shared opener.

A reparse point at the final component has to be refused by the OPEN, not by a verdict taken
on the path beforehand. Where the flag that does that is absent -- ``os.O_NOFOLLOW`` is ``0``
on Windows -- an ``lstat``-then-``os.open`` pair is a check-to-open window, and on that
platform a junction naming a UNC share turns the read into an outbound SMB/NTLM exchange, so
the window is a credential surface. ``platform_compat.open_file_no_reparse`` carries the
refusal on both platforms (``O_NOFOLLOW`` on POSIX, a ``FILE_FLAG_OPEN_REPARSE_POINT`` handle
whose attributes are read off the descriptor on Windows), and these tests pin the three leaf
readers here to it.

The Windows branches are reached on POSIX by forcing ``_dir_fd_supported`` False, which is how
the rest of this suite exercises them. What cannot be simulated is the Windows OPEN itself, so
the property asserted is the one that is platform-independent: which authority the read
obtains its descriptor from, and that no by-name verdict is taken before it.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from kiro_crew import platform_compat

from .test_producer import load_build

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="uses a symlink to stand in for a junction, and POSIX is where the suite runs",
)


def _no_dir_fd():
    """Load the builder with ``_dir_fd_supported`` forced False: the no-``dir_fd`` branches."""
    return load_build(
        mutate=(
            '    return os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY")',
            "    return False",
        )
    )


def _spy_on_the_shared_opener(monkeypatch) -> list[str]:
    """Record every path the shared opener is asked for, and still open it for real."""
    seen: list[str] = []
    real = platform_compat.open_file_no_reparse

    def _record(path, *, nonblocking: bool = False) -> int:
        seen.append(str(path))
        return real(path, nonblocking=nonblocking)

    monkeypatch.setattr(platform_compat, "open_file_no_reparse", _record)
    return seen


# ---------------------------------------------------------------------------
# adoption: each leaf reader's descriptor comes from the shared opener
# ---------------------------------------------------------------------------
def test_the_leaf_text_read_opens_through_the_shared_opener(tmp_path, monkeypatch) -> None:
    """``_read_text_nofollow`` asks the shared opener for its descriptor."""
    mod = load_build()
    seen = _spy_on_the_shared_opener(monkeypatch)
    spec = tmp_path / "frontdesk.json"
    spec.write_bytes(b'{"name": "frontdesk"}\n')

    assert mod._read_text_nofollow(spec) == '{"name": "frontdesk"}\n'
    assert seen == [str(spec)], (
        "the leaf text read did not obtain its descriptor from "
        "platform_compat.open_file_no_reparse, so its no-follow refusal is whatever the local "
        "flags happen to be on this platform rather than the shared guarantee"
    )


def test_the_bytes_fallback_opens_through_the_shared_opener(tmp_path, monkeypatch) -> None:
    """On the no-``dir_fd`` branch ``_read_bytes_openat`` asks the shared opener too."""
    mod = _no_dir_fd()
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    signed = b'{"reviewed_by": "an owner"}\n'
    (root / "sub" / "plan.json").write_bytes(signed)
    seen = _spy_on_the_shared_opener(monkeypatch)

    assert mod._read_bytes_openat(root, pathlib.Path("sub/plan.json")) == signed
    assert seen == [str(root / "sub" / "plan.json")], (
        "the bytes reader's no-dir_fd fallback opened the leaf by name with local flags "
        "instead of the shared opener"
    )


def test_the_marker_fallback_opens_through_the_shared_opener(tmp_path, monkeypatch) -> None:
    """On the no-``dir_fd`` branch the staging-marker read asks the shared opener too."""
    mod = _no_dir_fd()
    marker = tmp_path / "bundle.staging.owned"
    marker.write_text(mod._STAGING_MARKER_BODY, encoding="utf-8")
    seen = _spy_on_the_shared_opener(monkeypatch)

    assert mod._marker_is_ours(marker) is True
    assert seen == [str(marker)], (
        "the marker read's no-dir_fd fallback read the path by name, so the verdict that "
        "authorises a recursive delete rests on an lstat taken before that read"
    )


# ---------------------------------------------------------------------------
# the refusal is the open's, so no by-name verdict precedes it
# ---------------------------------------------------------------------------
@_posix_only
def test_the_marker_fallback_refuses_a_redirect_without_a_path_check(tmp_path, monkeypatch) -> None:
    """A redirect at the marker path answers False, and no path predicate is consulted.

    The link's target holds a VALID marker body, so a read that followed it would answer True
    and authorise the recursive delete of a staging tree this build does not own.
    """
    mod = _no_dir_fd()

    def _must_not_run(_probe):
        raise AssertionError("the marker read must not judge the path before opening it")

    monkeypatch.setattr(mod, "_is_redirecting_entry", _must_not_run)
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text(mod._STAGING_MARKER_BODY, encoding="utf-8")
    marker = tmp_path / "bundle.staging.owned"
    marker.symlink_to(elsewhere)

    assert mod._marker_is_ours(elsewhere) is True, (
        "the fixture is wrong: the body written here is not one this build accepts, so the "
        "assertion below would pass whether or not the link was followed"
    )
    assert mod._marker_is_ours(marker) is False


# ---------------------------------------------------------------------------
# the shapes that are not a readable file
# ---------------------------------------------------------------------------
def test_a_directory_at_the_read_path_is_refused(tmp_path) -> None:
    """A directory is not text: the opener reports it and the reader answers None."""
    mod = load_build()
    assert mod._read_text_nofollow(tmp_path) is None


def test_a_missing_path_is_refused(tmp_path) -> None:
    """The ordinary absent-file case still answers None rather than raising."""
    mod = load_build()
    assert mod._read_text_nofollow(tmp_path / "absent.json") is None


# ---------------------------------------------------------------------------
# what the leaf opener does NOT cover, pinned so it is not mistaken for closed
# ---------------------------------------------------------------------------
@_posix_only
def test_an_intermediate_component_swapped_after_the_chain_check_is_still_followed(
    tmp_path, monkeypatch
) -> None:
    """PINS a remaining window: the leaf opener settles the LAST component only.

    On the no-``dir_fd`` branch the components above the leaf are judged by ``lstat`` and then
    the leaf is opened by a path built from them, so a directory swapped for a redirect after
    that walk is still traversed. ``_redirect_between`` is forced to answer None to stand in
    for a walk that ran before the swap. Closing this needs an open taken relative to a
    directory descriptor, which the platform lacking ``dir_fd`` does not offer at all -- the
    same absence the builder's entry-point guard refuses on.
    """
    mod = _no_dir_fd()
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "spec.json").write_text('{"name": "ATTACKER"}\n', encoding="utf-8")
    (root / "sub").rmdir()
    (root / "sub").symlink_to(elsewhere)
    monkeypatch.setattr(mod, "_redirect_between", lambda _root, _path: None)

    assert mod._read_text_openat(root, pathlib.Path("sub/spec.json")) == (
        '{"name": "ATTACKER"}\n'
    ), (
        "this pin records the CURRENT answer: an intermediate component swapped after the "
        "chain check is followed. If this now refuses, the descriptor-relative half landed "
        "and this pin plus the entry-point guard should be revisited together"
    )
