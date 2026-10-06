"""Caller-pinned mask roots and private windows in the two sandbox backends.

``wrap_argv`` accepts an identity for a caller's mask root (``extra_hidden_dir_ids``) and
for each private window (``extra_private_dir_ids``), plus ``(device, inode)`` pairs for the
Linux pre-exec hardlink scan (``extra_alias_credential_ids``). These cases pin how the
Linux launcher and the Seatbelt profile consume them, and how a window inside a caller's
mask is pinned, staged and retired. They read the confinement plan, the profile the
Seatbelt path is handed, and -- for what the Linux child does with the plan -- the
launcher program's own stages driven against a stand-in libc over a real tree. They
spawn nothing.
"""

from __future__ import annotations

import ctypes
import errno
import inspect
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from test_sandbox_launcher_program import (
    CoveringLibc,
    RecordingLibc,
    identity,
    launch,
    payload,
    refusal,
)

from kiro_crew import sandbox
from kiro_crew import sandbox_launcher_program as program
from kiro_crew import sandbox_plan
from kiro_crew.sandbox import SandboxCeilingUnsealable

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits / bind-mount mask")
#: The launcher program's stages pin through ``O_PATH`` and ``/proc/self/fd``, which only
#: Linux has; the namespace launcher runs nowhere else.
_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the namespace launcher is Linux-only"
)

_MS_BIND = 4096


@pytest.fixture(autouse=True)
def _no_real_ssh_probe(monkeypatch):
    """Pin the ``lru_cache``d ``ssh -V`` probe behind the namespace plan.

    The plan these tests read must not vary with the host's ssh, and a real binary
    spawned from the test process is a host dependency.
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

        plan = sandbox._spawn_plan(
            "namespace", "cc", extra_hidden_dirs=(str(apps),), extra_alias_credential_ids=((7, 99),)
        )

        assert sandbox_plan.namespace_payload(plan)["alias_credential_ids"] == [[7, 99]]

        # The child arms its pre-exec scan from that literal alone. The credential is
        # nowhere under the masked tree the child could read -- the apps tree is empty,
        # as its mask leaves it -- and a second link to the carried inode is refused.
        secret = tmp_path / "read-by-the-parent" / ".app_secret"
        secret.parent.mkdir()
        secret.write_text("secret")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        os.link(secret, workspace / "alias")
        info = os.stat(secret)
        child_plan = sandbox_plan.plan_confinement(
            sandbox_plan.SandboxRequest(
                tier="cc",
                extra_hidden_dirs=(str(apps),),
                extra_alias_credential_ids=((info.st_dev, info.st_ino),),
            ),
            sandbox_plan.PlanHost(home=str(tmp_path / "home")),
        )
        child = launch(tmp_path, sandbox_plan.namespace_payload(child_plan))
        message = refusal(program.refuse_hardlinked_credentials, child, [str(workspace)])

        assert message is not None, "the carried pair did not arm the pre-exec scan"
        assert str(workspace / "alias") in message

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

        plan = sandbox._spawn_plan(
            "namespace", "cc", extra_hidden_dirs=(apps,), extra_private_dirs=(window,)
        )
        profile_windows, _refusals = sandbox_plan.private_windows(
            (window,), [apps, os.path.join(window, "edits")]
        )

        assert window in plan.windows
        assert profile_windows == []

    @_LINUX_ONLY
    def test_the_launcher_re_hides_a_nested_leaf_after_binding_the_window(self, tmp_path):
        """Order is the property: before the window, the re-mask lands on a shadowed path.

        The production shape, planned and then placed by the launcher program over a real
        tree: the tier masks the leaf ``apps/meetings/data/edits`` and the caller masks
        ``apps`` with the containing window ``apps/meetings/data``. The leaf's own mask is
        placed first, in list order, and the window's bind then lays the real tree back
        over it, so the leaf is masked AGAIN after the window is bound.
        """
        home = tmp_path / "home"
        apps = home / ".kirocrew" / "apps"
        window = apps / "meetings" / "data"
        nested = window / "edits"
        nested.mkdir(parents=True)
        (window / "notes.md").write_text("notes")
        (nested / "draft").write_text("draft")
        plan = sandbox_plan.plan_confinement(
            sandbox_plan.SandboxRequest(
                tier="cc", extra_hidden_dirs=(str(apps),), extra_private_dirs=(str(window),)
            ),
            sandbox_plan.PlanHost(
                home=str(home), tier_dirs=(".kirocrew/apps/meetings/data/edits",)
            ),
        )
        assert plan.sensitive_dirs == (str(nested), str(apps))
        assert plan.windows == (str(window),), "the namespace plan refused a containing window"
        libc = CoveringLibc()
        run = launch(tmp_path, sandbox_plan.namespace_payload(plan), libc=libc)

        assert refusal(program.place_masks, run) is None

        targets = [call.target_path for call in libc.calls if call.flags & _MS_BIND]
        last_nested = max(i for i, target in enumerate(targets) if target == str(nested))
        assert targets.index(str(window)) < last_nested, "no mask re-hid the leaf after the window"
        assert sorted(os.listdir(window)) == ["edits", "notes.md"]
        assert os.listdir(nested) == [], "the nested leaf came back live inside the window"

    @_POSIX_ONLY
    def test_a_window_equal_to_a_hidden_leaf_is_still_refused_everywhere(self):
        """The EQUALS case has no ordering that saves it: the window IS the masked tree."""
        home = os.path.expanduser("~")
        leaf = os.path.join(home, ".kirocrew", "apps", "aws-control", "data")

        assert sandbox_plan.window_is_a_hidden_target(leaf, [leaf])
        for remasks in (False, True):
            windows, _refusals = sandbox_plan.private_windows(
                (leaf,),
                [os.path.join(home, ".kirocrew", "apps"), leaf],
                remasks_contained_targets=remasks,
            )
            assert windows == []

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
        plan = sandbox._spawn_plan(
            "namespace", "cc", extra_hidden_dirs=(apps,), extra_private_dirs=(leaf, ordinary)
        )

        assert leaf not in plan.windows
        assert leaf in plan.sensitive_dirs
        assert ordinary in plan.windows


def _window_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A masked ``apps`` tree holding the window ``alpha/data`` and a sibling's secret."""
    apps = tmp_path / "home" / ".kirocrew" / "apps"
    window = apps / "alpha" / "data"
    window.mkdir(parents=True)
    (window / "state.db").write_text("own state")
    sibling = apps / "aws-control" / "data"
    sibling.mkdir(parents=True)
    (sibling / "secret").write_text("sibling secret")
    return apps, window, sibling


