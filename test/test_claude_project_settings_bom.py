"""A project's Claude settings file saved with a UTF-8 byte-order mark still loads.

Windows editors save UTF-8 "with BOM" by default, and ``json.loads`` refuses a
leading U+FEFF. The exclusion of a project's ``settings.local.json`` carries the
``deny`` and ``ask`` rules of both project settings files inline, so a file whose
mark made it unreadable dropped those rules.
"""

from __future__ import annotations

import codecs
import json
from pathlib import Path

from kiro_crew.acp import client as acp_client

_BOM = codecs.BOM_UTF8


def _write_bom_json(path: Path, doc: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_BOM + json.dumps(doc).encode("utf-8"))
    return path


class TestClaudeProjectSettings:
    def test_bom_deny_rules_are_carried(self, tmp_path: Path) -> None:
        path = _write_bom_json(
            tmp_path / "settings.json",
            {"permissions": {"deny": ["Bash(rm:*)"], "ask": ["Write"]}},
        )
        assert acp_client._project_settings_restrictions(path) == [
            ("deny", "Bash(rm:*)"),
            ("ask", "Write"),
        ]

    def test_bom_file_with_hooks_withholds_the_exclusion(self, tmp_path: Path) -> None:
        path = _write_bom_json(tmp_path / "settings.json", {"hooks": {"PreToolUse": []}})
        assert acp_client._project_settings_restrictions(path) is None
