"""Caller-pinned mask roots and private windows in the two sandbox backends.

``wrap_argv`` accepts an identity for a caller's mask root (``extra_hidden_dir_ids``) and
for each private window (``extra_private_dir_ids``), plus ``(device, inode)`` pairs for the
Linux pre-exec hardlink scan (``extra_alias_credential_ids``). These cases pin how the
Linux launcher and the Seatbelt profile consume them, and how a window inside a caller's
mask is pinned, staged and retired. They build launcher text or a profile and spawn
nothing.
"""

from __future__ import annotations

import inspect
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import sandbox
from kiro_crew.sandbox import SandboxCeilingUnsealable

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits / bind-mount mask")


@pytest.fixture(autouse=True)
def _no_real_ssh_probe(monkeypatch):
    """Pin the ``lru_cache``d ``ssh -V`` probe behind ``_build_launcher_script``.

    The launcher text these tests read must not vary with the host's ssh, and a real
    binary spawned from the test process is a host dependency.
    """
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)


class TestTheSeatbeltBackendVerifiesTheWindowItOpens:
    """A Seatbelt profile is path rules end to end, so it cannot enforce an inode at exec.

    It can still read the name's OWN identity immediately before the profile is written,
    which is what the mask roots get, so a window gets the same: granted when it is the
    directory the producer approved, and the spawn refused when it is not. A window
    nobody pinned is unchanged.
    """

    @staticmethod
    def _captured(**kwargs: object) -> dict:
        seen: dict = {}

        def fake_profile(level: str, **kw: object) -> str:
            seen.update(kw)
            return "(version 1)(allow default)"

        with patch.object(sandbox, "_build_seatbelt_profile", fake_profile):
            sandbox.sandbox_exec_argv(["/bin/true"], "cc", **kwargs)  # type: ignore[arg-type]
        return seen

    def test_a_pinned_window_that_is_the_approved_directory_is_granted(self, tmp_path):
        window = tmp_path / "apps" / "alpha" / "data"
        window.mkdir(parents=True)
        real = os.lstat(window)

        seen = self._captured(
            extra_hidden_dirs=(str(tmp_path / "apps"),),
            extra_private_dirs=(str(window),),
            extra_private_dir_ids=((str(window), real.st_dev, real.st_ino),),
        )

        assert seen["extra_private_dirs"] == (str(window),)

    def test_a_pinned_window_that_was_replaced_refuses_the_spawn(self, tmp_path):
        window = tmp_path / "apps" / "alpha" / "data"
        window.mkdir(parents=True)
        real = os.lstat(window)

        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="not the one this spawn"):
            self._captured(
                extra_hidden_dirs=(str(tmp_path / "apps"),),
                extra_private_dirs=(str(window),),
                extra_private_dir_ids=((str(window), real.st_dev, real.st_ino + 1),),
            )

    def test_a_pinned_window_that_is_now_a_file_refuses_the_spawn(self, tmp_path):
        (tmp_path / "apps" / "alpha").mkdir(parents=True)
        window = tmp_path / "apps" / "alpha" / "data"
        window.write_text("not a directory\n")
        real = os.lstat(window)

        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="no longer a directory"):
            self._captured(
                extra_hidden_dirs=(str(tmp_path / "apps"),),
                extra_private_dirs=(str(window),),
                extra_private_dir_ids=((str(window), real.st_dev, real.st_ino),),
            )

    def test_a_pinned_window_whose_name_is_gone_refuses_the_spawn(self, tmp_path):
        window = tmp_path / "apps" / "alpha" / "data"

        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="cannot confirm"):
            self._captured(
                extra_hidden_dirs=(str(tmp_path / "apps"),),
                extra_private_dirs=(str(window),),
                extra_private_dir_ids=((str(window), 66, 1234),),
            )

    def test_an_unpinned_window_is_still_granted(self):
        """The three pre-existing producers pin nothing, and keep their windows."""
        window = "/home/someone/.kirocrew/scratch/session/probe"

        seen = self._captured(
            extra_hidden_dirs=("/home/someone/.kirocrew/scratch",),
            extra_private_dirs=(window,),
        )

        assert seen["extra_private_dirs"] == (window,)

    def test_an_identity_for_a_window_this_spawn_does_not_open_is_not_checked(self, tmp_path):
        """Only the windows actually passed are verified; a stale id entry alone is inert."""
        window = tmp_path / "apps" / "alpha" / "data"

        seen = self._captured(
            extra_hidden_dirs=(str(tmp_path / "apps"),),
            extra_private_dirs=(),
            extra_private_dir_ids=((str(window), 66, 1234),),
        )

        assert seen["extra_private_dirs"] == ()


