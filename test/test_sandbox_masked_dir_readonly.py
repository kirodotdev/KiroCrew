"""A masked directory with no writable window is sealed read-only, not left writable.

The launcher binds an empty stand-in over each sensitive directory
(:func:`~kiro_crew.sandbox_launcher_program.mask_sensitive`). On Linux that bind
is a WRITABLE empty tmpfs, so a write into a masked path lands in disposable
storage and vanishes when the namespace is torn down -- the write reports success
while nothing is persisted. For a masked directory with no explicit writable
window there is no legitimate in-sandbox writer, so the stand-in must be sealed
read-only (bind, then an ``MS_REMOUNT | MS_BIND | MS_RDONLY`` remount, the same
pair the read-only seal and the unreadable file mask already use) so the write
fails loudly with EROFS instead of succeeding into a throwaway.

A masked directory that DOES carry a private window keeps its stand-in writable:
the window is the explicit write surface, and sealing the stand-in would be a
separate, larger change than this fix.

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


def _home(tmp_path: Path) -> dict[str, Any]:
    """A tree with one masked dir that has a window and one that has none.

    ``apps/alpha/data`` is a private (writable) window inside the masked ``apps``
    dir; ``lonely`` is a sensitive directory with no window at all -- the case the
    issue is about, a cron naming another app's bundle and writing its ``data/``.
    """
    home = tmp_path / "home"
    apps = home / "apps"
    window = apps / "alpha" / "data"
    window.mkdir(parents=True)
    (window / "state.db").write_text("db")
    lonely = home / "lonely"
    lonely.mkdir(parents=True)
    (lonely / "keep").write_text("x")
    return {
        "apps": apps,
        "window": window,
        "lonely": lonely,
        "plan": payload(
            sensitive_dirs=[str(apps), str(lonely)],
            private_dirs=[str(window)],
        ),
    }


def _stand_in_id(call: Any) -> tuple[int, int] | None:
    return call.source_id


def _remount_targets(libc: CoveringLibc) -> list[tuple[int, int] | None]:
    """Target identities of every read-only sealing remount the stages made."""
    return [
        call.target_id
        for call in libc.calls
        if call.flags & _MS_REMOUNT and call.flags & _MS_RDONLY
    ]


def test_masked_dir_without_window_is_sealed_read_only(tmp_path: Path) -> None:
    """The no-window mask's stand-in gets a read-only sealing remount.

    FAILS on origin/main: ``mask_sensitive`` binds the empty stand-in writable and
    never remounts it read-only, so a write into the masked path lands in the
    throwaway tmpfs and is silently lost.
    """
    tree = _home(tmp_path)
    libc = CoveringLibc()
    run = launch(tmp_path, tree["plan"], libc=libc)
    program.place_masks(run)

    lonely_id = run.masked_names.get(str(tree["lonely"]))
    assert lonely_id is not None, "the no-window directory was masked"
    sealed = _remount_targets(libc)
    assert lonely_id in sealed, (
        "the masked directory with no writable window must be sealed read-only so a "
        "write fails loudly instead of landing in the throwaway tmpfs; sealing "
        "remounts observed: %r" % (sealed,)
    )


def test_masked_dir_with_window_keeps_its_window_writable(tmp_path: Path) -> None:
    """A masked dir that carries a private window still binds that window.

    The read-only sealing of no-window masks must not disturb a windowed mask's
    write surface.
    """
    tree = _home(tmp_path)
    libc = CoveringLibc()
    run = launch(tmp_path, tree["plan"], libc=libc)
    program.place_masks(run)

    assert (
        str(tree["window"]).rstrip("/") in run.bound_windows
    ), "the private window inside the masked apps dir must still be bound"
