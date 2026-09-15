"""Regression guard for the baked ``EXTERNALLY-MANAGED`` marker step in
``packaging/build-desktop.sh``.

Background
----------
An edition whose installs are owned by an external package manager (a Toolbox,
a corporate installer) names its marker through ``KIROCREW_MANAGED_INSTALL_MARKER``.
The script copies it to ``website/electron/EXTERNALLY-MANAGED`` so electron-builder
packs it INTO ``app.asar`` next to ``main.js``, where ``readExternallyManaged``
(``website/electron/auto-update.js``) trusts it as the application's own code on
every platform -- unlike a marker dropped beside the app after the build, which
is gated on file provenance and refused on every user-owned install.

Why this test exists
--------------------
The reader treats a malformed marker as "managed, nothing to run": the updater
is OFF and the About panel offers no command. So a typo in an edition's marker
would ship an app that can neither self-update nor be updated from the UI, and
nothing in the running app would say why. The build step is where that must
fail loudly. These tests extract the step from the shipped script (not a copy,
so a revert is what runs here) and drive it through the accepted shape and each
rejected one, plus the "unset removes a stale leftover" contract that keeps a
previous local build's declaration from riding along.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# Tests that RUN the extracted step need bash and node; the pure-source
# assertions below run everywhere, Windows CI included.
runs_the_step = pytest.mark.skipif(
    os.name == "nt" or shutil.which("node") is None,
    reason="build-desktop.sh is a bash script and the step validates with node",
)

SCRIPT = Path(__file__).parent.parent / "packaging" / "build-desktop.sh"


def _extract_step() -> str:
    """Pull the marker handling out of the shipped script: the unconditional
    stale-marker cleanup (which sits ahead of the SKIP_ELECTRON early exit),
    the SKIP_ELECTRON branch itself, and step 3b, up to the step-4 header."""
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(
        r"(# A leftover staged marker from an earlier interrupted build.*?)"
        r"\n# --- 4\. Package the desktop app",
        text,
        re.DOTALL,
    )
    assert m, "step 3b (baked EXTERNALLY-MANAGED marker) not found in packaging/build-desktop.sh"
    return m.group(1)


def _run(tmp_path: Path, marker_env: str | None) -> tuple[subprocess.CompletedProcess, Path]:
    electron_dir = tmp_path / "electron"
    electron_dir.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_MANAGED_INSTALL_MARKER"}
    if marker_env is not None:
        env["KIROCREW_MANAGED_INSTALL_MARKER"] = marker_env
    env["ELECTRON_DIR"] = str(electron_dir)
    # The same strict mode the real script runs under, and its `log` helper.
    # The step arms an EXIT trap that removes the staged copy, so its content is
    # captured into a witness file BEFORE the shell exits; the test then asserts
    # both halves of the contract: packed content right, nothing left behind.
    script = (
        'set -euo pipefail\nlog() { echo "$@"; }\n'
        + _extract_step()
        + "\n"
        + 'if [ -f "$ELECTRON_DIR/EXTERNALLY-MANAGED" ]; then cp "$ELECTRON_DIR/EXTERNALLY-MANAGED" "$ELECTRON_DIR/.witness"; fi\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
    )
    return proc, electron_dir / "EXTERNALLY-MANAGED"


def _witness(dest: Path) -> Path:
    return dest.parent / ".witness"


VALID = {
    "managedBy": "Builder Toolbox",
    "updateCommand": "/opt/toolbox/bin/toolbox update kirocrew",
    "checkCommand": "/opt/toolbox/bin/kirocrew-update-check",
}


@runs_the_step
def test_valid_marker_is_staged_verbatim_and_cleaned_up_on_exit(tmp_path: Path) -> None:
    src = tmp_path / "marker.json"
    src.write_text(json.dumps(VALID))
    proc, dest = _run(tmp_path, str(src))
    assert proc.returncode == 0, proc.stderr
    assert (
        _witness(dest).read_text() == src.read_text()
    ), "staged copy is byte-identical while the build runs"
    assert not dest.exists(), "the staged copy must not outlive the build (EXIT trap)"
    assert "Baking EXTERNALLY-MANAGED marker" in proc.stdout


@runs_the_step
def test_unset_removes_a_stale_leftover(tmp_path: Path) -> None:
    # A previous local build with the variable set must not leak its
    # declaration into a build without it.
    electron_dir = tmp_path / "electron"
    electron_dir.mkdir()
    (electron_dir / "EXTERNALLY-MANAGED").write_text(json.dumps(VALID))
    proc, dest = _run(tmp_path, None)
    assert proc.returncode == 0, proc.stderr
    assert not dest.exists()


@runs_the_step
def test_empty_value_means_unset(tmp_path: Path) -> None:
    proc, dest = _run(tmp_path, "")
    assert proc.returncode == 0, proc.stderr
    assert not dest.exists()


@runs_the_step
@pytest.mark.parametrize(
    "body, reason",
    [
        ("not json", "not JSON"),
        (json.dumps([1, 2]), "must be a JSON object"),
        (json.dumps({**VALID, "extra": "x"}), "unknown field"),
        (json.dumps({**VALID, "updateCommand": 7}), "must be a string"),
        (json.dumps({"managedBy": "x"}), "no updateCommand"),
        (json.dumps({**VALID, "managedBy": "x" * 9000}), "caps at 8192"),
        # The reader trims and caps each field; the value it would SEE is what
        # is validated, or a whitespace-only command turns the marker bare and
        # an over-cap one is truncated into a different command.
        (json.dumps({**VALID, "updateCommand": "   "}), "leading/trailing whitespace"),
        (json.dumps({**VALID, "checkCommand": " /usr/bin/x"}), "leading/trailing whitespace"),
        (json.dumps({**VALID, "updateCommand": "/usr/bin/" + "x" * 513}), "caps at 512"),
        (json.dumps({**VALID, "managedBy": "m" * 129}), "caps at 128"),
    ],
    ids=[
        "not-json",
        "array",
        "unknown-field",
        "non-string",
        "no-updateCommand",
        "over-cap",
        "whitespace-only-command",
        "padded-command",
        "command-over-512",
        "managedBy-over-128",
    ],
)
def test_malformed_marker_fails_the_build(tmp_path: Path, body: str, reason: str) -> None:
    # Each rejection names its cause: a lane operator reading the build log must
    # not have to diff a JSON file against the reader to find the typo.
    src = tmp_path / "marker.json"
    src.write_text(body)
    proc, dest = _run(tmp_path, str(src))
    assert proc.returncode != 0
    assert reason in proc.stderr, proc.stderr
    assert "KIROCREW_MANAGED_INSTALL_MARKER rejected" in proc.stderr
    assert not _witness(dest).exists(), "a rejected marker must not be staged"


@runs_the_step
def test_missing_file_fails_the_build(tmp_path: Path) -> None:
    proc, dest = _run(tmp_path, str(tmp_path / "nope.json"))
    assert proc.returncode != 0
    assert "does not name a file" in proc.stderr
    assert not dest.exists()


def test_package_json_packs_the_marker_into_the_asar() -> None:
    # The copy is pointless unless electron-builder ships it: build.files is an
    # explicit allowlist, and readExternallyManaged looks next to main.js.
    pkg = json.loads((SCRIPT.parent.parent / "website" / "electron" / "package.json").read_text())
    assert "EXTERNALLY-MANAGED" in pkg["build"]["files"]
    assert "main.js" in pkg["build"]["files"]


def test_cleanup_trap_is_armed_before_the_copy_exists() -> None:
    """There must be no instant at which the staged marker exists without its
    cleanup: an interrupt between `cp` and `trap` would leave a stale marker
    for a hand-run electron-builder to pack into a later, different edition."""
    text = SCRIPT.read_text(encoding="utf-8")
    trap_at = text.index("trap 'rm -f \"$ELECTRON_DIR/EXTERNALLY-MANAGED\"' EXIT")
    copy_at = text.index('cp "$MARKER_SRC" "$ELECTRON_DIR/EXTERNALLY-MANAGED"')
    assert trap_at < copy_at, "arm the EXIT trap before copying the marker"


def test_step_caps_match_the_readers_constants() -> None:
    """Step 3b re-states the reader's caps as literals; keep the two in lockstep
    so a bumped constant in auto-update.js cannot silently pass a marker the
    packaged reader will truncate (or reject a marker it would accept)."""
    reader = (Path(__file__).parent.parent / "website" / "electron" / "auto-update.js").read_text(
        encoding="utf-8"
    )

    def const(name: str) -> int:
        m = re.search(rf"^const {name} = (\d+);", reader, re.M)
        assert m, name
        return int(m.group(1))

    step = SCRIPT.read_text(encoding="utf-8")
    m = re.search(
        r"const caps = \{ managedBy: (\d+), updateCommand: (\d+), checkCommand: (\d+) \};",
        step,
    )
    assert m, "step 3b caps literal not found"
    assert tuple(map(int, m.groups())) == (
        const("MANAGED_BY_MAX_CHARS"),
        const("UPDATE_COMMAND_MAX_CHARS"),
        const("CHECK_COMMAND_MAX_CHARS"),
    )
    m = re.search(r"buf\.length > (\d+)", step)
    assert m and int(m.group(1)) == const("EXTERNALLY_MANAGED_MAX_BYTES")


def test_stale_marker_cleanup_runs_before_the_skip_electron_exit() -> None:
    """A backend-only build (SKIP_ELECTRON=1) must still remove a marker left
    by an earlier interrupted run; otherwise a hand-run electron-builder packs
    stale updater commands into whatever is built next."""
    text = SCRIPT.read_text(encoding="utf-8")
    cleanup_at = text.index('rm -f "$ELECTRON_DIR/EXTERNALLY-MANAGED"')
    skip_at = text.index('if [ "${SKIP_ELECTRON:-0}" = "1" ]; then')
    assert cleanup_at < skip_at, "clean the stale marker before the SKIP_ELECTRON early exit"
