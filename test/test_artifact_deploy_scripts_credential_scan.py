"""The artifact-deploy scripts' credential scans read the redactor's presence check.

``deploy.sh`` and ``_common.sh`` each embed a Python program (a ``PYEOF`` heredoc)
that walks the directory about to be published and refuses the deploy when a
file carries a credential -- "This finding cannot be overridden". Both read the
RAW ``get_credential_patterns()`` with ``pat.search``, and the redactor now
keeps the key that names a value: text it already cleaned reads
``aws_secret_access_key=[REDACTED: credential]``, which the raw patterns match
again, so a deploy whose assets hold redacted text was refused for text that
holds no secret. The programs read ``contains_credential`` instead -- the
redactor's own rule for a tag standing as the value -- and this file runs each
program as the script does (``python3 - <dir>`` on the heredoc's text) against a
directory of redacted text (clean, exit 0) and one holding a live pair (refused,
exit 2, the path named and the secret not).

Every fixture secret is AWS's documented example key; nothing here is live.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.security import REDACTED_CREDENTIAL_TAG, is_sensitive_path
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_SCRIPTS = (
    Path(__file__).resolve().parents[1] / "src/kiro_crew/deploy/skills/artifact-deploy/scripts"
)
_SRC = Path(__file__).resolve().parents[1] / "src"
_EXAMPLE_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # AWS docs example, 40 chars


def _scan_program(script: str) -> str:
    """The credential-scan heredoc of *script*, as the shell hands it to ``python3 -``."""
    text = (_SCRIPTS / script).read_text(encoding="utf-8")
    programs = [
        body
        for body in re.findall(r"<<'PYEOF'\n(.*?)\nPYEOF\n", text, flags=re.DOTALL)
        if "is_sensitive_path" in body
    ]
    assert len(programs) == 1, (script, len(programs))
    return programs[0] + "\n"


def _run_scan(script: str, target: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(_SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return subprocess.run(
        [sys.executable, "-", str(target)],
        input=_scan_program(script),
        env=env,
        capture_output=True,
        timeout=120,
        check=False,
        **UTF8_TEXT,
    )


@pytest.fixture
def publish_root(tmp_path: Path) -> Path:
    root = tmp_path / "site"
    root.mkdir()
    if is_sensitive_path(str(root.resolve())):
        pytest.skip("the scan refuses a sensitive path before reading it; this tmp is one")
    return root


@pytest.mark.parametrize("script", ["deploy.sh", "_common.sh"])
def test_redacted_text_in_the_published_tree_is_not_a_credential(
    script: str, publish_root: Path
) -> None:
    """The redactor's own output -- bare, quoted, and quoted inside a JSON document
    where its quotes are escaped -- is clean to the scan; the deploy proceeds."""
    (publish_root / "notes.env").write_text(
        f"aws_secret_access_key={REDACTED_CREDENTIAL_TAG}\n"
        f'SessionToken="{REDACTED_CREDENTIAL_TAG}" # rotated\n',
        encoding="utf-8",
    )
    (publish_root / "history.json").write_text(
        json.dumps({"text": f'aws_secret_access_key="{REDACTED_CREDENTIAL_TAG}"'}),
        encoding="utf-8",
    )
    (publish_root / "index.html").write_text("<p>ordinary asset</p>", encoding="utf-8")

    result = _run_scan(script, publish_root)

    assert result.returncode == 0, (script, result.stdout, result.stderr)
    assert result.stdout.strip() == "", (script, result.stdout)


@pytest.mark.parametrize("script", ["deploy.sh", "_common.sh"])
def test_a_live_pair_in_the_published_tree_is_still_refused(
    script: str, publish_root: Path
) -> None:
    """A live pair -- bare, and quoted inside a JSON document -- is reported by path,
    the secret never echoed, and the scan exits 2 so the script refuses the deploy."""
    (publish_root / "config.env").write_text(
        f"aws_secret_access_key={_EXAMPLE_SECRET}\n", encoding="utf-8"
    )
    (publish_root / "dump.json").write_text(
        json.dumps({"text": f'aws_secret_access_key="{_EXAMPLE_SECRET}"'}), encoding="utf-8"
    )
    (publish_root / "index.html").write_text("<p>ordinary asset</p>", encoding="utf-8")

    result = _run_scan(script, publish_root)

    assert result.returncode == 2, (script, result.stdout, result.stderr)
    reported = set(result.stdout.split())
    assert reported == {str(publish_root / "config.env"), str(publish_root / "dump.json")}, reported
    assert _EXAMPLE_SECRET not in result.stdout + result.stderr