class TestTheMaskRootIdentityReachesBothBackends:
    """One approval, one rule for confirming it: no-follow, a real directory or nothing.

    A mask that cannot be confirmed REFUSES rather than skipping, because a skipped mask
    leaves the tree it was asked to hide fully readable -- the opposite of a window's skip.
    """

    @staticmethod
    def _captured(**kwargs: object) -> dict:
        seen: dict = {}

        def fake_profile(level: str, **kw: object) -> str:
            seen.update(kw)
            return "(version 1)(allow default)"

        with patch.object(sandbox, "_build_seatbelt_profile", fake_profile):
            sandbox.sandbox_exec_argv(["/bin/true"], "cc", **kwargs)  # type: ignore[arg-type]
        return seen

    def test_a_pinned_mask_root_that_still_matches_is_masked(self, tmp_path):
        """The approval is CONSUMED here, not discarded: a match proceeds as before."""
        apps = tmp_path / ".kirocrew" / "apps"
        apps.mkdir(parents=True)
        st = os.lstat(str(apps))

        seen = self._captured(
            extra_hidden_dirs=(str(apps),),
            extra_hidden_dir_ids=((str(apps), st.st_dev, st.st_ino),),
        )

        assert seen["extra_hidden_dirs"] == (str(apps),)

    def test_a_replaced_mask_root_refuses_the_spawn(self, tmp_path):
        """Withholding a MASK would open the tree, so the fail-closed answer is refusal.

        Break-arm: ``drop_seatbelt_mask_identity``.
        """
        apps = tmp_path / ".kirocrew" / "apps"
        apps.mkdir(parents=True)
        substitute = tmp_path / ".kirocrew" / "other"
        substitute.mkdir()
        approved = os.lstat(str(substitute))

        with pytest.raises(SandboxCeilingUnsealable) as exc:
            self._captured(
                extra_hidden_dirs=(str(apps),),
                extra_hidden_dir_ids=((str(apps), approved.st_dev, approved.st_ino),),
            )

        assert "not the one this spawn approved" in str(exc.value)

    def test_an_unreadable_mask_root_refuses_the_spawn(self, tmp_path):
        apps = tmp_path / ".kirocrew" / "apps"

        with pytest.raises(SandboxCeilingUnsealable) as exc:
            self._captured(
                extra_hidden_dirs=(str(apps),),
                extra_hidden_dir_ids=((str(apps), 66, 1234),),
            )

        assert "cannot confirm the masked directory" in str(exc.value)

    def test_a_mask_root_moved_aside_behind_a_symlink_refuses_the_spawn(self, tmp_path):
        """A rename PRESERVES the inode, so a followed lookup accepts the substitution.

        The tree is moved aside and a link left at the approved name. Following the link
        reaches the same ``(dev, ino)`` the approval recorded, so the comparison alone
        cannot see it, while the profile rule covers the link's name and the tree stays
        readable where it was moved to.

        Break-arm: ``follow_the_seatbelt_mask_identity``.
        """
        crew = tmp_path / ".kirocrew"
        crew.mkdir(parents=True)
        apps = crew / "apps"
        apps.mkdir()
        approved = os.lstat(str(apps))
        moved = crew / "apps.moved"
        os.rename(str(apps), str(moved))
        os.symlink(str(moved), str(apps))

        with pytest.raises(SandboxCeilingUnsealable) as exc:
            self._captured(
                extra_hidden_dirs=(str(apps),),
                extra_hidden_dir_ids=((str(apps), approved.st_dev, approved.st_ino),),
            )

        assert "no longer a directory" in str(exc.value)

    def test_the_backends_agree_on_how_a_mask_root_is_confirmed(self):
        """One approval, so one rule: no-follow, and a real directory or nothing.

        The producer records the identity under those two rules and the Linux child
        re-reads it under them. A backend confirming the same approval by a followed
        lookup compares a different object than the one that was approved.

        Break-arm: ``follow_the_seatbelt_mask_identity``.
        """
        source = inspect.getsource(sandbox.sandbox_exec_argv)
        confirm = source[source.index("for path_, dev, ino in extra_hidden_dir_ids:") :]

        assert "os.lstat(path_)" in confirm
        assert "os.stat(path_)" not in confirm
        assert "stat.S_ISDIR(st.st_mode)" in confirm

    def test_a_mask_identity_alone_still_reaches_this_backend(self):
        """No dispatch may carry a caller's MASK without the identity it was approved as.

        Both backend families, because the defect is the same either way and only one of
        them is this class's own: a dispatch that forwards ``extra_hidden_dirs`` and drops
        ``extra_hidden_dir_ids`` hands its backend a pathname where an inode was settled.
        The Linux hop was unpinned until a mutation removed it and every case here stayed
        green.

        Break-arm: ``drop_a_mask_identity_forward`` (either dispatch).
        """
        source = inspect.getsource(sandbox.wrap_argv)
        dispatches = [
            block
            for block in re.findall(r"\w+_argv\((?:[^()]|\([^()]*\))*\)", source)
            if "extra_hidden_dirs=" in block
        ]

        assert len(dispatches) >= 3, dispatches
        for block in dispatches:
            assert "extra_hidden_dir_ids=" in block, block
        # And the branch tests that gate them: an identity supplied without a path list
        # would otherwise take the no-extras route and be dropped before any dispatch.
        seatbelt = [b for b in dispatches if b.startswith("sandbox_exec_argv(")]
        assert source.count("or extra_hidden_dir_ids") == len(seatbelt)


