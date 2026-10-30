"""``kirocrew doctor`` names a leftover browser CLI prefix and never touches it.

An earlier version's browser CLI installer wrote ``<data-home>/playwright-cli``.
Nothing reads it any more; doctor's one informational row tells the operator it
can be deleted, prints nothing when it is absent, and deletes nothing itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.doctor_checks import install as doctor_install
from kiro_crew.doctor_checks import render


def test_doctor_says_nothing_when_no_leftover_prefix_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doctor_install._doctor_browser_leftover(tmp_path)
    assert capsys.readouterr().out == ""


def test_doctor_names_a_leftover_prefix_and_leaves_it_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    leftover = tmp_path / doctor_install._LEFTOVER_BROWSER_CLI_LEAF
    leftover.mkdir()
    marker = leftover / "bin"
    marker.mkdir()
    doctor_install._doctor_browser_leftover(tmp_path)
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert "browser:" in out and "left over from an earlier version" in out
    # The doctor prints paths through ``render._safe_display`` (``repr``), so a Windows
    # path shows with escaped backslashes; compare against that rendering.
    assert render._safe_display(str(leftover)) in out
    assert marker.is_dir(), "doctor reports the directory; it never deletes it"
