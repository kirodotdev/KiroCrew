"""Contract: one current README and no version-based test clones."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _policy():
    spec = importlib.util.spec_from_file_location("byo_project_hygiene", ROOT / "scripts/project_hygiene.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_tree_follows_one_document_policy():
    assert _policy().problems(ROOT) == []


def test_legacy_docs_and_versioned_test_names_are_rejected(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text("current", encoding="utf-8")
    (tmp_path / "AUDIT_OLD.md").write_text("obsolete", encoding="utf-8")
    (tmp_path / "tests/test_030_duplicate.py").write_text("pass", encoding="utf-8")
    issues = _policy().problems(tmp_path)
    assert any("AUDIT_OLD.md" in issue for issue in issues)
    assert any("test_030_duplicate.py" in issue for issue in issues)