@_POSIX_ONLY
class TestTheAliasedCredentialInodesReachTheLauncher:
    """A caller's ``(device, inode)`` pairs reach the Linux pre-exec hardlink scan as a literal."""

    def test_the_pairs_reach_the_launcher_as_a_literal(self, tmp_path):
        """A literal, not a scan: the child does no filesystem read of the apps tree."""
        apps = tmp_path / "apps"
        apps.mkdir()

        script = sandbox._build_launcher_script(
            "cc", extra_hidden_dirs=(str(apps),), extra_alias_credential_ids=((7, 99),)
        )

        ids = json.loads(re.search(r"ALIAS_CREDENTIAL_IDS = (\[.*?\])\n", script, re.S).group(1))
        assert ids == [[7, 99]]
        assert "for _acid in ALIAS_CREDENTIAL_IDS:" in script
        assert "os.scandir(_asr)" not in script, "the child must not read the tree"

    def test_the_seatbelt_backend_is_not_handed_inodes_it_cannot_use(self):
        """Stated boundary, not an oversight: there is no launcher script to scan in.

        The seatbelt path writes a profile and execs ``sandbox-exec``; the pre-exec scan is
        a step of the Linux launcher, so the parameter has no consumer there and is not
        forwarded. Every dispatch that DOES carry the mask to the Linux launcher carries it.
        """
        source = inspect.getsource(sandbox.wrap_argv)
        dispatches = [
            block
            for block in re.findall(r"\w+_argv\((?:[^()]|\([^()]*\))*\)", source)
            if "extra_hidden_dirs=" in block
        ]
        assert dispatches, "no dispatch carries a caller's mask"
        for block in dispatches:
            if "namespace_argv(" in block:
                assert "extra_alias_credential_ids=" in block, block
            else:
                assert "extra_alias_credential_ids=" not in block, block


