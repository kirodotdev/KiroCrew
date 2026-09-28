"""Detach drills for ``packaging/signing/hdiutil-detach.sh``.

``hdiutil detach`` unmounts the volume and then ejects the device, and exits
non-zero when either half fails.  Under Spotlight/XProtect load the unmount
can succeed while the eject reports "Resource busy", or the eject can finish
on its own moments after that verdict.  A retry loop that trusts the exit
status alone, or that addresses the mount path, then fails every attempt
against a volume that is already gone.  These drills stand in a fake
``hdiutil`` whose behaviour is scripted per scenario and assert the helper's
verdict against the device's real attachment state rather than the last exit
code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SIGNING_DIR = Path(__file__).resolve().parents[1] / "packaging" / "signing"
LIBRARY = SIGNING_DIR / "hdiutil-detach.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="hdiutil-detach.sh is a Bash library for the macOS signing runner",
)

DEVICE = "/dev/disk5"

# The fake keeps the device's attachment state in $FAKE_STATE_DIR/attached and
# counts detach calls in $FAKE_STATE_DIR/calls.  `hdiutil info -plist` reports
# the device (and its slice) only while the marker file exists.  The detach
# behaviour is chosen by $FAKE_SCENARIO.
FAKE_HDIUTIL = r"""#!/usr/bin/env bash
set -u
state="$FAKE_STATE_DIR"
case "$1" in
  info)
    echo '<plist><dict><key>images</key><array>'
    if [ -e "$state/attached" ]; then
      echo '<dict><key>system-entities</key><array>'
      echo '<dict><key>dev-entry</key><string>/dev/disk5</string></dict>'
      echo '<dict><key>dev-entry</key><string>/dev/disk5s1</string>'
      echo '<key>mount-point</key><string>/tmp/fake-mount</string></dict>'
      echo '</array></dict>'
    fi
    echo '</array></dict></plist>'
    exit 0
    ;;
  detach)
    target="$2"
    force=0
    [ "${3:-}" = "-force" ] && force=1
    n=$(( $(cat "$state/calls" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$state/calls"
    echo "$target${3:+ $3}" >> "$state/targets"
    if [ ! -e "$state/attached" ]; then
      echo "hdiutil: detach failed - No such file or directory" >&2
      exit 1
    fi
    case "$FAKE_SCENARIO" in
      clean)
        rm -f "$state/attached"; exit 0 ;;
      busy-then-gone)
        # First verdict is busy, but the eject completes on its own right after.
        rm -f "$state/attached"
        echo 'hdiutil: couldn'"'"'t eject "disk5" - Resource busy' >&2
        exit 1 ;;
      busy-until-force)
        if [ "$force" -eq 1 ]; then rm -f "$state/attached"; exit 0; fi
        echo 'hdiutil: couldn'"'"'t eject "disk5" - Resource busy' >&2
        exit 1 ;;
      always-busy)
        echo 'hdiutil: couldn'"'"'t eject "disk5" - Resource busy' >&2
        exit 1 ;;
      *)
        echo "fake hdiutil: unknown scenario $FAKE_SCENARIO" >&2; exit 99 ;;
    esac
    ;;
  *)
    echo "fake hdiutil: unexpected verb $1" >&2
    exit 99
    ;;
esac
"""


def _run_detach(tmp_path: Path, scenario: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "hdiutil"
    fake.write_text(FAKE_HDIUTIL, encoding="utf-8")
    fake.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    (state / "attached").touch()
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env.update(
        FAKE_STATE_DIR=str(state),
        FAKE_SCENARIO=scenario,
        DMG_DETACH_ATTEMPTS="3",
        DMG_DETACH_RETRY_BASE_SECS="0",
    )
    proc = subprocess.run(
        ["bash", "-c", 'source "$1" && dmg_detach_device "$2"', "drill", str(LIBRARY), DEVICE],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    return proc, state


def _targets(state: Path) -> list[str]:
    return (state / "targets").read_text(encoding="utf-8").splitlines()


def test_clean_detach_succeeds_first_try(tmp_path: Path) -> None:
    proc, state = _run_detach(tmp_path, "clean")
    assert proc.returncode == 0, proc.stderr
    assert _targets(state) == [DEVICE]
    assert not (state / "attached").exists()


def test_busy_verdict_over_an_eject_that_completed_is_success(tmp_path: Path) -> None:
    # The nightly failure shape: attempt 1 says "Resource busy" but the device
    # is gone by the time we look.  No retry, no force, exit 0.
    proc, state = _run_detach(tmp_path, "busy-then-gone")
    assert proc.returncode == 0, proc.stderr
    assert _targets(state) == [DEVICE]
    assert "no longer attached after a failed detach (attempt 1/3)" in proc.stderr


def test_persistent_busy_falls_through_to_force(tmp_path: Path) -> None:
    proc, state = _run_detach(tmp_path, "busy-until-force")
    assert proc.returncode == 0, proc.stderr
    # Three plain attempts, then exactly one -force, every one on the device node.
    assert _targets(state) == [DEVICE, DEVICE, DEVICE, f"{DEVICE} -force"]
    assert "forcing" in proc.stderr
    assert not (state / "attached").exists()


def test_still_attached_after_force_is_a_failure(tmp_path: Path) -> None:
    # Fail-closed: a device that survives -force is reported, not papered over.
    proc, state = _run_detach(tmp_path, "always-busy")
    assert proc.returncode == 1, proc.stderr
    assert _targets(state) == [DEVICE, DEVICE, DEVICE, f"{DEVICE} -force"]
    assert "still attached after a forced detach" in proc.stderr
    assert (state / "attached").exists()


@pytest.mark.parametrize(
    ("plist", "expected"),
    [
        (
            "<plist><dict><key>system-entities</key><array>"
            "<dict><key>dev-entry</key><string>/dev/disk7</string></dict>"
            "<dict><key>dev-entry</key><string>/dev/disk7s1</string>"
            "<key>mount-point</key><string>/tmp/m</string></dict>"
            "</array></dict></plist>",
            "/dev/disk7",
        ),
        (
            # The shape hdiutil actually prints: one key/value pair per line, tab-indented.
            '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">\n<dict>\n'
            "\t<key>system-entities</key>\n\t<array>\n\t\t<dict>\n"
            "\t\t\t<key>content-hint</key>\n\t\t\t<string>GUID_partition_scheme</string>\n"
            "\t\t\t<key>dev-entry</key>\n\t\t\t<string>/dev/disk5</string>\n\t\t</dict>\n"
            "\t\t<dict>\n\t\t\t<key>dev-entry</key>\n\t\t\t<string>/dev/disk5s1</string>\n"
            "\t\t\t<key>mount-point</key>\n\t\t\t<string>/private/tmp/kirocrew-dmg.x/mount</string>\n"
            "\t\t</dict>\n\t</array>\n</dict>\n</plist>\n",
            "/dev/disk5",
        ),
        (
            # Slice listed before the whole disk: the whole disk still wins.
            "<dict><key>dev-entry</key><string>/dev/disk12s2</string></dict>"
            "<dict><key>dev-entry</key><string>/dev/disk12</string></dict>",
            "/dev/disk12",
        ),
    ],
)
def test_device_is_read_from_the_attach_plist(tmp_path: Path, plist: str, expected: str) -> None:
    proc = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" && dmg_device_from_attach_plist "$2"',
            "drill",
            str(LIBRARY),
            plist,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


def test_missing_device_in_attach_plist_fails_loudly(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" && dmg_device_from_attach_plist "$2"',
            "drill",
            str(LIBRARY),
            "<plist/>",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "could not find the whole-disk dev-entry" in proc.stderr
