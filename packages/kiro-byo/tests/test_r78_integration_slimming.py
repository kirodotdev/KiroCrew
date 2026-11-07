"""R78: removed dead 0.12 source-pin path; preserve fail-closed admission/rollback."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_byo import integration_manager as m


REL = "src/kiro_crew/agent_sdk/backends.py"


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, inspected: str):
    adapter = tmp_path / "app" / "integration" / "kiro_crew" / "v0.12-plus"
    adapter.mkdir(parents=True)
    package = tmp_path / "crew"
    file_path = package / "agent_sdk" / "backends.py"
    file_path.parent.mkdir(parents=True)
    file_path.write_text("x = 1\n", encoding="utf-8")
    (adapter / "manifest.json").write_text(json.dumps({"files": [REL]}))
    compatibility = {
        "transformer": "adaptive12", "family_regex": r"^0\.12\.0$",
        "semantic_unpinned_files": [REL], "generated_files": [REL],
        "created_files": [], "min_version": "0.12.0",
        # Deliberately invalid pins: 0.12 uses mandatory semantic convergence.
        "pinned_source_sha256": {REL: {"pristine": "0" * 64, "patched": "1" * 64}},
    }
    (adapter / "compatibility.json").write_text(json.dumps(compatibility))
    monkeypatch.setattr(m, "compatibility_contract_issues", lambda *_: [])
    monkeypatch.setattr(m, "rc6_contract_issues", lambda *_: [])
    monkeypatch.setattr(m, "_dashboard_discovery", lambda *_: lambda *_: [])
    seen = []

    def inspect(rel, content):
        seen.append((rel, content))
        return SimpleNamespace(state=inspected, detail="")

    monkeypatch.setattr(m, "_transformer_for", lambda *_: (
        frozenset({REL}), ValueError, inspect, lambda rel, content: content,
    ))
    return adapter, package, seen


def test_semantic_unpinned_inventory_is_mandatory(tmp_path, monkeypatch):
    adapter, package, seen = _fixture(tmp_path, monkeypatch, inspected="patched")
    compat = adapter / "compatibility.json"
    cfg = json.loads(compat.read_text())
    cfg["semantic_unpinned_files"] = []
    compat.write_text(json.dumps(cfg))
    ok, issues, *_ = m._adapter_matches(adapter, "0.12.0", package)
    assert not ok
    assert any("source preflight inventory changed" in item for item in issues)
    assert not seen


def test_patched_unpinned_source_still_must_compile(tmp_path, monkeypatch):
    adapter, package, seen = _fixture(tmp_path, monkeypatch, inspected="patched")
    monkeypatch.setattr(m, "_converge_semantic_transform", lambda *_a, **_kw: "def broken(:\n")
    ok, issues, *_ = m._adapter_matches(adapter, "0.12.0", package)
    assert not ok
    assert len(seen) == 1
    assert any("unverified Crew source variant" in issue for issue in issues)


def test_written_seam_is_tracked_before_atomic_replace_failure(tmp_path, monkeypatch):
    dst = tmp_path / "backends.py"
    dst.write_text("old = 1\n")
    plan = {"dst": dst, "output_text": "new = 2\n"}
    touched = []

    def break_write(text, target):
        assert touched == [plan]  # the rollback owner is recorded before mutating
        raise OSError("simulated power loss")

    monkeypatch.setattr(m, "_atomic_write_text", break_write)
    with pytest.raises(OSError, match="power loss"):
        m._apply_install_plan(plan, touched)
    assert touched == [plan]
    assert dst.read_text() == "old = 1\n"


def test_identical_seams_are_not_touched(tmp_path):
    dst = tmp_path / "backends.py"
    dst.write_text("same = 1\n")
    touched = []
    m._apply_install_plan({"dst": dst, "output_text": "same = 1\n"}, touched)
    src = tmp_path / "source.py"
    src.write_text("same = 1\n")
    m._apply_install_plan({"dst": dst, "output_path": src}, touched)
    assert touched == []
