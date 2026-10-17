"""Contract tests for the macOS/Linux cloud launcher bootstrap."""

from pathlib import Path

CLOUD_INSTALL_SH = Path(__file__).resolve().parents[1] / "cloud-install.sh"


def test_voice_flag_selects_extra() -> None:
    """``--voice`` must flow through to the editable pip target."""
    script = CLOUD_INSTALL_SH.read_text()

    assert "--voice) WITH_VOICE=1;;" in script
    assert '_pip_target="${REPO_ROOT}[voice]"' in script
    assert 'pip" install -e "$_pip_target"' in script