@_LINUX_ONLY
class TestTheLauncherPinsAWindowAgainstASwappedAncestor:
    """The window is resolved a component at a time, from its mask entry down.

    ``O_NOFOLLOW`` on one open of the whole path refuses a link only at the LAST
    component, so a same-uid process that swaps an ANCESTOR -- making ``apps/alpha`` a
    link to ``apps/aws-control`` after the parent enumerated the windows -- would still
    have its target traversed, and the staging bind runs before the mask loop, so the
    masked leaf would be re-bound read-write at the window's path.

    Driven through the launcher program's staging stage with a stand-in libc: the swap is
    made on a real tree, and what the stage mounts -- or declines to -- is read back.
    """

    def test_every_component_below_the_mask_is_opened_relative_to_a_descriptor(self, tmp_path):
        apps, window, sibling = _window_tree(tmp_path)
        sibling_id = identity(sibling)
        # The ancestor swap, after the parent approved the window by name.
        os.rename(apps / "alpha", apps / "alpha.real")
        os.symlink(apps / "aws-control", apps / "alpha", target_is_directory=True)
        assert (window / "secret").exists(), "the swap must make the window name the sibling"
        libc = CoveringLibc()
        run = launch(
            tmp_path, payload(sensitive_dirs=[str(apps)], private_dirs=[str(window)]), libc=libc
        )

        program.stage_private_windows(run)

        # Nothing was staged: the walk met the link at ``alpha`` and skipped the window,
        # which leaves the parent's mask over it -- the fail-closed direction.
        assert libc.calls == [], "the swapped ancestor was traversed and its leaf staged"
        assert refusal(program.mask_sensitive, run) is None
        assert os.listdir(apps) == [], "the masked tree shows something after the swap"
        assert sibling_id not in {call.source_id for call in libc.calls}

    def test_the_bind_source_is_the_descriptor_not_the_name(self, tmp_path):
        """A second name resolution is a second chance to be redirected."""
        apps, window, _sibling = _window_tree(tmp_path)
        approved = identity(window)
        libc = _SwapBeforeFirstMount(window)
        run = launch(
            tmp_path, payload(sensitive_dirs=[str(apps)], private_dirs=[str(window)]), libc=libc
        )

        program.stage_private_windows(run)

        # The window's name was swapped for a decoy between the pin and the bind, and the
        # staging bind still reached the inode the pin holds.
        assert libc.calls[0].source_id == approved
        assert libc.calls[0].source_id != identity(window)

        # And that mount is a control the spawn refuses without, named for the window.
        failing = RecordingLibc(fail_at=1)
        apps, window, _sibling = _window_tree(tmp_path / "again")
        run = launch(
            tmp_path / "again",
            payload(sensitive_dirs=[str(apps)], private_dirs=[str(window)]),
            libc=failing,
        )
        message = refusal(program.stage_private_windows, run)
        assert message is not None
        assert message.startswith(f"sandbox: BLOCKED -- staging private window {window} failed")