class TestAWindowAtOrAboveAHiddenLeaf:
    """A window may CONTAIN a masked leaf only where the backend can re-mask it after the bind.

    A window EQUAL to a hidden leaf is refused on every backend; a window CONTAINING one is
    carried by the Linux launcher, which re-hides the leaf after binding the window, and
    refused by a Seatbelt profile, which cannot order its rules that way.
    """

    @_POSIX_ONLY
    def test_the_launcher_carries_a_containing_window_and_the_profile_does_not(self):
        """The two backends differ here on purpose, because only one can re-mask.

        The launcher re-hides the nested leaf after binding the window, so it may carry a
        containing window. A Seatbelt profile cannot order its rules that way, so the same
        window is still refused there -- which is what keeps the three pre-existing
        producers' macOS behaviour unchanged.
        """
        home = os.path.expanduser("~")
        apps = os.path.join(home, ".kirocrew", "apps")
        window = os.path.join(apps, "meetings", "data")

        script = sandbox._build_launcher_script(
            "cc", extra_hidden_dirs=(apps,), extra_private_dirs=(window,)
        )
        carried = json.loads(re.search(r"PRIVATE_DIRS = (\[.*?\])\n", script, re.S).group(1))
        profile_windows = sandbox._private_window_spellings(
            (window,), [apps, os.path.join(window, "edits")]
        )

        assert window in carried
        assert profile_windows == []

    @_POSIX_ONLY
    def test_the_launcher_re_hides_a_nested_leaf_after_binding_the_window(self):
        """Order is the property: before the window, the re-mask lands on a shadowed path."""
        home = os.path.expanduser("~")
        apps = os.path.join(home, ".kirocrew", "apps")
        window = os.path.join(apps, "meetings", "data")

        script = sandbox._build_launcher_script(
            "cc", extra_hidden_dirs=(apps,), extra_private_dirs=(window,)
        )

        assert script.index("opening private window") < script.index(
            "re-hiding nested masked directory"
        )

    @_POSIX_ONLY
    def test_a_window_equal_to_a_hidden_leaf_is_still_refused_everywhere(self):
        """The EQUALS case has no ordering that saves it: the window IS the masked tree."""
        home = os.path.expanduser("~")
        leaf = os.path.join(home, ".kirocrew", "apps", "aws-control", "data")

        assert sandbox._window_is_a_hidden_target(leaf, [leaf])
        for remasks in (False, True):
            assert (
                sandbox._private_window_spellings(
                    (leaf,),
                    [os.path.join(home, ".kirocrew", "apps"), leaf],
                    remasks_contained_targets=remasks,
                )
                == []
            )

    @_POSIX_ONLY
    def test_the_launcher_keeps_the_leaf_denied_and_opens_no_window_for_it(self):
        """The gate refuses the collision even when a caller names it directly.

        This is the guarantee that does not depend on the enumeration above: any caller
        handing the launcher such a window gets it dropped, and the leaf stays in the
        masked list, while an ordinary app's window on the same tree survives.
        """
        home = os.path.expanduser("~")
        apps = os.path.join(home, ".kirocrew", "apps")
        leaf = os.path.join(apps, "aws-control", "data")
        ordinary = os.path.join(apps, "alpha", "data")
        script = sandbox._build_launcher_script(
            "cc", extra_hidden_dirs=(apps,), extra_private_dirs=(leaf, ordinary)
        )
        masked = json.loads(re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S).group(1))
        windows = json.loads(re.search(r"PRIVATE_DIRS = (\[.*?\])\n", script, re.S).group(1))

        assert leaf not in windows
        assert leaf in masked
        assert ordinary in windows


