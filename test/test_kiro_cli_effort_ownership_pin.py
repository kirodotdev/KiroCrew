"""Tripwire for the ``kirocrew.effortOwned`` load-tolerance and stamp verification.

The effort ownership record lives in the workspace ``cli.json``, a file kiro-cli
itself loads and rewrites. The record counts only while its sibling
``kirocrew.effortOwnedStamp`` equals the file's whole-second mtime, so the design
needs two things of kiro-cli: it loads a workspace ``cli.json`` carrying both keys
it does not know, and its own ``settings --workspace`` write leaves the mtime
unequal to the recorded stamp, which voids the record. Whether that write keeps or
drops the keys decides nothing. Both checks were last run by hand on kiro-cli
2.27.1 and nothing else repeats them. This test ties the re-check to the one
kiro-cli version the repo does control: the BUNDLED CLI pinned in
``packaging/kiro-cli-version``, read by the desktop and Windows build
workflows. Raising that pin past the verified version fails here until someone
re-runs the checks. The same re-check confirms that a live ``/effort`` push still
sets a session's effort: that push carries a chat's level over a shared overlay
holding another one.

It covers the bundled CLI only. A ``kiro-cli`` the host installed itself, which
``kiro_cli.resolve_kiro_cli`` can still pick, is not gated by anything in this
repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.mcp_hot_reload import parse_kiro_cli_version

# The newest kiro-cli on which the checks in ``docs/reference/kiro-cli/README.md``
# were observed to hold: it loads a workspace ``cli.json`` carrying the unknown
# ``kirocrew.effortOwned`` and ``kirocrew.effortOwnedStamp`` keys, a
# ``kiro-cli settings --workspace`` write leaves the file's whole-second mtime
# unequal to the recorded stamp, and a live ``/effort`` push sets the session's
# effort. Raise it only after re-running those checks on the new version. Kept in
# the test, not in ``src/``: production code has no reader.
EFFORT_OWNERSHIP_VERIFIED_KIRO_CLI: tuple[int, int, int] = (2, 27, 1)

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
    """The bundled kiro-cli pin must not outrun the load-tolerance and stamp checks.

    Covers ``packaging/kiro-cli-version`` (the BUNDLED CLI) only, NOT a
    ``kiro-cli`` the host installed itself. When the pin is
    raised past ``EFFORT_OWNERSHIP_VERIFIED_KIRO_CLI``: re-run the checks in
    ``docs/reference/kiro-cli/README.md`` (on the new version kiro-cli loads a
    workspace ``cli.json`` carrying the ``kirocrew.effortOwned`` and
    ``kirocrew.effortOwnedStamp`` keys, with ``settings list`` showing the
    workspace ``chat.modelDefaults`` and a chat spawning and answering; a
    ``kiro-cli settings --workspace`` write leaves the file's whole-second mtime
    unequal to the recorded stamp; and a live ``/effort`` push sets the session's
    effort), then raise this constant and the verified-version note in
    ``docs/system-specs/modules/providers.md``.
    """
    pinned = read_bundled_kiro_cli_pin(BUNDLED_KIRO_CLI_PIN_FILE)
    assert pinned <= EFFORT_OWNERSHIP_VERIFIED_KIRO_CLI, (
        f"packaging/kiro-cli-version pins the bundled kiro-cli at "
        f"{_format_version(pinned)}, newer than "
        f"{_format_version(EFFORT_OWNERSHIP_VERIFIED_KIRO_CLI)}, the last version on "
        f"which kiro-cli was verified to load a workspace cli.json carrying the unknown "
        f"kirocrew.effortOwned and kirocrew.effortOwnedStamp keys and to leave the "
        f"file's mtime unequal to the stamp after its own write. This gate covers the "
        f"bundled CLI only, NOT a kiro-cli the host installed itself. "
        f"Re-run the checks in docs/reference/kiro-cli/README.md on "
        f"{_format_version(pinned)}: kiro-cli must still load a workspace cli.json "
        f"carrying both keys (`settings list` shows the workspace chat.modelDefaults, "
        f"and a chat spawns and answers), a `kiro-cli settings --workspace` write must "
        f"leave the file's whole-second mtime unequal to the recorded stamp, and a live "
        f"`/effort` push must set the session's effort. Then raise "
        f"EFFORT_OWNERSHIP_VERIFIED_KIRO_CLI in "
        f"test/test_kiro_cli_effort_ownership_pin.py and the verified-version note in "
        f"docs/system-specs/modules/providers.md."
    )
