"""The KAS Node watch script, run the way `kas-node-watch.yml` runs it.

Fixture data only: no network and no `gh`. Each test writes a pin file, an
index.json and an existing-titles file under `tmp_path` and runs the script as
a subprocess, then reads what it printed and whether it wrote a body.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from kiro_crew.acp.kas_pin import KAS_PIN_VERSION
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "kas_node_watch.py"
_REAL_PIN = _REPO_ROOT / "src" / "kiro_crew" / "acp" / "kas_pin.py"

_PIN_SOURCE = '"""Fixture pin."""\n\nKAS_PIN_VERSION = "0.66.117-node22.23.4"\n'
_BEHIND_TITLE = "KAS pin bundles Node 22.23.4; Node 22.23.5 is out"


def _release(version: str) -> dict[str, object]:
    return {"version": version, "date": "2026-11-11", "lts": "Jod", "security": True}


def _run(
    tmp_path: Path,
    index: list[dict[str, object]],
    existing: str = "",
    pin_file: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    if pin_file is None:
        pin_file = tmp_path / "kas_pin.py"
        pin_file.write_text(_PIN_SOURCE, encoding="utf-8")
    index_file = tmp_path / "index.json"
    index_file.write_text(json.dumps(index), encoding="utf-8")
    existing_file = tmp_path / "existing.txt"
    existing_file.write_text(existing, encoding="utf-8")
    body = tmp_path / "body.md"
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--pin-file",
            str(pin_file),
            "--index",
            str(index_file),
            "--existing",
            str(existing_file),
            "--body-out",
            str(body),
        ],
        capture_output=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    return result, body


def test_a_pin_behind_the_newest_of_its_major_files_one_issue(tmp_path: Path) -> None:
    result, body = _run(tmp_path, [_release("v22.23.5"), _release("v22.23.4")])

    assert result.returncode == 0, result.stderr
    assert result.stdout == _BEHIND_TITLE + "\n"
    text = body.read_text(encoding="utf-8")
    assert "22.23.4" in text and "22.23.5" in text
    assert "`KAS_PIN_VERSION` in `src/kiro_crew/acp/kas_pin.py`" in text
    assert '"Node patch release" row' in text and "`packaging/kas-al2/README.md`" in text
    assert "closes this issue itself once the pin is current" in text
    assert "until the next 22.x release" in text
    assert text.rstrip().endswith("<sub>Filed by `.github/workflows/kas-node-watch.yml`.</sub>")
    assert "security" not in text.lower()
    assert "@" not in text


def test_a_pin_on_the_newest_release_files_nothing(tmp_path: Path) -> None:
    result, body = _run(tmp_path, [_release("v22.23.4"), _release("v22.23.3")])

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert not body.exists()


def test_a_title_already_filed_open_or_closed_is_named_but_not_filed_again(
    tmp_path: Path,
) -> None:
    # The workflow keeps an open issue with this title and closes the others.
    existing = f"Some other issue\n{_BEHIND_TITLE}\n"
    result, body = _run(tmp_path, [_release("v22.23.5"), _release("v22.23.4")], existing)

    assert result.returncode == 0, result.stderr
    assert result.stdout == _BEHIND_TITLE + "\n"
    assert not body.exists()


def test_only_the_pins_major_counts_whatever_the_index_order(tmp_path: Path) -> None:
    # `22.9.0` sorts after `22.23.6` as a string and the true newest is not first.
    index = [
        _release("v27.0.0-alpha"),
        _release("v24.11.1"),
        _release("v22.9.0"),
        _release("v22.23.4"),
        _release("v22.23.6"),
        _release("v20.19.9"),
    ]

    result, body = _run(tmp_path, index)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "KAS pin bundles Node 22.23.4; Node 22.23.6 is out\n"
    assert "24.11.1" not in body.read_text(encoding="utf-8")


def test_a_malformed_index_is_an_error_not_a_pass(tmp_path: Path) -> None:
    result, body = _run(tmp_path, [{"date": "2026-11-11"}])

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.startswith("kas_node_watch: ")
    assert not body.exists()


def test_the_real_pin_file_parses_to_its_bundled_node(tmp_path: Path) -> None:
    pinned = KAS_PIN_VERSION.split("-node", 1)[1]
    major = pinned.split(".", 1)[0]
    newer = f"v{major}.999.0"

    result, body = _run(tmp_path, [_release(newer), _release(f"v{pinned}")], pin_file=_REAL_PIN)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"KAS pin bundles Node {pinned}; Node {newer[1:]} is out\n"
    assert body.exists()