class TestTheLauncherPinsAWindowAgainstASwappedAncestor:
    """The window is resolved a component at a time, from its mask entry down.

    ``O_NOFOLLOW`` on one open of the whole path refuses a link only at the LAST
    component, so a same-uid process that swaps an ANCESTOR -- making ``apps/alpha`` a
    link to ``apps/aws-control`` after the parent enumerated the windows -- would still
    have its target traversed, and the staging bind runs before the mask loop, so the
    masked leaf would be re-bound read-write at the window's path.

    Asserted over the generated launcher text, which is where the walk lives: the
    descriptor-relative form must be present AND the whole-path form must be absent.
    The second half is the one that catches a regression, since a reviewer adding a
    convenience open beside the walk leaves the first half true.
    """

    @_POSIX_ONLY
    def test_every_component_below_the_mask_is_opened_relative_to_a_descriptor(self):
        script = sandbox._build_launcher_script(
            "cc",
            extra_hidden_dirs=(os.path.join(os.path.expanduser("~"), ".kirocrew", "apps"),),
            extra_private_dirs=(
                os.path.join(os.path.expanduser("~"), ".kirocrew", "apps", "alpha", "data"),
            ),
        )

        assert "os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY" in script
        assert "dir_fd=fd" in script
        # The regression shape: one open of the joined path, which leaves every
        # ancestor component following.
        assert "os.open(p, os.O_PATH" not in script

    @_POSIX_ONLY
    def test_the_bind_source_is_the_descriptor_not_the_name(self):
        """A second name resolution is a second chance to be redirected."""
        script = sandbox._build_launcher_script(
            "cc",
            extra_hidden_dirs=(os.path.join(os.path.expanduser("~"), ".kirocrew", "apps"),),
            extra_private_dirs=(
                os.path.join(os.path.expanduser("~"), ".kirocrew", "apps", "alpha", "data"),
            ),
        )

        assert "/proc/self/fd/%d" in script
        assert 'staging private window %s" % p' in script


@_POSIX_ONLY
class TestNoSecondPathToAWindowOutlivesTheMask:
    """A window's staging mount is retired once every window is bound.

    The stage holds the window's real inode across the bind that hides its parent tree.
    Left in place it is the same tree reachable under a directory nothing masks, and the
    re-mask that re-hides a masked leaf INSIDE the window lands on the window's own path:
    a non-recursive bind carries no submount, so the leaf stays readable through the stage.

    Order is the property, which is why it is asserted over the generated launcher rather
    than over a call count: retiring before the window is bound would leave the child with
    no window, and retiring before the re-mask would leave the leaf live. A stage whose
    mask root never materialized is retired by the same pass, so the sweep's own guarantee
    is that NO stage outlives the mask loop.
    """

    @staticmethod
    def _script(apps: Path) -> str:
        return sandbox._build_launcher_script(
            "cc",
            extra_hidden_dirs=(str(apps),),
            extra_private_dirs=(str(apps / "alpha" / "data"),),
        )

    def test_every_stage_is_retired_after_the_windows_and_their_nested_masks(self, tmp_path):
        script = self._script(tmp_path / "apps")
        # The CALL, not the helper definition, which necessarily precedes the whole loop.
        call = script.index("_retire_stage_or_die(_private_stage.pop(_staged)")

        assert script.index("opening private window") < call
        assert script.index("re-hiding nested masked directory") < call
        assert "for _staged in list(_private_stage):" in script

    def test_the_sweep_runs_outside_the_loop_that_may_skip_a_mask_root(self, tmp_path):
        """A stage whose mask root never materialized is still a second path to the tree."""
        script = self._script(tmp_path / "apps")
        loop = script.index("for d in SENSITIVE_DIRS:")
        sweep = script.index("for _staged in list(_private_stage):")
        readonly = script.index("for d in READONLY_DIRS:")

        # The sweep runs AFTER the SENSITIVE_DIRS mask loop, so it retires a stage even
        # for a mask root that loop skipped. The READONLY_DIRS seal precedes the
        # SENSITIVE_DIRS hide, so a hide lands on top of an already-sealed parent rather
        # than under it. The load-bearing relationship here is that the sweep follows the
        # mask loop.
        assert readonly < loop < sweep

    def test_retiring_detaches_and_refuses_rather_than_warning(self, tmp_path):
        script = self._script(tmp_path / "apps")
        body = script.split("def _retire_stage_or_die(")[1].split("\ndef ")[0]

        assert "_MNT_DETACH" in body
        # A warning here would leave the exposure in place with the spawn proceeding.
        assert "sys.exit(" in body
        assert "sandbox: WARNING" not in body


