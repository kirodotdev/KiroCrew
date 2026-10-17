"""The installer smoke captures the NSIS wizard, never the window NSIS shows first.

``.github/scripts/test-windows-installer.ps1`` starts the installer without
``/S`` and screenshots its install-mode page as review evidence. Before the
wizard exists, NSIS can show an unowned "Verifying installer: N%" dialog
(``IDD_VERIFY``) for as long as its CRC pass over the exe runs past one second,
then destroys it. ``Process.MainWindowHandle`` reports that dialog while it
exists, so a capture that takes the first non-zero handle can pass a destroyed
window to ``GetWindowRect`` and fail the job with "Could not read the native
installer bounds." on a slow runner disk.

The script runs only on a Windows runner, so these tests pin the shape of the
wait statically: the handle that is captured must be one the poll accepted as
the wizard (it carries the wizard's Cancel control, dialog item 2), and it must
still be a window after the settle sleep.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_SCRIPT = ROOT / ".github" / "scripts" / "test-windows-installer.ps1"


@pytest.fixture(scope="module")
def script() -> str:
    return INSTALLER_SCRIPT.read_text(encoding="utf-8")


def _preview_block(script: str) -> str:
    start = script.index("$preview = Start-Process")
    end = script.index("Stop-Process -Id $preview.Id", start)
    return script[start:end]


def test_the_capture_never_takes_main_window_handle_unchecked(script: str) -> None:
    """The first non-zero MainWindowHandle may be the verify dialog."""
    capture = re.search(r"^Save-InstallerWindow\s+(\S+)", _preview_block(script), re.M)
    assert capture is not None, "the preview block no longer captures the wizard"
    assert capture.group(1) == "$wizardHandle", (
        "the preview must capture the handle its poll accepted as the wizard, "
        f"not {capture.group(1)}"
    )


def test_the_poll_accepts_only_a_window_with_the_wizard_cancel_control(script: str) -> None:
    block = _preview_block(script)
    assert "$preview.Refresh()" in block
    assert re.search(r"Test-InstallerWizardWindow\s+\$preview\.MainWindowHandle", block)
    assert re.search(r"\$NsisWizardCancelId\s*=\s*2\b", script)
    predicate = script[script.index("function Test-InstallerWizardWindow") :]
    predicate = predicate[: predicate.index("\n}\n")]
    assert "IsWindowVisible($Handle)" in predicate
    assert "GetDlgItem($Handle, $NsisWizardCancelId)" in predicate


def test_the_wizard_is_rechecked_after_the_settle_sleep(script: str) -> None:
    block = _preview_block(script)
    sleep = block.index("Start-Sleep -Milliseconds 750")
    recheck = block.index("IsWindow($wizardHandle)")
    capture = block.index("Save-InstallerWindow $wizardHandle")
    assert sleep < recheck < capture
