"""Regression guard: importing the knowledge readers must not load python-docx.

``kiro_crew.knowledge.readers`` sits on the gateway's boot import chain
(``dashboard.server`` reaches it through ``knowledge.ingestion``), and
python-docx plus the lxml it pulls in cost ~75 ms of cumulative import time.
Only ``FileReader._read_docx`` uses it, so it is imported there, on the first
``.docx`` read, instead of at module scope by every gateway start.

The check runs in a fresh interpreter: the warm test process has long since
imported ``docx`` through other tests, so an in-process ``sys.modules``
assertion would be vacuous (same pattern as ``test_cli_lazy_imports.py``).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.knowledge.readers import FileReader


def test_readers_import_does_not_load_docx(tmp_path: Path) -> None:
    code = (
        "import sys; "
        "import kiro_crew.knowledge.readers; "
        "print(repr([m for m in sys.modules if m == 'docx' or m.startswith('docx.')]))"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=tmp_path,
    )
    assert res.returncode == 0, f"import kiro_crew.knowledge.readers failed:\n{res.stderr}"
    present = ast.literal_eval(res.stdout.strip())
    assert present == [], (
        f"importing kiro_crew.knowledge.readers loaded python-docx: {present[:5]}. "
        "Keep the docx imports inside FileReader._read_docx."
    )


def test_docx_read_without_python_docx_reports_missing_dep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the import deferred, an absent python-docx still yields the
    missing-dependency result, not an ImportError out of the read."""
    monkeypatch.setitem(sys.modules, "docx", None)
    path = tmp_path / "doc.docx"
    path.write_bytes(b"PK\x03\x04")

    text, meta = FileReader()._read_docx(str(path))

    assert meta == {"format": "error", "error": "DOCX support requires python-docx"}
    assert text == "DOCX support requires python-docx: pip install python-docx"
