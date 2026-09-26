"""A private window inside a CALLER's own mask is enforced on both backends.

``extra_private_dirs`` re-exposes one directory inside a masked tree read-write
without lifting the mask: the process keeps its own state, every sibling stays
hidden, and a directory created in the tree AFTER the profile was built is
covered because the mask is over the whole tree rather than per leaf.

The primitive is honoured for TIER-masked trees and for a CALLER-masked tree
(``extra_hidden_dirs``) on both backends, and each builder reaches that the same
way: ``_build_launcher_script`` extends ``hidden_dirs`` with
``extra_hidden_dirs`` before it computes ``_private_window_spellings``, and
``_build_seatbelt_profile`` computes its windows against the caller's targets as
well as the tier list before it emits blanket denies over them. A builder that
omitted the caller's targets would swallow the window: the child loses read AND
write on its own directory. Fail-closed, so the spawn breaks rather than leaking,
but it leaves the primitive enforced on one platform only for exactly the shape
that needs it.

That shape is the durable-data view for an app-bundle cron script: mask the whole
``apps/`` ancestor so no app's ``.app_secret`` is reachable -- including an app
installed while a long-running script executes -- and keep ``apps/<app>/data``
live at its real path on its real inode, so provisioned dependencies
(``data/.kirocrew-deps``, whose swap renames require one filesystem) and logs
survive the run instead of landing in a tree that is deleted afterwards.

Every assertion here is lexical, over the two builders' output. No test in this
repo executes ``sandbox-exec`` or ``unshare``, so these pin the POLICY the
builders emit, which is what the two backends were disagreeing about.
"""

from __future__ import annotations

import json
import os
import re

import pytest

from kiro_crew import sandbox

_HOME = os.path.expanduser("~")
_APPS = os.path.join(_HOME, ".kiro", "crew", "apps")
_BUNDLE = os.path.join(_APPS, "demo-app")
_DATA = os.path.join(_BUNDLE, "data")
_OWN_SECRET = os.path.join(_BUNDLE, ".app_secret")
_SIBLING = os.path.join(_APPS, "other-app")
_SIBLING_SECRET = os.path.join(_SIBLING, ".app_secret")


def _seatbelt(**kwargs: object) -> list[str]:
    profile = sandbox._build_seatbelt_profile("cc", **kwargs)  # type: ignore[arg-type]
    return profile.splitlines()


def _rules_for(lines: list[str], operation: str, target: str) -> list[str]:
    return [ln for ln in lines if operation in ln and f"(subpath {json.dumps(target)})" in ln]


def _denied(path: str, hidden: list[str], windows: list[str]) -> bool:
    """The launcher child's effective verdict for *path*.

    Mirrors the launcher's own mount order: a masked tree is replaced by an
    empty directory, then each window is bound back at its real path. So a path
    is reachable exactly when it is inside a window, and denied when it is
    inside a masked tree and inside no window.
    """
    inside_mask = any(path == h or path.startswith(h.rstrip(os.sep) + os.sep) for h in hidden)
    inside_window = any(path == w or path.startswith(w.rstrip(os.sep) + os.sep) for w in windows)
    return inside_mask and not inside_window


def _launcher_view(**kwargs: object) -> tuple[list[str], list[str], list[str]]:
    script = sandbox._build_launcher_script("cc", **kwargs)  # type: ignore[arg-type]
    hidden = json.loads(re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S).group(1))
    files = json.loads(re.search(r"SENSITIVE_FILES = (\[.*?\])\n", script, re.S).group(1))
    windows = json.loads(re.search(r"PRIVATE_DIRS = (\[.*?\])\n", script, re.S).group(1))
    return hidden, files, windows


