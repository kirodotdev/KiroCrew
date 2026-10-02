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


@pytest.fixture(autouse=True)
def _no_host_ssh_probe(monkeypatch):
    """``_build_launcher_script`` asks the HOST's ``ssh -V`` for accept-new support.

    The private-window lists read out of the launcher do not depend on that answer,
    and a real ssh spawned from the test process is a host dependency this module is
    not about. Pinned so no binary runs.
    """
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)


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
        assert not any(ln.lstrip().startswith("(allow") and _APPS in ln for ln in lines)

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


_CODE = os.path.join(_BUNDLE, "src")


@pytest.mark.skipif(os.name == "nt", reason="POSIX backends only")
class TestAReadOnlyWindowInsideACallerMask:
    """``extra_readonly_private_dirs`` names the subset of a spawn's windows that come
    back READ-ONLY: an app bundle's code directory, which a cron child imports but must
    not be able to rewrite, since the app's own backend later executes it with the app's
    credential in reach. The data window beside it stays read-write. Both builders express
    that: the launcher seals the bound window ``MS_RDONLY`` and Seatbelt carves it out of
    the read deny alone."""

    _KWARGS = {
        "extra_hidden_dirs": (_APPS,),
        "extra_private_dirs": (_DATA, _CODE),
        "extra_readonly_private_dirs": (_CODE,),
    }

    @staticmethod
    def _launcher_readonly(**kwargs: object) -> tuple[list[str], list[str]]:
        script = sandbox._build_launcher_script("cc", **kwargs)  # type: ignore[arg-type]
        windows = json.loads(re.search(r"PRIVATE_DIRS = (\[.*?\])\n", script, re.S).group(1))
        readonly = json.loads(
            re.search(r"READONLY_WINDOWS = frozenset\((\[.*?\])\)\n", script, re.S).group(1)
        )
        return windows, readonly

    def test_the_launcher_names_the_read_only_window_and_no_other(self) -> None:
        windows, readonly = self._launcher_readonly(**self._KWARGS)
        assert windows == [_DATA, _CODE]
        assert readonly == [_CODE]

    def test_the_launcher_seals_the_window_after_binding_it(self) -> None:
        """The seal is a remount of the bind just placed: it has to follow the bind, and it
        has to be the two-step the READONLY_DIRS seal uses (``MS_RDONLY`` is ignored on the
        initial ``MS_BIND``), with the locked bits re-asserted or the kernel refuses it."""
        script = sandbox._build_launcher_script("cc", **self._KWARGS)  # type: ignore[arg-type]
        bind = script.index('"opening private window %s" % p')
        seal = script.index('"sealing read-only window %s" % p')
        assert bind < seal
        sealing = script[bind:seal]
        assert "READONLY_WINDOWS" in sealing
        assert "_MS_REMOUNT | _MS_BIND | _MS_RDONLY" in sealing
        assert "_locked_mount_flags(p.encode())" in sealing
        assert "_mount_or_die(" in sealing, "a seal that cannot be placed must end the spawn"

    def test_the_seal_is_the_bound_windows_own_spelling(self) -> None:
        """The child tests membership by string, so the set is spelled the way the admitted
        windows are: a trailing separator on the caller's side is folded, and an entry
        naming no admitted window seals nothing rather than failing the spawn."""
        windows, readonly = self._launcher_readonly(
            extra_hidden_dirs=(_APPS,),
            extra_private_dirs=(_DATA, _CODE),
            extra_readonly_private_dirs=(_CODE + os.sep, os.path.join(_HOME, "elsewhere")),
        )
        assert windows == [_DATA, _CODE]
        assert readonly == [_CODE]

    def test_a_read_only_entry_that_is_not_a_window_is_inert(self) -> None:
        """A window the gate withheld stays MASKED, which is stricter than read-only; the
        read-only request must not resurrect it."""
        windows, readonly = self._launcher_readonly(
            extra_hidden_dirs=(_APPS,),
            extra_private_dirs=(_DATA,),
            extra_readonly_private_dirs=(_CODE, _APPS),
        )
        assert windows == [_DATA]
        assert readonly == []

    def test_seatbelt_carves_the_window_out_of_the_read_deny_alone(self) -> None:
        lines = _seatbelt(**self._KWARGS)
        except_code = f"(require-not (subpath {json.dumps(_CODE)}))"
        except_data = f"(require-not (subpath {json.dumps(_DATA)}))"
        reads = _rules_for(lines, "file-read*", _APPS)
        assert any(except_code in ln and except_data in ln for ln in reads), reads
        for operation in ("file-write*", "file-link"):
            matching = _rules_for(lines, operation, _APPS)
            assert matching, operation
            assert all(ln.lstrip().startswith("(deny") for ln in matching), operation
            # The data window keeps its write exception; the code window gets none.
            assert any(except_data in ln for ln in matching), (operation, matching)
            assert not any(except_code in ln for ln in matching), (operation, matching)

    def test_seatbelt_denies_writes_blanket_when_every_window_is_read_only(self) -> None:
        """With no writable window the write deny is the plain subpath -- the rule the
        tree carries with no window at all -- rather than an empty ``require-all``."""
        lines = _seatbelt(
            extra_hidden_dirs=(_APPS,),
            extra_private_dirs=(_CODE,),
            extra_readonly_private_dirs=(_CODE,),
        )
        subpath = f"(subpath {json.dumps(_APPS)})"
        for operation in ("file-write*", "file-link"):
            matching = _rules_for(lines, operation, _APPS)
            assert matching == [f"(deny {operation} {subpath})"], (operation, matching)
        assert any(
            f"(require-not (subpath {json.dumps(_CODE)}))" in ln
            for ln in _rules_for(lines, "file-read*", _APPS)
        )

    def test_seatbelt_honours_it_inside_a_tier_mask_too(self) -> None:
        """The tier loop renders windows through the same split, so a read-only window in
        a tree the tier masks (the scratch root) is read-excepted and write-denied."""
        lines = _seatbelt(
            extra_private_dirs=(_OWN_SCRATCH, _TREE_SCRATCH),
            extra_readonly_private_dirs=(_TREE_SCRATCH,),
        )
        except_own = f"(require-not (subpath {json.dumps(_OWN_SCRATCH)}))"
        except_tree = f"(require-not (subpath {json.dumps(_TREE_SCRATCH)}))"
        reads = _rules_for(lines, "file-read*", _SCRATCH)
        assert any(except_own in ln and except_tree in ln for ln in reads), reads
        for operation in ("file-write*", "file-link"):
            matching = _rules_for(lines, operation, _SCRATCH)
            assert any(except_own in ln for ln in matching), (operation, matching)
            assert not any(except_tree in ln for ln in matching), (operation, matching)

    def test_a_spawn_naming_no_read_only_window_renders_as_before(self) -> None:
        """The new keyword is additive: a caller that passes none gets the byte-identical
        read-write rendering on both backends."""
        before = {"extra_hidden_dirs": (_APPS,), "extra_private_dirs": (_DATA,)}
        after = dict(before, extra_readonly_private_dirs=())
        assert _seatbelt(**before) == _seatbelt(**after)
        assert sandbox._build_launcher_script("cc", **before) == sandbox._build_launcher_script(  # type: ignore[arg-type]
            "cc", **after
        )

    def test_both_backends_agree_on_which_window_is_read_only(self) -> None:
        _windows, readonly = self._launcher_readonly(**self._KWARGS)
        lines = _seatbelt(**self._KWARGS)
        write_excepted = {
            w
            for w in (_DATA, _CODE)
            if any(
                f"(require-not (subpath {json.dumps(w)}))" in ln
                for ln in _rules_for(lines, "file-write*", _APPS)
            )
        }
        assert set(readonly) == {_DATA, _CODE} - write_excepted == {_CODE}
