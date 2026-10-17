"""The namespace launcher seals a packaged install directory without refusing the spawn.

The planner names the gateway's package directory in ``readonly_dirs`` in two
spellings: the lexical one the interpreter imports from and its ``realpath``. On the
managed install the lexical spelling runs through the stable ``crew-venv-current``
link to a versioned venv, so the launcher sees a target with a symlinked component.
A refusal there would stop every sandboxed spawn on that install, so these cases run
the launcher's own ``seal_readonly`` stage over that layout, through the shared
harness's stand-in libc, and require each spelling to be bound and remounted
read-only with no refusal.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the namespace launcher is Linux-only"
)

from test_sandbox_launcher_program import RecordingLibc, launch, payload, refusal  # noqa: E402

from kiro_crew import sandbox  # noqa: E402
from kiro_crew import sandbox_launcher_program as program  # noqa: E402

_MS_BIND = 4096
_MS_REMOUNT = 32
_MS_RDONLY = 1


def _managed_venv(tmp_path: Path) -> tuple[Path, Path]:
    """A versioned venv reached through a ``-current`` link, as the updater lays it out."""
    versioned = tmp_path / "crew-venv-2" / "lib" / "python3.12" / "site-packages" / "kiro_crew"
    (versioned / "static" / "dist").mkdir(parents=True)
    (versioned / "__init__.py").write_text("", encoding="utf-8")
    current = tmp_path / "crew-venv-current"
    current.symlink_to(tmp_path / "crew-venv-2", target_is_directory=True)
    lexical = current / "lib" / "python3.12" / "site-packages" / "kiro_crew"
    return lexical, versioned


def test_both_spellings_of_a_linked_install_are_sealed_without_refusal(tmp_path: Path) -> None:
    lexical, versioned = _managed_venv(tmp_path)
    targets = list(sandbox._install_root_targets_cached.__wrapped__(str(lexical)))
    assert targets == [str(lexical), str(versioned.resolve())]

    libc = RecordingLibc()
    run = launch(tmp_path, payload(readonly_dirs=targets), libc=libc)
    assert refusal(program.seal_readonly, run) is None

    remounts = [call for call in libc.calls if call.flags & _MS_REMOUNT and call.flags & _MS_RDONLY]
    sealed = {call.target_id for call in remounts}
    versioned_stat = os.stat(versioned)
    assert (versioned_stat.st_dev, versioned_stat.st_ino) in sealed
    assert all(call.flags & _MS_BIND for call in remounts)


def test_a_direct_site_packages_install_is_sealed_without_refusal(tmp_path: Path) -> None:
    package = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "kiro_crew"
    package.mkdir(parents=True)
    targets = list(sandbox._install_root_targets_cached.__wrapped__(str(package)))
    assert targets == [str(package)]

    libc = RecordingLibc()
    run = launch(tmp_path, payload(readonly_dirs=targets), libc=libc)
    assert refusal(program.seal_readonly, run) is None
    package_stat = os.stat(package)
    assert any(
        call.flags & _MS_REMOUNT
        and call.flags & _MS_RDONLY
        and call.target_id == (package_stat.st_dev, package_stat.st_ino)
        for call in libc.calls
    )