@pytest.mark.skipif(os.name == "nt", reason="Seatbelt profile only")
class TestSeatbeltHonoursAWindowInsideACallerMask:
    def test_the_tree_is_denied_except_the_window_in_every_direction(self) -> None:
        lines = _seatbelt(extra_hidden_dirs=(_APPS,), extra_private_dirs=(_DATA,))
        except_window = f"(require-not (subpath {json.dumps(_DATA)}))"
        for operation in ("file-read*", "file-write*", "file-link"):
            matching = _rules_for(lines, operation, _APPS)
            assert matching, operation
            assert all(ln.lstrip().startswith("(deny") for ln in matching), operation
            assert any(except_window in ln for ln in matching), operation

    def test_the_window_is_writable_not_merely_readable(self) -> None:
        """The deps swap renames write INTO the window, so a read-only
        exception (the shape ``extra_expose_files`` gets) would not do."""
        lines = _seatbelt(extra_hidden_dirs=(_APPS,), extra_private_dirs=(_DATA,))
        except_window = f"(require-not (subpath {json.dumps(_DATA)}))"
        for operation in ("file-write*", "file-link"):
            blanket = [ln for ln in _rules_for(lines, operation, _APPS) if except_window not in ln]
            assert blanket == [], (operation, blanket)

    def test_a_sibling_app_in_the_masked_tree_gets_no_exception(self) -> None:
        lines = _seatbelt(extra_hidden_dirs=(_APPS,), extra_private_dirs=(_DATA,))
        assert not any(_SIBLING in ln and "require-not" in ln for ln in lines)
        allows = [ln.strip() for ln in lines if ln.lstrip().startswith("(allow") and _APPS in ln]
        assert allows == [
            f"(allow file-read-metadata (literal {json.dumps(_BUNDLE)}))",
            f"(allow file-read-metadata (literal {json.dumps(_APPS)}))",
        ], allows

    def test_the_masked_ancestors_of_a_window_stay_stat_able(self) -> None:
        """``realpath`` of the window lstat()s every component above it, so a
        blanket ``file-read*`` deny on the masked root breaks any harness that
        canonicalizes its own $TMPDIR (the Copilot CLI fails session/new with
        "Directory does not exist or cannot be accessed"). The re-open is
        metadata-only and literal: no listing, no read, no sibling."""
        lines = _seatbelt(extra_hidden_dirs=(_APPS,), extra_private_dirs=(_DATA,))
        deny_at = max(
            i for i, ln in enumerate(lines) if ln in _rules_for(lines, "file-read*", _APPS)
        )
        for ancestor in (_APPS, _BUNDLE):
            rule = f"(allow file-read-metadata (literal {json.dumps(ancestor)}))"
            at = [i for i, ln in enumerate(lines) if ln.strip() == rule]
            assert at and at[0] > deny_at, (ancestor, "must follow the deny: last match wins")
        assert not any("(allow" in ln and "subpath" in ln and _APPS in ln for ln in lines)

    def test_an_exposed_file_keeps_its_read_carve_out_beside_a_window(self) -> None:
        """A tree can carry both: the window (read-write, its own state) and a
        read-only exposed file. The window branch must not drop either."""
        exposed = os.path.join(_APPS, "shared.json")
        lines = _seatbelt(
            extra_hidden_dirs=(_APPS,),
            extra_private_dirs=(_DATA,),
            extra_expose_files=(exposed,),
        )
        reads = _rules_for(lines, "file-read*", _APPS)
        assert any(
            f"(require-not (subpath {json.dumps(_DATA)}))" in ln
            and f"(require-not (literal {json.dumps(exposed)}))" in ln
            for ln in reads
        ), reads
        # The exposed file is READ-only: it gets no write exception.
        writes = _rules_for(lines, "file-write*", _APPS)
        assert not any(json.dumps(exposed) in ln for ln in writes), writes

    def test_a_window_equal_to_the_mask_is_refused(self) -> None:
        """Equality would be a mask lift by another name."""
        lines = _seatbelt(extra_hidden_dirs=(_APPS,), extra_private_dirs=(_APPS,))
        assert not any("require-not" in ln and _APPS in ln for ln in lines)
        assert _rules_for(lines, "file-read*", _APPS), "the blanket deny must remain"

    def test_a_window_outside_every_mask_grants_nothing(self) -> None:
        outside = os.path.join(_HOME, "not-masked", "scratch")
        lines = _seatbelt(extra_hidden_dirs=(_APPS,), extra_private_dirs=(outside,))
        assert not any(outside in ln for ln in lines)


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher only")
class TestTheTwoBackendsAgreeOnACallerMask:
    def test_both_carry_the_same_window_for_the_same_spawn(self) -> None:
        kwargs = {"extra_hidden_dirs": (_APPS,), "extra_private_dirs": (_DATA,)}
        hidden, _files, windows = _launcher_view(**kwargs)
        assert _APPS in hidden and windows == [_DATA]
        lines = _seatbelt(**kwargs)
        assert any(
            f"(require-not (subpath {json.dumps(_DATA)}))" in ln
            for ln in _rules_for(lines, "file-read*", _APPS)
        ), "the Linux launcher honours this window; Seatbelt must too"

    def test_the_launcher_stages_the_window_before_it_masks_the_parent(self) -> None:
        script = sandbox._build_launcher_script(
            "cc", extra_hidden_dirs=(_APPS,), extra_private_dirs=(_DATA,)
        )
        stage = script.index("staging private window")
        reopen = script.index("opening private window")
        mask_file = script.index("hiding sensitive file")
        assert stage < reopen < mask_file