@_POSIX_ONLY
class TestTheMaskBindsOntoTheApprovedDirectoryNotItsName:
    """Comparing the inode and then mounting on the NAME leaves the race open.

    The rename the comparison exists to catch can land between the two, and the mask then
    covers whatever answers to the name while the real tree stays readable where it moved.
    So the descriptor that was compared is the mount target. A caller that vouched for no
    identity keeps the plain name.
    """

    @staticmethod
    def _script(apps: Path, ids: tuple) -> str:
        return sandbox._build_launcher_script(
            "cc",
            extra_hidden_dirs=(str(apps),),
            extra_hidden_dir_ids=ids,
        )

    def test_the_bind_target_becomes_the_compared_descriptor(self, tmp_path):
        apps = tmp_path / "apps"
        apps.mkdir()

        st = os.lstat(str(apps))
        script = self._script(apps, ((str(apps), st.st_dev, st.st_ino),))
        region = script.split("_want_dir_id = SENSITIVE_DIR_IDS.get(d)")[1]
        region = region.split("hiding credential directory")[0]

        assert 'target = ("/proc/self/fd/%d" % _mask_fd).encode()' in region
        # The descriptor must still be open when the mount runs: closing it first is the
        # regression that turns the comparison back into a name check.
        assert "os.close(_mask_fd)" not in region.split("target = (")[1]

    def test_a_caller_that_vouches_for_nothing_still_binds_on_the_name(self, tmp_path):
        apps = tmp_path / "apps"
        apps.mkdir()

        script = self._script(apps, ())

        assert "if _want_dir_id is not None:" in script
        assert "_mask_fd = -1" in script


class TestThePrivateWindowGate:
    """``_private_window_spellings`` admits a window only where one is safe.

    Both directions are pinned: a window that re-exposes a hidden target is refused, and
    an ordinary window inside the same masked tree is still admitted. Without the second
    assertion a gate that refused everything would pass the first.

    Paths are built from ``tmp_path`` rather than written as literals, because the rule is
    lexical over ``os.sep`` and a POSIX literal makes every case vacuous on Windows.
    """

    @staticmethod
    def _paths(tmp_path: Path) -> tuple[str, str, str, str]:
        apps = str(tmp_path / "apps")
        return (
            apps,
            os.path.join(apps, "aws-control", "data"),
            os.path.join(apps, "meetings", "data"),
            os.path.join(apps, "alpha", "data"),
        )

    def test_a_window_equal_to_a_hidden_target_is_refused(self, tmp_path):
        apps, leaf, _contains, _ordinary = self._paths(tmp_path)
        assert sandbox._private_window_spellings((leaf,), [apps, leaf]) == []

    def test_a_window_containing_a_hidden_target_is_refused(self, tmp_path):
        apps, _leaf, contains, _ordinary = self._paths(tmp_path)
        nested = os.path.join(contains, "edits")
        assert sandbox._private_window_spellings((contains,), [apps, nested]) == []

    def test_an_ordinary_window_inside_the_masked_tree_is_admitted(self, tmp_path):
        apps, leaf, _contains, ordinary = self._paths(tmp_path)
        assert sandbox._private_window_spellings((ordinary,), [apps, leaf]) == [ordinary]

    def test_a_window_outside_every_masked_tree_is_dropped(self, tmp_path):
        apps, _leaf, _contains, _ordinary = self._paths(tmp_path)
        outside = str(tmp_path / "elsewhere")
        assert sandbox._private_window_spellings((outside,), [apps]) == []

    def test_a_trailing_separator_does_not_defeat_the_refusal(self, tmp_path):
        """The spellings differ by a separator the comparison has to normalise."""
        apps, leaf, _contains, _ordinary = self._paths(tmp_path)
        assert sandbox._private_window_spellings((leaf,), [apps, leaf + os.sep]) == []