class _SwapBeforeFirstMount(RecordingLibc):
    """A libc whose first ``mount`` finds *name* renamed aside and a decoy in its place.

    The swap a same-uid writer can make after a stage has checked a directory and before
    it mounts: whatever a mount's source or target names is judged at the moment
    ``mount`` runs, so one that names the directory by path reaches the decoy.
    """

    def __init__(self, name: Path) -> None:
        super().__init__()
        self.name = name

    def mount(self, source, target, fstype, flags, data):  # noqa: ANN001, ANN201
        if not self.calls:
            os.rename(self.name, self.name.with_name(self.name.name + ".moved"))
            self.name.mkdir()
        return super().mount(source, target, fstype, flags, data)


class _EventLibc(CoveringLibc):
    """A :class:`CoveringLibc` that keeps every mount and detach in one ordered log."""

    def __init__(self, umount_errno: int = 0) -> None:
        super().__init__()
        self.events: list[tuple[str, str, int]] = []
        self.umount_errno = umount_errno

    def mount(self, source, target, fstype, flags, data):  # noqa: ANN001, ANN201
        result = super().mount(source, target, fstype, flags, data)
        self.events.append(("mount", self.calls[-1].target_path, flags))
        return result

    def umount2(self, target, flags):  # noqa: ANN001, ANN201
        self.events.append(("detach", os.fsdecode(target), flags))
        if self.umount_errno:
            ctypes.set_errno(self.umount_errno)
            return -1
        return super().umount2(target, flags)


@_LINUX_ONLY
class TestNoSecondPathToAWindowOutlivesTheMask:
    """A window's staging mount is retired once every window is bound.

    The stage holds the window's real inode across the bind that hides its parent tree.
    Left in place it is the same tree reachable under a directory nothing masks, and the
    re-mask that re-hides a masked leaf INSIDE the window lands on the window's own path:
    a non-recursive bind carries no submount, so the leaf stays readable through the stage.

    Order is the property, which is why it is asserted over the order of the mounts and
    detaches the launcher program's stages make rather than over a call count: retiring
    before the window is bound would leave the child with no window, and retiring before
    the re-mask would leave the leaf live. A stage whose mask root never materialized is
    retired by the same pass, so the sweep's own guarantee is that NO stage outlives the
    mask loop.
    """

    @staticmethod
    def _launch(tmp_path: Path, libc: RecordingLibc, **overrides: object):
        apps, window, _sibling = _window_tree(tmp_path)
        nested = window / "edits"
        nested.mkdir()
        (nested / "draft").write_text("draft")
        fields: dict = {
            "sensitive_dirs": [str(apps), str(nested)],
            "private_dirs": [str(window)],
        }
        fields.update(overrides)
        return launch(tmp_path, payload(**fields), libc=libc), apps, window, nested

    def test_every_stage_is_retired_after_the_windows_and_their_nested_masks(self, tmp_path):
        libc = _EventLibc()
        run, _apps, window, nested = self._launch(tmp_path, libc)

        assert refusal(program.place_masks, run) is None

        order = [(kind, path) for kind, path, _flags in libc.events]
        stage = libc.calls[0].target_path
        retired = order.index(("detach", stage))
        assert order.index(("mount", str(window))) < retired
        nested_masks = [i for i, event in enumerate(order) if event == ("mount", str(nested))]
        assert nested_masks and max(nested_masks) < retired
        # Every stage is retired, and nothing is left at its path.
        assert libc.detached == [stage]
        assert not os.path.exists(stage)

    def test_the_sweep_runs_outside_the_loop_that_may_skip_a_mask_root(self, tmp_path):
        """A stage whose mask root never materialized is still a second path to the tree."""
        run_dir = tmp_path / "home" / ".kirocrew" / "run"
        run_dir.mkdir(parents=True)
        libc = _EventLibc()
        run, apps, _window, _nested = self._launch(tmp_path, libc, readonly_dirs=[str(run_dir)])

        assert refusal(program.place_masks, run) is None

        # The READONLY seal precedes the hide, so a hide lands on top of an already-sealed
        # parent rather than under it, and the sweep follows the mask loop.
        order = [(kind, path) for kind, path, _flags in libc.events]
        stage = libc.calls[0].target_path
        assert order.index(("mount", str(run_dir))) < order.index(("mount", str(apps)))
        assert order.index(("mount", str(apps))) < order.index(("detach", stage))

        # The sweep retires a stage even for a mask root the mask loop skipped: the root
        # vanished between the staging and the mask, so no mask and no window was placed.
        libc = _EventLibc()
        run, apps, _window, _nested = self._launch(tmp_path / "skipped", libc)
        program.stage_private_windows(run)
        stage = libc.calls[0].target_path
        os.rename(apps, apps.with_name("apps.gone"))

        assert refusal(program.mask_sensitive, run) is None
        assert [kind for kind, _path, _flags in libc.events] == ["mount", "detach"]
        assert libc.detached == [stage]

    def test_retiring_detaches_and_refuses_rather_than_warning(self, tmp_path, capfd):
        libc = _EventLibc(umount_errno=errno.EBUSY)
        run, _apps, _window, _nested = self._launch(tmp_path, libc)

        message = refusal(program.place_masks, run)

        detaches = [flags for kind, _path, flags in libc.events if kind == "detach"]
        assert detaches == [program._MNT_DETACH]
        # A warning here would leave the exposure in place with the spawn proceeding.
        assert message is not None
        assert message.startswith("sandbox: BLOCKED -- could not retire the staging mount for")
        assert "sandbox: WARNING" not in capfd.readouterr().err


