"""Tripwire for the ``kirocrew.effortOwned`` key-preservation verification.

The effort ownership record lives in the workspace ``cli.json``, a file kiro-cli
itself rewrites, so the record survives only while kiro-cli keeps a workspace key
it does not know. That was verified by hand on kiro-cli 2.24.1 and nothing else
re-checks it. This test ties the re-check to the one kiro-cli version the repo
does control: the BUNDLED CLI pinned in ``packaging/kiro-cli-version``, read by
the desktop, Windows and docker smoke workflows. Raising that pin past the
verified version fails here until someone re-runs the check. The same re-check
confirms that a live ``/effort`` push still sets a session's effort: that push
carries a chat's level over a shared overlay holding another one.

It covers the bundled CLI only. A ``kiro-cli`` found on PATH, which
``acp/client.py`` launches, is whatever the host has installed and is not gated
by anything in this repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.mcp_hot_reload import parse_kiro_cli_version

# The newest kiro-cli on which ``kiro-cli settings --workspace`` writes were
# observed to keep the unknown ``kirocrew.effortOwned`` key and a live ``/effort``
# push was observed to set the session's effort. Raise it only after re-running
# the checks in ``docs/reference/kiro-cli/README.md`` on the new version. Kept in
# the test, not in ``src/``: production code has no reader.
EFFORT_OWNERSHIP_VERIFIED_KIRO_CLI: tuple[int, int, int] = (2, 24, 1)

# Resolved from this file, not the CWD, so the test reads the same pin from any
# invocation directory.
BUNDLED_KIRO_CLI_PIN_FILE = Path(__file__).resolve().parents[1] / "packaging" / "kiro-cli-version"


def _format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(component) for component in version)


def read_bundled_kiro_cli_pin(pin_file: Path) -> tuple[int, int, int]:
    """The bundled kiro-cli version, parsed from its leading numeric components.

    A pre-release or build suffix (``2.25.0-rc1``) parses as ``(2, 25, 0)``; a
    missing patch component reads as 0. A pin that holds no version-shaped token
    is a failure of the pin file, so it fails loudly rather than reading as 0.0.0.
    """
    text = pin_file.read_text(encoding="utf-8")
    version = parse_kiro_cli_version(text)
    if version is None:
        pytest.fail(f"{pin_file} holds no version-shaped token: {text.strip()!r}")
    return version


def test_bundled_pin_not_newer_than_verified_ownership_version():
    """The bundled kiro-cli pin must not outrun the key-preservation check.

    Covers ``packaging/kiro-cli-version`` (the BUNDLED CLI) only, NOT a
    ``kiro-cli`` found on PATH, which ``acp/client.py`` launches. When the pin is
    raised past ``EFFORT_OWNERSHIP_VERIFIED_KIRO_CLI``: re-run the checks in
    ``docs/reference/kiro-cli/README.md`` (a ``kiro-cli settings --workspace``
    write on the new version keeps the ``kirocrew.effortOwned`` key, and a live
    ``/effort`` push sets the session's effort), then raise this constant and the
    verified-version note in ``docs/system-specs/modules/providers.md``.
    """
    pinned = read_bundled_kiro_cli_pin(BUNDLED_KIRO_CLI_PIN_FILE)
    assert pinned <= EFFORT_OWNERSHIP_VERIFIED_KIRO_CLI, (
        f"packaging/kiro-cli-version pins the bundled kiro-cli at "
        f"{_format_version(pinned)}, newer than "
        f"{_format_version(EFFORT_OWNERSHIP_VERIFIED_KIRO_CLI)}, the last version on "
        f"which kiro-cli was verified to keep the unknown kirocrew.effortOwned key in "
        f"the workspace cli.json. This gate covers the bundled CLI only, NOT a "
        f"kiro-cli found on PATH (which acp/client.py launches). Re-run the checks in "
        f"docs/reference/kiro-cli/README.md on {_format_version(pinned)}: a "
        f"`kiro-cli settings --workspace` write must keep the kirocrew.effortOwned "
        f"key, and a live `/effort` push must set the session's effort. Then raise "
        f"EFFORT_OWNERSHIP_VERIFIED_KIRO_CLI in "
        f"test/test_kiro_cli_effort_ownership_pin.py and the verified-version note in "
        f"docs/system-specs/modules/providers.md."
    )
