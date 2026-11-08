"""Regression guard: the Markdown export parser loads on first export, not at boot.

``dashboard.session_markdown`` is imported on every gateway start, through
``dashboard.server → routes → routes.chat → session_export``, but its CommonMark
parser is only used when a session is exported as Markdown. Building it at module
scope made every start pay for ``markdown_it`` (~35 ms cumulative under
``python -X importtime``) for a route most starts never serve.

The check runs in a subprocess: the test process has long since imported
``markdown_it``, so an in-process ``sys.modules`` assertion would be vacuous.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import kiro_crew

#: The child imports the same ``kiro_crew`` this process does: pytest puts the
#: checkout's ``src`` on ``sys.path``, a bare ``python -c`` child does not.
_SRC = str(Path(kiro_crew.__file__).resolve().parent.parent)


def _modules_after(code: str, cwd: Path) -> list[str]:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, (_SRC, os.environ.get("PYTHONPATH")))),
    }
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=cwd,
        env=env,
    )
    assert res.returncode == 0, res.stderr
    return ast.literal_eval(res.stdout.strip().splitlines()[-1])


def test_importing_session_export_does_not_load_markdown_it(tmp_path: Path) -> None:
    present = _modules_after(
        "import sys; import kiro_crew.dashboard.session_export; "
        "print(repr(sorted(m for m in sys.modules if m.split('.')[0] == 'markdown_it')))",
        tmp_path,
    )
    assert present == [], (
        f"importing session_export loaded {present}: the CommonMark parser must be "
        "built on first export (session_markdown._parser), not at module scope."
    )


def test_first_markdown_export_builds_and_reuses_the_parser(tmp_path: Path) -> None:
    present = _modules_after(
        "import sys; from kiro_crew.dashboard import session_markdown as sm; "
        "p = sm._parser(); assert sm._parser() is p; "
        "assert p.parse('# hi\\n')[0].type == 'heading_open'; "
        "print(repr(['markdown_it'] if 'markdown_it' in sys.modules else []))",
        tmp_path,
    )
    assert present == ["markdown_it"]