@_LINUX_ONLY
class TestTheMaskBindsOntoTheApprovedDirectoryNotItsName:
    """Comparing the inode and then mounting on the NAME leaves the race open.

    The rename the comparison exists to catch can land between the two, and the mask then
    covers whatever answers to the name while the real tree stays readable where it moved.
    So the descriptor that was compared is the mount target. A caller that vouched for no
    identity keeps the plain name.
    """

    def test_the_bind_target_becomes_the_compared_descriptor(self, tmp_path):
        apps = tmp_path / "apps"
        apps.mkdir()
        (apps / "secret").write_text("secret")
        st = os.lstat(str(apps))
        approved = (st.st_dev, st.st_ino)
        libc = _SwapBeforeFirstMount(apps)
        run = launch(
            tmp_path,
            payload(sensitive_dirs=[str(apps)], sensitive_dir_ids={str(apps): list(approved)}),
            libc=libc,
        )

        message = refusal(program.mask_sensitive, run)

        # The mask landed on the approved directory, wherever its name went, and the
        # descriptor was still open when the mount ran: a closed one names nothing.
        assert libc.calls[0].target_id == approved
        assert libc.calls[0].target_id != identity(apps)
        # The name now reaches the decoy rather than the mask, which the read-back refuses.
        assert message is not None and "does not reach its mask after mounting" in message

    def test_a_caller_that_vouches_for_nothing_still_binds_on_the_name(self, tmp_path):
        """A symlinked mask root is a supported layout when no identity was approved.

        Only an approved root is opened no-follow and refused as a link; any other root
        is pinned once through the name, following the link, and masked.
        """
        real = tmp_path / "real-apps"
        real.mkdir()
        (real / "secret").write_text("secret")
        apps = tmp_path / "apps"
        apps.symlink_to(real, target_is_directory=True)
        st = os.stat(real)

        run = launch(tmp_path, payload(sensitive_dirs=[str(apps)]))

        assert refusal(program.mask_sensitive, run) is None
        assert os.listdir(apps) == [], "the tree the link reaches is still readable"

        (tmp_path / "vouched").mkdir()
        vouched = launch(
            tmp_path / "vouched",
            payload(
                sensitive_dirs=[str(apps)], sensitive_dir_ids={str(apps): [st.st_dev, st.st_ino]}
            ),
        )
        message = refusal(program.mask_sensitive, vouched)
        assert message is not None
        assert message.startswith(f"sandbox: BLOCKED -- cannot open approved mask root {apps}")


class TestThePrivateWindowGate:
    """``sandbox_plan.private_windows`` admits a window only where one is safe.

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
        assert sandbox_plan.private_windows((leaf,), [apps, leaf])[0] == []

    def test_a_window_containing_a_hidden_target_is_refused(self, tmp_path):
        apps, _leaf, contains, _ordinary = self._paths(tmp_path)
        nested = os.path.join(contains, "edits")
        assert sandbox_plan.private_windows((contains,), [apps, nested])[0] == []

    def test_an_ordinary_window_inside_the_masked_tree_is_admitted(self, tmp_path):
        apps, leaf, _contains, ordinary = self._paths(tmp_path)
        assert sandbox_plan.private_windows((ordinary,), [apps, leaf])[0] == [ordinary]

    def test_a_window_outside_every_masked_tree_is_dropped(self, tmp_path):
        apps, _leaf, _contains, _ordinary = self._paths(tmp_path)
        outside = str(tmp_path / "elsewhere")
        assert sandbox_plan.private_windows((outside,), [apps])[0] == []

    def test_a_trailing_separator_does_not_defeat_the_refusal(self, tmp_path):
        """The spellings differ by a separator the comparison has to normalise."""
        apps, leaf, _contains, _ordinary = self._paths(tmp_path)
        assert sandbox_plan.private_windows((leaf,), [apps, leaf + os.sep])[0] == []
