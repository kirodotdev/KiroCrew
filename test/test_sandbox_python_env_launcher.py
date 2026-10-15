"""The namespace launcher seals the gateway's Python environment without refusing the
spawn.

On the managed install the environment's lexical spelling is the stable
``crew-venv-current`` link itself, so the launcher sees a ``readonly_dirs`` target
whose last component is a symlink, beside the versioned tree it names and the package
directory inside it. These cases run the launcher's own ``seal_readonly`` stage over
that layout, through the shared harness's stand-in libc, and require the tree to be
bound and remounted read-only with no refusal.
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

from kiro_crew import sandbox_launcher_program as program  # noqa: E402
from kiro_crew import sandbox_python_env  # noqa: E402

_MS_REMOUNT = 32
_MS_RDONLY = 1


def _sealed_ids(libc: RecordingLibc) -> set[tuple[int, int]]:
    return {
        call.target_id
        for call in libc.calls
        if call.flags & _MS_REMOUNT and call.flags & _MS_RDONLY
    }


def test_a_linked_managed_environment_is_sealed_without_refusal(tmp_path: Path) -> None:
    versioned = tmp_path / "crew-venv-2"
    package = versioned / "lib" / "python3.12" / "site-packages" / "kiro_crew"
    package.mkdir(parents=True)
    (versioned / "bin").mkdir()
    (versioned / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    current = tmp_path / "crew-venv-current"
    current.symlink_to(versioned, target_is_directory=True)
    lexical_package = current / "lib" / "python3.12" / "site-packages" / "kiro_crew"

    env = sandbox_python_env._python_env_targets_cached.__wrapped__(
        str(lexical_package), str(current), "/usr", sys.executable, "/nonexistent-home"
    )
    assert env[:2] == (str(current), str(versioned.resolve()))

    libc = RecordingLibc()
    targets = [str(lexical_package), str(package.resolve()), *env]
    run = launch(tmp_path, payload(readonly_dirs=targets), libc=libc)
    assert refusal(program.seal_readonly, run) is None
    sealed = _sealed_ids(libc)
    for path in (versioned, package):
        info = os.stat(path)
        assert (info.st_dev, info.st_ino) in sealed, path


def test_a_shared_interpreter_site_packages_is_sealed_without_refusal(tmp_path: Path) -> None:
    site = tmp_path / "user" / "lib" / "python3.12" / "site-packages"
    (site / "kiro_crew").mkdir(parents=True)
    env = sandbox_python_env._python_env_targets_cached.__wrapped__(
        str(site / "kiro_crew"), "/usr", "/usr", sys.executable, "/nonexistent-home"
    )
    assert env == (str(site),)

    libc = RecordingLibc()
    run = launch(tmp_path, payload(readonly_dirs=list(env)), libc=libc)
    assert refusal(program.seal_readonly, run) is None
    info = os.stat(site)
    assert (info.st_dev, info.st_ino) in _sealed_ids(libc)
