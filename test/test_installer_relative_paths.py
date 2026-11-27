"""cli.sh with a relative or ``~``-prefixed location: a launcher that resolves.

``~/.local/bin/kirocrew`` is a symlink to the managed venv's ``bin/kirocrew``,
and a relative symlink target is read from the link's own folder. So the
installer makes a relative ``KIROCREW_HOME`` or ``KIROCREW_VENV`` absolute
against the physical directory it runs in, and expands a ``KIROCREW_HOME`` of
``~`` or ``~/...`` against ``$HOME`` the way ``kirocrew`` itself reads it. The
cases drive the real script through the signed-manifest harness of
``test_installer_binary_only`` (venv branch) from the run root or from a
directory reached through a symlink, and check that the launcher (and the
``-current`` link) land in the expected venv.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from installer_test_helpers import run_bounded
from test_cli_manifest_signature import (  # noqa: F401  (fixtures are looked up by name)
    SigningKey,
    _openssl_bin,
    test_key,
)
from test_installer_binary_only import _prepare_installer


@pytest.fixture(scope="module")
def signing_key(test_key: SigningKey) -> SigningKey:  # noqa: F811 -- pytest fixture by name
    if os.name == "nt":
        pytest.skip("cli.sh is supported on macOS and Linux only")
    return test_key


@pytest.mark.parametrize(
    ("variable", "value", "venv_dir"),
    [
        ("KIROCREW_HOME", "my-data", "my-data-venv"),
        ("KIROCREW_VENV", "my-venv", "my-venv"),
        ("KIROCREW_HOME", "~/my-data", "home/my-data-venv"),
    ],
    ids=["relative-home", "relative-venv", "tilde-home"],
)
def test_a_relative_location_links_the_launcher_to_an_absolute_venv(
    tmp_path: Path, signing_key: SigningKey, variable: str, value: str, venv_dir: str
) -> None:
    argv, env, run_root, _argv_file = _prepare_installer(
        tmp_path / "case", signing_key, with_pipx=False, extra_env={variable: value}
    )

    result = run_bounded([*argv, "--no-kas"], env, cwd=str(run_root))

    assert result.returncode == 0, result.stderr
    launcher = run_root / "home" / ".local" / "bin" / "kirocrew"
    assert launcher.exists()
    assert launcher.resolve() == (run_root / venv_dir / "bin" / "kirocrew").resolve()


def test_a_relative_location_is_read_from_the_physical_directory(
    tmp_path: Path, signing_key: SigningKey
) -> None:
    """Run from a directory reached through a symlink, ``..`` leaves the real
    directory, as the kernel reads it, so the ``-current`` link (written with
    ``os.path.abspath``, which drops ``..`` as text) names the venv too."""
    argv, env, run_root, _argv_file = _prepare_installer(
        tmp_path / "case", signing_key, with_pipx=False, extra_env={"KIROCREW_HOME": "../my-data"}
    )
    real = run_root / "real" / "work"
    real.mkdir(parents=True)
    work = run_root / "work"
    work.symlink_to(real, target_is_directory=True)
    env["PWD"] = str(work)

    result = run_bounded([*argv, "--no-kas"], env, cwd=str(work))

    assert result.returncode == 0, result.stderr
    venv = (run_root / "real" / "my-data-venv").resolve()
    launcher = run_root / "home" / ".local" / "bin" / "kirocrew"
    assert launcher.resolve() == venv / "bin" / "kirocrew"
    assert (run_root / "real" / "my-data-venv-current").resolve() == venv
