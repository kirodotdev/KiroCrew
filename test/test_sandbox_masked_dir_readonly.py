"""An OPTED-IN masked directory with no writable window is sealed read-only.

The launcher binds an empty stand-in over each sensitive directory
(:func:`~kiro_crew.sandbox_launcher_program.mask_sensitive`). On Linux that bind
is a WRITABLE empty tmpfs, so a write into a masked path lands in disposable
storage and vanishes when the namespace is torn down -- the write reports success
while nothing is persisted. For a masked directory the caller explicitly OPTED IN
to sealing (``seal_windowless_masks``) and that has no explicit writable window,
there is no legitimate in-sandbox writer, so the stand-in is sealed read-only
(bind, then an ``MS_REMOUNT | MS_BIND | MS_RDONLY`` remount) and the write fails
loudly with EROFS.

The seal is a positive opt-in, keyed on nothing else: a masked directory that did
NOT opt in keeps its writable stand-in. That is what makes a credential mask like
``~/.aws`` -- which re-exposes ``~/.aws/config`` read-only through
``restore_exposed_files`` -- safe: it never opts in, so the restore writes into a
writable stand-in instead of hitting EROFS and aborting the spawn.

Driven through the launcher's own stages with the shared stand-in libc from
``test_sandbox_launcher_program`` for the reason that suite documents: this
process cannot create a user namespace, and the covering libc makes every bind
hide its target exactly as a real mount does, so every post-mount check the stage
makes runs for real.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from test_sandbox_launcher_program import CoveringLibc, launch, payload

from kiro_crew import sandbox_launcher_program

program = sandbox_launcher_program

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the launcher stages pin with os.O_PATH and mount through /proc/self/fd, "
    "neither of which exists outside Linux",
)

_MS_RDONLY = 1
_MS_REMOUNT = 32
_MS_BIND = 4096


def _remount_targets(libc: CoveringLibc) -> list[tuple[int, int] | None]:
    """Target identities of every read-only sealing remount the stages made."""
    return [
        call.target_id
        for call in libc.calls
        if call.flags & _MS_REMOUNT and call.flags & _MS_RDONLY
    ]


def _tree(tmp_path: Path) -> dict[str, Any]:
    """A tree with an opted-in cron mask, a non-opted caller mask, and the ``.aws`` case.

    ``bundle`` is a cron-style mask that opted in. ``other`` is a
    caller mask that did NOT opt in. ``aws`` stands in for a credential-home mask with
    ``aws/config`` re-exposed read-only, which must NOT opt in so the restore step can
    write the config copy back.
    """
    home = tmp_path / "home"
    bundle = home / "bundle" / "data"
    bundle.mkdir(parents=True)
    (bundle / "state.db").write_text("db")
    other = home / "other"
    other.mkdir()
    (other / "keep").write_text("x")
    aws = home / ".aws"
    aws.mkdir()
    (aws / "config").write_text("[profile x]\n")
    (aws / "credentials").write_text("[default]\n")
    return {"bundle": bundle, "other": other, "aws": aws, "home": home}


def test_opted_in_mask_without_window_is_sealed_read_only(tmp_path: Path) -> None:
    """The opted-in windowless mask's stand-in gets a read-only sealing remount.

    FAILS on origin/main: ``mask_sensitive`` binds the empty stand-in writable and
    never remounts it read-only, so a write into the masked path lands in the
    throwaway tmpfs and is silently lost.
    """
    tree = _tree(tmp_path)
    plan = payload(
        sensitive_dirs=[str(tree["bundle"]), str(tree["other"])],
        seal_windowless_masks=[str(tree["bundle"])],
    )
    libc = CoveringLibc()
    run = launch(tmp_path, plan, libc=libc)
    program.place_masks(run)

    bundle_id = run.masked_names.get(str(tree["bundle"]))
    assert bundle_id is not None, "the opted-in mask was masked"
    assert bundle_id in _remount_targets(libc), (
        "an opted-in masked directory with no writable window must be sealed read-only "
        "so a write fails loudly instead of landing in the throwaway tmpfs; sealing "
        "remounts observed: %r" % (_remount_targets(libc),)
    )


def test_a_mask_that_did_not_opt_in_is_left_writable(tmp_path: Path) -> None:
    """A masked dir absent from ``seal_windowless_masks`` keeps its writable stand-in."""
    tree = _tree(tmp_path)
    plan = payload(
        sensitive_dirs=[str(tree["bundle"]), str(tree["other"])],
        seal_windowless_masks=[str(tree["bundle"])],
    )
    libc = CoveringLibc()
    run = launch(tmp_path, plan, libc=libc)
    program.place_masks(run)

    other_id = run.masked_names.get(str(tree["other"]))
    assert other_id is not None, "the non-opted mask was masked"
    assert other_id not in _remount_targets(
        libc
    ), "a mask that did not opt in must keep its writable stand-in"


def test_aws_mask_with_reexposed_config_stays_writable_and_restore_succeeds(
    tmp_path: Path,
) -> None:
    """A credential mask that re-exposes a file is never sealed, so the restore succeeds.

    If ``.aws`` were sealed read-only,
    ``restore_exposed_files``' ``open(dest, "wb")`` would raise EROFS and abort the
    spawn. ``.aws`` does not opt in, so its stand-in stays writable and the config copy
    is written back without error.
    """
    tree = _tree(tmp_path)
    aws = tree["aws"]
    plan = payload(
        sensitive_dirs=[str(aws), str(tree["bundle"])],
        seal_windowless_masks=[str(tree["bundle"])],  # .aws deliberately NOT opted in
        expose_files=[[str(aws / "config"), "config"]],
    )
    libc = CoveringLibc()
    run = launch(tmp_path, plan, libc=libc)

    # The full mask pipeline, including preread + restore_exposed_files, must not raise.
    program.pick_stand_in_root(run)
    program.preread_exposed_files(run)
    program.place_masks(run)  # includes restore_exposed_files

    aws_id = run.masked_names.get(str(aws))
    assert aws_id is not None, "the .aws mask was masked"
    assert aws_id not in _remount_targets(libc), (
        "a credential mask that re-exposes a file must stay writable so the restore "
        "step can write the config copy back; sealing it would EROFS-abort the spawn"
    )
    # The restore wrote the config copy back into the writable stand-in.
    assert (aws / "config").read_text() == "[profile x]\n"