@pytest.mark.skipif(os.name == "nt", reason="POSIX backends only")
class TestTheDurableDataView:
    """The five requirements a durable-data view must meet at once, as one
    composition of primitives that behave the same on both backends."""

    _KWARGS = {
        "extra_hidden_dirs": (_APPS, _OWN_SECRET),
        "extra_private_dirs": (_DATA,),
    }

    def test_the_app_reaches_its_own_data_and_nothing_else_in_the_tree(self) -> None:
        hidden, _files, windows = _launcher_view(**self._KWARGS)
        assert not _denied(os.path.join(_DATA, ".kirocrew-deps", "pkg"), hidden, windows)
        assert not _denied(os.path.join(_DATA, "logs", "run.log"), hidden, windows)
        assert _denied(_SIBLING_SECRET, hidden, windows)
        # An app installed after the sandbox was built falls under the ancestor
        # mask, which is the residual a per-leaf .app_secret mask leaves open.
        assert _denied(os.path.join(_APPS, "installed-mid-run", ".app_secret"), hidden, windows)

    def test_the_apps_own_secret_is_masked_although_its_data_is_not(self) -> None:
        hidden, files, windows = _launcher_view(**self._KWARGS)
        assert _OWN_SECRET in files
        assert _denied(_OWN_SECRET, hidden, windows)
        assert not _denied(os.path.join(_DATA, "state.json"), hidden, windows)

    def test_seatbelt_expresses_the_same_view(self) -> None:
        lines = _seatbelt(**self._KWARGS)
        assert any(
            f"(require-not (subpath {json.dumps(_DATA)}))" in ln
            for ln in _rules_for(lines, "file-read*", _APPS)
        )
        assert any(
            ln.lstrip().startswith("(deny") and json.dumps(_OWN_SECRET) in ln for ln in lines
        )
        assert not any(_SIBLING_SECRET in ln and "require-not" in ln for ln in lines)


_SCRATCH = os.path.join(_HOME, ".kiro", "crew", "scratch")
_OWN_SCRATCH = os.path.join(_SCRATCH, "subagent-abc-11111111")
_TREE_SCRATCH = os.path.join(_SCRATCH, "runtime-22222222")
_OTHER_TREE = os.path.join(_SCRATCH, "chat-9-33333333")


@pytest.mark.skipif(os.name == "nt", reason="POSIX backends only")
class TestTwoWindowsInTheScratchMask:
    """A spawn made on a session tree's behalf passes TWO windows into the
    masked scratch root -- its own directory and the tree's
    (``agent_scratch``): both are re-exposed read-write, every other tree stays
    hidden, and the two builders agree. The primitive is N-ary by construction
    (``_private_window_spellings`` iterates); this pins that the second entry is
    honoured exactly like the first rather than assuming it."""

    _KWARGS = {"extra_private_dirs": (_OWN_SCRATCH, _TREE_SCRATCH)}

    def test_the_launcher_opens_both_windows_and_no_sibling(self) -> None:
        hidden, _files, windows = _launcher_view(**self._KWARGS)
        assert _SCRATCH in hidden
        assert windows == [_OWN_SCRATCH, _TREE_SCRATCH]
        assert not _denied(os.path.join(_OWN_SCRATCH, "tmpabc123"), hidden, windows)
        assert not _denied(os.path.join(_TREE_SCRATCH, "docs-refresh", "BRIEF.md"), hidden, windows)
        assert _denied(os.path.join(_OTHER_TREE, "BRIEF.md"), hidden, windows)
        assert _denied(_SCRATCH, hidden, windows)

    def test_seatbelt_carves_both_windows_out_of_the_same_denies(self) -> None:
        lines = _seatbelt(**self._KWARGS)
        for window in (_OWN_SCRATCH, _TREE_SCRATCH):
            except_window = f"(require-not (subpath {json.dumps(window)}))"
            for operation in ("file-read*", "file-write*", "file-link"):
                matching = _rules_for(lines, operation, _SCRATCH)
                assert matching, (operation, window)
                assert any(except_window in ln for ln in matching), (operation, window)
        assert not any(_OTHER_TREE in ln and "require-not" in ln for ln in lines)

    def test_a_single_window_spawn_is_unchanged(self) -> None:
        """The first-process shape (no tree to inherit) still gets exactly one window."""
        _hidden, _files, windows = _launcher_view(extra_private_dirs=(_OWN_SCRATCH,))
        assert windows == [_OWN_SCRATCH]
