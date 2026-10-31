"""The CLI installers download the pinned KAS engine by default.

The engine is not part of the wheel (it must never reach a GitHub Release or a
ghcr image, and it belongs in the data home's read-only ``engines/`` leaf), so
each installer runs the just-installed ``kirocrew kas install`` once Kiro Crew
itself is in. A failed download must not fail the install: it warns and names
the exact command to retry. ``--no-kas`` skips the step, and the installer never
turns ``agent.kas_bundle`` on.

The ``cli.sh`` cases drive the real script through the signed-manifest harness
of ``test_installer_binary_only`` (pipx branch, everything faked), with a
recorder standing in for the installed ``kirocrew`` launcher.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from installer_test_helpers import run_bounded
from test_cli_manifest_signature import (  # noqa: F401  (fixtures are looked up by name)
    SigningKey,
    _openssl_bin,
    test_key,
)
from test_installer_binary_only import _prepare_installer

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
CLI_SH = ROOT / "cli.sh"

# Stands in for the launcher pipx puts in PIPX_BIN_DIR (the fake pipx answers
# $HOME/.local/bin). Records every invocation, one per line, and exits with
# FAKE_KIROCREW_EXIT so the failure branch is reachable without a network.
_RECORDER = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_KIROCREW_LOG"
exit "${FAKE_KIROCREW_EXIT:-0}"
"""


@pytest.fixture(scope="module")
def signing_key(test_key: SigningKey) -> SigningKey:  # noqa: F811 -- pytest fixture by name
    if os.name == "nt":
        pytest.skip("cli.sh is supported on macOS and Linux only")
    return test_key


def _run_cli_sh(
    case: Path,
    key: SigningKey,
    *,
    extra_args: tuple[str, ...] = (),
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
    argv, env, run_root, _argv_file = _prepare_installer(
        case, key, with_pipx=True, extra_env=extra_env
    )
    bin_dir = run_root / "home" / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / "kirocrew"
    launcher.write_text(_RECORDER, encoding="utf-8")
    launcher.chmod(0o755)
    log = run_root / "kirocrew-calls"
    env["FAKE_KIROCREW_LOG"] = str(log)
    result = run_bounded([*argv, *extra_args], env, cwd=str(run_root))
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls, launcher


def test_cli_sh_runs_kas_install_after_the_wheel_by_default(
    tmp_path: Path, signing_key: SigningKey
) -> None:
    result, calls, _launcher = _run_cli_sh(tmp_path / "case", signing_key)

    assert result.returncode == 0, result.stderr
    # Exactly the install, and nothing that would flip agent.kas_bundle.
    assert calls == ["kas install"], calls
    out = result.stdout
    assert "Downloading the built-in KAS engine" in out
    # Kiro Crew itself is installed first; the engine is fetched by the new CLI.
    assert out.index("Installed kirocrew") < out.index("Downloading the built-in KAS engine")
    assert "could not download the built-in KAS engine" not in result.stderr


@pytest.mark.parametrize(
    ("extra_args", "extra_env"),
    [(("--no-kas",), None), ((), {"KIROCREW_INSTALL_KAS": "0"})],
    ids=["flag", "env"],
)
def test_cli_sh_no_kas_skips_the_engine(
    tmp_path: Path,
    signing_key: SigningKey,
    extra_args: tuple[str, ...],
    extra_env: dict[str, str] | None,
) -> None:
    result, calls, _launcher = _run_cli_sh(
        tmp_path / "case", signing_key, extra_args=extra_args, extra_env=extra_env
    )

    assert result.returncode == 0, result.stderr
    assert calls == [], calls
    assert "Downloading the built-in KAS engine" not in result.stdout


def test_cli_sh_failed_engine_download_warns_with_the_retry_command(
    tmp_path: Path, signing_key: SigningKey
) -> None:
    result, calls, launcher = _run_cli_sh(
        tmp_path / "case", signing_key, extra_env={"FAKE_KIROCREW_EXIT": "1"}
    )

    # The install itself succeeded, so the run does too.
    assert result.returncode == 0, result.stderr
    assert calls == ["kas install"], calls
    assert "Installed kirocrew" in result.stdout
    assert "could not download the built-in KAS engine" in result.stderr
    assert f"Retry when online: {launcher} kas install" in result.stderr


def test_cli_sh_help_names_the_opt_out() -> None:
    if os.name == "nt":
        pytest.skip("cli.sh is supported on macOS and Linux only")
    result = run_bounded(["sh", str(CLI_SH), "--help"], os.environ.copy(), cwd=str(ROOT))

    assert result.returncode == 0, result.stderr
    assert "--no-kas" in result.stdout


# ---------------------------------------------------------------------------
# install.sh: a full run builds the clone (npm, vite, pip), so these pin the
# script's own text instead.
# ---------------------------------------------------------------------------


def _install_sh() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def test_install_sh_downloads_the_engine_by_default() -> None:
    text = _install_sh()

    assert re.search(r"^WITH_KAS=1$", text, re.MULTILINE), "the engine download is the default"
    assert not re.search(r"^WITH_KAS=0$", text, re.MULTILINE)
    assert re.search(r"^\s*--no-kas\)\s+WITH_KAS=0 ;;$", text, re.MULTILINE)
    # The former opt-in keeps parsing, so an existing command line still runs.
    assert re.search(r"^\s*--kas\)\s+WITH_KAS=1 ;;$", text, re.MULTILINE)


def test_install_sh_failed_engine_download_only_warns() -> None:
    text = _install_sh()
    block = text[text.index('if "$_kirocrew" kas install; then') :]
    block = block[: block.index("\n    fi\n")]

    assert "exit" not in block, "a failed engine download must not fail the install"
    assert 'detail "Retry when online: kirocrew kas install"' in block


def test_install_sh_never_turns_the_bundle_route_on() -> None:
    text = _install_sh()

    # The switch is only ever named in hints; the installer never sets it.
    for line in text.splitlines():
        stripped = line.strip()
        if "kas_bundle true" in stripped and not stripped.startswith("#"):
            assert stripped.startswith(("echo ", "ok ", "detail ", "info ")), line
